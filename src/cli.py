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
from .modules import auth, notify
import json
import sqlite3

from .paths import ROOT
from .reconcile import classify_sales, reconcile, sn_row_index
from .report import summary_lines, write_report
from .session import CbgAuthError, CbgSession

#: 每月**头几天**顺带把上个月的云商销售重拉一遍。
#:
#: ⚠ 为什么需要：9-30 21:00 那趟跑完之后又录进去的销售，**只拉当月是永远补不到的**
#: —— 10-01 那次拉的是 `10-01 ~ 10-01`，9-30 那笔就此消失（用户 2026-09-17 提的）。
#:
#: ⚠ 为什么是 **3 天不是 1 天**：1 号正好赶上周末 / 那天没开机的话，
#: 这个补漏就整个错过了，而它一年只有 12 次机会。多拉两天的代价是
#: 多几段请求（一个月最多 4 段），重拉是 `INSERT OR REPLACE`，不会重复也不会删旧行。
BACKFILL_DAYS = 3

#: 门店配置的默认路径。⚠ **只此一处** —— `run_daily` 也用它，
#: 两处各写一份的话，哪天改了默认值另一处会静默用旧的。
DEFAULT_CONFIG = "config/store-SCN231409.yaml"
CST = datetime.timezone(datetime.timedelta(hours=8))

EXIT_OK, EXIT_AUTH, EXIT_FETCH, EXIT_DIFF, EXIT_INTERNAL = 0, 1, 2, 3, 9

#: **命令写错了**（不是跑失败）—— `daily` 忘了点名 `--steps`、点了不认识的步骤都走它。
#: ⚠ 数**故意**跟 `EXIT_FETCH` 一样是 2：argparse 自己的用法错误就是 2，
#:   把它们分开命名只是为了让读代码的人一眼看出"这不是取数失败"。
EXIT_USAGE = 2


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
    # ⚠ **只收体验店**（`kind == 体验店`）—— 名单 2026-09-18 从 14 家扩到 29 家，
    #   但「其他体验店卖出(调拨处理)」那条跳过说的是"**体验店之间**走调拨"，
    #   把 14 家合作店也算进去，它们的销售会被当成调拨跳过 —— 那是**改行为**，
    #   不是加数据。所以这里按 `kind` 过滤，扩名单前后行为完全一致。
    #
    #   `kind` 缺省的按**体验店**算：老版本写下的名单没有这个字段，
    #   而那时能进名单的**本来就只有**体验店（14 家全带串号标识）。
    def _is_experience(s):
        return (s.get("kind") or "体验店") == "体验店"

    cfg["_experience"] = {s["erp_name"] for s in stores
                          if s.get("erp_name") and _is_experience(s)}
    cfg["_experience_markers"] = {s["marker"] for s in stores
                                  if s.get("marker") and _is_experience(s)}
    cfg["_non_store_markers"] = set(stores_doc.get("non_store_markers") or ())
    doc_types = stores_doc.get("doc_types") or {}
    cfg.setdefault("check", {})
    cfg["check"].setdefault("include_doc_types", doc_types.get("include") or [])
    cfg["check"].setdefault("exclude_doc_types", doc_types.get("exclude") or [])

    # ⚠ 判据是**整份名单**，不是 `_experience`（那里面只有体验店）。
    #   2026-09-18：名单从 14 家扩到 29 家（15 体验店 + 14 合作店）之后，
    #   拿 `_experience` 判会把**每一家合作店**都报成"不在名单里"——
    #   而合作店不在调拨规则里本来就是对的，那条警告纯属噪音。
    own = cfg.get("erp_store_name")
    if own and own not in {s["erp_name"] for s in stores if s.get("erp_name")}:
        print(f"[警告] {own!r} 不在 config/stores.yaml 的门店名单里 —— "
              f"匹配不到华为编码和串号标识，请把这家店补进名单。", file=sys.stderr)
    return cfg


def store_profile_of(cfg: dict) -> dict:
    """这台机器是哪家店、要不要玲珑 —— `cli` 这边的入口。

    ⚠ 只是一层转发，真逻辑在 `config_io.store_profile` ——
      **全项目只有那一处判断**（`run_daily` / 门禁 / 菜单都靠它）。
      各写一份的话，迟早出现"菜单里藏了但任务还在跑"这种自相矛盾。
    """
    return config_io.store_profile(cfg, ROOT)


def session_path(cfg: dict) -> Path:
    """华为会话文件路径 —— **口径在 `modules/auth`**，这里只是入口层的名字。

    ⚠ 留这个薄转发（而不是各处直接 `auth.session_path`）是为了保住一个**接缝**：
      `tests/test_data_state.py` 等地方 `mock.patch.object(cli, "session_path")`，
      本文件里的调用点查的就是这个名字。实现只有一份，转发不算第二份真相。
    """
    return auth.session_path(cfg)


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


"""差异推送 / 邮件推送 —— **该不该推在这儿判，发出去走 `modules.notify`**。

用户 2026-09-19 划的分工：「**该不该推交给业务模块**，推送模块就只管接收
推送渠道编码和推送内容，然后推送出去，以及记录各个推送渠道」。

⇒ 所以下面这些 `should_send` / `--no-push` 判断**留在原处**（这是业务口径），
  但真正发出去那一步改走 `notify.send(渠道编码, 内容)` —— 它会在 `run_record`
  里记一笔（`notify:wecom` / `notify:mail`），"上次推成没成"于是看得见。

⚠ 还没搬的那半：这两个函数**是业务逻辑，却住在入口层**（`cli.py`）——
  和 `app/pos.py` 那条链一样的搬法（搬到 `app/` 去），等它们有第二个调用方时再动。
"""


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

    r = notify.send("wecom", {"template": "report", "ctx": ctx,
                              "missing": res.missing,
                              "reverse_unshipped": res.reverse_unshipped,
                              "matched": len(res.matched),
                              "total": len(res.matched) + len(res.missing),
                              "report_path": report_path},
                    cfg=cfg, root=ROOT)
    if not r["ok"]:
        print(f"\n[6/6] 企微：❌ 推送失败 —— {r['why']}", file=sys.stderr)
        print("      （对账本身是成功的，退出码不受影响）", file=sys.stderr)
        return
    print(f"\n[6/6] 企微：✅ {r['why']}")


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
    r = notify.send("mail", {"subject": subject, "body": body,
                             "attachments": [str(report_path)]},
                    cfg=cfg, root=ROOT)
    if not r["ok"]:
        # 对账结果是主产物，邮件只是投递方式 —— 邮件挂了要大声说，但别把整件事判成失败
        print(f"\n[5/6] 邮件：❌ 发送失败 —— {r['why']}", file=sys.stderr)
        print("      （对账本身是成功的，退出码不受影响）", file=sys.stderr)
        return
    print(f"\n[5/6] 邮件：✅ {r['why']}")


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


def cmd_mail_key_new(args) -> int:
    """生成一把邮件附件加密的密钥（**只在打包那台机器上跑**）。

    用户 2026-09-21：「解密密钥**随着大版本的安装包走，不进入小版本推包**」
    ⇒ 这条链是：**这儿生成** → `tools/build_package.sh` 把它拷进包根 →
    门店双击 `install.bat` 时由 `bootstrap._seed_mail_key()` 播进 `.secrets/` →
    `.secrets/` 在自更新的 `NEVER_TOUCH` 里 ⇒ **之后升级永远不动它**。

    ⚠ 门店机器上**不用跑这个** —— 它们的密钥是装包时播下来的。
    ⚠ 生成之后**别再改**：改一把发出去的密钥 = 换密钥 = 所有老包解不开
      （`pending/` 里压着的、邮箱里的老邮件都算）。
    """
    from . import mailcrypto
    got = mailcrypto.new_key(ROOT, key_id=getattr(args, "key_id", "") or "",
                             note=getattr(args, "note", "") or "")
    if not got.get("ok"):
        print(f"❌ {got.get('why')}", file=sys.stderr)
        return EXIT_USAGE
    print(f"✅ 生成密钥 {got['key_id']} → {got['path']}")
    print()
    print("下一步：打正式包时 tools/build_package.sh 会把它塞进包根（mail-key.json），")
    print("       门店装包时由 bootstrap 播进 .secrets/，之后自更新不会动它。")
    print("⚠ 这把密钥**不要提交进仓库**（仓库是公开的）—— .gitignore 已经挡住。")
    return EXIT_OK


