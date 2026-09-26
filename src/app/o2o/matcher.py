"""映射匹配器（M3 的核心）—— 天猫商品 × 云商商品 → auto / review / miss 三态。

两边编码体系是**断的**（天猫 `SPCG…` 官方零售码 ↔ 云商渠道料号，实测 0/7 命中，
见开发目标「三·五」），编码键判死 ⇒ 只能拿**名称**对碰，而名称一边是营销标题、
一边是 `类别/品牌/型号/颜色` 斜杠结构 ⇒ 先规范化再打分。

三条红线（每条都有测试钉子，别放松阈值）：

1. **歧义不猜** —— 唯一强命中才 `auto`，其余一律 `review` 进待核页。
   匹错 = 把 A 商品库存传到 B 链接（超卖/错卖），比"多点一次人工"贵得多。
2. **泛型号黑名单** —— 云商有「真无线蓝牙耳机」「无线鼠标」这种泛行，
   同时接住 FreeBuds7 和 7i（实测撞过）：泛行只许当 review 候选，永远不许 auto。
3. **占位脏编码判死** —— `111111111111` 这种占位码不当有效信息（实测存在）。

打分口径（原型三轮调出来的，见记忆日志）：**中文 bigram + 拉丁 alnum token**，
按云商「型号段」（第 3 段）建倒排；按 8 字分块那种切法和云商名永远对不齐（第 2 轮教训）。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# ---- 阈值：都是拿 161 vs 11,371 全量对碰试出来的，改动要有数据支撑 ----
MIN_SCORE = 4        # auto 的最低分（命中 token 数）
SCORE_GAP = 2        # 第一名至少比"并列组之后的下一个"高这么多
MAX_DF = 500         # token 区分度上限：出现在超过这么多行里就当废话丢掉

#: 营销词/大路词 —— 不参与打分（没有区分度，只会把分数灌水）
STOPWORDS = set(
    "华为 官方 旗舰店 新品 闪购 专属 优惠 教育 国家补贴 促销 手机 平板 智能 "
    "手表 耳机 路由 充电 数据线 适用于 官方正品 直营 鸿蒙 麒麟 红枫 影像 长续航 "
    "高清 无线 蓝牙 版 代 新一代 超薄 轻薄 高刷 护眼 全面屏 快速 大电池 安全 "
    "纯净 防诈 智慧 支持 适合 发货 颜色 随机".split()
)

#: 泛型号黑名单（云商第 3 段 = 这些词 ⇒ 不许 auto）。
#: 实测撞过：「真无线蓝牙耳机」同时接住 FreeBuds7 / FreeBuds 7i。
GENERIC_MODEL_KEYS = set(
    "真无线蓝牙耳机 无线耳机 无线鼠标 华为 耳机 手机壳 数据线 保护膜 贴膜".split()
)

#: 拉丁/数字 token（归一化后）
_LATIN = re.compile(r"[a-z0-9]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
#: 云商名里的括号规格段：`(16GB+256GB)` / `[R5-7430U…]`
_BRACKET = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
#: 渠道/颜色尾巴：`-HX` / `-JC` / `-外调` 这种短横后缀
_TAIL = re.compile(r"[-—][a-z0-9\u4e00-\u9fff]{1,6}$")


def normalize(s: Optional[str]) -> str:
    """小写 + 只留拉丁数字与中文（其余全剥）。两边比对前都过这一步。"""
    s = (s or "").lower().replace("│", "|").replace("｜", "|")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def title_tokens(title: str) -> Set[str]:
    """标题 → 打分 token：拉丁 alnum（≥2）+ 中文 bigram，去营销词。

    ⚠ 拉丁 token 必须在**原文小写**上找：先 normalize（去空格标点）会把
    `90 Pro Max 8500mAh` 粘成一个大 token，跟云商侧永远对不上 ——
    空格/标点就是天然词边界，别把边界剥掉再切（踩过）。
    """
    s = (title or "").lower()
    toks = set(_LATIN.findall(s))
    for run in _CJK_RUN.findall(title or ""):
        for i in range(len(run) - 1):
            toks.add(run[i] + run[i + 1])
    return {t for t in toks if t not in STOPWORDS}


def model_key(cloud_name: str) -> str:
    """云商 `类别/品牌/型号详情/颜色…` → **型号段归一化 key**。

    取第 3 段（不足 3 段用整名），剥掉括号规格、渠道/颜色尾巴 ——
    同型号多个 ProId（颜色/渠道拆行）必须归并成同一个 key，
    否则并列第一名会被判成"型号段不一致"全部误进待核（原型第 3 轮的教训）。
    """
    parts = (cloud_name or "").split("/")
    seg = parts[2] if len(parts) > 2 else (cloud_name or "")
    s = (seg or "").lower()
    prev = None
    while prev != s:            # 括号剥掉后可能又露出尾巴，循环到不动点
        prev = s
        s = _BRACKET.sub("", s)
        s = _TAIL.sub("", s)
    return normalize(s)


def is_generic(key: str) -> bool:
    """泛型号判定：黑名单命中 或 key 短到没有区分度（≤2 字）。"""
    if not key or len(key) <= 2:
        return True
    if key in GENERIC_MODEL_KEYS:
        return True
    return any(key in g or g in key for g in GENERIC_MODEL_KEYS)


def code_is_placeholder(code: Optional[str]) -> bool:
    """占位脏码判死：空、纯重复数字（`111111111111`）都算无效。"""
    c = (code or "").strip()
    if not c:
        return True
    body = re.sub(r"^[A-Za-z]+", "", c)
    return bool(body) and len(set(body)) == 1 and body[0].isdigit()


class CloudIndex:
    """云商商品表的匹配索引（倒排：token → 行下标）。纯数据结构，无 IO。"""

    def __init__(self, rows: Sequence[Tuple[str, str]]):
        """rows: [(ProId, 商品名), ...] —— 每行一个云商商品。"""
        self.rows: List[Tuple[str, str]] = list(rows)
        self.keys: List[str] = [model_key(n) for _, n in self.rows]
        self.inv: Dict[str, Set[int]] = defaultdict(set)
        for i, (_, name) in enumerate(self.rows):
            for t in title_tokens(name):
                self.inv[t].add(i)


class MatchResult:
    """一条天猫商品的匹配结论。status ∈ auto / review / miss。"""

    __slots__ = ("item_id", "status", "pro_ids", "model_key", "score", "second", "candidates")

    def __init__(self, item_id: str, status: str, pro_ids: List[str],
                 model_key: str = "", score: int = 0, second: int = 0,
                 candidates: Optional[List[Tuple[str, int]]] = None):
        self.item_id = item_id
        self.status = status            # auto | review | miss
        self.pro_ids = pro_ids          # auto 时 = 唯一命中的 ProId 列表（同型号多行先全给）
        self.model_key = model_key
        self.score = score
        self.second = second
        self.candidates = candidates or []

    def as_dict(self) -> dict:
        return {"item_id": self.item_id, "status": self.status,
                "pro_ids": self.pro_ids, "model_key": self.model_key,
                "score": self.score, "second": self.second,
                "candidates": self.candidates}

    def __repr__(self) -> str:          # pragma: no cover - 调试用
        return "MatchResult(%s, %s, %s, s=%d/%d)" % (
            self.item_id, self.status, self.pro_ids, self.score, self.second)


def match_item(index: CloudIndex, item_id: str, title: str) -> MatchResult:
    """一条天猫商品 → 三态结论。**唯一强命中才 auto**（红线 1）。"""
    tt = [t for t in title_tokens(title) if 1 <= len(index.inv.get(t, ())) <= MAX_DF]
    score: Counter = Counter()
    for t in sorted(tt):
        # ⚠ 集合遍历顺序受 PYTHONHASHSEED 随机化 —— 同分并列时结论会"每次不一样"
        #   （真数据 8 连跑出过 6/4 两个结果）。倒排遍历和排序都按行号钉死。
        for i in sorted(index.inv[t]):
            score[i] += 1
    if not score:
        return MatchResult(item_id, "miss", [])

    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    best = ranked[0][1]
    top = [i for i, s in ranked if s == best]
    keys = {index.keys[i] for i in top}
    second = ranked[len(top)][1] if len(top) < len(ranked) else 0
    key = sorted(keys, key=len, reverse=True)[0] if keys else ""
    pros = sorted({index.rows[i][0] for i in top})
    cands = [(index.rows[i][0], s) for i, s in ranked[:5]]

    if best < MIN_SCORE or best - second < SCORE_GAP or len(keys) != 1:
        return MatchResult(item_id, "review", [], key, best, second, cands)
    if is_generic(key):                  # 红线 2：泛型号不许 auto
        return MatchResult(item_id, "review", [], key, best, second, cands)
    # 红线 4：型号 key 必须真的出现在标题里 —— 分数够但驴唇不对马嘴照样拦
    # （真数据实测：手环10 打分打到云商「4g通话」还差点击 auto，s=4/2）
    if key not in normalize(title):
        return MatchResult(item_id, "review", [], key, best, second, cands)
    return MatchResult(item_id, "auto", pros, key, best, second, cands)


def match_all(index: CloudIndex,
               items: Iterable[Tuple[str, str, str]]) -> Dict[str, MatchResult]:
    """批量匹配。items: [(item_id, title, code), ...]。

    ⚠ 占位脏码（红线 3）不改变匹配算法，但结果里 model 也只到 review 兜底：
    这类商品的编码信息不可信，宁可人工看一眼。
    """
    out: Dict[str, MatchResult] = {}
    for item_id, title, code in items:
        r = match_item(index, item_id, title)
        if r.status == "auto" and code_is_placeholder(code):
            r.status = "review"          # 脏码商品 → 人工确认（实测 111111111111）
        out[item_id] = r
    return out
