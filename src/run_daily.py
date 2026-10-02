"""日常流程 —— **一条定时任务跑完三步**（用户 2026-09-16 定的）。

    1. 抓取玲珑数据：华为**当月**订单 + 玲珑在库 → 补进 out/cbg-<年>.db（**取并集**）
    2. 抓取云商数据：云商在库 + 云商销售明细 → 同一个库（2026-09-20 从第 1 步拆出来）
    2. POS 合规：库 → 月度分数 → 落 out/pos-<年>.json
    3. 双平台数据对比：A/B/C/D 四个池子做差集 → AD / BC → 推邮件 / 企业微信

⚠ 原来的第 2 步「报量排查」2026-09-17 整步拿掉了，并进第 3 步的双平台数据对比。

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
from pathlib import Path

from . import cli, config_io, version
from .paths import ROOT
from . import edition as _edition
if _edition.is_lifehall():
    # 生活馆包里这些执行件被裁（edition.PRUNE）；对应步骤也不在注册表里，
    # 下面 _RUNNERS 永远不会派发到它们。⚠ 属性必须存在 ——
    # full 模式的测试靠 mock.patch.object(run_daily, "attain_run") 打桩
    # （打桩打的是**这个名字**，名字不在当场 AttributeError，测试直接红）。
    pos_run = report_run = inbox_run = attain_run = plan_run = None
else:
    # ⚠ POS 走**执行模块**（不再经过 CLI）—— 见下面第 3 步那段注释
    from .app.pos import run as pos_run
    from .app.report import run as report_run
    from .app.report_inbox import run as inbox_run
    # ⚠ 模块级 import（**不是**函数里）—— 测试要能 `mock.patch.object(run_daily, "attain_run")`，
    #   藏在函数里的话打不着桩，测试就会**真去读腾讯文档**（实测：全量测试从 59 秒涨到 165 秒）。
    from .features.sales.attain.attain import run as attain_run
    # ⚠ 月度计划走**同一个模式**（2026-09-23 搬过来的）：它原来是 `_step_plan` 函数体里
    #   `from ... import plan` 再调 —— 测试**打不着桩** ⇒ 每次全量测试都真写**项目根**
    #   `out/plan-2026.json`（4.5MB 真落盘每次被覆盖），且固定名 `plan-2026.json.tmp`
    #   被三个头的 pytest 同时抢 ⇒ 后到的那个 `replace()` 报 `FileNotFoundError` ——
    #   就是 2026-09-23 抓到的那个并行偶发红。别改回函数内 import。
    from .features.plan.monthly.plan import run as plan_run

#: 一次日常流程有哪些步骤 —— ⚠ **从功能注册表派生**（用户 2026-09-19 的"安装注册机制"）。
#:
#: 以前这儿是**三张平行的手写表**（`STEPS` / `STEP_LABELS` / `STEP_FLAGS`），
#: 加上界面预设那三张（`BUTTON_STEPS` / `BUTTON_LABELS` / `AUTOMATION_DEFAULT_STEPS`）
#: 一共**六张**：加一个步骤要在一个文件里改六处，漏一处的表现是"界面上有、实际不跑"
#: 或者反过来（最难查的那种）。
#: ⚠ 2026-09-21 晚：界面预设那三张**删掉了**（"手动跑整批"整个取消），
#:   手动那份脚本的名单改用 `MANUAL_STEPS` —— 它也是从注册表派生的，只是名字直说了用途。
#:
#: 现在：**步骤在哪由功能自己声明**（`features/<一级>/__init__.py` 里的 `Step(...)`），
#: 能力层自带的（抓数）在 `registry.BUILTIN_STEPS`。
#: ⚠ 派生出来的值与手写时**逐字段一致**（`tests/test_registry.py` 钉着）。
from .features.registry import (default_steps as _reg_default,
                                step_flags as _reg_flags,
                                step_labels as _reg_labels,
                                steps as _reg_steps)

STEPS = _reg_steps()
STEP_LABELS = _reg_labels()
#: 步骤 → `daily` 的跳过开关（`Step.flag`；不给就按 `--skip-<cmd>` 推）。
#: ⚠ `reconcile` 对应的开关历史上叫 `--skip-check` —— 那张表不在这儿管了
#: （它已经不是 `daily` 的步骤，见 `STEP_FLAGS_EXTRA`）。
STEP_FLAGS = _reg_flags()

#: 门店那份**手动双击**的 `run-now.bat` 跑哪几步 —— **由注册表派生**（`Step.default`）。
#:
#: ⚠ 抓数据默认**必须**在里面（`BUILTIN_STEPS` 里 `default=True`）——
#:   不抓的话库永远是旧的，POS 和双平台数据对比都只是拿旧数据在算，
#:   而日志只会说"跑完了"。这是最难发现的一类错。
#:
#: ⚠⚠ 2026-09-21 晚（用户：「**现在不需要 run daily 吧，按定时器运行就行了**」）——
#:   **"整批"这个模式整个删掉了**，`daily` 现在**必须 `--steps` 点名**。
#:   三条入口一律点名，而且名单都能追到注册表：
#:     * 到点：内置定时器按**每一步自己的时刻**派发（`timer.wake_argv`）；
#:     * 各页「刷新」：`web.REFRESH_STEPS[page]`；
#:     * 门店双击：这份（`MANUAL_STEPS`）。
#:   ⇒ "跑什么"**只有一个来源**（注册表），没有第二处能悄悄限制范围。
#:   原来那张 `BUTTON_STEPS`（界面「整个项目」预设）和 `ALWAYS_STEPS`/`with_always`
#:   （"抓数永远跑"的补丁）**一起删了** —— 它们都在给"整批"打工，而整批没有了。
MANUAL_STEPS = _reg_default()


def requested_steps(raw) -> list:
    """两层 daily 入口共用的用法校验；执行任何副作用前先调用。"""
    only = [x.strip() for x in str(raw or "").replace("，", ",").split(",") if x.strip()]
    if not only:
        raise ValueError("现在必须点名跑 —— 请给 `--steps`（逗号分隔）")
    bad = [x for x in only if x not in STEP_FLAGS]
    if bad:
        raise ValueError("不认识的步骤：%s（只认 %s）"
                         % ("、".join(bad), "、".join(STEPS)))
    return only


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

    ⚠ 「一件都不选」是错的：`daily` 现在**必须点名**（没有"整批"这个默认了），
    一件都没点名 = 命令写错了，见 `main()` 顶上那段。
    """
    got = _norm_steps(steps)
    if not got:
        raise ValueError("至少要选一件事（%s）" % "、".join(STEP_LABELS[s] for s in STEPS))
    return got


