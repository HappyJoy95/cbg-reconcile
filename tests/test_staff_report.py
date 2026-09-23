"""保存人员设置 → **过 5 分钟自动上报**（2026-09-21 用户定的）。

⚠ **实现换过一次**：第一版是 `staff.py` 里一枚 `threading.Timer`（活在服务进程的内存里）。
用户随后要求「把 timer 的注册加上这种**单次注册**机制吧，记录日志，执行完删除注册」
⇒ 现在**登记一条一次性任务**（`timer.register_once`），由定时器心跳到点派发
`daily --steps report`。**所以这一份测的是"登记"，不是"计时器"** ——
注册表本身那套（落盘点删、到点派发、过期作废）在 `test_timer_once.py` 里。

用户原话（连着四句）：

> 「门店勾选完各店人员状态（门店发来的）后点击保存，然后就调用发送邮件，这个设置了吗」
> → 答「没有」→ 问收件人/时机 →「**两者**。**保存即发**。连点几次保存**合并成一封**。
>   **30s** 的窗口够了。**完整上报包还是带着人员名单**」
> → 随后改口径：「**那还是 5 分钟吧**」（他自己一勾就是一排人，30 秒点不完）

这一份盯四件事：

1. **合并**：窗口内连点 N 次保存 ⇒ **只登记一条**（用户要的就是这个，不是"每次发一封"）；
2. **到点真跑**：定时器派发 `daily --steps report` ⇒ **完整上报包**（人员表照旧在里面）；
3. **收件人两者**：区长 + 中台（去重，别让同一个地址出现两次）；
4. **旁边不许踩坏**：已经有一趟上报在跑就**别重复发**；发信失败/异常**绝不抛**、
   记一笔运行记录 —— **保存本身永远是成功的**（那是主产物）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import web                                                        # noqa: E402
from src.app import report as report_mod                                   # noqa: E402
from src.features.store import staff                                       # noqa: E402
from src.modules import timer                                              # noqa: E402
from src.storage import runlog                                             # noqa: E402

APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

#: 测试用的窗口 —— 真窗口 5 分钟，等不起；**逻辑一模一样**，只是短
SHORT = 0.25


def _quiet_now(root, minute=5):
    """挑一个"那一分钟**没有别的定时任务**到点"的时刻 —— 问 `timer.due()`，别靠猜。

    ⚠⚠ 为什么非要这样（2026-09-21 晚踩的，一天里有两个时段会红）：
      `tick()` **一次只派最早那一组 slot**。写死"真实时钟的 :05"时，
      ① 自动更新是**每小时 :17**（窗口 :17~:47）—— 落在这个窗口里它会抢先；
      ② 每天那几趟整点（**21:00** 那一串 dump/erp-dump/pos/pools/attain/plan、
         09:00 同理）在 :00~:30 窗口内也算到点 —— **那一小时里跑必红**。
      两次都是"看着像刚改的代码坏了"，其实测试自己在跟时钟较劲。
    """
    base = datetime.datetime.now()
    for h in range(24):
        cand = base.replace(hour=h, minute=minute, second=0, microsecond=0)
        if not timer.due(root, now=cand):
            return cand
    raise AssertionError("一天 24 小时每一刻都有到点任务？那是定时器表坏了")


def _root_with_db():
    """带最小库的临时安装目录（`runlog` 要有地方记，否则它是**静默 no-op**）。"""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "out").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
    runlog.ensure(conn)
    conn.commit()
    conn.close()
    return tmp, root


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp, self.root = _root_with_db()
        self.addCleanup(self.tmp.cleanup)


class Test登记成一封(_Base):
    """⭐ 用户那句「连点几次保存**合并成一封**」+「**那还是 5 分钟吧**」。"""

    def test_连点三次_只有一条登记(self):
        for _ in range(3):
            staff.schedule_report(self.root, note="保存人员设置")
        rows = timer.once_list(self.root)
        self.assertEqual(len(rows), 1, "连点几次排了 %d 条" % len(rows))
        self.assertEqual(rows[0]["cmd"], staff.REPORT_STEP)
        self.assertEqual(rows[0]["key"], staff.REPORT_ONCE_KEY)

    def test_每次点都把时间往后挪(self):
        staff.schedule_report(self.root, delay=60)
        first = timer.once_list(self.root)[0]["at"]
        staff.schedule_report(self.root, delay=600)
        row = timer.once_list(self.root)[0]
        self.assertEqual(len(timer.once_list(self.root)), 1)
        self.assertNotEqual(row["at"], first, "第二次点没把时间往后挪")
        self.assertLessEqual(timer.once_seconds_left(self.root, staff.REPORT_ONCE_KEY), 600)

    def test_默认窗口是5分钟(self):
        """⚠ 用户先定 30 秒，当天又改成 **5 分钟**（「那还是 5 分钟吧」）——
        以 5 分钟为准，别自己改回去（他勾的是一排人，窗口短了会发好几封）。"""
        self.assertEqual(staff.REPORT_MERGE_SECONDS, 5 * 60)
        r = staff.schedule_report(self.root)
        self.assertEqual(r["in"], 5 * 60)

    def test_登记要记日志(self):
        staff.schedule_report(self.root, note="保存人员设置", who="张三")
        rows = runlog.recent(self.root, kind=timer.ONCE_KIND, limit=3)
        self.assertTrue(rows, "登记没留痕")
        self.assertIn("保存人员设置", rows[0].get("note") or "")

    def test_落盘_换个进程也看得见(self):
        """⚠ 这就是换掉 `threading.Timer` 的原因：那个活在内存里，服务一重启就没了。"""
        staff.schedule_report(self.root, delay=300)
        raw = json.loads((self.root / timer.ONCE_REL).read_text(encoding="utf-8"))
        self.assertEqual(len(raw["once"]), 1)
        self.assertEqual(raw["once"][0]["note"], "保存人员设置")

    def test_report_in_和_取消(self):
        staff.schedule_report(self.root, delay=300)
        self.assertGreater(staff.report_in(self.root), 290)
        self.assertTrue(staff.cancel_report(self.root))
        self.assertEqual(staff.report_in(self.root), 0)
        self.assertEqual(timer.once_list(self.root), [])


class Test到点由定时器派发(_Base):
    """到点之后**不是**我们自己去发信 —— 是定时器那一跳派发 `daily --steps report`。"""

    def test_到点派发的是上报那一步(self):
        # ⚠⚠ **必须把时刻钉死**（2026-09-21 顺手修的，跟 M22 无关）：
        #   `tick()` 的规矩是"一次只派**最早那一组** slot"，而自动更新是**每小时 :17**
        #   （窗口 :17~:47）。用真实的 `now()` 时，只要跑测试的那一刻落在这个窗口里，
        #   自动更新的 slot 就比"一秒前注册的那个"更早 ⇒ 它抢先被派发，
        #   `res["ran"]` 变成 `["autoupdate"]` —— **一天里约一半时间会红**，
        #   而且看着像"最新那次改动弄坏的"（实测就这么误判过一轮）。
        #   ⇒ 把分钟定在 :05（不在 :17~:47 窗口里），并**把同一个时刻传给 tick**。
        # ⚠⚠ 光钉**分钟**还不够（2026-09-21 晚 21:5x 又红了）：**小时**还是取的真实时钟，
        #   而"每天 21:00"那趟（dump/erp-dump/pos/pools/attain/plan）在 21:00~21:30
        #   窗口内照样算到点 ⇒ 它比"一秒前注册的那个"更早 ⇒ `res["ran"]` 变成那一整串。
        #   **21 点这一小时里跑必红**（09:00 那趟同理）。⇒ 挑一个真没别的任务的小时。
        now = _quiet_now(self.root)
        argv = []
        timer.register_once(self.root, staff.REPORT_STEP,
                            at=now - datetime.timedelta(seconds=1),
                            key=staff.REPORT_ONCE_KEY, note="保存人员设置")
        res = timer.tick(self.root, spawn=lambda a: argv.append(list(a)),
                         config="config/store-X.yaml", now=now)
        self.assertEqual(res["ran"], [staff.REPORT_STEP], res)
        self.assertEqual(argv[0][argv[0].index("--steps") + 1], staff.REPORT_STEP)
        self.assertEqual(timer.once_list(self.root), [], "派发完没删注册")

    def test_发信那一段是完整上报包(self):
        """⚠ 用户：「**完整上报包**还是带着人员名单」⇒ 跑的是 `app/report.run()`，
        **不是**另写一封"只带人员表"的窄邮件（那条链在 `run_daily` 的 `report` 步里）。"""
        from src import run_daily
        self.assertIn(staff.REPORT_STEP, run_daily.STEPS)
        self.assertIn("staff", json.dumps(
            [t[0] for t in __import__("src.app.report", fromlist=["x"]).TABLES],
            ensure_ascii=False), "上报包里没有人员表了？")


class Test收件人两者(unittest.TestCase):
    """用户：「**两者**」—— 区长和中台**都收**（不是"区长没配才给中台"）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "stores.yaml").write_text(
            'stores:\n  - erp_name: "青岛城阳万象汇店"\n    region: 西北区\n',
            encoding="utf-8")

    def _managers(self, email):
        (self.root / "config" / "managers.yaml").write_text(
            "managers:\n  - name: 杨英梅\n    accounts: [\"SL15763940156\"]\n"
            "    region: 西北区\n    email: \"%s\"\n    regions: [西北区]\n" % email,
            encoding="utf-8")

    def test_区长和中台都收(self):
        from src import mailer
        self._managers("jiuzhang@example.com")
        to, why = report_mod._recipients(self.root, "青岛城阳万象汇店")
        self.assertIn("jiuzhang@example.com", to)
        self.assertIn(mailer.CENTRAL_ADDR, to)
        self.assertIn("中台", why)

    def test_区长没配邮箱时中台只出现一次(self):
        """⚠ 区长没邮箱时 `managers_of` 已经回落成中台了 —— 再无条件加一次
        就是**同一个地址两遍**（收件人重复会被某些邮箱判成垃圾邮件）。"""
        from src import mailer
        self._managers("")
        to, _why = report_mod._recipients(self.root, "青岛城阳万象汇店")
        self.assertEqual([x.lower() for x in to],
                         [x.lower() for x in to if x.lower() == mailer.CENTRAL_ADDR.lower()])
        self.assertEqual(len(to), 1, to)


