"""POS 合规率的回归测试。

钉的是**规则**，不是实现细节 —— 尤其是那几条一问就发现「我理解错了」的：

* 退货在**退货当月**扣，不在原单当月
* 原单**本来就没进分母**（国补/即时零售/Care+）→ **不扣**
* 原单**查不到** → 不猜、记下来
* 团单**保留**（用户明确说要，走单独申诉）
* 分子 = **非现金**（不是「只算微信支付宝」）
* 分母为 0 → 分数是 `None`（不是 0）
* 两个口径**本来就是不同的数**

改动口径，这里必须先红。
"""

import unittest

from src.features.compliance.pos import pos_metric as pm
from src.features.compliance.pos.pos_metric import (BY_LABEL, BY_REMARK, CARE_SCENARIO_TYPE, CASH_MEDIA,  # noqa: F401
                        TEAM_REMARKS,
                        INSTANT_RETAIL_BUSINESS_TYPE, MonthResult, Order, Returned,
                        both, is_noncash, is_team, months_of, score_all,
                        score_month, totals)


def o(dn, month, amt, noncash=0.0, gb_l=False, gb_r=False, jssl=False, care=False,
      team=False):
    return Order(document_no=dn, month=month, amount=amt, noncash=noncash,
                 is_gb_label=gb_l, is_gb_remark=gb_r,
                 is_instant_retail=jssl, is_care=care, is_team=team)


class TestEligibility(unittest.TestCase):
    def test_普通单进分母(self):
        self.assertTrue(o("a", "2026-09", 100).eligible(BY_LABEL))

    def test_国补按标签排除(self):
        self.assertFalse(o("a", "2026-09", 100, gb_l=True).eligible(BY_LABEL))

    def test_国补按备注排除(self):
        self.assertFalse(o("a", "2026-09", 100, gb_r=True).eligible(BY_REMARK))

    def test_两个口径互不干扰(self):
        """⚠ 只有备注的单：按标签口径**要算进去**，按备注口径不算。"""
        x = o("a", "2026-09", 100, gb_r=True)
        self.assertTrue(x.eligible(BY_LABEL))
        self.assertFalse(x.eligible(BY_REMARK))

    def test_即时零售和care两个口径都排除(self):
        for x in (o("a", "2026-09", 100, jssl=True), o("b", "2026-09", 100, care=True)):
            self.assertFalse(x.eligible(BY_LABEL))
            self.assertFalse(x.eligible(BY_REMARK))

    def test_排除用的编码是实测的那两个(self):
        """⚠ 这两个值是从真实数据比出来的（与字符串认法完全一致），别随手改。"""
        self.assertEqual(INSTANT_RETAIL_BUSINESS_TYPE, 10)
        self.assertEqual(CARE_SCENARIO_TYPE, 10)


class TestScore(unittest.TestCase):
    def test_分子是非现金不是只算微信支付宝(self):
        """用户原话：「不是现金都算在分子里的」—— 花呗分期也要算。"""
        r = score_month([o("a", "2026-09", 100, noncash=80)], [], "2026-09")
        self.assertEqual(r.den, 100)
        self.assertEqual(r.num, 80)

    def test_团单保留(self):
        """用户原话：「算进去吧，这个是要走单独的申诉的」。"""
        team = o("t", "2026-09", 39000, noncash=0)      # 团单是全现金
        r = score_month([team, o("a", "2026-09", 100, noncash=50)], [], "2026-09")
        self.assertEqual(r.den, 39100)
        self.assertEqual(r.num, 50)

    def test_全现金时分子为零_分数是真的0(self):
        r = score_month([o("a", "2026-09", 100)], [], "2026-09")
        self.assertEqual(r.rate, 0)

    def test_分母为零时分数是None不是0(self):
        """⚠ 「这个月没有符合条件的单」和「这个月一分钱没走 POS」是两回事。"""
        r = score_month([o("a", "2026-09", 100, gb_l=True)], [], "2026-09")
        self.assertEqual(r.den, 0)
        self.assertIsNone(r.rate)


