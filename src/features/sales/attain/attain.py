"""销售达成的 **IO 层** —— 读腾讯文档、查库、落盘、推进送。

口径在 `metric.py`（纯函数），这儿只管"东西从哪来、往哪去"。
分两个文件是**唯一能让口径脱离网络和数据库单测**的办法（照 `pos_metric` 那套）。

## 一步一步（每步失败都要说清是**哪一步**）

```
腾讯文档「任务目标分配」
  ├── tab 周度重点产品   → 权重行 + 门店行（台量）
  └── tab 周度重点映射表 → 产品列 → 商品编码集合 + 期间（C1/D1）
        ↓
   Plan（规范化目标表）
        ↓  +  erp_sales（期间内、已剔除演示机/体验机/外调、只留四种单据类型）
   compute()  →  落盘字典（形状 = `/api/attain` 的返回）
        ↓
   out/attain-<年>.json
```

## ⚠⚠ **不许在这里回落任何默认值**

读不到期间 / 读不到权重 / 列数不等 —— 一律往上抛。
**"空目标"和"没读到"必须能分开**：合成一个的后果是
**"达成率全 0 的一份很合理的假报告"**（设计 §3.3、验收标准第 3 条）。
`tests/test_attain.py::Test假数据必须炸` 就是钉这条的。

## 红线（`开发目标.md` 第八节）

* **不写回腾讯文档**（只读）；**不做排名、不算奖惩金额**；月度不做；
* `reconcile.py` / `metric` 以外的老口径文件**一行不改**。
"""

from __future__ import annotations

import datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import metric as M

#: 腾讯文档（任务目标分配）—— 复用 M1 已经做好的匿名读
DOC = "DTE90WnJBWUhtUHhS"

#: 落盘文件名（一年一个，只存"最近算出来的那一期"）
FILE_FMT = "attain-%d.json"


class AttainError(RuntimeError):
    """目标表 / 数据源的问题 —— **带"是哪一步"**（排查时省一轮）。"""


# ------------------------------------------------------------------ 读目标表
def region_map(grid: dict) -> Dict[int, str]:
    """A 列区域名 → 按行**向下填充**（合并单元格只有第一行有值）。

    ⚠ 一个合并块里可能写着**两个区域**（实测 A2 = `城阳\\n胶州`，合并 8 行）——
      所以原样留着，**不拆**（本版前端不用它，办公室那份汇总才用）。
    """
    out, cur = {}, ""
    for row in range(0, 200):
        val = grid.get((row, 0))
        if isinstance(val, str) and val.strip():
            cur = val.strip()
        out[row] = cur
    return out


def read_plan(root=None, doc: str = DOC, session=None) -> M.Plan:
    """腾讯文档 → `Plan`。**这是 M2 的全部。**

    ⚠ 期间是**从文档 C1/D1 推出来的**，不是"当前周"：办公室忘改表，
      我们就会照它算**上一周** —— 所以要提醒（界面上标"数据截至"），
      但**不硬失败**（周一早上办公室可能还没改完）。
    """
    from ....integrations import tdoc
    try:
        # ⚠ 一张 HTML 读两遍 tab：`read_tab` 每次都重下首页，干脆自己拿着用
        html = tdoc.fetch_html(doc, session=session)
        m_grid, _r, _n = _grid_of(tdoc, tdoc.MAPPING_TAB, doc, html, session)
        t_grid, rich, _n2 = _grid_of(tdoc, tdoc.TARGET_TAB, doc, html, session)
    except AttainError:
        raise
    except Exception as e:                                     # noqa: BLE001
        raise AttainError("读腾讯文档失败（网络 / 文档权限 / 表被改名）：%s: %s"
                          % (type(e).__name__, e))
    return build_plan(m_grid, t_grid, rich=rich)


def _grid_of(tdoc, name: str, doc: str, html, session):
    """按名字取一个 tab 的 `(grid, rich, numbers)` —— 复用已经拿到的那份 HTML。"""
    text0 = tdoc.fetch_tab(doc, html=html, session=session)
    tab_id = tdoc.resolve_tab(text0, html, name)
    if tab_id != tdoc.default_tab_id(html):
        text0 = tdoc.fetch_tab(doc, tab_id=tab_id, html=html, session=session)
    return tdoc.decode_grid(text0)


