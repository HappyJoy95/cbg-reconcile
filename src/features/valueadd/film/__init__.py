# -*- coding: utf-8 -*-
"""**防护膜达成情况** —— 增值下的子模块。

口径见 `metric.py`（**开发逆推稿不进正式包**）。

| 文件 | 干什么 | 能不能碰库 |
|---|---|---|
| `metric.py` | **口径**：新机/达成/毛利/达成率 | ❌ 纯函数 |
| `compute.py` | IO：查 `erp_sales`（**只读 SQLite**）→ 算 → 给接口 | ✅ |
| `export.py` | 导出 Excel | ✅ |

⚠ 页面**现算**（不读缓存）；定时步骤 `film` 只负责**落一份快照**
  （`out/film.json`）方便回看 —— 算一次很快，不值得为缓存引入失效问题。
"""

from __future__ import annotations

from ...registry import Step, Sub

from . import compute, export, metric  # noqa: F401


def step_run(ctx) -> bool:
    """执行入口（注册协议 v2）—— 原 `run_daily._step_film` 收编到这里。

    `ctx` 由 `run_daily` 统一构造：`root` / `config_path` / `emit`。
    返回 `True`（成功 → `EXIT_OK`）/ `False`（失败 → `EXIT_FETCH`）/ `int`（自定退出码）。
    ⚠ 本模块**不许 import `cli`**（features 红线）—— 退出码语义在引擎那侧翻译。
    """
    import sys

    print("\n[film] 防护膜达成（落快照 out/film.json）")
    res = compute.run(root=ctx.root, emit=ctx.emit)
    if not res.get("ok"):
        print("\n⚠ 防护膜达成没算成：%s" % res.get("why"), file=sys.stderr)
        return False
    return True


#: 主页面。带 `step` ⇒ 点「刷新」时跟 `erp-dump` 一起跑（`REFRESH_STEPS.film`）。
#:
#: ⚠ `whens=()` = **定时器不管它**（2026-09-22 用户：「防护膜这个不用自动跑吧，
#:   跟月度生意计划一样，门店手动拉达成就行了」）—— 空 `whens` 就是"只手动"，
#:   别写成"默认 21:00"。
#: ⚠ `default=False`：也不进双击 `daily` 那趟整批。
#:
#: ⭐ 操作/数据范围声明（注册协议 v2 试点，2026-10-02）：
#:   * `view` 三类身份都有（这一页登录就能看）；
#:   * `export` = 区长/平台（原 `_can_for` 里 `"film.export": not is_store` 的口径，
#:     现在**只此一处声明**，后端 `require()` 与 `role.can` 都从它派生）；
#:   * `data="authorized"` = 锁本店/所辖（原 `filter_film_rows` 的口径）。
SUB = Sub(key="film", label="防护膜达成情况", order=10,
          ops={"view": ("store", "manager", "platform"),
               "export": ("manager", "platform")},
          data="authorized",
          step=Step(cmd="film", label="防护膜达成（落快照）", order=47,
                    default=False, flag="--skip-film", whens=(),
                    run=step_run))
