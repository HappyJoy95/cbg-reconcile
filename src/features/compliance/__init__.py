"""**五项合规** —— 一级功能模块（对应控制台的一级菜单），下辖三个子模块。

```
五项合规（一级菜单，体验店/平台岗才看得到）
├── pos/          POS 合规          ← 定时步骤 pos
├── comparison/   报量查询（双平台数据对比） ← 定时步骤 pools
└── compliance-settings/  设置（现在长在 web 里）
```

⚠ 目录**照着菜单长**（用户 2026-09-19：「pos合规和报量查询还是一个功能模块（五项合规）的子模块」）——
这样"菜单里有什么"和"代码在哪儿"是同一个结构，找东西不用猜。

⚠ 适用门店：体验店 + 平台岗（`types="experience platform"`），合作店整个看不到 ——
**合作店不报量，没有"报没报"这回事**。子模块的 `types` 为空即继承这一条。
"""

from __future__ import annotations

from ...modules.timer import When          # noqa: F401  （声明唤醒时刻用的）
from ..registry import DEFAULT_WHENS, Feature, Step, Sub

#: 一级功能模块的声明（顺序 20：排在「销售数据」后面）
FEATURE = Feature(
    key="compliance", label="五项合规", order=20, types="experience platform",
    children=[
        # ⭐ 「什么时候唤醒、醒来做什么」由**这两个关键字**说了算（用户 2026-09-20 的接口）：
        #   `cmd` = 做什么，`whens` = 什么时候。默认 21:00（跟以前那条计划任务同点）。
        Sub(key="pos", label="POS 合规", order=10,
            step=Step(cmd="pos", label="POS 合规", order=30,
                      flag="--skip-pos", whens=DEFAULT_WHENS)),
        # ⚠ `key` 用的是**页面键**（`data-subtab="pools"`），不是文件夹名 ——
        #   文件夹叫 `comparison/`（双平台数据对比，域名更准），
        #   而页面/加载器/步骤名/跳过开关一路都是 `pools`。
        #   注册表的 key 必须跟**页面**对齐，否则"菜单漂移"那条测试就没意义了。
        Sub(key="pools", label="报量查询", order=20,
            step=Step(cmd="pools", label="双平台数据对比", order=40,
                      flag="--skip-pools", whens=DEFAULT_WHENS)),
        # ⚠ key 用 `compliance-settings`：二级 key 全表唯一（两个一级各有自己的「设置」）
        Sub(key="compliance-settings", label="设置", order=90),
    ],
)
