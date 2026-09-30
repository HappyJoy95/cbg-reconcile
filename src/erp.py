"""云商 ERP 取数（自带最小实现）。

契约来源：erp-api skill（`~/.dsh/skills/erp-api/references/接口与踩坑.md` §销售报表），
2026-09-15 逐条复核。部署到门店电脑时没有 skill 目录，所以这里自带一份，
**只做「登录 + 销售明细导出」两件事**。与 skill 冲突时以实测为准。

实测要点：
- 销售报表**必须表单式**（form），用 JSON 发一定报「操作失败，请稍后重试」
- **必须带 `Column[]` 列定义**，不带就报同样的错
- 单次区间上限 **10 天**
- 导出的 xlsx 第 0 行是大标题，**第 1 行才是表头**
- **不能并行登录**：多个进程同时登录会四域同时报「登录超时」，换 token 也没用
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from . import httputil   # 业务接口不吃代理（2026-09-29 死代理那个坑）
from . import envfile
from .paths import ROOT as _ROOT
from .xlsx_io import read_rows

API_BASE = "https://api.yserp.cc"
REPORT_BASE = "https://apireport.yserp.cc"
ERP_ORIGIN = "https://erp.yserp.cc"
ERP_REFERER = "https://erp.yserp.cc/"
DEFAULT_COMPANY = "00001937"

#: **门店云商账号**的存放文件 —— 跟公司账号**完全分开，两边不互相回落**。
#:
#: 用户 2026-09-18（第二次提，这次目的明确）：「加个门店的登录设置吧，
#: 主要是读取这个账号的门店信息。来匹配不同门店的设置，
#: 当然公司云商账号也是加密保留的，门店云商账号可以不加密」。
#:
#: ⚠ 上午（同一个下午）曾经删过一次，理由是"没必要" —— 那时**没有用途**，
#:   实测也证明取数上它没有任何优势（销售明细两边一模一样）。
#:   现在有了用途：**认"这台机器是哪家店"**，见 `branch_self()`。
#:
#: ⚠ 明文存（不加密）：公司账号那对是内置混淆的（`_BUILTIN_*`），
#:   门店账号按用户的话**不加密** —— 它就存在这台机器的 `.secrets/` 里，
#:   那份文件自更新一根手指都不碰。
STORE_ENV_FILE = ".secrets/erp-store.env"

#: 组织架构树 —— ⚠ **唯一一个按账号收窄的接口**（2026-09-18 两个账号实测对比）：
#:
#: | 接口 | 公司账号 | 门店账号 |
#: |---|---|---|
#: | `api/branch/OrgTreeBranchList` 组织架构 | 平台 + **41 家店** | **只有自己那一家** |
#: | `Api/Store/List` 仓库列表 | 50 个仓 | 50 个仓（**一模一样**） |
#: | `Api/Branch/CompanyStore` 公司门店对照 | 45 条 | 45 条（**一模一样**） |
#: | `sales_rows` 销售明细 | 42 家店 | 42 家店（**一模一样**） |
#:
#: 所以「这台机器是哪家店」**只能**从这个接口认。
BRANCH_TREE_URL = f"{API_BASE}/api/branch/OrgTreeBranchList"

#: 云商「用户」名单 —— 登录名 / 姓名 / 手机 / 所属机构 / 状态。
USER_LIST_URL = f"{API_BASE}/Api/User/UserList"
SALES_MAX_DAYS = 10
# 单据类型：6,7=零售 / 666,667=零售退 / 3,4=分销 / 668,669=分销退 / 71,72=客情 / 10,95=其它
DEFAULT_BILL_TYPES = "6,7,666,667,3,4,668,669,71,72,10,95"

# 顺序即导出顺序。id 是 ERP 内部的列 id。
SALES_COLUMNS = [
    ("BillCode", "单号", 3), ("BillTypeDesc", "单据类型", 4), ("ProId", "商品编码", 6),
    ("SNCode", "69码", 38), ("ProName", "商品名称", 7),
    ("CategoryName1", "一级分类", 8), ("CategoryName2", "二级分类", 9),
    ("CategoryName3", "三级分类", 10), ("CategoryName4", "四级分类", 10),
    ("Brand", "品牌", 11), ("Model", "型号", 12), ("Color", "颜色", 13),
    ("Imei", "串号", 14), ("OldFlag", "串号标识", 5),
    # ⚠ 销售报表**能带出**串号2/3 表头（2026-09-23 实测 9/1~20 全空 ——
    #   服务端对销售行不填副串号）。真 SN 在**库存**三列（imei/sub_imei/sub_imei1），
    #   待领页走库存反查（`claim.compute`）。列仍请求：上游哪天填了就能进库。
    ("Imei2", "串号2", 519), ("Imei3", "串号3", 520),
    ("BillDate", "支付时间", 15),
    ("CreateDate", "制单时间", 155), ("ReceivingDate", "入库时间", 36),
    ("ProPrice", "单价", 16), ("ProCount", "数量", 17), ("SubTotal", "金额", 18),
    ("SubTotalCost", "财务成本", 19), ("SubTotalProfit", "财务毛利", 20),
    ("SubNetCost", "不含税成本", 38), ("SubNetProfit", "不含税毛利", 39),
    ("GiftCostPrice", "赠品成本", 21), ("SingleProfit", "单品毛利", 22),
    ("SubTotalCost4Assess", "零售考核成本", 23), ("SubTotalProfit4Assess", "零售考核毛利", 24),
    ("SubTotalCost4BatchAssess", "批发考核成本", 23), ("SubTotalProfit4BatchAssess", "批发考核毛利", 24),
    ("PayDetails", "付款方式", 30), ("CouponName", "优惠券名称", 25), ("Discount", "优惠金额", 26),
    ("TestMobileStatusName", "验机状态", 101), ("ReturnStatusName", "退货状态", 102),
    ("IvcNumber", "发票号码", 157), ("InvoiceStatus", "开票状态", 156),
    ("SalesManName", "业务员", 27), ("BranchName", "门店", 28), ("HandlerName", "店员", 29),
    ("SupplierName", "供应商", 32), ("CustomerPhone", "客户/顾客手机", 34),
    ("CustomerName", "客户/顾客", 2), ("EngineerName", "工程师", 36),
    ("NumberRN", "RN工单号", 37), ("DetailDescription", "单行备注", 351), ("Description", "备注", 35),
]

# 关键列（对账用；其余列照样导出，方便人工复核）
KEY_COLS = ("单号", "单据类型", "商品名称", "串号", "串号标识", "支付时间", "金额", "门店", "店员")

#: 库存串号导出的**列规格**（`column[i][xxx]`）。
#:
#: ⚠⚠ **必须发完整的 10 个属性。** 只发精简的（Id/Title/Key）会一律返回
#: `ResponseID=-1「操作失败，请稍后重试」` —— 2026-09-14 就是这样把它
#: **误判成"没有导出接口"**的。照抄自 erp-api skill（那边实测收录）。
#:
#: 每项：`(Id, Weight, Align, CanHidden, CanSort, DataType, Key, Show, Title, Width)`
INVENTORY_IMEI_EXCEL_COLUMNS = [
    (220, 9, "Left", 1, 0, "", "RowId", "1", "序号", 50),
    (98, 10, "Left", 1, 1, "", "Imei", "1", "IMEI1", 180),
    (517, 11, "Left", 1, 1, "", "SubImei", "1", "IMEI2", 180),
    (518, 12, "Left", 1, 0, "", "SubImei1", "1", "IMEI3", 180),
    (94, 20, "Center", 1, 0, "", "ProId", "1", "编码", 100),
    (1656, 30, "Center", 1, 0, "", "Config", "1", "配置", 100),
    (303, 30, "Center", 1, 0, "", "SNCode", "1", "69码", 100),
    (95, 40, "Left", 1, 1, "", "ProName", "1", "名称", 200),
    (96, 50, "Left", 1, 1, "", "Brand", "1", "品牌", 100),
    (523, 51, "Left", 1, 1, "", "Model", "1", "机型", 100),
    (97, 60, "Left", 1, 1, "", "Color", "1", "颜色", 100),
    (99, 70, "Left", 1, 1, "", "StoreName", "1", "分仓", 100),
    (102, 80, "Center", 1, 1, "DateTime", "ReceivingDate", "1", "入库时间", 150),
    (0, 85, "Center", 1, 0, "", "old_flag", "1", "串号标识", 90),
    (315, 90, "Right", 1, 1, "Int", "Ages", "1", "库龄", 80),
    (521, 95, "Right", 1, 1, "Int", "AgeBranch", "1", "店龄", 50),
    (100, 100, "Right", 1, 1, "Money", "ReceivingPrice", "1", "入库价格（在库成本）", 100),
    (1621, 101, "Center", 1, 1, "Money", "TaxAmount", "1", "税额", 150),
    (1622, 102, "Center", 1, 1, "Money", "SubNetCost", "1", "不含税成本", 150),
    (101, 110, "Left", 1, 1, "", "ReceivingSupplierName", "1", "供应商", 200),
    (513, 120, "Left", 1, 1, "", "Unit", "1", "产品单位", 180),
    (512, 120, "Left", 1, 1, "", "ProCount", "1", "产品数量", 180),
    (0, 0, "Left", 1, 0, "", "Status", "1", "状态", 80),
    (0, 0, "Left", 1, 0, "", "Description", "1", "备注", 100),
]

#: 自定义库存表（`RptStoreNow`）的**维度列** —— `groupBy` 决定服务端返回哪些列。
#:
#: ⚠ 这份字符串是从 `Inventory Check/src/erp.js` 的 `GROUP_BY` 原样搬过来的
#:   （那边线上跑了很久）。**别顺手改**：少一列**不报错**，只是那一列全空 ——
#:   比如少了 `OldFlag`，"串号标识"整列变空，"只看样机/演示机"那个筛选就永远筛不出东西。
STORE_NOW_GROUP_BY = ("Store,Category1,Category2,Category3,Brand,Model,ProName,OldFlag,"
                      "Category4,Imei,Imei2,Imei3,ProId,SNCode,PriceLabel")

#: `outCol` 只认这两列（写别的列名服务端报「列名无效」）。
#: `ProCount` = 在库数量，`ProCount_OnTransfer` = **在途数量** ——
#: 盘点页「在途待入库」那个页签全靠后一列，少了它整页是空的（且不报错）。
STORE_NOW_OUT_COLS = "ProCount,ProCount_OnTransfer"

#: 盘点账面的接口。本项目走**表单式**（`ErpClient.call()` 的既有风格）。
#:
#: ⚠⚠ **"这个端点不认 JSON"是个假结论，别照着它写代码**（2026-09-20 自己踩的）：
#:   当时同一轮里先发 JSON 拿到 `ResponseID=1 未登录`，就记成"只认表单式"——
#:   而那一发用的是**开头抓的旧 token**，同一轮的表单式那发其实刚触发过重登。
#:   回头用**当下这个新 token** 只改 body 编码再 A/B 一次：
#:   `表单式 ResponseID=0 / JSON 式 ResponseID=0`，**两种都行**。
#:   ⇒ 教训跟 erp-api skill 坑 3b 是同一条：**限流期、token 过期期的报错不是接口规则**，
#:     下结论前必须换个干净的时刻复现一次（`test_erp_store_now.py` 钉的是行为，不是这条）。
STORE_NOW_URL = REPORT_BASE + "/api/supplier/RptStoreNow"

#: 一次要多少行。前端用的就是 30000（一个仓的账面一次拿全）。
#:
#: ⚠ erp-api skill 里那句「PageSize 调大 → 服务端 NRE」说的是**另一条路径**
#:   （`file.yserp.cc/api/export/ExportRptStoreNow`，那边默认 100）；
#:   本路径 2026-09-20 实测 PageSize=3 正常。拿全拿不全由下面的行数自检兜住 ——
#:   **不靠"页够大"这种假设**。
STORE_NOW_PAGE = 30000

#: 分页上限 —— 到了还没拉全就报错，别一直拉。
STORE_NOW_MAX_PAGES = 40

LEGACY_ENV_PATHS = [
    Path.cwd() / ".secrets" / "erp.env",
    _ROOT / ".secrets" / "erp.env",                                    # 项目根，与 cwd 无关
    Path.home() / ".dsh" / "secrets" / "erp.env",
]

DEFAULT_ENV_FILE = ".secrets/erp.env"

#: 云商账号**只有一个**（公司账号，最高权限，能拉全公司数据）。
#:
#: 2026-09-18 一度做过"两个账号"（公司 + 门店），当天就被用户砍掉了：
#: 「我想了想，不要门店云商账号了，没必要」。
#: ⚠ 实测也证明门店账号没必要 —— 除了组织架构树，**销售明细 / 仓库列表 /
#: 公司门店对照两边返回的一模一样**（公司账号 41 家店 vs 门店账号 1 家店那条
#: 只影响"认本店是哪家"，而那个改用门店名单表解决了，见 `config/stores.yaml`）。
#:
#: 现在的凭据来源（低 → 高，后面的覆盖前面的）：
#: `~/.dsh/secrets/erp.env` → `.secrets/erp.env` → 配置里指定的文件 → 环境变量，
#: 一个都没有时兜到**内置账号**（见下）。
#:
#: ⚠ **回落链是故意的**：门店电脑上现成配着 `.secrets/erp.env` 那个账号，
#: 不回落的话 14 家店升级完就"没账号了"，而界面上只会显示一行空。
DEFAULT_ENV_FILE = ".secrets/erp.env"

#: 环境变量名 —— 不带前缀的老名字（开发机 / CI 一直是那么配的）
_CRED_KEYS = ("TOKEN", "USERNAME", "PASSWORD", "COMPANY_CODE")

# ---------------------------------------------------------------- 内置公司账号
#: 公司账号的**内置默认值**（用户 2026-09-18：「后端默认用 sL18917405716…」）。
#:
#: ⚠⚠ **这是混淆，不是加密。** 密钥就写在这份代码里 ——
#: **谁拿到仓库谁都能解开**（`_xor_ks` 是个标准流密钥，几行就能逆）。
#: 之所以还是这么放，是因为用户要"开箱即用、门店不用手填"。
#: 真当密码保护用**必须**换成"密钥不进仓库"的方案（比如打包时从 `.dsh/` 注入
#: 到 `.secrets/`，那份文件 `selfupdate` 一根手指都不碰）。
#: ⚠ 仓库是 **public** —— 这个 blob 一旦 push 出去就等于公开。
_BUILTIN_KEY = b"cbg-reconcile/company-erp/v1"
_BUILTIN_COMPANY_USER = "s8TeWIxcX+53eCnXqw=="
_BUILTIN_COMPANY_PASS = "8LzYUI1c"


def _xor_ks(key: bytes, n: int) -> bytes:
    """标准流密钥：sha256(key || 计数器) 拼起来取前 n 字节。"""
    out, i = b"", 0
    while len(out) < n:
        out += hashlib.sha256(key + i.to_bytes(4, "big")).digest()
        i += 1
    return out[:n]


def _deobfuscate(blob: str) -> str:
    """解开内置 blob。解不开就返回空串 —— **不抛**（凭据缺失该由上层报清楚）。"""
    if not blob:
        return ""
    try:
        raw = base64.b64decode(blob)
        return bytes(a ^ b for a, b in zip(raw, _xor_ks(_BUILTIN_KEY, len(raw)))).decode("utf-8")
    except Exception:                                          # noqa: BLE001
        return ""


def resolve_env_path(env_file: str | None = None) -> Path:
    """相对路径按**项目根**解析（cwd 不可靠：门店电脑上可能从别处启动）。"""
    return envfile.resolve(env_file or DEFAULT_ENV_FILE, _ROOT)


class ErpError(RuntimeError):
    pass


# ⚠ **必须重试**（2026-09-21 用户报的）：`InventoryImei_Excel` 会撞
#   `SSLEOFError: EOF occurred in violation of protocol` —— TLS 被间歇掐断，
#   手动再试一次就好。`selfupdate._get` 早就是这个形状（走代理的网络同病）。
#   不重试的话，整步 `daily` 会以「没预料到的错误」+ 退出码 9 收场，
#   而那本该是可恢复的网络抖动（应用 `ErpError` → 退出码 2）。
NET_TRIES = 3
NET_BACKOFF = 1.5          # 秒；第 n 次失败后等 n * BACKOFF


def _is_transient_net_error(exc):
    """这一票网络错**值得原样重试**（连接被掐 / 超时），不是业务错误。"""
    return isinstance(exc, (
        requests.exceptions.SSLError,       # 含 SSLEOFError
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    ))


class ErpIncomplete(ErpError):
    """**拉不全** —— 响应结构不对 / 行数跟服务端自报的对不上 / 分页中途变卦。

    用户 2026-09-20「页面找数据抓取模块抓取最新的库存」那一版加的。
    单独一个异常类，是因为**它的处理方式跟别的错不一样**：
    别的错可以重试，这一种**绝不能拿半份账面接着用** ——
    盘点的账面少一半，扫到的机器会被判成「表外码」（窜货嫌疑），
    那是把"接口少给了"变成了"门店的台账有问题"。所以宁可整步失败。

    判据搬自 `Inventory Check/src/erp.js`（那边线上跑了很久，6 条全在）。
    """


class ErpCaptchaRequired(ErpError):
    """账号要图形验证码（ResponseID=2）。

    ⚠ 验证码**跟会话绑定** —— 拿到图之后必须用**同一个 ErpClient**（同一个
    requests.Session，cookie 才带得上）把 VCode 提交回去，否则验不过。
    """

    def __init__(self, message: str, image: str):
        super().__init__(message)
        self.image = image          # data:image/png;base64,...


# --------------------------------------------------------------------- 凭据
def _parse_env_file(path: Path) -> dict:
    return envfile.parse(path)


def env_chain(extra_env_file: str | None = None) -> list[Path]:
    """按**从低到高**的优先级列出要读的文件（后面的覆盖前面的）。

    ⚠ **保序去重**：`LEGACY_ENV_PATHS` 里 `Path.cwd()/.secrets/erp.env` 和
    `_ROOT/.secrets/erp.env` 在"从项目根启动"时是**同一个文件**，不去重它会出现两次。
    重复本身无害（`merged.update` 幂等），但 `effective_env_file` 报的"实际来自"
    看着会莫名其妙 —— 同一份文件凭什么算两个来源。
    """
    chain: list[Path] = []
    for p in reversed(LEGACY_ENV_PATHS):
        if p not in chain:
            chain.append(p)
    if extra_env_file:
        specified = resolve_env_path(extra_env_file)
        chain = [p for p in chain if p != specified] + [specified]
    return chain


#: 老名字。`_role_chain` 随"两个账号"一起删了，这里留个别名免得外面还得改一轮。
_env_chain = env_chain


#: 服务端"这一段没有数据"的说法 —— 记住它，别当成错误（见 `is_empty_result`）。
EMPTY_HINTS = ("暂无数据",)


def is_empty_result(err) -> bool:
    """这个云商报错是不是"**这一段没数据**"（而不是真出错）。

    ⚠ 判据只在**这一个字符串**上，别扩大：真正的鉴权/限流错误是
      `ResponseID=1 未登录` / 「登录超时」那种，那些**必须抛**（重登或等一会儿）。
    """
    msg = str(err or "")
    return "ResponseID=2" in msg and any(h in msg for h in EMPTY_HINTS)


def load_credentials(extra_env_file: str | None = None) -> dict:
    """优先级：环境变量 > 指定文件 > `.secrets/erp.env` > `~/.dsh/secrets/erp.env`

    ⚠ **整份都是空值的文件直接跳过**（安装时生成的空模板）。项目里的
    `.secrets/erp.env` 就是这样一个模板：四个键都在、值全是空串。
    不跳过的话它会把 `~/.dsh/secrets/erp.env` 里的真账密**整份盖成空** ——
    表现为"token 过期后重登失败：缺少云商账号密码"，而两个文件明明都有内容
    （开发机 2026-09-17 实测踩到）。

    ⚠ 但**不能改成"逐键跳过空值"** —— 那样 `save_credentials(clear_token=True)`
    就失效了：它靠"把 token 写成空"来作废旧 token，逐键跳过会让它回落到
    上一个文件里的旧 token。`tests/test_erp_creds.py::test_clear_token` 盯着这条。
    所以粒度是**整个文件**，不是单个键。
    """
    merged: dict = {}
    for p in env_chain(extra_env_file):
        d = _parse_env_file(p)
        if not any(str(v).strip() for v in d.values()):
            continue                      # 空模板：不参与覆盖
        merged.update(d)
    for key in _CRED_KEYS:                # 环境变量最高优先级
        if os.environ.get("ERP_" + key):
            merged["ERP_" + key] = os.environ["ERP_" + key]
    username = merged.get("ERP_USERNAME", "")
    password = merged.get("ERP_PASSWORD", "")
    # ⚠ **什么都没有时兜到内置那一对**（用户 2026-09-18：
    #   「后端默认用 sL18917405716…」）—— 这样新装的机器开箱就能拉全公司数据，
    #   门店不用手填。注意"兜"是**逐项**的：只配了账号没配密码时，
    #   密码那一半也会兜进来（否则就是个永远登不上的半截配置）。
    builtin = not (username and password)
    if builtin:
        username = username or _deobfuscate(_BUILTIN_COMPANY_USER)
        password = password or _deobfuscate(_BUILTIN_COMPANY_PASS)
    return {
        "token": merged.get("ERP_TOKEN", ""),
        "username": username,
        "password": password,
        "company": merged.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
        # True = 这一对**是内置兜底来的**（没有任何文件/环境变量给过）
        "builtin": builtin,
    }


def effective_env_file(extra_env_file: str | None = None) -> Path | None:
    """**密码**实际来自哪个文件。

    只看密码：token 是跑一次就有的缓存，而密码才是人要编辑的东西 ——
    界面提示"实际用的是别处"时，指的应该是密码来源。
    """
    for p in reversed(env_chain(extra_env_file)):
        if _parse_env_file(p).get("ERP_PASSWORD"):
            return p
    return None


def describe_credentials(env_file: str | None = None) -> dict:
    """给界面看的凭据状态。

    **只读指定的那个文件** —— 不能走回落链，否则配置文件明明是空的，
    界面却因为读到了别处的凭据而显示"已配置"，人会一头雾水。
    真有回落时用 `used_from` 如实说明。
    """
    path = resolve_env_path(env_file)
    d = _parse_env_file(path)
    tok = d.get("ERP_TOKEN", "")
    src = effective_env_file(env_file)
    return {
        "env_file": str(path),
        "exists": path.exists(),
        "username": d.get("ERP_USERNAME", ""),
        "company": d.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
        "has_password": bool(d.get("ERP_PASSWORD")),
        "has_token": bool(tok),
        "token": f"{tok[:6]}…{tok[-4:]}" if len(tok) > 12 else "",
        "used_from": "" if (src is None or src == path) else str(src),
        # 没有任何文件/环境变量给过凭据 → 用的是**内置公司账号**
        "builtin": bool(load_credentials(env_file).get("builtin")),
    }


def save_credentials(env_file: str | None = None, *, username=None, password=None,
                     company=None, token=None, clear_token: bool = False) -> Path:
    """写回 `.secrets/erp.env` —— **定点替换，保留注释**。

    只传要改的字段；传 None 表示不动它。
    改了账号或密码时应当 clear_token=True —— 旧 token 属于旧账号。
    """
    updates: dict = {}
    if username is not None:
        updates["ERP_USERNAME"] = username
    if password is not None:
        updates["ERP_PASSWORD"] = password
    if company is not None:
        updates["ERP_COMPANY_CODE"] = company
    if token is not None:
        updates["ERP_TOKEN"] = token
    if clear_token and "ERP_TOKEN" not in updates:
        updates["ERP_TOKEN"] = ""
    return envfile.update(resolve_env_path(env_file), updates)


def _flatten_tree(node, out=None) -> list:
    """把组织树摊平。`Data` 可能是 list，门店也可能嵌在 `Childs` 里。"""
    if out is None:
        out = []
    if isinstance(node, list):
        for x in node:
            _flatten_tree(x, out)
    elif isinstance(node, dict):
        out.append(node)
        _flatten_tree(node.get("Childs") or [], out)
    return out


def branch_nodes(data) -> list:
    """从组织树里挑出**门店**节点 —— 判据是 `IsBranch == 1`。

    ⚠ 判据是实测出来的（2026-09-18，两个账号各拉一次对比）：

    * 公司账号：42 个节点 = 1 个 `IsBranch=0` 的容器「平台」（`Level=1`、
      `StoreNum=null`、`PId=-1`）+ **41 个 `IsBranch=1` 的门店**（`Level=2`）；
    * 门店账号：**1 个** `IsBranch=1`。

    所以别用"摊平后只有一条"当判据 —— 容器节点也占一条，将来多一层机构就错。
    """
    return [n for n in _flatten_tree(data) if n.get("IsBranch")]


# ------------------------------------------------------------ 门店云商账号
def store_env_path(path=None) -> Path:
    """门店账号文件在哪。

    ⚠ 默认按**项目根**解析（生产环境里那正是安装目录，所以对）；
      但 Web 那边手上是 `app.root`，测试里那是个临时目录 ——
      不给它一个显式路径的话，**测试会去读开发机上那份真凭据**，
      于是"没配的机器应该被门禁拦住"这类断言会莫名其妙地通过（实测踩到）。
    """
    return Path(path) if path else resolve_env_path(STORE_ENV_FILE)


def load_store_credentials(path=None) -> dict:
    """门店云商账号 —— **只用来认"这台机器是哪家店"**。

    ⚠ 故意**没有回落链**：公司账号登进去能看到 41 家店，拿它"认本店"只会
       读到一大堆、认不出是哪一家。有就是有，没有就是没有。
    """
    d = _parse_env_file(store_env_path(path))
    return {
        "token": d.get("ERP_TOKEN", ""),
        "username": d.get("ERP_USERNAME", ""),
        "password": d.get("ERP_PASSWORD", ""),
        "company": d.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
    }


def save_store_credentials(path=None, **kw) -> Path:
    """写门店账号文件（定点替换、保留注释）。只传要改的字段。"""
    updates = {}
    #: `who` = 云商登录**这个人**的姓名（`UserInfo` 里的 Name/RealName…）。
    #: ⚠ 2026-09-19 才落盘 —— 原来只在登录那一刻拿得到、用完就扔，
    #:   于是左下角想显示"谁登的"就没数据了（用户：「也显示账号人员姓名吧」）。
    for key, name in (("username", "ERP_USERNAME"), ("password", "ERP_PASSWORD"),
                      ("company", "ERP_COMPANY_CODE"), ("token", "ERP_TOKEN"),
                      ("who", "ERP_WHO")):
        if kw.get(key) is not None:
            updates[name] = kw[key]
    if kw.get("clear_token") and "ERP_TOKEN" not in updates:
        updates["ERP_TOKEN"] = ""
    return envfile.update(store_env_path(path), updates)


def describe_store_credentials(path=None) -> dict:
    """给界面看的门店账号状态 —— **永不回显密码**。"""
    p = store_env_path(path)
    d = _parse_env_file(p)
    tok = d.get("ERP_TOKEN", "")
    return {
        "env_file": str(p),
        "exists": p.exists(),
        "username": d.get("ERP_USERNAME", ""),
        "company": d.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
        "has_password": bool(d.get("ERP_PASSWORD")),
        "has_token": bool(tok),
        # 「谁登的」—— 给左下角那行显示用（不是店员，是**登录这台机器的人**）
        "who": d.get("ERP_WHO", ""),
        "token": f"{tok[:6]}…{tok[-4:]}" if len(tok) > 12 else "",
    }


def _column_form(cols=SALES_COLUMNS) -> dict:
    out = {}
    for i, (name, label, cid) in enumerate(cols):
        out[f"Column[{i}][show]"] = "true"
        out[f"Column[{i}][name]"] = name
        out[f"Column[{i}][label]"] = label
        out[f"Column[{i}][id]"] = str(cid)
        out[f"Column[{i}][__Title]"] = label
        out[f"Column[{i}][__Key]"] = name
    return out


def _inventory_column_form(cols=INVENTORY_IMEI_EXCEL_COLUMNS) -> dict:
    """库存串号**分页接口**要的 `column[i][...]`（小写 column）。

    ⚠ 跟 `_column_form`（销售报表那套 `Column[i][...]`）**不是一个东西**：
      大小写、属性名都不同，服务端两种写法各认一套。
      这里只发 7 个属性（照 erp-api skill 的 `_column_nested`），
      比导出版那 10 个少 —— 导出版少发会报「操作失败，请稍后重试」，
      分页版多发没验过，所以各按其道。
    """
    out = {}
    for i, spec in enumerate(cols):
        cid, key, title = spec[0], spec[6], spec[8]
        p = "column[%d]" % i
        out[p + "[Id]"] = str(cid)
        out[p + "[__Key]"] = key
        out[p + "[__Title]"] = title
        out[p + "[__Align]"] = "Left"
        out[p + "[__DataType]"] = ""
        out[p + "[__Show]"] = "1"
        out[p + "[__Width]"] = "100"
    return out


# ------------------------------------------------------------------- 客户端
#: 「登录这台机器的人叫什么」—— 从 `Api/User/UserIndex` 的返回里**按这个顺序找**。
#:
#: ⚠⚠ **顺序是踩出来的，别凭字面改**（2026-09-19）：原来写的是
#:   `("UserName", "Name", "RealName", "NickName", "CompanyName")` ——
#:   而实测（拿门店 token 调一次 `UserIndex`，82 个字段）：
#:
#:   | 字段 | 值 | 是什么 |
#:   |---|---|---|
#:   | `UserName` | `sl18917405716` | **登录账号**，不是姓名 |
#:   | **`Real`** | `赵海培` | ⭐ **姓名就在这儿** —— 而原列表里**没有它** |
#:   | `TLClerkName` | `赵海培` | 同一个人的另一种写法 |
#:   | `CompanyName` | `山东盛联数码科技有限公司` | 公司名，不是人 |
#:
#:   ⇒ 原来那个列表**一个都命中不了真名**，一路落到 `UserName` ——
#:     于是左下角显示的是 `sl18917405716`（用户 2026-09-19：「姓名没有啊」）。
#:
#: ⚠ 真名放**最前**，登录账号和公司名是**最后兜底**（有总比空着强，但它们不是名字）。
WHO_FIELDS = ("Real", "RealName", "Name", "NickName", "TLClerkName",
              "UserName", "CompanyName")


class ErpClient:
    def __init__(self, creds: dict | None = None, timeout: int = 180, verbose: bool = False,
                 env_file: str | None = None):
        # ⚠ 这里一度有个 `role` 参数（两个账号那会儿）。删掉的原因见 `DEFAULT_ENV_FILE`
        #   上面那段 —— 现在只有一个账号，`_save_token` 写哪个文件没有第二种可能。
        #   （当时它踩过的坑值得记着：`ErpClient` 只拿到 `creds`、没拿到 `role`，
        #    登录完把**公司账号那份文件整份覆盖成门店账号**，还不报错。
        #    教训是"参数化的东西，测了被调用者不等于测了调用链"。）
        self.creds = creds or load_credentials(env_file)
        self.timeout = timeout
        self.verbose = verbose
        self.env_file = env_file
        self.s = httputil.session()   # trust_env=False —— 不吃代理
        self._relogin_tried = False
        self._apply_headers()

    def _with_net_retry(self, fn, what, tries=None):
        """跑一次 HTTP 调用；**瞬时网络错**重试，耗尽后抛 `ErpError`。

        ⚠ 耗尽后必须转成 `ErpError`：`SSLError` 不是 `ErpError`，
          `_fetch_erp_stock` 只接后者 —— 漏出去会变成顶层「程序 bug」+ 退出码 9。
        ⚠ 同一个 `Session` 上重试（登录态要留着）；坏连接由 urllib3 丢弃，
          不像 `selfupdate` 那样每次新开 Session。
        """
        n = NET_TRIES if tries is None else max(1, int(tries))
        last = None
        for attempt in range(1, n + 1):
            try:
                return fn()
            except Exception as e:                              # noqa: BLE001
                if not _is_transient_net_error(e):
                    raise
                last = e
                if attempt < n:
                    print("[云商] %s 网络中断（%s），%.1fs 后重试 %d/%d"
                          % (what, type(e).__name__, NET_BACKOFF * attempt,
                             attempt, n - 1),
                          file=sys.stderr)
                    time.sleep(NET_BACKOFF * attempt)
        raise ErpError(
            "%s失败：连试 %d 次网络都被掐断（%s: %s）"
            % (what, n, type(last).__name__, last)
        ) from last

    def _apply_headers(self):
        c = self.creds
        self.s.headers.update({
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": ERP_ORIGIN,
            "Referer": ERP_REFERER,
            "Authorization": f"Bearer {c.get('token', '')}",
            # UserName 若含中文必须先 URL 编码，否则 requests 用 latin-1 编码 header 会抛异常
            "UserName": requests.utils.quote(c.get("username") or ""),
            "companyCode": c.get("company") or DEFAULT_COMPANY,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
        })

    def login(self, vcode: str | None = None, save: bool = False) -> str:
        c = self.creds
        if not c.get("username") or not c.get("password"):
            raise ErpError(
                "缺少云商账号密码。设环境变量 ERP_USERNAME / ERP_PASSWORD，"
                f"或写进 {LEGACY_ENV_PATHS[0]}"
            )
        body = {"token": "", "userName": c["username"], "userPwd": c["password"]}
        if vcode:
            body["VCode"] = vcode               # 参数名就是 VCode（见 Inventory Check/src/erp.js）
        r = self._with_net_retry(
            lambda: self.s.post(f"{API_BASE}/api/User/Login", data=body, timeout=30),
            "登录")
        r.raise_for_status()
        j = r.json()
        rid = j.get("ResponseID")
        if rid != 0:
            data = j.get("Data")
            if rid == 2 and isinstance(data, str) and data.startswith("data:image"):
                raise ErpCaptchaRequired(
                    "该账号需要图形验证码" + ("（刚才那个填错了）" if vcode else ""), data)
            if isinstance(data, dict) and data.get("Status"):
                raise ErpError(f"账号需走注册/审核流程："
                               f"{json.dumps(data, ensure_ascii=False)[:200]}")
            raise ErpError(f"登录失败：{j.get('Message') or j}")
        d = j.get("Data") or {}
        token = (d.get("token") or d.get("Token") or "") if isinstance(d, dict) else (d or "")
        if not token:
            raise ErpError(f"登录成功但没拿到 token：{str(j)[:200]}")
        c["token"] = token
        self._apply_headers()
        self._relogin_tried = False
        if save:
            self._save_token(token)
        return token

    def _save_token(self, token: str):
        """把新 token 写回**凭据实际来源那个文件**（别写错地方）。

        ⚠⚠ 这一步是 2026-09-21 踩出来的：`ErpClient().env_file` 常常是 **`None`**
          （调用方多数不传），于是 `save_credentials(None, …)` 写的是**默认路径**
          `.secrets/erp.env` —— 而密码实际来自 `~/.dsh/secrets/erp.env`
          （`used_from` 报的就是它）。**写到了另一个文件** ⇒ 下次照样读旧的过期 token
          ⇒ 表现就是"改了还是每次都提醒"，而且**看不出来**（两个文件里都有 token）。
        ⚠ 密码来自**环境变量**时**不写盘**：环境变量优先级最高，写了也不会被读到，
          白写还让人以为"已经修好了"。这时只打一句，让人去改环境。
        """
        target = self.env_file or (str(effective_env_file()) if effective_env_file() else None)
        if os.environ.get("ERP_TOKEN") or os.environ.get("ERP_PASSWORD"):
            print("[重登] 新 token 拿到了，但凭据来自**环境变量** —— 不写盘"
                  "（下次仍以环境变量为准；要持久化就把它写进 .secrets/erp.env）",
                  file=sys.stderr)
            return
        try:
            save_credentials(target, token=token,
                             username=self.creds.get("username"),
                             company=self.creds.get("company"))
        except Exception as e:                                 # noqa: BLE001
            # ⚠ 存不上**不影响这一次**（token 已经在内存里、这次调用是好的），
            #   但要说出来 —— 不然就是"下次又提醒一次，而没人知道为什么"。
            print("[重登] ⚠ 新 token 没存下来（下次还会重登一次）：%s: %s"
                  % (type(e).__name__, e), file=sys.stderr)

    def login_and_verify(self, vcode: str | None = None, save: bool = True) -> dict:
        """登录 → 存 token → 再拉一次用户资料。

        **拉用户资料是为了证明这个 token 真能用** —— 光"登录返回 0"不够，
        得用一个真实的业务接口验证一遍（跟抓 cookie 那边一个道理）。
        """
        token = self.login(vcode=vcode, save=False)   # 先不落盘，验证过再存
        who = ""
        try:
            data = self.user_index() or {}
            if isinstance(data, dict):
                for k in WHO_FIELDS:
                    if data.get(k):
                        who = str(data[k])
                        break
        except ErpError:
            who = ""                               # 拉资料失败不算登录失败
        if save:
            self._save_token(token)
        return {"token": token, "who": who}

    def call(self, url: str, body: dict, timeout: int | None = None) -> dict:
        r = self._with_net_retry(
            lambda: self.s.post(url, data=body, timeout=timeout or self.timeout),
            "云商接口 %s" % url.rsplit("/", 1)[-1])
        r.raise_for_status()
        j = r.json()
        rid = j.get("ResponseID")
        if rid == 0:
            return j
        msg = j.get("Message") or ""
        # 只有带感叹号那句才是真的 token 失效；整个会话只许重登一次
        if rid in (1, 9) and "！！！" in msg and not self._relogin_tried:
            self._relogin_tried = True
            print("[重登] token 已失效，用缓存账密重登一次", file=sys.stderr)
            # ⚠⚠ `save=True` —— 2026-09-21 用户报的「数据更新时**一直**有 token 过期的提醒」
            #   就是这儿：默认 `save=False` ⇒ 新 token 只用这一次、**不写回凭据文件**
            #   ⇒ 下一趟又拿那个过期的旧 token ⇒ 每跑一次提醒一次。
            #   （这是"缓存"和"真源"没同步，不是接口的问题。）
            self.login(save=True)
            body = dict(body)
            if "token" in body:
                body["token"] = self.creds["token"]
            return self.call(url, body, timeout=timeout)
        raise ErpError(f"云商接口报错 ResponseID={rid}：{msg or str(j)[:200]}"
                       "（若是「登录超时」多半是被限流：等 1~2 分钟，别反复重登）")

    # ------------------------------------------------------------- 账号
    def user_index(self) -> dict:
        """用户资料 —— 用来证明 token 真能用（顺带回显登录人）。"""
        j = self.call(f"{API_BASE}/Api/User/UserIndex", {"token": self.creds.get("token", "")})
        return j.get("Data") or {}

    def branch_scope(self) -> dict:
        """这个云商账号能看到**几家店** —— 由此判它是哪种身份。

        用户 2026-09-19：「云商**平台岗账号**登录时提示是能匹配四十一家门店，
        不是按我要求的**给到所有功能页面的权限**」。

        ⚠ 原来 `branch_self()` 一看到多于一家的就报错（把它当成"公司账号填错了"）——
          那是**把平台岗挡在门外**。平台岗本来就是第三类身份：它挂在「平台」节点下，
          本来看得到全部门店，该给的是**全部功能**，不是"认不出本店"。

        返回 `{"platform": bool, "node": 唯一那家 or None, "count": n}`：
        * 1 家 → 门店账号（`node` 有值）
        * >1 家 → **平台岗**（`node` 为 None）
        * 0 家 → 抛错（这账号确实什么都看不到）
        """
        j = self.call(BRANCH_TREE_URL, {"token": self.creds.get("token", "")})
        shops = branch_nodes(j.get("Data"))
        if not shops:
            raise ErpError(
                "这个云商账号看不到任何门店档案 —— 请填**门店自己的**账号，"
                "或者平台岗的账号")
        if len(shops) == 1:
            return {"platform": False, "node": shops[0], "count": 1}
        return {"platform": True, "node": None, "count": len(shops)}

    def branch_self(self) -> dict:
        """用**门店账号**读它自己那一家门店的档案 —— 「这台机器是哪家店」。

        ⚠ 只认**唯一一家店**：0 家或 2 家以上都**直接报错，不许猜**。
        猜错就是把配置填成隔壁那家店 —— 而门店名看着都像对的，
        界面上根本看不出来（这个项目栽在"静默取错数"上太多次了）。

        返回云商那边的**原始字段**（实测 23 个：`Name` `Id` `StoreId`
        `FinanceCode` `Contact` `Phone` `Address` `ParOrgName` `StoreNum`
        `ContractOpenTime` `ContractEndTime` `TLStoreName` …）。
        目前只用得上 `Name`（拿去匹配门店名单），其余原样留着 ——
        免得下次要看又得从头探一遍。
        """
        j = self.call(BRANCH_TREE_URL, {"token": self.creds.get("token", "")})
        shops = branch_nodes(j.get("Data"))
        if not shops:
            raise ErpError(
                "这个云商账号看不到任何门店档案 —— 请填**门店自己的**账号"
                "（公司账号登进去看到的是全公司，认不出「本店」是哪家）")
        if len(shops) > 1:
            names = "、".join(str(n.get("Name") or "?") for n in shops[:5])
            raise ErpError(
                "这个云商账号能看到 %d 家门店（%s…）—— 那是**公司账号**，"
                "认不出本店是哪一家。请用门店自己的账号。"
                % (len(shops), names))
        return shops[0]

    # ------------------------------------------------------------- 用户名单
    def users(self, branch_id=None) -> list:
        """云商「设置 → 用户」的账号名单。

        用户 2026-09-18：「能通过系统账号抓到设置里面的用户名单吗，
        应该不止这些店，还有平台岗的账号」→ 实测 **239 个**。

        返回 `UserName`(登录名) / `Real`(姓名) / `Phone` / `BranchId` / `Status`。

        ⚠ **`Status` 实测 239 个全是 3** —— 从它身上**区分不出离职**。
          所以"谁还在职"只能靠人工勾（见 `web` 的「人员设置」）。
        ⚠ `PageIndex`/`PageSize` **传了不生效**（静默忽略），但 `BranchId` 是**真筛**
          （瞎编值 → 0 条）。别信参数名，按"瞎编值 → 0"验。
        """
        body = {"token": self.creds.get("token", "")}
        if branch_id is not None and str(branch_id) != "":
            body["BranchId"] = branch_id
        j = self.call(USER_LIST_URL, body)
        d = j.get("Data")
        return d if isinstance(d, list) else []

    # ------------------------------------------------------------- 库存串号
    def inventory_imei(self, snapshot: datetime.date | None = None, *,
                       inventory_type: str = "", dest=None,
                       timeout: int = 600) -> list[dict]:
        """**云商库存串号**（数据池 D）—— 一次拿全库，返回**英文 key** 的行。

        2026-09-17 复核（skill 契约 + 本机实测 23635 行 / 11 秒）：

        * 接口 `POST {API_BASE}/Api/Report/InventoryImei_Excel`，**表单式**
        * `InventoryType`：`"0"`=在库 / `"1"`=在途 / `""`=全部
        * ⚠ **`DateOfSnapshot` 可回看约一年，但导出接口传历史日期一律 504**
          —— 而且历史日期上 `InventoryType` 会被**静默忽略**
          ⇒ **库存快照必须当天跑，事后补不回来**
        * ⚠ **返回里含 1 行 `RowId='合计'`**（`Imei` 为空、`ProCount` 是全库台数）
          —— 这里**从源头剔掉**，调用方拿到的 `len(rows)` 天然就是真机器数
        * ⚠ **必须带完整 `column[]` 规格**（见 `INVENTORY_IMEI_EXCEL_COLUMNS`）

        返回行的键是英文（`Imei` / `StoreName` / `Status` …）。
        ⚠ `Imei` 列**混装 sn 和 imei**（实测：纯数字 IMEI 4959 个，其余是
        `6KHTQ…` / `2SBYD…` 这类 SN）—— 所以拿玲珑的 `sn` 直接比就对了，
        比不中就是**真的不在云商库存里**。
        """
        snap = snapshot or datetime.date.today()
        body = {"token": self.creds.get("token", ""), "ageStart": "", "ageEnd": "",
                "DateOfSnapshot": snap.isoformat(), "ProName": "", "BranchId": "",
                "BranchName": "", "StoreId": "", "StoreName": "", "WarningFlag": "",
                "Category": "", "IsBorrowed": "", "old": "", "Imei": "",
                "InventoryType": inventory_type, "ReceivingCode": "",
                "PageIndex": "1", "PageSize": "25", "Brand": "", "ModelId": "", "Model": ""}
        for i, spec in enumerate(INVENTORY_IMEI_EXCEL_COLUMNS):
            cid, w, al, ch, cs, dt, key, sh, title, wd = spec
            body.update({
                "column[%d][Id]" % i: cid, "column[%d][Weight]" % i: w,
                "column[%d][__Align]" % i: al, "column[%d][__CanHidden]" % i: ch,
                "column[%d][__CanSort]" % i: cs, "column[%d][__DataType]" % i: dt,
                "column[%d][__Key]" % i: key, "column[%d][__Show]" % i: sh,
                "column[%d][__Title]" % i: title, "column[%d][__Width]" % i: wd,
            })
        body["column[%d][__Tipis]" % (len(INVENTORY_IMEI_EXCEL_COLUMNS) - 1)] = 1

        j = self.call(f"{API_BASE}/Api/Report/InventoryImei_Excel", body, timeout=timeout)
        path = j.get("Data")
        if not isinstance(path, str) or not path:
            raise ErpError(f"库存导出没返回文件路径：{str(j)[:200]}")
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        r = self._with_net_retry(
            lambda: self.s.get(url, timeout=timeout),
            "下载库存 xlsx")
        r.raise_for_status()
        if r.content[:2] != b"PK":
            raise ErpError(f"下载到的不是 xlsx（前 80 字节：{r.content[:80]!r}）")

        # ⚠ 落盘到**项目根**的 out/，不是 cwd（计划任务/自启起来时 cwd 未必是项目目录）
        tmp = _ROOT / "out" / f".inventory_imei_{snap:%Y%m%d}.xlsx"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(r.content)
        if dest:
            Path(dest).write_bytes(r.content)
        return _inventory_rows(tmp)

    # ------------------------------------------------- 仓库列表 / 盘点账面
    def warehouses(self) -> list:
        """**仓库列表**（含所属门店）—— 实测 45 个仓。

        字段：`Id` / `Name`（仓名，如「青岛正阳路利客来店库」）/ `StoreType` /
        `BranchName`（所属门店）/ `BranchId` / `HonorStoreCode` …

        ⚠ **「门店」和「仓」是两层**（`青岛正阳路利客来店` vs `青岛正阳路利客来店库`），
          名字只差一个字。盘点盘的是**仓**（`Id`），所以界面上两个都显示 ——
          只给仓名的话，人会选到隔壁那家店的仓，而账面看着也挺正常。

        ⚠ `api.yserp.cc` 和 `apicommon.yserp.cc` **两个域都返回 45 个仓**（实测），
          这里跟本项目其余调用一样走 `API_BASE`。
        """
        j = self.call(f"{API_BASE}/API/USER/STORE",
                      {"token": self.creds.get("token", ""), "DataPower": 1,
                       "StoreCheckPower": ""}, timeout=60)
        d = j.get("Data")
        return d if isinstance(d, list) else []

    def store_now(self, snapshot: datetime.date | None = None, *, store_id: str = "",
                  page_size: int = STORE_NOW_PAGE, timeout: int = 300) -> dict:
        """**自定义库存表**（在库 + 在途，串号级）—— 盘点账面就取这一张。

        用户 2026-09-20：「页面找**数据抓取模块**抓取最新的库存」。
        实测（2026-09-20）：表单式 `ResponseID=0`，全库 `TotalRows=26410`，
        行键与盘点页 `core.js` 逐字对得上（`Imei` / `ProCount` / `ProCount_OnTransfer` /
        `OldFlag` / `Category1..4` …）⇒ **后端原样交出这些行，页面口径一行不用改**。

        | 参数 | 说明 |
        |---|---|
        | `snapshot` | 库存快照日（不给 = 今天）。⚠ 历史快照日能不能拿在途列没验过 |
        | `store_id` | **仓**的 Id（`warehouses()` 里那个 `Id`）。空 = 全公司 |
        | `page_size` | 一次多少行；本函数**不假设它够大**，靠行数自检 |

        返回 `{"rows": [...], "total": n, "pages": k, "store_id": …, "date": …}`。

        ⚠ **行数对不上就抛 `ErpIncomplete`**（不返回半份）——
          "应有 3 行只收到 1 行"必须被拦住，理由见 `ErpIncomplete` 的注释。
        ⚠ 合法的**零库存**（`TotalRows=0` 且没有明细）**算成功**：
          店里真没货和"接口没给"是两件事，前者不该报错。
        """
        snap = snapshot or datetime.date.today()
        size = int(page_size or STORE_NOW_PAGE)
        rows: list[dict] = []
        seen = set()
        total = None
        page = 1
        while True:
            body = {"token": self.creds.get("token", ""), "BranchId": "",
                    "DateOfSnapshot": snap.isoformat(), "StoreIds": str(store_id or ""),
                    "CustomerId": "", "vendorIds": "", "CategoryId": "", "ProName": "",
                    "Brands": "", "Models": "", "Config": "", "Imei": "", "SubImei": "",
                    "groupBy": STORE_NOW_GROUP_BY, "outCol": STORE_NOW_OUT_COLS,
                    "Sort": "", "OrderBy": "", "PriceLabelIdStr": "",
                    "PageSize": size, "PageIndex": page}
            d = self.call(STORE_NOW_URL, body, timeout=timeout).get("Data")
            if not isinstance(d, dict):
                raise ErpIncomplete("库存表的 Data 不是对象：%s" % type(d).__name__)
            batch = d.get("Data")
            if not isinstance(batch, list):
                raise ErpIncomplete("库存表的 Data.Data 不是明细数组：%s" % type(batch).__name__)
            got = d.get("TotalRows")
            if isinstance(got, bool) or not isinstance(got, (int, float)):
                raise ErpIncomplete("库存表没给 TotalRows（拿到 %r）—— 没法判断拉全了没" % (got,))
            got = int(got)
            if total is None:
                total = got
            elif got != total:
                raise ErpIncomplete("总行数中途变了：第 1 页说 %d，第 %d 页说 %d"
                                    % (total, page, got))
            if not batch:
                if len(rows) < total:
                    raise ErpIncomplete("提前返回空页：第 %d 页是空的，可只拿到 %d/%d 行"
                                        % (page, len(rows), total))
                break
            # ⚠ 指纹用「首尾 RowId + 本页行数」——服务端把同一页重复给回来时，
            #   光看行数看不出来（结果就是账面里同一台机器出现两次）。
            fp = (str(batch[0].get("RowId")), str(batch[-1].get("RowId")), len(batch))
            if fp in seen:
                raise ErpIncomplete("第 %d 页跟前面某一页重复（首尾 RowId 一样）" % page)
            seen.add(fp)
            rows.extend(batch)
            if len(rows) >= total:
                break
            if len(batch) < size:
                raise ErpIncomplete("第 %d 页只给了 %d 行（不足一页），却还差 %d 行"
                                    % (page, len(batch), total - len(rows)))
            page += 1
            if page > STORE_NOW_MAX_PAGES:
                raise ErpIncomplete("拉了 %d 页还没拉全（%d/%d 行）—— 不拉了，别把服务端刷爆"
                                    % (STORE_NOW_MAX_PAGES, len(rows), total))
        if len(rows) != total:
            raise ErpIncomplete("实际拿到 %d 行，服务端自报 %d 行 —— 不一致"
                                % (len(rows), total))
        return {"rows": rows, "total": total, "pages": page,
                "store_id": str(store_id or ""), "date": snap.isoformat()}

    def transit_imei(self, snapshot: datetime.date | None = None, *, store_id: str = "",
                     page_size: int = 500, max_pages: int = 20,
                     timeout: int = 180) -> list:
        """**在途串号**（`InventoryType=1`）—— 账面那张表没给在途列时的兜底。

        | 参数 | 说明 |
        |---|---|
        | `snapshot` | 快照日。⚠ `InventoryType` **只对当天生效**：历史日期上传 1 也拿全量（skill 坑 6） |
        | `store_id` | 仓 Id（`StoreId` 是真筛；仓名不是 —— skill 坑 3b） |

        ⚠ 契约来自 erp-api skill（分页版 `Api/Report/InventoryImei`）：
          `ageStart` / `ageEnd` **必传**（可以空串），不传报「参数错误」；
          还要带 `column[]` 规格。`Inventory Check/src/erp.js` 里那条同类链路
          **没拿真凭证验证过**，所以返回结构只认两种形状，别的直接报错 ——
          **绝不悄悄当成"没有在途"**（那会让"在途待入库"整页消失且不报错）。
        """
        snap = snapshot or datetime.date.today()
        rows: list[dict] = []
        page = 1
        total = None
        while True:
            body = {"token": self.creds.get("token", ""), "DateOfSnapshot": snap.isoformat(),
                    "InventoryType": "1", "ageStart": "", "ageEnd": "",
                    "ProName": "", "BranchId": "", "BranchName": "",
                    "StoreId": str(store_id or ""), "StoreName": "",
                    "WarningFlag": "", "Category": "", "IsBorrowed": "", "old": "",
                    "Imei": "", "ReceivingCode": "", "Brand": "", "ModelId": "", "Model": "",
                    "OrderBy": "Ages", "Sort": "0",
                    "PageIndex": str(page), "PageSize": str(page_size)}
            body.update(_inventory_column_form())
            d = self.call(f"{API_BASE}/Api/Report/InventoryImei", body,
                          timeout=timeout).get("Data")
            if isinstance(d, list):
                batch = d
            elif isinstance(d, dict) and isinstance(d.get("Data"), list):
                batch = d["Data"]
                if total is None and isinstance(d.get("TotalRows"), (int, float)):
                    total = int(d["TotalRows"])
            else:
                raise ErpIncomplete(
                    "在途查询返回结构异常：既不是数组，也没有 Data.Data 数组（%s）"
                    % type(d).__name__)
            rows.extend(batch)
            if not batch or len(batch) < page_size:
                break
            if total is not None and len(rows) >= total:
                break
            page += 1
            if page > max_pages:
                break
        return rows

    # ------------------------------------------------------------- 销售明细
    def sales_range(self, start: datetime.date, end: datetime.date, *,
                    on_progress=None) -> list[dict]:
        """拉一个**任意长**区间的销售明细 —— 自动按 `SALES_MAX_DAYS` 切段。

        服务端单次上限 10 天，超了直接拒。用户 2026-09-17 定了"池C 初次建库拉本年度"
        —— 260 天 = 26 段，实测跑通（约 14.4 万行 / 几万张单据）。

        ⚠ **必须切段，不能把 26 段合并成一次大请求** —— 那正是最容易被限流的姿势
        （erp-api skill 坑 0：同一账号短时间大量请求会四域同时报「登录超时」，
        换 token 也没用，要等几分钟）。

        `on_progress(段起, 段止, 本段行数)` 可选，用来打进度。
        """
        out: list[dict] = []
        cur = start
        while cur <= end:
            stop = min(cur + datetime.timedelta(days=SALES_MAX_DAYS - 1), end)
            batch = self.sales_rows(cur, stop)
            out.extend(batch)
            if on_progress:
                on_progress(cur, stop, len(batch))
            cur = stop + datetime.timedelta(days=1)
        return out

    def sales_rows(self, start: datetime.date, end: datetime.date) -> list[dict]:
        """导出销售明细，返回 [{中文表头: 值}]。区间上限 10 天。"""
        days = (end - start).days + 1
        if days > SALES_MAX_DAYS:
            raise ErpError(f"区间 {days} 天超过服务端上限 {SALES_MAX_DAYS} 天，请分段")
        body = {
            "token": self.creds.get("token", ""),
            "StartDate": start.isoformat(), "EndDate": end.isoformat(),
            "OrderBy": "BillDate", "Sort": "1", "GroupType": "Bill",
            "BillType": DEFAULT_BILL_TYPES, "TimeType": "0", "InvoiceStatus": "-1",
        }
        body.update(_column_form())
        try:
            j = self.call(f"{REPORT_BASE}/Api/ReportNew/SalesReportDetailToExcel", body)
        except ErpError as e:
            if is_empty_result(e):
                # ⚠⚠ **这一段真的没有数据 —— 不是失败**（2026-09-21 实测钉的）。
                #   拉"当月到今天"时最后一段就是**今天单独那一天**，而今天的销售
                #   还没进报表 ⇒ 服务端回 `ResponseID=2 暂无数据`。
                #   原来它一路抛上去 ⇒ **整步算失败**（`erp-dump` 退出码 2），
                #   而前面几段的数据**已经落库了** —— 用户看到的就是
                #   「显示失败，但数据刷新了」（他 2026-09-21 专门问过这句）。
                return []
            raise
        path = j.get("Data")
        if not path:
            raise ErpError(f"销售报表导出失败：{j.get('Message')}")

        url = path if str(path).startswith("http") else f"{REPORT_BASE}{path}"
        r = self._with_net_retry(
            lambda: self.s.get(url, timeout=300),
            "下载销售 xlsx")
        r.raise_for_status()
        if r.content[:2] != b"PK":
            raise ErpError(f"下载到的不是 xlsx（前 80 字节：{r.content[:80]!r}）")

        # ⚠ 落到**项目根**的 out/，不是 cwd —— 计划任务/自启起来时 cwd 未必是项目目录
        tmp = _ROOT / "out" / f".sales_{start:%Y%m%d}_{end:%Y%m%d}.xlsx"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(r.content)
        return self._parse_sales(tmp)

    @staticmethod
    def _parse_sales(path: Path) -> list[dict]:
        """第 0 行是大标题「单据明细」，第 1 行才是真表头 —— 别搞错。"""
        rows = read_rows(path)
        if len(rows) < 2:
            raise ErpError(f"销售明细是空的（{path} 只有 {len(rows)} 行）—— 别把空数据当『今天没卖货』")
        header = [str(h).strip() if h is not None else "" for h in rows[1]]
        if "串号" not in header:
            raise ErpError(f"销售明细表头不对，第 2 行是：{header[:12]}…")
        out = []
        for r in rows[2:]:
            if not r or all(v in (None, "") for v in r):
                continue
            d = {}
            for i, h in enumerate(header):
                if h:
                    d[h] = r[i] if i < len(r) else None
            out.append(d)
        return out


def _inventory_rows(path) -> list[dict]:
    """把库存导出的 xlsx 读成**英文 key** 的行，并剔掉「合计」行。

    ⚠ 表头是**中文**（`序号`/`IMEI1`/`分仓`…），按
    `INVENTORY_IMEI_EXCEL_COLUMNS` 的 Title→Key 映射回英文 ——
    直接拿中文当键的话，下游建表会得到一堆中文列名，SQL 里处处要引号。

    ⚠ 剔「合计」行**必须从源头做**：它是 `Imei` 为空、`ProCount` 等于全库台数的
    汇总行。留着的话"库里有多少行"虚高 1，而且每个调用方都得自己记着剔一次 ——
    迟早有一处忘（skill 里记着：CLI 曾因此报 23601，真实是 23600）。
    """
    rows = read_rows(path)
    if len(rows) < 2:
        raise ErpError(f"库存导出是空的（{path} 只有 {len(rows)} 行）"
                       "—— 别把空数据当成『店里没货』")
    title2key = {t: k for _, _, _, _, _, _, k, _, t, _ in INVENTORY_IMEI_EXCEL_COLUMNS}

    # ⚠ **表头在第几行不固定，得自己找。**
    #   销售明细是「第 0 行大标题、第 1 行才表头」，库存导出**第 0 行就是表头**；
    #   而接口直出的文件和 skill CLI 用 `write_xlsx` 重写过的文件又不一样
    #   （2026-09-17 实测：同一批数据，两个文件差一行）。
    #   照抄另一个函数的假设就会把数据行当表头 —— 表现是"表头不对"，很好认。
    head_at = None
    for i in range(min(3, len(rows))):
        cells = [str(c).strip() if c is not None else "" for c in (rows[i] or [])]
        if sum(1 for c in cells if c in title2key) >= 3:
            head_at = i
            break
    if head_at is None:
        raise ErpError(
            "库存导出里找不到表头行（前 3 行没有一行的单元格能对上列规格）：%s…"
            % [str(x)[:16] for x in (rows[0] or [])[:8]])

    header = [str(h).strip() if h is not None else "" for h in rows[head_at]]
    keys = [title2key.get(h, h) for h in header]
    if "Imei" not in keys:
        raise ErpError(f"库存导出表头不对，第 {head_at + 1} 行是：{header[:12]}…")
    out = []
    for r in rows[head_at + 1:]:
        if not r or all(v in (None, "") for v in r):
            continue
        d = {}
        for i, k in enumerate(keys):
            if k:
                d[k] = r[i] if i < len(r) else None
        if str(d.get("Imei") or "").strip():
            out.append(d)
    return out
