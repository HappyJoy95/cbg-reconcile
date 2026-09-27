"""本地 Web 界面（Python 标准库，**零新增依赖**）。

    python -m src.cli serve                 # 127.0.0.1:8787
    python -m src.cli serve --port 9000 --open

为什么不用 Flask/FastAPI：部署目标是门店电脑，能少装一个包就少一个装不上的理由。
为什么只监听回环地址：这是本地管理工具，能改配置、能触发对账，不该暴露到局域网。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .app import data_state as app_data
from .features.store import staff as app_staff
from .modules import auth, health, theme, timer
from .storage import runlog
from . import (autostart, browser, config_io, elevate, erp, mailer, pools_history,
               pools_notify,
               run_daily, schedule,
               selfupdate, service, startup, upgrade, version, wecom, whatsnew)
from .cbg import CbgClient, CbgError
from .erp import (DEFAULT_ENV_FILE, STORE_ENV_FILE, ErpCaptchaRequired, ErpClient,
                  ErpError, describe_credentials, describe_store_credentials,
                  load_credentials, load_store_credentials, save_credentials,
                  save_store_credentials)
from .paths import ROOT
from .report import delete_report, list_reports, load_report
from . import runner
from .runner import manager
from .session import CbgAuthError, CbgSession

WEB_DIR = ROOT / "web"

#: 「**刷新**」按钮点下去**先抓哪几步新数据**（用户 2026-09-20：
#: 「周度重点的刷新按钮，还有 pos 合规和报量查询的刷新按钮**需要单独调用一次抓取新数据**」）。
#:
#: ⚠ 三个页面**各要各的**，一个都不能少、也一个都不能多：
#:   * 达成读的是**云商销售明细**（`erp_sales`）⇒ 抓云商 + 算达成；
#:   * POS 读的是**玲珑侧**的单据（orders/payments/returns）⇒ 抓玲珑 + 算 POS；
#:   * 双平台对比**两边都要** ⇒ 两份都抓 + 算对比。
#: ⚠ 顺序由 `daily --steps` 里的 `Step.order` 兜底（抓在前、算在后）——
#:   这里写的顺序只是给人看的。
REFRESH_STEPS = {
    "attain": ("erp-dump", "attain"),
    # 月度生意计划读的**也是**云商销售明细（`erp_sales`）⇒ 跟达成同一条链。
    "monthly": ("erp-dump", "plan"),
    "pos": ("dump", "pos"),
    "pools": ("dump", "erp-dump", "pools"),
    # ⚠ 防护膜：**只手动**（`whens=()`），点「刷新」= 先抓云商销售导出再落快照。
    "film": ("erp-dump", "film"),
    # 无忧会员权益：同防护膜 —— 读 erp_sales，点刷新先抓再算。
    "benefit": ("erp-dump", "benefit"),
    # 权益领取 · 待领：**没有自己的 Step**（状态/匹配读接口现算），
    # 刷新只保证 erp_sales 是新的 —— 所以只点名 `erp-dump`。
    "claim-pending": ("erp-dump",),
    # 串号追踪：读 erp_stock + erp_sales —— 刷新同样先抓云商。
    "sn-trace": ("erp-dump",),
}

#: 手动「刷新」时，这两步**半小时内成功抓过就跳过**（2026-09-22 用户）。
#: ⚠ **只挡手动刷新**（`/api/refresh`）—— 定时器那趟**不过这道闸**。
#: ⚠ 键是 `daily --steps` 里的步骤名；值是 `fetch_attempt.kind`（池名）。
FETCH_COOLDOWN_MIN = 30
FETCH_COOLDOWN = {
    "dump": ("dump", "lg-stock"),            # 玲珑 / 华为
    "erp-dump": ("erp-stock", "erp-sales"),  # 云商
}


def _cool_skip(steps, root):
    """手动刷新：把「半小时内已成功抓过」的抓取步滤掉。

    返回 `(要跑的步骤, 跳过了哪几步)`。算的那几步（attain/plan/film…）**永远跑** ——
    跳过的只是"再拉一遍库"。
    """
    from .app import data_state as ds
    keep, skipped = [], []
    for s in steps:
        kinds = FETCH_COOLDOWN.get(s)
        if kinds and ds.fetched_within(root, kinds, minutes=FETCH_COOLDOWN_MIN):
            skipped.append(s)
        else:
            keep.append(s)
    return tuple(keep), tuple(skipped)
DEFAULT_CONFIG = "config/store-SCN231409.yaml"


def _shown_step_cmds() -> list:
    """「下一次自动跑」那两处（定时器页顶上那行大字 / 左下角小标）算**哪几步**。

    ⚠⚠ 判据是"**是不是内部步骤**"，**不是**"算不算每天那趟"（`Step.default`）——
      2026-09-21 用户：「我把**数据交换**改到 21:00，**这个位置不加上啊**，
      要注意以后这个地方」：`上报数据` / `收取门店上报` 是 `default=False`
      （它们各有自己的时刻），用 `default` 去筛的话，用户把它们调到 21:00
      之后，顶上那行大字**照样不列它们** —— 而那一刻它们**真的会跑**。
      那行字的语义是"**到点会跑什么**" ⇒ 得按"**实际排在这个时间**"算。
    ⚠ 唯一要排掉的是**内部步骤**（`autoupdate`：每小时都跑一趟）——
      它算进来的话，这两处永远显示"今天 15:17"（同一个坑，2026-09-21 踩过）。
    ⚠ 加新步骤**不用来改这儿**（名单从注册表派生）。
    """
    from .features import registry
    return [s.cmd for s in registry.all_steps()
            if s.cmd not in runner.INTERNAL_STEPS]


class CaptureJob:
    """自动抓 cookie 的后台任务状态（前端轮询它显示进度）。

    抓取要开浏览器、要等人登录，可能要几分钟 —— 所以必须异步，
    而且每一步都要报出来，否则用户不知道卡在哪。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.running = False
            self.state = "idle"        # idle|running|ok|saved|error|need_captcha
            self.need = ""             # 运行中"还差人做一件事"，目前只有 "captcha"
            self.steps: list[str] = []
            self.message = ""
            self.headless = True

    def say(self, msg: str):
        with self._lock:
            self.steps.append(msg)
            del self.steps[:-120]

    def snapshot(self) -> dict:
        with self._lock:
            return {"running": self.running, "state": self.state,
                    "steps": list(self.steps), "message": self.message,
                    "need": self.need, "headless": self.headless}


class PendingLogin:
    """等验证码的登录。

    ⚠ **必须留着那个 ErpClient** —— 验证码跟会话（cookie）绑定，
    换成新 client 再提交 VCode 一定验不过。
    """

    TTL = 300

    def __init__(self):
        self.reset()

    def reset(self):
        self.client = None
        self.username = ""
        self.password = None
        self.company = None
        self.image = ""
        self.tries = 0
        self.created_at = 0.0

    def hold(self, client, username, password, company, image):
        self.reset()
        self.client = client
        self.username = username
        self.password = password
        self.company = company
        self.image = image
        self.created_at = time.time()

    def alive(self) -> bool:
        return self.client is not None and (time.time() - self.created_at) < self.TTL


def store_path(app) -> Path:
    """这台机器的门店账号文件。

    ⚠ **必须挂在 `app.root` 下**，不能用 `erp` 那个按项目根解析的默认值 ——
      否则测试里的临时目录会去读**开发机上那份真凭据**，
      而"没配的机器该被门禁拦住"这类断言就会莫名其妙地通过（实测踩到）。
    """
    return Path(app.root) / STORE_ENV_FILE


def store_lookup(app, code: str = "", username: str = "",
                 password: str = "", company: str = "") -> dict:
    """**用门店云商账号读出「本店是哪家店」，并匹配门店名单。**

    用户 2026-09-18：「加个门店的登录设置吧，主要是读取这个账号的门店信息。
    来匹配不同门店的设置」。

    一次请求做完整条链：登录 → 读组织架构（只返回它自己那一家）→
    拿门店名去 `config/stores.yaml` 里匹配 → 回门店配置该填的那几项。

    ⚠ **只匹配，不写配置** —— 回填到表单里，由人确认后点「保存门店配置」。
       自动写的话，万一读到的是隔壁那家店（账号填错了），
       配置会被**静默**改掉，而界面上只会显示"已保存"。

    ⚠ 验证码：门店账号也可能要。要的时候返回 `need_captcha` + 图，
       并把 client **留着**（`pending_store`）—— 验证码跟会话绑定，
       换个 client 再提交一定验不过。
    """
    # ⚠ 支持**临时账密**：用户 2026-09-19 定「确认登录」的实际操作是
    #   "获取下登录信息看看账密是不是对，**对了就保存下来**"。
    #   所以得**先验证再落盘** —— 先 PUT 再验证的话，密码打错一个字母
    #   会把好密码覆盖掉，而失败提示只说"登录失败"，人根本不知道是保存造成的。
    tmp = {"username": username.strip(), "password": password,
           "company": company.strip() or None}
    d = describe_store_credentials(store_path(app))
    fresh = bool(tmp["username"] and tmp["password"])
    if fresh:
        d = {"username": tmp["username"], "has_token": False,
             "company": tmp["company"] or d.get("company") or "",
             "has_password": True}
    elif not (d["username"] or d["has_token"]):
        return {"ok": False, "error": "门店云商账号还没填 —— 先在上面填账号密码"}

    # ⚠ 门店账号**固定一个文件**，没有 per-app 覆盖 —— 公司账号那条能改
    #   `cfg.erp.env_file`（老门店自己改过路径），门店账号是新东西，不给这个口子。
    env = store_path(app)
    held = pending_store if (code and pending_store.alive()) else None
    if held is None:
        creds = load_store_credentials(store_path(app))
        if fresh:
            creds.update({"username": tmp["username"], "password": tmp["password"],
                          "company": tmp["company"] or creds.get("company")})
            creds["token"] = ""              # 新账密 ⇒ 旧 token 不算数
        elif not creds.get("token"):
            creds["token"] = ""
        client = ErpClient(creds, env_file=env, timeout=60)
    else:
        client = held.client
    try:
        if held is not None:
            r = client.login_and_verify(vcode=code, save=False)
        elif creds.get("token"):
            # 已有 token：直接用它读，读不动再登录（`call()` 自己会重登一次）
            r = {"token": creds["token"], "who": ""}
        else:
            r = client.login_and_verify(save=False)
        scope = client.branch_scope()
        node = scope["node"]
    except ErpCaptchaRequired as e:
        pending_store.hold(client, d["username"], None, d["company"], e.image)
        return {"ok": False, "need_captcha": True, "image": e.image,
                "message": "云商要图形验证码 —— 照着下图填一下（这是云商的，不是华为的）"}
    except ErpError as e:
        pending_store.reset()
        return {"ok": False, "error": str(e)}
    except Exception as e:                                     # noqa: BLE001
        pending_store.reset()
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}

    # 登录成过就把 token 落盘（下次不用再登，少撞限流）
    if r.get("token"):
        # ⚠ **验证通过了才落盘**（临时账密那条路要连密码一起存）
        kw = {"token": r["token"], "username": d["username"] or None}
        if fresh:
            kw["password"] = tmp["password"]
            kw["company"] = tmp["company"]
        # ⚠ 只在**拿到了**才写：`who` 是登录后额外拉一次用户资料才有的，
        #   那次拉取失败就返回空串。写空会把上次记住的姓名抹掉，
        #   而后台每天照跑、谁也看不出名字是什么时候没的。
        if r.get("who"):
            kw["who"] = r["who"]
        save_store_credentials(store_path(app), **kw)
    pending_store.reset()

    # ---- 平台岗：**不绑某一家店** —— 写个标记，菜单全开
    #
    # 用户 2026-09-19：「云商**平台岗账号**登录时提示是能匹配四十一家门店，
    # 不是按我要求的**给到所有功能页面的权限**」。
    if scope["platform"]:
        # ⚠ 2026-09-19（用户）：「平台岗就做个**虚拟的平台岗门店**就是了」。
        #
        # 原来是写一个**独立的标志** `platform: true`，而**不清**原来那家店的字段 ——
        # 于是配置里会同时留着「麦凯乐店」+「平台岗」，而画像是 platform 优先短路
        # ⇒ 那家店成了**死数据**，哪天标志一关它就"活过来"（"旧值一直赢"，
        # 这个坑 2026-09-18 在串号标识上踩过一次）。
        #
        # 现在：**跟普通门店登录走同一条路** —— 把「平台岗」这家虚拟店写进配置，
        # 顺手把别的身份字段清掉。画像照名单认（`kind: 平台岗` ⇒ type=platform）。
        # `platform: ""` 那一项是给**老配置**清的（那时它是个 true）。
        config_io.update(app.config_path, {
            "erp_store_name": config_io.PLATFORM_STORE,
            "store_code": "", "marker": "", "erp_branch_id": "", "platform": ""})
        pending_store.reset()
        hit = config_io.find_store(config_io.PLATFORM_STORE, app.root) or {}
        return {"ok": True, "platform": True, "count": scope["count"],
                "who": r.get("who", ""),
                "matched": {"found": bool(hit),
                            "erp_name": config_io.PLATFORM_STORE,
                            "huawei_code": "", "marker": "",
                            "huawei_name": hit.get("huawei_name") or "",
                            "kind": config_io.PLATFORM_KIND},
                "written": ["erp_store_name"], "rehint": False}

    # 门店账号：**把平台标记清掉**（换回门店账号时不能还留着它）
    before_platform = str(config_io.pick(
        config_io.load_raw(app.config_path)).get("platform") or "")
    if before_platform.strip().lower() in ("1", "true", "yes"):
        config_io.update(app.config_path, {"platform": False})

    name = str(node.get("Name") or "").strip()
    hit = config_io.find_store(name, app.root) if name else None
    matched = {
        # ⚠ `found=False` 不是错误：云商里的店不一定都在名单上。
        #   界面上要说清"只写进了云商名、编码是空的"，别静默少写一项。
        "found": bool(hit),
        "erp_name": (hit or {}).get("erp_name") or name,
        "huawei_code": (hit or {}).get("huawei_code") or "",
        "marker": (hit or {}).get("marker") or "",
        "huawei_name": (hit or {}).get("huawei_name") or "",
        "kind": (hit or {}).get("kind") or "",
    }

    # ---- **直接写进配置**（用户 2026-09-18：「直接写」）
    #
    # ⚠ 界面上那三个输入框已经删了，所以这里成了**唯一**的配置来源。
    # ⚠ **匹配上了就要写全，包括"名单里没有"的那几项**。
    #
    #   2026-09-18 实测踩到（用户当场发现）：「但是这个标识不应该存在啊」——
    #   他从新业广场店换登成**麦凯乐店**（合作店，名单里**没有**串号标识），
    #   而 `marker: Y` 是上一家店的**旧值没被清掉**。
    #   后果不只是脏数据：`needs_linglong` 是看 marker 的 ⇒
    #   **一家合作店被算成"要玲珑"**，永远进不去控制台。
    #
    #   所以分两种情形，判据是"名单里到底有没有这家店"：
    #   * **匹配上了** → 三项全写（空也要写，那是"这家店确实没有"）；
    #   * **没匹配上** → **只更新云商名**，编码和标识一个字都不动 ——
    #     那才是"问不出来"，抹掉会把门店上次配好的东西弄没，
    #     而现象是"华为订单突然查不到了"，完全看不出是这一步干的。
    before = config_io.pick(config_io.load_raw(app.config_path))
    updates = {}
    if matched["erp_name"]:
        updates["erp_store_name"] = matched["erp_name"]
    if node.get("Id") is not None:
        # 云商组织架构里的机构 Id —— 「人员设置」靠它筛本店员工
        updates["erp_branch_id"] = node.get("Id")
    if matched["found"]:
        updates["store_code"] = matched["huawei_code"]
        updates["marker"] = matched["marker"]
    if updates:
        config_io.update(app.config_path, updates)

    # ⚠ 华为会话是按**门店编码**存的文件（`.secrets/cbg-<编码>.json`）——
    #   编码一变等于换了一份会话，门店会看到「会话未导入」，像是会话丢了。
    #   所以变了就单独说一句，别让人自己猜。
    code_from = (before.get("store_code") or "").strip()
    code_to = (matched["huawei_code"] or code_from).strip()
    return {
        "ok": True,
        "who": r.get("who", ""),
        "store": {"erp_name": name, "branch_id": node.get("Id")},
        "matched": matched,
        "written": sorted(updates),
        "rehint": bool(code_to and code_to != code_from),
        "store_code_from": code_from,
        "store_code_to": code_to,
    }


#: 没「登录好」之前**还能调**的接口 —— 其余一律 403。
#:
#: ⚠ 前缀匹配，不是全等：`/api/session` 要放行 `/api/session/auto`（抓会话）、
#:   `/api/session/auto/browser`（检测浏览器）、`/api/session/ping`（自检）。
#:   写成一串全等的话，登录页上那几个按钮全是 403 —— 那就**永远登不进去**了。
SETUP_ALLOW = (
    "/api/health",          # start.bat / stop.bat 探活
    "/api/boot",            # 启动自检结果 —— **登录页也要看**（"为什么进不去"）
    "/api/setup",           # 登录页自己要看状态
    "/api/store-account",   # 第一步：云商门店账号
    "/api/session",         # 第二步：玲珑会话（抓取 / 自检 / 手抄 curl）
    "/api/hwlogin",         # 抓会话要用的华为账号密码
    # 「先看看界面」（预览模式）—— **登录页上那个按钮要能调**，所以必须在这儿
    "/api/setup/preview",
)


#: 「预览模式」标记 —— `.secrets/preview.json`（**这台机器自己的**，自更新不碰）。
#:
#: 用户 2026-09-21：「先把**体验店登录需要玲珑**这个跳过一下，我测试电脑上没有玲珑，
#: 但是我想看看**体验店的界面**」。
#:
#: ⚠⚠ 它**只放行界面**，不改任何判据：
#:   * 菜单 / 页面照 `needs_linglong` 走（所以能看到体验店那几页）；
#:   * **抓数 / 对比 / POS 该失败还是失败**（没有会话就是没有）——
#:     绝不能让"预览"变成"看着像能用"；
#:   * 界面上**一直挂着一条横幅**说清这是预览（下面 `preview` 字段给前端）。
PREVIEW_REL = ".secrets/preview.json"


