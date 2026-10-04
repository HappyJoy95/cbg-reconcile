"""M22 —— **导出为 Excel**（`src/modules/notify/export.py`）。

用户 2026-09-21 原话：

> 「区长账号有**导出为 excel** 功能，这个**落在数据推送模块**吧，
>   功能模块**调用「导出为 excel」**来把自己的数据生成为 excel **到本地**」

这一份盯四件事：

1. **能力本身**：写得出 / 读得回 / 同名不覆盖 / 文件名洗得能在 Windows 上建出来 /
   失败**不抛**且说得出为什么（导出的失败模式全是"用户点了按钮在等一个文件"）；
2. **落盘位置**：`out/exports/`，**不是 `out/` 根** —— 那儿是每天的差异清单
   （`report.list_reports()` 就在那儿 glob），导出件混进去分不清哪些是人点的；
3. **口径进文件**：达成率**存数字、显示成百分比**（不是文本 "89.3%"）；
   没算的列写**空单元格**，**绝不写 0**（写 0 就是"这家店一列都没卖"，而真相是"没算"）；
4. ⚠⚠ **导出不是绕过权限的第二条路**：接口导出来的必须**正好**是
   `App.attain()` 给这个身份的那几行（门店=本店）。判据只有 `role_scope()` 一处。
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import web                                                   # noqa: E402
from src.features.sales.attain import export as attain_export         # noqa: E402
from src.modules import notify                                        # noqa: E402
from src.storage import runlog                                        # noqa: E402
from src.xlsx_io import read_sheets                                   # noqa: E402

APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js",
                              "features/valueadd/film/page.js",
                              "features/valueadd/benefit/page.js",
                              "features/sales/attain/page.js", "app.js"))
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _tmp_root():
    """临时安装目录 —— ⚠ **得有一个库文件**，`runlog` 才有地方记（照 `test_modules` 的套路）。

    没有它的话 `runlog.record` 会**静默什么都不做**（它按设计"绝不抛"），
    于是"留痕"那两条测试会红在一个看不出原因的地方。
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    return tmp, root


def _payload():
    """一小份达成数据 —— **口径跟真的一样**（两种门店名、没映射的列、没有 people 的行）。"""
    return {
        "exists": True, "period": "2026-W38", "start": "2026-09-14",
        "end": "2026-09-20", "data_until": "2026-09-17",
        "computed_at": "2026-09-21 09:00", "file": "attain-2026.json",
        "columns": ["A品", "B品"], "weights": [0.6, 0.4], "missing_columns": ["C品"],
        "rows": [
            {"store": "青岛城阳万象汇店", "erp_name": "青岛城阳万象汇店", "matched": True,
             "targets": [6, 3], "actuals": [7, 2], "rates": [1.2, 0.6667], "total": 0.8933,
             "people": [[["张三", 7, ["商品1", "商品2"]]], [["李四", 2, ["商品3"]]]]},
            # 区长所辖里的**第二家** —— 他应该导得到，门店账号导不到
            {"store": "青岛城阳万达店", "erp_name": "青岛城阳万达店", "matched": True,
             "targets": [3, 3], "actuals": [3, 1], "rates": [1.0, 0.3333], "total": 0.7333,
             "people": [[["王五", 3, ["商品4"]]], []]},
            # 没映射上云商门店 + 一列没算（`rates[0] is None`）
            {"store": "鲁疆广场", "erp_name": "", "matched": False,
             "targets": [0, 4], "actuals": [0, 0], "rates": [None, 0.0], "total": None,
             "people": []},
        ],
    }


def _overview_rows(book):
    """「总览」那张表的**数据行**。

    ⚠ 它的表头是**两行**（第一行产品名分组、第二行 达成/目标/达成率，见
      `overview_sheet`），所以数据从**第 3 行**开始 —— 老写法 `[1:]` 会把
      "达成/目标/达成率"当成一家门店。
    """
    return book["总览"][2:]


class _Server:
    """真起一个 HTTP 服务 —— 测的是**路由和状态码**，不是 mock 出来的路由。

    ⚠ 登录门禁（`setup_state`）在 `/api/*` 前面：这一份测的是导出，
      门禁另有 `test_setup_gate.py` 盯着，所以直接放行。
    """

    def __init__(self, root: Path, store: str = "青岛城阳万象汇店"):
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "%s"\nstore_code: "SCN231409"\n' % store, encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _ServerCase(unittest.TestCase):
    #: 这台机器登录的是哪家店
    STORE = "青岛城阳万象汇店"

    def setUp(self):
        self.tmp, self.root = _tmp_root()
        self.addCleanup(self.tmp.cleanup)
        (self.root / "out" / "attain-2026.json").write_text(
            json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
        self.srv = _Server(self.root, store=self.STORE)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def export(self, **kw):
        st, d = self.srv.request("POST", "/api/attain/export", kw or {})
        return st, d


class Test能力(unittest.TestCase):
    """`notify.export_xlsx` —— 六条边界，每条都对应一个真会发生的失败。"""

    def setUp(self):
        self.tmp, self.root = _tmp_root()
        self.addCleanup(self.tmp.cleanup)

    def export(self, sheets, **kw):
        kw.setdefault("name", "测试")
        return notify.export_xlsx(sheets, root=self.root, **kw)

    def test_单表_写得出也读得回(self):
        r = self.export((["门店", "台量"], [["A店", 3], ["B店", 5]]))
        self.assertTrue(r["ok"], r["why"])
        self.assertEqual(r["sheets"], ["数据", "说明"])          # 没给表名 → 「数据」
        rows = read_sheets(r["path"])["数据"]
        self.assertEqual(rows[0], ["门店", "台量"])
        self.assertEqual(rows[1], ["A店", 3])
        self.assertEqual(rows[2], ["B店", 5])

    def test_多表_顺序不变_说明在最后(self):
        r = self.export({"总览": (["a"], [[1]]), "明细": (["b"], [[2]])})
        self.assertEqual(r["sheets"], ["总览", "明细", "说明"])
        book = read_sheets(r["path"])
        self.assertEqual(list(book), ["总览", "明细", "说明"])

    def test_表头写键_行写字典(self):
        """功能模块手里就是一堆 dict —— 别逼它们先转成按位置的数组（转错了看不出来）。"""
        r = self.export({"总览": ([("store", "门店"), ("qty", "台量")],
                                  [{"store": "A店", "qty": 3}])})
        self.assertEqual(read_sheets(r["path"])["总览"][1], ["A店", 3])

    def test_行是字典而表头只有文字_直接报错(self):
        """⚠ 按 `dict.values()` 取值**不会报错**，只会把两列换个个儿（Excel 上看不出来）。"""
        r = self.export((["门店", "台量"], [{"store": "A店", "qty": 3}]))
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["why"])
        self.assertEqual(r["path"], "")

    def test_百分比存的是数字_显示才是百分比(self):
        """⚠ 存文本 "89.3%" 就没法在 Excel 里求和/排序了 —— 数字 + `number_format` 才对。"""
        import openpyxl
        r = self.export({"总览": (["门店", "达成率"], [["A店", 0.8933]], {2: "0.0%"})})
        ws = openpyxl.load_workbook(r["path"])["总览"]
        self.assertAlmostEqual(ws.cell(2, 2).value, 0.8933, places=4)
        self.assertEqual(ws.cell(2, 2).number_format, "0.0%")

    def test_多行表头_分组格合并_冻结在第3行(self):
        """用户 2026-09-21：「每个品项要有**达成/目标/达成率三个**」⇒ 表头两行。

        第一行是分组名（`None` = 跟左边合并），第二行是叶子名（`""` = 跟上面合并）。
        """
        import openpyxl
        r = self.export({"总览": ([["门店", None, None, "A品", None, None],
                                  ["", "", "", "达成", "目标", "达成率"]],
                                 [["青岛城阳万象汇店", "", "", 7, 6, 1.2]])})
        self.assertTrue(r["ok"], r["why"])
        ws = openpyxl.load_workbook(r["path"])["总览"]
        merged = {str(m) for m in ws.merged_cells.ranges}
        self.assertIn("A1:A2", merged)              # 门店：竖着跨两行
        self.assertIn("D1:F1", merged)              # A品：横着跨三列
        self.assertEqual(ws.freeze_panes, "A3")     # 冻结在表头下面那一行
        self.assertEqual(ws["D1"].value, "A品")
        self.assertEqual(ws["D2"].value, "达成")
        self.assertEqual(ws["E2"].value, "目标")
        self.assertEqual(ws["F2"].value, "达成率")

    def test_列宽按显示宽度算_汉字占两格(self):
        """⚠ 用 `len()` 算的话「青岛城阳万象汇店」算 8 宽、实际要 16 ⇒ 显示成「青岛城阳万…」。"""
        import openpyxl
        r = self.export((["门店", "台量"], [["青岛城阳万象汇店", 7]]))
        ws = openpyxl.load_workbook(r["path"])["数据"]
        self.assertGreaterEqual(ws.column_dimensions["A"].width, 16)

    def test_多行表头配字典行_报错(self):
        r = self.export(([["门店", None], ["", "达成"]], [{"store": "A店"}]))
        self.assertFalse(r["ok"])
        self.assertIn("多行表头", r["why"])

    def test_键标题要写tuple_写成list就成了表头行(self):
        """⚠ 这是两种写法**唯一**的区别，写错了不报错、只会把列接错（见 `_header_pairs`）。"""
        r = self.export(([("store", "门店")], [{"store": "A店"}]))
        self.assertEqual(read_sheets(r["path"])["数据"][1], ["A店"])
        r2 = self.export(([["store", "门店"], ["", "达成"]], [["A店", 1]]))
        self.assertTrue(r2["ok"], r2["why"])
        self.assertEqual(read_sheets(r2["path"])["数据"][0], ["store", "门店"])   # 这是**表头**，不是键

    def test_列格式也能写表头文字(self):
        import openpyxl
        r = self.export({"总览": (["门店", "达成率"], [["A店", 0.5]], {"达成率": "0%"}),
                         "明细": (["x"], [[1]], {"列占比": "0%"})})
        self.assertFalse(r["ok"])                                # 第二张表写错了列名 → 报错
        self.assertIn("列占比", r["why"])
        r2 = self.export({"总览": (["门店", "达成率"], [["A店", 0.5]], {"达成率": "0%"})})
        self.assertEqual(openpyxl.load_workbook(r2["path"])["总览"].cell(2, 2).number_format, "0%")

    def test_没算的列写空单元格_不写0(self):
        r = self.export((["门店", "A品"], [["鲁疆广场", ""], ["A店", None]]))
        book = read_sheets(r["path"])["数据"]
        self.assertEqual(book[1], ["鲁疆广场", None])
        self.assertEqual(book[2], ["A店", None])

    def test_同名不覆盖_往后编号(self):
        """⚠ Windows 上"文件正被 Excel 打开着"覆盖会 `PermissionError` —— 干脆不覆盖。"""
        a = self.export((["x"], [[1]]), name="同名")
        b = self.export((["x"], [[1]]), name="同名")
        self.assertNotEqual(a["path"], b["path"])
        self.assertTrue(a["path"].endswith("-2.xlsx") is False)
        self.assertIn("-2.xlsx", b["file"])
        self.assertTrue(Path(a["path"]).is_file() and Path(b["path"]).is_file())

    def test_洗文件名_windows上建得出来(self):
        """`\\ / : * ? " < > |` 一个都不能留，而且**结尾不能是点或空格**。"""
        r = self.export((["x"], [[1]]), name='达成 2026/09:21 *全区* ？')
        self.assertTrue(r["ok"], r["why"])
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, r["file"])
        self.assertFalse(Path(r["file"]).stem.endswith((" ", ".")))
        self.assertEqual(notify.safe_stem(""), "导出",
                         "空名字要有兜底，否则最终文件名会变成 `-20260921-1042.xlsx`")
        self.assertLessEqual(len(notify.safe_stem("很长" * 100)), notify.MAX_STEM)
        self.assertLessEqual(len(notify.safe_sheet("很长" * 40)), 31)   # Excel 的表名上限

    def test_落盘在_out_exports_不是_out根(self):
        r = self.export((["x"], [[1]]))
        self.assertEqual(r["dir"], "out/exports")
        self.assertEqual(r["rel"], "out/exports/" + r["file"])
        self.assertEqual(Path(r["path"]).parent, self.root / "out" / "exports")
        # ⚠ 相对路径一律正斜杠（Windows 上是反斜杠，写进 JSON 到前端就成了转义 —— 坑 1）
        self.assertNotIn("\\", r["rel"])

    def test_失败也不抛_而且说得出为什么(self):
        with mock.patch("src.xlsx_io.write_sheets", side_effect=OSError("磁盘满了")):
            r = self.export((["x"], [[1]]))
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], notify.EXPORT_FAILED)
        self.assertIn("磁盘满了", r["why"])
        self.assertEqual((r["path"], r["file"], r["rel"]), ("", "", ""))

    def test_空表也是失败_不是空文件(self):
        r = self.export({})
        self.assertFalse(r["ok"])
        self.assertIn("空的", r["why"])

    def test_失败也留痕(self):
        """用户那边只看到"点了没反应"，运行记录是唯一查得到的地方。"""
        with mock.patch("src.xlsx_io.write_sheets", side_effect=OSError("满了")):
            self.export((["x"], [[1]]), name="失败的")
        rows = runlog.recent(self.root, limit=5, kind="export")
        self.assertTrue(rows, "失败没记进 runlog")
        self.assertFalse(rows[0]["ok"])
        self.assertIn("满了", json.dumps(rows[0], ensure_ascii=False))

    def test_谁导的_进说明也进运行记录(self):
        r = self.export((["x"], [[1]]), name="留痕", who="杨英梅")
        meta = read_sheets(r["path"])["说明"]
        self.assertIn(["导出人", "杨英梅"], meta)
        rows = runlog.recent(self.root, limit=5, kind="export")
        self.assertIn("杨英梅", json.dumps(rows[0], ensure_ascii=False))

    def test_说明表自己说清自己是什么(self):
        r = self.export({"总览": (["x"], [[1]])}, name="说明表",
                        meta={"期间": "2026-W38", "数据截至": "2026-09-17"})
        meta = dict((k, v) for k, v in read_sheets(r["path"])["说明"][1:])
        self.assertEqual(meta["导出内容"], "说明表")
        self.assertEqual(meta["期间"], "2026-W38")
        self.assertEqual(meta["数据截至"], "2026-09-17")
        self.assertIn("生成时间", meta)
        self.assertIn("2 张表", meta["共"])

    def test_recent_exports(self):
        self.export((["x"], [[1]]), name="甲")
        rows = notify.recent_exports(self.root)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["name"].endswith(".xlsx"))
        self.assertIn("out/exports/", rows[0]["rel"])
        self.assertTrue(rows[0]["at"])


