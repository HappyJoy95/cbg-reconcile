"""邮件推送测试。

**不真发邮件** —— 把 smtplib 换成假的，测的是：
配置怎么读、什么情况该发、邮件怎么拼（尤其是中文主题和中文附件名）。
"""

import json
import smtplib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import config_io, mailer


class FakeSMTP:
    """假的 SMTP 服务器，记录被调用了什么。"""

    last = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.login_args = None
        self.tls = False
        self.sent = None
        self.quit = False
        FakeSMTP.last = self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.quit = True
        return False

    def ehlo(self):
        return (250, b"ok")

    def starttls(self):
        self.tls = True

    def login(self, u, p):
        self.login_args = (u, p)

    def send_message(self, msg):
        self.sent = msg


def _mc(**over):
    base = dict(enabled=True, host="smtp.test.com", port=465, security="ssl",
                username="u@test.com", password="pw", sender="",
                recipients=["a@x.com"], subject_prefix="[报量对账]", when="always")
    base.update(over)
    return mailer.MailConfig(**base)


class TestRecipients(unittest.TestCase):
    def test_splits_on_common_separators(self):
        r = mailer.split_recipients("a@x.com, b@y.com;c@z.com\nd@w.com")
        self.assertEqual(r, ["a@x.com", "b@y.com", "c@z.com", "d@w.com"])

    def test_dedupes_case_insensitively(self):
        self.assertEqual(mailer.split_recipients("A@x.com, a@X.com"), ["A@x.com"])

    def test_drops_non_addresses(self):
        self.assertEqual(mailer.split_recipients("a@x.com, 这不是邮箱, , b@y.com"),
                         ["a@x.com", "b@y.com"])

    def test_empty(self):
        self.assertEqual(mailer.split_recipients(""), [])
        self.assertEqual(mailer.split_recipients(None), [])


