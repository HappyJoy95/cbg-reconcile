"""从 GitHub 检查更新 / 手动更新。

**设计取舍**

* **不走 git**：门店电脑上不装 git，也不该装。下载的是**发行包**，
  解开来**照着铺一遍**就完事了。所以"解压正式包"和"git clone"两种装法，
  更新效果完全一样。
* **只覆盖代码，绝不碰数据**：`config/`、`.secrets/`、`out/` 三处一根手指都不碰 ——
  碰了就是丢门店配置 / 华为会话 / 历史报告。

**两个仓库，各管一半**（2026-10-02 起源码仓 / 发行仓分离）

* `RELEASE_REPO`（**公开**，见下面 `RELEASE_ASSET`）—— 客户端**唯一依赖**的仓库：
  版本号在它的 `VERSION` 里，更新包在它的 Releases 里。
  它**不含任何 `.py` 源码**，连更新包本身都是**密文**（路线 A）。
* `REPO`（源码仓，将改 **Private**）—— **只作版本号的退路**：
  发行仓的 `VERSION` 读不到时才问它。

⚠ **下载没有源码仓退路**（2026-10-02 用户定「不过渡，当做没有门店用过」）：
  源码仓 zipball 给的是**明文**，留着它就等于公开渠道上还有一条明文路。
  所以下载只从发行仓拿，且 `_unseal_in_place` 收到明文**直接报错**（fail closed）。
  发行仓短时间不可用就等下一轮（`auto_update` 每小时一次），
  不为了可用性把明文路留着。

⚠ **顺序绝对不能反**（只针对**版本号**那两个源）：发行仓必须排在源码仓前面。
  反过来的话，源码仓一私有，还在跑旧代码的门店**当场就查不到更新**，
  而且没有任何补救 —— 这是整次迁移里唯一不可逆的一步，顺序就是它的保险。

⚠ **全程不碰 `github.com`**。实测（本机直连，无代理）：
  `api.github.com` 0.35s / `codeload` 0.5s / `objects.githubusercontent.com` 2.4s，
  而 `github.com` **curl 20s 超时**（门店网络多半更糟）。所以：
  版本走 `api.github.com/.../contents`，下载走 Release 资产的
  **`api.github.com` 资产地址**（带 `Accept: application/octet-stream`，见
  `_release_asset_url`），`github.com/.../releases/download/...` 只当最后的直链退路。

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
from typing import Optional

# ------------------------------------------------------------------ 两个仓库
# ⚠ **发行仓是公开的，而且不含源码** —— 它只放：`VERSION`（一行版本号）、
#   Releases 里的更新包、CHANGELOG、安装说明和用户文档。
#   源码仓（`REPO`）将来改 Private，那时它只作下面的退路。
RELEASE_REPO = "HappyJoy95/cbg-reconcile-release"
RELEASE_BRANCH = "main"

#: 发行仓里更新包的**固定名字** —— 每条 Release 都叫这个，
#: 于是 `/releases/latest/download/<它>` 永远指向最新那一版，不用先查 API。
#:
#: ⚠ **只发这一个，而且它是密文**（路线 A，2026-10-02 用户定：
#:   「不过渡，现在就当做没有门店用过」）—— 所以**没有明文资产那条退路**，
#:   `_unseal_in_place` 拿到明文**直接报错**（fail closed）。
#:   以前部署过的客户端用的是它们自己代码里那份名单，不受这里影响。
RELEASE_ASSET = "cbg-reconcile-update.zip.sealed"

# 源码仓。**发行仓排在它前面**（见模块文档里那条"顺序绝对不能反"）。
REPO = "HappyJoy95/cbg-reconcile"

# ⚠ 下面两行必须待在所有 `def` **之前**：`BRANCH` 被 `_version_api_url(ref=BRANCH)`、
#   `download(ref=BRANCH)`、`apply_update(ref=BRANCH)` 等的**默认参数在
#   import 那一刻捕获** —— 放到 def 后面再改分支，那些默认值还停在旧分支上。
#   `from . import edition` 也必须在这行之前（否则 `is_lifehall` 还没进来）。
from . import edition as _edition                           # noqa: E402
#: 更新渠道 —— 生活馆机器读 lifehall 分支（独立版本号、独立下发内容）。
#: ⚠ 这是 **lifehall 分支私有段**：合回 main 时要连同 `_targets` 的 PRUNE
#:   过滤、`whatsnew.LIFEHALL_SKIP` 一起处理，别单独把这一行合过去。
BRANCH = "lifehall" if _edition.is_lifehall() else "main"

# 版本源：`(标签, api 地址, raw 地址)`，**按可靠性排序**。
#
# ⚠ 第一个必须是 `api.github.com`，不是 raw —— 实测踩到：`raw.githubusercontent.com`
#   有 ~5 分钟的 CDN 缓存（`cache-control: max-age=300`）。刚发的版本它还在返回旧的
#   —— 表现就是"明明发了新版，检查更新却说没有新版"，而且加时间戳参数没用
#   （CDN 层的缓存不看 query）。`contents` 接口拿到的是当前提交，实测是新的。
#   代价：匿名每小时 60 次配额。门店一天查几次，够用；raw 留作退路。
def _version_sources() -> list:
    """按可靠性排序的版本源：`[(标签, api_url, raw_url), ...]`。

    发行仓一个都没问到，才轮到源码仓 —— 源码仓私有化之后它会一路 404，
    但那时发行仓早就成功了，根本走不到这儿。
    """
    return [
        (f"api.github.com/{RELEASE_REPO}",
         f"https://api.github.com/repos/{RELEASE_REPO}/contents/VERSION?ref={RELEASE_BRANCH}",
         f"https://raw.githubusercontent.com/{RELEASE_REPO}/{RELEASE_BRANCH}/VERSION"),
        (f"api.github.com/{REPO}",
         f"https://api.github.com/repos/{REPO}/contents/src/version.py?ref={BRANCH}",
         f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/src/version.py"),
    ]


def _version_api_url(ref: str = RELEASE_BRANCH) -> str:
    """发行仓 `VERSION` 的 api 地址（版本检查的第一源）。"""
    return f"https://api.github.com/repos/{RELEASE_REPO}/contents/VERSION?ref={ref}"


#: 下载 Release 资产要用的请求头 —— 没有它，`api.github.com` 的资产地址
#: 返回的是**元数据 JSON**，而不是文件字节。
_OCTET_HEADERS = {"Accept": "application/octet-stream"}

WEB_URL = f"https://github.com/{RELEASE_REPO}"


def _release_asset_url(timeout: int = 15) -> tuple:
    """问 `api.github.com` 要最新 Release 里那个更新包的**直链**，返回 `(url, tag)`。

    拿不到返回 `("", "")` —— **绝不抛**：调用方还有好几条退路，
    这里失败不该把整次下载判死。

    ⚠ 为什么绕这一圈，不直接用 `browser_download_url`：
      后者是 `github.com/<owner>/<repo>/releases/download/...`，而**那个域名
      在门店和本机网络里时通时断**（实测 curl 20s 超时，同一时刻
      `api.github.com` 0.35s）。资产 JSON 里的 `url` 字段却是 **`api.github.com`**
      上的地址，带上 `Accept: application/octet-stream` 就直接吐文件字节，
      再 302 到 `objects.githubusercontent.com`（实测可达）—— 全程不碰 `github.com`。

    ⚠ `tries=1`：这是**最前面**的候选源，不通就赶紧换后面几条，
      别在这儿耗掉三次重试（每次都要等退避）。
    """
    try:
        r = _get(f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest",
                 timeout=timeout, tries=1)
        info = r.json()
    except Exception:                                          # noqa: BLE001
        return "", ""
    if not isinstance(info, dict):
        return "", ""
    tag = str(info.get("tag_name") or "")
    for a in (info.get("assets") or []):
        if isinstance(a, dict) and a.get("name") == RELEASE_ASSET and a.get("url"):
            return str(a["url"]), tag
    return "", tag


def _zip_urls(ref: str) -> list:
    """候选下载地址，**按可靠性排序** —— 每项是 `(url, headers)`。

    1. Release 资产的 **api 直链**（`_release_asset_url` 现查，最可靠）；
    2~3. `github.com/.../releases/download/<tag|latest>/<资产名>` ——
       不吃 API 配额，但那个域名在门店/本机网络里时通时断（见模块文档），
       所以只排第二。

    ⚠ **没有源码仓那条退路**（2026-10-02 用户定「不过渡」）：
      它给的是**明文** zipball —— 走它就等于公开渠道上还有一条明文路，
      而 `_unseal_in_place` 也不再收明文了。发行仓出问题就等下一轮重试
      （`auto_update` 每小时一次），别为了可用性把那条明文路留着。

    `ref` 参数留着是为了不改调用方签名（`repair()` 会传）。
    """
    urls = []
    asset, tag = _release_asset_url()
    if asset:
        urls.append((asset, _OCTET_HEADERS))
    if tag:
        urls.append((f"https://github.com/{RELEASE_REPO}/releases/download/"
                     f"{tag}/{RELEASE_ASSET}", None))
    urls.append((f"https://github.com/{RELEASE_REPO}/releases/latest/download/"
                 f"{RELEASE_ASSET}", None))
    return urls



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
#: ⚠ `in`（2026-09-21 晚加）：**收进来的东西**（各店发来的上报包 / 收信库）——
#:   跟 `out` 一样是"这台电脑自己的"，自更新一根手指都不许碰。
#:   它跟 `out` 是一对：`out` 是我生出来的，`in` 是别人发来的。
NEVER_TOUCH = ("config", ".secrets", "out", "in", "dist", "tools", "__pycache__", ".dsh",
               "agent.md", "docs")

# ⚠ `.dsh/` 也在名单里：它是**工作区隔离区**（记忆日志 / 备份 / 临时任务 /
#   本机 venv），跟 `.secrets/` 一样是"这台电脑自己的东西"。
#   它本来就在 `.gitignore` 里、进不了仓库，所以自更新拿到的 zip 里不会有它；
#   列在这里是**明说这件事**，顺便让 `tools/build_package.sh` 的排除项
#   跟这里对得上（那边以前漏了 `.dsh/`，把本机 venv 打进过包，被自检逮住）。
#
# ⚠ `docs/`（2026-09-29 收银那轮加）：根目录的**开发计划笔记**，同 `.dsh` 待遇
#   —— 打包 rsync 排除 + 自检反查 + 这里，三处对得上。它还没入 git，
#   但没被 ignore：哪天有人顺手 commit，没有这道镜子就会把开发笔记铺进门店。
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

# ⚠ **门店目录不下发的顶层目录**（用户 2026-09-22 方案 2）：
#   正式包 `--exclude 'tests/'`，自更新也**跳过** —— 两边一致，门店用不上 pytest。
#   仓库 git 里**保留** `tests/`（开发三头全靠它）。
#   ⚠ 不要塞进 `NEVER_TOUCH`：那是"门店自己的数据不许碰"；
#     这里是"程序安装件里不带测试"。旧机器上残留的 `tests/` 交给 prune
#     （`PRUNE_PREFIXES` 仍含 `tests`；zip 里没有 ⇒ 到点可清）。
SKIP_APPLY = ("tests",)


class UpdateError(RuntimeError):
    pass


class PartialUpdate(UpdateError):
    """**改到一半失败了**：已经落地的文件是完整的，现场在 journal 里。

    ⚠ 调用方（控制台）应当**不要重启服务** —— 当前进程还跑在"改之前"的代码上，
    它正是唯一还能干活的那一份。把错误和"修复"入口显示出来就行。

    `result` 里带着这次已经改了什么（`changed` / `added` / `backup` / `journal`）。
    """

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result or {}


# ⚠ **必须重试**。实测（走代理的网络）：TLS 握手会被间歇性掐断，
#   报 `SSLEOFError: EOF occurred in violation of protocol`——
#   手动再试一次就好了。门店电脑挂代理/防火墙也是这个形状。
#   不重试的话，检查更新会"时好时坏"，用户完全摸不着规律。
TRIES = 3
BACKOFF = 1.5          # 秒；第 n 次等 n * BACKOFF


def _proxy_broken(e) -> bool:
    """这次失败是不是「代理那条线」断的 —— 兜底只在这种情况才插手。

    ⚠ 判据不能只靠 `isinstance`：测试会用**假 requests 模块**（只有
      `RequestException` / `Session`）顶替真模块，那时候取不到 `exceptions.ProxyError`。
      所以两条腿：类名对得上就算；对不上再看报错原文里有没有 proxy 字样。
    """
    import requests
    cls = getattr(getattr(requests, "exceptions", None), "ProxyError", None)
    if cls is not None and isinstance(e, cls):
        return True
    text = str(e)
    return "ProxyError" in text or "Unable to connect to proxy" in text


def _get(url: str, *, timeout: int, stream: bool = False, tries: int = None,
         headers: Optional[dict] = None):
    """带重试的 GET。每次都用**新的 Session** —— 连接池里的坏连接别再复用。

    `headers` 是给 Release 资产直链用的（`Accept: application/octet-stream`，
    见 `_release_asset_url`）—— 不传就一个请求头都不加，行为跟从前一样。

    ⚠ **代理不通就脱掉代理直连再来一轮**（2026-09-29 用户：「加个兜底吧」）：
      代理软件重启 / 换端口那阵，环境变量还指着一个没人听的端口 ⇒ 每一发都是
      `ProxyError`，而 GitHub 直连**可能是通的** —— 检查更新不该被
      「代理抖一下」卡住（当天实测：走代理全挂、直连 200）。
      ⚠ 代理错**不占用原来那几次重试**：端口没监听是确定性的，
        等它三遍纯属浪费时间，发现就立刻换直连。
    """
    import requests
    n = TRIES if tries is None else max(1, tries)
    last = None
    for attempt in range(1, n + 1):
        try:
            with requests.Session() as s:
                r = s.get(url, timeout=timeout, stream=stream, headers=headers)
                r.raise_for_status()
                return r
        except requests.RequestException as e:
            if _proxy_broken(e):
                return _get_direct(url, timeout=timeout, stream=stream,
                                   tries=n, proxy_err=e, headers=headers)
            last = e
            if attempt < n:
                time.sleep(BACKOFF * attempt)
    raise UpdateError(f"连不上 GitHub（试了 {n} 次）：{last}")


def _get_direct(url: str, *, timeout: int, stream: bool = False, tries: int = 1,
                proxy_err=None, headers: Optional[dict] = None):
    """上一条的兜底：**这一次请求不走代理**，别的环境行为照旧。

    ⚠ 只把 `proxies` 三个键置 `None`（`httputil.NO_PROXY`），
      **不动 `trust_env`** —— 自定义 CA、netrc 这些还得照常生效。
    """
    import requests
    from . import httputil
    n = max(1, int(tries))
    last = None
    for attempt in range(1, n + 1):
        try:
            with requests.Session() as s:
                r = s.get(url, timeout=timeout, stream=stream,
                          proxies=httputil.NO_PROXY, headers=headers)
                r.raise_for_status()
                return r
        except requests.RequestException as e:
            last = e
            if attempt < n:
                time.sleep(BACKOFF * attempt)
    raise UpdateError("代理不通、直连也不通 —— 代理：%s；直连：%s"
                      % (proxy_err, last))


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


def has_update(latest: str, current: str, *, beta: Optional[bool] = None) -> bool:
    """远端算不算"有更新" —— **含「beta 包换成同号正式版」这一条**。

    ⚠ **为什么需要这一条**（用户 2026-09-18 提的，「beta可以升级到正式版」）：
    beta 包和正式包的**版本号是同号的**（beta 只是"这一版正在测"的标记），
    所以装过 `2.1.0-beta3` 的机器 `VERSION` **也是 2.1.0** ——
    正式版推上去之后 `2.1.0 > 2.1.0` 不成立 ⇒
    **那台机器永远看不到「有新版本」，卡在 beta 上再也升不上来**。

    （2.0.1 那次的"解法"是**顺手把版本号升一位**绕过去 —— 见
    `src/version.py` 注释 ④。那是权宜：每发一次正式版都得多升一个号，
    而且**正式版一旦跟 beta 同号就没救了**，2026-09-18 这次就是。）

    现在的规则：

        有更新 = 远端 > 本地                    ← 原有
              或 远端 == 本地 且 本地是 beta 包   ← 新增

    **为什么"同号也算"可以放宽**：GitHub 上那条 `main`（以及发行仓的最新 Release）
    **就是正式线**，从那儿下下来的必然是正式包；而且 `apply_update` 装完会把
    `BUILD.txt` 重写成 `Release · v2.1.1`（不含 beta）⇒ 判据回到"远端 > 本地"，
    **不会反复提示**。

    ⚠ **不能写成 `>=`**：本地在测**更高版本**的 beta（比如 2.2.0）时，
    不能反过来提示"降级到 2.1.1"。

    `beta=None` 时读本机的 `BUILD.txt`（`version.is_beta()`）。
    """
    if is_newer(latest, current):
        return True
    if beta is None:
        from . import version                      # 延迟 import：本模块要能被单独 import
        beta = version.is_beta()
    return bool(beta) and parse_version(latest) == parse_version(current)


def _version_in_text(text: str) -> str:
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text or "", re.M)
    if not m:
        raise UpdateError("那边那个 version.py 里找不到 VERSION")
    return m.group(1)


def remote_version(timeout: int = 15) -> str:
    """去 GitHub 读最新版本号 —— **先问发行仓，再问源码仓**（顺序见 `_version_sources`）。

    ⚠ **每个仓库内部都是 api 优先，不是 raw**。实测踩到：`raw.githubusercontent.com`
    有大约 5 分钟的 CDN 缓存（`cache-control: max-age=300`），刚发的新版本它还在
    返回旧的 —— 表现就是"明明发了新版，检查更新却说没有新版"。
    加时间戳参数没用（那是 CDN 层的缓存，不看 query）。

    `api.github.com/.../contents` 没有这个问题，代价是匿名每小时 60 次配额 ——
    门店一天查几次，完全够。raw 留作退路（不吃配额，但会慢一个 CDN 周期）。

    ⚠ 源码仓私有化之后，它那两个源都会 404 —— 那是**预期之内**的：
      发行仓排在前面，正常情况下根本轮不到它。只有发行仓整体挂掉时才会走到，
      那时报错里会多一条，无伤大雅（总比把门店卡死强）。
    """
    errs = []
    for label, api_url, raw_url in _version_sources():
        for tag, fetch in (_api_version_source(api_url, timeout),
                           _raw_version_source(raw_url, timeout)):
            try:
                return fetch()
            except UpdateError as e:
                errs.append(f"{tag}/{label}: {str(e)[:70]}")
    raise UpdateError("拿不到最新版本号 —— " + "；".join(errs))


def _api_version_source(api_url: str, timeout: int):
    """(标签, 取值函数) —— api.github.com，**没有 CDN 缓存**。"""
    def fetch() -> str:
        # tries=1：这个源不通就赶紧换 raw，别在这儿耗掉三次重试
        r = _get(api_url, timeout=timeout, tries=1)
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


def _raw_version_source(raw_url: str, timeout: int):
    """(标签, 取值函数) —— raw，不吃配额，但有 ~5 分钟 CDN 缓存。"""
    def fetch() -> str:
        r = _get(raw_url, timeout=timeout)
        return _version_in_text(r.text)
    return "raw.githubusercontent.com", fetch


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
    info = {"at": now, "current": current, "repo": RELEASE_REPO, "web": WEB_URL}
    try:
        latest = remote_version(timeout=timeout)
        info.update({"latest": latest, "has_update": has_update(latest, current),
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

    ⚠ **两条路必须用同一个判据**：`check()` 是后台每天查一次的，
    这里是**界面每次刷新读的**（`cached()`）。只改一条的话，
    后台说"有更新"而界面照样显示"已是最新" —— 而那正是门店唯一看得到的地方。
    """
    out = dict(cached)
    if out.get("latest"):
        out["current"] = current
        out["has_update"] = has_update(out["latest"], current)
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
    """(zip 里的文件, 安装目录里的相对路径) —— 照原样铺，除了 NEVER_TOUCH / SKIP_APPLY
    （lifehall 版再加一道 `edition.PRUNE`，见函数尾）。"""
    out = []
    for f in _iter_files(zip_root):
        if not f.is_file():
            continue
        rel = f.relative_to(zip_root)
        rels = _rel_key(rel)          # ⚠ 别用 str() —— Windows 上分隔符是反斜杠
        if rels not in ALLOW_EVEN_IF_NEVER and (
                rel.parts[0] in NEVER_TOUCH or rel.parts[0] in SKIP_APPLY
                or "__pycache__" in rel.parts):
            continue
        if rel.name.startswith(".") and rel.name not in (".gitattributes", ".gitignore"):
            continue
        out.append((f, rel))
    if _edition.is_lifehall():
        # 生活馆机器只铺保留的代码 —— 生活馆分支的 zip 是**全量仓库**，
        # 里面仍有被裁文件；不滤掉的话，第一次自更新就把云商/对账代码
        # 从 zip 里铺回来了（等于裁剪白做）。清单是 `edition.PRUNE` 单源，
        # 打包脚本 `tools/build_package.sh` 的 rsync 排除读同一份，谁也不许另抄一张。
        out = [(f, rel) for f, rel in out if not _edition.pruned(_rel_key(rel))]
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


