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

# ⚠ 本仓库（lifehall 分支）的 `EDITION` 文件写着 lifehall，但**既有 2810 条
#   测试全是按主包（full）写的** —— 测试默认钉 full，验生活馆行为的测试显式
#   patch env 再 `edition.reload()`（见 tests/test_edition.py）。
#   生产机器上没有这个 env，读到的就是 `EDITION` 文件 —— 两条路互不打扰。
os.environ.setdefault("CBG_EDITION", "full")


# ─────────────────────── 测试**不许往项目根写东西** ───────────────────────
#
# ⚠ 这条是**用真金白银换来的**（2026-09-19 一天里踩了三次）：
#   `runlog.record(root=None)` / `attain.run(root=None)` 这些的 `root=None`
#   = **项目根**（那是给生产用的默认值）。测试里忘了传临时 root，
#   就会往开发机的 `out/` 里写：跑过 28 行假 `notify:*` 记录（健康面板跟着误报
#   "sms 连着失败 7 次"），也覆盖过真的 `out/attain-2026.json`。
#
# 光靠"我记得传 root"拦不住 —— 每次加一个会写盘的新步骤，都要把所有相关测试
# 过一遍补桩，而漏掉的那个测试**只在开发机上才看得出问题**。
# 所以在这儿兜一道：**跑之前拍一张快照，跑完比对**，多出来的文件直接报错。
#: ⚠ `in/`（2026-09-21 晚）也要盯：它是**收进来的东西**（各店发来的上报包 + 收信库），
#:   跟 `out/` 一样属于"这台电脑自己的"，忘了传临时 root 照样会写进开发机。
_WATCH_DIRS = ("out", "in")


_OUT_DIRS = _WATCH_DIRS          # 老名字，别再引用


def _snapshot():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    got = {}
    for d in _WATCH_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            for name in os.listdir(p):
                fp = os.path.join(p, name)
                try:
                    st = os.stat(fp)
                    got[d + "/" + name] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    got[d + "/" + name] = None
    return got


def pytest_sessionstart(session):
    session._cbg_files_before = _snapshot()


def pytest_sessionfinish(session, exitstatus):
    before = getattr(session, "_cbg_files_before", None)
    if before is None:
        return
    after = _snapshot()
    new = sorted(set(after) - set(before))
    # ⚠ **改了已有文件也要报**（2026-09-29 加）：只盯"新增"时漏掉了一整类 ——
    #   `health.boot(ROOT)` 的 `check_schema(apply=True)` 会把迁移真跑进开发机
    #   真库 `out/cbg-2026.db`（加 004 迁移那次实测：schema 3→4 + 建表）。
    #   数据没坏（跟门店自检做的事一样），但"测试不许写项目根"的口径要两边都守。
    changed = sorted(k for k in set(after) & set(before)
                     if after[k] and before[k] and after[k] != before[k])
    if new or changed:
        # ⚠ 只**报**不失败（`pytest_sessionfinish` 改不了退出码）——
        #   但这条红字足够定位：说明某个测试在往项目根写东西。
        print("\n" + "!" * 70)
        if new:
            print("⚠ 测试往项目根写了新文件（大概率是忘了传临时 root）：")
            for n in new:
                print("    " + n)
        if changed:
            print("⚠ 测试改了项目根的已有文件（查法：谁拿 root= 项目根跑了写操作）：")
            for n in changed:
                print("    " + n)
        print("  查法：`ls -l` 看 mtime 落在哪个测试；那个测试的 root= 要传 tmp。")
        print("!" * 70)