class Test达成摆表(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = _tmp_root()
        self.addCleanup(self.tmp.cleanup)
        self.d = _payload()

    def test_三张表_谁卖的也在(self):
        sheets = attain_export.sheets_of(self.d)
        self.assertEqual(list(sheets), ["总览", "谁卖的"])

    def test_没有people就不出谁卖的(self):
        d = dict(self.d, rows=[dict(r, people=[]) for r in self.d["rows"]])
        self.assertEqual(list(attain_export.sheets_of(d)), ["总览"])

    def test_总览一行一店_匹配没匹配都写出来(self):
        header, rows, fmt = attain_export.overview_sheet(self.d)
        top, sub = header
        self.assertEqual(top[:3], ["门店", "云商门店", "匹配"])
        self.assertEqual(sub[:3], ["", "", ""])
        by = {r[0]: r for r in rows}
        self.assertEqual(by["青岛城阳万象汇店"][:3],
                         ["青岛城阳万象汇店", "青岛城阳万象汇店", "✓"])
        self.assertIn("✗", by["鲁疆广场"][2])       # 云商里没这个名字 → 说出来，别混在正常行里

    def test_每个品项三格_达成目标达成率(self):
        """用户 2026-09-21 看图后定的：「每个品项要有**达成/目标/达成率三个**」。"""
        top, sub = attain_export.overview_sheet(self.d)[0]
        # A 品占第 4~6 列：分组名在第 4 格，后面两格写 `None`（= 跟左边合并）
        self.assertEqual(top[3:6], ["A品（60%）", None, None])
        self.assertEqual(sub[3:6], ["达成", "目标", "达成率"])
        # B 品同理（权重 0.4 → 40%）
        self.assertEqual(top[6:9], ["B品（40%）", None, None])
        self.assertEqual(sub[6:9], ["达成", "目标", "达成率"])
        self.assertEqual(top[9], "总达成率")
        self.assertEqual(sub[9], "")                # 竖着合并 ⇒ 下面那格留空
        _h, rows, fmt = attain_export.overview_sheet(self.d)
        self.assertEqual(rows[0][3:9], [7, 6, 1.2, 2, 3, 0.6667])   # 达成 / 目标 / 达成率
        self.assertEqual(fmt[6], "0.0%")            # A 品的达成率那格
        self.assertEqual(fmt[9], "0.0%")            # B 品
        self.assertEqual(fmt[10], "0.0%")           # 总达成率

    def test_没落下的长表_列占比挪进了表头(self):
        """⚠ 「目标与达成」那张长表用户让删了 —— 它承载的**列占比**搬进分组表头。"""
        top = attain_export.overview_sheet(self.d)[0][0]
        self.assertIn("60%", top[3])
        self.assertIn("40%", top[6])

    def test_没算的列是空的_不是0(self):
        _h, rows, _f = attain_export.overview_sheet(self.d)
        lu = {r[0]: r for r in rows}["鲁疆广场"]
        self.assertEqual(lu[3:6], [0, 0, ""])       # A品没配编码 ⇒ **达成率那格空着**（不许写 0%）
        self.assertEqual(lu[6:9], [0, 4, 0.0])      # B品算得出来：真的是 0%
        self.assertEqual(lu[9], "")                 # 总达成率：一列都没算 → None

    def test_谁卖的摊平人和商品(self):
        _h, rows, _f = attain_export.people_sheet(self.d)
        self.assertEqual(rows[0], ["青岛城阳万象汇店", "A品", "张三", 7, "商品1；商品2"])
        self.assertEqual(len(rows), 3)

    def test_文件名带期间(self):
        self.assertEqual(attain_export.file_name(self.d), "周度达成-2026-W38")
        self.assertEqual(attain_export.file_name({}), "周度达成")

    def test_说明写清口径和_数据截至(self):
        m = attain_export.meta_of(self.d, who="杨英梅")
        self.assertIn("2026-W38", m["期间"])
        self.assertEqual(m["数据截至"], "2026-09-17")
        self.assertIn("周中累计", m["⚠ 注意"])       # ⚠ 不是最终达成，这句话必须跟着文件走
        self.assertIn("C品", m["⚠ 没映射的产品列"])
        self.assertIn("封顶 120%", m["口径·单项达成率"])
        self.assertIn("不是 0", m["空单元格"])
        # ⚠ 口径那些话是写进 **Excel 单元格**的，别带 markdown 的 `**`
        self.assertNotIn("**", json.dumps(m, ensure_ascii=False))
        # ⚠「导出人」由**能力层**补（`export_xlsx(who=…)`）—— 这边再写一遍就是两行
        self.assertNotIn("导出人", m)

    def test_说明表里导出人只有一行(self):
        r = attain_export.export(self.root, self.d, who="杨英梅")
        keys = [row[0] for row in read_sheets(r["path"])["说明"][1:]]
        self.assertEqual(keys.count("导出人"), 1, keys)

    def test_数据到期末尾就不提醒(self):
        m = attain_export.meta_of(dict(self.d, data_until="2026-09-20"))
        self.assertNotIn("⚠ 注意", m)

    def test_端到端_导出到临时根(self):
        r = attain_export.export(self.root, self.d, who="杨英梅")
        self.assertTrue(r["ok"], r["why"])
        book = read_sheets(r["path"])
        self.assertEqual(list(book), ["总览", "谁卖的", "说明"])
        self.assertNotIn(str(ROOT / "out"), r["path"], "测试不许往真 out/ 里写")


class Test门店不能导出(_ServerCase):
    """用户 2026-09-21：「**门店不用导出**」⇒ 门店账号点它应当 **403**。

    ⚠ 这一组**不 mock `_can_for`** —— 装的就是一台门店机器（没有 `config/managers.yaml`
      ⇒ `role_scope()` 退到门店），走的是真判据。
    """

    def test_门店导出_403_而且什么都不写(self):
        st, d = self.export()
        self.assertEqual(st, 403, d)
        self.assertFalse(d["ok"])
        self.assertTrue(d["forbidden"])
        self.assertIn("导出", d["error"])           # ⚠ 前端 `api()` 只认 error / message
        self.assertFalse((self.root / "out" / "exports").exists(), "被拦了却还是写了文件")

    def test_按钮照注册role_ops隐藏_前端不自己判断(self):
        """注册的 export 操作同时驱动前端按钮和后端 require。"""
        self.assertIn("[data-op]", APP_JS)
        self.assertIn("role.ops", APP_JS)
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        button = re.search(r'<button[^>]*id="btn-export-attain"[^>]*>', html, re.S)
        self.assertIsNotNone(button)
        self.assertIn('data-op="export"', button.group(0))
        self.assertIn('data-op-page="attain"', button.group(0))
        # ⚠ **剥掉注释再查**：这条规矩的说明本身就写在 `applyProfile` 那段注释里，
        #   直接 assertNotIn 会被**自己的注释**顶掉（`test_web` 为这个坑专门写了
        #   `_strip_js_comments`）。
        code = re.sub(r"/\*.*?\*/", "", APP_JS, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", "", code)
        self.assertNotIn("role === 'store'", code)


class Test平台能导出(_ServerCase):
    """用户 2026-09-21：「**平台也要能导出**」⇒ 平台岗导的是**全部**门店。"""

    def setUp(self):
        super().setUp()
        prof = {"erp_name": "平台岗", "type": "platform", "platform": True,
                "show_all": True, "who": "赵海培", "needs_linglong": True}
        p = mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof))
        p.start()
        self.addCleanup(p.stop)

    def test_平台导的是全部门店(self):
        st, d = self.export()
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"], d.get("why"))
        stores = [r[0] for r in _overview_rows(read_sheets(d["path"]))]
        self.assertEqual(stores, ["青岛城阳万象汇店", "青岛城阳万达店", "鲁疆广场"])