def cmd_mail_key_show(args) -> int:
    """看邮件附件加密的密钥状态 —— ⚠ **不打印密钥本体**（它会进日志/截图）。"""
    from . import mailcrypto
    d = mailcrypto.describe(ROOT)
    print(f"密钥文件：{d['path']}")
    if d.get("ok"):
        print(f"当前密钥：{d['key_id']}"
              f"（本机共 {len(d.get('keys') or [])} 把：{'、'.join(d.get('keys') or [])}）")
        print("走邮件渠道的推送，附件都会用它加密（正文照旧是明文，能直接看）。")
    else:
        print(f"⚠️ 没有可用的密钥：{d.get('why')}")
        print("   ⇒ 邮件附件会**明文发出去**，正文末尾会写一行说明。")
        print("   ⇒ 3.0.0 之前的机器靠自更新升上来时就是这样：")
        print("     把带密钥的完整安装包再拷一次、双击 install.bat。")
    return EXIT_OK


def cmd_release_key_new(args) -> int:
    """生成**更新包的**加密密钥（路线 A）—— **只在打包那台机器上跑一次**。

    链路跟邮件那把同构，但**钥匙是分开的**（见 `mailcrypto.RELEASE_KEY_REL`）：

        这儿生成 → build_package.sh 塞进包根 release.key
        → 门店 install.bat 由 bootstrap._seed_release_key() 播进 .secrets/
        → publish_release.sh 用它**加密**公开仓上的更新包
        → 门店 selfupdate 下载后用 .secrets/release.key 解开

    ⚠ **公开资产里永远没有这把钥匙**（`publish_release.sh` 的 `BAN_NAMES` 挡着）。
    ⚠ 生成之后**别再改**：改一把发出去的密钥 = 所有已经发出去的密文包解不开。
    ⚠ 门店机器上**不用跑这个** —— 他们的钥匙是装包时播下来的。
    """
    from . import mailcrypto
    got = mailcrypto.new_key(ROOT, key_id=getattr(args, "key_id", "") or "",
                             note=getattr(args, "note", "") or "",
                             rel=mailcrypto.RELEASE_KEY_REL)
    if not got.get("ok"):
        print(f"❌ {got.get('why')}", file=sys.stderr)
        return EXIT_USAGE
    print(f"✅ 生成更新包密钥 {got['key_id']} → {got['path']}")
    print()
    print("下一步：打正式包时 tools/build_package.sh 会把它塞进包根（release.key），")
    print("       门店装包时由 bootstrap 播进 .secrets/，之后自更新不会动它。")
    print("⚠ 它**不进仓库、也不进公开资产** —— .gitignore 和发布脚本两道都挡着。")
    return EXIT_OK


def cmd_release_key_show(args) -> int:
    """看更新包密钥状态 —— ⚠ **不打印密钥本体**（进日志/截图就等于泄露）。"""
    from . import mailcrypto
    d = mailcrypto.describe(ROOT, rel=mailcrypto.RELEASE_KEY_REL)
    print(f"密钥文件：{d['path']}")
    if d.get("ok"):
        print(f"当前密钥：{d['key_id']}"
              f"（本机共 {len(d.get('keys') or [])} 把：{'、'.join(d.get('keys') or [])}）")
        print("公开仓上的更新包用它加密 —— 这台机器能正常自更新。")
    else:
        print(f"⚠️ 没有可用的密钥：{d.get('why')}")
        print("   ⇒ 这台机器**解不开公开仓上的密文更新包**，自更新会失败。")
        print("     把 release.key 放进 .secrets\\，或拿一次完整安装包重装。")
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


def selftest_data_lines(root=None) -> tuple:
    """自检第 8 项的内容 —— 返回 `(要打印的行, 要不要记成失败)`。

    ⚠ **只有"采集失败"才算失败**：`missing`（刚装完还没抓过）和 `stale`
    （数据旧了）都是**信息**，不该让"装完自检"直接不通过 ——
    门店第一次跑 selftest 时库里本来就是空的。
    """
    from .app import data_state as ds
    from .storage import migrate as _mig
    from .storage import db as _db
    lines = []
    # 结构版本（M15 / 阶段 3.6）—— 以前 `meta.schema` 写了没人读，现在它是权威
    try:
        db_path = ds._find_db(root or ROOT)
        if db_path:
            conn = _db.read_only(db_path)
            try:
                lines.append("结构：" + _mig.describe(_mig.status(conn)))
            finally:
                conn.close()
    except Exception as e:                                     # noqa: BLE001
        lines.append("结构：读不出来（%s: %s）" % (type(e).__name__, e))
    st = ds.data_state(root or ROOT)
    lines += ds.summary_lines(st)
    failed = any(x["state"] == ds.FAILED for x in st["sources"])
    if not failed and not st["ok"]:
        lines.append("（都不算故障：没抓过 / 数据旧了，跑一次日常流程就好）")
    # 第 8 项顺带一条：**周度目标那份腾讯文档读得到吗**（M2–M5 的数据源）。
    # ⚠ 它**不算故障**：办公室周一早上可能还没改表、门店也可能断网 ——
    #   这两件事都不该让"装完自检"不通过。但要**说清是网络还是文档**，
    #   不然门店只会看到"销售达成那一页是空的"。
    lines.append(_attain_doc_line())
    return lines, failed


def _attain_doc_line() -> str:
    """腾讯文档读不读得到 —— 一句话（**只读一次、失败也算信息**）。"""
    try:
        from .features.sales.attain import attain as _attain
        plan = _attain.read_plan(ROOT)
    except Exception as e:                                     # noqa: BLE001
        msg = str(e)
        # 网络类错误单独说 —— 门店看到"读不到目标表"会以为表被删了
        if any(k in msg for k in ("Timeout", "timed out", "Connection", "Max retries",
                                  "getaddrinfo", "SSL", "ProxyError")):
            return "周度目标：连不上腾讯文档（网络？）—— 销售达成那一步会跳过"
        return "周度目标：读不到（%s）—— 销售达成那一步会跳过" % msg[:60]
    return ("周度目标：%s（%s ~ %s，%d 家门店 %d 个产品列）"
            % (plan.period, plan.start, plan.end, len(plan.stores), len(plan.columns)))


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
    # **区长分区体检**（2026-09-21 用户定的 B 方案：区长按**区域**圈店）——
    # ⚠ 这一条盯的是"静默看不见"：区域名打错一个字，那个区长**一家店都看不到**，
    #   而界面上只会显示"没有数据"，没人会想到是配置写错了。
    # ⚠ 门店机器上通常没有区长名单 ⇒ 这一块一个字都不打印。
    try:
        _mgrs = config_io.managers_table(ROOT)
        _probs = config_io.region_audit(ROOT) if _mgrs else []
    except Exception:                                          # noqa: BLE001
        _mgrs, _probs = [], []
    if _mgrs:
        if _probs:
            print(f"  区长分区 ⚠️ {len(_mgrs)} 位区长，有 {len(_probs)} 条要处理：")
            for _p in _probs:
                print("          · " + _p)
            if any("一家店都看不到" in _p for _p in _probs):
                failures.append("区长分区配错（有区长一家店都看不到）")
        else:
            _regs = config_io.regions_of(config_io.stores_table(ROOT))
            print(f"  区长分区 {len(_mgrs)} 位 · {len(_regs)} 个区（{'、'.join(_regs)}）"
                  " —— 按区域圈店，没重没漏")
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
    # **邮件附件加密的密钥**（2026-09-21）—— 「密钥随大版本安装包走，不进小版本推包」，
    # 而自更新（小版本推包）**不带它** ⇒ **靠自更新升上来的机器是没有密钥的**，
    # 得人工把完整包再拷一次。⚠ 这件事必须**看得见**：不然门店升完只看到
    # "附件怎么还是明文"，而没有任何地方说得清为什么。
    # ⚠ 它**不算 failures**：附件不加密不影响对账；算成"自检失败"会让门店
    #   以为程序坏了，跑去修一个没坏的东西（而真正该做的是重拷一次包）。
    from . import mailcrypto as _mailcrypto
    try:
        _mk = _mailcrypto.describe(ROOT)
    except Exception as _e:                                    # noqa: BLE001
        _mk = {"ok": False, "why": "读不出来：%s" % _e, "key_id": "", "keys": []}
    if _mk.get("ok"):
        print(f"  邮件加密 密钥 {_mk['key_id']}"
              f"（本机 {len(_mk.get('keys') or [])} 把）—— 走邮件的附件都会加密")
    else:
        print("  邮件加密 ⚠️ 没有密钥 —— 邮件附件**不会加密**（明文发出去）")
        print(f"          {_mk.get('why') or ''}")
        print("          3.0.0 之前的机器靠自更新升上来时就是这样：")
        print("          把带密钥的完整安装包再拷一次、双击 install.bat 就有了。")
    # ⚠ **更新包密钥**（路线 A，2026-10-02）—— 公开仓上是密文包，靠它解开。
    #   ⚠ 这里**不算 failures**：过渡期明文那份还在（`_ASSET_ORDER` 会退回 `.zip`），
    #     所以没钥匙暂时还升得上去；等明文撤了，`selfupdate` 的报错会自己说清楚
    #     （`mailcrypto.unseal` 那句"把 release.key 放进 .secrets"）。
    #     现在就判失败，会让一批还没拿到钥匙的机器在自检里红一片、而其实能用。
    try:
        _rk = _mailcrypto.describe(ROOT, rel=_mailcrypto.RELEASE_KEY_REL)
    except Exception as _e:                                    # noqa: BLE001
        _rk = {"ok": False, "why": "读不出来：%s" % _e, "key_id": "", "keys": []}
    if _rk.get("ok"):
        print(f"  更新包密钥 {_rk['key_id']}"
              f"（本机 {len(_rk.get('keys') or [])} 把）—— 公开仓的密文更新包解得开")
    else:
        print("  更新包密钥 ⚠️ 没有 —— 明文更新包撤掉之后**自更新会失败**")
        print(f"          {_rk.get('why') or ''}")
        print("          解法：把 release.key 放进 .secrets\\，或拿一次完整安装包。")
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

    # ⚠ 中断的升级必须**在人看得见的地方**说出来：否则程序会静默跑在
    #   "一半新一半旧"的代码上（这正是加 journal 要解决的事）。
    head(7, "代码完整性")
    from . import selfupdate
    _lines = selfupdate.integrity_lines(ROOT)
    if _lines:
        for _ln in _lines:
            print("  " + _ln)
        failures.append("上次升级没走完")
    else:
        print("  正常（没有断在半路的升级）")

    # ⚠ 五态里**只有"采集失败"算故障** —— 刚装完（missing）或数据旧了（stale）
    #   都是信息。门店第一次跑自检时库里本来就是空的（M14 / 阶段 3.3）。
    head(8, "数据能不能算")
    _lines, _failed = selftest_data_lines(ROOT)
    for _ln in _lines:
        print("  " + _ln)
    if _failed:
        failures.append("有数据源采集失败")

    print()
    if failures:
        print("自检发现问题：" + "、".join(failures))
        print("照着上面的提示处理；搞不定就把这段输出发回来。")
        return EXIT_FETCH
    print("自检通过，可以开始用了。")
    return EXIT_OK