def preview_on(app) -> bool:
    try:
        d = json.loads((app.root / PREVIEW_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool((d or {}).get("on"))


def set_preview(app, on: bool) -> dict:
    p = app.root / PREVIEW_REL
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"on": bool(on)}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        return {"ok": False, "message": "写不成：%s" % e}
    return {"ok": True, "preview": bool(on),
            "message": ("已进入预览模式 —— 界面能看，**抓数/对比/POS 仍然要玲珑会话**"
                        if on else "已退出预览模式")}


def setup_state(app) -> dict:
    """这台机器「能用了吗」—— 不能用就只给登录页。

    用户 2026-09-18：「我打算做个登录机制，门店鉴权。第一次安装会进入登录页面，
    需要先登录云商再登录玲珑才可以进入正式页面。不登录或者登录失败不给用」。

    两步的判据（用户定：**都要真的能用**）：

    * **云商** —— 门店云商账号**真登过**（有 token）。
      ⚠ 但**已经部署的机器放行**（用户：「能用就放行，不管是不是升级上来的」）：
      门店登录这个功能 2026-09-18 才有，14 家老店从来没登过门店账号，
      按"必须登过"会把它们全锁在登录页外面 —— 而它们本来干得好好的。
      所以：**门店身份已就绪**（配置里门店名 + 华为编码都在）也算过。
    * **玲珑** —— 会话在，**且自检通过**。会话过期正是要拦的情况。

    ⚠ 判据**不发网络请求**：这个函数每个 API 请求都会调一次，
       每次都去 ping 一下华为的话，控制台会被自己拖死。
    """
    v = config_io.pick(config_io.load_raw(app.config_path))
    store = describe_store_credentials(store_path(app))
    prof = config_io.store_profile(v, app.root)

    # ⚠ **平台岗**登录过就是过了 —— 它不绑某一家店，所以配置里
    #   `erp_store_name` / `store_code` 都是空的，走下面那条"门店身份已就绪"
    #   永远过不了（实测：平台岗被自己的门禁挡住）。
    if prof.get("platform"):
        erp_ok, erp_why = True, ""
    elif store.get("has_token"):
        erp_ok, erp_why = True, "门店账号已登录"
    elif prof["erp_name"] and prof["huawei_code"]:
        # 「能用就放行」：老机器没登过门店账号（那功能 2026-09-18 才有），
        # 但门店身份是配好的 —— 按"必须登过"会把 14 家老店全锁在登录页外面。
        erp_ok, erp_why = True, "门店配置已就绪"
    else:
        erp_ok = False
        erp_why = ("还没登录门店云商账号" if not store.get("username")
                   else "登录过，但没成功过 —— 再试一次")

    # ⚠ **要不要玲珑，取决于这家店是谁**（用户 2026-09-18）：
    #   「串号标识里有的那十四家店需要登录玲珑，其余店不需要」。
    #   合作店连 POS 合规都做不了（那页读的是玲珑数据）—— 它们的控制台
    #   只有「达成」，所以**不要求**登录玲珑，否则它们永远进不去。
    if not prof["needs_linglong"]:
        ling_ok, ling_why = True, ("这家店不走玲珑" if prof["erp_name"]
                                   else "还没认出是哪家店")
    else:
        s = app.session_info()
        if not s.get("exists"):
            ling_ok, ling_why = False, "还没导入玲珑会话"
        elif s.get("check_ok"):
            ling_ok, ling_why = True, "玲珑会话自检通过"
        else:
            ling_ok = False
            ling_why = (s.get("check_message") and "玲珑会话自检没过：%s" % s["check_message"]
                        or "玲珑会话还没自检过")

    # ⚠ **预览模式**：玲珑那一步没过、但用户显式开了预览 ⇒ 放行**界面**。
    #   其它一切照旧（`linglong.ok` 仍然是 False，前端据此挂横幅、也据此报错）。
    prev = preview_on(app)
    return {
        "ready": bool(erp_ok and (ling_ok or prev)),
        #: 现在是"预览模式"（玲珑没登录也能进来看界面）—— 界面上要一直挂着这条
        "preview": bool(prev and not ling_ok),
        "preview_available": bool(erp_ok and not ling_ok),
        "erp": {"ok": bool(erp_ok), "why": erp_why,
                "username": store.get("username") or "",
                "has_token": bool(store.get("has_token"))},
        "linglong": {"ok": bool(ling_ok), "why": ling_why},
        "profile": prof,
        # 缺哪一步 —— 前端据此决定登录页停在第几步
        "need": ("" if (erp_ok and (ling_ok or prev))
                 else ("erp" if not erp_ok else "linglong")),
    }


#: 「人员设置」里被门店**手动剔除**的人存这儿。
#:
#: ⚠ 为什么不进 `config/store-*.yaml`：那个写入器（`config_io.update`）只支持**标量**，
#:   列表写进去会变成 Python 的 `str(list)`，YAML 直接坏掉。
#: ⚠ 为什么在 `.secrets/`：它是**这台机器自己的**状态（跟云商账号一个性质），
#:   自更新一根手指都不碰。
# ⚠ 2026-09-19：人员名单的**业务逻辑搬到 `src/features/store/staff.py`** 了（阶段 2 的 2.3）。
#   原来它长在这里，而 `startup.py`（启动刷新）为了复用它只能 `from . import web` ——
#   一条反向依赖（业务模块反过来依赖 HTTP 层）。现在两边都调执行模块，
#   这一层只剩"请求 ↔ 响应"。
STAFF_REL = app_staff.STAFF_REL


# --------------------------------------------------------------- 角色与权限
#
# M17（2026-09-21 并入 3.0.0）：用户拍板 **B 方案** —— 真登录 + **按账号判角色**，
# 「门店 / 区长 / 平台」三种身份看到、能改的东西各不相同。
# 口径在 `.dsh/docs/2026-09-20-角色权限矩阵-设计.md`，
# 计划与数据契约在 `.dsh/docs/2026-09-18-3.0.0-开发目标.md` 的「四·八」。
#
# ⚠⚠ **这一节是唯一的判据。** 前端藏菜单不是权限（项目自己早写过的规矩：
#   「前端挡的话，别的程序照样能调接口」）—— 每个 `/api/*` 都要问这里。

#: 角色编码 —— 只有这三个。
ROLE_STORE = "store"
ROLE_MANAGER = "manager"
ROLE_PLATFORM = "platform"

#: 给界面 / 日志用的中文名
ROLE_LABELS = {ROLE_STORE: "门店", ROLE_MANAGER: "区长", ROLE_PLATFORM: "平台岗"}


def _can_for(role: str) -> dict:
    """这个角色**能写什么** —— 口径只有这一处（矩阵文档 §四 的 C1/C2/C5/C7）。

    ⚠ 写操作**永远回落到最小范围**：判"能写"之后，还要用 `scope_store_ok()`
      判"写的是不是他范围内的那家店"（比如门店只能写**本店**的目标拆分）。
    """
    is_store = role == ROLE_STORE
    return {
        # C1：目标拆分**只有门店能写**（区长/平台只读）
        "attain.split.write": is_store,
        # 「发送给区长」也是门店的动作 —— 区长/平台点它只是把自己的那份再发一遍
        "attain.split.send": is_store,
        # C2（2026-09-21 **改过**）：人员设置**只有门店能改自己店的**。
        # ⚠ 原来是"区长改所辖、平台改全部" —— 用户当天否了：
        #   「区长/平台**不能改**别家店的这份名单，**读取门店发送的状态表**吧」
        #   ⇒ 区长/平台那一页**只读**，而且读的是**门店发过来的那份状态表**
        #     （跟着每天那趟上报包一起来，见 `app/report.py` 的 `staff` 表）。
        "staff.write": is_store,
        # 机器属性（登录 / 邮件 / 企微 / 定时 / 更新）：**谁在这台机器上谁能改** —— 它改的是本机
        "machine.config": True,
        # C7：导出并推送 —— 登录即可用，但要记一条日志（见 `audit`）
        "push.inventory": True,
        # 导出为 Excel（用户 2026-09-21：「**区长账号有**导出为 excel 功能」，
        #   当天又补两句：「**平台也要能导出**」「**门店不用导出**」）⇒ 就这两种身份。
        # ⚠ 判据只有这一处：后端 403 用它，`/api/overview` 的 `role.can` 也用它，
        #   前端**照 `can` 藏按钮**（不在 `app.js` 里另写一遍"是不是门店" —— 那就是第二份判据）。
        "attain.export": not is_store,
        # 月度生意计划的导出 —— **跟达成同一个口径**（用户 2026-09-21：
        #   「区长账号有导出为 excel 功能」「平台也要能导出」「门店不用导出」）。
        # ⚠ 导出的范围就是**它自己看得到的那几家**（来源是 `App.plan()`，
        #   已经按身份滤过、合计也重算过）—— 门店压根没这个按钮。
        "plan.export": not is_store,
        "film.export": not is_store,
        "benefit.export": not is_store,
    }


def _store_aliases(names, root, rows=None) -> set:
    """门店名 → **两种写法的集合**（`erp_name` / `tdoc_name`）—— 范围判断用。

    ⚠ 门店名在本项目里有两种写法（腾讯文档里叫「鲁疆广场」、云商里叫「青岛鲁疆广场店」），
      拿一种写法去比另一种**永远不相等**，而表现是"区长看不到自己管的店"。

    ⚠ `rows` = **已经读出来的名单**（`config_io.stores_table`）。
      传进来就别再读第二遍：这一份 30 家、每读一遍 ~5ms，而
      `role_scope()` 是**每个 `/api/*` 请求都要算一次**的（实测按店数读 3~4 遍
      = 25ms/请求；只读一遍 ≈ 10ms）。
    """
    from . import config_io
    out = set()
    if rows is None:
        try:
            rows = config_io.stores_table(root)
        except Exception:                                      # noqa: BLE001
            rows = []
    for raw in names or ():
        n = str(raw or "").strip()
        if not n:
            continue
        out.add(n)
        hit = config_io.find_store_in(rows, n) or {}
        for k in ("erp_name", "tdoc_name"):
            if hit.get(k):
                out.add(str(hit[k]).strip())
        for r in rows:
            pair = {str(r.get("erp_name") or "").strip(),
                    str(r.get("tdoc_name") or "").strip()}
            if n in pair:
                out |= {x for x in pair if x}
    return out


def role_scope(app) -> dict:
    """**这台机器是什么身份、能看哪些店、能写什么** —— 全项目唯一的判据。

    ⚠⚠ 判定顺序：**区长 → 平台 → 门店**。为什么必须是这个顺序：
      平台岗原来的判据是"这个账号能看到 >1 家店"（`branch_scope()`），
      而**区长账号恰恰就是这种账号**（他管好几家）⇒ 先判平台的话，
      区长会被当成平台岗：**看全部门店、菜单全开，而界面上不会有任何提示**。
    ⚠ 认不出来一律退到**门店**（范围最小）—— 宁可少给，也不多给。

    | 键 | 含义 |
    |---|---|
    | `role` | `store` / `manager` / `platform` |
    | `label` | 给界面看的一句话（如「区长 杨英梅（西北区）」） |
    | `stores` | **门店名集合**（含两种写法）；⚠ `None` = 全部（平台岗） |
    | `can` | 每个操作能不能写（见 `_can_for`） |
    | `pages` | **这个身份看得见哪些页**（`PAGE_RULES` 派生，前端照着渲染） |
    | `kind` / `needs_linglong` | 店型 / 要不要玲珑（**可见性按后者，不按店型** —— 见文档「四·八」） |

    ⚠ 门店范围一律用 `scope_store_ok()` 判，别自己比字符串（两种写法会漏）。

    ⚠ **性能**：这个函数**每个 `/api/*` 请求算一次**（见 `Handler._api`），
      所以里面的文件读取都只能读一遍 —— 门店名单走 `roster()`
      （30 家 = 5ms/遍，按店数各读一遍实测 25ms/请求）。加字段前先看一眼这点。
    """
    from . import config_io
    try:
        cfg = config_io.load_raw(app.config_path) or {}
        prof = app._profile_with_who(cfg)
    except Exception:                                          # noqa: BLE001
        prof = {}
    try:
        acc = str(describe_store_credentials(store_path(app)).get("username") or "").strip()
    except OSError:
        acc = ""
    who = str(prof.get("who") or "").strip()
    kind = str(prof.get("kind") or "")
    needs_linglong = bool(prof.get("needs_linglong"))
    base = {"who": who, "account": acc, "kind": kind, "needs_linglong": needs_linglong}

    # ⚠ **门店名单整张只读一遍**，下面（范围别名 / 要不要玲珑）都用这一份。
    #   按需读（平台岗那条路读都不读）：这一份 30 家、每读一遍 ~5ms，
    #   而这是**每个 `/api/*` 请求都要算一次**的函数 —— 一家店读 3~4 遍
    #   实测 25ms/请求（只读一遍 ≈ 10ms）。⚠ 每个分支的 return 前都得算上它。
    _roster = []

    def roster():
        if not _roster:
            try:
                _roster.extend(config_io.stores_table(app.root))
            except Exception:                                  # noqa: BLE001
                pass
        return _roster

    # ① 区长（名单命中）—— **按账号**认人（账号唯一稳定）；账号没配时才退回按姓名
    #    （跟 `features/sales/attain/split.py::managers_of` 一个规矩）
    try:
        managers = config_io.managers_table(app.root)
    except Exception:                                          # noqa: BLE001
        managers = []
    for m in managers:
        accs = {str(x).strip().upper() for x in (m.get("accounts") or [])}
        hit = (acc and acc.upper() in accs) or \
              (not accs and who and str(m.get("name") or "").strip() == who)
        if not hit:
            continue
        # ⚠ 「这家店归不归他管」**只认 `config_io.stores_of_manager()`**（唯一口径）：
        #   老写法 `stores:`（手抄店名）和新写法 `regions:`（按门店的区域列自动圈）
        #   都在它里面合并。原来这里是 `if not m.get("stores"): continue` ——
        #   那样一写，**只按区域配的区长会被整条跳过**（表现是他掉成门店身份、
        #   只看得到一家店，而界面上没有任何提示）。
        mstores = config_io.stores_of_manager(m, roster())
        # ⚠⚠ 圈到 **0 家店**时：**认出来的身份不能丢**（他确实是区长，只是配置对不上）。
        #   退回"门店"是**错的方向** —— 那会让他看起来像一家店，而真正的原因
        #   （区域名打错 / 门店名单里没标区域）就再也没人看得见了。
        #   ⇒ 照旧给区长身份 + **把警告写进 label**（界面上任何显示身份的地方都带着它），
        #     同时 `config_io.region_audit()` 在自检里点名。
        warn = "" if mstores else "　⚠ 这个区在门店名单里一家店都没有（检查区域名 / 名单没标区域）"
        return _with_pages(dict(
            base, role=ROLE_MANAGER, region=str(m.get("region") or ""),
            label="区长 %s（%s）%s" % (m.get("name") or "?",
                                     m.get("region") or "未分区", warn),
            who=who or str(m.get("name") or ""),
            stores=_store_aliases(mstores, app.root, roster()),
            can=_can_for(ROLE_MANAGER)), app.root, roster())

    # ② 平台岗（不绑某一家店 —— 菜单全开、范围是全部）
    #
    # ⚠ **三个字段任一个成立都算平台岗**：`platform` / `show_all` / `type == "platform"`。
    #   为什么这么宽：这三个本来就是同一件事的不同写法（`store_profile()` 三个都给），
    #   而历史上两处判据**各看各的**（`attain_store()` 看 `show_all`、
    #   `_split_scope()` 看 `type`）⇒ 只认一个的话，另一处构造出来的画像会被判成门店，
    #   表现是"平台岗的达成突然只剩一家店"（实测：`test_web.py` 里两个不同来源的
    #   假画像各挂了一次）。三者互为同义，宽容没有代价。
    if (prof.get("platform") or prof.get("show_all")
            or str(prof.get("type") or "") == "platform"):
        return _with_pages(dict(base, role=ROLE_PLATFORM, region="", stores=None,
                                label="平台岗（全部门店）",
                                can=_can_for(ROLE_PLATFORM)), app.root)

    # ③ 门店（兜底：认不出来就用它，范围=本店）
    # ⚠ 店名**两个来源都看**：画像里的（正常情况）→ 配置里的（画像残缺时兜底）。
    #   老代码就是从配置取的；只看画像的话，"画像没给 erp_name"会变成"范围是空的"，
    #   而那意味着**什么都看不到**（比"看到别家"更糟：页面直接空白，还不报错）。
    name = (str(prof.get("erp_name") or "").strip()
            or str(cfg.get("erp_store_name") or "").strip())
    out = dict(base, role=ROLE_STORE, region="",
               label="门店 · %s" % (name or "（还没认出是哪家店）"),
               stores=_store_aliases([name] if name else [], app.root, roster()),
               can=_can_for(ROLE_STORE))
    return _with_pages(out, app.root, roster())


#: **页面 key → 谁能看见它** —— ⚠ 可见性表**只有这一份**（甲方案，用户 2026-09-21 定）。
#:
#: 前端（`web/app.js` 的 `applyProfile`）**照着这里下发的 `pages` 渲染**，
#: 不再自己在 HTML 上标 `data-types` —— 那样是"两处各写一份可见性"，而这个项目
#: 为"两份定义"栽过好几次（菜单藏了、接口还给，或者反过来）。
#:
#: 值的写法跟 HTML 时代**同一套词**（`config_io.STORE_TYPES`）：
#: `""` = 三类身份都看得见；`"experience platform"` = 只有**走玲珑的身份**和平台岗。
#: 那三类是怎么算出来的见 `scope_type()`：
#:   * 平台岗 ✓（它管的就是这些店）
#:   * **区长**：辖区里**至少有一家**要走玲珑的店 ✓（混合辖区按"有"算 ——
#:     整页藏掉会让区长以为那几家店没数据，逐店标注在 M20 里做）
#:   * 合作店 / 没有标识的体验店 ✗（它们压根没有玲珑数据，点进去是空的）
#:
#: ⚠ key 就是 HTML 上的 `data-tab` / `data-subtab` / `data-foot` **值本身**
#:   （一级 `compliance`、二级 `pos`、左下角 `linglong`）—— 前端不用记第二张对照表。
#: ⚠ **漏一个 key 的表现是"那一项永远看不见"**（`pages` 里没有它 ⇒ 被藏），
#:   所以有测试**双向对照**（HTML 的 key ↔ 这张表），见 `tests/test_roles.py`。
#: ⚠ 功能那部分（一级 + 二级）**是从注册表派生的**，别在这儿手写 —— 见下行。

#: 左下角那几项 —— ⚠ **不是功能模块**（是"这台电脑"自己的设置，注册表里没有它们），
#: 所以只能手写在这儿。值同一套词。
FOOT_PAGES = {
    "account": "", "general": "", "update": "", "scheduler": "",
    # 主题设置（2026-09-22）—— 这台电脑的外观，全员可见
    "theme": "",
    # ⚠ 「玲珑授权」是"给这台机器抓玲珑会话"的页面 —— 不走玲珑的身份看它没意义
    "linglong": "experience platform",
    # ⚠ 「数据交换」（M20 的「每店一张卡」）—— 用户 2026-09-21：
    #   「数据上报这个是好的，**放到左下角的系统设置**吧，一级标签改叫**数据交换**」。
    #   ⇒ 它从"顶部一级功能模块"变成左下角那一行，可见性也跟着从注册表挪到这儿
    #     （注册表只管**功能模块**的菜单；左下角这组是**手写**的，见文件头那段）。
    #   ⚠ 仍然是 `multi`：**只有区长 / 平台**看得见，门店连那一行都没有；
    #     而且接口对门店**直接 403**（C4：门店不能看全区排名）。
    "stores": "multi",
}


def _build_page_rules() -> dict:
    """`PAGE_RULES` 的来历：**功能那部分从注册表派生**，不是在这儿再抄一份。

    ⚠ 加一个功能页时**只改 `Feature` / `Sub` 的 `types`**（照开发指南 §四）——
      在这儿再写一行就是第二份定义，而两份定义走散的表现正是
      "菜单里藏了、接口还给"（M17 之前的样子）。
      派生链只有这一条：`Feature.types` / `Sub.types`（子空则继承父）→ 这张表。
    ⚠ 左下角那几项不是功能模块 ⇒ 手写在 `FOOT_PAGES`。
    """
    from .features import registry
    out = {}
    for f in registry.all_features():
        out[f.key] = f.types or ""
        for s in f.children:
            out[s.key] = s.types or f.types or ""
    out.update(FOOT_PAGES)
    return out


PAGE_RULES = _build_page_rules()


#: 可见性词表里**多出来的一档**：`multi` = **管多店的身份**（区长 / 平台）。
#:
#: ⚠ 为什么需要它（M20，2026-09-21）：「每店一张卡」只有区长/平台该看见，
#:   而 `experience` / `partner` 那三个词说的是"**这家店**要不要玲珑" ——
#:   区长机器上它算出来照样是 `experience`/`partner`，表达不了"他是管多店的"。
#: ⚠ 一个人**同时**属于几档：区长 = `multi` + （辖区里有没有玲珑店 → experience/partner），
#:   平台 = `multi` + `platform`。所以判据是**集合相交**，不是相等（见 `scope_types`）。
MULTI = "multi"


def scope_types(scope: dict, root=None, rows=None) -> set:
    """这个身份**算哪几档**（`{experience|partner}` / `+multi` / `+platform`）—— 决定可见性。

    ⚠ 这就是 HTML 时代 `data-types` 匹配的那个 `type`，只是现在在**后端**算一次
      （前端拿现成的 `pages` 渲染）。⚠ 判据是"**要不要玲珑**"，不是 `kind` 店型：
      青岛海信广场店是体验店但名单里没标识 ⇒ 它不看五项合规（那页是空的）。
    ⚠ **返回集合**：区长/平台既是 `multi`，又带着自己那一档玲珑语义
      （少了它，区长会连"五项合规"都看不见）。
    ⚠ 区长那一档的三种"没有"见 `_scope_has_linglong_store`（**宁多勿少**）。
    """
    sc = scope or {}
    role = sc.get("role")
    if role == ROLE_PLATFORM:
        return {MULTI, "platform"}
    if role == ROLE_MANAGER:
        ling = "experience" if _scope_has_linglong_store(sc, root, rows) else "partner"
        return {MULTI, ling}
    return {"experience" if sc.get("needs_linglong") else "partner"}


def scope_type(scope: dict, root=None, rows=None) -> str:
    """**主档**（单个值）—— 只给"要一个词"的地方用（日志、老的断言）。

    ⚠ 判可见性一律用 `scope_types()`（集合），别用这个 —— 区长用它会**丢掉 `multi`**。
    """
    got = scope_types(scope, root, rows)
    for want in ("platform", "experience", "partner"):
        if want in got:
            return want
    return "partner"


def _scope_has_linglong_store(scope: dict, root, rows=None) -> bool:
    """这个身份的范围里，**有没有一家要走玲珑的店**（区长混合辖区那一档）。

    ⚠ **三种"没有"要分开**，别都返回 `False`（那会把页面藏掉且毫无提示）：

    | 情况 | 返回 | 为什么 |
    |---|---|---|
    | 名单里**对上了**我的店，其中有带标识的 | `True` | 正常 |
    | 名单里**对上了**我的店，一家带标识的都没有 | `False` | 真的没有 —— 藏掉是对的 |
    | **一家都对不上**（名单里没这几家店）/ 名单读不到 | `True` | 认不出来 ⇒ **宁多勿少** |

    最后一档是这个项目的老规矩：**少给**的表现是"区长看不到辖区里某家店的合规，
    而他没有任何提示"（用户会以为那功能就是没有）；**多给**只是菜单多两项、
    点进去 403 或者空表。真正拦得住的是 `role_scope()` + `forbid()`，不是这里。

    ⚠ `rows` = 已经读出来的名单（见 `_store_aliases` 那段），别重复读。
    """
    stores = scope.get("stores")
    if stores is None:                    # 平台岗：本来就是全部
        return True
    want = {str(x).strip() for x in stores if str(x or "").strip()}
    if not want:
        return False
    from . import config_io
    if rows is None:
        try:
            rows = config_io.stores_table(root)
        except Exception:                                      # noqa: BLE001
            return True                                        # 读不到 ⇒ 别藏
    matched = False
    for r in rows:
        names = {str(r.get("erp_name") or "").strip(),
                 str(r.get("tdoc_name") or "").strip()}
        if names & want:
            matched = True
            if str(r.get("marker") or "").strip():
                return True
    return not matched                    # 一家都没对上 ⇒ 也是"认不出来"


#: 池明细行里"这家店是谁"那一列 —— 两个方向各一个（见 `_scope_pools`）。
POOL_STORE_KEYS = ("云商门店", "玲珑门店")


def _scope_pools(scope: dict, detail: dict) -> dict:
    """把四池明细**按范围筛一遍**（M20，M17 遗留③）。

    ⚠ 为什么必须在这儿筛：区长机器上抓的是**全公司**云商数据（账号就是系统那个，
      用户 2026-09-21 定的）⇒ `out/pools-<年>.json` 里**别家店的单子也在**，
      不筛的表现是"区长看到别区的差异清单，而界面完全正常" —— 这个项目最怕的一类错。
    ⚠ 判据用 `scope_store_ok()`（认两种店名写法），别自己比字符串。
    ⚠ 行里**两个店名列都可能空**（比如玲珑侧没匹配上）—— 空的那半不参与判断，
      两边都空就**留着**（宁多勿少：看不出来源的行删掉，用户会以为数据丢了）。
    """
    out = dict(detail or {})
    if scope.get("stores") is None:                # 平台岗：本来就该看全部
        return out
    for k in ("AD", "BC"):
        rows = []
        for r in (out.get(k) or []):
            names = [str(r.get(x) or "").strip() for x in POOL_STORE_KEYS]
            names = [n for n in names if n]
            if not names or any(scope_store_ok(scope, n) for n in names):
                rows.append(r)
        out[k] = rows
    counts = dict(out.get("counts") or {})
    for k in ("AD", "BC"):
        if k in counts:
            counts[k] = len(out.get(k) or [])
    out["counts"] = counts
    out["scoped"] = True
    return out


def pages_for(scope: dict, root=None, rows=None) -> list:
    """这个身份**能看见哪些页** —— 前端照着渲染（甲方案）。

    ⚠ 失败时返回 **空表**（谁都不给）还是**全部**？选**全部**：
      这一层是"菜单别露出来"的体验，真正的闸门在 `role_scope()` + `forbid()`
      （每个 `/api/*` 都拦）。可见性算错只会让人多点一下并看到 403，
      而"全都藏起来"会让门店**连自己该用的页面都找不到**（那才是事故）。

    ⚠ `rows` = 已经读出来的门店名单（`role_scope()` 手里那份，见 `_store_aliases`）。
    """
    mine = scope_types(scope, root, rows)
    try:
        return sorted(k for k, need in PAGE_RULES.items()
                      if not need or (mine & set(str(need).split())))
    except Exception:                                          # noqa: BLE001
        return sorted(PAGE_RULES)


def _with_pages(scope: dict, root=None, rows=None) -> dict:
    """给身份补上 `pages`（**最后一个 return 都得过这里**，别漏）。"""
    scope["pages"] = pages_for(scope, root, rows)
    return scope


def scope_store_ok(scope: dict, store: str) -> bool:
    """这家店在这个身份的范围内吗 —— **写操作和"按店读"都要先问它**。

    ⚠ `stores is None` = 全部（平台岗）；⚠ **空名字一律不放行**：
      "没指定门店"不等于"随便哪家"（那种默认值最贵，这个项目为它栽过）。
    """
    s = str(store or "").strip()
    if not s:
        return False
    if scope.get("stores") is None:
        return True
    return s in (scope.get("stores") or set())


def forbid(scope: dict, what: str, why: str) -> dict:
    """越权时的统一回复（HTTP 403）。

    ⚠ **必须带 `error`** —— 前端 `api()` 读的是 `data.error || data.message`，
      只有 `why` 的话界面上会显示成干巴巴的「HTTP 403」。
    ⚠ 也带 `role` / `what`：排查时一眼看出"是谁、想干什么被拦了"。
    """
    return {"ok": False, "forbidden": True, "what": what, "role": scope.get("role"),
            "who": scope.get("who") or scope.get("account") or "",
            "scope": scope.get("label") or "",
            "error": "没有权限：%s（当前身份：%s）" % (why, scope.get("label") or "?")}


def audit(app, what: str, scope: dict = None, **detail) -> None:
    """改**本机配置**这类动作记一笔：谁、什么时候、改了什么。

    ⚠ 矩阵里对"机器属性"那一档的要求就是这条（改可以，但要留痕）：
      「那是对的（机器属性），但要**记一条日志**：谁改的」。
    ⚠ `runlog.record` 本来就不抛 —— 记不上不影响动作本身。
    """
    sc = scope or role_scope(app)
    who = sc.get("who") or sc.get("account") or "（不知道是谁）"
    try:
        runlog.record("config", True, note="%s · %s" % (sc.get("label") or "?", what),
                      root=app.root, who=who, **detail)
    except Exception:                                          # noqa: BLE001
        pass


def _staff_file(app) -> Path:
    return app_staff.staff_file(app.root)


def staff_state(app) -> dict:
    """本店员工名单 —— **转发给 `features.store.staff`**（这里不再有业务逻辑）。"""
    return app_staff.staff_state(app.root, app.config_path, app.erp_env_file())


def _fmt_ts(sec) -> str:
    """unix 秒 → `YYYY-MM-DD HH:MM`。

    ⚠ 用**本机时区**，跟前端 `fmtTs()` 一模一样 —— 两边不一致的话，
    同一个时间在抽屉里和「玲珑授权」页上会差几个小时，看着像两个数。
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(sec)))
    except (TypeError, ValueError):
        return "—"


pending_login = PendingLogin()
#: 门店账号的验证码暂存 —— **跟公司账号分开**：验证码跟会话绑定，
#: 混用一个的话，两边同时点会互相把对方那次登录顶掉。
pending_store = PendingLogin()
capture_job = CaptureJob()


def _capture_worker(app: "App", headless: bool):
    """后台抓会话：启动浏览器 → 等登录 → 读 cookie + csrf → **自检通过才落盘**。"""
    job = capture_job
    try:
        cfg = config_io.load_raw(app.config_path)
        store = cfg.get("store_code") or None

        def verify(sess):
            """⚠ 返回 `(过没过, 为什么)`，**别只返回 bool**。

            `ping()` 的第二个返回值里写着真正的病因
            （"会话/权限问题：没有门店或数据范围 XXX 的权限"、"接口异常：…"），
            老写法 `.ping()[0]` 把它扔了，用户最后只看到一句"自检没过" ——
            实测就卡在这儿：只能反复说"就是抓不到"，谁也定位不了。
            """
            try:
                ok, why = CbgClient(sess, store_code=store, timeout=25).ping()
                return ok, why
            except (CbgAuthError, CbgError) as e:
                return False, f"{type(e).__name__}: {e}"

        profile = browser.profile_path(cfg, app.root)
        found = browser.find_browser(cfg)
        job.say(f"浏览器：{found[1]}（{found[0]}）" if found else "没找到 Chrome / Edge")
        job.say(f"profile：{profile}")
        job.say("无头静默续期…" if headless else "已打开浏览器窗口，请在里面登录华为账号…")
        # 无头不需要等人，等太久没意义；有窗口的登录流程才给足时间
        creds = browser.load_login_credentials(cfg, app.root)
        if browser.captcha_marked(app.root):
            # ⚠ 上次撞了验证码。再自动填只会把**同一个**验证码撞出来、
            #   然后又被中止 —— 死循环，"请手动登录"那句提示永远执行不了。
            job.say("上次抓取撞上了图形验证码 —— 这次**不自动填账号密码**，"
                    "请在窗口里手动登录并输入验证码")
            creds = ("", "")
        if all(creds):
            job.say(f"用已保存的华为账号自动登录：{creds[0]}")
        else:
            job.say("没配华为账号密码（或上次撞了验证码）—— 会等你手动登录")
        sess = browser.capture_session(profile, headless=headless,
                                       timeout=90 if headless else 300,
                                       on_step=job.say, verify=verify,
                                       url=browser.login_url(cfg),
                                       credentials=creds if all(creds) else None,
                                       state_root=app.root,
                                       on_need=lambda what: setattr(job, "need", what))
        p = sess.save(app.session_path(cfg))              # 只有自检过了才走到这里
        ok, msg = CbgClient(sess, store_code=store).ping()
        app.record_check(sess, ok, msg)                   # 记下这次自检，界面要显示时间
        job.say(f"已保存 → {p.name}")
        job.state = "ok" if ok else "saved"
        job.message = msg
    except browser.CbgCaptchaRequired as e:
        # ⚠ 走到这儿浏览器**已经关了**（`capture_session` 的 finally）——
        #   现在才能删 profile：Windows 上还有句柄就删不掉。
        #   而且只有这里知道 `app.root`，所以删除放在这一层。
        job.state = "need_captcha"
        deleted, dmsg = browser.delete_profile(profile)
        job.message = f"{e}\n{dmsg}"
        job.say(("✅ " if deleted else "⚠️ ") + dmsg)
        job.say("下一步：重新点「打开浏览器抓取」，在窗口里**手动登录并输入验证码**"
                "（这次不会再自动填账号密码了）。")
    except Exception as e:                                # noqa: BLE001
        job.state = "error"
        job.message = str(e)
        job.say(f"❌ {type(e).__name__}: {e}")
        if headless:
            # 静默续期要求这台电脑**之前用有界面的方式成功登录过**
            # （profile 里得有 SSO 登录态）。没有的话它必然失败，
            # 而且失败得很闷 —— 无头模式下没有任何窗口让人补登录。
            job.say("提示：静默续期要求这台电脑之前**用「打开浏览器抓取」成功登录过**。"
                    "没登录过就先点它一次（窗口里登完一次就行），以后静默续期才有效。")
        else:
            job.say("提示：登录完**别关那个浏览器窗口** —— 程序还要从它那里读 cookie，"
                    "读完它自己会关。")
        job.say("现有会话**没有被覆盖** —— 先照旧用着，再试一次或走手抄 curl。")
    finally:
        job.running = False


class App:
    """把根目录和配置路径绑在一起，省得每个 handler 都去猜。"""

    def __init__(self, root: Path | str = ROOT, config: str = DEFAULT_CONFIG):
        self.root = Path(root)
        self.config = config
        self.server = None                  # serve() 里塞进来，/api/shutdown 要用
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        # 启动脚本自愈只做一次（见 `_selfheal_runner_script`）
        self._runner_healed = False
        self._runner_rebuilt = False
        # 启动自检结果（`boot_state` 里填）
        self._boot = None
        self._boot_at = 0.0
        #: `attain_split_all` 逐个店调 `attain_split` 时**共用一份身份** ——
        #: 每个店算一遍 `role_scope()` 是 10ms × 店数（实测 30 家 = 300ms）。
        self._split_scope_cache = None

    @property
    def config_path(self) -> Path:
        p = Path(self.config)
        return p if p.is_absolute() else self.root / p

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    # ------------------------------------------------------------------ 会话
    def session_path(self, cfg: dict | None = None) -> Path:
        """华为会话文件路径 —— **口径在 `modules/auth`**（这里以前是第二份实现）。

        ⚠ 原来这里是 `f".secrets/cbg-{store}.json"` 的**第二份**，和
          `cli.session_path` 逐字一样 —— 改一处忘一处就是"换店没换会话"。
          现在全项目只有 `auth.session_path` 一份。
        """
        cfg = cfg if cfg is not None else config_io.load_raw(self.config_path)
        return auth.session_path(cfg, self.root)

    # ------------------------------------------------------- 启动自检（健康）
    def boot_state(self, force: bool = False) -> dict:
        """`modules/health.boot()` 的结果 —— 启动流程、界面横幅都看它。

        ⚠ 缓存 15 秒：`/api/boot` 会被界面反复拉，而 `check_data` 要开库数行。
          `force=True` 给**服务启动**那一次用（那时还没人拉过）。
        ⚠ **绝不抛**：自检自己炸了只是"看不到状态"，
          绝不能让控制台起不来（AGENTS.md 坑 2：启动路径上不许有失败模式）。
        """
        now = time.time()
        if not force and self._boot and (now - self._boot_at) < 15:
            return self._boot
        try:
            self._boot = health.boot(self.root, config_path=self.config_path)
        except Exception as e:                                 # noqa: BLE001
            self._boot = {"items": [], "ok": False, "blocking": [], "warnings": [],
                          "todos": [], "allow_start": True,
                          "why": "自检自己出错了：%s: %s" % (type(e).__name__, e)}
        self._boot_at = now
        return self._boot

    # ------------------------------------------------------ 会话自检记录
    def check_state_path(self) -> Path:
        return self.root / ".secrets" / "session-check.json"

    def _fp(self, sess) -> str:
        """会话指纹 —— 判断"这条自检记录还算不算数"。

        换了一份会话（重新抓取 / 重新导入）之后，旧记录必须失效；
        否则界面会拿旧的"✅ 通过"去给新会话背书 —— 那比不显示更糟。

        ⚠ **门店也要算进去**。自检是"拿这份会话去查**配置里那个门店**"，
        所以改了门店（设置页那三行，改完立刻生效、不用重启）之后，
        同一个会话的有效性就完全变了：
          - 从"没权限的店"改成"有权限的店" → 旧记录说没过，其实现在能过
          - 反过来 → 旧记录说过了，其实现在查不了
        只按 cookie 算指纹的话，这两种都会被缓存骗过去。
        """
        import hashlib
        store = ""
        try:
            store = str(config_io.load_raw(self.config_path).get("store_code") or "")
        except Exception:                          # noqa: BLE001
            pass
        seed = f"{sess.cookies or ''}\x00{store}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]

    def record_check(self, sess, ok: bool, message: str) -> None:
        """记下"什么时候自检过、结果如何"。写不进去不影响自检本身。"""
        try:
            p = self.check_state_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "at": time.time(), "ok": bool(ok), "message": message,
                "fp": self._fp(sess),
            }, ensure_ascii=False), encoding="utf-8")
        except (OSError, TypeError):
            pass

    def _raw_check(self) -> dict:
        try:
            d = json.loads(self.check_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    def last_check(self, sess) -> dict:
        """**只认指纹对得上的**记录；对不上就当没自检过。"""
        d = self._raw_check()
        return d if d and d.get("fp") == self._fp(sess) else {}

    def check_stale(self, sess) -> bool:
        """有自检记录，但**属于上一份会话** —— 界面据此提示"要重新自检"。

        前端自己判断不了这个（它只看到"没有记录"），所以得后端说。
        """
        d = self._raw_check()
        return bool(d) and d.get("fp") != self._fp(sess)

    def session_info(self) -> dict:
        p = self.session_path()
        info = {"exists": p.exists(), "path": str(p.relative_to(self.root))
                if str(p).startswith(str(self.root)) else str(p)}
        if not p.exists():
            return info
        try:
            s = CbgSession.load(p)
        except Exception as e:                       # noqa: BLE001
            info["error"] = f"会话文件读不出来：{e}"
            return info
        names = [c.split("=", 1)[0] for c in s.cookies.split("; ") if c]
        info.update({
            "cookie_names": names,
            "csrf": f"{s.csrf[:4]}…{s.csrf[-4:]}" if len(s.csrf) > 8 else "****",
            "saved_at": p.stat().st_mtime,
        })
        chk = self.last_check(s)
        if chk:
            info.update({"checked_at": chk.get("at"),
                         "check_ok": bool(chk.get("ok")),
                         "check_message": chk.get("message") or ""})
        elif self.check_stale(s):
            info["check_stale"] = True
        return info

    def status_brief(self) -> dict:
        """右下角那个状态抽屉要的一屏 —— **一个请求给全，话也由后端写好**。

        用户 2026-09-18：「点击后的悬窗显示目前门店的名称，编码，公司云商账号状态、
        玲珑会话状态，推送哪个通道是开着的，下面是运行一次按钮和日志窗口」。

        ⚠ **别让前端自己拼**（overview + /api/erp + /api/mail + /api/wecom 四个请求）：
        ① 那四个接口的字段名一改，抽屉就静默变空，而界面上只会显示一个「—」；
        ② 措辞（"内置账号" / "自检失败" / "都没开"）本身就是**判断**，
           散在前端就只能靠肉眼看，`pytest` 一条也测不到 —— 这个项目在
           "同一件事两处各写一份"上栽过好几次。

        返回的是**可以直接渲染的行**：`{label, value, kind}`，
        `kind` 只用来上色（`ok` / `warn` / `bad` / 空）。
        """
        cfg = config_io.load_raw(self.config_path)
        v = config_io.pick(cfg)
        rows = []

        def add(label, value, kind=""):
            rows.append({"label": label, "value": value, "kind": kind})

        # ---- 这台电脑是哪家店
        erp_name = (v.get("erp_store_name") or "").strip()
        add("门店名称", erp_name or "未配置", "" if erp_name else "bad")
        code = (v.get("store_code") or "").strip()
        add("华为门店编码", code or "会话默认", "" if code else "warn")
        marker = (v.get("marker") or "").strip()
        add("串号标识（本店码）", marker or "未配置", "" if marker else "bad")

        # ---- 公司云商账号
        # ⚠ **只说"能不能用"，不露账号名、也不露文件路径**。
        #
        #   用户 2026-09-18：「（可）密码来自 `.dsh/secrets/` 下那个 env，这个不要」。
        #   两条都不要露，理由不一样：
        #   * **路径**是本机内部实现，对门店毫无意义（`~/.dsh/secrets/…` 是开发机的，
        #     门店那台上根本不是这个路径），摆出来只会让人以为"要去改那个文件"；
        #   * **账号名**是用户早就定过的「公司账号前端不显示」——
        #     抽屉里把它印出来，等于绕开那条规矩又露了一遍。
        #
        #   ⚠ 这一行还踩过一次"自相矛盾"：第一版拆成「没配置」+「密码实际来自 …」两行，
        #     而 `describe_credentials` 按设计**只读指定的那个文件**，
        #     所以"这个文件里没有" ≠ "没账号"（真账号在回落链下一站）。
        #     现在四种来源**一律显示「已配置」**，靠 `kind` 上色区分好坏就够了。
        d = describe_credentials(self.erp_env_file())
        if d.get("builtin") or d.get("has_password") or d.get("used_from"):
            add("公司云商账号", "已配置", "ok")
        elif d.get("has_token"):
            # 只有 token 没密码：现在能用，但 token 一过期就续不上
            add("公司云商账号", "只有 token，没存密码 —— 过期后续不上", "warn")
        else:
            add("公司云商账号", "没配置", "bad")

        # ---- 玲珑（华为）会话
        s = self.session_info()
        if not s.get("exists"):
            add("玲珑会话", "未导入", "bad")
        elif s.get("check_ok"):
            add("玲珑会话", "自检通过（%s）" % _fmt_ts(s.get("checked_at")), "ok")
        elif s.get("checked_at"):
            add("玲珑会话", "自检失败：%s" % (s.get("check_message") or "未知原因"), "bad")
        else:
            add("玲珑会话", "已导入，还没自检过", "warn")

        # ---- 定时器：**下一次跑什么、上次跑成没成**（用户 2026-09-20：
        #      「定时器设置页面最上面大字写着下一次执行的是啥，什么时间。
        #        右下角的控制板也加上这个」）
        from .modules import timer as _timer
        try:
            # ⚠⚠ 2026-09-21 用户：「**这个悬浮窗就别显示下次时间了**」——
            #   "下一次什么时候跑"这件事**只留在「定时器设置」页**（顶上那行大字，
            #   还能点进去改）。悬浮窗是"读一眼现在什么状态"的地方，
            #   摆一个下一趟时间既不完整（同刻几步要合成一条才准）、又容易过期。
            #   ⇒ 这里**只留"上次自动跑"**（发生过的事，不会因为看的时间而变）。
            # ⚠ 上一次要跳过"只有内部步骤"的那几趟（自动更新每小时都跑）
            w = ([x for x in _timer.wakes(self.root, limit=20)
                  if [c for c in x["steps"] if c not in runner.INTERNAL_STEPS]] or [None])[0]
            if w:
                add("上次自动跑", "%s · %s · %s"
                    % ((w.get("at") or "")[5:16], w.get("label") or "",
                       "成功" if w.get("ok") else ("失败：%s" % (w.get("why") or "未知"))),
                    "ok" if w.get("ok") else "bad")
            else:
                add("上次自动跑", "还没跑过", "")
        except Exception as e:                                 # noqa: BLE001
            # ⚠ 控制板是"读一眼状态"的地方，**定时器读不出来不许把整屏带崩**
            add("上次自动跑", "读不出来：%s: %s" % (type(e).__name__, e), "warn")

        # ---- 推送通道：**开着的**才列出来，一个都没有就直说
        chans = []
        mc = mailer.describe_mail(self.mail_config())
        if mc.get("enabled"):
            chans.append("邮件" + ("" if mc.get("ready") else "（缺配置）"))
        wc = wecom.describe_wecom(self.wecom_config())
        if wc.get("enabled"):
            chans.append("企业微信" + ("" if wc.get("ready") else "（缺配置）"))
        add("推送通道", "、".join(chans) if chans else "都没开",
            "ok" if chans else "warn")

        return {"rows": rows}

    # ------------------------------------------------- 黄横幅"关掉过哪条"
    #: 记"这一条已经被关掉了" —— 只存**指纹**（`data_state.fingerprint`）。
    #: ⚠ 放 `.secrets/`（这台机器自己的界面状态；自更新不碰）。
    DISMISS_FILE = ".secrets/ui-dismissed.json"

    def _data_state_with_dismiss(self) -> dict:
        """数据状态 + "这条被关掉过没"。

        ⚠ 用户 2026-09-21：「上面这个东西**一直不让关**」——
          原来那条黄横幅只能看、不能关，数据一坏就一直挂着。
        ⚠ 但**关的是"这一条"**：指纹一变（又失败一次 / 换了状态 / 换了数据源）
          就重新露出来 —— 不然"关掉"就等于把告警永久关掉了。
        """
        brief = app_data.brief(app_data.data_state(self.root))
        brief["dismissed"] = bool(brief.get("fingerprint")
                                  and brief["fingerprint"] == self._dismissed_fp())
        return brief

    def _dismissed_fp(self) -> str:
        try:
            raw = json.loads((self.root / self.DISMISS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str((raw or {}).get("data_state") or "")

    def dismiss_data_state(self) -> dict:
        """把**当前这条**数据告警关掉（指纹写进 `.secrets/`）。"""
        brief = app_data.brief(app_data.data_state(self.root))
        fp = brief.get("fingerprint") or ""
        if not fp:
            return {"ok": True, "message": "本来就没有要关的"}
        path = self.root / self.DISMISS_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            cur = {}
            try:
                cur = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                cur = {}
            cur["data_state"] = fp
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            return {"ok": False, "message": "记不下来：%s" % e}
        return {"ok": True, "message": "这条数据告警先收起来了 —— "
                                      "再出新的（或又失败一次）会重新弹出来",
                "fingerprint": fp}

    def cbg_client(self) -> CbgClient:
        cfg = config_io.load_raw(self.config_path)
        sess = CbgSession.load(self.session_path(cfg))
        return CbgClient(sess, store_code=cfg.get("store_code") or None)

    # ------------------------------------------------------------------ 云商
    def erp_env_file(self) -> str:
        cfg = config_io.load_raw(self.config_path)
        return (cfg.get("erp") or {}).get("env_file") or DEFAULT_ENV_FILE

    def erp_client(self) -> ErpClient:
        """造一个云商客户端（**只有一个账号**，见 `erp.DEFAULT_ENV_FILE`）。

        ⚠ 2026-09-20 起**有人调它了**：库存盘点那组接口（`inventory_*`）。
          那是"页面找数据抓取模块抓取最新的库存"那条链的入口 ——
          门店不再需要在盘点页里再登一次云商账号。
        """
        f = self.erp_env_file()
        return ErpClient(load_credentials(f), env_file=f, timeout=60)

    # ------------------------------------------------------------------ 邮件
    # ⚠ 2026-09-22 设置改版：`MAIL_KEYS` 只给**老 yaml 回落**用；
    #   界面保存走 `/api/mail` 的 `paths`（`.secrets/push-paths.json`）。
    MAIL_KEYS = ("host", "port", "security", "sender", "recipients",
                 "subject_prefix")

    def mail_config(self) -> mailer.MailConfig:
        return mailer.load_mail_config(config_io.load_raw(self.config_path), self.root)

    def mail_paths_raw(self) -> list:
        cfg = config_io.load_raw(self.config_path)
        return mailer.describe_mail_paths(cfg, self.root)

    def mail_from_body(self, body: dict) -> mailer.MailConfig:
        """用界面上填的值拼一份配置 —— **测试邮件不先保存**（跟云商登录一个道理）。

        body 里出现的字段就**以 body 为准，空了就是空的** ——
        否则用户清空服务器地址再点测试，会悄悄回退到已保存的值，
        明明填错了却"测试通过"，最难查的那种坑。
        （密码例外：它是"留空＝不改"，因为界面上永远不回显。）
        """
        # 路径模式：body 本身就是一条路径行（测试不先保存）
        if "host" in body or "paths" in body:
            row = dict(body)
            if isinstance(body.get("paths"), list) and body["paths"]:
                row = dict(body["paths"][0])
            # 密码没填 / 没回显 → 按 id 从已存路径取
            if not row.get("password") and row.get("id"):
                try:
                    from . import push_paths as _pp
                    if _pp.has_file(self.root):
                        for r in _pp.load_raw(self.root).get("mail") or []:
                            if str(r.get("id") or "") == str(row["id"]):
                                row["password"] = r.get("password") or ""
                                break
                except Exception:                               # noqa: BLE001
                    pass
            return mailer.mail_from_row(row, self.root)
        cfg = config_io.load_raw(self.config_path)
        m = dict(cfg.get("mail") or {})
        for k in self.MAIL_KEYS:
            if k in body:
                m[k] = body[k]
        cfg["mail"] = m
        mc = mailer.load_mail_config(cfg, self.root)
        if "username" in body:
            mc.username = str(body.get("username") or "").strip()
        if body.get("password"):
            mc.password = str(body["password"])
        return mc

    # ------------------------------------------------------------------ 企微
    WECOM_KEYS = ()   # 界面字段已全走 paths；保留空元组兼容老引用

    def wecom_config(self) -> wecom.WecomConfig:
        return wecom.load_wecom_config(config_io.load_raw(self.config_path), self.root)

    def wecom_from_body(self, body: dict) -> wecom.WecomConfig:
        """用界面上填的值拼配置 —— 测试推送**不先保存**。"""
        if "webhook" in body or "paths" in body:
            row = dict(body)
            if isinstance(body.get("paths"), list) and body["paths"]:
                row = dict(body["paths"][0])
            # 界面不回显 webhook：没粘新的就按 id 取已存的
            if not row.get("webhook") and row.get("id"):
                try:
                    from . import push_paths as _pp
                    if _pp.has_file(self.root):
                        for r in _pp.load_raw(self.root).get("wecom") or []:
                            if str(r.get("id") or "") == str(row["id"]):
                                row["webhook"] = r.get("webhook") or ""
                                break
                except Exception:                               # noqa: BLE001
                    pass
            return wecom.wecom_from_row(row, self.root)
        cfg = config_io.load_raw(self.config_path)
        w = dict(cfg.get("wecom") or {})
        for k in self.WECOM_KEYS:
            if k in body:
                w[k] = body[k]
        cfg["wecom"] = w
        wc = wecom.load_wecom_config(cfg, self.root)
        if body.get("webhook"):
            wc.webhook = str(body["webhook"]).strip()
        return wc

    # ------------------------------------------------------------------ 总览
    def filter_attain_rows(self, d: dict) -> dict:
        """按身份把达成数据过滤成**我该看的那几行** —— 判据只有 `role_scope()` 一份。

        | 身份 | 看哪些行 |
        |---|---|
        | 门店 | 本店那一行 |
        | **区长** | **所辖那几家**（含 `erp_name` / `tdoc_name` 两种写法） |
        | 平台 | 全部（不过滤） |

        ⚠ 为什么**读的时候**还要再滤一遍：落盘那份 `out/attain-<年>.json`
          是**算的时候**按当时的门店过滤的，而页面读的可能是**别人（或上一次）**
          算好的那一份 —— 门店账号下就会看到全区（用户 2026-09-21 报的就是这个）。
        ⚠ 匹配规则跟算的时候**同一套**（`store` 或 `erp_name` 精确相等），
          各写一套迟早出现"算出来有、页面上没有"。
        ⚠⚠ 2026-09-21（M17）：判据改成 `role_scope()`。原来这里自己看 `show_all`，
          而"区长"在原逻辑里**根本不存在**（`store_profile()` 只认
          experience / partner / platform）⇒ 区长机器上会退化成
          "只看某一家"或"看全部"，两种都不对。原来那个 `attain_store()`
          （只会返回**一个**店名或空串）一并删掉 —— 它表达不了"所辖几家"，
          留着只会被下一个人拿去用错。
        """
        sc = role_scope(self)
        stores = sc.get("stores")
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        from .features.sales.attain import metric as attain_metric
        if stores is None:                                     # 平台岗：不过滤
            d["store_filter"] = ""
            # ⚠ 分区共计**跟着当前 rows 重算**（老落盘可能没有 region_sums，
            #   或者是别人算的全区那份 —— 跟 summary 同一个坑）
            d["region_sums"] = attain_metric.region_sums(
                d.get("rows") or [], d.get("weights") or [])
            return d
        allow = {str(x).strip() for x in stores if str(x or "").strip()}
        rows = [r for r in (d.get("rows") or [])
                if str(r.get("store") or "").strip() in allow
                or str(r.get("erp_name") or "").strip() in allow]
        d["rows"] = rows
        d["region_sums"] = attain_metric.region_sums(rows, d.get("weights") or [])
        d["store_filter"] = "、".join(sorted(allow))
        if not rows and d.get("exists"):
            # ⚠ 过滤完一条都没剩：要说清"这份数据里没有你要看的店"，
            #   而不是让页面显示一片空白（"看着很合理的空"最坑）。
            d["error"] = ("这份达成数据里没有「%s」%s —— 可能在别的机器上算的"
                          "（那边看的是全区）。点「刷新」会按本店重算。"
                          % (d["store_filter"] or "本店",
                             "那一行" if len(allow) == 1 else "那几行"))
        return d

    def filter_plan_rows(self, d: dict) -> dict:
        """按身份把月度生意计划过滤成**我该看的那几家店** —— 判据只有 `role_scope()` 一份。

        | 身份 | 看哪些店 |
        |---|---|
        | 门店 | 本店 |
        | **区长** | **所辖那几家**（名单里按区域圈出来的） |
        | 平台 | 全部（名单内的 28 家） |

        ⚠ 落盘那份 `out/plan-<年>.json` 里是**名单内全部 28 家**
          （名单外那 15 家在**算的时候**就剔了，见 `plan.load_sales`）——
          但区长机器上抓的就是全公司，所以**读的时候必须按当前身份再过一遍**。
        ⚠⚠ **滤完 `summary` 必须重算**（`metric.summarize`）：那份合计本来是 28 家的，
          不重算的话门店看到的合计比自己的明细大十几倍 —— 而界面上完全正常，
          属于最难解释的一类错（比"多给一行"严重得多）。
        ⚠ 匹配用 `scope_store_ok()`（认两种店名写法），别自己比字符串。
        """
        from .features.plan.monthly import metric as plan_metric
        sc = role_scope(self)
        stores = sc.get("stores")
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        if stores is None:                                     # 平台岗：不过滤
            d["store_filter"] = ""
            # ⚠ 分区共计**跟着当前 rows 重算**（老落盘可能没有 region_sums —— 跟 summary 同坑）
            d["region_sums"] = plan_metric.region_sums(
                d.get("rows") or [], d.get("regions") or {})
            return d
        rows = [r for r in (d.get("rows") or [])
                if scope_store_ok(sc, str(r.get("store") or ""))]
        d["rows"] = rows
        d["summary"] = plan_metric.summarize(rows)             # ⚠ 跟着滤过的 rows 重算
        d["region_sums"] = plan_metric.region_sums(rows, d.get("regions") or {})
        d["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
        if not rows and d.get("exists"):
            # ⚠ 滤完一条都没剩：要说清"这份数据里没有你要看的店"，
            #   而不是让页面显示一片空白（"看着很合理的空"最坑）。
            d["error"] = ("这份月度数据里没有「%s」%s —— 可能在别的机器上算的"
                          "（那边看的是全部）。等下一次定时器到点会按本机重算。"
                          % (d["store_filter"] or "本店",
                             "那一行" if len(stores) == 1 else "那几行"))
        return d

    def filter_film_rows(self, d: dict) -> dict:
        """防护膜达成按身份滤店 —— 判据跟 `filter_plan_rows` 同一份 `role_scope()`。

        ⚠ 门店登录锁本店、区长锁所辖（用户 2026-09-22：跟无忧会员权益同一套）。
        """
        from .features.valueadd.film import metric as film_metric
        sc = role_scope(self)
        stores = sc.get("stores")
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        if stores is None:
            d["store_filter"] = ""
            return d
        rows = [r for r in (d.get("rows") or [])
                if scope_store_ok(sc, str(r.get("store") or ""))]
        d["rows"] = rows
        d["summary"] = film_metric.summarize(rows)
        d["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
        return d

    def film(self) -> dict:
        """「增值 · 防护膜达成情况」—— 本地销售明细**现算**；范围先按身份收窄再滤。"""
        from .features.valueadd.film import compute as film_compute
        sc = role_scope(self)
        # stores=None（平台）= 全部；门店/区长只算自己看得见的那些店
        d = film_compute.load(self.root, stores=sc.get("stores"))
        return self.filter_film_rows(d)

    def film_export(self, who: str = "", name: str = "") -> dict:
        """导出防护膜达成 Excel —— 走的就是 `self.film()`（已按身份过滤）。"""
        from .features.valueadd.film import export as film_export_mod
        return film_export_mod.export(self.root, self.film(), who=who, name=name)

    def filter_benefit_rows(self, d: dict) -> dict:
        """无忧会员权益按身份滤店 —— 店 / 区域 / 人员三块都滤，合计重算。

        ⚠ 门店锁本店、区长锁所辖，判据只有 `role_scope()` 一份（坑 18）。
        """
        from .features.valueadd.benefit import metric as benefit_metric
        sc = role_scope(self)
        stores = sc.get("stores")
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        if stores is None:
            d["store_filter"] = ""
            return d
        srows = [r for r in (d.get("stores") or [])
                 if scope_store_ok(sc, str(r.get("store") or ""))]
        d["stores"] = srows
        d["summary"] = benefit_metric.summarize_stores(srows)
        if srows:
            by_region = {}
            for r in srows:
                by_region.setdefault(r.get("region") or "未分组", []).append(r)
            order = [x.get("region") for x in (d.get("regions") or [])]
            new_regs = []
            seen = set()
            for name in order:
                if name in by_region and name not in seen:
                    new_regs.append(benefit_metric.region_row(name, stores=by_region[name]))
                    seen.add(name)
            for name, grp in by_region.items():
                if name not in seen:
                    new_regs.append(benefit_metric.region_row(name, stores=grp))
            d["regions"] = new_regs
            d["summary_regions"] = benefit_metric.summarize_regions(new_regs)
        prows = [r for r in (d.get("people") or [])
                 if scope_store_ok(sc, str(r.get("store") or ""))]
        d["people"] = benefit_metric.rank_people(prows)
        d["summary_people"] = benefit_metric.summarize_people(d["people"])
        # 赛道：只留范围内店的分成
        tracks = []
        for t in (d.get("tracks") or []):
            rows = [r for r in (t.get("rows") or [])
                    if scope_store_ok(sc, str(r.get("store") or ""))]
            tracks.append(dict(t, rows=rows,
                               paid=sum(r.get("paid") or 0 for r in rows)))
        d["tracks"] = tracks
        d["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
        return d

    def benefit(self) -> dict:
        """「增值 · 无忧会员权益」—— 本地 erp_sales 现算；名册走**系统人店表**。"""
        from .features.valueadd.benefit import compute as benefit_compute
        sc = role_scope(self)
        d = benefit_compute.load(
            self.root,
            stores=sc.get("stores"),
            config_path=self.config_path,
            env_file=self.erp_env_file(),
        )
        return self.filter_benefit_rows(d)

    def benefit_export(self, who: str = "", name: str = "") -> dict:
        from .features.valueadd.benefit import export as benefit_export_mod
        return benefit_export_mod.export(self.root, self.benefit(), who=who, name=name)

    def claim_activities(self) -> dict:
        """「小工具 · 权益领取 · 活动一览」—— 只读配置，零网络。

        ⚠ 返回**全部**活动并标 `expired`：前端默认只显示进行中，
          可点「显示已过期」；过期行文字用删除线。
          待领匹配仍用 `in_window(销售日)` —— 期外卖的机器不进待领。
        """
        import datetime as _dt
        from .features.tools.claim.activities import catalog
        today = _dt.date.today().isoformat()
        all_acts = catalog.load_activities(self.root)
        acts = []
        n_active = n_out = 0
        for a in all_acts:
            item = dict(a)
            item["expired"] = not catalog.in_window(a, today)
            if item["expired"]:
                n_out += 1
            else:
                n_active += 1
            acts.append(item)
        note = "%d 场进行中" % n_active
        if n_out:
            note += " · %d 场已过期（可勾选显示）" % n_out
        return {
            "ok": True,
            "activities": acts,
            "active_count": n_active,
            "expired_count": n_out,
            "note": note,
            "today": today,
            "role": role_scope(self).get("role"),
        }

    def claim_pending(self) -> dict:
        """「小工具 · 权益领取 · 待领清单」—— erp_sales 匹配 + 状态 join + **滤店**。

        ⚠ 门禁：**登录**（接口不在 `SETUP_ALLOW`）+ **`role_scope()` 滤店**
          —— 门店账号只能读到本店；区长所辖；平台全部（坑 18）。
        """
        from .features.tools.claim.pending import compute as claim_compute
        from .features.tools.claim.pending import metric as claim_metric
        sc = role_scope(self)
        stores = sc.get("stores")
        # scope_store_ok 认两种店名 —— compute 里先按 erp_name 集合粗滤，
        # 这里再按 scope 精滤（别名）并重算合计。
        d = claim_compute.load(self.root, stores=list(stores) if stores is not None else None)
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        if stores is None:
            d["store_filter"] = ""
            d["scoped"] = False
            return d
        rows = [r for r in (d.get("rows") or [])
                if scope_store_ok(sc, str(r.get("store") or ""))]
        d["rows"] = rows
        d["summary"] = claim_metric.summarize(rows)
        d["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
        d["scoped"] = True
        return d

    def _claim_rows_in_scope(self) -> list:
        """当前身份**有权碰**的待领行（与读接口同一套滤店）。"""
        d = self.claim_pending()
        return list(d.get("rows") or [])

    def _claim_sn_allowed(self, sn: str) -> bool:
        """提交用的号必须出现在**本身份待领清单**里 —— 不许替别家店提交。

        认两种：`sn`（销售侧，状态键用）和 `claim_sn`（反查到的真 SN，
        在线领取实际提交的号）。前端点「在线领取」带的是 `claim_sn`。
        """
        sn = str(sn or "").strip()
        if not sn:
            return False
        for r in self._claim_rows_in_scope():
            if sn in (str(r.get("sn") or "").strip(),
                      str(r.get("claim_sn") or "").strip()):
                return True
        return False

    def _claim_key_allowed(self, key: str) -> bool:
        """status_key 必须在**本身份待领清单**里 —— 不能改别店状态。"""
        key = str(key or "").strip()
        if not key:
            return False
        for r in self._claim_rows_in_scope():
            if str(r.get("status_key") or "") == key:
                return True
        return False

    def sn_trace(self, code: str) -> dict:
        """「小工具 · 串号追踪」—— 86 码 / SN → 库存快照 + 销售记录。

        ⚠ 门禁：**登录**；销售明细按 `role_scope()` 滤店（库存是全库快照只读）。
        """
        from .features.tools.sn_trace import trace as sn_trace_mod
        sc = role_scope(self)
        stores = sc.get("stores")
        d = sn_trace_mod.collect(
            self.root, code,
            stores=set(stores) if stores is not None else None)
        d["role"] = sc.get("role")
        d["scope"] = sc.get("label") or ""
        if stores is None:
            d["store_filter"] = ""
            d["scoped"] = False
        else:
            d["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
            d["scoped"] = True
        return d

    def claim_status_set(self, key: str, status: str, by: str = "", note: str = "") -> dict:
        """标已领 / 撤销 —— 写 `out/claim-status.json`。

        ⚠ **门禁**：`status_key` 必须属于当前身份滤店后的待领行
          （门店只能改本店，区长只能改所辖）。
        """
        from .features.tools.claim.pending import status as claim_status
        sc = role_scope(self)
        if not self._claim_key_allowed(key):
            return {"ok": False,
                    "why": "这条待领不在你的门店范围内（%s）" % (sc.get("label") or "?"),
                    "forbidden": True}
        who = by or sc.get("who") or sc.get("account") or ""
        return claim_status.set_status(self.root, key, status, by=who, note=note)

    def claim_query(self, sn: str, activity_id: str = "") -> dict:
        """按 SN 查华为可领权益（可选按活动 privilege 码过滤，**不提交**）。

        ⚠ **门禁**：SN 必须在本身份待领清单里。
        """
        from .features.tools.claim.activities import catalog
        from .features.tools.claim import submit as claim_submit
        sc = role_scope(self)
        if not self._claim_sn_allowed(sn):
            return {"ok": False, "forbidden": True,
                    "why": "该 SN 不在你的门店待领范围内（%s）" % (sc.get("label") or "?")}
        q = claim_submit.query_rights(sn)
        if not q.get("ok"):
            return q
        rights = q.get("rights") or []
        act = catalog.activity_by_id(activity_id, self.root) if activity_id else None
        codes = set()
        if act:
            for c in act.get("privilege_codes") or []:
                codes.add(str(c).strip())
        matched = [r for r in rights if not codes or str(r.get("privilegeCode") or "") in codes]
        return {
            "ok": True,
            "sn": q.get("sn"),
            "code": q.get("code"),
            "rights": rights,
            "matched": matched,
            "activity_id": activity_id or "",
            "note": ("命中活动权益 %d / 可领 %d" % (len(matched), len(rights))) if act
                    else ("可领权益 %d 项" % len(rights)),
        }

    def claim_submit_online(self, sn: str, activity_id: str = "",
                            status_key: str = "") -> dict:
        """**直提**华为领取接口（不跳转）；成功后顺手把本机待领标成已领。

        ⚠ **门禁**：SN / status_key 都必须在本身份待领清单里。
        """
        from .features.tools.claim.activities import catalog
        from .features.tools.claim import submit as claim_submit
        from .features.tools.claim.pending import status as claim_status
        sc = role_scope(self)
        label = sc.get("label") or "?"
        if not self._claim_sn_allowed(sn):
            return {"ok": False, "forbidden": True,
                    "why": "该 SN 不在你的门店待领范围内（%s）" % label}
        if status_key and not self._claim_key_allowed(status_key):
            return {"ok": False, "forbidden": True,
                    "why": "这条待领不在你的门店范围内（%s）" % label}
        act = catalog.activity_by_id(activity_id, self.root) if activity_id else None
        if activity_id and not act:
            return {"ok": False, "why": "活动不存在：%s" % activity_id}
        codes = list((act or {}).get("privilege_codes") or [])
        if activity_id and not codes:
            # ⚠ 没码就**不全提** —— 会把 SN 下别的活动权益一起领掉
            return {"ok": False,
                    "why": "该活动未配置 privilege 码，请改用官网领取页"}
        res = claim_submit.claim(sn, privilege_codes=codes or None)
        res["activity_id"] = activity_id or ""
        # 官网 popupInfo3 =「已经领取过」—— 对门店来说结果就是**已领**，
        # 本机状态直接标掉，别让人再点一次「标已领」（用户 2026-09-23）。
        already = (
            str(res.get("popup_key") or "") == "popupInfo3"
            or "已经领取" in str(res.get("why") or res.get("popup") or "")
            or "已经领取" in str(res.get("popup") or "")
        )
        res["already_claimed"] = bool(already and not res.get("ok"))
        should_mark = bool(res.get("ok") or res.get("already_claimed"))
        if should_mark and status_key:
            if not self._claim_key_allowed(status_key):
                return {"ok": False, "forbidden": True,
                        "why": "这条待领不在你的门店范围内（%s）" % label}
            who = sc.get("who") or sc.get("account") or ""
            note = ("华为接口直提成功" if res.get("ok")
                    else "华为返回已领取，本机自动标已领")
            claim_status.set_status(
                self.root, status_key, "claimed", by=who,
                note="%s code=%s" % (note, res.get("code")))
            res["local_marked"] = True
            # 已领取时把 ok 提成 true？ —— 不：前端要能区分文案。
            # 只加 already_claimed + local_marked，失败态仍走 else 分支渲染。
        return res

    def plan(self) -> dict:
        """「月度生意计划」的数据 —— **纯读盘，一个网络请求都不发**。

        由 `python -m src.cli plan`（或日常流程第 6 步）算好落
        `out/plan-<年>.json`，这一页只读它。**看板不做计算，也不读数据库** ——
        理由跟达成那边一样：算一次要全表扫两遍，而页面是随时会被刷新的。
        """
        from .features.plan.monthly import plan as plan_mod
        d = plan_mod.load(self.root)
        return self.filter_plan_rows(d)

    def plan_export(self, who: str = "", name: str = "") -> dict:
        """把**我现在看得到的这份月度数据**导成 Excel 落到本机。

        ⚠⚠ 数据来源就是 `self.plan()` —— 它里面已经按身份过滤过、合计也重算过。
          这里**绝对不许**另开一条"直接读 `out/plan-*.json` 再导"的近路：
          那份落盘文件里是**全部 28 家**，绕过去等于把 `filter_plan_rows`
          那份判据整个废掉，而界面上不会有任何异常（AGENTS.md 坑 18）。
        """
        from .features.plan.monthly import export as plan_export_mod
        d = self.plan()
        if not d.get("exists"):
            # 「还没算过」/「读不出来」要在导出这一步就说清楚，**别导一个空表出去**
            return {"ok": False, "state": "failed", "path": "", "file": "", "rel": "",
                    "why": d.get("error") or d.get("hint") or "还没有月度数据，先跑一次"}
        return plan_export_mod.export(self.root, d, who=who, name=name)

    def attain(self) -> dict:
        """「周度目标达成情况」的数据 —— **纯读盘，一个网络请求都不发**。

        达成由 `python -m src.cli attain`（或日常流程的第 5 步）算好落
        `out/attain-<年>.json`，这一页只读它。**看板不做计算，也不读腾讯文档** ——
        概览页 30 秒刷一次，每次去读文档的话：① 慢；② 办公室改表会让界面
        在两次刷新之间跳数字（而"哪个数算数"说不清）。

        ⚠ 读不到时返回 `{"exists": False, "error": …}`，**绝不给
          `exists: True` + 空 rows 的壳** —— 那就是"看着很合理的空"。
        """
        from .features.sales.attain import attain as attain_mod
        d = attain_mod.load(self.root)
        # ⚠⚠ **按登录进来的身份过滤**（用户 2026-09-21 报的 bug：
        #   「我登录了个门店的账号，他的周度重点达成情况显示的是**全部的**」）。
        #   落盘那份 `out/attain-<年>.json` 是**算的时候**按当时的门店过滤的，
        #   而页面读的是**别人（或上一次）算好的那一份** —— 在门店账号下就会看到全区。
        #   ⇒ 读的时候**再按当前画像过一遍**：门店端只留自己那一行，
        #     平台岗 / 办公室（`show_all`）照旧看全部。
        #   ⚠ 匹配规则跟算的时候**同一套**（`store` 或 `erp_name` 精确相等）——
        #     各写一套的话，"算出来有、页面上没有"这种自相矛盾迟早出现。
        self.filter_attain_rows(d)
        from .features.plan.monthly.plan import stores_by_region
        regmap = stores_by_region(self.root)
        # ⭐ **区域一律以 stores.yaml 为准**（用户 2026-09-22：「周度重点产品分区错了」）——
        #   腾讯文档 A 列实测是合并块里的 `城阳\n胶州`，跟月度/区长圈店用的
        #   西北区/市区/南区不是一套。yaml 有这家店就覆盖；没有才留文档里那份。
        #   ⚠ 原来是 `if not r.get("region")` 才补 —— 文档分区非空 ⇒ 永远补不上。
        for r in d.get("rows") or []:
            key = str(r.get("erp_name") or r.get("store") or "").strip()
            yaml_reg = regmap.get(key) or ""
            if yaml_reg:
                r["region"] = yaml_reg
            elif not r.get("region"):
                r["region"] = ""
        # ⚠ 分区共计要在**补完 region 之后**再算一次 —— filter 里那次用的还是
        #   老落盘里可能缺 region 的行（补 region 和滤店是两步，顺序不能反）。
        from .features.sales.attain import metric as attain_metric
        d["region_sums"] = attain_metric.region_sums(
            d.get("rows") or [], d.get("weights") or [])
        # 「周度重点产品 › 设置」要显示"这条推送现在走不走得出去"。
        # ⚠ 只读**本地配置**（不发请求、不试连）—— 通道能不能真发出去，
        #   那是"跑一次"才知道的事，页面上不该假装知道。
        try:
            cfg = config_io.load_raw(self.config_path) or {}
            mc = mailer.load_mail_config(cfg, self.root)
            wc = wecom.load_wecom_config(cfg, self.root)
            from .modules.notify import prefs as push_prefs
            from .features.sales.attain import attain as attain_mod
            head, lines = attain_mod.notify_lines(d, "")
            d["notify"] = {"mail_enabled": bool(getattr(mc, "enabled", False)),
                           "wecom_enabled": bool(getattr(wc, "enabled", False)),
                           "push_on": push_prefs.enabled("attain", self.root),
                           "preview": {"head": head, "lines": lines}}
        except Exception as e:                                 # noqa: BLE001
            d["notify"] = {"mail_enabled": False, "wecom_enabled": False,
                           "push_on": True,
                           "why": "通道配置读不出来：%s" % e}
        return d

    def attain_export(self, who: str = "", name: str = "") -> dict:
        """把**我现在看得到的这份达成数据**导成 Excel 落到本机 —— 用户 2026-09-21：

        > 「区长账号有**导出为 excel** 功能，这个落在数据推送模块吧，功能模块
        >   调用『导出为 excel』来把自己的数据生成为 excel **到本地**」

        ⚠⚠ **数据来源就是 `self.attain()`** —— 它里面已经按身份过滤过了。
          这里**绝对不许**另开一条"直接读 `out/attain-*.json` 再导"的近路：
          那份落盘文件里是**全部门店**（区长机器上算的就是全区），
          绕过去等于把 `filter_attain_rows` 那份判据整个废掉，
          而界面上不会有任何异常（AGENTS.md 坑 18 说的正是这个）。

        ⚠ 文件落在 `out/exports/`（`notify.EXPORT_DIR`），**同名不覆盖**往后编号。
        """
        from .features.sales.attain import export as attain_export
        d = self.attain()
        if not d.get("exists"):
            # 「还没算过」/「读不出来」要在导出这一步就说清楚，**别导一个空表出去**
            # （那就是"看着很合理的空"——用户拿着空 Excel 只会以为导出坏了）。
            return {"ok": False, "state": "failed",
                    "why": d.get("error") or d.get("hint") or "还没有达成数据，先跑一次",
                    "path": "", "file": "", "rel": ""}
        return attain_export.export(self.root, d, who=who, name=name)

    def export_file(self, name: str):
        """导出件的绝对路径（下载接口用）；**没有这份 / 名字不合法 → `None`**。

        ⚠ 判据是"名字里不许有分隔符"，不是 `startswith(out_dir)`
          （那个写法在 `..%2f` 之类的编码花样面前不牢靠；这里干脆只许**一个文件名**）。
        """
        from .modules import notify
        if not name or Path(name).name != name or not name.lower().endswith(".xlsx"):
            return None
        target = notify.export_dir(self.root) / name
        return target if target.is_file() else None

    def timer_tasks(self) -> dict:
        """**定时器任务表**（用户 2026-09-20 的接口）—— 界面那张表就是它。

        ⚠ 「做什么」和「什么时候」来自**功能模块的声明**（`Step.whens`），
          这台机器改过的在 `.secrets/wakes.json`；「跑不跑」还是
          「自动化跑什么」那一份 —— **没有第二个开关**。
        """
        from .modules import timer
        # ⚠ `busy` 传的是控制台那个任务管理器：定时器派发的每一趟都从它走，
        #   所以"此刻在不在跑"以它为准最准（只读状态文件的话，跑完最多会
        #   多显示 30 秒 —— 状态是下一跳才清的）。
        state = timer.now(self.root, busy=lambda: bool(manager.current()))
        # ⚠ **内部步骤不进前端**（用户 2026-09-21：「自动更新不进入计时器前端显示，
        #   前端日志也不显示」）—— 它每小时都跑，摆在任务表里既占地方、
        #   又关不掉（`required`），日志里更会把真活淹掉。
        #   ⚠ 它**照样记进 runlog**（排查要用），只是不往前端露。
        return {"exists": True,
                "tasks": [t for t in timer.tasks(self.root)
                          if t["cmd"] not in runner.INTERNAL_STEPS],
                "next_at": timer.next_at(self.root),
                # ⭐「下一次执行的是啥、什么时间」——定时器页顶上那行大字用它。
                #   ⚠ 同刻到点的几步**合成一条**（那正是派发时的行为）；
                #     分开列会写成"21:00 抓数据，21:00 算 POS"，看着像跑两趟。
                # 下一次 = **到点真的会跑的那几步**（见 `_shown_step_cmds`：
                #   只排掉自动更新那种内部步骤，**不**按 `default` 筛 ——
                #   用户把「上报数据」调到 21:00 之后，它就该出现在这行字里）
                "next_run": timer.next_run(self.root, cmds=_shown_step_cmds()),
                # ⭐ 执行日志：什么时间唤醒了什么、成功没（`run_record` 里 kind="wake"）
                # 执行日志同样只留"给人看的"那几趟（内部步骤的照样记在 runlog 里）
                "history": [w for w in timer.wakes(self.root, limit=40)
                            if [x for x in w["steps"]
                                if x not in runner.INTERNAL_STEPS]],
                "state": state,
                "problems": timer.wake_problems(self.root),
                "knobs": {"window_minutes": timer.WAKE_WINDOW_MINUTES,
                          "interval_seconds": timer.HEARTBEAT_SECONDS,
                          "note": "到点后 %d 分钟内跑；错过了不补（等下一个时间点）"
                                  % timer.WAKE_WINDOW_MINUTES}}

    def timer_set_when(self, cmd: str, whens) -> dict:
        """改一步的唤醒时刻（空列表 = 恢复默认时间，**不动开关**）。"""
        from .modules import timer
        try:
            res = timer.set_whens(self.root, cmd, whens)
        except ValueError as e:
            return {"ok": False, "why": str(e)}
        # ⚠ "下一次"一律是**这一步自己的**（`timer.next_of`），**不是**全局那个
        #   （`timer.next_at` / `next_run` 把所有步骤放一起取最近的，而自动更新
        #   每小时都跑 :17 ⇒ 白天任何时候都是它。2026-09-21 用户就是这么被绕进去的：
        #   「『dump』改成：每天 21:00（下一趟 2026-09-21 **15:17**）。
        #   这个下一趟是自动更新的，**不要显示自动更新的**」）。
        # ⚠ 它**当数据回**（`next_at` / `next`），不塞进那句提示语：提示语贴在行里，
        #   而"下次什么时候"那一列就在旁边写着 —— 重复一遍只会让那句话长到换行。
        mine = res.get("next") or {}
        res["next_at"] = mine.get("at", "")
        # ⚠ 三种情况分开说（用户：「单击保存会弹出来一些奇怪的东西，会突然消失」——
        #   那句话在**一个字没动**时也说"改成…"，本身就怪）：
        #   * **一个字没动**（点保存只是再存一遍）⇒ 直说"没改动"
        #   * 恢复默认（`whens: []`；它 = 跟随模块声明的默认值，**不是**"只手动跑"）
        #   * 真改了
        # ⚠ 也不带「「步骤名」」—— 它就贴在那一行上，再来一遍又长又重复。
        if not res.get("changed"):
            head = "没改动：本来就是%s" % (res.get("text") or "—")
        elif res.get("restored"):
            head = "已恢复默认：%s" % (res.get("text") or "—")
        else:
            head = "已保存：%s" % (res.get("text") or "—")
        # ⚠ 关着的步骤要**说出来**（不然"改好了"会让人以为到点会跑）
        res["message"] = head if not mine.get("off") else head + "（这一步现在没在跑）"
        return res

    def timer_set_order(self, order) -> dict:
        """**改执行顺序**（用户 2026-09-21：「相同时间执行的任务，按照定时器这个列表
        从上到下执行，然后定时器列表给个调顺序的功能」）。

        `order` = 界面那张表**从上到下**的 `cmd` 串（整份发过来，不是"上移一格"）——
        整份发的好处：后端不用猜"现在是什么顺序"，也不会因为并发改动作废。
        ⚠ 只改顺序（`whens` / `enabled` 不动）；认不出来的 `cmd` 当场拒（400）。
        """
        from .modules import timer
        try:
            res = timer.set_order(self.root, order)
        except ValueError as e:
            return {"ok": False, "why": str(e)}
        res["tasks"] = [t for t in timer.tasks(self.root)
                        if t["cmd"] not in runner.INTERNAL_STEPS]
        res["message"] = "执行顺序已更新：%s" % res.get("text", "")
        return res

    def timer_set_enabled(self, cmd: str, on: bool) -> dict:
        """**那个开关滑块**：开了就注册到定时器，不开就不注册（用户 2026-09-20）。"""
        from .modules import timer
        try:
            res = timer.set_enabled(self.root, cmd, on)
        except ValueError as e:
            return {"ok": False, "why": str(e)}
        res["state"] = timer.now(self.root, busy=lambda: bool(manager.current()))
        res["next_run"] = timer.next_run(self.root)
        return res

    def attain_history(self, period: str = "") -> dict:
        """周度达成的**历史记录**（用户 2026-09-20：过了一周就把上一周锁住存档）。

        ⚠ **只读** —— 存档那边唯一的写入口在 `attain.run()` 里换周那一下。
          这里给个写入口的话，"锁住"就不成立了。
        """
        from .features.sales.attain import attain as attain_mod
        if period:
            # ⚠ 存档那一份**也是全区的** ⇒ 同样按当前身份过滤（跟实时那份一个口径）
            return self.filter_attain_rows(attain_mod.history_load(self.root, period))
        return {"exists": True, "items": attain_mod.history_list(self.root)}

    def attain_split(self, store: str, period: str, roster=None) -> dict:
        """一家店一周的**成员目标 + 达成** —— 点门店名展开时用。

        ⚠ 达成的**台量**来自已经算好的落盘（`people`：谁卖了哪几台），
          目标来自 `out/attain-split.json`（人填的）。
          这里**不重算**任何东西 —— 跟门店层那张表用同一份数据。
        """
        from .features.sales.attain import split as attain_split
        scope = getattr(self, "_split_scope_cache", None) or role_scope(self)
        self._split_scope_cache = scope
        d = self.attain()
        if not d.get("exists"):
            return {"exists": False, "error": d.get("error") or "还没算过"}
        period = period or d.get("period") or ""
        row = {}
        for r in d.get("rows") or []:
            if r.get("store") == store or r.get("erp_name") == store:
                row = r
                break
        if not row:
            return {"exists": False, "error": "这份数据里没有这家店：%s" % store}
        cols = d.get("columns") or []
        ncols = len(cols)
        # ⭐ 2026-09-21（M21）：**区长 / 平台那边，目标不是本机填的，是店长发邮件来的**
        #   （用户：「店长改，发给区长，**区长邮件里加载**」）。
        #   ⇒ 非门店身份**优先读收信库**（`in/report.db` 的 `splits` 表），
        #     读不到才回落到本机那份（比如平台岗自己那台机器上的旧数据）。
        #   ⚠ 这条也**顺带堵住一个坑**：区长机器上的 `out/att-split.json` 只有它自己
        #     那家"虚拟平台岗"的拆分 —— 拿它当"所辖各店的目标"是错的（全是空的）。
        source = None
        saved = {}
        if scope.get("role") != ROLE_STORE:
            from .app import report_inbox as inbox_mod
            got = inbox_mod.split_of(self.root, store, period)
            if got:
                saved = dict(got.get("targets") or {})
                source = {"kind": "mail", "store_code": got.get("store_code") or "",
                          "period": got.get("period") or "",
                          "imported_at": got.get("imported_at") or "",
                          "generated_at": got.get("generated_at") or "",
                          "from": got.get("saved_by") or "",
                          "unverified": bool(got.get("unverified")),
                          "file": got.get("file") or "",
                          "mail_date": got.get("mail_date") or ""}
                if got.get("columns"):
                    cols = got["columns"] or cols
                    ncols = len(cols)
        if not saved:
            saved = attain_split.targets_of(self.root, store, period)
        members, seen = [], []
        for i, col in enumerate(row.get("people") or []):
            for rec in col:
                who = rec[0] if rec else ""
                if who and who not in seen:
                    seen.append(who)
        # ⚠⚠ **在册成员也要在名单里**（2026-09-19 补）：原来只从"这周卖过的人"里推，
        #   于是**没卖过东西的人根本分不了目标** —— 而"给他定目标"恰恰是这一页的意义。
        #
        # ⚠⚠ 2026-09-21（用户：「点开门店名称时下面的人员名单应该是**门店在职全部的**，
        #   不是谁有数据才显示谁」）：**后端自己把名单补齐**，不再等前端传。
        #   来源 = `store.staff.rosters_by_store()`（组织架构树 + 240 个账号按机构归堆，
        #   两次调用拿到全区，缓存 12 小时）。前端那条 `roster=` 参数仍然认
        #   （老页面/手工指定），但**不再依赖**它。
        roster_source = "param" if roster else ""
        if not roster:
            try:
                from .features.store import staff as _staff
                roster = _staff.rosters_by_store(self.root, self.config_path).get(store) or []
                roster_source = "erp" if roster else ""
            except Exception as e:                             # noqa: BLE001
                roster = []
                roster_source = "none"
                d["roster_error"] = "读云商在册名单失败：%s: %s" % (type(e).__name__, e)
        d["roster_source"] = roster_source
        d["roster_count"] = len(roster or [])
        for who in (roster or []):
            if who and who not in seen:
                seen.append(who)
        # ⚠⚠ **已经存过目标的人也要在名单里**（测试逮到的）：只认"卖过的 + 传来的名单"
        #   的话，一个人这周没卖东西、名单又没带全 ⇒ 他**已保存的目标会从页面上消失**，
        #   合计跟着少算 —— 那是"填了的东西不见了"，比"没填"严重得多。
        for who in saved:
            if who and who not in seen:
                seen.append(who)
        for who in seen:
            actuals = [0] * ncols
            for i, col in enumerate(row.get("people") or []):
                for rec in col:
                    if rec and rec[0] == who:
                        actuals[i] = int(rec[1] or 0)
            tg = list(saved.get(who) or [])[:ncols]
            tg += [0] * (ncols - len(tg))
            members.append({
                "name": who, "actuals": actuals, "targets": tg,
                "rates": [attain_split.rate_of(tg[i], actuals[i]) for i in range(ncols)],
            })
        # 每列拆出来的合计 —— 跟**门店那一列的目标**对一眼就知道拆多还是拆少
        totals = [sum(int((m["targets"] or [0] * ncols)[i] or 0) for m in members)
                  for i in range(ncols)]
        store_tg = [int(x or 0) for x in (row.get("targets") or [])]
        return {"exists": True, "store": row.get("store") or store, "period": period,
                # ⚠ 名单是**从哪来的**要带出去：`erp` = 云商在册名单（含没开单的人）、
                #   `param` = 前端传的、`none` = 读不到（那就只有"卖过的 + 存过目标的"）
                "roster_source": roster_source, "roster_count": len(roster or []),
                # 读不到在册名单时把原因带出去（页面上要说一句，别让人以为"就这几个人"）
                "roster_error": d.get("roster_error") or "",
                "totals": totals, "diff": [totals[i] - (store_tg[i] if i < len(store_tg) else 0)
                                           for i in range(ncols)],
                "start": d.get("start") or "", "end": d.get("end") or "",
                "data_until": d.get("data_until") or "",
                "columns": cols, "weights": d.get("weights") or [],
                "store_targets": row.get("targets") or [],
                "members": members, "saved": bool(saved),
                # ⭐ 数据的**来源与时间**（四·八验收 6）：`kind="mail"` = 这份是
                #   X 店 Y 日那封邮件里的；`None` = 本机自己填的。
                "source": source}

    def attain_split_all(self, roster=None) -> dict:
        """拆分页要显示哪几家店 —— **平台岗全看，区长只看自己管的**（用户 2026-09-19）。

        判定顺序（**从宽到窄，认不出来就退到本店**）：
          ① `profile.type == "platform"`（平台岗）⇒ **全部门店**；
          ② 登录者姓名（`profile.who`，云商账号那个）**跟 `config/managers.yaml`
             里的区长同名** ⇒ 只给**他管的那些店**；
          ③ 其余（普通门店）⇒ 只给**本店**那一行。
        ⚠ 认不出来时**退到本店**，宁可少给也不多给 —— 门店的周度目标不该被别家看到。
        """
        from .features.sales.attain import split as attain_split
        d = self.attain()
        if not d.get("exists"):
            return {"exists": False, "error": d.get("error") or "还没算过"}
        rows = d.get("rows") or []
        scope, scope_label = self._split_scope(rows)
        out = []
        for r in rows:
            name = r.get("store") or ""
            if scope is not None and name not in scope and (r.get("erp_name") or "") not in scope:
                continue
            one = self.attain_split(name, d.get("period") or "", roster)
            if one.get("exists"):
                out.append(one)
        self._split_scope_cache = None
        return {"exists": True, "period": d.get("period") or "",
                "start": d.get("start") or "", "end": d.get("end") or "",
                "data_until": d.get("data_until") or "",
                # ⚠ 用户 2026-09-19：「**平台岗的这个页面不能设置**，只能看店长拆分的目标和达成情况」
                #   ⇒ 平台岗只读（目标显示成数字，不是输入框；保存按钮也不给）。
                # ⚠ 用户 2026-09-20：「**如果是门店账号就可以改目标数，区长和平台账号不能改**」
                #   ⇒ 只有"本店"这一种角色能改（区长看所辖、平台看全部，都只读）。
                "can_edit": (scope_label or "") == "本店",
                "scope": scope_label, "stores": out}

    def _split_scope(self, rows):
        """这一页该显示哪几家店 —— **转发 `role_scope()`**（判据只有那一份）。

        返回 `(名单或 None, 说明)`：`None` = 不筛（平台岗看全部）；
        名单是**门店名集合**（两种写法都在里面）。

        ⚠ 2026-09-21（M17）：这里原来是**第二份**角色判据（自己读配置、自己比账号），
          跟 `role_scope()` 会漂 —— 而"两份定义"是这个项目的老毛病。
          现在只剩一处：`role_scope()`。**判定顺序也跟着改了**：
          **区长优先于平台**（原来的顺序会把区长判成平台岗，见 `role_scope` 的注释）。
        """
        sc = role_scope(self)
        if sc.get("stores") is None:
            return None, sc.get("label") or "全部"
        # ⚠ 门店那一档的说明**保持"本店"**（不换成 role_scope 的长标签）：
        #   页面上那行写的是"范围：本店"，换字会让前端文案跟着动 ——
        #   而前端这一版正在被另一个会话改，能不动就不动。
        label = "本店" if sc.get("role") == ROLE_STORE else (sc.get("label") or "")
        return set(sc.get("stores") or ()), label

    def attain_split_save(self, store: str, period: str, targets: dict) -> dict:
        """把成员目标写进 `out/attain-split.json`（用户定：跟达成那份放一起，方便一起推给区长）。"""
        from .features.sales.attain import split as attain_split
        if not store or not period:
            return {"ok": False, "message": "缺门店或期间"}
        d = self.attain()
        ncols = len(d.get("columns") or []) or 1
        clean = attain_split.set_targets(self.root, store, period, targets, ncols)
        # ⚠⚠ 2026-09-20（用户）：「门店设定好目标有个**保存**，还要有个**发送**按钮，
        #   把拆好的目标发送给区长的邮箱」⇒ **保存只管保存**，发送是另一个按钮
        #   （同一个动作绑两个后果不好用：想先存档、回头再发就不行了）。
        return {"ok": True, "store": store, "period": period, "members": len(clean),
                "message": "已保存（%d 个成员）" % len(clean)}

    def attain_split_send(self, store: str, period: str) -> dict:
        """把这家店拆好的目标**发给区长**（用户要的「发送」按钮）。

        ⚠ 发不出去不是错误（`ok` 仍为真）：目标已经存好了，邮件只是投递方式。
          返回里带 `sent` / `to` / `why`，界面照着说人话。
        """
        from .features.sales.attain import split as attain_split
        if not store or not period:
            return {"ok": False, "message": "缺门店或期间"}
        res = attain_split.send_report(self.root, store, period)
        return {"ok": True, "store": store, "period": period,
                "sent": res.get("sent", 0), "to": res.get("to") or [],
                "why": res.get("why") or "", "failed": res.get("failed") or [],
                "message": ("已发给 %s" % "、".join(res.get("to") or [])
                            if res.get("sent") else "没发出去：%s" % (res.get("why") or "？"))}

    # --------------------------------------------------------- 库存盘点（M16）
    #
    # 用户 2026-09-20：「把库存盘点功能整理成一个模块接入我们这个项目？
    # 导出 excel 这一步接给推送。这个就不注册定时器了」
    #
    # ⚠ 这一组接口是给 `web/inventory.html`（盘点页）用的 —— **那一页不碰云商 token**，
    #   取数一律经这儿走 `features/inventory` → `erp.py`（"页面找数据抓取模块抓取"）。
    # ⚠ 全部**默认要登录**（没进 `SETUP_ALLOW`）—— 跟别的业务接口一样。

    def inventory_ready(self) -> dict:
        """盘点页开工前要知道的**本机事实**：后端有没有云商凭据、这台机器是哪家店。

        ⚠ **一个网络请求都不发** —— 页面一进来就调它，卡在这儿整页就白屏了。
          仓库列表是另一个接口（`/api/inventory/warehouses`），那一个才联网。
        ⚠ 账号名取 **`load_credentials()`（合并链之后的那份）**，不是
          `describe_credentials()` 那份 —— 后者**只看指定文件**，而实际用的是
          回落链（环境变量 > `.secrets/erp.env` > `~/.dsh/...` > 内置）。
          用错的那份，页面上会显示成「（后端没给用户名）」而其实是有的
          （2026-09-20 实测踩到：账号明明配着，盘点半句提示说没有）。
        """
        env = self.erp_env_file()
        info = erp.describe_credentials(env)
        creds = erp.load_credentials(env)
        ok = bool(creds.get("username") and (creds.get("password") or creds.get("token")))
        try:
            cfg = config_io.load_raw(self.config_path) or {}
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "why": "配置读不出来：%s" % e, "store": {}}

        return {
            "ok": ok,
            "why": "" if ok else "这台机器还没配云商账号 —— 去左下角「通用设置 › 云商账号」"
                                 "填一次账号密码（盘点要它去云商拉账面）",
            "username": creds.get("username") or info.get("username", ""),
            "company": creds.get("company") or info.get("company", ""),
            "builtin": bool(info.get("builtin")),
            "has_token": bool(creds.get("token")),
            # 凭据不是从指定文件来的（走了回落链）时说一声 —— 排查时少绕一圈
            "used_from": info.get("used_from", ""),
            "store": {"erp_name": cfg.get("erp_store_name") or "",
                      "store_code": cfg.get("store_code") or "",
                      "marker": cfg.get("marker") or ""},
        }

    def _inv_record(self, res: dict, what: str) -> dict:
        """取数**失败**时记一笔（`kind=inventory-fetch`）。

        ⚠ **只记失败**，成功不记：盘点页每开一次就拉一遍账面，成功也记的话
          运行记录会被它刷满（`KEEP_PER_KIND=200`），真出事时反而看不见。
          失败记下来才有用 —— 健康面板"连着失败三次"那条告警靠的就是它。
        """
        if res.get("ok"):
            return res
        from .storage import runlog
        runlog.record("inventory-fetch", False, why=str(res.get("why") or ""),
                      note="库存盘点 · %s" % what, root=self.root)
        return res

    def inventory_warehouses(self) -> dict:
        """仓库列表（含所属门店）+ **本店默认是哪个仓**。

        ⚠ 这是**联网**的（云商 45 个仓）—— 失败不抛，`ok=False` + `why`，
          页面自己决定是"再点一次刷新"还是"手动选仓"。
        """
        from .features.inventory import book as inv
        got = inv.warehouses(cl=self.erp_client())
        got["default_store_id"] = ""
        try:
            cfg = config_io.load_raw(self.config_path) or {}
        except Exception:                                       # noqa: BLE001
            cfg = {}
        mine = inv.store_for(got.get("warehouses") or [], cfg.get("erp_store_name") or "")
        if mine:
            got["default_store_id"] = str(mine.get("Id") or "")
            got["default_store_name"] = str(mine.get("Name") or "")
        got["erp_store_name"] = cfg.get("erp_store_name") or ""
        return self._inv_record(got, "仓库列表")

    def inventory_book(self, date: str = "", store_id: str = "") -> dict:
        """**账面**（在库 + 在途，串号级）—— 盘点的主数据。

        ⚠ 行**原样**给页面（键就是 `core.js` 读的那套）—— 归一化/uid/在途拆分
          全在页面那唯一一份口径里，后端不参与"算"。
        """
        from .features.inventory import book as inv
        return self._inv_record(inv.book(date, store_id, cl=self.erp_client()), "账面")

    def inventory_transit(self, date: str = "", store_id: str = "") -> dict:
        """在途**兜底**（账面那张表没给在途列时才用）—— 失败只提示、不阻断盘点。"""
        from .features.inventory import book as inv
        return self._inv_record(inv.transit(date, store_id, cl=self.erp_client()), "在途")

    def inventory_index(self, date: str = "") -> dict:
        """**全库串号索引** —— 「表外码」据此说出"是哪个仓的货"。"""
        from .features.inventory import book as inv
        return self._inv_record(inv.index(date, cl=self.erp_client()), "全库索引")

    def inventory_export(self, name: str, data: bytes, *, store: str = "",
                         date: str = "") -> dict:
        """**导出并推送**：把前端那份 xlsx 落盘，再推邮件（附件）+ 企微（汇总）。

        ⚠ **推送失败不是导出失败**：文件已经落在 `out/inventory/` 了，
          返回里两个渠道各带 `state`/`why`，页面照着说人话
          （跟"目标存好了但邮件没发出去"同一个道理）。
        ⚠ 文案**从这份 xlsx 的「汇总」表里读**（`push.read_summary`）——
          后端不另算一份口径，否则"推出去的"和"门店看到的那份"迟早对不上。
        """
        from .features.inventory import push as inv_push
        try:
            path = inv_push.save_export(self.root, name, data)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        cfg = {}
        try:
            cfg = config_io.load_raw(self.config_path) or {}
        except Exception:                                       # noqa: BLE001
            cfg = {}
        # C7：推送是**外部可见动作** —— 把"谁点的"一起记进运行记录
        res = inv_push.push(path, cfg=cfg, root=self.root, store=store, date=date,
                            who=role_scope(self).get("who") or role_scope(self).get("account") or "")
        return {"ok": True, "path": str(path), "name": path.name,
                "head": res["head"], "lines": res["lines"],
                "mail": res["mail"], "wecom": res["wecom"]}

    def pos(self) -> dict:
        """「POS 合规」tab 的数据 —— **纯读盘，一个网络请求都不发**。

        依据就是上面那条注释（概览页 30 秒刷一次，不能每次都戳网）。
        POS 分数由 `python -m src.cli pos`（或日常流程）算好落 `out/pos-<年>.json`，
        看板只负责把它读出来。**看板不做计算，也不登任何系统。**
        """
        files = sorted(self.out_dir.glob("pos-[0-9][0-9][0-9][0-9].json"))
        if not files:
            # ⚠ 以前这里只有一句"还没算过" —— 于是"今天没数据"和"今天抓失败了"
            #   在门店眼里**一模一样**（M14 的 C 项要解决的正是这个）。
            #   现在把判据的**病因**直接说出来。
            st = app_data.data_state(self.root)
            bad = [x for x in st["sources"] if x["state"] != app_data.OK]
            hint = "还没算过 —— 先抓数（dump）再算分（pos）"
            if bad:
                hint = "%s：%s" % (bad[0]["state_label"], bad[0]["why"])
            return {"exists": False, "rows": [], "hint": hint,
                    "data_state": app_data.brief(st)}
        newest = files[-1]
        try:
            d = json.loads(newest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return {"exists": False, "rows": [], "error": "%s：%s" % (newest.name, e)}
        d["exists"] = True
        d["file"] = newest.name
        return d

    def _selfheal_runner_script(self) -> None:
        """`run.bat` 过时就按当前模板重建 —— **每进程只试一次**。

        ## 为什么必须挂在这儿

        `run.bat` / `run-now.bat` 是**安装时生成、不进版本库**的，
        自更新**不会重写它们**。而 2.0.0 的日常流程换了命令
        （`check` → `daily`：先抓华为当月写进库，再做两个分析）——
        门店那份 bat 不改的话，`check` 会每天以「库不新鲜」失败。

        `schedule.refresh_runner_scripts()` 本来就是干这个的，
        **但它挂在 `GET /api/schedule` 上，而前端从不 GET 那个路径**
        （只有 POST 注册 / POST 运行 / DELETE 删除）—— 从来没跑过。
        挂到概览页上：概览是**每次开界面都会拉**的，于是它真的会跑。

        ## 为什么只试一次

        概览 30 秒轮询一次，`refresh_runner_scripts` 每次都要读一遍 `run.bat`。
        一次文件读不算什么，但**重建**只在升级后发生一次 ——
        用一个实例标志把它收敛掉，顺带避免"正跑着任务时去覆盖 bat"
        （Windows 上 cmd 正在执行的 .bat 未必能覆盖掉）。
        失败也不再重试：真失败了，`run_check.py` 里那个垫片还兜着。
        """
        if getattr(self, "_runner_healed", False):
            return
        self._runner_healed = True
        try:
            rebuilt = schedule.refresh_runner_scripts(self.root, self.config)
        except Exception:                       # noqa: BLE001 - 自愈失败不该拖垮概览
            return
        if rebuilt:
            # 让界面能提一句"启动脚本已更新" —— 门店下次看到命令变了不会懵
            self._runner_rebuilt = True

    def _profile_with_who(self, cfg) -> dict:
        """门店画像 + **登录这台机器的人叫什么**。

        ⚠ 「谁登的」不在配置里（配置只记"哪家店"），它在
          `.secrets/erp-store.env` 的 `ERP_WHO` 里 —— 所以**不能塞进
          `store_profile()`**：那是个纯函数（只吃配置 + 名单），
          让它去读凭据文件会把"画像"和"凭据"搅在一起，也没法单测了。
        """
        prof = config_io.store_profile(config_io.pick(cfg), self.root)
        try:
            prof["who"] = describe_store_credentials(store_path(self)).get("who") or ""
        except OSError:
            prof["who"] = ""
        return prof

    def _staff_from_inbox(self, scope: dict) -> dict:
        """**区长 / 平台**那一页读的东西：各店**发过来的**人员状态表（只读）。

        来源是每天那趟**上报包**里的 `staff` 表（门店侧 `report.py` 打包时带上）——
        ⚠ 不是现场去戳云商（那只能查到本机那家店），也不是让区长自己维护一份。
        每张表都带**来源与时间**（四·八验收 6）：哪家店、哪天的邮件。
        """
        from .app import report_inbox as inbox_mod
        rows = inbox_mod.tables_of(self.root, "staff")
        out = []
        for code, one in rows:
            name = str(one.get("store_name") or "")
            if not scope_store_ok(scope, name) and not scope_store_ok(scope, code):
                continue
            people = one.get("rows") or []
            out.append({
                "store_code": code, "store_name": name,
                "report_date": one.get("report_date") or "",
                "imported_at": one.get("imported_at") or "",
                "mail_subject": one.get("subject") or "",
                "people": people,
                "count": len(people),
                "active_count": sum(1 for p in people if p.get("active")),
            })
        out.sort(key=lambda x: x.get("store_name") or x.get("store_code") or "")
        return {"ok": True, "readonly": True, "from_mail": True,
                "scope": {"role": scope.get("role"), "label": scope.get("label"),
                          "count": len(out)},
                "stores": out,
                "count": sum(x["count"] for x in out),
                "active_count": sum(x["active_count"] for x in out),
                "hint": ("这些是**门店发过来的**人员状态表（每天那趟上报包里带的）—— "
                         "只有门店能改，这边只读。还没收到过的话，等那家店跑完一天。")}

    def _stores_cards(self, scope: dict) -> dict:
        """**数据交换 › 每店一张卡**的数据（M20）—— 范围按角色，**本机收信库**是唯一来源。

        ⚠ 三件必须一起看的事：
          ① **范围用 `role_scope()`**（区长 = 所辖、平台 = 全部）——
             库里是"所有发过信的店"，不筛就是"区长看到别区"；
          ② **按名单出卡，不是按库**：没上报过的店也要有卡（`known=False`），
             否则"哪几家没报"就看不出来 —— 而那正是这个页面最该回答的问题；
          ③ **收信失败时保持原样**：这里只读库、绝不因为"这次没收上来"清空任何东西。
        """
        from .app import report_inbox as inbox_mod
        roster = []
        try:
            roster = config_io.stores_table(self.root)
        except Exception:                                      # noqa: BLE001
            roster = []
        pairs = []                       # [(门店码, 店名)]
        for r in roster:
            code = str(r.get("huawei_code") or "").strip()
            if not code:
                continue
            name = str(r.get("erp_name") or "").strip()
            if not scope_store_ok(scope, name):
                continue
            pairs.append((code, name))
        codes = [c for c, _n in pairs]
        cards = inbox_mod.cards(self.root, codes)
        by_code = {c["store_code"]: c for c in cards}
        # ⚠ **还按店名兜一次**：名单里那家店的「华为编码」可能是空的 / 写错了，
        #   而包里带着店名 —— 只认编码的话表现是"收信库里有数据，卡片却说从来没收到过"，
        #   而那种错**只能靠人肉比对**才发现（名单和邮件两边都"看着对"）。
        by_name = {}
        for c in cards:
            nm = str(c.get("store_name") or "").strip()
            if nm:
                by_name.setdefault(nm, c)
        stores = []
        for code, name in pairs:
            hit = by_code.get(code) or by_name.get(name)
            card = dict(hit or {"store_code": code, "known": False,
                                "why": "从来没收到过这家店的上报", "stale_days": None})
            card["store_code"] = code           # 明细接口按它找回来，得统一
            if not card.get("store_name"):
                card["store_name"] = name
            stores.append(card)
        # ⚠ 库里还有**名单之外的店**（换了店名 / 名单没更新）—— 也得能看见，
        #   不然"有数据但页面上没有"是最难查的一种。平台岗才列（区长范围有限）。
        extra = []
        if scope.get("stores") is None:
            known = set(codes)
            for card in inbox_mod.cards(self.root):
                if card["store_code"] not in known:
                    extra.append(card)
        on, why = inbox_mod.configured(None, self.root)
        return {"ok": True, "today": time.strftime("%Y-%m-%d"),
                "scope": {"role": scope.get("role"), "label": scope.get("label"),
                          "count": len(stores)},
                "stores": stores + extra,
                "extra": extra,
                "inbox": {"configured": bool(on), "why": "" if on else why,
                          "skips": inbox_mod.skips(self.root, 20)}}

    def overview(self) -> dict:
        cfg = config_io.load_raw(self.config_path)
        latest = manager.latest()
        self._selfheal_runner_script()
        return {
            "root": str(self.root),
            "version": version.VERSION,
            "build": version.build_id(),
            # 只看缓存，**一个网络请求都不发** —— 概览页 30 秒刷一次，
            # 不能每次都去戳 GitHub。主动查是后台线程的活（每天一次）。
            # 用 cached() 而不是 read_cache()：缓存里的 has_update 是按
            # **当时的版本**算的，升级之后要重算，否则一直说"有新版本"。
            "update": selfupdate.cached(self.root, version.VERSION),
            "config": {"path": self.config, "values": config_io.pick(cfg)},
            # 这台机器是哪家店、要不要玲珑 —— 前端据此**藏菜单**
            # （合作店不显示「玲珑授权」「报量查询」，整个「五项合规」都不显示）
            "profile": self._profile_with_who(cfg),
            # ⭐ **身份与权限**（M17，2026-09-21）：`role` / `stores`（范围）/ `can`（能写什么）。
            # ⚠ 前端**从这一份渲染**（甲方案：后端下发 pages/can，前端只渲染）——
            #   别在前端再写一套"按 type 猜"（那就是第二份判据，迟早跟后端漂）。
            # ⚠ 这一项**不是安全边界**：真正的拦截在每个 `/api/*` 里（`role_scope` + `forbid`）。
            "role": role_scope(self),
            # 「账号设置」页要显示**云商账号状态与信息**（登录账号 / 姓名 / 公司 /
            #   登录态）。⚠ 这是**读本地凭据文件**，不是发请求 —— 概览页 30 秒
            #   刷一次，不能每次都去戳云商。密码那类敏感字段 `describe_*` 本来就不回显。
            "store_account": describe_store_credentials(store_path(self)),
            # 「上次升级没走完」——**必须让人看见**（阶段 1.4d）。
            # ⚠ 中断的升级会让程序静默跑在"一半新一半旧"的代码上，
            #   所以只要 journal 不是终态，概览页就挂横幅 + 两个修复按钮。
            "update_pending": selfupdate.pending(self.root),
            # 「这份数据能不能算」—— 五种状态（M14 / 阶段 3.3）。
            # ⚠ 只在**不是全 ok** 时前端才挂横幅：没事别老挂一条黄条。
            #   实测成本 ~29ms（概览页 30 秒刷一次，够便宜）。
            "data_state": self._data_state_with_dismiss(),
            # **启动自检**（用户 2026-09-19 的启动流程）——
            # 前端只用 `blocking`：硬门槛没过就挂一条红横幅（修复页那种）。
            # ⚠ 后端结果缓存 15 秒（见 `App.boot_state`），不会每次轮询都重算。
            "boot": self.boot_state(),
            "session": self.session_info(),
            "schedule": schedule.status(self.root),
            # 老定时任务要不要**主动弹一次**（用户 2026-09-17 选的方案 C）。
            # ⚠ 判据全在后端：① 系统里真挂着老名字的任务 ② 这一版还没弹过。
            #   前端不自己记"弹过没" —— 那种状态放前端一定会漂。
            "legacy_prompt": schedule.legacy_prompt_pending(self.root),
            # ⚠ 2026-09-21 晚：这里的 `"automation"` 字段**删了** ——
            #   那个设置 2026-09-20 就取消了（用户：「这些去掉吧，也不用设置了」），
            #   字段一直留着给老前端读；现在连"手动整批"都没了，
            #   跑什么**只由注册表 + 每一步自己的时刻**决定，没有任何可设的余地。
            #   （前端早就不读它了，见 `app.js` 里那段"自动化勾选不再有接线"。）
            # ⭐ 左下角那个「定时」小标要显示**真实的下一次时间**（用户 2026-09-21：
            #   「左下角未设定时改成真实时间吧」）。
            #   ⚠ 它原来读的是 `schedule.installed`（**Windows 计划任务**），
            #     而那个兜底 2026-09-20 已经整个撤掉了 ⇒ 永远显示「未设定时」，
            #     明明内置定时器排着 6 件事。⇒ 改读**计时模块**。
            #   ⚠ 只取 `next_run()`（读注册表 + 唤醒设置，不碰库）——
            #     `timer.now()` 会查 runlog，而总览刷新很勤，别把库拖进来。
            "timer": {"next_run": timer.next_run(self.root),
                      # ⚠ 左下角那个小标显示的是**到点真会跑的那几步**
                      #   （不是"下一次任意步骤" —— 自动更新每小时都跑，
                      #    显示它的话小标永远在"一小时内"，门店看不出什么）。
                      #   ⚠ 名单见 `_shown_step_cmds()`：**别按 `default` 筛**。
                      "next_daily": timer.next_run(self.root,
                                                   cmds=_shown_step_cmds())},
            # "启动脚本这次被重建过" —— 界面可以据此提一句，
            # 免得门店发现定时任务的命令悄悄变了会懵
            "runner_rebuilt": self._runner_rebuilt,
            "reports": list_reports(self.out_dir),
            "run": latest.snapshot(0) if latest else None,
            # 「更新了，这一版要做什么」—— 每版只弹一次（记在 .secrets/whatsnew.json，
            # 那是自更新不碰的地方，所以跨版本有效）。
            # ⚠ 内容在 `src/whatsnew.py` 里、跟着代码走 —— 不能读 `发布说明.md`：
            #   那个文件**不在 git 里**，自更新的门店拿到的永远是当初拷包那一版。
            "whatsnew": whatsnew.pending(self.root, version.VERSION),
            # 升级记录 —— 「你什么时候升的级、从哪一版升上来的」。
            # 这类"门店自己用、没人管"的工具上很值：报上来的现象经常
            # 跟"它其实还在跑半年前的版本"有关。
            "upgrades": upgrade.history(self.root),
            "running": bool(manager.current()),
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "cbg-reconcile"
    app: App = None            # 由 serve() 注入

    # ------------------------------------------------------------- 基础设施
    def log_message(self, fmt, *args):           # 安静点，别把控制台刷满
        pass

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ⚠ HTTP 状态码的约定：
    #   200 = 请求被正常处理了（**哪怕结果是 ok:false**，比如"注册计划任务失败"）
    #   4xx = 请求本身有问题（参数不对、路径不合法、字段不允许改）
    #   5xx = 服务端真出 bug 了
    # 把"业务失败"当 500 返回，会让前端的通用错误分支盖掉 message，
    # 用户只看到一句"HTTP 500"，完全不知道发生了什么。
    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ValueError("请求体不是合法 JSON") from None

    #: 盘点导出的 xlsx 上限。一个仓的账面清单 **不到 1MB**（7 张表、最多几万行）——
    #: 20MB 是给"前端哪天多塞了东西"留的余量，同时也是**不让人往这台机器灌大文件**的闸门。
    MAX_UPLOAD = 20 * 1024 * 1024

    def _read_bytes(self, limit: int = 0) -> bytes:
        """读**原始请求体**（盘点导出那份 xlsx 是二进制，不是 JSON）。

        ⚠ 超限**直接抛**（返回 400），不"读一半"——半份 xlsx 会一路走到
          `save_export` 才因为不是 zip 而失败，那时人已经以为发出去了。
        """
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return b""
        if limit and n > limit:
            raise ValueError("请求体 %d 字节，超过上限 %d 字节" % (n, limit))
        return self.rfile.read(n)

    def _host_ok(self) -> bool:
        """防 DNS rebinding：只认本机 Host。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _write_origin_ok(self) -> bool:
        """浏览器写请求只收同源；无来源头的本机脚本仍可调用。"""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            sent = urlparse(origin)
            local = urlparse("http://" + (self.headers.get("Host") or ""))
            return (sent.scheme == "http" and sent.hostname == local.hostname
                    and sent.port == local.port and not sent.username
                    and not sent.password and not sent.path and not sent.query)
        except ValueError:
            return False

    # ---------------------------------------------------------------- 路由
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        u = urlparse(self.path)
        path, query = u.path, parse_qs(u.query)

        if path.startswith("/api/") and not self._host_ok():
            return self._json({"error": "只接受来自本机的请求"}, 403)
        if path.startswith("/api/") and method in ("POST", "PUT", "DELETE") \
                and not self._write_origin_ok():
            return self._json({"error": "只接受本机页面的写请求"}, 403)

        try:
            if path.startswith("/api/"):
                return self._api(method, path, query)
            return self._static(path)
        except (ValueError, KeyError) as e:
            return self._json({"error": f"参数不对：{e}"}, 400)
        except (CbgAuthError, CbgError, RuntimeError) as e:
            return self._json({"error": str(e)}, 409)
        except SystemExit as e:
            # ⚠ `SystemExit` 是 **BaseException**，不接的话它会**穿过**下面那层
            #   `except Exception`，把连接直接掐断 —— 界面只看到"失败"两个字，
            #   连错误信息都没有（实测踩到：`load_config` 找不到配置文件时）。
            #   `load_config` / argparse 这些地方都会抛它。
            return self._json({"error": str(e) or "启动参数不对"}, 500)
        except Exception as e:                       # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------------------------------------------------------------- 静态
    def _static(self, path: str):
        rel = "index.html" if path in ("/", "") else unquote(path).lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        # ⚠ 缓存头**在 `_send()` 里统一发**（`Cache-Control: no-store`）——
        #   静态文件也不许让浏览器缓存：前端是"无构建步骤、改完刷新即可"，
        #   拿了旧的会表现成"改完了、刷新了、还是老样子"。
        #   ⚠⚠ 别在这儿自己再传一个缓存头：`_send()` **没有** `extra` 参数，
        #     多传一个 kwarg 会让**每个 .js/.css 请求都 500**（2026-09-19 我真踩了，
        #     用户看到的就是"表头还是没显示"——其实是静态文件整个取不到）。
        return self._send(200, target.read_bytes(), ctype)

    # ------------------------------------------------------------------ API
    def _api(self, method: str, path: str, query: dict):
        app = self.app

        # 探活与停止：start.bat / stop.bat 靠这两个判断后台服务在不在
        if path == "/api/health" and method == "GET":
            return self._json({"ok": True, "app": "cbg-reconcile", "pid": os.getpid(),
                               "version": version.VERSION,
                               "build": version.build_id(),
                               "port": self.server.server_address[1] if self.server else 0,
                               "started_at": app.started_at})

        # 启动自检的结果（用户 2026-09-19 定的启动流程）——
        # ⚠ 放行给登录页：进不去的时候，这条正是"为什么进不去"的答案。
        if path == "/api/boot" and method == "GET":
            return self._json(app.boot_state())

        # 这个身份的权限（**每个接口共用一份判据** —— 见 `role_scope`）。
        # ⚠ 放在探活 / 启动自检那两条 early-return **之后**：那两条登录页也要调，
        #   没必要为它们读一遍配置和凭据（实测这一算 ~11ms，概览页 30 秒轮一次）。
        # ⚠ **一次请求算一次、传下去** —— 别在各个分支里各算各的：漏一处就是
        #   "这个接口没人管"，而那正是这次要堵的东西。
        scope = role_scope(app)

        # ---- 登录门禁：没「登录好」之前，除了登录相关的一律 403
        #
        # 用户 2026-09-18：「不登录或者登录失败**不给用**」——
        # 所以这是**后端**挡，不是前端画个遮罩：前端挡的话，别的程序照样能调接口。
        if path.startswith("/api/") and not path.startswith(SETUP_ALLOW):
            st = setup_state(app)
            if not st["ready"]:
                return self._json({"error": "还没登录好，先用不了",
                                   "need": st["need"], "setup": st}, 403)

        # 「重新登录」= **清除门店登录数据**（用户 2026-09-19：
        #   「重新登陆这个操作应该是清除掉门店登录数据的」）。
        # ⚠ 只清**门店账号**那个文件 —— 公司账号是内置的，玲珑会话是另一回事，
        #   都不该被这个动作带走。
        if path == "/api/store-account/logout" and method == "POST":
            f = store_path(app)
            try:
                if f.exists():
                    f.unlink()
            except OSError as e:
                return self._json({"ok": False,
                                   "error": "删不掉 %s：%s" % (f, e)}, 400)

            # ⚠⚠ **光删凭据文件不够 —— "这台机器是哪家店"还写在 config 里。**
            #
            #   实测（2026-09-19，用户报的）：点「重新登录 / 换账号」→ 回到登录页 →
            #   **不登录直接走** → 再打开控制台，**还是能进主界面**。
            #
            #   根因：门禁放行的判据里，除了"门店账号登过"还有两条**兜底**
            #   （`setup_state`）：① `platform: true` 直接放行；② `erp_store_name`
            #   + `store_code` 都在就算"门店配置已就绪"。清掉凭据文件之后这两条
            #   照样成立 ⇒ **退出登录等于没退**。
            #   （开发机上真踩到：`config/store-SCN231409.yaml` 里留着
            #    `platform: true` + `erp_branch_id`，于是怎么点都能进。）
            #
            #   ⇒ 退出登录 = 把**身份**一起清掉。
            # ⚠ **只清这五个身份字段**，别动时区 / 抓取参数 / 邮件 / 企微 ——
            #   那些是门店自己配的，不该被一次"退出登录"带走。
            # ⚠ `platform` 要写成 `""` 不是 `False`：`store_profile` 是按字符串判的
            #   （`in ("1","true","yes")`），空串进不去那个集合。
            identity = ("erp_store_name", "store_code", "marker",
                        "platform", "erp_branch_id")
            try:
                config_io.update(app.config_path, {k: "" for k in identity})
            except (OSError, ValueError) as e:
                return self._json({"ok": False,
                                   "error": "门店身份清不掉（%s）：%s"
                                            % (app.config_path, e)}, 400)
            return self._json({"ok": True, "identity_cleared": list(identity),
                               **describe_store_credentials(f)})

        if path == "/api/staff" and method == "GET":
            # ⭐ 2026-09-21：**区长/平台看到的是"门店发来的状态表"**，不是现场去查云商
            #   （用户：「区长/平台不能改别家店的这份名单，**读取门店发送的状态表**吧」）。
            #   ⚠ 现场查那条路（`staff_state`）**只有本机那家店** —— 区长机器上查出来的
            #     是它自己（虚拟平台岗）的名单，拿它当"所辖各店的人"是错的。
            if scope.get("role") != ROLE_STORE:
                return self._json(app._staff_from_inbox(scope))
            return self._json(staff_state(app))

        if path == "/api/staff" and method in ("PUT", "POST"):
            # ⚠ 人员名单**只有门店能改，而且只能改本店** —— C2 当天改过（见 `_can_for`）。
            body = self._read_json()
            ex = body.get("excluded")
            if not isinstance(ex, list):
                return self._json({"error": "excluded 得是一个数组"}, 400)
            # C2：人员设置**三种角色都能写**（区长改所辖、平台改全部）。
            # ⚠ 现在这张表还是"本机那家店"的，跨店那一半在 M20（多店视图）——
            #   所以这里只做声明 + 留痕，不做门店维度的拦截。
            if not scope["can"].get("staff.write"):
                return self._json(forbid(scope, "改人员设置", "这个身份不能改人员设置"), 403)
            app_staff.save_excluded(app.root, ex)
            audit(app, "人员设置", scope, rows=len(ex))
            # ⭐ **保存即上报**（用户 2026-09-21：「两者。保存即发。连点几次保存合并成一封。
            #   **那还是 5 分钟吧**。完整上报包还是带着人员名单」）——
            #   过一个窗口（默认 5 分钟）把那趟上报发出去（发的是**完整上报包**，
            #   人员表照旧在里面），窗口里再点保存就**把时间往后挪** ⇒ 一串点击只发一封。
            # ⚠ **只有门店才触发**：上报是"门店 → 区长/中台"这件事（M18），
            #   区长/平台机器上跑它只会把本机那份发去中台 —— 没意义还吵。
            #   （区长/平台看到的那份本来就是**门店发来的**，页面上也只读。）
            # ⚠ 具体怎么发：**登记一条一次性任务**（`timer.register_once`，落盘），
            #   由定时器心跳到点派发 `daily --steps report` —— 所以关掉浏览器照样发、
            #   服务重启也不丢、运行日志里看得到那一趟。见 `staff.schedule_report`。
            #   失败也不影响这次保存（保存早就落盘了），包里发不出去会留在 pending/ 下次补。
            sched = {}
            if scope.get("role") == ROLE_STORE:
                sched = app_staff.schedule_report(
                    app.root, who=scope.get("who") or scope.get("account") or "")
            return self._json({"ok": True, "saved": True,
                               "report_in": sched.get("in", 0), **staff_state(app)})

        if path == "/api/setup/preview" and method == "POST":
            body = self._read_json()
            return self._json(set_preview(app, bool(body.get("on"))))

        if path == "/api/setup" and method == "GET":
            return self._json(setup_state(app))

        if path == "/api/shutdown" and method == "POST":
            if app.server:
                threading.Thread(target=app.server.shutdown, daemon=True).start()
            return self._json({"ok": True, "message": "正在停止"})

        # ---- 门店云商账号（只用来认"这台机器是哪家店"）
        if path == "/api/store-account" and method == "GET":
            return self._json(describe_store_credentials(store_path(app)))

        if path == "/api/store-account" and method in ("PUT", "POST"):
            body = self._read_json()
            username = (body.get("username") or "").strip() or None
            company = (body.get("company") or "").strip() or None
            password = body.get("password") or None          # 空串 = 不改
            if username is None and password is None and company is None:
                return self._json({"error": "什么都没改"}, 400)
            old = describe_store_credentials(store_path(app))
            # ⚠ 换了账号/密码就**作废旧 token** —— 旧 token 属于旧账号，
            #   留着的话"改了账号却还在用上一个账号的登录态"。
            changed = ((username and username != old["username"])
                       or (company and company != old["company"]))
            save_store_credentials(store_path(app), username=username,
                                   password=password, company=company,
                                   clear_token=bool(changed))
            return self._json({"ok": True, "saved": True,
                               **describe_store_credentials(store_path(app))})

        if path == "/api/store-account/lookup" and method == "POST":
            body = self._read_json()
            return self._json(store_lookup(
                app, str(body.get("code") or "").strip(),
                str(body.get("username") or ""), str(body.get("password") or ""),
                str(body.get("company") or "")))

        if path == "/api/status" and method == "GET":
            return self._json(app.status_brief())

        if path == "/api/overview" and method == "GET":
            return self._json(app.overview())

        if path == "/api/pos" and method == "GET":
            return self._json(app.pos())

        # ---- 库存盘点（M16）—— 盘点页 `web/inventory.html` 专用的那一组
        #
        # ⚠ 盘点页**不碰云商 token**：这几个接口就是它跟云商之间的全部通道。
        # ⚠ 业务失败一律 **200 + ok:false + why**（不是 4xx/5xx）——
        #   前端 `api()` 认 `error`/`message`，只有 why 的话界面上会变成干巴巴的「HTTP 400」。
        if path == "/api/inventory/ready" and method == "GET":
            return self._json(app.inventory_ready())
        if path == "/api/inventory/warehouses" and method == "GET":
            return self._json(app.inventory_warehouses())
        if path == "/api/inventory/book" and method == "POST":
            body = self._read_json()
            return self._json(app.inventory_book(str(body.get("date") or ""),
                                                 str(body.get("storeId") or "")))
        if path == "/api/inventory/transit" and method == "POST":
            body = self._read_json()
            return self._json(app.inventory_transit(str(body.get("date") or ""),
                                                    str(body.get("storeId") or "")))
        if path == "/api/inventory/index" and method == "POST":
            body = self._read_json()
            return self._json(app.inventory_index(str(body.get("date") or "")))
        # 「导出并推送」：**请求体就是 xlsx 本身**（不是 JSON 信封）——
        # 少一层 base64 就少 33% 的体积，也少一个"编码错了"的地方。
        # 文件名叫什么、哪个店哪一天，走 query 传。
        if path == "/api/inventory/export" and method == "POST":
            try:
                data = self._read_bytes(self.MAX_UPLOAD)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json(app.inventory_export(
                (query.get("name") or [""])[0], data,
                store=(query.get("store") or [""])[0],
                date=(query.get("date") or [""])[0]))

        # ---- 壁纸（用户 2026-09-22）：**只管文件**，不提供 /api/theme。
        #      选中哪张只在浏览器 localStorage（`cbg-wallpaper`）；主题切换仍是
        #      body[data-theme] + cbg-theme（执行规范 7.2）。
        if path == "/api/wallpaper" and method == "GET":
            return self._json({
                "ok": True,
                "items": theme.wallpapers(app.root),
                "exts": list(theme.WALLPAPER_EXTS),
                "max_bytes": theme.MAX_WALLPAPER,
                "photo_theme": theme.PHOTO_THEME,
            })
        if path == "/api/wallpaper" and method == "POST":
            name = (query.get("name") or [""])[0]
            # ⚠ 超限时**先把请求体读完再回 400** —— 不读的话客户端还在 send，
            #   会拿到 BrokenPipe（测试里真炸过；浏览器上则是"上传失败"无详情）。
            n = int(self.headers.get("Content-Length") or 0)
            if n > theme.MAX_WALLPAPER:
                left = n
                while left > 0:
                    chunk = self.rfile.read(min(left, 65536))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self._json({"ok": False, "error": "图片 %d 字节，超过上限 %d 字节"
                                                          % (n, theme.MAX_WALLPAPER)}, 400)
            try:
                data = self._read_bytes(theme.MAX_WALLPAPER)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            try:
                saved = theme.save_wallpaper(app.root, name, data)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            audit(app, "上传壁纸 %s" % saved["name"], scope, bytes=saved["bytes"])
            return self._json(saved)
        if path == "/api/wallpaper" and method == "DELETE":
            name = (query.get("name") or [""])[0]
            try:
                res = theme.delete_wallpaper(app.root, name)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            if res.get("ok"):
                audit(app, "删除壁纸 %s" % res.get("name"), scope)
            return self._json(res, 200 if res.get("ok") else 404)

        if path == "/api/notify-pref" and method == "GET":
            from .modules.notify import prefs as push_prefs
            return self._json({"ok": True, "prefs": push_prefs.all_prefs(app.root)})

        if path == "/api/notify-pref" and method == "PUT":
            from .modules.notify import prefs as push_prefs
            body = self._read_json() or {}
            key = str(body.get("key") or "").strip()
            on = bool(body.get("enabled"))
            try:
                if key.startswith("plat:"):
                    prefs = push_prefs.set_platform(key[5:], on, app.root)
                else:
                    prefs = push_prefs.set_enabled(key, on, app.root)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "prefs": prefs,
                               "message": "%s：推送已%s"
                                          % (prefs[key]["label"],
                                             "打开" if prefs[key]["enabled"] else "关闭")})

        if path == "/api/attain" and method == "GET":
            # ⚠ 过滤在 `App.attain()` 里做（`filter_attain_rows` → `role_scope()`）——
            #   落盘那份含全部门店，读的时候必须按**当前身份**再过一遍。
            return self._json(app.attain())

        # **导出为 Excel**（用户 2026-09-21：「区长账号有导出为 excel 功能」）——
        # 生成在**推送模块**（`modules.notify.export_xlsx`），这里只把"谁能导、
        # 导的是哪份数据"接上。⚠ 每个接口都要有一行 `forbid`（坑 18）。
        if path == "/api/attain/export" and method == "POST":
            if not scope["can"].get("attain.export"):
                return self._json(forbid(scope, "导出为 Excel",
                                         "这个身份不能导出达成数据"), 403)
            body = self._read_json()
            res = app.attain_export(who=scope.get("who") or scope.get("account") or "",
                                    name=body.get("name") or "")
            # 导出**留痕**在 `notify.export_xlsx` 里（kind = `export:attain`，
            # 带"谁导的"）—— 不再走 `audit()`，那是"改本机配置"那条路的记录。
            if not res.get("ok"):
                # ⚠ 失败时**必须补一个 `error`** —— 前端 `api()` 读的是
                #   `data.error || data.message`，只给 `why` 的话页面上会显示成
                #   干巴巴的「HTTP 400」，而"为什么失败"恰恰是这次唯一有用的信息
                #   （磁盘满 / 名字非法 / 还没算过…）。
                res = dict(res, error=res.get("why") or "导出失败")
            return self._json(res, 200 if res.get("ok") else 400)

        # **月度生意计划**（M22，2026-09-21）—— 纯读盘；范围在 `App.plan()` 里按身份滤。
        # ⚠ 每个接口都要有一行 `forbid`（坑 18）：导出的判据跟达成同一个（`not is_store`）。
        # **增值 · 防护膜达成情况**（2026-09-22）—— 本地库现算；范围在 `App.film()` 里按身份滤。
        if path == "/api/film" and method == "GET":
            return self._json(app.film())
        if path == "/api/film/export" and method == "POST":
            if not scope["can"].get("film.export"):
                return self._json(forbid(scope, "导出为 Excel",
                                         "门店账号不导出，要看明细就在页面上看"), 403)
            res = app.film_export(who=scope.get("who") or scope.get("account") or "",
                                  name=(self._read_json() or {}).get("name") or "")
            return self._json(res)
        # **增值 · 无忧会员权益**（2026-09-22）—— 本地库现算；范围在 App.benefit() 滤。
        if path == "/api/benefit" and method == "GET":
            return self._json(app.benefit())
        if path == "/api/benefit/export" and method == "POST":
            if not scope["can"].get("benefit.export"):
                return self._json(forbid(scope, "导出为 Excel",
                                         "门店账号不导出，要看明细就在页面上看"), 403)
            res = app.benefit_export(who=scope.get("who") or scope.get("account") or "",
                                     name=(self._read_json() or {}).get("name") or "")
            return self._json(res)
        # **小工具 · 权益领取**（2026-09-22）—— 活动只读配置；待领滤店；状态写本机。
        if path == "/api/claim/activities" and method == "GET":
            return self._json(app.claim_activities())
        if path == "/api/claim/pending" and method == "GET":
            return self._json(app.claim_pending())
        if path == "/api/claim/status" and method == "POST":
            body = self._read_json() or {}
            res = app.claim_status_set(
                str(body.get("key") or ""),
                str(body.get("status") or ""),
                by=str(body.get("by") or ""),
                note=str(body.get("note") or ""),
            )
            if not res.get("ok"):
                res = dict(res, error=res.get("why") or "更新状态失败")
                # ⚠ 越权用 403（跟 forbid 同一档），参数/业务失败仍 400
                code = 403 if res.get("forbidden") else 400
                return self._json(res, code)
            return self._json(res)
        # **在线领取直提**（不跳转华为页）：先 query 预览，再 submit 真领
        if path == "/api/claim/query" and method == "POST":
            body = self._read_json() or {}
            res = app.claim_query(str(body.get("sn") or ""),
                                  str(body.get("activity_id") or ""))
            if not res.get("ok"):
                res = dict(res, error=res.get("why") or "查询失败")
                code = 403 if res.get("forbidden") else 400
                return self._json(res, code)
            return self._json(res)
        if path == "/api/claim/submit" and method == "POST":
            body = self._read_json() or {}
            res = app.claim_submit_online(
                str(body.get("sn") or ""),
                str(body.get("activity_id") or ""),
                status_key=str(body.get("status_key") or ""),
            )
            # ⚠「已经领取过」要 **HTTP 200** —— 否则前端 api() 会 throw，
            #   走不到「自动标已领 / 确认关闭」分支（2026-09-23 截图就是这个）。
            if res.get("already_claimed"):
                res = dict(res, error=None)
                return self._json(res, 200)
            if not res.get("ok"):
                res = dict(res, error=res.get("why") or "领取失败")
                code = 403 if res.get("forbidden") else 400
                return self._json(res, code)
            return self._json(res)
        # **小工具 · 串号追踪**（2026-09-23）—— 86码/SN → 库存+销售全程
        if path == "/api/sn-trace" and method == "GET":
            code = (query.get("code") or [""])[0]
            return self._json(app.sn_trace(code))
        if path == "/api/plan" and method == "GET":
            return self._json(app.plan())
        if path == "/api/plan/export" and method == "POST":
            if not scope["can"].get("plan.export"):
                return self._json(forbid(scope, "导出为 Excel",
                                         "这个身份不能导出月度生意计划"), 403)
            body = self._read_json()
            res = app.plan_export(who=scope.get("who") or scope.get("account") or "",
                                  name=body.get("name") or "")
            if not res.get("ok"):
                res = dict(res, error=res.get("why") or "导出失败")
            return self._json(res, 200 if res.get("ok") else 400)
        # 导出件的下载 —— 本机用户直接去 `out/exports/` 拿就行，这条是给
        # **从别的机器浏览器打开控制台**的场景（区长在办公室看区里的机器）。
        if path == "/api/export/download" and method == "GET":
            target = app.export_file((query.get("name") or [""])[0])
            if target is None:
                return self._json({"error": "没有这份导出"}, 404)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"export.xlsx\"; filename*=UTF-8''%s"
                             % quote(target.name))
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return None

        # 门店**成员的目标拆分**（用户 2026-09-19：点门店名展开，目标可单独设）
        if path == "/api/attain/split" and method == "GET":
            store = (query.get("store") or [""])[0]
            period = (query.get("period") or [""])[0]
            # `roster=` 逗号分隔的**在册成员姓名** —— 门店的完整名单来自云商
            # （`staff_state` 要联网，**不能**在页面加载时自动去拉）。
            # ⚠ 界面上是"点一下带上全部成员"才传它。
            roster = [x.strip() for x in
                      ((query.get("roster") or [""])[0] or "").split(",") if x.strip()]
            if (query.get("all") or [""])[0] in ("1", "true", "yes"):
                # ⚠ 一次给全部门店（用户 2026-09-19：「像我图里这样**一个个下来**就行了，
                #   不需要选择」）—— 后端循环一次读盘就够，前端别打 28 个请求。
                return self._json(app.attain_split_all(roster))
            # ⚠ 单店那条要**自己过一遍范围**（M17）—— `all=1` 那条已经按角色筛了，
            #   而这条能直接点名任意门店：不拦的话，门店账号就能读别家的目标与人头。
            if not scope_store_ok(scope, store):
                return self._json(forbid(scope, "看目标拆分",
                                         "只能看自己范围内的门店：%s" % (store or "（没给门店）")), 403)
            return self._json(app.attain_split(store, period, roster))
        # **定时器任务表**（用户 2026-09-20：「给个接口，让各个模块设置什么时间点
        # 唤醒、以及唤醒做什么」）—— GET 读、PUT 改某一步的时间点。
        if path == "/api/timer" and method == "GET":
            return self._json(app.timer_tasks())
        if path == "/api/timer" and method == "PUT":
            # ⚠ 一个入口两件事（都按 `cmd` 定位）：
            #   * `enabled`（**开关滑块**：开了就注册到定时器）
            #   * `whens`（时间点）
            #   两个都给了就都做 —— 但**别把"只改开关"当成"恢复默认时间"**：
            #   `whens` 缺省就当没传（`body.get("whens")` 为 None）。老前端只传 whens ✓。
            body = self._read_json()
            cmd = body.get("cmd") or ""
            if "order" in body:
                # ⚠ 顺序是**整张表**的事（不是某一步）⇒ 走 `order` 这条，不看 `cmd`
                res = app.timer_set_order(body.get("order") or [])
                if res.get("ok"):
                    audit(app, "定时器顺序：%s" % " → ".join(res.get("order") or []), scope)
                if not res.get("ok"):
                    res.setdefault("error", res.get("why") or "没改成")
                return self._json(res, 200 if res.get("ok") else 400)
            if "enabled" in body:
                res = app.timer_set_enabled(cmd, bool(body.get("enabled")))
                # 机器属性：改可以，但要留痕（矩阵："那是对的（机器属性），但要记一条日志：谁改的"）
                if res.get("ok"):
                    audit(app, "定时器开关 %s=%s" % (cmd, bool(body.get("enabled"))), scope)
            elif "whens" in body:
                res = app.timer_set_when(cmd, body.get("whens") or [])
            else:
                res = {"ok": False,
                       "error": "要带 `enabled`（开关）/ `whens`（时间点）/ `order`（顺序）"}
            # ⚠ 失败时**必须给 `error`** —— 前端 `api()` 读的是 `data.error || data.message`，
            #   只有 `why` 的话界面上会显示成干巴巴的「HTTP 400」（这个项目踩过同型的坑）。
            if not res.get("ok"):
                res.setdefault("error", res.get("why") or "没改成")
            return self._json(res, 200 if res.get("ok") else 400)

        # 周度达成的**历史记录**（用户 2026-09-20：过了一周就锁住存档）
        if path == "/api/attain/history" and method == "GET":
            period = (query.get("period") or [""])[0]
            return self._json(app.attain_history(period))
        if path == "/api/attain/split/send" and method == "POST":
            body = self._read_json()
            store = body.get("store") or ""
            # C1：发送是**门店**的动作（区长/平台点它，只是把自己那份再发一遍）
            if not scope["can"].get("attain.split.send"):
                return self._json(forbid(scope, "发送给区长",
                                         "只有门店账号能发目标拆分（区长/平台只读）"), 403)
            if not scope_store_ok(scope, store):
                return self._json(forbid(scope, "发送给区长",
                                         "只能发自己范围内的门店：%s" % (store or "（没给门店）")), 403)
            res = app.attain_split_send(store, body.get("period") or "")
            return self._json(res, 200 if res.get("ok") else 400)
        if path == "/api/attain/split" and method == "PUT":
            body = self._read_json()
            store = body.get("store") or ""
            # ⚠⚠ **这是 M17 堵的第一个真洞**：原来这里**一个校验都没有** ——
            #   任何登录过的人（包括区长 / 平台）都能改**任意门店**的目标拆分，
            #   而 C1 定的是"只有门店能写、区长/平台只读"（口径在角色权限矩阵 §四）。
            if not scope["can"].get("attain.split.write"):
                return self._json(forbid(scope, "改目标拆分",
                                         "只有门店账号能改目标拆分（区长/平台只读）"), 403)
            if not scope_store_ok(scope, store):
                return self._json(forbid(scope, "改目标拆分",
                                         "只能改本店的目标：%s" % (store or "（没给门店）")), 403)
            res = app.attain_split_save(store, body.get("period") or "",
                                        body.get("targets") or {})
            return self._json(res, 200 if res.get("ok") else 400)

        # ---- 报告
        if path == "/api/report" and method == "GET":
            name = (query.get("name") or [""])[0]
            target = (app.out_dir / name).resolve()
            if not name or not str(target).startswith(str(app.out_dir.resolve())) \
                    or not target.is_file():
                return self._json({"error": "没有这份报告"}, 404)
            return self._json({"name": name, **load_report(target)})

        if path == "/api/report" and method == "DELETE":
            name = (query.get("name") or [""])[0]
            if not name:
                return self._json({"error": "没给文件名"}, 400)
            ok, msg = delete_report(app.out_dir, name)
            return self._json({"ok": ok, "message": msg}, 200 if ok else 400)

        if path == "/api/report/download" and method == "GET":
            name = (query.get("name") or [""])[0]
            target = (app.out_dir / name).resolve()
            if not name or not str(target).startswith(str(app.out_dir.resolve())) \
                    or not target.is_file():
                return self._json({"error": "没有这份报告"}, 404)
            self.send_response(200)
            self.send_header("Content-Type",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(target.stat().st_size))
            self.end_headers()
            self.wfile.write(target.read_bytes())
            return None

        # ---- 会话
        if path == "/api/session" and method == "GET":
            return self._json(app.session_info())

        if path == "/api/session" and method == "POST":
            body = self._read_json()
            text = (body.get("curl") or "").strip()
            if not text:
                return self._json({"error": "粘一份 curl 进来先"}, 400)
            sess = CbgSession.from_curl(text)              # 解析失败 → 409
            p = sess.save(app.session_path())
            result = {"saved": str(p), "cookies": sess.describe()}
            try:
                result.update(self._ping_result(app))
            except (CbgAuthError, CbgError) as e:
                result["ping"] = {"ok": False, "message": str(e)}
            return self._json(result)

        if path == "/api/session/ping" and method == "POST":
            return self._json(self._ping_result(app))

        if path == "/api/session" and method == "DELETE":
            p = app.session_path()
            if p.exists():
                p.unlink()
            return self._json({"deleted": True})

        # ---- 云商账号
        # ⚠ 界面上的入口 2026-09-18 全删了（公司账号内置、门店账号不要了），
        #   所以**没有前端在调这几个接口**。留着是因为它们是"用程序改一次云商账号"
        #   的入口（另一个是 `python -m src.cli erp-login`，带终端验证码流程）——
        #   云商偶尔会强制图形验证码，那时候自动登录走不通，总得有条路。
        #   要彻底删也行，但那是"再开一件事"，别顺手删。
        if path == "/api/erp" and method == "GET":
            return self._json(describe_credentials(app.erp_env_file()))

        if path == "/api/erp" and method in ("PUT", "POST"):
            body = self._read_json()
            env = app.erp_env_file()
            username = (body.get("username") or "").strip() or None
            company = (body.get("company") or "").strip() or None
            password = body.get("password") or None          # 空串 = 不改
            token = (body.get("token") or "").strip() or None
            if username is None and password is None and company is None and token is None:
                return self._json({"error": "什么都没改"}, 400)

            # 只有**账号或密码真的变了**才作废旧 token。
            # 否则界面上点一次「保存」就会把好好的 token 清掉，下次还得重登（还会撞限流）。
            old = describe_credentials(env)
            changed = (username is not None and username != old["username"]) or password is not None
            clear = changed and token is None
            save_credentials(env, username=username, password=password,
                             company=company, token=token, clear_token=clear)
            return self._json({"ok": True, "token_cleared": clear,
                               **describe_credentials(env)})

        if path == "/api/erp/login" and method == "POST":
            body = self._read_json()
            env = app.erp_env_file()
            new_user = (body.get("username") or "").strip()
            new_pwd = body.get("password") or ""
            new_comp = (body.get("company") or "").strip()

            creds = dict(load_credentials(env))
            if new_user:
                creds["username"] = new_user
            if new_pwd:
                creds["password"] = new_pwd
            if new_comp:
                creds["company"] = new_comp

            client = ErpClient(creds, env_file=env, timeout=60)
            try:
                r = client.login_and_verify(save=False)
            except ErpCaptchaRequired as e:
                # 账号要图形验证码 —— 把图交给页面，**同时留着这个 client**
                # （验证码跟会话绑定，换个 client 再提交一定验不过）
                pending_login.hold(client, creds.get("username", ""), new_pwd or None,
                                   creds.get("company"), e.image)
                return self._json({"ok": False, "need_captcha": True, "image": e.image,
                                   "message": str(e), "saved": False}, 200)
            except ErpError as e:
                # **失败不落盘** —— 打错一个字母不该把好密码覆盖掉
                return self._json({"ok": False, "message": str(e), "saved": False}, 200)
            except Exception as e:                            # noqa: BLE001
                return self._json({"ok": False, "message": f"{type(e).__name__}: {e}",
                                   "saved": False}, 200)

            pending_login.reset()
            save_credentials(env, username=creds.get("username"),
                             password=(new_pwd or None), company=creds.get("company"),
                             token=r["token"], clear_token=False)
            return self._json({"ok": True, "who": r.get("who", ""), "saved": True,
                               **describe_credentials(env)})

        if path == "/api/erp/login/captcha" and method == "POST":
            body = self._read_json()
            code = (body.get("code") or "").strip()
            if not pending_login.alive():
                pending_login.reset()
                return self._json({"ok": False, "expired": True,
                                   "message": "验证码过期了（或还没发起登录），"
                                              "重新点一次「测试登录」"})
            if not code:
                return self._json({"ok": False, "message": "先把验证码填上"})

            env = app.erp_env_file()
            try:
                # 用**同一个 client** 提交 —— 它带着发验证码时那个会话的 cookie
                r = pending_login.client.login_and_verify(vcode=code, save=False)
            except ErpCaptchaRequired as e:
                pending_login.image = e.image
                pending_login.tries += 1
                pending_login.created_at = time.time()          # 续期，让人重填
                return self._json({"ok": False, "need_captcha": True, "image": e.image,
                                   "message": str(e), "tries": pending_login.tries})
            except ErpError as e:
                pending_login.reset()
                return self._json({"ok": False, "message": str(e), "saved": False})
            except Exception as e:                              # noqa: BLE001
                pending_login.reset()
                return self._json({"ok": False, "message": f"{type(e).__name__}: {e}",
                                   "saved": False})

            save_credentials(env, username=pending_login.username,
                             password=pending_login.password,
                             company=pending_login.company,
                             token=r["token"], clear_token=False)
            who = r.get("who", "")
            pending_login.reset()
            return self._json({"ok": True, "who": who, "saved": True,
                               **describe_credentials(env)})

        # ---- 邮件推送（路径列表 · 2026-09-22）
        if path == "/api/mail" and method == "GET":
            # 先把老配置迁成路径（有文件就不动），门店升级不用重填
            try:
                from . import push_paths
                push_paths.migrate_from_legacy(
                    config_io.load_raw(app.config_path), app.root)
            except Exception:                                   # noqa: BLE001
                pass
            return self._json({"config_path": app.config,
                               **app.mail_paths_raw()})

        if path == "/api/mail" and method in ("PUT", "POST"):
            body = self._read_json()
            rows = body.get("paths")
            if rows is None:
                return self._json({"error": "要传 paths 数组"}, 400)
            if not isinstance(rows, list):
                return self._json({"error": "paths 得是数组"}, 400)
            # 密码留空 = 保留原密码（界面不回显）
            try:
                from . import push_paths as _pp
                old = {}
                if _pp.has_file(app.root):
                    for r in _pp.load_raw(app.root).get("mail") or []:
                        if r.get("id"):
                            old[r["id"]] = r
                fixed = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    rr = dict(r)
                    rid = str(rr.get("id") or "")
                    if not rr.get("password") and rid and old.get(rid, {}).get("password"):
                        rr["password"] = old[rid]["password"]
                    fixed.append(rr)
                mailer.save_mail_paths(fixed, app.root)
            except Exception as e:                              # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            audit(app, "邮件推送路径", scope, fields=["paths"])
            return self._json({"ok": True, "saved": True,
                               **app.mail_paths_raw()})

        if path == "/api/mail/test" and method == "POST":
            body = self._read_json()
            mc = app.mail_from_body(body)
            bad = mc.problems()
            if bad:
                return self._json({"ok": False, "saved": False,
                                   "message": "配置不全：" + "、".join(bad)}, 200)
            try:
                mailer.send(mc, "测试邮件",
                            "这是一封测试邮件。\n\n收到就说明 SMTP 配置没问题，"
                            "之后有几条路径就会各发一份。\n")
            except mailer.MailError as e:
                # 失败不落盘
                return self._json({"ok": False, "saved": False, "message": str(e)}, 200)
            return self._json({"ok": True, "saved": False,
                               "message": f"已发送到 {'、'.join(mc.recipients)}"}, 200)

        # ---- 企微推送（路径列表 · 2026-09-22）
        if path == "/api/wecom" and method == "GET":
            try:
                from . import push_paths as _pp
                _pp.migrate_from_legacy(
                    config_io.load_raw(app.config_path), app.root)
            except Exception:                                   # noqa: BLE001
                pass
            return self._json(wecom.describe_wecom_paths(
                config_io.load_raw(app.config_path), app.root))

        if path == "/api/wecom" and method in ("PUT", "POST"):
            body = self._read_json()
            rows = body.get("paths")
            if rows is None:
                return self._json({"error": "要传 paths 数组"}, 400)
            if not isinstance(rows, list):
                return self._json({"error": "paths 得是数组"}, 400)
            try:
                from . import push_paths as _pp
                old = {}
                if _pp.has_file(app.root):
                    for r in _pp.load_raw(app.root).get("wecom") or []:
                        if r.get("id"):
                            old[r["id"]] = r
                fixed = []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    rr = dict(r)
                    hook = str(rr.get("webhook") or "").strip()
                    rid = str(rr.get("id") or "")
                    if not hook and rid and old.get(rid, {}).get("webhook"):
                        rr["webhook"] = old[rid]["webhook"]   # 空 = 不改
                    if rr.get("webhook"):
                        if not wecom.extract_key(str(rr["webhook"])):
                            return self._json(
                                {"error": "webhook 地址看不出来 key —— "
                                          "把整条地址（含 key=…）粘进来"}, 400)
                    fixed.append(rr)
                wecom.save_wecom_paths(fixed, app.root)
            except Exception as e:                              # noqa: BLE001
                return self._json({"error": str(e)}, 400)
            audit(app, "企业微信推送路径", scope, fields=["paths"])
            return self._json({"ok": True, "saved": True,
                               **wecom.describe_wecom_paths(
                                   config_io.load_raw(app.config_path), app.root)})

        if path == "/api/wecom/test" and method == "POST":
            body = self._read_json()
            wc = app.wecom_from_body(body)
            bad = wc.problems()
            if bad:
                return self._json({"ok": False, "message": "配置不全：" + "、".join(bad)})
            try:
                msg = wecom.test_push(wc)
            except wecom.WecomError as e:
                return self._json({"ok": False, "message": str(e)})    # 失败不落盘
            return self._json({"ok": True, "message": msg})

        # ---- 后台服务 / 开机自启
        if path == "/api/service" and method == "GET":
            running = service.find_running(app.root)
            return self._json({
                "running": bool(running),
                "port": (running or {}).get("port", 0),
                "pid": (running or {}).get("pid", 0),
                "started_at": (running or {}).get("started_at", ""),
                "this_pid": os.getpid(),
                "autostart": autostart.status(app.root),
            })

        if path == "/api/autostart" and method == "POST":
            body = self._read_json()
            # elevated：显式指定"以管理员身份"还是"普通权限"。
            # ⚠ **不传就是普通权限**（`autostart.install(elevated=None)`）。
            #   这里以前是"不传就按当前进程是不是管理员来猜" —— 从提权进程里调
            #   就会静默注册成管理员模式，而管理员模式会让自动抓会话不可用。
            want_elevated = body.get("elevated")
            if want_elevated is not None:
                want_elevated = bool(want_elevated)
            res = autostart.install(app.root, elevated=want_elevated) \
                if body.get("enabled", True) is not False else autostart.remove()
            res["autostart"] = autostart.status(app.root)
            return self._json(res)      # 业务失败也是 200：请求处理成功了，只是操作没成

        # ---- 按需提权：**只把这一步**提权重做一遍
        #
        # ⚠ 为什么需要：有两件事**只有管理员能做**，而它们都是"一次性清理/注册"：
        #   1. 删掉**老版本留下的**那条提权计划任务（普通权限删不掉，留着的话
        #      每次登录还是以管理员拉起服务 → 自动抓会话永远坏着）；
        #   2. 覆盖一条**由管理员创建过**的定时任务。
        #   逼用户"右键 install.bat 以管理员身份运行"是错的 ——
        #   那会把 pip install 也一起提权跑掉（见 bootstrap.py 里的说明）。
        #   所以这里只弹一次 UAC，把**那一个子命令**提权重跑。
        #
        # 返回：`{"ok":..., "message":...}`。拿不到提权子进程的结果（用户点了"否"、
        # 超时）→ `elevated: None`，让界面告诉用户"要么没点「是」，要么超时了"。
        if path == "/api/schedule/legacy-prompt/seen" and method == "POST":
            # 「知道了 / 稍后再说」—— 记下这一版弹过了，以后不再主动弹。
            # ⚠ 记不上也返回 ok（只是下次再弹一次），为了记状态把界面卡住不值得。
            return self._json({"ok": True,
                               "saved": schedule.mark_legacy_prompted(app.root)})

        if path == "/api/schedule/replace" and method == "POST":
            # 「一键处理老任务」：建新的 → 建成了再删老的。
            #
            # ⚠⚠ **顺序由 `schedule.replace_legacy` 守着**，这里只管
            #   "先用普通权限试、不行才提权"（跟 `/api/schedule` 那条一个路子：
            #   首选普通权限 —— 建出来的任务归当前用户，以后读改删都不用管理员，
            #   绝大多数机器到这就成了，**一次 UAC 都不弹**）。
            body = self._read_json()
            st = schedule.status(app.root)
            time_str = str(body.get("time") or st.get("time") or schedule.DEFAULT_TIME)
            # ⚠ 2026-09-21 晚：`--days-ago` **早就废弃了**（脚本里不再写它，
            #   那个参数不影响任何一步）⇒ 不再从旧脚本里把它读回来。
            #   留个常量只是为了兼容 `replace_legacy` / `install` 的签名
            #   （它们收下但不用，见 `schedule._win_install` 里那段）。
            days_ago = schedule.DEFAULT_DAYS_AGO

            res = schedule.replace_legacy(app.root, time_str, days_ago, str(app.config))
            res["elevated"] = False
            if not res.get("ok") and not elevate.is_admin():
                # 普通权限没成 ⇒ 才轮到提权。**一次 UAC 里把"建新的 + 删老的"做完**
                # （所以是 `schedule-replace` 一个子命令，不是 install 后再 remove）。
                got = elevate.run_elevated(
                    app.root / "bootstrap.py",
                    ["schedule-replace", "--time", time_str,
                     "--days-ago", str(days_ago), "--config", str(app.config)],
                    timeout=90)
                if got is not None:
                    got["elevated"] = True
                    got["status"] = schedule.status(app.root)
                    return self._json(got)
                # 提权也没拿到结果（点了"否" / 超时）：把**普通权限那次**的
                # 失败原因如实给出去，别让用户只看到"没反应"。
                res["elevated"] = None
                res["message"] = ("%s 提权重试也没拿到结果 —— 要么你点了 UAC 的「否」，"
                                  "要么那个窗口还在等你按回车。"
                                  % res.get("message", ""))
            res["status"] = schedule.status(app.root)
            return self._json(res)

        if path == "/api/elevate" and method == "POST":
            body = self._read_json()
            what = (body.get("what") or "").strip()
            if what == "autostart":
                # 提权跑 `bootstrap.py autostart`：普通权限那条（注册表 Run 项）
                # 会重新注册一遍，顺带把删不掉的旧提权任务删掉。
                args = ["autostart"]
            elif what in ("schedule", "schedule-remove"):
                # ⚠ **这是"环境不允许"的兜底，不是首选路。**
                #
                # 首选是 `/api/schedule` POST —— **普通权限**注册。建出来的任务归当前用户，
                # 以后读 / 改 / 删都不需要管理员。绝大多数机器到那一步就成了，
                # **一次 UAC 都不弹**。只有那一步真的失败了才轮到本按钮。
                #
                # 失败就两种，而这两种**提权都能解**：
                #   ① 以前用**管理员身份**建过同名任务 —— 普通权限连 `/f` 都覆盖不了
                #      （门店实测报的就是 `错误: 拒绝访问。`）；
                #   ② 那台机器上普通权限**根本建不了**任务 —— 账户被 UAC 过滤、
                #      或组策略收紧了任务库的权限。
                # 分不清是哪一种也没关系：两种情况提权都能做成。
                #
                # ⚠ 代价得认下来：提权建出来的任务所有者是 `Administrators`，
                #   **之后普通权限连详情都读不到**（`schtasks /query /tn <名> /xml` 被拒），
                #   界面上时间和命令会空着 —— 用户的原话是
                #   「没有管理员权限就看不到定时执行设置了」。
                #   → 所以**我们自己记一份注册参数**（`.secrets/schedule.json`），
                #     界面显示走记录，不依赖 Windows 的 ACL 行为。
                #     见 `schedule._remember` / `_recall` ——
                #     **那份记录是"提权建任务"能成立的前提**，别绕过它。
                time_str = str(body.get("time") or schedule.DEFAULT_TIME)
                days_ago = int(body.get("days_ago", schedule.DEFAULT_DAYS_AGO))
                # ⚠ 前端的修复按钮手上只有 `full_name`（`\CBG报量对账-21点20`），
                #   而 `schedule.install` 会拒收带 `\` 的名字（那是防路径注入的）——
                #   不取叶子名的话这个按钮**必然 400**，表现又是"点了没反应"。
                name = str(body.get("name") or "").strip().rsplit("\\", 1)[-1]
                if what == "schedule":
                    # `/create` 带 `/f`，旧的同名任务（哪怕是管理员建的）一并覆盖掉，
                    # 不需要先单独删一次
                    args = ["schedule-install", "--time", time_str,
                            "--days-ago", str(days_ago),
                            "--config", str(app.config)]
                else:
                    args = ["schedule-remove"]
                if name:
                    args += ["--name", name]
            else:
                return self._json({"ok": False, "message": f"不认识的提权动作：{what!r}"}, 400)

            if elevate.is_admin():
                # 服务本身已经是管理员了 —— 不用再弹 UAC，直接跑效果一样，
                # 但**不能装作是提权成功的**：管理员身份本身就是要修掉的问题。
                return self._json({
                    "ok": False, "elevated": None,
                    "message": "服务现在本身就是以管理员身份在跑，不需要再提权。"
                               "请先按上面的办法把服务改成普通权限。"})
            got = elevate.run_elevated(app.root / "bootstrap.py", args, timeout=90)
            if got is None:
                return self._json({
                    "ok": False, "elevated": None,
                    "message": "没拿到提权窗口的结果 —— 要么你点了 UAC 的「否」，"
                               "要么窗口还没关（它在等你按回车）。"
                               "关掉那个窗口再点一次本按钮。"})
            got["elevated"] = True
            if what == "autostart":
                got["autostart"] = autostart.status(app.root)
            got["status"] = schedule.status(app.root)
            return self._json(got)

        # ---- 自动抓 cookie
        if path == "/api/session/auto" and method == "GET":
            return self._json(capture_job.snapshot())

        if path == "/api/session/auto" and method == "POST":
            body = self._read_json()
            headless = (body.get("mode") or "login") == "refresh"
            if capture_job.running:
                return self._json({"error": "已经有一个抓取在跑了，等它结束"}, 409)
            _cfg = config_io.load_raw(app.config_path)
            if not browser.find_browser(_cfg):
                return self._json({"error": "没找到 Chrome / Edge，找不到就没法自动抓 —— "
                                            "退回「粘贴 curl」那条路"}, 409)
            capture_job.reset()
            capture_job.running = True
            capture_job.state = "running"
            capture_job.headless = headless
            threading.Thread(target=_capture_worker, args=(app, headless),
                             daemon=True).start()
            return self._json(capture_job.snapshot())

        if path == "/api/session/auto/browser" and method == "GET":
            found = browser.find_browser(config_io.load_raw(app.config_path))
            return self._json({"found": bool(found),
                               "name": found[1] if found else "",
                               "path": found[0] if found else ""})

        # ---- 华为账号（自动登录用）
        if path == "/api/hwlogin" and method == "GET":
            return self._json(browser.describe_login(config_io.load_raw(app.config_path),
                                                     app.root))

        if path == "/api/hwlogin" and method in ("PUT", "POST"):
            body = self._read_json()
            cfg = config_io.load_raw(app.config_path)
            user = body.get("username")
            pwd = body.get("password") or None          # 空 = 不改
            if user is None and pwd is None:
                return self._json({"error": "什么都没改"}, 400)
            browser.save_login_credentials(
                cfg, app.root,
                username=(str(user).strip() if user is not None else None),
                password=pwd)
            return self._json({"ok": True,
                               **browser.describe_login(
                                   config_io.load_raw(app.config_path), app.root)})

        # ---- 配置
        if path == "/api/config" and method == "GET":
            return self._json(config_io.pick(config_io.load_raw(app.config_path)))

        if path == "/api/config" and method in ("PUT", "POST"):
            body = self._read_json()
            values = body.get("values") or body
            bad = [k for k in values if k not in config_io.EDITABLE]
            if bad:
                # 明确报错，别静默丢弃 —— 否则改了个不生效的字段还以为成功了
                return self._json({"error": f"这些字段不允许从界面改：{bad}"}, 400)
            cfg = config_io.update(app.config_path, values)
            audit(app, "通用设置", scope, fields=sorted(values))
            return self._json({"ok": True, "values": config_io.pick(cfg)})

        # ---- 定时执行
        if path == "/api/schedule" and method == "GET":
            # ⚠ 顺手自愈：run.bat 是**注册任务时**生成的，升级代码不会碰它 ——
            #   门店电脑上很容易留着旧脚本（用户就踩到了：黑窗没修掉、日志格式还是旧的、
            #   界面认不出"跑完没"）。这里比对脚本里的版本标记，过时就按当前模板重建，
            #   **并保住原来的 --days-ago**（不能默默改掉对账的目标日）。
            #   幂等：不是旧版就只读一个文件、不写。
            rebuilt = False
            try:
                rebuilt = schedule.refresh_runner_scripts(app.root, app.config)
            except Exception:                       # noqa: BLE001
                pass
            st = schedule.status(app.root)
            if rebuilt:
                st["script_rebuilt"] = True
            return self._json(st)

        if path == "/api/whatsnew/seen" and method == "POST":
            # 「知道了」/ 弹窗一显示 —— 记下这一版看过了，以后不再弹。
            # ⚠ 记不上也返回 ok（`mark_seen` 写不成只是下次再弹一次），
            #   为了记状态把界面卡住不值得。
            body = self._read_json()
            v = str(body.get("version") or version.VERSION)
            # ⚠⚠ **顺序不能反**：先把"现在该弹的这一份"算出来存下，**再**记 seen。
            #   反过来的话 `seen` 已经是 v 了，`versions_after` 算出**空的待办**
            #   —— 坑 13 那条「看过 ≠ 做完了」，差点又踩一次。
            snap = whatsnew.digest(app.root, v)
            return self._json({"ok": True,
                               "saved": whatsnew.mark_seen(app.root, v, body=snap)})

        if path == "/api/whatsnew" and method == "GET":
            # 「看这一版的更新说明」—— 从「设置 → 检查更新」随时翻回来。
            # ⚠ **不重新算**：算出来的是"现在还没做的"，而门店要的是
            #   "升级那天弹给我的那份"。所以优先取存档（见 `last_digest`）。
            #   取不到（刚装上、还没弹过）才现算一份。
            return self._json({"body": whatsnew.last_digest(app.root)
                                       or whatsnew.digest(app.root, version.VERSION)})

        if path == "/api/pools/history":
            # 「报量查询」页的历史。
            #   ?date=YYYY-MM-DD  → 那一天的 AD/BC 明细
            #   不带                → 有哪些年、那一年的逐日条数
            # ⚠ 明细行是 `pools.details()` 的原样 dict，键就是中文
            #   （串号/机型/门店/单号）—— 前端直接照着渲染，别在这儿翻译一遍。
            year = (query.get("year") or [""])[0]
            day = (query.get("date") or [""])[0].strip()
            y = int(year) if str(year).isdigit() else None
            if day:
                return self._json(_scope_pools(scope, pools_history.detail(app.root, day, y)))
            return self._json({"years": pools_history.years(app.root),
                               "days": pools_history.days(app.root, y)})

        # ------------------------------- 数据交换（M20，左下角那一行）
        if path == "/api/report/stores" and method == "GET":
            # **每店一张卡**（用户 2026-09-21 C3）。
            # ⚠ 判据：**只有管多店的身份**能看（门店一律 403 —— C4 明说了
            #   「门店不能看全区排名」，而藏菜单从来不算权限）。
            if scope.get("role") == ROLE_STORE:
                return self._json(forbid(scope, "看多店视图",
                                         "这是区长/平台看的页面"), 403)
            return self._json(app._stores_cards(scope))

        if path == "/api/report/store" and method == "GET":
            if scope.get("role") == ROLE_STORE:
                return self._json(forbid(scope, "看某家店的上报",
                                         "这是区长/平台看的页面"), 403)
            code = (query.get("code") or [""])[0].strip()
            if not code:
                return self._json({"error": "要给 code（门店编码）"}, 400)
            cards = {c["store_code"]: c for c in app._stores_cards(scope)["stores"]}
            if code not in cards:
                # ⚠ 不在**你的范围**里 ⇒ 和"这家店不存在"回同一句：
                #   分开说会变成一个"猜别家店编码"的探针。
                return self._json(forbid(scope, "看这家店",
                                         "这家店不在你的范围里"), 403)
            from .app import report_inbox as inbox_mod
            return self._json({"ok": True, "store": cards[code],
                               "days": inbox_mod.store_days(app.root, code, 14)})

        if path == "/api/report/inbox" and method == "POST":
            # 「现在收一次」（区长/平台机器上那个按钮）。
            if scope.get("role") == ROLE_STORE:
                return self._json(forbid(scope, "收门店上报", "这是区长/平台干的事"), 403)
            from .app import report_inbox as inbox_mod
            import io as _io
            buf = _io.StringIO()
            res = inbox_mod.run(app.root, config_path=app.config,
                                emit=lambda m: buf.write(str(m) + "\n"))
            audit(app, "收取门店上报", scope, mails=res.get("mails"),
                  ok_packages=res.get("ok_packages"))
            return self._json({"ok": res.get("ok", True), "result": res,
                               "log": buf.getvalue()})

        if path == "/api/pools-notify/clear" and method == "POST":
            # 「清除推送记忆」—— 清完下次推送会把所有串号重新当"新出现"强调。
            # ⚠ **只删那个记忆文件**，不动库、不动清单、不影响定时任务。
            #   如实回报"之前到底有没有"，不然界面永远说"已清除"，用户分不清
            #   是清成功了还是按钮没生效。
            had = pools_notify.clear(app.root)
            return self._json({"ok": True, "had": had,
                               "message": "已清除推送记忆" if had else "本来就没有推送记忆"})

        if path == "/api/report-bug" and method == "POST":
            # ⚠ 同步跑（跟 `/api/mail/test`、`/api/wecom/test` 一个路子）——
            #   这是一次性点击，用户就在旁边等着看结果。
            #   **打包那一步不碰网络**，所以最坏情况也就是等两个超时。
            body = self._read_json()
            from . import cli               # 延迟 import：cli 顶层会碰一堆东西，
                                            # 在这儿再导免得和 web 绕圈
            try:
                res = cli.report_bug(app.root, app.config,
                                     no_mail=bool(body.get("no_mail")),
                                     no_push=bool(body.get("no_push")))
            except Exception as e:                    # noqa: BLE001
                return self._json({"ok": False, "message": f"上报失败：{e}"})
            return self._json(res)

        if path == "/api/schedule/automation" and method in ("PUT", "POST"):
            # ⚠ 「自动化跑什么」这个设置 2026-09-20 就取消了，2026-09-21 晚连
            #   "手动整批"也删了 ⇒ 这里**不再改任何东西**（脚本跑什么是注册表派生的），
            #   只把话说清楚。
            #   ⚠ 还是给它一个**说人话的 410**（不是 404）：老页面缓存里的「保存」
            #     按钮打过来时，用户要看到的是"这个设置没有了"，
            #     而不是一个看不懂的 404、更不是"点了没反应"。
            return self._json(
                {"ok": False, "cancelled": True,
                 "message": "「自动化跑什么」这个设置已经取消 —— 每一步跑不跑，"
                            "由它自己的唤醒时刻决定（见「定时器设置」页）。"}, 410)

        if path == "/api/schedule" and method == "POST":
            body = self._read_json()
            res = schedule.install(app.root, body.get("time") or schedule.DEFAULT_TIME,
                                   int(body.get("days_ago", schedule.DEFAULT_DAYS_AGO)),
                                   app.config,
                                   name=body.get("name"))
            res["status"] = schedule.status(app.root)
            if res.get("ok"):
                audit(app, "注册定时任务 %s" % (body.get("time") or ""), scope)
            # 参数不合法 → 400；环境不允许（没权限改 crontab 之类）→ 200 + ok:false，
            # 因为请求本身处理得好好的，message 里带着人能照着做的办法
            return self._json(res, 400 if res.get("invalid") else 200)

        # ---- 检查更新 / 手动更新 / 回退
        if path == "/api/update" and method == "GET":
            # cached=1：只读缓存，一个网络请求都不发 —— 界面刷新走这条。
            # 主动查是后台线程的活（每天一次），见 selfupdate.daily_watcher。
            if (query.get("cached") or [""])[0] in ("1", "true", "yes"):
                return self._json(selfupdate.cached(app.root, version.VERSION))
            force = (query.get("force") or [""])[0] in ("1", "true", "yes")
            return self._json(selfupdate.check(app.root, version.VERSION, force=force))

        if path == "/api/update" and method == "POST":
            body = self._read_json()
            # dry=1 只下下来看看会改哪些文件，**不落盘** —— 让人更新前先看一眼
            if body.get("dry"):
                try:
                    root = selfupdate.download()
                    try:
                        pairs = selfupdate._targets(root)
                        return self._json({"ok": True, "files": sorted(
                            str(rel) for _, rel in pairs)})
                    finally:
                        import shutil as _sh
                        _sh.rmtree(root.parent, ignore_errors=True)
                except selfupdate.UpdateError as e:
                    return self._json({"ok": False, "message": str(e)})
            # 修复上次没走完的升级（概览页横幅上的两个按钮）：
            #   repair="rerun"   → 用上次留下的解压目录重跑（**不用联网**）
            #   repair="restore" → 从备份退回升级前的代码
            repair = (body.get("repair") or "").strip()
            if repair:
                try:
                    res = selfupdate.repair(
                        app.root, current=version.VERSION,
                        mode="restore" if repair == "restore" else "auto")
                except selfupdate.PartialUpdate as e:
                    out = dict(e.result or {})
                    out.update({"ok": False, "partial": True, "restarting": False,
                                "message": str(e)})
                    return self._json(out)
                except selfupdate.UpdateError as e:
                    return self._json({"ok": False, "message": str(e)})
                if not res.get("ok"):
                    return self._json(res)
                res["message"] = (res.get("message")
                                  or f"已重跑到 v{res.get('to') or '?'}，正在重启控制台…")
                res["restarting"] = selfupdate.restart_later(app.root)
                if res["restarting"] and app.server:
                    import threading as _th
                    _th.Timer(1.0, app.server.shutdown).start()
                return self._json(res)
            # ⚠ **历史版本回退已去掉**（用户 2026-09-23：版本回退没必要）。
            #   只保留：升级到 main 最新 + 半截失败后的 repair/restore。
            try:
                res = selfupdate.apply_update(app.root, current=version.VERSION)
            except selfupdate.PartialUpdate as e:
                # ⚠⚠ **改到一半失败了 —— 绝不重启服务。**
                #   当前进程还跑在"改之前"的代码上，它正是唯一还能干活的那一份；
                #   重启等于把"能修的手"换成"半新半旧的一摊"。
                #   现场（哪些文件已改、备份在哪）跟着返回，界面上给"修复"入口。
                out = dict(e.result or {})
                out.update({"ok": False, "partial": True, "message": str(e),
                            "restarting": False})
                return self._json(out)
            except selfupdate.UpdateError as e:
                return self._json({"ok": False, "message": str(e)})
            res["rollback"] = False
            # ⚠ 代码已经换了，但**当前进程还跑在旧代码上** —— 必须重启才生效
            res["restarting"] = selfupdate.restart_later(app.root)
            res["message"] = (f"已更新到 v{res.get('to') or '?'}，正在重启控制台…"
                              if res["restarting"] else
                              f"已更新到 v{res.get('to') or '?'}，"
                              "但自动重启没成功 —— 请双击 start.bat")
            if res["restarting"] and app.server:
                import threading as _th
                _th.Timer(1.0, app.server.shutdown).start()
            return self._json(res)

        if path == "/api/runlog" and method == "GET":
            # 计划任务跑的时候是个黑盒 —— 用户点完「执行」看不到任何东西，
            # 于是"点了也不推送"就成了唯一能观察到的现象。这里把日志tail出来。
            tail = int((query.get("tail") or ["150"])[0])
            return self._json(read_run_log(app, tail))

        if path == "/api/schedule/run" and method == "POST":
            body = self._read_json()
            res = schedule.run_now(body.get("name"))
            return self._json(res)      # 业务失败也是 200，跟删除一致

        if path == "/api/schedule" and method == "DELETE":
            # 按名字删**选中的那一个**；不传名字才退回删默认任务（兼容老前端）
            name = (query.get("name") or [""])[0].strip() or None
            # 传 root：删成功就顺手抹掉我们自己的注册记录，
            # 否则界面上会"删了还显示时间和命令"
            res = schedule.remove(name, app.root)
            res["status"] = schedule.status(app.root)
            return self._json(res)      # 业务失败也是 200：请求处理成功了，只是操作没成

        # ---- 运行
        # ⭐ 「刷新」= **先抓一次新数据、再重新读这一页**（用户 2026-09-20）。
        #   ⚠ 起的是**跟手动「跑一次」同一条路**的后台任务（同一把锁、日志进抽屉），
        #     不是在这儿同步跑 —— 抓数要一两分钟，把 HTTP 请求挂在那儿
        #     只会让页面转圈、还看不出卡在哪一步。
        # 黄横幅「关掉」：只关**当前这一条**（指纹一变就再出来）
        if path == "/api/data-state/dismiss" and method == "POST":
            return self._json(app.dismiss_data_state())

        if path == "/api/refresh" and method == "POST":
            body = self._read_json()
            page = (body.get("page") or "").strip()
            steps = REFRESH_STEPS.get(page)
            if not steps:
                return self._json(
                    {"ok": False,
                     "error": "不认识的页面：%s（认得的是 %s）"
                              % (page, "、".join(sorted(REFRESH_STEPS)))}, 400)
            # 已经在跑就别排队（RunManager 也会挡，这儿提前说清楚理由）
            if manager.current():
                return self._json({"ok": False, "running": True,
                                   "error": "已经有一趟在跑了，等它结束再刷新"}, 409)
            # ⭐ 手动刷新 30 分钟冷却（2026-09-22 用户）：云商/玲珑刚拉过就**不再拉**，
            #   只重算。⚠ 定时器（`start_argv`）**不过这道闸**。
            run_steps, cool_skipped = _cool_skip(steps, app.root)
            if not run_steps:
                return self._json(
                    {"ok": True, "steps": [], "skipped": list(cool_skipped),
                     "message": "云商/玲珑数据 %d 分钟内刚拉过，不用再抓——直接重读这一页"
                                % FETCH_COOLDOWN_MIN})
            job = manager.start_steps(app.root, app.config, run_steps,
                                      what_label="抓新数据（%s）" % page)
            msg = "正在抓：%s（跑完自动刷新这一页）" % run_daily.steps_label(run_steps)
            if cool_skipped:
                msg = ("云商/玲珑 %d 分钟内已拉过，跳过：%s；%s"
                       % (FETCH_COOLDOWN_MIN, run_daily.steps_label(cool_skipped), msg))
            return self._json({"ok": True, "job": job.snapshot(0),
                               "steps": list(run_steps),
                               "skipped": list(cool_skipped),
                               # ⚠ 说清**抓的是哪几步**（别只写"抓新数据"）——
                               #   用户要看得出"刷达成只抓云商，不碰玲珑"。
                               "message": msg})

        if path == "/api/run" and method == "POST":
            # ⚠⚠ 2026-09-21 晚（用户：「现在不需要 run daily 吧，按定时器运行就行了」）——
            #   **"手动跑一整趟"这个入口删掉了**：
            #     * 界面上那张「跑一次」的卡先删的（右下角抽屉里）；
            #     * 然后 `runner.start()` + `BUTTON_STEPS` 那套"预设"也删了。
            #   现在起一趟只有两条路，**都得点名**：`/api/refresh`（各页「刷新」）
            #   和内置定时器（`timer.tick` → `start_argv`）。
            #   ⚠ 留个**说人话的 410**，不静默 404：老页面缓存里的 JS 还可能打过来，
            #     而 404 只会让那个按钮"点了没反应"（这个项目最怕的一种失败）。
            return self._json(
                {"error": "「跑一次」这个入口已经去掉了 —— 到点由内置定时器按每一步"
                          "自己的时刻跑（「定时器设置」页能看到）；想现在抓一次新数据，"
                          "用各页右上角的「刷新」。"}, 410)

        if path == "/api/run" and method == "GET":
            job_id = (query.get("id") or [""])[0]
            since = int((query.get("since") or ["0"])[0])
            # ⚠ 不指名 job 时给"**最近一趟该给人看的**"（跳过自动更新这种内部步骤）——
            #   否则抽屉里的运行日志天天是"已经是最新版"，真活那趟被顶掉。
            job = manager.get(job_id) if job_id else manager.latest_visible()
            if not job:
                return self._json({"running": False, "lines": [], "line_count": 0,
                                   "exit_code": None, "id": None})
            return self._json(job.snapshot(since))

        if path == "/api/run/stop" and method == "POST":
            job = manager.current()
            if job:
                job.kill()
            return self._json({"stopped": bool(job)})

        return self._json({"error": f"没有这个接口：{path}"}, 404)

    @staticmethod
    def _ping_result(app: App) -> dict:
        """自检。**结果要落盘** —— 否则刷新页面又变回"没验证过"（用户报过这个）。"""
        client = app.cbg_client()
        ok, msg = client.ping()
        app.record_check(client.session, ok, msg)
        return {"ping": {"ok": ok, "message": msg}}