def steps_label(steps) -> str:
    """「跑什么」那一列的文字 —— **列出执行项目**，不是给一句概括。

    写「整个项目」门店看不懂那指什么；写成
    「抓华为数据 + 报量排查 + POS 合规」一眼就知道每天到底干了哪几件事。
    """
    return " + ".join(STEP_LABELS[s] for s in _check_steps(steps))


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
    # ⚠⚠ **`--skip-*` 这一组已经不用了**（"整批"这个模式 2026-09-21 晚取消了，
    #   见 `MANUAL_STEPS` 那段）：跑什么一律由 `--steps` 点名。
    #   留着它们只有一个理由 —— **老门店电脑上那份 `run-now.bat` / 老计划任务里
    #   写死了这些开关**，删掉会以 `unrecognized arguments` 收场，
    #   而那种失败最难查（门店看到的是"双击了一下，窗口一闪就没了"）。
    #   ⇒ 收下、**打印一句"它没用了，怎么改"**、然后忽略（见下面那段）。
    #   ⚠ 名单从注册表派生（`Step.flag`），别在这儿手抄一份 ——
    #     手抄的那次就漏了 `--skip-autoupdate`（`daily --skip-autoupdate`
    #     会以 unrecognized arguments 收场，而那个开关在界面上写着"有"）。
    for _cmd, _flag in STEP_FLAGS.items():
        ap.add_argument(_flag, action="store_true",
                        help="（已废弃）跑什么改由 --steps 点名，这个开关不再起作用")
    ap.add_argument("--skip-check", action="store_true",
                    help="（已废弃）报量排查整步拿掉了，这个开关现在没有作用")
    # ⭐ **唯一的入口**（2026-09-21 晚定的）：跑什么必须点名。
    #   ⚠ 原来不给 `--steps` 就是"整批"（`default=True` 那几步）—— 那条路删了：
    #     同一个"跑一遍"有三种说法（界面按钮 / 整批 / 点名），迟早有一处跑错东西。
    ap.add_argument("--steps", default="",
                    help="**必给**：就跑这几步（逗号分隔，如 dump,erp-dump,pos,pools,attain）")
    # ⚠ 只用来**记一笔**（`run_record.kind="wake"`）—— 定时器去重靠它，
    #   所以它必须由**真跑这一步的进程**写，不是定时器代笔。
    ap.add_argument("--wake-slot", default="",
                    help="内置定时器派发时的那个时间点（YYYY-MM-DD HH:MM），只用来记录")
    ap.add_argument("--log-file", default="", help="把对账那段同时写一份到文件")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # ---- 「就跑这几步」—— **唯一**的入口（`--steps` 必给）
    try:
        only = requested_steps(getattr(args, "steps", ""))
    except ValueError as e:
        # ⚠ **报错，不回落**。回落成"整批"的话，一份老脚本 / 一次手滑
        #   （忘了写 `--steps`）会变成"把一整套又跑一遍"：抓数、推送、上报
        #   全都真做一遍，而**发出去的东西收不回来**。
        print("❌ %s，例如：" % e, file=sys.stderr)
        print("     python -m src.cli -c %s daily --steps %s"
              % (args.config, ",".join(MANUAL_STEPS)), file=sys.stderr)
        print("   认得的步骤：%s" % "、".join(STEPS), file=sys.stderr)
        print("   到点跑哪几步不再需要人来定 —— 内置定时器按**每一步自己的时刻**派发"
              "（控制台「定时器设置」页里能看到每一步的时间）。", file=sys.stderr)
        return cli.EXIT_USAGE
    # ⚠ **老脚本带来的 `--skip-*`**：先看一眼（下面那圈会把它们全覆写掉）。
    _legacy = sorted(f for c, f in STEP_FLAGS.items()
                     if getattr(args, "skip_" + c.replace("-", "_"), False))
    for _s in STEPS:
        # ⚠ 走 `set_skip`（短横线→下划线）—— 直接 setattr 的话 `erp-dump`
        #   会被写成读不到的属性名，于是"没点名的步骤照样跑"（真踩到）。
        set_skip(args, _s, _s not in only)
    args.only_steps = list(only)
    if _legacy:
        # ⚠ 必须**说出来**：不说的话，老脚本里那几个开关就像"还在生效"，
        #   而下一个人会照着一个已经不存在的行为去调它。
        print("⚠️ 这几个开关已经不用了：%s" % " ".join(_legacy))
        print("   （跑什么改由 `--steps` 点名 —— 这份脚本多半是旧版生成的，"
              "打开一次控制台它会自动重建）")

    t0 = time.time()
    # ⚠ 一律走 `skip_of()`（**短横线会换成下划线**）—— 别再直接读 `args.skip_xxx`：
    #   `erp-dump` 那种带短横线的步骤名，手写属性名会读成一个不存在的属性 ⇒
    #   永远 False ⇒ "点了跳过却照样跑"（2026-09-20 真踩到）。
    plan = [n for n, cmd in (("抓华为数据", "dump"), ("抓云商数据", "erp-dump"),
                             ("POS 合规", "pos"), ("双平台数据对比", "pools"),
                             ("销售达成", "attain")) if not skip_of(args, cmd)]
    print("=" * 64)
    print("日常流程：" + (" → ".join(plan) if plan else "**全跳过，什么都不做**"))
    print("=" * 64)

    # ------------------------------------------------- 这家店根本不走玲珑？
    #
    # 用户 2026-09-18：「云商登录成功之后看是哪个店，**如果是我们串号标识里有的
    # 那十四家店需要登录玲珑，其余店不需要**……这就是做的账号、门店权限与内容的划分」。
    #
    # ⚠ 这三步**一步都跑不了**：
    #   * 第 1 步抓的就是玲珑数据（华为当月订单）；
    #   * POS 合规读的是 `orders`/`payments`/`returns` —— **也是玲珑侧**的表
    #     （`pos_report.py`；这一点我一开始判错过，见 2026-09-18 日志）；
    #   * 双平台数据对比两边都要。
    #
    # ⚠ **不报错**（退出码 0）：这是"这家店本来就没有这些活"，不是失败。
    #   报错的话门店会天天看到一条红的，然后就不看了。
    # ⚠ 读不到配置时**不许走早退** —— 那会把"配置文件在哪、为什么读不到"
    #   这个真正的错吃掉，然后每天"成功"地什么都不干。
    #   读不到就照常往下走，让各步骤自己报错。
    try:
        _cfg = cli.load_config(args.config)
    except SystemExit:
        _cfg = None
    profile = cli.store_profile_of(_cfg) if _cfg else {"needs_linglong": True}
    if not profile["needs_linglong"]:
        # ⚠ 点名模式下（比如"刷新销售达成"）**别报这一段** ——
        #   这段是解释"为什么玲珑那三步不跑"，而那时候它们本来就没被点名，
        #   说一遍只会让人以为"刷达成动了玲珑"（用户 2026-09-21 这么误解过）。
        _ling = [c for c in ("dump", "pos", "pools")
                 if not getattr(args, "only_steps", None) or c in args.only_steps]
        if _ling:
            print("这家店不走玲珑（`config/stores.yaml` 里它没有串号标识）——")
            print("  抓取玲珑数据、POS 合规、双平台数据对比三步读的都是玲珑数据，跳过。")
        # ⚠⚠ 2026-09-20 改：**合作店照样跑"销售达成"**。
        #   原来这儿是直接 `return EXIT_OK`（整趟什么都不干），当时的理由是
        #   「达成还没做（M4）」—— 而现在它做好了，那句话就成了**静默的假成功**：
        #   合作店的达成**永远不会自动算**，日志里却写着"日常流程结束 exit=0"。
        #   ⇒ 改成"把玲珑那三步按掉，剩下的照常走"；真要一步都不剩（比如
        #     `--steps dump,pos`）再早退，而且**说清楚**为什么。
        for _c in ("dump", "pos", "pools"):
            set_skip(args, _c, True)
        # ⚠ 云商那一步**不按掉**：合作店的销售达成读的就是云商销售明细
        #   （`erp_sales`）—— 恰恰是最需要它的那一步。
        # ⚠⚠ 判据是"**还有没有这家店能跑的步骤**"，不是"达成跳过没" ——
        #   2026-09-20 加了自动更新之后，`daily --steps autoupdate` 会被老判据
        #   误判成"没活干"直接早退 ⇒ **那一趟的自动更新永远不会发生**。
        #   合作店能跑的：抓云商（达成/无忧要靠它）、销售达成、无忧会员权益、自动更新。
        #   ⚠ 2026-09-21（M18）补了 `report` —— 跟 autoupdate 当年一模一样的坑：
        #     合作店**也能上报**（它上报的是云商侧那几张表 + 玲珑在库快照），
        #     漏在这张表里的表现是"点名跑上报"被这句早退掉，日志里还写着正常结束。
        #   ⚠ 2026-09-22 补 `benefit` / `film` —— 同理：只读云商，合作店也该能算。
        _left = [_s for _s in ("erp-dump", "attain", "plan", "autoupdate",
                               "report", "report-inbox", "film", "benefit")
                 if not skip_of(args, _s)]
        if not _left:
            print("  这趟里没有这家店能跑的步骤（剩下的都被跳过了）。")
            return cli.EXIT_OK  # ⚠ 常量在 `cli` 里（本模块别自己再定义一份）

    # ⚠ 顶上写清"就跑这几步"，没点名的那几步**一律不吭声** ——
    #   原来每步都会打一行「抓取玲珑数据：**已用 --skip-dump 跳过**」，
    #   于是"刷新销售达成"的日志里也出现"抓取玲珑数据"字样，
    #   用户 2026-09-21 当场问「刷达成为什么要动玲珑」（它压根不依赖玲珑）。
    print("这趟跑的：%s（其余 %d 步不跑）"
          % (" + ".join(STEP_LABELS.get(c, c) for c in args.only_steps),
             len(STEPS) - len(args.only_steps)))
    print()

    # ⚠ `--skip-check` 早没用了（报量排查整步拿掉）—— 老脚本还在传，说一声
    if getattr(args, "skip_check", False):
        print("⚠ --skip-check 已经没用了：报量排查整步拿掉了，"
              "现在跑的是「抓取玲珑数据 → POS 合规 → 双平台数据对比」。")
    # ⚠ 只把**真跑了**的步骤记进来 —— 跳过的步骤不许参与退出码。
    #   第一版固定拿 rc2/rc3 两个变量去算，于是"只算 POS"会把没跑过的
    #   报量排查那一步的默认值也算进去，退出码就说不清了。
    done = []                              # [(步骤名, 退出码)]

    # ================================================================ 跑
    # ⚠⚠ 2026-09-21（用户：「**相同时间执行的任务，按照定时器这个列表从上到下执行**」）——
    #   每一步包成一个小函数，**按点名的顺序**调（`args.only_steps` 就是定时器派发时
    #   给的顺序 = 控制台那张表从上到下的顺序；用户还能在表里调它）。
    #   ⚠ 以前这 8 个块是**写死的代码顺序** ⇒ 用户在表里调了顺序，实际还是按代码顺序跑
    #     （**静默不一致**：日志顶上写着"A + B"，跑的却是"B → A"）。
    #   ⚠ 「抓数失败就中止后面」那条规矩原来是**隐式**的（写在前面的块直接 return）——
    #     改成按顺序跑之后必须**显式**写出来（`_ABORT_AFTER`），否则一次抓数失败会接着
    #     拿旧库去算、发一份看着很合理的错清单（那正是当年要防的事）。
    #   ⚠ 加新步骤：在这儿加一个 `_step_xxx`，并确认它在 `_RUNNERS` 里
    #     （有测试钉着"注册表里的每一步都有块"）。

    def _step_dump():
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
            print("\n[1/9] 抓取玲珑数据：**本地还没有订单库** —— 这是第一次跑，"
                  "抓**今年（%d）至今**的全量补上（只补这一次，以后每天抓当月增量）"
                  % this_year)
        else:
            print("\n[1/9] 抓取玲珑数据：华为当月 → 补进订单库（取并集，不删旧行）")
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