def cmd_update(args) -> int:
    """升级 / 修复没走完的升级 —— **服务起不来时唯一还能用的那条路**。

    不带参数：把现场念一遍（有没有断在半路的升级）。
    """
    from . import selfupdate
    try:
        if args.restore:
            res = selfupdate.repair(ROOT, current=version.VERSION, mode="restore")
            if not res.get("ok"):
                print("❌ " + str(res.get("message") or "恢复失败"))
                return EXIT_FETCH
            print("✅ " + res["message"])
            print("   现在双击 start.bat 启动控制台。")
            return EXIT_OK
        if args.repair:
            res = selfupdate.repair(ROOT, current=version.VERSION)
            print(f"✅ 重跑完成：v{res.get('to') or '?'}，"
                  f"改了 {res.get('count') or 0} 个文件")
            if res.get("removed"):
                print(f"   清掉 {len(res['removed'])} 个旧文件（都在 "
                      f"{res.get('backup') or '备份目录'} 里）")
            print("   现在双击 start.bat 启动控制台。")
            return EXIT_OK
    except selfupdate.PartialUpdate as e:
        print(f"❌ 又断在半路：{e}")
        print("   现场还在，可以再跑一次 --repair，或者改用 --restore 退回上一版。")
        return EXIT_FETCH
    except selfupdate.UpdateError as e:
        print(f"❌ {e}")
        return EXIT_FETCH

    lines = selfupdate.integrity_lines(ROOT)
    if not lines:
        print("没有没走完的升级。")
        print("（平时升级在控制台「设置 → 检查更新」里点；命令行这条路是给"
              "「服务起不来」准备的。）")
        return EXIT_OK
    print("\n".join(lines))
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


#: 计划任务那份脚本留下的日志（`out/run.log`）—— 和 `schedule.LOG_NAME` 同一个文件名。
#: ⚠ 写死在这儿而不是 import `schedule`：那个模块会 import 平台细节，
#:   而这条命令要在**任何**平台上都能跑（包括没注册过任务的机器）。
LOG_NAME = "run.log"


def cmd_ensure_service(args) -> int:
    """**确保服务在跑**（不在跑就拉起来）—— 系统计划任务现在只干这件事。

    用户 2026-09-20 定的分工：**内置定时器负责干活**（它就在服务里，
    到点自己叫醒各功能模块），系统计划任务**降级成兜底** ——
    服务被关了 / 机器刚起来而自启没生效时，把它拉起来。

    ⚠ 为什么不让计划任务继续直接跑 `daily`：
      两边都会跑 ⇒ 一天两遍（云商不能并行登录，撞上就是四个域一起超时）。
      "谁干活"只能有一个答案，现在是**服务里的定时器**。

    ⚠ 已经在跑就直接退出（**退出码 0**）—— 这是"没事发生"，不是失败。
    """
    from . import service
    got = service.find_running(ROOT)

    # 往 out/run.log 留一行 —— 计划任务那边**只看得到这份日志**。
    # ⚠ 原来这行是 bat 自己 echo 的（"run.bat launching"），现在判断"在不在跑"
    #   要读状态文件 + 探接口，bat 干不了，所以连同这行痕迹一起搬进 Python。
    def _trace(what: str) -> None:
        try:
            log = ROOT / "out" / LOG_NAME
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "a", encoding="utf-8", errors="replace") as f:
                f.write("[%s] ensure-service：%s\n"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), what))
        except OSError:
            pass

    if got:
        url = f"http://{got.get('host', '127.0.0.1')}:{got.get('port', 8787)}/"
        print(f"服务已经在跑（{url}）—— 计划任务这一步什么都不用做。")
        _trace("服务已经在跑，什么都不用做")
        return EXIT_OK
    print("服务没在跑 —— 拉起来（到点由服务里的定时器干活）。")
    _trace("服务没在跑 —— 拉起来")
    from .web import serve
    # ⚠ 不 `open_browser=False` 的话，系统计划任务到点会**弹出浏览器窗口** ——
    #   门店半夜看到浏览器自己开了，只会当成中毒。
    serve(port=8787, host="127.0.0.1", open_browser=False, root=ROOT, config=args.config)
    return EXIT_OK


def cmd_serve(args) -> int:
    # 开控制台也算一次"启动" —— 更新完马上打开控制台的话，这里就能记上升级。
    # ⚠ 同一版只推一次（`upgrade` 里记着 `pushed`），所以跟 daily 不会重复推。
    _upgrade_check(args.config)
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
                # ⚠⚠ **改走 notify**（2026-09-21 接加密时改的）：邮件附件要在
                #   **推送模块**里加密（`notify.seal_attachment`），而直接调
                #   `mailer.send` 会**绕过加密** —— bug 包里恰恰带着业务数据
                #   （串号 / 金额），正是该保护的那种。
                #   ⚠ 顺带修掉一个真 bug：`_send_mail` 原来写的是
                #   `content.get("prefix") or None`，`""` 会被吃掉 ⇒ 主题上
                #   又套回 `[报量对账]`（直接调 mailer 时看不出来）。
                res = notify.send("mail", {"subject": subject, "body": body,
                                           "attachments": [path], "prefix": ""},
                                  cfg=cfg, root=root)
                if res.get("state") != notify.SENT:
                    raise RuntimeError(res.get("why") or "发送失败")
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


