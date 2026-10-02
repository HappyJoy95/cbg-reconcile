"""打包成 exe 之后的入口 —— 等价于 `python -m src.cli`。

**为什么不直接拿 `src/cli.py` 当入口**：PyInstaller 要一个能分析出依赖的
入口文件，而 `src/cli.py` 里有 `if __name__ == "__main__"` 但作为模块
被分析时不会走到那行。这个薄壳只做一件事：把控制权交给 `src.cli.main`。

⚠ **`sys.argv` 不用动**：`sys.argv[0]` 在 exe 里是 exe 的路径，
后面就是用户传的参数，`argparse` 拿 `argv=None` 时读的正是它 ——
跟 `python -m src.cli serve --port 9000` 是同一个形状。
"""

import sys

from src.cli import main

if __name__ == "__main__":
    sys.exit(main())
