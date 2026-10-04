"""后台服务：记录进程、查状态、停掉它。

`start.bat` 双击后要能：
  1. 发现已经在跑 → 直接开浏览器，别起第二个
  2. 没在跑 → 后台起来 → **确认真的起来了**再报"启动成功"
  3. 报完就关窗口，服务继续在后台待着

所以要有个地方记住「谁在跑、跑在哪个端口」→ `.secrets/server.json`。

⚠ Windows 上**不能用 `os.kill(pid, 0)` 探活** —— Python 在 Windows 上会把
除 CTRL_C_EVENT/CTRL_BREAK_EVENT 之外的信号直接当成 `TerminateProcess(pid, sig)`，
也就是**会把进程杀掉**。探活必须走 tasklist。
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .winutil import quiet_kwargs

STATE_FILE = ".secrets/server.json"
DEFAULT_PORT = 8787
_APP_TAG = "cbg-reconcile"


def state_path(root) -> Path:
    return Path(root) / STATE_FILE


def read_state(root) -> dict | None:
    p = state_path(root)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(root, *, pid: int, host: str, port: int) -> Path:
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"app": _APP_TAG, "pid": pid, "host": host, "port": port,
                             "started_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                            ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def clear_state(root) -> None:
    try:
        state_path(root).unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if platform.system() == "Windows":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                                 **quiet_kwargs(),
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(int(pid)) in (out.stdout or "")
    try:
        os.kill(int(pid), 0)          # 0 = 只探测，不真发信号（POSIX 上安全）
        return True
    except OSError:
        return False


def health(host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict | None:
    """问一下服务在不在。返回 /api/health 的内容，不通就 None。"""
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if d.get("app") == _APP_TAG else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def find_running(root, default_port: int = DEFAULT_PORT) -> dict | None:
    """正在跑的服务。先信状态文件，再退一步试默认端口。"""
    st = read_state(root)
    if st and health(st.get("host", "127.0.0.1"), st.get("port", default_port)):
        return st
    info = health("127.0.0.1", default_port)
    if info:
        return {"app": _APP_TAG, "pid": info.get("pid"), "host": "127.0.0.1",
                "port": default_port, "started_at": info.get("started_at", "")}
    return None


def wait_ready(root, timeout: float = 25.0, interval: float = 0.5) -> dict | None:
    """等后台服务起来。start.bat 靠这个决定报"启动成功"还是"启动失败"。"""
    end = time.time() + timeout
    while time.time() < end:
        got = find_running(root)
        if got:
            return got
        time.sleep(interval)
    return None


def stop(root, default_port: int = DEFAULT_PORT, timeout: float = 8.0) -> tuple[bool, str]:
    """先好好说（HTTP shutdown），说不通再动手（杀进程）。"""
    st = find_running(root, default_port)
    if not st:
        clear_state(root)
        return False, "没有在运行的服务"

    host, port = st.get("host", "127.0.0.1"), st.get("port", default_port)
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/shutdown", method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError):
        pass                                  # 关了之后连接断掉是正常的

    end = time.time() + timeout
    while time.time() < end:
        if not health(host, port, timeout=1):
            clear_state(root)
            return True, f"已停止（端口 {port}）"
        time.sleep(0.4)

    pid = st.get("pid")
    if pid and pid_alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
            time.sleep(1.0)
        except OSError as e:
            # 开机自启注册成了"以管理员身份运行"的话，普通权限的 stop.bat 是杀不掉它的。
            # 正常情况下走不到这里（前面 HTTP 那条路跟权限无关，能自关），
            # 只有服务卡死、HTTP 不响应时才会落到强杀。
            return False, (f"停不掉（PID {pid}）：{e}。"
                           "如果服务是以管理员身份启动的，普通权限杀不掉它 —— "
                           "右键 stop.bat →「以管理员身份运行」再试一次")
        if not pid_alive(pid):
            clear_state(root)
            return True, f"已强制停止（PID {pid}）"
        return False, (f"进程 {pid} 还在，请到任务管理器结束它"
                       "（服务是管理员身份的话，任务管理器也要以管理员身份打开）")

    clear_state(root)
    return True, "状态文件已清理（进程本来就不在了）"