class TestReturns(unittest.TestCase):
    def test_退货在退货当月扣_不在原单当月(self):
        """⭐ 用户原话：「当月退货当月扣除」。9 月退 8 月的单 → **9 月扣**。"""
        aug = o("a", "2026-08", 1000, noncash=600)
        sep = o("b", "2026-09", 500, noncash=500)
        ret = Returned(month="2026-09", amount=1000, noncash_refund=600, orig=aug)

        r8 = score_month([aug, sep], [ret], "2026-08")
        self.assertEqual((r8.den, r8.num), (1000, 600), "8 月不该被扣")
        r9 = score_month([aug, sep], [ret], "2026-09")
        self.assertEqual((r9.den, r9.num), (500 - 1000, 500 - 600), "9 月扣掉")

    def test_原单本来就没进分母就不扣(self):
        """退的是国补单 —— 它压根没进分母，扣了反而错。"""
        gb = o("a", "2026-09", 1000, noncash=0, gb_l=True)
        ok = o("b", "2026-09", 500, noncash=500)
        ret = Returned(month="2026-09", amount=1000, noncash_refund=0, orig=gb)
        r = score_month([gb, ok], [ret], "2026-09")
        self.assertEqual((r.den, r.num), (500, 500))
        self.assertEqual(r.cut_den, 0)

    def test_原单查不到时_不扣且记下来(self):
        """⚠ 跨年退货就会这样 —— **不猜**，记下来让人去看。"""
        ret = Returned(month="2027-01", amount=999, noncash_refund=999, orig=None)
        r = score_month([o("b", "2027-01", 500, noncash=500)], [ret], "2027-01")
        self.assertEqual((r.den, r.num), (500, 500))
        self.assertEqual(len(r.orphan_returns), 1)
        self.assertEqual(r.returned, 1, "单数还是要记的")


class TestDeductionFollowsTheOriginal(unittest.TestCase):
    """⭐ 用户原话：「**看这个对应的销售单是算在了分子里还是分母里还是都有，
    扣的时候也做对应的扣减**」。

    ⚠ 这条推翻了第一版实现：那一版拿**退款走什么方式**去扣分子。
    数据里 4 张退货的退款方式**恰好都和原收款方式一致**，所以碰巧同结果 ——
    但原单现金买、退款走微信时就**扣错边**了。**扣哪边，只看原单。**
    """

    def test_原单只进了分母_分子不许扣(self):
        cash_only = o("a", "2026-09", 1000, noncash=0)        # 全额现金 → 只在分母
        ret = Returned(month="2026-09", amount=1000, noncash_refund=1000, orig=cash_only)
        r = score_month([cash_only], [ret], "2026-09")
        self.assertEqual(r.cut_den, 1000)
        self.assertEqual(r.cut_num, 0.0, "原单没进分子，退款走什么方式都不该扣分子")

    def test_原单两边都在_两边都扣(self):
        mixed = o("a", "2026-09", 1000, noncash=600)
        ret = Returned(month="2026-09", amount=1000, noncash_refund=600, orig=mixed)
        r = score_month([mixed], [ret], "2026-09")
        self.assertEqual((r.cut_den, r.cut_num), (1000, 600))

    def test_部分退货按原单非现金占比摊(self):
        """退一半 → 分子只扣一半的非现金（不是把非现金全扣掉）。"""
        mixed = o("a", "2026-09", 1000, noncash=600)
        ret = Returned(month="2026-09", amount=500, noncash_refund=300, orig=mixed)
        r = score_month([mixed], [ret], "2026-09")
        self.assertEqual(r.cut_den, 500)
        self.assertEqual(r.cut_num, 300.0)                     # 600 × 500/1000

    def test_退款方式和原收款方式对不上_要报出来但不影响扣减(self):
        """对不上不一定错（门店可能手工改退款渠道），但值得看一眼。"""
        cash_only = o("a", "2026-09", 1000, noncash=0)
        ret = Returned(month="2026-09", amount=1000, noncash_refund=1000, orig=cash_only)
        r = score_month([cash_only], [ret], "2026-09")
        self.assertEqual(len(r.odd_refunds), 1)
        self.assertEqual(r.cut_num, 0.0, "报出来归报出来，扣减还是看原单")

    def test_原单是全现金时_占比法不会误扣(self):
        """全额现金单 noncash=0 → 占比算出来还是 0，天然安全。"""
        cash_only = o("a", "2026-09", 888, noncash=0)
        ret = Returned(month="2026-09", amount=888, noncash_refund=888, orig=cash_only)
        self.assertEqual(ret.deduction(BY_LABEL), (888, 0.0))

    def test_退货月也要出现在月份列表里(self):
        ret = Returned(month="2027-01", amount=1, noncash_refund=1, orig=None)
        self.assertIn("2027-01", months_of([o("a", "2026-12", 1)], [ret]))


