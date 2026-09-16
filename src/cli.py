"""命令行编排。

    python -m src.cli auth  --from-curl curl.txt     导入会话（粘贴的 curl）
    python -m src.cli ping                           会话自检
    python -m src.cli check --date 2026-09-14        跑对账

退出码：0 正常 / 1 会话过期 / 2 取数失败 / 3 有差异（供计划任务判断）
"""

from __future__ import annotations

import argparse
import datetime
import platform
import subprocess
import sys
import time
from pathlib import Path

import yaml

from . import (autostart, browser, config_io, lockfile, mailer, runtime,
               service, version, wecom)
from .cbg import CbgClient, CbgError
from .erp import (ErpCaptchaRequired, ErpClient, ErpError, describe_credentials,
                  effective_env_file, load_credentials)
import json
import sqlite3

from .reconcile import classify_sales, reconcile, sn_row_index
from .report import summary_lines, write_report
from .session import CbgAuthError, CbgSession

ROOT = Path(__file__).resolve().parent.parent
#: 门店配置的默认路径。⚠ **只此一处** —— `run_daily` 也用它，
#: 两处各写一份的话，哪天改了默认值另一处会静默用旧的。
DEFAULT_CONFIG = "config/store-SCN231409.yaml"
CST = datetime.timezone(datetime.timedelta(hours=8))

EXIT_OK, EXIT_AUTH, EXIT_FETCH, EXIT_DIFF, EXIT_INTERNAL = 0, 1, 2, 3, 9


def browser_profile(cfg: dict) -> Path:
    """浏览器 profile 目录 —— 存登录态的地方，日常静默续期靠它。"""
    return browser.profile_path(cfg, ROOT)


# --------------------------------------------------------------------- 配置
def load_config(path: str | Path, root=None) -> dict:
    """读门店配置。

    ⚠ `root` 给 Web 那边用：它手上是**安装目录**（`app.root`），
    不一定等于 `cli.ROOT`（测试里就是临时目录）。
    不传的话按 `ROOT` 解析 —— 跟以前一样。
    """
    base = ROOT if root is None else Path(root)
    p = Path(path)
    if not p.is_absolute():
        p = base / p
    if not p.exists():
        raise SystemExit(f"找不到配置文件 {p}\n"
                         f"（部署到门店电脑时，请改 config/store-*.yaml 里的三行再运行）")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg["_path"] = str(p)

    stores_doc = yaml.safe_load((base / "config" / "stores.yaml").read_text(encoding="utf-8"))
    stores = stores_doc.get("stores") or []
    cfg["_experience"] = {s["erp_name"] for s in stores if s.get("erp_name")}
    cfg["_experience_markers"] = {s["marker"] for s in stores if s.get("marker")}
    cfg["_non_store_markers"] = set(stores_doc.get("non_store_markers") or ())
    doc_types = stores_doc.get("doc_types") or {}
    cfg.setdefault("check", {})
    cfg["check"].setdefault("include_doc_types", doc_types.get("include") or [])
    cfg["check"].setdefault("exclude_doc_types", doc_types.get("exclude") or [])

    own = cfg.get("erp_store_name")
    if own and own not in cfg["_experience"]:
        print(f"[警告] {own!r} 不在 config/stores.yaml 的体验店名单里 —— "
              f"「其他体验店卖出跳过」这条规则会对它失效，请补进名单。", file=sys.stderr)
    return cfg


def session_path(cfg: dict) -> Path:
    rel = (cfg.get("session") or {}).get("file") or f".secrets/cbg-{cfg.get('store_code') or 'default'}.json"
    p = Path(rel)
    return p if p.is_absolute() else ROOT / p


def make_client(cfg: dict, verbose=False) -> CbgClient:
    sess = CbgSession.load(session_path(cfg))
    return CbgClient(sess, store_code=cfg.get("store_code") or None, verbose=verbose)


def ensure_session(cfg: dict, *, verbose=False, allow_refresh=True):
    """拿到一个**能用的**华为客户端；会话失效时先试静默续期。

    返回 `(client, ok, why)`。`client` 在失败时可能是 `None`。

    ⚠ **这段是从 `_run_check` 里原样搬出来的，不是重写的。**
    第 4 步取数改造把华为那套整体挪去了第 1 步（`dump`），当时连着
    "会话失效就静默续期"一起丢掉了 —— 结果就是会话一到期，整条日常流程
    中止到有人手动 `auth --auto`。**报错是对的（用户定的），自愈丢了是另一件事。**
    现在把它挂在第 1 步上，位置才是对的：**华为只在这一步被登录**。

    ⚠ "静默"是真的静默（`headless=True`）：夜里那条定时任务在门店电脑上跑，
    **不许弹浏览器窗口**。失败的会话**不会被覆盖**（`capture_session` 先
    `verify` 再 `save`），所以续期失败不会把好的会话弄坏。
    """
    try:
        client = make_client(cfg, verbose=verbose)
    except CbgAuthError as e:
        client = None
        ok, msg = False, str(e)
    else:
        ok, msg = client.ping()

    auto_refresh = (cfg.get("session") or {}).get("auto_refresh", True)
    if ok or not (auto_refresh and allow_refresh):
        return client, ok, msg

    print(f"华为会话：❌ {msg}")
    print("      会话失效 → 用浏览器 profile 静默续期（自检通过才会覆盖）…")
    store = cfg.get("store_code") or None

    def _verify(s):
        """⚠ 返回 `(过没过, 为什么)`，**别只返回 bool**。

        `ping()` 的第二个返回值里写着真正的病因
        （"会话/权限问题：没有门店或数据范围 XXX 的权限"、"接口异常：…"），
        老写法 `.ping()[0]` 把它扔了，用户最后只看到一句"自检没过" ——
        实测就卡在这儿：只能反复说"就是抓不到"，谁也定位不了。
        """
        try:
            ok2, why2 = CbgClient(s, store_code=store, timeout=25).ping()
            return ok2, why2
        except (CbgAuthError, CbgError) as e:
            return False, f"{type(e).__name__}: {e}"

    try:
        creds = browser.load_login_credentials(cfg, ROOT)
        sess2 = browser.capture_session(browser_profile(cfg), headless=True, timeout=90,
                                        on_step=lambda m: print("        " + m),
                                        verify=_verify,
                                        url=browser.login_url(cfg),
                                        credentials=creds if all(creds) else None)
        sess2.save(session_path(cfg))
        client = CbgClient(sess2, store_code=store, verbose=verbose)
        ok, msg = client.ping()
    except (browser.BrowserError, CbgAuthError) as e:
        print(f"      自动续期没成功：{e}")
        print("      现有会话没有被覆盖。")
    return client, ok, msg


def require_session(cfg: dict, *, verbose=False, allow_refresh=True):
    """`ensure_session` 的"失败就报错并给下一步"版本 —— 返回 `(client, 退出码)`。

    退出码 `None` = 拿到了。收尾提示只此一处，免得每个调用方各写一遍、
    然后各漏一句（老代码里那句"需要人工登录一次"就是这么散的）。
    """
    client, ok, msg = ensure_session(cfg, verbose=verbose, allow_refresh=allow_refresh)
    print(f"华为会话：{'✅' if ok else '❌'} {msg}")
    if ok:
        return client, None
    print("      需要人工登录一次（浏览器里登完会自动抓走，不用再复制 curl）：\n"
          "        python -m src.cli auth --auto", file=sys.stderr)
    return None, EXIT_AUTH


def _day_bounds(day: datetime.date, tz=CST) -> tuple[int, int]:
    a = datetime.datetime.combine(day, datetime.time(0, 0, 0), tzinfo=tz)
    b = datetime.datetime.combine(day, datetime.time(23, 59, 59), tzinfo=tz)
    return int(a.timestamp()), int(b.timestamp())


