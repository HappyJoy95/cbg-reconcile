"""开机自动启动的注册与取消。

| 平台 | 机制 | 权限 |
|---|---|---|
| Windows（**默认**） | 注册表 `HKCU\\...\\Run` | 普通权限，**不需要管理员、不弹 UAC** |
| Windows（可选） | 登录触发的计划任务，`RunLevel=HighestAvailable` | 以管理员身份，注册时要一次 UAC |
| macOS | `~/Library/LaunchAgents/com.cbg-reconcile.plist` | — |
| Linux | `~/.config/autostart/cbg-reconcile.desktop` | — |

**默认为什么是注册表 Run 项**：写的是 `HKEY_CURRENT_USER` —— 当前用户自己的
注册表分支，跟写自己的"文档"目录一个性质，**任何用户都能写，一次 UAC 都不用弹**。

⚠ **这里以前默认是"以管理员身份启动"（计划任务那条），2026-09-16 改掉了。**
原因不是洁癖，是那条路会把**自动抓华为会话彻底弄坏**：
服务以管理员身份跑 → 它拉起来的 Edge / Chrome 也是管理员 → 而浏览器
（Chrome 138 起明确禁止）**拒绝以管理员运行**，把命令行交棒出去就自己退 0 ——
我们给的 `--user-data-dir` / `--remote-debugging-port` 落不到任何活着的实例上，
调试端口永远没人监听。实测症状是「Edge 启动后立刻退出（退出码 0）」，
而链接跑到了用户原来那个浏览器里。

更要命的是这个代价**换不来任何东西**：查过一遍，整个项目除了"把自己设成管理员"
之外没有任何一处真需要管理员 ——
每天那条定时对账任务的 `schtasks /create` **不带 `/rl`**，本来就是普通权限跑的；
程序目录在 `D:\\cbg-reconcile`，普通用户就能写。

计划任务那条路**保留着**（`elevated=True`，界面上还能选），因为它是 Windows 上
唯一"静默提权"的正规做法，将来真有需要时还在；但界面上会明说它会让抓会话不可用。

启动的是项目根目录下的 `boot.py`，它自己 `chdir` 到项目根再起服务 ——
**不依赖任务/注册表里配工作目录**。
"""

from __future__ import annotations

import os
import ntpath
import platform
import subprocess
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .. import version
from .winutil import decode, parse_xml, schtasks, xml_text
from . import runtime

APP_ID = "cbg-reconcile"
#: ⚠ **别在这儿再写一份** —— 从 `version` 拿（原来两处各写了一遍 `CBG报量对账`，
#: 改名时只改一处就漏一处）。
APP_NAME = version.APP_NAME

#: 改过名之前的那些名字。**开机自启的注册表键名 + 计划任务名都跟着变**，
#: 所以建新的时必须顺手删旧的 —— 不删的话 Windows 那边**两份并存、开机启两次**
#: （跟定时任务改名那次一模一样的坑）。
LEGACY_APP_NAMES = ("CBG报量对账",)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 选了"以管理员身份启动"时要跟着一起返回的警告。
# ⚠ 单独拎出来是因为**三处都要说同一句话**（界面、命令行、发布说明），
#   分散写迟早漏一处 —— 而漏掉的那处正好就是把人坑了的那处。
ELEVATED_BREAKS_CAPTURE = (
    "⚠️ 注意：以管理员身份运行时，**「自动抓华为会话」会失败** —— "
    "Edge / Chrome 拒绝以管理员运行，会把命令行交棒出去然后自己退出，"
    "我们等不到调试端口。真要用抓会话，把「启动方式」改回**普通权限**。")

# 开机自启用的计划任务名。带后缀，跟「定时执行」那个（`schedule.TASK_NAME`）区分开 ——
# 用户在任务表里一眼能看出哪个是"开机常驻"、哪个是"每天跑一次"。
AUTOSTART_TASK = f"{APP_NAME}-开机自启"

