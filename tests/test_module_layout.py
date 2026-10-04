"""模块布局与依赖方向 —— **规则要钉住，不能只写在文档里**。

阶段 2（M13）按 `开发目标 §4.5.5` 的七步搬文件，这是**第 1 步**：
`pos_metric` / `pos_report` / `pos_export` → `src/features/compliance/pos/`。

⚠ 为什么这类测试值得写：搬文件本身谁都会，**难的是"搬完之后规则还在"** ——
`features/*` 一旦反过来 import `cli`/`web`，就回到了"改一个功能要动入口层"的老路，
而这种倒退**不会报错**，只会在下一次改需求时以"怎么又要改这么多文件"的形式出现。
"""

import ast
import os
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

#: 只许 import 标准库的"纯内核" —— 口径住在这里，能脱离 IO 单测
PURE_KERNEL = ("features/compliance/pos/pos_metric.py", "reconcile.py")

#: `features/*` 里**不许**出现的模块（入口层 / HTTP 层 / CLI）
FORBIDDEN_IN_FEATURES = {"cli", "web", "startup", "runner"}


def _parse(py: Path) -> ast.Module:
    return ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


def _imported_modules(py: Path) -> list:
    """这个文件 import 了谁：相对 import 返回模块名（`from . import x` → `x`）。"""
    out = []
    for node in ast.walk(_parse(py)):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out += [(base + "." + a.name).strip(".") for a in node.names]
    return out


class TestPOS模块搬到了features里(unittest.TestCase):
    def test_三个模块在新位置(self):
        for name in ("pos_metric.py", "pos_report.py", "pos_export.py"):
            self.assertTrue((SRC / "features" / "compliance" / "pos" / name).is_file(),
                            f"src/features/compliance/pos/{name} 不在 —— 第 1 步搬过去之后就别搬回来")

    def test_老位置不留壳(self):
        """⚠ 留壳 = 门店升级后**两份实现共存**：旧的没被清理、
        新代码却 import 新的那份，谁在跑说不清（1.4c 的清理规则就是为这个）。"""
        left = sorted(p.name for p in SRC.glob("pos_*.py"))
        self.assertEqual(left, [], f"src/ 下还留着老文件：{left}")

    def test_搬过去之后内部相对import还是对的(self):
        """三个模块一起搬，所以它们之间的 `from . import pos_metric` **一个字都不用改** ——
        这条就是在钉这件事（哪天有人把它改成绝对路径，这里会红）。"""
        text = (SRC / "features" / "compliance" / "pos" / "pos_report.py").read_text(encoding="utf-8")
        self.assertIn("from . import pos_metric", text)


class Test纯内核只依赖标准库(unittest.TestCase):
    """`pos_metric` / `reconcile` 是**口径**所在 —— 只 import 标准库，
    这样"算法对不对"能脱离数据库和网络直接单测（门店侧那 33 条就是这么来的）。
    """

    def _classify(self, mod: str) -> str:
        """`"stdlib"` / `"project"` / `"third"` / `"unknown"`。

        ⚠ 三个坑都是实测踩的：
        ① **别写死一份标准库名单** —— 第一版那么写，漏了 `dataclasses`，3.8 上假红
           （`sys.stdlib_module_names` 要 3.10+，老机器只能走兜底名单）。
        ② **别拿 `sysconfig` 的 stdlib 路径去比** —— Homebrew 那个是软链
           （`/opt/homebrew/opt/...`），`find_spec` 给的是解析后的真实路径
           （`/opt/homebrew/Cellar/...`），`startswith` 永远不成立。
        ③ ⚠⚠ **更不能拿"在不在项目根下"当判据** —— 3.8 那个解释器是 uv 装的，
           就落在 `.dsh/uv-python/cpython-3.8-…/`，**在项目目录里**：
           那样判会把 `dataclasses` 当成"我们自己的模块"（3.8 红、3.14 绿）。
        ⇒ 顺序：site-packages 里的是第三方；**解释器自己那棵树**（`sys.base_prefix` /
           `sys.prefix`）里的是标准库；剩下才是我们的代码。
        """
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError):
            return "unknown"
        origin = (getattr(spec, "origin", "") or "").replace("\\", "/")
        if not origin or origin in ("built-in", "frozen"):
            return "stdlib"
        if "site-packages" in origin or "dist-packages" in origin:
            return "third"
        # ⚠ 两边都要 `realpath`：Homebrew 的 `sys.base_prefix` 是软链
        #   （`/opt/homebrew/opt/…`），而 `find_spec` 给的是展开后的 `Cellar` 路径
        for prefix in (sys.base_prefix, sys.prefix):
            if not prefix:
                continue
            base = os.path.realpath(prefix).replace("\\", "/").rstrip("/")
            if origin.startswith(base + "/") or os.path.realpath(origin).startswith(base + "/"):
                return "stdlib"
        return "project"

    def test_只import标准库(self):
        bad = []
        for rel in PURE_KERNEL:
            py = SRC / rel
            self.assertTrue(py.is_file(), f"{rel} 不在了？")
            for mod in _imported_modules(py):
                head = mod.split(".")[0]
                if head == "__future__":
                    continue
                kind = self._classify(head)
                if kind in ("project", "third"):
                    bad.append(f"{rel} → {mod}（{kind}）")
        self.assertEqual(bad, [], "纯内核里出现了非标准库依赖（口径就不再能单独测了）："
                         + ", ".join(bad))