def _unseal_in_place(path: Path, root=None) -> str:
    """把**密文**包解成 zip（就地），返回一句说明。

    ⚠ **明文直接报错**（2026-10-02 用户定「不过渡，当做没有门店用过」）——
      fail closed：收到明文只可能是"下错了东西"（错误页、镜像站、被换过的
      资产），而按明文铺下去等于**把源码从公开渠道装进门店**，
      正是路线 A 要消灭的事。宁可这次更新失败，也不接受它。

    ⚠ 解不开**抛 `UpdateError`** —— 调用方的 `except` 会把这句话记进报错，
      然后接着试下一个源；全挂了就把话原样端给门店。
      `mailcrypto.unseal` 缺钥匙时那句已经能照做（"把 release.key 放进
      .secrets"），钥匙**不对**时它只会说"校验不过"，所以这里补一句。

    ⚠ 用 `rel=RELEASE_KEY_REL` —— 邮件附件那把钥匙解不开更新包，
      而解不开**不会报"钥匙用错了"**，只报"校验不过"，非常难查。
    """
    from . import mailcrypto
    try:
        data = path.read_bytes()
    except OSError as e:
        raise UpdateError(f"读不下刚下的包：{e}") from e
    if not mailcrypto.is_sealed(data):
        raise UpdateError(
            "下下来的东西**不是密文包** —— 路线 A 之后公开仓上只发加密的更新包，"
            "收到明文说明下错了（代理/镜像站换过内容？）。"
            f"开头是：{data[:16]!r}")
    plain, how = mailcrypto.unseal(data, root=root, rel=mailcrypto.RELEASE_KEY_REL)
    if how.get("state") != "opened":
        why = str(how.get("why") or "未知原因")
        # ⚠ **"校验不过"是最难查的一种**（2026-10-02 测试当场逮到）：
        #   本机有 release.key、但**不是打包机那把**时，`mailcrypto` 只会说
        #   「密钥不对，或者内容被改过」—— 门店看到这句会以为包坏了，
        #   于是去重装、去重下，白折腾好几轮，而真正该做的是换一把钥匙。
        #   `unseal` 只在"**根本没有钥匙**"那种情况下提到 release.key，
        #   所以这里补一句：已经提到的就不再重复。
        if "release.key" not in why:
            why += ("。这台机器的 release.key 跟打包机的**不是同一把** —— "
                    "把正确的 release.key 放进 .secrets\\ 再试一次，"
                    "或者拿一次完整安装包（它带这把钥匙）")
        raise UpdateError("更新包是加密的，解不开 —— %s" % why)
    path.write_bytes(plain)
    return f"已解密（密钥 {how.get('key_id')}）"


