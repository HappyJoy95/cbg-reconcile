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
import sys
import time
from pathlib import Path

from . import cli
# ⭐ 2026-10-02（协议 v2）：报告/收取/自动更新也收编进 `Step(run=...)` ——
#   本文件**不再按名字 import 任何执行件**；测试桩打**实现模块**
#   （`src.app.report.run` / `src.app.report_inbox.run` /
#   `src.modules.health.auto_update`）—— 桩打错地方 = 真去连邮箱/写真库
#   （2026-09-19 / 2026-09-23 各踩过一次）。

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
from .features import registry as _registry
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


def step_decl(cmd: str):
    """注册表里这一步的声明（`Step`）—— 没有就 `None`。

    合作店早退、失败中止、注册表执行入口三处都问它（协议 v2 的判据入口）。
    """
    return next((s for s in _registry.all_steps() if s.cmd == cmd), None)


def _make_registry_runner(step, done, config, args=None):
    """把注册表声明的 `Step.run`（协议 v2）包成当前日常流程的一步。

    * `ctx`：`root`（None = 项目根）/ `config` / `emit` /
      `args`（`daily` 的解析参数 —— `no_push` / `no_mail` 这类开关从这拿）；
      抓取系统步骤还会收到 CLI 适配的认证与采集器服务；
    * 返回值翻译：`True`/`None` → `EXIT_OK`，`False` → `EXIT_FETCH`，`int` 原样 ——
      功能模块**不许 import `cli`**（features 红线），退出码语义集中在这儿；
    * `run` 自己打自己的步骤头（跟手写块同一个约定），这里不代打；
    * 成功/失败都记进 `done`（汇总行与退出码跟手写块同口径）——
      除非声明 `Step(record=False)`（autoupdate：不进汇总）。
    """
    def _run() -> int:
        ctx = argparse.Namespace(
            root=None, config=config, emit=print,
            args=argparse.Namespace(**vars(args)) if args else None,
            daily_step=getattr(step, "cmd", "") in ("dump", "erp-dump"),
            services=(cli.fetch_execution_services()
                      if getattr(step, "cmd", "") in ("dump", "erp-dump") else None))
        out = step.run(ctx)
        if isinstance(out, bool):
            rc = cli.EXIT_OK if out else cli.EXIT_FETCH
        elif out is None:
            rc = cli.EXIT_OK
        else:
            rc = int(out)
        if step.record:                 # `record=False` 不进汇总（autoupdate 的规矩）
            done.append((step.label, rc))
        return rc
    return _run


def main(argv=None) -> int:
    from .modules.auth import runtime_guard
    with runtime_guard.guard(cli.ROOT) as acquired:
        if not acquired:
            print("有任务或入口设置正在使用本机运行身份，请等待结束后重试", file=sys.stderr)
            return cli.EXIT_USAGE
        return _main(argv)


