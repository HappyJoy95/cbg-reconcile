"""开机自启的测试。

Windows 那条路**本机跑不了**（macOS 上没有 schtasks / winreg），所以这里全部靠
假 `schtasks` + 假 winreg 验证**我们拼出来的东西对不对**：

* 任务 XML 是不是合法的、`RunLevel` 是不是 `HighestAvailable`（提权就靠这一句）
* `ExecutionTimeLimit` 有没有写 `PT0S` —— 漏了的话服务跑满 72 小时会被系统**静默杀掉**
* 路径带空格时 `Command` / `Arguments` 有没有分开（这是当初弃用 `/tr` 的原因）
* 拿不到管理员权限时有没有**老实退回注册表并说清楚**，而不是假装成功
"""

import sys
import tempfile
import types
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from src import autostart

NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


class _FakeWinreg:
    """够用的 winreg 替身 —— 只需要 增/查/删 一个字符串值。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    REG_SZ = 1

    def __init__(self, initial=None):
        self.store = dict(initial or {})

    class _Key:
        def __init__(self, reg):
            self.reg = reg

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved, access):
        return self._Key(self)

    def CreateKeyEx(self, root, path, reserved, access):
        return self._Key(self)

    def QueryValueEx(self, key, name):
        if name not in key.reg.store:
            raise FileNotFoundError(name)
        return key.reg.store[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, typ, value):
        key.reg.store[name] = value

    def DeleteValue(self, key, name):
        if name not in key.reg.store:
            raise FileNotFoundError(name)
        del key.reg.store[name]


class _WinCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / "boot.py").write_text("# stub\n", encoding="utf-8")
        self.reg = _FakeWinreg()
        p = mock.patch.dict(sys.modules, {"winreg": self.reg})
        p.start()
        self.addCleanup(p.stop)
        for name, val in (("kind", lambda: "windows"),
                          ("platform", mock.Mock(system=lambda: "Windows")),
                          ("is_elevated", lambda: True),
                          ("_pythonw", lambda: r"C:\Program Files\Python39\pythonw.exe")):
            q = mock.patch.object(autostart, name, val)
            q.start()
            self.addCleanup(q.stop)

    def _schtasks(self, results):
        """results: 按调用顺序给的返回值列表（callable 或 SimpleNamespace）。"""
        calls = []

        def fake(args, **kw):
            calls.append(args)
            r = results[min(len(calls) - 1, len(results) - 1)]
            return r(args) if callable(r) else r

        p = mock.patch.object(autostart, "schtasks", fake)
        p.start()
        self.addCleanup(p.stop)
        return calls


def _ok(stdout=""):
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _fail(stderr=""):
    return types.SimpleNamespace(returncode=1, stdout="", stderr=stderr)


class TestTaskXml(unittest.TestCase):
    """XML 是本机唯一能真验证的部分 —— 而且它恰好是最容易写错的地方。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp)
        (self.root / "boot.py").write_text("# stub\n", encoding="utf-8")

    def test_xml_is_wellformed(self):
        root = ET.fromstring(autostart.task_xml(self.root))
        self.assertTrue(root.tag.endswith("Task"), root.tag)

    def test_runs_elevated_without_uac(self):
        """提权就靠 `RunLevel=HighestAvailable` 这一句 —— 少了它服务就是普通权限。"""
        root = ET.fromstring(autostart.task_xml(self.root))
        self.assertEqual(root.find(f".//{NS}RunLevel").text, "HighestAvailable")
        # InteractiveToken：只在用户登录后跑（要弹浏览器 / 显示界面，不能用 S4U）
        self.assertEqual(root.find(f".//{NS}LogonType").text, "InteractiveToken")
        self.assertIsNotNone(root.find(f".//{NS}LogonTrigger"), "要是登录触发，不是定时触发")

    def test_no_execution_time_limit(self):
        """⚠ 不写 PT0S 的话默认 PT72H —— 服务跑满 3 天会被任务计划程序**静默杀掉**。"""
        root = ET.fromstring(autostart.task_xml(self.root))
        self.assertEqual(root.find(f".//{NS}ExecutionTimeLimit").text, "PT0S")

    def test_command_is_never_quoted_but_arguments_always_are(self):
        """⚠ 这两个元素的引号规则**正好相反**，搞错哪个都是静默失败：

        * `<Command>` 是单个文件路径 —— 加了引号 schtasks 会找不到文件；
        * `<Arguments>` 是命令行参数 —— 路径带空格时**必须**加引号，
          否则 pythonw 会把 `D:\\门店 测试\\boot.py` 拆成两个参数。
        """
        with mock.patch.object(autostart, "_pythonw",
                               lambda: r"C:\Program Files\Python 3.9\pythonw.exe"):
            root = ET.fromstring(autostart.task_xml(self.root))
        cmd = root.find(f".//{NS}Command").text
        arg = root.find(f".//{NS}Arguments").text
        self.assertEqual(cmd, r"C:\Program Files\Python 3.9\pythonw.exe")
        self.assertNotIn('"', cmd, "Command 加了引号 → schtasks 找不到 pythonw.exe")
        self.assertTrue(arg.startswith('"') and arg.endswith('"'),
                        f"Arguments 必须整体加引号，否则带空格的路径会被拆开：{arg!r}")
        self.assertIn("boot.py", arg)

    def test_arguments_quote_a_path_with_spaces(self):
        """真的给一个带空格的目录 —— 引号要把它整段包住。"""
        spaced = Path(tempfile.mkdtemp()) / "门店 测试 目录"
        spaced.mkdir()
        root = ET.fromstring(autostart.task_xml(spaced))
        arg = root.find(f".//{NS}Arguments").text
        self.assertEqual(arg, f'"{spaced / "boot.py"}"')

    def test_no_xml_comments(self):
        """`schtasks` 的解析校验本机验不了 —— 就不给它增加输入面。解释写在 Python 里。"""
        self.assertNotIn("<!--", autostart.task_xml(self.root))

    def test_xml_file_is_utf16(self):
        """schtasks 要求任务 XML 是 Unicode；描述里有中文，写 UTF-8 有被拒的风险。"""
        p = autostart._write_task_xml(self.root)
        raw = p.read_bytes()
        self.assertTrue(raw[:2] in (b"\xff\xfe", b"\xfe\xff"),
                        f"没带 UTF-16 BOM：{raw[:4]!r}")
        self.assertIn("以管理员身份", raw.decode("utf-16"))

    def test_xml_round_trips_through_our_own_parser(self):
        """我们自己要能把它读回来（状态页显示的就是读回来的内容）。"""
        from src.winutil import parse_xml, xml_text
        p = autostart._write_task_xml(self.root)
        raw = p.read_bytes()
        r = parse_xml(raw)
        self.assertIsNotNone(r)
        self.assertEqual(xml_text(r, "RunLevel"), "HighestAvailable")


