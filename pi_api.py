#!/usr/bin/env python3
"""小爱音箱 HTTP/HTTPS 接口：打字或说话 → pi 思考 → 音箱回答

端点：
  GET  /                    网页对话界面
  GET  /mic                 浏览器麦克风页面（需 HTTPS）
  POST /ask   {"text":…}    问 pi 并播报
  POST /say   {"text":…}    只播报
  POST /stt   <audio bytes> 语音转文字（faster-whisper 本地识别）
  GET  /status              状态

默认同时监听：
  HTTP  0.0.0.0:8447
  HTTPS 0.0.0.0:8448 （自签证书，麦克风必须走这个）
"""
from __future__ import annotations

import contextlib
import difflib
import importlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

pb = importlib.import_module('pi_bridge')
config = importlib.import_module('config')
CFG = config.load()

CERT_DIR = BASE / 'certs'
CERT_FILE = CERT_DIR / 'lan.crt'
KEY_FILE = CERT_DIR / 'lan.key'


def _to_int(raw, default: int) -> int:
    """容错转 int：外部头/环境变量可能不是数字，不能让服务起不来。"""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


HOST = CFG['http_host']
PORT = _to_int(CFG['http_port'], 8447)
HTTPS_PORT = _to_int(CFG['https_port'], 0)   # 0 = 不开 HTTPS
WHISPER_MODEL = CFG['whisper_model']
WHISPER_LANG = CFG['whisper_lang']
# 热词：把领域专有名词喂给模型，显著减少同音错字（可在 config.json 里改）
WHISPER_PROMPT = CFG['whisper_prompt']

_model = None
_model_lock = threading.Lock()


