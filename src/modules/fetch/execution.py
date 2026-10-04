"""抓取步骤与数据池采集的执行编排。命令行只负责提供入口服务。"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

from ...paths import ROOT

EXIT_OK, EXIT_AUTH, EXIT_FETCH = 0, 1, 2
CST = datetime.timezone(datetime.timedelta(hours=8))


def _services(ctx):
    services = getattr(ctx, "services", None)
    if not isinstance(services, dict):
        raise RuntimeError("抓取执行入口缺少服务适配器")
    return services


def run_dump(ctx) -> int:
    """执行华为订单与玲珑库存抓取；保留初次建库和 Lifehall 分支语义。"""
    services = _services(ctx)
    root = Path(getattr(ctx, "root", None) or ROOT)
    args = getattr(ctx, "args", None)
    config = getattr(ctx, "config", "") or "config/store-SCN231409.yaml"
    cfg = services["load_config"](config)
    session = services["session_path"](cfg)
    if not session.is_file():
        print("❌ 没有华为会话：%s\n   先跑一次：python -m src.cli -c %s auth --auto"
              % (session, config), file=sys.stderr)
        return EXIT_AUTH

    no_refresh = bool(getattr(args, "no_refresh", False))
    verbose = bool(getattr(args, "verbose", False))
    _, rc = services["require_session"](
        cfg, verbose=verbose, allow_refresh=not no_refresh)
    if rc is not None:
        return rc

    argv = ["--session", str(session)]
    code = (cfg.get("store_code") or "").strip()
    if code:
        argv += ["--store-code", code]
    first_time = bool(getattr(ctx, "daily_step", False)
                      and not services["find_pos_db"]())
    year = datetime.date.today().year
    if first_time:
        print("\n[1/9] 抓取玲珑数据：**本地还没有订单库** —— 这是第一次跑，"
              "抓**今年（%d）至今**的全量补上（只补这一次，以后每天抓当月增量）"
              % year)
    elif getattr(ctx, "daily_step", False):
        print("\n[1/9] 抓取玲珑数据：华为当月 → 补进订单库（取并集，不删旧行）")

    requested_year = getattr(args, "year", 0) if args is not None else 0
    requested_all = bool(getattr(args, "all", False)) if args is not None else False
    requested_month = getattr(args, "month", "") if args is not None else ""
    if first_time:
        argv += ["--year", str(year)]
    elif requested_year:
        argv += ["--year", str(requested_year)]
    elif requested_all:
        argv.append("--all")
    else:
        argv += ["--month", requested_month or "current"]

    started = datetime.datetime.now(CST)
    from ... import dump as dumpmod
    try:
        rc = dumpmod.main(argv)
    except Exception as exc:                                 # noqa: BLE001
        services["record_fetch"](
            "dump", False, why="%s: %s" % (type(exc).__name__, exc), started=started)
        raise
    services["record_fetch"](
        "dump", rc == 0,
        why="" if rc == 0 else "退出码 %d（看上面的报错）" % rc,
        started=started)
    if rc != 0:
        if getattr(ctx, "daily_step", False):
            print("\n" + "=" * 64, file=sys.stderr)
            print("❌ 第 1 步失败（退出码 %d）—— **跳过后续分析，什么都不发**。"
                  % rc, file=sys.stderr)
            print("   抓取失败后继续会拿旧库计算，生成一份看似合理的错误清单。",
                  file=sys.stderr)
            print("   先解决第 1 步（多半是华为会话过期）：", file=sys.stderr)
            print("     python -m src.cli -c %s dump" % config, file=sys.stderr)
            print("=" * 64, file=sys.stderr)
        return rc

    from ..auth import runtime
    if runtime.is_lifehall(root):
        print("生活馆版：已完成华为订单抓取，跳过完整版库存对比池")
        return EXIT_OK

    print()
    print("— 顺带抓玲珑在库（池B）—")
    pool_args = argparse.Namespace(
        config=config, fetch=["lg-stock"], start="", end="", date="",
        days_ago=0, no_refresh=True, no_push=True, no_mail=True, verbose=verbose)
    return run_pool_fetch(pool_args, services=services, root=root)


def run_erp_dump(ctx) -> int:
    """执行云商库存和销售抓取；单池成功落库不因另一池失败而回滚。"""
    services = _services(ctx)
    root = Path(getattr(ctx, "root", None) or ROOT)
    args = getattr(ctx, "args", None)
    config = getattr(ctx, "config", "") or "config/store-SCN231409.yaml"
    verbose = bool(getattr(args, "verbose", False))
    if getattr(ctx, "daily_step", False):
        print("\n[2/9] 抓取云商数据：云商在库 + 云商销售明细 → 补进订单库")
    print("— 抓云商数据（云商在库 池D / 云商销售 池C）—")
    pool_args = argparse.Namespace(
        config=config, fetch=["erp-stock", "erp-sales"], start="", end="",
        date="", days_ago=0, no_refresh=True, no_push=True, no_mail=True,
        verbose=verbose)
    rc = run_pool_fetch(pool_args, services=services, root=root)

    try:
        from ...app import data_state
        state = {item["key"]: item for item in data_state.data_state(root).get("sources", [])}
        for key, label in (("erp-stock", "池D 云商在库"),
                           ("erp-sales", "池C 云商销售")):
            one = state.get(key) or {}
            ok = one.get("state") == "ok"
            print("   %s %s：%s%s" % (
                "✓" if ok else "✗", label, one.get("state_label") or "?",
                "" if ok else "（%s）" % (one.get("why") or "")))
    except Exception as exc:                                 # noqa: BLE001
        print("   （两个池各自的状态没读出来：%s: %s）" % (type(exc).__name__, exc))
    if rc != 0:
        if getattr(ctx, "daily_step", False):
            print("\n" + "=" * 64, file=sys.stderr)
            print("❌ 第 2 步（抓云商数据）失败（退出码 %d）—— "
                  "**跳过后面的分析，什么都不发**。" % rc, file=sys.stderr)
            print("   云商数据不完整时，双平台对比和销售分析都不可信。",
                  file=sys.stderr)
            print("   先单独试一次：python -m src.cli -c %s erp-dump" % config,
                  file=sys.stderr)
            print("=" * 64, file=sys.stderr)
        print("   ⚠ 整步算失败（后面那几步要用**这两个池**）——"
              "但上面 ✓ 的那个池**数据已经更新了**，✗ 的那个没进来。")
    return rc


def run_pool_fetch(args, *, services, root=None) -> int:
    """采集指定池；成功池分别落库，失败池分别记录尝试并保留退出码。"""
    from ... import dump as dumpmod, pools as poolmod

    root = Path(root or ROOT)
    db = services["find_pos_db"]() or dumpmod.year_db(
        root / "out", datetime.date.today().year)
    conn = dumpmod.connect(str(db))
    try:
        poolmod.ensure(conn)
        dumpmod._run_migrations(conn)
        try:
            cfg = services["load_config"](args.config)
        except SystemExit:
            cfg = {}

        rc = 0
        for pool in args.fetch:
            started = datetime.datetime.now(CST)
            try:
                fetch = services["fetchers"].get(pool)
                if fetch is None:
                    print("❌ 不认识的池：%s（可选：%s）" % (
                        pool, " / ".join(poolmod.POOLS)), file=sys.stderr)
                    return 2
                # 玲珑采集器需要门店配置；ERP 采集器原有签名只有连接和参数。
                one = (fetch(cfg, conn, args) if pool == "lg-stock"
                       else fetch(conn, args))
            except Exception as exc:                           # noqa: BLE001
                services["record_fetch"](
                    pool, False, why="%s: %s" % (type(exc).__name__, exc),
                    started=started, conn=conn)
                raise
            services["record_fetch"](
                pool, one == EXIT_OK,
                why="" if one == EXIT_OK else "退出码 %d" % one,
                started=started, conn=conn)
            rc |= one

        purged = poolmod.purge_snapshots(conn)
        hit = {key: value for key, value in purged.items() if value}
        if hit:
            print("快照轮转（保留 %d 天）：%s" % (
                poolmod.SNAP_KEEP_DAYS,
                "、".join("%s 删 %d 行" % pair for pair in hit.items())))
        return rc
    finally:
        conn.close()
