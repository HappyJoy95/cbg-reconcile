"""定时执行：Windows 计划任务（schtasks）/ unix crontab。

门店电脑是 Windows，开发机是 macOS —— 两套都实现，前端只调
`status()` / `install()` / `remove()` 三个函数。

设计取舍：**先写一个 run 脚本，再让计划任务去调它**。
这样日志重定向、工作目录、编码这些脏活都在脚本里解决一次，
计划任务本身只负责"每天几点叫我"。
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import time
from pathlib import Path

from .autostart import AUTOSTART_TASK
from . import runtime
from .winutil import decode as _decode, parse_xml, schtasks as _schtasks, xml_text

#: 定时任务的默认名字。带执行时间后缀，比如 `门店数据拉取与计算-21点00`。
#:
#: ⚠ **改过名**（2026-09-16）：以前叫 `CBG报量对账`。那时候它确实只做对账，
#: 现在它每天干三件事（抓数据 → 报量排查 → POS 合规），
#: 名字还叫"报量对账"就名不副实了 —— 门店在 Windows 任务计划程序里看到
#: 一个叫"报量对账"的任务，不会想到它还管 POS。
#:
#: ⚠ 改名意味着**老门店那条任务还在**（同名才是覆盖，不同名就并存）——
#: 于是会一天跑两遍。`LEGACY_TASK_NAMES` 就是用来认出它们、在界面上提醒删掉的。
TASK_NAME = "门店数据拉取与计算"

#: 历史上用过的名字。**只增不减** —— 认不出来就等于"这不是我们建的任务"，
#: 界面上既不会提示、也删不掉（`_is_ours` 拿它做判断）。
LEGACY_TASK_NAMES = ("CBG报量对账",)
LOG_NAME = "run.log"          # 计划任务跑完留下的日志（out\ 下）
# ⚠ 生成的脚本要能被**认出来是哪一版**。
#   run.bat 是**注册任务时**生成的，升级代码**不会**动它 ——
#   所以门店电脑上很容易留着一个旧脚本（用户就踩到了：日志格式还是旧的，
#   而且黑窗、退出码这些修好的东西一样没生效）。
#   启动时比对这一行，不一致就重建。
# ⚠ 每次**改了这个文件里生成的 bat 内容**就要 +1 —— 门店靠它判断
#   "我这份 run.bat 是不是旧的"（`runner_outdated`），过时才重建。
#   v5：日常流程从 `check` 换成 `daily`（先抓华为当月写进库，再做两个分析）。
#   不 +1 的话，**已装门店的 run.bat 永远不会被重写** ——
#   而 2.0.0 的 check 只从本地库读，没人抓数据 = 每天报「库不新鲜」。
RUNNER_MARK = "rem cbg-runner v5"
CRON_MARK = "# cbg-reconcile"          # crontab 里的归属标记，用于增删改
DEFAULT_TIME = "21:00"        # 门店一般晚上关门前跑
DEFAULT_DAYS_AGO = 1                   # 跑昨天（那天的销售早就结束，零遗漏）


def kind() -> str:
    return "windows" if platform.system() == "Windows" else "unix"


def script_path(root: Path) -> Path:
    return Path(root) / ("run.bat" if kind() == "windows" else "run.sh")


def manual_script_path(root: Path) -> Path:
    """给人**手动双击**用的那个 —— 跑完停住，方便看结果。

    `run.bat` 是给计划任务调的：跑完窗口自己关，来不及看。
    """
    return Path(root) / ("run-now.bat" if kind() == "windows" else "run-now.sh")


def _python() -> str:
    """计划任务里用什么解释器。用绝对路径，免得计划任务的环境变量跟交互式不一样。

    优先用**安装时记下的那一个**（`src/runtime.py`）—— 一台电脑上有两个 Python 时，
    计划任务必须拉起"装过依赖的那一个"。用错了的表现是：每天到点了，
    任务计划程序里显示"上次运行成功"，而 `out/` 里压根没有新报告。
    """
    return runtime.current() or ("python" if kind() == "windows" else "python3")


def _pythonw() -> str:
    """Windows 上用 pythonw.exe —— 它**不带控制台窗口**。

    计划任务跑 run.bat 时会开一个 cmd 黑窗；如果里面同步跑 python.exe，
    那个黑窗会**挂满整个对账过程**（一两分钟），用户反馈"影响效果"。
    换成 `start "" pythonw.exe …` 之后：cmd 立刻退出（黑窗只闪一下），
    pythonw 自己没有控制台 —— 界面上就干净了。
    """
    if kind() == "windows":
        exe = Path(_python())
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return _python()


#: 自动化勾选 → 步骤。**和界面上的复选框一一对应**。
#: ⚠ **不含 `dump`** —— 用户 2026-09-17 把那个复选框拿掉了
#: （「默认执行这个。不可选」），它由 `run_daily.ALWAYS_STEPS` 无条件补上。
#: 真值在 `run_daily`，这里只是给不 import run_daily 的调用方一个方便入口，
#: 有测试盯着两边对得上。
AUTOMATION_CHOICES = ("pos", "pools")


def steps_from_choices(picked):
    """勾了哪几项 → 步骤元组（**含必做的**）。校验交给 `run_daily`，只此一处。

    ⚠ **两个都不勾是合法的**（用户 2026-09-17 定）：含义是「每天只抓数据，
    不算也不推」。原来那条"至少勾一项"防的是"取消了勾选、结果照样推"，
    而现在的行为严格照着勾选走（空 + 必做），**不会静默扩大**。
    """
    from . import run_daily
    return run_daily.with_always(picked)


def choices_from_steps(steps) -> list:
    """步骤 → 勾选框该点亮哪几个。

    ⚠ **必做的项不参与**（`ALWAYS_STEPS`）—— 界面上没有它的框，
    混进去的话前端会去点一个不存在的复选框。
    """
    from . import run_daily
    try:
        got = run_daily.with_always(steps)
    except ValueError:
        return list(AUTOMATION_CHOICES)
    return [s for s in got if s not in run_daily.ALWAYS_STEPS]


def existing_steps(root) -> tuple:
    """从现有的 run 脚本里**反推**它跑哪几件事。

    重建时保住用户的勾选，跟 `existing_days_ago` 一个道理。
    读不出来就当默认（三件都做）—— 老脚本没有跳过开关，确实是这样。

    ⚠ 出口统一走 `with_always`：老 run.bat 里带着 `--skip-dump` 的
    （那时候"抓数据"还能取消）要**补回来**，否则升级后库永远不更新。
    """
    from . import run_daily
    for p in (script_path(root), manual_script_path(root)):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if " daily" not in text:
            continue
        skipped = {s for s, flag in run_daily.STEP_FLAGS.items() if flag in text}
        kept = tuple(s for s in run_daily.STEPS if s not in skipped)
        if kept:
            return run_daily.with_always(kept)
    return run_daily.with_always(run_daily.AUTOMATION_DEFAULT_STEPS)


def automation_steps(root) -> tuple:
    """定时任务当前跑哪几件事。**注册记录优先**，没有就从脚本反推。

    记录优先的原因和任务详情一样：提权注册的任务普通权限读不到，
    但我们自己记了一份（`.secrets/schedule.json`）。

    ⚠ 三条出口**都要过 `with_always`** —— 老记录里可能没有 `dump`
    （那时它还是可选项），不补的话那份记录会一直生效，库永远不更新。
    """
    from . import run_daily
    for rec in (_recall(root) or {}).values():
        if not isinstance(rec, dict):
            continue
        steps = rec.get("steps")
        if steps:
            try:
                return run_daily.with_always(steps)
            except ValueError:
                pass
        what = rec.get("what")            # 老格式：单个字符串
        if what:
            try:
                return run_daily.with_always(run_daily.BUTTON_STEPS[what])
            except (KeyError, ValueError):
                pass
    return existing_steps(root)


def set_automation_steps(root, config: str, picked, run_daily_fn=None) -> dict:
    """改「自动化跑什么」：写记录 + 重写 run 脚本。**不碰 Windows 任务。**

    ⚠ 为什么不用重新注册任务：计划任务跑的是 `run.bat`，改的是 run.bat 的
    内容 —— 任务本身一个字都没变。重新注册有可能弹 UAC（旧任务覆盖不了 /
    账户被 UAC 过滤），为了改个勾选弹框不值得，而且弹了还未必成。
    """
    from . import run_daily                        # 延迟 import：避免和 cli 绕圈
    steps = run_daily.with_always(picked)
    root = Path(root)
    days = existing_days_ago(root)
    if days is None:
        days = DEFAULT_DAYS_AGO
    write_runner_script(root, config, days, steps=steps)
    # 记录：把每个已注册任务都更新一遍（任务名是按时间起的，可能不止一个）
    rec = _recall(root)
    if rec:
        for task in list(rec):
            if isinstance(rec.get(task), dict):
                _remember(root, task, time_str=rec[task].get("time", ""),
                          days_ago=rec[task].get("days_ago"), config=config,
                          steps=steps)
    else:
        # 还没注册过任务（或者记录丢了）：也记一条，界面至少能显示勾选
        _remember(root, TASK_NAME, config=config, steps=steps)
    label = run_daily.steps_label(steps)
    return {"ok": True, "steps": list(steps), "label": label,
            "message": "已改成「%s」，下次定时执行生效（不用重新注册任务）" % label}


def write_runner_script(root: Path, config: str, days_ago: int = DEFAULT_DAYS_AGO,
                        steps=None) -> Path:
    root = Path(root)
    path = script_path(root)
    # ⚠ 解释器和脚本路径**都要加引号** —— Windows 上 Python 常装在
    #   `C:\Program Files\Python311\`，不加引号 cmd 会把 `C:\Program` 当命令，
    #   计划任务就每天静默失败。类 Unix 上路径带空格同理。
    # ⚠ **不要在 bat 里写 `>> out\run.log`**：那样屏幕上什么都没有，
    #   双击的人看到的是黑窗口 + 一分多钟 + 自己关掉，完全判断不了跑没跑。
    #   日志交给 Python 分流（--log-file），屏幕和文件两边都有。
    # 勾了「只算 POS / 只做四池对账」就带上对应的跳过开关 —— 映射表在 run_daily，
    # **这里不另写一份**（各写一份必然有一天对不上）。
    #
    # ⚠ 过一道 `with_always`：**定时任务永远要抓数据**（用户 2026-09-17 定的，
    #   界面上那个复选框都拿掉了）。不过这道的话，一份老记录里的
    #   `--skip-dump` 会一直生成下去，库再也不更新，
    #   而日志里只会说"跑完了"。
    #   ⚠ 命令行 `daily --skip-dump` 仍然可以（调试用），**管的只是这里** ——
    #   走 `flags_for`/`flags_for_steps` 那条路不经过 `with_always`。
    from . import run_daily
    if steps is None:
        steps = run_daily.AUTOMATION_DEFAULT_STEPS
    steps = run_daily.with_always(steps)
    extra = "".join(" " + f for f in run_daily.flags_for_steps(steps))
    base = (f'"{_pythonw()}" -m src.cli -c "{config}" daily{extra}'
            f' --days-ago {int(days_ago)}')
    # ⚠ 路径要写全：只给 "run.log" 的话，工作目录是项目根 → 写到根目录去了，
    #   而下面 bat 追加退出码用的又是 out\run.log —— 两处对不上，排查时会被坑。
    log = ('--log-file "out\\%s"' % LOG_NAME) if kind() == "windows" \
        else ('--log-file "out/%s"' % LOG_NAME)
    if kind() == "windows":
        # ⚠ 不在这里拼 `python -m src.cli …`：那样任何**启动阶段**的失败
        #   （缺依赖、路径不对、假 python）在 pythonw 下连 traceback 都留不下来，
        #   日志里只剩一个莫名其妙的退出码。交给 run_check.py —— 它先开日志
        #   再 import，从第一行起什么都不会丢。
        pyw = _pythonw()
        logfile = f"out\\{LOG_NAME}"
        body = (
            "@echo off\r\n"
            f"{RUNNER_MARK}\r\n"
            "chcp 65001 >nul\r\n"
            'cd /d "%~dp0"\r\n'
            "if not exist out mkdir out\r\n"
            "rem Leave a trace, so 'bat never ran' and 'python never started' differ.\r\n"
            f'echo [%DATE% %TIME%] run.bat launching>> "{logfile}"\r\n'
            "rem start = cmd exits at once, so this console closes in a blink instead of\r\n"
            "rem hanging around for the whole run. pythonw.exe has no console of its own.\r\n"
            f'start "" "{pyw}" "run_check.py" -c "{config}" daily{extra} --days-ago {int(days_ago)}\r\n'
            "if errorlevel 1 (\r\n"
            "  rem 'start' itself failed -- pythonw.exe missing or path wrong\r\n"
            f'  echo [%DATE% %TIME%] FAILED to start pythonw: "{pyw}">> "{logfile}"\r\n'
            f'  echo [%DATE% %TIME%] FAILED to start pythonw: "{pyw}"\r\n'
            "  exit /b 9009\r\n"
            ")\r\n"
            "exit /b 0\r\n"
        )
    else:
        body = (
            "#!/bin/sh\n"
            'cd "$(dirname "$0")" || exit 1\n'
            "mkdir -p out\n"
            f"{base} {log}\n"
            "exit $?\n"
        )
    # ⚠ 不能用 path.write_text(newline=...) —— 那是 Python 3.10 才有的参数，
    #   门店电脑上装的很可能是 3.9，会直接 TypeError。
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    if kind() != "windows":
        path.chmod(0o755)

    # 再写一个**给人手动双击**的。
    # ⚠ 它**不能**简单地 `call run.bat` —— run.bat 现在用 `start` 起进程、不等结果，
    #   那样 run-now 会在对账还没跑完时就 pause，等于废了。
    #   手动跑就该**同步**跑：屏幕上能看到全过程，跑完停住看结果。
    try:
        mpath = manual_script_path(root)
        mbase = f'"{_python()}" -m src.cli -c "{config}" daily{extra} --days-ago {int(days_ago)}'
        mlog = ('--log-file "out\\%s"' % LOG_NAME) if kind() == "windows" \
            else ('--log-file "out/%s"' % LOG_NAME)
        if kind() == "windows":
            mbody = (
                "@echo off\r\n"
                f"{RUNNER_MARK}\r\n"
            "chcp 65001 >nul\r\n"
                "rem PURE ASCII on purpose -- see bootstrap.py.\r\n"
                "rem Manual run: synchronous, so you can watch it and read the result.\r\n"
                'cd /d "%~dp0"\r\n'
                "if not exist out mkdir out\r\n"
                f'"{_python()}" "run_check.py" -c "{config}" daily{extra} --days-ago {int(days_ago)}\r\n'
                "echo.\r\n"
                "echo   Press any key to close this window\r\n"
                "pause >nul\r\n"
            )
        else:
            mbody = (
                "#!/bin/sh\n"
                'cd "$(dirname "$0")" || exit 1\n'
                "mkdir -p out\n"
                f"{mbase} {mlog}\n"
                "printf '\\n按回车关闭…'\n"
                "read _\n"
            )
        with open(mpath, "w", encoding="utf-8", newline="") as f:
            f.write(mbody)
        if kind() != "windows":
            mpath.chmod(0o755)
    except OSError:
        pass                                 # 写不出来不影响计划任务本身
    return path


# --------------------------------------------------------------------- Windows
def existing_days_ago(root) -> int | None:
    """从现有的 run 脚本里把 `--days-ago N` 读出来。

    重建脚本时要**保住用户原来选的**（昨天/今天），不能默默改回默认值 ——
    那会让对账的目标日悄悄变掉，比不重建更糟。
    """
    for p in (script_path(root), manual_script_path(root)):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"--days-ago\s+(\d+)", text)
        if m:
            return int(m.group(1))
    return None


def runner_outdated(root) -> bool:
    """脚本是不是这一版生成的（老版本没有标记 / 标记不一样）。"""
    p = script_path(root)
    if not p.exists():
        return True
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return RUNNER_MARK not in text


def refresh_runner_scripts(root, config: str) -> bool:
    """脚本过时/缺失就按当前模板重建，**保住原来的 --days-ago**。

    为什么需要：脚本只在**注册任务时**生成，升级代码不会碰它。
    结果就是"代码修好了，门店电脑上跑的还是旧脚本"——
    （黑窗没修掉、日志格式还是旧的、界面认不出跑完没）。
    """
    root = Path(root)
    if not runner_outdated(root):
        return False
    days = existing_days_ago(root)
    if days is None:
        days = DEFAULT_DAYS_AGO
    try:
        # ⚠ 勾选也要保住 —— 否则一次界面自愈就把"只算 POS"悄悄改回"整个项目"
        write_runner_script(root, config, days, steps=existing_steps(root))
    except OSError:
        return False
    return True


def _same_task(recorded: str, task: str) -> bool:
    """cron 行里记的任务名，跟这次要装的是不是**同一条**。

    ⚠ 不能直接比字符串 —— 任务名 2026-09-16 从 `CBG报量对账` 改成了
    `门店数据拉取与计算`。老名字的 cron 行比不相等 ⇒ **不会被替换、也不会被删**，
    结果是同一件事在 crontab 里躺两份（跟 Windows 那边"不同名就并存"一个道理）。

    判据：把老前缀换成新前缀之后相等就算同一条。
    """
    if recorded == task:
        return True
    for legacy in LEGACY_TASK_NAMES:
        if recorded.startswith(legacy):
            if recorded.replace(legacy, TASK_NAME, 1) == task:
                return True
    return False


def default_task_name(time_str: str) -> str:
    r"""默认任务名**带上执行时间**，比如 21:00 → `门店数据拉取与计算-21点00`。

    ⚠ 为什么必须带时间：用户想一天跑两次（中午一次、打烊一次），如果两次都留空名字，
    名字就是同一个，第二次的 `/f` 会把第一次**直接覆盖掉** ——
    现象正是"注册了两个，列表里只有一个"。

    ⚠ 为什么是「21点00」不是「21:00」：Windows 计划任务的名字**就是文件名**
    （存在 `C:\Windows\System32\Tasks\` 下），非法字符是
    `\ / : * ? " < > |` —— **冒号不能用**，用了 schtasks 直接拒绝。
    见 https://learn.microsoft.com/en-us/answers/questions/2840245/
    """
    hh, mm = time_str.split(":")
    return f"{TASK_NAME}-{int(hh)}点{mm}"


def _leaf(name: str) -> str:
    return (name or "").rsplit("\\", 1)[-1].strip()


def _is_ours(name: str) -> bool:
    """是不是"定时执行"该管的那个任务。

    用**包含**而不是相等 —— 这样 `CBG报量对账-21点00`、`我的CBG报量对账`
    也能列出来（多开一个、或者手工建的），不然用户根本看不见、也删不掉。

    ⚠ 但**必须排除开机自启那个**（`CBG报量对账-开机自启`）：它不是每天定时的，
    归「后台服务」卡片管。混进这张表的话，用户在这个页面点「删除」会**顺手把
    开机自启删掉**，然后完全不知道服务为什么不再自启。
    """
    leaf = _leaf(name)
    if leaf == AUTOSTART_TASK:
        return False
    # ⚠ 老名字也要认 —— 不认的话那条旧任务在界面上是个"外人"：
    #   删不掉、也拿不到"你还有一条旧任务在跑"的提醒，于是一天跑两遍。
    return any(n in leaf for n in (TASK_NAME,) + LEGACY_TASK_NAMES)


def _win_list_names() -> list[str]:
    """列出所有任务名。

    用 `/fo CSV /nh`：**第一列就是任务名**，跟系统语言无关 ——
    比解析 `字段名: 值` 那套稳得多。
    """
    r = _schtasks(["/query", "/fo", "CSV", "/nh"])
    if r is None or r.returncode != 0:
        return []
    out = []
    for line in _decode(r.stdout).splitlines():
        m = re.match(r'^"([^"]+)"', line.strip())
        if m and m.group(1).strip():
            out.append(m.group(1))
    return out


def _win_task_info(name: str, record: dict = None) -> dict:
    """单个任务的详情。**优先用 /xml** —— 里面的标签名是固定的英文，不受系统语言影响。

    ## ⚠ `unreadable` 这个标记是有来历的

    任务**列得出来**（`/query /fo CSV` 只读名字，普通权限就能列），
    但**读不到详情** —— 因为它是**以管理员身份建**的：

    * 任务的所有者是 `Administrators`；
    * 而我们的服务现在是**普通权限**（过滤令牌，不在那个组里）
      → `schtasks /query /tn <名> /xml` 直接被拒。

    结果是界面上的时间/命令全空，显示成「时间没读出来」——
    用户看到的就是**"没有管理员权限就看不到定时执行的设置"**。

    所以这里要把"读不到"和"根本没有"**分开**：前者是权限问题（有救），
    后者才该提示去注册。标出来，界面才能给出对症的话。
    """
    info = {"name": _leaf(name), "full_name": name, "time": "", "command": "",
            "enabled": None, "detail_source": "", "unreadable": False}
    r = _schtasks(["/query", "/tn", name, "/xml"])
    if r is not None and r.returncode == 0:
        root = parse_xml(r.stdout)
        if root is not None:
            m = re.search(r"T(\d{2}:\d{2})", xml_text(root, "StartBoundary"))
            if m:
                info["time"] = m.group(1)
            parts = [xml_text(root, "Command"), xml_text(root, "Arguments")]
            info["command"] = " ".join(x for x in parts if x)
            en = xml_text(root, "Enabled")
            if en:
                info["enabled"] = en.lower() == "true"
            info["detail_source"] = "xml"
            return info

    # 退路：解析 `字段名: 值`（中文/英文两套都试）
    r = _schtasks(["/query", "/tn", name, "/fo", "LIST", "/v"])
    if r is None or r.returncode != 0:
        # 两条路都读不到 —— 名字明明在列表里（调用方就是这么拿到它的），
        # 那就是**权限不够**，不是"没有这个任务"。
        #
        # ⚠ 但我们**自己记过**注册参数（提权建的任务读不到详情，这是常态）——
        #   有记录就把它填上，界面照样能显示时间和命令，
        #   再标一句"这份是注册时记下的，不是刚从系统读的"。
        if record and record.get("time"):
            info["time"] = record["time"]
            info["detail_source"] = "record"
            info["unreadable"] = False
            return info
        info["unreadable"] = True
        return info
    text = _decode(r.stdout)

    def pick(*labels):
        for line in text.splitlines():
            for lab in labels:
                if line.strip().startswith(lab):
                    return line.split(":", 1)[-1].strip()
        return ""

    nxt = pick("下次运行时间", "Next Run Time")
    m = re.search(r"(\d{1,2}:\d{2})", nxt)
    if m:
        info["time"] = m.group(1).zfill(5)
    info["command"] = pick("要运行的任务", "Task To Run")
    info["detail_source"] = "list"
    return info


# ---------------------------------------------------------------- 注册记录
# 我们**自己**记一份"注册了什么"。
#
# ⚠ 为什么必须有这个：那台机器上（过滤令牌的管理员）**普通权限连建都建不了**
#   计划任务（`schtasks /create` 直接「拒绝访问」），所以只能**提权建**；
#   而提权建出来的任务所有者是 `Administrators` —— 之后普通权限
#   `schtasks /query /tn <名> /xml` **又被拒**，界面上时间和命令全空。
#
#   **那就别去问 Windows 了**：注册的时候是我们自己传的参数，记下来就行。
#   这条不依赖任何 ACL 行为、任何系统语言、任何 schtasks 版本 ——
#   比解析它的输出可靠得多。
#
# 放 `.secrets/`（selfupdate 的 NEVER_TOUCH）：它是**这台电脑的**运行状态，
# 不该被"照仓库原样铺"的升级冲掉，也不该跟着包走。
RECORD_FILE = ".secrets/schedule.json"


def record_path(root) -> Path:
    return Path(root) / RECORD_FILE


def _recall(root) -> dict:
    """读注册记录。读不到/坏了都给空 dict —— 这是显示用的，**绝不抛**。"""
    try:
        d = json.loads(record_path(root).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _remember(root, task: str, *, time_str: str = "", days_ago=None,
              config: str = "", steps=None, what: str = "") -> None:
    """记下"这个任务是我们用这些参数注册的"。**写不成不影响注册本身。**"""
    try:
        d = _recall(root)
        old = d.get(task) if isinstance(d.get(task), dict) else {}
        entry = {"time": time_str, "days_ago": days_ago, "config": config,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        # ⚠ steps 没给就**保留上一次的** —— 改时间/目标日时别把勾选弄丢。
        #   （老记录里可能是 `what: "all"` 这种单值格式，原样留着，
        #    读的时候 `automation_steps` 会换算。）
        if steps:
            entry["steps"] = list(steps)
        elif old.get("steps"):
            entry["steps"] = list(old["steps"])
        elif old.get("what"):
            entry["what"] = old["what"]
        d[task] = entry
        p = record_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _forget(root, task: str) -> None:
    """抹掉一条注册记录。

    ⚠ 要**两个键都试**：界面上删除按钮发的是 `full_name`
    （`\\CBG报量对账-21点20`，带反斜杠），而记录里的键是**叶子名**。
    只 `pop(task)` 的话永远删不掉 —— 表现就是"删了之后时间和命令还挂在那儿"。
    """
    try:
        d = _recall(root)
        hit = False
        for key in {task, _leaf(task)}:
            if key and d.pop(key, None) is not None:
                hit = True
        if hit:
            record_path(root).write_text(
                json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


# `schtasks` 每次要起一个进程（列任务 ~几百毫秒，XML 再一个进程）。
# 概览页每 30 秒刷一次，不缓存的话光是看页面就在不停 fork。
# 10 秒足够短 —— 用户点完注册/删除我们会**主动作废缓存**，看到的还是实时的。
_LIST_CACHE: dict = {"at": 0.0, "tasks": []}
_LIST_TTL = 10.0


def invalidate_cache() -> None:
    _LIST_CACHE["at"] = 0.0


def _win_list(force: bool = False, root=None) -> list[dict]:
    now = time.time()
    if not force and now - _LIST_CACHE["at"] < _LIST_TTL:
        return _LIST_CACHE["tasks"]
    rec = _recall(root)
    tasks = [_win_task_info(n, rec.get(_leaf(n)) or rec.get(n))
             for n in _win_list_names() if _is_ours(n)]
    _LIST_CACHE["at"] = now
    _LIST_CACHE["tasks"] = tasks
    return tasks


def _win_status(root=None) -> dict:
    """`root` 用来读我们自己的注册记录（提权建的任务读不到详情时靠它兜底）。"""
    tasks = _win_list(root=root)
    if not tasks:
        return {"installed": False, "tasks": []}
    return {"installed": True, "tasks": tasks,
            "time": tasks[0].get("time", ""), "next_run": "",
            "name": tasks[0]["name"], "full_name": tasks[0]["full_name"]}


def _win_install(root: Path, time_str: str, days_ago: int, config: str,
                 name: str | None = None) -> dict:
    bat = write_runner_script(root, config, days_ago)
    task = name or TASK_NAME
    # ⚠ `/tr` 的值**不要自己加引号** —— subprocess 在 Windows 上会走 list2cmdline，
    #   手工加的引号会被它转义成 `\"D:\...\run.bat\"`，而 schtasks 是原生 Win32 程序、
    #   不认 C 运行时的转义，会把反斜杠也当成路径的一部分 → 计划任务指向一个坏路径。
    #   直接给裸路径：没空格就不加引号，有空格 subprocess 会自己加，两种都对。
    args = ["/create", "/tn", task, "/sc", "daily", "/st", time_str,
            "/tr", str(bat), "/f"]
    r = _schtasks(args, timeout=30)
    invalidate_cache()
    if r is None:
        return {"ok": False, "task": task, "message": f"调用 schtasks 失败", "script": str(bat)}
    ok = r.returncode == 0
    msg = _decode(r.stdout or r.stderr).strip()[:400]
    if not ok:
        # ⚠ 这条提示以前写的是"试试右键 start.bat → 以管理员身份运行" —— **错的**。
        #   start.bat 是"启动服务"，跟建计划任务没有半点关系，照着做只会白跑一趟。
        #
        #   真实原因有两种，而且经常叠加：
        #   1. **旧的、由管理员建的**同名任务还在 —— 非提权进程用 `/f` 也覆盖不了它
        #      （拒绝访问）。这条最坑：用户刚从"以管理员身份跑"切过来，旧任务必然在。
        #   2. 非提权进程本来就建不了任务 —— 微软文档原话：
        #      *"Only Administrators can schedule tasks"*。
        #      **这跟开机自启不一样**：开机自启写的是当前用户自己的注册表分支
        #      （`HKCU\\...\\Run`），任何用户都能写；计划任务建在**系统任务库**里。
        #
        #   好在**只有建这一下**要权限：
        #   `/rl` 默认就是 `Limited`，所以哪怕提权去建，建出来的任务
        #   **照样是普通权限运行的** —— 不会把"服务变管理员"那个坑带回来。
        msg = (f"{msg or 'schtasks 返回非 0'}"
               "（注册计划任务失败。两件事要一起看："
               "① 这个动作**需要管理员权限** —— 计划任务建在系统任务库里，"
               "跟开机自启不一样（那个写自己的注册表，不需要权限）；"
               "② 如果以前用**管理员身份**建过同名任务，普通权限连 `/f` 覆盖不了它。"
               "点上面的「以管理员身份重试」即可 —— **只弹这一次 UAC**，"
               "而且建出来的任务照样是**普通权限运行**的（schtasks 的 /rl 默认 Limited）。"
               "或用下面这条命令在管理员命令行里手动注册）")
    return {
        "ok": ok, "task": task, "message": msg, "script": str(bat),
        "manual": " ".join(f'"{a}"' if " " in a else a for a in (["schtasks"] + args)),
    }


def _win_remove(name: str | None = None) -> dict:
    task = name or TASK_NAME
    r = _schtasks(["/delete", "/tn", task, "/f"])
    invalidate_cache()
    if r is None:
        return {"ok": False, "task": task, "message": "调用 schtasks 失败"}
    return {"ok": r.returncode == 0, "task": task,
            "message": _decode(r.stdout or r.stderr).strip()[:300] or "已删除"}


# ------------------------------------------------------------------------ unix
def _cron_lines() -> list[str]:
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return (r.stdout or "").splitlines()


_PERM_HINT = "（多半是没有权限改 crontab，或当前环境不允许）"


def _cron_write(lines: list[str]) -> tuple[bool, str]:
    body = "\n".join(lines).rstrip() + "\n"
    try:
        r = subprocess.run(["crontab", "-"], input=body, capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"调用 crontab 失败：{e}{_PERM_HINT}"
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[:300]
        return False, f"{msg or 'crontab 返回非 0'}{_PERM_HINT}"
    return True, "ok"


def _unix_tasks() -> list[dict]:
    out = []
    for line in _cron_lines():
        if CRON_MARK not in line:
            continue
        head, _, tail = line.partition(CRON_MARK)
        parts = head.split()
        hm = f"{parts[1].zfill(2)}:{parts[0].zfill(2)}" if len(parts) >= 2 else ""
        name = tail.strip() or TASK_NAME
        out.append({"name": name, "full_name": name, "time": hm, "command": head.strip(),
                    "enabled": True, "line": line})
    return out


def _unix_status() -> dict:
    tasks = _unix_tasks()
    if not tasks:
        return {"installed": False, "tasks": []}
    first = tasks[0]
    return {"installed": True, "tasks": tasks, "time": first["time"],
            "next_run": "", "line": first["line"], "name": first["name"]}


def _unix_install(root: Path, time_str: str, days_ago: int, config: str,
                  name: str | None = None) -> dict:
    sh = write_runner_script(root, config, days_ago)
    task = name or TASK_NAME
    hh, mm = time_str.split(":")
    # 路径带空格（macOS 上很常见）时，crontab 里不加引号会被拆成两个词
    line = f'{int(mm)} {int(hh)} * * * "{sh}" {CRON_MARK} {task}'
    # 只替换同名的那条，别的任务留着
    kept = [x for x in _cron_lines()
            if x.strip() and not (CRON_MARK in x
                                   and _same_task(x.partition(CRON_MARK)[2].strip()
                                                  or TASK_NAME, task))]
    ok, msg = _cron_write(kept + [line])
    return {
        "ok": ok, "task": task, "message": msg, "script": str(sh),
        "manual": f'(crontab -l; echo "{line}") | crontab -',
    }


def _unix_remove(name: str | None = None) -> dict:
    task = name or TASK_NAME
    kept = [x for x in _cron_lines()
            if not (CRON_MARK in x
                    and _same_task(x.partition(CRON_MARK)[2].strip() or TASK_NAME, task))]
    ok, msg = _cron_write(kept)
    return {"ok": ok, "task": task, "message": msg}


# ------------------------------------------------------------------------ API
def status(root: Path) -> dict:
    info = _win_status(root) if kind() == "windows" else _unix_status()
    info.setdefault("time", "")
    tasks = info.get("tasks") or []
    info.update({
        "installed": bool(tasks),
        "tasks": tasks,
        "platform": platform.system(),
        "kind": kind(),
        "task_name": TASK_NAME,
        # 老名字清单 —— 界面靠它认出"改名之前注册的那条任务"。
        # ⚠ 不同名就是**并存**，不是覆盖：老门店升级后会**一天跑两遍**
        #   （两条任务各自到点跑一次 run.bat）。必须提示，不能装作没看见。
        "legacy_names": list(LEGACY_TASK_NAMES),
        "script": str(script_path(Path(root))),
        "script_exists": script_path(Path(root)).exists(),
        "default_time": DEFAULT_TIME,
    })
    if tasks and not info.get("time"):
        info["time"] = tasks[0].get("time", "")
    # 每条任务「跑什么」—— 界面上单独一列。
    #
    # ⚠ 所有任务共用同一个 `run.bat`，所以它们的"跑什么"是**同一个值**
    #   （`.secrets/schedule.json` 里那份记录，或从脚本反推）。
    #   一天跑两次、一次只排查一次只 POS 是**做不到**的 —— 那不是这里漏了，
    #   是设计如此：任务只是"到点执行 run.bat"，跑什么由脚本决定。
    #   想要不同时间跑不同东西，得改 run.bat，现在没这个入口。
    #   （先如实写出来，别让界面显得它能做到。）
    from . import run_daily                     # 延迟 import：避免和 cli 绕圈
    auto = automation_steps(root)
    label = run_daily.steps_label(auto)
    for t in tasks:
        t.setdefault("steps", list(auto))
        t.setdefault("what_label", label)
    info["automation_steps"] = list(auto)
    return info


def install(root: Path, time_str: str, days_ago: int = DEFAULT_DAYS_AGO,
            config: str = "config/store-SCN231409.yaml",
            name: str | None = None) -> dict:
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", time_str or ""):
        # invalid=True 表示"请求本身不合法"（→ 400），跟"环境不允许"（→ 200 + ok:false）区分开
        return {"ok": False, "invalid": True,
                "message": f"时间格式不对：{time_str!r}（要 HH:MM，24 小时制）"}
    task = (name or "").strip() or default_task_name(time_str)
    if any(c in task for c in '\\/:*?"<>|'):
        return {"ok": False, "invalid": True,
                "message": "任务名不能带这些字符：\\ / : * ? \" < > |"}
    hh, mm = time_str.split(":")
    time_str = f"{int(hh):02d}:{mm}"
    fn = _win_install if kind() == "windows" else _unix_install
    res = fn(Path(root), time_str, days_ago, config, task)
    res["time"] = time_str
    if res.get("ok"):
        # ⚠ 注册成功就**立刻**记下参数 —— 这是整个记录机制的意义：
        #   一旦是提权建的（任务所有者 Administrators），普通权限以后
        #   `schtasks /query /xml` 会被拒，界面上的时间和命令就全靠这份记录。
        #   失败**不记**：没建成的任务不该在界面上装作存在。
        _remember(root, task, time_str=time_str, days_ago=days_ago, config=config)
    return res


def remove_all(root=None) -> dict:
    """删掉**所有**我们的定时任务 —— 卸载时用。

    ⚠ 跟 `remove(name)` 不一样：那个只删一条（界面上点哪行删哪行），
    这个是"把整套东西撤干净"，连开机自启那个任务也一起。

    `root` 传了就顺带把注册记录整个删掉（`_forget` 逐条不如直接删文件，
    因为注册记录里可能还留着**已经不存在**的任务）。
    """
    gone, failed = [], []
    if kind() == "windows":
        names = [n for n in _win_list_names() if _is_ours(n) or _leaf(n) == AUTOSTART_TASK]
        for n in names:
            r = _schtasks(["/delete", "/tn", n, "/f"])
            (gone if r is not None and r.returncode == 0 else failed).append(_leaf(n))
        invalidate_cache()
    else:
        lines = _cron_lines()
        keep = [x for x in lines if CRON_MARK not in x]
        if len(keep) != len(lines):
            ok, msg = _cron_write(keep)
            if ok:
                gone = [_cron_task_name(x) for x in lines if CRON_MARK in x]
            else:
                failed.append(msg)
    if root is not None and not failed:
        # ⚠ **删干净了才抹记录**：还有任务没删掉的话，那份记录是界面
        #   唯一能显示它的东西，抹了就真成"看不见也删不掉"了。
        try:
            record_path(root).unlink()
        except OSError:
            pass
    return {"ok": not failed, "removed": gone, "failed": failed,
            "message": (f"已删除 {len(gone)} 个定时任务"
                        + (f"，{len(failed)} 个删不掉：{'、'.join(failed)}" if failed else ""))}


def _cron_task_name(line: str) -> str:
    return (line.partition(CRON_MARK)[2].strip() or TASK_NAME) if CRON_MARK in line else ""


def run_now(name: str | None = None) -> dict:
    """立刻跑一次这个任务 —— **跟到点自动跑走的是同一条路**。

    为什么用 `schtasks /run` 而不是直接调 `check`：这样验证的才是**任务本身**
    （路径对不对、参数对不对、权限够不够）。直接调 check 只能证明代码没问题，
    证明不了"这条计划任务能不能跑起来"—— 而后者恰恰是用户想确认的。
    """
    task = (name or "").strip() or TASK_NAME
    if kind() == "windows":
        r = _schtasks(["/run", "/tn", task])
        if r is None:
            return {"ok": False, "task": task, "message": "调用 schtasks 失败"}
        ok = r.returncode == 0
        msg = _decode(r.stdout or r.stderr).strip()[:300]
        if not ok:
            return {"ok": False, "task": task,
                    "message": f"{msg or 'schtasks 返回非 0'}"
                               "（任务被禁用？或者名字对不上？）"}
        return {"ok": True, "task": task,
                "message": f"已触发「{task}」—— 它跑完会把报告写到 out 目录"}

    # unix 上 crontab 没有"立刻跑一次"这回事，直接执行那条脚本
    for line in _cron_lines():
        if CRON_MARK not in line:
            continue
        head, _, tail = line.partition(CRON_MARK)
        if (tail.strip() or TASK_NAME) != task:
            continue
        m = re.search(r'"([^"]+)"', head) or re.search(r"(\S+)$", head.strip())
        if not m:
            return {"ok": False, "task": task, "message": "这条 cron 里找不到脚本路径"}
        script = m.group(1)
        try:
            subprocess.Popen([script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            return {"ok": False, "task": task, "message": f"启动失败：{e}"}
        return {"ok": True, "task": task, "message": f"已触发「{task}」（{script}）"}
    return {"ok": False, "task": task, "message": f"没找到任务「{task}」"}


def remove(name: str | None = None, root=None) -> dict:
    """删掉一个定时任务。删掉了就顺手抹掉我们自己的注册记录。

    ⚠ `root` 不传时**不动记录**（老调用方 / 提权子进程删完就退出，
    记录在父进程那边抹）。传了就一并维护，免得界面上"删了还显示时间"。
    """
    res = _win_remove(name) if kind() == "windows" else _unix_remove(name)
    if res.get("ok") and root is not None:
        _forget(root, name or TASK_NAME)
    return res


# --------------------------------------------------- 老任务：认出来 + 换掉
#: 「表里还有老名字的任务」那个弹窗记这儿 —— **每版只弹一次**。
#: 放 `.secrets/`（自更新的 NEVER_TOUCH），跟 `whatsnew.json` 一个路子。
LEGACY_PROMPT_REL = ".secrets/legacy-prompt.json"


def is_legacy_name(name: str) -> bool:
    """这条任务名是不是**改名之前**那套（`CBG报量对账…`）。

    ⚠ 判据必须跟界面**一致**（`web/app.js` 那边也是按前缀比）——
    不一致的话会出现"界面说有、后端说没有"，而用户看到的就是按钮点了没反应。
    """
    leaf = _leaf(name or "")
    return any(leaf.startswith(n) for n in LEGACY_TASK_NAMES)


def legacy_task_names(root) -> list:
    """现在系统里还挂着的**老名字任务**的完整名（可能不止一条）。

    ⚠ 读不到任务列表时返回空 —— 于是"没有老任务"，弹窗不弹。
    **这个方向的错法是对的**：宁可漏弹一次，也不要瞎报"你有老任务要删"。
    """
    try:
        tasks = status(Path(root)).get("tasks") or []
    except Exception:                        # noqa: BLE001
        return []
    out = []
    for t in tasks:
        full = str(t.get("full_name") or t.get("name") or "")
        if full and is_legacy_name(full):
            out.append(full)
    return out


def replace_legacy(root, time_str: str = DEFAULT_TIME, days_ago: int = DEFAULT_DAYS_AGO,
                   config: str = "", name: str | None = None) -> dict:
    """把**老名字的任务**换成新的 —— ⚠⚠ **先建后删，顺序是这条需求的全部要害**。

    1. 先把新的建出来（`install`；`/create` 自带 `/f`，同名的一并覆盖）
    2. **确认建成了**才去删老的
    3. 建失败 ⇒ **一个老的都不许动**

    反过来的话（先删后建），中间任何一步失败都会落到
    **"门店再也不会自动跑"** —— 那是这条需求最坏的失败模式，
    而且**当天不会有人发现**（要等第二天到点没出报告才知道）。

    返回 `{ok, installed, legacy_before, removed, failed, message}`。
    """
    root = Path(root)
    old = legacy_task_names(root)

    res = install(root, time_str, days_ago, config, name=name)
    out = {"ok": bool(res.get("ok")), "installed": bool(res.get("ok")),
           "legacy_before": old, "removed": [], "failed": [],
           "install_message": res.get("message", "")}
    if not res.get("ok"):
        # ⚠⚠ **这条分支就是整条需求的护栏。** 新的没建成，老的一个都不许动 ——
        #   门店原来怎么跑现在还怎么跑，最坏也只是"老任务还在"，不会变成"啥都没有"。
        out["message"] = ("新任务**没建成**（%s）—— **老任务一条都没动**，"
                          "门店原来怎么跑现在还怎么跑。"
                          % (res.get("message") or "原因不明"))
        return out

    for full in old:
        r = _win_remove(full) if kind() == "windows" else _unix_remove(full)
        leaf = _leaf(full)
        if r.get("ok"):
            out["removed"].append(leaf)
            # 记录里那份也抹掉 —— 不然界面上"删了还显示时间"
            _forget(root, leaf)
        else:
            out["failed"].append(leaf)
    invalidate_cache()

    out["ok"] = not out["failed"]
    parts = ["新任务已建成"]
    if out["removed"]:
        parts.append("删掉老的 %d 条：%s" % (len(out["removed"]), "、".join(out["removed"])))
    if out["failed"]:
        parts.append("⚠ 有 %d 条老任务**删不掉**：%s —— 新的不受影响，"
                     "但这两条会一天跑两遍，得手动去任务计划程序里删"
                     % (len(out["failed"]), "、".join(out["failed"])))
    if not old:
        parts.append("表里本来就没有老任务")
    out["message"] = "；".join(parts) + "。"
    return out


def _prompt_path(root) -> Path:
    return Path(root) / LEGACY_PROMPT_REL


def legacy_prompt_pending(root) -> dict:
    """要不要弹「老任务要处理」那个框。

    两个条件**都**满足才弹：① 系统里真还挂着老名字的任务；② 这一版还没弹过。
    ⚠ 判据全在后端 —— 前端不自己记"弹过没"，那种状态放前端一定会漂。
    """
    names = legacy_task_names(root)
    if not names:
        return {"show": False, "names": [], "task_name": TASK_NAME}
    seen = ""
    try:
        seen = str(json.loads(_prompt_path(root).read_text(encoding="utf-8"))
                   .get("version") or "")
    except (OSError, ValueError, AttributeError):
        seen = ""
    from . import version                         # 延迟 import：避免和 cli 绕圈
    return {"show": seen != version.VERSION, "names": names,
            "version": version.VERSION, "task_name": TASK_NAME}


def mark_legacy_prompted(root) -> bool:
    """记下"这一版弹过了"，以后不再主动弹（设置页那个横幅照旧在）。"""
    from . import version
    p = _prompt_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": version.VERSION,
                                   "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)                            # 原子替换：写一半断电不留坏文件
        return True
    except OSError:
        # ⚠ 记不上不算失败 —— 大不了下次再弹一次，别为了记状态把界面卡住
        return False
