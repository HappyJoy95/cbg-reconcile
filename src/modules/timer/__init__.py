"""**计时模块** —— 「到点叫醒谁」，以及**开机自启**。

用户 2026-09-19 定的位置：

> 功能模块……**被计时模块唤醒**

## 这个模块管什么、不管什么

| 管 | 不管 |
|---|---|
| 每天几点跑、跑哪几步（**唤醒计划**） | 每一步怎么跑（那是功能模块自己的 `run`） |
| 计划任务怎么注册 / 开机自启 | 登录（`auth`）、抓数（`fetch`）、推送（`notify`） |
| "上次跑成没成"（读 `run_record`） | 记日志本身（写的那半在 `storage/runlog.py`） |

⚠ **唤醒计划只能有一份**：`.secrets/schedule.json` 里勾的那几步，
和界面菜单、和 `run_daily` 实际跑的，是同一份东西（`features/registry.py` 派生）。
历史上这三处各写一份，出过"界面上关了 POS，任务里还在跑"。

## ⭐ 唤醒接口（用户 2026-09-20）

> 「要给个接口，**让各个模块设置什么时间点唤醒，以及唤醒做什么**。
>   然后**定时器看到到点了就做这个**。设置要可以设置**日期、时间，每周几，每个月几号**这种」

| 谁回答什么 | 在哪儿 |
|---|---|
| **做什么** | `Step(cmd=…)` —— 功能模块的注册表里 |
| **什么时候**（默认值） | `Step(whens=(When(…),))` —— 同上，**模块自己声明** |
| **什么时候**（这台机器改过的） | `.secrets/wakes.json` —— 界面写的，见 `store.py` |
| **跑不跑** | 「定时器设置」里每一步的开关滑块（`timer.set_enabled`）—— **不新增第二个开关**<br>⚠ 2026-09-21 晚：`schedule.automation_steps`（"自动化跑什么"）删了 —— "跑什么"只有注册表一处 |
| 到点了干什么 | 本模块：`tasks()` → `due()` → `tick()`；详细设计见
  `.dsh/docs/2026-09-20-M10-内置定时器与唤醒接口-详细设计.md` |

⚠ 三条硬规矩（`due()` 里逐条实现，别绕开）：

1. **到点后只有 30 分钟窗口**（`WAKE_WINDOW_MINUTES`）—— 过了就不补（用户选的）；
2. **同一个 slot 只跑一次**，而且**按"试过没"去重，不按"成没成"** ——
   失败也记一条。按成功去重的话，一次失败会被每 30 秒重试一遍，
   把机器和云商账号一起打爆（云商**不能并行登录**，见 `lockfile.py`）；
3. **正在跑就不开第二趟**。

## 为什么 `status()` 要把三样合起来看

门店报"今天没跑"的可能有四种：任务没注册 / 注册了但脚本没了 /
注册了也跑了但失败了 / 跑了也成功了（那就是数据的事，不在这儿）。
`status()` 一次把四种分开说，别让人挨个页面翻。
"""

from __future__ import annotations

import datetime
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

#: 把「唤醒时刻」这个值对象对外暴露 —— 功能模块声明自己的时间点时用它：
#:     from ....modules.timer import When
#:     Step(cmd="attain", label="销售达成", whens=(When(kind="weekly", weekdays=(1,)),))
from .when import DEFAULT_TIME, KINDS, When, describe  # noqa: F401

#: ⭐ **一次性注册**（"从现在起 N 秒后跑一次，跑完把登记删掉"）—— 用户 2026-09-21 定的。
#: 实现和完整说明都在 `once.py`，这儿只把它**当出口暴露**（调用方写 `timer.register_once(...)`）：
#:     from ....modules import timer
#:     timer.register_once(root, "report", after=300, note="保存人员设置")
#: ⚠ 它跟上面的 `When`（静态时刻表）是**两回事**：那个是"每天都跑"，这个是"就跑这一次"。
from . import once as once_mod                         # noqa: F401
from .once import ONCE_GRACE_HOURS, REL as ONCE_REL    # noqa: F401
from .once import KIND as ONCE_KIND                    # noqa: F401
from .once import cancel as cancel_once                # noqa: F401
from .once import load as once_list                    # noqa: F401
from .once import register as register_once            # noqa: F401
from .once import seconds_left as once_seconds_left    # noqa: F401
from .once import tasks as once_tasks                  # noqa: F401

#: 一天里"今天这趟跑过了吗"的判定窗口（小时）—— 定时任务一般设在凌晨
TODAY_WINDOW_HOURS = 20

#: 到点之后**容忍多久**（分钟）。过了这个窗口就**不补跑**（用户 2026-09-20 选的：
#: 「不补，错过了就等下一个时间点」）。
#: ⚠ 30 分钟是给这些情况留的余地：服务正好在重启、机器刚从睡眠里醒、
#:   系统计划任务刚把服务拉起来（它比到点晚几十秒很常见）。
WAKE_WINDOW_MINUTES = 30

#: 心跳间隔（秒）。⚠ 比窗口小得多就行 —— 30 秒意味着最坏晚 30 秒开跑。
HEARTBEAT_SECONDS = 30

#: 唤醒记录的类型（`run_record.kind`）。写入者是**真跑那一步的那个进程**
#: （`daily --wake-slot …` 收尾时写），不是定时器 —— 这样"记的"和"发生的"是同一件事。
WAKE_KIND = "wake"

#: 正在派发的那一趟（跨"跳"记着）。派发时写、下一跳发现不在跑了就**搬进"跑过"台账**。
#: 放 `.secrets/`：这台机器自己的，自更新不碰。
STATE_FILE = ".secrets/wake-current.json"

#: **跑过的 slot 台账**（最近 50 条）。⚠ 别省掉它，理由见 `_mark_done()`。
DONE_FILE = ".secrets/wake-done.json"
DONE_KEEP = 50

#: 派发时用的命令行前缀 —— 和手动「跑一次」、和系统计划任务**同一条路**。
_SELF = lambda: [sys.executable or "python", "-u", "-m", "src.cli"]  # noqa: E731


def steps() -> list:
    """能被唤醒的步骤（**唯一来源** = 功能模块的注册表）。"""
    from ...features import registry
    return registry.all_steps()


