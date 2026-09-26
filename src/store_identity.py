# -*- coding: utf-8 -*-
"""**从玲珑会话认出这是哪家店** —— 生活馆版的身份来源。

用户 2026-09-26 定：生活馆没有云商登录，"是哪家店"从**玲珑会话**里认：

    抓到会话 → 不带 storeCode 查一次订单（会话自带所属门店）
    → 返回行里的 storeCode → 门店名单匹配店名 → 写配置
    → 把 cbg-default.json 挪成 cbg-<店码>.json（换店=换会话的规矩不破）

三条边界：
* **名单里没这家店照样放行**（店码写上、店名留空）—— 菜单由 edition 决定，
  不依赖认店成败；店名只是显示。
* **查不出店码不写配置**（半截写比不写糟：店码错了会话就白抓了）。
* **full 版完全不走这条路**（它靠云商登录认店，见 web.setup_state）。

⚠ 认店失败**不拦登录** —— 登录判据是"有会话文件"（见 web._setup_state_lifehall），
认店只影响显示和"换店时会话文件名对不对"。

⚠ `_probe_store_code` 是**三态**（Task 6 的验收点，别折成 bool）：

    ("店码", "ok")     拿到店码 —— 会话真、店也真有单
    ("", "empty")      接口通了但 0 行 —— 会话是真的，只是这 30 天没卖货
                       （新店常见）→ verify **放行**，但 why 要把话说清
    ("", "authfail")   CbgAuthError —— 会话是假的 / 已失效 → verify **必须拦**

  网络、接口 5xx 这类失败（CbgError）**不折进上面三态**：那是"没验成"，
  不是"验出来是假的"，文案不该冤枉会话 —— 让它原样抛，`identify` 兜住
  （`probe="error"`），verify 同样拦，但话说成"接口异常"。
"""

from __future__ import annotations

import os
from typing import Tuple

from . import config_io
from . import edition as _edition
from .modules import auth


def _lookup_store(root, code):
    """店码 → 门店名单（`config/stores.yaml`）那一行；没命中给 None。"""
    code = str(code or "").strip()
    if not code:
        return None
    for row in config_io.stores_table(root):
        if str(row.get("huawei_code") or "").strip() == code:
            return row
    return None


def _move_session_file(root, config_path) -> bool:
    """`cbg-default.json` → `cbg-<店码>.json`（幂等）—— 真挪了返回 True。

    ⚠ **先 mkdir 再 os.replace**：目标目录不一定在（换过 `session.file` 配置时
      新路径可能整个是新目录，缺这一步直接 FileNotFoundError）。
    ⚠ **目标已存在就不覆盖**：目标那份是这家店名下已有的会话，default 这份
      来历不明（多半是上次没挪完的），盖上去等于把真会话弄丢。
    """
    old = auth.session_path({"store_code": ""}, root)
    new = auth.session_path(config_io.load_raw(config_path) or {}, root)
    if old == new or not old.exists() or new.exists():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(old), str(new))
    return True


def _probe_store_code(sess, window_days: int = 30) -> Tuple[str, str]:
    """**不带 storeCode** 查一次订单，从返回行里拿店码 → `(店码, 状态)`。

    ⚠ `cbg.list_orders` 不传 storeCode 时按会话所属门店查（cbg.py:240）——
      这正是"会话自带身份"的依据。

    三态（Task 6 验收点）：
      `"ok"`       拿到店码（会话真 + 店真有单）
      `"empty"`    接口通了但 0 行 —— 会话是真的，只是这 30 天没卖货
      `"authfail"` `CbgAuthError` —— 会话是假的 / 已失效，**verify 必须拦**

    ⚠ 其他失败（网络、接口异常的 CbgError）**原样抛**，不许折进三态 ——
      "没验成"和"验出来是假的"是两回事（由 identify 兜住成 `probe="error"`）。
    ⚠ 手上没会话（sess=None）按 authfail：没有登录态，本来就没得验。
    """
    import time
    from .cbg import CbgAuthError, CbgClient
    if sess is None:
        return "", "authfail"
    now = int(time.time())
    try:
        client = CbgClient(sess, store_code=None, timeout=25)
        rows = client.list_orders(now - window_days * 86400, now, page_size=20)
    except CbgAuthError:
        return "", "authfail"
    for r in rows or []:
        code = str((r or {}).get("storeCode") or "").strip()
        if code:
            return code, "ok"
    return "", "empty"


#: 三态文案 —— "0 行"和"假会话"必须分开说（合并了 verify 就没法一个放一个拦）
_WHY_EMPTY = ("这 30 天没有查到本店订单，认不出店码"
              "（不影响使用，抓到销售后会自动补上）")
_WHY_AUTHFAIL = "会话没验过（登录态无效或已过期，或还没抓到会话）—— 请重新抓一次"


def identify(root, config_path, sess=None) -> dict:
    """认店（幂等）→ `{"ok", "skipped"?, "store_code"?, "store_name"?,
    "why"?, "probe"?}`。

    * `skipped`：不是生活馆版（full 靠云商登录认店，这条路整个不走）。
    * `probe`：探测三态原样带回来（`ok`/`empty`/`authfail`/`error`）——
      verify 靠它区分"0 行的真会话（放行）"和"假会话（拦）"。
    * **绝不往外抛**：认店是锦上添花，炸在保存点上会把"会话其实已经存好了"
      说成失败（坑 11 的同款教训）；出错折成 `ok=False` + why。
    """
    try:
        return _identify(root, config_path, sess)
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "probe": "error",
                "why": "认店出错（%s）：%s" % (type(e).__name__, e)}