class TestLoadConfig(unittest.TestCase):
    """⚠ 每个用例自带**空 root**：`root=None` 会读到开发机的
    `.secrets/push-paths.json`（2026-09-22 路径列表），整段 dict 配置被短路。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_reads_from_dict(self):
        cfg = {"mail": {"enabled": True, "host": "smtp.x.com", "port": 587,
                        "security": "starttls", "recipients": "a@x.com,b@y.com",
                        "sender": "s@x.com", "subject_prefix": "[对账]", "when": "only_diff"}}
        mc = mailer.load_mail_config(cfg, self.root)
        self.assertTrue(mc.enabled)
        self.assertEqual(mc.host, "smtp.x.com")
        self.assertEqual(mc.port, 587)
        self.assertEqual(mc.security, "starttls")
        self.assertEqual(mc.recipients, ["a@x.com", "b@y.com"])
        self.assertEqual(mc.from_addr, "s@x.com")
        self.assertEqual(mc.when, "only_diff")

    def test_password_comes_from_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "mail.env"
            env.write_text("MAIL_USERNAME=u@x.com\nMAIL_PASSWORD=SECRET\n", encoding="utf-8")
            mc = mailer.load_mail_config(
                {"mail": {"env_file": str(env), "recipients": "a@x.com"}}, self.root)
            self.assertEqual(mc.password, "SECRET")
            self.assertEqual(mc.username, "u@x.com")

    def test_enabled_string_forms(self):
        for raw, want in [("true", True), ("1", True), ("yes", True), ("on", True),
                          ("false", False), ("0", False), ("", False), (True, True)]:
            mc = mailer.load_mail_config({"mail": {"enabled": raw}}, self.root)
            self.assertIs(mc.enabled, want, f"enabled={raw!r}")

    def test_bad_port_falls_back(self):
        mc = mailer.load_mail_config({"mail": {"port": "不是数字"}}, self.root)
        self.assertEqual(mc.port, 465)


class TestShouldSend(unittest.TestCase):
    def test_disabled(self):
        ok, why = mailer.should_send(_mc(enabled=False), has_diff=True)
        self.assertFalse(ok)
        self.assertIn("没有邮件推送路径", why)

    def test_when_and_diff_ignored(self):
        """2026-09-22 改版：不再看 when / has_diff —— 配了路径就发。"""
        ok, why = mailer.should_send(_mc(when="only_diff"), has_diff=False)
        self.assertTrue(ok, why)
        # 签名里的 has_diff / ignore_when 仍收（业务调用点不改）
        ok2, why2 = mailer.should_send(_mc(when="only_diff"), has_diff=False,
                                       ignore_when=False)
        self.assertTrue(ok2, why2)

    def test_always_sends_even_without_diff(self):
        """默认每次都发 —— 「没收到邮件」和「没跑」要能区分开。"""
        self.assertTrue(mailer.should_send(_mc(), has_diff=False)[0])

    def test_missing_config(self):
        for over in ({"host": ""}, {"recipients": []}, {"username": "", "sender": ""}):
            ok, why = mailer.should_send(_mc(**over), has_diff=True)
            self.assertFalse(ok, over)
            self.assertIn("配置不全", why)


class TestPaths(unittest.TestCase):
    """路径列表读写 + 老配置回落（2026-09-22）。"""

    def test_from_row_enables_and_parses(self):
        mc = mailer.mail_from_row({
            "host": "smtp.x.com", "port": "587", "security": "starttls",
            "username": "u@x.com", "password": "pw",
            "recipients": "a@x.com, b@y.com", "sender": "",
        })
        self.assertTrue(mc.enabled)
        self.assertEqual(mc.port, 587)
        self.assertEqual(mc.recipients, ["a@x.com", "b@y.com"])
        self.assertTrue(mc.ready)

    def test_paths_file_roundtrip(self):
        from src import push_paths
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            mailer.save_mail_paths([{
                "host": "smtp.a.com", "port": 465, "security": "ssl",
                "username": "a@x.com", "password": "S3CRET",
                "recipients": "a@x.com", "subject_prefix": "[t]",
            }], root)
            self.assertTrue(push_paths.has_file(root))
            paths = mailer.load_mail_paths({}, root)
            self.assertEqual(len(paths), 1)
            pid, mc = paths[0]
            self.assertTrue(pid)
            self.assertEqual(mc.password, "S3CRET")
            # describe 绝不回密码明文（has_password 布尔可以有）
            dsc = mailer.describe_mail_paths({}, root)
            blob = json.dumps(dsc, ensure_ascii=False)
            self.assertNotIn("S3CRET", blob)
            self.assertNotIn('"password"', blob)
            self.assertTrue(dsc["paths"][0]["has_password"])

    def test_empty_paths_file_means_not_configured(self):
        from src import push_paths
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            push_paths.save({"mail": [], "wecom": []}, root)
            self.assertEqual(mailer.load_mail_paths({}, root), [])
            mc = mailer.load_mail_config({}, root)
            self.assertFalse(mc.enabled)
            ok, why = mailer.should_send(mc, has_diff=True)
            self.assertFalse(ok)
            self.assertIn("没有邮件推送路径", why)

    def test_legacy_fallback_without_file(self):
        """没 push-paths.json 时从老 mail: 读成一条（门店不用重填）。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = root / "mail.env"
            env.write_text("MAIL_USERNAME=u@x.com\nMAIL_PASSWORD=SECRET\n",
                           encoding="utf-8")
            cfg = {"mail": {"enabled": True, "host": "smtp.x.com",
                            "recipients": "a@x.com",
                            "env_file": str(env)}}
            paths = mailer.load_mail_paths(cfg, root)
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0][1].password, "SECRET")

    def test_migrate_once(self):
        from src import push_paths
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = {"mail": {"enabled": True, "host": "smtp.x.com",
                            "recipients": "a@x.com", "sender": "s@x.com",
                            "env_file": str(root / "mail.env")},
                   "wecom": {"enabled": True, "webhook": URL_WECOM}}
            (root / "mail.env").write_text(
                "MAIL_USERNAME=u@x.com\nMAIL_PASSWORD=PW\n", encoding="utf-8")
            self.assertTrue(push_paths.migrate_from_legacy(cfg, root))
            self.assertTrue(push_paths.has_file(root))
            # 第二次不动
            self.assertFalse(push_paths.migrate_from_legacy(cfg, root))
            self.assertEqual(len(mailer.load_mail_paths(cfg, root)), 1)


URL_WECOM = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=693a91e1-1111-2222-3333-444455556666"


