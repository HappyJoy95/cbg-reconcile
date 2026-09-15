"""版本指纹的测试 —— 这东西的存在意义就是"别再来回一轮"。"""
import tempfile, types, unittest
from pathlib import Path
from unittest import mock

from src import version, web


def _fake_git(root: Path, *, ref="refs/heads/main", detached_sha=None,
              sha="b0250d232b2a1cdf2b47f888ab2e35b765657e88") -> Path:
    """造一个够真的 `.git` —— 只要 HEAD 和 refs，别的 version.py 不看。

    `detached_sha` 非空 = detached HEAD（HEAD 里直接写 sha，没有 ref）。
    注意别用 `ref=""` 来模拟 detached：那会写出 `HEAD` = `"ref: "` ——
    是个**不成形**的 HEAD，不是 detached。
    """
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text(
        f"{detached_sha}\n" if detached_sha else f"ref: {ref}\n", encoding="utf-8")
    if ref and not detached_sha:
        (git / ref).write_text(sha + "\n", encoding="utf-8")
    return git


class TestBuildStamp(unittest.TestCase):
    def test_says_unpackaged_when_there_is_no_BUILD_txt(self):
        """既没有 BUILD.txt、也没有 .git —— 才是真的什么都没打包。"""
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertEqual(version.build_id(), version.UNPACKAGED)
            self.assertFalse(version.packaged())

    def test_reads_the_stamp_the_packager_wrote(self):
        d = Path(tempfile.mkdtemp())
        f = d / "BUILD.txt"
        f.write_text("2026-09-15 13:42\n", encoding="utf-8")
        with mock.patch.object(version, "BUILD_FILE", f):
            self.assertEqual(version.build_id(), "2026-09-15 13:42")
            self.assertTrue(version.packaged())

    def test_describe_mentions_both_version_and_build(self):
        d = Path(tempfile.mkdtemp())
        f = d / "BUILD.txt"
        f.write_text("2026-09-15 13:42", encoding="utf-8")
        with mock.patch.object(version, "BUILD_FILE", f):
            s = version.describe()
        self.assertIn(version.VERSION, s)
        self.assertIn("2026-09-15 13:42", s)

    def test_health_and_overview_expose_the_build(self):
        """界面和 /api/health 都要能看到 —— 门店同事报问题时让他念这一行就行。"""
        import inspect
        src = inspect.getsource(web.App.overview)
        self.assertIn("version.build_id()", src, "overview 没带构建指纹")
        src2 = inspect.getsource(web.Handler._api)
        self.assertIn('"build"', src2, "/api/health 没带构建指纹")


