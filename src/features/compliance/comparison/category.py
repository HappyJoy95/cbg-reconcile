# -*- coding: utf-8 -*-
"""四池的**品类范围** —— 只有六类串号参与对账（纯逻辑，**只 import 标准库**）。

用户 2026-09-21：「四池对比时，只对比手机、穿戴、音频、平板、电脑、智慧屏这些，
其他的东西不对比」；「**电脑**」= 只算笔记本（MateBook 这类）。

⚠⚠ **这跟周度达成那张「产品列 → 商品编码」映射表是两码事**（用户同日明确：
「这个和映射表没关系，这个只是五项合规里面的功能」）。那张在 `features/sales/attain`，
这张在 `features/compliance/comparison` —— **两条口径各自独立**，别合成一处。

## 为什么不能各池各判一次

四个池装的是**同一批机器**（同一批串号在池间流动）。品类是**这台机器**的属性，
所以四个池谁认得出来就用谁的（云商侧最准，优先），合并成 `{串号: 品类}` 再统一过滤。

各池各判的话，同一台机器可能在一侧算「手机」、另一侧算「配件」⇒ 一侧排掉一侧保留
⇒ **凭空多出 AD/BC** —— 那正是本项目最忌讳的假差异。

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


def head_of(pro_name) -> str:
    """池D 的商品名 → 前缀（`智能手机/华为/...` → `智能手机`）。"""
    return str(pro_name or "").split("/")[0].strip()


def _rank(cat) -> int:
    """合并时的取舍：**六类 > 其它 > 空 / 未知**。

    ⚠ 同一台机器两边说法不一致时（理论上不该有），选**更具体**的那个：
      多带进一台六类的机器只是多点噪音，而少算一台会让它在差异里凭空出现。
    """
    if cat in CATS:
        return 2
    if cat == OTHER:
        return 1
    return 0


def merge(sources: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """`{来源: {串号: 品类}}` → `{串号: 品类}` —— **一台机器只有一个品类**。

    按 `SOURCES` 的顺序取，认得越具体越优先（见 `_rank`）。
    """
    out: Dict[str, str] = {}
    for name in SOURCES:
        for sn, cat in (sources.get(name) or {}).items():
            if _rank(cat) > _rank(out.get(sn)):
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
