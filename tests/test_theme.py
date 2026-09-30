"""设计令牌 / 主题 / 动效的**规矩**（`web/theme.css` + `web/style.css`）。

用户 2026-09-18：「为以后的工作提前做个规划，把涉及到动画、显示效果的内容做到
一个分类里面，以后可能会做精简模式和不同的主题风格，用户可以一键切换」。

把这件事做成的前提不是"文件分得漂亮"，而是**组件规则里不许再出现写死的
颜色 / 阴影 / 时长** —— 只要有一处写死，换主题时它就不会跟着变，
而你在界面上只会看到"大部分变了、某一小块没变"，很难定位。

这组测试就是钉这条的。⚠ 断言前一律**剥注释**：
这个项目的注释里会**原样提到**被禁的写法（解释"为什么不用 `transition: none`"
时就得写出那几个字），不剥的话注释自己会把断言顶掉（踩过三次了）。
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WEB = ROOT / "web"


def _strip_css(text: str) -> str:
    """去掉 CSS 注释。"""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


class _Base(unittest.TestCase):
    def setUp(self):
        self.raw_theme = (WEB / "theme.css").read_text(encoding="utf-8")
        self.raw_style = (WEB / "style.css").read_text(encoding="utf-8")
        # ⭐ 一主题一文件（2026-09-22）：配色在 web/themes/*.css
        self.themes_dir = WEB / "themes"
        self.raw_themes = {}
        for p in sorted(self.themes_dir.glob("*.css")):
            self.raw_themes[p.stem] = p.read_text(encoding="utf-8")
        self.raw_default = self.raw_themes.get("default", "")
        self.theme = _strip_css(self.raw_theme)
        self.default = _strip_css(self.raw_default)
        # 共享 + 默认基线 = 「定义了」的全集；各主题文件只是覆盖
        self.tokens = self.theme + self.default
        self.raw_all_themes = self.raw_theme + self.raw_default + "".join(
            self.raw_themes.values())
        self.all_themes = self.theme + "".join(
            _strip_css(v) for v in self.raw_themes.values())
        self.style = _strip_css(self.raw_style)
        self.html = (WEB / "index.html").read_text(encoding="utf-8")


class Test两份样式表的规矩(_Base):
    def test_令牌只有一处(self):
        """⚠ `:root` 散在文件各处 = 同一类东西两份定义。

        2026-09-18 之前 `style.css` 里**有三个** `:root`（颜色、`--side-w`、
        `--fab-*`），散在 300 多行里 —— 加一个主题得翻遍全文才知道有哪些令牌。
        """
        self.assertNotIn(":root", self.style,
                         "style.css 里不该再有 :root —— 令牌在 theme.css / themes/")
        self.assertIn(":root", self.theme, "共享令牌在 theme.css")
        self.assertIn(":root", self.default, "基线配色在 themes/default.css")

    def test_theme_排在_style_前面(self):
        """⚠ 令牌本身跟顺序无关（`var()` 是算的时候才解析），
        但主题/精简模式块靠**层叠顺序**压过组件规则 —— 排后面会把组件颜色一起盖掉。"""
        # ⚠ 认 href，别认裸文件名 —— 注释里也写着 style.css（踩过）
        i = self.html.index('href="/theme.css')
        j = self.html.index('href="/style.css')
        self.assertLess(i, j, "theme.css 必须排在 style.css 前面")
        # 每个主题配色也要在 style.css 前
        for name in sorted(self.raw_themes):
            k = self.html.index('href="/themes/%s.css' % name)
            self.assertLess(k, j, "themes/%s.css 必须排在 style.css 前面" % name)

    def test_组件里不许写死时长或缓动(self):
        """⚠ 写死一处，"关掉动画"就漏一处 —— 而漏掉的那个正是"关不掉的那个"。"""
        for m in re.finditer(r"transition:\s*([^;]+);", self.style):
            with self.subTest(decl=m.group(1).strip()[:60]):
                self.assertIn("var(--motion-", m.group(1),
                              "过渡时长/缓动要写成 var(--motion-*)，别写死")

    def test_组件里不许写死阴影(self):
        """阴影是**主题**会整体换掉的东西（深色主题的阴影几乎不可见，要换一套）。"""
        for m in re.finditer(r"box-shadow:\s*([^;]+);", self.style):
            with self.subTest(decl=m.group(1).strip()[:60]):
                self.assertIn("var(--shadow-", m.group(1))
        for m in re.finditer(r"filter:\s*(drop-shadow\([^;]+)\);", self.style):
            with self.subTest(decl=m.group(1).strip()[:60]):
                self.assertIn("var(--glow-", m.group(1))

    def test_每个令牌都用得上(self):
        """⚠ 定义了没人用的令牌 = 文档谎话：读的人以为改它有用，其实没接上。

        （反过来"用了没定义"浏览器会静默失效，也要查 —— 见下一条。）
        """
        # ⚠ app.js 里也有 var(--…)（拼 HTML 的内联样式，如 --hot）——
        #   不扫的话「定义了但只在 JS 用」会被误报成没人用。
        used = set(re.findall(r"var\((--[\w-]+)",
                              self.style + self.all_themes
                              + (WEB / "app.js").read_text(encoding="utf-8")))
        defined = set(re.findall(r"^\s*(--[\w-]+):", self.tokens, re.M))
        unused = sorted(defined - used)
        # ⚠ **不许开白名单** —— 我第一版给 `--ink*` 开了个豁免，
        #   结果那几个令牌定义了却根本没人用（`.log` 还在写死 `#0f172a`），
        #   等于测试替我遮住了真问题。
        self.assertEqual(unused, [], "这些令牌定义了但没人用：%s" % unused)

    def test_用了的令牌都定义了(self):
        used = set(re.findall(r"var\((--[\w-]+)", self.style))
        # 主题/共享文件里自己用自己的（比如 --fab-center 引用 --fab-gap）
        used |= set(re.findall(r"var\((--[\w-]+)", self.all_themes))
        defined = set(re.findall(r"^\s*(--[\w-]+):", self.tokens, re.M))
        missing = sorted(used - defined)
        self.assertEqual(missing, [], "这些令牌没定义：%s" % missing)


class Test动效总开关(_Base):
    def test_只有一处关动画(self):
        """⚠ 用**令牌归零**，不是逐个选择器 `transition: none` ——
        前者改一处全站生效，后者漏一个就有一个关不掉。"""
        self.assertIn("prefers-reduced-motion", self.theme)
        i = self.theme.index("prefers-reduced-motion")
        blk = self.theme[i:i + 300]
        self.assertIn("--motion-fast: 0s", blk)
        self.assertIn("--motion-normal: 0s", blk)
        # 组件表里不该再出现"逐个选择器关动画"
        self.assertNotIn("prefers-reduced-motion", self.style)

    def test_给一键切换留了钩子(self):
        """前端「一键切换」改的是 `body` 上的属性 —— 钩子先留好。"""
        self.assertIn('body[data-motion="off"]', self.theme)
        # 主题块在 web/themes/*.css（一主题一文件）
        self.assertIn('body[data-theme=', self.raw_all_themes)
        self.assertIn('body[data-density=', self.raw_theme)

    def test_关动画不会把状态动画弄丢(self):
        """⚠ 归零的是**时长**，不是删掉 `transition` 属性 ——
        留着属性、时长为 0，状态切换仍然瞬时生效，也不会因为过渡属性没了引起抖动。"""
        self.assertNotIn("transition: none", self.style)


class Test过渡动画(_Base):
    """用户 2026-09-18：「二级标签栏的出现和消失也做个过渡动画，
    左边栏的出现与消失也做个动画」。

    ⚠ 两处都栽过同一个跟头：**`display: none` 过渡不了** ——
    写 `display: none` 的话收起是"啪"地消失，加了 transition 也没用。
    """

    def setUp(self):
        super().setUp()
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_侧栏是滑出去不是直接消失(self):
        """原来是 `body.side-collapsed .sidebar { display: none }`。"""
        i = self.style.index("body.side-collapsed .sidebar")
        blk = self.style[i:self.style.index("}", i)]
        self.assertIn("margin-left: calc(var(--side-w) * -1)", blk,
                      "侧栏收起还是 display:none？那没法做动画")
        self.assertNotIn("display: none", blk)
        # 负 margin 让 flex 项外框变 0，内容区自然拿到全宽 —— 这是能这么写的前提
        self.assertIn("transition:", blk, "没有过渡")

    def test_侧栏收起后不会被_Tab_聚焦到(self):
        """⚠ 光"滑出视口"不够：里面的链接**还能被 Tab 聚焦** ——
        键盘用户会跳进一个看不见的侧栏里。滑完要 `visibility: hidden`。"""
        i = self.style.index("body.side-collapsed .sidebar")
        blk = self.style[i:self.style.index("}", i)]
        self.assertIn("visibility: hidden", blk)
        # 延迟一个动画时长再藏，否则滑到一半就没了
        self.assertIn("visibility 0s linear var(--motion-normal)", blk,
                      "visibility 要**延迟**到滑完才切，否则动画被切掉")

    def test_一级标签是就地变形_不是浮层(self):
        """⚠ 2026-09-19 用户：「上面一级标签的结构也改成**左下角的结构和动画效果**」。

        左下角那套（`.side-foot`）是**就地变形**：没有绝对定位、没有浮层。
        所以这条钉住三件事：
          ① `.nav-item` 是**竖着排**的容器（二级项在它下面撑开）；
          ② `.nav-menu` 用 `height` 过渡（不用 `max-height` —— 猜数会发顿）；
          ③ **不许再出现浮层时代的定位/位移**（`position: absolute` / `translateY`）。
        """
        i = self.style.index("\n.nav-item {")
        item = self.style[i:self.style.index("}", i)]
        self.assertIn("flex-direction: column", item)
        j = self.style.index("\n.nav-menu {")
        blk = self.style[j:self.style.index("}", j)]
        self.assertIn("transition:", blk)
        self.assertIn("height: 0", blk, "没有初始态就没有可过渡的东西")
        self.assertIn("overflow: hidden", blk)
        nav = self.style[i:self.style.index(".nav-menu .subtab {", i)]
        self.assertNotIn("position: absolute", nav, "浮层时代的东西要清干净")
        self.assertNotIn("translateY", nav)

    def test_每个一级标签的展开高度跟它的二级项数对上(self):
        """⚠ 高度是**写死的**（两行 / 三行），写错就会"展开到一半被截掉"。

        所以这条直接对着 HTML 数二级项：`--nav-h` 必须 = 项数 × 行高 + 间隙。
        """
        # ⚠ 切片要**限定在这一个 nav-item 里** —— 切到 `</nav>` 的话
        #   会把后面那个一级标签的二级项也数进来（第一版就这么算错了）。
        def count(tab):
            i0 = self.html.index('class="nav-item" data-tab="%s"' % tab)
            i1 = self.html.index("</div>\n  </div>", i0)
            return len(re.findall(r'data-subtab="', self.html[i0:i1]))

        def expected(tab):
            n = count(tab)
            return str(n * 35 + (n - 1) * 2)

        i = self.style.index("\n.nav-item:hover .nav-menu")
        blk = self.style[i:self.style.index("\n.nav-menu .subtab {", i)]
        # sales 是**默认那条**（不带 [data-tab] 限定）；项数和它不一样的**每个都要单独写**
        self.assertEqual(re.search(r"height: (\d+)px", blk).group(1), expected("sales"),
                         "sales 的展开高度和它的二级项数对不上")
        # ⚠ 逐个标签核对 —— 只盯 sales/compliance 会漏掉后来加的 1 项页
        #   （2026-09-21 用户：「月度生意计划的二级标签空白的有点多」就是这么漏的）。
        for tab in ("compliance", "plan", "inventory", "valueadd", "tools"):
            with self.subTest(tab=tab):
                m = re.search(r'data-tab="%s"[^{]*\{[^}]*height: (\d+)px' % re.escape(tab), blk)
                self.assertIsNotNone(m, "%s 没写自己的展开高度" % tab)
                self.assertEqual(m.group(1), expected(tab),
                                 "%s 的展开高度和它的二级项数对不上" % tab)

    def test_一级菜单的显隐只靠_open_类(self):
        """⚠ 跟悬浮窗那套 `setVisible` **分家**了：就地变形靠 CSS 过渡，
        JS 只切 `.open`（碰 `hidden` 的话 `display: none` 过渡不了）。"""
        i = self.js.index("function closeNavMenus(")
        blk = self.js[i:i + 900]
        self.assertIn("item.classList.remove('open')", blk)
        # ⚠ 看**代码行**，别整段搜：那段注释里正好写着"不再碰 `hidden`"（说明用），
        #   整段 `assertNotIn` 会把说明也判成实现（这个坑今天踩过第二次了）。
        code = "\n".join(l for l in blk.splitlines() if not l.strip().startswith("//"))
        self.assertNotIn("hidden", code)
        self.assertNotIn("setVisible", code)
        j = self.js.index("function openNavMenu(")
        self.assertIn("item.classList.add('open')", self.js[j:j + 400])
        self.assertNotIn("setVisible", self.js[j:j + 400])


class Test令牌覆盖得够不够(_Base):
    """换主题/做精简模式时，**没令牌化的写死值**就是漏网点。

    这里只钉"已经令牌化的那几类"不许回退；
    "还有哪些写死的值没令牌化"是已知工作量，记在规划文档里，不用测试钉。
    """

    def test_侧栏宽度只有一处定义(self):
        """⚠ 折叠开关靠它算位置 —— 写死两处迟早对不上。"""
        self.assertEqual(len(re.findall(r"--side-w:", self.theme)), 1)
        self.assertIn("var(--side-w)", self.style)

    def test_把手圆心是算出来的不是写死的(self):
        """⚠ 写死一个 41px 的话，把手尺寸或边距一改，"窗角对圆心"立刻分家。"""
        self.assertIn("--fab-center: calc(var(--fab-gap) + var(--fab-size) / 2)",
                      self.theme)

    def test_深浅两套底色都有令牌(self):
        """深色主题要用的那几个底（日志框 / toast）现在写死成 `#0f172a` 之类的，
        换主题会漏 —— 已经令牌化成 `--ink*`。"""
        for name in ("--ink:", "--ink-2:", "--ink-text:"):
            with self.subTest(token=name):
                self.assertIn(name, self.tokens, "配色在 themes/default.css")


class Test提示条对比度(unittest.TestCase):
    """暮山蓝 × 晚桃粉的 toast 看不见（用户 2026-09-30 截图「已修改」）。

    两个根因各钉一条：
    ① `toast()` 老代码传 `'good'`，CSS 只认 `ok`/`bad` ⇒ 落成**裸 toast**
 （`--ink-2` 底 + `--on-brand` 字）—— 暮山蓝里两个令牌都是近黑，1:1 看不见；
    ② 基础 `.toast` 的字色**不许用 `--on-brand`**（那是"主色底上的字"），
      深色主题必跟 `--ink-2` 撞 —— 用 `--ink-text`（ink 底的配对字色）。
    """

    def setUp(self):
        self.style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_toast的good别名映射成ok(self):
        i = self.js.index("function toast(")
        blk = self.js[i:i + 400]
        self.assertIn("'good'", blk, "good 要在 toast() 里被映射掉")
        self.assertIn("'ok'", blk)

    def test_基础toast字色用ink_text_okbad才用onbrand(self):
        i = self.style.index(".toast {")
        blk = self.style[i:i + 300]
        self.assertIn("color: var(--ink-text)", blk)
        self.assertNotIn("color: var(--on-brand)", blk)
        for cls in (".toast.bad {", ".toast.ok {"):
            with self.subTest(cls=cls):
                j = self.style.index(cls)
                self.assertIn("color: var(--on-brand)", self.style[j:j + 120],
                              "%s 的底是状态色，字色单独钉 --on-brand" % cls.strip(" {"))


if __name__ == "__main__":
    unittest.main()


class Test达成标色的四档(_Base):
    """⚠ 2026-09-20 用户两条：「**这个颜色不跟着数据块走了**」+「**这个颜色要加到主题里**」。

    两件事：

    1. **色块必须跟着数据块**（永久性修法）——
       **涂在 `.attain-cell` 上，不涂在 `<td>` 上**。
       涂 `td` 的时候，格子宽是 `table-layout: fixed` 平均分的、数据块宽是内容决定的，
       两者只在某个屏幕宽度下才凑巧一样（实测宽屏上格子 184px、数据 118px ⇒
       色块比数字多出 66px，看着就是"颜色没跟着数据走"）。
       涂在数据块自己身上 ⇒ **同一个元素**，从构造上不可能错开。
    2. **四个档位是主题令牌**（`--rate-*`），换主题只改 theme.css。

    ⚠ 别改回 `td` —— 那样这条测试会红，而且红得有理由（上面那段数字就是当时的实测）。
    """

    #: 四档的类名 → 令牌名。**两处必须一一对应**（少一个就有档位不变色）。
    TIERS = {"t-red": "--rate-red", "t-orange": "--rate-orange",
             "t-lg": "--rate-lg", "t-g": "--rate-g"}

    def test_四档颜色在主题里定义(self):
        """⚠ 用户明确要求："这个颜色要加到主题里，以后换主题会用得到"。"""
        for klass, token in self.TIERS.items():
            with self.subTest(tier=klass):
                self.assertIn(token + ":", self.tokens,
                              "%s 没在 theme.css / themes/ 里定义" % token)

    def test_底色涂在数据块上_不涂在格子上(self):
        for klass, token in self.TIERS.items():
            with self.subTest(tier=klass):
                self.assertIn(".table-scroll td.%s .attain-cell" % klass, self.style)
                self.assertIn("var(%s)" % token, self.style)
        # ⚠ 反面：不许再有 `td.t-xxx { background: … }` 那种"整格涂色"
        bad = re.findall(r"td\.(t-(?:red|orange|lg|g))\s*\{[^}]*background", self.style)
        self.assertEqual(bad, [], "又涂回整个格子了 ⇒ 宽屏上色块会比数字宽一大截：%s" % bad)

    def test_组件里不许写死标色的值(self):
        """四条规则里**只许** `var(--rate-*)` —— 写死一处，换主题就漏一处。

        ⚠ 别按"某几个十六进制值"去查：那两个值 2026-09-20 一天里改过三次，
          照值查的话这条测试**早就是摆设了**。改成"规则里不许出现 `#`"。
        """
        for klass in self.TIERS:
            with self.subTest(tier=klass):
                m = re.search(r"\.table-scroll td\.%s\s+\.attain-cell\s*\{([^}]*)\}"
                              % klass, self.style)
                self.assertIsNotNone(m, "找不到 td.%s 那条规则" % klass)
                self.assertIn("var(--rate-", m.group(1))
                self.assertNotIn("#", m.group(1), "写死了颜色值 ⇒ 换主题会漏这一档")


class Test达成格里的间距(_Base):
    """⚠ 2026-09-20 用户：「**中间横线是不是太长了，百分比数字太靠右了**」。

    * 横线（`.attain-sep`）原来 `align-self: stretch` ⇒ 撑满 `.attain-pair`
      （46px），而数字只有 9px 宽 ⇒ 一条线压在两个一位数中间像"整格被划了一道"。
      ⇒ 现在**固定 22px + 居中**（用户 2026-09-19 就说过"分割线要居中"）。
    * 百分比离数字远：数字是**居中在目标框**里的，框右边的空白也算进视觉间距。
      ⇒ 框 56 → 46px、间距 10 → 3px。
    """

    def test_横线是短的而且是居中的(self):
        m = re.search(r"\.attain-sep\s*\{([^}]*)\}", self.style)
        self.assertIsNotNone(m, "找不到 .attain-sep")
        blk = m.group(1)
        self.assertIn("align-self: center", blk, "横线要居中（别用 stretch 撑满）")
        self.assertIn("width: 22px", blk)
        self.assertNotIn("stretch", blk)

    def test_三个目标框同宽(self):
        """⚠ 主表那行 / 展开的静止态 / 展开的编辑态 —— **必须同宽**，
        否则点开编辑时里面的数字会左右挪（用户为这条来过三个来回）。

        ⚠ 宽度是 46px（2026-09-20 从 56 收的）—— 改的话**三个一起改**。
        """
        got = {}
        for name in (".split-edit", ".split-in", ".num-box"):
            m = re.search(re.escape(name) + r"\s*\{([^}]*)\}", self.style)
            self.assertIsNotNone(m, "找不到 %s" % name)
            w = re.search(r"width:\s*(\d+)px", m.group(1))
            self.assertIsNotNone(w, "%s 没写 width" % name)
            got[name] = w.group(1)
        self.assertEqual(len(set(got.values())), 1,
                         "三种状态的宽度不一致 ⇒ 点开编辑会挪位：%s" % got)


class Test新主题必须给标色四档(_Base):
    """⚠ 用户 2026-09-20：「**这个颜色要加到主题里**，以后换主题会用得到」。

    四个 `--rate-*` 是**浅底色** —— 深色主题里照搬会又亮又刺眼，
    而"忘了给"的表现是**看着还行、就是有点怪**，没人会当成 bug 报上来。
    ⇒ 定成主题契约的一部分：**只要开了 `body[data-theme="…"]`，
      就必须把四档一起给全**（浅色变体想保持一致，就照抄 `:root` 那四个值）。

    ⚠ 现在还没有真正的主题块（只有一个注释示例），所以这条测试眼下是"占位"的 ——
      但它拦的正是**将来加主题那一刻**最容易漏的东西。
    """

    REQUIRED = ("--rate-red", "--rate-orange", "--rate-lg", "--rate-g")

    def _theme_blocks(self):
        # ⚠ 只在**剥掉注释之后**的文本里找 —— 否则会匹到 theme.css 里那段示例
        # ⚠ 一主题一文件：扫全部 themes/*.css（剥注释后）
        return re.findall(r"body\[data-theme=[^\]]+\]\s*\{([^}]*)\}", self.all_themes)

    def test_开了主题就把四档给全(self):
        for i, blk in enumerate(self._theme_blocks()):
            for token in self.REQUIRED:
                with self.subTest(block=i, token=token):
                    self.assertIn(token + ":", blk,
                                  "这个主题块没给 %s —— 深色底上照搬浅色会发光" % token)

    def test_示例里也带着四档(self):
        """注释里那段示例是**给人照抄的** —— 它漏了，抄的人就一起漏。"""
        for token in self.REQUIRED:
            with self.subTest(token=token):
                self.assertIn(token + ":", self.raw_all_themes,
                              "注释示例/主题文件里要带着 %s" % token)


class Test每个色块等宽(_Base):
    """⚠ 用户 2026-09-20：「**能不能等宽啊**」。

    量过：百分比自己宽度在 35~53px 之间晃（`0.0%` vs `116.7%`），
    色块跟着一格一个宽度（98~115px）—— 整张表看着毛毛糙糙。
    ⇒ 给百分比那一格**定死宽度 + 右对齐**（`class="rate"`）：
      所有色块一样宽，而且百分比那一列的小数点对得齐。

    ⚠ 用例名 `.rate`，别写成 `b:last-child` ——
      合计行里的 `<b class="attain-num num-box">` 也是 `.attain-cell` 的直接子元素，
      选错会把那个框也撑宽 ⇒ 又跟成员行的目标数错开（那条来回改过三次）。
    """

    def test_百分比有固定槽位(self):
        m = re.search(r"\.attain-cell > b\.rate\s*\{([^}]*)\}", self.style)
        self.assertIsNotNone(m, "找不到 `.attain-cell > b.rate`（等宽靠它）")
        blk = m.group(1)
        self.assertIn("width: 53px", blk)
        self.assertIn("text-align: right", blk)

    def test_两处渲染都带上这个类(self):
        """主表成员行 / 主表区域小计 / 历史页 —— **三处**渲染都要等宽槽位。

        ⚠ 2026-09-22：防护膜小计也挂了 `rate`，计数从 2 改成 3（只加不放宽：
        仍是「出现次数 == 渲染点数」的钉子，漏一处就红）。"""
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertEqual(js.count('<b class="rate">'), 3, "主表 / 区域小计 / 历史页各一处")
        self.assertEqual(js.count('<b class="rate hint">—</b>'), 3)


class Test样式表里不许有游离的注释标记(_Base):
    """⚠⚠ 2026-09-20 **踩了两次**的真坑，代价是"100-120% 那档不红"。

    在 `style.css` 里补注释时手一滑，**多加了一个注释闭标记** ——
    第一个闭标记之后的文字变成"游离的垃圾 token"，浏览器按错误恢复规则
    **吞掉了紧跟其后的第一条规则**（正好是 `td.t-red`）⇒ 表现是
    「**那档颜色没生效**」，而另外三档好好的，看着像"颜色配错了"，
    其实是 CSS 根本没解析到那一条。

    ⚠ 更阴的是：**"写一行注释解释这件事"本身也会触发** ——
      只要那行字里出现闭标记，注释当场被截断（`theme.css` 里栽过一次，
      `style.css` 里又栽一次）。所以这条测试盯的是**文件本身**，不是"记得小心"。
    """

    def test_两份样式表都没有游离的注释标记(self):
        import re
        _files = [("style.css", self.raw_style), ("theme.css", self.raw_theme)]
        _files += [("themes/%s.css" % k, v) for k, v in sorted(self.raw_themes.items())]
        for name, raw in _files:
            with self.subTest(f=name):
                stripped = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
                self.assertEqual(stripped.count("*/"), 0,
                                 "%s 里有**游离的注释闭标记** —— 它会把后面第一条规则吞掉" % name)
                self.assertEqual(stripped.count("/*"), 0,
                                 "%s 里有没配对的注释开标记" % name)

    def test_四档的规则一条都没被吞(self):
        """上一条是"病因"，这条是"后果" —— 四条规则必须都真的在。"""
        for klass in ("t-red", "t-orange", "t-lg", "t-g"):
            with self.subTest(tier=klass):
                self.assertIn(".table-scroll td.%s .attain-cell" % klass, self.style)


class Test日夜自动主题与侧栏天气(_Base):
    """2026-09-22 用户：接进控制台，不做根目录独立页；日夜映射门店自定；
    检查别每分钟；天气放左下角门店信息旁。"""

    def setUp(self):
        super().setUp()
        self.js = (WEB / "app.js").read_text(encoding="utf-8")

    def test_设置页有自动开关和日夜映射(self):
        for eid in ("theme-auto", "theme-day", "theme-night", "theme-auto-status"):
            with self.subTest(id=eid):
                self.assertIn('id="%s"' % eid, self.html)

    def test_侧栏门店信息旁有天气槽(self):
        """塞在 .foot-info 里，不新增 data-foot 行。"""
        self.assertIn('id="foot-weather"', self.html)
        i = self.html.index('data-foot="account"')
        seg = self.html[i:self.html.index("</button>", i)]
        self.assertIn('id="store-line"', seg)
        self.assertIn('id="foot-weather"', seg)
        self.assertIn('class="foot-info', seg)

    def test_检查间隔不是每分钟(self):
        """用户：「每分钟检查有点狠了」⇒ 常量必须 ≥ 5 分钟。"""
        m = __import__("re").search(
            r"THEME_CHECK_MS\s*=\s*(\d+)\s*\*\s*60\s*\*\s*1000", self.js)
        self.assertIsNotNone(m, "找不到 THEME_CHECK_MS")
        minutes = int(m.group(1))
        self.assertGreaterEqual(
            minutes, 5,
            "日夜/天气检查间隔只有 %s 分钟 —— 用户明确不要每分钟级别" % minutes)

    def test_日夜映射只走_localStorage_无后端主题接口(self):
        for key in ("cbg-theme-auto", "cbg-theme-day", "cbg-theme-night"):
            with self.subTest(key=key):
                self.assertIn(key, self.js)
        # 执行规范 7.2：modules/theme 不提供 /api/theme
        # ⚠ 只盯**真的请求**（api('/api/theme… / fetch('…')），别整段搜
        #   字面量 —— 源码说明里会原样写出「不提供 /api/theme」。
        self.assertNotRegex(self.js, r"""(?:api|fetch)\(\s*['"]/api/theme""")
        self.assertNotIn("'/api/theme'", self.js)

    def test_根目录没有误建的独立演示页(self):
        """2026-09-22 第一版误在仓库根目录做了 index/styles/app 三件套，已删。"""
        for name in ("index.html", "styles.css", "app.js"):
            with self.subTest(f=name):
                self.assertFalse(
                    (ROOT / name).exists(),
                    "根目录不该再有独立演示页 %s（应接进 web/）" % name)

    def test_回前台会补判一次(self):
        self.assertIn("visibilitychange", self.js)
        self.assertIn("applyThemeSchedule", self.js)

    def test_手动切主题会关掉日夜自动(self):
        """2026-09-22 用户：「手动切了上面的主题时，下面的日夜自动切换就关掉」。

        ⚠ 只有**手动**（卡片 / 下拉）传 `{ manual: true }`；
          `applyThemeSchedule` / 首屏恢复不许传 —— 传了自动开一次就把自己关了。
        """
        self.assertIn("manual: true", self.js, "手动入口没标 manual")
        i = self.js.index("function setTheme(")
        blk = self.js[i:self.js.index("\n}", i)]
        self.assertIn("THEME_AUTO_KEY", blk, "setTheme 没碰自动开关")
        self.assertIn("opts.manual", blk)
        # 自动调度那条不许带 manual
        j = self.js.index("function applyThemeSchedule(")
        auto = self.js[j:self.js.index("\n}", j)]
        self.assertNotIn("manual: true", auto)
        # 手动入口两处都标了
        self.assertGreaterEqual(self.js.count("manual: true"), 2)


class Test自定义照片主题与壁纸(_Base):
    """2026-09-22 用户：主题设置加自定义背景壁纸；算一档「自定义照片主题」；
    上传到 web/wallpaper/；选中只记 localStorage `cbg-wallpaper`；
    接口 GET/POST/DELETE /api/wallpaper（只管文件，**不提供 /api/theme**）。"""

    def setUp(self):
        super().setUp()
        self.js = (WEB / "app.js").read_text(encoding="utf-8")
        self.photo = (self.themes_dir / "photo.css").read_text(encoding="utf-8")

    def test_有照片主题文件且链在_style_前(self):
        self.assertIn("photo", self.raw_themes)
        k = self.html.index('href="/themes/photo.css')
        j = self.html.index('href="/style.css')
        self.assertLess(k, j, "photo.css 必须排在 style.css 前面")
        self.assertIn('label: 自定义照片主题', self.photo)
        self.assertIn('body[data-theme="photo"]', self.photo)

    def test_卡片和三个下拉都有这一档(self):
        self.assertIn('data-theme-pick="photo"', self.html)
        self.assertIn("自定义照片主题", self.html)
        # theme-select / theme-day / theme-night 三处 option
        self.assertGreaterEqual(self.html.count('value="photo"'), 3,
                                "主题下拉 + 白天 + 夜晚都要能选照片主题")

    def test_设置页有壁纸上传区(self):
        for eid in ("wallpaper-file", "btn-wallpaper-upload",
                    "btn-wallpaper-clear", "wallpaper-status", "wallpaper-grid"):
            with self.subTest(id=eid):
                self.assertIn('id="%s"' % eid, self.html)

    def test_选中壁纸只走_cbg_wallpaper(self):
        self.assertIn("cbg-wallpaper", self.js)
        self.assertIn("const WALLPAPER_KEY = 'cbg-wallpaper'", self.js)
        self.assertIn("function applyWallpaper(", self.js)
        # setTheme 里要刷背景（切到照片主题立刻出图）
        i = self.js.index("function setTheme(")
        blk = self.js[i:self.js.index("\n}", i)]
        self.assertIn("applyWallpaper", blk)

    def test_只调_wallpaper_接口不调_theme_接口(self):
        self.assertIn("'/api/wallpaper'", self.js)
        # ⚠ 只盯**真的请求**（api('/api/…') / fetch('…')），别整段搜字面量
        self.assertNotRegex(self.js, r"""(?:api|fetch)\(\s*['"]/api/theme""")
        self.assertNotIn("'/api/theme'", self.js)

    def test_上传扩展名白名单在前端也有(self):
        for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            with self.subTest(ext=ext):
                self.assertIn("'%s'" % ext, self.js)

    def test_切页会拉壁纸列表(self):
        self.assertIn("loadWallpapers", self.js)
        i = self.js.index("theme: () =>")
        blk = self.js[i:self.js.index("\n", i)]
        self.assertIn("loadWallpapers", blk)
