"""POS 合规的**执行模块** —— 算、落盘、推送，一次做完。

## 依赖方向（开发目标 §4.5.4）

这个模块**不 import CLI / HTTP**：

| 入口 | 它只负责 |
|---|---|
| `cli.cmd_pos` | 解析参数、打日志、把结果翻成退出码 |
| `run_daily` | 顺序与失败依赖（跑完这步再跑下一步） |
| 控制台（`web.py`） | **只读**落盘的 `out/pos-<年>.json`，不现场算 |

⇒ 三个入口看到的是**同一个执行模块、同一份口径**（阶段 2 的 2.1/2.2）。

## ⚠ 为什么要抽出来

这条链原来整段长在 `cmd_pos` 里（80 行），而 `run_daily` 为了复用它，
**手工拼了一个 `argparse.Namespace(db=…)`** —— 2026-09-19 那条
「daily 每天算了 POS，却从来没推过 POS」的缺口就是这么来的：
只传了 `db`，漏了 `config` 和两个开关，而 `_maybe_pos_push` 第一句就是
`if not config_path: return`，于是静默跳过（1410 条测试一条都没抓到，
因为它们把 `cmd_pos` 整个 mock 掉了）。

⇒ 现在的契约是**关键字参数 + 具名结果**：漏一个参数在调用点就看得出来，
而"推没推"写在 `PosRun.notices` 里，**能被断言**，不用去读日志。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..features.compliance.pos import pos_metric as pm, pos_report
from ..paths import ROOT

#: 通知的四种结局（阶段 2 的 2.4 要求**各有断言**，不是只有"成功/失败"两种）
SENT = "sent"            # 真发出去了
SKIPPED = "skipped"      # 没配置 —— "没配邮箱"和"配了但关了"是两件事，别混
DISABLED = "disabled"    # 主动关掉（`--no-push` / 设置里的开关）
FAILED = "failed"        # 发出去了但失败（网络、SMTP 拒绝…）


@dataclass
class PosRun:
    """一次 POS 执行的**结果契约**（三个入口共用）。

    ⚠ `ok=False` 一定带 `why`（给人看的一句话）；`notices` 只在**算出来了**之后才有内容 ——
    "算都没算成"和"算成了但没推出去"必须分得开。
    """

    ok: bool = False
    why: str = ""
    db: str = ""                  # 相对项目根（落盘的 JSON 里也是这个口径）
    year: int = 0
    out_path: str = ""
    orders: int = 0
    returns: int = 0
    rows: list = field(default_factory=list)
    #: `{"wecom": {"state": …, "why": …}, "mail": {…}}`
    notices: dict = field(default_factory=dict)

    def state(self, channel: str) -> str:
        return (self.notices.get(channel) or {}).get("state", "")

    def why_of(self, channel: str) -> str:
        return (self.notices.get(channel) or {}).get("why", "")


def find_db(root=None) -> Optional[Path]:
    """`out/` 里最新的那个 `cbg-<年>.db`。一年一个库，取年份最大的。"""
    d = Path(root or ROOT) / "out"
    if not d.is_dir():
        return None
    cands = sorted(d.glob("cbg-[0-9][0-9][0-9][0-9].db"))
    return cands[-1] if cands else None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))


def compute(db, *, root=None) -> PosRun:
    """**只算不推**：读库 → 逐月算 → 落 `out/pos-<年>.json`。

    ⚠ 落盘里的 `db` 走**相对项目根**的写法 —— 别把开发机的绝对路径带进门店的报告里。
    """
    root = Path(root or ROOT)
    db = Path(db)
    if not db.is_file():
        return PosRun(ok=False, why=f"没找到订单库：{db}",
                      db=str(db))
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        # 一个读事务同时覆盖指标读取和门店身份快照；避免订单库在两次查询之间
        # 更新，导致汇总与授权证明不对应。
        conn.execute("BEGIN")
        orders, returns = pos_report.load(conn)
        source_stores = pos_report.store_identities(conn)
        conn.commit()
    finally:
        conn.close()

    months = pm.months_of(orders, returns)
    # ⚠ 「暂定」：口径是「退货在退货当月扣减」⇒ **前一个月的分数还会被这个月的退货改**。
    #   所以最近两个月标暂定，免得两个月后有人拿旧报表对不上账。
    def provisional(m):
        return m >= months[-2] if len(months) >= 2 else True

    rows = []
    for m in months:
        row = {"month": m, "provisional": provisional(m)}
        for by in pm.BOTH:
            cur = pm.score_month(orders, returns, m, by)
            ap = pm.score_month(orders, returns, m, by, exclude_team=True)
            row[by] = {"den": round(cur.den, 2), "num": round(cur.num, 2), "rate": cur.rate,
                       "orders": cur.orders, "cut_den": round(cur.cut_den, 2),
                       "ap_den": round(ap.den, 2), "ap_num": round(ap.num, 2),
                       "ap_rate": ap.rate}
            # ⭐ **官方那套**（PPT《POS合规：计算逻辑及方法》，2026-09-21 接进来）：
            #   现金+记账 扣减、**不扣退货**、**先按天算再取日均值**。
            #   上面那个 `rate` 是我们自己的口径（非现金/全部、扣退货、整月汇总）——
            #   **两个都留着**：官方那个跟财经的成绩对得上，我们那个是申诉时看惯的数。
            row.setdefault("official", {})[by] = pm.score_month_official(
                orders, returns, month=m, by=by)
            row["official"]["ap_" + by] = pm.score_month_official(
                orders, returns, month=m, by=by, exclude_team=True)["rate"]
        rows.append(row)

    year = int(months[-1][:4]) if months else _now().year
    out = root / "out" / ("pos-%d.json" % year)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_rel = str(db.relative_to(root))
    except ValueError:
        db_rel = str(db)
    out.write_text(json.dumps({
        "generated_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "year": year, "db": db_rel, "orders": len(orders), "returns": len(returns),
        "source_stores": source_stores,
        "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    return PosRun(ok=True, db=db_rel, year=year, out_path=str(out),
                  orders=len(orders), returns=len(returns), rows=rows)


def _ctx(cfg: dict) -> dict:
    """推送的上下文 —— 字段名跟报量排查那边一致（`门店`/`生成时间`…），
    这样 `wecom` / `mailer` 两套组装不用为"POS 版"再分一次支。
    """
    return {
        "门店": cfg.get("erp_store_name") or cfg.get("store_code") or "?",
        "华为门店编码": cfg.get("store_code") or "",
        "串号标识": cfg.get("marker") or "",
        "生成时间": _now().strftime("%Y-%m-%d %H:%M:%S"),
        "配置文件": cfg.get("_path") or "",
    }


def _notice(r: dict) -> dict:
    """把 `notify.send` 的返回值翻成 `PosRun.notices` 里那一格。

    ⚠ 只映射 `sent` / `failed` 两种 —— `disabled`（开关关了）和 `skipped`
    （没配置）是**业务**的判断，推送模块根本不认识这两个状态。
    """
    return {"state": SENT if r.get("ok") else FAILED, "why": r.get("why") or ""}


def notify(res: PosRun, *, config_path=None, no_push=False, no_mail=False,
           root=None, emit=None) -> PosRun:
    """把结果推出去（企微 / 邮件各一条），把**结局**写进 `res.notices`。

    ⚠ 这里**刻意不复用**报量排查那两条推送函数：那两条是围着 `ReconcileResult`
    长的（missing / unshipped / xlsx 附件），硬塞进 POS 只会长出一堆 `if`。
    共用的只有**格式化**（`pos_report.notify_lines`）和**发送**
    （`wecom.push_pos` / `mailer.build_pos_mail`）。

    ⚠ 推送失败**不影响 `res.ok`** —— 分数是主产物，推送只是投递方式。
    """
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    # ⭐ 业务开关（用户 2026-09-22）：模块设置页里「POS 合规」推不推
    from ..modules.notify import prefs as push_prefs
    off = push_prefs.why_off("pos", root)
    if off:
        res.notices = {"wecom": {"state": DISABLED, "why": off},
                       "mail": {"state": DISABLED, "why": off}}
        say("\n[推送] POS：跳过（%s）" % off)
        return res
    if no_push and no_mail:
        res.notices = {"wecom": {"state": DISABLED, "why": "已用 --no-push --no-mail 跳过"},
                       "mail": {"state": DISABLED, "why": "已用 --no-push --no-mail 跳过"}}
        say("\n[推送] POS：已用 --no-push --no-mail 跳过")
        return res
    if not config_path:
        # ⚠ 这一句就是 2026-09-19 那个缺口的现场：没有配置就**静默**返回了，
        #   调用方以为推过了。现在它是一条**能被断言的**记录。
        res.notices = {"wecom": {"state": SKIPPED, "why": "没有配置（config 没传）"},
                       "mail": {"state": SKIPPED, "why": "没有配置（config 没传）"}}
        return res

    from .. import config_io, mailer, wecom      # 延迟 import：算分那条路不需要它们
    from ..modules import notify as push         # ⚠ 别名：本模块自己也有个 `notify()`

    # ⚠ 用 `config_io.load_raw`（读不到就返回 `{}`），**不要** `cli.load_config`：
    #   ① 那是入口层的函数，app 不许依赖它；
    #   ② 它找不到文件时抛的是 **`SystemExit`**，而 `SystemExit` 是 `BaseException` ——
    #      老代码的 `except Exception` 接不住它，配置一丢就把整条 `cmd_pos` 带崩
    #      （分数明明已经算好了）。这里退化成一条 `skipped` 记录，稳。
    p = Path(config_path)
    if not p.is_absolute():
        p = root / p
    try:
        cfg = config_io.load_raw(p) or {}
    except Exception as e:                                    # noqa: BLE001
        why = f"配置读不出来（{e}）"
        res.notices = {"wecom": {"state": SKIPPED, "why": why},
                       "mail": {"state": SKIPPED, "why": why}}
        say(f"\n[推送] POS：⚠️ {why}，跳过")
        return res
    if not cfg:
        why = f"配置不存在或读不到：{p}"
        res.notices = {"wecom": {"state": SKIPPED, "why": why},
                       "mail": {"state": SKIPPED, "why": why}}
        say(f"\n[推送] POS：⚠️ {why}，跳过")
        return res
    cfg.setdefault("_path", str(p))

    ctx = _ctx(cfg)
    lines = pos_report.notify_lines(res.rows)
    head = pos_report.headline(res.rows)

    if no_push:
        res.notices["wecom"] = {"state": DISABLED, "why": "--no-push"}
    else:
        try:
            wc = wecom.load_wecom_config(cfg, root)
            # ⚠ `ignore_when=True`：POS 没有"差异"概念，
            #   「只有差异才推」那个开关是给报量排查的
            ok, why = wecom.should_send(wc, has_diff=False, ignore_when=True)
            if not ok:
                res.notices["wecom"] = {"state": DISABLED, "why": why}
                say(f"\n[推送] POS 企微：跳过（{why}）")
            else:
                # ⚠ **发出去**这一步交给推送模块（`modules.notify`）—— 它管
                #   「收到渠道编码 + 内容 → 发出去」和「记录各个推送渠道」；
                #   而**上面那两句 `should_send` 就是"该不该推"，留在业务这边**
                #   （用户 2026-09-19 的分工：业务最清楚自己该不该推）。
                r = push.send("wecom", {"template": "pos", "ctx": ctx,
                                      "lines": lines, "head": head},
                              cfg=cfg, root=root)
                res.notices["wecom"] = _notice(r)
                say(f"\n[推送] POS 企微：{'✅' if r['ok'] else '❌'} {r['why']}")
        except Exception as e:                                # noqa: BLE001
            res.notices["wecom"] = {"state": FAILED, "why": str(e)}
            say(f"\n[推送] POS 企微：❌ {e}")
            say("      （分数已经算好了，退出码不受影响）")

    if no_mail:
        res.notices["mail"] = {"state": DISABLED, "why": "--no-mail"}
    else:
        try:
            mc = mailer.load_mail_config(cfg, root)
            ok, why = mailer.should_send(mc, has_diff=False)
            if not ok:
                res.notices["mail"] = {"state": DISABLED, "why": why}
                say(f"[推送] POS 邮件：跳过（{why}）")
            else:
                subject, body = mailer.build_pos_mail(ctx, lines, head)
                r = push.send("mail", {"subject": subject, "body": body,
                                       "prefix": mailer.POS_SUBJECT_PREFIX},
                              cfg=cfg, root=root)
                res.notices["mail"] = _notice(r)
                say(f"[推送] POS 邮件：{'✅' if r['ok'] else '❌'} {r['why']}")
        except Exception as e:                                # noqa: BLE001
            res.notices["mail"] = {"state": FAILED, "why": str(e)}
            say(f"[推送] POS 邮件：❌ {e}")
            say("      （分数已经算好了，退出码不受影响）")
    return res


def run(*, db="", config_path=None, no_push=False, no_mail=False,
        root=None, emit=None) -> PosRun:
    """算 + 落盘 + 推送，一次做完并返回**具名结果**。

    * `db=""` ⇒ 自己找 `out/` 里最新的那个库；
    * `emit` 是"要说的话往哪儿写"（CLI 传 `print`，进每天的日志；不传就安静）；
    * ⚠ 找不到库时返回 `ok=False`（**不抛异常**）—— 三个入口都要能自己决定
      "这算失败还是跳过得说一句"。
    """
    say = emit or (lambda _s: None)
    root = Path(root or ROOT)
    path = Path(db) if db else find_db(root)
    if not path or not path.is_file():
        return PosRun(ok=False, why="没找到订单库（out/cbg-<年>.db）")

    res = compute(path, root=root)
    if not res.ok:
        return res
    say(f"POS 合规 → {res.out_path}")
    for line in pos_report.console_lines(res.rows):
        say(line)
    return notify(res, config_path=config_path, no_push=no_push, no_mail=no_mail,
                  root=root, emit=emit)
