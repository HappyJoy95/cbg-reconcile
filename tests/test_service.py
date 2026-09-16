"""后台服务 + 开机自启的测试。

**不真去改注册表 / LaunchAgents / autostart** —— 那会在你本机上留下开机自启项。
把系统调用换掉，只验证：状态怎么读、命令怎么拼、失败怎么报、
以及几个真踩过的坑（Windows 上 os.kill(pid,0) 会杀人、Run 键不设工作目录）。
"""

import json
import os
import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import autostart, service


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_write_read_clear(self):
        self.assertIsNone(service.read_state(self.root))
        service.write_state(self.root, pid=1234, host="127.0.0.1", port=8787)
        st = service.read_state(self.root)
        self.assertEqual(st["pid"], 1234)
        self.assertEqual(st["port"], 8787)
        self.assertEqual(st["app"], "cbg-reconcile")
        service.clear_state(self.root)
        self.assertIsNone(service.read_state(self.root))

    def test_clear_is_idempotent(self):
        service.clear_state(self.root)          # 文件不存在也不能炸
        service.clear_state(self.root)

    def test_corrupt_state_file_is_ignored(self):
        service.state_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        service.state_path(self.root).write_text("{坏掉的 json", encoding="utf-8")
        self.assertIsNone(service.read_state(self.root))


class TestPidAlive(unittest.TestCase):
    def test_zero_pid_is_dead(self):
        self.assertFalse(service.pid_alive(0))
        self.assertFalse(service.pid_alive(None))

    def test_own_pid_is_alive(self):
        self.assertTrue(service.pid_alive(os.getpid()))

    def test_bogus_pid_is_dead(self):
        self.assertFalse(service.pid_alive(999_999))

    def test_windows_does_not_use_os_kill_zero(self):
        """⚠ Windows 上 os.kill(pid, 0) 会被当成 TerminateProcess(pid, 0) —— **会杀掉进程**。

        所以 Windows 分支必须走 tasklist，绝不能碰 os.kill。
        """
        called = {"kill": False, "tasklist": False}

        def fake_kill(*a, **k):
            called["kill"] = True

        def fake_run(args, **kw):
            called["tasklist"] = True
            return subprocess.CompletedProcess(args, 0, stdout=f"{4242}  python.exe", stderr="")

        with mock.patch.object(service.platform, "system", lambda: "Windows"), \
                mock.patch.object(service.os, "kill", fake_kill), \
                mock.patch.object(service.subprocess, "run", fake_run):
            self.assertTrue(service.pid_alive(4242))
        self.assertTrue(called["tasklist"], "Windows 上必须用 tasklist 探活")
        self.assertFalse(called["kill"], "Windows 上绝不能 os.kill —— 那会杀掉进程")

    def test_windows_reports_dead_when_tasklist_says_nothing(self):
        def fake_run(args, **kw):
            return subprocess.CompletedProcess(args, 0, stdout="信息: 没有运行的任务匹配指定标准。", stderr="")

        with mock.patch.object(service.platform, "system", lambda: "Windows"), \
                mock.patch.object(service.subprocess, "run", fake_run):
            self.assertFalse(service.pid_alive(4242))


