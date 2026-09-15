#!/usr/bin/env python3
"""统一的启动入口 —— **只用标准库**。

为什么需要它：`src/cli.py` 顶部就 import requests，**没装依赖时根本起不来**。
而 `install.bat` 的职责恰恰是"装依赖" —— 这就成了先有鸡还是先有蛋：
在全新的门店电脑上双击 install.bat，只会甩一个英文 ImportError 出来。

所以所有 .bat 都走这里：
    python bootstrap.py install          装依赖（然后问一句要不要开机自启）
    python bootstrap.py autostart        只注册开机自启（可被提权后单独调用）
    python bootstrap.py uninstall        卸载：撤掉服务 / 定时任务 / 开机自启
                         加 --yes 跳过确认；加 --purge 连报告和凭据一起删
    python bootstrap.py service-start    后台启动（start.bat）
    python bootstrap.py stop             停止（stop.bat）
    python bootstrap.py selftest         逐项自检（selftest.bat）

**`install` 和 `autostart` 只用标准库**，依赖没装也能跑 —— `autostart` 会被
install 用 UAC 单独提权重新拉起来（见 `relaunch_as_admin`）。

中文提示也在这里打印 —— Python 在 Windows 上走 WriteConsoleW，
跟控制台代码页无关，不会像批处理那样显示成方块。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

REQUIRED = ("requests", "yaml", "openpyxl")       # 模块名（yaml 是 PyYAML）
STDLIB_ONLY = ("install", "install-deps", "autostart", "uninstall")  # 不碰第三方包

# 门店电脑装的是 Python 3.14（2026 年的最新版），开发机是 3.9 —— 所以代码按
# **3.9+ 都能跑** 写，两头都测过。这里只拦"实在太旧"的版本。
MIN_PYTHON = (3, 9)


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def check_python() -> int:
    """版本够不够。**先说清楚现在是多少** —— 门店电脑上版本不对时，
    "报了个错"和"你现在是 3.8，请装 3.14"完全是两回事。
    """
    v = sys.version_info[:2]
    if v >= MIN_PYTHON:
        return 0
    need = ".".join(str(x) for x in MIN_PYTHON)
    print(f"Python 版本太旧：现在 {python_version()}，需要 {need} 或更高。")
    print()
    print("去 https://www.python.org/downloads/ 装最新版（3.14），")
    print("安装时**务必勾选 Add python.exe to PATH**，装完重开一个窗口再跑。")
    return 2


def missing() -> list[str]:
    import importlib.util
    return [m for m in REQUIRED if importlib.util.find_spec(m) is None]


def write_build_stamp() -> None:
    """这个目录只有 `.git`、没有 `BUILD.txt` 时，补一个构建指纹。

    为 **clone 部署**准备：正式包的 BUILD.txt 是打包时写好的，clone 下来的
    没有（那文件是未跟踪的），于是界面和自检只能显示"源码运行（未打包）" ——
    偏偏那是出问题时最想知道的一行。

    读的是 `.git` 里的文件，**不调 git 命令**（门店电脑上不装 git）。
    只补空缺，不覆盖已有的 —— 别把打包（或自更新）写好的时间戳冲掉。

    任何失败都只打印一行，**绝不能因此让安装失败**。
    """
    build_file = ROOT / "BUILD.txt"          # 用自己的 ROOT，别去借 src.version 的
    if build_file.exists():
        return
    try:
        from src.version import git_stamp
        stamp = git_stamp(ROOT)              # 问的是**这个**目录，不是 src 包所在的那个
        if not stamp:
            return
        build_file.write_text(stamp + "\n", encoding="utf-8")
    except Exception as e:                                   # noqa: BLE001
        print(f"    · 构建指纹没写成（不影响使用）：{e}")
        return
    print(f"    · 构建指纹：{stamp}（从 .git 读的）")


def do_install() -> int:
    req = ROOT / "requirements.txt"
    print("正在安装依赖（requests / pyyaml / openpyxl）…")
    print(f"  Python：{python_version()}")
    print(f"  解释器：{sys.executable}")
    print(f"  依赖清单：{req}")
    print()
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    except OSError as e:
        print(f"启动 pip 失败：{e}")
        return 2
    print()
    if r.returncode != 0:
        print("依赖安装失败。检查网络，或换国内镜像重试：")
        print(f'  "{sys.executable}" -m pip install -r "{req}" '
              "-i https://pypi.tuna.tsinghua.edu.cn/simple")
        return 2
    left = missing()
    if left:
        print(f"装完了但还是找不到：{', '.join(left)} —— 多半是装到了别的 Python 环境")
        return 2
    write_build_stamp()
    print("依赖装好了。接下来双击 start.bat 启动控制台。")
    return 0


def is_admin() -> bool:
    """当前进程有没有管理员权限。

    非 Windows 直接返回 True —— 别的平台没这个概念，别拿它拦事情。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:                        # noqa: BLE001
        return False


