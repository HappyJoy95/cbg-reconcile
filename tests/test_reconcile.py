"""对账规则回归测试。

夹具 = **2026-09-14 的真实数据**（云商 Y 标识 9 行 + 华为已报量 5 个 SN），
人工核对过的期望结果：
    留下 6 行（排除 悦荟卖的 / 丽达茂卖的 / 核销单）
    已报量 4 台 · 未报量 2 台 · 反向差异 1 台
规则一改，这里必须先红。
"""

import unittest

from src.reconcile import (KIND_TRANSFER, KIND_UNKNOWN, SaleRow, classify_reverse,
                           classify_sales, is_own_marker, marker_codes, reconcile,
                           sn_row_index)

EXPERIENCE = {
    "青岛城阳万达店", "青岛城阳万象汇店", "胶南合美MALL店", "青岛顺和汇店",
    "青岛新业广场店", "青岛悦荟店", "青岛胶州龙湖店", "黄岛传媒广场店",
    "青岛丽达茂店", "深蓝中心店", "上街里容滙城店", "青岛CBD万达店",
    "市南金茂湾店", "城阳首创奥莱店",
}
OWN_STORE, MARKER = "青岛新业广场店", "Y"

INCLUDE = ["零售", "分销", "客情单"]
EXCLUDE = ["零售退", "分销退", "核销"]


def _row(sn, item, doc, dtype, seller, pay, amount, marker, clerk=""):
    return {"串号": sn, "商品名称": item, "单号": doc, "单据类型": dtype,
            "门店": seller, "支付时间": pay, "金额": amount, "串号标识": marker, "店员": clerk}


# 09-14 云商里串号标识含 Y 的全部 9 行（真实数据）
SALES_0914 = [
    _row("6UPBB26818007283", "智能手机/华为/畅享90 Pro Max", "SI1937202609140018", "分销",
         "青岛新业广场店", "2026-09-14 11:21:52", "2291.52", "Y,新"),
    _row("8PH0226808000539", "华为笔记本/华为/MateBook Pro S", "LS1937202609140017", "零售",
         "青岛悦荟店", "2026-09-14 12:02:37", "10433", "Y,新"),          # 其他体验店卖的 → 跳过
    _row("7MXTQ26722005113", "耳机麦克/FreeClip 2 典藏版", "SI1937202609140025", "分销",
         "青岛新业广场店", "2026-09-14 21:02:57", "1434.06", "Y,新"),
    _row("860000000000001", "手表/华为/儿童手表5pro", "SI1937202609140026", "分销",
         "青岛新业广场店", "2026-09-14 14:12:48", "844.12", "Y,新"),
    _row("6HQ0226728015671", "智能手机/华为/Pura X Max", "SI1937202609140031", "分销",
         "青岛丽达茂店", "2026-09-14 14:54:30", "11661.47", "Y,新"),       # 其他体验店卖的 → 跳过
    _row("6UNBB26820044877", "智能手机/华为/畅享90 Pro Max", "LS1937202609140074", "零售",
         "青岛新业广场店", "2026-09-14 21:02:49", "2199", "Y,新"),
    _row("PPLMA307742", "保护壳套/华为/Pura X View", "HX1937202608160015", "核销",
         "青岛新业广场店", "2026-09-14 17:13:09", "0", "Y,新"),            # 核销单 → 跳过
    _row("3AX0226331019726", "智能手机/华为/mate X6", "LS1937202609140100", "零售",
         "青岛新业广场店", "2026-09-14 17:35:47", "9500", "Y,新"),
    _row("6HQ0226605000991", "智能手机/华为/Pura X Max", "LS1937202609140098", "零售",
         "青岛新业广场店", "2026-09-14 14:50:20", "11300", "Y,新"),
    # 龙湖(LH)的货，在新业卖出、并被报量在新业名下 —— 「体验店卖另一个体验店的货」的镜像场景。
    # 业务确认：不用管。这条专门用来钉住它别跑进反向差异。
    _row("8BBUT26901016452", "智能手机/华为/Pura X View", "LS1937202609140102", "零售",
         "青岛新业广场店", "2026-09-14 17:26:39", "6999", "LH,新"),
]

