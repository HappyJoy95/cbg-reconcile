"""POS 合规那条推送 —— **和报量排查是分开的两条**（用户 2026-09-16 定的）。

分开的理由是"一条消息只讲一件事"：报量排查是**今天有活要干**
（哪几台没报量、快去补），POS 是**月度成绩**。混在一条里，
前者那份紧迫感会被后者的表格冲掉，而后者也没人会去翻。

三条**故意不同**，测试逐条钉住（都是"顺手统一了就出事"的地方）：

1. **不 @人** —— 每天 @所有人 会被门店屏蔽，连带把真正要紧的报量排查也屏蔽掉；
2. **不受「只有差异才推」约束** —— POS 没有"差异"这个概念；
3. **正文格式只定义一次** —— 邮件和企微共用 `pos_report.notify_lines`。
"""

import json
import unittest
from pathlib import Path
from unittest import mock

from src import mailer, wecom
from src.features.compliance.pos import pos_report as pr

ROWS = [
    {"month": "2026-07", "provisional": False,
     "label": {"den": 236262.0, "num": 142444.0, "rate": 60.28, "orders": 120,
               "cut_den": 2698.0, "ap_den": 233564.0, "ap_num": 142444.0, "ap_rate": 60.93},
     "remark": {"den": 236262.0, "num": 141492.0, "rate": 59.90, "orders": 120,
                "cut_den": 2698.0, "ap_den": 233564.0, "ap_num": 142444.0, "ap_rate": 60.93}},
    {"month": "2026-08", "provisional": True,
     "label": {"den": 268960.0, "num": 120290.0, "rate": 44.73, "orders": 130,
               "cut_den": 1488.0, "ap_den": 229960.0, "ap_num": 120290.0, "ap_rate": 52.32},
     "remark": {"den": 268960.0, "num": 123480.0, "rate": 45.91, "orders": 130,
                "cut_den": 1488.0, "ap_den": 229960.0, "ap_num": 123480.0, "ap_rate": 53.94}},
]


def _ctx():
    return {"门店": "青岛新业广场店", "生成时间": "2026-09-16 21:30",
            "配置文件": "config/store-X.yaml"}


class TestNotifyLines(unittest.TestCase):
    """正文格式**只定义一次**，邮件和企微共用。"""

    def test_每个月两行_标题加分母(self):
        lines = pr.notify_lines(ROWS)
        self.assertEqual(len(lines), 4)
        self.assertIn("2026-07", lines[0])
        self.assertIn("分母 236,262.00", lines[1])

    def test_暂定要标出来(self):
        """⚠ 「退货在退货当月扣减」⇒ 上个月的分数还会被这个月的退货改。
        不标的话，两个月后有人拿旧报表对不上账，会以为是程序算错了。"""
        lines = pr.notify_lines(ROWS)
        self.assertIn("（暂定）", lines[2])
        self.assertNotIn("（暂定）", lines[0])

    def test_分母为零显示破折号而不是零(self):
        """建店当月只有国补/即时零售单，分母是 0 —— 那是**算不出来**，不是 0%。"""
        rows = [{"month": "2026-06", "provisional": False,
                 "label": {"den": 0.0, "num": 0.0, "rate": None, "orders": 0,
                           "cut_den": 0.0, "ap_den": 0.0, "ap_num": 0.0, "ap_rate": None},
                 "remark": {"den": 0.0, "num": 0.0, "rate": None, "orders": 0,
                            "cut_den": 0.0, "ap_den": 0.0, "ap_num": 0.0, "ap_rate": None}}]
        lines = pr.notify_lines(rows)
        self.assertIn("—", lines[0])
        self.assertNotIn("0.00%", lines[0])

    def test_月份太多只列最近几个(self):
        many = []
        for i in range(1, 13):
            r = json.loads(json.dumps(ROWS[0]))
            r["month"] = "2026-%02d" % i
            many.append(r)
        lines = pr.notify_lines(many, limit=3)
        self.assertIn("只列最近 3 个月", lines[0])
        self.assertIn("2026-12", "\n".join(lines))
        self.assertNotIn("2026-01", "\n".join(lines))

    def test_headline_是最新那个月(self):
        self.assertIn("2026-08", pr.headline(ROWS))
        self.assertIn("44.73%", pr.headline(ROWS))

    def test_空数据不崩(self):
        self.assertEqual(pr.notify_lines([]), [])
        self.assertIn("没有", pr.headline([]))


