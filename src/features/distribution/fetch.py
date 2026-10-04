"""现拉分销单 → 落 `out/distribution.db`（M1 的执行链）。

```
fetch.run(root, start, end)
  └─ ErpClient.sales_range(start, end)     自动按 10 天分段（≤10 天是服务端上限）
       └─ 筛 单据类型 ∈ {分销, 分销退} 且 门店 == 渠道分销部
            └─ store.replace_range(...)    区间覆盖写（重拉即修正）
```

## 为什么不复用 `erp_sales`（写代码前实测过，别再走回头路）

`out/cbg-2026.db` 的 `erp_sales` 有全年数据，但 2026-09-29 回源核对
1/25~1/31：**回源 1,980 行 vs 库 1,760 行，139 个单整单不在库里** ——
支付时间就在区间内、接口现在拉得到、库里从来没有（最可能：事后补录，
而 erp-dump 平时只重拉当月，1 月的洞永远补不上）。
⇒ 看板必须**按用户选的时间段现拉**；`erp_sales` 是对账的地盘，一行不碰。

## 限流纪律（erp-api skill 坑 0）

一个进程登录一次跑完、不并行。页面上"拉取"是**串行**调用
（同一时刻只允许一个 fetch 在跑 —— `run` 里的进程锁），
失败把 `ErpError` 的原话带给界面，不静默。
"""

from __future__ import annotations

import datetime
import threading
from pathlib import Path
from typing import Callable, Optional

from . import store
from . import TARGET_STORE

#: 分销单的单据类型（负金额的退货也在里面 —— 看板一律净额）。
DIST_TYPES = ("分销", "分销退")

#: ⚠ 同时只许一个拉取在跑（限流纪律）。界面会看到 busy 的报错而不是排队。
_LOCK = threading.Lock()

#: 单次拉取的区间上限 —— `sales_range` 会自己分段，但一页让人拉 2 年
#: 会把 70+ 段请求砸向云商（限流坑 0）。366 天够任何看板用了。
MAX_DAYS = 366


class FetchBusy(RuntimeError):
    """已有拉取在跑 —— 界面提示"稍后再试"，不是失败。"""


def _erp_client():
    """造客户端（延迟 import：`src.erp` 拉 requests，别让纯读路径付这笔钱）。"""
    from ...integrations.erp import ErpClient
    return ErpClient()


def run(root: Optional[Path] = None, start: datetime.date = None,
        end: datetime.date = None, on_progress: Optional[Callable] = None,
        client=None) -> dict:
    """拉 `[start, end]` 的分销单并区间覆盖写。返回 `{ok, rows, ...}`。

    `client` 供测试注入（假客户端不碰网络）。`on_progress(段起, 段止, 行数)`
    透传给 `sales_range` 打进度。
    """
    from ...integrations.erp import ErpError

    today = datetime.date.today()
    end = end or today
    start = start or end.replace(day=1)
    if end < start:
        return {"ok": False, "why": "结束日期比开始日期早（%s > %s）" % (start, end)}
    if (end - start).days + 1 > MAX_DAYS:
        return {"ok": False, "why": "区间超过 %d 天，请缩短时间段" % MAX_DAYS}

    if not _LOCK.acquire(blocking=False):
        raise FetchBusy("已经有一次拉取在跑，等它结束再试")
    try:
        c = client or _erp_client()
        try:
            rows = c.sales_range(start, end, on_progress=on_progress)
        except ErpError as e:
            # ⚠ 原话带出去（限流的「登录超时」和真错误要能分清 —— skill 坑 0：
            #   限流等几分钟自愈，别让人以为是账号坏了去重登，那会雪上加霜）
            return {"ok": False, "why": "云商销售拉取失败：%s" % e}
        except Exception as e:                                  # noqa: BLE001
            return {"ok": False, "why": "云商销售拉取失败（%s）：%s" % (type(e).__name__, e)}

        picked = [r for r in rows
                  if str(r.get("单据类型") or "").strip() in DIST_TYPES
                  and str(r.get("门店") or "").strip() == TARGET_STORE]
        written = store.replace_range(root, start, end, picked)
        return {"ok": True, "rows": written, "scanned": len(rows),
                "start": start.isoformat(), "end": end.isoformat(),
                "store": TARGET_STORE}
    finally:
        _LOCK.release()


def busy() -> bool:
    """现在有没有拉取在跑（界面按钮禁用态用它）。"""
    return _LOCK.locked()
