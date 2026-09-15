# -*- coding: utf-8 -*-
"""更新失败的现场诊断 —— 放在 cbg-reconcile 目录下运行：

    python update-debug.py

它**只在下载之后、临时目录被删之前**做检查 —— 那正是自更新失败时错过的窗口。
不改任何东西，看完可以直接把这个文件删掉。

什么时候需要它：控制台点「立即更新」报「下载下来的包里缺少 src/cli.py」
（或类似"这不像我们的包"）的时候。把输出整个发回来。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import selfupdate as su                                  # noqa: E402


def head(title):
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


head("环境")
print(f"  Python {sys.version.split()[0]}   {sys.platform}   os.name={os.name}")
try:
    from src import version
    print(f"  本地版本：v{version.VERSION}")
except Exception:                                                 # noqa: BLE001
    pass

head("下载 zipball（和自更新同一个地址）")
try:
    zip_root = su.download(timeout=120)
except Exception as e:                                            # noqa: BLE001
    print(f"  ✗ 下载失败：{type(e).__name__}: {e}")
    sys.exit(1)
print(f"  ✓ 解到 {zip_root}")

head("关键对照：_targets() vs 直接遍历")
pairs = su._targets(zip_root)
targets = {str(r) for _, r in pairs}
all_files = [p for p in zip_root.rglob("*") if p.is_file()]
rglob_rel = {p.relative_to(zip_root) for p in all_files}

print(f"  _targets()  返回：{len(targets)} 个")
print(f"  rglob 遍历到：{len(rglob_rel)} 个")

diff = rglob_rel - {Path(r) for r in targets}
print(f"  ⚠ 遍历到、但 _targets 没带回的（{len(diff)} 个）：")
for r in sorted(str(x) for x in diff):
    p = zip_root / r
    print(f"      {r}")
    print(f"        exists={p.exists()} is_file={p.is_file()} "
          f"is_symlink={p.is_symlink()}")
    try:
        st = p.stat()
        print(f"        size={st.st_size} mode={oct(st.st_mode)}")
    except OSError as e:
        print(f"        stat 失败：{e}")

head("锚点逐个查（防御代码问的就是这些）")
for a, _desc in su.ANCHORS:
    p = zip_root / a
    print(f"  {a}")
    print(f"    is_file()      = {p.is_file()}")
    print(f"    exists()       = {p.exists()}")
    print(f"    is_symlink()   = {p.is_symlink()}")
    print(f"    resolve()      = {p.resolve()}")
    print(f"    在 _targets 里 = {a in targets}")
    try:
        print(f"    读前 20 字节   = {p.read_bytes()[:20]!r}")
    except OSError as e:
        print(f"    读失败         = {e}")

head("src/ 目录本身")
src = zip_root / "src"
print(f"  is_dir={src.is_dir()} is_symlink={src.is_symlink()} resolve={src.resolve()}")
try:
    items = sorted(src.iterdir())
    print(f"  共 {len(items)} 项：")
    for x in items:
        print(f"    {x.name:26} is_file={x.is_file()} is_symlink={x.is_symlink()}")
except OSError as e:
    print(f"  列目录失败：{e}")

head("照 _targets 的过滤逻辑走一遍，看谁被跳过")
skipped = []
for f in sorted(zip_root.rglob("*")):
    try:
        rel = f.relative_to(zip_root)
    except ValueError:
        continue
    if not f.is_file():
        skipped.append((str(rel), "is_file()=False"))
        continue
    rels = str(rel)
    if rels not in su.ALLOW_EVEN_IF_NEVER and (
            rel.parts[0] in su.NEVER_TOUCH or "__pycache__" in rel.parts):
        skipped.append((rels, f"NEVER_TOUCH 命中 parts[0]={rel.parts[0]!r}"))
        continue
    if rel.name.startswith(".") and rel.name not in (".gitattributes", ".gitignore"):
        skipped.append((rels, "隐藏文件"))
print(f"  NEVER_TOUCH = {su.NEVER_TOUCH}")
print(f"  被跳过 {len(skipped)} 项：")
for rel, why in skipped:
    print(f"    {rel:44} ← {why}")

print()
print("诊断结束 —— 请把以上全部内容发回来。")
