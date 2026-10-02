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
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import selfupdate

#: 真的项目根 —— 给"冒烟命令本身对不对"那条测试用（假安装目录 import 不起来）
ROOT = Path(__file__).resolve().parent.parent


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


class TestBetaCanUpgradeToRelease(unittest.TestCase):
    """⭐ 2.1.1 的主线：**beta 包能升回同号的正式版**。

    用户 2026-09-18：「还有个设定，beta可以升级到正式版，做到2.1.1吧」。

    **现象（真卡住了）**：装过 `2.1.0-beta3` 的机器 `VERSION` **也是 2.1.0**
    （beta 只是"这一版正在测"的标记），正式版推上去之后
    `2.1.0 > 2.1.0` 不成立 ⇒ **那台机器永远看不到「有新版本」**，
    再也不会自己升上来。

    判据只能是 `BUILD.txt` 里的 beta 标记 —— 版本号同号，从号上分不出来。
    """

    def test_本地是beta_远端同号_算有更新(self):
        self.assertTrue(selfupdate.has_update("2.1.0", "2.1.0", beta=True))

    def test_本地是正式包_远端同号_不算(self):
        """⚠ 原本就对，**别改坏** —— 这条错了会让所有正常门店
        反复收到"有新版本"，点更新又是同一版。"""
        self.assertFalse(selfupdate.has_update("2.1.0", "2.1.0", beta=False))

    def test_远端更高两种本地都算(self):
        self.assertTrue(selfupdate.has_update("2.1.1", "2.1.0", beta=False))
        self.assertTrue(selfupdate.has_update("2.1.1", "2.1.0", beta=True))

    def test_远端更低_不许提示降级(self):
        """⚠ 本地在测**更高版本**的 beta（比如 2.2.0）时，
        不能反过来提示"降级到 2.1.1" —— 所以判据是 `==` 不是 `>=`。"""
        self.assertFalse(selfupdate.has_update("2.1.1", "2.2.0", beta=True))

    def test_不给_beta_参数时读本机_BUILD_txt(self):
        """⚠ 界面那条路（`cached` → `_recompute`）**不传 beta** ——
        它得靠 `version.is_beta()` 自己读 `BUILD.txt`。
        这条钉住"默认值真的读了本机状态"，不然两条路会一个说有一个说没有。
        """
        from src import version as V
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "BUILD.txt"
            with mock.patch.object(V, "BUILD_FILE", bf):
                bf.write_text("beta3 · 2026-09-18 10:20", encoding="utf-8")
                self.assertTrue(selfupdate.has_update("2.1.1", "2.1.1"))
                self.assertFalse(selfupdate.is_newer("2.1.1", "2.1.1"),
                                 "前提：光比版本号是「不算」的")
                bf.write_text("2026-09-18 11:00", encoding="utf-8")
                self.assertFalse(selfupdate.has_update("2.1.1", "2.1.1"))

    def test_两条路用同一个判据(self):
        """⚠⚠ `check()` 是**后台每天查一次**的，`_recompute()` 是
        **界面每次刷新读的**（`cached()`）。只改一条的话，
        后台说"有更新"、界面照样显示"已是最新" ——
        而那正是门店唯一看得到的地方。两条都得走 `has_update()`。
        """
        from src import version as V
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "BUILD.txt"
            bf.write_text("beta1 · 2026-09-18 10:20", encoding="utf-8")
            with mock.patch.object(V, "BUILD_FILE", bf):
                out = selfupdate._recompute({"latest": "2.1.1", "at": 1}, "2.1.1")
                self.assertTrue(out["has_update"], "_recompute 没走新判据（界面会说已是最新）")

    def test_更新装完就不再提示(self):
        """⚠ 这是"不会反复提示"的保证：`apply_update` 装完把 `BUILD.txt`
        重写成 `GitHub main · v2.1.1`（**不含 beta**）⇒ 判据回到「远端 > 本地」。

        不成立的话门店会陷入"更新完还说有新版本、点了又是同一版"的死循环。
        """
        from src import version as V
        with tempfile.TemporaryDirectory() as d:
            bf = Path(d) / "BUILD.txt"
            bf.write_text("GitHub main · v2.1.1", encoding="utf-8")
            with mock.patch.object(V, "BUILD_FILE", bf):
                self.assertFalse(V.is_beta(), "自更新写的那行不该被判成 beta")
                self.assertFalse(selfupdate.has_update("2.1.1", "2.1.1"))


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

        ⚠ `tests/` **故意不铺**（2026-09-22 方案 2，用户）：门店不跑 pytest；
        手工包也 `--exclude 'tests/'`，两条路径一致。
        """
        for rel in ("src/cli.py", "web/app.js", "bootstrap.py",
                    "install.bat", "运维手册.md", "tests/test_x.py",
                    "run_check.py", ".gitattributes"):
            self._mk(rel)
        got = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertEqual(got, {
            "src/cli.py", "web/app.js", "bootstrap.py",
            "install.bat", "运维手册.md",
            "run_check.py", ".gitattributes",
        }, "路径被改过了 —— 那就不再是'跟着仓库走'")
        self.assertNotIn("tests/test_x.py", got,
                         "门店包/自更新都不该下发 tests/")

    def test_skip_apply_skips_tests(self):
        """`SKIP_APPLY` 里的顶层目录 zip 里有也不铺。"""
        self.assertIn("tests", selfupdate.SKIP_APPLY)
        self._mk("tests/test_selfupdate.py", "x")
        self._mk("src/cli.py", "x")
        got = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertNotIn("tests/test_selfupdate.py", got)
        self.assertIn("src/cli.py", got)

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

    def test_packaging_script_never_ships_the_workspace_dir(self):
        """打包脚本必须排除 `.dsh/` —— 那是工作区隔离区。

        ⚠ **真出过事**：`rsync` 的排除列表里一直没有 `.dsh/`，只是因为那阵子
        里面恰好没东西，所以谁都没发现。有一天本机 venv 建在了
        `.dsh/tasks/` 下，整个 venv 被塞进包里，最后是
        「包里不能有本机绝对路径」那条自检把它拦下来的。

        `.dsh/` 里有记忆日志、备份、临时任务、本机 venv —— 跟 `.secrets/`
        一样是"这台电脑自己的东西"，发给门店既没用也不合适。

        这里同时钉住**两处**：脚本里的 rsync 排除项，和 `NEVER_TOUCH`。
        两处对不上就是下次踩坑的开始。

        ⚠ **打包脚本本身不进包**（`rsync` 排除 `tools/`），而门店那边 `selftest`
        会跑测试 —— 所以装出来的包里要**跳过**这条，否则门店每次自检都红一条，
        而它红得毫无意义（那条脚本根本不在那台电脑上）。
        ⚠ 只在 `tools/` **整个目录都没有**时跳过。目录在、脚本不在 = 有人挪走了它，
        那是真问题，**要让它炸**，别用"文件不存在就跳过"把真问题一起吞掉。
        """
        tools = Path(__file__).resolve().parent.parent / "tools"
        if not tools.is_dir():
            self.skipTest("装出来的包里没有 tools/（打包脚本不进包）—— 这条只在仓库里跑")
        script = (tools / "build_package.sh").read_text(encoding="utf-8")
        self.assertIn("--exclude '.dsh/'", script, "打包脚本没排除 .dsh/")
        self.assertIn('check_absent "${STAGE}/.dsh"', script,
                      "还缺一条自检 —— 排除项哪天被删了没人会发现")
        self.assertIn(".dsh", selfupdate.NEVER_TOUCH,
                      "自更新也该明说不碰 .dsh/")

    def test_agent_md_is_blocked_from_both_paths(self):
        """`agent.md` 必须同时挡在**两条路**外面：正式包 + 自更新。

        ⚠ 它跟 `AGENTS.md` **不撞名** —— `AGENTS` 是六个字母（`agents.md`），
        别以为改个大小写就是同一个文件。

        为什么单独挡它，而 README / AGENTS / 运维手册 仍然照铺（那是 AGENTS.md
        里"有意留着"的决定）：那三本是**查资料**用的，翻到也就翻到了；
        这本是**叫人动手**的（备份、改代码、跑测试），出现在门店目录里性质不一样。

        和 `.dsh/` 那条一样，同时钉住**两处**：打包脚本（排除项 + 自检断言）
        和 `NEVER_TOUCH`。⚠ 这次还发现自检断言**漏了 `AGENTS.md`** ——
        rsync 排除了它，但反查没查，而 AGENTS.md 正文里写着"两道"。
        文档承诺了、代码没做，所以一并补上。
        """
        tools = Path(__file__).resolve().parent.parent / "tools"
        if not tools.is_dir():
            self.skipTest("装出来的包里没有 tools/（打包脚本不进包）—— 这条只在仓库里跑")
        script = (tools / "build_package.sh").read_text(encoding="utf-8")

        self.assertIn("--exclude 'agent.md'", script, "打包脚本没排除 agent.md")
        self.assertIn(
            "for _doc in README.md 设计文档.md 运维手册.md AGENTS.md agent.md;",
            script,
            "自检断言的名单和 --exclude 对不上 —— 排除项哪天被删了没人会发现")
        self.assertIn("agent.md", selfupdate.NEVER_TOUCH,
                      "自更新也该不碰 agent.md")

    def test_never_touch_accepts_a_top_level_file(self):
        """光把 `agent.md` 写进黑名单不够 —— 要验它**真的**被拦下来。

        `NEVER_TOUCH` 的判定是 `rel.parts[0] in NEVER_TOUCH`，对**文件**同样成立
        （顶层文件的 `parts[0]` 就是它自己的名字）。这条钉住这个前提：
        哪天有人把判定改成"只认目录"，agent.md 会**静默地**重新开始往门店铺。
        """
        self._mk("agent.md")
        self._mk("src/cli.py")
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertNotIn("agent.md", targets, "agent.md 还是被铺下去了")
        self.assertIn("src/cli.py", targets, "别把不相干的文件也拦了")

    def test_发布说明跟仓库走(self):
        """`发布说明.md` 2026-09-23 入 git（2.2.1 修的）—— zipball 里有它，
        `_targets` 必须放行：根目录文件不在 `NEVER_TOUCH`/`SKIP_APPLY` 里。
        谁哪天顺手把它加进黑名单，门店的发布说明就**静默地**停回装包那一版
        —— 正是这次要修的病，钉住别退回去。
        """
        self._mk("发布说明.md")
        self._mk("src/cli.py")
        targets = {str(rel) for _, rel in selfupdate._targets(self.zip_root)}
        self.assertIn("发布说明.md", targets,
                      "发布说明没被铺下去 —— 门店又收不到新说明了")
        self.assertIn("src/cli.py", targets, "别把不相干的文件也拦了")

    def test_发布说明正文住在仓库里_打包只拷贝(self):
        """⚠ 2026-09-23（用户选进 2.2.1）：正文从打包脚本的 heredoc 挪进 git。

        病灶：正文嵌在 `build_package.sh` 里生成，而**打包脚本不下发门店**
        ⇒ 走自更新升级的机器，`发布说明.md` 永远停在当初拷包那一版
        （AGENTS「仓库 ≠ 包内容」那节原来就记着这个差）。
        入了 git 之后：zipball 带它 ⇒ `_targets` 对根目录文件照原样铺 ⇒ 自更新送到。

        这条钉两头：① 仓库根有 `发布说明.md`；② 脚本里**不再有**正文 heredoc、
        只 `cp`。顺带的好处：正文里的反斜杠转义坑随 f-string 一起消失
        （原名 `test_release_notes_python_has_no_invalid_escapes` ——
        病没了，测试就改钉新形状；"病没了"这件事本身也要有测试看着）。
        """
        root = Path(__file__).resolve().parent.parent
        tools = root / "tools"
        if not tools.is_dir():
            self.skipTest("装出来的包里没有 tools/")
        self.assertTrue(
            (root / "发布说明.md").is_file(),
            "发布说明.md 不在仓库根 —— 自更新就又拿不到新说明了")
        script = (tools / "build_package.sh").read_text(encoding="utf-8")
        self.assertNotIn("<<'RELNOTES'", script,
                         "发布说明正文又回到打包脚本里了 —— 门店会重新收不到更新")
        self.assertIn('cp "${ROOT}/发布说明.md" "${STAGE}/发布说明.md"', script,
                      "打包脚本没把仓库的 发布说明.md 拷进包（自检会缺文件）")

    def test_beta_包名带编号且指纹里留着_beta(self):
        """⚠ 用户 2026-09-17 定的：beta 包要编号（`beta0` / `beta1`…），
        别再同一天打第二个就把第一个盖掉。

        三件事一起钉：

        1. **包名带编号** —— `-beta<N>-` 进 ZIPNAME；
        2. **从 beta0 开始** —— `_max` 初值必须是 **-1**：一个都没有时下一个是
           `beta0`。写成 0 的话第一包会变成 `beta1`，序列里就没有 beta0 了
           （用户原话：「上来是 beta0，beta1 一直往后」）；
        3. **指纹里必须留着 "beta" 这几个字母** —— `dbmigrate` 那道
           「beta 包不许动门店的库」的门槛靠的就是这个子串。
           哪天有人把标记改成纯数字（`1 · 2026-09-17 18:49`），
           门槛会**静默失效**：拿 beta 包去门店测一下，
           **门店那个 77MB 的库就被改名归档了**（这事真发生过一次，
           见 `dbmigrate.only_in_release` 的注释）。
        """
        tools = Path(__file__).resolve().parent.parent / "tools"
        if not tools.is_dir():
            self.skipTest("装出来的包里没有 tools/（打包脚本不进包）—— 这条只在仓库里跑")
        script = (tools / "build_package.sh").read_text(encoding="utf-8")

        self.assertIn('BETA_LABEL="beta${BETA_N}"', script,
                      "beta 标记不再以 'beta' 开头了 —— dbmigrate 的门槛会失效")
        self.assertIn('SUFFIX="-${BETA_LABEL}"', script,
                      "包名里没接上编号")
        # 指纹必须用带编号的那个标签，不能退回去写死 'beta'
        self.assertIn('printf \'%s · %s\\n\' "${BETA_LABEL}" "${BUILD_STAMP}"', script,
                      "BUILD.txt 没写带编号的标记")
        self.assertNotIn("'beta · %s\\n'", script,
                         "BUILD.txt 又写回不带编号的 'beta' 了")
        # 自动编号 + 手写编号两条路都要通
        self.assertIn('1|true|yes|beta|BETA) BETA_N="auto"', script)
        self.assertIn('beta[0-9]|beta[0-9][0-9]', script)
        # ⚠ 从 beta0 起：初值 -1（见上面第 2 条）
        self.assertIn("_max=-1", script,
                      "自动编号的初值不是 -1 了 —— 第一包会从 beta1 起，没有 beta0")
        self.assertNotIn("_max=0", script,
                         "又把初值写回 0 了 —— 序列会跳过 beta0")

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


class _ApplyCase(unittest.TestCase):
    """升级类测试的公共脚手架（`TestApply` / `TestUpdateJournal` 共用）。"""

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

    def _apply(self, files, anchors=True, keep_staging=False, smoke=False):
        """anchors=False 用来模拟"下载下来的根本不是我们的包"。

        `keep_staging=True` 用于**失败**的用例：`apply_update` 会故意保留解压目录
        （重跑要用），这里就不替它删 —— 免得测试自己把要验的东西删掉。

        `smoke=False` 是**默认**：临时目录里的 `src/cli.py` 内容就是"新代码"三个字，
        根本 import 不起来。冒烟那一套自己有测试（`TestSmokeAndRollback`）。
        """
        if anchors:
            files = {"bootstrap.py": "x", "src/cli.py": "旧代码", **files}
        blob = _fake_zip("cbg-reconcile-main", files)
        with mock.patch.object(selfupdate, "download") as dl:
            d = Path(tempfile.mkdtemp())
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                z.extractall(d)
            dl.return_value = d / "cbg-reconcile-main"
            if keep_staging:
                self.addCleanup(shutil.rmtree, d, ignore_errors=True)
                return selfupdate.apply_update(self.root, current="1.2.0", smoke=smoke)
            try:
                return selfupdate.apply_update(self.root, current="1.2.0", smoke=smoke)
            finally:
                shutil.rmtree(d, ignore_errors=True)

    def _boom(self, after=0):
        """造一个「铺完前 `after` 个文件就炸」的 `_write_file`（几个升级类共用）。

        ⚠ 别在子类里重定义它 —— 会出现"看起来只有一份、其实后面那份把前面那份盖了"
        的情况（同名方法后者胜，且没有任何提示）。
        """
        state = {"calls": 0}

        def fake(src, dst):
            if state["calls"] >= after:
                raise OSError("磁盘满了")
            state["calls"] += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

        return fake

class TestApply(_ApplyCase):
    """把 zip 铺到安装目录（升级 / 回退走的是同一条路）。"""

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
        """从发行仓更新过来的没有打包时间戳 —— 写一个，界面才显示得出是哪一版。"""
        self._apply({"src/version.py": 'VERSION = "1.3.0"\n'})
        stamp = (self.root / "BUILD.txt").read_text(encoding="utf-8")
        self.assertIn("1.3.0", stamp)
        self.assertIn("Release", stamp)

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


class TestUpdateJournal(_ApplyCase):
    """升级现场：journal + 快照（阶段 1.4a）。

    ⚠ 这一组盯的是「失败时**有人知道**、而且**退得回去**」——
    在这之前，`apply_update` 改到一半被打断是**完全静默**的：
    下一次启动 `upgrade.record()` 只比版本号，于是程序在混合版本上照跑。
    """

    def test_动第一个文件之前现场就写好了(self):
        seen = {}

        def boom(src, dst):
            seen["journal"] = selfupdate.read_journal(self.root)
            raise OSError("磁盘满了")

        with mock.patch.object(selfupdate, "_write_file", boom):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        self.assertEqual(seen["journal"].get("state"), "applying",
                         "铺文件之前没写 journal —— 中断了没人知道")
        self.assertIn("src/cli.py", seen["journal"]["plan"]["update"])

    def test_快照存的是改之前的内容(self):
        res = self._apply({"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'})
        backup = Path(res["backup"])
        self.assertTrue(backup.is_dir())
        self.assertEqual((backup / "src" / "cli.py").read_text(encoding="utf-8"),
                         "旧代码", "备份里不是改之前的内容")
        self.assertGreaterEqual(res["saved"], 1)
        self.assertFalse((backup / "bootstrap.py").exists(),
                         "这次是新加的文件，没什么可备份的")

    def test_成功之后现场是_done(self):
        self._apply({"src/version.py": 'VERSION = "1.3.0"\n'})
        j = selfupdate.read_journal(self.root)
        self.assertEqual(j["state"], "done")
        self.assertEqual(j["to"], "1.3.0")
        self.assertEqual(selfupdate.pending(self.root), {},
                         "走完了还被当成「没走完」，界面上会一直挂横幅")

    def test_失败抛的是_PartialUpdate_并且带着现场(self):
        with mock.patch.object(selfupdate, "_write_file", self._boom()):
            with self.assertRaises(selfupdate.PartialUpdate) as cm:
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        res = cm.exception.result
        self.assertIn("铺到一半", str(cm.exception))
        self.assertTrue(Path(res["backup"]).is_dir(), "失败后没有退路")
        self.assertTrue(Path(res["journal"]).is_file())
        self.assertEqual(res["changed"], [], "第一个文件就炸了，不该有「已改完」的")

    def test_失败之后_pending_报得出来(self):
        with mock.patch.object(selfupdate, "_write_file", self._boom()):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        j = selfupdate.pending(self.root)
        self.assertEqual(j.get("state"), "failed")
        self.assertIn("磁盘满了", j.get("error", ""))

    def test_失败时保留解压目录(self):
        """⚠ 重跑要用它 —— 老代码在 `finally` 里**无条件**删掉，于是中断之后
        既不能重跑，也没有现场可查。"""
        with mock.patch.object(selfupdate, "_write_file", self._boom()):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        staging = selfupdate.read_journal(self.root).get("staging", "")
        self.assertTrue(staging and Path(staging).is_dir(),
                        "解压目录被删了：重跑就得重新下载，离线时等于没救")

    def test_还没动过文件就不留现场(self):
        """锚点不对（根本不是我们的包）⇒ 一个文件都没动 ⇒ 不该留下 journal。"""
        with self.assertRaises(selfupdate.UpdateError):
            self._apply({"src/x.py": "y"}, anchors=False)
        self.assertEqual(selfupdate.read_journal(self.root), {})
        self.assertEqual(selfupdate.pending(self.root), {})

    def test_重跑一遍是幂等的(self):
        files = {"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'}
        self._apply(files)
        again = self._apply(files)
        self.assertEqual(again["changed"], [])
        self.assertEqual(again["added"], [])
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")

    def test_只留最近两份现场(self):
        for i in range(3):
            self._apply({"src/cli.py": f"第 {i} 版", "src/version.py": 'VERSION = "1.3.0"\n'})
        base = self.root / ".secrets" / "update"
        dirs = sorted(p.name for p in base.iterdir() if p.is_dir())
        self.assertEqual(len(dirs), 2, f"现场堆着不清：{dirs}")

    def test_现场放在_NEVER_TOUCH_里面(self):
        """放别处会被下一次升级当代码覆盖掉 —— 那就白写了。"""
        res = self._apply({"src/version.py": 'VERSION = "1.3.0"\n'})
        rel = Path(res["journal"]).relative_to(self.root)
        self.assertEqual(rel.parts[0], ".secrets")
        self.assertIn(rel.parts[0], selfupdate.NEVER_TOUCH)


class TestAtomicWrite(_ApplyCase):
    """阶段 1.4b：铺单个文件必须是**原子**的。

    ⚠ 盯的是一种最难查的损坏：`copyfile` 先截断再写，中断留下**半个 .py**。
    那以后报出来的是"程序坏了"，没人会想到是某次升级被掐断。
    """

    def test_先写临时文件再改名(self):
        """不许直接往目标路径写 —— 记下 `copyfile` 的目标就知道。"""
        seen = []
        real = shutil.copyfile

        def spy(src, dst, *a, **k):
            seen.append(str(dst))
            return real(src, dst, *a, **k)

        with mock.patch.object(shutil, "copyfile", spy):
            self._apply({"src/cli.py": "新代码"})
        wrote = [p for p in seen if p.endswith("src/cli.py")]
        self.assertEqual(len(wrote), 1)
        self.assertNotIn(str(self.root / "src" / "cli.py"), wrote,
                         "直接写目标了 —— 中断会留下半个文件")

    def test_改名失败时目标还是旧内容(self):
        """⚠ **别 mock 整个 `os` 模块** —— `_write_journal` 和 `_iter_files`
        也在用它（`os.replace` / `os.walk`），整块换掉会先炸在写 journal 上，
        测出来的是另一回事。要打就打真正那个缝：`_replace_retry`。"""
        with mock.patch.object(selfupdate, "_replace_retry",
                               side_effect=OSError("占住了")):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"),
                         "旧代码", "改名没成功，目标却已经被改了")

    def test_失败之后不留临时文件(self):
        with mock.patch.object(selfupdate, "_replace_retry",
                               side_effect=OSError("占住了")):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, keep_staging=True)
        left = list((self.root / "src").glob("*.new-*"))
        self.assertEqual(left, [], f"门店目录里留下垃圾了：{left}")

    def test_被占住时会重试(self):
        """杀软/索引器**瞬时**占住是常态 —— 不能一次就把升级判死。

        只让**目标文件**那一次 replace 失败；journal 自己的 replace 照常走，
        否则测的是"journal 写不下去"，不是重试。
        """
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(a, b):
            if str(b).endswith("src/cli.py") and calls["n"] == 0:
                calls["n"] += 1
                raise PermissionError("杀软正扫着")
            return real_replace(a, b)

        with mock.patch.object(selfupdate.os, "replace", flaky), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            res = self._apply({"src/cli.py": "新代码"})
        self.assertTrue(res["ok"])
        self.assertEqual(calls["n"], 1, "没有重试就放弃了")
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")


class TestWriteOrder(_ApplyCase):
    """阶段 1.4b：**入口文件最后落地**。

    ⚠ 新代码可能 import 新模块。入口最后写 ⇒ 任何时刻中断，
    旧入口配着"新模块已经在"的目录都还能跑（旧入口不认识新模块，
    但它也不会去 import 它们）—— 这是"不搞整目录交换也能接受"的前提之一。
    """

    def test_入口排在最后(self):
        order = []
        real = selfupdate._write_file

        def spy(src, dst):
            order.append(selfupdate._rel_key(Path(dst).relative_to(self.root)))
            return real(src, dst)

        with mock.patch.object(selfupdate, "_write_file", spy):
            self._apply({"src/cli.py": "新代码", "src/zzz_new.py": "Z", "src/aaa_new.py": "A"})
        self.assertEqual(order[-1], "src/cli.py")
        self.assertEqual(order[-2], "bootstrap.py")
        self.assertNotIn("src/cli.py", order[:-1])
        self.assertLess(order.index("src/zzz_new.py"), order.index("src/cli.py"),
                        "普通模块必须排在入口前面")


class TestPruneCandidates(_ApplyCase):
    """阶段 1.4c：哪些旧文件**可以**清、哪些**绝对不许碰**。

    ⚠ 这一版 `PRUNE_ENABLED=False` —— **只算不删**（用户 2026-09-19 定的审计版）。
    先把门店真实候选看清楚，再打开真删。
    """

    def _zip(self, files):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for rel, text in files.items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return d

    def _put(self, rel, text="x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def _cand(self, zipfiles):
        z = self._zip(zipfiles)
        have = {selfupdate._rel_key(r) for r in zipfiles}
        return selfupdate.prune_candidates(self.root, z, have)

    def test_新版没有的旧模块算残留(self):
        self._put("src/pools.py", "老的四池实现")
        c = self._cand({"src/cli.py": "旧代码"})
        self.assertIn("src/pools.py", c["files"])
        self.assertFalse(c["blocked"])

    def test_根目录一律不删(self):
        """`run*.bat` 是本机生成的、`BUILD.txt` 是装完写的、根目录文档
        （`发布说明.md` 2026-09-23 起也已在仓库里）不参与差集 ——
        在根目录做差集就会删掉它们。"""
        for name in ("run.bat", "run-now.bat", "BUILD.txt", "AGENTS.md", "发布说明.md"):
            self._put(name)
        c = self._cand({"src/cli.py": "旧代码"})
        for name in ("run.bat", "run-now.bat", "BUILD.txt", "AGENTS.md", "发布说明.md"):
            self.assertNotIn(name, c["files"])

    def test_门店配置和会话永不删(self):
        self._put("config/store-SCN231409.yaml", "我的门店配置")
        self._put("out/报告.xlsx", "历史报告")
        self._put(".secrets/erp.env", "我的账号")
        self._put("tools/build_package.sh", "打包工具")
        self._put(".dsh/memory/x.md", "记忆")
        c = self._cand({"src/cli.py": "旧代码"})
        self.assertEqual(c["files"], [], f"动了不该动的东西：{c['files']}")

    def test_stores_yaml_不会被清掉(self):
        """它是**随程序走的门店映射表**（`ALLOW_EVEN_IF_NEVER`）——
        删了 29 家店一起瞎。"""
        self._put("config/stores.yaml", "门店映射表")
        c = self._cand({"src/cli.py": "旧代码"})
        self.assertNotIn("config/stores.yaml", c["files"])
        self.assertIn("映射表", c["skipped"].get("config/stores.yaml", ""))

    def test_锚点永不进删除候选(self):
        """⚠ 老更新器硬依赖这两个路径 —— 删掉 = 全公司门店再也收不到更新。

        `src/cli.py` 落在前缀里，靠**锚点那道闸**挡住；`bootstrap.py` 在根目录，
        第一道闸（前缀白名单）就挡住了。两条路都必须"不可删"。
        """
        z = self._zip({"src/other.py": "x"})
        self.assertEqual(selfupdate.prune_reason(self.root, z, "src/cli.py"), "更新器锚点")
        self.assertNotEqual(selfupdate.prune_reason(self.root, z, "bootstrap.py"), "")
        self._put("bootstrap.py", "入口")
        c = self._cand({"src/other.py": "x"})
        self.assertNotIn("src/cli.py", c["files"])
        self.assertNotIn("bootstrap.py", c["files"])

    def test_删之前再问一次文件系统(self):
        """门店**真出过** `os.walk` 漏掉 `src/cli.py`（`:717-735` 的兜底就是为它写的）。

        以前那只是"更新被拒"；有了删除之后就变成"删掉 cli.py"。
        所以：**遍历说没有、但 `is_file()` 说有 ⇒ 不删。**
        """
        z = self._zip({"src/ghost.py": "新版里其实有这个文件"})
        have = set()                      # 模拟"遍历漏了"：一个都没带上
        c = selfupdate.prune_candidates(self.root, z, have)
        self.assertNotIn("src/ghost.py", c["files"])
        self.assertEqual(selfupdate.prune_reason(self.root, z, "src/ghost.py"),
                         "新版里其实还有它（遍历漏了，按存在处理）")

    def test_手工备份和缓存不会被当成残留(self):
        for name in ("src/pools.py.bak-20260919", "src/pools.py.orig",
                     "src/pools.py.new-4242", "src/notes.txt.tmp"):
            self._put(name)
        (self.root / "src" / "__pycache__").mkdir(exist_ok=True)
        (self.root / "src" / "__pycache__" / "pools.cpython-38.pyc").write_text("x")
        c = self._cand({"src/cli.py": "旧代码"})
        self.assertEqual(c["files"], [], f"把手工备份/缓存当残留了：{c['files']}")

    def test_候选太多就整体不删(self):
        """一次正常重构不会让两成文件消失 —— 那种情况多半是包不对/解压不全。"""
        for i in range(30):
            self._put(f"src/old_{i}.py")
        c = self._cand({"src/cli.py": "旧代码"})
        self.assertTrue(c["blocked"], f"30/{c['total']} 个候选居然没被拦")
        self.assertEqual(len(c["files"]), 30, "候选还是要算出来给人看")

    def test_少量残留不触发比例闸(self):
        for i in range(3):
            self._put(f"src/old_{i}.py")
        self.assertFalse(self._cand({"src/cli.py": "旧代码"})["blocked"])

    def test_审计版只报告不删(self):
        self._put("src/pools.py", "老的四池实现")
        res = self._apply({"src/cli.py": "新代码"})
        self.assertIn("src/pools.py", res["removed_candidates"])
        self.assertEqual(res["removed"], [], "审计版不该真删")
        self.assertFalse(res["prune_enabled"])
        self.assertTrue((self.root / "src" / "pools.py").is_file(),
                        "审计版把文件删了 —— 那还叫审计吗")

    def test_残留也进快照(self):
        """⚠ 要删的东西才是**最需要退路**的 —— 备份必须发生在删之前。"""
        self._put("src/pools.py", "老的四池实现")
        res = self._apply({"src/cli.py": "新代码"})
        backup = Path(res["backup"])
        self.assertEqual((backup / "src" / "pools.py").read_text(encoding="utf-8"),
                         "老的四池实现")

    def test_升级时新版独有的残留会被清掉(self):
        """`apply_update` 的删除规则：新版没有的旧文件要能清掉（有备份）。"""
        self._put("src/brand_new.py", "只在新版里有")
        res = self._apply({"src/cli.py": "新代码"})
        self.assertIn("src/brand_new.py", res["removed_candidates"])

    def test_打开开关就真的搬走而且有备份(self):
        """⚠ 真删那条路**现在就测**（把开关 mock 成 True）——
        否则 1.4d 打开 `PRUNE_ENABLED` 时是在动一段没人验过的代码。"""
        self._put("src/pools.py", "老的四池实现")
        with mock.patch.object(selfupdate, "PRUNE_ENABLED", True):
            res = self._apply({"src/cli.py": "新代码"})
        self.assertTrue(res["prune_enabled"])
        self.assertEqual(res["removed"], ["src/pools.py"])
        self.assertFalse((self.root / "src" / "pools.py").exists(), "该清的没清")
        self.assertEqual((Path(res["backup"]) / "src" / "pools.py").read_text(encoding="utf-8"),
                         "老的四池实现", "搬走了却没进备份 —— 那就退不回来了")

    def test_比例闸拦住时一个都不删(self):
        for i in range(30):
            self._put(f"src/old_{i}.py")
        with mock.patch.object(selfupdate, "PRUNE_ENABLED", True):
            res = self._apply({"src/cli.py": "新代码"})
        self.assertTrue(res["prune_blocked"])
        self.assertEqual(res["removed"], [])
        self.assertTrue((self.root / "src" / "old_0.py").is_file())

    def test_没有备份目录就一个都不搬(self):
        """宁可不删，也不能删了找不回。"""
        self._put("src/pools.py", "老的四池实现")
        self.assertEqual(selfupdate._prune(self.root, ["src/pools.py"], ""), [])
        self.assertTrue((self.root / "src" / "pools.py").is_file())


class TestRepairAndRestore(_ApplyCase):
    """阶段 1.4d：断了之后**怎么收尾** —— 重跑（不用联网）或者从备份退回去。

    ⚠ 这条路是给"服务起不来、控制台进不去"准备的，所以它**不能依赖网络**，
    也不能抛异常（命令行和 HTTP 两条路都会调它）。
    """

    def setUp(self):
        super().setUp()
        # ⚠ `repair()` 内部走的 `apply_update` 默认会冒烟，而假安装目录
        #   （`src/cli.py` 里就"新代码"三个字）根本 import 不起来。
        #   冒烟本身由 `TestSmokeAndRollback` 覆盖。
        p = mock.patch.object(selfupdate, "smoke_test", lambda root, timeout=90: (True, ""))
        p.start()
        self.addCleanup(p.stop)

    def _tree_hash(self):
        """整棵安装目录的哈希（不含 `.secrets/update` —— 现场本来就该变）。"""
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = selfupdate._rel_key(p.relative_to(self.root))
            if rel.startswith(".secrets/update"):
                continue
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
        return h.hexdigest()

    def _break(self, files=None):
        """跑到一半断掉，留下现场。"""
        with mock.patch.object(selfupdate, "_write_file", self._boom(after=1)):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply(files or {"src/cli.py": "新代码"}, keep_staging=True)

    def test_从备份恢复能回到升级前(self):
        """整棵树逐字节比对 —— 包括"这次新加的文件要被挪走"。"""
        before = self._tree_hash()
        self._break({"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'})
        self.assertNotEqual(self._tree_hash(), before, "都没改动，这个测试就没意义了")
        res = selfupdate.restore_backup(self.root)
        self.assertTrue(res["ok"], res.get("message"))
        self.assertEqual(self._tree_hash(), before, "没回到升级前")
        self.assertEqual(selfupdate.pending(self.root), {}, "恢复完了还报'没走完'")

    def test_恢复也不会碰数据(self):
        self._break()
        selfupdate.restore_backup(self.root)
        self.assertEqual((self.root / "config" / "store-SCN231409.yaml").read_text(encoding="utf-8"),
                         "我的门店配置")
        self.assertEqual((self.root / ".secrets" / "erp.env").read_text(encoding="utf-8"),
                         "我的账号")
        self.assertEqual((self.root / "out" / "报告.xlsx").read_text(encoding="utf-8"),
                         "历史报告")

    def test_没有备份就说清楚(self):
        res = selfupdate.restore_backup(self.root)
        self.assertFalse(res["ok"])
        self.assertIn("找不到备份", res["message"])

    def test_重跑优先用上次的解压目录(self):
        """⚠ **不联网**也能修 —— 门店断网时这是唯一的路。"""
        self._break()
        with mock.patch.object(selfupdate, "download") as dl:
            res = selfupdate.repair(self.root, current="1.2.0")
        dl.assert_not_called()
        self.assertTrue(res["ok"])
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")
        self.assertEqual(selfupdate.pending(self.root), {})

    def test_解压目录没了就重新下载(self):
        """系统临时目录重启后会被清 —— 那种情况只能重下。"""
        self._break()
        shutil.rmtree(selfupdate.read_journal(self.root)["staging"], ignore_errors=True)
        blob = _fake_zip("cbg-reconcile-main",
                         {"bootstrap.py": "x", "src/cli.py": "新代码"})
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(d)
        with mock.patch.object(selfupdate, "download", lambda **k: d / "cbg-reconcile-main") as dl:
            res = selfupdate.repair(self.root, current="1.2.0")
        self.assertTrue(res["ok"])

    def test_没有没走完的升级就直说(self):
        res = selfupdate.repair(self.root, current="1.2.0")
        self.assertFalse(res["ok"])
        self.assertIn("没有没走完", res["message"])

    def test_自检会报出没走完的升级(self):
        """⚠ 中断必须**在人看得见的地方**说出来 —— 否则程序静默跑在混合版本上。"""
        self._break()
        lines = selfupdate.integrity_lines(self.root)
        self.assertTrue(lines, "自检一声不吭")
        self.assertIn("没走完", lines[0])
        self.assertIn("--restore", "\n".join(lines))
        selfupdate.restore_backup(self.root)
        self.assertEqual(selfupdate.integrity_lines(self.root), [], "修完了还在报")

    def test_命令行_repair_和_restore_能跑(self):
        """服务起不来时唯一的路 —— 退出码必须是 0/非 0 说得清的。"""
        import argparse

        from src import cli
        with mock.patch.object(cli, "ROOT", self.root):
            self.assertEqual(cli.cmd_update(argparse.Namespace(repair=False, restore=False)), 0)
            self._break()
            self.assertEqual(cli.cmd_update(argparse.Namespace(repair=True, restore=False)), 0)
            self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")


class TestSmokeAndRollback(_ApplyCase):
    """阶段 1.4e：铺完**冒烟一遍**，不过就**自动退回升级前**。

    ⚠ 这两步合起来是"无人值守自动更新"能不能成立的**分界线**：
    没有它，一次断在半路或铺进一个 import 不起来的版本的更新，
    对门店来说就是"某天开始，程序莫名其妙起不来了"。
    """

    def _tree_hash(self):
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = selfupdate._rel_key(p.relative_to(self.root))
            if rel.startswith(".secrets/update"):
                continue
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
        return h.hexdigest()

    def test_冒烟不过就自动退回升级前(self):
        before = self._tree_hash()
        with mock.patch.object(selfupdate, "smoke_test",
                               lambda root, timeout=90: (False, "ModuleNotFoundError: requests")):
            with self.assertRaises(selfupdate.PartialUpdate) as cm:
                self._apply({"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'},
                            smoke=True)
        self.assertIn("自动退回", str(cm.exception))
        self.assertTrue(cm.exception.result.get("rolled_back"))
        self.assertEqual(self._tree_hash(), before, "退回去了但目录没回到原样")
        self.assertEqual(selfupdate.pending(self.root), {},
                         "已经自己退回来了，不该还挂着「没走完」的红条")

    def test_冒烟不过时不写_BUILD_txt(self):
        """⚠ 代码退回去了、指纹却写着新版本 —— 界面会显示一个它没在跑的版本。"""
        (self.root / "BUILD.txt").write_text("2026-09-01 10:00\n", encoding="utf-8")
        with mock.patch.object(selfupdate, "smoke_test", lambda root, timeout=90: (False, "boom")):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码", "src/version.py": 'VERSION = "1.3.0"\n'},
                            smoke=True)
        self.assertEqual((self.root / "BUILD.txt").read_text(encoding="utf-8"),
                         "2026-09-01 10:00\n")

    def test_冒烟不过就不清旧文件(self):
        """新代码还没被证明能用，就先别动旧文件。"""
        self._put_old()
        with mock.patch.object(selfupdate, "PRUNE_ENABLED", True), \
                mock.patch.object(selfupdate, "smoke_test", lambda root, timeout=90: (False, "x")):
            with self.assertRaises(selfupdate.PartialUpdate):
                self._apply({"src/cli.py": "新代码"}, smoke=True)
        self.assertTrue((self.root / "src" / "pools.py").is_file())

    def test_冒烟通过就照常完成(self):
        with mock.patch.object(selfupdate, "smoke_test", lambda root, timeout=90: (True, "ok")):
            res = self._apply({"src/cli.py": "新代码"}, smoke=True)
        self.assertTrue(res["ok"])
        self.assertEqual((self.root / "src" / "cli.py").read_text(encoding="utf-8"), "新代码")

    def test_冒烟命令本身在真项目根上能过(self):
        """⚠ 这条是**真实解释器**那一档证据：证明冒烟命令本身是对的，
        而不只是"我们调了它"。"""
        ok, why = selfupdate.smoke_test(ROOT)
        self.assertTrue(ok, f"在真项目根上冒烟没过：{why}")

    def test_冒烟命令在坏目录上会失败(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        ok, why = selfupdate.smoke_test(d)
        self.assertFalse(ok)
        self.assertIn("退出码", why)

    def test_没改任何文件就不冒烟(self):
        """全都一样时不该白起一个进程（每次检查更新都会走这条）。"""
        (self.root / "bootstrap.py").write_text("x", encoding="utf-8")   # 跟包里一致
        calls = []
        with mock.patch.object(selfupdate, "smoke_test",
                               lambda root, timeout=90: calls.append(1) or (True, "")):
            res = self._apply({"src/cli.py": "旧代码"}, smoke=True)
        self.assertTrue(res["ok"])
        self.assertEqual(calls, [], "没有任何改动还去起了一个进程")

    def _put_old(self):
        p = self.root / "src" / "pools.py"
        p.write_text("老的四池实现", encoding="utf-8")
        return p


class Test打包规则对账(unittest.TestCase):
    """阶段 1.4f：**同一条规则的两次实现必须能对账**。

    排除规则写在两个地方：`selfupdate.NEVER_TOUCH`（自更新照铺时不许碰什么）
    和 `tools/build_package.sh` 的 `--exclude`（打包时不许装什么）。
    两边靠**注释**对齐已经有漂过（实测：自检名单里躺着 `设计文档.md`、`packaging` 两个
    死条目），所以改成测试钉住。
    """

    SH = ROOT / "tools" / "build_package.sh"

    def _excludes(self) -> list:
        text = self.SH.read_text(encoding="utf-8")
        return re.findall(r"--exclude '([^']+)'", text)

    def _selfcheck_names(self) -> list:
        """自检那两段名单（文档 / 开发垃圾）。

        ⚠ 只认 `_doc` 和 `junk` 这两个循环变量 —— 脚本里还有别的 `for … in …; do`
        （拷 bat 那个），一把抓会把 `install start stop …` 当成文件名。
        """
        text = self.SH.read_text(encoding="utf-8")
        names = []
        for m in re.finditer(r"for\s+(?:_doc|junk)\s+in\s+([^;]+);", text):
            body = m.group(1).replace("\\\n", " ")
            names += [x.strip("'\"") for x in re.findall(r"'[^']+'|\S+", body)
                      if x.strip("'\"")]
        return names

    def test_打包脚本的排除项覆盖所有_NEVER_TOUCH(self):
        """自更新不碰的东西，包里也不该有 —— 两边漏一个就是一次事故。

        ⚠ 比较前剥掉**前导 `/`**（f65d901 起打包用 `/tools` 这种**顶格锚定**写法，
        防 rsync 把 `web/tools` 一起误伤）—— 锚定只是写法差异，"排除了没有"得按
        剥干净的名字对账，否则会把 `/tools` 误判成"没排除 tools"。
        """
        ex = [e.strip("/") for e in self._excludes()]
        missing = []
        for name in selfupdate.NEVER_TOUCH:
            if any(e == name or e.startswith(name + "/") for e in ex):
                continue
            missing.append(name)
        self.assertEqual(missing, [], "打包脚本没排除这些："
                         + ", ".join(missing) + f"（--exclude 里有：{ex}）")

    def test_打包脚本不会丢掉四个前缀里的文件(self):
        """⚠ **src / web 必须进包**（删除规则与自更新都依赖这两棵）。
        `tests/` 例外（2026-09-22 用户方案 2）：手工包与自更新都**不下发**，
        git 里保留 —— 两边一致，不是"包里没有、更新却会给"。
        `config/` 只许排除 `store-*.yaml`（这台电脑自己的）。"""
        for e in self._excludes():
            key = e.strip("/")           # 前导 `/` = 顶格锚定（防误伤 web/tools），
                                         # 对账按剥干净的名字比（见上一条的说明）
            if key == "tests" or key.startswith("tests/"):
                continue                      # 有意排除：门店不跑 pytest
            self.assertNotIn(
                key, ("src", "web"),
                f"--exclude '{e}' 会把 {key}/ 挡在包外 —— 自更新却会给门店，两边就不一致了")
            self.assertFalse(
                key.startswith(("src/", "web/")),
                f"--exclude '{e}' 动了 src/ 或 web/")
            if key.startswith("config/"):
                self.assertEqual(
                    key, "config/store-*.yaml",
                    f"--exclude '{e}'：config/ 里只允许排除**这台电脑自己的**门店配置，"
                    "别的文件（比如 config/stores.yaml）必须进包")
        # tests 必须被排除（跟 SKIP_APPLY 对齐）
        ex = [e.strip("/") for e in self._excludes()]
        self.assertIn("tests", ex,
                      "打包脚本应 --exclude 'tests/'，与 selfupdate.SKIP_APPLY 对齐")

    def test_自检名单和排除项对得上(self):
        """⚠ 脚本自己写着「名单要和 `--exclude` 那一段一一对上」——
        本轮核的时候**已经对不上**（`设计文档.md`、`packaging` 两边都没有）。
        这条测试就是那句话的执行版。

        ⚠ 同上：`--exclude` 里的**前导 `/`（顶格锚定，防误伤 `web/tools`）
        比较时剥掉** —— 锚定是写法，对账问的是"排除了没有"。
        """
        ex = [e.strip("/") for e in self._excludes()]
        bad = []
        for name in self._selfcheck_names():
            if name.startswith("${") or name.startswith("$"):
                continue                      # 脚本里的变量，不是文件名
            if name in ex:
                continue
            if (ROOT / name).exists():
                bad.append(f"{name}（文件在，但 --exclude 里没有）")
            else:
                bad.append(f"{name}（既不在仓库里、也不在 --exclude 里 —— 死条目）")
        self.assertEqual(bad, [], "自检名单和 --exclude 对不上：\n  " + "\n  ".join(bad))

    def test_删除白名单不会碰到本机生成的文件(self):
        """`PRUNE_PREFIXES` 里的每一项要么**不在** `NEVER_TOUCH`，
        要么就是 `config`（靠 `ALLOW_EVEN_IF_NEVER` 单独放行）。"""
        for p in selfupdate.PRUNE_PREFIXES:
            if p in selfupdate.NEVER_TOUCH:
                self.assertEqual(p, "config",
                                 f"{p} 在 NEVER_TOUCH 里，却进了删除白名单")
        self.assertNotIn("config/stores.yaml", selfupdate.PRUNE_PREFIXES,
                         "门店映射表永远不许进删除白名单")

    def test_打包前要求工作区干净(self):
        """⚠ D7：`rsync` 拷的是**工作区**，未跟踪文件会进包、而 zip 里没有 ——
        门店装了这种包，下一次自更新就被当残留清掉（发了个短命包）。"""
        text = self.SH.read_text(encoding="utf-8")
        self.assertIn("status --porcelain", text)
        self.assertIn("CBG_ALLOW_DIRTY", text)
        # ⚠ 顺序也是要求：必须**在复制之前**拦下来 —— 拷完再检查就晚了。
        #   锚"行首的 rsync"：注释里也提到过它（那段说明就写在检查上面）。
        m = re.search(r"(?m)^rsync -a", text)
        self.assertIsNotNone(m, "找不到复制那一步")
        self.assertLess(text.index("status --porcelain"), m.start(),
                        "工作区检查排在 rsync 后面 —— 那已经拷完了")


class TestDownloadSources(unittest.TestCase):
    """下载候选源的**顺序**—— 源码仓 / 发行仓分离之后，这就是私有化的保险。

    见 `selfupdate` 模块文档：发行仓必须排在源码仓前面。顺序一反，
    源码仓改 Private 的那一刻，还在跑旧代码的门店**当场就查不到更新**，
    而且没有任何补救 —— 那是整次迁移里唯一不可逆的一步。
    """

    #: 发行仓资产的 api 直链（`Accept: application/octet-stream` 才吐字节）
    ASSET = ("https://api.github.com/repos/HappyJoy95/cbg-reconcile-release"
             "/releases/assets/4242")
    TAG = "v26.0929.191128"

    def _patch_release(self, asset=ASSET, tag=TAG):
        """把"问 API 要资产地址"这一步**离线化** —— `_zip_urls` 会现查它。"""
        return mock.patch.object(selfupdate, "_release_asset_url",
                                 lambda timeout=15: (asset, tag))

    def _urls(self, ref=None, asset=ASSET, tag=TAG):
        with self._patch_release(asset, tag):
            return selfupdate._zip_urls(ref or selfupdate.BRANCH)

    @staticmethod
    def _release_urls(urls):
        """只留发行仓的地址 —— 收 `(url, headers)` 或裸 `url` 都认。"""
        return [u if isinstance(u, str) else u[0] for u in urls
                if (u if isinstance(u, str) else u[0]).find("/releases/") >= 0]

    @staticmethod
    def _source_urls(urls):
        """只留源码仓的地址（zipball / codeload）—— 同上。"""
        out = []
        for u in urls:
            u = u if isinstance(u, str) else u[0]
            if "zipball" in u or "codeload" in u:
                out.append(u)
        return out

    def test_release_asset_is_tried_first(self):
        """**发行仓排在源码仓前面** —— 顺序反了，源码仓私有化就是灾难。"""
        urls = [u for u, _ in self._urls()]
        rel, src = self._release_urls(urls), self._source_urls(urls)
        self.assertTrue(rel, "发行仓一个候选源都没有")
        self.assertTrue(src, "源码仓退路没了")
        self.assertEqual(urls[0], self.ASSET, "第一个必须是发行仓的资产直链")
        self.assertLess(max(urls.index(u) for u in rel),
                        min(urls.index(u) for u in src),
                        "发行仓的候选源必须整体排在源码仓前面")

    def test_asset_url_carries_octet_stream_header(self):
        """不带这个请求头，`api.github.com` 的资产地址返回的是**元数据 JSON**，
        解压时会报"这不是 zip" —— 而报错完全看不出是请求头的问题。"""
        pairs = self._urls()
        self.assertEqual(pairs[0][1], selfupdate._OCTET_HEADERS)
        # 没有请求头的那些必须是 None（别让每个源都带上无用的头）
        for url, headers in pairs[1:]:
            self.assertIsNone(headers, url)

    def test_source_repo_urls_follow_the_requested_ref(self):
        """`download(ref=…)` 要能把源码仓那两条的 ref 换成指定分支/tag。

        ⚠ 发行仓那几条**故意不跟 ref 走** —— 它们的包跟着"最新 Release"。
        """
        sha = "abc1234def5678"
        src = self._source_urls(self._urls(sha))
        self.assertEqual(len(src), 2, "源码仓的两条退路没了")
        for u in src:
            self.assertIn(sha, u)
        for u in self._release_urls(self._urls(sha)):
            self.assertNotIn(sha, u)

    def test_release_repo_unreachable_still_has_the_source_fallback(self):
        """发行仓整个挂了（问不到资产地址）—— 源码仓那两条还在。"""
        urls = [u for u, _ in self._urls(asset="", tag="")]
        self.assertTrue(self._source_urls(urls), "退路没了")
        self.assertTrue(any("releases/latest/download" in u for u in urls),
                        "拿不到 tag 也该留一条固定名的直链")

    def test_release_asset_url_never_raises(self):
        """这条是**最前面**的候选源 —— 它抛异常会把整次下载判死。
        拿不到就返回空，让后面的退路上。"""
        with mock.patch.object(selfupdate, "_get",
                               side_effect=selfupdate.UpdateError("挂了")):
            self.assertEqual(selfupdate._release_asset_url(), ("", ""))

    def test_tries_every_source_then_reports_them_all(self):
        """一个个试过去，全挂了要把**每个源**都写进报错 —— 门店要靠它判断
        是"整个 GitHub 都不通"还是"只有发行仓出事"。"""
        tried = []

        def fake_get(url, *, timeout, stream=False, **kw):
            tried.append(url)
            raise selfupdate.UpdateError("挂了")

        with self._patch_release(), mock.patch.object(selfupdate, "_get", fake_get):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.download()
        self.assertEqual(tried[0], self.ASSET, "发行仓资产必须最先试")
        self.assertIn("zipball", tried[-2])
        self.assertIn("codeload", tried[-1], "codeload 是最后一道退路")
        msg = str(cm.exception)
        self.assertIn("api.github.com", msg)
        self.assertIn("codeload", msg, "报错要说清源码仓那两条都试过了")
        self.assertIn("下载失败", msg)


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
                selfupdate.apply_update(self.tmp, current="1.3.2", smoke=False)
        msg = str(cm.exception)
        self.assertIn("src/cli.py", msg, "缺哪个文件要说")
        self.assertIn("命令行入口", msg, "它是干什么的也要说")
        self.assertIn("README.md", msg, "实际收到的东西要列出来")
        self.assertIn("手动升级", msg, "要给出退路 —— 网络问题是修不好的")
        self.assertIn("运维手册", msg, "退路要指到具体文档")

    def test_a_good_package_still_passes(self):
        """闸门不能把正常的包也拦下来。"""
        pkg = self._pkg("HappyJoy95-cbg-reconcile-abc1234", {
            "src/cli.py": "# cli", "src/version.py": 'VERSION = "1.3.2"',
            "bootstrap.py": "x", "install.bat": "y",
        })
        with mock.patch.object(selfupdate, "download", lambda *a, **k: pkg):
            res = selfupdate.apply_update(self.tmp, current="1.3.1", smoke=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["to"], "1.3.2")
        self.assertTrue((self.tmp / "src" / "cli.py").is_file())


class Test发行仓发布脚本(unittest.TestCase):
    """`tools/publish_release.sh` —— **对着公开仓库的那道闸**。

    发行仓是公开的，所以"打包时注入的密钥不许上去"这件事**不能靠人记得**：
    `build_package.sh` 当年就是靠 `--exclude` + 反查两道才稳住的，这里照抄那个形状。
    """

    SH = ROOT / "tools" / "publish_release.sh"

    def _text(self) -> str:
        return self.SH.read_text(encoding="utf-8")

    def test_发行仓地址从_selfupdate_读而不是写死(self):
        """写死一份迟早和 `selfupdate.RELEASE_REPO` 对不上 ——
        那时的表现是**客户端去问 A 仓、发布脚本往 B 仓发**，两边都不报错。"""
        t = self._text()
        self.assertIn('sed -n', t)
        self.assertIn("RELEASE_REPO", t)
        self.assertNotIn('RELEASE_REPO = "HappyJoy95/', t,
                         "发行仓地址必须从 src/selfupdate.py 读，不许在这儿再写一份")

    def test_两把密钥都在禁发名单里(self):
        """`central-mail.env`（中台授权码）和 `mail-key.json`（附件加密密钥）
        —— 进了公开 Release 就**收不回来**（CDN 缓存 / 别人的 clone 里还在）。"""
        t = self._text()
        m = re.search(r"BAN_NAMES\s*=\s*\{([^}]+)\}", t)
        self.assertIsNotNone(m, "找不到禁发名单 BAN_NAMES")
        names = set(re.findall(r'"([^"]+)"', m.group(1)))
        for must in ("central-mail.env", "mail-key.json"):
            self.assertIn(must, names, f"{must} 不在禁发名单里")

    def test_区长名单不进公开资产(self):
        """`config/managers.yaml` = 区长名单，里面是**云商登录名和私人邮箱**。

        它在 `selfupdate.NEVER_TOUCH` 里（`config/` 整个目录）⇒ 更新本来就
        不会覆盖它，从更新包里拿掉对已装机器零影响。但它已经在公开源码仓里
        躺过一轮了（源码仓私有化会顺带解决）—— **发行仓这份不能再带上**。
        """
        t = self._text()
        m = re.search(r"BAN_PATHS\s*=\s*\{([^}]+)\}", t)
        self.assertIsNotNone(m, "找不到按路径禁发的 BAN_PATHS")
        paths = set(re.findall(r'"([^"]+)"', m.group(1)))
        self.assertIn("config/managers.yaml", paths)

        # 三处判据必须一致：剔除 / 反查 / 实际写出 —— 少一处就是漏
        self.assertGreaterEqual(t.count("BAN_PATHS"), 3,
                                "BAN_PATHS 只在一处生效 —— 剔除、反查、写出要三处都认")

    def test_反查排在上传之前(self):
        """⚠ 断言必须在**动作之前**拦下来 —— 上传完再发现就晚了，
        而且 GitHub 的 release 资产删掉也不保证 CDN 上没了。"""
        t = self._text()
        check = t.find("反查没过")
        upload = t.find("gh release create")
        self.assertGreater(check, 0, "找不到反查断言")
        self.assertGreater(upload, 0, "找不到上传那一步")
        self.assertLess(check, upload, "反查排在上传后面 —— 那是事后诸葛亮")

    def test_beta_包不动发行仓的_VERSION(self):
        """beta 的版本号和正式版**同号**（AGENTS 发版那节）。
        beta 要是把发行仓的 VERSION 改了，门店会看到一个假的"有新版"，
        点下去装的却是测试包。"""
        t = self._text()
        guard = t.find('if [ -z "${BETA}" ]; then')
        put = t.find('put_file "VERSION"')
        self.assertGreater(guard, 0, "找不到 beta 判断")
        self.assertGreater(put, 0, "找不到写 VERSION 那一步")
        self.assertLess(guard, put, "VERSION 写在 beta 判断外面了")

    def test_资产名和客户端读的是同一个(self):
        """脚本上传的文件名必须 == `selfupdate.RELEASE_ASSET`，
        否则 `_zip_urls` 那条固定名的退路永远 404。"""
        t = self._text()
        self.assertIn("RELEASE_ASSET", t)
        m = re.search(r'RELEASE_ASSET=.*?sed -n[^\n]+', t)
        self.assertIsNotNone(m, "资产名不是从 selfupdate.py 读的")
        self.assertIn("src/selfupdate.py", m.group(0))


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

    def test_release_repo_is_asked_before_the_source_repo(self):
        """⚠ **发行仓必须排在源码仓前面** —— 这是"源码仓私有化"的保险。

        顺序反了的话：源码仓改 Private 的那一刻，还没升上来的门店当场 404，
        再也收不到更新，而那时已经没有任何补救（唯一不可逆的一步）。
        """
        tried = []

        def fake_get(url, **kw):
            tried.append(url)
            return self._api_payload("9.9.9")

        with mock.patch.object(selfupdate, "_get", fake_get):
            selfupdate.remote_version()
        self.assertEqual(len(tried), 1, "第一个源就答上来了，不该再问第二个")
        self.assertIn(selfupdate.RELEASE_REPO, tried[0],
                      f"第一个问的是源码仓，不是发行仓：{tried[0]}")
        self.assertIn("contents/VERSION", tried[0],
                      "发行仓的版本号在根目录的 VERSION 里，不是 src/version.py")

    def test_source_repo_is_the_last_resort(self):
        """发行仓两个源全挂了，才轮到源码仓（源码仓私有化之后它会 404，
        但那时发行仓已经答上来了）。"""
        seen = []

        def fake_get(url, **kw):
            seen.append(url)
            if selfupdate.RELEASE_REPO in url:
                raise selfupdate.UpdateError("发行仓不通")
            return self._api_payload("1.3.4")

        with mock.patch.object(selfupdate, "_get", fake_get):
            self.assertEqual(selfupdate.remote_version(), "1.3.4")
        self.assertEqual(len(seen), 3, "发行仓 api+raw 两发，源码仓 api 一发")
        self.assertIn(selfupdate.RELEASE_REPO, seen[0])
        self.assertIn("raw.githubusercontent", seen[1])
        self.assertIn(selfupdate.REPO + "/contents", seen[2])

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
            res = selfupdate.apply_update(self.root, current="1.4.2", smoke=False)
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
                selfupdate.apply_update(self.root, current="1.4.2", smoke=False)
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
            selfupdate.apply_update(self.root, current="1.4.2", smoke=False)
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
                calls.setdefault("kw", []).append(kw)
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
        """每个源都试过、都放弃之后才报错。

        ⚠ 现在是**两个仓库 × (api + raw) = 四个源**（发行仓优先、源码仓退路）：
        api 那两条各试 1 次（不通就赶紧换下一个源），raw 那两条各试 TRIES 次。
        """
        import requests as real
        fake, calls = self._fake_requests([real.exceptions.SSLError("boom")])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate.remote_version()
        self.assertEqual(calls["n"], len(selfupdate._version_sources())
                         * (1 + selfupdate.TRIES))
        self.assertIn("试了", str(cm.exception))

    def test_each_attempt_uses_a_fresh_session(self):
        """坏连接别复用 —— 每次都要新开 Session。"""
        import inspect
        src = inspect.getsource(selfupdate._get)
        self.assertIn("Session()", src)
        self.assertIn("with requests.Session()", src)


class Test代理兜底(unittest.TestCase):
    """代理软件重启 / 换端口那阵，环境变量还指着没人听的端口 ⇒ 每一发都是
    `ProxyError`（2026-09-29 实测：走代理全挂、直连 200）。

    用户：「加个兜底吧，感觉有必要」—— 检查更新不该被「代理抖一下」卡住。
    """

    @staticmethod
    def _fake(results):
        """results: 每次调用给一个异常或 None（None = 成功的假响应）。"""
        import types as _t
        import requests as real
        calls = {"n": 0, "kw": []}

        class FakeResp:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"content": ""}

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, **kw):
                i = calls["n"]
                calls["n"] += 1
                calls["kw"].append(kw)
                r = results[min(i, len(results) - 1)]
                if isinstance(r, Exception):
                    raise r
                return FakeResp()

        fake = _t.ModuleType("requests")
        fake.RequestException = Exception
        fake.Session = FakeSession
        fake.exceptions = real.exceptions          # _proxy_broken 要拿 ProxyError
        return fake, calls, real

    def test_代理不通_立刻换直连不把重试耗在它身上(self):
        import requests as real
        fake, calls, _real = self._fake(
            [real.exceptions.ProxyError("Unable to connect to proxy"), None])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            r = selfupdate._get("https://x/y", timeout=1, tries=3)
        self.assertIsNotNone(r)
        self.assertEqual(calls["n"], 2,
                         "代理端口没人听是**确定性**的 —— 一次就该换直连")
        self.assertTrue(calls["kw"][1].get("proxies"),
                        "直连那次要显式带 proxies（不走代理）")

    def test_代理不通直连也不通_报错要写清两边(self):
        import requests as real
        fake, calls, _real = self._fake(
            [real.exceptions.ProxyError("Unable to connect to proxy"),
             real.exceptions.ConnectionError("Connection timed out")])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate._get("https://x/y", timeout=1, tries=2)
        msg = str(cm.exception)
        self.assertIn("代理不通、直连也不通", msg)
        self.assertIn("Unable to connect to proxy", msg, "代理那条的原始错误要留着")

    def test_普通网络错不触发兜底(self):
        """TLS 被掐那种（老的重试理由）不该顺手改成直连 —— 兜底只管代理。"""
        import requests as real
        fake, calls, _real = self._fake([real.exceptions.SSLError("boom")])
        with mock.patch.dict("sys.modules", {"requests": fake}), \
                mock.patch.object(selfupdate.time, "sleep", lambda s: None):
            with self.assertRaises(selfupdate.UpdateError) as cm:
                selfupdate._get("https://x/y", timeout=1, tries=2)
        self.assertEqual(calls["n"], 2, "该重试还是要重试满")
        for kw in calls["kw"]:
            self.assertNotIn("proxies", kw, "没走兜底就不该带直连参数")
        self.assertIn("连不上 GitHub", str(cm.exception))


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
