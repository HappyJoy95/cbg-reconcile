# -*- coding: utf-8 -*-
"""待领清单的**纯口径**（零 IO）—— 匹配、状态合并、汇总。"""

from __future__ import annotations

import datetime
from typing import Dict, Iterable, List, Optional

from ..activities.catalog import in_window, matches_model

#: 状态词表：pending=待领（默认）· claimed=已领 · na=不适用/已排除
STATUS_PENDING = "pending"
STATUS_CLAIMED = "claimed"
STATUS_NA = "na"

STATUS_LABELS = {
    STATUS_PENDING: "待领",
    STATUS_CLAIMED: "已领",
    STATUS_NA: "不适用",
}


#: 设备串号候选列（云商：串号 / 串号1~3；库存英文键 Imei*）。
#: ⚠ **绝不含 `串号标识`** —— 那是门店/机况标记（如 `W,新`），不是 SN。
SN_CANDIDATE_KEYS = (
    "sn", "串号", "串号1", "串号2", "串号3",
    "Imei", "IMEI1", "Imei2", "Imei3", "imei",
)


def _looks_like_device_sn(v: str) -> bool:
    """云商设备串号：常见 **16 位**字母数字（也兼容 15/18 位 IMEI）。

    ⚠ 15 位纯数字是 **IMEI（86 码）**，不是华为领取要的 SN ——
    仍可进候选（有的行串号列只写了 IMEI），但要用 `sn_kind()` 分开。
    """
    t = str(v or "").strip()
    if not t or " " in t or "," in t or "，" in t:
        return False
    # 串号标识形如 W,新 / 新 —— 直接不要
    if any(ch in t for ch in ",，"):
        return False
    if not t.isalnum():
        return False
    if len(t) == 16:
        return True
    # 15 位纯数字 = IMEI；18 位偶见
    if len(t) == 15 and t.isdigit():
        return True
    if len(t) == 18 and t.isalnum():
        return True
    return False


def looks_like_imei(v: str) -> bool:
    """15 位纯数字 = IMEI / 86 码（云商「串号」列实测大量如此）。

    华为 `awardDeviceRightV3` 的 `ownerId` 要的是 **SN**；拿 86 码去查
    只会回 `…effectiveRules.sn.NotFound`，文案还被映射成「不符合领取条件」，
    门店会以为活动不对（2026-09-23 用户：「给的是86码，不是sn」）。
    """
    t = str(v or "").strip()
    return len(t) == 15 and t.isdigit()


def sn_kind(v: str) -> str:
    """`imei` / `sn` / `''` —— 给前端标「86码」用；空 = 没识别出设备号。"""
    t = str(v or "").strip()
    if not t:
        return ""
    if looks_like_imei(t):
        return "imei"
    if _looks_like_device_sn(t):
        return "sn"
    return ""


def pick_true_sn(*candidates) -> str:
    """从一串候选里挑**真 SN**（16 位优先）；全是 86 码 → 空。

    库存一行常是 `imei`=86 码、`sub_imei`=真 SN —— 和 `pick_device_sn`
    同规则，但**拒绝回落到 IMEI**（在线领取要的必须是 SN）。
    """
    p = pick_device_sn(*candidates)
    return p if sn_kind(p) == "sn" else ""


def resolve_claim_sn(sales_sn: str, stock_map: Optional[dict]) -> str:
    """销售侧是 86 码时，用库存三列反查真 SN；查不到 → 空。

    `stock_map`：`{任意串号 → 真 SN}`（`compute` 从最新 `erp_stock` 建）。
    销售侧本来就是 SN 时原样返回（不查表）。
    """
    sn = str(sales_sn or "").strip()
    if not sn:
        return ""
    if sn_kind(sn) == "sn":
        return sn
    if not stock_map or not looks_like_imei(sn):
        return ""
    got = str(stock_map.get(sn) or "").strip()
    return got if sn_kind(got) == "sn" else ""


#: 云商串号里可能带的**渠道前缀**（不是设备号的一部分）—— 去掉后再认 16 位。
#: 实测 `JC` + 16 位 = 18 字符（2026-09-23 用户：「序列号前缀可能有 JC 的标识」）。
SN_PREFIXES = ("JC",)