def _upgrade_check(config_path, *, push=True) -> dict:
    """比对"上次跑的是哪一版"：是升级就记一条；该推就把「要做的事」推出去。

    ⚠ **任何异常都不许冒泡** —— 升级提醒是锦上添花，
    为了它把每天的对账搞失败是本末倒置。（`upgrade.check` 内部已经全吞了，
    这里再兜一层，因为连 `load_config` 都可能抛。）
    """
    try:
        from . import upgrade
        cfg = None
        try:
            cfg = load_config(config_path, root=ROOT)
        except BaseException:                                 # noqa: BLE001
            # ⚠ `load_config` 找不到配置文件时抛的是 **SystemExit**，它是
            #   `BaseException` —— 只接 `Exception` 的话会**穿过去**，
            #   在 `daily` 第一行就把整条流程带走（见 AGENTS.md 坑 11）。
            pass
        return upgrade.check(ROOT, cfg, version.VERSION, push=push)
    except BaseException as e:                                # noqa: BLE001
        return {"checked": False, "change": None, "pushed": False,
                "why": "", "result": "升级检测跳过：%s" % e}


def _maybe_rebuild_db() -> None:
    """2.1.0 的一次性库重建 —— **只做一次**，之后永远跳过。

    见 `src/dbmigrate.py`：**是"改名归档"不是删除**（万一后悔改回来就行）。

    ⚠ 挂在这儿而不是 `selfupdate`：`out/` 是自更新的 `NEVER_TOUCH` 红线
    （AGENTS.md 坑 5「自更新只许碰代码」），而且"更新代码时顺手删业务数据"
    本来就危险。挂在每次 `daily` 的开头，失败了下一次还会再试。
    """
    from . import dbmigrate
    if dbmigrate.done(ROOT):
        return
    r = dbmigrate.rebuild_once(ROOT)
    if r.get("archived"):
        print("⚠ 2.1.0 库重建：老的订单库已**改名归档**（没有删）——")
        for old, new in r["archived"]:
            print("    %s → %s" % (Path(old).name, Path(new).name))
        print("    确认没问题后可以自己删掉那些 `.%s-*` 文件。" % dbmigrate.SUFFIX)
        print("    ⚠ 下一步「抓取玲珑数据」会重新抓一遍 —— **第一次会很慢（几分钟）**，"
              "而且在那之前 POS 看板是空的。")
    elif not r.get("skipped"):
        print("⚠ 2.1.0 库重建没做成：%s（下次启动还会再试）" % r.get("reason"),
              file=sys.stderr)


def cmd_daily(args) -> int:
    """日常流程 —— 一条定时任务跑完三步（编排在 `run_daily` 里）。

    ⚠ 第 1 步失败会**跳过第 2、3 步**（理由写在 `run_daily` 的模块注释里）。

    ⚠ 参数**逐个转发**，别写"够用就行"的子集：老门店的 `run.bat` 里是 `check`，
    把它迁到 `daily` 时，`check` 认得的参数在这里必须都接得住
    （子解析器是超集，见 `daily` 那段）。
    """
    from . import run_daily
    try:
        run_daily.requested_steps(getattr(args, "steps", ""))
    except ValueError as e:
        print("❌ %s" % e, file=sys.stderr)
        return EXIT_USAGE

    # ⚠ 一次性库重建（2.1.0）—— 必须在其他有效命令的动作**之前**，
    #   因为它会把库改名，后面所有读库的步骤都得看到"新世界"。
    _maybe_rebuild_db()

    # 「升级记录 + 大版本升级就推提醒」挂在这儿，因为**这是每天都会跑的那条**：
    # 门店更新完，第二天的定时任务就会检测到并把"要做的事"推出去。
    # （光靠控制台弹窗不够 —— 门店的日常是"它自己跑，我不看"。）
    _upgrade_check(args.config)

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
    # ⚠ 这两个是**内置定时器那条路**的参数，必须原样转发 ——
    #   漏了的话定时器派发出来的命令会变成"整批跑"，而且日志里看不出来。
    for flag, val in (("--steps", getattr(args, "steps", "")),
                      ("--wake-slot", getattr(args, "wake_slot", ""))):
        if val:
            argv += [flag, str(val)]
    # ⚠ 老的 `--skip-*` **原样转发**（内层 `daily` 收下 → 提醒一句"它没用了" → 忽略）：
    #   漏掉一个的话，老脚本里那个开关会在**这一层**就被拒（unrecognized arguments），
    #   而门店看到的是"双击了一下，窗口一闪就没了"。
    from .features.registry import step_flags as _step_flags
    for _flag in _step_flags().values():
        if getattr(args, _flag.lstrip("-").replace("-", "_"), False):
            argv.append(_flag)
    for flag, on in (("--skip-check", getattr(args, "skip_check", False)),
                     ("--no-mail", args.no_mail),
                     ("--no-push", args.no_push),
                     ("--no-refresh", args.no_refresh)):
        if on:
            argv.append(flag)
    if getattr(args, "verbose", False):
        argv.append("-v")
    t0 = time.time()
    rc = run_daily.main(argv)
    # ⚠ 内置定时器那一趟：**在这儿记**（不在 `run_daily.main` 里）——
    #   那边有好几个提前 return（第 1 步失败、合作店早退），
    #   而"失败的那一趟"恰恰是最该记的：不记就会被每 30 秒重派一次。
    if getattr(args, "wake_slot", ""):
        run_daily.record_wake(getattr(args, "steps", ""), args.wake_slot, rc,
                              seconds=time.time() - t0)
    return rc


def _record_fetch(kind: str, ok: bool, *, why: str = "", rows: int = 0,
                  started=None, conn=None) -> None:
    """记一次采集**尝试**（M14 / C 项的 3.2）—— **失败也记**。

    ⚠ 以前失败时库里一行都不写 ⇒ "没跑"和"跑失败了"长得一模一样，
    门店在界面上只会看到"还是昨天那份"（`out/pools-<年>.json` 只有成功才写）。

    ⚠ **为什么埋在编排层（`cli`）而不是 `dump`/`pools` 内部**：那两个函数里
    有六七处早期返回（没会话 / ping 不过 / 窗口定不了…），挨个埋点必漏；
    而"什么时候算一次尝试"本来就是编排的事。

    ⚠ **绝不抛**：记不上不能影响抓取本身（跟 `startup` 那套一个道理）。
    """
    from . import dump as dumpmod
    from .app import data_state as ds
    try:
        own = conn is None
        if own:
            db = _find_pos_db() or dumpmod.year_db(ROOT / "out", datetime.date.today().year)
            conn = dumpmod.connect(str(db))
        try:
            ds.record_attempt(conn, kind, ok, why=why, rows=rows,
                              started_at=started, finished_at=datetime.datetime.now(CST))
        finally:
            if own:
                conn.close()
    except Exception:                                          # noqa: BLE001
        pass


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
    if getattr(args, "year", 0):
        # ⚠ **第一次建库走这条**（用户 2026-09-17 定：「跨年不重要，就拉当年的全量」）。
        #   原来第一次跑传的是 `--all`（不传时间窗 = 全部历史），
        #   但 `dump.py` 拿到跨年数据会**直接报错**要求按年分次抓 ——
        #   门店第一次跑正好会卡在这儿。只抓当年就没这问题。
        argv += ["--year", str(args.year)]
    elif args.all:
        argv.append("--all")
    else:
        argv += ["--month", args.month or "current"]
    _t0 = datetime.datetime.now(CST)
    try:
        rc = dumpmod.main(argv)
    except Exception as e:                                     # noqa: BLE001
        _record_fetch("dump", False, why="%s: %s" % (type(e).__name__, e), started=_t0)
        raise
    _record_fetch("dump", rc == 0,
                  why="" if rc == 0 else "退出码 %d（看上面的报错）" % rc, started=_t0)
    if rc != 0:
        # ⚠ 华为没抓到就**不再往下抓** —— 保持"第 1 步失败 → 整条流程中止"的语义，
        #   免得后面对着一份半新不旧的库算对账。
        return rc

    # 顺手把**玲珑那边**的池子也拉了（池A = 刚抓的华为订单，池B = 玲珑在库）。
    #
    # ⚠⚠ 2026-09-20 **改过**（用户：「数据抓取再加个**云商数据定时抓取**吧」）：
    #   **云商那两个池子（池C 销售 / 池D 在库）挪去 `erp-dump` 了** ——
    #   它们跟玲珑是两件独立的事，各自要能单独设时间、单独开关。
    #   原来是"顺手"一起拉的，界面上看不出来，也没法"只重抓云商"。
    #   ⚠ 只有 `--fetch` 时 `cmd_pools` 才**只抓不算**（不算象限、不推送）——
    #   对账和推送是后面那几步的事。
    print()
    print("— 顺带抓玲珑在库（池B）—")
    return cmd_pools(argparse.Namespace(
        config=args.config, fetch=["lg-stock"],
        start="", end="", date="", days_ago=0,
        no_refresh=True, no_push=True, no_mail=True,
        verbose=getattr(args, "verbose", False)))