class TestTwoDefinitions(unittest.TestCase):
    def test_两个口径本来就该给出不同的数(self):
        """⚠ 这不是 bug —— 用户就是要看两份。"""
        rows = [o("a", "2026-09", 1000, noncash=600, gb_l=True),   # 只有标签
                o("b", "2026-09", 2000, noncash=1000, gb_r=True),  # 只有备注
                o("c", "2026-09", 500, noncash=500)]
        la = score_month(rows, [], "2026-09", BY_LABEL)
        re_ = score_month(rows, [], "2026-09", BY_REMARK)
        self.assertEqual(la.den, 2500)      # 排掉 a（标签），留 b
        self.assertEqual(re_.den, 1500)     # 排掉 b（备注），留 a
        self.assertNotEqual(la.rate, re_.rate)

    def test_both_返回两份(self):
        got = both([o("a", "2026-09", 100, noncash=50)], [])
        self.assertEqual(set(got), {BY_LABEL, BY_REMARK})
        for v in got.values():
            self.assertEqual(len(v), 1)

    def test_全部月份合计不等于各月相加时_说明有跨月退货(self):
        """交叉核对用：跨月退货会让「合计」比「各月相加」少。"""
        aug = o("a", "2026-08", 1000, noncash=600)
        ret = Returned(month="2026-09", amount=1000, noncash_refund=600, orig=aug)
        ms = score_all([aug], [ret], BY_LABEL)
        self.assertEqual(len(ms), 2)
        self.assertEqual(ms[0].den, 1000)          # 8 月原样
        self.assertEqual(ms[1].den, -1000)         # 9 月只有一笔扣减
        self.assertEqual(totals([aug], [ret], BY_LABEL).den, 0)   # 合计 1000-1000

    def test_零金额的单不改变分数(self):
        r = score_month([o("a", "2026-09", 0, noncash=0), o("b", "2026-09", 100, noncash=100)],
                        [], "2026-09")
        self.assertEqual(r.den, 100)
        self.assertEqual(r.rate, 100.0)


if __name__ == "__main__":
    unittest.main()


class TestCashDefinition(unittest.TestCase):
    """⭐ 用户专门回来纠正过的一条：「**银商mis刷卡算分子吧，属于非现金**」。

    ⚠ 这条规则原来写在 IO 层（`pos_report.load` 里的 `!= "现金"`），
    **纯函数的测试根本覆盖不到** —— 哪天有人改成白名单，测试照样全绿。
    现在挪进内核（`is_noncash`），在这儿钉死。
    """

    def test_现金是唯一算现金的(self):
        self.assertEqual(CASH_MEDIA, "现金")
        self.assertFalse(is_noncash("现金"))

    def test_刷卡算非现金(self):
        """⭐ 用户原话：「银商mis刷卡算分子吧，属于非现金」。"""
        self.assertTrue(is_noncash("银商MIS刷卡"))

    def test_字典里八种收款方式的归属(self):
        used = ["现金", "银商MIS微信", "银商MIS支付宝", "花呗分期"]
        unused = ["银商MIS刷卡", "公对公打款", "华为会员积分", "支付宝"]
        for m in used + unused:
            with self.subTest(media=m):
                self.assertEqual(is_noncash(m), m != "现金")

    def test_没见过的新方式默认算非现金(self):
        """⚠ **宁可多算进分子，也不要静默漏掉** —— 漏掉是分数被低估且看不出原因。

        门店哪天启用一个新收款方式，我们没更新字典，它会走到这里。
        """
        for m in ("云闪付", "数字人民币", "数币", "微信", "银行卡", ""):
            with self.subTest(media=m):
                self.assertTrue(is_noncash(m))

    def test_空值算非现金(self):
        """`None` / `''` 不是「现金」—— 不能因为字段缺失就把整单当现金排除出分子。"""
        for v in (None, "", "   "):
            self.assertTrue(is_noncash(v))


