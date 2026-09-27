"""**定时器的"单次注册"**（`modules/timer/once.py`）—— 用户 2026-09-21 定的：

> 「把 timer 的注册加上这种**单次注册**机制吧，**记录日志**，**执行完删除注册**」

它跟 `whens`（那张静态时刻表：每天 21:15 / 每周一 9:00）是**两回事**：
静态那张是"功能模块声明 + 这台机器改的时刻"，**一直在**；
这个是**运行期**产生的一次性任务 ——「从现在起 N 秒后跑一遍，跑完把登记删掉」。

这一份盯五件事：

1. **落盘** ⇒ 服务重启不丢（用户第一句就是"关浏览器不影响吧"，注册得比进程活得久）；
2. **同一个 key 再登记 = 改时间**（不是排两条）—— "连点保存只发一封"靠它；
3. **到点被 `tick()` 派发**，走的是 `daily --steps …` 那条**同一条执行路径**；
4. **执行完删除注册**（`take_due()` 取走即删）；
5. **三笔日志**：登记 / 到点 / 过期作废（`runlog` kind = `wake-once`）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.modules import timer                                          # noqa: E402
from src.modules.timer import once                                     # noqa: E402
from src.storage import runlog                                         # noqa: E402

#: 用一个**不撞静态时刻表**的"现在"（那些默认是 21:00 / 21:15 / 21:30）
NOW = datetime.datetime(2026, 9, 21, 10, 0, 0)


def _root():
    """临时安装目录 + 一个最小库（`runlog` 没库是**静默 no-op**）。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
    runlog.ensure(conn)
    conn.commit()
    conn.close()
    return tmp, root


class Test登记(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = _root()
        self.addCleanup(self.tmp.cleanup)

    def test_登记落盘_重启也还在(self):
        r = timer.register_once(self.root, "report", after=300, note="保存人员设置")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["in"], 300)
        # 落盘 = 换个"进程"（重读文件）照样看得见
        raw = json.loads((self.root / timer.ONCE_REL).read_text(encoding="utf-8"))
        self.assertEqual(len(raw["once"]), 1)
        self.assertEqual(raw["once"][0]["cmd"], "report")
        self.assertEqual(len(timer.once_list(self.root)), 1)

    def test_同一个key再登记就是改时间(self):
        """⚠ "连点几次保存合并成一封"——靠的是 key，不是撤计时器。"""
        timer.register_once(self.root, "report", at=NOW + datetime.timedelta(minutes=5))
        first = timer.once_list(self.root)[0]["at"]
        timer.register_once(self.root, "report", at=NOW + datetime.timedelta(minutes=9))
        rows = timer.once_list(self.root)
        self.assertEqual(len(rows), 1, rows)
        self.assertNotEqual(rows[0]["at"], first)
        self.assertEqual(rows[0]["at"], (NOW + datetime.timedelta(minutes=9)).strftime(
            "%Y-%m-%d %H:%M:%S"))

    def test_不同的key互不影响(self):
        timer.register_once(self.root, "report", after=60, key="甲")
        timer.register_once(self.root, "report", after=60, key="乙")
        self.assertEqual(len(timer.once_list(self.root)), 2)

    def test_还差几秒(self):
        timer.register_once(self.root, "report", after=300)
        left = timer.once_seconds_left(self.root, "report")
        self.assertLessEqual(left, 300)
        self.assertGreater(left, 290)
        self.assertEqual(timer.once_seconds_left(self.root, "没这个 key"), 0)

    def test_撤掉(self):
        timer.register_once(self.root, "report", after=300)
        self.assertTrue(timer.cancel_once(self.root, "report"))
        self.assertEqual(timer.once_list(self.root), [])
        self.assertFalse(timer.cancel_once(self.root, "report"), "撤过还说撤掉了")

    def test_登记要记日志(self):
        timer.register_once(self.root, "report", after=300, note="保存人员设置", who="张三")
        rows = runlog.recent(self.root, kind=timer.ONCE_KIND, limit=3)
        self.assertTrue(rows, "登记没留痕")
        self.assertIn("登记", rows[0].get("note") or "")
        self.assertIn("保存人员设置", rows[0].get("note") or "")

    def test_参数不全会说清_而且不抛(self):
        r = timer.register_once(self.root, "report")
        self.assertFalse(r["ok"])
        self.assertIn("after", r["why"])
        r2 = timer.register_once(self.root, "")
        self.assertFalse(r2["ok"])


