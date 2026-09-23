"""**门店 → 区长/平台 的数据上报**（M18）—— 打包 + 发送 + 补发。

设计在 `.dsh/docs/2026-09-21-M18M19-上报协议与收信落库-详细设计.md`
（口径 / 实测 / 风险都在那儿），这里只写"为什么这么写"。

## 三句话

1. **增量 = 指纹差集**，不是时间戳、不是 rowid：
   ⚠ 每天那趟抓的是**整月**（`dump.month_range`）而且 `INSERT OR REPLACE`
   ⇒ ① `orders.create_time` 其实是**业务时间**（和 `doc_create_time` 逐字相同），
   判不出"今天新写的"；② `rowid` 每重拉一遍就翻倍（实测 447 行 / max(rowid)=894）。
   指纹差集还顺带解决了**补录**（云商里 6 月的单今天才补进来）—— 那才是最该报的。
2. **渠道关着就不生成包**（`mail.enabled=false`）—— 生成了就是每天堆一个几百 KB 的死文件，
   而门店什么都看不到。关着就明说"没发"，等开了以后差集自然会把没发的一次发全。
3. **指纹在"包生成出来"那一刻前进**（不是"发成功之后"）⇒ **同一行绝不进两个包**；
   发不出去的包留在 `pending/`，下次开跑**先补发**。

⚠ 与 M19 的分工：**协议（表 / 键 / 包结构）只有这一份**；收信侧认 `_manifest` 里的
`mode` / `keys`，**不 import 本模块的 `TABLES`**（那样就成两份定义了）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import pathlib
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: 上报那 6 张表 —— **协议的唯一一份定义**：`(表名, 收信方怎么收, 主键列)`
#:
#: | 模式 | 收信方怎么落 |
#: |---|---|
#: | `incremental` | 按 `keys` upsert（这次没提到的行**不动**） |
#: | `snapshot` | 先把这个 (门店, 表) 清空再写（快照的语义就是"以这份为准"） |
#:
#: ⚠ `lg_stock` **没有主键**，`(snapshot_date, sn)` 也不唯一（实测 414 行只有 318 个不同 sn）
#:   ⇒ 它是 `snapshot` 模式、键留空，靠"整份覆盖"保证幂等。
TABLES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("orders", "incremental", ("document_no",)),
    ("order_lines", "incremental", ("document_no", "line_no")),
    ("payments", "incremental", ("document_no", "payment_no")),
    ("returns", "incremental", ("kind", "document_no")),
    ("lg_stock", "snapshot", ()),
    ("run_record", "incremental", ("id",)),
    # ⭐ 人员状态表（2026-09-21）：**区长/平台只读的那份就从这儿来** ——
    #   用户：「区长/平台**不能改**别家店的这份名单，**读取门店发送的状态表**吧」。
    #   ⚠ 它**不在本机库里**（是从云商组织架构 + 用户名单现算的，见 `_staff_rows`），
    #     所以这张表的行由**取数函数**给，`pick_rows` 那条路走不到它。
    #   ⚠ `snapshot`：人员名单的语义是"**以这份为准**"（谁在职、谁离职）——
    #     收信方先清空再写，不会把去年离职的人留在表里。
    ("staff", "snapshot", ()),
)

#: 包里有、但**不在本机库里**的表 —— 行由这个取数函数给：`{表名: 函数(root, cfg) -> [行]}`。
#: ⚠ 取数函数收 `(root, cfg, config_path)` —— **配置文件路径要传进去**：
#:   2026-09-21 实测踩到，`_staff_rows` 原来写死 `config/store-SCN231409.yaml`，
#:   而机器上真正用的可能是 `config/store-X.yaml`（临时 root / 换过店名的机器）
#:   ⇒ 读不到店名 ⇒ `staff_state()` 直接返回"还没认出这家店" ⇒ **人员表永远是空的**，
#:   而它按设计不报错，只有那句新加的 `staff_why` 把原因说了出来。
PROVIDERS = {
    "staff": lambda root, cfg, config_path=None: _staff_rows(root, cfg, config_path),
}


def _staff_rows(root, cfg, config_path=None) -> list:
    """本店的人员状态表（`account / real / phone / active`）。

    ⚠ 数据源是**云商组织架构 + 用户名单**（`features/store/staff.py`，带 12 小时缓存）。
    ⚠ 失败时**不抛**（拿不到就这张表空着，别让一个"顺手带上的表"把整封上报搞崩），
      但**要留下原因**（`LAST_STAFF_WHY`）—— 2026-09-21 实测踩到：
      它静默空着，于是"包里没这张表"和"这家店真没员工"**看起来一模一样**，
      真发那一下才发现是云商 token 过期、名单压根没取到。
    """
    global LAST_STAFF_WHY
    LAST_STAFF_WHY = ""
    try:
        from ..features.store import staff as staff_mod
        # ⚠ 用**这次真正在用的那份配置**（`build()` 传下来的）——
        #   没有才退回默认那个（别写死：机器上八成不是那个名字）。
        cp = config_path or (pathlib.Path(root) / DEFAULT_CONFIG)
        if not pathlib.Path(cp).is_absolute():
            cp = pathlib.Path(root) / cp
        got = staff_mod.staff_state(root, config_path=cp)
        if not got.get("ok", True):
            LAST_STAFF_WHY = str(got.get("error") or "读不到云商用户名单")
        return list(got.get("people") or [])
    except Exception as e:                                     # noqa: BLE001
        LAST_STAFF_WHY = "%s: %s" % (type(e).__name__, e)
        return []


#: 上一次取人员状态表**为什么是空的**（给人看的一句话；取到了就是空串）。
#: ⚠ 模块级的一个"上次结果"：`build` 是同步的，取完立刻读，不会串。
LAST_STAFF_WHY = ""

#: 协议版本 —— 收信方**只认它**：不认识的版本要说出来并跳过（不猜）。
PROTOCOL = 1

#: 主题前缀 —— ⚠ 只给人看 + 给**本地筛**用（QQ 的服务端 `SUBJECT` 是假筛，2026-09-21 实测）。
SUBJECT_PREFIX = "[CBG上报]"

#: 这台机器自己那点东西（`out/` 是 never-touch，自更新不会碰）。
REPORT_REL = "out/report"

#: 第一次上报（还没有指纹）发最近几天 —— ⚠ 不整库发：真实门店的库是**几十 MB**，
#: 第一封就超附件上限。这条只在"这台机器第一次上报"时用一次。
FIRST_DAYS = 7

#: `pending/` 最多留几个没发出去的包 —— 超了把**最老的**挪去 `sent/`
#: （**不删**：删了那天的数据就真没了，而邮件里那份还不一定在）。
PENDING_KEEP = 7

#: 行 hash 取前几位（这是"这行变没变"的比对，不是防篡改）。
HASH_LEN = 16


# --------------------------------------------------------------- 路径 / 状态

def paths(root) -> Dict[str, Path]:
    base = Path(root) / REPORT_REL
    return {"base": base, "state": base / "state.db",
            "pending": base / "pending", "sent": base / "sent",
            "inbox": base / "inbox"}


def ensure_dirs(root) -> Dict[str, Path]:
    p = paths(root)
    for k in ("base", "pending", "sent", "inbox"):
        p[k].mkdir(parents=True, exist_ok=True)
    return p


def open_state(root) -> sqlite3.Connection:
    """指纹 / 台账 / 计数器 —— 一个**独立的小库**（不动主库的 schema）。"""
    p = ensure_dirs(root)
    conn = sqlite3.connect(str(p["state"]))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fingerprints (
          table_name TEXT NOT NULL, row_key TEXT NOT NULL,
          row_hash TEXT NOT NULL, sent_at TEXT NOT NULL,
          PRIMARY KEY (table_name, row_key));
        CREATE TABLE IF NOT EXISTS sends (
          date TEXT PRIMARY KEY, file TEXT, rows INTEGER, bytes INTEGER,
          ok INTEGER, why TEXT, mail_to TEXT, cols_hash TEXT,
          created_at TEXT, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    conn.commit()
    return conn


def meta_get(conn, key, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def meta_set(conn, key, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, str(value)))


# ------------------------------------------------------------------- 行的指纹

def row_key(row, keys) -> str:
    """主键 → 字符串。⚠ 用 `\\x1f` 连（门店名/单号里都不会有它）。"""
    return "\x1f".join("" if row.get(k) is None else str(row.get(k)) for k in keys)


def row_hash(row) -> str:
    """这一行的指纹 —— ⚠ **值一律先转成文本**再序列化。

    SQLite 里 `3`（整数）和 `3.0`（浮点）是两种东西，`None` 和 `''` 也是。
    不统一的话同一行会天天"变"一次 ⇒ 天天当新增发出去，而收信方**看不出错**。
    """
    norm = {str(k): ("" if v is None else str(v)) for k, v in dict(row).items()}
    blob = json.dumps(norm, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_LEN]


def cols_hash(cols_by_table: Dict[str, List[str]]) -> str:
    """**列集快照**的指纹（"悄悄多发了列"要能一眼看见 —— 用户要 163 列全发，那不拦，但要说）。"""
    blob = json.dumps({t: sorted(c) for t, c in cols_by_table.items()},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_LEN]


def table_hash(rows: List[dict], keys) -> str:
    """整张表的 hash —— 收信方据此判"这封和台账里那封是不是同一份"。"""
    pairs = []
    for r in rows:
        pairs.append([row_key(r, keys), row_hash(r)] if keys else [row_hash(r)])
    blob = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_LEN]


# --------------------------------------------------------------------- 读源库

def source_dbs(root) -> List[Path]:
    """要读哪几个库 —— **当年 + 去年**。

    ⚠ 为什么要看去年：① 第一次上报的窗口可能跨年（1 月 2 日往前数是去年 12 月）；
      ② 跨年退货要能对上去年的原单。两个库加起来才是"这台机器手上的数据"。
    """
    from ..dump import year_db
    out_dir = Path(root) / "out"
    now = datetime.datetime.now()
    got = [year_db(out_dir, now.year), year_db(out_dir, now.year - 1)]
    return [Path(p) for p in got if Path(p).is_file()]


def table_cols(conn, table: str) -> List[str]:
    return [r[1] for r in conn.execute('PRAGMA table_info("%s")' % table)]


def table_ddl(conn, table: str) -> str:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone()
    return (row[0] if row else "") or ""


# --------------------------------------------------------------------- 挑行

def _business_dt(row) -> Optional[datetime.datetime]:
    """这行的**业务时间** —— 只给"第一次上报"那个窗口用（正常路径用不上）。"""
    ts = row.get("doc_create_ts")
    if ts:
        try:
            return datetime.datetime.fromtimestamp(int(ts))
        except (TypeError, ValueError, OSError):
            pass
    for k in ("doc_create_time", "payment_time"):
        raw = str(row.get(k) or "").strip()
        if len(raw) >= 19:
            try:
                return datetime.datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
    return None


def pick_rows(conn, table: str, mode: str, *, first: bool,
              now: datetime.datetime) -> Tuple[List[dict], List[dict]]:
    """这张表这次**要发哪些行** + **库里的全部行**（`(send, all)`）。

    ⚠ 为什么要一起返回"全部"：**第一次上报只发最近 7 天**，但指纹必须把
      **整库都登记上**。不然第二天那趟会发现"库里还有一堆我从没见过的行"
      ⇒ 把**一整年**当成新增发出去（实测：第 1 封 476 行 / 327 KB，
      第 2 封 1401 行 / 3.9 MB —— "第一次不整库发"的初衷当场落空）。
      ⇒ 语义是"**我已经把整库都登记为已知**，往后只有变化才发"。
    """
    try:
        rows = [dict(r) for r in conn.execute('SELECT * FROM "%s"' % table)]
    except sqlite3.Error:
        return [], []                                # 表还没建（新机器）—— 不算错
    if not rows:
        return [], []
    if table == "run_record":
        # 只要**今天**的运行记录（区长一眼看出"这家店今天跑成没跑成"）。
        today = now.strftime("%Y-%m-%d")
        return [r for r in rows if str(r.get("started_at") or "").startswith(today)], rows
    if mode == "snapshot":
        # 快照：只发**最新那一份**（不是增量）。
        days = [d for d in (str(r.get("snapshot_date") or "") for r in rows) if d]
        if not days:
            return [], rows
        newest = max(days)
        return [r for r in rows if str(r.get("snapshot_date") or "") == newest], rows
    if not first:
        return rows, rows                            # 正常路径：全给，由 `build` 按指纹筛
    since = now - datetime.timedelta(days=FIRST_DAYS)
    send = []
    for r in rows:
        dt = _business_dt(r)
        if dt is not None and dt >= since:
            send.append(r)
    return send, rows


# --------------------------------------------------------------------- 打包

def _identity(root, cfg) -> dict:
    """这台机器是谁 —— 走**能力层**（`modules.auth`），不自己读配置文件。"""
    from ..modules import auth
    try:
        got = (auth.accounts(cfg, root) or {}).get("store") or {}
    except Exception:                                          # noqa: BLE001
        got = {}
    code = str(got.get("code") or "").strip()
    name = str(got.get("name") or "").strip()
    return {"code": code, "name": name}


def build(root, *, date: str = "", cfg: dict = None, config_path=None, now=None,
          force: bool = False, conn=None) -> dict:
    """算差集 + 生成包。**指纹在这一步前进**（见模块头第 3 条）。

    ⚠ 不发送、不看渠道开关 —— 那两件事在 `run()` 里（这一层要能单独测）。
    返回 `{ok, why, file, rows, bytes, tables, manifest, first, cols_changed}`。
    """
    from .. import version
    from ..dump import open_db
    from ..modules import auth                     # noqa: F401  （上面 _identity 用）
    root = Path(root)
    now = now or datetime.datetime.now()
    date = date or now.strftime("%Y-%m-%d")
    if cfg is None:
        cfg = _load_cfg(root, config_path)
    who = _identity(root, cfg)
    if not who["code"]:
        return {"ok": False, "file": "", "rows": 0, "bytes": 0, "tables": {},
                "first": False, "cols_changed": [], "manifest": {},
                "why": "这台机器没有门店编码（`store_code`）—— 上报要按门店归档，先登录一次"}
    if not who["name"]:
        who["name"] = str((cfg or {}).get("erp_store_name") or "").strip()

    conn = conn or open_state(root)
    first = conn.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 0

    srcs = source_dbs(root)
    if not srcs:
        return {"ok": False, "file": "", "rows": 0, "bytes": 0, "tables": {},
                "first": first, "cols_changed": [], "manifest": {},
                "why": "没有找到订单库（out/cbg-<年>.db）—— 先跑一次抓数"}

    # ---- 每张表挑行 → 按指纹筛 → 攒进包里
    p = ensure_dirs(root)
    out_path = p["base"] / ("cbg-%s-%s.db" % (who["code"], date))
    if out_path.exists():
        out_path.unlink()                          # 同一天重跑 = 重新生成（幂等）
    pkg = sqlite3.connect(str(out_path))
    pkg.row_factory = sqlite3.Row
    src = [open_db(str(s)) for s in srcs]
    try:
        tables_meta, cols_by_table, total = {}, {}, 0
        ch, changed = "", []
        for table, mode, keys in TABLES:
            ddl = ""
            for c in src:
                ddl = table_ddl(c, table)
                if ddl:
                    break
            if not ddl and table in PROVIDERS:
                # ⚠ 取数型的表（`staff`）：DDL 由**行自己的键**推出来，
                #   数据从 PROVIDERS 拿 —— 「包里有一张本机库里没有的表」就靠这条。
                rows = list(PROVIDERS[table](root, cfg, config_path) or [])
                if not rows:
                    tables_meta[table] = {"rows": 0, "mode": mode, "keys": list(keys),
                                          "cols": [], "hash": table_hash([], keys)}
                    continue
                cols = sorted({k for r in rows for k in r})
                rows = [{k: r.get(k) for k in cols} for r in rows]
                cols_by_table[table] = cols
                _create_by_cols(pkg, table, cols, keys)
                _insert_rows(pkg, table, rows)
                tables_meta[table] = {"rows": len(rows), "mode": mode, "keys": list(keys),
                                      "cols": cols, "hash": table_hash(rows, keys)}
                total += len(rows)
                continue
            if not ddl:
                continue                           # 这个库里压根没这张表
            rows, allrows = [], []
            for c in src:
                send, every = pick_rows(c, table, mode, first=first, now=now)
                rows += send
                allrows += every
            cols_by_table[table] = table_cols(src[0], table) or []
            fresh, sent_at = [], now.strftime("%Y-%m-%d %H:%M:%S")
            for r in rows:
                key = row_key(r, keys) if keys else ""
                h = row_hash(r)
                if mode == "snapshot":
                    fresh.append(r)                # 快照整份发（幂等靠收信方清空重写）
                    continue
                old = conn.execute(
                    "SELECT row_hash FROM fingerprints WHERE table_name=? AND row_key=?",
                    (table, key)).fetchone()
                if old and old["row_hash"] == h and not force:
                    continue
                fresh.append(r)
            # ⚠ 快照表：日期和内容都没变就别再发一遍（省一封几百 KB 的重复）
            if mode == "snapshot":
                snap = ""
                if fresh:
                    snap = str(fresh[0].get("snapshot_date") or "")
                if (meta_get(conn, "snap_" + table) == snap
                        and meta_get(conn, "snap_hash_" + table) == table_hash(fresh, keys)
                        and not force):
                    fresh = []
                else:
                    meta_set(conn, "snap_" + table, snap)
                    meta_set(conn, "snap_hash_" + table, table_hash(fresh, keys))
            _create_like(pkg, ddl)
            if fresh:
                _insert_rows(pkg, table, fresh)
            tables_meta[table] = {"rows": len(fresh), "mode": mode,
                                  "keys": list(keys),
                                  "cols": cols_by_table[table],
                                  "hash": table_hash(fresh, keys)}
            if mode != "snapshot":
                # ⚠ 第一次要登记**整库**（见 `pick_rows` 那段），之后只登记发出去的。
                for r in (allrows if first else fresh):
                    conn.execute(
                        "INSERT OR REPLACE INTO fingerprints "
                        "(table_name, row_key, row_hash, sent_at) VALUES (?,?,?,?)",
                        (table, row_key(r, keys), row_hash(r), sent_at))
            total += len(fresh)

        ch = cols_hash(cols_by_table)
        prev = meta_get(conn, "cols_hash")
        if prev and prev != ch:
            changed = _cols_diff(conn, cols_by_table)
        manifest = {
            "protocol": PROTOCOL,
            "store_code": who["code"], "store_name": who["name"],
            "erp_name": str((cfg or {}).get("erp_store_name") or ""),
            "tdoc_name": str((cfg or {}).get("tdoc_name") or ""),
            "date": date, "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": version.VERSION, "reason": "manual" if force else "daily",
            "rows_total": total, "tables_json": json.dumps(tables_meta, ensure_ascii=False),
            "cols_hash": ch, "prev_cols_hash": prev,
            "cols_changed_json": json.dumps(changed, ensure_ascii=False),
        }
        _create_manifest(pkg)
        _insert_json(pkg, "_manifest", manifest)
        pkg.commit()
    finally:
        pkg.close()

    meta_set(conn, "cols_hash", ch)
    meta_set(conn, "store_code", who["code"])
    conn.commit()
    size = out_path.stat().st_size if out_path.exists() else 0
    return {"ok": True, "why": "", "file": str(out_path), "rows": total,
            "bytes": size, "tables": tables_meta, "manifest": manifest,
            "first": first, "cols_changed": changed,
            # ⚠ 人员状态表空着的话，**原因**带出去（`run()` 会打出来）
            "staff_why": LAST_STAFF_WHY}


def _cols_diff(conn, cols_by_table) -> List[str]:
    """列集和上次比，多了/少了哪些 —— 只**说出来**，不拦（用户要全发）。"""
    try:
        old = json.loads(meta_get(conn, "cols_json") or "{}")
    except ValueError:
        old = {}
    out = []
    for t, cols in cols_by_table.items():
        was = set(old.get(t) or [])
        now = set(cols)
        for c in sorted(now - was):
            out.append("+%s.%s" % (t, c))
        for c in sorted(was - now):
            out.append("-%s.%s" % (t, c))
    meta_set(conn, "cols_json", json.dumps({t: sorted(c) for t, c in cols_by_table.items()},
                                           ensure_ascii=False))
    return out


def _create_like(conn, ddl: str) -> None:
    """照抄源库的 DDL 建表 —— 包能用任何 SQLite 工具直接打开。"""
    conn.execute(ddl)


def _create_by_cols(conn, table: str, cols, keys) -> None:
    """按列名建表（取数型那张表用）—— 有主键就带上，收信方/人都好读。"""
    defs = []
    for c in cols:
        if c in ("active",):
            defs.append('"%s" INTEGER' % c)
        else:
            defs.append('"%s" TEXT' % c)
    pk = ", ".join('"%s"' % k for k in keys or () if k in cols)
    body = ", ".join(defs)
    if pk:
        body += ", PRIMARY KEY (%s)" % pk
    conn.execute('CREATE TABLE IF NOT EXISTS "%s" (%s)' % (table, body))


def _create_manifest(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS _manifest (
        protocol INTEGER, store_code TEXT, store_name TEXT, erp_name TEXT,
        tdoc_name TEXT, date TEXT, generated_at TEXT, version TEXT, reason TEXT,
        rows_total INTEGER, tables_json TEXT, cols_hash TEXT, prev_cols_hash TEXT,
        cols_changed_json TEXT, PRIMARY KEY (store_code, date))""")


