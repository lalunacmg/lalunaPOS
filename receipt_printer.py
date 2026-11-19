"""La Luna receipt rendering and optional Windows ESC/POS transport.

Pairing Bluetooth devices happens in Windows, not through the web server.
RAW Windows queues and Bluetooth serial COM ports must support ESC/POS.
The browser's print dialog is the driver-based compatibility fallback.
"""
from datetime import datetime, timedelta, timezone
from html import escape
import importlib.util
import os
import re
import textwrap
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Windows Python installations may not include the IANA timezone database.
# Prefer the real Asia/Manila rules when available, but do not crash the
# entire ordering server when optional OS/PyPI timezone data is missing.
try:
    MANILA = ZoneInfo('Asia/Manila')
except ZoneInfoNotFoundError:
    # The Philippines currently uses UTC+08:00 year-round (no DST).
    # This fallback is accurate for current cafe receipts without tzdata.
    MANILA = timezone(timedelta(hours=8), name='Asia/Manila')
MODES = ('browser', 'windows_raw', 'bluetooth_serial')
PAPER_WIDTHS = (58, 80)
BAUD_RATES = (9600, 19200, 38400, 57600, 115200)


def printer_capabilities():
    windows = os.name == 'nt'
    queues = []
    ports = []
    queue_error = ''
    serial_error = ''
    have_win32print = bool(importlib.util.find_spec('win32print')) if windows else False
    have_pyserial = bool(importlib.util.find_spec('serial'))
    if windows and have_win32print:
        try:
            import win32print
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            queues = sorted({p[2] for p in win32print.EnumPrinters(flags) if p[2]})
        except Exception as error:
            queue_error = str(error)[:180]
    if windows and have_pyserial:
        try:
            from serial.tools import list_ports
            ports = sorted([{'device': p.device, 'description': p.description or p.device}
                            for p in list_ports.comports()], key=lambda p:p['device'])
        except Exception as error:
            serial_error = str(error)[:180]
    return {'windows':windows, 'win32print_installed':have_win32print,
            'pyserial_installed':have_pyserial, 'queues':queues, 'serial_ports':ports,
            'queue_error':queue_error, 'serial_error':serial_error}


def normalize_settings(config, capabilities):
    method = str(config.get('method','browser'))
    target = str(config.get('target','')).strip()
    try:
        paper_width = int(config.get('paper_width',58))
        baud_rate = int(config.get('baud_rate',9600))
    except (TypeError, ValueError):
        raise ValueError('Invalid paper width or serial speed')
    if method not in MODES or paper_width not in PAPER_WIDTHS or baud_rate not in BAUD_RATES:
        raise ValueError('Select a valid print method, paper width and serial speed')
    auto_pos = bool(config.get('auto_print_pos', False))
    if method == 'browser':
        target = ''
        auto_pos = False
    elif not capabilities['windows']:
        raise ValueError('Direct printing is available only on a Windows server; use browser printing on this device')
    elif method == 'windows_raw':
        if not capabilities['win32print_installed']:
            raise ValueError('Install pywin32 from requirements-printer-windows.txt first')
        if target not in capabilities['queues']:
            raise ValueError('Select an installed Windows printer queue from the refreshed list')
    elif method == 'bluetooth_serial':
        if not capabilities['pyserial_installed']:
            raise ValueError('Install pyserial from requirements-printer-windows.txt first')
        if not re.fullmatch(r'COM\d{1,3}', target, flags=re.I):
            raise ValueError('Choose a paired Bluetooth COM port')
        if target.casefold() not in {p['device'].casefold() for p in capabilities['serial_ports']}:
            raise ValueError('Choose a currently detected serial COM port from the refreshed list')
    return dict(method=method, target=target, paper_width=paper_width,
                baud_rate=baud_rate, auto_print_pos=auto_pos)


def _ph_datetime(value):
    try:
        d = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if d.tzinfo is None:
            return str(value)[:25]
        return d.astimezone(MANILA).strftime('%d %b %Y, %I:%M %p')
    except (ValueError, TypeError):
        return str(value or '')[:30]