# ---------------------------------------------------------------- 2
# ⚠ 2026-09-20 新增（用户：「数据抓取再加个**云商数据定时抓取**吧」）——
#   云商那两个池子（池C 销售 / 池D 在库）原来混在第 1 步里"顺手"拉，
#   现在**单独一步**：能单独设时间、单独开关、单独跑一次。
# ⚠ 失败也**中止后面**：POS 读玲珑、双平台两边都要、销售达成读云商销售明细 ——
#   少了云商那半边，"双平台"就成了一边有一边没有，那种报告看着很合理但全错。
    def _step_erp_dump():
        print("\n[2/9] 抓取云商数据：云商在库 + 云商销售明细 → 补进订单库")
        rc_erp = cli.cmd_erp_dump(argparse.Namespace(config=args.config,
                                                     verbose=getattr(args, "verbose", False)))
        if rc_erp != 0:
            print("\n" + "=" * 64, file=sys.stderr)
            print(f"❌ 第 2 步（抓云商数据）失败（退出码 {rc_erp}）—— "
                  "**跳过后面的分析，什么都不发**。", file=sys.stderr)
            print("   云商那半边没进来，双平台对比会变成「一边有一边没有」，"
                  "而那种报告看着很合理。", file=sys.stderr)
            print(f"   先单独试一次：python -m src.cli -c {args.config} erp-dump",
                  file=sys.stderr)
            print("=" * 64, file=sys.stderr)
            return rc_erp

