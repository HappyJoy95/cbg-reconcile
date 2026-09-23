"""登录门禁 + **账号 / 门店 / 权限 / 内容** 的划分（M9）。

用户 2026-09-18：「我打算做个登录机制，门店鉴权。第一次安装会进入登录页面，
需要先登录云商再登录玲珑才可以进入正式页面。不登录或者登录失败不给用」。
随后补了关键一条：「云商登录成功之后看是哪个店，**如果是我们串号标识里有的
那十四家店需要登录玲珑，其余店不需要**。但是他们的控制台也不会显示玲珑相关的
内容。**这就是做的账号、门店权限与内容的划分**」。

⚠ 这个划分的判据是**名单里这家店有没有串号标识**（15 家 `kind=体验店` 里
只有 14 家有标识）—— 见 `config_io.store_profile`，全项目只有那一处判断。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config_io, erp, web                                 # noqa: E402
from src.features.store import staff as app_staff                              # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        # 名单要拷真的 —— 判据全靠它
        shutil.copy(ROOT / "config" / "stores.yaml", self.root / "config" / "stores.yaml")
        # ⚠ 门店账号文件是按**项目根**解析的（`erp.resolve_env_path`）——
        #   生产环境里那正是安装目录，所以对；但测试的临时根指不到它，
        #   不指过来的话会读到**开发机自己那份真凭据**，
        #   于是"没配也应该没过"这条会莫名其妙地过（实测踩到）。
        #   `web` 那边是 `from .erp import STORE_ENV_FILE` 按值拿的，两处都要指。
        store_env = str(self.root / ".secrets" / "erp-store.env")
        for mod in (erp, web):
            p = mock.patch.object(mod, "STORE_ENV_FILE", store_env)
            p.start()
            self.addCleanup(p.stop)
        self.app = web.App(self.root, "config/store-X.yaml")

    def cfg(self, name="", code="", marker=""):
        (self.root / "config" / "store-X.yaml").write_text(
            "erp_store_name: %s\nstore_code: %s\nmarker: %s\n" % (name, code, marker),
            encoding="utf-8")
        return self.app


class Test画像就是判据(_Base):
    def test_有串号标识的才要玲珑(self):
        """用户说的"那十四家" = 名单里**有串号标识**的。"""
        p = config_io.store_profile(
            {"erp_store_name": "青岛CBD万达店", "store_code": "SCN328987"}, self.root)
        self.assertEqual(p["marker"], "C")
        self.assertTrue(p["needs_linglong"])

    def test_合作店不要玲珑(self):
        p = config_io.store_profile(
            {"erp_store_name": "青岛永旺东部店", "store_code": "CNSCN162188"}, self.root)
        self.assertEqual(p["marker"], "")
        self.assertFalse(p["needs_linglong"])

    def test_名单说了算_配置里的旧标识不算数(self):
        """⚠ 2026-09-18 实测踩到（用户当场发现）：「但是这个标识不应该存在啊」。

        他从新业广场店（标识 Y）换登成麦凯乐店（合作店、名单里没标识），
        配置里那个 Y 没清掉 —— 于是**一家合作店被算成要玲珑**，永远进不去。
        名单是**随程序更新的唯一真源**，它说了算。
        """
        p = config_io.store_profile(
            {"erp_store_name": "青岛麦凯乐店", "store_code": "CNSCN258646",
             "marker": "Y"}, self.root)      # ← 旧值
        self.assertEqual(p["marker"], "", "旧标识又赢了")
        self.assertFalse(p["needs_linglong"])

    def test_名单里没这家店时用配置兜底(self):
        p = config_io.store_profile(
            {"erp_store_name": "云商里新开的一家店", "marker": "ZZ"}, self.root)
        self.assertEqual(p["marker"], "ZZ")
        self.assertFalse(p["in_roster"])

    def test_十四家要玲珑_其余不要(self):
        """把 29 家过一遍 —— 这条是给"划分"本身兜底的。"""
        import yaml
        roster = yaml.safe_load(
            (ROOT / "config" / "stores.yaml").read_text(encoding="utf-8"))["stores"]
        # ⚠ 名单里 2026-09-19 起多一行**虚拟平台岗门店**，它不是真店 ⇒ 排除
        real = [s for s in roster if s.get("kind") != config_io.PLATFORM_KIND]
        need = [s["erp_name"] for s in real
                if config_io.store_profile({"erp_store_name": s["erp_name"]},
                                           self.root)["needs_linglong"]]
        self.assertEqual(len(need), 14, "要玲珑的家数变了：%s" % need)
        self.assertEqual(len(real), 29)

    def test_虚拟平台岗门店在名单里(self):
        """⚠ 用户 2026-09-19：「平台岗就做个**虚拟的平台岗门店**就是了」。

        钉两件事：① 名单里真有这一行、`kind` 就是判据用的那个值；
        ② 它的画像是 `platform`（菜单全开、不要玲珑），但**店名是真的填着的** ——
        这正是它跟老标志 `platform: true` 的区别（老标志不写店名，
        于是"平台岗"和"某家店"两种身份能同时留在配置里）。
        """
        import yaml
        roster = {s["erp_name"]: s for s in yaml.safe_load(
            (ROOT / "config" / "stores.yaml").read_text(encoding="utf-8"))["stores"]}
        hit = roster.get(config_io.PLATFORM_STORE)
        self.assertIsNotNone(hit, "名单里没有虚拟平台岗门店")
        self.assertEqual(hit["kind"], config_io.PLATFORM_KIND)
        self.assertFalse((hit.get("huawei_code") or "").strip(), "虚拟店不该绑华为编码")

        prof = config_io.store_profile(
            {"erp_store_name": config_io.PLATFORM_STORE}, self.root)
        self.assertEqual(prof["type"], "platform")
        self.assertTrue(prof["platform"])
        self.assertFalse(prof["needs_linglong"])
        self.assertEqual(prof["erp_name"], config_io.PLATFORM_STORE,
                         "虚拟店也要把店名填上 —— 否则又回到'没有身份'的老样子")


class Test门禁(_Base):
    """`setup_state` —— 后端挡，不是前端画个遮罩。"""

    def test_没配就是没过(self):
        st = web.setup_state(self.cfg())
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "erp")

    def test_体验店配好了但没玲珑会话_还是没过(self):
        st = web.setup_state(self.cfg("青岛CBD万达店", "SCN328987", "C"))
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "linglong")
        self.assertTrue(st["erp"]["ok"])

    def test_合作店配好了就直接过(self):
        """⚠ 合作店**连 POS 合规都做不了**（那页读的是玲珑数据），
        所以绝不能要求它们登玲珑 —— 否则它们永远进不去。"""
        st = web.setup_state(self.cfg("青岛永旺东部店", "CNSCN162188"))
        self.assertTrue(st["ready"])
        self.assertEqual(st["need"], "")
        self.assertIn("不走玲珑", st["linglong"]["why"])

    def test_有token也算云商那步过了(self):
        """门店账号真登过（有 token）—— 即使门店配置还没写完。"""
        (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
        (self.root / ".secrets" / "erp-store.env").write_text(
            "ERP_USERNAME=u\nERP_PASSWORD=p\nERP_TOKEN=abc123\n", encoding="utf-8")
        st = web.setup_state(self.cfg())
        self.assertTrue(st["erp"]["ok"])
        self.assertTrue(st["erp"]["has_token"])


class Test退出登录要真的退(_Base):
    """⚠ 2026-09-19 用户报的：

        「重登账号/换账号，会回到登录页面，**不登录退出的话，再次打开还是能进入界面**」

    根因：`/api/store-account/logout` 原来**只删 `.secrets/erp-store.env`**，
    可门禁放行的判据里还有两条**兜底**（`web.setup_state`）：

      ① `platform: true` —— 平台岗**直接放行**；
      ② `erp_store_name` + `store_code` 都在 —— "门店配置已就绪"。

    ⇒ 凭据删了，这两条照样成立，**退出登录等于没退**。
    （开发机上真踩到：`config/store-SCN231409.yaml` 里留着
    `platform: true` + `erp_branch_id`，于是怎么点都能进主界面。）

    ⇒ 退出登录 = 把**身份**一起清掉。
    """

    def _logout(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            web.Handler.app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("POST", "/api/store-account/logout", body="{}",
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8"))

    def _cfg_full(self):
        """一份**已登录**的配置：门店身份 + 邮件 / 企微 / 时区都配好了。

        ⚠ 门店**从名单里现读一家合作店**，别写死 ——
          写成体验店的话门禁还要求玲珑会话（这一步是另一个判据），
          `ready` 本来就不是 True，测的就不是这条了。
          （第一版写死了「青岛CBD万达店」，就是这么红的。）
        """
        import yaml
        roster = yaml.safe_load((ROOT / "config" / "stores.yaml")
                                .read_text(encoding="utf-8"))["stores"]
        partner = next(s for s in roster
                       if (s.get("erp_name") or "").strip()
                       and not (s.get("marker") or "").strip())
        (self.root / "config" / "store-X.yaml").write_text(
            "erp_store_name: %s\n"
            "store_code: %s\n"
            "erp_branch_id: 308466\n"
            "timezone: Asia/Shanghai\n"
            "mail:\n  enabled: true\n  host: smtp.example.com\n"
            "wecom:\n  enabled: true\n" % (partner["erp_name"],
                                            partner["huawei_code"]),
            encoding="utf-8")
        return partner

    def test_平台标记也要清掉(self):
        """⚠ 这条是**用户那台机器的真实形状**：`platform: true` 在配置里,
        光有它门禁就永远放行（`setup_state` 第一句）。"""
        self._cfg_full()
        config_io.update(self.app.config_path, {"platform": True})
        self.assertTrue(web.setup_state(self.app)["ready"])
        code, body = self._logout()
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(web.setup_state(self.app)["ready"],
                         "清了登录数据还是放行 —— 退出登录等于没退")

    def test_门店身份也要清掉(self):
        """⚠ 另一条兜底：「门店配置已就绪」 —— `erp_store_name` + `store_code`
        都在就算过了（那是给**没登过门店账号的老店**留的），
        所以退出登录必须把它俩也清掉。"""
        self._cfg_full()
        self.assertTrue(web.setup_state(self.app)["ready"])
        self._logout()
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "erp")
        prof = st["profile"]
        for k in ("erp_name", "huawei_code", "marker", "platform"):
            with self.subTest(k=k):
                self.assertFalse(prof[k], "%s 还留着 —— 门禁会因此放行" % k)

    def test_别把门店自己配的东西带走(self):
        """⚠ 退出登录**只许清身份**。

        邮箱 / 企微 / 时区 / 抓取参数是门店自己配的 —— 被一次"退出登录"抹掉的话，
        重新登录之后推送就哑了，而**界面上看不出来**。
        """
        self._cfg_full()
        config_io.update(self.app.config_path, {"platform": True})
        self._logout()
        cfg = config_io.pick(config_io.load_raw(self.app.config_path))
        self.assertEqual(cfg.get("timezone"), "Asia/Shanghai")
        self.assertTrue(cfg.get("mail.enabled"))
        self.assertEqual(cfg.get("mail.host"), "smtp.example.com")
        self.assertTrue(cfg.get("wecom.enabled"))


class Test接口被真的挡住(_Base):
    """⚠ 用户要的是「**不给用**」—— 所以必须真挡，不能只是前端不显示。"""

    def _call(self, path, method="GET", body=None):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import json
        import threading
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            web.Handler.app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8"))

    def test_没过门禁时业务接口全403(self):
        self.cfg()                                   # 什么都没配
        for path in ("/api/overview", "/api/config", "/api/status", "/api/pos"):
            with self.subTest(path=path):
                code, body = self._call(path)
                self.assertEqual(code, 403)
                self.assertEqual(body["need"], "erp")
                self.assertIn("setup", body, "403 里要带上状态，前端才知道停在哪一步")

    def test_登录要用的那几个不能被挡(self):
        """⚠ 挡了它们就等于**永远登不进去**（登录页上每个按钮都 403）。"""
        self.cfg()
        for path in ("/api/setup", "/api/store-account", "/api/session/auto",
                     "/api/hwlogin", "/api/health"):
            with self.subTest(path=path):
                code, _ = self._call(path)
                self.assertNotEqual(code, 403, "%s 被挡了 —— 那就登不进去了" % path)

    def test_过了就能用(self):
        self.cfg("青岛永旺东部店", "CNSCN162188")
        code, _ = self._call("/api/overview")
        self.assertEqual(code, 200)


class Test重新登录按钮(_Base):
    """⚠ 用户 2026-09-19：「我点重新登陆怎么不好使啊」—— **我自己写出来的 bug**。

    那两个按钮原来调的是 `checkSetup()`，而它在"已经能用"时会**把登录页收起来**：
    于是一台正常的机器上点「重新登录」= 查完发现没问题 → 页面藏了 → 看着就是
    **"点了没反应"**。

    手动打开的语义是"我要去换账号 / 重抓"，**跟 ready 没关系**。
    """

    def setUp(self):
        super().setUp()
        import re
        raw = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        self.js = "\n".join(
            (l[:re.search(r"(?<!:)//", l).start()] if re.search(r"(?<!:)//", l) else l)
            for l in raw.splitlines())
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_按钮不走_checkSetup(self):
        """⚠ 这是那个 bug 的根：`checkSetup()` 在 ready 时**藏**页面。"""
        i = self.js.index("async function openSetup()")
        blk = self.js[i:i + 400]
        self.assertIn("showSetup(", blk, "手动打开要**强制显示**")
        self.assertNotIn("checkSetup(", blk)
        # ⚠ `#btn-open-setup`（通用设置里那张"门店"卡上的按钮）2026-09-21
        #   跟着卡一起删了（用户：「通用设置里第一块门店去掉」）——
        #   现在登录入口只有玲珑授权页那个 `-ll`，加上登录门禁自己。
        for ident in ("btn-open-setup-ll",):
            with self.subTest(ident=ident):
                j = self.js.index("$('#%s')?.addEventListener" % ident)
                self.assertIn("openSetup", self.js[j:j + 120])

    def test_重新登录要清除门店登录数据(self):
        """用户 2026-09-19：「重新登陆这个操作应该是**清除掉门店登录数据**的」。

        ⚠ 不先清的话，登录页会显示"已登录"、而人以为自己已经退出了。
        ⚠ 只清**门店账号**那份 —— 公司账号是内置的、玲珑会话是另一回事，
          都不该被这个动作带走。
        """
        i = self.js.index("async function openSetup()")
        blk = self.js[i:i + 700]
        self.assertIn("/api/store-account/logout", blk)
        self.assertIn("showSetup(", blk)
        # 后端那条接口真的会删文件
        import inspect
        src = inspect.getsource(web.Handler._api)
        self.assertIn("/api/store-account/logout", src)
        self.assertIn("unlink()", src)

    def test_退出按钮关页面但不停服务(self):
        """用户：「读取本店信息那个按钮改成退出，点击了就关掉浏览器页面了，
        **但是不退出后台**」。"""
        i = self.js.index("$('#btn-sa-quit')")
        blk = self.js[i:i + 500]
        self.assertIn("window.close()", blk)
        self.assertNotIn("/api/shutdown", blk, "别把后台服务也停了")


class Test人员设置(_Base):
    """用户 2026-09-18：「通用设置里面加一个人员设置，登录到门店后自动加载挂在
    门店下所有在职员工，当然也加上在职的复选框，门店可以选择剔除掉
    **已离职但是状态没更新**的员工」。

    ⚠ 复选框是这个功能**唯一**的手段：云商 `UserList` 的 `Status`
      实测 239 个账号**全是 3**，从它身上区分不出离职。
    """

    def _staff(self, users, cfg=None, excluded=None):
        cfg = cfg or {"erp_store_name": "青岛麦凯乐店", "erp_branch_id": 308466}
        (self.root / "config" / "store-X.yaml").write_text(
            "".join("%s: %s\n" % (k, v) for k, v in cfg.items()), encoding="utf-8")
        if excluded is not None:
            (self.root / ".secrets").mkdir(parents=True, exist_ok=True)
            (self.root / ".secrets" / "staff.json").write_text(
                '{"excluded": %s}' % json.dumps(excluded), encoding="utf-8")
        client = mock.Mock()
        client.users.return_value = users
        client.creds = {"token": "T"}
        # ⚠ 2026-09-19：业务搬到 `src/features/store/staff.py` 了 ⇒ **打桩点也要跟着搬**。
        #   不搬的话这里会去打**真云商**（实测：日志里出现"重登 token 已失效"，
        #   也就是说测试在联网）—— 挂点变了、约束没变。
        with mock.patch.object(app_staff, "ErpClient", lambda *a, **k: client), \
                mock.patch.object(app_staff, "load_credentials",
                                  lambda *a, **k: {"token": "T"}):
            return web.staff_state(self.app)

    def test_按机构加载员工_默认全在职(self):
        d = self._staff([
            {"UserName": "SL1", "Real": "张三", "Phone": "1", "BranchId": 308466},
            {"UserName": "sL2", "Real": "李四", "Phone": "2", "BranchId": 308466},
        ])
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["active_count"], 2, "默认应该全是在职")
        self.assertTrue(all(p["active"] for p in d["people"]))

    def test_剔除是按登录名_且忽略大小写(self):
        """⚠ 登录名大小写**不统一**（`SL…`/`sL…`/`sl…` 混着）——
        不忽略大小写的话，同一个人会被算成两个、剔除也不生效。"""
        d = self._staff(
            [{"UserName": "sL2", "Real": "李四", "Phone": "2"}],
            excluded=["SL2"])                      # 存的是大写，账号是小写
        self.assertFalse(d["people"][0]["active"], "大小写没忽略 —— 剔不掉")

    def test_存的是被剔除的那批不是在职那批(self):
        """⚠ 存"在职"的话，**以后新入职的人会默认变成不在职** —— 最坏的一种默认。"""
        # ⚠ `/api/staff` 在**门禁后面**（没登录好就不该看到人员名单）——
        #   所以这里要先把机器配成"能用的"：有门店名 + 编码、没标识（合作店）。
        self._staff([{"UserName": "SL1", "Real": "张三"}],
                    cfg={"erp_store_name": "青岛麦凯乐店",
                         "store_code": "CNSCN258646", "erp_branch_id": 308466})
        body = {"excluded": ["SL1"]}
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            web.Handler.app = self.app
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        c = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        c.request("PUT", "/api/staff", body=json.dumps(body),
                  headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 200)
        saved = json.loads((self.root / ".secrets" / "staff.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["excluded"], ["SL1"])

    def test_没认出门店就指路(self):
        d = self._staff([], cfg={"erp_store_name": "", "erp_branch_id": ""})
        self.assertFalse(d["ok"])
        self.assertIn("门店", d["error"])

    def test_机构id没有就按门店名查一次并写回配置(self):
        """老机器没登过门店账号、没有 `erp_branch_id` —— 靠这条兜底。"""
        client = mock.Mock()
        client.creds = {"token": "T"}
        client.users.return_value = [{"UserName": "SL1", "Real": "甲"}]
        client.call.return_value = {"Data": [
            {"Name": "平台", "Id": 0, "IsBranch": 0},
            {"Name": "青岛麦凯乐店", "Id": 308466, "IsBranch": 1},
        ]}
        (self.root / "config" / "store-X.yaml").write_text(
            'erp_store_name: "青岛麦凯乐店"\n', encoding="utf-8")
        with mock.patch.object(app_staff, "ErpClient", lambda *a, **k: client), \
                mock.patch.object(app_staff, "load_credentials",
                                  lambda *a, **k: {"token": "T"}):
            d = web.staff_state(self.app)
        self.assertEqual(d["branch_id"], 308466)
        v = config_io.pick(config_io.load_raw(self.app.config_path))
        self.assertEqual(v["erp_branch_id"], 308466, "查到了没写回配置，下次还得再查")


class Test平台岗(_Base):
    """用户 2026-09-19：「云商**平台岗账号**登录时提示是能匹配四十一家门店，
    不是按我要求的**给到所有功能页面的权限**」。

    ⚠ 原来 `branch_self()` 一看到多于一家店就报错（当成"公司账号填错了"）——
      **把平台岗挡在门外**。平台岗是第三类身份：挂在「平台」节点下、看得到全部门店，
      该给的是**全部功能**。
    """

    def _scope(self, data):
        from src import erp
        c = erp.ErpClient({"username": "u", "password": "p", "company": "C", "token": "t"})
        c.call = lambda url, body, timeout=None: {"ResponseID": 0, "Data": data}
        return c.branch_scope()

    def test_一家店算门店(self):
        r = self._scope([{"Name": "青岛CBD万达店", "Id": 1, "IsBranch": 1}])
        self.assertFalse(r["platform"])
        self.assertEqual(r["node"]["Name"], "青岛CBD万达店")

    def test_多家店算平台岗_不再报错(self):
        data = [{"Name": "平台", "Id": 0, "IsBranch": 0,
                 "Childs": [{"Name": "店%d" % i, "Id": i, "IsBranch": 1}
                            for i in range(41)]}]
        r = self._scope(data)
        self.assertTrue(r["platform"], "42 个节点该被认成平台岗")
        self.assertEqual(r["count"], 41)
        self.assertIsNone(r["node"])

    def test_一家都看不到才报错(self):
        from src import erp
        with self.assertRaises(erp.ErpError):
            self._scope([])

    def test_平台岗的门禁不要求玲珑(self):
        """⚠ 玲珑会话是**按门店编码**存的 —— 平台岗不属于任何一家店，
        所以不拿它卡门禁，否则平台岗永远进不去。"""
        p = config_io.store_profile({"platform": True}, self.root)
        self.assertTrue(p["platform"])
        self.assertTrue(p["show_all"], "平台岗菜单要全开")
        self.assertFalse(p["needs_linglong"])

    def test_平台岗过了门禁(self):
        (self.root / "config" / "store-X.yaml").write_text(
            "platform: true\n", encoding="utf-8")
        st = web.setup_state(self.app)
        self.assertTrue(st["ready"], "平台岗该直接能用")
        self.assertTrue(st["profile"]["show_all"])

    def test_换回门店账号要清掉平台标记(self):
        """⚠ 不清的话，换回门店账号之后菜单还是全开的、且不要求玲珑 —— 权限串了。"""
        import inspect
        src = inspect.getsource(web.store_lookup)
        self.assertIn('"platform": False', src)

    def test_平台岗看到所有功能页面(self):
        """⚠ 用户 2026-09-19：「云商平台岗账号……不是按我要求的
        **给到所有功能页面的权限**」。

        实现上**不单独给平台岗开小灶** —— 它就是一种身份（`platform`），
        表里"只有走玲珑的身份才看得见"的那几页，平台岗全算数。
        **这才是"整理到一起"的意思。**

        ⚠ 2026-09-21（M17 甲方案）：可见性表从 HTML 搬到了后端 ——
        现在断言的是 `pages_for()` 给出 `PAGE_RULES` 的**全部 key**（一页不漏）。
        """
        from src import config_io, web
        p = config_io.store_profile({"platform": True}, self.root)
        self.assertEqual(p["type"], "platform")
        self.assertEqual(web.pages_for({"role": web.ROLE_PLATFORM}),
                         sorted(web.PAGE_RULES), "平台岗该看得见**每一页**")
        # 平台岗不该被门禁拦住
        (self.root / "config" / "store-X.yaml").write_text("platform: true\n",
                                                           encoding="utf-8")
        self.assertTrue(web.setup_state(self.app)["ready"])


class Test前端接线(unittest.TestCase):
    def setUp(self):
        import re
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        raw = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
        self.js = "\n".join(
            (l[:re.search(r"(?<!:)//", l).start()] if re.search(r"(?<!:)//", l) else l)
            for l in raw.splitlines())

    def test_登录页在HTML里(self):
        # ⚠ 「我已登录完，重新检查」「收起」两个按钮 2026-09-19 被用户去掉了；
        #   想走就点「退出」（关页面、后台照常跑）。
        for ident in ("setup-mask", "setup-step-erp", "setup-step-linglong",
                      "btn-sa-quit", "setup-erp-why", "setup-linglong-why"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, self.html)

    def test_云商登录动作只在登录页有一份(self):
        """⚠ 云商那套（账号 / 密码 / 确认登录）**只在登录页里** ——
        同一套表单写两份、改一处漏一处是迟早的事（这个项目栽过好几次）。"""
        i = self.html.index('id="setup-mask"')
        # ⚠ 「公司代码」和「读取本店信息」2026-09-19 被去掉了
        #   （登录动作合并进「确认登录」一个按钮）。
        for ident in ("sa-username", "sa-password", "btn-sa-save"):
            with self.subTest(ident=ident):
                self.assertGreater(self.html.index('id="%s"' % ident), i,
                                   "%s 还在设置页里 —— 应该只有登录页那一份" % ident)

    def test_玲珑抓取控件是一份_DOM_两个宿主(self):
        """⚠ 2026-09-19（用户）：「玲珑授权页面显示不全，输入账密和打开浏览器登录
        和静默登录那个没了」——

        做登录门禁时，那几件控件被**只搬去了登录页**，玲珑授权页就只剩一句状态。
        用户报了之后改成**一份 DOM、两个宿主**：

        * **平时住 `#ll-home`**（玲珑授权页里）；
        * 门禁打开、而且这家店要走玲珑时，由 `app.js` 的 `mountLinglong()`
          **整块搬进**登录页的 `#ll-host`。

        ⚠ **关键不变量是"全文档只有一份"** —— 写成两份的话 id 会重复，
          `$('#hw-username')` 只命中第一个，另一处永远读不到值。
          （原来那条 `test_登录动作只在登录页有一份` 钉的正好是反的：
           它要求这些 id 出现在登录页**之后**，也就是"只许登录页有"。）
        """
        import re
        ids = re.findall(r'id="([^"]+)"', self.html)
        moved = ("hw-username", "hw-password", "btn-hw-save", "btn-auto-login",
                 "btn-auto-refresh", "btn-ping", "curl-input", "btn-import",
                 "import-result", "auto-result", "browser-line")
        for ident in moved:
            with self.subTest(ident=ident):
                self.assertEqual(ids.count(ident), 1,
                                 "%s 出现了 %d 次 —— 这些控件全文档只许有一份"
                                 % (ident, ids.count(ident)))

        # 家在哪、临时的宿主在哪
        home = self.html.index('id="ll-home"')
        panel = self.html.index('id="ll-panel"')
        host = self.html.index('id="ll-host"')
        self.assertLess(home, panel, "`#ll-panel` 得住在 `#ll-home` 里面")
        self.assertLess(panel, self.html.index('id="btn-auto-login"'),
                        "控件默认应该住在 `#ll-panel` 里")
        self.assertGreater(host, self.html.index('id="setup-mask"'),
                           "`#ll-host` 是登录页那边的临时住处")

    def test_搬移逻辑在_JS_里_且不是复制(self):
        """⚠ 必须是**搬移**（`appendChild`），不能是复制。

        复制有两种写法都很危险：
          * `host.innerHTML = panel.outerHTML` —— id 会重复；
          * 在 JS 里拼字符串造第二份 —— 绑定和提示文案会各自漂移。
        另外 `appendChild` 搬**不会丢事件监听**（元素本身没换），`innerHTML` 重建会丢。
        """
        i = self.js.index("function mountLinglong(")
        blk = self.js[i:i + 900]
        self.assertIn("appendChild", blk)
        self.assertNotIn("innerHTML", blk, "别用 innerHTML 重建 —— 会丢事件监听")
        self.assertIn("$('#ll-host')", blk)
        self.assertIn("$('#ll-home')", blk)
        # 门禁开/关时各调一次
        self.assertIn("mountLinglong(needLL)", self.js)
        self.assertIn("mountLinglong(false)", self.js)

    def test_门禁在最前面没过就别拉数据(self):
        """⚠ 不先问一次的话，控制台会先闪一下空页面再被 403 顶出登录页。"""
        # ⚠ 锚在**启动那句真代码**上，别锚注释 —— `self.js` 是**剥过注释**的
        #   （这个文件里好几处都被这个坑绊过）。
        #   也别锚 `(async () => {`：`pollRun` 里也有一个，会匹到隔壁去。
        i = self.js.index("const ready = await checkSetup()")
        blk = self.js[i - 200:i + 600]
        self.assertIn("await checkSetup()", blk)
        self.assertIn("if (!ready) return", blk)

    def test_403会把登录页顶出来(self):
        """会话中途过期时（比如用户点了自检没过）也得回得来。"""
        i = self.js.index("async function api(")
        blk = self.js[i:i + 900]
        self.assertIn("r.status === 403", blk)
        self.assertIn("showSetup(data.setup)", blk)

    def test_可见性表在后端_标签上不再标(self):
        """⚠ 用户 2026-09-19：「体验店也不是全开……你需要把这个**整理到一起**，
        每个一级标签和二级标签内容上都加上这个标记。为了以后的开发方便」。

        ⚠ 2026-09-21（M17 甲方案）：那条规矩**还在**，但"表"从 HTML 搬到了后端
        （`web.PAGE_RULES`，随 `/api/overview.role.pages` 下发）——
        这里盯两件事：① 标签上的 `data-types` **真的退休了**（否则又是两份定义）；
        ② 标签自己那个 key（`data-tab` / `data-subtab` / `data-foot`）**都还在**，
        因为前端就是拿它去 `pages` 里查的。
        ⚠ key ↔ `PAGE_RULES` 的**双向对照**在 `tests/test_roles.py::Test可见性对照`。
        """
        self.assertNotIn("data-types=", self.html,
                         "标签上不该再有 data-types（表在后端 PAGE_RULES）")
        # ⚠ 「玲珑授权」在**左下角那几行**里（`data-foot`，不是 `data-subtab`）——
        #   2026-09-19 用户把设置从浮层改成"左下角那几行就地变形"，
        #   键也跟着从 `data-subtab` 换成 `data-foot`（见 `test_web_ia`）。
        for sel in ('data-tab="compliance"',
                    'data-subtab="pos"',
                    'data-subtab="pools"',
                    'data-foot="linglong"',
                    'data-subtab="compliance-settings"'):
            with self.subTest(sel=sel):
                self.assertIn(sel, self.html)
        self.assertIn('data-tab="sales"', self.html)
        # ⚠ 「人员设置」2026-09-21 并进「账号与人员」了（用户：「合并到一起吧」）——
        #   那一行没了，人员名单在账号页里。这里改成确认**菜单里不再有它**。
        self.assertNotIn('data-foot="staff"', self.html)
        self.assertIn('id="staff-list"', self.html, "人员名单得还在（并进账号页了）")

    def test_前端只按后端下发的pages过滤_不写死判断(self):
        """⚠ 一旦在 `applyProfile` 里写 `if (needs_linglong)` / `if (type === ...)`，
        就是第二份可见性表了 —— 而"页面藏了、接口照样给"就是那么来的。"""
        i = self.js.index("function applyProfile(")
        blk = self.js[i:self.js.index("\n}\n", i)]      # 只取这一个函数体
        self.assertIn("role.pages", blk, "该照着后端下发的 pages 渲染")
        self.assertIn("el.hidden", blk)
        for bad in ("needs_linglong", "experience", "partner", "data-types"):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, blk, "又在前端自己判断可见性了")
        self.assertNotIn("querySelectorAll('.nav-item", blk, "又按标签名逐个判断了")

    def test_第二步按画像决定显不显示(self):
        """⚠ 合作店**连这一步都不显示** —— 显示成"可选的一步"会让人以为还得登。"""
        i = self.js.index("function showSetup(")
        blk = self.js[i:i + 1600]
        # ⚠ 按**身份**判，不看 needs_linglong（平台岗也不走玲珑）
        self.assertIn("'experience'", blk)
        self.assertIn("$('#setup-step-linglong').hidden = !needLL", blk)


if __name__ == "__main__":
    unittest.main()
