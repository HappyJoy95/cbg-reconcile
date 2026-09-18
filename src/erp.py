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

from . import envfile
from .xlsx_io import read_rows

_ROOT = Path(__file__).resolve().parent.parent

API_BASE = "https://api.yserp.cc"
REPORT_BASE = "https://apireport.yserp.cc"
ERP_ORIGIN = "https://erp.yserp.cc"
ERP_REFERER = "https://erp.yserp.cc/"
DEFAULT_COMPANY = "00001937"
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
    ("Imei", "串号", 14), ("OldFlag", "串号标识", 5), ("BillDate", "支付时间", 15),
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

LEGACY_ENV_PATHS = [
    Path.cwd() / ".secrets" / "erp.env",
    _ROOT / ".secrets" / "erp.env",                                    # 项目根，与 cwd 无关
    Path.home() / ".dsh" / "secrets" / "erp.env",
]

DEFAULT_ENV_FILE = ".secrets/erp.env"

#: 两个云商账号（用户 2026-09-18：「需要存两个云商账号，一个是门店的账号，
#: 用来获取门店登录信息，一个是我的最高权限账号，用来拉取全公司的数据」）。
#:
#: * **公司账号**（最高权限）—— 拉**全公司**的数据（池C/池D 那些 `全部门店·云商…`）。
#: * **门店账号** —— 本店的。
#:
#: ⚠ **公司账号要回落读老的 `.secrets/erp.env`**（见 `_role_chain`）：
#: 门店电脑上现成就配着那一个账号，而且它本来就是能看全公司的那个 ——
#: 不回落的话，14 家店升级完就"没账号了"，而界面上只会显示一行空。
ROLE_COMPANY = "company"
ROLE_STORE = "store"
ROLES = (ROLE_COMPANY, ROLE_STORE)
ROLE_LABELS = {
    ROLE_COMPANY: "公司账号（最高权限）",
    ROLE_STORE: "门店账号",
}
#: 角色 → 配置文件（相对项目根）。
#:
#: ⚠ **公司账号就沿用它原来那个 `.secrets/erp.env`** ——
#: 门店机器上现成配着的那一个本来就是能看全公司的（实测它能拉到 42 家店的销售），
#: 另起一个 `erp-company.env` 只会让 14 家店升级完"没账号了"。
#: **门店账号是新文件**，谁都不受影响。
ROLE_ENV_FILES = {
    ROLE_COMPANY: DEFAULT_ENV_FILE,          # .secrets/erp.env（沿用）
    ROLE_STORE: ".secrets/erp-store.env",    # 新增
}
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


#: 角色 → 环境变量前缀。**公司账号额外认不带前缀的老名字**（`ERP_USERNAME` 等）
ROLE_ENV_PREFIX = {ROLE_COMPANY: "ERP_COMPANY_", ROLE_STORE: "ERP_STORE_"}
_CRED_KEYS = ("TOKEN", "USERNAME", "PASSWORD", "COMPANY_CODE")


def resolve_env_path(env_file: str | None = None) -> Path:
    """相对路径按**项目根**解析（cwd 不可靠：门店电脑上可能从别处启动）。"""
    return envfile.resolve(env_file or DEFAULT_ENV_FILE, _ROOT)


class ErpError(RuntimeError):
    pass


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


def _role_chain(role: str, extra_env_file: str | None = None):
    """按**从低到高**的优先级列出这个角色要读的文件（后面的覆盖前面的）。

    * **公司账号**：老路径（`~/.dsh/secrets/erp.env` → `.secrets/erp.env`）
      → `.secrets/erp-company.env` → 显式指定的文件。
      回落老路径是**故意**的，见 `ROLE_COMPANY` 的注释。
    * **门店账号**：只有 `.secrets/erp-store.env`（+ 显式指定的）。
      **故意不回落到老文件** —— 那是公司账号，拿它冒充门店账号
      会让"这台机器到底用哪个账号"变得谁也说不清。
    """
    if role not in ROLES:
        raise ValueError("不认识的角色：%r（只认 %s）" % (role, "、".join(ROLES)))
    chain = list(reversed(LEGACY_ENV_PATHS)) if role == ROLE_COMPANY else []
    role_file = resolve_env_path(ROLE_ENV_FILES[role])
    # ⚠ 去重后再补 —— 公司账号的角色文件**就是**老链里那个 `.secrets/erp.env`，
    #   不去重的话它会被插两次（虽然结果一样，但 `effective_env_file` 看着莫名其妙）
    chain = [p for p in chain if p != role_file] + [role_file]
    if extra_env_file:
        specified = resolve_env_path(extra_env_file)
        chain = [p for p in chain if p != specified] + [specified]
    return chain


