# -*- coding: utf-8 -*-
"""
Companion Lite · 主服务（零依赖，标准库）

启动：python app.py  →  http://127.0.0.1:8899

只读写本目录 data/ 下的文件，绝不碰 SillyTavern。
"""
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 打包成 exe 后：数据放在 exe 旁边，资源在临时解包目录
if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
    RES = getattr(sys, "_MEIPASS", BASE)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    RES = BASE

sys.path.insert(0, os.path.join(RES, "core"))

from store import Store          # noqa: E402
from retrieval import Index      # noqa: E402
from llm import stream_chat      # noqa: E402
import state as st_mod          # noqa: E402
import diary as diary_mod        # noqa: E402
import proactive as pro_mod      # noqa: E402
import import_st
import importers as mover        # noqa: E402
import world as world_mod        # noqa: E402
import memory2 as mem2           # noqa: E402
import query as query_mod        # noqa: E402
import neural as neural_mod      # noqa: E402
import logx                      # noqa: E402
import rel as rel_mod            # noqa: E402
import selfquery                 # noqa: E402
import export_st                 # noqa: E402

PORT = int(os.environ.get("COMPANION_PORT") or 8899)
HOST = "127.0.0.1"   # 运行时由 pick_host() 决定
APP_VERSION = "3.2"
DATA = os.environ.get("COMPANION_DATA") or os.path.join(BASE, "data")
WEB = os.path.join(RES, "web")

DEFAULT_CONFIG = {
    "st_dir": "",
    "name": "",
    "user_name": "我",
    "base_url": "https://api.deepseek.com",
    "api_key": "",
    "model": "deepseek-flash",
    "temperature": 1.1,
    "max_tokens": 600,
    "history_rounds": 20,
    "memory_enabled": True,
    "memory_limit": 6,
    "memory_chars": 900,
    "setup_done": False,
    "state_enabled": True,
    "diary_enabled": True,
    "diary_hour": 23,
    "no_thinking": True,
    "proactive_enabled": False,
    "proactive_interval_min": 20,
    "bind": "auto",
    "quick_replies": ["今天好累", "陪我会儿", "讲讲你今天", "我睡不着", "晚安"],
    "world_enabled": False,
    "world_city": "",
    "world_hour": 7,
    "world_hot_count": 6,
    "summary_every": 40,
    "smart_query": True,
    "backup_enabled": True,
    "backup_hour": 4,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    "neural_vec": True,
    "bg_color": "#111318",
    "bg_image": "",
    "bg_dim": 0,
    "self_lookup": True,
    "show_thinking": False,
}


def tailscale_ip():
    """找本机的 100.x.y.z（Tailscale）地址"""
    try:
        import socket
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
        for ip in ips:
            if ip.startswith("100.") and not ip.startswith("100.100."):
                return ip
    except Exception:
        pass
    return None


def pick_host():
    """auto = 有 Tailscale 就用它，没有就本机"""
    cfg = load_config()
    want = (cfg.get("bind") or "auto").strip()
    if want and want != "auto":
        return want
    return tailscale_ip() or "127.0.0.1"


def load_config():
    path = os.path.join(DATA, "config.json")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# 记忆优先：上限只是防止异常文件撑爆上下文，正常内容一律完整注入
BLOCK_LIMITS = {"persona.md": 12000, "canon.md": 12000, "world.md": 12000, "user.md": 12000, "rules.md": 12000}


def clip(text, limit, name=""):
    """按行截断，尽量不切句子；一旦截断必须留痕"""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    try:
        LOG.warn("memory", f"⚠️ 注入被截断：{name or '未知文件'} {len(text)} -> {limit} 字")
    except Exception:
        pass
    cut = text[:limit]
    pos = max(cut.rfind("\n"), cut.rfind("。"))
    if pos > limit * 0.6:
        cut = cut[:pos + 1]
    return cut.rstrip() + "\n…"


def read_block(name):
    path = os.path.join(DATA, name)
    if os.path.exists(path):
        try:
            return open(path, encoding="utf-8").read().strip()
        except Exception:
            return ""
    return ""


def load_setup_raw():
    path = os.path.join(DATA, "setup.json")
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


# 说话纪律（系统加的，不属于角色设定）
MEM_RULE = """# 记性纪律
1. 事实只有两个来源：{{user}}这次说的话，以及标了日期的「他说」记录。
2. 对话历史里**你自己说过的**话，可能是记错的；不要把它当成事实，也不要顺着它重复。
3. 如果发现自己前面说错了，直接改口，不要圆，不要编理由。
4. 记不清具体哪天，就说记不清；但不要否认事情本身发生过。"""



_COND_CACHE = {"mtime": None, "rules": []}


def load_cond_rules():
    """条件守则：只在关键词出现时才注入（恢复酒馆的触发方式）"""
    path = os.path.join(DATA, "cond_rules.json")
    try:
        mt = os.path.getmtime(path)
        if _COND_CACHE["mtime"] != mt:
            import json as _j
            _COND_CACHE["rules"] = _j.load(open(path, encoding="utf-8"))
            _COND_CACHE["mtime"] = mt
    except Exception:
        return []
    return _COND_CACHE["rules"]


def match_cond_rules(text):
    """返回命中的守则文本列表"""
    out = []
    try:
        for r in load_cond_rules():
            if any(k and k in text for k in (r.get("keys") or [])):
                out.append(r.get("text") or "")
    except Exception:
        pass
    return [x for x in out if x]


def load_persona(cfg):
    """角色 + 世界观 + 主人档案 + 规则，拼成静态前缀"""
    parts = []
    p = read_block("persona.md")
    if p:
        parts.append(clip(p, BLOCK_LIMITS["persona.md"], "persona.md"))
    c = read_block("canon.md")
    if c:
        parts.append("# 【不能改的设定】最高优先：任何记忆/摘要/事实与这里冲突时，一律以这里为准\n"
                     + clip(c, BLOCK_LIMITS["canon.md"], "canon.md"))
    w = read_block("world.md")
    if w:
        parts.append("# 世界观\n" + clip(w, BLOCK_LIMITS["world.md"], "world.md"))
    u = read_block("user.md")
    if u:
        parts.append("# 主人档案（关于{{user}}）\n" + clip(u, BLOCK_LIMITS["user.md"], "user.md"))
    r = read_block("rules.md")
    if r:
        parts.append("# 规则\n" + clip(r, BLOCK_LIMITS["rules.md"], "rules.md"))
    if not parts:
        parts.append("你是{{char}}。用自然、口语的中文和{{user}}说话，像真人聊天。"
                     "不要复述设定，不要解释自己，直接回应。")
    parts.append(MEM_RULE)
    txt = "\n\n".join(parts)
    return (txt.replace("{{char}}", cfg["name"])
               .replace("{{user}}", cfg["user_name"]))


store = Store(DATA)
try:
    store.con.execute("PRAGMA journal_mode=WAL")
    store.con.execute("PRAGMA synchronous=1")
    store.con.commit()
except Exception:
    pass
LOG = logx.Log(os.path.join(DATA, "logs", "app.log"))
LOG.install_hooks()


LOCK_PATH = None


def _pid_alive(pid):
    """Windows/Unix 通用的进程存活判断"""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
            if not h:
                return False
            code = wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259   # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_lock():
    """同一份数据只能有一个实例；返回 (ok, 旧pid或None)"""
    global LOCK_PATH
    LOCK_PATH = os.path.join(DATA, ".instance.lock")
    if os.path.exists(LOCK_PATH):
        try:
            txt = open(LOCK_PATH, encoding="utf-8").read().strip().split()
            old = int(txt[0])
            if _pid_alive(old) and old != os.getpid():
                return False, old
        except Exception:
            pass
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception:
        pass
    return True, None