# ---------------------------------------------------------------- 3
    def _step_pos():
        print("\n[3/9] POS 合规率 → out/pos-<年>.json")
        # ⚠ 2026-09-19 起**直接调执行模块**（`app.pos.run`），不再手工拼 `Namespace`：
        #   原来那行是 `cli.cmd_pos(argparse.Namespace(db=""))` —— 只传了 `db`，
        #   而推送那条路第一句就是 `if not config_path: return`
        #   ⇒ **daily 每天算了 POS，却从来没推过 POS**（门店只有手动跑才收得到）。
        #   现在参数是**关键字、有名字**，漏一个在测试里就看得出来；
        #   这条链也不再经过 CLI（daily 只管顺序与失败依赖）。
        #   ⚠ 当时 1410 条测试一条都没抓到 —— 因为那些测试把 `cmd_pos` 整个 mock 掉了。
        #     `TestDaily要把POS推送跑起来` 现在**不 mock 执行模块**，只换外部发送端。
        res = pos_run(db="", config_path=args.config, no_push=args.no_push,
                      no_mail=args.no_mail, emit=print)
        rc3 = cli.EXIT_OK if res.ok else cli.EXIT_FETCH
        done.append(("POS 合规", rc3))
        if rc3 != cli.EXIT_OK:
            print(f"\n⚠ 第 3 步（POS）没跑通：{res.why}", file=sys.stderr)

