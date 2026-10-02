"""**推送模块**（`src/modules/notify/`）—— 用户 2026-09-19 划的分工：

| 谁 | 管什么 |
|---|---|
| **业务模块** | **该不该推**（开关、"只有差异才发"、这次有没有内容…） |
| **本模块** | ① 收到「渠道编码 + 内容」→ **发出去**；② **记录各个推送渠道** |

所以这里钉三件事：

1. `send()` **绝不抛**，永远给一个能看的 `why` ——
   推送失败只是"没送出去"，主产物（分数 / 报告）照样算完了；
2. 渠道是**登记表**（`CHANNELS` + `_SENDERS` 一一对应）——
   加渠道只改这两处，业务侧不用动；
3. ⚠ **`should_send` 不许搬进来**：那是业务的判断。历史上两个渠道的
   `should_send` 签名就不一样（一个收 `has_diff`、一个收 `has_diff + ignore_when`），
   搬进来等于把"该不该推"变成全局规则 —— 那正是这次要拆掉的东西。
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import mailer, wecom
from src.modules import auth, fetch, notify, theme, timer
from src.storage import runlog

ROOT = Path(__file__).resolve().parents[1]


def _root_with_db():
    """带最小结构的临时安装目录（和 `test_health.py` 同一个套路）。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir()
    db = root / "out" / "cbg-2026.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    return tmp, root, db


def _mail_cfg(*a, **k):
    return mailer.MailConfig(enabled=True, host="smtp.example.com", port=465,
                             username="s@example.com", password="x",
                             recipients=["boss@example.com"], when="always")


