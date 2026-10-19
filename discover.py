#!/usr/bin/env python3
"""自动发现小爱音箱：deviceID / did / hardware / 局域网 IP，并写回 config.json

用法：
  python discover.py                 # 用 config.json 里的账号登录，自动找音箱
  python discover.py --scan 192.168.1   # 额外扫某个网段找 DLNA 端口
  python discover.py --name 客厅       # 有多台音箱时按名字挑
  python discover.py --dlna 192.168.1.23  # 直接指定 IP（跳过扫描）
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900
MSEARCH = (b'M-SEARCH * HTTP/1.1\r\n'
           b'HOST: 239.255.255.250:1900\r\n'
           b'MAN: "ssdp:discover"\r\n'
           b'MX: 2\r\nST: ssdp:all\r\n\r\n')


def _lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def ssdp_probe(extra_hosts=(), wait: float = 6.0) -> dict:
    """找局域网里的 UPnP MediaRenderer，返回 {ip: device_id}。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', 0))
    sock.settimeout(0.5)

    targets = [SSDP_ADDR] + list(extra_hosts)
    for _ in range(3):                       # 三轮，轮间等一下让设备回包
        for host in targets:
            with contextlib.suppress(OSError):
                sock.sendto(MSEARCH, (host, SSDP_PORT))
        time.sleep(0.4)

    found: dict = {}
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(8192)
        except TimeoutError:
            continue
        text = data.decode('utf-8', 'ignore')
        if 'MediaRenderer' not in text and 'xiaomi' not in text.lower():
            continue
        loc = re.search(r'Location:\s*(\S+)', text)
        usn = re.search(r'USN:\s*uuid:([0-9a-fA-F-]{36})', text)
        ip = addr[0]
        device_id = usn.group(1) if usn else ''
        if loc and not device_id:
            device_id = _device_id_from_xml(loc.group(1))
        found[ip] = device_id
    sock.close()
    return found


def _device_id_from_xml(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=4) as r:   # noqa: S310 - 仅内网 http
            body = r.read().decode('utf-8', 'ignore')
    except OSError:
        return ''
    m = re.search(r'<UDN>uuid:([0-9a-fA-F-]{36})</UDN>', body)
    return m.group(1) if m else ''


async def _discover_via_cloud(cfg: dict, name_hint: str) -> list:
    from miservice import MiAccount, MiIOService, MiNAService  # type: ignore[import-not-found]
    acc = MiAccount(None, cfg['mi_user'], cfg['mi_pass'], cfg['auth_cache'])
    token = await acc.token_store.load_token() if acc.token_store else None
    if token:
        acc.token = token
    else:
        for sid in ('xiaomiio', 'micoapi'):
            if not await acc.login(sid):
                raise RuntimeError(f'登录失败: {acc._login_error}')

    mina = await MiNAService(acc).device_list()
    try:
        miio = await MiIOService(acc).device_list()
    except Exception:  # noqa: BLE001 - did 拿不到不影响主流程
        miio = []
    by_name = {d.get('name'): d for d in (miio or [])}

    out = []
    for d in (mina or []):
        if not isinstance(d, dict) or 'deviceID' not in d:
            continue
        nm = d.get('name') or ''
        if name_hint and name_hint not in nm:
            continue
        extra = by_name.get(nm) or {}
        out.append({
            'device_id': d.get('deviceID', ''),
            'device_did': str(extra.get('did') or d.get('miotDID') or ''),
            'hardware': d.get('hardware', '') or '',
            'device_name': nm,
            'serial': d.get('serialNumber', ''),
            'rom': d.get('romVersion', ''),
            'model': extra.get('model', ''),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--scan', action='append', default=[],
                    help='额外扫描的网段，如 192.168.1（会单播 SSDP 到该网段全部地址）')
    ap.add_argument('--name', default='', help='按设备名筛选')
    ap.add_argument('--dlna', default='', help='直接指定音箱 IP')
    ap.add_argument('--no-save', action='store_true', help='只打印，不写 config.json')
    args = ap.parse_args()

    cfg = config.load()
    print(f'本机 IP: {_lan_ip()}')

    devices = []
    if cfg.get('mi_user') and cfg.get('mi_pass') or Path(cfg['auth_cache']).exists():
        try:
            devices = asyncio.run(_discover_via_cloud(cfg, args.name))
        except Exception as e:  # noqa: BLE001 - 登录不了就走纯网络发现
            print(f'[云] 查询失败（可以只靠网络发现）: {e}')
    if devices:
        print(f'\n云上找到 {len(devices)} 台小爱设备:')
        for i, d in enumerate(devices, 1):
            print(f"  {i}. {d['device_name']}  hw={d['hardware']}  "
                  f"did={d['device_did']}  rom={d['rom']}  serial={d['serial']}")
    else:
        print('未从云端拿到设备列表（需要 config.json 里的 mi_user / mi_pass）')

    # --- 找 DLNA IP ---
    hosts = []
    for net in args.scan:
        hosts.extend(f'{net}.{i}' for i in range(1, 255))
    print('\n扫描局域网 UPnP 设备（约 10 秒）…')
    found = ssdp_probe(hosts)
    # 用扫到的 UUID 反查是哪台设备，避免「设备填 A、IP 填 B」
    by_uuid = {d['device_id'].lower(): d for d in devices if d.get('device_id')}
    matched = None
    for ip, dev_id in found.items():
        if dev_id and dev_id.lower() in by_uuid:
            matched = (ip, by_uuid[dev_id.lower()])
            break
    if matched:
        devices = [matched[1]] + [d for d in devices if d is not matched[1]]
    wanted = devices[0]['device_id'] if devices else ''
    dlna_ip = args.dlna or (matched[0] if matched else '')
    if not dlna_ip and len(found) == 1:
        dlna_ip = next(iter(found))
    if found:
        print('发现以下 UPnP 设备:')
        for ip, dev_id in found.items():
            mark = ' <== 与云上设备匹配' if wanted and dev_id.lower() == wanted.lower() else ''
            print(f'  {ip}  uuid={dev_id or "?"}{mark}')
    else:
        print('  没扫到（音箱可能在别的网段，试试 --scan 或直接 --dlna 指定 IP）')

    if not devices and not dlna_ip:
        print('\n什么都没发现，先确认账号密码或指定 --dlna IP')
        return 1

    updates = {}
    if devices:
        d = devices[0]
        updates = {'device_id': d['device_id'], 'device_did': d['device_did'],
                   'hardware': d['hardware'], 'device_name': d['device_name']}
    if dlna_ip:
        updates['dlna_ip'] = dlna_ip

    print('\n将写入配置:')
    print(json.dumps(updates, ensure_ascii=False, indent=2))
    if not args.no_save and updates:
        path = config.save(updates)
        print(f'已写入 {path}（权限 600，已在 .gitignore 里）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