class TestInstall(_WinCase):
    def test_prefers_elevated_task(self):
        calls = self._schtasks([_ok()])
        res = autostart._win_install(self.root, elevated=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "task")
        self.assertTrue(res["elevated"], "注册成计划任务就应该是提权的")
        self.assertIn("管理员身份", res["message"], "成功提示要写明是提权的")
        self.assertIn("UAC", res["message"], "要说明不弹 UAC —— 这正是选计划任务的原因")
        self.assertIn("/xml", calls[0])
        self.assertEqual(calls[0][calls[0].index("/tn") + 1], autostart.AUTOSTART_TASK)

    def test_falls_back_to_command_line_when_xml_rejected(self):
        """不同 Windows 版本对任务 XML 的校验宽严不一 —— 命令行那条路要能兜住。"""
        calls = self._schtasks([_fail("XML 格式错误"), _ok()])
        res = autostart._win_install(self.root, elevated=True)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "task")
        self.assertTrue(res["elevated"])
        self.assertEqual(len(calls), 2)
        self.assertIn("onlogon", calls[1])
        self.assertIn("HIGHEST", calls[1])

    def test_explicit_normal_mode_skips_the_task_entirely(self):
        """用户明确选了「普通权限」—— 就**别再去碰计划任务**了。

        这条路是给 Chrome 抓会话留的退路：Chrome 138+ 从管理员进程启动会把自己
        降权重启（AutoDeElevate），个别机器上这一步会失败 —— 那时唯一的解法
        就是让服务别以管理员跑。
        """
        calls = self._schtasks([_ok()])
        res = autostart._win_install(self.root, elevated=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["mode"], "runkey")
        self.assertFalse(res["elevated"])
        self.assertIn(autostart.APP_NAME, self.reg.store)
        # 唯一那条 schtasks 调用只可能是"删掉提权任务"，绝不该是 /create
        for a in calls:
            self.assertNotIn("/create", a,
                             "选了普通权限还去注册计划任务了")

    def test_degrades_to_runkey_without_admin(self):
        """想提权但提不到时**不能假装成功** —— 要退回注册表并说清楚不是管理员。"""
        calls = self._schtasks([_fail("拒绝访问"), _fail("拒绝访问")])
        res = autostart._win_install(self.root, elevated=True)
        self.assertTrue(res["ok"], "功能还是可用的，只是没提权")
        self.assertEqual(res["mode"], "runkey")
        self.assertFalse(res["elevated"])
        self.assertIn("管理员", res["message"])
        self.assertIn("install.bat", res["message"])
        self.assertEqual(len(calls), 2)
        self.assertIn(autostart.APP_NAME, self.reg.store)

    def test_switching_modes_cleans_up_the_other_one(self):
        """两种方式**互斥** —— 换了就把另一条清掉。

        否则任务列表里会多一条没人认识的；虽然 pidfile 挡得住第二个实例，
        但用户看到"我明明关了开机自启，怎么还有个任务"只会更慌。
        """
        # 切到普通权限 → 要删掉提权任务
        self.reg.store[autostart.APP_NAME] = "old"
        calls = self._schtasks([_ok()])
        autostart._win_install(self.root, elevated=False)
        self.assertTrue(any("/delete" in a for a in calls),
                        "切到普通权限时没把计划任务删掉")

        # 切回管理员 → 要清掉注册表项
        self.reg.store[autostart.APP_NAME] = "old"
        calls2 = self._schtasks([_ok()])
        res = autostart._win_install(self.root, elevated=True)
        self.assertEqual(res["mode"], "task")
        self.assertNotIn(autostart.APP_NAME, self.reg.store,
                         "切回管理员时注册表项没清掉")

    def test_reports_failure_when_everything_fails(self):
        # 计划任务两条路都挂，且写注册表也抛错
        self._schtasks([_fail("拒绝"), _fail("拒绝")])
        boom = mock.Mock()
        boom.CreateKeyEx.side_effect = OSError("注册表被锁")
        p = mock.patch.dict(sys.modules, {"winreg": boom})
        p.start()
        self.addCleanup(p.stop)
        res = autostart._win_install(self.root, elevated=True)
        self.assertFalse(res["ok"])
        self.assertIn("失败", res["message"])

    def test_uses_xml_not_tr_for_the_task(self):
        """回归：绝对不能再退回手工加引号的 `/tr`（会被 list2cmdline 转义成 \\"…\\"）。"""
        calls = self._schtasks([_ok()])
        autostart._win_install(self.root, elevated=True)
        self.assertNotIn("/tr", calls[0])


