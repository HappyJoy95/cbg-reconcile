# -*- coding: utf-8 -*-
"""待领清单门禁 —— 登录后的 role_scope 滤店 / 越权写与越权提交。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import web


def _scope(role, stores):
    """stores=[] → 空集合（什么都看不到）；None → 平台全部。"""
    if stores is None:
        store_set = None
    else:
        store_set = set(stores)
    return {
        "role": role,
        "label": "测试·%s" % role,
        "stores": store_set,
        "can": web._can_for(role),
        "pages": [],
        "who": "张三",
        "account": "acc",
        "kind": "",
        "needs_linglong": False,
    }


class Test待领滤店(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        self.app = web.App(self.root, "config/store-X.yaml")

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_rows(self, *stores):
        rows = []
        for i, st in enumerate(stores):
            rows.append({
                "store": st, "sn": "SN%02d" % i, "name": "机器%s" % st,
                "status_key": "sn:SN%02d#a" % i, "status": "pending",
                "category": "手机", "activity_id": "a",
            })
        return {
            "ok": True, "rows": rows,
            "summary": {"total": len(rows), "pending": len(rows),
                        "claimed": 0, "na": 0, "claimed_rate": 0.0},
            "note": "", "role": "", "scope": "",
        }

    def test_门店只看到本店(self):
        def load(root=None, stores=None, day=None):
            # 模拟 compute 已按 stores 粗滤，再经 web 用 scope_store_ok 精滤
            all_rows = self._fake_rows("甲店", "乙店").get("rows")
            if stores is not None:
                all_rows = [r for r in all_rows if r["store"] in stores]
            d = self._fake_rows("甲店", "乙店")
            d["rows"] = all_rows
            return d

        sc = _scope("store", ["甲店"])
        # 别名：scope_store_ok 需要 stores 集合含甲店
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch("src.features.tools.claim.pending.compute.load", load):
            d = self.app.claim_pending()
        self.assertEqual([r["store"] for r in d["rows"]], ["甲店"])
        self.assertEqual(d["store_filter"], "甲店")
        self.assertTrue(d["scoped"])

    def test_门店改别店状态_403语义(self):
        sc = _scope("store", ["甲店"])
        rows = self._fake_rows("甲店")["rows"]
        # 键属于乙店
        key = "sn:OTHER#act"
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch.object(web.App, "_claim_rows_in_scope",
                               lambda self: rows):
            r = self.app.claim_status_set(key, "claimed")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("forbidden"))
        self.assertIn("门店范围", r["why"])

    def test_门店改本店状态_ok(self):
        sc = _scope("store", ["甲店"])
        rows = self._fake_rows("甲店")
        key = rows["rows"][0]["status_key"]
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch.object(web.App, "_claim_rows_in_scope",
                               lambda self: rows["rows"]):
            r = self.app.claim_status_set(key, "claimed")
        self.assertTrue(r["ok"], r)

    def test_门店查别店SN_拒(self):
        sc = _scope("store", ["甲店"])
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch.object(web.App, "_claim_sn_allowed",
                               lambda self, sn: sn != "FOREIGN"):
            r = self.app.claim_query("FOREIGN")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("forbidden"))

        r2 = self.app.claim_submit_online("FOREIGN", "care-fold-202609", "k")
        self.assertFalse(r2["ok"])
        self.assertTrue(r2.get("forbidden"))

    def test_平台范围None_放行本机清单(self):
        sc = _scope("platform", None)
        rows = self._fake_rows("任意店")["rows"]
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch.object(web.App, "_claim_rows_in_scope",
                               lambda app: rows):
            self.assertTrue(self.app._claim_sn_allowed("SN00"))

    def test_门禁认claim_sn_反查到的真SN可提交(self):
        """86 码销售行：状态键仍是 sn，但在线提交带的是 claim_sn。"""
        sc = _scope("store", ["甲店"])
        rows = [{
            "store": "甲店",
            "sn": "864468081285466",
            "claim_sn": "88Z9K26819007417",
            "sn_kind": "imei",
            "name": "nova 16 Pro",
            "status_key": "sn:864468081285466#a",
            "status": "pending",
            "category": "手机",
            "activity_id": "a",
        }]
        with mock.patch.object(web, "role_scope", lambda _a: sc), \
             mock.patch.object(web.App, "_claim_rows_in_scope",
                               lambda app: rows):
            # 销售侧 86 码（状态键）—— 允许
            self.assertTrue(self.app._claim_sn_allowed("864468081285466"))
            # 反查到的真 SN —— 在线领取要提交这个
            self.assertTrue(self.app._claim_sn_allowed("88Z9K26819007417"))
            # 别家的号
            self.assertFalse(self.app._claim_sn_allowed("FOREIGN"))


class Test已领取自动标(unittest.TestCase):
    def test_popupInfo3_失败但自动标已领(self):
        from unittest import mock as m
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        (root / "out").mkdir(parents=True, exist_ok=True)
        app = web.App(root, "config/store-X.yaml")
        sc = _scope("store", ["甲店"])
        key = "sn:ABC#care-fold-202609"
        row = {"store": "甲店", "sn": "ABC", "status_key": key,
               "name": "机器", "status": "pending"}

        def fake_claim(sn, privilege_codes=None):
            return {"ok": False, "code": "E20", "why": "您的设备已经领取过权益，无法再领取",
                    "popup": "您的设备已经领取过权益，无法再领取",
                    "popup_key": "popupInfo3", "sn": sn, "submitted": 1}

        with m.patch.object(web, "role_scope", lambda _a: sc), \
             m.patch.object(web.App, "_claim_rows_in_scope", lambda self: [row]), \
             m.patch.object(web.App, "_claim_sn_allowed", lambda self, sn: True), \
             m.patch.object(web.App, "_claim_key_allowed", lambda self, k: k == key), \
             m.patch("src.features.tools.claim.activities.catalog.activity_by_id",
                     lambda *a, **k: {"privilege_codes": ["881"]}), \
             m.patch("src.features.tools.claim.submit.claim", fake_claim):
            r = app.claim_submit_online("ABC", "care-fold-202609", key)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("already_claimed"))
        self.assertTrue(r.get("local_marked"))
        from src.features.tools.claim.pending import status as st
        data = st.load(root)
        self.assertEqual(data[key]["status"], "claimed")


class Test查询阶段已领取也标(unittest.TestCase):
    def test_query阶段E20也标已领(self):
        from unittest import mock as m
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp())
        (root / "out").mkdir(parents=True, exist_ok=True)
        app = web.App(root, "config/store-X.yaml")
        sc = _scope("store", ["甲店"])
        key = "sn:ABC#care-fold-202609"
        row = {"store": "甲店", "sn": "ABC", "status_key": key,
               "name": "机器", "status": "pending"}

        def fake_claim(sn, privilege_codes=None):
            # 查询阶段就返回已领取（claim() 第一步 query_rights 失败）
            return {"ok": False, "code": " _1:E20, _2:E20",
                    "why": "您的设备已经领取过权益，无法再领取",
                    "popup": "您的设备已经领取过权益，无法再领取",
                    "popup_key": "popupInfo3", "rights": [], "sn": sn}

        with m.patch.object(web, "role_scope", lambda _a: sc), \
             m.patch.object(web.App, "_claim_rows_in_scope", lambda self: [row]), \
             m.patch.object(web.App, "_claim_sn_allowed", lambda self, sn: True), \
             m.patch.object(web.App, "_claim_key_allowed", lambda self, k: k == key), \
             m.patch("src.features.tools.claim.activities.catalog.activity_by_id",
                     lambda *a, **k: {"privilege_codes": ["881"]}), \
             m.patch("src.features.tools.claim.submit.claim", fake_claim):
            r = app.claim_submit_online("ABC", "care-fold-202609", key)
        self.assertTrue(r.get("already_claimed"), r)
        self.assertTrue(r.get("local_marked"), r)
        from src.features.tools.claim.pending import status as st
        self.assertEqual(st.load(root)[key]["status"], "claimed")


if __name__ == "__main__":
    unittest.main()