def read_run_log(app: "App", tail: int = 150) -> dict:
    """读计划任务的日志（out/run.log），给界面看。

    额外把「推没推、为什么没推」挑出来 —— 那两行是用户最想知道的，
    埋在 150 行里等于没有。
    """
    p = app.out_dir / schedule.LOG_NAME
    try:
        st = p.stat()
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"exists": False, "mtime": 0, "size": 0, "lines": [],
                "exit": None, "mail": "", "wecom": "", "has_output": False}

    lines = text.splitlines()
    # 判断"对账程序到底跑起来没有"：日志里只有 bat 写的 exit= 行、
    # 没有任何对账输出 —— 说明 Python **根本没执行**（路径不对 / 假 python）。
    # 用户报的"退出码 120"就是这个形状，光看退出码完全指不到方向。
    has_output = any(
        ("===" in ln and "exit=" not in ln) or "[1/6]" in ln or "对账" in ln
        or "报量" in ln or "Traceback" in ln
        for ln in lines)
    shown = lines[-tail:] if tail > 0 else lines
    m = None
    for ln in reversed(lines):                    # 最后一条 exit= 就是本次的退出码
        hit = re.search(r"exit=(\d+)", ln)
        if hit:
            m = int(hit.group(1))
            break
    mail = wecom = ""
    for ln in reversed(lines):
        if not mail and "邮件：" in ln:
            mail = ln.strip()
        if not wecom and "企微：" in ln:
            wecom = ln.strip()
        if mail and wecom:
            break
    return {"exists": True, "mtime": st.st_mtime, "size": st.st_size,
            "lines": shown, "truncated": len(lines) > len(shown),
            "exit": m, "mail": mail, "wecom": wecom,
            "has_output": has_output}


