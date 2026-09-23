"""**数据抓取模块** —— 「数据从哪儿来、什么时候抓的、还能不能算」。

用户 2026-09-19 定的位置：

> 功能模块……**从数据抓取模块获取数据**

所以这个模块的对外出口是**一张数据源登记表** + 一份**新鲜度结论**：

| 出口 | 给谁 | 说什么 |
|---|---|---|
| `sources()` | 写功能的人 | 这个系统里到底有哪几路数据、各自**用什么抓、落在哪张表、多久算过期** |
| `state(root)` | 启动自检 / 概览横幅 | 每路数据现在能不能算（五种状态，见 `app/data_state.py`） |
| `mail.recent(...)` | 需要"从邮件里拿东西"的功能 | **收信**（哪个邮箱、怎么搜、返回什么 —— 见 `mail.py`） |
| `unseal_attachment(...)` | 收进来的附件 | **解密**（附件是密文就还原，是明文就原样过）—— 见下面「解密也是取数」 |

## ⚠ 为什么要有这张表

写第 3 个功能时才发现的坑：**"数据从哪来"这件事全项目没有一处说得清**。
POS 知道要 `out/cbg-<年>.db` 的 `orders` 表，报量核对知道要 `erp_sales`，
销售达成要腾讯文档那份目标 —— 各写各的，谁也不知道还有没有别的路子，
更不知道新功能该接哪一根。这张表就是给"下一个功能"看的。

## 邮件也是"外面的数据"（2026-09-20 加）

用户：「数据抓取模块要加一个**获取指定邮件**的能力，**默认读取的是
439845914@qq.com** 这个邮箱，**有配置 smtp 的话那就读取自己配置的邮箱**的指定邮件」
⇒ `fetch.mail.recent(subject_contains=…, sender_contains=…)`。
⚠ 它跟推送（`modules/notify`）是**两个方向**：那边只发、这边只收，别混。

## 解密也是取数（2026-09-21 加）

用户：「这个加密功能算在推送模块里，**解密功能做在 fetch 模块里**」——
跟上面那句"收信也是取数"是同一条思路：**进来的东西怎么还原**，归这个模块。

⇒ `fetch.unseal_attachment(data, root=…)`，用在 M18/M19 那条链上：
   `mail.recent()` 把邮件连附件拿回来 → `app/report_inbox.py` 落库**之前**先过它。

⚠ **算法本体不在这儿**，在 `src/mailcrypto.py` —— 密文格式只能有**一份定义**，
   加解密拆成两份迟早走散（这个项目为"两份定义"栽过好几次）。
   两个模块各是**自己那一侧的对外入口**：那边 `notify.seal_attachment()`（发出去），
   这边 `unseal_attachment()`（收进来）。
⚠ 密钥状态（有没有、是哪把）在推送那边问：`notify.mail_key()`。

## 边界

* **不搬家实现**：真正去抓数的还是 `dump.py`（华为订单）、`erp.py`（云商）、
  `tdoc.py`（腾讯文档）。这个模块只回答"有哪些、新不新鲜"。
* **不在自检里真去抓**（用户 2026-09-19 定：启动只做静态检查）——
  真抓一次要登录、要几十秒，还会把"今天还没抓"变成"今天抓失败了"。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

#: 数据源登记表 —— 加一路数据就在这儿加一行。
#: `state_key` 有值 ⇒ 新鲜度由 `app/data_state.py` 判（它连着数据库）；
#: 没有 ⇒ 这路数据不进"能不能算"的判定（比如目标表是人工维护的）。
SOURCES = (
    {"key": "orders", "label": "华为订单（已报量）", "grab": "dump.py",
     "where": "out/cbg-<年>.db · orders / reported_sns", "state_key": "dump",
     "how": "cbg.py 拉订单列表，会话来自 auth 模块", "daily": True},
    {"key": "erp-sales", "label": "云商销售明细", "grab": "erp.py",
     "where": "out/cbg-<年>.db · erp_sales", "state_key": "erp-sales",
     "how": "Api/Sale 明细，按门店 + 日期区间", "daily": True},
    {"key": "lg-stock", "label": "玲珑库存（池B）", "grab": "dump.py",
     "where": "out/cbg-<年>.db · lg_stock", "state_key": "lg-stock",
     "how": "随订单一起入库的快照", "daily": False},
    {"key": "erp-stock", "label": "云商库存（池D）", "grab": "erp.py",
     "where": "out/cbg-<年>.db · erp_stock", "state_key": "erp-stock",
     "how": "在库/在途/串号级", "daily": False},
    # 盘点账面（M16，用户 2026-09-20：「页面找**数据抓取模块**抓取最新的库存」）——
    # ⚠ **现抓现用、不落库**：盘点要的是"此刻账上有什么"，落库那一刻它就旧了。
    #   `state_key` 故意留空 ⇒ 不进"能不能算"的判定（这路数据没有"新不新鲜"，
    #   取不到就是取不到，盘点页当场会说清）。
    {"key": "inventory-book", "label": "盘点账面（按仓，实时）", "grab": "erp.py",
     "where": "现抓现用，不落库（冻结在盘点页的浏览器存储里）", "state_key": "",
     "how": "自定义库存表 RptStoreNow：StoreIds 筛仓，含在途列", "daily": False},
    {"key": "targets", "label": "周度任务目标分配（腾讯文档）", "grab": "tdoc.py",
     "where": "现读现算，不落库（腾讯文档会覆盖改写，**没有历史**）",
     "how": "匿名读 docs.qq.com，tab「周度重点产品」+「周度重点映射表」",
     "state_key": "", "daily": False},
    # ⚠ **收信也是取数**（用户 2026-09-21：「收信也是 fetch 啊」）—— 方向跟发推送相反，
    #   但性质一样：从**外面的系统**（邮箱）把数据拿进来。所以它登记在这儿，
    #   **不是第七个系统模块**。（M18/M19 那条链：`fetch.mail.recent()` 读邮箱 →
    #   `app/report_inbox.py` 落 `in/report.db`。）
    # ⚠ `state_key` 先留空：`app/data_state.py` 里**还没有它的探针**，
    #   没探针就写个 key 会让横幅显示"从没采过"（假警报）。
    #   要让它进五态判定，得连探针 + 横幅文案一起加 —— 那是 M20 的活，别顺手塞。
    # ⚠ `daily: True`：它就是 `run_daily` 里那一步（"[7/8] 收取门店上报"）。
    #   `daily_keys()` 的 docstring 写的是"每天那趟该抓哪几路，和 run_daily 对得上"，
    #   所以它得进 —— 不然那条断言和真实步骤是两回事。
    # ⚠ `grab` 要写**一个真实存在的入口**（有测试按这个查文件）——
    #   收信这条路是"mail.py 取回来 + report_inbox.py 落库"，这儿写落库那半，
    #   取信那半在 `how` 里说。
    {"key": "report-inbox", "label": "门店上报（收信）", "grab": "app/report_inbox.py",
     "where": "in/report.db · inbox / rows_ / splits / skips",
     "how": "读邮箱里的上报包（`fetch.mail.recent`）→ 落库；包留在 in/packages/",
     "state_key": "", "daily": True},
    {"key": "stores", "label": "门店映射表", "grab": "config/stores.yaml",
     "where": "config/stores.yaml（随程序走）", "state_key": "",
     "how": "人工维护：ERP 店名 ↔ 华为门店编码 ↔ 串号标识", "daily": False},
)


def sources() -> List[dict]:
    """登记表的副本（**别让调用方改到原表**）。"""
    return [dict(s) for s in SOURCES]


def get(key: str) -> Optional[dict]:
    for s in SOURCES:
        if s["key"] == key:
            return dict(s)
    return None


def daily_keys() -> tuple:
    """每天那趟**该抓**哪几路 —— 和 `run_daily` 的步骤对得上。"""
    return tuple(s["key"] for s in SOURCES if s.get("daily"))


def state(root=None, db: str = "", *, need=None) -> dict:
    """每路数据现在**能不能算** —— 转发 `app/data_state.py`（判据只有那一处）。

    回来的 `{"sources": [...], "worst": ...}` 只覆盖连着库的那几路；
    登记表里 `state_key` 为空的那几路（目标、门店表）**不进判定** ——
    它们是人工维护的，卡住它们的从来不是"新不新鲜"。
    """
    from ...app import data_state
    return data_state.data_state(root, db, need=need)


def table(root=None) -> list:
    """登记表 + 最新状态，一行一路 —— 界面 / 排查直接打这张表。

    没进判定的那几路状态写 `-`（**不写 ok**：没判过就说 ok 是编的）。
    """
    from ...app import data_state
    st = state(root)
    by = {s["key"]: s for s in (st.get("sources") or [])}
    rows = []
    for s in SOURCES:
        row = dict(s, state="", state_label="", why="", rows=0, as_of="", collected_at="")
        k = s.get("state_key") or ""
        got = by.get(k) if k else None
        if got:
            row.update(state=got.get("state", ""),
                       state_label=data_state.LABELS.get(got.get("state"), ""),
                       why=got.get("why", ""), rows=got.get("rows", 0),
                       as_of=got.get("as_of", ""), collected_at=got.get("collected_at", ""))
        else:
            row["state_label"] = "-"
        rows.append(row)
    return rows


def brief(root=None) -> str:
    """一句话概括数据状况（给日志 / 概览页用）。"""
    from ...app import data_state
    return data_state.brief(state(root)).get("text", "")


def exists_orders_db(root=None) -> Path:
    """订单库在不在 —— 路径口径收在这儿（`app/data_state._find_db` 那份是判据用的）。"""
    from ...paths import ROOT
    from ...app import data_state
    root = Path(root) if root else ROOT
    found = data_state._find_db(root)
    return Path(found) if found else Path("")


def unseal_attachment(data, *, root=None):
    """**把收进来的附件还原** —— 解密这一半归这个模块（见模块头「解密也是取数」）。

    返回 `(原始字节, 说明)`，说明里的 `state` 有三种：

    | state | 什么意思 | 调用方该怎么办 |
    |---|---|---|
    | `plain` | **不是密文**（明文老包，或者别的功能的附件） | 原样往下走 —— 升级是渐进的，两边的版本会不一致 |
    | `opened` | 解开了 | 用返回的字节 |
    | `failed` | 是密文但**解不开**（缺密钥 / 密钥不对 / 被改过） | ⚠ **返回的是空字节**：必须看 state，**不许把它当内容写下去** |

    ⚠ **绝不抛**：这是收信路径上的东西，抛出去的表现是"区长收不下门店的包"，
      而那件事不该由加密来决定（`mailcrypto` 顶部那条规矩）。
    ⚠ 收信侧**没有密钥**时也要照旧把包收下来、把问题**说出来**
      （`report_inbox` 会记进 `problems`，M20「数据交换」页那块「收信的问题」会显示）。
    """
    from ... import mailcrypto
    return mailcrypto.unseal(data, root=root)
