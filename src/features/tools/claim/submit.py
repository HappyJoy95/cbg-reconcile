# -*- coding: utf-8 -*-
"""华为官网**权益领取直提** —— 按 SN 查可领权益，再按活动 privilegeCode 提交。

不跳转浏览器：后端直接打华为网关（与 consumer.huawei.com 活动页同一套接口）。

两步（官方页也是这两步）：

1. **查询** `POST /forward/ccpc/ccps/awardDeviceRightV3/1000`
   body: ``{grantor, ownerId: SN, langType, usedType, usedChannel, countryCode}``
2. **领取** `POST /forward/ccpc/ccps/serviceSalesOrderBatche/1000`
   body: ``[{sn, orderNo, scCode, subActivityCode, retailDate, …}]``
   其中 ``scCode`` = 第一步返回的 ``privilegeCode``。

⚠ 网关 / SGW-APP-ID 与官网活动页**逐字相同**（2026-09-23 从页面内联脚本抄的）。
⚠ 本模块**只在人点了「在线领取」时**发请求；失败说人话，不静默。
⚠ 超时要短 —— 门店网络差时别把控制台挂死。
"""

from __future__ import annotations

import datetime
import json
from typing import Dict, List, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest

#: 正式网关（官网 WEB_API_URL 中国区正式）
GATEWAY = "https://sgw-cn.c.huawei.com"
QUERY_PATH = "/forward/ccpc/ccps/awardDeviceRightV3/1000"
CLAIM_PATH = "/forward/ccpc/ccps/serviceSalesOrderBatche/1000"
#: 官网活动页写死的应用号（公开前端头，非密钥）
SGW_APP_ID = "96E6614BD648F3582A2D6D657C5CC768"
TIMEOUT_SEC = 12

#: ⭐ **官网前端 `queryCode` → `popupInfo` 同一套**（2026-09-23 从
#:   consumer.huawei.com 活动页内联脚本逐字抄的）—— 用户：
#:   「官网提示是…能不能加载官网的前端渲染逻辑，我们这边也同步上」。
#:
#: 官网逻辑：`queryCode[i].indexOf(responseCode) >= 0` → case i → popupInfo{i+1}
QUERY_CODE_GROUPS = (
    # case 0 → popupInfo1 成功
    (("200",), 1),
    # case 1 → popupInfo2 设备过保 / 不符合
    ((" _1:E02, _2:E02", "E02"), 2),
    # case 2 → popupInfo3 **重复领取**（E05/E20/E27）
    ((" _1:E05, _2:E05", "E05",
      " _1:E20, _2:E20", "E20",
      " _1:E27, _2:E27", "E27"), 3),
    # case 3 → popupInfo4 超过激活时间
    ((" _1:E10, _2:E10", "E10",
      " _1:E11, _2:E11", "E11",
      " _1:E15, _2:E15", "E15"), 4),
)

#: 官网 popupInfo 文案（纯文本版 —— 去掉 950800 的 span，便于 toast / JSON）
POPUP_TXT = {
    1: "恭喜，您已成功领取权益",
    2: "抱歉，您的设备不符合领取条件，可以咨询“在线客服”或拨打华为服务热线 950800",
    3: "您的设备已经领取过权益，无法再领取",
    4: "抱歉，您的设备不符合领取条件，可以咨询“在线客服”或拨打华为服务热线 950800",
    5: "抱歉，您的设备不符合领取条件，可以咨询“在线客服”或拨打华为服务热线 950800",
    6: "请阅读并同意华为数据收集隐私协议",
    7: "设备序列号（SN）不能为空",
    8: "无法连接网络，请稍后重试",
}

#: 旧的简单码表（中文短句）—— 仅当官网组匹配不到时兜底
QUERY_MSG = {
    "200": POPUP_TXT[1],
    "5000": "设备信息不存在",
    "5001": "设备已经领取过权益，无法再领取",
    "5002": "设备不符合领取条件",
    "5003": "超过领取时间",
}


def popup_index_for(code: str) -> int:
    """按官网 `queryCode` 规则找 popupInfo 下标；找不到 → 5（兜底不符合）。"""
    c = str(code or "")
    for group, idx in QUERY_CODE_GROUPS:
        # 官网：indexOf 精确子串 —— 整串码列表里有就命中
        for item in group:
            if item and (c == item or item in c):
                return idx
        # 也试 stripped
        cs = c.strip()
        for item in group:
            if item and item.strip() and cs == item.strip():
                return idx
    # 无绑定规则等未进官网表的 → 兜底 5（与官网 default 一致）
    if "no_bind_rule" in c or "no_bind" in c:
        return 5
    return 5