# ------------------------------------------------------------------- 子命令
def cmd_auth(args) -> int:
    cfg = load_config(args.config)
    profile = browser_profile(cfg)

    if args.auto or args.refresh:
        headless = bool(args.refresh)

        def _verify(s):
            """⚠ 返回 `(过没过, 为什么)`，**别只返回 bool**。

            `ping()` 的第二个返回值里写着真正的病因
            （"会话/权限问题：没有门店或数据范围 XXX 的权限"、"接口异常：…"），
            老写法 `.ping()[0]` 把它扔了，用户最后只看到一句"自检没过" ——
            实测就卡在这儿：只能反复说"就是抓不到"，谁也定位不了。
            """
            try:
                ok, why = CbgClient(s, store_code=cfg.get("store_code") or None, timeout=25).ping()
                return ok, why
            except (CbgAuthError, CbgError) as e:
                return False, f"{type(e).__name__}: {e}"

        creds = browser.load_login_credentials(cfg, ROOT)
        if creds[0] and creds[1]:
            print(f"用已保存的华为账号自动登录：{creds[0]}")
        else:
            print("没配华为账号密码 —— 会打开窗口等你手动登录")
        try:
            sess = browser.capture_session(profile, headless=headless,
                                           timeout=args.timeout,
                                           on_step=lambda m: print("  " + m),
                                           verify=_verify,
                                           url=browser.login_url(cfg),
                                           credentials=creds if all(creds) else None)
        except (browser.BrowserError, CbgAuthError) as e:
            print(f"[失败] {e}", file=sys.stderr)
            print("      现有会话没有被覆盖。", file=sys.stderr)
            return EXIT_AUTH
    elif args.from_curl:
        if args.from_curl == "-":
            text, src = sys.stdin.read(), "stdin"
        else:
            text, src = Path(args.from_curl).read_text(encoding="utf-8", errors="replace"), args.from_curl
        try:
            sess = CbgSession.from_curl(text)
        except CbgAuthError as e:
            print(f"[失败] 解析 curl 失败：{e}", file=sys.stderr)
            return EXIT_AUTH
    else:
        print("要么 --auto（打开浏览器抓），要么 --refresh（静默续期），"
              "要么 --from-curl 文件（手动粘贴）", file=sys.stderr)
        return EXIT_AUTH

    p = sess.save(session_path(cfg))
    print(f"已保存会话 → {p}")
    print(f"  {sess.describe()}")
    client = CbgClient(sess, store_code=cfg.get("store_code") or None)
    ok, msg = client.ping()
    print(("✅ 会话可用：" if ok else "❌ 会话不可用：") + msg)
    return EXIT_OK if ok else EXIT_AUTH


def cmd_ping(args) -> int:
    cfg = load_config(args.config)
    try:
        client = make_client(cfg, verbose=args.verbose)
    except CbgAuthError as e:
        print(f"❌ {e}", file=sys.stderr)
        return EXIT_AUTH
    ok, msg = client.ping()
    print(("✅ " if ok else "❌ ") + msg)
    return EXIT_OK if ok else EXIT_AUTH


def compute_windows(target: datetime.date, lookback: int, lookahead: int):
    """算出销售窗口和华为窗口。抽出来是为了能单测 —— 这两个窗口搞错就会误报。

        销售窗口 = [target - lookback, target]
        华为窗口 = [target - lookback, target + lookahead]

    两个方向的含义不一样：
        lookback  往前多看几天（补查历史漏报）
        lookahead 往后多看几天（容忍跨零点/次日补报）
    """
    sales_start = target - datetime.timedelta(days=max(lookback, 0))
    sales_end = target
    cbg_start = sales_start
    cbg_end = target + datetime.timedelta(days=max(lookahead, 0))
    return sales_start, sales_end, cbg_start, cbg_end


class _Tee:
    """同时写屏幕和文件。

    ⚠ 为什么不是在 bat 里写 `>> out\run.log`：那样**屏幕上什么都没有** ——
    双击 run.bat 的人看到的是一个黑窗口、一分多钟、然后自己关掉，
    完全判断不了到底跑没跑（门店实测就是这么反馈的："什么都没发生"）。
    改成 Python 这边分流：屏幕看得到，日志也留得下。
    """

    def __init__(self, stream, fp):
        self.stream, self.fp = stream, fp

    def write(self, s):
        self.stream.write(s)
        try:
            self.fp.write(s)
        except (OSError, ValueError):
            pass
        # ⚠ **必须每行刷盘**。stdout 重定向到文件时是块缓冲，
        #   进程被 kill / 卡住 / 崩溃时缓冲区整个丢掉 —— 而日志的全部意义
        #   就是"出事之后还能看"。实测踩到：跑了一分钟，日志 0 字节。
        if s.endswith("\n"):
            self.flush()
        return len(s)

    def flush(self):
        for x in (self.stream, self.fp):
            try:
                x.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        try:
            return self.stream.isatty()
        except Exception:                          # noqa: BLE001
            return False

    def __getattr__(self, name):                   # encoding 之类透传
        return getattr(self.stream, name)


def _tee_to(path) -> None:
    """把 stdout/stderr 分一份到 path（追加）。失败就算了，不能让日志拖垮对账。"""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fp = open(p, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"⚠️ 日志打不开（{e}），只在屏幕上输出", file=sys.stderr)
        return
    sys.stdout = _Tee(sys.stdout, fp)
    sys.stderr = _Tee(sys.stderr, fp)


def cmd_check(args) -> int:
    """跑对账。**先抢跨进程锁** —— 云商不能并行登录。

    Web 界面里那把锁只管得住界面自己，管不住计划任务。

    带 `--log-file` 时（计划任务走的就是这条）**开头结尾各写一行标记**，
    结束那行带退出码 —— 界面靠它判断"跑完了没"。
    ⚠ 这行必须由 **Python** 写：run.bat 现在用 `start` 起 pythonw 之后
    立刻退出，它自己拿不到退出码（而这是故意的，见 write_runner_script）。
    """
    log_file = getattr(args, "log_file", None)
    if log_file:
        _tee_to(log_file)
        print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 开始 ===")
    rc = _cmd_check_locked(args)
    if log_file:
        print(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} 结束 exit={rc} ===")
    return rc


def _cmd_check_locked(args) -> int:
    cfg = load_config(args.config)
    lock = lockfile.Lock(ROOT / ".secrets" / "check.lock")
    ok, why = lock.acquire()
    if not ok:
        print(f"❌ {why}", file=sys.stderr)
        print("   云商不能并行登录，等它跑完再试。", file=sys.stderr)
        print(f"   若确认那个进程已经不在了，删掉 {lock.path} 即可。", file=sys.stderr)
        return EXIT_FETCH
    try:
        return _run_check(args, cfg)
    finally:
        lock.release()