def _free_port(host: str, port: int) -> int:
    if port:
        return port
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def serve(port: int = 8787, host: str = "127.0.0.1", open_browser: bool = True,
          root: Path | str = ROOT, config: str = DEFAULT_CONFIG) -> None:
    app = App(root, config)
    Handler.app = app
    (app.root / "out").mkdir(parents=True, exist_ok=True)   # 报告目录，先建好省得后面各处判断

    # 已经在跑就别起第二个（开机自启 + 用户双击 start.bat 会遇到这种情况）
    existing = service.find_running(app.root)
    if existing:
        url = f"http://{existing.get('host', '127.0.0.1')}:{existing['port']}/"
        print(f"服务已经在后台运行了：{url}")
        if open_browser:
            webbrowser.open(url)
        return

    port = _free_port(host, port)
    httpd = ThreadingHTTPServer((host, port), Handler)
    app.server = httpd
    service.write_state(app.root, pid=os.getpid(), host=host, port=port)

    url = f"http://{host}:{port}/"
    # ⚠ 用**产品名**，别写仓库名（`cbg-reconcile`）——
    #   用户 2026-09-19：「日志里这个 cbg-reconcile 好改吗」。
    print(f"{version.APP_NAME} 控制台已启动： {url}")
    print(f"  项目目录：{app.root}")
    print(f"  配置文件：{app.config}")
    print(f"  停止服务：python -m src.cli stop（或双击 stop.bat）")

    # 每天开机后自动刷一次界面要用的数据（用户 2026-09-18：「加个每天系统刚启动时
    # 自动更新数据的逻辑，具体更新什么我们再定」）。
    #
    # ⚠ **排在启动提示之后、且是后台线程** —— 绝不能拖慢控制台起来。
    # ⚠ 到底刷什么写在 `src/startup.py` 的 `TASKS` 里（现在定的是
    #   「组织架构 + 本店人员」和「检查更新」）；一天只跑一次。
    try:
        startup.start_background(app)
    except Exception as e:                                  # noqa: BLE001
        # ⚠ 启动路径上不许抛（见 AGENTS.md 坑 2）——
        #   刷新失败跟"控制台能不能起来"一点关系都没有。
        print(f"  ⚠ 启动刷新没起起来（不影响使用）：{type(e).__name__}: {e}")

    # **启动自检**（用户 2026-09-19 定的流程：先健康，再启动）。
    #
    # ⚠ 三件事必须一起看，少一件就会做错：
    #   ① **跑一次自检** `health.boot()` —— 它自己绝不抛；
    #   ② **把结果说出来**（命令行 + `/api/boot`）—— 界面横幅读的是同一份；
    #   ③ **记一笔** `run_record`（kind=`boot`）—— 用户要求"健康模块记录日志"。
    #
    # ⚠⚠ **只有硬门槛（blocking）才拦业务**，而且拦的方式是"界面给修复页"，
    #     **不是"服务起不来"** —— 门店连不上网是常态，硬挡的后果是连
    #     "上报 bug"那个按钮都点不了（设计基线 §一·十一）。
    try:
        st = app.boot_state(force=True)
        for line in health.boot_lines(st):
            print(line)
        runlog.record("boot", bool(st.get("allow_start")),
                      why="" if st.get("allow_start")
                      else "硬门槛没过：%s" % (st["blocking"][0]["why"] if st["blocking"] else ""),
                      note="启动自检 %d 项，硬门槛 %d / 警告 %d / 待办 %d"
                           % (len(st.get("items") or []), len(st.get("blocking") or []),
                              len(st.get("warnings") or []), len(st.get("todos") or [])),
                      root=app.root, blocking=[i["why"] for i in (st.get("blocking") or [])])
    except Exception as e:                                  # noqa: BLE001
        print(f"  · 启动自检没跑起来（不影响使用）：{type(e).__name__}: {e}")

    # ⭐ **内置定时器**（M10，用户 2026-09-20）：服务自己在到点时把 daily 叫起来。
    #
    # ⚠ 三个"为什么不"：
    #   ① **不用 `threading.Timer` 排一次** —— 系统时间会被改（夏令时 / 门店手动校时），
    #      排好的那一刻就错了；心跳是"每 30 秒重新算一次该不该跑"，改时间也不怕；
    #   ② **不在服务线程里直接调 `run_daily`** —— 走的必须是子进程，
    #      和手动「跑一次」、和系统计划任务**同一条路**（日志进抽屉、退出码、锁、
    #      工作目录、编码只维护一份）；
    #   ③ **不许把异常抛到这儿** —— 这是服务启动路径，抛一下 = 服务起不来。
    try:
        _timer_th = timer.start_heartbeat(
            root=app.root, config=app.config,
            spawn=lambda argv: manager.start_argv(app.root, argv),
            busy=lambda: bool(manager.current()),
            say=lambda m: print("  " + m))
        app.timer_thread = _timer_th
    except Exception as e:                                  # noqa: BLE001
        print(f"  · 内置定时器没起起来（不影响手动「跑一次」）：{type(e).__name__}: {e}")

    # 后台每天查一次更新 —— 门店同事不会主动点那个按钮，不查就永远停在旧版本。
    # **只报不装**：更新完服务要重启，撞上对账任务就把那趟断了。
    #
    # ⚠ 也包在 try 里：这是**服务启动路径**，这里抛出去就是"服务起不来"，
    #   而对账本身跟更新检查一点关系都没有。
    try:
        threading.Thread(
            target=selfupdate.daily_watcher, args=(app.root, version.VERSION),
            name="update-watcher", daemon=True).start()
    except Exception as e:                                  # noqa: BLE001
        print(f"  · 更新检查线程没起来（不影响对账）：{type(e).__name__}: {e}")

    # 管理员身份是浏览器自动化的**已知杀手**：Edge / Chrome 拒绝以管理员运行，
    # 会把命令行交棒出去然后自己退 0 —— 「自动抓会话」拿不到调试端口，**必坏**。
    #
    # ⚠ 从 2026-09-16 起，**正常装出来的服务就是普通权限**（开机自启走注册表 Run 项）。
    #   所以走到下面这个分支说明这台机器上是**老版本装出来的管理员模式**，
    #   或者有人手动用了 `autostart --elevated` —— 是异常，要说明白并给解法。
    #   整块包在 try 里，而且**不用 emoji / 特殊符号**：后台服务是 pythonw 起的，
    #   stdout 没有控制台（或被重定向），这时 Python 按 locale 编码（中文 Windows
    #   是 GBK）写输出，`⚠`（U+26A0）这类字符编不出来会抛 UnicodeEncodeError ——
    #   而它就在 serve_forever() 前面，一抛服务就永远起不来了。
    try:
        if autostart.is_elevated():
            print()
            print("  注意：这个服务是**以管理员身份**在跑。")
            print("        「自动抓华为会话」会失败（Edge / Chrome 拒绝以管理员运行）。")
            print("        对账本身不受影响。改法：到「设置 - 后台服务」把「启动方式」")
            print("        改成「普通权限」保存，然后停掉服务、用普通权限双击 start.bat。")
            print()
    except Exception:                                       # noqa: BLE001
        pass          # 提示打不出来不是问题，**服务起不来才是问题**

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        service.clear_state(app.root)
        httpd.server_close()
