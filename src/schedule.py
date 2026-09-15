"""定时执行：Windows 计划任务（schtasks）/ unix crontab。

门店电脑是 Windows，开发机是 macOS —— 两套都实现，前端只调
`status()` / `install()` / `remove()` 三个函数。

设计取舍：**先写一个 run 脚本，再让计划任务去调它**。
这样日志重定向、工作目录、编码这些脏活都在脚本里解决一次，
计划任务本身只负责"每天几点叫我"。
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from .autostart import AUTOSTART_TASK
from .winutil import decode as _decode, parse_xml, schtasks as _schtasks, xml_text

TASK_NAME = "CBG报量对账"
LOG_NAME = "run.log"          # 计划任务跑完留下的日志（out\ 下）
# ⚠ 生成的脚本要能被**认出来是哪一版**。
#   run.bat 是**注册任务时**生成的，升级代码**不会**动它 ——
#   所以门店电脑上很容易留着一个旧脚本（用户就踩到了：日志格式还是旧的，
#   而且黑窗、退出码这些修好的东西一样没生效）。
#   启动时比对这一行，不一致就重建。
RUNNER_MARK = "rem cbg-runner v4"
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
    """计划任务里用什么解释器。用绝对路径，免得计划任务的环境变量跟交互式不一样。"""
    return sys.executable or ("python" if kind() == "windows" else "python3")


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


def write_runner_script(root: Path, config: str, days_ago: int = DEFAULT_DAYS_AGO) -> Path:
    root = Path(root)
    path = script_path(root)
    # ⚠ 解释器和脚本路径**都要加引号** —— Windows 上 Python 常装在
    #   `C:\Program Files\Python311\`，不加引号 cmd 会把 `C:\Program` 当命令，
    #   计划任务就每天静默失败。类 Unix 上路径带空格同理。
    # ⚠ **不要在 bat 里写 `>> out\run.log`**：那样屏幕上什么都没有，
    #   双击的人看到的是黑窗口 + 一分多钟 + 自己关掉，完全判断不了跑没跑。
    #   日志交给 Python 分流（--log-file），屏幕和文件两边都有。
    base = f'"{_pythonw()}" -m src.cli -c "{config}" check --days-ago {int(days_ago)}'
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
            f'start "" "{pyw}" "run_check.py" -c "{config}" check --days-ago {int(days_ago)}\r\n'
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
        mbase = f'"{_python()}" -m src.cli -c "{config}" check --days-ago {int(days_ago)}'
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
                f'"{_python()}" "run_check.py" -c "{config}" check --days-ago {int(days_ago)}\r\n'
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
        write_runner_script(root, config, days)
    except OSError:
        return False
    return True


def default_task_name(time_str: str) -> str:
    r"""默认任务名**带上执行时间**，比如 21:00 → `CBG报量对账-21点00`。

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
    return TASK_NAME in leaf


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


def _win_task_info(name: str) -> dict:
    """单个任务的详情。**优先用 /xml** —— 里面的标签名是固定的英文，不受系统语言影响。"""
    info = {"name": _leaf(name), "full_name": name, "time": "", "command": "",
            "enabled": None, "detail_source": ""}
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


# `schtasks` 每次要起一个进程（列任务 ~几百毫秒，XML 再一个进程）。
# 概览页每 30 秒刷一次，不缓存的话光是看页面就在不停 fork。
# 10 秒足够短 —— 用户点完注册/删除我们会**主动作废缓存**，看到的还是实时的。
_LIST_CACHE: dict = {"at": 0.0, "tasks": []}
_LIST_TTL = 10.0


def invalidate_cache() -> None:
    _LIST_CACHE["at"] = 0.0


def _win_list(force: bool = False) -> list[dict]:
    now = time.time()
    if not force and now - _LIST_CACHE["at"] < _LIST_TTL:
        return _LIST_CACHE["tasks"]
    tasks = [_win_task_info(n) for n in _win_list_names() if _is_ours(n)]
    _LIST_CACHE["at"] = now
    _LIST_CACHE["tasks"] = tasks
    return tasks


def _win_status() -> dict:
    tasks = _win_list()
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
        msg = (f"{msg or 'schtasks 返回非 0'}"
               "（注册计划任务失败 —— 可能是权限不够，"
               "试试右键 start.bat → 以管理员身份运行；或用下面这条命令手动注册）")
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
            if x.strip() and not (CRON_MARK in x and (x.partition(CRON_MARK)[2].strip() or TASK_NAME) == task)]
    ok, msg = _cron_write(kept + [line])
    return {
        "ok": ok, "task": task, "message": msg, "script": str(sh),
        "manual": f'(crontab -l; echo "{line}") | crontab -',
    }


def _unix_remove(name: str | None = None) -> dict:
    task = name or TASK_NAME
    kept = [x for x in _cron_lines()
            if not (CRON_MARK in x and (x.partition(CRON_MARK)[2].strip() or TASK_NAME) == task)]
    ok, msg = _cron_write(kept)
    return {"ok": ok, "task": task, "message": msg}


# ------------------------------------------------------------------------ API
def status(root: Path) -> dict:
    info = _win_status() if kind() == "windows" else _unix_status()
    info.setdefault("time", "")
    tasks = info.get("tasks") or []
    info.update({
        "installed": bool(tasks),
        "tasks": tasks,
        "platform": platform.system(),
        "kind": kind(),
        "task_name": TASK_NAME,
        "script": str(script_path(Path(root))),
        "script_exists": script_path(Path(root)).exists(),
        "default_time": DEFAULT_TIME,
    })
    if tasks and not info.get("time"):
        info["time"] = tasks[0].get("time", "")
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
    return res


def remove_all() -> dict:
    """删掉**所有**我们的定时任务 —— 卸载时用。

    ⚠ 跟 `remove(name)` 不一样：那个只删一条（界面上点哪行删哪行），
    这个是"把整套东西撤干净"，连开机自启那个任务也一起。
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


def remove(name: str | None = None) -> dict:
    if kind() == "windows":
        return _win_remove(name)
    return _unix_remove(name)
