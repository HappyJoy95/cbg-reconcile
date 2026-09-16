"""日常流程 —— **一条定时任务跑完三步**（用户 2026-09-16 定的）。

    1. 华为：拉**当月**订单 → 补进 out/cbg-<年>.db（**取并集**）
    2. 云商：拉**当日**销售明细 → 报量对账（华为侧**从库里读**）
    3. POS 合规：库 → 月度分数 → 落 out/pos-<年>.json

## ⚠ 第 1 步失败 ⇒ **跳过第 2、3 步，直接报错**（用户定的）

为什么不"接着跑、只是标个警告"：

报量对账的华为侧现在从库读。库要是没抓到今天的单，差集会把当天**所有**销售
都算成「未报量」—— 一份**完全错误的清单**，而且**看着很合理**，
门店会照着它去补报一批假的。

**宁可今天没有报告，也不要发一份假的。**（第 2 步失败不跳第 3 步 ——
POS 只读库，跟云商没关系，照算。）

## 关于锁

⚠ 保护的是**云商**不是华为（`check.lock` 的报错原文是「云商不能并行登录」）。
第 1 步（华为）和第 3 步（算分）都不需要锁；`check` 自己会抢。
三步在同一个进程里串行，所以不会自己撞自己；
但**手动跑 check 会和这条定时任务撞** —— 那由 `check` 自己那把锁挡住。
"""

from __future__ import annotations

import argparse
import sys
import time

from . import cli


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="日常流程：抓华为当月 → 对账 → 算 POS")
    ap.add_argument("-c", "--config", default=str(cli.DEFAULT_CONFIG))
    ap.add_argument("--date", default="", help="对账目标日 YYYY-MM-DD（默认用 --days-ago）")
    ap.add_argument("--days-ago", type=int, default=1, help="目标日 = 今天往前 N 天，默认 1（昨天）")
    ap.add_argument("--lookback", type=int, help="覆盖配置：销售窗口往前多看几天")
    ap.add_argument("--lookahead", type=int, help="覆盖配置：华为窗口往后多看几天")
    ap.add_argument("--no-mail", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-refresh", action="store_true",
                    help="会话失效时别开浏览器静默续期（第 1 步会直接失败）")
    ap.add_argument("--skip-dump", action="store_true", help="别抓华为（调试用）")
    ap.add_argument("--skip-pos", action="store_true", help="别算 POS（调试用）")
    ap.add_argument("--log-file", default="", help="把对账那段同时写一份到文件")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    t0 = time.time()
    print("=" * 64)
    print("日常流程 1) 抓华为当月 → 2) 报量对账 → 3) POS 合规")
    print("=" * 64)

    # ---------------------------------------------------------------- 1
    if args.skip_dump:
        print("\n[1/3] 抓华为当月：**已用 --skip-dump 跳过**（调试模式，别在定时任务里用）")
    else:
        # ⚠ **库里一片空白 ⇒ 这是第一次跑**（刚装、或者刚从 1.x 升上来）⇒ 抓全量。
        #   只抓当月的话，POS 页**只有当月一个数、前面几个月全是空的** ——
        #   而"历史几个月的合规率"恰恰是这个看板最要紧的东西。
        #   补这一次之后，以后每天都是当月增量（取并集，不会重复）。
        first_time = not cli._find_pos_db()
        if first_time:
            print("\n[1/3] 抓华为：**本地还没有订单库** —— 这是第一次跑，"
                  "抓**全部历史**补上（只补这一次，以后每天抓当月增量）")
        else:
            print("\n[1/3] 抓华为当月 → 补进订单库（取并集，不删旧行）")
        rc = cli.cmd_dump(argparse.Namespace(config=args.config, month="",
                                             all=first_time,
                                             no_refresh=args.no_refresh,
                                             verbose=args.verbose))
        if rc != 0:
            print("\n" + "=" * 64, file=sys.stderr)
            print(f"❌ 第 1 步失败（退出码 {rc}）—— **跳过第 2、3 步，什么都不发**。",
                  file=sys.stderr)
            print("   为什么不接着跑：库覆盖不了今天的窗口，差集会把当天**所有**销售",
                  file=sys.stderr)
            print("   算成「未报量」—— 一份完全错误的清单，而且看着很合理，",
                  file=sys.stderr)
            print("   门店会照着去补报一批假的。**宁可今天没有报告。**", file=sys.stderr)
            print("   先解决第 1 步（多半是华为会话过期）：", file=sys.stderr)
            print(f"     python -m src.cli -c {args.config} dump", file=sys.stderr)
            print("=" * 64, file=sys.stderr)
            return rc

    # -------------------------------------------------------------- 2 + 3a
    print("\n[2/3] 报量对账：云商当日销售 ↔ **库里的**华为报量")
    rc2 = cli.cmd_check(argparse.Namespace(
        config=args.config, date=args.date or None,
        days_ago=None if args.date else args.days_ago,
        lookback=args.lookback, lookahead=args.lookahead,
        no_refresh=True,                 # ⚠ 已经不碰华为了，续期那套用不上
        no_mail=args.no_mail, no_push=args.no_push,
        out_dir=args.out_dir or None, log_file=args.log_file or None,
        verbose=args.verbose))
    # 0 = 无差异、3 = 有差异，**两个都算跑通**；1/2/9 才是失败
    ok2 = rc2 in (cli.EXIT_OK, cli.EXIT_DIFF)
    if not ok2:
        print(f"\n⚠ 第 2 步（报量对账）没跑通：退出码 {rc2}"
              f"（{'会话/库问题' if rc2 == cli.EXIT_FETCH else '见上'}）", file=sys.stderr)
        print("  **但第 3 步照跑** —— POS 只读库，跟云商没关系。", file=sys.stderr)

    # ------------------------------------------------------------------ 3b
    rc3 = cli.EXIT_OK
    if args.skip_pos:
        print("\n[3/3] POS 合规：**已用 --skip-pos 跳过**")
    else:
        print("\n[3/3] POS 合规率 → out/pos-<年>.json")
        rc3 = cli.cmd_pos(argparse.Namespace(db=""))
        if rc3 != cli.EXIT_OK:
            print(f"\n⚠ 第 3 步（POS）没跑通：退出码 {rc3}", file=sys.stderr)

    print("\n" + "=" * 64)
    print("日常流程结束：用了 %.1f 秒（对账退出码 %d，POS 退出码 %d）"
          % (time.time() - t0, rc2, rc3))
    print("=" * 64)
    # 退出码取第一个不成功的 —— 计划任务那边只看得到这一个数
    for rc in (rc2, rc3):
        if rc not in (cli.EXIT_OK, cli.EXIT_DIFF):
            return rc
    return rc2 if rc2 == cli.EXIT_DIFF else cli.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