class Test区长导出(_ServerCase):
    """⚠ 用户点的就是这个身份：「**区长账号有**导出为 excel 功能」。

    ⇒ 他导出来的必须**正好是所辖那几家** —— 不是"本店一家"（门店口径），
      也不是"全区"（平台口径）。这两头都错过（M17 的区长 bug 就是这个形状）。
    """

    def setUp(self):
        super().setUp()
        # 区长名单：账号 → 所辖两家店（判据在 `config_io.managers_table` + `role_scope`）
        (self.root / "config" / "managers.yaml").write_text(
            "managers:\n"
            "  - name: 杨英梅\n"
            '    accounts: ["SL15763940156"]\n'
            "    region: 西北区\n"
            "    stores:\n"
            "      - 青岛城阳万象汇店\n"
            "      - 青岛城阳万达店\n", encoding="utf-8")
        # ⚠ 登录的是哪个云商账号 —— `role_scope()` 按**账号**认区长
        p = mock.patch.object(web, "describe_store_credentials",
                              lambda *a, **k: {"username": "SL15763940156", "who": "杨英梅"})
        p.start()
        self.addCleanup(p.stop)

    def test_导的是所辖那两家(self):
        st, d = self.export()
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"], d.get("why"))
        book = read_sheets(d["path"])
        self.assertEqual([r[0] for r in _overview_rows(book)],
                         ["青岛城阳万象汇店", "青岛城阳万达店"])
        self.assertNotIn("鲁疆广场", json.dumps(book["总览"], ensure_ascii=False))
        # 说明那张表里也要写清"看的是哪些门店"
        meta = dict((k, v) for k, v in book["说明"][1:])
        self.assertIn("区长", str(meta.get("看的是哪些门店") or ""))

    def test_身份是区长(self):
        st, d = self.srv.request("GET", "/api/overview")
        self.assertEqual(st, 200, d)
        self.assertEqual(web.role_scope(self.srv.app)["role"], "manager")

    def test_落盘在安装目录的_out_exports(self):
        _st, d = self.export()
        self.assertEqual(Path(d["path"]).parent, self.root / "out" / "exports")
        self.assertEqual(d["dir"], "out/exports")

    def test_还没算过_不导空表_而且给error文案(self):
        (self.root / "out" / "attain-2026.json").unlink()
        st, d = self.export()
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])
        self.assertTrue(d.get("error"), "前端 api() 只认 error/message，缺了会显示成 HTTP 400")
        self.assertIn("还没算过", d["error"])
        self.assertFalse((self.root / "out" / "exports").exists(), "失败还留了个目录/空文件")

    def test_下载只认一个文件名(self):
        post_status, d = self.export()
        self.assertEqual(post_status, 200, d)
        st, raw = self.srv.request("GET", "/api/export/download?name=" + quote(d["file"]))
        self.assertEqual(st, 200, raw)
        self.assertEqual(raw[:2], b"PK")                        # 就是个 xlsx
        for bad in ("", "nope.xlsx", "../../src/web.py", "..%2F..%2Fsrc%2Fweb.py",
                    d["file"] + "/../" + d["file"]):
            st2, d2 = self.srv.request("GET", "/api/export/download?name=" + quote(bad))
            self.assertEqual(st2, 404, bad)
            self.assertTrue(d2.get("error"), bad)

    def test_下载重验页面导出权限和当前范围(self):
        _status, exported = self.export()
        original_scope = web.role_scope(self.srv.app)
        narrower = dict(original_scope, stores={"鲁疆广场"})
        with mock.patch.object(web, "role_scope", lambda _app: narrower):
            status, body = self.srv.request(
                "GET", "/api/export/download?name=" + quote(exported["file"]))
        self.assertEqual(status, 403, body)

        with mock.patch.dict(web.PERM_RULES, {
                "attain": {"data": "authorized", "ops": {"export": frozenset()}}}):
            status, body = self.srv.request(
                "GET", "/api/export/download?name=" + quote(exported["file"]))
        self.assertEqual(status, 403, body)

    def test_点两次不覆盖(self):
        _s1, d1 = self.export()
        _s2, d2 = self.export()
        self.assertNotEqual(d1["file"], d2["file"])
        self.assertTrue(Path(d1["path"]).is_file(), "第二次导出把第一份盖掉了")


