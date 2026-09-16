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


def rewrite_legacy_check(argv, ap):
    """老 `run.bat` 里的 `check` → `daily`。返回 `(argv, 改没改)`。

    ## 为什么需要这个垫片

    `run.bat` / `run-now.bat` 是**安装时生成**的，而且**不进版本库**
    （`.gitignore` 里有它们）—— 所以自更新**不会重写它们**，
    门店电脑上那份会一直写着 `check --days-ago 1`。

    而 2.0.0 的 `check` **只从本地库读**华为数据了：没人跑第 1 步（`dump`），
    库就永远是旧的，`check` 每天都会以「库不新鲜」失败 ——
    **每家已装门店升级后第 2 天起就没有对账报告了。**

    这个文件（`run_check.py`）**在版本库里**，自更新会覆盖它 ——
    所以它是唯一能在"不改门店那个 bat"的前提下把命令换掉的地方。

    界面上重新注册一次定时任务、或者把 `refresh_runner_scripts()` 的自愈
    接上之后，`run.bat` 会自己写成 `daily`，那时这个垫片就只是兜底了。

    ## 为什么不硬编码"-c 吃一个值"

    那些"吃一个值"的选项从**真解析器**里读。硬编码一份的话，
    哪天全局选项变了（比如 `-c` 加了别名），扫描就会错位 ——
    错位的表现是**把配置文件路径当成子命令**，然后什么都不改，
    静默回到"每天失败"的老样子。
    """
    takes_value = set()
    for a in ap._actions:
        if a.option_strings and a.nargs != 0:
            takes_value.update(a.option_strings)

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in takes_value:                       # `-c <值>`：跳过它的值
            i += 2
            continue
        if any(tok.startswith(o + "=") for o in takes_value):   # `--config=<值>`
            i += 1
            continue
        if tok.startswith("-"):                      # 别的开关
            i += 1
            continue
        # 第一个非选项 token 就是子命令
        if tok == "check":
            return argv[:i] + ["daily"] + argv[i + 1:], True
        return argv, False                           # 别的子命令，不碰
    return argv, False


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
        from src.cli import build_parser, main as cli_main   # ← 这一步最容易出问题，它在日志里了
        argv, legacy = rewrite_legacy_check(sys.argv[1:], build_parser())
        if legacy:
            # 写清楚"我替你改了"，不然日志里出现 daily 会让人以为 bat 已经更新了
            print("[run_check] 这个启动脚本还是旧版（写的是 check）——"
                  "按**日常流程**执行（daily：抓华为当月 → 对账 → 算 POS）。")
            print("[run_check] 只想要对账、不抓数据的话，直接跑"
                  " `python -m src.cli check`（绕开本启动器）。")
        rc = cli_main(argv)
        if not isinstance(rc, int):
            rc = 0
    except SystemExit as e:
        if isinstance(e.code, int):
            rc = e.code
        elif e.code is None:
            rc = 0
        else:
            # ⚠ `raise SystemExit("消息")` / `sys.exit("消息")` 的 code 是**字符串**。
            #   老写法 `e.code if isinstance(e.code, int) else 0` 把它变成 **0 = 成功**，
            #   而且因为我们把异常**接住了**，Python 也不会替我们去印那条消息 ——
            #   于是日志里只剩一句 `结束 exit=0`。
            #   实测：配置路径写错时就是这样，"看着像跑成功了，其实什么都没干"。
            #   这里恢复 Python 自己那套语义（消息进 stderr、退出码 1）。
            print(e.code, file=sys.stderr)
            rc = 1
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
