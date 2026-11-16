# -*- coding: utf-8 -*-
"""
Companion Lite · 她自己决定要不要翻旧账

做法：用一次极小的调用（约 60 token）问她"这句需不需要查记录"。
她回答 [查:词1,词2] 或 [不查]。
- 需要 → 用她给的词去检索
- 不需要 → 本次完全不检索（更快、更便宜，也不会被旧事干扰）

失败/超时 → 回退到"自动检索"，不影响聊天。
"""
import json
import re

PROMPT = """你在扮演{char}，和{user}聊天。

他刚说：{text}

判断：要回答这句，需不需要翻你们以前的聊天记录？
- 需要查 → 只输出：[查:关键词1,关键词2]（1~3 个词，用来检索旧对话）
- 不需要 → 只输出：[不查]

不要解释，只输出一行。"""


def decide(cfg, text, stream_chat_fn, timeout_tokens=40):
    """返回 (need: bool, terms: list[str], raw: str)"""
    prompt = PROMPT.format(char=cfg.get("name", "她"),
                           user=cfg.get("user_name", "他"),
                           text=text[:200])
    buf = []
    try:
        for piece in stream_chat_fn(
            [{"role": "user", "content": prompt}],
            base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
            temperature=0.1, max_tokens=timeout_tokens, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return None, [], f"err:{e}"

    raw = "".join(buf).strip()
    if "[不查]" in raw or "不查" in raw:
        return False, [], raw[:40]
    m = re.search(r"\[?查[:：]\s*([^\]]+)\]?", raw)
    if m:
        terms = [t.strip() for t in re.split(r"[,，、\s]+", m.group(1)) if t.strip()]
        terms = [t for t in terms if 1 <= len(t) <= 8][:3]
        if terms:
            return True, terms, raw[:60]
    # 认不出来 → 回退到自动检索
    return None, [], raw[:40]
