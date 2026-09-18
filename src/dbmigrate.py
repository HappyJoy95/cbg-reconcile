"""一次性库迁移 —— **2.1.0 这一版做一次，以后不做**（用户 2026-09-17 定）。

## 背景

2.1.0 把 `out/cbg-<年>.db` 从"只有华为订单"扩成"四池同库"，多了三张表
（`lg_stock` / `erp_stock` / `erp_sales`）。**其实不迁也能用** ——
`pools.ensure()` 会建表、`dump.ensure_columns()` 会补列。

用户仍然要"这一版重建一次库"，那就重建，但**按最安全的方式**：

* ⚠⚠ **绝不真删。** 老库**改名**成 `cbg-<年>.db.bak-2.1.0-<时间戳>`。
  名字一改，程序就认不出它了（等于"没有库"），效果跟删一样；
  但万一发现哪里不对，**改回来就全回来了**。确认没问题之后再人工删。
* **只做一次**：做完记一个标记，之后永远跳过。
* ⚠ **不挂 `selfupdate`** —— 那是**代码更新**，`out/` 是它的 `NEVER_TOUCH` 红线
  （AGENTS.md 坑 5：「自更新只许碰代码」）。挂在这儿会被红线挡住，
  而且"更新代码时顺手删业务数据"本来就危险。所以挂在**首次启动**。

## 后续会发生什么

库被改名之后，程序眼里"没有库"⇒ 下一次 `daily` 的第 1 步会走
**"第一次跑 → 抓当年全量"** 那条路，把数据重新抓回来。
这是设计好的衔接，不是 bug —— 日志里会明说。

⚠ 代价：**今年的订单历史会重新抓一遍**（华为全量 + 云商 26 段，几分钟），
而且 **POS 看板在那之前是空的**。
"""

from __future__ import annotations

import datetime
import json
import shutil
from pathlib import Path

#: 做完就写它，之后永远跳过
MARK_REL = ".secrets/db-rebuilt-2.1.0.json"

#: 归档后缀（带时间戳，避免同一天跑两次互相覆盖）
SUFFIX = "bak-2.1.0"


def mark_path(root) -> Path:
    return Path(root) / MARK_REL


def done(root) -> bool:
    return mark_path(root).is_file()


def only_in_release(root) -> str:
    """**只在正式包里做**（用户 2026-09-17 定）—— 返回空串表示"该做"，否则是原因。

    * **beta 包不做** —— 那是给开发/自己测的，测的时候不该把门店的库改名
    * **源码运行 / git clone 不做** —— 开发机上更不该动
    * 只有**门店拿到的正式包**（有 `BUILD.txt` 且不带 `beta` 标记）才做

    ⚠⚠ **不能靠 `VERSION` 判断** —— beta 包和正式包**版本号同号**
    （「beta 只是给同一个版本加个标记」的直接后果，见 AGENTS.md 发版那节）。
    所以判据只能是 `BUILD.txt`：它是**打包时写的**，开发机上根本不存在。
    """
    import os
    import re

    from . import version

    # ⚠⚠ **逃生口。** 2026-09-17 实测踩过一次：开发机上跑了某个测试，
    #   而当时项目根**正好存在**一个 `BUILD.txt`（测试/脚本写的，不是打包脚本写的），
    #   门槛被骗过 ⇒ **开发机的 77MB 真库被改名了**。
    #   （幸亏是"改名"不是"删"，`mv` 回来就全好了 —— 这就是那条设计的意义。）
    #   `tests/conftest.py` 会把它设上，**所有测试都不可能误触发**。
    if str(os.environ.get("CBG_NO_DB_REBUILD") or "").strip():
        return "设了 CBG_NO_DB_REBUILD（测试 / 手动跳过）"

    try:
        build = version.BUILD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "没有 BUILD.txt（源码运行 / git clone）"
    if not build:
        return "BUILD.txt 是空的"
    # ⚠ 判据抽在 `version.is_beta()` —— `selfupdate.has_update` 用的是同一条
    #   （那边管"beta 包能不能升回同号正式版"）。各写一份的话，
    #   哪天有人改了其中一处的大小写处理，另一处会**静默失效**。
    if version.is_beta(build):
        return "这是 beta 包（%s）" % build
    # ⚠ 再收一道：**正式包的 BUILD.txt 就是 `YYYY-MM-DD HH:MM`**（打包脚本写的）。
    #   不匹配这个形状的一律不做 —— 免得项目根上随便一个同名的文件又把门槛骗过去。
    if not re.match(r"^\d{4}-\d{2}-\d{2}[ T]", build):
        return "BUILD.txt 不像正式包的格式（%s）" % build
    return ""


def rebuild_once(root, out_dir=None, *, dry_run: bool = False,
                 force: bool = False) -> dict:
    """把 `out/cbg-<年>.db` **改名归档**（只做一次）。

    返回 `{ok, skipped, archived: [新旧路径], reason}`。
    **任何异常都不往外抛** —— 这个动作失败不该把启动流程弄挂。
    """
    root = Path(root)
    if done(root):
        return {"ok": True, "skipped": True, "archived": [],
                "reason": "这一版已经重建过了"}
    if not force:
        why = only_in_release(root)
        if why:
            # ⚠ **不写标记** —— beta 测完、正式包上来时还要再做一次
            return {"ok": True, "skipped": True, "archived": [],
                    "reason": "跳过（%s）" % why}

    d = Path(out_dir) if out_dir else (root / "out")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = []
    try:
        if d.is_dir():
            for db in sorted(d.glob("cbg-[0-9][0-9][0-9][0-9].db")):
                target = db.with_name("%s.%s-%s" % (db.name, SUFFIX, stamp))
                if dry_run:
                    archived.append((str(db), str(target)))
                    continue
                # ⚠ **改名不是删除** —— 见模块顶部。
                #   同目录改名是原子的，断电也不会出现"半个库"。
                shutil.move(str(db), str(target))
                archived.append((str(db), str(target)))
        if not dry_run:
            p = mark_path(root)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(
                {"at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "archived": [b for _, b in archived]},
                ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as e:
        # ⚠ 归档失败**不要写标记** —— 下次启动还得再试一遍
        return {"ok": False, "skipped": False, "archived": archived,
                "reason": "归档失败：%s" % e}
    return {"ok": True, "skipped": False, "archived": archived,
            "reason": "归档了 %d 个库（改了名，没删）" % len(archived)}
