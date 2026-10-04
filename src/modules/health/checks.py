"""系统健康状态 —— 汇总证据、判"能不能启动"、决定"能不能自动更新"。

用户 2026-09-19：「加一个**系统健康状态模块**，**保持自动更新**和记录日志；
每个功能模块也要给它输出日志。**系统启动的时候也是先运行健康模块**，健康模块对其他模块进行自检。」

## 两条它**不**干的事（都写进设计基线 §一·六 / §一·十一）

1. **不提供能力**：它收集证据，不替别的模块干活；
2. **不自己猜**：每个模块的判据由**那个模块自己**提供（`timer.status()` / `notify.status()` /
   `migrate.status()` / `data_state()`），这里只**调用并汇总**。

## 分级（用户 2026-09-19 的 boot flow，我改过四处，见设计基线 §一·十一）

| 档 | 谁 | 不过怎么办 |
|---|---|---|
| `blocking`（硬门槛） | 代码完整性 · 冒烟 · 库结构 · 功能注册表 | **不起业务**，但**要能起修复页** |
| `warnings`（软警告） | 计时 · 推送 · 主题 · 数据过期 | **照常启动** + 横幅 + 记 `run_record` |
| `todos`（待办） | 登录验证 · 门店没配全 | **门禁**（只给登录页） |

⚠⚠ **`boot()` 绝不抛、也绝不"因为某一项不过就不许启动"** ——
门店连不上网、没配邮箱、计划任务提权失败，这些都是**常态**；
硬挡的后果是**门店连"上报 bug"那个按钮都点不了**（设计基线 §一·十一 改动 1）。
"""

from __future__ import annotations

from pathlib import Path

from ...paths import ROOT
from ...storage import migrate, runlog

BLOCKING, WARNING, TODO = "blocking", "warning", "todo"

#: 五组（界面按这个顺序显示）—— 加一组就在这儿加
GROUP_LABELS = {
    "code": "代码",
    "schema": "结构",
    "data": "数据",
    "timer": "定时",
    "notify": "推送",
    "auth": "登录",
    "features": "功能",
}


def _item(group: str, state: str, why: str, *, level: str = WARNING, detail=None) -> dict:
    return {"group": group, "group_label": GROUP_LABELS.get(group, group),
            "state": state, "why": why, "level": level, "detail": detail or {}}


# ------------------------------------------------------------------ 各组自检
def check_code(root=None) -> list:
    """① 代码完整性 + 冒烟（1.4d / 1.4e 那两件）—— **硬门槛**。"""
    out = []
    from ... import selfupdate
    j = selfupdate.pending(root or ROOT)
    if j:
        out.append(_item("code", "failed",
                         "上次升级没走完（%s）：%s" % (j.get("state"), j.get("error") or "中断了"),
                         level=BLOCKING, detail={"journal": str(selfupdate.journal_path(root or ROOT))}))
    else:
        out.append(_item("code", "ok", "升级没有断在半路"))
    return out


def check_registry(root=None) -> list:
    """⑨ **功能注册表**（`features.registry.validate`）—— 硬门槛。

    用户 2026-09-19 的启动流程最后一句：「系统模块都运行好之后**再检查我们的
    功能模块注册这些**」——就是这一项。

    ⚠ 为什么是**硬门槛**：撞车意味着"两个功能抢同一个菜单 key / 同一个步骤名"，
      那种状态下点哪一页、跑哪一步都说不清（`run_daily` 派发时拿到的是**先注册的那个**）。
      这种错**必须在启动时当场报出来**，而不是等门店点到那一页才发现 ——
      它也不像"没配邮箱"，不是能凑合用的东西。

    ⚠ 但**注册表自己 import 不进来就退化成警告**：那多半是代码没更新完，
      升级中断那一项（`check_code`）已经在拦了，不必为它再拦一次。
    """
    try:
        from ...features import registry
        bad = registry.validate()
        n = len(registry.steps())
    except Exception as e:                                     # noqa: BLE001
        return [_item("features", "missing", "注册表读不出来：%s: %s"
                      % (type(e).__name__, e))]
    if bad:
        return [_item("features", "failed", "注册表有问题：%s" % "；".join(bad),
                      level=BLOCKING, detail={"problems": bad})]
    return [_item("features", "ok", "%d 个步骤已注册（%s）"
                  % (n, "、".join(registry.steps())))]