def _run_check(args, cfg: dict) -> int:
    check = cfg.get("check") or {}
    marker = cfg.get("marker") or ""
    own_store = cfg.get("erp_store_name") or ""
    if not marker or not own_store:
        print("[失败] 配置里缺 marker 或 erp_store_name", file=sys.stderr)
        return EXIT_FETCH

    if args.date and args.days_ago is not None:
        raise SystemExit("--date 和 --days-ago 只能给一个")
    if args.date:
        target = datetime.date.fromisoformat(args.date)
    elif args.days_ago is not None:
        target = datetime.datetime.now(CST).date() - datetime.timedelta(days=args.days_ago)
    else:
        target = datetime.datetime.now(CST).date()
    lookback = int(args.lookback if args.lookback is not None else check.get("lookback_days", 0))
    lookahead = int(args.lookahead if args.lookahead is not None
                    else check.get("report_lookahead_days", 0))
    sales_start, sales_end, cbg_start, cbg_end = compute_windows(target, lookback, lookahead)

    print(f"=== 对账 {own_store}（标识 {marker}）目标日 {target} ===")

    # 1. 本地订单库（**华为侧改从它读** —— 融合后 check 完全不碰华为）
    #
    # ⚠ 为什么必须在这一步卡死：库若覆盖不了窗口（今天还没抓），
    #   差集会把当天**所有**销售都算成「未报量」—— 一份完全错误的清单，
    #   而且**看着很合理**，门店会照着去补报一批假的。宁可不出，也不出错。
    #
    # 用户 2026-09-16 定的流程：拉完数据之后，2/3a/3b **都不需要登录华为**。
    # 所以会话自检搬去了 dump（第 1 步），这里只认库。
    from . import dump as dumpmod          # 延迟 import：dump 有 48KB，别拖慢每条命令
    db_path = _find_pos_db()
    if not db_path or not db_path.is_file():
        print("❌ 还没有订单库（out/cbg-<年>.db）", file=sys.stderr)
        print("   先抓一次：python -m src.cli dump --all", file=sys.stderr)
        return EXIT_FETCH
    # 窗口末尾若在未来（lookahead>0），只能要求"抓到此刻"
    need_by = min(_day_bounds(cbg_end)[1], int(time.time()))
    conn = dumpmod.open_db(db_path)
    ok, msg = dumpmod.check_freshness(conn, need_by)
    print(f"[1/6] 订单库：{'✅' if ok else '❌'} {msg}")
    if not ok:
        conn.close()
        print(f"      库：{db_path}", file=sys.stderr)
        print("      先抓一次再对账：python -m src.cli dump", file=sys.stderr)
        return EXIT_FETCH

    # 2. 华为侧：已报量 SN —— **从库里读**（华为只在第 1 步拉过一次，两个分析共用）
    #
    # ⚠ 和接口版**故意不同的两点**（都是改进，写进 `dump.reported_sns_from_db` 了）：
    #   1. 不传 `returnStatus` ⇒ 已退货/已关闭的原单**也在里面**。
    #      接口版传 `returnStatus=0` 会把它们整张滤掉，于是云商侧还在的销售
    #      被误报成「未报量」。**这是个真误报，改从库读顺带修掉了。**
    #   2. 一个 SN 挂多张单时取**最早那张**（接口版是"后写覆盖先写"，顺序不定）。
    s_ts, _ = _day_bounds(cbg_start)
    _, e_ts = _day_bounds(cbg_end)
    try:
        reported = dumpmod.reported_sns_from_db(conn, s_ts, e_ts)
    finally:
        conn.close()
    print(f"[2/6] 华为已报量：{len(reported)} 个 SN（{cbg_start} ~ {cbg_end}，来自本地库）")

    # 3. 云商侧：销售明细
    try:
        env_file = (cfg.get("erp") or {}).get("env_file")
        erp = ErpClient(load_credentials(env_file), env_file=env_file, verbose=args.verbose)
        rows = erp.sales_rows(sales_start, sales_end)
    except ErpError as e:
        print(f"❌ 云商取数失败：{e}", file=sys.stderr)
        return EXIT_FETCH
    except Exception as e:  # noqa: BLE001 - 网络/解析异常都要变成明确的退出码
        print(f"❌ 云商取数异常：{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FETCH
    print(f"[3/6] 云商销售明细：{len(rows)} 行（{sales_start} ~ {sales_end}）")

    # 4. 过滤 + 差集
    sales, skipped = classify_sales(
        rows, marker=marker, own_store=own_store,
        experience_stores=cfg["_experience"],
        include_types=check.get("include_doc_types"),
        exclude_types=check.get("exclude_doc_types"),
    )
    res = reconcile(sales, reported, total_rows=len(rows), skipped=skipped,
                    index=sn_row_index(rows), own_marker=marker,
                    experience_markers=cfg["_experience_markers"])

    ctx = {
        "门店": own_store, "华为门店编码": cfg.get("store_code") or "",
        "串号标识": marker, "目标日": str(target),
        "销售区间": f"{sales_start} ~ {sales_end}", "华为区间": f"{cbg_start} ~ {cbg_end}",
        "生成时间": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "配置文件": cfg.get("_path", ""),
    }
    out_dir = Path(args.out_dir or (ROOT / "out"))
    path = write_report(out_dir, res, ctx, str(target), cfg.get("store_code") or own_store)
    print(f"[4/6] 差异清单 → {path}")
    print()
    lines = summary_lines(res, ctx)
    for line in lines:
        print(line)

    # 5/6 邮件、6/6 企微 —— **失败只喊一声，绝不影响对账的退出码**
    _maybe_mail(cfg, args, ctx, lines, path, res)
    _maybe_wecom(cfg, args, ctx, path, res)
    return EXIT_OK if res.ok else EXIT_DIFF


def _maybe_wecom(cfg: dict, args, ctx: dict, report_path, res) -> None:
    if getattr(args, "no_push", False):
        print("\n[6/6] 企微：已用 --no-push 跳过")
        return
    try:
        wc = wecom.load_wecom_config(cfg, ROOT)
    except Exception as e:                                    # noqa: BLE001
        print(f"\n[6/6] 企微：⚠️ 配置读不出来（{e}），跳过")
        return

    ok, why = wecom.should_send(wc, has_diff=not res.ok)
    if not ok:
        print(f"\n[6/6] 企微：跳过（{why}）")
        return

    try:
        what = wecom.push(wc, ctx, res.missing, res.reverse_unshipped,
                          matched=len(res.matched),
                          total=len(res.matched) + len(res.missing),
                          report_path=report_path)
    except wecom.WecomError as e:
        print(f"\n[6/6] 企微：❌ 推送失败 —— {e}", file=sys.stderr)
        print("      （对账本身是成功的，退出码不受影响）", file=sys.stderr)
        return
    print(f"\n[6/6] 企微：✅ {what}")


def _maybe_mail(cfg: dict, args, ctx: dict, lines: list, report_path, res) -> None:
    if getattr(args, "no_mail", False):
        print("\n[5/6] 邮件：已用 --no-mail 跳过")
        return
    try:
        mc = mailer.load_mail_config(cfg, ROOT)
    except Exception as e:                                    # noqa: BLE001
        print(f"\n[5/6] 邮件：⚠️ 配置读不出来（{e}），跳过")
        return

    ok, why = mailer.should_send(mc, has_diff=not res.ok)
    if not ok:
        print(f"\n[5/6] 邮件：跳过（{why}）")
        return

    subject, body = mailer.build_report_mail(ctx, lines, len(res.missing),
                                             len(res.reverse_unshipped))
    try:
        mailer.send(mc, subject, body, [report_path])
    except mailer.MailError as e:
        # 对账结果是主产物，邮件只是投递方式 —— 邮件挂了要大声说，但别把整件事判成失败
        print(f"\n[5/6] 邮件：❌ 发送失败 —— {e}", file=sys.stderr)
        print("      （对账本身是成功的，退出码不受影响）", file=sys.stderr)
        return
    print(f"\n[5/6] 邮件：✅ 已发送到 {'、'.join(mc.recipients)}")


# ---------------------------------------------------------------------- main
def cmd_erp_login(args) -> int:
    """模拟登录云商换 token —— 界面上「测试登录」按钮走的就是这条。"""
    cfg = load_config(args.config)
    env_file = (cfg.get("erp") or {}).get("env_file")
    d0 = describe_credentials(env_file)
    print(f"云商账号：{d0['username'] or '（没配）'} | 公司：{d0['company']}")
    print(f"凭据文件：{d0['env_file']}")
    if not d0["username"] or not d0["has_password"]:
        print("❌ 账号或密码没配 —— 到控制台「设置 → 云商账号」里填", file=sys.stderr)
        return EXIT_FETCH
    print("正在登录…")
    client = ErpClient(load_credentials(env_file), env_file=env_file,
                       timeout=60, verbose=args.verbose)
    try:
        r = client.login_and_verify(save=False)
    except ErpCaptchaRequired as e:
        # 账号要图形验证码 —— 存图 + 在终端里问一次。
        # ⚠ 必须用**同一个 client** 提交（验证码跟会话绑定）
        img = ROOT / "out" / "云商验证码.png"
        try:
            import base64
            b64 = e.image.split(",", 1)[-1]
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(base64.b64decode(b64))
        except Exception as ex:                              # noqa: BLE001
            print(f"⚠️ 验证码图存不下来（{ex}）；到控制台「设置 → 云商账号」里填更方便",
                  file=sys.stderr)
            return EXIT_FETCH
        print(f"\n这个账号要图形验证码。图片已存到：\n  {img}")
        if not sys.stdin.isatty():
            print("（当前不是交互终端）到控制台「设置 → 云商账号」点「测试登录」，"
                  "验证码会直接显示在页面上。", file=sys.stderr)
            return EXIT_FETCH
        print("打开看一眼，把上面的字敲进来：")
        try:
            code = input("验证码> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消", file=sys.stderr)
            return EXIT_FETCH
        if not code:
            print("没填验证码", file=sys.stderr)
            return EXIT_FETCH
        try:
            r = client.login_and_verify(vcode=code, save=False)
        except ErpCaptchaRequired:
            print("❌ 验证码不对。再跑一次 erp-login 换一张，"
                  "或到控制台「设置 → 云商账号」里填（更省事）", file=sys.stderr)
            return EXIT_FETCH
        except ErpError as e2:
            print(f"❌ 登录失败：{e2}", file=sys.stderr)
            return EXIT_FETCH
    except ErpError as e:
        print(f"❌ 登录失败：{e}", file=sys.stderr)
        return EXIT_FETCH
    client._save_token(r["token"])                            # 验证过才落盘
    d = describe_credentials(env_file)
    who = f"：{r['who']}" if r.get("who") else ""
    print(f"✅ 登录成功{who}")
    print(f"   token {d['token']} 已写入 {d['env_file']}")
    return EXIT_OK


def cmd_mail_test(args) -> int:
    """发一封测试邮件 —— 界面上「发送测试邮件」按钮走的就是这条。"""
    cfg = load_config(args.config)
    mc = mailer.load_mail_config(cfg, ROOT)
    bad = mc.problems()
    if bad:
        print(f"❌ 邮件配置不全：{'、'.join(bad)}", file=sys.stderr)
        return EXIT_FETCH
    print(f"SMTP：{mc.host}:{mc.port}（{mc.security}）| 发件人 {mc.from_addr}")
    print(f"收件人：{'、'.join(mc.recipients)}")
    print("正在发送…")
    body = ("这是一封测试邮件。\n\n"
            "收到就说明 SMTP 配置没问题，之后每次对账跑完都会发到这个邮箱。\n")
    try:
        mailer.send(mc, "测试邮件", body)
    except mailer.MailError as e:
        print(f"❌ 发送失败：{e}", file=sys.stderr)
        return EXIT_FETCH
    print("✅ 已发送，去收件箱看看（顺便翻翻垃圾箱）")
    return EXIT_OK


def cmd_wecom_test(args) -> int:
    """往企微群推一条测试消息 —— 界面上「推送测试消息」按钮走的就是这条。"""
    cfg = load_config(args.config)
    wc = wecom.load_wecom_config(cfg, ROOT)
    bad = wc.problems()
    if bad:
        print(f"❌ 企微配置不全：{'、'.join(bad)}", file=sys.stderr)
        return EXIT_FETCH
    print(f"webhook key：{wecom.mask_key(wc.key)}")
    print("正在推送…")
    try:
        print("✅ " + wecom.test_push(wc))
    except wecom.WecomError as e:
        print(f"❌ 推送失败：{e}", file=sys.stderr)
        return EXIT_FETCH
    return EXIT_OK


def _spawn_detached(argv: list[str]):
    """脱离当前进程启动 —— 前台窗口关掉后它还活着。"""
    kw = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：不继承控制台，关窗不受影响
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(argv, **kw)


def _python_for_background() -> str:
    """后台服务用哪个解释器。

    **优先用安装时记下的那一个**（`src/runtime.py`）：一台电脑上有两个 Python 时，
    后台服务必须拉起来装过依赖的那个，否则 `start.bat` 报了"启动失败"，
    而真正的原因只是拉错了 Python。

    Windows 上再换成 `pythonw.exe` —— 它不带控制台窗口，不会闪黑框。
    """
    exe = runtime.current()
    if platform.system() == "Windows":
        return runtime.pythonw_for(exe)
    return exe


def cmd_service_start(args) -> int:
    """把服务放到后台跑起来 —— start.bat 调的就是这个。

    **中文一律在这里打印**，不让批处理去 echo：
    .bat 的编码受控制台代码页摆布（GBK 遇上 UTF-8 控制台就是一堆方块），
    而 Python 在 Windows 上走 WriteConsoleW，跟代码页无关，一定能显示对。
    """
    import webbrowser

    def line(txt=""):
        print(txt)

    def box(title):
        line()
        line("=" * 46)
        line(f"  {title}")
        line("=" * 46)

    running = service.find_running(ROOT)
    if running:
        url = f"http://{running.get('host', '127.0.0.1')}:{running['port']}/"
        box("服务已经在后台运行")
        line()
        line(f"  控制台： {url}")
        line()
        webbrowser.open(url)
        return EXIT_OK

    line("正在后台启动服务…")
    _spawn_detached([_python_for_background(), "-m", "src.cli", "serve", "--no-open"])
    line(f"等待服务就绪…（最多 {int(args.timeout)} 秒，这台电脑慢的话会久一点）")

    got = service.wait_ready(ROOT, timeout=args.timeout)
    if not got:
        # ⚠ 超时**不等于失败**：门店电脑上第一次冷启动（杀毒实时扫描、
        #   机械盘、Windows Defender 先扫一遍 pythonw.exe）超过等待时间是常事。
        #   实测反馈就是"窗口说要按回车，回车之后服务其实是好的"。
        #   所以这里再确认一次，能救回来就别报故障 —— 免得门店白折腾一轮。
        got = service.wait_ready(ROOT, timeout=10)
        if not got:
            box("等待超时 —— 服务可能还在启动")
            line()
            line("  这个窗口**关掉不影响**后台服务。先等 30 秒，然后：")
            line("    · 双击 start.bat 再看一次（服务要是已经起来，它会直接打开浏览器）")
            line("    · 或者手动打开控制台：http://127.0.0.1:8787/")
            line()
            line("  等了还是打不开，再跑 selftest.bat 看详细报错。")
            line()
            return EXIT_FETCH

    url = f"http://{got.get('host', '127.0.0.1')}:{got['port']}/"
    box("启动成功，服务已在后台运行")
    line()
    line(f"  控制台： {url}")
    line("  浏览器马上自动打开；没开就手动复制上面的地址")
    line()
    line("  停止服务：双击 stop.bat")
    line("  这个窗口关掉不影响后台服务")
    line()
    webbrowser.open(url)
    return EXIT_OK


def cmd_selftest(args) -> int:
    """逐项自检 —— selftest.bat 调这个（中文同样由 Python 输出）。"""
    import unittest

    def head(n, title):
        print()
        print("-" * 46)
        print(f"  {n}. {title}")
        print("-" * 46)

    failures = []

    head(0, "运行环境")
    print(f"  版本　 {version.describe()}")
    # 门店放最前面，而且显眼 —— 正式包里的配置是**新业广场店**的，
    # 发到别的店忘了改的话，会对到别的店账上去，而且看起来一切正常。
    # 装完跑一次 selftest 就能一眼确认。
    _cfg = {}
    try:
        _cfg = config_io.load_raw(ROOT / args.config)
    except Exception:                              # noqa: BLE001
        pass
    _code = str(_cfg.get("store_code") or "").strip()
    _name = str(_cfg.get("erp_store_name") or "").strip()
    _mark = str(_cfg.get("marker") or "").strip()
    if _code and _name:
        print(f"  门店　 {_code} · {_name}" + (f" · 标识 {_mark}" if _mark else ""))
    else:
        print("  门店　 ⚠️ 没配全（store_code / erp_store_name）—— "
              "到控制台「设置 → 门店」里填")
        print("          ⚠️ 配错门店会对到别的店账上去，而且看起来一切正常")
    # 门店电脑上可能是 3.14，也可能是 Win7 老机器的 3.8.10 —— 出问题时这一行能省很多来回。
    # ⚠ **两个都要打印**：现在跑着的这个，和安装时记下的那个。不一样就说明
    #   "双击 bat 用错 Python 了"，那是门店最常见的一类"装没成功"。
    print(f"  Python {platform.python_version()}（{sys.executable}）")
    try:
        from .elevate import always_admin_reason
        _why = always_admin_reason()
    except Exception:                                      # noqa: BLE001
        _why = ""
    if _why:
        # 这不是"提示"，是**这台电脑上抓会话注定失败的原因** —— 单独一行说清楚
        print(f"  ⚠️ {_why}")
    _pin = runtime.describe(ROOT)
    if _pin.get("pinned"):
        same = runtime.same_install(str(_pin["python"]), sys.executable or "")
        print(f"  安装时用的是 {_pin['version']}（{_pin['python']}）"
              + ("" if same else "　⚠️ 跟现在跑的不是同一个"))
    else:
        print("  安装时用的 Python：没记录（双击 install.bat 会补上）")
    print(f"  系统　 {platform.system()} {platform.release()}")
    import importlib.metadata as md
    for pkg in ("requests", "PyYAML", "openpyxl"):
        try:
            print(f"  {pkg:<8} {md.version(pkg)}")
        except Exception:                          # noqa: BLE001
            print(f"  {pkg:<8} ⚠️ 没装")

    head(1, "单元测试")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=0, stream=sys.stdout).run(suite)
    if not res.wasSuccessful():
        failures.append("单元测试没过")

    head(2, "浏览器（自动抓 cookie 要用）")
    # ⚠ cfg 要**先加载**：浏览器选哪个由 `browser.prefer` 定（默认 Chrome 优先）
    _bcfg = {}
    try:
        _bcfg = load_config(args.config)
    except Exception:                              # noqa: BLE001
        pass
    found = browser.find_browser(_bcfg)
    print(f"  {found[1]}：{found[0]}" if found
          else "  ✗ 没找到 Chrome / Edge —— 自动抓取用不了，只能走手抄 curl")
    print(f"  优先级：{browser._browser_prefer(_bcfg) or '默认（Chrome → Edge）'}")
    if not found:
        failures.append("没有浏览器")

    head(3, "云商账号")
    cfg = load_config(args.config)
    env_file = (cfg.get("erp") or {}).get("env_file")
    dc = describe_credentials(env_file)
    # 看**实际生效的**凭据，不是"这个文件里有什么" —— 否则会出现
    # "第3步说缺密码、第4步却登录成功"这种自相矛盾。
    creds = load_credentials(env_file)
    if creds["username"] and creds["password"]:
        print(f"  {creds['username']}（公司 {creds['company']}）")
        src = effective_env_file(env_file)
        if src and str(src) != dc["env_file"]:
            print(f"  注意：密码来自 {src}，不是 {dc['env_file']}")
    else:
        print("  ✗ 缺账号或密码 —— 到控制台「设置 → 云商账号」里填")
        failures.append("云商凭据不全")

    head(4, "云商登录（真去换一次 token）")
    try:
        r = ErpClient(load_credentials(env_file), env_file=env_file,
                      timeout=60, verbose=args.verbose).login_and_verify(save=False)
        print(f"  登录成功：{r.get('who') or dc['username']}")
    except ErpCaptchaRequired:
        print("  要图形验证码 —— 到控制台「设置 → 云商账号」点「测试登录」，"
              "验证码会显示在页面上")
    except ErpError as e:
        print(f"  ✗ {e}")
        failures.append("云商登录失败")

    head(5, "华为会话")
    try:
        cfg2 = load_config(args.config)
        client = make_client(cfg2, verbose=args.verbose)
        ok, msg = client.ping()
        print(("  " if ok else "  ✗ ") + msg)
        if not ok:
            failures.append("华为会话不可用")
    except CbgAuthError:
        print("  还没登录过（正常）—— 到控制台「会话」页点「打开浏览器抓取」")

    head(6, "后台服务")
    got = service.find_running(ROOT)
    print(f"  运行中：{got.get('host')}:{got['port']}" if got
          else "  没在跑（正常）—— 双击 start.bat 启动")

    print()
    if failures:
        print("自检发现问题：" + "、".join(failures))
        print("照着上面的提示处理；搞不定就把这段输出发回来。")
        return EXIT_FETCH
    print("自检通过，可以开始用了。")
    return EXIT_OK