def build_plan(m_grid: dict, t_grid: dict, rich=None) -> M.Plan:
    """两张 grid → `Plan` —— **纯拼装**（网络那半在 `read_plan`，这样好测）。"""
    from ....integrations import tdoc
    info = tdoc.read_mapping(m_grid)                  # 抛 TdocError：表空 / C1D1 没日期
    targets = tdoc.read_targets(t_grid, rich=rich,
                               mapping_columns=[c[0] for c in info["columns"]])
    if len(targets["columns"]) != len(info["columns"]):
        raise AttainError("映射表 %d 列 vs 目标表 %d 列，对不上"
                          % (len(info["columns"]), len(targets["columns"])))
    codes_by_name = {tdoc.norm_column(name): codes for name, codes in info["columns"]}
    regions = region_map(t_grid)

    columns = []
    for i, name in enumerate(targets["columns"]):
        codes = codes_by_name.get(tdoc.norm_column(name))
        if codes is None:
            raise AttainError("目标表的列『%s』在映射表里没有对应行 —— 表被改坏了？" % name)
        columns.append(M.Column(name=name, codes=frozenset(str(c).strip() for c in codes),
                                weight=float(targets["weights"][i])))
    stores = []
    for row_i, (store, qtys) in enumerate(targets["rows"]):
        # `read_targets` 跳过了「合计」行，所以行号不能直接拿来查区域 ——
        # 用门店名在 grid 里反查它那一行（B 列就是门店名）
        r = _row_of_store(t_grid, store)
        stores.append(M.StorePlan(name=store, region=regions.get(r, ""),
                                  targets=tuple(int(q) for q in qtys)))
    if not columns:
        raise AttainError("一个产品列都没有 —— 表被改坏了？")
    return M.Plan(period=M.period_of(info["start"], info["end"]),
                  start=info["start"], end=info["end"],
                  columns=tuple(columns), stores=tuple(stores))


def _row_of_store(grid: dict, store: str) -> int:
    """门店名 → 它在 grid 里的行号（区域填充要靠它）。找不到给 -1（区域就空着）。"""
    for (row, col), val in grid.items():
        if col == 1 and isinstance(val, str) and val.strip() == store:
            return row
    return -1


# ------------------------------------------------------------------ 匹配门店
def match_store(name: str, root=None) -> Optional[dict]:
    """文档门店名 → `config/stores.yaml` 里那一条。

    先按 `erp_name` 严格相等，再按 `tdoc_name` 严格相等（别名，见 stores.yaml 里那段）。

    ⚠ **只做 `strip()`，别的加工一概不做** —— 名单那 29 家是手工维护的，
      模糊一下就会配到隔壁店，而界面上**看不出来**（设计 §0.2）。
    """
    from .... import config_io
    from ....paths import ROOT
    rows = config_io.stores_table(root or ROOT)
    key = str(name or "").strip()
    for want in ("erp_name", "tdoc_name"):
        for r in rows:
            if str(r.get(want) or "").strip() == key and key:
                return r
    return None


def store_map(plan: M.Plan, root=None) -> Dict[str, str]:
    """`{文档门店名: 云商门店名}` —— 没匹配上的不出现在表里（`all_results` 会给它 `None`）。"""
    out = {}
    for sp in plan.stores:
        row = match_store(sp.name, root)
        if row and str(row.get("erp_name") or "").strip():
            out[sp.name] = str(row["erp_name"]).strip()
    return out


# ------------------------------------------------------------------ 取销售
#: 一次取数要的列（**一次全查**，别按列查 9 次 —— 表上没有可用的索引，
#: 每次都要全表扫；实测 9 次 ≈ 100ms，全区 28 店 × 9 列 ≈ 2.8 秒）
SQL = ("SELECT 门店, 商品编码, 数量, 单据类型, 商品名称, 串号标识, 店员 "
       "FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 <= ?")