class Test前端接线(unittest.TestCase):
    def test_按钮和结果行都在(self):
        self.assertIn('id="btn-export-attain"', INDEX_HTML)
        self.assertIn('id="attain-export-result"', INDEX_HTML)

    def test_app_js_接上了那个按钮和接口(self):
        self.assertIn("$('#btn-export-attain')", APP_JS)
        self.assertIn("/api/attain/export", APP_JS)
        self.assertIn("/api/export/download", APP_JS)
        # ⚠ 失败的"为什么"要说出来（`api()` 抛的是 `error`，后端补了）
        self.assertIn("导出失败", APP_JS)

    def test_点完直接调浏览器下载(self):
        """用户 2026-09-21：「导出为 excel 我需要**调浏览器下载**，
        **保存到 out 门店和区长找不到**」⇒ 点完必须**自动触发下载**，
        不能让用户去 `out/exports/` 里翻。"""
        self.assertIn("function triggerDownload(", APP_JS)
        self.assertIn("triggerDownload(url, r.file)", APP_JS)
        # ⚠ 用 `a.download` 点一下，**不是** `location.href = …`
        #   （后者会把整个控制台导航成一个 xlsx）
        self.assertIn("a.download", APP_JS)
        self.assertNotIn("location.href = '/api/export/download", APP_JS)
        # 结果那行要告诉人"去哪找"
        self.assertIn("浏览器", APP_JS)

    def test_增值业务两个导出也直接下载(self):
        """防护膜 / 无忧会员权益：以前只给一个「下载」链接，
        用户要的是跟周度达成一样**点完直接调浏览器下载**。"""
        for bid, api_path, box in (
            ("btn-export-film", "/api/film/export", "film-export-result"),
            ("btn-export-benefit", "/api/benefit/export", "benefit-export-result"),
        ):
            head = "$('#%s')" % bid
            self.assertIn(head, APP_JS, bid)
            i = APP_JS.index(head)
            end = APP_JS.find("\n$('#", i + 10)
            if end < 0:
                end = len(APP_JS)
            seg = APP_JS[i:end]
            self.assertIn(api_path, seg, bid)
            self.assertIn("triggerDownload(url, r.file)", seg, bid)
            self.assertIn(box, seg, bid)
            # 不再只甩一个链接让人自己点
            self.assertNotIn('">下载</a>', seg, bid)


