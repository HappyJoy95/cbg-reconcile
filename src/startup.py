"""每天**服务刚起来时**自动更新一次数据。

用户 2026-09-18：「加个**每天系统刚启动时**自动更新数据的逻辑呗，**具体更新什么我们再定**」。

所以这里只做**机制**，不写死要更新什么：

* **一天只跑一次** —— 记在 `.secrets/startup-refresh.json`（按**日期**记，不是"跑过没"）。
  ⚠ 门店那台机器可能一天开关好几次，不记的话每次开机都去拉一遍，
    白白撞云商限流（那个限流会要图形验证码，很烦）。
* **只在服务启动时跑**，不在每次请求里跑。
* **后台线程、且排在启动提示之后** —— 绝不能拖慢控制台起来。
  ⚠ 启动路径上出任何事都不能让服务起不来（见 AGENTS.md 坑 2）：
    所以整个函数体包在 try 里，只往日志/标准输出写，不往上抛。
* ⚠ **跟每天那条定时任务是两件事**：定时任务（`daily`）管**抓数 + 算 + 推**，
  这个只管**把界面要用的东西刷新一下**（列表现状、组织架构）。
  两者互不依赖 —— 这台机器可能压根没注册定时任务。

## 「具体更新什么」在哪加

往 `TASKS` 里加一条就行。每条 `(名字, 函数)`，函数收一个 `app`。
⚠ 每条独立 try：**一条坏了不许拖累后面的**，也不许让整次刷新记成"没跑过"
（不然下次开机又从头再来一遍，坏的那条会一直挡着）。
"""

from __future__ import annotations

import datetime
import json
import threading
import traceback
from pathlib import Path
from typing import Callable, List, Tuple

#: 刷新记录 —— 按日期记。`{"last": "2026-09-18"}`。跟账号一个性质（这台机器自己的），
#: 所以放 `.secrets/`，自更新不碰。
STATE_REL = ".secrets/startup-refresh.json"


def _state_path(root) -> Path:
    return Path(root) / STATE_REL


def last_run(root) -> str:
    try:
        return str(json.loads(_state_path(root).read_text(encoding="utf-8")).get("last") or "")
    except (OSError, ValueError):
        return ""


def should_run(root, today: str = "") -> bool:
    """今天跑过没。**按日期比**，不是"有没有记录"。"""
    today = today or datetime.date.today().isoformat()
    return last_run(root) != today


def mark(root, today: str = "") -> None:
    today = today or datetime.date.today().isoformat()
    try:
        p = _state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last": today}), encoding="utf-8")
    except OSError:
        # 记不上就记不上 —— 大不了下次开机再刷一遍，不值得让启动失败
        pass


# ---------------------------------------------------------------- 要刷新什么
def _refresh_roster(app) -> str:
    """组织架构 + 本店人员名单。

    ⚠ 这两样都是**「人员设置」和门禁判据要用**的：云商那边加了人、调了店，
      这里不刷新的话界面会一直显示旧的（而人不会知道要去点刷新）。

    ⚠ 2026-09-19（阶段 2 的 2.3）：原来这里 `from . import web` —— 启动刷新
      **反向依赖 HTTP 层**。现在调的是执行模块 `features.store.staff`，这条依赖断了。
      （`app` 参数是**鸭子类型**：只用到 `root` / `config_path` / `erp_env_file()`。）
    """
    from .features.store import staff as app_staff
    st = app_staff.staff_state(app.root, app.config_path, app.erp_env_file())
    if not st.get("ok"):
        return "跳过：%s" % st.get("error")
    return "本店人员 %d 人（在职 %d）" % (st["count"], st["active_count"])


def _refresh_version(app) -> str:
    """看一眼有没有新版本（走缓存，一天一次正好）。"""
    from . import selfupdate, version
    u = selfupdate.check(app.root, version.VERSION)
    return "有新版本 v%s" % u["latest"] if u.get("has_update") else "已是最新"


#: 要刷新的事 —— **加东西就加在这儿**。函数收 `app`，返回一句给人看的结果。
TASKS: List[Tuple[str, Callable]] = [
    ("门店组织架构 / 本店人员", _refresh_roster),
    ("检查更新", _refresh_version),
]


def tasks(root=None) -> List[Tuple[str, Callable]]:
    """按版本给启动刷新清单；生活馆不加载云商人员链。"""
    from .modules.auth import runtime
    if runtime.is_lifehall(root):
        return [task for task in TASKS if task[0] != "门店组织架构 / 本店人员"]
    return list(TASKS)


def run_once(app, force: bool = False, say=print) -> dict:
    from .modules.auth import runtime_guard
    with runtime_guard.guard(app.root) as acquired:
        if not acquired:
            return {"ran": False, "results": {}, "reason": "有任务正在使用本机运行身份，稍后重试启动刷新"}
        return _run_once(app, force=force, say=say)


def _run_once(app, force: bool = False, say=print) -> dict:
    """今天还没跑过就跑一轮。返回 `{"ran": bool, "results": {名字: 结果}}`。

    ⚠ **一条坏了不影响别的**，也不影响"记成今天跑过" —— 见模块开头那段。
    """
    if not force and not should_run(app.root):
        return {"ran": False, "results": {}, "reason": "今天已经刷过了"}
    results = {}
    for name, fn in tasks(app.root):
        try:
            results[name] = fn(app) or "好了"
        except Exception as e:                                 # noqa: BLE001
            # 启动路径上不许抛 —— 顶多这句没刷成
            results[name] = "失败：%s: %s" % (type(e).__name__, e)
            say("[启动刷新] %s 失败：%s" % (name, traceback.format_exc(limit=2)))
    mark(app.root)
    for name, r in results.items():
        say("[启动刷新] %s：%s" % (name, r))
    return {"ran": True, "results": results}


def start_background(app, say=print) -> threading.Thread:
    """服务启动时调一次：**后台线程**跑，绝不阻塞启动。

    ⚠ 整个函数体包 try：`serve()` 里起不来是最难查的一种坏（见 AGENTS.md 坑 2）。
    """
    def worker():
        try:
            run_once(app, say=say)
        except Exception:                                      # noqa: BLE001
            say("[启动刷新] 整轮失败（不影响服务）：%s" % traceback.format_exc(limit=3))

    t = threading.Thread(target=worker, name="startup-refresh", daemon=True)
    t.start()
    return t
