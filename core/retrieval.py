# -*- coding: utf-8 -*-
"""
Companion Lite · 检索核心 v2

依据 AgentIR (arXiv 2605.25092)：
  - 对话记忆场景 BM25 单路已是最强（LoCoMo Hit@10 0.945）
  - 7 天时间分区 + 新到旧扫描 + 早停
  - 级联：只看 BM25 前二名分数差，够大就不跑向量
  - 打分 = 秩分 + α·e^(−Δt/τ) + β·重要度（α 远小于主分，只做平局打破）

本模块只读数据库（mode=ro），不写任何东西。
"""
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta

try:
    import jieba
except Exception:
    jieba = None

TS_FMT = "%Y-%m-%d %H:%M:%S"

# ---------------- 参数（可调） ----------------
PARTITION_DAYS = 7        # 时间分区宽度
PARTITION_MAX = 8         # 最多扫几个分区
CANDIDATE_POOL = 30       # 主路候选数
RRF_K = 60
TAU_DAYS = 30.0           # 时效衰减尺度
ALPHA = 0.010             # 时效奖励权重（远小于主分）
BETA = 0.005              # 重要度权重
CASCADE_MARGIN = 3.0      # 前二分差 ≥ 此值 → 跳过向量

# ---------------- 停用词 ----------------
FUNC = set("""记得 还记得 记忆 忘记 什么 哪些 啥 怎么 怎么样 如何 怎样 为什么
我们 你们 他们 咱们 我 你 他 她 它 自己 别人 之前 以前 以后 之后 上次 下次
刚才 刚刚 现在 当时 那时候 今天 昨天 前天 明天 最近 这次 那次 这回 那回 这些 那些
这样 那样 这个 那个 一个 一件 一点 一些 一样 一直 一起 一定 一般 一边
东西 事情 事 问题 办法 方法 地方 时候 样子 情况 意思 内容
告诉 知道 觉得 感觉 认为 想要 需要 可以 可能 应该 能够 有没有 是不是 能不能
可不可以 要不要 行不行 会不会 对不对 好不好
是 有 在 就 也 都 还 又 再 才 很 太 更 最 不 没 没有 别 被 把 给 让 对 从 到
和 与 跟 同 的 了 过 着 吗 呢 吧 啊 呀 哦 嗯 喔 嘛 么 咯
说过 聊过 提过 讲过 谈过 说起 提起 提到 谈到 问到 问过 讲了 说了 聊了
往前 往后 往这 往那 这边 那边 里面 外面 上面 下面
真的 其实 好像 有点 还是 就是 不是 已经 因为 所以 但是 如果 不过 而且 然后
看来 改了 做完 好了 别的 其他 另外 反正 不管 无论 还有 不用 干嘛 干啥
关于 当时 那句 说法 讲法 大概 意思 内容 情况 那次 这回 那次
什么样 么样 怎样 怎么样 不好 很好 好多 多少 一些 这些 那些 有点 一直
""".split())

TIME_WORDS = ["大前天", "前天", "昨天", "昨日", "今天", "今日", "上周", "上个月"]

# ---------------- 同义词组（Tier A） ----------------
SYNONYM_GROUPS = [
    # 拖延 / 动力
    ["拖延", "懒", "不想动", "没动力", "摆烂", "拖", "明天再", "不想做", "使不上劲",
     "不想学", "学不进去", "学不下去", "提不起劲", "打不起精神"],
    # 作息
    ["熬夜", "没睡好", "睡不好", "睡不着", "失眠", "通宵", "睡过头", "晚睡",
     "起不来", "困", "睡不够", "睡不踏实"],
    # 饮食
    ["外卖", "炸鸡", "泡面", "不吃早饭", "饿", "宵夜", "快餐"],
    # 情绪
    ["烦", "难受", "压力", "崩", "累", "抑郁", "焦虑", "撑不住"],
    # 自我评价
    ["废物", "没用", "无能", "自责", "对不起", "都是我的错"],
    # 亲密
    ["想你", "喜欢", "抱抱", "亲亲", "撒娇", "么么哒"],
    # 学习
    ["六级", "考试", "复习", "背单词", "单词", "初会", "C++", "编程", "代码", "学习", "学"],
    # 宿舍
    ["宿舍", "寝室", "舍友", "同屋"],
]
SYNONYM_MAP = {}
for group in SYNONYM_GROUPS:
    for w in group:
        SYNONYM_MAP.setdefault(w, set()).update(group)


