# -*- coding: utf-8 -*-
"""无忧会员权益 —— 查本地 **`erp_sales`** 算门店 / 区域 / 人员三块 + 赛道奖。

窗口：本月 1 号 ～ 今天（含）。

配置：
* **赛道 / 台量目标 / 区域** ← `config/valueadd-benefit.yaml`（缺失回落内置默认）
* **店员名册** ← **系统人店表** `store.staff.rosters_by_store()`
  （云商组织架构 + 用户名单；**不读 Excel Sheet5** —— 用户 2026-09-22 定的）
  读不到网络时回落 yaml `people`（仅兜底）。

范围：调用方传入 `stores=`（来自 `role_scope()`）——
门店登录只算本店、区长只算所辖，平台算全部赛道店。

⚠ 新机口径与「防护膜」页 **不同**：这里 **不乘 0.9**（防护膜页有渠道折算）
  —— 两页数字**不要对拍**。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ....paths import ROOT
from ....config_io import load_raw
from ...tools.claim.activities import catalog as claim_catalog
from . import metric
from .. import foreign as foreign_mod


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def month_window(day=None) -> Tuple[str, str]:
    d = day or datetime.date.today()
    start = d.replace(day=1).isoformat()
    end = d.isoformat()
    return start, end


def find_db(root) -> Optional[Path]:
    from ....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


def load_config(root=None) -> dict:
    """`config/valueadd-benefit.yaml` 覆盖内置默认。

    ⚠ **只含赛道 / 台量目标 / 区域** —— 不含人员；人员见 `load_people`。
    ⚠ **不读任何用户 Excel**（2026-09-22）。
    """
    root = Path(root or ROOT)
    over = load_raw(root / "config" / "valueadd-benefit.yaml") or {}
    # 即使 yaml 误写了 people 也丢掉 —— 名册唯一来源是系统人店表
    if isinstance(over, dict):
        over = {k: v for k, v in over.items() if k != "people"}
    return metric.merge_config(metric.default_config(), over)


def load_people(root=None, config_path=None, env_file=None
                ) -> Tuple[Dict[Tuple[str, str], str], str, str]:
    """系统人店表 → `{(门店, 姓名): 职位}`。

    * **唯一主源**：`features.store.staff.rosters_by_store()`（云商组织架构，12h 缓存）
    * 返回：`(roster, source, why)` —— source = `system` / `empty`
    * 读不到就空名册 + why（**不回落 Excel / yaml people** —— 用户 2026-09-22：
      业务与名单都只认系统拉下来的，不认桌面上的表）

    ⚠ 组织架构树里**没有职位**（店长/顾问），title 留空 —— 名字对上就能进人榜。
    """
    root = Path(root or ROOT)
    try:
        # features.valueadd.benefit → features.store.staff
        from ...store import staff as staff_mod
        by_store = staff_mod.rosters_by_store(root, config_path, env_file)
        roster: Dict[Tuple[str, str], str] = {}
        for st, names in (by_store or {}).items():
            st = str(st or "").strip()
            if not st:
                continue
            for nm in names or []:
                nm = str(nm or "").strip()
                if nm:
                    roster[(st, nm)] = ""
        if roster:
            return roster, "system", ""
        return {}, "empty", "系统人店表为空"
    except Exception as e:  # noqa: BLE001 —— 说清楚，但不拿 Excel 顶上
        return {}, "empty", "系统人店表读失败：%s" % e


def _load_rows(db: Path, start: str, end: str):
    """窗口内 `erp_sales` 全部相关行（手机 / 会员 / 延保）。"""
    end_ex = (datetime.date.fromisoformat(end)
              + datetime.timedelta(days=1)).isoformat()
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        # 「客户/顾客」是判「美团转线上」的关键列（万达备注不写美团、客户字段写）
        # ⚠ 老库可能没这列 —— PRAGMA 问一下，缺就补 '' AS，别让整页炸（同 film._select_sql）
        try:
            have = {str(x[1]) for x in conn.execute("PRAGMA table_info(erp_sales)")}
        except sqlite3.Error:
            have = set()
        cust = '"客户/顾客"' if "客户/顾客" in have else "'' AS \"客户/顾客\""
        # 串号标识：认演示机/样机（口径E）用
        sid = "串号标识" if "串号标识" in have else "'' AS 串号标识"
        sql = ("SELECT 门店, 店员, 单据类型, 商品名称, 一级分类, 二级分类,"
               " 数量, 零售考核毛利, 支付时间, 备注, 单行备注, " + cust + ", " + sid +
               " FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 < ?")
        for r in conn.execute(sql, (start, end_ex)):
            yield {
                "store": r["门店"] or "",
                "who": r["店员"] or "",
                "typ": r["单据类型"] or "",
                "name": r["商品名称"] or "",
                "c1": r["一级分类"] or "",
                "c2": r["二级分类"] or "",
                "qty": _f(r["数量"]),
                "profit": _f(r["零售考核毛利"]),
                "ts": r["支付时间"] or "",
                "note": " ".join(str(r[k] or "") for k in ("备注", "单行备注")),
                "cust": r["客户/顾客"] or "",
                "sid": r["串号标识"] or "",
            }
    finally:
        conn.close()


def care_models_of(root) -> list:
    """送 **Care+** 的机型（折叠屏金秋礼遇活动）—— 这些机型的手机行**不算新机**。

    用户 2026-09-26 拍板；机型表**跟着权益领取活动目录走**（换月随版本更新）。
    判据 = benefit 含 "Care+" 且是手机类活动（nova无忧礼包/丢失无忧等不命中美掉）。
    ⚠ `compute` 和 `drill_rows` 都从这里取 —— 两头各写一份迟早走散。
    """
    return [a for a in claim_catalog.load_activities(root)
            if "Care+" in str((a or {}).get("benefit") or "")
            and str((a or {}).get("category") or "") == "手机"]


def new_bucket(c1: str, typ: str, name: str, sid: str, note: str, cust: str,
               care_models) -> str:
    """这行算不算「新机」—— `''` 不算 · `retail` 零售净 · `online` 美团/抖音转线上。

    ⚠ **唯一判据**：行归类看 `row_kind()`，新机那一档由这里定 ——
      `compute` 的累加和 `drill_rows()` 的明细底稿都走同一条路。
      判据写两份迟早走散（页面数字和明细合计对不上 —— 那是最贵的一种走散）。
    四类不算（用户 2026-09-26 拍板）：演示机/样机 · 送 Care+ 的折叠屏 ·
    普通批发分销（京东/天猫，非美团抖音转线上）· 非计件单据（核销等）。
    """
    if not metric.is_phone(c1):
        return ""
    if not metric.sell_ok(typ):
        return ""
    if metric.is_demo(name, sid):
        return ""
    if any(claim_catalog.matches_model(a, name) for a in care_models):
        return ""
    online = metric.is_meituan(note) or metric.is_online_cust(cust)
    if typ in ("分销", "分销退") and not online:
        return ""
    return "online" if online else "retail"


def row_kind(c1: str, c2: str, typ: str, name: str, sid: str, note: str,
             cust: str, care_models) -> str:
    """这行属于哪个指标 —— `''` 哪个都不算 · `new_retail`/`new_online` ·
    `care`（延保服务）· `tier`（无忧六档）。

    ⚠ **唯一判据**：`compute` 的累加和 `drill_rows()` 的明细底稿**都走它**。
      判据写两份迟早走散（页面数字和明细合计对不上 —— 那是最贵的一种走散）。
    ⚠ 顺序跟原来一致：计件单据 → 手机（新机，整行归它）→ Care+ → 无忧。
    """
    if not metric.sell_ok(typ):
        return ""
    if metric.is_phone(c1):
        b = new_bucket(c1, typ, name, sid, note, cust, care_models)
        if b == "retail":
            return "new_retail"
        if b == "online":
            return "new_online"
        return ""
    if metric.is_care(c2):
        return "care"
    if metric.tier_of(name):
        return "tier"
    return ""


#: 下钻指标表（**页面上能点的那些格**）—— kind → 归属行 / 取值字段 / 单位 / 标题 / 折算。
#:
#: ⚠ 金额类跟台数类**常常是同一批行换个取值字段**（无忧台数 vs 权益利润），
#:   所以判据还是那一份 `row_kind()`，别为金额类另写一遍行判定。
#: ⚠ **例外是 Care+ 那两个**：件数认全部延保服务行、利润只认
#:   `care_profit_ok`（含 `Care+` 且含 `/华为/`）的行 —— 页面本来就这么算的，
#:   金额类明细按 `field == "profit"` 时**把不计利润的 care 行整行剔掉**
#:   （不然毛利列会出现"有数但不计"的行，合计对不上）。
#: ⚠ **`profit_total`（利润合计）故意不做**：`metric.store_row` 里它是
#:   `rebate + care_profit + tier_profit` —— 后返是**台数×单价**，不是这些单的
#:   毛利；硬做的话底部合计跟毛利列的和对不上（2026-09-29 测试当场抓到 269≠169）。
DRILL_KINDS: Dict[str, tuple] = {
    "new":          (frozenset(("new_retail", "new_online")), "qty", "台",
                     "新机", 1.0),
    "wuyou":        (frozenset(("tier",)), "qty", "台", "无忧", 1.0),
    "care":         (frozenset(("care",)), "qty", "件", "Care+", 1.0),
    "total":        (frozenset(("tier", "care")), "qty", "件", "合计", 1.0),
    "tier_profit":  (frozenset(("tier",)), "profit", "元", "权益利润", 1.0),
    "care_profit":  (frozenset(("care",)), "profit", "元", "Care+利润", 1.0),
}


def _blank_store(name: str, cfg: dict) -> dict:
    track, target = metric.track_of(name, cfg.get("tracks") or {})
    region = metric.region_of(name, cfg.get("regions") or {})
    return {
        "new_retail": 0.0, "new_online": 0.0,
        "tiers": {k: 0.0 for k, _l, _f, _p in metric.TIERS},
        "tier_profit": 0.0, "care_qty": 0.0, "care_profit": 0.0,
        "_track": track, "_target": target, "_region": region,
    }


def _blank_person(store: str, name: str, title: str) -> dict:
    return {
        "store": store, "name": name, "title": title,
        "new_retail": 0.0, "new_online": 0.0,
        "tiers": {k: 0.0 for k, _l, _f, _p in metric.TIERS},
        "tier_profit": 0.0, "care_qty": 0.0, "care_profit": 0.0,
    }


def compute(root=None, stores: Optional[List[str]] = None, day=None,
            config_path=None, env_file=None) -> dict:
    root = Path(root or ROOT)
    start, end = month_window(day)
    d = day or datetime.date.today()
    cfg = load_config(root)

    # 名册：**系统人店表**（店 → 人）；网络失败才落 yaml
    roster, roster_src, roster_why = load_people(root, config_path, env_file)

    want = set(stores) if stores is not None else None
    # 店名单只来自**赛道 + 区域配置** —— 不拿名册反推店
    # （组织树里若还残留已关店，不会因为有人就冒出一行）
    track_stores = set()
    for _t, cfg_t in (cfg.get("tracks") or {}).items():
        s = (cfg_t or {}).get("stores") if isinstance(cfg_t, dict) else None
        if isinstance(s, dict):
            track_stores.update(s.keys())
    for members in (cfg.get("regions") or {}).values():
        track_stores.update(members or [])

    if want is None:
        names = sorted(track_stores)
    else:
        # 权限给了范围就只算范围内的（门店=本店，区长=所辖）
        names = sorted(want)

    acc = {n: _blank_store(n, cfg) for n in names}
    pacc: Dict[Tuple[str, str], dict] = {}
    for (st, nm), title in roster.items():
        if st in acc:  # 只给**范围内且在赛道/区域表里**的店建人行
            pacc[(st, nm)] = _blank_person(st, nm, title)

    orphan = 0
    orphan_names: Dict[str, int] = {}
    #: 备注点名别家门店的行 —— **2026-09-29 拍板：算转出店**，不再剔。
    #: 只记台账、在 note 里报出来（不漏不重要看得见，别静默改数）。
    transfer = 0
    fidx: Dict = {}
    as_of = ""
    src = "missing"
    db = find_db(root)
    if db and db.exists():
        src = "erp_sales"
        # 倒排索引只建一次（读 config/stores.yaml）；名单读不到 = 空 = 一个都不认
        fidx = foreign_mod.name_index(root)
        # 送 **Care+** 的机型（折叠屏金秋礼遇活动）不算新机 ——
        # 用户 2026-09-26 拍板；判据见 `care_models_of`（明细下钻同源）。
        care_models = care_models_of(root)
        for r in _load_rows(db, start, end):
            store = r["store"]
            if want is not None and store not in want:
                continue
            if r["ts"] and r["ts"] > as_of:
                as_of = r["ts"]
            a = acc.get(store)
            pa = pacc.get((store, r["who"])) if r["who"] else None

            if a is None:
                # 店不在赛道/区域表（联想店、已关店）—— 整行跳过
                continue
            # ⚠ 备注点名**别家门店**（转单/转线上）→ **算转出店**（行上的门店）。
            #   用户 2026-09-29 覆盖 2026-09-26 的「整行剔」：
            #   「算转出店的，因为这个是转出店没有这个线上平台，通过别的店走的量，
            #    增值业务肯定要算是原门店的销售」—— 整行剔会两边都算不着
            #   （9 月实测：报表内门店 29 行 / 净 27 台 / 8499 元）。
            #   判据仍走 foreign（识别 + 台账），只是不再 continue。
            if foreign_mod.other_store_in(store, r.get("note"), fidx):
                transfer += 1
            if pa is None and r["who"]:
                # 有单但不在人店表：**计入店、不进人榜**（note 里报笔数）
                orphan += 1
                orphan_names[r["who"]] = orphan_names.get(r["who"], 0) + 1

            # ⚠ 归类走 `row_kind()` —— 它里面先问 `sell_ok`（核销等不计），
            #   再按 手机→新机 / 延保→Care+ / 无忧六档 归位；
            #   明细下钻（`drill_rows`）问的是同一个函数，合计才对得上。
            k = row_kind(r["c1"], r["c2"], r["typ"], r["name"], r.get("sid") or "",
                         r["note"], r.get("cust") or "", care_models)
            if k == "new_retail":
                for tgt in (a, pa):
                    if tgt is not None:
                        tgt["new_retail"] += r["qty"]
            elif k == "new_online":
                for tgt in (a, pa):
                    if tgt is not None:
                        tgt["new_online"] += r["qty"]
            elif k == "care":
                for tgt in (a, pa):
                    if tgt is None:
                        continue
                    tgt["care_qty"] += r["qty"]
                    # ⚠ 达成件数照算，**利润**只认「含 Care+ 且含 /华为/」的行
                    #   （用户 2026-09-26 拍板；Mate XT 那类 899、延长服务宝、
                    #    matepad Care+ 12月都不算利润）
                    if metric.care_profit_ok(r["name"]):
                        tgt["care_profit"] += r["profit"]
            elif k == "tier":
                key = metric.tier_of(r["name"])
                for tgt in (a, pa):
                    if tgt is None:
                        continue
                    tgt["tiers"][key] = tgt["tiers"].get(key, 0.0) + r["qty"]
                    tgt["tier_profit"] += r["profit"]

    # 店行
    store_rows = []
    for n in sorted(acc.keys()):
        a = acc[n]
        if want is not None and n not in want:
            continue
        # 没有任何动静的店也列出（赛道成员要有行，进度 0 也看得见）
        store_rows.append(metric.store_row(
            n,
            track=a["_track"],
            day_target=a["_target"],
            region=a["_region"],
            new_retail=a["new_retail"],
            new_online=a["new_online"],
            tiers=a["tiers"],
            tier_profit=a["tier_profit"],
            care_qty=a["care_qty"],
            care_profit=a["care_profit"],
            day=d.day,
        ))

    # 区域行
    by_region: Dict[str, List[dict]] = {}
    for r in store_rows:
        by_region.setdefault(r.get("region") or "未分组", []).append(r)
    region_order = list((cfg.get("regions") or {}).keys())
    region_rows = []
    for name in region_order:
        if name in by_region:
            region_rows.append(metric.region_row(name, stores=by_region.pop(name)))
    for name in sorted(by_region.keys()):
        region_rows.append(metric.region_row(name, stores=by_region.pop(name)))

    # 人行
    person_rows = []
    for (_st, _nm), pa in pacc.items():
        # 名册里的人即使没单也出行（方便盯零达成）
        person_rows.append(metric.person_row(
            pa["store"], pa["name"], pa["title"],
            new_retail=pa["new_retail"], new_online=pa["new_online"],
            tiers=pa["tiers"], tier_profit=pa["tier_profit"],
            care_qty=pa["care_qty"], care_profit=pa["care_profit"],
        ))
    # 名册外但有单的人（店在范围内）—— 已在 orphan 里计；从 store 侧无法还原姓名则不追加
    person_rows = metric.rank_people(person_rows)

    # 赛道奖
    tracks_cfg = cfg.get("tracks") or {}
    track_bonus = metric.allocate_all_tracks(tracks_cfg, store_rows)
    # 店行带上应得分成（方便表里直接显示）
    paid_by_store = {}
    for t in track_bonus:
        for row in t["rows"]:
            paid_by_store[row["store"]] = row

    for r in store_rows:
        b = paid_by_store.get(r["store"])
        r["manager_bonus"] = (b or {}).get("paid") or 0.0
        r["manager_rank"] = (b or {}).get("rank") or ""

    note = "本地库现算 · 本月 1 号起 · 名册=%s" % (
        "系统人店表" if roster_src == "system" else "无名册")
    if roster_why and roster_src != "system":
        note += "（%s）" % roster_why
    if src == "missing":
        note = "找不到订单库（out/cbg-*.db）—— 先跑「抓数据」"
    if orphan:
        note += " · 名册外有单 %d 笔（已计入店、未进人榜）" % orphan
    if transfer:
        # ⚠ 必须报出来 —— 转单行**计入本店**这件事要看得见（不漏不重的台账，坑13 同类）
        note += " · 备注点名别家门店 %d 行（已计入本店=转出店）" % transfer

    return {
        "ok": src != "missing",
        "why": "" if src != "missing" else note,
        "start": start,
        "end": end,
        "day": d.day,
        "progress": round(min(1.0, d.day / metric.DAYS_IN_MONTH), 4),
        "as_of": as_of,
        "src": src,
        "note": note,
        "orphan": orphan,
        "transfer_rows": transfer,
        "roster_source": roster_src,
        "roster_why": roster_why,
        "stores": store_rows,
        "regions": region_rows,
        "people": person_rows,
        "tracks": track_bonus,
        "summary": metric.summarize_stores(store_rows),
        "summary_regions": metric.summarize_regions(region_rows),
        "summary_people": metric.summarize_people(person_rows),
        "tiers_meta": [{"key": k, "label": lab, "rebate": p}
                       for k, lab, _f, p in metric.TIERS],
    }


def drill_rows(root=None, store: str = "", kind: str = "new", day=None) -> dict:
    """某店本窗口**某个指标**纳入统计的销售行 —— 页面那一格数字的明细底稿。

    `kind` 见 `DRILL_KINDS`（页面上能点的那些格：新机 / 无忧 / Care+ / 合计 /
    三份利润）。列（用户 2026-09-29 点名，同日追加「带上毛利吧」）：
    **销售单号 · 单据类型 · 商品名称 · 数量 · 销售时间 · 毛利**。
    **含退货**：源数据里退货数量本来就是负数，进明细也进合计（直接相加）。

    ⚠ 行判定 = `row_kind()`（与 `compute` 同一个）⇒ `合计` 必须等于页面那格的值
      （本页**不乘 0.9**，与防护膜页不同）。改口径只改 `row_kind` / `DRILL_KINDS`。
    ⚠ **金额类的 Care+ 行只认 `care_profit_ok`** —— 件数认全部延保服务行、
      利润只认「含 Care+ 且含 /华为/」的行；不这么收，毛利列会出现
      "有数但不计"的行、合计对不上（页面 care_profit 本来就是这么算的）。
    ⚠ 老库可能没有 `单号` 列 —— `PRAGMA` 问一下，缺就补 `'' AS 单号`。
    """
    spec = DRILL_KINDS.get(kind)
    if spec is None:
        return {"ok": False, "why": "不认识的指标：%s" % kind}
    kinds, field, unit, label, factor = spec
    root = Path(root or ROOT)
    start, end = month_window(day)
    store = str(store or "").strip()
    if not store:
        return {"ok": False, "why": "没指定门店"}
    db = find_db(root)
    if not (db and db.exists()):
        return {"ok": False, "why": "找不到订单库（out/cbg-*.db）—— 先跑「抓数据」"}
    end_ex = (datetime.date.fromisoformat(end)
              + datetime.timedelta(days=1)).isoformat()
    care_models = care_models_of(root)
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows: List[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        try:
            have = {str(x[1]) for x in conn.execute("PRAGMA table_info(erp_sales)")}
        except sqlite3.Error:
            have = set()
        no = '"单号"' if "单号" in have else "'' AS \"单号\""
        cust = '"客户/顾客"' if "客户/顾客" in have else "'' AS \"客户/顾客\""
        sid = "串号标识" if "串号标识" in have else "'' AS 串号标识"
        sql = ("SELECT " + no + ", 单据类型, 一级分类, 二级分类, 商品名称, 数量,"
               " 零售考核毛利, 支付时间, 备注, 单行备注, " + cust + ", " + sid +
               " FROM erp_sales"
               " WHERE 支付时间 >= ? AND 支付时间 < ? AND 门店 = ?")
        for r in conn.execute(sql, (start, end_ex, store)):
            # ⚠ `note` 只拼备注+单行备注，客户字段单独传（同 `_load_rows`）
            note = " ".join(str(r[k] or "") for k in ("备注", "单行备注"))
            k = row_kind(r["一级分类"] or "", r["二级分类"] or "",
                         r["单据类型"] or "", r["商品名称"] or "",
                         r["串号标识"] or "", note, r["客户/顾客"] or "", care_models)
            if k not in kinds:
                continue
            if field == "profit" and k == "care" and \
                    not metric.care_profit_ok(r["商品名称"] or ""):
                continue                      # 这行的毛利页面上没计 —— 不进金额明细
            rows.append({
                "no": r["单号"] or "",
                "typ": r["单据类型"] or "",
                "name": r["商品名称"] or "",
                "qty": _f(r["数量"]),
                "profit": _f(r["零售考核毛利"]),
                "ts": r["支付时间"] or "",
            })
    finally:
        conn.close()
    rows.sort(key=lambda x: (str(x["ts"]), str(x["no"])), reverse=True)
    total = sum((x["qty"] if field == "qty" else x["profit"]) for x in rows)
    shown = round(total * factor, 2)
    if field == "qty":
        note = "合计是原始%s数（退货是负数、已在合计里冲减）；本页不乘 0.9，就是页面上的「%s」" % (
            unit, label)
    else:
        note = "合计是这些单的零售考核毛利之和，就是页面上的「%s」" % label
    return {
        "ok": True, "why": "",
        "store": store, "start": start, "end": end,
        "kind": kind, "label": label, "unit": unit, "field": field,
        "rows": rows, "total": total, "factor": factor, "shown": shown,
        "note": note,
    }


_CACHE: Dict[str, dict] = {}
_FINGER: Dict[str, tuple] = {}


def _fingerprint(db: Optional[Path], start: str, end: str, *cfg_paths) -> tuple:
    parts = [start, end]
    for p in (db,) + tuple(cfg_paths):
        if p and Path(p).exists():
            st = Path(p).stat()
            parts.append((str(p), int(st.st_mtime_ns), int(st.st_size)))
        else:
            parts.append(None)
    return tuple(parts)


def load(root=None, stores: Optional[List[str]] = None, day=None, force: bool = False,
         config_path=None, env_file=None) -> dict:
    """带指纹缓存的入口（库 / 赛道配置 / 人店表缓存没变就不重算）。"""
    root = Path(root or ROOT)
    start, end = month_window(day)
    db = find_db(root)
    key = "all" if stores is None else ",".join(sorted(stores))
    from ...store import staff as _staff_mod
    fp = _fingerprint(
        db, start, end,
        root / "config" / "valueadd-benefit.yaml",
        root / _staff_mod.ROSTER_REL,
        # 转单识别/台账读门店名单（foreign.name_index）—— 改了名单必须重算，
        # 否则 note 里那句「备注点名别家门店 N 行」会陈旧（**只影响台账，不影响台量**）
        root / "config" / "stores.yaml",
    )
    if not force and _FINGER.get(key) == fp and key in _CACHE:
        return _CACHE[key]
    d = compute(root, stores=stores, day=day,
                config_path=config_path, env_file=env_file)
    _CACHE[key] = d
    _FINGER[key] = fp
    return d


def run(root=None, emit=None) -> dict:
    """步骤 `benefit`：算一遍并落快照 `out/benefit.json`（页面仍现算）。"""
    root = Path(root or ROOT)
    from ....modules import health
    res = health.begin("benefit", note="无忧会员权益", root=root)
    try:
        d = load(root, force=True)
        if not d.get("ok"):
            why = str(d.get("why") or "权益数据没算成")
            res.done(ok=False, why=why)
            if emit:
                emit("  失败：" + why)
            return {"ok": False, "why": why, "rows": 0}
        out = root / "out"
        out.mkdir(parents=True, exist_ok=True)
        payload = dict(d)
        payload["_saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        tmp = out / "benefit.json.tmp"
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                           encoding="utf-8")
            tmp.replace(out / "benefit.json")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        res.done(ok=True, rows=len(d.get("stores") or []))
        if emit:
            emit("  门店 %d · 区域 %d · 人员 %d（名册=%s） · 快照 out/benefit.json" % (
                len(d.get("stores") or []), len(d.get("regions") or []),
                len(d.get("people") or []), d.get("roster_source") or "?"))
        return {"ok": True, "why": "", "rows": len(d.get("stores") or [])}
    except Exception as e:
        res.done(ok=False, why=str(e))
        if emit:
            emit("  失败：" + str(e))
        return {"ok": False, "why": str(e)}


__all__ = ["compute", "load", "run", "load_config", "load_people",
           "month_window", "find_db", "new_bucket", "row_kind", "drill_rows",
           "care_models_of", "DRILL_KINDS"]
