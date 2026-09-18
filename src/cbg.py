"""华为 CBG 取数。

实测契约（2026-09-15，详见设计文档 §2.1）：

  报量账本  POST /isrp/srs/sale-order/paged-list
            真筛：startTime / endTime / storeCode / payStatus / returnStatus
            ⚠ historyData=true 直接报内部错误，只能 false
  订单详情  POST /isrp/soms/sale-order      ← **唯一带 SN 的投影**（details[].sn）
  门店详情  GET  /isrp/sms/store-info/store-detail   ← 会话自检用

⚠ 别用 `sts/basket/handling/list`：那是"当前挂起的购物篮"，结算后就没了，
  而且 type/documentType/status 全是假筛（传什么都返回同一条）。
"""

from __future__ import annotations

import datetime
import sys
import time
from dataclasses import dataclass, field

import requests

from .session import CBG_BASE, CbgAuthError, CbgSession

LIST_PATH = "/isrp/srs/sale-order/paged-list"
DETAIL_PATH = "/isrp/soms/sale-order"
STORE_PATH = "/isrp/sms/store-info/store-detail"
#: **本店名下 SN 清单**（玲珑在库）—— 2026-09-17 探到，是"该谁报量"的判据来源
INVENTORY_PATH = "/isrp/sws/inventory/query/physical"

CST = datetime.timezone(datetime.timedelta(hours=8))

# 命中就判定为会话/权限问题，而不是"没数据"
_AUTH_HINTS = ("未登录", "登录超时", "没有门店或数据范围", "unauthorized", "unauthenticated",
               "not.login", "session", "认证失败", "token")


class CbgError(RuntimeError):
    def __init__(self, msg, code=None, detail=None):
        super().__init__(msg)
        self.code = code
        self.detail = detail


@dataclass
class ReportedOrder:
    document_no: str
    order_no: str
    store_code: str
    created_at: datetime.datetime | None
    amount: float | None
    guide: str
    sns: list[str] = field(default_factory=list)
    items: dict = field(default_factory=dict)   # sn -> 商品名


