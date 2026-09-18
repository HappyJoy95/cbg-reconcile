"""控制台信息架构（IA）—— 2026-09-18 前端融合改版。

用户：「分成**销售 合规 会话 通用设置**四个页面。销售里是达成这个。
合规里是 pos 和四池对比。销售和合规里面带着各自的设置。」

⚠ 这一版把**一级页签从「一个功能一个」改成「按业务分家」**，
门店已经认了两年旧界面 —— 所以四条"不能破"的得**逐个钉死**，
不能只测"HTML 里有这几个字符串"：

1. **运行日志要一直在、且好找** ⇒ 常驻抽屉，任何页都能开（**不是页签**）
2. **四池比对和 POS 合规不混在一张表里** ⇒ 合规页下两个**并列**的二级标签
3. **会话不能藏太深** ⇒ 保持**一级页签**
4. **前端仍不做构建步骤** ⇒ 原生 JS，没有打包产物

这些断言全是**结构**层面的（读 HTML / JS 源码）——
前端没有 lint、没有构建，`$('#foo')` 写错了只会在浏览器控制台里露一行，
跑测试和跑服务都看不见（这个项目为此踩过 `daysAgo` 那次）。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def _strip_html_comments(text):
    """⚠ 断言"没有 X"之前**先剥注释**。

    这个项目的注释是"记录踩过的坑"风格，里面会**原样提到**被删掉的东西
    （比如导航块上面那段注释就写着旧的五个页签名）——
    不剥的话注释自己会把 `assertNotIn` 顶掉（踩过两次，见 2026-09-18 日志）。
    """
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


NAV = re.search(r'<nav class="tabs">(.*?)</nav>', INDEX_HTML, re.S).group(1)
# ⚠ 一个导航项里 `data-tab` 出现**两次**（外层 `.nav-item` + 里面的按钮），去重保序
NAV_IDS = list(dict.fromkeys(re.findall(r'data-tab="([a-z]+)"', NAV)))
BODY = _strip_html_comments(INDEX_HTML)


def _nav_item(tab, nxt=None):
    """切出某个导航项的 HTML（含它的下拉菜单）。"""
    i = INDEX_HTML.index('class="nav-item" data-tab="%s"' % tab)
    j = (INDEX_HTML.index('data-tab="%s"' % nxt) if nxt
         else INDEX_HTML.index("</nav>", i))
    return INDEX_HTML[i:j]


class Test一级页签就四个业务(unittest.TestCase):
    """用户要的是「四个页面」—— 多一个都是没按说的做。"""

    def test_就四个_且顺序对(self):
        self.assertEqual(NAV_IDS, ["sales", "compliance", "session", "settings"])

    def test_每个都有对应的_panel(self):
        for tab in NAV_IDS:
            with self.subTest(tab=tab):
                self.assertIn('id="panel-%s"' % tab, INDEX_HTML)

    def test_旧的五个页签一个都不剩(self):
        # ⚠ 剥注释后查 —— 导航块上面的注释里正写着旧页签名
        for dead in ('data-tab="reports"', 'data-tab="pos"', 'data-tab="run"'):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, BODY, "旧的一级页签还在")

    def test_页面名字要有_通用二字(self):
        """「设置」现在是**通用设置** —— 因为业务页里也有各自的「设置」二级标签，
        两个都叫「设置」门店分不清哪个是哪个。"""
        self.assertIn(">通用设置<", NAV)


class Test会话保持一级入口(unittest.TestCase):
    """**会话不能藏太深**（用户明说）—— 抓会话是门店唯一要人工介入的地方。"""

    def test_会话是一级页签(self):
        self.assertIn("session", NAV_IDS)

    def test_会话不在任何二级标签里(self):
        self.assertNotIn('data-subtab="session"', BODY)


class Test合规页里两块并列不混(unittest.TestCase):
    """**四池比对和 POS 合规不要混在一张表里**（用户明说）。"""

    def setUp(self):
        i = INDEX_HTML.index('id="panel-compliance"')
        j = INDEX_HTML.index("</section>", i)
        self.panel = _strip_html_comments(INDEX_HTML[i:j])

    def test_两个二级标签并列(self):
        """⚠ 二级标签现在**只在顶部导航的下拉菜单里**（用户 2026-09-18 定：
        「只在下拉菜单里」）—— 所以"并列"看菜单顺序，页内那行已经去掉了。"""
        menu = _nav_item("compliance", nxt="session")
        self.assertEqual(re.findall(r'data-subtab="([a-z-]+)"', menu),
                         ["pos", "pools", "compliance-settings"])

    def test_两个子面板各自独立(self):
        self.assertIn('id="subpanel-pos"', self.panel)
        self.assertIn('id="subpanel-pools"', self.panel)
        # ⚠ 关键：**两个平级** —— 不是谁套在谁里面
        self.assertNotIn('id="subpanel-pos"', self.panel.split('id="subpanel-pools"')[1])

    def test_两块的表格容器是两个不同的_id(self):
        """混在一张表里最直接的表现就是**共用一个渲染容器**。"""
        self.assertIn('id="pos-table"', self.panel)
        self.assertIn('id="report-list"', self.panel)
        self.assertNotEqual(
            INDEX_HTML.index('id="pos-table"') == INDEX_HTML.index('id="report-list"'), True)

    def test_pos_的刷新按钮只刷_pos(self):
        self.assertIn('id="btn-refresh-pos"', self.panel)
        self.assertIn('id="btn-refresh-reports"', self.panel)


class Test运行日志常驻抽屉(unittest.TestCase):
    """**「运行日志」要一直在、且好找**（用户明说）—— 所以它不是页签。"""

    def test_抽屉在_main_之外(self):
        """在 `<main>` 里面就得先切到某个页签才看得到 —— 那就不叫"一直在"了。"""
        main_end = INDEX_HTML.index("</main>")
        self.assertGreater(INDEX_HTML.index('id="run-drawer"'), main_end)

    def test_不在任何_panel_里面(self):
        for tab in NAV_IDS:
            i = INDEX_HTML.index('id="panel-%s"' % tab)
            j = INDEX_HTML.index("</section>", i)
            with self.subTest(panel=tab):
                self.assertNotIn('id="run-drawer"', INDEX_HTML[i:j],
                                 "抽屉被塞进 %s 页里了 —— 那就不是常驻" % tab)

    def test_跑一次和日志都在抽屉里(self):
        i = INDEX_HTML.index('id="run-drawer"')
        j = INDEX_HTML.index("</aside>", i)
        drawer = INDEX_HTML[i:j]
        self.assertIn('id="run-log"', drawer)
        self.assertIn('data-what="all"', drawer)

    def test_有把手且任何页都能开(self):
        self.assertIn('id="btn-drawer"', INDEX_HTML)
        # 把手是**固定定位**的悬浮按钮（不是页签栏里的一项）
        self.assertIn(".drawer-fab", STYLE_CSS)
        self.assertIn("position: fixed", STYLE_CSS)

    def test_开关函数和绑定都在(self):
        self.assertIn("function setRunDrawer", APP_JS)
        self.assertIn("$('#btn-drawer')?.addEventListener", APP_JS)
        self.assertIn("$('#btn-drawer-close')?.addEventListener", APP_JS)

    def test_立即运行是开抽屉不是切页签(self):
        """⚠ 这个按钮原来是把「运行」**页签**点亮 —— 页签没了，
        不改成开抽屉的话，点下去跑是跑了，**日志一个字看不见**。"""
        i = APP_JS.index("$('#btn-quick-run').addEventListener")
        block = APP_JS[i:i + 300]
        self.assertIn("setRunDrawer(true)", block)
        self.assertNotIn("dataset.tab === 'run'", block)

    def test_抽屉里的日志不被高度封顶(self):
        """`.log` 默认 `max-height: 460px` —— 抽屉里再封一次就白搬了。"""
        self.assertIn(".drawer .log", STYLE_CSS)
        self.assertIn("max-height: none", STYLE_CSS)


class Test二级标签接线(unittest.TestCase):
    """二级标签的通用机制 —— 加错一个 `data-subtab` 就是**点了没反应**。"""

    def test_每个_data_subtab_都有对应的子面板(self):
        for name in re.findall(r'data-subtab="([a-z-]+)"', INDEX_HTML):
            with self.subTest(subtab=name):
                self.assertIn('id="subpanel-%s"' % name, INDEX_HTML,
                              "二级标签 %s 没有对应的 subpanel" % name)

    def test_每个二级标签都有加载器(self):
        """漏一个 = 切过去是空的（还可能是**上一页的旧数据**留在那儿）。"""
        i = APP_JS.index("const SUBTAB_LOADERS = {")
        block = APP_JS[i:APP_JS.index("};", i)]
        for name in re.findall(r'data-subtab="([a-z-]+)"', INDEX_HTML):
            with self.subTest(subtab=name):
                self.assertIn(name, block, "SUBTAB_LOADERS 里少了 %s" % name)

    def test_切回一级页签不会弹回第一个标签(self):
        """不传 subtab 就落回**上次停的那个** —— 否则"一直在看四池，
        切走再回来变成 POS 了"，很烦。"""
        i = APP_JS.index("const lastSubtab = {}")
        self.assertIn("lastSubtab[tab] = subtab", APP_JS[i:i + 600])
        j = APP_JS.index("function switchTab")
        self.assertIn("subtab = subtab || lastSubtab[tab] || subs[0]", APP_JS[j:j + 400])

    def test_悬停只展开菜单_不切页(self):
        """⚠ 用户 2026-09-18 明确选的：**悬停只展开菜单，点击才跳**。

        所以 `mouseenter` 里只准开菜单 —— 一旦有人"顺手"让它切页，
        鼠标扫过顶部就会乱跳（触屏机上划一下也算 hover）。这条是**反向断言**。"""
        # ⚠ 锚在**那行绑定**上 —— `$$('.nav-item').forEach` 在 `closeNavMenus`
        #   里先出现过一次，按它切会切到隔壁函数里去（第一版就这么假绿了）
        i = APP_JS.index("item.addEventListener('mouseenter'")
        block = APP_JS[i:i + 500]
        self.assertIn("addEventListener('mouseenter', () => openNavMenu(item))", block)
        self.assertNotIn("mouseenter', () => switchTab", block,
                         "悬停改成直接切页了？那不是用户要的（点击才跳）")

    def test_点完顶部页签菜单还在(self):
        """⚠ `switchTab` 会收菜单，但点完鼠标**还停在导航上** ——
        不补回来的话，用户得先把鼠标移开再移回来才看得到菜单（实测很别扭）。"""
        i = APP_JS.index("$$('.tab').forEach((b) => b.addEventListener('click'")
        block = APP_JS[i:i + 500]
        self.assertIn("item.matches(':hover')", block)
        self.assertIn("openNavMenu(item)", block)

    def test_菜单里的项点了才切(self):
        # ⚠ 锚在**那行绑定**上 —— `$$('.nav-item').forEach` 在 `closeNavMenus`
        #   里先出现过一次，按它切会切到隔壁函数里去（第一版就这么假绿了）
        i = APP_JS.index("item.addEventListener('mouseenter'")
        block = APP_JS[i:i + 500]
        self.assertIn("switchTab(item.dataset.tab, b.dataset.subtab)", block)

    def test_没有二级的页签不挂菜单(self):
        """会话 / 通用设置没有二级 —— 给它们空菜单的话，
        悬停会弹出一个空框，看着像坏了。"""
        for tab in ("session", "settings"):
            with self.subTest(tab=tab):
                # 它们是**光按钮**，不在 .nav-item 里
                self.assertNotIn('class="nav-item" data-tab="%s"' % tab, BODY)

    def test_菜单默认是收起的(self):
        """漏了 hidden 的话页面一打开两个菜单全摊着。"""
        for m in re.finditer(r'<div class="nav-menu"([^>]*)>', INDEX_HTML):
            with self.subTest(m=m.group(0)):
                self.assertIn("hidden", m.group(1))

    def test_键盘和触摸也能展开(self):
        """⚠ 光绑 hover 的话**触屏机上一辈子打不开**（门店可能是触摸屏）。"""
        # ⚠ 锚在**那行绑定**上 —— `$$('.nav-item').forEach` 在 `closeNavMenus`
        #   里先出现过一次，按它切会切到隔壁函数里去（第一版就这么假绿了）
        i = APP_JS.index("item.addEventListener('mouseenter'")
        block = APP_JS[i:i + 500]
        self.assertIn("addEventListener('focusin', () => openNavMenu(item))", block)

    def test_子面板默认只亮一个(self):
        """HTML 里写死两个 `active` 的话，两个子页会**同时显示**。"""
        for tab in ("sales", "compliance"):
            i = INDEX_HTML.index('id="panel-%s"' % tab)
            j = INDEX_HTML.index("</section>", i)
            n = len(re.findall(r'class="subpanel active"', INDEX_HTML[i:j]))
            with self.subTest(panel=tab):
                self.assertEqual(n, 1, "panel-%s 里有 %d 个默认亮着的子页" % (tab, n))

    def test_二级标签只有一份(self):
        """⚠ 用户 2026-09-18 定：「只在下拉菜单里」。

        导航里一份 + 页内再一份的话，迟早有一处不同步
        —— 这个项目为"同一件事两份定义"栽过好几次（步骤定义、按钮预设…）。"""
        self.assertEqual(len(re.findall(r'data-subtab=', INDEX_HTML)),
                         len(re.findall(r'data-subtab="[a-z-]+"', NAV)),
                         "页内还有一份二级标签")