class TestBuildMessage(unittest.TestCase):
    def test_subject_has_prefix_and_recipients(self):
        msg = mailer.build_message(_mc(), "青岛新业 2026-09-14 · ❌ 未报量 2 台", "正文")
        self.assertIn("[报量对账]", msg["Subject"])
        self.assertIn("青岛新业", msg["Subject"])
        self.assertEqual(msg["To"], "a@x.com")
        self.assertEqual(msg["From"], "u@test.com")

    def test_chinese_subject_survives_roundtrip(self):
        """中文主题必须能正确编码 —— 收件箱里不能是乱码。

        （主题会被 RFC2047 编码成 `=?utf-8?b?...?=`，这是对的；
        这里用 policy=default 解回来验证内容没丢。）
        """
        import email
        import email.policy
        msg = mailer.build_message(_mc(), "青岛新业广场店 · 未报量 2 台", "正文")
        again = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
        self.assertIn("青岛新业广场店", str(again["Subject"]))
        self.assertIn("未报量 2 台", str(again["Subject"]))

    def test_chinese_body_survives_roundtrip(self):
        import email
        import email.policy
        msg = mailer.build_message(_mc(), "s", "青岛新业广场店 未报量 2 台")
        again = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
        self.assertIn("青岛新业广场店", again.get_content())

    def test_attaches_xlsx_with_chinese_filename(self):
        import email
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "差异_2026-09-14_SCN231409.xlsx"
            f.write_bytes(b"PK\x03\x04fake")
            msg = mailer.build_message(_mc(), "s", "b", [f])
            again = email.message_from_bytes(msg.as_bytes())
            names = [p.get_filename() for p in again.walk() if p.get_filename()]
            self.assertIn("差异_2026-09-14_SCN231409.xlsx", names)

    def test_missing_attachment_is_skipped_not_fatal(self):
        msg = mailer.build_message(_mc(), "s", "b", ["/no/such/file.xlsx"])
        self.assertEqual([p for p in msg.walk() if p.get_filename()], [])


class TestSend(unittest.TestCase):
    def test_to_能传下去(self):
        """⚠⚠ `send(to=[…])` —— **真发那一下才发现的 bug**（2026-09-21）。

        `notify._send_mail` 一直是按 `to=[区长邮箱]` 调的，而 `mailer.send()`
        **签名里没有 `to`** ⇒ `TypeError: send() got an unexpected keyword argument 'to'`
        —— **所有"指定收件人"的邮件一封都发不出去**（M18 上报、M21 拆分都是），
        而它们各自的测试都把 `notify.send` 打了桩，所以一路绿灯到真发才露出来。
        ⇒ 这条钉住"收下 `to` 并转给 `build_message`"。
        """
        with mock.patch.object(mailer, "_SMTP4SSL", FakeSMTP):
            mailer.send(_mc(security="ssl"), "主题", "正文", to=["jiuzhang@example.com"])
        msg = FakeSMTP.last.sent
        self.assertEqual(msg["To"], "jiuzhang@example.com")
        # ⚠ 指定了 `to` 之后，配置里那份收件人**不该再被发到**（"这封只发给他"）
        self.assertNotIn("a@x.com", msg["To"] or "")

    def test_不传_to_还是配置里的收件人(self):
        with mock.patch.object(mailer, "_SMTP4SSL", FakeSMTP):
            mailer.send(_mc(security="ssl"), "主题", "正文")
        self.assertNotIn("jiuzhang@example.com", FakeSMTP.last.sent["To"] or "")

    def test_ssl_login_and_send(self):
        with mock.patch.object(mailer, "_SMTP4SSL", FakeSMTP):
            mailer.send(_mc(security="ssl"), "主题", "正文")
        s = FakeSMTP.last
        self.assertEqual((s.host, s.port), ("smtp.test.com", 465))
        self.assertEqual(s.login_args, ("u@test.com", "pw"))
        self.assertFalse(s.tls, "SSL 模式不该再 STARTTLS")
        self.assertIsNotNone(s.sent)

    def test_starttls_calls_starttls(self):
        with mock.patch.object(mailer, "_SMTP4", FakeSMTP):
            mailer.send(_mc(security="starttls", port=587), "主题", "正文")
        self.assertTrue(FakeSMTP.last.tls)

    def test_none_security_still_works(self):
        with mock.patch.object(mailer, "_SMTP4", FakeSMTP):
            mailer.send(_mc(security="none", port=25), "主题", "正文")
        self.assertFalse(FakeSMTP.last.tls)
        self.assertIsNotNone(FakeSMTP.last.sent)

    def test_auth_error_becomes_mail_error_with_hint(self):
        class Bad(FakeSMTP):
            def login(self, u, p):
                raise smtplib.SMTPAuthenticationError(535, b"bad auth")

        with mock.patch.object(mailer, "_SMTP4SSL", Bad):
            with self.assertRaises(mailer.MailError) as ctx:
                mailer.send(_mc(), "s", "b")
        self.assertIn("授权码", str(ctx.exception), "要提示多半是授权码的问题")

    def test_connection_error_becomes_mail_error(self):
        class Dead(FakeSMTP):
            def __init__(self, *a, **k):
                raise OSError("连不上")

        with mock.patch.object(mailer, "_SMTP4SSL", Dead):
            with self.assertRaises(mailer.MailError) as ctx:
                mailer.send(_mc(), "s", "b")
        self.assertIn("连不上", str(ctx.exception))

    def test_no_login_when_no_username(self):
        with mock.patch.object(mailer, "_SMTP4SSL", FakeSMTP):
            mailer.send(_mc(username="", sender="noreply@x.com"), "s", "b")
        self.assertIsNone(FakeSMTP.last.login_args)
        self.assertIsNotNone(FakeSMTP.last.sent)