def plan(root=None) -> dict:
    """**唤醒计划** —— 这台机器实际会跑哪几步。

    ⚠ 2026-09-20 **语义改过**：原来是"`.secrets/schedule.json` 里勾选的步骤"，
      而那个"自动化跑什么"的设置取消了 ⇒ 现在 = **注册到定时器的那几步**
      （`enabled_cmds()`：默认全都在，除了被那个开关关掉的）。
    """
    from ...features import registry
    from ...paths import ROOT
    root = Path(root) if root else ROOT
    try:
        picked = list(enabled_cmds(root))
        why = ""
    except Exception as e:                                     # noqa: BLE001
        picked = list(registry.steps())
        why = "读不出「注册到定时器」的步骤（%s），按全跑处理" % e
    by = {s.cmd: s for s in steps()}
    return {"picked": picked,
            "steps": [by[c] for c in picked if c in by],
            "unknown": [c for c in picked if c not in by],   # 老配置里留下的、现在已经没有的步骤
            "labels": [registry.step_labels().get(c, c) for c in picked],
            "why": why}


def status(root=None) -> dict:
    """定时任务 + 开机自启 + **上次跑成没成**，一处看全。"""
    from ...desktop import autostart, schedule
    from ...paths import ROOT
    from ...storage import runlog
    root = Path(root) if root else ROOT
    out = {"platform": schedule.kind(), "problems": []}
    for key, fn in (("task", lambda: schedule.status(root)),
                    ("boot", lambda: autostart.status(root))):
        try:
            out[key] = fn()
        except Exception as e:                                 # noqa: BLE001
            out[key] = {"installed": False, "why": "%s: %s" % (type(e).__name__, e)}
            out["problems"].append("%s 读不出来：%s" % (key, e))
    try:
        out["plan"] = plan(root)
    except Exception as e:                                     # noqa: BLE001
        out["plan"] = {"picked": [], "steps": [], "labels": [], "why": str(e)}
    out["last_run"] = last_run(root)
    # ⚠ 现在是**按唤醒计划算**（各模块声明的时间点），不再看系统计划任务的注册时间
    out["next_run"] = next_run(root)
    for cond, msg in ((not (out.get("task") or {}).get("installed"),
                       "还没注册定时任务 —— 每天不会自己跑"),
                      (not (out.get("boot") or {}).get("registered"),
                       "没设开机自启 —— 重启后要手动启动服务")):
        if cond:
            out["problems"].append(msg)
    return out


def last_run(root=None) -> dict:
    """上次那趟日常流程（`run_record` 里 kind = `daily`）。

    没有记录时回 `{}` —— **"没记过"和"跑了但没记"是两件事**，
    这里只能说"没记过"（界面上也要这么说，别写成"从没跑过"）。
    """
    from ...storage import runlog
    rows = [r for r in runlog.recent(root, limit=50, kind="daily")]
    if not rows:
        return {}
    r = rows[0]
    return {"started_at": r.get("started_at") or "", "finished_at": r.get("finished_at") or "",
            "ok": bool(r.get("ok")), "why": r.get("why") or "", "note": r.get("note") or "",
            "detail": r.get("detail") or {}}


# ⚠ 这儿原来有个 `next_run(task: dict)`：**按系统计划任务的注册时间**推下一次。
#   2026-09-20 删掉了，两个原因：
#     ① 那条任务现在只负责"把服务拉起来"（用户：「不用系统的计划任务」），
#        它的时间**不决定**任何事 —— 留着会让人以为"下一次 = 那条任务的时间"；
#     ② 它和下面那个**按唤醒计划算**的 `next_run()` **重名** ——
#        后者会把它盖掉，而"盖掉"是静默的（`test_module_layout` 里那条
#        「不许有重名的顶层定义」把它抓出来了）。

def installed(root=None) -> bool:
    """有没有在计时（任务或开机自启有一个就算）—— 启动自检用。"""
    st = status(root)
    return bool((st.get("task") or {}).get("installed")
                or (st.get("boot") or {}).get("registered"))


def install(root, time_str: str, **kw) -> dict:
    """注册每天那趟（转发 `schedule.install`，**参数校验也归它**）。"""
    from ...desktop import schedule
    return schedule.install(Path(root), time_str, **kw)


def remove(root=None, name: str = "") -> dict:
    """删一条（不传名字就删我们的全部）—— 卸载 / 换时间用。"""
    from ...desktop import schedule
    return schedule.remove(name, root=root) if name else schedule.remove_all(root)


def boot_install(root, elevated=None) -> dict:
    """设开机自启 —— `elevated=None` = **普通权限**（用户 2026-09-16 定的默认）。"""
    from ...desktop import autostart
    return autostart.install(root, elevated=elevated)


def boot_remove(root=None) -> dict:
    from ...desktop import autostart
    return autostart.remove(root)


# ═══════════════════════════════════════════════════════ 唤醒：谁、什么时候、做什么
# 设计文档：`.dsh/docs/2026-09-20-M10-内置定时器与唤醒接口-详细设计.md`


def _root_of(root) -> Path:
    """`root` 没给 = **这台机器**（`ROOT`）—— 全项目只有 `paths.py` 知道自己在第几层。

    ⚠ 统一走这一个出口：我第一版在几个函数里写了 `Path(root) if root else None or ROOT`，
      运算符优先级把它变成"永远走 else"，而 `ROOT` 当时又没 import ——
      **根传空就 NameError**，而所有用例都传了 `root`，所以一条测试都没红。
    """
    from ...paths import ROOT
    return Path(root) if root else ROOT


def wakes_of(root=None) -> dict:
    """`{cmd: [When, …]}` —— 这台机器**实际**的唤醒时刻（注册表默认 ⊕ 改过的）。

    ⚠ 只有声明了 `whens` 的步骤在里面（`registry.wakes()`）——
    "没声明" = 只手动跑，**不是**"默认每天 21:00"。
    """
    from . import store
    root = _root_of(root)
    over, _problems = store.load(root)
    out = {}
    for s in steps():
        if not s.whens:
            continue
        entry = over.get(s.cmd) or {}
        out[s.cmd] = list(entry.get("whens") or s.whens)
    return out


def wake_problems(root=None) -> list:
    """唤醒设置读出来的问题（界面/自检能显示）。**绝不抛。**"""
    from . import store
    try:
        return store.load(_root_of(root))[1]
    except Exception as e:                                     # noqa: BLE001
        return ["唤醒设置读不出来：%s: %s" % (type(e).__name__, e)]


