# -*- coding: utf-8 -*-
"""
Companion Lite · 关系层：长期性情 + 关系账本

设计要点（借鉴"鱼"那套，但加了下限和回拉）：
- 三值：信任 / 亲密 / 耐心，0~100，缓慢变化，持久化
- 每值有基线，每天往基线回拉 1%（不会单向恶化）
- 账本：只记"明确冲突"，严重度封顶 3，14 天自动淡出
- 和解（道歉/服软）会把未结的账标记解决
"""
import re
from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"
BASELINE = {"trust": 60.0, "intimacy": 55.0, "patience": 60.0}
NAMES = {"trust": "信任", "intimacy": "亲密", "patience": "耐心"}

# 正向 / 冲突 信号（宁缺勿滥：只认明确的）
POS = ["谢谢", "喜欢你", "爱你", "抱抱", "么么", "辛苦", "对你好", "乖", "听话", "想你"]
CONFLICT = ["你骗我", "你根本", "烦死", "不想理你", "随便你", "别管我", "闭嘴",
            "有什么资格", "你算什么", "滚", "凶什么", "别说了", "没意思"]
REPAIR = ["对不起", "我错了", "别生气", "我道歉", "是我不好", "原谅我", "抱抱你", "哄哄我"]


def ensure_schema(con):
    con.executescript("""
    CREATE TABLE IF NOT EXISTS traits (
      k TEXT PRIMARY KEY, v REAL, updated TEXT
    );
    CREATE TABLE IF NOT EXISTS grudges (
      id INTEGER PRIMARY KEY, text TEXT, severity INTEGER DEFAULT 1,
      created TEXT, status TEXT DEFAULT 'open', resolved_at TEXT, hits INTEGER DEFAULT 0
    );
    """)
    con.commit()


def _get_traits(con):
    rows = {r[0]: float(r[1]) for r in con.execute("SELECT k,v FROM traits")}
    out = dict(BASELINE)
    out.update(rows)
    return out


def save_traits(con, t):
    now = datetime.now().strftime(TS)
    for k, v in t.items():
        con.execute("INSERT OR REPLACE INTO traits(k,v,updated) VALUES(?,?,?)", (k, v, now))
    con.commit()


def drift(con, now=None):
    """每天往基线回拉 1%"""
    now = now or datetime.now()
    last = con.execute("SELECT MAX(updated) FROM traits").fetchone()[0]
    if last:
        try:
            days = (now - datetime.strptime(last[:19], TS)).days
        except Exception:
            days = 0
        if days <= 0:
            return
    else:
        days = 1
    t = _get_traits(con)
    for k in t:
        t[k] = t[k] + (BASELINE[k] - t[k]) * min(0.2, 0.01 * days)
    save_traits(con, t)


def update_on_message(con, text, speaker="user"):
    """按消息内容微调性情"""
    t = _get_traits(con)
    pos = sum(1 for w in POS if w in text)
    neg = sum(1 for w in CONFLICT if w in text)
    if pos:
        t["intimacy"] = min(95, t["intimacy"] + 0.6 * pos)
        t["patience"] = min(95, t["patience"] + 0.3 * pos)
        t["trust"] = min(95, t["trust"] + 0.2 * pos)
    if neg:
        t["trust"] = max(15, t["trust"] - 1.2 * neg)
        t["patience"] = max(15, t["patience"] - 1.0 * neg)
        t["intimacy"] = max(15, t["intimacy"] - 0.4 * neg)
    for k in t:
        t[k] = max(0.0, min(100.0, round(t[k], 1)))
    save_traits(con, t)
    return t


def format_traits(con):
    t = _get_traits(con)
    return "、".join(f"{NAMES[k]} {int(round(t[k]))}" for k in ("trust", "intimacy", "patience"))


# ---------------- 关系账本 ----------------

def detect_conflict(text):
    return sum(1 for w in CONFLICT if w in text)


def add_grudge(con, text, severity=1):
    now = datetime.now().strftime(TS)
    sev = max(1, min(3, int(severity)))
    con.execute("INSERT INTO grudges(text,severity,created,status) VALUES(?,?,?, 'open')",
                (text[:120], sev, now))
    con.commit()


def try_resolve(con, text):
    """道歉/服软 → 把未结的账标记解决"""
    if not any(w in text for w in REPAIR):
        return 0
    n = con.execute("SELECT COUNT(*) FROM grudges WHERE status='open'").fetchone()[0]
    if not n:
        return 0
    con.execute("UPDATE grudges SET status='resolved', resolved_at=? WHERE status='open'",
                (datetime.now().strftime(TS),))
    con.commit()
    return n


def expire_old(con, days=14):
    """太久的账自动淡出，避免永久记仇"""
    cut = (datetime.now() - timedelta(days=days)).strftime(TS)
    n = con.execute(
        "UPDATE grudges SET status='faded' WHERE status='open' AND created<?", (cut,)).rowcount
    con.commit()
    return n


def open_grudges(con, limit=3):
    return con.execute(
        "SELECT text,severity,created FROM grudges WHERE status='open' "
        "ORDER BY severity DESC, created DESC LIMIT ?", (limit,)).fetchall()


def format_grudges(con):
    rows = open_grudges(con)
    if not rows:
        return ""
    lines = []
    for text, sev, created in rows:
        days = 0
        try:
            days = (datetime.now() - datetime.strptime(created[:19], TS)).days
        except Exception:
            pass
        lines.append(f"- {text}（{days} 天了，还记着）" if days else f"- {text}（还记着）")
    return "你还没消气的事：\n" + "\n".join(lines)