class TestDescribe(unittest.TestCase):
    def test_never_leaks_password(self):
        d = mailer.describe_mail(_mc(password="TOP-SECRET"))
        self.assertNotIn("TOP-SECRET", repr(d))
        self.assertNotIn("password", d)
        self.assertTrue(d["has_password"])

    def test_reports_problems(self):
        d = mailer.describe_mail(_mc(host="", recipients=[]))
        self.assertFalse(d["ready"])
        self.assertIn("SMTP 服务器", d["problems"])
        self.assertIn("收件人", d["problems"])

    def test_ships_presets(self):
        d = mailer.describe_mail(_mc())
        self.assertTrue(any(p["host"] == "smtp.qq.com" for p in d["presets"]))


class TestSubjectLine(unittest.TestCase):
    def _ctx(self):
        return {"门店": "青岛新业广场店", "目标日": "2026-09-14", "配置文件": "x.yaml"}

    def test_subject_says_the_result_without_opening(self):
        s, _ = mailer.build_report_mail(self._ctx(), ["line"], missing=2)
        # ⚠ 口径名 2026-09-16 由门店定名：「玲珑」= 华为那个销售系统的代号
        self.assertIn("玲珑无但云商有 2 台", s)
        s, _ = mailer.build_report_mail(self._ctx(), ["line"], missing=0)
        self.assertIn("无差异", s)
        s, _ = mailer.build_report_mail(self._ctx(), ["line"], missing=0, unshipped=1)
        self.assertIn("玲珑有但云商无", s)

    def test_body_contains_summary_and_footer(self):
        _, b = mailer.build_report_mail(self._ctx(), ["第一行", "第二行"], missing=1)
        self.assertIn("第一行", b)
        self.assertIn("第二行", b)
        self.assertIn("cbg-reconcile", b)


