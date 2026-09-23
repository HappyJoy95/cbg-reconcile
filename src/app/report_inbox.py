"""**收门店上报**（M19）—— 收信 → 解析 → 落**自己那个独立的库**。

设计在 `.dsh/docs/2026-09-21-M18M19-上报协议与收信落库-详细设计.md`。
跑在**区长 / 平台**那台机器上（门店机器上 IMAP 没配 ⇒ 跳过，不算失败）。

## 四件事，每件都有理由

1. **认人不认表**：包里那份 `_manifest` 是**自描述**的（每张表的 `mode` / `keys` /
   `cols` 都在里面）⇒ 收信方**不 import 发送方的 `TABLES`**，也不照抄那 7 张表的 schema。
   用户要"163 列全发"、接口以后还会加字段 —— 照抄 schema 的话，每加一列都要动收信方。
2. **行表是"三列 + JSON"**：`(门店码, 表名, 行键)` 主键 + `row_json`。
   重放/覆盖的语义只取决于这三个键，跟列无关 ⇒ 幂等天然成立。
3. **`incremental` 按行键 upsert；`snapshot` 先清空再写** —— 后者是"以这份为准"的语义，
   ⚠ 不是"版本不同"：`lg_stock` 没有主键、`(snapshot_date, sn)` 也不唯一（实测），
   所以它只能整份覆盖。收信库里**只留最新一份**（400 行/店，恒定；历史在邮件里）。
4. **收不下 / 不认识的包一律记 `skips` 并打印** —— 静默丢一封的表现是
   "这家店今天好像没报"，而**没有任何地方能看出是收信这一步丢的**。
"""

from __future__ import annotations

import datetime
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

#: 收信库 —— **独立一个文件**：绝不和本机自己抓的数据混在一张表里
#: （四·八边界 5；这个项目最怕"两份数据混着还看不出来"）。
#:
#: ⚠ 2026-09-21 晚（用户：「**收取的文件应该放在 in 文件夹里吧，不应该放 out**」）——
#:   收进来的东西**一律在 `in/` 下**，`out/` 只放这台机器自己**产出**的
#:   （报告、本机抓的库、待发/已发的包）。分界线是"**这是别人发来的，还是我生出来的**"。
#:   ⚠ 老位置（`out/report.db` / `out/report/inbox/`）有数据的话**自动搬一次**，
#:     见 `_migrate_legacy()` —— 不搬的话区长那台机器会显示"从来没收到过"，
#:     而数据其实还在盘上（"看着像丢了"是最难查的一种）。
INBOX_DB = "in/report.db"

#: 收到的**原件**（门店发来的包 / 目标拆分包）—— 人工补看、以后重放都靠它。
INBOX_REL = "in/packages"

#: 老位置（2026-09-21 晚之前）—— **只用来搬迁**，别在别处引用。
LEGACY_DB = "out/report.db"
LEGACY_REL = "out/report/inbox"

#: 收信侧认识的主题前缀 —— ⚠ 跟发送方**同一个字面量**，但它必须能独立判断：
#: 认不出的主题就不碰（INBOX 里还有一堆别的邮件）。
SUBJECT_PREFIX = "[CBG上报]"

#: **目标拆分**那封的主题前缀（M21）—— 它走的是同一台机器的邮箱，
#: 但落的是另一张表（拆分不是"上报的数据"，是"店长分到人头上的活"）。
SPLIT_PREFIX = "[目标拆分]"

#: 拆分包的协议版本（跟 `features/sales/attain/split.py::SPLIT_PROTOCOL` 对齐）。
SPLIT_PROTOCOLS = (1,)

#: 收信时最多往回扫几封（本地筛，见 `fetch.mail` 那段"QQ 的 SUBJECT 是假筛"）。
SCAN = 200

#: 收信侧支持的协议版本（不认识的要说出来、跳过、记 `skips`）。
PROTOCOLS = (1,)


# --------------------------------------------------------------------- 库

#: 搬过的根目录 —— 别每次读都去 stat 一遍（`db_path()` 是每个请求都会走的）。
_MIGRATED = set()


