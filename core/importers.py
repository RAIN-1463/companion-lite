# -*- coding: utf-8 -*-
"""通用对话导入：粘贴 / QQ式 txt / 通用 jsonl

统一输出：[{"ts": "YYYY-MM-DD HH:MM:SS", "speaker": "user"|"char", "text": "..."}]

设计原则
- 不依赖任何平台 SDK，纯文本解析
- 先预览再入库；入库时按 (ts, speaker, text) 去重
- 解析失败不清空、不丢输入
"""
import json
import re
from datetime import datetime, timedelta

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 常见时间前缀（可带方括号/括号）
_DATE_RE = re.compile(
    r"^\s*[\[\(]?\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?\s*[\)\]]?\s*"
    r"[T\s]*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*[\)\]]?\s*")
# 「名字：内容」/「名字: 内容」/「[名字] 内容」
_NAME_RE = re.compile(r"^\s*[\[【]?([^\[\]【】:：\s]{1,16})[\]】]?\s*[:：]\s*(.*)$")


def _norm_ts(m):
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh, mm = int(m.group(4)), int(m.group(5))
    ss = int(m.group(6) or 0)
    try:
        return datetime(y, mo, d, hh, mm, ss).strftime(TS_FMT)
    except Exception:
        return None


def _mk_seq(start_ts=None):
    """没有时间戳时，造一串递增时间，保证顺序"""
    base = datetime.strptime(start_ts, TS_FMT) if start_ts else datetime.now().replace(
        hour=8, minute=0, second=0, microsecond=0)
    n = [0]

    def nxt():
        n[0] += 1
        return (base + timedelta(seconds=30 * n[0])).strftime(TS_FMT)
    return nxt


def _who(name, her, me):
    name = (name or "").strip()
    if not name:
        return None
    if her and (her in name or name in her):
        return "char"
    if me and (me in name or name in me):
        return "user"
    # 常见称呼兜底
    if name in ("我", "本人", "主人"):
        return "user"
    return "user"


def parse_paste(text, her_name="", me_name=""):
    """粘贴格式：可选时间前缀 + 「名字：内容」，或纯对话逐行"""
    out = []
    last_ts = [None]

    def nxt_ts():
        if last_ts[0] is None:
            last_ts[0] = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            last_ts[0] = last_ts[0] + timedelta(seconds=30)
        return last_ts[0].strftime(TS_FMT)

    last = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if last is not None:
                out[last]["text"] += "\n"
            continue
        ts = None
        m = _DATE_RE.match(line)
        if m:
            ts = _norm_ts(m)
            line = line[m.end():]
            if not line.strip():
                continue
        nm = _NAME_RE.match(line)
        if nm:
            who = _who(nm.group(1), her_name, me_name)
            body = nm.group(2).strip()
            if who:
                if ts:
                    try:
                        last_ts[0] = datetime.strptime(ts, TS_FMT)
                    except Exception:
                        pass
                out.append({"ts": ts or nxt_ts(), "speaker": who, "text": body})
                last = len(out) - 1
                continue
        if last is not None:
            out[last]["text"] += "\n" + line.strip()
        else:
            out.append({"ts": ts or nxt_ts(), "speaker": "user", "text": line.strip()})
            last = len(out) - 1
    for m in out:
        m["text"] = m["text"].strip()
    return [m for m in out if m["text"]]


def parse_txt(text, her_name="", me_name=""):
    """QQ 导出的 txt：一行『时间 昵称』做头，后面若干行是内容"""
    out = []
    seq = _mk_seq()
    cur = None
    header = re.compile(
        r"^\s*(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})日?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+?)\s*$")
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        hm = header.match(line)
        if hm:
            ts = _norm_ts(hm)
            name = hm.group(7)
            name = re.sub(r"[<（(].*?[>）)]", "", name).strip()
            who = _who(name, her_name, me_name)
            cur = {"ts": ts or seq(), "speaker": who, "text": ""}
            out.append(cur)
            continue
        if cur is not None:
            cur["text"] += (("\n" if cur["text"] else "") + line)
        elif line.strip():
            out.append({"ts": seq(), "speaker": "user", "text": line.strip()})
            cur = out[-1]
    for m in out:
        m["text"] = m["text"].strip()
    return [m for m in out if m["text"]]


def parse_jsonl(text, her_name="", me_name=""):
    """通用 jsonl / json 数组；兼容 role/content、is_user/mes、sender/text"""
    items = []
    txt = (text or "").strip()
    if not txt:
        return []
    if txt[0] in "[{":
        try:
            obj = json.loads(txt)
            items = obj if isinstance(obj, list) else [obj]
        except Exception:
            items = []
    if not items:
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except Exception:
                continue

    out = []
    seq = _mk_seq()
    for it in items:
        if not isinstance(it, dict):
            continue
        # 嵌套：{"message": {...}}
        if "message" in it and isinstance(it["message"], dict):
            it = it["message"]
        content = it.get("content") or it.get("mes") or it.get("text") \
            or it.get("message") or it.get("msg")
        if isinstance(content, list):      # ChatGPT 多段
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            continue
        if "is_user" in it:
            who = "char" if not it.get("is_user") else "user"
        elif str(it.get("role", "")).lower() in ("user", "human", "me"):
            who = "user"
        elif str(it.get("role", "")).lower() in ("assistant", "ai", "bot", "char"):
            who = "char"
        else:
            who = _who(it.get("name") or it.get("sender") or "", her_name, me_name)
        ts = it.get("ts") or it.get("time") or it.get("timestamp") or it.get("send_date")
        if isinstance(ts, str):
            m = _DATE_RE.match(ts) or _DATE_RE.match("2020-01-01 00:00:00 " + ts)
            ts = _norm_ts(m) if m else None
            if not ts:
                try:
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime(TS_FMT)
                except Exception:
                    ts = None
        elif isinstance(ts, (int, float)):
            try:
                v = ts / 1000.0 if ts > 1e11 else ts
                ts = datetime.fromtimestamp(v).strftime(TS_FMT)
            except Exception:
                ts = None
        out.append({"ts": ts or seq(), "speaker": who, "text": content.strip()})
    return out


PARSERS = {
    "paste": parse_paste,
    "txt": parse_txt,
    "jsonl": parse_jsonl,
}


def parse(source, text, her_name="", me_name=""):
    fn = PARSERS.get(source)
    if not fn:
        raise ValueError("不认识的来源: " + str(source))
    rows = fn(text, her_name, me_name)
    rows.sort(key=lambda r: r["ts"])
    return rows
