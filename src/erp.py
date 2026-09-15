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

import datetime
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

LEGACY_ENV_PATHS = [
    Path.cwd() / ".secrets" / "erp.env",
    _ROOT / ".secrets" / "erp.env",                                    # 项目根，与 cwd 无关
    Path.home() / ".dsh" / "secrets" / "erp.env",
]

DEFAULT_ENV_FILE = ".secrets/erp.env"


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


def load_credentials(extra_env_file: str | None = None) -> dict:
    """优先级：环境变量 > 指定文件 > 项目内 .secrets/erp.env > ~/.dsh/secrets/erp.env"""
    merged: dict = {}
    for p in reversed(LEGACY_ENV_PATHS):
        merged.update(_parse_env_file(p))
    if extra_env_file:
        merged.update(_parse_env_file(resolve_env_path(extra_env_file)))
    for k in ("ERP_TOKEN", "ERP_USERNAME", "ERP_PASSWORD", "ERP_COMPANY_CODE"):
        if os.environ.get(k):
            merged[k] = os.environ[k]
    return {
        "token": merged.get("ERP_TOKEN", ""),
        "username": merged.get("ERP_USERNAME", ""),
        "password": merged.get("ERP_PASSWORD", ""),
        "company": merged.get("ERP_COMPANY_CODE") or DEFAULT_COMPANY,
    }


def _env_chain(extra_env_file: str | None = None) -> list[Path]:
    """按**从低到高**的优先级列出要读的文件（后面的覆盖前面的）。"""
    specified = resolve_env_path(extra_env_file) if extra_env_file else None
    chain = list(reversed(LEGACY_ENV_PATHS))
    if specified:
        chain = [p for p in chain if p != specified] + [specified]
    return chain


def effective_env_file(extra_env_file: str | None = None) -> Path | None:
    """**密码**实际来自哪个文件。

    只看密码：token 是跑一次就有的缓存，而密码才是人要编辑的东西 ——
    界面提示"实际用的是别处"时，指的应该是密码来源。
    """
    for p in reversed(_env_chain(extra_env_file)):
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
    }


def save_credentials(env_file: str | None = None, *, username=None, password=None,
                     company=None, token=None, clear_token: bool = False) -> Path:
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
    return envfile.update(resolve_env_path(env_file), updates)


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
                 env_file: str | None = None):
        self.creds = creds or load_credentials(env_file)
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
        save_credentials(self.env_file, token=token,
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

    # ------------------------------------------------------------- 销售明细
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
