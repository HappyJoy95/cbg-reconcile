"""M18 + M19 —— **门店 → 区长/平台 的邮件上报与落库**（文件名避开 `test_report.py`：
那一份测的是 `src/report.py` 的差异清单落盘，两码事）。

设计：`.dsh/docs/2026-09-21-M18M19-上报协议与收信落库-详细设计.md`（§八 就是这份测试的清单）。

这一份盯四件事：

1. **增量口径**：指纹差集 —— 加一行/改一行才进包，没动就不进；
   而"第一次只发最近 7 天"**必须同时把整库登记进指纹**（否则第二天会把一整年当新增发出去，
   实测第 1 封 476 行 / 327 KB、第 2 封 1401 行 / 3.9 MB）。
2. **渠道关着就不生成包**（不然每天堆一个几百 KB 的死文件，而门店什么都看不到）。
3. **发不出去留着补发**，而且指纹**不回退**（同一行绝不进两个包）。
4. **收信侧认 `_manifest`**（不认表名、不照抄 schema）：幂等、坏包进 `skips`、绝不抛。
"""

from __future__ import annotations

import datetime
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.app import report, report_inbox                        # noqa: E402


# --------------------------------------------------------------- 造一个源库

DDL = {
    "orders": ('CREATE TABLE orders (document_no TEXT PRIMARY KEY, doc_create_ts INTEGER, '
               'doc_create_time TEXT, remark TEXT, amount REAL)'),
    "order_lines": ('CREATE TABLE order_lines (document_no TEXT, line_no INTEGER, sn TEXT, '
                    'item_name TEXT, PRIMARY KEY (document_no, line_no))'),
    "payments": ('CREATE TABLE payments (document_no TEXT, payment_no TEXT, payment_amount REAL, '
                 'payment_time TEXT, PRIMARY KEY (document_no, payment_no))'),
    "returns": ('CREATE TABLE returns (kind TEXT, document_no TEXT, order_no TEXT, '
                'doc_create_ts INTEGER, PRIMARY KEY (kind, document_no))'),
    "lg_stock": ('CREATE TABLE lg_stock (snapshot_date TEXT, sn TEXT, id INTEGER, '
                 'item_name TEXT, quantity INTEGER)'),
    "run_record": ('CREATE TABLE run_record (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, '
                   'note TEXT, started_at TEXT, finished_at TEXT, ok INTEGER, why TEXT)'),
}


def make_root(tmp: Path, *, store_code="SCN231409", name="青岛城阳万达店") -> Path:
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "out").mkdir(parents=True, exist_ok=True)
    (tmp / "config" / "store-X.yaml").write_text(
        'erp_store_name: "%s"\nstore_code: "%s"\n' % (name, store_code), encoding="utf-8")
    (tmp / "config" / "stores.yaml").write_text(
        "stores:\n  - erp_name: %s\n    tdoc_name: 城阳万达\n    kind: 体验店\n    marker: W\n"
        % name, encoding="utf-8")
    return tmp


def db_path(root: Path, year=2026) -> Path:
    return root / "out" / ("cbg-%d.db" % year)


def connect(root: Path, year=2026) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path(root, year)))
    conn.row_factory = sqlite3.Row
    for ddl in DDL.values():
        conn.execute(ddl)
    conn.commit()
    return conn


def add_order(conn, no="D1", *, ts=None, remark="", amount=100.0, lines=1, pay=1):
    ts = ts if ts is not None else int(datetime.datetime.now().timestamp())
    conn.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?)",
                 (no, ts, datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                  remark, amount))
    for i in range(lines):
        conn.execute("INSERT OR REPLACE INTO order_lines VALUES (?,?,?,?)",
                     (no, i + 1, "SN%s-%d" % (no, i), "商品"))
    for i in range(pay):
        conn.execute("INSERT OR REPLACE INTO payments VALUES (?,?,?,?)",
                     (no, "P%d" % (i + 1), amount, "2026-09-21 10:00:00"))
    conn.commit()


def add_snapshot(conn, date="2026-09-21", rows=3):
    for i in range(rows):
        conn.execute("INSERT INTO lg_stock VALUES (?,?,?,?,?)",
                     (date, "SN%d" % i, i + 1, "机器", 1))
    conn.commit()


def add_run(conn, ok=1, when=None):
    when = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("INSERT INTO run_record (kind, note, started_at, finished_at, ok, why) "
                 "VALUES ('daily','日常',?,?,?,'')", (when, when, ok))
    conn.commit()


CFG = {"erp_store_name": "青岛城阳万达店", "store_code": "SCN231409"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))
        self.conn = connect(self.root)
        add_snapshot(self.conn)
        add_run(self.conn)

    def build(self, **kw):
        return report.build(self.root, cfg=dict(CFG), **kw)

    def pack_rows(self, info, table):
        conn = sqlite3.connect(info["file"])
        conn.row_factory = sqlite3.Row
        got = [dict(r) for r in conn.execute('SELECT * FROM "%s"' % table)]
        conn.close()
        return got


# ------------------------------------------------------------------ 增量口径