#: **所有**开机自启的任务名（含旧名字的）。
#:
#: ⚠⚠ 这个元组是给 `schedule._is_ours` 用的，**不是可有可无的**：
#:   旧名字那个 `CBG报量对账-开机自启` 正好**以 `CBG报量对账` 开头**，
#:   而那是 `LEGACY_TASK_NAMES` 里的一项 ⇒ 它会被当成"老名字的定时任务"
#:   列到「定时执行」那张表上、让门店点「删除」。
#:   **删了等于把开机自启杀了**（而用户完全不知道服务为什么不再自启）——
#:   这正是 `_is_ours` 那段注释警告的事，只是换名字会让它重新发生。
AUTOSTART_TASK_NAMES = (AUTOSTART_TASK,) + tuple(
    f"{n}-开机自启" for n in LEGACY_APP_NAMES)

# 任务计划 XML 的命名空间。标签名固定英文，不受系统语言影响
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / f"com.{APP_ID}.plist"
LINUX_DESKTOP = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"


class AutostartError(RuntimeError):
    pass


def boot_script(root) -> Path:
    return Path(root) / "boot.py"


def _python_exe() -> str:
    """开机自启用哪个解释器。

    **优先用安装时记下的那一个**（`src/runtime.py`）—— 这台电脑上可能有两个
    Python（Win7 老机器只有 3.8.10 能装，新机器 3.14），依赖只装在其中一个里。
    开机自启用错的那个，表现是"每天开机都静静地起不来"，界面上什么都看不到，
    只有 `out/autostart.log` 里一行 ImportError。

    Windows 上再换成 `pythonw.exe` —— 它**不带控制台窗口**，开机时不会闪黑框。
    """
    exe = runtime.current()
    if platform.system() == "Windows":
        return runtime.pythonw_for(exe)
    return exe


def command(root) -> str:
    return f'"{_python_exe()}" "{boot_script(root)}"'


def kind() -> str:
    s = platform.system()
    return "windows" if s == "Windows" else ("macos" if s == "Darwin" else "linux")


# ------------------------------------------------------------------ Windows
def is_elevated() -> bool:
    """当前进程是不是以管理员身份在跑。

    ⚠ 只在 Windows 上有意义；别的平台返回 False（那边没有这套概念）。
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _pythonw() -> str:
    """计划任务里用 pythonw.exe —— 它**不带控制台窗口**，开机时不会闪黑框。"""
    return _python_exe()


def task_xml(root) -> str:
    """开机自启任务的 XML。

    **为什么用 XML 而不是 `schtasks /tr` 命令行参数**：
    1. `/tr` 只能收一整条命令行，路径带空格时得在里面塞引号，而 subprocess 在
       Windows 上会走 list2cmdline 把它转义成 `\"…\"`，schtasks 不认 —— 之前踩过。
       XML 的 `<Command>` / `<Arguments>` 是**分开的两个元素**，各自转义，没有这个坑。
    2. `RunLevel=HighestAvailable`（提权）和 `ExecutionTimeLimit=PT0S`
       （不限时长）在命令行上没有对应参数，只能走 XML。

    ⚠ `ExecutionTimeLimit` **必须显式写 `PT0S`**。默认值是 `PT72H` ——
    服务跑满 3 天会被任务计划程序**直接杀掉**，而且没有任何提示。

    ⚠ 生成的 XML 里**不留注释**：`schtasks` 的解析校验我没法在本机验证，
    就不给它增加输入面。解释都写在 Python 这边。
    """
    # ⚠ Command 是**单个文件路径**，加了引号 schtasks 就找不到文件；
    #   Arguments 是命令行参数，路径带空格时**必须**加引号，否则 pythonw 会把
    #   `D:\门店 测试\cbg-reconcile\boot.py` 拆成两个参数。
    exe = xml_escape(_pythonw())
    script = xml_escape(f'"{boot_script(root)}"')
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{TASK_NS}">
  <RegistrationInfo>
    <Description>{xml_escape(APP_NAME)} 控制台 —— 登录后自动在后台启动（以管理员身份运行）</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{exe}</Command>
      <Arguments>{script}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _win_task_info() -> dict | None:
    """读任务详情。任务不存在 / 读不出来 → None。"""
    r = schtasks(["/query", "/tn", AUTOSTART_TASK, "/xml"])
    if r is None or r.returncode != 0:
        return None
    from .winutil import parse_xml
    root = parse_xml(r.stdout)
    if root is None:
        return None
    return {
        "command": " ".join(x for x in (xml_text(root, "Command"),
                                        xml_text(root, "Arguments")) if x),
        "run_level": xml_text(root, "RunLevel"),
        "enabled": xml_text(root, "Settings/Enabled").lower() == "true",
    }


def _win_runkey_value() -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
            return val
    except (FileNotFoundError, OSError):
        return None


def _split_windows_command(value: str) -> list[str]:
    """拆解 Run 项的简单命令行；不支持引号内转义时会保守返回空列表。"""
    text = str(value or "").strip()
    parts = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        if text[index] == '"':
            end = text.find('"', index + 1)
            if end < 0:
                return []
            parts.append(text[index + 1:end])
            index = end + 1
            if index < len(text) and not text[index].isspace():
                return []
        else:
            end = index
            while end < len(text) and not text[end].isspace():
                end += 1
            parts.append(text[index:end])
            index = end
    return parts


def _runkey_action_is_ours(value: str, root) -> bool:
    parts = _split_windows_command(value)
    if len(parts) != 2:
        return False
    executable, script = parts
    actual_script = ntpath.normcase(ntpath.normpath(script))
    expected_script = ntpath.normcase(ntpath.normpath(str(boot_script(root))))
    return (ntpath.basename(executable).casefold() in
            ("python.exe", "pythonw.exe", "python", "pythonw")
            and actual_script == expected_script)


def _runkey_ownership(name: str, root) -> str:
    """区分当前用户 Run 项不存在、本程序所有、外部所有和不可读取。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "owned" if _runkey_action_is_ours(value, root) else "foreign"


