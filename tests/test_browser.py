"""自动抓 cookie 的测试。

**不真的启动浏览器** —— 那是几十秒的事，而且 CI 上未必有浏览器。
把 launch / CDP 读取换成假的，专测真正的逻辑：
"怎么筛 cookie" 和 "什么时候才允许把凭据交出去"。
"""

import json
import os
import stat
import tempfile
import types
import unittest
from contextlib import ExitStack
from itertools import count
from pathlib import Path
from unittest import mock

from src import browser
from src.session import CbgAuthError


def _ck(name, value, domain):
    return {"name": name, "value": value, "domain": domain, "path": "/"}


class TestPickCookies(unittest.TestCase):
    def test_drops_analytics_and_foreign_domains(self):
        raw = [
            _ck("JSESSIONID", "S1", "cbg.huawei.com"),
            _ck("hwssot3", "H1", ".huawei.com"),
            _ck("_ga", "GA", ".huawei.com"),
            _ck("Hm_lvt_48e5", "1,2", ".huawei.com"),
            _ck("someOtherSite", "X", "example.com"),
        ]
        cookies, picked = browser.pick_cookies(raw)
        self.assertIn("JSESSIONID=S1", cookies)
        self.assertIn("hwssot3=H1", cookies)
        self.assertNotIn("_ga", cookies)
        self.assertNotIn("Hm_lvt", cookies)
        self.assertNotIn("someOtherSite", cookies)

    def test_prefers_cbg_domain_for_same_name(self):
        """同名 cookie 有多个域时，必须取最贴合 cbg.huawei.com 的那个。"""
        raw = [
            _ck("JSESSIONID", "FROM-WILDCARD", ".huawei.com"),
            _ck("JSESSIONID", "FROM-CBG", "cbg.huawei.com"),
            _ck("JSESSIONID", "FROM-LOGIN", "login.huawei.com"),
        ]
        cookies, _ = browser.pick_cookies(raw)
        self.assertIn("JSESSIONID=FROM-CBG", cookies)
        self.assertNotIn("FROM-WILDCARD", cookies)

    def test_login_huawei_cn_also_accepted(self):
        cookies, _ = browser.pick_cookies([_ck("hwssot3", "V", ".huawei.cn")])
        self.assertIn("hwssot3=V", cookies)

    def test_empty(self):
        self.assertEqual(browser.pick_cookies([]), ("", {}))
        self.assertEqual(browser.pick_cookies(None), ("", {}))

    def test_keeps_hwstore_session(self):
        cookies, _ = browser.pick_cookies([
            _ck("HWSTORE-SESSION", "ABC", "cbg.huawei.com"),
            _ck("WPSESSIONID", "W", "cbg.huawei.com")])
        self.assertIn("HWSTORE-SESSION=ABC", cookies)
        self.assertIn("WPSESSIONID=W", cookies)


class TestProfilePath(unittest.TestCase):
    def test_default(self):
        self.assertEqual(browser.profile_path({}, "/root"),
                         Path("/root") / browser.PROFILE_DIRNAME)

    def test_config_override_relative(self):
        cfg = {"session": {"browser_profile": "custom/prof"}}
        self.assertEqual(browser.profile_path(cfg, "/root"), Path("/root/custom/prof"))

    def test_config_override_absolute(self):
        cfg = {"session": {"browser_profile": "/abs/prof"}}
        self.assertEqual(browser.profile_path(cfg, "/root"), Path("/abs/prof"))


class _FakeProc:
    def __init__(self, alive=True):
        self.alive = alive
        self.returncode = None if alive else 1

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


def _patch_launch(alive=True):
    return mock.patch.object(browser, "launch",
                             return_value=(_FakeProc(alive), 12345))


class TestProfileLock(unittest.TestCase):
    """profile 被占着的时候，新起的浏览器会**立刻退出（退出码 21）**。

    看起来像"启动失败"，其实只是"别人已经在用了"。这个坑会**连锁**：
    上次抓取失败时浏览器没关掉（比如拿不到进程句柄），它会一直占着 profile，
    之后每次抓取都撞锁、都报 21。

    实测就是这样：一个残留的 Edge 让后面所有尝试全部失败，而报错只说
    "启动后立刻退出"，完全指不到方向。
    """

    def test_detects_lock_file(self):
        d = Path(tempfile.mkdtemp())
        self.assertFalse(browser.profile_locked(d))
        (d / "SingletonLock").write_text("host-1234", encoding="utf-8")
        self.assertTrue(browser.profile_locked(d))

    def test_detects_lock_symlink(self):
        """macOS/Linux 上 SingletonLock 是**软链**，不是普通文件。"""
        d = Path(tempfile.mkdtemp())
        (d / "SingletonLock").symlink_to("MacBook-Air.local-41703")
        self.assertTrue(browser.profile_locked(d))

    def test_missing_dir_is_not_locked(self):
        self.assertFalse(browser.profile_locked(Path("/nonexistent/profile/xyz")))

    def test_hint_names_the_profile_when_it_is_locked(self):
        """锁着的时候，提示里**必须先说 profile 被占**，而不是泛泛的"受限制环境"。"""
        d = Path(tempfile.mkdtemp())
        (d / "SingletonLock").write_text("x", encoding="utf-8")
        hint = browser._launch_failure_hint("Chrome", d)
        self.assertIn("profile", hint)
        self.assertIn("关掉", hint, "要给出能照着做的动作")
        self.assertLess(hint.index("profile"), hint.index("受限环境"),
                        "profile 被占是最可能的原因，要排前面")

    def test_hint_still_works_without_a_profile_dir(self):
        self.assertIn("profile 目录有问题", browser._launch_failure_hint("Chrome"))


