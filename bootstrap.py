#!/usr/bin/env python3
"""统一的启动入口 —— **只用标准库**。

为什么需要它：`src/cli.py` 顶部就 import requests，**没装依赖时根本起不来**。
而 `install.bat` 的职责恰恰是"装依赖" —— 这就成了先有鸡还是先有蛋：
在全新的门店电脑上双击 install.bat，只会甩一个英文 ImportError 出来。

所以所有 .bat 都走这里：
    python bootstrap.py install          装依赖（然后问一句要不要开机自启）
    python bootstrap.py autostart        只注册开机自启（默认**普通权限**）
                       加 --elevated 才注册"以管理员身份启动"（要管理员，且会弄坏抓会话）
    python bootstrap.py uninstall        卸载：撤掉服务 / 定时任务 / 开机自启
                         加 --yes 跳过确认；加 --purge 连报告和凭据一起删
    python bootstrap.py service-start    后台启动（start.bat）
    python bootstrap.py stop             停止（stop.bat）
    python bootstrap.py selftest         逐项自检（selftest.bat）

**全程不需要管理员权限，一次 UAC 都不用弹。**
（2026-09-16 改的：以前注册开机自启会弹一次 UAC 去建"以管理员身份启动"的计划任务，
而管理员身份会让自动抓华为会话彻底不可用，见 `src/autostart.py` 顶部。）

**`install` 和 `autostart` 只用标准库**，依赖没装也能跑。
"按需提权"的零件留在 `relaunch_as_admin`，目前没有调用方。

中文提示也在这里打印 —— Python 在 Windows 上走 WriteConsoleW，
跟控制台代码页无关，不会像批处理那样显示成方块。
"""

# ⚠ 这一行**不是**可有可无的装饰，它修的是一个真把门店卡住的 bug。
#   下面 `def missing() -> list[str]` 里的 `list[str]` 是 PEP 585（**3.9** 才有）；
#   没有这个 future import 时，注解在 `def` 那一刻就求值 → 3.8 上 **import 本文件
#   直接 TypeError**，而 check_python() 那句"版本太旧，请装 X"根本来不及打印。
#   也就是说：**这段友好提示在任何能触发它的解释器上都到不了** ——
#   3.8 及以下全都会先炸在这一行，用户看到的是一屏英文 traceback。
#   （测试抓不到：测试跑在 3.9+ 上，那时 list[str] 求值正常。）
#   有了它，注解变成字符串、不求值，3.8 也能正常跑完并打印该打印的话。
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

REQUIRED = ("requests", "yaml", "openpyxl")       # 模块名（yaml 是 PyYAML）
STDLIB_ONLY = ("install", "install-deps", "autostart", "uninstall",
               "schedule-install", "schedule-remove")   # 不碰第三方包

# 开机自启那条计划任务的名字。**引一次就够** —— `print_leftover_task_help()` 要
# 在管理员命令行里拼出删除命令，硬写两处迟早对不上（改了一处、另一处就成了
# "删一个不存在的任务"）。名字的真源在 `src/autostart.py`，但那要 import 依赖，
# 而这个提示可能在任何时候打印，所以在这儿留一个常量、并由测试钉住两者一致。
AUTOSTART_TASK_NAME = "CBG报量对账-开机自启"

# 底线是 **3.8**，不是"随便多旧都行"。
#
# 为什么是 3.8 而不是更高：**Windows 7 只能装到 3.8.10** ——
# 3.9 起 CPython 用了 `api-ms-win-core-path-l1-1.0.dll`，Win7 上根本没有，
# 安装包直接起不来（bugs.python.org/issue40740）。门店还有 Win7 老电脑，
# 所以 3.8.10 必须能用。
#
# 为什么不再往下放（3.7/3.6）：没有任何一台机器需要。
# 3.8 是**最后一个支持 Win7 的版本**，往下兼容只是凭空多一份要维护的目标。
MIN_PYTHON = (3, 8)

# 3.8 已经于 2024-10-07 EOL（官方只发安全补丁到这天）。**不拦**，但要提一句 ——
# Win7 上没得选，用户知道就行；有得选的机器应该往上装。
EOL_MINORS = (8,)

WIN7_DOWNLOAD = "https://www.python.org/downloads/release/python-3810/"
LATEST_DOWNLOAD = "https://www.python.org/downloads/"