class TestWecomPosMessage(unittest.TestCase):
    def test_正文含月份和口径说明(self):
        md = wecom.build_pos_markdown(_ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertIn("POS 合规", md)
        self.assertIn("2026-08", md)
        self.assertIn("分母 = 非国补、非即时零售、非 Care+", md)

    def test_不超企微的字节上限(self):
        md = wecom.build_pos_markdown(_ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertLessEqual(len(md.encode("utf-8")), wecom.MARKDOWN_LIMIT)

    def test_月份行加粗_明细行不加(self):
        md = wecom.build_pos_markdown(_ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertIn("> **2026-07", md)
        self.assertIn("> 分母 236,262.00", md)


class TestPosPushDoesNotMention(unittest.TestCase):
    """⚠ POS **不 @人** —— 每天 @所有人 会被门店屏蔽，
    连带把真正要紧的报量排查一起屏蔽掉。"""

    def test_push_pos_只发一条_markdown(self):
        fake = _Fake()
        with mock.patch.object(wecom, "requests", fake):
            out = wecom.push_pos(_wc(), _ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertEqual(fake.sent_types(), ["markdown"], "POS 不该发 text（那是用来 @人的）")
        self.assertNotIn("@", json.dumps(fake.calls[0]["json"]))
        self.assertIn("已发", out)

    def test_配置里开了_at_all_也不_at(self):
        """⚠ `mention_all` 是给报量排查的开关，POS 不许受它影响。"""
        fake = _Fake()
        with mock.patch.object(wecom, "requests", fake):
            wecom.push_pos(_wc(mention_all=True), _ctx(),
                           pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertEqual(fake.sent_types(), ["markdown"])


class TestShouldSendIgnoresWhenForPos(unittest.TestCase):
    """⚠ 2026-09-22 设置改版：发送时机已去掉 —— `when` / `has_diff` 都不再看。"""

    def test_when_and_diff_ignored(self):
        wc = _wc(when="only_diff")
        ok, why = wecom.should_send(wc, has_diff=False)
        self.assertTrue(ok, "配了路径就推：" + why)
        ok2, _ = wecom.should_send(wc, has_diff=False, ignore_when=True)
        self.assertTrue(ok2, "签名里的 ignore_when 仍收")

    def test_没配路径_POS_也不发(self):
        ok, why = wecom.should_send(_wc(enabled=False), has_diff=False, ignore_when=True)
        self.assertFalse(ok)
        self.assertIn("没有企微推送路径", why)

    def test_配置不全_POS_也不发(self):
        ok, _ = wecom.should_send(_wc(webhook=""), has_diff=False, ignore_when=True)
        self.assertFalse(ok)


class TestPosMail(unittest.TestCase):
    def test_主题带自己的前缀(self):
        """⚠ 两条推送都顶着 `[报量对账]` 会让人以为发重了。"""
        subj, _ = mailer.build_pos_mail(_ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        self.assertIn("POS", mailer.POS_SUBJECT_PREFIX)
        self.assertIn("青岛新业广场店", subj)
        self.assertIn("44.73%", subj)
        self.assertNotIn("报量对账", mailer.POS_SUBJECT_PREFIX)

    def test_没有官方字段时不许标成官方(self):
        """⚠ 官方分数只有在 payload 里**真有** `official` 时才报。

        回落成"我们那个数"再标上「官方」= **撒谎**（判据和显示不一致，
        这个项目最忌讳的一类错）。老 fixture / 老 `pos-<年>.json` 走这条。
        """
        lines = pr.notify_lines(ROWS)
        self.assertNotIn("官方", "".join(lines))

    def test_有官方字段时两个口径都给(self):
        rows = [dict(ROWS[1], official={"label": {"rate": 49.5, "rate_sum": 44.49},
                                        "remark": {"rate": 49.6, "rate_sum": 44.6}})]
        text = "".join(pr.notify_lines(rows))
        self.assertIn("官方 49.50%", text)
        self.assertIn("旧口径 44.73%", text)
        self.assertEqual(pr.official_rate(rows[0]), 49.5)

    def test_正文含口径和暂定说明(self):
        _, body = mailer.build_pos_mail(_ctx(), pr.notify_lines(ROWS), pr.headline(ROWS))
        # ⚠ 2026-09-21 起正文口径说明换成了**官方那套**（PPT）
        self.assertIn("先按天算再取日均值", body)
        self.assertIn("不含退货", body)
        self.assertIn("异常金额", body, "得说清这一项暂时没数据源")
        self.assertIn("退货", body)
        self.assertIn("暂定", body)

    def test_prefix_能覆盖而默认不变(self):
        """`prefix=None` 时必须还是走配置里那个 —— 报量排查那封**不能**受影响。"""
        mc = _mc()
        self.assertIn("[报量对账]", str(mailer.build_message(mc, "s", "b")["Subject"]))
        self.assertIn("[POS 合规]",
                      str(mailer.build_message(mc, "s", "b", prefix="[POS 合规]")["Subject"]))


# ------------------------------------------------------------------ 替身
# ⚠ 字段名照**真的**那两份 dataclass 来（`webhook` 不是 `key`、`sender` 不是
#   `from_addr`）—— `key` / `from_addr` 是**属性**，传进去会 TypeError。
#   第一版就是照着自己以为的名字写的，六条测试全挂在 TypeError 上。
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=" + "k" * 20


def _wc(**over):
    base = dict(enabled=True, webhook=WEBHOOK, when="always",
                mention_all=True, send_file=True)
    base.update(over)
    return wecom.WecomConfig(**base)


def _mc(**over):
    base = dict(enabled=True, host="smtp.test.com", port=465, security="ssl",
                username="u@test.com", password="pw", sender="",
                recipients=["a@x.com"], subject_prefix="[报量对账]", when="always")
    base.update(over)
    return mailer.MailConfig(**base)


class _Fake:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return _Resp()

    def sent_types(self):
        return [c["json"]["msgtype"] for c in self.calls if "json" in c]


class _Resp:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"errcode": 0, "errmsg": "ok"}


if __name__ == "__main__":
    unittest.main()
