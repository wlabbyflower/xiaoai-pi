# xiaoai-pi

把小爱音箱接到 [pi](https://github.com/badlogic/pi-mono)（或任何命令行 LLM agent）上：
**打字/说话 → agent 思考（带长期记忆、能联网、能跑本机命令）→ 音箱念出来**。

不刷机、不拆机、不改固件。音箱只要有网就行。

```
          ┌─────────────┐   HTTP    ┌──────────────┐   subprocess   ┌─────────┐
 浏览器/手机 │  /talk      │ ────────▶ │  pi_api.py   │ ─────────────▶ │   pi    │
  (麦克风)   │  /ask /stt  │           │  (HTTP 服务)  │                │ (agent) │
          └─────────────┘           └──────┬───────┘                └────┬────┘
                                            │                             │
                                   长期记忆 memory.db              session 上下文
                                            │                             │
                                     ┌──────▼───────┐                     │
                                     │ pi_bridge.py │◀────────────────────┘
                                     │  (语音循环)   │
                                     └──────┬───────┘
                                            │ ① 本地 ffmpeg 解码 + faster-whisper 识别
                                            │ ② edge-tts 合成 mp3
                                            ▼
                                     ┌─────────────┐
                                     │  小爱音箱    │  DLNA/UPnP 播放（不经过小米云）
                                     └─────────────┘
```

## 特性

- **两种输入**：网页打字 / 手机浏览器当麦克风说话（不需要买外设）
- **本地语音识别**：faster-whisper，2 秒左右一句，支持热词纠正专有名词
- **本地合成 + DLNA 播报**：不依赖小米云 TTS，1~2 秒出声；失败自动回落云端 TTS
- **长期记忆**：SQLite 存事实，自动提取 + 显式 `记住：…`，跨会话生效
- **能联网、能查本机**：默认带 `bash / read / web_search`，涉及实时信息会真去查
- **异步研究任务**：说「查一下…」立刻回一句"我去查"，查完主动播报
- **防重复 / 防回声**：按播放位置精确停播（小爱默认单曲循环），并丢弃自己声音被录回去的识别结果
- **连续对话**：同一 session 保持上下文，空闲超时自动开新会话

## 前置要求

- Linux + Python 3.10+
- `ffmpeg` / `ffprobe` / `openssl`（`apt install ffmpeg openssl`）
- 一个能跑起来的 [pi](https://github.com/badlogic/pi-mono)（`pi -p "hi"` 能出声）
- 一台已经绑定到小米账号、联网的小爱音箱
- 音箱和你这台机器**网络互通**（同一网段或路由器可路由）

## 快速开始

```bash
git clone <this-repo> xiaoai-pi && cd xiaoai-pi
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1) 登录小米账号（会提示输入短信验证码）
cp config.example.json config.json && chmod 600 config.json
$EDITOR config.json          # 填 mi_user / mi_pass
python mi_login.py           # 成功后 token 缓存到 ~/.mi_token.json

# 2) 自动发现音箱（deviceID / did / 硬件型号 / 局域网 IP）
python discover.py
#   乐器在别的网段时： python discover.py --scan 192.168.1
#   或直接指定：      python discover.py --dlna 192.168.1.23

# 3) 验证
python pi_bridge.py --say "链路通了"
```

### 起服务

```bash
# 网页 + 语音接口（默认只监听 127.0.0.1:8447）
python pi_api.py

# 语音轮询循环（保留：等小米云端有对话记录时可用）
python pi_bridge.py
```

想用手机当麦克风，需要 HTTPS（浏览器只允许安全上下文调麦克风）：

```json
{ "http_host": "0.0.0.0", "https_port": 8448 }
```

首次访问会看到自签证书警告，点「继续前往」即可。

- 打字界面：`http://<本机IP>:8447/`
- 连续对话：`https://<本机IP>:8448/talk`（点一次「开始对话」，之后全自动）
  - 页面默认要求**唤醒词**（「小P」），唤醒后 2 分钟内免唤醒；可在页面上关闭
  - 识别结果含唤醒词变体（小派/小屁/小批…）也能命中，容忍 ASR 误差
- 单句录音：`https://<本机IP>:8448/mic`

### systemd user 服务

```bash
mkdir -p ~/.config/systemd/user
cp systemd/user/xiaoai-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now xiaoai-api xiaoai-bridge
loginctl enable-linger "$USER"      # 开机自启（不登录也跑）
```

## 配置项

`config.json`（已被 `.gitignore` 忽略）或同名环境变量，环境变量优先：

| 键 | 环境变量 | 说明 |
|---|---|---|
| `mi_user` / `mi_pass` | `MI_USER` / `MI_PASS` | 小米账号，只在没有缓存 token 时用 |
| `device_id` | `MI_DEVICE_ID` | miNA deviceID（UUID），`discover.py` 自动填 |
| `device_did` | `MI_DID` | MIoT did（数字），`discover.py` 自动填 |
| `hardware` | `MI_HARDWARE` | 设备型号，如 `S12A` |
| `dlna_ip` / `dlna_port` | `BRIDGE_DLNA_IP` / `BRIDGE_DLNA_PORT` | 音箱局域网地址 |
| `auth_cache` | `MI_TOKEN_FILE` | token 缓存路径 |
| `pi_bin` / `pi_cwd` | `PI_BIN` / `BRIDGE_PI_CWD` | pi 可执行文件、工作目录 |
| `http_host` / `http_port` / `https_port` | `PI_API_HOST` / `PI_API_PORT` / `PI_HTTPS_PORT` | HTTP 服务 |
| `whisper_model` / `whisper_lang` / `whisper_prompt` | `PI_WHISPER_*` | 识别模型、语言、热词 |
| `tts_voice` | `BRIDGE_TTS_VOICE` | edge-tts 音色 |
| `speak_mode` | `BRIDGE_SPEAK_MODE` | `auto` / `dlna` / `cloud` |
| `memory_auto` | `BRIDGE_MEMORY_AUTO` | 每轮自动抽取长期记忆 |
| `session_ttl` | `BRIDGE_SESSION_TTL` | 空闲多久开新会话（秒） |
| `peer_turns` | `BRIDGE_PEER_TURNS` | 注入「搭档 PI 会话」最近几轮，0 关闭 |

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 打字对话界面 |
| GET | `/talk` | 连续对话（麦克风，需 HTTPS） |
| GET | `/mic` | 单句录音 |
| GET | `/status` | 状态、后台任务 |
| GET | `/speaking` | 是否正在播报 |
| GET | `/jobs` | 后台研究任务列表 |
| GET | `/audio/<file>` | 供音箱拉取的合成音频 |
| POST | `/ask` | `{"text":"…","session":"…","speak":true}` |
| POST | `/say` | `{"text":"…"}` 只播报 |
| POST | `/stt` | body = 音频字节，返回识别文本 |

## 命令行

```bash
bin/xiaoai say "让它念这句话"
bin/xiaoai ask "问一个问题"                    # 需要实时信息会自己去查
bin/xiaoai ask "继续追问" --session work       # 同 session = 连续对话
bin/xiaoai status
bin/xiaoai stop                                # 打断播报
```

## 已知限制

- **不能靠喊「小爱同学」直接唤醒本 agent**：音箱的麦克风数据只在本机和小米云，云端对话记录接口对部分固件返回空、本地也没有 shell。想用它自己的耳朵，只有两条路：小米「小爱技能开放平台」（官方，需注册+审核+公网 HTTPS）或 root 音箱（需拆机）。本项目的耳朵是自己实现的（手机/电脑麦克风）。
- **音箱默认单曲循环**：已用「监听播放位置 + 到末尾/回跳即 Stop」处理；如果你的型号不返回 `GetPositionInfo`，会退回按音频时长定时停。
- **回声**：手机喇叭和音箱在同一房间时，会把自己的播报录回去。已按文本相似度过滤最近 5 条播报内容。
- **DLNA IP 是 DHCP**：建议在路由器上给音箱固定 IP，否则掉线后需要重跑 `discover.py`。
- 首次使用 faster-whisper 会下载模型（`small` 约 500MB）。

## 安全提醒

- `config.json`、`~/.mi_token.json`、`memory.db`、`bridge.log`、`audio/`、`certs/` 都在 `.gitignore` 里，**不要提交**。
- token 相当于小米账号的长期凭据，泄露等于账号被接管；建议单独开一个小号或定期改密码。
- HTTP 服务默认只监听 `127.0.0.1`。改成 `0.0.0.0` 时请确认所在网络可信，并注意它没有鉴权。

## 目录

```
config.py       配置加载（env > config.json > 默认值）
discover.py     发现音箱：deviceID / did / 型号 / DLNA IP
mi_login.py     小米账号登录（支持无 TTY 的 OTP 验证码流程）
pi_bridge.py    agent 调用、长期记忆注入、播报、后台任务
pi_api.py       HTTP/HTTPS 服务与页面
dlna.py         音箱 DLNA 播放控制（含精确停播）
memory.py       长期记忆（SQLite + 2-gram 检索 + 自动提取）
bin/xiaoai      命令行入口
systemd/user/   开机自启模板
```

## License

MIT