class TestDetachedBrowser(unittest.TestCase):
    """Chrome 从**管理员进程**启动时会把自己降权重启（AutoDeElevate，Chrome 138+）。

    现象：我们 Popen 的那条进程立刻退出（**退出码 21**），真正的浏览器是另一个进程。
    这时进程句柄就废了 —— 唯一还靠得住的是**调试端口**。

    这个 bug 在门店机器上真的踩到了：日志里"浏览器已启动"打完就报
    "Chrome 启动后立刻退出（退出码 21）"，但窗口其实是开着的。
    """

    def test_poll_uses_the_port_not_the_dead_handle(self):
        # ⚠ poll() 必须在 patch 生效期间调用 —— 它每次都是现探端口
        with mock.patch.object(browser, "_port_alive", lambda p: True):
            self.assertIsNone(browser.DetachedBrowser(9999).poll(),
                              "端口还活着就必须算活着")

    def test_poll_reports_dead_when_the_port_is_gone(self):
        with mock.patch.object(browser, "_port_alive", lambda p: False), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            self.assertIsNotNone(browser.DetachedBrowser(9999).poll())

    def test_a_transient_port_hiccup_is_not_a_death(self):
        """⚠ **这是门店那个 bug 的核心。**

        实测：服务以管理员身份跑时，Edge 会在自动登录提交后自我降权重启，
        那个窗口里调试端口**短暂失灵**。旧实现探一次探不到就判"浏览器关掉了"，
        于是整个抓取在中途放弃 —— 日志里就是那句莫名其妙的"退出码 None"。

        端口几秒后会在**同一个端口**上回来，所以必须连续探不到才算死。
        """
        seq = [False, True]
        calls = {"n": 0}

        def flaky(port):
            v = seq[min(calls["n"], len(seq) - 1)]
            calls["n"] += 1
            return v

        with mock.patch.object(browser, "_port_alive", flaky), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            self.assertIsNone(browser.DetachedBrowser(9999).poll(),
                              "端口恢复后必须算活着，不能判死")

    def test_a_real_death_still_reports_dead(self):
        """容错不能把"真死了"也吞掉 —— 连续探不到就得认。"""
        seen = {"n": 0}

        def never(port):
            seen["n"] += 1
            return False

        with mock.patch.object(browser, "_port_alive", never), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            self.assertIsNotNone(browser.DetachedBrowser(9999).poll())
        self.assertGreater(seen["n"], 1, "判死之前必须多探几次")

    def test_kill_asks_the_browser_to_close_itself(self):
        """没有句柄可 kill，只能走 CDP 的 Browser.close。"""
        seen = {}
        with mock.patch.object(browser, "_close_via_cdp", lambda p: seen.setdefault("port", p)):
            browser.DetachedBrowser(4321).kill()
        self.assertEqual(seen.get("port"), 4321)

    def test_shutdown_works_on_a_detached_browser(self):
        """_shutdown 不能只认 Popen —— terminate/wait/kill 三个方法它都要有。"""
        with mock.patch.object(browser, "_port_alive", lambda p: False), \
                mock.patch.object(browser, "_close_via_cdp", lambda p: None):
            browser._shutdown(browser.DetachedBrowser(4321))     # 不该抛

    def test_wait_returns_when_the_port_dies(self):
        with mock.patch.object(browser, "_port_alive", lambda p: False), \
                mock.patch.object(browser, "_close_via_cdp", lambda p: None):
            self.assertEqual(browser.DetachedBrowser(1).wait(timeout=2), 0)


class TestLaunchWaitsThroughDeElevation(unittest.TestCase):
    """`launch()` 在降权重启期间**不能因为"进程死了"就提前放弃**。

    原来的循环一看到进程退出就 break，可 Chrome 那条新进程还没起来、
    端口还没应答 —— 于是必然误报失败。
    """

    def _launch(self, port_sequence, proc_alive_after=False, settle=0):
        """port_sequence: 每次探测端口回什么（True/False 的列表，用完取最后一个）。"""
        calls = {"n": 0}

        def fake_http(port, path, timeout=1):
            i = min(calls["n"], len(port_sequence) - 1)
            calls["n"] += 1
            if not port_sequence[i]:
                raise browser.CdpError("还没起来")
            return {"Browser": "Chrome/138"}

        # 默认模拟"原进程已经退了"（降权重启）；proc_alive_after=True 是普通情况
        proc = _FakeProc(alive=proc_alive_after)

        with mock.patch.object(browser, "find_browser",
                               lambda *a, **k: (r"C:\chrome.exe", "Chrome")), \
                mock.patch.object(browser, "http_json", fake_http), \
                mock.patch.object(browser.subprocess, "Popen", lambda *a, **k: proc), \
                mock.patch.object(browser.time, "sleep", lambda s: None), \
                mock.patch.object(browser, "SETTLE_SECONDS", settle):
            return browser.launch(Path("/tmp/prof-x"), settle=settle)

    def test_port_up_after_the_launcher_died_is_success(self):
        """端口起来了 → 成功，而且是 DetachedBrowser。"""
        proc, port = self._launch([False, False, True])
        self.assertIsInstance(proc, browser.DetachedBrowser)
        self.assertEqual(port, proc.port)

    def test_live_process_still_returns_the_real_handle(self):
        """没降权的普通情况不受影响 —— 还是原来的 Popen。"""
        proc, port = self._launch([False, True], proc_alive_after=True)
        self.assertIsInstance(proc, _FakeProc)
        self.assertFalse(isinstance(proc, browser.DetachedBrowser))

    def test_gives_up_when_the_port_never_comes_up(self):
        """真起不来时要报错，而且提示里要提到管理员那条（门店最常见的）。"""
        with mock.patch.object(browser, "_am_i_admin", lambda: True):
            with self.assertRaises(browser.BrowserError) as cm:
                self._launch([False])
        msg = str(cm.exception)
        self.assertIn("管理员", msg, "管理员身份是最可能的原因，必须写出来")
        self.assertIn("browser-profile", msg, "要给出能照着做的下一步")

    def test_missing_browser_message_does_not_claim_edge_is_builtin(self):
        """找不到浏览器时的提示**不能**说"Edge 是系统自带的"。

        ⚠ Win10 上 Edge 确实是系统自带的，**Win7 上不是** —— 出厂只有 IE11，
        Chromium Edge 是 2020 年微软通过 Windows Update 推过去的，
        那之后没更新过的机器上就没有。Win7 门店的店员照着"开始菜单里找 Edge"
        会找不到，然后卡在这儿。
        而且 **IE11 必须点名说不行**：它跑不了控制台前端（`fetch` / `async` 一个
        都不支持），也没有 CDP，两个功能都用不了。
        """
        with mock.patch.object(browser, "find_browser", lambda *a, **k: None):
            with self.assertRaises(browser.BrowserError) as cm:
                browser.launch(Path("/tmp/prof-none"))
        msg = str(cm.exception)
        self.assertNotIn("系统自带", msg, "Win7 上 Edge 不是自带的，这句会误导人")
        self.assertIn("IE11", msg, "要知道 IE11 不行")
        self.assertIn("Windows 7", msg, "要点明是 Win7 的坑")
        self.assertIn("browser.prefer", msg, "要给出能照着做的下一步")