def _wecom_cfg(*a, **k):
    # ⚠ webhook 必须能被 `extract_key` 认出（带 `?key=`）——
    #   `https://x/y` 的 ready=False ⇒ `load_wecom_paths` 会当成没配路径。
    return wecom.WecomConfig(
        enabled=True,
        webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=00000000-1111-2222-3333-444444444444")


class Test渠道登记表(unittest.TestCase):
    """「有哪些渠道」是**全局**的事 —— 集中在这儿才看得见全貌。"""

    def test_两个渠道都在表里(self):
        self.assertEqual(sorted(notify.CHANNELS), ["mail", "wecom"])
        for code, meta in notify.CHANNELS.items():
            self.assertTrue(meta["label"], code)
            self.assertTrue(meta["config"].startswith(".secrets/"), code)
            self.assertIn("how", meta)

    def test_登记表和发送表一一对应(self):
        """⚠ 只加一半的渠道 = 界面上列得出来、一点就"不认识的渠道编码"。

        两张表挨着放就是为了这个 —— 这条测试盯着它们别错位。
        """
        self.assertEqual(sorted(notify.CHANNELS), sorted(notify._SENDERS))

    def test_channels_带配没配和上次的结果(self):
        tmp, root, _db = _root_with_db()
        self.addCleanup(tmp.cleanup)
        runlog.record("notify:wecom", False, why="webhook 500", root=root)
        with mock.patch.object(mailer, "load_mail_config", _mail_cfg), \
                mock.patch.object(wecom, "load_wecom_config", _wecom_cfg):
            rows = {r["code"]: r for r in notify.channels({}, root=root)}
        self.assertTrue(rows["mail"]["enabled"])
        self.assertEqual(rows["mail"]["detail"], "boss@example.com")
        self.assertEqual(rows["wecom"]["last_fail_why"], "webhook 500")
        self.assertEqual(rows["wecom"]["fail_streak"], 1)

    def test_channels_配置读不出来也照样把渠道列出来(self):
        """⚠ 读配置炸了**不许**让整张表消失 —— 那正是"渠道不见了"最难查的时候。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(mailer, "load_mail_config",
                               side_effect=RuntimeError("mail.env 读不了")):
            rows = {r["code"]: r for r in notify.channels({}, root=Path(tmp.name))}
        self.assertIn("mail", rows)
        self.assertIn("mail.env 读不了", rows["mail"]["detail"])
        self.assertIn("wecom", rows)


class Test发送(unittest.TestCase):
    """⚠ 这些用例**必须自带 root**：`runlog.record(root=None)` 会写到**项目根**，
    也就是开发机那个真库 —— 实测跑三轮全量测试往 `out/cbg-2026.db` 里
    塞了 28 行 `notify:*`，健康面板于是报"notify:sms 连着失败 7 次"。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)          # 空目录 ⇒ 没有库 ⇒ 记不进去，也不写盘

    def test_企微把_ctx_lines_head_递下去(self):
        seen = {}

        def fake_push(wc, ctx, lines, head):
            seen.update(ctx=ctx, lines=lines, head=head)
            return "已发送"

        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pos", fake_push):
            r = notify.send("wecom", {"template": "pos", "ctx": {"门店": "青岛CBD万达店"},
                                      "lines": ["A", "B"], "head": "标题"},
                            cfg={}, root=self.root)
        self.assertEqual(r["state"], notify.SENT)
        self.assertTrue(r["ok"])
        self.assertEqual(r["why"], "已发送")
        self.assertEqual(seen["head"], "标题")
        self.assertEqual(seen["lines"], ["A", "B"])
        self.assertEqual(seen["ctx"]["门店"], "青岛CBD万达店")

    def test_邮件用调用方给的_subject_body_prefix(self):
        seen = {}

        def fake_send(mc, subject, body, attachments=(), prefix=None):
            seen.update(subject=subject, body=body, prefix=prefix,
                        to=list(mc.recipients))

        with mock.patch.object(mailer, "load_mail_config", _mail_cfg), \
                mock.patch.object(mailer, "send", fake_send):
            r = notify.send("mail", {"subject": "POS 合规 2026-09",
                                     "body": "正文", "prefix": "[门店] "},
                            cfg={}, root=self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(seen["subject"], "POS 合规 2026-09")
        self.assertEqual(seen["prefix"], "[门店] ")
        self.assertEqual(seen["to"], ["boss@example.com"])
        self.assertIn("boss@example.com", r["why"])

    def test_发送失败不抛_给得出原因(self):
        """⚠ 推送失败不许把主流程带崩 —— 分数/报告才是主产物。"""
        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pos",
                                  side_effect=RuntimeError("webhook 500")):
            r = notify.send("wecom", {"template": "pos", "head": "x"}, cfg={}, root=self.root)
        self.assertEqual(r["state"], notify.FAILED)
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["why"])
        self.assertIn("webhook 500", r["why"])

    def test_没说模板就失败_不许猜(self):
        """⚠⚠ 这条是真踩出来的：第一版按"有没有 lines"猜模板，
        `{"head": "x"}` 被猜成 `report`，**静默发了一条内容完全不对的消息**（真 webhook
        打回来 errcode=93000 才发现）。猜错了不报错、只推错 —— 默认值里最贵的一种。

        ⚠ 三条模板**故意不一样**（@不@人 / 附不附清单 / 措辞），统一了就是事故。
        """
        with mock.patch.object(wecom, "push") as wp, \
                mock.patch.object(wecom, "push_pos") as wpp, \
                mock.patch.object(wecom, "push_pools") as wpl:
            r = notify.send("wecom", {"head": "x", "lines": ["y"]},
                            cfg={}, root=self.root)
        self.assertFalse(r["ok"])
        self.assertIn("没说清用哪套模板", r["why"])
        self.assertIn("pos", r["why"])
        self.assertFalse(wp.called or wpp.called or wpl.called, "猜了模板还发出去了？")

    def test_report_模板发差异清单(self):
        seen = {}

        def fake_push(wc, ctx, missing, unshipped, matched=0, total=0, report_path=None):
            seen.update(missing=missing, unshipped=unshipped, matched=matched,
                        total=total, report_path=report_path)
            return "推了"

        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push", fake_push):
            r = notify.send("wecom", {"template": "report", "ctx": {"门店": "X"},
                                      "missing": ["SN1"], "reverse_unshipped": ["SN2"],
                                      "matched": 9, "total": 10,
                                      "report_path": "/tmp/r.xlsx"},
                            cfg={}, root=self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(seen["missing"], ["SN1"])
        self.assertEqual(seen["unshipped"], ["SN2"])
        self.assertEqual(seen["total"], 10)
        self.assertEqual(seen["report_path"], "/tmp/r.xlsx")

    def test_pools_模板带上清单附件(self):
        seen = {}

        def fake_pools(wc, ctx, lines, head, xlsx=None):
            seen.update(lines=lines, head=head, xlsx=xlsx)
            return "推了"

        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pools", fake_pools):
            r = notify.send("wecom", {"template": "pools", "ctx": {},
                                      "lines": ["a"], "head": "双平台",
                                      "xlsx": "/tmp/p.xlsx"},
                            cfg={}, root=self.root)
        self.assertTrue(r["ok"])
        self.assertEqual(seen["head"], "双平台")
        self.assertEqual(seen["xlsx"], "/tmp/p.xlsx")

    def test_不认识的渠道编码_说清可选哪些(self):
        with mock.patch.object(wecom, "push_pos") as wp, \
                mock.patch.object(mailer, "send") as ms:
            r = notify.send("sms", {"template": "pos", "head": "x"}, cfg={}, root=self.root)
        self.assertEqual(r["state"], notify.FAILED)
        self.assertIn("不认识的渠道编码", r["why"])
        self.assertIn("mail", r["why"])
        self.assertIn("wecom", r["why"])
        self.assertFalse(wp.called or ms.called, "不认识的编码还去发了？")

    def test_没配就是一次失败_不是异常(self):
        """刚装完什么都没配 —— 这条路上**不能抛**（不然每天那趟就断在这儿）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        r = notify.send("mail", {"subject": "x", "body": "y"},
                        cfg={}, root=Path(tmp.name))
        self.assertEqual(r["state"], notify.FAILED)
        self.assertTrue(r["why"], "失败了却一个字都没说")


class Test记一笔(unittest.TestCase):
    """**记录各个推送渠道** —— 这一半落在 `run_record`（kind = `notify:<编码>`）。"""

    def setUp(self):
        self.tmp, self.root, _db = _root_with_db()
        self.addCleanup(self.tmp.cleanup)

    def _send_ok(self, code="wecom"):
        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pos", lambda *a: "已发送"):
            return notify.send(code, {"template": "pos", "head": "x"}, cfg={}, root=self.root)

    def test_发一次记一笔(self):
        self._send_ok()
        rows = runlog.recent(self.root, kind="notify:wecom")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["note"], "已发送")

    def test_失败也记_原因写进库(self):
        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pos",
                                  side_effect=RuntimeError("webhook 500")):
            notify.send("wecom", {"template": "pos", "head": "x"}, cfg={}, root=self.root)
        row = runlog.recent(self.root, kind="notify:wecom")[0]
        self.assertEqual(row["ok"], 0)
        self.assertIn("webhook 500", row["why"])
        self.assertEqual(runlog.summary(self.root)["notify:wecom"]["fail_streak"], 1)

    def test_记不上也照样把结果返回(self):
        """库坏了 / 还没建库 —— 记账失败不许影响"发出去"这个结论。"""
        bad = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        with mock.patch.object(wecom, "load_wecom_config", _wecom_cfg), \
                mock.patch.object(wecom, "push_pos", lambda *a: "已发送"):
            r = notify.send("wecom", {"template": "pos", "head": "x"}, cfg={}, root=bad)
        self.assertTrue(r["ok"])
        self.assertFalse((bad / "out").exists(), "为了记一行日志去建库了？")

    def test_history_按渠道取(self):
        self._send_ok("wecom")
        with mock.patch.object(mailer, "load_mail_config", _mail_cfg), \
                mock.patch.object(mailer, "send", lambda *a, **k: None):
            notify.send("mail", {"subject": "s", "body": "b"},
                        cfg={}, root=self.root)
        self.assertEqual(len(notify.history("wecom", root=self.root)), 1)
        self.assertEqual(len(notify.history(root=self.root)), 2)
        self.assertEqual(notify.history("mail", root=self.root)[0]["kind"], "notify:mail")


class Test分工边界(unittest.TestCase):
    """⚠ 这条边界是**用户明确划的**，别顺手把业务的判断搬进来。"""

    def test_没有_should_send_这类东西(self):
        for name in ("should_send", "should", "state_of", "decide"):
            self.assertFalse(hasattr(notify, name),
                             "「该不该推」是业务的判断，不该长在推送模块上：%s" % name)

    def test_状态只有_发送本身_那两种(self):
        self.assertEqual((notify.SENT, notify.FAILED), ("sent", "failed"))
        for name in ("DISABLED", "SKIPPED"):
            self.assertFalse(hasattr(notify, name),
                             "「关了」和「没配」是业务的结论，不是发送的结局")

    def test_该不该推仍然写在业务侧(self):
        """源码级的分工钉子 —— 两边各归各，谁也别越界。"""
        app = (ROOT / "src" / "app" / "pos.py").read_text(encoding="utf-8")
        # ⚠ 只看代码：模块 docstring 里**讲**了这条分工（"不用再记 should_send 的签名"），
        #   那是说明不是实现 —— 整篇搜会把说明也判成越界。
        body = (ROOT / "src" / "modules" / "notify" / "__init__.py").read_text(
            encoding="utf-8").split('"""', 2)[2]
        self.assertIn("should_send", app, "业务侧不再判该不该推了？")
        self.assertIn("push.send(", app, "POS 没走推送模块发？")
        self.assertNotIn("should_send", body)
        self.assertIn("_SENDERS", body)
        # ⚠ 实现**不搬家**：真发出去的还是 integrations 那层的 mailer / wecom，
        #   这儿只是"按编码找到它"（延迟 import，算分那条路不需要它们）。
        self.assertIn("mailer.send(", body)
        self.assertIn("wecom.push_pos(", body)


class Test登录验证模块(unittest.TestCase):
    """`modules/auth` —— 账号信息 + 三套登录态的自检。

    ⚠ 自检**只看本地证据**（用户 2026-09-19 定：启动不做真抓）——
      这条有测试盯着（不许 `ping`）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".secrets").mkdir()

    def _write_session(self, name="cbg-SCN231409.json", cookies="a=1; b=2", csrf="xyz12345"):
        import json
        p = self.root / ".secrets" / name
        p.write_text(json.dumps({"cookies": cookies, "csrf": csrf}), encoding="utf-8")
        return p

    def test_会话路径按项目根解析(self):
        """⚠ 配置里写相对路径时按**项目根**，不是 cwd ——
        控制台可能是从别处起的进程，按 cwd 会各找各的。"""
        cfg = {"store_code": "SCN231409", "session": {"file": ".secrets/x.json"}}
        self.assertEqual(auth.session_path(cfg, self.root), self.root / ".secrets" / "x.json")
        self.assertEqual(auth.session_path({"store_code": "S1"}, self.root),
                         self.root / ".secrets" / "cbg-S1.json")
        self.assertEqual(auth.session_path({}, self.root),
                         self.root / ".secrets" / "cbg-default.json")
        abs_p = Path("/tmp/绝对路径.json")
        self.assertEqual(auth.session_path({"session": {"file": str(abs_p)}}, self.root), abs_p)

    def test_会话文件就是换店换文件(self):
        """换店 = 换一份会话（华为会话是按店存的）。"""
        a = auth.session_path({"store_code": "AAA"}, self.root)
        b = auth.session_path({"store_code": "BBB"}, self.root)
        self.assertNotEqual(a, b)
        self.assertIn("BBB", b.name)

    def test_没有会话就要人工登录一次(self):
        st = auth.state({"store_code": "SCN231409"}, self.root)
        self.assertTrue(st["need_login"])
        row = [i for i in st["items"] if i["key"] == "cbg-session"][0]
        self.assertIn("还没有会话文件", row["why"])
        self.assertIn("抓不到华为订单", row["need"], "缺了会怎样要和为什么缺分开写")

    def test_会话正常就不用登录(self):
        self._write_session()
        st = auth.state({"store_code": "SCN231409"}, self.root)
        self.assertFalse(st["need_login"])
        row = [i for i in st["items"] if i["key"] == "cbg-session"][0]
        self.assertTrue(row["ok"])
        self.assertGreaterEqual(row["age_hours"], 0)

    def test_会话文件是空的照样算没过(self):
        self._write_session(cookies="", csrf="")
        st = auth.state({"store_code": "SCN231409"}, self.root)
        self.assertTrue(st["need_login"])
        self.assertIn("cookie", st["why"])

    def test_自检绝不联网(self):
        """夜里那条自检要是真去 ping 一次华为，就等于每天多抓一次数。"""
        from src import cbg
        self._write_session()
        with mock.patch.object(cbg.CbgClient, "ping",
                               side_effect=AssertionError("自检不该联网")):
            auth.state({"store_code": "SCN231409"}, self.root)

    def test_账号信息一个密码都不回(self):
        st = auth.accounts({"store_code": "SCN231409",
                            "erp_store_name": "青岛CBD万达店"}, self.root)
        text = json.dumps(st, ensure_ascii=False)
        self.assertNotIn("password\"", text.replace("has_password", ""))
        self.assertNotIn("PASSWORD", text)
        self.assertEqual(st["store"]["code"], "SCN231409")
        self.assertIn("has_password", st["erp"])

    def test_云商两套账号任一可用就算通(self):
        """主账号空着、门店账号配好了 —— 销售明细走的就是门店账号那套。

        ⚠ 只看主账号的话，门店明明配好了自检却说"没有云商凭据"（开发机实测）。
        """
        from src import erp
        with mock.patch.object(erp, "describe_credentials",
                               lambda *a, **k: {"env_file": "x", "username": "",
                                                "has_password": False, "has_token": False,
                                                "builtin": False, "used_from": ""}), \
                mock.patch.object(erp, "describe_store_credentials",
                                  lambda *a, **k: {"username": "sl189", "who": "赵海培",
                                                   "has_password": True, "has_token": True,
                                                   "env_file": "y", "company": "00001937"}):
            st = auth.state({"store_code": "S"}, self.root)
        row = [i for i in st["items"] if i["key"] == "erp"][0]
        self.assertTrue(row["ok"])
        self.assertIn("门店账号", row["usable"])


class Test数据抓取模块(unittest.TestCase):
    """`modules/fetch` —— 数据源登记表 + 新鲜度（转发 `app/data_state`）。"""

    def test_每一路都说清用什么抓_落在哪(self):
        rows = fetch.sources()
        self.assertGreaterEqual(len(rows), 4)
        for s in rows:
            for key in ("key", "label", "grab", "where", "how"):
                self.assertTrue(s.get(key), "%s 少了 %s" % (s.get("key"), key))

    def test_登记表的_state_key_都是真的(self):
        """⚠ 登记表里写了个判据那边不认识的 key ⇒ 界面上这一路永远"没判过"。"""
        from src.app import data_state
        real = {s["key"] for s in data_state.SOURCES}
        for s in fetch.sources():
            if s.get("state_key"):
                self.assertIn(s["state_key"], real, s["key"])

    def test_每天该抓的那几路和步骤对得上(self):
        """`daily=True` 的那几路 ⇒ 每天都该更新，抓它们的步骤就在 `run_daily` 里。

        ⚠ 别把它写成"每一路都对应一个步骤" —— 云商那路是**搭在**第 1 步里抓的
          （同一次跑里连着抓），没有独立步骤。写成一一对应会假红。
        ⚠ 2026-09-21 加了第三路 **report-inbox（收信）** —— 用户：「收信也是 fetch 啊」。
          它在 `run_daily` 里是独立一步（"[7/8] 收取门店上报"）。
        """
        from src import run_daily
        self.assertEqual(fetch.daily_keys(), ("orders", "erp-sales", "report-inbox"))
        self.assertIn("dump", run_daily.STEPS)
        for s in fetch.sources():
            if s.get("daily"):
                self.assertTrue(s.get("grab"), s["key"])
                self.assertTrue((ROOT / "src" / s["grab"]).is_file()
                                or s["grab"].startswith("config/"),
                                "%s 说的抓取入口不存在：%s" % (s["key"], s["grab"]))

    def test_收信那一路也登记了(self):
        """⚠ 用户 2026-09-21：「**收信也是 fetch 啊**」——

        收信跟推送是**两个方向**（那边只发、这边只收），但性质一样：
        从**外面的系统**（邮箱）把数据拿进来 ⇒ 它是**登记表里的一行**，
        **不是第七个系统模块**。
        """
        s = fetch.get("report-inbox")
        self.assertTrue(s, "收信那一路没登记进 SOURCES")
        self.assertIn("in/report.db", s["where"])
        self.assertIn("report-inbox", fetch.daily_keys(), "收信是每天那条链里的一步")

    def test_sources_给的是副本(self):
        a = fetch.sources()
        a[0]["label"] = "被改了"
        self.assertNotEqual(fetch.sources()[0]["label"], "被改了")

    def test_没进判定的那几路不装ok(self):
        """没判过的写 `-`。**不能写 ok** —— 没看就说好是编的。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        rows = {r["key"]: r for r in fetch.table(Path(tmp.name))}
        self.assertEqual(rows["targets"]["state_label"], "-")
        self.assertEqual(rows["stores"]["state_label"], "-")

    def test_state_就是那份判据(self):
        """转发不许多一套逻辑 —— 判据只有 `app/data_state.py` 一份。"""
        from src.app import data_state
        with mock.patch.object(data_state, "data_state",
                               lambda *a, **k: {"sources": [], "worst": "ok", "mark": 1}):
            self.assertEqual(fetch.state()["mark"], 1)


class Test计时模块(unittest.TestCase):
    """`modules/timer` —— 唤醒计划（来自注册表）+ 定时/自启状态。"""

    def test_唤醒计划来自注册表(self):
        """⚠ 计划只有一份：勾选的步骤、界面菜单、`run_daily` 跑的，
        都从 `features/registry.py` 派生（历史上三处各写一份，出过
        "界面上关了 POS、任务里还在跑"）。"""
        from src.features import registry
        p = timer.plan()
        # ⚠ 2026-09-20：`picked` 不再是"勾选的"（那个设置取消了）——
        #   现在是"**注册到定时器的那几步**"，默认就是全部。
        # ⚠ `picked` 是**会被定时器叫醒**的那几步（`registry.wakes()`）——
        #   2026-09-21 晚起「上报数据」也有自己的时刻了（21:15），所以它在这份计划里。
        self.assertEqual(p["picked"], [s.cmd for s in registry.wakes()])
        self.assertEqual([s.cmd for s in p["steps"]], p["picked"])
        self.assertEqual(p["labels"], [registry.step_labels()[c] for c in p["picked"]])

    def test_已经不存在的步骤单独列出来(self):
        """老设置里留着一步现在已经没有的 ⇒ 不能静默当成跑过了。

        ⚠ 2026-09-20：`picked` 的来源从「勾选项」改成了「注册到定时器的步骤」
          （那个设置取消了），所以桩要打在 `timer.enabled_cmds` 上。
        """
        with mock.patch.object(timer, "enabled_cmds",
                               lambda root=None: ("dump", "早没这一步了")):
            p = timer.plan()
        self.assertEqual(p["picked"], ["dump", "早没这一步了"])
        self.assertEqual(p["unknown"], ["早没这一步了"])
        self.assertEqual([s.cmd for s in p["steps"]], ["dump"])

    def test_读不出就按全跑(self):
        with mock.patch.object(timer, "enabled_cmds",
                               side_effect=RuntimeError("记录坏了")):
            p = timer.plan()
        self.assertIn("记录坏了", p["why"])
        self.assertTrue(p["picked"])

    def test_必做的三步关不掉(self):
        """⚠⭐ 用户 2026-09-20：「**自动更新和数据抓取模块不允许关闭**。
        数据抓取能改时间」。

        ⚠ 后端**必须拒**，不能只靠前端把滑块画灰 —— 前端画灰只是"看起来不能点"，
          老缓存页面 / curl 照样能关，而关掉的后果（库永远是旧的）**看不出来**。
        """
        for cmd in ("dump", "erp-dump", "autoupdate"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(ValueError) as cm:
                    timer.set_enabled(".", cmd, False)
                self.assertIn("关不掉", str(cm.exception))
                self.assertIn("时间可以改", str(cm.exception), "得说清时间还能改")

    def test_下次运行按唤醒计划算(self):
        """⚠ 语义**改过**（2026-09-20）：原来是"按**系统计划任务的注册时间**推"，
        而那条任务现在只负责"把服务拉起来"（用户：「不用系统的计划任务」）——
        它的时间不决定任何事。现在按**各模块声明的唤醒时刻**算。

        ⚠ 老那个 `next_run(task: dict)` 已删：它和新这个**重名**，
          后者会把前者盖掉，而"盖掉"是静默的（`test_module_layout` 抓到的）。
        """
        got = timer.next_run(".")
        self.assertRegex(got["at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
        self.assertTrue(got["cmds"], "得说清下一次唤醒哪几步")
        self.assertEqual(got["at_text"].split(" ")[0] in ("今天", "明天", "后天", "1 天后"),
                         True)

    def test_没注册要说出来(self):
        from src import autostart, schedule
        with mock.patch.object(schedule, "status", lambda root: {"installed": False}), \
                mock.patch.object(autostart, "status", lambda root: {"registered": False}):
            st = timer.status(Path("/tmp/假装"))
        self.assertFalse(timer.installed(Path("/tmp/假装")))
        self.assertIn("还没注册定时任务 —— 每天不会自己跑", st["problems"])
        self.assertIn("没设开机自启 —— 重启后要手动启动服务", st["problems"])

    def test_上次跑没跑看_run_record(self):
        """⚠ 没记录 ≠ 从没跑过 —— 只能说"没记过"。"""
        self.assertEqual(timer.last_run(Path("/tmp/假装没有这个目录")), {})
        tmp, root, _db = _root_with_db()
        self.addCleanup(tmp.cleanup)
        runlog.record("daily", True, note="3/3 步成功", root=root)
        self.assertTrue(timer.last_run(root)["ok"])


class Test主题和壁纸模块(unittest.TestCase):
    """`modules/theme` —— 主题**值**只有一份（`web/theme.css`），这儿只读不定义。"""

    def _web(self, theme_css="", index_html="", themes=None):
        """themes = {文件名: 内容}，一主题一文件（2026-09-22）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "web").mkdir()
        (root / "web" / "theme.css").write_text(theme_css, encoding="utf-8")
        (root / "web" / "index.html").write_text(index_html, encoding="utf-8")
        td = root / "web" / "themes"
        td.mkdir()
        # 默认基线总是有一个
        files = {"default.css": ":root { --bg: #fff; }\n"}
        if themes:
            files.update(themes)
        for name, body in files.items():
            (td / name).write_text(body, encoding="utf-8")
        return root

    def test_注释里的主题示例不算数(self):
        """⚠ 注释里的主题是**贴给人看的样板**。
        不删注释就会被当成"已经做好的主题" —— 界面上能选、切了没反应。"""
        dark = ('/* body[data-theme="draft"] { --bg: #000; } */\n'
                'body[data-theme="dark"] { --bg: #0f172a; }\n')
        root = self._web(themes={"dark.css": dark})
        self.assertEqual(theme.names(root), ["default", "dark"])
        self.assertNotIn("draft", theme.names(root))

    def test_主题清单带中文名(self):
        rows = {t["name"]: t for t in theme.themes(ROOT)}
        self.assertEqual(rows["default"]["label"], "默认（浅色）")
        self.assertEqual(rows["default"]["kind"], "light")

    def test_theme_css_必须排在_style_css_前面(self):
        """⚠ 主题块靠**层叠顺序**压过组件规则；顺序反了深色主题会被盖回去。"""
        good = ('<link href="theme.css">'
                '<link href="themes/default.css">'
                '<link href="themes/dark.css">'
                '<link href="style.css">')
        bad = ('<link href="theme.css">'
               '<link href="style.css">'
               '<link href="themes/default.css">'
               '<link href="themes/dark.css">')
        root_good = self._web("", good, themes={
            "dark.css": 'body[data-theme="dark"] { --bg: #000; }'})
        self.assertTrue(theme.order_ok(root_good)[0])
        ok, why = theme.order_ok(self._web("", bad, themes={
            "dark.css": 'body[data-theme="dark"] { --bg: #000; }'}))
        self.assertFalse(ok)
        self.assertTrue("盖回去" in why or "style.css 后" in why, why)
        self.assertFalse(theme.order_ok(self._web("", ""))[0], "压根没引也该报")

    def test_主题文件没链进页面要报出来(self):
        """加了 themes/新主题.css 却忘了 link ⇒ 界面能选、切了没反应。"""
        html = ('<link href="theme.css">'
                '<link href="themes/default.css">'
                '<link href="style.css">')
        root = self._web("", html, themes={
            "dark.css": 'body[data-theme="dark"] { --bg: #000; }'})
        ok, why = theme.order_ok(root)
        self.assertFalse(ok)
        self.assertIn("dark.css", why)
        res = theme.check(root)
        self.assertFalse(res["ok"])

    def test_仓库里的前端是自洽的(self):
        """真文件上的那条 —— 顺序被人改反，这里立刻红。"""
        res = theme.check(ROOT)
        self.assertTrue(res["ok"], res["why"])
        self.assertTrue(all(f["exists"] for f in theme.files(ROOT)))

    def test_壁纸只认白名单格式(self):
        """`_static()` 是按文件原样发的 —— 丢个 .svg 进去等于把带脚本的文件挂上去。"""
        root = self._web("", '<link href="theme.css">'
                             '<link href="themes/default.css">'
                             '<link href="style.css">')
        d = root / "web" / "wallpaper"
        d.mkdir()
        (d / "店庆.png").write_bytes(b"x")
        (d / "坏.svg").write_text("<svg/>", encoding="utf-8")
        rows = {w["name"]: w for w in theme.wallpapers(root)}
        self.assertTrue(rows["店庆.png"]["ok"])
        self.assertFalse(rows["坏.svg"]["ok"])
        res = theme.check(root)
        self.assertFalse(res["ok"])
        self.assertIn("坏.svg", res["why"])

    def test_壁纸文件操作只认白名单和纯文件名(self):
        """POST/DELETE 走 save/delete —— 穿越名、怪后缀当场拒，不落盘。"""
        root = self._web("", '<link href="theme.css">'
                             '<link href="themes/default.css">'
                             '<link href="style.css">')
        for bad in ("a.svg", "noext", "../x.png", "sub/dir.png", "a.exe"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError):
                    theme.save_wallpaper(root, bad, b"\x89PNG")
                with self.assertRaises(ValueError):
                    theme.delete_wallpaper(root, bad)
        saved = theme.save_wallpaper(root, "店庆.PNG", b"\x89PNG\r\n")
        self.assertEqual(saved["name"], "店庆.PNG")
        p = root / "web" / "wallpaper" / "店庆.PNG"
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"\x89PNG\r\n")
        # 空内容 / 超大拒收
        with self.assertRaises(ValueError):
            theme.save_wallpaper(root, "empty.png", b"")
        with self.assertRaises(ValueError):
            theme.save_wallpaper(root, "big.png", b"x" * (theme.MAX_WALLPAPER + 1))
        # 删掉后列表里没有；.gitkeep 不当壁纸列
        (root / "web" / "wallpaper" / ".gitkeep").write_text("", encoding="utf-8")
        self.assertEqual([w["name"] for w in theme.wallpapers(root)], ["店庆.PNG"])
        self.assertTrue(theme.delete_wallpaper(root, "店庆.PNG")["ok"])
        self.assertEqual(theme.wallpapers(root), [])
        self.assertFalse(theme.delete_wallpaper(root, "店庆.PNG")["ok"],
                         "再删一次要说没有，别假装成功")

    def test_照片主题在清单里(self):
        """「自定义照片主题」= themes/photo.css，names() 自动跟上。"""
        rows = {t["name"]: t for t in theme.themes(ROOT)}
        self.assertIn("photo", rows)
        self.assertEqual(rows["photo"]["label"], "自定义照片主题")
        self.assertEqual(theme.PHOTO_THEME, "photo")

    def test_主题切换是前端的事_后端不掺和(self):
        """规范 7.2：切换 = `localStorage` + `body.dataset.theme`，不加后端接口。"""
        body = (ROOT / "src" / "modules" / "theme" / "__init__.py").read_text(
            encoding="utf-8").split('"""', 2)[2]
        self.assertNotIn("def api", body)
        self.assertNotIn("localStorage", body, "那是前端的事")
        self.assertNotIn("Registry", body)


class Test六个模块的规矩(unittest.TestCase):
    """用户 2026-09-19：「系统级的模块包括……（共六个）」+「互相也有调用」。"""

    def test_六个模块都在(self):
        from src import modules
        for name in modules.MODULES:
            self.assertTrue((ROOT / "src" / "modules" / name / "__init__.py").is_file(),
                            "模块少了：%s" % name)
        self.assertEqual(len(modules.MODULES), 6)

    def test_目录里不许有没登记的模块(self):
        """只加文件夹不加登记表 ⇒ 下一个会话不知道有这个模块。"""
        from src import modules
        got = sorted(p.name for p in (ROOT / "src" / "modules").iterdir()
                     if p.is_dir() and (p / "__init__.py").is_file()
                     and not p.name.startswith("__"))
        self.assertEqual(got, sorted(modules.MODULES))

    def _edges(self):
        """六个模块之间的 import 关系（**连函数里的延迟 import 也算** ——
        成环就是成环，藏在函数里也一样）。"""
        import ast
        from src import modules
        out = {}
        for name in modules.MODULES:
            pkg = ["src", "modules", name]
            seen = set()
            for py in sorted((ROOT / "src" / "modules" / name).rglob("*.py")):
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.ImportFrom):
                        continue
                    up = pkg[:len(pkg) - (node.level - 1)] if node.level else []
                    full = up + (node.module or "").split(".")
                    if len(full) >= 3 and full[1] == "modules" and full[2] != name:
                        seen.add(full[2])
            out[name] = sorted(seen)
        return out

    def test_模块之间不许成环(self):
        """用户 2026-09-19：「互相也有调用，哦不对，加一个系统健康状态模块」。

        ⚠ 可以互相调，但**不许成环** —— 成环的代价是"改一个牵动另一个"，
        而且初始化顺序变成玄学（谁先 import 谁就赢）。
        """
        edges = self._edges()
        state = {}

        def walk(n, path):
            if state.get(n) == 1:
                self.fail("模块成环：%s" % " → ".join(path + [n]))
            if state.get(n) == 2:
                return
            state[n] = 1
            for m in edges.get(n, []):
                walk(m, path + [n])
            state[n] = 2

        for n in edges:
            walk(n, [])

    def test_模块不许反向依赖入口层(self):
        """模块是"能力"，入口层（cli/web）反过来依赖它们 —— 不许倒过来。"""
        import ast
        from src import modules
        bad = []
        for name in modules.MODULES:
            for py in sorted((ROOT / "src" / "modules" / name).rglob("*.py")):
                tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        if (node.module or "") in ("cli", "web", "startup", "runner"):
                            bad.append("%s → %s" % (py.name, node.module))
        self.assertEqual(bad, [])

    def test_健康模块能看见其它模块(self):
        """启动自检要一次说清"登录/数据/计时/推送/界面"各什么状态 ——
        这五项正好是五个模块的出口（用户要的启动流程）。

        ⚠ **root 必须是临时目录**（2026-09-29 收银那轮，conftest 的
        "改已有文件"监视抓到的）：`check_schema` 默认 `apply=True`（门店升级
        自愈，生产设计没错），拿 `ROOT` 跑就会把**新迁移真写进开发机
        `out/cbg-2026.db`** —— 004 那次、005/006 这次都这么写进去的。
        空临时 root 下 `find_db` 找不到库 → 走 "missing" 警告分支，不写任何东西。"""
        from src.modules import health
        with tempfile.TemporaryDirectory() as d:
            boot = health.boot(Path(d))
        groups = {i["group"] for i in boot["items"]}
        for want in ("auth", "data", "timer", "notify", "theme"):
            self.assertIn(want, groups)
        self.assertIn("allow_start", boot)

    def test_登录和界面都不拦启动(self):
        """⚠ 用户 2026-09-19 的原话是「自检不过就**弹登录**」——
        不是"不许启动"。硬挡的后果是门店连"上报 bug"都点不了。

        （root 换临时目录的原因见上一条 —— 同一处，别改回 `ROOT`。）"""
        from src.modules import health
        with tempfile.TemporaryDirectory() as d:
            boot = health.boot(Path(d))
        for i in boot["items"]:
            if i["group"] in ("auth", "theme", "timer", "notify"):
                self.assertNotIn(i, boot["blocking"], "%s 不该拦启动" % i["group"])
        self.assertTrue(boot["allow_start"])


class Test启动流程接上了(unittest.TestCase):
    """用户 2026-09-19 批的启动流程，最后一公里：**自检结果要真能到界面**。

    > 先验证计时模块、主题模块和推送模块是否正常，正常后才能启动；
    > 启动后如果登录验证模块自检不过就弹登录；……系统模块都运行好之后再检查功能模块注册。

    ⚠ 每一步都**只到后端**就等于没做：门店看到的只有页面。
      所以这里钉的是"后端 → 接口 → 横幅"这条链，而不是"函数返回值对不对"。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)

    def test_接口放行给登录页(self):
        """进不去的时候，`/api/boot` 正是"为什么进不去"的答案 —— 不能要登录才给。"""
        from src import web
        self.assertIn("/api/boot", web.SETUP_ALLOW)

    def test_app_缓存自检结果(self):
        """⚠ 界面 30 秒轮询一次，`check_data` 每次都要开库数行 —— 缓存 15 秒。"""
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        with mock.patch.object(web.health, "boot", lambda *a, **k: {"items": [], "mark": 1}):
            first = app.boot_state(force=True)
            again = app.boot_state()
        self.assertEqual(first["mark"], 1)
        self.assertIs(again, first, "15 秒内应该给同一个结果，不是重算一份")

    def test_自检自己炸了也不许拦住控制台(self):
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        with mock.patch.object(web.health, "boot", side_effect=RuntimeError("炸了")):
            st = app.boot_state(force=True)
        self.assertTrue(st["allow_start"], "自检炸了 → 更要让人进得去")
        self.assertIn("炸了", st["why"])

    def test_概览里带着自检结果(self):
        from src import web
        app = web.App(self.root, "config/store-X.yaml")
        with mock.patch.object(web.health, "boot", lambda *a, **k: {"items": [], "boot_mark": 7}), \
                mock.patch.object(web.config_io, "load_raw", lambda p: {}):
            ov = app.overview()
        self.assertEqual(ov["boot"]["boot_mark"], 7, "前端读的是 overview.boot")

    def test_起服务时跑一次自检并记一笔(self):
        """用户：「**健康模块负责……记录日志**」—— 启动自检这条也要留痕。"""
        src = (ROOT / "src" / "web.py").read_text(encoding="utf-8")
        self.assertIn("boot_state(force=True)", src)
        self.assertIn('runlog.record("boot"', src)

    def test_前端横幅接上了(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="boot-broken"', html)
        self.assertIn("function renderBoot(", js)
        self.assertIn("renderBoot(o.boot)", js, "渲染函数写了但没被调 = 永远不显示")
        self.assertIn("boot.blocking", js, "挂横幅要看的是 blocking 那一级，别看成 ok")

    def test_只有硬门槛才挂红横幅(self):
        """⚠ 警告（没配邮箱）和待办（没登录）都是**常态** —— 一起挂上来，
        门店两天就对这个横幅免疫了，真出事时反而不看。"""
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        body = js.split("function renderBoot(", 1)[1].split("\n}", 1)[0]
        self.assertIn("blocking", body)
        self.assertNotIn("warnings", body)
        self.assertNotIn("todos", body)

    def test_启动那几行只用_ascii_能编出来的字符(self):
        """⚠ AGENTS.md 坑 2：后台服务是 pythonw 起的，stdout 按 locale 编码，
        编不出来的字符会抛 UnicodeEncodeError —— 而它在 serve_forever() 前面，
        一抛**服务就起不来**。所以启动自检打印的话也得先过这一关。

        用 `gbk` 编一遍是最接近门店那台机器的判断（中文 Windows 的
        `sys.stdout.encoding` 就是 gbk）—— 编不过去的字符这里立刻红。
        """
        from src.modules import health
        # 临时 root：别让 check_schema 的 apply=True 把迁移写进开发机真库
        # （原因见 `test_健康模块能看见其它模块` 的注释）
        with tempfile.TemporaryDirectory() as d:
            lines = health.boot_lines(health.boot(Path(d)))
        self.assertTrue(lines)
        for line in lines:
            line.encode("gbk")
            self.assertNotIn("⚠", line, "⚠ 编不进 GBK（U+26A0）")
        self.assertTrue(any("启动自检" in s for s in lines), "启动那几行得说人话")


class Test功能注册这项自检(unittest.TestCase):
    """用户 2026-09-19 的启动流程最后一句：

    > 系统模块都运行好之后**再检查我们的功能模块注册这些**

    ⚠ 这一项是**硬门槛**：撞车时 `run_daily` 派发拿到的是"先注册的那个"，
    点哪一页、跑哪一步都说不清 —— 必须在启动时当场报，不能等门店点到那一页。
    """

    def test_注册表没问题就是一条_ok(self):
        from src.features import registry
        from src.modules.health import checks
        row = checks.check_registry()[0]
        self.assertEqual(row["level"], "warning")
        self.assertEqual(row["state"], "ok")
        self.assertIn("个步骤已注册", row["why"])
        self.assertIn("dump", row["why"])
        self.assertEqual(registry.validate(), [])

    def test_撞车就是硬门槛(self):
        from src.features import registry
        from src.modules.health import checks
        with mock.patch.object(registry, "validate",
                               lambda: ["定时步骤重复：pos —— 两个功能抢同一个步骤名"]):
            row = checks.check_registry()[0]
        self.assertEqual(row["level"], "blocking")
        self.assertIn("两个功能抢同一个步骤名", row["why"])

    def test_注册表读不出来只算警告(self):
        """⚠ 那多半是"代码没更新完"，而"升级断在半路"那一项已经在拦了 ——
        为它再拦一次只会让门店看到两条一样的红。"""
        from src.modules.health import checks
        with mock.patch.dict("sys.modules", {"src.features.registry": None}):
            row = checks.check_registry()[0]
        self.assertNotEqual(row["level"], "blocking")

    def test_它在启动自检的第一段里(self):
        """静态的（代码 / 结构 / 注册表）排在最前面 —— 这三样不对，
        后面"数据新不新鲜"根本没有意义。

        （顺序是 `snapshot()` 里写死的，跟 root 无关；临时 root 只为
        别让 check_schema 把迁移写进开发机真库 —— 见上面同款注释。）"""
        from src.modules import health
        with tempfile.TemporaryDirectory() as d:
            groups = [i["group"] for i in health.boot(Path(d))["items"]]
        self.assertIn("features", groups)
        self.assertLess(groups.index("code"), groups.index("data"))


if __name__ == "__main__":
    unittest.main()


class Test收信能力(unittest.TestCase):
    """`modules/fetch/mail.py` —— 用户 2026-09-20：

    > 「数据抓取模块要加一个**获取指定邮件**的能力，**默认读取的是 439845914@qq.com**
    >   这个邮箱，**有配置 smtp 的话那就读取自己配置的邮箱**的指定邮件」

    ⚠ 只用标准库（`imaplib` + `email`），这一层不引第三方。
    ⚠ 放在**抓取**模块（外面的数据进来），跟推送（只发）分得很清楚。
    """

    def setUp(self):
        from src.modules.fetch import mail as fetch_mail
        from src import mailer
        self.fm, self.mailer = fetch_mail, mailer
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".secrets").mkdir(parents=True)

    def _env(self, text):
        (self.root / ".secrets" / "mail.env").write_text(text, encoding="utf-8")

    def test_平台岗没配自己的就用中台(self):
        self._env("MAIL_CENTRAL_PASSWORD=pw\n")
        got = self.fm.config({}, self.root, platform=True)
        self.assertEqual(got["user"], self.mailer.CENTRAL_ADDR)
        # ⚠ 收信的主机是**按域名猜**的（`imap.qq.com`），跟发信那台 `smtp.qq.com` 不是一回事
        self.assertEqual(got["host"], "imap.qq.com")
        self.assertEqual(got["source"], "central", "要标出来用的是中台那份")

    def test_门店和区长没配自己的也读中台邮箱(self):
        """⚠⚠ 2026-09-21 **改过**（真发那一下发现的）。

        原来（用户 2026-09-20）：「门店和区长**如果没配发件账号，那就不读取邮箱**，
        只有平台的会抓取中台邮箱」⇒ 这里断言 `platform=False` 时返回 `{}`。
        **但 M18/M19 的设计是「门店上报 → 邮件（抄送中台）→ 区长收信落库」** ——
        区长机器要是没配自己的收件账号就**一封都收不到**，
        "区长今晚看到"直接变成"永远看不到"，界面上只有一句"没配收信"。

        ⇒ 现在的规矩跟**发信**一致：**没配自己的 ⇒ 用中台那份**（IMAP 跟 SMTP 共用
          同一个授权码）。⚠ 中台邮箱里有**全区**的邮件 —— 只看自己辖区那几家
          靠 `role_scope()` 筛，跟"库里全有、按角色筛"是同一个道理。
        """
        self._env("MAIL_CENTRAL_PASSWORD=pw\n")
        got = self.fm.config({}, self.root, platform=False)
        self.assertEqual(got.get("user"), "439845914@qq.com",
                         "门店/区长没配自己的 ⇒ 读中台那份（不然收不到上报）")
        # ⚠ 但**发信**照旧：没配自己的门店，发邮件仍然默认用中台（用户点名要保住的）
        from src import mailer as _m
        self.assertTrue(_m.load_mail_config({}, self.root).ready,
                        "发信这条：门店没配自己的 ⇒ 默认用中台发（不能因为收信那条把它也关掉）")

    def test_配了自己的就用自己那份(self):
        """⚠ 这条是用户点名要的："**有配置 smtp 的话那就读取自己配置的邮箱**"。"""
        self._env("MAIL_CENTRAL_PASSWORD=pw\nMAIL_USERNAME=store@163.com\n"
                  "MAIL_PASSWORD=mine\n")
        cfg = {"mail": {"enabled": True, "host": "smtp.163.com",
                        "recipients": "a@x.com"}}
        got = self.fm.config(cfg, self.root)
        self.assertEqual(got["user"], "store@163.com")
        self.assertEqual(got["host"], "imap.163.com", "主机按域名猜")
        self.assertEqual(got["source"], "store")

    def test_两个都没有就说清没配(self):
        self.assertEqual(self.fm.config({}, self.root), {})
        self.assertTrue(self.mailer.imap_problems({}))

    def test_猜得出常见邮箱的_IMAP_主机(self):
        for addr, host in (("x@qq.com", "imap.qq.com"),
                           ("x@163.com", "imap.163.com"),
                           ("x@126.com", "imap.126.com")):
            self.assertEqual(self.mailer.imap_host_of(addr), host)

    def test_邮件解析_编码主题和正文(self):
        raw = (b"Subject: =?utf-8?B?5rWL6K+V6YKu5Lu2?=\r\n"
               b"From: =?utf-8?B?5p2o6Iux5qKF?= <a@qq.com>\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               + "正文内容".encode("utf-8"))
        d = self.fm.parse(raw)
        self.assertEqual(d["subject"], "测试邮件")
        self.assertIn("杨英梅", d["from"])
        self.assertIn("正文内容", d["body"])
        self.assertFalse(d["html"])

    def test_只有_HTML_时给原文_不假装解析过(self):
        raw = (b"Subject: t\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
               + "<p>hi</p>".encode("utf-8"))
        d = self.fm.parse(raw)
        self.assertIn("<p>hi</p>", d["body"])
        self.assertTrue(d["html"], "标出来这是 HTML —— 上层自己决定怎么处理")

    def test_没配好要抛_不是返回空列表(self):
        """⚠ 「抓不到」和「这段时间没有新邮件」是两件事 —— 返回空列表会让上层
        以为"没数据"（这个项目栽过好几次）。"""
        with self.assertRaises(RuntimeError) as cm:
            self.fm.recent({}, self.root)
        self.assertIn("收信没配好", str(cm.exception))


class Test收信的关键词是真筛(unittest.TestCase):
    """`fetch.mail.recent(subject_contains=…)` —— **本地筛**，不是服务端搜。

    ⚠⚠ 2026-09-20 实测（中台那个 QQ 邮箱，A/B 双向）钉出来的：

    | 搜索条件 | 返回 |
    |---|---|
    | `ALL` | 7 封（全部） |
    | `SUBJECT "库存盘点"`（真有） | **7 封** |
    | `SUBJECT "绝无此主题XYZZY"`（瞎编） | **7 封** |
    | `FROM "000000000"`（瞎编） | **7 封** |
    | `UNSEEN` / `SINCE 01-Jan-2030` | 0 封 ✓ 真筛 |

    ⇒ QQ 把 `SUBJECT`/`FROM` **整个忽略、还不报错**（跟云商那些假筛参数一个套路），
      中文关键词还会让 imaplib 直接 `UnicodeEncodeError`。
      **假筛比报错更坏**：调用方会拿到"全部邮件"当结果，还以为搜到了。
    ⇒ 修法是：关键词一律拉回本地比（`matches()`），服务端只留真管用的 `UNSEEN`。
      下面这组测试就是钉这条 —— 少一条都可能悄悄退回"服务端筛"。
    """

    def setUp(self):
        from src.modules.fetch import mail as fetch_mail
        self.fm = fetch_mail

    def _fake_box(self, store):
        class FakeBox:
            def __init__(self, *a, **kw):
                self.commands = []
                store["box"] = self

            def login(self, user, pw):
                store["login"] = (user, pw)
                return "OK", [b""]

            def select(self, name):
                return "OK", [b"2"]

            def search(self, charset, *crit):
                # ⚠ 记下来：**这就是"有没有把关键词送给服务端"的判据**
                self.commands.append((charset, crit))
                return "OK", [b"1 2"]

            def fetch(self, one, spec):
                raw = {"1": store["raw_a"], "2": store["raw_b"]}[one.decode()]
                return "OK", [(b"1 (RFC822", raw)]

            def logout(self):
                return "BYE", [b""]

        return FakeBox

    def _run(self, **kw):
        import imaplib
        from unittest import mock
        from src import mailer
        store = {
            "raw_a": ("Subject: =?utf-8?B?5bqT5a2Y55uY54K5?=\r\nFrom: a@qq.com\r\n"
                      "Content-Type: text/plain; charset=utf-8\r\n\r\n甲".encode("utf-8")),
            "raw_b": (b"Subject: POS report\r\nFrom: b@qq.com\r\n"
                      b"Content-Type: text/plain; charset=utf-8\r\n\r\nB"),
        }
        # raw_a 的主题是「库存盘点」（base64 编的），raw_b 是 ASCII 的「POS report」
        with mock.patch.object(self.fm, "config",
                               return_value={"host": "h", "port": 993, "user": "u",
                                             "password": "p"}), \
             mock.patch.object(mailer, "imap_problems", return_value=[]), \
             mock.patch.object(imaplib, "IMAP4_SSL", self._fake_box(store)):
            out = self.fm.recent({}, None, **kw)
        return store, out

    def test_关键词一个都不往服务端送(self):
        store, _out = self._run(limit=5, subject_contains="库存盘点")
        _charset, crit = store["box"].commands[0]
        self.assertEqual(list(crit), ["ALL"], "只有真管用的条件才送服务端")
        self.assertNotIn("SUBJECT", crit)
        self.assertNotIn("FROM", crit)

    def test_unseen_only_还是要送服务端(self):
        """⚠ `UNSEEN` 是**真筛**（实测 0 封）—— 别为了"统一"把它也搬到本地。"""
        store, _out = self._run(limit=5, unseen_only=True)
        _charset, crit = store["box"].commands[0]
        self.assertEqual(list(crit), ["UNSEEN"])

    def test_中文关键词能筛出来(self):
        _store, out = self._run(limit=5, subject_contains="库存盘点")
        self.assertEqual(len(out), 1)
        self.assertIn("库存盘点", out[0]["subject"])

    def test_瞎编关键词筛出零封(self):
        """⚠ 这条是关键：**假筛的表现就是"瞎编也返回全部"**。"""
        _store, out = self._run(limit=5, subject_contains="绝无此主题XYZZY")
        self.assertEqual(out, [])

    def test_发件人也能筛(self):
        _store, out = self._run(limit=5, sender_contains="b@qq.com")
        self.assertEqual([m["subject"] for m in out], ["POS report"])

    def test_limit_还是管用(self):
        _store, out = self._run(limit=1)
        self.assertEqual(len(out), 1, "不带关键词时不该多拉")

    def test_matches_大小写和空白(self):
        m = {"subject": "  [报量对账] POS Report ", "from": "A@QQ.com"}
        self.assertTrue(self.fm.matches(m, subject_contains="pos report"))
        self.assertTrue(self.fm.matches(m, sender_contains="a@qq.com"))
        self.assertTrue(self.fm.matches(m, subject_contains="  "), "空关键词 = 不筛")
        self.assertFalse(self.fm.matches(m, subject_contains="库存"))


class Test邮件附件要看得见(unittest.TestCase):
    """`fetch.mail.parse()` 要能拿出附件 —— 这个能力的用途就是"从邮件里取数据文件"。

    ⚠ 2026-09-20 实测踩到的：真发了一封盘点清单到中台，主题正文都解析得出来，
      **附件一个都看不见**（`parse()` 只 walk 正文）——
      收件人明明拿到了文件，程序却以为"这封没有附件"。
    """

    RAW = (
        b"Subject: =?utf-8?B?5bqT5a2Y55uY54K5?=\r\n"
        b"From: a@qq.com\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + "正文".encode("utf-8") + b"\r\n"
        b"--B\r\nContent-Type: application/vnd.openxmlformats-officedocument"
        b".spreadsheetml.sheet\r\n"
        b'Content-Disposition: attachment; filename="a.xlsx"\r\n'
        b"Content-Transfer-Encoding: base64\r\n\r\nUEsDBAoAAAAA\r\n"
        b"--B--\r\n"
    )

    def setUp(self):
        from src.modules.fetch import mail as fetch_mail
        self.fm = fetch_mail

    def test_附件要带文件名和字节(self):
        d = self.fm.parse(self.RAW)
        self.assertIn("正文", d["body"])
        self.assertEqual(len(d["attachments"]), 1)
        a = d["attachments"][0]
        self.assertEqual(a["filename"], "a.xlsx")
        self.assertEqual(a["size"], len(a["data"]))
        self.assertEqual(a["data"][:2], b"PK", "拿到的必须是**原始字节**（xlsx 头）")

    def test_没有附件就是空表(self):
        raw = (b"Subject: t\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nhi")
        self.assertEqual(self.fm.parse(raw)["attachments"], [])


class Test发出去的邮件带_Date_头(unittest.TestCase):
    """⚠ `smtplib.send_message()` **不会**替你补 `Date` —— 要自己加。

    少了它的后果是"**我们自己收信时 `date` 是空的**"（2026-09-20 实测），
    而邮件客户端显示的是服务器收到的时间 ⇒ **人看不出来**。这种缺字段最容易留着。
    """

    def test_有_Date_头(self):
        from src import mailer
        mc = mailer.MailConfig(host="h", port=465, username="a@qq.com",
                               recipients=["b@qq.com"])
        msg = mailer.build_message(mc, "主题", "正文")
        self.assertTrue(msg.get("Date"), "少了 Date 头")
