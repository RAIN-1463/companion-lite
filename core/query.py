# -*- coding: utf-8 -*-
"""
Companion Lite · 查询理解（LLM 改写 + 重排）

依据：检索文献里，**重排器是不依赖神经向量时最大的一根杠杆**；
查询改写（query expansion）次之。两者都只在"检索不确定"时才花钱。
"""
import json
import re

EXPAND_PROMPT = """把用户这句话改写成用于检索旧对话的**关键词组**。

规则：
- 输出 JSON：{{"terms":["词1","词2",...],"about_past":true/false}}
- terms：3~6 个，2~4 字，覆盖他的**同义说法**（例："不想学"→["拖延","没动力","不想动","学不进去"]）
- 不要人名、不要虚词
- about_past：这句话是否在问过去的事（记得/之前/上次/哪天）→ true，否则 false

用户的话：{q}
"""

RERANK_PROMPT = """用户在找旧对话。下面是 8 条候选，请挑出**最相关的 4 条**（按相关度排序）。

用户的话：{q}

候选：
{cands}

只输出 JSON 数组，元素是候选编号，例如 [3,1,5,2]。不要解释。
"""


class QueryBoost:
    def __init__(self, cfg, stream_chat_fn, cache_size=300):
        self.cfg = cfg
        self.llm = stream_chat_fn
        self.cache = {}
        self.cache_size = cache_size

    def _call(self, prompt, max_tokens=200):
        buf = []
        for piece in self.llm(
            [{"role": "user", "content": prompt}],
            base_url=self.cfg["base_url"], api_key=self.cfg["api_key"],
            model=self.cfg["model"], temperature=0.3,
            max_tokens=max_tokens, no_thinking=True):
            buf.append(piece)
        return "".join(buf)

    def expand(self, text):
        """返回 (terms, about_past)；失败返回 ([], False)"""
        key = text.strip()[:80]
        if key in self.cache:
            return self.cache[key]
        try:
            raw = self._call(EXPAND_PROMPT.format(q=key))
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return [], False
            d = json.loads(m.group(0))
            terms = [str(t).strip() for t in (d.get("terms") or []) if 2 <= len(str(t).strip()) <= 6]
            res = (terms[:6], bool(d.get("about_past")))
        except Exception:
            res = ([], False)
        if len(self.cache) > self.cache_size:
            self.cache.clear()
        self.cache[key] = res
        return res

    def rerank(self, query, items, keep=4):
        """items: [{text,...}]；返回重排后的列表"""
        if len(items) <= 2:
            return items
        cands = "\n".join(f"{i+1}. {it['text'].replace(chr(10),' ')[:70]}" for i, it in enumerate(items[:8]))
        try:
            raw = self._call(RERANK_PROMPT.format(q=query[:60], cands=cands), max_tokens=80)
            m = re.search(r"\[[^\]]*\]", raw)
            if not m:
                return items
            order = [int(x) for x in re.findall(r"\d+", m.group(0))]
        except Exception:
            return items
        out, seen = [], set()
        for n in order:
            i = n - 1
            if 0 <= i < len(items) and i not in seen:
                seen.add(i)
                out.append(items[i])
        for i, it in enumerate(items):
            if i not in seen and len(out) < keep:
                out.append(it)
        return out or items
