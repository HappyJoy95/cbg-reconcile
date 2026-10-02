# -*- coding: utf-8 -*-
"""诊断脚本：抓会话时「自检没过」到底卡在哪 —— 打原始 HTTP 响应，不吞错误。

背景（2026-09-27，生活馆新机实测）：
  窗口明明登录成功、进了 CBG 门店首页，300 秒里自检却一次没过，
  日志只有「会话没验过（登录态无效或已过期，或还没抓到会话）」——
  底层报错在 `store_identity._probe_store_code` 那层被吞成了三态里的
  authfail（HTTP 403？权限错？返回的是登录页 HTML？日志里完全看不出来）。

这个脚本不吞错误：起浏览器（和控制台「打开浏览器抓取」**同一个 profile、
同一个登录 URL、同一套自动填表**），等你登录，然后每隔几秒发一次
`paged-list` 探测，把**原始状态码 + 响应体**直接打出来；同时跑三个变体，
一次分清四个假设：

  A  页面 localStorage 的 csrf + 带 role-code 头   ← 正式流程走的就是这条
  B  接口换来的 csrf      + 带 role-code 头        ← 分清「csrf 过期」
  C  页面 localStorage 的 csrf + **去掉** role-code ← 分清「角色头不匹配」

怎么跑（门店电脑、项目目录下）：

    python tools\\diag_probe.py          （默认 300 秒；可带参数改秒数）

只读诊断：不写配置、不挪会话文件、不碰正式的 cbg-*.json。
A 成功时把会话**另存**到 .secrets\\diag-session.json，下次复测不用再登录。

把**全部输出**贴回来即可定案：403 / 权限文案 / 登录页 HTML，一眼分清。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests                                     # noqa: E402
from src import browser, config_io                  # noqa: E402
from src.cbg import LIST_PATH                        # noqa: E402
from src.session import CBG_BASE, CbgSession         # noqa: E402

#: 认「已经有登录 cookie」的名字，和 capture_session 同一份
SESSION_COOKIE_NAMES = frozenset({"JSESSIONID", "HWSTORE-SESSION",
                                  "hwssot3", "WPSESSIONID"})
#: 变体表：(代号, 说明, csrf 来源, 是否去掉 role-code 头)
VARIANTS = (
    ("A", "页面csrf + 带role-code", "page", False),
    ("B", "接口csrf + 带role-code", "api", False),
    ("C", "页面csrf + 去role-code", "page", True),
)


def _harden_stdout():
    """Windows 中文控制台是 GBK —— 别让打印本身炸掉（AGENTS 坑 2）。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")          # py3.7+；失败就随缘
        except Exception:                            # noqa: BLE001
            pass


def _pick_cfg(root: Path) -> dict:
    """门店配置 —— 只用来定 profile / 账号的自定义项，找不到就空着走默认。"""
    cands = sorted((root / "config").glob("store-*.yaml"))
    for p in cands:
        if p.name in ("stores.yaml", "managers.yaml"):
            continue
        try:
            cfg = config_io.load_raw(p) or {}
        except Exception:                            # noqa: BLE001
            continue
        print("[diag] 配置：%s" % p.relative_to(root))
        cfg["_path"] = str(p)
        return cfg
    print("[diag] 没找到 config/store-*.yaml —— 用默认 profile 与账号")
    return {}


def _page_url(port: int) -> str:
    try:
        ws = browser._page_ws(port)
        if not ws:
            return "?"
        pg = browser.Cdp(ws, timeout=8)
        try:
            return str(browser._eval(pg, "location.href") or "?")
        finally:
            pg.close()
    except Exception:                                # noqa: BLE001
        return "?"


def _cookie_line(picked: dict) -> str:
    """cookie 名@域/路径 一行 —— 用来盯「迁移来的老 profile 残留」。"""
    parts = []
    for name in sorted(picked or {}):
        c = picked[name] or {}
        parts.append("%s@%s%s" % (name, c.get("domain") or "?",
                                  c.get("path") or ""))
    return " ".join(parts)


def _paged_body() -> dict:
    """和 `cbg.CbgClient.list_orders` 逐字同款，**不带 storeCode**。"""
    now = int(time.time())
    return {
        "ean": "", "startTime": now - 30 * 86400, "endTime": now,
        "curPage": 1, "pageSize": 20,
        "payStatus": 2, "returnStatus": 0,
        "historyData": False, "bussinessTypes": [],
        "language": "Cn", "timezone": "Asia/Shanghai",
    }