class TestExitCode21MeansProfileInUse(unittest.TestCase):
    """⚠ **退出码 21 是"profile 被占着"的铁证**，比翻锁文件可靠。

    21 就是 Chromium 的 `RESULT_CODE_PROFILE_IN_USE`。
    实测踩过：报错写着"退出码 21"，而 `profile_locked()` 翻不到锁文件
    （那台机器/那个版本的 Edge 上文件名不一样）—— 于是"profile 被占着"
    这条提示**根本没出现**，用户看到的是一堆不相干的建议。
    **退出码是浏览器直接告诉我们的，优先信它。**
    """

    def test_returncode_21_adds_the_profile_hint(self):
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/prof", returncode=21)
        self.assertIn("profile 被占着", msg)
        self.assertIn("PROFILE_IN_USE", msg, "要说清 21 是什么")
        self.assertIn("msedge.exe", msg, "要给出能照着做的下一步（结束残留进程）")

    def test_says_the_trap_is_a_chain(self):
        """这次没关掉的浏览器会让**之后每次**抓取都撞 —— 这个连锁必须说破。"""
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/p", returncode=21)
        self.assertIn("连锁", msg)

    def test_gives_a_pasteable_kill_command(self):
        """⚠ 残留的那个浏览器**多半是看不见的**（静默续期走无头模式，没有窗口）。

        所以「关掉所有浏览器窗口」这句对用户等于没说 —— 他关了、还是撞 21，
        完全不知道还能干什么。必须给一条**能直接粘的命令**。
        """
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/p", returncode=21)
        self.assertIn("taskkill /f /im msedge.exe", msg, "要给能直接粘的命令")
        self.assertIn("无头", msg, "要点破「看不见」这件事")
        self.assertIn("chrome.exe", msg, "用 Chrome 的机器也要能用")

    def test_elevation_line_does_not_hardcode_exit_code_zero(self):
        """⚠ 别把"你看到的是退出码 0"写死 —— 那只是**典型**表现。

        这次可能先撞上 profile 被占（21），写死了就跟下面第 2 条自相矛盾，
        用户会以为程序在胡说。（实测真撞上过。）
        """
        with mock.patch.object(browser, "_am_i_admin", lambda: True), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/p", returncode=21)
        self.assertIn("典型表现", msg)
        self.assertNotIn("所以你看到的是「启动后立刻退出（退出码 0）」", msg)

    def test_other_returncodes_do_not_claim_locked(self):
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/p", returncode=0)
        self.assertNotIn("profile 被占着", msg)

    def test_lock_file_still_works_without_a_returncode(self):
        """没有退出码（跑到一半浏览器没了那条路）时，还得靠锁文件判断。"""
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: True):
            msg = browser._launch_failure_hint("Edge", "/tmp/p")
        self.assertIn("profile 被占着", msg)

    def test_says_edge_not_just_chrome(self):
        """⚠ 门店用的是 **Edge**，只写 Chrome 会让人以为"跟我无关"。"""
        with mock.patch.object(browser, "_am_i_admin", lambda: True), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/p")
        self.assertIn("Edge / Chrome", msg)


class TestRetryKeepsTheFirstExitCode(unittest.TestCase):
    """`launch()` 失败后会**降级重试一次**，而抛出去的是**重试**那个退出码。

    ⚠ 两次的退出码含义完全不同（0 = 交棒/权限；21 = profile 被占），
    第一次那个被吞掉的话，用户只看到一个 21，而真正的原因是第一次的 0 ——
    实测就是这么被误导的。
    """

    def test_first_code_is_reported(self):
        codes = [0, 21]          # 第一次 0，重试 21
        procs = []

        class _P:
            def __init__(self, code):
                self.returncode = code
            def poll(self):
                return self.returncode
            def kill(self):
                pass

        def fake_popen(*a, **k):
            p = _P(codes[min(len(procs), len(codes) - 1)])
            procs.append(p)
            return p

        with mock.patch.object(browser, "find_browser",
                               lambda *a, **k: (r"C:\edge.exe", "Edge")), \
                mock.patch.object(browser, "http_json",
                                  mock.Mock(side_effect=browser.CdpError("没起来"))), \
                mock.patch.object(browser.subprocess, "Popen", fake_popen), \
                mock.patch.object(browser.time, "sleep", lambda s: None), \
                mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            with self.assertRaises(browser.BrowserError) as cm:
                browser.launch(Path("/tmp/prof-retry"))
        msg = str(cm.exception)
        self.assertIn("第一次", msg, "要把第一次的退出码补出来")
        self.assertIn("0", msg)


class TestVerifyResult(unittest.TestCase):
    """⚠ 自检没过时，**必须把原因带出来**。

    以前 `verify` 只返回 bool，而 `CbgClient.ping()` 的第二个返回值里写着真正的
    病因（"会话/权限问题：没有门店或数据范围 XXX 的权限"、"接口异常：…"）——
    被 `.ping()[0]` 直接扔掉了。用户最后只看到一句"自检一直没过"，
    只能反复说"就是抓不到"，谁也定位不了。实测就卡在这儿。
    """

    def test_pair_carries_the_reason(self):
        ok, why = browser._verify_result(lambda s: (False, "会话/权限问题：没有权限"), None)
        self.assertFalse(ok)
        self.assertIn("没有权限", why)

    def test_plain_bool_still_works(self):
        """老写法（返回 bool）不能炸 —— 兼容它，只是原因未知。"""
        self.assertEqual(browser._verify_result(lambda s: True, None), (True, ""))
        self.assertEqual(browser._verify_result(lambda s: False, None), (False, ""))

    def test_no_verify_means_pass(self):
        self.assertEqual(browser._verify_result(None, None), (True, ""))

    def test_verify_raising_is_its_own_reason(self):
        """校验函数自己炸了 ≠ 校验没过 —— 混成一句会让人去查账号，其实是代码问题。"""
        def boom(s):
            raise RuntimeError("端口没了")
        ok, why = browser._verify_result(boom, None)
        self.assertFalse(ok)
        self.assertIn("RuntimeError", why)
        self.assertIn("端口没了", why)

    def test_reason_reaches_the_timeout_message(self):
        """最终报错里要**真的有那句话**，不然前面都白改。"""
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S; hwssot3=1", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="CSRF1"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser, "page_targets", lambda p: []), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(
                    Path("/x"), headless=True, timeout=1.2,
                    verify=lambda s: (False, "会话/权限问题：没有门店或数据范围 SCN9 的权限"))
        msg = str(ctx.exception)
        self.assertIn("没有门店或数据范围 SCN9 的权限", msg,
                      "自检给的原因必须出现在最终报错里")
        self.assertIn("自检/接口说的是", msg)

    def test_csrf_source_is_reported_when_csrf_is_missing(self):
        """cookie 有、csrf 取不到 —— 这跟"没登录"是两回事，要说准。"""
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S; hwssot3=1", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value=None), \
                mock.patch.object(browser, "csrf_from_api", return_value=None), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser, "page_targets", lambda p: []), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=True, timeout=1.2,
                                        verify=lambda s: True)
        self.assertIn("csrf 取不到", str(ctx.exception))


