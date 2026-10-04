"""库存盘点（M16）—— 模块接入 / 取数 / 落盘推送 / 盘点页接线。

用户 2026-09-20：「把库存盘点功能整理成一个模块接入我们这个项目？
**导出 excel 这一步接给推送**。**这个就不注册定时器了**」

这一份盯的是**接入**那几处（"不注册定时器"是其中最容易被顺手做错的一条）：
注册表里有没有它 · 有没有偷偷多出一个定时步骤 · 页面里的 id 对不对得上 ·
凭据有没有又跑回浏览器 · 导出推送那条链有没有把 xlsx 真的带上。

⚠ 盘点**口径**（uid / 在途拆分 / 匹配引擎）在 `web/inventory/core.js` 里，
   那是从上游 Inventory Check 原样搬来的、由它自己那 521 条 node 断言盯着 ——
   见 `Test原样搬来的三个文件`。
"""

from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.features import registry                                # noqa: E402
from src.features.inventory import book as inv_book              # noqa: E402
from src.features.inventory import push as inv_push              # noqa: E402
from src.modules import fetch                                    # noqa: E402
from src.xlsx_io import write_sheets                             # noqa: E402
from src import web                                              # noqa: E402

WEB = ROOT / "web"
PAGE = (WEB / "inventory.html").read_text(encoding="utf-8")
UI_JS = (WEB / "inventory" / "ui.js").read_text(encoding="utf-8")
API_JS = (WEB / "inventory" / "api.js").read_text(encoding="utf-8")


def _strip_js_comments(s: str) -> str:
    """剥掉 JS 注释 —— **反向断言必须先剥**。

    ⚠ 这个项目的注释是"记录踩过的坑"风格：删掉一段代码时会把
      "以前这儿是什么、为什么删"写进注释，而注释里就带着那个刚删掉的名字。
      直接 `assertNotIn("IC.erp", ui) `会被**自己的注释**顶掉 ——
      实测就踩了（`saveCfg 里不该出现 token` 被我自己那句"不存 token"顶红）。
      （`tests/test_web.py::_strip_js_comments` 是同一个道理，那边还带着踩坑记录。）
    """
    import re
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?m)//.*$", "", s)


class Test注册表里有它但没定时步骤(unittest.TestCase):
    """⚠ **「这个就不注册定时器了」是用户明说的** ——
    多一个 `step=` 的表现是：盘点多出一条"每天 21:00 自动跑"，
    而它根本没法自动跑（要人拿扫码枪）—— 门店会看到一条永远失败的记录。
    """

    def test_一级功能在(self):
        keys = [f.key for f in registry.all_features()]
        self.assertIn("inventory", keys)

    def test_它有一个子模块且没有_step(self):
        f = [x for x in registry.all_features() if x.key == "inventory"][0]
        self.assertEqual([s.key for s in f.children],
                         ["inventory", "inventory-settings"])
        self.assertIsNone(f.children[0].step, "库存盘点不该有定时步骤")
        self.assertEqual(f.steps(), [])

    def test_步骤表里没有它(self):
        self.assertNotIn("inventory", registry.steps())
        self.assertIsNone(registry.step_by_cmd("inventory"))
        self.assertNotIn("inventory", [s.cmd for s in registry.wakes()])

    def test_菜单里有它(self):
        menus = {m["key"]: m for m in registry.menus()}
        self.assertIn("inventory", menus)
        self.assertEqual(menus["inventory"]["label"], "库存盘点")
        self.assertEqual([c["key"] for c in menus["inventory"]["children"]],
                         ["inventory", "inventory-settings"])
        self.assertFalse(menus["inventory"]["children"][0]["is_step"])

    def test_注册表自检干净(self):
        self.assertEqual(registry.validate(), [])


class Test数据源登记表(unittest.TestCase):
    """用户：「页面找**数据抓取模块**抓取最新的库存」——
    所以这路数据得在 `fetch.SOURCES` 里，别人才查得到"盘点账面从哪来"。"""

    def test_登记了这一路(self):
        one = fetch.get("inventory-book")
        self.assertIsNotNone(one)
        self.assertEqual(one["grab"], "erp.py")
        self.assertEqual(one["state_key"], "", "现抓现用，不该进'新不新鲜'的判定")

    def test_不算每天那趟(self):
        """⚠ 盘点不注册定时器 ⇒ 它也不该出现在"每天该抓哪几路"里。"""
        self.assertNotIn("inventory-book", fetch.daily_keys())

    def test_登记表打得出来(self):
        row = [r for r in fetch.table(ROOT) if r["key"] == "inventory-book"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["state_label"], "-", "没进判定的那几路状态写 -，不写 ok")


