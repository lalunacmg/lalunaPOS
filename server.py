"""La Luna Grill + Cafe: self-hosted back office + ordering API.
Run with uvicorn server:app --host 127.0.0.1 --port 8000
No cloud credentials are embedded in source or frontend assets.
"""
import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlsplit
from PIL import Image, ImageOps, UnidentifiedImageError
from network_access import lan_addresses, ips as lan_ips_detected, self_health

import qrcode
from receipt_printer import (printer_capabilities, normalize_settings, receipt_html,
                             escpos_bytes, send_to_printer)
from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()  # iPad/iPhone HEIC and HEIF uploads
except ImportError:
    pass  # JPEG/PNG/WebP/GIF are supported even without the optional codec

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get('LALUNA_DB_PATH', str(ROOT / 'laluna.db')))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
# All user-uploaded files live alongside SQLite on the persistent Render disk.
MEDIA_DIR = Path(os.environ.get('LALUNA_MEDIA_DIR', str(DB_PATH.parent / 'laluna-media')))
PRODUCT_MEDIA = MEDIA_DIR / 'products'
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
IMAGE_FILE_RE = re.compile(r'^[0-9a-f]{32}\.webp$')
COOKIE = 'laluna_staff_session'
SECRET = os.environ.get('LALUNA_SESSION_SECRET', '')
MAX_ITEMS = 100
ACTIVE_STATUSES = ('accepted','preparing','ready','completed','delivered')
ORDER_STATUSES = ('pending','accepted','preparing','ready','completed','delivered','cancelled')
PAY_STATUSES = ('unpaid','paid','refunded')
FULFILLMENT = ('Dine-In','Take-Out','Advance Dine-in','Dine-in','Pick-up','Delivery')
BUILD_ID = '2026-11-19-product-image-upload-v1'
FEATURE_RELEASE = '2026-11-19-editable-product-photos-v1'

def public_base_url():
    # Render provides this automatically after deployment. An explicit value
    # overrides it for custom HTTPS domains or other public hosting providers.
    return (os.environ.get('LALUNA_PUBLIC_URL', '').strip() or
            os.environ.get('RENDER_EXTERNAL_URL', '').strip()).rstrip('/')

def public_mode():
    return os.environ.get('LALUNA_PUBLIC_MODE') == '1' or bool(os.environ.get('RENDER'))

login_attempts = defaultdict(list)
app = FastAPI(title='La Luna Grill + Cafe Operations API', docs_url=None, redoc_url=None)

@contextmanager
def db(write=False):
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=15000')
    try:
        if write: c.execute('BEGIN IMMEDIATE')
        yield c
        if write: c.commit()
    except Exception:
        if write: c.rollback()
        raise
    finally: c.close()

def tojson(v): return json.dumps(v, ensure_ascii=False, separators=(',',':'))
def now(): return datetime.now(timezone.utc).isoformat()
def rowdict(x): return dict(x) if x else None
def bad(msg, status=400): raise HTTPException(status, msg)
def password_hash(password):
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 310000)
    return 'pbkdf2_sha256$310000$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(key).decode()
def password_ok(password, hashed):
    try:
        method, rounds, salt, key = hashed.split('$')
        if method != 'pbkdf2_sha256': return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(actual, base64.b64decode(key))
    except (ValueError, TypeError): return False