class TestWhereIsTheBrowser(unittest.TestCase):
    """超时时要能说出"窗口现在停在哪一页"。

    ⚠ 这是排查"抓不到会话"时最想知道的一件事，而原来恰恰没有 ——
    用户只能说"就是抓不到"，我们只能猜。实测就卡在这儿。
    """

    def test_lists_the_open_pages(self):
        with mock.patch.object(browser, "page_targets", lambda p: [
                {"url": "https://cbg.huawei.com/#/login"},
                {"url": "https://uniportal.huawei.com/uniportal1/?x=1"}]):
            got = browser._where_is_the_browser(1234)
        self.assertIn("cbg.huawei.com", got)
        self.assertIn("uniportal.huawei.com", got,
                      "SSO 页也要列出来 —— 那正是「卡在登录」的特征")

    def test_skips_devtools_pages(self):
        with mock.patch.object(browser, "page_targets", lambda p: [
                {"url": "devtools://devtools/bundled/x.html"},
                {"url": "https://cbg.huawei.com/ok"}]):
            got = browser._where_is_the_browser(1)
        self.assertNotIn("devtools://", got)

    def test_no_pages_is_empty_not_an_error(self):
        """⚠ 诊断信息**永远不该**让原本的报错变成另一个报错。"""
        with mock.patch.object(browser, "page_targets", lambda p: []):
            self.assertEqual(browser._where_is_the_browser(1), "")
        with mock.patch.object(browser, "page_targets",
                               mock.Mock(side_effect=RuntimeError("端口没了"))):
            self.assertEqual(browser._where_is_the_browser(1), "")

    def test_caps_the_list(self):
        with mock.patch.object(browser, "page_targets",
                               lambda p: [{"url": f"https://cbg.huawei.com/{i}"}
                                          for i in range(20)]):
            got = browser._where_is_the_browser(1)
        listed = [ln for ln in got.splitlines() if ln.startswith("  · ")]
        self.assertEqual(len(listed), 5, "最多列 5 条，别把报错刷成一屏")


class TestLaunchFailureHint(unittest.TestCase):
    """启动失败的提示必须**给证据**，不能写成"可能的原因"。

    ⚠ 踩过：原来一律写"可能的原因（按可能性排）"，而第 1 条是"服务现在是管理员
    身份"。门店看到会想"我没用管理员啊"，然后去试第 2、3 条，白折腾一轮 ——
    而那条其实**不是猜的**：它只在 `IsUserAnAdmin()` 真返回真时才出现。
    """

    def test_elevated_says_confirmed_and_leads_with_it(self):
        with mock.patch.object(browser, "_am_i_admin", lambda: True), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/prof")
        self.assertIn("已确认", msg, "管理员那条是查出来的，不是猜的")
        self.assertNotIn("可能的原因", msg, "别给用户「这是猜测」的印象")
        self.assertLess(msg.index("管理员"), msg.index("profile 目录"),
                        "确认的原因必须排在最前面")
        self.assertIn("普通权限", msg, "要给出能照着做的下一步")

    def test_not_elevated_does_not_mention_admin(self):
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: False):
            msg = browser._launch_failure_hint("Edge", "/tmp/prof")
        self.assertNotIn("管理员", msg, "不是就别提，免得指错方向")

    def test_locked_profile_is_reported_when_present(self):
        with mock.patch.object(browser, "_am_i_admin", lambda: False), \
                mock.patch.object(browser, "profile_locked", lambda d: True):
            msg = browser._launch_failure_hint("Edge", "/tmp/prof")
        self.assertIn("profile 被占着", msg)