def enabled_cmds(root=None) -> tuple:
    """**注册到定时器的那几步** —— 就是各模块设置页里那个**开关滑块**打开的那些。

    用户 2026-09-20：「**开了就注册到定时器，不开就不注册**」。

    ⚠ 缺省是**全部打开**（`store.enabled_of` 里那条"没写过就是真"）——
      所以"没动过设置"的机器行为和以前一样：声明的步骤都跑。
    ⚠ 关掉的那一步**时间点还留着**（只是不注册），再打开还是原来那个时间。
    """
    from ...features import registry
    from . import store
    root = _root_of(root)
    return tuple(s.cmd for s in registry.wakes() if store.enabled_of(root, s.cmd))


def tasks(root=None) -> list:
    """**任务表** —— 界面那张表就是它，定时器也照它跑。

    每个步骤一行：`{cmd, label, order, whens, when_text, enabled, declared, overridden}`
    `whens` 是给机器看的（`as_dict` 后的形状），`when_text` 是给人看的。
    """
    from . import store
    from ...features import registry
    root = _root_of(root)
    over, _problems = store.load(root)
    picked = enabled_cmds(root)
    clock = datetime.datetime.now()
    out = []
    # ⚠⚠ 2026-09-21（用户：「**相同时间执行的任务，按照定时器这个列表从上到下执行**」）——
    #   表格**从上到下就是执行顺序**（同一时刻到点的几步按它排，见 `due()` 的排序）。
    #   门店在界面上调过就用它存的 `order`；没调过用模块声明的 `Step.order`。
    #   ⚠ 排序要**稳定**：同一序号时按注册表顺序（`all_steps()` 已经按 order 排过），
    #     所以用 `enumerate` 的下标当第二关键字，别拿 cmd 字符串排（那样 `attain`
    #     会跑到 `dump` 前面，看着像随机的）。
    rows = []
    from ..auth import runtime
    for _i, s in enumerate(steps()):
        if not runtime.step_available(s.cmd, root, recurring=True):
            continue
        entry = over.get(s.cmd) or {}
        mine = list(entry.get("whens") or s.whens)
        # 这一步**下一次**什么时候跑（界面「下次」那一列）。
        nxt = _next_of(clock, mine)
        # ⚠ 归属（哪个模块的）**从注册表推**，界面上不另写一份 ——
        #   "每个模块的设置页只显示自己那几步"要靠它（用户 2026-09-20）。
        owner = registry.step_owner(s.cmd)
        mine_order = entry.get("order")
        rows.append({
            "_i": _i,
            "next_text": nxt.strftime("%m-%d %H:%M") if nxt else "—",
            "owner_key": owner["key"], "owner_label": owner["label"],
            "cmd": s.cmd, "label": s.label,
            # ⚠ 执行顺序 = 这台机器调过的那个；没调过 = 模块声明的 `Step.order`
            "order": (mine_order if isinstance(mine_order, int)
                      and not isinstance(mine_order, bool) else s.order),
            "whens": [w.as_dict() for w in mine],
            "when_text": describe(mine),
            # ⚠ "声明了没有"和"用户改过没有"都要能看出来 ——
            #   界面上要能说"这是跟着代码走的默认值"还是"这台机器自己设的"。
            "declared": bool(s.whens),
            "overridden": bool(entry.get("whens")),
            # ⭐ 那个开关滑块（用户：「开了就注册到定时器，不开就不注册」）：
            #   `enabled` = 现在注册没注册；`switched` = **这台机器动过这个开关没有**
            #   （界面上区分"默认开着"和"我手动开/关过"）。
            "enabled": s.cmd in picked,
            "switched": "enabled" in entry,
            # ⭐ **这个开关不许关**（用户 2026-09-20：「自动更新和数据抓取模块
            #   不允许关闭」）—— 前端据此把滑块画成"锁死的"，后端也会拒。
            "required": bool(getattr(s, "required", False)),
        })
    rows.sort(key=lambda r: (r["order"], r["_i"]))
    for r in rows:
        r.pop("_i", None)
    return rows


def _next_of(now: datetime.datetime, whens) -> Optional[datetime.datetime]:
    """这几个时刻里**最近的一个**（严格晚于 `now`）。取不到给 `None`。

    ⚠⚠ **算的是"这一步自己的"下一次，不是全局那个**（2026-09-21 用户：
      「『dump』改成：每天 21:00（**下一趟 2026-09-21 15:17**）。这个下一趟是自动更新的，
      **不要显示自动更新的**」）——
      全局那个是 `next_run()`，它把**所有**步骤放一起取最近的一个；而自动更新
      每小时都跑（:17），所以白天任何时候它都是"最近的那一趟"。
      拿它当某一步的"下一趟" ⇒ 门店改完 `dump` 的时间，看到的是自动更新的时间，
      一头雾水（这已经不是第一次被这条坑到了：页面上那列早就改成本步自己的了）。
    ⚠ 所以"某一步下一次什么时候跑"**只此一处**：`tasks()` 的「下次」列、
      改时间的提示语、开开关的提示语，全走它。
    """
    best = None
    for w in whens or ():
        try:
            cand = (w if isinstance(w, When) else When.from_dict(w)).next_after(now)
        except ValueError:
            continue
        if cand and (best is None or cand < best):
            best = cand
    return best


def next_of(root=None, cmd: str = "", now=None) -> dict:
    """**这一步**下一次什么时候跑 → `{"at": "YYYY-MM-DD HH:MM", "at_text": "今天 21:00"}`。

    关着的步骤 / 没有时刻的步骤 ⇒ `{"at": "", "at_text": "", "off": True}` ——
    提示语里要能说清"它现在不会跑"，别显示一个它根本不会跑的时间。
    """
    root = _root_of(root)
    now = now or datetime.datetime.now()
    if not cmd:
        return {"at": "", "at_text": "", "off": True}
    row = None
    for t in tasks(root):
        if t["cmd"] == cmd:
            row = t
            break
    if row is None:
        return {"at": "", "at_text": "", "off": True}
    if not row["enabled"]:
        return {"at": "", "at_text": "", "off": True}
    nxt = _next_of(now, [When.from_dict(w) for w in row["whens"]])
    if nxt is None:
        return {"at": "", "at_text": "", "off": True}
    return {"at": nxt.strftime("%Y-%m-%d %H:%M"), "at_text": at_text(nxt, now),
            "off": False}


#: 两个**数据抓取**（用户 2026-09-23）：「顺序排在最前面且同步的，
#: 后面的时间不能早于这个时间」—— 界面调顺序 / 改时间都按这三条钉死。
FETCH_CMDS = ("dump", "erp-dump")