def _win_status(root=None) -> dict:
    if root is None:
        from ..paths import ROOT
        root = ROOT
    task_state = _task_ownership(AUTOSTART_TASK, root)
    task = _win_task_info() if task_state == "owned" else None
    if task:
        return {
            "installed": True,
            "mode": "task",
            "elevated": task["run_level"].lower() == "highestavailable",
            "registered": task["command"],
            "task_name": AUTOSTART_TASK,
            "run_level": task["run_level"],
        }

    val = _win_runkey_value()
    if val and _runkey_action_is_ours(val, root):
        # 退路：注册表 Run 项 —— 能开机自启，但**不是管理员**
        info = {"installed": True, "mode": "runkey", "elevated": False,
                "registered": val, "task_name": AUTOSTART_TASK}
        if task_state != "absent":
            info["ownership_unverified"] = True
            info["ownership_message"] = "发现同名计划任务，但无法确认其动作归属；该任务未被本程序操作。"
        return info

    if val or task_state != "absent" or task_state == "unknown":
        return {"installed": False, "mode": None, "elevated": False,
                "task_name": AUTOSTART_TASK, "ownership_unverified": True,
                "ownership_message": "发现同名启动项，但无法确认归属；为保护其他程序，本程序不会覆盖或删除它。"}
    return {"installed": False, "mode": None, "elevated": False,
            "task_name": AUTOSTART_TASK}


def _write_task_xml(root) -> Path:
    """schtasks 要求 XML 是 **Unicode**（UTF-16）—— 我们这里带中文描述，
    写成 UTF-8 有可能被拒。用 utf-16 写（Python 会带 BOM）。

    ⚠ 文件名里**带进程号**（2026-09-19 加）：原来是系统临时目录里一个**固定名字**，
    两个进程同时跑（比如开发机上并行跑两套测试）会互相覆盖 —— 实测就是这么假红了一条。
    写法仍然"看得见、找得到"，只是不再共用同一个文件。
    """
    path = Path(tempfile.gettempdir()) / f"{APP_ID}-autostart-task-{os.getpid()}.xml"
    path.write_text(task_xml(root), encoding="utf-16")
    return path


def _write_launch_bat(root) -> Path:
    """退路二用的启动脚本 —— `/tr` 只能给一个路径，所以把带引号的命令行塞进 bat。

    （XML 那条路能用上就不会走这里；留着是因为不同 Windows 版本对任务 XML
    的校验宽严不一，多一条路少一次白跑。）
    """
    path = Path(root) / "boot-run.bat"
    with open(path, "w", encoding="ascii", newline="") as stream:
        stream.write(_launch_bat_body(root))
    return path


