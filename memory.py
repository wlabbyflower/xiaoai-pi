#!/usr/bin/env python3
"""长期记忆：给语音助理用的持久化事实库（SQLite）

设计要点：
  - 只存"事实"，不存聊天流水；每条一行，去重
  - 检索用字符 2-gram 重叠打分 + 时间衰减，中文不需要分词器
  - 显式指令（记住/忘掉/记忆）优先，自动提取作为补充
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / 'memory.db'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    subject TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL UNIQUE,
    source  TEXT NOT NULL DEFAULT 'user',
    hits    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject);
"""


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.executescript(_SCHEMA)
    return con


def add(content: str, subject: str = 'general', source: str = 'user') -> bool:
    """存一条事实；已存在则刷新时间。返回是否新增。"""
    content = content.strip()
    if not content:
        return False
    with _conn() as con:
        cur = con.execute('SELECT id FROM memories WHERE content = ?', (content,))
        row = cur.fetchone()
        if row:
            con.execute('UPDATE memories SET ts = ? WHERE id = ?', (time.time(), row[0]))
            return False
        con.execute(
            'INSERT INTO memories (ts, subject, content, source) VALUES (?,?,?,?)',
            (time.time(), subject, content, source),
        )
        return True


def delete(pattern: str) -> int:
    """按子串删除，返回删除条数。"""
    pattern = pattern.strip()
    if not pattern:
        return 0
    with _conn() as con:
        cur = con.execute('DELETE FROM memories WHERE content LIKE ?', (f'%{pattern}%',))
        return cur.rowcount


def all_items(limit: int = 50) -> list:
    with _conn() as con:
        cur = con.execute(
            'SELECT id, ts, subject, content FROM memories ORDER BY ts DESC LIMIT ?', (limit,))
        return [{'id': r[0], 'ts': r[1], 'subject': r[2], 'content': r[3]} for r in cur]


def _bigrams(text: str) -> set:
    t = re.sub(r'[\s\W_]+', '', text.lower())
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def search(query: str, limit: int = 8) -> list:
    """2-gram 重叠打分 + 时间衰减；没匹配就回退到最近的事实。"""
    qgrams = _bigrams(query)
    now = time.time()
    scored = []
    with _conn() as con:
        for mid, ts, subject, content in con.execute(
                'SELECT id, ts, subject, content FROM memories'):
            grams = _bigrams(content)
            overlap = len(qgrams & grams)
            if qgrams and not overlap:
                continue
            age_days = max(0.0, (now - ts) / 86400.0)
            score = overlap * 10.0 + 1.0 / (1.0 + age_days / 30.0)
            scored.append((score, mid, ts, subject, content))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [{'id': s[1], 'ts': s[2], 'subject': s[3], 'content': s[4]}
            for s in scored[:limit]]


def remember_hits(ids) -> None:
    if not ids:
        return
    with _conn() as con:
        con.executemany('UPDATE memories SET hits = hits + 1 WHERE id = ?',
                        [(i,) for i in ids])


def build_prompt_block(query: str, limit: int = 8) -> str:
    """把相关记忆拼成注入系统提示的一段文本。"""
    items = search(query, limit)
    if not items:
        return ''
    remember_hits([i['id'] for i in items])
    lines = '\n'.join(f'- {i["content"]}' for i in items)
    return ('以下是你长期记忆中与本次对话可能相关的事实（来自过去和用户的交流，'
            '可直接使用；与用户当前说法冲突时以当前说法为准）：\n' + lines)


# ---- 自动提取：从一轮问答里抽取值得长期记住的事实 ----

EXTRACT_PROMPT = """从下面这一轮对话中，提取"值得长期记住的用户事实"：
偏好、身份、习惯、设备、项目、常问的事、明确的约定。
不要提取寒暄、一次性问题、模型的推测。

只输出 JSON 数组，元素是字符串，每条不超过 40 字。没有可记的就输出 []。
示例：["用户叫老王","用户在开发懒猫微服","用户偏好结论先行"]

用户说：{q}
助手答：{a}
"""


def parse_extracted(raw: str) -> list:
    """从模型输出里捞出 JSON 数组，容错处理前后废话和代码块。"""
    if not raw:
        return []
    m = re.search(r'\[.*?\]', raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for x in data:
        if isinstance(x, str) and 2 <= len(x.strip()) <= 80:
            out.append(x.strip())
    return out[:5]


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        for i in all_items(100):
            print(f'#{i["id"]:3d} [{i["subject"]}] {i["content"]}')
    elif len(sys.argv) > 2 and sys.argv[1] == 'add':
        print('新增' if add(' '.join(sys.argv[2:])) else '已存在')
    elif len(sys.argv) > 2 and sys.argv[1] == 'search':
        for i in search(' '.join(sys.argv[2:])):
            print(f'#{i["id"]:3d} {i["content"]}')
    else:
        print('用法: memory.py list | memory.py add <文本> | memory.py search <查询>')