def check_schema(root=None, *, apply: bool = True) -> list:
    """② 库结构（M15 的迁移）—— **硬门槛，但先自己修**。

    ⚠⚠ 这里踩过一个真坑：第一版只**报**"还欠 2 条迁移"，于是
    **门店升级后第一次打开控制台会被拦在门外**（迁移要等 `daily` 才跑）。
    我上一节刚说过"自检不许硬挡"，自己就犯了一次。

    ⇒ 正确做法：**先跑一次迁移**（幂等、便宜：全跑过时只读一次 `meta.schema`），
    跑完还欠 / 跑失败 ⇒ 才算硬门槛。**"能自己修"的，不该变成"拦住用户的理由"。**
    """
    from ...storage import db as _db
    path = runlog.find_db(root)
    if not path:
        return [_item("schema", "missing", "还没有订单库（刚装完？）", level=WARNING)]
    err = ""
    if apply:
        try:
            conn = _db.connect(path)
            try:
                res = migrate.run(conn)
                if res["applied"]:
                    print("[健康] 结构迁移：%d → %d（%s）"
                          % (res["from"], res["to"],
                             "、".join(x["name"] for x in res["applied"])), flush=True)
            finally:
                conn.close()
        except Exception as e:                                 # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
    try:
        conn = _db.read_only(path)
        try:
            st = migrate.status(conn)
        finally:
            conn.close()
    except Exception as e:                                     # noqa: BLE001
        return [_item("schema", "failed", "读不出来：%s: %s" % (type(e).__name__, e),
                      level=BLOCKING)]
    if st["pending"]:
        return [_item("schema", "failed",
                      "还欠 %d 条迁移没跑成%s（%s）"
                      % (len(st["pending"]), "：" + err if err else "",
                         "、".join(x["name"] for x in st["pending"])),
                      level=BLOCKING, detail=st)]
    if err:
        return [_item("schema", "failed", "迁移没跑成：%s" % err, level=BLOCKING, detail=st)]
    return [_item("schema", "ok", migrate.describe(st), detail=st)]


def check_data(root=None, need=None) -> list:
    """③ 数据能不能算（M14 五态）—— **软警告**（旧数据照跑，功能页自己会说为什么）。"""
    from ...app import data_state as ds
    try:
        st = ds.data_state(root, need=need)
    except Exception as e:                                     # noqa: BLE001
        return [_item("data", "failed", "判据自己出错了：%s" % e, level=WARNING)]
    if st["ok"]:
        return [_item("data", "ok", "、".join(s["label"] for s in st["sources"]) + " 都正常",
                      detail=st)]
    bad = [s for s in st["sources"] if s["state"] != ds.OK]
    return [_item("data", bad[0]["state"], bad[0]["why"], level=WARNING,
                  detail={"worst": st["worst"], "lines": ds.summary_lines(st)})]


