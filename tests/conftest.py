"""pytest 全局配置 —— **别删这个文件**。

⚠ `CBG_NO_DB_REBUILD` 是**保命用的**：`src/dbmigrate.py` 会在 `daily` 开头
把 `out/cbg-<年>.db` **改名**（2.1.0 的一次性库重建）。门槛靠 `BUILD.txt`，
而开发机上项目根**可能正好存在**一个同名文件（测试/脚本写的）——
2026-09-17 就这么把开发机的 77MB 真库改过名。

设上这个环境变量之后，**任何测试都不可能触发那一步**。
（那次能救回来，是因为它是"改名"不是"删"——见 `dbmigrate` 顶部。）
"""

import os

os.environ.setdefault("CBG_NO_DB_REBUILD", "1")