def set_order(root, cmds) -> dict:
    """**改执行顺序**（用户 2026-09-21：「定时器列表给个调顺序的功能」）。

    `cmds` = 从上到下的那串 `cmd`（界面把整张表的顺序发过来）。
    ⚠ 认不出来的 `cmd` **当场抛错，不回落** —— 落回的后果是"界面上调了顺序、
      实际按老顺序跑"（这个项目最怕的一类：界面说一套、跑的是另一套）。
    ⚠ 只改**顺序**：`whens` / `enabled` 一个字都不动。
    ⚠ **同一个时间点**的几步才受它影响（不同时间的本来就各跑各的）。
    ⭐ 2026-09-23：`dump` / `erp-dump` **永远钉在名单最前**（先玲珑后云商）——
      其余步骤保持界面发上来的相对顺序。名单里没点名抓取时也不动它们的
      `Step.order`（10/20），所以 partial 名单照样安全。
    """
    from . import store
    _check_cmd_list(cmds)
    given = [str(x) for x in (cmds or ())]
    got = [c for c in FETCH_CMDS if c in given]
    got += [c for c in given if c not in FETCH_CMDS]
    store.set_order(_root_of(root), got)
    return {"ok": True, "order": got,
            "text": " → ".join(_label_of(c) for c in got)}


def _declared(cmd: str):
    """这一步**模块声明的**时刻（这台机器没改过它时，生效的就是它）。"""
    from ...features import registry
    step = registry.step_by_cmd(cmd)
    return list(getattr(step, "whens", ()) or ())


def _check_cmd_list(cmds) -> None:
    """顺序名单里的**每一个**都得是认得的步骤 —— 认不出来就抛（见 `set_order`）。"""
    from ...features import registry
    known = {s.cmd for s in registry.all_steps()}
    bad = [str(x) for x in (cmds or ()) if str(x) not in known]
    if bad:
        raise ValueError("不认识的步骤：%s（只认 %s）"
                         % ("、".join(bad), "、".join(sorted(known))))
    if not cmds:
        raise ValueError("顺序名单是空的 —— 要调顺序至少得给一步")


def _when_clock_min(w) -> Optional[int]:
    """这一刻的**钟点**（当天第几分钟）；`hourly` / 没有点数的给 `None`（不参与比较）。"""
    if not isinstance(w, When):
        w = When.from_dict(w)
    if w.kind == "hourly":
        return None
    try:
        hh, mm = w.hhmm
    except ValueError:
        return None
    return int(hh) * 60 + int(mm)


def _whens_clock_min(whens) -> Optional[int]:
    """这几个时刻里**最早**的钟点（多时刻取 min；全没有给 `None`）。"""
    mins = [_when_clock_min(w) for w in (whens or ())]
    mins = [m for m in mins if m is not None]
    return min(mins) if mins else None


def _effective_whens(root, cmd: str) -> list:
    """这一步**现在生效的**时刻（改过的用改过的，否则用模块声明的）。"""
    from . import store
    stored = list(store.whens_of(_root_of(root), cmd))
    return stored if stored else _declared(cmd)


