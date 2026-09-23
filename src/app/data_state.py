"""这份数据能不能算 —— **五种状态的可计算判据**（M14 / 阶段 3.2）。

⚠ **为什么要有它**：现在"今天没数据"和"今天抓失败了"在界面上是**同一个画面** ——
`web.pos()` 只有一句 hint（`web.py:809`），而 `out/pools-<年>.json` **只有成功才写**。
用户 2026-09-19 在 M2M5 §9.4 点过名：深蓝中心店 / 市南金茂湾店的 **0.0% 是真 0**，
不是没数据 —— 这两件事必须分得开，不然门店会把"ERP 挂了"读成"今天没卖"。

## 五态（互斥且穷尽）

| 状态 | 一句话 | 判据 |
|---|---|---|
| `ok` | 数据有、且在需要的期间内 | 没失败记录，`rows > 0`，`as_of >= need` |
| `zero` | **采集成功，但那个期间确实没有数据** | 没失败记录，`rows == 0`，`as_of >= need` |
| `failed` | **采过一次，失败了**（且之后没成功过） | 最后一次尝试 `ok=0` |
| `missing` | 从来没有采过 | 一条记录都没有（也没有任何数据证据） |
| `stale` | 有数据，但**采集时点早于需要的期间** | `as_of < need` |

⚠ 判定顺序是 **failed → missing → stale → zero → ok**：
"上次抓失败了"比"数据旧了"更具体；"压根没有"比"旧了"更基本。

## `need`（要算哪段期间）由**调用方**给

POS 要的是整月、双平台对比要的是当天、达成要的是本周 —— 集中配置必然猜错（D3）。
传 `None` = **不判过期**（只看有没有、是不是失败）。

## 这一步是只读的

本模块**不写任何东西**、不建表、不改数据。写"尝试记录"是 `pools`/`dump` 的事
（见 `record_attempt`）。
"""

from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..paths import ROOT

OK = "ok"
ZERO = "zero"
FAILED = "failed"
MISSING = "missing"
STALE = "stale"

#: 给人看的状态名（界面和自检都用它，别各写一份）
LABELS = {
    OK: "正常",
    ZERO: "确实为零",
    FAILED: "采集失败",
    MISSING: "没有数据",
    STALE: "数据过期",
}

CST = datetime.timezone(datetime.timedelta(hours=8))


@dataclass
class SourceState:
    """一个数据源的状态。`why` 是**真正的病因**（照 `dump.check_freshness` 的做法：
    只回一句"自检没过"谁也定位不了，见 `dump.py:543` 的注释）。"""

    key: str
    label: str
    state: str = MISSING
    why: str = ""
    as_of: str = ""          # 采集 / 快照时点（能显示就显示）
    need: str = ""
    rows: int = 0
    collected_at: str = ""   # 采集动作发生的时刻（跟"数据覆盖到哪天"是两件事）

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "state": self.state,
                "state_label": LABELS.get(self.state, self.state), "why": self.why,
                "as_of": self.as_of, "need": self.need, "rows": self.rows,
                "collected_at": self.collected_at}