def role_env_file(role: str = ROLE_COMPANY) -> Path:
    """这个角色**该写哪个文件**（界面上的「实际来自」也按它算）。"""
    if role not in ROLES:
        raise ValueError("不认识的角色：%r（只认 %s）" % (role, "、".join(ROLES)))
    return resolve_env_path(ROLE_ENV_FILES[role])


def load_credentials(extra_env_file: str | None = None,
                     role: str = ROLE_COMPANY) -> dict:
    """优先级：环境变量 > 指定文件 > 角色文件 > 老文件（仅公司账号） > ~/.dsh/secrets/erp.env

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
    for p in _role_chain(role, extra_env_file):
        d = _parse_env_file(p)
        if not any(str(v).strip() for v in d.values()):
            continue                      # 空模板：不参与覆盖
        merged.update(d)
    # 环境变量最高优先级。公司账号额外认不带前缀的老名字（`ERP_USERNAME` 等）——
    # 开发机/CI 里一直是那么配的，别让升级把它们弄丢。
    prefixes = [ROLE_ENV_PREFIX[role]]
    if role == ROLE_COMPANY:
        prefixes.append("ERP_")
    for prefix in prefixes:
        for key in _CRED_KEYS:
            name = prefix + key
            if os.environ.get(name):
                merged["ERP_" + key] = os.environ[name]
    username = merged.get("ERP_USERNAME", "")
    password = merged.get("ERP_PASSWORD", "")
    # ⚠ **公司账号什么都没有时兜到内置那一对**（用户 2026-09-18：
    #   「后端默认用 sL18917405716…」）—— 这样新装的机器开箱就能拉全公司数据，
    #   门店不用手填。门店账号**不兜**：它本来就该是每家店自己的。
    if role == ROLE_COMPANY and not (username and password):
        username = username or _deobfuscate(_BUILTIN_COMPANY_USER)
        password = password or _deobfuscate(_BUILTIN_COMPANY_PASS)
    return {
        "token": merged.get("ERP_TOKEN", ""),
        "username": username,
        "password": password,
        "company": merged.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
        "role": role,
        # True = 这一对**是内置兜底来的**（没有任何文件/环境变量给过）
        "builtin": bool(username) and not (merged.get("ERP_USERNAME")
                                           or merged.get("ERP_PASSWORD")),
    }


def _env_chain(extra_env_file: str | None = None,
               role: str = ROLE_COMPANY) -> list[Path]:
    """`_role_chain` 的老名字（默认公司账号，行为跟以前一致）。"""
    return _role_chain(role, extra_env_file)


def effective_env_file(extra_env_file: str | None = None,
                       role: str = ROLE_COMPANY) -> Path | None:
    """**密码**实际来自哪个文件。

    只看密码：token 是跑一次就有的缓存，而密码才是人要编辑的东西 ——
    界面提示"实际用的是别处"时，指的应该是密码来源。
    """
    for p in reversed(_env_chain(extra_env_file, role)):
        if _parse_env_file(p).get("ERP_PASSWORD"):
            return p
    return None


def describe_credentials(env_file: str | None = None,
                         role: str = ROLE_COMPANY) -> dict:
    """给界面看的凭据状态。

    **只读指定的那个文件** —— 不能走回落链，否则配置文件明明是空的，
    界面却因为读到了别处的凭据而显示"已配置"，人会一头雾水。
    真有回落时用 `used_from` 如实说明。
    """
    # ⚠ 不传 env_file 时按**角色自己的文件** —— 以前写死 `DEFAULT_ENV_FILE`，
    #   加了两个角色之后还那样就是"门店账号也去读公司那个文件"。
    path = resolve_env_path(env_file) if env_file else role_env_file(role)
    d = _parse_env_file(path)
    tok = d.get("ERP_TOKEN", "")
    src = effective_env_file(env_file, role)
    return {
        "role": role,
        "role_label": ROLE_LABELS.get(role, role),
        "env_file": str(path),
        "exists": path.exists(),
        "username": d.get("ERP_USERNAME", ""),
        "company": d.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
        "has_password": bool(d.get("ERP_PASSWORD")),
        "has_token": bool(tok),
        "token": f"{tok[:6]}…{tok[-4:]}" if len(tok) > 12 else "",
        "used_from": "" if (src is None or src == path) else str(src),
    }


def save_credentials(env_file: str | None = None, *, username=None, password=None,
                     company=None, token=None, clear_token: bool = False,
                     role: str = ROLE_COMPANY) -> Path:
    """写回 .secrets/erp.env —— **定点替换，保留注释**。

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
    # ⚠ 不传 env_file 就写**角色自己的文件** —— 以前写死 `DEFAULT_ENV_FILE`，
    #   加了角色之后还那样就会"改门店账号、结果把公司账号覆盖了"。
    return envfile.update(resolve_env_path(env_file) if env_file else role_env_file(role),
                          updates)


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