def probe(cookies: str, csrf: str, drop_role: bool) -> dict:
    """发一次 paged-list，**原样报状态码和响应体**。"""
    sess = CbgSession(cookies=cookies, csrf=csrf, source="diag")
    headers = sess.headers()
    if drop_role:
        headers.pop("role-code", None)
    params = {"t": int(time.time() * 1000), "locale": "zh_CN"}
    try:
        r = requests.post(CBG_BASE + LIST_PATH, params=params, json=_paged_body(),
                          headers=headers, timeout=25)
    except requests.RequestException as e:
        return {"ok": False, "info": "请求失败：%s" % e, "sess": sess}
    flat = " ".join((r.text or "").split())
    if r.status_code != 200:
        return {"ok": False, "info": "HTTP %d · %s" % (r.status_code, flat[:260]),
                "sess": sess}
    try:
        j = r.json()
    except ValueError:
        # 网关把登录页 HTML 交回来了 —— 掉登录 / 被重定向
        return {"ok": False, "info": "HTTP 200 但不是 JSON · %s" % flat[:260],
                "sess": sess}
    if j.get("status") == "success":
        rows = j.get("result") or []
        codes = sorted({str(x.get("storeCode") or "").strip()
                        for x in rows if (x or {}).get("storeCode")})
        return {"ok": True, "sess": sess, "codes": codes,
                "info": "HTTP 200 status=success · %d 行 · storeCode=%s"
                        % (len(rows), ",".join(codes) or "(0 行里没店码)")}
    return {"ok": False, "sess": sess,
            "info": "HTTP 200 status=%s code=%s message=%s detail=%s"
                    % (j.get("status"), j.get("code"), j.get("message"),
                       str(j.get("detail") or "")[:160])}


