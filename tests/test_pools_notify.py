"""推送记忆（`src/pools_notify.py`）的回归。

盯的是：**同一个串号连着几天推，第二天起必须弱化** ——
批发单云商先报、玲珑过后才报，会连着推好几天。
弄错的方向有两种，都很难看：
  * 全都当"新的" → 门店第二天就麻木，真新出现的被淹掉
  * 全都当"推过了" → 强调永远拿不到
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import pools_notify as N                          # noqa: E402


class TestAnnotate(unittest.TestCase):
    def test_first_time_is_new(self):
        m = N.annotate(["A"], {})
        self.assertTrue(m["A"]["new"])
        self.assertEqual(m["A"]["count"], 0)

    def test_second_time_is_not_new(self):
        st = N.remember({}, ["A"], "2026-09-14")
        m = N.annotate(["A"], st)
        self.assertFalse(m["A"]["new"])
        self.assertEqual(m["A"]["count"], 1)
        # ⚠ 显示用 first：门店要知道"从哪天开始催的"，不是"上次哪天推的"
        self.assertEqual(m["A"]["first"], "2026-09-14")

    def test_count_grows_and_first_sticks(self):
        st = N.remember({}, ["A"], "2026-09-14")
        st = N.remember(st, ["A"], "2026-09-15")
        st = N.remember(st, ["A"], "2026-09-16")
        self.assertEqual(st["A"]["count"], 3)
        self.assertEqual(st["A"]["first"], "2026-09-14")
        self.assertEqual(st["A"]["last"], "2026-09-16")

    def test_annotate_does_not_mutate(self):
        """`annotate` 只读 —— 推送失败就不该记（记了门店永远拿不到那条强调）。"""
        st = {}
        N.annotate(["A"], st)
        self.assertEqual(st, {})

    def test_disappeared_sn_forgotten(self):
        """从清单消失（报量了/出库了）= 解决了，记忆里删掉。"""
        st = N.remember({}, ["A", "B"], "2026-09-14")
        st = N.remember(st, ["A"], "2026-09-15")
        self.assertEqual(set(st), {"A"})
        # 又冒出来就重新当"新的" —— 它确实解决过，那是对的
        self.assertTrue(N.annotate(["B"], st)["B"]["new"])

    def test_empty_push_clears_everything(self):
        st = N.remember({}, ["A"], "2026-09-14")
        self.assertEqual(N.remember(st, [], "2026-09-15"), {})


class TestState(unittest.TestCase):
    def test_broken_file_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = N.state_path(d)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{ 这不是 json", encoding="utf-8")
            self.assertEqual(N.load(d), {}, "记忆坏了当没有，别把流程弄挂")

    def test_clear_reports_whether_there_was(self):
        """如实回报 —— 永远说"已清除"的话，用户分不清清成功了还是按钮没生效。"""
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(N.clear(d))
            N.save(d, {"A": {"count": 1}})
            self.assertTrue(N.clear(d))
            self.assertEqual(N.load(d), {})

    def test_清完之后每一条都重新变新(self):
        """⚠⚠ **这条钉的是"清除推送记忆"的语义方向** —— 用户 2026-09-18 指出
        `whatsnew` 里那句话的前半截写反了：

            原话：「推送里某条如果不用再提醒了，可以清掉推送记忆」

        **它是反的。** 这个动作**不会让任何一条少被提醒** ——
        恰恰相反：清完之后记忆是空的，`annotate` 对**清单里每一条**
        都返回 `new=True`，下次推送**全部**重新戴上 `★`。

        真正"不用再提醒了"是**自动**发生的：报量了 / 出库了，
        串号从清单里消失，`remember` 顺手就把它忘掉（见
        `test_disappeared_sn_forgotten`）—— 用不着手动清。

        ⚠ 所以这个按钮是**反过来**用的：想让清单里每一条都重新醒目一次时才点。
        文案改错了不会报错，只会让门店以为"点一下就能让它别再烦我"，
        结果下次推送**满屏 ★**。
        """
        with tempfile.TemporaryDirectory() as d:
            N.save(d, {"A": {"first": "2026-09-14", "last": "2026-09-16", "count": 3}})
            self.assertFalse(N.annotate(["A"], N.load(d))["A"]["new"], "前提：本来是老的")
            N.clear(d)
            got = N.annotate(["A", "B"], N.load(d))
            self.assertTrue(got["A"]["new"], "清完之后老的也该重新算新的")
            self.assertTrue(got["B"]["new"])
            self.assertEqual(got["A"]["count"], 0, "次数也归零了")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            N.save(d, {"A": {"first": "2026-09-14", "last": "2026-09-16", "count": 3}})
            self.assertEqual(N.load(d)["A"]["count"], 3)


if __name__ == "__main__":
    unittest.main()