def check_timer(root=None) -> list:
    """④ 计时（**内置定时器** + 系统计划任务）—— **软警告**。

    ⚠ "注册不上系统任务"是**常态**（提权失败、组策略禁掉 schtasks），
    硬挡会让门店连手动跑一次都做不到。

    ⚠⚠ **2026-09-20 起判据变了**：干活的从"系统计划任务"换成了**服务里的定时器**
      （用户定的：内置为主、计划任务降级成只负责把服务拉起来）。
      所以"有没有定时任务"不再是"会不会自动跑"的判据 ——
      哪怕一条计划任务都没有，只要**服务开着 + 有步骤声明了唤醒时刻**，
      到点它就会跑。原来那句「没有定时任务 —— 每天那趟不会自动跑」
      现在**是错的**，会把门店吓一跳（而且它会去建一条根本不需要的任务）。
    """
    from ...desktop import schedule
    from .. import timer
    try:
        st = schedule.status(root or ROOT)
    except Exception as e:                                     # noqa: BLE001
        st = {"error": "%s: %s" % (type(e).__name__, e), "tasks": []}
    out = []
    # ① 内置定时器：这才是"会不会自动跑"的正主
    try:
        tasks = [t for t in timer.tasks(root or ROOT) if t.get("enabled") and t.get("whens")]
        nxt = timer.next_at(root or ROOT)
        if tasks:
            names = "、".join("%s %s" % (t["label"], t["when_text"]) for t in tasks)
            out.append(_item("timer", "ok",
                             "内置定时器在（%s）—— 下一趟 %s" % (names, nxt or "—"),
                             detail={"tasks": tasks, "next_at": nxt}))
        else:
            out.append(_item("timer", "missing",
                             "没有哪一步设了唤醒时刻 —— 得手动跑（双击 run-now.bat）",
                             detail={"tasks": []}))
    except Exception as e:                                     # noqa: BLE001
        out.append(_item("timer", "failed", "内置定时器读不出来：%s: %s"
                         % (type(e).__name__, e)))
    # ② 系统计划任务：**2026-09-20 起产品不再用它**（用户：「把兜底去掉吧，
    #    不用系统的计划任务」）——服务靠**开机自启**常驻，到点由上面的定时器跑。
    #    ⇒ **没有任务不是问题**（以前这里会报"没设系统计划任务"，那是误导，
    #      还会让门店去建一条根本不需要的任务）。
    #      只有"**还留着旧的**"才值得说一句：它到点白跑一趟，建议删。
    tasks = (st.get("tasks") or [])
    if st.get("error"):
        # ⚠ 读旧任务出错**不是缺失**（`state` 决定界面上红不红）——
        #   以前写成 "missing" 会跟"真没有"混成一样，而这两件事完全不同。
        out.append(_item("timer", "info",
                         "（读旧的系统计划任务时出错：%s—— 不影响自动跑）" % st["error"],
                         detail=st))
    elif tasks or st.get("installed"):
        names = "、".join((t.get("name") or "?") for t in tasks) or "（读不到详情）"
        out.append(_item("timer", "todo",
                         "这台机器上还留着旧的系统计划任务（%s）—— 现在不需要了"
                         "（到点由内置定时器跑）；建议在「设置 > 定时器设置」里删掉"
                         % names, detail=st))
    return out


def check_notify(root=None, config_path=None) -> list:
    """⑤ 推送（邮件 / 企微配了没）—— **软警告**：新店默认就是没配。"""
    from ... import config_io, mailer, wecom
    try:
        cfg = config_io.load_raw(config_path) if config_path else {}
    except Exception:                                          # noqa: BLE001
        cfg = {}
    out = []
    try:
        mc = mailer.load_mail_config(cfg, root or ROOT)
        out.append(_item("notify", "ok" if getattr(mc, "enabled", False) else "missing",
                         "邮件已配（%s）" % "、".join(mc.recipients)
                         if getattr(mc, "enabled", False) else "没配邮件（不影响别的）"))
    except Exception as e:                                     # noqa: BLE001
        out.append(_item("notify", "missing", "邮件配置读不出来：%s" % e))
    try:
        wc = wecom.load_wecom_config(cfg, root or ROOT)
        out.append(_item("notify", "ok" if getattr(wc, "enabled", False) else "missing",
                         "企微已配" if getattr(wc, "enabled", False) else "没配企微（不影响别的）"))
    except Exception as e:                                     # noqa: BLE001
        out.append(_item("notify", "missing", "企微配置读不出来：%s" % e))
    return out


