"""Windows LAN discovery and private-network QR reachability helpers.

Only inspect local interfaces; never send arbitrary user-provided URLs to the network.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
from urllib.request import build_opener, ProxyHandler


def _valid_ip(value):
    try:
        ip = ipaddress.ip_address(str(value))
        return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local
    except ValueError:
        return False


def lan_addresses():
    """Return ranked local IPv4 interfaces, Wi-Fi before ethernet / VPN."""
    addresses = []
    if os.name == 'nt':
        ps = ("$ErrorActionPreference='Stop'; "
              "Get-NetIPConfiguration | Where-Object {$_.IPv4Address} | ForEach-Object { "
              "[PSCustomObject]@{name=$_.InterfaceAlias; "
              "ipv4=@($_.IPv4Address | ForEach-Object {$_.IPv4Address}); "
              "gateway=[bool]$_.IPv4DefaultGateway} "
              "} | ConvertTo-Json -Depth 4 -Compress")
        try:
            result = subprocess.run(
                ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps],
                text=True, capture_output=True, timeout=8, check=True)
            records = json.loads(result.stdout.strip().lstrip('\ufeff') or '[]')
            if isinstance(records, dict):
                records = [records]
            for record in records:
                name = str(record.get('name') or 'Network adapter')
                ips = record.get('ipv4') or []
                if isinstance(ips, str):
                    ips = [ips]
                for addr in ips:
                    if _valid_ip(addr):
                        addresses.append({'ip':str(addr), 'adapter':name,
                                          'gateway':bool(record.get('gateway'))})
        except (subprocess.SubprocessError, ValueError, OSError, TypeError):
            pass

    # ipconfig is a second Windows-only fallback when PowerShell network
    # cmdlets are unavailable or blocked by policy. Ignore DNS/router rows.
    if os.name == 'nt' and not addresses:
        try:
            output = subprocess.run(['ipconfig'], capture_output=True, text=True,
                                    errors='replace', timeout=5, check=True).stdout
            adapter = 'Windows network adapter'
            for line in output.splitlines():
                line = line.strip()
                if line.endswith(':') and 'adapter' in line.lower():
                    adapter = line[:-1].split('adapter ', 1)[-1]
                if 'IPv4' in line:
                    for addr in re.findall(r'(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)',line):
                        if _valid_ip(addr):
                            addresses.append({'ip':addr,'adapter':adapter,'gateway':False})
        except (OSError, subprocess.SubprocessError):
            pass

    # Fallback for platforms without PowerShell or with restricted permissions.
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 80))
            candidates.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    for ip in candidates:
        if _valid_ip(ip) and not any(record['ip'] == ip for record in addresses):
            addresses.append({'ip': ip, 'adapter': 'Detected network address', 'gateway':False})

    def rank(item):
        alias = item['adapter'].lower()
        if any(token in alias for token in ('wi-fi', 'wifi', 'wireless', 'wlan')):
            return 0
        if any(token in alias for token in ('vpn', 'virtual', 'wsl', 'docker', 'hyper-v', 'vmware', 'vbox', 'tailscale', 'zerotier')):
            return 4
        if 'ethernet' in alias or 'local area connection' in alias:
            return 1
        return 2 if item['gateway'] else 3

    addresses.sort(key=lambda item: (rank(item), not item['gateway'], item['adapter'].lower()))
    seen = set()
    return [x for x in addresses if not (x['ip'] in seen or seen.add(x['ip']))]


def ips():
    return [x['ip'] for x in lan_addresses()]


def self_health(ip, port=8000, timeout=2):
    """Probe only an explicit local-IP candidate. Returns health JSON or None."""
    if ip not in ips():
        return None
    # Ignore system HTTP proxies for LAN checks (VPN / proxy can break them).
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f'http://{ip}:{port}/api/health', timeout=timeout) as response:
            if response.status != 200:
                return None
            data = json.loads(response.read(4096).decode('utf-8'))
            return data if isinstance(data, dict) and data.get('ok') is True else None
    except (ValueError, OSError, TimeoutError, json.JSONDecodeError):
        return None


def ensure_firewall_rule_interactive():
    """Ask before adding an inbound TCP 8000 rule limited to PRIVATE local subnet."""
    if os.name != 'nt':
        return
    name = 'La Luna Ordering Private LAN 8000'
    try:
        result = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule', 'name=' + name],
                                capture_output=True, timeout=5)
        if result.returncode == 0:
            print('Windows Firewall: existing private-LAN rule found.')
            return
    except (OSError, subprocess.SubprocessError):
        pass
    print('\nWINDOWS FIREWALL: Customer phones may need inbound access to port 8000.')
    print('Optional rule: TCP port 8000, PRIVATE networks, LOCAL SUBNET only.')
    print('This does not expose the site to the public internet or change your router.')
    try:
        consent = input('Allow Windows to request administrator approval for this rule? [y/N]: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if consent != 'y':
        print('Firewall unchanged. You may need to allow Python on PRIVATE networks manually.')
        return
    ps = ("$p = Start-Process -FilePath 'netsh.exe' -ArgumentList "
          "'advfirewall firewall add rule name=\"La Luna Ordering Private LAN 8000\" "
          "dir=in action=allow protocol=TCP localport=8000 remoteip=localsubnet "
          "profile=private enable=yes' -Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    encoded = base64.b64encode(ps.encode('utf-16le')).decode('ascii')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive',
                                 '-EncodedCommand', encoded], timeout=45)
        if result.returncode == 0:
            print('Firewall rule added for private-network devices only.')
        else:
            print('Firewall rule not added. Check UAC approval or configure Windows Firewall manually.')
    except (OSError, subprocess.SubprocessError):
        print('Firewall setup could not finish; you can configure it in Windows Security.')


if __name__ == '__main__':
    for entry in lan_addresses():
        print(f"{entry['adapter']}: {entry['ip']} -> http://{entry['ip']}:8000/qr-test")
    if len(sys.argv) > 1 and sys.argv[1] == '--firewall':
        ensure_firewall_rule_interactive()
