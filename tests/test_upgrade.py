"""升级记录 + 大版本升级推送提醒。

用户 2026-09-16 提的："能不能做个升级记录，检测到是 1.x.x 的版本升到 2.x.x
就推送这个提醒"。

**为什么光有控制台弹窗不够**：弹窗只有有人打开控制台才看得到，而门店的日常是
"它自己跑，我不看"。大版本升级恰恰带着**必须做的事**（2.0.0 那次是
"不删旧定时任务就一天跑两遍"）。**看不到 = 没做 = 出事。**

判据（都在 `upgrade.should_push` 一处，别散到调用点）：
* 大版本变了 → 一定推。⚠ 「大版本」由 `upgrade.MAJOR_ABOVE` **手工声明**
  （用户 2026-09-23：「大版本靠我定义吧，不靠版本号」）——
  版本号现在是时间戳 `yy.mmdd.hhmmss`，拆第 1 段会把年份当年份大版本、年年误推；
* 小版本但带着还没看过的待办 → 不推（待办靠控制台弹窗，2026-09-17 定的）；
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
    """大版本 = **跨过 `MAJOR_ABOVE` 里手工声明的边界**，不看版本号形状
    （用户 2026-09-23：「大版本靠我定义吧，不靠版本号」）。
    判据函数一律在 patch 过的清单上测 —— 生产清单会随发版追加，别钉死它。"""

    def test_跨过声明的边界才算大版本(self):
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("2.1.1", "3.0.0")]):
            # 线上 2.1.1 升到时间戳号 / 老机器 1.6.1 直接跳 —— 都跨线
            self.assertTrue(upgrade.is_major_jump("2.1.1", "26.0923.153045"))
            self.assertTrue(upgrade.is_major_jump("1.6.1", "26.0923.153045"))
            # 线内的普通升级、时间戳号之间往前走 —— 谁也没跨线
            self.assertFalse(upgrade.is_major_jump("2.0.0", "2.1.0"))
            self.assertFalse(upgrade.is_major_jump("26.0923.153045",
                                                   "26.0924.090000"))

    def test_年份跳动不算大版本(self):
        """⚠ 时间戳号第 1 段是**年份** —— 按老办法拆第 1 段比，
        2027 年第一个包（26→27）会被判成大版本、年年白推一条。"""
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("2.1.1", "3.0.0")]):
            self.assertFalse(upgrade.is_major_jump("26.1231.235959",
                                                   "27.0101.090000"))

    def test_代号跟出来(self):
        """推送文案要带代号 —— 时间戳号 `26.0923.x` 对门店没意义，`3.0.0` 才看得懂。"""
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("2.1.1", "3.0.0")]):
            self.assertEqual(upgrade.major_label("2.1.1", "26.0923.153045"), "3.0.0")
            self.assertEqual(upgrade.major_label("26.0923.153045",
                                                 "26.0924.090000"), "")

    def test_拿不到的版本号不算数(self):
        """⚠ 状态文件坏了（`""`、`"?"`、`"beta"`）时 `parse_version` 一律给
        `(0,0,0)`，会排到所有边界**之前**判成"跨了大版本" ——
        于是给门店推一条"你从 ? 升到了 …"。宁可当作"不知道"。"""
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("2.1.1", "3.0.0")]):
            for bad in ("", None, "?", "beta", "v2"):
                with self.subTest(bad=bad):
                    self.assertEqual(upgrade.major_label(bad, "26.0923.153045"), "")
                    self.assertFalse(upgrade.is_major_jump(bad, "26.0923.153045"))
                    self.assertFalse(upgrade.is_major_jump("2.1.1", bad))

    def test_生产清单成形(self):
        """手写清单没人拦得住，测试兜个底：每条得是 `(能比的边界, 非空代号)`。"""
        self.assertTrue(upgrade.MAJOR_ABOVE, "清单不能是空的 —— 空了大版本永远不推")
        for bound, name in upgrade.MAJOR_ABOVE:
            with self.subTest(bound=bound):
                self.assertRegex(str(bound), r"^\d", "边界得是能比大小的版本号")
                self.assertTrue(str(name).strip(), "代号不能为空")

    def test_首条边界是_2_1_1(self):
        """钉住用户 2026-09-23 定的首条边界：线上 2.1.1 升到时间戳号 = 大版本 2.2.0。"""
        self.assertTrue(upgrade.is_major_jump("2.1.1", "26.0923.153045"))
        # 代号也钉住：用户当天把这一版的号定为 2.2.0（beta 要用对号），
        # 代号必须跟着它 —— 推送里写着「3.0.0」而实际升的是 2.2.0 会自相矛盾。
        self.assertEqual(upgrade.major_label("2.1.1", "26.0923.153045"), "2.2.0")

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
        # 1.6.1 → 2.0.0 要被记成"大版本"，得先把边界声明到这条线上
        # （生产清单首条是 2.1.1，这条升级在它之下、不算大版本 —— 判据是手工声明的）
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("1.9.9", "2.0.0")]):
            got = upgrade.record(root, "2.0.0")
        self.assertEqual(got["from"], "1.6.1")
        self.assertTrue(got["major"])
        self.assertEqual(got["major_label"], "2.0.0")
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
        self.assertTrue(why, "大版本升级必须推")
        self.assertIn("大版本", why)

    def test_推送理由带代号(self):
        """理由里带上手工声明的代号 —— `26.0923.153045` 对门店没意义，`3.0.0` 才看得懂。"""
        tmp, root = _root(running="2.1.1")
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(upgrade, "MAJOR_ABOVE", [("2.1.1", "3.0.0")]):
            change = upgrade.record(root, "26.0923.153045")
        why = upgrade.should_push(root, "26.0923.153045", change)
        self.assertIn("大版本", why)
        self.assertIn("3.0.0", why)

    def test_小版本没待办就不推(self):
        tmp, root = _root(running="2.0.1")
        self.addCleanup(tmp.cleanup)
        fake = {"2.0.0": whatsnew.NOTES["2.0.0"],
                "2.0.1": {"title": "小修", "highlights": ["修了个东西"], "todo": []}}
        with mock.patch.object(whatsnew, "NOTES", fake):
            self.assertIsNone(upgrade.should_push(
                root, "2.0.1", {"from": "2.0.0", "to": "2.0.1", "major": False}))

    def test_小版本不推(self):
        """⚠ **升级提醒只在大版本推** —— 用户 2026-09-17 实测后定的。

        原先这里还有一条"小版本只要带了待办也推"（理由是"待办不推出去等于没有"），
        结果 2.0.1 → 2.1.0 这种普通升级也发一封邮件出来，用户当场指出
        「升级提醒不用推送吧」。
        要做的事**控制台弹窗照旧会讲** —— 那是每次更新都弹的，不用再占一次推送。
        """
        tmp, root = _root(running="2.0.1")
        self.addCleanup(tmp.cleanup)
        fake = {"2.0.0": whatsnew.NOTES["2.0.0"],
                "2.0.1": {"title": "小修", "highlights": ["x"],
                          "todo": [{"text": "**去做一件新的事**，做完就好了。",
                                    "go": "settings"}]}}
        with mock.patch.object(whatsnew, "NOTES", fake):
            why = upgrade.should_push(
                root, "2.0.1", {"from": "2.0.0", "to": "2.0.1", "major": False})
        self.assertIsNone(why, "小版本不该推，哪怕它带了待办")

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

    def setUp(self):
        # 这些用例统一走 1.6.1 → 2.0.0，且都要走到"该推送"那一步才测得到东西 ——
        # 把边界声明到这条线上（生产清单首条是 2.1.1，1.6.1→2.0.0 在它之下不算大版本）
        p = mock.patch.object(upgrade, "MAJOR_ABOVE", [("1.9.9", "2.0.0")])
        p.start()
        self.addCleanup(p.stop)

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
