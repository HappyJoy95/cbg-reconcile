"""后台跑一次任务 + 日志缓冲。

前端点「运行」页的按钮 → 起一个子进程 → 轮询 `/api/run` 拿实时日志。
用子进程而不是线程，是为了拿到真实的退出码，并且跟手动跑的命令完全一致
（"界面里跑得通、命令行跑不通"这种问题不该存在）。

## 跑「什么」由 `what` 决定（2026-09-16 起）

原来这里只有一件事：跑对账。现在「运行」页有四个按钮 ——
抓数据 / 报量排查 / POS 合规 / 整个项目 —— 对应的就是 `what`。

⚠ **四个按钮都走 `daily` 这一个入口**，只是加不同的跳过开关。
走四条不同命令的话，`daily` 那些行为（第 1 步失败就不发报告、
库里没有就抓全量、会话失效先静默续期）在界面上就全都享受不到，
而且"界面跑的和定时任务跑的"迟早分叉。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from . import run_daily
from .winutil import quiet_kwargs

MAX_LINES = 2000          # 日志上限，防止一个跑飞的进程把内存吃光
KEEP_JOBS = 20


class RunJob:
    def __init__(self, job_id: str, argv: list[str], cwd: str):
        self.id = job_id
        self.argv = argv
        self.cwd = cwd
        self.what = "all"                  # 跑的是哪件（`what`，见 RunManager.start）
        self.what_label = ""               # 给人看的名字
        self.lines: list[str] = []
        self.running = True
        self.exit_code: int | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    def append(self, line: str):
        with self._lock:
            self.lines.append(line.rstrip("\n"))
            if len(self.lines) > MAX_LINES:
                del self.lines[: len(self.lines) - MAX_LINES]

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            total = len(self.lines)
            chunk = self.lines[since:] if since < total else []
        return {
            "id": self.id, "running": self.running, "exit_code": self.exit_code,
            "lines": chunk, "line_count": total, "since": since,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "command": " ".join(self.argv),
            "what": self.what,
            "what_label": self.what_label,
        }

    def kill(self):
        if self._proc and self.running:
            self._proc.terminate()


class RunManager:
    def __init__(self):
        self.jobs: dict[str, RunJob] = {}
        self.order: list[str] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> RunJob | None:
        return self.jobs.get(job_id)

    def latest(self) -> RunJob | None:
        return self.jobs[self.order[-1]] if self.order else None

    def current(self) -> RunJob | None:
        j = self.latest()
        return j if (j and j.running) else None

    # ---------------------------------------------------------------- 启动
    def start(self, root: Path, config: str, *, what: str = run_daily.DEFAULT_WHAT,
              days_ago: int | None = 1, date: str | None = None,
              lookback: int | None = None, lookahead: int | None = None) -> RunJob:
        # ⚠ 先算 flags —— `what` 认不出来要**在起进程之前**就炸（`flags_for` 会抛）。
        #   起完再炸的话会留一个半死的 job，界面上一直显示"在跑"。
        flags = run_daily.flags_for(what)
        with self._lock:
            if self.current():
                raise RuntimeError("已经有一个任务在跑了，等它结束")
            argv = [sys.executable or "python", "-u", "-m", "src.cli", "-c", config,
                    "daily", *flags]
            if date:
                argv += ["--date", date]
            elif days_ago is not None:
                argv += ["--days-ago", str(int(days_ago))]
            if lookback is not None:
                argv += ["--lookback", str(int(lookback))]
            if lookahead is not None:
                argv += ["--lookahead", str(int(lookahead))]

            job = RunJob(uuid.uuid4().hex[:12], argv, str(root))
            job.what = what
            job.what_label = run_daily.BUTTON_LABELS.get(what, what)
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > KEEP_JOBS:
                self.jobs.pop(self.order.pop(0), None)

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        try:
            job._proc = subprocess.Popen(
                argv, cwd=str(root), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
                # ⚠ 服务是 pythonw 跑的（没有控制台），起 python.exe 这个控制台程序时
                #   Windows 会**新开一个黑窗**，而且会挂满整个对账过程（一两分钟）。
                #   界面上点「运行」时用户看到的就是它。
                **quiet_kwargs(),
            )
        except OSError as e:
            job.append(f"[启动失败] {e}")
            job.running, job.exit_code, job.finished_at = False, -1, time.time()
            return job

        threading.Thread(target=self._pump, args=(job,), daemon=True).start()
        return job

    @staticmethod
    def _pump(job: RunJob):
        try:
            for line in job._proc.stdout:            # type: ignore[union-attr]
                job.append(line)
        finally:
            try:
                job.exit_code = job._proc.wait(timeout=30)   # type: ignore[union-attr]
            except Exception:                                # noqa: BLE001
                job.exit_code = -1
            job.running = False
            job.finished_at = time.time()
            job.append(f"[结束] 退出码 {job.exit_code}"
                       + ("（3 = 有差异，正常）" if job.exit_code == 3 else ""))


manager = RunManager()