def check_theme(root=None) -> list:
    """⑦ 界面（主题和壁纸）—— **软警告**：页面白板了不耽误数据，但一定要看得见。

    判据在 `modules/theme.check`（它才知道 `theme.css` 的顺序规矩）——
    这里只汇总，**不自己解析 CSS**。
    """
    from .. import theme as theme_mod
    res = theme_mod.check(root or ROOT)
    return [_item("theme", "ok" if res.get("ok") else "failed", res.get("why") or "前端文件齐",
                  detail={"items": res.get("items"), "themes": res.get("themes")})]


def check_auth(root=None, config_path=None) -> list:
    """⑧ 登录验证（云商凭据 / 华为会话 / 华为 SSO 账号）—— 待办，**永不拦启动**。

    用户 2026-09-19 的 boot flow：「启动后如果登录验证模块自检不过就弹登录」——
    **弹登录**，不是"不许启动"。所以这里是 `TODO`。

    ⚠ 判据在 `modules/auth.state`：它**只看本地证据，不联网**
      （真去 ping 一次华为就等于每天多抓一次数）。
    """
    from .. import auth as auth_mod
    from ... import config_io
    try:
        cfg = config_io.load_raw(config_path) if config_path else {}
    except Exception:                                          # noqa: BLE001
        cfg = {}
    st = auth_mod.state(cfg, root or ROOT)
    out = []
    for i in st.get("items") or []:
        out.append(_item("auth", "ok" if i["ok"] else "missing",
                         i.get("why") or "%s 正常" % i["name"],
                         level=TODO, detail={"key": i["key"], "name": i["name"],
                                             "need": i.get("need", ""),
                                             "detail": i.get("detail", ""),
                                             "need_login": bool(st.get("need_login"))}))
    return out


def check_runs(root=None) -> list:
    """⑥ 功能/能力**最近跑得怎么样**（`run_record`）—— 软警告。

    ⚠ **连着失败三次**才提级成警告：一次失败可能只是网络抖。
    """
    try:
        s = runlog.summary(root)
    except Exception as e:                                     # noqa: BLE001
        return [_item("features", "missing", "运行记录读不出来：%s" % e)]
    if not s:
        return [_item("features", "missing", "还没有运行记录（没跑过任何功能）")]
    bad = [v for v in s.values() if v.get("fail_streak", 0) >= 3]
    if bad:
        return [_item("features", "failed",
                      "%s 连着失败 %d 次（最近：%s）"
                      % ("、".join(v["kind"] for v in bad),
                         max(v["fail_streak"] for v in bad),
                         bad[0].get("last_fail_why") or "没写原因"),
                      detail={"by_kind": s})]
    return [_item("features", "ok", "%d 个功能有运行记录" % len(s), detail={"by_kind": s})]


# ------------------------------------------------------------------ 汇总
def snapshot(root=None, *, config_path=None, need=None) -> dict:
    """**一次拿全**：各组状态。**绝不抛**（界面 30 秒调一次、自检也调它）。

    ⚠ 顺序 = 用户定的启动流程：静态的（代码 / 结构）→ 能力的（计时 / 推送 / 界面）
      → 数据的（能不能算）→ 登录 → 功能注册表。
    """
    items = []
    for fn in (check_code, check_schema, check_registry):
        items += _safe(fn, root)
    items += _safe(check_data, root, need=need)
    from ..auth import runtime
    if not runtime.is_lifehall(root):
        items += _safe(check_timer, root)
        items += _safe(check_notify, root, config_path=config_path)
    items += _safe(check_theme, root)
    items += _safe(check_auth, root, config_path=config_path)
    items += _safe(check_runs, root)
    bad = [i for i in items if i["state"] not in ("ok",)]
    return {"items": items, "ok": not bad,
            "blocking": [i for i in bad if i["level"] == BLOCKING],
            "warnings": [i for i in bad if i["level"] == WARNING],
            "todos": [i for i in bad if i["level"] == TODO]}