def relaunch_as_admin(cmd: str) -> bool:
    """用 UAC 把 `bootstrap.py <cmd>` 重新拉起来（管理员身份）。

    **只在注册开机自启这一步用** —— 不在装依赖之前提权。原因：
    如果这台电脑是"标准用户 + 管理员账号密码"的配置，UAC 提权后跑的 pip
    会装进**那个管理员账号**的 site-packages 里，普通用户这边根本 import 不到。
    所以依赖照旧用当前用户装，只有"写系统开机项"这一步需要提权。

    返回 True = 已经交给提权后的窗口了，**当前进程该收工了**；
    返回 False = 没提成（用户点了"否" / 账号不是管理员），继续按普通权限跑。
    """
    import ctypes
    params = f'"{ROOT / "bootstrap.py"}" {cmd} --pause'
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, str(ROOT), 1)
    except Exception as e:                   # noqa: BLE001
        print(f"  请求提权失败：{e}")
        return False
    # ShellExecuteW 约定：返回值 <= 32 表示失败。5 = 拒绝访问（用户点了"否"）
    if rc <= 32:
        print("  没拿到管理员权限（UAC 被拒绝？），改用普通权限注册。")
        return False
    return True


def describe_autostart(res: dict) -> None:
    print(("  ✓ " if res.get("ok") else "  ✗ ") + str(res.get("message", "")))


def do_autostart() -> int:
    """只注册开机自启 —— 被 install 提权后单独调用，也可以在界面上触发。"""
    from src import autostart
    try:
        res = autostart.install(ROOT)
    except Exception as e:                   # noqa: BLE001
        print(f"  ✗ 注册失败：{e}")
        return 2
    describe_autostart(res)
    return 0 if res.get("ok") else 2