# ---------------- 时间解析 ----------------
CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def parse_time_range(text, now=None):
    now = now or datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    t = text
    for kw, d in (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("今天", 0), ("今日", 0)):
        if kw in t:
            s = today - timedelta(days=d)
            return s, s + timedelta(days=1) - timedelta(seconds=1)
    m = re.search(r"(\d+|[一两二三四五六七八九十])\s*天前", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else CN_NUM.get(m.group(1), 1)
        s = today - timedelta(days=n)
        return s, s + timedelta(days=1) - timedelta(seconds=1)
    m = re.search(r"(上|这|本)?(?:周|礼拜|星期)([一二三四五六日天])", t)
    if m:
        pre, wd = (m.group(1) or ""), WEEKDAY.get(m.group(2), 0)
        mon = today - timedelta(days=today.weekday())
        if pre == "上":
            mon -= timedelta(days=7)
        s = mon + timedelta(days=wd)
        return s, s + timedelta(days=1) - timedelta(seconds=1)
    if re.search(r"上周|上个?礼拜|上个?星期", t):
        mon = today - timedelta(days=today.weekday()) - timedelta(days=7)
        return mon, mon + timedelta(days=7) - timedelta(seconds=1)
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[号日]", t)
    if m:
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)))
            return d, d + timedelta(days=1) - timedelta(seconds=1)
        except ValueError:
            return None
    if re.search(r"最近|这几天|前几天", t):
        return today - timedelta(days=7), now
    return None