EXPERIENCE_MARKERS = {"D", "W", "H", "S", "Y", "T", "LH", "SCM", "M", "GT", "Z", "C", "JM", "K"}

# 华为 09-14 的 5 张订单（payStatus=2）取详情后的 SN
REPORTED_0914 = {
    "7MXTQ26722005113": {"documentNo": "SCN231409126091484149645", "item": "FreeClip 2", "amount": 1499.0, "time": "2026-09-14 21:02:57", "guide": "黄旭东"},
    "6UNBB26820044877": {"documentNo": "SCN231409126091484149609", "item": "畅享 90 Pro Max", "amount": 2199.0, "time": "2026-09-14 21:02:49", "guide": "黄旭东"},
    "3AX0226331019726": {"documentNo": "SCN231409126091484129060", "item": "Mate X6", "amount": 9500.0, "time": "2026-09-14 17:35:47", "guide": "黄旭东"},
    "8BBUT26901016452": {"documentNo": "SCN231409126091484128077", "item": "Pura X View", "amount": 6999.0, "time": "2026-09-14 17:26:39", "guide": "于洋"},
    "6HQ0226605000991": {"documentNo": "SCN231409126091484111704", "item": "Pura X Max", "amount": 8900.0, "time": "2026-09-14 14:50:20", "guide": "黄旭东"},
}


class TestMarker(unittest.TestCase):
    def test_split_and_drop_noise(self):
        self.assertEqual(marker_codes("Y,新"), {"Y"})
        self.assertEqual(marker_codes("新,Y"), {"Y"})     # 顺序不固定，实测两种都出现过
        self.assertEqual(marker_codes("s,新"), {"s"})
        self.assertEqual(marker_codes("新"), set())        # 只有机况词 = 无归属
        self.assertEqual(marker_codes(None), set())

    def test_case_insensitive(self):
        """顺和汇在数据里是小写 s —— 这是真踩过的坑。"""
        self.assertTrue(is_own_marker("s,新", "S"))
        self.assertTrue(is_own_marker("S,新", "s"))

    def test_not_own(self):
        self.assertFalse(is_own_marker("W,新", "Y"))
        self.assertFalse(is_own_marker("jc,新", "Y"))
        self.assertFalse(is_own_marker("外调,新", "Y"))


class TestClassify(unittest.TestCase):
    def test_keeps_only_reportable_rows(self):
        kept, skipped = classify_sales(
            SALES_0914, marker=MARKER, own_store=OWN_STORE,
            experience_stores=EXPERIENCE, include_types=INCLUDE, exclude_types=EXCLUDE)
        self.assertEqual(len(kept), 6)
        sns = {r.sn for r in kept}
        self.assertNotIn("8PH0226808000539", sns, "其他体验店卖的必须跳过（走调拨）")
        self.assertNotIn("6HQ0226728015671", sns, "其他体验店卖的必须跳过（走调拨）")
        self.assertNotIn("PPLMA307742", sns, "核销单必须跳过")
        self.assertEqual(skipped["其他体验店卖出(调拨处理)"], 2)
        self.assertEqual(skipped["排除单据类型:核销"], 1)

    def test_unknown_doc_type_is_counted_not_silently_dropped(self):
        rows = [_row("SN1", "x", "D1", "客情单", OWN_STORE, "", "1", "Y,新"),
                _row("SN2", "x", "D2", "某种新单据", OWN_STORE, "", "1", "Y,新")]
        kept, skipped = classify_sales(rows, marker="Y", own_store=OWN_STORE,
                                       experience_stores=EXPERIENCE,
                                       include_types=INCLUDE, exclude_types=EXCLUDE)
        self.assertEqual(len(kept), 1)
        self.assertEqual(skipped.get("未识别单据类型:某种新单据"), 1)

    def test_rows_without_sn_skipped(self):
        rows = [_row("", "配件", "D1", "零售", OWN_STORE, "", "1", "Y,新")]
        kept, skipped = classify_sales(rows, marker="Y", own_store=OWN_STORE,
                                       experience_stores=EXPERIENCE,
                                       include_types=INCLUDE, exclude_types=EXCLUDE)
        self.assertEqual(kept, [])
        self.assertEqual(skipped["无串号"], 1)