def load_sales(conn: sqlite3.Connection, start, end) -> Tuple[List[M.Sale], dict]:
    """期间内的销售行 → `(sales, dropped)`。

    `dropped` 是**剔除统计**：`{"演示机/体验机": n, "外调": n, "单据类型不计入": n}` ——
    ⚠ 静默剔除是查不出来的（`pools` 那边吃过"数字对不上又找不到人"的亏）。

    ⚠ 剔除放在 **Python 侧**（不在 SQL 里 `NOT LIKE`）：
      ① SQL 里剔了就**数不着**了，而"剔了几行"要能报出来；
      ② `erp_sales` 没有可用索引，`LIKE` 会让全表扫再来一遍。
    ⚠ 期间是**左闭右闭**（`<= end`），传进来的 `end` 已经是那天的日期。
    """
    lo = "%s 00:00:00" % start
    hi = "%s 23:59:59" % end
    dropped: Dict[str, int] = {}
    sales: List[M.Sale] = []
    rows = conn.execute(SQL, (lo, hi))
    for store, code, qty, kind, product, mark, who in rows:
        why = M.drop_reason(kind, product, mark)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
            continue
        sales.append(M.Sale(store=str(store or ""), code=str(code or "").strip(),
                            qty=M.qty_of(qty), kind=str(kind or ""),
                            who=str(who or "").strip(),
                            product=str(product or "").strip()))
    return sales, dropped


def group_by_store(sales) -> Dict[str, List[M.Sale]]:
    out: Dict[str, List[M.Sale]] = {}
    for s in sales:
        out.setdefault(s.store, []).append(s)
    return out


def data_until(conn: sqlite3.Connection, start, end) -> str:
    """期间内 `MAX(支付时间)` 的**日期部分**（不分店 —— 见设计 §5.1）。

    ⚠ 分店算的话，某店这周没卖就成空字符串，界面上会显示成"没有数据"，
      而实际只是"这家没卖"。所以**全区分一个**。
    """
    lo, hi = "%s 00:00:00" % start, "%s 23:59:59" % end
    row = conn.execute("SELECT MAX(支付时间) FROM erp_sales WHERE 支付时间 >= ? AND 支付时间 <= ?",
                       (lo, hi)).fetchone()
    val = str((row or [""])[0] or "")
    return val[:10]


# ------------------------------------------------------------------ 算 + 落盘
def compute(plan: M.Plan, sales, *, conn=None, store_filter: str = "",
            root=None) -> dict:
    """`Plan` + 销售行 → 落盘字典（形状 = `/api/attain` 的返回）。

    ⚠ **本店过滤在这儿做，不在接口层**：门店端只要自己那一行，
      办公室 / 平台岗要全部（设计 §5.3）—— 放接口层的话，
      落盘里就没有全区那份了，办公室那份还得再算一次。
    """
    smap = store_map(plan, root)
    rows = M.all_results(plan, group_by_store(sales), store_map=smap)
    if store_filter:
        rows = [r for r in rows if r.erp_name == store_filter or r.store == store_filter]
    missing = [c.name for c in plan.columns if not c.codes]
    row_dicts = [r.as_dict() for r in rows]
    # ⭐ 落盘时就用 stores.yaml 的区域盖掉腾讯文档 A 列（用户 2026-09-22：
    #   「周度重点产品分区错了」—— 文档合并块是 `城阳\n胶州`，不是西北区/市区/南区）。
    #   接口层读老文件时还会再盖一遍（web.attain），这里先保证新算的就对。
    try:
        from ..plan.monthly.plan import stores_by_region
        regmap = stores_by_region(root)
    except Exception:                                         # noqa: BLE001
        regmap = {}
    if regmap:
        for rd in row_dicts:
            key = str(rd.get("erp_name") or rd.get("store") or "").strip()
            if regmap.get(key):
                rd["region"] = regmap[key]
    weights = [float(c.weight) for c in plan.columns]
    return {
        "exists": True,
        "period": plan.period,
        "start": plan.start.isoformat(),
        "end": plan.end.isoformat(),
        "data_until": (data_until(conn, plan.start, plan.end) if conn is not None else ""),
        "computed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "columns": list(plan.column_names()),
        "weights": weights,
        "missing_columns": missing,
        "rows": row_dicts,
        # 分区汇总「共计」行 —— 后端算好再下发（照 film）；接口层滤完店还会重算
        "region_sums": M.region_sums(row_dicts, weights),
    }


