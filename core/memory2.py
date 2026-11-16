# -*- coding: utf-8 -*-
"""
Companion Lite · 记忆增强层 v2
包含：摘要 / 结构化事实 / 遗忘曲线 / 字符向量检索 / 时间轴

设计原则：
- 全部本地计算，只有"摘要"和"抽取事实"两处调用模型
- 任何一步失败都静默降级，绝不影响聊天
"""
import json
import math
import re
from datetime import datetime, timedelta

TS = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS summaries (
  id INTEGER PRIMARY KEY,
  chat_id TEXT,
  upto TEXT,
  text TEXT,
  created TEXT
);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY,
  chat_id TEXT,
  text TEXT UNIQUE,
  kind TEXT DEFAULT 'fact',
  importance INTEGER DEFAULT 5,
  strength REAL DEFAULT 20,
  last_hit TEXT,
  hits INTEGER DEFAULT 0,
  status TEXT DEFAULT 'active',
  created TEXT
);
"""


def ensure_schema(con):
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(summaries)")]
    if "level" not in cols:
        try:
            con.execute("ALTER TABLE summaries ADD COLUMN level TEXT DEFAULT 'day'")
        except Exception:
            pass
    con.commit()
    cols = [r[1] for r in con.execute("PRAGMA table_info(timeline)")]
    if "kind" not in cols:
        try:
            con.execute("ALTER TABLE timeline ADD COLUMN kind TEXT")
        except Exception:
            pass
    con.commit()


# ==================== 摘要 ====================
SUM_PROMPT = """下面是一段你和{user}的对话。请压成一段摘要（180 字以内）：

要求：
1. 用第三人称陈述事实和状态，不要抒情
2. 保留：他最近的处境、情绪变化、约定、还没做完的事
3. 不要逐条罗列，不要复述原话
4. 禁止用"今天/昨天/明天/后天/待明日"这类相对时间，一律换成具体日期（如"11 月 5 日"）
5. 每行开头有 [MM-DD HH:MM]，那是那条消息发生的时间；写事件时把日期带出来
   （例："11 月 5 日下午他翻了三小节书"）

对话：
{text}
"""


def need_summary(con, chat_id, every=40):
    """返回 (是否需要, 起始 ts)"""
    last = con.execute(
        "SELECT upto FROM summaries WHERE chat_id=? ORDER BY id DESC LIMIT 1", (chat_id,)
    ).fetchone()
    since = last[0] if last else "1970-01-01"
    n = con.execute(
        "SELECT COUNT(*) FROM messages WHERE chat_id=? AND ts>?", (chat_id, since)
    ).fetchone()[0]
    return n >= every, since


def make_summary(store, cfg, stream_chat_fn, chat_id=None, force=False, level="day"):
    con = store.con
    cid = chat_id or store.current_chat() or "main"
    ok, since = need_summary(con, cid)
    if not ok and not force:
        return False, "还不够多"

    rows = con.execute(
        "SELECT ts,speaker,text FROM messages WHERE chat_id=? AND ts>? ORDER BY ts LIMIT 120",
        (cid, since)).fetchall()
    if len(rows) < 8:
        return False, "对话太少"

    lines = []
    for ts, sp, tx in rows:
        who = "他" if sp == "user" else "我"
        one = " ".join(tx.split())[:100]
        stamp = str(ts)[5:16]      # MM-DD HH:MM
        lines.append(f"[{stamp}] {who}：{one}")

    prompt = SUM_PROMPT.format(user=cfg.get("user_name", "他"), text="\n".join(lines))
    buf = []
    try:
        for piece in stream_chat_fn([{"role": "user", "content": prompt}],
                                    base_url=cfg["base_url"], api_key=cfg["api_key"],
                                    model=cfg["model"], temperature=0.5,
                                    max_tokens=400, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return False, f"调用失败: {e}"

    text = " ".join("".join(buf).split())
    if len(text) < 10:
        return False, "输出太短"
    text = text[:600]
    upto = rows[-1][0]
    con.execute("INSERT INTO summaries(chat_id,upto,text,created,level) VALUES(?,?,?,?,?)",
                (cid, upto, text, datetime.now().strftime(TS), level))
    con.commit()
    return True, text


WEEK_PROMPT = """下面是最近几天的几段摘要。请再压成一段**周记**（200 字以内）：

