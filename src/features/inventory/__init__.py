"""**库存盘点**（一级功能模块）—— 扫码核对账面，出「未扫到 / 表外码」清单。

```
库存盘点（一级）
└── inventory（库存盘点）← 这个，**纯页面，没有定时步骤**
```

用户 2026-09-20：

> 「把**库存盘点功能整理成一个模块接入我们这个项目**？**导出 excel 这一步接给推送**。
>  **这个就不注册定时器了**」

⚠ **`SUB` 故意不给 `step`** —— 注册表里"没声明 `whens` = 只手动跑"，
   所以它不会出现在定时器任务表里，也没有 `--skip-inventory`。
   盘点是人拿着扫码枪站在货架前干的活，定时器叫不醒它
   （要真加：给 `Sub(step=Step(...))` 一行就够，别的都自动跟上）。

⚠ **它跟"报量对账"是两件事**：那个问"卖出去的报没报量"，
   这个问"账上的货还在不在"。别把盘点差异混进对账口径。

| 文件 | 干什么 | 碰网络/库 |
|---|---|---|
| `book.py` | 取数：仓库列表 / 本店仓匹配 / 账面 / 在途兜底 / 全库索引 | ✅ 走 `erp.py` |
| `push.py` | 导出的 xlsx 落盘 `out/inventory/` + 读汇总 + 组装文案 + 推送 | ✅ 走 `notify` |
| `web/inventory.html` + `web/inventory/*.js` | 扫码台那一页（前端，口径仍在 `core.js` 里） | 只调本项目接口 |
"""

from __future__ import annotations

# ⚠ 这里是**两个点**（`..registry` = `src.features.registry`）。
#   2026-09-20 写成过三个点 ⇒ 找 `src.registry` ⇒ `ModuleNotFoundError`，
#   而 `features/__init__.py` 会 import 它 ⇒ **整条 `daily` 直接崩**（exit 9）。
#   同一层的 `sales/` `compliance/` 都是两个点，照那个来。
from ..registry import Feature, Sub

#: 子模块 —— **没有 `step`**（见文件头那段）。
#: `key` 同时是页面 key：`web/index.html` 里那个 `data-subtab="inventory"`。
SUB = Sub(key="inventory", label="库存盘点", order=10)

#: 一级功能模块。`order=30` 排在「五项合规」(20) 之后。
#: `types=""` = 三类门店都看得见（盘点是每个店自己的货，不分店型）。
#: ⭐ 操作/数据范围（协议 v2，2026-10-02）：`push.inventory` 那把尺（原 `_can_for`
#:   写死 True）收编成这里的 `export` —— 全员可导（跟原来逐字一致）；
#:   盘点是**本店/本机**的活 ⇒ `data="store"`（最窄缺省）。
FEATURE = Feature(audience=("erp", "platform"), key="inventory", label="库存盘点", order=30, types="",
                  ops={"view": ("store", "manager", "platform"),
                       "export": ("store", "manager", "platform")},
                  data="authorized",
                  children=[SUB])

from . import book, push                                    # noqa: E402,F401
from . import settings as _settings                         # noqa: E402,F401
FEATURE.children.append(_settings.SUB)

# 路由归属只在业务登记；新增接口未登记时公共门禁拒绝。
FEATURE.routes = (
    ('GET', '/api/inventory/ready', 'inventory', 'view'),
    ('GET', '/api/inventory/warehouses', 'inventory', 'view'),
    ('POST', '/api/inventory/book', 'inventory', 'view'),
    ('POST', '/api/inventory/transit', 'inventory', 'view'),
    ('POST', '/api/inventory/index', 'inventory', 'view'),
    ('POST', '/api/inventory/export', 'inventory', 'export'),
)
FEATURE.route_data = {('POST', '/api/inventory/index'): 'all'}