def cmd_erp_dump(args) -> int:
    """**抓云商数据**（池D 云商在库 + 池C 云商销售）→ 同一个库。

    用户 2026-09-20：「数据抓取再加个**云商数据定时抓取**吧」——
    这一步以前是**混在 `dump` 里顺手做**的（`cmd_dump` 末尾那段），
    现在拆出来：能在「定时器设置」里单独设时间、单独开关，
    也能单独跑一次（`python -m src.cli erp-dump`）。

    ⚠ 跟 `dump` 一样**只抓不算**（`fetch=[...]` + `--no-push --no-mail`）：
    算象限和推送是「双平台数据对比」那一步的事。
    """
    print("— 抓云商数据（云商在库 池D / 云商销售 池C）—")
    rc = cmd_pools(argparse.Namespace(
        config=args.config, fetch=["erp-stock", "erp-sales"],
        start="", end="", date="", days_ago=0,
        no_refresh=True, no_push=True, no_mail=True,
        verbose=getattr(args, "verbose", False)))
    # ⚠⚠ **两个池各报各的**（用户 2026-09-21 问过：「为啥显示失败但是数据刷新了」）——
    #   这一步是"在库 + 销售"两件事，**一个成一个败时整步算失败**，
    #   但成功那个池的数据**已经落库了**。不写清的话，日志只有一句"失败"，
    #   看起来像"什么都没更新"（实际上池D 更新了）。
    try:
        from .app import data_state as _ds
        st = {x["key"]: x for x in _ds.data_state(ROOT).get("sources", [])}
        for key, label in (("erp-stock", "池D 云商在库"), ("erp-sales", "池C 云商销售")):
            one = st.get(key) or {}
            ok = one.get("state") == "ok"
            print("   %s %s：%s%s" % ("✓" if ok else "✗", label,
                                      one.get("state_label") or "?",
                                      "" if ok else "（%s）" % (one.get("why") or "")))
    except Exception as e:                                     # noqa: BLE001
        print("   （两个池各自的状态没读出来：%s: %s）" % (type(e).__name__, e))
    if rc != 0:
        print("   ⚠ 整步算失败（后面那几步要用**这两个池**）——"
              "但上面 ✓ 的那个池**数据已经更新了**，✗ 的那个没进来。")
    return rc


def cmd_autoupdate(args) -> int:
    """**自动更新**（定时器每小时叫醒的那一步）。

    用户 2026-09-20：「健康模块默认注册一个自动更新，**固定一个小时执行一次**」。

    ⚠ 这里只是"命令行入口"，判断和动手都在 `modules/health/auto_update()`：
      查新版本 → 过策略（`update_plan`：健康 + 今天那趟跑完 + 没有半截升级）→
      铺代码 → 起小助手重启服务。
    ⚠ **没有新版本是绝大多数情况**（一天 24 次里 23 次都是）—— 那时候要安静地退 0，
      不能报错、也不能刷一堆日志。
    """
    from .modules import health
    res = health.auto_update(ROOT, version.VERSION, emit=print)
    if res.get("action") == "updated":
        print("自动更新：%s" % res.get("why"))
        return EXIT_OK
    if res.get("ok"):
        # 已经是最新 / 策略说等等 —— 都不是错
        return EXIT_OK
    print("❌ 自动更新没跑成：%s" % (res.get("why") or "未知原因"), file=sys.stderr)
    return EXIT_FETCH


def cmd_pos(args) -> int:
    """算 POS 合规率 → 落 `out/pos-<年>.json`（**看板只读它，不现场算**）。

    算完**单独推一条** POS 合规（和报量排查分开两条）——
    见 `app.pos.notify`。用 `--no-push --no-mail` 可以只算不发。

    ⚠ 真正的活在 **`src/app/pos.py`** 里（阶段 2 / B 项试点）：这里只管
    「参数 → 执行 → 退出码」。`daily` 和控制台走的是**同一个**执行模块，
    所以"手动跑出 44.7%、daily 跑出别的"这类不一致从结构上就不可能。
    """
    from .app import pos as pos_app
    res = pos_app.run(db=getattr(args, "db", "") or "",
                      config_path=getattr(args, "config", None),
                      no_push=getattr(args, "no_push", False),
                      no_mail=getattr(args, "no_mail", False),
                      emit=print)
    if not res.ok:
        print(f"❌ {res.why}\n   先抓一次：python -m src.cli dump", file=sys.stderr)
        return EXIT_FETCH
    return EXIT_OK


def cmd_attain(args) -> int:
    """算周度销售达成 → 落 `out/attain-<年>.json`（**看板只读它，不现场算**）。

    ⚠ 真正的活在 `features/sales/attain/`（口径在 `metric.py`，纯函数）：
      这里只管「参数 → 执行 → 退出码」，和 `cmd_pos` 一个形状。
    """
    from .features.sales.attain import attain as attain_mod
    store = getattr(args, "store", "") or ""
    if not store:
        # 门店端只看自己那一行；办公室 / 平台岗看全区（M2–M5 设计 §5.3）
        try:
            cfg = load_config(args.config)
            if config_io.store_profile(cfg, ROOT).get("type") != "platform":
                store = cfg.get("erp_store_name") or ""
        except SystemExit:
            store = ""
    res = attain_mod.run(db=getattr(args, "db", "") or "", root=ROOT,
                         store_filter=store, config_path=args.config,
                         no_push=getattr(args, "no_push", False),
                         no_mail=getattr(args, "no_mail", False), emit=print)
    if not res.get("ok"):
        print(f"[销售达成] 没算成：{res.get('why')}", file=sys.stderr)
        return EXIT_FETCH
    return EXIT_OK


def cmd_plan(args) -> int:
    """算**月度生意计划** → 落 `out/plan-<年>.json`（**看板只读它，不现场算**）。

    ⚠ 真正的活在 `features/plan/`（口径在 `monthly/metric.py`，纯函数）：
      这里只管「参数 → 执行 → 退出码」，和 `cmd_pos` / `cmd_attain` 一个形状。
    ⚠ 它算的是**全部 28 家店**（名单内）—— 按身份的过滤在**接口层**做，
      不在这儿（落盘里只有本店的话，区长/平台那份还得再算一次）。
    """
    from .features.plan.monthly import plan as plan_mod
    res = plan_mod.run(db=getattr(args, "db", "") or "", root=ROOT, emit=print)
    if not res.get("ok"):
        print(f"[月度生意计划] 没算成：{res.get('why')}", file=sys.stderr)
        return EXIT_FETCH
    return EXIT_OK


def cmd_film(args) -> int:
    """防护膜达成 —— 算一遍并落 `out/film.json` 快照（页面本身是现算的）。"""
    from .features.valueadd.film import compute as film_compute
    res = film_compute.run(root=ROOT, emit=print)
    return EXIT_OK if res.get("ok") else EXIT_FETCH


def cmd_report(args) -> int:
    """**把当天新增上报给区长**（M18）—— 打包 + 发信 + 补发欠着的。

    ⚠ 真正的活在 `app/report.py`：这里只管「参数 → 执行 → 退出码」，和 `cmd_pos` 一个形状。
    ⚠ 手动跑这一条**不会**绕过渠道开关：邮件没开就明说"没发"（见那条模块注释第 2 条）。
      要"只生成不发"用 `--dry-run`。
    """
    from .app import report as report_mod
    res = report_mod.run(ROOT, config_path=args.config,
                         date=getattr(args, "date", "") or "",
                         no_push=getattr(args, "no_push", False),
                         force=getattr(args, "force", False),
                         dry_run=getattr(args, "dry_run", False), emit=print)
    if res.get("skipped"):
        print("[数据上报] 没发：%s" % res["skipped"], file=sys.stderr)
        return EXIT_OK                       # 「没发」不是失败（渠道关着 / --no-push）
    if not res.get("ok"):
        print("[数据上报] 没发出去：%s" % (res.get("why") or "？"), file=sys.stderr)
        return EXIT_FETCH
    return EXIT_OK


