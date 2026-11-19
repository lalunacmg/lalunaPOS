"""Start the La Luna local web server and open the customer/back-office pages.

Requires dependencies from requirements.txt. Credentials stay in this process's
inherited environment for initial owner creation; nothing is saved in a script.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from network_access import lan_addresses, ensure_firewall_rule_interactive
import webbrowser

ROOT = Path(__file__).resolve().parent
BASE = 'http://127.0.0.1:8000'
WIFI_MODE = os.environ.get('LALUNA_ALLOW_WIFI') == '1'
EXPECTED_BUILD = '2026-11-19-product-image-upload-v1'


def server_health():
    try:
        with urlopen(BASE + '/api/health', timeout=1) as response:
            data = json.load(response)
        if data.get('ok') is True and data.get('provider') == 'Local delivery':
            return data
    except (OSError, URLError, ValueError, TypeError, KeyError):
        pass
    return None


def is_our_server():
    return bool(server_health())


def wifi_ipv4_addresses():
    return [entry['ip'] for entry in lan_addresses()]


def port_is_taken():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(('127.0.0.1', 8000)) == 0


def owner_exists(db_path):
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(str(db_path)) as con:
            return con.execute("SELECT COUNT(*) FROM staff WHERE role='owner'").fetchone()[0] > 0
    except sqlite3.Error:
        return False


def prepare_owner_credentials(env):
    configured_db = Path(env.get('LALUNA_DB_PATH', str(ROOT / 'laluna.db')))
    if owner_exists(configured_db):
        print('Existing owner account found. Use your original login.')
        return

    print('\nFIRST-TIME OWNER SETUP')
    print('This information creates your owner login in the local database.')
    print('Enter an email address and a password at least 12 characters long.\n')
    email = env.get('LALUNA_ADMIN_EMAIL', '').strip().lower()
    while not email or '@' not in email or ' ' in email:
        email = input('Owner email: ').strip().lower()
        if not email or '@' not in email or ' ' in email:
            print('Please enter a valid email address.')
    password = env.get('LALUNA_ADMIN_PASSWORD', '')
    while len(password) < 12:
        password = getpass.getpass('Owner password (12+ characters, hidden): ')
        if len(password) < 12:
            print('Password must contain at least 12 characters.')
    env['LALUNA_ADMIN_EMAIL'] = email
    env['LALUNA_ADMIN_PASSWORD'] = password


def open_pages():
    print('\nLa Luna customer site: ' + BASE + '/')
    print('La Luna back office:  ' + BASE + '/backoffice')
    if WIFI_MODE:
        adapters = lan_addresses()
        print('SAME-WI-FI PHONE TEST ADDRESSES (choose your actual Wi-Fi adapter):')
        for entry in adapters:
            print('  ' + entry['adapter'] + ': http://' + entry['ip'] + ':8000/qr-test')
        if not adapters:
            print('  No LAN IP detected. Run ipconfig and find your Wi-Fi IPv4 address.')
        print('On the phone, use the SAME Wi-Fi as this computer; allow Python in the Windows Private-network firewall.')
        print('LAN mode is for testing on a trusted private network, NOT public internet hosting.')
    else:
        print('This launcher is local-only. For customer phones, restart this BAT and choose menu option 2.')
    print('For customers on any network, deploy the app to a secure HTTPS host.\n')
    try:
        webbrowser.open_new_tab(BASE + '/')
        webbrowser.open_new_tab(BASE + '/backoffice')
    except Exception:
        pass


def main():
    os.chdir(ROOT)
    print('LA LUNA GRILL + CAFE  |  LOCAL WEBSITE LAUNCHER')
    print('-' * 48)
    running = server_health()
    if running:
        if running.get('build') != EXPECTED_BUILD:
            print('ERROR: An older La Luna server is already using port 8000.')
            print('Close the earlier server window, then start the updated project again.')
            print('Do not run old and new La Luna installations simultaneously.')
            return 1
        if WIFI_MODE and not running.get('lan_access'):
            print('ERROR: La Luna is already running in local-only mode on port 8000.')
            print('Stop the OLD server (Ctrl+C or close its launcher window), then start this Wi-Fi launcher again.')
            return 1
        print('La Luna is already running on port 8000.')
        open_pages()
        return 0
    if port_is_taken():
        print('ERROR: Port 8000 is in use by another application.')
        print('Stop that application before starting La Luna, then run this file again.')
        return 1

    env = os.environ.copy()
    if WIFI_MODE:
        ensure_firewall_rule_interactive()
    prepare_owner_credentials(env)

    cmd = [sys.executable, '-m', 'uvicorn', 'server:app',
           '--host', '0.0.0.0' if WIFI_MODE else '127.0.0.1', '--port', '8000']
    print('\nStarting the server. Keep this window open while you use the website.\n')
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
    except OSError as error:
        print('Could not start Python server:', error)
        return 1
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if is_our_server():
                open_pages()
                print('Press Ctrl+C here to stop La Luna.\n')
                return proc.wait()
            if proc.poll() is not None:
                print('\nServer exited early. Check the error messages above.')
                return proc.returncode or 1
            time.sleep(0.3)
        print('\nServer did not become ready. Check the errors above.')
        return 1
    except KeyboardInterrupt:
        print('\nStopping local server...')
        return 0
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print('\nSetup cancelled.')
        raise SystemExit(1)