def download(timeout: int = 120, ref: str = BRANCH, root=None) -> Path:
    """把**发行包（密文）**下到临时目录，返回解压出来的根目录。

    候选地址按可靠性排序（见 `_zip_urls`）：Release 资产的 api 直链 →
    两条 `github.com` 直链。**只从发行仓拿，没有源码仓那条明文退路**
    （2026-10-02 用户定「不过渡」，理由见 `_zip_urls`）。

    下来的东西**必须是密文**（`_unseal_in_place`），解开再解压；
    `root` 指到安装目录（密钥在 `<root>/.secrets/release.key`），
    不传就用本机默认那套。

    `ref` 参数留着不改调用方签名（`repair()` 会传），实际不参与选地址。
    """
    import requests
    tmp = Path(tempfile.mkdtemp(prefix="cbg-update-"))
    blob = tmp / "src.zip"
    errors = []
    for url, headers in _zip_urls(ref):
        host = url.split("/")[2]
        try:
            r = _get(url, timeout=timeout, stream=True, headers=headers)
            with open(blob, "wb") as fp:
                for chunk in r.iter_content(64 * 1024):
                    fp.write(chunk)
            opened = _unseal_in_place(blob, root=root)
            if opened:
                _log(f"[更新] {host}: {opened}")
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


