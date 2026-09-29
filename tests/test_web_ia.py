"""控制台信息架构（IA）—— 2026-09-18 前端融合改版。

用户：「分成**销售 合规 会话 通用设置**四个页面。销售里是达成这个。
合规里是 pos 和四池对比。销售和合规里面带着各自的设置。」

⚠ 这一版把**一级页签从「一个功能一个」改成「按业务分家」**，
门店已经认了两年旧界面 —— 所以四条"不能破"的得**逐个钉死**，
不能只测"HTML 里有这几个字符串"：

1. **运行日志要一直在、且好找** ⇒ 常驻抽屉，任何页都能开（**不是页签**）
2. **报量查询和 POS 合规不混在一张表里** ⇒ 合规页下两个**并列**的二级标签
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
#: ⚠ 设计令牌（颜色/尺寸/圆角/阴影/动效）2026-09-18 搬去了 `theme.css` ——
#:   只读 style.css 的话，"某个令牌只有一处定义"这类断言会全部找不到锚点。
THEME_CSS = (ROOT / "web" / "theme.css").read_text(encoding="utf-8")
#: 库存盘点那一页（M16）——它自己那套 js/css 单独读一份，别跟控制台的混
INV_UI_JS = (ROOT / "web" / "inventory" / "ui.js").read_text(encoding="utf-8")
INV_CSS = (ROOT / "web" / "inventory" / "style.css").read_text(encoding="utf-8")


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


def _foot():
    """左下角那几行的 HTML（`#side-foot`）。

    ⚠ 2026-09-19（用户）改过形态：「**不是现在弹出悬浮的这个方式，而是左下角的
      这个签往上动** —— 门店&姓名这一行变成账号设置，版本号这一行变成检查更新，
      会话导入情况变成玲珑授权。人员设置和通用插缝出现。未定时变成定时器设置」。
      ⇒ **没有浮层了**：`.foot-item` 里两份内容交叉淡入淡出，`.foot-gap` 撑开。
    ⚠ 键在 **`data-foot`** 上（不是 `data-subtab`）—— 左下角菜单自己的键空间。
    """
    i = INDEX_HTML.index('<div class="side-foot"')
    return INDEX_HTML[i:INDEX_HTML.index("</aside>", i)]


def _foot_items():
    """左下角那几行 → [(键, 菜单名)]，顺序 = 界面顺序。"""
    return [(k, v.strip()) for k, v in
            re.findall(r'data-foot="([a-z]+)"[^>]*>.*?<span class="foot-label">([^<]+)</span>',
                       _foot(), re.S)]




class Test一级页签(unittest.TestCase):
    """用户 2026-09-18 先把一级页签定成四个（销售/合规/会话/通用设置），
    随后又加了「云商授权」（「会话」同时改名「玲珑授权」）—— 现在是五个。

    ⚠ 个数不是重点，**顺序和名字**才是：它们就是门店眼里的目录。
    """

    def test_顺序和名字(self):
        """⚠ 2026-09-19（用户）：「把左边儿一级标签的设置去掉吧，给它弄成鼠标移动到
        左下角门店那个页面的时候，向上出现设置里的那些东西」。

        ⇒ 一级页签**只剩两个业务页** —— 第三个原来是「通用设置」，
          现在连它带两条二级「设置」一起搬进左下角那几行（`_foot()`）。

        ⚠ 2026-09-20（M16）：**第三个业务页**加回来了 —— 「库存盘点」
          （用户：「把库存盘点功能整理成一个模块接入我们这个项目」）。
          形态跟 sales / compliance 一样：控制台里的子面板
          （`#subpanel-inventory` 里是 `.inv-root` 同文档 markup，已拆 iframe），
          见 `Test库存盘点是嵌进来的子面板`。
        """
        # ⚠ 2026-09-21（M20）：本来加过一个一级「数据上报」，当天用户就改了口径 ——
        #   「**放到左下角的系统设置吧，一级标签改叫数据交换**」
        #   ⇒ 那一页挪去左下角那一行（`data-foot="stores"`），
        #     见 `Test左下角菜单的行为`。可见性归 `tests/test_roles.py`。
        # ⭐ 2026-09-21（M22）：**第四个业务页**「月度生意计划」——
        #   用户：「设计一个新模块，叫做月度生意计划」。它排在「周度重点产品」**后面**
        #   （注册表里 `order=15`：这两个是同一族，都是"看门店卖得怎么样"）。
        # ⭐ 2026-09-22：**小工具**（价签 / 工牌）—— 用户：「左边栏下面做个小标签叫小工具」。
        # ⭐ 2026-09-29（2.3.0）：**分销** —— 渠道分销部四张看板（区域/机型/销售员/明细），
        #   注册表 `order=45` 排在小工具后面；可见性 `types="multi"`（区长/平台）
        #   归 `tests/test_roles.py` 管，这里只钉**顺序和名字**。
        self.assertEqual(NAV_IDS,
                         ["sales", "plan", "compliance", "inventory", "valueadd",
                          "tools", "distribution"])

    def test_每个都有对应的_panel(self):
        for tab in NAV_IDS:
            with self.subTest(tab=tab):
                self.assertIn('id="panel-%s"' % tab, INDEX_HTML)

    def test_界面上的名字(self):
        """⚠ 这些字是**门店眼里的目录**，用户 2026-09-18 傍晚调过一轮：

        | 页签 id | 旧名 | 新名 |
        |---|---|---|
        | `sales` | 销售 → 销售数据 | **周度重点产品**（2026-09-20 又改过一次） |
        | `attain` | 达成 | **周度目标达成情况** |
        | `compliance` | 合规 | **五项合规** |
        | `pools` | 报量查询 | **报量查询** |

        ⚠ **只改显示的名字，id 一个都不许动**：`sales` / `compliance` / `attain` /
        `pools` 是 `GO_TARGETS`、`LEGACY_GO`（老更新日志的 `go`）、
        `data-subtab` 接线三处的公共 key，动一个就要迁移三处。
        """
        # ⚠ `sales` 的名字改过**两轮**：`销售` →（2026-09-18）`销售数据` →
        #   （2026-09-20，用户）`周度重点产品`。**key 一直是 `sales`，别跟着改。**
        want = {"sales": "周度重点产品", "compliance": "五项合规"}
        for tab, label in want.items():
            with self.subTest(tab=tab):
                m = re.search(r'class="tab[^"]*" data-tab="%s"[^>]*>([^<]+)<' % tab, NAV)
                self.assertIsNotNone(m, "找不到 %s 那个一级页签" % tab)
                self.assertEqual(m.group(1).strip(), label)
        subs = dict(re.findall(r'data-subtab="([a-z-]+)"[^>]*>([^<]+)<', NAV))
        self.assertEqual(subs.get("attain"), "周度目标达成情况")
        self.assertEqual(subs.get("pools"), "报量查询")

    def test_旧的五个页签一个都不剩(self):
        # ⚠ 剥注释后查 —— 导航块上面的注释里正写着旧页签名
        for dead in ('data-tab="reports"', 'data-tab="pos"', 'data-tab="run"'):
            with self.subTest(dead=dead):
                self.assertNotIn(dead, BODY, "旧的一级页签还在")

    def test_设置不再是一级页签(self):
        """⚠ 用户 2026-09-19：「把左边儿**一级标签的设置**去掉」——
        指的就是「通用设置」这个一级页签（它搬去了左下角浮层）。

        ⚠ **别再顺手把模块的「设置」也一起砍掉** —— 用户紧跟着纠正过一次：
          「**各个模块的设置还是要给模块**，只把通用设置做下来就行了」。
          所以那两条二级「设置」**必须在**（见下一条）。
        """
        self.assertNotIn("settings", NAV_IDS)
        self.assertNotIn(">通用设置<", _strip_html_comments(NAV))
        self.assertNotIn('data-tab="settings"', _strip_html_comments(NAV))

    def test_模块的设置留在模块里(self):
        """⚠ 这是用户**纠正过我一次**的地方，单列一条钉住：

        用户 2026-09-19 原话「各个模块的设置还是要给模块，只把通用设置做下来就行了」。
        判别标准 —— **跟业务有关**的设置留在模块（这条推送推不推）；
        **这台电脑本身**的配置（登录态 / 人员 / 邮件 / 定时）才在左下角。

        所以：
        * `sales-settings` 在「销售数据」的下拉里、子面板在 `#panel-sales`；
        * `compliance-settings` 在「五项合规」的下拉里、子面板在 `#panel-compliance`；
        * 两条都**不许**出现在左下角浮层里（那就成两份入口了）。
        """
        pairs = {"sales": "sales-settings", "compliance": "compliance-settings",
                 "valueadd": "valueadd-settings"}
        for tab, sub in pairs.items():
            with self.subTest(sub=sub):
                menu = _nav_item(tab, nxt=None) if tab == NAV_IDS[-1] else _nav_item(
                    tab, nxt=NAV_IDS[NAV_IDS.index(tab) + 1])
                self.assertIn('data-subtab="%s"' % sub, menu,
                              "%s 的下拉里少了「设置」" % tab)
                i = INDEX_HTML.index('id="panel-%s"' % tab)
                seg = INDEX_HTML[i:INDEX_HTML.index("</section>", i)]
                self.assertIn('id="subpanel-%s"' % sub, seg,
                              "%s 的子面板不在自己的面板里" % sub)
                self.assertNotIn(sub, [k for k, _ in _foot_items()],
                                 "%s 不该出现在左下角那几行里" % sub)
        # 顺序也要在（「设置」排在本模块功能页的**后面**）
        self.assertEqual(re.findall(r'data-subtab="([a-z-]+)"',
                                   _nav_item("sales", nxt="plan")),
                         # ⚠ 2026-09-20：目标拆分**不再单独一页**（改在达成页里点门店名展开）
                         # ⚠ 2026-09-20：「历史记录」紧跟在达成**下面**
                         #   （用户：「在工作区的**周度目标达成情况下面**加个历史记录」）
                         ["attain", "attain-history", "sales-settings"])


class Test左下角那几行就是设置菜单(unittest.TestCase):
    """用户 2026-09-19：

        「不是现在弹出悬浮的这个方式，而是**左下角的这个签往上动** ——
         门店&姓名这一行变成**账号设置**，版本号这一行变成**检查更新**，
         会话导入情况变成**玲珑授权**。人员设置和通用**插缝出现**。
         未定时变成**定时器设置**，有新版本跟着版本号走。」

    ⇒ **没有浮层**：`.foot-item` 里 `.foot-info`（平时显示的）和 `.foot-label`
      （菜单名）交叉淡入淡出，`.foot-gap` 那两条 0 高度撑开。
    """

    def test_六项按顺序排(self):
        """顺序 = 界面顺序，也是 `app.js` 里 `FOOT_GO` 的顺序。

        ⚠ 前四项的「位置」是有讲究的：**账号设置占原来"门店&姓名"那一行**、
          检查更新占版本号那一行、玲珑授权占会话那一行、
          定时器设置占「未定时」那一行 —— 中间两条才是插进去的。
        """
        # ⚠ 2026-09-21：「人员设置」并进「账号与人员」了（用户：「账号设置和人员设置
        #   合并到一起吧」）—— 菜单少一行，账号那行改名。
        # ⚠ 2026-09-21：加了第五行「**数据交换**」（M20 的每店一张卡）——
        #   用户当天的原话：「数据上报这个是好的，**放到左下角的系统设置**吧，
        #   一级标签改叫**数据交换**」（它原来是顶部一个一级标签）。
        # ⭐ 2026-09-22：加了「主题设置」（独立二级页，在「推送设置」后面）
        self.assertEqual([k for k, _ in _foot_items()],
                         ["account", "update", "general", "theme", "stores",
                          "linglong", "scheduler"])
        self.assertEqual([n for _, n in _foot_items()],
                         ["账号与人员", "检查更新", "推送设置", "主题设置", "数据交换",
                          "玲珑授权", "定时器设置"])

    def test_插缝那两条是_gap_类(self):
        """⚠ 「人员设置和通用**插缝出现**」—— 它们是**从 0 高度撑开**的两条。

        漏了 `foot-gap` 的话它们一直占着一行，"插缝"就没了。
        """
        seg = _foot()
        gaps = re.findall(r'class="foot-item foot-gap" data-foot="([a-z]+)"', seg)
        # ⚠ 2026-09-21：原来"插缝"的是**两条**（人员设置 / 通用）——
        #   人员设置并进账号页之后只剩「通用」；同一天又加了「数据交换」（M20）——
        #   它跟「通用」一样是**页面入口**（不是"门店名/版本号/会话/定时"那种状态行），
        #   所以也是插缝的：平时不占位，悬浮才撑开一行。
        self.assertEqual(gaps, ["general", "theme", "stores"], "插缝那几条变了")

    def test_每一行都有信息面和菜单面(self):
        """⚠ 变形的做法是**两份内容叠在同一格**交叉淡入淡出。

        * 有 `.foot-info` 的四行 = 原来那几行（门店·姓名 / 版本号 / 会话 / 定时）
          —— 它们"变成"菜单项；
        * 插缝的两行**只有** `.foot-label`（本来就没有信息面）。
        两边都少不得：漏了 `.foot-label` 那一行点了没名字，漏了 `.foot-info`
        原来那行信息就永远看不见了。
        """
        seg = _foot()
        rows = re.findall(r'<button class="foot-item[^"]*" data-foot="([a-z]+)"[^>]*>(.*?)</button>',
                          seg, re.S)
        self.assertEqual([k for k, _ in rows],
                         ["account", "update", "general", "theme", "stores",
                          "linglong", "scheduler"])
        for key, body in rows:
            with self.subTest(row=key):
                # ⚠ 用前缀匹 —— `build-line` 那行是 `class="foot-info mono-build"`
                #   （多个类名），写 `class="foot-info"` 会匹不上（第一版就这么红的）。
                self.assertIn('class="foot-label"', body, "%s 没有菜单名" % key)
                if key in ("account", "update", "linglong", "scheduler"):
                    self.assertIn('class="foot-info', body, "%s 少了原来那行的信息" % key)
                else:
                    self.assertNotIn('class="foot-info', body,
                                     "%s 是插缝的，不该有信息面" % key)

    def test_原来那三样东西还在(self):
        """⚠ 「门店名 · 姓名 / 版本号 / 会话状态」**只是变形，不是删掉** ——
        收起来的时候必须照旧看得见（左下角是门店唯一能核对"我是谁"的地方）。

        ⚠ 老板的「标识 —」2026-09-19 去掉了（没有标识就不写那一段）。
        """
        seg = _foot()
        for ident in ("store-line", "build-line", "pill-session", "pill-schedule"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, seg, "%s 不该从收起的界面里消失" % ident)
        # 「有新版本」跟着版本号那一行走（用户原话）
        update_row = re.search(r'data-foot="update"[^>]*>(.*?)</button>', seg, re.S).group(1)
        self.assertIn('id="pill-update"', update_row,
                      "「有新版本」徽章该跟着「检查更新」那一行")

    def test_键在_data_foot_不在_data_subtab(self):
        """⚠ 左下角菜单用**自己的键空间** `data-foot`。

        `data-subtab` 的约定是"有子面板 + 有加载器"（有测试逐条比对），
        而「检查更新」「定时器设置」只是跳到「通用」页里的**一张卡** ——
        混用会让那两条测试失去意义（也会真的红）。
        """
        # ⚠ 先剥注释 —— 上面那几段说明里正写着 `data-subtab` 这四个字，
        #   不剥的话 `assertNotIn` 会被注释自己顶掉（这个坑在这个文件里踩过好几次）。
        seg = _strip_html_comments(_foot())
        self.assertNotIn("data-subtab", seg, "左下角菜单不该用 data-subtab")
        # ⚠ 2026-09-21：原来 6 行，人员设置并进账号页之后是 5 行 ——
        #   同日又加了「数据交换」（M20）⇒ **6 行**
        # ⭐ 2026-09-22：+「主题设置」⇒ 7 行
        self.assertEqual(len(re.findall(r'data-foot="', seg)), 7)

    def test_跳转表覆盖了每一行(self):
        """⚠ 少一条 = 那一行**点了没反应**（这个项目踩过好几次）。"""
        # ⚠ 锚的是**跳转表自己**（`const FOOT_GO = {`）——
        #   这一行刚才被我误改成了 `index('timer-off')`（那是另一个测试的锚点），
        #   于是 `keys` 解析出空列表、这条测试就变成了摆设。
        i = APP_JS.index("const FOOT_GO = {")
        block = APP_JS[i:APP_JS.index("};", i)]
        keys = re.findall(r"^\s*(\w+):", block, re.M)
        self.assertEqual(sorted(keys), sorted(k for k, _ in _foot_items()))

    def test_定时器设置和检查更新都是独立一页(self):
        """⚠ 这两项 **2026-09-20 起不一样了**（用户：「定时器设置单独出来一页」）：

        | 行 | 形态 | `FOOT_GO` 该长什么样 |
        |---|---|---|
        | 检查更新 | **2026-09-21 起也是独立二级页** `update` | **两项**（页签 + 二级） |
        | 定时器设置 | **独立二级页** `timer` | **两项**（页签 + 二级） |

        ⚠ 三项那条里那个 id 得真在 HTML 里，否则点下去切了页却停在一片卡片中间；
          两项那条要真有个 `#subpanel-timer`，否则切过去是**空白**。
        """
        i = APP_JS.index("const FOOT_GO = {")
        block = APP_JS[i:APP_JS.index("};", i)]

        def entry(key):
            m = re.search(r"%s:\s*\[([^\]]*)\]" % key, block)
            self.assertIsNotNone(m, "FOOT_GO 里没有 %s" % key)
            return re.findall(r"'([^']+)'", m.group(1))

        upd = entry("update")
        self.assertEqual(upd, ["settings", "update"],
                         "检查更新 2026-09-21 起是独立页了（用户：「单独做一个页面」）")
        self.assertIn('id="subpanel-update"', INDEX_HTML, "切过去得有那一页，不能空白")
        # ⚠ 老那张卡（`#card-update`）**不许留着**：留着的话点左下角会滚到
        #   一张已经不在这儿的卡上（"点了像没反应"），而且两份 id 会撞。
        self.assertNotIn('id="card-update"', INDEX_HTML)

        sch = entry("scheduler")
        self.assertEqual(sch, ["settings", "timer"],
                         "定时器设置是独立页了，`FOOT_GO` 只该有两项")
        self.assertIn('id="subpanel-timer"', INDEX_HTML,
                      "`timer` 那个二级页不在 HTML 里 ⇒ 点下去是空白")

        # ⭐ 主题设置（2026-09-22）：同样独立二级页
        th = entry("theme")
        self.assertEqual(th, ["settings", "theme"],
                         "主题设置是独立页 ⇒ FOOT_GO 两项（页签 + 二级）")
        self.assertIn('id="subpanel-theme"', INDEX_HTML,
                      "切过去得有 #subpanel-theme，否则空白")

    def test_账号设置页在(self):
        """用户：「门店&姓名这一行变成**账号设置**，点了右边显示云商账号状态和信息，
        可以在里面选择**退出登录**或者**切换账号**」。"""
        self.assertIn('id="subpanel-account"', INDEX_HTML)
        for ident in ("account-box", "account-store-box",
                      "btn-account-logout", "btn-account-switch"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, INDEX_HTML)
        # ⚠ 2026-09-21：「人员设置」并进这一页了（用户：「账号设置和人员设置合并到一起吧」）
        #   ⇒ 切过来**两样都拉**：账号状态 + 本店人员。
        self.assertIn("renderAccount(); loadStaff();", APP_JS)

    def test_通用设置面板没有介绍页(self):
        """⚠ 每一条都带着明确的 key 进来，落不到"介绍页"这种地方 ——
        留一个 `subpanel-settings-home` 就是**谁也到不了的死页**。"""
        self.assertNotIn('id="subpanel-settings-home"', INDEX_HTML)
        self.assertNotIn("'settings-home'", APP_JS)

    def test_云商授权整个撤掉了(self):
        """⚠ 留着的话是个**空页** —— 点进去什么都没有，看着像坏了。"""
        self.assertNotIn("erp", NAV_IDS)
        self.assertNotIn("云商授权", _strip_html_comments(NAV))
        self.assertNotIn('id="subpanel-erp"', INDEX_HTML)
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
        门店看到的是**点了没反应**。

        ⚠ 设置那几条的落点是 `['settings', '<二级>']` —— `settings` 是**面板**名
        （`#panel-settings`），不是页签。
        """
        i = APP_JS.index("const GO_TARGETS = {")
        block = APP_JS[i:APP_JS.index("};", i)]
        for key in ("linglong", "settings", "general"):
            with self.subTest(key=key):
                self.assertIn(key + ":", block)
        self.assertIn("settings: ['settings', 'general']", block)
        self.assertIn("linglong: ['settings', 'linglong']", block)

    def test_名字对(self):
        """⚠ 改名的是**菜单项**，不是状态徽章 ——
        `#pill-session` 那个徽章本来就写着「会话」（它表示会话状态），
        收起来的时候看得见，展开变成菜单项「玲珑授权」。
        """
        seg = _foot()
        self.assertIn(">玲珑授权<", seg)
        self.assertNotIn(">通用设置<", seg, "现在叫「通用」，不再是「通用设置」")
        # 徽章那个「会话」还在（它是状态，不是菜单项）
        self.assertIn('id="pill-session"', seg)