class TestAppealBasis(unittest.TestCase):
    """⭐ 用户 2026-09-16：「申诉后口径也加上吧」。

    团单**默认保留**（用户原话「这个是要走单独的申诉的」）——
    所以现状口径里它压在分母上、把分数拉低；**申诉后**口径才把它排掉。
    **两个数并排看**：现状是申诉的依据，申诉后是"如果通过"的样子。
    """

    def test_团单默认保留在分母里(self):
        t = o("t", "2026-09", 39000, noncash=0, team=True)
        r = score_month([t, o("a", "2026-09", 1000, noncash=600)], [], "2026-09")
        self.assertEqual(r.den, 40000, "现状口径下团单要在分母里")
        self.assertEqual(r.rate, 1.5)

    def test_申诉后口径排掉团单(self):
        t = o("t", "2026-09", 39000, noncash=0, team=True)
        r = score_month([t, o("a", "2026-09", 1000, noncash=600)], [], "2026-09",
                        exclude_team=True)
        self.assertEqual(r.den, 1000)
        self.assertEqual(r.rate, 60.0, "团单是全现金，排掉它分数会明显抬高")

    def test_退货扣减跟着口径走(self):
        """⚠ 原单是团单：现状口径要扣（它在分母里），**申诉后口径不该扣**（它没进）。"""
        t = o("t", "2026-09", 39000, noncash=0, team=True)
        ret = Returned(month="2026-09", amount=39000, noncash_refund=0, orig=t)
        self.assertEqual(score_month([t], [ret], "2026-09").cut_den, 39000)
        self.assertEqual(score_month([t], [ret], "2026-09", exclude_team=True).cut_den, 0)

    def test_is_team_精确匹配(self):
        for v in ("团单", "团单出单"):
            self.assertTrue(is_team(v), v)
        for v in ("团单xx", "国补", "", None, "大团单", "即时零售：xxx"):
            self.assertFalse(is_team(v), v)

    def test_团单常量就是实测那两条(self):
        """⚠ 全库只有这两条；改了要回门店核。"""
        self.assertEqual(TEAM_REMARKS, ("团单", "团单出单"))