class Test本店仓匹配(unittest.TestCase):
    """默认帮门店选好仓 —— 少一次手滑选到隔壁店的机会。

    ⚠ 判据是"**只在能唯一确定时才给**"：同名多仓、只能模糊匹配的一律 `None`。
      猜错的表现是"账面看着正常、其实盘的是别人家的货"，最难发现。
    """

    WHS = [
        {"Id": 1, "Name": "青岛正阳路利客来店库", "BranchName": "青岛正阳路利客来店"},
        {"Id": 2, "Name": "青岛万达店库", "BranchName": "青岛万达店"},
        {"Id": 3, "Name": "青岛万达二店库", "BranchName": "青岛万达店"},
        {"Id": 4, "Name": "上街里容滙城店库", "BranchName": "上街里容滙城店"},
    ]

    def test_精确同名给那一个(self):
        got = inv_book.store_for(self.WHS, "青岛正阳路利客来店")
        self.assertEqual(got["Id"], 1)

    def test_同名多个不给猜(self):
        self.assertIsNone(inv_book.store_for(self.WHS, "青岛万达店"))

    def test_名字带库也认(self):
        whs = [{"Id": 9, "Name": "某某店库", "BranchName": ""}]
        self.assertEqual(inv_book.store_for(whs, "某某店")["Id"], 9)

    def test_匹配不上就是_None(self):
        self.assertIsNone(inv_book.store_for(self.WHS, "不存在的店"))
        self.assertIsNone(inv_book.store_for(self.WHS, ""))
        self.assertIsNone(inv_book.store_for([], "青岛万达店"))

    def test_不做模糊包含(self):
        """「万达」能匹到两家 —— 模糊匹配在这里只会制造选错的机会。"""
        self.assertIsNone(inv_book.store_for(self.WHS, "万达"))


