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


class Test一级页签(unittest.TestCase):
    """用户 2026-09-18 先把一级页签定成四个（销售/合规/会话/通用设置），
    随后又加了「云商授权」（「会话」同时改名「玲珑授权」）—— 现在是五个。

    ⚠ 个数不是重点，**顺序和名字**才是：它们就是门店眼里的目录。
    """

    def test_顺序和名字(self):
        """⚠ 2026-09-18 后半程用户又收了一次：「把玲珑授权和云商授权做到设置的
        二级标签里面」—— 两个授权从一级页签降成「通用设置」的二级标签，
        一级回到**三个**。"""
        self.assertEqual(NAV_IDS, ["sales", "compliance", "settings"])

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


class Test授权页在通用设置下面(unittest.TestCase):
    """用户 2026-09-18：「把玲珑授权和云商授权做到设置的二级标签里面」。

    「会话」也一起改名成了「玲珑授权」（玲珑 = 华为那个销售系统的代号）。

    ⚠ 同一天**「云商授权」又删了** —— 用户先砍掉公司账号那张卡
    （「公司账号前端不显示吧，后端默认用…」），再砍掉门店账号
    （「我想了想，不要门店云商账号了，没必要」）。两张卡都没了，
    那一页就是空的，所以整个二级标签一起撤。
    → 现在「通用设置」下面是 **通用 / 玲珑授权** 两个。

    它们**不是一级页签** —— 但也不能藏得不认识路：
    `whatsnew` 里那几条「去会话」的待办得能跳对地方（见 `GO_TARGETS`）。"""

    def _settings(self):
        i = INDEX_HTML.index('id="panel-settings"')
        return _strip_html_comments(INDEX_HTML[i:INDEX_HTML.index("</section>", i)])

    def test_玲珑是通用设置的二级标签(self):
        """⚠ 落点在**导航下拉**里，不是页内那排按钮
        （用户 2026-09-18 选的："跟销售/合规一样，页内那排删掉"）。"""
        menu = _nav_item("settings")
        self.assertEqual(re.findall(r'data-subtab="([a-z-]+)"', menu),
                         ["general", "linglong"])

    def test_玲珑的子面板在通用设置里(self):
        self.assertIn('id="subpanel-linglong"', self._settings())

    def test_云商授权整个撤掉了(self):
        """⚠ 留着的话是个**空页** —— 点进去什么都没有，看着像坏了。"""
        self.assertNotIn("erp", NAV_IDS)
        self.assertNotIn("云商授权", _strip_html_comments(NAV))
        self.assertNotIn('id="subpanel-erp"', INDEX_HTML)
        self.assertNotIn("erp", re.findall(r'data-subtab="([a-z-]+)"',
                                           _strip_html_comments(NAV)))
        # 页面上也不许再有云商账号的输入框（两套 id 都查）
        for dead in ("erp-username", "erp-password", "erp-store-username",
                     "btn-erp-save", "btn-erp-store-save"):
            with self.subTest(dead=dead):
                self.assertNotIn('id="%s"' % dead, BODY,
                                 "云商账号的表单又回来了？前端不该有它的入口")

    def test_一级页签里没有它(self):
        self.assertNotIn("linglong", NAV_IDS)

    def test_跳转表里有它们(self):
        """⚠ 少了映射，`whatsnew` 点「去会话」就会去切一个不存在的页签 ——
        门店看到的是**点了没反应**。"""
        i = APP_JS.index("const GO_TARGETS = {")
        block = APP_JS[i:APP_JS.index("};", i)]
        for key in ("linglong", "settings", "general"):
            with self.subTest(key=key):
                self.assertIn(key + ":", block)

    def test_名字对(self):
        menu = _nav_item("settings")
        self.assertIn(">玲珑授权<", menu)
        self.assertNotIn(">会话<", menu, "「会话」应该已经改名成「玲珑授权」了")
        # ⚠ 名字也**不许**在页内再写一份（同一件事两份定义必然有一天不同步）
        self.assertNotIn("玲珑授权", BODY.replace(_strip_html_comments(NAV), ""))