# ------------------------------------------------------------------ 升级现场：journal + 快照
#
# ⚠ **为什么要有这个**：升级是**跨进程**的 —— 中途断电 / 被杀 / 磁盘满，
#   安装目录里就剩下"一半新、一半旧"，而**没有任何人知道**。
#   下一次启动时 `upgrade.record()` 只比版本号（`upgrade.py:109`），
#   于是程序照常在混合版本上跑起来 —— 这是最难查的一种状态。
#
# 所以：**动第一个文件之前**，先把"我打算干什么"写下来（journal），
# 并把**即将被覆盖的文件**复制一份（快照）。两样都放 `.secrets/update/`：
#
#   * `.secrets/` 在 `NEVER_TOUCH` 里，升级自己不会碰它（有测试盯着）；
#   * 和 `schedule.json` / `python.txt` / `update-check.json` 同一类 —— "我们自己的记录"。
#
# ⚠ 这两样只是**现场**，不是备份策略：真正的恢复是 `restore_backup`（铺备份 zip）。
#   快照的用途只有一个 ——
#   **铺到一半失败时能立刻退回去**。
UPDATE_DIR = ".secrets/update"


def update_dir(root) -> Path:
    return Path(root) / UPDATE_DIR


def journal_path(root) -> Path:
    return update_dir(root) / "journal.json"