# ---------------------------------------------------------------- 4
    def _step_pools():
        print("\n[4/9] 双平台数据对比（AD=玲珑报了云商没报 / BC=云商报了玲珑没报）")
        rc4 = cli.cmd_pools(argparse.Namespace(
            config=args.config, fetch=[], start="", end="", date="",
            days_ago=0, no_refresh=True, verbose=getattr(args, "verbose", False),
            no_mail=args.no_mail, no_push=args.no_push))
        done.append(("双平台数据对比", rc4))
        if rc4 != cli.EXIT_OK:
            print(f"\n⚠ 第 4 步（双平台数据对比）没跑通：退出码 {rc4}", file=sys.stderr)

# ---------------------------------------------------------------- 5
    def _step_attain():
        print("\n[5/9] 销售达成（本周目标 vs 云商实际）")
        # ⚠ 直接调执行模块（照 POS 那一处的做法），**不手工拼 Namespace** ——
        #   2026-09-19 的教训：POS 那次只传了 `db`，推送那一句 `if not config_path: return`
        #   就静默跳过了，**daily 每天算了 POS 却从来没推过**。
        #   这里参数是关键字、有名字，漏一个在调用点就看得出来。
        # ⚠ 门店端只看自己那一行；办公室 / 平台岗看全区（`store_filter`）。
        # ⚠ 用 `config_io.load_raw`（读不到给 `{}`），**不要** `cli.load_config` ——
        #   后者找不到文件时抛的是 `SystemExit`，而它是 `BaseException`，
        #   会把整条 daily 带崩（AGENTS.md 坑 11）。
        store = ""
        try:
            _cfg = config_io.load_raw(Path(args.config)) or {}
            if not _cfg:
                raise ValueError("门店配置为空或读不到")
            # ⚠⚠ **平台岗 / 办公室不能当成"某家店"** —— 它们的 `erp_store_name`
            #   是「平台岗」（虚拟门店），拿它当过滤条件 ⇒ 一行都匹配不上 ⇒
            #   报「这家店不在目标表里（门店名对不上？）：平台岗」（实测踩过）。
            #   判据用**画像**（`show_all`），别自己去猜名字。
            _prof = cli.store_profile_of(_cfg)
            # ⚠ `erp_name` 拿不到就**回落到配置里的店名**，**不许悄悄变成"看全区"** ——
            #   那正是用户 2026-09-21 报的那个 bug（门店账号看到全区）。
            store = "" if _prof.get("show_all") else (
                _prof.get("erp_name") or _cfg.get("erp_store_name") or "")
            if not _prof.get("show_all") and not store:
                raise ValueError("没有可确认的本店名称")
        except Exception as e:                                    # noqa: BLE001
            print(f"  ❌ 读不出本店名（{type(e).__name__}: {e}）—— 销售达成本次不算，"
                  "避免把全区当本店", file=sys.stderr)
            done.append(("销售达成", cli.EXIT_FETCH))
            return
        res = attain_run(config_path=args.config, root=None, store_filter=store,
                             no_push=args.no_push, no_mail=args.no_mail, emit=print)
        rc5 = cli.EXIT_OK if res.get("ok") else cli.EXIT_FETCH
        done.append(("销售达成", rc5))
        if rc5 != cli.EXIT_OK:
            print(f"\n⚠ 第 5 步（销售达成）没跑通：{res.get('why')}", file=sys.stderr)
            print("   （这一步失败**不影响**前面几步的结果，那些已经落盘了）",
                  file=sys.stderr)

