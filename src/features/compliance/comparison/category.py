# -*- coding: utf-8 -*-
"""四池的**品类范围** —— 只有六类串号参与对账（纯逻辑，**只 import 标准库**）。

用户 2026-09-21：「四池对比时，只对比手机、穿戴、音频、平板、电脑、智慧屏这些，
其他的东西不对比」；「**电脑**」= 只算笔记本（MateBook 这类）。

⚠⚠ **这跟周度达成那张「产品列 → 商品编码」映射表是两码事**（用户同日明确：
「这个和映射表没关系，这个只是五项合规里面的功能」）。那张在 `features/sales/attain`，
这张在 `features/compliance/comparison` —— **两条口径各自独立**，别合成一处。

## 为什么不能各池各判一次

四个池装的是**同一批机器**（同一批串号在池间流动）。品类是**这台机器**的属性，
所以四个池谁认得出来就用谁的，合并成 `{串号: 品类}` 再统一过滤。

各池各判的话，同一台机器可能在一侧算「手机」、另一侧算「配件」⇒ 一侧排掉一侧保留
⇒ **凭空多出 AD/BC** —— 那正是本项目最忌讳的假差异。

## 合并的两层规则（2026-09-23 用户拍板，两层缺一不可）

| 层 | 规则 | 为什么 |
|---|---|---|
| **池内**多行（`_rank_within`） | 六类 > 其它 —— **具体的赢** | 同串号既买手机又挂 Care+ 行，取「这台是手机」；反过来会把真手机判成配件剔掉 |
| **池间**冲突（`_rank_between`） | 其它 > 六类 —— **排除的赢** | 池C 粗类说「穿戴」、池D/玲珑细判说「配件」时信细判 —— 表带不许进 BC（第一版「六类优先」就是让表带混进来的 bug） |

## ⚠⚠ 玲珑的**父类**别拿来判

`lg_stock` 有 `category_parent_name`（手机 / 穿戴 / 平板 / PC / **配件** / 礼品 / 物料 / 家居），
看着正好能用 —— **但实测：音频系列（44 个）的父类就是「配件」**。
按父类过滤 ⇒ 每台耳机都在玲珑侧被排掉、云商侧保留 ⇒
现在 BC 里那 8 台音频会整批变成「云商报了、玲珑没报」的假差异。
→ **一律按细类 `category_name` 认**（音频系列 → 音频）。

## 三种返回值，别混

| 返回 | 含义 | 处理 |
|---|---|---|
| 六类之一 | 属于要对比的六类 | **参与** |
| `OTHER` | 认得出，但不在这六类里（配件 / 全屋智能 / 礼品…） | 不参与 |
| `None` | 空的 | 不参与 |
| `UNKNOWN` | **没见过这个词** | 不参与，**而且必须报出来** |

⚠ 最后一行是本项目的红线：**没见过的词不许静默归「其它」**。
玲珑和云商的品类词都会新增（`order_lines.category_id` 尤其 —— 玲珑只给内部编码），
静默归并 ⇒ 新品类被悄悄排除、页面上看不出来。

## 映射表怎么来的

2026-09-21 对着 `out/cbg-2026.db` **实测全量**列出来的（`erp_sales` 13 种 /
`erp_stock` 最新快照 101 种 / `lg_stock` 17 种 / `order_lines` 17 种），
所以覆盖率是 100%、「未知」实测为 0。**新增了词就会冒出来** —— 这正是要的效果。

2026-09-23 用参考库补了一批（玲珑新词 MateBook 系列 / 通话手环 / TY·ZY·TD /
智能音箱…、云商 会员·增值服务·鸿蒙汽车、池A 体脂秤编码）——
⚠ **`SY`（商品名「DFG-PD / 东风-Jade」）故意没补**：看不出是手机还是车品，
不猜，留着 UNKNOWN 继续报出来。
另：池C 判定改成 `classify_sales`（**商品名前缀优先**，`一级分类` 只兜底），
因为粗类把表带 202 行 / 眼镜 74 行全归了「智能穿戴」。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

#: 参与四池对账的六个大类（用户 2026-09-21 定）。顺序 = 界面上的展示顺序。
CATS = ("手机", "穿戴", "音频", "平板", "电脑", "智慧屏")

#: 认得出、但**不参与**（明确不是这六类）
OTHER = "其它"

#: **没见过这个词** —— 跟 `OTHER` 分开，而且必须报出来
UNKNOWN = "未知"

# ---------------------------------------------------------------- 池C 云商销售单
#: `erp_sales.一级分类`（实测 13 种，全在上面）
ERP_SALES = {
    "手机": "手机",
    "智能穿戴": "穿戴",
    "音频产品": "音频",
    "平板": "平板",
    "笔记本": "电脑",            # ⚠ 用户定：「电脑」只算笔记本
    "智慧屏": "智慧屏",
    # —— 认得出、但不在这六类 ——
    "手机平板周边": OTHER,
    "电脑周边": OTHER,           # ⚠ 鼠标键盘包，别算进「电脑」（用户明确）
    "台显打印": OTHER,           # ⚠ 台式机/显示器/打印机，也不算
    "全屋智能": OTHER,
    "外购散件": OTHER,
    "智能家居": OTHER,
    "潮玩礼品": OTHER,
    # 2026-09-23 参考库补（这三个都无串号 / 汽车件，兜底别落 UNKNOWN）：
    "会员": OTHER,
    "增值服务": OTHER,
    "鸿蒙汽车": OTHER,
}

# ---------------------------------------------------------------- 池D 云商在库
#: `erp_stock.pro_name` 的**第一段**（按 `/` 切）。实测 101 种。
#:
#: 这一池**没有品类列**，只能从商品名前缀认 —— 好在格式很规整
#: （`智能手机/华为/...`、`手表/华为/...`）。
ERP_STOCK = {
    # —— 六类 ——
    "智能手机": "手机",
    "手表": "穿戴",
    "手环": "穿戴",              # ⚠ 原先漏了（136 个）：手环也是穿戴
    "耳机麦克": "音频",
    "音箱": "音频",
    "平板电脑": "平板",
    "华为笔记本": "电脑",
    "联想笔记本": "电脑",
    "WIKO笔记本": "电脑",        # WIKO 是智选品牌，但**笔记本就是笔记本**
    "智慧屏": "智慧屏",
    "华为智慧屏": "智慧屏",      # ⚠ 原先漏了（81 个）
    # ⚠ 「智慧屏」这个写法最新快照里没有（只有「华为智慧屏」），留着是**预留** ——
    #   云商侧换个写法就会出现；真出现了也不用改代码。
    "智慧屏": "智慧屏",
    # —— 认得出、但不在这六类 ——
    # 台式机 / 显示器 / 打印机 / 一体机：用户明确「电脑只算笔记本」
    "消费台式机": OTHER, "商用台式机": OTHER, "显示器": OTHER,
    "打印机": OTHER, "一体机": OTHER,
    # ⚠ 「音箱」原先映射音频 —— 用户 2026-09-23 拍板：**音箱不算音频、不参与对账**
    #   （AI 音箱 2e / Sound X 那批，一级分类也归「全屋智能」，两侧口径拉平成不参与）。
    "音箱": OTHER,
    # 配件 / 周边
    "保护壳套": OTHER, "数据线": OTHER, "充电器": OTHER, "移动电源": OTHER,
    "触控笔": OTHER, "电脑鼠标": OTHER, "电脑键盘": OTHER, "键盘": OTHER,
    "蓝牙键盘": OTHER, "平板键盘": OTHER, "磁吸配件": OTHER, "表带": OTHER,
    "散热底座": OTHER, "笔记本包": OTHER, "相机套": OTHER, "适配器": OTHER,
    "扩展器": OTHER, "硬盘": OTHER, "支架": OTHER, "面板支架": OTHER,
    "中控屏支架": OTHER, "三脚架": OTHER, "影像套件": OTHER, "音乐配件": OTHER,
    "车载配件": OTHER, "汽车配件": OTHER, "开关配件": OTHER,
    "华为全能充车载充电器（Max 100W-演示机）": OTHER,
    # 全屋智能 / 家居
    "墙面开关": OTHER, "射灯": OTHER, "智能窗帘机": OTHER, "调光器": OTHER,
    "AI传感器": OTHER, "移动传感器": OTHER, "智能隔离器": OTHER, "驱动": OTHER,
    "全屋路由": OTHER, "中控屏": OTHER, "面板片": OTHER, "智能主机": OTHER,
    "网关": OTHER, "摄像头": OTHER, "吸顶音箱": OTHER, "灯带": OTHER,
    "环境传感器": OTHER, "筒灯": OTHER, "安防传感器": OTHER, "智能门锁": OTHER,
    "地脚灯": OTHER, "枪机壁装支架": OTHER, "摄像机": OTHER, "吸顶灯": OTHER,
    "智能生活": OTHER, "交换机": OTHER, "指向枪": OTHER, "浴霸": OTHER,
    "门禁机": OTHER, "信号放大器": OTHER, "室外壁挂天线": OTHER,
    "对讲机": OTHER, "开关模块": OTHER, "机柜": OTHER, "求助开关": OTHER,
    "台灯": OTHER, "嵌入式射灯": OTHER, "嵌入式灯具": OTHER, "录像机": OTHER,
    "无线话筒": OTHER, "空白面板": OTHER, "空调": OTHER, "雕刻机": OTHER,
    "饮水吧": OTHER, "剃须刀": OTHER, "智能汽车": OTHER, "语音面板": OTHER,
    "路由器": OTHER, "插座": OTHER, "音箱影院": OTHER, "眼镜": OTHER,
    "体脂秤": OTHER, "儿童玩具": OTHER, "促销品": OTHER, "服务": OTHER,
    "延保服务": OTHER, "控制器": OTHER, "场景遥控器": OTHER,
}

# ---------------------------------------------------------------- 池B 玲珑在库
#: `lg_stock.category_name`（**细类**，实测 17 种）。
#: ⚠ 别用 `category_parent_name` —— 音频的父类是「配件」，见模块头。
LG_STOCK = {
    # —— 六类 ——
    "华为P系列": "手机", "华为Mate系列": "手机", "华为nova系列": "手机",
    "华为畅享系列": "手机",
    "华为手表": "穿戴", "华为手环": "穿戴",
    "音频系列": "音频",
    "华为M系列": "平板", "华为T系列": "平板",
    "WK": "电脑",
    # —— 认得出、但不在这六类 ——
    "华为礼品": OTHER, "华为物料": OTHER, "智能系列": OTHER,
    "华为专属配件": OTHER, "第三方配件": OTHER, "电源系列": OTHER,
    "华为路由器": OTHER,
    # 2026-09-23 参考库补的**玲珑新词**（当时 16 个词全落 UNKNOWN：
    # MateBook 系列被判"没见过" ⇒ 电脑被悄悄剔出对账 —— 正是红线说的静默）：
    "华为MateBook系列": "电脑", "华为MateBook GT系列": "电脑",
    "华为MateBook D系列": "电脑", "华为MateBook E系列": "电脑",
    "华为MateBook X系列": "电脑",
    "华为通话手环": "穿戴",
    # ⚠ TY/ZY/TD 的商品名是 Adora/Jackie/Bayne/OceanM「8GB+256GB 全网通版」= 手机；
    #   **SY 不补** —— 它的商品名是「DFG-PD / 东风-Jade」，看不出是手机还是车品，
    #   不猜，留着 UNKNOWN 继续报出来（认不出就报，这是红线）。
    "TY": "手机", "ZY": "手机", "TD": "手机",
    "智能音箱": OTHER,            # 用户 2026-09-23：音箱不参与
    "打印机": OTHER, "HiLink生态产品": OTHER, "摄影系列": OTHER,
}

# ---------------------------------------------------------------- 池A 玲珑销售单
#: `order_lines.category_id`（实测 17 种）。
#: ⚠⚠ 玲珑只给**内部编码**，没有可读的品类名 —— 这张表是 2026-09-21
#: **从 `item_name` 人工对出来的**（CMCG10000013 = Mate 70 Air / Mate X7 …）。
#: 所以它是这里**最不可靠**的一张，而且玲珑新增品类时一定会冒出未知词
#: ⇒ 靠 `UNKNOWN` 那条路径把它报出来，别当它永远够用。
LG_SALES = {
    # —— 六类 ——
    "CMCG10000013": "手机", "CMCG10000014": "手机", "CMCG10000015": "手机",
    "CMCG10000017": "手机",
    "CMCG10000021": "穿戴", "CMCG10000022": "穿戴",
    "CMCG10000035": "音频",
    "CMCG10000018": "平板", "CMCG10000019": "平板",
    "CMCG10000020": "电脑", "CMCG10000400": "电脑",
    # —— 认得出、但不在这六类 ——
    "ISRP12000001": OTHER,          # 礼品
    "CMCG10000040": OTHER,          # HUAWEI Care+
    "CMCG10000034": OTHER,          # 移动电源
    "CMCG10000024": OTHER,          # 路由
    "CMCG10000140": OTHER,          # 手机壳
    "CMCG10000037": OTHER,          # 体脂秤（2026-09-23 补，原先落 UNKNOWN）
    "HWExclusiveAccessories": OTHER,
}

#: 四个池的**优先级** —— 云商侧比玲珑侧准（品类字段是给人看的分类）
SOURCES = ("erp_sales", "erp_stock", "lg_stock", "lg_sales")

#: 每个来源用哪张表
TABLES = {
    "erp_sales": ERP_SALES,
    "erp_stock": ERP_STOCK,
    "lg_stock": LG_STOCK,
    "lg_sales": LG_SALES,
}


def classify(value, table) -> Optional[str]:
    """一个品类值 → 六类之一 / `OTHER` / `UNKNOWN` / `None`（空）。"""
    text = str(value or "").strip()
    if not text:
        return None
    return table.get(text, UNKNOWN)


def classify_sales(bill_class, item_name) -> Tuple[str, Optional[str]]:
    """池C 一行的品类 —— **商品名前缀优先，一级分类兜底**（用户 2026-09-23）。

    返回 `(品类, 要收进 unknown_words 的原词 or None)`。

    ⚠⚠ 为什么不能只看 `一级分类`（第一版的 bug）：
    池C 的 `一级分类` 是**粗类** —— 表带 202 行、眼镜 74 行全归「智能穿戴」
    ⇒ 判成「穿戴」参与对账；而池D 按商品名前缀（`表带/…`→其它）和玲珑侧
    （`华为专属配件`→其它）都判配件 ⇒ 粗类把两侧的细判盖掉 ⇒ **表带混进 BC**。
    池D 的商品名格式和池C **完全一样**（`表带/华为/…`），所以先拿同一个
    `ERP_STOCK` 词表按前缀判；前缀认不出（`手机贴膜` / `维修费` 等 329 个词）
    才回落 `一级分类`。

    ⚠ unknown 只在**两边都认不出**时收：前缀认出 = 词表够用；
    前缀认不出但一级分类认出 = 兜住了（不报）；两边都不行才要人去补表。
    """
    head = head_of(item_name)
    if head and head in ERP_STOCK:
        return ERP_STOCK[head], None
    cat = classify(bill_class, ERP_SALES)
    if cat == UNKNOWN:
        return cat, str(bill_class or "").strip() or head or None
    if cat is None and head:
        # 一级分类是空的、前缀又认不出 —— 按"没见过"报，别当空的混过去
        return UNKNOWN, head
    return cat, None


def head_of(pro_name) -> str:
    """池D 的商品名 → 前缀（`智能手机/华为/...` → `智能手机`）。"""
    return str(pro_name or "").split("/")[0].strip()


def _rank_within(cat) -> int:
    """**池内**多行取舍：六类(2) > 其它(1) > 空/未知(0) —— 具体的赢。

    池内的多行是**同一侧的事实**（这台机器在云商开过手机单、同串号还挂了
    HUAWEI Care+ 行）—— 取"这台机器是什么"就该取具体的那个：
    取 OTHER 会把 `8BBUT26820011620` 这种**真手机**连带 Care+ 一起判成配件剔掉。
    """
    if cat in CATS:
        return 2
    if cat == OTHER:
        return 1
    return 0


def _rank_between(cat) -> int:
    """**池间**冲突取舍：其它(2) > 六类(1) > 空/未知(0) —— **排除的赢**。

    ⚠⚠ 这条是 2026-09-23 用户拍的，**推翻了**第一版「六类优先 / 云商最准」：
    表带的案例里，池C 粗类说「穿戴」、池D 和玲珑侧细判都说「配件」——
    按旧规则粗类的六类赢 ⇒ 表带进 BC。用户要的是**表带这类配件不出现**，
    所以两侧说法矛盾时信「不参与」那一侧（宁可少一条差异，不报假差异）。

    ⚠ 代价已实测量化（参考库全量）：掉 30 台 = 15 表带眼镜 + 12 音箱 + 3 手机，
    前两类本就该剔；3 手机那类由 `_rank_within` 的池内先取具体拦掉，
    两层规则缺一不可。
    """
    if cat == OTHER:
        return 2
    if cat in CATS:
        return 1
    return 0


def merge(sources: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """`{来源: {串号: 品类}}` → `{串号: 品类}` —— **一台机器只有一个品类**。

    按 `SOURCES` 的顺序遍历，**池间冲突排除优先**（`_rank_between`）；
    平手（两边都是六类）保留先到的 = 云商侧（`SOURCES` 里云商在前）。
    """
    out: Dict[str, str] = {}
    for name in SOURCES:
        for sn, cat in (sources.get(name) or {}).items():
            if _rank_between(cat) > _rank_between(out.get(sn)):
                out[sn] = cat
    return out


def keep_set(resolved: Dict[str, str]) -> set:
    """哪些串号参与对账（六类）。"""
    return set(sn for sn, cat in resolved.items() if cat in CATS)


def summary(resolved: Dict[str, str], unknown_words: Optional[Dict[str, int]] = None,
            total: int = 0) -> dict:
    """排除统计 —— **必须能报出来**（口径不许静默）。

    `unknown_words` 由**取数侧**收集（它在 classify 那一刻手里才有原词）：
    `{原词: 出现次数}`。收原词而不是台数 —— 出问题时人要看到"哪个词不认识"，
    才可能去补映射表。
    """
    out = {c: 0 for c in CATS}
    other = unknown = empty = 0
    for cat in resolved.values():
        if cat in CATS:
            out[cat] += 1
        elif cat == OTHER:
            other += 1
        elif cat == UNKNOWN:
            unknown += 1
        else:
            empty += 1
    out["其它"] = other
    out["未知"] = unknown
    out["空"] = empty
    out["_unknown_words"] = dict(unknown_words or {})
    out["_total"] = total or len(resolved)
    return out