class Test导出落盘与推送文案(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inv-test-"))

    def _xlsx(self, rows=None):
        """造一份跟盘点页导出同形的 xlsx（第一张表叫「汇总」，两列：项目/数值）。"""
        summary = rows or [
            ["项目", "数值"],
            ["盘点仓库", "青岛万达店库"],
            ["快照日期", "2026-09-20"],
            ["应盘（有串号）", 120],
            ["已盘到", 118],
            ["其中扫码盘到", 115],
            ["其中手工确认", 3],
            ["未扫到", 2],
            ["盘点完成率", "98.3%"],
            ["表外码（去重）", 1],
            ["在途（待入库）台数", 4],
            ["其中已扫到（货已到）", 1],
            ["无串号商品行数", 35],
            ["无串号已手工盘过行数", "3 / 35"],
            ["无串号差异（仅已盘部分）", -1],
        ]
        p = self.tmp / "库存盘点_青岛万达店库_2026-09-20.xlsx"
        write_sheets(p, {"汇总": (None, summary),
                         "未扫到": (["商品名称"], [["手机甲"]])})
        return p

    def test_文件名净化(self):
        self.assertEqual(inv_push.safe_name('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(inv_push.safe_name("  "), "库存盘点")
        self.assertEqual(inv_push.safe_name("x" * 300), "x" * 120)

    def test_落盘到_out_inventory(self):
        p = inv_push.save_export(self.tmp, "库存盘点_甲店_2026-09-20", b"PK\x03\x04hello")
        self.assertEqual(p.parent, self.tmp / "out" / "inventory")
        self.assertEqual(p.name, "库存盘点_甲店_2026-09-20.xlsx")
        self.assertEqual(p.read_bytes(), b"PK\x03\x04hello")

    def test_不是_xlsx_就拒收(self):
        """⚠ 不校验的话，前端哪天传了半截，我们会把一个坏附件推给门店 —— 而没人会当场说。"""
        for bad in (b"", b'{"a":1}', b"<html>"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    inv_push.save_export(self.tmp, "x", bad)

    def test_文案从导出的汇总表里读(self):
        """⚠ 口径**只有一份**：推出去的和门店看到的是同一份 xlsx 里的数。"""
        head, lines = inv_push.build_message("青岛万达店库", "2026-09-20",
                                             inv_push.read_summary(self._xlsx()))
        self.assertIn("青岛万达店库", head)
        self.assertIn("应盘 120 台", head)
        self.assertIn("98.3%", head)
        body = "\n".join(lines)
        self.assertIn("未扫到 2 台", body)
        self.assertIn("表外码 1 个", body)
        self.assertIn("在途待入库 4 台", body)
        self.assertIn("已扫到（货已到）1 台", body)

    def test_缺的标签不编数(self):
        """汇总表里没有的项**不写 0** —— "没有这个数"和"这个数是 0"不一样。"""
        head, lines = inv_push.build_message("甲店", "2026-09-20",
                                             {"盘点仓库": "甲店", "快照日期": "2026-09-20"})
        self.assertIn("甲店", head)
        self.assertNotIn("应盘", head)
        self.assertEqual(len(lines), 1)
        self.assertIn("没读到汇总行", lines[0])

    def test_推送两个渠道都发_邮件带附件(self):
        p = self._xlsx()
        sent = []

        def fake_send(code, content, **kw):
            sent.append((code, content, kw))
            return {"code": code, "state": "sent", "ok": True, "why": "已发送"}

        with mock.patch.object(inv_push, "read_summary", inv_push.read_summary), \
             mock.patch.object(inv_push, "_channel_on", return_value=True), \
             mock.patch("src.modules.notify.send", fake_send), \
             mock.patch("src.storage.runlog.record") as rec:
            res = inv_push.push(p, cfg={}, root=self.tmp, store="青岛万达店库",
                                date="2026-09-20")
        codes = [c for c, _content, _kw in sent]
        self.assertEqual(codes, ["mail", "wecom"])
        mail = [c for code, c, _kw in sent if code == "mail"][0]
        self.assertEqual(mail["attachments"], (p,), "邮件必须带上那份 xlsx")
        wecom = [c for code, c, _kw in sent if code == "wecom"][0]
        self.assertEqual(wecom["template"], "stock", "企微要显式说清用哪套模板")
        self.assertTrue(res["mail"]["ok"] and res["wecom"]["ok"])
        # 运行日志里记一笔（kind=inventory）—— 谁的机器什么时候推过盘点清单
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args[0][0], "inventory")

    def test_邮件没发出去时_企微那条要说实话(self):
        """⚠ 尾部写"见邮件附件"而邮件其实没发出去 = 骗人，而门店会照着去找。"""
        p = self._xlsx()
        seen = {}

        def fake_send(code, content, **kw):
            seen[code] = content
            if code == "mail":
                return {"code": code, "state": "failed", "ok": False, "why": "没配邮箱"}
            return {"code": code, "state": "sent", "ok": True, "why": "已发"}

        with mock.patch.object(inv_push, "_channel_on", return_value=True), \
             mock.patch("src.modules.notify.send", fake_send), \
             mock.patch("src.storage.runlog.record"):
            res = inv_push.push(p, cfg={}, root=self.tmp, store="甲店")
        self.assertFalse(res["mail"]["ok"])
        self.assertIn("邮件没发出去", seen["wecom"]["ctx"]["附件说明"])

    def test_关掉的渠道不发(self):
        """⚠ `mailer.send` / `wecom.send_markdown` **都不看 `enabled`**（谁调谁负责）——
        漏了这一步的表现是"门店在设置里关掉了，照样发出去"。

        ⚠ 这条也是**实测的安全阀**：拿一份两个渠道都关着的配置跑真控制台，
           点「导出并推送」不会真的发东西出去。
        """
        sent = []
        with mock.patch.object(inv_push, "_channel_on", return_value=False), \
             mock.patch("src.modules.notify.send",
                        side_effect=lambda *a, **k: sent.append(a) or {}), \
             mock.patch("src.storage.runlog.record"):
            res = inv_push.push(self._xlsx(), cfg={}, root=self.tmp, store="甲店")
        self.assertEqual(sent, [], "关着的渠道一个请求都不该发")
        self.assertEqual(res["mail"]["state"], "skipped")
        self.assertEqual(res["wecom"]["state"], "skipped")
        self.assertIn("没启用", res["mail"]["why"])

    def test_读不出开关就当没开(self):
        """⚠ 宁可少发一条（界面会说明白），也别因为读不出开关就往群里发东西。"""
        with mock.patch("src.mailer.load_mail_config", side_effect=RuntimeError("坏了")):
            self.assertFalse(inv_push._channel_on({}, self.tmp, "mail"))


class Test页面接线(unittest.TestCase):
    """前端没有 lint、也没有构建步骤 —— 这几条就是它的"编译检查"。"""

    def test_盘点页自己加载了主题且首屏读_cbg_theme(self):
        """2026-09-22 用户「库存盘点页面颜色不对，没完整接入」。

        - **控制台路径（已拆 iframe）**：markup 在 index.html，直接吃父页主题。
        - **单独打开 inventory.html**：CSS 变量穿不进来 ⇒ 仍必须自己
          link `theme.css` + `themes/*.css`，首屏读 `localStorage['cbg-theme']`。
        """
        # ① 单独打开：主题在组件样式前
        i = PAGE.index('href="/theme.css')
        j = PAGE.index('href="/themes/dark.css')
        k = PAGE.index('href="/inventory/style.css')
        self.assertLess(i, j)
        self.assertLess(j, k, "主题必须排在 inventory/style.css 前面")
        # 每个 themes/*.css 都链了（跟 index.html 同一套清单）
        import re
        linked = set(re.findall(r'href="/themes/([a-z]+)\.css', PAGE))
        themes = {p.stem for p in (ROOT / "web" / "themes").glob("*.css")}
        self.assertEqual(linked, themes, "盘点页漏链主题文件")
        # ② 首屏脚本
        self.assertIn("cbg-theme", PAGE)
        self.assertIn("setAttribute('data-theme'", PAGE)
        # ③ 同文档：index 里有 inv-root + inventory 样式；控制台主题直接生效
        idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="inv-root"', idx, "拆 iframe 后 markup 要在控制台里")
        self.assertIn("/inventory/style.css", idx, "盘点样式要挂进 index")
        # 单独打开仍认 postMessage（兼容层）；控制台 setTheme 不必再点名盘点
        self.assertIn("ic !== 'theme'", UI_JS, "ui.js 要认 parent 的 theme 消息")

    def test_盘点样式不许再写死整页底色(self):
        """`--bg: #f2f4f8` 写回 :root 会盖掉主题（同特异性、后加载赢）。"""
        import re
        css = (ROOT / "web" / "inventory" / "style.css").read_text(encoding="utf-8")
        # 剥注释后看 :root 第一段有没有整页色
        m = re.search(r":root\s*,\s*body\s*\{([^}]+)\}", css)
        self.assertIsNotNone(m, "找不到 :root,body 令牌块")
        blk = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        for bad in ("--bg:", "--card:", "--line:", "--text:", "--muted:", "--brand:"):
            self.assertNotIn(bad, blk, "%s 写死在盘点 :root 里会盖掉主题" % bad)
        self.assertIn("--danger: var(--bad)", blk, "danger 要跟主题的 --bad 走")

    def test_引用的_id_都在页面上(self):
        """`$('#x')` 里的 x 必须在 inventory.html 里有 `id="x"`。

        ⚠ 上游那边是"任何一个 id 缺失都会在 `main()` 里抛 TypeError、整页停止初始化"
          （`bind()` 里全是 `$('#xxx').onclick = …`，没有 null 检查）。
        """
        import re
        used = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", UI_JS))
        defined = set(re.findall(r'id="([A-Za-z0-9_-]+)"', PAGE))
        # `#search` 是表格渲染时**拼出来**的（在 tab-body 里），不在静态骨架里
        # `#toast` 是 inv-toast 的**回退**（拆 iframe 后主 id 是 inv-toast）
        self.assertEqual(sorted(used - defined - {"search", "toast"}), [])
        # 同文档路径：index 里也要有关键 id（脚本注入后跑在同一份 DOM 上）
        idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        idx_ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', idx))
        core_ids = {"scan-input", "scan-card", "setup", "inv-toast", "foot-ver", "loading"}
        self.assertEqual(sorted(core_ids - idx_ids), [], "index 缺盘点关键 id")

    def test_那个折叠的接口设置块没了_但三个勾选还在(self):
        """⚠ 2026-09-21（用户：「**接口设置这个页面就不要了**」）——

        那个折叠块（「云商账号 / 接口设置」）里原来有：账号只读信息（`#cfg-user` /
        `#cfg-company`）+ 一段说明 + 三个盘点偏好。账号早在控制台配了 ⇒ 摊平成两行。

        ⚠ 三个勾选**一个都不能少**（提示音 / 全库索引 / 在途）—— 它们是真设置，
          删掉的话界面上就再也改不了（而且 `ui.js` 里 `$('#opt-…')` 会拿到 null，
          上游那套 `bind()` 是**不判空**的，整页会停在 TypeError 上）。
        """
        self.assertNotIn('<details class="adv">', PAGE)
        self.assertNotIn('id="cfg-user"', PAGE)
        self.assertNotIn('id="cfg-company"', PAGE)
        # 「重新读取」和那句账号状态留着（账号没配时要靠它说清去哪儿配）
        for keep in ('id="btn-relogin"', 'id="login-status"', 'id="opt-sound"',
                     'id="opt-index"', 'id="opt-transit"', 'id="btn-gi"'):
            with self.subTest(keep=keep):
                self.assertIn(keep, PAGE, "摊平的时候把 %s 弄丢了" % keep)

    def test_脚本顺序是_core_api_store_xlsx_ui(self):
        """⚠ 五个文件在**加载时**就解引用 `IC.xxx`（`const core = IC.core`）——
        顺序错了直接 TypeError，整页白板。上游的 build.mjs 也靠这个顺序内联。"""
        import re
        # ⚠ 正则要收下 `?v=`（2026-09-21 给盘点页补防缓存版本号时改的）——
        #   不收的话匹配结果变成**空列表**，而那条 assertEqual 就**永远绿**，
        #   等于这条测试废了（AGENTS 坑：别让循环/断言跑在空列表上）。
        got = re.findall(r'<script src="/inventory/([a-z]+)\.js(?:\?v=\d+)?"></script>', PAGE)
        self.assertEqual(got, ["core", "api", "store", "xlsx", "ui"])

    def test_页面上没有任何凭据(self):
        """⚠ 这一页**不存 token / 密码 / 账号** —— 那些在控制台的 `.secrets/erp.env` 里。

        上游那一版是"页面自己登录、token 存 localStorage"，接进控制台后那条路删了。
        """
        for name, text in (("ui.js", UI_JS), ("api.js", API_JS), ("inventory.html", PAGE)):
            with self.subTest(file=name):
                low = text.lower()
                self.assertNotIn("erp_password", low)
                self.assertNotIn("cfg-password", low, "账号密码框该没了")
                self.assertNotIn("cfg-token", low, "token 框该没了")
                self.assertNotIn("apireport.yserp.cc", low, "页面不该再直连云商")

    def test_saveCfg_只写界面偏好(self):
        """⚠ 本地存储的写入面要窄：写成"整个 cfg"就会把身份（和以后的凭据）带进去。"""
        code = _strip_js_comments(UI_JS)
        i = code.index("function saveCfg()")
        blk = code[i:code.index("\n  }", i)]
        for bad in ("token", "password", "account", "companycode"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, blk, "saveCfg 里不该出现 %s" % bad)
        for good in ("sound", "loadGlobalIndex", "loadInTransit"):
            with self.subTest(good=good):
                self.assertIn(good, blk)

    def test_导出并推送那个按钮接了(self):
        self.assertIn('id="btn-push"', PAGE)
        self.assertIn("$('#btn-push').onclick", UI_JS)
        self.assertIn("async function exportAndPush(", UI_JS)
        # 跟"导出到本机"用**同一份**字节（sheetsForExport）—— 别各拼一份表
        i = UI_JS.index("async function exportAndPush(")
        self.assertIn("xlsx.build(sheetsForExport())", UI_JS[i:i + 500])

    def test_后端调用只在_api_js_里(self):
        """ui.js 里不该再出现 `erp.` 那套调用（名字改了，调用点得跟着改）。"""
        import re
        code = _strip_js_comments(UI_JS)
        self.assertIsNone(re.search(r"\berp\.(fetch|is|login|incomplete|assert)", code))
        self.assertNotIn("IC.erp", code)
        self.assertIn("const api = IC.api;", code)


class Test原样搬来的三个文件(unittest.TestCase):
    """`core.js` / `store.js` / `xlsx.js` 是从 `Inventory Check/src/` **逐字节搬来**的。

    它们装着盘点**全部口径**（uid 生成、在途拆分、匹配引擎、xlsx 生成），
    上游那 521 条 node 断言验的就是这份代码 —— 所以在这里改它们，
    等于**绕过了唯一一套验证**。

    ⇒ 要改：**回上游改**（`cd "~/vibe-coding/Inventory Check" && npm test`），
      跑完再拷回来，然后把下面的哈希更新掉（并在当天日志里写清为什么）。

    哈希是一把"提醒锁"，不是加密：它的作用是在有人**顺手**改这里时当场红给他看。
    """

    WANT = {
        "core.js": "30739d275940094cb3ab30504a87edcd1517d86e37aaac50c424c12004b6bda9",
        "store.js": "280375f9c661a5369d65ec2ebb27fa8dca37a058fc754ea706c9eaf5ece221b8",
        "xlsx.js": "f877e3e1defda2b46674f1d2b270b8392359b74d10492b0027dc1c4f681996e5",
    }

    def test_逐字节没动过(self):
        for name, want in self.WANT.items():
            with self.subTest(file=name):
                got = hashlib.sha256((WEB / "inventory" / name).read_bytes()).hexdigest()
                self.assertEqual(
                    got, want,
                    "%s 跟搬来的时候不一样了 —— 要改它请回上游改 + 跑 npm test，"
                    "改完再更新这里的哈希（见本类注释）" % name)


class Test接口(unittest.TestCase):
    """`App.inventory_*` —— 盘点页唯一的那条通道。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inv-app-"))
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "store-X.yaml").write_text(
            "erp_store_name: 青岛万达店\nstore_code: SCN1\n", encoding="utf-8")
        self.app = web.App(self.tmp, "config/store-X.yaml")

    def test_ready_不发网络请求(self):
        """页面一进来就调它 —— 卡在这儿整页白屏，所以它只能读本机配置。

        ⚠ 这里**故意不 mock 网络**：真发了请求的话这条测试会真的去连云商。
        """
        with mock.patch.object(inv_book, "client", side_effect=AssertionError("不该建客户端")):
            got = self.app.inventory_ready()
        self.assertIn("ok", got)
        self.assertEqual(got["store"]["erp_name"], "青岛万达店")

    def test_warehouses_带上本店默认仓(self):
        fake = {"ok": True, "warehouses": [
            {"Id": 7, "Name": "青岛万达店库", "BranchName": "青岛万达店"}], "why": ""}
        with mock.patch.object(inv_book, "warehouses", return_value=fake):
            got = self.app.inventory_warehouses()
        self.assertEqual(got["default_store_id"], "7")
        self.assertEqual(got["erp_store_name"], "青岛万达店")

    def test_book_原样透传行(self):
        rows = [{"Imei": "A1", "ProCount": 1, "ProCount_OnTransfer": 0}]
        with mock.patch.object(inv_book, "book",
                               return_value={"ok": True, "rows": rows, "total": 1}):
            got = self.app.inventory_book("2026-09-20", "7")
        self.assertEqual(got["rows"], rows)

    def test_export_落盘加推送_推送失败也算导出成功(self):
        """⚠ 文件已经落盘了 ⇒ `ok` 仍为真，两个渠道各带 state/why 让界面说实话。"""
        p = self.tmp / "out" / "inventory" / "x.xlsx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK\x03\x04")
        with mock.patch.object(inv_push, "save_export", return_value=p), \
             mock.patch.object(inv_push, "push", return_value={
                 "head": "h", "lines": ["l"], "path": str(p),
                 "mail": {"ok": False, "state": "failed", "why": "没配邮箱"},
                 "wecom": {"ok": True, "state": "sent", "why": "已发"}}):
            got = self.app.inventory_export("x", b"PK\x03\x04", store="甲店")
        self.assertTrue(got["ok"])
        self.assertFalse(got["mail"]["ok"])
        self.assertTrue(got["wecom"]["ok"])
        self.assertEqual(got["name"], "x.xlsx")

    def test_export_不是_xlsx_给_400_级的错误(self):
        got = self.app.inventory_export("x", b"not a zip", store="甲店")
        self.assertFalse(got["ok"])
        self.assertIn("xlsx", got["error"])

    def test_接口路由都在(self):
        """业务模块明确列全路由，公共入口只按路径委托。"""
        src = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
        from src.features.inventory import http as inventory_http
        routes = {path for _method, path in inventory_http.ROUTES}
        for path in ("/api/inventory/ready", "/api/inventory/warehouses",
                     "/api/inventory/book", "/api/inventory/transit",
                     "/api/inventory/index", "/api/inventory/export"):
            with self.subTest(path=path):
                self.assertIn(path, routes)
        self.assertIn('path.startswith("/api/inventory/")', src)


if __name__ == "__main__":
    unittest.main()


class Test取数失败要留痕(unittest.TestCase):
    """`App.inventory_*` 取数失败时记一笔 `run_record`（kind=inventory-fetch）。

    ⚠ **只记失败**：盘点页每开一次就拉一遍账面，成功也记的话运行记录被刷满，
      真出事时反而看不见。健康面板"连着失败三次"那条靠的就是这个。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inv-rec-"))
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "store-X.yaml").write_text("erp_store_name: 甲店\n",
                                                          encoding="utf-8")
        self.app = web.App(self.tmp, "config/store-X.yaml")

    def test_成功不记(self):
        with mock.patch.object(inv_book, "book",
                               return_value={"ok": True, "rows": [], "total": 0}), \
             mock.patch("src.storage.runlog.record") as rec:
            self.app.inventory_book("2026-09-20", "7")
        self.assertFalse(rec.called, "成功也记的话记录会被刷满")

    def test_失败记一笔_带原因(self):
        with mock.patch.object(inv_book, "book",
                               return_value={"ok": False, "rows": [], "total": 0,
                                             "why": "ErpIncomplete: 只拿到 1/3 行"}), \
             mock.patch("src.storage.runlog.record") as rec:
            self.app.inventory_book("2026-09-20", "7")
        self.assertTrue(rec.called)
        self.assertEqual(rec.call_args[0][0], "inventory-fetch")
        self.assertIn("1/3", rec.call_args[1]["why"])
        self.assertEqual(rec.call_args[1]["root"], self.tmp, "必须写临时 root，别污染真库")


class Test账号名取合并链那份(unittest.TestCase):
    """⚠ 2026-09-20 实测踩到：`describe_credentials()` **只看指定文件**，
    而实际用的是**回落链**（环境变量 > `.secrets/erp.env` > `~/.dsh/...` > 内置）。
    用错那份，页面上会显示成「（后端没给用户名）」—— 而账号其实配着，
    门店照着这句会去重配一遍账号，白折腾。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="inv-creds-"))
        (self.tmp / "config").mkdir(parents=True, exist_ok=True)
        (self.tmp / "config" / "store-X.yaml").write_text("erp_store_name: 甲店\n",
                                                          encoding="utf-8")
        self.app = web.App(self.tmp, "config/store-X.yaml")

    def test_用合并链里的账号(self):
        with mock.patch("src.web.erp.describe_credentials",
                        return_value={"username": "", "company": "", "builtin": False,
                                      "has_token": False, "used_from": "/别处/erp.env"}), \
             mock.patch("src.web.erp.load_credentials",
                        return_value={"username": "sl18917405716", "company": "00001937",
                                      "password": "x", "token": ""}):
            got = self.app.inventory_ready()
        self.assertTrue(got["ok"])
        self.assertEqual(got["username"], "sl18917405716")
        self.assertEqual(got["company"], "00001937")
        self.assertEqual(got["used_from"], "/别处/erp.env")

    def test_没有账号就说清去哪儿配(self):
        with mock.patch("src.web.erp.describe_credentials",
                        return_value={"username": "", "company": "", "builtin": False,
                                      "has_token": False, "used_from": ""}), \
             mock.patch("src.web.erp.load_credentials",
                        return_value={"username": "", "password": "", "token": ""}):
            got = self.app.inventory_ready()
        self.assertFalse(got["ok"])
        self.assertIn("通用设置", got["why"])


class Test企微那条也要带文件(unittest.TestCase):
    """用户 2026-09-21：「推送没问题，**但是没有文件**ok嘛」。

    ⚠ 原来这条模板只发摘要（我按最早的答复定的），而盘点清单门店是要**拿去对货**的 ——
      只给数字不给清单，等于让他们自己去控制台翻。
      现在跟双平台对比一个规矩：**摘要 + 清单文件**，文件受 `wc.send_file` 控制。

    ⚠ 顺序也是判据：**先传文件、再发摘要** —— 这样尾部那句"群里也发了一份"
      是按事实写的（反过来的话，正文说完"在下面那个文件里"、文件却传失败）。
    """

    def setUp(self):
        from src import wecom
        self.wc = wecom.WecomConfig(enabled=True, webhook="https://qyapi.weixin.qq.com/"
                                                          "cgi-bin/webhook/send?key=abc-123",
                                    send_file=True)
        self.wecom = wecom

    def _run(self, *, send_file=True, upload_raises=None):
        calls = []
        self.wc.send_file = send_file

        def fake_upload(_wc, path):
            calls.append(("upload", str(path)))
            if upload_raises:
                raise self.wecom.WecomError(upload_raises)
            return "MID-1"

        def fake_markdown(_wc, content):
            calls.append(("markdown", content))

        def fake_send_media(_wc, media_id):
            calls.append(("file", media_id))

        with mock.patch.object(self.wecom, "upload_file", fake_upload), \
             mock.patch.object(self.wecom, "send_markdown", fake_markdown), \
             mock.patch.object(self.wecom, "send_media_id", fake_send_media):
            why = self.wecom.push_stock(self.wc, {"门店": "甲店", "生成时间": "t",
                                                  "附件说明": "完整清单在邮件附件里"},
                                        ["未扫到 1 台"], "标题", xlsx="/tmp/a.xlsx")
        return calls, why

    def test_先传文件再发摘要_并真的发了文件(self):
        calls, why = self._run()
        self.assertEqual([c[0] for c in calls], ["upload", "markdown", "file"])
        md = [c[1] for c in calls if c[0] == "markdown"][0]
        self.assertIn("群里也发了一份", md, "正文要按事实说清文件在群里")
        self.assertIn("完整清单在邮件附件里", md, "邮件那句还在")
        self.assertIn("已发清单文件", why)

    def test_关掉_send_file_就不发文件(self):
        calls, why = self._run(send_file=False)
        self.assertEqual([c[0] for c in calls], ["markdown"], "关掉了就别传")
        self.assertNotIn("群里也发了一份", [c[1] for c in calls if c[0] == "markdown"][0])

    def test_文件传失败_摘要照发且说实话(self):
        """⚠ 摘要本身有用（未扫到几台、表外几个）—— 不能因为文件失败就一起不发。"""
        calls, why = self._run(upload_raises="企微返回 40001")
        # 传文件的动作**试过了**（`upload` 记在抛异常之前），只是没成 —— 然后摘要照发
        self.assertEqual([c[0] for c in calls], ["upload", "markdown"])
        md = [c[1] for c in calls if c[0] == "markdown"][0]
        self.assertIn("群里那份文件没传上去", md)
        self.assertNotIn("群里也发了一份", md)
        self.assertEqual(why, "已发摘要")

    def test_推送时把_xlsx_交给企微模板(self):
        """⚠ 少传这一个参数，群里就永远没有文件 —— 而 Python 测试全绿。"""
        import re
        from src.features.inventory import push as inv_push
        src = Path(inv_push.__file__).read_text(encoding="utf-8")
        i = src.index('notify.send("wecom"')
        blk = src[i:i + 400]
        self.assertIn('"template": "stock"', blk)
        self.assertIn('"xlsx": p', blk)
        # notify 那边也要往下传（两处都得在，缺一处就是静默没有文件）
        nt = (ROOT / "src" / "modules" / "notify" / "__init__.py").read_text(encoding="utf-8")
        self.assertRegex(nt, r'push_stock\(wc, ctx, lines, head, xlsx=content\.get\("xlsx"\)\)')