def _launch_bat_body(root) -> str:
    """返回退路启动脚本内容，供写盘和任务归属核对共用。"""
    body = (
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        f'start "" "{_pythonw()}" "{boot_script(root)}"\r\n'
    )
    return body


def _task_lookup(task_name: str, root, task_names=None) -> tuple[str, str]:
    """返回 (absent/owned/foreign/unknown, 完整任务名)。

    仅凭名字操作 Windows 任务会误覆盖或误删其他软件的任务。先用任务列表
    区分“不存在”和“列表不可读”，再核对动作确实启动这份安装目录里的 boot.py。
    """
    if task_names is None:
        try:
            from . import schedule
            task_names, readable = schedule._win_task_names_query()
        except Exception:  # noqa: BLE001
            return "unknown", ""
    else:
        readable = True
    if not readable:
        return "unknown", ""
    if root is None:
        from ..paths import ROOT
        root = ROOT

    wanted = task_name.rsplit("\\", 1)[-1].casefold()
    matches = [str(name) for name in task_names
               if str(name).rsplit("\\", 1)[-1].strip().casefold() == wanted]
    if not matches:
        return "absent", ""
    if len(matches) != 1:
        return "unknown", ""

    full_name = matches[0]
    r = schtasks(["/query", "/tn", full_name, "/xml"])
    if r is None or r.returncode != 0:
        return "unknown", full_name
    task = parse_xml(r.stdout)
    if task is None:
        return "unknown", full_name

    command = xml_text(task, "Command").strip().strip('"')
    arguments = xml_text(task, "Arguments").strip()
    expected_script = ntpath.normcase(ntpath.normpath(str(boot_script(root))))
    argument_path = ""
    if (len(arguments) >= 2 and arguments[0] == arguments[-1] == '"'
            and '"' not in arguments[1:-1]):
        argument_path = arguments[1:-1]
    elif arguments and not any(ch.isspace() for ch in arguments) and '"' not in arguments:
        argument_path = arguments
    if (argument_path
            and ntpath.normcase(ntpath.normpath(argument_path)) == expected_script
            and ntpath.basename(command).casefold() in ("python.exe", "pythonw.exe")):
        return "owned", full_name

    expected_bat = ntpath.normcase(ntpath.normpath(str(Path(root) / "boot-run.bat")))
    actual_command = ntpath.normcase(ntpath.normpath(command))
    if actual_command == expected_bat and not arguments:
        try:
            content = (Path(root) / "boot-run.bat").read_text(encoding="ascii")
        except OSError:
            return "unknown", full_name
        if content == _launch_bat_body(root):
            return "owned", full_name
    return "foreign", full_name


def _task_ownership(task_name: str, root) -> str:
    """为调用方提供不含任务路径细节的归属判定。"""
    return _task_lookup(task_name, root)[0]


def remove_task_if_owned(task_name: str, root, task_names=None) -> tuple[bool, str]:
    """只删除已核实属于本安装目录的自启任务。"""
    state, full_name = _task_lookup(task_name, root, task_names)
    if state == "absent":
        return True, ""
    if state != "owned":
        reason = "无法读取任务归属" if state == "unknown" else "任务同名但动作不属于本程序"
        return False, f"{reason}，未删除「{task_name}」"
    r = schtasks(["/delete", "/tn", full_name, "/f"])
    if r is not None and r.returncode == 0:
        return True, f"已删除开机启动任务「{task_name}」"
    out = decode((r.stdout or b"") if r is not None else b"").strip()
    if not out and r is not None:
        out = decode(r.stderr or b"").strip()
    return False, (out.splitlines()[0][:160] if out else "删除开机启动任务失败")