def _main(argv=None) -> int:
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
    from .modules.auth import runtime
    denied = [cmd for cmd in only if not runtime.step_available(
        cmd, cli.ROOT, recurring=bool(args.wake_slot))]
    if denied:
        print("生活馆入口不支持执行：%s" % "、".join(denied), file=sys.stderr)
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
    profile = ({"needs_linglong": True} if runtime.is_lifehall(cli.ROOT)
               else cli.store_profile_of(_cfg) if _cfg else {"needs_linglong": True})
    if not profile["needs_linglong"]:
        # ⭐ 合作店跳过哪几步 = **注册表声明**（`Step.partner_ok=False`，协议 v2 单源）。
        #   原来是两张写死的名单：下面这个跳过集合 + "能跑的剩余步骤"白名单 ——
        #   白名单漏过两次（autoupdate / report 都被静默早退过，历史注释还在下面），
        #   现在新步骤**默认照跑**（`partner_ok` 缺省 True），不再需要"记得补名单"。
        # ⚠ 今天声明 False 的恰好是玲珑三步，有测试钉着这个集合 ——
        #   将来多出一个的话，下面那句解释文案要一起改。
        _nop = [s.cmd for s in _registry.all_steps() if not s.partner_ok]
        # ⚠ 点名模式下（比如"刷新销售达成"）**别报这一段** ——
        #   这段是解释"为什么玲珑那三步不跑"，而那时候它们本来就没被点名，
        #   说一遍只会让人以为"刷达成动了玲珑"（用户 2026-09-21 这么误解过）。
        _ling = [c for c in _nop
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
        for _c in _nop:
            set_skip(args, _c, True)
        # ⚠ 云商那一步**不按掉**：合作店的销售达成读的就是云商销售明细
        #   （`erp_sales`）—— 恰恰是最需要它的那一步。
        # ⚠⚠ 判据是"**还有没有这家店能跑的步骤**"，不是"达成跳过没" ——
        #   2026-09-20 加了自动更新之后，`daily --steps autoupdate` 会被老判据
        #   误判成"没活干"直接早退 ⇒ **那一趟的自动更新永远不会发生**。
        #   （历史：这张白名单 2026-09-21 补 `report`、2026-09-22 补
        #    `benefit`/`film`，漏一个就是"点名跑了却静默早退" ——
        #    协议 v2 后判据从声明派生：`partner_ok=True` 且没被跳过。）
        _left = [_s for _s in STEPS
                 if step_decl(_s).partner_ok and not skip_of(args, _s)]
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
    #   ⭐ 所有步骤统一声明 `Step(run=...)`（协议 v2）；流程编排不再保留
    #     按命令名手写的第二套路由表。

# ---------------------------------------------------------------- 6b
# **防护膜达成** —— 2026-10-02 起走**注册表执行入口**（协议 v2 试点）：
# 声明在 `features/valueadd/film/__init__.py` 的 `Step(run=step_run)`，
# 由 `_make_registry_runner` 统一派发。
# ⚠ 历史注释保留：页面是**现算**的；这一步只是留痕 + 给 timer 一个可注册的钩子。
# ⚠ `default=False`：不进每天双击那趟 —— 要跑就在「增值 › 设置」里定时刻。

# ---------------------------------------------------------------- 7/8/9
# **上报 / 收取 / 自动更新** —— 2026-10-02 起走**注册表执行入口**（协议 v2）：
# 声明在 `registry.BUILTIN_STEPS` 的 `Step(run=_run_report / _run_report_inbox /
# _run_autoupdate)`；autoupdate 另有 `record=False`（不进汇总，历史规矩）。

    #: 点名的步骤 → 跑它的那个小函数（键就是注册表里的 `cmd`）。
    #: 所有步骤都由注册表执行入口（`Step.run`，协议 v2）派发；
    #:   没有声明执行入口才报"还没接进日常流程"。
    #: 这几步失败 ⇒ **中止后面**（后面的步骤读的就是它们写进去的库）——
    #: ⭐ 判据来自注册表声明（`Step.fatal`，协议 v2），原来这张写死的集合删了。
    _fatal = {s.cmd for s in _registry.all_steps() if s.fatal}

    for _cmd in list(args.only_steps):
        if not runtime.step_available(_cmd, cli.ROOT, recurring=bool(args.wake_slot)):
            print("运行入口已改变，停止后续步骤：%s" % _cmd, file=sys.stderr)
            return cli.EXIT_USAGE
        if skip_of(args, _cmd):
            continue            # 合作店那三步会被按掉（见上面那段）
        _decl = step_decl(_cmd)
        _fn = (_make_registry_runner(_decl, done, args.config, args)
               if _decl is not None and _decl.run is not None else None)
        if _fn is None:
            print("⚠ 「%s」还没有接进日常流程 —— 这趟不跑它"
                  % STEP_LABELS.get(_cmd, _cmd), file=sys.stderr)
            continue
        _rc = _fn()
        if _rc and _cmd in _fatal:
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