class TestReconcile0914(unittest.TestCase):
    """端到端规则回归：对照 2026-09-14 人工核对的结果。"""

    def setUp(self):
        self.sales, self.skipped = classify_sales(
            SALES_0914, marker=MARKER, own_store=OWN_STORE,
            experience_stores=EXPERIENCE, include_types=INCLUDE, exclude_types=EXCLUDE)
        self.res = reconcile(self.sales, REPORTED_0914,
                             total_rows=len(SALES_0914), skipped=self.skipped,
                             index=sn_row_index(SALES_0914), own_marker=MARKER,
                             experience_markers=EXPERIENCE_MARKERS)

    def test_counts(self):
        self.assertEqual(len(self.sales), 6)
        self.assertEqual(len(self.res.matched), 4)
        self.assertEqual(len(self.res.missing), 2)
        self.assertEqual(len(self.res.reverse), 1)
        self.assertEqual(self.res.total_rows, 10)

    def test_missing_are_the_two_distribution_sales(self):
        """未报量的两台都是本店自己卖出的分销单 —— 这才是要抓的东西。"""
        self.assertEqual({s.sn for s in self.res.missing},
                         {"6UPBB26818007283", "860000000000001"})
        for s in self.res.missing:
            self.assertEqual(s.seller, OWN_STORE)

    def test_other_experience_store_sales_never_show_as_missing(self):
        missing = {s.sn for s in self.res.missing}
        self.assertNotIn("8PH0226808000539", missing)
        self.assertNotIn("6HQ0226728015671", missing)

    def test_reverse_keeps_the_transferred_goods(self):
        """`8BBUT26901016452` 是龙湖调过来的货，在新业卖出并被报量在新业名下。

        业务要求**保留**它 —— 门店靠它核对"调拨过来的货有没有出库"。
        它不该被算成未报量，也不该被丢掉。
        """
        self.assertNotIn("8BBUT26901016452", {s.sn for s in self.res.missing})
        self.assertEqual([r.sn for r in self.res.reverse], ["8BBUT26901016452"])
        item = self.res.reverse[0]
        self.assertEqual(item.kind, KIND_TRANSFER)
        self.assertTrue(item.shipped, "云商里有它的零售单 LS1937202609140102")
        self.assertEqual([y.doc_no for y in item.yun], ["LS1937202609140102"])
        self.assertEqual(item.yun[0].marker, "LH,新")
        self.assertEqual(item.yun[0].seller, "青岛新业广场店")

    def test_reverse_slices(self):
        self.assertEqual(len(self.res.reverse_transfer), 1)
        self.assertEqual(len(self.res.reverse_unknown), 0)
        self.assertEqual(len(self.res.reverse_unshipped), 0, "这台云商里有出库记录")

    def test_unknown_sn_stays_in_reverse(self):
        """云商里根本查不到的 SN 必须留在反向差异里 —— 那才是真异常。"""
        reported = dict(REPORTED_0914)
        reported["NOSUCHSN0000001"] = {"documentNo": "X1", "item": "?", "amount": 1}
        res = reconcile(self.sales, reported, index=sn_row_index(SALES_0914),
                        own_marker=MARKER, experience_markers=EXPERIENCE_MARKERS)
        self.assertIn("NOSUCHSN0000001", [r.sn for r in res.reverse])
        item = next(r for r in res.reverse if r.sn == "NOSUCHSN0000001")
        self.assertEqual(item.kind, KIND_UNKNOWN)
        self.assertFalse(item.shipped)
        self.assertEqual([r.sn for r in res.reverse_unshipped], ["NOSUCHSN0000001"])

    def test_ok_flag(self):
        self.assertFalse(self.res.ok)
        clean = reconcile(self.sales[:1], {"6UPBB26818007283": {"documentNo": "x"}})
        self.assertTrue(clean.ok)