要求：
1. 用第三人称陈述（"这一周他…"），保留：他的状态变化、说定的事、还没解决的事
2. 不要逐条罗列，写"这一周整体怎么样"
3. 只输出正文

摘要：
{text}
"""


def make_weekly(store, cfg, stream_chat_fn, chat_id=None):
    """把最近 3 篇日摘要压成一篇周摘要"""
    con = store.con
    cid = chat_id or store.current_chat() or "main"
    rows = con.execute(
        "SELECT upto,text FROM summaries WHERE chat_id=? AND (level IS NULL OR level='day') "
        "ORDER BY id DESC LIMIT 3", (cid,)).fetchall()
    if len(rows) < 3:
        return False, "日摘要不够 3 篇"
    body = "\n\n".join(f"[{u[:10]}] {t}" for u, t in reversed(rows))
    prompt = WEEK_PROMPT.format(text=body)
    buf = []
    try:
        for piece in stream_chat_fn([{"role": "user", "content": prompt}],
                                    base_url=cfg["base_url"], api_key=cfg["api_key"],
                                    model=cfg["model"], temperature=0.5,
                                    max_tokens=500, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return False, f"调用失败: {e}"
    text = " ".join("".join(buf).split())[:700]
    if len(text) < 20:
        return False, "输出太短"
    upto = rows[0][0]
    con.execute("INSERT INTO summaries(chat_id,upto,text,created,level) VALUES(?,?,?,?,'week')",
                (cid, upto, text, datetime.now().strftime(TS)))
    con.commit()
    return True, text


def recent_summaries(con, chat_id, n=2, level=None):
    if level:
        return con.execute(
            "SELECT upto,text FROM summaries WHERE chat_id=? AND level=? ORDER BY id DESC LIMIT ?",
            (chat_id, level, n)).fetchall()
    return con.execute(
        "SELECT upto,text FROM summaries WHERE chat_id=? AND level='day' ORDER BY id DESC LIMIT ?",
        (chat_id, n)).fetchall()


def latest_week(con, chat_id):
    r = con.execute(
        "SELECT upto,text FROM summaries WHERE chat_id=? AND level='week' ORDER BY id DESC LIMIT 1",
        (chat_id,)).fetchone()
    return r


# ==================== 结构化事实 ====================
FACT_PROMPT = """从下面这段对话里，抽出**稳定的、以后还用得上**的事实（不是今天吃了什么这种一次性的）。

输出 JSON 数组，每条：
{{"text":"一句话陈述事实（20~50字）","kind":"fact|pref|rule","importance":1-10}}

规则：
- fact：关于他的稳定信息（身份、作息、身体状况、在意的事、关系状态）
- pref：他的偏好（喜欢/不喜欢被怎么对待）
- rule：说定的约定（几点睡、怎么劝、什么不接）
- 不要写具体日期、不要写对话原话、不要写一次性的情绪
- **不要从一次拒绝、一次情绪、一次争执里推出永久态度**——
   一次说“不给”只是那一刻，不是规则；只写反复出现、两人都沉底的东西
- 主体必须写清：是“他”的事还是“她”的事，不要混
- **以下属于"设定"，不要抽成事实**：她的名字/来历/外形/有没有实体/关系阶段/身体边界。
  这些由人设文件决定，不由对话决定；抽出来会和人设打架。
- 最多 6 条；没有就给 []

