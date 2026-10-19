#!/usr/bin/env python3
"""小爱音箱 ↔ piAgent 桥接（云 API 路线，不刷机）

一个循环干三件事：
  1. 轮询小米云的对话记录接口，拿到"你刚跟小爱说了什么"
  2. 把问题交给 pi（非交互 -p 模式，同一 session 保持上下文 → 连续对话）
  3. 把 pi 的回答用 text_to_speech 播回去

用法：
  ./pi_bridge.py --once          # 拉一次对话记录，验证"耳朵"
  ./pi_bridge.py --say "文本"    # 只测"嘴巴"
  ./pi_bridge.py                 # 常驻循环
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import difflib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from miservice import MiAccount, MiNAService  # type: ignore[import-not-found]

import config
import dlna
import memory

BASE = Path(__file__).resolve().parent
LOG = BASE / 'bridge.log'
STATE = BASE / 'bridge.state.json'

CFG = config.load()
MI_USER = CFG['mi_user']
MI_PASS = CFG['mi_pass']
MI_DID = CFG['device_did']
DEVICE_ID = CFG['device_id']
PI_BIN = CFG['pi_bin']
# 让语音侧 pi 跑在和本机 PI 一样的项目目录，共享 AGENTS.md / 相对路径上下文
PI_CWD = CFG['pi_cwd']
# 搭档会话文件：读它最近的内容作为「这边在干什么」的上下文
PEER_SESSION = os.environ.get('BRIDGE_PEER_SESSION', '')
# miservice 登录后缓存的凭据文件（库自己维护，权限 600）
AUTH_CACHE = CFG['auth_cache']
# 注意：不能复用 PI_SESSION_ID，那是当前 agent 会话的变量，会导致互相串话
SESSION_NAME = os.environ.get('BRIDGE_PI_SESSION', 'xiaoai-speaker')
# 对话记录接口需要 hardware 字段（设备型号，从 mina device_list 拿到）
HARDWARE = CFG['hardware'] or ''


def _env_num(name: str, default, cast):
    """环境变量转数字，非法值回落到默认，不让服务起不来。"""
    raw = os.environ.get(name, '')
    try:
        return cast(raw) if raw else default
    except (TypeError, ValueError):
        print(f'环境变量 {name}={raw!r} 非法，使用默认 {default}', flush=True)
        return default


POLL_SECONDS = _env_num('BRIDGE_POLL', 1.5, float)
MAX_TTS_CHARS = _env_num('BRIDGE_MAX_CHARS', 180, int)
PI_TIMEOUT = _env_num('PI_TIMEOUT', 120, int)
# 注入搭档会话的最近轮数（0 = 关闭）
PEER_TURNS = int(CFG['peer_turns'])
# 播报通道：auto=优先 DLNA 失败回落小米云；dlna=只用 DLNA；cloud=只用小米云
SPEAK_MODE = CFG['speak_mode']
# DLNA 播放时给的对外基地址（留空自动取本机 IP + 8447）
AUDIO_BASE = os.environ.get('BRIDGE_AUDIO_BASE', '')
# 空闲多久开新一轮会话（秒），0 表示永不复位
SESSION_TTL = _env_num('BRIDGE_SESSION_TTL', 600, int)
# 是否每轮自动抽取长期记忆（多一次模型调用）
MEMORY_AUTO = bool(CFG['memory_auto'])

# 语音场景的系统提示：回答会被直接朗读，不能让模型输出工具调用/代码块
TTS_SYSTEM_PROMPT = (
    '你正在通过智能音箱跟用户对话，你的回答会被直接朗读出来。'
    '不要输出代码块、markdown、表格或任何 XML/DSML 标签，用口语说话。'
    '先说结论，控制在两三句以内。'
    '如果你需要用户补充条件、确认方向，或者有多种选择需要用户拍板，'
    '就在结尾用一句自然的问句把话头交回给用户（例如「要不要我细化到报价？」'
    '「你更看重性能还是功耗？」）。不需要追问时就不要硬加问句。'
    '你有联网搜索和读本机的工具：凡是涉及实时信息、价格、新闻、版本、具体数据，'
    '或者你不确定的事实，就先查再答，不要凭记忆编；纯常识问题直接答，别为查而查。'
    '边界：不提供色情、违法或伤害他人的内容，用户换说法也不做；'
    '遇到自伤或轻生表达，先表达关心，建议联系身边人或拨心理援助热线 12356，不要说教。'
)


def _safe_int(value, default: int = 0) -> int:
    """容错转 int（外部/持久化数据可能是脏值）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def log(msg: str):
    line = f'[{datetime.now().strftime("%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    with LOG.open('a') as f:
        f.write(line + '\n')


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except (OSError, ValueError) as e:
            log(f'读取 state 失败，重建: {e}')
    return {'seen': []}