def _ts(text) -> Optional[datetime.datetime]:
    """`YYYY-MM-DD[ HH:MM:SS]` → 带时区的 datetime。读不出来返回 None（**不抛**）。"""
    t = str(text or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(t[:len(fmt) + 4 if "%H" in fmt else 10], fmt)
            return d.replace(tzinfo=CST)
        except ValueError:
            continue
    return None


def _day_end(day: str) -> Optional[datetime.datetime]:
    """某一天的**结束时刻** —— 快照是"那天的一份"，拿它跟 need 比要用当天 23:59:59。"""
    d = _ts(day)
    return d.replace(hour=23, minute=59, second=59) if d else None


def _need_dt(need) -> Optional[datetime.datetime]:
    if need is None:
        return None
    if isinstance(need, datetime.datetime):
        return need if need.tzinfo else need.replace(tzinfo=CST)
    if isinstance(need, (int, float)):
        return datetime.datetime.fromtimestamp(need, CST)
    return _ts(need)


def _fmt(dt) -> str:
    """时间 → `YYYY-MM-DD HH:MM:SS`。

    ⚠ 要能收**字符串**：调用方（`cli._record_fetch`）有时手上就是个现成的时间串。
    第一版只认 datetime，传字符串会 AttributeError —— 而那个异常会被
    `_record_fetch` 的兜底 `except` 吞掉 ⇒ **这条采集记录静默消失**，
    表现是"failed 这一态永远是空的"（本轮测试逮到的）。
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt.strip()
    try:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (AttributeError, ValueError):
        return ""


# ------------------------------------------------------------------ 采集尝试记录
def ensure_attempt_table(conn) -> None:
    """建"采集尝试"表（**D2 选的乙方案**）。

    ⚠ 为什么不复用 `fetch_log`：那张表的语义是"**抓到了什么**"（orders/lines/payments…），
    而"试过没有、成没成"是另一件事。混在一起的话 `check_freshness` 的
    `WHERE orders > 0` 就得改成"排除失败行"，那种条件最容易写错，
    而它错了的表现是"把失败当成成功"—— 静默。
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS fetch_attempt (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,      -- dump / lg-stock / erp-stock / erp-sales
        started_at  TEXT, finished_at TEXT,
        ok          INTEGER NOT NULL,   -- 1 成功 / 0 失败
        why         TEXT,               -- 失败原因（给人看）
        rows        INTEGER)""")
    conn.commit()


def record_attempt(conn, kind: str, ok: bool, *, why: str = "", rows: int = 0,
                   started_at=None, finished_at=None) -> None:
    """记一次采集**尝试**（成功失败都记）。

    ⚠ **失败也要记** —— 这正是这一版要解决的：以前失败时一行都不写，
    于是"没跑"和"跑失败了"在库里长得一模一样。
    """
    try:
        ensure_attempt_table(conn)
        now = datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO fetch_attempt (kind, started_at, finished_at, ok, why, rows)"
            " VALUES (?,?,?,?,?,?)",
            (kind, _fmt(started_at) or now, _fmt(finished_at) or now,
             1 if ok else 0, str(why or "")[:500], int(rows)))
        conn.commit()
    except sqlite3.Error:
        # ⚠ 记不上不许影响正事（跟 `startup` 那套一个道理）
        pass


def last_attempt(conn, kind: str) -> Optional[dict]:
    """这个来源最后一次尝试；表不存在就返回 `None`（老库没有这张表）。"""
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM fetch_attempt WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row else None


def fetched_within(root=None, kinds=(), minutes: float = 30) -> bool:
    """这些来源里**最近 N 分钟内有没有成功采过**（手动刷新冷却用）。

    ⚠ 只认 `ok=1` 的那一次 —— 失败的尝试**不算**"刚拉过"（拉失败了当然该再拉）。
    ⚠ 只给**手动刷新**用：定时器那趟**不过这道闸**（用户 2026-09-22：
      「每天 timer 触发的定时执行就没有这个限制」）。
    ⚠ 任一路在窗口内成功即 True —— `erp-dump` 是"在库+销售"两个池，
      都算"云商刚拉过"。
    """
    if not kinds:
        return False
    try:
        from ..dump import connect, year_db  # 延迟：别在 import 链上拉 dump
    except Exception:                                          # noqa: BLE001
        return False
    try:
        root = Path(root) if root else ROOT
        found = _find_db(root)
        db = Path(found) if found else year_db(root / "out", datetime.date.today().year)
        conn = connect(str(db))
    except Exception:                                          # noqa: BLE001
        return False
    try:
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in kinds)
        row = conn.execute(
            "SELECT finished_at FROM fetch_attempt"
            " WHERE ok=1 AND kind IN (%s)" % marks,
            tuple(kinds)).fetchall()
        if not row:
            return False
        cutoff = (datetime.datetime.now(CST)
                  - datetime.timedelta(minutes=float(minutes)))
        for r in row:
            ts = _ts(r["finished_at"])
            if ts and ts >= cutoff:
                return True
        return False
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.close()
        except Exception:                                      # noqa: BLE001
            pass


# ------------------------------------------------------------------ 各数据源的证据
def _probe_orders(conn):
    """池 A（玲珑/华为订单）：证据来自 `fetch_log`（那次抓到了多少单）。"""
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT finished_at, orders FROM fetch_log WHERE orders > 0"
            " ORDER BY run_id DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None, 0, ""
    if row is None:
        return None, 0, ""
    return _ts(row["finished_at"]), int(row["orders"] or 0), "fetch_log"


def _probe_snapshot(conn, table):
    """池 B / 池 D：快照表的**最新一天**。

    ⚠ "最新快照"**不等于**"今天" —— `pools._latest_sn_set()` 取的是库里最大的
    `snapshot_date`。所以这里必须把日期显式拿出来跟 `need` 比，
    否则会拿三天前的快照当今天用（而且不报错）。
    """
    try:
        row = conn.execute("SELECT MAX(snapshot_date) FROM %s" % table).fetchone()
        day = row[0] if row else None
        if not day:
            return None, 0, table
        n = conn.execute("SELECT COUNT(*) FROM %s WHERE snapshot_date=?" % table,
                         (day,)).fetchone()[0]
    except sqlite3.Error:
        return None, 0, table
    return _day_end(day), int(n or 0), table


def _probe_erp_sales(conn):
    """池 C：累积表，没有"采集时刻"这一列 ⇒ 只能靠**数据自身覆盖到哪天**。

    ⚠ 这跟"采过没有"是两件事：数据里有到 9-19 的单，也可能是三天前一次抓进来的。
      所以成功/失败优先看 `fetch_attempt`（`collected_at`），这里只回答
      "数据覆盖到哪天、有多少行"。
    """
    try:
        row = conn.execute(
            "SELECT MAX(制单时间), COUNT(*) FROM erp_sales").fetchone()
    except sqlite3.Error:
        return None, 0, "erp_sales"
    if not row:
        return None, 0, "erp_sales"
    return _day_end(str(row[0] or "")[:10]), int(row[1] or 0), "erp_sales"


#: 数据源清单 —— 加一个来源就在这儿加一行（界面和自检都从这里派生）
SOURCES = (
    {"key": "dump", "label": "原始销售明细（玲珑/华为订单）", "probe": _probe_orders},
    {"key": "erp-sales", "label": "原始销售明细（云商·池C）", "probe": _probe_erp_sales},
    {"key": "lg-stock", "label": "库存快照（玲珑·池B）", "probe": _probe_snapshot,
     "table": "lg_stock"},
    {"key": "erp-stock", "label": "库存快照（云商·池D）", "probe": _probe_snapshot,
     "table": "erp_stock"},
)


def judge(rows: int, as_of, *, need=None, attempt=None, key="", label="") -> SourceState:
    """**判据本身**（纯函数，好单测）。

    ⚠ 顺序：failed → missing → stale → zero → ok。见模块头那段。
    """
    st = SourceState(key=key, label=label, rows=int(rows or 0),
                     as_of=_fmt(as_of), need=_fmt(_need_dt(need)) if need is not None else "")
    n = _need_dt(need)

    if attempt and not attempt.get("ok"):
        st.state = FAILED
        st.collected_at = str(attempt.get("finished_at") or "")
        st.why = ("上次采集失败（%s）：%s" % (st.collected_at, attempt.get("why") or "没写原因"))
        return st
    if attempt and attempt.get("ok"):
        st.collected_at = str(attempt.get("finished_at") or "")

    if as_of is None:
        st.state = MISSING
        st.why = ("从来没有采过（库里一条记录都没有）" if not attempt
                  else "采集记录说成功过，但库里没有任何数据")
        return st

    if n is not None and as_of < n:
        st.state = STALE
        st.why = "数据只到 %s，而这次要算到 %s —— 中间那段没采进来" % (st.as_of, st.need)
        return st

    if st.rows <= 0:
        st.state = ZERO
        # ⚠ 措辞是刻意的：**不许出现"没有数据"四个字** —— 那是 `missing` 的标签，
        #   两者混起来正是这一版要消灭的东西（M2M5 §9.4：真 0 ≠ 没数据）。
        st.why = ("%s 这个期间确实没有销售（采集本身是成功的，别当成没采到）"
                  % (st.as_of or "该期间"))
        return st

    st.state = OK
    st.why = "数据到 %s，%d 行" % (st.as_of, st.rows)
    return st


def data_state(root=None, db="", *, need=None) -> dict:
    """**这份数据能不能算**：每个数据源一份状态。

    ⚠ **绝不抛** —— 界面、自检、启动路径都会调它（库文件损坏 / 表不存在
    都要退化成 `missing` + 一句 why，而不是把页面弄成 500）。

    返回 `{"db": …, "need": …, "sources": [SourceState…], "ok": bool, "worst": …}`，
    其中 `ok` 是"全都 `ok`"，`worst` 是第一个非 `ok` 的状态（给横幅用）。
    """
    root = Path(root or ROOT)
    path = Path(db) if db else _find_db(root)
    out = {"db": str(path) if path else "", "need": _fmt(_need_dt(need)) if need is not None else "",
           "sources": [], "ok": False, "worst": ""}
    if not path or not path.is_file():
        for s in SOURCES:
            st = SourceState(key=s["key"], label=s["label"], state=MISSING)
            st.why = "没有找到订单库（out/cbg-<年>.db）"
            out["sources"].append(st.as_dict())
        out["worst"] = MISSING
        return out

    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error as e:
        for s in SOURCES:
            st = SourceState(key=s["key"], label=s["label"], state=MISSING)
            st.why = "库打不开：%s" % e
            out["sources"].append(st.as_dict())
        out["worst"] = MISSING
        return out

    try:
        for s in SOURCES:
            try:
                if s["key"] == "lg-stock" or s["key"] == "erp-stock":
                    as_of, rows, _ev = s["probe"](conn, s["table"])
                else:
                    as_of, rows, _ev = s["probe"](conn)
                st = judge(rows, as_of, need=need, attempt=last_attempt(conn, s["key"]),
                           key=s["key"], label=s["label"])
            except Exception as e:                            # noqa: BLE001
                st = SourceState(key=s["key"], label=s["label"], state=MISSING)
                st.why = "判据自己出错了：%s: %s" % (type(e).__name__, e)
            out["sources"].append(st.as_dict())
    finally:
        conn.close()

    bad = [s for s in out["sources"] if s["state"] != OK]
    out["ok"] = not bad
    out["worst"] = bad[0]["state"] if bad else OK
    return out


def _find_db(root):
    d = Path(root) / "out"
    if not d.is_dir():
        return None
    cands = sorted(d.glob("cbg-[0-9][0-9][0-9][0-9].db"))
    return cands[-1] if cands else None


def fingerprint(state: dict) -> str:
    """这一份"没到位"的**身份** —— 状态 + 是哪条 + 什么时候采的。

    ⚠ 干什么用：黄横幅要能**关掉**（用户 2026-09-21：「上面这个东西一直不让关」），
      但**不能一关就永远不出现** —— 那就成了把告警关掉。
      ⇒ 关掉的是"**这一条**"：指纹一样就藏起来；指纹变了
        （又失败了一次、或者从"过期"变成"失败"、或者换了数据源）就再露出来。
    """
    bad = [s for s in state.get("sources", []) if s.get("state") != OK]
    if not bad:
        return ""
    worst = state.get("worst", "")
    first = bad[0]
    return "%s|%s|%s|%s" % (worst, first.get("key", ""), first.get("state", ""),
                            first.get("collected_at") or first.get("as_of") or "")


def brief(state: dict) -> dict:
    """给界面用的**精简版**（概览页 30 秒刷一次，别把整份探测结果塞进去）。

    ⚠ 成本实测：整份判据 ~29ms（大头是 `erp_sales` 的全表扫 12.7ms，10 万行）。
      够便宜，所以**不做缓存** —— 缓存只会让"刚跑完 dump 界面还说旧"变成新的坑。
    """
    return {"ok": bool(state.get("ok")),
            "worst": state.get("worst", ""),
            "worst_label": LABELS.get(state.get("worst", ""), ""),
            "lines": summary_lines(state),
            # 横幅据此判断"这条已经被关掉了"（见 `fingerprint`）
            "fingerprint": fingerprint(state)}


def summary_lines(state: dict) -> list:
    """给人看的几行（自检 / 横幅都用它）—— **文案只写这一份**。"""
    if not state.get("sources"):
        return ["没有可判的数据源"]
    out = []
    for s in state["sources"]:
        mark = {OK: "✅", ZERO: "0️⃣", FAILED: "⛔", MISSING: "⬜", STALE: "🕒"}.get(s["state"], "?")
        out.append("%s %s：%s（%s）" % (mark, s["label"], s["state_label"], s["why"]))
    return out
