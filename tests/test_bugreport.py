"""「上报 bug」—— 收集现场、打包、试着推出去。

**这个文件里最要紧的是"凭据绝不进包"那几条。** 包是要发给别人的，
漏一次就等于把门店的云商账号 / 华为会话 / 邮箱授权码发出去了 ——
而且**发出去就收不回来**。

用户 2026-09-16 提这个功能时自己也问了："日志会不会 webhook 推不出去？"
所以另外几条钉的是**推送失败时的行为**：包必须留在本地、必须把路径说出来。
"""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import bugreport, cli

ROOT = Path(__file__).resolve().parent.parent


def _store(**files):
    """造一个"门店电脑"的目录。`files` 是 相对路径 → 内容。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / ".secrets").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp, root


def _names(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return z.namelist()


def _all_text(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())


class TestNoSecretsEverGetIn(unittest.TestCase):
    """⚠⚠ **这组是这个功能的底线。**

    包是要发出去的（邮件附件 / 企微文件），漏一次就收不回来了。
    """

    FAKE_SECRETS = {
        ".secrets/erp.env": "ERP_USERNAME=laoban\nERP_PASSWORD=hunter2\n",
        ".secrets/cbg-SCN231409.json": '{"cookie":"SESSION=abcdef","csrfToken":"zzz"}',
        ".secrets/huawei.env": "HW_PASSWORD=hw-secret\n",
        ".secrets/mail.env": "QQ_EMAIL_AUTH_CODE=abcd1234\n",
        ".secrets/wecom.env":
            "WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=deadbeef\n",
        ".secrets/browser-profile/Cookies": "session-cookie-blob",
    }

    def _pack(self, extra=None):
        files = dict(self.FAKE_SECRETS)
        files["config/store-X.yaml"] = "store_code: SCN231409\n"
        files["out/run.log"] = "=== 跑了一次 ===\n[1/3] 抓华为\n"
        files.update(extra or {})
        tmp, root = _store(**files)
        self.addCleanup(tmp.cleanup)
        return root, bugreport.build_zip(root, "config/store-X.yaml")

    def test_凭据文件一个都没进包(self):
        root, z = self._pack()
        names = _names(z)
        for bad in (".env", "cbg-", "browser-profile", "Cookies"):
            with self.subTest(bad=bad):
                self.assertFalse([n for n in names if bad in n],
                                 "包里混进了 %s 相关的东西：%s" % (bad, names))

    def test_凭据内容也没进包(self):
        """⚠ 光看文件名不够 —— 内容里出现了才算真漏。"""
        root, z = self._pack()
        text = _all_text(z)
        for needle in ("hunter2", "hw-secret", "abcd1234", "deadbeef",
                       "SESSION=abcdef", "session-cookie-blob"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_反查能拦下人为塞进来的凭据(self):
        """⚠ `assert_no_secrets` 是**第二道**防线（第一道是"根本不收集"）。

        没有它的话，哪天有人加了个新条目、又忘了加排除，就静默漏出去了。
        """
        with self.assertRaises(ValueError):
            bugreport.assert_no_secrets([("随便.txt", b"ERP_PASSWORD=x")])
        with self.assertRaises(ValueError):
            bugreport.assert_no_secrets([("cbg-SCN1.json", b"{}")])
        with self.assertRaises(ValueError):
            bugreport.assert_no_secrets(
                [("x.txt", b"cbgSession: {\"cookie\": \"a\"}")])

    def test_正常的包不会被反查误伤(self):
        bugreport.assert_no_secrets([("执行日志.txt", "=== 跑了一次 ===\n".encode())])

    def test_反查失败时什么都不发出去(self):
        """⚠ 拦下来之后**不许留下半个包** —— 用户会以为"上报成功了"。"""
        tmp, root = _store(**{"out/run.log": "x"})
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(bugreport, "collect",
                               side_effect=ValueError("⛔ 测试用")):
            with self.assertRaises(ValueError):
                bugreport.build_zip(root, "config/store-X.yaml")
        self.assertEqual(list((root / "out").glob("*.zip")), [])


class TestRedaction(unittest.TestCase):
    """第二道防线：就算哪天混进来一行，也不原样发出去。"""

    def test_看着像凭据的行会被打码(self):
        for line in ("ERP_PASSWORD=hunter2", "密码: abc", "token=xyz",
                     "webhook: https://qyapi...key=zzz", "username: laoban"):
            with self.subTest(line=line):
                self.assertIn("已打码", bugreport._redact_line(line))
                self.assertNotIn("hunter2", bugreport._redact_line(line))

    def test_普通行不动(self):
        for line in ("[1/3] 抓华为当月", "门店 青岛新业广场店", "云商卖 12 台"):
            with self.subTest(line=line):
                self.assertEqual(bugreport._redact_line(line), line)


class TestLogTail(unittest.TestCase):
    """日志**只追加、从不轮转** —— 门店跑几个月能涨到几十 MB。

    整个塞进包既发不出去（企微 20MB）也没人看，所以只取尾部，
    而且**开头要写明截了多少**（不然看的人以为日志就这么点）。
    """

    def test_没日志不崩(self):
        tmp, root = _store()
        self.addCleanup(tmp.cleanup)
        text, note = bugreport.tail_log(root / "out" / "run.log")
        self.assertEqual(text, "")
        self.assertIn("没有", note)

    def test_短的整份拿走(self):
        tmp, root = _store(**{"out/run.log": "abc\ndef\n"})
        self.addCleanup(tmp.cleanup)
        text, note = bugreport.tail_log(root / "out" / "run.log")
        self.assertIn("abc", text)
        self.assertEqual(note, "")

    def test_长的只取尾部并写明截断(self):
        tmp, root = _store(**{"out/run.log": "头\n" + ("x" * 5000) + "\n尾\n"})
        self.addCleanup(tmp.cleanup)
        text, _ = bugreport.tail_log(root / "out" / "run.log", limit=1000)
        self.assertIn("只有日志的**最后", text)
        self.assertIn("尾", text)
        self.assertNotIn("头\n", text)

    def test_不从半个汉字中间切(self):
        tmp, root = _store(**{"out/run.log": "中文中文中文\n" * 400})
        self.addCleanup(tmp.cleanup)
        text, _ = bugreport.tail_log(root / "out" / "run.log", limit=1001)
        self.assertNotIn("\\ufffd", text)
        self.assertEqual(text.count("\\ufffd"), 0)


class TestBundleContents(unittest.TestCase):
    def test_该有的几份都在(self):
        tmp, root = _store(**{
            "out/run.log": "=== 开始 ===\n",
            "config/store-X.yaml": "store_code: SCN1\n",
        })
        self.addCleanup(tmp.cleanup)
        names = _names(bugreport.build_zip(root, "config/store-X.yaml"))
        for want in ("说明.txt", "执行日志.txt", "定时任务.txt",
                     "配置.yaml", "环境.txt"):
            with self.subTest(want=want):
                self.assertIn(want, names)

    def test_说明里写清了有什么没什么(self):
        tmp, root = _store()
        self.addCleanup(tmp.cleanup)
        z = bugreport.build_zip(root, "config/store-X.yaml")
        with zipfile.ZipFile(z) as zz:
            readme = zz.read("说明.txt").decode("utf-8")
        self.assertIn("没有能拿去登录的凭据", readme)
        self.assertIn("执行日志.txt", readme)
        # ⚠ 也要说清**有**业务数据 —— 不然用户以为可以随便发
        self.assertIn("业务数据", readme)

    def test_打包不碰网络(self):
        """⚠ **这条是设计核心**：最常见的 bug 就是"推送坏了"，
        所以打包那一步必须能独立成功。"""
        tmp, root = _store(**{"out/run.log": "x"})
        self.addCleanup(tmp.cleanup)
        with mock.patch("socket.socket.connect",
                        side_effect=AssertionError("打包不该碰网络")):
            bugreport.build_zip(root, "config/store-X.yaml")

    def test_先写临时文件再改名(self):
        """⚠ 半路断电/被杀不该留下**半个 zip** —— 那比没有更糟：
        用户会拿着一个坏文件以为"上报成功了"。"""
        tmp, root = _store()
        self.addCleanup(tmp.cleanup)
        z = bugreport.build_zip(root, "config/store-X.yaml")
        self.assertTrue(z.is_file())
        self.assertEqual(list((root / "out").glob("*.part")), [])
        with zipfile.ZipFile(z) as zz:                 # 必须是完整的 zip
            self.assertIsNone(zz.testzip())


class TestPushFailureStillLeavesTheBundle(unittest.TestCase):
    """⚠⚠ 用户自己问的："日志会不会 webhook 推不出去？"

    **会** —— 而且那正是最常见的 bug。所以：

    * 推送失败**不算上报失败**（`ok` 仍然是 True）；
    * 包必须留在本地，路径必须说出来；
    * 邮件和企微**各自独立**，一条挂了不影响另一条。
    """

    def _run(self, mail="", wecom=""):
        tmp, root = _store(**{"out/run.log": "x", "config/store-X.yaml": "a: 1\n"})
        self.addCleanup(tmp.cleanup)

        def fake_mail(*a, **k):
            if mail:
                raise RuntimeError(mail)
        def fake_wecom(*a, **k):
            if wecom:
                raise RuntimeError(wecom)
        with mock.patch.object(cli, "load_config", lambda p, **kw: {}), \
             mock.patch.object(cli, "ROOT", root), \
             mock.patch("src.mailer.load_mail_config",
                        side_effect=lambda *a, **k: mock.Mock(
                            recipients=["a@x.com"], when="always", enabled=True)), \
             mock.patch("src.wecom.load_wecom_config",
                        side_effect=lambda *a, **k: mock.Mock(
                            enabled=True, when="always", webhook="k" * 20)), \
             mock.patch("src.mailer.should_send", return_value=(True, "")), \
             mock.patch("src.wecom.should_send", return_value=(True, "")), \
             mock.patch("src.mailer.send", side_effect=fake_mail), \
             mock.patch("src.wecom.send_text", side_effect=fake_wecom), \
             mock.patch("src.wecom.send_file", side_effect=fake_wecom):
            return cli.report_bug(root, "config/store-X.yaml")

    def test_两条都发不出去_包还在_而且_ok(self):
        res = self._run(mail="SMTP 认证失败", wecom="webhook 404")
        self.assertTrue(res["ok"], "推送失败不该把上报判成失败")
        self.assertTrue(Path(res["path"]).is_file(), "推送失败但包必须留着")
        self.assertIn("❌", res["mail"])
        self.assertIn("❌", res["wecom"])
        # ⚠ 说明里必须给出包的路径 —— 用户要拿它人工发
        self.assertIn(res["path"], res["message"])
        self.assertIn("直接把这个文件发给开发者", res["message"])

    def test_一条挂了另一条照发(self):
        res = self._run(mail="SMTP 挂了")
        self.assertIn("企微", res["sent"])
        self.assertNotIn("邮件", res["sent"])

    def test_都成功时消息里也说包在哪(self):
        res = self._run()
        self.assertEqual(sorted(res["sent"]), ["企微", "邮件"])
        self.assertIn(res["path"], res["message"])

    def test_跳过推送时也要打包(self):
        tmp, root = _store(**{"out/run.log": "x", "config/store-X.yaml": "a: 1\n"})
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(cli, "load_config", lambda p, **kw: {}), \
             mock.patch.object(cli, "ROOT", root):
            res = cli.report_bug(root, "config/store-X.yaml",
                                 no_mail=True, no_push=True)
        self.assertTrue(res["ok"])
        self.assertTrue(Path(res["path"]).is_file())


class TestReportBugWiring(unittest.TestCase):
    def test_是子命令(self):
        ap = cli.build_parser()
        sub = [a for a in ap._actions
               if hasattr(a, "choices") and a.choices and "report-bug" in a.choices][0]
        self.assertIn("report-bug", sub.choices)

    def test_子命令能关掉推送(self):
        ap = cli.build_parser()
        ns = ap.parse_args(["report-bug", "--no-mail", "--no-push"])
        self.assertTrue(ns.no_mail)
        self.assertTrue(ns.no_push)


if __name__ == "__main__":
    unittest.main()
