# -*- coding: utf-8 -*-
"""Companion Lite · 主动消息

她不等你开口，自己在合适的时候说一句。
规则全部本地，只有真要说话时才调用一次模型。
"""
import random
from datetime import datetime, timedelta

# 什么时候可以主动开口（本地时间）
WINDOWS = [(8, 5, 8, 55), (12, 5, 12, 55), (17, 30, 18, 40), (21, 20, 22, 40)]

MIN_IDLE_MINUTES = 60      # 你刚说过话，她就不插嘴
MIN_GAP_MINUTES = 90       # 两次主动之间至少隔这么久
TICK_CHANCE = 0.4          # 每个时间窗里每次检查的概率

PROMPT = """{state}

现在是 {now}。{user}已经 {idle} 没跟你说话了。

请主动说一句（1~2 句，不超过 40 字）：
- 顺着最近聊的事、或者你记着的他的状态说
- 不要问"在吗"，不要报时间，不要解释你为什么开口
- 不要复述设定，不要像客服
- 直接给他这句话的正文

最近的对话：
{recent}
"""


def _in_window(now):
    for h1, m1, h2, m2 in WINDOWS:
        start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
        end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if start <= now <= end:
            return True
    return False


def should_speak(store, cfg, now=None):
    """返回 (要不要说, 原因)"""
    now = now or datetime.now()
    if not cfg.get("proactive_enabled", False):
        return False, "未开启"
    if not cfg.get("api_key"):
        return False, "没有 key"

    # 最后一次说话时间
    row = store.con.execute(
        "SELECT ts FROM messages ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        try:
            last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            gap = (now - last).total_seconds() / 60
            if gap < MIN_IDLE_MINUTES:
                return False, f"刚聊过（{int(gap)} 分钟前）"
        except Exception:
            pass

    # 上一次主动
    last_pro = store.get_state("last_proactive")
    if last_pro:
        try:
            lp = datetime.strptime(last_pro, "%Y-%m-%d %H:%M:%S")
            if (now - lp).total_seconds() / 60 < MIN_GAP_MINUTES:
                return False, "刚主动过"
        except Exception:
            pass

    if not _in_window(now):
        return False, "不在时间窗"

    # 窗口最后一次检查时必发，其余按概率
    force = now.hour == WINDOWS[-1][2] and now.minute >= 40
    if not force and random.random() > TICK_CHANCE:
        return False, "这次没抽中"

    return True, "ok"


def build_prompt(store, cfg, state_block):
    now = datetime.now()
    row = store.con.execute(
        "SELECT ts FROM messages WHERE speaker='user' ORDER BY ts DESC LIMIT 1").fetchone()
    idle = "一阵子"
    if row:
        try:
            last = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
            mins = int((now - last).total_seconds() / 60)
            idle = f"{mins // 60} 个多小时" if mins >= 60 else f"{mins} 分钟"
        except Exception:
            pass

    recent = store.con.execute(
        "SELECT speaker, text FROM messages ORDER BY ts DESC LIMIT 8").fetchall()
    recent = list(reversed(recent))
    lines = []
    for sp, tx in recent:
        who = "他" if sp == "user" else "我"
        one = " ".join(tx.split())
        lines.append(f"{who}：{one[:80]}")

    return PROMPT.format(
        state=state_block,
        now=f"{now:%H:%M}",
        user=cfg.get("user_name", "他"),
        idle=idle,
        recent="\n".join(lines),
    )


def speak(store, cfg, stream_chat_fn, state_block):
    """生成一条主动消息并写入聊天，返回文本或 None"""
    prompt = build_prompt(store, cfg, state_block)
    buf = []
    try:
        for piece in stream_chat_fn(
            [{"role": "user", "content": prompt}],
            base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
            temperature=1.0, max_tokens=200, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        print("[proactive] 调用失败:", e)
        return None

    text = "".join(buf).strip()
    if not text or len(text) > 300:
        return None
    store.append("char", text)
    store.set_state("last_proactive", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return text
