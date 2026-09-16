r"""安装时到底用了哪个 Python —— 记下来，以后一律用它。

**为什么需要这个**：一台电脑上装两个 Python 是常态。
门店的 Win7 老机器只能装 3.8.10（3.9 以上不支持 Win7），新机器装 3.14；
有的机器上还躺着 Anaconda、以及 Windows 应用商店那个**假别名**。
于是 `install.bat` 把依赖装进了 A，过两天双击 `start.bat` 探测到的却是 B ——
报错是 `ModuleNotFoundError: No module named 'requests'`，
看着像"当初没装成功"，实际是"装到另一个 Python 去了"。
`bootstrap.py` 里那句「多半是双击 bat 用的 Python 跟装依赖的不是同一个」
就是这个坑的化石。

所以安装成功时把**这一次用的解释器**记下来：

* 存 `.secrets/python.txt` —— 属于 `selfupdate.NEVER_TOUCH`，
  升级覆盖代码时**不会**把它冲掉（记在项目根或 `src/` 里都会被覆盖）；
* **纯文本、两行** —— 第一行是解释器绝对路径（**带引号**），第二行是版本号。
  `.bat` 用 `set /p` 只读第一行就能用，不需要解析 JSON；
  带引号存是为了让 bat 里 `%PYBIN% bootstrap.py` 直接展开就对 ——
  Python 常装在 `C:\Program Files\...`，路径带空格，
  在 bat 里判断"要不要加引号"很容易出错。
* 版本号单独一行是**给人看的**：`selftest` 能直接说出
  "安装时用的是 3.8.10"，不用再起一个子进程去问。

**它是提示不是法律**：记录里的路径不存在了（Python 被卸载、挪走、装机镜像换了），
就当没有这回事，退回去按 PATH 探测。所以这里只做"读出来 → 校验 → 返回"，
任何一步不确定就返回 None，**绝不抛异常** —— 这个模块会在启动路径上被调用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

# 项目根。跟 src/version.py 一样**用自己的位置算**，不借别人的 ——
# 安装布局和仓库布局是同一套（见 selfupdate 顶部），所以这里算出来的
# 永远是"这份代码所在的项目根"。
ROOT = Path(__file__).resolve().parent.parent

REL = Path(".secrets") / "python.txt"

# 运维/测试用的旁路：设了它就以它为准，连文件都不用有。
# 门店用不到，但排查"到底在用哪个 Python"时很省事。
ENV_OVERRIDE = "CBG_PYTHON"


def path_for(root=None) -> Path:
    return Path(root or ROOT) / REL


def _unquote(s: str) -> str:
    s = (s or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].strip()
    return s


def _usable(exe: str) -> bool:
    """这个路径现在还能用吗。

    ⚠ 用 `is_file()` 而不是 `exists()` —— 目录也能 exists，
    而把一个目录当解释器传给 `subprocess` 会得到一个很难懂的错误。
    """
    if not exe:
        return False
    try:
        return Path(exe).is_file()
    except OSError:                      # 路径里有非法字符 / 太长
        return False


def pinned(root=None) -> Optional[str]:
    """安装时记下的解释器绝对路径。没有 / 不可用 → None。"""
    env = _unquote(os.environ.get(ENV_OVERRIDE, ""))
    if env:
        # ⚠ 环境变量走的是"我说了算"：**故意不校验存在性**。
        #   排查时经常要指向一个还没建的路径试行为，卡在这儿反而碍事。
        return env
    try:
        lines = path_for(root).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return None
    exe = _unquote(lines[0]) if lines else ""
    return exe if _usable(exe) else None


def pinned_version(root=None) -> str:
    """记录里的版本号（给界面/自检显示用）。没有 → 空串。"""
    try:
        lines = path_for(root).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return ""
    return lines[1].strip() if len(lines) > 1 else ""


def pythonw_for(exe: str) -> str:
    """同目录下有 `pythonw.exe` 就返回它 —— 它**不带控制台窗口**，开机时不闪黑框。

    ⚠ 这里**故意不判断平台**：判断留给调用方。`autostart` / `schedule` / `cli`
    各有各的 `platform.system()` 判断（测试也是照着那几个函数钉的），
    在这个底层小函数里再判一次，等于同一个条件写两遍、还容易两边不一致。
    """
    try:
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.is_file():
            return str(pyw)
    except OSError:                      # 路径非法 / 太长
        pass
    return exe


def current() -> str:
    """现在该用哪个解释器：安装时记的优先，否则就是当前进程自己。

    ⚠ 退回 `sys.executable` 是**必须**的：第一次安装时还没有记录文件，
    而那时候当前进程恰恰就是用户双击 bat 探测出来的那个 Python，
    也就是马上要把依赖装进去的那个 —— 正是我们要的。
    """
    return pinned() or sys.executable or "python"


def record(root=None, exe: Optional[str] = None) -> Optional[Path]:
    """把这次用的解释器记下来。失败**不抛异常**（调用方是安装流程，
    记不下来最多退回老行为，不该因此让安装失败）。返回写成的路径 / None。
    """
    exe = exe or sys.executable
    if not exe:
        return None
    ver = "{}.{}.{}{}".format(*sys.version_info[:3],
                              "" if sys.version_info[3] == "final"
                              else "-" + str(sys.version_info[3]))
    p = path_for(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # 带引号存（bat 直接用），版本号第二行（只给人看；`set /p` 只读第一行）
        p.write_text(f'"{exe}"\n{ver}\n', encoding="utf-8")
    except OSError:
        return None
    return p


def same_install(a: str, b: str) -> bool:
    """两个解释器路径是不是**同一个 Python 安装**。

    ⚠ 比的是**目录**不是文件名：记录里存的是 `python.exe`，
    而控制台和后台服务是 `pythonw.exe` 起来的 —— 同一个 Python、两个 exe 名。
    比文件名会把每一台机器都误报成"跟现在跑的不是同一个"，
    然后所有人学会无视这条警告。
    """
    if not a or not b:
        return False
    try:
        return Path(str(a)).parent == Path(str(b)).parent
    except (TypeError, ValueError, OSError):
        return False


def describe(root=None) -> dict:
    """给自检/界面用的一句话描述。"""
    exe = pinned(root)
    return {
        "pinned": bool(exe),
        "python": exe or sys.executable,
        "version": pinned_version(root) if exe else "",
        "running": sys.executable,
        "running_version": "{}.{}.{}".format(*sys.version_info[:3]),
        "file": str(path_for(root)),
    }