class Test增值导出照源表美化(unittest.TestCase):
    """用户 2026-09-23：「两个导出格式参考一下这两个表，对应数据列做好相应的美化」。

    * 防护膜 ←《9月钢化膜数据目标》：表头/共计 = 纯红 `#FF0000` 白字微软雅黑
    * 汇机保 ←《汇机保数据统计》：表头/合计 = 深红 `#C00000`；正文细边框
    * 列格式：率类显示百分比、台数整数、利润带千分位
    """

    def _film_d(self):
        return {
            "start": "2026-09-01", "end": "2026-09-21", "progress": "73%",
            "scope": "全部",
            "rows": [{
                "region": "北区", "store": "青岛城阳万达店",
                "new": 100, "target": 50, "done": 40, "unit_profit": 40,
                "attach": 0.4, "profit_target": 3500, "baseline": 35,
                "film_profit": 1600, "gift_profit": 800, "total_profit": 2400,
                "profit_rate": 0.69, "total_rate": 0.72, "gift_pkg": 20,
                "gift_done": 10, "gift_rate": 0.5, "avg_addon": 24,
            }],
        }

    def _benefit_d(self):
        store = {
            "track": "A", "store": "青岛城阳万达店",
            "day_target": 200, "slot_progress": 140, "new": 50, "new_rate": 0.7,
            "goal": 28, "wuyou": 10, "care": 3, "total": 13, "attach": 0.26,
            "attach_goal_rate": 0.46, "overall": 0.55,
            "tiers": {"opt": 3, "sup": 2, "adv": 2, "max": 1, "p399": 1, "p499": 1},
            "rebate": 1000, "care_profit": 450, "tier_profit": 1200,
            "profit_total": 2650, "avg_profit": 53, "manager_bonus": 265,
        }
        return {
            "start": "2026-09-01", "end": "2026-09-21", "scope": "全部",
            "stores": [store],
            "regions": [{
                "region": "北区", "stores": 1, "new": 50, "goal": 28,
                "wuyou": 10, "care": 3, "total": 13, "attach": 0.26,
                "goal_rate": 0.357, "rebate": 1000, "tier_profit": 1200,
                "profit_total": 2200, "avg_profit": 44,
            }],
            "people": [{
                "rank": 1, "store": "青岛城阳万达店", "title": "销售顾问",
                "name": "张三", "new": 30, "attach_ratio": 0.3,
                "tiers": {"opt": 2, "sup": 1, "adv": 1, "max": 1, "p399": 1, "p499": 0},
                "care": 2, "total": 6, "rebate": 600, "care_profit": 300,
                "tier_profit": 500, "profit_total": 1400, "avg_profit": 83.3,
                "bonus": 140, "bonus_month": 72,
            }],
            "tracks": [],
        }

    def _export(self, sheets_fn, d, name):
        tmp = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        root = Path(tmp)
        (root / "var").mkdir()
        # runlog 需要库；照 `_tmp_root` 的套路建一个空文件即可（record 失败不抛）
        return sheets_fn, root, name, d

    def test_防护膜样式跟钢化膜源表(self):
        import openpyxl
        from src.features.valueadd.film import export as fexp

        _s, root, _n, d = self._export(fexp.export, self._film_d(), "film")
        r = fexp.export(root, d, who="测试")
        self.assertTrue(r["ok"], r["why"])
        ws = openpyxl.load_workbook(r["path"])["数据"]
        head = ws.cell(1, 1)
        self.assertIn(head.fill.fgColor.rgb, ("FFFF0000", "00FF0000"))
        self.assertIn(head.font.color.rgb, ("FFFFFFFF", "00FFFFFF"))
        self.assertEqual(head.font.name, "微软雅黑")
        # 共计 / 合计 整行反白
        totals = [i for i in range(2, ws.max_row + 1)
                  if str(ws.cell(i, 1).value or "").strip() == "合计"
                  or str(ws.cell(i, 1).value or "").startswith("共计")]
        self.assertTrue(totals, "没有合计/共计行")
        cell = ws.cell(totals[-1], 1)
        self.assertIn(cell.fill.fgColor.rgb, ("FFFF0000", "00FF0000"))
        # 正文细边框 + 跟机率显示百分比
        self.assertEqual(ws.cell(2, 1).border.left.style, "thin")
        self.assertIn("%", ws.cell(2, 7).number_format)   # 跟机率 = 第 7 列
        # 条件标色：源表同款 6 条阈值
        ops = self._cf_summary(ws)
        self.assertIn(("跟机率", "lessThan", "0.3"), ops)
        self.assertIn(("毛利达成率", "lessThan", "0.9"), ops)
        self.assertIn(("毛利达成率", "greaterThan", "0.9"), ops)
        self.assertIn(("总达成率", "greaterThan", "1"), ops)
        self.assertIn(("台均增值利润", "lessThan", "20"), ops)
        self.assertIn(("台均增值利润", "greaterThan", "27"), ops)

    def _cf_summary(self, ws):
        """{(表头名, op, 阈值)} —— 按 sqref 列反查表头。"""
        head = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
        from openpyxl.utils import column_index_from_string
        import re
        out = set()
        for cf in ws.conditional_formatting:
            for rule in cf.rules:
                m = re.match(r"([A-Z]+)(\d+)", str(cf.sqref).split()[0])
                col = column_index_from_string(m.group(1))
                out.add((head[col - 1], rule.operator, str(rule.formula[0])))
        return out

    def test_无忧会员样式跟汇机保源表(self):
        import openpyxl
        from src.features.valueadd.benefit import export as bexp

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        r = bexp.export(root, self._benefit_d(), who="测试")
        self.assertTrue(r["ok"], r["why"])
        wb = openpyxl.load_workbook(r["path"])
        ws = wb["门店"]
        head = ws.cell(1, 1)
        self.assertIn(head.fill.fgColor.rgb, ("FFC00000", "00C00000"))
        self.assertEqual(head.font.name, "微软雅黑")
        # 底部「合计」行（门店在第 2 列）反白
        trow = next(i for i in range(2, ws.max_row + 1) if ws.cell(i, 2).value == "合计")
        self.assertIn(ws.cell(trow, 2).fill.fgColor.rgb, ("FFC00000", "00C00000"))
        self.assertEqual(ws.cell(2, 1).border.left.style, "thin")
        # 总达成率（第 13 列）百分比；利润类带千分位
        self.assertIn("%", ws.cell(2, 13).number_format)
        self.assertIn("#,##0", ws.cell(2, 20).number_format)   # 后返
        # 区域 / 人员 也各有合计
        self.assertTrue(any(wb["区域"].cell(i, 1).value == "合计"
                            for i in range(2, wb["区域"].max_row + 1)))
        self.assertTrue(any(wb["人员"].cell(i, 2).value == "合计"
                            for i in range(2, wb["人员"].max_row + 1)))
        # 条件标色：门店 连带/总达成；区域、人员 配比率
        self.assertIn(("连带率", "lessThan", "0.1"), self._cf_summary(ws))
        self.assertIn(("连带达成率", "lessThan", "0.5"), self._cf_summary(ws))
        self.assertIn(("总达成率", "greaterThan", "0.9"), self._cf_summary(ws))
        self.assertIn(("连带率", "greaterThan", "0.15"), self._cf_summary(wb["区域"]))
        self.assertIn(("配比率", "lessThan", "0.1"), self._cf_summary(wb["人员"]))
        self.assertIn(("配比率", "greaterThan", "0.15"), self._cf_summary(wb["人员"]))

    def test_不给样式时还是原来的蓝表头(self):
        """老调用点（差异清单 / 达成 / 盘点）一个字都不能被改到。"""
        import openpyxl
        from src.xlsx_io import write_sheets

        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, ignore_errors=True)
        p = root / "x.xlsx"
        write_sheets(p, {"t": (["门店"], [["A店"]])})
        ws = openpyxl.load_workbook(p).active
        self.assertIn(ws.cell(1, 1).fill.fgColor.rgb, ("FF4472C4", "004472C4"))
        self.assertIsNone(ws.cell(2, 1).border.left.style)
        self.assertFalse(list(ws.conditional_formatting), "没给样式不该有条件格式")


if __name__ == "__main__":
    unittest.main()
