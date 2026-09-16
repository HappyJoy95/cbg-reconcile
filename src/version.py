"""构建指纹 —— 一眼看出这台电脑上跑的是哪一版。

**为什么需要这个**：门店电脑上跑的常常是**拷过去的旧版本**。
旧版本的报错文案、界面、行为都跟新版本不一样，但没有任何地方能看出来 ——
于是变成"我明明修好了 / 你那边怎么还报这个"，白白来回一整轮。

有了它：
* `selftest.bat` 第 0 节会打印构建时间；
* 控制台右上角会显示；
* `/api/health` 里也有。

三个来源，按优先级：

1. `BUILD.txt` —— 打包时 `tools/build_package.sh` 写时间戳；从 GitHub 自更新
   之后由 `src/selfupdate.py` 写成 `GitHub main · v1.2.0`。
2. `.git/HEAD` —— **clone 下来的仓库**没有 `BUILD.txt`（那文件是未跟踪的），
   但 `.git` 里就有分支和 commit。直接读文件，**不调 git 命令**：
   门店电脑上不装 git，装到一半的机器上也未必有。
3. 都没有 —— 写"源码运行（未打包）"，说明这棵树上什么线索都没有。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

APP_NAME = "CBG报量对账"
VERSION = "1.6.1"           # 功能有增删就动这个；打包时间看 BUILD.txt
                            # 1.6.1：抓会话撞图形验证码时**当场醒目提示**并收尾 ——
                            #        自动登录撞的：中止 + 关窗 + 重置 profile；
                            #        手动登录撞的：只提示、不打扰用户操作。
                            #        并记住"上次撞过"，下次不自动填账号密码
                            #        （否则重试会撞同一个验证码，死循环）。
                            # 1.6.0（主线：**兼容 Windows 7**）
                            #        ① 支持 Windows 7 / Python 3.8.10（代码降到 3.8 能跑，
                            #           三头 3.8.20 / 3.9.25 / 3.14 各自跑全套测试）
                            #        ② 安装时记住解释器（多 Python 机器不装错）
                            #        ③ 全程不再需要管理员权限（装机那次 UAC 也取消了）
                            #        ④ 定时任务：提权注册 + 自己记一份注册参数
                            #           （`.secrets/schedule.json`），提权建的任务
                            #           照样看得到时间和命令

ROOT = Path(__file__).resolve().parent.parent
BUILD_FILE = ROOT / "BUILD.txt"
UNPACKAGED = "源码运行（未打包）"


def _git_dir(root: Optional[Path] = None) -> Path:
    """`.git` 的位置。可能是目录（普通 clone），也可能是个文件（worktree）。"""
    base = ROOT if root is None else Path(root)
    p = base / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            line = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return p
        if line.lower().startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            q = Path(target)
            return q if q.is_absolute() else (base / q)
    return p


def _read_sha(git: Path, ref: str) -> str:
    """按 ref 名找 commit sha。refs 缺失时退到 packed-refs。"""
    try:
        sha = (git / ref).read_text(encoding="utf-8").strip()
    except OSError:
        sha = ""
    if sha:
        return sha
    try:                                   # gc 过之后 ref 会挪进 packed-refs
        for line in (git / "packed-refs").read_text(
                encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    except OSError:
        pass
    return ""


def git_revision(root: Optional[Path] = None) -> Optional[Tuple[str, str]]:
    """从 `.git` 读出 `(分支, 短 sha)`。不是 git 仓库就返回 None。

    detached HEAD（sha 直接写在 HEAD 里）也能认，分支名留空。
    """
    git = _git_dir(root)
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head:
        return None

    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        if not ref:                        # `ref: ` 后面是空的 —— 不成形，别当分支
            return None
        sha = _read_sha(git, ref)
        branch = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ""
    else:
        sha, branch = head, ""             # detached：HEAD 里就是 sha

    if not sha:
        return None
    return branch, sha[:7]


def git_stamp(root: Optional[Path] = None) -> str:
    """`git main@b0250d2`；读不出来就返回空串。

    `root` 给 `bootstrap.py` 用 —— 它得问"**这个**目录是不是 clone 来的"。
    """
    got = git_revision(root)
    if not got:
        return ""
    branch, short = got
    return f"git {branch}@{short}" if branch else f"git {short}"


def build_id() -> str:
    """构建指纹。优先级：BUILD.txt > .git > "源码运行（未打包）"。"""
    try:
        txt = BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        txt = ""
    return txt or git_stamp() or UNPACKAGED


def packaged() -> bool:
    """是不是"装出来的"。

    clone 下来的算装好了 —— 它有自己的版本线索（见 `build_id`），
    而 `BUILD.txt` 存在的意义就是区分"有线索"和"什么都没有"。
    """
    return build_id() != UNPACKAGED


def describe() -> str:
    """一行说清楚：`CBG报量对账 v1.2.0 · 2026-09-15 13:42`"""
    return f"{APP_NAME} v{VERSION} · {build_id()}"
