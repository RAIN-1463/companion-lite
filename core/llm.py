# -*- coding: utf-8 -*-
"""Companion Lite · LLM 调用（OpenAI 兼容，流式，支持思考过程）"""
import json
import urllib.request


def stream_chat(messages, *, base_url, api_key, model, temperature=1.0,
                max_tokens=600, timeout=120, no_thinking=False, with_reasoning=False):
    """
    流式产出。
    - 默认：逐块 yield 文本（str）
    - with_reasoning=True：yield dict {"t": "reason"|"content", "v": 文本}
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if no_thinking:
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "ignore").strip()
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            reason = delta.get("reasoning_content")
            piece = delta.get("content")
            if with_reasoning:
                if reason:
                    yield {"t": "reason", "v": reason}
                if piece:
                    yield {"t": "content", "v": piece}
            else:
                if piece:
                    yield piece