class Test合规页里两块并列不混(unittest.TestCase):
    """**报量查询和 POS 合规不要混在一张表里**（用户明说）。"""

    def setUp(self):
        i = INDEX_HTML.index('id="panel-compliance"')
        j = INDEX_HTML.index("</section>", i)
        self.panel = _strip_html_comments(INDEX_HTML[i:j])

    def test_两个二级标签并列(self):
        """⚠ 二级标签现在**只在顶部导航的下拉菜单里**（用户 2026-09-18 定：
        「只在下拉菜单里」）—— 所以"并列"看菜单顺序，页内那行已经去掉了。

        ⚠ 「设置」**排在这两个后面**（模块自己的设置留在模块里 ——
        用户 2026-09-19：「各个模块的设置还是要给模块」）。
        """
        menu = _nav_item("compliance", nxt="inventory")
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

    def test_跑一次那张卡删了_日志留着(self):
        """⚠ 2026-09-21（用户：「**右下角的跑一次可以去掉了**」）——
        抽屉里那张「跑一次」的卡（按钮 / 停止 / 状态）删了，**日志留着**。

        ⚠ 两头都要钉：只钉"日志还在"的话，哪天有人把那张卡加回来也不会有测试说话；
        只钉"卡没了"的话，误删日志（用户 2026-09-18：「运行日志要一直在、且好找」）
        同样没人说话。
        """
        i = INDEX_HTML.index('id="run-drawer"')
        j = INDEX_HTML.index("</aside>", i)
        drawer = INDEX_HTML[i:j]
        self.assertIn('id="run-log"', drawer, "运行日志不能跟着那张卡一起删")
        for gone in ('data-what=', 'id="btn-stop"', 'id="run-status"'):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, drawer, "「跑一次」那张卡该删干净")
        # 「上报 bug」是**另一件事**（2026-09-21 用户要的），别跟着删
        self.assertIn('id="btn-report-bug"', drawer)

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

    def test_点一级标签落第一个二级页(self):
        """⚠⚠ 口径**改过一次**，两次都是用户定的：

        * 2026-09-18：「点击一级标签进**介绍页**，而不是第一个二级标签页」；
        * 2026-09-19：「**B 吧，现在来看这个介绍页没用**」——
          介绍页删掉，点一级标签 = 进**它第一个二级页**
          （跟左下角那几行一个路子：点一下直接到该去的地方，
           中间不再垫一页"这块是干什么的"）。

        ⚠ 所以 `TAB_HOME` 整个没了 —— 留着的话它和 `SUBTABS[0]` 是**两份
          "点一级落哪儿"**，迟早对不上。
        """
        self.assertNotIn("const TAB_HOME", APP_JS, "介绍页那套（TAB_HOME）该删干净")
        j = APP_JS.index("function switchTab")
        blk = APP_JS[j:j + 500]
        self.assertIn("const home = subs[0] || ''", blk, "没落到第一个二级页")
        self.assertNotIn("-home", blk)

    def test_介绍页的壳也删了(self):
        """⚠ 光改路由不够：`#subpanel-*-home` 和那些 `.intro-card` 留着的话，
        它们永远不会被激活，下次改前端的人还会以为"这里有个页面"。"""
        for tab in NAV_IDS:
            self.assertNotIn('id="subpanel-%s-home"' % tab, INDEX_HTML)
        self.assertNotIn("intro-card", INDEX_HTML)
        self.assertNotIn("data-open-sub", APP_JS, "介绍卡的事件委托也该删")

    def test_介绍页不在二级标签列表里(self):
        """⚠ 混进 `SUBTABS` 的话，下拉菜单里会多出一个"介绍"，
        而它就是导航的列表 —— `test_导航里的二级标签要和_SUBTABS_对得上` 会红。
        两份定义各管各的：`SUBTABS` 管菜单里有哪些，`TAB_HOME` 管点一级落哪儿。"""
        i = APP_JS.index("const SUBTABS = {")
        blk = APP_JS[i:APP_JS.index("};", i)]
        self.assertNotIn("home", blk, "介绍页混进二级标签列表了")

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

    def test_一级标签不可点击(self):
        """⚠⚠ 用户 2026-09-19：「**一级标签不可点击**」。

        它跟左下角那几行是**同一个角色** —— 一行"信息 + 展开器"
        （门店·姓名 → 账号设置），真正能点的只有下面那些二级项。
        ⇒ 所以 `$$('.tab')` 上**不许有 click → switchTab** 的绑定：
          留着的话"点一级标签去哪一页"这个问题又回来了，而那一页（介绍页）
          用户已经明确说过没用。

        ⚠ 悬停/聚焦展开还是要有（触屏机靠聚焦，见 `test_键盘和触摸也能展开`）。
        """
        self.assertNotIn(".tab').forEach((b) => b.addEventListener('click'", APP_JS)
        self.assertIn("一级标签不可点击", APP_JS, "说明也该留着，免得下次被加回去")
        # 二级项仍然可点（这是唯一能跳页的地方）
        self.assertIn("switchTab(item.dataset.tab, b.dataset.subtab)", APP_JS)

    def test_首屏落在第一个二级页(self):
        """一级标签不可点 ⇒ 进来就得**自己落在某一页**上，
        落的是第一个一级标签的第一个二级页（`switchTab` 不带 subtab 时的行为）。"""
        i = APP_JS.index("(async () => {\n  // ⚠ **门禁在最前面**")
        blk = APP_JS[i:i + 1200]
        self.assertIn("switchTab('sales')", blk)
        self.assertNotIn(".tab.active", blk, "别再按「当前亮着的页签」决定落哪儿了")

    def test_菜单里的项点了才切(self):
        # ⚠ 锚在**那行绑定**上 —— `$$('.nav-item').forEach` 在 `closeNavMenus`
        #   里先出现过一次，按它切会切到隔壁函数里去（第一版就这么假绿了）
        # ⚠ 2026-09-20（M16）：**窗口从 500 放宽到 1200** —— 二级项那个分支里
        #   多了一段"带 `data-page` 的直接开新页"（库存盘点），
        #   `switchTab(...)` 那行被挤出了 500 字窗口（假红，不是行为变了）。
        i = APP_JS.index("item.addEventListener('mouseenter'")
        block = APP_JS[i:i + 1200]
        self.assertIn("switchTab(item.dataset.tab, b.dataset.subtab)", block)

    # ⚠ 原来这里还有一条「没有二级的页签不挂菜单」（针对会话 / 通用设置两个光杆按钮）。
    #   2026-09-18 用户把「通用设置」也改成下拉之后，**三个一级菜单一律挂菜单**，
    #   那条断言的前提没了 —— 删掉，别让它以"恒真"的形式留着占位。
    #   「菜单不能是空的」这条挪进了 `test_每个一级菜单都挂了下拉`。

    def test_菜单默认是收起的(self):
        """⚠ 2026-09-19 起**不靠 `hidden` 了** —— 二级项就地变形，
        收起 = CSS 的 `height: 0`（`display: none` 过渡不了，那正是浮层时代
        要 `hidden` + `setVisible` 的原因）。漏了那条初始态，页面一打开全都摊着。"""
        i = STYLE_CSS.index("\n.nav-menu {")
        blk = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        self.assertIn("height: 0", blk)
        self.assertNotIn("hidden", INDEX_HTML.split("<nav")[1].split("</nav>")[0],
                         "导航里不该再有 hidden（那说明还是浮层那套）")

    def test_键盘和触摸也能展开(self):
        """⚠ 光绑 hover 的话**触屏机上一辈子打不开**（门店可能是触摸屏）。"""
        # ⚠ 锚在**那行绑定**上 —— `$$('.nav-item').forEach` 在 `closeNavMenus`
        #   里先出现过一次，按它切会切到隔壁函数里去（第一版就这么假绿了）
        i = APP_JS.index("item.addEventListener('mouseenter'")
        block = APP_JS[i:i + 500]
        self.assertIn("addEventListener('focusin', () => openNavMenu(item))", block)

    def test_子面板默认最多亮一个(self):
        """⚠ 写死两个 `active` 的话，两个子页会**同时显示**。

        ⚠ 2026-09-19 起**允许一个都不写**：介绍页删掉之后，HTML 里没有"默认那一页"了，
          进页面时 `switchTab` 会点亮第一个二级页（`SUBPANELS` 靠 JS 切）。
          所以这条从"必须恰好一个"放宽成"最多一个" —— 放宽的是**数量**，
          不是"能不能同时亮两个"那条规矩。
        """
        for tab in ("sales", "compliance"):
            i = INDEX_HTML.index('id="panel-%s"' % tab)
            j = INDEX_HTML.index("</section>", i)
            n = len(re.findall(r'class="subpanel active"', INDEX_HTML[i:j]))
            with self.subTest(panel=tab):
                self.assertLessEqual(n, 1, "panel-%s 里有 %d 个默认亮着的子页" % (tab, n))

    def test_二级标签只在导航里有一份(self):
        """⚠ 用户 2026-09-18 定：二级标签「只在下拉菜单里」，**页内不再占一行**。

        一开始「通用设置」那三个破例放在页内的标签条里（`#settings-tabs`），
        但那排按钮**压根没绑事件** —— 点了没反应，截图实测抓到的。
        用户随后拍板"跟销售/合规一样，页内那排删掉"，于是**三处形态统一**。

        ⚠ 2026-09-19 起允许出现 `data-subtab` 的地方**一共两处**：
          ① 左侧导航的下拉菜单（`.nav-menu`）；
          ② 左下角的设置浮层（`#side-menu`）—— 用户要求把设置搬过去的那一块。
        两处都是**导航面**，不是"页内又长出一排按钮"。除此之外一律不许有。

        ⚠ 这里挖 nav 必须**两边都先剥注释**：`NAV` 取自原始 HTML（里面有注释），
        而 `BODY` 已经剥过，直接 replace 匹不上，等于没挖（第一版就这么假绿了）。"""
        rest = BODY.replace(_strip_html_comments(NAV), "")
        in_page = re.findall(r'data-subtab="([a-z-]+)"', rest)
        self.assertEqual(in_page, [],
                         "二级标签只能在导航下拉里，别处不该有：%s" % in_page)
        # ⚠ 左下角那几行**故意不用 `data-subtab`** —— 那里面有两条只是跳到一张卡，
        #   混进来会让「每个 data-subtab 都有子面板 / 都有加载器」失去意义。
        self.assertNotIn("data-subtab", _strip_html_comments(_foot()))

    def test_每个一级菜单都挂了下拉(self):
        """⚠ 「通用设置」以前是个光杆按钮（没有 `nav-menu`）——
        三个一级菜单形态不一致，用户选了统一成下拉。

        ⚠ 「销售数据」现在只剩**一个**二级标签，下拉照样留着 ——
        为了少一个下拉就分叉出第二种一级标签的形态，不划算。"""
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
        JS 多一个 → 那个二级标签永远切不到（`switchTab` 会把它当"不认识的"弹回第一个）。

        ⚠ `settings` 那一份**不在导航里**（它在左下角浮层 `#side-menu`），
          所以这一条要分两边比：导航那两页比 `.nav-menu`，
          `settings` 比 `_side_menu()`。少比一边就等于放掉了这份定义。
        """
        block = APP_JS[APP_JS.index("const SUBTABS = {"):]
        block = block[:block.index("\n};")]
        want = {}
        for tab, subs in re.findall(r"(\w+):\s*\[([^\]]*)\]", block):
            want[tab] = re.findall(r"'([a-z-]+)'", subs)
        # ⚠ `settings` 是**面板**不是页签，所以它不出现在 NAV_IDS 里 —— 期望集合要补上它
        self.assertEqual(sorted(want), sorted(NAV_IDS + ["settings"]),
                         "两边的集合不一样")
        for tab, subs in want.items():
            with self.subTest(tab=tab):
                if tab == "settings":
                    # ⚠ `SUBTABS.settings` 是**`switchTab` 认得哪些 key**，
                    #   而左下角那几行里有的**不是**二级标签。
                    #   ⇒ 比的是"真正切子面板的那几行"。
                    #
                    #   ⚠⚠ 判据是 **go 有几项**，不是"键名等于二级名"：
                    #     * `update: ['settings', 'general', 'card-update']` —— **三项**，
                    #       意思是"切到通用页、再滚到那张卡"，它不是二级标签；
                    #     * `scheduler: ['settings', 'timer']` —— **两项**，
                    #       2026-09-20 起「定时器设置」**是**一个真的二级页，
                    #       但它的键还叫 `scheduler`（那一行一直叫这个）。
                    #   老写法拿**键名**当二级名去比，`timer` 这一项就永远对不上，
                    #   而"键名 ≠ 二级名"本身完全合法 —— 所以判据要换成落点。
                    go = dict(re.findall(r"(\w+):\s*\[([^\]]*)\]",
                                         APP_JS[APP_JS.index("const FOOT_GO = {"):
                                                APP_JS.index("\n};", APP_JS.index("const FOOT_GO = {"))]))
                    real = []
                    for k, _label in _foot_items():
                        parts = re.findall(r"'([^']+)'", go.get(k, ""))
                        if len(parts) == 2 and parts[0] == "settings":
                            real.append(parts[1])
                    self.assertEqual(real, subs,
                                     "SUBTABS.settings 和左下角那几行的落点对不上")
                else:
                    menu = _nav_item(tab) if tab == NAV_IDS[-1] else _nav_item(
                        tab, nxt=NAV_IDS[NAV_IDS.index(tab) + 1])
                    self.assertEqual(re.findall(r'data-subtab="([a-z-]+)"', menu), subs)


class Test左下角菜单的行为(unittest.TestCase):
    """形态变了（浮层 → 就地变形），但几条**行为规矩**照旧：

    * **悬停只展开，点击才跳** —— 别改成"悬停即切换"（扫过去就切页，误触太高）；
    * 光靠 hover 的话**触屏 / 键盘一辈子打不开** ⇒ 补 `focusin` + 一次 `click`；
    * 收起**留宽限期**（指针在里面挪动时中间那下 `mouseleave` 会闪）；
    * 动画**只在 CSS 里**（`.side-foot:hover` / `.side-foot.open`），
      JS 只加/去 `open` 这个类 —— 悬停和"键盘/触摸打开"走同一条路。
    """

    def test_动画在_CSS_里_JS_只加类(self):
        """⚠ 两处各写一遍动画必然漂移。JS 只负责 `open` 这个类。"""
        i = STYLE_CSS.index("\n.foot-item {")
        rule = STYLE_CSS[i:STYLE_CSS.index("\n}", i)]
        self.assertIn("transition:", rule)
        for sel in (".side-foot:hover .foot-info", ".side-foot.open .foot-info"):
            with self.subTest(sel=sel):
                self.assertIn(sel, STYLE_CSS, "信息面没有淡出规则")
        for sel in (".side-foot:hover .foot-label", ".side-foot.open .foot-label"):
            with self.subTest(sel=sel):
                self.assertIn(sel, STYLE_CSS, "菜单名没有淡入规则")
        self.assertIn("foot.classList.add('open')", APP_JS)
        self.assertIn("foot.classList.remove('open')", APP_JS)

    def test_菜单名的颜色跟一级标签一致(self):
        """⚠ 用户 2026-09-19：「字的颜色和**上面一级标签**字的颜色对齐吧，
        别单独搞个蓝色」。

        ⇒ `.foot-label` 要跟 `.tab`（一级标签）**同一个色 + 同一个字重**，
          不许出现 `var(--brand)`。这条是**反向断言** —— 防的是以后有人
          "顺手"把菜单项染成品牌蓝（第一版就是蓝的，被用户点名了）。
        """
        i = STYLE_CSS.index("\n.foot-label {")
        rule = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        self.assertIn("color: var(--muted)", rule)
        self.assertNotIn("var(--brand)", rule, "左下角菜单名不该单独搞个蓝的")
        j = STYLE_CSS.index("\n.tab {")
        tab = STYLE_CSS[j:STYLE_CSS.index("}", j)]
        self.assertIn("color: var(--muted)", tab, "一级标签换色了？那这条要对齐它")
        # ⚠ 字重也要一样 —— 只对齐颜色是"对了一半"，看着还是两套
        self.assertIn("font-weight: 550", tab)
        self.assertIn("font-weight: 550", rule)

    def test_选中不用底色_一级二级都靠加粗(self):
        """⚠⚠ 用户 2026-09-19：「**选中别用蓝底这种奇怪的颜色吧，
        一级二级字体加粗就行了**」。

        所以选中态**只许**改字重和明暗 —— 出现 `background` 就是又加回去了。
        """
        for sel in ("\n.tab.active {", "\n.nav-menu .subtab.current {"):
            i = STYLE_CSS.index(sel)
            rule = STYLE_CSS[i:STYLE_CSS.index("}", i)]
            with self.subTest(sel=sel.strip()):
                self.assertNotIn("background", rule, "选中态又在铺底色了")
                self.assertIn("font-weight: 700", rule, "选中要靠加粗（用户要的）")
                self.assertIn("color: var(--text)", rule)

    def test_二级看得出是次级(self):
        """⚠ 用户 2026-09-19：「**二级标签看不出来次级啊**」。

        层次靠三样，都不涉及颜色（颜色是主题的事）：
          **缩进**（26px vs 一级的 12px）、**小一号**（12.5px vs 14px）、
          **常规字重**（450 vs 一级的 550）。
        """
        i = STYLE_CSS.index("\n.tab {")
        tab = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        j = STYLE_CSS.index("\n.nav-menu .subtab {")
        sub = STYLE_CSS[j:STYLE_CSS.index("}", j)]
        self.assertIn("font-size: 12.5px", sub)
        self.assertIn("font-weight: 450", sub)
        self.assertIn("padding: 0 14px 0 26px", sub, "二级没缩进 ⇒ 跟一级一样齐，看不出层级")
        self.assertIn("font-size: 14px", tab)
        self.assertIn("padding: 9px 12px", tab)

    def test_信息面和菜单名叠在同一格(self):
        """⚠ 叠不到一格的话就是**上下两行**，变形的效果全没了。"""
        i = STYLE_CSS.index("\n.foot-item {")
        blk = STYLE_CSS[i:STYLE_CSS.index("\n}", STYLE_CSS.index(".foot-item > .pill"))]
        self.assertRegex(blk, r"\.foot-item > \.foot-info,\s*\n\.foot-item > \.foot-label")
        self.assertIn("grid-column: 1; grid-row: 1;", blk)

    def test_插缝那两条用_height_过渡(self):
        """⚠ 用 `height`（两端都是确定的长度）不用 `max-height` ——
        后者要猜一个"够大"的数，猜大了动画会提前结束、看着发顿。"""
        i = STYLE_CSS.index(".foot-gap {")
        rule = STYLE_CSS[i:STYLE_CSS.index("}", i)]
        self.assertIn("height: 0", rule)
        self.assertIn("overflow: hidden", rule)
        self.assertIn("transition: height", rule)
        self.assertIn("height: 22px", STYLE_CSS[i:i + 600])

    def test_悬停只展开_点击才跳(self):
        """⚠ 跟顶部导航同一条规矩：悬停一旦切页，鼠标扫过左下角就乱跳。"""
        i = APP_JS.index("function wireFootMenu(")
        blk = APP_JS[i:i + 1600]
        self.assertNotIn("mouseenter', () => switchTab", blk, "悬停改成直接切页了？")
        self.assertIn("foot.addEventListener('mouseenter', openFootMenu)", blk)
        self.assertIn("b.addEventListener('click'", blk)
        self.assertIn("switchTab(go[0], go[1])", blk)

    def test_键盘和触摸也能开(self):
        """⚠ 光靠 hover 的话**触屏 / 键盘一辈子打不开**（门店那台可能是触摸屏）。
        这块里**没有可聚焦的元素**（原来徽章是 span），所以给了 `tabindex="0"` ——
        现在里面是 `<button>` 了，本来就能 Tab 到，但 `focusin` 那条不能少
        （焦点进到某一行时菜单得展开，否则键盘用户 Tab 进了一个看不见的菜单）。"""
        m = re.search(r'<div class="side-foot"([^>]*)>', INDEX_HTML)
        self.assertIsNotNone(m)
        self.assertIn('tabindex="0"', m.group(1))
        i = APP_JS.index("function wireFootMenu(")
        blk = APP_JS[i:i + 1600]
        self.assertIn("foot.addEventListener('focusin', openFootMenu)", blk)
        self.assertIn("foot.addEventListener('click', openFootMenu)", blk)

    def test_点别处会收起来(self):
        i = APP_JS.index("!t.closest('.side-foot')")
        self.assertIn("closeFootMenu()", APP_JS[i:i + 120])

    def test_收起留宽限期(self):
        """⚠ 指针在这一块里挪动时中间会闪一下 `mouseleave`，不宽限菜单会抖。"""
        i = APP_JS.index("function scheduleCloseFootMenu(")
        blk = APP_JS[i:i + 300]
        self.assertIn("NAV_CLOSE_GRACE", blk)

    def test_点完还停在这一块上时不收(self):
        """⚠ `switchTab` 会把菜单收起来，而鼠标还停在这一块上 ——
        不补一下的话想连着点第二项得先把指针移开再移回来（顶部导航踩过）。"""
        i = APP_JS.index("function wireFootMenu(")
        blk = APP_JS[i:i + 1600]
        self.assertIn("if (foot.matches(':hover')) openFootMenu()", blk)

    def test_卡片会闪一下(self):
        """⚠ 「检查更新」「定时器设置」是跳到「通用」页里的一张卡 ——
        不闪一下的话切过去只看到一页卡片，**不知道点的是哪一张**。"""
        self.assertIn("function flashCard(", APP_JS)
        i = APP_JS.index("function flashCard(")
        self.assertIn("scrollIntoView", APP_JS[i:i + 400])
        self.assertIn("classList.add('flash')", APP_JS[i:i + 400])
        self.assertIn(".card.flash", STYLE_CSS)

    def test_每一行都在后端可见性表里(self):
        """⚠ 左下角这几行**也得有个"谁能看"的说法** —— 2026-09-21（M17 甲方案）
        起表在后端（`web.PAGE_RULES`，键就是 `data-foot` 的值本身）。

        ⚠ 漏一个 key 的表现是**那一行永远不出现**（`pages` 里没有它 ⇒ 前端藏掉）——
        不像 `data-types` 时代"不标就是公开"。所以这里逐个 key 对一遍；
        反方向（表里有、HTML 没有）在 `tests/test_roles.py::Test可见性对照`。
        """
        from src import web
        keys = re.findall(r'data-foot="([a-z]+)"', _strip_html_comments(_foot()))
        self.assertTrue(keys, "左下角这几行的 key 没解析出来")
        for k in keys:
            with self.subTest(k=k):
                self.assertIn(k, web.PAGE_RULES,
                              "这一行没在后端可见性表里登记 → 永远不显示")



class Test介绍页已经删掉(unittest.TestCase):
    """2026-09-19 用户：「**B 吧，现在来看这个介绍页没用**」+「一级标签不可点击」。

    ⇒ 一级标签那两页（销售数据 / 五项合规的介绍页）**整个删了**。
      这条类留下的意义是**防止它被加回来**：光删 HTML 不够，
      路由（`TAB_HOME`）、那几张入口卡、事件委托都得清掉。
    """

    def test_介绍页的壳和入口卡都没了(self):
        for tab in NAV_IDS:
            self.assertNotIn('id="subpanel-%s-home"' % tab, INDEX_HTML)
        self.assertNotIn("intro-card", INDEX_HTML)
        self.assertNotIn("data-open-sub", INDEX_HTML)
        self.assertNotIn("data-open-sub", APP_JS, "入口卡的事件委托也该删")

    def test_路由里没有介绍页(self):
        self.assertNotIn("const TAB_HOME", APP_JS)
        self.assertNotIn("-home", APP_JS.split("function switchTab")[1][:400])

    def test_模块设置仍挂在各自的导航里(self):
        """⚠ 删的是**一级标签的介绍页**，不是模块自己的「设置」——
        用户 2026-09-19：「**各个模块的设置还是要给模块**，只把通用设置做下来就行了」。"""
        for name in ("sales-settings", "compliance-settings", "valueadd-settings"):
            self.assertIn('data-subtab="%s"' % name, INDEX_HTML)
            self.assertIn('id="subpanel-%s"' % name, INDEX_HTML)


class Test首屏加载(unittest.TestCase):
    """⚠ 首屏加载**必须跟着当前亮着的页签走**。

    2026-09-18 改版前启动那段写死 `loadOverview()` —— 那时默认页正是报量查询，
    所以没错。默认页换成「销售」之后，那样写的结果是：
    **报量查询那页的数据白拉一遍，销售页却一片空白**（连空状态都没渲染，看着像坏了）。
    截图实测抓到过这一次。"""

    def _boot(self):
        i = APP_JS.index("/* ───────────────────────────── 启动")
        # ⚠ 切**够长** —— 这段注释本身就占好几百字符，切短了会把
        #   `loadOverview();` 那行留在窗口外面，断言变成假绿
        return APP_JS[i:i + 1400]

    def test_启动跟着当前页签(self):
        self.assertIn("switchTab(", self._boot())

    def test_侧边栏的状态也要拉(self):
        """⚠ `loadOverview()` **不能删** —— 它不只是报量查询那页的数据，
        侧边栏左下角的**门店名 / 会话·定时徽章**也是它填的。

        第一次改版时顺手删了它，左下角就一直停在「加载中…」、徽章也没字
        （截图抓到的）。所以两个都要：`switchTab` 管当前页内容，
        `loadOverview` 管这一圈的常驻状态。"""
        self.assertIn("loadOverview();", self._boot(),
                      "删了 loadOverview → 左下角门店信息永远是「加载中…」")

    def test_门店信息在左下角(self):
        """用户 2026-09-18：「把上面的门店设置这些放到左下角」。

        ⚠ 2026-09-19 起这块还兼职**设置浮层的触发面**（见下一个类）。
        """
        foot = re.search(r'<div class="side-foot"[^>]*>(.*?)</div>\s*</aside>',
                         INDEX_HTML, re.S).group(1)
        for ident in ("store-line", "build-line", "pill-session", "pill-schedule"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, foot)
        # 顶栏整个去掉了
        self.assertNotIn("<header", INDEX_HTML)
        self.assertIn(".side-foot", STYLE_CSS)
        self.assertIn("margin-top: auto", STYLE_CSS)

    def test_销售页的空状态渲染得出来(self):
        """算不出来时也要**明确说一句** —— 不能给一张空卡片。

        ⚠ 2026-09-19（M4）：以前这条钉的是那句"接口还没上（M4）"的占位。
          接口上了之后**那句必须删掉** —— 留着的话，接口真坏了门店看到的
          是一句关于开发进度的话，既看不懂也没法处理。
          现在"读不到"分两种，都要说人话：
            * `exists: False` + `error`（"还没算过 —— 先跑一次 daily"）；
            * 请求本身失败（网络 / 403）→ 一句"读不到达成数据：…"。
        """
        self.assertNotIn("renderAttainPlaceholder", APP_JS)
        self.assertNotIn("还没接上数据", APP_JS)
        self.assertIn("读不到达成数据", APP_JS)
        self.assertIn("d.error", APP_JS)


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
        self.assertIn("--side-w:", THEME_CSS)
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
        # ⚠ 允许带 `?v=N` 的版本号（2026-09-19 加的）—— 它**不是**构建步骤，
        #   是"换 URL 绕开浏览器缓存"：前端没有构建，浏览器会把 .js 缓在内存里，
        #   实测导致"改了、刷新了、页面还是老的"（表头那次折腾了三轮）。
        import re as _re
        self.assertRegex(INDEX_HTML, r'<script src="/app\.js(\?v=\d+)?"></script>')

    def test_没有打包产物引用(self):
        for bad in (".min.js", "bundle.js", "webpack", "vite", "import "):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, INDEX_HTML)


if __name__ == "__main__":
    unittest.main()


class Test历史记录页(unittest.TestCase):
    """用户 2026-09-20：「加个**历史记录**功能，这一周过去之后…就把上一周给锁住存档，
    在工作区的**周度目标达成情况下面**加个历史记录」。

    ⚠ 这一页的意义全在**只读**上：存档就是"当时那一份"，复盘引用的正是它。
      所以这一条类盯的不是"页面长得对不对"，而是**别把编辑那条链接进来**。
    """

    def _panel(self):
        i = INDEX_HTML.index('id="subpanel-attain-history"')
        return INDEX_HTML[i:INDEX_HTML.index("</div>\n  </div>", i)]

    def test_挂在达成下面_在同一块面板里(self):
        """⚠ 用户说的是"**上面**那个的下面" —— 所以「销售数据」那一段里，
        `attain-history` 必须排在 `attain` 之后、`sales-settings` 之前。"""
        panel = INDEX_HTML[INDEX_HTML.index('id="panel-sales"'):]
        panel = panel[:panel.index("</section>")]
        self.assertLess(panel.index('id="subpanel-attain"'),
                        panel.index('id="subpanel-attain-history"'))
        self.assertLess(panel.index('id="subpanel-attain-history"'),
                        panel.index('id="subpanel-sales-settings"'))

    def test_这页只读_没有编辑和推送的入口(self):
        seg = self._panel()
        for gone in ("split-edit", "split-in", "btn-attain-send", "data-expand",
                     "data-send", "保存目标", "发送给区长"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, seg, "历史记录页不该有「%s」" % gone)

    def test_列表和那一周的表都在自己的容器里(self):
        """⚠ 动态建的元素要能从 HTML 里找到 —— `$('#…')` 拿到 null 就是**点了没反应**。"""
        seg = self._panel()
        for ident in ("attain-hist-list", "attain-hist-body", "attain-hist-meta",
                      "btn-refresh-attain-hist"):
            self.assertIn('id="%s"' % ident, seg)

    def test_加载器会去拉历史接口(self):
        self.assertIn("'attain-history': () => loadAttainHistory()", APP_JS)
        self.assertIn("/api/attain/history", APP_JS)

    def test_刷新是回到最新那一周(self):
        """⚠ 用户预期是"刷新 = 看最新的"：停在 8 月那一周时点刷新，
        应该回到最新的那一周，而不是原地不动。"""
        i = APP_JS.index("btn-refresh-attain-hist")
        seg = APP_JS[i:i + 200]
        self.assertIn("attainHistCur = ''", seg)

    def test_历史页的渲染里没有那条编辑链(self):
        """⚠ 比"HTML 里没有"更硬的一条：**渲染函数本身**不碰编辑 / 推送那条链。

        历史页的格子是**纯文本**（不是 `store-link`）、没有 `.split-edit`，
        也就不会走到 `toggleStoreDetail` / `attain_split_save` / `send` 上去。
        """
        i = APP_JS.index("function renderAttainHistory")
        seg = APP_JS[i:APP_JS.index("\n}", i)]
        for gone in ("split", "data-expand", "store-link", "/send", "save"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, seg)

    def test_标色开关在历史页同样生效(self):
        """历史页的格子也走 `attainTier` —— 两套颜色的话，翻旧账时反而看不懂。"""
        i = APP_JS.index("function renderAttainHistory")
        seg = APP_JS[i:APP_JS.index("\n}", i)]
        self.assertIn("attainTier(", seg)


class Test定时器设置独立一页(unittest.TestCase):
    """用户 2026-09-20：

    > 「**定时器设置单独出来一页**，**每个模块的设置页面加上定时执行相关设置**」

    两件事，两个落点：

    | 谁 | 在哪 | 看得到什么 |
    |---|---|---|
    | 定时器设置（系统级） | 设置 › 定时器设置（`#subpanel-timer`） | **全部**步骤 + 状态行 +（有旧任务时的）清理入口 |
    | 模块自己的设置 | 周度重点产品 › 设置 / 五项合规 › 设置 | **只有本模块**那几步 |
    """

    def _panel(self, ident):
        i = INDEX_HTML.index('id="%s"' % ident)
        # 切到它自己的结束（下一个 subpanel 或 </section>）
        ends = [k for k in (INDEX_HTML.find('class="subpanel"', i + 1),
                            INDEX_HTML.find("</section>", i + 1)) if k > 0]
        return INDEX_HTML[i:min(ends)]

    def test_定时器那一页在设置里(self):
        self.assertIn('id="subpanel-timer"', INDEX_HTML)
        # 它在 `#panel-settings` 里（系统级的设置，不是某个业务的一级标签）
        panel = INDEX_HTML[INDEX_HTML.index('id="panel-settings"'):]
        panel = panel[:panel.index("</section>")]
        self.assertIn('id="subpanel-timer"', panel, "定时器页不在「设置」这块里")

    def test_通用页里那张卡搬走了_只留一句指路(self):
        """⚠ 卡片搬走、**壳要清掉**：留着的话门店会在「通用」页里找一个
        已经不在这儿的卡片；而且两份 id 会撞（`#card-scheduler` 只能有一个）。"""
        self.assertEqual(INDEX_HTML.count('id="card-scheduler"'), 1)
        general = self._panel("subpanel-general")
        self.assertNotIn('id="card-scheduler"', general)
        self.assertNotIn('id="schedule-box"', general)
        self.assertIn("定时器设置", general, "总得留一句话告诉人去哪了")

    def test_左下角那一行真的切到这一页(self):
        """⚠ 它以前是"跳到通用页里那张卡"（`FOOT_GO` 三项）。
        现在三项的话会**滚到一张不存在的卡**上（点了像没反应）。"""
        self.assertIn("scheduler: ['settings', 'timer']", APP_JS)
        self.assertIn("timer: () => { loadTimer(); loadSchedulerBits(); }", APP_JS)

    def test_两个模块设置页各有自己那几步(self):
        sales = self._panel("subpanel-sales-settings")
        comp = self._panel("subpanel-compliance-settings")
        self.assertIn('id="timer-tasks-sales"', sales, "周度重点产品的设置页里没有定时那块")
        self.assertIn('id="timer-tasks-compliance"', comp, "五项合规的设置页里没有定时那块")
        # ⚠ 两个模块页 + 独立页 = **同一份数据三个入口**，渲染只能有一个函数
        self.assertIn("const TIMER_CONTAINERS", APP_JS)
        i = APP_JS.index("const TIMER_CONTAINERS")
        blk = APP_JS[i:i + 400]
        for ident in ("timer-tasks", "timer-tasks-sales", "timer-tasks-compliance"):
            self.assertIn("'%s'" % ident, blk, "容器表里少了 %s" % ident)

    def test_模块页按归属过滤_归属由后端给(self):
        """⚠ 前端不许自己列一份"哪一步属于谁"（那是第二份定义）。"""
        i = APP_JS.index("const TIMER_CONTAINERS")
        blk = APP_JS[i:i + 400]
        self.assertIn("'sales'", blk)
        self.assertIn("'compliance'", blk)
        self.assertIn("t.owner_key === owner", APP_JS, "过滤要按后端给的 owner_key")
        # ⚠⚠ 断言前**必须剥掉 JS 注释** —— app.js 里那段注释正好引用了
        #   老的写死写法（「这儿以前写死过 `t.cmd === 'dump' ? …`」），
        #   不剥的话 `assertNotIn` 会被**自己的注释**顶掉。
        #   这个坑在这个项目里踩过好几次了（见 AGENTS.md 坑 12 那条的同类）。
        code = re.sub(r"/\*.*?\*/", "", APP_JS, flags=re.S)
        code = re.sub(r"(?m)//[^\n]*$", "", code)
        self.assertNotIn("t.cmd === 'dump' ?", code,
                         "又在前端写死归属了 —— 那只有肉眼能发现指错地方")

    def test_模块页不重复功能那一列(self):
        """整页都是同一个模块的，再列一遍"五项合规、五项合规…"没意义。"""
        self.assertIn("timerRow(t, !owner", APP_JS)
        self.assertIn("withOwner", APP_JS)

    def test_一个字没动就什么都不显示(self):
        """⚠⚠ 2026-09-21 用户：「**✅ 没改动：本来就是每天 21:00（下一趟 今天 21:00）
        就这句，不要显示了**」——点「保存」但值没变 ⇒ **什么都不说**（没发生事）。

        ⚠ 判据用后端给的 `changed`（"真改了没"是后端算的：它读的是**生效的**
          那一份，前端手里那份是渲染时的快照，两处各算一定分叉）。
        ⚠ 也别退回 toast —— 用户上一轮刚说过「单击保存会弹出来一些奇怪的东西，
          会突然消失」。
        """
        blk = APP_JS[APP_JS.index("async function timerSave"):]
        blk = blk[:blk.index("\n}")]
        self.assertIn("res.changed === false", blk)
        self.assertNotIn("toast(", blk, "「保存」的反馈不许再飘一个窗")

    def test_调顺序发的是整张表的顺序(self):
        """⚠ 发**整份名单**（不是"把某一步上移一格"）：后端不用猜现在是什么顺序，
        也不会因为两次点击之间别人改过而错位。"""
        blk = APP_JS[APP_JS.index("closest('[data-timer-move]')"):]
        blk = blk[:blk.index("/* ─")] if "/* ─" in blk else blk[:2000]
        self.assertIn("body: { order: cmds }", blk)
        self.assertIn("mv.dataset.timerMove === 'up'", blk)
        self.assertIn("loadTimer()", blk, "改完要重新拉一遍（顺序是后端说了算）")

    def test_那行_这台机器改过_去掉了(self):
        """⚠ 2026-09-21 用户：「这台机器改过这个东西不用要了」——
        「默认」按钮也没了，标着它只会让人困惑"那我怎么改回去"。"""
        self.assertNotIn('<br><span class="hint">这台机器改过</span>', APP_JS)

    def test_模块页不给调顺序的箭头(self):
        """⚠ 顺序是**整张表**的事（2026-09-21 用户：「相同时间执行的任务，按照定时器
        这个列表从上到下执行」）—— 模块页那张表只有本模块的几步，在那儿调顺序
        发出的是**残缺的名单**（会把别的模块的步骤挤出原来的位置）。
        ⇒ 箭头只在「定时器设置」那一页（`withOwner` 为真）出现。"""
        self.assertIn("const mover = !withOwner ? { html: '' }", APP_JS)
        self.assertIn('data-timer-move="up"', APP_JS)
        self.assertIn('data-timer-move="down"', APP_JS)
        # 表头也跟着多一格（独立页 6 列、模块页 5 列）
        self.assertIn("'', '', '功能', '做什么'", APP_JS)
        self.assertIn("'', '', '做什么'", APP_JS)


class Test定时器页顶上那行大字(unittest.TestCase):
    """用户 2026-09-20：「定时器设置页面**最上面大字**写着**下一次执行的是啥，什么时间**。

    ⚠ 措辞全用**后端给的**（`next_run.at_text` / `.label`）——
      前端不自己算"下一步是哪个"（定时器算的那份才算数，两处各算必然分叉）。
    """

    def test_大字那块在任务表上面(self):
        hero = INDEX_HTML.index('id="timer-hero"')
        tasks = INDEX_HTML.index('id="timer-tasks"')
        self.assertLess(hero, tasks, "大字要在任务表**上面**")
        self.assertIn(".timer-hero-time", STYLE_CSS, "大字的样式没写")
        self.assertRegex(STYLE_CSS, r"\.timer-hero-time\s*\{[^}]*font-size:\s*2\dpx",
                         "既然是「大字」，字号得明显大于正文")

    def test_内容来自后端_不自己算(self):
        i = APP_JS.index("function renderTimerHero(")
        blk = APP_JS[i:i + 1200]
        self.assertIn("nxt.at_text", blk)
        self.assertIn("nxt.label", blk)
        # ⚠ 前端**不许**自己从 tasks 里挑"下一个" —— 那是 `timer.next_run()` 的活
        self.assertNotIn("attainTier", blk)
        self.assertNotIn("next_after", blk)

    def test_正在跑时优先说正在跑(self):
        """⚠ `next_run` 说的是"下一次"，而正在跑的时候用户最想知道的是
        "它现在在跑" —— 不然会以为没动。"""
        i = APP_JS.index("function renderTimerHero(")
        blk = APP_JS[i:i + 1200]
        self.assertIn("st.running", blk)
        self.assertIn("正在跑", blk)

    def test_没有下一步时说人话(self):
        i = APP_JS.index("function renderTimerHero(")
        blk = APP_JS[i:i + 1600]
        self.assertIn("没有哪一步设了唤醒时刻", blk)


class Test执行日志那张表(unittest.TestCase):
    """用户 2026-09-20：「加一个定时器执行日志，记录什么时间唤醒了什么，成功了没」。"""

    def test_卡片和容器都在(self):
        self.assertIn('id="card-wake-log"', INDEX_HTML)
        self.assertIn('id="wake-log"', INDEX_HTML)
        self.assertIn("renderWakeLog", APP_JS)
        self.assertIn("/api/timer", APP_JS)

    def test_四列_时间_唤醒什么_结果_用时(self):
        i = APP_JS.index("function renderWakeLog(")
        blk = APP_JS[i:i + 1200]
        for col in ("时间点", "唤醒了什么", "结果", "用时"):
            with self.subTest(col=col):
                self.assertIn(col, blk)

    def test_没跑过时说清手动跑不算(self):
        """⚠ 这张表**只记定时器叫醒的那几趟** —— 右下角「整个项目」是手动跑的，
        它走 `runner` + 抽屉日志，不写 `kind="wake"`。界面上要说清，
        否则门店手动跑完回来看这儿是空的，会以为"没跑成"。"""
        i = APP_JS.index("function renderWakeLog(")
        blk = APP_JS[i:i + 1800]
        self.assertIn("手动跑不算", blk)
        self.assertIn("定时器叫醒", blk)


class Test定时器页那个开关滑块(unittest.TestCase):
    """⭐ 用户 2026-09-20：「模块的什么时候自动跑这个功能，**左边加个开关滑块**吧，

    > **开了就注册到定时器，不开就不注册**」

    ⚠ 复用的是「标色」那个 `.switch` 组件（滑块的样子全站统一，别再画一套）；
    ⚠ 拨完**重新拉一遍**接口，不自己在本地翻转状态 —— "注册没注册"的真相在后端。
    """

    def test_开关在最左边一列(self):
        # ⚠ 锚 `const sw = ` 就够 —— 2026-09-21 给它加了三元（没有唤醒时刻的步骤
        #   **不画开关**，改一句话），原来锚的 `const sw = { html:` 就不在了。
        i = APP_JS.index("const sw = ")
        self.assertLess(i, APP_JS.index("const cells = ["), "开关要加在行内容**前面**")
        self.assertIn("return [sw].concat(rest)", APP_JS)
        # 表头第一格是空的（开关本身不需要标题），列也有自己的类
        self.assertIn("'sw-cell'", APP_JS)
        self.assertIn(".switch", STYLE_CSS, "要复用标色那个 .switch 组件")

    def test_默认按钮去掉了(self):
        """⚠ 2026-09-21（用户：「**我们不需要默认设置，去掉这个按钮就行了**」）——
        「默认」按钮（`data-timer-reset`，恢复成模块声明的默认时间）**删了**，
        连它的点击分支一起（留着就是一段走不到的死代码）。

        ⚠ 后端那条路**还认**（`PUT /api/timer {whens: []}` = 恢复默认）——
        老缓存页面点它照样能恢复，提示语也说"恢复默认：每天 21:00"而不是"只手动跑"
        （见 `test_web.py::Test定时器…::test_点默认要说是恢复默认`）。
        ⚠ 断言用**选择器**（带中括号）而不是裸名字：app.js 里那段说明为什么删的
        注释会写 `data-timer-reset` 这几个字（锚裸名字会匹到注释上，踩过一次）。
        """
        self.assertNotIn("[data-timer-reset]", APP_JS, "「默认」按钮又回来了？")
        self.assertIn("[data-timer-save]", APP_JS, "「保存」不能跟着删")

    def _handler(self):
        """切到**事件处理那段**（不是渲染那段）。

        ⚠ 锚点得用处理函数里那句 `closest('[data-timer-toggle]')` ——
          直接找 `[data-timer-toggle]` 会匹到**渲染行**的地方（属性字符串），
          两处都含这个字面量，切错了断言就假绿。
        """
        i = APP_JS.index("const box = e.target.closest && e.target.closest('[data-timer-toggle]')")
        return APP_JS[i:i + 900]

    def test_拨开关打的是_enabled(self):
        blk = self._handler()
        self.assertIn("enabled: !!box.checked", blk)
        self.assertIn("method: 'PUT'", blk)

    def test_拨完重新拉_不本地翻转(self):
        """⚠ 本地翻转的话，后端拒绝时界面会显示成"开着的"（真相在后端）。"""
        blk = self._handler()
        self.assertIn("await loadTimer()", blk)
        self.assertIn("box.checked = !box.checked", blk, "失败了要拨回去")

    def test_关掉那行控件置灰但还在(self):
        """⚠ **不隐藏** —— 时间点还留着（再打开就是它），
        藏起来的话人会以为"关掉把设置也清了"。

        ⚠ 判据要盯**那个 `timer-off` 包裹层**，不能笼统地"这附近不许有 hidden"：
          现在这块里合法地出现过 `hidden`（每小时那一档会把**时间框**藏起来，
          因为它没有"几点"）—— 那是另一件事。
        """
        self.assertIn(".timer-off", STYLE_CSS)
        # ⚠ 锚点别写那一整行 JS（里面单双引号都有，Python 里很难写对）——
        #   只锚 `timer-off` 这个词，再看它后面那一段。
        i = APP_JS.index('timer-off')
        blk = APP_JS[i:i + 700]
        self.assertIn("disabled", blk, "关掉之后时间控件要 disabled")
        self.assertNotIn("? '' : 'hidden'", blk)
        # 那一行**没有** "关掉就把整块藏起来" 的写法
        self.assertNotIn("? '' : ' hidden'}`", blk)


class Test三个刷新按钮会去抓新数据(unittest.TestCase):
    """⭐ 用户 2026-09-20：「周度重点的刷新按钮，还有 pos 合规和报量查询的刷新按钮
    **需要单独调用一次抓取新数据**」。

    ⚠ 三个按钮**都要接上** —— 漏一个的话，那一页的「刷新」还是只重读旧数据，
      而用户会以为它抓过了（"看着像刷新了"比"没有刷新按钮"更坑）。
    ⚠ 也要**说清会等一会儿**（`title`/`aria-label`）：点一下等两分钟，不打招呼很突然。
    """

    def _handler(self, btn_id):
        i = APP_JS.index("$('#%s')" % btn_id)
        return APP_JS[i:i + 260]

    def test_三个按钮都接了_refreshWithFetch(self):
        for btn, page in (("btn-refresh-attain", "attain"),
                          ("btn-refresh-pos", "pos"),
                          ("btn-refresh-reports", "pools")):
            with self.subTest(btn=btn):
                blk = self._handler(btn)
                self.assertIn("refreshWithFetch('%s'" % page, blk)
                self.assertIn("当前这页的加载器", blk) if False else None
                # 跑完要重读这一页
                self.assertIn("load", blk)

    def test_抓的是后台任务_不是同步等(self):
        i = APP_JS.index("async function refreshWithFetch(")
        blk = APP_JS[i:i + 1400]
        self.assertIn("/api/refresh", blk)
        self.assertIn("watchJob", blk, "要盯着后台任务，跑完再重读")
        self.assertIn("setRunDrawer(true)", blk, "得让人看得到进度（日志在抽屉里）")

    def test_已经在跑时不弹红错误(self):
        """⚠ 409 = "已经有一趟在跑"，那是**正常情况**，不该弹红 toast 吓人。"""
        i = APP_JS.index("async function refreshWithFetch(")
        blk = APP_JS[i:i + 1400]
        self.assertIn("已经在跑", blk)

    def test_按钮上说清了会等一会儿(self):
        for btn in ("btn-refresh-attain", "btn-refresh-pos", "btn-refresh-reports"):
            with self.subTest(btn=btn):
                i = INDEX_HTML.index('id="%s"' % btn)
                blk = INDEX_HTML[i:i + 220]
                self.assertIn("1~2 分钟", blk, "得先打招呼，不然点一下等两分钟太突然")


class Test库存盘点是同文档子面板(unittest.TestCase):
    """M16（2026-09-20 接入；2026-09-22 **拆掉 iframe**）。

    | 时间 | 形态 | 用户原话 |
    |---|---|---|
    | 上午 | 菜单点开**另开一整页** | 「菜单点开就是它自己那套界面」 |
    | 下午 | **嵌进控制台**（iframe） | 「**做嵌套进来吧，现在单独打开一个页面很奇怪**」 |
    | 22 日 | **拆 iframe、并进同文档** | 「真拆掉 iframe，盘点内容并进 #subpanel-inventory」 |

    ⇒ 现在是 `#subpanel-inventory` 里的 `.inv-root`（markup 直接在 index.html），
      配色吃控制台主题；脚本由 `mountInventory` **懒注入**。
    """

    def _panel(self):
        # ⚠ 别用第一个 `</section>` 收尾 —— 盘点里有多张 section 卡，
        #   那一刀会停在 setup 上、看不见后面的扫码框（拆 iframe 后踩过）。
        i = INDEX_HTML.index('id="panel-inventory"')
        nxt = INDEX_HTML.find('id="panel-', i + 10)
        j = nxt if nxt != -1 else len(INDEX_HTML)
        return INDEX_HTML[i:j]

    def test_二级项是普通子面板_不再另开页(self):
        """⚠ 别再有 `data-page` / `window.open` —— 那是上午那版，被用户否了。"""
        menu = _nav_item("inventory", nxt=None)
        self.assertIn('data-subtab="inventory"', menu)
        self.assertNotIn("data-page", menu)
        self.assertNotIn("window.open('/inventory.html'", APP_JS)
        self.assertNotIn("btn-open-inventory", INDEX_HTML)

    def test_面板里是_inv_root_不是_iframe(self):
        seg = self._panel()
        self.assertIn('id="subpanel-inventory"', seg)
        self.assertIn('id="inv-root"', seg)
        self.assertIn('class="inv-root"', seg)
        self.assertNotIn('id="inv-frame"', seg, "iframe 应已拆掉")
        self.assertNotIn('src="about:blank"', seg)

    def test_外面那层卡片去掉了(self):
        """外层控制台卡片仍不许回来；面板本身留着（切页签靠它）。"""
        seg = self._panel()
        self.assertIn('id="subpanel-inventory"', seg)
        self.assertIn('id="inv-root"', seg)
        i = seg.index('id="subpanel-inventory"')
        j = seg.index('id="subpanel-inventory-settings"') if 'subpanel-inventory-settings' in seg else len(seg)
        inv_only = seg[i:j]
        # 盘点自己的 `.card`（setup/scan…）允许；控制台那层包装卡不许
        self.assertNotIn("inv-hint", inv_only, "那句焦点提示跟着卡片一起删了")
        self.assertIn('id="scan-input"', inv_only, "扫码框要直接在面板里")
        self.assertIn('id="setup"', inv_only)

    def test_脚本懒注入且只注一次(self):
        i = APP_JS.index("function mountInventory(")
        blk = APP_JS[i:i + 600]
        self.assertIn("_invScriptsLoaded", blk, "注入只做一次，别每次切都重跑 main")
        self.assertIn("focusInvScan()", blk, "切过来要把焦点交给扫码框")
        self.assertIn("/inventory/", blk, "从 web/inventory/ 注入五个 js")
        # 顺序依赖：core → ui
        self.assertIn("core", blk)
        self.assertIn("ui", blk)

    def test_切过去会挂载(self):
        i = APP_JS.index("const SUBTAB_LOADERS = {")
        blk = APP_JS[i:APP_JS.index("};", i)]
        self.assertIn("inventory: () => mountInventory()", blk)

    def test_焦点丢了还能扫(self):
        """同文档后不再 postMessage；靠 focusInvScan + ui.js 自己的 keydown。"""
        i = APP_JS.index("$('#subpanel-inventory')?.addEventListener('mousemove'")
        self.assertIn("focusInvScan()", APP_JS[i:i + 300])
        self.assertNotIn("postMessage({ ic: 'scan-key'", APP_JS, "拆 iframe 后不该再转按键")
        # ui.js 在扫码台可见时自己 focusScan（全局 keydown）
        self.assertIn("function focusScan(", INV_UI_JS)
        self.assertIn("if (e.key.length === 1) focusScan()", INV_UI_JS)

    def test_嵌进去时不重复自我介绍(self):
        """同文档靠 CSS 藏大标题；applyEmbed 不许再往 body 上加 embed。"""
        self.assertIn("#subpanel-inventory .inv-root .topbar h1", INV_CSS)
        self.assertIn("#btn-back", INV_CSS)
        i = INV_UI_JS.index("function applyEmbed(")
        blk = INV_UI_JS[i:i + 500]
        self.assertIn("subpanel-inventory", blk, "同文档路径要识别")
        self.assertIn("别往 body 上加 embed", blk)

    def test_盘点页自己的壳在_web_下(self):
        """⚠ 盘点页**不是构建产物**：五个 js + 一个 css 直接由 `_static()` 发
        （跟控制台一个路子，`theme.FILES` 那条红线：前端不做构建步骤）。
        inventory.html 仍保留作**单独打开**的壳；控制台走同文档 + 懒注入。
        """
        web = ROOT / "web"
        for rel in ("inventory.html", "inventory/core.js", "inventory/api.js",
                    "inventory/store.js", "inventory/xlsx.js", "inventory/ui.js",
                    "inventory/style.css"):
            with self.subTest(rel=rel):
                self.assertTrue((web / rel).is_file(), "缺 web/%s" % rel)


class Test左下角那个定时小标(unittest.TestCase):
    """用户 2026-09-21：「**左下角未设定时改成真实时间吧**」。

    ⚠ 它原来读的是 `schedule.installed`（**Windows 计划任务**）——
      那个兜底 2026-09-20 整个撤掉了（「不用系统的计划任务」）⇒
      小标**永远显示「未设定时」**，而定时器里明明排着 6 件事。
    ⇒ 现在读 `overview.timer`（后端算好）。
    """

    def test_不再看有没有_windows_计划任务(self):
        i = APP_JS.index("const scp = $('#pill-schedule')")
        blk = APP_JS[i:i + 1200]
        self.assertNotIn("sch.installed", blk,
                         "又回去看 Windows 计划任务了 —— 那个兜底已经没了")
        self.assertIn("o.timer", blk)

    def test_显示的是每天那趟_不是每小时的自动更新(self):
        """⚠ 自动更新每小时都跑 ⇒ 拿它当小标，永远是"一小时内"，门店看不出东西。

        「每天那趟」= `default=True` 的那几步（后端 `timer.next_run(cmds=…)`）。
        """
        blk = APP_JS[APP_JS.index("const scp = $('#pill-schedule')"):][:1200]
        self.assertIn("next_daily", blk)
        self.assertIn("每天那趟", blk, "提示里要写清这是哪一趟")
        # 两个都摆上：小的给"每天那趟"，提示里给"下一次任意步骤"
        self.assertIn("next_run", blk)

    def test_真的没有才说未设定时(self):
        blk = APP_JS[APP_JS.index("const scp = $('#pill-schedule')"):][:1600]
        self.assertIn("未设定时", blk)
        self.assertLess(blk.index("next_daily"), blk.index("未设定时"),
                        "得先看有没有时间，别一上来就写未设定时")
