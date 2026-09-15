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
        self.assertIn("可能的原因", browser._launch_failure_hint("Chrome"))


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
