# -*- coding: utf-8 -*-
"""
一键搬家：从 SillyTavern 把角色卡 + 世界书 + 全部聊天搬到本客户端。

**全程只读**：只打开 ST 的文件读取，绝不写入、绝不改属性。
输出只写到本项目的 data/ 目录。
"""
import base64
import json
import os
from datetime import datetime
import re

def _first(d, exts):
    try:
        for n in sorted(os.listdir(d)):
            if n.lower().endswith(exts):
                return os.path.join(d, n)
    except Exception:
        pass
    return ""


def _pick(d, exts, prefer=""):
    files = []
    try:
        files = [n for n in sorted(os.listdir(d)) if n.lower().endswith(exts)]
    except Exception:
        return ""
    if prefer:
        for n in files:
            if prefer in n:
                return os.path.join(d, n)
    return os.path.join(d, files[0]) if files else ""


def defaults(st_dir="", prefer=""):
    """酒馆目录 → 默认文件路径；没配就返回空（由界面填写）"""
    st = (st_dir or os.environ.get("COMPANION_ST_DIR") or "").strip()
    if not st:
        return {"card": "", "world": "", "memdb": "", "chats": ""}
    du = os.path.join(st, "data", "default-user")
    return {
        "card": _pick(os.path.join(du, "characters"), (".png", ".json"), prefer),
        "world": _pick(os.path.join(du, "worlds"), (".json",), prefer),
        "memdb": os.path.join(st, "_memory", "memory.db"),
        "chats": os.path.join(du, "chats"),
    }


def _read_png_text(path):
    """纯标准库读 PNG 里的 tEXt 块（酒馆角色卡把 JSON 存在这里）"""
    import struct, base64, json
    out = {}
    try:
        data = open(path, 'rb').read()
        if data[:8] != b'\x89PNG\r\n\x1a\n':
            return out
        pos = 8
        while pos + 8 <= len(data):
            ln = struct.unpack('>I', data[pos:pos + 4])[0]
            typ = data[pos + 4:pos + 8].decode('latin-1', 'ignore')
            body = data[pos + 8:pos + 8 + ln]
            if typ == 'tEXt':
                key, _, val = body.partition(b'\x00')
                k = key.decode('latin-1', 'ignore')
                try:
                    out[k] = base64.b64decode(val).decode('utf-8')
                except Exception:
                    pass
            elif typ == 'iTXt':
                parts = body.split(b'\x00', 3)
                if len(parts) == 4:
                    k = parts[0].decode('latin-1', 'ignore')
                    try:
                        out[k] = parts[3].decode('utf-8')
                    except Exception:
                        pass
            pos += 12 + ln
            if typ == 'IEND':
                break
    except Exception:
        pass
    return out


def _read_card_text(path):
    """从 PNG 的 chara 块读出角色卡字段（只读，不需要 Pillow）"""
    raw = None
    try:
        texts = _read_png_text(path)
        raw = texts.get("chara") or texts.get("ccv3")
    except Exception:
        raw = None
        texts = {}
    if not raw:
        try:
            from PIL import Image
            img = Image.open(path)
            raw = img.info.get("chara") or img.info.get("ccv3")
            img.close()
        except Exception:
            raw = None
    if not raw:
        return {}
    try:
        data = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(
            base64.b64decode(raw).decode("utf-8"))
    except Exception:
        return {}
    return data.get("data", data)


def _read_world(path):
    with open(path, encoding="utf-8") as f:
        w = json.load(f)
    entries = []
    for uid, e in (w.get("entries") or {}).items():
        entries.append({
            "uid": uid,
            "comment": (e.get("comment") or "").strip(),
            "content": (e.get("content") or "").strip(),
            "key": e.get("key") or [],
            "constant": bool(e.get("constant")),
            "disable": bool(e.get("disable")),
        })
    return entries


def _classify(entries):
    """把世界书条目分到 rules / user / world 三类"""
    rules, user, world = [], [], []
    for e in entries:
        if e["disable"] or not e["content"]:
            continue
        c = e["comment"]
        if "时间" in c:
            rules.append(("时间", e["content"]))
        elif "记忆" in c and ("规则" in c or "使用" in c):
            rules.append(("记忆使用", e["content"]))
        elif "边界" in c or "关系" in c:
            rules.append(("关系与边界", e["content"]))
        elif c.startswith("守则·"):
            rules.append((c.replace("守则·", "应对："), e["content"]))
        elif "档案" in c:
            user.append(("档案", e["content"]))
        elif c.startswith("记忆·"):
            user.append((c.replace("记忆·", ""), e["content"]))
        elif c.startswith("事件·"):
            user.append((c.replace("事件·", "记得的事："), e["content"]))
        else:
            world.append((c, e["content"]))
    return rules, user, world


