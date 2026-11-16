# -*- coding: utf-8 -*-
"""Companion Lite · 外界感知（天气 + 热榜，压缩成一句）

原则：她不是新闻播报员，只是"听到外面的动静"。
抓不到就静默跳过，绝不影响聊天。
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://www.bilibili.com/"}


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


WEATHER_CN = [
    (("Smoky haze", "Haze", "Mist"), "有霾"),
    (("Fog",), "有雾"),
    (("Thunder",), "雷雨"),
    (("Heavy rain",), "大雨"),
    (("Moderate rain",), "中雨"),
    (("Light rain", "Patchy rain", "Drizzle"), "小雨"),
    (("Heavy snow",), "大雪"),
    (("Light snow", "Patchy snow", "Blizzard"), "小雪"),
    (("Overcast",), "阴"),
    (("Partly cloudy",), "多云"),
    (("Cloudy",), "多云"),
    (("Clear", "Sunny"), "晴"),
]


def _cn_weather(desc):
    for keys, cn in WEATHER_CN:
        for k in keys:
            if k.lower() in desc.lower():
                return cn
    return ""


def fetch_weather(city=""):
    """wttr.in（免费无 key）。返回 '晴 11℃' 这种短句"""
    try:
        q = urllib.parse.quote(city.strip()) if city.strip() else ""
        url = f"https://wttr.in/{q}?format=j1"
        data = json.loads(_get(url, timeout=10).decode("utf-8", "ignore"))
        cc = (data.get("current_condition") or [{}])[0]
        temp = cc.get("temp_C") or ""
        desc = ((cc.get("weatherDesc") or [{}])[0].get("value") or "")
        cn = _cn_weather(desc)
        if cn and temp:
            return f"{cn} {temp}℃"
        if temp:
            return f"{temp}℃"
        return ""
    except Exception:
        return ""


def fetch_hot(n=6):
    """百度热搜（公开页面，无需 key）。过滤掉时政/灾难/营销的，只留能闲聊的"""
    try:
        import re as _re
        raw = _get("https://top.baidu.com/board?tab=realtime", timeout=12).decode("utf-8", "ignore")
        m = _re.search(r"<!--s-data:(.*?)-->", raw, _re.S)
        if not m:
            return []
        data = json.loads(m.group(1))
        words = []
        for card in data.get("data", {}).get("cards", []):
            for it in card.get("content", []) or []:
                w = (it.get("word") or it.get("query") or "").strip()
                if w and w not in words:
                    words.append(w)
        return words
    except Exception:
        return []


BAD_WORDS = ["震惊", "破防", "塌房", "爆料", "内幕", "撕逼", "骂战", "出轨",
             "跳楼", "死亡", "身亡", "遇难", "凶杀", "袭击", "自杀", "车祸",
             "习近平", "总书记", "国务院", "政治局", "省委", "市委", "两会",
             "被查", "落马", "反腐", "通报", "警方", "导弹", "战争", "疫苗",
             "暴跌", "暴涨", "怒赚", "狂赚", "官宣"]


def _clean(titles):
    return [t for t in titles if not any(w in t for w in BAD_WORDS)]


SUMMARY_PROMPT = """今天是 {date}。
天气：{weather}
网上在聊（部分）：
{hot}

请用**她的口吻**写成 2 句话（60 字以内），像"待在家里听来的外面的事"：
1. 第一句说天气（如果没抓到天气就跳过）
2. 第二句说一两件外面在聊的事，要说得含糊一点（"网上都在说…"），不要用夸张词，不要像新闻标题
3. 不要出现"用户""系统""数据"这类词
4. 只输出这两句话
"""


def build_world_text(cfg, stream_chat_fn, now=None):
    """抓取 + 压缩，返回一句可她用的话；失败返回空"""
    now = now or datetime.now()
    weather = fetch_weather(cfg.get("world_city", ""))
    hot = _clean(fetch_hot(int(cfg.get("world_hot_count", 6))))
    if not weather and not hot:
        return "", "抓取失败"

    prompt = SUMMARY_PROMPT.format(
        date=f"{now:%Y年%m月%d日}",
        weather=weather or "（没抓到）",
        hot="\n".join(f"- {t}" for t in hot) or "（没抓到）",
    )
    buf = []
    try:
        for piece in stream_chat_fn(
            [{"role": "user", "content": prompt}],
            base_url=cfg["base_url"], api_key=cfg["api_key"], model=cfg["model"],
            temperature=0.7, max_tokens=200, no_thinking=True):
            buf.append(piece)
    except Exception as e:
        return "", f"压缩失败: {e}"

    text = "".join(buf).strip()
    text = " ".join(text.split())
    if not text or len(text) > 160:
        return "", "输出不合规"
    return text, "ok"