def strip_sn_prefix(v: str) -> str:
    """去掉 `JC` 等渠道前缀；**只在去掉后更像设备号时**才剥。"""
    t = str(v or "").strip()
    for pre in SN_PREFIXES:
        if t[:len(pre)].upper() == pre and len(t) > len(pre) + 10:
            rest = t[len(pre):]
            # 剥完是 16 位字母数字，或原来整串不是合法 16 位
            if len(rest) == 16 and rest.isalnum():
                return rest
            if not (len(t) == 16 and t.isalnum()):
                if rest.isalnum() and len(rest) >= 14:
                    return rest
    return t


def pick_device_sn(*candidates) -> str:
    """从多路候选里挑**设备 SN**（优先 16 位；去 JC 前缀；剔除串号标识）。

    输入可以是散参或列表；内部会再按空白/中英逗号切开多串挤一行。
    """
    pool: List[str] = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, (list, tuple, set)):
            pool.extend(str(x) for x in c)
        else:
            pool.append(str(c))
    # 拆「主串 副串」挤在一格的情况 + 去渠道前缀
    pieces: List[str] = []
    for raw in pool:
        for part in str(raw).replace("，", " ").replace(",", " ").split():
            part = part.strip()
            if not part:
                continue
            part = strip_sn_prefix(part)
            if part:
                pieces.append(part)
    # 1. 精确 16 位
    for p in pieces:
        if len(p) == 16 and p.isalnum() and not any(ch in p for ch in ",，"):
            return p
    # 2. 其它像设备号的
    for p in pieces:
        if _looks_like_device_sn(p):
            return p
    return ""


def status_key(row: dict) -> str:
    """状态的**稳定键** —— 优先串号，否则门店+单号+商品+支付时间。

    ⚠ 同一笔销售每次刷新必须算出同一个键，否则「标了已领」第二天丢了。
    """
    sn = str((row or {}).get("sn") or "").strip()
    if sn and not sn.startswith("nosn:"):
        return "sn:" + sn
    doc = str((row or {}).get("doc") or "").strip()
    store = str((row or {}).get("store") or "").strip()
    name = str((row or {}).get("name") or "").strip()
    ts = str((row or {}).get("ts") or "").strip()
    return "row:%s|%s|%s|%s" % (store, doc, name, ts)


#: 机型级硬排除（活动 exclude 漏配也挡）—— **Pura X View 不是折叠屏系列**
#: （用户 2026-09-23 两次反馈：`Pura X` 子串会吸到 View / VOL-AL00）。
HARD_EXCLUDE_SUBSTR = (
    "pura x view",
    "vol-al00",
    "vde-al00",
)


def hard_excluded(product_name: str) -> bool:
    n = (product_name or "").lower()
    return any(x in n for x in HARD_EXCLUDE_SUBSTR)


def match_activities(product_name: str, day, activities: Iterable[dict],
                     c1: str = "", c2: str = "") -> List[dict]:
    """一笔商品名 + 支付日 → 命中的活动列表。

    ``c1``/``c2`` = 一级/二级分类；给了就先过整机白名单（挡手提袋等）。
    """
    if isinstance(day, str):
        day = day[:10] if day else ""
    # 有品类信息时必须是整机；没有品类（老测试）只看商品名
    if (c1 or c2) and not is_device_row(c1, c2, product_name):
        return []
    # View 等硬排除：任何活动都不进（防止某活动漏配 exclude）
    if hard_excluded(product_name):
        return []
    out = []
    for act in activities or ():
        if matches_model(act, product_name) and in_window(act, day):
            out.append(act)
    return out


#: **整机品类白名单**（`一级分类`）—— 只有整机才谈得上「领赠权益」。
#: ⚠ 手提袋 / 保护壳 / 延保 Care+ 单本身 **不进待领**（2026-09-23 用户：
#:   「手提袋儿什么的都有」—— 靠商品名子串会把
#:   `Pura X专属礼品-手提袋` 这种吸进来）。
DEVICE_C1 = frozenset({
    "手机", "平板", "笔记本电脑", "电脑", "PC",
    "音频产品", "耳机", "智能穿戴", "智能手表", "手表",
    "智慧屏", "显示器", "打印机",
})
#: 二级分类兜底（有的店一级分类写得含糊）
DEVICE_C2 = frozenset({
    "华为手机", "华为平板电脑", "华为笔记本电脑", "耳机", "无线耳机",
    "华为智能手表", "智能手表", "智能穿戴",
})
#: 明确排除（出现即不要，哪怕一级分类怪）
DEVICE_EXCLUDE_C1 = frozenset({
    "潮玩礼品", "手机平板周边", "电脑周边", "配件", "赠品",
    "延保服务", "服务产品", "贴膜", "保护壳套", "笔记本包",
})
DEVICE_EXCLUDE_C2 = frozenset({
    "促销品", "延保服务", "Care+", "平板保护壳", "平板键盘",
    "笔记本包", "贴膜", "手机保护壳",
})


