r"""按需提权：**只在真正需要管理员的那一步**弹一次 UAC。

## 为什么需要这个模块

2026-09-16 起，这个项目**默认全程不需要管理员**：
装依赖、跑对账、起控制台、抓华为会话、注册开机自启，全都是普通权限。
唯一真正需要管理员的是两件**一次性的清理/注册**动作：

1. 删掉**老版本留下的**那条"以管理员身份启动"计划任务 ——
   它是以管理员身份创建的，普通权限删不掉。而它留着的话，每次登录照样把服务
   以管理员拉起来，于是「自动抓华为会话」永远坏着，界面还显示"普通权限"。
2. 覆盖一条**由管理员创建过**的定时任务（`schtasks /create ... /f`）。

这两件事**不该**逼用户"右键 install.bat 以管理员身份运行" ——
那会把 `pip install` 也一起提权跑掉（项目专门在 `bootstrap.py` 里警告过这个坑：
标准用户 + 管理员密码的机器上，提权后的 pip 会把包装进**另一个账号**）。

所以要的是"**只把这一步提权**"。这就是本模块的全部职责。

## 怎么拿到提权子进程的结果

提权走 `ShellExecuteW(..., "runas", ...)` —— 它**另起一个进程**，
父子之间没有管道，子进程的 stdout 我们拿不到（服务本身还是 `pythonw` 起的，
连控制台都没有）。所以约定：

* 父进程给子进程一个 `--result-file <路径>`；
* 子进程跑完把结果写成 JSON 放那儿；
* 父进程**轮询**那个文件，读到就返回。

拿不到结果（用户点了"否"、子进程崩了、超时）→ 返回 `None`，
调用方按"没成，让用户自己看着办"处理。**任何情况都不抛异常** ——
这条路是在 HTTP 请求里走的，抛出去就是界面上一个 500。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# 项目根。跟 src/version.py 一样**用自己的位置算**。
ROOT = Path(__file__).resolve().parent.parent


def is_windows() -> bool:
    """是不是 Windows。

    ⚠ **单独抽成一个函数，是为了测试能 patch 它**，而不是去 patch `os.name`：
    `mock.patch.object(os, "name", "nt")` 改的是**全局**的 `os.name`，
    而 `ctypes/__init__.py` 自己也读它 —— 在 macOS 上这么一 patch，
    `import ctypes` 会直接 `ImportError`（真踩了，五条测试一起崩）。

    ⚠ 也别改用 `platform.system()`：那是个**模块属性**，
    `mock.patch.object(某模块.platform, "system", ...)` 同样会全局生效，
    在 macOS 上被 patch 成 "Windows" 就会去调 `ctypes.windll` 而崩
    （`winutil.py` 里踩过同款）。
    """
    return os.name == "nt"


def is_admin() -> bool:
    """当前进程是不是以管理员身份在跑。非 Windows 恒为 False。"""
    if not is_windows():
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                        # noqa: BLE001
        return False


# UAC 的两个开关都在这一个注册表键下面
_UAC_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System"


def _uac_flags() -> dict:
    """读 UAC 的开关。读不到就给空 dict —— **别抛**，这是启动路径上的东西。"""
    if not is_windows():
        return {}
    try:
        import winreg
        out = {}
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _UAC_KEY) as k:
            for name in ("EnableLUA", "FilterAdministratorToken"):
                try:
                    out[name] = int(winreg.QueryValueEx(k, name)[0])
                except OSError:
                    pass
        return out
    except OSError:
        return {}


def always_admin_reason() -> str:
    """这台电脑上"为什么每个程序都是管理员、而且不弹授权框"。

    查不出来就返回空串；查出来就是一段**能直接显示给用户看**的话。

    ⚠ **纯文本，不要 Markdown**（`**加粗**`、反引号都不行）：
    它会被前端 `esc()` 之后原样打进 `innerHTML` —— `esc()` 只转义 HTML，
    **不渲染 Markdown**，于是用户看到的是一坨 `**内置 Administrator 账户**`，
    又长又难读。实测就是这么被看到的。
    换行用 `\n`，前端那边用 `white-space: pre-line` 显示。

    ## 为什么要有这个

    实测踩过：一台门店电脑上，**任何**程序选「以管理员身份运行」都不弹 UAC，
    我们的服务不管怎么启动都是管理员 —— 于是 Edge 拒绝以管理员运行，
    「自动抓会话」永远失败。用户照着提示改了「启动方式」、还把 UAC 滑块
    拉到最高，**全都没用** —— 因为原因根本不在我们代码里，而在 Windows 账户本身：

    * **用的是内置 Administrator 账户**（RID 500），而它的"管理员批准模式"
      默认是**关**的：`FilterAdministratorToken` **默认值 0**。
      这个设置下，它启动的**每一个**进程都直接拿完整管理员令牌，
      **永远不会弹 UAC**。
      ⚠ **拉高 UAC 滑块对这个账户没有任何影响** —— 滑块管的是
      `ConsentPromptBehaviorAdmin`，那是给"普通管理员账户"用的。
    * 或者干脆 `EnableLUA=0`（UAC 整个关掉）。

    两条都**不是我们的代码能改的**。只能**说清楚 + 给出能照着做的解法** ——
    否则用户会把系统的毛病当成程序的毛病，反复试、反复失败。
    """
    flags = _uac_flags()
    if not flags:
        return ""
    if flags.get("EnableLUA") == 0:
        return (
            "这台电脑的 UAC 是关掉的（EnableLUA=0）——\n"
            "任何程序都直接以管理员身份跑，也永远不会弹授权框，\n"
            "所以「自动抓会话」用不了。\n"
            "\n"
            "解法：控制面板 →「用户账户」→「更改用户账户控制设置」，\n"
            "把滑块往上调一格，然后重启。\n"
            "之后普通双击 start.bat 就是普通权限了。")
    if flags.get("FilterAdministratorToken") != 1 and _looks_like_builtin_admin():
        return (
            "这台电脑登录的是「内置 Administrator 账户」，而它默认不受 UAC 管\n"
            "（FilterAdministratorToken=0）—— 它启动的每个程序都是管理员，\n"
            "永远不会弹授权框。\n"
            "\n"
            "别再去改「启动方式」了，这台机器上它改不出普通权限；\n"
            "把 UAC 滑块拉到最高也没用，那是给普通管理员账户用的另一条设置。\n"
            "\n"
            "解法二选一：\n"
            "  ① 建一个普通用户账户，用它登录来跑这个程序（最省事）\n"
            "  ② 让内置 Administrator 也受 UAC 管 —— 在管理员命令行里跑：\n"
            "       reg add \"HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion"
            "\\Policies\\System\" /v FilterAdministratorToken /t REG_DWORD /d 1 /f\n"
            "     然后重启（之后提权操作会开始弹框，这是正常的）")
    return ""


def _looks_like_builtin_admin() -> bool:
    r"""看着像不像"内置 Administrator"。

    ⚠ 只看环境变量（账户名 / 用户目录名），**不查 SID**：查 SID 要
    `GetTokenInformation` + 手工解析，几十行 ctypes，而这里只是个提示文案 ——
    判错的代价是"多说一句或漏说一句"，不值得为它引入一段
    **没有人能在这台机器上验证**的代码。内置 Administrator 默认就叫
    `Administrator`（用户目录也是），被改过名的少之又少；
    没判出来最多退回原来那句泛泛的提示，不会更糟。
    """
    user = (os.environ.get("USERNAME") or "").strip().lower()
    home = (os.environ.get("USERPROFILE") or "").strip().replace("/", "\\").lower()
    return user == "administrator" or home.endswith("\\administrator")


def console_python() -> str:
    """**带控制台**的 python.exe —— 提权弹窗要让人看得见结果。

    ⚠ 不能用 `sys.executable`：后台服务是 `pythonw.exe` 起的，它没有控制台；
    拿它去提权，用户会看到一个 UAC 弹窗，然后**什么都没有** ——
    既不知道成没成，也不知道错在哪。
    """
    exe = Path(sys.executable or "python")
    if is_windows():
        py = exe.with_name("python.exe")
        try:
            if py.is_file():
                return str(py)
        except OSError:
            pass
    return str(exe)


def run_elevated(script: Path, args: list, timeout: float = 90.0,
                 pause: bool = False) -> Optional[dict]:
    """把 `python <script> <args...> --result-file <临时文件>` **提权**跑一次。

    返回子进程写回来的结果 dict；没拿到（UAC 被拒 / 超时 / 崩了）返回 None。

    ⚠ 提权子进程会**另开一个窗口**：这样用户能看到它在干什么、成没成。
    跑完窗口会停住（`--pause`）等按回车 —— 否则一闪而过，用户以为没反应。
    """
    if not is_windows():
        return None

    fd, tmp = tempfile.mkstemp(prefix="cbg-elevate-", suffix=".json")
    os.close(fd)
    try:
        os.unlink(tmp)                       # 让它一开始不存在，好判断"写没写"
    except OSError:
        pass

    argv = [str(script), *[str(a) for a in args], "--result-file", tmp]
    if pause:
        argv.append("--pause")
    params = subprocess.list2cmdline(argv)

    try:
        import ctypes
        # ShellExecuteW 的目录参数用项目根：提权后的进程 CWD 是 system32，
        # 不给它就会在 system32 下找相对路径。
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", console_python(), params, str(ROOT), 1)
    except Exception:                        # noqa: BLE001
        return None
    # 约定：返回值 <= 32 表示失败。5 = 拒绝访问（用户点了"否"）
    if rc <= 32:
        return None

    end = time.time() + timeout
    while time.time() < end:
        got = _read_json(tmp)
        if got is not None:
            return got
        time.sleep(0.3)
    return None


def _read_json(path: str) -> Optional[dict]:
    """读结果文件。**没写完 / 是空的 / 语法不对都返回 None**，下次再读。

    ⚠ 必须容忍"读到半个文件"：子进程是**先创建再写**的，中间有一瞬间是空文件。
    这里不重试就当成坏结果的话，会偶发地丢掉一次成功。
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if not raw.strip():
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def cleanup_result_file(path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass
