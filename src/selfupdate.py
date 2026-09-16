"""从 GitHub 检查更新 / 手动更新。

**设计取舍**

* **不走 git**：门店电脑上不装 git，也不该装。下载的是仓库的 zip 包
  （两个源，优先 `api.github.com` 的 zipball，见下面的 `ZIP_URLS`），
  解开来**照着铺一遍**就完事了。所以"解压正式包"和"git clone"两种装法，
  更新效果完全一样。
* **检查**读的是仓库里的 `src/version.py`（raw 文件，不走 API）——
  这样不用维护第二个版本号来源，改代码时只动一个地方。也不吃 GitHub API
  的速率限制（门店电脑没配 token，匿名只有 60 次/小时）。
* **只覆盖代码，绝不碰数据**：`config/`、`.secrets/`、`out/` 三处一根手指都不碰 ——
  碰了就是丢门店配置 / 华为会话 / 历史报告。

**仓库布局 == 安装布局**（`packaging/` 中间层已经删了），所以更新就是
**照仓库原样铺到安装目录**，不需要任何映射表。只有 `NEVER_TOUCH` 里的顶层目录
（或顶层文件）被排除，加一个新文件不用回来改这里的代码。

唯一的**文件级**例外是 `config/stores.yaml`（见 `ALLOW_EVEN_IF_NEVER`）：
它是随程序走的门店映射表，加了新店要能带下去；而同一个目录里的
`config/store-<门店码>.yaml` 是**这台电脑**的配置，绝对不能动。
按目录一刀切会二选一错一个，所以只能精确到文件。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# 更新源。想换仓库改这一行即可。
REPO = "HappyJoy95/cbg-reconcile"
BRANCH = "main"

RAW_VERSION = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/src/version.py"
# ⚠ 版本检查**优先用这个**，不是 raw。
#
# 实测踩到：`raw.githubusercontent.com` 有 ~5 分钟的 CDN 缓存
# （`cache-control: max-age=300`）。刚发的版本它还在返回旧的 ——
# 表现就是"明明发了 v1.3.4，检查更新却说没有新版"，而且加时间戳参数没用
# （CDN 层的缓存不看 query）。`contents` 接口拿到的是当前提交，实测是新的。
#
# 代价：匿名每小时 60 次配额。门店一天查一次，够用；raw 留作退路。

# ⚠ 下载 zip 也是**两个源，优先 API 的 zipball**。
#
# 实测：`codeload.github.com` 会发**缓存的旧 zip** —— 仓库已经改成新布局了，
# 它还在给旧的那份（加时间戳参数也没用）。对更新功能来说这是**危险**的：
# 你以为更新了，实际拿到的是旧代码，甚至是旧的目录结构。
# `api.github.com/.../zipball` 拿到的是当前提交，实测是新的。
#
# `ref` 可以是分支名、tag，也可以是**某个 commit 的 sha** —— 回退功能靠它
# 拿历史版本（见 `download` / `rollback`）。
WEB_URL = f"https://github.com/{REPO}"


def _zip_urls(ref: str) -> tuple:
    """按 ref 生成两个下载地址（api 优先，codeload 退路）。"""
    return (
        f"https://api.github.com/repos/{REPO}/zipball/{ref}",
        f"https://codeload.github.com/{REPO}/zip/refs/heads/{ref}",
    )


def _version_api_url(ref: str = BRANCH) -> str:
    return f"https://api.github.com/repos/{REPO}/contents/src/version.py?ref={ref}"


# 检查结果的缓存：别每刷一次页面就去问一次 GitHub。
CACHE_FILE = ".secrets/update-check.json"
#
# ⚠ 别调大。实测踩过：发完 v1.3.4 想验证，本机点「检查更新」看到的还是旧结论
#   —— 因为 1 小时前那次检查（当时最新还是旧版）还压在缓存里。
#   缓存是**给页面刷新去重**用的（概览页 30 秒刷一次），不是给"发版后想看结果"
#   添堵的。主动点「检查更新」走的是 force=1，不受这里影响。
CACHE_TTL = 3600

# ------------------------------------------------------------------ 每日自动检查
#
# 光有按钮没用：门店同事**不会去点**。不主动查一次，那台电脑就永远停在装上去
# 的那一版 —— 修好的 bug 也到不了店里。
#
# 所以后台自己每天问一次，有新版本就在控制台上挂提醒，**点不点由人决定**。
# 不自动应用：更新完服务会重启，撞上 21:00 那趟对账就把任务断了。
WATCH_INTERVAL = 24 * 3600     # 每 24 小时查一次
WATCH_FIRST_DELAY = 90         # 启动后先等 90 秒 —— 别和开机那一刻抢网络


def _log(msg: str) -> None:
    """打一行日志。**绝不抛异常。**

    这个线程跑在后台服务里（门店是 `pythonw.exe`，没有控制台；或者 stdout
    被重定向到文件）—— 那时 Python 按 locale 编码（中文 Windows 是 GBK）写输出，
    编不出来的字符会抛 UnicodeEncodeError。日志写不出来无所谓，
    但**不能因此把更新检查弄死**。
    """
    try:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)
    except Exception:                                        # noqa: BLE001
        pass

# **绝不碰**的顶层目录 —— 这是整个功能的底线。
#
# 为什么可以用黑名单（而不是白名单）：仓库里**只有代码**。
# `config/`、`.secrets/`、`out/` 都在 .gitignore 里，从出生那天起就不会进仓库。
# 所以"照着仓库铺一遍、但这几个不许碰"是安全的，而且加新文件时不用改这里。
#
# ⚠ 万一哪天有人把 `config/` 提交进仓库了，这个名单是最后一道闸 ——
#   tests/test_selfupdate.py 里有一条测试专门验它。
NEVER_TOUCH = ("config", ".secrets", "out", "dist", "tools", "__pycache__", ".dsh",
               "agent.md")

# ⚠ `.dsh/` 也在名单里：它是**工作区隔离区**（记忆日志 / 备份 / 临时任务 /
#   本机 venv），跟 `.secrets/` 一样是"这台电脑自己的东西"。
#   它本来就在 `.gitignore` 里、进不了仓库，所以自更新拿到的 zip 里不会有它；
#   列在这里是**明说这件事**，顺便让 `tools/build_package.sh` 的排除项
#   跟这里对得上（那边以前漏了 `.dsh/`，把本机 venv 打进过包，被自检逮住）。
#
# ⚠ `agent.md` 是名单里**唯一一个文件**（其余都是顶层目录）。
#   它是"给 AI agent 看的开发/部署指令"，2026-09-16 用户要求挡在门店外面。
#   它和 `AGENTS.md` **不撞名**（`AGENTS` 六个字母 → `agents.md`），别搞混。
#   为什么单独挡它、而 README/AGENTS/运维手册 仍然照铺（那是 AGENTS.md 里
#   "有意留着"的决定）：那三本是**查资料**用的，翻到也就翻到了；
#   这本是**叫人动手**的（备份、改代码、跑测试），出现在门店目录里性质不一样。
#   `parts[0]` 对文件同样成立，所以放进这个元组就能生效（见 `_targets`）。

# 例外：这几个虽然在 NEVER_TOUCH 底下，但它们是**随程序走的**，不是门店自己的数据。
#   config/stores.yaml —— 14 家体验店的映射表（串号标识 → 门店）。加了新店，
#   更新时当然要带下去；而隔壁的 config/store-SCN231409.yaml 是**这台电脑**的配置，
#   绝对不能动。两个文件在同一个目录里，只能用**文件级**例外区分。
ALLOW_EVEN_IF_NEVER = ("config/stores.yaml",)

# 下载下来的东西**必须**包含这几个 —— 用来确认"这确实是我们的包"。
# 现在是照原样铺（不是白名单），万一返回了个错误页、或者仓库地址写错了，
# 没有这道闸就会把一堆不相干的文件铺进安装目录。
#
# 每项带一句说明：报错时要把"缺的是什么、它是干什么的"一起说出来 ——
# 门店看到 `缺少 ['src/cli.py']` 是没法自己判断的。
ANCHORS = (
    ("src/cli.py", "命令行入口，对账全靠它"),
    ("bootstrap.py", "install / start 那些 bat 的统一入口"),
)


class UpdateError(RuntimeError):
    pass


# ⚠ **必须重试**。实测（走代理的网络）：TLS 握手会被间歇性掐断，
#   报 `SSLEOFError: EOF occurred in violation of protocol`——
#   手动再试一次就好了。门店电脑挂代理/防火墙也是这个形状。
#   不重试的话，检查更新会"时好时坏"，用户完全摸不着规律。
TRIES = 3
BACKOFF = 1.5          # 秒；第 n 次等 n * BACKOFF


def _get(url: str, *, timeout: int, stream: bool = False, tries: int = None):
    """带重试的 GET。每次都用**新的 Session** —— 连接池里的坏连接别再复用。"""
    import requests
    n = TRIES if tries is None else max(1, tries)
    last = None
    for attempt in range(1, n + 1):
        try:
            with requests.Session() as s:
                r = s.get(url, timeout=timeout, stream=stream)
                r.raise_for_status()
                return r
        except requests.RequestException as e:
            last = e
            if attempt < n:
                time.sleep(BACKOFF * attempt)
    raise UpdateError(f"连不上 GitHub（试了 {n} 次）：{last}")


# ------------------------------------------------------------------ 版本比较
def parse_version(text: str) -> tuple:
    """`1.2.10` → `(1, 2, 10)`。比不出数字的都当 0，不抛异常。

    别用字符串比 —— `"1.10.0" < "1.9.0"` 会得出 True，那就永远升不上去。
    """
    parts = []
    for chunk in re.split(r"[.\-+]", (text or "").strip()):
        m = re.match(r"^(\d+)", chunk)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _version_in_text(text: str) -> str:
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text or "", re.M)
    if not m:
        raise UpdateError("那边那个 version.py 里找不到 VERSION")
    return m.group(1)


def remote_version(timeout: int = 15) -> str:
    """去 GitHub 读最新版本号。

    ⚠ **优先走 api.github.com，不是 raw**。实测踩到：`raw.githubusercontent.com`
    有大约 5 分钟的 CDN 缓存（`cache-control: max-age=300`），刚发的新版本它还在
    返回旧的 —— 表现就是"明明发了 v1.3.4，检查更新却说没有新版"。
    加时间戳参数没用（那是 CDN 层的缓存，不看 query）。

    `api.github.com/.../contents` 没有这个问题，代价是匿名每小时 60 次配额 ——
    门店一天查一次，完全够。raw 留作退路（不吃配额，但会慢一个 CDN 周期）。
    """
    errs = []
    for label, fetch in (_api_version_source(timeout), _raw_version_source(timeout)):
        try:
            return fetch()
        except UpdateError as e:
            errs.append(f"{label}: {str(e)[:70]}")
    raise UpdateError("拿不到最新版本号 —— " + "；".join(errs))


def _api_version_source(timeout: int):
    """(标签, 取值函数) —— api.github.com，**没有 CDN 缓存**。"""
    def fetch() -> str:
        # tries=1：这个源不通就赶紧换 raw，别在这儿耗掉三次重试
        r = _get(_version_api_url(), timeout=timeout, tries=1)
        try:
            content = r.json().get("content") or ""
        except ValueError as e:
            raise UpdateError(f"返回的不是 JSON：{e}") from e
        if not content:
            raise UpdateError("响应里没有 content 字段")
        # GitHub 给的 content 是 base64，而且**带换行** —— 不洗掉解不出来
        try:
            text = base64.b64decode(content.replace("\n", "")).decode("utf-8", "replace")
        except (ValueError, TypeError) as e:
            raise UpdateError(f"base64 解不开：{e}") from e
        return _version_in_text(text)
    return "api.github.com", fetch


def _raw_version_source(timeout: int):
    """(标签, 取值函数) —— raw，不吃配额，但有 ~5 分钟 CDN 缓存。"""
    def fetch() -> str:
        r = _get(RAW_VERSION, timeout=timeout)
        return _version_in_text(r.text)
    return "raw.githubusercontent.com", fetch


# ------------------------------------------------------------------ 历史版本 / 回退
def _git_json(url: str, timeout: int = 20, tries: int = 2):
    """调 GitHub API 拿 JSON。

    `tries=2` 是折中：门店网络间歇性抽风（实测 TLS 会被掐断），试一次太容易失败；
    但也不能像别处那样试 3 次 —— 匿名配额只有 60 次/小时，失败请求也计数。
    """
    r = _get(url, timeout=timeout, tries=tries)
    try:
        return r.json()
    except ValueError as e:
        raise UpdateError(f"返回的不是 JSON：{e}") from e


def _version_from_message(message: str) -> str:
    """从提交信息里抠版本号。

    ⚠ **只认 `release: v1.4.2` 这种形式**，不能见 `vX.Y.Z` 就抓。

    实测踩到：`fix(update): 检查更新优先走…` 这种提交，正文里往往会提到
    "顺带把 CACHE_TTL…（v1.3.4 就这样）"。用宽松正则的话，这个 fix 提交会被
    贴上 `v1.3.4` 的标签 —— 列表里于是出现两个 v1.3.4，而且**都不是它真正的版本**。
    门店照着这个列表回退，等于闭着眼睛选。

    抠不出来就返回空串（调用方会把这个提交跳过）。
    """
    m = re.search(r"^\s*release:\s*v(\d+\.\d+(?:\.\d+)?)\b",
                  message or "", re.M | re.I)
    return m.group(1) if m else ""


def history(limit: int = 20) -> list:
    """能回退到的历史版本（最近的在前面）。

    每条：`{version, sha, short, date, message}`。**没有版本号的提交不返回** ——
    回退列表里要是塞满 `refactor:` / `fix:` 这种条目，人根本不知道该选哪个。
    """
    data = _git_json(f"https://api.github.com/repos/{REPO}/commits"
                     f"?sha={BRANCH}&per_page={min(max(limit, 1), 100)}")
    if not isinstance(data, list):
        raise UpdateError("拿历史版本失败：返回的结构不对（大概是被限流了）")

    out = []
    for item in data:
        commit = item.get("commit") or {}
        msg = commit.get("message") or ""
        ver = _version_from_message(msg)
        if not ver:
            continue
        sha = item.get("sha") or ""
        out.append({
            "version": ver,
            "sha": sha,
            "short": sha[:7],
            "date": (commit.get("committer") or {}).get("date", ""),
            "message": msg.splitlines()[0].strip(),
        })
    return out


def rollback(root, *, ref: str, current: str = "") -> dict:
    """把代码换成 `ref`（某个 commit sha）那一版。

    ⚠ **回退 = 一次普通的铺代码**，跟升级走同一条路（`apply_update`）——
    同样只碰代码，`config/`、`.secrets/`、`out/` 一根手指都不动。
    所以门店的配置、会话、历史报告都不会丢。
    """
    if not ref or not re.fullmatch(r"[0-9a-fA-F]{7,40}", (ref or "").strip()):
        raise UpdateError(f"这个看着不像 commit：{ref!r}")
    return apply_update(root, current=current, ref=ref.strip())


# ------------------------------------------------------------------ 检查
def _cache_path(root) -> Path:
    return Path(root) / CACHE_FILE


def read_cache(root) -> dict:
    try:
        d = json.loads(_cache_path(root).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_cache(root, data: dict) -> None:
    try:
        p = _cache_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def check(root, current: str, *, force: bool = False, timeout: int = 15) -> dict:
    """看看有没有新版本。网络不通**不算错误**，如实说一句就行。"""
    cached = read_cache(root)
    if not force and cached and time.time() - cached.get("at", 0) < CACHE_TTL:
        return _recompute(cached, current)

    now = time.time()
    info = {"at": now, "current": current, "repo": REPO, "web": WEB_URL}
    try:
        latest = remote_version(timeout=timeout)
        info.update({"latest": latest, "has_update": is_newer(latest, current),
                     "checked_at": now})
    except UpdateError as e:
        # ⚠ `at`（"下次还查不查"）和 `checked_at`（"上次真问到了是什么时候"）
        #   必须分开：失败也写 `at`，否则每刷一次页面就重试一次；
        #   但界面要能说出"上次成功问到是三天前"，那时网络就已经不通了。
        info.update({"latest": "", "has_update": False, "error": str(e),
                     "failed_at": now})
        if cached.get("checked_at"):
            info["checked_at"] = cached["checked_at"]
    write_cache(root, info)
    return info


def _recompute(cached: dict, current: str) -> dict:
    """缓存里的 `has_update` 是按**当时的版本**算的 —— 升级之后要重算。

    不重算的话：升到最新版了，界面还在说"有新版本"（缓存 6 小时）。
    """
    out = dict(cached)
    if out.get("latest"):
        out["current"] = current
        out["has_update"] = is_newer(out["latest"], current)
    return out


def cached(root, current: str) -> dict:
    """**只读**缓存，一个网络请求都不发。

    界面刷新走这条 —— 主动查是后台线程的活（每天一次）。别让开一次控制台
    就打一次 GitHub，门店网络本来就时通时断。
    """
    return _recompute(read_cache(root), current)


def daily_watcher(root, current: str, *, interval: int = WATCH_INTERVAL,
                  first_delay: int = WATCH_FIRST_DELAY, stop=None) -> None:
    """后台线程：每天检查一次更新，把结果写进缓存，界面据此挂提醒。

    **只报不装** —— 装不装由人决定。更新完服务会重启，撞上对账任务就断了。

    跑在守护线程里：崩了不能影响对账，也不能拦住进程退出。
    """
    if stop is not None:
        stop.wait(first_delay)
    while True:
        try:
            info = check(root, current, force=True, timeout=20)
            if info.get("has_update"):
                _log(f"发现新版本 v{info.get('latest')}（现在 v{current}）"
                     "—— 控制台「设置」里有「立即更新」")
            elif info.get("error"):
                _log(f"更新检查失败（不影响对账）：{info['error']}")
            else:
                _log(f"已是最新版本（v{current}）")
        except Exception as e:                              # noqa: BLE001
            # 这个线程**绝不能**因为任何原因死掉 —— 死了就永远不再检查了
            _log(f"更新检查出错了（不影响对账）：{type(e).__name__}: {e}")
        if stop is not None:
            if stop.wait(interval):
                return
        else:
            time.sleep(interval)


# ------------------------------------------------------------------ 更新
def _rel_key(rel) -> str:
    """相对路径的**比较键** —— 与平台无关，永远用正斜杠。

    ⚠ 门店 Windows 上踩到的真事：锚点检查原来是 `str(rel) not in ANCHORS`，
    而 Windows 上 `str(PureWindowsPath("src/cli.py"))` 是 **`src\\cli.py`** ——
    跟常量里的正斜杠**永远比不相等**。于是文件明明铺进去了，还是被判
    "缺少 src/cli.py"，更新被拒（门店连着卡了三次）。

    同一个坑还让 `ALLOW_EVEN_IF_NEVER` 失效 —— 那是 `config/stores.yaml` 的
    文件级例外，它失效就意味着**门店映射表永远更新不下去**（加了新店也带不到门店）。

    别用 `str(Path)`：它的分隔符随平台变。要比较就先过这里。
    """
    return str(rel).replace("\\", "/")


def _iter_files(zip_root: Path) -> list:
    """列出包里的所有文件。

    ⚠ **用 `os.walk` 而不是 `Path.rglob("*")`。**

    门店 Windows 上反复撞到一种诡异现象：同一个 `zip_root`，
    `(zip_root / "src/cli.py").is_file()` 说**在**、`rglob("cli.py")` 也能**搜到**，
    可是 `rglob("*")` 遍历出来的结果里**就是没有它** —— 于是更新被误判成
    "这不像我们的包"而拒绝，包其实是好的。（本地怎么试都复现不出来。）

    `os.walk` 走的是另一套目录遍历（`scandir`），行为更朴素、更可预测，
    也不掺和 `Path` 的符号链接/相对路径处理。这里不需要 glob 的花哨功能，
    只要"把文件列全"。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(zip_root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        base = Path(dirpath)
        for name in filenames:
            out.append(base / name)
    return sorted(out)


def _targets(zip_root: Path) -> list:
    """(zip 里的文件, 安装目录里的相对路径) —— 照原样铺，除了 NEVER_TOUCH。"""
    out = []
    for f in _iter_files(zip_root):
        if not f.is_file():
            continue
        rel = f.relative_to(zip_root)
        rels = _rel_key(rel)          # ⚠ 别用 str() —— Windows 上分隔符是反斜杠
        if rels not in ALLOW_EVEN_IF_NEVER and (
                rel.parts[0] in NEVER_TOUCH or "__pycache__" in rel.parts):
            continue
        if rel.name.startswith(".") and rel.name not in (".gitattributes", ".gitignore"):
            continue
        out.append((f, rel))
    return out


def _looks_like_html(blob: Path) -> bool:
    """下到的到底是个网页还是压缩包。

    门店那种网络里，代理 / 安全设备 / 镜像站经常**返回一个 200 的 HTML 页面**
    （"403 Forbidden"、"请先登录"、"该地址已被拦截"），文件名和 content-type
    却还是 zip。不认出来的话，报错只会说"File is not a zip file"，
    而人完全不知道该怎么办。
    """
    try:
        head = blob.read_bytes()[:400].lstrip().lower()
    except OSError:
        return False
    return head.startswith(b"<!doctype") or head.startswith(b"<html") or b"<html" in head


def _describe_payload(blob: Path, status: int, ctype: str) -> str:
    """「下载失败」时把**到底收到了什么**带出来。"""
    size = blob.stat().st_size if blob.exists() else 0
    head = ""
    try:
        head = blob.read_text(encoding="utf-8", errors="replace")[:200].strip()
    except OSError:
        pass
    out = f"HTTP {status} · {size} 字节 · content-type: {ctype or '(无)'}"
    if _looks_like_html(blob):
        out += f"\n      收到的是一个**网页**，不是压缩包 —— 开头是：{head[:120]}"
    return out


def download(timeout: int = 120, ref: str = BRANCH) -> Path:
    """把仓库的 zip 下到临时目录，返回解压出来的根目录。

    `ref` 可以是分支名，也可以是**某个 commit 的 sha** —— 回退要用。
    """
    import requests
    tmp = Path(tempfile.mkdtemp(prefix="cbg-update-"))
    blob = tmp / "src.zip"
    errors = []
    for url in _zip_urls(ref):
        host = url.split("/")[2]
        try:
            r = _get(url, timeout=timeout, stream=True)
            with open(blob, "wb") as fp:
                for chunk in r.iter_content(64 * 1024):
                    fp.write(chunk)
            with zipfile.ZipFile(blob) as z:
                z.extractall(tmp)
            break
        except (UpdateError, requests.RequestException, zipfile.BadZipFile, OSError) as e:
            # ⚠ 别只写一句 `BadZipFile: File is not a zip file` ——
            #   门店的网络会把响应换成网页（见 _looks_like_html），
            #   不说清收到的是什么，报错就等于没说。
            status = getattr(locals().get("r"), "status_code", "-")
            ctype = (getattr(locals().get("r"), "headers", {}) or {}).get("content-type", "")
            errors.append(f"{host}: {str(e)[:80]}\n      {_describe_payload(blob, status, ctype)}")
            # 换下一个源之前先清干净，免得两个源的解压结果混在一起
            for f in tmp.iterdir():
                if f == blob:
                    continue
                if f.is_dir():
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    try:
                        f.unlink()
                    except OSError:
                        pass
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError("下载失败 —— " + "；".join(errors))

    roots = [p for p in tmp.iterdir() if p.is_dir()]
    if len(roots) != 1:
        got = sorted(p.name for p in tmp.iterdir()) or ["(空)"]
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError(
            f"下载下来的包结构不对：应该有且只有一个顶层目录，"
            f"实际收到 {len(roots)} 个。\n    收到的东西：{got[:12]}")
    return roots[0]


def _describe_package(zip_root: Path) -> str:
    """这个包里到底有什么 —— 锚点缺失时用它把话说清楚。

    ⚠ 门店实测遇到过一种**自相矛盾**的报错：上面说"缺少 src/cli.py"，
    这里却说"缺的是 []"（= `is_file()` 认为文件在）。两个判断用的是同一个
    `zip_root`，结果不一致，说明**光看文件名看不出来**。所以这里把
    "文件到底落在哪"直接查出来：`src/` 下有什么、`rglob` 能不能找到 cli.py。
    """
    try:
        tops = sorted(p.name for p in zip_root.iterdir())[:15]
    except OSError as e:
        return f"    （这个目录都读不了：{e}）"

    pairs = _targets(zip_root)
    missing = [a for a, _ in ANCHORS if not (zip_root / a).is_file()]

    lines = [f"    顶层：{tops}",
             f"    位置：{zip_root}",
             f"    包内共 {len(pairs)} 个文件；缺的是 {missing}"]

    src = zip_root / "src"
    if src.is_dir():
        names = sorted(p.name for p in src.iterdir())
        lines.append(f"    src/ 下 {len(names)} 项：{names[:12]}")
    else:
        lines.append("    ⚠ 没有 src/ 这个目录")

    # 决定性的一问：cli.py 到底在不在（不管在哪一层）
    found = [str(p) for p in zip_root.rglob("cli.py")][:5]
    lines.append(f"    rglob 找 cli.py：{found or '（整个包里都没有）'}")

    # 上面两个判断打架时，明说 —— 这信息比"缺少 X"有用得多
    in_targets = {_rel_key(r) for _, r in pairs}
    if not missing and [a for a, _ in ANCHORS if a not in in_targets]:
        lines.append("    ⚠ 注意：is_file() 说文件在，但 _targets() 没带上它 ——"
                     " 这是程序内部不一致，请把这段整个发回来")
    return "\n".join(lines)


def _dump_inconsistency(root, zip_root: Path, expects: list) -> Path:
    """把"文件在，但 `_targets()` 没带上"的现场存到 `out/update-diag.txt`。

    ⚠ 为什么要落盘：这种不一致只在**门店的机器上**出现过（本地怎么试都好），
    而 `download()` 结束时会**把临时目录删掉** —— 事后想看现场什么都没了。
    用它的人当时也不知道该看什么，等问起来，包早没了。

    所以：碰到就自己存一份。下次再出问题，直接把这个文件发回来。
    """
    out = Path(root) / "out"
    lines = []
    try:
        out.mkdir(parents=True, exist_ok=True)
        lines.append(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"Python：{sys.version.split()[0]}   平台：{sys.platform}")
        lines.append(f"zip_root：{zip_root}")
        lines.append(f"锚点：{[a for a, _ in ANCHORS]}")
        lines.append(f"磁盘上存在但 _targets() 没带回的：{expects}")
        lines.append("")

        # 逐项对照：rglob 找到的 vs _targets 返回的
        all_files = [p for p in zip_root.rglob("*") if p.is_file()]
        pairs = _targets(zip_root)
        by_rglob = {p.relative_to(zip_root) for p in all_files}
        by_targets = {r for _, r in pairs}
        lines.append(f"rglob(is_file) 找到 {len(by_rglob)} 个；_targets 返回 {len(by_targets)} 个")
        lines.append(f"rglob 找到但 _targets 没带回的（{len(by_rglob - by_targets)} 个）：")
        for r in sorted(str(x) for x in (by_rglob - by_targets)):
            lines.append(f"    {r}")
        lines.append("")

        # 关键文件的 stat —— 看是不是类型/权限异常
        for a in expects:
            p = zip_root / a
            try:
                st = p.stat()
                lines.append(f"{a}: is_file={p.is_file()} is_symlink={p.is_symlink()} "
                             f"size={st.st_size} mode={oct(st.st_mode)}")
            except OSError as e:
                lines.append(f"{a}: stat 失败 {e}")
        lines.append("")

        # 那个文件到底在 rglob 的哪个位置
        name = Path(expects[0]).name if expects else ""
        if name:
            hits = [p for p in all_files if p.name == name]
            lines.append(f"rglob 里所有叫 {name} 的：")
            for p in hits:
                rel = p.relative_to(zip_root)
                lines.append(f"    {p}   → relative_to={rel}  parts={rel.parts}")

        path = out / "update-diag.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    except Exception as e:                                   # noqa: BLE001
        print(f"[更新] （诊断信息没写成，不影响升级：{e}）", flush=True)
        return None


def apply_update(root, *, current: str = "", ref: str = BRANCH) -> dict:
    """把代码换成 `ref` 那一版。**数据目录一根手指都不碰。**

    `ref` 默认 main（= 升级）；传某个 commit sha 就是**回退**到那一版。
    """
    root = Path(root)
    if not root.is_dir():
        raise UpdateError(f"安装目录不存在：{root}")

    zip_root = download(ref=ref)
    try:
        pairs = _targets(zip_root)
        have = {_rel_key(rel) for _, rel in pairs}
        missing = [(a, d) for a, d in ANCHORS if a not in have]
        if missing:
            # ⚠ 实测（门店 Windows）：`_targets()` 偶尔会**漏掉真实存在的文件** ——
            #   同一个 zip_root，`is_file()` 和 `rglob` 都说 `src/cli.py` 在，
            #   它却没带上。那时旧代码会拒绝升级、报"这不像我们的包"，
            #   而包其实是好的（门店照着提示手工换包，白跑一趟）。
            #
            #   所以先问一次文件系统：**真的在就用**，并且把这次不一致记下来。
            #   判断标准应该是"文件在不在"，而不是"某个遍历函数有没有带上它"。
            really_there = [a for a, _ in missing if (zip_root / a).is_file()]
            if really_there:
                # 打到服务控制台，并且**把现场存下来**（临时目录马上会被删掉）
                diag = _dump_inconsistency(root, zip_root, really_there)
                print(f"[更新] ⚠ 内部不一致：{really_there} 在磁盘上存在，"
                      f"但 _targets() 没带上；本次按存在处理。"
                      f"现场已存 → {diag}", flush=True)
                for a in really_there:
                    pairs.append((zip_root / a, Path(a)))
                pairs.sort(key=lambda p: str(p[1]))
                have = {_rel_key(rel) for _, rel in pairs}
                missing = [(a, d) for a, d in ANCHORS if a not in have]
        if missing:
            # ⚠ 光说"缺少 X"没用：门店的网络可能返回一个**完全不相干的 zip**，
            #   也可能是个网页。把实际结构带出来，一眼就知道该找谁。
            raise UpdateError(
                "下载下来的包里缺少 "
                + "、".join(f"{a}（{d}）" for a, d in missing)
                + " —— 这不像我们的包。\n"
                + _describe_package(zip_root)
                + "\n    （网络代理 / 镜像站换掉了内容？或者下载被截断了）"
                + "\n    手动升级：把正式包解压覆盖过去，见「运维手册 → 升级到新版本」")

        remote = ""
        try:
            remote = re.search(r'^VERSION\s*=\s*"([^"]+)"',
                               (zip_root / "src" / "version.py").read_text(encoding="utf-8"),
                               re.M).group(1)
        except (OSError, AttributeError, ValueError):
            pass

        changed, added = [], []
        for src, rel in pairs:
            dst = root / rel
            old = None
            try:
                old = dst.read_bytes()
            except OSError:
                pass
            new = src.read_bytes()
            if old == new:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            (changed if old is not None else added).append(_rel_key(rel))

        # 打完补丁写个指纹 —— 从 GitHub 更新过来的没有打包时间戳
        if remote:
            try:
                (root / "BUILD.txt").write_text(
                    f"GitHub {BRANCH} · v{remote}", encoding="utf-8")
            except OSError:
                pass

        return {"ok": True, "from": current, "to": remote,
                "changed": sorted(set(changed)), "added": sorted(set(added)),
                "count": len(set(changed) | set(added))}
    finally:
        shutil.rmtree(zip_root.parent, ignore_errors=True)


# ------------------------------------------------------------------ 重启
_SNIPPET = r"""
import os, subprocess, sys, time
pid, root, exe = int(sys.argv[1]), sys.argv[2], sys.argv[3]
win = os.name == "nt"
time.sleep(2.0)
for _ in range(80):                       # 最多等 40 秒
    if win:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True,
                             creationflags=0x08000000).stdout
        gone = str(pid) not in out
    else:
        try:
            os.kill(pid, 0); gone = False
        except OSError:
            gone = True
    if gone:
        break
    time.sleep(0.5)
kw = {"cwd": root, "stdin": subprocess.DEVNULL,
      "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if win:
    kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED | NEW_GROUP
else:
    kw["start_new_session"] = True
subprocess.Popen([exe, "-m", "src.cli", "serve", "--no-open"], **kw)
"""


def restart_later(root) -> bool:
    """起一个**脱离当前进程**的小助手：等我们退干净，再把服务拉起来。

    为什么不能在进程内重启：我们正跑在**刚被替换掉**的那些文件上。
    """
    root = Path(root)
    exe = sys.executable or "python"
    try:
        kw = {"cwd": str(root), "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "win32":
            kw["creationflags"] = 0x00000008 | 0x00000200
        else:
            kw["start_new_session"] = True
        subprocess.Popen([exe, "-c", _SNIPPET, str(__import__("os").getpid()),
                          str(root), exe], **kw)
        return True
    except OSError:
        return False
