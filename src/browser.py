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

# 登录态相关的 cookie 所在的域
_COOKIE_DOMAINS = ("cbg.huawei.com", ".huawei.com", "login.huawei.com", ".huawei.cn")

# ⚠ 顺序 = 优先级。**Chrome 在前**：
#   * 门店那些电脑上 Chrome 往往也装了，而它遇到的麻烦更少 —— Edge 从
#     管理员进程启动时自我降权重启（AutoDeElevate）更凶，抓会话更容易断；
#   * 想换别的浏览器不用改代码：config 里写 `browser.prefer: [edge, chrome]` 即可
#     （见 `_browser_prefer`）。
# 注意 Windows 上 Edge 是**系统自带**的，所以没装 Chrome 的机器仍然会用到它。
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


def profile_path(cfg: dict, root) -> Path:
    """配置文件里的 `session.browser_profile`，没配就用默认的 .secrets/browser-profile。"""
    rel = (cfg.get("session") or {}).get("browser_profile") or PROFILE_DIRNAME
    p = Path(rel)
    return p if p.is_absolute() else Path(root) / p


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


def _launch_failure_hint(name: str, profile_dir=None, reason: str = "") -> str:
    """启动失败时给一句**能照着做**的话。

    别只说"受限制的环境" —— 门店电脑上最可能的是管理员权限那条。

    `reason` 非空表示**不是启动失败，而是跑到一半浏览器没了**：
    这时两条"启动"相关的建议（profile 被占、沙箱）都不对症，先别列出来，
    否则门店照着一个不相干的建议折腾半天。
    """
    lines = []
    if reason:
        if _am_i_admin():
            lines.append("服务现在是**管理员**身份，而 Edge / Chrome 拒绝以管理员运行"
                         "——它会在跑到一半时把自己降权重启。"
                         "到「设置 → 后台服务」把开机自启改成**普通权限**，"
                         "或者右键 stop.bat 以管理员身份停止后，"
                         "再用**普通权限**双击 start.bat 重试")
        lines.append("**是不是把浏览器窗口手动关掉了？** 登录完**别关那个窗口** —— "
                     "程序还要从它那里读 cookie，读完它自己会关。"
                     "重新点一次「打开浏览器抓取」就行")
        lines.append("浏览器自己崩了或降权重启 —— 重新点一次「打开浏览器抓取」")
        return "\n可能的原因（按可能性排）：\n" + "\n".join(
            f"  {i}. {x}" for i, x in enumerate(lines, 1))

    if profile_dir is not None and profile_locked(profile_dir):
        lines.append("**profile 被占着**（检测到 SingletonLock）—— 有 Chrome/Edge "
                     "实例正在用这个 profile。关掉**所有** Chrome/Edge 窗口再试。"
                     "如果找不到窗口，打开任务管理器把残留的 chrome.exe / msedge.exe 结束掉")
    if _am_i_admin():
        lines.append("服务现在是**管理员**身份，而 Chrome 拒绝以管理员运行"
                     "（它会把自己降权重启，这次没成功）—— "
                     "到「设置 → 后台服务」把开机自启改回普通权限，或右键 stop.bat "
                     "以管理员身份停止后，用普通权限双击 start.bat 再试")
    lines.append("profile 目录有问题（坏掉了？）—— 删掉 .secrets\\browser-profile 让它重建")
    lines.append("受限环境（服务器 / 沙箱）里 Chrome 建不了子进程沙箱 —— "
                 "设环境变量 CBG_BROWSER_NO_SANDBOX=1 再试")
    return "\n可能的原因（按可能性排）：\n" + "\n".join(
        f"  {i}. {x}" for i, x in enumerate(lines, 1))


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
    found = find_browser(cfg)
    if not found:
        raise BrowserError("没找到 Chrome / Edge。Windows 上 Edge 是系统自带的，"
                           "要是都没有，就在 config 里写 browser.prefer 指定，"
                           "或把完整路径填进 browser.path")
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
        return launch(profile_dir, url, headless, port, no_sandbox=True,
                      settle=settle, cfg=cfg)
    raise BrowserError(
        f"{name} 启动后立刻退出（退出码 {proc.returncode}）。"
        + _launch_failure_hint(name, profile_dir))


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
                    cfg: dict | None = None) -> CbgSession:
    """抓一份可用会话。

    headless=False：弹窗口（首次 / SSO 过期时；有账号密码就自动填）
    headless=True ：复用 profile 里的登录态静默取 cookie（日常自动续期）
    url           ：从哪个页面开始（默认门店日常入口，未登录会自己跳 SSO）
    credentials   ：(账号, 密码)。给了就自动填表提交；没给就等人手动登。

    verify: 校验函数 `(CbgSession) -> bool`。**一定要传** —— 光看"cookie 名字对不对"
        不够（未登录时 cbg 也会发 JSESSIONID）。校验不过就继续等，
        绝不把没验证过的凭据交出去。
    """
    say = on_step or (lambda msg: None)
    start_url = url or PORTAL_URL
    user, pwd = credentials or ("", "")
    proc, port = launch(Path(profile_dir), url=start_url, headless=headless, cfg=cfg)
    say(f"浏览器已启动（调试端口 {port}）")
    say(f"打开：{start_url}")

    deadline = time.time() + timeout
    last = ""
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
                csrf = csrf_from_page(port) or csrf_from_api(cookies)
                if csrf:
                    sess = CbgSession(cookies=cookies, csrf=csrf, source="browser",
                                      extra={"cookies_detail": detail})
                    if verify is None or verify(sess):
                        say(f"✅ 抓到 {len(names)} 个 cookie + csrf token，自检通过")
                        return sess
                    if not said_invalid:
                        # ⚠ 未登录时 cbg 也会发 JSESSIONID —— 名字齐 ≠ 能用
                        say("已拿到 cookie，但自检没过 —— 继续等登录完成…")
                        said_invalid = True

            # ---- 自动登录 ----
            if user and pwd and tries < MAX_LOGIN_TRIES:
                r = try_auto_login(port, user, pwd, say)
                if r == "submitted":
                    tries += 1
                    say(f"等待登录跳转…（第 {tries}/{MAX_LOGIN_TRIES} 次）")
                    time.sleep(6)
                    continue
                if r == "captcha":
                    say("⚠️ 登录页要图形验证码 —— 自动登录走不通，"
                        "请在弹出的窗口里手动登录")
                    user = pwd = ""                    # 不再无谓重试
                    nav_done = True
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
            if said_invalid:
                # 拿到 cookie 了但自检没过：**静默续期时最常见的原因**是这台机器
                # 还没有过一次"有界面"的登录（profile 里没有有效 SSO 登录态），
                # 而无头模式没人能补这次登录 —— 所以要明确指向下一步动作。
                last = ("拿到了 cookie，但自检一直没过（登录可能没真正完成，"
                        "或账号没这个门店的权限）"
                        + ("；静默续期要求这台电脑**之前用「打开浏览器抓取」"
                           "成功登录过**，没有的话请改用它" if headless else
                           " —— 请在窗口里完成登录"))
            else:
                last = "始终没看到登录 cookie" + (
                    "（窗口开着，请在窗口里完成登录）" if not (user and pwd) else "（自动登录没成功）")
            time.sleep(3)
    finally:
        _shutdown(proc)

    raise CbgAuthError(f"{int(timeout)} 秒内没抓到可用会话：{last}")


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
