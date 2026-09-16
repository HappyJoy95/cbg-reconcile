"""POS 融合进主项目之后的「接线」测试。

口径本身在 `test_pos_metric.py`（33 条，从门店侧搬过来的）。
这里钉的是**融合引入的三件事**：

1. 控制台的 POS tab **接线对得上**（照 `TestFrontendWiring` 的做法）
2. `/api/pos` **纯读盘，一个网络请求都不发** ← 早定的架构决定
3. 库和落盘都在 `out/`，**自更新和打包都不能碰** ← 碰了就是丢门店数据
"""

import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import selfupdate, web


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class TestPosTabWiring(unittest.TestCase):
    """页签三件套：nav 按钮 / panel id / app.js 的分发行。

    ⚠ 这个项目里「按钮加了、panel id 打错」踩过 —— 页面上什么都不显示，
    而且不报错。所以要有测试盯着。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (_repo_root() / "web" / "index.html").read_text(encoding="utf-8")
        cls.js = (_repo_root() / "web" / "app.js").read_text(encoding="utf-8")

    def test_nav_里有_pos_按钮(self):
        self.assertIn('data-tab="pos"', self.html)

    def test_panel_id_和_data_tab_对得上(self):
        self.assertIn('id="panel-pos"', self.html)

    def test_appjs_里有分发行和两个函数(self):
        self.assertIn("b.dataset.tab === 'pos'", self.js)
        self.assertIn("async function loadPos", self.js)
        self.assertIn("function renderPos", self.js)

    def test_appjs_引用的_pos_id_都在_html_里(self):
        """`$('#pos-xxx')` 打错就是**静默失效**（拿不到元素，不抛错）。"""
        import re
        ids = set(re.findall(r"\$\('#([a-z0-9-]+)'\)", self.js))
        have = set(re.findall(r'id="([a-z0-9-]+)"', self.html))
        missing = sorted(i for i in ids if i.startswith("pos-") and i not in have)
        self.assertEqual(missing, [], "app.js 引用了 html 里没有的 id（页面上会静默失效）")

    def test_不按达标线染色(self):
        """⚠ **达标线还没定** —— 染色就是编一个阈值出来。

        等用户给了线，再在 `pct()` 里加 `.ok/.warn/.bad`，并改这条测试。

        ⚠ 断言只扫 **POS 那一段**：`kpi ok` 在「报告」页的卡片里本来就有，
        扫整个 app.js 会误报（第一版就是这么红的）。
        """
        pos_js = self.js[self.js.index("POS 合规 ─"):self.js.index("总览 ─")]
        self.assertIn("不按达标线染色", pos_js)
        for cls in ("kpi ok", "kpi warn", "kpi bad"):
            self.assertNotIn(cls, pos_js, "POS 卡片不该有达标色 —— 达标线还没定")


class TestPosApiIsReadOnly(unittest.TestCase):
    """⚠ `/api/pos` **不发任何网络请求**。

    依据是 `web.py` 里 `overview()` 上面那条注释：
    「概览页 30 秒刷一次，一个网络请求都不发」。
    POS 分数是 `cli pos` 算好落盘的，看板只读它。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir()
        self.app = web.App(self.root, "config/store-X.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def test_没有文件时给提示_不是报错(self):
        d = self.app.pos()
        self.assertFalse(d["exists"])
        self.assertEqual(d["rows"], [])
        self.assertTrue(d.get("hint"), "没有数据时要给一句人话，不能只是空")

    def test_有文件时读出来(self):
        (self.root / "out" / "pos-2026.json").write_text(
            json.dumps({"year": 2026, "rows": [{"month": "2026-08"}]}), encoding="utf-8")
        d = self.app.pos()
        self.assertTrue(d["exists"])
        self.assertEqual(d["file"], "pos-2026.json")
        self.assertEqual(d["rows"][0]["month"], "2026-08")

    def test_坏_json_不炸(self):
        """落盘文件被写坏时，看板该说"读不了"，不该 500。"""
        (self.root / "out" / "pos-2026.json").write_text("{坏掉的", encoding="utf-8")
        d = self.app.pos()
        self.assertFalse(d["exists"])
        self.assertIn("pos-2026.json", d.get("error", ""))

    def test_一个网络请求都不发(self):
        (self.root / "out" / "pos-2026.json").write_text(
            json.dumps({"year": 2026, "rows": []}), encoding="utf-8")

        def boom(*a, **kw):
            raise AssertionError("pos() 不该碰网络！")

        with mock.patch.object(socket.socket, "connect", boom), \
             mock.patch.object(socket, "create_connection", boom):
            d = self.app.pos()
        self.assertTrue(d["exists"])


class TestPosDataIsProtected(unittest.TestCase):
    """⚠ 库（`out/cbg-<年>.db`）和 POS 落盘（`out/pos-<年>.json`）都是**门店数据**。

    自更新**不能碰**、打包**不能发**。碰了就是把门店几个月的抓取结果洗掉。
    """

    def test_out_在_NEVER_TOUCH_里(self):
        self.assertIn("out", selfupdate.NEVER_TOUCH,
                      "out/ 不在自更新黑名单里 —— 库会被更新覆盖！")

    def test_打包脚本排除_out(self):
        # ⚠ `tools/` **不进包** —— 而 `tests/` 进包。所以这条测试在解压出来的包里
        #   跑时会找不到脚本。**跳过，不是失败**：它要守的是"仓库里那份脚本"，
        #   在包里根本没有那份脚本可守。
        script = _repo_root() / "tools" / "build_package.sh"
        if not script.is_file():
            self.skipTest("不在仓库里（包内不含 tools/）")
        text = script.read_text(encoding="utf-8")
        self.assertIn("--exclude 'out/'", text, "打包脚本没排除 out/ —— 门店数据会进包")

    def test_自更新不会铺_out_下的东西(self):
        """⭐ 真跑一遍 `_targets()`：`out/cbg-2026.db` 和 `out/pos-2026.json` 都不该被铺。

        ⚠ `_targets()` 收的是**解压后的目录**，不是 zip 文件
        （第一版我传了 zip，结果拿到空集 "通过" 了 —— 假绿）。
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in ("src/cli.py", "out/cbg-2026.db", "out/pos-2026.json",
                        ".secrets/erp.env"):
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("x", encoding="utf-8")
            got = {str(rel) for _, rel in selfupdate._targets(root)}
            self.assertIn("src/cli.py", got, "正常代码该被铺 —— 不然这条测试是假绿")
            self.assertNotIn("out/cbg-2026.db", got, "自更新会覆盖订单库！")
            self.assertNotIn("out/pos-2026.json", got, "自更新会覆盖 POS 落盘！")
            self.assertNotIn(".secrets/erp.env", got, "自更新会覆盖凭据！")


class TestMonthRange(unittest.TestCase):
    """`dump --month` 的窗口 —— 日常流程用的就是它。"""

    def test_过去的月拉整月(self):
        from src import dump
        a, b = dump.month_range("2026-07")
        self.assertEqual(dump.ts2str(a), "2026-07-01 00:00:00")
        self.assertEqual(dump.ts2str(b), "2026-07-31 23:59:59")

    def test_当月拉到此刻而不是月末(self):
        """⚠ 拉未来的日期没意义；而且**当月要拉到"现在"**，
        否则 21:00 之后发生的单会被漏掉（这正是"拉整月"的理由）。"""
        from src import dump
        a, b = dump.month_range(dump.this_month())
        self.assertEqual(dump.ts2str(a)[-8:], "00:00:00")
        self.assertLess(b, 4 * 10 ** 12)

    def test_十二月不会算崩(self):
        from src import dump
        a, b = dump.month_range("2026-12")
        self.assertEqual(dump.ts2str(b), "2026-12-31 23:59:59")


if __name__ == "__main__":
    unittest.main()
