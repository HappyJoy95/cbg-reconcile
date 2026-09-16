"""自动抓 cookie —— 不用再手抄 curl。

思路：**让我们自己启动的浏览器把 cookie 交出来**，不去解密浏览器的 cookie 数据库
（Chrome 127+ 加了 App-Bound 加密，版本一升就废，不能依赖）。

    cookie ← CDP `Network.getAllCookies`（明文）
    csrf   ← CDP 在 cbg 页面上执行 JS 读 localStorage['pix-environment-csrf-token']
             （前端就是这么存的，见平台引擎 app.*.min.js：
              `$PIX.Util.localStorage.set("pix-environment-csrf-token", a)`）
             读不到再退回去调 `phoenix-gws/phoenix.sso.csrf.token`

用**独立 profile 目录**（不是用户日常那个浏览器），所以：
- 不影响店员的日常浏览器，也不受它影响
- profile 会长期保留 SSO 登录态 → **日常跑可以无头静默续期，完全不用人管**

⚠ 只读我们自己启动的这个 profile，不碰用户浏览器 —— 这是隐私边界。
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

from . import envfile
from .cdp import Cdp, CdpError, http_json, page_targets
from .session import CBG_BASE, CbgAuthError, CbgSession, _clean_cookies

PORTAL_URL = f"{CBG_BASE}/"
# 门店日常用的入口。**以这个为准** —— 未登录时它会自己跳到 SSO 登录页，
# 我们不去硬编码登录地址（换域名/换 SSO 版本时不用改代码）。
DEFAULT_LOGIN_URL = f"{CBG_BASE}/#/group/smart-store/homepage"
CSRF_STORAGE_KEY = "pix-environment-csrf-token"
CSRF_PATH = "/phoenix-gws/phoenix.sso.csrf.token"
PROFILE_DIRNAME = ".secrets/browser-profile"
LOGIN_ENV_FILE = ".secrets/huawei.env"
SSO_ENTRY = "https://uniportal.huawei.com/uniportal1/"

MAX_LOGIN_TRIES = 3

# 上次抓取撞了验证码的标记。放 `.secrets/`：它是**这台电脑的**运行状态，
# 不跟包走、也不该被自更新冲掉（selfupdate 的 NEVER_TOUCH）。
CAPTURE_STATE = ".secrets/capture-state.json"

# 登录态相关的 cookie 所在的域
_COOKIE_DOMAINS = ("cbg.huawei.com", ".huawei.com", "login.huawei.com", ".huawei.cn")

# ⚠ 顺序 = 优先级。**Chrome 在前**：
#   * 门店那些电脑上 Chrome 往往也装了，而它遇到的麻烦更少 —— Edge 从
#     管理员进程启动时自我降权重启（AutoDeElevate）更凶，抓会话更容易断；
#   * 想换别的浏览器不用改代码：config 里写 `browser.prefer: [edge, chrome]` 即可
#     （见 `_browser_prefer`）。
# ⚠ 别把"Edge 是系统自带的"当成前提：**Win10 是，Win7 不是**。
#   Win7 出厂只有 IE11 —— IE11 既跑不了控制台前端（`fetch` / `async` 一个都不支持），
#   也没有 CDP，抓会话完全靠不上。Chromium Edge 是 2020 年微软通过 Windows Update
#   推过去的，那之后一直没更新过的 Win7 上就没有。
#   而 Win7 上最高只能到 **Edge 109**（微软 2023-01 停了 Win7/Win8.1 支持），之后不再更新
#   —— 109 是 Chromium 内核，前端和 CDP 都够用，但**不会再收到安全更新**。
#   下面按"文件在不在"找，找不到会明确报错，不会静默失败。
_WIN_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
_MAC_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

# `browser.prefer` 里认的名字 → 按平台找可执行文件。名字小写。
_BROWSER_ALIASES = {
    "chrome": {"windows": ["chrome"], "darwin": ["chrome"],
               "linux": ["google-chrome", "google-chrome-stable"]},
    "edge": {"windows": ["msedge"], "darwin": ["edge"], "linux": ["microsoft-edge"]},
    "brave": {"windows": ["brave"], "darwin": ["brave"], "linux": ["brave-browser"]},
    "chromium": {"windows": ["chromium"], "darwin": ["chromium"],
                 "linux": ["chromium", "chromium-browser"]},
}


class BrowserError(RuntimeError):
    pass


class CbgCaptchaRequired(CbgAuthError):
    """自动登录撞上了图形验证码 —— 自动这条路走不通，得人来。

    ⚠ **必须继承 `CbgAuthError`**：`cli.py` 有三处 `except CbgAuthError`、
    `probe_session` 里还有一处 `except (CbgAuthError, BrowserError)` ——
    不是子类的话，这个新异常会直接炸穿那些调用方。

    ⚠ 抛它之前，`capture_session` 的 `finally` 已经**把浏览器关掉了**
    （`_shutdown`）—— 调用方拿到它就可以安全地删 profile 了
    （Windows 上还有句柄就删不掉）。
    """

    def __init__(self, profile_dir=None):
        super().__init__(
            "自动登录时登录页要**图形验证码** —— 自动填表这条路走不通。"
            "已经把浏览器关掉、并重置了这台电脑的抓取 profile"
            "（那个半成品留着也没用，反而会让下次从脏状态开始）。"
            "请重新点「打开浏览器抓取」，在弹出的窗口里**手动登录并输入验证码**。"
            + (f"\n（profile：{profile_dir}）" if profile_dir else ""))


def profile_path(cfg: dict, root) -> Path:
    """配置文件里的 `session.browser_profile`，没配就用默认的 .secrets/browser-profile。"""
    rel = (cfg.get("session") or {}).get("browser_profile") or PROFILE_DIRNAME
    p = Path(rel)
    return p if p.is_absolute() else Path(root) / p


def _state_path(root) -> Path:
    return Path(root) / CAPTURE_STATE


def captcha_marked(root) -> bool:
    """上次抓取是不是撞了验证码。

    ⚠ 这是**降级用**的东西：读不到就当没有，**绝不抛** ——
    抓取路径上抛一次就是整条流程挂掉，而它只是个提示开关。
    """
    try:
        d = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return bool(isinstance(d, dict) and d.get("captcha_at"))
    except (OSError, TypeError, ValueError):
        return False


def mark_captcha(root) -> None:
    """记下"这次是被验证码挡下来的"。写不成不影响抓取本身。"""
    try:
        p = _state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"captcha_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def clear_captcha(root) -> None:
    """抓到可用会话了 —— 标记没用了，清掉，恢复自动填表。"""
    try:
        _state_path(root).unlink()
    except OSError:
        pass


def login_url(cfg: dict) -> str:
    return ((cfg.get("session") or {}).get("login_url") or DEFAULT_LOGIN_URL).strip()


# ------------------------------------------------------------- 华为账号密码
def login_env_path(cfg: dict, root) -> Path:
    rel = (cfg.get("session") or {}).get("login_env_file") or LOGIN_ENV_FILE
    return envfile.resolve(rel, root)


def load_login_credentials(cfg: dict, root) -> tuple[str, str]:
    """读华为账号密码。存在 .secrets/huawei.env —— 性质等同密码，不进仓库配置。"""
    d = envfile.parse(login_env_path(cfg, root))
    return (d.get("HUAWEI_USERNAME") or "").strip(), d.get("HUAWEI_PASSWORD") or ""


def save_login_credentials(cfg: dict, root, *, username=None, password=None) -> Path:
    updates = {}
    if username is not None:
        updates["HUAWEI_USERNAME"] = username
    if password is not None:
        updates["HUAWEI_PASSWORD"] = password
    return envfile.update(login_env_path(cfg, root), updates)


def describe_login(cfg: dict, root) -> dict:
    """给界面看 —— **绝不回传密码**。"""
    p = login_env_path(cfg, root)
    u, pw = load_login_credentials(cfg, root)
    return {
        "username": u,
        "has_password": bool(pw),
        "env_file": str(p),
        "exists": p.exists(),
        "login_url": login_url(cfg),
        "ready": bool(u and pw),
    }


# --------------------------------------------------------------- 自动登录
# 华为 SSO 登录页（uniportal，Vue + Element-UI）实测只有三个控件：
#   #username（账号名/邮箱/手机号/W3账号）、#password、一个"记住用户名"复选框
# 提交按钮是 BUTTON.common-button，文字「登录」。
#
# ⚠ 直接改 `.value` **不会触发 Vue 的响应式** —— 必须走原型链上的原生 setter
#   再派发 input/change 事件。这是 Element-UI / React 都认的标准做法。
_DETECT_JS = r"""
(() => {
  const u = document.querySelector('#username, input[name="username"]');
  const p = document.querySelector('#password, input[type="password"]');
  const vis = (e) => e && e.offsetParent !== null;
  // 提交失败时页面会亮出错误提示。**认出来就别再重试了** ——
  // 密码错了连打三次，轻则弹验证码，重则锁号。
  const errSel = '.el-form-item__error, [class*="error-tip"], [class*="errorTip"],'
               + ' [class*="login-error"], [class*="warn-tip"], [class*="err-msg"], [role="alert"]';
  let errText = '';
  for (const e of document.querySelectorAll(errSel)) {
    if (vis(e) && (e.innerText || '').trim()) { errText = e.innerText.trim().slice(0, 60); break; }
  }
  if (!errText) {
    const body = (document.body.innerText || '');
    for (const hint of ['密码错误', '账号或密码', '用户名或密码', '密码不正确', '账号不存在',
                        '已锁定', '账户被锁', '尝试次数', '多次失败']) {
      if (body.includes(hint)) { errText = hint; break; }
    }
  }
  return JSON.stringify({
    hasForm: vis(u) && vis(p),
    url: location.href,
    captcha: !!document.querySelector('#captcha img, [class*="verify-code"] img, [class*="captcha"] img'),
    error: errText
  });
})()
"""

_FILL_TMPL = r"""
(() => {
  const set = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
    d.set.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  };
  const u = document.querySelector('#username, input[name="username"]');
  const p = document.querySelector('#password, input[type="password"]');
  set(u, %s);
  set(p, %s);
  return JSON.stringify({u: u.value.length, p: p.value.length});
})()
"""

_SUBMIT_JS = r"""
(() => {
  const btn = [...document.querySelectorAll('button')]
    .find(b => (b.innerText || '').trim() === '\u767b\u5f55' && b.offsetParent !== null);
  if (btn) { btn.click(); return 'clicked'; }
  const f = document.querySelector('form');
  if (f) { f.submit(); return 'submitted'; }
  return 'no-button';
})()
"""


def _page_ws(port: int, *hints: str) -> str | None:
    """找一个页面 target；优先 URL 含 hints 的。"""
    pages = page_targets(port)
    for t in pages:
        u = t.get("url") or ""
        if any(h in u for h in hints):
            return t["webSocketDebuggerUrl"]
    return pages[0]["webSocketDebuggerUrl"] if pages else None


def _eval(pg: Cdp, expression: str, timeout: float = 15.0):
    r = pg.call("Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                timeout=timeout)
    if r.get("exceptionDetails"):
        raise CdpError("页面里执行 JS 报错")
    return (r.get("result") or {}).get("value")


def try_auto_login(port: int, username: str, password: str, say=None) -> str:
    """当前页面是登录页就填了提交。

    返回：
        'submitted'  已提交（接下来等跳转）
        'captcha'    要图形验证码 —— 自动不了，得人来
        'error'      页面已经报错了（多半是账号密码不对）—— **必须停手**
        'no-form'    不在登录页（可能已经登上了）
    """
    say = say or (lambda m: None)
    ws = _page_ws(port)
    if not ws:
        return "no-form"
    pg = Cdp(ws, timeout=20)
    try:
        info = json.loads(_eval(pg, _DETECT_JS) or "{}")
        if not info.get("hasForm"):
            return "no-form"
        if info.get("captcha"):
            return "captcha"
        if info.get("error"):
            # ⚠ 页面已经报错了。再填再提交就是往同一个账号上连打失败登录 ——
            #   轻则弹验证码、重则锁号。**立刻停手**，把错误原文交给上层。
            say(f"登录页报错：{info['error']}")
            return "error"

        # 用 json.dumps 转义，别手拼字符串（密码里可能有引号/反斜杠）
        _eval(pg, _FILL_TMPL % (json.dumps(username), json.dumps(password)))
        filled = json.loads(_eval(
            pg, "JSON.stringify({p: (document.querySelector('#password')||{}).value || ''})") or "{}")
        if not filled.get("p"):
            say("⚠️ 密码没填进去（页面结构可能变了）")
            return "no-form"

        how = _eval(pg, _SUBMIT_JS)
        if how == "no-button":
            say("⚠️ 没找到「登录」按钮（页面结构可能变了）")
            return "no-form"
        say("已填好账号密码并提交登录")
        return "submitted"
    except (CdpError, ValueError):
        return "no-form"
    finally:
        pg.close()


def goto_url(port: int, url: str, say=None) -> None:
    """导航到指定页面（还没跳到登录页时别干等）。"""
    ws = _page_ws(port)
    if not ws:
        return
    pg = Cdp(ws, timeout=20)
    try:
        pg.call("Page.enable")
        pg.call("Page.navigate", {"url": url})
        (say or (lambda m: None))(f"导航到 {url}")
    except CdpError:
        pass
    finally:
        pg.close()


def login_page_url(target: str) -> str:
    from urllib.parse import quote
    return f"{SSO_ENTRY}?redirect={quote(target, safe='')}"


# ------------------------------------------------------------------ 找浏览器
def _browser_prefer(cfg: dict | None) -> list[str]:
    """配置里 `browser.prefer` 指定的浏览器优先级。

    写法（config/store-<门店码>.yaml）：

        browser:
          prefer: [chrome, edge]      # 想用 Edge 就写 [edge, chrome]

    名字不认得的直接忽略 —— 不能因为写错一个词就让"自动抓会话"整个用不了。
    """
    raw = (cfg or {}).get("browser") or {}
    if not isinstance(raw, dict):
        return []
    listed = raw.get("prefer")
    if isinstance(listed, str):                    # 容错：写成字符串也认
        listed = [listed]
    if not isinstance(listed, (list, tuple)):
        return []
    out = []
    for item in listed:
        name = str(item).strip().lower()
        if name in _BROWSER_ALIASES and name not in out:
            out.append(name)
    return out


def _find_named(name: str) -> str | None:
    """按名字找一个浏览器：先查常见安装路径，再退到 PATH。"""
    system = platform.system().lower()
    key = "windows" if system == "windows" else ("darwin" if system == "darwin" else "linux")
    paths = {
        "windows": _WIN_PATHS,
        "darwin": _MAC_PATHS,
        "linux": [],
    }[key]
    if name == "chrome":
        for p in paths:
            if "Chrome" in p and Path(p).exists():
                return p
    elif name == "edge":
        for p in paths:
            if "Edge" in p and Path(p).exists():
                return p
    elif name in ("brave", "chromium"):
        for p in paths:
            if name.capitalize() in p and Path(p).exists():
                return p
    for exe in _BROWSER_ALIASES.get(name, {}).get(key, []):
        p = shutil.which(exe)
        if p:
            return p
    return None


def find_browser(cfg: dict | None = None) -> tuple[str, str] | None:
    """返回 (可执行文件, 名字)。

    **默认 Chrome 优先**（两台都装时用 Chrome）：它在门店电脑上遇到的麻烦更少，
    Edge 从管理员进程启动时自我降权重启更凶，抓会话更容易断。

    想换顺序就在 config 里写 `browser.prefer: [edge, chrome]`。
    写错名字不会让功能失效 —— 认不出的忽略掉，剩下的照常找。
    """
    for name in _browser_prefer(cfg):
        path = _find_named(name)
        if path:
            return path, name.capitalize() if name != "edge" else "Edge"

    system = platform.system()
    candidates: list[tuple[str, str]] = []
    if system == "Windows":
        candidates = [(p, "Edge" if "Edge" in p else "Chrome") for p in _WIN_PATHS]
    elif system == "Darwin":
        candidates = [(p, "Edge" if "Edge" in p else "Chrome") for p in _MAC_PATHS]
    else:
        for exe in ("google-chrome", "google-chrome-stable", "microsoft-edge",
                    "chromium", "chromium-browser"):
            p = shutil.which(exe)
            if p:
                candidates.append((p, exe))

    for path, name in candidates:
        if path and Path(path).exists():
            return path, name
    for exe, name in (("chrome", "Chrome"), ("msedge", "Edge"), ("chromium", "Chromium")):
        p = shutil.which(exe)
        if p:
            return p, name
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -------------------------------------------------------------------- 启动
SETTLE_SECONDS = 3.0     # 端口通了之后还要观察这么久，确认真活下来
PROBE_INTERVAL = 0.4     # 探测调试端口的间隔
DE_ELEVATE_GRACE = 8.0   # 启动进程死了之后，还给端口多少次机会（秒）
DEATH_GRACE = 1.2        # 判"浏览器关掉了"之前，连续探这么久才认（见 DetachedBrowser.poll）


def profile_locked(profile_dir) -> bool:
    """profile 是不是**正被别的浏览器实例占着**。

    Chrome / Edge 会在 profile 目录里放 `SingletonLock`。它还在 = 有实例占着，
    这时新起的进程会**立刻退出（退出码 21）** —— 看起来像"启动失败"，
    其实只是"别人已经在用了"。

    ⚠ 这个坑会**连锁**：上一次抓取失败时如果浏览器没被关掉（比如我们拿不到
    进程句柄），它会一直占着 profile，之后每次抓取都撞锁、都报退出码 21。
    实测就是这样：一个残留的 Edge 让后面所有尝试全部失败。
    """
    try:
        p = Path(profile_dir)
    except TypeError:
        return False
    for name in ("SingletonLock", "SingletonCookie", "lockfile"):
        f = p / name
        try:
            if f.is_symlink() or f.exists():
                return True
        except OSError:
            continue
    return False


def _launch_failure_hint(name: str, profile_dir=None, reason: str = "",
                         returncode=None) -> str:
    """启动失败时给一句**能照着做**的话。

    ⚠ **说"已确认"别说"可能的原因"**（2026-09-16 改）。原来一律写成"可能的原因
    （按可能性排）"，结果门店看到"服务现在是管理员身份"那条时会想
    "我没用管理员啊" —— 然后去试第 2、3 条，白折腾一轮。
    其实这条**不是猜的**：它只在 `IsUserAnAdmin()` 真返回真的时候才出现。
    把"已确认"和实测过的现象写出来，人才会信。

    `reason` 非空表示**不是启动失败，而是跑到一半浏览器没了**：
    这时两条"启动"相关的建议（profile 被占、沙箱）都不对症，先别列出来，
    否则门店照着一个不相干的建议折腾半天。
    """
    lines = []
    if reason:
        if _am_i_admin():
            lines.append("**已确认：服务是以管理员身份在跑的**"
                         "（`IsUserAnAdmin()` 返回真）—— 而 Edge / Chrome 拒绝以管理员"
                         "运行，会在跑到一半时把命令行交棒出去然后自己退出。"
                         "改法看下面第 1 条")
        lines.append("**是不是把浏览器窗口手动关掉了？** 登录完**别关那个窗口** —— "
                     "程序还要从它那里读 cookie，读完它自己会关。"
                     "重新点一次「打开浏览器抓取」就行")
        lines.append("浏览器自己崩了 —— 重新点一次「打开浏览器抓取」")
        return "\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(lines, 1))

    if _am_i_admin():
        # ⚠ 这条放在**最前面**，而且写成"已确认"不是"可能"：
        #   管理员身份下这个功能是**必坏**的，不是"可能出问题"。
        lines.append("**已确认：服务是以管理员身份在跑的。** 这是本次失败的原因 —— "
                     "Edge / Chrome 拒绝以管理员运行：进程起来以后把命令行交棒出去、"
                     "自己退 0，我们给的 `--user-data-dir` / `--remote-debugging-port` "
                     "落不到活着的实例上，调试端口永远没人监听"
                     # ⚠ 别把它写死成"你看到的是退出码 0"：那说的是**典型**表现，
                     #   而这一次可能先撞上 profile 被占（21）。写死了就跟下面第 2 条
                     #   自相矛盾，用户会以为程序在胡说。（实测真撞上过。）
                     "（典型表现是「启动后立刻退出（退出码 0）」，"
                     "而且链接跑到了你原来那个浏览器里）。\n"
                     "     改法：控制台「设置 → 后台服务」→「启动方式」选"
                     "**普通权限** → 保存 → 双击 `stop.bat` → 用**普通权限**双击 "
                     "`start.bat` → 再抓一次。"
                     + _always_admin_note())
    # ⚠ **退出码 21 是"profile 被占着"的铁证**，比翻锁文件可靠 ——
    #   21 就是 Chromium 的 `RESULT_CODE_PROFILE_IN_USE`。
    #   实测踩过：报错写着"退出码 21"，可 `profile_locked()` 翻不到锁文件
    #   （那一版 Edge / 那台机器上文件名不一样），于是这条提示**没出现**，
    #   用户看到的是一堆不相干的建议。**退出码是浏览器直接告诉我们的，优先信它。**
    locked = (returncode == 21) or (profile_dir is not None and profile_locked(profile_dir))
    if locked:
        why21 = "（退出码 21 = Chromium 的 PROFILE_IN_USE）" if returncode == 21 else ""
        # ⚠ 残留的那个浏览器**很可能看不见**：静默续期走的是**无头模式**，
        #   根本没有窗口 —— 用户"关掉所有浏览器窗口"之后照样撞 21，
        #   然后完全不知道还能干什么。所以这里必须给一条**能直接粘的命令**。
        lines.append(f"**profile 被占着**{why21} —— 有 Edge / Chrome 实例正在用这个 "
                     "profile。"
                     "\n     ⚠ 它**多半是看不见的**：静默续期用的是**无头模式**"
                     "（没有窗口），所以「关掉所有浏览器窗口」不一定管用。"
                     "\n     最省事的做法 —— 打开「命令提示符」粘这一条（会把**所有**"
                     "Edge 一起关掉，包括你自己开着的，先存好网页）："
                     "\n       taskkill /f /im msedge.exe"
                     "\n     用 Chrome 的话把 msedge.exe 换成 chrome.exe。"
                     "\n     也可以：任务管理器 →「详细信息」选项卡 → 把所有 "
                     "`msedge.exe` 结束掉（有十几个是正常的，全结束）。"
                     "\n     ⚠ 上一次抓取失败时如果浏览器没被关掉，它会一直占着，"
                     "之后**每次**抓取都撞这个 —— 这个坑会连锁")
    lines.append("profile 目录有问题（坏掉了？）—— 删掉 .secrets\\browser-profile 让它重建")
    lines.append("受限环境（服务器 / 沙箱）里 Edge / Chrome 建不了子进程沙箱 —— "
                 "设环境变量 CBG_BROWSER_NO_SANDBOX=1 再试")
    # ⚠ 统一写"Edge / Chrome"：门店用的是 Edge，只提 Chrome 会让人以为"跟我无关"。
    lines = [ln.replace("Chrome 拒绝以管理员运行", "Edge / Chrome 拒绝以管理员运行")
             for ln in lines]
    return "\n" + "\n".join(f"  {i}. {x}" for i, x in enumerate(lines, 1))


def _always_admin_note() -> str:
    """如果这台电脑**根本没法不管理员**（内置 Administrator / UAC 关着），补一句。

    ⚠ 少了这句，用户会照着上面"改成普通权限"折腾半天 ——
    而那台机器上**改了也没用**，因为每个进程都注定是管理员。
    实测就是这么来回好几轮的。能查出来就直说，别让人白试。
    """
    try:
        from .elevate import always_admin_reason
        reason = always_admin_reason()
    except Exception:                              # noqa: BLE001
        return ""
    return f"\n     ⚠ **但这台电脑改不了**：{reason}" if reason else ""


def _am_i_admin() -> bool:
    try:
        from .autostart import is_elevated
        return is_elevated()
    except Exception:                              # noqa: BLE001
        return False


def _port_alive(port: int) -> bool:
    try:
        http_json(port, "/json/version", timeout=1)
        return True
    except Exception:                              # noqa: BLE001
        return False


def _close_via_cdp(port: int) -> None:
    """没进程句柄了，只能请浏览器自己关（CDP 的 Browser.close）。"""
    try:
        cdp = Cdp.connect(port)
        try:
            cdp.call("Browser.close", {}, timeout=5)
        finally:
            try:
                cdp.close()
            except Exception:                      # noqa: BLE001
                pass
    except Exception:                              # noqa: BLE001
        pass


class DetachedBrowser:
    """Chrome 自己降权重启之后的**替身**。

    实测踩到：服务以**管理员身份**运行时启动 Chrome，Chrome（138+，2025 年
    Edge 团队贡献的 AutoDeElevate）会**把自己降权重启** —— 我们 Popen 出来的
    那条进程立刻退出（退出码 21），真正在跑的是另一个进程。

    这时**进程句柄就废了**（poll() 永远说"死了"），但浏览器明明是活的。
    唯一还靠得住的是**调试端口**：端口在应答 = 浏览器活着。
    关机也走 CDP 的 Browser.close，不再 kill 进程。

    见 https://issues.chromium.org/issues/436869753
    """

    detached = True

    def __init__(self, port: int):
        self.port = port
        self.returncode = None
        self.last_failure = ""          # 上一次判定"死了"时，探测到底报了什么

    def poll(self):
        """死了返回退出码，活着返回 None。

        ⚠ **不能探一次就算死**。实测（门店电脑、服务以管理员身份跑）：
        自动登录提交之后 Edge 会自我降权重启（AutoDeElevate），那个窗口里
        调试端口会**短暂失灵** —— 探一次探不到，就会被当成"用户把浏览器关了"，
        于是整个抓取在中途放弃。而浏览器其实几秒后就在同一个端口上回来了。

        见 https://issues.chromium.org/issues/436869753
        """
        tries = max(1, int(DEATH_GRACE / PROBE_INTERVAL))
        for i in range(tries):
            if _port_alive(self.port):
                self.last_failure = ""
                return None
            if i + 1 < tries:
                time.sleep(PROBE_INTERVAL)
        self.last_failure = (f"调试端口 {self.port} 连续 {tries} 次探测都没应答"
                             f"（约 {DEATH_GRACE:.1f} 秒）")
        return 0

    def kill(self):
        _close_via_cdp(self.port)

    terminate = kill

    def wait(self, timeout=None):
        end = time.time() + (timeout if timeout is not None else 5)
        while time.time() < end and self.poll() is None:
            time.sleep(0.3)
        return 0


def forced_user_data_dir() -> tuple:
    """浏览器被**组策略**强制指定了 user-data-dir 吗。返回 `(名字, 路径)`，没有就 `("", "")`。

    ## ⚠ 这一条会让"独立 profile"整个失效

    `UserDataDir` 是浏览器的**强制策略**（Edge 和 Chrome 都有）。一旦设了它，
    浏览器**直接忽略命令行上的 `--user-data-dir`** —— 于是我们那套全废：

    * 我们给的独立目录根本没被用上；
    * 浏览器开在**用户日常那个 profile** 里；
    * URL 跑到用户已经开着的浏览器里，我们等的调试端口**永远没人监听**；
    * 报出来的是「启动后立刻退出（退出码 21）」或者干脆链接跑到别处去了。

    **这条是门店实测查出来的**（用户找的，不是我们）—— 前面好几轮我们一直在
    查权限、查 profile 残留、查版本，全都不对。

    ⚠ 这里必须**硬失败**，不能"退而求其次用那个被强制的目录"：那等于去读
    **用户日常浏览器的 cookie** —— 本模块顶部写明的那条隐私边界
    （只读我们自己启动的 profile）就是这么破的。

    读不到注册表就返回空 —— 这是启动路径，**不抛异常**。
    """
    if os.name != "nt":
        return ("", "")
    import winreg
    for name, key in (("Edge", r"SOFTWARE\Policies\Microsoft\Edge"),
                      ("Chrome", r"SOFTWARE\Policies\Google\Chrome")):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key) as k:
                    val, _ = winreg.QueryValueEx(k, "UserDataDir")
                val = str(val or "").strip()
                if val:
                    return (name, val)
            except OSError:
                continue
    return ("", "")


def _forced_dir_error() -> str:
    """被强制策略挡住时的说明 —— 要说清"这不是你操作的问题"和怎么查。"""
    name, path = forced_user_data_dir()
    if not name:
        return ""
    return (f"{name} 被**组策略**强制指定了 user-data-dir：\n"
            f"    {path}\n"
            f"    设了这个策略之后，浏览器会**忽略命令行上的 `--user-data-dir`**，"
            f"所以「用独立 profile 抓会话」这条路根本走不通 —— "
            f"而且会去读你日常浏览器的登录态（我们不碰那个）。\n"
            f"    怎么确认（命令提示符里跑）：\n"
            f"      reg query \"HKLM\\SOFTWARE\\Policies\\Microsoft\\{name}\" /v UserDataDir\n"
            f"      reg query \"HKCU\\SOFTWARE\\Policies\\Microsoft\\{name}\" /v UserDataDir\n"
            f"    怎么去掉：这两条各跑一次（哪条有就删哪条），然后**重启浏览器**：\n"
            f"      reg delete \"HKLM\\SOFTWARE\\Policies\\Microsoft\\{name}\" /v UserDataDir /f\n"
            f"      reg delete \"HKCU\\SOFTWARE\\Policies\\Microsoft\\{name}\" /v UserDataDir /f\n"
            f"    （这策略一般是单位用域/组策略推下来的。删不掉的话问一下 IT —— "
            f"『Edge 的 UserDataDir 策略把浏览器的用户数据目录锁死了』。）")


def launch(profile_dir: Path, url: str = PORTAL_URL, headless: bool = False,
           port: int | None = None, no_sandbox: bool | None = None,
           settle: float | None = None, cfg: dict | None = None):
    # 返回 (进程, 调试端口)。进程可能是 Popen，也可能是 DetachedBrowser
    # （Chrome 把自己降权重启了，句柄作废，只能靠端口判断死活）
    """启动浏览器。返回 (进程, 调试端口)。

    `cfg` 用来定浏览器优先级（见 `find_browser`）—— 不传就用默认顺序。

    ⚠ **端口通了不等于活下来了**（实测踩过）：受限环境里 Chrome 起不了自己的
    子进程沙箱，它会在**端口应答之后大约 1~2 秒**整个进程退出。
    所以这里通完端口还要再观察 `settle` 秒；真死了就用 `--no-sandbox` 重试一次。
    门店电脑上不会触发这条，只有受限制的环境才需要。
    """
    # ⚠ **先查组策略**：被强制指定了 user-data-dir 的话，后面全是白忙 ——
    #   浏览器会忽略我们的 `--user-data-dir`，我们等的调试端口永远不会有人监听。
    #   实测就是这么卡了好几轮（最后是用户自己查出来的）——
    #   所以宁可现在就硬失败、把原因说清楚，也别让用户再看一遍"启动后立刻退出"。
    _forced = _forced_dir_error()
    if _forced:
        raise BrowserError(_forced)

    found = find_browser(cfg)
    if not found:
        # ⚠ 别提"Edge 是系统自带的" —— **Win7 上不是**，那台机器的店员会照着
        #   去"开始菜单里找 Edge"然后找不到。Win7 出厂只有 IE11，而 IE11
        #   既跑不了控制台页面也没有 CDP，两个功能都用不了。
        raise BrowserError(
            "没找到 Chrome / Edge。这两种浏览器都行，装一个再试；"
            "装好之后如果还是没有，就在 config 里写 "
            "`browser.prefer: [edge, chrome]` 指定，或把完整路径填进 browser.path。"
            "（Windows 7 上出厂只有 IE11 —— 它**不行**，得另外装 Edge 109 或 Chrome 109）")
    exe, name = found
    profile_dir = Path(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    port = port or _free_port()

    if no_sandbox is None:
        # 受限环境（容器 / 某些沙箱）里 Chrome 起不了自己的沙箱，会自动退一次重试
        no_sandbox = os.environ.get("CBG_BROWSER_NO_SANDBOX") == "1"

    args = [
        exe,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={port}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-background-networking", "--disable-sync",
        "--disable-features=Translate,MediaRouter",
        "--window-size=1100,860",
    ]
    if headless:
        # ⚠ `--headless=new` 这个写法对老的 Chromium 也是安全的，别"顺手"改成 `--headless`。
        #   新式无头是 Chrome/Edge **112** 才有的；在更老的版本上（Win7 最高 109），
        #   `--headless=new` 会被当成"headless 开关 + 值 new"，解析成**老式无头**——
        #   功能照样有，只是实现是旧的。反过来写成裸 `--headless`，
        #   在 112~131 上会走**老式**无头（跟新机器的预期不一致）。
        args.append("--headless=new")
    if no_sandbox:
        args += ["--no-sandbox", "--disable-gpu"]
    args.append(url)

    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if platform.system() == "Windows":
        kwargs["creationflags"] = 0x00000008 | 0x00000200     # DETACHED | NEW_PROCESS_GROUP
    proc = subprocess.Popen(args, **kwargs)

    settle = SETTLE_SECONDS if settle is None else settle
    deadline = time.time() + 25
    # ⚠ 进程死了**不代表没戏**：Chrome 从管理员进程启动时会把自己降权重启
    #   （AutoDeElevate），端口要等那条新进程起来。所以死了之后还继续探一会儿。
    #   用**探测次数**而不是墙钟时间 —— 否则测试里 sleep 被 mock 掉以后会空转 8 秒。
    grace = max(1, int(DE_ELEVATE_GRACE / PROBE_INTERVAL))
    probes_after_death = 0
    up = False
    while time.time() < deadline:
        try:
            http_json(port, "/json/version", timeout=1)
            up = True
            break
        except CdpError:
            if proc.poll() is not None:
                probes_after_death += 1
                if probes_after_death > grace:
                    break
            time.sleep(PROBE_INTERVAL)

    # 端口通了也别急着返回 —— 再盯一会儿，看它会不会立刻死掉。
    # ⚠ 盯完**要再确认一次端口**：受限环境里 Chrome 会在端口应答之后 1~2 秒
    #   整个退出，那时进程和端口一起没，跟"降权重启"（进程没、端口在）不一样。
    if up and settle > 0:
        time.sleep(settle)
        if not _port_alive(port):
            up = False

    if up:
        if proc.poll() is None:
            return proc, port
        # 进程没了、端口还在 —— 就是 Chrome 把自己降权重启了。
        # 浏览器是活的，只是不再是我们 Popen 的那条进程。
        return DetachedBrowser(port), port

    # 到这里就是真没起来
    try:
        proc.kill()
    except Exception:                              # noqa: BLE001
        pass
    if not no_sandbox:
        # 自动降级重试一次：受限环境里 Chrome 建不了自己的子进程沙箱
        #
        # ⚠ **要把第一次的退出码带下去**：两次的退出码**含义完全不同** ——
        #   0  = 交棒给别的实例后正常退出（典型的"管理员身份"那种）
        #   21 = Chromium 的 PROFILE_IN_USE（profile 被占着）
        #   而重试自己也会产生一个退出码，**抛出去的是重试那个** ——
        #   于是"第一次是 0"这个关键线索被吞掉了。
        #   实测踩过：用户只看到一个 21，而真正的原因是第一次那个 0。
        first_code = proc.returncode
        try:
            return launch(profile_dir, url, headless, port, no_sandbox=True,
                          settle=settle, cfg=cfg)
        except BrowserError as e:
            raise BrowserError(
                f"{e}\n（补充：不加 `--no-sandbox` 的那**第一次**，退出码是 {first_code}。"
                "两次退出码含义不同 —— 0 多半是权限那条，21 是 profile 被占，"
                "对着上面逐条看）") from e
    raise BrowserError(
        f"{name} 启动后立刻退出（退出码 {proc.returncode}）。"
        + _launch_failure_hint(name, profile_dir, returncode=proc.returncode))


# --------------------------------------------------------------- 从浏览器取
def _pick_cbg_page(port: int) -> str | None:
    """找一个在 cbg.huawei.com 上的标签页；没有就开一个。"""
    for t in page_targets(port):
        if "cbg.huawei.com" in (t.get("url") or ""):
            return t["webSocketDebuggerUrl"]
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/json/new?{PORTAL_URL}", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode()).get("webSocketDebuggerUrl")
    except Exception:                                     # noqa: BLE001
        return None


def _all_cookies(port: int) -> list[dict]:
    """先试浏览器级的 Storage.getCookies，不行再退回页面级的 Network.getAllCookies。"""
    cdp = Cdp.connect(port)
    try:
        raw = cdp.call("Storage.getCookies", {}, timeout=10).get("cookies") or []
        if raw:
            return raw
    except CdpError:
        pass
    finally:
        try:
            cdp.close()
        except Exception:                                 # noqa: BLE001
            pass

    page = _pick_cbg_page(port)
    if not page:
        return []
    pg = Cdp(page, timeout=10)
    try:
        pg.call("Network.enable")
        return pg.call("Network.getAllCookies", timeout=10).get("cookies") or []
    except CdpError:
        return []
    finally:
        pg.close()


def pick_cookies(raw: list[dict]) -> tuple[str, dict]:
    """从 CDP 给的 cookie 列表里挑出该发给华为的那些。**纯函数，好测。**

    - 只保留华为相关域（别把别的站的 cookie 带出去）
    - 同名 cookie 取最贴合 cbg.huawei.com 的那个
    - 埋点 cookie 交给 session._clean_cookies 统一丢掉
    """
    def rank(dom: str) -> int:
        d = (dom or "").lstrip(".")
        if d == "cbg.huawei.com":
            return 0
        if d.endswith("huawei.com"):
            return 1
        return 2

    picked: dict[str, dict] = {}
    for c in sorted(raw or [], key=lambda c: rank(c.get("domain", ""))):
        dom = (c.get("domain") or "").lstrip(".")
        if not any(dom == d.lstrip(".") or dom.endswith(d) for d in _COOKIE_DOMAINS):
            continue
        name = c.get("name")
        if name and name not in picked:
            picked[name] = c

    raw_str = "; ".join(f"{k}={v.get('value', '')}" for k, v in picked.items())
    return _clean_cookies(raw_str), picked


def cookies_from_browser(port: int) -> tuple[str, dict]:
    """返回 (cookie 串, 详细信息)。"""
    return pick_cookies(_all_cookies(port))


def csrf_from_page(port: int, timeout: float = 6.0) -> str | None:
    """在 cbg 页面上读 localStorage —— 前端把 csrf token 存在这里。"""
    page = _pick_cbg_page(port)
    if not page:
        return None
    pg = Cdp(page, timeout=timeout)
    try:
        res = pg.call("Runtime.evaluate", {
            "expression": f"window.localStorage.getItem({CSRF_STORAGE_KEY!r}) || ''",
            "returnByValue": True,
        }, timeout=timeout)
        val = (res.get("result") or {}).get("value")
        return val.strip() if isinstance(val, str) and val.strip() else None
    except CdpError:
        return None
    finally:
        pg.close()


def csrf_from_api(cookies: str, timeout: int = 20) -> str | None:
    """退路：csrf 也能从接口换（前端 js 里就是 `post("phoenix-gws/phoenix.sso.csrf.token")`）。"""
    try:
        r = requests.post(f"{CBG_BASE}{CSRF_PATH}",
                          headers={"accept": "application/json", "cookie": cookies,
                                   "referer": PORTAL_URL,
                                   "user-agent": "Mozilla/5.0"},
                          timeout=timeout)
        if r.status_code != 200:
            return None
        try:
            j = r.json()
        except ValueError:
            return None
        for key in ("data", "csrfToken", "token", "result"):
            v = j.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                for k2 in ("csrfToken", "token", "data"):
                    v2 = v.get(k2)
                    if isinstance(v2, str) and v2.strip():
                        return v2.strip()
        return None
    except requests.RequestException:
        return None


def capture_session(profile_dir: Path, *, headless: bool = False, timeout: float = 240,
                    on_step=None, verify=None, url: str | None = None,
                    credentials: tuple[str, str] | None = None,
                    cfg: dict | None = None, state_root=None,
                    on_need=None) -> CbgSession:
    """抓一份可用会话。

    headless=False：弹窗口（首次 / SSO 过期时；有账号密码就自动填）
    headless=True ：复用 profile 里的登录态静默取 cookie（日常自动续期）
    url           ：从哪个页面开始（默认门店日常入口，未登录会自己跳 SSO）
    credentials   ：(账号, 密码)。给了就自动填表提交；没给就等人手动登。

    verify: 校验函数 `(CbgSession) -> bool`。**一定要传** —— 光看"cookie 名字对不对"
        不够（未登录时 cbg 也会发 JSESSIONID）。校验不过就继续等，
        绝不把没验证过的凭据交出去。
    state_root：给了就维护"上次撞了验证码"的标记（`.secrets/capture-state.json`）。
        ⚠ 必须**显式传**，不从 `profile_dir.parent` 推 ——
        `session.browser_profile` 可以配成任意绝对路径，推出来的 `.secrets/`
        可能根本不是我们的。
    on_need   ：`(what: str) -> None`，运行中"还差人做一件事"时回调。
        目前只有 `'captcha'`（手动登录时页面要验证码）。
    """
    say = on_step or (lambda msg: None)
    start_url = url or PORTAL_URL
    user, pwd = credentials or ("", "")
    if state_root is not None and credentials and captcha_marked(state_root):
        # ⚠ 上一次就是被验证码挡下来的。再自动填一次只会把**同一个**验证码
        #   再撞出来，然后又被中止 —— 死循环，"请手动登录"那句提示永远执行不了。
        say("上次抓取撞上了图形验证码 —— 这次**不自动填账号密码**，"
            "请在窗口里手动登录（含验证码）")
        user = pwd = ""
    proc, port = launch(Path(profile_dir), url=start_url, headless=headless, cfg=cfg)
    say(f"浏览器已启动（调试端口 {port}）")
    say(f"打开：{start_url}")

    deadline = time.time() + timeout
    last = ""
    last_reason = ""            # 自检/接口给出的**原因** —— 最后要一起报出去
    specific = ""               # 循环里查到的**更具体**的卡点（见下面的 csrf 那条）
    csrf_src = ""
    said_invalid = False
    tries = 0
    nav_at = time.time()
    nav_done = False
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                # ⚠ 走到这里只说明**调试端口连续探不到**（见 DetachedBrowser.poll），
                #   不等于"用户把窗口关了"：Edge 在管理员身份下自我降权重启时，
                #   端口会短暂失灵。报错必须把这一点说清楚，别指错方向。
                if getattr(proc, "detached", False):
                    why = getattr(proc, "last_failure", "") or f"调试端口 {port} 无应答"
                else:
                    why = (f"浏览器进程退出了"
                           f"（退出码 {proc.returncode}）")
                raise CbgAuthError(
                    f"抓取中途浏览器没了：{why}。"
                    + _launch_failure_hint("浏览器", profile_dir, reason=why))
            try:
                cookies, detail = cookies_from_browser(port)
            except CdpError as e:
                last = f"读 cookie 失败：{e}"
                time.sleep(2)
                continue

            names = {c.split("=", 1)[0] for c in cookies.split("; ") if c}
            if names & {"JSESSIONID", "HWSTORE-SESSION", "hwssot3", "WPSESSIONID"}:
                # ⚠ 两条路都记下来源：页面 localStorage 里可能是**过期的** csrf
                #   （老 profile 留下的），而接口换的一定是当前的。
                #   报错时带上它，能一眼看出是不是这个原因。
                csrf = csrf_from_page(port)
                csrf_src = "页面 localStorage"
                if not csrf:
                    csrf = csrf_from_api(cookies)
                    csrf_src = "接口换的" if csrf else ""
                if csrf:
                    sess = CbgSession(cookies=cookies, csrf=csrf, source="browser",
                                      extra={"cookies_detail": detail,
                                             "csrf_source": csrf_src})
                    ok, why = _verify_result(verify, sess)
                    if ok:
                        say(f"✅ 抓到 {len(names)} 个 cookie + csrf token（{csrf_src}），自检通过")
                        if state_root is not None:
                            # 抓到一次就恢复正常：以后照旧自动填账号密码
                            clear_captcha(state_root)
                        return sess
                    last_reason = why or last_reason
                    if not said_invalid:
                        # ⚠ 未登录时 cbg 也会发 JSESSIONID —— 名字齐 ≠ 能用
                        #   把**自检给的原因**一起说出来：少了它这句等于没说，
                        #   用户只知道"没过"，不知道是权限、过期还是接口异常。
                        say("已拿到 cookie，但自检没过"
                            + (f"：{why}" if why else "（原因未知）")
                            + " —— 继续等登录完成…")
                        said_invalid = True
                else:
                    specific = (f"cookie 有了（{len(names)} 个），但 csrf 取不到 —— "
                                "页面 localStorage 和接口两条路都试过了")

            # ---- 自动登录 ----
            if user and pwd and tries < MAX_LOGIN_TRIES:
                r = try_auto_login(port, user, pwd, say)
                if r == "submitted":
                    tries += 1
                    say(f"等待登录跳转…（第 {tries}/{MAX_LOGIN_TRIES} 次）")
                    time.sleep(6)
                    continue
                if r == "captcha":
                    # ⚠ 不在 `finally` 里删 profile：那时浏览器**还开着**，
                    #   Windows 上有句柄就删不掉。抛出去，让调用方在
                    #   `finally` 跑完之后删 —— 见 CbgCaptchaRequired 的文档。
                    raise CbgCaptchaRequired(profile_dir)
                elif r == "error":
                    # 账号密码不对之类 —— 再试只会把账号试锁，直接停
                    say("⚠️ 账号密码可能不对 —— 停止自动重试（免得把账号试锁），"
                        "请到「会话」页核对账号密码，或在窗口里手动登录")
                    user = pwd = ""
                    nav_done = True
            elif user and tries >= MAX_LOGIN_TRIES:
                say("⚠️ 自动登录试了几次都没成 —— 请在窗口里手动登录一次")
                user = pwd = ""
                nav_done = True

            # ---- 兜底：一直没跳到登录页就自己导航过去 ----
            # ⚠ **无头模式下绝对不要做这件事**：没有人能看到那个窗口，
            #   导航过去只会让浏览器停在一个要人工操作的页面上干等到超时。
            #   实测（门店）：静默续期时 profile 里没有有效 SSO 登录态，
            #   程序傻等 90 秒才报"拿到了 cookie，但自检一直没过" ——
            #   而真正该做的是**先点一次「打开浏览器抓取」**。
            if not headless and not nav_done and time.time() - nav_at > 12:
                goto_url(port, login_page_url(start_url), say)
                nav_done = True
                nav_at = time.time()

            # 把"到底卡在哪一步"说准 —— 这两种情况的排查方向完全不同
            # ⚠ 这段是**兜底总结**，只在循环里没查出更具体的原因时才用。
            #   原来它无条件覆盖 `last`，于是循环里刚查到的"csrf 取不到"这类
            #   精确卡点被冲掉，用户看到的还是那句笼统的"没看到登录 cookie" ——
            #   而那句会把人指去查登录，实际卡在别处。（写测试时被逮住的。）
            if said_invalid:
                # 拿到 cookie 了但自检没过：**静默续期时最常见的原因**是这台机器
                # 还没有过一次"有界面"的登录（profile 里没有有效 SSO 登录态），
                # 而无头模式没人能补这次登录 —— 所以要明确指向下一步动作。
                last = ("拿到了 cookie，但自检一直没过（登录可能没真正完成，"
                        "或账号没这个门店的权限）"
                        + ("；静默续期要求这台电脑**之前用「打开浏览器抓取」"
                           "成功登录过**，没有的话请改用它" if headless else
                           " —— 请在窗口里完成登录"))
            elif specific:
                last = specific
            else:
                last = "始终没看到登录 cookie" + (
                    "（窗口开着，请在窗口里完成登录）" if not (user and pwd) else "（自动登录没成功）")
            time.sleep(3)
    finally:
        # ⚠ "浏览器停在哪一页"要在 `_shutdown` **之前**问 —— 关掉之后就问不到了。
        #   所以放在 finally 里取，报错在 finally 外面抛。
        where = _where_is_the_browser(port)
        _shutdown(proc)

    # ⚠ **把自检给的原因附在最前面**：那句话里通常直接写着病因
    #   （"没有门店或数据范围 XXX 的权限" / "接口异常：…" / csrf 取不到），
    #   而原来的报错把它丢了，只留一句"自检没过" —— 用户只能反复说"就是抓不到"。
    reason = f"\n自检/接口说的是：{last_reason}" if last_reason else ""
    if not last_reason and csrf_src:
        reason = f"\ncsrf 来源：{csrf_src}"
    raise CbgAuthError(f"{int(timeout)} 秒内没抓到可用会话：{last}{reason}{where}")


def _verify_result(verify, sess) -> tuple:
    """跑校验，归一成 `(过没过, 没过的话为什么)`。

    ⚠ **以前这里只要一个 bool，于是最有用的那句话被丢掉了。**
    `CbgClient.ping()` 返回的是 `(ok, msg)`，而 `msg` 里写着真正的病因——
    比如「会话/权限问题：没有门店或数据范围 XXX 的权限」「接口异常：…」。
    老写法 `verify=lambda s: client.ping()[0]` 把 `msg` 直接扔掉，
    用户最后只能看到一句"拿到了 cookie，但自检一直没过" ——
    对排查**毫无帮助**（实测就卡在这儿：只能反复说"就是抓不到"）。

    约定：`verify` 返回 `bool`（老写法，原因未知）或 `(bool, str)`（推荐）。

    没给 `verify` 时按"通过"—— 那是调用方自己放弃校验，不是我们的判断。
    """
    if verify is None:
        return True, ""
    try:
        got = verify(sess)
    except Exception as e:                   # noqa: BLE001
        # 校验函数自己炸了也要报出来：这跟"校验没过"是两回事，
        # 混成一句"自检没过"会让人去查账号，而其实是代码问题。
        return False, f"自检函数自己报错：{type(e).__name__}: {e}"
    if isinstance(got, tuple) and len(got) == 2:
        return bool(got[0]), str(got[1] or "")
    return bool(got), ""


def _where_is_the_browser(port: int) -> str:
    """超时时补一句：**浏览器现在停在哪一页**。

    ⚠ 这是排查"抓不到会话"时最想知道的一件事，而原来恰恰没有：
    窗口是卡在登录页？还是已经登进去了、但停在一个我们没预期的页面
    （比如**多门店账号**登录后的「选择门店」）？
    没有这一句，用户只能说"就是抓不到"，我们只能猜 —— 实测就卡在这儿。

    拿不到就返回空串：诊断信息**永远不该**让原本的报错变成另一个报错。
    """
    try:
        urls = [t.get("url") or "" for t in page_targets(port)]
    except Exception:                        # noqa: BLE001
        return ""
    urls = [u for u in urls if u and not u.startswith("devtools://")]
    if not urls:
        return ""
    return "\n窗口里现在的页面：\n" + "\n".join(f"  · {u}" for u in urls[:5])


def _shutdown(proc):
    # DetachedBrowser 的 terminate/kill 走的是 CDP 的 Browser.close，
    # wait() 看的是端口 —— 所以这里不用分支，两种都能关。
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:                                     # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                 # noqa: BLE001
            pass


def probe_session(profile_dir: Path, *, headless: bool = True, timeout: float = 90,
                  verify=None, url: str | None = None,
                  credentials: tuple[str, str] | None = None,
                  cfg: dict | None = None) -> CbgSession | None:
    """静默模式：取不到可用会话就返回 None（不报错）。"""
    try:
        return capture_session(Path(profile_dir), headless=headless, timeout=timeout,
                               on_step=lambda m: None, verify=verify,
                               url=url, credentials=credentials, cfg=cfg)
    except (CbgAuthError, BrowserError):
        return None