def _hhmm_text(minute: int) -> str:
    return "%02d:%02d" % (int(minute) // 60, int(minute) % 60)


def _assert_fetch_floor(root, cmd: str, whens) -> None:
    """「后面的时间不能早于数据抓取」—— 改**非抓取**时校验新时刻。"""
    if cmd in FETCH_CMDS:
        return
    floor = _whens_clock_min(_effective_whens(root, "dump"))
    new = _whens_clock_min(whens)
    if floor is None or new is None:
        return
    if new < floor:
        raise ValueError(
            "「%s」不能早于数据抓取（%s）—— 先抓数才能算后面那几步"
            % (_label_of(cmd), _hhmm_text(floor)))


def _assert_others_not_earlier(root, whens) -> None:
    """改**抓取时刻**时：别的步骤里有没有谁会因此变得比它还早。"""
    floor = _whens_clock_min(whens)
    if floor is None:
        return
    from ...features import registry
    early = []
    for s in registry.all_steps():
        if s.cmd in FETCH_CMDS or not s.whens:
            continue
        if s.cmd == "autoupdate":
            continue                      # 每小时内部步骤，不进这张表
        m = _whens_clock_min(_effective_whens(root, s.cmd))
        if m is not None and m < floor:
            early.append("%s（%s）" % (_label_of(s.cmd), _hhmm_text(m)))
    if early:
        raise ValueError(
            "数据抓取改成 %s 会比后面这些还早：%s —— 先把它们调到不早于 %s"
            % (_hhmm_text(floor), "、".join(early), _hhmm_text(floor)))


def set_whens(root, cmd: str, whens) -> dict:
    """改一步的唤醒时刻（界面调它）。空列表 = **恢复默认时间**（开关不动）。

    ⚠ 认不出来的 `cmd` **抛错，不回落**：改空气的后果是"界面显示改了、
      实际还在按老时间跑"，而那种错没人看得出来。
    ⭐ 2026-09-23（用户）：两个数据抓取**时间同步**；后面步骤的钟点
      **不能早于**抓取（见 `_assert_*`）。
    """
    from . import store
    _check_cmd(cmd)
    got = []
    for w in (whens or []):
        got.append(w if isinstance(w, When) else When.from_dict(w))
    root = _root_of(root)
    # ⚠ 先记下"改之前**生效的**那份"，好回答"到底改没改"——
    #   点「保存」但一个字没动时，界面该说「没改动」，而不是说「改成…」
    #   （2026-09-21 用户：「单击保存会弹出来一些奇怪的东西，会突然消失」）。
    was = [w.as_dict() for w in (list(store.whens_of(root, cmd)) or _declared(cmd))]
    from ...features import registry
    step = registry.step_by_cmd(cmd)
    effective = got or list(getattr(step, "whens", ()) or ())
    # ⭐ 先校验再落盘（拒了就一个字都不写）
    if cmd in FETCH_CMDS:
        _assert_others_not_earlier(root, effective)
    else:
        _assert_fetch_floor(root, cmd, effective)
    store.set_cmd(root, cmd, got)
    synced = []
    if cmd in FETCH_CMDS:
        # 两个抓取**同步**：改谁都是改这一对
        for other in FETCH_CMDS:
            if other == cmd:
                continue
            store.set_cmd(root, other, got)
            synced.append(other)
    # ⚠⚠ **空列表 = 恢复默认**，回给界面的那句话得说"**恢复之后实际是什么**"。
    #   直接 `describe(got)` 的话，`got` 是空的 ⇒ 说成「**只手动跑**」——
    #   而"只手动跑"的意思是"这一步**没有**时刻"，跟"恢复成默认的 21:00"是两回事。
    #   2026-09-21 用户点「默认」就撞上了：「『attain』改成：**只手动跑**」
    #   （他没看错：那一刻**存下来的**确实是空 —— 空 = 跟随模块声明的默认值，
    #    见 `tasks()` 里 `entry.get("whens") or s.whens`）。
    #   ⇒ 报的是**生效的**那一份（`got` 空 ⇒ 取模块自己声明的 `Step.whens`）。
    res = {"ok": True, "cmd": cmd, "label": _label_of(cmd),
           # 真改了没（一个字没动 ⇒ False）—— 提示语据此分开说
           "changed": [w.as_dict() for w in effective] != was,
           "whens": [w.as_dict() for w in got],
           "restored": not got,                    # 这一次是"恢复默认"吗
           "text": describe(effective),            # ⚠ 生效的那一份，不是存的那一份
           "next": next_of(root, cmd)}
    if synced:
        res["synced"] = [cmd] + synced
    return res


def set_enabled(root, cmd: str, on: bool) -> dict:
    """**那个开关滑块**：开了就注册到定时器，不开就不注册（用户 2026-09-20）。

    ⚠ 关掉**不抹掉时间点** —— 再打开还是原来那个时间（`store` 里两个字段分开存）。
    ⚠⚠ **必做的那几步关不掉**（用户：「自动更新和数据抓取模块不允许关闭」）——
      后端**必须拒**，不能只靠前端把滑块画灰：前端画灰只是"看起来不能点"，
      curl / 老缓存页面照样能关，而关掉的后果（库永远是旧的）**看不出来**。
    """
    from . import store
    from ...features import registry
    _check_cmd(cmd)
    step = registry.step_by_cmd(cmd)
    # ⚠ 2026-09-21：**没有唤醒时刻的步骤**（`whens=()`，「上报数据」就是）开关点不动 ——
    #   因为它没有时刻可注册（跟着每天那趟跑）。原来这里是**静默无效果**：
    #   界面上的开关永远是关的、点了没反应，看着像坏了。
    #   ⇒ 明确拒，并说清它跟谁跑（前端现在也不画那个开关了，两头都要有）。
    if step is not None and not step.whens and on:
        raise ValueError(
            "「%s」没有自己的唤醒时刻 —— 它跟着每天那趟一起跑，不用开关"
            % getattr(step, "label", cmd))
    if not on and step is not None and getattr(step, "required", False):
        raise ValueError("「%s」是必做的，关不掉（%s）—— 时间可以改，开关不给关"
                         % (step.label, REQUIRED_WHY.get(cmd, "关掉之后没法自动恢复")))
    root = _root_of(root)
    store.set_enabled(root, cmd, bool(on))
    nxt = next_run(root)
    mine = next_of(root, cmd)          # ⚠ **这一步**的下一趟（不是全局那个，见 `_next_of`）
    label = _label_of(cmd)
    # ⚠ 两句话**分开拼**：塞进一个三元里再 `%` 的话，两支的占位符个数不一样，
    #   少传一个就 `TypeError: not all arguments converted`（当场踩到）。
    if on:
        msg = "「%s」已注册到定时器（下一趟 %s）" % (
            label, mine.get("at_text") or "—")
    else:
        msg = "「%s」不再注册到定时器 —— 到点不会叫它了（时间点留着，随时可以再打开）" % label
    return {"ok": True, "cmd": cmd, "enabled": bool(on),
            "registered": bool(on),
            "next_at": nxt.get("at", ""),
            "message": msg}


def _check_cmd(cmd: str) -> None:
    """认不出来的步骤**抛错，不回落**（写空气没人看得出来）。"""
    from ...features import registry
    if not registry.step_by_cmd(cmd):
        raise ValueError("没有这一步：%s（认得的：%s）"
                         % (cmd, "、".join(s.cmd for s in steps())))


def _label_of(cmd: str) -> str:
    from ...features import registry
    step = registry.step_by_cmd(cmd)
    return step.label if step else cmd


def _order_of(cmd: str) -> int:
    """这一步在注册表里的排序 —— 一次性任务也要能跟同时到点的静态任务**排在一起**
    （表里的顺序 = 跑的顺序，用户 2026-09-21 定的）。认不出来的排最后。"""
    from ...features import registry
    step = registry.step_by_cmd(cmd)
    return getattr(step, "order", 0) or 0


# ─────────────────────────────────────────────────────────── 到点判定（三条）


def _slot_text(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _done_path(root) -> Path:
    return Path(root) / DONE_FILE


def _done_list(root) -> list:
    """跑过的 slot 台账（新的在前）。读不出来当空 —— **绝不抛**。"""
    try:
        d = json.loads(_done_path(root).read_text(encoding="utf-8"))
        items = d.get("done") if isinstance(d, dict) else d
        return [x for x in (items or []) if isinstance(x, dict)]
    except (OSError, TypeError, ValueError):
        return []


def _mark_done(root, slot_text: str, steps) -> None:
    """把"这个 slot 派过了"记到**本地台账**。

    ⚠⚠ 为什么除了 `run_record` 还要这一份：那条记录是**子进程**写的，
      而它**写不成的时候一声不吭**（`runlog.record` 在没有库文件时直接返回 ——
      刚装好、还没抓过数的机器正是这样）。
      只靠记录去重的话，那种机器会被**每 30 秒重派一次**、连着派 30 分钟
      （窗口内），云商账号直接被并发的登录挤爆。
      ⇒ 谁派发的谁记，记在本地，不依赖子进程活着、也不依赖库在不在。
    """
    got = [x for x in _done_list(root) if str(x.get("slot") or "") != slot_text]
    got.insert(0, {"slot": slot_text, "steps": [str(x) for x in (steps or [])]})
    try:
        p = _done_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"_note": "内置定时器跑过的 slot —— 用来防重复派发",
                                   "done": got[:DONE_KEEP]}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def attempted(root, cmd: str, slot_text: str) -> bool:
    """这个 **slot** 已经试过了吗（成功失败都算）—— 两处**任一**说跑过就算跑过：

    1. `run_record`（`kind="wake"`）—— 子进程收尾记的，**它才是"真跑过"的证据**；
    2. 本地台账（`.secrets/wake-done.json`）—— 派发时记的，**兜住"子进程没记成"**。

    ⚠⚠ **按"试过没"去重，不按"成没成"** —— 失败也记。
      按成功去重的话，一次失败会被每 30 秒重试一遍，把机器和云商账号一起打爆。
    """
    from ...storage import runlog
    for r in runlog.recent(root, limit=100, kind=WAKE_KIND):
        d = r.get("detail") or {}
        if str(d.get("slot") or "") != slot_text:
            continue
        if cmd in (d.get("steps") or []):
            return True
    for x in _done_list(root):
        if str(x.get("slot") or "") == slot_text and cmd in (x.get("steps") or []):
            return True
    return False


def due(root=None, now=None) -> list:
    """**这一跳该唤醒哪些** —— 只看"到点 + 窗口内 + 这个 slot 没试过"。

    ⚠ "现在在不在跑"**不在这儿**（那要连 `tick`/界面状态一起看，见 `tick()`）：
      这里保持"该不该跑"的纯判定，好测。
    """
    root = _root_of(root)
    now = now or datetime.datetime.now()
    window = datetime.timedelta(minutes=WAKE_WINDOW_MINUTES)
    out = []
    for t in tasks(root):
        if not t["enabled"]:
            continue
        for w in t["whens"]:
            try:
                ww = When.from_dict(w)
            except ValueError:
                continue
            slot = ww.slot(now)
            if not slot or (now - slot) > window:
                continue                    # 没到点，或者过了窗口（**不补跑**）
            st = _slot_text(slot)
            if attempted(root, t["cmd"], st):
                continue
            out.append({"cmd": t["cmd"], "label": t["label"], "order": t["order"],
                        "slot": slot, "slot_text": st, "when_text": ww.text()})
    out.sort(key=lambda x: (x["slot"], x["order"]))
    return out


def at_text(when: datetime.datetime, now: datetime.datetime = None) -> str:
    """给人看的时间：**今天 21:00 / 明天 21:00 / 3 天后 21:00**，再远就写日期。

    ⚠ 不写"还剩 3 小时 12 分"那种倒计时 —— 它**每看一次都不一样**，
      而页面上不会自己刷新，看的人反而会以为卡住了。
    """
    now = now or datetime.datetime.now()
    days = (when.date() - now.date()).days
    hm = when.strftime("%H:%M")
    if days == 0:
        return "今天 %s" % hm
    if days == 1:
        return "明天 %s" % hm
    if days == 2:
        return "后天 %s" % hm
    if 0 < days <= 7:
        return "%d 天后（%s）%s" % (days, when.strftime("%m-%d"), hm)
    return when.strftime("%Y-%m-%d %H:%M")


def next_run(root=None, now=None, cmds=None) -> dict:
    """**下一次会唤醒什么、什么时候** —— 定时器页顶上那行大字、和右下角控制板都用它。

    ⚠ 同一时刻到点的几步要**合成一条**（那正是派发时的行为：
      `tick()` 一次只处理一个 slot，几步走同一条命令）——
      分开列的话页面上会写成"21:00 抓数据，21:00 算 POS"，看着像跑两趟。

    返回 `{}` 表示**没有任何下一步**（没有步骤声明唤醒时刻）。

    `cmds` 只算这几步（给"每天那趟"用 —— 左下角小标要显示**干活那趟**的时间，
    而不是每小时都在跑的自动更新）。不给 = 全部注册了的步骤。
    """
    root = _root_of(root)
    now = now or datetime.datetime.now()
    # ⚠ `cmds=[]`（空列表）**不等于**"不给"：前者是"这几步里没有能跑的"（返回 `{}`），
    #   后者才是"全部步骤"。写成 `if cmds` 的话空列表会退回"全部" —— 静默多算。
    want = None if cmds is None else set(cmds)
    best = None
    hits = []
    for t in tasks(root):
        if not t["enabled"]:
            continue                      # 没注册到定时器的，不算"下一次"
        if want is not None and t["cmd"] not in want:
            continue
        for w in t["whens"]:
            try:
                ww = When.from_dict(w)
            except ValueError:
                continue
            nxt = ww.next_after(now)
            if not nxt:
                continue
            if best is None or nxt < best:
                best, hits = nxt, [(t, ww)]
            elif nxt == best:
                hits.append((t, ww))
    if best is None:
        return {}
    return {"at": best.strftime("%Y-%m-%d %H:%M"),
            "at_text": at_text(best, now),
            "cmds": [t["cmd"] for t, _w in hits],
            "labels": [t["label"] for t, _w in hits],
            "when_texts": [w.text() for _t, w in hits],
            # 一句话版（前端/控制板直接用，别两处各拼一遍）
            "label": " + ".join(t["label"] for t, _w in hits)}


def wakes(root=None, *, limit: int = 20) -> list:
    """**执行日志**：最近几次唤醒 —— 什么时间、唤醒了什么、成功没。

    ⚠ 数据源就是 `run_record`（`kind="wake"`），**由真跑那一步的进程写**
      （不是定时器代笔）—— 所以"记的"和"发生的"是同一件事：
      机器断电 / 服务被杀时，日志里就不会出现一条"跑成功了"。

    ⚠ 只读、绝不抛：读不出来给空列表（别让一个嵌套层的异常把整页带崩）。

    ⚠ 2026-09-21：这份日志里**也有一次性注册那几笔**（`wake-once`：登记 / 到点 / 作废）——
      用户要的"**记录日志**"得**看得见**才算数；只写进库、界面上永远看不到，
      那就跟没记一样。真正执行那一趟仍然是 `kind="wake"`（由跑那一步的进程写），
      所以"登记 → 执行 → 删注册"三件事在这张表里连得起来。
    """
    from ...storage import runlog
    out = []
    try:
        rows = runlog.recent(root, limit=int(limit), kind=WAKE_KIND)
        # ⚠ `limit` 是**每类**各取这么多，合起来再排一次 —— 只按时间截断会在下面做。
        rows += runlog.recent(root, limit=int(limit), kind=once_mod.KIND)
        rows.sort(key=lambda r: str(r.get("finished_at") or r.get("started_at") or ""),
                  reverse=True)
        rows = rows[:int(limit)]
    except Exception:                                          # noqa: BLE001
        return []
    for r in rows:
        if r.get("kind") == once_mod.KIND:
            d = r.get("detail") or {}
            cmd = str(d.get("cmd") or "")
            note = str(r.get("note") or "")
            head = note.split("：", 1)[0] if "：" in note else "一次性"
            out.append({
                "slot": str(d.get("at") or ""),
                "at": r.get("finished_at") or r.get("started_at") or "",
                "started_at": r.get("started_at") or "",
                "steps": [cmd] if cmd else [],
                "labels": [_step_label(cmd)] if cmd else [],
                "label": "%s · %s" % (head, _step_label(cmd) if cmd else "（没记步骤）"),
                "ok": bool(r.get("ok")),
                "why": r.get("why") or "",
                "seconds": None,
                "exit_code": None,
                "once": True,
                "note": note,
            })
            continue
        d = r.get("detail") or {}
        cmds = [str(x) for x in (d.get("steps") or [])]
        out.append({
            "slot": str(d.get("slot") or ""),
            "at": r.get("finished_at") or r.get("started_at") or "",
            "started_at": r.get("started_at") or "",
            "steps": cmds,
            "labels": [_step_label(c) for c in cmds],
            "label": " + ".join(_step_label(c) for c in cmds) or "（没记步骤）",
            "ok": bool(r.get("ok")),
            "why": r.get("why") or "",
            "seconds": d.get("seconds"),
            "exit_code": d.get("exit_code"),
        })
    return out


#: 必做那几步"为什么关不掉" —— 逐条说清，别只丢一句"不允许"。
REQUIRED_WHY = {
    "dump": "不抓数，本地库永远是旧的，后面的分析和推送都只是拿旧数据在算",
    "erp-dump": "同上：云商那半边不抓，双平台对比就成了「一边有一边没有」",
    "autoupdate": "关掉之后门店永远停在装机那一版，安全修复也进不来",
}


def _step_label(cmd: str) -> str:
    """`cmd` → 中文名（注册表里那份，**不另写一份**）。取不到就退回 cmd。"""
    from ...features import registry
    try:
        step = registry.step_by_cmd(cmd)
        return step.label if step else cmd
    except Exception:                                          # noqa: BLE001
        return cmd


def next_at(root=None, now=None) -> str:
    """下一次会自己跑是什么时候（给人看）。**没有任何时间点 ⇒ 空串。**"""
    root = _root_of(root)
    now = now or datetime.datetime.now()
    best = None
    for t in tasks(root):
        if not t["enabled"]:
            continue
        for w in t["whens"]:
            try:
                nxt = When.from_dict(w).next_after(now)
            except ValueError:
                continue
            if nxt and (best is None or nxt < best):
                best = nxt
    return best.strftime("%Y-%m-%d %H:%M") if best else ""


# ─────────────────────────────────────────────────────────── 在不在跑 / 状态


def state_path(root) -> Path:
    return Path(root) / STATE_FILE


def current(root=None) -> dict:
    """正在派发的那一趟（没有就 `{}`）。**读不出来当没有**（绝不抛）。"""
    try:
        d = json.loads(state_path(_root_of(root)).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _mark_current(root, info: dict) -> None:
    try:
        p = state_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _clear_current(root) -> None:
    try:
        state_path(root).unlink()
    except OSError:
        pass


def running(root=None, *, busy: Callable[[], bool] = None) -> dict:
    """此刻有没有在跑。

    判据是"**有没有一趟派出去了还没收回来**"（状态文件）+ 调用方给的探针：
    * 服务里跑的时候，`busy` 传的是控制台那个任务管理器（`runner.current()`）——
      它同时管住了手动「跑一次」，两边不会撞；
    * 没有探针（自检、纯读）时只看状态文件 —— 它由 `tick()` 写、下一跳清。
    """
    root = _root_of(root)
    info = current(root)
    if busy is not None:
        try:
            return {"running": bool(busy()), "current": info}
        except Exception as e:                                 # noqa: BLE001
            return {"running": bool(info), "current": info,
                    "why": "探针读不出来：%s" % e}
    return {"running": bool(info), "current": info}


def last_wake(root=None) -> dict:
    """上一次唤醒的结果（`run_record` 里 `kind="wake"` 最新那条）。**没记过给 `{}`。**"""
    from ...storage import runlog
    rows = runlog.recent(root, limit=20, kind=WAKE_KIND)
    if not rows:
        return {}
    r = rows[0]
    d = r.get("detail") or {}
    return {"started_at": r.get("started_at") or "", "finished_at": r.get("finished_at") or "",
            "ok": bool(r.get("ok")), "why": r.get("why") or "", "note": r.get("note") or "",
            "slot": d.get("slot") or "", "steps": d.get("steps") or [],
            "exit_code": d.get("exit_code")}


def now(root=None, *, busy: Callable[[], bool] = None) -> dict:
    """**给健康模块和界面用的那一份** —— 「在不在跑 + 今天跑完没 + 下次什么时候」。

    ⚠ 这正是 `health.update_plan(timer_state=…)` 要的两个字段
      （`running` / `ran_today`）—— 自动更新从此有了判据。
    """
    from ...storage import runlog
    root = _root_of(root)
    today = datetime.date.today().isoformat()
    ran_today = False
    for r in runlog.recent(root, limit=50, kind=WAKE_KIND):
        if r.get("ok") and str(r.get("finished_at") or "").startswith(today):
            ran_today = True
            break
    if not ran_today:
        # 兼容老路径（`daily` 直接记的那种，历史上还没有 —— 留着以防万一）
        for r in runlog.recent(root, limit=50, kind="daily"):
            if r.get("ok") and str(r.get("finished_at") or "").startswith(today):
                ran_today = True
                break
    rel = running(root, busy=busy)
    return {"running": bool(rel.get("running")), "current": rel.get("current") or {},
            "ran_today": ran_today, "last": last_wake(root), "next_at": next_at(root),
            "problems": wake_problems(root)}


# ─────────────────────────────────────────────────────────── 派发（一跳）


def wake_argv(root, config: str, cmds, slot_text: str) -> list:
    """到点了要跑的那条命令 —— `daily --steps …`（**和手动「跑一次」同一条路**）。

    ⚠ 用 `--steps`（**精确这几步**）而不是一堆 `--skip-*`：
      `--skip-*` 那条路当年会被 `ALWAYS_STEPS = ("dump","attain")` 补回来，
      于是"只在周一跑达成"根本做不到（每天都会跑）。
    """
    return _SELF() + ["-c", str(config), "daily",
                      "--steps", ",".join(cmds),
                      "--wake-slot", slot_text]


def tick(root=None, *, spawn: Callable[[list], object] = None, config: str = "",
         busy: Callable[[], bool] = None, now: datetime.datetime = None) -> dict:
    """**一跳**：到点就派发一次。服务里的心跳线程调它；测试传假的 `spawn`。

    返回 `{ran: [cmd…], slot, command, why}`（没跑就 `ran: []` + `why`）。

    ⚠ 一次只处理**一个** slot 的活（最早那组）：一个 slot = 一条记录 = 一次去重判定，
      混在一起记的话"这趟跑没跑过"就说不清了。同一时刻到点的几步**合成一条命令**，
      这样 `run_daily` 里"先抓数、后算"的顺序和"第 1 步失败就跳过后面"的规矩一条不丢。
    """
    root = _root_of(root)
    if busy is None:
        busy = lambda: bool(current(root))                     # noqa: E731  兜底：只看状态文件
    try:
        busy_now = bool(busy())
    except Exception as e:                                     # noqa: BLE001
        return {"ran": [], "why": "判断有没有在跑时出错：%s: %s" % (type(e).__name__, e)}
    if busy_now:
        return {"ran": [], "why": "有任务正在跑，等下一跳"}
    # 不在跑 ⇒ 上一趟派出去了、现在收回来了：
    #   ① **先把那个 slot 记进台账**（子进程可能压根没记成，见 `_mark_done`），
    #   ② 再抹掉"正在跑"的状态（下一跳才好继续判）。
    info = current(root)
    if info:
        _mark_done(root, str(info.get("slot") or ""), info.get("steps") or [])
        _clear_current(root)
    try:
        hits = due(root, now=now)
    except Exception as e:                                     # noqa: BLE001
        return {"ran": [], "why": "算到点任务时出错：%s: %s" % (type(e).__name__, e)}
    # ⭐ **一次性注册**也并进这一跳（用户 2026-09-21：「把 timer 的注册加上这种
    #   **单次注册**机制吧，**记录日志**，**执行完删除注册**」）。
    #   ⚠ 先看候选，选中本次 slot 后才取走它；其他已到点任务留给下一跳。
    #     所以上面那个"有任务正在跑就等下一跳"的早退特别重要：它保证**没被取走的
    #     登记一定还在**（旁边那趟跑完，下一跳照样会派发它）。
    once_problem = ""
    try:
        from ..auth import runtime
        if not runtime.is_lifehall(root):
            once_mod.take_due(root, now=now, selected=())  # full 模式照常清掉过期登记
        for row in once_mod.peek_due(root, now=now):
            if not runtime.step_available(row["cmd"], root, recurring=True):
                continue
            when = once_mod._parse(row.get("at") or "") or now
            hits.append({"cmd": row["cmd"], "label": _label_of(row["cmd"]),
                         "order": _order_of(row["cmd"]), "slot": when,
                         "slot_text": row["slot_text"],
                         "when_text": "一次性（%s）" % row.get("at"),
                         "once": True, "once_key": row.get("key", ""),
                         "once_at": row.get("at", "")})
    except Exception as e:                                     # noqa: BLE001
        once_problem = "算一次性任务时出错：%s: %s" % (type(e).__name__, e)
    if not hits:
        return {"ran": [], "why": once_problem}
    hits = [h for h in hits if runtime.step_available(h["cmd"], root, recurring=True)]
    if not hits:
        return {"ran": [], "why": once_problem}
    hits.sort(key=lambda x: (x["slot"], x["order"]))
    slot_text = hits[0]["slot_text"]
    group = [h for h in hits if h["slot_text"] == slot_text]
    selected_once = {(str(h["once_key"]), str(h["once_at"]))
                     for h in group if h.get("once")}
    if selected_once:
        try:
            taken = once_mod.take_due(root, now=now, selected=selected_once)
        except Exception as e:                                 # noqa: BLE001
            return {"ran": [], "why": "取一次性任务时出错：%s: %s"
                    % (type(e).__name__, e)}
        if {(str(x.get("key") or ""), str(x.get("at") or "")) for x in taken} != selected_once:
            return {"ran": [], "why": "一次性任务登记已变化，下一跳重新判定"}
    cmds = [h["cmd"] for h in group]
    argv = wake_argv(root, config, cmds, slot_text)
    info = {"slot": slot_text, "steps": cmds, "argv": argv,
            "started_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _mark_current(root, info)
    # ⚠ 派发的同时就记台账：子进程万一起不来 / 崩在前面 / 写不成记录，
    #   这一跳也不会被无限重派（"派了没跑成"要人来管，不是每 30 秒撞一次）。
    _mark_done(root, slot_text, cmds)
    if spawn is None:
        return {"ran": [], "slot": slot_text, "command": argv,
                "why": "没有给派发入口（`spawn=`）—— 只算出来了，没跑"}
    try:
        spawn(argv)
    except Exception as e:                                     # noqa: BLE001
        # ⚠ 派发失败要**把状态抹掉**：留着的话下一跳会一直以为"有任务在跑"，
        #   从此再也不唤醒任何东西 —— 那种"没反应"最难查。
        _clear_current(root)
        # ⚠ 一次性那条**登记不复活**（`take_due` 已经删了）：没跑成这件事要**留在日志里**
        #   让人看见 —— 而"每 30 秒重试一遍"比没跑成更糟（云商不能并行登录）。
        if any(h.get("once") for h in group):
            from ...storage import runlog
            runlog.record(once_mod.KIND, False,
                          why="没派出去：%s: %s" % (type(e).__name__, e),
                          note="一次性任务：%s" % "、".join(_label_of(c) for c in cmds),
                          root=root)
        return {"ran": [], "slot": slot_text, "command": argv,
                "why": "没派出去：%s: %s" % (type(e).__name__, e)}
    return {"ran": cmds, "slot": slot_text, "command": argv, "why": ""}


def start_heartbeat(*, root=None, config: str = "", spawn=None, busy=None,
                    interval: int = HEARTBEAT_SECONDS, say=None,
                    stop: threading.Event = None) -> threading.Thread:
    """起**心跳线程**（守护线程，服务启动时调一次）。

    ⚠ 这一圈**绝不许把异常抛到启动路径上**（AGENTS.md 坑 2：服务是 `pythonw` 起的，
      启动路径上抛一下 = 服务起不来）——
      整圈包 try，出错写一句话，下一跳再来。
    """
    log = say or (lambda _m: None)
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                res = tick(root, spawn=spawn, config=config, busy=busy)
                if res.get("ran"):
                    log("内置定时器：%s（%s）" % ("+".join(res["ran"]), res.get("slot")))
                elif res.get("why"):
                    log("内置定时器：%s" % res["why"])
            except Exception as e:                             # noqa: BLE001
                log("内置定时器这一跳出错（不影响别的）：%s: %s" % (type(e).__name__, e))
            stop.wait(max(5, int(interval)))

    th = threading.Thread(target=loop, name="timer-heartbeat", daemon=True)
    th.start()
    return th
