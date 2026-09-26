"""**即时零售（O2O）**（一级功能模块，4.0.0）—— 云商库存 → 平台库存。

```
即时零售（一级）
└── o2o-source（库存源设置）← 本轮唯一一页，纯页面、没有定时步骤
```

开发目标「十一」（2026-09-24 用户拍板）：本轮**只做核心 + 设置页** —

* 核心三件套在 `src/app/o2o/`：`snapshot`（门店+商品编号两键的快照）、
  `source`（逐行库存源决策：云商/手动/缺映射…）、`settings`（config/o2o 读写）；
* 这一页：逐行选 `跟随云商库存` / `手动填写`（sku 级粒度、手动=常驻固定值、
  未设置默认云商），加本店分仓选择（门店名自动对库名，对不上下拉手选）；
* **五平台的模板转换与上传留 4.1**（平台接入期四样材料到位后，加子模块/Step）——
  所以本模块**现在不碰网络、不碰云商接口**（2026-09-24 实测连打会把云商
  账号挤下线，见 `snapshot` 模块头）。

⚠ 两个点 import（`..registry` = `src.features.registry`）——
写成三个点整条 `daily` 会崩（`features/inventory/__init__.py` 顶部的教训）。
"""

from __future__ import annotations

from ..registry import Feature, Sub

#: 子模块 —— **没有 `step`**（设置页不需要每天被叫醒；上传定时留 4.1）。
#: `key` 同时是页面 key：`web/index.html` 里 `data-subtab="oto-source"`。
SUB = Sub(key="oto-source", label="库存源设置", order=10)

#: 一级功能模块。`order=35` 排在「库存盘点」(30) 之后。
#: `types=""` = 所有身份可见（每家店自己的库存同步配置，不分店型）。
FEATURE = Feature(key="oto", label="即时零售", order=35, types="",
                  children=[SUB])
