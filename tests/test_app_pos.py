"""POS **执行模块**（`src/app/pos.py`）—— 阶段 2 / B 项试点。

验收要求（`四项核心重构-开发目标.md` 阶段 2）：

* 2.1 执行模块**不依赖 CLI / HTTP**（结构那条在 `test_module_layout.py` 里钉）；
* 2.2 三个入口调**同一个**模块（CLI 那条由 `cmd_pos` 的薄封装保证）；
* 2.4 ⭐ **通知策略的四种结果各有断言** —— 无配置 / 主动关闭 / 发送失败 / 计算失败，
  而且**换掉真实发送端**，不是断言"顶层 mock 被调用了"。

⚠ 为什么这四种要分开：以前只有"发了 / 没发"两种，于是 2026-09-19 那条
「daily 每天算 POS 却从来不推」的缺口**在日志里和"今天没配邮箱"长得一模一样**。
"""

import unittest
from pathlib import Path
from unittest import mock

from src import config_io, mailer, wecom
from src.app import pos as app_pos
from src.app.pos import DISABLED, FAILED, SENT, SKIPPED, PosRun
from src.features.compliance.pos import pos_metric as pm


def _rows():
    """一条形状正确的月记录（`pos_metric.score_month` 的输出口径）。"""
    def one(rate, ap_rate):
        return {"den": 100.0, "num": rate, "rate": rate, "orders": 10, "cut_den": 0.0,
                "ap_den": 100.0, "ap_num": ap_rate, "ap_rate": ap_rate}
    return [{"month": "2026-08", "provisional": False,
             pm.BY_LABEL: one(50.0, 55.0), pm.BY_REMARK: one(40.0, 45.0)}]


