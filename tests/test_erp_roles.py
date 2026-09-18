"""云商**两个账号**（公司 / 门店）的凭据回归。

用户 2026-09-18：「需要存两个云商账号，一个是门店的账号，用来获取门店登录信息，
一个是我的最高权限账号，用来拉取全公司的数据」。

盯的是**会静默串账号**的那几处 —— 串了不报错，只是"拿错了账号去取数"：

* **公司账号必须回落读老的 `.secrets/erp.env`** —— 门店机器上现成配着的那一个
  本来就是能看全公司的（实测能拉到 42 家店）。不回落 ⇒ 14 家店升级完"没账号了"，
  而界面上只会显示一行空。
* **门店账号不许回落** —— 回落就等于拿公司账号冒充门店账号，
  "这台机器到底用哪个账号"从此谁也说不清。
* **写的时候别串** —— `save_credentials(role='store')` 碰到公司那个文件，
  就是"改门店账号、把公司账号覆盖了"，而且不报错。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import erp                                             # noqa: E402


class TestRoleFiles(unittest.TestCase):
    def test_公司账号沿用老文件(self):
        """⚠ 不是新开一个 `erp-company.env` —— 老门店那个文件本来就是公司账号。"""
        self.assertEqual(erp.ROLE_ENV_FILES[erp.ROLE_COMPANY], erp.DEFAULT_ENV_FILE)

    def test_门店账号是新文件(self):
        self.assertEqual(erp.ROLE_ENV_FILES[erp.ROLE_STORE], ".secrets/erp-store.env")

    def test_两个文件不一样(self):
        self.assertNotEqual(erp.ROLE_ENV_FILES[erp.ROLE_COMPANY],
                            erp.ROLE_ENV_FILES[erp.ROLE_STORE])

    def test_role_env_file_认得出两个角色(self):
        self.assertNotEqual(erp.role_env_file(erp.ROLE_COMPANY),
                            erp.role_env_file(erp.ROLE_STORE))

    def test_不认识的角色要抛(self):
        with self.assertRaises(ValueError):
            erp.role_env_file("boss")
        with self.assertRaises(ValueError):
            erp._role_chain("boss")


class TestRoleChain(unittest.TestCase):
    """读的**文件链** —— 这里决定了会不会串账号。"""

    def test_公司账号会回落老路径(self):
        chain = erp._role_chain(erp.ROLE_COMPANY)
        self.assertTrue(any(p == erp.resolve_env_path(erp.DEFAULT_ENV_FILE) for p in chain),
                        "公司账号没回落读 .secrets/erp.env —— 老门店升级完会没账号")
        self.assertIn(Path.home() / ".dsh" / "secrets" / "erp.env", chain)

    def test_门店账号不回落(self):
        chain = erp._role_chain(erp.ROLE_STORE)
        self.assertEqual(chain, [erp.role_env_file(erp.ROLE_STORE)],
                         "门店账号回落了 —— 那就等于拿公司账号冒充门店账号")

    def test_公司账号那条链里老文件只有一份(self):
        chain = erp._role_chain(erp.ROLE_COMPANY)
        self.assertEqual(len(chain), len(set(chain)), "链里有重复：%s" % chain)

    def test_显式指定的文件优先级最高(self):
        chain = erp._role_chain(erp.ROLE_COMPANY, "/tmp/other.env")
        self.assertEqual(chain[-1], Path("/tmp/other.env"))
        self.assertEqual(len(chain), len(set(chain)))


class TestLoadByRole(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def _patched(self):
        """把两个角色文件都指到临时目录，别碰真项目里的 `.secrets/`。"""
        return mock.patch.multiple(
            erp,
            LEGACY_ENV_PATHS=[self.root / "legacy.env"],
            ROLE_ENV_FILES={erp.ROLE_COMPANY: str(self.root / "company.env"),
                            erp.ROLE_STORE: str(self.root / "store.env")})

    def test_两个账号各读各的(self):
        (self.root / "company.env").write_text("ERP_USERNAME=co\nERP_PASSWORD=cp\n",
                                               encoding="utf-8")
        (self.root / "store.env").write_text("ERP_USERNAME=st\nERP_PASSWORD=sp\n",
                                             encoding="utf-8")
        with self._patched():
            co = erp.load_credentials(role=erp.ROLE_COMPANY)
            st = erp.load_credentials(role=erp.ROLE_STORE)
        self.assertEqual((co["username"], co["password"]), ("co", "cp"))
        self.assertEqual((st["username"], st["password"]), ("st", "sp"))

    def test_门店账号读不到就返回空_不会串公司账号(self):
        """⚠ 这条是**核心**：门店账号没配时必须是空，而不是悄悄用上公司账号。"""
        (self.root / "company.env").write_text("ERP_USERNAME=co\nERP_PASSWORD=cp\n",
                                               encoding="utf-8")
        with self._patched():
            st = erp.load_credentials(role=erp.ROLE_STORE)
        self.assertEqual(st["username"], "", "门店账号读到了公司账号的凭据")
        self.assertEqual(st["password"], "")

    def test_公司账号读不到角色文件时回落老文件(self):
        (self.root / "legacy.env").write_text("ERP_USERNAME=old\nERP_PASSWORD=op\n",
                                              encoding="utf-8")
        with self._patched():
            co = erp.load_credentials(role=erp.ROLE_COMPANY)
        self.assertEqual(co["username"], "old")

    def test_返回里带角色(self):
        for role in erp.ROLES:
            with self.subTest(role=role):
                with self._patched():
                    self.assertEqual(erp.load_credentials(role=role)["role"], role)

    def test_环境变量前缀(self):
        env = {"ERP_STORE_USERNAME": "env-store", "ERP_STORE_PASSWORD": "ep",
               "ERP_USERNAME": "env-company", "ERP_PASSWORD": "ecp"}
        with self._patched(), mock.patch.dict("os.environ", env, clear=False):
            st = erp.load_credentials(role=erp.ROLE_STORE)
            co = erp.load_credentials(role=erp.ROLE_COMPANY)
        self.assertEqual(st["username"], "env-store")
        self.assertEqual(co["username"], "env-company")

    def test_公司账号也认不带前缀的老变量名(self):
        """开发机/CI 一直是 `ERP_USERNAME` 那么配的，别让升级把它们弄丢。"""
        with self._patched(), mock.patch.dict("os.environ",
                                              {"ERP_USERNAME": "legacy-env"}, clear=False):
            co = erp.load_credentials(role=erp.ROLE_COMPANY)
        self.assertEqual(co["username"], "legacy-env")


class TestSaveAndDescribeByRole(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.files = {erp.ROLE_COMPANY: str(self.root / "company.env"),
                      erp.ROLE_STORE: str(self.root / "store.env")}

    def test_写门店账号不碰公司账号(self):
        """⚠ 串了就是"改门店账号、把公司账号覆盖了"，而且不报错。"""
        erp.save_credentials(self.files[erp.ROLE_COMPANY],
                             username="co", password="cp")
        with mock.patch.multiple(erp, ROLE_ENV_FILES=self.files,
                                 LEGACY_ENV_PATHS=[]):
            erp.save_credentials(username="st", password="sp", role=erp.ROLE_STORE)
            co = erp.load_credentials(role=erp.ROLE_COMPANY)
            st = erp.load_credentials(role=erp.ROLE_STORE)
        self.assertEqual(co["username"], "co", "公司账号被门店账号覆盖了")
        self.assertEqual(st["username"], "st")

    def test_不传路径就写角色自己的文件(self):
        with mock.patch.multiple(erp, ROLE_ENV_FILES=self.files, LEGACY_ENV_PATHS=[]):
            erp.save_credentials(username="st", role=erp.ROLE_STORE)
        self.assertIn("st", (self.root / "store.env").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "company.env").exists(),
                         "写门店账号时把公司账号那个文件创建出来了")

    def test_describe_默认看角色自己的文件(self):
        with mock.patch.multiple(erp, ROLE_ENV_FILES=self.files, LEGACY_ENV_PATHS=[]):
            d = erp.describe_credentials(role=erp.ROLE_STORE)
        self.assertEqual(d["role"], erp.ROLE_STORE)
        self.assertTrue(d["env_file"].endswith("store.env"), d["env_file"])
        self.assertEqual(d["username"], "")

    def test_describe_带角色标签(self):
        with mock.patch.multiple(erp, ROLE_ENV_FILES=self.files, LEGACY_ENV_PATHS=[]):
            self.assertIn("门店", erp.describe_credentials(role=erp.ROLE_STORE)["role_label"])
            self.assertIn("公司", erp.describe_credentials(role=erp.ROLE_COMPANY)["role_label"])

    def test_describe_公司账号回落时说清实际来自哪(self):
        legacy = self.root / "legacy.env"
        legacy.write_text("ERP_USERNAME=old\nERP_PASSWORD=op\n", encoding="utf-8")
        with mock.patch.multiple(erp, ROLE_ENV_FILES=self.files,
                                 LEGACY_ENV_PATHS=[legacy]):
            d = erp.describe_credentials(role=erp.ROLE_COMPANY)
        self.assertEqual(d["username"], "", "只读角色文件本身，不该显示别处的账号")
        self.assertTrue(d["used_from"].endswith("legacy.env"),
                        "回落了就要说清实际生效的是哪个文件")


class TestWebApiRoles(unittest.TestCase):
    """HTTP 接口那一层 —— 角色要能传到底，认不出来要**报错**而不是默认成公司账号。"""

    def test_认不出来要报错_不许默认成公司账号(self):
        from src.web import _erp_role
        role, err = _erp_role({"role": ["boss"]})
        self.assertIsNone(role)
        self.assertIn("不认识的角色", err)

    def test_缺省是公司账号(self):
        from src.web import _erp_role
        self.assertEqual(_erp_role({})[0], erp.ROLE_COMPANY)
        self.assertEqual(_erp_role({}, {})[0], erp.ROLE_COMPANY)

    def test_query_与_body_都认(self):
        from src.web import _erp_role
        self.assertEqual(_erp_role({"role": ["store"]})[0], erp.ROLE_STORE)
        self.assertEqual(_erp_role({}, {"role": "store"})[0], erp.ROLE_STORE)

    def test_body_覆盖_query(self):
        from src.web import _erp_role
        self.assertEqual(_erp_role({"role": ["company"]}, {"role": "store"})[0],
                         erp.ROLE_STORE)


if __name__ == "__main__":
    unittest.main()