def _insert_rows(conn, table: str, rows: List[dict]) -> None:
    for r in rows:
        cols = ", ".join('"%s"' % c for c in r)
        marks = ", ".join("?" for _ in r)
        conn.execute('INSERT OR REPLACE INTO "%s" (%s) VALUES (%s)' % (table, cols, marks),
                     [("" if v is None else v) for v in r.values()])


def _insert_json(conn, table: str, data: dict) -> None:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    conn.execute('INSERT OR REPLACE INTO %s (%s) VALUES (%s)' % (table, cols, marks),
                 list(data.values()))


#: 默认配置路径 —— ⚠ 和 `cli.DEFAULT_CONFIG` **同一个值**（那边是入口层，这里不能 import 它：
#: `test_module_layout` 钉着"`app/` 不许 import 入口层"）。改一处记得改两处，有测试比对。
DEFAULT_CONFIG = "config/store-SCN231409.yaml"


def _load_cfg(root, config_path=None) -> dict:
    """门店配置 —— 用 `config_io.load_raw`（**读不到给 `{}`，不抛**）。

    ⚠ 为什么不用 `cli.load_config`：① `app/` 不许 import 入口层（布局测试钉着）；
      ② 它找不到文件时抛 **`SystemExit`**，而那是 `BaseException`，
      会把整条 daily 带走（AGENTS.md 坑 11）。
    """
    from .. import config_io
    p = Path(config_path or DEFAULT_CONFIG)
    if not p.is_absolute():
        p = Path(root) / p
    try:
        return config_io.load_raw(p) or {}
    except Exception:                                          # noqa: BLE001
        return {}