class TestStatus(_WinCase):
    def _query_returns(self, xml_text_):
        def fake(args, **kw):
            if "/query" in args and "/xml" in args:
                return _ok(xml_text_)
            return _fail()
        p = mock.patch.object(autostart, "schtasks", fake)
        p.start()
        self.addCleanup(p.stop)

    def test_reads_elevated_task(self):
        self._query_returns(autostart.task_xml(self.root))
        st = autostart._win_status()
        self.assertTrue(st["installed"])
        self.assertEqual(st["mode"], "task")
        self.assertTrue(st["elevated"])
        self.assertEqual(st["run_level"], "HighestAvailable")

    def test_detects_non_elevated_task(self):
        xml = autostart.task_xml(self.root).replace("HighestAvailable", "LeastPrivilege")
        self._query_returns(xml)
        st = autostart._win_status()
        self.assertTrue(st["installed"])
        self.assertFalse(st["elevated"], "LeastPrivilege 不是提权，不能显示成管理员")

    def test_detects_runkey_fallback(self):
        self._query_returns("")           # 任务不存在
        self.reg.SetValueEx(_FakeWinreg._Key(self.reg), autostart.APP_NAME, 0,
                            _FakeWinreg.REG_SZ, r'"C:\py\pythonw.exe" "D:\cbg\boot.py"')
        st = autostart._win_status()
        self.assertTrue(st["installed"])
        self.assertEqual(st["mode"], "runkey")
        self.assertFalse(st["elevated"])

    def test_not_installed(self):
        self._query_returns("")
        st = autostart._win_status()
        self.assertFalse(st["installed"])
        self.assertIsNone(st["mode"])