def release_lock():
    try:
        if LOCK_PATH and os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def verify_import(src_db=None, expect=None, chat_id=None):
    """搬家后自检：条数 / 完整性 / 抽样"""
    rep = {"ok": True, "problems": [], "count": 0, "integrity": ""}
    try:
        rep["count"] = store.count_all()
        rep["integrity"] = store.con.execute("PRAGMA quick_check").fetchone()[0]
        if rep["integrity"] != "ok":
            rep["ok"] = False
            rep["problems"].append(f"库完整性异常: {rep['integrity']}")
        if expect is not None and rep["count"] != expect:
            rep["problems"].append(f"条数不一致：预期 {expect}，实际 {rep['count']}")
            rep["ok"] = False
        rows = store.con.execute(
            "SELECT ts,speaker,text FROM messages ORDER BY RANDOM() LIMIT 3").fetchall()
        rep["sample"] = [{"ts": r[0], "who": r[1], "text": r[2][:40]} for r in rows]
        if not rows:
            rep["problems"].append("库里一条消息都没有")
            rep["ok"] = False
    except Exception as e:
        rep["ok"] = False
        rep["problems"].append(f"校验时出错: {e}")
    return rep


def make_backup(tag="manual"):
    """把 data 压成 zip 放到 data/backups/，返回路径"""
    import zipfile
    bdir = os.path.join(DATA, "backups")
    os.makedirs(bdir, exist_ok=True)
    name = f"{tag}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    target = os.path.join(bdir, name)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(DATA):
            if "backups" in root:
                continue
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, DATA))
    return target


def log(tag, msg):
    LOG.info(tag, msg)


def logerr(tag, msg, exc=None):
    LOG.error(tag, msg, exc)
index_lock = threading.Lock()
_index = None
_vec = None
_boost = None
_nv = None


def get_index():
    global _index, _vec
    if _index is None:
        with index_lock:
            if _index is None:
                _index = Index(store.db_path)
    return _index


def pc_idle_minutes():
    """Windows 上取"多久没动鼠标键盘"（分钟）；失败返回 -1"""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
            return -1
        millis = ctypes.windll.kernel32.GetTickCount() - li.dwTime
        return int(millis / 60000)
    except Exception:
        return -1


