"""云商凭据读写测试。

重点：**界面永不回显密码**、**改账号要作废旧 token**、**注释不能丢**。
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from src import erp
from src.erp import (DEFAULT_COMPANY, describe_credentials, load_credentials,
                     resolve_env_path, save_credentials)


class TestResolveEnvPath(unittest.TestCase):
    def test_relative_goes_to_project_root(self):
        p = resolve_env_path(".secrets/erp.env")
        self.assertTrue(p.is_absolute())
        self.assertTrue(str(p).endswith(".secrets/erp.env"))

    def test_absolute_kept(self):
        self.assertEqual(resolve_env_path("/tmp/x.env"), Path("/tmp/x.env"))


class TestEnvChain(unittest.TestCase):
    """凭据文件链 —— 从"两个账号"那版收回来时挪过来的，行为没变。

    ⚠ 这几条**原来在 `tests/test_erp_roles.py`** 里，那个文件随"门店云商账号"
    一起删了（用户 2026-09-18：「我想了想，不要门店云商账号了，没必要」）。
    但链本身跟角色无关，是"别把 `~/.dsh/secrets/erp.env` 里的真账密
    整份盖成空"那条规矩的守卫，所以搬过来留着。
    """

    def test_链里包含所有老路径(self):
        chain = erp.env_chain()
        for p in erp.LEGACY_ENV_PATHS:
            with self.subTest(p=p):
                self.assertIn(erp.resolve_env_path(str(p)), chain)

    def test_链里没有重复(self):
        """⚠ 重复本身无害，但 `effective_env_file` 的"实际来自"会看着莫名其妙。"""
        chain = erp.env_chain()
        self.assertEqual(len(chain), len(set(chain)), chain)

    def test_优先级是从低到高(self):
        """后面的覆盖前面的 —— 所以 `.secrets/erp.env` 必须排在
        `~/.dsh/secrets/erp.env` **后面**（项目自己的那份说了算）。"""
        chain = erp.env_chain()
        home = erp.resolve_env_path(str(erp.LEGACY_ENV_PATHS[2]))
        local = erp.resolve_env_path(str(erp.LEGACY_ENV_PATHS[0]))
        self.assertLess(chain.index(home), chain.index(local))

    def test_显式指定的文件排最后(self):
        chain = erp.env_chain("/tmp/other.env")
        self.assertEqual(chain[-1], erp.resolve_env_path("/tmp/other.env"))


class Test内置公司账号(unittest.TestCase):
    """一个文件都没配时，要能兜到内置账号（用户 2026-09-18：「后端默认用…」）。"""

    def _clean(self):
        return mock.patch.multiple(
            erp, LEGACY_ENV_PATHS=[], _deobfuscate=lambda blob: "X" if blob else "")

    def test_什么都没有就用内置的(self):
        with self._clean(), mock.patch.dict("os.environ", {}, clear=True):
            d = erp.load_credentials()
        self.assertEqual(d["username"], "X")
        self.assertEqual(d["password"], "X")
        self.assertTrue(d["builtin"], "没标成 builtin，界面上会说不出这是哪来的账号")

    def test_文件里有真凭据就不算内置(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "erp.env"
            f.write_text("ERP_USERNAME=real\nERP_PASSWORD=pw\n", encoding="utf-8")
            with mock.patch.multiple(erp, LEGACY_ENV_PATHS=[f]):
                d = erp.load_credentials()
        self.assertEqual(d["username"], "real")
        self.assertFalse(d["builtin"])

    def test_内置的账号密码能解开(self):
        """⚠ 混淆串写错一个字符就静默变成空串 —— 那样门店只会看到"缺少账号密码"。"""
        self.assertEqual(erp._deobfuscate(erp._BUILTIN_COMPANY_USER), "sL18917405716")
        self.assertEqual(erp._deobfuscate(erp._BUILTIN_COMPANY_PASS), "047081")

    def test_describe_里也有_builtin(self):
        with self._clean(), mock.patch.dict("os.environ", {}, clear=True):
            d = erp.describe_credentials()
        self.assertTrue(d["builtin"])


class TestSaveCredentials(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / "erp.env"

    def tearDown(self):
        self.dir.cleanup()

    def test_creates_file_with_restrictive_perms(self):
        save_credentials(str(self.env), username="u1", password="p1")
        self.assertTrue(self.env.exists())
        mode = stat.S_IMODE(os.stat(self.env).st_mode)
        self.assertEqual(mode, 0o600, "凭据文件必须是 600")

    def test_roundtrip(self):
        save_credentials(str(self.env), username="u1", password="p1", company="00001937")
        c = load_credentials(str(self.env))
        self.assertEqual(c["username"], "u1")
        self.assertEqual(c["password"], "p1")
        self.assertEqual(c["company"], "00001937")

    def test_only_touches_given_fields(self):
        save_credentials(str(self.env), username="u1", password="p1", company="C1")
        save_credentials(str(self.env), password="p2")
        c = load_credentials(str(self.env))
        self.assertEqual(c["username"], "u1", "没传的字段不能被清掉")
        self.assertEqual(c["company"], "C1")
        self.assertEqual(c["password"], "p2")

    def test_preserves_comments_and_unknown_keys(self):
        self.env.write_text(
            "# 云商账号\n"
            "ERP_USERNAME=old   # 别删我\n"
            "SOMETHING_ELSE=keep\n",
            encoding="utf-8")
        save_credentials(str(self.env), username="new")
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("# 云商账号", text)
        self.assertIn("SOMETHING_ELSE=keep", text)
        self.assertIn("# 别删我", text, "行尾注释要保住")
        self.assertIn("ERP_USERNAME=new", text)

    def test_clear_token(self):
        save_credentials(str(self.env), token="TOK123")
        self.assertEqual(load_credentials(str(self.env))["token"], "TOK123")
        save_credentials(str(self.env), username="u2", clear_token=True)
        self.assertEqual(load_credentials(str(self.env))["token"], "",
                         "换了账号，旧 token 必须作废")

    def test_explicit_token_wins_over_clear(self):
        save_credentials(str(self.env), token="OLD")
        save_credentials(str(self.env), username="u2", token="NEW", clear_token=True)
        self.assertEqual(load_credentials(str(self.env))["token"], "NEW")

    def test_default_company(self):
        save_credentials(str(self.env), username="u1", password="p1")
        self.assertEqual(load_credentials(str(self.env))["company"], DEFAULT_COMPANY)


class TestDescribeCredentials(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = Path(self.dir.name) / "erp.env"

    def tearDown(self):
        self.dir.cleanup()

    def test_never_leaks_password(self):
        save_credentials(str(self.env), username="u1", password="SUPER-SECRET")
        d = describe_credentials(str(self.env))
        blob = repr(d)
        self.assertNotIn("SUPER-SECRET", blob, "密码绝不能出现在给界面的数据里")
        self.assertNotIn("password", d)
        self.assertTrue(d["has_password"], "但要让界面知道密码配了")

    def test_masks_token(self):
        save_credentials(str(self.env), token="abcdef1234567890xyz")
        d = describe_credentials(str(self.env))
        self.assertNotIn("abcdef1234567890xyz", repr(d))
        self.assertTrue(d["token"].startswith("abcdef"))
        self.assertIn("…", d["token"])

    def test_missing_file_reports_nothing_configured(self):
        """只读指定文件 —— 不能因为本机别处有凭据就报"已配置"（开发机上有回落，
        但门店电脑的界面必须如实反映它自己那个文件）。"""
        d = describe_credentials(str(self.env))
        self.assertFalse(d["exists"])
        self.assertFalse(d["has_password"])
        self.assertFalse(d["has_token"])
        self.assertEqual(d["username"], "")
        self.assertEqual(d["company"], DEFAULT_COMPANY)

    def test_used_from_points_elsewhere_when_file_has_no_password(self):
        """文件里没密码、但实际会用别处的密码 → 必须如实说，别让人以为改的是这个文件。"""
        self.env.write_text("ERP_USERNAME=u\n", encoding="utf-8")
        d = describe_credentials(str(self.env))
        self.assertFalse(d["has_password"])
        # 开发机上 ~/.dsh/secrets/erp.env 有密码，所以这里应当指出真实来源
        if d["used_from"]:
            self.assertNotEqual(d["used_from"], str(self.env))
            self.assertTrue(d["used_from"].endswith("erp.env"))

    def test_token_alone_does_not_count_as_password_source(self):
        """只存了 token（跑过一次就有）不算密码来源 —— 否则提示会被自己盖掉。"""
        save_credentials(str(self.env), token="TOKENONLY")
        d = describe_credentials(str(self.env))
        self.assertFalse(d["has_password"])
        self.assertTrue(d["has_token"])

    def test_used_from_empty_when_this_file_is_the_source(self):
        save_credentials(str(self.env), username="u1", password="p1")
        d = describe_credentials(str(self.env))
        self.assertEqual(d["used_from"], "", "用的就是这个文件，不该再提示别处")


if __name__ == "__main__":
    unittest.main()


class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class TestLoginCaptcha(unittest.TestCase):
    """云商图形验证码：ResponseID=2 + Data 是 data:image；参数名是 VCode。

    协议来自 `Inventory Check/src/erp.js`（浏览器端那套已经跑通过的实现）。
    """

    def _client(self, payload):
        from unittest import mock
        from src.erp import ErpClient
        c = ErpClient({"username": "u", "password": "p", "company": "C", "token": ""},
                      env_file=str(Path(tempfile.gettempdir()) / "x.env"))
        c.s = mock.MagicMock()
        c.s.post.return_value = _FakeResp(payload)
        return c

    def test_success(self):
        from src.erp import ErpClient  # noqa: F401
        c = self._client({"ResponseID": 0, "Data": {"token": "TK"}})
        self.assertEqual(c.login(), "TK")

    def test_captcha_required_carries_image(self):
        from src.erp import ErpCaptchaRequired
        c = self._client({"ResponseID": 2, "Data": "data:image/png;base64,AAAA"})
        with self.assertRaises(ErpCaptchaRequired) as ctx:
            c.login()
        self.assertEqual(ctx.exception.image, "data:image/png;base64,AAAA")

    def test_vcode_is_sent_as_VCode(self):
        """参数名必须正好是 VCode —— 名字错了服务端会当没传，一直要验证码。"""
        c = self._client({"ResponseID": 2, "Data": "data:image/png;base64,BBBB"})
        with self.assertRaises(Exception):
            c.login(vcode="8899")
        body = c.s.post.call_args.kwargs["data"]
        self.assertEqual(body["VCode"], "8899")
        self.assertEqual(body["userName"], "u")
        self.assertEqual(body["token"], "")

    def test_no_vcode_key_when_not_given(self):
        c = self._client({"ResponseID": 0, "Data": {"token": "T"}})
        c.login()
        self.assertNotIn("VCode", c.s.post.call_args.kwargs["data"])

    def test_same_session_is_reused(self):
        """验证码跟会话绑定 —— 两次请求必须走同一个 session（同一批 cookie）。"""
        c = self._client({"ResponseID": 2, "Data": "data:image/png;base64,C"})
        with self.assertRaises(Exception):
            c.login()
        c.s.post.return_value = _FakeResp({"ResponseID": 0, "Data": {"token": "OK"}})
        self.assertEqual(c.login(vcode="1234"), "OK")
        self.assertEqual(c.s.post.call_count, 2, "两次都要用同一个 session")

    def test_looks_like_captcha_message_mentions_retry(self):
        from src.erp import ErpCaptchaRequired
        c = self._client({"ResponseID": 2, "Data": "data:image/png;base64,D"})
        with self.assertRaises(ErpCaptchaRequired):
            c.login(vcode="wrong")
        # 带 vcode 再失败时，提示要说"刚才那个填错了"，人才知道是重填不是首次
        with self.assertRaises(ErpCaptchaRequired) as ctx:
            c.login(vcode="wrong2")
        self.assertIn("填错", str(ctx.exception))

    def test_register_flow_still_works(self):
        """原代码里这处用了 json.dumps 但没 import json —— 顺手修掉了，加个测试盯着。"""
        from src.erp import ErpError
        c = self._client({"ResponseID": 3, "Data": {"Status": "pending"}})
        with self.assertRaises(ErpError) as ctx:
            c.login()
        self.assertIn("注册/审核", str(ctx.exception))

    def test_wrong_password(self):
        from src.erp import ErpError
        c = self._client({"ResponseID": 1, "Message": "账号或密码不正确"})
        with self.assertRaises(ErpError) as ctx:
            c.login()
        self.assertIn("不正确", str(ctx.exception))


class TestPendingLogin(unittest.TestCase):
    """待验证码的登录态。**必须留住那个 client** —— 换个 client 就一定验不过。"""

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.web import PendingLogin
        self.cls = PendingLogin
        self.p = PendingLogin()

    def test_hold_and_alive(self):
        self.assertFalse(self.p.alive())
        self.p.hold(object(), "u", "pw", "C", "data:image/png;base64,X")
        self.assertTrue(self.p.alive())
        self.assertEqual(self.p.username, "u")
        self.assertEqual(self.p.company, "C")

    def test_expires(self):
        import time as _t
        self.p.hold(object(), "u", None, None, "img")
        self.p.created_at = _t.time() - self.cls.TTL - 1
        self.assertFalse(self.p.alive(), "过期了就不能再提交验证码")

    def test_reset_clears_client(self):
        self.p.hold(object(), "u", "pw", "C", "img")
        self.p.reset()
        self.assertFalse(self.p.alive())
        self.assertIsNone(self.p.client, "client 必须被丢掉，别留着连接")
        self.assertEqual(self.p.image, "")


class Test空区间不算失败(unittest.TestCase):
    """⚠⚠ 2026-09-21 实测钉的：**「这一段没数据」不是错误**。

    用户那天问「为啥显示失败但是数据刷新了」—— 根因就是它：
    `erp-dump` 拉"当月到今天"时，**最后一段是今天单独那一天**，
    而今天的销售还没进报表 ⇒ 服务端回 `ResponseID=2 暂无数据` ⇒ 一路抛上去
    ⇒ **整步算失败**（退出码 2），可前面几段的数据**早就落库了**。
    """

    def test_认得暂无数据(self):
        from src import erp
        e = erp.ErpError("云商接口报错 ResponseID=2：暂无数据（若是「登录超时」…）")
        self.assertTrue(erp.is_empty_result(e))

    def test_别的错不许当成空(self):
        """⚠ 判据只认 `ResponseID=2` + 「暂无数据」——鉴权/限流那些**必须抛**。"""
        from src import erp
        for msg in ("云商接口报错 ResponseID=1：未登录！！！",
                    "云商接口报错 ResponseID=9：登录超时",
                    "云商接口报错 ResponseID=-1：操作失败，请稍后重试",
                    "下载到的不是 xlsx"):
            with self.subTest(msg=msg):
                self.assertFalse(erp.is_empty_result(erp.ErpError(msg)))

    def test_空区间返回空列表而不是抛(self):
        from src import erp

        class Fake(erp.ErpClient):
            def __init__(self):
                self.s = None
                self.creds = {"token": "t"}
                self.timeout = 1
                self._relogin_tried = False

            def call(self, url, body, timeout=None):
                raise erp.ErpError("云商接口报错 ResponseID=2：暂无数据")

        import datetime
        self.assertEqual(Fake().sales_rows(datetime.date(2026, 9, 21),
                                           datetime.date(2026, 9, 21)), [])

class Test重登要把新token存下来(unittest.TestCase):
    """⚠⚠ 用户 2026-09-21 报的「数据更新时**一直**有 token 过期的提醒」的根因。

    `call()` 里 token 失效时会重登一次，而它原来调的是 `self.login()`（`save=False`
    是默认）⇒ 新 token **只用这一次、不写盘** ⇒ 下一趟又拿那个过期的旧 token
    ⇒ 每跑一次提醒一次。而且 `ErpClient().env_file` 常常是 `None`（调用方多数不传），
    写回**默认路径**也没用 —— 密码实际来自哪个文件（`effective_env_file()`）才是该写的那个。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home" / ".dsh" / "secrets"
        self.home.mkdir(parents=True)
        self.mine = self.root / ".secrets"
        self.mine.mkdir(parents=True)

    def _patch_env(self, files):
        """把回落链**钉死**成我们给的这几个文件（不碰真环境）。"""
        from src import erp
        paths = []
        for path, body in files:
            path.write_text(body, encoding="utf-8")
            paths.append(path)
        return mock.patch.object(erp, "env_chain", lambda extra=None: list(paths)), \
            mock.patch.dict(erp.os.environ, {"HOME": str(self.root / "home")})

    def test_写到密码实际来源那个文件(self):
        from src import erp
        p_home = self.home / "erp.env"
        p_mine = self.mine / "erp.env"
        p_mine.write_text("ERP_USERNAME=\nERP_PASSWORD=\nERP_TOKEN=\n", encoding="utf-8")
        p_home.write_text("ERP_USERNAME=u\nERP_PASSWORD=pw\nERP_TOKEN=old\n",
                          encoding="utf-8")
        c = erp.ErpClient()                       # ⚠ env_file 不传（调用方多数这样）
        with mock.patch.object(erp, "env_chain", lambda extra=None: [p_mine, p_home]):
            c._save_token("new-token")
        self.assertIn("new-token", p_home.read_text(encoding="utf-8"),
                      "新 token 没写回**密码实际来源**那个文件（下次照样读旧的）")
        self.assertNotIn("new-token", p_mine.read_text(encoding="utf-8"),
                         "写到了默认那个文件 —— 那不是凭据真正来自的地方")

    def test_来源是环境变量就不写盘(self):
        from src import erp
        p_home = self.home / "erp.env"
        p_home.write_text("ERP_USERNAME=u\nERP_PASSWORD=pw\n", encoding="utf-8")
        c = erp.ErpClient()
        with mock.patch.object(erp, "env_chain", lambda extra=None: [p_home]), \
                mock.patch.dict(erp.os.environ, {"ERP_TOKEN": "from-env"}):
            c._save_token("new-token")
        self.assertNotIn("new-token", p_home.read_text(encoding="utf-8"),
                         "环境变量优先级最高，写了也不会被读到")