def read_journal(root) -> dict:
    """读升级现场。**绝不抛** —— 自检、界面、启动路径都要读它。"""
    try:
        data = json.loads(journal_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: 走到这些状态就算"收尾了"（`pending()` 不再报它）
TERMINAL_STATES = ("done", "restored")


def pending(root) -> dict:
    """有没有**没走完**的升级（`state` 不是 `done` / `restored`）。没有就返回 `{}`。

    自检 / 控制台横幅用它 —— 光写 journal 没人看等于没写。
    """
    j = read_journal(root)
    if not j.get("state") or j.get("state") in TERMINAL_STATES:
        return {}
    return j


def integrity_lines(root) -> list:
    """「代码完整性」那几行 —— `selftest` 和控制台都显示它。

    没走完的升级要在**人看得见的地方**说出来：不然程序会静默跑在
    "一半新一半旧"的代码上（这正是加 journal 要解决的事）。
    """
    j = pending(root)
    if not j:
        return []
    done = j.get("done") or []
    lines = [f"✗ 上次升级没走完（{j.get('state')}）：{j.get('error') or '中断了'}",
             f"  目标是 v{j.get('to') or '?'}，已经改了 {len(done)} 个文件"]
    if j.get("backup"):
        lines.append(f"  备份：{j['backup']}")
    lines.append("  修复：python -m src.cli update --repair（重跑一遍）"
                 " / --restore（从备份退回去）")
    return lines


def smoke_test(root, timeout: int = 90) -> tuple:
    """铺完代码**冒烟**一遍：起一个干净进程，真的 import 一次入口。

    返回 `(ok, 说明)`。⚠ **绝不抛异常** —— 抛出去会把"已经铺好的代码"当成失败。

    ⚠ 为什么不在当前进程里 `importlib.reload`：我们正跑在**刚被替换掉**的那些
    文件上，而且"语法错 / 缺依赖 / 新模块没铺到"这几类问题只有在**干净进程**里
    才测得准。命令行也故意走 `src.cli` 这个真入口（`bootstrap.py` 最后就是调它）。

    ⚠ 它是"无人值守自动更新"能不能成立的**分界线**：过了它才敢把新代码留着，
    不过就整个退回升级前（见 `apply_update` 里那段）。
    """
    root = Path(root)
    py = sys.executable or "python"
    code = ("import src.cli as c\n"          # 语法错 / 缺依赖 / 新模块没铺到，都炸在这一行
            "c.main(['--help'])\n")
    try:
        r = subprocess.run([py, "-c", code], cwd=str(root), timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"冒烟进程起不来：{e}"
    out = (r.stdout or b"").decode("utf-8", "replace").strip()
    if r.returncode != 0:
        return False, f"退出码 {r.returncode}：{out[-400:] or '(没有任何输出)'}"
    return True, out[-200:]


def restore_backup(root, backup="") -> dict:
    """把备份里的文件搬回去 —— **不需要网络**。

    `backup` 不传就用 journal 里记的那一份。**只恢复代码**：
    `out/` 里的数据、`.secrets/`、门店配置一律不碰（代码恢复 ≠ 数据恢复）。

    返回 `{ok, restored, removed, backup, message}`。
    """
    root = Path(root)
    j = read_journal(root)
    backup = backup or j.get("backup") or ""
    if not backup or not Path(backup).is_dir():
        return {"ok": False, "restored": [], "removed": [], "backup": backup,
                "message": "找不到备份目录 —— 没法恢复（可以手工把安装目录换回旧版）"}

    restored = []
    for src in sorted(Path(backup).rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(Path(backup))
        try:
            _write_file(src, root / rel)          # 同样走**原子写**
            restored.append(_rel_key(rel))
        except OSError as e:
            return {"ok": False, "restored": restored, "removed": [], "backup": backup,
                    "message": f"恢复到 {rel} 时失败：{e}"}

    # 这次升级**新加**的文件：备份里没有它们，要挪走才算真的回到升级前。
    # ⚠ 用 `plan["add"]`（**当初打算加的**）而不是 `done`（已经写成功的）：
    #   `done` 只在收尾时落盘，中途被打断时它是空的 —— 那时用 `done` 就
    #   一个都清不掉，恢复出来是个"多了几个新文件"的伪升级前。
    #   不存在的直接跳过，所以"打算加但其实没写"的那些天然是安全的。
    added = set(j.get("plan", {}).get("add") or [])
    removed = []
    for rel in sorted(added):
        p = root / rel
        if not p.is_file():
            continue
        try:
            dst = Path(backup) / "_新增" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dst))
            removed.append(rel)
        except OSError as e:
            return {"ok": False, "restored": restored, "removed": removed, "backup": backup,
                    "message": f"清不掉这次新加的文件 {rel}：{e}"}

    _write_journal(root, dict(j, state="restored",
                              restored_at=time.strftime("%Y-%m-%d %H:%M:%S")),
                   required=False)
    return {"ok": True, "restored": restored, "removed": removed, "backup": backup,
            "message": f"已恢复 {len(restored)} 个文件"
                       + (f"，清掉 {len(removed)} 个这次新加的" if removed else "")}


def repair(root, *, current: str = "", mode: str = "auto") -> dict:
    """把**没走完的升级**收尾。

    * `mode="auto"`（默认）：解压目录还在就**重跑**（不用联网），不在就重新下载再跑；
    * `mode="restore"`：**从备份退回去**（不管网络，最可靠的那条路）。

    没有没走完的升级就返回 `{ok: False, message: …}` —— 不抛异常（它会从 HTTP 和
    命令行两条路调进来）。
    """
    j = pending(root)
    if not j:
        return {"ok": False, "message": "没有没走完的升级，不用修"}
    if mode == "restore":
        return restore_backup(root)
    staging = j.get("staging") or ""
    if staging and Path(staging).is_dir():
        _log(f"[更新] 用上次留下的解压目录重跑：{staging}")
        return apply_update(root, current=current, ref=j.get("ref") or BRANCH,
                            staging=staging)
    _log("[更新] 上次的解压目录已经不在了（临时目录被清过）—— 重新下载再跑")
    return apply_update(root, current=current, ref=j.get("ref") or BRANCH)


def _write_journal(root, data: dict, *, required: bool = True) -> bool:
    """写现场。**先写临时文件再 replace** —— 半个 journal 比没有更糟。

    `required=True` 用在"动第一个文件之前"那一次：写不下来就**别升级**
    （没有现场的升级，正是我们要消灭的东西）。
    """
    path = journal_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True
    except OSError as e:
        if required:
            raise UpdateError(f"写不了升级现场（{path}）：{e}") from e
        _log(f"⚠ 升级现场没写成（不影响本次升级）：{e}")
        return False


def make_backup(root) -> Path:
    """开一个备份目录（名字带时间戳，方便人认）。"""
    base = update_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(base),
                                prefix=time.strftime("backup-%Y%m%d-%H%M%S-")))