def notify(title, msg):
    """发一条手机推送（ntfy，免费）。没配就静默跳过"""
    cfg = load_config()
    topic = (cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return False
    server = (cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    try:
        import urllib.request
        req = urllib.request.Request(
            f"{server}/{topic}",
            data=msg.encode("utf-8"),
            headers={"Title": title.encode("utf-8").decode("latin-1", "ignore"),
                     "Tags": "speech_balloon"},
            method="POST")
        urllib.request.urlopen(req, timeout=8)
        return True
    except Exception as e:
        print("[notify] 失败:", e)
        return False


def add_usage(tin, tout):
    """粗略累计 token（按字数估：中文约 0.7 token/字）"""
    try:
        u = json.loads(store.get_state("usage") or "{}")
    except Exception:
        u = {}
    u["in"] = int(u.get("in", 0)) + int(tin)
    u["out"] = int(u.get("out", 0)) + int(tout)
    u["calls"] = int(u.get("calls", 0)) + 1
    u["day"] = datetime.now().strftime("%Y-%m-%d")
    store.set_state("usage", json.dumps(u, ensure_ascii=False))


def clean_reply(t):
    """去掉空行、连续重复的行"""
    lines = [x.strip() for x in (t or "").split("\n") if x.strip()]
    out = []
    for x in lines:
        if out and x == out[-1]:
            continue
        out.append(x)
    return "\n".join(out).strip()




def get_neural():
    global _nv
    if _nv is None:
        _nv = neural_mod.NeuralVec(DATA, store.con)
    return _nv


def warm_vec():
    """后台预热向量索引，免得第一次搜索卡 3 秒"""
    import time as _t
    _t.sleep(8)
    try:
        n = get_vec().build()
        LOG.info("vec", f"预热完成 {n} 条")
    except Exception as e:
        LOG.info("vec", f"预热跳过: {e}")


def neural_warmup():
    """启动后台慢慢建向量索引，不挡聊天"""
    try:
        cfg = load_config()
        if not cfg.get("neural_vec", True):
            return
        nv = get_neural()
        if nv.build():
            LOG.info("neural", f"索引就绪 {len(nv.ids)} 条")
    except Exception as e:
        LOG.info("neural", f"跳过: {e}")


def get_boost():
    global _boost
    if _boost is None:
        _boost = query_mod.QueryBoost(load_config(), stream_chat)
    return _boost


def get_vec():
    global _vec
    if _vec is None:
        _vec = mem2.CharVec(store.con)
    return _vec


def hybrid_search(query, limit=6, max_chars=900):
    """FTS（主）+ 字符向量（补）→ RRF 融合"""
    fts = get_index().search(query, limit=limit, max_chars=max_chars)
    items = [it for it in (fts.get("items") or []) if it["text"].strip() != query.strip()]
    # 只在主路不够确定时才用向量补（AgentIR 的级联思路）
    try:
        need_vec = len(items) < 3 or fts.get("margin", 99) < 3.0
    except Exception:
        need_vec = False
    vids = []
    if need_vec:
        try:
            vids = get_vec().search(query, 20)
            # 太弱的相似不要
            vids = vids[:10]
        except Exception:
            vids = []
    if vids:
        try:
            from retrieval import parse_time_range as _ptr
            _rng = _ptr(query)
        except Exception:
            _rng = None
        score = {}
        for rank, it in enumerate(items):
            score[it["text"]] = score.get(it["text"], 0) + 1.0 / (60 + rank + 1)
        qmarks = ",".join("?" * len(vids))
        rows = store.con.execute(
            f"SELECT id,ts,speaker,text FROM messages WHERE id IN ({qmarks})", vids).fetchall()
        by_id = {r[0]: r for r in rows}
        for rank, vid in enumerate(vids):
            r = by_id.get(vid)
            if not r:
                continue
            if _rng:
                try:
                    from datetime import datetime as _dt3
                    _ts = _dt3.strptime(str(r[1])[:19], "%Y-%m-%d %H:%M:%S")
                    if not (_rng[0] <= _ts <= _rng[1]):
                        continue
                except Exception:
                    pass
            score[r[3]] = score.get(r[3], 0) + 1.0 / (60 + rank + 1)
        merged = {}
        for it in items:
            merged[it["text"]] = it
        for vid in vids:
            r = by_id.get(vid)
            if r:
                merged[r[3]] = {"id": r[0], "ts": r[1], "speaker": r[2], "text": r[3]}
        out = sorted(merged.values(), key=lambda it: -score.get(it["text"], 0))
        out = [it for it in out if it["text"].strip() != query.strip()][:limit]
        used, res = 0, []
        for it in out:
            if used + len(it["text"]) > max_chars:
                it = dict(it)
                it["text"] = it["text"][:max(0, max_chars - used)]
            if not it["text"]:
                break
            used += len(it["text"])
            res.append(it)
        items = res
        fts["items"] = items
        fts["mode"] = fts.get("mode", "lexical") + "+vec"

    # ---- 神经向量（语义补召，只在可用时）----
    try:
        cfg2 = load_config()
        nv = get_neural()
        if cfg2.get("neural_vec", True) and nv.ready:
            nhits = nv.search(query, 10)
            if nhits:
                ids = [i for i, sim in nhits]
                qm = ",".join("?" * len(ids))
                rows = store.con.execute(
                    f"SELECT id,ts,speaker,text FROM messages WHERE id IN ({qm})", ids).fetchall()
                by_id = {r[0]: r for r in rows}
                have = {it["text"] for it in (fts.get("items") or [])}
                merged = list(fts.get("items") or [])
                for rank, mid in enumerate(ids):
                    r = by_id.get(mid)
                    if not r or r[3] in have:
                        continue
                    have.add(r[3])
                    merged.append({"id": r[0], "ts": r[1], "speaker": r[2], "text": r[3]})
                if len(merged) > len(fts.get("items") or []):
                    fts["items"] = merged[:limit + 4]
                    fts["mode"] = str(fts.get("mode", "lexical")) + "+neural"
    except Exception:
        pass

    # ---- 低置信时才花钱：查询改写 + 重排 ----
    try:
        low_conf = len(fts.get("items") or []) < 3 or fts.get("margin", 99) < 3.0
    except Exception:
        low_conf = False
    import re as _re
    PAST = ("记得", "之前", "上次", "以前", "那次", "那天", "哪天", "说过",
            "聊过", "提过", "还记得", "回忆", "我们聊", "我说过")
    past_intent = any(k in query for k in PAST)
    if low_conf and past_intent and load_config().get("smart_query", True):
        boost = get_boost()
        terms, about_past = boost.expand(query)
        if terms:
            try:
                extra = get_index().search(query + " " + " ".join(terms),
                                           limit=limit + 2, max_chars=max_chars * 2)
                seen = {it["text"] for it in (fts.get("items") or [])}
                merged = list(fts.get("items") or [])
                for it in extra.get("items") or []:
                    if it["text"] not in seen:
                        seen.add(it["text"])
                        merged.append(it)
                if merged:
                    fts["items"] = merged
                    fts["mode"] = str(fts.get("mode", "lexical")) + "+expand"
            except Exception:
                pass
        try:
            if len(fts.get("items") or []) >= 4:
                fts["items"] = boost.rerank(query, fts["items"], keep=limit)
                fts["mode"] = str(fts.get("mode", "")) + "+rerank"
        except Exception:
            pass
        # 截断到字数预算
        out, used = [], 0
        for it in fts.get("items") or []:
            if used + len(it["text"]) > max_chars:
                it = dict(it)
                it["text"] = it["text"][: max(0, max_chars - used)]
            if not it["text"]:
                break
            used += len(it["text"])
            out.append(it)
        fts["items"] = [it for it in out[:limit] if it["text"].strip() != query.strip()]
    return fts



def _absolutize(text, base_dt):
    """把摘要里的"今天/明天/昨天"换成具体日期；引号内保持原样"""
    from datetime import timedelta as _td

    def d(n, tail=""):
        x = base_dt + _td(days=n)
        return f"{x.month}月{x.day}日{tail}"

    table = [
        ("待明日", d(1)), ("明早", d(1)), ("明天", d(1)), ("明日", d(1)),
        ("后天", d(2)),
        ("今晚", d(0, "晚上")), ("今天", d(0)), ("今日", d(0)),
        ("刚才", d(0)), ("刚刚", d(0)),
        ("刚说", d(0, "说")), ("刚提到", d(0, "提到")), ("刚聊", d(0, "聊")),
        ("昨晚", d(-1, "晚上")), ("昨天", d(-1)), ("昨日", d(-1)),
    ]
    out = []
    inside = 0          # 0=外面 1=「」 2=“” 3=""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "「":
            inside += 1
            out.append(ch)
            i += 1
            continue
        if ch == "」" and inside == 1:
            inside = 0
            out.append(ch)
            i += 1
            continue
        if ch in "“":
            inside = 2
            out.append(ch)
            i += 1
            continue
        if ch in "”" and inside == 2:
            inside = 0
            out.append(ch)
            i += 1
            continue
        if ch == '"' and inside in (0, 3):
            inside = 3 if inside == 0 else 0
            out.append(ch)
            i += 1
            continue
        hit = None
        if inside == 0:
            for word, rep in table:
                if text.startswith(word, i):
                    hit = (word, rep)
                    break
        if hit:
            out.append(hit[1])
            i += len(hit[0])
        else:
            out.append(ch)
            i += 1
    return "".join(out)



def old_summary_hits(query, limit=2, exclude_latest=True):
    """在历史日摘要里按关键词找，返回 [(upto, text)]"""
    out = []
    try:
        rows = store.con.execute(
            "SELECT upto, text FROM summaries ORDER BY upto DESC LIMIT 60").fetchall()
        if exclude_latest and rows:
            rows = rows[1:]          # 最近那篇已经单独注入了
        terms = []
        try:
            terms = [t for t in get_index().extract_terms(query or "") if 2 <= len(t) <= 8]
        except Exception:
            terms = []
        if not terms:
            terms = [t for t in re.split(r"[\s，。？?！!、,]+", query or "") if 2 <= len(t) <= 6]
        if not terms:
            return []
        scored = []
        for upto, text in rows:
            hit = sum(1 for t in terms if t in text)
            if hit:
                scored.append((hit, upto, text))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for _, upto, text in scored[:limit]:
            out.append((upto, text))
    except Exception:
        pass
    return out


def memory_blocks(cfg, query):
    """摘要 + 事实 + 时间轴"""
    parts = []
    cid = store.current_chat() or "main"
    try:
        for _up, _st in old_summary_hits(query, limit=2):
            try:
                from datetime import datetime as _dt4
                _dd = (_dt4.now() - _dt4.strptime(_up[:19], "%Y-%m-%d %H:%M:%S")).days
                _body = _absolutize(_st, _dt4.strptime(_up[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                _dd, _body = 0, _st
            parts.append(f"[旧摘要 · {_up[:10]}（{_dd} 天前）· 里面的今天/明天指那一天]\n" + _body)
    except Exception:
        pass

    try:
        parts.append("[性情]\n" + rel_mod.format_traits(store.con))
        g = rel_mod.format_grudges(store.con)
        if g:
            parts.append(g)
    except Exception:
        pass
    try:
        wk = mem2.latest_week(store.con, cid)
        if wk:
            parts.append("[这一周 · 到 " + wk[0][5:10] + "]\n" + wk[1])
        sums = mem2.recent_summaries(store.con, cid, 1)
        for upto, text in sums:
            try:
                from datetime import datetime as _dt2
                _d = (_dt2.now() - _dt2.strptime(upto[:19], "%Y-%m-%d %H:%M:%S")).days
            except Exception:
                _d = 0
            head = f"[摘要 · 写于 {_d} 天前（{upto[5:16]}）· 里面的\"今天/明天\"指那一天]"
            try:
                from datetime import datetime as _dtb
                body = _absolutize(text, _dtb.strptime(upto[:19], "%Y-%m-%d %H:%M:%S"))
            except Exception:
                body = text
            if _d >= 1:
                body += "\n（上面是当时记的；\"明天/待明日/还没\"都是那天的说法，不是现在的待办）"
            parts.append(head + "\n" + body)
    except Exception:
        pass
    try:
        facts = mem2.relevant_facts(store.con, query, 5)
        if facts:
            lines = [f"- {f['text']}" for f in facts]
            parts.append("[关于他的事]\n" + "\n".join(lines))
            mem2.touch_facts(store.con, [f["id"] for f in facts])
    except Exception:
        pass
    try:
        tl = mem2.recent_timeline(store.con, hours=6, limit=6)
        if tl:
            lines = [f"{t[11:16]} {note}" for t, note in tl]
            parts.append("[最近几小时]\n" + "\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(parts)


def build_messages(cfg, user_text, lookup=None, lookup_terms=None):
    """人设放最前（静态前缀，利于缓存），记忆块插在最后一条用户消息之前"""
    msgs = [{"role": "system", "content": load_persona(cfg)}]

    rounds = max(1, int(cfg.get("history_rounds", 20)))
    hist = store.recent(rounds * 2)   # (ts, speaker, text, id)

    evidence = ""
    do_lookup = True
    if lookup is False:
        do_lookup = False
    q = user_text
    if lookup_terms:
        q = user_text + " " + " ".join(lookup_terms)

    if cfg.get("memory_enabled") and user_text.strip() and do_lookup:
        try:
            r = hybrid_search(q,
                                   limit=int(cfg.get("memory_limit", 6)),
                                   max_chars=int(cfg.get("memory_chars", 900)))
            evidence = format_evidence(r)
        except Exception as e:
            evidence = ""

    # 历史（不含最后一条，最后一条单独作为 user）
    body = hist[:-1] if hist else []
    for ts, sp, text, _mid in body:
        msgs.append({"role": "user" if sp == "user" else "assistant", "content": text})

    try:
        conds = match_cond_rules(user_text)
        if conds:
            msgs.append({"role": "system", "content": "[这一情形怎么做]\n" + "\n".join(conds)})
            LOG.info("cond", f"命中 {len(conds)} 条守则")
    except Exception:
        pass

    mb = ""
    if cfg.get("memory_enabled", True) and lookup is not False:
        mb = memory_blocks(cfg, user_text)
    if mb:
        msgs.append({"role": "system", "content": mb})

    if cfg.get("state_enabled", True):
        try:
            st = st_mod.compute_state(store)
            di = store.recent_diary(1)
            block = st_mod.format_state_block(st, di)
            if cfg.get("world_enabled"):
                wt = store.get_state("world_text")
                wd = store.get_state("world_date")
                today = datetime.now().strftime("%Y-%m-%d")
                if wt and wd == today:
                    block += "\n\n[外面]\n" + wt
            if block:
                msgs.append({"role": "system", "content": block})
        except Exception:
            pass

    if evidence:
        msgs.append({"role": "system", "content": evidence})

    msgs.append({"role": "user", "content": user_text})
    return msgs


def format_evidence(r):
    items = r.get("items") or []
    if not items:
        return ""
    now = datetime.now()
    lines = ["[长期记忆检索结果 — 这是过去说过的话，不是现在]"]
    for it in items:
        try:
            dt = datetime.strptime(it["ts"], "%Y-%m-%d %H:%M:%S")
            days = (now.date() - dt.date()).days
            rel = "今天" if days == 0 else ("昨天" if days == 1 else f"{days} 天前")
            stamp = f"{dt:%Y-%m-%d %H:%M} · {rel}"
        except Exception:
            stamp = it["ts"]
        who = "他说" if it["speaker"] == "user" else "你说过"
        lines.append(f"- [{stamp}] {who}：{it['text'].replace(chr(10), ' ')[:200]}")
    lines.append("")
    lines.append("【怎么用这些记录】")
    lines.append("1. 「他说」的内容是事实依据；「你说过」只是你以前的原话，用来回忆。")
    lines.append("2. 不要照抄「你说过」那些句子，也不要重复整段旧回答——用现在的话重新说。")
    lines.append("3. 每行开头是历史时间，不是现在几点。")
    lines.append("4. 「你说过」那些句子只是旧记录，语气可能和现在的你不一样——只取事实，别学那个腔调。")
    lines.append("5. 记不清具体哪天就说记不清，但不要否认事情本身。")
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 工具 ----------
    def _json(self, code, obj):
        try:
            ms = int((time.time() - getattr(self, "_t0", time.time())) * 1000)
            path = self.path.split("?")[0]
            if code >= 400 or ms >= 3000:
                LOG.warn("http", f"{self.command} {path} -> {code} {ms}ms")
            elif path.startswith("/api/") and path not in ("/api/state", "/api/background"):
                LOG.info("http", f"{self.command} {path} -> {code} {ms}ms")
        except Exception:
            pass
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n) or b"{}")

    def _file(self, path, ctype, no_cache=False):
        if not os.path.exists(path):
            self._json(404, {"error": "not found"})
            return
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if no_cache:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    # ---------- 路由 ----------
    def do_GET(self):
        self._t0 = time.time()
        p = self.path.split("?")[0]
        if p == "/manifest.webmanifest":
            return self._json(200, {
                "name": "Companion",
                "short_name": "Companion",
                "id": "/",
                "start_url": "/?src=pwa",
                "scope": "/",
                "display": "standalone",
                "display_override": ["standalone", "fullscreen"],
                "orientation": "portrait",
                "lang": "zh-CN",
                "background_color": "#111318",
                "theme_color": "#111318",
                "categories": ["social", "lifestyle"],
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                    {"src": "/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
                ],
            })

        if p.startswith("/backgrounds/"):
            name = os.path.basename(p)
            path = os.path.join(DATA, "backgrounds", name)
            if os.path.exists(path):
                ext = os.path.splitext(name)[1].lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "application/octet-stream")
                return self._file(path, ctype)
            return self._json(404, {"error": "not found"})

        if p in ("/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"):
            return self._file(os.path.join(RES, "web", "icons", os.path.basename(p)), "image/png", no_cache=True)

        if p == "/sw.js":
            path = os.path.join(RES, "web", "sw.js")
            if os.path.exists(path):
                return self._file(path, "application/javascript; charset=utf-8", no_cache=True)
            return self._json(404, {"error": "no sw"})

        if p == "/offline":
            return self._json(503, {"error": "offline"})

        if p == "/icon.svg":
            svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                   b'<rect width="64" height="64" rx="14" fill="#111318"/>'
                   b'<circle cx="32" cy="32" r="13" fill="#7aa2f7"/></svg>')
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return

        if p in ("/", "/index.html"):
            return self._file(os.path.join(WEB, "index.html"), "text/html; charset=utf-8", no_cache=True)
        if p == "/api/config":
            cfg = load_config()
            shown = dict(cfg)
            shown["api_key"] = ("***" + cfg["api_key"][-4:]) if cfg.get("api_key") else ""
            shown["_count"] = store.count()
            shown["_persona"] = load_persona(cfg)[:120]
            return self._json(200, shown)
        if p == "/api/history":
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                limit = int((qs.get("limit", ["80"])[0]))
            except Exception:
                limit = 80
            limit = max(10, min(500, limit))
            before = (qs.get("before", [""])[0] or "").strip()
            rows = store.recent(limit, before_id=before or None)
            total = store.total_in_chat()
            oldest = rows[0][3] if rows else None
            has_more = bool(oldest) and store.recent(1, before_id=oldest) != []
            return self._json(200, {
                "chat": store.current_chat(),
                "total": total,
                "has_more": has_more,
                "items": [{"id": i, "ts": t, "speaker": s, "text": x} for t, s, x, i in rows]})
        if p == "/api/diary":
            return self._json(200, {"items": store.recent_diary(14),
                                    "state": st_mod.compute_state(store)})

        if p == "/api/files":
            from urllib.parse import parse_qs, unquote
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            name = unquote((qs.get("name", [""])[0]) or "")
            if name:
                if name not in ['persona.md', 'canon.md', 'world.md', 'user.md', 'rules.md', 'cond_rules.json']:
                    return self._json(400, {"error": "不允许的文件"})
                fp = os.path.join(DATA, name)
                txt = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
                return self._json(200, {"name": name, "text": txt, "size": len(txt)})
            out = []
            for n in ['persona.md', 'canon.md', 'world.md', 'user.md', 'rules.md', 'cond_rules.json']:
                fp = os.path.join(DATA, n)
                out.append({"name": n,
                            "size": len(open(fp, encoding="utf-8").read()) if os.path.exists(fp) else 0})
            return self._json(200, {"files": out})

        if p == "/api/setup":
            cfg = load_config()
            return self._json(200, {
                "done": bool(cfg.get("setup_done")),
                "persona": read_block("persona.md"),
                "world": read_block("world.md"),
                "user": read_block("user.md"),
                "rules": read_block("rules.md"),
                "raw": load_setup_raw(),
            })

        if p == "/api/export_st":
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            cid = (qs.get("chat", [None])[0]) or (store.current_chat() or "main")
            cfg = load_config()
            data, n = export_st.write_zip(store, cid, cfg.get("name", "她"),
                                          cfg.get("user_name", "我"))
            if not data:
                return self._json(400, {"error": "这段聊天是空的"})
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             "attachment; filename=st-export.zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/export":
            import io, zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name in ("persona.md", "world.md", "user.md", "rules.md", "setup.json"):
                    fp = os.path.join(DATA, name)
                    if os.path.exists(fp):
                        z.write(fp, name)
                if os.path.exists(store.db_path):
                    z.write(store.db_path, "memory.db")
            data = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=companion-backup.zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/state":
            return self._json(200, {
                "messages": store.count(),
                "total": store.count_all(),
                "chat": store.current_chat(),
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

        if p == "/api/chats":
            return self._json(200, {"items": store.list_chats(), "current": store.current_chat()})

        if p == "/api/pwa":
            from urllib.parse import unquote
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            LOG.info("pwa", unquote(q)[:200])
            return self._json(200, {"ok": True})

        if p == "/api/logs":
            n = 200
            try:
                n = int((self.path.split("n=")[1].split("&")[0]) if "n=" in self.path else 200)
            except Exception:
                pass
            if "download=1" in self.path and os.path.exists(LOG.path):
                return self._file(LOG.path, "text/plain; charset=utf-8")
            return self._json(200, {"lines": LOG.tail(max(20, min(2000, n))),
                                    "path": LOG.path,
                                    "size": os.path.getsize(LOG.path) if os.path.exists(LOG.path) else 0})

        if p == "/api/files":
            name = (body.get("name") or "").strip()
            text = body.get("text")
            if name not in ['persona.md', 'canon.md', 'world.md', 'user.md', 'rules.md', 'cond_rules.json']:
                return self._json(400, {"error": "不允许的文件"})
            if not isinstance(text, str):
                return self._json(400, {"error": "内容无效"})
            if name.endswith(".json"):
                try:
                    import json as _j
                    _j.loads(text)
                except Exception as e:
                    return self._json(400, {"error": f"JSON 格式不对: {e}"})
            fp = os.path.join(DATA, name)
            old_len = len(open(fp, encoding="utf-8").read()) if os.path.exists(fp) else 0
            try:
                make_backup("before-edit-" + name.replace(".", "_"))
            except Exception:
                pass
            open(fp, "w", encoding="utf-8").write(text)
            LOG.info("files", f"{name}: {old_len} -> {len(text)} 字")
            return self._json(200, {"ok": True, "name": name, "size": len(text)})

        if p == "/api/background":
            cfg = load_config()
            return self._json(200, {
                "color": cfg.get("bg_color") or "#111318",
                "image": cfg.get("bg_image") or "",
                "dim": cfg.get("bg_dim", 0),
            })

        if p == "/api/neural":
            nv = get_neural()
            return self._json(200, {
                "ready": bool(nv.ready),
                "count": int(len(nv.ids)) if nv.ids is not None else 0,
                "model": os.path.exists(nv.model_path),
                "error": nv.error or "",
            })

        if p == "/api/usage":
            try:
                u = json.loads(store.get_state("usage") or "{}")
            except Exception:
                u = {}
            return self._json(200, {"usage": u})

        if p == "/api/memory":
            cid = store.current_chat() or "main"
            sums = mem2.recent_summaries(store.con, cid, 3)
            facts = store.con.execute(
                "SELECT text,kind,importance,status FROM facts ORDER BY id DESC LIMIT 12").fetchall()
            tl = mem2.recent_timeline(store.con, hours=12, limit=8)
            try:
                traits = rel_mod._get_traits(store.con)
                grudges = [{"text": t, "severity": s_, "created": c}
                           for t, s_, c in rel_mod.open_grudges(store.con, 5)]
            except Exception:
                traits, grudges = {}, []
            return self._json(200, {
                "summaries": [{"upto": u, "text": t} for u, t in sums],
                "facts": [{"text": f[0], "kind": f[1], "importance": f[2], "status": f[3]} for f in facts],
                "timeline": [{"ts": t, "note": n} for t, n in tl],
                "traits": {k: round(v, 1) for k, v in traits.items()},
                "grudges": grudges,
            })

        if p == "/api/world":
            return self._json(200, {
                "enabled": bool(load_config().get("world_enabled")),
                "text": store.get_state("world_text") or "",
                "date": store.get_state("world_date") or "",
                "today": datetime.now().strftime("%Y-%m-%d"),
            })

        if p == "/api/quick":
            cfg = load_config()
            return self._json(200, {"items": cfg.get("quick_replies") or []})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        global store, _index, _vec, _boost, _nv
        self._t0 = time.time()
        p = self.path.split("?")[0]
        try:
            body = self._read_json()
        except Exception:
            return self._json(400, {"error": "bad json"})

        if p == "/api/search":
            q = (body.get("query") or "").strip()
            if not q:
                return self._json(400, {"error": "empty query"})
            r = hybrid_search(q, limit=int(body.get("limit", 6)))
            return self._json(200, {
                "ok": True, "ms": r["ms"], "mode": r["mode"],
                "terms": r["terms"], "items": r["items"],
                "text": format_evidence(r),
            })

        if p in ("/api/import/preview", "/api/import/commit"):
            src = (body.get("source") or "paste").strip()
            txt = body.get("text") or ""
            her = (body.get("her_name") or "").strip()
            me = (body.get("me_name") or "").strip()
            if not txt.strip():
                return self._json(400, {"error": "内容为空"})
            try:
                rows = mover.parse(src, txt, her, me)
            except Exception as e:
                return self._json(400, {"error": str(e)})
            if not rows:
                return self._json(400, {"error": "没解析出消息，检查一下格式或「她叫什么」填对了没有"})
            stat = {"total": len(rows),
                    "char": sum(1 for r in rows if r["speaker"] == "char"),
                    "user": sum(1 for r in rows if r["speaker"] == "user"),
                    "first": rows[0]["ts"], "last": rows[-1]["ts"]}
            if p.endswith("preview"):
                return self._json(200, {"ok": True, "stat": stat, "sample": rows[:20]})
            # commit
            title = (body.get("chat_title") or "").strip()
            cid = store.current_chat() or "main"
            if title:
                cid = store.new_chat(title)
            added = dup = 0
            seen = set()
            for r in rows:
                key = (r["ts"], r["speaker"], r["text"])
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                ex = store.con.execute(
                    "SELECT 1 FROM messages WHERE chat_id=? AND ts=? AND speaker=? AND text=? LIMIT 1",
                    (cid, r["ts"], r["speaker"], r["text"])).fetchone()
                if ex:
                    dup += 1
                    continue
                store.append(r["speaker"], r["text"], r["ts"], cid)
                added += 1
            try:
                store.rebuild_grams()
            except Exception:
                pass
            LOG.info("import", f"{src}: 解析 {len(rows)} 条，入库 {added}，跳过 {dup}")
            return self._json(200, {"ok": True, "stat": stat, "added": added,
                                    "skipped": dup, "chat": cid})

        if p == "/api/config":
            cfg = load_config()
            for k, v in body.items():
                if k in cfg and not k.startswith("_"):
                    cfg[k] = v
            if body.get("api_key") and body["api_key"].startswith("***"):
                cfg["api_key"] = load_config().get("api_key", "")
            save_config(cfg)
            return self._json(200, {"ok": True})

        if p == "/api/export_st":
            cfg = load_config()
            cid = (body.get("chat") or store.current_chat() or "main").strip()
            out_dir = (body.get("dir") or "").strip()
            if not out_dir:
                name = export_st.safe_name(cfg.get("name", "她"))
                _st = (cfg.get("st_dir") or os.environ.get("COMPANION_ST_DIR") or "").strip()
            if _st:
                out_dir = os.path.join(_st, "data", "default-user", "chats", name)
            else:
                out_dir = os.path.join(DATA, "export", name)
            try:
                make_backup("pre-export-st")
            except Exception:
                pass
            r = export_st.write_to_dir(store, cid, out_dir,
                                       cfg.get("name", "她"), cfg.get("user_name", "我"))
            if r.get("ok"):
                LOG.info("export_st", f"{r['count']} 条 -> {r['path']}")
            return self._json(200, r)

        if p == "/api/reset":
            if (body.get("confirm") or "").strip() != "清空":
                return self._json(400, {"error": "需要输入'清空'两个字确认"})
            try:
                bak = make_backup("before-reset")
            except Exception as e:
                return self._json(500, {"error": f"备份失败，已中止: {e}"})

            cfg = load_config()
            keep = {k: cfg.get(k) for k in ("base_url", "api_key", "model", "temperature",
                                            "max_tokens", "no_thinking", "bg_color",
                                            "bg_image", "bg_dim") if k in cfg}

            try:
                store.close()
            except Exception:
                pass
            for fn in ("memory.db", "memory.db-wal", "memory.db-shm", "setup.json",
                       "persona.md", "world.md", "user.md", "rules.md"):
                fp = os.path.join(DATA, fn)
                if os.path.exists(fp):
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
            bdir = os.path.join(DATA, "backgrounds")
            if os.path.isdir(bdir):
                for fn in os.listdir(bdir):
                    try:
                        os.remove(os.path.join(bdir, fn))
                    except Exception:
                        pass

            store = Store(DATA)
            try:
                mem2.ensure_schema(store.con)
                rel_mod.ensure_schema(store.con)
            except Exception as e:
                LOG.error("reset", f"重建表失败: {e}", e)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(keep or {})
            cfg["setup_done"] = False
            save_config(cfg)

            _index = None
            _vec = None
            _boost = None
            _nv = None
            LOG.info("reset", f"已清空，备份在 {os.path.basename(bak)}")
            return self._json(200, {"ok": True, "backup": os.path.basename(bak)})

        if p == "/api/import_st":
            data_dir = DATA
            custom = {}
            for k in ("card", "world", "memdb"):
                v = (body.get(k) or "").strip()
                if v:
                    custom[k] = v
            chats_dir = (body.get("chats_dir") or "").strip()
            chat_title = (body.get("title") or "").strip()
            # 给了聊天原文目录、又没指定记忆库 → 以原文为准，跳过 memory.db
            if chats_dir and not (body.get("memdb") or "").strip():
                custom["memdb"] = ""

            # 先自动备份，再动
            try:
                make_backup("pre-import")
            except Exception as e:
                LOG.warn("import", f"备份失败: {e}")

            rep, out = mover.import_all(data_dir, paths=custom or None, write=True,
                                        st_dir=(load_config().get("st_dir") or ""),
                                        prefer=(load_config().get("name") or ""))
            if not rep.get("ok"):
                return self._json(200, {"ok": False, "report": rep})

            # 同步导入聊天
            added = 0
            if rep.get("memdb") and os.path.exists(rep["memdb"]):
                added, total = store.import_from_st(rep["memdb"])
            else:
                total = store.count()

            chats_added, chats_total, cid = 0, store.count_all(), None
            if chats_dir:
                files = mover.find_chats(chats_dir)
                if files:
                    chats_added, chats_total, cid = mover.import_jsonl(store, files, chat_title)

            _index = None
            _vec = None
            _boost = None
            _nv = None

            cfg = load_config()
            if rep.get("name"):
                cfg["name"] = rep["name"]
            cfg["setup_done"] = True
            save_config(cfg)

            # 用卡里的开场白做第一条（只有在库为空时）
            vrep = verify_import()
            LOG.info("import", f"校验 ok={vrep['ok']} 条数={vrep['count']} 问题={vrep['problems']}")
            return self._json(200, {
                "ok": True, "report": rep, "added": added, "total": total,
                "chats_added": chats_added, "chats_total": chats_total, "chat_id": cid,
                "verify": vrep,
                "name": cfg.get("name"), "files": rep.get("files"),
            })

        if p == "/api/import":
            _std = (load_config().get("st_dir") or os.environ.get("COMPANION_ST_DIR") or "").strip()
            src = body.get("st_db") or (os.path.join(_std, "_memory", "memory.db") if _std else "")
            if not os.path.exists(src):
                return self._json(400, {"error": "找不到来源数据库"})
            added, total = store.import_from_st(src)
            _index = None
            _vec = None
            _boost = None
            _nv = None
            return self._json(200, {"ok": True, "added": added, "total": total})

        if p == "/api/proactive":
            cfg = load_config()
            do, why = pro_mod.should_speak(store, cfg)
            if body.get("force"):
                do, why = True, "force"
            if not do:
                return self._json(200, {"ok": False, "reason": why})
            st = st_mod.compute_state(store)
            block = st_mod.format_state_block(st, store.recent_diary(1))
            text = pro_mod.speak(store, cfg, stream_chat, block)
            if text:
                notify(cfg.get("name", "她"), text[:120])
            return self._json(200, {"ok": bool(text), "text": text or "", "reason": why})

        if p == "/api/diary":
            cfg = load_config()
            day = body.get("day")
            ok, out = diary_mod.generate_diary(store, cfg, stream_chat, day=day,
                                               force=bool(body.get("force")))
            return self._json(200, {"ok": ok, "text": out if ok else "", "error": "" if ok else out,
                                    "items": store.recent_diary(7)})

        if p == "/api/setup":
            return self._setup(body)

        if p == "/api/delete":
            mid = int(body.get("id") or 0)
            if mid:
                store.delete_msg(mid)
                _index = None
                _vec = None
                _boost = None
                _nv = None
            return self._json(200, {"ok": True})

        if p == "/api/edit":
            mid = int(body.get("id") or 0)
            text = (body.get("text") or "").strip()
            if mid and text:
                store.update_msg(mid, text)
                _index = None
                _vec = None
                _boost = None
                _nv = None
            return self._json(200, {"ok": bool(mid and text)})

        if p == "/api/chat/new":
            cid = store.new_chat((body.get("title") or "").strip() or None)
            return self._json(200, {"ok": True, "chat": cid})

        if p == "/api/background":
            cfg = load_config()
            if body.get("reset"):
                cfg["bg_color"] = "#111318"
                cfg["bg_image"] = ""
                cfg["bg_dim"] = 0
                save_config(cfg)
                return self._json(200, {"ok": True, "reset": True})

            if "color" in body:
                c = str(body["color"]).strip()
                if c.startswith("#") and len(c) in (4, 7):
                    cfg["bg_color"] = c
            if "dim" in body:
                try:
                    cfg["bg_dim"] = max(0, min(80, int(body["dim"])))
                except Exception:
                    pass
            data_url = body.get("data_url")
            if isinstance(data_url, str) and data_url.startswith("data:image/"):
                try:
                    import base64
                    head, b64 = data_url.split(",", 1)
                    ext = ".png" if "png" in head else (".webp" if "webp" in head else ".jpg")
                    raw = base64.b64decode(b64)
                    if len(raw) > 8 * 1024 * 1024:
                        return self._json(400, {"error": "图片太大（限 8 MB）"})
                    bdir = os.path.join(DATA, "backgrounds")
                    os.makedirs(bdir, exist_ok=True)
                    for f in os.listdir(bdir):
                        try:
                            os.remove(os.path.join(bdir, f))
                        except Exception:
                            pass
                    name = "bg" + ext
                    with open(os.path.join(bdir, name), "wb") as f:
                        f.write(raw)
                    cfg["bg_image"] = name
                except Exception as e:
                    return self._json(400, {"error": f"图片保存失败: {e}"})
            if body.get("clear_image"):
                cfg["bg_image"] = ""
            save_config(cfg)
            return self._json(200, {"ok": True, "color": cfg.get("bg_color"),
                                    "image": cfg.get("bg_image"), "dim": cfg.get("bg_dim", 0)})

        if p == "/api/chat/rename":
            cid = (body.get("id") or "").strip()
            title = (body.get("title") or "").strip()[:30]
            if cid and title:
                store.con.execute("UPDATE chats SET title=? WHERE id=?", (title, cid))
                store.con.commit()
            return self._json(200, {"ok": True})

        if p == "/api/chat/delete":
            cid = (body.get("id") or "").strip()
            if cid and cid != "main":
                store.con.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
                store.con.execute("DELETE FROM chats WHERE id=?", (cid,))
                store.con.commit()
                if store.current_chat() == cid:
                    store.set_chat("main")
                _index = None
                _vec = None
            return self._json(200, {"ok": True, "chat": store.current_chat()})

        if p == "/api/chat/switch":
            cid = (body.get("id") or "").strip()
            if cid:
                store.set_chat(cid)
            return self._json(200, {"ok": True, "chat": store.current_chat()})

        if p == "/api/regen":
            tail = store.tail()
            if tail and tail[0][1] == "char":
                store.delete_msg(tail[0][0])
                _index = None
                _vec = None
                _boost = None
                _nv = None
            tail = store.tail()
            if not tail or tail[0][1] != "user":
                return self._json(200, {"ok": False, "error": "没有可重答的用户消息"})
            return self._chat({"text": tail[0][2]}, skip_user=True)

        if p == "/api/memory":
            cfg = load_config()
            what = (body.get("what") or "").strip()
            out = {}
            if what in ("", "summary"):
                ok, res = mem2.make_summary(store, cfg, stream_chat, force=True)
                out["summary"] = {"ok": ok, "result": res}
            if what in ("", "facts"):
                ok, res = mem2.extract_facts(store, cfg, stream_chat, force=True)
                out["facts"] = {"ok": ok, "result": res}
            if what == "week":
                ok, res = mem2.make_weekly(store, cfg, stream_chat)
                out["week"] = {"ok": ok, "result": res}
            if what == "sweep":
                out["archived"] = mem2.sweep_facts(store.con)
            out["ok"] = True
            return self._json(200, out)

        if p == "/api/world":
            cfg = load_config()
            if not cfg.get("api_key"):
                return self._json(400, {"error": "没填 API Key"})
            text, why = world_mod.build_world_text(cfg, stream_chat)
            if text:
                store.set_state("world_text", text)
                store.set_state("world_date", datetime.now().strftime("%Y-%m-%d"))
                return self._json(200, {"ok": True, "text": text})
            return self._json(200, {"ok": False, "reason": why})

        if p == "/api/chat":
            return self._chat(body)

        return self._json(404, {"error": "not found"})

    # ---------- 首次设置 ----------
    def _setup(self, body):
        import re as _re

        if body.get("blank"):
            for fn in ("persona.md", "world.md", "user.md", "rules.md"):
                open(os.path.join(DATA, fn), "w", encoding="utf-8").write("")
            cfg = load_config()
            cfg["setup_done"] = True
            save_config(cfg)
            return self._json(200, {"ok": True, "blank": True})
        ch = body.get("char") or {}
        wo = body.get("world") or {}
        us = body.get("user") or {}
        ru = body.get("rules") or {}

        name = (ch.get("name") or "她").strip()[:20]
        user_name = (us.get("name") or "我").strip()[:20]

        persona = []
        persona.append(f"# {name}")
        for key, label in (("identity", "身份"), ("personality", "性格"),
                           ("style", "说话方式"), ("appearance", "外貌")):
            v = (ch.get(key) or "").strip()
            if v:
                persona.append(f"## {label}\n{v}")
        if (ch.get("call_user") or "").strip():
            persona.append(f"## 称呼\n她叫{{{{user}}}}：{ch['call_user'].strip()}")
        if (ch.get("example") or "").strip():
            persona.append(f"## 对话示例\n{ch['example'].strip()}")
        open(os.path.join(DATA, "persona.md"), "w", encoding="utf-8").write("\n\n".join(persona))

        wparts = []
        for key, label in (("genre", "世界类型"), ("era", "时代与地点"),
                           ("rules", "世界规则"), ("taboos", "禁忌与底线"),
                           ("terms", "专有名词")):
            v = (wo.get(key) or "").strip()
            if v:
                wparts.append(f"## {label}\n{v}")
        open(os.path.join(DATA, "world.md"), "w", encoding="utf-8").write(
            ("# 世界观\n\n" + "\n\n".join(wparts)) if wparts else "")

        uparts = []
        for key, label in (("age", "年龄与身份"), ("routine", "作息与习惯"),
                           ("care", "在意的事"), ("pressure", "高压区（别追问）"),
                           ("likes", "喜欢被怎样对待"), ("dislikes", "不喜欢什么")):
            v = (us.get(key) or "").strip()
            if v:
                uparts.append(f"## {label}\n{v}")
        open(os.path.join(DATA, "user.md"), "w", encoding="utf-8").write(
            ("# 主人档案\n\n" + "\n\n".join(uparts)) if uparts else "")

        rparts = []
        for key, label in (("time", "时间"), ("memory", "记忆使用"), ("boundary", "边界")):
            v = (ru.get(key) or "").strip()
            if v:
                rparts.append(f"## {label}\n{v}")
        open(os.path.join(DATA, "rules.md"), "w", encoding="utf-8").write(
            ("# 规则\n\n" + "\n\n".join(rparts)) if rparts else "")

        try:
            with open(os.path.join(DATA, "setup.json"), "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        cfg = load_config()
        cfg["name"] = name
        cfg["user_name"] = user_name
        cfg["setup_done"] = True
        save_config(cfg)

        greeting = (ch.get("greeting") or "").strip()
        if greeting and store.count() == 0:
            store.append("char", greeting)

        return self._json(200, {"ok": True, "name": name, "user_name": user_name})

    # ---------- 聊天（SSE） ----------
    def _chat(self, body, skip_user=False):
        cfg = load_config()
        text = (body.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "empty"})
        if not cfg.get("api_key"):
            return self._json(400, {"error": "没填 API Key（右上角设置）"})

        if not skip_user:
            store.append("user", text)
            try:
                rel_mod.update_on_message(store.con, text)
                n_rep = rel_mod.try_resolve(store.con, text)
                if n_rep:
                    LOG.info("rel", f"和解，清掉 {n_rep} 条旧账")
                elif rel_mod.detect_conflict(text) >= 1 and len(text) <= 80:
                    strong = any(w in text for w in ("你骗我", "你根本", "不想理你", "有什么资格", "你算什么"))
                    sev = 2 if strong else 1
                    rel_mod.add_grudge(store.con, text, sev)
                    LOG.info("rel", f"记了一笔（严重度 {sev}）：{text[:30]}")
            except Exception as e:
                LOG.error("rel", f"关系更新失败: {e}", e)
        try:
            store.con.execute("INSERT INTO timeline(ts,note) VALUES(?,?)",
                              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "他说了话"))
            store.con.commit()
        except Exception:
            pass
        lookup, terms, raw = None, [], ""
        if cfg.get("self_lookup", True):
            # 先用便宜的门槛：不像在问过去、也不是问句 → 不问模型，直接不查
            PAST_HINT = ("记得", "之前", "上次", "以前", "那次", "哪天", "那天",
                         "说过", "聊过", "提过", "还记得", "回忆", "你记",
                         "什么", "怎么", "哪", "多少", "几", "是不是", "有没有",
                         "为什么", "如何", "还", "曾经")
            looks_past = any(k in text for k in PAST_HINT)
            is_question = text.strip().endswith(("吗", "呢", "?", "？", "么", "了", "的"))
            if looks_past or is_question:
                lookup, terms, raw = selfquery.decide(cfg, text, stream_chat)
            else:
                lookup, raw = False, "heuristic:no"
            try:
                LOG.info("selfquery", f"{'查' if lookup else ('不查' if lookup is False else '回退')} terms={terms} raw={raw}")
            except Exception:
                pass

        msgs = build_messages(cfg, text, lookup=lookup, lookup_terms=terms)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        def emit(obj):
            self.wfile.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        t_start = time.time()
        try:
            r_info = ""
            for m in msgs:
                if m.get("role") == "system" and "[长期记忆检索结果" in str(m.get("content", "")):
                    r_info = f"证据 {len(m['content'])} 字"
            LOG.info("chat", f"in={len(text)}字 历史={len(msgs)-3}条 {r_info}")
        except Exception:
            pass

        buf = []
        want_reason = bool(cfg.get("show_thinking", False))
        reason_t0 = None
        reason_chars = 0
        try:
            for piece in stream_chat(
                    msgs,
                    base_url=cfg["base_url"],
                    api_key=cfg["api_key"],
                    model=cfg["model"],
                    temperature=float(cfg.get("temperature", 1.1)),
                    max_tokens=int(cfg.get("max_tokens", 600)),
                    no_thinking=bool(cfg.get("no_thinking", True)) and not want_reason,
                    with_reasoning=want_reason):
                if want_reason and isinstance(piece, dict):
                    if piece["t"] == "reason":
                        if reason_t0 is None:
                            reason_t0 = time.time()
                        reason_chars += len(piece["v"])
                        emit({"reason": piece["v"]})
                        continue
                    piece = piece["v"]
                buf.append(piece)
                emit({"delta": piece})
            reply = clean_reply("".join(buf))
            if reply:
                store.append("char", reply)
                try:
                    tin = sum(len(str(m.get("content", ""))) for m in msgs)
                    add_usage(int(tin * 0.7), int(len(reply) * 0.7) + 20)
                    LOG.info("chat", f"out={len(reply)}字 总耗时={int((time.time()-t_start)*1000)}ms"
                           + (f" 思考={reason_chars}字" if reason_chars else ""))
                except Exception:
                    pass
            emit({"done": True})
        except Exception as e:
            LOG.error("chat", f"生成失败: {e}", e)
            emit({"error": str(e)})


def diary_worker():
    """每 10 分钟看一次：到点且今天没写，就写一篇"""
    import time as _time
    while True:
        try:
            cfg = load_config()
            if cfg.get("diary_enabled", True) and cfg.get("api_key"):
                now = datetime.now()
                if now.hour >= int(cfg.get("diary_hour", 23)):
                    day = now.strftime("%Y-%m-%d")
                    row = store.con.execute("SELECT 1 FROM diary WHERE day=?", (day,)).fetchone()
                    if not row:
                        ok, out = diary_mod.generate_diary(store, cfg, stream_chat, day=day)
                        LOG.info("diary", f"{day}: {'ok' if ok else out}")
        except Exception as e:
            LOG.error("diary", f"失败: {e}", e)
        _time.sleep(600)


def proactive_worker():
    """每 N 分钟检查一次：该不该主动开口"""
    import time as _time
    while True:
        interval = 20
        try:
            cfg = load_config()
            interval = max(5, int(cfg.get("proactive_interval_min", 20)))
            if cfg.get("proactive_enabled") and cfg.get("api_key"):
                do, why = pro_mod.should_speak(store, cfg)
                if do:
                    st = st_mod.compute_state(store)
                    block = st_mod.format_state_block(st, store.recent_diary(1))
                    text = pro_mod.speak(store, cfg, stream_chat, block)
                    LOG.info("proactive", f"{why} -> {'说了' if text else '没说'}")
                    if text:
                        notify(cfg.get("name", "她"), text[:120])
        except Exception as e:
            LOG.error("proactive", f"失败: {e}", e)
        _time.sleep(interval * 60)


def world_worker():
    """每小时看一眼：到点且今天还没抓，就抓一次"""
    import time as _time
    while True:
        try:
            cfg = load_config()
            if cfg.get("world_enabled") and cfg.get("api_key"):
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                if now.hour >= int(cfg.get("world_hour", 7)) and store.get_state("world_date") != today:
                    text, why = world_mod.build_world_text(cfg, stream_chat)
                    if text:
                        store.set_state("world_text", text)
                        store.set_state("world_date", today)
                        LOG.info("world", f"{today} 已更新")
                    else:
                        LOG.info("world", f"跳过: {why}")
        except Exception as e:
            LOG.error("world", f"失败: {e}", e)
        _time.sleep(3600)


def backup_worker():
    """每天一次，把 data 目录压成 zip，留最近 7 个"""
    import time as _time
    import zipfile
    bdir = os.path.join(DATA, "backups")
    while True:
        try:
            cfg = load_config()
            if cfg.get("backup_enabled", True):
                os.makedirs(bdir, exist_ok=True)
                day = datetime.now().strftime("%Y%m%d")
                target = os.path.join(bdir, f"companion-{day}.zip")
                if not os.path.exists(target) and datetime.now().hour >= int(cfg.get("backup_hour", 4)):
                    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                        for root, _dirs, files in os.walk(DATA):
                            if "backups" in root:
                                continue
                            for fn in files:
                                p = os.path.join(root, fn)
                                z.write(p, os.path.relpath(p, DATA))
                    LOG.info("backup", f"{os.path.basename(target)} ok")
                    olds = sorted(f for f in os.listdir(bdir) if f.endswith(".zip"))
                    for f in olds[:-7]:
                        os.remove(os.path.join(bdir, f))
        except Exception as e:
            LOG.error("backup", f"失败: {e}", e)
        _time.sleep(1800)


def memory_worker():
    """摘要 / 事实抽取 / 时间轴 / 遗忘"""
    import time as _time
    ticks = 0
    while True:
        try:
            cfg = load_config()
            if cfg.get("api_key") and cfg.get("memory_enabled", True):
                cid = store.current_chat() or "main"
                ok, res = mem2.need_summary(store.con, cid, every=int(cfg.get("summary_every", 40)))
                if ok:
                    good, out = mem2.make_summary(store, cfg, stream_chat)
                    LOG.info("memory", f"摘要 {'ok' if good else out}")
                if ticks % 3 == 0:
                    good, out = mem2.extract_facts(store, cfg, stream_chat)
                    if good:
                        LOG.info("memory", f"事实 {out}")
                mem2.sample_timeline(store.con, pc_idle=pc_idle_minutes())
                if ticks % 72 == 0:
                    n = mem2.sweep_facts(store.con)
                    if n:
                        LOG.info("memory", f"归档 {n} 条快忘光的事实")
                if datetime.now().weekday() == 6 and datetime.now().hour >= 22 and ticks % 6 == 0:
                    good, out = mem2.make_weekly(store, cfg, stream_chat)
                    if good:
                        LOG.info("memory", "周摘要已生成")
                    good, out = mem2.make_month(store, cfg, stream_chat)
                    if good:
                        LOG.info("memory", "月摘要已生成")
                    good, out = mem2.make_quarter(store, cfg, stream_chat)
                    if good:
                        LOG.info("memory", "季摘要已生成")
                if False:
                    n = 0
                    if n:
                        LOG.info("memory", f"归档 {n} 条快忘光的事实")
                ticks += 1
        except Exception as e:
            LOG.error("memory", f"失败: {e}", e)
        _time.sleep(600)


def install_signals(srv):
    import signal

    def bye(signum, frame):
        try:
            LOG.info("app", f"收到信号 {signum}，正在退出")
        except Exception:
            pass
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            store.close()
        except Exception:
            pass
        release_lock()
        os._exit(0)

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None),
                getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, bye)
            except Exception:
                pass


def main():
    global HOST
    try:
        mem2.ensure_schema(store.con)
        rel_mod.ensure_schema(store.con)
    except Exception as e:
        print("schema:", e)
    os.makedirs(DATA, exist_ok=True)
    threading.Thread(target=diary_worker, daemon=True).start()
    threading.Thread(target=proactive_worker, daemon=True).start()
    threading.Thread(target=world_worker, daemon=True).start()
    threading.Thread(target=memory_worker, daemon=True).start()
    threading.Thread(target=backup_worker, daemon=True).start()
    threading.Thread(target=neural_warmup, daemon=True).start()
    threading.Thread(target=warm_vec, daemon=True).start()

    hosts = ["127.0.0.1"]
    ok, old = acquire_lock()
    if not ok:
        msg = f"已经在跑了（PID {old}），直接帮你打开。"
        print(msg)
        try:
            LOG.warn("app", msg)
        except Exception:
            pass
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}/?again=1")
        except Exception:
            pass
        time.sleep(4)
        sys.exit(0)

    ts = pick_host()
    if ts and ts != "127.0.0.1":
        hosts.append(ts)

    servers = []
    for h in hosts:
        try:
            srv = ThreadingHTTPServer((h, PORT), Handler)
            srv.daemon_threads = True
            servers.append((h, srv))
        except OSError as e:
            print(f"绑定 {h}:{PORT} 失败（{e}）")

    if not servers:
        print("没有可用的监听地址，退出")
        return

    install_signals(servers[0][1])

    try:
        for _f, _lim in BLOCK_LIMITS.items():
            _raw = read_block(_f)
            _st = "完整" if len(_raw) <= _lim else f"被截断({len(_raw)}>{_lim})"
            LOG.info("memory", f"{_f}: {len(_raw)} 字 {_st}")
    except Exception:
        pass
    LOG.info("app", f"启动 v{APP_VERSION} 数据目录={DATA}")
    if getattr(sys, "frozen", False):
        LOG.redirect_std()
    try:
        if os.name == "nt":
            os.system(f"title {load_config().get('name', '陪伴')} · 关掉这个窗口就是关掉她")
    except Exception:
        pass
    print("Companion Lite 已启动")
    for h, _ in servers:
        label = "本机访问" if h == "127.0.0.1" else "手机访问"
        print(f"  {label}:   http://{h}:{PORT}")
    print(f"  数据目录:   {DATA}")

    if os.environ.get("COMPANION_NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()

    for _, srv in servers[1:]:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    servers[0][1].serve_forever()


if __name__ == "__main__":
    main()
