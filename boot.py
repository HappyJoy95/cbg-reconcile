#!/usr/bin/env python3
"""开机自启 / 后台启动的入口。

由「设置 → 后台服务 → 开机自动启动」注册（Windows 注册表 / macOS LaunchAgent /
Linux autostart）。**不弹任何窗口** —— Windows 上用 pythonw.exe 跑。

为什么不直接在注册表里写 `python -m src.cli serve`：
Windows 的 Run 键**不设工作目录**，`-m src.cli` 会找不到模块。所以走这个脚本，
它自己 chdir 到项目根，跟从哪个目录被拉起来无关。
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
(ROOT / "out").mkdir(exist_ok=True)          # 日志要写这儿，先建好

if __name__ == "__main__":
    try:
        from src.cli import main
    except ImportError as e:
        # 这种时候没有控制台可打印，只能记日志
        with open(ROOT / "out" / "autostart.log", "a", encoding="utf-8") as f:
            f.write(f"[自启失败] 导入 src 失败：{e}\n"
                    f"  项目目录：{ROOT}\n"
                    f"  解释器：{sys.executable}\n")
        raise SystemExit(1)

    sys.exit(main(["serve", "--no-open"]))
