"""唤醒时刻 + 那个开关滑块的**存盘** —— `.secrets/wakes.json`（这台机器自己的设置，自更新不碰）。

用户 2026-09-20 要的接口分两半：

* **声明**（默认值）在功能模块里 —— `Step(whens=(When(...),))`，那是**代码**；
* **设置**（门店改过的）在这儿 —— 那是**这台机器**的事，跟 `.secrets/` 里
  其余东西一个性质（不进版本库、升级不覆盖、卸载也留着）。

## ⚠ 为什么不塞进 `.secrets/schedule.json`

那个文件的语义是「**我们注册了哪几条系统计划任务**」，生命周期是跟着任务走的 ——
`remove_all()` 会**整个删掉**它。唤醒时刻不是那回事：它是"这台机器几点干活"，
跟系统计划任务在不在没关系（内置定时器本来就不依赖它）。混在一起的话，
卸载一次就把用户设的时间点弄丢了。

## 形状

```json
{ "attain": {"whens": [{"kind": "weekly", "weekdays": [1], "time": "08:30"}],
             "enabled": true},
  "dump":   {"enabled": false},
  "pos":    {"order": 30} }
```

* 键 = 步骤的 `cmd`；**没出现过的 cmd = 全用注册表里声明的默认值**
  （所以升级上来的机器**不需要迁移**，文件不存在就是"全默认"）；
* `whens` 是**列表**（一个步骤可以有多个时间点）；**缺省 = 代码里声明的那个**；
* `enabled` 是**那个开关滑块**（用户 2026-09-20：「**开了就注册到定时器，
  不开就不注册**」）—— **缺省 = true**（没动过的步骤照跑）；
* `order` 是**执行顺序**（2026-09-21 用户：「**相同时间执行的任务，按照定时器
  这个列表从上到下执行**，然后定时器列表给个调顺序的功能」）——
  **同一时刻到点的几步按它排**（`timer.due()` 就是按 `(slot, order)` 排的）；
  **缺省 = 用模块声明的 `Step.order`**（升级上来的机器不用迁移）；
* ⚠ **两样东西互相独立**：关掉（`enabled: false`）**不抹掉时间点** ——
  再打开还是原来那个时间。所以文件里是两个字段，不是"删掉就等于关掉"。

⚠ 写不成 / 读不成一律**安静放过**（跟 `schedule._remember` 一个态度：
设置存不上不该让定时器停摆），但**读坏了要能说出来**（`load` 返回 `problems`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Tuple

from .when import When

#: 相对项目根的位置。放 `.secrets/`：这台机器自己的，`selfupdate.NEVER_TOUCH` 里有它。
REL = ".secrets/wakes.json"


def path(root) -> Path:
    return Path(root) / REL


def load(root) -> Tuple[Dict[str, dict], List[str]]:
    """读回设置 → `({cmd: {"whens": [When, …], "enabled": bool}}, 问题清单)`。

    ⚠ 形状 2026-09-20 改过一次：原来是 `{cmd: [When, …]}`，加了那个开关滑块之后
      一条设置里有**两样东西**（时间点 + 开没开）⇒ 包成 dict，
      缺省的键由 `whens_of()` / `enabled_of()` 补默认值（**别在调用点各自补一遍**）。

    ⚠ **坏了不许当成"没设置过"就完事**：那会让用户改过的时间点**悄悄变回默认**、
    而且下一跳就按默认时间跑起来 —— 静默改行为。所以坏掉的那条**跳过并在
    `problems` 里说出来**，界面/自检能显示。
    """
    p = path(root)
    if not p.is_file():
        return {}, []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {}, ["唤醒设置读不出来（%s）：%s —— 这几步先用注册表里的默认值" % (p.name, e)]
    if not isinstance(raw, dict):
        return {}, ["唤醒设置不是个对象（%s）—— 这几步先用注册表里的默认值" % p.name]
    out: Dict[str, dict] = {}
    bad: List[str] = []
    for cmd, entry in raw.items():
        if str(cmd).startswith("_"):            # 留给自己写注释/版本用
            continue
        if isinstance(entry, dict):
            items = entry.get("whens")
            on = entry.get("enabled")
        else:
            items = entry                        # 也认"直接给一个列表"的老写法
            on = None
        got: List[When] = []
        if items is None:
            pass                                 # 只写了开关、没动时间 ⇒ 时间用默认值
        elif not isinstance(items, (list, tuple)):
            bad.append("%s 的唤醒时刻不是一个列表，已跳过" % cmd)
            continue
        else:
            for one in items:
                try:
                    got.append(When.from_dict(one))
                except ValueError as e:
                    bad.append("%s 的唤醒时刻不合法（%s），已跳过" % (cmd, e))
            if items and not got:
                bad.append("%s 的唤醒时刻一条都没认出来，已跳过" % cmd)
                continue
        ord_ = entry.get("order") if isinstance(entry, dict) else None
        one_entry: Dict[str, object] = {}
        if got:
            one_entry["whens"] = got
        if on is not None:
            one_entry["enabled"] = bool(on)
        if isinstance(ord_, int) and not isinstance(ord_, bool):
            one_entry["order"] = ord_
        if one_entry:
            out[str(cmd)] = one_entry
    return out, bad


def save(root, data: Dict[str, dict]) -> None:
    """整份写回（tmp + rename，照 `attain.save` 的做法）。**写不成不抛。**"""
    p = path(root)
    body = {"_note": "唤醒时刻与开关 —— 由控制台「设置 › 定时器设置」写的，手改也行",
            "_how": "键 = 步骤的 cmd（dump/pos/pools/attain）；没出现的 = 全用代码里声明的默认值；"
                    "enabled=false = 这一步不注册到定时器（时间点仍然留着）"}
    for cmd, entry in sorted(data.items()):
        one: Dict[str, object] = {}
        whens = entry.get("whens") if isinstance(entry, dict) else entry
        if whens:
            one["whens"] = [(w if isinstance(w, When) else When.from_dict(w)).as_dict()
                            for w in whens]
        if isinstance(entry, dict) and "enabled" in entry:
            one["enabled"] = bool(entry.get("enabled"))
        if isinstance(entry, dict) and isinstance(entry.get("order"), int) \
                and not isinstance(entry.get("order"), bool):
            one["order"] = int(entry.get("order"))
        if one:
            body[cmd] = one
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def entry_of(root, cmd: str) -> Tuple[dict, list]:
    """这一步的整条设置 → `({"whens": [...], "enabled": bool}, 问题清单)`。

    ⚠ 读不到 / 写坏了**不抛**：给"什么都没改过"的样子（全默认），
      把问题放进第二个返回值 —— 跟 `load()` 一个态度
      （设置读不出来不该让定时器停摆，但要能说出来）。
    """
    raw, problems = load(root)
    return dict(raw.get(cmd) or {}), problems


def whens_of(root, cmd: str) -> List[When]:
    """这一步**被改过**的时间点；没改过就是空列表（= 用注册表默认值）。"""
    entry, _ = entry_of(root, cmd)
    return list(entry.get("whens") or [])


def enabled_of(root, cmd: str) -> bool:
    """这一步**注册到定时器了吗** —— 缺省是"注册"（没动过的步骤照跑）。"""
    entry, _ = entry_of(root, cmd)
    if "enabled" not in entry:
        return True
    return bool(entry.get("enabled"))


def order_of(root, cmd: str):
    """这一步的**执行顺序**（门店在界面上调过的）；没调过给 `None` = 用 `Step.order`。"""
    entry, _ = entry_of(root, cmd)
    got = entry.get("order")
    return got if isinstance(got, int) and not isinstance(got, bool) else None


def set_order(root, cmds) -> Dict[str, dict]:
    """把**执行顺序**整份记下来（`cmds` 是从上到下的那串 `cmd`）。

    ⚠ 写的是"第几个"（`(i+1)*10`，留出插空余量），**不是**原样存一份列表 ——
      列表存下来的话，"后来加了一个新步骤"就没有它的位置（得额外定规矩），
      而按序号存的话，没出现在这份名单里的步骤**照旧用 `Step.order`**（缺省即默认）。
    ⚠ `whens` / `enabled` **一个字都不动**（只补 `order` 这一格）。
    """
    data, _ = load(root)
    for i, cmd in enumerate(cmds or ()):
        entry = dict(data.get(str(cmd)) or {})
        entry["order"] = (i + 1) * 10
        data[str(cmd)] = entry
    save(root, data)
    return data


def set_cmd(root, cmd: str, whens) -> Dict[str, dict]:
    """改一步的**时间点**并落盘，返回**整份**设置。

    ⚠ 空列表 = **恢复默认时间**（把 `whens` 这条删掉），但**不动开关** ——
      "恢复默认时间"和"关掉这一步"是两件事（后者归 `set_enabled`）。
    """
    data, _ = load(root)
    got = [w if isinstance(w, When) else When.from_dict(w) for w in (whens or [])]
    entry = dict(data.get(cmd) or {})
    if got:
        entry["whens"] = got
    else:
        entry.pop("whens", None)
    if entry:
        data[cmd] = entry
    else:
        data.pop(cmd, None)
    save(root, data)
    return data


def set_enabled(root, cmd: str, on: bool) -> Dict[str, dict]:
    """打开 / 关掉**这个开关滑块**（`True` = 注册到定时器）。

    ⚠ 关掉**不等于抹掉时间点** —— 时间点留着，再打开还是原来那个时间。
      所以存的是两个独立字段（`whens` / `enabled`），关的时候只翻 `enabled`。
    """
    data, _ = load(root)
    entry = dict(data.get(cmd) or {})
    entry["enabled"] = bool(on)
    data[cmd] = entry
    save(root, data)
    return data


def clear(root, cmd: str = "") -> Dict[str, dict]:
    """恢复默认（**时间点和开关一起**）：给 `cmd` 只清那一步，不给就整份清掉。"""
    if cmd:
        data, _ = load(root)
        data.pop(cmd, None)
        save(root, data)
        return data
    save(root, {})
    return {}
