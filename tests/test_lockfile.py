"""跨进程锁。

**为什么必须存在**：云商不能并行登录 —— 两个进程同时登，四个域一起报「登录超时」，
换 token 也没用，要等几分钟。计划任务 09:00 跑的同时有人点「立即运行」就会撞上。
Web 界面里那把锁只管得住界面自己。
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from src.lockfile import Lock

SRC_CLI = (Path(__file__).resolve().parent.parent / "src" / "cli.py")


class TestLock(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "check.lock"

    def tearDown(self):
        self.dir.cleanup()

    def test_second_holder_is_blocked(self):
        a, b = Lock(self.path), Lock(self.path)
        self.assertTrue(a.acquire()[0])
        ok, why = b.acquire()
        self.assertFalse(ok, "第二个必须被挡住")
        self.assertIn("已经有一个对账在跑", why)
        self.assertIn(str(os.getpid()), why, "要说清是谁占着")

    def test_release_lets_the_next_one_in(self):
        a, b = Lock(self.path), Lock(self.path)
        a.acquire()
        a.release()
        self.assertTrue(b.acquire()[0])

    def test_release_removes_the_file(self):
        a = Lock(self.path)
        a.acquire()
        a.release()
        self.assertFalse(self.path.exists())

    def test_dead_holder_is_stolen(self):
        """程序崩了/被 kill 不会走 finally —— 死锁必须能自动解开。"""
        self.path.write_text(json.dumps({"pid": 999_999, "started_at": time.time()}),
                             encoding="utf-8")
        ok, why = Lock(self.path).acquire()
        self.assertTrue(ok, f"持有者已死就该抢过来：{why}")

    def test_too_old_lock_is_stolen(self):
        """PID 被系统复用了也别永久锁死 —— 超时也算失效。"""
        self.path.write_text(
            json.dumps({"pid": os.getpid(), "started_at": time.time() - 99999}),
            encoding="utf-8")
        self.assertTrue(Lock(self.path, stale_after=60).acquire()[0])

    def test_live_holder_within_stale_is_respected(self):
        self.path.write_text(json.dumps({"pid": os.getpid(), "started_at": time.time()}),
                             encoding="utf-8")
        ok, _ = Lock(self.path, stale_after=3600).acquire()
        self.assertFalse(ok, "人还活着、也没超时，就不该抢")

    def test_corrupt_lock_file_is_stolen(self):
        self.path.write_text("{坏掉的 json", encoding="utf-8")
        self.assertTrue(Lock(self.path).acquire()[0])

    def test_context_manager_actually_acquires(self):
        """`with Lock(p):` 必须真加锁 —— 只 return self 是个静默空操作。"""
        with Lock(self.path) as lk:
            self.assertTrue(lk._held, "进了 with 就该持锁")
            self.assertTrue(self.path.exists())
        self.assertFalse(self.path.exists(), "出了 with 就该放锁")

    def test_context_manager_raises_when_busy(self):
        holder = Lock(self.path)
        holder.acquire()
        with self.assertRaises(RuntimeError):
            with Lock(self.path):
                pass

    def test_release_only_removes_own_lock(self):
        """万一被判定过期抢走过，别把新持有者的锁删了。"""
        a = Lock(self.path)
        a.acquire()
        self.path.write_text(json.dumps({"pid": 999_999, "started_at": time.time()}),
                             encoding="utf-8")          # 模拟已被别人接管
        a.release()
        self.assertTrue(self.path.exists(), "不该删别人的锁")

    def test_lock_file_is_0600(self):
        import stat
        a = Lock(self.path)
        a.acquire()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_acquire_is_atomic_under_race(self):
        """同时抢只能有一个成功 —— O_CREAT|O_EXCL 保证。"""
        locks = [Lock(self.path) for _ in range(8)]
        wins = [lk.acquire()[0] for lk in locks]
        self.assertEqual(sum(wins), 1, f"应当恰好一个成功，实际 {sum(wins)}")


class TestCheckUsesTheLock(unittest.TestCase):
    def test_cmd_check_acquires_before_running(self):
        src = SRC_CLI.read_text(encoding="utf-8")
        self.assertIn("lockfile.Lock", src, "check 必须抢锁")
        self.assertIn("lock.release()", src, "而且必须释放（finally）")

    def test_message_points_at_the_lock_file(self):
        src = SRC_CLI.read_text(encoding="utf-8")
        self.assertIn("删掉", src, "被挡住时要告诉人怎么解")

    def test_nothing_else_takes_the_lock(self):
        """只有 check 需要这把锁 —— 别顺手给别的命令也加上，会互相挡。"""
        src = SRC_CLI.read_text(encoding="utf-8")
        self.assertEqual(src.count("lockfile.Lock("), 1)


if __name__ == "__main__":
    unittest.main()