def cmd_report_inbox(args) -> int:
    """**收门店上报**（M19）—— 读邮箱里的 `[CBG上报]` 附件 → 落 `in/report.db`。

    ⚠ 这台机器没配收信（IMAP）就**跳过**（门店机器就是这样），退出码仍是 0：
      "没配"和"收失败了"是两件事（后者要看得到）。
    """
    from .app import report_inbox as inbox_mod
    res = inbox_mod.run(ROOT, config_path=args.config,
                        limit=int(getattr(args, "limit", 0) or inbox_mod.SCAN),
                        dry_run=getattr(args, "dry_run", False), emit=print)
    if res.get("skipped"):
        print("[收上报] 跳过：%s" % res["skipped"], file=sys.stderr)
        return EXIT_OK
    if not res.get("ok"):
        return EXIT_FETCH
    return EXIT_OK


def cmd_pos_export(args) -> int:
    """出 POS 明细 Excel。"""
    from .features.compliance.pos import pos_export
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
    """找 `out/` 里最新的那个 `cbg-<年>.db`。一年一个库，取年份最大的。

    ⚠ 实现在 `app.pos.find_db`（一个业务一份实现，别两处各写一遍）——
    这里留个名字是因为 `cli.py` 自己（dump 那步判断"第一次跑"）和测试都在用它。
    """
    from .app.pos import find_db
    return find_db(ROOT)


# --------------------------------------------------------------------- 双平台
def _maybe_pools_push(cfg: dict, conn, xlsx, counts: dict, args) -> None:
    """把双平台数据对比推出去 —— 邮件 / 企微各一条，并**更新推送记忆**。

    ⚠ **不受「只有差异才推」约束**：没有差异时也推一条"都对得上"，
    否则门店分不清"今天没差异"和"今天压根没跑"（跟 POS 同一个理由）。

    ⚠ **记忆只在真推成功之后才写**。先写的话，某天推送失败（网不通），
    第二天门店看到的会是"已推 2 次"的弱化行 —— **那条强调永远拿不到了**。
    """
    from . import mailer, pools_notify, wecom
    from . import pools as P

    ctx = {"门店": cfg.get("store_name") or cfg.get("store_code") or "?",
           "配置文件": str(args.config)}

    # 推送记忆：同一个串号连着几天推（批发单云商先报、玲珑过后才报），
    # 第二天起弱化并标"已推 N 次、上次几号" —— 见 `pools_notify` 顶部。
    sns = [r["sn"] for q in ("AD", "BC") for r in P.details(conn, q)]
    state = pools_notify.load(ROOT)
    marks = pools_notify.annotate(sns, state) if sns else {}
    n_new = sum(1 for m in marks.values() if m.get("new"))

    head = "双平台数据对比：AD %d 台 / BC %d 台" % (counts["AD"], counts["BC"])
    if sns:
        head += "，其中新出现 %d 台" % n_new
    lines = P.notify_lines(conn, marks=marks) or ["本次没有差异 —— 两边都对得上。"]
    has_diff = bool(counts["AD"] or counts["BC"])
    sent_ok = False

    if not getattr(args, "no_push", False):
        try:
            wc = wecom.load_wecom_config(cfg, ROOT)
            ok, why = wecom.should_send(wc, has_diff=has_diff, ignore_when=True)
            if not ok:
                print("[推送] 双平台企微：跳过（%s）" % why)
            else:
                r = notify.send("wecom", {"template": "pools", "ctx": ctx,
                                          "lines": lines, "head": head, "xlsx": xlsx},
                                cfg=cfg, root=ROOT)
                print("[推送] 双平台企微：%s %s" % ("✅" if r["ok"] else "❌", r["why"]),
                      file=sys.stdout if r["ok"] else sys.stderr)
                sent_ok = r["ok"]
        except Exception as e:                                # noqa: BLE001
            print("[推送] 双平台企微：❌ %s" % e, file=sys.stderr)
            print("      （清单已经算好了，退出码不受影响）", file=sys.stderr)

    if not getattr(args, "no_mail", False):
        try:
            mc = mailer.load_mail_config(cfg, ROOT)
            ok, why = mailer.should_send(mc, has_diff=has_diff, ignore_when=True)
            if not ok:
                print("[推送] 双平台邮件：跳过（%s）" % why)
            else:
                subject, body = mailer.build_pools_mail(ctx, lines, head, has_attach=True)
                r = notify.send("mail", {"subject": subject, "body": body,
                                         "attachments": [str(xlsx)],
                                         "prefix": mailer.POOLS_SUBJECT_PREFIX},
                                cfg=cfg, root=ROOT)
                print("[推送] 双平台邮件：%s %s" % ("✅" if r["ok"] else "❌", r["why"]),
                      file=sys.stdout if r["ok"] else sys.stderr)
                sent_ok = sent_ok or r["ok"]
        except Exception as e:                                # noqa: BLE001
            print("[推送] 双平台邮件：❌ %s" % e, file=sys.stderr)
            print("      （清单已经算好了，退出码不受影响）", file=sys.stderr)

    if sent_ok:
        # 顺手把"已经不在清单里"的扔掉（报量了/出库了 = 解决了）
        pools_notify.save(ROOT, pools_notify.remember(state, sns, P.today()))
        print("[推送] 已记住 %d 个串号（下次再出现会弱化并标上次日期）" % len(sns))
    elif sns:
        print("[推送] 记忆未更新（这一条没真发出去）", file=sys.stderr)


def _pool_day(args) -> datetime.date:
    """快照日期：`--date` 优先，`--days-ago` 次之，默认今天。"""
    if getattr(args, "date", ""):
        return datetime.date.fromisoformat(args.date)
    n = getattr(args, "days_ago", 0) or 0
    return datetime.date.today() - datetime.timedelta(days=n)


def cmd_pools(args) -> int:
    """四个数据池 —— 建库 / 拉取 / 看状态。

    池的定义和口径见 `src/pools.py` 顶部。这里只管**编排**：
    调哪个取数方法、落哪张表、什么时候轮转快照。

    ⚠ **同库**（`out/cbg-<年>.db`，跟对账/POS 是同一个）——
    对账时四张表直接 JOIN，不用 ATTACH。
    """
    from . import dump as dumpmod
    from . import pools as P

    db = _find_pos_db() or dumpmod.year_db(ROOT / "out", datetime.date.today().year)
    conn = dumpmod.connect(str(db))
    P.ensure(conn)
    dumpmod._run_migrations(conn)      # 结构迁移（幂等，见 storage/migrate.py）
    # ⚠ 配置**只用来推送**（门店名 / 邮件 / 企微）—— 看状态时不该强依赖它：
    #   没配置文件时"只看一眼双平台"必须照样能用（只有 `--fetch` 才真需要配置）。
    try:
        cfg = load_config(args.config)
    except SystemExit:
        cfg = {}

    if not args.fetch:
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
            # ⚠ 排除掉的**必须让人看得见** —— 不静默过滤
            print("      └ 另有 %d 台是样机，已排除"
                  "（云商卖了样机、玲珑那边报不了量，属于已知的正常情况）"
                  % q["BC_样机"])
        if not getattr(args, "quiet", False):
            print()
            print("  只在 A %d / 只在 B %d / 只在 C %d / 只在 D %d"
                  % (q["only_A"], q["only_B"], q["only_C"], q["only_D"]))
            print("  （「只在其中一个」大多是礼品/别的渠道/别的门店，不用管；"
                  "⚠ 但「只在 B」里可能藏着窗口没覆盖到的漏报）")

        # ★ 两个"有事"的象限，出到**串号 / 机型 / 门店 / 单号**这一级 ——
        #   这就是最后推送给门店、让他们照着处理的内容。
        for quad in ("AD", "BC"):
            rows = P.details(conn, quad)
            if not rows:
                continue
            print()
            print("★ %s %s：%d 台" % (quad, P.QUADRANT_LABELS[quad], len(rows)))
            print("-" * 70)
            for r in rows:
                print("  串号 %s" % r["sn"])
                for key, label in P.DETAIL_COLS:
                    if key in ("sn", "direction", "问题"):
                        continue
                    v = r.get(key)
                    if v not in (None, ""):
                        print("      %-12s %s" % (label, v))

        # 出 Excel + 推送 —— 明细就是**最后推给门店的那份**
        xlsx, counts = P.export_xlsx(conn, db.parent / ("双平台数据对比-%s.xlsx" % P.today()))
        print()
        print("清单已出：%s（AD %d 台 / BC %d 台）" % (xlsx, counts["AD"], counts["BC"]))

        # 记一份到历史 —— 控制台「报量查询」页翻的就是它。
        # ⚠ 明细存 `details()` 的原样行（串号/机型/门店/单号都在），
        #   前端和 Excel 用同一份，不另排一遍。
        from . import pools_history as pools_hist
        hp = pools_hist.save_day(
            ROOT, P.today(),
            {k: q.get(k, 0) for k in ("AD", "BC", "AC", "BD", "BC_样机")},
            P.details(conn, "AD"), P.details(conn, "BC"))
        print("历史已记：%s" % hp)

        _maybe_pools_push(cfg, conn, xlsx, counts, args)
        return 0

    rc = 0
    for pool in args.fetch:
        # ⚠ 每个池各记一次**尝试**（成功失败都记）—— 池名就是 `data_state` 里的来源名，
        #   两处对得上，"哪个池挂了"才判得出来（M14 的 C 项）。
        _t0 = datetime.datetime.now(CST)
        try:
            if pool == "lg-stock":
                one = _fetch_lg_stock(cfg, conn, args)
            elif pool == "erp-stock":
                one = _fetch_erp_stock(conn, args)
            elif pool == "erp-sales":
                one = _fetch_erp_sales(conn, args)
            else:
                print("❌ 不认识的池：%s（可选：%s）" % (pool, " / ".join(P.POOLS)),
                      file=sys.stderr)
                return 2
        except Exception as e:                                 # noqa: BLE001
            _record_fetch(pool, False, why="%s: %s" % (type(e).__name__, e),
                          started=_t0, conn=conn)
            raise
        _record_fetch(pool, one == EXIT_OK,
                      why="" if one == EXIT_OK else "退出码 %d" % one,
                      started=_t0, conn=conn)
        rc |= one

    purged = P.purge_snapshots(conn)
    hit = {k: v for k, v in purged.items() if v}
    if hit:
        print("快照轮转（保留 %d 天）：%s"
              % (P.SNAP_KEEP_DAYS, "、".join("%s 删 %d 行" % (k, v) for k, v in hit.items())))
    return rc


