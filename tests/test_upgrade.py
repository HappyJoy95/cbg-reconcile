"""升级记录 + 大版本升级推送提醒。

用户 2026-09-16 提的："能不能做个升级记录，检测到是 1.x.x 的版本升到 2.x.x
就推送这个提醒"。

**为什么光有控制台弹窗不够**：弹窗只有有人打开控制台才看得到，而门店的日常是
"它自己跑，我不看"。大版本升级恰恰带着**必须做的事**（2.0.0 那次是
"不删旧定时任务就一天跑两遍"）。**看不到 = 没做 = 出事。**

判据（都在 `upgrade.should_push` 一处，别散到调用点）：
* 大版本变了（1.x → 2.x）→ 一定推；
* 小版本但带着还没看过的待办 → 也推（待办不推等于没有）；
* 同一版只推一次。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import upgrade, version, whatsnew

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _root(**state):
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    if state:
        p = root / upgrade.STATE_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return tmp, root


class TestUpgradeDetection(unittest.TestCase):
    def test_版本号取大版本(self):
        self.assertEqual(upgrade.major("2.0.1"), "2")
        self.assertEqual(upgrade.major("10.3"), "10")

    def test_拿不到的大版本不算数(self):
        """⚠ 状态文件坏了（`"?"`、`"beta"`）时，返回 `"?"` 会让判据成立，
        于是给门店推一条"你从 ? 升到了 2.0.0"。宁可当作"不知道"。"""
        for bad in ("", None, "?", "beta", "v2"):
            with self.subTest(bad=bad):
                self.assertEqual(upgrade.major(bad), "")
                self.assertFalse(upgrade.is_major_jump(bad, "2.0.0"))

    def test_只有大版本变了才算大版本升级(self):
        self.assertTrue(upgrade.is_major_jump("1.6.1", "2.0.0"))
        self.assertTrue(upgrade.is_major_jump("1.4.11", "2.0.0"))
        self.assertFalse(upgrade.is_major_jump("2.0.0", "2.0.1"))
        self.assertFalse(upgrade.is_major_jump("2.0.0", "2.9.9"))

    def test_第一次记录不算升级(self):
        """⚠ 刚装上的机器没有 `from` —— 不该推"你从 ? 升到了 2.0.0"。

        而且**这功能刚上线时所有门店都是"第一次记录"**，
        判错的话会一次性给所有门店推一条莫名其妙的提醒。
        """
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        got = upgrade.record(root, "2.0.0")
        self.assertTrue(got["first"])
        self.assertFalse(got["major"])
        self.assertIsNone(upgrade.should_push(root, "2.0.0", got))

    def test_同一版不重复记(self):
        tmp, root = _root(running="2.0.0")
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(upgrade.record(root, "2.0.0"))

    def test_真升级会记一条(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        got = upgrade.record(root, "2.0.0")
        self.assertEqual(got["from"], "1.6.1")
        self.assertTrue(got["major"])
        self.assertFalse(got["first"])
        self.assertEqual(upgrade.load(root)["running"], "2.0.0")

    def test_读坏了当没有_不崩(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        p = root / upgrade.STATE_REL
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{{{ 不是 json", encoding="utf-8")
        self.assertEqual(upgrade.load(root), {})
        self.assertIsNotNone(upgrade.record(root, "2.0.0"))    # 当成第一次


class TestShouldPush(unittest.TestCase):
    def test_大版本升级一定推(self):
        tmp, root = _root(running="2.0.0")
        self.addCleanup(tmp.cleanup)
        why = upgrade.should_push(root, "2.0.0",
                                  {"from": "1.6.1", "to": "2.0.0", "major": True})
        self.assertTrue(why)
        self.assertIn("大版本", why)

    def test_小版本没待办就不推(self):
        tmp, root = _root(running="2.0.1")
        self.addCleanup(tmp.cleanup)
        fake = {"2.0.0": whatsnew.NOTES["2.0.0"],
                "2.0.1": {"title": "小修", "highlights": ["修了个东西"], "todo": []}}
        with mock.patch.object(whatsnew, "NOTES", fake):
            self.assertIsNone(upgrade.should_push(
                root, "2.0.1", {"from": "2.0.0", "to": "2.0.1", "major": False}))

    def test_小版本但有待办也推(self):
        """⚠ 待办**不推出去等于没有**。"""
        tmp, root = _root(running="2.0.1")
        self.addCleanup(tmp.cleanup)
        fake = {"2.0.0": whatsnew.NOTES["2.0.0"],
                "2.0.1": {"title": "小修", "highlights": ["x"],
                          "todo": [{"text": "**去做一件新的事**，做完就好了。",
                                    "go": "settings"}]}}
        with mock.patch.object(whatsnew, "NOTES", fake):
            why = upgrade.should_push(
                root, "2.0.1", {"from": "2.0.0", "to": "2.0.1", "major": False})
        self.assertTrue(why)

    def test_同一版只推一次(self):
        tmp, root = _root(running="2.0.0", pushed="2.0.0")
        self.addCleanup(tmp.cleanup)
        self.assertIsNone(upgrade.should_push(
            root, "2.0.0", {"from": "1.6.1", "to": "2.0.0", "major": True}))


class TestMessageContents(unittest.TestCase):
    """⚠ **推送的全部意义就是「需要你做的事」那一段。**

    踩过一次：`digest` 用「看过没」决定待办范围，门店点过控制台的「知道了」
    之后那一段**整个是空的** —— 而推送本身照发。
    **看过 ≠ 做完了。**
    """

    def test_待办在最前面(self):
        notes = whatsnew.digest(ROOT, version.VERSION, since="1.6.1")
        subj, body = upgrade.build_message("青岛店", {"from": "1.6.1", "to": "2.0.0"}, notes)
        self.assertIn("需要你做的事", body)
        self.assertLess(body.index("需要你做的事"), body.index("改了什么"),
                        "门店扫一眼就该看到「我得干什么」，不是先读六条改动")

    def test_点过知道了也照样带待办(self):
        """⚠ 这条就是那个 bug 的回归测试。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        whatsnew.mark_seen(root, version.VERSION)          # 控制台点过「知道了」
        self.assertIsNone(whatsnew.pending(root, version.VERSION))
        notes = whatsnew.digest(root, version.VERSION, since="1.6.1")
        self.assertTrue(notes["todo"], "点过弹窗之后，推送里的待办不能是空的")
        _, body = upgrade.build_message("青岛店",
                                        {"from": "1.6.1", "to": "2.0.0"}, notes)
        self.assertIn("需要你做的事", body)

    def test_主题说清从哪版到哪版(self):
        notes = whatsnew.digest(ROOT, version.VERSION, since="1.6.1")
        subj, _ = upgrade.build_message("青岛店", {"from": "1.6.1", "to": "2.0.0"}, notes)
        self.assertIn("1.6.1", subj)
        self.assertIn("2.0.0", subj)

    def test_notes_为_None_也不崩(self):
        upgrade.build_message("青岛店", {"from": "1.6.1", "to": "2.0.0"}, None)
        upgrade._markdown("青岛店", {"from": "1.6.1", "to": "2.0.0"}, None)