class Test增量口径(_Base):
    def test_没动就不进包(self):
        add_order(self.conn, "D1")
        self.build()                                  # 第一次：登记整库 + 发
        info = self.build()
        self.assertEqual(info["rows"], 0, "什么都没改却又发了东西")

    def test_改一行只发那一行(self):
        add_order(self.conn, "D1")
        add_order(self.conn, "D2")
        self.build()
        self.conn.execute("UPDATE orders SET remark='改了' WHERE document_no='D2'")
        self.conn.commit()
        info = self.build()
        self.assertEqual([r["document_no"] for r in self.pack_rows(info, "orders")], ["D2"])
        self.assertEqual(info["tables"]["orders"]["rows"], 1)

    def test_加一行发新的那一行(self):
        add_order(self.conn, "D1")
        self.build()
        add_order(self.conn, "D2")
        info = self.build()
        self.assertEqual([r["document_no"] for r in self.pack_rows(info, "orders")], ["D2"])

    def test_第一次只发最近7天但整库登记指纹(self):
        """⚠⚠ 这条是实测踩出来的：只登记"发出去的那几行"⇒ 第二天把一整年当新增发。"""
        old = int((datetime.datetime.now() - datetime.timedelta(days=30)).timestamp())
        add_order(self.conn, "老单", ts=old)
        add_order(self.conn, "今天的单")
        info = self.build()
        self.assertTrue(info["first"])
        self.assertEqual([r["document_no"] for r in self.pack_rows(info, "orders")],
                         ["今天的单"])
        info2 = self.build()
        self.assertEqual(info2["rows"], 0, "老单第二次又被当新增发了一遍")
        state = sqlite3.connect(str(report.paths(self.root)["state"]))
        keys = {r[0] for r in state.execute(
            "SELECT row_key FROM fingerprints WHERE table_name='orders'")}
        state.close()
        self.assertIn("老单", keys)

    def test_第一次的窗口之外的老单不发(self):
        old = int((datetime.datetime.now() - datetime.timedelta(days=30)).timestamp())
        add_order(self.conn, "老单", ts=old)
        info = self.build()
        self.assertNotIn("老单", [r["document_no"] for r in self.pack_rows(info, "orders")])

    def test_子表跟着父单走(self):
        add_order(self.conn, "D1", lines=2, pay=2)
        self.build()
        self.conn.execute("UPDATE order_lines SET item_name='换个名' WHERE line_no=2")
        self.conn.commit()
        info = self.build()
        lines = self.pack_rows(info, "order_lines")
        self.assertEqual([(r["document_no"], r["line_no"]) for r in lines], [("D1", 2)])

    def test_今天之外的运行记录不发(self):
        add_run(self.conn, when="2026-01-01 21:00:00")
        info = self.build()
        got = self.pack_rows(info, "run_record")
        today = datetime.date.today().isoformat()
        self.assertTrue(all(str(r["started_at"]).startswith(today) for r in got))

    def test_值类型不影响指纹(self):
        """⚠ `3` 和 `3.0` 在 SQLite 里是两种类型 —— 不统一的话同一行天天"变"。"""
        add_order(self.conn, "D1", amount=100)
        self.build()
        conn = sqlite3.connect(str(db_path(self.root)))
        conn.execute("UPDATE orders SET amount=100 WHERE document_no='D1'")
        conn.commit()
        conn.close()
        info = self.build()
        self.assertEqual(info["tables"]["orders"]["rows"], 0)


class Test快照表(_Base):
    def test_新快照整份发(self):
        info = self.build()
        self.assertEqual(info["tables"]["lg_stock"]["rows"], 3)
        self.conn.execute("DELETE FROM lg_stock")
        add_snapshot(self.conn, date="2026-09-22", rows=2)
        info2 = self.build()
        self.assertEqual(info2["tables"]["lg_stock"]["rows"], 2)
        self.assertEqual(info2["tables"]["lg_stock"]["mode"], "snapshot")

    def test_同一天同内容不再发(self):
        self.build()
        info = self.build()
        self.assertEqual(info["tables"]["lg_stock"]["rows"], 0, "同一份快照发了两遍")

    def test_force_能强制重发(self):
        self.build()
        info = self.build(force=True)
        self.assertEqual(info["tables"]["lg_stock"]["rows"], 3)


# ------------------------------------------------------------------- manifest

class Test协议自描述(_Base):
    def test_manifest_该有的都有(self):
        add_order(self.conn, "D1")
        info = self.build()
        man = info["manifest"]
        for k in ("protocol", "store_code", "store_name", "date", "generated_at",
                  "version", "rows_total", "tables_json", "cols_hash"):
            with self.subTest(k=k):
                self.assertIn(k, man)
        self.assertEqual(man["protocol"], report.PROTOCOL)
        self.assertEqual(man["store_code"], "SCN231409")
        tables = json.loads(man["tables_json"])
        for t, meta in tables.items():
            with self.subTest(t=t):
                self.assertIn(meta["mode"], ("incremental", "snapshot"))
                self.assertIn("keys", meta)
                self.assertIn("cols", meta)
                self.assertIn("hash", meta)
        self.assertEqual(tables["orders"]["keys"], ["document_no"])
        self.assertEqual(tables["order_lines"]["keys"], ["document_no", "line_no"])
        self.assertEqual(tables["lg_stock"]["keys"], [])

    def test_列集变化要能看出来(self):
        self.build()
        self.conn.execute("ALTER TABLE orders ADD COLUMN 新字段 TEXT")
        self.conn.commit()
        info = self.build()
        self.assertIn("+orders.新字段", info["cols_changed"])

    def test_包里能直接读_manifest(self):
        info = self.build()
        man = report_inbox.read_manifest(info["file"])
        self.assertEqual(man["store_code"], "SCN231409")

    def test_没有门店码就不发(self):
        info = report.build(self.root, cfg={"erp_store_name": "青岛城阳万达店"})
        self.assertFalse(info["ok"])
        self.assertIn("门店编码", info["why"])

    def test_没有库就说清(self):
        db_path(self.root).unlink()
        info = report.build(self.root, cfg=dict(CFG))
        self.assertFalse(info["ok"])
        self.assertIn("订单库", info["why"])


# --------------------------------------------------------------- 发送 / 补发