def _fetch_lg_stock(cfg: dict, conn, args) -> int:
    """池B 玲珑在库 —— 需要华为会话。"""
    from . import pools as P
    from .cbg import CbgError

    _, rc = require_session(cfg, verbose=getattr(args, "verbose", False),
                            allow_refresh=not getattr(args, "no_refresh", False))
    if rc is not None:
        return rc
    day = _pool_day(args)
    try:
        rows, total = make_client(cfg, verbose=getattr(args, "verbose", False)).inventory()
    except CbgError as e:
        print("❌ 玲珑在库拉取失败：%s" % e, file=sys.stderr)
        return EXIT_FETCH

    # ⚠ 完整性自检：接口自报 totalRows 跟实际拿到的不一致 = **少给了还不吭声**
    if total and len(rows) != int(total):
        print("⚠ 玲珑库存：接口自报 %s 行，实收 %d 行 —— 不一致，别当成功"
              % (total, len(rows)), file=sys.stderr)
        return EXIT_FETCH
    n = P.save_snapshot(conn, "lg-stock", rows, date=day.isoformat(), sn_field="sn")
    print("池B 玲珑在库 %s：%d 行" % (day, n))
    return 0


def _fetch_erp_stock(conn, args) -> int:
    """池D 云商在库 —— `InventoryImei_Excel`，一次拿全。

    ⚠ **只能当天跑**：导出接口传历史日期一律 504，`InventoryType` 也会被静默忽略。
    """
    from . import pools as P
    from .erp import ErpClient, ErpError

    day = _pool_day(args)
    try:
        rows = ErpClient(verbose=getattr(args, "verbose", False)).inventory_imei(day)
    except ErpError as e:
        print("❌ 云商在库拉取失败：%s" % e, file=sys.stderr)
        return EXIT_FETCH
    n = P.save_snapshot(conn, "erp-stock", rows, date=day.isoformat(), sn_field="imei")
    print("池D 云商在库 %s：%d 行" % (day, n))
    return 0