def _snapshot(root, rels, dest) -> int:
    """把即将被覆盖的文件复制到 `dest`，返回份数。

    ⚠ **一个失败就整体放弃**（调用方会让整个升级中止）——
    宁可不升，也不要"升到一半又没有退路"。
    """
    n = 0
    for rel in rels:
        src = Path(root) / rel
        if not src.is_file():
            continue                     # 这次是新加的文件，没什么可备份的
        dst = Path(dest) / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        n += 1
    return n


def prune_backups(root, keep: int = 2) -> list:
    """只留最近 `keep` 份现场（按名字里的时间戳排），返回删掉的目录名。"""
    base = update_dir(root)
    try:
        dirs = sorted((p for p in base.iterdir()
                       if p.is_dir() and p.name.startswith("backup-")),
                      key=lambda p: p.name)
    except OSError:
        return []
    gone = []
    for p in (dirs[:-keep] if keep > 0 else dirs):
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    return gone


def _replace_retry(tmp: Path, dst: Path, tries: int = 3) -> None:
    """`os.replace`，被占住时重试几次。

    ⚠ 为什么要重试：Windows 上杀软 / 搜索索引器会**瞬时**占住刚写出来的文件，
    报 `PermissionError`。门店那台机器上这很常见，而它跟"真的没权限"长得一样 ——
    不重试就会把一次偶然的占用报成"升级失败"。
    """
    for i in range(tries):
        try:
            os.replace(str(tmp), str(dst))
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5 * (i + 1))


