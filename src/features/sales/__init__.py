"""**周度重点产品** —— 一级功能模块（对应控制台的一级菜单）。

```
周度重点产品（一级菜单；2026-09-20 前叫「销售数据」）
├── attain/          周度目标达成情况（M2–M5）
├── attain-history/  历史记录（换周时自动锁住存档的那些周，**只读**）
└── sales-settings/  这条推送推不推、推给谁（现在长在 web 里）
```

⚠ 适用门店：三类都看（`types=""`）。
⚠ 子模块的 key 用 `sales-settings`（不是 `settings`）：**二级 key 全表唯一**，
   两个一级各有自己的「设置」，撞了注册表校验会红。
"""

from __future__ import annotations

from . import attain
from ..registry import Feature, Sub

#: 一级功能模块的声明（顺序 10：排在「五项合规」前面）
#: ⚠ 名字**改过一次**（用户 2026-09-20）：「**销售数据**」→「**周度重点产品**」——
#:   这一页看的就是腾讯文档「任务目标分配」里那张**周度重点产品**表（按台量定目标），
#:   "销售数据"四个字反而让人以为它是个数据清单。
#:   ⚠ **`key` 一动都不能动**：`sales` 是 `data-tab` / `GO_TARGETS` / `LEGACY_GO`
#:     三处共用的 key（见 `tests/test_web_ia.py::test_界面上的名字`）。
FEATURE = Feature(
    key="sales", label="周度重点产品", order=10, types="",
    children=[
        attain.SUB,                                  # 周度目标达成（带定时步骤）
        # ⚠ 「目标拆分」**不再单独一页**（用户 2026-09-20 改的）：
        #   改在「周度目标达成情况」里 —— **点门店名就地展开**，
        #   里面是每个人的目标与达成；**只有门店账号能改**（区长/平台只读）。
        # 「历史记录」**紧跟在它下面**（用户 2026-09-20：「在工作区的**周度目标达成
        #   情况下面**加个历史记录」）—— 过了这一周就自动锁住存档，这页只看不改。
        Sub(key="attain-history", label="历史记录", order=20),
        Sub(key="sales-settings", label="设置", order=90),
    ],
)