def save(root, payload: dict, year: Optional[int] = None) -> Path:
    """落 `out/attain-<年>.json` —— **先写临时文件再 rename**（照 `pools_history` 的做法）。

    ⚠ 直接覆盖写的话，中途断电 / 被杀就是一个半截 JSON，
      而前端读它时只会说"读不出来"（跟"还没算过"长得一样）。
    """
    root = Path(root)
    out = root / "out"
    out.mkdir(parents=True, exist_ok=True)
    # ⚠ 别写成 `int(str(...)[:4]) or today` —— `int("")` 先抛 ValueError，
    #   右边的兜底根本轮不到（2026-09-20 被一条"空 payload"的用例抓到）。
    head = str(payload.get("start") or "")[:4]
    y = year or (int(head) if head.isdigit() else datetime.date.today().year)
    path = out / (FILE_FMT % y)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def load(root, year: Optional[int] = None) -> dict:
    """读回最新算出来的那一期（`/api/attain` 用的是这个）。**读不到给 `exists: False`。**"""
    root = Path(root)
    y = year or datetime.date.today().year
    p = root / "out" / (FILE_FMT % y)
    if not p.is_file():
        # 跨年那一刻还没跑过今年的 ⇒ 退回去年那份（别让界面上"空掉"）
        cands = sorted((root / "out").glob("attain-*.json"))
        if not cands:
            return {"exists": False,
                    "error": "还没算过 —— 先跑一次 daily（或点右下角「跑一次」）"}
        p = cands[-1]
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001
        return {"exists": False, "error": "落盘文件读不出来（%s）：%s" % (p.name, e)}
    d["exists"] = True
    d["file"] = p.name
    return d


# ------------------------------------------------------------------ 执行入口
def find_db(root) -> Optional[Path]:
    """订单库（和 POS 那条链**同一个口径** —— `app/data_state` 说了算）。"""
    from ....app import data_state
    found = data_state._find_db(Path(root))
    return Path(found) if found else None


def run(*, db: str = "", config_path=None, root=None, store_filter: str = "",
        no_push: bool = False, no_mail: bool = False, emit=None) -> dict:
    """**算一次**：读目标表 → 查库 → 核算 → 落盘 → 推送。返回具名结果（**不抛**）。

    ⚠ 推送失败**不影响 `ok`** —— 达成数字才是主产物，推送只是投递方式
      （和 POS 那条链同一条规矩）。四态写在 `res["notices"]` 里，能被断言。

    ⚠ 本店过滤：门店端只看自己那一行，办公室 / 平台岗看全区（`store_filter=""`）。
      默认**自己不猜** —— 由调用方（cli / daily）按 `store_profile` 传进来。
    """
    from ....paths import ROOT
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    conn = None
    try:
        plan = read_plan(root)
    except Exception as e:                                     # noqa: BLE001
        return {"ok": False, "why": "读目标表失败：%s" % e}
    say("销售达成 → %s（%s ~ %s）" % (plan.period, plan.start, plan.end))
    path = Path(db) if db else find_db(root)
    if not path or not path.is_file():
        return {"ok": False, "why": "没找到订单库（out/cbg-<年>.db）—— 先跑一次 dump",
                "plan": plan}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        sales, dropped = load_sales(conn, plan.start, plan.end)
        payload = compute(plan, sales, conn=conn, store_filter=store_filter, root=root)
    except sqlite3.Error as e:
        return {"ok": False, "why": "查库失败：%s" % e, "plan": plan}
    finally:
        if conn is not None:
            conn.close()
    ours = [r for r in payload["rows"]]
    if not ours:
        return {"ok": False, "why": "这家店不在目标表里（门店名对不上？）：%s"
                % (store_filter or "本店"), "plan": plan, "payload": payload}
    if dropped:
        say("  （剔除了 %s）" % "、".join("%s %d 行" % kv for kv in sorted(dropped.items())))
    for r in ours:
        rate = "—" if r["total"] is None else "%.2f%%" % (r["total"] * 100)
        say("  %s：%s%s" % (r["store"], rate, "" if r["matched"] else "（门店名没匹配上）"))
    # ⚠ **先归档上一周，再覆盖落盘**（用户 2026-09-20 要的"锁住存档"）——
    #   顺序反了的话：新的一覆盖，上一周就没了（那是不可逆的丢数据）。
    rolled = archive_rolled(root, payload, emit=say)
    out = save(root, payload)
    say("  已落盘：%s" % out.name)
    # 周一兜底重发（用户选的："拆完点保存就发 + **每周一兜底重发**"）。
    # ⚠ 只在周一且这周还没发过时才动；平时一句话都不说。
    try:
        from . import split as _split
        again = _split.maybe_weekly_send(root, cfg=None, emit=say)
        if again.get("sent"):
            say("  （周一兜底：又给区长发了一次 —— %s）" % "、".join(again.get("did") or []))
    except Exception as e:                                     # noqa: BLE001
        say("  ⚠ 周一兜底重发没成：%s: %s" % (type(e).__name__, e))
    notices = notify(payload, config_path=config_path, no_push=no_push,
                     no_mail=no_mail, store=store_filter, root=root, emit=emit)
    return {"ok": True, "plan": plan, "payload": payload, "dropped": dropped,
            "out_path": str(out), "notices": notices, "archived": rolled}