def _win_install(root, elevated: bool | None = None) -> dict:
    """注册开机自启。

    `elevated`：
      * `False` 或 `None` —— 写注册表 `HKCU\\...\\Run`（普通权限）。**这是默认。**
      * `True` —— 登录触发的计划任务 + `HighestAvailable`（以管理员身份，注册时要 UAC）

    ⚠ **`None` 走普通权限，不猜当前进程**。这里以前是 `elevated = is_elevated()` ——
    从提权进程里调就会静默注册成"以管理员身份启动"，而管理员身份会让
    **自动抓会话彻底不可用**（浏览器拒绝以管理员运行，见模块顶部的说明）。
    "猜"这个默认值踩过一次，别再改回去。

    两条路**互斥** —— 装一条就把另一条清掉，否则会互相打架
    （虽然 pidfile 挡住第二个实例，但任务列表里会多一条没人认识的）。
    """
    if not elevated:
        ownership = _task_ownership(AUTOSTART_TASK, root)
        if ownership in ("foreign", "unknown"):
            why = ("任务同名但动作不属于本程序" if ownership == "foreign"
                   else "无法读取计划任务列表或任务动作")
            return {"ok": False, "mode": None, "elevated": False,
                    "message": f"{why}，为避免重复启动或误删，未添加注册表启动项。"}
        res = _install_runkey(root)
        if res.get("ok"):
            # 从"管理员"切回普通权限：必须把旧的提权任务清掉。
            # ⚠ 删不掉**一定要报出来** —— 否则界面上写着"普通权限"，
            #   开机照样被那条任务以管理员拉起来，问题原样存在还更难查。
            gone, msg = _drop_task(root)
            if not gone:
                res = dict(res)
                res["message"] = (
                    f"{res['message']}，但**旧的提权任务没删掉**：{msg}。"
                    "那条任务会在下次登录时以管理员身份把服务拉起来，"
                    "「自动抓会话」会因此失败 —— 请右键 install.bat →「以管理员身份运行」"
                    "再保存一次，或到「任务计划程序」里手动删掉"
                    f"「{AUTOSTART_TASK}」。")
                res["task_leftover"] = True
        return res

    # `/create /f` 会覆盖同名任务。仅当任务不存在，或动作已核实指向本安装目录时才继续。
    ownership = _task_ownership(AUTOSTART_TASK, root)
    if ownership in ("foreign", "unknown"):
        why = ("任务同名但动作不属于本程序" if ownership == "foreign"
               else "无法读取计划任务列表或任务动作")
        return {"ok": False, "mode": None, "elevated": True,
                "message": f"{why}，为避免覆盖其他任务，未注册「{AUTOSTART_TASK}」。"}

    # ---- 一级：登录触发 + HighestAvailable 的计划任务（不弹 UAC 的提权）
    xml_path = _write_task_xml(root)
    create_args = ["/create", "/tn", AUTOSTART_TASK, "/xml", str(xml_path)]
    if ownership == "owned":
        create_args.append("/f")
    r = schtasks(create_args, timeout=30)
    if r is not None and r.returncode == 0:
        runkey_left = _drop_runkey(root)
        left = _drop_legacy(root)
        left = list(left) + list(runkey_left)
        return {"ok": True, "mode": "task", "elevated": True,
                "message": (f"已注册开机启动「{AUTOSTART_TASK}」—— 登录后**以管理员身份**自动运行，不弹 UAC"
                            + ("；未清理旧启动项：" + "、".join(left) if left else "")),
                "warning": ELEVATED_BREAKS_CAPTURE}
    xml_err = decode(r.stdout or r.stderr).strip()[:300] if r is not None else "调用 schtasks 失败"

    # ---- 二级：命令行形式（同样要管理员，但绕开 XML 校验差异）
    bat = _write_launch_bat(root)
    fallback_args = ["/create", "/tn", AUTOSTART_TASK, "/sc", "onlogon",
                     "/rl", "HIGHEST", "/tr", str(bat)]
    if ownership == "owned":
        fallback_args.append("/f")
    r2 = schtasks(fallback_args, timeout=30)
    if r2 is not None and r2.returncode == 0:
        runkey_left = _drop_runkey(root)
        left = _drop_legacy(root)
        left = list(left) + list(runkey_left)
        return {"ok": True, "mode": "task", "elevated": True, "script": str(bat),
                "message": (f"已注册开机启动「{AUTOSTART_TASK}」—— 登录后**以管理员身份**自动运行，不弹 UAC"
                            + ("；未清理旧启动项：" + "、".join(left) if left else "")),
                "warning": ELEVATED_BREAKS_CAPTURE}

    # 若两种任务注册方式都失败，只有在任务仍不存在时才退到 Run 项。
    # 任务可能在预检与写入之间被其他进程创建；再次核对可避免并行启动两份。
    current_ownership = _task_ownership(AUTOSTART_TASK, root)
    if current_ownership != "absent":
        why = ("同名任务归属无法确认" if current_ownership == "unknown"
               else "同名任务已存在")
        return {"ok": False, "mode": None, "elevated": True,
                "message": (f"计划任务注册失败：{xml_err}；{why}，未添加重复的注册表启动项。")}

    # ---- 三级：注册表 Run 项（不需要权限，但**不是管理员**）
    res = _install_runkey(root)
    if not res.get("ok"):
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"注册开机启动失败。计划任务：{xml_err}；注册表：{res.get('message')}"}

    why = ("需要管理员权限才能注册「以管理员身份启动」"
           if not is_elevated() else "计划任务注册被系统拒绝")
    res.update({
        "mode": "runkey", "elevated": False,
        "message": (f"已注册开机启动，**普通权限**（想以管理员身份启动但没成：{why}）。"
                    "普通权限就够用，不用管这条；真想让服务以管理员跑，"
                    "右键 install.bat →「以管理员身份运行」，"
                    "或在管理员命令行里跑 `python bootstrap.py autostart --elevated`"
                    f"（但那样「自动抓会话」会不可用）。"),
        "task_error": xml_err,
    })
    return res


