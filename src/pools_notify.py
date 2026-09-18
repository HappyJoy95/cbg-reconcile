"""推送记忆 —— 同一个串号连着几天推，别每天都当"新的"再报一遍。

用户 2026-09-17 提的场景：**批发单云商 9-14 就报了，玲珑那边要求 9-18 才报**
—— 中间这几天每天都会推同一条。批发单量大时，几天的推送几乎全是重复内容，
门店看两天就麻木了，**那几天里真正新冒出来的反而被淹掉**。

规则：

* **第一次**出现在推送里 → `★` 强调（这才是要重点看的）
* **推过了** → 弱化，标出**上次推送日期**和**推了几次**
  （门店一眼知道"这条催过了"，不用重新核一遍）
* 从清单里**消失**（报量了 / 云商出库了）→ 记忆里**删掉**它。
  哪天又冒出来（比如退货之后重新卖）会被当"新的" —— 那**是对的**。

⚠ 状态放 `.secrets/`（跟 `whatsnew.json` / `upgrade.json` 一个地方）：
那是"本机自己的东西"，自更新一根手指都不碰。
"""

from __future__ import annotations

import json
from pathlib import Path

#: 状态文件（相对项目根）
STATE_REL = ".secrets/pools-notify.json"


def state_path(root) -> Path:
    return Path(root) / STATE_REL


def load(root) -> dict:
    """读记忆。**文件坏了当没有** —— 记忆丢了不影响对账，别让它把整条流程弄挂。"""
    try:
        d = json.loads(state_path(root).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(root, state: dict) -> None:
    """写记忆。**先写临时文件再原子替换** —— 写一半断电不会留下坏文件。"""
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)


def clear(root) -> bool:
    """清空记忆。返回"之前到底有没有"（好让界面说实话，而不是永远回"已清除"）。"""
    p = state_path(root)
    if p.is_file():
        p.unlink()
        return True
    return False


def annotate(sns, state: dict) -> dict:
    """给这批串号标注「是新的 / 推过几次 / 上次什么时候推的」。

    ⚠ **只读、不改 state** —— 真正记下来是 `remember()` 的事，两者分开是为了
    "推送失败就不该记"：记了的话门店永远收不到那条强调，因为系统以为推过了。
    """
    out = {}
    for sn in sns:
        old = state.get(sn) or {}
        out[sn] = {
            "new": not old,
            "count": int(old.get("count") or 0),
            # ⚠ 显示用的是 **first（首次）不是 last（上次）**：
            #   门店要知道的是"这条从哪天开始催的、挂了几天"，
            #   "上次哪天推的"没意义 —— 反正每天都在推。
            "first": str(old.get("first") or ""),
            "last": str(old.get("last") or ""),
        }
    return out


def remember(state: dict, sns, day: str) -> dict:
    """记下这次推过的，并**顺手扔掉已经不在清单里的**。

    ⚠ 清理这一步不能省：不扔的话记忆无限增长，而且"解决了又回来"的
    会被永远当成老问题，再也拿不到 `★`。
    """
    keep = {}
    for sn in sns:
        old = state.get(sn) or {}
        keep[sn] = {
            "first": str(old.get("first") or day),
            "last": day,
            "count": int(old.get("count") or 0) + 1,
        }
    return keep