# ---------------------------------------------------------------- 6
# **月度生意计划**（M22，2026-09-21）：七个大块（折叠机/FD/ND/穿戴/音频/平板/电脑）
# 的**当月至今 vs 上月同期**，销量 / 销售额 / 利润 + 环比；块下面还能展开到系列、机型。
# ⚠ 读的是**云商销售明细**（`erp_sales`，第 2 步刚写完的那张）⇒ 排在 `attain` 后面。
# ⚠ 失败**不中止后面**（跟 attain 一样）：它只落自己那份 JSON，别的步骤不读它。
# ⚠ 合作店**也跑这一步**（它读云商、不读玲珑）—— 见下面 `_left` 那张表的注释。
    def _step_plan():
        print("\n[6/9] 月度生意计划（七块 × 本月至今 vs 上月同期）")
        # ⚠ 走**模块级别名** `plan_run`（测试在这儿下桩）—— 别改回函数内 import，
        #   那样打不着桩，测试会真写项目根 out/（2026-09-23 并行偶发红的根因）。
        res = plan_run(root=None, emit=print)
        rc_plan = cli.EXIT_OK if res.get("ok") else cli.EXIT_FETCH
        done.append(("月度生意计划", rc_plan))
        if rc_plan != cli.EXIT_OK:
            print(f"\n⚠ 第 6 步（月度生意计划）没跑通：{res.get('why')}", file=sys.stderr)
            print("   （这一步失败**不影响**前面几步的结果，那些已经落盘了）",
                  file=sys.stderr)

# ---------------------------------------------------------------- 6b
# **防护膜达成**（2026-09-22）：扫销售导出 → 落 `out/film.json` 快照。
# ⚠ 页面是**现算**的；这一步只是留痕 + 给 timer 一个可注册的钩子。
# ⚠ `default=False`：不进每天双击那趟 —— 要跑就在「增值 › 设置」里定时刻。
    def _step_film():
        print("\n[6b] 防护膜达成（落快照 out/film.json）")
        from .features.valueadd.film import compute as film_compute
        res = film_compute.run(root=None, emit=print)
        rc_film = cli.EXIT_OK if res.get("ok") else cli.EXIT_FETCH
        done.append(("防护膜达成（落快照）", rc_film))
        if rc_film != cli.EXIT_OK:
            print(f"\n⚠ 防护膜达成没算成：{res.get('why')}", file=sys.stderr)

