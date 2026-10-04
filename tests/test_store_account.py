"""门店云商账号 —— **只用来认「这台机器是哪家店」**。

用户 2026-09-18：「加个门店的登录设置吧，主要是读取这个账号的门店信息。
来匹配不同门店的设置，当然公司云商账号也是加密保留的，门店云商账号可以不加密」。

⚠ 这个功能当天**被删过一次又请回来**：上午删是因为"没有用途"
（实测也证明取数上它没有任何优势 —— 销售明细公司账号和它一模一样），
后来有了明确用途（读本店档案 → 匹配门店配置）。

这里钉的是**两条线不许串**：
* 公司账号：取数用、内置兜底（混淆）、界面不显示；
* 门店账号：只读本店档案、明文存、界面可填。
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml                                                       # noqa: E402

from src import config_io, erp, web                                # noqa: E402


class Test两个账号各是各的(unittest.TestCase):
    def test_文件不一样(self):
        self.assertNotEqual(erp.STORE_ENV_FILE, erp.DEFAULT_ENV_FILE)

    def test_门店账号不回落公司账号(self):
        """⚠ 公司账号能看到全公司 41 家店 —— 拿它"认本店"只会读到一大堆。

        所以门店账号**故意没有回落链**：有就是有，没有就是没有。
        """
        with tempfile.TemporaryDirectory() as tmp:
            # 把老路径指到一个**有内容**的文件上：真回落的话这里就读到了
            legacy = Path(tmp) / "erp.env"
            legacy.write_text("ERP_USERNAME=company\\nERP_PASSWORD=pw\\n", encoding="utf-8")
            # ⚠ 别用反斜杠续行拼多个 with —— 这文件是写出来的，`\\` 会被字面化；
            #   括号化的 `with (a, b):` 又是 3.10 才有的语法（本项目底线 3.8）。
            with mock.patch.multiple(erp, LEGACY_ENV_PATHS=[legacy],
                                     STORE_ENV_FILE=str(Path(tmp) / "store.env")):
                with mock.patch.dict("os.environ", {}, clear=True):
                    d = erp.load_store_credentials()
        self.assertEqual(d["username"], "", "门店账号回落到了公司账号那份")
        self.assertEqual(d["password"], "")

    def test_门店账号明文存不要混淆(self):
        """用户：「门店云商账号**可以不加密**」。

        公司账号那对是 `_BUILTIN_*` 混淆的；门店账号按用户的话就明文写文件。
        """
        with tempfile.TemporaryDirectory() as tmp:
            f = str(Path(tmp) / "store.env")
            with mock.patch.object(erp, "STORE_ENV_FILE", f):
                erp.save_store_credentials(username="sl123", password="pw456")
            text = Path(f).read_text(encoding="utf-8")
        self.assertIn("sl123", text, "账号没明文写进去")
        self.assertIn("pw456", text, "密码被加密/改写了？用户说可以不加密")

    def test_describe_永不回显密码(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = str(Path(tmp) / "store.env")
            with mock.patch.object(erp, "STORE_ENV_FILE", f):
                erp.save_store_credentials(username="u", password="secret123")
                d = erp.describe_store_credentials()
        self.assertNotIn("password", d)
        self.assertNotIn("secret123", json.dumps(d))
        self.assertTrue(d["has_password"])

    def test_换账号要作废旧_token(self):
        """⚠ 旧 token 属于旧账号 —— 不清的话"改了账号却还在用上一个账号的登录态"。"""
        with tempfile.TemporaryDirectory() as tmp:
            f = str(Path(tmp) / "store.env")
            with mock.patch.object(erp, "STORE_ENV_FILE", f):
                erp.save_store_credentials(username="a", password="p", token="TOK-A")
                erp.save_store_credentials(username="b", clear_token=True)
                d = erp.describe_store_credentials()
        self.assertEqual(d["username"], "b")
        self.assertFalse(d["has_token"], "换了账号旧 token 还留着")


    def test_姓名要挑_Real_不是登录账号(self):
        """⚠⚠ 用户 2026-09-19：「我重新登录了，**姓名没有啊**」。

        实测（拿门店 token 调一次 `Api/User/UserIndex`，返回 82 个字段）：

        | 字段 | 值 | 是什么 |
        |---|---|---|
        | `UserName` | `sl18917405716` | **登录账号** |
        | **`Real`** | `赵海培` | ⭐ **姓名** |
        | `TLClerkName` | `赵海培` | 同一个人的另一种写法 |
        | `CompanyName` | 山东盛联数码科技有限公司 | 公司名 |

        而原来的候选列表是
        `("UserName", "Name", "RealName", "NickName", "CompanyName")` ——
        **一个都命中不了真名**（真名字段叫 `Real`，不是 `RealName`），
        于是一路落到 `UserName`，左下角显示成了登录账号。

        ⇒ 这条钉**顺序**：真名在前，登录账号 / 公司名垫底。
        """
        fake = {"UserName": "sl18917405716", "Real": "赵海培",
                "TLClerkName": "赵海培", "CompanyName": "山东盛联数码科技有限公司",
                "UserPwd": "别再往这儿加字段了"}          # 返回里真有个 UserPwd
        c = erp.ErpClient({"username": "u", "password": "p",
                           "company": "C", "token": "T"})
        with mock.patch.object(erp.ErpClient, "login", lambda *a, **k: "T"), \
             mock.patch.object(erp.ErpClient, "user_index", lambda *a, **k: fake), \
             mock.patch.object(erp.ErpClient, "_save_token", lambda *a, **k: None):
            r = erp.ErpClient.login_and_verify(c, save=False)
        self.assertEqual(r["who"], "赵海培", "又挑成登录账号了")

    def test_没有真名时才退回登录账号(self):
        """⚠ 兜底也要有 —— 有的账号 `Real` 是空的。
        退回登录账号 / 公司名**总比空着强**，但要**排在真名后面**。"""
        c = erp.ErpClient({"username": "u", "password": "p",
                           "company": "C", "token": "T"})
        with mock.patch.object(erp.ErpClient, "login", lambda *a, **k: "T"), \
             mock.patch.object(erp.ErpClient, "user_index",
                               lambda *a, **k: {"UserName": "sl001", "Real": ""}), \
             mock.patch.object(erp.ErpClient, "_save_token", lambda *a, **k: None):
            r = erp.ErpClient.login_and_verify(c, save=False)
        self.assertEqual(r["who"], "sl001")

    def test_登录人的姓名要落盘(self):
        """⚠ 用户 2026-09-19：「左下角显示门店的同时**也显示账号人员姓名**吧」。

        `who` 是云商登录后**额外拉一次用户资料**才拿到的（`login_and_verify`），
        原来**用完就扔** —— 于是左下角想显示"谁登的"根本没数据。
        ⇒ 现在落到 `.secrets/erp-store.env` 的 `ERP_WHO` 里。
        """
        with tempfile.TemporaryDirectory() as tmp:
            f = str(Path(tmp) / "store.env")
            with mock.patch.object(erp, "STORE_ENV_FILE", f):
                erp.save_store_credentials(username="u", who="张三")
                d = erp.describe_store_credentials()
            # 明文存（和门店账号一个规矩），但不许把密码一起带出来
            self.assertIn("张三", Path(f).read_text(encoding="utf-8"))
        self.assertEqual(d["who"], "张三")
        self.assertNotIn("password", d)

    def test_没拿到姓名时不许把上次的抹掉(self):
        """⚠ `who` 是登录后**额外拉一次**用户资料才有的，那次失败就返回空串。

        写空会把上次记住的姓名抹掉，而后台每天照跑 —— **谁也看不出名字
        是什么时候没的**（跟 `test_没匹配上不许把已有的编码抹成空` 同一个道理）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            f = str(Path(tmp) / "store.env")
            with mock.patch.object(erp, "STORE_ENV_FILE", f):
                erp.save_store_credentials(username="u", who="张三")
                # `who=None` ⇒ **这个字段不动**（`save_store_credentials` 的约定）
                erp.save_store_credentials(username="u", token="T")
                d = erp.describe_store_credentials()
        self.assertEqual(d["who"], "张三", "没拿到姓名却把上次的抹掉了")


