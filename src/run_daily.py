"""日常流程 —— **一条定时任务跑完三步**（用户 2026-09-16 定的）。

    1. 抓四池数据：华为**当月**订单 + 云商销售/在库 → 补进 out/cbg-<年>.db（**取并集**）
    2. POS 合规：库 → 月度分数 → 落 out/pos-<年>.json
    3. 四池对账：A/B/C/D 四个池子做差集 → AD / BC → 推邮件 / 企业微信

⚠ 原来的第 2 步「报量排查」2026-09-17 整步拿掉了，并进第 3 步的四池对账。

## ⚠ 第 1 步失败 ⇒ **跳过第 2、3 步，直接报错**（用户定的）

为什么不"接着跑、只是标个警告"：

对账的华为侧和云商侧都从库里读。库要是没抓到今天的单，差集会把当天**所有**销售
都算成「玲珑无但云商有」—— 一份**完全错误的清单**，而且**看着很合理**，
门店会照着它去补报一批假的。

**宁可今天没有报告，也不要发一份假的。**（第 2 步失败不跳第 3 步 ——
POS 只读库，跟云商没关系，照算。）

## 关于锁

⚠ 保护的是**云商**不是华为（`check.lock` 的报错原文是「云商不能并行登录」）。
第 1 步和第 2 步都不需要锁；抓云商取数时自己会抢。
三步在同一个进程里串行，所以不会自己撞自己。
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time

from . import cli

#: 一次日常流程由**三件事**组成。这是全项目唯一的步骤定义。
STEPS = ("dump", "pos", "pools")
STEP_LABELS = {
    "dump": "抓四池数据",
    "pos": "POS 合规",
    "pools": "四池对账",
}
#: 步骤 → `daily` 的跳过开关。⚠ 注意 `reconcile` 对应的开关叫 `--skip-check`
#: （命令行的子命令历史上叫 `check`，改名成本太高，这里映射一下就行）。
STEP_FLAGS = {
    "dump": "--skip-dump",
    "pos": "--skip-pos",
    "pools": "--skip-pools",
}

#: 界面「运行」页的按钮 = 几个**预设**。
#: ⚠ 2026-09-17 起界面上只剩「整个项目」一个（用户定：目标日/高级/分开跑都不要了），
#:   但 `dump` / `pos` 两个预设**留着** —— `/api/run` 和 `run_one` 还认，
#:   老 `.secrets/schedule.json` 里 `what: "pos"` 那种记录也靠它读回来。
BUTTON_STEPS = {
    "all": ("dump", "pos", "pools"),
    "dump": ("dump",),
    "pos": ("pos",),
}
BUTTON_LABELS = {
    "all": "整个项目",
    "dump": "抓华为数据",
    "pos": "POS 合规",
    "pools": "四池对账",
}
DEFAULT_WHAT = "all"

#: 定时任务「自动化跑什么」的**默认值**：三件都做（用户 2026-09-16 定的）。
#: ⚠ 抓数据默认**必须**在里面 —— 不抓的话库永远是旧的，
#: POS 和四池对账都只是拿旧数据在算，而日志只会说"跑完了"。
AUTOMATION_DEFAULT_STEPS = ("dump", "pos", "pools")

#: **永远会跑、界面上取消不掉**的步骤（用户 2026-09-17 定：
#: 「把抓数据这个复选框去掉吧，默认执行这个。不可选」）。
#:
#: 为什么不给选：不抓数据的话本地库永远是旧的，POS 和四池对账
#: 都只是**拿旧数据在算** —— 而界面上只会显示"跑完了"。这是最难发现的一类错。
#:
#: ⚠ 它**不进 `schedule.AUTOMATION_CHOICES`**（界面不给复选框），
#: 由 `with_always()` 无条件补上 —— 连老记录 / 老 run.bat 里被取消过的也补回来。
ALWAYS_STEPS = ("dump",)


def _norm_steps(steps):
    """校验 + 按 `STEPS` 的固定顺序去重。

    **一件都不选是合法的，返回空元组** —— 不允许空的是 `_check_steps` 那一层。
    """
    got = tuple(steps or ())
    bad = [x for x in got if x not in STEP_FLAGS]
    if bad:
        raise ValueError("不认识的步骤：%s（只认 %s）"
                         % ("、".join(map(str, bad)), "、".join(STEPS)))
    return tuple(s for s in STEPS if s in set(got))


def _check_steps(steps):
    """校验 + 排序，**并且不许为空**（老规矩：一件都不选要抛）。

    ⚠ 「一件都不选」在这一层是错的，但在**定时任务勾选**那层是合法的
    （见 `with_always`）：那里的空 = 「只做必做的那几件」。
    """
    got = _norm_steps(steps)
    if not got:
        raise ValueError("至少要选一件事（%s）" % "、".join(STEP_LABELS[s] for s in STEPS))
    return got


def with_always(steps) -> tuple:
    """补上**永远会跑**的步骤（`ALWAYS_STEPS`），按 `STEPS` 顺序排好。

    ⚠ **允许空输入** —— 「可选项一件都不勾」是合法的，含义是
    「每天只抓数据，不算也不推」（用户 2026-09-17 定）。
    这不是"静默当成都跑"：结果是 `空 + ALWAYS`，**没有扩大**用户的选择。
    """
    got = set(_norm_steps(steps)) | set(ALWAYS_STEPS)
    return tuple(s for s in STEPS if s in got)


def flags_for_steps(steps) -> list:
    """选了哪几件事 → 该往 `daily` 后面加什么跳过开关。

    ⚠ 认不出来 / 一件没选 **抛错，不回落**。回落的后果是
    "勾了只算 POS，结果数据也抓了、推送也发了"，而日志里一个字都不会说。
    """
    got = set(_check_steps(steps))
    return [STEP_FLAGS[s] for s in STEPS if s not in got]


def steps_label(steps) -> str:
    """「跑什么」那一列的文字 —— **列出执行项目**，不是给一句概括。

    写「整个项目」门店看不懂那指什么；写成
    「抓华为数据 + 报量排查 + POS 合规」一眼就知道每天到底干了哪几件事。
    """
    return " + ".join(STEP_LABELS[s] for s in _check_steps(steps))


def flags_for(what: str) -> list:
    """界面「运行」页的按钮用的（现在只剩 `all` 一个，其余留给老调用方）。"""
    if what not in BUTTON_STEPS:
        raise ValueError("不认识的 what：%r（认得的是 %s）"
                         % (what, "、".join(sorted(BUTTON_STEPS))))
    return flags_for_steps(BUTTON_STEPS[what])


def run_one(what: str) -> int:
    """跑**一件**事 —— 命令行/脚本想按预设跑时走这条。

    ⚠ 界面上**已经没有**单独执行的按钮了（2026-09-17），这条留着给
    `daily` 的几个预设当统一入口，别的地方还在用。
    """
    return main(flags_for(what))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="日常流程：抓华为当月 → 对账 → 算 POS")
    ap.add_argument("-c", "--config", default=str(cli.DEFAULT_CONFIG))
    # ⚠ 下面这四条**现在都不生效了**（2026-09-17 报量排查整步拿掉，
    #   界面上的「目标日」「高级」也跟着删了）。**保留是为了不弄挂
    #   老 run.bat / 计划任务** —— `schedule.write_runner_script()` 会往
    #   run.bat 里写死 `--days-ago N`，门店那边早就跑着了，
    #   删掉会以 `unrecognized arguments` 收场。跟 `--skip-check` 一样的处置。
    ap.add_argument("--date", default="", help="（已废弃）原来只影响报量排查")
    ap.add_argument("--days-ago", type=int, default=1, help="（已废弃）原来只影响报量排查")
    ap.add_argument("--lookback", type=int, help="（已废弃）")
    ap.add_argument("--lookahead", type=int, help="（已废弃）")
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-refresh", action="store_true",
                    help="会话失效时别开浏览器静默续期（第 1 步会直接失败）")
    # ⚠ 这三个开关是**产品功能**，不只是调试用：界面上「整个项目」按钮
    #   （= 一个都不跳）和设置里的「自动化」勾选，都是靠它们组合出来的 ——
    #   一个入口、几条路，行为才不会分叉。
    #   （命令行 `daily --skip-dump --skip-pos` 也是同一条路，照旧能用。）
    ap.add_argument("--skip-dump", action="store_true", help="跳过第 1 步：不抓华为数据")
    # ⚠ `--skip-check` **保留但已经没用了**（2026-09-17 报量排查整步拿掉）。
    #   留着是因为老门店的 run.bat / 计划任务里可能还带着它 ——
    #   删掉会以 `unrecognized arguments` 收场，那种失败最难查。
    #   **但必须提示"它没用了"**，别让它变成 AGENTS.md 说的那种"死参数"。
    ap.add_argument("--skip-check", action="store_true",
                    help="（已废弃）报量排查整步拿掉了，这个开关现在没有作用")
    ap.add_argument("--skip-pos", action="store_true", help="跳过第 3 步：不算 POS 合规")
    ap.add_argument("--skip-pools", action="store_true", help="跳过第 4 步：不做四池对账")
    ap.add_argument("--log-file", default="", help="把对账那段同时写一份到文件")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    plan = [n for n, skip in (("抓华为数据", args.skip_dump),
                              ("POS 合规", args.skip_pos),
                              ("四池对账", args.skip_pools)) if not skip]
    print("=" * 64)
    print("日常流程：" + (" → ".join(plan) if plan else "**三步全跳过，什么都不做**"))
    print("=" * 64)

    # ---------------------------------------------------------------- 1
    if getattr(args, "skip_check", False):
        print("⚠ --skip-check 已经没用了：报量排查整步拿掉了，"
              "现在跑的是「抓四池数据 → POS 合规 → 四池对账」。")

    if args.skip_dump:
        print("\n[1/3] 抓四池数据：**已用 --skip-dump 跳过**（调试模式，别在定时任务里用）")
    else:
        # ⚠ **库里一片空白 ⇒ 这是第一次跑**（刚装、或者刚从 1.x 升上来）⇒ 抓**今年至今**。
        #   只抓当月的话，POS 页**只有当月一个数、前面几个月全是空的** ——
        #   而"历史几个月的合规率"恰恰是这个看板最要紧的东西。
        #   补这一次之后，以后每天都是当月增量（取并集，不会重复）。
        #
        #   ⚠ **是"今年"不是"全部历史"**（用户 2026-09-17 定：「跨年不重要，
        #   就拉当年的全量就行」）。原来传 `--all`，而 `dump.py` 拿到跨年数据会
        #   **直接报错**要求按年分次抓 —— 老店第一次跑正好卡在这儿。
        #   要不要更多历史：`python -m src.cli dump --year 2025` 手动补，一年一个库。
        first_time = not cli._find_pos_db()
        this_year = datetime.date.today().year
        if first_time:
            print("\n[1/4] 抓四池数据：**本地还没有订单库** —— 这是第一次跑，"
                  "抓**今年（%d）至今**的全量补上（只补这一次，以后每天抓当月增量）"
                  % this_year)
        else:
            print("\n[1/4] 抓四池数据：华为当月 → 补进订单库（取并集，不删旧行）")
        rc = cli.cmd_dump(argparse.Namespace(config=args.config, month="",
                                             all=False,
                                             year=(this_year if first_time else 0),
                                             no_refresh=args.no_refresh,
                                             verbose=args.verbose))
        if rc != 0:
            print("\n" + "=" * 64, file=sys.stderr)
            print(f"❌ 第 1 步失败（退出码 {rc}）—— **跳过第 2、3 步，什么都不发**。",
                  file=sys.stderr)
            print("   为什么不接着跑：库覆盖不了今天的窗口，差集会把当天**所有**销售",
                  file=sys.stderr)
            print("   算成「玲珑无但云商有」—— 一份完全错误的清单，而且看着很合理，",
                  file=sys.stderr)
            print("   门店会照着去补报一批假的。**宁可今天没有报告。**", file=sys.stderr)
            print("   先解决第 1 步（多半是华为会话过期）：", file=sys.stderr)
            print(f"     python -m src.cli -c {args.config} dump", file=sys.stderr)
            print("=" * 64, file=sys.stderr)
            return rc

    # ⚠ 只把**真跑了**的步骤记进来 —— 跳过的步骤不许参与退出码。
    #   第一版固定拿 rc2/rc3 两个变量去算，于是"只算 POS"会把没跑过的
    #   报量排查那一步的默认值也算进去，退出码就说不清了。
    done = []                              # [(步骤名, 退出码)]

    # ---------------------------------------------------------------- 3
    if args.skip_pos:
        print("\n[2/3] POS 合规：**已跳过**")
    else:
        print("\n[3/4] POS 合规率 → out/pos-<年>.json")
        rc3 = cli.cmd_pos(argparse.Namespace(db=""))
        done.append(("POS 合规", rc3))
        if rc3 != cli.EXIT_OK:
            print(f"\n⚠ 第 3 步（POS）没跑通：退出码 {rc3}", file=sys.stderr)

    # ---------------------------------------------------------------- 4
    if args.skip_pools:
        print("\n[3/3] 四池对账：**已跳过**")
    else:
        print("\n[4/4] 四池对账（AD=玲珑报了云商没报 / BC=云商报了玲珑没报）")
        rc4 = cli.cmd_pools(argparse.Namespace(
            config=args.config, fetch=[], start="", end="", date="",
            days_ago=0, no_refresh=True, verbose=getattr(args, "verbose", False),
            no_mail=args.no_mail, no_push=args.no_push))
        done.append(("四池对账", rc4))
        if rc4 != cli.EXIT_OK:
            print(f"\n⚠ 第 4 步（四池对账）没跑通：退出码 {rc4}", file=sys.stderr)

    print("\n" + "=" * 64)
    print("日常流程结束：用了 %.1f 秒（%s）"
          % (time.time() - t0,
             "、".join("%s=%d" % (n, rc) for n, rc in done) or "没有步骤被执行"))
    print("=" * 64)
    # 退出码取**第一个不成功的** —— 计划任务那边只看得到这一个数。
    # `EXIT_DIFF`(3) 是"跑通了、只是有差异"，算成功，但要**传出去**。
    for _, rc in done:
        if rc not in (cli.EXIT_OK, cli.EXIT_DIFF):
            return rc
    return cli.EXIT_DIFF if any(rc == cli.EXIT_DIFF for _, rc in done) else cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
