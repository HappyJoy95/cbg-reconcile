"""跨进程互斥锁。

**为什么需要它**：云商**不能并行登录** —— 两个进程同时登录，四个域会一起报
「登录超时」，而且换新 token 也没用，得等几分钟自愈。

什么时候会撞上：计划任务 09:00 跑一次，正好有人点了「立即运行」；
或者手动补跑时上一次还没结束。**Web 界面里那把锁只管得住界面自己，管不住计划任务。**

实现：`O_CREAT|O_EXCL` 原子建文件。文件里记 PID + 开始时间，
**持有者进程已经死了就抢过来** —— 否则程序崩一次就永久锁死了。
"""

from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path

from .service import pid_alive

DEFAULT_STALE = 1800          # 半小时。一次对账正常几十秒，含自动续期也就几分钟


class Lock:
    def __init__(self, path, stale_after: int = DEFAULT_STALE):
        self.path = Path(path)
        self.stale_after = stale_after
        self._held = False

    # ------------------------------------------------------------------ 信息
    def holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _live(self, info: dict) -> bool:
        age = time.time() - float(info.get("started_at", 0) or 0)
        if age > self.stale_after:
            return False
        return pid_alive(info.get("pid") or 0)

    # ------------------------------------------------------------------ 加锁
    def acquire(self) -> tuple[bool, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):                     # 第一次撞上就抢，抢完再试一次
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                info = self.holder()
                if info and self._live(info):
                    age = int(time.time() - float(info.get("started_at", 0) or 0))
                    return False, (f"已经有一个对账在跑（PID {info.get('pid')}，"
                                   f"开始了 {age} 秒）")
                # 持有者已经死了 / 太久没动静 → 抢过来
                try:
                    self.path.unlink()
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "started_at": time.time(),
                           "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "host": platform.node()}, f, ensure_ascii=False)
            self._held = True
            return True, "ok"
        return False, "抢锁失败（有别人一直在抢）"

    def release(self) -> None:
        if not self._held:
            return
        try:
            info = self.holder()
            # 只删自己的锁 —— 万一被判定过期抢走过，别把别人的删了
            if not info or info.get("pid") == os.getpid():
                self.path.unlink()
        except OSError:
            pass
        self._held = False

    def __enter__(self):
        # ⚠ 必须真的去抢 —— 只 return self 的话 `with Lock(p):` 是个静默空操作，
        #   看着像加了锁其实没有（测试抓到过）。
        ok, why = self.acquire()
        if not ok:
            raise RuntimeError(why)
        return self

    def __exit__(self, *exc):
        self.release()
        return False