def official_popup(code: str, desc: str = "") -> dict:
    """官网同款弹窗：`{popup, popup_key, ok}`。"""
    idx = popup_index_for(code)
    ok = idx == 1 and str(code).strip() in ("200",) or str(code) == "200"
    # success only when code is exactly 200 (or contains only 200)
    ok = str(code or "").strip() == "200"
    return {
        "ok": ok,
        "popup_key": "popupInfo%d" % idx,
        "popup": POPUP_TXT.get(idx) or POPUP_TXT[5],
        "popup_index": idx,
        "raw_desc": desc,
    }


def human_query_fail(desc: str, code: str) -> str:
    """查询失败人话 —— **优先官网 popupInfo 文案**。"""
    pop = official_popup(code, desc)
    # 官网 case0 是成功；到这儿一定是失败分支
    if str(code or "").strip() == "200":
        return POPUP_TXT[1]
    # no_bind 等：官网 default 同款 5；但补充机器码便于排查
    text = pop["popup"]
    code_s = str(code or "").strip()
    if code_s and code_s not in text and "E0" not in code_s and "E1" not in code_s             and "E2" not in code_s:
        # 保留简短 code 提示（如 no_bind_rule），长拼串不塞进话术
        if "no_bind" in code_s:
            text += "（华为侧无绑定规则）"
    if str(desc or "").strip() and str(desc).strip() not in (
            "query award device right fail", text):
        # 英文原话只在完全映射不到时附上
        if pop["popup_index"] == 5 and desc and "query award" in desc.lower():
            pass  # 已用官网兜底话术，不叠加英文
    return text


def _now_date() -> str:
    return datetime.date.today().isoformat()


def _now_time() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _post(path: str, payload, extra_headers: Optional[dict] = None) -> dict:
    """打华为网关。返回解析后的 JSON；网络/非 JSON 给 ``{"_error": …}``。"""
    url = GATEWAY + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json text/plain; charset=UTF-8;",
        "SGW-APP-ID": SGW_APP_ID,
        "User-Agent": "Mozilla/5.0 (compatible; CBGReconcile/claim)",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://consumer.huawei.com",
        "Referer": "https://consumer.huawei.com/",
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urlerror.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"_error": "华为网关 HTTP %s %s" % (e.code, body or e.reason)}
    except urlerror.URLError as e:
        return {"_error": "连不上华为网关：%s" % getattr(e, "reason", e)}
    except Exception as e:  # noqa: BLE001 —— 超时等
        return {"_error": "请求华为网关失败：%s" % e}
    try:
        obj = json.loads(raw)
    except ValueError:
        return {"_error": "华为网关返回不是 JSON：%s" % raw[:200]}
    if not isinstance(obj, dict):
        return {"_error": "华为网关返回格式异常"}
    return obj


#: 云商串号列是 15 位 86 码时的人话（不打网关 —— 华为只会 sn.NotFound）
IMEI_NOT_SN_WHY = (
    "云商给的是86码（IMEI），不是设备SN，无法在线领取。"
    "请扫机身/包装上的SN条码后再领，或用「手动领取」并在官网填SN。"
)


def looks_like_imei(v: str) -> bool:
    """15 位纯数字 = IMEI / 86 码 —— 不是华为 ownerId 要的 SN。"""
    t = str(v or "").strip()
    return len(t) == 15 and t.isdigit()


def query_rights(sn: str) -> dict:
    """按 SN 查可领权益。

    返回::

        {"ok": True, "code": "200", "rights": [ {orderNo, privilegeCode, ...}, ... ]}
        {"ok": False, "why": "...", "code": "5000"}
    """
    sn = str(sn or "").strip()
    if not sn:
        return {"ok": False, "why": "缺少 SN"}
    # ⚠ 86 码先拦：实测华为回 sn.NotFound，会被映射成「不符合领取条件」误导门店
    if looks_like_imei(sn):
        return {
            "ok": False, "why": IMEI_NOT_SN_WHY, "popup": IMEI_NOT_SN_WHY,
            "code": "IMEI_NOT_SN", "sn": sn, "sn_kind": "imei",
            "rights": [],
        }
    body = {
        "grantor": "1",
        "ownerId": sn,
        "langType": "1",
        "usedType": "2",
        "usedChannel": "9",
        "countryCode": "CN",
    }
    res = _post(QUERY_PATH, body)
    if "_error" in res:
        return {"ok": False, "why": res["_error"], "code": ""}
    code = str(res.get("responseCode") or "")
    rights = res.get("responseData") or []
    if not isinstance(rights, list):
        rights = []
    if str(code).strip() == "200":
        return {"ok": True, "code": code, "rights": rights, "sn": sn,
                "desc": POPUP_TXT[1], "popup": POPUP_TXT[1],
                "popup_key": "popupInfo1"}
    desc = str(res.get("responseDesc") or "")
    pop = official_popup(code, desc)
    # 200 但 rights 空仍算成功结构 —— 正常 200 在上面已返回
    why = QUERY_MSG.get(code) or human_query_fail(desc, code)
    # 若码表有更准的官网话术，用官网的
    why = pop["popup"] if str(code).strip() != "200" else why
    if str(code).strip() == "200":
        # 理论上到不了（上面已 return ok）
        return {"ok": True, "code": code, "rights": rights, "sn": sn,
                "popup": pop["popup"], "popup_key": pop["popup_key"]}
    return {"ok": False, "code": code, "why": why, "popup": pop["popup"],
            "popup_key": pop["popup_key"], "raw_desc": desc,
            "rights": [], "sn": sn}