class Test认本店(unittest.TestCase):
    """`branch_self()` —— 组织架构是**唯一按账号收窄**的接口（实测过）。"""

    def _client(self, data):
        c = erp.ErpClient({"username": "u", "password": "p", "company": "C", "token": "t"})
        c.call = lambda url, body, timeout=None: {"ResponseID": 0, "Data": data}
        return c

    def test_门店账号只看到自己那一家(self):
        node = {"Name": "青岛CBD万达店", "Id": 308468, "IsBranch": 1, "Level": 2,
                "Childs": []}
        self.assertEqual(self._client([node]).branch_self()["Name"], "青岛CBD万达店")

    def test_公司账号要多报错_不许自己挑一家(self):
        """⚠ 猜错就是把配置填成隔壁那家店 —— 而门店名看着都像对的。

        公司账号实测摊平后 42 个节点（1 个容器「平台」+ 41 家店）。
        """
        data = [{"Name": "平台", "Id": 0, "IsBranch": 0, "PId": -1,
                 "Childs": [{"Name": "店%d" % i, "Id": i, "IsBranch": 1, "Childs": []}
                            for i in range(41)]}]
        with self.assertRaises(erp.ErpError) as cm:
            self._client(data).branch_self()
        self.assertIn("公司账号", str(cm.exception))

    def test_容器节点不算门店(self):
        """⚠ 判据是 `IsBranch`，不是"摊平后有几条" —— 容器节点也占一条。"""
        self.assertEqual(erp.branch_nodes([{"Name": "平台", "IsBranch": 0}]), [])
        self.assertEqual(len(erp.branch_nodes([{"Name": "平台", "IsBranch": 0,
                                                "Childs": [{"Name": "店", "IsBranch": 1}]}])), 1)

    def test_一家都看不到也要报错(self):
        with self.assertRaises(erp.ErpError):
            self._client([]).branch_self()


