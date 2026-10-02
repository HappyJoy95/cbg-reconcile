"""业务功能模块的**注册表** —— 加功能就在 `build_all()` 里加一行。

⚠ 这是**唯一**一处声明"有哪些功能"的地方（用户 2026-09-19 要的"安装注册机制"）。
`run_daily` 的步骤表、控制台菜单、自动化勾选项都从这儿派生 ——
在那之前，"有哪些步骤"摊在 `run_daily.py` 的 **6 张平行表**上，加一个步骤要改 6 处。

## 加一个功能要做什么

1. 建目录 `src/features/<一级>/<子模块>/`，在它的 `__init__.py` 里写一条
   `SUB = Sub(key=…, label=…, step=Step(…))`；
2. 在它的**父**模块（一级功能模块）的 `__init__.py` 里 import 它、列进 `children`
   —— 父带"目录"、子带"内容"（设计基线 §一·九·二）；
3. 若是个新的一级功能模块，在 `build_all()` 里加一行（**两处**：full 清单）。

⚠ **不许**扫目录 / 读 manifest / 反射 import（红线：不许长出插件框架）。
这儿的每一行都是**能被 grep 出来**的 —— 出问题时要一眼看到谁接了进来。

⚠ **按版收窄（生活馆版，2026-09-26）**：清单是函数 `build_all()`，生活馆分支
**条件 import** —— 被裁的目录（见 `edition.PRUNE`）在生活馆包里根本不存在，
无条件 `from . import compliance` 会当场 `ModuleNotFoundError`。
"""

from __future__ import annotations

from typing import List

from .. import edition as _edition
from . import cashier, tools


def build_all() -> List["Feature"]:  # noqa: F821 —— 类型只是文档，运行期不求值
    """**显式清单**（红线：不扫目录）—— 生活馆包里被裁的目录根本不 import。

    ⚠ 生活馆分支裁掉 `compliance/sales/plan/inventory/valueadd` 目录
    （见 `edition.PRUNE`），所以这里必须**按版条件 import** —— 无条件 import
    会在生活馆包里 `ModuleNotFoundError`，启动当场死。
    ⚠ `tools` 那条**每次现建**（`tools.build_feature()`，不直接拿 `tools.FEATURE`）：
      它的 children 是按版组装的，而 `tools.FEATURE` 在**模块导入那一刻**就定格了 ——
      同一个进程里先按 full 导入、再切 env 验生活馆（测试就是这么干的）会拿到旧的那份。
    ⚠ `cashier` **两版都注册**（2026-09-29 收银界面）：入口形态是生活馆的
      （`nav-lifehall` 直连块，full 版 CSS 藏），但 key 两版必须一致 ——
      `roles` 的 HTML↔`PAGE_RULES` 双向对照按 full 版跑，少一边就红。
    """
    # ⚠ tools 每次现建（children 按版组装，见上）；其余各目录没有版本分支，用常量即可。
    if _edition.is_lifehall():
        return [tools.build_feature(), cashier.FEATURE]
    from . import compliance, distribution, inventory, plan, sales, valueadd
    return [
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
        tools.build_feature(),
        # 分销（2.3.0，2026-09-29）：渠道分销部四张看板 —— **手动选时间段现拉**，
        # 无定时步骤；`types="multi"` 只给区长/平台（见 `distribution/__init__.py`）。
        distribution.FEATURE,
        # 收银（2026-09-29）：生活馆利润核算的录入端，**没有定时步骤**（人一笔笔录）。
        cashier.FEATURE,
    ]


#: 老名字 —— 模块导入时定格（full 生产路径 2810 条测试钉的是它）。
#: **要问"这版有哪些功能"一律走 `registry.all_features()`**，它每次现算。
ALL = build_all()