def build_claim_items(sn: str, rights: Sequence[dict],
                      privilege_codes: Optional[Sequence[str]] = None) -> List[dict]:
    """查询结果 → 领取报文（与官网 ``serviceSalesOrder4POSTdata`` 同构）。

    ``privilege_codes`` 非空则只留这些权益（按活动路由）；空 = 全部可领的都提。
    """
    allow = {str(c).strip() for c in (privilege_codes or []) if str(c).strip()}
    today = _now_date()
    now = _now_time()
    items: List[dict] = []
    for val in rights or ():
        if not isinstance(val, dict):
            continue
        pc = str(val.get("privilegeCode") or "")
        if allow and pc not in allow:
            continue
        if not pc:
            continue
        items.append({
            "sn": sn,
            "orderNo": str(val.get("orderNo") or ""),
            "scCode": pc,
            "scName": str(val.get("privilegeName") or val.get("scName") or ""),
            "retailDate": str(val.get("retailDate") or today) or today,
            "retailShopCode": str(val.get("retailShopCode") or ""),
            "retailShopName": str(val.get("retailShopName") or ""),
            "channelSource": "9",
            "currency": str(val.get("currency") or ""),
            "countryCode": "CN",
            "subActivityCode": str(val.get("subActivityCode") or ""),
            "lastUpdate": now,
            "receiveDate": today,
        })
    return items


def claim(sn: str, privilege_codes: Optional[Sequence[str]] = None) -> dict:
    """查询 → 过滤 → 提交领取。一步做完（给「在线领取」按钮用）。"""
    sn = str(sn or "").strip()
    q = query_rights(sn)
    if not q.get("ok"):
        return q
    items = build_claim_items(sn, q.get("rights") or [], privilege_codes)
    if not items:
        codes = ", ".join(str(c) for c in (privilege_codes or [])) or "(全部)"
        return {
            "ok": False,
            "why": "该 SN 下没有匹配到可领权益（活动码 %s）" % codes,
            "code": q.get("code"),
            "rights_total": len(q.get("rights") or []),
            "sn": sn,
        }
    res = _post(CLAIM_PATH, items)
    if "_error" in res:
        return {"ok": False, "why": res["_error"], "sn": sn,
                "submitted": len(items)}
    code = str(res.get("responseCode") or "")
    desc = str(res.get("responseDesc") or "")
    if str(code).strip() == "200":
        return {
            "ok": True,
            "code": code,
            "sn": sn,
            "claimed": len(items),
            "items": [{"scCode": i["scCode"], "orderNo": i["orderNo"],
                       "scName": i["scName"]} for i in items],
            "desc": POPUP_TXT[1],
            "popup": POPUP_TXT[1],
            "popup_key": "popupInfo1",
        }
    pop = official_popup(code, desc)
    why = pop["popup"]
    return {"ok": False, "code": code, "why": why, "popup": why,
            "popup_key": pop["popup_key"], "raw_desc": desc,
            "sn": sn, "submitted": len(items)}


def claim_for_activity(sn: str, activity: Optional[dict]) -> dict:
    """按活动配置（含 ``privilege_codes``）提交；没有配置码则尝试全部可领权益。"""
    codes = []
    if activity:
        raw = activity.get("privilege_codes") or activity.get("privilegeCodes") or []
        if isinstance(raw, str):
            codes = [x.strip() for x in raw.split(",") if x.strip()]
        elif isinstance(raw, (list, tuple)):
            codes = [str(x).strip() for x in raw if str(x).strip()]
    # 没有 privilege 配置的活动（链接 404 / 输出中）——仍允许按 SN 查后全提？
    # ⚠ 不：全提可能误领别的活动。没码就先查、不过滤，由调用方确认。
    return claim(sn, privilege_codes=codes or None) if codes else claim(sn, None)