class Test官方口径的POS使用率(unittest.TestCase):
    """⭐ 官方 PPT《POS合规：计算逻辑及方法》（用户 2026-09-21 给的）。

        POS使用率 = 1 − (线下现金 + 记账 + 异常) / (异常 + 总金额)   ← 先按天算，再取日均值
        备注：考核数据范围**不包含退货数据**

    ⚠ 跟我们原来那套（`score_month`）**三处不一样**，逐条钉住：
      ① 扣减项 = 现金 **+ 记账**（我们只认「现金」两个字）；
      ② **不扣退货**（我们按退货当月扣）；
      ③ **先按天算再平均**（我们整月汇总）。
    """

    def _order(self, day, pays, **kw):
        return pm.Order(document_no="d" + day, month=day[:7], amount=sum(p.amount for p in pays),
                        noncash=0.0, day=day, pays=tuple(pays), **kw)

    def test_现金和记账都算扣减(self):
        """⚠ 判据是**机器可读**的（开钱箱 / 渠道名「记账」），不是收款方式的名字清单。

        实测：「现金」那一条 `open_cash_drawer=1`；「公对公打款」的
        `media_desc` 是「记账本」、`pay_channel_name` 就是「记账」。
        """
        self.assertTrue(pm.is_cashlike("现金"))
        self.assertTrue(pm.is_cashlike("公对公打款", pay_channel="记账"))
        self.assertTrue(pm.is_cashlike("随便什么", open_cash_drawer=1))
        self.assertFalse(pm.is_cashlike("银商MIS微信", pay_channel="银商MIS"))
        self.assertFalse(pm.is_cashlike("花呗分期", pay_channel="支付宝"))
        # ⚠ 老函数（用户 2026-09-16 定的）**没变** —— 两个口径并存
        self.assertTrue(pm.is_noncash("公对公打款"))

    def test_一天一笔现金一天一笔微信(self):
        """两天：一天全现金、一天全微信 ⇒ 日均值 = (0% + 100%) / 2 = 50%。

        ⚠ 汇总法也是 50%（两天金额一样）—— 这条只是打底；
          真正拉开差距的是下面那条**金额不均**的。
        """
        orders = [self._order("2026-08-01", [pm.Pay("2026-08-01", 100.0, "现金")]),
                  self._order("2026-08-02", [pm.Pay("2026-08-02", 100.0, "银商MIS微信")])]
        got = pm.score_month_official(orders, month="2026-08")
        self.assertEqual(got["rate"], 50.0)
        self.assertEqual(got["days"], 2)
        self.assertEqual(got["deduct"], 100.0)
        self.assertEqual(got["total"], 200.0)

    def test_日均值和汇总法真的不一样(self):
        """⚠⚠ **这是这次改口径最容易出错的地方**：官方是**先按天算再平均**。

        造一天"大额现金" + 一天"小额微信"：
          * 日均值 = (0% + 100%) / 2 = **50%**
          * 汇总法 = 1 − 10000/10100 = **0.99%**（大额那天把整月压死）
        实测数据上这两个数差 **5~15 个点**（09 月：日均 54.24% vs 汇总 69.69%）。
        """
        orders = [self._order("2026-08-01", [pm.Pay("2026-08-01", 10000.0, "现金")]),
                  self._order("2026-08-02", [pm.Pay("2026-08-02", 100.0, "银商MIS微信")])]
        got = pm.score_month_official(orders, month="2026-08")
        self.assertEqual(got["rate"], 50.0, "官方 = 日比率的平均")
        self.assertLess(got["rate_sum"], 2.0, "汇总法会被大额那天压死")
        self.assertNotEqual(got["rate"], got["rate_sum"], "两个数必须能看出差别")

    def test_退货要扣掉(self):
        """官方备注「考核数据范围**不包含退货数据**」= **把退货剔除、算净额**。

        ⚠⚠ 我第一版读反了（当成"退货不参与计算"），用户 2026-09-21 当场纠正：
          「不包含退货数据**那就是要把退货扣除掉呀**」。
        """
        o = self._order("2026-08-01", [pm.Pay("2026-08-01", 1000.0, "现金")])
        o2 = self._order("2026-08-02", [pm.Pay("2026-08-02", 1000.0, "银商MIS微信")])
        no_ret = pm.score_month_official([o, o2], (), month="2026-08")
        ret = pm.Returned(month="2026-08", day="2026-08-02", amount=1000.0, orig=o2)
        with_ret = pm.score_month_official([o, o2], [ret], month="2026-08")
        self.assertEqual(no_ret["rate"], 50.0, "不扣退货：两天一个 0% 一个 100%")
        self.assertEqual(with_ret["rate"], 0.0,
                         "微信那单退掉了 ⇒ 剩下的全是现金 ⇒ 0%")
        self.assertEqual(with_ret["total"], 1000.0, "分母扣掉了被退的那单")
        self.assertEqual(with_ret["deduct"], 1000.0, "分子（现金）不动 —— 原单本来就是现金")

    def test_退货扣在退货当天_不是月初(self):
        """⚠ 扣在**退货发生的那一天**。

        第一版图省事写成"扣在月初 1 号" ⇒ 月初没有销售时那天的净额变成负数、
        `daily_rate` 判"算不出来"跳过 ⇒ **这笔退货就悄悄丢了**
        （实测本店 09 月三张 6,999 一分没扣上）。⇒ 那天没销售就退回**原单那天**。
        """
        o = self._order("2026-08-10", [pm.Pay("2026-08-10", 1000.0, "银商MIS微信")])
        ret = pm.Returned(month="2026-08", day="2026-08-10", amount=1000.0, orig=o)
        days = pm.daily_totals([o], [ret], month="2026-08")
        self.assertEqual(list(days), ["2026-08-10"], "别落到 08-01 去")
        dd, tt = days["2026-08-10"]
        self.assertEqual((dd, tt), (0.0, 0.0), "全退了 ⇒ 那天的额都归零")

    def test_退货那天没销售就退回原单那天(self):
        """退货单和销售不同天（门店先卖、隔几天再退）⇒ 扣原单那天，钱不能丢。"""
        o = self._order("2026-08-05", [pm.Pay("2026-08-05", 800.0, "现金")])
        ret = pm.Returned(month="2026-08", day="2026-08-20", amount=800.0, orig=o)
        days = pm.daily_totals([o], [ret], month="2026-08")
        self.assertEqual(list(days), ["2026-08-05"])
        self.assertEqual(days["2026-08-05"], (0.0, 0.0))

    def test_原单没进分母的退货不扣(self):
        """国补/即时零售/Care+ 的单压根没算进来 ⇒ 退它的时候**不能扣**（扣了反而错）。"""
        gb = self._order("2026-08-01", [pm.Pay("2026-08-01", 5000.0, "现金")],
                         is_gb_label=True)
        ok = self._order("2026-08-02", [pm.Pay("2026-08-02", 100.0, "银商MIS微信")])
        ret = pm.Returned(month="2026-08", day="2026-08-02", amount=5000.0, orig=gb)
        got = pm.score_month_official([gb, ok], [ret], month="2026-08")
        self.assertEqual(got["total"], 100.0, "国补那单没进来，它的退货也不该扣")

    def test_排除的那几类照样不进来(self):
        """国补 / 即时零售 / Care+ 的排除**两套口径都保留**。

        ⚠ 理由是我们**数据**的性质：那几类在库里全走现金（实测国补占支付额 54%），
          不排除的话会被补贴砸掉几十分 —— 而官方那边它们压根不是现金渠道。
        """
        gb = self._order("2026-08-01", [pm.Pay("2026-08-01", 9999.0, "现金")],
                         is_gb_label=True)
        ok = self._order("2026-08-02", [pm.Pay("2026-08-02", 100.0, "银商MIS微信")])
        got = pm.score_month_official([gb, ok], month="2026-08")
        self.assertEqual(got["total"], 100.0, "国补那单没进来")
        self.assertEqual(got["rate"], 100.0)

    def test_没有支付明细的老数据不炸(self):
        """⚠ 老 payload / 手搓的 `Order` 没有 `pays` ⇒ 退化成"天 = 月初"，
        日均值就等于汇总值（不至于没有数，也不至于除以 0）。"""
        o = pm.Order(document_no="d1", month="2026-08", amount=500.0, noncash=200.0)
        got = pm.score_month_official([o], month="2026-08")
        self.assertEqual(got["days"], 1)
        self.assertEqual(got["total"], 500.0)
        self.assertEqual(got["rate"], 100.0, "没有 pays ⇒ 扣减算 0（不知道就别瞎扣）")

    def test_异常金额还没有数据源(self):
        """官方公式里有「异常金额」这一项，我们**没有建议零售价** ⇒ 按 0 算，
        而且要**明说**没算（`abnormal_known=False`），别让人以为已经算进去了。"""
        o = self._order("2026-08-01", [pm.Pay("2026-08-01", 100.0, "现金")])
        got = pm.score_month_official([o], month="2026-08")
        self.assertEqual(got["abnormal"], 0.0)
        self.assertFalse(got["abnormal_known"])

    def test_没有交易的天不进平均(self):
        o = self._order("2026-08-01", [pm.Pay("2026-08-01", 100.0, "银商MIS微信")])
        got = pm.score_month_official([o], month="2026-08")
        self.assertEqual(got["days"], 1, "只有 8-01 有交易 ⇒ 只算一天")