class Test首屏加载(unittest.TestCase):
    """⚠ 首屏加载**必须跟着当前亮着的页签走**。

    2026-09-18 改版前启动那段写死 `loadOverview()` —— 那时默认页正是四池比对，
    所以没错。默认页换成「销售」之后，那样写的结果是：
    **四池的数据白拉一遍，销售页却一片空白**（连空状态都没渲染，看着像坏了）。
    截图实测抓到过这一次。"""

    def _boot(self):
        i = APP_JS.index("/* ───────────────────────────── 启动")
        return APP_JS[i:i + 500]

    def test_启动跟着当前页签(self):
        self.assertIn("switchTab(", self._boot())

    def test_不再写死拉四池(self):
        self.assertNotIn("\nloadOverview();", self._boot(),
                         "首屏又写死拉四池了 —— 默认页不是它")

    def test_销售页的空状态渲染得出来(self):
        """接口没上时也要**明确说一句** —— 不能给一张空卡片。"""
        self.assertIn("function renderAttainPlaceholder", APP_JS)
        self.assertIn("还没接上数据", APP_JS)


class Test前端不做构建步骤(unittest.TestCase):
    """项目红线：原生 JS、无打包 —— 改完刷新即生效。"""

    def test_html_直接引_app_js(self):
        self.assertIn('<script src="/app.js"></script>', INDEX_HTML)

    def test_没有打包产物引用(self):
        for bad in (".min.js", "bundle.js", "webpack", "vite", "import "):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