class _Server:
    """真起一个 HTTP 服务 —— 测的是**保存那一下接口回了什么**。"""

    def __init__(self, root: Path):
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛城阳万象汇店"\nstore_code: "SCN231409"\n',
            encoding="utf-8")
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw.decode("utf-8"))
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Test接口(_Base):
    """保存那一下接口回什么 —— 门店**要**触发，区长/平台**不**触发。"""

    def setUp(self):
        super().setUp()
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)
        p = mock.patch.object(web, "setup_state",
                              lambda app: {"ready": True, "need": "", "why": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_门店保存后说清还等几秒(self):
        """真跑一遍：门店保存 ⇒ **登记一条一次性任务** ⇒ 接口回"还有几秒"。"""
        st, d = self.srv.request("PUT", "/api/staff", {"excluded": ["a"]})
        self.assertEqual(st, 200, d)
        self.assertTrue(d.get("ok"), d)
        self.assertGreater(d.get("report_in"), 5 * 60 - 10)
        self.assertLessEqual(d.get("report_in"), 5 * 60)
        rows = timer.once_list(self.root)
        self.assertEqual([r["cmd"] for r in rows], [staff.REPORT_STEP], rows)
        self.assertEqual(rows[0]["key"], staff.REPORT_ONCE_KEY)

    def test_区长保存不触发上报(self):
        """⚠ 上报是"门店 → 区长/中台"这件事；区长机器上跑它只会把本机那份发去中台。"""
        prof = {"erp_name": "平台岗", "type": "platform", "platform": True,
                "show_all": True, "who": "杨英梅", "needs_linglong": True}
        with mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: dict(prof)), \
                mock.patch.object(web, "describe_store_credentials",
                                  lambda *a, **k: {"username": "SL15763940156"}), \
                mock.patch.object(staff, "schedule_report",
                                  return_value={"scheduled": True, "in": 300}) as m:
            (self.root / "config" / "managers.yaml").write_text(
                "managers:\n  - name: 杨英梅\n    accounts: [\"SL15763940156\"]\n"
                "    region: 西北区\n    email: \"a@b.c\"\n    regions: [西北区]\n",
                encoding="utf-8")
            st, d = self.srv.request("PUT", "/api/staff", {"excluded": []})
        # ⚠ C2 当天改过：人员设置**只有门店能改**（区长/平台只读门店发来的那份）
        #   ⇒ 他连保存都到不了，自然也不会触发上报。两道都要在：
        #   ① `_can_for["staff.write"]` 拦在读接口那行；② 这行 `ROLE_STORE` 兜底。
        self.assertEqual(st, 403, d)
        self.assertTrue(d.get("forbidden"))
        self.assertFalse(m.called, "区长那边不该触发上报")


class Test前端接线(unittest.TestCase):
    def test_保存之后要把等待说清楚(self):
        """⚠ 不写的话门店点完看不到东西，会以为没保存成功（这个项目最怕"看着没反应"）。"""
        self.assertIn("d.report_in", APP_JS)
        self.assertIn("后自动上报", APP_JS)
        self.assertIn("这期间再点保存也只会发一封", APP_JS)

    def test_等多久要写成人话_不是秒数(self):
        """⚠ 5 分钟 = 300 秒 —— 提示里直接印「300 秒后自动上报」的话，
        门店得自己拿计算器（第一版就是那样）。"""
        self.assertIn("function waitText(", APP_JS)
        self.assertIn("分钟", APP_JS)
        self.assertIn("waitText(secs)", APP_JS)


if __name__ == "__main__":
    unittest.main()