# ---------------- 选词 ----------------
class Index:
    def __init__(self, db_path):
        self.db_path = db_path
        self.con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self.con.execute("PRAGMA temp_store=MEMORY")
        self._df = None

    # ---- 词频表（优先用库里现成的 gram_df）----
    def df(self):
        if self._df is None:
            try:
                self._df = {g: c for g, c in self.con.execute("SELECT gram, df FROM gram_df")}
            except Exception:
                self._df = {}
            if not self._df:
                # 没有就现场统计（只读，不写库）
                counts = {}
                for (txt,) in self.con.execute("SELECT text FROM messages"):
                    seen = set()
                    for n in (2, 3):
                        for i in range(len(txt) - n + 1):
                            w = txt[i:i + n]
                            if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9+]+", w):
                                seen.add(w)
                    for w in seen:
                        counts[w] = counts.get(w, 0) + 1
                self._df = {w: c for w, c in counts.items() if c <= 200}
        return self._df

    def extract_terms(self, text, cap=12):
        s = text
        for w in TIME_WORDS:
            s = s.replace(w, " ")
        s = re.sub(r"\d+\s*[天月号日]", " ", s)
        toks = []
        if jieba is not None:
            for tk in jieba.cut(s):
                tk = tk.strip()
                if len(tk) >= 2 and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9+]+", tk):
                    toks.append(tk)
        if not toks:
            toks = [p for p in re.split(r"[^\u4e00-\u9fa5A-Za-z0-9+]+", s) if len(p) >= 2]

        cand = []
        for tk in toks:
            if tk in FUNC:
                continue
            if tk not in cand:
                cand.append(tk)
            if len(tk) >= 3:
                for i in range(len(tk) - 1):
                    w = tk[i:i + 2]
                    if w not in FUNC and w not in cand:
                        cand.append(w)

        df = self.df()

        def ok(d, w):
            if d is None or d < 1:
                return False
            if len(w) == 2:
                return d <= 100
            if len(w) == 3:
                return d <= 120
            return d <= 150

        keep = [(df.get(w), w) for w in cand]
        keep = [(d, w) for d, w in keep if ok(d, w)]
        if keep:
            keep.sort(key=lambda x: (x[0], -len(x[1])))
            out = [w for _, w in keep]
            out.sort(key=lambda w: (-len(w), w))
            return out[:cap]
        return sorted(cand, key=lambda x: (-len(x), x))[:cap]

    def synonym_terms(self, text, cap=10):
        """从原句里直接扫同义词组（含单字键），返回组内的 2 字以上词"""
        hits = []
        for key, group in SYNONYM_MAP.items():
            if key in text:
                if key not in hits and key not in FUNC:
                    hits.append(key)          # 原词本身（含单字，如"懒""困"）
                for w in group:
                    if len(w) >= 2 and w not in hits and w not in FUNC:
                        hits.append(w)
        return hits[:cap]

    def expand(self, terms):
        """同义词扩展（只在级联低置信时调用）"""
        out = list(terms)
        for t in terms:
            for syn in SYNONYM_MAP.get(t, ()):  # 精确匹配组
                if syn not in out and syn not in FUNC:
                    out.append(syn)
        return out[:18]

    # ---- 单分区 FTS 查询 ----
    def _fts(self, term, lo, hi, limit=20):
        sql = ("SELECT m.id, m.ts, m.speaker, m.text, bm25(messages_fts) AS s "
               "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
               "WHERE messages_fts MATCH ? AND m.ts >= ? AND m.ts <= ? "
               "ORDER BY s LIMIT ?")
        try:
            return self.con.execute(sql, [f'"{term}"', lo, hi, limit]).fetchall()
        except Exception:
            return []

    def _like(self, term, lo, hi, limit=12):
        sql = ("SELECT m.id, m.ts, m.speaker, m.text FROM messages m "
               "WHERE m.text LIKE ? AND m.ts >= ? AND m.ts <= ? ORDER BY m.ts ASC LIMIT ?")
        try:
            return self.con.execute(sql, [f"%{term}%", lo, hi, limit]).fetchall()
        except Exception:
            return []

    # ---- 主检索 ----
    def search(self, query, limit=6, max_chars=900, now=None):
        t0 = time.time()
        now = now or datetime.now()
        rng = parse_time_range(query, now=now)
        core_terms = self.extract_terms(query)
        n_core = len(core_terms)
        syn_list = self.synonym_terms(query)
        syn_terms = [w for w in syn_list if w not in core_terms]

        hi = rng[1] if rng else now
        lo_all = datetime(2000, 1, 1)

        def partition_bounds(i):
            p_hi = hi - timedelta(days=PARTITION_DAYS * i)
            p_lo = p_hi - timedelta(days=PARTITION_DAYS)
            if rng:
                p_lo = max(p_lo, rng[0])
                p_hi = min(p_hi, rng[1])
            return p_lo, p_hi

        score = {}

        def add(rid, ts, speaker, text, bm25, which):
            if rid not in score:
                score[rid] = {"item": {"id": rid, "ts": ts, "speaker": speaker, "text": text},
                              "core": 0, "syn": 0, "bm25": bm25 if bm25 is not None else 0.0}
            score[rid][which] += 1
            if bm25 is not None:
                score[rid]["bm25"] = min(score[rid]["bm25"], bm25)

        def query_term(term, lo, hp, which):
            if len(term) >= 3:
                rows = self._fts(term, lo.strftime(TS_FMT), hp.strftime(TS_FMT))
                if rows:
                    for r in rows:
                        add(r[0], r[1], r[2], r[3], r[4], which)
                    return
            rows = self._like(term, lo.strftime(TS_FMT), hp.strftime(TS_FMT))
            for r in rows:
                add(r[0], r[1], r[2], r[3], None, which)

        # ---------- 第一路：核心词（新 → 旧分区，够数即停）----------
        scanned = 0
        for p in range(PARTITION_MAX):
            lo, phi = partition_bounds(p)
            if phi <= (rng[0] if rng else lo_all):
                break
            for term in core_terms[:10]:
                query_term(term, lo, phi, "core")
            scanned += 1
            if len(score) >= CANDIDATE_POOL:
                break

        # ---------- 日期兜底：只有时间、没有词 ----------
        if not score and rng:
            lo, hp = rng[0].strftime(TS_FMT), rng[1].strftime(TS_FMT)
            rows = self.con.execute(
                "SELECT id, ts, speaker, text FROM messages WHERE ts >= ? AND ts <= ? ORDER BY ts, id",
                [lo, hp]).fetchall()
            if rows:
                step = max(1, len(rows) // limit)
                for r in rows[::step][:limit]:
                    add(r[0], r[1], r[2], r[3], None, "core")

        # ---------- 计算边际，决定是否级联 ----------
        ranked = sorted(score.values(), key=lambda v: (-v["core"], v["bm25"]))
        margin = 0.0
        if len(ranked) >= 2:
            margin = abs(ranked[1]["bm25"] - ranked[0]["bm25"])

        mode = "lexical"
        need_cascade = (len(ranked) < 3) or (margin < CASCADE_MARGIN)
        if need_cascade and syn_terms:
            mode = "cascade"
            lo = (rng[0] if rng else lo_all)
            for term in syn_terms[:8]:
                query_term(term, lo, hi, "syn")

        # ---------- 排序：核心词权重 2，同义词权重 1 ----------
        need = 2 if n_core >= 5 else 1
        picked = [v for v in score.values() if v["core"] + v["syn"] >= 1 and (v["core"] >= need or v["core"] + v["syn"] >= need)]
        if not picked:
            picked = list(score.values())
        picked.sort(key=lambda v: (-(2 * v["core"] + v["syn"]), v["bm25"]))

        final = []
        for idx, v in enumerate(picked):
            rank_score = 1.0 / (RRF_K + idx + 1)
            try:
                dt = datetime.strptime(v["item"]["ts"], TS_FMT)
                age_days = max(0.0, (now - dt).total_seconds() / 86400)
            except Exception:
                age_days = 999
            recency = ALPHA * pow(2.718281828, -age_days / TAU_DAYS)
            final.append((rank_score + recency, v["item"]))
        final.sort(key=lambda x: -x[0])

        items = [it for _, it in final][:limit]
        out, used = [], 0
        for it in items:
            txt = it["text"]
            if used + len(txt) > max_chars:
                txt = txt[: max(0, max_chars - used)]
            if not txt:
                break
            used += len(txt)
            it["text"] = txt
            out.append(it)
            if used >= max_chars:
                break

        return {
            "items": out,
            "terms": (core_terms + syn_terms)[:12],
            "range": rng,
            "mode": mode,
            "margin": round(margin, 3),
            "partitions": scanned,
            "ms": int((time.time() - t0) * 1000),
        }
