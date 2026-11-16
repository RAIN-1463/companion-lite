# -*- coding: utf-8 -*-
"""
把客户端的对话导出成酒馆能读的 .jsonl

酒馆格式：
  第一行（头）: {"user_name":..,"character_name":..,"create_date":..,"talkativeness":"0.5","chat_metadata":{}}
  之后每条:    {"name":..,"is_user":..,"send_date":"2026-10-29T14:32:26.873Z","mes":..,"extra":{}}
"""
import json
import os
import re
import zipfile
from datetime import datetime, timedelta


def _iso(ts):
    """客户端 'YYYY-MM-DD HH:MM:SS' → 酒馆 'YYYY-MM-DDTHH:MM:SS.000Z'（按本地时区转 UTC）"""
    try:
        dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        dt = dt - timedelta(hours=8)          # 本地 +8 → UTC
        return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_jsonl(store, chat_id, char_name="她", user_name="我"):
    rows = store.con.execute(
        "SELECT ts,speaker,text FROM messages WHERE chat_id=? ORDER BY ts,id", (chat_id,)
    ).fetchall()
    if not rows:
        return "", 0

    head = {
        "user_name": user_name,
        "character_name": char_name,
        "create_date": _iso(rows[0][0]),
        "talkativeness": "0.5",
        "chat_metadata": {},
    }
    lines = [json.dumps(head, ensure_ascii=False)]
    for ts, sp, text in rows:
        lines.append(json.dumps({
            "name": user_name if sp == "user" else char_name,
            "is_user": sp == "user",
            "send_date": _iso(ts),
            "mes": text,
            "extra": {},
        }, ensure_ascii=False))
    return "\n".join(lines) + "\n", len(rows)


def safe_name(s):
    return re.sub(r'[\\/:*?"<>|]', "_", (s or "她")).strip() or "她"


def write_to_dir(store, chat_id, out_dir, char_name="她", user_name="我", overwrite=False):
    """写到指定目录（默认给一个新文件名，绝不覆盖已有聊天）"""
    if not os.path.isdir(out_dir):
        return {"ok": False, "error": f"目录不存在: {out_dir}"}
    body, n = build_jsonl(store, chat_id, char_name, user_name)
    if not body:
        return {"ok": False, "error": "这段聊天是空的"}

    stamp = datetime.now().strftime("%Y-%m-%d@%Hh%Mm%Ss%fms")[:-6] + "ms"
    fname = f"{safe_name(char_name)} - 客户端迁回 {stamp}.jsonl"
    path = os.path.join(out_dir, fname)
    if os.path.exists(path) and not overwrite:
        return {"ok": False, "error": "同名文件已存在"}
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return {"ok": True, "path": path, "count": n}


def write_zip(store, chat_id, char_name="她", user_name="我"):
    """返回 zip 字节（内含 jsonl），供浏览器下载"""
    import io
    body, n = build_jsonl(store, chat_id, char_name, user_name)
    if not body:
        return None, 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{safe_name(char_name)} - 客户端迁回.jsonl", body)
    return buf.getvalue(), n