def is_device_row(c1: str = "", c2: str = "", name: str = "") -> bool:
    """是不是**整机**行（可进待领）。品类未知时再看商品名前缀。"""
    c1 = str(c1 or "").strip()
    c2 = str(c2 or "").strip()
    if c1 in DEVICE_EXCLUDE_C1 or c2 in DEVICE_EXCLUDE_C2:
        return False
    if c1 in DEVICE_C1 or c2 in DEVICE_C2:
        return True
    # 品类没写上：用常见整机前缀兜底（仍挡礼品/周边）
    n = str(name or "")
    prefixes = (
        "手机/", "平板电脑/", "笔记本电脑/", "笔记本/", "PC/",
        "耳机麦克/", "手表/", "智慧屏/", "显示器/",
        "HUAWEI ", "华为", "HUAWEI Mate", "HUAWEI Pura", "HUAWEI nova",
        "HUAWEI Free", "HUAWEI WATCH", "MatePad ", "MateBook ",
    )
    return any(n.startswith(p) for p in prefixes)


def is_return(doc_type: str) -> bool:
    """退货单 —— **不进待领**（权益跟着原单，原单侧仍有记录）。"""
    t = str(doc_type or "")
    return "退" in t


def join_status(rows: List[dict], statuses: Optional[dict]) -> List[dict]:
    """把 `out/claim-status.json` 的记录并进行；无记录 = pending。"""
    st = statuses or {}
    out = []
    for r in rows:
        key = r.get("status_key") or status_key(r)
        rec = st.get(key) or {}
        status = str(rec.get("status") or STATUS_PENDING)
        if status not in STATUS_LABELS:
            status = STATUS_PENDING
        item = dict(r)
        item["status_key"] = key
        item["status"] = status
        item["status_label"] = STATUS_LABELS.get(status, status)
        item["status_at"] = str(rec.get("at") or "")
        item["status_by"] = str(rec.get("by") or "")
        item["status_note"] = str(rec.get("note") or "")
        out.append(item)
    return out


def summarize(rows: List[dict]) -> dict:
    """合计 —— 滤店后必须重算（别留全区数）。"""
    pending = claimed = na = 0
    for r in rows or ():
        s = r.get("status") or STATUS_PENDING
        if s == STATUS_CLAIMED:
            claimed += 1
        elif s == STATUS_NA:
            na += 1
        else:
            pending += 1
    total = pending + claimed + na
    return {
        "total": total,
        "pending": pending,
        "claimed": claimed,
        "na": na,
        "claimed_rate": (claimed / total) if total else 0.0,
    }


def sort_rows(rows: List[dict]) -> List[dict]:
    """待领在前、已领在后；同状态按支付时间倒序（新单先看）。"""
    rank = {STATUS_PENDING: 0, STATUS_NA: 1, STATUS_CLAIMED: 2}

    def key(r):
        return (rank.get(r.get("status") or STATUS_PENDING, 0),
                str(r.get("ts") or "")[::-1],  # 日期字符串反转近似倒序
                str(r.get("store") or ""),
                str(r.get("name") or ""))

    # 真正按 ts 降序更直观：
    def key2(r):
        return (rank.get(r.get("status") or STATUS_PENDING, 0),
                _neg_ts(str(r.get("ts") or "")),
                str(r.get("store") or ""),
                str(r.get("name") or ""))

    return sorted(rows or [], key=key2)


def _neg_ts(ts: str):
    """把 ISO 时间串变成可降序比较的键（没有的排后面）。"""
    if not ts:
        return ("\uffff",)
    return ("", tuple(-ord(c) for c in ts[:19]))


def filter_stores(rows: List[dict], allowed: Optional[set]) -> List[dict]:
    """按允许门店集合滤行（`allowed=None` = 全部）。⚠ 名字用调用方 scope 判。"""
    if allowed is None:
        return list(rows)
    return [r for r in rows if str(r.get("store") or "") in allowed]


def default_window(day=None) -> tuple:
    """展示窗口默认：本月 1 号～今天（活动匹配仍按**活动自己的 start/end**）。"""
    d = day or datetime.date.today()
    return d.replace(day=1).isoformat(), d.isoformat()