def run(timeout: float) -> int:
    _harden_stdout()
    cfg = _pick_cfg(ROOT)
    profile = browser.profile_path(cfg, ROOT)
    start_url = browser.login_url(cfg)
    print("[diag] profile：%s" % profile)
    print("[diag] 起始页：%s" % start_url)
    print("[diag] 三个变体：%s" % " / ".join(
        "%s=%s" % (t, d) for t, d, _s, _d in VARIANTS))
    print("[diag] 输出只在**结果变化**时才打；登录后盯着 A 那行看。")

    user, pwd = browser.load_login_credentials(cfg, ROOT)
    if browser.captcha_marked(ROOT):
        print("[diag] 上次撞过图形验证码 —— 这次不自动填，请在窗口手动登录")
        user = pwd = ""
    if user and pwd:
        print("[diag] 自动登录账号：%s" % user)
    else:
        print("[diag] 没配华为账号密码 —— 请在窗口里手动登录")

    try:
        proc, port = browser.launch(Path(profile), url=start_url,
                                    headless=False, cfg=cfg)
    except browser.BrowserError as e:
        print("[X] 浏览器起不来：%s" % e)
        return 2
    print("[diag] 浏览器已启动（调试端口 %s）—— 请在窗口里登录" % port)

    deadline = time.time() + timeout
    tries = 0
    nav_at = time.time()
    nav_done = False
    prev_sig = None
    prev_cookies = None
    last_tick = time.time()
    seen = {}                     # 变体代号 -> 出现过的响应（去重汇总用）
    saved = None
    rc = 1
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                why = getattr(proc, "last_failure", "") or (
                    "退出码 %s" % getattr(proc, "returncode", "?"))
                print("[X] 浏览器没了（%s）" % why)
                return 2
            try:
                cookies, picked = browser.cookies_from_browser(port)
            except browser.CdpError as e:
                print("[!] 读 cookie 失败：%s" % e)
                time.sleep(2)
                continue

            names = set(picked or {})
            if not (names & SESSION_COOKIE_NAMES):
                sig = ("wait", _cookie_line(picked))
                if sig != prev_sig:
                    prev_sig = sig
                    print("[%s] 还没有登录 cookie（%d 个）· 页面=%s"
                          % (time.strftime("%H:%M:%S"), len(names),
                             _page_url(port)))
                # ---- 自动登录 / 兜底导航，节奏照抄 capture_session ----
                if user and pwd and tries < 3:
                    r = browser.try_auto_login(port, user, pwd, say=print)
                    if r == "submitted":
                        tries += 1
                        print("[diag] 等待登录跳转…（第 %d/3 次）" % tries)
                        time.sleep(6)
                        continue
                    if r == "captcha":
                        print("[!] 登录页要图形验证码 —— 停止自动填，"
                              "请在窗口手动登录并输入验证码")
                        user = pwd = ""
                    elif r == "error":
                        print("[!] 登录页报错（账号密码？）—— 停止自动重试")
                        user = pwd = ""
                if not nav_done and time.time() - nav_at > 12:
                    browser.goto_url(port, browser.login_page_url(start_url),
                                     say=print)
                    nav_done = True
                time.sleep(4)
                continue

            csrf_p = browser.csrf_from_page(port)
            csrf_a = browser.csrf_from_api(cookies)
            if prev_cookies != _cookie_line(picked):
                prev_cookies = _cookie_line(picked)
                print("[%s] cookie 变了：%s" % (time.strftime("%H:%M:%S"),
                                                prev_cookies))

            sig = []
            results = {}
            for tag, _desc, src, drop in VARIANTS:
                csrf = csrf_p if src == "page" else csrf_a
                if not csrf:
                    info = "csrf 取不到（来源=%s）" % src
                    res = {"ok": False, "info": info}
                else:
                    res = probe(cookies, csrf, drop)
                    info = res["info"]
                results[tag] = res
                seen.setdefault(tag, [])
                if info not in seen[tag]:
                    seen[tag].append(info)
                sig.append("%s=%s" % (tag, info))

            if tuple(sig) != prev_sig:
                prev_sig = tuple(sig)
                print("[%s] 页面=%s · csrf页面=%s · csrf接口=%s"
                      % (time.strftime("%H:%M:%S"), _page_url(port),
                         (csrf_p or "-")[:8], (csrf_a or "-")[:8]))
                for tag, _desc, _src, _d in VARIANTS:
                    print("    %s %s" % (tag, results[tag]["info"]))

            a = results.get("A") or {}
            if a.get("ok"):
                print("[OK] A 通了 —— 正式流程走的就是这条，探测本身能成。")
                if a.get("codes"):
                    print("[OK] 认店会拿到店码：%s" % ", ".join(a["codes"]))
                else:
                    print("[OK] 但 0 行里没有店码（新店 30 天没卖货 -> empty 态）")
                if saved is None:
                    sess = a.get("sess")
                    if sess is None:
                        sess = CbgSession(cookies=cookies,
                                          csrf=csrf_p or csrf_a or "",
                                          source="diag")
                    p = sess.save(ROOT / ".secrets" / "diag-session.json")
                    saved = p
                    print("[OK] 会话已另存 %s（不碰正式 cbg-*.json）" % p)
                rc = 0
                break

            if time.time() - last_tick > 30:
                last_tick = time.time()
                print("[%s] 等登录/重试中…（还剩 %d 秒）"
                      % (time.strftime("%H:%M:%S"),
                         max(0, int(deadline - time.time()))))

            if user and pwd and tries < 3:
                r = browser.try_auto_login(port, user, pwd, say=print)
                if r == "submitted":
                    tries += 1
                    print("[diag] 等待登录跳转…（第 %d/3 次）" % tries)
                    time.sleep(6)
                    continue
                if r == "captcha":
                    print("[!] 登录页要图形验证码 —— 停止自动填，"
                          "请在窗口手动登录并输入验证码")
                    user = pwd = ""
                elif r == "error":
                    print("[!] 登录页报错（账号密码？）—— 停止自动重试")
                    user = pwd = ""
            if not nav_done and time.time() - nav_at > 12:
                browser.goto_url(port, browser.login_page_url(start_url),
                                 say=print)
                nav_done = True
            time.sleep(4)
    finally:
        where = _page_url(port)
        try:
            browser._shutdown(proc)
        except Exception:                            # noqa: BLE001
            pass
        print("[diag] 收尾时窗口停在：%s" % where)

    print("")
    print("==== 汇总（每个变体出现过的全部响应，去重）====")
    for tag, desc, _s, _d in VARIANTS:
        print("  %s（%s）" % (tag, desc))
        for info in seen.get(tag) or ["(没跑成)"]:
            print("      %s" % info)
    if rc == 0:
        print("结论：A 通了。若正式流程仍卡住，差异只可能在 csrf/cookie 取值时机。")
    else:
        print("结论：超时没通。看上面 A/B/C 的原始响应定案：")
        print("  · HTTP 403 / 不是 JSON       -> cookie 或 csrf 是老的（profile 迁移残留）")
        print("  · message 含「没有门店或数据范围」-> 不带 storeCode 被权限层拒（认店前提不成立）")
        print("  · C 通、A 不通               -> role-code 头和账号角色不匹配")
    return rc


if __name__ == "__main__":
    secs = 300
    if len(sys.argv) > 1:
        try:
            secs = max(30, int(sys.argv[1]))
        except ValueError:
            print("用法：python tools/diag_probe.py [超时秒数]")
            sys.exit(2)
    try:
        sys.exit(run(secs))
    except KeyboardInterrupt:
        print("\n[diag] 手动中断")
        sys.exit(130)
