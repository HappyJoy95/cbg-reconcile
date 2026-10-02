# -*- mode: python ; coding: utf-8 -*-
"""打包成 Windows 可执行程序（onedir）。

**为什么是 onedir 不是 onefile**

* onefile 每次启动都要把几十 MB 解到临时目录（门店那台机器上 5~15 秒），
  而且解出来的临时目录会变成"运行目录" —— `config/`、`.secrets/`、`out/`
  这些**要持久**的东西跟着一起跑到临时目录里，重启就没了。
* onedir 就是一个文件夹 + 一个 exe，**跟现在的安装形态一模一样**：
  `ROOT` = exe 所在的目录，`web/`、`config/`、`.secrets/`、`out/` 全在旁边。
  自更新、门店手工覆盖、`install.bat` 那套心智模型都不用改。

**PyInstaller 5.x 的 onedir 布局是平的**（exe 旁边直接是数据文件，
没有 6.x 那个 `_internal/` 子目录）—— 这正是我们要的：
`ROOT / "web"`、`ROOT / "config"` 直接成立，`paths.ROOT` 只需一行判断。
⚠ 别随手升到 6.x：6.x 默认多一层 `_internal/`，`paths.ROOT` 和
`selfupdate.ANCHORS` 都得跟着改（`contents_directory` 能摊平，但那是另一个坑）。

**数据文件**：只放运行时真要读的。`tests/`、`tools/`、`.secrets/`、
`config/store-*.yaml`（别的机器的门店配置）一个都不进 ——
CI 上是干净 checkout，这些本来就不在。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

# ⚠ 路径一律**拼成绝对的**：spec 里给相对路径时，入口按 **spec 文件所在目录**
#   解析（于是 `tools/exe_entry.py` 变成 `tools/tools/exe_entry.py`，第一次跑就撞了），
#   数据文件按 cwd 解析 —— 两套基准，谁在哪个目录下跑都能出岔子。
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))


def res(rel):
    return os.path.join(ROOT, rel)


# ⚠ `src/features/registry.py` 是**运行时反射**加载功能模块的
#   （`importlib.import_module`），PyInstaller 的静态分析看不见 ——
#   少了它，打包能过、跑起来才在"点开某个功能页"时炸。
#   整个 `src` 包一把抓最省事，代价是包大一点（几百 KB）。
hidden = collect_submodules("src")

datas = [
    (res("web"), "web"),                             # 控制台前端（唯一真正的大头）
    (res("src/store-config.default.yaml"), "src"),   # 门店配置模板（bootstrap 要读）
    (res("requirements.txt"), "."),                  # 给人看的：依赖是哪些
]

# ⚠⚠ `config/` **一个文件一个文件地挑**，别整目录拷 ——
#   整目录拷会把**这台机器自己的** `config/store-SCN231409.yaml` 带进包
#   （2026-10-02 本机试跑当场被 `build_exe.sh` 的自检逮住）。
#   那是门店配置，进包 = 装到别的店会**静默对到别家账上去**。
#   `store-*.yaml` 里只有 `stores.yaml` 是随程序走的门店映射表，其余全是机器自己的。
_cfg = os.path.join(ROOT, "config")
for _name in sorted(os.listdir(_cfg)):
    if _name.startswith("store-"):
        continue
    _path = os.path.join(_cfg, _name)
    if os.path.isfile(_path):
        datas.append((_path, "config"))

for _name in ("门店操作手册.md", "发布说明.md"):
    if os.path.isfile(res(_name)):
        datas.append((res(_name), "."))

# ⚠ `selftest` 第 0 节用 `importlib.metadata.version("requests"…)` 报依赖版本 ——
#   不把 dist-info 带进来，冻结后一律显示「⚠️ 没装」，而程序其实跑得起来
#   （第一次打出来就是这个：三个包全报没装）。
for _pkg in ("requests", "PyYAML", "openpyxl"):
    try:
        datas += copy_metadata(_pkg)
    except Exception:                                  # noqa: BLE001
        pass

# ⚠ 构建指纹 —— `version.build_id()` 读它，门店判断"我跑的是哪一版"全靠这行。
#   路径由 `tools/build_exe.sh` 通过 `CBG_BUILD_FILE` 指到**产物目录**，
#   别让它落在仓库根：那会污染开发机的 `version.build_id()`，
#   还会让 `dbmigrate` 那道"不是发版包就别动库"的门槛误判。
_build = os.environ.get("CBG_BUILD_FILE") or res("BUILD.txt")
if os.path.isfile(_build):
    # ⚠ 第二项是**放到哪个目录**，不是目标文件名 —— 写成 "BUILD.txt" 会在包里
    #   建出一个**叫 BUILD.txt 的目录**（`cat: Is a directory`），而
    #   `version.build_id()` 按文件读它 ⇒ 指纹丢失、退回"BUILD.txt 不在"。
    datas.append((_build, "."))

a = Analysis(
    [res("tools/exe_entry.py")],
    pathex=[ROOT],        # ⚠ 入口在 tools/ 下，不给 ROOT 的话 `import src` 找不到
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "tkinter"],
    # ⚠ `unittest` **不能排除** —— `cli selftest` 用它跑测试发现
    #   （第一次打出来就是这么炸的：`No module named 'unittest'`）。
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cbg-reconcile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # 门店杀软对 upx 压缩的 exe 特别敏感，别冒这个险
    console=True,          # 计划任务 / cmd 里要能看到输出
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cbg-reconcile",
)