def _install_runkey(root) -> dict:
    import winreg
    ownership = _runkey_ownership(APP_NAME, root)
    if ownership in ("foreign", "unknown"):
        why = ("启动项同名但命令不属于本程序" if ownership == "foreign"
               else "无法读取启动项归属")
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"{why}，为避免覆盖其他启动项，未写入注册表。"}
    cmd = command(root)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"写注册表失败：{e}"}
    # ⚠ **装成功了才清旧的**（不是先删后建）—— 反过来中间失败就变成
    #   "旧的没了、新的没装上"，开机再也不会自启，而当天没人会发现。
    left = _drop_legacy(root)
    msg = "已注册开机启动（普通权限，注册表启动项）"
    out = {"ok": True, "mode": "runkey", "elevated": False, "message": msg}
    if left:
        # ⚠ 删不掉必须**报出来** —— 否则就是"界面写着普通权限、旧键还在、
        #   开机启两次"，而用户完全不知道（这个项目为"旧任务删不掉"栽过好几轮）。
        out["message"] = msg + "；⚠ 旧名字的启动项没删掉：" + "、".join(left)
        out["leftover"] = left
    return out


def _drop_runkey(root=None) -> list:
    """删**当前名字**那个注册表启动项（装计划任务那条路时要它让位）。"""
    return _drop_runkey_named([APP_NAME], root)


def _drop_runkey_named(names, root=None) -> list:
    """删指定名字的启动项，返回**没删掉的**。

    ⚠ 改名迁移要用（见 `_drop_legacy`）；本来那个"吞掉一切异常"的写法
      在这条路上不行 —— 删不掉必须**报出来**，不然就是"界面上写着普通权限、
      实际旧键还在、开机启两次"（这个项目为"旧任务删不掉"栽过好几轮）。
    """
    import winreg
    if root is None:
        from ..paths import ROOT
        root = ROOT
    failed = []
    for name in names:
        ownership = _runkey_ownership(name, root)
        if ownership == "absent":
            continue
        if ownership != "owned":
            reason = "归属无法确认" if ownership == "unknown" else "命令不属于本程序"
            failed.append(f"{name}（{reason}）")
            continue
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass                      # 本来就没有，正常
        except OSError:
            failed.append(name)
    return failed


def _drop_legacy(root) -> list:
    """把**旧名字**留下的自启痕迹清掉（注册表键 + 计划任务）。返回没清掉的。

    ⚠ 不复用 `_drop_runkey_named` 的话会漏掉计划任务那条路：
      旧名字可能既有 Run 键、又有个计划任务（老版本两条路都装过）。
    ⚠ 只在**新名字装成功之后**才调 —— 先删后建的话，中间失败就变成
      "旧的没了、新的没装上"，**开机再也不会自启，而当天没人会发现**。
    """
    failed = list(_drop_runkey_named(list(LEGACY_APP_NAMES), root))
    for legacy in LEGACY_APP_NAMES:
        name = f"{legacy}-开机自启"
        ok, message = remove_task_if_owned(name, root)
        if not ok:
            failed.append(f"{name}（{message}）")
    return failed


