"""检查更新 / 手动更新的测试。

**没有一条测试会真的去连 GitHub** —— 门店电脑连不上外网是常态，
测试也不该依赖网络。所有网络调用都 mock 掉，测的是我们自己的逻辑：

* 版本比较（字符串比会在 `1.10.0` vs `1.9.0` 上悄悄出错）
* **白名单**：`config/` / `.secrets/` / `out/` 一根手指都不许碰
* **仓库布局 == 安装布局**：照原样铺过去，不做任何路径映射
* 没变化就不写盘（别每次检查都把所有文件重写一遍）
"""

import base64
import contextlib
import io
import json
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import selfupdate


def _fake_zip(root_in_zip: str, files: dict) -> bytes:
    """造一个跟 GitHub 下载下来同构的 zip（顶层一个目录）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, content in files.items():
            z.writestr(f"{root_in_zip}/{path}", content)
    return buf.getvalue()


class TestVersionCompare(unittest.TestCase):
    def test_numeric_not_string_compare(self):
        """⚠ 字符串比的话 `"1.10.0" < "1.9.0"` 是 True —— 那就永远升不上去。"""
        self.assertTrue(selfupdate.is_newer("1.10.0", "1.9.0"))
        self.assertFalse(selfupdate.is_newer("1.9.0", "1.10.0"))

    def test_plain_cases(self):
        self.assertTrue(selfupdate.is_newer("1.2.0", "1.1.0"))
        self.assertTrue(selfupdate.is_newer("2.0.0", "1.99.99"))
        self.assertFalse(selfupdate.is_newer("1.2.0", "1.2.0"))
        self.assertFalse(selfupdate.is_newer("1.1.0", "1.2.0"))

    def test_garbage_does_not_crash(self):
        self.assertEqual(selfupdate.parse_version(""), (0, 0, 0))
        self.assertEqual(selfupdate.parse_version("坏值"), (0, 0, 0))
        self.assertFalse(selfupdate.is_newer("坏值", "1.0.0"))

    def test_two_part_version(self):
        self.assertEqual(selfupdate.parse_version("1.2"), (1, 2, 0))
        self.assertTrue(selfupdate.is_newer("1.3", "1.2.9"))


class TestCheck(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_reports_an_available_update(self):
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.3.0"):
            d = selfupdate.check(self.root, "1.2.0", force=True)
        self.assertTrue(d["has_update"])
        self.assertEqual(d["latest"], "1.3.0")
        self.assertEqual(d["current"], "1.2.0")

    def test_up_to_date(self):
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.2.0"):
            d = selfupdate.check(self.root, "1.2.0", force=True)
        self.assertFalse(d["has_update"])

    def test_network_failure_is_not_an_exception(self):
        """连不上 GitHub 是**常态**（门店外网受限），不能让页面崩。"""
        def boom(timeout=15):
            raise selfupdate.UpdateError("连不上 GitHub：超时")
        with mock.patch.object(selfupdate, "remote_version", boom):
            d = selfupdate.check(self.root, "1.2.0", force=True)
        self.assertFalse(d["has_update"])
        self.assertIn("连不上", d["error"])

    def test_result_is_cached(self):
        """概览页 30 秒刷一次，不能每次都去戳 GitHub。"""
        calls = {"n": 0}

        def counted(timeout=15):
            calls["n"] += 1
            return "1.3.0"

        with mock.patch.object(selfupdate, "remote_version", counted):
            selfupdate.check(self.root, "1.2.0", force=True)
            selfupdate.check(self.root, "1.2.0")           # 走缓存
            selfupdate.check(self.root, "1.2.0")           # 走缓存
        self.assertEqual(calls["n"], 1)

    def test_cache_can_be_forced(self):
        calls = {"n": 0}

        def counted(timeout=15):
            calls["n"] += 1
            return "1.3.0"

        with mock.patch.object(selfupdate, "remote_version", counted):
            selfupdate.check(self.root, "1.2.0", force=True)
            selfupdate.check(self.root, "1.2.0", force=True)
        self.assertEqual(calls["n"], 2)


class TestDailyWatch(unittest.TestCase):
    """后台每天自动查一次。

    光有按钮没用：门店同事**不会去点**，不主动查就永远停在装上去的那一版，
    修好的 bug 也到不了店里。所以这里测的是"没人管的时候它自己会不会查"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    # ---------------------------------------------------------------- cached()
    def test_cached_makes_no_network_call(self):
        """界面每 30 秒刷一次概览 —— 走这条路绝不能产生网络请求。"""
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.3.0"):
            selfupdate.check(self.root, "1.2.0", force=True)

        def boom(timeout=15):
            raise AssertionError("cached() 不许发网络请求")
        with mock.patch.object(selfupdate, "remote_version", boom):
            d = selfupdate.cached(self.root, "1.2.0")
        self.assertEqual(d["latest"], "1.3.0", "缓存里的东西要原样给出来")

    def test_cached_on_a_cold_cache_is_empty_not_an_error(self):
        """还没查过 —— 返回空字典，别在这里自作主张去查一次。"""
        with mock.patch.object(selfupdate, "remote_version",
                               lambda timeout=15: "1.3.0"):
            self.assertEqual(selfupdate.cached(self.root, "1.2.0"), {})

    def test_cached_recomputes_after_we_upgrade(self):
        """缓存里的 has_update 是按**当时的版本**算的。

        不重算的话：升到 1.3.0 之后界面还在说"有新版本 v1.3.0"（缓存 6 小时）。
        """
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.3.0"):
            selfupdate.check(self.root, "1.2.0", force=True)
        self.assertTrue(selfupdate.cached(self.root, "1.2.0")["has_update"])
        self.assertFalse(selfupdate.cached(self.root, "1.3.0")["has_update"],
                         "升级之后不该还提示有新版本")
        self.assertFalse(selfupdate.cached(self.root, "1.4.0")["has_update"])

    def test_appends_clear_time(self):
        """`at`（下次还查不查）和 `checked_at`（上次真问到是什么时候）是两回事。"""
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.3.0"):
            d = selfupdate.check(self.root, "1.2.0", force=True)
        self.assertIn("checked_at", d)
        self.assertNotIn("failed_at", d)

    def test_a_failure_keeps_the_last_successful_check_time(self):
        """失败会盖掉 `at`（免得每刷一次页面就重试），

        但界面要能说出"上次成功问到是三天前" —— 那说明网络已经不通三天了。
        """
        with mock.patch.object(selfupdate, "remote_version", lambda timeout=15: "1.2.0"):
            good = selfupdate.check(self.root, "1.2.0", force=True)

        def boom(timeout=15):
            raise selfupdate.UpdateError("连不上 GitHub（试了 3 次）：超时")
        with mock.patch.object(selfupdate, "remote_version", boom):
            bad = selfupdate.check(self.root, "1.2.0", force=True)

        self.assertIn("error", bad)
        self.assertIn("failed_at", bad)
        self.assertEqual(bad["checked_at"], good["checked_at"], "别把上次成功的时间抹掉")

    # ------------------------------------------------------------ daily_watcher
    def _stop(self, waits):
        """一个只 wait 这些次数的 stop —— 让 `while True` 能跑有限轮。

        序列用完了还继续 wait，就说明轮次比预期多 —— 让它**炸**，
        比悄悄 StopIteration 好查。
        """
        def next_wait(*a, **kw):
            raise AssertionError("wait 被调用的次数比预期多 —— 循环没按预期退出")
        return mock.MagicMock(wait=mock.MagicMock(side_effect=list(waits) + [next_wait]))

    def test_checks_after_a_first_delay_then_exits_on_stop(self):
        """先等一会儿再查（别和开机那一刻抢网络），stop 置位就干净退出。"""
        calls = {"n": 0}

        def counted(root, current, **kw):
            calls["n"] += 1
            return {"has_update": False, "latest": "", "current": current}

        stop = self._stop([False, True])   # 首次延迟 → 查一次 → 等一天 → 停
        with mock.patch.object(selfupdate, "check", counted), \
                mock.patch.object(selfupdate, "_log", lambda m: None):
            selfupdate.daily_watcher(self.root, "1.2.0", stop=stop)

        self.assertEqual(calls["n"], 1)
        self.assertEqual(stop.wait.call_args_list[0].args[0],
                         selfupdate.WATCH_FIRST_DELAY)
        self.assertEqual(stop.wait.call_args_list[-1].args[0],
                         selfupdate.WATCH_INTERVAL)

    def test_logs_when_a_new_version_shows_up(self):
        """发现了就得说出来 —— 不说等于没查。"""
        said = []
        stop = self._stop([False, True])
        with mock.patch.object(selfupdate, "check",
                               lambda r, c, **kw: {"has_update": True, "latest": "1.3.0"}), \
                mock.patch.object(selfupdate, "_log", said.append):
            selfupdate.daily_watcher(self.root, "1.2.0", stop=stop)
        self.assertTrue(any("1.3.0" in m for m in said), f"没说有新版本：{said}")

    def test_a_crash_does_not_kill_the_watcher(self):
        """这个线程绝不能死 —— 死了就**永远**不再检查，而且没人会发现。"""
        calls = {"n": 0}

        def flaky(root, current, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("不该在这上面炸掉")
            return {"has_update": False, "latest": ""}

        # 炸一次 → 还要接着跑第二轮 → 再退出
        stop = self._stop([False, False, True])
        with mock.patch.object(selfupdate, "check", flaky), \
                mock.patch.object(selfupdate, "_log", lambda m: None):
            selfupdate.daily_watcher(self.root, "1.2.0", stop=stop)
        self.assertEqual(calls["n"], 2, "炸一次就退出了 —— 那之后永远不再检查")

    def test_checks_once_a_day_not_once_a_minute(self):
        """一天一次。查太勤会被 GitHub 限流，而且毫无意义。"""
        self.assertEqual(selfupdate.WATCH_INTERVAL, 24 * 3600)


class TestWhitelist(unittest.TestCase):
    """**最重要的一条**：更新只覆盖代码，绝不碰数据。"""

    def setUp(self):
        self.zip_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.zip_root, ignore_errors=True))

    def _mk(self, rel, content="x"):
        p = self.zip_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_paths_follow_the_repo_exactly(self):
        """仓库布局 **就是** 安装布局 —— 照原样铺，不做任何映射。

        以前 bat 在仓库的 `packaging/` 下、安装目录里在根，更新时得靠一张
        映射表硬凑。那种"两套布局 + 对照表"的结构，加一个文件要想两处，迟早漏。
        """
        for rel in ("src/cli.py", "web/app.js", "bootstrap.py",
                    "install.bat", "安装部署指南.md", "tests/test_x.py",
                    "run_check.py", ".gitattributes"):
            self._mk(rel)
        got = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertEqual(got, {
            "src/cli.py", "web/app.js", "bootstrap.py",
            "install.bat", "安装部署指南.md", "tests/test_x.py",
            "run_check.py", ".gitattributes",
        }, "路径被改过了 —— 那就不再是'跟着仓库走'")

    def test_never_touches_data_directories(self):
        """⚠ 这是整个功能的底线。

        `config/` 是门店配置、`.secrets/` 是账号和华为会话、`out/` 是历史报告 ——
        覆盖任何一个都是在毁用户的数据。
        """
        for rel in ("config/store-SCN231409.yaml", ".secrets/erp.env",
                    "out/run.log", "dist/x.zip", "tools/build_package.sh",
                    "tools/build_package.sh"):
            self._mk(rel)
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        for bad in ("config/store-SCN231409.yaml", ".secrets/erp.env",
                    "out/run.log", "dist/x.zip", "tools/build_package.sh"):
            self.assertNotIn(bad, targets, f"黑名单漏了，会覆盖 {bad}")

    def test_stores_yaml_is_updated_but_its_neighbour_is_not(self):
        """⚠ `config/` 里住着两种东西，必须分开对待：

        * `config/stores.yaml` —— 14 家店的映射表，**随程序走**。加了新店要能更新下来
        * `config/store-<门店码>.yaml` —— **这台电脑**的配置，动了就是丢门店设置

        按目录一刀切会二选一错一个，所以只能做**文件级**例外。
        """
        self._mk("config/stores.yaml")
        self._mk("config/store-SCN231409.yaml")
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertIn("config/stores.yaml", targets,
                      "映射表更新不下去 —— 以后加门店就传不到店里")
        self.assertNotIn("config/store-SCN231409.yaml", targets,
                         "门店自己的配置被覆盖了")

    def test_new_repo_files_are_picked_up_automatically(self):
        """仓库里加了新文件，**不用改更新代码**就该跟着走。

        （这正是"路径跟着仓库走"换来的好处：白名单时代加个文件就得回来补名单，
        忘了就永远更新不到。）
        """
        self._mk("src/brand_new.py")
        self._mk("newdir/helper.py")
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertIn("src/brand_new.py", targets)
        self.assertIn("newdir/helper.py", targets)

    def test_skips_pycache(self):
        self._mk("src/cli.py")
        self._mk("src/__pycache__/cli.cpython-314.pyc")
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertNotIn("src/__pycache__/cli.cpython-314.pyc", targets)