class Test到点(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = _root()
        self.addCleanup(self.tmp.cleanup)

    def test_没到点不取(self):
        timer.register_once(self.root, "report", at=NOW + datetime.timedelta(minutes=1))
        self.assertEqual(once.take_due(self.root, now=NOW), [])
        self.assertEqual(len(timer.once_list(self.root)), 1, "没到点却被删了")

    def test_到点_取走即删_而且记日志(self):
        """⭐ 用户那句「**执行完删除注册**」。"""
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=5),
                            note="保存人员设置")
        got = once.take_due(self.root, now=NOW)
        self.assertEqual([x["cmd"] for x in got], ["report"])
        self.assertEqual(got[0]["slot_text"], "once:2026-09-21 09:59:55")
        self.assertEqual(got[0]["late"], 5)
        self.assertEqual(timer.once_list(self.root), [], "执行完没删注册")
        notes = [r.get("note") or "" for r in runlog.recent(self.root, kind=timer.ONCE_KIND,
                                                           limit=5)]
        self.assertTrue(any("到点" in n for n in notes), notes)

    def test_过期作废_不跑_也删掉(self):
        """⚠ 机器关着好几天再开机，别把一堆陈旧任务全跑一遍。"""
        timer.register_once(self.root, "report",
                            at=NOW - datetime.timedelta(hours=timer.ONCE_GRACE_HOURS + 1))
        self.assertEqual(once.take_due(self.root, now=NOW), [])
        self.assertEqual(timer.once_list(self.root), [], "作废的没删掉")
        rows = runlog.recent(self.root, kind=timer.ONCE_KIND, limit=3)
        self.assertTrue(any("作废" in (r.get("note") or "") for r in rows), rows)
        self.assertFalse(rows[0]["ok"])

    def test_时间读不出来的也清掉(self):
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        (self.root / timer.ONCE_REL).write_text(
            json.dumps({"once": [{"key": "x", "cmd": "report", "at": "不是时间"}]},
                       ensure_ascii=False), encoding="utf-8")
        self.assertEqual(once.take_due(self.root, now=NOW), [])
        self.assertEqual(timer.once_list(self.root), [])


class Testtick派发(unittest.TestCase):
    """到点了，**由心跳那一跳派发出去** —— 跟静态那几路走同一条命令。"""

    def setUp(self):
        self.tmp, self.root = _root()
        self.addCleanup(self.tmp.cleanup)
        self.argv = []

    def _spawn(self, argv):
        self.argv.append(list(argv))

    def test_到点派发的是daily那一步(self):
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=1),
                            note="保存人员设置")
        res = timer.tick(self.root, spawn=self._spawn, config="config/store-X.yaml", now=NOW)
        self.assertEqual(res["ran"], ["report"], res)
        self.assertTrue(self.argv, "没派发")
        argv = self.argv[0]
        self.assertIn("daily", argv)
        self.assertEqual(argv[argv.index("--steps") + 1], "report")
        self.assertTrue(argv[argv.index("--wake-slot") + 1].startswith("once:"),
                        argv)
        self.assertEqual(timer.once_list(self.root), [], "派发完没删注册")

    def test_不同时刻一起逾期_下一组留到下一跳(self):
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=2),
                            key="先到")
        timer.register_once(self.root, "attain", at=NOW - datetime.timedelta(seconds=1),
                            key="后到")

        first = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW)
        self.assertEqual(first["ran"], ["report"])
        self.assertEqual([x["key"] for x in timer.once_list(self.root)], ["后到"])

        second = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW,
                            busy=lambda: False)
        self.assertEqual(second["ran"], ["attain"])
        self.assertEqual(timer.once_list(self.root), [])
        self.assertEqual([a[a.index("--steps") + 1] for a in self.argv],
                         ["report", "attain"])

    def test_周期任务先派发时_一次性登记仍在(self):
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=1))
        periodic = {"cmd": "dump", "label": "抓数", "order": 1,
                    "slot": NOW - datetime.timedelta(seconds=3),
                    "slot_text": "2026-09-21 09:59", "when_text": "每天 09:59"}
        with mock.patch.object(timer, "due", return_value=[periodic]):
            first = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW)
        self.assertEqual(first["ran"], ["dump"])
        self.assertEqual(len(timer.once_list(self.root)), 1)
        second = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW,
                            busy=lambda: False)
        self.assertEqual(second["ran"], ["report"])

    def test_正在跑就不取_登记还在(self):
        """⚠ `tick()` 先看"有没有在跑"再取 —— 所以撞上别的活时**登记不会被吃掉**。"""
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=1))
        res = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW,
                         busy=lambda: True)
        self.assertEqual(res["ran"], [])
        self.assertFalse(self.argv)
        self.assertEqual(len(timer.once_list(self.root)), 1, "撞上别的活就把登记吃了")

    def test_派发失败_登记不复活但日志里看得见(self):
        timer.register_once(self.root, "report", at=NOW - datetime.timedelta(seconds=1))
        with mock.patch.object(timer, "_mark_done", lambda *a, **k: None), \
                mock.patch.object(timer, "_mark_current", lambda *a, **k: None), \
                mock.patch.object(timer, "_clear_current", lambda *a, **k: None):
            res = timer.tick(self.root, spawn=lambda argv: (_ for _ in ()).throw(
                OSError("起不来")), config="c.yaml", now=NOW)
        self.assertEqual(res["ran"], [])
        self.assertIn("没派出去", res["why"])
        self.assertEqual(timer.once_list(self.root), [], "登记不复活（重试会比没跑更糟）")
        rows = runlog.recent(self.root, kind=timer.ONCE_KIND, limit=3)
        self.assertTrue(any(not r["ok"] and "没派出去" in (r.get("why") or "") for r in rows),
                        rows)

    def test_没登记的步骤也能跑(self):
        """⚠ `cmd` 是自由的（不必是注册表里的步骤）—— 认不出来就当标签是它自己。"""
        timer.register_once(self.root, "不存在的步骤", at=NOW - datetime.timedelta(seconds=1))
        res = timer.tick(self.root, spawn=self._spawn, config="c.yaml", now=NOW)
        self.assertEqual(res["ran"], ["不存在的步骤"])