def is_win7() -> bool:
    """是不是 Windows 7（版本号 6.1）。

    只在"该装哪个 Python"这件事上有用：Win7 上**只有 3.8.10 一个选择**，
    而别处都该装新的。给错下载地址等于让门店白跑一趟。
    """
    if os.name != "nt":
        return False
    try:
        v = sys.getwindowsversion()
        return (v.major, v.minor) == (6, 1)
    except Exception:                        # noqa: BLE001
        return False


def python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def needs_python_help() -> str:
    """该装哪个 Python —— **按系统给**，别一律叫人家装最新版。

    ⚠ 这里写死版本号踩过坑：原来一律叫人去装 3.14，
    而 **Win7 上 3.9 以上根本装不上**（缺 api-ms-win-core-path-l1-1.0.dll），
    门店照着做会卡在安装包报错上，然后就没有下文了。
    所以要分开说：Win7 只有 3.8.10 一个选择，别处才该装新的。
    """
    if is_win7():
        return (f"  Windows 7 只能装 **Python 3.8.10**（3.9 以上不支持 Win7）：\n"
                f"    {WIN7_DOWNLOAD}\n"
                f"  下载页里找 **Windows installer (64-bit)** 那个。")
    return (f"  装 3.9 以上都行，最新版在这里：\n"
            f"    {LATEST_DOWNLOAD}")


def check_python() -> int:
    """版本够不够。**先说清楚现在是多少** —— 门店电脑上版本不对时，
    "报了个错"和"你现在是 3.7.9，请装 3.8.10"完全是两回事。
    """
    v = sys.version_info[:2]
    if v >= MIN_PYTHON:
        return 0
    need = ".".join(str(x) for x in MIN_PYTHON)
    print(f"Python 版本太旧：现在 {python_version()}，需要 {need} 或更高。")
    print()
    print(needs_python_help())
    print()
    print("安装时**务必勾选 Add python.exe to PATH**，装完重开一个窗口再跑。")
    return 2


def warn_if_eol() -> None:
    """3.8 能跑，但官方已经不更新它了 —— 提一句，不拦。

    为什么是"提一句"而不是"拦住让去升级"：Win7 上**没得升**。
    拦住就等于把这台机器判死刑，而它其实跑得好好的。
    """
    if sys.version_info[:2] in [(3, m) for m in EOL_MINORS]:
        print(f"  提示：Python {python_version()} 官方已停止安全更新"
              "（最后一版是 3.8.10）。Win7 上没得选，能用就先用着；")
        print("        别的系统建议装 3.9 以上。")


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
    warn_if_eol()
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
    record_runtime()
    print("依赖装好了。接下来双击 start.bat 启动控制台。")
    return 0


def record_runtime() -> None:
    """把**这次用的解释器**记下来（.secrets/python.txt）。

    为什么必须记：这台电脑上可能不止一个 Python（Win7 老机器装 3.8.10 是唯一
    选择，新机器装 3.14，有的机器还有 Anaconda）。依赖是装进**这一个**的，
    而 .bat 每次是重新去 PATH 上找的 —— 找到另一个就报
    `No module named 'requests'`，看着像当初没装成功。
    记下来之后，几个 .bat 和计划任务都优先用它。

    失败**绝不让安装失败** —— 记不下来最多退回老行为（按 PATH 探测）。
    """
    try:
        from src import runtime
    except Exception as e:                       # noqa: BLE001
        print(f"  （解释器记录没写成，不影响使用：{e}）")
        return
    p = runtime.record(ROOT)
    if p:
        print(f"  已记住这台电脑用的 Python：{python_version()}（{sys.executable}）")
    else:
        print("  （解释器记录没写成，不影响使用）")