class TestCaptureSession(unittest.TestCase):
    """核心承诺：**没验证过的凭据，绝不交出去**。"""

    def test_returns_session_when_verified(self):
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S; hwssot3=1", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="CSRF1"), \
                mock.patch.object(browser, "_shutdown", lambda p: None):
            sess = browser.capture_session(Path("/x"), headless=True, timeout=5,
                                           verify=lambda s: True)
        self.assertEqual(sess.csrf, "CSRF1")
        self.assertEqual(sess.source, "browser")

    def test_never_returns_unverified_credentials(self):
        """自检一直不过 → 必须抛错，而不是把这份 cookie 交出去。"""
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S; hwssot3=1", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="CSRF1"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=True, timeout=1.2,
                                        verify=lambda s: False)
        msg = str(ctx.exception)
        self.assertIn("自检一直没过", msg, "要说清楚是卡在自检，而不是笼统的'没登录'")

    def test_headless_failure_points_at_the_visible_capture(self):
        """静默续期失败时，要说清下一步是**改用有界面的抓取**。

        门店实测：静默续期报"拿到了 cookie，但自检一直没过"，人看着这句话
        完全不知道该干什么 —— 而真正的原因只是这台电脑还没有过一次
        "有界面"的登录（profile 里没有 SSO 登录态）。无头模式下没有任何窗口
        能补这次登录，所以必须明确指向另一个按钮。
        """
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S; hwssot3=1", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="CSRF1"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=True, timeout=1.2,
                                        verify=lambda s: False)
        self.assertIn("打开浏览器抓取", str(ctx.exception),
                      "无头失败要说清改用哪个按钮")

    def _nav_watch(self, loops=8):
        """把 time 换成假时钟，让循环真走到"12 秒没跳登录页"那个兜底。

        `timeout` 给小了到不了阈值（1.2 秒 < 12 秒），给大了每轮要等 3 秒 ——
        所以直接把 `time.time()` 每次调用拨快 3 秒，`sleep` 吃掉。
        （用 count 而不是有限序列：`launch()` 内部也在调 `time.time()`。）
        """
        self.navigated = []
        ticks = count(0, 3)
        return [
            mock.patch.object(browser.time, "time", lambda: next(ticks)),
            mock.patch.object(browser.time, "sleep", lambda s: None),
            mock.patch.object(browser, "launch",
                              return_value=(_FakeProc(True), 12345)),
            mock.patch.object(browser, "_shutdown", lambda p: None),
            mock.patch.object(browser, "cookies_from_browser",
                              return_value=("lang=zh_CN", {})),
            mock.patch.object(browser, "goto_url",
                              lambda port, url, say=None: self.navigated.append(url)),
        ]

    def test_headless_never_navigates_to_the_login_page(self):
        """⚠ 无头模式下**不要**导航到登录页等人操作。

        没有人能看到那个窗口 —— 导航过去只会让浏览器停在一个需要人工操作的
        页面上干等到超时（门店就是傻等了 90 秒）。
        """
        with ExitStack() as stack:
            for p in self._nav_watch():
                stack.enter_context(p)
            with self.assertRaises(CbgAuthError):
                # timeout 要够跑好几轮：假时钟每轮推进约 6 秒，而导航阈值是 12 秒
                browser.capture_session(Path("/x"), headless=True, timeout=30,
                                        verify=lambda s: True)
        self.assertEqual(self.navigated, [], "无头模式不该导航到登录页")

    def test_visible_session_still_navigates_to_the_login_page(self):
        """有界面时那条兜底**必须留着** —— 一直没跳到登录页就自己导航过去。"""
        with ExitStack() as stack:
            for p in self._nav_watch():
                stack.enter_context(p)
            with self.assertRaises(CbgAuthError):
                # timeout 要够跑好几轮，否则到不了 12 秒的导航阈值
                browser.capture_session(Path("/x"), headless=False, timeout=30,
                                        verify=lambda s: True)
        self.assertTrue(self.navigated, "有界面时要导航到登录页，否则人不知道该登什么")

    def test_waits_until_verification_passes(self):
        """先不过、后过 —— 要能等到通过为止（登录是需要时间的）。"""
        calls = {"n": 0}

        def verify(_):
            calls["n"] += 1
            return calls["n"] >= 3

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="C"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            sess = browser.capture_session(Path("/x"), headless=True, timeout=30,
                                           verify=verify)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sess.csrf, "C")

    def test_aborts_when_the_browser_dies(self):
        """浏览器进程没了 → 立刻报错，别干等到超时。

        （可能是用户关了窗口，也可能是它自己崩了 —— 提示要把两种可能都说出来。）
        """
        with _patch_launch(alive=False), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=False, timeout=300,
                                        verify=lambda s: True)
        msg = str(ctx.exception)
        self.assertIn("退出", msg)
        self.assertIn("别关", msg, "最常见的原因是用户把登录窗口关了 —— 要说出来")

    def test_a_mid_run_death_does_not_give_launch_advice(self):
        """**跑到一半**浏览器没了，就别再念"启动失败"那套。

        门店真踩到过：服务是管理员身份、Edge 自我降权重启，端口短暂失灵被判成
        "浏览器关掉了"，然后提示里第一条是"profile 被占着" —— 照着做纯属白折腾。
        中途死掉就该说中途的事。
        """
        with _patch_launch(alive=False), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=False, timeout=300,
                                        verify=lambda s: True)
        msg = str(ctx.exception)
        self.assertNotIn("CBG_BROWSER_NO_SANDBOX", msg,
                         "沙箱那条只对'启动失败'对症，中途死掉列出来是误导")
        self.assertNotIn("SingletonLock", msg, "profile 被占同理")

    def test_detached_death_explains_the_probe_not_a_mythical_exit_code(self):
        """DetachedBrowser 的 `returncode` **永远是 None**。

        旧文案写成"退出码 None"，看着像崩溃，其实是"调试端口没应答" ——
        而且管理员身份下端口失灵是**会自己恢复**的，不该一探不到就判死。
        """
        with mock.patch.object(browser, "launch",
                               return_value=(browser.DetachedBrowser(53860), 53860)), \
                mock.patch.object(browser, "_port_alive", lambda p: False), \
                mock.patch.object(browser, "_close_via_cdp", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=False, timeout=300,
                                        verify=lambda s: True)
        msg = str(ctx.exception)
        self.assertIn("53860", msg, "要说清是哪个端口没了")
        self.assertIn("连续", msg, "要说明是连续探测失败，不是一次没探到")
        self.assertNotIn("退出码 None", msg, "别报一个根本不存在的退出码")

    def test_no_login_cookie_keeps_waiting(self):
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("lang=zh_CN", {})), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=True, timeout=1.0,
                                        verify=lambda s: True)
        self.assertIn("没看到登录 cookie", str(ctx.exception))

    def test_browser_is_always_shut_down(self):
        """不管成功失败，都得把浏览器进程收掉，不能留一堆僵尸。"""
        killed = []
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=S", {})), \
                mock.patch.object(browser, "csrf_from_page", return_value="C"), \
                mock.patch.object(browser, "_shutdown", lambda p: killed.append(p)), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            browser.capture_session(Path("/x"), headless=True, timeout=5, verify=lambda s: True)
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=True, timeout=0.5,
                                        verify=lambda s: False)
        self.assertEqual(len(killed), 2)


class TestCsrfFromApi(unittest.TestCase):
    def _resp(self, payload, status=200):
        r = types.SimpleNamespace(status_code=status)
        r.json = lambda: payload
        return r

    def test_reads_data_field(self):
        with mock.patch.object(browser.requests, "post",
                               return_value=self._resp({"status": "success", "data": "TOK-1"})):
            self.assertEqual(browser.csrf_from_api("a=b"), "TOK-1")

    def test_reads_nested(self):
        with mock.patch.object(browser.requests, "post",
                               return_value=self._resp({"result": {"csrfToken": "TOK-2"}})):
            self.assertEqual(browser.csrf_from_api("a=b"), "TOK-2")

    def test_returns_none_on_http_error(self):
        with mock.patch.object(browser.requests, "post",
                               return_value=self._resp({}, status=403)):
            self.assertIsNone(browser.csrf_from_api("a=b"))

    def test_returns_none_on_network_error(self):
        with mock.patch.object(browser.requests, "post",
                               side_effect=browser.requests.RequestException("boom")):
            self.assertIsNone(browser.csrf_from_api("a=b"))


class TestFindBrowser(unittest.TestCase):
    def test_returns_tuple_or_none(self):
        got = browser.find_browser()
        self.assertTrue(got is None or (isinstance(got, tuple) and len(got) == 2))