class Test匹配门店名单(unittest.TestCase):
    """读出来的门店名要去 `config/stores.yaml` 里匹配 —— 这是这个功能的**全部意义**。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "out").mkdir(parents=True, exist_ok=True)
        # ⚠ 名单要拷真的一份进去 —— 生产环境里 `app.root` 是安装目录，
        #   那儿**有** `config/stores.yaml`；临时目录里没有的话，
        #   匹配永远返回"没找到"，测试就测了个寂寞。
        (self.root / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "config" / "stores.yaml", self.root / "config" / "stores.yaml")
        self.app = web.App(self.root, "config/store-X.yaml")
        # 期望值**从名单里现读** —— 别把 SCN328987 这种数据写死在测试里，
        # 名单是随程序走的数据文件，改了它测试就该跟着走
        self.roster = {s["erp_name"]: s for s in
                       yaml.safe_load((ROOT / "config" / "stores.yaml")
                                      .read_text(encoding="utf-8"))["stores"]}

    def _cfg(self):
        return config_io.load_raw(self.app.config_path)

    def _write_cfg(self, **kv):
        (self.root / "config" / "store-X.yaml").write_text(
            "".join("%s: %s\n" % (k, v) for k, v in kv.items()), encoding="utf-8")

    def _lookup(self, name, has_password=True):
        node = {"Name": name, "Id": 1, "IsBranch": 1}
        client = mock.Mock()
        # ⚠ 2026-09-19 起用 `branch_scope()`（它把"看到几家店"变成结构化结果）——
        #   还用 `branch_self` 的话，Mock 会返回一个**真值**，于是被当成平台岗
        #   走另一条分支，配置一条都不写（实测踩到）。
        client.branch_scope.return_value = {"platform": False, "node": node, "count": 1}
        client.login_and_verify.return_value = {"token": "T", "who": "w"}
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "u", "company": "C",
                                                "has_password": has_password,
                                                "has_token": False}):
            with mock.patch.object(web, "load_store_credentials",
                                   lambda *a, **k: {"username": "u", "password": "p",
                                                    "company": "C", "token": ""}):
                with mock.patch.object(web, "ErpClient", lambda *a, **k: client):
                    # ⚠ 现在第一个位置参数是**文件路径**（挂在 `app.root` 下）——
                    #   写 `lambda **k` 会 TypeError（实测踩到）
                    with mock.patch.object(web, "save_store_credentials",
                                           lambda *a, **k: self.root / "x"):
                        return web.store_lookup(self.app)

    def test_匹配上就把配置带出来(self):
        want = self.roster["青岛CBD万达店"]
        r = self._lookup("青岛CBD万达店")
        self.assertTrue(r["ok"])
        m = r["matched"]
        self.assertTrue(m["found"], "名单里明明有这家店")
        self.assertEqual(m["huawei_code"], want["huawei_code"])
        self.assertEqual(m["marker"], want["marker"])
        self.assertEqual(m["kind"], want["kind"])

    def test_名单是_29_家_合作店也在里面(self):
        """用户 2026-09-18 给了全量名单：15 家授权体验店 + 14 家合作店。

        ⚠ 合作店**在名单里**（能被匹配到），但**不进调拨规则** ——
           `cli.load_config` 的 `_experience` 只收 `kind == 体验店`。
        """
        # ⚠ 2026-09-19 起名单里**多了一行虚拟门店**（`kind: 平台岗`）——
        #   用户：「平台岗就做个虚拟的平台岗门店就是了」。
        #   它**不是真店**（没有华为编码、不绑任何一家店），
        #   所以这里的计数要把它排除掉，而不是把 29 改成 30。
        virtual = {n: s for n, s in self.roster.items()
                   if s.get("kind") == config_io.PLATFORM_KIND}
        self.assertEqual(list(virtual), [config_io.PLATFORM_STORE],
                         "虚拟平台岗门店不在名单里（或者名字对不上）")
        real = {n: s for n, s in self.roster.items() if n not in virtual}
        self.assertEqual(len(real), 29)
        kinds = {}
        for s in real.values():
            kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
        self.assertEqual(kinds, {"体验店": 15, "合作店": 14})
        # 每家**真店**都要有华为编码（这份名单的主要价值之一）
        miss = [n for n, s in real.items() if not s.get("huawei_code")]
        self.assertEqual(miss, [], "这些店没有华为编码：%s" % miss)

    def test_没匹配上不是错误_但要说清(self):
        """⚠ 云商里的店不一定都在名单上（合作店、或名单还没补）。

        静默什么都不填的话，门店只会看到"点了没反应"。
        """
        r = self._lookup("不存在的店")
        self.assertTrue(r["ok"], "匹配不上不该当成失败")
        self.assertFalse(r["matched"]["found"])
        self.assertEqual(r["matched"]["erp_name"], "不存在的店", "云商名还是要回填的")
        self.assertEqual(r["matched"]["marker"], "")

    def test_匹配上就直接写配置(self):
        """用户 2026-09-18：「直接写」—— 界面上那三个输入框已经删了，
        所以这是**唯一**的配置来源。"""
        self._write_cfg(erp_store_name="旧店", store_code="OLD", marker="X")
        r = self._lookup("青岛CBD万达店")
        self.assertIn("erp_store_name", r["written"])
        v = self._cfg()
        self.assertEqual(v["erp_store_name"], "青岛CBD万达店")
        self.assertEqual(v["store_code"], "SCN328987")
        self.assertEqual(v["marker"], "C")

    def test_换到没有标识的店要清掉旧标识(self):
        """⚠⚠ 2026-09-18 实测踩到（用户当场发现）：「但是这个标识不应该存在啊」。

        他从新业广场店（标识 Y）换登成**麦凯乐店**（合作店，名单里**没有**标识），
        而 `marker: Y` 是上一家店留下的旧值。

        **后果不只是脏数据**：`needs_linglong` 是看 marker 的 ⇒
        一家合作店被算成"要玲珑"，永远进不去控制台。
        """
        # 麦凯乐店在名单里是**合作店、没有标识**
        self.assertFalse(self.roster["青岛麦凯乐店"].get("marker"),
                         "名单变了？这条测试的前提没了")
        self._write_cfg(erp_store_name="青岛新业广场店",
                        store_code="SCN231409", marker="Y")
        r = self._lookup("青岛麦凯乐店")
        self.assertTrue(r["matched"]["found"])
        v = self._cfg()
        self.assertEqual(v["erp_store_name"], "青岛麦凯乐店")
        self.assertEqual(v["store_code"], self.roster["青岛麦凯乐店"]["huawei_code"])
        self.assertEqual(v["marker"], "", "旧标识没清掉 —— 这家店会被算成要玲珑")
        # 而且画像要跟着变对
        prof = config_io.store_profile(v, self.root)
        self.assertFalse(prof["needs_linglong"], "合作店不该要玲珑")

    def test_没匹配上不许把已有的编码抹成空(self):
        """⚠⚠ 这条最要紧。

        `found=False` 时如果照样写 `store_code=""`，会把门店**上次配好的编码抹掉** ——
        而现象是"华为订单突然查不到了"，完全看不出是"读了本店信息"导致的。
        """
        self._write_cfg(erp_store_name="旧店", store_code="SCN000000", marker="X")
        r = self._lookup("云商里新开的一家店")
        self.assertFalse(r["matched"]["found"])
        v = self._cfg()
        self.assertEqual(v["store_code"], "SCN000000", "把已有的华为编码抹掉了")
        self.assertEqual(v["marker"], "X", "把已有的串号标识抹掉了")
        # 云商名是**读到的真实值**，这个该更新
        self.assertEqual(v["erp_store_name"], "云商里新开的一家店")

    def test_编码变了要提示重新抓会话(self):
        """⚠ 华为会话是按**门店编码**存的文件（`.secrets/cbg-<编码>.json`）——
        编码一变等于换了一份会话，门店会看到「会话未导入」，像是会话丢了。"""
        self._write_cfg(erp_store_name="旧店", store_code="SCN000000")
        r = self._lookup("青岛CBD万达店")
        self.assertTrue(r["rehint"])
        self.assertEqual(r["store_code_from"], "SCN000000")
        self.assertEqual(r["store_code_to"], "SCN328987")

    def test_编码没变就别瞎提示(self):
        self._write_cfg(erp_store_name="青岛CBD万达店", store_code="SCN328987")
        self.assertFalse(self._lookup("青岛CBD万达店")["rehint"])

    def test_没配账号要指路(self):
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "", "has_token": False,
                                                "company": "C", "has_password": False}):
            r = web.store_lookup(self.app)
        self.assertFalse(r["ok"])
        self.assertIn("还没填", r["error"])

    def test_平台岗登录要写成虚拟门店_并清掉原来那家店(self):
        """⚠ 2026-09-19 用户：「平台岗就做个**虚拟的平台岗门店**就是了」。

        改之前是写一个**独立的标志** `platform: true`，而**不清**原来那家店的字段 ——
        配置里会同时留着「某家店」+「平台岗」，而画像是 platform 优先短路
        ⇒ 那家店成了**死数据**，哪天标志一关它就"活过来"
        （"旧值一直赢"，这个坑 2026-09-18 在串号标识上踩过一次；
         开发机上真出现这个形状，也正是"退出登录不生效"那个 bug 的土壤）。

        ⇒ 现在跟普通门店登录**走同一条路**：写虚拟店名 + 清别的身份字段。
        """
        # 先配成一家真店
        self._write_cfg(erp_store_name="青岛CBD万达店", store_code="SCN328987",
                        marker="D")
        config_io.update(self.app.config_path, {"platform": True,
                                                "erp_branch_id": "308466"})
        # 再从平台岗登录
        client = mock.Mock()
        client.branch_scope.return_value = {"platform": True, "node": {},
                                            "count": 41}
        client.login_and_verify.return_value = {"token": "T", "who": "平台岗的人"}
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "u", "company": "C",
                                                "has_password": True,
                                                "has_token": False}), \
             mock.patch.object(web, "load_store_credentials",
                               lambda *a, **k: {"username": "u", "password": "p",
                                                "company": "C", "token": ""}), \
             mock.patch.object(web, "ErpClient", lambda *a, **k: client), \
             mock.patch.object(web, "save_store_credentials",
                               lambda *a, **k: self.root / "x"):
            r = web.store_lookup(self.app)

        self.assertTrue(r["ok"])
        v = config_io.pick(config_io.load_raw(self.app.config_path))
        self.assertEqual(v["erp_store_name"], config_io.PLATFORM_STORE,
                         "平台岗没写成虚拟门店")
        for k in ("store_code", "marker", "erp_branch_id"):
            with self.subTest(k=k):
                self.assertFalse(v.get(k), "原来那家店的 %s 还留着 —— 两种身份并存了" % k)
        self.assertFalse(v.get("platform"),
                         "老标志该清掉 —— 身份现在由名单里那行虚拟门店说了算")
        # 画像要认得出这是平台岗
        prof = config_io.store_profile(v, self.root)
        self.assertEqual(prof["type"], "platform")
        self.assertFalse(prof["needs_linglong"])

    def test_认证方式不对时要说清是账号问题(self):
        client = mock.Mock()
        client.login_and_verify.side_effect = erp.ErpError("这个云商账号能看到 41 家门店")
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "u", "company": "C",
                                                "has_password": True, "has_token": False}):
            with mock.patch.object(web, "load_store_credentials",
                                   lambda *a, **k: {"username": "u", "password": "p",
                                                    "company": "C", "token": ""}):
                with mock.patch.object(web, "ErpClient", lambda *a, **k: client):
                    r = web.store_lookup(self.app)
        self.assertFalse(r["ok"])
        self.assertIn("41 家", r["error"])


def _js_code(src: str) -> str:
    """剥掉 JS 注释。

    ⚠ 这个项目的注释专门写"这里以前是什么、为什么拿掉"，里面会**原样出现**
    被禁的标识符 —— 不剥的话注释自己就把 `assertNotIn` 顶掉了
    （这个坑在 HTML / CSS / JS 三边都踩过）。
    `//` 要排除 `https://`，否则会把整行从协议头那儿切掉。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.splitlines():
        m = re.search(r"(?<!:)//", line)
        out.append(line[:m.start()] if m else line)
    return "\n".join(out)