class TestRemove(_WinCase):
    def test_clears_both_task_and_runkey(self):
        self.reg.store[autostart.APP_NAME] = "old"
        calls = self._schtasks([_ok()])
        res = autostart._win_remove()
        self.assertTrue(res["ok"])
        self.assertIn("/delete", calls[0])
        self.assertNotIn(autostart.APP_NAME, self.reg.store, "注册表项没清掉")

    def test_removing_when_nothing_registered_is_ok(self):
        self._schtasks([_fail()])
        res = autostart._win_remove()
        self.assertTrue(res["ok"])


class TestFailuresDoNotCrash(unittest.TestCase):
    """注册失败必须是 `ok:false` + message，**不能把 OSError 抛出去**。

    抛出去的话命令行下是一段 traceback、界面上是「HTTP 500」——
    用户看不到"权限不够，该怎么办"，只看到一个英文栈。
    """

    def test_install_catches_oserror(self):
        root = Path(tempfile.mkdtemp())
        (root / "boot.py").write_text("# stub\n", encoding="utf-8")
        with mock.patch.object(autostart, "_linux_install",
                               side_effect=PermissionError("Operation not permitted")), \
                mock.patch.object(autostart, "kind", lambda: "linux"):
            res = autostart.install(root)
        self.assertFalse(res["ok"])
        self.assertIn("权限", res["message"])

    def test_remove_catches_oserror(self):
        with mock.patch.object(autostart, "_linux_remove",
                               side_effect=PermissionError("nope")), \
                mock.patch.object(autostart, "kind", lambda: "linux"):
            res = autostart.remove()
        self.assertFalse(res["ok"])
        self.assertIn("失败", res["message"])


class TestCommandString(unittest.TestCase):
    def test_command_quotes_both_paths(self):
        """路径带空格时两个都要加引号 —— 之前的教训。"""
        with mock.patch.object(autostart, "_python_exe",
                               lambda: r"C:\Program Files\Python39\pythonw.exe"):
            cmd = autostart.command(Path("/x/y"))
        self.assertTrue(cmd.startswith('"C:\\Program Files'), cmd)
        self.assertIn('" ', cmd, "解释器和脚本之间要有空格")

    def test_boot_script_is_boot_py(self):
        self.assertEqual(autostart.boot_script("/a/b").name, "boot.py")


class TestTaskNameIsDistinct(unittest.TestCase):
    def test_autostart_task_differs_from_daily_task(self):
        """开机自启的任务名不能跟「定时执行」那个撞 ——
        撞了的话开机自启会把每天对账那条覆盖掉，用户还找不到原因。
        """
        from src import schedule
        self.assertNotEqual(autostart.AUTOSTART_TASK, schedule.TASK_NAME)
        self.assertTrue(autostart.AUTOSTART_TASK.startswith(schedule.TASK_NAME),
                        "前缀留着，用户在任务列表里能看出是同一套东西")


if __name__ == "__main__":
    unittest.main()
