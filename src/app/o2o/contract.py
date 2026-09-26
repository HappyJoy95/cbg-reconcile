"""天猫（淘宝即时零售）库存表契约 —— 6 列，列序照抄官方模板别调。

来源：官方 `StoreInventoryTemplate.xlsx`（sheet「门店商品库存」，表头第 1 行），
2026-09-23 实测解出，详见开发目标文档「三」。

两个必须写死的坑（都真撞过）：

1. **id 必须按 E 记法转整数** —— 平台导出的 xlsx 把 itemid/skuId/门店id 存成
   `1.030667544278E12` 这种科学计数法（原始 XML 就是这样，不是显示问题）。
   13 位实测精度没丢（float64 安全范围 < 2^53），但**直接当字符串用必错**。
2. **写回表格时 id 一律给 str** —— 传 int/float，Excel 又会显示成科学计数法，
   平台按文本解析就炸。
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

#: sheet 名 —— 上传接口按它找表（模板里就这一个 sheet）
SHEET_NAME = "门店商品库存"

#: 6 列表头**原文**（列序 = 下标序，别重排）
HEADERS = [
    "商品itemid(必填)",
    "门店id(必填)",
    "商品SkuId(有SKU商品必填)",
    "商家货品编码",
    "sku 名称（非必填)",
    "库存数（导入后库存数更新",
]

_EPOCH = re.compile(r"^[-+]?\d+(?:\.\d+)?[eE][-+]?\d+$")
_INTLIKE = re.compile(r"^\d+\.0?$")


def excel_id(value):
    """把导出文件里的 id（E 记法/浮点/整数/字符串）规整成**纯数字字符串**。

    >>> excel_id("1.030667544278E12")
    '1030667544278'
    >>> excel_id(1167502199.0)
    '1167502199'

    ⚠ 超过 2^53 的值 float64 已经丢精度了，那种情况**宁可报错也不猜** ——
    悄悄给个错 id 会把库存传到别人的链接上。
    """
    if value is None:
        raise ValueError("id 不能是空")
    s = str(value).strip()
    if not s:
        raise ValueError("id 不能是空")
    if _EPOCH.match(s) or "." in s:
        f = float(s)
        if abs(f) >= 2 ** 53:
            raise ValueError("id 超出 float 精度，拒绝猜测：%r" % (value,))
        if not f.is_integer():
            raise ValueError("id 不是整数：%r" % (value,))
        return str(int(f))
    if _INTLIKE.match(s):                      # "123.0" 这种
        return str(int(float(s)))
    if s.isdigit():
        return s
    raise ValueError("认不出的 id：%r" % (value,))


def build_row(item_id, store_id, sku_id, code, sku_name, qty):
    """按模板列序拼一行（全部 str；库存数保持 int 便于求和/校验）。

    A 商品itemid / B 门店id / C SkuId —— **必填**，缺一个就地报错（fail fast，
    比平台回一句"格式错误"好查）；D/E 允许空；F 库存数必须是 ≥0 的整数。
    """
    it = excel_id(item_id)
    st = excel_id(store_id)
    if sku_id is None or str(sku_id).strip() == "":
        raise ValueError("有 SKU 商品必须给 SkuId（itemid=%s）" % (it,))
    sk = excel_id(sku_id)
    q = int(qty)
    if q < 0:
        raise ValueError("库存数不能为负：%r" % (qty,))
    return [it, st, sk, "" if code is None else str(code).strip(),
            "" if sku_name is None else str(sku_name), q]


def validate_rows(rows: List[List[Any]]) -> Optional[str]:
    """整表校验（上传前的最后闸门）：列数/列头/必填/库存数。通过回 None。"""
    for i, r in enumerate(rows):
        if len(r) != len(HEADERS):
            return "第 %d 行列数 %d ≠ %d" % (i + 1, len(r), len(HEADERS))
        if not str(r[0]).isdigit() or not str(r[1]).isdigit():
            return "第 %d 行 itemid/门店id 必须是纯数字：%r" % (i + 1, r[:3])
        if r[2] in ("", None) or not str(r[2]).isdigit():
            return "第 %d 行 SkuId 必须是纯数字：%r" % (i + 1, r[2])
        if not isinstance(r[5], int) or r[5] < 0:
            return "第 %d 行 库存数必须是 ≥0 的整数：%r" % (i + 1, r[5])
    return None