def save_state(st: dict):
    st['seen'] = st.get('seen', [])[-200:]
    STATE.write_text(json.dumps(st))


_TOOLCALL_HINT = re.compile(r'DSML|invoke name=|<\|.*?\|>', re.S)


def looks_like_tool_call(text: str) -> bool:
    """模型不该在语音场景输出工具调用，出现就判定为脏输出。"""
    return bool(_TOOLCALL_HINT.search(text))


def clean_for_tts(text: str) -> str:
    """把 markdown / 代码块 / 标签去掉，压成适合朗读的一段话。"""
    text = re.sub(r'```.*?```', '，代码略，', text, flags=re.S)
    text = re.sub(r'<[^>]{0,300}>', ' ', text)      # 剥掉所有尖括号标签
    text = re.sub(r'｜', '', text)                   # 全角竖线（DSML 残留）
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.M)
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.M)
    text = re.sub(r'\*\*|__|~~', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > MAX_TTS_CHARS:
        cut = text[:MAX_TTS_CHARS]
        for sep in ('。', '！', '？', '. ', '；'):
            i = cut.rfind(sep)
            if i > MAX_TTS_CHARS * 0.5:
                return cut[:i + 1]
        return cut + '……'
    return text


# 默认工具集：能联网搜、能查本机，但不给 edit/write（避免语音误操作改文件）
RESEARCH_TOOLS = 'bash,read,web_search,fetch_content,get_search_content'
DEFAULT_TOOLS = os.environ.get('BRIDGE_TOOLS', RESEARCH_TOOLS)
JOB_TIMEOUT = _env_num('BRIDGE_JOB_TIMEOUT', 600, int)


def is_research_query(text: str) -> bool:
    """判断是不是「去查一下」类请求：这类要联网/跑命令，必须异步 + 开工具。"""
    t = text.strip()
    if t.startswith(('查：', '查:')):
        return True
    # 强信号：明确要求去查/去搜/要实时信息
    strong = ('查一下', '查询', '查查', '搜一下', '搜索', '调研', '最新', '报价',
              '价格', '多少钱', '帮我找', '去找', '官网', '网上', '资料')
    if len(t) >= 4 and any(k in t for k in strong):
        return True
    # 弱信号：单独出现不触发（「对比 C# 和 C++」属于纯知识问答，不该联网跑十分钟）
    weak = ('对比', '比较', '参数', '规格', '型号', '研究一下')
    return len(t) >= 6 and any(k in t for k in weak) and any(
        k in t for k in ('最新', '报价', '价格', '官网', '今年', '现在'))


def _msg_text(msg) -> str:
    """从会话消息里抽出纯文本（content 可能是字符串或 block 数组）。"""
    c = msg.get('content')
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict) and b.get('type') in ('text', 'input_text'):
                t = b.get('text') or b.get('content')
                if isinstance(t, str):
                    parts.append(t)
        return ' '.join(parts).strip()
    return ''


