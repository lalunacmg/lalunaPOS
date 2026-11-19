"""La Luna safe startup/API self-check and SQLite online backup.

Run via START_LA_LUNA_ALL_IN_ONE.bat menu options 5 and 6, or:
  python laluna_diagnostics.py
  python laluna_diagnostics.py --backup

The self-check creates a disposable database in a temporary directory. No
existing order, admin account or local database is read or written in tests.
It validates application endpoints, not physical Bluetooth printing or LAN.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parent


def safe_backup() -> int:
    source = Path(os.environ.get('LALUNA_DB_PATH') or ROOT / 'laluna.db').expanduser()
    if not source.is_file():
        print('ERROR: Existing La Luna database not found:', source)
        print('A backup is available after your first successful server startup.')
        return 1
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    destination = source.with_name(source.name + '.backup-' + stamp + '-' + secrets.token_hex(3) + '.sqlite3')
    try:
        with sqlite3.connect(str(source), timeout=20) as live:
            # Back up a consistent SQLite snapshot, including WAL data.
            with sqlite3.connect(str(destination), timeout=20) as backup:
                live.backup(backup)
            with sqlite3.connect(str(destination)) as verify:
                check = verify.execute('PRAGMA integrity_check').fetchone()[0]
                if check != 'ok':
                    raise RuntimeError('SQLite integrity_check failed: ' + check)
        print('SUCCESS: Database backup verified:', destination)
        # Product photos are stored outside SQLite but MUST be backed up with it.
        media=Path(os.environ.get('LALUNA_MEDIA_DIR') or source.parent/'laluna-media')/'products'
        photos=sorted(x for x in media.glob('*.webp') if x.is_file()) if media.is_dir() else []
        if photos:
            archive=source.with_name('laluna-product-photos-'+stamp+'.zip')
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
                for photo in photos:z.write(photo,arcname='products/'+photo.name)
            with zipfile.ZipFile(archive) as z:
                if z.testzip():raise RuntimeError('Product photo archive verification failed')
            print(f'SUCCESS: {len(photos)} menu photo(s) backed up:',archive)
        else:
            print('No uploaded menu photos to back up.')
        print('Keep these backups private; download both the SQLite backup and photo ZIP off Render.')
        return 0
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print('ERROR: Database backup failed:', error)
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        return 1


def request_json(opener, base, method, route, data=None, csrf=''):
    raw = None if data is None else json.dumps(data).encode('utf-8')
    headers = {'Accept': 'application/json'}
    if raw is not None:
        headers['Content-Type'] = 'application/json'
    if csrf:
        headers['X-CSRF-Token'] = csrf
    req = Request(base + route, data=raw, headers=headers, method=method)
    try:
        with opener.open(req, timeout=6) as res:
            return res.status, res.headers, res.read()
    except HTTPError as err:
        return err.code, err.headers, err.read()


def run_smoke() -> int:
    essential = [ROOT / 'server.py', ROOT / 'receipt_printer.py', ROOT / 'menu_seed.json',
                 ROOT / 'static' / 'index.html', ROOT / 'static' / 'backoffice.html',
                 ROOT / 'static' / 'logo-backoffice-transparent.png']
    for f in essential:
        if not f.is_file():
            print('FAIL: Project asset missing:', f)
            return 1

    print('LA LUNA - SAFE SYSTEM SELF-CHECK')
    print('Uses a temporary database. Does NOT touch your existing laluna.db.')
    count = 0

    def check(condition, description):
        nonlocal count
        if not condition:
            raise AssertionError(description)
        count += 1
        print('PASS:', description)

    with tempfile.TemporaryDirectory(prefix='laluna-safe-check-') as tmp:
        env = os.environ.copy()
        env.update({
            'LALUNA_DB_PATH': str(Path(tmp) / 'isolated-test.db'),
            'LALUNA_MEDIA_DIR': str(Path(tmp) / 'media'),
            'LALUNA_ADMIN_EMAIL': 'diagnostic-owner@example.test',
            'LALUNA_ADMIN_PASSWORD': 'temporary-test-secret-123',
            'LALUNA_ADMIN_TAB_PIN':'nonproduction-smoke-pin',
            'LALUNA_ALLOW_WIFI': '0',
        })
        env.pop('LALUNA_FORCE_SECURE_COOKIE', None)
        env.pop('LALUNA_PUBLIC_URL', None)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        base = 'http://127.0.0.1:' + str(port)
        logfile = Path(tmp) / 'server-test-output.log'
        with logfile.open('wb') as logs:
            proc = subprocess.Popen(
                [sys.executable, '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', str(port)],
                cwd=str(ROOT), env=env, stdout=logs, stderr=subprocess.STDOUT,
            )
            try:
                opener = build_opener(HTTPCookieProcessor(CookieJar()))
                ready = False
                end = time.monotonic() + 25
                while time.monotonic() < end:
                    if proc.poll() is not None:
                        break
                    try:
                        status, _, body = request_json(opener, base, 'GET', '/api/health')
                        if status == 200 and json.loads(body).get('ok'):
                            ready = True
                            break
                    except (OSError, URLError, ValueError):
                        pass
                    time.sleep(0.2)
                check(ready, 'Test server starts and /api/health responds')
                health = json.loads(body)
                check(health['configured'] and health['provider'] == 'Local delivery', 'Admin bootstrap and local delivery configuration')
                check(health['lan_access'] is False, 'Local-only network mode stays private')
                for path, label in [('/', 'Customer ordering page'), ('/backoffice', 'Back-office page'),
                                    ('/static/backoffice.js', 'Back-office JavaScript'),
                                    ('/static/backoffice.css', 'Back-office styles'),
                                    ('/static/logo-backoffice-transparent.png', 'Original transparent La Luna logo'),
                                    ('/sw.js', 'Service worker')]:
                    status, _, content = request_json(opener, base, 'GET', path)
                    check(status == 200 and len(content) > 100, label)
                status, _, content = request_json(opener, base, 'GET', '/api/menu')
                catalog = json.loads(content)
                check(status == 200 and len(catalog['products']) == 38, '38 active menu products and enabled payments')
                check(status == 200 and len(catalog['payments']) >= 1, 'Payment methods available')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/printers')
                check(status == 401, 'Printer settings protected from anonymous access')
                status, _, body = request_json(opener, base, 'POST', '/api/auth/login',
                    {'email': 'diagnostic-owner@example.test', 'password': 'temporary-test-secret-123'})
                auth = json.loads(body)
                check(status == 200 and auth['role'] == 'owner' and auth['csrf'], 'Owner login and CSRF token')
                csrf = auth['csrf']
                status, _, _ = request_json(opener, base, 'GET', '/api/admin/overview')
                check(status == 423, 'Admin sections locked by default')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/access/unlock',
                    {'pin': 'nonproduction-smoke-pin'}, csrf)
                check(status == 200 and json.loads(body)['unlocked'], 'Admin PIN unlock verifies staff session')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/overview')
                check(status == 200, 'Analytics dashboard')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/staff')
                check(status == 200, 'Staff access and management')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/products')
                check(status == 200, 'Back-office product and inventory management')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/printers')
                check(status == 200 and 'capabilities' in json.loads(body), 'Receipt-printer setup and discovery API')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/receipts/test')
                check(status == 200 and b'TEST ONLY' in body, '58mm/80mm browser-print receipt template')
                status, _, payload = request_json(opener, base, 'GET', '/api/admin/receipt-bytes/test?paper_width=80')
                check(status == 200 and payload.startswith(b'\x1b@') and b'TEST ONLY' in payload,
                      'Authenticated BLE ESC/POS sample receipt bytes')
                status, headers, body = request_json(opener, base, 'GET',
                    '/api/admin/qr/T1?base_url=' + quote('https://order.example.com'))
                check(status == 200 and headers.get('X-QR-Target') == 'https://order.example.com/?table=T1'
                      and body.startswith(b'\x89PNG'), 'Table QR PNG points to usable HTTPS URL')
                status, _, body = request_json(opener, base, 'GET',
                    '/api/admin/qr-menu?base_url=' + quote('http://127.0.0.1:8000'))
                check(status == 422, 'Unreachable localhost QR is rejected')
                status, _, body = request_json(opener, base, 'POST', '/api/loyalty/join',
                    {'name': 'Test Customer', 'phone': '09123456789', 'consent': True})
                check(status == 201 and 'card_token' in json.loads(body), 'Customer loyalty enrollment')
                status, _, body = request_json(opener, base, 'POST', '/api/orders',
                    {'name': 'Test Customer', 'phone': '09123456789', 'payment_method': 'Cash',
                     'fulfillment': 'Take-Out', 'items': [{'product_id': 1, 'qty': 1, 'mods': ''}]})
                order = json.loads(body)
                check(status == 201 and order['order_id'], 'Customer ordering and server price validation')
                oid = order['order_id']
                status, _, body = request_json(opener, base, 'GET', '/api/orders/' + oid + '/status')
                check(status == 200 and json.loads(body)['status'] == 'pending', 'Order tracking')
                status, _, body = request_json(opener, base, 'PATCH', '/api/admin/orders/' + oid,
                    {'status': 'completed', 'payment_status': 'paid'}, csrf)
                check(status == 409, 'Direct paid-status bypass is rejected')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/orders/' + oid + '/settle',
                    {'payment_method':'Cash', 'tendered_amount':200}, csrf)
                check(status == 200 and json.loads(body)['payment_status']=='paid', 'Orders-only payment and cash-change confirmation')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/receipts/' + oid)
                check(status == 200 and b'PAID' in body, 'Paid order receipt preview')
                status, _, payload = request_json(opener, base, 'GET', '/api/admin/receipt-bytes/' + oid)
                check(status == 200 and b'PHP' in payload and payload.endswith(b'\x1dV\x00'),
                      'Actual stored-order receipt printable over Android BLE')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/pos-tickets',
                    {'name':'Counter Walk-in', 'fulfillment':'Take-Out',
                     'items':[{'product_id':1,'qty':1,'mods':''}]}, csrf)
                staged=json.loads(body) if status==201 else {}
                check(status == 201 and staged.get('payment_status')=='unpaid', 'POS sends a new ticket to Orders unpaid')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/orders/'+staged['order_id']+'/settle',
                    {'payment_method':'GCash/QRPh','discount_kind':'PWD',
                     'discount_scope':'bill','discount_percent':10},csrf)
                check(status == 200 and json.loads(body)['discount_amount']>0, 'Bill discount uses actual recorded payment')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/pos',
                    {'name': 'Test Customer', 'phone': '09123456789', 'payment_method': 'Cash',
                     'fulfillment': 'Dine-in', 'items': [{'product_id': 1, 'qty': 1, 'mods': ''}]}, csrf)
                check(status == 201 and 'order_id' in json.loads(body), 'POS sale creation')
                status, _, body = request_json(opener, base, 'POST', '/api/admin/pos',
                    {'name':'Delivery Example','phone':'09171234567','payment_method':'Cash',
                     'fulfillment':'Delivery','address':'Barangay Poblacion, Catarman, Camiguin',
                     'delivery_fee':35.50,'items':[{'product_id':1,'qty':1,'mods':''}]},csrf)
                delivery = json.loads(body) if status==201 else {}
                check(status == 201 and delivery.get('delivery_fee')==35.5,
                      'Staff POS Delivery includes confirmed delivery fee')
                if delivery.get('order_id'):
                    status, _, body = request_json(opener, base, 'GET',
                        '/api/admin/receipts/'+delivery['order_id'])
                    check(status == 200 and b'PHP 35.50' in body,
                          'POS Delivery printed receipt includes fee and address')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/customers')
                check(status == 200, 'Customer list and growth tracking')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/payments')
                check(status == 200, 'Payment method administration')
                status, _, body = request_json(opener, base, 'GET', '/api/admin/qr-network')
                check(status == 200 and not json.loads(body)['wifi_mode'], 'QR network diagnostics')
                status, _, body = request_json(opener, base, 'GET', '/qr-test')
                check(status == 200 and b'QR Phone Connection Test' in body, 'Public phone QR connection-test page')
                status, headers, body = request_json(opener, base, 'GET', '/api/admin/qr-test?base_url=https%3A%2F%2Forder.example.com')
                check(status == 200 and headers.get('X-QR-Target')=='https://order.example.com/qr-test' and body.startswith(b'\x89PNG'), 'Staff QR phone-test image and encoded destination')
                for removed in ['/api/admin/lalamove/status', '/api/admin/lalamove/quote/TEST']:
                    status, _, _ = request_json(opener, base, 'GET', removed)
                    check(status == 404, 'Removed legacy courier integration stays disabled')
            except Exception as exc:
                print('FAIL:', type(exc).__name__ + ':', exc)
                print('\nTest server log (last 25 lines):')
                try:
                    for line in logfile.read_text(encoding='utf-8', errors='replace').splitlines()[-25:]:
                        print(' ', line)
                except OSError:
                    pass
                return 1
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=6)
        print('\nSUCCESS: ' + str(count) + ' isolated checks passed. Your live database was untouched.')
        print('Physical mobile printing, device pairing, Wi-Fi and public internet access still need on-device testing.')
        return 0


def main():
    parser = argparse.ArgumentParser(description='Safe La Luna system check or SQLite backup.')
    parser.add_argument('--backup', action='store_true', help='Back up existing SQLite database without changing it')
    args = parser.parse_args()
    return safe_backup() if args.backup else run_smoke()


if __name__ == '__main__':
    raise SystemExit(main())
