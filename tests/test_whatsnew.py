"""更新日志弹窗 —— 更新后第一次打开控制台弹一次。

用户 2026-09-16："每次更新第一次启动加个更新日志的弹窗，然后给门店强调一下要做啥"。

两条设计约束，测试逐条钉住：

1. **内容必须在代码里（`src/whatsnew.py`），不能读 `发布说明.md`** ——
   那个文件**不在 git 里**，走自更新的门店拿到的永远是当初拷包那一版，
   弹出来会是"上一版的更新日志"（比没有更糟）。
2. **升 VERSION 就必须补一条日志** —— 忘了补的话弹窗是空的，
   而"更新了却什么都没说"正是这个弹窗要消灭的东西。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import version, whatsnew

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def _root():
    tmp = tempfile.TemporaryDirectory()
    return tmp, Path(tmp.name)


class TestEveryVersionHasNotes(unittest.TestCase):
    """⚠⚠ **这个文件里最要紧的一条。**

    弹窗的内容是手写的。忘了给新版本补一条的话，弹窗就是空的 ——
    而"更新了却什么都没说"正是这个功能要消灭的东西。
    """

    def test_当前版本必须写了更新日志(self):
        self.assertIsNotNone(
            whatsnew.notes_for(version.VERSION),
            "升级到 v%s 了，但 src/whatsnew.py 里没写这一版的更新日志 ——"
            " 门店更新后会弹一个空窗" % version.VERSION)

    def test_当前版本要有改动列表(self):
        got = whatsnew.notes_for(version.VERSION)
        self.assertTrue(got.get("highlights"), "这一版没写「改了什么」")

    def test_每一条要做的事都能照着做(self):
        """⚠ 用户的原话是"**给门店强调一下要做啥**"。

        写一句"请注意"等于没说 —— 每条都得是一句能照着做的动作。

        ⚠ 看的是**一台真升级的机器会看到什么**（`since` 给一个老版本），
        不是"最新那一版自己写没写待办" —— 有的版本（比如 2.0.1）本身没新增待办，
        但会把前面版本没做完的带上来（`versions_after`）。
        要求"每一版都必须有待办"是错的，那会逼着人硬凑。
        """
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        got = whatsnew.digest(root, version.VERSION, since="1.6.1")
        todos = got.get("todo") or []
        self.assertTrue(todos, "从 1.6.1 升上来一条待办都没有 —— 这个弹窗就只剩通知了")
        for t in todos:
            with self.subTest(t=t["text"][:30]):
                self.assertGreaterEqual(len(t["text"]), 25,
                                        "太短了，门店看不出要做什么")

    def test_要做的事都能跳到对的页(self):
        for v, got in whatsnew.NOTES.items():
            for t in got.get("todo") or ():
                with self.subTest(v=v, go=t.get("go")):
                    self.assertIn(t.get("go"), whatsnew.TABS,
                                  "go 必须是已知的标签页 id")

    def test_标签页_id_和前端对得上(self):
        """⚠ 后端给的 `go` 要能真的切到那个页 —— 对不上就是"点了没反应"。"""
        for tab in whatsnew.TABS:
            with self.subTest(tab=tab):
                self.assertIn('data-tab="%s"' % tab, INDEX_HTML)
                self.assertIn('id="panel-%s"' % tab, INDEX_HTML)


class TestPending(unittest.TestCase):
    def test_第一次打开要弹(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        got = whatsnew.pending(root, version.VERSION)
        self.assertIsNotNone(got)
        self.assertEqual(got["version"], version.VERSION)
        self.assertTrue(got["highlights"])
        self.assertTrue(got["todo"])

    def test_看过之后不再弹(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        whatsnew.mark_seen(root, version.VERSION)
        self.assertIsNone(whatsnew.pending(root, version.VERSION))

    def test_升级到新版本会再弹一次(self):
        """⚠ 这才是"每次更新第一次启动"—— 记的是**版本号**，不是一个布尔开关。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        whatsnew.mark_seen(root, "1.6.1")
        self.assertIsNotNone(whatsnew.pending(root, version.VERSION))

    def test_没写日志的版本不弹空壳(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(whatsnew.pending(root, "0.0.0-不存在"))

    def test_状态文件坏了当没看过(self):
        """⚠ 读坏了必须当成"没看过"（再弹一次），**不能崩** ——
        为了一个提示把控制台首页搞挂是不划算的。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        p = root / whatsnew.STATE_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ 这不是 json", encoding="utf-8")
        self.assertEqual(whatsnew.seen_version(root), "")
        self.assertIsNotNone(whatsnew.pending(root, version.VERSION))

    def test_写不成也不抛(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("磁盘只读")):
            self.assertFalse(whatsnew.mark_seen(root, "2.0.0"))   # 不许抛

    def test_状态存在_secrets_下(self):
        """⚠ 必须放 `.secrets/` —— 那是 `selfupdate.NEVER_TOUCH` 里的，
        升级不会碰它。放别处的话每次自更新都会把"看过了"抹掉，每次开都弹。"""
        self.assertTrue(whatsnew.STATE_REL.startswith(".secrets/"))
        from src import selfupdate
        self.assertIn(".secrets", selfupdate.NEVER_TOUCH)

    def test_按钮文案由后端给(self):
        """前端不自己维护"go → 标签名"的表（各写一份必然有一天对不上）。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        got = whatsnew.pending(root, version.VERSION)
        for t in got["todo"]:
            with self.subTest(go=t["go"]):
                self.assertTrue(t["go_label"])
                self.assertTrue(t["go_label"].startswith("去"))


class TestContentIsShippable(unittest.TestCase):
    """内容跟着**代码**走 —— 自更新的门店必须拿得到。"""

    def test_在_src_下(self):
        self.assertTrue((ROOT / "src" / "whatsnew.py").is_file())

    def test_不依赖发布说明文件(self):
        """⚠ `发布说明.md` **不在 git 里**（打包时生成）——
        自更新的门店拿到的永远是当初拷包那一版。
        拿它当弹窗内容 = 弹"上一版的更新日志"。

        ⚠ 只看**代码部分**：模块开头的注释里**故意**解释了"为什么不读它"
        （第一版把注释也算进去了，于是这条测试自己把自己绊倒）。
        """
        import ast
        tree = ast.parse((ROOT / "src" / "whatsnew.py").read_text(encoding="utf-8"))
        # 去掉模块 docstring，剩下的是真代码
        body = tree.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        code = "\n".join(ast.dump(n) for n in body)
        self.assertNotIn("发布说明.md", code, "代码里引用了那个不在 git 里的文件")
        self.assertNotIn(".md", code, "代码里读了 .md 文件")
        # ⚠ 只查**路径引用**（带扩展名的那种）。文案里提一句"发布说明"
        #   是给人看的正常内容，不是依赖 —— 第一版把这也拦了，
        #   于是给 2.0.1 写更新日志时自己把自己绊倒。

    def test_没有任何文件读取(self):
        """⚠ 内容必须在**代码**里。读任何外部文件都可能读到过期的东西 ——
        唯一允许的是它自己那个"看过没"的状态文件（只读 `.secrets/whatsnew.json`）。"""
        import inspect
        src = inspect.getsource(whatsnew)
        self.assertNotIn("open(", src)
        self.assertNotIn("read_text", src.replace(
            "_state_path(root).read_text", ""))

    def test_2_0_0_的日志里说了那几件必须做的事(self):
        """升级到 2.0.0 门店真需要知道的三件事，一条都不能少。"""
        got = whatsnew.notes_for("2.0.0")
        blob = " ".join(t["text"] for t in got["todo"])
        for needle, why in (("定时任务", "任务改名后会并存，一天跑两遍"),
                            ("删", "得说清是「删除」这个动作"),
                            ("历史", "第一次跑会补全部历史，要等"),
                            ("会话", "没收到报告多半是会话过期")):
            with self.subTest(needle=needle):
                self.assertIn(needle, blob, "2.0.0 的「要做的事」里没提：%s" % why)

    def test_改名的对照表写进了改动列表(self):
        """门店最容易被"说法变了"搞糊涂，得写出来。"""
        got = whatsnew.notes_for("2.0.0")
        blob = " ".join(got["highlights"])
        self.assertIn("玲珑无但云商有", blob)
        self.assertIn("玲珑有但云商无", blob)
        self.assertIn("玲珑", blob)


class TestModalWiring(unittest.TestCase):
    def test_骨架那几个_id_都在(self):
        for i in ("whatsnew-mask", "wn-head", "wn-title", "wn-highlights",
                  "wn-todo-sec", "wn-todo", "btn-wn-ok"):
            with self.subTest(id=i):
                self.assertIn('id="%s"' % i, INDEX_HTML)

    def test_默认是藏着的(self):
        """⚠ 忘了加 `hidden` 的话，没更新日志的机器会一进来就看到一个空弹窗。"""
        i = INDEX_HTML.index('id="whatsnew-mask"')
        self.assertIn("hidden", INDEX_HTML[i - 40:i + 40])

    def test_前端不自己判断弹不弹(self):
        """决定权在后端（`overview.whatsnew` 为 null 就不弹）——
        状态放前端一定会漂。"""
        self.assertIn("state.overview.whatsnew", APP_JS)

    def test_要做什么那段有独立的容器和样式(self):
        """⚠ 用户要的是"**强调**一下要做啥" —— 视觉权重必须比"改了什么"高。

        门店不看改动没关系；漏做那几步会真的出问题（旧任务没删 = 一天跑两遍）。
        """
        self.assertIn('class="todo-box"', INDEX_HTML)
        self.assertIn(".todo-box", STYLE_CSS)
        self.assertIn(".todo-num", STYLE_CSS)

    def test_每条能跳到该去的页(self):
        self.assertIn("data-go", APP_JS)
        self.assertIn("function goto(", APP_JS)

    def test_弹窗里的文字先转义再做粗体(self):
        """⚠ 顺序反了就是自己开一个 XSS 口子。

        （`table()` 那个「忘了包 `{html:}`」的坑是"显示成源码"，这个是"注进去"。
        两回事，别混。）
        """
        i = APP_JS.index("function inlineMd(")
        seg = APP_JS[i:i + 260]
        self.assertLess(seg.index("esc("), seg.index(".replace("),
                        "必须先 esc 再替换成 HTML")


if __name__ == "__main__":
    unittest.main()


class TestSkippingVersions(unittest.TestCase):
    """⚠ **门店会跳版本升级**：一台还在 1.6.1 的电脑，某天直接更新到 2.0.1。

    如果弹窗只看当前版本的日志，它就**看不到 2.0.0 里那几条"必须做的事"**
    （删掉改名前的旧定时任务、第一次跑会补历史）—— 而那几条恰恰最要紧。

    所以待办按**版本区间**合并：上次看过的版本之后、一直到当前版本，全都算上。
    """

    def test_版本号能比大小(self):
        self.assertLess(whatsnew._vkey("1.6.1"), whatsnew._vkey("2.0.0"))
        self.assertLess(whatsnew._vkey("2.0.0"), whatsnew._vkey("2.0.1"))
        self.assertLess(whatsnew._vkey("2.0.0"), whatsnew._vkey("2.10.0"))
        self.assertEqual(whatsnew._vkey("2.0.1"), (2, 0, 1))

    def test_怪版本号不崩(self):
        for v in ("", None, "beta", "2.0.0-beta", "1.x"):
            with self.subTest(v=v):
                self.assertIsInstance(whatsnew._vkey(v), tuple)

    def test_跳版本时把中间版本的待办也带上(self):
        """伪造一个 2.0.1，验证 1.6.1 的机器升上去时**两个版本的待办都在**。"""
        fake = dict(whatsnew.NOTES)
        fake["2.0.1"] = {
            "title": "小修",
            "highlights": ["修了个东西"],
            "todo": [{"text": "**看一眼这个新东西**，确认没问题就行，别的不用管。",
                      "go": "reports"}],
        }
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(whatsnew, "NOTES", fake):
            # 一台从没看过日志的机器（比如还在 1.6.1）
            got = whatsnew.pending(root, "2.0.1")
            self.assertIsNotNone(got)
            self.assertEqual(got["highlights"], ["修了个东西"],
                             "「改了什么」只讲当前版本")
            todos = [t["text"] for t in got["todo"]]
            self.assertTrue(any("定时任务" in t for t in todos),
                            "2.0.0 的待办没带上 —— 跳版本的门店会漏做")
            self.assertTrue(any("看一眼这个新东西" in t for t in todos))
            # 看过 2.0.0 的机器升到 2.0.1：只带 2.0.1 的
            whatsnew.mark_seen(root, "2.0.0")
            got2 = whatsnew.pending(root, "2.0.1")
            self.assertEqual(len(got2["todo"]), 1)
            self.assertIn("看一眼这个新东西", got2["todo"][0]["text"])

    def test_每条待办带上来历(self):
        """`from` 让界面能说清"这条是哪个版本留下的"。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        got = whatsnew.pending(root, version.VERSION)
        for t in got["todo"]:
            with self.subTest(t=t["text"][:20]):
                self.assertIn(t["from"], whatsnew.NOTES)