class Test发送与补发(_Base):
    def setUp(self):
        super().setUp()
        add_order(self.conn, "D1")

    def _on(self, ok=True, why=""):
        return mock.patch.object(report, "_mail_on", lambda cfg, root: (ok, why))

    def test_渠道关着就不生成包(self):
        with self._on(False, "「通用设置 › 邮件」里没启用"):
            res = report.run(self.root, cfg=dict(CFG), emit=None)
        self.assertEqual(res["skipped"], "「通用设置 › 邮件」里没启用")
        self.assertFalse(res["built"], "渠道关着还生成了包")
        self.assertEqual(list(report.paths(self.root)["pending"].glob("*")), [])

    def test_发成功就进_sent_且台账记_ok(self):
        with self._on(), mock.patch.object(
                report, "send_file",
                lambda *a, **k: {"ok": True, "why": "", "to": ["q@x.com"], "subject": "s"}):
            res = report.run(self.root, cfg=dict(CFG), emit=None)
        self.assertTrue(res["sent"])
        self.assertEqual(len(list(report.paths(self.root)["sent"].glob("*.db"))), 1)
        rows = report.history(self.root)
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["rows"], res["rows"])

    def test_发失败就留pending_下次先补发(self):
        sent = []

        def fake(root, path, man, *, cfg=None, emit=None):
            sent.append(Path(path).name)
            ok = len(sent) > 1                      # 第一次失败，第二次成
            return {"ok": ok, "why": "" if ok else "网络不通",
                    "to": ["q@x.com"], "subject": "s"}

        with self._on(), mock.patch.object(report, "send_file", fake):
            res = report.run(self.root, cfg=dict(CFG), emit=None)
        self.assertFalse(res["sent"])
        pend = list(report.paths(self.root)["pending"].glob("*.db"))
        self.assertEqual(len(pend), 1, "发失败没留下待发包")
        old = pend[0].name
        with self._on(), mock.patch.object(report, "send_file", fake):
            res2 = report.run(self.root, cfg=dict(CFG), emit=None)
        self.assertEqual(sent[1], old, "没有先补发欠着的那封")
        self.assertEqual(res2["pending_sent"], 1)
        self.assertEqual(list(report.paths(self.root)["pending"].glob("*.db")), [])

    def test_指纹不回退_同一行不进两个包(self):
        with self._on(False, "关着"):
            report.run(self.root, cfg=dict(CFG), emit=None)
        with self._on(), mock.patch.object(
                report, "send_file",
                lambda *a, **k: {"ok": False, "why": "失败", "to": [], "subject": "s"}):
            r1 = report.run(self.root, cfg=dict(CFG), emit=None)
        with self._on(), mock.patch.object(
                report, "send_file",
                lambda *a, **k: {"ok": True, "why": "", "to": [], "subject": "s"}):
            r2 = report.run(self.root, cfg=dict(CFG), emit=None)
        self.assertGreater(r1["rows"], 0)
        self.assertEqual(r2["rows"], 0, "补发之外又重算了一遍增量（同一行进两个包）")

    def test_pending_超上限挪走但不删(self):
        p = report.ensure_dirs(self.root)["pending"]
        for i in range(report.PENDING_KEEP + 3):
            (p / ("cbg-SCN231409-2026-09-%02d.db" % (i + 1))).write_bytes(b"x")
        report._trim_pending(self.root, lambda _s: None)
        self.assertEqual(len(list(p.glob("*.db"))), report.PENDING_KEEP)
        self.assertEqual(len(list(report.paths(self.root)["sent"].glob("*.db"))), 3)

    def test_no_push_什么都不生成(self):
        res = report.run(self.root, cfg=dict(CFG), no_push=True, emit=None)
        self.assertEqual(res["skipped"], "no-push")
        self.assertFalse(res["built"])

    def test_收件人走那一份现成口径(self):
        """⚠ 2026-09-21 用户定：**区长和中台都发**（「两者」）——
        原来只发区长、没配邮箱才回落中台，现在中台是**每封都在**的那一份。"""
        from src import mailer
        (self.root / "config" / "managers.yaml").write_text(
            "managers:\n  - name: 杨英梅\n    accounts: ['SL1']\n    region: 西北区\n"
            "    email: 'jiuzhang@example.com'\n    stores:\n      - 青岛城阳万达店\n",
            encoding="utf-8")
        to, who = report._recipients(self.root, "青岛城阳万达店")
        self.assertEqual(to, ["jiuzhang@example.com", mailer.CENTRAL_ADDR])
        self.assertIn("杨英梅", who)
        self.assertIn("中台", who)

    def test_区长没配邮箱时中台不重复(self):
        """⚠ `managers_of` 已经把没配邮箱的区长回落成中台了 —— 再无条件加一次
        就是同一个地址两遍（收件人重复在某些邮箱里会被判成垃圾邮件）。"""
        from src import mailer
        (self.root / "config" / "managers.yaml").write_text(
            "managers:\n  - name: 杨英梅\n    accounts: ['SL1']\n    region: 西北区\n"
            "    email: ''\n    stores:\n      - 青岛城阳万达店\n",
            encoding="utf-8")
        to, _who = report._recipients(self.root, "青岛城阳万达店")
        self.assertEqual(to, [mailer.CENTRAL_ADDR])


# --------------------------------------------------------------------- 收信

def make_package(root, *, code="SCN231409", date="2026-09-21", tables=None,
                 protocol=1, manifest=True, path=None):
    """造一个"门店发出来的包"（形状照协议手搓：收信侧**只认 `_manifest`**）。"""
    tables = tables if tables is not None else {
        "orders": {"rows": 1, "mode": "incremental", "keys": ["document_no"],
                   "cols": ["document_no", "remark"], "hash": "h1"},
        "lg_stock": {"rows": 2, "mode": "snapshot", "keys": [],
                     "cols": ["snapshot_date", "sn"], "hash": "h2"},
    }
    p = Path(path or (Path(tempfile.mkdtemp(prefix="pkg-"))
                      / ("cbg-%s-%s.db" % (code, date))))
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE orders (document_no TEXT PRIMARY KEY, remark TEXT)")
    conn.execute("INSERT INTO orders VALUES ('D1','好')")
    conn.execute("CREATE TABLE lg_stock (snapshot_date TEXT, sn TEXT)")
    conn.execute("INSERT INTO lg_stock VALUES ('%s','SN1')" % date)
    conn.execute("INSERT INTO lg_stock VALUES ('%s','SN2')" % date)
    if manifest:
        conn.execute("""CREATE TABLE _manifest (protocol INTEGER, store_code TEXT,
            store_name TEXT, erp_name TEXT, tdoc_name TEXT, date TEXT, generated_at TEXT,
            version TEXT, reason TEXT, rows_total INTEGER, tables_json TEXT, cols_hash TEXT,
            prev_cols_hash TEXT, cols_changed_json TEXT, PRIMARY KEY (store_code, date))""")
        conn.execute("INSERT INTO _manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (protocol, code, "青岛城阳万达店", "青岛城阳万达店", "城阳万达", date,
                      "2026-09-21 21:03:00", "2.1.1", "daily", 3,
                      json.dumps(tables, ensure_ascii=False), "ch1", "", "[]"))
    conn.commit()
    conn.close()
    return str(p)


