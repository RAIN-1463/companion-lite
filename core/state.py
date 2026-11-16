# -*- coding: utf-8 -*-
"""Companion Lite · 状态机（纯本地规则，不花钱）

给她一份"我此刻是什么状态"，而不是每次从零开始。
只读数据库 + 系统时间，不做任何 API 调用。
"""
from datetime import datetime, timedelta

NEG = ["累", "烦", "难受", "崩", "废物", "不想", "难过", "压力", "哭", "烦死",
       "失眠", "焦虑", "撑不住", "没意思", "算了"]
POS = ["喜欢", "开心", "好耶", "么么", "爱", "哈哈", "不错", "舒服", "谢谢"]


def _minutes_between(a, b):
    return max(0, int((b - a).total_seconds() // 60))


def human_gap(minutes):
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 个多小时前"
    days = hours // 24
    return f"{days} 天前" if days > 1 else "一天多前"


def compute_state(store, now=None):
    """返回 dict：mood / energy / idle_minutes / last_seen / streak_days / notes"""
    now = now or datetime.now()
    row = store.con.execute(
        "SELECT ts, speaker, text FROM messages WHERE speaker='user' ORDER BY ts DESC LIMIT 8"
    ).fetchall()

    last_ts = None
    if row:
        try:
            last_ts = datetime.strptime(row[0][0], "%Y-%m-%d %H:%M:%S")
        except Exception:
            last_ts = None

    idle = _minutes_between(last_ts, now) if last_ts else 24 * 60

    # 能量：聊得越久越久没说话，越低（下限 40，绝不掉到"冷淡"）
    energy = 100 - min(60, idle / 60.0 * 4)
    energy = int(max(40, min(100, energy)))

    # 心情：看最近几条用户消息的用词
    text = " ".join([r[2] for r in row])
    neg = sum(text.count(w) for w in NEG)
    pos = sum(text.count(w) for w in POS)

    if neg > pos * 2:
        mood = "低落"
    elif neg > pos:
        mood = "闷"
    elif pos > neg:
        mood = "轻快"
    else:
        mood = "平静"

    # 连续聊了几天
    days = [r[0][:10] for r in
            store.con.execute("SELECT ts FROM messages ORDER BY ts DESC LIMIT 500").fetchall()]
    streak, seen = 0, set()
    d = now.date()
    day_set = set(days)
    while str(d) in day_set:
        streak += 1
        d = d - timedelta(days=1)

    return {
        "mood": mood,
        "energy": energy,
        "idle_minutes": idle,
        "last_seen": human_gap(idle) if last_ts else "还没有过",
        "streak_days": streak,
        "notes": f"最近 {len(row)} 条里负面词 {neg} 次、正面词 {pos} 次" if row else "",
    }


def format_state_block(state, diary_rows=None):
    """拼成注入用的文本块（她自己的状态，不是规则）"""
    from datetime import datetime as _dt
    _now = _dt.now()
    _wd = "一二三四五六日"[_now.weekday()]
    lines = ["[你现在的状态]"]
    lines.append(f"现在是 {_now.strftime('%Y-%m-%d')} 周{_wd} {_now.strftime('%H:%M')}")
    lines.append(f"心情：{state['mood']}　能量：{state['energy']}")
    lines.append(f"上次和他说话：{state['last_seen']}")
    if state.get("streak_days"):
        lines.append(f"已经连着聊了 {state['streak_days']} 天")
    if diary_rows:
        for day, text in diary_rows:
            t = text.strip().replace("\n", " ")
            if len(t) > 220:
                t = t[:220] + "…"
            lines.append("")
            lines.append(f"[你的日记 · {day}]")
            lines.append(t)
    return "\n".join(lines)