def _migrate_legacy(root: Path) -> None:
    """把老位置的收信库 / 原件搬到 `in/` 下。**只在搬得动、且新位置还没有的时候搬。**

    ⚠ 为什么必须有这一段（不能"改了路径就完事"）：区长 / 平台那台机器上
      `out/report.db` 里**已经有数据**（各家店发来的台账 + 明细 + 拆分）。
      改了路径而不管它 ⇒ 那一页显示「从来没收到过」，**而数据其实还在盘上**。
      "看着像丢了"是这个项目最怕的一类故障（页面上没有任何提示）。
    ⚠ 两条底线：
      ① **新位置已经有东西就不搬**（绝不覆盖）；
      ② **搬不动就留着老的、继续按老位置用**（`db_path()` / `packages_dir()` 里有回落）——
         宁可位置乱一点，也不能把数据搬丢。
    ⚠ 搬完还要把库里 `inbox.file` 那一列的前缀改掉：它记的是**收下来那个文件的
      完整路径**，界面上「跳过的包」那张表直接显示它 —— 不改的话点开一看是个
      已经不存在的老路径（指着空气）。
    """
    root = Path(root)
    key = str(root)
    if key in _MIGRATED:
        return
    _MIGRATED.add(key)
    old_db, new_db = root / LEGACY_DB, root / INBOX_DB
    moved = False
    try:
        if old_db.is_file() and not new_db.exists():
            new_db.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_db), str(new_db))
            moved = True
    except OSError as e:                                       # noqa: BLE001
        print("  ⚠ 收信库搬不动（%s）—— 继续用老位置：%s" % (e, old_db))
    try:
        old_keep, new_keep = root / LEGACY_REL, root / INBOX_REL
        if old_keep.is_dir() and not new_keep.exists():
            new_keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_keep), str(new_keep))
        # 老目录空了就顺手收掉（`out/report/` 里没别的东西时）
        if old_keep.is_dir() and not any(old_keep.iterdir()):
            old_keep.rmdir()
    except OSError as e:                                       # noqa: BLE001
        print("  ⚠ 收到的原件搬不动（%s）—— 继续用老位置：%s" % (e, old_keep))
    if not moved:
        return
    print("  ⚠ 收信库换了位置：%s → %s（用户 2026-09-21：「收取的文件应该放在 in 文件夹里」）"
          % (LEGACY_DB, INBOX_DB))
    # `inbox.file` 里记的是老路径 ⇒ 改成新的，别让界面上显示一个不存在的路径
    try:
        conn = sqlite3.connect(str(new_db))
        try:
            conn.execute("UPDATE inbox SET file = replace(file, ?, ?)",
                         (str(root / LEGACY_REL), str(root / INBOX_REL)))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:                                 # noqa: BLE001
        print("  ⚠ 库里那几行「文件在哪」没改过来（不影响数据）：%s" % e)


def db_path(root) -> Path:
    """收信库在哪儿（`in/report.db`）—— 收信侧**唯一**一个库。

    ⚠ 老位置（`out/report.db`）**搬不动**时回落到它（见 `_migrate_legacy`）——
      宁可位置乱，也不能让页面变成"从来没收到过"。
    """
    root = Path(root)
    _migrate_legacy(root)
    p = root / INBOX_DB
    if not p.is_file() and (root / LEGACY_DB).is_file():
        return root / LEGACY_DB
    return p


def packages_dir(root) -> Path:
    """收到的**原件**放哪儿（`in/packages`）—— 同样的回落规则（见 `_migrate_legacy`）。"""
    root = Path(root)
    _migrate_legacy(root)
    p = root / INBOX_REL
    if not p.is_dir() and (root / LEGACY_REL).is_dir():
        return root / LEGACY_REL
    return p


