# -*- coding: utf-8 -*-
"""**这台安装是哪个版** —— 生活馆版（lifehall）判据，全项目唯一一处。

用户 2026-09-26：生活馆门店没有云商、只有玲珑 —— 单独打一个包、独立更新渠道，
包里**物理不含**对账/云商代码与凭据。

判据优先级：**环境变量 `CBG_EDITION` > 仓库根 `EDITION` 文件 > `full`**。

* `EDITION` 文件由分支决定：`lifehall` 分支写 `lifehall`，主包（main）没有
  该文件（或写 `full`）—— 打包和自更新都会把它带下去，装在哪台机器上就是哪个版。
* env 是给**测试**留的：conftest 把测试默认钉成 `full`（既有 2810 条测试都是
  按 full 写的），要验生活馆行为的测试显式 `mock.patch.dict(os.environ, ...)`
  再 `edition.reload()`。
* ⚠ 只许问这里：菜单收窄、登录门禁、步骤过滤、打包裁剪、自更新分支
  全从这一个模块派生 —— 各处自己写 `if 生活馆` 就是第二份判据
  （本项目的红线：两份定义走散的表现是"菜单里藏了、接口还给"）。
"""

from __future__ import annotations

import os
from pathlib import Path

from .paths import ROOT   # ⚠ 项目根全项目只许从 paths.py 取（tests/test_paths.py 钉着）

#: 合法值 —— 其它一律回落 `full`（宁可当主包跑，也别半疯）。
VALID = ("full", "lifehall")

#: 生活馆版**保留**的页面 key（`PAGE_RULES` 的键 = HTML 的 `data-tab/subtab/foot`）。
#: ⚠ 不在这张表里的页面在生活馆包里**连接口一起 404**（见 `web.LIFEHALL_GONE`）。
LIFEHALL_PAGES = (
    "tools", "pricetag", "badge", "claim-pending",        # 三个工具 + 一级容器
    "account", "update", "general", "theme", "linglong",  # 左下角（定时器/数据交换不要）
)

#: 生活馆版保留的**能力层步骤**：抓玲珑（待领清单的数据源）+ 自动更新。
#: `dump` 的 `whens` 会被剥成 `()`（用户定：不建计划任务，手动刷）。
LIFEHALL_BUILTIN_STEPS = ("dump", "autoupdate")

#: **物理裁剪清单**（相对仓库根）—— 打包（build_package.sh）和自更新
#: （selfupdate._targets）共用这一份，谁也不许另抄一张表。
#:
#: ⚠ 清单里的路径必须真实存在（`tests/test_edition.py` 逐条钉着）——
#: 写错路径 = 打包静默漏裁，而"漏了"这种事只有装到门店机器上才看得出来。
PRUNE = (
    # 业务功能（生活馆没有这些活）
    "src/features/compliance",
    "src/features/sales",
    "src/features/plan",
    "src/features/inventory",
    "src/features/valueadd",
    "src/features/tools/sn_trace",
    # 云商 / 对账执行件（⚠ 含内置公司账号凭据的 erp.py 必须在内）
    "src/erp.py",
    "src/reconcile.py",
    "src/report.py",
    "src/pools.py",
    "src/pools_history.py",
    "src/pools_notify.py",
    "src/tdoc.py",
    "src/app/pos.py",
    "src/app/report.py",
    "src/app/report_inbox.py",
    # 前端整页 / 名单 / 手册
    "web/inventory.html",
    "web/inventory",
    "config/managers.yaml",
    "门店操作手册.md",
    "发布说明.md",
)

_cache = {"value": None}


def read_edition_file(root=None) -> str:
    """读 `EDITION` 文件 → 合法值；读不到 / 垃圾内容 → `""`。"""
    base = Path(root) if root is not None else ROOT
    try:
        raw = (base / "EDITION").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""
    return raw if raw in VALID else ""


def value(root=None) -> str:
    """当前版：env > 文件 > full。`root` 只在测试里用来指别的目录。"""
    env = (os.environ.get("CBG_EDITION") or "").strip().lower()
    if env in VALID:
        return env
    if root is not None:                       # 测试指路：不进缓存
        return read_edition_file(root) or "full"
    if _cache["value"] is None:
        _cache["value"] = read_edition_file() or "full"
    return _cache["value"]


def is_lifehall(root=None) -> bool:
    return value(root) == "lifehall"


def reload() -> None:
    """清文件缓存 —— 测试切 env 后调它（env 本身每次现读，不缓存）。"""
    _cache["value"] = None


def pruned(rel: str) -> bool:
    """相对路径 `rel` 是不是在裁剪清单里（目录前缀匹配）。"""
    r = str(rel).replace("\\", "/").lstrip("/")
    for p in PRUNE:
        pp = p.rstrip("/")
        if r == pp or r.startswith(pp + "/"):
            return True
    return False