def lan_ip() -> str:
    """取本机对外主 IP（不真的发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'


def ensure_cert() -> bool:
    """生成自签证书（带 SAN，否则浏览器连 IP 会报错）。"""
    if CERT_FILE.exists() and KEY_FILE.exists():
        return True
    CERT_DIR.mkdir(mode=0o700, exist_ok=True)
    ip = lan_ip()
    cmd = [
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(KEY_FILE), '-out', str(CERT_FILE),
        '-days', '825', '-subj', '/CN=xiaoai-pi',
        '-addext', f'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:{ip}',
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        KEY_FILE.chmod(0o600)
        pb.log(f'已生成自签证书 SAN 含 {ip}')
        return True
    except (subprocess.SubprocessError, OSError) as e:
        pb.log(f'生成证书失败（HTTPS 关闭）: {e}')
        return False


def get_model():
    """懒加载 whisper 模型，失败返回 None。"""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        except ImportError as e:
            pb.log(f'faster-whisper 未安装，语音识别不可用: {e}')
            return None
        pb.log(f'加载 whisper 模型 {WHISPER_MODEL}（首次会下载）…')
        try:
            _model = WhisperModel(WHISPER_MODEL, device='cpu', compute_type='int8')
        except Exception as e:  # noqa: BLE001 - 模型加载失败不该打死服务
            pb.log(f'whisper 模型加载失败: {e}')
            return None
        pb.log('whisper 模型就绪')
        return _model


def _decode_pcm(path: str):
    """用 ffmpeg 解成 16k 单声道 float32，绕开 PyAV 的 metadata_errors 兼容问题。"""
    import numpy as np
    proc = subprocess.run(
        ['ffmpeg', '-v', 'quiet', '-i', path,
         '-f', 's16le', '-ac', '1', '-ar', '16000', '-'],
        capture_output=True, check=True, timeout=120)
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(raw: bytes, suffix: str = '.webm') -> str:
    model = get_model()
    if model is None:
        raise RuntimeError('语音识别不可用（模型未就绪）')
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(raw)
        path = f.name
    try:
        audio = _decode_pcm(path)
        if audio.size == 0:
            return ''
        segments, _info = model.transcribe(audio, language=WHISPER_LANG, vad_filter=True,
                                          initial_prompt=WHISPER_PROMPT)
        return ''.join(s.text for s in segments).strip()
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


# ---------------- 页面 ----------------

TALK_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>连续对话</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:20px auto;padding:0 14px;background:#111;color:#eee}
 #big{width:100%;padding:18px;font-size:20px;border-radius:12px;border:0;background:#ef4444;color:#fff;font-weight:600}
 #big.on{background:#22c55e}
 #state{font-size:15px;color:#fcd34d;margin:12px 0;min-height:22px}
 #log{margin-top:14px;white-space:pre-wrap;font-size:15px;line-height:1.65}
 .q{color:#7dd3fc}.a{color:#a7f3d0}.e{color:#fca5a5}.m{color:#888;font-size:13px}
 a{color:#7dd3fc}
 input#s{background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:6px;padding:6px}
</style>
<h2>连续对话</h2>
<div class="m">点一次开始，之后它会自动听、答、说完再听。它问你就直接答。</div>
<p><label>会话 <input id=s value="talk"></label></p>
<button id=big onclick="toggle()">▶ 开始对话</button>
<div id=state>待机</div>
<div id=log></div>
<p><a href="/">打字界面</a> · <a href="/mic">单句录音</a></p>
<script>
const $=id=>document.getElementById(id);
const log=(c,t)=>{const d=document.createElement('div');d.className=c;d.textContent=t;$('log').prepend(d)};
const setState=t=>{$('state').textContent=t};
let running=false, stream=null, ac=null, analyser=null, data=null;

async function toggle(){
  if(running){ stopAll(); return; }
  try{
    stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  }catch(e){ log('e','拿不到麦克风：'+e+'（必须用 HTTPS 打开并授权）'); return; }
  ac=new (window.AudioContext||window.webkitAudioContext)();
  const src=ac.createMediaStreamSource(stream);
  analyser=ac.createAnalyser(); analyser.fftSize=1024; src.connect(analyser);
  data=new Uint8Array(analyser.fftSize);
  running=true; $('big').textContent='⏹ 结束对话'; $('big').classList.add('on');
  log('m','对话开始');
  loop();
}

function stopAll(){
  running=false; $('big').textContent='▶ 开始对话'; $('big').classList.remove('on');
  setState('待机'); log('m','对话结束');
  if(stream) stream.getTracks().forEach(t=>t.stop());
  if(ac) ac.close().catch(()=>{});
  stream=null; ac=null; analyser=null;
}

const rms=()=>{ analyser.getByteTimeDomainData(data);
  let s=0; for(let i=0;i<data.length;i++){ const v=(data[i]-128)/128; s+=v*v; }
  return Math.sqrt(s/data.length); };

// 录一句：等到有人说话、再等到静音，自动结束
function recordUtterance(){
  return new Promise(resolve=>{
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')?'audio/webm;codecs=opus':'audio/webm';
    let rec; const chunks=[];
    let spoke=false, lastVoice=0, started=performance.now(), finished=false;
    let noise=0.0, nsamples=0, thr=0.02;

    const finish=(ok)=>{
      if(finished) return; finished=true; clearInterval(timer);
      $('state').textContent='…';
      rec.onstop=()=>resolve(ok&&chunks.length?new Blob(chunks,{type:'audio/webm'}):null);
      try{ rec.stop(); }catch(e){ resolve(null); }
    };

    try{ rec=new MediaRecorder(stream,{mimeType:mime}); }
    catch(e){ resolve(null); return; }
    rec.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
    rec.start(200);

    const timer=setInterval(()=>{
      if(!running){ finish(false); return; }
      const v=rms(), now=performance.now();
      if(!spoke){
        if(nsamples<10){ noise+=v; nsamples++; thr=Math.max(0.02, (noise/nsamples)*3); }
        if(v>thr){ spoke=true; lastVoice=now; setState('🎙 在听你说…'); }
        else if(now-started>12000){ finish(false); }        // 12 秒没人说话就放弃
      }else{
        if(v>thr) lastVoice=now;
        if(now-lastVoice>1200) finish(true);                // 静音 1.2 秒判定说完
        else if(now-started>25000) finish(true);            // 单句上限 25 秒
      }
    },100);
  });
}

async function stt(blob){
  const r=await fetch('/stt',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:blob});
  const j=await r.json();
  if(!j.ok){ log('e','识别失败：'+(j.error||'')); return ''; }
  return (j.text||'').trim();
}

async function ask(text){
  const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text,session:$('s').value,speak:true})});
  return r.json();
}

// 等音箱真的说完了再重新开听，避免把自己的声音录进去
async function waitSpoken(){
  const t0=performance.now();
  await new Promise(r=>setTimeout(r,500));               // 给云端下发留点时间
  while(running && performance.now()-t0<120000){
    try{
      const r=await fetch('/speaking'); const j=await r.json();
      if(!j.playing) break;
      setState('🔊 它在说…');
    }catch(e){ break; }
    await new Promise(r=>setTimeout(r,400));
  }
  await new Promise(r=>setTimeout(r,900));               // 尾巴多留一点，避免回声
}

async function loop(){
  while(running){
    setState('👂 在听…（直接说话就行）');
    const blob=await recordUtterance();
    if(!running) break;
    if(!blob){ continue; }
    setState('📝 识别中…');
    const text=await stt(blob);
    if(!text){ setState('没听清，再说一次'); continue; }
    log('q','你：'+text);
    setState('🤔 想…');
    let j;
    try{ j=await ask(text); }catch(e){ log('e','请求失败：'+e); continue; }
    if(!j.ok){ log('e','失败：'+(j.error||'')); continue; }
    if(j.mode==='research'){
      log('a','pi：'+j.answer+'（后台查询中，查完会念出来）');
      setState('🔎 后台在查…');
      await new Promise(r=>setTimeout(r,1500));
      continue;
    }
    log('a','pi：'+j.answer);
    await waitSpoken();
  }
}
</script>"""