class Test收进来的东西放_in(unittest.TestCase):
    """⚠ 用户 2026-09-21：「**收取的文件应该放在 in 文件夹里吧，不应该放 out**」。

    分界线是"**这是别人发来的，还是我生出来的**"：
      * `in/`  = 各店发来的（`in/report.db` 收信库 + `in/packages/` 收到的原件）；
      * `out/` = 这台机器自己产出的（报告 / 本机抓的库 / 待发·已发的包 / 拆分文件）。

    ⚠ 这一条的要害不在路径好不好看，而在**改路径不能把已有的数据改丢** ——
      区长那台机器上 `out/report.db` 里已经有各家店的台账，
      不管它就等于"页面上显示从来没收到过，而数据还在盘上"。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))
        # ⚠ `_MIGRATED` 是模块级缓存（"这个根搬过了"）—— 每个用例都得清掉，
        #   否则第二个用例的搬迁逻辑压根不跑（假绿）。
        report_inbox._MIGRATED.clear()
        self.addCleanup(report_inbox._MIGRATED.clear)

    def test_库和原件都在_in_下(self):
        self.assertEqual(report_inbox.db_path(self.root),
                         self.root / "in" / "report.db")
        self.assertEqual(report_inbox.packages_dir(self.root),
                         self.root / "in" / "packages")

    def test_收到的东西落在_in_下(self):
        report_inbox.import_package(self.root, make_package(self.root))
        self.assertTrue((self.root / "in" / "report.db").is_file(), "收信库没落在 in/")
        self.assertFalse((self.root / "out" / "report.db").exists())

    def _legacy(self):
        """造一份"老位置"的现场：`out/report.db` + `out/report/inbox/<包>`。"""
        old_keep = self.root / report_inbox.LEGACY_REL
        old_keep.mkdir(parents=True, exist_ok=True)
        pkg = make_package(self.root)
        target = old_keep / Path(pkg).name
        shutil.copy(pkg, target)
        # 老位置那个库：`file` 列记的是**完整路径**（界面上那张表直接显示它）
        conn = sqlite3.connect(str(self.root / report_inbox.LEGACY_DB))
        conn.execute("CREATE TABLE inbox (store_code TEXT, report_date TEXT, file TEXT)")
        conn.execute("INSERT INTO inbox VALUES ('SCN231409','2026-09-21',?)",
                     (str(target),))
        conn.commit()
        conn.close()
        return target

    def test_老位置的东西自动搬过来(self):
        """⚠ **升级路径**：老位置的收信库和原件要搬到 `in/` 下，
        而且库里 `file` 那一列也得跟着改 —— 不然界面上点开是个**不存在的路径**。"""
        target = self._legacy()
        got = report_inbox.db_path(self.root)            # 任何读/写都会触发搬迁
        self.assertEqual(got, self.root / "in" / "report.db")
        self.assertTrue(got.is_file(), "库没搬过来")
        self.assertTrue((self.root / "in" / "packages" / target.name).is_file(),
                        "原件没搬过来")
        self.assertFalse((self.root / report_inbox.LEGACY_DB).exists())
        self.assertFalse((self.root / report_inbox.LEGACY_REL).exists(),
                         "老目录空了该收掉")
        conn = sqlite3.connect(str(got))
        try:
            f = conn.execute("SELECT file FROM inbox").fetchone()[0]
        finally:
            conn.close()
        self.assertIn(str(self.root / "in" / "packages"), f, "file 列还指着老路径")

    def test_新位置已经有东西就不搬(self):
        """**绝不覆盖**：新库里已经有数据，就把老位置原样留着（人工去处理）。"""
        old = self.root / report_inbox.LEGACY_DB
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"old")
        new = self.root / report_inbox.INBOX_DB
        new.parent.mkdir(parents=True, exist_ok=True)
        new.write_bytes(b"new")
        self.assertEqual(report_inbox.db_path(self.root), new)
        self.assertTrue(old.is_file(), "老库被覆盖/删了")
        self.assertEqual(new.read_bytes(), b"new")

    def test_搬不动就按老位置继续用(self):
        """⚠ **宁可位置乱，也不能让页面变成"从来没收到过"** ——
        搬不动（权限 / 文件被占用）时回落到老位置，而且不抛异常。"""
        self._legacy()
        with mock.patch.object(report_inbox.shutil, "move",
                               side_effect=OSError("拒绝访问")):
            got = report_inbox.db_path(self.root)
        self.assertEqual(got, self.root / report_inbox.LEGACY_DB, "没回落 ⇒ 数据看着像丢了")
        self.assertTrue((self.root / report_inbox.LEGACY_DB).is_file())


class Test收信落库(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))

    def test_收信未提供范围时默认不导入(self):
        pkg = make_package(self.root, code="SCN-OUTSIDE")
        item = {"subject": "[CBG上报] 未知范围门店", "attachments": [
            {"filename": Path(pkg).name, "data": Path(pkg).read_bytes()}]}
        from src.modules.fetch import mail as fetch_mail

        with mock.patch.object(report_inbox, "configured", return_value=(True, "")), \
             mock.patch.object(fetch_mail, "recent", return_value=[item]):
            result = report_inbox.run(self.root, cfg={})

        self.assertEqual(result["ok_packages"], 0)
        self.assertEqual(result["skipped_packages"], 1)
        self.assertEqual(report_inbox.stores(self.root), [])
        self.assertFalse((Path(self.root) / report_inbox.INBOX_REL
                          / Path(pkg).name).exists())

    def test_只有显式全量范围才导入所有门店(self):
        pkg = make_package(self.root, code="SCN-OUTSIDE")
        item = {"subject": "[CBG上报] 平台授权门店", "attachments": [
            {"filename": Path(pkg).name, "data": Path(pkg).read_bytes()}]}
        from src.modules.fetch import mail as fetch_mail

        with mock.patch.object(report_inbox, "configured", return_value=(True, "")), \
             mock.patch.object(fetch_mail, "recent", return_value=[item]):
            result = report_inbox.run(self.root, cfg={}, allowed_store_codes=None)

        self.assertEqual(result["ok_packages"], 1)
        self.assertEqual([row["store_code"] for row in report_inbox.stores(self.root)],
                         ["SCN-OUTSIDE"])

    def test_落库_台账加行(self):
        res = report_inbox.import_package(self.root, make_package(self.root))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["store_code"], "SCN231409")
        self.assertEqual(res["rows"], 3)
        conn = report_inbox.open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rows_").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        conn.close()

    def test_同一封收两次_不重复(self):
        p = make_package(self.root)
        report_inbox.import_package(self.root, p)
        res2 = report_inbox.import_package(self.root, p)
        self.assertTrue(res2["ok"])
        self.assertTrue(res2["duplicate"], "同一封没收出'重复投递'")
        conn = report_inbox.open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM rows_").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        conn.close()

    def test_快照表重复收只有一份的量(self):
        report_inbox.import_package(self.root, make_package(self.root))
        report_inbox.import_package(self.root, make_package(self.root))
        conn = report_inbox.open_db(self.root)
        n = conn.execute("SELECT COUNT(*) FROM rows_ WHERE table_name='lg_stock'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 2, "快照被收成了两份（该先清空再写）")

    def test_同店同日不同内容_覆盖(self):
        report_inbox.import_package(self.root, make_package(self.root))
        p2 = make_package(self.root, tables={"orders": {
            "rows": 2, "mode": "incremental", "keys": ["document_no"],
            "cols": ["document_no", "remark"], "hash": "h9"}})
        res = report_inbox.import_package(self.root, p2)
        self.assertTrue(res["ok"])
        conn = report_inbox.open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)
        conn.close()

    def test_坏包进_skips_且不抛(self):
        cases = ("不是 sqlite", "没有 manifest")
        for name in cases:
            with self.subTest(case=name):
                p = Path(tempfile.mkdtemp()) / "bad.db"
                if name == "没有 manifest":
                    p.write_bytes(Path(make_package(self.root, manifest=False)).read_bytes())
                else:
                    p.write_bytes("这不是数据库".encode("utf-8"))
                res = report_inbox.import_package(self.root, p)
                self.assertFalse(res["ok"])
                self.assertTrue(res["why"])
        self.assertEqual(len(report_inbox.skips(self.root)), len(cases))

    def test_协议版本不认识要说出来(self):
        res = report_inbox.import_package(self.root, make_package(self.root, protocol=99))
        self.assertFalse(res["ok"])
        self.assertIn("协议版本", res["why"])

    def test_没有门店码的包收不下(self):
        res = report_inbox.import_package(self.root, make_package(self.root, code=""))
        self.assertFalse(res["ok"])

    def test_店里能查最新一份(self):
        report_inbox.import_package(self.root, make_package(self.root))
        got = report_inbox.latest(self.root, "SCN231409")
        self.assertEqual(got["report_date"], "2026-09-21")
        self.assertIn("orders", got["tables"])
        self.assertEqual([s["store_code"] for s in report_inbox.stores(self.root)],
                         ["SCN231409"])

    def test_没配收信就跳过(self):
        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None:
                               (False, "这台机器没有自己的发信账号")):
            res = report_inbox.run(self.root, cfg={}, emit=None)
        self.assertTrue(res["ok"])
        self.assertIn("没有自己的发信账号", res["skipped"])

    def test_收信把附件落地并按主题筛(self):
        pkg = make_package(self.root)
        item = {"subject": "[CBG上报] SCN231409 2026-09-21", "date": "2026-09-21 21:03:00",
                "from": "x@y.z", "attachments": [
                    {"filename": Path(pkg).name, "content_type": "application/octet-stream",
                     "size": Path(pkg).stat().st_size, "data": Path(pkg).read_bytes()}]}
        from src.modules.fetch import mail as fetch_mail
        seen = {}

        def fake_recent(**k):
            seen.update(k)
            return [item]

        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None: (True, "")), \
                mock.patch.object(fetch_mail, "recent", fake_recent):
            res = report_inbox.run(
                self.root, cfg={}, allowed_store_codes=frozenset({"SCN231409"}),
                emit=None)
        # ⚠ 2026-09-21（M21）：**筛从"传给 recent"改成"自己在本地筛"** ——
        #   现在要认**两个**主题前缀（`[CBG上报]` + `[目标拆分]`），
        #   而 `recent()` 只收一个 `subject_contains`；调它两遍 = 白连两次 IMAP。
        #   所以：`recent(limit=…)` 拉回来，**本地按前缀挑**（QQ 的服务端筛本来就是假的）。
        self.assertIn("limit", seen)
        self.assertNotIn("subject_contains", seen)
        self.assertEqual(res["ok_packages"], 1)
        self.assertEqual(res["rows"], 3)
        self.assertTrue((Path(self.root) / report_inbox.INBOX_REL
                         / Path(pkg).name).is_file(), "附件没落地")

    def test_受限收信不保存也不导入授权范围外的包(self):
        pkg = make_package(self.root, code="SCN-OUTSIDE")
        item = {"subject": "[CBG上报] SCN-OUTSIDE 2026-09-21",
                "attachments": [{"filename": Path(pkg).name,
                                 "data": Path(pkg).read_bytes()}]}
        from src.modules.fetch import mail as fetch_mail

        with mock.patch.object(report_inbox, "configured", return_value=(True, "")), \
             mock.patch.object(fetch_mail, "recent", return_value=[item]):
            result = report_inbox.run(
                self.root, cfg={}, allowed_store_codes=frozenset({"SCN-IN-SCOPE"}))

        self.assertEqual(result["ok_packages"], 0)
        self.assertEqual(result["stores"], [])
        self.assertFalse((Path(self.root) / report_inbox.INBOX_REL
                          / Path(pkg).name).exists(), "越权包原件被保存到收信目录")
        self.assertEqual(report_inbox.stores(self.root), [])

    def test_受限收信不在日志暴露无法解密附件的文件名和原因(self):
        from src.modules.fetch import mail as fetch_mail
        cases = (
            ("[CBG上报] 未授权门店", "SCN-OUTSIDE-private-report.db"),
            ("[目标拆分] 未授权门店", "SCN-OUTSIDE-private-split.json"),
        )
        with mock.patch.object(report_inbox, "configured", return_value=(True, "")), \
             mock.patch("src.modules.fetch.unseal_attachment",
                        return_value=(b"", {"state": "failed", "why": "outside key detail"})):
            for subject, filename in cases:
                with self.subTest(filename=filename):
                    logs = []
                    item = {"subject": subject, "attachments": [
                        {"filename": filename, "data": b"sealed"}]}
                    with mock.patch.object(fetch_mail, "recent", return_value=[item]):
                        result = report_inbox.run(
                            self.root, cfg={},
                            allowed_store_codes=frozenset({"SCN-IN-SCOPE"}),
                            emit=logs.append)

                    self.assertEqual(result["problems"], [])
                    self.assertEqual(result["skipped_packages"], 1)
                    self.assertNotIn("SCN-OUTSIDE", "\n".join(logs))
                    self.assertNotIn("outside key detail", "\n".join(logs))

            # 显式全量调用的平台仍可看到解密诊断，便于排障。
            logs = []
            item = {"subject": "[CBG上报] 平台排障", "attachments": [
                {"filename": "SCN-OUTSIDE-private-report.db", "data": b"sealed"}]}
            with mock.patch.object(fetch_mail, "recent", return_value=[item]):
                result = report_inbox.run(self.root, cfg={},
                                          allowed_store_codes=None,
                                          emit=logs.append)
        self.assertIn("SCN-OUTSIDE-private-report.db", result["problems"][0])
        self.assertIn("outside key detail", "\n".join(logs))

    def test_没有附件的那封只记问题不收(self):
        from src.modules.fetch import mail as fetch_mail
        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None: (True, "")), \
                mock.patch.object(fetch_mail, "recent",
                                  lambda **k: [{"subject": "[CBG上报] 空手来的",
                                                "attachments": []}]):
            res = report_inbox.run(self.root, cfg={}, emit=None)
        self.assertEqual(res["ok_packages"], 0)
        self.assertTrue(res["problems"])

    def test_收信连不上要报出来(self):
        from src.modules.fetch import mail as fetch_mail

        def boom(**k):
            raise RuntimeError("连不上 imap.qq.com")

        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None: (True, "")), \
                mock.patch.object(fetch_mail, "recent", boom):
            res = report_inbox.run(self.root, cfg={}, emit=None)
        self.assertFalse(res["ok"], "收信失败被当成了'没有新邮件'")
        self.assertTrue(res["problems"])


# ------------------------------------------------------- 每店一张卡（M20）

class Test每店一张卡(unittest.TestCase):
    """收信库的**读出口**（M20）。⚠ 数据层这一份只管"给什么"，
    "谁能看"（范围 / 403）在 `tests/test_roles.py`。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))

    def test_没收到过也要有卡(self):
        got = report_inbox.cards(self.root, ["SCN231409", "SCN999999"])
        self.assertEqual([c["store_code"] for c in got], ["SCN231409", "SCN999999"])
        for c in got:
            with self.subTest(code=c["store_code"]):
                self.assertFalse(c["known"])
                self.assertIn("从来没收到过", c["why"])
                self.assertIsNone(c["stale_days"])

    def test_收到过就有来源和时间(self):
        report_inbox.import_package(self.root, make_package(self.root))
        # ⚠ 包日期钉在 2026-09-21：`today` 不传会用真实今天，stale_days 会跟着日历跑
        card = report_inbox.cards(self.root, ["SCN231409"], today="2026-09-21")[0]
        self.assertTrue(card["known"])
        self.assertEqual(card["report_date"], "2026-09-21")
        self.assertTrue(card["imported_at"])
        self.assertIn("orders", card["tables"])
        self.assertEqual(card["stale_days"], 0)

    def test_好几天没报就标出来(self):
        report_inbox.import_package(self.root, make_package(self.root, date="2026-09-01"))
        card = report_inbox.cards(self.root, ["SCN231409"], today="2026-09-21")[0]
        self.assertEqual(card["stale_days"], 20)

    def test_不带范围就列出库里所有店(self):
        report_inbox.import_package(self.root, make_package(self.root, code="SCNA"))
        report_inbox.import_package(self.root, make_package(self.root, code="SCNB"))
        codes = sorted(c["store_code"] for c in report_inbox.cards(self.root))
        self.assertEqual(codes, ["SCNA", "SCNB"])

    def test_跑成没跑成从包里读出来(self):
        """⚠ `run_record` 进包的**全部理由**：区长一眼看出"哪家店今天没跑成"。"""
        p = make_package(self.root)
        conn = sqlite3.connect(p)
        conn.execute("CREATE TABLE run_record (id INTEGER, kind TEXT, started_at TEXT, "
                     "finished_at TEXT, ok INTEGER, why TEXT)")
        conn.execute("INSERT INTO run_record VALUES (1,'daily','2026-09-21 21:00:00',"
                     "'2026-09-21 21:02:00',0,'云商登录失败')")
        conn.commit()
        conn.close()
        tables = {"run_record": {"rows": 1, "mode": "incremental", "keys": ["id"],
                                 "cols": ["id", "kind", "started_at", "finished_at",
                                          "ok", "why"], "hash": "h"}}
        man = sqlite3.connect(p)
        man.execute("UPDATE _manifest SET tables_json=?", (json.dumps(tables),))
        man.commit()
        man.close()
        report_inbox.import_package(self.root, p)
        card = report_inbox.cards(self.root, ["SCN231409"])[0]
        self.assertEqual(card["run"]["ok"], False)
        self.assertIn("云商登录失败", card["run"]["why"])

    def test_最近几天按天列出来(self):
        for d in ("2026-09-19", "2026-09-20", "2026-09-21"):
            report_inbox.import_package(self.root,
                                        make_package(self.root, date=d))
        days = report_inbox.store_days(self.root, "SCN231409", 2)
        self.assertEqual([x["report_date"] for x in days], ["2026-09-21", "2026-09-20"])