class Test官方那个例子逐格复现(unittest.TestCase):
    """⭐ 用户给的 PPT（《POS合规：计算逻辑及方法》第 4 页）里那个例子，**照着数字复现**：

        时间            1号    2号    3号
        交易总金额        200    300    300
        现金+记账交易金额   50     90     20
        异常金额           50      0    100
        POS使用率         60%    70%    70%
        1-3号期间 POS使用率 = (60% + 70% + 70%) / 3 = 67%

    ⚠ 这个例子一次钉住**三件事**，缺一条都对不上：
      ① 分母 = 总金额 **+ 异常**，分子 = 现金+记账 **+ 异常**；
      ② 统计期 = **日比率的简单平均**（不是整月汇总）；
      ③ **异常金额是每天一个数**（整月一个常数表达不了这三天 50/0/100）。
    """

    def _day(self, day, cash, other):
        """造一天：现金 `cash` + 微信 `other`（微信是非现金，不进扣减项）。"""
        pays = []
        if cash:
            pays.append(pm.Pay(day, float(cash), "现金"))
        if other:
            pays.append(pm.Pay(day, float(other), "银商MIS微信"))
        return pm.Order(document_no="d" + day, month=day[:7],
                        amount=float(cash + other), noncash=float(other),
                        day=day, pays=tuple(pays))

    def setUp(self):
        self.orders = [self._day("2026-08-01", 50, 150),      # 总额 200
                       self._day("2026-08-02", 90, 210),      # 总额 300
                       self._day("2026-08-03", 20, 280)]      # 总额 300
        self.ab = {"2026-08-01": 50.0, "2026-08-02": 0.0, "2026-08-03": 100.0}

    def test_三天各自的比率(self):
        days = pm.daily_totals(self.orders, month="2026-08")
        self.assertEqual(days["2026-08-01"], (50.0, 200.0))
        self.assertEqual(days["2026-08-02"], (90.0, 300.0))
        self.assertEqual(days["2026-08-03"], (20.0, 300.0))
        # 1号 = 1 − (50+50)/(50+200) = 60%
        self.assertAlmostEqual(pm.daily_rate(50, 200, 50) * 100, 60.0, places=6)
        # 2号 = 1 − (0+90)/(0+300) = 70%
        self.assertAlmostEqual(pm.daily_rate(90, 300, 0) * 100, 70.0, places=6)
        # 3号 = 1 − (100+20)/(100+300) = 70%
        self.assertAlmostEqual(pm.daily_rate(20, 300, 100) * 100, 70.0, places=6)

    def test_统计期是日比率的平均(self):
        got = pm.score_month_official(self.orders, month="2026-08",
                                      abnormal_by_day=self.ab)
        # (60 + 70 + 70) / 3 = 66.67（PPT 写 67%，是四舍五入）
        self.assertAlmostEqual(got["rate"], 66.67, places=2)
        self.assertEqual(got["days"], 3)
        self.assertEqual(got["abnormal"], 150.0)
        self.assertTrue(got["abnormal_known"])

    def test_异常金额必须按天(self):
        """⚠ 三天是 50/0/100 —— 整月一个常数表达不了。

        拿"整月 150"去算会得到另一个数（1 − (160+150)/(800+150) = 67.37% 的**汇总**，
        跟日均值的 66.67% 不是一回事）—— 这条就是防"图省事用一个数"。
        """
        got = pm.score_month_official(self.orders, month="2026-08",
                                      abnormal_by_day=self.ab)
        self.assertEqual(got["abnormal"], 150.0)
        flat = pm.score_month_official(self.orders, month="2026-08")
        self.assertEqual(flat["abnormal"], 0.0, "不给就是 0（没有数据源）")
        self.assertNotEqual(got["rate"], flat["rate"], "异常金额进了公式，数就该变")
