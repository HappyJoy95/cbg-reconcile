"""`key=value` 凭据文件的读写。

云商账号、邮箱密码都用这套 —— 两处都要满足同样的三条：
  1. 定点替换，**保留注释**（注释就是操作手册）
  2. 文件权限 600
  3. 相对路径按**项目根**解析，不按 cwd（门店电脑上可能从别处启动）
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def resolve(path, root: Path | None = None) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (root or _ROOT) / p


def parse(path) -> dict:
    out: dict = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def update(path, updates: dict, *, secure: bool = True) -> Path:
    """把 updates 写进文件。**传 None 的键不动**；传空串就是真的清空。"""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    todo = {k: v for k, v in updates.items() if v is not None}

    written = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z_][\w]*)\s*=", line)
        if not m or m.group(1) not in todo:
            continue
        key = m.group(1)
        cm = re.search(r"\s+#", line)                       # 保住行尾注释
        lines[i] = f"{key}={todo[key]}" + (line[cm.start():] if cm else "")
        written.add(key)

    for key, val in todo.items():
        if key not in written:
            lines.append(f"{key}={val}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    if secure:
        os.chmod(path, 0o600)
    return path