class Test合规页里两块并列不混(unittest.TestCase):
    """**四池比对和 POS 合规不要混在一张表里**（用户明说）。"""

    def setUp(self):
        i = INDEX_HTML.index('id="panel-compliance"')
        j = INDEX_HTML.index("</section>", i)
        self.panel = _strip_html_comments(INDEX_HTML[i:j])

    def test_两个二级标签并列(self):
        """⚠ 二级标签现在**只在顶部导航的下拉菜单里**（用户 2026-09-18 定：
        「只在下拉菜单里」）—— 所以"并列"看菜单顺序，页内那行已经去掉了。"""
        menu = _nav_item("compliance", nxt="settings")
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

    def test_立即运行按钮已删且没留悬空引用(self):
        """⚠ 用户 2026-09-18：「左下那个立即运行按钮不要了」。

        ⚠ **元素删了、引用必须一起删** —— 留着的话 `$('#btn-quick-run')`
        拿到 null 再 `.addEventListener` 就抛 TypeError，
        **它后面的整段 app.js 都不会执行**（前端没 lint，只有浏览器控制台露一行）。
        这跟 2.x 那次 `daysAgo` 是同一类错。"""
        self.assertNotIn('id="btn-quick-run"', INDEX_HTML)
        used = set(re.findall(r"""\$\(\s*['\"]#([A-Za-z0-9_-]+)['\"]\s*\)""", APP_JS))
        self.assertNotIn("btn-quick-run", used, "按钮没了但 JS 还在引用它")

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

    # ⚠ 原来这里还有一条「没有二级的页签不挂菜单」（针对会话 / 通用设置两个光杆按钮）。
    #   2026-09-18 用户把「通用设置」也改成下拉之后，**三个一级菜单一律挂菜单**，
    #   那条断言的前提没了 —— 删掉，别让它以"恒真"的形式留着占位。
    #   「菜单不能是空的」这条挪进了 `test_每个一级菜单都挂了下拉`。

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

    def test_二级标签只在导航里有一份(self):
        """⚠ 用户 2026-09-18 定：二级标签「只在下拉菜单里」，**页内不再占一行**。

        一开始「通用设置」那三个破例放在页内的标签条里（`#settings-tabs`），
        但那排按钮**压根没绑事件** —— 点了没反应，截图实测抓到的。
        用户随后拍板"跟销售/合规一样，页内那排删掉"，于是**三处形态统一**。

        ⚠ 这里挖 nav 必须**两边都先剥注释**：`NAV` 取自原始 HTML（里面有注释），
        而 `BODY` 已经剥过，直接 replace 匹不上，等于没挖（第一版就这么假绿了）。"""
        in_page = re.findall(r'data-subtab="([a-z-]+)"',
                             BODY.replace(_strip_html_comments(NAV), ""))
        self.assertEqual(in_page, [], "二级标签只能在下拉菜单里，页内不该再有：%s" % in_page)

    def test_每个一级菜单都挂了下拉(self):
        """⚠ 「通用设置」以前是个光杆按钮（没有 `nav-menu`）——
        三个一级菜单形态不一致，用户选了统一成下拉。"""
        for tab in NAV_IDS:
            menu = _nav_item(tab, nxt=None) if tab == NAV_IDS[-1] else _nav_item(
                tab, nxt=NAV_IDS[NAV_IDS.index(tab) + 1])
            with self.subTest(tab=tab):
                self.assertIn('class="nav-menu"', menu, "%s 没有下拉菜单" % tab)
                self.assertTrue(re.findall(r'data-subtab="([a-z-]+)"', menu),
                                "%s 的下拉是空的" % tab)

    def test_导航里的二级标签要和_SUBTABS_对得上(self):
        """⚠ `data-subtab` 在 HTML 里、`SUBTABS` 在 JS 里 —— **同一件事两份定义**。

        对不上的后果分两种，都很难查：HTML 多一个 → 那按钮点了没反应；
        JS 多一个 → 那个二级标签永远切不到（`switchTab` 会把它当"不认识的"弹回第一个）。"""
        block = APP_JS[APP_JS.index("const SUBTABS = {"):]
        block = block[:block.index("\n};")]
        want = {}
        for tab, subs in re.findall(r"(\w+):\s*\[([^\]]*)\]", block):
            want[tab] = re.findall(r"'([a-z-]+)'", subs)
        self.assertEqual(sorted(want), sorted(NAV_IDS), "两边的页签集合不一样")
        for tab, subs in want.items():
            menu = _nav_item(tab) if tab == NAV_IDS[-1] else _nav_item(
                tab, nxt=NAV_IDS[NAV_IDS.index(tab) + 1])
            with self.subTest(tab=tab):
                self.assertEqual(re.findall(r'data-subtab="([a-z-]+)"', menu), subs)