class Test依赖方向(unittest.TestCase):
    """`开发目标 §4.5.4`：依赖只能朝着"入口 → 编排 → 业务 → 集成 → 存储"流。

    ⚠ 现在只钉**已经成立**的那两条（`features/*` 和 `app/*` 都不碰入口层）——
    别的方向等对应的搬迁做完再加，不然一上来就一片红，测试就废了。
    """

    def test_features不许import入口层(self):
        bad = []
        for py in sorted((SRC / "features").rglob("*.py")):
            for mod in _imported_modules(py):
                head = mod.split(".")[0]
                if head in FORBIDDEN_IN_FEATURES:
                    bad.append(f"{py.relative_to(ROOT)} → {mod}")
        self.assertEqual(bad, [], "业务模块反过来依赖入口层了（改一个功能就要动 CLI/HTTP）："
                         + "; ".join(bad))

    def test_app不许import入口层(self):
        """⚠ 这是**阶段 2 试点的验收条件**：执行模块（`app/pos.py`）一旦
        import 了 `cli` / `web`，"三个入口调同一个模块"就名存实亡 ——
        它会顺着 CLI 把 argparse、退出码、`sys.argv` 一起拖进业务逻辑里。"""
        bad = []
        for py in sorted((SRC / "app").rglob("*.py")):
            for mod in _imported_modules(py):
                head = mod.split(".")[0]
                if head in FORBIDDEN_IN_FEATURES:
                    bad.append(f"{py.relative_to(ROOT)} → {mod}")
        self.assertEqual(bad, [], "执行模块依赖入口层了（业务就不止一份了）："
                         + "; ".join(bad))

    def test_app里的业务模块都在(self):
        self.assertTrue((SRC / "app" / "pos.py").is_file(),
                        "POS 的执行模块不在了 —— 三个入口是不是又各算各的了？")

    def test_四池按规则读库导出文案分开了(self):
        """M15 / 阶段 3.5：双平台对比那摊按 POS 那套分工拆开。

        ⚠ 四件事各自成模块，而且 **`rules.py` 必须只 import 标准库** ——
        口径要能脱离数据库单测（`combine()` 就是这么用的）。
        """
        comp = SRC / "features" / "compliance" / "comparison"
        for name in ("__init__.py", "rules.py", "store.py", "export.py", "text.py"):
            self.assertTrue((comp / name).is_file(), f"features/compliance/comparison/{name} 不在")
        rules = comp / "rules.py"
        for mod in _imported_modules(rules):
            head = mod.split(".")[0]
            self.assertIn(head, ("__future__", "re", "typing", "os", "sys", "datetime"),
                          "规则层出现了非标准库依赖：%s（口径就不再能单独测了）" % mod)

    def test_四池的壳还在而且名字齐(self):
        """⚠ `pools.py` 是**部署契约**：`cli.py` 二十多处 `P.xxx`、`pools_notify`、
        `pools_history`、一大批测试都 import 它。拆实现可以，**断引用不行**。"""
        from src import pools
        for name in ("POOLS", "POOL_LABELS", "SCHEMA", "ensure", "save_snapshot",
                     "save_sales", "purge_snapshots", "snapshots", "latest",
                     "quadrants", "details", "status", "notify_lines", "export_xlsx",
                     "is_sample_marker", "pool_colname", "pool_row_from",
                     "_latest_sn_set", "_reported_sns", "_sold_sns", "_table_cols"):
            self.assertTrue(hasattr(pools, name), "pools.%s 不见了" % name)

    def test_纯规则能脱离库单测(self):
        """`combine()` 不碰库 —— 随便造四个集合就能验四象限口径。"""
        from src.features.compliance.comparison import rules
        out = rules.combine({"a1"}, {"b1"}, {"c1"}, {"s1"}, {"d1"})
        self.assertEqual(out["AC"], {"a1"} & {"c1"})
        self.assertEqual(out["BC_样机"], {"b1"} & {"s1"})
        self.assertEqual(out["only_A"], {"a1"})
        self.assertEqual(out["all"], {"a1", "b1", "c1", "s1", "d1"},
                         "算「只在谁那儿」时样机也算池 C")

    def test_startup不反向依赖web(self):
        """阶段 2 的 **2.3**：启动刷新（`startup.py`）**不许** import HTTP 层。

        ⚠ 这条是"业务执行从 CLI / HTTP 抽出"最容易漏的一角 ——
        它是**反向**的依赖（底下的模块反过来 import 上面的），
        在 import 图里不报错，只会在"换个入口复用不了"的时候才发作。
        """
        heads = [m.split(".")[0] for m in _imported_modules(SRC / "startup.py")]
        self.assertNotIn("web", heads,
                         "startup 又 import web 了 —— 人员刷新该走 features/store/staff.py")
        self.assertNotIn("cli", heads)
        # ⚠ 2026-09-19：人员从 `app/staff.py` 挪到 `features/store/staff.py`（用户规划：
        #   门店身份/人员属于 features）。这条断言跟着改，**钉的东西没变**：
        #   startup 只能依赖"业务模块"，不能依赖入口层。
        self.assertIn("features", heads, "人员刷新应当调业务模块（features/store/staff.py）")

    def test_每个模块里不许有重名的顶层定义(self):
        """⚠⚠ 模块被"劈成两份"是**静默**的：Python 取**最后一个**定义，
        于是新代码被旧代码盖掉，而**另外两个解释器可能照样全绿**。

        真踩过（2026-09-19）：批量改 `dump.py` 时我用
        `s.index("_COLS_CACHE")` / `s.index("def jd")` 两头一切 —— 而 `jd` 其实
        排在 `_COLS_CACHE` **前面** ⇒ 切片反了 ⇒ `jd` 到 `ensure_columns` 那 161 行
        **原地复制了一份**。2.9 万行里多 161 行，`grep` 不出来、diff 也不显眼；
        3.9 / 3.14 全绿，只有 3.8 因为 `sqlite3` 事务行为不同才炸出来。

        ⇒ 这条测试就是那道"以后别再劈"的闸。
        """
        bad = []
        for py in sorted(SRC.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            seen = {}
            for node in _parse(py).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in seen:
                        bad.append("%s：%s（第 %d 行与第 %d 行）"
                                   % (py.relative_to(ROOT), node.name,
                                      seen[node.name], node.lineno))
                    seen[node.name] = node.lineno
        self.assertEqual(bad, [], "模块里有重名的顶层定义（后一个会盖掉前一个）：\n  "
                         + "\n  ".join(bad))

    def test_新包的init都在(self):
        """子包要有 `__init__.py`（跟 `src/` 一个风格）——
        别指望命名空间包，门店的 3.8 上行为差异不值得赌。"""
        for pkg in ("features", "features/compliance", "features/compliance/pos", "app"):
            self.assertTrue((SRC / pkg / "__init__.py").is_file(),
                            f"src/{pkg}/__init__.py 不在")


class Test运行支撑模块迁移兼容(unittest.TestCase):
    """运行支撑实现搬入目录后，旧导入路径必须是新模块本身。

    只转发几个公开名字会让旧测试替身、模块级补丁和共享单例悄悄分叉；
    因此兼容入口必须与新路径解析到同一个 module object。
    """

    MOVES = (
        ("src.erp", "src.integrations.erp"),
        ("src.erp_stub", "src.integrations.erp_stub"),
        ("src.cbg", "src.integrations.cbg"),
        ("src.browser", "src.integrations.browser"),
        ("src.cdp", "src.integrations.cdp"),
        ("src.tdoc", "src.integrations.tdoc"),
        ("src.pmall", "src.integrations.pmall"),
        ("src.mailer", "src.integrations.mailer"),
        ("src.wecom", "src.integrations.wecom"),
        ("src.autostart", "src.desktop.autostart"),
        ("src.elevate", "src.desktop.elevate"),
        ("src.runtime", "src.desktop.runtime"),
        ("src.schedule", "src.desktop.schedule"),
        ("src.service", "src.desktop.service"),
        ("src.winutil", "src.desktop.winutil"),
        ("src.runner", "src.desktop.runner"),
        ("src.web", "src.http"),
    )

    def test_所有旧路径与新路径是同一个模块对象(self):
        import importlib

        for old_name, new_name in self.MOVES:
            with self.subTest(old=old_name, new=new_name):
                old = importlib.import_module(old_name)
                new = importlib.import_module(new_name)
                self.assertIs(old, new, "%s 与 %s 不是同一个模块对象" %
                              (old_name, new_name))

    def test_旧路径上的模块级补丁能从新路径看到(self):
        import importlib

        old = importlib.import_module("src.erp")
        new = importlib.import_module("src.integrations.erp")
        marker = object()
        had = hasattr(old, "_migration_patch_probe")
        previous = getattr(old, "_migration_patch_probe", None)
        try:
            old._migration_patch_probe = marker
            self.assertIs(new._migration_patch_probe, marker)
        finally:
            if had:
                old._migration_patch_probe = previous
            else:
                del old._migration_patch_probe

    def test_web兼容路径的补丁也进入HTTP实现命名空间(self):
        import importlib

        web = importlib.import_module("src.web")
        http = importlib.import_module("src.http")
        implementation = importlib.import_module("src.http.app")
        original = web.role_scope
        replacement = lambda _app: {"role": "migration-probe"}
        try:
            web.role_scope = replacement
            self.assertIs(http.role_scope, replacement)
            self.assertIs(implementation.role_scope, replacement)
        finally:
            web.role_scope = original

    def test_HTTP路由策略集中在独立模块并由旧入口公开(self):
        import importlib

        web = importlib.import_module("src.web")
        policy = importlib.import_module("src.http.policy")
        self.assertIs(web._build_page_rules, policy.build_page_rules)
        self.assertIs(web._build_perm_rules, policy.build_perm_rules)