class TestBrowserPreference(unittest.TestCase):
    """两台都装时用哪个。

    默认 **Chrome 优先**：门店电脑上 Chrome 往往也装了，而它遇到的麻烦更少 ——
    Edge 从管理员进程启动时自我降权重启（AutoDeElevate）更凶，抓会话容易被掐断。
    """

    def _fake_system(self, installed):
        """装哪些浏览器。system('Windows') 是因为要挑 _WIN_PATHS 那组路径。"""
        def fake_exists(self):
            s = str(self)
            win = ("\\" in s) or (":\\" in s)
            if not win:
                return False
            if "Chrome" in s:
                return "chrome" in installed
            if "Edge" in s or "msedge" in s:
                return "edge" in installed
            return False
        return (mock.patch.object(browser.platform, "system", lambda: "Windows"),
                mock.patch.object(browser.Path, "exists", fake_exists))

    def test_chrome_wins_when_both_are_installed(self):
        """⚠ 这条就是需求本身：两台都装 → 用 Chrome。"""
        p1, p2 = self._fake_system({"chrome", "edge"})
        with p1, p2:
            got = browser.find_browser()
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "Chrome")
        self.assertIn("Chrome", got[0])

    def test_edge_is_used_when_chrome_is_absent(self):
        """只有 Edge 的机器（Windows 自带）必须照常能用。"""
        p1, p2 = self._fake_system({"edge"})
        with p1, p2:
            got = browser.find_browser()
        self.assertIsNotNone(got)
        self.assertEqual(got[1], "Edge")

    def test_config_can_flip_the_order(self):
        """想用 Edge 不用改代码：browser.prefer: [edge, chrome]。"""
        p1, p2 = self._fake_system({"chrome", "edge"})
        with p1, p2:
            got = browser.find_browser({"browser": {"prefer": ["edge", "chrome"]}})
        self.assertEqual(got[1], "Edge")

    def test_prefer_falls_through_to_the_next_name(self):
        """prefer 里第一个没装 → 用第二个，不是直接失败。"""
        p1, p2 = self._fake_system({"edge"})
        with p1, p2:
            got = browser.find_browser({"browser": {"prefer": ["chrome", "edge"]}})
        self.assertEqual(got[1], "Edge")

    # ------------------------------------------------------------ 配置解析
    def test_prefer_parsing(self):
        self.assertEqual(browser._browser_prefer({"browser": {"prefer": ["chrome"]}}),
                         ["chrome"])
        self.assertEqual(browser._browser_prefer({"browser": {"prefer": "EDGE"}}),
                         ["edge"], "写成字符串也认")
        self.assertEqual(
            browser._browser_prefer({"browser": {"prefer": ["chrome", "chrome"]}}),
            ["chrome"], "去重")
        self.assertEqual(browser._browser_prefer({"browser": {"prefer": ["firefox"]}}),
                         [], "不认得的名字忽略掉")

    def test_a_typo_never_disables_the_feature(self):
        """写错名字不能把"自动抓会话"整个弄没 —— 退回默认顺序。"""
        p1, p2 = self._fake_system({"chrome"})
        with p1, p2:
            got = browser.find_browser({"browser": {"prefer": ["netscape", "ie"]}})
        self.assertIsNotNone(got, "认不出就回退默认，不能返回 None")
        self.assertEqual(got[1], "Chrome")

    def test_garbage_config_does_not_crash(self):
        """配置写坏了（标量、None、数字）也只能降级，不能抛。"""
        for bad in ({"browser": "chrome"}, {"browser": None}, {"browser": {"prefer": 7}},
                    {"browser": {"prefer": None}}, {}):
            self.assertEqual(browser._browser_prefer(bad), [], f"{bad} 应该被忽略")

    def test_cfg_is_optional(self):
        """老调用方不传 cfg 也得能用（向后兼容）。"""
        p1, p2 = self._fake_system({"chrome"})
        with p1, p2:
            self.assertEqual(browser.find_browser()[1], "Chrome")
            self.assertEqual(browser.find_browser(None)[1], "Chrome")


if __name__ == "__main__":
    unittest.main()