# ---------------------------------------------------------------- 6c
# **无忧会员权益**（2026-09-22）：erp_sales → 落 `out/benefit.json` 快照。
# ⚠ 同 film：页面现算；这一步只留痕。`default=False` 不进每天双击那趟。
    def _step_benefit():
        print("\n[6c] 无忧会员权益（落快照 out/benefit.json）")
        from .features.valueadd.benefit import compute as benefit_compute
        res = benefit_compute.run(root=None, emit=print)
        rc_b = cli.EXIT_OK if res.get("ok") else cli.EXIT_FETCH
        done.append(("无忧会员权益（落快照）", rc_b))
        if rc_b != cli.EXIT_OK:
            print(f"\n⚠ 无忧会员权益没算成：{res.get('why')}", file=sys.stderr)

# ---------------------------------------------------------------- 7
# **数据上报**（M18，2026-09-21）：把当天新增/变化的行打成 SQLite 附件，
# 邮件发给本店区长（抄送中台）。
# ⚠ 它**有自己的时刻（21:15）**，所以**不在每天那趟整批里**（`default=False`）——
#   用户 2026-09-21 晚：「**上报数据还是有自己的吧**」。走整批时把它按掉，
#   跟 `report-inbox` / `autoupdate` 一个道理（不然一天跑两遍）。
# ⚠ 时间卡在两个约束之间（见注册表那条注释）：晚于 21:00 那趟（要发刚抓完的数）、
#   早于 21:30（区长/平台那台机器 21:30 收信，晚了就变"明天才看到"）。
    def _step_report():
        print("\n[7/9] 上报数据（当天新增/变化的行 → SQLite 附件 → 邮件给区长）")
        res = report_run(root=None, config_path=args.config,
                         no_push=args.no_push, emit=print)
        rc6 = cli.EXIT_OK if (res.get("ok") or res.get("skipped")) else cli.EXIT_FETCH
        done.append(("数据上报", rc6))
        if rc6 != cli.EXIT_OK:
            print(f"\n⚠ 第 6 步（上报数据）没发出去：{res.get('why')}", file=sys.stderr)
            print("   ⚠ 包已经留在 out/report/pending/ —— **下次跑会自动补发**，"
                  "不用人工干预", file=sys.stderr)

# ---------------------------------------------------------------- 8
# **收取门店上报**（M19）：区长 / 平台那台机器收信落库。
# ⚠ 不在整批里（`default=False`）：它 21:30 自己跑一趟（门店 21:00 才发信）。
#   走整批时把它按掉 —— 跟 `autoupdate` 一个道理（那条规矩在上面）。
    def _step_report_inbox():
        print("\n[8/9] 收取门店上报（读邮箱 → 落 in/report.db）")
        res = inbox_run(root=None, config_path=args.config, emit=print)
        rc7 = cli.EXIT_OK if res.get("ok") else cli.EXIT_FETCH
        done.append(("收取上报", rc7))
        if rc7 != cli.EXIT_OK:
            print("\n⚠ 第 7 步（收取门店上报）没成功：%s"
                  % "；".join(res.get("problems") or []) or "？", file=sys.stderr)

# ---------------------------------------------------------------- 9
# ⚠ **健康模块**的自动更新（用户 2026-09-20：「健康模块默认注册一个自动更新，
#   **固定一个小时执行一次**」）。
#   ⚠ 它**不在"每天那趟整批"里**（`default=False`）—— 定时器按小时单独叫醒它
#     （`daily --steps autoupdate`）。走整批时上面那段会把它按掉。
    def _step_autoupdate():
        from .modules import health
        print("\n[9/9] 自动更新（先问策略：健康 + 今天那趟跑完了才动手）")
        res = health.auto_update(ROOT, version.VERSION, emit=print)
        rc6 = cli.EXIT_OK if res.get("ok", True) else cli.EXIT_FETCH
        # ⚠ 更新成功**不进 done** —— 那会把这一趟的退出码变成一个"刚换完代码"的
        #   进程说了算的东西，而它下一秒就要退出了。跑没跑成看日志和界面。
        if rc6 != cli.EXIT_OK:
            print("\n⚠ 自动更新没跑成：%s" % (res.get("why") or ""), file=sys.stderr)

    #: 点名的步骤 → 跑它的那个小函数（键就是注册表里的 `cmd`）
    _RUNNERS = {
        "dump":        _step_dump,
        "erp-dump":    _step_erp_dump,
        "pos":         _step_pos,
        "pools":       _step_pools,
        "attain":      _step_attain,
        "plan":        _step_plan,
        "film":        _step_film,
        "benefit":     _step_benefit,
        "report":      _step_report,
        "report-inbox": _step_report_inbox,
        "autoupdate":  _step_autoupdate,
    }

    #: 这几步失败 ⇒ **中止后面**（后面的步骤读的就是它们写进去的库）
    _ABORT_AFTER = {"dump", "erp-dump"}

    for _cmd in list(args.only_steps):
        if skip_of(args, _cmd):
            continue            # 合作店那三步会被按掉（见上面那段）
        _fn = _RUNNERS.get(_cmd)
        if _fn is None:
            print("⚠ 「%s」还没有接进日常流程 —— 这趟不跑它"
                  % STEP_LABELS.get(_cmd, _cmd), file=sys.stderr)
            continue
        _rc = _fn()
        if _rc and _cmd in _ABORT_AFTER:
            # ⚠ 为什么中止、接下来怎么办 —— 那段话由各步自己打（它们最清楚原因）
            return _rc

    print("\n" + "=" * 64)
    print("日常流程结束：用了 %.1f 秒（%s）"
          % (time.time() - t0,
             "、".join("%s=%d" % (n, rc) for n, rc in done) or "没有步骤被执行"))
    print("=" * 64)
    # ⚠ 内置定时器那一趟的**运行记录不在这个函数里写** —— 它有好几个提前 return
    #   （第 1 步失败就不往下走了、合作店直接早退），在这儿写就**漏掉最该记的那种**：
    #   失败的那一趟。而"这个 slot 试过没"正是靠它去重的，
    #   漏了就会被每 30 秒重派一次（云商不能并行登录，会被挤爆）。
    #   ⇒ 写在调用方 `cli.cmd_daily` 的收尾处（`record_wake`），**每条出口都过**。

    return _final_rc(done)