class Test日志看得见(unittest.TestCase):
    """⚠ 用户要的「记录日志」得**看得见**才算数 —— 只写进库、界面上永远看不到，
    就跟没记一样。定时器页那张"执行日志"（`timer.wakes()`）现在把它们也列出来。"""

    def setUp(self):
        self.tmp, self.root = _root()
        self.addCleanup(self.tmp.cleanup)

    def test_登记_到点_作废_三笔都在执行日志里(self):
        timer.register_once(self.root, "report", after=300, note="保存人员设置")
        timer.register_once(self.root, "report",
                            at=NOW - datetime.timedelta(seconds=1), note="保存人员设置")
        due = once.take_due(self.root, now=NOW)
        self.assertEqual(len(due), 1)
        timer.register_once(self.root, "report",
                            at=NOW - datetime.timedelta(hours=timer.ONCE_GRACE_HOURS + 2))
        once.take_due(self.root, now=NOW)
        labels = [w["label"] for w in timer.wakes(self.root, limit=20)]
        self.assertTrue(any(x.startswith("登记") for x in labels), labels)
        self.assertTrue(any(x.startswith("到点") for x in labels), labels)
        self.assertTrue(any(x.startswith("作废") for x in labels), labels)
        # 三条都要有"时间点"和"唤醒了什么"（界面上那两列）
        for w in timer.wakes(self.root, limit=20):
            self.assertTrue(w["slot"], w)
            self.assertIn("上报数据", w["label"])

    def test_不混进静态那张表(self):
        timer.register_once(self.root, "report", after=300)
        rows = timer.tasks(self.root)
        self.assertFalse([t for t in rows if t.get("cmd") == staff_report_key()], rows)


def staff_report_key():
    """`staff.REPORT_ONCE_KEY` —— 一次性登记的 key **不该**出现在"每天要跑"那张表里。"""
    from src.features.store import staff
    return staff.REPORT_ONCE_KEY


class Test对外(unittest.TestCase):
    def test_出口都在timer上(self):
        """调用方写 `timer.register_once(...)` —— 别去 import 子模块。"""
        for name in ("register_once", "once_tasks", "once_list", "cancel_once",
                     "once_seconds_left", "ONCE_KIND", "ONCE_REL", "ONCE_GRACE_HOURS"):
            self.assertTrue(hasattr(timer, name), name)

    def test_一次性的不混进静态那张表(self):
        """⚠ 「定时器设置」那张表列的是"每天都跑"的任务 —— 一次性混进去
        会让人以为要天天跑（`once.py` 模块头写了这条）。"""
        tmp, root = _root()
        self.addCleanup(tmp.cleanup)
        timer.register_once(root, "report", after=300)
        self.assertTrue(timer.once_list(root))
        self.assertNotIn("staff-report", json.dumps(timer.tasks(root), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