def cmd_status(args) -> int:
    """后台服务在不在。start.bat 靠退出码判断（0=在跑）。"""
    got = (service.wait_ready(ROOT, timeout=args.wait) if args.wait
           else service.find_running(ROOT))
    if not got:
        print("服务未在运行" + ("（等超时了）" if args.wait else ""))
        return EXIT_FETCH
    print(f"服务运行中：http://{got.get('host', '127.0.0.1')}:{got['port']}/"
          f"  (PID {got.get('pid') or '?'})")
    return EXIT_OK


def cmd_stop(args) -> int:
    """停掉后台服务。"""
    ok, msg = service.stop(ROOT)
    print(("✅ " if ok else "⚠️ ") + msg)
    return EXIT_OK if ok else EXIT_FETCH


def cmd_autostart(args) -> int:
    """开机自启：不加参数=看状态，--on/--off=注册/取消。"""
    if args.on:
        res = autostart.install(ROOT)
    elif args.off:
        res = autostart.remove()
    else:
        st = autostart.status(ROOT)
        print(f"平台：{st['platform']}（{st['kind']}）")
        print(f"状态：{'已注册' if st['installed'] else '未注册'}")
        print(f"命令：{st['command']}")
        if st.get("stale"):
            print("⚠️ 注册的还是老路径（项目挪过位置？）—— 重新注册一次")
        return EXIT_OK
    print(("✅ " if res.get("ok") else "❌ ") + str(res.get("message", "")))
    st = autostart.status(ROOT)
    print(f"现在：{'已注册' if st['installed'] else '未注册'}")
    return EXIT_OK if res.get("ok") else EXIT_FETCH


