# -*- coding: utf-8 -*-
"""四池的**品类范围** —— 只有六类参与对账（用户 2026-09-21）。

业务：「四池对比时，只对比手机、穿戴、音频、平板、电脑、智慧屏这些，其他的东西不对比」；
「电脑」= 只算笔记本。

⚠ 这个文件钉的是**四件容易错的事**：

1. **音频在玲珑侧**：`lg_stock` 的父类 `category_parent_name` 把「音频系列」归到
   **「配件」**下 —— 按父类过滤会把这批耳机全判成"玲珑没有"，于是 BC 里凭空多出
   一堆假差异。所以判据一律走**细类 `category_name`**。
2. **对称**：只在一侧过滤 ⇒ 造出假 AD/BC。测试里专门有一条。
3. **退化不许清空**：库里没有品类列时（老库 / 精简库），四个池会被整体清空，
   界面上是"四象限全 0"、看着像没数据 —— 必须**退化成不过滤**，而且说明原因。
4. **未知词不许静默归「其它」**：要单独报出来（本项目红线：少给了必须吭声）。
"""

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.features.compliance.comparison import category as CAT      # noqa: E402
from src.features.compliance.comparison import store as S           # noqa: E402
from src.features.compliance.comparison import text as T            # noqa: E402


# ---------------------------------------------------------------- 纯函数
class Test判据(unittest.TestCase):
    def test_四种返回值(self):
        """六类之一 / 其它 / 未知 / 空 —— 四种，别混。"""
        t = {"手机": "手机", "保护壳套": CAT.OTHER}
        self.assertEqual(CAT.classify("手机", t), "手机")
        self.assertEqual(CAT.classify("保护壳套", t), CAT.OTHER)
        self.assertEqual(CAT.classify("没见过的词", t), CAT.UNKNOWN)
        for empty in (None, "", "   "):
            self.assertIsNone(CAT.classify(empty, t), repr(empty))

    def test_关键钉子_六个大类各一个(self):
        """每个大类挑一个**实测出现过**的词钉住 —— 改映射表时改错会当场红。"""
        self.assertEqual(CAT.ERP_SALES["手机"], "手机")
        self.assertEqual(CAT.ERP_STOCK["手环"], "穿戴")          # ⚠ 第一版漏了它
        self.assertEqual(CAT.LG_STOCK["音频系列"], "音频")       # ⚠ 父类是「配件」
        self.assertEqual(CAT.ERP_STOCK["平板电脑"], "平板")
        self.assertEqual(CAT.ERP_SALES["笔记本"], "电脑")        # 用户：电脑只算笔记本
        self.assertEqual(CAT.ERP_STOCK["华为智慧屏"], "智慧屏")  # ⚠ 第一版漏了它
        self.assertEqual(CAT.ERP_STOCK["WIKO笔记本"], "电脑")

    def test_电脑只算笔记本(self):
        """用户 2026-09-21：「只算笔记本（MateBook 这类）」—— 台式机/显示器/打印机/周边都不算。"""
        for word in ("消费台式机", "商用台式机", "显示器", "打印机", "一体机"):
            with self.subTest(word=word):
                self.assertEqual(CAT.ERP_STOCK[word], CAT.OTHER, word)
        for word in ("电脑周边", "台显打印"):
            with self.subTest(word=word):
                self.assertEqual(CAT.ERP_SALES[word], CAT.OTHER, word)

    def test_值只能是六类或其它(self):
        """映射表里**不许出现第三种值**（那样 `keep_set` 会静默漏掉它）。"""
        for name, table in CAT.TABLES.items():
            for word, cat in table.items():
                with self.subTest(table=name, word=word):
                    self.assertIn(cat, CAT.CATS + (CAT.OTHER,), cat)

    def test_六个大类就是用户说的那六个(self):
        self.assertEqual(CAT.CATS, ("手机", "穿戴", "音频", "平板", "电脑", "智慧屏"))

    def test_head_of_取商品名前缀(self):
        self.assertEqual(CAT.head_of("智能手机/华为/Mate 80"), "智能手机")
        self.assertEqual(CAT.head_of(""), "")

    def test_classify_sales_商品名前缀优先于一级分类(self):
        """⭐ 2026-09-23 报障：表带被 `一级分类=智能穿戴` 判成「穿戴」混进 BC。

        池C 改成**商品名前缀优先**（跟池D 同一张词表），一级分类只兜底。
        """
        # 表带：一级分类说穿戴、商品名前缀说配件 → 取配件
        cat, w = CAT.classify_sales("智能穿戴", "表带/华为/银河紫素皮氟橡胶复合表带EFT-LF(22mm)-银河紫")
        self.assertEqual(cat, CAT.OTHER, "表带不该算穿戴")
        self.assertIsNone(w, "前缀认得出就不收 unknown")
        # 眼镜同理
        self.assertEqual(CAT.classify_sales("智能穿戴", "眼镜/华为/华为AI眼镜 AIL-G01")[0], CAT.OTHER)
        # 前缀认不出（手机贴膜）→ 回落一级分类
        self.assertEqual(CAT.classify_sales("手机平板周边", "手机贴膜/某品牌钢化膜")[0], CAT.OTHER)
        self.assertEqual(CAT.classify_sales("手机", "某认不出的前缀/xx")[0], "手机")
        # 两边都认不出 → 收进 unknown（红线：不许静默）
        cat, w = CAT.classify_sales("没见过的分类", "没见过的前缀/xx")
        self.assertEqual(cat, CAT.UNKNOWN)
        self.assertEqual(w, "没见过的分类")

    def test_表带这类被玲珑归成华为手表也要剔(self):
        """⭐ 池间「其它赢」才抓得到的表带 —— 玲珑在库把表带归成「华为手表」(穿戴)。

        实测 `4SCBB25408118002`：云商商品名=表带(其它)、玲珑 category_name=华为手表(穿戴)。
        只靠 `classify_sales` 抓不到（玲珑说六类），必须池间「其它赢」。
        """
        conn = _db()
        _sold(conn, "SN_STRAP", "智能穿戴")
        conn.execute("UPDATE erp_sales SET 商品名称=? WHERE sn=?",
                     ("表带/华为/某表带", "SN_STRAP"))
        _lg_stock(conn, "SN_STRAP", "华为手表")     # 玲珑把表带归进华为手表
        self.assertEqual(S.quadrants(conn)["BC"], 0, "表带不该进 BC")

    def test_merge_池间平手时云商优先(self):
        """两侧都判**六类**时（平手）按 `SOURCES` 顺序 = 云商侧赢。"""
        got = CAT.merge({"erp_sales": {"SN1": "平板"}, "lg_stock": {"SN1": "手机"}})
        self.assertEqual(got["SN1"], "平板", "平手时云商侧（SOURCES 在前）保留")

    def test_merge_池间冲突时其它赢(self):
        """⭐ 用户 2026-09-23 拍板**推翻第一版**：池间说法矛盾时「不参与」那侧赢。

        第一版是「六类 > 其它」（多带一台只是噪音），但表带的实测把它证伪了 ——
        池C 粗类说「穿戴」、池D/玲珑细判说「配件」，六类赢 ⇒ 表带混进 BC。
        → 现在两侧矛盾就信「配件」，宁可少一条差异、不报假差异。
        """
        got = CAT.merge({"erp_sales": {"SN1": "手机"}, "lg_stock": {"SN1": CAT.OTHER}})
        self.assertEqual(got["SN1"], CAT.OTHER, "一侧说配件、一侧说手机 → 剔")
        got2 = CAT.merge({"erp_sales": {"SN1": CAT.OTHER}, "erp_stock": {"SN1": "手机"}})
        self.assertEqual(got2["SN1"], CAT.OTHER)

    def test_merge_池内多行六类优先(self):
        """⚠ 池**内**多行是另一套（`_rank_within`，六类赢）—— 跟池间别混。

        同一串号在池A 既买手机又挂 HUAWEI Care+（其它）⇒ 这台是手机。
        若池内也按「其它赢」，`8BBUT26820011620` 这种真手机会被当成配件剔掉。
        """
        got = CAT.merge({"erp_sales": {"SN1": "手机"}})   # 单池内已由 take 按六类取过
        self.assertEqual(got["SN1"], "手机")
        # 直接钉池内 rank 的次序
        self.assertGreater(CAT._rank_within("手机"), CAT._rank_within(CAT.OTHER))
        # 池间 rank 次序相反
        self.assertGreater(CAT._rank_between(CAT.OTHER), CAT._rank_between("手机"))

    def test_merge_未知不覆盖认得出的(self):
        got = CAT.merge({"erp_sales": {"SN1": CAT.UNKNOWN}, "lg_stock": {"SN1": "音频"}})
        self.assertEqual(got["SN1"], "音频")

    def test_keep_set_只要六类(self):
        resolved = {"A": "手机", "B": CAT.OTHER, "C": CAT.UNKNOWN, "D": None}
        self.assertEqual(CAT.keep_set(resolved), {"A"})

    def test_summary_把未知词收在原词上(self):
        resolved = {"A": "手机", "B": CAT.OTHER, "C": CAT.UNKNOWN}
        s = CAT.summary(resolved, {"量子传送门": 2})
        self.assertEqual(s["手机"], 1)
        self.assertEqual(s["其它"], 1)
        self.assertEqual(s["未知"], 1)
        self.assertEqual(s["_unknown_words"], {"量子传送门": 2})


