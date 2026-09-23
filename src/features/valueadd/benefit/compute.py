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
from . import metric


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
        sql = ("SELECT 门店, 店员, 单据类型, 商品名称, 一级分类, 二级分类,"
               " 数量, 零售考核毛利, 支付时间, 备注, 单行备注"
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
            }
    finally:
        conn.close()


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
    as_of = ""
    src = "missing"
    db = find_db(root)
    if db and db.exists():
        src = "erp_sales"
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
            if pa is None and r["who"]:
                # 有单但不在人店表：**计入店、不进人榜**（note 里报笔数）
                orphan += 1
                orphan_names[r["who"]] = orphan_names.get(r["who"], 0) + 1

            if not metric.sell_ok(r["typ"]):
                continue

            # —— 新机（手机）
            if metric.is_phone(r["c1"]):
                bucket = "new_o" if metric.is_meituan(r["note"]) else "new_r"
                for tgt in (a, pa):
                    if tgt is None:
                        continue
                    if bucket == "new_r":
                        tgt["new_retail"] += r["qty"]
                    else:
                        tgt["new_online"] += r["qty"]
                continue

            # —— Care+
            if metric.is_care(r["c2"]):
                for tgt in (a, pa):
                    if tgt is None:
                        continue
                    tgt["care_qty"] += r["qty"]
                    tgt["care_profit"] += r["profit"]
                continue

            # —— 无忧六档
            key = metric.tier_of(r["name"])
            if key:
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

    return {
        "ok": src != "missing",
        "why": "" if src != "missing" else note,
        "start": start,
        "end": end,
        "day": d.day,
        "progress": round((d.day - 1) / metric.DAYS_IN_MONTH, 4),
        "as_of": as_of,
        "src": src,
        "note": note,
        "orphan": orphan,
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
        out = root / "out"
        out.mkdir(parents=True, exist_ok=True)
        payload = dict(d)
        payload["_saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
        (out / "benefit.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        ok = bool(d.get("ok"))
        res.done(ok=ok, rows=len(d.get("stores") or []),
                 why="" if ok else str(d.get("why") or ""))
        if emit:
            emit("  门店 %d · 区域 %d · 人员 %d（名册=%s） · 快照 out/benefit.json" % (
                len(d.get("stores") or []), len(d.get("regions") or []),
                len(d.get("people") or []), d.get("roster_source") or "?"))
        return {"ok": ok, "why": d.get("why") or "", "rows": len(d.get("stores") or [])}
    except Exception as e:
        res.done(ok=False, why=str(e))
        if emit:
            emit("  失败：" + str(e))
        return {"ok": False, "why": str(e)}


__all__ = ["compute", "load", "run", "load_config", "load_people",
           "month_window", "find_db"]