class _NotifyCase(unittest.TestCase):
    """`notify()` 那一层的脚手架：配置能读出来、发送端是可观察的假货。"""

    def setUp(self):
        self.res = PosRun(ok=True, rows=_rows())
        self.sent = []

        def fake_push(wc, ctx, lines, head):
            self.sent.append({"wecom": head, "ctx": ctx, "lines": lines})
            return "已发送"

        def fake_send(mc, subject, body, attachments=(), prefix=None):
            self.sent.append({"subject": subject, "prefix": prefix, "body": body})

        self.patches = [
            mock.patch.object(config_io, "load_raw",
                              lambda p: {"erp_store_name": "青岛CBD万达店",
                                         "store_code": "SCN328987", "marker": "C"}),
            mock.patch.object(wecom, "load_wecom_config",
                              lambda c, r: wecom.WecomConfig(
                                  enabled=True,
                                  webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-1111-2222-3333-444444444444")),
            mock.patch.object(wecom, "load_wecom_paths",
                              lambda c, r=None: [("t", wecom.WecomConfig(
                                  enabled=True,
                                  webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-1111-2222-3333-444444444444"))]),
            mock.patch.object(wecom, "should_send", lambda *a, **k: (True, "")),
            mock.patch.object(wecom, "push_pos", fake_push),
            mock.patch.object(mailer, "load_mail_config",
                              lambda c, r: mailer.MailConfig(
                                  enabled=True, host="smtp.example.com", port=465,
                                  username="s@example.com", password="x",
                                  recipients=["boss@example.com"], when="always")),
            mock.patch.object(mailer, "load_mail_paths",
                              lambda c, r=None: [("t", mailer.MailConfig(
                                  enabled=True, host="smtp.example.com", port=465,
                                  username="s@example.com", password="x",
                                  recipients=["boss@example.com"], when="always"))]),
            mock.patch.object(mailer, "send", fake_send),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _notify(self, **kw):
        kw.setdefault("config_path", "config/store-X.yaml")
        return app_pos.notify(self.res, root=Path("/tmp/假装的项目根"), **kw)


class Test通知的四种结局(_NotifyCase):
    def test_没配置记_skipped(self):
        """⚠ "没配"和"关了"是两件事 —— 混在一起就分不清"忘了配"和"故意不发"。"""
        self.res.notices = {}
        app_pos.notify(self.res, config_path=None)
        self.assertEqual(self.res.state("wecom"), SKIPPED)
        self.assertEqual(self.res.state("mail"), SKIPPED)
        self.assertIn("没有配置", self.res.why_of("mail"))
        self.assertEqual(self.sent, [], "没配置还发出去了？")

    def test_配置读不出来也记_skipped_而且不崩(self):
        with mock.patch.object(config_io, "load_raw", lambda p: {}):
            self._notify()
        self.assertEqual(self.res.state("mail"), SKIPPED)
        self.assertIn("配置", self.res.why_of("wecom"))

    def test_主动关闭记_disabled(self):
        self._notify(no_push=True, no_mail=True)
        self.assertEqual(self.res.state("wecom"), DISABLED)
        self.assertEqual(self.res.state("mail"), DISABLED)
        self.assertEqual(self.sent, [])

    def test_只关一个时另一个照发(self):
        """两个开关是各管各的 —— `--no-mail` 不该把企微也一起关了。"""
        self._notify(no_mail=True)
        self.assertEqual(self.res.state("wecom"), SENT)
        self.assertEqual(self.res.state("mail"), DISABLED)
        self.assertEqual(len(self.sent), 1)

    def test_发送失败记_failed_但分数还算数(self):
        """⚠ 推送失败**不许**把 `ok` 打下去 —— 分数才是主产物。"""
        with mock.patch.object(wecom, "push_pos",
                               side_effect=RuntimeError("webhook 500")):
            self._notify()
        self.assertEqual(self.res.state("wecom"), FAILED)
        self.assertIn("webhook 500", self.res.why_of("wecom"))
        self.assertTrue(self.res.ok, "推送失败把结果判成失败了 —— 退出码会跟着错")
        self.assertEqual(self.res.state("mail"), SENT, "企微挂了不该连累邮件")

    def test_真发出去了才记_sent(self):
        self._notify()
        self.assertEqual(self.res.state("wecom"), SENT)
        self.assertEqual(self.res.state("mail"), SENT)
        self.assertEqual([s["prefix"] for s in self.sent if "subject" in s],
                         [mailer.POS_SUBJECT_PREFIX],
                         "POS 那封的主题前缀必须是它自己的 —— 不然门店以为发重了")

    def test_ctx_里带上了门店和标识(self):
        self._notify()
        ctx = [s for s in self.sent if "ctx" in s][0]["ctx"]
        self.assertEqual(ctx["门店"], "青岛CBD万达店")
        self.assertEqual(ctx["串号标识"], "C")


class Test计算失败(_NotifyCase):
    def test_找不到库时_ok_是_False_且带原因(self):
        """⚠ **不抛异常** —— 三个入口都要能自己决定"这算失败还是跳过得说一句"。"""
        res = app_pos.run(db="/绝对没有这个库.db", root=self._tmp_root())
        self.assertFalse(res.ok)
        self.assertIn("没找到订单库", res.why)
        self.assertEqual(res.notices, {}, "算都没算成，不该有推送记录")

    def test_算都没算成时不推(self):
        res = app_pos.run(db="", root=self._tmp_root())
        self.assertFalse(res.ok)
        self.assertEqual(self.sent, [])

    def test_第一次跑时_build_不会因为没库而崩(self):
        """`out/` 里一个库都没有（刚装完）⇒ 返回 ok=False，不是 traceback。"""
        root = self._tmp_root()
        (root / "out").mkdir(parents=True, exist_ok=True)
        res = app_pos.run(db="", root=root)
        self.assertFalse(res.ok)
        self.assertIn("没找到订单库", res.why)

    def _tmp_root(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)


class Test算完就落盘(_NotifyCase):
    def _make_db(self, root):
        """造一个**最小但真能读**的库：`pos_report.load` 要的那两张表在里面。

        ⚠ 用真库而不是 mock —— 这条要验的是"算 + 落盘"这一整段，
        mock 掉就读不出"落盘路径对不对"了。
        """
        import sqlite3
        (root / "out").mkdir(parents=True, exist_ok=True)
        db = root / "out" / "cbg-2026.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, date TEXT, sn TEXT,
                                 type TEXT, remark TEXT, shop TEXT);
            CREATE TABLE returns (id INTEGER PRIMARY KEY, date TEXT, sn TEXT,
                                  type TEXT, remark TEXT, shop TEXT);
        """)
        conn.commit()
        conn.close()
        return db

    def test_落盘和结果契约对得上(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db = self._make_db(root)
        res = app_pos.run(db=str(db), root=root)         # 不给配置 ⇒ 推送到此为止
        self.assertTrue(res.ok, res.why)
        self.assertEqual(res.db, "out/cbg-2026.db", "落盘里的库路径要相对项目根")
        out = Path(res.out_path)
        self.assertTrue(out.is_file(), "JSON 没落盘")
        self.assertTrue(str(out).startswith(str(root)), "落到项目根外面去了")
        self.assertEqual(res.state("wecom"), SKIPPED)