# ------------------------------------------------------------------- 发送

def _mail_on(cfg, root) -> Tuple[bool, str]:
    """邮件那条渠道开着没 —— ⚠ **业务侧自己判**（`mailer.send` 不看 `enabled`，AGENTS.md 坑 15）。"""
    from ..modules import notify
    try:
        ok = notify.channel_on("mail", cfg=cfg, root=root)
    except Exception as e:                                     # noqa: BLE001
        return False, "读邮件配置失败（当没开）：%s: %s" % (type(e).__name__, e)
    return bool(ok), "" if ok else "「通用设置 › 邮件」里没启用"


def _recipients(root, store_name) -> Tuple[List[str], str]:
    """这封发给谁 —— **区长 + 中台**（用户 2026-09-21 定：「**两者**」）。

    ⚠ 区长那半复用**已有那一份**口径（`split.managers_of`：两种店名写法都认，
      没配邮箱的区长回落到中台）—— 不再写第二份"谁是区长"的判断。
    ⚠ **中台是"每封都发"的那一份，不是只在区长没配时才发**：用户要的是
      "区长和中台两个都收"。所以这里**无条件**把 `CENTRAL_ADDR` 加进去，并**去重**
      （区长没配邮箱时 `managers_of` 已经回落成中台了，别让同一个地址出现两次 ——
      收件人重复在某些邮箱里会被判成垃圾邮件）。
    ⚠ **保存人员设置触发的那封**走的是同一个 `send_file()` ⇒ 收件人只有这一处判据
      （"判据只有一处"是这个项目的规矩：两处各判各的迟早走散）。
    """
    from ..features.sales.attain import split as attain_split
    from .. import mailer
    try:
        got = attain_split.managers_of(store_name, root) or []
    except Exception:                                          # noqa: BLE001
        got = []
    to, seen = [], set()
    for m in got:
        addr = str(m.get("email") or "").strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            to.append(addr)
    central = str(getattr(mailer, "CENTRAL_ADDR", "") or "").strip()
    if central and central.lower() not in seen:
        seen.add(central.lower())
        to.append(central)
    why = "、".join("%s%s" % (m.get("name") or "?", "（回落中台）" if m.get("fallback") else "")
                   for m in got)
    if central:
        why = (why + " + 中台") if why else "中台"
    return to, why