def _safe(fn, *a, **kw) -> list:
    """跑一个自检项；**它自己炸了也不能把健康模块带崩**（退化成一条警告）。"""
    try:
        return fn(*a, **kw) or []
    except Exception as e:                                     # noqa: BLE001
        return [_item("code", "failed", "%s 自己出错了：%s: %s"
                      % (getattr(fn, "__name__", fn), type(e).__name__, e), level=WARNING)]


def boot(root=None, *, config_path=None) -> dict:
    """**启动自检** —— 按用户定的流程跑一遍，返回分级结果。

    ⚠ **它不负责"拦不拦"** —— 拦是调用方的事（`web` 起服务时看 `blocking`）。
    而且**只有 `blocking` 才拦业务**，其余一律照常启动（设计基线 §一·十一）。
    """
    st = snapshot(root, config_path=config_path)
    st["allow_start"] = not st["blocking"]      # 硬门槛不过 ⇒ 不起业务（但要能起修复页）
    return st


def boot_lines(st: dict) -> list:
    """启动时**该说的话** —— 一份话，命令行和界面都说它。

    ⚠ 只用 ASCII 能编出来的字符（`log_lines` 里连 ✓/✗ 都不给）：
      后台服务是 `pythonw.exe` 起的，stdout 按 locale 编码（中文 Windows 是 GBK），
      编不出来的字符会抛 `UnicodeEncodeError` —— 而它在 `serve_forever()` 前面，
      一抛**服务就起不来**（AGENTS.md 坑 2）。所以这里只允许 `[ok]/[!]/[ ]`。
    """
    out = []
    blocking = st.get("blocking") or []
    warn = st.get("warnings") or []
    todo = st.get("todos") or []
    out.append("  启动自检：%d 项（硬门槛 %d / 警告 %d / 待办 %d）"
               % (len(st.get("items") or []), len(blocking), len(warn), len(todo)))
    for i in blocking:
        out.append("  [!!] %s：%s" % (i.get("group_label") or i.get("group"), i.get("why")))
    if blocking:
        out.append("  [!!] 硬门槛没过 —— 控制台只给修复页；数据链路不会被调用")
    for i in warn:
        out.append("  [!]  %s：%s" % (i.get("group_label") or i.get("group"), i.get("why")))
    for i in todo:
        out.append("  [ ]  %s：%s" % (i.get("group_label") or i.get("group"), i.get("why")))
    if not (blocking or warn or todo):
        out.append("  [ok] 全过")
    return out


def update_plan(root=None, *, timer_state=None) -> dict:
    """**自动更新的策略那半**（机制在 `selfupdate`）：现在能不能更新。

    用户 2026-09-19：「健康状态**也负责自动更新**。」

    | 结果 | 什么时候 |
    |---|---|
    | `no` | 有功能正在跑 / 上次升级断在半路 / 刚更新过没多久 |
    | `later` | 今天那趟对账还没跑（等它跑完再动代码） |
    | `yes` | 上面都不拦 |

    ⚠ 返回里带 `why`（给人看）—— 自动更新做没做、为什么没做，必须写进记录里。
    """
    from ... import selfupdate, version
    # ⚠ `timer_state` 不传就**自己问计时模块要**（2026-09-20 起它能答了）——
    #   以前这儿只能等调用方喂，而没有任何调用方 ⇒ 自动更新**永远停在 `later`**
    #   （"今天那趟还没跑完"），一个谁也没注意到的死结。
    if timer_state is None:
        try:
            from .. import timer
            timer_state = timer.now(root or ROOT)
        except Exception:                                      # noqa: BLE001
            timer_state = None      # 问不出来就照旧（退化成"还没跑"，不会误判成"能更新"）
    why_no = []
    j = selfupdate.pending(root or ROOT)
    if j:
        why_no.append("上次升级断在半路（%s）—— 先修它" % (j.get("state")))
    st = snapshot(root)
    if st["blocking"]:
        why_no.append("硬门槛没过：%s" % st["blocking"][0]["why"])
    if why_no:
        return {"action": "no", "why": "；".join(why_no)}
    if timer_state and timer_state.get("running"):
        return {"action": "no", "why": "有任务正在跑"}
    if not timer_state or not timer_state.get("ran_today"):
        return {"action": "later", "why": "今天那趟对账还没跑完 —— 等它跑完再动代码"}
    return {"action": "yes", "why": "系统健康、今天那趟已跑完", "current": version.VERSION}