def _identify(root, config_path, sess=None) -> dict:
    if not _edition.is_lifehall():
        return {"ok": True, "skipped": True}
    cfg = config_io.load_raw(config_path) or {}
    have = str(cfg.get("store_code") or "").strip()
    if have:
        # 已经认过了 → **不重复探测**。只剩一件事可能欠着：会话文件挪名 ——
        # 抓取流程用的是**开跑时读的旧 cfg**（那时 store_code 还空着），会话
        # 存出来是 cbg-default.json，这里补挪一次（幂等，没得挪就跳过）。
        _move_session_file(root, config_path)
        hit = _lookup_store(root, have)
        return {"ok": True, "store_code": have,
                "store_name": str((hit or {}).get("erp_name") or "")}
    if sess is None:
        # 没给会话就现读一份；读不到**不在这儿短路** —— 三态由
        # `_probe_store_code` 定（打桩测试走的就是这条路），真没会话它回 authfail。
        try:
            from .session import CbgSession
            sess = CbgSession.load(auth.session_path(cfg, root))
        except Exception:                               # noqa: BLE001
            sess = None
    try:
        code, status = _probe_store_code(sess)
    except Exception as e:                              # noqa: BLE001
        # 网络 / 接口异常（CbgError）从探测里抛出来走这儿：
        # 没验成 ≠ 假会话，文案分开说；verify 拦，但不说成会话是假的
        return {"ok": False, "probe": "error",
                "why": "认店探测没通（接口异常）：%s" % e}
    if status == "ok" and code:
        hit = _lookup_store(root, code)
        try:
            config_io.update(config_path, {
                "store_code": code,
                # 没命中也写空串：老配置里可能残留上一家店的名字（坑 12 同款）
                "erp_store_name": str((hit or {}).get("erp_name") or ""),
                "marker": str((hit or {}).get("marker") or "")})
            # 会话文件跟着挪：写完 store_code 后 session_path 变了，
            # 不挪等于"会话丢了"（换店=换会话的规矩就破了）
            _move_session_file(root, config_path)
        except Exception as e:                          # noqa: BLE001
            # ⚠ 写配置失败不冤枉会话：探测已经证明会话是真的，
            #   店码带回去，verify 会拿它正经 ping 一次
            return {"ok": False, "probe": "ok", "store_code": code,
                    "why": "店码 %s 认到了，但写配置失败（%s）：%s"
                           % (code, type(e).__name__, e)}
        return {"ok": True, "store_code": code, "probe": "ok",
                "store_name": str((hit or {}).get("erp_name") or "")}
    if status == "ok":
        status = "error"       # 契约漂移：说 ok 就必须带店码（防打桩打歪）
    if status == "empty":
        why = _WHY_EMPTY
    elif status == "authfail":
        why = _WHY_AUTHFAIL
    else:
        why = "认店探测没通过（%s）" % status
    return {"ok": False, "probe": status, "why": why}


def verify_with_identity(sess, root, config_path, emit=None) -> Tuple[bool, str]:
    """抓会话流程的 verify 回调（生活馆版）：先认店，再正经自检。

    返回 `(过没过, 为什么)` —— 形状照 `web._capture_worker.verify` 的约定。

    三态怎么走（Task 6 验收点）：
      * `ok`（拿到店码）→ 拿店码正经 `ping()` 一次，按 ping 的结果过 / 不过；
      * `empty`（0 行的新店）→ **放行**，why 说清"会话是真的，只是没订单"；
      * `authfail`（假会话）→ **必须拦**，why 说清要重新抓。
    ⚠ 这里拦的只有"会话验出来是假的"；**认店失败本身不拦登录** ——
      登录判据是"有会话文件"（见 `web._setup_state_lifehall`）。

    `emit`：调用方可以把 `job.say` 传进来。当前**刻意不在这里吼** ——
      保存点那次 `identify` 会播报同一句，两处都说就是刷屏（浏览器对
      "自检通过"只说通过、不复述 why，紧跟着保存点就会补上）。
    """
    from .cbg import CbgAuthError, CbgClient, CbgError
    res = identify(root, config_path, sess)
    code = res.get("store_code") or ""
    if code:
        try:
            ok, why = CbgClient(sess, store_code=code, timeout=25).ping()
            return ok, why
        except (CbgAuthError, CbgError) as e:
            return False, "%s: %s" % (type(e).__name__, e)
    probe = res.get("probe")
    if probe == "empty":
        # 探测通了、只是 0 行：会话是真的，店码没有 —— 放行但把话说清
        return True, res.get("why") or _WHY_EMPTY
    if probe == "ok":
        # 订单探测成功 = 真实登录数据 = 放行（店码没写成时的兜底分支）
        return True, "会话真实可用（未认出店码，不影响使用）"
    # authfail / error：没证明会话是真的 → 拦（假会话必须死在这儿）
    return False, res.get("why") or "会话没验过"