def body_text(manifest: dict) -> str:
    """邮件正文 —— 给人看的（区长手机上扫一眼就知道"这家店今天报了什么"）。"""
    tables = json.loads(manifest.get("tables_json") or "{}")
    lines = ["门店：%s（%s）" % (manifest.get("store_name") or "?",
                                manifest.get("store_code") or "?"),
             "日期：%s" % manifest.get("date"),
             "生成：%s（程序 v%s）" % (manifest.get("generated_at"),
                                      manifest.get("version")),
             ""]
    label = {"orders": "订单", "order_lines": "订单明细", "payments": "付款",
             "returns": "退货", "lg_stock": "玲珑在库（快照）",
             "run_record": "本机运行记录"}
    for t, meta in tables.items():
        lines.append("  %-12s %5d 行" % (label.get(t, t), meta.get("rows") or 0))
    lines.append("")
    lines.append("合计 %d 行；明细在附件 %s 里（SQLite，可直接打开）。"
                 % (manifest.get("rows_total") or 0,
                    "cbg-%s-%s.db" % (manifest.get("store_code"), manifest.get("date"))))
    ch = json.loads(manifest.get("cols_changed_json") or "[]")
    if ch:
        lines.append("")
        lines.append("⚠ 这次的列集和上次不一样：%s" % "、".join(ch))
    return "\n".join(lines)