def read_only_db(root) -> Optional[sqlite3.Connection]:
    """**读**收信库 —— ⚠ 库还不存在就返回 `None`，**绝不顺手建一个**。

    ⚠ 为什么专门拆一个（2026-09-21 实测踩到）：`cards()` 那些读接口原来走的
      是 `open_db()`（会 `CREATE TABLE IF NOT EXISTS`）⇒ **光是打开「数据交换」
      那一页就会在磁盘上建出一个库来**。后果有两个：
        ① 区长机器上"还没收到过任何上报"也会多一个空库（没必要）；
        ② 测试里只要有人用真根目录碰一下这个接口，项目的 `out/` 里就多一个
           `report.db` —— conftest 那条"测试不许往项目根写东西"的守卫当场报出来。
      ⇒ **读用这个、写用 `open_db`**，差别摆在函数名上。
    """
    p = db_path(root)
    if not p.is_file():
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def open_db(root) -> sqlite3.Connection:
    p = db_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS inbox (
          store_code TEXT NOT NULL, report_date TEXT NOT NULL,
          store_name TEXT, erp_name TEXT, tdoc_name TEXT,
          file TEXT, generated_at TEXT, version TEXT, reason TEXT,
          rows_total INTEGER, cols_hash TEXT, prev_cols_hash TEXT,
          cols_changed TEXT, tables_json TEXT, subject TEXT, mail_date TEXT,
          imported_at TEXT, PRIMARY KEY (store_code, report_date));
        CREATE TABLE IF NOT EXISTS rows_ (
          store_code TEXT NOT NULL, table_name TEXT NOT NULL, row_key TEXT NOT NULL,
          report_date TEXT NOT NULL, row_json TEXT NOT NULL,
          PRIMARY KEY (store_code, table_name, row_key));
        CREATE TABLE IF NOT EXISTS splits (
          store_code TEXT NOT NULL, period TEXT NOT NULL,
          store_name TEXT, erp_name TEXT, tdoc_name TEXT,
          start TEXT, end TEXT, columns_json TEXT, targets_json TEXT,
          saved_by TEXT, unverified INTEGER, generated_at TEXT, version TEXT,
          file TEXT, subject TEXT, mail_date TEXT, imported_at TEXT,
          PRIMARY KEY (store_code, period));
        CREATE TABLE IF NOT EXISTS skips (
          store_code TEXT, report_date TEXT, file TEXT, why TEXT, at TEXT);
        CREATE INDEX IF NOT EXISTS ix_rows_store ON rows_(store_code, table_name);
    """)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- 一个包 → 库

def read_manifest(path) -> dict:
    """读包里的 `_manifest`。读不出来返回 `{}`（调用方给原因，别在这儿猜）。"""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM _manifest LIMIT 1").fetchone()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}


def import_package(root, path, *, subject: str = "", mail_date: str = "",
                   conn: Optional[sqlite3.Connection] = None) -> dict:
    """把一个包落库 —— **幂等**：同一个 (门店, 日期) 再来 = 覆盖。

    返回 `{ok, why, store_code, date, rows, tables, duplicate}`。**绝不抛**：
    收信这一步崩了会连累整个 `daily` 的退出码，而"这封包坏了"是**数据的事**，
    不是"程序坏了"—— 记进 `skips` 让页面上看得见。
    """
    path = Path(path)
    own_conn = conn is None
    conn = conn or open_db(root)
    out = {"ok": False, "why": "", "store_code": "", "date": "", "rows": 0,
           "tables": {}, "duplicate": False, "file": str(path)}
    man = read_manifest(path)
    code = str(man.get("store_code") or "").strip()
    date = str(man.get("date") or "").strip()
    try:
        if not man:
            raise ValueError("包里没有 _manifest（不是我们发的包？）")
        proto = int(man.get("protocol") or 0)
        if proto not in PROTOCOLS:
            raise ValueError("协议版本 %s 不认识（这台机器该升级了）" % proto)
        if not code or not date:
            raise ValueError("_manifest 里缺 store_code / date")

        # 同一 (店, 日期) 再来一封、而且**内容 hash 一样** ⇒ 记一句"重复投递"
        old = conn.execute("SELECT cols_hash, rows_total, tables_json FROM inbox "
                           "WHERE store_code=? AND report_date=?",
                           (code, date)).fetchone()
        tables = json.loads(man.get("tables_json") or "{}")
        dup = bool(old and old["tables_json"] == json.dumps(tables, ensure_ascii=False))

        # ---- 行：先按表落，再写台账（台账失败也不会留下"半份"的行数据）
        for tname, meta in tables.items():
            mode = str(meta.get("mode") or "incremental")
            keys = [str(k) for k in (meta.get("keys") or [])]
            if mode == "snapshot":
                conn.execute("DELETE FROM rows_ WHERE store_code=? AND table_name=?",
                             (code, tname))
            for row in _rows_of(path, tname):
                key = _row_key(row, keys) if keys else _fallback_key(row)
                conn.execute("INSERT OR REPLACE INTO rows_ "
                             "(store_code, table_name, row_key, report_date, row_json) "
                             "VALUES (?,?,?,?,?)",
                             (code, tname, key, date,
                              json.dumps(row, ensure_ascii=False, separators=(",", ":"))))
        conn.execute("INSERT OR REPLACE INTO inbox "
                     "(store_code, report_date, store_name, erp_name, tdoc_name, file, "
                     " generated_at, version, reason, rows_total, cols_hash, prev_cols_hash, "
                     " cols_changed, tables_json, subject, mail_date, imported_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, date, man.get("store_name"), man.get("erp_name"),
                      man.get("tdoc_name"), path.name, man.get("generated_at"),
                      man.get("version"), man.get("reason"), man.get("rows_total"),
                      man.get("cols_hash"), man.get("prev_cols_hash"),
                      man.get("cols_changed_json"),
                      json.dumps(tables, ensure_ascii=False), subject, mail_date, _now()))
        conn.commit()
        out.update({"ok": True, "store_code": code, "date": date,
                    "rows": int(man.get("rows_total") or 0), "tables": tables,
                    "duplicate": dup,
                    "why": "重复投递（内容一样，覆盖为空操作）" if dup else ""})
    except Exception as e:                                     # noqa: BLE001
        conn.execute("INSERT INTO skips (store_code, report_date, file, why, at) "
                     "VALUES (?,?,?,?,?)",
                     (code, date, path.name, "%s: %s" % (type(e).__name__, e), _now()))
        conn.commit()
        out["why"] = "%s: %s" % (type(e).__name__, e)
        out["store_code"], out["date"] = code, date
    finally:
        if own_conn:
            conn.close()
    return out


def _rows_of(path, table: str) -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute('SELECT * FROM "%s"' % table)]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def _row_key(row: dict, keys: List[str]) -> str:
    return "\x1f".join("" if row.get(k) is None else str(row.get(k)) for k in keys)


def _fallback_key(row: dict) -> str:
    """`snapshot` 表（没有键）用**整行 hash** 当行键 —— 一份快照内不会撞。"""
    import hashlib
    blob = json.dumps({k: ("" if v is None else str(v)) for k, v in row.items()},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------- 收信

def configured(cfg=None, root=None) -> tuple:
    """这台机器的收信配好了没 → `(bool, 为什么没配好)`。

    ⚠ **门店机器必然没配**（它只有发件账号）⇒ 那一步要**安静跳过**，不是失败：
      报错的话门店天天看到一条红的，然后就不看了。
    """
    try:
        from .. import mailer
        from ..modules.fetch import mail as fetch_mail
        # ⚠ 用 `fetch.mail.config()`（= `mailer.imap_config`，**口径只在那一处**）——
        #   它管着"中台邮箱只有平台岗会读"这条规矩：门店/区长没配自己的账号就返回 `{}`，
        #   `imap_problems({})` 会把原因说清（而不是让我们猜一个默认邮箱去连）。
        info = fetch_mail.config(cfg, root)
        bad = mailer.imap_problems(info)
    except Exception as e:                                     # noqa: BLE001
        return False, "读不到收信配置：%s: %s" % (type(e).__name__, e)
    return (not bad), "、".join(bad)


def _unsealed(a, root, out, say):
    """把一个附件**解密**（不是密文就原样过 —— 见 `fetch.unseal_attachment`）。

    返回 `None` = 解不开（缺密钥 / 密钥不对 / 被改过）。⚠ 调用方**必须**处理它：
    那时 `unseal` 给的是**空字节**，当内容写下去会造出一个 0 行的空包，
    而且 `import_package` 那边"看着成功了"—— 正是这个项目最忌讳的那类失败。

    ⚠ 解密**归 fetch 模块**（用户 2026-09-21：「这个加密功能算在推送模块里，
      解密功能做在 fetch 模块里」）—— 这一步就是那条链上"收进来"的入口。
    """
    from ..modules import fetch
    raw, how = fetch.unseal_attachment(a.get("data") or b"", root=root)
    if how.get("state") != "failed":
        return raw
    name = a.get("filename") or "（没写文件名）"
    why = how.get("why") or "解不开"
    # ⚠ **要说出来**：这一行会进 M20「数据交换」页那块「收信的问题」，
    #   区长/平台一眼看得出"是包坏了还是这台机器缺密钥"。
    out["problems"].append("%s：%s" % (name, why))
    say("  ⚠ %s 收不下：%s" % (name, why))
    return None


def run(root, *, cfg=None, config_path=None, limit: int = SCAN, dry_run: bool = False,
        emit=None) -> dict:
    """**daily 里那一步**：把邮箱里还没落库的包收进来。

    ⚠ 收信**不改"已读"状态**（不动别人的邮箱）；同一封重收 = 覆盖，安全。
    """
    from ..modules.fetch import mail as fetch_mail   # ⚠ 包 `__init__` 没 import 子模块
    from ..paths import ROOT
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    out = {"ok": True, "skipped": "", "mails": 0, "ok_packages": 0, "skipped_packages": 0,
           "rows": 0, "stores": [], "problems": []}
    if cfg is None:
        from .report import _load_cfg
        cfg = _load_cfg(root, config_path)
    ok, why = configured(cfg, root)
    if not ok:
        say("  收信：跳过 —— %s" % why)
        out["skipped"] = why
        return out

    try:
        # ⚠ 只按 `[CBG上报]` 筛的话，拆分包（`[目标拆分]`）永远收不到 ——
        #   而它们走的是**同一个邮箱、同一趟收信**（用户 2026-09-21：M21）。
        items = [m for m in fetch_mail.recent(cfg=cfg, root=root, limit=limit)
                 if SUBJECT_PREFIX in str(m.get("subject") or "")
                 or SPLIT_PREFIX in str(m.get("subject") or "")]
    except Exception as e:                                     # noqa: BLE001
        # ⚠ 连不上 / 认证失败**要报出来**（"这段时间没有新邮件"和"收不了"是两件事）
        out["ok"] = False
        out["problems"].append("%s: %s" % (type(e).__name__, e))
        say("  收信失败：%s" % out["problems"][-1])
        return out

    out["mails"] = len(items)
    keep = packages_dir(root)
    keep.mkdir(parents=True, exist_ok=True)
    conn = open_db(root)
    try:
        for it in items:
            subj = it.get("subject") or ""
            atts = [a for a in (it.get("attachments") or [])
                    if str(a.get("filename") or "").lower().endswith(".db")]
            jsons = [a for a in (it.get("attachments") or [])
                     if str(a.get("filename") or "").lower().endswith(".json")]
            # ⭐ 目标拆分（M21）：**同一台机器、同一个邮箱**，落另一张表（只读的那份）。
            #   ⚠ 认的是主题前缀 —— 别的功能发来的 .json 附件（以后可能有）不碰。
            if jsons and SPLIT_PREFIX.strip("[]") in subj:
                for a in jsons:
                    p = keep / a["filename"]
                    data = _unsealed(a, root, out, say)
                    if data is None:               # 解不开：原件留着排查，不落库
                        p.write_bytes(a.get("data") or b"")
                        continue
                    p.write_bytes(data)
                    res = import_split(root, p, subject=subj,
                                       mail_date=it.get("date") or "", conn=conn)
                    if res["ok"]:
                        out.setdefault("splits", []).append(
                            "%s %s（%d 人）" % (res["store_code"], res["period"],
                                              res["members"]))
                        say("  %s %s 的目标拆分 → %d 人"
                            % (res["store_code"], res["period"], res["members"]))
                    else:
                        out["problems"].append("%s：%s" % (p.name, res["why"]))
                        say("  ⚠ %s 收不下：%s" % (p.name, res["why"]))
            if not atts:
                if jsons:
                    continue           # 这封是拆分包（已经处理过），不是上报包
                out["skipped_packages"] += 1
                out["problems"].append("「%s」没有 .db 附件" % subj)
                continue
            for a in atts:
                p = keep / a["filename"]
                data = _unsealed(a, root, out, say)
                if data is None:                   # 解不开：原件留着排查，不落库
                    p.write_bytes(a.get("data") or b"")
                    continue
                p.write_bytes(data)
                if dry_run:
                    man = read_manifest(p)
                    say("  [dry-run] 会收：%s（%s 行）"
                        % (p.name, man.get("rows_total")))
                    continue
                res = import_package(root, p, subject=it.get("subject") or "",
                                     mail_date=it.get("date") or "", conn=conn)
                if res["ok"]:
                    out["ok_packages"] += 1
                    out["rows"] += res["rows"]
                    if res["store_code"] not in out["stores"]:
                        out["stores"].append(res["store_code"])
                    line = "  %s %s → %d 行" % (res["store_code"], res["date"], res["rows"])
                    if res["duplicate"]:
                        line += "（重复投递）"
                    say(line)
                    ch = json.loads(_cols_changed(conn, res["store_code"], res["date"]) or "[]")
                    if ch:
                        # ⚠ 列集变了要**说出来**（用户要求全发 ⇒ 不拦，但要看得见）
                        say("    ⚠ 这家店的列集变了：%s" % "、".join(ch))
                        out["problems"].append("%s 列集变了：%s"
                                               % (res["store_code"], "、".join(ch)))
                else:
                    out["skipped_packages"] += 1
                    out["problems"].append("%s：%s" % (p.name, res["why"]))
                    say("  ⚠ %s 收不下：%s" % (p.name, res["why"]))
    finally:
        conn.close()
    if not items:
        say("  收信：没有新的上报邮件")
    else:
        say("  收信：%d 封里收到 %d 个包（%d 行）"
            % (out["mails"], out["ok_packages"], out["rows"]))
    return out


def _cols_changed(conn, code, date) -> str:
    row = conn.execute("SELECT cols_changed FROM inbox WHERE store_code=? AND report_date=?",
                       (code, date)).fetchone()
    return (row["cols_changed"] if row else "") or ""


# --------------------------------------------------------------- 查询（M20 用）

def stores(root) -> List[dict]:
    """每店一条：**最后一次上报是哪天、多少行、哪一版**（M20「每店一张卡」的原料）。"""
    conn = read_only_db(root)
    if conn is None:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT store_code, MAX(report_date) AS last_date, COUNT(*) AS days "
            "FROM inbox GROUP BY store_code ORDER BY store_code")]
        for r in rows:
            one = conn.execute(
                "SELECT store_name, rows_total, version, imported_at, cols_changed "
                "FROM inbox WHERE store_code=? AND report_date=?",
                (r["store_code"], r["last_date"])).fetchone()
            r.update(dict(one) if one else {})
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def latest(root, code: str) -> dict:
    """某家店最新那份包的台账 + 每张表多少行。"""
    conn = read_only_db(root)
    if conn is None:
        return {}
    try:
        row = conn.execute("SELECT * FROM inbox WHERE store_code=? "
                           "ORDER BY report_date DESC LIMIT 1", (code,)).fetchone()
        if not row:
            conn.close()
            return {}
        got = dict(row)
        got["tables"] = json.loads(got.get("tables_json") or "{}")
        conn.close()
        return got
    except sqlite3.Error:
        return {}


def skips(root, limit: int = 50) -> List[dict]:
    """收不下的包（界面/排障用）。"""
    conn = read_only_db(root)
    if conn is None:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM skips ORDER BY at DESC LIMIT ?", (int(limit),))]
        conn.close()
        return rows
    except sqlite3.Error:
        return []


# ------------------------------------------------------- 每店一张卡（M20）

def _run_of(conn, code: str) -> dict:
    """这家店**最后一次跑成没跑成** —— 从包里那份 `run_record` 聚合出来。

    ⚠ 这就是 `run_record` 进上报包的**全部理由**（用户 2026-09-20 那半句：
      「记录什么时间唤醒了什么，成功了没」）—— 区长一眼看出"哪家店今天没跑成"。
    """
    try:
        rows = conn.execute(
            "SELECT row_json FROM rows_ WHERE store_code=? AND table_name='run_record'",
            (code,)).fetchall()
    except sqlite3.Error:
        return {}
    best = {}
    for r in rows:
        try:
            d = json.loads(r["row_json"])
        except ValueError:
            continue
        if str(d.get("kind") or "") not in ("daily", "wake"):
            continue
        at = str(d.get("finished_at") or d.get("started_at") or "")
        if at >= str(best.get("at") or ""):
            best = {"at": at, "ok": bool(d.get("ok")), "why": str(d.get("why") or "")}
    return best


def cards(root, codes=None, *, today: str = "") -> list:
    """**每店一张卡**的数据（M20）—— 收信库的读出口。

    `codes` 给了就按它出（**哪怕这家店从来没上报过也要出**，`known=False`）——
    ⚠ 那正是区长最需要看到的："哪几家还没报"（不列出来的话，没上报的店等于不存在）。
    `codes=None` ⇒ 只列库里有的店。
    """
    today = today or datetime.date.today().isoformat()
    conn = read_only_db(root)
    if conn is None:
        return [{"store_code": str(c), "store_name": "", "known": False,
                 "why": "从来没收到过这家店的上报", "stale_days": None}
                for c in (codes or [])]
    out = []
    try:
        for code in (codes if codes is not None else [r["store_code"] for r in conn.execute(
                "SELECT DISTINCT store_code FROM inbox")]):
            code = str(code)
            row = conn.execute("SELECT * FROM inbox WHERE store_code=? "
                               "ORDER BY report_date DESC LIMIT 1", (code,)).fetchone()
            if not row:
                out.append({"store_code": code, "store_name": "", "known": False,
                            "why": "从来没收到过这家店的上报", "stale_days": None})
                continue
            got = dict(row)
            try:
                got["tables"] = json.loads(got.get("tables_json") or "{}")
            except ValueError:
                got["tables"] = {}
            try:
                got["cols_changed"] = json.loads(got.get("cols_changed") or "[]")
            except ValueError:
                got["cols_changed"] = []
            try:
                got["stale_days"] = (datetime.date.fromisoformat(today)
                                     - datetime.date.fromisoformat(str(got["report_date"]))
                                     ).days
            except ValueError:
                got["stale_days"] = None
            got["known"] = True
            got["run"] = _run_of(conn, code)
            got["why"] = ""
            out.append(got)
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def import_split(root, path, *, subject: str = "", mail_date: str = "",
                 conn: Optional[sqlite3.Connection] = None) -> dict:
    """**收一份门店发来的目标拆分**（M21）—— 幂等：同一 `(门店码, ISO 周)` 先删后写。

    ⚠ 只有**店长**发的算数（C1）。收信这一步没法验"发件人是不是店长"，
      能验的是**包里写的是哪家店** + 主题前缀 —— 所以：
        * `store_code` 空 ⇒ 收不下（`skips` 里说清"这封没带门店编码"）；
        * 身份那半带 `unverified`（没有云商登录名时）⇒ **照样收，但标出来**，
          页面上要能看出"这条是谁发的没核实"。
    """
    path = Path(path)
    own = conn is None
    conn = conn or open_db(root)
    out = {"ok": False, "why": "", "store_code": "", "period": "", "members": 0,
           "file": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("不是一份拆分（顶层不是对象）")
        proto = int(raw.get("protocol") or 0)
        if proto not in SPLIT_PROTOCOLS:
            raise ValueError("拆分协议版本 %s 不认识（这台机器该升级了）" % proto)
        code = str(raw.get("store_code") or "").strip()
        period = str(raw.get("period") or "").strip()
        if not code:
            raise ValueError("这封没带门店编码（`store_code`）—— 没法归档")
        if not period:
            raise ValueError("这封没带期间（`period`）")
        cols = [str(c) for c in (raw.get("columns") or [])]
        members = [m for m in (raw.get("members") or []) if isinstance(m, dict)]
        targets = {str(m.get("name") or ""): [int(x or 0) for x in (m.get("targets") or [])]
                   for m in members if str(m.get("name") or "")}
        conn.execute("INSERT OR REPLACE INTO splits "
                     "(store_code, period, store_name, erp_name, tdoc_name, start, end, "
                     " columns_json, targets_json, saved_by, unverified, generated_at, "
                     " version, file, subject, mail_date, imported_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (code, period, raw.get("store_name"), raw.get("erp_name"),
                      raw.get("tdoc_name"), raw.get("start"), raw.get("end"),
                      json.dumps(cols, ensure_ascii=False),
                      json.dumps(targets, ensure_ascii=False),
                      raw.get("saved_by"), 1 if raw.get("unverified") else 0,
                      raw.get("generated_at"), raw.get("version"), path.name,
                      subject, mail_date, _now()))
        conn.commit()
        out.update({"ok": True, "store_code": code, "period": period,
                    "columns": cols, "targets": targets, "members": len(targets)})
    except Exception as e:                                     # noqa: BLE001
        conn.execute("INSERT INTO skips (store_code, report_date, file, why, at) "
                     "VALUES (?,?,?,?,?)",
                     ("", "", path.name, "拆分：" + "%s: %s" % (type(e).__name__, e), _now()))
        conn.commit()
        out["why"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if own:
            conn.close()
    return out


def split_of(root, store: str, period: str) -> dict:
    """**区长/平台**那边读"这家店这一周发过来的拆分"（只读）—— 没有就给 `{}`。

    ⚠ 按**门店码或店名**找：调用方手上是"达成表里的门店名"（可能是腾讯文档那种
      短名），而包里两种名字都带着（`store_name` / `erp_name` / `tdoc_name`）。
    """
    conn = read_only_db(root)
    if conn is None:
        return {}
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM splits WHERE period=? AND (store_code=? OR store_name=? "
            "OR erp_name=? OR tdoc_name=?)", (str(period), str(store), str(store),
                                              str(store), str(store)))]
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not rows:
        return {}
    got = rows[0]
    try:
        got["columns"] = json.loads(got.get("columns_json") or "[]")
        got["targets"] = json.loads(got.get("targets_json") or "{}")
    except ValueError:
        got["columns"], got["targets"] = [], {}
    return got


def tables_of(root, table_name: str) -> list:
    """收信库里**某一张表**的行，按门店分组 —— `[(门店码, {rows, report_date, …}), …]`。

    ⚠ 这是"门店发来的状态表"那类东西的读出口（M18 的包里有哪张表就能读哪张）：
      行是 JSON（收信时按 `_manifest` 里的 `cols` 原样存下来的），**读的时候不认 schema**
      —— 这样门店那边加一列，这边不用改代码（用户要的"163 列全发"就是这个意思）。
    """
    conn = read_only_db(root)
    if conn is None:
        return []
    try:
        rows = list(conn.execute(
            "SELECT store_code, report_date, row_json FROM rows_ WHERE table_name=? "
            "ORDER BY store_code", (str(table_name),)))
        meta = {r["store_code"]: dict(r) for r in conn.execute(
            "SELECT store_code, store_name, report_date, imported_at, subject "
            "FROM inbox")}
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out: Dict[str, dict] = {}
    for r in rows:
        code = str(r["store_code"])
        one = out.setdefault(code, {"rows": [], "report_date": r["report_date"],
                                    "store_name": (meta.get(code) or {}).get("store_name") or "",
                                    "imported_at": (meta.get(code) or {}).get("imported_at") or "",
                                    "subject": (meta.get(code) or {}).get("subject") or ""})
        try:
            one["rows"].append(json.loads(r["row_json"]))
        except ValueError:
            continue
    # ⚠ 快照表在收信时是**整份覆盖**的，但 `report_date` 取每行自己的（同一批一样）
    return sorted(out.items(), key=lambda kv: kv[0])


def store_days(root, code: str, limit: int = 7) -> list:
    """这家店最近几天收到过什么（卡片点开看它）—— ⚠ **按天**，不是原始行。"""
    conn = read_only_db(root)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT report_date, rows_total, imported_at, version, reason, "
            "cols_changed, file FROM inbox WHERE store_code=? "
            "ORDER BY report_date DESC LIMIT ?", (str(code), int(limit)))]
    except sqlite3.Error:
        return []
    finally:
        conn.close()
