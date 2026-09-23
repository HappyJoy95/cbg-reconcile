"""后台跑一次任务 + 日志缓冲。

前端点「运行」页的按钮 → 起一个子进程 → 轮询 `/api/run` 拿实时日志。
用子进程而不是线程，是为了拿到真实的退出码，并且跟手动跑的命令完全一致
（"界面里跑得通、命令行跑不通"这种问题不该存在）。

## 跑「什么」由 `what` 决定（2026-09-16 起）

原来这里只有一件事：跑对账。后来「运行」页拆成四个按钮 ——
抓数据 / 报量排查 / POS 合规 / 整个项目 —— 对应的就是 `what`。

⚠ **2026-09-17 起界面上只剩「整个项目」一个按钮**（用户定），但 `what`
这套名字留着：`/api/run` 和命令行都还认，`daily --skip-dump` 那套一步没动。

⚠ **所有按钮都走 `daily` 这一个入口**，只是加不同的跳过开关。
走几条不同命令的话，`daily` 那些行为（第 1 步失败就不发报告、
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


#: **内部步骤** —— 跑了、也记日志，但**不进前端**（用户 2026-09-21 明说）。
#: ⚠ 这些名字是**步骤的 cmd**（`features/registry.py`）；加新的一条要想清楚
#:   "门店需不需要在界面上看见它"。
INTERNAL_STEPS = ("autoupdate",)


def is_internal_job(job) -> bool:
    """这趟是不是**内部步骤**（自动更新）—— 看命令行里的 `--steps`。

    ⚠ 认的是 `--steps` 的值，不是 `what`：定时器派发的所有任务 `what` 都是 `"wake"`
      （`what_label` 也是），区分不出来。看 `--steps` 是唯一稳的判据。
    """
    argv = list(getattr(job, "argv", None) or [])
    try:
        steps = argv[argv.index("--steps") + 1]
    except (ValueError, IndexError):
        return False
    got = [s for s in str(steps).split(",") if s]
    return bool(got) and all(s in INTERNAL_STEPS for s in got)


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

    def latest_visible(self) -> RunJob | None:
        """**最近一趟"该给人看的"任务** —— 跳过内部步骤（自动更新）。

        ⚠ 用户 2026-09-21：「自动更新**不进入计时器前端显示，前端日志也不显示**」。
          自动更新每小时都跑一趟，`latest()` 拿到的基本永远是它 ⇒
          抽屉里的「运行日志」天天显示一行"已经是最新版"，
          而门店真正关心的那趟对账日志被顶掉了。
        ⚠ 它**照样记进 runlog**（排查要用），只是**不往前端露**。
        """
        for jid in reversed(self.order):
            job = self.jobs.get(jid)
            if job is not None and not is_internal_job(job):
                return job
        return None

    def current(self) -> RunJob | None:
        j = self.latest()
        return j if (j and j.running) else None

    # ---------------------------------------------------------------- 启动
    # ⚠ 2026-09-21 晚：**`start(what=…)` 那套"预设"删了** —— 界面上那个
    #   「跑一次」的卡没有了，"整个项目 / 抓华为数据 / POS 合规"三个 what 也就
    #   没有调用方了（`BUTTON_STEPS` / `flags_for` 一起删）。
    #   现在起一趟只有两条路，**都必须点名跑哪几步**：
    #     * `start_steps()` —— 各页「刷新」（先抓一次新数据）；
    #     * `start_argv()`  —— 内置定时器（`timer.wake_argv` 拼好的 `daily --steps …`）。
    #   ⚠ 别再往回加"不给步骤就跑一大套"的默认：那正是"三处各说一套"的来源。
    def start_steps(self, root, config: str, steps, *,
                    what_label: str = "抓新数据") -> RunJob:
        """**按指定的几步**跑一趟 `daily` —— 「刷新」按钮先抓新数据走这条。

        ⚠ 用 `--steps`（**就这几步**）而不是 `--skip-*`：后者会被
          `ALWAYS_STEPS`（dump/attain）补回来，于是"只抓云商 + 算达成"
          会变成"整批都跑"，而界面上写着"正在抓新数据"。
        ⚠ 跟手动「跑一次」、跟内置定时器**同一条路**（同一把锁、日志进同一个抽屉、
          退出码有人收）—— 各起各的进程迟早就分叉。
        """
        argv = [sys.executable or "python", "-u", "-m", "src.cli", "-c", config,
                "daily", "--steps", ",".join(str(x) for x in steps)]
        return self._spawn(root, argv, what="refresh", what_label=what_label)

    def start_argv(self, root, argv, *, what: str = "wake",
                   what_label: str = "内置定时器") -> RunJob:
        """**按现成的命令**起一趟 —— 内置定时器（`timer.tick`）走这条。

        ⚠ 为什么不让定时器自己 `subprocess.Popen`：控制台里的每一趟都该是
          **同一条路** —— 右下角抽屉能看到日志、`RunManager` 会把并发挡住、
          退出码有人收。定时器绕过去的话，门店会遇到"它自己跑了一趟，
          界面上什么都没有"，而那种时候正是要去看日志的时候。
        """
        return self._spawn(root, list(argv), what=what, what_label=what_label)

    def _spawn(self, root, argv, *, what: str, what_label: str) -> RunJob:
        with self._lock:
            if self.current():
                raise RuntimeError("已经有一个任务在跑了，等它结束")
            job = RunJob(uuid.uuid4().hex[:12], argv, str(root))
            job.what = what
            job.what_label = what_label
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