def _write_file(src: Path, dst: Path) -> None:
    """把一个文件从 zip 铺到安装目录 —— **原子**的。

    ⚠⚠ 不能直接 `copyfile(src, dst)`：它是"**先截断再写**"，
    中途断电/被杀就留下**半个 `.py`**（语法错误）—— 门店再也起不来，
    而且这种损坏"看起来像程序坏了"，根本查不到升级头上。

    改成"临时文件 + `os.replace`"：同一个盘上的 replace 在 Windows 上是
    `MoveFileEx(MOVEFILE_REPLACE_EXISTING)`，**原子** ——
    任何时刻中断，这个文件要么是旧的、要么是新的，不会是半截。

    ⚠ 临时名里带 `.new-<pid>`：占位很显眼，真出事时人一眼能看出是升级留下的；
    删除那一步（1.4c）的闸门也把它排除在"我们的文件"之外。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".new-" + str(os.getpid()))
    try:
        shutil.copyfile(src, tmp)
        _replace_retry(tmp, dst)
    finally:
        # 成功时 tmp 已经不存在；失败时别把垃圾留在门店目录里
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


# ------------------------------------------------------------------ 清理旧文件（阶段 1.4c）
#
# 背景：`apply_update` 以前只记 `changed` / `added` —— **搬走的旧模块会永远留在门店**
# （`src/pools.py` 搬到 `src/storage/pools.py` 之后，旧文件还在，还可能被 import 到）。
#
# 删除判据 = `本机文件 − have`（`have` = 刚下下来的那一版拥有的文件全集），
# 再过下面**七道闸**。⚠ 不需要任何"本地清单"文件 —— 所以**老门店第一次升级就能清干净**，
# 不需要先发一版"过渡版"。
#
# ⚠⚠ 两道额外的保险，缺一不可：
#   1. **删之前再问一次文件系统**（`is_file`）。门店**真出过** `os.walk` 漏掉
#      `src/cli.py`（见 `_iter_files` 的注释）—— 那次后果只是"更新被拒"，
#      有了删除之后就变成"删掉 cli.py"，量级完全不同。
#   2. **比例闸**：一次正常重构不会让两成文件消失。候选项一多就**整体不删**，
#      只报告 —— 那种情况多半是"下到的包不对/解压不全"，而不是真删了。
#
# 只有这四个前缀允许删 —— 它们是**两条安装路径里逐字节一致**的部分（见设计文档 0.2）。
# 根目录一律不碰：`run*.bat` 是本机生成的、`BUILD.txt` 是装完写的、根目录文档
# （`AGENTS.md`、`发布说明.md` —— 后者 2026-09-23 起也进了仓库）不参与差集。
PRUNE_PREFIXES = ("src", "web", "tests", "config")

#: 认得出"这是我们的代码文件"的扩展名 —— **白名单**，不是黑名单。
PRUNE_EXTS = (".py", ".js", ".css", ".html", ".yaml", ".yml", ".json", ".md", ".txt")

#: 一看就不是我们铺的东西（手工备份 / 编辑器残留 / 我们自己的临时文件）
PRUNE_JUNK = (".bak", ".orig", "~", ".tmp", ".new-", ".swp", ".old")

#: 比例闸：超过 `max(5, 本机文件的 20%)` 就整体不删
PRUNE_MAX_MIN = 5
PRUNE_MAX_RATIO = 0.2

#: ⚠⚠ **先审计一版**（用户 2026-09-19 定，D2）：只把候选算出来、写进升级结果和日志，
#:    **不动手删**。这样能先看一版门店的真实候选，代价最低。
#:    打开真删（改成 True）时**必须**同时把"从备份恢复"和界面上的修复入口做完
#:    （1.4d）—— 删错了没有退路是不行的。
PRUNE_ENABLED = False


def _local_code_files(root) -> list:
    """本机在四个前缀下现有的代码文件（相对路径，正斜杠）。"""
    root = Path(root)
    out = []
    for prefix in PRUNE_PREFIXES:
        base = root / prefix
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                rel = Path(dirpath, name).relative_to(root)
                out.append(_rel_key(rel))
    return sorted(out)


def prune_reason(root, zip_root, rel) -> str:
    """`""` = 可以删；否则是**不能删的原因**（原样写进日志给人看）。"""
    rels = _rel_key(rel)
    parts = Path(rels).parts
    name = parts[-1]
    if not parts or parts[0] not in PRUNE_PREFIXES:
        return "不在代码前缀里（根目录一律不删）"
    if "__pycache__" in parts:
        return "编译缓存"
    if parts[0] in NEVER_TOUCH and rels not in ALLOW_EVEN_IF_NEVER:
        return "在 NEVER_TOUCH 里"
    if rels in ALLOW_EVEN_IF_NEVER:
        return "随程序走的门店映射表，删了所有店一起瞎"
    if rels in {a for a, _ in ANCHORS}:
        return "更新器锚点"
    if not name.lower().endswith(PRUNE_EXTS):
        return "看着不是我们铺的文件"
    if any(j in name for j in PRUNE_JUNK):
        return "像是手工备份/临时文件"
    if (Path(zip_root) / rel).is_file():
        # ⭐ 遍历漏了，但文件在 —— **以文件系统为准**，绝不删
        return "新版里其实还有它（遍历漏了，按存在处理）"
    return ""


def prune_candidates(root, zip_root, have) -> dict:
    """算出"新版已经没有、本机还留着"的旧文件。

    返回 `{"files": [...], "skipped": {rel: 原因}, "total": 本机文件数, "blocked": bool}`。
    **只看不算改** —— 真正搬走是 `_prune()` 的事。

    ⚠ 候选一律从**本机文件**出发（`_local_code_files`）—— "zip 里有、本地没有"
      的文件压根不进循环，报不出候选（Task 8 核对过的生活馆情形：lifehall 的
      zip 里带着 `edition.PRUNE` 那些被裁文件、而 `_targets()` 已把它们滤出
      `have`，本机又没有 ⇒ 双向都碰不到，不会误报）。反过来本机真残留被裁
      文件时，`prune_reason` 还会先问一次文件系统（zip 里还有它 ⇒ "按存在
      处理"、不删）—— 两道闸都指向"宁可不删"。行为钉在
      `tests/test_edition_update.py::Test_prune候选只看本机`。
    """
    files, skipped = [], {}
    for rel in _local_code_files(root):
        if _rel_key(rel) in have:
            continue                                  # 新版还有它，不是残留
        why = prune_reason(root, zip_root, rel)
        if why:
            skipped[rel] = why
        else:
            files.append(rel)
    total = len(_local_code_files(root))
    blocked = len(files) > PRUNE_MAX_MIN and len(files) > total * PRUNE_MAX_RATIO
    return {"files": sorted(files), "skipped": skipped, "total": total, "blocked": blocked}


def _prune(root, rels, backup) -> list:
    """把残留**搬进备份目录**（**永不真删**）。没有备份目录就一个都不动。"""
    if not backup:
        _log("⚠ 没有备份目录 —— 本次不清理（宁可不删，也不能删了找不回）")
        return []
    gone = []
    for rel in rels:
        src = Path(root) / rel
        if not src.is_file():
            continue
        dst = Path(backup) / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            gone.append(rel)
        except OSError as e:
            _log(f"⚠ 清不掉 {rel}（不影响本次升级）：{e}")
    return gone


def apply_update(root, *, current: str = "", ref: str = BRANCH, staging=None,
                 smoke: bool = True) -> dict:
    """把代码换成 `ref` 那一版。**数据目录一根手指都不碰。**

    `ref` 默认 main（升级到最新）。
    `staging` 是"上次留下的解压目录"（`repair()` 用它重跑，省一次下载）。
    `smoke=False` 关掉铺完之后的冒烟自检（**只有测试用** —— 假的安装目录 import 不起来）。

    ⚠ **动第一个文件之前**必须先做完两件事：把即将被覆盖的文件**快照**一份、
    把"我打算干什么"写进 **journal**。中途失败抛 `PartialUpdate`，
    现场留在 `.secrets/update/journal.json`（详见下面 `UPDATE_DIR` 那段注释）。
    """
    root = Path(root)
    if not root.is_dir():
        raise UpdateError(f"安装目录不存在：{root}")

    zip_root = (Path(staging) if staging and Path(staging).is_dir()
                else download(ref=ref, root=root))
    # ⚠ 这三个得在 try **外面**初始化：`except` 里要拿它们拼返回值
    started = False
    finished = False
    backup = ""
    saved = 0
    plan = {"update": [], "add": [], "remove": []}
    base = {}
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

        # 第一遍：只算差集（读旧、读新、比一比），**先不写盘** ——
        # 因为"要写哪些、要备份哪些"必须先知道，才能写 journal。
        todo, changed, added = [], [], []
        for src, rel in pairs:
            dst = root / rel
            old = None
            try:
                old = dst.read_bytes()
            except OSError:
                pass
            if old == src.read_bytes():
                continue
            todo.append((src, rel))
            (changed if old is not None else added).append(_rel_key(rel))

        # ⚠ **入口文件最后落地**：新代码可能 import 新模块，而入口最后写 ⇒
        #   任何时刻中断，旧入口配着"新模块已经在"的目录都还能跑
        #   （旧入口不认识新模块，但它也不会去 import 它们）。
        anchors = {a for a, _ in ANCHORS}
        todo.sort(key=lambda p: (_rel_key(p[1]) in anchors, _rel_key(p[1])))

        # 残留候选：新版没有、本机还留着的旧文件（只算，不动手）
        cand = prune_candidates(root, zip_root, have)
        plan = {"update": sorted(set(changed)), "add": sorted(set(added)),
                "remove": cand["files"], "remove_blocked": cand["blocked"]}
        base = {"ref": ref, "from": current, "to": remote,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "plan": plan}

        # ★ 现场：先快照、再写 journal，**然后才动第一个文件**
        #   （快照失败 = 磁盘满/权限不对 ⇒ 一个文件都没动，抛出去就好）
        # ⚠ 要把"即将删掉的"也快照进去 —— 它们才是最需要退路的。
        # ⚠ `BUILD.txt` 也得进快照：它是**这份代码的版本指纹**，
        #   退回去却留着新版本号的话，界面会显示一个它其实没在跑的版本。
        if plan["update"] or plan["remove"]:
            backup = str(make_backup(root))
            saved = _snapshot(root, sorted(set(plan["update"]) | set(plan["remove"])
                                           | {"BUILD.txt"}), backup)
        _write_journal(root, dict(base, state="planned", backup=backup,
                                  saved=saved, staging=str(zip_root)))

        # 第二遍：真的铺（重新从 zip 读 —— 比把几 MB 内容全揣在内存里干净）
        started = True
        _write_journal(root, dict(base, state="applying", backup=backup,
                                  saved=saved, staging=str(zip_root)), required=False)
        done = []
        try:
            for src, rel in todo:
                _write_file(src, root / rel)
                done.append(_rel_key(rel))
        except OSError as e:
            # ⚠ 不"跳过它继续" —— 那会造出更乱的混合状态
            detail = f"铺到一半失败了（{len(done)}/{len(todo)} 个文件）：{e}"
            _write_journal(root, dict(base, state="failed", backup=backup, saved=saved,
                                      staging=str(zip_root), error=detail, done=done),
                           required=False)
            raise PartialUpdate(detail, {
                "from": current, "to": remote, "changed": done, "added": [],
                "count": len(done), "backup": backup, "saved": saved,
                "journal": str(journal_path(root))}) from e

        # ★ 冒烟：**新代码真的 import 得起来吗**（语法错 / 缺依赖都炸在这一关）。
        #   ⚠ 必须在写 `BUILD.txt` **之前** —— 冒烟不过就整个退回去，
        #     那时 BUILD.txt 要是已经写成新版本号，界面就会显示一个它其实没在跑的版本。
        #   ⚠ 也必须在清理旧文件**之前** —— 新代码还没被证明能用，就先别动旧文件。
        if smoke and todo:
            ok, why = smoke_test(root)
            if not ok:
                back = restore_backup(root)
                msg = f"新代码自检没过，已自动退回升级前的代码：{why}"
                if not back.get("ok"):
                    msg += f"　⚠ 退回也没成功：{back.get('message')}"
                _log("[更新] " + msg)
                raise PartialUpdate(msg, {
                    "from": current, "to": remote, "changed": [], "added": [],
                    "count": 0, "backup": backup, "saved": saved,
                    "rolled_back": bool(back.get("ok")),
                    "journal": str(journal_path(root))})

        # 打完补丁写个指纹 —— 从发行仓更新过来的没有打包时间戳
        if remote:
            try:
                (root / "BUILD.txt").write_text(
                    f"Release · v{remote}", encoding="utf-8")
            except OSError:
                pass

        # ---- 清理旧文件（**最后一步**：中断在这里只会"多几个旧文件"，程序照跑）
        removed = []
        if plan["remove"]:
            if PRUNE_ENABLED and not cand["blocked"]:
                removed = _prune(root, plan["remove"], backup)
            else:
                why = "比例闸拦住了" if cand["blocked"] else "本版先只报告（`PRUNE_ENABLED=False`）"
                _log(f"[更新] 有 {len(plan['remove'])} 个旧文件该清（{why}）："
                     + "、".join(plan["remove"][:8])
                     + ("…" if len(plan["remove"]) > 8 else ""))

        _write_journal(root, dict(base, state="done", backup=backup, saved=saved,
                                  staging=str(zip_root), done=done, removed=removed),
                       required=False)
        prune_backups(root, keep=2)
        finished = True
        return {"ok": True, "from": current, "to": remote,
                "changed": sorted(set(changed)), "added": sorted(set(added)),
                "count": len(set(changed) | set(added)),
                "backup": backup, "saved": saved,
                # ⚠ `removed` = 真搬走的；`removed_candidates` = 算出来的（审计版只报告不删，
                #   所以这个版本里前者恒为空、后者可能有值）
                "removed": removed, "removed_candidates": plan["remove"],
                "prune_blocked": cand["blocked"], "prune_enabled": PRUNE_ENABLED,
                "journal": str(journal_path(root))}
    finally:
        # ⚠ 失败了就**保留**解压目录：重跑要用它，现场也要留给人看
        #   （系统临时目录重启后可能被清 —— 所以 journal 里记了路径，
        #     修复时先校验它还在不在）。
        if started and not finished:
            _log(f"⚠ 升级没走完 —— 解压目录保留在 {zip_root}（重跑要用，别删）")
        else:
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
