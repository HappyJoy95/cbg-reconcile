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

from . import autostart, browser, config_io, lockfile, mailer, service, version, wecom
from .cbg import CbgClient, CbgError
from .erp import (ErpCaptchaRequired, ErpClient, ErpError, describe_credentials,
                  effective_env_file, load_credentials)
from .reconcile import classify_sales, reconcile, sn_row_index
from .report import summary_lines, write_report
from .session import CbgAuthError, CbgSession

ROOT = Path(__file__).resolve().parent.parent
CST = datetime.timezone(datetime.timedelta(hours=8))

EXIT_OK, EXIT_AUTH, EXIT_FETCH, EXIT_DIFF, EXIT_INTERNAL = 0, 1, 2, 3, 9


def browser_profile(cfg: dict) -> Path:
    """浏览器 profile 目录 —— 存登录态的地方，日常静默续期靠它。"""
    return browser.profile_path(cfg, ROOT)


# --------------------------------------------------------------------- 配置
def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        raise SystemExit(f"找不到配置文件 {p}\n"
                         f"（部署到门店电脑时，请改 config/store-*.yaml 里的三行再运行）")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg["_path"] = str(p)

    stores_doc = yaml.safe_load((ROOT / "config" / "stores.yaml").read_text(encoding="utf-8"))
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

        def _verify(s) -> bool:
            try:
                return CbgClient(s, store_code=cfg.get("store_code") or None, timeout=25).ping()[0]
            except (CbgAuthError, CbgError):
                return False

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

    # 1. 华为会话（失效时先尝试静默续期，别动不动就让人去抓包）
    try:
        client = make_client(cfg, verbose=args.verbose)
    except CbgAuthError as e:
        client = None
        ok, msg = False, str(e)

    if client is not None:
        ok, msg = client.ping()

    auto_refresh = (cfg.get("session") or {}).get("auto_refresh", True)
    if not ok and auto_refresh and not args.no_refresh:
        print(f"[1/6] 华为会话：❌ {msg}")
        print("      会话失效 → 用浏览器 profile 静默续期（自检通过才会覆盖）…")
        store = cfg.get("store_code") or None

        def _verify(s) -> bool:
            try:
                return CbgClient(s, store_code=store, timeout=25).ping()[0]
            except (CbgAuthError, CbgError):
                return False

        try:
            creds = browser.load_login_credentials(cfg, ROOT)
            sess2 = browser.capture_session(browser_profile(cfg), headless=True, timeout=90,
                                            on_step=lambda m: print("        " + m),
                                            verify=_verify,
                                            url=browser.login_url(cfg),
                                            credentials=creds if all(creds) else None)
            sess2.save(session_path(cfg))
            client = CbgClient(sess2, store_code=store, verbose=args.verbose)
            ok, msg = client.ping()
        except (browser.BrowserError, CbgAuthError) as e:
            print(f"      自动续期没成功：{e}")
            print("      现有会话没有被覆盖。")

    print(f"[1/6] 华为会话：{'✅' if ok else '❌'} {msg}")
    if not ok:
        print("      需要人工登录一次（浏览器里登完会自动抓走，不用再复制 curl）：\n"
              "        python -m src.cli auth --auto", file=sys.stderr)
        return EXIT_AUTH

    # 2. 华为侧：已报量 SN
    try:
        s_ts, _ = _day_bounds(cbg_start)
        _, e_ts = _day_bounds(cbg_end)
        reported = client.reported_sns(s_ts, e_ts,
                                       pay_status=check.get("pay_status", 2),
                                       return_status=check.get("return_status", 0),
                                       page_size=int(check.get("page_size", 200)))
    except CbgAuthError as e:
        print(f"❌ 华为会话失效：{e}", file=sys.stderr)
        return EXIT_AUTH
    except CbgError as e:
        print(f"❌ 华为取数失败：{e}", file=sys.stderr)
        return EXIT_FETCH
    print(f"[2/6] 华为已报量：{len(reported)} 个 SN（{cbg_start} ~ {cbg_end}）")

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
    """Windows 上用 pythonw.exe —— 它不带控制台窗口，不会闪黑框。"""
    exe = sys.executable or "python"
    if platform.system() == "Windows":
        pyw = Path(exe).with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
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
    # 门店电脑上是 3.14，开发机是 3.9 —— 出问题时这一行能省很多来回
    print(f"  Python {platform.python_version()}（{sys.executable}）")
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


def main(argv=None) -> int:
    _harden_stdout()
    ap = argparse.ArgumentParser(prog="cbg-reconcile", description="云商 ↔ 华为报量对账")
    ap.add_argument("-c", "--config", default="config/store-SCN231409.yaml", help="门店配置文件")
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
    p.add_argument("--no-refresh", action="store_true",
                   help="会话失效时不要自动开浏览器续期（计划任务里想跑快一点可以加）")
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

    p = sub.add_parser("serve", help="打开本地控制台（报告 / 运行 / 会话 / 设置）")
    p.add_argument("--port", type=int, default=8787, help="端口，默认 8787；被占用时自动换")
    p.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    p.add_argument("--no-open", action="store_true", help="别自动开浏览器")
    p.set_defaults(func=cmd_serve)

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