def is_admin() -> bool:
    """当前进程有没有管理员权限。

    非 Windows 直接返回 True —— 别的平台没这个概念，别拿它拦事情。

    ⚠ **这个项目正常跑不需要管理员**（2026-09-16 起）：装依赖、跑对账、起控制台、
    抓会话、注册开机自启，全都不用。唯一"想要"管理员的是
    `autostart --elevated` 那条可选的路（以管理员身份启动），而它会让
    「自动抓华为会话」失败 —— 所以默认不走，也不推荐。
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

    ⚠ **目前没有任何地方在用它** —— 留着是因为它是"**按需提权**"的现成零件：
    将来哪一步真需要管理员（比如某台机器上普通权限注册不了计划任务），
    就用它单独弹一次 UAC 把那一步提权重跑，跑完退出，不影响别的。

    为什么坚决不做成"装机就提权"：如果这台电脑是"标准用户 + 管理员账号密码"的
    配置，UAC 提权后跑的是一个**另一个账号**的进程 —— 它装的 pip 包、
    写的数据都在那个账号下，当前用户这边根本看不到。
    现象是"明明装完了还说缺依赖"，极难排查。

    返回 True = 已经交给提权后的窗口了，**当前进程该收工了**；
    返回 False = 没提成（用户点了"否" / 账号不是管理员）。
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


def do_autostart(elevated: bool = False, result_file: str = "") -> int:
    """注册开机自启。**默认普通权限**（注册表 Run 项，不需要管理员）。

    `elevated=True` 才去注册"以管理员身份启动"的计划任务 —— 那条路要求
    当前进程已经是管理员，而且**会让「自动抓华为会话」失败**，
    所以它只能靠 `python bootstrap.py autostart --elevated` 显式要求。

    `result_file`：写给**提权后的那个进程**回传结果用的（见 `src/elevate.py`）。
    """
    from src import autostart
    try:
        res = autostart.install(ROOT, elevated=True if elevated else None)
    except Exception as e:                   # noqa: BLE001
        res = {"ok": False, "message": f"注册失败：{e}"}
    describe_autostart(res)
    if res.get("warning"):
        print(f"  {res['warning']}")
    if res.get("task_leftover"):
        print_leftover_task_help()
    _write_result(result_file, res)
    return 0 if res.get("ok") else 2


def print_leftover_task_help() -> None:
    """⚠ 旧提权任务没删掉时，把话说到**不可能被忽略**。

    为什么单独拎出来大声说：这条任务留着的话，**每次登录它都会以管理员把服务
    拉起来**，于是：
      * 「自动抓华为会话」永远坏着（浏览器拒绝以管理员运行）；
      * 界面上却写着"普通权限"，报错却说是"管理员"—— 自相矛盾，最难查；
      * 连"每天定时对账"的任务也建不了（旧的是管理员建的，普通权限覆盖不了）。
    实测就这么坑了用户一轮：三条症状全指向它，但提示只混在成功消息里一句带过。
    """
    print()
    print("  " + "!" * 56)
    print("  ⚠️  还有一件事必须做，不然上面这些等于白改：")
    print("  " + "!" * 56)
    print("  有一条**旧版本留下的提权任务**没删掉，而删它需要管理员权限。")
    print("  它留着的话，每次开机还是会以管理员身份把服务拉起来 ——")
    print("  「自动抓华为会话」会一直失败。")
    print()
    print("  做法：右键「命令提示符」→「以管理员身份运行」，粘这一条回车：")
    print()
    print(f'    schtasks /delete /tn "{AUTOSTART_TASK_NAME}" /f')
    print()
    print("  然后 stop.bat 停掉服务、用普通权限双击 start.bat。")
    print()


def do_schedule_install(argv: list, result_file: str = "") -> int:
    """注册「每天定时对账」的计划任务。

    ⚠ 单独做成一个子命令，是为了**能提权重跑**：这条任务以前可能是管理员身份
    的服务建的，普通权限覆盖不了（`schtasks ... /f` 会拒绝访问）。
    见 `src/elevate.py` 和 `src/web.py` 的 `/api/schedule` ——
    失败时界面会问一句，用这个子命令弹一次 UAC 重试。
    """
    def opt(name, default=""):
        return argv[argv.index(name) + 1] if name in argv and \
            argv.index(name) + 1 < len(argv) else default

    time_str = opt("--time", "21:00")
    days_ago = opt("--days-ago", "1")
    config = opt("--config", "config/store-SCN231409.yaml")
    name = opt("--name") or None
    from src import schedule
    try:
        res = schedule.install(ROOT, time_str, int(days_ago), config, name=name)
    except Exception as e:                   # noqa: BLE001
        res = {"ok": False, "message": f"注册失败：{e}"}
    print(("  ✓ " if res.get("ok") else "  ✗ ") + str(res.get("message", "")))
    if not res.get("ok") and res.get("manual"):
        print()
        print("  也可以拿管理员权限手动跑这一条：")
        print(f"    {res['manual']}")
    _write_result(result_file, res)
    return 0 if res.get("ok") else 2


def do_schedule_remove(argv: list, result_file: str = "") -> int:
    """删掉一个定时任务 —— **专门给"提权"用的**。

    ## 为什么要单独做这一件事

    任务如果是以**管理员身份建**的，普通权限**连读都读不到**（更别说删）：
    所有者是 `Administrators`，而服务跑在过滤令牌下。
    用户看到的现象就是「定时执行」里**只看得到一个名字、时间和命令全是空的**。

    删也是同理 —— 所以要有一条能提权跑的删除路径，就是本命令。

    ⚠ 但**"提权只删、建还是普通权限建"那条路走不通**（曾经的设计，2026-09-16 被实测推翻）：
    门店那台机器上普通权限 `schtasks /create` 直接报 `错误: 拒绝访问。`
    —— 旧任务被管理员建过时 `/f` 覆盖不了；账户被 UAC 过滤时更是压根建不了。
    所以注册那条路也得能提权跑（`schedule-install`），
    代价是任务归 `Administrators`、以后详情读不到
    → **由我们自己记的注册参数兜底**（`.secrets/schedule.json`）。
    """
    name = ""
    if "--name" in argv and argv.index("--name") + 1 < len(argv):
        name = argv[argv.index("--name") + 1]
    from src import schedule
    try:
        # 传 ROOT：提权子进程删完也要抹掉注册记录（同一个 .secrets/schedule.json），
        # 不然界面上会"任务已经没了、时间和命令还显示着"
        res = schedule.remove(name or None, ROOT)
    except Exception as e:                   # noqa: BLE001
        res = {"ok": False, "message": f"删除失败：{e}"}
    print(("  ✓ " if res.get("ok") else "  ✗ ") + str(res.get("message", "")))
    _write_result(result_file, res)
    return 0 if res.get("ok") else 2


def _write_result(path: str, obj: dict) -> None:
    """把结果写成 JSON —— 给**提权后的那个进程**回传用。

    提权是 `ShellExecuteW` 另起一个进程，父子之间没有管道，
    子进程的 stdout 我们拿不到（服务本身还是 pythonw 起的，连控制台都没有）。
    所以只能落到文件里让父进程读。

    失败**不抛异常**：回传失败最多是"界面上看不到结果"，
    不该让已经做成的操作反而报错。
    """
    if not path:
        return
    try:
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception:                        # noqa: BLE001
        pass


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
        res = schedule.remove_all(ROOT)
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

    ⚠ **这一步不再请求任何权限、不弹 UAC**（2026-09-16 改）。
    注册的是注册表 `HKCU\\...\\Run` —— 当前用户自己的分支，任何用户都能写。
    以前这里会用 UAC 提权去注册"以管理员身份启动"的计划任务，
    而管理员身份会让**「自动抓华为会话」彻底不可用**（浏览器拒绝以管理员运行）。
    那次 UAC 除了一件事都换不来，那件事还正好是坏事。
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
    # ⚠ 这里以前是"已经是普通权限的 → 顺手升级成管理员"。现在反过来：
    #   已经是管理员的要**劝回普通权限**，因为管理员会让抓会话不可用。
    is_elevated_mode = bool(st.get("elevated"))

    if already and not is_elevated_mode:
        print(f"开机自动启动：已经设过了（{st.get('platform')}，普通权限）")
        return

    if already and is_elevated_mode:
        print()
        print("开机自动启动：已经设过了，但注册的是「**以管理员身份**运行」。")
        print("  这个模式会让**「自动抓华为会话」不可用** —— Edge / Chrome 拒绝")
        print("  以管理员运行，会把命令行交棒出去然后自己退出，我们等不到调试端口。")
        print("  建议改回普通权限（对账本身不受影响，普通权限完全够用）。")

    if not (sys.stdin and sys.stdin.isatty()):
        # 非交互（比如被别的脚本调起来）就别卡在这儿
        print("开机自动启动：没设。之后可以在控制台「设置 → 后台服务」里开，")
        print("                或跑 python -m src.cli autostart --on")
        return

    print()
    print("-" * 46)
    if already:
        print("  要把开机自启改成**普通权限**吗？")
    else:
        print("  要把控制台设成开机自动启动吗？")
    print("-" * 46)
    print("  开了之后，每次登录这台电脑服务会自动在后台跑起来（不弹窗），")
    print("  店员直接开浏览器就能看报告，不用记得去双击 start.bat。")
    print()
    print("  注册的是**普通权限**的启动项（写当前用户自己的注册表），")
    print("  **不需要管理员、不会弹 UAC**。")
    print()
    try:
        ans = input("  设为开机自动启动？[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if ans not in ("", "y", "yes", "是", "1"):
        print("  跳过。之后可以在控制台「设置 → 后台服务」里开。")
        return

    code = do_autostart()
    if code != 0:
        print("    可以之后到控制台「设置 → 后台服务」里再试。")


def _runtime_pinned() -> bool:
    """已经记过解释器了吗。

    ⚠ 返回 True 表示"别去记" —— 所以 import/读取出任何岔子都返回 True：
    记不上是小事，为此在启动路径上抛异常才是大事。
    """
    try:
        from src import runtime
        return bool(runtime.pinned(ROOT))
    except Exception:                        # noqa: BLE001
        return True


# —— 运行时要用的目录，**包里一个都不带**，由 `ensure_layout()` 按需创建 ——
#
# ⚠ 为什么不放进包：这三个目录是**这台电脑自己的东西**（凭据、报告、门店配置）。
#   以前包里带着它们（哪怕只是空模板），后果是**手工把新包拷到已有安装上时
#   会把门店的设置冲掉** —— 每次拷贝都得记着"跳过这三个目录"，迟早出错。
#   现在包里一个都不带，**整个目录直接覆盖就是安全的**。
#
# ⚠ 而且**只在缺失时创建**：`erp.env` 里是云商账号密码，覆盖它等于把门店的
#   账号抹了。所以每个文件都是"没有才写"，一个字节都不许盖。
DEFAULT_CONFIG = "config/store-SCN231409.yaml"
CONFIG_TEMPLATE = "src/store-config.default.yaml"

_SECRETS_README = """这个目录放凭据，不要外传。

  erp.env                云商账号密码
  huawei.env             华为账号密码（自动登录用，界面上填）
  mail.env               邮箱 SMTP 授权码（界面上填）
  wecom.env              企业微信群机器人 webhook（界面上填）
  cbg-<门店码>.json      华为会话（在本机「会话」页登录后自动生成）
  session-check.json     上次会话自检的时间/结果（界面「当前会话」显示的那个）
  browser-profile/       浏览器 profile（登录态，自动续期靠它）
  python.txt             安装时用的那个 Python（多 Python 机器不装错）
