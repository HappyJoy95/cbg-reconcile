#!/usr/bin/env python3
"""计划任务的入口 —— **保证不管出什么事都记进日志**。

为什么需要这个文件：

`run.bat` 用 `pythonw.exe` 启动（它没有控制台窗口，不会弹黑窗，用户明确要求过）。
但代价是 **`pythonw` 连 stderr 都没有** —— `sys.stdout` / `sys.stderr` 直接是 `None`。
于是只要在 `import src.cli` 那一步挂了（缺依赖、路径不对、假 python…），
**traceback 一个字都留不下来**，日志里只剩一个莫名其妙的退出码。

门店实测就是这个形状：
    [2026-09-15 14:23:36.91] exit=120
    [2026-09-15 14:24:11.83] exit=120
只有退出码、没有任何对账输出 —— 完全查不出原因。

所以这里先**把日志打开、把 stdout/stderr 都接过去**，再去 import 真正的 CLI。
从这一行开始，后面发生的任何事都会落在 `out/run.log` 里。

用法（run.bat / run-now.bat 都调它）：
    pythonw run_check.py -c "config/store-XXX.yaml" check --days-ago 1
"""

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

LOG = ROOT / "out" / "run.log"


class _Sink:
    """`pythonw` 下 sys.stdout 是 None —— 得有个能吞东西的替身。"""

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return None


class _Tee:
    """屏幕和日志两边都写。

    ⚠ **每行刷盘**：stdout 重定向到文件是块缓冲，进程被 kill / 卡住 / 崩了
    缓冲区整个丢掉 —— 而日志的全部意义就是"出事之后还能看"。
    """

    def __init__(self, stream, fp):
        self.stream, self.fp = stream, fp

    def write(self, s):
        for target in (self.stream, self.fp):
            try:
                target.write(s)
            except (OSError, ValueError, AttributeError):
                pass
        if s.endswith("\n"):
            self.flush()
        return len(s)

    def flush(self):
        for target in (self.stream, self.fp):
            try:
                target.flush()
            except (OSError, ValueError, AttributeError):
                pass

    def isatty(self):
        try:
            return bool(self.stream.isatty())
        except Exception:                          # noqa: BLE001
            return False

    def __getattr__(self, name):
        return getattr(self.stream, name, None)


def open_log():
    """打开日志；打不开就在屏幕上说一声（打不开也比崩掉强）。"""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        return open(LOG, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        try:
            sys.stderr.write(f"[run_check] 日志打不开：{e}\n")
        except Exception:                          # noqa: BLE001
            pass
        return None


def main() -> int:
    fp = open_log()
    real_out = sys.stdout if sys.stdout is not None else _Sink()
    real_err = sys.stderr if sys.stderr is not None else _Sink()
    if fp is not None:
        sys.stdout = _Tee(real_out, fp)
        sys.stderr = _Tee(real_err, fp)

    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始"
          f"（python {sys.version.split()[0]}） ===")

    rc = 9
    try:
        from src.cli import main as cli_main     # ← 这一步最容易出问题，它在日志里了
        rc = cli_main(sys.argv[1:])
        if not isinstance(rc, int):
            rc = 0
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    except BaseException:                        # noqa: BLE001
        # ⚠ 这里是关键：pythonw 没有 stderr，不这么写 traceback 就永远看不到
        traceback.print_exc()
        rc = 9

    print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 结束 exit={rc} ===")
    if fp is not None:
        try:
            fp.flush()
            fp.close()
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
