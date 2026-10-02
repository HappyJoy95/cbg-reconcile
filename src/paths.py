"""项目根 —— 全项目**唯一**一处知道"自己在第几层"的文件。

**为什么要有这个文件**（2026-09-19 实测）：`Path(__file__).resolve().parent.parent`
这句话把"我这个文件在 `<根>/src/` 下面"**写死了**，而 `src/` 下**八处**各写了一遍
（`cli` / `dump` / `elevate` / `envfile` / `erp` / `runtime` / `version` / `web`）。

把某个模块搬进子目录时（比如 `pools.py` → `storage/pools.py`），深度一变，
那句话算出来的是 **`src/` 而不是项目根** —— `out/` 变成 `src/out/`、
`.secrets/` 变成 `src/.secrets/`：**门店配置、华为会话、历史报告全部"找不到"，
而且不报错**，程序会在 `src/` 下面新建一套空目录照常跑起来。
这种"静默换根"比启动失败难查得多。

所以**搬文件之前**必须先把这句话收到一处（M12 / 阶段 1.5）。

⚠ **两条规矩，都用测试钉着**（`tests/test_paths.py`）：

1. **不许 import 任何项目内模块** —— `src/version.py` 要 import 它，而 version
   是自更新的硬依赖（`selfupdate.ANCHORS` + 两个远端固定路径）。它 import 谁，
   谁就变成**部署契约**的一部分：这个文件一旦出问题，"检查更新"这条
   最不该失败的路就跟着失败。只用标准库。
2. **这个文件自己永远不许移动** —— 它的 `parent.parent` 就是全项目的深度基准。
   搬别的文件没事，搬它等于把根挪走。与 `src/cli.py` / `src/version.py` /
   `bootstrap.py` 同级待遇（见开发目标 4.3 红线）。

用法：`from .paths import ROOT`。

⚠ 必须用 `from … import ROOT` 这种写法，**不要**改成满项目 `paths.ROOT`：
有 12 处测试是 `mock.patch.object(cli, "ROOT", 临时目录)` 这种按**模块属性**打补丁的，
`from … import` 让每个模块仍然有自己的 `ROOT` 名字，那些测试一行都不用改。

⚠ **打包成 exe 之后（2026-10-02）根不一样**：PyInstaller 把 `src/` 编进
可执行文件，`__file__` 指向**归档内部**的假路径 —— 那时 `.parent.parent`
算出来的是临时/内部目录，`config/`、`.secrets/`、`out/` 会**静默建到那儿去**
（正是上面说的"静默换根"，这次是打包给的）。所以冻结时改认
**exe 所在的目录**（onedir 布局里 `web/`、`config/` 就在它旁边）。
这个判断只能写成**一句赋值** —— `tests/test_paths.py::test_里面只有一句求值`
盯着，别在里面加函数。
"""

from pathlib import Path
import sys

ROOT = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent.parent)