def _peso(value):
    try:
        return 'PHP {:,.2f}'.format(float(value))
    except (TypeError, ValueError):
        return 'PHP 0.00'


def _esc(s):
    return escape(str(s or ''), quote=True)


def receipt_html(order=None, paper_width=58, test=False):
    """Print-friendly document with no externally loaded content or unescaped order fields."""
    if paper_width not in PAPER_WIDTHS:
        raise ValueError('Unsupported receipt width')
    if test:
        order = {'id':'TEST-RECEIPT','created':datetime.now(MANILA).isoformat(),
                 'status':'test','payment_status':'test','payment_method':'Cash',
                 'fulfillment':'Dine-in','customer_name':'Sample Customer',
                 'table_number':'T1','notes':'Sample print only — no sale recorded',
                 'subtotal':145.0,'delivery_fee':0.0,'total':145.0,
                 'items':[{'name':'Sample Coffee','mods':'','qty':1,'total':95.0},
                          {'name':'Sample Pastry','mods':'','qty':1,'total':50.0}]}
    if not order:
        raise ValueError('Missing receipt order')
    line_items = []
    for item in order.get('items',[]):
        description = _esc(item.get('name','Item'))
        mods = _esc(item.get('mods',''))
        qty = _esc(item.get('qty',1))
        amount = _esc(_peso(item.get('total',0)))
        line_items.append('<div class="item"><div><strong>'+qty+' × '+description+'</strong>'+
                          ('<small>'+mods+'</small>' if mods else '')+
                          '</div><strong>'+amount+'</strong></div>')
    fulfillment = _esc(order.get('fulfillment',''))
    location = _esc(order.get('table_number') or order.get('address') or '')
    notes = _esc(order.get('notes',''))
    fee = float(order.get('delivery_fee',0) or 0)
    status = _esc(str(order.get('payment_status','unpaid')).upper())
    if test: status = 'TEST ONLY — NOT A SALE'
    rows = [
        ('Order',_esc(order.get('id',''))),
        ('Date',_esc(_ph_datetime(order.get('created','')))),
        ('Type',fulfillment),
        ('Customer',_esc(order.get('customer_name') or 'Walk-in')),
        *([('Served by',_esc(order.get('served_by_name')))] if order.get('served_by_name') else []),
        ('Payment method',_esc(order.get('payment_method',''))),
    ]
    if location: rows.insert(3,('Table / address', location))
    fields = ''.join('<div class="meta"><span>'+label+'</span><span>'+value+'</span></div>' for label,value in rows)
    fee_row = ('<div class="meta"><span>Delivery fee</span><span>'+_esc(_peso(fee))+'</span></div>') if fee or order.get('fulfillment')=='Delivery' else ''
    discount = float(order.get('discount_amount') or 0)
    discount_label = str(order.get('discount_kind') or 'Discount')
    scope = str(order.get('discount_scope') or '')
    discount_row = ('<div class="meta"><span>'+_esc(discount_label)+' '+_esc(scope)+' discount ('+_esc(order.get('discount_percent') or 0)+'%)</span><span>−'+_esc(_peso(discount))+'</span></div>') if discount else ''
    tender = float(order.get('tendered_amount') or 0)
    change = float(order.get('change_amount') or 0)
    tender_row = ('<div class="meta"><span>Cash received</span><span>'+_esc(_peso(tender))+'</span></div><div class="meta"><span>Change</span><span>'+_esc(_peso(change))+'</span></div>') if order.get('payment_method')=='Cash' and order.get('payment_status')=='paid' and tender else ''
    note_row = '<p class="note"><b>Notes:</b> '+notes+'</p>' if notes else ''
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>La Luna Receipt - '''+_esc(order.get('id',''))+'''</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#eaeaea;color:#151515;font-family:Arial,Helvetica,sans-serif}
.paper{width:'''+str(paper_width)+'''mm;max-width:100%;padding:3mm 2mm 6mm;background:white;margin:20px auto;font-size:11px;line-height:1.45}
.name{text-align:center;font-size:17px;font-weight:900;letter-spacing:1.1px;margin:4px 0 1px}.sub{text-align:center;font-size:10px;letter-spacing:1.4px}
.center{text-align:center}.muted{color:#555}.rule{border:0;border-top:1px dashed #333;margin:12px 0}.meta,.item{display:flex;justify-content:space-between;gap:7px;margin:5px 0}.meta span:first-child{color:#444;flex:0 0 35%}.meta span:last-child{text-align:right;overflow-wrap:anywhere;min-width:0}
.item div{min-width:0;flex:1;overflow-wrap:anywhere}.item strong:last-child{text-align:right;white-space:nowrap}.item small{display:block;color:#555;font-size:10px}
.total{font-size:15px;font-weight:900;display:flex;justify-content:space-between}.payment{border:1px solid #111;padding:6px;text-align:center;font-weight:bold;overflow-wrap:anywhere;margin-top:8px}
.note{overflow-wrap:anywhere}.actions{display:flex;gap:10px;justify-content:center;padding:20px}.actions button{background:#161616;color:white;border:0;border-radius:8px;padding:10px 20px;font-weight:bold}
@page{size:'''+str(paper_width)+'''mm auto;margin:0}@media print{body{background:white}.paper{margin:0;width:'''+str(paper_width)+'''mm;padding:2mm 2mm 5mm}.actions{display:none}html,body{width:'''+str(paper_width)+'''mm}}
</style></head><body><div class="actions"><button type="button" onclick="window.print()">Print receipt</button><button type="button" onclick="window.close()">Close</button></div>
<main class="paper"><div class="name">LA LUNA</div><div class="sub">GRILL + CAFE</div><p class="center">Catarman, Camiguin<br>FB Messenger: laluna.cmg</p>
<hr class="rule">'''+fields+'''<hr class="rule">'''+''.join(line_items)+'''<hr class="rule">
<div class="meta"><span>Subtotal</span><span>'''+_esc(_peso(order.get('subtotal',0)))+'''</span></div>'''+fee_row+discount_row+'''
<div class="total"><span>TOTAL</span><span>'''+_esc(_peso(order.get('total',0)))+'''</span></div>'''+tender_row+'''
<div class="payment">'''+status+'''</div>'''+note_row+'''
<hr class="rule"><p class="center">Thank you for visiting La Luna!<br><span class="muted">Please keep this receipt.</span></p></main></body></html>'''


def _normalize_plain(value):
    return (str(value or '').replace('₱','PHP ').replace('×','x').replace('—','-')
            .replace('–','-').replace('’',"'").replace('“','"').replace('”','"'))


def escpos_bytes(order=None, paper_width=58, test=False):
    """ESC/POS thermal bytes (text only) for printers using CP437 or ASCII fallback."""
    if paper_width not in PAPER_WIDTHS:
        raise ValueError('Unsupported receipt width')
    if test:
        order = {'id':'TEST-RECEIPT','created':datetime.now(MANILA).isoformat(),
                 'fulfillment':'Dine-in','payment_status':'test','payment_method':'Cash',
                 'customer_name':'Sample Customer', 'subtotal':145.0,'delivery_fee':0,
                 'total':145.0, 'items':[{'qty':1,'name':'Sample Coffee','mods':'','total':95.0},
                                       {'qty':1,'name':'Sample Pastry','mods':'','total':50.0}]}
    if not order:
        raise ValueError('Missing receipt order')
    cols = 32 if paper_width==58 else 48
    buff = bytearray(b'\x1b@')  # Initialize ESC/POS printer
    def data(s): buff.extend(_normalize_plain(s).encode('cp437',errors='replace'))
    def ln(s=''): data(s+'\n')
    def center(s): ln(s.center(cols))
    def section(): ln('-'*cols)
    def pair(left,right):
        left = _normalize_plain(left);right=_normalize_plain(right)
        max_left=max(4,cols-len(right)-1)
        wrapped = textwrap.wrap(left,max_left,break_long_words=True,break_on_hyphens=False) or ['']
        for fragment in wrapped[:-1]: ln(fragment)
        last=wrapped[-1]
        ln(last+(' '*max(1,cols-len(last)-len(right)))+right)
    buff.extend(b'\x1ba\x01')  # center
    center('LA LUNA')
    center('GRILL + CAFE')
    center('Catarman, Camiguin')
    buff.extend(b'\x1ba\x00')  # left
    section()
    for label, value in [
        ('Order',order.get('id','')),
        ('Date',_ph_datetime(order.get('created',''))),
        ('Type',order.get('fulfillment','')),
        ('Customer',order.get('customer_name') or 'Walk-in'),
        *([('Served by',order.get('served_by_name'))] if order.get('served_by_name') else []),
        ('Payment',order.get('payment_method','')),
    ]:
        ln(label+': '+_normalize_plain(value))
    location=order.get('table_number') or order.get('address') or ''
    if location:
        for line in textwrap.wrap('Table / address: '+_normalize_plain(location),cols):ln(line)
    section()
    for item in order.get('items',[]):
        for line in textwrap.wrap(str(item.get('qty',1))+' x '+_normalize_plain(item.get('name','Item')),cols):ln(line)
        if item.get('mods'):
            for line in textwrap.wrap('  '+_normalize_plain(item['mods']),cols):ln(line)
        pair('',_peso(item.get('total',0)))
    section()
    pair('Subtotal',_peso(order.get('subtotal',0)))
    if order.get('fulfillment')=='Delivery' or order.get('delivery_fee'):
        pair('Delivery',_peso(order.get('delivery_fee',0)))
    if order.get('discount_amount'):
        pair(str(order.get('discount_kind') or 'Discount')+' discount ('+str(order.get('discount_percent') or 0)+'%)',
             '-'+_peso(order.get('discount_amount')))
    pair('TOTAL',_peso(order.get('total',0)))
    if order.get('payment_method')=='Cash' and order.get('payment_status')=='paid' and order.get('tendered_amount'):
        pair('Cash received',_peso(order.get('tendered_amount')))
        pair('Change',_peso(order.get('change_amount',0)))
    section()
    center('TEST ONLY - NOT A SALE' if test else str(order.get('payment_status','unpaid')).upper())
    note = order.get('notes','')
    if note:
        ln('Notes:')
        for line in textwrap.wrap(_normalize_plain(note),cols):ln(line)
    section()
    buff.extend(b'\x1ba\x01')
    center('Thank you for visiting La Luna!')
    buff.extend(b'\n\n\n\x1dV\x00')  # full cut, ignored by printers without cutter
    return bytes(buff)


def send_to_printer(payload:bytes, config:dict):
    """Submitting bytes confirms spool/serial write, not physical paper output."""
    method=config['method']
    if os.name!='nt':
        raise RuntimeError('Direct receipt printing requires the La Luna server to run on Windows')
    if method=='windows_raw':
        try:
            import win32print
        except ImportError:
            raise RuntimeError('Windows RAW printing requires pywin32')
        handle=win32print.OpenPrinter(config['target'])
        started=False
        page=False
        try:
            job_id=win32print.StartDocPrinter(handle,1,('La Luna Thermal Receipt',None,'RAW'))
            started=True
            win32print.StartPagePrinter(handle);page=True
            written=win32print.WritePrinter(handle,payload)
            if written!=len(payload):raise RuntimeError('Printer accepted only part of the receipt')
            return {'queued':True,'job_id':job_id,'bytes_written':written,'message':'Submitted to Windows print queue; check the physical printout'}
        finally:
            if page:win32print.EndPagePrinter(handle)
            if started:win32print.EndDocPrinter(handle)
            win32print.ClosePrinter(handle)
    if method=='bluetooth_serial':
        try:
            import serial
        except ImportError:
            raise RuntimeError('Bluetooth serial printing requires pyserial')
        with serial.Serial(config['target'],baudrate=config['baud_rate'],timeout=3,write_timeout=5) as conn:
            written=conn.write(payload)
            conn.flush()
            if written!=len(payload):raise RuntimeError('Printer accepted only part of the receipt')
            return {'queued':True,'bytes_written':written,'message':'Sent to Bluetooth serial port; check the physical printout'}
    raise ValueError('Select a direct print method, or use the browser print dialog')