def main(argv=None) -> int:
    """`python -m src.cli attain` 的入口 —— 返回**退出码**，不抛 `SystemExit`（坑 11）。"""
    import argparse
    from ....paths import ROOT
    ap = argparse.ArgumentParser(prog="attain", description="周度销售达成")
    ap.add_argument("--db", default="")
    ap.add_argument("-c", "--config", default=None)
    ap.add_argument("--store", default="", help="只看这家云商门店（默认：全区）")
    ap.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    ap.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    args = ap.parse_args(argv)
    store = args.store
    if not store and args.config:
        from .... import config_io
        try:
            cfg = config_io.load_raw(Path(args.config) if Path(args.config).is_absolute()
                                     else ROOT / args.config) or {}
            if config_io.store_profile(cfg, ROOT).get("type") != "platform":
                store = cfg.get("erp_store_name") or ""
        except Exception:                                      # noqa: BLE001
            store = ""
    res = run(db=args.db, root=ROOT, store_filter=store, config_path=args.config,
              no_push=args.no_push, no_mail=args.no_mail, emit=print)
    if not res.get("ok"):
        print("[销售达成] 没算成：%s" % res.get("why"), file=__import__("sys").stderr)
        return 9
    return 0


# ------------------------------------------------------------------ 推送（M5）
def _one_row(payload: dict, store: str = "") -> Optional[dict]:
    """本店那一行 —— 门店端 `rows` 只有一行，办公室端要挑（挑不到给 None）。"""
    rows = payload.get("rows") or []
    if store:
        for r in rows:
            if r.get("erp_name") == store or r.get("store") == store:
                return r
        return None
    return rows[0] if rows else None


def notify_lines(payload: dict, store: str = "") -> Tuple[str, List[str]]:
    """推送正文 —— **邮件和企微共用这一份**（照 POS 那条规矩：只定义一次）。

    内容 = 设计 §6.2：期间 + 总达成率 + 逐列「达成率 实际/目标」+ 数据截至。
    """
    row = _one_row(payload, store)
    if not row:
        return "", []
    cols = payload.get("columns") or []
    total = row.get("total")
    head = "%s 总达成率 %s" % (payload.get("period") or "",
                              "—" if total is None else "%.1f%%" % (total * 100))
    lines = []
    for i, name in enumerate(cols):
        rate = (row.get("rates") or [None] * len(cols))[i]
        if rate is None:
            lines.append("%s：—（这列还没配编码）" % name)
            continue
        lines.append("%s：%.1f%%　%d/%d 台"
                     % (name, rate * 100, (row.get("actuals") or [0])[i],
                        (row.get("targets") or [0])[i]))
    until = payload.get("data_until") or ""
    if until and until != (payload.get("end") or ""):
        # ⚠ 两个原因都会让数字偏低，**分开说**（设计 §5.4 第 3 条）：
        #   ① 这周还没过完；② 库里的数据只到某天。混成一句门店会以为系统坏了。
        lines.append("⚠ 数据截至 %s（周中累计，不是最终达成）" % until)
    return head, lines