class TestYamlQuoting(unittest.TestCase):
    """`subject_prefix: [报量对账]` 里开头的方括号在 YAML 里是**流式列表** ——
    不加引号会被解析成 ["报量对账"]，值悄悄变成列表。"""

    def test_bracketed_value_stays_a_string(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.yaml"
            p.write_text("mail:\n  subject_prefix: old\n", encoding="utf-8")
            config_io.update(p, {"mail.subject_prefix": "[报量对账]"})
            cfg = config_io.load_raw(p)
            self.assertEqual(cfg["mail"]["subject_prefix"], "[报量对账]")
            self.assertIsInstance(cfg["mail"]["subject_prefix"], str)

    def test_roundtrip_various_values(self):
        import yaml
        for val in ["[报量对账]", "a: b", "#开头", "true", "123", "带 空格", "普通"]:
            out = config_io._fmt(val)
            self.assertEqual(yaml.safe_load(f"k: {out}")["k"], val, f"{val!r} 序列化错了")


if __name__ == "__main__":
    unittest.main()


class TestIPv4Only(unittest.TestCase):
    """smtp.qq.com 有 IPv6 地址但那条路会卡 —— 必须只走 IPv4。"""

    def test_uses_af_inet(self):
        import socket
        seen = {}

        def fake_getaddrinfo(host, port, family=0, type=0, *a, **k):
            seen["family"] = family
            raise OSError("stop here")

        with mock.patch("socket.getaddrinfo", fake_getaddrinfo):
            self.assertIsNone(mailer._connect_ipv4("smtp.test.com", 465, 5))
        self.assertEqual(seen["family"], socket.AF_INET)

    def test_returns_none_when_no_ipv4_so_caller_falls_back(self):
        with mock.patch("socket.getaddrinfo", lambda *a, **k: []):
            self.assertIsNone(mailer._connect_ipv4("only6.test.com", 465, 5))

    def test_connect_failure_raises(self):
        import socket as _s
        infos = [(_s.AF_INET, _s.SOCK_STREAM, 6, "", ("127.0.0.1", 1))]
        with mock.patch("socket.getaddrinfo", lambda *a, **k: infos):
            with self.assertRaises(OSError):
                mailer._connect_ipv4("x.test.com", 1, 1)


class Test中台邮箱(unittest.TestCase):
    """用户 2026-09-19：「加一个**中台邮箱**，**在没有发件邮箱时默认使用这个**，
    **接收时都带上这个**」。

    * 「没有发件邮箱时默认使用这个」= 这台机器**自己那份配不齐** ⇒ 用中台账号发；
      ⚠ 中台那份**也得配了才顶得上**，什么都没配照旧报"没配邮件"（不静默不发）。
    * 「接收时都带上」= 每封都**抄送**中台（`Cc`，不是 `To`，收件人一眼看得出抄送给了谁）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".secrets").mkdir(parents=True)

    def _env(self, text):
        (self.root / ".secrets" / "mail.env").write_text(text, encoding="utf-8")

    def test_没授权码就当中台没配(self):
        """⚠ 地址是**公开常量**（随包走），但**没授权码发不出去** ⇒ 当没配。
        顶上来只会让调用方以为"能发了"，然后卡在 SMTP 认证失败上。"""
        self.assertIsNone(mailer.central_config(self.root))
        mc = mailer.load_mail_config({}, self.root)
        self.assertFalse(mc.ready, "什么都没配 ⇒ 还是'没配邮件'，不能静默不发")

    def test_自己那份配不齐时用中台(self):
        self._env("MAIL_CENTRAL_PASSWORD=pw\n")          # 只放授权码，其余走公开常量
        mc = mailer.load_mail_config({}, self.root)
        self.assertTrue(mc.ready, "门店没配 ⇒ 用中台账号发")
        self.assertEqual(mc.username, mailer.CENTRAL_ADDR)
        self.assertEqual(mc.host, mailer.CENTRAL_HOST)
        self.assertEqual(mc.cc, [mailer.CENTRAL_ADDR])

    def test_自己那份配好了就用自己那份(self):
        """⚠ 分工：**主机/收件人在门店配置里**（设置页写的），`.secrets/mail.env`
        只管**口令那半**（`MAIL_USERNAME` / `MAIL_PASSWORD`）——
        我第一版把 `MAIL_HOST` 也写进 env 文件，结果主机是空的、被判成"没配"，
        中台就把自己顶上来了（测试逮到的）。"""
        self._env("MAIL_CENTRAL_PASSWORD=pw\n"
                  "MAIL_USERNAME=store@x.com\nMAIL_PASSWORD=pw2\n")
        cfg = {"mail": {"enabled": True, "host": "smtp.store.com",
                        "recipients": "boss@x.com"}}
        mc = mailer.load_mail_config(cfg, self.root)
        self.assertEqual(mc.username, "store@x.com", "自己配好了就别抢")
        self.assertEqual(mc.cc, [mailer.CENTRAL_ADDR], "但中台还是要抄送")

    def test_每封都抄送中台(self):
        mc = mailer.MailConfig(enabled=True, host="h", port=465, username="u",
                               password="x", recipients=["a@x.com"],
                               cc=["central@x.com"], when="always")
        msg = mailer.build_message(mc, "主题", "正文")
        self.assertEqual(msg["To"], "a@x.com")
        self.assertEqual(msg["Cc"], "central@x.com")

    def test_收件人就是中台时不重复(self):
        mc = mailer.MailConfig(enabled=True, host="h", port=465, username="u",
                               password="x", recipients=["central@x.com"],
                               cc=["central@x.com"], when="always")
        self.assertIsNone(mailer.build_message(mc, "主题", "正文").get("Cc"))

    def test_明确关掉就绝不顶中台(self):
        """⚠ 配置里写了 `enabled: false` = **主动关掉邮件** ——
        中台那份**绝不能**顶上来，否则就成了"关了还在发"，那是最不该有的行为。"""
        self._env("MAIL_CENTRAL_PASSWORD=pw\n")
        mc = mailer.load_mail_config({"mail": {"enabled": False}}, self.root)
        self.assertFalse(mc.enabled)
        self.assertNotEqual(mc.username, mailer.CENTRAL_ADDR, "关了就是关了")

    def test_自己配过口令就别抢(self):
        """⚠ 自己那份**已经有口令** ⇒ 这台机器配过，别用中台盖掉它 ——
        盖了会让人以为"我配的没生效"。（缺主机/收件人该报出来，不是悄悄换账号。）"""
        self._env("MAIL_USERNAME=mine@x.com\nMAIL_PASSWORD=mine-pw\n"
                  "MAIL_CENTRAL_PASSWORD=central-pw\n")
        mc = mailer.load_mail_config({"mail": {"host": "smtp.mine.com",
                                               "recipients": "a@x.com"}}, self.root)
        self.assertEqual(mc.username, "mine@x.com")
        self.assertEqual(mc.password, "mine-pw")
