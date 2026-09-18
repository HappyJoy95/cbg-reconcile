"""老定时任务「一键换掉」的回归（`schedule.replace_legacy` + 那个弹窗的判据）。

## 为什么这个文件的测试格外要紧

这一步**动的是门店的定时任务**。写错方向的后果不是"某个功能不好用"，
而是**门店从此再也不会自动跑** —— 而且**当天不会有人发现**：
要等第二天到点没出报告，才会有人觉得"今天怎么没推送"。

所以两条铁律各有一组测试盯着：

1. **先建后删** —— 新的建成了才允许动老的
2. **建失败 ⇒ 一个老的都不许动**（最坏的失败模式就是反着来）

⚠ 顺带钉住"弹窗判据在后端" —— 放前端的话，"弹过没"这种状态一定会漂。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import schedule as S                              # noqa: E402

WEB_PY = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

LEGACY_FULL = "\\CBG报量对账-21点20"
LEGACY_LEAF = "CBG报量对账-21点20"


class _Base(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        self.calls = []

    def _patch(self, *, install_ok=True, remove_ok=True,
               legacy=(LEGACY_FULL,)):
        """把"建"和"删"都换成记录器 —— 好断言**顺序**，而不只是结果。"""
        def fake_install(root, time_str="", days_ago=0, config="", name=None):
            self.calls.append("install")
            return {"ok": install_ok,
                    "message": "已注册" if install_ok else "错误: 拒绝访问。"}

        def fake_remove(full):
            self.calls.append("remove")
            return {"ok": remove_ok,
                    "message": "已删除" if remove_ok else "错误: 拒绝访问。"}

        return (
            mock.patch.object(S, "install", fake_install),
            mock.patch.object(S, "legacy_task_names", lambda root: list(legacy)),
            mock.patch.object(S, "kind", lambda: "windows"),
            mock.patch.object(S, "_win_remove", fake_remove),
            mock.patch.object(S, "_unix_remove", fake_remove),
        )

    def run_replace(self, **kw):
        ps = self._patch(**kw)
        for p in ps:
            p.start()
        try:
            return S.replace_legacy(self.root, "21:00", 1, "config/store-X.yaml")
        finally:
            for p in ps:
                p.stop()


class TestOrder(_Base):
    def test_先建后删(self):
        """⚠⚠ 顺序错了就是"门店再也不会自动跑"。"""
        res = self.run_replace()
        self.assertEqual(self.calls, ["install", "remove"], "必须是先建后删")
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], [LEGACY_LEAF])

    def test_没有老任务时也会建新的(self):
        res = self.run_replace(legacy=())
        self.assertEqual(self.calls, ["install"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["removed"], [])
        self.assertIn("本来就没有老任务", res["message"])


class TestInstallFailure(_Base):
    """⚠ 这组是整条需求的**护栏**：新的没建成，老的一个都不许动。"""

    def test_建失败就一个老的都不动(self):
        res = self.run_replace(install_ok=False)
        self.assertEqual(self.calls, ["install"], "建失败了还去删老的 —— 门店会啥都不剩")
        self.assertFalse(res["ok"])
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["legacy_before"], [LEGACY_FULL])

    def test_建失败的说明必须写清老的还在(self):
        """用户看到"没成"会慌；得明确告诉他**原来那条还在跑**。"""
        res = self.run_replace(install_ok=False)
        self.assertIn("一条都没动", res["message"])
        self.assertIn("没建成", res["message"])

    def test_建失败要把原因带出来(self):
        res = self.run_replace(install_ok=False)
        self.assertIn("拒绝访问", res["message"], "别只说'失败了'，原因要带上")


class TestRemoveFailure(_Base):
    def test_删不掉不影响新的(self):
        """⚠ 删不掉**不能**回头把新的也撤了 —— 新的才是门店要的那条。"""
        res = self.run_replace(remove_ok=False)
        self.assertEqual(self.calls, ["install", "remove"])
        self.assertFalse(res["ok"])
        self.assertTrue(res["installed"], "新的建成过，这个事实不能因为删不掉就改口")
        self.assertEqual(res["failed"], [LEGACY_LEAF])
        self.assertIn("一天跑两遍", res["message"])

    def test_部分删掉时两边都报(self):
        res = self.run_replace(legacy=(LEGACY_FULL, r"\CBG报量对账-21点00"))
        self.assertEqual(self.calls, ["install", "remove", "remove"])
        self.assertEqual(len(res["removed"]), 2)


class TestLegacyName(unittest.TestCase):
    def test_认得出老名字(self):
        self.assertTrue(S.is_legacy_name(LEGACY_FULL))
        self.assertTrue(S.is_legacy_name(LEGACY_LEAF))
        self.assertTrue(S.is_legacy_name("\\CBG报量对账-21点00"))

    def test_不误伤新名字(self):
        self.assertFalse(S.is_legacy_name("\\门店数据拉取与计算-21点00"))
        self.assertFalse(S.is_legacy_name(""))
        self.assertFalse(S.is_legacy_name("\\开机自启"))


class TestPrompt(unittest.TestCase):
    """弹窗判据必须在**后端** —— 前端记"弹过没"一定会漂。"""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name

    def _p(self, names):
        return mock.patch.object(S, "legacy_task_names", lambda root: list(names))

    def test_没有老任务就不弹(self):
        with self._p([]):
            self.assertFalse(S.legacy_prompt_pending(self.root)["show"])

    def test_有老任务且没弹过就弹(self):
        with self._p([LEGACY_FULL]):
            got = S.legacy_prompt_pending(self.root)
        self.assertTrue(got["show"])
        self.assertEqual(got["names"], [LEGACY_FULL])

    def test_弹过就不再弹(self):
        with self._p([LEGACY_FULL]):
            S.mark_legacy_prompted(self.root)
            self.assertFalse(S.legacy_prompt_pending(self.root)["show"],
                             "同一版弹过一次就够了")

    def test_老任务还在但换了一版还会再弹(self):
        """⚠ 用户"稍后再说"之后一直没处理 —— 下一版得再提醒一次。"""
        with self._p([LEGACY_FULL]):
            S.mark_legacy_prompted(self.root)
            p = S._prompt_path(self.root)
            p.write_text(json.dumps({"version": "0.0.1-old"}), encoding="utf-8")
            self.assertTrue(S.legacy_prompt_pending(self.root)["show"])

    def test_标记文件坏了当没弹过(self):
        with self._p([LEGACY_FULL]):
            p = S._prompt_path(self.root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{ 这不是 json", encoding="utf-8")
            self.assertTrue(S.legacy_prompt_pending(self.root)["show"])

    def test_老任务全没了就不再弹(self):
        with self._p([LEGACY_FULL]):
            S.mark_legacy_prompted(self.root)
        with self._p([]):
            self.assertFalse(S.legacy_prompt_pending(self.root)["show"])


class TestWiring(unittest.TestCase):
    def test_后端两个接口都在(self):
        self.assertIn('"/api/schedule/replace"', WEB_PY)
        self.assertIn('"/api/schedule/legacy-prompt/seen"', WEB_PY)
        self.assertIn('"legacy_prompt"', WEB_PY, "概览得把判据给前端")

    def test_先普通权限试_不行才提权(self):
        """⚠ 首选普通权限：建出来的任务归当前用户，以后读改删都不用管理员。"""
        i_replace = WEB_PY.index('"/api/schedule/replace"')
        seg = WEB_PY[i_replace:i_replace + 2600]
        self.assertLess(seg.index("schedule.replace_legacy"), seg.index("run_elevated"),
                        "得先用普通权限试一次")

    def test_提权走的是_schedule_replace_一个子命令(self):
        """⚠ 一个 UAC 里做完"建新的 + 删老的" —— 分成两次会弹两次 UAC，
        而且中间那次失败会留下'删了没建'的烂摊子。"""
        i = WEB_PY.index('"/api/schedule/replace"')
        self.assertIn('"schedule-replace"', WEB_PY[i:i + 2600])

    def test_前端有弹窗和按钮(self):
        self.assertIn('id="legacy-mask"', INDEX)
        self.assertIn('id="btn-legacy-fix"', INDEX)
        self.assertIn("renderLegacyPrompt", APP_JS)
        self.assertIn("/api/schedule/replace", APP_JS)

    def test_前端不自动提权(self):
        """⚠ 服务是无人值守的 —— 只有用户点按钮才提权，绝不能自动弹 UAC。"""
        i = APP_JS.index("btn-legacy-fix")
        seg = APP_JS[i:i + 300]
        self.assertNotIn("auto", seg.lower().replace("automatic", ""),
                         "别在加载时就调提权")


if __name__ == "__main__":
    unittest.main()