# ---------------------------------------------------------------- 连库
SCHEMA = """
CREATE TABLE orders (document_no TEXT PRIMARY KEY, status_name TEXT,
                     return_status INTEGER, refund_status INTEGER, store_name TEXT);
CREATE TABLE order_lines (document_no TEXT, sn TEXT, category_id TEXT, item_name TEXT);
CREATE TABLE lg_stock (snapshot_date TEXT, sn TEXT, category_name TEXT, item_name TEXT,
                       warehouse_name TEXT, stock_age INTEGER);
CREATE TABLE erp_sales (sn TEXT, 单据类型 TEXT, 串号标识 TEXT, 一级分类 TEXT,
                        document_no TEXT, 商品名称 TEXT, 门店 TEXT, 支付时间 TEXT);
CREATE TABLE erp_stock (snapshot_date TEXT, sn TEXT, imei TEXT, sub_imei TEXT,
                        sub_imei1 TEXT, pro_name TEXT, store_name TEXT, ages INTEGER,
                        status TEXT);
"""


def _db(*, with_cat=True):
    """四池的内存库。`with_cat=False` 模拟**没有品类列**的老库 / 精简库。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    if not with_cat:
        for sql in ("ALTER TABLE order_lines DROP COLUMN category_id",
                    "ALTER TABLE lg_stock DROP COLUMN category_name",
                    "ALTER TABLE erp_sales DROP COLUMN 一级分类",
                    "ALTER TABLE erp_stock DROP COLUMN pro_name"):
            conn.execute(sql)
    return conn


def _sold(conn, sn, word, mark="新", kind="零售"):
    conn.execute("INSERT INTO erp_sales (sn, 单据类型, 串号标识, 一级分类) VALUES (?,?,?,?)",
                 (sn, kind, mark, word))


def _stock(conn, sn, word, day="2026-09-21"):
    conn.execute("INSERT INTO erp_stock (snapshot_date, sn, imei, pro_name) VALUES (?,?,?,?)",
                 (day, sn, sn, "%s/华为/某型号" % word))


def _lg_stock(conn, sn, word, day="2026-09-21"):
    conn.execute("INSERT INTO lg_stock (snapshot_date, sn, category_name) VALUES (?,?,?)",
                 (day, sn, word))


def _reported(conn, sn, word, doc=None):
    # ⚠ `orders.document_no` 是主键 —— 每次换一个，否则第二次插就 IntegrityError
    doc = doc or ("D%d" % (conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] + 1))
    conn.execute("INSERT INTO orders (document_no, status_name) VALUES (?, '已完成')", (doc,))
    conn.execute("INSERT INTO order_lines (document_no, sn, category_id) VALUES (?,?,?)",
                 (doc, sn, word))


class Test过滤生效(unittest.TestCase):
    def test_配件不再造出假AD(self):
        """玲珑卖了台**键盘**、云商还挂着 → 旧口径会报成 AD（云商没报）。

        用户口径：配件不参与 ⇒ AD 里不该有它，但**真手机那台要留着**。
        """
        conn = _db()
        _reported(conn, "SN_PHONE", "CMCG10000013")     # 手机
        _stock(conn, "SN_PHONE", "智能手机")
        _reported(conn, "SN_KEY", "HWExclusiveAccessories")   # 键盘（配件）
        _stock(conn, "SN_KEY", "保护壳套")
        self.assertEqual(S.quadrants(conn)["AD"], 1, "手机那台该照常报")

        conn2 = _db()
        _reported(conn2, "SN_KEY", "HWExclusiveAccessories")
        _stock(conn2, "SN_KEY", "保护壳套")
        self.assertEqual(S.quadrants(conn2)["AD"], 0, "配件不该出现在 AD 里")

    def test_音频在玲珑侧不被误判(self):
        """⚠ 本文件最要紧的一条。

        云商卖了耳机、玲珑还挂在库 → 这是**真 BC**。
        而玲珑把「音频系列」归在父类「配件」下 —— 谁要是图省事按父类过滤，
        这台就会被判成"玲珑没有"，BC 里凭空多出一台假差异。
        """
        conn = _db()
        _sold(conn, "SN_EAR", "音频产品")
        _lg_stock(conn, "SN_EAR", "音频系列")
        self.assertEqual(S.quadrants(conn)["BC"], 1)
        keep, stats = S.category_scope(conn)
        self.assertIn("SN_EAR", keep)
        self.assertEqual(stats["音频"], 1)

    def test_穿戴和智慧屏也算六类(self):
        conn = _db()
        _sold(conn, "SN_BAND", "智能穿戴")
        _lg_stock(conn, "SN_BAND", "华为手环")
        _sold(conn, "SN_TV", "智慧屏")
        # ⚠ 玲珑侧**没有**智慧屏这种类目，实测池B 一个智慧屏都没有 ——
        #   所以这里给个**认不出的新词**（UNKNOWN），不是「华为专属配件」(其它)。
        #   2026-09-23 池间改成「其它赢」后，给 OTHER 会把这台真智慧屏剔掉；
        #   而 UNKNOWN 的 rank 最低、不参与池间胜负 ⇒ 池C 说的「智慧屏」(六类) 保留。
        _lg_stock(conn, "SN_TV", "智慧屏系列")
        self.assertEqual(S.quadrants(conn)["BC"], 2)

    def test_对称_只在一侧过滤会造假差异(self):
        """同一台机器，两侧的品类词**都认识** ⇒ 要么都留、要么都去，不会一侧留一侧去。

        这里用同一个串号在两池给出**同一个大类**，结果必须是"留下"。
        """
        conn = _db()
        _reported(conn, "SN1", "CMCG10000020")      # 玲珑：MateBook
        _stock(conn, "SN1", "华为笔记本")            # 云商：笔记本
        keep, _ = S.category_scope(conn)
        self.assertEqual(keep, {"SN1"})
        self.assertEqual(S.quadrants(conn)["AD"], 1)

    def test_一台机器一个品类_云商说了算(self):
        """玲珑认不出（内部编码没登记）时，云商认得出 ⇒ 照样参与。"""
        conn = _db()
        _reported(conn, "SN9", "CMCG99999999")      # 玲珑：没见过的编码
        _stock(conn, "SN9", "智能手机")              # 云商：手机
        keep, stats = S.category_scope(conn)
        self.assertIn("SN9", keep)
        self.assertIn("CMCG99999999", stats["_unknown_words"])


class Test退化不许清空(unittest.TestCase):
    def test_没有品类列时不过滤(self):
        """老库 / 精简库没有品类列 ⇒ **退化成不过滤**，而不是把四池清空。

        清空的表现是"四象限全 0"，看着像库里没数据 —— 本项目最忌讳的那种失败。
        """
        conn = _db(with_cat=False)
        conn.execute("INSERT INTO erp_sales (sn, 单据类型, 串号标识) VALUES "
                     "('SN1','零售','新')")
        conn.execute("INSERT INTO lg_stock (snapshot_date, sn) VALUES ('2026-09-21','SN1')")
        keep, stats = S.category_scope(conn)
        self.assertIsNone(keep, "读不到品类列时必须返回 None（= 不过滤）")
        self.assertIn("_skipped", stats)
        self.assertEqual(S.quadrants(conn)["BC"], 1, "退化时四池要照常出数，不能全 0")

    def test_库是空的也不算退化出错(self):
        conn = _db()
        keep, stats = S.category_scope(conn)
        self.assertIsNone(keep)
        self.assertEqual(S.quadrants(conn)["BC"], 0)


class Test口径要吭声(unittest.TestCase):
    def test_排除掉的量报得出来(self):
        conn = _db()
        _stock(conn, "SN1", "智能手机")
        _stock(conn, "SN2", "保护壳套")
        _keep, stats = S.category_scope(conn)
        self.assertEqual(stats["手机"], 1)
        self.assertEqual(stats["其它"], 1)

    def test_推送文案里带范围说明(self):
        """⚠ 门店看到差异从 18 变 16，得知道是**口径收窄**，不是数据变了。"""
        conn = _db()
        _stock(conn, "SN1", "智能手机")
        _stock(conn, "SN2", "保护壳套")
        text = "\n".join(T.notify_lines(conn))
        self.assertIn("【范围】", text)
        self.assertIn("只对账", text)
        self.assertIn("未纳入", text)

    def test_未知词要在文案里点名(self):
        """不点名就没人去补映射表 —— 新品类会被一直悄悄排掉。"""
        conn = _db()
        _sold(conn, "SN1", "量子传送门")
        _lg_stock(conn, "SN1", "音频系列")
        conn.execute("INSERT INTO orders (document_no, status_name) VALUES ('D9','已完成')")
        text = "\n".join(T.notify_lines(conn))
        self.assertIn("量子传送门", text)

    def test_退化时文案里也说明(self):
        conn = _db(with_cat=False)
        conn.execute("INSERT INTO erp_sales (sn, 单据类型, 串号标识) VALUES ('SN1','零售','新')")
        text = "\n".join(T.notify_lines(conn))
        self.assertIn("未按品类过滤", text)


if __name__ == "__main__":
    unittest.main()
