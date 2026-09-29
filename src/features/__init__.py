"""业务功能模块的**注册表** —— 加功能就在 `ALL` 里加一行。

⚠ 这是**唯一**一处声明"有哪些功能"的地方（用户 2026-09-19 要的"安装注册机制"）。
`run_daily` 的步骤表、控制台菜单、自动化勾选项都从这儿派生 ——
在那之前，"有哪些步骤"摊在 `run_daily.py` 的 **6 张平行表**上，加一个步骤要改 6 处。

## 加一个功能要做什么

1. 建目录 `src/features/<一级>/<子模块>/`，在它的 `__init__.py` 里写一条
   `SUB = Sub(key=…, label=…, step=Step(…))`；
2. 在它的**父**模块（一级功能模块）的 `__init__.py` 里 import 它、列进 `children`
   —— 父带"目录"、子带"内容"（设计基线 §一·九·二）；
3. 若是个新的一级功能模块，在下面 `ALL` 里加一行。

⚠ **不许**扫目录 / 读 manifest / 反射 import（红线：不许长出插件框架）。
这儿的每一行都是**能被 grep 出来**的 —— 出问题时要一眼看到谁接了进来。
"""

from __future__ import annotations

from . import compliance, distribution, inventory, plan, sales, tools, valueadd

#: 全部一级功能模块。**顺序无所谓**（`Feature.order` 说了算）。
ALL = [
    compliance.FEATURE,
    sales.FEATURE,
    # 月度生意计划（M22）：七块 × 当月 vs 上月同期 × 销量/销售额/利润 + 环比。
    # ⚠ 跟「周度重点产品」是**两回事** —— 那边算目标达成率，这边算实际经营 + 环比。
    plan.FEATURE,
    # 库存盘点（M16）：**没有定时步骤** —— 人拿扫码枪站在货架前干的活，
    # 定时器叫不醒它（见 `features/inventory/__init__.py` 顶部）。
    inventory.FEATURE,
    # 增值 · 防护膜达成情况（2026-09-22）：本地库现算，**没有定时步骤**。
    valueadd.FEATURE,
    # 小工具（2026-09-22）：价签 / 工牌 / **权益领取** / **串号追踪** —— 无定时步骤。
    tools.FEATURE,
    # 分销（2.3.0，2026-09-29）：渠道分销部四张看板 —— **手动选时间段现拉**，
    # 无定时步骤；`types="multi"` 只给区长/平台（见 `distribution/__init__.py`）。
    distribution.FEATURE,
]
