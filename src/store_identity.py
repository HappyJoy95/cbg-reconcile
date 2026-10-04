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

    ("店码", "ok", "")       拿到店码 —— 会话真、店也真有单
    ("", "empty", "")        接口通了但 0 行 —— 会话是真的，只是这 30 天没卖货
                             （新店常见）→ verify **放行**，但 why 要把话说清
    ("", "authfail", "底层") CbgAuthError —— 会话是假的 / 已失效 → verify **必须拦**

  第三段是 **底层报错原文**（HTTP 403？「没有门店或数据范围」？返回的是登录页
  HTML？）—— 2026-09-27 排查生活馆抓取超时时发现它被吞掉过：日志只剩
  「会话没验过」，四个候选假设一个都分不开。**别再把第三段扔了。**

  网络、接口 5xx 这类失败（CbgError）**不折进上面三态**：那是"没验成"，
  不是"验出来是假的"，文案不该冤枉会话 —— 让它原样抛，`identify` 兜住
  （`probe="error"`），verify 同样拦，但话说成"接口异常"。
"""

from __future__ import annotations

import os
import re
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


def _move_session_file(root, config_path, target_config=None) -> bool:
    """`cbg-default.json` → `cbg-<店码>.json`（幂等）—— 真挪了返回 True。

    ⚠ **先 mkdir 再 os.replace**：目标目录不一定在（换过 `session.file` 配置时
      新路径可能整个是新目录，缺这一步直接 FileNotFoundError）。
    ⚠ **目标已存在就不覆盖**：目标那份是这家店名下已有的会话，default 这份
      来历不明（多半是上次没挪完的），盖上去等于把真会话弄丢。
    """
    old = auth.session_path({"store_code": ""}, root)
    cfg = (target_config if target_config is not None
           else config_io.load_raw(config_path) or {})
    new = auth.session_path(cfg, root)
    if old == new or not old.exists() or new.exists():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    os.replace(str(old), str(new))
    return True


def set_store_code(root, config_path, code) -> dict:
    """**手输店码**（用户 2026-09-27：「加一个手动输入门店编码吧，
    现在匹配不起来拉不到会话」）—— 写配置 + 挪会话文件，返回结果说明。

    为什么需要它：认店的探测（不带 storeCode 查订单）在那台机器上**一直 403**，
    抓取流程卡死在 verify —— 而 `identify` 见配置里已有店码就**跳过探测**、
    直接拿店码正经 ping（见 `_identify` 开头那段）。人把店码给了，
    就不用再问接口"你是哪家店"。

    * 店码：去空格、转大写；格式 `[A-Za-z0-9][A-Za-z0-9-]{3,31}`
      （名单里是 `SCN328987` / `CNSCN162188` 这种）—— 格式不对直接拒，
      不许把「店名」「编码」这类输入写进配置（半截写比不写糟）。
    * 名单里有 → 店名/标识一起写；**没有 → 店码照写、店名留空**
      （写空是"这家店确实没名字"，别留上一家店的旧值 —— 坑 12 同款；
      边界：名单没这家店照样放行，菜单由 edition 决定、不依赖认店）。
    * 会话文件已存在就补挪 `cbg-default.json` → `cbg-<店码>.json`
      （换店 = 换会话的规矩不破）。
    * **绝不往外抛**：写配置失败折成 `{"ok": False, "error": ...}`。
    """
    code = str(code or "").strip().upper()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{3,31}", code):
        return {"ok": False,
                "error": "店码看着不对（例：SCN328987）"
                         " —— 只能是 4~32 位字母 / 数字 / 横杠"}
    hit = _lookup_store(root, code)
    path = os.fspath(config_path)
    old_session = auth.session_path({"store_code": ""}, root)
    original_config = None
    config_backed_up = False
    new_session = None
    should_move = False
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                original_config = f.read()
        config_backed_up = True
        current_config = config_io.load_raw(config_path) or {}
        target_config = dict(current_config)
        target_config["store_code"] = code
        new_session = auth.session_path(target_config, root)
        should_move = (old_session != new_session and old_session.exists()
                       and not new_session.exists())
        # 先挪会话，再写店码；重命名失败时配置仍保持原样。
        moved = _move_session_file(root, config_path, target_config)
        config_io.update(config_path, {
            "store_code": code,
            "erp_store_name": str((hit or {}).get("erp_name") or ""),
            "marker": str((hit or {}).get("marker") or "")})
    except Exception as e:                              # noqa: BLE001
        rollback_errors = []
        # 即使文件移动之后配置写入失败，也尽量把两边恢复到调用前。
        if should_move and new_session.exists() and not old_session.exists():
            try:
                os.replace(str(new_session), str(old_session))
            except Exception as rollback_error:          # noqa: BLE001
                rollback_errors.append("会话回滚失败：%s" % rollback_error)
        if config_backed_up:
            try:
                if original_config is None:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    with open(path, "wb") as f:
                        f.write(original_config)
            except Exception as rollback_error:          # noqa: BLE001
                rollback_errors.append("配置回滚失败：%s" % rollback_error)
        detail = "保存失败（%s）：%s" % (type(e).__name__, e)
        if rollback_errors:
            detail += "；" + "；".join(rollback_errors)
        return {"ok": False, "error": detail}
    return {"ok": True, "store_code": code, "found": bool(hit),
            "store_name": str((hit or {}).get("erp_name") or ""),
            "moved": moved}


def _probe_store_code(sess, window_days: int = 30) -> Tuple[str, str, str]:
    """**不带 storeCode** 查一次订单，从返回行里拿店码 → `(店码, 状态, 底层报错)`。

    ⚠ `cbg.list_orders` 不传 storeCode 时按会话所属门店查（cbg.py:240）——
      这正是"会话自带身份"的依据。

    三态（Task 6 验收点）：
      `"ok"`       拿到店码（会话真 + 店真有单）
      `"empty"`    接口通了但 0 行 —— 会话是真的，只是这 30 天没卖货
      `"authfail"` `CbgAuthError` —— 会话是假的 / 已失效，**verify 必须拦**

    第三段：authfail 时带 **CbgAuthError 原文**（其余状态空串）。
    2026-09-27 实测教训：生活馆抓取超时，日志只有「会话没验过」，
    403 / 权限错 / HTML 分不开 —— 原文必须一路带到 verify 与超时报错里。

    ⚠ 其他失败（网络、接口异常的 CbgError）**原样抛**，不许折进三态 ——
      "没验成"和"验出来是假的"是两回事（由 identify 兜住成 `probe="error"`）。
    ⚠ 手上没会话（sess=None）按 authfail：没有登录态，本来就没得验。
    """
    import time
    from .integrations.cbg import CbgAuthError, CbgClient
    if sess is None:
        return "", "authfail", "手上没有会话文件"
    now = int(time.time())
    try:
        client = CbgClient(sess, store_code=None, timeout=25)
        rows = client.list_orders(now - window_days * 86400, now, page_size=20)
    except CbgAuthError as e:
        return "", "authfail", str(e)
    for r in rows or []:
        code = str((r or {}).get("storeCode") or "").strip()
        if code:
            return code, "ok", ""
    return "", "empty", ""


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
        code, status, detail = _probe_store_code(sess)
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
        # ⚠ 底层报错原文必须带上 —— 2026-09-27 排查超时时它被吞掉过：
        #   只说「会话没验过」，403 / 权限错 / 登录页 HTML 一个都分不开。
        why = _WHY_AUTHFAIL + ("（底层：%s）" % detail if detail else "")
    else:
        why = "认店探测没通过（%s）" % status
    return {"ok": False, "probe": status, "why": why}


def check_after_save(sess, store_code: str) -> Tuple[bool, str]:
    """抓取**保存点**的自检（生活馆）：没店码时别拿去 ping。

    ⚠ 为什么单开一条：`CbgClient.ping()` 走 `store_detail`，没有 storeCode
      必报「接口异常：没给 storeCode，无法查门店详情」—— 对 0 订单的新店
      （probe=empty，店码还没认出来）那不是会话坏了，却会把界面显示成
      「已保存，但自检没过」（2026-09-27 抓取超时排查时顺出来的毛刺）。
    ⚠ 有店码就正经 ping —— 那时的失败是真失败，照旧拦。
    """
    from .integrations.cbg import CbgClient
    if not str(store_code or "").strip():
        return True, ("会话已保存（这 30 天没订单，店码还没认出来"
                      " —— 抓到销售后自动补上）")
    return CbgClient(sess, store_code=store_code).ping()


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
    from .integrations.cbg import CbgAuthError, CbgClient, CbgError
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