class Test首屏加载(unittest.TestCase):
    """⚠ 首屏加载**必须跟着当前亮着的页签走**。

    2026-09-18 改版前启动那段写死 `loadOverview()` —— 那时默认页正是四池比对，
    所以没错。默认页换成「销售」之后，那样写的结果是：
    **四池的数据白拉一遍，销售页却一片空白**（连空状态都没渲染，看着像坏了）。
    截图实测抓到过这一次。"""

    def _boot(self):
        i = APP_JS.index("/* ───────────────────────────── 启动")
        # ⚠ 切**够长** —— 这段注释本身就占好几百字符，切短了会把
        #   `loadOverview();` 那行留在窗口外面，断言变成假绿
        return APP_JS[i:i + 1400]

    def test_启动跟着当前页签(self):
        self.assertIn("switchTab(", self._boot())

    def test_侧边栏的状态也要拉(self):
        """⚠ `loadOverview()` **不能删** —— 它不只是四池那页的数据，
        侧边栏左下角的**门店名 / 会话·定时徽章**也是它填的。

        第一次改版时顺手删了它，左下角就一直停在「加载中…」、徽章也没字
        （截图抓到的）。所以两个都要：`switchTab` 管当前页内容，
        `loadOverview` 管这一圈的常驻状态。"""
        self.assertIn("loadOverview();", self._boot(),
                      "删了 loadOverview → 左下角门店信息永远是「加载中…」")

    def test_门店信息在左下角(self):
        """用户 2026-09-18：「把上面的门店设置这些放到左下角」。"""
        foot = re.search(r'<div class="side-foot">(.*?)</div>\s*</aside>',
                         INDEX_HTML, re.S).group(1)
        for ident in ("store-line", "build-line", "pill-session"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, foot)
        # 顶栏整个去掉了
        self.assertNotIn("<header", INDEX_HTML)
        self.assertIn(".side-foot", STYLE_CSS)
        self.assertIn("margin-top: auto", STYLE_CSS)

    def test_销售页的空状态渲染得出来(self):
        """接口没上时也要**明确说一句** —— 不能给一张空卡片。"""
        self.assertIn("function renderAttainPlaceholder", APP_JS)
        self.assertIn("还没接上数据", APP_JS)


class Test左侧栏折叠(unittest.TestCase):
    """用户 2026-09-18：「加个折叠的按钮，可以把左侧的标签栏折叠隐藏和展开」。"""

    def test_开关在侧栏外面(self):
        """⚠ **开关不能放在侧栏里** —— 一收起它自己也被藏了，就再也展不开。"""
        side = re.search(r'<aside class="sidebar"[^>]*>', INDEX_HTML)
        self.assertIsNotNone(side)
        # 开关在 <aside> **之前**
        self.assertLess(INDEX_HTML.index('id="btn-sidebar"'), side.start())

    def test_开关不要框(self):
        """⚠ 用户 2026-09-18：「这个按钮能不能不要这样，**不要框**」。

        所以 `.side-toggle` 那条规则里**不许有 border / 底色 / 阴影** ——
        它就是个箭头。hover 那圈淡蓝是"能点"的反馈，不算框。
        （分开写一条是因为"调样式"最容易顺手把框加回来。）"""
        i = STYLE_CSS.index(".side-toggle {")
        rule = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        # ⚠ 正面断言"显式无框"，别用 `assertNotIn("border:", ...)` ——
        #   `border: 0` 里**就是**含 `border:` 三个字，那样写必假红（第一版就写了）
        self.assertIn("border: 0", rule, "折叠开关要有显式的 border: 0")
        self.assertIn("background: none", rule, "折叠开关不该有底色")
        self.assertNotIn("box-shadow", rule, "折叠开关不该有阴影")

    def test_展开时跨在侧栏边界上(self):
        """「展开时突出半个按钮」—— `left` 是侧栏宽 **减半个按钮**，
        所以它一半在侧栏里、一半探到内容区。"""
        i = STYLE_CSS.index(".side-toggle {")
        rule = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        self.assertIn("left: calc(var(--side-w) - 13px)", rule)
        self.assertIn("width: 26px", rule)

    def test_折叠时留在左上角(self):
        """「折叠时留个按钮」—— 侧栏没了它也得在，否则展不开。"""
        self.assertIn("body.side-collapsed .side-toggle", STYLE_CSS)
        i = STYLE_CSS.index("body.side-collapsed .side-toggle")
        self.assertIn("left: 10px", STYLE_CSS[i:i + 80])

    def test_开关是固定定位(self):
        i = STYLE_CSS.index(".side-toggle")
        self.assertIn("position: fixed", STYLE_CSS[i:i + 400])

    def test_折叠就是藏掉侧栏(self):
        self.assertIn("body.side-collapsed .sidebar", STYLE_CSS)
        self.assertIn("display: none", STYLE_CSS)

    def test_侧栏宽度只有一处定义(self):
        """开关的位置靠 `--side-w` 算 —— 宽度写死两处迟早对不上。"""
        self.assertIn("--side-w:", STYLE_CSS)
        self.assertIn("flex: 0 0 var(--side-w)", STYLE_CSS)
        # ⚠ 只断言"位置是**算**出来的"，不钉具体像素 ——
        #   钉死了以后调个 1px 就得改测试（第一版就写成 `- 30px`，白红一次）
        self.assertIn("left: calc(var(--side-w)", STYLE_CSS)

    def test_有折叠逻辑且首屏就应用(self):
        self.assertIn("function setSidebarCollapsed", APP_JS)
        i = APP_JS.index("function setSidebarCollapsed")
        self.assertIn("setSidebarCollapsed(sidebarCollapsed)", APP_JS[i:],
                      "首屏没应用 —— 会先展开一下再收起（闪一下）")

    def test_折叠状态记得住(self):
        self.assertIn("localStorage", APP_JS)
        self.assertIn("cbg-side-collapsed", APP_JS)

    def test_窄屏时开关不飘在中间(self):
        """<820px 侧栏横过来铺在上面，开关得跟着回左上角。"""
        i = STYLE_CSS.index("@media (max-width: 820px)")
        self.assertIn(".side-toggle", STYLE_CSS[i:i + 400])