"""

_ERP_ENV_TEMPLATE = """# 盛联 ERP（云商）凭据 —— **本机专用，不要外传，也不要提交到 git**
#
# 怎么填（二选一）：
#   1. 打开控制台「设置 → 云商账号」，填账号 / 密码 / 公司代码，点「测试登录」
#      —— 这是推荐做法：会当场验证账号对不对，通过了才写进这个文件
#   2. 直接编辑本文件（键名不要改）
#
# ERP_TOKEN 不用手填，登录成功后自动写进来。
#
# 优先级：环境变量 > 这个文件 > ~/.dsh/secrets/erp.env

ERP_USERNAME=
ERP_PASSWORD=
ERP_COMPANY_CODE=

# 登录成功后自动填，不用手写
ERP_TOKEN=
"""

_OUT_README = """对账产出的差异清单、运行日志都写在这个目录。

  差异_<日期>_<门店码>.xlsx    差异清单（每次跑都生成，没差异也生成）
  差异_<日期>_<门店码>.json    给控制台看的摘要
  run.log                      计划任务跑的日志（如果配了）
  autostart.log                开机自启的日志（如果有报错）
"""


def ensure_layout() -> None:
    """把运行时目录补齐。**只补缺的，绝不覆盖已有的。**

    装出来的包里**没有** `.secrets/`、`out/`、`config/store-*.yaml` 这三个 ——
    它们都是"这台电脑自己的东西"，放在包里会让手工拷贝/装包把门店设置冲掉。
    所以由这里按需生成（模板在 `src/store-config.default.yaml`）。

    任何一步失败都**只打印一行、绝不抛异常**：这是启动路径上的东西，
    补不出来最多是"门店得自己去界面上填一次"，不该让程序起不来。
    """
    for d in (ROOT / ".secrets", ROOT / "out"):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"  （{d.name}\\ 建不出来，不影响使用：{e}）")
    for rel, body in ((".secrets/README.txt", _SECRETS_README),
                      (".secrets/erp.env", _ERP_ENV_TEMPLATE),
                      ("out/README.txt", _OUT_README)):
        _write_if_missing(ROOT / rel, body)
    _ensure_default_config()


def _write_if_missing(path: Path, body: str) -> None:
    """只在文件不存在时写。

    ⚠ 判断要用 `exists()`（**目录也算存在**）—— 万一那里是个同名目录，
    写进去会抛 `IsADirectoryError`；`exists()` 为真就跳过，正好躲开。
    """
    try:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    except OSError as e:
        print(f"  （{path.name} 没建成，不影响使用：{e}）")


def _ensure_default_config() -> None:
    """没有任何门店配置时，从模板生成一份。

    ⚠ 判断的是"**有没有 `config/store-*.yaml`**"，不是"默认那个文件在不在"：
    门店可能把文件改名成自己店的编码（那是允许的，界面上能改），
    这时候再去补一个默认文件，就会多出一份没用的配置，还容易看错。

    ⚠ 按字典序排一下再判断只是为了让结果稳定（`glob` 的顺序不保证），
    取不取第一个都不影响"有没有"这个结论。
    """
    try:
        cfg_dir = ROOT / "config"
        if cfg_dir.is_dir() and any(cfg_dir.glob("store-*.yaml")):
            return
        tpl = ROOT / CONFIG_TEMPLATE
        if not tpl.exists():
            print(f"  （没有 {CONFIG_TEMPLATE}，门店配置要自己建）")
            return
        _write_if_missing(ROOT / DEFAULT_CONFIG, tpl.read_text(encoding="utf-8"))
        print(f"  已生成门店配置：{DEFAULT_CONFIG}")
        print("  ⚠ 里面三行（store_code / marker / erp_store_name）是**空的**，")
        print("     到控制台「设置 → 门店」填成这家店的 —— 填错会对到别家账上去。")
    except OSError as e:
        print(f"  （门店配置没生成，不影响使用：{e}）")


def main() -> int:
    argv = sys.argv[1:]
    # 提权后的新窗口是自己弹出来的，跑完就没了 —— 加 --pause 让用户看得到结果
    pause = "--pause" in argv
    argv = [a for a in argv if a != "--pause"]
    # 提权子进程把结果写这儿，父进程轮询着读（见 src/elevate.py）
    result_file = ""
    if "--result-file" in argv:
        i = argv.index("--result-file")
        result_file = argv[i + 1] if i + 1 < len(argv) else ""
        del argv[i:i + 2]
    cmd = (argv[0] if argv else "install").strip()

    try:
        # 版本太旧就什么命令都别跑 —— 与其后面报一堆看不懂的错，不如现在说清楚
        vcode = check_python()
        if vcode:
            return vcode

        # ⚠ 卸载**不能**补目录 —— 用户正要删东西，我们却又建回来，很荒唐。
        if cmd != "uninstall":
            ensure_layout()

        if cmd in ("install", "install-deps"):
            code = do_install()
            if code == 0:
                ask_autostart()
            return code

        if cmd == "autostart":
            # 纯标准库，依赖没装也能跑。
            # ⚠ 默认**普通权限**；`--elevated` 才去注册管理员模式（那条路要管理员
            #   权限，而且会让自动抓会话失败 —— 所以必须显式要，不能猜）。
            return do_autostart(elevated="--elevated" in argv,
                                result_file=result_file)

        if cmd == "schedule-remove":
            # ⚠ 只做"删"这一件事 —— 提权删掉管理员建的旧任务，
            #   然后由调用方**以普通权限**重建（见 web.py 的 /api/elevate）。
            return do_schedule_remove(argv, result_file=result_file)

        if cmd == "schedule-install":
            # ⚠ 这个子命令存在的唯一理由是**能提权重跑一次**：那条定时任务
            #   可能是管理员身份建的，普通权限覆盖不了。见 do_schedule_install。
            return do_schedule_install(argv, result_file=result_file)

        if cmd == "uninstall":
            return do_uninstall(yes="--yes" in sys.argv, purge="--purge" in sys.argv)

        left = missing()
        if left:
            print("依赖还没装： " + ", ".join(left))
            print()
            print("请先双击 install.bat（它会装 requests / pyyaml / openpyxl）。")
            print("如果已经装过还报这个，多半是双击 bat 用的 Python 跟装依赖的不是同一个 ——")
            print("双击 install.bat 重装一次，它会把这次用的 Python 记下来，以后就走它。")
            return 2

        # 走到这儿说明依赖齐了 —— 顺手把解释器记一次。
        # 覆盖两种 install 没记上的情况：手工拷贝部署、老版本升上来的。
        # 只补空、不覆盖已有的记录（见 runtime.record 的调用方语义）。
        if not _runtime_pinned():
            record_runtime()

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
