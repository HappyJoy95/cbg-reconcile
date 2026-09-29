# -*- coding: utf-8 -*-
"""`src/pmall.py` —— pmall 导出手动更新链路（会话 A 案 + 四端点 + 解析）。

开发目标见 `.dsh/docs/2026-09-29-生活馆利润核算-手动更新取数-开发目标.md`。
钉的都是**踩过或会踩**的：

* `taskTypeDisplay` 必须是真文案（空格 = `params error`，实测过）；
* GUEST 会话不许提交导出（换出来的匿名 csrf 打不动接口，实测过）；
* 下载必须过 `PK` 魔数（登录态失效回 HTML 错误页，会冒充 xlsx）；
* **活窗口优先、绝不弹窗**（A 案的全部意义）；弹窗成功后**不关窗口**。
"""

import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import pmall
from src.pmall import PmallError, PmallSession


def _jwt(payload: dict) -> str:
    def b64(obj):
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return "%s.%s.sig" % (b64({"alg": "HS256", "typ": "JWT"}), b64(payload))


UID_JWT = _jwt({"CSRF": "18660228618", "exp": 1790669999})
GUEST_JWT = _jwt({"CSRF": "GUEST", "exp": 1790669999})


def _uid_sess(source="window"):
    return PmallSession(cookies="WPSESSIONID=x; a=b", csrf=UID_JWT, source=source)


class _Resp:
    """requests 响应的最小替身。"""

    def __init__(self, status=200, body=None, content=None):
        self.status_code = status
        self._body = body
        self.content = (content if content is not None
                        else json.dumps(body or {}).encode("utf-8"))

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Proc:
    """launch() 返回的进程替身 —— poll 常活；关窗用例给非 None。"""

    def __init__(self, code=None):
        self._code = code
        self.pid = 4242
        self.returncode = code

    def poll(self):
        return self._code


