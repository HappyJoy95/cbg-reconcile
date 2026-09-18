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
from urllib.parse import parse_qs, unquote, urlparse

from . import (autostart, browser, config_io, elevate, mailer,
               pools_history, pools_notify,
               run_daily, schedule,
               selfupdate, service, upgrade, version, wecom, whatsnew)
from .cbg import CbgClient, CbgError
from .erp import (DEFAULT_ENV_FILE, ErpCaptchaRequired, ErpClient, ErpError,
                  describe_credentials, load_credentials, save_credentials)
from .report import delete_report, list_reports, load_report
from .runner import manager
from .session import CbgAuthError, CbgSession

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DEFAULT_CONFIG = "config/store-SCN231409.yaml"


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


pending_login = PendingLogin()
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

    @property
    def config_path(self) -> Path:
        p = Path(self.config)
        return p if p.is_absolute() else self.root / p

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    # ------------------------------------------------------------------ 会话
    def session_path(self, cfg: dict | None = None) -> Path:
        cfg = cfg if cfg is not None else config_io.load_raw(self.config_path)
        rel = (cfg.get("session") or {}).get("file") \
            or f".secrets/cbg-{(cfg.get('store_code') or 'default')}.json"
        p = Path(rel)
        return p if p.is_absolute() else self.root / p

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

    def cbg_client(self) -> CbgClient:
        cfg = config_io.load_raw(self.config_path)
        sess = CbgSession.load(self.session_path(cfg))
        return CbgClient(sess, store_code=cfg.get("store_code") or None)

    # ------------------------------------------------------------------ 云商
    def erp_env_file(self) -> str:
        cfg = config_io.load_raw(self.config_path)
        return (cfg.get("erp") or {}).get("env_file") or DEFAULT_ENV_FILE

    def erp_client(self) -> ErpClient:
        f = self.erp_env_file()
        return ErpClient(load_credentials(f), env_file=f, timeout=60)

    # ------------------------------------------------------------------ 邮件
    MAIL_KEYS = ("enabled", "host", "port", "security", "sender", "recipients",
                 "subject_prefix", "when")

    def mail_config(self) -> mailer.MailConfig:
        return mailer.load_mail_config(config_io.load_raw(self.config_path), self.root)

    def mail_from_body(self, body: dict) -> mailer.MailConfig:
        """用界面上填的值拼一份配置 —— **测试邮件不先保存**（跟云商登录一个道理）。

        body 里出现的字段就**以 body 为准，空了就是空的** ——
        否则用户清空服务器地址再点测试，会悄悄回退到已保存的值，
        明明填错了却"测试通过"，最难查的那种坑。
        （密码例外：它是"留空＝不改"，因为界面上永远不回显。）
        """
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
    WECOM_KEYS = ("enabled", "when", "mention_all", "send_file")

    def wecom_config(self) -> wecom.WecomConfig:
        return wecom.load_wecom_config(config_io.load_raw(self.config_path), self.root)

    def wecom_from_body(self, body: dict) -> wecom.WecomConfig:
        """用界面上填的值拼配置 —— 测试推送**不先保存**。"""
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
    def pos(self) -> dict:
        """「POS 合规」tab 的数据 —— **纯读盘，一个网络请求都不发**。

        依据就是上面那条注释（概览页 30 秒刷一次，不能每次都戳网）。
        POS 分数由 `python -m src.cli pos`（或日常流程）算好落 `out/pos-<年>.json`，
        看板只负责把它读出来。**看板不做计算，也不登任何系统。**
        """
        files = sorted(self.out_dir.glob("pos-[0-9][0-9][0-9][0-9].json"))
        if not files:
            return {"exists": False, "rows": [],
                    "hint": "还没算过 —— 先抓数（dump）再算分（pos）"}
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
            "session": self.session_info(),
            "schedule": schedule.status(self.root),
            # 老定时任务要不要**主动弹一次**（用户 2026-09-17 选的方案 C）。
            # ⚠ 判据全在后端：① 系统里真挂着老名字的任务 ② 这一版还没弹过。
            #   前端不自己记"弹过没" —— 那种状态放前端一定会漂。
            "legacy_prompt": schedule.legacy_prompt_pending(self.root),
            # 「自动化跑什么」——界面渲染复选框要用，所以连**可选值**一起给，
            # 免得前端自己写一份标签表（那就是"两处各写一遍"的开始）。
            "automation": {
                "steps": list(schedule.automation_steps(self.root)),
                # ⚠ **只给可选的那两项**（用户 2026-09-17 把「抓四池数据」
                #   的复选框拿掉了）。复选框列表和旁边那列「跑什么」必须对得上 ——
                #   否则用户看到"只勾了两项"、实际跑了三件，会以为程序乱来。
                "choices": [{"value": k, "label": run_daily.STEP_LABELS[k]}
                            for k in schedule.AUTOMATION_CHOICES],
                # 取消不掉的项单独给 —— 前端**只写一句说明，不画复选框**
                #   （画成灰掉的勾选框反而像"能改但改不动"）。
                "always_on": [{"value": k, "label": run_daily.STEP_LABELS[k]}
                              for k in run_daily.ALWAYS_STEPS],
                "label": run_daily.steps_label(schedule.automation_steps(self.root)),
            },
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

    def _host_ok(self) -> bool:
        """防 DNS rebinding：只认本机 Host。"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

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

        if path == "/api/shutdown" and method == "POST":
            if app.server:
                threading.Thread(target=app.server.shutdown, daemon=True).start()
            return self._json({"ok": True, "message": "正在停止"})

        if path == "/api/overview" and method == "GET":
            return self._json(app.overview())

        if path == "/api/pos" and method == "GET":
            return self._json(app.pos())

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

        # ---- 邮件推送
        if path == "/api/mail" and method == "GET":
            return self._json({"config_path": app.config,
                               **mailer.describe_mail(app.mail_config())})

        if path == "/api/mail" and method in ("PUT", "POST"):
            body = self._read_json()
            mc = app.mail_config()
            values = {f"mail.{k}": body[k] for k in app.MAIL_KEYS if k in body}
            if body.get("port") not in (None, ""):
                try:
                    values["mail.port"] = int(body["port"])
                except (TypeError, ValueError):
                    return self._json({"error": "端口得是数字"}, 400)
            if values:
                config_io.update(app.config_path, values)

            sec_keys = {}
            if body.get("username") is not None:
                sec_keys["username"] = str(body["username"]).strip()
            if body.get("password"):
                sec_keys["password"] = str(body["password"])
            if sec_keys:
                # 换了账号就作废旧密码？—— 不。邮箱密码改了必须重新填，别自作聪明清空。
                mailer.save_mail_secrets(mc.env_file, **sec_keys)
            return self._json({"ok": True, "saved": True,
                               **mailer.describe_mail(app.mail_config())})

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
                            "之后每次对账跑完都会发到这个邮箱。\n")
            except mailer.MailError as e:
                # 失败不落盘
                return self._json({"ok": False, "saved": False, "message": str(e)}, 200)
            return self._json({"ok": True, "saved": False,
                               "message": f"已发送到 {'、'.join(mc.recipients)}"}, 200)

        # ---- 企微推送
        if path == "/api/wecom" and method == "GET":
            return self._json(wecom.describe_wecom(app.wecom_config()))

        if path == "/api/wecom" and method in ("PUT", "POST"):
            body = self._read_json()
            wc = app.wecom_config()
            values = {f"wecom.{k}": body[k] for k in app.WECOM_KEYS if k in body}
            if values:
                config_io.update(app.config_path, values)
            # webhook 是凭据，进 .secrets；留空表示不改
            if body.get("webhook"):
                key = wecom.extract_key(str(body["webhook"]))
                if not key:
                    return self._json({"error": "webhook 地址看不出来 key —— "
                                                "把整条地址（含 key=…）粘进来"}, 400)
                wecom.save_wecom_secrets(wc.env_file, str(body["webhook"]).strip())
            return self._json({"ok": True, "saved": True,
                               **wecom.describe_wecom(app.wecom_config())})

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
            days_ago = schedule.existing_days_ago(app.root)
            if days_ago is None:
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
            # 「四池比对」页的历史。
            #   ?date=YYYY-MM-DD  → 那一天的 AD/BC 明细
            #   不带                → 有哪些年、那一年的逐日条数
            # ⚠ 明细行是 `pools.details()` 的原样 dict，键就是中文
            #   （串号/机型/门店/单号）—— 前端直接照着渲染，别在这儿翻译一遍。
            year = (query.get("year") or [""])[0]
            day = (query.get("date") or [""])[0].strip()
            y = int(year) if str(year).isdigit() else None
            if day:
                return self._json(pools_history.detail(app.root, day, y))
            return self._json({"years": pools_history.years(app.root),
                               "days": pools_history.days(app.root, y)})

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
            # ⚠ 只改**跑什么**，不动时间/目标日 —— 所以**不用重新注册 Windows 任务**，
            #   只重写 run.bat 就行。那任务是跑 run.bat 的，内容变了它自然跟着变，
            #   一次 UAC 都不用弹（重新注册可能弹）。
            body = self._read_json()
            # 认 `steps`；也认老的 `what`（那时它也是一串步骤名，用法一样）
            picked = body.get("steps")
            if picked is None:
                picked = body.get("what")
            # ⚠ **空数组是合法的**（用户 2026-09-17 定）：含义是「每天只抓数据，
            #   不算也不推」。要挡的只是"传了个不是数组的东西"（老前端传字符串）。
            if not isinstance(picked, list):
                return self._json(
                    {"error": "steps 要是个数组（可选项：%s；「%s」是必做的，不用传）"
                              % (" / ".join(run_daily.STEP_LABELS[k]
                                            for k in schedule.AUTOMATION_CHOICES),
                                 " / ".join(run_daily.STEP_LABELS[k]
                                            for k in run_daily.ALWAYS_STEPS))}, 400)
            try:
                res = schedule.set_automation_steps(app.root, app.config, picked)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except OSError as e:
                return self._json({"ok": False, "message": f"写 run.bat 失败：{e}"})
            res["status"] = schedule.status(app.root)
            res["automation"] = {"steps": list(schedule.automation_steps(app.root))}
            return self._json(res)

        if path == "/api/schedule" and method == "POST":
            body = self._read_json()
            res = schedule.install(app.root, body.get("time") or schedule.DEFAULT_TIME,
                                   int(body.get("days_ago", schedule.DEFAULT_DAYS_AGO)),
                                   app.config,
                                   name=body.get("name"))
            res["status"] = schedule.status(app.root)
            # 参数不合法 → 400；环境不允许（没权限改 crontab 之类）→ 200 + ok:false，
            # 因为请求本身处理得好好的，message 里带着人能照着做的办法
            return self._json(res, 400 if res.get("invalid") else 200)

        # ---- 检查更新 / 手动更新 / 回退
        if path == "/api/update" and method == "GET":
            # cached=1：只读缓存，一个网络请求都不发 —— 界面刷新走这条。
            # 主动查是后台线程的活（每天一次），见 selfupdate.daily_watcher。
            if (query.get("cached") or [""])[0] in ("1", "true", "yes"):
                return self._json(selfupdate.cached(app.root, version.VERSION))
            # history=1：能回退到的历史版本列表（给「历史版本」那块用）
            if (query.get("history") or [""])[0] in ("1", "true", "yes"):
                try:
                    return self._json({"ok": True,
                                       "current": version.VERSION,
                                       "versions": selfupdate.history()})
                except selfupdate.UpdateError as e:
                    return self._json({"ok": False, "message": str(e),
                                       "current": version.VERSION})
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
            # ref=<commit sha> → 回退到那一版。走的是**同一条铺代码的路**，
            # 所以门店配置 / 会话 / 历史报告一样不会动。
            ref = (body.get("ref") or "").strip()
            try:
                res = (selfupdate.rollback(app.root, ref=ref, current=version.VERSION)
                       if ref else
                       selfupdate.apply_update(app.root, current=version.VERSION))
            except selfupdate.UpdateError as e:
                return self._json({"ok": False, "message": str(e)})
            res["rollback"] = bool(ref)
            # ⚠ 代码已经换了，但**当前进程还跑在旧代码上** —— 必须重启才生效
            res["restarting"] = selfupdate.restart_later(app.root)
            verb = "回退到" if ref else "更新到"
            res["message"] = (f"已{verb} v{res.get('to') or '?'}，正在重启控制台…"
                              if res["restarting"] else
                              f"已{verb} v{res.get('to') or '?'}，"
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
        if path == "/api/run" and method == "POST":
            body = self._read_json()
            # ⚠ `what` 认不出来要**在起进程之前**返回 400（`runner.start` 会抛 ValueError）。
            #   不拦的话界面上会留一个半死的 job，一直显示"在跑"。
            what = body.get("what") or run_daily.DEFAULT_WHAT
            if what not in run_daily.BUTTON_STEPS:
                return self._json(
                    {"error": "不认识的 what：%s（认得的是 %s）"
                              % (what, "、".join(sorted(run_daily.BUTTON_STEPS)))}, 400)
            # ⚠ 2026-09-17：`mode` / `date` / `days_ago` / `lookback` / `lookahead`
            #   **不再收**，也不再往命令行拼 —— 报量排查整步拿掉之后它们一个都
            #   不影响结果，界面上那两块（目标日 / 高级）也删了。
            #   老前端/老脚本多传字段**不报错、直接忽略**（刷新前的那一版
            #   还在跑旧 JS，报 400 会让按钮点了没反应）。
            job = manager.start(app.root, app.config, what=what)
            return self._json(job.snapshot(0))

        if path == "/api/run" and method == "GET":
            job_id = (query.get("id") or [""])[0]
            since = int((query.get("since") or ["0"])[0])
            job = manager.get(job_id) if job_id else manager.latest()
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
    print(f"cbg-reconcile 控制台已启动： {url}")
    print(f"  项目目录：{app.root}")
    print(f"  配置文件：{app.config}")
    print(f"  停止服务：python -m src.cli stop（或双击 stop.bat）")

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
