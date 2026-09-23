# -*- coding: utf-8 -*-
"""华为领取直提 —— 报文组装 / 活动码过滤（不打真网关）。"""

from __future__ import annotations

import unittest
from unittest import mock

from src.features.tools.claim import submit
from src.features.tools.claim.activities import catalog


class TestBuildClaimItems(unittest.TestCase):
    def test_按活动码过滤(self):
        rights = [
            {"privilegeCode": "8813043801", "orderNo": "O1", "subActivityCode": "A"},
            {"privilegeCode": "999", "orderNo": "O2", "subActivityCode": "B"},
        ]
        items = submit.build_claim_items("SN1", rights, ["8813043801"])
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["sn"], "SN1")
        self.assertEqual(it["scCode"], "8813043801")
        self.assertEqual(it["orderNo"], "O1")
        self.assertEqual(it["subActivityCode"], "A")
        self.assertEqual(it["channelSource"], "9")
        self.assertEqual(it["countryCode"], "CN")
        self.assertTrue(it["receiveDate"])
        self.assertTrue(it["lastUpdate"])

    def test_无过滤则全进(self):
        rights = [
            {"privilegeCode": "1", "orderNo": "A"},
            {"privilegeCode": "2", "orderNo": "B"},
        ]
        self.assertEqual(len(submit.build_claim_items("S", rights, None)), 2)

    def test_空码跳过(self):
        rights = [{"privilegeCode": "", "orderNo": "A"},
                  {"privilegeCode": "9", "orderNo": "B"}]
        items = submit.build_claim_items("S", rights, None)
        self.assertEqual([i["scCode"] for i in items], ["9"])


class Test内置活动带privilege码(unittest.TestCase):
    def test_主活动有码(self):
        a = catalog.activity_by_id("care-fold-202609")
        self.assertTrue(a and a.get("privilege_codes"))
        self.assertIn("8813043801", a["privilege_codes"])

    def test_nova有码(self):
        a = catalog.activity_by_id("nova16-gift-202609")
        self.assertTrue(a and a.get("privilege_codes"))


class TestClaimQueryMock(unittest.TestCase):
    def test_查询失败原样返回(self):
        with mock.patch.object(submit, "_post",
                               return_value={"_error": "连不上华为网关：timeout"}):
            r = submit.query_rights("SN")
        self.assertFalse(r["ok"])
        self.assertIn("连不上", r["why"])

    def test_86码本地拦截不打网关(self):
        # 云商串号列是 IMEI 时拿去查华为只会 sn.NotFound（2026-09-23 用户反馈）
        with mock.patch.object(submit, "_post") as post:
            r = submit.query_rights("864468081285466")
        post.assert_not_called()
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "IMEI_NOT_SN")
        self.assertEqual(r.get("sn_kind"), "imei")
        self.assertIn("86码", r["why"])
        self.assertIn("不是设备SN", r["why"])

    def test_真SN仍走网关(self):
        with mock.patch.object(submit, "_post", return_value={
            "responseCode": "200", "responseData": [], "responseDesc": "",
        }) as post:
            r = submit.query_rights("7ED9K26611031362")
        post.assert_called_once()
        self.assertTrue(r["ok"])

    def test_查询200带rights(self):
        with mock.patch.object(submit, "_post", return_value={
            "responseCode": "200", "responseData": [{"privilegeCode": "1"}],
            "responseDesc": "",
        }):
            r = submit.query_rights("SN")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["rights"]), 1)

    def test_设备不存在码(self):
        with mock.patch.object(submit, "_post", return_value={
            "responseCode": "5000", "responseData": [],
            "responseDesc": "设备信息不存在",
        }):
            r = submit.query_rights("SN")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "5000")
        # 官网无 5000 组 → 落 popupInfo5 兜底（与官网 default 一致）
        self.assertIn("不符合领取条件", r["why"])
        self.assertIn("popup", r)


class Test官网popupInfo同步(unittest.TestCase):
    def test_E20是重复领取(self):
        pop = submit.official_popup(" _1:E20, _2:E20")
        self.assertEqual(pop["popup_key"], "popupInfo3")
        self.assertEqual(pop["popup"], "您的设备已经领取过权益，无法再领取")

    def test_E02是过保文案(self):
        pop = submit.official_popup("E02")
        self.assertEqual(pop["popup_key"], "popupInfo2")
        self.assertIn("不符合领取条件", pop["popup"])

    def test_E10是超激活(self):
        pop = submit.official_popup("E10")
        self.assertEqual(pop["popup_key"], "popupInfo4")

    def test_未知码兜底popup5(self):
        pop = submit.official_popup(" _1:E16, _2:E16")
        self.assertEqual(pop["popup_index"], 5)
        self.assertIn("不符合领取条件", pop["popup"])

    def test_人话优先官网文案(self):
        w = submit.human_query_fail("query award device right fail", "E20")
        self.assertEqual(w, "您的设备已经领取过权益，无法再领取")


class Test错误人话(unittest.TestCase):
    def test_no_bind_rule_用官网兜底文案(self):
        w = submit.human_query_fail(
            "query award device right fail",
            " _1:5000.no_bind_rule, _2:5000.no_bind_rule")
        self.assertIn("不符合领取条件", w)
        self.assertIn("绑定规则", w)

    def test_E20_同步官网重复领取文案(self):
        w = submit.human_query_fail(
            "query award device right fail", " _1:E20, _2:E20")
        self.assertEqual(w, "您的设备已经领取过权益，无法再领取")


class TestClaimStepMock(unittest.TestCase):
    def test_成功路径_过滤后提交(self):
        query = {"responseCode": "200", "responseData": [
            {"privilegeCode": "AAA", "orderNo": "1", "subActivityCode": "S"},
            {"privilegeCode": "BBB", "orderNo": "2", "subActivityCode": "S"},
        ]}
        claim_res = {"responseCode": "200", "responseDesc": "ok"}
        posts = []

        def fake_post(path, payload, extra_headers=None):
            posts.append((path, payload))
            if path.endswith("awardDeviceRightV3/1000"):
                return dict(query)
            return dict(claim_res)

        with mock.patch.object(submit, "_post", side_effect=fake_post):
            r = submit.claim("SNX", privilege_codes=["AAA"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["claimed"], 1)
        # 第二笔是领取数组
        self.assertEqual(len(posts), 2)
        arr = posts[1][1]
        self.assertIsInstance(arr, list)
        self.assertEqual(arr[0]["scCode"], "AAA")
        self.assertEqual(arr[0]["sn"], "SNX")

    def test_过滤后为空不提交(self):
        query = {"responseCode": "200", "responseData": [
            {"privilegeCode": "ZZZ", "orderNo": "1"},
        ]}
        with mock.patch.object(submit, "_post", return_value=dict(query)):
            r = submit.claim("SNX", privilege_codes=["AAA"])
        self.assertFalse(r["ok"])
        self.assertIn("没有匹配", r["why"])


if __name__ == "__main__":
    unittest.main()