def _drop_task(root=None) -> tuple:
    """删掉开机自启的**计划任务**。

    返回 `(删干净了没, 说明)`，三种情况：
      * 任务本来就不存在 → `(True, "")` —— 不算失败，`_win_remove` 也据此判断"要不要报一声"
      * 删掉了           → `(True, "已删除开机启动任务")`
      * 没删掉           → `(False, "<schtasks 给的原因>")`

    ⚠ **删失败必须报出来**，不能像以前那样静默丢掉返回值。
    从"以管理员身份启动"切回"普通权限"时，如果这条任务没删掉，
    下次登录它照样以管理员把服务拉起来 —— 界面上写着"普通权限"、抓会话的报错
    却说是"管理员"，比不切还难查。而删它**需要管理员权限**，偏偏"切回普通权限"
    这个动作常常是在非管理员进程里做的，正好删不掉。

    ⚠ 名单来自系统任务枚举；详情必须能读并且动作指向本安装目录，才允许删除。
    `_win_remove` 会先核验 Run 项和任务两边，避免一边是外部项目时先删掉另一边。
    """
    if root is None:
        from ..paths import ROOT
        root = ROOT
    return remove_task_if_owned(AUTOSTART_TASK, root)


def _win_remove(root=None) -> dict:
    """两处都清 —— 提权任务和退路的注册表项，谁在就删谁。

    卸载也走这里，所以**两种模式都要能清干净**（用户可能中途换过方式）。
    """
    if root is None:
        from ..paths import ROOT
        root = ROOT
    task_state = _task_ownership(AUTOSTART_TASK, root)
    runkey_state = _runkey_ownership(APP_NAME, root)
    if task_state in ("foreign", "unknown"):
        reason = "归属无法确认" if task_state == "unknown" else "动作不属于本程序"
        return {"ok": False,
                "message": f"开机启动任务同名但{reason}，未做更改——请到「任务计划程序」核对"}
    if runkey_state in ("foreign", "unknown"):
        reason = "归属无法确认" if runkey_state == "unknown" else "命令不属于本程序"
        return {"ok": False,
                "message": f"注册表启动项同名但{reason}，未做更改——请手动核对后再试"}

    notes = []
    gone, msg = _drop_task(root)
    if not gone:
        return {"ok": False,
                "message": f"开机启动任务未更改（{msg}）—— 请到「任务计划程序」核对归属"}
    if msg:
        notes.append(msg if gone
                     else f"⚠️ 开机启动任务没删掉（{msg}）—— 到「任务计划程序」里手动删")
    left = _drop_runkey_named([APP_NAME], root)
    if left:
        return {"ok": False, "message": "删注册表启动项失败：" + "、".join(left)}
    if runkey_state == "owned":
        notes.append("已清理注册表项")
    return {"ok": True, "message": "；".join(notes) or "本来就没注册"}


# -------------------------------------------------------------------- macOS
def _mac_plist(root) -> str:
    log = Path(root) / "out" / "autostart.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.{APP_ID}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_python_exe()}</string>
    <string>{boot_script(root)}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def _mac_status() -> dict:
    return {"installed": MAC_PLIST.exists(), "path": str(MAC_PLIST)}


def _mac_install(root) -> dict:
    MAC_PLIST.parent.mkdir(parents=True, exist_ok=True)
    MAC_PLIST.write_text(_mac_plist(root), encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(MAC_PLIST)],
                   capture_output=True, text=True, timeout=15)
    r = subprocess.run(["launchctl", "load", str(MAC_PLIST)],
                       capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return {"ok": True, "message": "plist 已写入，但 launchctl load 失败"
                                       f"（下次登录仍会生效）：{(r.stderr or '').strip()[:120]}"}
    return {"ok": True, "message": "已加入开机启动"}


def _mac_remove() -> dict:
    if MAC_PLIST.exists():
        subprocess.run(["launchctl", "unload", str(MAC_PLIST)],
                       capture_output=True, text=True, timeout=15)
        MAC_PLIST.unlink()
    return {"ok": True, "message": "已取消开机启动"}


# -------------------------------------------------------------------- Linux
def _linux_status() -> dict:
    return {"installed": LINUX_DESKTOP.exists(), "path": str(LINUX_DESKTOP)}


def _linux_install(root) -> dict:
    LINUX_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
    LINUX_DESKTOP.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        f"Exec={command(root)}\n"
        f"Path={root}\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8")
    return {"ok": True, "message": "已加入开机启动"}


