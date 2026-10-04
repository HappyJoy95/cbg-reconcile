"""**分销**（一级功能模块）—— 渠道分销部的分销单看板（2.3.0）。

```
分销（一级，types="platform" = **只给平台岗**，区长也看不见）
├── dist-region   ① 区域看板   九区分布 + 「待确认」队列（下拉定区、持久可改）
├── dist-model    ② 机型看板   一级分类 / 三级分类 两个维度
├── dist-salesman ③ 销售员看板  店员总销售额 + 每人品类占比
└── dist-detail   ④ 分销明细   三张看板共用的底表（筛选 + 导出 + 逐行改区）
```

## 三条用户拍板的口径（2026-09-29，开发目标 `.dsh/docs/2026-09-29-2.3.0-开发目标.md`）

1. **只算渠道分销部**（`门店=渠道分销部` 的分销单，单据类型 ∈ {分销, 分销退}）。
2. **区域识别不用门店名兜底** —— 认不出的一律进「待确认」手动下拉；
   确认结果按**客户名**落 `region_map`（跨时段沿用、可改可重置）。
   ⚠ 客户名 100% 覆盖（实测 25,986 行分销单没有一行客户为空）⇒ 按客户记忆可靠。
3. **手动选时间段现拉**，不注册定时步骤（`Sub` 不给 `step`，同库存盘点那条）。

## ⚠ 数据从哪来：**现拉，不读 `erp_sales`**

2026-09-29 回源实测钉死的理由：`erp_sales`（每天 erp-dump 落的那张）**历史有缺行** ——
1/25~1/31 回源 1,980 行 vs 库 1,760 行，139 个单整单不在库里（支付时间就在区间内、
接口现在拉得到、库里从来没有；最可能是事后补录 + 平时只重拉当月 ⇒ 1 月的洞永远补不上）。
所以看板按用户选的时间段**现拉接口** → 筛分销单 → 落自己的 `dist_sales`（区间覆盖写，
重拉即修正）。`erp_sales` 是对账的地盘，**一行不碰**。

| 文件 | 干什么 | 碰网络/库 |
|---|---|---|
| `store.py`  | 建表 / 区间覆盖写 / 按区间读 + `region_map` 读写 | ✅ `out/distribution.db`（本机产出，独立于对账库） |
| `fetch.py`  | 现拉：`ErpClient.sales_range` → 筛分销 → 落库 | ✅ 走 `erp.py` |
| `region.py` | 九区关键词识别 + 客户映射优先级（纯函数为主） | 只读写映射表 |
| `metrics.py`| 三看板聚合（净额 = 分销 + 分销退） | 纯函数 |
"""

from __future__ import annotations

from ..registry import Feature, Sub

#: 数据范围 —— 只算这家店（用户 2026-09-29 拍板）。
#: ⚠ 它**不在** `config/stores.yaml` 的 30 家名单里（那是门店名单，
#:   渠道分销部是职能部门）⇒ 不能用 `scope_store_ok()` 判，直接字符串筛。
TARGET_STORE = "渠道分销部"

#: 区域看板的九个区（用户点名）。识别=在备注里找这些词，**不拿门店名兜底**。
ZONES = ("市南", "市北", "城阳", "胶州", "黄岛", "平度", "莱西", "即墨", "崂山")

#: 识别不出的落在这儿（看板上单独一档，下拉人工定区）。
ZONE_PENDING = "待确认"

#: 下钻筛选**允许按哪几列精确匹配**（`/api/dist/detail?field=…&value=…`）。
#:
#: 2026-09-30 用户要的两处下钻：机型看板按 **一级/三级分类、商品名称**，
#: 销售员看板按 **店员**（+ 第二级再按一级分类出该品类的单）。
#: ⚠ **白名单，不开放"任意列名"**：列名要拿去跟行比（将来也可能进 SQL），
#:   名单外的一律**报错**而不是静默忽略 —— 前端拼错列名会表现成
#:   「点开是全部行」，那种错没人看得出来。
DRILL_FIELDS = ("一级分类", "二级分类", "三级分类", "商品名称",
                "店员", "业务员", "品牌", "单据类型", "客户/顾客", "供应商")

#: 空值在看板上的**占位名**（`metrics._bucket` 归「（空）」、销售员归「（无店员）」）。
#: 下钻要能点它们 —— 否则「（无店员）」那一行点开永远是 0 行。
DRILL_EMPTY = ("（空）", "（无店员）")


def drill_hit(row: dict, field: str, value: str) -> bool:
    """这一行是不是该档的 —— 精确匹配（strip 后），空占位名匹配空值。"""
    got = str(row.get(field) or "").strip()
    if value in DRILL_EMPTY:
        return not got
    return got == value

SUBS = [
    # ① 区域：九区分布 + 待确认队列（手动下拉那套在这一页）
    Sub(key="dist-region", label="区域看板", order=10),
    # ② 机型：一级分类 / 三级分类
    Sub(key="dist-model", label="机型看板", order=20),
    # ③ 销售员：店员总销售额 + 每人品类占比（⚠ 业务员字段全空，用**店员**列）
    Sub(key="dist-salesman", label="销售员看板", order=30),
    # ④ 明细底表：筛选 / 导出 / 逐行改区
    Sub(key="dist-detail", label="分销明细", order=40),
]

#: 一级功能模块。`order=45` 排在「小工具」(40) 之后 —— 不挪动任何现有页的顺序。
#: `types="platform"`：**只有平台岗看得见**（用户 2026-09-30 复核：
#: 「分销仅平台岗可见」）—— 区长**也看不见**，门店更没有。
#: ⚠ 原来写的是 `types="multi"`（区长/平台），2026-09-30 收紧成 `platform`：
#:   `multi` 那一档包含区长（`scope_types()` 里区长 = `{multi, 玲珑档}`），
#:   想"仅平台"只能写 `platform`（平台 = `{multi, platform}`，区长不含它）。
#: ⚠ 藏菜单从来不算权限（坑 18）：路由 `if path.startswith("/api/dist")`
#:   那道统一 403 也一并收紧成 `role != ROLE_PLATFORM`，接口另有一道 `dist.write/export`。
#: ⭐ 操作/数据范围（协议 v2，2026-10-02）：整组三档操作**都只给平台岗**
#:   —— 原路由层那道 `role != platform` 统一 403 和 `_can_for` 的 dist.write/export
#:   全从这一份声明派生（`require(scope, "distribution", …)`；子页不写 ops 继承父）。
FEATURE = Feature(audience=("platform",), key="distribution", label="分销", order=45, types="platform",
                  ops={"view": ("platform",),
                       "modify": ("platform",),
                       "export": ("platform",)},
                  data="authorized",
                  children=SUBS)

# 路由归属只在业务登记；新增接口未登记时公共门禁拒绝。
FEATURE.routes = (
    ('GET', '/api/dist/board', 'distribution', 'view'),
    ('GET', '/api/dist/detail', 'distribution', 'view'),
    ('GET', '/api/dist/map', 'distribution', 'view'),
    ('POST', '/api/dist/fetch', 'distribution', 'modify'),
    ('POST', '/api/dist/zone', 'distribution', 'modify'),
    ('POST', '/api/dist/map/reset', 'distribution', 'modify'),
    ('POST', '/api/dist/export', 'distribution', 'export'),
)