class _RootCase(unittest.TestCase):
    """自带临时 root（⚠ 测试不许写项目根，conftest 连"改已有文件"都报）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


class Test身份(unittest.TestCase):
    def test_从csrf解出uid(self):
        self.assertEqual(pmall.uid_of(UID_JWT), "18660228618")

    def test_GUEST当没登录(self):
        """GUEST 是匿名 token —— 实测拿它打业务接口 401，必须当未登录。"""
        self.assertEqual(pmall.uid_of(GUEST_JWT), "")

    def test_垃圾csrf不抛(self):
        self.assertEqual(pmall.uid_of("not-a-jwt"), "")
        self.assertEqual(pmall.uid_of(""), "")
        self.assertEqual(pmall.identity("x.y.z"), {})


class Test请求头(unittest.TestCase):
    def test_业务头带上csrf和站点(self):
        h = _uid_sess().headers()
        self.assertEqual(h["x-pix-csrf-token"], UID_JWT)
        self.assertEqual(h["pmall-app-id"], pmall.SITE_ID)
        self.assertEqual(h["cookie"], "WPSESSIONID=x; a=b")


class Test状态文件(_RootCase):
    def test_窗口状态读写往返(self):
        self.assertTrue(pmall.save_window(self.root, {"port": 50648, "pid": 1}))
        self.assertEqual(pmall.load_window(self.root)["port"], 50648)

    def test_坏json当没有(self):
        p = self.root / ".secrets" / pmall.WINDOW_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ 坏文件", encoding="utf-8")
        self.assertEqual(pmall.load_window(self.root), {})

    def test_jar读写往返(self):
        pmall.save_jar(self.root, _uid_sess("jar"))
        jar = pmall.load_jar(self.root)
        self.assertIn("WPSESSIONID", jar["cookies"])
        self.assertEqual(jar["source"], "jar")
        self.assertIn("saved_at", jar)


class Test换csrf(unittest.TestCase):
    def test_200就取data(self):
        with mock.patch.object(pmall, "_post",
                               return_value=_Resp(body={"code": "200", "data": UID_JWT})):
            self.assertEqual(pmall.exchange_csrf("a=b"), UID_JWT)

    def test_非200回None(self):
        with mock.patch.object(pmall, "_post", return_value=_Resp(status=502)):
            self.assertIsNone(pmall.exchange_csrf("a=b"))

    def test_断网回None不抛(self):
        import requests as _rq
        with mock.patch.object(pmall, "_post", side_effect=_rq.ConnectionError("断了")):
            self.assertIsNone(pmall.exchange_csrf("a=b"))

    def test_没cookie直接None(self):
        with mock.patch.object(pmall, "_post") as post:
            self.assertIsNone(pmall.exchange_csrf(""))
            post.assert_not_called()


class Test收窗口会话(unittest.TestCase):
    def test_收不齐回None(self):
        with mock.patch.object(pmall._browser, "cookies_from_browser",
                               return_value=("", {})):
            self.assertIsNone(pmall.probe_port(1))

    def test_收齐就是一份会话(self):
        with mock.patch.object(pmall._browser, "cookies_from_browser",
                               return_value=("WPSESSIONID=x", {})), \
                mock.patch.object(pmall, "exchange_csrf", return_value=UID_JWT):
            sess = pmall.probe_port(1, source="window")
        self.assertEqual(sess.uid, "18660228618")
        self.assertEqual(sess.source, "window")


class Test会话三分支(_RootCase):
    """A 案：活窗 → jar → 弹窗。**有活窗时绝不弹窗**是这套的全部意义。"""

    def test_活窗口有uid就复用且不弹窗(self):
        with mock.patch.object(pmall, "load_window", return_value={"port": 5555}), \
                mock.patch.object(pmall, "_alive", return_value=True), \
                mock.patch.object(pmall, "probe_port", return_value=_uid_sess()), \
                mock.patch.object(pmall, "launch_login",
                                  side_effect=AssertionError("不该弹窗")), \
                mock.patch.object(pmall, "wait_login_in",
                                  side_effect=AssertionError("会话好好的，重登什么")):
            sess = pmall.ensure_session(self.root)
        self.assertEqual(sess.source, "window")
        self.assertEqual(sess.uid, "18660228618")
        # 复用顺手刷新 jar（下回没窗口时能兜底）
        self.assertIn("WPSESSIONID", pmall.load_jar(self.root)["cookies"])

    def test_活窗口过期就同窗重登_不新开(self):
        """窗口在、会话掉 —— 在**同一个窗口**里请人重登，别再开一个窗。"""
        with mock.patch.object(pmall, "load_window", return_value={"port": 5555}), \
                mock.patch.object(pmall, "_alive", return_value=True), \
                mock.patch.object(pmall, "probe_port", return_value=None), \
                mock.patch.object(pmall, "wait_login_in", return_value=_uid_sess()) as win, \
                mock.patch.object(pmall, "launch_login",
                                  side_effect=AssertionError("不许开第二个窗口")):
            sess = pmall.ensure_session(self.root)
        self.assertEqual(sess.source, "window")
        win.assert_called_once()

    def test_没窗口就退到jar(self):
        with mock.patch.object(pmall, "load_window", return_value={}), \
                mock.patch.object(pmall, "session_from_jar", return_value=_uid_sess("jar")), \
                mock.patch.object(pmall, "launch_login",
                                  side_effect=AssertionError("jar 活着就别弹窗")):
            sess = pmall.ensure_session(self.root)
        self.assertEqual(sess.source, "jar")

    def test_全都没有才弹窗(self):
        with mock.patch.object(pmall, "load_window", return_value={}), \
                mock.patch.object(pmall, "session_from_jar", return_value=None), \
                mock.patch.object(pmall, "launch_login", return_value=_uid_sess()) as pop:
            sess = pmall.ensure_session(self.root)
        self.assertEqual(sess.source, "window")
        pop.assert_called_once()

    def test_窗口登记坏了就当没窗口(self):
        """`port` 是垃圾字符串时别炸 —— 降级到 jar/弹窗那条路。"""
        with mock.patch.object(pmall, "load_window", return_value={"port": "不是数字"}), \
                mock.patch.object(pmall, "session_from_jar", return_value=_uid_sess("jar")), \
                mock.patch.object(pmall, "launch_login",
                                  side_effect=AssertionError("有 jar 就不该弹窗")):
            sess = pmall.ensure_session(self.root)
        self.assertEqual(sess.source, "jar")


class Test弹窗登录(_RootCase):
    def _launch_ok(self):
        return mock.patch.object(
            pmall._browser, "launch",
            return_value=(_Proc(None), 59999))

    def test_登录成功不关窗口并存jar(self):
        """⚠ 成功后**不许关窗口** —— 关了 session cookie 就没了，A 案第一步白做。"""
        with self._launch_ok(), \
                mock.patch.object(pmall, "_alive", return_value=True), \
                mock.patch.object(pmall, "probe_port", return_value=_uid_sess()), \
                mock.patch.object(pmall, "_sleep"), \
                mock.patch.object(pmall._browser, "_shutdown",
                                  side_effect=AssertionError("登录成功不许关窗口")):
            sess = pmall.launch_login(self.root, say=lambda _m: None)
        self.assertEqual(sess.uid, "18660228618")
        self.assertEqual(pmall.load_window(self.root)["port"], 59999)
        self.assertIn("WPSESSIONID", pmall.load_jar(self.root)["cookies"])

    def test_窗口被关掉要说人话(self):
        with mock.patch.object(pmall._browser, "launch",
                               return_value=(_Proc(1), 59999)):
            with self.assertRaises(PmallError) as cm:
                pmall.launch_login(self.root)
        self.assertIn("窗口被关掉", str(cm.exception))

    def test_超时也报清楚(self):
        with self._launch_ok(), \
                mock.patch.object(pmall, "_alive", return_value=True), \
                mock.patch.object(pmall, "probe_port", return_value=None), \
                mock.patch.object(pmall, "_sleep"):
            with self.assertRaises(PmallError) as cm:
                pmall.launch_login(self.root, timeout=0)
        self.assertIn("超时", str(cm.exception))

    def test_同窗重登会导航到登录入口(self):
        with mock.patch.object(pmall, "_alive", return_value=True), \
                mock.patch.object(pmall, "probe_port", return_value=None), \
                mock.patch.object(pmall, "_sleep"), \
                mock.patch.object(pmall._browser, "goto_url") as go:
            with self.assertRaises(PmallError):
                pmall.wait_login_in(5555, root=self.root, timeout=0)
        go.assert_called_once_with(5555, pmall.PMALL_URL)


class Test提交导出(_RootCase):
    def test_body带真文案和身份uid(self):
        """`taskTypeDisplay` 空格 = params error（实测踩过）；uid 取自 csrf 身份。"""
        seen = {}

        def rec(url, **kw):
            seen["url"] = url
            seen["body"] = json.loads(kw["data"].decode("utf-8"))
            return _Resp(body={"code": "200"})

        with mock.patch.object(pmall, "_post", side_effect=rec):
            pmall.export_policy(_uid_sess())
        body = seen["body"]
        self.assertEqual(body["taskTypeDisplay"], "价格返利政策商品导出")
        self.assertNotIn("          ", body["taskTypeDisplay"], "空格文案又混进来了")
        self.assertEqual(body["taskType"], pmall.TASK_TYPE)
        self.assertEqual(body["userId"], "18660228618")
        self.assertEqual(body["extendParams"]["uid"], "18660228618")
        self.assertIn("mp.asyncTask.export", seen["url"])

    def test_GUEST会话拒绝提交(self):
        sess = PmallSession(cookies="a=b", csrf=GUEST_JWT, source="window")
        with mock.patch.object(pmall, "_post") as post:
            with self.assertRaises(PmallError) as cm:
                pmall.export_policy(sess)
        post.assert_not_called()
        self.assertIn("GUEST", str(cm.exception))

    def test_业务拒绝要报原文(self):
        with mock.patch.object(pmall, "_post",
                               return_value=_Resp(body={"code": "0122001001",
                                                        "message": "params error"})):
            with self.assertRaises(PmallError) as cm:
                pmall.export_policy(_uid_sess())
        self.assertIn("params error", str(cm.exception))

    def test_http非200要报(self):
        with mock.patch.object(pmall, "_post", return_value=_Resp(status=500)):
            with self.assertRaises(PmallError) as cm:
                pmall.export_policy(_uid_sess())
        self.assertIn("HTTP 500", str(cm.exception))


class Test任务列表(unittest.TestCase):
    def test_取dataList(self):
        rows = [{"id": "T1", "status": "success"}]
        with mock.patch.object(pmall, "_get",
                               return_value=_Resp(body={"pageArgs": {}, "dataList": rows})):
            got = pmall.list_exports(_uid_sess())
        self.assertEqual(got, rows)

    def test_缺dataList就报(self):
        """响应回错误信封时别把 None 当空列表 —— 那会让轮询永远等下去。"""
        with mock.patch.object(pmall, "_get", return_value=_Resp(body={"code": "401"})):
            with self.assertRaises(PmallError):
                pmall.list_exports(_uid_sess())


class Test轮询(unittest.TestCase):
    REC_OK = {"id": "T2", "status": "success", "taskType": pmall.TASK_TYPE,
              "fileName": "sale_goods.xlsx", "createdDate": "2026-09-29 13:23:10.000"}

    def test_新任务成功就返回(self):
        with mock.patch.object(pmall, "list_exports",
                               side_effect=[[], [], [dict(self.REC_OK)]]), \
                mock.patch.object(pmall, "_sleep"):
            got = pmall.wait_new_export(_uid_sess(), known={"T1"})
        self.assertEqual(got["id"], "T2")

    def test_旧任务不算新(self):
        """known 里有的任务不许当新 —— 否则每次更新都拿上次的文件。"""
        with mock.patch.object(pmall, "list_exports",
                               return_value=[dict(self.REC_OK)]), \
                mock.patch.object(pmall, "_sleep"):
            with self.assertRaises(PmallError) as cm:
                pmall.wait_new_export(_uid_sess(), known={"T2"}, timeout=0)
        self.assertIn("超时", str(cm.exception))

    def test_失败任务当场报错(self):
        bad = dict(self.REC_OK, status="failed")
        with mock.patch.object(pmall, "list_exports", return_value=[bad]):
            with self.assertRaises(PmallError) as cm:
                pmall.wait_new_export(_uid_sess(), known=set(), timeout=0)
        self.assertIn("失败", str(cm.exception))

    def test_超时文案指路页面(self):
        with mock.patch.object(pmall, "list_exports", return_value=[]), \
                mock.patch.object(pmall, "_sleep"):
            with self.assertRaises(PmallError) as cm:
                pmall.wait_new_export(_uid_sess(), known=set(), timeout=0)
        self.assertIn("导出查询", str(cm.exception))


class Test下载(unittest.TestCase):
    def test_PK头才收(self):
        with mock.patch.object(pmall, "_get",
                               return_value=_Resp(content=b"PK\x03\x04real-xlsx")):
            self.assertEqual(pmall.download(_uid_sess(), "T1"), b"PK\x03\x04real-xlsx")

    def test_错误页不许冒充xlsx(self):
        """登录态失效时 downloadFromEDM 回 HTML —— 不校验就会把错误页存成表。"""
        with mock.patch.object(pmall, "_get",
                               return_value=_Resp(content=b"<html>401</html>")):
            with self.assertRaises(PmallError) as cm:
                pmall.download(_uid_sess(), "T1")
        self.assertIn("不是 xlsx", str(cm.exception))

    def test_缺taskId报错(self):
        with mock.patch.object(pmall, "_get") as get:
            with self.assertRaises(PmallError):
                pmall.download(_uid_sess(), "  ")
            get.assert_not_called()


class Test一条龙(unittest.TestCase):
    def test_记旧_提交_等新_下载(self):
        order = []

        def fake_list(sess, **kw):
            order.append("list")
            return [{"id": "OLD"}]

        def fake_export(sess, **kw):
            order.append("export")
            return {"code": "200"}

        def fake_wait(sess, **kw):
            order.append("wait")
            self.assertEqual(kw.get("known"), {"OLD"}, "known 必须是提交前记下的旧任务")
            return {"id": "NEW", "fileName": "x.xlsx"}

        def fake_dl(sess, tid, **kw):
            order.append("download:" + tid)
            return b"PK\x03\x04"

        with mock.patch.object(pmall, "list_exports", side_effect=fake_list), \
                mock.patch.object(pmall, "export_policy", side_effect=fake_export), \
                mock.patch.object(pmall, "wait_new_export", side_effect=fake_wait), \
                mock.patch.object(pmall, "download", side_effect=fake_dl):
            data, rec = pmall.fetch_policy(_uid_sess())
        self.assertEqual(data, b"PK\x03\x04")
        self.assertEqual(rec["id"], "NEW")
        self.assertEqual(order, ["list", "export", "wait", "download:NEW"])


class Test解析(unittest.TestCase):
    HEADER = ("店铺名称", "商品名称", "商品编码", "颜色", "商品状态",
              "基准提货价*", "无条件单台返利金额", "有条件最高单台返利金额",
              "价格生效日期")

    def _xlsx(self, rows):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(list(self.HEADER))
        for r in rows:
            ws.append(list(r))
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        return buf.getvalue()

    def test_九列原样_星号表头保留(self):
        data = self._xlsx([("销售MSC", "华为风范双肩包 随行款 棕色", "51996188",
                            "棕色", "已上架", 399.0, 75.012, 11.97, "2026-09-28")])
        rows = pmall.parse_policy(data)
        self.assertEqual(len(rows), 1)
        self.assertIn("基准提货价*", rows[0], "表头的星号不许被'顺手'洗掉")
        self.assertEqual(rows[0]["商品编码"], "51996188")
        self.assertEqual(rows[0]["基准提货价*"], 399.0)
        self.assertEqual(len(rows[0]), 9)

    def test_空行跳过(self):
        data = self._xlsx([("a", "b", "c", "d", "e", 1, 2, 3, "x"),
                           (None, None, None, None, None, None, None, None, None),
                           ("A", "B", "C", "D", "E", 1, 2, 3, "y")])
        rows = pmall.parse_policy(data)
        self.assertEqual(len(rows), 2, "空行混进来会让行数对不上接口")

    def test_坏文件报错(self):
        with self.assertRaises(PmallError) as cm:
            pmall.parse_policy("这不是 xlsx".encode("utf-8"))
        self.assertIn("打不开", str(cm.exception))

    def test_空表报错(self):
        from openpyxl import Workbook
        buf = io.BytesIO()
        Workbook().save(buf)
        with self.assertRaises(PmallError) as cm:
            pmall.parse_policy(buf.getvalue())
        self.assertIn("表头", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
