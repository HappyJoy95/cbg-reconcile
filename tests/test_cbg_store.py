"""华为 CBG 客户端的**门店校验**。

⚠ 这一条是"一个账号能看两个店"引出来的，但**实现方式改过一次**，别改回去：

最初写的是"发现别家店就 raise"，结果**一次正常的每日对账会被直接掐死**
（`check` 里 `CbgError` → `EXIT_FETCH` → 不出报告）—— 实测就是这么让定时任务
跑不出结果的。**一个后加的安全检查，不该有能力弄挂一条本来能跑的产线。**

现在：**只警告、不拦、也不丢弃**。
不丢弃很重要 —— 丢掉别家的那几单会让报量集合变少，
那几单的串号会被算成「未报量」，**报告反而是错的**。
"""

import unittest
from unittest import mock
from contextlib import redirect_stderr
import io

from src.cbg import DETAIL_PATH, LIST_PATH, CbgClient, CbgError, ReportedOrder

STORE_A = "SCN231409"
STORE_B = "SCN231410"


def _client(store_code, responses):
    """搭一个不打网络的 CbgClient。`responses` 是 {path: 返回的 json}。"""
    c = CbgClient(session=mock.Mock(), store_code=store_code)
    calls = []

    def fake_request(method, path, payload=None, params=None):
        calls.append({"method": method, "path": path,
                      "payload": payload or {}, "params": params or {}})
        got = responses[path]
        return got(payload) if callable(got) else got

    c._request = fake_request
    return c, calls


def _list_page(orders):
    return {"status": "success", "result": orders, "pageVO": {"totalPages": 1}}


def _order(code, doc="D1"):
    return {"documentNo": doc, "storeCode": code}


class TestForeignOrdersOnlyWarn(unittest.TestCase):
    """⚠ **绝不阻断出报告。**

    加这道检查的第一版是 `raise` —— 于是 `check` 走到 `EXIT_FETCH`，
    那天门店的定时对账**一单报告都没出**。安全网不能变成产线故障。
    """

    def _run(self, store_code, orders):
        c, calls = _client(store_code, {LIST_PATH: _list_page(orders)})
        buf = io.StringIO()
        with redirect_stderr(buf):
            got = c.list_orders(1, 2)
        return got, buf.getvalue(), calls

    def test_matching_store_is_silent(self):
        got, err, calls = self._run(STORE_A, [_order(STORE_A, "D1")])
        self.assertEqual(len(got), 1)
        self.assertEqual(err, "", "本店的数据不该有任何输出")
        self.assertEqual(calls[0]["payload"].get("storeCode"), STORE_A)

    def test_foreign_orders_do_not_raise(self):
        """拿到别家店的数据 → **照收、只吼一声**。"""
        got, err, _ = self._run(STORE_A, [_order(STORE_B)])
        self.assertEqual(len(got), 1, "不许丢 —— 丢了会把报量算少")
        self.assertIn("警告", err)
        self.assertIn(STORE_A, err)
        self.assertIn(STORE_B, err)

    def test_mixed_list_keeps_everything(self):
        """混着的时候**两边都要留着** —— 只留本店的会让报告算错。"""
        got, err, _ = self._run(STORE_A, [_order(STORE_A, "D1"),
                                          _order(STORE_B, "D2"),
                                          _order(STORE_B, "D3")])
        self.assertEqual(len(got), 3)
        self.assertIn("2 单", err, "要说清混了几单")

    def test_warning_points_at_the_real_risk(self):
        """要提醒"多店账号填错不会报错"，也要留"可能只是格式不同"的余地。"""
        _, err, _ = self._run(STORE_A, [_order(STORE_B)])
        self.assertIn("静默算错", err)

    def test_missing_store_code_field_is_silent(self):
        """接口没给 storeCode → 什么都不该说（不能凭空怀疑）。"""
        got, err, _ = self._run(STORE_A, [{"documentNo": "D1"}, {"documentNo": "D2"}])
        self.assertEqual(len(got), 2)
        self.assertEqual(err, "")

    def test_no_configured_store_skips_the_check(self):
        got, err, calls = self._run("", [_order(STORE_B)])
        self.assertEqual(len(got), 1)
        self.assertEqual(err, "")
        self.assertNotIn("storeCode", calls[0]["payload"])

    def test_case_and_spaces_are_normalised(self):
        got, err, _ = self._run(STORE_A, [_order(" scn231409 ")])
        self.assertEqual(len(got), 1)
        self.assertEqual(err, "", "大小写/空格不同不算另一家店")

    def test_takes_all_pages_even_after_a_warning(self):
        """⚠ 警告之后**必须继续翻页** —— 第一页看到别家的就停，
        等于把本店后面几页的数据也丢了。"""
        seen = {"n": 0}

        def pages(payload):
            seen["n"] += 1
            page = payload.get("curPage", 1)
            code = STORE_B if page == 1 else STORE_A
            return {"status": "success", "result": [_order(code, f"D{page}")],
                    "pageVO": {"totalPages": 3}}

        c, _ = _client(STORE_A, {LIST_PATH: pages})
        with redirect_stderr(io.StringIO()):
            got = c.list_orders(1, 2)
        self.assertEqual(seen["n"], 3, "三页都要拉完")
        self.assertEqual(len(got), 3)


class TestOrderSnsForeignOrders(unittest.TestCase):
    """详情接口是**唯一带 SN 的投影** —— 同样只警告，不拦。"""

    def _detail(self, code):
        return {DETAIL_PATH: {"status": "success", "result": {
            "documentNo": "D1", "orderNo": "O1", "storeCode": code,
            "details": [{"sn": "123456789012345"}],
        }}}

    def test_matching_store_is_silent(self):
        c, _ = _client(STORE_A, self._detail(STORE_A))
        buf = io.StringIO()
        with redirect_stderr(buf):
            got = c.order_sns("D1")
        self.assertIsInstance(got, ReportedOrder)
        self.assertEqual(got.sns, ["123456789012345"])
        self.assertEqual(buf.getvalue(), "")

    def test_foreign_store_still_returns_the_order(self):
        c, _ = _client(STORE_A, self._detail(STORE_B))
        buf = io.StringIO()
        with redirect_stderr(buf):
            got = c.order_sns("D1")
        self.assertEqual(got.sns, ["123456789012345"], "不许丢")
        self.assertIn("警告", buf.getvalue())

    def test_missing_field_is_silent(self):
        c, _ = _client(STORE_A, {DETAIL_PATH: {"status": "success", "result": {
            "documentNo": "D1", "details": [{"sn": "123456789012345"}]}}})
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(c.order_sns("D1").sns, ["123456789012345"])
        self.assertEqual(buf.getvalue(), "")
