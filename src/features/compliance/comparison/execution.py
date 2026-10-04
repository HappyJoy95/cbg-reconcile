"""双平台数据对比的完整执行链：读库、出清单、记历史并按配置推送。"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

from .... import dump as dumpmod
from .... import pools as P, pools_history, pools_notify
from ....integrations import mailer, wecom
from ....app.pos import find_db
from ....config_io import load_raw
from ....modules import notify
from ....paths import ROOT


def run(ctx) -> int:
    """执行 `pools` 步骤；所有文件和数据库都相对本次运行根目录。"""
    root = Path(ctx.root) if getattr(ctx, "root", None) is not None else ROOT
    args = getattr(ctx, "args", None)
    config = getattr(ctx, "config", "") or "config/store-SCN231409.yaml"
    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = root / config_path
    cfg = load_raw(config_path)

    db = find_db(root) or dumpmod.year_db(
        root / "out", datetime.date.today().year)
    conn = dumpmod.connect(str(db))
    try:
        P.ensure(conn)
        dumpmod._run_migrations(conn)
        return _compare(root, db, conn, cfg, config, args)
    finally:
        conn.close()


def _compare(root, db, conn, cfg, config, args) -> int:
    print("库：%s" % db)
    print()
    print("%-20s %-30s %s" % ("池", "说明", "行数"))
    print("-" * 70)
    for label, note, n in P.status(conn):
        print("%-20s %-30s %d" % (label, note, n))

    q = P.quadrants(conn)
    print()
    print("四象限（各池取最新快照）")
    print("-" * 70)
    print("  池A 玲珑销售单 %6d      池B 玲珑在库 %6d" % (q["A"], q["B"]))
    print("  池C 云商销售单 %6d      池D 云商在库 %6d" % (q["C"], q["D"]))
    print()
    print("  AC 都卖了                    %6d" % q["AC"])
    print("  BD 都没卖                    %6d" % q["BD"])
    print("  ★ AD 玲珑报了、云商没报        %6d" % q["AD"])
    print("  ★ BC 云商报了、玲珑没报        %6d" % q["BC"])
    if q.get("BC_样机"):
        print("      └ 另有 %d 台是样机，已排除"
              "（云商卖了样机、玲珑那边报不了量，属于已知的正常情况）"
              % q["BC_样机"])
    if not getattr(args, "quiet", False):
        print()
        print("  只在 A %d / 只在 B %d / 只在 C %d / 只在 D %d"
              % (q["only_A"], q["only_B"], q["only_C"], q["only_D"]))
        print("  （「只在其中一个」大多是礼品/别的渠道/别的门店，不用管；"
              "⚠ 但「只在 B」里可能藏着窗口没覆盖到的漏报）")

    for quad in ("AD", "BC"):
        rows = P.details(conn, quad)
        if not rows:
            continue
        print()
        print("★ %s %s：%d 台" % (quad, P.QUADRANT_LABELS[quad], len(rows)))
        print("-" * 70)
        for row in rows:
            print("  串号 %s" % row["sn"])
            for key, label in P.DETAIL_COLS:
                if key in ("sn", "direction", "问题"):
                    continue
                value = row.get(key)
                if value not in (None, ""):
                    print("      %-12s %s" % (label, value))

    xlsx, counts = P.export_xlsx(
        conn, db.parent / ("双平台数据对比-%s.xlsx" % P.today()))
    print()
    print("清单已出：%s（AD %d 台 / BC %d 台）"
          % (xlsx, counts["AD"], counts["BC"]))
    history = pools_history.save_day(
        root, P.today(),
        {key: q.get(key, 0) for key in ("AD", "BC", "AC", "BD", "BC_样机")},
        P.details(conn, "AD"), P.details(conn, "BC"))
    print("历史已记：%s" % history)
    _maybe_push(root, cfg, config, conn, xlsx, counts, args)
    return 0


def _maybe_push(root, cfg, config, conn, xlsx, counts, args) -> None:
    """发送比较结果；只有至少一条实际发送成功后才更新推送记忆。"""
    ctx = {"门店": cfg.get("store_name") or cfg.get("store_code") or "?",
           "配置文件": str(config)}
    sns = [row["sn"] for quad in ("AD", "BC") for row in P.details(conn, quad)]
    state = pools_notify.load(root)
    marks = pools_notify.annotate(sns, state) if sns else {}
    n_new = sum(1 for mark in marks.values() if mark.get("new"))
    head = "双平台数据对比：AD %d 台 / BC %d 台" % (counts["AD"], counts["BC"])
    if sns:
        head += "，其中新出现 %d 台" % n_new
    lines = P.notify_lines(conn, marks=marks) or ["本次没有差异 —— 两边都对得上。"]
    has_diff = bool(counts["AD"] or counts["BC"])
    sent_ok = False

    if not getattr(args, "no_push", False):
        try:
            wc = wecom.load_wecom_config(cfg, root)
            ok, why = wecom.should_send(wc, has_diff=has_diff, ignore_when=True)
            if not ok:
                print("[推送] 双平台企微：跳过（%s）" % why)
            else:
                result = notify.send(
                    "wecom", {"template": "pools", "ctx": ctx, "lines": lines,
                              "head": head, "xlsx": xlsx}, cfg=cfg, root=root)
                print("[推送] 双平台企微：%s %s"
                      % ("✅" if result["ok"] else "❌", result["why"]),
                      file=sys.stdout if result["ok"] else sys.stderr)
                sent_ok = result["ok"]
        except Exception as exc:                            # noqa: BLE001
            print("[推送] 双平台企微：❌ %s" % exc, file=sys.stderr)
            print("      （清单已经算好了，退出码不受影响）", file=sys.stderr)

    if not getattr(args, "no_mail", False):
        try:
            mc = mailer.load_mail_config(cfg, root)
            ok, why = mailer.should_send(mc, has_diff=has_diff, ignore_when=True)
            if not ok:
                print("[推送] 双平台邮件：跳过（%s）" % why)
            else:
                subject, body = mailer.build_pools_mail(
                    ctx, lines, head, has_attach=True)
                result = notify.send(
                    "mail", {"subject": subject, "body": body,
                             "attachments": [str(xlsx)],
                             "prefix": mailer.POOLS_SUBJECT_PREFIX},
                    cfg=cfg, root=root)
                print("[推送] 双平台邮件：%s %s"
                      % ("✅" if result["ok"] else "❌", result["why"]),
                      file=sys.stdout if result["ok"] else sys.stderr)
                sent_ok = sent_ok or result["ok"]
        except Exception as exc:                            # noqa: BLE001
            print("[推送] 双平台邮件：❌ %s" % exc, file=sys.stderr)
            print("      （清单已经算好了，退出码不受影响）", file=sys.stderr)

    if sent_ok:
        pools_notify.save(root, pools_notify.remember(state, sns, P.today()))
        print("[推送] 已记住 %d 个串号（下次再出现会弱化并标上次日期）"
              % len(sns))
    elif sns:
        print("[推送] 记忆未更新（这一条没真发出去）", file=sys.stderr)
