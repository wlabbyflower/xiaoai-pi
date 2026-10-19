"""集中配置：环境变量 > config.json > 代码默认值。

敏感信息（小米账号、token）只放 config.json 或环境变量，不要写进代码。
config.json 已被 .gitignore 忽略，权限建议 600。
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get('XIAOAI_CONFIG') or (BASE / 'config.json'))

DEFAULTS = {
    # --- 小米账号（仅首次登录需要）---
    'mi_user': '',
    'mi_pass': '',
    # --- 设备（discover.py 会自动填）---
    'device_id': '',      # miNA deviceID，UUID 形式
    'device_did': '',     # MIoT did，纯数字
    'device_name': '',    # 可选：同名多台时用来挑
    'hardware': '',       # 设备型号，如 S12A / L05C
    'dlna_ip': '',        # 音箱局域网 IP
    'dlna_port': 9999,
    # --- pi ---
    'pi_bin': 'pi',
    'pi_cwd': str(Path.home()),
    # --- 认证缓存 ---
    'auth_cache': str(Path.home() / '.mi_token.json'),
    # --- HTTP 服务 ---
    'http_host': '127.0.0.1',
    'http_port': 8447,
    'https_port': 0,      # 0 = 不开 HTTPS；手机用麦克风必须 >0
    # --- 语音识别 ---
    'whisper_model': 'small',
    'whisper_lang': 'zh',
    'whisper_prompt': ('以下是简体中文普通话。常见专有名词：参数、报价、价格、'
                       '型号、规格、显卡、内存、存储。'),
    # --- 播报 ---
    'tts_voice': 'zh-CN-XiaoxiaoNeural',
    'speak_mode': 'auto',    # auto | dlna | cloud
    # --- 行为 ---
    'memory_auto': True,     # 每轮自动抽取长期记忆
    'session_ttl': 600,      # 空闲多久开新会话（秒），0 = 不复位
    'peer_turns': 6,         # 注入搭档 PI 会话的最近轮数，0 = 关闭
}

_ENV_MAP = {
    'mi_user': 'MI_USER',
    'mi_pass': 'MI_PASS',
    'device_id': 'MI_DEVICE_ID',
    'device_did': 'MI_DID',
    'device_name': 'MI_DEVICE_NAME',
    'hardware': 'MI_HARDWARE',
    'dlna_ip': 'BRIDGE_DLNA_IP',
    'dlna_port': 'BRIDGE_DLNA_PORT',
    'pi_bin': 'PI_BIN',
    'pi_cwd': 'BRIDGE_PI_CWD',
    'auth_cache': 'MI_TOKEN_FILE',
    'http_host': 'PI_API_HOST',
    'http_port': 'PI_API_PORT',
    'https_port': 'PI_HTTPS_PORT',
    'whisper_model': 'PI_WHISPER_MODEL',
    'whisper_lang': 'PI_WHISPER_LANG',
    'whisper_prompt': 'PI_WHISPER_PROMPT',
    'tts_voice': 'BRIDGE_TTS_VOICE',
    'speak_mode': 'BRIDGE_SPEAK_MODE',
    'memory_auto': 'BRIDGE_MEMORY_AUTO',
    'session_ttl': 'BRIDGE_SESSION_TTL',
    'peer_turns': 'BRIDGE_PEER_TURNS',
}


def _coerce(raw: str, default):
    if isinstance(default, bool):
        return raw.strip().lower() in ('1', 'true', 'yes', 'on')
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def _read_config() -> dict:
    """读 config.json；文件缺失或格式坏了都当空配置处理。"""
    if not CONFIG_PATH.is_file():
        return {}
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(CONFIG_PATH.read_text())
        if isinstance(data, dict):
            return data
    return {}


def load() -> dict:
    cfg = dict(DEFAULTS)
    data = _read_config()
    if data:
        cfg.update({k: v for k, v in data.items()
                    if v not in (None, '') and k in DEFAULTS})
    for key, env in _ENV_MAP.items():
        raw = os.environ.get(env)
        if raw not in (None, ''):
            cfg[key] = _coerce(raw, DEFAULTS[key])
    return cfg


def save(updates: dict) -> Path:
    """把发现到的设备信息写回 config.json（合并，600 权限）。"""
    data = _read_config()
    data.update({k: v for k, v in updates.items() if v not in (None, '')})
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.chmod(CONFIG_PATH, 0o600)
    except OSError as e:
        raise RuntimeError(f'写配置失败 {CONFIG_PATH}: {e}') from e
    return CONFIG_PATH


def missing(cfg: dict) -> list:
    """列出还不能跑起来的必填项。"""
    need = [('device_id', '音箱 deviceID'), ('dlna_ip', '音箱局域网 IP')]
    return [label for key, label in need if not cfg.get(key)]


CFG = load()
