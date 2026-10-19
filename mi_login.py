#!/usr/bin/env python3
"""小米账号登录 + 设备清单（支持在无 TTY 环境下手动输入 OTP 验证码）。

流程：
  1. 登录 xiaomiio / micoapi 两个 sid
  2. 如需 OTP，脚本会把状态写入 STATUS 文件并等待 /tmp/.mi_otp 出现验证码
  3. 登录成功后拉取 MIoT 设备清单 + MiNA(小爱) 设备清单
  4. 结果写入 RESULT，token 缓存到 TOKEN（600）
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from miservice import MiAccount, MiIOService, MiNAService  # type: ignore[import-not-found]

import config

CFG = config.load()
BASE = Path(__file__).resolve().parent
OTP_FILE = BASE / '.mi_otp'
STATUS = BASE / '.mi_status'
RESULT = BASE / '.mi_devices.json'
# miservice 登录后缓存的凭据文件
AUTH_CACHE = Path(CFG['auth_cache'])

SIDS = ('xiaomiio', 'micoapi')


def random_device_id(length: int = 16) -> str:
    """生成与 miservice 内部格式一致的 hex 大写 deviceId（无需引入内部模块）。"""
    import random
    import string
    alphabet = string.ascii_letters + string.digits
    return ''.join(random.choice(alphabet) for _ in range(length)).upper()


def log(msg: str):
    STATUS.write_text(f'{time.strftime("%H:%M:%S")} {msg}\n')
    print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)


async def otp_callback(method: str) -> str:
    """SMS/Email 验证码：等待 otp 文件出现，最多 15 分钟。"""
    log(f'WAITING_OTP {method}')
    if OTP_FILE.exists():
        code = OTP_FILE.read_text().strip()
        if code:
            OTP_FILE.unlink(missing_ok=True)
            log(f'GOT_OTP_PRESEEDED {method} -> {code}')
            return code
    OTP_FILE.unlink(missing_ok=True)
    for _ in range(900):
        if OTP_FILE.exists():
            code = OTP_FILE.read_text().strip()
            if code:
                OTP_FILE.unlink(missing_ok=True)
                log(f'GOT_OTP {method} -> {code}')
                return code
        await asyncio.sleep(1)
    raise Exception('OTP timeout after 15min')


async def main() -> int:
    user = os.environ.get('MI_USER')
    pw = os.environ.get('MI_PASS')
    if not user or not pw:
        log('MISSING_CREDENTIALS')
        return 2

    account = MiAccount(None, user, pw, str(AUTH_CACHE), otp_callback)
    # 复用固定 deviceId，避免每次重试都换设备导致验证码绑定失效
    did_file = BASE / '.mi_deviceid'
    if did_file.exists() and did_file.read_text().strip():
        account.token = {'deviceId': did_file.read_text().strip()}
        log(f'REUSE deviceId={account.token["deviceId"]}')
    else:
        account.token = {'deviceId': random_device_id()}
        did_file.write_text(account.token['deviceId'])
        did_file.chmod(0o600)
        log(f'NEW deviceId={account.token["deviceId"]}')

    for sid in SIDS:
        log(f'LOGIN sid={sid}')
        ok = await account.login(sid)
        if not ok:
            log(f'LOGIN_FAILED sid={sid} err={account._login_error}')
            return 1
        log(f'LOGIN_OK sid={sid} userId={account.token.get("userId")}')

    miio = MiIOService(account)
    log('FETCH miio device_list')
    devices = await miio.device_list()

    mina = MiNAService(account)
    log('FETCH mina device_list')
    try:
        mdevices = await mina.device_list()
    except Exception as e:
        mdevices = {'error': str(e)}
        log(f'MINA_FAILED {e}')

    data = {'miio': devices, 'mina': mdevices}
    RESULT.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f'DONE miio={len(devices) if isinstance(devices, list) else "?"} '
        f'mina={len(mdevices) if isinstance(mdevices, list) else "?"}')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
