# -*- coding: utf-8 -*-
"""Companion Lite · 日记（每天一篇，第一人称，写"我当时怎么想"）"""
from datetime import datetime

DIARY_PROMPT = """下面是今天你和{{user}}的对话。请用**你自己的口吻**写一篇日记（第一人称"我"）。

要求：
1. 250 字以内，不要分点，像人随手写的。
2. 不要复述对话，写**你的感受和判断**：今天他怎么样、你怎么看、你有没有哪句话说得不对。
3. 可以记一两件具体的事，但不要流水账。
4. 不要写成客服总结，不要出现"用户""对话"这类词。
5. 只输出日记正文，不要标题。

今天的对话：
{transcript}
"""


def _today_transcript(store, day=None, limit=120):
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = store.con.execute(
        "SELECT ts, speaker, text FROM messages WHERE substr(ts,1,10)=? ORDER BY ts LIMIT ?",
        (day, limit)).fetchall()
    if not rows:
        return "", 0
    lines = []
    for ts, sp, tx in rows:
        who = "他" if sp == "user" else "我"
        one = " ".join(tx.split())
        if len(one) > 120:
            one = one[:120] + "…"
        lines.append(f"[{ts[11:16]}] {who}：{one}")
    return "\n".join(lines), len(rows)


def generate_diary(store, cfg, stream_chat_fn, day=None, force=False):
    """生成并保存某天的日记。返回 (ok, text_or_error)"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    exist = store.con.execute("SELECT text FROM diary WHERE day=?", (day,)).fetchone()
    if exist and not force:
        return True, "already"

    transcript, n = _today_transcript(store, day)
    if n < 4:
        return False, "今天对话太少（少于 4 条），不写"

    prompt = DIARY_PROMPT.replace("{{user}}", cfg.get("user_name", "他")).replace(
        "{transcript}", transcript)

    buf = []
    try:
        for piece in stream_chat_fn(
            [{"role": "user", "content": prompt + "\n\n" + transcript}],
            base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
            temperature=0.9, max_tokens=500, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return False, f"调用失败: {e}"

    text = "".join(buf).strip()
    if not text:
        return False, "模型返回空"

    # 去掉可能带的标题
    if text.startswith("#"):
        text = "\n".join(text.split("\n")[1:]).strip()

    store.add_diary(day, text[:800])
    return True, text