CHAT_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>小爱音箱 × pi</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 14px;background:#111;color:#eee}
 textarea{width:100%;height:90px;font-size:16px;padding:10px;border-radius:8px;border:1px solid #444;background:#1b1b1b;color:#eee;box-sizing:border-box}
 button{padding:10px 18px;font-size:16px;border-radius:8px;border:0;background:#3b82f6;color:#fff;cursor:pointer;margin:8px 8px 0 0}
 button:disabled{background:#555}
 a{color:#7dd3fc}
 #log{margin-top:18px;white-space:pre-wrap;font-size:15px;line-height:1.6}
 .q{color:#7dd3fc}.a{color:#a7f3d0}.e{color:#fca5a5}.m{color:#fcd34d;font-size:13px}
 input#s{background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:6px;padding:6px}
</style>
<h2>小爱音箱 × pi</h2>
<div class="m">想用麦克风说话 → <a href="/mic">单句录音</a> ｜ <a href="/talk">连续对话（推荐）</a>（都需要 HTTPS）</div>
<p><label>会话 <input id=s value="xiaoai-speaker"></label>
<label style="margin-left:12px"><input type="checkbox" id=spk checked> 音箱播报</label></p>
<textarea id=t placeholder="输入文字，Ctrl+Enter 发送"></textarea>
<div><button id=b onclick="send()">发 送</button></div>
<div id=log></div>
<script>
const log=(c,t)=>{const d=document.createElement('div');d.className=c;d.textContent=t;document.getElementById('log').prepend(d)};
async function send(){
  const t=document.getElementById('t'), b=document.getElementById('b');
  const text=t.value.trim(); if(!text) return;
  b.disabled=true; log('q','你：'+text);
  try{
    const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,session:document.getElementById('s').value,speak:document.getElementById('spk').checked})});
    const j=await r.json();
    if(j.ok){log('a','pi：'+j.answer); if(j.tts!==j.answer) log('a','音箱念：'+j.tts);}
    else log('e','失败：'+(j.error||'无回答'));
    t.value='';
  }catch(e){log('e','请求失败：'+e)}
  b.disabled=false; t.focus();
}
document.getElementById('t').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))send()});
</script>"""

MIC_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>说话 → 小爱音箱</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:24px auto;padding:0 14px;background:#111;color:#eee}
 button{padding:14px 22px;font-size:17px;border-radius:10px;border:0;background:#ef4444;color:#fff;cursor:pointer}
 button.on{background:#22c55e}
 button:disabled{background:#555}
 #log{margin-top:18px;white-space:pre-wrap;font-size:15px;line-height:1.6}
 .q{color:#7dd3fc}.a{color:#a7f3d0}.e{color:#fca5a5}.m{color:#fcd34d;font-size:13px}
 a{color:#7dd3fc}
</style>
<h2>说话 → pi → 音箱回答</h2>
<div class="m">第一次会请求麦克风权限；说完自动停止并识别。</div>
<p><label>会话 <input id=s value="xiaoai-speaker" style="background:#1b1b1b;color:#eee;border:1px solid #444;border-radius:6px;padding:6px"></label></p>
<p><button id=b onclick="toggle()">🎤 开始说话</button></p>
<div id=log></div>
<p><a href="/">← 打字界面</a> · <a href="/talk">连续对话 →</a></p>
<script>
const log=(c,t)=>{const d=document.createElement('div');d.className=c;d.textContent=t;document.getElementById('log').prepend(d)};
let rec=null, chunks=[], on=false, timer=null;
async function toggle(){ on ? stop() : start(); }
async function start(){
  const b=document.getElementById('b');
  try{
    const st=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
    const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')?'audio/webm;codecs=opus':'audio/webm';
    rec=new MediaRecorder(st,{mimeType:mime}); chunks=[];
    rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
    rec.onstop=async()=>{ st.getTracks().forEach(t=>t.stop()); await handle(new Blob(chunks,{type:'audio/webm'})); };
    rec.start(); on=true; b.textContent='⏹ 说完点这里'; b.classList.add('on');
    timer=setTimeout(stop, 15000);   // 15 秒上限
    log('m','录音中…（点按钮或 15 秒后自动停）');
  }catch(e){ log('e','拿不到麦克风：'+e+'（需要 HTTPS 访问，且要授权）'); }
}
function stop(){ if(!on) return; on=false; clearTimeout(timer);
  const b=document.getElementById('b'); b.textContent='🎤 开始说话'; b.classList.remove('on');
  try{ rec.stop(); }catch(e){}
}
async function handle(blob){
  log('m','识别中…');
  let text='';
  try{
    const r=await fetch('/stt',{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:blob});
    const j=await r.json();
    if(!j.ok){ log('e','识别失败：'+(j.error||'未知')); return; }
    text=(j.text||'').trim();
    if(!text){ log('e','没听清，再说一次'); return; }
    log('q','你：'+text);
  }catch(e){ log('e','上传失败：'+e); return; }
  try{
    const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text,session:document.getElementById('s').value,speak:true})});
    const j=await r.json();
    if(j.ok) log('a','pi：'+j.answer); else log('e','失败：'+(j.error||'无回答'));
  }catch(e){ log('e','请求失败：'+e); }
}
</script>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                   'application/json; charset=utf-8')

    def _html(self, text: str):
        self._send(200, text.encode(), 'text/html; charset=utf-8')

    def _read(self) -> bytes:
        n = _to_int(self.headers.get('Content-Length') or 0, 0)
        return self.rfile.read(n) if n else b''

    def _json_body(self) -> dict:
        raw = self._read()
        if not raw:
            return {}
        try:
            d = json.loads(raw)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}

    def do_GET(self):  # noqa: N802 - stdlib 命名约定
        path = self.path.split('?', 1)[0]
        if path == '/status':
            st = pb.load_state()
            self._json(200, {
                'device': pb.DEVICE_ID,
                'hardware': pb.HARDWARE,
                'default_session': pb.SESSION_NAME,
                'last_session': st.get('session'),
                'seen_records': len(st.get('seen', [])),
                'ear': 'unavailable-on-this-firmware',
                'stt_model': WHISPER_MODEL,
                'stt_ready': _model is not None,
                'https_port': HTTPS_PORT,
                'jobs': pb.jobs_snapshot(5),
            })
            return
        if path == '/speaking':
            st = asyncio_run(pb.get_play_state())
            self._json(200, st if isinstance(st, dict) else {'playing': False})
            return
        if path == '/talk':
            self._html(TALK_PAGE)
            return
        if path == '/jobs':
            self._json(200, {'jobs': pb.jobs_snapshot(20)})
            return
        if path.startswith('/audio/'):
            name = os.path.basename(path[len('/audio/'):])
            f = pb.dlna.AUDIO_DIR / name
            if not name or not f.is_file():
                self._json(404, {'error': 'audio not found'})
                return
            self._send(200, f.read_bytes(), 'audio/mpeg')
            return
        if path == '/mic':
            self._html(MIC_PAGE)
            return
        if path in ('/', '/index.html'):
            self._html(CHAT_PAGE)
            return
        self._json(404, {'error': 'not found'})

    def do_POST(self):  # noqa: N802
        path = self.path.split('?', 1)[0]
        try:
            if path.startswith('/stt'):
                raw = self._read()
                if not raw:
                    self._json(400, {'ok': False, 'error': '没有音频数据'})
                    return
                try:
                    text = transcribe(raw)
                except Exception as e:  # noqa: BLE001 - 单次识别失败返回错误即可
                    pb.log(f'[stt] 识别失败: {e}')
                    self._json(500, {'ok': False, 'error': str(e)})
                    return
                if text:
                    if pb.looks_like_echo(text):
                        pb.log(f'[stt] 丢弃回声（和我们刚播报的内容重合）: "{text[:40]}"')
                        text = ''
                    elif difflib.SequenceMatcher(
                            None, re.sub(r'\s', '', text),
                            re.sub(r'\s', '', WHISPER_PROMPT)).ratio() > 0.6:
                        pb.log(f'[stt] 丢弃 Prompt 回声: "{text[:40]}"')
                        text = ''
                pb.log(f'[stt] {len(raw)} 字节 -> "{text[:60]}"')
                self._json(200, {'ok': True, 'text': text})
                return

            body = self._json_body()
            text = (body.get('text') or '').strip()
            if not text:
                self._json(400, {'ok': False, 'error': '缺少 text'})
                return
            if path.startswith('/say'):
                rc = pb_asyncio_say(text)
                self._json(200, {'ok': rc == 0, 'tts': text})
            elif path.startswith('/ask'):
                session = (body.get('session') or pb.SESSION_NAME).strip() or pb.SESSION_NAME
                r = pb_asyncio_ask(text, session, bool(body.get('speak', True)))
                self._json(200 if r['ok'] else 502, r)
            else:
                self._json(404, {'ok': False, 'error': 'not found'})
        except Exception as e:  # noqa: BLE001 - 请求级失败不该打死服务
            pb.log(f'[api] {path} 失败: {e}')
            self._json(500, {'ok': False, 'error': str(e)})

    def log_message(self, format, *args):  # noqa: A002 - 与基类签名保持一致
        pb.log(f'[api] {self.address_string()} {format % args}')


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)


def pb_asyncio_say(text: str):
    import asyncio
    return asyncio.run(pb.cmd_say(text))


def pb_asyncio_ask(text: str, session: str, speak: bool):
    import asyncio
    return asyncio.run(pb.cmd_ask(text, session, speak))


def serve_https():
    if not ensure_cert():
        return
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))
    srv = ThreadingHTTPServer((HOST, HTTPS_PORT), Handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    pb.log(f'HTTPS 监听 https://{lan_ip()}:{HTTPS_PORT}/ （自签证书，浏览器需点「继续访问」）')
    srv.serve_forever()


def main() -> int:
    ip = lan_ip()
    pb.log(f'HTTP 接口启动 http://{ip}:{PORT}/  device={pb.DEVICE_ID}')
    if HTTPS_PORT:
        threading.Thread(target=serve_https, daemon=True).start()
    threading.Thread(target=get_model, daemon=True).start()   # 预热语音模型
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pb.log('HTTP 接口收到中断')
    return 0


if __name__ == '__main__':
    sys.exit(main())