def send_file(root, path, manifest: dict, *, cfg=None, emit=None) -> dict:
    """把包发出去（**只看结果，不改指纹** —— 指纹在 `build` 那一步就前进了）。"""
    from ..modules import notify
    say = emit or (lambda _s: None)
    cfg = cfg if cfg is not None else _load_cfg(root)
    to, who = _recipients(root, manifest.get("store_name") or "")
    subject = "%s %s" % (SUBJECT_PREFIX,
                         "%s %s" % (manifest.get("store_code"), manifest.get("date")))
    res = notify.send("mail", {"subject": subject, "body": body_text(manifest),
                               "attachments": (str(path),), "to": to or None},
                      cfg=cfg, root=root)
    say("  发给：%s%s" % ("、".join(to) if to else "（配置里的收件人）",
                          "（%s）" % who if who else ""))
    return {"ok": bool(res.get("ok")), "why": str(res.get("why") or ""),
            "to": to, "subject": subject, "state": res.get("state")}


# ------------------------------------------------------------- 补发 / 主流程

def pending(root) -> List[Path]:
    """还没发出去的包（老的在前）。"""
    p = ensure_dirs(root)["pending"]
    return sorted([x for x in p.glob("cbg-*.db") if x.is_file()])


def _read_manifest(path) -> dict:
    try:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM _manifest LIMIT 1").fetchone()
        conn.close()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def _record(root, conn, date, *, file, rows, size, ok, why, to, cols) -> None:
    conn.execute("INSERT OR REPLACE INTO sends "
                 "(date, file, rows, bytes, ok, why, mail_to, cols_hash, created_at, sent_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (date, file, rows, size, 1 if ok else 0, why, ",".join(to or []),
                  cols, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                  datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if ok else ""))
    conn.commit()
    try:
        from ..storage import runlog
        runlog.record("report", bool(ok), root=root, note="数据上报 %s" % date,
                      why=why or "",
                      detail={"file": str(file), "rows": rows, "bytes": size,
                              "to": to or []})
    except Exception:                                          # noqa: BLE001
        pass                                                   # 记日志失败不该影响上报