def do_uninstall(yes: bool = False, purge: bool = False) -> int:
    """卸载：把我们对这台电脑做过的改动**全部撤掉**。

    撤的是"痕迹"，不是"文件"：
      1. 停掉后台服务
      2. 删掉所有定时任务（每天对账的 + 开机自启的）
      3. 删掉开机自启的注册表项
      4. 问一句要不要连凭据和报告一起删
      5. 打印还剩下什么

    **目录本身不删** —— 一个正在跑的脚本删不掉自己所在的目录，
    而且用户多半想先留着报告。最后告诉他手动删哪个目录。
    """
    from src import autostart, schedule

    print()
    print("=" * 46)
    print("  卸载 CBG 报量对账")
    print("=" * 46)
    print(f"  目录：{ROOT}")
    print()
    print("  会撤掉这些：")
    print("    · 后台服务（先停掉）")
    print("    · 所有定时任务（每天对账的、开机自启的）")
    print("    · 开机自启的注册表项")
    print()
    print("  **不会**动这些（要删得你自己来）：")
    print("    · 程序目录本身")
    print("    · out\\ 里的历史报告")
    print("    · .secrets\\ 里的账号和会话")
    print()
    print("  历史报告和凭据默认**保留** —— 要一起删加 --purge，或在下面回答 y。")
    print()

    if not yes:
        if not (sys.stdin and sys.stdin.isatty()):
            print("非交互环境，没删任何东西。要卸载请加 --yes。")
            return 2
        try:
            ans = input("  确定要卸载吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
        if ans not in ("y", "yes", "是", "1"):
            print("  已取消，什么都没动。")
            return 0

    problems = []

    # 1) 停服务
    print()
    print("  [1/4] 停后台服务…")
    try:
        from src import service
        stopped, msg = service.stop(ROOT)
        print(f"        {msg}")
    except Exception as e:                        # noqa: BLE001
        print(f"        跳过（{e}）")

    # 2) 定时任务
    print("  [2/4] 删定时任务…")
    try:
        res = schedule.remove_all()
        for n in res.get("removed", []):
            print(f"        已删除「{n}」")
        if not res.get("removed"):
            print("        没有找到我们的定时任务")
        if res.get("failed"):
            problems.append("有些定时任务删不掉（可能要管理员权限）")
    except Exception as e:                        # noqa: BLE001
        problems.append(f"删定时任务出错：{e}")

    # 3) 开机自启（注册表项 + 可能残留的任务）
    print("  [3/4] 取消开机自启…")
    try:
        r = autostart.remove()
        print(f"        {r.get('message', '')}")
    except Exception as e:                        # noqa: BLE001
        problems.append(f"取消开机自启出错：{e}")

    # 4) 数据
    print("  [4/4] 数据…")
    # ⚠ `--yes` 只跳过"确定要卸载吗"这一步，**不代表要删数据**。
    #   删报告的破坏性太大，必须单独一个 --purge（或者交互里明确回答 y）。
    wipe = purge
    if not purge and sys.stdin and sys.stdin.isatty():
        try:
            a = input("        连 .secrets\\（账号/会话）和 out\\（报告）一起删？"
                      "**删了就恢复不了** [y/N] ").strip().lower()
            wipe = a in ("y", "yes", "是", "1")
        except (EOFError, KeyboardInterrupt):
            wipe = False
    if wipe:
        import shutil
        import time as _time
        for name in (".secrets", "out"):
            d = ROOT / name
            if not d.exists():
                continue
            # ⚠ 可能还有对账进程正拿着里面的文件（锁、浏览器 profile）——
            #   实测会 ENOTEMPTY。等两秒重试一次，还不行就报告出来别硬撑。
            for attempt in (1, 2):
                try:
                    shutil.rmtree(d)
                    print(f"        已删除 {name}\\")
                    break
                except OSError as e:
                    if attempt == 2:
                        problems.append(
                            f"删 {name}\\ 失败：{e}（还有程序在用？先确认没有对账在跑）")
                    else:
                        _time.sleep(2)
    else:
        print("        保留 .secrets\\ 和 out\\（不删）")

    print()
    print("=" * 46)
    if problems:
        print("  卸载完成，但有几件事没做成：")
        for x in problems:
            print(f"    · {x}")
        print("    （定时任务删不掉的话，右键 uninstall.bat → 以管理员身份运行）")
    else:
        print("  ✅ 卸载完成 —— 这台电脑上已经没有我们的服务、定时任务和开机自启了。")
    print()
    print("  最后一步（要彻底清干净的话）：手动删掉这个目录")
    print(f"    {ROOT}")
    print()
    return 0


def ask_autostart() -> None:
    """装完依赖后问一句要不要开机自启。

    为什么问而不是直接装：安装脚本静默改系统开机项会让人意外
    （"我就装个依赖，怎么开机多了个东西"）。但完全不做又会漏 ——
    门店电脑上最容易忘的就是这一步。所以问一句，回车即开。
    """
    try:
        from src import autostart            # 纯标准库，这时候能 import
    except Exception as e:                   # noqa: BLE001
        print(f"（读不到开机自启模块，跳过：{e}）")
        return

    try:
        st = autostart.status(ROOT)
    except Exception as e:                   # noqa: BLE001
        print(f"（查不了开机自启状态，跳过：{e}）")
        return

    already = bool(st.get("installed"))
    elevated = bool(st.get("elevated"))

    if already and (elevated or os.name != "nt"):
        print(f"开机自动启动：已经设过了（{st.get('platform')}）")
        return
    if already and not elevated:
        # 之前注册的是注册表项（普通权限）—— 顺手升级成"以管理员身份启动"
        print()
        print("开机自动启动：已设过，但是**普通权限**的。")
        print("  要以管理员身份启动，需要提权重新注册一次。")

    if not (sys.stdin and sys.stdin.isatty()):
        # 非交互（比如被别的脚本调起来）就别卡在这儿
        print("开机自动启动：没设。之后可以在控制台「设置 → 后台服务」里开，")
        print("                或跑 python -m src.cli autostart --on")
        return

    print()
    print("-" * 46)
    print("  要把控制台设成开机自动启动吗？")
    print("-" * 46)
    print("  开了之后，每次登录这台电脑服务会自动在后台跑起来（不弹窗），")
    print("  店员直接开浏览器就能看报告，不用记得去双击 start.bat。")
    print()
    print("  会注册成「**以管理员身份**运行」—— 这样界面上注册计划任务、")
    print("  写程序目录都不再报权限错。用的是登录触发的计划任务，")
    print("  **只在注册这一步弹一次 UAC，以后每次开机都不会再弹**。")
    print()
    try:
        ans = input("  设为开机自动启动？[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if ans not in ("", "y", "yes", "是", "1"):
        print("  跳过。之后可以在控制台「设置 → 后台服务」里开。")
        return

    # 提权这一步单独做：把 autostart 交给一个管理员进程去注册
    if os.name == "nt" and not is_admin():
        print()
        print("  注册「以管理员身份启动」需要管理员权限，正在请求提权…")
        print("  （马上会弹一个 UAC 窗口，点「是」；会另开一个窗口显示结果）")
        if relaunch_as_admin("autostart"):
            return                            # 交给提权后的那个窗口，本进程收工
        # 提权没成 → 往下走，用普通权限注册（功能可用，只是不提权）

    code = do_autostart()
    if code != 0:
        print("    可以之后到控制台「设置 → 后台服务」里再试。")


def main() -> int:
    argv = sys.argv[1:]
    # 提权后的新窗口是自己弹出来的，跑完就没了 —— 加 --pause 让用户看得到结果
    pause = "--pause" in argv
    argv = [a for a in argv if a != "--pause"]
    cmd = (argv[0] if argv else "install").strip()

    try:
        # 版本太旧就什么命令都别跑 —— 与其后面报一堆看不懂的错，不如现在说清楚
        vcode = check_python()
        if vcode:
            return vcode

        if cmd in ("install", "install-deps"):
            code = do_install()
            if code == 0:
                ask_autostart()
            return code

        if cmd == "autostart":
            # 纯标准库，依赖没装也能跑（提权后的窗口就是走这条路）
            return do_autostart()

        if cmd == "uninstall":
            return do_uninstall(yes="--yes" in sys.argv, purge="--purge" in sys.argv)

        left = missing()
        if left:
            print("依赖还没装： " + ", ".join(left))
            print()
            print("请先双击 install.bat（它会装 requests / pyyaml / openpyxl）。")
            print("如果已经装过还报这个，多半是双击 bat 用的 Python 跟装依赖的不是同一个。")
            return 2

        from src.cli import main as cli_main          # 依赖齐了才 import
        return cli_main([cmd, *argv[1:]])
    finally:
        if pause:
            try:
                input("\n按回车关闭这个窗口…")
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    sys.exit(main())