def cmd_serve(args) -> int:
    from .web import serve
    serve(port=args.port, host=args.host, open_browser=not args.no_open,
          root=ROOT, config=args.config)
    return EXIT_OK


def _harden_stdout() -> None:
    """让**任何**字符编不出编码时不再抛异常。

    ⚠ 这不是洁癖，是门店踩出来的：后台服务由 `pythonw.exe` 起（没有控制台），
    或者 stdout 被重定向到文件时，Python 按 locale 编码写输出（中文 Windows 是
    GBK）。这时打印一个该编码里没有的字符（`⚠`、`✅` 这类符号）会直接抛
    `UnicodeEncodeError` —— 而 `serve()` 里那些启动提示就在 `serve_forever()`
    **前面**，一抛就是"服务根本起不来"。

    输出难看一点无所谓，**服务起不来才是事故**。`errors="replace"` 让编不出来的
    字符退化成 `?`，进程照常跑下去。
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")            # Python 3.7+
        except Exception:                                   # noqa: BLE001
            pass          # 没有控制台时 stream 可能是 None / 不支持重配 —— 无所谓


# --------------------------------------------------------------- POS 合规
def report_bug(root, config_path, *, no_mail=False, no_push=False,
               out_dir=None) -> dict:
    r"""**一键上报 bug**：收集现场 → 打包 → 试着推出去。返回**结构化结果**。

    ⚠ **顺序是死的：先落盘，再谈推送。**

    上报通道不能只依赖出问题的那条通道 —— 最常见的 bug 恰恰是"推送失败"
    （webhook key 过期、群机器人被删、SMTP 认证失败、门店网络不通）。
    这时候用推送去上报推送的故障，**必然也失败**。
    所以①打包落盘这一步**不碰网络**，一定能成；②③只是"顺手试着发"，
    发不出去就把包的路径摆出来，让人工有退路。

    ⚠ 包里**没有任何能拿去登录的凭据**（`.secrets` 整个不进包），
    但有业务数据（门店名 / 串号 / 金额）—— 详见 `bugreport` 模块。

    打印交给调用方：CLI 打给人看，Web 拿去渲染成 JSON。
    """
    from . import bugreport
    root = Path(root)
    cfg = load_config(config_path, root=root)
    out = {"ok": True, "path": "", "size_kb": 0.0, "entries": [],
           "mail": "", "wecom": "", "sent": [], "message": ""}

    try:
        path = bugreport.build_zip(root, config_path, out_dir=out_dir)
    except ValueError as e:
        # 反查拦下的 —— 这是**好事**，说明防线起作用了
        return {"ok": False, "path": "", "size_kb": 0.0, "entries": [],
                "mail": "", "wecom": "", "sent": [],
                "message": "⛔ %s（什么都没发出去，请把这条报给开发者）" % e}
    except OSError as e:
        return {"ok": False, "path": "", "size_kb": 0.0, "entries": [],
                "mail": "", "wecom": "", "sent": [],
                "message": "打包失败：%s" % e}

    out["path"] = str(path)
    out["size_kb"] = round(path.stat().st_size / 1024.0, 1)
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            out["entries"] = list(z.namelist())
    except Exception:                                          # noqa: BLE001
        pass

    if no_push and no_mail:
        out["mail"] = out["wecom"] = "已跳过"
        out["message"] = "已打包（没开推送）：%s" % path
        return out

    # ② 邮件（附件）—— 和 ③ 各自独立，哪条通了算哪条
    if no_mail:
        out["mail"] = "已跳过"
    else:
        try:
            mc = mailer.load_mail_config(cfg, root)
            # ⚠ `has_diff=True` 是为了**绕过**「只有差异才发」——
            #   上报 bug 跟有没有差异没关系
            ok, why = mailer.should_send(mc, has_diff=True)
            if not ok:
                out["mail"] = "跳过（%s）" % why
            else:
                when = bugreport._now().strftime("%m-%d %H:%M")
                subject = "[bug 上报] %s %s" % (cfg.get("erp_store_name") or "门店", when)
                body = ("门店点了「上报 bug」，现场在附件里。\n\n"
                        "包里没有凭据（.secrets 整个没进包），"
                        "但有业务数据（串号 / 金额）。\n"
                        "先看附件里的「执行日志.txt」。\n\n"
                        "—— 由 cbg-reconcile 自动发送，%s\n"
                        % bugreport._now().strftime("%Y-%m-%d %H:%M:%S"))
                # ⚠ prefix="" ：主题自己带了 `[bug 上报]`，
                #   再套一层 `[报量对账]` 会读成"对账邮件"
                mailer.send(mc, subject, body, [path], prefix="")
                out["mail"] = "✅ 已发到 %s" % "、".join(mc.recipients)
                out["sent"].append("邮件")
        except Exception as e:                                 # noqa: BLE001
            out["mail"] = "❌ %s" % e

    # ③ 企微（文件）
    if no_push:
        out["wecom"] = "已跳过"
    else:
        try:
            wc = wecom.load_wecom_config(cfg, root)
            ok, why = wecom.should_send(wc, has_diff=True, ignore_when=True)
            if not ok:
                out["wecom"] = "跳过（%s）" % why
            else:
                # 先发一条 text 说明这是什么 —— 群里突然冒出一个 zip 没人敢点
                wecom.send_text(wc, "【bug 上报】%s：现场日志见下一条文件"
                                    % (cfg.get("erp_store_name") or "门店"))
                wecom.send_file(wc, path)
                out["wecom"] = "✅ 已发出"
                out["sent"].append("企微")
        except Exception as e:                                 # noqa: BLE001
            out["wecom"] = "❌ %s" % e

    if out["sent"]:
        out["message"] = "已通过 %s 发出；包也留在 %s" % ("、".join(out["sent"]), path)
    else:
        # ⚠ 推送全失败**不算失败** —— 包在本地，人工能发。
        #   这里若判成失败，用户会以为"上报没成"，然后就不管了。
        out["message"] = ("自动发送没成功（**这也可能正是你要报的那个 bug**）。"
                          "包已经打好了，直接把这个文件发给开发者就行：%s" % path)
    return out


def cmd_report_bug(args) -> int:
    """`report-bug` 子命令 —— 把 `report_bug()` 的结果打给人看。"""
    print("=" * 64)
    print("上报 bug：收集现场 → 打包 → 推送")
    print("=" * 64)
    res = report_bug(ROOT, args.config, no_mail=args.no_mail,
                     no_push=args.no_push, out_dir=args.out_dir or None)
    if not res["ok"]:
        print("❌ %s" % res["message"], file=sys.stderr)
        return EXIT_INTERNAL

    print("\n① 现场已打包：%s（%.1f KB）" % (res["path"], res["size_kb"]))
    print("   包里**没有**能拿去登录的凭据（.secrets 整个没进包），可以放心发。")
    print("   ⚠ 但有业务数据（门店名 / 串号 / 金额）—— 发之前确认收件人是自己人。")
    for n in res["entries"]:
        print("     · %s" % n)
    print("\n② 邮件：%s" % res["mail"])
    print("③ 企微：%s" % res["wecom"])
    print("\n" + "=" * 64)
    print(res["message"])
    print("=" * 64)
    return EXIT_OK


def cmd_daily(args) -> int:
    """日常流程 —— 一条定时任务跑完三步（编排在 `run_daily` 里）。

    ⚠ 第 1 步失败会**跳过第 2、3 步**（理由写在 `run_daily` 的模块注释里）。

    ⚠ 参数**逐个转发**，别写"够用就行"的子集：老门店的 `run.bat` 里是 `check`，
    把它迁到 `daily` 时，`check` 认得的参数在这里必须都接得住
    （子解析器是超集，见 `daily` 那段）。
    """
    from . import run_daily
    argv = ["-c", args.config]
    # argparse 默认值 vs "用户真给了" —— 空串/None 表示没给，别把默认值当成用户意图
    # 硬塞过去（塞了会覆盖 `run_daily` 自己的默认，两处默认值迟早对不上）。
    for flag, val in (("--date", args.date),
                      ("--lookback", args.lookback),
                      ("--lookahead", args.lookahead),
                      ("--out-dir", args.out_dir),
                      ("--log-file", args.log_file)):
        if val not in ("", None):
            argv += [flag, str(val)]
    if args.days_ago is not None and not args.date:
        # ⚠ 显式给了 --date 就别再给 --days-ago：两个都传的话谁赢不确定
        argv += ["--days-ago", str(args.days_ago)]
    for flag, on in (("--skip-dump", args.skip_dump),
                     ("--skip-check", args.skip_check),
                     ("--skip-pos", args.skip_pos),
                     ("--no-mail", args.no_mail),
                     ("--no-push", args.no_push),
                     ("--no-refresh", args.no_refresh)):
        if on:
            argv.append(flag)
    if getattr(args, "verbose", False):
        argv.append("-v")
    return run_daily.main(argv)


def cmd_dump(args) -> int:
    """抓华为订单 → 补进 `out/cbg-<年>.db`（**取并集**，不删旧行）。

    ⚠ 复用**主项目的会话和门店配置** —— 不再要 `--store-code`，
    也不会出现"两个会话文件"（换店时另一个是旧的，这个坑很难查）。

    ⚠ 会话失效时**先静默续期**再抓（见 `ensure_session`）。这一步是整条
    日常流程里**唯一登录华为**的地方，所以续期挂在这儿，别处都不用管。
    """
    from . import dump as dumpmod
    cfg = load_config(args.config)
    sp = session_path(cfg)
    if not sp.is_file():
        # ⚠ 续期也得先有个会话文件当"基底"（要复用它的浏览器 profile 登录态）。
        #   一张白纸的情况只能人工登录 —— 报错里直接给命令。
        print(f"❌ 没有华为会话：{sp}\n   先跑一次：python -m src.cli -c {args.config} auth --auto",
              file=sys.stderr)
        return EXIT_AUTH
    _, rc = require_session(cfg, verbose=getattr(args, "verbose", False),
                            allow_refresh=not getattr(args, "no_refresh", False))
    if rc is not None:
        return rc
    argv = ["--session", str(sp)]
    code = (cfg.get("store_code") or "").strip()
    if code:
        argv += ["--store-code", code]
    if args.all:
        argv.append("--all")
    else:
        argv += ["--month", args.month or "current"]
    return dumpmod.main(argv)


def cmd_pos(args) -> int:
    """算 POS 合规率 → 落 `out/pos-<年>.json`（**看板只读它，不现场算**）。

    算完**单独推一条** POS 合规（和报量排查分开两条）——
    见 `_maybe_pos_push`。用 `--no-push --no-mail` 可以只算不发。
    """
    from . import pos_report
    db = Path(args.db) if args.db else _find_pos_db()
    if not db or not db.is_file():
        print("❌ 没找到订单库（out/cbg-<年>.db）\n   先抓一次：python -m src.cli dump",
              file=sys.stderr)
        return EXIT_FETCH
    from . import pos_metric as pm
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    orders, returns = pos_report.load(conn)
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
                       "ap_den": round(ap.den, 2), "ap_num": round(ap.num, 2), "ap_rate": ap.rate}
        rows.append(row)

    year = int(months[-1][:4]) if months else datetime.datetime.now(CST).year
    out = ROOT / "out" / ("pos-%d.json" % year)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:                                    # 库路径写成相对 ROOT 的，别把开发机绝对路径带进落盘
        db_rel = str(db.relative_to(ROOT))
    except ValueError:
        db_rel = str(db)
    out.write_text(json.dumps({
        "generated_at": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "year": year, "db": db_rel, "orders": len(orders), "returns": len(returns),
        "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"POS 合规 → {out}")
    for row in rows:
        la = row[pm.BY_LABEL]
        f = lambda v: "  —  " if v is None else "%5.2f" % v     # 分母为 0 时分数是 None，不是 0
        print("  %s%s  按标签 %s%%（申诉后 %s%%）  分母 %s"
              % (row["month"], "（暂定）" if row["provisional"] else "        ",
                 f(la["rate"]), f(la["ap_rate"]), format(la["den"], ",.2f")))
    _maybe_pos_push(getattr(args, "config", None), args, rows)
    return EXIT_OK


def _pos_ctx(cfg: dict) -> dict:
    """POS 推送的上下文 —— 字段名跟报量排查那边一致（`门店`/`生成时间`…），
    这样 `wecom`/`mailer` 两套组装不用为"POS 版"再分一次支。
    """
    return {
        "门店": cfg.get("erp_store_name") or cfg.get("store_code") or "?",
        "华为门店编码": cfg.get("store_code") or "",
        "串号标识": cfg.get("marker") or "",
        "生成时间": datetime.datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "配置文件": cfg.get("_path") or "",
    }


def _maybe_pos_push(config_path, args, rows) -> None:
    """POS 合规的**第二条推送** —— 和报量排查分开发（用户 2026-09-16 定的）。

    ⚠ 这里**刻意不复用** `_maybe_mail` / `_maybe_wecom`：
    那两条是围着 `ReconcileResult` 长的（missing / unshipped / xlsx 附件），
    硬塞进 POS 只会长出一堆 `if`。共用的是**格式化**（`pos_report.notify_lines`）
    和**发送**（`wecom.push_pos` / `mailer.build_pos_mail`）。

    ⚠ 推送失败**不影响退出码** —— 分数是主产物，推送只是投递方式。
    这条跟报量排查那边一个道理（那边也是这么写的）。
    """
    if not config_path:
        return
    from . import pos_report          # 延迟 import：跟 `cmd_pos` 保持一致
    if getattr(args, "no_push", False) and getattr(args, "no_mail", False):
        print("\n[推送] POS：已用 --no-push --no-mail 跳过")
        return
    try:
        cfg = load_config(config_path)
    except Exception as e:                                    # noqa: BLE001
        print(f"\n[推送] POS：⚠️ 配置读不出来（{e}），跳过")
        return
    ctx = _pos_ctx(cfg)
    lines = pos_report.notify_lines(rows)
    head = pos_report.headline(rows)

    if not getattr(args, "no_push", False):
        try:
            wc = wecom.load_wecom_config(cfg, ROOT)
            # ⚠ `ignore_when=True`：POS 没有"差异"概念，
            #   「只有差异才推」那个开关是给报量排查的
            ok, why = wecom.should_send(wc, has_diff=False, ignore_when=True)
            if not ok:
                print(f"\n[推送] POS 企微：跳过（{why}）")
            else:
                print(f"\n[推送] POS 企微：✅ {wecom.push_pos(wc, ctx, lines, head)}")
        except Exception as e:                                # noqa: BLE001
            print(f"\n[推送] POS 企微：❌ {e}", file=sys.stderr)
            print("      （分数已经算好了，退出码不受影响）", file=sys.stderr)

    if not getattr(args, "no_mail", False):
        try:
            mc = mailer.load_mail_config(cfg, ROOT)
            ok, why = mailer.should_send(mc, has_diff=False)
            if not ok:
                print(f"[推送] POS 邮件：跳过（{why}）")
            else:
                subject, body = mailer.build_pos_mail(ctx, lines, head)
                mailer.send(mc, subject, body, prefix=mailer.POS_SUBJECT_PREFIX)
                print(f"[推送] POS 邮件：✅ 已发送到 {'、'.join(mc.recipients)}")
        except Exception as e:                                # noqa: BLE001
            print(f"[推送] POS 邮件：❌ {e}", file=sys.stderr)
            print("      （分数已经算好了，退出码不受影响）", file=sys.stderr)


def cmd_pos_export(args) -> int:
    """出 POS 明细 Excel。"""
    from . import pos_export
    db = Path(args.db) if args.db else _find_pos_db()
    if not db or not db.is_file():
        print("❌ 没找到订单库", file=sys.stderr)
        return EXIT_FETCH
    argv = ["--db", str(db), "--month", args.month]
    if args.out:
        argv += ["--out", args.out]
    if args.by:
        argv += ["--by", args.by]
    return pos_export.main(argv)


def _find_pos_db():
    """找 `out/` 里最新的那个 `cbg-<年>.db`。一年一个库，取年份最大的。"""
    d = ROOT / "out"
    if not d.is_dir():
        return None
    cands = sorted(d.glob("cbg-[0-9][0-9][0-9][0-9].db"))
    return cands[-1] if cands else None


def build_parser() -> argparse.ArgumentParser:
    """建命令行解析器。

    ⚠ **单独一个函数**，不是为了好看：`daily` 必须是 `check` 的**参数超集**
    （理由见 `daily` 那段注释 —— 老门店的 run.bat 里写的是 `check`，
    迁移时要能原样喂给 `daily`）。这条约束得有测试盯着，
    而测试**只能内省解析器**才能可靠地比对参数集合 ——
    正则去猜源码是猜不准的（第一版就那么写的，写出一堆空转）。
    """
    ap = argparse.ArgumentParser(prog="cbg-reconcile", description="云商 ↔ 华为报量对账")
    ap.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="门店配置文件")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="获取 / 更新华为会话")
    p.add_argument("--auto", action="store_true",
                   help="打开浏览器自动抓（首次或 SSO 过期时，登录一次即可）")
    p.add_argument("--refresh", action="store_true",
                   help="无头静默续期（复用浏览器 profile 里的登录态，不弹窗）")
    p.add_argument("--from-curl", help="退路：存放 curl 的文件，`-` 表示从标准输入读")
    p.add_argument("--timeout", type=int, default=240, help="等待登录的秒数，默认 240")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("ping", help="华为会话自检")
    p.set_defaults(func=cmd_ping)

    p = sub.add_parser("erp-login", help="模拟登录云商换 token（验证账号密码配对了没）")
    p.set_defaults(func=cmd_erp_login)

    p = sub.add_parser("mail-test", help="发一封测试邮件，验证 SMTP 配置")
    p.set_defaults(func=cmd_mail_test)

    p = sub.add_parser("wecom-test", help="往企微群推一条测试消息，验证 webhook")
    p.set_defaults(func=cmd_wecom_test)

    p = sub.add_parser("check", help="跑对账")
    p.add_argument("--date", help="目标日 YYYY-MM-DD，默认今天")
    p.add_argument("--days-ago", type=int,
                   help="目标日 = 今天往前 N 天（计划任务里用 --days-ago 1 查昨天，"
                        "免得依赖 Windows 的日期格式）")
    p.add_argument("--lookback", type=int, help="覆盖配置：销售窗口往前多看几天")
    p.add_argument("--lookahead", type=int, help="覆盖配置：华为窗口往后多看几天")
    # ⚠ `--no-refresh` 从这里**搬走了** —— 它控的是"华为会话失效时要不要静默续期"，
    #   而华为那套已经整体搬去第 1 步（`dump`）了。留在这儿的话它是个**死参数**：
    #   `--help` 里写着一段已经不存在的行为了。
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.add_argument("--no-push", action="store_true", help="本次不推企微")
    p.add_argument("--out-dir", help="输出目录，默认 ./out")
    p.add_argument("--log-file", help="把输出**同时**写一份到这个文件（run.bat 用）。"
                                      "不写的话计划任务跑完什么都留不下")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("service-start", help="把服务放到后台跑起来（start.bat 调的）")
    # ⚠ 别调小：门店电脑冷启动（杀毒实时扫描 / 机械盘）超过 25 秒是常事，
    #   超时就会误报"启动失败"，而服务其实好好的。
    p.add_argument("--timeout", type=float, default=60,
                   help="等就绪的秒数，默认 60（超时后还会再确认 10 秒）")
    p.set_defaults(func=cmd_service_start)

    p = sub.add_parser("selftest", help="逐项自检（selftest.bat 调的）")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("status", help="后台服务在不在（退出码 0 = 在跑）")
    p.add_argument("--wait", type=float, default=0,
                   help="最多等 N 秒直到服务起来（start.bat 用这个确认启动成功）")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stop", help="停掉后台服务")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("autostart", help="开机自启：默认看状态")
    p.add_argument("--on", action="store_true", help="注册开机自启")
    p.add_argument("--off", action="store_true", help="取消开机自启")
    p.set_defaults(func=cmd_autostart)

    p = sub.add_parser("report-bug",
                       help="一键上报 bug：打包现场日志 → 邮件 / 企微推送")
    p.add_argument("--no-mail", action="store_true", help="别发邮件")
    p.add_argument("--no-push", action="store_true", help="别推企业微信")
    p.add_argument("--out-dir", default="", help="包放哪（默认 out/）")
    p.set_defaults(func=cmd_report_bug)

    p = sub.add_parser("daily", help="日常流程：抓华为当月 → 报量对账 → 算 POS"
                                     "（**定时任务跑这个**）")
    # ⚠ 这里的参数是 `check` 的**超集**，不是"够用就行"。
    #   理由：老门店电脑上的 run.bat 是**安装时生成的、不进版本库**，
    #   自更新不会重写它 —— 它里面写的是 `check`。要把那种 bat 平滑迁到
    #   `daily`，`daily` 就必须认得 `check` 认得的**每一个**参数，
    #   否则迁移那天会以"unrecognized arguments"收场。
    p.add_argument("--date", default="", help="对账目标日 YYYY-MM-DD")
    p.add_argument("--days-ago", type=int, default=1, help="对账目标日 = 今天往前 N 天")
    p.add_argument("--lookback", type=int, help="覆盖配置：销售窗口往前多看几天")
    p.add_argument("--lookahead", type=int, help="覆盖配置：华为窗口往后多看几天")
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    p.add_argument("--no-refresh", action="store_true",
                   help="会话失效时别开浏览器静默续期（第 1 步就会直接失败）")
    p.add_argument("--out-dir", default="", help="报告输出目录")
    p.add_argument("--skip-dump", action="store_true", help="跳过第 1 步：不抓华为数据")
    p.add_argument("--skip-check", action="store_true", help="跳过第 2 步：不做报量排查")
    p.add_argument("--skip-pos", action="store_true", help="跳过第 3 步：不算 POS 合规")
    p.add_argument("--log-file", default="", help="把对账那段同时写一份到文件")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("dump", help="抓华为订单 → out/cbg-<年>.db（取并集）")
    p.add_argument("--month", default="", help="YYYY-MM；给 current 或不给 = 当月（日常用这个）")
    p.add_argument("--all", action="store_true", help="抓全部历史（首次安装/补历史用，**不进日常流程**）")
    p.add_argument("--no-refresh", action="store_true",
                   help="会话失效时别开浏览器静默续期")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("pos", help="算 POS 合规率 → out/pos-<年>.json（并单独推一条）")
    p.add_argument("--db", default="", help="订单库；不给就取 out/ 里最新的 cbg-<年>.db")
    p.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.set_defaults(func=cmd_pos)

    p = sub.add_parser("pos-export", help="出 POS 明细 Excel")
    p.add_argument("--month", required=True, help="2026-08")
    p.add_argument("--db", default="")
    p.add_argument("--out", default="")
    p.add_argument("--by", default="", choices=["", "label", "remark"])
    p.set_defaults(func=cmd_pos_export)

    p = sub.add_parser("serve", help="打开本地控制台（报告 / 运行 / 会话 / POS / 设置）")
    p.add_argument("--port", type=int, default=8787, help="端口，默认 8787；被占用时自动换")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p.add_argument("--no-open", action="store_true", help="别自动开浏览器")
    p.set_defaults(func=cmd_serve)

    return ap


def main(argv=None) -> int:
    _harden_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as e:                                    # noqa: BLE001
        # ⚠ 不接住的话 Python 默认退出码是 1 —— 跟"会话过期"撞车，
        #   计划任务会把"程序崩了"当成"该重新登录了"，排查方向完全跑偏。
        import traceback
        print(f"\n❌ 没预料到的错误：{type(e).__name__}: {e}", file=sys.stderr)
        print("   这是程序 bug，不是会话过期 —— 把下面这段发回来：", file=sys.stderr)
        traceback.print_exc()
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