对话：
{text}
"""


def extract_facts(store, cfg, stream_chat_fn, chat_id=None, force=False):
    con = store.con
    cid = chat_id or store.current_chat() or "main"
    last = con.execute("SELECT created FROM facts ORDER BY id DESC LIMIT 1").fetchone()
    since = last[0] if last else "1970-01-01"
    rows = con.execute(
        "SELECT ts,speaker,text FROM messages WHERE chat_id=? AND ts>? ORDER BY ts LIMIT 200",
        (cid, since)).fetchall()
    if len(rows) < 10 and not force:
        return False, "对话太少"

    lines = []
    for ts, sp, tx in rows:
        who = "他" if sp == "user" else "我"
        lines.append(f"{who}：{' '.join(tx.split())[:90]}")

    prompt = FACT_PROMPT.format(text="\n".join(lines))
    buf = []
    try:
        for piece in stream_chat_fn([{"role": "user", "content": prompt}],
                                    base_url=cfg["base_url"], api_key=cfg["api_key"],
                                    model=cfg["model"], temperature=0.4,
                                    max_tokens=600, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return False, f"调用失败: {e}"

    raw = "".join(buf).strip()
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return False, "没解析出 JSON"
    try:
        items = json.loads(m.group(0))
    except Exception:
        return False, "JSON 不合法"

    added = 0
    now = datetime.now().strftime(TS)
    for it in items[:6]:
        text = " ".join(str(it.get("text", "")).split())[:120]
        if len(text) < 8:
            continue
        kind = str(it.get("kind", "fact"))
        imp = max(1, min(10, int(it.get("importance", 5) or 5)))
        try:
            con.execute(
                "INSERT OR IGNORE INTO facts(chat_id,text,kind,importance,strength,last_hit,hits,created) "
                "VALUES(?,?,?,?,?,?,0,?)", (cid, text, kind, imp, 20.0, now, now))
            added += 1
        except Exception:
            pass
    con.commit()
    return True, f"新增 {added} 条"


# ==================== 遗忘曲线 ====================
def decay_score(fact_row, now=None):
    """R = e^(-t/S)，S = strength × importance/5"""
    now = now or datetime.now()
    last = fact_row["last_hit"] or fact_row["created"] or now.strftime(TS)
    try:
        t = (now - datetime.strptime(last[:19], TS)).total_seconds() / 86400
    except Exception:
        t = 0
    s = max(1.0, float(fact_row["strength"] or 20) * (int(fact_row["importance"] or 5) / 5.0))
    return math.exp(-t / s)


def sweep_facts(con, archive_below=0.10):
    """把快忘光的归档（不删），核心规则免疫"""
    rows = con.execute(
        "SELECT id,text,kind,importance,strength,last_hit,created FROM facts "
        "WHERE status='active'").fetchall()
    n = 0
    for r in rows:
        d = dict(zip(["id", "text", "kind", "importance", "strength", "last_hit", "created"], r))
        if d["kind"] == "rule" or int(d["importance"] or 5) >= 9:
            continue
        if decay_score(d) < archive_below:
            con.execute("UPDATE facts SET status='archived' WHERE id=?", (d["id"],))
            n += 1
    con.commit()
    return n


def relevant_facts(con, query, limit=5):
    """先按关键词命中的事实，再补高重要度且没过期的"""
    rows = con.execute(
        "SELECT id,text,kind,importance,strength,last_hit,created FROM facts "
        "WHERE status='active'").fetchall()
    scored = []
    for r in rows:
        d = dict(zip(["id", "text", "kind", "importance", "strength", "last_hit", "created"], r))
        hit = sum(1 for ch in set(query) if ch in d["text"])
        score = decay_score(d) * (1 + hit * 0.25) * (int(d["importance"] or 5) / 5.0)
        if hit == 0 and int(d["importance"] or 5) < 7:
            continue
        scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[:limit]]


def touch_facts(con, ids):
    now = datetime.now().strftime(TS)
    for i in ids:
        con.execute("UPDATE facts SET last_hit=?, hits=hits+1 WHERE id=?", (now, i))
    con.commit()


# ==================== 字符向量（TF-IDF，无模型） ====================
class CharVec:
    """本地稀疏向量：字符 bigram + TF-IDF，纯 Python，无依赖"""

    def __init__(self, con):
        self.con = con
        self.docs = []      # [(id, vec)]
        self.df = {}
        self.n = 0
        self.built = None

    def _grams(self, text):
        """优先用 jieba 词（语义更好），没有就退回字符 bigram"""
        t = text or ""
        out = {}
        try:
            import jieba as _jb
            for w in _jb.cut(t):
                w = w.strip()
                if len(w) >= 2 and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9+]+", w):
                    out[w] = out.get(w, 0) + 1
            if out:
                return out
        except Exception:
            pass
        c = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9+]", "", t)
        for i in range(len(c) - 1):
            g = c[i:i + 2]
            out[g] = out.get(g, 0) + 1
        return out

    def build(self):
        rows = self.con.execute("SELECT id, text FROM messages").fetchall()
        self.n = len(rows)
        self.df = {}
        raw = []
        for mid, text in rows:
            g = self._grams(text)
            raw.append((mid, g))
            for k in g:
                self.df[k] = self.df.get(k, 0) + 1
        self.docs = [(mid, self._tfidf(g, len(g))) for mid, g in raw]
        self.built = self.n
        return self.n

    def _tfidf(self, g, ln):
        vec = {}
        for k, c in g.items():
            idf = math.log((self.n + 1) / (self.df.get(k, 0) + 1)) + 1
            vec[k] = (1 + math.log(c)) * idf
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        for k in vec:
            vec[k] /= norm
        return vec

    def search(self, query, k=30):
        if self.built != self.n or not self.docs:
            self.build()
        qg = self._grams(query)
        if not qg:
            return []
        q = self._tfidf(qg, len(qg))
        scored = []
        for mid, vec in self.docs:
            dot = sum(w * vec.get(g, 0.0) for g, w in q.items())
            if dot > 0:
                scored.append((dot, mid))
        scored.sort(reverse=True)
        return [mid for _, mid in scored[:k]]


# ==================== 时间轴 ====================
def add_timeline(con, note, kind="tick"):
    con.execute("INSERT INTO timeline(ts,note,kind) VALUES(?,?,?)",
                (datetime.now().strftime(TS), note, kind))
    con.commit()


def recent_timeline(con, hours=6, limit=8):
    since = (datetime.now() - timedelta(hours=hours)).strftime(TS)
    rows = con.execute(
        "SELECT ts,note FROM timeline WHERE ts>=? ORDER BY ts DESC LIMIT ?",
        (since, limit)).fetchall()
    return list(reversed(rows))


def sample_timeline(con, now=None, pc_idle=-1):
    """每 20 分钟看一眼：他还在不在，记一笔"""
    now = now or datetime.now()
    last = con.execute(
        "SELECT ts FROM messages ORDER BY ts DESC LIMIT 1").fetchone()
    if not last:
        return
    try:
        mins = int((now - datetime.strptime(last[0][:19], TS)).total_seconds() / 60)
    except Exception:
        return
    if pc_idle >= 0 and pc_idle >= 30 and mins >= 30:
        note = f"电脑 {pc_idle} 分钟没动，他也 {mins} 分钟没说话"
    elif mins < 20:
        note = "他在说话"
    elif mins < 60:
        note = f"他 {mins} 分钟没说话"
    elif mins < 180:
        note = f"他安静了 {mins // 60} 个多小时"
    else:
        note = f"他已经 {mins // 60} 个小时没来"
    add_timeline(con, note, "tick")

MONTH_PROMPT = """把下面几篇周摘要压成一段**月记**（220 字以内）：
1. 第三人称陈述（"这个月他…"）
2. 保留：状态起伏、说定的事、没解决的事
3. 不要逐条罗列
4. 只输出正文

