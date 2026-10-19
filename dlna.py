#!/usr/bin/env python3
"""把音箱当 DLNA 播放器用（绕开小米云 TTS）

原理：小爱音箱 S12 在 :9999 上暴露 UPnP MediaRenderer，
      发 SOAP 到 /AVTransport/control 就能让它播放任意 HTTP 音频链接。
好处：不依赖小米云、不吃 token、没有风控，延迟更低。
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import config

CFG = config.load()
DLNA_IP = CFG['dlna_ip']
DLNA_PORT = str(CFG['dlna_port'])
SECONDARY_IPS = [ip.strip() for ip in
                 os.environ.get('BRIDGE_DLNA_IPS', '').split(',') if ip.strip()]
BASE = Path(__file__).resolve().parent
AUDIO_DIR = BASE / 'audio'

TTS_VOICE = CFG['tts_voice']


def _find_edge_tts() -> str:
    """优先用当前解释器同目录下的 edge-tts（venv 里默认不在 PATH 上）。"""
    local = Path(sys.executable).parent / 'edge-tts'
    if local.is_file():
        return str(local)
    return shutil.which('edge-tts') or 'edge-tts'


EDGE_TTS = _find_edge_tts()

_ENVELOPE = (
    '<?xml version="1.0"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
    '<u:{action} xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
    '<InstanceID>0</InstanceID>{extra}</u:{action}>'
    '</s:Body></s:Envelope>'
)


def _candidates() -> list:
    ips = [DLNA_IP] + SECONDARY_IPS
    seen, out = set(), []
    for ip in ips:
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _soap(ip: str, action: str, extra: str = '', timeout: float = 6.0) -> str:
    if not re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', ip):
        raise OSError(f'非法渲染器地址: {ip!r}')
    body = _ENVELOPE.format(action=action, extra=extra).encode()
    url = f'http://{ip}:{DLNA_PORT}/AVTransport/control'
    if not url.startswith('http://'):        # 只允许 http 到本机内网 IP
        raise OSError(f'拒绝非 http 地址: {url!r}')
    req = urllib.request.Request(
        url, data=body,
        headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPACTION': f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')


def find_renderer() -> str:
    """逐个试，谁能应答 GetTransportInfo 就用谁。"""
    for ip in _candidates():
        try:
            if 'TransportState' in _soap(ip, 'GetTransportInfo', timeout=3):
                return ip
        except OSError:
            continue
    return ''


def _parse_time(value: str) -> float:
    """H:MM:SS(.mmm) / M:SS -> 秒。"""
    try:
        parts = [float(x) for x in value.strip().split(':')]
    except (AttributeError, ValueError):
        return 0.0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] if parts else 0.0


def position(ip: str = '') -> tuple:
    """返回 (当前播放位置秒, 轨道总长秒)。"""
    try:
        xml = _soap(ip or DLNA_IP, 'GetPositionInfo', timeout=4)
    except OSError:
        return 0.0, 0.0
    rel = re.search(r'<RelTime>([^<]*)<', xml)
    dur = re.search(r'<TrackDuration>([^<]*)<', xml)
    return (_parse_time(rel.group(1)) if rel else 0.0,
            _parse_time(dur.group(1)) if dur else 0.0)


def state(ip: str = '') -> str:
    ip = ip or DLNA_IP
    try:
        xml = _soap(ip, 'GetTransportInfo', timeout=4)
    except OSError:
        return 'unreachable'
    m = re.search(r'<CurrentTransportState>([^<]+)<', xml)
    return m.group(1) if m else 'unknown'


def play_url(url: str, ip: str = '', title: str = 'pi') -> bool:
    """让音箱播放一个 HTTP 音频地址。"""
    ip = ip or DLNA_IP
    meta = (
        '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
        f'&lt;item id="1" parentID="0" restricted="1"&gt;&lt;dc:title&gt;{title}'
        '&lt;/dc:title&gt;&lt;upnp:class&gt;object.item.audioItem.musicTrack'
        '&lt;/upnp:class&gt;&lt;res protocolInfo="http-get:*:audio/mpeg:*"&gt;'
        f'{url}&lt;/res&gt;&lt;/item&gt;&lt;/DIDL-Lite&gt;')
    try:
        # 关键：音箱默认是「单曲循环」(loop_type=1)，不关掉会把一句话反复播。
        # SetPlayMode NORMAL 是本地 UPnP 动作，不需要经过小米云。
        with contextlib.suppress(OSError):
            _soap(ip, 'Stop')
        with contextlib.suppress(OSError):
            _soap(ip, 'SetPlayMode', '<NewPlayMode>NORMAL</NewPlayMode>')
        _soap(ip, 'SetAVTransportURI',
              f'<CurrentURI>{url}</CurrentURI><CurrentURIMetaData>{meta}</CurrentURIMetaData>')
        _soap(ip, 'Play', '<Speed>1</Speed>')
        return True
    except OSError:
        return False


def stop(ip: str = '') -> None:
    with contextlib.suppress(OSError):
        _soap(ip or DLNA_IP, 'Stop')


def synthesize(text: str, ip: str = '') -> str:
    """用 edge-tts 本地合成 mp3，返回文件路径（失败返回空串）。"""
    AUDIO_DIR.mkdir(exist_ok=True)
    safe = re.sub(r'[^0-9a-zA-Z]+', '', str(abs(hash(text))))[:16]
    out = AUDIO_DIR / f'tts_{safe}.mp3'
    if out.exists() and out.stat().st_size > 512:
        return str(out)
    fd, tmp = tempfile.mkstemp(suffix='.mp3', dir=str(AUDIO_DIR))
    os.close(fd)
    try:
        subprocess.run([EDGE_TTS, '--voice', TTS_VOICE, '--text', text,
                        '--write-media', tmp],
                       check=True, capture_output=True, timeout=60)
        os.replace(tmp, out)
        return str(out)
    except (subprocess.SubprocessError, OSError):
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return ''


_play_gen = 0
_gen_lock = threading.Lock()


def duration_seconds(path: str) -> float:
    """读 mp3 时长（ffprobe）；失败返回 0。"""
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return float(out) if out else 0.0
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0.0


def _watch_and_stop(ip: str, generation: int, fallback: float) -> None:
    """盯播放位置：到末尾或发现回跳（循环重启）立刻 Stop。

    这台音箱默认单曲循环，放完会重新拉同一个 URL；
    只靠「定时」会踩在重启点上，所以用位置判断更可靠。
    """
    last = -1.0
    deadline = time.time() + fallback + 8
    while time.time() < deadline:
        with _gen_lock:
            if generation != _play_gen:
                return
        pos, dur = position(ip)
        if dur > 0 and pos >= dur - 0.45:
            break
        if last >= 0 and pos < last - 1.0:      # 位置回跳 = 已经开始重播
            break
        if pos > last:
            last = pos
        time.sleep(0.35)
    with _gen_lock:
        if generation != _play_gen:
            return
    with contextlib.suppress(OSError):
        _soap(ip, 'Stop')


def _schedule_stop(ip: str, generation: int, seconds: float) -> None:
    """播完就停：这台音箱默认单曲循环，不主动 Stop 会把一句话念到天荒地老。"""
    def run():
        time.sleep(max(0.5, seconds))
        with _gen_lock:
            if generation != _play_gen:      # 期间又播了新内容，别打断它
                return
        with contextlib.suppress(OSError):
            _soap(ip, 'Stop')

    threading.Thread(target=run, daemon=True).start()


def speak(text: str, base_url: str, ip: str = '') -> bool:
    """合成并通过 DLNA 播报。base_url 形如 http://<本机内网IP>:8447。"""
    global _play_gen
    ip = ip or DLNA_IP          # 不解析成真实 IP 的话，定时停播会因为空地址静默失败
    path = synthesize(text, ip)
    if not path:
        return False
    fname = Path(path).name
    ok = play_url(f'{base_url.rstrip("/")}/audio/{fname}', ip, 'pi reply')
    if ok:
        with _gen_lock:
            _play_gen += 1
            gen = _play_gen
        dur = duration_seconds(path) or max(2.0, len(text) / 4.5)
        _watch_and_stop(ip, gen, dur)
        _schedule_stop(ip, gen, max(0.8, dur - 0.25))   # 兜底：略早于结尾停下
    return ok


if __name__ == '__main__':
    print('可用渲染器:', find_renderer() or '未找到')
    print('当前状态:', state())