class TestReverseClassify(unittest.TestCase):
    """反向差异只做分类、不做过滤 —— 一条都不能丢。"""

    def _classify(self, rows, reported):
        return classify_reverse(reported, set(), sn_row_index(rows),
                                own_marker="Y", experience_markers=EXPERIENCE_MARKERS)

    def test_other_experience_store_marker_is_transfer(self):
        rows = [_row("S1", "x", "D1", "零售", OWN_STORE, "", "1", "LH,新")]
        items = self._classify(rows, {"S1": {}})
        self.assertEqual(items[0].kind, KIND_TRANSFER)
        self.assertTrue(items[0].shipped)

    def test_own_marker_is_unknown(self):
        """本店标识的货 —— 云商该管销售里却没有，那是真异常，不能归到调拨。"""
        rows = [_row("S2", "x", "D1", "零售", OWN_STORE, "", "1", "Y,新")]
        self.assertEqual(self._classify(rows, {"S2": {}})[0].kind, KIND_UNKNOWN)

    def test_non_store_marker_is_unknown(self):
        rows = [_row("S3", "x", "D1", "零售", OWN_STORE, "", "1", "J,新"),
                _row("S4", "x", "D2", "零售", OWN_STORE, "", "1", "jc,新")]
        for sn in ("S3", "S4"):
            self.assertEqual(self._classify(rows, {sn: {}})[0].kind, KIND_UNKNOWN)

    def test_no_yun_record_is_unknown_and_unshipped(self):
        items = self._classify([], {"S5": {}})
        self.assertEqual(items[0].kind, KIND_UNKNOWN)
        self.assertFalse(items[0].shipped)

    def test_transfer_but_no_shipment_record_is_flagged(self):
        """调拨进来、华为报了量，但云商里查不到出库 —— 这正是门店要追的。"""
        rows = [_row("S6", "x", "D1", "零售退", OWN_STORE, "", "1", "LH,新")]
        items = self._classify(rows, {"S6": {}})
        self.assertEqual(items[0].kind, KIND_TRANSFER)
        self.assertTrue(items[0].shipped, "有云商单据（哪怕是退货单）就算有记录")

        items2 = self._classify([], {"S7": {}})
        self.assertFalse(items2[0].shipped)

    def test_nothing_is_dropped(self):
        """核心承诺：反向差异只分类不丢弃。"""
        rows = [_row("A1", "x", "D1", "零售", OWN_STORE, "", "1", "LH,新")]
        reported = {"A1": {}, "B1": {}, "C1": {}}
        items = self._classify(rows, reported)
        self.assertEqual({i.sn for i in items}, {"A1", "B1", "C1"})

    def test_transfer_items_sort_first(self):
        rows = [_row("Z9", "x", "D1", "零售", OWN_STORE, "", "1", "LH,新")]
        items = self._classify(rows, {"Z9": {}, "A1": {}})
        self.assertEqual([i.sn for i in items], ["Z9", "A1"], "调拨的排前面，先看要核对的")


class TestErpRowParsing(unittest.TestCase):
    def test_from_erp_handles_none(self):
        r = SaleRow.from_erp({"串号": None, "商品名称": None, "单号": None, "单据类型": None,
                              "门店": None, "支付时间": None, "金额": None, "串号标识": None})
        self.assertEqual(r.sn, "")
        self.assertEqual(r.marker, "")


if __name__ == "__main__":
    unittest.main()