class TestFindRunning(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_prefers_state_file(self):
        service.write_state(self.root, pid=1, host="127.0.0.1", port=9999)
        with mock.patch.object(service, "health",
                               lambda h, p, timeout=2.0: {"app": "cbg-reconcile"} if p == 9999 else None):
            got = service.find_running(self.root)
        self.assertEqual(got["port"], 9999)

    def test_falls_back_to_default_port(self):
        with mock.patch.object(service, "health",
                               lambda h, p, timeout=2.0:
                               {"app": "cbg-reconcile", "pid": 7} if p == service.DEFAULT_PORT else None):
            got = service.find_running(self.root)
        self.assertEqual(got["port"], service.DEFAULT_PORT)

    def test_nothing_running(self):
        with mock.patch.object(service, "health", lambda *a, **k: None):
            self.assertIsNone(service.find_running(self.root))

    def test_wait_ready_times_out(self):
        with mock.patch.object(service, "health", lambda *a, **k: None):
            self.assertIsNone(service.wait_ready(self.root, timeout=0.6, interval=0.2))

    def test_wait_ready_returns_when_up(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            return {"app": "cbg-reconcile", "pid": 9} if calls["n"] >= 2 else None

        with mock.patch.object(service, "health", flaky):
            got = service.wait_ready(self.root, timeout=5, interval=0.05)
        self.assertIsNotNone(got)


class TestStop(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_stop_when_not_running(self):
        with mock.patch.object(service, "find_running", lambda *a, **k: None):
            ok, msg = service.stop(self.root)
        self.assertFalse(ok)
        self.assertIn("没有在运行", msg)

    def test_graceful_shutdown(self):
        """先好好说：POST /api/shutdown，服务自己退。"""
        service.write_state(self.root, pid=4242, host="127.0.0.1", port=8787)
        state = {"alive": True}

        def fake_urlopen(req, timeout=None):
            state["alive"] = False               # 收到 shutdown 就"退"了
            return mock.MagicMock(read=lambda: b"{}")

        with mock.patch.object(service, "find_running",
                               lambda *a, **k: {"pid": 4242, "host": "127.0.0.1", "port": 8787}), \
                mock.patch.object(service.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(service, "health", lambda *a, **k: None if not state["alive"] else {}), \
                mock.patch.object(service, "pid_alive", lambda p: state["alive"]):
            ok, msg = service.stop(self.root, timeout=2)
        self.assertTrue(ok)
        self.assertIn("已停止", msg)
        self.assertIsNone(service.read_state(self.root), "停完要清状态文件")

    def test_falls_back_to_kill(self):
        """说不通就动手。"""
        service.write_state(self.root, pid=4242, host="127.0.0.1", port=8787)
        killed = {"pid": None}

        def fake_kill(pid, sig):
            killed["pid"] = pid

        with mock.patch.object(service, "find_running",
                               lambda *a, **k: {"pid": 4242, "host": "127.0.0.1", "port": 8787}), \
                mock.patch.object(service.urllib.request, "urlopen",
                                  side_effect=OSError("refused")), \
                mock.patch.object(service, "health", lambda *a, **k: {"app": "cbg-reconcile"}), \
                mock.patch.object(service, "pid_alive", lambda p: False), \
                mock.patch.object(service.os, "kill", fake_kill), \
                mock.patch.object(service.time, "sleep", lambda s: None):
            ok, msg = service.stop(self.root, timeout=0.5)
        self.assertTrue(ok)


def _fake_winreg(**over):
    """`winreg` 是 Windows 专有模块，macOS 上 import 不到 —— 塞一个假的进 sys.modules。

    `_win_install` 里是函数内 `import winreg`，所以 patch sys.modules 就能生效。
    """
    import sys
    import types
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = "HKCU"
    mod.KEY_SET_VALUE = 2
    mod.KEY_READ = 1
    mod.REG_SZ = 1

    def _not_installed(*a, **k):
        raise FileNotFoundError("没有这个值")

    mod.OpenKey = _not_installed          # 默认"没注册"
    for k, v in over.items():
        setattr(mod, k, v)
    return mod


class TestAutostart(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "boot.py").write_text("# launcher\n", encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def test_command_points_at_boot_py_with_absolute_paths(self):
        cmd = autostart.command(self.root)
        self.assertIn(str(self.root / "boot.py"), cmd)
        self.assertTrue(cmd.startswith('"'), "路径要带引号，否则带空格的目录会断")

    def test_windows_uses_pythonw_when_present(self):
        """pythonw.exe 不带控制台窗口 —— 开机时不会闪黑框。"""
        fake_py = Path("/fake/python.exe")
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.object(autostart.runtime, "current", lambda: str(fake_py)), \
                mock.patch.object(autostart.runtime, "pythonw_for",
                                  lambda e: str(Path(e).with_name("pythonw.exe"))):
            self.assertTrue(autostart._python_exe().endswith("pythonw.exe"))

    def test_windows_falls_back_when_no_pythonw(self):
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.object(autostart.runtime, "current", lambda: "/fake/python.exe"), \
                mock.patch.object(autostart.runtime, "pythonw_for", lambda e: e):
            self.assertEqual(autostart._python_exe(), "/fake/python.exe")

    def test_autostart_prefers_the_recorded_interpreter(self):
        """开机自启必须拉起**安装时记下的那个** Python。

        一台电脑上两个 Python 时，用错的那个表现是"每天开机都静静地起不来"——
        界面上什么都看不到，只有 out/autostart.log 里一行 ImportError。
        """
        with mock.patch.object(autostart.platform, "system", lambda: "Linux"), \
                mock.patch.object(autostart.runtime, "current",
                                  lambda: "/recorded/python3"):
            self.assertEqual(autostart._python_exe(), "/recorded/python3")

    def test_install_refuses_without_boot_script(self):
        empty = Path(self.dir.name) / "empty"
        empty.mkdir()
        res = autostart.install(empty)
        self.assertFalse(res["ok"])
        self.assertIn("boot.py", res["message"])

    def test_windows_install_writes_run_key(self):
        import sys
        written = {}

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_set(key, name, reserved, typ, value):
            written["name"], written["value"], written["type"] = name, value, typ

        fake = _fake_winreg(CreateKeyEx=lambda *a, **k: FakeKey(), SetValueEx=fake_set)
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.dict(sys.modules, {"winreg": fake}), \
                mock.patch.object(autostart, "status", lambda root: {"installed": True}):
            res = autostart.install(self.root)
        self.assertTrue(res["ok"])
        self.assertEqual(written["name"], autostart.APP_NAME)
        self.assertEqual(written["type"], fake.REG_SZ)
        self.assertIn("boot.py", written["value"])

    def test_windows_install_reports_registry_failure(self):
        import sys

        def boom(*a, **k):
            raise OSError("拒绝访问")

        fake = _fake_winreg(CreateKeyEx=boom)
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.dict(sys.modules, {"winreg": fake}):
            res = autostart.install(self.root)
        self.assertFalse(res["ok"])
        self.assertIn("拒绝访问", res["message"])

    def test_mac_plist_shape(self):
        xml = autostart._mac_plist(self.root)
        self.assertIn("<key>RunAtLoad</key><true/>", xml)
        self.assertIn(str(self.root / "boot.py"), xml)
        self.assertIn(str(self.root), xml, "要带 WorkingDirectory，日志路径才稳")
        import plistlib
        plistlib.loads(xml.encode("utf-8"))          # 必须是合法 plist

    def test_linux_desktop_shape(self):
        with mock.patch.object(autostart, "LINUX_DESKTOP",
                               Path(self.dir.name) / "auto.desktop"):
            res = autostart._linux_install(self.root)
            self.assertTrue(res["ok"])
            text = (Path(self.dir.name) / "auto.desktop").read_text(encoding="utf-8")
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("boot.py", text)
        self.assertIn("X-GNOME-Autostart-enabled=true", text)

    def test_status_reports_stale_registration(self):
        """项目挪过位置 → 注册的还是老路径，必须提示。"""
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.object(autostart, "_win_status",
                                  lambda: {"installed": True, "registered": '"C:\\old\\boot.py"'}):
            st = autostart.status(self.root)
        self.assertTrue(st["installed"])
        self.assertTrue(st.get("stale"))
        self.assertNotEqual(st["registered"], st["command"])

    def test_status_not_stale_when_same(self):
        with mock.patch.object(autostart.platform, "system", lambda: "Windows"), \
                mock.patch.object(autostart, "_win_status",
                                  lambda: {"installed": True,
                                           "registered": autostart.command(self.root)}):
            st = autostart.status(self.root)
        self.assertFalse(st.get("stale"))


if __name__ == "__main__":
    unittest.main()