class TestGitFallback(unittest.TestCase):
    """clone 部署没有 BUILD.txt（那文件是未跟踪的）。

    没有这一层回退，门店界面上就只剩「源码运行（未打包）」——
    偏偏那是出问题时最想知道的一行：跑的是哪个 commit。
    """

    def test_reads_branch_and_short_sha_from_dot_git(self):
        d = Path(tempfile.mkdtemp())
        _fake_git(d)
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertEqual(version.git_revision(), ("main", "b0250d2"))
            self.assertEqual(version.git_stamp(), "git main@b0250d2")
            self.assertEqual(version.build_id(), "git main@b0250d2")
            self.assertTrue(version.packaged(), "clone 出来的算装好了")

    def test_BUILD_txt_wins_over_git(self):
        """打包/自更新写的时间戳比 .git 更权威 —— 别被开发机的 commit 顶掉。"""
        d = Path(tempfile.mkdtemp())
        _fake_git(d)
        (d / "BUILD.txt").write_text("2026-09-15 15:14\n", encoding="utf-8")
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertEqual(version.build_id(), "2026-09-15 15:14")

    def test_detached_head_has_no_branch_name(self):
        """detached HEAD：HEAD 里直接就是 sha，别硬编一个分支名出来。"""
        d = Path(tempfile.mkdtemp())
        _fake_git(d, ref="", detached_sha="b0250d232b2a1cdf2b47f888ab2e35b765657e88")
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertEqual(version.git_stamp(), "git b0250d2")

    def test_malformed_head_ref_is_not_a_branch(self):
        """`HEAD` 写成 `ref: `（后面空的）—— 只能当读不出来，不能编个空分支名。"""
        d = Path(tempfile.mkdtemp())
        (d / ".git").mkdir()
        (d / ".git" / "HEAD").write_text("ref: \n", encoding="utf-8")
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertIsNone(version.git_revision())
            self.assertEqual(version.build_id(), version.UNPACKAGED)

    def test_falls_back_to_packed_refs(self):
        """git gc 之后 refs 会挪进 packed-refs，那条路也得认。"""
        d = Path(tempfile.mkdtemp())
        git = _fake_git(d)
        (git / "refs" / "heads" / "main").unlink()      # gc 把 ref 收走了
        (git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted\n"
            "b0250d232b2a1cdf2b47f888ab2e35b765657e88 refs/heads/main\n",
            encoding="utf-8")
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertEqual(version.git_stamp(), "git main@b0250d2")

    def test_worktree_dot_git_file_is_followed(self):
        """worktree 里 .git 是个文件（gitdir: ...），不是目录。"""
        d = Path(tempfile.mkdtemp())
        real = d / "real-git"
        (real / "refs" / "heads").mkdir(parents=True)
        (real / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (real / "refs" / "heads" / "main").write_text(
            "b0250d232b2a1cdf2b47f888ab2e35b765657e88\n", encoding="utf-8")
        root = d / "worktree"
        root.mkdir()
        (root / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
        with mock.patch.object(version, "ROOT", root), \
                mock.patch.object(version, "BUILD_FILE", root / "BUILD.txt"):
            self.assertEqual(version.git_stamp(), "git main@b0250d2")

    def test_broken_dot_git_does_not_raise(self):
        """`.git` 存在但内容不成形 —— 只能降级，绝不能抛异常（界面会白屏）。"""
        d = Path(tempfile.mkdtemp())
        (d / ".git").mkdir()
        with mock.patch.object(version, "ROOT", d), \
                mock.patch.object(version, "BUILD_FILE", d / "BUILD.txt"):
            self.assertIsNone(version.git_revision())
            self.assertEqual(version.build_id(), version.UNPACKAGED)

    def test_git_stamp_needs_no_git_binary(self):
        """门店电脑上不装 git —— 所以这条路只能读文件，不能调命令。"""
        import inspect
        src = (inspect.getsource(version.git_revision)
               + inspect.getsource(version._read_sha))
        self.assertNotIn("subprocess", src)
        self.assertNotIn("Popen", src)


class TestSelftestShowsTheStore(unittest.TestCase):
    """正式包里的配置是**新业广场店**的。

    发到别的店忘了改那三行 → 会对到**别的店**账上去，而且**看起来一切正常**。
    所以自检第 0 节要把它摆在最显眼的位置：装完跑一次就知道配的是哪个店。
    """

    def _run_selftest(self, root) -> str:
        import contextlib
        import io
        from src import cli
        args = types.SimpleNamespace(config="config/store-X.yaml")
        buf = io.StringIO()
        # 只跑到"运行环境"这一节就够；后面的会去连网络
        with mock.patch.object(cli, "ROOT", root), \
                contextlib.redirect_stdout(buf), \
                mock.patch.object(cli, "cmd_selftest", cli.cmd_selftest):
            try:
                cli.cmd_selftest(args)
            except Exception:                      # noqa: BLE001
                pass
        return buf.getvalue()

    def test_prints_the_configured_store(self):
        d = Path(tempfile.mkdtemp())
        (d / "config").mkdir()
        (d / "config" / "store-X.yaml").write_text(
            "store_code: SCN231409\nerp_store_name: 青岛新业广场店\nmarker: Y\n",
            encoding="utf-8")
        out = self._run_selftest(d)
        self.assertIn("门店", out)
        self.assertIn("SCN231409", out)
        self.assertIn("青岛新业广场店", out)
        self.assertIn("标识 Y", out)

    def test_warns_when_the_store_is_not_configured(self):
        d = Path(tempfile.mkdtemp())
        (d / "config").mkdir()
        (d / "config" / "store-X.yaml").write_text(
            'store_code: ""\nerp_store_name: ""\n', encoding="utf-8")
        out = self._run_selftest(d)
        self.assertIn("没配全", out)
        self.assertIn("别的店", out, "要提醒配错的后果")


class TestAutostartModeIsPlumbed(unittest.TestCase):
    def test_api_accepts_an_explicit_elevated_flag(self):
        import inspect
        src = inspect.getsource(web.Handler._api)
        self.assertIn('body.get("elevated")', src,
                      "接口没接 elevated —— 界面上的「启动方式」就白做了")


if __name__ == "__main__":
    unittest.main()