class TestApply(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # 安装目录：有代码，也有**绝不能动**的数据
        (self.root / "src").mkdir()
        (self.root / "src" / "cli.py").write_text("旧代码", encoding="utf-8")
        (self.root / "config").mkdir()
        (self.root / "config" / "store-SCN231409.yaml").write_text("我的门店配置", encoding="utf-8")
        (self.root / ".secrets").mkdir()
        (self.root / ".secrets" / "erp.env").write_text("我的账号", encoding="utf-8")
        (self.root / "out").mkdir()
        (self.root / "out" / "报告.xlsx").write_text("历史报告", encoding="utf-8")

    def _apply(self, files, anchors=True):
        """anchors=False 用来模拟"下载下来的根本不是我们的包"。"""
        if anchors:
            files = {"bootstrap.py": "x", "src/cli.py": "旧代码", **files}
        blob = _fake_zip("cbg-reconcile-main", files)
        with mock.patch.object(selfupdate, "download") as dl:
            d = Path(tempfile.mkdtemp())
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                z.extractall(d)
            dl.return_value = d / "cbg-reconcile-main"
            try:
                return selfupdate.apply_update(self.root, current="1.2.0")
            finally:
                __import__("shutil").rmtree(d, ignore_errors=True)

    def test_updates_code(self):
        res = self._apply({"src/cli.py": "新代码", "src/version.py":
                           'VERSION = "1.3.0"\n'})
        self.assertTrue(res["ok"])
        self.assertEqual(res["to"], "1.3.0")
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")
        self.assertIn("src/cli.py", res["changed"])

    def test_data_survives_an_update(self):
        """更新代码之后，门店配置 / 账号 / 历史报告**必须原样还在**。"""
        self._apply({"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'})
        self.assertEqual((self.root / "config" / "store-SCN231409.yaml").read_text(encoding="utf-8"),
                         "我的门店配置")
        self.assertEqual((self.root / ".secrets" / "erp.env").read_text(encoding="utf-8"),
                         "我的账号")
        self.assertEqual((self.root / "out" / "报告.xlsx").read_text(encoding="utf-8"),
                         "历史报告")

    def test_even_a_malicious_zip_cannot_touch_data(self):
        """就算下载下来的包里**带** `config/`，我们也不认它。"""
        self._apply({"config/store-SCN231409.yaml": "恶意覆盖",
                     "src/version.py": 'VERSION = "1.3.0"\n'})
        self.assertEqual((self.root / "config" / "store-SCN231409.yaml").read_text(encoding="utf-8"),
                         "我的门店配置", "白名单失效了 —— 包里的 config/ 被写进去了")

    def test_reports_new_files_separately(self):
        res = self._apply({"src/version.py": 'VERSION = "1.3.0"\n',
                           "src/brand_new.py": "新的"})
        self.assertIn("src/brand_new.py", res["added"])
        self.assertNotIn("src/brand_new.py", res["changed"])

    def test_unchanged_files_are_not_rewritten(self):
        """没变就别写盘 —— 别每次检查更新都把几百个文件重写一遍。"""
        res = self._apply({"src/cli.py": "旧代码",            # 跟现有一模一样
                           "src/version.py": 'VERSION = "1.3.0"\n'})
        self.assertNotIn("src/cli.py", res["changed"])

    def test_writes_a_build_stamp(self):
        """从 GitHub 更新过来的没有打包时间戳 —— 写一个，界面才显示得出是哪一版。"""
        self._apply({"src/version.py": 'VERSION = "1.3.0"\n'})
        stamp = (self.root / "BUILD.txt").read_text(encoding="utf-8")
        self.assertIn("1.3.0", stamp)
        self.assertIn("GitHub", stamp)

    def test_a_foreign_zip_is_rejected(self):
        """⚠ 现在是"照原样铺"（不是白名单），所以得先确认**这确实是我们的包**。

        GitHub 返回个错误页、或者仓库地址写错了，没有这道闸就会把一堆
        不相干的文件铺进安装目录 —— 而且不会报错。
        """
        with self.assertRaises(selfupdate.UpdateError) as cm:
            self._apply({"README-other.txt": "x"}, anchors=False)
        self.assertIn("不像我们的包", str(cm.exception))

    def test_anchor_missing_is_rejected(self):
        """少一个锚点也要拒 —— 半拉子的包更危险。"""
        with self.assertRaises(selfupdate.UpdateError):
            self._apply({"src/cli.py": "x"}, anchors=False)      # 缺 bootstrap.py


class TestDownloadSources(unittest.TestCase):
    """⚠ 实测：`codeload.github.com` 会发**缓存的旧 zip**。

    仓库已经改成新布局了，它还在给旧的那份（加时间戳参数也没用）。
    对更新功能来说这是**危险**的 —— 你以为更新了，拿到的是旧代码甚至是旧的
    目录结构。所以优先走 `api.github.com/.../zipball`，codeload 只当退路。
    """

    def test_api_zipball_is_tried_first(self):
        urls = selfupdate._zip_urls(selfupdate.BRANCH)
        self.assertEqual(len(urls), 2, "退路没了")
        self.assertIn("api.github.com", urls[0])
        self.assertIn("codeload", urls[1])

    def test_zip_urls_follow_the_requested_ref(self):
        """回退要能指定任意 commit —— 地址里的 ref 必须换成它。"""
        sha = "abc1234def5678"
        urls = selfupdate._zip_urls(sha)
        self.assertIn(sha, urls[0])
        self.assertIn(sha, urls[1])
        self.assertNotIn(selfupdate.BRANCH, urls[0].replace(f"zipball/{sha}", ""))

    def test_falls_back_to_the_second_source(self):
        """第一个源挂了要用第二个 —— 门店网络什么样都有。"""
        import requests as real
        tried = []

        def fake_get(url, *, timeout, stream=False):
            tried.append(url)
            if "api.github.com" in url:
                raise selfupdate.UpdateError("第一个源挂了")
            raise selfupdate.UpdateError("第二个也挂了")

        with mock.patch.object(selfupdate, "_get", fake_get):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.download()
        self.assertEqual(len(tried), 2)
        self.assertIn("下载失败", str(cm.exception))

    def test_error_mentions_both_sources(self):
        def fake_get(url, *, timeout, stream=False):
            raise selfupdate.UpdateError("连不上")

        with mock.patch.object(selfupdate, "_get", fake_get):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.download()
        msg = str(cm.exception)
        self.assertIn("api.github.com", msg)
        self.assertIn("codeload", msg, "报错要说清两个源都试过了")


class TestFailureDiagnostics(unittest.TestCase):
    """更新失败时，报错必须说清**到底收到了什么**。

    门店实测：更新报「下载下来的包里缺少 ['src/cli.py'] —— 这不像我们的包」。
    就这一句，门店既看不出收到了什么东西，也不知道下一步做什么 ——
    而这恰恰是最需要信息的时候（那些网络里，代理 / 安全设备 / 镜像站
    换个响应是常事）。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _blob(self, data: bytes) -> Path:
        p = self.tmp / "src.zip"
        p.write_bytes(data)
        return p

    def _pkg(self, name: str, files: dict) -> Path:
        # ⚠ 包要放在 updates/ 下面，别和安装目录（self.tmp）混在一起 ——
        #   混了的话 apply_update 会把包铺进去，断言看到的路径就对不上。
        root = self.tmp / "updates" / name
        for rel, content in files.items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        return root

    # ---------------------------------------------------------- 认得出网页
    def test_detects_an_html_error_page(self):
        """代理 / 安全设备拦下来时给的是**网页**，content-type 却还是 zip。"""
        self.assertTrue(selfupdate._looks_like_html(
            self._blob(b"<!DOCTYPE html><html><body>403 Forbidden</body></html>")))

    def test_detects_html_with_leading_whitespace(self):
        self.assertTrue(selfupdate._looks_like_html(self._blob(b"\n\n  <html>x")))

    def test_a_real_zip_is_not_html(self):
        self.assertFalse(selfupdate._looks_like_html(self._blob(b"PK\x03\x04rest")))

    def test_empty_file_is_not_html(self):
        self.assertFalse(selfupdate._looks_like_html(self._blob(b"")))

    def test_missing_file_does_not_raise(self):
        self.assertFalse(selfupdate._looks_like_html(self.tmp / "nope.bin"))

    def test_payload_description_names_the_page(self):
        blob = self._blob(b"<!DOCTYPE html><html><body>Access Denied</body></html>")
        desc = selfupdate._describe_payload(blob, 200, "application/zip")
        self.assertIn("HTTP 200", desc)
        self.assertIn("application/zip", desc, "要报出服务器声明的类型（大概率是错的）")
        self.assertIn("网页", desc, "要说清收到的是网页不是压缩包")
        self.assertIn("Access Denied", desc, "把内容开头带出来，人一眼能认")

    def test_payload_description_survives_a_missing_file(self):
        desc = selfupdate._describe_payload(self.tmp / "nope.bin", 502, "")
        self.assertIn("502", desc)

    # ------------------------------------------------- 包结构要能说清
    def test_package_description_lists_what_arrived(self):
        pkg = self._pkg("other-repo", {"README.md": "# x", "app.py": "y"})
        desc = selfupdate._describe_package(pkg)
        self.assertIn("README.md", desc)
        self.assertIn("src/cli.py", desc, "缺的是什么要说出来")
        self.assertIn("2 个文件", desc)

    def test_package_description_does_not_mix_up_names_and_purposes(self):
        """`ANCHORS` 是 (路径, 说明) —— 别把说明当成文件名报出去。"""
        pkg = self._pkg("other2", {"README.md": "# x"})
        desc = selfupdate._describe_package(pkg)
        self.assertIn("['src/cli.py', 'bootstrap.py']", desc)
        self.assertNotIn("命令行入口", desc.split("缺的是")[-1],
                         "「缺的是」后面应该只有路径")

    # --------------------------------------------- 端到端的报错内容
    def test_anchor_failure_tells_the_store_what_to_do(self):
        """锚点缺失是**门店会遇到**的那条路径，报错要能照着做。"""
        pkg = self._pkg("HappyJoy95-cbg-reconcile-deadbeef",
                        {"README.md": "# 不是我们的仓库", "web/index.html": "x"})
        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.apply_update(self.tmp, current="1.3.2")
        msg = str(cm.exception)
        self.assertIn("src/cli.py", msg, "缺哪个文件要说")
        self.assertIn("命令行入口", msg, "它是干什么的也要说")
        self.assertIn("README.md", msg, "实际收到的东西要列出来")
        self.assertIn("手动升级", msg, "要给出退路 —— 网络问题是修不好的")
        self.assertIn("安装部署指南", msg, "退路要指到具体文档")

    def test_a_good_package_still_passes(self):
        """闸门不能把正常的包也拦下来。"""
        pkg = self._pkg("HappyJoy95-cbg-reconcile-abc1234", {
            "src/cli.py": "# cli", "src/version.py": 'VERSION = "1.3.2"',
            "bootstrap.py": "x", "install.bat": "y",
        })
        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg):
            res = selfupdate.apply_update(self.tmp, current="1.3.1")
        self.assertTrue(res["ok"])
        self.assertEqual(res["to"], "1.3.2")
        self.assertTrue((self.tmp / "src" / "cli.py").is_file())


class TestVersionSourcePriority(unittest.TestCase):
    """版本检查**必须优先走 api.github.com**。

    实测踩到：`raw.githubusercontent.com` 有 ~5 分钟的 CDN 缓存
    （`cache-control: max-age=300`）。刚发的 v1.3.4，它还在返回 v1.3.3 ——
    表现就是"检查更新说没有新版"，而且加时间戳参数没用（CDN 层的缓存不看 query）。

    `api.github.com/.../contents` 拿到的是当前提交，实测是新的。
    """

    def _api_payload(self, version: str, *, newlines=True):
        """造一个 API 响应：content 是 base64，而且 GitHub **带换行**。"""
        import base64 as b64
        text = f'"""x"""\n\nVERSION = "{version}"          # 注释\n'
        enc = b64.b64encode(text.encode("utf-8")).decode()
        if newlines:
            enc = "\n".join(enc[i:i + 60] for i in range(0, len(enc), 60))
        resp = types.SimpleNamespace(status_code=200, text="")
        resp.json = lambda: {"content": enc, "encoding": "base64"}
        return resp

    def _raw_payload(self, version: str):
        return types.SimpleNamespace(
            status_code=200, text=f'VERSION = "{version}"          # 注释\n')

    def test_api_is_tried_first(self):
        """顺序反了就等于没修 —— raw 会继续给旧版本。"""
        tried = []

        def fake_get(url, **kw):
            tried.append(url)
            return self._api_payload("1.3.4")

        with mock.patch.object(selfupdate, "_get", fake_get):
            v = selfupdate.remote_version()
        self.assertEqual(v, "1.3.4")
        self.assertIn("api.github.com", tried[0], "第一个必须问 api，不能是 raw")
        self.assertNotIn("raw.githubusercontent", tried[0])

    def test_decodes_base64_with_newlines(self):
        """GitHub 的 content 是 base64 **带换行** —— 不洗掉就解不出来。"""
        with mock.patch.object(selfupdate, "_get",
                               lambda url, **kw: self._api_payload("9.9.9")):
            self.assertEqual(selfupdate.remote_version(), "9.9.9")

    def test_falls_back_to_raw_when_api_fails(self):
        """api 挂了（限流 / 不通）还能退回 raw —— 只是可能慢一个 CDN 周期。"""
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            if "api.github.com" in url:
                raise selfupdate.UpdateError("连不上 GitHub（试了 1 次）：超时")
            return self._raw_payload("1.3.4")

        with mock.patch.object(selfupdate, "_get", fake_get):
            v = selfupdate.remote_version()
        self.assertEqual(v, "1.3.4")
        self.assertEqual(len(seen), 2, "应该正好试两个源")
        self.assertIn("api.github.com", seen[0])
        self.assertIn("raw.githubusercontent", seen[1])

    def test_api_uses_a_single_try(self):
        """api 不通要**赶紧换 raw**，别在这儿耗掉三次重试。"""
        tries = []

        def fake_get(url, **kw):
            tries.append(kw.get("tries"))
            if "api.github.com" in url:
                raise selfupdate.UpdateError("boom")
            return self._raw_payload("1.0.0")

        with mock.patch.object(selfupdate, "_get", fake_get):
            selfupdate.remote_version()
        self.assertEqual(tries[0], 1, "api 那条路只该试一次")

    def test_both_sources_failing_is_reported_together(self):
        with mock.patch.object(selfupdate, "_get",
                               side_effect=selfupdate.UpdateError("连不上")):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.remote_version()
        msg = str(cm.exception)
        self.assertIn("api.github.com", msg)
        self.assertIn("raw", msg, "两个源都试过了要说出来")

    def test_garbage_content_is_an_error_not_a_wrong_version(self):
        """拿到的内容里没有 VERSION → 报错，**绝不能**猜一个版本号出来。"""
        resp = types.SimpleNamespace(status_code=200, text="")
        resp.json = lambda: {"content": ""}
        with mock.patch.object(selfupdate, "_get", lambda url, **kw: resp):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.remote_version()


class TestHistoryAndRollback(unittest.TestCase):
    """历史版本回退。

    升级出问题时要有退路 —— 尤其自更新这条路本身还在被门店网络折腾的时候。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    # -------------------------------------------------- 版本号从哪来
    def test_only_release_commits_count(self):
        """⚠ 只认 `release: vX.Y.Z`。

        实测踩到：宽松正则会把 `fix(update): 检查更新优先走…（v1.3.4 就这样）`
        这种**正文里提到版本号**的提交也抓进来，列表里于是出现两个 v1.3.4，
        而且都不是它真正的版本 —— 门店照着这种列表回退，等于闭着眼睛选。
        """
        self.assertEqual(selfupdate._version_from_message("release: v1.4.2"), "1.4.2")
        self.assertEqual(selfupdate._version_from_message("Release: V1.4.2"), "1.4.2")
        self.assertEqual(
            selfupdate._version_from_message("release: v1.4.2\n\n两个修复一起出"),
            "1.4.2")
        self.assertEqual(
            selfupdate._version_from_message("fix(update): 检查更新\n\n（v1.3.4 就这样）"),
            "")
        self.assertEqual(selfupdate._version_from_message("chore: 忽略 BUILD.txt"), "")
        self.assertEqual(selfupdate._version_from_message(""), "")

    def test_history_skips_commits_without_a_version(self):
        payload = [
            {"sha": "a" * 40, "commit": {"message": "release: v1.4.2",
                                         "committer": {"date": "2026-09-15T09:58:00Z"}}},
            {"sha": "b" * 40, "commit": {"message": "fix: 一些小修",
                                         "committer": {"date": "2026-09-15T09:57:00Z"}}},
            {"sha": "c" * 40, "commit": {"message": "release: v1.4.1",
                                         "committer": {"date": "2026-09-15T09:44:00Z"}}},
        ]
        with mock.patch.object(selfupdate, "_git_json", lambda url, **k: payload):
            vs = selfupdate.history()
        self.assertEqual([v["version"] for v in vs], ["1.4.2", "1.4.1"])
        self.assertEqual(vs[0]["short"], "a" * 7)
        self.assertIn("2026-09-15", vs[0]["date"])

    def test_history_reports_a_bad_response(self):
        """限流 / 返回非列表 → 报错，别让界面拿到半个列表。"""
        with mock.patch.object(selfupdate, "_git_json",
                               lambda url, **k: {"message": "API rate limit exceeded"}):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.history()
        self.assertIn("历史版本", str(cm.exception))

    # ------------------------------------------------------------ 回退
    def test_rollback_refuses_a_non_sha(self):
        """`ref` 直接拼进 URL —— 先卡住明显不对的东西。"""
        for bad in ("", "main", "../../etc", "v1.4.2", "abc"):
            with self.assertRaises(selfupdate.UpdateError):
                selfupdate.rollback(self.root, ref=bad)

    def test_rollback_passes_the_ref_through(self):
        seen = {}

        def fake_apply(root, *, current="", ref=None):
            seen["ref"] = ref
            return {"ok": True, "to": "1.3.6"}

        with mock.patch.object(selfupdate, "apply_update", fake_apply):
            res = selfupdate.rollback(self.root, ref="ddfb39d" + "0" * 33,
                                      current="1.4.2")
        self.assertTrue(res["ok"])
        self.assertTrue(seen["ref"].startswith("ddfb39d"))

    def test_rollback_only_touches_code(self):
        """回退走的是同一条铺代码的路 —— 数据目录一样不许碰。"""
        pkg = Path(tempfile.mkdtemp()) / "cbg-old"
        for rel in ("src/cli.py", "src/version.py", "bootstrap.py", "install.bat"):
            f = pkg / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('VERSION = "1.3.0"' if rel.endswith("version.py") else "x",
                         encoding="utf-8")
        # 门店自己的东西
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "config" / "store-SCN231409.yaml").write_text("marker: C",
                                                                  encoding="utf-8")
        (self.root / ".secrets").mkdir(exist_ok=True)
        (self.root / ".secrets" / "erp.env").write_text("ERP_USERNAME=me",
                                                        encoding="utf-8")
        (self.root / "out").mkdir(exist_ok=True)
        (self.root / "out" / "差异.xlsx").write_text("报告", encoding="utf-8")

        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg):
            res = selfupdate.apply_update(self.root, current="1.4.2",
                                          ref="ddfb39d" + "0" * 33)
        self.assertTrue(res["ok"])
        self.assertTrue((self.root / "src" / "cli.py").is_file(), "代码要铺过去")
        # ⚠ 数据一个都不许动
        self.assertEqual((self.root / "config" / "store-SCN231409.yaml")
                         .read_text(encoding="utf-8"), "marker: C")
        self.assertEqual((self.root / ".secrets" / "erp.env")
                         .read_text(encoding="utf-8"), "ERP_USERNAME=me")
        self.assertTrue((self.root / "out" / "差异.xlsx").is_file())


class TestTargetsInconsistency(unittest.TestCase):
    """`_targets()` 漏掉**真实存在**的文件时，不能因此拒绝一个好好的包。

    门店实测（Windows）：同一个 `zip_root`，`is_file()` 和 `rglob` 都说
    `src/cli.py` 在，`_targets()` 却没带上它 —— 于是更新报
    "这不像我们的包"，而包其实是好的。门店照着提示去手工换包，白跑一趟。

    判断标准应该是**文件在不在**，而不是"某个遍历函数有没有带上它"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir()

    def _pkg(self, files=("src/cli.py", "src/version.py", "bootstrap.py")):
        pkg = Path(tempfile.mkdtemp()) / "HappyJoy95-cbg-reconcile-abc1234"
        for rel in files:
            f = pkg / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('VERSION = "1.4.4"' if rel.endswith("version.py") else "x",
                         encoding="utf-8")
        return pkg

    def test_a_missing_from_targets_but_present_on_disk_is_not_fatal(self):
        real = selfupdate._targets

        def drops_anchor(zip_root):
            return [(f, r) for f, r in real(zip_root) if str(r) != "src/cli.py"]

        pkg = self._pkg()
        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg), \
                mock.patch.object(selfupdate, "_targets", drops_anchor):
            res = selfupdate.apply_update(self.root, current="1.4.2")
        self.assertTrue(res["ok"], "包是好的，不该被拒")
        self.assertTrue((self.root / "src" / "cli.py").is_file(),
                        "漏掉的文件要补进去，否则铺过去的代码是残的")

    def test_a_genuinely_missing_anchor_is_still_rejected(self):
        """闸门不能拆 —— 真不是我们的包，必须拒绝。"""
        bad = Path(tempfile.mkdtemp()) / "别的仓库"
        (bad / "docs").mkdir(parents=True)
        (bad / "README.md").write_text("# 不是我们的", encoding="utf-8")
        with mock.patch.object(selfupdate, "download", lambda *a, **k: bad):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.apply_update(self.root, current="1.4.2")
        self.assertIn("src/cli.py", str(cm.exception))

    def test_the_inconsistency_is_reported_loudly(self):
        """这种不一致很罕见，得留个脚印 —— 不然它永远是笔糊涂账。"""
        real = selfupdate._targets

        def drops_anchor(zip_root):
            return [(f, r) for f, r in real(zip_root) if str(r) != "src/cli.py"]

        pkg = self._pkg()
        buf = io.StringIO()
        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg), \
                mock.patch.object(selfupdate, "_targets", drops_anchor), \
                contextlib.redirect_stdout(buf):
            selfupdate.apply_update(self.root, current="1.4.2")
        out = buf.getvalue()
        self.assertIn("内部不一致", out)
        self.assertIn("src/cli.py", out)


class TestIterFiles(unittest.TestCase):
    """列包里的文件。**用 os.walk，不用 rglob。**

    门店 Windows 上反复撞到：同一个 `zip_root`，`is_file()` 说 `src/cli.py` 在、
    `rglob("cli.py")` 也能搜到，可 `rglob("*")` 遍历出来就是没有它 ——
    更新于是被误判成"这不像我们的包"。本地怎么都复现不出来。

    `os.walk` 走另一套目录遍历（scandir），行为更朴素；这里也不需要 glob
    的花哨功能，只要"把文件列全"。
    """

    def _pkg(self, files):
        root = Path(tempfile.mkdtemp()) / "pkg"
        for rel in files:
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        return root

    def test_lists_everything_including_nested(self):
        root = self._pkg(["src/cli.py", "src/web.py", "web/app.js",
                          "config/stores.yaml", "a/b/c/deep.py"])
        got = {str(p.relative_to(root)) for p in selfupdate._iter_files(root)}
        for want in ("src/cli.py", "src/web.py", "web/app.js",
                     "config/stores.yaml", "a/b/c/deep.py"):
            self.assertIn(want, got)

    def test_skips_pycache(self):
        root = self._pkg(["src/cli.py", "src/__pycache__/cli.cpython-314.pyc"])
        got = {str(p.relative_to(root)) for p in selfupdate._iter_files(root)}
        self.assertIn("src/cli.py", got)
        self.assertFalse([g for g in got if "__pycache__" in g], "缓存不该带出去")

    def test_agrees_with_rglob_on_a_normal_package(self):
        """正常情况下两者必须一致 —— 换了实现不能改变结果。"""
        root = self._pkg(["src/cli.py", "bootstrap.py", "web/app.js",
                          "config/stores.yaml"])
        mine = {str(p.relative_to(root)) for p in selfupdate._iter_files(root)}
        theirs = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
        self.assertEqual(mine, theirs)

    def test_targets_still_finds_the_anchors(self):
        root = self._pkg(["src/cli.py", "bootstrap.py", "install.bat",
                          "config/stores.yaml", "config/store-X.yaml", "out/x.log"])
        rels = {str(r) for _, r in selfupdate._targets(root)}
        self.assertIn("src/cli.py", rels)
        self.assertIn("bootstrap.py", rels)
        self.assertIn("install.bat", rels)
        self.assertIn("config/stores.yaml", rels, "文件级例外要留着")
        self.assertNotIn("config/store-X.yaml", rels, "门店配置不许动")
        self.assertNotIn("out/x.log", rels)


class TestRelKey(unittest.TestCase):
    """相对路径的**比较键**必须与平台无关。

    门店 Windows 上踩到的真事：锚点检查是
    `str(rel) not in ("src/cli.py", "bootstrap.py")` —— 而 Windows 上
    `str(PureWindowsPath("src/cli.py"))` 是 **`src\\cli.py`（反斜杠）**，
    跟常量里的正斜杠**永远比不相等**。

    于是文件明明铺进去了，还是被判"缺少 src/cli.py"，更新被拒（门店连着卡了三次）。
    同一个坑还让 `ALLOW_EVEN_IF_NEVER` 失效 —— 那是 `config/stores.yaml` 的
    文件级例外，它失效就意味着**门店映射表永远更新不下去**（加了新店也带不到门店）。
    """

    def test_windows_backslashes_are_normalised(self):
        self.assertEqual(selfupdate._rel_key("src\\cli.py"), "src/cli.py")

    def test_posix_separators_are_left_alone(self):
        self.assertEqual(selfupdate._rel_key("src/cli.py"), "src/cli.py")

    def test_real_path_objects_are_accepted(self):
        self.assertEqual(selfupdate._rel_key(Path("src") / "cli.py"), "src/cli.py")

    def test_the_platform_actually_differs(self):
        """证明这个测试有意义：Windows 的 `str(Path)` 确实带反斜杠。

        在 POSIX 上跑不出反斜杠，所以用 `PureWindowsPath` 把 Windows 的行为
        显式演出来 —— 否则这条 bug 在开发机上永远是"看不见"的。
        """
        from pathlib import PureWindowsPath
        win = str(PureWindowsPath("src") / "cli.py")
        self.assertEqual(win, "src\\cli.py")
        self.assertNotEqual(win, "src/cli.py", "不规范化就永远比不相等")

    def test_targets_keys_are_normalised(self):
        """`_targets` 给出的键也得是规范化的 —— 调用方拿它去比对锚点。"""
        root = Path(tempfile.mkdtemp()) / "pkg"
        for rel in ("src/cli.py", "bootstrap.py", "config/stores.yaml",
                    "config/store-X.yaml"):
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x", encoding="utf-8")
        keys = {selfupdate._rel_key(r) for _, r in selfupdate._targets(root)}
        self.assertIn("src/cli.py", keys)
        self.assertIn("bootstrap.py", keys)
        self.assertIn("config/stores.yaml", keys, "文件级例外要生效")
        self.assertNotIn("config/store-X.yaml", keys, "门店配置不许动")

    def test_describe_package_uses_the_same_key(self):
        """诊断里的"打架判断"也得用规范化键，否则在 Windows 上永远不会触发。"""
        src = Path(selfupdate.__file__).read_text(encoding="utf-8")
        block = src.split("def _describe_package")[1].split("\ndef ")[0]
        self.assertIn("_rel_key", block,
                      "_describe_package 里还在用 str(r) —— Windows 上比不出来")
        self.assertNotIn("{str(r) for _, r in pairs}", block)

    def test_the_whole_anchor_check_works_with_windows_paths(self):
        """⚠ 端到端：拿**真的** `PureWindowsPath` 走一遍完整判断。

        门店那三次失败就发生在这一串比较里。这里把 Windows 的分隔符原样演出来，
        证明修完之后：锚点能认出来、`ALLOW_EVEN_IF_NEVER` 也生效。
        """
        from pathlib import PureWindowsPath
        root = PureWindowsPath(r"C:\tmp\HappyJoy95-cbg-reconcile-9687a77")
        rels = [PureWindowsPath(p).relative_to(root) for p in (
            r"C:\tmp\HappyJoy95-cbg-reconcile-9687a77\src\cli.py",
            r"C:\tmp\HappyJoy95-cbg-reconcile-9687a77\bootstrap.py",
            r"C:\tmp\HappyJoy95-cbg-reconcile-9687a77\config\stores.yaml",
        )]

        # Windows 上原始的 str() 是什么样 —— 先确认这个前提
        self.assertEqual(str(rels[0]), "src\\cli.py")
        self.assertNotEqual(str(rels[0]), "src/cli.py", "这就是 bug 的根源")

        keys = {selfupdate._rel_key(r) for r in rels}
        have = keys
        missing = [a for a, _ in selfupdate.ANCHORS if a not in have]
        self.assertEqual(missing, [], "锚点必须能认出来，否则更新永远被拒")

        # 文件级例外也要生效，否则门店映射表永远更新不下去
        self.assertIn("config/stores.yaml", keys)
        self.assertIn("config/stores.yaml", selfupdate.ALLOW_EVEN_IF_NEVER)
        for r in rels:
            if r.parts[0] == "config":
                self.assertIn(selfupdate._rel_key(r), selfupdate.ALLOW_EVEN_IF_NEVER,
                              "config/stores.yaml 要被放行")

    def test_upgrade_survives_windows_style_relative_paths(self):
        """端到端：`apply_update` 在 Windows 上不会因为分隔符而误判。

        直接喂 `PureWindowsPath`，让"Windows 的 `relative_to` 给反斜杠"
        这件事被完整地走一遍 —— 而不是只测那个转换函数。
        """
        from pathlib import PureWindowsPath

        class WinPkg:
            """只实现 `_targets` 用到的那几个方法，行为照 Windows 来。"""
            def __init__(self, root):
                self.root = PureWindowsPath(root)

            def __truediv__(self, other):
                return self.root / other

            def is_file(self):
                return True

            def relative_to(self, other):
                base = other.root if isinstance(other, WinPkg) else PureWindowsPath(other)
                return self.root.relative_to(base)

        zip_root = WinPkg(r"C:\tmp\HappyJoy95-cbg-reconcile-abc1234")
        names = ["src/cli.py", "src/version.py", "bootstrap.py",
                 "config/stores.yaml", "config/store-SCN231409.yaml",
                 "install.bat", "web/app.js"]
        fake = [WinPkg(rf"C:\tmp\HappyJoy95-cbg-reconcile-abc1234\{n.replace('/', chr(92))}")
                for n in names]

        def fake_iter(_root):
            return fake

        real_iter = selfupdate._iter_files
        pairs = None
        with mock.patch.object(selfupdate, "_iter_files", fake_iter):
            pairs = selfupdate._targets(zip_root)

        keys = [_rel for _, _rel in pairs]
        as_text = [selfupdate._rel_key(r) for r in keys]

        self.assertIn("src/cli.py", as_text)
        self.assertIn("bootstrap.py", as_text)
        self.assertIn("config/stores.yaml", as_text, "文件级例外要生效")
        self.assertNotIn("config/store-SCN231409.yaml", as_text, "门店配置不许动")

        # 锚点检查（apply_update 里那一句）
        have = set(as_text)
        missing = [a for a, _ in selfupdate.ANCHORS if a not in have]
        self.assertEqual(missing, [], "Windows 路径下锚点也得认得出")

        # 而且每个 rel 自己就是 Windows 风格 —— 确保这个测试真的在演 Windows
        self.assertIn("\\", str(keys[0]), "这个测试没演成 Windows 就白做了")
        selfupdate._iter_files = real_iter


class TestNetworkRetry(unittest.TestCase):
    """⚠ 实测（走代理的网络）：TLS 握手会被**间歇性**掐断，
    报 `SSLEOFError: EOF occurred in violation of protocol`，重试一次就好。

    门店电脑挂代理 / 防火墙也是这个形状。不重试的话"检查更新"会时好时坏，
    用户完全摸不着规律 —— 只会觉得"这功能坏了"。
    """

    def _fake_requests(self, results):
        """results: 每次调用返回异常实例还是假响应。"""
        calls = {"n": 0}

        class FakeResp:
            status_code = 200
            text = 'VERSION = "9.9.9"\n'

            def raise_for_status(self):
                pass

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **kw):
                i = calls["n"]
                calls["n"] += 1
                r = results[min(i, len(results) - 1)]
                if isinstance(r, Exception):
                    raise r
                return FakeResp()

        import types as _t
        fake = _t.ModuleType("requests")
        fake.RequestException = Exception
        fake.Session = FakeSession
        return fake, calls

    def test_retries_then_succeeds(self):
        import requests as real
        fake, calls = self._fake_requests(
            [real.exceptions.SSLError("EOF occurred in violation of protocol"), None])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            v = selfupdate.remote_version()
        self.assertEqual(v, "9.9.9")
        self.assertEqual(calls["n"], 2, "第一次失败后没有重试")

    def test_gives_up_after_tries(self):
        """两个源都试过、都放弃之后才报错。

        ⚠ 现在是**两个源**（api 优先、raw 退路），所以总尝试次数是
        api 的 1 次 + raw 的 TRIES 次 —— 不再是单纯的 TRIES。
        """
        import requests as real
        fake, calls = self._fake_requests([real.exceptions.SSLError("boom")])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.remote_version()
        self.assertEqual(calls["n"], 1 + selfupdate.TRIES)
        self.assertIn("试了", str(cm.exception))

    def test_each_attempt_uses_a_fresh_session(self):
        """坏连接别复用 —— 每次都要新开 Session。"""
        import inspect
        src = inspect.getsource(selfupdate._get)
        self.assertIn("Session()", src)
        self.assertIn("with requests.Session()", src)


class TestRestartSnippet(unittest.TestCase):
    def test_snippet_is_valid_python(self):
        """重启助手是拼出来的一段源码 —— 语法错了就永远起不来。"""
        import ast
        ast.parse(selfupdate._SNIPPET)

    def test_snippet_waits_for_us_to_die_first(self):
        """必须先等我们退干净再拉服务 —— 否则端口还占着，新进程起不来。"""
        self.assertIn("tasklist", selfupdate._SNIPPET)
        self.assertIn("os.kill", selfupdate._SNIPPET)

    def test_snippet_detaches(self):
        """新服务要脱离助手进程 —— 助手退出不能把服务带走。"""
        self.assertIn("DETACHED", selfupdate._SNIPPET)
        self.assertIn("start_new_session", selfupdate._SNIPPET)


if __name__ == "__main__":
    unittest.main()