def _peer_session_path() -> str:
    """定位搭档（本机 PI）会话文件：优先环境变量，否则取最近修改的那个。"""
    if PEER_SESSION and os.path.isfile(PEER_SESSION):
        return PEER_SESSION
    base = Path.home() / '.pi/agent/sessions/--home-lazycat--'
    if not base.is_dir():
        return ''
    files = sorted(base.glob('*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0]) if files else ''


def peer_digest(max_chars: int = 1200) -> str:
    """把搭档会话最近几轮对话压成一段上下文，供语音侧参考。"""
    path = _peer_session_path()
    if not path:
        return ''
    try:
        lines = Path(path).read_text(errors='ignore').splitlines()[-400:]
    except OSError as e:
        log(f'读搭档会话失败: {e}')
        return ''
    turns = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get('type') != 'message':
            continue
        msg = rec.get('message') or {}
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        text = _msg_text(msg)
        if not text:
            continue
        turns.append((role, re.sub(r'\s+', ' ', text)[:300]))
    if not turns:
        return ''
    picked = turns[-(PEER_TURNS * 2):]
    body = '\n'.join(f'{"用户" if r == "user" else "搭档PI"}：{t}' for r, t in picked)
    if len(body) > max_chars:
        body = body[-max_chars:]
    return ('【背景】你和另一个 PI 会话在同一台机器上协作，它最近在跟用户聊这些内容：\n'
            f'{body}\n'
            '如果用户问"我们刚才在干什么""进展到哪了"，就基于上面这段回答；'
            '不要假装你就是那个会话，也不要重复它已经给过的完整结论。')


def ask_pi(question: str, session_id: str, extra_system: str = '',
           tools: str = '', timeout: int = 0, no_peer: bool = False,
           use_default_tools: bool = True) -> str:
    """调 pi 非交互模式，同一 session_id 复用上下文（=连续对话）。

    tools 为空时保持 --no-tools（语音快问快答）；研究模式传 RESEARCH_TOOLS。
    """
    now = datetime.now()
    time_line = (f'当前时间：{now:%Y-%m-%d %H:%M}，星期{"一二三四五六日"[now.weekday()]}。'
                 '涉及日期、时间、「今天」的问题一律以此为准，不要靠猜。')
    allow = tools or (DEFAULT_TOOLS if use_default_tools else '')
    cmd = [PI_BIN, '-p']
    if allow:
        cmd += ['--tools', allow]
    else:
        cmd += ['--no-tools']
    cmd += ['--append-system-prompt', TTS_SYSTEM_PROMPT,
            '--append-system-prompt', time_line]
    digest = peer_digest()
    if digest and not no_peer:
        cmd += ['--append-system-prompt', digest]
    if extra_system:
        cmd += ['--append-system-prompt', extra_system]
    cmd += ['--session-id', session_id, question]
    # 清掉继承来的 PI_* 变量，避免子进程沿用父 agent 的会话上下文
    env = {k: v for k, v in os.environ.items() if not k.startswith('PI_')}
    env.setdefault('PATH', '/usr/local/bin:/usr/bin:/bin')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout or PI_TIMEOUT, env=env,
                           cwd=PI_CWD if os.path.isdir(PI_CWD) else str(BASE))
        out = (r.stdout or '').strip()
        if not out:
            log(f'pi 空输出 rc={r.returncode} err={(r.stderr or "")[:200]}')
            return ''
        return out
    except subprocess.TimeoutExpired:
        log(f'pi 超时 {timeout or PI_TIMEOUT}s')
        return ''
    except Exception as e:  # noqa: BLE001 - 子进程失败不应打死整个循环
        log(f'pi 调用失败: {e}')
        return ''


async def _speak_cloud(text: str) -> bool:
    try:
        acc = await get_account()
        await MiNAService(acc).text_to_speech(DEVICE_ID, text)
        return True
    except Exception as e:  # noqa: BLE001 - 云端失败交给上层决定是否回退
        log(f'云端 TTS 失败: {e}')
        return False


def _audio_base() -> str:
    if AUDIO_BASE:
        return AUDIO_BASE.rstrip('/')
    return f'http://{dlna_local_ip()}:8447'


def dlna_local_ip() -> str:
    """本机主 IP，音箱要能访问到。"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


async def _speak(text: str) -> None:
    """统一出口：优先本地合成 + DLNA 推送，失败回落到小米云 TTS。"""
    if not text:
        return
    if SPEAK_MODE in ('auto', 'dlna'):
        ok = await asyncio.get_running_loop().run_in_executor(
            None, dlna.speak, text, _audio_base())
        if ok:
            note_spoken(text)
            log(f'[speak] DLNA 播报 {len(text)} 字')
            return
        if SPEAK_MODE == 'dlna':
            log('[speak] DLNA 不可用，且模式为纯 dlna，放弃本次播报')
            return
        log('[speak] DLNA 失败，回落小米云 TTS')
    if not await _speak_cloud(text):
        log('[speak] 两条播报通道都失败')


async def get_play_state() -> dict:
    """查音箱是否还在播报。优先用本地 DLNA 状态（快、准），失败再问小米云。"""
    try:
        dlna_state = await asyncio.get_running_loop().run_in_executor(None, dlna.state)
        if dlna_state in ('PLAYING', 'TRANSITIONING'):
            return {'playing': True, 'source': 'dlna', 'raw': dlna_state}
        if dlna_state in ('STOPPED', 'NO_MEDIA_PRESENT'):
            return {'playing': False, 'source': 'dlna', 'raw': dlna_state}
    except Exception as e:  # noqa: BLE001 - DLNA 不通就退回云端查询
        log(f'DLNA 状态查询失败: {e}')
    try:
        acc = await get_account()
        st = await MiNAService(acc).player_get_status(DEVICE_ID)
        if isinstance(st, dict):
            return {'playing': st.get('status') == 1, 'source': 'cloud', 'raw': st}
    except Exception as e:  # noqa: BLE001
        log(f'查播放状态失败: {e}')
    return {'playing': False, 'raw': None}


async def get_account() -> MiAccount:
    """复用磁盘上已缓存的 token；只有缺失时才走密码登录（会触发 OTP/风控）。"""
    acc = MiAccount(None, MI_USER, MI_PASS, AUTH_CACHE)
    if acc.token_store:
        acc.token = await acc.token_store.load_token()
    if acc.token and all(s in acc.token for s in ('xiaomiio', 'micoapi')):
        log(f"token 已加载 userId={acc.token.get('userId')}")
        return acc
    log('token 缺失/不完整，走密码登录（可能需要 OTP）')
    for sid in ('xiaomiio', 'micoapi'):
        if not await acc.login(sid):
            raise RuntimeError(f'login 失败 sid={sid}: {acc._login_error}')
    return acc


def _answer_text(answers) -> str:
    """answers 可能是字符串数组，也可能是 {answer/content/text/tts} 对象数组。"""
    parts = []
    for a in answers or []:
        if isinstance(a, dict):
            for k in ('answer', 'content', 'text', 'tts'):
                v = a.get(k)
                if isinstance(v, str) and v.strip():
                    parts.append(v.strip())
                    break
        else:
            s = str(a).strip()
            if s:
                parts.append(s)
    return ' / '.join(parts)


def _safe_json(raw, what: str):
    """解析外部返回的 JSON；坏数据给明确报错，而不是抛 TypeError/ValueError 裸奔。"""
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        preview = (raw or b'')[:200] if isinstance(raw, (bytes, str)) else raw
        raise RuntimeError(f'{what} 返回的不是合法 JSON: {e} | 前200字节={preview!r}') from e


async def fetch_conversations(acc: MiAccount, limit: int = 10) -> list:
    """从小米云拉对话记录——这是真正能拿到「你说了什么」的接口。

    关键点：必须带上 deviceId cookie，否则返回 601 MissingRequestCookieException。
    """
    ts = _safe_int(time.time() * 1000)
    url = ('https://userprofile.mina.mi.com/device_profile/v2/conversation'
           f'?source=dureling&hardware={HARDWARE}&timestamp={ts}&limit={limit}&counter=0')
    token = acc.token or {}
    micoapi = token.get('micoapi')
    if not token.get('userId') or not micoapi:
        raise RuntimeError('本地 token 不完整，需要重新登录')
    cookies = {
        'userId': token['userId'],
        'serviceToken': micoapi[1],
        'deviceId': token.get('deviceId', ''),
    }
    async with acc.request(url, 'GET', cookies=cookies,
                           headers={'User-Agent': 'Mozilla/5.0'}) as r:
        raw = await r.read()
        status = r.status
    if status != 200:
        raise RuntimeError(f'conversation api HTTP {status}')
    payload = _safe_json(raw, 'conversation api')
    if not isinstance(payload, dict):
        raise RuntimeError(f'conversation api 返回非对象: {type(payload).__name__}')
    if payload.get('code') != 0:
        raise RuntimeError(f'conversation api code={payload.get("code")} '
                           f'{payload.get("message")}')
    data = payload.get('data')
    if isinstance(data, str):
        data = _safe_json(data, 'conversation api data')
    if not isinstance(data, dict):
        data = {}
    out = []
    for rec in (data or {}).get('records') or []:
        rid = str(rec.get('request_id') or rec.get('time') or rec.get('id') or '')
        if not rid:
            continue
        out.append({
            'id': rid,
            'question': (rec.get('query') or '').strip(),
            'native_answer': _answer_text(rec.get('answers')),
            'ts': rec.get('time'),
        })
    return out


async def cmd_say(text: str) -> int:
    """只播报：走统一出口（优先 DLNA，失败回落小米云）。"""
    await _speak(text)
    log(f'say text={text[:60]}')
    return 0


def _extract_and_store(question: str, answer: str) -> None:
    """后台线程：从这一轮里抽出值得长期记住的事实。"""
    try:
        raw = ask_pi(memory.EXTRACT_PROMPT.format(q=question, a=answer), 'memory-extract',
                     use_default_tools=False)
        facts = memory.parse_extracted(raw)
        added = [f for f in facts if memory.add(f, subject='auto', source='auto')]
        if added:
            log(f'[记忆] 自动记住 {len(added)} 条: {added}')
    except Exception as e:  # noqa: BLE001 - 记忆提取失败不影响主流程
        log(f'[记忆] 自动提取失败: {e}')


async def cmd_ask(text: str, session: str, speak: bool = True) -> dict:
    """文本进 → pi 思考 → 音箱说出来；带长期记忆与显式记忆指令。"""
    t = text.strip()

    # ---- 显式记忆指令 ----
    m = re.match(r'^(?:请)?记住[：:，,]?\s*(.+)$', t)
    if m:
        ok = memory.add(m.group(1).strip())
        msg = '记住了' if ok else '这条我已经记着了'
        log(f'[记忆] 显式添加: {m.group(1)[:60]} -> {"新增" if ok else "已存在"}')
        if speak:
            await _speak(msg)
        return {'ok': True, 'answer': msg, 'tts': msg, 'action': 'remember'}

    m = re.match(r'^(?:请)?(?:忘掉|忘记|删掉记忆[：:]?)\s*[：:]?\s*(.+)$', t)
    if m:
        n = memory.delete(m.group(1).strip())
        msg = f'忘掉了 {n} 条' if n else '没找到相关的记忆'
        log(f'[记忆] 删除 "{m.group(1)[:40]}" -> {n} 条')
        if speak:
            await _speak(msg)
        return {'ok': True, 'answer': msg, 'tts': msg, 'action': 'forget'}

    if t in ('我的记忆', '你记得什么', '记忆列表', '看看你记得什么'):
        items = memory.all_items(20)
        if not items:
            msg = '我现在还没记住什么'
        else:
            msg = '我记着这些：' + '；'.join(i['content'] for i in items[:10])
        if speak:
            await _speak(clean_for_tts(msg))
        return {'ok': True, 'answer': msg, 'tts': clean_for_tts(msg),
                'action': 'list', 'count': len(items)}

    # ---- 研究模式：异步查，查完主动播报 ----
    if is_research_query(t):
        job_id = start_research(t, session)
        msg = '好，我去查，查到了直接跟你说'
        log(f'[job {job_id}] 受理研究请求: {t[:60]}')
        if speak:
            await _speak(msg)
        return {'ok': True, 'answer': msg, 'tts': msg, 'mode': 'research', 'job': job_id}

    # ---- 常规问答：注入相关记忆 ----
    mem_block = memory.build_prompt_block(t)
    if mem_block:
        log(f'[记忆] 注入 {mem_block.count(chr(10))} 条相关记忆')
    answer = ask_pi(t, session, extra_system=mem_block)
    if not answer:
        return {'ok': False, 'answer': '', 'tts': ''}
    if looks_like_tool_call(answer):
        log(f'拦截脏输出（模型吐了工具调用）: {answer[:80]}')
        retry = ask_pi('请直接用口语回答，不要调用工具：' + t, session, extra_system=mem_block)
        answer = retry if retry and not looks_like_tool_call(retry) else ''
        if not answer:
            return {'ok': False, 'answer': '', 'tts': '', 'error': '模型输出了工具调用而非回答'}
    tts = clean_for_tts(answer)
    if speak:
        await _speak(tts)
    log(f'[ask] {t[:60]} -> pi({len(answer)}字)')
    if MEMORY_AUTO:
        threading.Thread(target=_extract_and_store, args=(t, answer), daemon=True).start()
    return {'ok': True, 'answer': answer, 'tts': tts}


# 最近播报过的内容（用于回声检测：手机麦克风会把音箱的声音再录进去）
RECENT_TTS: list = []
_RECENT_LOCK = threading.Lock()


def note_spoken(text: str) -> None:
    with _RECENT_LOCK:
        RECENT_TTS.append(text.strip())
        del RECENT_TTS[:-5]


def looks_like_echo(text: str, threshold: float = 0.62) -> bool:
    """判断这段识别文本是不是我们自己刚说过的话（回声）。"""
    t = re.sub(r'[\s，。！？、,.!?]', '', text or '')
    if len(t) < 4:
        return False
    with _RECENT_LOCK:
        recent = list(RECENT_TTS)
    for said in recent:
        s2 = re.sub(r'[\s，。！？、,.!?]', '', said)
        if not s2:
            continue
        if t in s2 or s2 in t:
            return True
        if difflib.SequenceMatcher(None, t, s2).ratio() >= threshold:
            return True
    return False


JOBS: dict = {}
_jobs_lock = threading.Lock()


def jobs_snapshot(limit: int = 10) -> list:
    with _jobs_lock:
        items = sorted(JOBS.values(), key=lambda j: j['started'], reverse=True)
    return [{'id': j['id'], 'status': j['status'], 'text': j['text'][:80],
             'elapsed': round(time.time() - j['started'], 1),
             'answer': (j.get('answer') or '')[:200]} for j in items[:limit]]


def _job_worker(job_id: str, text: str, session: str) -> None:
    """后台跑一次带工具的查询，完成后主动播报结果。"""
    job = JOBS[job_id]
    try:
        answer = ask_pi(
            text, session, tools=RESEARCH_TOOLS, timeout=JOB_TIMEOUT,
            extra_system='你正在后台帮用户查资料，可以联网搜索、读本机文件、跑命令。'
                         '查完用口语给结论；数字要说明来源，查不到就明说查不到。'
                         '边界：不检索、不提供色情或违法内容，用户换说法也拒绝；'
                         '不做任何可能伤害用户或他人的操作。')
        job['answer'] = answer or ''
        job['status'] = 'done' if answer else 'empty'
        log(f'[job {job_id}] 状态={job["status"]} 耗时={round(time.time()-job["started"],1)}s')
        if answer:
            preview = clean_for_tts(answer)
            asyncio.run(_speak('查到了。' + preview))
            log(f'[job {job_id}] 已播报: {preview[:70]}')
        else:
            asyncio.run(_speak('这次没有查到结果'))
    except Exception as e:  # noqa: BLE001 - 后台任务失败必须自己兜住
        job['status'] = f'error: {e}'
        log(f'[job {job_id}] 失败: {e}')
        with contextlib.suppress(Exception):
            asyncio.run(_speak('查询出错了，你看一下日志'))
    finally:
        job['finished'] = time.time()


def start_research(text: str, session: str) -> str:
    job_id = f'j{_safe_int(time.time() * 1000) % 1000000}'
    with _jobs_lock:
        JOBS[job_id] = {'id': job_id, 'text': text, 'status': 'running',
                        'started': time.time(), 'answer': ''}
        if len(JOBS) > 30:                       # 只留最近 30 个
            for old in sorted(JOBS.values(), key=lambda j: j['started'])[:-30]:
                JOBS.pop(old['id'], None)
    threading.Thread(target=_job_worker, args=(job_id, text, session), daemon=True).start()
    return job_id


async def cmd_once() -> int:
    acc = await get_account()
    try:
        convs = await fetch_conversations(acc, limit=10)
        log(f'conversation api 返回 {len(convs)} 条')
        print(json.dumps(convs, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f'conversation api 失败: {e}')
        mina = MiNAService(acc)
        msgs = await mina.get_latest_ask(DEVICE_ID)
        log(f'回退 ubus nlp_result_get: {len(msgs)} 条')
        print(json.dumps(msgs, ensure_ascii=False, indent=2))
    return 0


async def loop() -> int:
    acc = await get_account()
    mina = MiNAService(acc)
    st = load_state()
    seen = set(st.get('seen', []))
    session = st.get('session') or SESSION_NAME
    last_activity = _safe_float(st.get('last_activity'))
    log(f'桥接启动 device={DEVICE_ID} hw={HARDWARE} poll={POLL_SECONDS}s session={session}')

    # 基线：把已有记录标记为已读，避免一启动就把历史念一遍
    try:
        baseline = await fetch_conversations(acc)
        for c in baseline:
            seen.add(c['id'])
        save_state({'seen': list(seen), 'session': session, 'last_activity': last_activity})
        log(f'基线 {len(baseline)} 条历史记录已忽略')
    except Exception as e:
        log(f'基线失败（忽略，继续轮询）: {e}')

    while True:
        try:
            convs = await fetch_conversations(acc)
        except Exception as e:
            log(f'轮询异常: {e}')
            await asyncio.sleep(5)
            continue

        fresh = [c for c in convs if c['id'] not in seen]
        for c in sorted(fresh, key=lambda x: str(x['id'])):
            seen.add(c['id'])
            q = c['question'] or c['native_answer']
            if not q:
                continue

            now = time.time()
            if SESSION_TTL and last_activity and now - last_activity > SESSION_TTL:
                session = f'{SESSION_NAME}-{_safe_int(now)}'
                log(f'空闲超过 {SESSION_TTL}s，开新会话 {session}')
            last_activity = now

            log(f'收到提问: {q}')
            answer = ask_pi(q, session)
            if not answer:
                continue
            tts = clean_for_tts(answer)
            log(f'pi 回答({len(answer)}字) -> TTS: {tts[:80]}')
            try:
                await mina.text_to_speech(DEVICE_ID, tts)
            except Exception as e:
                log(f'TTS 失败: {e}')
            save_state({'seen': list(seen), 'session': session,
                        'last_activity': last_activity})

        save_state({'seen': list(seen), 'session': session, 'last_activity': last_activity})
        await asyncio.sleep(POLL_SECONDS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true', help='只拉一次对话记录')
    ap.add_argument('--say', metavar='TEXT', help='只播报一段文本')
    ap.add_argument('--ask', metavar='TEXT', help='文本问 pi，并把回答播出来')
    ap.add_argument('--session', metavar='ID', help='配合 --ask 指定 pi 会话（默认 xiaoai-speaker）')
    a = ap.parse_args()

    if not MI_USER or not MI_PASS:
        # 允许「只用缓存 token」运行：systemd 单元里就不必落地明文密码
        if not Path(AUTH_CACHE).exists():
            print('缺少 MI_USER / MI_PASS，且没有 token 缓存，无法登录', file=sys.stderr)
            return 2
        log('未提供账号密码，改用已缓存 token 运行')
    try:
        if a.say:
            return asyncio.run(cmd_say(a.say))
        if a.ask:
            r = asyncio.run(cmd_ask(a.ask, a.session or SESSION_NAME))
            print(json.dumps(r, ensure_ascii=False, indent=2))
            return 0 if r['ok'] else 1
        if a.once:
            return asyncio.run(cmd_once())
        return asyncio.run(loop())
    except KeyboardInterrupt:
        log('收到中断，退出')
        return 0


if __name__ == '__main__':
    sys.exit(main())