class TestLoginCredentials(unittest.TestCase):
    """华为账号密码 —— 性质等同密码，存储和回显都要按凭据对待。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)

    def tearDown(self):
        self.dir.cleanup()

    def test_roundtrip(self):
        cfg = {}
        self.assertEqual(browser.load_login_credentials(cfg, self.root), ("", ""))
        browser.save_login_credentials(cfg, self.root, username="u1", password="p1")
        self.assertEqual(browser.load_login_credentials(cfg, self.root), ("u1", "p1"))

    def test_only_touches_given_field(self):
        cfg = {}
        browser.save_login_credentials(cfg, self.root, username="u1", password="p1")
        browser.save_login_credentials(cfg, self.root, username="u2")
        self.assertEqual(browser.load_login_credentials(cfg, self.root), ("u2", "p1"))

    def test_describe_never_leaks_password(self):
        cfg = {}
        browser.save_login_credentials(cfg, self.root, username="u1", password="TOP-SECRET")
        d = browser.describe_login(cfg, self.root)
        self.assertNotIn("TOP-SECRET", repr(d))
        self.assertNotIn("password", d)
        self.assertTrue(d["has_password"])
        self.assertTrue(d["ready"])

    def test_describe_not_ready_without_password(self):
        cfg = {}
        browser.save_login_credentials(cfg, self.root, username="u1")
        self.assertFalse(browser.describe_login(cfg, self.root)["ready"])

    def test_env_file_is_0600(self):
        cfg = {}
        p = browser.save_login_credentials(cfg, self.root, username="u", password="p")
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_default_login_url_is_the_store_entry(self):
        """以门店日常入口为准 —— 未登录时它自己跳 SSO，别硬编码登录地址。"""
        url = browser.login_url({})
        self.assertIn("cbg.huawei.com", url)
        self.assertIn("smart-store/homepage", url)

    def test_config_can_override_login_url(self):
        self.assertEqual(browser.login_url({"session": {"login_url": "https://x/y"}}),
                         "https://x/y")

    def test_sso_url_carries_redirect(self):
        u = browser.login_page_url("https://cbg.huawei.com/#/a/b")
        self.assertTrue(u.startswith(browser.SSO_ENTRY))
        self.assertIn("redirect=", u)
        self.assertIn("%23", u, "# 必须转义，否则 redirect 会被截断")


class TestAutoLogin(unittest.TestCase):
    """自动填表：靠 JS 在页面里做，这里验证"分支判断对不对"。"""

    def _patch_eval(self, replies):
        seq = list(replies)

        def fake(pg, expr, timeout=15.0):
            return seq.pop(0) if seq else None

        return mock.patch.object(browser, "_eval", fake)

    def _patch_page(self):
        return mock.patch.object(browser, "_page_ws", lambda port: "ws://x")

    def test_submits_when_form_present(self):
        with self._patch_page(), self._patch_eval([
                '{"hasForm": true, "captcha": false}',      # ① 检测到登录表单
                '{"u": 5, "p": 8}',                         # ② 填
                '{"p": "secret"}',                          # ③ 回读校验（值真的进去了）
                "clicked",                                  # ④ 点「登录」
        ]), mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            said = []
            r = browser.try_auto_login(1234, "user", "pw", say=said.append)
        self.assertEqual(r, "submitted")
        self.assertTrue(any("提交" in s for s in said))

    def test_page_error_is_detected_and_reported(self):
        """登录页已经报错了（多半是账号密码不对）→ 不能再填再提交。

        密码错了连打三次，轻则弹验证码、重则锁号。
        """
        with self._patch_page(), \
                self._patch_eval(['{"hasForm": true, "captcha": false, "error": "账号或密码错误"}']), \
                mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            said = []
            self.assertEqual(browser.try_auto_login(1, "u", "p", say=said.append), "error")
        self.assertTrue(any("账号或密码错误" in s for s in said), "要把页面原文带出来")

    def test_error_takes_precedence_over_filling(self):
        """检测到错误就不该再去填表 —— 一次都不行。"""
        calls = []

        def fake(pg, expr, timeout=15.0):
            calls.append(expr)
            return '{"hasForm": true, "captcha": false, "error": "密码不正确"}'

        with self._patch_page(), mock.patch.object(browser, "_eval", fake), \
                mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            browser.try_auto_login(1, "u", "p")
        self.assertEqual(len(calls), 1, "只该调一次检测，不该往下走到填表")

    def test_captcha_is_reported_not_retried(self):
        with self._patch_page(), self._patch_eval(['{"hasForm": true, "captcha": true}']), \
                mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            self.assertEqual(browser.try_auto_login(1, "u", "p"), "captcha")

    def test_no_form_means_not_on_login_page(self):
        with self._patch_page(), self._patch_eval(['{"hasForm": false}']), \
                mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            self.assertEqual(browser.try_auto_login(1, "u", "p"), "no-form")

    def test_password_not_sticking_is_reported(self):
        """页面结构变了、值没填进去，要说出来，别假装成功。"""
        with self._patch_page(), self._patch_eval([
                '{"hasForm": true, "captcha": false}',
                '{"u": 5, "p": 0}',                         # 填
                '{"p": ""}',                                # 回读是空 → 没填进去
        ]), mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            said = []
            self.assertEqual(browser.try_auto_login(1, "u", "p", say=said.append), "no-form")
        self.assertTrue(any("没填进去" in s for s in said))

    def test_password_is_json_escaped(self):
        """密码里有引号/反斜杠也不能把注入的 JS 拼坏（更不能泄漏到别处）。"""
        captured = {}
        seq = ['{"hasForm": true, "captcha": false}', '{"u":1,"p":1}',
               '{"p": "x"}', "clicked"]

        def fake(pg, expr, timeout=15.0):
            if "const set = (el, v)" in expr:      # 就是那段填表 JS
                captured["expr"] = expr
            return seq.pop(0)

        with self._patch_page(), mock.patch.object(browser, "_eval", fake), \
                mock.patch.object(browser, "Cdp") as CdpMock:
            CdpMock.return_value = mock.MagicMock()
            browser.try_auto_login(1, 'u"x', 'p\\"y')
        self.assertIn(json.dumps('p\\"y'), captured["expr"], "密码必须经过 json.dumps 转义")


class TestCaptureWithCredentials(unittest.TestCase):
    def test_auto_login_is_attempted_and_retried(self):
        calls = {"n": 0}

        def fake_login(port, u, p, say=None):
            calls["n"] += 1
            return "submitted"

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login", fake_login), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=False, timeout=1.0,
                                        credentials=("u", "p"), verify=lambda s: True)
        self.assertGreater(calls["n"], 0, "给了账号密码就该去自动登录")

    def test_no_attempt_without_credentials(self):
        calls = {"n": 0}

        def fake_login(*a, **k):
            calls["n"] += 1
            return "submitted"

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login", fake_login), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=False, timeout=0.8,
                                        verify=lambda s: True)
        self.assertEqual(calls["n"], 0, "没给账号密码就不该瞎试")

    def test_login_error_stops_further_attempts(self):
        """账号密码不对时**只试一次** —— 连打失败登录会把账号试锁。"""
        calls = {"n": 0}

        def fake_login(*a, **k):
            calls["n"] += 1
            return "error"

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login", fake_login), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=False, timeout=0.8,
                                        credentials=("u", "p"), verify=lambda s: True)
        self.assertEqual(calls["n"], 1, "报错之后一次都不该再试")

    def test_captcha_stops_further_attempts(self):
        calls = {"n": 0}

        def fake_login(*a, **k):
            calls["n"] += 1
            return "captcha"

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login", fake_login), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=False, timeout=0.8,
                                        credentials=("u", "p"), verify=lambda s: True)
        self.assertEqual(calls["n"], 1, "碰到验证码就别再重试了，免得把账号试锁")


class TestLaunchSettling(unittest.TestCase):
    """**端口通了 ≠ 活下来了。**

    实测踩过：受限环境里 Chrome 起不了自己的子进程沙箱，它会在**端口应答之后
    约 1~2 秒**整个进程退出。原来的 `launch` 看到端口通了就返回，
    结果抓取循环一直读不到 cookie，只能干等到超时 —— 用户看到的就是"自动抓取失败"。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def _run(self, die_on=(), explicit_no_sandbox=None):
        """只 mock 底层（Popen / http_json），让真的 launch() 跑起来。

        die_on: 第几次 Popen（1 起算）出来的进程会立刻死。
        """
        popen_calls = []
        made = []

        def fake_popen(args, **kw):
            popen_calls.append(args)
            n = len(popen_calls)
            proc = types.SimpleNamespace(
                returncode=(1 if n in die_on else None),
                poll=lambda: (1 if n in die_on else None),
                kill=lambda: None, terminate=lambda: None, wait=lambda timeout=None: 0)
            made.append(proc)
            return proc

        def fake_http(*a, **k):
            # 端口跟进程**同生共死** —— 进程没了端口也就没了。
            # （老版本这里无论死活都返回成功，模型不真实：分不出
            #  "起来又死了" 和 "Chrome 降权重启、换了个进程接着服务端口"。）
            cur = made[-1] if made else None
            if cur is not None and cur.poll() is not None:
                raise browser.CdpError("端口没了")
            return {}

        with mock.patch.object(browser, "find_browser",
                               lambda *a, **k: ("/fake/chrome", "Chrome")), \
                mock.patch.object(browser.subprocess, "Popen", fake_popen), \
                mock.patch.object(browser, "http_json", fake_http), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            try:
                got = browser.launch(Path(self.dir.name), no_sandbox=explicit_no_sandbox)
                return got, popen_calls, None
            except browser.BrowserError as e:
                return None, popen_calls, e

    def test_retries_with_no_sandbox_when_it_dies_right_after_starting(self):
        got, calls, err = self._run(die_on=(1,))
        self.assertIsNone(err, f"应当自动重试成功：{err}")
        self.assertEqual(len(calls), 2, "第一次死了要再来一次")
        self.assertNotIn("--no-sandbox", calls[0], "第一次先按正常方式起")
        self.assertIn("--no-sandbox", calls[1], "第二次要带上 --no-sandbox")
        self.assertIn("--disable-gpu", calls[1])

    def test_does_not_retry_when_it_comes_up_fine(self):
        got, calls, err = self._run()
        self.assertIsNone(err)
        self.assertEqual(len(calls), 1, "正常起来就别重试")
        self.assertNotIn("--no-sandbox", calls[0])

    def test_gives_up_with_a_clear_message(self):
        got, calls, err = self._run(die_on=(1, 2), explicit_no_sandbox=True)
        self.assertIsNotNone(err, "两次都死就该报错")
        self.assertIn("退出", str(err))
        self.assertIn("profile", str(err), "要提示另一种可能：profile 目录坏了")

    def test_only_two_attempts(self):
        """别无限重试 —— 起不来就是起不来。"""
        got, calls, err = self._run(die_on=(1, 2, 3, 4))
        self.assertEqual(len(calls), 2)

    def test_settle_window_exists(self):
        self.assertGreater(browser.SETTLE_SECONDS, 0, "返回前必须观察一小会儿")