def notify(payload: dict, *, config_path=None, no_push: bool = False,
           no_mail: bool = False, store: str = "", root=None, emit=None) -> dict:
    """把算出来的达成推出去（企微 / 邮件各一条），返回**四态** `notices`。

    用户 2026-09-19 划的分工：**该不该推在这层判**（`should_send` / 两个 `--no*`），
    发出去走 `modules.notify.send(渠道, 内容)`（它只管发 + 记各渠道）。

    ⚠⚠ **不许套「只有差异才推」**（`has_diff`）—— 达成**每天都有数，可能就是 0%**，
      而 0% 恰恰是最该推的那条。所以一律按 `has_diff=True` 走：
      那个开关的语义是"有没有内容值得发"，不是"成绩好不好看"。
    """
    from ....paths import ROOT
    from .... import config_io, mailer, wecom
    from ....modules import notify as push
    from ....modules.notify import prefs as push_prefs

    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    # ⭐ 业务开关（用户 2026-09-22）：「可推可不推的门店可以自己设置」——
    #   在「周度重点产品 › 设置」里关掉就整条不推（邮件+企微一起）。
    off = push_prefs.why_off("attain", root)
    if off:
        say("[推送] 销售达成：跳过（%s）" % off)
        return {"wecom": {"state": "disabled", "why": off},
                "mail": {"state": "disabled", "why": off}}
    if no_push and no_mail:
        return {"wecom": {"state": "disabled", "why": "--no-push --no-mail"},
                "mail": {"state": "disabled", "why": "--no-push --no-mail"}}
    if not (payload.get("exists") and payload.get("rows")):
        why = "没算出来（没有 rows）—— 先算再推"
        return {"wecom": {"state": "skipped", "why": why},
                "mail": {"state": "skipped", "why": why}}

    cfg = {}
    try:
        p = Path(config_path) if config_path else None
        cfg = config_io.load_raw(p) or {}
    except Exception as e:                                     # noqa: BLE001
        cfg = {}
        say("  ⚠ 配置读不出来（%s）—— 推送按默认配置走" % e)
    head, lines = notify_lines(payload, store)
    ctx = {"门店": (cfg.get("erp_store_name") or payload.get("rows")[0].get("store") or "?"),
           "生成时间": payload.get("computed_at") or "",
           "配置文件": str(config_path or "")}
    out = {}

    if no_push:
        out["wecom"] = {"state": "disabled", "why": "--no-push"}
    else:
        try:
            wc = wecom.load_wecom_config(cfg, root)
            ok, why = wecom.should_send(wc, has_diff=True, ignore_when=True)
            if not ok:
                out["wecom"] = {"state": "disabled", "why": why}
                say("[推送] 销售达成企微：跳过（%s）" % why)
            else:
                r = push.send("wecom", {"template": "attain", "ctx": ctx,
                                        "lines": lines, "head": head},
                              cfg=cfg, root=root, feature="attain")
                out["wecom"] = {"state": "sent" if r["ok"] else "failed", "why": r["why"]}
                say("[推送] 销售达成企微：%s %s" % ("✅" if r["ok"] else "❌", r["why"]))
        except Exception as e:                                 # noqa: BLE001
            out["wecom"] = {"state": "failed", "why": "%s: %s" % (type(e).__name__, e)}
            say("[推送] 销售达成企微：❌ %s" % e)

    if no_mail:
        out["mail"] = {"state": "disabled", "why": "--no-mail"}
    else:
        try:
            mc = mailer.load_mail_config(cfg, root)
            ok, why = mailer.should_send(mc, has_diff=True)     # ⚠ 见 docstring
            if not ok:
                out["mail"] = {"state": "disabled", "why": why}
                say("[推送] 销售达成邮件：跳过（%s）" % why)
            else:
                subject, body = mailer.build_attain_mail(ctx, lines, head)
                r = push.send("mail", {"subject": subject, "body": body,
                                       "prefix": mailer.ATTAIN_SUBJECT_PREFIX},
                              cfg=cfg, root=root, feature="attain")
                out["mail"] = {"state": "sent" if r["ok"] else "failed", "why": r["why"]}
                say("[推送] 销售达成邮件：%s %s" % ("✅" if r["ok"] else "❌", r["why"]))
        except Exception as e:                                 # noqa: BLE001
            out["mail"] = {"state": "failed", "why": "%s: %s" % (type(e).__name__, e)}
            say("[推送] 销售达成邮件：❌ %s" % e)
    return out


