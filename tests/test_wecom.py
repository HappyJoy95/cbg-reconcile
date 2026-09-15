"""企微群推送测试。

**不真往群里发** —— 把 requests 换成假的，测的是：
webhook 怎么解析、什么情况该推、markdown 怎么拼（尤其是字节截断）、
以及"markdown 不能 @人，只能另发 text"这条企微限制有没有被正确处理。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import wecom

KEY = "693a91e1-1111-2222-3333-444455556666"
URL = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={KEY}"


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise wecom.requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


class FakeRequests:
    """记录所有发出去的请求。"""

    def __init__(self, payloads=None):
        self.calls = []
        self.payloads = payloads or {}

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if "upload_media" in url:
            return FakeResp(self.payloads.get("upload", {"errcode": 0, "media_id": "MID1"}))
        body = kw.get("json") or {}
        return FakeResp(self.payloads.get(body.get("msgtype"), {"errcode": 0, "errmsg": "ok"}))

    def sent_types(self):
        return [c["json"]["msgtype"] for c in self.calls if "json" in c]


def _wc(**over):
    base = dict(enabled=True, webhook=URL, when="always", mention_all=True, send_file=True)
    base.update(over)
    return wecom.WecomConfig(**base)


class _Ctx:
    """假的 SaleRow / ReverseItem —— 只用到几个属性。"""

    def __init__(self, sn, item="商品", seller="门店", pay="2026-09-14 10:00", amount="100"):
        self.sn, self.item, self.seller, self.pay_time, self.amount = sn, item, seller, pay, amount
        self.info = {"item": item}


class TestExtractKey(unittest.TestCase):
    def test_full_url(self):
        self.assertEqual(wecom.extract_key(URL), KEY)

    def test_bare_key(self):
        self.assertEqual(wecom.extract_key(KEY), KEY)

    def test_url_with_extra_params(self):
        self.assertEqual(wecom.extract_key(f"https://x/y?foo=1&key={KEY}&bar=2"), KEY)

    def test_garbage(self):
        self.assertEqual(wecom.extract_key("随便写点什么"), "")
        self.assertEqual(wecom.extract_key(""), "")

    def test_mask(self):
        self.assertNotIn(KEY, wecom.mask_key(KEY))
        self.assertIn("…", wecom.mask_key(KEY))


class TestLoadConfig(unittest.TestCase):
    def test_reads_config_and_secret(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / "wecom.env"
            env.write_text(f"WECOM_WEBHOOK={URL}\n", encoding="utf-8")
            wc = wecom.load_wecom_config({"wecom": {
                "enabled": True, "env_file": str(env), "when": "only_diff",
                "mention_all": False, "send_file": False}})
            self.assertTrue(wc.enabled)
            self.assertEqual(wc.key, KEY)
            self.assertEqual(wc.when, "only_diff")
            self.assertFalse(wc.mention_all)
            self.assertFalse(wc.send_file)

    def test_defaults_when_mention_and_file(self):
        """默认要 @人、要传文件 —— 门店群里不 @ 就等于没发。"""
        wc = wecom.load_wecom_config({"wecom": {}})
        self.assertTrue(wc.mention_all)
        self.assertTrue(wc.send_file)

    def test_enabled_false_by_default(self):
        self.assertFalse(wecom.load_wecom_config({}).enabled)


class TestShouldSend(unittest.TestCase):
    def test_disabled(self):
        ok, why = wecom.should_send(_wc(enabled=False), True)
        self.assertFalse(ok)
        self.assertIn("没开", why)

    def test_only_diff(self):
        self.assertFalse(wecom.should_send(_wc(when="only_diff"), has_diff=False)[0])
        self.assertTrue(wecom.should_send(_wc(when="only_diff"), has_diff=True)[0])

    def test_missing_webhook(self):
        ok, why = wecom.should_send(_wc(webhook=""), True)
        self.assertFalse(ok)
        self.assertIn("webhook", why)


class TestMarkdown(unittest.TestCase):
    def _ctx(self):
        return {"门店": "青岛新业广场店", "目标日": "2026-09-14", "生成时间": "2026-09-15 11:00"}

    def test_lists_missing_items(self):
        md = wecom.build_markdown(self._ctx(), [_Ctx("SN001"), _Ctx("SN002")], [],
                                  matched=4, total=6)
        self.assertIn("青岛新业广场店", md)
        self.assertIn("未报", md)
        self.assertIn("SN001", md)
        self.assertIn("SN002", md)

    def test_clean_day(self):
        md = wecom.build_markdown(self._ctx(), [], [], matched=6, total=6)
        self.assertIn("全部已报量", md)
        self.assertNotIn("未报量（", md)

    def test_caps_long_list(self):
        many = [_Ctx(f"SN{i:04d}") for i in range(50)]
        md = wecom.build_markdown(self._ctx(), many, [], matched=0, total=50)
        self.assertIn("还有", md)
        self.assertIn(f"**{50 - wecom.MAX_LIST}**", md)
        self.assertLess(len(md.encode("utf-8")), wecom.MARKDOWN_LIMIT)

    def test_long_content_stays_within_limit(self):
        """商品名和列表都封顶了，正常拼出来不会超；这里确认边界，超了也会被 _fit 兜住。"""
        many = [_Ctx(f"SN{i:04d}", item="很长的商品名称" * 30) for i in range(200)]
        md = wecom.build_markdown(self._ctx(), many, [], matched=0, total=200)
        self.assertLessEqual(len(md.encode("utf-8")), wecom.MARKDOWN_LIMIT)
        md.encode("utf-8").decode("utf-8")          # 不能把汉字截成半个
        self.assertIn("还有", md)

    def test_fit_passes_short_text_through(self):
        self.assertEqual(wecom._fit("短"), "短")

    def test_fit_counts_bytes_not_chars(self):
        text = "中" * 3000                          # 9000 字节，但只有 3000 字符
        out = wecom._fit(text)
        self.assertLessEqual(len(out.encode("utf-8")), wecom.MARKDOWN_LIMIT)
        self.assertIn("已截断", out)
        out.encode("utf-8").decode("utf-8")          # 必须仍是合法 UTF-8

    def test_fit_suffix_itself_is_counted(self):
        """截断后缀自己的字节数也要算进去，否则"截断后"仍然超限。"""
        for n in (1300, 1400, 2000):
            out = wecom._fit("中" * n)
            self.assertLessEqual(len(out.encode("utf-8")), wecom.MARKDOWN_LIMIT,
                                 f"{n} 个汉字时截断后仍超限")

    def test_fit_leaves_no_half_character(self):
        for n in range(1270, 1300):                 # 逐个试，找切在半个汉字上的位置
            out = wecom._fit("中" * n)
            self.assertLessEqual(len(out.encode("utf-8")), wecom.MARKDOWN_LIMIT)

    def test_mention_text_only_when_needed(self):
        self.assertEqual(wecom.build_mention_text(self._ctx(), 0, 0), "")
        self.assertIn("未报量", wecom.build_mention_text(self._ctx(), 3))
        self.assertIn("调拨货", wecom.build_mention_text(self._ctx(), 0, 2))


class TestPush(unittest.TestCase):
    def _ctx(self):
        return {"门店": "青岛新业广场店", "目标日": "2026-09-14", "生成时间": "x"}

    def test_mention_first_then_markdown(self):
        """企微 markdown 不能 @人 —— 必须先发一条 text 带 mentioned_list。"""
        fake = FakeRequests()
        with mock.patch.object(wecom, "requests", fake):
            out = wecom.push(_wc(), self._ctx(), [_Ctx("SN1")], [], matched=1, total=2)
        self.assertEqual(fake.sent_types(), ["text", "markdown"])
        text = fake.calls[0]["json"]["text"]
        self.assertEqual(text["mentioned_list"], ["@all"])
        self.assertIn("未报量", text["content"])
        self.assertIn("@所有人", out)

    def test_no_mention_when_no_diff(self):
        fake = FakeRequests()
        with mock.patch.object(wecom, "requests", fake):
            wecom.push(_wc(), self._ctx(), [], [], matched=5, total=5)
        self.assertEqual(fake.sent_types(), ["markdown"])

    def test_mention_all_off(self):
        fake = FakeRequests()
        with mock.patch.object(wecom, "requests", fake):
            wecom.push(_wc(mention_all=False), self._ctx(), [_Ctx("SN1")], [])
        self.assertEqual(fake.sent_types(), ["markdown"])

    def test_file_uploaded_when_asked(self):
        fake = FakeRequests()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "差异.xlsx"
            f.write_bytes(b"PK\x03\x04data")
            with mock.patch.object(wecom, "requests", fake):
                out = wecom.push(_wc(), self._ctx(), [], [], report_path=f)
        self.assertIn("file", fake.sent_types())
        self.assertIn("已发报告附件", out)
        upload = [c for c in fake.calls if "upload_media" in c["url"]]
        self.assertEqual(len(upload), 1)
        self.assertIn("type=file", upload[0]["url"])

    def test_file_skipped_when_off(self):
        fake = FakeRequests()
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.xlsx"
            f.write_bytes(b"PK")
            with mock.patch.object(wecom, "requests", fake):
                wecom.push(_wc(send_file=False), self._ctx(), [], [], report_path=f)
        self.assertNotIn("file", fake.sent_types())
        self.assertFalse([c for c in fake.calls if "upload_media" in c["url"]])

    def test_oversize_file_rejected_before_uploading(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "big.xlsx"
            f.write_bytes(b"x" * (21 * 1024 * 1024))     # 真写 21MB，别 mock stat
            with self.assertRaises(wecom.WecomError) as ctx:
                wecom.upload_file(_wc(), f)
        self.assertIn("20MB", str(ctx.exception))


class TestErrors(unittest.TestCase):
    def test_bad_key_error_is_explained(self):
        fake = FakeRequests(payloads={"markdown": {"errcode": 93000, "errmsg": "invalid"}})
        with mock.patch.object(wecom, "requests", fake):
            with self.assertRaises(wecom.WecomError) as ctx:
                wecom.send_markdown(_wc(), "hi")
        self.assertIn("webhook 地址不对", str(ctx.exception))

    def test_rate_limit_error_is_explained(self):
        fake = FakeRequests(payloads={"markdown": {"errcode": 45009, "errmsg": "limit"}})
        with mock.patch.object(wecom, "requests", fake):
            with self.assertRaises(wecom.WecomError) as ctx:
                wecom.send_markdown(_wc(), "hi")
        self.assertIn("每分钟 20 条", str(ctx.exception))

    def test_network_error(self):
        # 只换 post，别把 requests 整个换掉 —— 否则连异常类都取不到了
        with mock.patch.object(wecom.requests, "post",
                               side_effect=wecom.requests.RequestException("boom")):
            with self.assertRaises(wecom.WecomError) as ctx:
                wecom.send_markdown(_wc(), "hi")
        self.assertIn("连不上企微", str(ctx.exception))

    def test_upload_without_media_id(self):
        fake = FakeRequests(payloads={"upload": {"errcode": 0}})
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.xlsx"
            f.write_bytes(b"PK")
            with mock.patch.object(wecom, "requests", fake):
                with self.assertRaises(wecom.WecomError) as ctx:
                    wecom.upload_file(_wc(), f)
        self.assertIn("media_id", str(ctx.exception))


class TestDescribe(unittest.TestCase):
    def test_never_leaks_the_key(self):
        d = wecom.describe_wecom(_wc())
        self.assertNotIn(KEY, json.dumps(d, ensure_ascii=False))
        self.assertTrue(d["has_webhook"])
        self.assertIn("…", d["webhook_key"])
        self.assertNotIn("webhook", d)

    def test_reports_problems(self):
        d = wecom.describe_wecom(_wc(webhook=""))
        self.assertFalse(d["ready"])
        self.assertIn("webhook 地址", d["problems"])


if __name__ == "__main__":
    unittest.main()
