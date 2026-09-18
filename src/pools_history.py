"""四池对账的**每日记录** —— 给控制台「四池比对」页翻历史。

## 为什么这一页不再是「报量排查」

原来那个页列的是每天的差异报告，判据是云商 `串号标识`。**那个判据已被证伪**
（实测 = 该机器**首次采购入库的店**，不跟调拨变）—— 留着它只会跟四池对账打架：
一个说"没差异"、另一个说"BC 有 3 台"。用户 2026-09-17 选的是
**形态留着、内容换掉**：还是"历史列表 + 点开看明细 + 下载 xlsx"，换成 AD/BC 的记录。

## 落盘

`out/pools-<年>.json`，**一年一个文件**（跟 `pos-<年>.json` 一个路子）。
结构：

```json
{"2026-09-17": {"counts": {"AD": 2, "BC": 3, "AC": 429, "BD": 304},
                "AD": [{...明细行...}], "BC": [...]}}
```

⚠ **同一天重跑 = 覆盖那一条**（不是叠加）：一天跑两次不该出两条记录。
⚠ 明细行直接存 `pools.details()` 的原样 dict —— 里面已经有串号/机型/门店/单号，
前端和 Excel 用的是同一份，不另排一遍。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

#: 相对项目根
REL_TMPL = "out/pools-%d.json"


def path_for(root, year: int) -> Path:
    return Path(root) / (REL_TMPL % year)


def _year_of(day: str) -> int:
    try:
        return int(str(day)[:4])
    except (TypeError, ValueError):
        return datetime.date.today().year


def save_day(root, day: str, counts: dict, ad, bc) -> Path:
    """把某一天的四池结果记下来（**覆盖当天那一条**）。返回文件路径。"""
    year = _year_of(day)
    p = path_for(root, year)
    data = load(root, year)
    data[str(day)] = {"counts": dict(counts or {}),
                      "AD": list(ad or []),
                      "BC": list(bc or [])}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(p)                       # 原子替换：写一半断电不留坏文件
    return p


def load(root, year: int | None = None) -> dict:
    """读某一年的记录。**文件坏了当空的** —— 历史看不了不该把控制台弄挂。"""
    y = int(year or datetime.date.today().year)
    try:
        d = json.loads(path_for(root, y).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def years(root) -> list:
    """有哪些年份的记录（倒序）—— 前端要拿它做年份切换。"""
    out = []
    try:
        for f in (Path(root) / "out").glob("pools-[0-9][0-9][0-9][0-9].json"):
            try:
                out.append(int(f.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
    except OSError:
        return []
    return sorted(out, reverse=True)


def days(root, year: int | None = None) -> list:
    """列表页要的：`[{date, AD, BC, AC, BD}]`，**新的在前**。"""
    y = int(year or datetime.date.today().year)
    data = load(root, y)
    out = []
    for day, rec in data.items():
        c = (rec or {}).get("counts") or {}
        out.append({"date": day,
                    "AD": int(c.get("AD") or 0),
                    "BC": int(c.get("BC") or 0),
                    "AC": int(c.get("AC") or 0),
                    "BD": int(c.get("BD") or 0),
                    # ⚠ 样机单列一项：**"云商卖了样机、玲珑报不了量"是已知的正常情况**，
                    #   但被排除掉多少台必须让人看得见（不静默过滤）。
                    "BC_样机": int(c.get("BC_样机") or 0)})
    out.sort(key=lambda r: r["date"], reverse=True)
    return out


def detail(root, day: str, year: int | None = None) -> dict:
    """某一天的 AD/BC 明细。没有就返回空的两段（前端不用判 None）。"""
    data = load(root, year or _year_of(day))
    rec = data.get(str(day)) or {}
    return {"date": str(day),
            "counts": rec.get("counts") or {},
            "AD": rec.get("AD") or [],
            "BC": rec.get("BC") or []}