{text}
"""


def _agg(store, cfg, stream_chat_fn, src_level, dst_level, n, prompt_tpl, chat_id=None):
    con = store.con
    cid = chat_id or store.current_chat() or "main"
    rows = con.execute(
        "SELECT upto,text FROM summaries WHERE chat_id=? AND level=? ORDER BY id DESC LIMIT ?",
        (cid, src_level, n)).fetchall()
    if len(rows) < n:
        return False, f"{src_level} 不够 {n} 篇"
    # 已有的最高层是否已经覆盖到同样的时间点
    exist = con.execute(
        "SELECT upto FROM summaries WHERE chat_id=? AND level=? ORDER BY id DESC LIMIT 1",
        (cid, dst_level)).fetchone()
    if exist and exist[0] >= rows[0][0]:
        return False, "已经生成过"
    body = "\n\n".join(f"[{u[:10]}] {t}" for u, t in reversed(rows))
    buf = []
    try:
        for piece in stream_chat_fn([{"role": "user", "content": prompt_tpl.format(text=body)}],
                                    base_url=cfg["base_url"], api_key=cfg["api_key"],
                                    model=cfg["model"], temperature=0.5,
                                    max_tokens=600, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return False, f"调用失败: {e}"
    text = " ".join("".join(buf).split())[:800]
    if len(text) < 20:
        return False, "输出太短"
    con.execute("INSERT INTO summaries(chat_id,upto,text,created,level) VALUES(?,?,?,?,?)",
                (cid, rows[0][0], text, datetime.now().strftime(TS), dst_level))
    con.commit()
    return True, text


def make_month(store, cfg, stream_chat_fn, chat_id=None):
    return _agg(store, cfg, stream_chat_fn, "week", "month", 4, MONTH_PROMPT, chat_id)


def make_quarter(store, cfg, stream_chat_fn, chat_id=None):
    return _agg(store, cfg, stream_chat_fn, "month", "quarter", 3, MONTH_PROMPT, chat_id)