def run(root, *, cfg=None, config_path=None, date: str = "", no_push: bool = False,
        force: bool = False, dry_run: bool = False, emit=None) -> dict:
    """**daily 里那一步**：先补发 → 再看渠道 → 生成包 → 发。

    ⚠ 顺序不许换：**先补发**，否则网络恢复的那天会先把今天的发出去、
      而前面欠的那几封继续压着（越压越多，最后要靠人去 `pending/` 里翻）。
    """
    from ..paths import ROOT
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    cfg = cfg if cfg is not None else _load_cfg(root, config_path)
    ensure_dirs(root)
    conn = open_state(root)
    out = {"pending_sent": 0, "pending_failed": 0, "built": False, "sent": False,
           "ok": False, "why": "", "file": "", "rows": 0, "bytes": 0,
           "to": [], "skipped": "", "cols_changed": []}

    if no_push:
        say("  上报：这趟不发（--no-push）")
        out["skipped"] = "no-push"
        return out

    on, why_off = _mail_on(cfg, root)
    if not on:
        # ⚠ 渠道关着 ⇒ **不生成包**（见模块头第 2 条）。指纹不动，欠的账一笔不丢。
        say("  上报：跳过 —— %s" % why_off)
        out["skipped"] = why_off
        out["why"] = why_off
        conn.execute("INSERT OR REPLACE INTO sends "
                     "(date, file, rows, bytes, ok, why, created_at) VALUES (?,?,?,?,?,?,?)",
                     (date or datetime.date.today().isoformat(), "", 0, 0, 0,
                      "没发：" + why_off,
                      datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return out

    # ① 补发欠着的
    olds = pending(root)
    for f in olds:
        man = _read_manifest(f)
        res = send_file(root, f, man, cfg=cfg, emit=say)
        if res["ok"]:
            shutil.move(str(f), str(ensure_dirs(root)["sent"] / f.name))
            out["pending_sent"] += 1
            say("  补发成功：%s" % f.name)
        else:
            out["pending_failed"] += 1
            say("  补发失败（留着下次再发）：%s —— %s" % (f.name, res["why"]))

    if dry_run:
        out["why"] = "dry-run"
        return out

    # ② 生成今天这一封
    info = build(root, date=date, cfg=cfg, config_path=config_path,
                 force=force, conn=conn)
    if not info.get("ok"):
        say("  上报：没生成包 —— %s" % info.get("why"))
        out["why"] = info.get("why") or ""
        return out
    out["built"] = True
    out["file"] = info["file"]
    out["rows"] = info["rows"]
    out["bytes"] = info["bytes"]
    out["cols_changed"] = info.get("cols_changed") or []
    if info.get("first"):
        say("  ⚠ 这是**第一次**上报：发的是最近 %d 天的数据（以后只发新增/变化的）"
            % FIRST_DAYS)
    if info.get("staff_why"):
        say("  ⚠ 人员状态表没取到（这张表会空着）：%s" % info["staff_why"])
    for line in info.get("cols_changed") or []:
        say("  ⚠ 列集和上次不一样：%s（用户要求全发 ⇒ 不拦，但你该知道）" % line)

    path = Path(info["file"])
    res = send_file(root, path, info["manifest"], cfg=cfg, emit=say)
    out["to"] = res["to"]
    if res["ok"]:
        shutil.move(str(path), str(ensure_dirs(root)["sent"] / path.name))
        out["sent"] = True
        out["ok"] = True
        say("  上报成功：%s（%d 行 / %.0f KB）"
            % (path.name, info["rows"], info["bytes"] / 1024.0))
    else:
        # ⚠ 发不出去就**留在 pending/**（下次先补发）。指纹**不回退**：
        #   这封总会发出去的，回退只会让同一行进两个包。
        os_path = ensure_dirs(root)["pending"] / path.name
        if path.exists():
            shutil.move(str(path), str(os_path))
        out["why"] = res["why"]
        say("  上报没发出去（已留在待发目录，下次自动补发）：%s" % res["why"])
    _record(root, conn, info["manifest"].get("date") or "",
            file=(out["file"] if res["ok"] else str(os_path)),
            rows=info["rows"], size=info["bytes"], ok=res["ok"],
            why=res["why"] or "已发送", to=res["to"],
            cols=info["manifest"].get("cols_hash") or "")
    _trim_pending(root, say)
    return out


def _trim_pending(root, say) -> None:
    """`pending/` 超过 `PENDING_KEEP` ⇒ 最老的挪去 `sent/`（**不删**）。"""
    olds = pending(root)
    if len(olds) <= PENDING_KEEP:
        return
    for f in olds[:len(olds) - PENDING_KEEP]:
        shutil.move(str(f), str(ensure_dirs(root)["sent"] / f.name))
        say("  ⚠ %s 一直没发出去，先挪去 sent/（没删，还能人工补发）" % f.name)


def history(root, limit: int = 20) -> List[dict]:
    """台账（界面/排障用）—— 最近的在前。"""
    try:
        conn = open_state(root)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM sends ORDER BY date DESC LIMIT ?", (int(limit),))]
        conn.close()
        return rows
    except sqlite3.Error:
        return []