def _fetch_erp_sales(conn, args) -> int:
    """池C 云商销售单 —— 自动按 10 天切段。

    ⚠ 默认窗口是**当月**（跟 `dump` 的日常口径一致）：重拉当月能把 21:00 之后的
    更新收进来（`INSERT OR REPLACE` 只增不改）。**首次建库**要显式给
    `--start`/`--end` 拉全年。
    """
    from . import pools as P
    from .erp import ErpClient, ErpError

    today = datetime.date.today()
    # ⚠ **表空 = 第一次建库 → 拉本年度**，不能默认只拉当月。
    #   只拉当月的话，几天之前的 AD/BC 全都算不出来，而且**不报错** ——
    #   看着就像"以前没差异"。（跟池A 那条"没有本地库就抓全部历史"一个思路。）
    try:
        empty = conn.execute("SELECT COUNT(*) FROM erp_sales").fetchone()[0] == 0
    except Exception:                                         # noqa: BLE001
        empty = True
    if args.start or args.end:
        start = datetime.date.fromisoformat(args.start) if args.start else today.replace(day=1)
    elif getattr(args, "rewrite", False):
        # ⭐ **强制刷新**（2026-09-29 用户：「按新规则全部重写数据库」）：
        #   老月份要按当前口径整表重落 —— 典型是 2026-09-22 起无串号的贴膜/礼包
        #   行才入库，1–8 月一条都没有，只看日期窗口会看到"上月全是 0"。
        start = datetime.date(today.year, 1, 1)
        print("  ⚠ 强制刷新：按**当前口径**重抓本年度（%s ~ %s）→ 抓全后**整表重写**"
              % (start, today), flush=True)
    elif empty:
        start = datetime.date(today.year, 1, 1)
        print("  ⚠ 池C 还是空的 —— 第一次建库，拉本年度（%s ~ %s），会分几段，稍等" % (start, today))
    else:
        start = today.replace(day=1)
        # ⚠ **月初补上月**（用户 2026-09-17 提的）：见 `BACKFILL_DAYS` 的注释。
        if today.day <= BACKFILL_DAYS:
            start = (today - datetime.timedelta(days=1)).replace(day=1)
            print("  ⚠ 月初补漏：连上个月（%s 起）一起重拉 —— "
                  "上月最后一天跑完之后录进去的销售，只拉当月是补不到的" % start)
    end = datetime.date.fromisoformat(args.end) if args.end else today
    if end < start:
        print("❌ --start 比 --end 晚（%s > %s）" % (start, end), file=sys.stderr)
        return 2

    def prog(a, b, n):
        print("  ✓ %s ~ %s：%d 行" % (a, b, n), flush=True)

    try:
        rows = ErpClient(verbose=getattr(args, "verbose", False)).sales_range(
            start, end, on_progress=prog if getattr(args, "verbose", False) else None)
    except ErpError as e:
        print("❌ 云商销售拉取失败：%s" % e, file=sys.stderr)
        return EXIT_FETCH
    try:
        if getattr(args, "rewrite", False):
            # ⭐ **强制刷新**：抓全了才动手，`replace_sales` 内部
            #   DELETE + 写入同事务（失败回滚）；0 行会抛 PoolError 拒绝清库。
            w, sns, nosn = P.replace_sales(conn, "erp-sales", rows)
            print("  ⛔ 整表重写完成：旧表已清空，按**当前口径**写入 %d 行"
                  "（拆串 %d · 无串号 %d）" % (w, sns, nosn), flush=True)
        else:
            w, sns, nosn = P.save_sales(conn, "erp-sales", rows)
    except P.PoolError as e:
        print("❌ 强制刷新没做成，旧数据一行没动：%s" % e, file=sys.stderr)
        return EXIT_FETCH
    print("池C 云商销售 %s ~ %s：明细 %d 行 → 落库 %d 行"
          "（拆出串号 %d，无串号行 %d —— 贴膜/配件等，合成 nosn: 键落库，不进串号对账）"
          % (start, end, len(rows), w, sns, nosn))
    # ⚠⚠ **只有这里能判"有没有多串号行"** —— `save_sales` 对"主串 空格 副串"的行会
    #   **拆成多行、每行复制原行的金额与数量**（对账必须这么拆），而拆完的产物
    #   跟"一单卖了两台同款同价"**在库里长得一模一样**，事后从 `erp_sales` 反推不出来。
    #   2026-09-21 踩过：M22 一开始在 `plan` 侧写了条"同 (单号,商品编码,金额,数量)
    #   出现多行 = 拆行"的体检，实测 **1028 行全是假阳性**（回源数据逐单核过：
    #   源里就是两行、一行一个串号、卖了两只同价鼠标）⇒ 那条体检已删。
    #   ⇒ **要判就在源头判**（这里才有原始行）。
    _multi = 0
    for _r in rows:
        _parts = [x for x in str(_r.get("串号") or "").replace("，", " ").split()
                  if len(x.strip()) >= 8]
        if len(_parts) > 1:
            _multi += 1
    if _multi:
        print("  ⚠ 有 %d 行的「串号」列挤了多个串号 —— 会被拆成多行，**每行都复制"
              "原行的金额与数量**。对账要这么拆，但**按销售额/毛利汇总时那几行的钱"
              "会重复**，要按 (单号, 商品编码) 先去重" % _multi)
    return 0



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

    p = sub.add_parser("mail-key-new",
                       help="生成邮件附件加密的密钥（**只在打包那台机器上跑**）")
    p.add_argument("--key-id", default="", help="指定编号（默认自动排 m1 / m2 …）")
    p.add_argument("--note", default="", help="备注，写进密钥文件备忘")
    p.set_defaults(func=cmd_mail_key_new)

    p = sub.add_parser("mail-key-show", help="看邮件附件加密的密钥状态（不打印密钥本体）")
    p.set_defaults(func=cmd_mail_key_show)

    p = sub.add_parser("release-key-new",
                       help="生成更新包的加密密钥（**只在打包那台机器上跑**）")
    p.add_argument("--key-id", default="", help="指定编号（默认自动排 m1 / m2 …）")
    p.add_argument("--note", default="", help="备注，写进密钥文件备忘")
    p.set_defaults(func=cmd_release_key_new)

    p = sub.add_parser("release-key-show",
                       help="看更新包密钥状态（不打印密钥本体）")
    p.set_defaults(func=cmd_release_key_show)

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

    # ⚠ 存在的理由：**服务起不来的时候，控制台进不去** ——
    #   那就只剩命令行这一条路（`selftest` 也会把这两句提示打出来）。
    p = sub.add_parser("update", help="升级 / 修复没走完的升级（服务起不来时用）")
    p.add_argument("--repair", action="store_true", help="重跑上次没走完的升级")
    p.add_argument("--restore", action="store_true", help="从备份退回升级前的代码")
    p.set_defaults(func=cmd_update)

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
    # ⚠⚠ `--skip-*` 这一组**已经不用了**（2026-09-21 晚：跑什么一律由 `--steps` 点名）。
    #   留着**只为一件事**：老门店那份 `run.bat` / `run-now.bat` 里写死了它们，
    #   而那份脚本是**安装时生成、不进版本库**的 —— 自更新不会重写它，
    #   得等门店打开一次控制台（概览页里那条自愈）才换成新的。
    #   在那之前双击它，参数必须**收得下**，否则报的是
    #   `unrecognized arguments`（门店看到的是"窗口一闪就没了"，最难查的一种）。
    #   ⚠ 名单**从注册表派生**，别在这儿再手抄一份 —— 手抄的那份当年就漏了
    #     `--skip-erp-dump`（老脚本里真带着它）。
    from .features.registry import step_flags as _step_flags
    for _flag in _step_flags().values():
        p.add_argument(_flag, action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--skip-check", action="store_true", help=argparse.SUPPRESS)
    # ⭐ 内置定时器（M10）派发时走这两个：`--steps` 说"就这几步"，
    #   `--wake-slot` 是那个时间点（收尾记一笔，定时器靠它去重）。
    p.add_argument("--steps", default="",
                   help="**必给**：就跑这几步（逗号分隔，如 dump,erp-dump,pos,pools,attain）")
    p.add_argument("--wake-slot", default="",
                   help="内置定时器派发的那个时间点（YYYY-MM-DD HH:MM），只用来记录")
    p.add_argument("--log-file", default="", help="把对账那段同时写一份到文件")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("dump", help="抓华为订单 → out/cbg-<年>.db（取并集）")
    p.add_argument("--month", default="", help="YYYY-MM；给 current 或不给 = 当月（日常用这个）")
    p.add_argument("--all", action="store_true", help="抓全部历史（首次安装/补历史用，**不进日常流程**）")
    p.add_argument("--no-refresh", action="store_true",
                   help="会话失效时别开浏览器静默续期")
    p.set_defaults(func=cmd_dump)

    p = sub.add_parser("autoupdate", help="自动更新：查新版本 → 过策略 → 铺代码 → 重启")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_autoupdate)

    p = sub.add_parser("erp-dump", help="抓云商数据（在库 + 销售明细）→ 同一个订单库")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_erp_dump)

    p = sub.add_parser("pos", help="算 POS 合规率 → out/pos-<年>.json（并单独推一条）")
    p.add_argument("--db", default="", help="订单库；不给就取 out/ 里最新的 cbg-<年>.db")
    p.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.set_defaults(func=cmd_pos)

    p = sub.add_parser("attain", help="周度销售达成 → out/attain-<年>.json（读腾讯文档目标）")
    p.add_argument("--db", default="", help="订单库；不给就取 out/ 里最新的 cbg-<年>.db")
    p.add_argument("--store", default="", help="只看这家云商门店（默认：按本店配置 / 全区）")
    p.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.set_defaults(func=cmd_attain)

    p = sub.add_parser("plan", help="月度生意计划 → out/plan-<年>.json（七块 × 本月 vs 上月同期）")
    p.add_argument("--db", default="", help="订单库；不给就取 out/ 里最新的 cbg-<年>.db")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("film", help="防护膜达成（落快照 out/film.json）")
    p.set_defaults(func=cmd_film)

    p = sub.add_parser("report", help="把当天新增上报给区长（SQLite 附件走邮件）")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--date", default="", help="包里写哪天（默认今天）")
    p.add_argument("--no-push", action="store_true", help="只生成，不发")
    p.add_argument("--dry-run", action="store_true", help="连包都不生成，只看会跑哪几步")
    p.add_argument("--force", action="store_true",
                   help="指纹说没变也照样发（人工重发用）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("report-inbox", help="收门店上报 → in/report.db（区长/平台机器用）")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--limit", type=int, default=0, help="最多往回扫几封（默认 200）")
    p.add_argument("--dry-run", action="store_true", help="只看会收哪几封，不落库")
    p.set_defaults(func=cmd_report_inbox)

    p = sub.add_parser("pos-export", help="出 POS 明细 Excel")
    p.add_argument("--month", required=True, help="2026-08")
    p.add_argument("--db", default="")
    p.add_argument("--out", default="")
    p.add_argument("--by", default="", choices=["", "label", "remark"])
    p.set_defaults(func=cmd_pos_export)

    p = sub.add_parser("pools",
                       help="四个数据池：建库 / 拉取 / 看状态（池A 玲珑销售单已有，"
                            "这里管 B/C/D）")
    p.add_argument("--fetch", action="append", default=[], metavar="池",
                   choices=["lg-stock", "erp-stock", "erp-sales"],
                   help="拉哪个池（可重复给）：lg-stock=池B玲珑在库 / "
                        "erp-stock=池D云商在库 / erp-sales=池C云商销售单。"
                        "**不给就只显示状态**")
    p.add_argument("--start", default="", help="erp-sales 起始日 YYYY-MM-DD，默认当月 1 号")
    p.add_argument("--end", default="", help="erp-sales 结束日 YYYY-MM-DD，默认今天")
    p.add_argument("--rewrite", action="store_true",
                   help="**强制刷新**（2026-09-29 用户）：不给 --start 就从本年 1 月 1 日起，"
                        "抓全之后**整表重写**（按当前口径把老月份重新落一遍）。"
                        "⚠ 抓失败/抓到 0 行都不动旧数据。")
    p.add_argument("--date", default="", help="快照日 YYYY-MM-DD，默认今天")
    p.add_argument("--days-ago", type=int, default=0, help="快照日 = 今天往前 N 天")
    p.add_argument("--no-refresh", action="store_true", help="会话失效时别开浏览器静默续期")
    p.add_argument("--no-push", action="store_true", help="本次不推企业微信")
    p.add_argument("--no-mail", action="store_true", help="本次不发邮件")
    p.set_defaults(func=cmd_pools)

    p = sub.add_parser("ensure-service", help="服务没跑就把它拉起来（系统计划任务只干这个）")
    p.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    p.set_defaults(func=cmd_ensure_service)

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