# ------------------------------------------- 人员状态表（区长/平台只读，C2 改过）

class Test人员状态表(unittest.TestCase):
    """用户 2026-09-21：「区长/平台**不能改**别家店的这份名单，**读取门店发送的状态表**吧」。

    ⇒ ① 只有**门店**能改（`/api/staff` PUT 对区长/平台 403）；
      ② 区长/平台读的是**门店发来的那张表**（跟着每天那趟上报包一起），**不是现场查云商**。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))
        self.conn = connect(self.root)
        add_snapshot(self.conn)
        add_run(self.conn)

    def test_上报包里带上人员状态表(self):
        import src.app.report as R
        with mock.patch.dict(R.PROVIDERS, {"staff": lambda root, cfg, config_path=None: [
                {"account": "sl001", "real": "张三", "phone": "138", "active": 1},
                {"account": "sl002", "real": "李四", "phone": "139", "active": 0}]}):
            info = R.build(self.root, cfg=dict(CFG))
        meta = info["tables"]["staff"]
        self.assertEqual(meta["rows"], 2)
        self.assertEqual(meta["mode"], "snapshot", "人员名单该是快照语义（以这份为准）")
        self.assertEqual(meta["keys"], [])
        conn = sqlite3.connect(info["file"])
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM staff ORDER BY account")]
        conn.close()
        self.assertEqual([r["real"] for r in rows], ["张三", "李四"])
        self.assertEqual(rows[1]["active"], 0)

    def test_拿不到名单就不带这张表不报错(self):
        import src.app.report as R
        with mock.patch.dict(R.PROVIDERS, {"staff": lambda root, cfg, config_path=None: []}):
            info = R.build(self.root, cfg=dict(CFG))
        self.assertTrue(info["ok"])
        self.assertEqual(info["tables"]["staff"]["rows"], 0)

    def test_取数函数收到的是这次真正在用的配置(self):
        """⚠⚠ 2026-09-21 实测踩到：`_staff_rows` 原来写死
        `config/store-SCN231409.yaml` —— 而机器上真正用的是别的名字
        （临时 root / 换过店名的机器）⇒ 读不到店名 ⇒ `staff_state()` 直接
        「还没认出这家店」⇒ **人员表永远是空的**。"""
        import src.app.report as R
        seen = {}

        def fake(root, cfg, config_path=None):
            seen["config_path"] = config_path
            return [{"account": "a", "real": "甲", "active": 1}]

        with mock.patch.dict(R.PROVIDERS, {"staff": fake}):
            info = R.build(self.root, cfg=dict(CFG),
                           config_path="config/store-X.yaml")
        self.assertEqual(seen.get("config_path"), "config/store-X.yaml",
                         "取数函数没拿到这次真正在用的配置")
        self.assertEqual(info["tables"]["staff"]["rows"], 1)

    def test_拿不到名单要说清为什么(self):
        """⚠ 2026-09-21 实测踩到：它**静默**空着 ⇒ "包里没这张表"和"这家店真没员工"
        看起来一模一样（真发那一下才发现是云商 token 过期、名单压根没取到）。"""
        import src.app.report as R
        with mock.patch("src.features.store.staff.staff_state",
                        lambda *a, **k: {"ok": False, "error": "读云商用户名单失败：token 过期",
                                         "people": []}):
            info = R.build(self.root, cfg=dict(CFG))
        self.assertIn("token 过期", info.get("staff_why") or "",
                      "人员表空了却不说原因")
        lines = []
        R.run(self.root, cfg=dict(CFG), no_push=True, emit=lines.append)
        # `--no-push` 那条路不发，所以这里直接看 build 带出来的原因（上面已断言）

    def test_区长侧读得出来(self):
        """端到端：门店打包（带那张表）→ 收下 → `tables_of()` 按店读出。"""
        import src.app.report as R
        with mock.patch.dict(R.PROVIDERS, {"staff": lambda root, cfg, config_path=None: [
                {"account": "sl001", "real": "张三", "active": 1}]}):
            info = R.build(self.root, cfg=dict(CFG))
        boss = Path(tempfile.mkdtemp())
        make_root(boss)
        report_inbox.import_package(boss, info["file"])
        got = report_inbox.tables_of(boss, "staff")
        self.assertEqual(len(got), 1)
        code, one = got[0]
        self.assertEqual(code, "SCN231409")
        self.assertEqual([p["real"] for p in one["rows"]], ["张三"])
        self.assertTrue(one["report_date"])

    def test_快照语义_重收不会叠人(self):
        import src.app.report as R
        boss = Path(tempfile.mkdtemp())
        make_root(boss)
        for people in (["张三", "李四"], ["张三"]):
            with mock.patch.dict(R.PROVIDERS, {"staff": lambda root, cfg, config_path=None, ps=people: [
                    {"account": "sl%03d" % i, "real": n, "active": 1}
                    for i, n in enumerate(ps, 1)]}):
                info = R.build(self.root, cfg=dict(CFG))
            report_inbox.import_package(boss, info["file"])
        got = report_inbox.tables_of(boss, "staff")
        self.assertEqual([p["real"] for p in got[0][1]["rows"]], ["张三"], "快照没覆盖（叠人了）")


# ------------------------------------------------------------------ 接线

class Test接线(unittest.TestCase):
    def test_登记了两步而且上报不注册定时器(self):
        from src.features import registry
        cmds = {s.cmd: s for s in registry.all_steps()}
        self.assertIn("report", cmds)
        self.assertIn("report-inbox", cmds)
        # ⚠ 2026-09-21 晚（用户：「**上报数据还是有自己的吧**」）：它**有**自己的时刻
        #   （21:15）—— 原来 `whens=()` 时界面上那个开关永远开不了，看着像坏了。
        self.assertTrue(cmds["report"].whens, "上报要有自己的时刻")
        self.assertEqual(cmds["report"].whens[0].text(), "每天 21:15")
        self.assertFalse(cmds["report"].default, "有自己的时刻了 ⇒ 不进整批（否则一天两遍）")
        self.assertTrue(cmds["report-inbox"].whens, "收信要有自己的时刻")
        self.assertIn("report", [s.cmd for s in registry.wakes()])
        self.assertIn("report-inbox", [s.cmd for s in registry.wakes()])

    def test_归属指对了模块(self):
        from src.features import registry
        for cmd in ("report", "report-inbox"):
            with self.subTest(cmd=cmd):
                self.assertEqual(registry.step_owner(cmd)["label"], "数据交换")

    def test_跳过开关都在(self):
        from src import run_daily
        self.assertEqual(run_daily.STEP_FLAGS["report"], "--skip-report")
        self.assertEqual(run_daily.STEP_FLAGS["report-inbox"], "--skip-report-inbox")

    def test_合作店点名跑上报不被早退(self):
        """⚠ autoupdate 当年就踩过这个坑：合作店那条早退判据里写死了几个 cmd。"""
        from src import run_daily
        called = []
        with tempfile.TemporaryDirectory() as tmp:
            root = make_root(Path(tmp), name="青岛麦凯乐店", store_code="CNSCN258646")
            (root / "config" / "stores.yaml").write_text(
                "stores:\n  - erp_name: 青岛麦凯乐店\n    tdoc_name: 麦凯乐\n"
                "    kind: 合作店\n    marker: ''\n", encoding="utf-8")
            with mock.patch("src.app.report.run",
                                   lambda **k: called.append(k) or {"ok": True}), \
                    mock.patch.object(run_daily.cli, "ROOT", root):
                run_daily.main(["--config", str(root / "config" / "store-X.yaml"),
                                "--steps", "report", "--no-push"])
        self.assertTrue(called, "合作店点名跑上报被'没活干'早退了")

    def test_上报不在每天那趟里(self):
        """⚠ 2026-09-21 晚：它有自己的时刻（21:15）⇒ **不在"每天那趟"的名单里**
        （那份名单现在只有一处：`run_daily.MANUAL_STEPS` ——
         界面上的「整个项目」按钮和 `BUTTON_STEPS` 那套预设都删掉了）。"""
        from src import run_daily
        self.assertNotIn("report", run_daily.MANUAL_STEPS)
        self.assertNotIn("report-inbox", run_daily.MANUAL_STEPS)
        self.assertIn("report", run_daily.STEPS, "它仍然得是一步（有自己的时刻）")


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------- 目标拆分走邮件（M21）

def _split_item(root, *, code="SCN231409", period="2026-W38", **kw):
    """造一封"门店发来的目标拆分"（用**真的打包代码**，免得协议两边各写一份）。"""
    from src.features.sales.attain import split as split_mod
    pkg = split_mod.package(
        root, kw.pop("store", "青岛城阳万达店"), period,
        columns=["X6", "X7"],
        members=[{"name": "张三", "targets": [2, 1]},
                 {"name": "李四", "targets": [0, 3]}],
        cfg={"store_code": code, "erp_store_name": "青岛城阳万达店"}, **kw)
    f = split_mod.write_package(root, pkg)
    return {"subject": "%s %s 2026-W38" % (report_inbox.SPLIT_PREFIX, code),
            "date": "2026-09-21 21:05:00", "from": "x@y.z",
            "attachments": [{"filename": f.name, "content_type": "application/json",
                             "size": f.stat().st_size, "data": f.read_bytes()}]}


class Test目标拆分走邮件(unittest.TestCase):
    """M21：**店长改 → 发出去 → 区长/平台收信落库 → 只读加载**（C1）。

    ⚠ 收信这一步**没法验"发件人是不是店长"** —— 能验的是"包里写的是哪家店"
      + 主题前缀；身份那半只能在包里标 `unverified`，页面上写出来。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_root(Path(self.tmp.name))

    def test_收得下而且按店按周存(self):
        item = _split_item(self.root)
        a = item["attachments"][0]
        p = Path(self.root) / report_inbox.INBOX_REL / a["filename"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(a["data"])
        res = report_inbox.import_split(self.root, p, subject=item["subject"])
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["store_code"], res["period"]), ("SCN231409", "2026-W38"))
        self.assertEqual(res["members"], 2)
        got = report_inbox.split_of(self.root, "青岛城阳万达店", "2026-W38")
        self.assertEqual(got["targets"]["张三"], [2, 1])
        self.assertEqual(got["columns"], ["X6", "X7"])

    def test_同一周重发是覆盖(self):
        for targets in ([2, 1], [5, 5]):
            item = _split_item(self.root, members=None) if False else None
            from src.features.sales.attain import split as split_mod
            pkg = split_mod.package(self.root, "青岛城阳万达店", "2026-W38",
                                    columns=["X6", "X7"],
                                    members=[{"name": "张三", "targets": targets}],
                                    cfg={"store_code": "SCN231409"})
            p = split_mod.write_package(self.root, pkg)
            report_inbox.import_split(self.root, p)
        got = report_inbox.split_of(self.root, "青岛城阳万达店", "2026-W38")
        self.assertEqual(got["targets"]["张三"], [5, 5], "同一周重发没覆盖")
        conn = report_inbox.open_db(self.root)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM splits").fetchone()[0], 1)
        conn.close()

    def test_坏包进_skips_不抛(self):
        p = Path(tempfile.mkdtemp()) / "split-x.json"
        for body in ("not json", '{"protocol": 99, "store_code": "X", "period": "P"}',
                     '{"protocol": 1, "store_code": "", "period": "2026-W38"}'):
            with self.subTest(body=body[:20]):
                p.write_text(body, encoding="utf-8")
                res = report_inbox.import_split(self.root, p)
                self.assertFalse(res["ok"])
                self.assertTrue(res["why"])
        self.assertEqual(len(report_inbox.skips(self.root)), 3)

    def test_收信那一趟会顺便收拆分包(self):
        """⚠ 同一台机器、同一个邮箱 —— M21 的拆分包跟着 M18 的收信一起进来。"""
        from src.modules.fetch import mail as fetch_mail
        item = _split_item(self.root)
        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None: (True, "")), \
                mock.patch.object(fetch_mail, "recent", lambda **k: [item]):
            res = report_inbox.run(
                self.root, cfg={}, allowed_store_codes=frozenset({"SCN231409"}),
                emit=None)
        self.assertEqual(res.get("splits"), ["SCN231409 2026-W38（2 人）"])
        self.assertTrue(report_inbox.split_of(self.root, "青岛城阳万达店", "2026-W38"))

    def test_受限收信不保存也不导入授权范围外的拆分(self):
        from src.modules.fetch import mail as fetch_mail
        item = _split_item(self.root, code="SCN-OUTSIDE")
        filename = item["attachments"][0]["filename"]

        with mock.patch.object(report_inbox, "configured", return_value=(True, "")), \
             mock.patch.object(fetch_mail, "recent", return_value=[item]):
            result = report_inbox.run(
                self.root, cfg={}, allowed_store_codes=frozenset({"SCN-IN-SCOPE"}))

        self.assertEqual(result.get("splits", []), [])
        self.assertFalse(report_inbox.split_of(self.root, "SCN-OUTSIDE", "2026-W38"))
        self.assertFalse((Path(self.root) / report_inbox.INBOX_REL / filename).exists())

    def test_别的功能的_json_不碰(self):
        from src.modules.fetch import mail as fetch_mail
        item = {"subject": "别的什么东西", "attachments": [
            {"filename": "x.json", "data": b"{}"}]}
        with mock.patch.object(report_inbox, "configured",
                               lambda cfg=None, root=None: (True, "")), \
                mock.patch.object(fetch_mail, "recent", lambda **k: [item]):
            res = report_inbox.run(self.root, cfg={}, emit=None)
        self.assertEqual(res["ok_packages"], 0)
        self.assertFalse(report_inbox.split_of(self.root, "青岛城阳万达店", "2026-W38"))

    def test_包里那几样都在(self):
        """四·八数据契约：哪家店 / 哪个周 / 谁 / 每项多少台 / 追溯 一样不能少。"""
        from src.features.sales.attain import split as split_mod
        pkg = split_mod.package(self.root, "青岛城阳万达店", "2026-W38",
                                columns=["X6"], members=[{"name": "张三", "targets": [2]}],
                                cfg={"store_code": "SCN231409"}, saved_by="sl0001")
        for k in ("protocol", "store_code", "store_name", "erp_name", "tdoc_name",
                  "period", "start", "end", "generated_at", "version", "saved_by",
                  "unverified", "columns", "members"):
            with self.subTest(k=k):
                self.assertIn(k, pkg)
        self.assertEqual((pkg["start"], pkg["end"]), ("2026-09-14", "2026-09-20"))
        self.assertFalse(pkg["unverified"], "带了登录名就不该标没核实")
        self.assertEqual(pkg["members"][0]["targets"], [2])

    def test_没登录名要标出来(self):
        from src.features.sales.attain import split as split_mod
        pkg = split_mod.package(self.root, "青岛城阳万达店", "2026-W38",
                                columns=[], members=[], cfg={"store_code": "S"})
        self.assertTrue(pkg["unverified"])

    def test_周换算成起止日期(self):
        from src.features.sales.attain import split as split_mod
        self.assertEqual([str(d) for d in split_mod.period_range("2026-W38")],
                         ["2026-09-14", "2026-09-20"])
        self.assertEqual(split_mod.period_range("乱写的"), (None, None))

    def test_发出去的邮件带一份_json(self):
        """⚠ 附件是**给机器读的**：正文照旧给人看（区长手机上那条不变）。"""
        from src.features.sales.attain import split as split_mod
        sent = []

        def fake_send(code, content, *, cfg, root=None):
            sent.append(content)
            return {"ok": True, "why": "", "code": code}

        (self.root / "config" / "managers.yaml").write_text(
            "managers:\n  - name: 杨英梅\n    accounts: ['SL1']\n    region: 西北区\n"
            "    email: 'jiuzhang@example.com'\n    stores:\n      - 青岛城阳万达店\n",
            encoding="utf-8")
        split_mod.set_targets(self.root, "青岛城阳万达店", "2026-W38",
                              {"张三": [2, 1]}, 2)
        (self.root / "out" / "attain-2026.json").write_text(
            json.dumps({"columns": ["X6", "X7"]}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(split_mod, "load", lambda root: {
                split_mod.key("青岛城阳万达店", "2026-W38"): {"张三": [2, 1]}}), \
                mock.patch("src.modules.notify.send", fake_send), \
                mock.patch("src.mailer.load_mail_config",
                           lambda c, r: type("M", (), {"enabled": True})()):
            res = split_mod.send_report(self.root, "青岛城阳万达店", "2026-W38",
                                        cfg={"store_code": "SCN231409"})
        self.assertEqual(res["sent"], 1, res)
        atts = sent[0].get("attachments") or ()
        self.assertEqual(len(atts), 1, "邮件里没带那份 JSON")
        self.assertTrue(str(atts[0]).endswith(".json"))
        blob = json.loads(Path(atts[0]).read_text(encoding="utf-8"))
        self.assertEqual(blob["store_code"], "SCN231409")
        self.assertEqual(blob["members"][0]["targets"], [2, 1])