class Test前端接线(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.js = _js_code("\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js", "app.js")))

    def test_卡片和控件都在(self):
        # ⚠ 「公司代码」「读取本店信息」2026-09-19 去掉了；
        #   「保存账号」改叫「确认登录」，入口切换在登录卡片底部。
        for ident in ("sa-username", "sa-password", "sa-status",
                      "btn-sa-save", "sa-captcha", "sa-msg"):
            with self.subTest(ident=ident):
                self.assertIn('id="%s"' % ident, self.html)

    def test_前端不再引用那三个输入框(self):
        """⚠ 用户 2026-09-18：「门店登录完，是不是上面那个门店信息设置就不用了」——
        三个输入框删了，配置改成**由后端直接写**。

        前端要是还留着 `$('#cfg-store_code')`，那就是 `null.value` 当场抛错，
        而它后面的初始化全不执行（前端没 lint，只在控制台露一行）。
        """
        for ident in ("cfg-store_code", "cfg-marker", "cfg-erp_store_name",
                      "btn-save-store", "applyMatchedStore"):
            with self.subTest(ident=ident):
                self.assertNotIn(ident, self.js,
                                 "%s 还留着 —— 那三格已经删了" % ident)

    def test_门店那张卡和摘要一起删了(self):
        """⚠ 2026-09-21（用户：「**通用设置里第一块门店去掉**」）。

        原来那张卡上有一行摘要（店名 · 编码 · 标识），是"一眼确认这台机器配的是
        哪家店"的地方 —— 删了之后那三个信息在**左下角那行**（`#store-line`）
        和「账号设置」页上，不是消失。
        ⚠ 删 HTML 必须连 JS 一起清：只删 HTML 的话 `renderStoreSummary()` 会
          对着一个 null 默默什么都不干（这个项目栽过好几次）。
        """
        self.assertNotIn('id="store-summary"', self.html)
        self.assertNotIn("function renderStoreSummary(", self.js)
        self.assertNotIn("renderStoreSummary(", self.js)
        self.assertIn('id="store-line"', self.html, "店名/编码/标识得有个地方看得见")


    def test_注释里别再写别再把它加回来(self):
        """⚠ 这功能一天内删了又加。留着那句"别再照着加回来"的话，
        下一个人会照着它把功能又拆了。"""
        self.assertNotIn("别再照着", self.js)


if __name__ == "__main__":
    unittest.main()