# ------------------------------------------------------------------- 客户端
class ErpClient:
    def __init__(self, creds: dict | None = None, timeout: int = 180, verbose: bool = False,
                 env_file: str | None = None, role: str | None = None):
        # ⚠ **角色必须跟着 client 走**：`_save_token` 靠它决定往哪个文件写。
        #
        #   2026-09-18 实测踩过 —— `ErpClient(load_credentials(role=store))`
        #   只给了 `creds`、没给 `role`，登录完 `_save_token` 就按默认的 company
        #   写进 `.secrets/erp.env`，**把公司账号整份覆盖成门店账号**（token 也换了）。
        #   症状很隐蔽：门店账号"配好了"，公司那边从此用门店的身份取数。
        #
        #   所以这里**不靠调用方记得传**：`load_credentials()` 返回的字典里本来就带
        #   `role`，没显式给就从它那儿认。这样
        #   `ErpClient(load_credentials(role=ROLE_STORE))` 这种写法**天然是对的**，
        #   这个 bug 结构上不可能再犯（`tests/test_erp_roles.py::TestClientCarriesRole`）。
        self.role = role or (creds or {}).get("role") or ROLE_COMPANY
        self.creds = creds or load_credentials(env_file, role=self.role)
        self.timeout = timeout
        self.verbose = verbose
        self.env_file = env_file
        self.s = requests.Session()
        self._relogin_tried = False
        self._apply_headers()

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
        r = self.s.post(f"{API_BASE}/api/User/Login", data=body, timeout=30)
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
        # ⚠ role 一定要传 —— 不传就按默认的 company 写，见 `__init__` 那段注释
        save_credentials(self.env_file, role=self.role, token=token,
                         username=self.creds.get("username"),
                         company=self.creds.get("company"))

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
                for k in ("UserName", "Name", "RealName", "NickName", "CompanyName"):
                    if data.get(k):
                        who = str(data[k])
                        break
        except ErpError:
            who = ""                               # 拉资料失败不算登录失败
        if save:
            self._save_token(token)
        return {"token": token, "who": who}

    def call(self, url: str, body: dict, timeout: int | None = None) -> dict:
        r = self.s.post(url, data=body, timeout=timeout or self.timeout)
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
            self.login()
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
        r = self.s.get(url, timeout=timeout)
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
        j = self.call(f"{REPORT_BASE}/Api/ReportNew/SalesReportDetailToExcel", body)
        path = j.get("Data")
        if not path:
            raise ErpError(f"销售报表导出失败：{j.get('Message')}")

        url = path if str(path).startswith("http") else f"{REPORT_BASE}{path}"
        r = self.s.get(url, timeout=300)
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