class TestCaptchaAbortsAutoLogin(unittest.TestCase):
    """⚠ 自动登录撞上验证码 → **必须中止并把这件事传给上层**。

    以前只是 `say()` 一句就继续干等 —— 而 `say()` 在控制台里只进滚动日志，
    最后那个醒目的横幅一个字都不提验证码。用户等到超时，
    看到的是「始终没看到登录 cookie」，完全不知道要输验证码。
    """

    def test_raises_dedicated_error_inheriting_auth_error(self):
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login",
                                  lambda *a, **k: "captcha"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(browser.CbgCaptchaRequired) as ctx:
                browser.capture_session(Path("/x"), headless=False, timeout=1.0,
                                        credentials=("u", "p"), verify=lambda s: True)
        self.assertIn("验证码", str(ctx.exception))
        # ⚠ 必须是 CbgAuthError 的子类：cli.py 三处 + probe_session 靠它兜底，
        #   不是子类的话新异常会直接炸穿那些调用方
        self.assertIsInstance(ctx.exception, CbgAuthError)


class TestCaptchaMemory(unittest.TestCase):
    """⚠ 没有这个标记就会**死循环**：

    撞验证码 → 删 profile → 用户按提示重新点一次 → 自动登录又把同一个验证码
    撞出来 → 又中止…… 「重新点一次并手动登录」这句提示根本执行不了。
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_mark_then_read_then_clear(self):
        self.assertFalse(browser.captcha_marked(self.root))
        browser.mark_captcha(self.root)
        self.assertTrue(browser.captcha_marked(self.root))
        browser.clear_captcha(self.root)
        self.assertFalse(browser.captcha_marked(self.root))

    def test_broken_state_file_is_not_a_crash(self):
        """这是降级用的东西 —— 坏了最多是「这次照常自动填」，**绝不能抛**。"""
        p = browser._state_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        for junk in ("{ 不是 json", "[]", "", "null"):
            p.write_text(junk, encoding="utf-8")
            self.assertFalse(browser.captcha_marked(self.root), repr(junk))

    def test_marked_means_no_auto_fill(self):
        browser.mark_captcha(self.root)
        calls = {"n": 0}

        def fake_login(*a, **k):
            calls["n"] += 1
            return "submitted"

        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "try_auto_login", fake_login), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError):
                browser.capture_session(Path("/x"), headless=False, timeout=0.8,
                                        credentials=("u", "p"), verify=lambda s: True,
                                        state_root=self.root)
        self.assertEqual(calls["n"], 0, "标记还在时**不许**再自动填账号密码")

    def test_success_clears_the_mark(self):
        browser.mark_captcha(self.root)
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser",
                                  return_value=("JSESSIONID=abc", {})), \
                mock.patch.object(browser, "csrf_from_page", lambda p: "C"), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            browser.capture_session(Path("/x"), headless=True, timeout=5,
                                    verify=lambda s: True, state_root=self.root)
        self.assertFalse(browser.captcha_marked(self.root),
                         "抓到一次就该把标记清掉，否则以后永远不自动填了")


class TestCaptchaDuringManualLogin(unittest.TestCase):
    """⚠ 口径和自动登录那条**故意不一样**。

    自动登录撞的 → 中止（是我们自己试出来的，收尾要干净）。
    用户手动登录时页面出现验证码 → **别动它** —— 那一瞬他正在输，
    关窗 + 删 profile 等于把他手里的东西抢走，还得从头再来一轮。
    """

    def _run(self, headless, js_value):
        need = []
        # ⚠ `Cdp.__init__` 是**急切连接**（里面就 `WebSocket(url)`）——
        #   不换掉它，测试会真的去连 `ws://`。`_page_ws` 同理，
        #   它底下 `http_json` 会去连 12345 端口然后抛 CdpError。
        #
        # ⚠ `CAPTCHA_GRACE` 也**必须调小**：命中验证码会把 deadline 往后延那么多秒
        #   （真实值 120），不换掉的话这条测试要真跑两分钟 —— 第一次就是这么挂住的。
        # ⚠ 这行注释只能写在 `with` **外面**：夹在 `\` 续行中间是语法错（踩过）。
        with _patch_launch(), \
                mock.patch.object(browser, "cookies_from_browser", return_value=("", {})), \
                mock.patch.object(browser, "_page_ws", lambda port, *h: "ws://stub"), \
                mock.patch.object(browser, "Cdp",
                                  lambda ws, timeout=20: mock.Mock(close=lambda: None)), \
                mock.patch.object(browser, "_eval", lambda *a, **k: js_value), \
                mock.patch.object(browser, "_shutdown", lambda p: None), \
                mock.patch.object(browser, "CAPTCHA_GRACE", 0.05), \
                mock.patch.object(browser.time, "sleep", lambda s: None):
            with self.assertRaises(CbgAuthError) as ctx:
                browser.capture_session(Path("/x"), headless=headless, timeout=0.6,
                                        verify=lambda s: True,
                                        on_need=need.append)
        return need, ctx.exception

    def test_manual_login_reports_but_does_not_abort(self):
        need, exc = self._run(False, json.dumps({"captcha": True}))
        self.assertEqual(need, ["captcha"], "要回调出去让界面提示")
        self.assertNotIsInstance(exc, browser.CbgCaptchaRequired,
                                 "手动登录撞验证码**不许**中止")

    def test_headless_has_nobody_to_type_it(self):
        """无头模式没有人能输验证码 —— 所以一律走中止那条。"""
        need, exc = self._run(True, json.dumps({"captcha": True}))
        self.assertIsInstance(exc, browser.CbgCaptchaRequired)

    def test_no_captcha_means_no_callback(self):
        need, _ = self._run(False, json.dumps({"captcha": False}))
        self.assertEqual(need, [])
