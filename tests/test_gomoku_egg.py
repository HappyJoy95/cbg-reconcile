"""彩蛋五子棋：遮罩 / 资源 / 触发 / 挂载入口 的静态钉子。

设计：`.dsh/docs/2026-09-22-彩蛋五子棋-设计.md`
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js", "app.js"))
GAME_JS = ROOT / "web" / "gomoku" / "game.js"
GAME_CSS = ROOT / "web" / "gomoku" / "style.css"


class TestGomokuEgg(unittest.TestCase):
    def test_mask_exists_and_hidden(self):
        m = re.search(r'id="gomoku-mask"[^>]*', INDEX)
        self.assertIsNotNone(m, "index.html 要有 #gomoku-mask")
        self.assertIn("hidden", m.group(0), "遮罩默认必须 hidden")

    def test_assets_exist_and_referenced(self):
        self.assertTrue(GAME_JS.is_file(), "缺 web/gomoku/game.js")
        self.assertTrue(GAME_CSS.is_file(), "缺 web/gomoku/style.css")
        self.assertIn("/gomoku/style.css", INDEX)
        self.assertIn("/gomoku/game.js", INDEX)

    def test_trigger_counts_status_store_name(self):
        """触发面 = 右下角状态抽屉 #status-rows 里的「门店名称」行（委托点击）。"""
        self.assertIn("#status-rows", APP_JS)
        self.assertIn("门店名称", APP_JS)
        self.assertIn("gomoku", APP_JS.lower())
        # 不许再绑左下角 side-foot / store-line 当触发面
        self.assertNotIn("gomokuEggRow", APP_JS)

    def test_no_auto_new_on_load(self):
        src = GAME_JS.read_text(encoding="utf-8")
        # 加载即开局的老写法不许回来
        self.assertNotRegex(
            src,
            r"(?m)^const game\s*=\s*new GomokuGame\(\)\s*;?\s*$",
            "不许脚本一加载就 new GomokuGame",
        )
        self.assertIn("mountGomoku", src)
        self.assertIn("destroyGomoku", src)

    def test_localstorage_prefixed(self):
        src = GAME_JS.read_text(encoding="utf-8")
        self.assertIn("cbg-gomoku-", src)
        self.assertNotIn("'gomoku_ai_knowledge'", src)
        self.assertNotIn('"gomoku_ai_knowledge"', src)

    def test_style_scoped_under_mask(self):
        css = GAME_CSS.read_text(encoding="utf-8")
        # 裸 * / body 不许（会污染控制台）
        self.assertNotRegex(css, r"(?m)^\s*\*\s*\{", "不许全局 *")
        self.assertNotRegex(css, r"(?m)^body\s*\{", "不许 body 选择器")
        self.assertIn("#gomoku-mask", css)


if __name__ == "__main__":
    unittest.main()