def set_skip(args, cmd: str, skip: bool) -> None:
    """把"跳过某一步"记到 `args` 上 —— **`cmd` 里的短横线要换成下划线**。

    ⚠⚠ 2026-09-20 踩到：`--steps` 那条路原来是 `setattr(args, "skip_" + cmd, …)`，
      而 `erp-dump` 带短横线 ⇒ 写成 `args.skip_erp-dump`（一个**语法上就不存在**
      的属性名，`setattr` 反而照收），读的地方 `getattr(args, "skip_erp_dump", False)`
      永远拿 False ⇒ **`--steps` 里没点名的步骤照样会跑**，而且一声不吭
      （实测：`--steps dump` 把"抓云商数据"也带跑了，那一步会真去动数据）。

    ⇒ **读写都走这两个函数**，别在别处手写 `args.skip_xxx`。
    """
    setattr(args, "skip_" + str(cmd).replace("-", "_"), bool(skip))


def skip_of(args, cmd: str) -> bool:
    """读"这一步跳过没"（跟 `set_skip` 同一套映射）。"""
    return bool(getattr(args, "skip_" + str(cmd).replace("-", "_"), False))


def _final_rc(done) -> int:
    """整趟的退出码 = **第一个不成功的那个**（计划任务那边只看得到这一个数）。

    ⚠ `EXIT_DIFF`(3) 是"跑通了、只是有差异"，**算成功**，但要原样传出去。
    """
    for _, rc in done:
        if rc not in (cli.EXIT_OK, cli.EXIT_DIFF):
            return rc
    return cli.EXIT_DIFF if any(rc == cli.EXIT_DIFF for _, rc in done) else cli.EXIT_OK


def record_wake(steps_csv: str, slot: str, rc: int, seconds: float = 0.0) -> None:
    """内置定时器那趟的**运行记录**（`run_record.kind="wake"`）。**每条出口都过它。**

    ⚠ 写入者是**真跑这一步的进程**（这条命令自己），不是定时器 ——
      服务中途被杀 / 机器断电时，记录里就不会出现一条"跑成功了"。
    ⚠⚠ **成功失败都要记**：`timer.due()` 按"这个 slot 试过没"去重，
      只记成功的话，一次失败会被每 30 秒重试一遍（云商不能并行登录，会一起挂）。
      ⇒ 所以它**不能写在 `main()` 里**：那儿第 1 步失败 / 合作店早退都是直接 return。
    ⚠ 记不上不许影响退出码（`runlog.record` 自己就不抛）。
    """
    from .storage import runlog
    only = [x.strip() for x in str(steps_csv or "").replace("，", ",").split(",") if x.strip()]
    # ⚠ 记的就是**点名的这几步**（2026-09-21 晚起 `daily` 必须点名）——
    #   原来那句 `only or list(STEPS)` 是给"整批"兜底的，整批没有了。
    steps = [x for x in only if x in STEP_FLAGS]
    ok = rc in (cli.EXIT_OK, cli.EXIT_DIFF)
    runlog.record("wake", ok,
                  why="" if ok else "退出码 %d" % rc,
                  note="内置定时器：%s" % "、".join(STEP_LABELS[x] for x in steps),
                  detail={"slot": str(slot or ""), "steps": steps, "trigger": "timer",
                          "exit_code": int(rc), "seconds": round(seconds, 1)})


if __name__ == "__main__":
    sys.exit(main())