def auto_update(root=None, current: str = "", *, emit=None) -> dict:
    """**自动更新这一趟做不做、做了什么** —— 定时器每小时叫醒的那一步走这里。

    用户 2026-09-20：「健康模块默认注册一个自动更新，**固定一个小时执行一次**」。

    ⚠ 顺序是**先看有没有新版本、再问策略**（反过来会白问一次网络）：
      1. `selfupdate.check(force=True)` —— 没有新版本就到此为止（绝大多数小时是这样）；
      2. `update_plan()` —— `later`/`no` 就不动手，**把 `why` 带回去**
         （"没更新"必须有个说得出口的理由，否则用户只会看到"它怎么不更新"）；
      3. `apply_update()` + `restart_later()` —— 铺代码、然后起个脱离本进程的小助手
         等我们退干净再拉起服务（`restart_later` 里写了为什么不能在进程内重启）。

    ⚠ **它绝不抛**：这是定时任务的一个步骤，崩了会连累整趟的退出码。
      所有异常都变成 `{"ok": False, "why": ...}`。

    返回：`{"ok", "action": "uptodate"|"later"|"no"|"updated"|"failed", "why", ...}`
    """
    from ... import selfupdate, version

    def say(msg):
        if emit:
            try:
                emit(msg)
            except Exception:                                  # noqa: BLE001
                pass

    root = root or ROOT
    current = current or version.VERSION
    try:
        info = selfupdate.check(root, current, force=True)
    except Exception as e:                                     # noqa: BLE001
        say("  更新检查失败（不影响别的）：%s: %s" % (type(e).__name__, e))
        return {"ok": False, "action": "failed", "why": "检查更新失败：%s" % e}
    if info.get("error"):
        say("  更新检查失败（不影响别的）：%s" % info["error"])
        return {"ok": False, "action": "failed", "why": "检查更新失败：%s" % info["error"]}
    if not info.get("has_update"):
        say("  已经是最新版 v%s" % current)
        return {"ok": True, "action": "uptodate", "why": "已经是最新版", "current": current}

    latest = info.get("latest") or "?"
    say("  发现新版本 v%s（现在 v%s）" % (latest, current))
    plan = update_plan(root)
    if plan.get("action") != "yes":
        say("  这次先不动：%s" % (plan.get("why") or ""))
        return {"ok": True, "action": plan.get("action") or "later",
                "why": plan.get("why") or "", "latest": latest, "current": current}

    try:
        res = selfupdate.apply_update(root, current=current)
    except selfupdate.PartialUpdate as e:
        # ⚠ **改到一半失败了** —— 现场在 `.secrets/update/journal.json`，
        #   界面上有「修复」入口；这里只把话说清，**别重启**（当前进程是唯一能干活的那个）。
        say("  ⚠ 更新改到一半失败了：%s（界面上有「修复」）" % e)
        return {"ok": False, "action": "failed", "why": str(e), "partial": True,
                "latest": latest, "current": current}
    except Exception as e:                                     # noqa: BLE001
        say("  ⚠ 更新失败：%s: %s" % (type(e).__name__, e))
        return {"ok": False, "action": "failed", "why": str(e),
                "latest": latest, "current": current}

    restarting = selfupdate.restart_later(root)
    say("  已更新到 v%s，%s" % (res.get("to") or latest,
                               "正在重启服务…" if restarting else
                               "但自动重启没成功 —— 下次开机/手动 start.bat 才生效"))
    out = dict(res)
    out.update({"ok": True, "action": "updated", "restarting": bool(restarting),
                "latest": latest, "current": current,
                "why": "已更新到 v%s" % (res.get("to") or latest)})
    return out