# ------------------------------------------------------------------ 历史存档
#: 归档目录（一周一个文件，**一旦写进去就不再改** —— 用户要的"锁住存档"）
ARCHIVE_DIR = "out/attain-history"
_ARCHIVE_PERIOD = re.compile(r"\d{4}-W(?:0[1-9]|[1-4][0-9]|5[0-3])")


def archive_dir(root) -> Path:
    return Path(root) / ARCHIVE_DIR


def archive_path(root, period: str) -> Path:
    value = str(period or "")
    if not _ARCHIVE_PERIOD.fullmatch(value):
        raise ValueError("历史期间格式无效")
    return archive_dir(root) / ("%s.json" % value)


def archive_rolled(root, payload: dict, *, emit=None) -> str:
    """**周一切换到新的一周时，把上一周锁住存档**（用户 2026-09-20）：

    > 「加个历史记录功能，这一周过去之后，比如这一周 14-20 号，**21 号再获取达成**，
    >   就把**上一周的给锁住存档**」

    ⚠ 触发点是**"存的那份期间 ≠ 新算出来的期间"** —— 不靠"今天几号"去猜：
      办公室晚改表、或者补跑一次，都可能让期间反复；以**落盘里的期间**为准最稳。
    ⚠ **已经归档的期间绝不再写**（那就是"锁住"）—— 重复跑同一周不该改历史，
      否则"上周的成绩"会被人事后改掉，而那正是复盘时要引用的东西。
    """
    say = emit or (lambda _s: None)
    cur = load(root)
    old = str(cur.get("period") or "")
    new = str(payload.get("period") or "")
    if not old or not new or old == new:
        return ""
    if not cur.get("rows"):
        return ""
    dst = archive_path(root, old)
    if dst.exists():
        say("  （%s 已经归档过，不动它 —— 历史是锁住的）" % old)
        return ""
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = dict(cur)
    body["locked_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tmp = dst.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(dst)
    say("  （上一周 %s 已锁住存档 → %s）" % (old, dst.name))
    return old


def history_list(root) -> list:
    """归档过哪些周（**新的在前**）—— 给「历史记录」页用。"""
    d = archive_dir(root)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            one = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                      # noqa: BLE001
            continue
        rows = one.get("rows") or []
        totals = [r.get("total") for r in rows if r.get("total") is not None]
        out.append({"period": one.get("period") or p.stem,
                    "start": one.get("start") or "", "end": one.get("end") or "",
                    "locked_at": one.get("locked_at") or "",
                    "stores": len(rows), "file": p.name,
                    "avg": (sum(totals) / len(totals)) if totals else None})
    out.sort(key=lambda x: str(x.get("period") or ""), reverse=True)
    return out


def history_load(root, period: str) -> dict:
    """读某一周的存档（**只读** —— 它就是"锁住"的那份）。"""
    try:
        p = archive_path(root, period)
    except ValueError:
        return {"exists": False, "error": "历史期间格式无效"}
    if not p.is_file():
        return {"exists": False, "error": "没有这一周的存档：%s" % period}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001
        return {"exists": False, "error": "存档读不出来（%s）：%s" % (p.name, e)}
    d["exists"] = True
    d["file"] = p.name
    d["locked"] = True
    return d
