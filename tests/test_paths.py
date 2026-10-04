"""项目根收口（M12 / 阶段 1.5）—— 三条规矩，用 `ast` 钉住，不靠自觉。

背景：`Path(__file__).resolve().parent.parent` 写死了"我在 `<根>/src/` 下一层"。
2026-09-19 实测 `src/` 下有**八处**各写一遍；搬文件（`pools.py` → `storage/pools.py`）
会让它们**静默算错根**（`out/` → `src/out/`，不报错）。收口到 `src/paths.py`。

⚠ 这个文件里的断言是**结构约束**，不是风格偏好：
`tests/` 下自己推根**不管**（它们不随门店发布、也不在 4.5.5 的迁移路径上），
禁的是 `src/` —— 那才是会被搬来搬去的那一半。
"""

import ast
import unittest
from pathlib import Path

from src import paths

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PATHS = SRC / "paths.py"

#: `paths.py` 只许 import 这些（标准库，且是"够用就好"的最小集）
ALLOWED_IMPORTS = {"pathlib", "os", "sys"}


def _parse(py: Path) -> ast.Module:
    return ast.parse(py.read_text(encoding="utf-8"), filename=str(py))


def _src_files():
    """`src/` 下所有 .py（含以后新加的子目录）。"""
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _depth_usages(py: Path) -> list:
    """这个文件里"猜自己在第几层"的写法，返回 `["第 12 行：.parent.parent"]`。"""
    out = []
    for node in ast.walk(_parse(py)):
        if (isinstance(node, ast.Attribute) and node.attr == "parent"
                and isinstance(node.value, ast.Attribute) and node.value.attr == "parent"):
            out.append(f"第 {node.lineno} 行：.parent.parent")
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents"):
            out.append(f"第 {node.lineno} 行：.parents[...]")
        elif isinstance(node, ast.Call) and _is_nested_dirname(node):
            out.append(f"第 {node.lineno} 行：os.path.dirname(os.path.dirname(...))")
    return out


def _is_nested_dirname(call: ast.Call) -> bool:
    """`os.path.dirname(os.path.dirname(__file__))` —— 同一个坑的另一张皮。"""
    fn = call.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == "dirname"):
        return False
    return any(isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
               and a.func.attr == "dirname" for a in call.args)


class TestPaths模块本身(unittest.TestCase):
    def test_算出来就是仓库根(self):
        """不能只对 `Path(__file__)` 求值 —— 要对着**磁盘上的事实**验。"""
        self.assertEqual(paths.ROOT, ROOT)
        self.assertTrue((paths.ROOT / "src" / "cli.py").is_file())
        self.assertTrue((paths.ROOT / "bootstrap.py").is_file())
        self.assertTrue((paths.ROOT / "web" / "index.html").is_file())

    def test_自己在_src_下一层(self):
        """它就是全项目的深度基准 —— 挪了它，所有人的根一起错。"""
        self.assertEqual(PATHS.parent, SRC)

    def test_不依赖任何项目内模块(self):
        """`version.py` 要 import 它 ⇒ 它进部署契约 ⇒ 只许标准库。"""
        bad = []
        for node in ast.walk(_parse(PATHS)):
            if isinstance(node, ast.ImportFrom):
                if node.level:                       # `from . import x` / `from .x import y`
                    bad.append(f"第 {node.lineno} 行：相对 import")
                elif (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                    bad.append(f"第 {node.lineno} 行：import {node.module}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                        bad.append(f"第 {node.lineno} 行：import {a.name}")
        self.assertEqual(bad, [], "paths.py 只许用标准库（" + ", ".join(sorted(ALLOWED_IMPORTS)) + "）")

    def test_里面只有一句求值(self):
        """防止有人在里面顺手加"读配置 / 建目录"这类有失败模式的代码。"""
        body = [n for n in _parse(PATHS).body
                if not isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr))]
        self.assertEqual([type(n).__name__ for n in body], ["Assign"],
                         "paths.py 里除了 import 和文档，只该有一个 ROOT 赋值")


class Test只有一处知道深度(unittest.TestCase):
    def test_src下不许再出现parent_parent(self):
        bad = []
        for py in _src_files():
            if py == PATHS:
                continue
            bad += [f"{py.relative_to(ROOT)} {u}" for u in _depth_usages(py)]
        self.assertEqual(bad, [], "把项目根从 src/paths.py 里取，别再自己推：\n  "
                                  + "\n  ".join(bad))

    def test_八个调用点都改成从_paths_取(self):
        """⚠ 这条是**反向**断言：光禁掉 `parent.parent` 还不够 ——
        有人可能改写成 `src` 之外的别的算法。八个模块必须都真的 import 了它。"""
        want = {
            "cli.py": ("src/cli.py", "from .paths import"),
            "dump.py": ("src/dump.py", "from .paths import"),
            "elevate.py": ("src/desktop/elevate.py", "from ..paths import"),
            "envfile.py": ("src/envfile.py", "from .paths import"),
            "erp.py": ("src/integrations/erp.py", "from ..paths import"),
            "runtime.py": ("src/desktop/runtime.py", "from ..paths import"),
            "version.py": ("src/version.py", "from .paths import"),
            "web.py": ("src/http/app.py", "from ..paths import"),
        }
        missing = []
        for name, (rel, needle) in want.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            if needle not in text:
                missing.append(rel)
        self.assertEqual(missing, [], "这些实现没有从 src/paths.py 取根：" + ", ".join(missing))


class Test根脚本不许挪(unittest.TestCase):
    """.bat 是**按名字**调它们的（`selftest.bat` → `bootstrap.py`），
    所以它们的位置是部署契约，和 `ANCHORS` 一个性质。"""

    def test_三个根脚本仍在根目录(self):
        for name in ("bootstrap.py", "boot.py", "run_check.py"):
            self.assertTrue((ROOT / name).is_file(), f"{name} 不在项目根 —— .bat 会调不到")

    def test_bat_里引用的还是这些名字(self):
        """.bat 是纯 ASCII，只能写文件名 —— 改成子目录路径它们就废了。"""
        for name in ("install.bat", "start.bat", "selftest.bat"):
            text = (ROOT / name).read_text(encoding="ascii")
            self.assertIn("bootstrap.py", text, f"{name} 没在调 bootstrap.py")