def build_persona(card):
    name = card.get("name") or "她"
    parts = [f"# {name}"]
    desc = (card.get("description") or "").strip()
    pers = (card.get("personality") or "").strip()
    scen = (card.get("scenario") or "").strip()
    ex = (card.get("mes_example") or "").strip()

    if desc:
        parts.append("## 设定\n" + desc)
    if pers:
        parts.append("## 性格\n" + pers)
    if scen:
        parts.append("## 场景\n" + scen)
    if ex:
        parts.append("## 对话示例\n" + ex)
    return "\n\n".join(parts), name


def import_all(data_dir, paths=None, write=True, st_dir=""):
    """执行搬家。返回 (报告 dict, 各文件内容 dict)"""
    p = dict(defaults(st_dir))
    if paths:
        p.update(paths)

    report = {"ok": False, "notes": []}
    out = {}

    # 1) 角色卡
    card = {}
    if os.path.exists(p["card"]):
        try:
            card = _read_card_text(p["card"])
            persona, name = build_persona(card)
            out["persona.md"] = persona
            report["name"] = name
            report["notes"].append(f"角色卡：读到 {len(card)} 个字段")
        except Exception as e:
            report["notes"].append(f"角色卡读取失败：{e}")
    else:
        report["notes"].append("角色卡不存在，跳过")

    # 2) 世界书
    entries = []
    if os.path.exists(p["world"]):
        try:
            entries = _read_world(p["world"])
            rules, user, world = _classify(entries)
            if world:
                out["world.md"] = "# 世界观\n\n" + "\n\n".join(
                    f"## {c}\n{t}" for c, t in world)
            if user:
                out["user.md"] = "# 主人档案\n\n" + "\n\n".join(
                    f"## {c}\n{t}" for c, t in user)
            if rules:
                out["rules.md"] = "# 规则\n\n" + "\n\n".join(
                    f"## {c}\n{t}" for c, t in rules)
            report["notes"].append(
                f"世界书：{len(entries)} 条 → 规则 {len(rules)} / 档案 {len(user)} / 世界 {len(world)}")
        except Exception as e:
            report["notes"].append(f"世界书读取失败：{e}")
    else:
        report["notes"].append("世界书不存在，跳过")

    # 3) 聊天
    if os.path.exists(p["memdb"]):
        report["memdb"] = p["memdb"]
    else:
        report["notes"].append("记忆库不存在，跳过历史")

    if write:
        os.makedirs(data_dir, exist_ok=True)
        for name, text in out.items():
            with open(os.path.join(data_dir, name), "w", encoding="utf-8") as f:
                f.write(text)
        # 把人设原始字段也存一份，方便以后"重新设置"回填
        if card:
            raw = {"char": {
                "name": card.get("name", ""),
                "identity": (card.get("description") or "")[:400],
                "personality": card.get("personality", ""),
                "example": card.get("mes_example", "")[:800],
            }}
            with open(os.path.join(data_dir, "setup.json"), "w", encoding="utf-8") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2)

    report["ok"] = bool(out)
    report["files"] = list(out.keys())
    return report, out


def find_chats(chats_dir):
    """找出酒馆聊天目录下的所有 jsonl"""
    if not chats_dir or not os.path.isdir(chats_dir):
        return []
    out = []
    for root, _dirs, files in os.walk(chats_dir):
        for fn in files:
            if fn.lower().endswith(".jsonl"):
                out.append(os.path.join(root, fn))
    out.sort(key=lambda p: os.path.getmtime(p))
    return out


def _parse_send_date(raw):
    """支持两种：2026-10-29@20:15:33.123  和  2026-10-29T14:32:26.873Z"""
    raw = str(raw or "").strip()
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})@(\d{2}:\d{2}:\d{2})", raw)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    try:
        s2 = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def import_jsonl(store, paths, chat_title=None):
    """把一个或多个酒馆 jsonl 读进客户端；返回 (added, total, chat_id)"""
    import json as _json

    title = (chat_title or "").strip() or "从酒馆导入"
    cid = store.new_chat(title)
    rows = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = _json.loads(line)
                    except Exception:
                        continue
                    mes = (o.get("mes") or "").strip()
                    if not mes or "chat_metadata" in o:
                        continue
                    ts = _parse_send_date(o.get("send_date"))
                    if not ts:
                        continue
                    sp = "user" if o.get("is_user") else "char"
                    rows.append((ts, sp, mes, cid))
        except Exception:
            continue

    before = store.count_all()
    if rows:
        store.con.executemany(
            "INSERT OR IGNORE INTO messages(ts,speaker,text,chat_id) VALUES(?,?,?,?)", rows)
        store.con.commit()
        store.rebuild_grams()
    return store.count_all() - before, store.count_all(), cid
