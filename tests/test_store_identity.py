# -*- coding: utf-8 -*-
"""从玲珑会话认店 `src/store_identity.py`（实施计划 Task 6）。

三条要钉的事：
* **三态**：`_probe_store_code` 的 `ok` / `empty` / `authfail` 必须分得开 ——
  假会话（authfail）拦在 verify，0 行的新店（empty）放行但说清，折成 bool 就红。
* **边界**：名单没命中照样放行（店名留空）、已有店码不重复探测、查不出不写半截配置。
* **接线**：web 的两处 + cli 的两处（含 `cmd_auth` 嵌套 `_verify`）——
  这两个文件正被并行任务改，钉源码防"改没了"（AGENTS 坑 17）。

⚠ 切版的测试 cleanup **恢复原值**再 `edition.reload()` —— 绝不
   `os.environ.pop("CBG_EDITION")`：本分支 `EDITION` 文件=lifehall，pop 掉 env
   之后 edition 读文件，后面所有测试都按 lifehall 跑（实测连锁打红一串）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition                                            # noqa: E402


def _restore_edition_env(old):
    """把 `CBG_EDITION` 恢复成跑这条测试之前的值，再清 edition 的缓存。"""
    os.environ["CBG_EDITION"] = old if old else "full"        # ⚠ 恢复，绝不 pop
    edition.reload()


class _LifehallCase(unittest.TestCase):
    """共用底盘：临时 root + 真名单 + 空配置 + lifehall env（cleanup 恢复原值）。"""

    def setUp(self):
        self._old_edition = os.environ.get("CBG_EDITION")
        os.environ["CBG_EDITION"] = "lifehall"
        edition.reload()
        self.addCleanup(_restore_edition_env, self._old_edition)

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        # 名单要拷真的 —— 认店 / 店名的判据全靠它
        shutil.copy(ROOT / "config" / "stores.yaml",
                    self.root / "config" / "stores.yaml")
        self.cfg_path = self.root / "config" / "store-X.yaml"
        self.cfg_path.write_text("erp_store_name: \nstore_code: \nmarker: \n",
                                 encoding="utf-8")

    def _sess_file(self, name="default"):
        p = self.root / ".secrets" / ("cbg-%s.json" % name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"cookies": "JSESSIONID=1", "csrf": "abcd1234ef"}',
                     encoding="utf-8")
        return p

    def _raw(self):
        from src import config_io
        return config_io.load_raw(self.cfg_path)


class Test从会话认店(_LifehallCase):
    def test_名单里有这家店_写店名并挪会话文件(self):
        from src import config_io, store_identity
        old = self._sess_file("default")
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN328987", "ok")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertTrue(res["ok"])
        v = config_io.pick(config_io.load_raw(self.cfg_path))
        self.assertEqual(v["store_code"], "SCN328987")
        self.assertEqual(v["erp_store_name"], "青岛CBD万达店")   # 名单里 SCN328987
        # 会话文件跟着挪：cbg-default.json → cbg-SCN328987.json
        self.assertFalse(old.exists())
        self.assertTrue((self.root / ".secrets" / "cbg-SCN328987.json").exists())

    def test_名单里没有_只写店码不留空名(self):
        from src import store_identity
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN9999999", "ok")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertTrue(res["ok"])
        v = self._raw()
        self.assertEqual(v["store_code"], "SCN9999999")
        self.assertEqual(v["erp_store_name"], "")   # 认不出店名：留空，照样放行
        self.assertEqual(v["marker"], "")           # 标识也别残留上一家店的

    def test_查不出店码_不写配置不抛(self):
        from src import store_identity
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("", "authfail")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertFalse(res["ok"])
        self.assertIn("why", res)
        self.assertNotIn("store_code", res)
        self.assertFalse(self._raw().get("store_code"), "没认出来不许写半截配置")

    def test_已经有店码_不重复探测(self):
        from src import store_identity
        self.cfg_path.write_text(
            "erp_store_name: x\nstore_code: SCN328987\nmarker: C\n",
            encoding="utf-8")
        with mock.patch.object(store_identity, "_probe_store_code") as pr:
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertTrue(res["ok"])
        pr.assert_not_called()

    def test_full版不认店(self):
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        from src import store_identity
        res = store_identity.identify(self.root, self.cfg_path)
        self.assertEqual(res, {"ok": True, "skipped": True})

    def test_配置读不出来也不抛(self):
        # identify 是"锦上添花"：炸在保存点上会把"会话其实存好了"说成失败
        from src import config_io, store_identity
        with mock.patch.object(config_io, "load_raw",
                               side_effect=ValueError("yaml 写坏了")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertFalse(res["ok"])
        self.assertIn("why", res)
        self.assertIn("yaml 写坏了", res["why"])

    def test_接口异常identify兜住成error(self):
        from src import cbg, store_identity
        with mock.patch.object(store_identity, "_probe_store_code",
                               side_effect=cbg.CbgError("接口 500")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["probe"], "error", "网络异常不是假会话，状态要分家")
        self.assertIn("接口 500", res["why"])


class Test探测三态(unittest.TestCase):
    """Task 6 硬验收：`_probe_store_code` 的三态 —— 折成 bool 就地红。"""

    def _probe(self, rows=None, exc=None, sess="会话"):
        from src import cbg, store_identity

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            def list_orders(self, *a, **k):
                if exc is not None:
                    raise exc
                return rows if rows is not None else []

        with mock.patch.object(cbg, "CbgClient", _FakeClient):
            return store_identity._probe_store_code(sess)

    def test_ok_拿到店码(self):
        self.assertEqual(self._probe(rows=[{"storeCode": " SCN328987 "},
                                           {"storeCode": ""}]),
                         ("SCN328987", "ok"))

    def test_empty_零行(self):
        # 新店 30 天没卖货：会话是真的，只是没有店码 —— 不能跟假会话混
        self.assertEqual(self._probe(rows=[]), ("", "empty"))

    def test_empty_行里没店码(self):
        self.assertEqual(self._probe(rows=[{"orderNo": "X1"}]), ("", "empty"))

    def test_authfail_假会话(self):
        from src import cbg
        self.assertEqual(
            self._probe(exc=cbg.CbgAuthError("会话已失效（HTTP 401）")),
            ("", "authfail"))

    def test_没会话也按authfail(self):
        from src import store_identity
        self.assertEqual(store_identity._probe_store_code(None), ("", "authfail"))

    def test_接口异常不折进三态(self):
        # "没验成"≠"验出来是假的" —— CbgError 原样抛，由 identify 兜成 error
        from src import cbg
        with self.assertRaises(cbg.CbgError):
            self._probe(exc=cbg.CbgError("请求 /order 失败：超时"))


class Test三态拦放(_LifehallCase):
    """verify 的三态走向：假会话必须拦、0 行放行说清、拿到店码正经 ping。"""

    def test_假会话拦在verify(self):
        from src import store_identity
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("", "authfail")):
            ok, why = store_identity.verify_with_identity(
                object(), self.root, self.cfg_path)
        self.assertFalse(ok, "假会话必须死在 verify")
        self.assertIn("会话没验过", why)
        self.assertFalse(self._raw().get("store_code"))

    def test_零行的新店放行但说清(self):
        from src import store_identity
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("", "empty")):
            ok, why = store_identity.verify_with_identity(
                object(), self.root, self.cfg_path)
        self.assertTrue(ok, "0 行是新店不是坏会话 —— 放行")
        self.assertIn("没有查到本店订单", why)
        self.assertIn("不影响使用", why)

    def test_拿到店码就正经ping_过了也把配置写了(self):
        from src import cbg, store_identity
        self._sess_file()

        class _PingOk:
            def __init__(self, *a, **k):
                pass

            def ping(self):
                return True, "SCN328987 青岛CBD万达店"

        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN328987", "ok")), \
                mock.patch.object(cbg, "CbgClient", _PingOk):
            ok, why = store_identity.verify_with_identity(
                object(), self.root, self.cfg_path)
        self.assertTrue(ok)
        self.assertIn("青岛CBD万达店", why)
        v = self._raw()
        self.assertEqual(v["store_code"], "SCN328987")
        self.assertEqual(v["erp_store_name"], "青岛CBD万达店")
        self.assertTrue((self.root / ".secrets" / "cbg-SCN328987.json").exists())

    def test_店码拿到但ping没过照样拦(self):
        from src import cbg, store_identity

        class _PingBad:
            def __init__(self, *a, **k):
                pass

            def ping(self):
                return False, "会话失效：会话已失效（HTTP 401）"

        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN328987", "ok")), \
                mock.patch.object(cbg, "CbgClient", _PingBad):
            ok, why = store_identity.verify_with_identity(
                object(), self.root, self.cfg_path)
        self.assertFalse(ok)
        self.assertIn("会话失效", why)


class Test会话文件挪名(_LifehallCase):
    def test_已有店码时补挪会话但不重复探测(self):
        # 抓取流程用的是**开跑时读的旧 cfg**：verify 里写完店码后，会话却存成了
        # cbg-default.json —— 保存点这次 identify 必须补挪，同时不许再探测。
        from src import store_identity
        old = self._sess_file("default")
        self.cfg_path.write_text(
            "erp_store_name: 青岛CBD万达店\nstore_code: SCN328987\nmarker: C\n",
            encoding="utf-8")
        with mock.patch.object(store_identity, "_probe_store_code") as pr:
            res = store_identity.identify(self.root, self.cfg_path)
        pr.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertFalse(old.exists())
        self.assertTrue((self.root / ".secrets" / "cbg-SCN328987.json").exists())

    def test_目标已有会话不覆盖(self):
        # 目标那份是这家店名下已有的会话，default 来历不明 —— 盖了就把真会话弄丢
        from src import store_identity
        old = self._sess_file("default")
        tgt = self.root / ".secrets" / "cbg-SCN328987.json"
        tgt.write_text('{"cookies": "OLD=1", "csrf": "oldcsrf1234"}',
                       encoding="utf-8")
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN328987", "ok")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertTrue(res["ok"])
        self.assertTrue(old.exists(), "没挪动就不该动 default")
        self.assertIn("OLD=1", tgt.read_text(encoding="utf-8"))

    def test_挪之前先建目录(self):
        # 目标目录不在时（换过 session.file 配置）先 mkdir 再 os.replace，
        # 少这一步就是 FileNotFoundError
        from src import store_identity
        old = self._sess_file("default")
        self.cfg_path.write_text(
            "erp_store_name: \nstore_code: \nmarker: \n"
            "session:\n  file: .secrets/nested/cbg-custom.json\n",
            encoding="utf-8")
        with mock.patch.object(store_identity, "_probe_store_code",
                               return_value=("SCN328987", "ok")):
            res = store_identity.identify(self.root, self.cfg_path)
        self.assertTrue(res["ok"])
        new = self.root / ".secrets" / "nested" / "cbg-custom.json"
        self.assertTrue(new.exists(), "目标目录要先建出来")
        self.assertFalse(old.exists())


class TestCLI认店(_LifehallCase):
    """cli.cmd_auth：lifehall 的 verify 走认店判据，full 逐字还是老 ping。"""

    def _run_auth(self):
        """跑一次 `cmd_auth --auto`（抓浏览器那段打桩），交回收到的 kwargs。"""
        from src import cli
        got = {}

        def fake_capture(profile, **kw):
            got.update(kw)
            raise cli.browser.BrowserError("到此为止（不真抓浏览器）")

        args = mock.Mock(auto=True, refresh=False, from_curl=None,
                         timeout=5, config=str(self.cfg_path))
        cfg = {"_path": str(self.cfg_path), "store_code": ""}
        with mock.patch.object(cli, "load_config",
                               lambda p, root=None: cfg), \
                mock.patch.object(cli, "browser_profile",
                                  lambda c: self.root / "prof"), \
                mock.patch.object(cli.browser, "load_login_credentials",
                                  lambda c, r: ("u", "p")), \
                mock.patch.object(cli.browser, "capture_session", fake_capture):
            rc = cli.cmd_auth(args)
        return rc, got, cfg

    def test_lifehall下_verify走认店判据而不是ping(self):
        from src import cli, store_identity

        class _NoPing:
            def __init__(self, *a, **k):
                raise AssertionError("lifehall 下不该造 ping 客户端（store_code 还空着）")

        with mock.patch.object(cli, "CbgClient", _NoPing), \
                mock.patch.object(store_identity, "verify_with_identity",
                                  return_value=(True, "认店判据")) as v:
            rc, got, _cfg = self._run_auth()
            self.assertIn("verify", got, "capture_session 必须收到 verify 回调")
            ok, why = got["verify"](object())
        self.assertEqual(rc, cli.EXIT_AUTH)        # 假抓取在 verify 之前就收场
        self.assertTrue(ok)
        self.assertEqual(why, "认店判据")
        v.assert_called_once()
        self.assertEqual(v.call_args[0][2], str(self.cfg_path))

    def test_full版verify还是老ping_不碰认店(self):
        os.environ["CBG_EDITION"] = "full"
        edition.reload()
        from src import cli, store_identity

        class _PingOk:
            def __init__(self, *a, **k):
                pass

            def ping(self):
                return True, "老路径"

        with mock.patch.object(cli, "CbgClient", _PingOk), \
                mock.patch.object(store_identity,
                                  "verify_with_identity") as v:
            rc, got, _cfg = self._run_auth()
            ok, why = got["verify"](object())
        self.assertEqual(rc, cli.EXIT_AUTH)
        self.assertTrue(ok)
        self.assertEqual(why, "老路径")
        v.assert_not_called()


class Test三个保存点接线(unittest.TestCase):
    """钉源码 —— web.py / cli.py 正被并行任务改（AGENTS 坑 17），改没了当场红。"""

    @staticmethod
    def _func(src, name):
        """取一个顶格 def 的整段函数体（到下一个顶格 def 为止）。"""
        i = src.index("\ndef %s(" % name)
        j = src.index("\ndef ", i + 1)
        return src[i:j]

    def setUp(self):
        self.web = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
        self.cli = (ROOT / "src" / "cli.py").read_text(encoding="utf-8")

    def test_抓取的verify走认店(self):
        self.assertIn("verify_with_identity",
                      self._func(self.web, "_capture_worker"))

    def test_抓取保存后调identify(self):
        self.assertIn("store_identity.identify",
                      self._func(self.web, "_capture_worker"))

    def test_curl导入把认店结果并进result(self):
        self.assertIn('result["identify"]', self.web)
        self.assertIn("store_identity.identify", self.web)

    def test_cli抓会话的verify走同一份判据(self):
        self.assertIn("verify_with_identity", self._func(self.cli, "cmd_auth"))

    def test_cli保存后调identify打一行(self):
        fn = self._func(self.cli, "cmd_auth")
        self.assertIn("store_identity.identify", fn)
        self.assertIn("认店：", fn)


if __name__ == "__main__":
    unittest.main()