class TestCheckNeverBreaksTheDailyFlow(unittest.TestCase):
    """⚠ 升级提醒是**锦上添花** —— 为了它把每天的对账搞失败是本末倒置。"""

    def test_推送炸了也不抛(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(upgrade, "notify", side_effect=RuntimeError("网不通")):
            res = upgrade.check(root, {}, "2.0.0")         # 不许抛
        self.assertIn("出错", res["result"])
        self.assertFalse(res["pushed"])

    def test_状态写不成也不抛(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("只读")):
            res = upgrade.check(root, None, "2.0.0")       # 不许抛
        self.assertTrue(res["checked"])

    def test_没有配置就跳过推送(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        res = upgrade.check(root, None, "2.0.0")
        self.assertEqual(res["result"], "没有配置，跳过推送")
        self.assertFalse(res["pushed"])

    def test_推送全跳过时不记_pushed(self):
        """⚠ 门店当时没配邮箱、后来又配了 —— 这条提醒还得能收到。

        全跳过/全失败时记了 `pushed` 的话，就**永远收不到了**。
        """
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(upgrade, "notify",
                               return_value=("邮件跳过（没开）、企微跳过（没开）", [])):
            res = upgrade.check(root, {}, "2.0.0")
        self.assertFalse(res["pushed"])
        self.assertNotIn("pushed", upgrade.load(root))
        # 下次还会再判一次
        self.assertTrue(upgrade.should_push(
            root, "2.0.0", {"from": "1.6.1", "to": "2.0.0", "major": True}))

    def test_真发出去了才记_pushed(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(upgrade, "notify", return_value=("邮件", ["邮件"])):
            res = upgrade.check(root, {}, "2.0.0")
        self.assertTrue(res["pushed"])
        self.assertEqual(upgrade.load(root)["pushed"], "2.0.0")

    def test_没升级就什么都不做(self):
        tmp, root = _root(running="2.0.0")
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(upgrade, "notify") as m:
            res = upgrade.check(root, {}, "2.0.0")
        self.assertFalse(m.called)
        self.assertIsNone(res["change"])


class TestUpgradeHistory(unittest.TestCase):
    def test_第一次记录不进历史显示(self):
        """⚠ 第一条例是 `from: ""`（刚装上）——
        显示成"从 ? 升到 2.0.0"会让人以为出过问题。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        upgrade.record(root, "2.0.0")
        self.assertEqual(upgrade.history(root), [])

    def test_真升级才显示(self):
        tmp, root = _root(running="1.6.1")
        self.addCleanup(tmp.cleanup)
        upgrade.record(root, "2.0.0")
        got = upgrade.history(root)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["from"], "1.6.1")
        self.assertEqual(got[0]["to"], "2.0.0")
        self.assertTrue(got[0]["at"])

    def test_只留最近若干条(self):
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        for i in range(upgrade.KEEP_HISTORY + 8):
            upgrade.record(root, "V%d" % i)
        self.assertLessEqual(len(upgrade.load(root)["history"]), upgrade.KEEP_HISTORY)

    def test_状态存在_secrets_下(self):
        self.assertTrue(upgrade.STATE_REL.startswith(".secrets/"))
        from src import selfupdate
        self.assertIn(".secrets", selfupdate.NEVER_TOUCH)


class TestWiring(unittest.TestCase):
    def test_daily_会检测(self):
        """⚠ 挂在 `daily` 上是因为**这是每天都会跑的那条** ——
        门店更新完，第二天的定时任务就会检测到并把提醒推出去。"""
        import inspect
        from src import cli
        src = inspect.getsource(cli.cmd_daily)
        self.assertIn("_upgrade_check", src)
        self.assertIn("_upgrade_check", inspect.getsource(cli.cmd_serve))

    def test_检测不会拖垮主流程(self):
        """连 `load_config` 都可能抛 —— `_upgrade_check` 必须全吞。"""
        from src import cli
        with mock.patch.object(cli, "load_config", side_effect=SystemExit("配置没找到")):
            res = cli._upgrade_check("config/nope.yaml")     # 不许抛
        self.assertIn("checked", res)

    def test_控制台显示升级记录(self):
        for i in ("upgrade-history",):
            with self.subTest(id=i):
                self.assertIn('id="%s"' % i, INDEX_HTML)
        self.assertIn("renderUpgrades", APP_JS)
        self.assertIn("state.overview.upgrades", APP_JS)


if __name__ == "__main__":
    unittest.main()