def _linux_remove() -> dict:
    try:
        LINUX_DESKTOP.unlink()
    except FileNotFoundError:
        pass
    return {"ok": True, "message": "已取消开机启动"}


# ---------------------------------------------------------------------- API
def status(root) -> dict:
    k = kind()
    if k == "windows":
        info = _win_status(root)
    else:
        info = {"macos": _mac_status, "linux": _linux_status}[k]()
    info.update({
        "supported": True,
        "kind": k,
        "platform": platform.system(),
        "app_name": APP_NAME,
        "command": command(root),
        "boot_script": str(boot_script(root)),
        "boot_script_exists": boot_script(root).exists(),
        "python": _python_exe(),
        # 当前**跑着的这个进程**是不是管理员 —— 跟"注册的方式"是两件事：
        # 可能注册成了管理员任务，但你现在是从普通双击的 start.bat 在看页面
        "self_elevated": is_elevated(),
        "self_admin": is_elevated(),
        # ⚠ 这台电脑是不是**根本没法不管理员**（内置 Administrator / UAC 关着）。
        #   有值的话，界面上就别再让人"改成普通权限"了 —— 改了也没用。
        "always_admin_reason": _always_admin_reason(),
    })
    info.setdefault("elevated", False)
    info.setdefault("mode", None)

    if k == "windows":
        if info.get("registered") and info["registered"] != info["command"]:
            # 项目挪过地方，注册的还是老路径。
            # 计划任务里命令和参数是分开存的，比较时会差一层引号 —— 归一化再比
            if _norm(info["registered"]) != _norm(info["command"]):
                info["stale"] = True
    return info


def _always_admin_reason() -> str:
    """问 `elevate` 要一句"为什么这台机器上所有程序都是管理员"。查不到就空串。"""
    try:
        from .elevate import always_admin_reason
        return always_admin_reason()
    except Exception:                        # noqa: BLE001
        return ""


def _norm(s: str) -> str:
    """比较注册的命令时去掉引号和多余空格 —— XML 里是 `Command` + `Arguments` 两段拼的，
    跟 `command()` 拼出来的字符串在引号上可能不完全一致，但那不是"路径变了"。"""
    return " ".join((s or "").replace('"', " ").split()).lower()


def install(root, elevated: bool | None = None) -> dict:
    """注册开机自启。Windows 上**默认是普通权限**（注册表 Run 项），不弹 UAC。

    `elevated=True` 是显式选择"以管理员身份启动"（计划任务，注册时要一次 UAC）——
    那条路会让「自动抓会话」失败，返回结果里会带上 `ELEVATED_BREAKS_CAPTURE` 警告。
    """
    if not boot_script(root).exists():
        return {"ok": False, "message": f"缺少 {boot_script(root).name}，没法注册"}
    k = kind()
    try:
        res = ({"windows": lambda: _win_install(root, elevated),
                "macos": lambda: _mac_install(root),
                "linux": lambda: _linux_install(root)}[k])()
    except OSError as e:
        # ⚠ 不能让 OSError 飞出去：命令行下就是一段 traceback，
        #   界面上就是「HTTP 500」。跟别处一样按"业务失败"报。
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"注册开机启动失败：{e}（写不进去 —— 权限不够，或者目录被安全软件拦了）"}
    res["status"] = status(root)
    return res


def remove(root=None) -> dict:
    k = kind()
    try:
        if k == "windows":
            return _win_remove(root)
        return {"macos": _mac_remove, "linux": _linux_remove}[k]()
    except OSError as e:
        return {"ok": False, "message": f"取消失败：{e}"}