class Test不能有重复_id(unittest.TestCase):
    """⚠ 同一个 `id` 在 HTML 里出现两次 —— 2026-09-18 真出现过一次。

    搬「云商账号」那张卡时，脚本把"从通用设置里删掉"那一步漏了
    （`rw()` 从磁盘重读，而改过的字符串忘了写回去），结果卡片**两份**、id 也两份。
    后果很阴：`$('#erp-status')` 只拿到**第一个**，第二份永远是空的/旧的，
    看着像"设置没保存"。

    （`test_every_referenced_id_exists_in_html` 只查"有没有"，查不出"有几份"。）
    """

    def test_每个_id只出现一次(self):
        ids = re.findall(r"""id=["']([A-Za-z0-9_-]+)["']""", _strip_html_comments(INDEX_HTML))
        dup = sorted({i for i in ids if ids.count(i) > 1})
        self.assertFalse(dup, "这些 id 出现了不止一次：%s" % dup)

    def test_云商账号在界面上没有任何入口(self):
        """⚠ 2026-09-18 **一天之内走了个来回**，最后是一个输入框都不留：

        1. 「需要存两个云商账号」（公司 + 门店）→ 做了两张卡；
        2. 「公司账号前端不显示吧，后端默认用 sL18917405716」→ 删公司那张；
        3. 「我想着，不要门店云商账号了，没必要」→ **门店那张也删**
           （原话是「我想了想」，意思是想了想决定不要了）。

        钉住"真删干净了"：两套 id 一个都不许剩 —— 留着的话
        `$('#erp-username')` 拿得到元素、看着一切正常，
        实际改的是一个界面上看不见的账号（上一版正是这个形状）。
        """
        for ident in ("erp-username", "erp-password", "erp-company", "erp-token",
                      "erp-status", "btn-erp-save", "btn-erp-login",
                      "erp-store-username", "erp-store-password", "erp-store-company",
                      "erp-store-token", "erp-store-status", "erp-store-captcha",
                      "btn-erp-store-save", "btn-erp-store-login"):
            with self.subTest(ident=ident):
                self.assertNotIn('id="%s"' % ident, BODY,
                                 "云商账号的表单又回来了？前端不该有它的入口")

    def test_前端不再有云商账号的接线(self):
        """⚠ 表单删了、接线还留着 = `$('#…')` 全部拿到 null，
        `loadErpAll()` 一抛 —— **它后面的初始化全不执行**
        （前端没 lint，只在浏览器控制台露一行，门店只会说"这页是空的"）。"""
        for dead in ("loadErpAll", "ERP_ROLES", "ERP_F", "ERP_B", "saveErp",
                     "loadErp("):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, APP_JS,
                                 "%s 还留着 —— 云商账号那张卡已经删了" % dead)

    def test_云商授权那个二级标签也没了(self):
        """⚠ 两张卡都删之后那一页是**空的**，留着就是个点了没反应的死页签。"""
        self.assertNotIn("erp", re.findall(r'data-subtab="([a-z-]+)"',
                                           _strip_html_comments(NAV)))


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