class CbgClient:
    def __init__(self, session: CbgSession, store_code: str | None = None,
                 timeout: int = 40, verbose: bool = False, retries: int = 2):
        self.session = session
        self.store_code = store_code or ""
        self.timeout = timeout
        self.verbose = verbose
        self.retries = retries
        self.http = requests.Session()

    # ------------------------------------------------------------------ 基础
    def _request(self, method: str, path: str, payload: dict | None = None,
                 params: dict | None = None) -> dict:
        url = f"{CBG_BASE}{path}"
        p = {"t": int(time.time() * 1000), "locale": "zh_CN"}
        p.update(params or {})
        last = None
        for attempt in range(self.retries + 1):
            try:
                r = self.http.request(method, url, params=p, json=payload,
                                      headers=self.session.headers(), timeout=self.timeout)
            except requests.RequestException as e:
                last = e
                if attempt < self.retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise CbgError(f"请求 {path} 失败：{e}") from e

            if r.status_code in (401, 403) or "login" in r.headers.get("Location", "").lower():
                raise CbgAuthError(f"会话已失效（HTTP {r.status_code}）→ 重新抓一份 curl 导入")

            try:
                j = r.json()
            except ValueError:
                # 网关把登录页 HTML 返回来了 —— 就是掉登录
                raise CbgAuthError(
                    f"{path} 返回的不是 JSON（HTTP {r.status_code}，前 120 字："
                    f"{r.text[:120]!r}）→ 会话八成过期了，重新抓一份 curl 导入"
                ) from None

            if j.get("status") == "success":
                return j

            code = str(j.get("code") or "")
            msg = str(j.get("message") or "")
            detail = str(j.get("detail") or "")
            blob = f"{code} {msg} {detail}".lower()
            if any(h.lower() in blob for h in _AUTH_HINTS):
                raise CbgAuthError(f"会话/权限问题：{msg} {detail}".strip())
            # 服务端偶发内部错误 → 退避重试
            if "internalError" in code and attempt < self.retries:
                last = f"{code} {msg} {detail}"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise CbgError(f"{path} 返回失败：{msg} {detail}".strip(), code=code, detail=detail)
        raise CbgError(f"{path} 重试 {self.retries} 次仍失败：{last}")

    # ------------------------------------------------------------------ 接口
    def _warn_foreign_orders(self, codes, where: str) -> None:
        """接口返回的订单里混了**别家店**的，就吼一声。**不拦、不丢。**

        ## 为什么只警告不报错（改过一次，这是改回来的）

        最初写的是"发现别家店就 raise" —— 那样**一次正常运行的每日对账会被直接
        掐死**（`check` 里 `CbgError` → `EXIT_FETCH` → 不出报告）。
        一个我今天才加的安全检查，**不该有能力弄挂一条本来能跑的产线**。
        实测就出事了：加了之后定时任务跑不出结果。教训记在这儿。

        ## 为什么也不把别家的那些**丢掉**

        丢掉看起来"更干净"，其实更糟：报量集合少了几单 → 那几单的串号会被算成
        「未报量」→ **报告反而是错的**，而且错得很合理（看起来就是漏报）。
        报多少就认多少 —— 那才是这家店真实的报量口径。

        ## 那这道检查还有用吗

        有用：它把"门店编码可能填错了"这件事**写进日志**（`out/run.log` 和屏幕上），
        而不是让它静默过去。真正会坑人的场景是**一个华为账号能看两个店**——
        填成另一家自己有权限的店时，接口会欣然返回那家的数据、一声不吭。
        所以这里给一句能照着查的话，但**绝不代替用户做决定**。

        ⚠ 也说清楚：`storeCode` 是**真筛**，正常情况下这里什么都不会打印。
        真出现了，先怀疑**输出的编码格式**跟输入不是同一个（很常见），
        而不是一上来就认定配错了店。
        """
        want = (self.store_code or "").strip().upper()
        if not want:
            return
        foreign = {}
        for raw in codes:
            got = str(raw or "").strip()
            if got and got.upper() != want:
                foreign[got] = foreign.get(got, 0) + 1
        if not foreign:
            return
        detail = "、".join(f"{k}（{v} 单）" for k, v in sorted(foreign.items())[:5])
        print(f"[警告] {where} 里混了不属于本店的数据："
              f"配置的是「{self.store_code}」，实际还有 {detail}。"
              f" —— 报量按接口给的照算（**没有丢弃**）。"
              f"如果本店不该有这些，检查「设置 → 门店」的『华为门店编码』是不是填成了"
              f"别的店；⚠ 一个账号能看多个店时，填错**不会报错**、只会静默算错。",
              file=sys.stderr)

    # ------------------------------------------------------- 库存（数据池 B）
    def inventory(self, page_size: int = 1000) -> tuple:
        """**本店名下 SN 清单**（玲珑在库）→ `(rows, total)`，rows 是接口原样的 dict。

        2026-09-17 实测契约：

        * **观测单位：一行 = 一台机器** ✅（不是商品汇总 —— 这点是本接口的价值所在）
        * 串号字段：`sn` / `imei1` / `imei2` / `meid`
        * 归属：`storeCode` + `warehouseName`（实测三个仓：可售仓 / 礼品仓 / 物料仓）
        * `stockAge` = 库龄（天）；`updateTime` = 库存变动时间（毫秒）
        * `pageSize=1000` **一次拉全**（实测 414 行 / 1 页 / `totalRows=414`）
        * `sn` 是**真筛**（真值 1 行、瞎编值 0 行，A/B 验过）

        ⚠ **没有日期参数** —— 只能看**当前快照**。但这**不影响**漏报排查：
        没报量的机器会**一直挂在库里**（实测那台挂了 42 天还在），
        所以每天扫一遍当前快照 = 累积的漏报全覆盖。

        ⚠ 返回的 `sn` 里混着**非串号值**：礼品/物料类商品给的是 `***`（实测 80 行）
        或空串（18 行）。**调用方必须自己过滤**（长度 ≥ 8 且不是全星号），
        否则会把 `***` 当成一个串号去对账。
        """
        body = {
            "curPage": 1, "pageSize": page_size, "sort": "-update_time",
            "storeCode": self.store_code or "",
            "warehouseId": None, "sku": None, "spu": None, "spuName": None,
            "spuNameEn": None, "ean": None, "itemName": None, "itemNameEn": None,
            "bpart": None, "sn": None, "rfid": None, "receiver": None,
            "stockAgeInterval": [], "itemType": None, "tagCodeList": None,
            "categoryIds": [], "language": "Cn", "timezone": "Asia/Shanghai",
        }
        rows: list[dict] = []
        page, total = 1, None
        while True:
            body["curPage"] = page
            j = self._request("POST", INVENTORY_PATH, payload=body)
            batch = (j.get("result") or {}).get("physicalInventory") or []
            pv = j.get("pageVO") or {}
            if total is None:
                total = pv.get("totalRows")
            rows.extend(batch)
            total_pages = pv.get("totalPages") or 1
            if self.verbose:
                print(f"    库存第 {page}/{total_pages} 页：{len(batch)} 台")
            if page >= total_pages or not batch:
                break
            page += 1
        return rows, (total if total is not None else len(rows))

    def store_detail(self, store_code: str | None = None) -> dict:
        code = store_code or self.store_code
        if not code:
            raise CbgError("没给 storeCode，无法查门店详情")
        j = self._request("GET", STORE_PATH, params={
            "storeCode": code, "language": "Cn", "timezone": "Asia/Shanghai"})
        return j.get("result") or {}

    def ping(self) -> tuple[bool, str]:
        """连通 + 会话自检。返回 (是否可用, 说明)。"""
        try:
            r = self.store_detail()
        except CbgAuthError as e:
            return False, f"会话失效：{e}"
        except CbgError as e:
            return False, f"接口异常：{e}"
        name = r.get("storeName") or r.get("abbreviation") or "?"
        return True, f"{r.get('storeNo')} {name}"

    def list_orders(self, start_ts: int, end_ts: int, pay_status: int | str = 2,
                    return_status: int | str = 0, page_size: int = 200,
                    history_data: bool = False) -> list[dict]:
        """当日订单列表（分页取全）。"""
        body = {
            "ean": "", "startTime": int(start_ts), "endTime": int(end_ts),
            "curPage": 1, "pageSize": page_size,
            "payStatus": pay_status, "returnStatus": return_status,
            "historyData": history_data, "bussinessTypes": [],
            "language": "Cn", "timezone": "Asia/Shanghai",
        }
        if self.store_code:
            body["storeCode"] = self.store_code

        orders, page = [], 1
        while True:
            body["curPage"] = page
            j = self._request("POST", LIST_PATH, payload=body)
            batch = j.get("result") or []
            self._warn_foreign_orders((o.get("storeCode") for o in batch),
                                      "订单列表")
            orders.extend(batch)
            pv = j.get("pageVO") or {}
            total_pages = pv.get("totalPages") or 1
            if self.verbose:
                print(f"    订单列表第 {page}/{total_pages} 页：{len(batch)} 张")
            if page >= total_pages or not batch:
                break
            page += 1
        return orders

    def order_sns(self, document_no: str) -> ReportedOrder:
        """订单详情 → SN。列表投影里**没有** SN，必须走这一步。"""
        body = {"documentNo": document_no, "historyData": False,
                "language": "Cn", "timezone": "Asia/Shanghai"}
        if self.store_code:
            body["storeCode"] = self.store_code
        j = self._request("POST", DETAIL_PATH, payload=body)
        r = j.get("result") or {}
        # 详情是**唯一带 SN 的投影**，报量数字最后就是从这里出来的 —— 也吼一声
        self._warn_foreign_orders([r.get("storeCode")], "订单详情")
        created = None
        if r.get("docCreateTime"):
            created = datetime.datetime.fromtimestamp(r["docCreateTime"], CST)
        out = ReportedOrder(
            document_no=r.get("documentNo") or document_no,
            order_no=r.get("orderNo") or "",
            store_code=r.get("storeCode") or "",
            created_at=created,
            amount=r.get("includedTaxAmount"),
            guide=r.get("consumerGuideName") or r.get("salesAssistantId") or "",
        )
        for line in (r.get("details") or []):
            sn = (line.get("sn") or "").strip()
            if sn:
                out.sns.append(sn)
                out.items[sn] = line.get("itemName") or ""
        return out

    def reported_sns(self, start_ts: int, end_ts: int, **kw) -> dict[str, dict]:
        """目标区间内已报量的 SN 集合。

        返回 {sn: {documentNo, orderNo, item, amount, time, guide}}
        """
        orders = self.list_orders(start_ts, end_ts, **kw)
        out: dict[str, dict] = {}
        for i, o in enumerate(orders, 1):
            d = self.order_sns(o["documentNo"])
            if self.verbose:
                print(f"    [{i}/{len(orders)}] {d.document_no} → {d.sns or '（无 SN）'}")
            for sn in d.sns:
                out[sn] = {
                    "documentNo": d.document_no,
                    "orderNo": d.order_no,
                    "item": d.items.get(sn, ""),
                    "amount": d.amount,
                    "time": d.created_at.strftime("%Y-%m-%d %H:%M:%S") if d.created_at else "",
                    "guide": d.guide,
                }
        return out