def init_db():
    with db(write=True) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS staff(id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','manager','cashier')), password_hash TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, staff_id INTEGER NOT NULL REFERENCES staff(id), csrf TEXT NOT NULL, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '🍽️', description TEXT NOT NULL DEFAULT '', price REAL NOT NULL, cost REAL NOT NULL DEFAULT 0, variants TEXT NOT NULL DEFAULT '[]', addons TEXT NOT NULL DEFAULT '[]', popular INTEGER NOT NULL DEFAULT 0, dual INTEGER NOT NULL DEFAULT 0, bundle_qty INTEGER NOT NULL DEFAULT 0, stock INTEGER, active INTEGER NOT NULL DEFAULT 1, customer_visible INTEGER NOT NULL DEFAULT 1 CHECK(customer_visible IN (0,1)), image_file TEXT NOT NULL DEFAULT '', CHECK(price>=0), CHECK(stock IS NULL OR stock>=0));
        CREATE TABLE IF NOT EXISTS product_categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payment_methods(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, instructions TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE, joined TEXT NOT NULL, consent INTEGER NOT NULL DEFAULT 0, stamps INTEGER NOT NULL DEFAULT 0, visits INTEGER NOT NULL DEFAULT 0, token_hash TEXT, lifetime_spend REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, created TEXT NOT NULL, updated TEXT NOT NULL, channel TEXT NOT NULL, fulfillment TEXT NOT NULL, table_number TEXT NOT NULL DEFAULT '', customer_id INTEGER REFERENCES customers(id), customer_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', scheduled TEXT NOT NULL DEFAULT '', payment_method TEXT NOT NULL, payment_status TEXT NOT NULL DEFAULT 'unpaid', status TEXT NOT NULL DEFAULT 'pending', items TEXT NOT NULL, subtotal REAL NOT NULL, delivery_fee REAL NOT NULL DEFAULT 0, total REAL NOT NULL, stock_deducted INTEGER NOT NULL DEFAULT 0, loyalty_awarded INTEGER NOT NULL DEFAULT 0, staff_id INTEGER REFERENCES staff(id));
        CREATE TABLE IF NOT EXISTS inventory_movements(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), change_qty INTEGER NOT NULL, note TEXT NOT NULL, created TEXT NOT NULL, order_id TEXT);
        CREATE TABLE IF NOT EXISTS receipt_printer_settings(id INTEGER PRIMARY KEY CHECK (id=1), method TEXT NOT NULL DEFAULT 'browser', target TEXT NOT NULL DEFAULT '', paper_width INTEGER NOT NULL DEFAULT 58, baud_rate INTEGER NOT NULL DEFAULT 9600, auto_print_pos INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS loyalty_ledger(id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), delta INTEGER NOT NULL, kind TEXT NOT NULL, order_id TEXT, created TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS orders_created ON orders(created);
        CREATE INDEX IF NOT EXISTS orders_customer ON orders(customer_id);
        CREATE INDEX IF NOT EXISTS movements_product ON inventory_movements(product_id);
        ''')
        # New sensitive-tab unlock state; unlock is per staff session, never global.
        session_cols={row['name'] for row in c.execute('PRAGMA table_info(sessions)')}
        if 'admin_unlocked_until' not in session_cols:
            c.execute('ALTER TABLE sessions ADD COLUMN admin_unlocked_until INTEGER NOT NULL DEFAULT 0')
        # Register notes and item/bill discount + tender records are additive migrations.
        order_cols={row['name'] for row in c.execute('PRAGMA table_info(orders)')}
        order_extra={
            'discount_kind':"TEXT NOT NULL DEFAULT ''",
            'discount_scope':"TEXT NOT NULL DEFAULT ''",
            'discount_percent':'REAL NOT NULL DEFAULT 0',
            'discount_amount':'REAL NOT NULL DEFAULT 0',
            'discount_item_indices':"TEXT NOT NULL DEFAULT '[]'",
            'tendered_amount':'REAL NOT NULL DEFAULT 0',
            'change_amount':'REAL NOT NULL DEFAULT 0',
            'paid_at':'TEXT'
        }
        for col,definition in order_extra.items():
            if col not in order_cols:
                c.execute('ALTER TABLE orders ADD COLUMN '+col+' '+definition)
        # Restaurant tables, employee directory, and audited cash-register sessions.
        # Added in place: no existing product, staff, loyalty, or order rows are removed.
        c.executescript('''
        CREATE TABLE IF NOT EXISTS dining_tables(
          id INTEGER PRIMARY KEY, number TEXT NOT NULL UNIQUE COLLATE NOCASE,
          seats INTEGER NOT NULL CHECK(seats BETWEEN 1 AND 30),
          location TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL DEFAULT 'Service Staff',
          pin_hash TEXT, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS register_sessions(
          id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL REFERENCES employees(id),
          opened_by INTEGER NOT NULL REFERENCES staff(id), opened_at TEXT NOT NULL,
          opening_counts TEXT NOT NULL, opening_cents INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('open','closed')),
          closed_by_employee INTEGER REFERENCES employees(id),
          closed_by_staff INTEGER REFERENCES staff(id), closed_at TEXT,
          closing_counts TEXT, closing_cents INTEGER, expected_cents INTEGER,
          variance_cents INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS one_open_register ON register_sessions(status) WHERE status='open';
        ''')
        register_cols={row['name'] for row in c.execute('PRAGMA table_info(register_sessions)')}
        if 'opening_note' not in register_cols:
            c.execute("ALTER TABLE register_sessions ADD COLUMN opening_note TEXT NOT NULL DEFAULT ''")
        if 'closing_note' not in register_cols:
            c.execute("ALTER TABLE register_sessions ADD COLUMN closing_note TEXT NOT NULL DEFAULT ''")
        # Every physical table receives a permanent, opaque QR path. The key
        # survives table renaming, change of seating capacity and app redeploys
        # because it is stored on the same persistent database disk.
        table_columns={row['name'] for row in c.execute('PRAGMA table_info(dining_tables)')}
        if 'qr_key' not in table_columns:
            c.execute('ALTER TABLE dining_tables ADD COLUMN qr_key TEXT')
        if 'deleted_at' not in table_columns:
            c.execute('ALTER TABLE dining_tables ADD COLUMN deleted_at TEXT')
        for row in c.execute("SELECT id FROM dining_tables WHERE qr_key IS NULL OR qr_key='' ").fetchall():
            c.execute('UPDATE dining_tables SET qr_key=? WHERE id=?',
                      (secrets.token_urlsafe(12),row['id']))
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS dining_tables_qr_key ON dining_tables(qr_key)')
        order_columns_extra={row['name'] for row in c.execute('PRAGMA table_info(orders)')}
        if 'guests' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN guests INTEGER NOT NULL DEFAULT 1')
        if 'register_id' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN register_id INTEGER REFERENCES register_sessions(id)')
        if 'employee_id' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN employee_id INTEGER REFERENCES employees(id)')
        if 'handled_by_staff_id' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN handled_by_staff_id INTEGER REFERENCES staff(id)')
        # Stable table identity keeps historical tickets attached when a manager
        # edits a table number. Old printed receipts retain their original label.
        if 'table_id' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN table_id INTEGER REFERENCES dining_tables(id)')
        if 'served_by_employee_id' not in order_columns_extra:
            c.execute('ALTER TABLE orders ADD COLUMN served_by_employee_id INTEGER REFERENCES employees(id)')
        if 'served_by_name' not in order_columns_extra:
            c.execute("ALTER TABLE orders ADD COLUMN served_by_name TEXT NOT NULL DEFAULT ''")
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_table_id ON orders(table_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_table ON orders(table_number)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_orders_register ON orders(register_id)')
        # Immutable monetary entries: sale (+) and refund (-). A UNIQUE order/type
        # pair prevents double-counting when a staff member retries a request.
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sales_ledger(
          id INTEGER PRIMARY KEY,
          order_id TEXT NOT NULL REFERENCES orders(id),
          event_type TEXT NOT NULL CHECK(event_type IN ('payment','refund')),
          amount_cents INTEGER NOT NULL,
          occurred_at TEXT NOT NULL,
          estimated_date INTEGER NOT NULL DEFAULT 0,
          UNIQUE(order_id,event_type)
        );
        CREATE INDEX IF NOT EXISTS idx_sales_ledger_date ON sales_ledger(occurred_at);
        """)

        if c.execute('SELECT COUNT(*) AS n FROM employees').fetchone()['n']==0:
            for staff_name in ('Grace','Jhay','Bong'):
                c.execute('INSERT INTO employees(name,title,created) VALUES(?,?,?)',
                          (staff_name,'Service Staff',now()))
        c.executescript('''
        CREATE TABLE IF NOT EXISTS order_item_edits (
          id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
          staff_id INTEGER NOT NULL REFERENCES staff(id),
          created TEXT NOT NULL, before_items TEXT NOT NULL, after_items TEXT NOT NULL,
          previous_total_cents INTEGER NOT NULL, new_total_cents INTEGER NOT NULL,
          action TEXT NOT NULL CHECK(action IN ('edit','settle'))
        );
        CREATE INDEX IF NOT EXISTS order_item_edits_order ON order_item_edits(order_id);
        ''')
        # Backward-compatible migration; existing restaurant orders and accounts remain intact.
        order_columns={row['name'] for row in c.execute('PRAGMA table_info(orders)')}
        if 'client_order_key' not in order_columns:
            c.execute('ALTER TABLE orders ADD COLUMN client_order_key TEXT')
        if 'client_payload_hash' not in order_columns:
            c.execute('ALTER TABLE orders ADD COLUMN client_payload_hash TEXT')
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_order_key ON orders(client_order_key) WHERE client_order_key IS NOT NULL')
        c.execute("INSERT OR IGNORE INTO receipt_printer_settings(id) VALUES(1)")
        if c.execute('SELECT COUNT(*) n FROM products').fetchone()['n']==0:
            seed=json.loads((ROOT/'menu_seed.json').read_text(encoding='utf-8'))
            for p in seed:
                c.execute('INSERT INTO products(id,category,name,icon,description,price,variants,addons,popular,dual,bundle_qty,stock,active) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                    p['id'],p['category'],p['name'],p['icon'],p['description'],p['price'],tojson(p['variants']),tojson(p['addons']),int(p['popular']),int(p['dual']),int(p.get('bundleQty',0)),p.get('stock'),1))
        # Product visibility and independent categories. Older items remain
        # customer-visible by default; inactive products remain unavailable to POS.
        # Never drop/reseed existing product or transaction rows.
        product_columns={row['name'] for row in c.execute('PRAGMA table_info(products)')}
        if 'customer_visible' not in product_columns:
            c.execute('ALTER TABLE products ADD COLUMN customer_visible INTEGER NOT NULL DEFAULT 1 CHECK(customer_visible IN (0,1))')
        if 'image_file' not in product_columns:
            c.execute("ALTER TABLE products ADD COLUMN image_file TEXT NOT NULL DEFAULT ''")
        c.execute('''CREATE TABLE IF NOT EXISTS product_categories(
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            created TEXT NOT NULL)''')
        # Existing records and any historical/imported categories become selectable.
        for row in c.execute("SELECT DISTINCT category FROM products WHERE trim(category)<>''").fetchall():
            c.execute('INSERT OR IGNORE INTO product_categories(name,created) VALUES(?,?)',
                      (row['category'],now()))
        # Menu update: archive the old Coffee Jelly product once for EXISTING databases.
        # Do not delete it: historical order items and inventory references must survive.
        # A one-time marker avoids resetting later legitimate changes by the owner.
        c.execute('CREATE TABLE IF NOT EXISTS app_migrations(key TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
        # One-time association of legacy tickets; do not rematch unmatched
        # historical orders on subsequent startups (table codes can be reused).
        table_migration='2026-11-17-table-identity-v1'
        if c.execute('SELECT 1 FROM app_migrations WHERE key=?',(table_migration,)).fetchone() is None:
            c.execute('''UPDATE orders SET table_id=(
                SELECT t.id FROM dining_tables t WHERE t.number=orders.table_number COLLATE NOCASE
            ) WHERE table_id IS NULL AND table_number<>'' AND EXISTS(
                SELECT 1 FROM dining_tables t WHERE t.number=orders.table_number COLLATE NOCASE
            )''')
            c.execute('INSERT INTO app_migrations(key,applied_at) VALUES(?,?)',(table_migration,now()))
        migration_key='2026-11-16-archive-coffee-jelly'
        if c.execute('SELECT 1 FROM app_migrations WHERE key=?',(migration_key,)).fetchone() is None:
            c.execute("UPDATE products SET active=0 WHERE lower(trim(name))='coffee jelly'")
            c.execute('INSERT INTO app_migrations(key,applied_at) VALUES(?,?)',(migration_key,now()))
        # Existing order rows have no trustworthy payment timestamp. Backfill
        # only CURRENT paid sales and make estimated dates explicit in reports.
        # Orders already refunded before this migration cannot be dated reliably.
        historical_key='2026-11-17-sales-ledger'
        if c.execute('SELECT 1 FROM app_migrations WHERE key=?',(historical_key,)).fetchone() is None:
            c.execute("""INSERT OR IGNORE INTO sales_ledger(order_id,event_type,amount_cents,occurred_at,estimated_date)
                SELECT id,'payment',CAST(ROUND(total*100) AS INTEGER),created,1
                FROM orders WHERE payment_status='paid' AND status!='cancelled'""")
            c.execute('INSERT INTO app_migrations(key,applied_at) VALUES(?,?)',(historical_key,now()))
        for method,inst in [('Cash','Pay in person on pickup or dine-in'),('GCash/QRPh','Await cashier confirmation before marking paid'),('Bank Transfer','Await confirmation of transfer'),('Card (manual POS)','Processed via your external terminal')]:
            c.execute('INSERT OR IGNORE INTO payment_methods(name,instructions) VALUES(?,?)',(method,inst))
        if c.execute('SELECT COUNT(*) n FROM staff').fetchone()['n']==0:
            email=os.environ.get('LALUNA_ADMIN_EMAIL','').strip().lower()
            password=os.environ.get('LALUNA_ADMIN_PASSWORD','')
            if email and len(password)>=12:
                c.execute('INSERT INTO staff(email,name,role,password_hash) VALUES(?,?,?,?)', (email,'La Luna Owner','owner',password_hash(password)))

@app.on_event('startup')
def startup():
    # Public deployment must not accidentally launch without an owner login,
    # secure session cookies, or a configured HTTPS origin for table QR links.
    if public_mode():
        if not public_base_url().startswith('https://'):
            raise RuntimeError('PUBLIC MODE: supply LALUNA_PUBLIC_URL or RENDER_EXTERNAL_URL (HTTPS).')
        if os.environ.get('LALUNA_FORCE_SECURE_COOKIE') != '1':
            raise RuntimeError('PUBLIC MODE: LALUNA_FORCE_SECURE_COOKIE=1 is required.')
        if not os.environ.get('LALUNA_DB_PATH'):
            raise RuntimeError('PUBLIC MODE: set LALUNA_DB_PATH to persistent storage, e.g. /var/data/laluna.db.')
        if not MEDIA_DIR.resolve().is_relative_to(DB_PATH.parent.resolve()):
            raise RuntimeError('PUBLIC MODE: LALUNA_MEDIA_DIR must be under the same persistent disk directory as LALUNA_DB_PATH.')
        # This code protects sensitive operational tabs; public deployments
        # must never silently run with the secondary admin gate disabled.
        configured_pin = os.environ.get('LALUNA_ADMIN_TAB_PIN', '')
        if not re.fullmatch(r'[0-9]{6,12}', configured_pin):
            raise RuntimeError('PUBLIC MODE: configure LALUNA_ADMIN_TAB_PIN (6-12 digits) in Render Environment; do not put it in GitHub.')
    PRODUCT_MEDIA.mkdir(parents=True, exist_ok=True)
    init_db()
    if public_mode():
        with db() as c:
            if not c.execute("SELECT 1 FROM staff WHERE role='owner' AND active=1 LIMIT 1").fetchone():
                raise RuntimeError('PUBLIC MODE: configure LALUNA_ADMIN_EMAIL and LALUNA_ADMIN_PASSWORD (12+ chars) before first deploy.')


@app.middleware('http')
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
    response.headers['X-Frame-Options']='SAMEORIGIN'
    # Preserve access-specific cache headers for product photos. POS-only
    # photos must never be cached as public resources by intermediaries.
    if not request.url.path.startswith('/media/products/'):
        response.headers['Cache-Control']=('no-store' if (request.url.path.startswith('/api/') or request.url.path in ('/', '/index.html', '/backoffice', '/backoffice/', '/qr-test', '/sw.js') or request.url.path.startswith('/t/') or request.url.path.endswith(('.js','.css'))) else 'public, max-age=60')
    return response

def session_staff(request: Request, c):
    token=request.cookies.get(COOKIE,'')
    if not token: bad('Staff login required',401)
    digest=hashlib.sha256(token.encode()).hexdigest()
    r=c.execute('SELECT s.id,s.name,s.email,s.role,ss.csrf,ss.admin_unlocked_until FROM sessions ss JOIN staff s ON s.id=ss.staff_id WHERE ss.token_hash=? AND ss.expires>? AND s.active=1',(digest,int(time.time()))).fetchone()
    if not r: bad('Session expired; sign in again',401)
    if request.method not in ('GET','HEAD','OPTIONS') and request.headers.get('X-CSRF-Token')!=r['csrf']:
        bad('CSRF token required',403)
    return dict(r)

PROTECTED_ROOTS = ('/api/admin/overview','/api/admin/restaurant-dashboard',
    '/api/admin/sales-ledger.csv','/api/admin/inventory/',
    '/api/admin/customers','/api/admin/loyalty/redeem', '/api/admin/staff')
PROTECTED_MUTATIONS = ('/api/admin/products','/api/admin/categories','/api/admin/employees',
    '/api/admin/payments','/api/admin/printers','/api/admin/qr')

def tab_protection_required(request):
    path=request.url.path
    return (any(path==x or path.startswith(x+'/') for x in PROTECTED_ROOTS)
            or (request.method not in ('GET','HEAD','OPTIONS') and
                any(path==x or path.startswith(x+'/') for x in PROTECTED_MUTATIONS)))

def verify_tab_unlock(request,c,staff):
    if tab_protection_required(request):
        if not os.environ.get('LALUNA_ADMIN_TAB_PIN'):
            # Local developer deployments without a configured PIN retain the
            # original behavior. Public Render deployments must configure it.
            if public_mode():
                bad('Admin tab PIN not configured in Render (LALUNA_ADMIN_TAB_PIN)',503)
            return
        if int(staff.get('admin_unlocked_until') or 0)<int(time.time()):
            bad('Admin PIN required to unlock this section',423)

@app.get('/api/admin/access/state')
def admin_access_state(request:Request):
    with db() as c:
        staff=session_staff(request,c)
        configured=bool(os.environ.get('LALUNA_ADMIN_TAB_PIN'))
        return {'configured':configured,
                'unlocked':(int(staff.get('admin_unlocked_until') or 0)>int(time.time())
                            or (not configured and not public_mode())),
                'expires':int(staff.get('admin_unlocked_until') or 0)}

@app.post('/api/admin/access/unlock')
async def admin_access_unlock(request:Request):
    data=await request.json()
    with db(write=True) as c:
        staff=session_staff(request,c)
        configured=os.environ.get('LALUNA_ADMIN_TAB_PIN','')
        if not configured: bad('Set LALUNA_ADMIN_TAB_PIN in Render environment first',503)
        key='admin-pin:'+str(staff['id'])+':'+(request.client.host if request.client else '')
        recent=[ts for ts in login_attempts[key] if time.time()-ts<900]
        if len(recent)>=5:bad('Too many attempts; try again in 15 minutes',429)
        value=str(data.get('pin',''))
        if not hmac.compare_digest(hashlib.sha256(value.encode()).digest(),
                                   hashlib.sha256(configured.encode()).digest()):
            login_attempts[key]=recent+[time.time()]
            bad('Incorrect admin PIN',403)
        login_attempts.pop(key,None)
        expiry=int(time.time())+600
        token_hash=hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest()
        c.execute('UPDATE sessions SET admin_unlocked_until=? WHERE token_hash=? AND staff_id=?',
                  (expiry,token_hash,staff['id']))
        return {'unlocked':True,'expires':expiry}

def require(request: Request, c, roles=None):
    staff=session_staff(request,c)
    if roles and staff['role'] not in roles: bad('Insufficient permission',403)
    verify_tab_unlock(request,c,staff)
    return staff

def clean_text(s, cap=255): return str(s or '').strip()[:cap]
def digits(s): return re.sub(r'\D','',str(s or ''))
def product_out(row):
    p=dict(row)
    p['variants']=json.loads(p.pop('variants'))
    p['addons']=json.loads(p.pop('addons'))
    p['popular']=bool(p['popular']);p['dual']=bool(p['dual']);p['active']=bool(p['active'])
    p['customer_visible']=bool(p['customer_visible'])
    filename=p.get('image_file') or ''
    p['image_url']=('/media/products/'+filename if IMAGE_FILE_RE.fullmatch(filename) else '')
    p.pop('image_file', None)
    return p

def ensure_price(p, mods):
    """Reprice checkout using catalog values, never trust client supplied prices."""
    mods=clean_text(mods,500)
    base=float(p['price'])
    variants=json.loads(p['variants']); addons=json.loads(p['addons'])
    if p['bundle_qty']:
        # Bundles retain their configured price and optional add-ons.
        detail=mods.split(', ')
        if not detail[0].startswith('Milktea: '): bad('Choose bundle flavors')
        rawflavors=mods.split('Milktea: ',1)[1]
        # No bundle currently seeded; validate against known flavor set.
        flavors=['Taro','Wintermelon','Okinawa','Cookies & Cream','Chocolate']
        count=0
        for token in rawflavors.split(', '):
            m=re.fullmatch(r'(\d+)× (.+)',token)
            n=int(m.group(1)) if m else 1
            flavor=m.group(2) if m else token
            if flavor not in flavors or n<1: bad('Invalid bundle flavor')
            count+=n
        if count!=int(p['bundle_qty']): bad('Incorrect bundle quantity')
        return round(base,2)
    if p['dual']:
        parts=mods.split(', ') if mods else ['']
        chosen=parts[0].split(' + ')
        labels=[x['l'] for x in variants]
        if len(chosen)!=2 or any(x not in labels for x in chosen): bad('Select two wing flavors')
        trailing=parts[1:]
    else:
        parts=mods.split(', ') if mods else []
        labels={v['l']:float(v['p']) for v in variants}
        matches=[x for x in parts if x in labels]
        if variants:
            if len(matches)!=1: bad('Select one menu variant')
            base=labels[matches[0]]
        trailing=[x for x in parts if x not in labels]
    available={a['name']:float(a['price']) for a in addons}
    if len(set(trailing))!=len(trailing) or any(x not in available for x in trailing): bad('Invalid add-on selection')
    return round(base+sum(available[x] for x in trailing),2)

def place_order(c, data, channel='web', staff=None):
    items=data.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=MAX_ITEMS: bad('Add at least one valid item')
    fulfillment=clean_text(data.get('fulfillment'),40)
    if fulfillment not in FULFILLMENT: bad('Choose a valid order type')
    if channel=='web' and fulfillment not in ('Dine-In','Take-Out'):
        bad('Customer orders support Dine-In or Take-Out only')
    payment=clean_text(data.get('payment_method'),100)
    if not (channel=='staff' and payment=='Pending selection') and not c.execute('SELECT 1 FROM payment_methods WHERE name=? AND enabled=1',(payment,)).fetchone(): bad('Select an available payment method')
    customer_name=clean_text(data.get('name'),120)
    phone=digits(data.get('phone'))
    if channel=='web' and (len(customer_name)<2 or not 10<=len(phone)<=15): bad('Enter a valid name and phone number')
    table=clean_text(data.get('table_number'),16)
    table_record=None
    if fulfillment in ('Dine-In','Dine-in') and channel=='web' and not re.fullmatch(r'[A-Za-z0-9_-]{1,12}',table):
        bad('Enter a valid table number (1-12 letters, numbers, dashes or underscores)')
    if fulfillment in ('Take-Out','Pick-up'):
        table=''  # A takeaway order must never appear assigned to a dine-in table.
    try: guests=int(data.get('guests',1))
    except (TypeError,ValueError): bad('Guests must be a whole number')
    if not 1<=guests<=30: bad('Guests must be between 1 and 30')
    if fulfillment in ('Take-Out','Pick-up'): guests=1
    if fulfillment in ('Dine-In','Dine-in'):
        record=c.execute('SELECT * FROM dining_tables WHERE number=? COLLATE NOCASE AND deleted_at IS NULL',(table,)).fetchone() if table else None
        if record and (not record['active'] or record['deleted_at']): bad('This table is inactive. Please ask staff for help',409)
        if record:
            table=record['number']
            table_record=record
            if guests>record['seats']: bad('Guest count exceeds table capacity')
        elif channel in ('pos','staff') and fulfillment=='Dine-In':
            bad('Select a configured active table before recording a Dine-In sale')
        elif channel=='web' and fulfillment=='Dine-In' and c.execute('SELECT 1 FROM dining_tables LIMIT 1').fetchone():
            bad('The table QR is not registered. Please ask restaurant staff for a current QR code',409)
    address_input=data.get('address','')
    if isinstance(address_input,dict):
        text=clean_text(address_input.get('text'),350)
        try:
            lat=float(address_input['lat']);lng=float(address_input['lng'])
        except (ValueError,KeyError,TypeError): bad('Delivery GPS coordinates must be valid')
        if not -90<=lat<=90 or not -180<=lng<=180: bad('Delivery location outside valid coordinates')
        address=tojson({'text':text,'lat':round(lat,6),'lng':round(lng,6)})
    else:
        address=clean_text(address_input,500)
    if fulfillment=='Delivery' and len(address)<5: bad('Enter your delivery address')
    cooked=[]
    subtotal=0
    needed=Counter()
    for item in items:
        if not isinstance(item,dict): bad('Invalid line item')
        try: pid=int(item.get('product_id')); qty=int(item.get('qty'))
        except (ValueError,TypeError): bad('Invalid product or quantity')
        if not 1<=qty<=100: bad('Quantity out of range')
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if not p: bad(f'Product {pid} is unavailable')
        if channel=='web' and not p['customer_visible']: bad(f'Product {pid} is not available on the customer menu',409)
        price=ensure_price(p,item.get('mods',''))
        needed[pid]+=qty
        cooked.append({'product_id':pid,'name':p['name'],'icon':p['icon'],'category':p['category'],'unit_cost':p['cost'],'qty':qty,'mods':clean_text(item.get('mods'),500),'unit_price':price,'total':round(price*qty,2)})
        subtotal+=round(price*qty,2)
    for pid,qty in needed.items():
        p=c.execute('SELECT stock,name FROM products WHERE id=?',(pid,)).fetchone()
        if p['stock'] is not None and p['stock']<qty: bad(f"Not enough stock: {p['name']}",409)
    if fulfillment=='Delivery' and channel in ('pos','staff'):
        # Delivery exists only in staff POS; customer QR checkout still offers
        # Dine-In / Take-Out. Charge exactly the fee confirmed by the cashier.
        if len(phone)<10 or len(phone)>15 or not customer_name:
            bad('POS delivery requires customer name and a valid contact phone number')
        try:
            fee_input=float(data.get('delivery_fee',0) or 0)
        except (ValueError,TypeError):
            bad('Enter a valid delivery fee')
        import math
        if not math.isfinite(fee_input) or not 0<=fee_input<=5000:
            bad('Delivery fee must be between PHP 0 and PHP 5,000')
        fee=round(fee_input,2)
    else:
        # Unpaid customer checkout never fabricates a delivery charge.
        fee=0
    register_id=None
    employee_id=None
    served_by_employee=None
    if channel in ('pos','staff') and data.get('employee_id') not in (None,''):
        try: served_by_id=int(data['employee_id'])
        except (ValueError,TypeError): bad('Select a valid employee')
        served_by_employee=c.execute('SELECT id,name FROM employees WHERE id=? AND active=1',(served_by_id,)).fetchone()
        if not served_by_employee: bad('The selected employee is inactive or unavailable',409)
    if channel=='pos':
        # Cash cannot bypass a configured register. Existing local installations
        # are backward compatible until an employee PIN is configured.
        if payment=='Cash':
            active=c.execute("SELECT id,employee_id FROM register_sessions WHERE status='open'").fetchone()
            if active:
                register_id=active['id'];employee_id=active['employee_id']
            elif c.execute('SELECT 1 FROM employees WHERE active=1 AND pin_hash IS NOT NULL LIMIT 1').fetchone():
                bad('Unlock the cash register with an employee PIN before recording a cash sale',409)
    oid=uuid.uuid4().hex[:16].upper()
    customer_id=None
    if phone:
        customer=c.execute('SELECT id FROM customers WHERE phone=?',(phone,)).fetchone()
        customer_id=customer['id'] if customer else None
    timestamp=now()
    status='completed' if channel=='pos' else 'pending'
    payment_status='paid' if channel=='pos' else 'unpaid'
    c.execute('INSERT INTO orders(id,created,updated,channel,fulfillment,table_number,customer_id,customer_name,phone,notes,address,scheduled,payment_method,payment_status,status,items,subtotal,delivery_fee,total,staff_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
        oid,timestamp,timestamp,channel,fulfillment,table,customer_id,customer_name,phone,clean_text(data.get('notes'),500),address,clean_text(data.get('scheduled'),80),payment,payment_status,status,tojson(cooked),round(subtotal,2),fee,round(subtotal+fee,2),staff['id'] if staff else None))
    c.execute('''UPDATE orders SET guests=?,register_id=?,employee_id=?,table_id=?,
              served_by_employee_id=?,served_by_name=? WHERE id=?''',
              (guests,register_id,employee_id,table_record['id'] if table_record else None,
               served_by_employee['id'] if served_by_employee else None,
               served_by_employee['name'] if served_by_employee else '',oid)) 
    if channel=='pos':
        c.execute("INSERT INTO sales_ledger(order_id,event_type,amount_cents,occurred_at) VALUES(?,'payment',?,?)",
                  (oid,round((subtotal+fee)*100),timestamp))
    if channel=='pos':
        adjust_order_stock(c,oid,False,True)
        reward_if_eligible(c,oid)
    return {'order_id':oid,'status':status,'payment_status':payment_status,'subtotal':round(subtotal,2),'delivery_fee':fee,'total':round(subtotal+fee,2),'items':cooked}

def adjust_order_stock(c,oid,already,new_state):
    if bool(already)==bool(new_state): return
    order=c.execute('SELECT items FROM orders WHERE id=?',(oid,)).fetchone()
    changes=Counter()
    for x in json.loads(order['items']): changes[x['product_id']]+=int(x['qty'])
    for pid,qty in changes.items():
        stock=c.execute('SELECT stock,name FROM products WHERE id=?',(pid,)).fetchone()
        if stock and stock['stock'] is not None:
            delta=-qty if new_state else qty
            if stock['stock']+delta<0: bad('Insufficient inventory to accept order',409)
            c.execute('UPDATE products SET stock=stock+? WHERE id=?',(delta,pid))
            c.execute('INSERT INTO inventory_movements(product_id,change_qty,note,created,order_id) VALUES(?,?,?,?,?)',(pid,delta,'Order accepted' if new_state else 'Order cancelled',now(),oid))
    c.execute('UPDATE orders SET stock_deducted=? WHERE id=?',(int(new_state),oid))

def reward_if_eligible(c,oid):
    o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if o['loyalty_awarded'] or o['payment_status']!='paid' or o['status'] not in ('completed','delivered'): return
    if o['customer_id']:
        cust=c.execute('SELECT consent FROM customers WHERE id=?',(o['customer_id'],)).fetchone()
        if cust and cust['consent']:
            c.execute('UPDATE customers SET stamps=stamps+1, visits=visits+1,lifetime_spend=lifetime_spend+? WHERE id=?',(o['total'],o['customer_id']))
            c.execute('INSERT INTO loyalty_ledger(customer_id,delta,kind,order_id,created) VALUES(?,?,?,?,?)',(o['customer_id'],1,'purchase',oid,now()))
    c.execute('UPDATE orders SET loyalty_awarded=1 WHERE id=?',(oid,))

def order_out(r):
    o=dict(r);o['items']=json.loads(o['items']);return o

@app.head('/')
def root_head(): return Response(status_code=200)
@app.get('/')
@app.get('/index.html')
def root(): return FileResponse(ROOT/'static'/'index.html')
@app.head('/backoffice')
@app.head('/backoffice/')
def backoffice_head(): return Response(status_code=200)
@app.get('/backoffice')
@app.get('/backoffice/')
@app.get('/backoffice.html')
def backoffice(): return FileResponse(ROOT/'static'/'backoffice.html')
@app.get('/sw.js')
def service_worker(): return FileResponse(ROOT/'static'/'sw.js',media_type='text/javascript',headers={'Service-Worker-Allowed':'/'})
@app.get('/api/health')
def health():
    with db() as c: ready=c.execute("SELECT COUNT(*) n FROM staff WHERE role='owner'").fetchone()['n']>0
    return {'ok':True,'build':BUILD_ID,'features_release':FEATURE_RELEASE,'configured':ready,'checkout_protocol':'direct-orders-v1',
            'order_types':['Dine-In','Take-Out'],'provider':'Local delivery',
            'timezone':'Asia/Manila','lan_access':os.environ.get('LALUNA_ALLOW_WIFI')=='1',
            'public_access':public_mode() and bool(public_base_url()),
            'admin_tab_pin_configured':bool(os.environ.get('LALUNA_ADMIN_TAB_PIN'))}
def current_menu(c):
    # The current catalog is read from the same persistent database used by
    # the back office. No product or menu information is encoded in table QRs.
    products=[product_out(x) for x in c.execute('SELECT * FROM products WHERE active=1 AND customer_visible=1 ORDER BY id')]
    payments=[dict(x) for x in c.execute('SELECT id,name,instructions FROM payment_methods WHERE enabled=1 ORDER BY id')]
    return {'products':products, 'payments':payments}

def current_menu_revision(menu_data):
    # Content-derived revision also detects changes from inventory, staff edits,
    # or a restored database without requiring a schema migration or triggers.
    return hashlib.sha256(tojson(menu_data).encode('utf-8')).hexdigest()[:24]

# Public, versioned image paths use random filenames; private POS-only product
# photos additionally require an authenticated back-office login.
@app.get('/media/products/{filename}', include_in_schema=False)
def product_image(filename: str, request: Request):
    if not IMAGE_FILE_RE.fullmatch(filename):
        bad('Image not found', 404)
    with db() as c:
        row=c.execute('SELECT active,customer_visible FROM products WHERE image_file=?',(filename,)).fetchone()
        if not row: bad('Image not found',404)
        if not (row['active'] and row['customer_visible']):
            session_staff(request,c)
    path=PRODUCT_MEDIA / filename
    if not path.is_file(): bad('Image file missing from persistent storage',404)
    return FileResponse(path,media_type='image/webp',headers={'Cache-Control':'public, max-age=31536000, immutable' if row['active'] and row['customer_visible'] else 'private, no-store'})

def prepare_product_image(data:bytes):
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.format not in ('JPEG','PNG','WEBP','GIF','HEIF','HEIC'):
                bad('Use a JPEG, PNG, WebP, GIF or HEIC image')
            if original.width*original.height>MAX_IMAGE_PIXELS:
                bad('Image dimensions are too large (limit: 24 megapixels)',413)
            original.seek(0)  # animated image: take its first frame
            original.load()
            normalized=ImageOps.exif_transpose(original)
            image=normalized.convert('RGBA' if 'A' in normalized.getbands() else 'RGB')
            image.thumbnail((1200,1200),Image.Resampling.LANCZOS)
            output=io.BytesIO()
            image.save(output,format='WEBP',quality=84,method=4)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        bad('Image could not be decoded; use a valid JPEG, PNG, WebP or HEIC photo')

@app.put('/api/admin/products/{pid}/image')
async def upload_product_image(pid:int,request:Request):
    # Stream and cap requests instead of buffering an unlimited upload.
    with db() as c:
        require(request,c,('owner','manager'))
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():
            bad('Product not found',404)
    media_type=request.headers.get('content-type','').split(';',1)[0].strip().lower()
    if media_type not in ('image/jpeg','image/png','image/webp','image/gif','image/heic','image/heif'):
        bad('Unsupported image type. Choose JPEG, PNG, WebP, GIF or HEIC',415)
    if int(request.headers.get('content-length') or 0)>MAX_IMAGE_BYTES: bad('Image exceeds 8 MB',413)
    chunks=[];length=0
    async for chunk in request.stream():
        length+=len(chunk)
        if length>MAX_IMAGE_BYTES: bad('Image exceeds 8 MB',413)
        chunks.append(chunk)
    if not length:bad('Choose an image first')
    converted=prepare_product_image(b''.join(chunks))
    PRODUCT_MEDIA.mkdir(parents=True,exist_ok=True)
    filename=uuid.uuid4().hex+'.webp'
    path=PRODUCT_MEDIA/filename
    try:
        path.write_bytes(converted)
        with db(write=True) as c:
            require(request,c,('owner','manager'))
            old=c.execute('SELECT image_file FROM products WHERE id=?',(pid,)).fetchone()
            if not old:bad('Product not found',404)
            c.execute('UPDATE products SET image_file=? WHERE id=?',(filename,pid))
            former=old['image_file']
        if IMAGE_FILE_RE.fullmatch(former) and former!=filename:
            with db() as c:
                still_used=c.execute('SELECT 1 FROM products WHERE image_file=?',(former,)).fetchone()
            if not still_used:(PRODUCT_MEDIA/former).unlink(missing_ok=True)
        return {'ok':True,'image_url':'/media/products/'+filename}
    except Exception:
        # Never retain a generated file when its database update failed.
        with db() as c:
            saved=c.execute('SELECT 1 FROM products WHERE image_file=?',(filename,)).fetchone()
        if not saved:path.unlink(missing_ok=True)
        raise

@app.delete('/api/admin/products/{pid}/image')
def remove_product_image(pid:int,request:Request):
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        row=c.execute('SELECT image_file FROM products WHERE id=?',(pid,)).fetchone()
        if not row:bad('Product not found',404)
        old=row['image_file']
        c.execute("UPDATE products SET image_file='' WHERE id=?",(pid,))
    if IMAGE_FILE_RE.fullmatch(old):
        with db() as c:
            still_used=c.execute('SELECT 1 FROM products WHERE image_file=?',(old,)).fetchone()
        if not still_used:(PRODUCT_MEDIA/old).unlink(missing_ok=True)
    return {'ok':True,'image_url':''}

@app.get('/api/menu/revision')
def menu_revision():
    with db() as c:
        data=current_menu(c)
    return {'revision':current_menu_revision(data)}

@app.get('/api/menu')
def menu():
    with db() as c:
        data=current_menu(c)
    return {**data,'revision':current_menu_revision(data)}
@app.post('/api/orders', status_code=201)
async def order_submit(request:Request):
    raw=await request.body()
    if len(raw)>48*1024: bad('Order request too large',413)
    try: data=json.loads(raw)
    except (ValueError,UnicodeDecodeError): bad('Invalid JSON order')
    if not isinstance(data,dict): bad('Invalid request')
    # Older clients may omit the key; the updated storefront always sends one.
    key=data.get('client_order_key')
    if key is not None and (not isinstance(key,str) or not re.fullmatch(r'[0-9a-f-]{36}',key,re.IGNORECASE)):
        bad('Invalid order request key')
    fingerprint=hashlib.sha256(tojson({k:v for k,v in sorted(data.items()) if k!='client_order_key'}).encode('utf-8')).hexdigest() if key else None
    with db(write=True) as c:
        if key:
            existing=c.execute('SELECT id,status,payment_status,subtotal,delivery_fee,total,items,client_payload_hash FROM orders WHERE client_order_key=?',(key,)).fetchone()
            if existing:
                if existing['client_payload_hash']!=fingerprint: bad('Order request key was already used for a different cart',409)
                return {'order_id':existing['id'],'status':existing['status'],'payment_status':existing['payment_status'],'subtotal':existing['subtotal'],'delivery_fee':existing['delivery_fee'],'total':existing['total'],'items':json.loads(existing['items']),'already_received':True}
        result=place_order(c,data)
        if key: c.execute('UPDATE orders SET client_order_key=?,client_payload_hash=? WHERE id=?',(key,fingerprint,result['order_id']))
        return result
@app.get('/api/orders/{oid}/status')
def order_status(oid:str):
    with db() as c:
        o=c.execute('SELECT id,status,payment_status,fulfillment,table_number,total,created FROM orders WHERE id=?',(oid.upper(),)).fetchone()
        if not o: bad('Order not found',404)
        return dict(o)

@app.post('/api/loyalty/join', status_code=201)
async def loyalty_join(request:Request):
    data=await request.json()
    if not isinstance(data,dict): bad('Invalid request')
    name=clean_text(data.get('name'),120);phone=digits(data.get('phone'))
    if not 2<=len(name)<=120 or not 10<=len(phone)<=15: bad('Enter a valid name and phone')
    if data.get('consent') is not True: bad('Consent is required to join loyalty')
    token=secrets.token_urlsafe(32)
    digest=hashlib.sha256(token.encode()).hexdigest()
    with db(write=True) as c:
        existing=c.execute('SELECT id,token_hash FROM customers WHERE phone=?',(phone,)).fetchone()
        if existing: bad('This number is already registered. Ask staff to help recover your card.',409)
        c.execute('INSERT INTO customers(name,phone,joined,consent,token_hash) VALUES(?,?,?,?,?)',(name,phone,now(),1,digest))
        return {'card_token':token,'name':name,'stamps':0,'visits':0,'goal':8,'note':'Card token is stored on your device. Phone ownership is not verified.'}
@app.get('/api/loyalty/card')
def loyalty_card(token:str=''):
    if not token: bad('Loyalty card token required',401)
    digest=hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        r=c.execute('SELECT name,stamps,visits,joined FROM customers WHERE token_hash=?',(digest,)).fetchone()
        if not r: bad('Card not found',404)
        return {**dict(r),'goal':8,'available_rewards':int(r['stamps'])//8}

@app.post('/api/auth/login')
async def login(request:Request,response:Response):
    data=await request.json()
    email=clean_text(data.get('email'),200).lower();password=str(data.get('password',''))
    remote=request.client.host if request.client else 'unknown'
    key=f'{remote}:{email}'
    recent=[t for t in login_attempts[key] if time.time()-t<900]
    if len(recent)>=10: bad('Too many attempts, try again later',429)
    with db(write=True) as c:
        r=c.execute('SELECT * FROM staff WHERE email=? AND active=1',(email,)).fetchone()
        if not r or not password_ok(password,r['password_hash']):
            login_attempts[key]=recent+[time.time()]
            bad('Invalid email or password',401)
        login_attempts.pop(key,None)
        token=secrets.token_urlsafe(48);csrf=secrets.token_urlsafe(28)
        c.execute('INSERT INTO sessions(token_hash,staff_id,csrf,expires) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),r['id'],csrf,int(time.time())+28800))
    secure=request.url.scheme=='https' or os.environ.get('LALUNA_FORCE_SECURE_COOKIE')=='1'
    response.set_cookie(COOKIE,token,httponly=True,secure=secure,samesite='lax',max_age=28800,path='/')
    return {'name':r['name'],'role':r['role'],'csrf':csrf}
@app.get('/api/auth/me')
def whoami(request:Request):
    with db() as c:return require(request,c)
@app.post('/api/auth/logout')
def logout(request:Request,response:Response):
    with db(write=True) as c:
        staff=require(request,c)
        token=request.cookies.get(COOKIE,'')
        c.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie(COOKIE,path='/');return {'ok':True}

PH_TZ = timezone(timedelta(hours=8))

def ph_datetime(iso):
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:  # Old migrated rows may use naive UTC timestamps.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PH_TZ)

def sales_accounting(c):
    """All monetary summaries derive from the same immutable ledger (cents)."""
    events=c.execute('SELECT * FROM sales_ledger ORDER BY occurred_at,id').fetchall()
    gross_cents=sum(int(e['amount_cents']) for e in events if e['event_type']=='payment')
    refund_cents=-sum(int(e['amount_cents']) for e in events if e['event_type']=='refund')
    net_cents=gross_cents-refund_cents
    orders=c.execute("SELECT * FROM orders WHERE payment_status='paid' AND status!='cancelled'").fetchall()
    expected_cents=sum(round(float(o['total'])*100) for o in orders)
    legacy_refunded=c.execute("""SELECT COUNT(*) AS n FROM orders o
         WHERE o.payment_status='refunded' AND NOT EXISTS(
           SELECT 1 FROM sales_ledger l WHERE l.order_id=o.id)""").fetchone()['n']
    return {'orders':orders,'events':events,'net_cents':net_cents,'gross_cents':gross_cents,
            'refund_cents':refund_cents,'difference_cents':net_cents-expected_cents,
            'reconciled':net_cents==expected_cents,
            'legacy_date_estimates':sum(int(e['estimated_date']) for e in events),
            'legacy_unreconstructable_refunds':legacy_refunded}

@app.get('/api/admin/overview')
def overview(request:Request):
    with db() as c:
        require(request,c)
        accounting=sales_accounting(c)
        orders=accounting['orders']
        today_ph=datetime.now(PH_TZ).date()
        daily=defaultdict(lambda:{'orders':0,'revenue_cents':0})
        selling=Counter()
        # Revenue belongs to the date the payment/refund was recorded, not the
        # date the customer originally requested an unpaid table order.
        for e in accounting['events']:
            day=ph_datetime(e['occurred_at']).date().isoformat()
            daily[day]['revenue_cents']+=int(e['amount_cents'])
            if e['event_type']=='payment':daily[day]['orders']+=1
            elif e['event_type']=='refund':daily[day]['orders']-=1
        for o in orders:
            for item in json.loads(o['items']):selling[item['name']]+=item['qty']
        pending=c.execute("SELECT COUNT(*) n FROM orders WHERE status IN ('pending','accepted','preparing','ready') AND payment_status!='refunded'").fetchone()['n']
        customers=c.execute('SELECT COUNT(*) n FROM customers').fetchone()['n']
        new_customers=c.execute('SELECT COUNT(*) n FROM customers WHERE joined>=?',((datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),)).fetchone()['n']
        low=c.execute('SELECT id,name,stock FROM products WHERE stock IS NOT NULL AND stock<=5 AND active=1 ORDER BY stock').fetchall()
        days=[(today_ph-timedelta(days=i)).isoformat() for i in range(29,-1,-1)]
        money=lambda v:round(v/100,2)
        return {'revenue':money(accounting['net_cents']),'paid_orders':len(orders),
                'average_order':round(money(sum(round(o['total']*100) for o in orders))/len(orders),2) if orders else 0,
                'today_revenue':money(daily[today_ph.isoformat()]['revenue_cents']),
                'pending_orders':pending,'customers':customers,'new_customers_30d':new_customers,
                'daily':[{'date':k,'revenue':money(daily[k]['revenue_cents']),'orders':daily[k]['orders']} for k in days],
                'best_sellers':[{'name':k,'units':v} for k,v in selling.most_common(8)],
                'low_stock':[dict(x) for x in low],
                'sales_audit':{k:v for k,v in accounting.items() if k in (
                     'reconciled','difference_cents','legacy_date_estimates','legacy_unreconstructable_refunds')}
                    | {'gross_payments':money(accounting['gross_cents']),
                       'refunds':money(accounting['refund_cents']),
                       'net_sales':money(accounting['net_cents']),
                       'as_of_utc':now(),'timezone':'Asia/Manila',
                       'source':'Render/local SQLite transaction ledger'}}
@app.get('/api/admin/sales-ledger.csv')
def export_sales_ledger(request:Request):
    # Restricted, full transaction export for reconciling real payments, not a
    # truncated or hand-typed sample chart. No customer phone numbers included.
    with db() as c:
        require(request,c)
        rows=c.execute("""SELECT l.*,o.payment_method,o.channel,o.fulfillment,
                        o.payment_status,o.status
                        FROM sales_ledger l JOIN orders o ON o.id=l.order_id
                        ORDER BY l.occurred_at,l.id""").fetchall()
    target=io.StringIO(newline='')
    writer=csv.writer(target)
    writer.writerow(['Timestamp UTC','Philippine business date','Order ID','Transaction','Amount PHP',
                     'Payment method','Channel','Order type','Current payment status','Current order status',
                     'Date estimated from historical order'])
    for r in rows:
        def cell(value):
            v=str(value)
            return "'"+v if v and v[0] in ('=','+','-','@') else v
        writer.writerow([r['occurred_at'],ph_datetime(r['occurred_at']).date().isoformat(),r['order_id'],
                         r['event_type'],f"{r['amount_cents']/100:.2f}",cell(r['payment_method']),
                         cell(r['channel']),cell(r['fulfillment']),cell(r['payment_status']),
                         cell(r['status']),int(r['estimated_date'])])
    return Response(content=target.getvalue(),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':'attachment; filename="laluna-sales-ledger.csv"',
                             'Cache-Control':'no-store'})

@app.get('/api/admin/orders')
def list_orders(request:Request,limit:int=120,table_id:int|None=None):
    with db() as c:
        require(request,c)
        limit=max(1,min(limit,500))
        table_detail=None
        if table_id is not None:
            if table_id<1: bad('Invalid table id',422)
            t=c.execute('SELECT id,number,seats,location,active FROM dining_tables WHERE id=?',(table_id,)).fetchone()
            if not t:bad('Table not found',404)
            table_detail=dict(t)
            # Include pre-migration orders which still have the same code; all
            # new orders have table_id, so renames don't mix up histories.
            where="(o.table_id=? OR (o.table_id IS NULL AND o.table_number=? COLLATE NOCASE))"
            values=(table_id,t['number'])
            rows=c.execute('SELECT o.* FROM orders o WHERE '+where+' ORDER BY o.created DESC LIMIT ?',(*values,limit)).fetchall()
            total=c.execute('SELECT COUNT(*) n FROM orders o WHERE '+where,values).fetchone()['n']
        else:
            rows=c.execute('SELECT * FROM orders ORDER BY created DESC LIMIT ?',(limit,)).fetchall()
            total=c.execute('SELECT COUNT(*) n FROM orders').fetchone()['n']
        return {'orders':[order_out(o) for o in rows],'total':total,'table':table_detail}
@app.post('/api/admin/pos-tickets',status_code=201)
async def create_pos_ticket(request:Request):
    data=await request.json()
    with db(write=True) as c:
        staff=require(request,c,('owner','manager','cashier'))
        # Staff POS no longer accepts tender; payment belongs to Orders.
        data={**data,'payment_method':'Pending selection'}
        return place_order(c,data,'staff',staff)

@app.post('/api/admin/pos',status_code=201)
async def pos(request:Request):
    # Legacy local POS API remains for compatibility with older local tooling,
    # but PUBLIC deployments must route every paid transaction through Orders.
    if public_mode():bad('Use POS to create an unpaid ticket, then complete payment in Orders',409)
    data=await request.json()
    with db(write=True) as c: return place_order(c,data,'pos',require(request,c,('owner','manager','cashier')))
def edit_unpaid_table_ticket(c, oid, data, staff):
    '''Atomic delta changes to one Dine-In ticket, preserving old line prices.

    Payment/finalization, if requested, is in the SAME database transaction.
    An edit never creates a new order and never changes paid historical sales.
    '''
    o=c.execute('SELECT * FROM orders WHERE id=?',(oid.upper(),)).fetchone()
    if not o:bad('Order not found',404)
    if o['fulfillment']!='Dine-In' or not o['table_id']:
        bad('Only configured Dine-In table tickets can be edited',409)
    if o['payment_status']!='unpaid' or o['status'] not in ('pending','accepted','preparing','ready'):
        bad('Paid, cancelled and closed tickets cannot be edited; start a new order if necessary',409)
    table=c.execute('SELECT * FROM dining_tables WHERE id=?',(o['table_id'],)).fetchone()
    if not table or not table['active'] or table['deleted_at']:
        bad('Table is unavailable',409)
    if data.get('expected_updated')!=o['updated']:
        bad('Ticket changed on another device. Refresh this table before editing',409)
    changes=data.get('changes')
    if not isinstance(changes,list) or len(changes)>MAX_ITEMS:
        bad('Send a valid list of item changes')
    before=json.loads(o['items'])
    items=[dict(x) for x in before]
    delta_stock=Counter()
    for change in changes:
        if not isinstance(change,dict):bad('Invalid ticket change')
        if type(change.get('delta')) is not int or type(change.get('product_id')) is not int:
            bad('Product and quantity changes must be whole numbers')
        pid=change['product_id'];delta=change['delta']
        if delta==0 or abs(delta)>100:bad('Quantity change out of range')
        mods=clean_text(change.get('mods'),500)
        if pid<1:bad('Invalid product')
        if delta>0:
            product=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
            if not product:bad('Product is unavailable',409)
            price=ensure_price(product,mods)
            # Keep original prices on existing units, including unchanged items.
            matching=next((x for x in items if x['product_id']==pid and x['mods']==mods and
                 round(float(x['unit_price'])*100)==round(price*100) and x['qty']+delta<=100),None)
            if matching:
                matching['qty']+=delta
                matching['total']=round(matching['qty']*float(matching['unit_price']),2)
            else:
                items.append({'product_id':pid,'name':product['name'],'icon':product['icon'],
                    'category':product['category'],'unit_cost':product['cost'],'qty':delta,
                    'mods':mods,'unit_price':price,'total':round(price*delta,2)})
        else:
            remaining=-delta
            for item in reversed(items):
                if item['product_id']!=pid or item['mods']!=mods:continue
                taken=min(int(item['qty']),remaining)
                item['qty']-=taken
                remaining-=taken
                item['total']=round(int(item['qty'])*float(item['unit_price']),2)
                if not remaining:break
            if remaining:bad('Cannot remove more items than this table ticket contains',409)
            items=[x for x in items if x['qty']>0]
        delta_stock[pid]+=delta
    if not items or len(items)>MAX_ITEMS:
        bad('A table ticket must contain at least one item; cancel an empty ticket',409)
    if any(x['qty']<1 or x['qty']>100 for x in items):bad('Item quantity out of range',409)
    payment=clean_text(data.get('payment_method',o['payment_method']),100)
    if payment!='Pending selection' and not c.execute('SELECT 1 FROM payment_methods WHERE name=? AND enabled=1',(payment,)).fetchone():
        bad('Choose an available payment method')
    try: guests=int(data.get('guests',o['guests']))
    except (TypeError,ValueError):bad('Enter a valid diner count')
    if not 1<=guests<=table['seats']:bad('Diner count exceeds this table capacity')
    customer_name=clean_text(data.get('name',o['customer_name']),120)
    phone=digits(data.get('phone',o['phone']))
    if phone and not 10<=len(phone)<=15:bad('Customer contact number must have 10 to 15 digits')
    notes=clean_text(data.get('notes',o['notes']),500)
    customer=c.execute('SELECT id FROM customers WHERE phone=?',(phone,)).fetchone() if phone else None
    customer_id=customer['id'] if customer else None
    new_employee_id=o['served_by_employee_id'];new_employee_name=o['served_by_name']
    if 'employee_id' in data:
        selected=data['employee_id']
        if selected in (None,''):
            new_employee_id=None;new_employee_name=''
        else:
            try:selected=int(selected)
            except (TypeError,ValueError):bad('Select an active employee')
            employee=c.execute('SELECT id,name FROM employees WHERE id=? AND active=1',(selected,)).fetchone()
            if not employee:bad('Selected employee is unavailable',409)
            new_employee_id=employee['id'];new_employee_name=employee['name']
    settle=data.get('complete_paid',False)
    if not isinstance(settle,bool):bad('Invalid payment confirmation')
    if settle and public_mode():bad('Complete payments in Orders so tender, method and discounts are recorded',409)
    register_id=o['register_id'];drawer_employee_id=o['employee_id']
    if settle and payment=='Cash':
        drawer=c.execute("SELECT id,employee_id FROM register_sessions WHERE status='open'").fetchone()
        if drawer:
            register_id=drawer['id'];drawer_employee_id=drawer['employee_id']
        elif c.execute('SELECT 1 FROM employees WHERE active=1 AND pin_hash IS NOT NULL').fetchone():
            bad('Open the Cash Register before collecting a cash payment',409)
    # Strict stock changes: unpaid accepted/preparing tickets have already been
    # reserved; pending tickets only consume stock when payment finalizes them.
    for pid,delta in delta_stock.items():
        if not delta:continue
        product=c.execute('SELECT stock,name FROM products WHERE id=?',(pid,)).fetchone()
        if product and product['stock'] is not None:
            if o['stock_deducted'] and product['stock']-delta<0:
                bad('Insufficient stock for '+product['name'],409)
            if not o['stock_deducted'] and delta>0:
                remaining=sum(int(x['qty']) for x in items if x['product_id']==pid)
                if remaining>product['stock']:bad('Insufficient stock for '+product['name'],409)
            if o['stock_deducted']:
                c.execute('UPDATE products SET stock=stock-? WHERE id=?',(delta,pid))
                c.execute('INSERT INTO inventory_movements(product_id,change_qty,note,created,order_id) VALUES(?,?,?,?,?)',
                     (pid,-delta,'Table ticket item edit',now(),oid.upper()))
    subtotal=round(sum(round(float(x['total'])*100) for x in items)/100,2)
    new_total=round(subtotal+float(o['delivery_fee']),2)
    changed_at=now()
    c.execute('''UPDATE orders SET items=?,subtotal=?,total=?,payment_method=?,
        served_by_employee_id=?,served_by_name=?,guests=?,customer_name=?,phone=?,notes=?,
        customer_id=?,updated=?,handled_by_staff_id=? WHERE id=?''',
        (tojson(items),subtotal,new_total,payment,new_employee_id,new_employee_name,guests,customer_name,
         phone,notes,customer_id,changed_at,staff['id'],oid.upper()))
    if settle:
        if not o['stock_deducted']:
            adjust_order_stock(c,oid.upper(),False,True)
        c.execute('''UPDATE orders SET payment_status='paid',status='completed',register_id=?,
                   employee_id=? WHERE id=?''',(register_id,drawer_employee_id,oid.upper()))
        c.execute("INSERT INTO sales_ledger(order_id,event_type,amount_cents,occurred_at) VALUES(?,'payment',?,?)",
                  (oid.upper(),round(new_total*100),changed_at))
        reward_if_eligible(c,oid.upper())
    c.execute('''INSERT INTO order_item_edits(order_id,staff_id,created,before_items,after_items,
        previous_total_cents,new_total_cents,action) VALUES(?,?,?,?,?,?,?,?)''',
        (oid.upper(),staff['id'],changed_at,tojson(before),tojson(items),round(o['total']*100),
         round(new_total*100),'settle' if settle else 'edit'))
    return {'ok':True,'order_id':oid.upper(),'updated':changed_at,'items':items,
        'total':new_total,'payment_status':'paid' if settle else 'unpaid',
        'status':'completed' if settle else o['status'],'employee_name':new_employee_name}

@app.patch('/api/admin/table-orders/{oid}/items')
async def update_table_items(oid:str,request:Request):
    data=await request.json()
    if not isinstance(data,dict):bad('Invalid ticket update')
    with db(write=True) as c:
        staff=require(request,c,('owner','manager','cashier'))
        return edit_unpaid_table_ticket(c,oid,data,staff)

@app.post('/api/admin/table-orders',status_code=201)
async def submit_table_ticket(request:Request):
    data=await request.json()
    if data.get('fulfillment')!='Dine-In':bad('Table tickets must be Dine-In')
    with db(write=True) as c:
        staff=require(request,c)
        if not c.execute('SELECT 1 FROM dining_tables WHERE number=? COLLATE NOCASE AND active=1 AND deleted_at IS NULL',(clean_text(data.get('table_number'),16),)).fetchone():
            bad('Select an active configured table')
        return place_order(c,data,'staff',staff)

def settlement_preview(o,data):
    # Monetary arithmetic in cents; discounts exclude delivery fees and cannot
    # silently change recorded line-item prices.
    def cents(value):
        try:
            d=Decimal(str(value))
            if not d.is_finite(): raise ValueError()
            return int((d*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
        except (InvalidOperation,ValueError,TypeError): bad('Invalid amount')
    kind=str(data.get('discount_kind',''))
    scope=str(data.get('discount_scope',''))
    if kind not in ('','Senior','PWD'): bad('Choose Senior or PWD discount')
    if scope not in ('','bill','item'): bad('Choose bill or item discount')
    try: rate=Decimal(str(data.get('discount_percent',0)))
    except (InvalidOperation,ValueError): bad('Invalid discount percent')
    if not rate.is_finite() or rate<0 or rate>100:bad('Discount must be 0-100 percent')
    if not kind and rate!=0:bad('Choose a discount type')
    if kind and scope not in ('bill','item'):bad('Choose discount scope')
    items=json.loads(o['items']);indices=data.get('discount_item_indices',[])
    if not isinstance(indices,list) or any(type(i) is not int or i<0 or i>=len(items) for i in indices):
        bad('Select valid discount items')
    if len(set(indices))!=len(indices):bad('Duplicate discount item')
    subtotal=cents(o['subtotal']);fee=cents(o['delivery_fee'])
    if kind and scope=='item':
        if not indices:bad('Select at least one discounted line item')
        eligible=sum(cents(items[i]['total']) for i in indices)
    elif kind and scope=='bill':eligible=subtotal
    else:eligible=0;indices=[];rate=Decimal(0);scope=''
    discount=int((Decimal(eligible)*rate/Decimal(100)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    due=subtotal+fee-discount
    return {'subtotal_cents':subtotal,'fee_cents':fee,'discount_cents':discount,
            'total_cents':due,'discount_kind':kind,'discount_scope':scope,
            'discount_percent':float(rate),'discount_item_indices':indices}

@app.post('/api/admin/orders/{oid}/settle')
async def settle_order(oid:str,request:Request):
    data=await request.json()
    with db(write=True) as c:
        staff=require(request,c)
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid.upper(),)).fetchone()
        if not o:bad('Order not found',404)
        if o['payment_status']!='unpaid' or o['status'] in ('cancelled','completed','delivered'):
            bad('Only open, unpaid tickets can be settled',409)
        method=clean_text(data.get('payment_method'),100)
        if not c.execute('SELECT 1 FROM payment_methods WHERE name=? AND enabled=1',(method,)).fetchone():
            bad('Choose an enabled payment type')
        settlement=settlement_preview(o,data)
        due=settlement['total_cents'];received=due;change=0
        if method=='Cash':
            try:
                amount=Decimal(str(data.get('tendered_amount',0)))
                if not amount.is_finite():raise InvalidOperation()
                received=int((amount*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            except (InvalidOperation,ValueError,TypeError):bad('Enter a valid amount received')
            if received<due:bad('Insufficient cash received',409)
            if received>100000000:bad('Cash amount exceeds safe transaction limit')
            change=received-due
            drawer=c.execute("SELECT id,employee_id FROM register_sessions WHERE status='open'").fetchone()
            if drawer:
                c.execute('UPDATE orders SET register_id=?,employee_id=? WHERE id=?',
                          (drawer['id'],drawer['employee_id'],oid.upper()))
            elif c.execute('SELECT 1 FROM employees WHERE active=1 AND pin_hash IS NOT NULL LIMIT 1').fetchone():
                bad('Open Cash Register before collecting cash',409)
        # Optional employee attribution: retain POS choice, or set at settlement.
        served_id=o['served_by_employee_id'];served_name=o['served_by_name']
        if data.get('employee_id') not in (None,''):
            try: eid=int(data['employee_id'])
            except (ValueError,TypeError):bad('Invalid employee')
            employee=c.execute('SELECT id,name FROM employees WHERE id=? AND active=1',(eid,)).fetchone()
            if not employee:bad('Select an active employee',409)
            served_id=employee['id'];served_name=employee['name']
        adjust_order_stock(c,oid.upper(),bool(o['stock_deducted']),True)
        ts=now()
        c.execute('''UPDATE orders SET payment_method=?,payment_status='paid',status='completed',
            discount_kind=?,discount_scope=?,discount_percent=?,discount_amount=?,discount_item_indices=?,
            tendered_amount=?,change_amount=?,paid_at=?,served_by_employee_id=?,served_by_name=?,
            total=?,updated=?,handled_by_staff_id=? WHERE id=?''',
            (method,settlement['discount_kind'],settlement['discount_scope'],settlement['discount_percent'],
             settlement['discount_cents']/100,tojson(settlement['discount_item_indices']),
             received/100,change/100,ts,served_id,served_name,due/100,ts,staff['id'],oid.upper()))
        c.execute("INSERT INTO sales_ledger(order_id,event_type,amount_cents,occurred_at) VALUES(?,'payment',?,?)",
                  (oid.upper(),due,ts))
        reward_if_eligible(c,oid.upper())
        return {'ok':True,'order_id':oid.upper(),'payment_method':method,'total':due/100,
                'discount_amount':settlement['discount_cents']/100,'tendered_amount':received/100,
                'change_amount':change/100,'payment_status':'paid'}

@app.patch('/api/admin/orders/{oid}')
async def update_order(oid:str,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c)
        o=c.execute('SELECT * FROM orders WHERE id=?',(oid.upper(),)).fetchone()
        if not o: bad('Order not found',404)
        status=data.get('status',o['status']);pay=data.get('payment_status',o['payment_status'])
        if status not in ORDER_STATUSES or pay not in PAY_STATUSES: bad('Invalid order status')
        if pay=='paid' and o['payment_status']=='unpaid': bad('Use Orders > Pay / Complete to select a method and confirm tender',409)
        if status in ('completed','delivered') and o['payment_status']=='unpaid': bad('Settle this ticket before marking it complete',409)
        if o['payment_status']=='paid' and pay=='unpaid': bad('Paid orders must be refunded, not marked unpaid')
        if o['payment_status']=='refunded' and pay=='paid': bad('Create a new transaction for refunded orders')
        if o['status']=='cancelled' and status!='cancelled': bad('Cancelled orders cannot be reopened')
        if status=='cancelled' and pay=='paid': bad('Refund payment before cancelling a paid order')
        was_active=bool(o['stock_deducted'])
        next_active=status in ACTIVE_STATUSES
        adjust_order_stock(c,oid.upper(),was_active,next_active)
        fee=o['delivery_fee']
        if 'delivery_fee' in data:
            if o['fulfillment']!='Delivery':bad('Delivery fee is only available for delivery orders')
            if o['payment_status']!='unpaid':bad('Cannot edit delivery fee after payment')
            try:fee=round(float(data['delivery_fee']),2)
            except (TypeError,ValueError):bad('Invalid delivery fee')
            if not 0<=fee<=5000:bad('Delivery fee out of range')
        if o['payment_method']=='Cash' and pay=='paid' and o['payment_status']!='paid':
            drawer=c.execute("SELECT id,employee_id FROM register_sessions WHERE status='open'").fetchone()
            if drawer:
                c.execute('UPDATE orders SET register_id=?,employee_id=? WHERE id=?',(drawer['id'],drawer['employee_id'],oid.upper()))
            elif c.execute('SELECT 1 FROM employees WHERE active=1 AND pin_hash IS NOT NULL LIMIT 1').fetchone():
                bad('Unlock the cash register before collecting cash',409)
        c.execute('UPDATE orders SET status=?,payment_status=?,delivery_fee=?,total=?,updated=? WHERE id=?',(status,pay,fee,round(o['subtotal']+fee,2),now(),oid.upper()))
        if pay!=o['payment_status']:
            if pay=='paid' and o['payment_status']=='unpaid':
                c.execute("INSERT INTO sales_ledger(order_id,event_type,amount_cents,occurred_at) VALUES(?,'payment',?,?)",
                          (oid.upper(),round((o['subtotal']+fee)*100),now()))
            elif pay=='refunded' and o['payment_status']=='paid':
                c.execute("INSERT INTO sales_ledger(order_id,event_type,amount_cents,occurred_at) VALUES(?,'refund',?,?)",
                          (oid.upper(),-round(o['total']*100),now()))

        if status!=o['status'] or pay!=o['payment_status']:
            staff_id=require(request,c)['id']
            c.execute('UPDATE orders SET handled_by_staff_id=? WHERE id=?',(staff_id,oid.upper()))
        reward_if_eligible(c,oid.upper())
        return {'ok':True,'status':status,'payment_status':pay}

# Receipt printing: printer pairing is done by the Windows OS, while the app
# can submit ESC/POS to an installed Windows printer or paired Bluetooth COM port.
def _printer_config(c):
    r=c.execute('SELECT method,target,paper_width,baud_rate,auto_print_pos FROM receipt_printer_settings WHERE id=1').fetchone()
    return {**dict(r),'auto_print_pos':bool(r['auto_print_pos'])} if r else {'method':'browser','target':'','paper_width':58,'baud_rate':9600,'auto_print_pos':False}


def _get_receipt_order(c,oid):
    if not re.fullmatch(r'[A-F0-9]{16}',oid.upper()):bad('Invalid order reference',422)
    r=c.execute('SELECT * FROM orders WHERE id=?',(oid.upper(),)).fetchone()
    if not r:bad('Order not found',404)
    return order_out(r)


@app.get('/api/admin/printers')
def printers(request:Request):
    with db() as c:
        require(request,c)
        conf=_printer_config(c)
    return {'settings':conf,'capabilities':printer_capabilities(),
            'pairing_note':'Tablet printing happens on the tablet: Android Chromium can connect to compatible Bluetooth LE receipt printers; iPad Safari requires AirPrint over Wi-Fi or a manufacturer app. Windows RAW/COM modes work only on a locally running Windows server, not on Render.'}


@app.put('/api/admin/printers')
async def save_printers(request:Request):
    settings=await request.json()
    if not isinstance(settings,dict):bad('Invalid printer settings')
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        try:updated=normalize_settings(settings,printer_capabilities())
        except ValueError as exc:bad(str(exc),422)
        c.execute('UPDATE receipt_printer_settings SET method=?,target=?,paper_width=?,baud_rate=?,auto_print_pos=? WHERE id=1',
                  (updated['method'],updated['target'],updated['paper_width'],updated['baud_rate'],int(updated['auto_print_pos'])))
    return {'settings':updated,'ok':True}



# Authenticated ESC/POS bytes consumed by compatible Android BLE printers.
# The payload is generated on demand from the order stored on the server;
# no raw commands can be uploaded by a customer, and no printer lives on Render.
@app.get('/api/admin/receipt-bytes/test')
def receipt_bytes_test(request: Request, paper_width: int=58):
    if paper_width not in (58,80):bad('Paper width must be 58 or 80 mm',422)
    with db() as c:require(request,c)
    payload=escpos_bytes(paper_width=paper_width,test=True)
    return Response(content=payload,media_type='application/octet-stream',headers={
        'Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff'})

@app.get('/api/admin/receipt-bytes/{oid}')
def receipt_bytes_order(oid: str,request: Request,paper_width: int=58):
    if paper_width not in (58,80):bad('Paper width must be 58 or 80 mm',422)
    with db() as c:
        require(request,c)
        order=_get_receipt_order(c,oid)
    payload=escpos_bytes(order,paper_width=paper_width)
    return Response(content=payload,media_type='application/octet-stream',headers={
        'Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff'})

@app.get('/api/admin/receipts/test',response_class=HTMLResponse)
def test_receipt_preview(request:Request,paper_width:int=0):
    with db() as c:
        require(request,c)
        config=_printer_config(c)
    width=paper_width or config['paper_width']
    if width not in (58,80):bad('Paper width must be 58 or 80 mm',422)
    return HTMLResponse(receipt_html(paper_width=width,test=True),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@app.get('/api/admin/receipts/{oid}',response_class=HTMLResponse)
def order_receipt_preview(oid:str,request:Request,paper_width:int=0):
    if paper_width and paper_width not in (58,80):bad('Paper width must be 58 or 80 mm',422)
    with db() as c:
        require(request,c)
        order=_get_receipt_order(c,oid)
        width=paper_width or _printer_config(c)['paper_width']
    return HTMLResponse(receipt_html(order,paper_width=width),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


def _print_direct(config,order=None,test=False):
    if config['method']=='browser':bad('Browser print mode selected. Open the receipt preview and use Print receipt.',409)
    try:
        # Recheck the configured device against the current Windows device list.
        normalize_settings(config,printer_capabilities())
        payload=escpos_bytes(order,paper_width=config['paper_width'],test=test)
        return send_to_printer(payload,config)
    except ValueError as exc:bad(str(exc),422)
    except (OSError,RuntimeError) as exc:bad('Could not send to printer: '+str(exc)[:250],503)


@app.post('/api/admin/printers/test')
def printer_test(request:Request):
    with db() as c:
        require(request,c)
        config=_printer_config(c)
    return _print_direct(config,test=True)


@app.post('/api/admin/printers/print/{oid}')
def printer_print_order(oid:str,request:Request):
    with db() as c:
        require(request,c)
        order=_get_receipt_order(c,oid)
        config=_printer_config(c)
    return _print_direct(config,order=order)


def valid_category_name(raw):
    name=clean_text(raw,60)
    if not re.fullmatch(r'[A-Za-z0-9 &/+_-]{1,60}',name) or not name.strip():
        bad('Category must be 1-60 characters: letters, numbers, spaces, &, /, +, underscore or dash')
    return name

@app.get('/api/admin/categories')
def categories(request:Request):
    with db() as c:
        require(request,c)
        return {'categories':[dict(row) for row in c.execute('''SELECT cat.id,cat.name,COUNT(p.id) AS product_count
                 FROM product_categories cat LEFT JOIN products p
                   ON p.category=cat.name COLLATE NOCASE
                 GROUP BY cat.id ORDER BY cat.name COLLATE NOCASE''')]}

@app.post('/api/admin/categories',status_code=201)
async def create_category(request:Request):
    data=await request.json()
    if not isinstance(data,dict):bad('Invalid category')
    name=valid_category_name(data.get('name'))
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        if c.execute('SELECT id FROM product_categories WHERE name=? COLLATE NOCASE',(name,)).fetchone():
            bad('Category already exists',409)
        c.execute('INSERT INTO product_categories(name,created) VALUES(?,?)',(name,now()))
        return {'id':c.execute('SELECT last_insert_rowid()').fetchone()[0],'name':name}

@app.patch('/api/admin/categories/{cid}')
async def rename_category(cid:int,request:Request):
    data=await request.json()
    if not isinstance(data,dict):bad('Invalid category')
    name=valid_category_name(data.get('name'))
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        old=c.execute('SELECT name FROM product_categories WHERE id=?',(cid,)).fetchone()
        if not old:bad('Category not found',404)
        collision=c.execute('SELECT id FROM product_categories WHERE name=? COLLATE NOCASE',(name,)).fetchone()
        if collision and collision['id']!=cid:bad('Category already exists',409)
        c.execute('UPDATE product_categories SET name=? WHERE id=?',(name,cid))
        c.execute('UPDATE products SET category=? WHERE category=? COLLATE NOCASE',(name,old['name']))
        return {'ok':True,'name':name}

@app.get('/api/admin/products')
def products(request:Request):
    with db() as c:
        require(request,c)
        return {'products':[product_out(r) for r in c.execute('SELECT * FROM products ORDER BY id')]}
@app.post('/api/admin/products',status_code=201)
async def create_product(request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        p=validated_product(data)
        c.execute('INSERT OR IGNORE INTO product_categories(name,created) VALUES(?,?)',(p['category'],now()))
        p['category']=c.execute('SELECT name FROM product_categories WHERE name=? COLLATE NOCASE',(p['category'],)).fetchone()['name']
        c.execute('INSERT INTO products(category,name,icon,description,price,cost,variants,addons,popular,dual,bundle_qty,stock,active,customer_visible) VALUES(:category,:name,:icon,:description,:price,:cost,:variants,:addons,:popular,:dual,:bundle_qty,:stock,:active,:customer_visible)',p)
        return {'id':c.execute('SELECT last_insert_rowid() id').fetchone()['id']}
def validated_product(d,existing=None):
    if not isinstance(d,dict): bad('Invalid product')
    p={}
    for key in ('name','category','description','icon'):
        p[key]=clean_text(d.get(key,(existing[key] if existing else '')),300 if key=='description' else 120)
    if not p['name'] or not p['category']: bad('Product name and category required')
    p['category']=valid_category_name(p['category'])
    for key in ('price','cost'):
        try:p[key]=round(float(d.get(key,existing[key] if existing else 0)),2)
        except (ValueError,TypeError):bad('Invalid product price')
        if not 0<=p[key]<=999999: bad('Price out of range')
    for key in ('variants','addons'):
        v=d.get(key,json.loads(existing[key]) if existing else [])
        if not isinstance(v,list) or len(v)>30: bad('Invalid '+key)
        for x in v:
            if not isinstance(x,dict):bad('Invalid '+key)
            try:
                if key=='variants':
                    if not isinstance(x.get('l'),str) or not x['l'].strip() or len(x['l'])>80 or ',' in x['l'] or not 0<=float(x.get('p',-1))<=999999:bad('Invalid variant')
                else:
                    if not isinstance(x.get('name'),str) or not x['name'].strip() or len(x['name'])>80 or ',' in x['name'] or not 0<=float(x.get('price',-1))<=999999:bad('Invalid add-on')
            except (TypeError,ValueError):bad('Invalid '+key)
        p[key]=tojson(v)
    for key in ('popular','dual','active','customer_visible'):
        p[key]=int(bool(d.get(key,existing[key] if existing else (key in ('active','customer_visible')))))
    p['bundle_qty']=int(d.get('bundle_qty',existing['bundle_qty'] if existing else 0))
    if p['bundle_qty']<0 or p['bundle_qty']>20: bad('Invalid bundle size')
    stock=d.get('stock',existing['stock'] if existing else None)
    if stock=='' or stock is None: p['stock']=None
    else:
        try:p['stock']=int(stock)
        except (ValueError,TypeError):bad('Invalid stock')
        if p['stock']<0:bad('Stock cannot be negative')
    return p
@app.put('/api/admin/products/{pid}')
async def save_product(pid:int,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        existing=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not existing:bad('Product not found',404)
        p=validated_product(data,existing)
        c.execute('INSERT OR IGNORE INTO product_categories(name,created) VALUES(?,?)',(p['category'],now()))
        p['category']=c.execute('SELECT name FROM product_categories WHERE name=? COLLATE NOCASE',(p['category'],)).fetchone()['name']
        c.execute('UPDATE products SET category=:category,name=:name,icon=:icon,description=:description,price=:price,cost=:cost,variants=:variants,addons=:addons,popular=:popular,dual=:dual,bundle_qty=:bundle_qty,stock=:stock,active=:active,customer_visible=:customer_visible WHERE id=:id',{**p,'id':pid})
        return {'ok':True}
@app.post('/api/admin/inventory/{pid}')
async def stock_adjust(pid:int,request:Request):
    data=await request.json()
    try:delta=int(data.get('change'))
    except (ValueError,TypeError):bad('Invalid quantity')
    if delta==0 or abs(delta)>100000:bad('Quantity out of range')
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        p=c.execute('SELECT stock FROM products WHERE id=?',(pid,)).fetchone()
        if not p:bad('Product not found',404)
        if p['stock'] is None:bad('Set a starting stock quantity first')
        if p['stock']+delta<0:bad('Stock cannot be negative')
        c.execute('UPDATE products SET stock=stock+? WHERE id=?',(delta,pid))
        c.execute('INSERT INTO inventory_movements(product_id,change_qty,note,created) VALUES(?,?,?,?)',(pid,delta,clean_text(data.get('note'),250) or 'Manual adjustment',now()))
        return {'stock':p['stock']+delta}
@app.get('/api/admin/customers')
def list_customers(request:Request):
    with db() as c:
        require(request,c)
        return {'customers':[dict(x) for x in c.execute('SELECT id,name,phone,joined,stamps,visits,lifetime_spend,consent FROM customers ORDER BY joined DESC LIMIT 2000')]}
@app.post('/api/admin/loyalty/redeem/{customer_id}')
async def redeem(customer_id:int,request:Request):
    with db(write=True) as c:
        require(request,c)
        r=c.execute('SELECT stamps FROM customers WHERE id=?',(customer_id,)).fetchone()
        if not r:bad('Customer not found',404)
        if r['stamps']<8:bad('Requires eight stamps to redeem',409)
        c.execute('UPDATE customers SET stamps=stamps-8 WHERE id=?',(customer_id,))
        c.execute('INSERT INTO loyalty_ledger(customer_id,delta,kind,created) VALUES(?,?,?,?)',(customer_id,-8,'free_drink_redeemed',now()))
        return {'stamps':r['stamps']-8,'reward':'Free drink (redemption logged; staff fulfills in-store)'}
@app.get('/api/admin/payments')
def admin_payments(request:Request):
    with db() as c:require(request,c);return {'payments':[dict(x) for x in c.execute('SELECT * FROM payment_methods ORDER BY id')]}
@app.post('/api/admin/payments')
async def add_payment(request:Request):
    data=await request.json();name=clean_text(data.get('name'),100)
    if not name:bad('Payment method name required')
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        try:c.execute('INSERT INTO payment_methods(name,instructions) VALUES(?,?)',(name,clean_text(data.get('instructions'),300)))
        except sqlite3.IntegrityError:bad('Payment method already exists',409)
        return {'ok':True}
@app.put('/api/admin/payments/{pid}')
async def edit_payment(pid:int,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        r=c.execute('SELECT * FROM payment_methods WHERE id=?',(pid,)).fetchone()
        if not r:bad('Payment method not found',404)
        c.execute('UPDATE payment_methods SET enabled=?,instructions=? WHERE id=?',(int(bool(data.get('enabled',r['enabled']))),clean_text(data.get('instructions',r['instructions']),300),pid))
        return {'ok':True}
@app.get('/api/admin/staff')
def staff_list(request:Request):
    with db() as c:
        require(request,c,('owner',))
        return {'staff':[dict(x) for x in c.execute('SELECT id,email,name,role,active FROM staff ORDER BY id')]}
@app.post('/api/admin/staff',status_code=201)
async def add_staff(request:Request):
    data=await request.json()
    name=clean_text(data.get('name'),80);email=clean_text(data.get('email'),200).lower();pwd=str(data.get('password',''));role=data.get('role')
    if '@' not in email or not name or len(pwd)<12 or role not in ('manager','cashier'):bad('Name, valid email, role, and password (12+ chars) required')
    with db(write=True) as c:
        require(request,c,('owner',))
        total=c.execute("SELECT COUNT(*) n FROM staff WHERE role!='owner' AND active=1").fetchone()['n']
        if total>=3:bad('Maximum of three active staff accounts',409)
        try:c.execute('INSERT INTO staff(email,name,role,password_hash) VALUES(?,?,?,?)',(email,name,role,password_hash(pwd)))
        except sqlite3.IntegrityError:bad('Email already in use',409)
        return {'ok':True}
@app.patch('/api/admin/staff/{sid}')
async def disable_staff(sid:int,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner',))
        staff=c.execute('SELECT * FROM staff WHERE id=?',(sid,)).fetchone()
        if not staff or staff['role']=='owner':bad('Cannot change the owner account',403)
        active=int(bool(data.get('active',staff['active'])))
        if active and not staff['active'] and c.execute("SELECT COUNT(*) n FROM staff WHERE role!='owner' AND active=1").fetchone()['n']>=3:bad('Maximum 3 active staff',409)
        name=clean_text(data.get('name',staff['name']),80)
        email=clean_text(data.get('email',staff['email']),200).lower()
        role=data.get('role',staff['role'])
        pwd=data.get('password')
        if len(name)<2 or '@' not in email or role not in ('cashier','manager'):bad('Valid staff name, email and role required')
        if pwd is not None and (not isinstance(pwd,str) or len(pwd)<12):bad('Password must have at least 12 characters')
        hashed=password_hash(pwd) if pwd is not None else staff['password_hash']
        try:c.execute('UPDATE staff SET name=?,email=?,role=?,password_hash=?,active=? WHERE id=?',(name,email,role,hashed,active,sid))
        except sqlite3.IntegrityError:bad('Email already in use',409)
        if not active or pwd is not None:c.execute('DELETE FROM sessions WHERE staff_id=?',(sid,))
        return {'ok':True}


# Table management. Tables can be archived without orphaning historical orders.
TABLE_CODE = re.compile(r'^[A-Za-z0-9_-]{1,12}$')

def validate_table_form(data):
    number=clean_text(data.get('number'),20).upper()
    if not TABLE_CODE.fullmatch(number): bad('Table number must be 1–12 letters, digits, - or _')
    try:seats=int(data.get('seats'))
    except (TypeError,ValueError):bad('Enter a valid table capacity')
    if not 1<=seats<=30:bad('Table capacity must be between 1 and 30')
    location=clean_text(data.get('location'),80)
    if not location:bad('Specify the table location')
    return number,seats,location

@app.get('/api/admin/tables')
def table_list(request:Request):
    with db() as c:
        require(request,c)
        tables=[dict(r) for r in c.execute('SELECT * FROM dining_tables WHERE deleted_at IS NULL ORDER BY number COLLATE NOCASE')]
        for t in tables:
            where='(o.table_id=? OR (o.table_id IS NULL AND o.table_number=? COLLATE NOCASE))'
            args=(t['id'],t['number'])
            summary=c.execute('''SELECT
                 SUM(CASE WHEN o.status IN ('pending','accepted','preparing','ready') THEN 1 ELSE 0 END) AS open_orders,
                 SUM(CASE WHEN o.status IN ('pending','accepted','preparing','ready') THEN o.guests ELSE 0 END) AS diners_on_open_tickets,
                 SUM(CASE WHEN o.payment_status='paid' AND o.status!='cancelled' THEN 1 ELSE 0 END) AS paid_orders,
                 COUNT(*) AS total_orders, MAX(o.created) AS last_order_at
                 FROM orders o WHERE '''+where,args).fetchone()
            # Reuse the immutable accounting ledger for table revenue. This
            # includes refunds and matches the business overview; pending tickets
            # cannot become revenue just because a cashier created an order.
            ledger=c.execute('''SELECT COALESCE(SUM(l.amount_cents),0) AS net_cents
                FROM sales_ledger l JOIN orders o ON o.id=l.order_id WHERE '''+where,args).fetchone()
            t.update({'open_orders':summary['open_orders'] or 0,
                      'diners_on_open_tickets':summary['diners_on_open_tickets'] or 0,
                      'paid_orders':summary['paid_orders'] or 0,
                      'paid_revenue':round((ledger['net_cents'] or 0)/100,2),
                      'total_orders':summary['total_orders'] or 0,
                      'last_order_at':summary['last_order_at']})
        return {'tables':tables}

@app.post('/api/admin/tables',status_code=201)
async def table_create(request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        number,seats,location=validate_table_form(data)
        try:r=c.execute('INSERT INTO dining_tables(number,seats,location,created,qr_key) VALUES(?,?,?,?,?)',(number,seats,location,now(),secrets.token_urlsafe(12)))
        except sqlite3.IntegrityError:bad('Table number already exists',409)
        return {'id':r.lastrowid,'number':number,'seats':seats,'location':location}

@app.patch('/api/admin/tables/{tid}')
async def table_edit(tid:int,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        old=c.execute('SELECT * FROM dining_tables WHERE id=? AND deleted_at IS NULL',(tid,)).fetchone()
        if not old:bad('Table not found',404)
        merged={**dict(old),**data}
        number,seats,location=validate_table_form(merged)
        if not isinstance(merged['active'],(bool,int)) or merged['active'] not in (0,1,False,True):
            bad('Active must be true or false')
        active=int(merged['active'])
        where='(table_id=? OR (table_id IS NULL AND table_number=? COLLATE NOCASE))'
        active_orders=c.execute('''SELECT COALESCE(MAX(guests),0) max_guests,COUNT(*) open_count
            FROM orders WHERE '''+where+" AND status IN ('pending','accepted','preparing','ready')",(tid,old['number'])).fetchone()
        if (not active or number!=old['number']) and active_orders['open_count']:
            bad('Complete or cancel open orders before renaming or disabling this table',409)
        if seats<active_orders['max_guests']:
            bad('Cannot reduce capacity below the diner count on an open table ticket',409)
        try:c.execute('UPDATE dining_tables SET number=?,seats=?,location=?,active=? WHERE id=?',(number,seats,location,active,tid))
        except sqlite3.IntegrityError:bad('Table number already exists',409)
        # Rename does not change historical order references.
        return {'ok':True}


@app.delete('/api/admin/tables/{tid}')
def table_delete(tid:int,request:Request):
    """Remove an inactive table from operations but retain its audit/QR identity.

    Historical orders, sales and refunds are never deleted. An archived QR
    returns Gone rather than silently assigning its code to another table.
    """
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        t=c.execute('SELECT * FROM dining_tables WHERE id=? AND deleted_at IS NULL',(tid,)).fetchone()
        if not t:bad('Table not found',404)
        if t['active']:bad('Mark this table inactive before deleting it',409)
        open_orders=c.execute("""SELECT COUNT(*) n FROM orders
            WHERE (table_id=? OR (table_id IS NULL AND table_number=? COLLATE NOCASE))
            AND payment_status='unpaid' AND status IN ('pending','accepted','preparing','ready')""",
            (tid,t['number'])).fetchone()['n']
        if open_orders:bad('Close or cancel open tickets before removing this table',409)
        c.execute('UPDATE dining_tables SET deleted_at=? WHERE id=?',(now(),tid))
        return {'ok':True,'archived':True,'message':'Table removed from operations; historical records retained'}


# Employee identity for opening/closing cash drawer. This does not replace login.
@app.get('/api/admin/employees')
def employee_list(request:Request):
    with db() as c:
        require(request,c)
        return {'employees':[dict(r) for r in c.execute('SELECT id,name,title,active,pin_hash IS NOT NULL AS pin_set,created FROM employees ORDER BY name COLLATE NOCASE')]}

def validate_employee_form(data):
    name=clean_text(data.get('name'),80)
    title=clean_text(data.get('title','Service Staff'),80)
    if len(name)<2 or not title:bad('Employee name and role/title are required')
    return name,title

@app.post('/api/admin/employees',status_code=201)
async def employee_create(request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        name,title=validate_employee_form(data)
        pin=str(data.get('pin',''))
        if pin and not re.fullmatch(r'\d{6,8}',pin):bad('PIN must contain 6–8 digits')
        r=c.execute('INSERT INTO employees(name,title,pin_hash,created) VALUES(?,?,?,?)',
                    (name,title,password_hash(pin) if pin else None,now()))
        return {'id':r.lastrowid,'name':name}

@app.patch('/api/admin/employees/{eid}')
async def employee_edit(eid:int,request:Request):
    data=await request.json()
    with db(write=True) as c:
        require(request,c,('owner','manager'))
        old=c.execute('SELECT * FROM employees WHERE id=?',(eid,)).fetchone()
        if not old:bad('Employee not found',404)
        name,title=validate_employee_form({**dict(old),**data})
        pin=data.get('pin',None)
        active=int(bool(data.get('active',old['active'])))
        if pin is not None and (not isinstance(pin,str) or not re.fullmatch(r'\d{6,8}',pin)):
            bad('New PIN must contain 6–8 digits')
        if not active and c.execute("SELECT 1 FROM register_sessions WHERE status='open' AND employee_id=?",(eid,)).fetchone():
            bad('Close the register before disabling this employee',409)
        new_pin=password_hash(pin) if pin is not None else old['pin_hash']
        c.execute('UPDATE employees SET name=?,title=?,pin_hash=?,active=? WHERE id=?',(name,title,new_pin,active,eid))
        return {'ok':True}

DENOMINATIONS={
    'coin_001':1,'coin_005':5,'coin_010':10,'coin_025':25,
    'coin_1':100,'coin_5':500,'coin_10':1000,'coin_20':2000,
    'bill_20':2000,'bill_50':5000,'bill_100':10000,
    'bill_200':20000,'bill_500':50000,'bill_1000':100000}

def counted_cash(data):
    counts=data.get('counts')
    if not isinstance(counts,dict) or any(key not in DENOMINATIONS for key in counts):
        bad('Enter valid Philippine coins and bills')
    result={};total=0
    for key,cents in DENOMINATIONS.items():
        v=counts.get(key,0)
        if isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=10000:
            bad('Denomination counts must be whole numbers between 0 and 10,000')
        result[key]=v;total+=cents*v
    return result,total

def verify_employee_pin(c,employee_id,pin,remote):
    try:eid=int(employee_id)
    except (ValueError,TypeError):bad('Select a valid employee')
    e=c.execute('SELECT * FROM employees WHERE id=? AND active=1',(eid,)).fetchone()
    key=f'register:{remote}:{eid}'
    recent=[t for t in login_attempts[key] if time.time()-t<900]
    if len(recent)>=5:bad('Too many incorrect PIN attempts. Try again in 15 minutes',429)
    if not e or not e['pin_hash'] or not re.fullmatch(r'\d{6,8}',str(pin or '')) or not password_ok(str(pin),e['pin_hash']):
        login_attempts[key]=recent+[time.time()]
        bad('Employee PIN is not configured or is incorrect',403)
    login_attempts.pop(key,None)
    return e

def current_register(c):
    r=c.execute("SELECT rs.*,e.name AS employee_name FROM register_sessions rs JOIN employees e ON e.id=rs.employee_id WHERE rs.status='open'").fetchone()
    if not r:return None
    a=dict(r);a['opening_counts']=json.loads(a['opening_counts'])
    sales=c.execute("SELECT COALESCE(SUM(total),0) v,COUNT(*) n FROM orders WHERE register_id=? AND payment_method='Cash' AND payment_status='paid' AND status!='cancelled'",(r['id'],)).fetchone()
    a['cash_sales']=round(sales['v'],2);a['cash_orders']=sales['n']
    a['expected_cash']=round((a['opening_cents']+round(float(sales['v'])*100))/100,2)
    a['opening_total']=a['opening_cents']/100
    a.pop('opening_cents',None)
    return a

@app.get('/api/admin/register')
def register_status(request:Request):
    with db() as c:
        require(request,c)
        latest=[dict(r) for r in c.execute('SELECT rs.id,rs.opened_at,rs.closed_at,rs.status,rs.opening_cents,rs.closing_cents,rs.expected_cents,rs.variance_cents,rs.opening_note,rs.closing_note,e.name employee_name FROM register_sessions rs JOIN employees e ON rs.employee_id=e.id ORDER BY rs.id DESC LIMIT 12')]
        return {'active':current_register(c),'history':latest,'denominations':DENOMINATIONS}

@app.post('/api/admin/register/open',status_code=201)
async def register_open(request:Request):
    data=await request.json()
    with db(write=True) as c:
        staff=require(request,c)
        if c.execute("SELECT 1 FROM register_sessions WHERE status='open'").fetchone():bad('Close the existing cash register before opening a new one',409)
        employee=verify_employee_pin(c,data.get('employee_id'),data.get('pin'),request.client.host if request.client else 'unknown')
        counts,total=counted_cash(data)
        try:
            r=c.execute('INSERT INTO register_sessions(employee_id,opened_by,opened_at,opening_counts,opening_cents,status,opening_note) VALUES(?,?,?,?,?,?,?)',
                    (employee['id'],staff['id'],now(),tojson(counts),total,'open',clean_text(data.get('note'),1000)))
        except sqlite3.IntegrityError:bad('Another employee has already opened the register',409)
        return {'id':r.lastrowid,'opening_total':round(total/100,2),'employee_name':employee['name']}

@app.post('/api/admin/register/close')
async def register_close(request:Request):
    data=await request.json()
    with db(write=True) as c:
        staff=require(request,c)
        r=c.execute("SELECT * FROM register_sessions WHERE status='open'").fetchone()
        if not r:bad('No open cash register',409)
        employee=verify_employee_pin(c,data.get('employee_id'),data.get('pin'),request.client.host if request.client else 'unknown')
        counts,total=counted_cash(data)
        sales=c.execute("SELECT COALESCE(SUM(total),0) v FROM orders WHERE register_id=? AND payment_method='Cash' AND payment_status='paid' AND status!='cancelled'",(r['id'],)).fetchone()['v']
        expected=r['opening_cents']+round(float(sales)*100)
        c.execute("UPDATE register_sessions SET status='closed',closed_by_employee=?,closed_by_staff=?,closed_at=?,closing_counts=?,closing_cents=?,expected_cents=?,variance_cents=?,closing_note=? WHERE id=?",(employee['id'],staff['id'],now(),tojson(counts),total,expected,total-expected,clean_text(data.get('note'),1000),r['id']))
        return {'id':r['id'],'closing_total':round(total/100,2),'expected_total':round(expected/100,2),'variance':round((total-expected)/100,2)}

@app.get('/api/admin/restaurant-dashboard')
def restaurant_dashboard(request:Request):
    with db() as c:
        require(request,c)
        book=sales_accounting(c)
        records=book['orders']
        money=lambda v:round(v/100,2)
        sold=Counter();cats=defaultdict(lambda:{'revenue_cents':0,'units':0});product_revenue=Counter()
        margin=Counter();table_rev=Counter();operator_rev=Counter()
        hourly=Counter();weekday=Counter();dine_revenue=0;guests=0;estimated_cost_lines=0
        staff_names={r['id']:r['name'] for r in c.execute('SELECT id,name FROM staff')}
        employee_names={r['id']:r['name'] for r in c.execute('SELECT id,name FROM employees')}
        product_info={r['id']:dict(r) for r in c.execute('SELECT id,category,cost FROM products')}
        current_table_names={r['id']:r['number'] for r in c.execute('SELECT id,number FROM dining_tables')}
        for ev in book['events']:
            ts=ph_datetime(ev['occurred_at'])
            cents=int(ev['amount_cents'])
            hourly[ts.strftime('%H:00')]+=cents
            weekday[ts.strftime('%A')]+=cents
        for o in records:
            amount=round(o['total']*100)
            if o['fulfillment'] in ('Dine-In','Dine-in'):
                guests+=max(1,o['guests'])
                dine_revenue+=amount
                if o['table_number']: table_rev[current_table_names.get(o['table_id'],o['table_number'])]+=amount
            responsible=o['served_by_name'] or employee_names.get(o['employee_id']) or staff_names.get(o['handled_by_staff_id'] or o['staff_id']) or 'Online (unassigned)'
            operator_rev[responsible]+=amount
            for i in json.loads(o['items']):
                name=i['name'];qty=int(i['qty']);line_cents=round(float(i['total'])*100);pid=i['product_id']
                sold[name]+=qty;product_revenue[name]+=line_cents
                product=product_info.get(pid,{})
                if 'category' not in i or 'unit_cost' not in i:estimated_cost_lines+=1
                category=i.get('category',product.get('category','Unclassified'))
                cats[category]['revenue_cents']+=line_cents
                cats[category]['units']+=qty
                margin[name]+=line_cents-round(float(i.get('unit_cost',product.get('cost',0)))*100)*qty
        pos_orders=[o for o in records if o['channel']=='pos']
        pos_cents=sum(round(o['total']*100) for o in pos_orders)
        # Date POS orders by their payment ledger event (not their order creation time).
        pos_ids={o['id'] for o in pos_orders}
        by_month=Counter(ph_datetime(ev['occurred_at']).strftime('%Y-%m') for ev in book['events']
                         if ev['event_type']=='payment' and ev['order_id'] in pos_ids)
        sessions=[]
        for rs in c.execute('SELECT id,opened_at,status FROM register_sessions ORDER BY id DESC LIMIT 12'):
            stats=c.execute("SELECT COUNT(*) n,COALESCE(SUM(total),0) v FROM orders WHERE register_id=? AND payment_status='paid' AND status!='cancelled'",(rs['id'],)).fetchone()
            sessions.append({'session':rs['id'],'opened':rs['opened_at'],'status':rs['status'],'orders':stats['n'],'revenue':round(stats['v'],2)})
        return {
            'revenue':money(book['net_cents']),'orders':len(records),
            'average_order':round(sum(o['total'] for o in records)/len(records),2) if records else 0,
            'pos_orders':len(pos_orders),'pos_revenue':money(pos_cents),
            'pos_monthly':[{'month':m,'orders':by_month[m]} for m in sorted(by_month)],
            'register_sessions':sessions,'guests':guests,
            'revenue_per_guest':money(round(dine_revenue/guests)) if guests else 0,
            'discount_rate':None,
            'hourly':[{'hour':f'{h:02d}:00','revenue':money(hourly[f'{h:02d}:00'])} for h in range(24)],
            'weekly':[{'day':d,'revenue':money(weekday[d])} for d in ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')],
            'products':[{'name':k,'units':n,'revenue':money(product_revenue[k]),'estimated_margin':money(margin[k])} for k,n in sold.most_common(10)],
            'categories':[{'name':k,'units':v['units'],'revenue':money(v['revenue_cents'])} for k,v in sorted(cats.items(),key=lambda a:-a[1]['revenue_cents'])],
            'tables':[{'table':k,'revenue':money(v)} for k,v in table_rev.most_common(10)],
            'responsibles':[{'name':k,'revenue':money(v)} for k,v in operator_rev.most_common(10)],
            'legacy_cost_estimate_lines':estimated_cost_lines,
            'open_register':current_register(c)}

def qr_base_url(value: str):
    # Only encode a customer-reachable address, never 127.0.0.1.
    value=(value or '').strip().rstrip('/')
    if len(value)>300 or not value:bad('Enter your customer-accessible website URL',422)
    if not value.isascii():bad('QR website URL must use ASCII/punycode domain characters',422)
    try:
        url=urlsplit(value)
        hostname=(url.hostname or '').lower().rstrip('.')
        port=url.port
    except ValueError:
        bad('Invalid QR website URL or port number',422)
    if (url.scheme not in ('http','https') or not hostname or url.username or url.password
            or url.path or url.query or url.fragment or any(ch.isspace() for ch in value)
            or chr(92) in value):
        bad('Enter only a root URL, e.g. http://192.168.1.25:8000 or https://your-domain.com',422)
    if hostname in ('localhost','0.0.0.0'):
        bad('localhost and 0.0.0.0 cannot be used by customer phones.',422)
    try:
        ip=ipaddress.ip_address(hostname)
    except ValueError:
        ip=None
    if ip and (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
        bad('127.0.0.1 and other loopback/link-local addresses cannot be used in QR codes.',422)
    if public_mode():
        # When hosted, all table QRs must go to the SAME public backend as the
        # back office. Prevent accidental LAN/localhost or unrelated domain QRs.
        expected=public_base_url()
        if url.scheme!='https' or value.lower()!=expected.lower():
            bad('Public mode is active. Use this La Luna HTTPS site: '+expected,422)
    if url.scheme=='http':
        if not ip or not ip.is_private:
            bad('Public sites require HTTPS. Local QR codes must use a private Wi-Fi/LAN IP.',422)
        if os.environ.get('LALUNA_ALLOW_WIFI')!='1':
            bad('Wi-Fi mode is OFF. Restart the all-in-one BAT with option 2.',422)
        if port != 8000:
            bad('Local QR ordering uses port 8000. Enter http://YOUR-WIFI-IP:8000.',422)
        if hostname not in lan_ips_detected():
            bad('This IP is not assigned to the La Luna computer. Choose its actual Wi-Fi IPv4 address (run ipconfig), not the router/VPN/another computer.',422)
        result=self_health(hostname,8000,timeout=2)
        if not result:
            bad('La Luna cannot load its own API at '+value+'. Restart launcher in option 2, verify port 8000 and Windows Firewall, then retry.',422)
        if not result.get('lan_access') or result.get('build')!=BUILD_ID:
            bad('An older or local-only server is running at this address. Stop ALL old La Luna servers and restart option 2.',409)
    return value


def lan_ips():
    return lan_ips_detected()


@app.get('/qr-test',response_class=HTMLResponse)
def qr_connection_test():
    # Deliberately public: staff scan this page on a phone to confirm reachability.
    # It exposes no admin records, credentials, or private order information.
    return HTMLResponse("""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>La Luna QR Connection Test</title><style>body{font:16px system-ui;background:#161411;color:#fff3db;max-width:480px;margin:10vh auto;padding:24px;text-align:center}h1{color:#e9a342}a{display:inline-block;background:#16834a;color:white;padding:15px 22px;border-radius:12px;text-decoration:none;font-weight:700}#result{padding:18px;line-height:1.6}</style></head><body><h1>La Luna Grill + Cafe</h1><h2>QR Phone Connection Test</h2><p id=\"result\">Checking live ordering connection...</p><a href=\"/\">Open Customer Ordering</a><script>fetch('/api/health',{cache:'no-store'}).then(r=>r.json()).then(h=>{document.getElementById('result').textContent=h.ok&&h.public_access&&location.protocol==='https:'?'CONNECTED! The public HTTPS La Luna ordering server works on this network. Test a table QR and check Back Office > Orders.':h.ok&&h.lan_access?'CONNECTED! This phone can reach La Luna on the same private Wi-Fi. For mobile data or other Wi-Fi use a public HTTPS deployment.':h.ok?'Server reachable but this is local-only mode. For any network, deploy La Luna online with HTTPS.':'The ordering server did not respond correctly.'}).catch(()=>document.getElementById('result').textContent='Connection failed. Ask staff to verify the Wi-Fi connection.')</script></body></html>""",headers={'Cache-Control':'no-store'})


@app.get('/t/{qr_key}', include_in_schema=False)
def open_permanent_table_qr(qr_key: str):
    """Public repeat-use link printed on the physical table.

    Uses a database-stored opaque key rather than a changing table number.
    Redirects on every scan so renamed tables use their CURRENT table number.
    Never embeds localhost or the current LAN IP in the printed code.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,32}',qr_key):
        bad('This table QR is invalid',404)
    with db() as c:
        t=c.execute('SELECT number,active,deleted_at FROM dining_tables WHERE qr_key=?',(qr_key,)).fetchone()
    if not t:
        return HTMLResponse('<h1>Table QR not found</h1><p>Ask La Luna staff for assistance.</p><a href="/">Open the ordering menu</a>',status_code=404,headers={'Cache-Control':'no-store'})
    if t['deleted_at']:
        return HTMLResponse('<h1>This table has been removed</h1><p>Please ask staff for a new table QR.</p>',status_code=410,headers={'Cache-Control':'no-store'})
    if not t['active']:
        return HTMLResponse('<h1>Table temporarily unavailable</h1><p>Dine-In at this table is currently unavailable. Please ask the staff for help.</p><a href="/">Open the menu for Take-Out</a>',status_code=200,headers={'Cache-Control':'no-store'})
    return RedirectResponse('/?table='+quote(t['number']),status_code=302,headers={'Cache-Control':'no-store'})

@app.get('/api/admin/qr-network')
def qr_network(request:Request):
    with db() as c:require(request,c)
    adapters=[] if public_mode() else lan_addresses()
    return {'wifi_mode':os.environ.get('LALUNA_ALLOW_WIFI')=='1' and not public_mode(),
            'public_mode':public_mode(),
            'wifi_urls':['http://'+entry['ip']+':8000' for entry in adapters],
            'wifi_addresses':[{'url':'http://'+entry['ip']+':8000','adapter':entry['adapter'],
                               'gateway':entry['gateway']} for entry in adapters],
            'public_url':public_base_url(),
            'remote_backoffice_url':public_base_url()+'/backoffice' if public_base_url() else '',
            'permanent_table_qr_ready':public_mode() and public_base_url().startswith('https://'),
            'permanent_table_qr_format':'/t/{permanent-table-key}'}


def qr_image(target,filename):
    image=qrcode.make(target)
    buf=io.BytesIO();image.save(buf,format='PNG');buf.seek(0)
    return StreamingResponse(buf,media_type='image/png',
                             headers={'Content-Disposition':'inline; filename="'+filename+'"',
                                      'X-QR-Target':target})


@app.get('/api/admin/qr-test')
def qr_phone_test(request:Request,base_url:str=''):
    with db() as c:require(request,c)
    public=qr_base_url(base_url or public_base_url() or str(request.base_url))
    return qr_image(public+'/qr-test', 'laluna-phone-connection-test.png')


@app.get('/api/admin/qr-menu')
def qr_general(request:Request,base_url:str=''):
    with db() as c:require(request,c)
    public=qr_base_url(base_url or public_base_url() or str(request.base_url))
    return qr_image(public+'/', 'laluna-general-ordering-qr.png')


@app.get('/api/admin/qr-static/{table_number}')
def qr_static_table(table_number:str,request:Request):
    # Only a live public HTTPS host can serve ANY phone on ANY network.
    # No guest Wi-Fi, localhost or self-selected external site is encoded.
    with db() as c:
        require(request,c)
        if not public_mode() or not public_base_url().startswith('https://'):
            bad('Permanent table QRs require the public HTTPS La Luna deployment. Generate codes from your LIVE Render back office, not localhost or GitHub Pages.',409)
        if not TABLE_CODE.fullmatch(table_number):bad('Invalid table number',422)
        t=c.execute('SELECT qr_key FROM dining_tables WHERE number=? COLLATE NOCASE AND active=1',(table_number,)).fetchone()
        if not t:bad('Configure and activate this table in Back Office > Tables before generating its permanent QR.',409)
        if not t['qr_key']:bad('Table QR key is missing; restart the server to complete the database migration.',409)
    base=qr_base_url(public_base_url())
    target=base+'/t/'+quote(t['qr_key'])
    response=qr_image(target,'laluna-permanent-table-'+table_number+'.png')
    response.headers['X-QR-Permanent']='true'
    return response

@app.get('/api/admin/qr/{table_number}')
def qr_table(table_number:str,request:Request,base_url:str=''):
    with db() as c:
        require(request,c)
        table=c.execute('SELECT qr_key FROM dining_tables WHERE number=? COLLATE NOCASE AND active=1',(table_number,)).fetchone()
        if c.execute('SELECT 1 FROM dining_tables LIMIT 1').fetchone() and not table:
            bad('This table is not configured or active; add it under Tables first',409)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,12}',table_number):bad('Invalid table number')
    public=qr_base_url(base_url or public_base_url() or str(request.base_url))
    # Keep older API callers compatible while making hosted, configured QRs
    # repeat-use and insensitive to later table renaming.
    if table and public_mode():
        return qr_image(public+'/t/'+quote(table['qr_key']), 'laluna-permanent-table-qr.png')
    return qr_image(public+'/?table='+quote(table_number), 'laluna-legacy-table-qr.png')

@app.get('/api/upsells')
def upsells(ids:str=''):
    """Data-driven co-purchase rankings + category fallback; not a third-party LLM."""
    try:cart_ids=set(int(x) for x in ids.split(',') if x) if ids else set()
    except ValueError:bad('Invalid product IDs')
    with db() as c:
        products={x['id']:dict(x) for x in c.execute('SELECT * FROM products WHERE active=1 AND customer_visible=1 AND (stock IS NULL OR stock>0)')}
        pair=Counter()
        for o in c.execute("SELECT items FROM orders WHERE payment_status='paid' AND status!='cancelled' ORDER BY created DESC LIMIT 1000"):
            present={x['product_id'] for x in json.loads(o['items'])}
            for p in present & cart_ids:
                for other in present-cart_ids:pair[other]+=1
        candidates=[p for p in products.values() if p['id'] not in cart_ids]
        drinks={'Coffee','Milktea','Fruit Shakes & Desserts','Frappe','Ube Series'}
        has_drink=any(products.get(i,{}).get('category') in drinks for i in cart_ids)
        ranked=sorted(candidates,key=lambda x:(pair[x['id']]*100+(12 if (not has_drink and x['category'] in drinks) else 0)+int(x['popular'])*3,-x['price']),reverse=True)[:3]
        return {'recommendations':[{'id':p['id'],'name':p['name'],'icon':p['icon'],'price':p['price'],'reason':'Often ordered together' if pair[p['id']]>0 else 'Pairs well with your order'} for p in ranked], 'method':'Purchase-pattern ranking with menu-category fallback'}

app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
