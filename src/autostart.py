"""开机自动启动的注册与取消。

| 平台 | 机制 | 提权 |
|---|---|---|
| Windows | **登录触发的计划任务**，`RunLevel=HighestAvailable` | ✅ 以管理员身份，**不弹 UAC** |
| Windows（退路） | 注册表 `HKCU\\...\\Run` | ❌ 普通权限 |
| macOS | `~/Library/LaunchAgents/com.cbg-reconcile.plist` | — |
| Linux | `~/.config/autostart/cbg-reconcile.desktop` | — |

**为什么 Windows 不用注册表 Run 项**：Run 项**没法提权**。想让服务以管理员身份
跑，只能在兼容性标记里加 `RUNASADMIN`（`AppCompatFlags\\Layers`）—— 但那**每次开机
都会弹一次 UAC**，门店电脑上没人会去点。

登录触发的计划任务 + `HighestAvailable` 是 Windows 上唯一"静默提权"的正规做法：
开机登录后由任务计划程序直接以高完整性级别拉起，**全程无 UAC 弹窗**。

代价：**注册这个任务本身需要管理员权限**（这是 Windows 的安全边界，绕不过去）。
所以 `install.bat` 会先要一次 UAC；如果拿不到（用户点了"否"，或者账号不是管理员），
我们**退回注册表 Run 项**，功能可用但服务不是管理员，界面上会明说。

启动的是项目根目录下的 `boot.py`，它自己 `chdir` 到项目根再起服务 ——
**不依赖任务/注册表里配工作目录**。
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .winutil import decode, schtasks, xml_text

APP_ID = "cbg-reconcile"
APP_NAME = "CBG报量对账"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# 开机自启用的计划任务名。带后缀，跟「定时执行」那个（CBG报量对账）区分开 ——
# 用户在任务表里一眼能看出哪个是"开机常驻"、哪个是"每天跑一次"。
AUTOSTART_TASK = f"{APP_NAME}-开机自启"

# 任务计划 XML 的命名空间。标签名固定英文，不受系统语言影响
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

MAC_PLIST = Path.home() / "Library" / "LaunchAgents" / f"com.{APP_ID}.plist"
LINUX_DESKTOP = Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"


class AutostartError(RuntimeError):
    pass


def boot_script(root) -> Path:
    return Path(root) / "boot.py"


def _python_exe() -> str:
    """Windows 上用 pythonw.exe —— 它**不带控制台窗口**，开机时不会闪黑框。"""
    if platform.system() == "Windows":
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return sys.executable or "python3"


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


def _win_status() -> dict:
    info: dict = {}
    task = _win_task_info()
    if task:
        info.update({
            "installed": True,
            "mode": "task",
            "elevated": task["run_level"].lower() == "highestavailable",
            "registered": task["command"],
            "task_name": AUTOSTART_TASK,
            "run_level": task["run_level"],
        })
        return info

    val = _win_runkey_value()
    if val:
        # 退路：注册表 Run 项 —— 能开机自启，但**不是管理员**
        info.update({
            "installed": True,
            "mode": "runkey",
            "elevated": False,
            "registered": val,
            "task_name": AUTOSTART_TASK,
        })
        return info
    return {"installed": False, "mode": None, "elevated": False,
            "task_name": AUTOSTART_TASK}


def _write_task_xml(root) -> Path:
    """schtasks 要求 XML 是 **Unicode**（UTF-16）—— 我们这里带中文描述，
    写成 UTF-8 有可能被拒。用 utf-16 写（Python 会带 BOM）。"""
    path = Path(tempfile.gettempdir()) / f"{APP_ID}-autostart-task.xml"
    path.write_text(task_xml(root), encoding="utf-16")
    return path


def _write_launch_bat(root) -> Path:
    """退路二用的启动脚本 —— `/tr` 只能给一个路径，所以把带引号的命令行塞进 bat。

    （XML 那条路能用上就不会走这里；留着是因为不同 Windows 版本对任务 XML
    的校验宽严不一，多一条路少一次白跑。）
    """
    path = Path(root) / "boot-run.bat"
    body = (
        "@echo off\r\n"
        'cd /d "%~dp0"\r\n'
        f'start "" "{_pythonw()}" "{boot_script(root)}"\r\n'
    )
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return path


def _win_install(root, elevated: bool | None = None) -> dict:
    """注册开机自启。

    `elevated`：
      * `True`  —— 登录触发的计划任务 + `HighestAvailable`（**不弹 UAC 的提权**）
      * `False` —— 直接写注册表 Run 项（普通权限）
      * `None`  —— 按当前进程是不是管理员来猜（老调用方）

    提权那条路是**默认**，但**必须留一条普通权限的路**：
    Chrome / Edge（138+，`AutoDeElevate`）从管理员进程启动时会把自己降权重启，
    个别机器上这一步会失败，那时唯一的解法就是让服务别以管理员跑。
    界面上有「启动方式」下拉，就是给这个用的。

    两条路**互斥** —— 装一条就把另一条清掉，否则会互相打架
    （虽然 pidfile 挡住第二个实例，但任务列表里会多一条没人认识的）。
    """
    if elevated is None:
        elevated = is_elevated()

    if not elevated:
        res = _install_runkey(root)
        if res.get("ok"):
            _drop_task()                      # 换回普通权限：把提权任务清掉
        return res

    # ---- 一级：登录触发 + HighestAvailable 的计划任务（不弹 UAC 的提权）
    xml_path = _write_task_xml(root)
    r = schtasks(["/create", "/tn", AUTOSTART_TASK, "/xml", str(xml_path), "/f"], timeout=30)
    if r is not None and r.returncode == 0:
        _drop_runkey()
        return {"ok": True, "mode": "task", "elevated": True,
                "message": f"已注册开机启动「{AUTOSTART_TASK}」—— 登录后**以管理员身份**自动运行，不弹 UAC"}
    xml_err = decode(r.stdout or r.stderr).strip()[:300] if r is not None else "调用 schtasks 失败"

    # ---- 二级：命令行形式（同样要管理员，但绕开 XML 校验差异）
    bat = _write_launch_bat(root)
    r2 = schtasks(["/create", "/tn", AUTOSTART_TASK, "/sc", "onlogon",
                   "/rl", "HIGHEST", "/tr", str(bat), "/f"], timeout=30)
    if r2 is not None and r2.returncode == 0:
        _drop_runkey()
        return {"ok": True, "mode": "task", "elevated": True, "script": str(bat),
                "message": f"已注册开机启动「{AUTOSTART_TASK}」—— 登录后**以管理员身份**自动运行，不弹 UAC"}

    # ---- 三级：注册表 Run 项（不需要权限，但**不是管理员**）
    res = _install_runkey(root)
    if not res.get("ok"):
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"注册开机启动失败。计划任务：{xml_err}；注册表：{res.get('message')}"}

    why = ("需要管理员权限才能注册「以管理员身份启动」"
           if not is_elevated() else "计划任务注册被系统拒绝")
    res.update({
        "mode": "runkey", "elevated": False,
        "message": (f"已注册开机启动，但**是普通权限**（{why}）。"
                    "右键 install.bat →「以管理员身份运行」，或在管理员命令行里跑 "
                    "`python bootstrap.py autostart` 可改为管理员身份启动。"),
        "task_error": xml_err,
    })
    return res


def _install_runkey(root) -> dict:
    import winreg
    cmd = command(root)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        return {"ok": False, "mode": None, "elevated": False,
                "message": f"写注册表失败：{e}"}
    return {"ok": True, "mode": "runkey", "elevated": False,
            "message": "已注册开机启动（普通权限，注册表启动项）"}


def _drop_runkey() -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
    except (FileNotFoundError, OSError):
        pass


def _drop_task() -> None:
    schtasks(["/delete", "/tn", AUTOSTART_TASK, "/f"])


def _win_remove() -> dict:
    """两处都清 —— 提权任务和退路的注册表项，谁在就删谁。

    卸载也走这里，所以**两种模式都要能清干净**（用户可能中途换过方式）。
    """
    notes = []
    r = schtasks(["/delete", "/tn", AUTOSTART_TASK, "/f"])
    if r is not None and r.returncode == 0:
        notes.append("已删除开机启动任务")
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
        notes.append("已清理注册表项")
    except FileNotFoundError:
        pass
    except OSError as e:
        return {"ok": False, "message": f"删注册表失败：{e}"}
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
    info = {"windows": _win_status, "macos": _mac_status, "linux": _linux_status}[k]()
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


def _norm(s: str) -> str:
    """比较注册的命令时去掉引号和多余空格 —— XML 里是 `Command` + `Arguments` 两段拼的，
    跟 `command()` 拼出来的字符串在引号上可能不完全一致，但那不是"路径变了"。"""
    return " ".join((s or "").replace('"', " ").split()).lower()


def install(root, elevated: bool | None = None) -> dict:
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


def remove() -> dict:
    k = kind()
    try:
        return {"windows": _win_remove, "macos": _mac_remove, "linux": _linux_remove}[k]()
    except OSError as e:
        return {"ok": False, "message": f"取消失败：{e}"}
