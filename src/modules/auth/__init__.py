"""**登录验证模块** —— 「账号信息从哪儿来、会话还能不能用」。

用户 2026-09-19 定的位置：

> 功能模块……**从登录验证模块获取账号信息**

所以这个模块对外只有两类东西：

| 出口 | 给谁 | 说什么 |
|---|---|---|
| `accounts(cfg)` | 功能模块 / 界面 | **账号信息**（哪套账号、什么来源、够不够用）—— **绝不回密码** |
| `state(cfg)` | 启动自检 / 健康面板 | **自检结论**（有没有、能不能用、要不要人工登录一次） |

## 这个系统里其实有**三套**登录，别混

| 谁 | 凭据在哪 | 干什么用 | 没人管会怎样 |
|---|---|---|---|
| **云商 ERP** | `.secrets/erp.env` / 环境变量 / **内置公司账号** | 查销售、库存、员工 | 取不到云商数据（会退化，不会崩） |
| **华为 CBG 会话** | `.secrets/cbg-<门店码>.json` | 抓订单、报量核对 | 抓不到订单 —— **换店等于换一份会话** |
| **华为 SSO 账号** | `.secrets/cbg-login.env` | 会话过期时**静默续期**（不弹窗） | 会话一过期就得人工 `auth --auto` |

⚠ **三套的失败后果完全不同**，所以 `state()` 把它们的"缺了会怎样"分开写；
界面上也别合成一句"未登录" —— 门店看到的应该是"会员会话过期了，点这里重新登录"。

## 边界

* **只读状态，不弹窗、不抓取** —— 真正弹浏览器登录的是入口层（`cli auth` / 控制台按钮）。
  自检要是顺手弹个窗，夜里那条定时任务就变成弹窗器了（`browser.launch` 那套
  headless 续期是 `cli.ensure_session` 的事）。
* `session_path` 这个**路径口径**收在这儿（以前住在 `cli.py`）：它和"会话文件长什么样"
  是一件事，散在入口层的话，`web.py` / `app` / 自检各拼一次路径迟早拼歪。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

#: 华为会话默认落在哪 —— 换店**必须**换文件（换店 = 换一份会话）
SESSION_REL = ".secrets/cbg-{store}.json"

#: 会话"快过期"的提醒线（天）—— 华为那边实测一周上下会失效
SESSION_WARN_DAYS = 5


def session_path(cfg: dict, root=None) -> Path:
    """华为会话文件的**唯一**路径口径（`session.file` 可覆盖）。

    ⚠ 配置里写相对路径时按**项目根**解析（不是 cwd）——
      控制台是从别处起的进程，按 cwd 解析会各找各的。
    """
    from ...paths import ROOT
    root = Path(root) if root else ROOT
    store = (cfg or {}).get("store_code") or "default"
    rel = ((cfg or {}).get("session") or {}).get("file") or SESSION_REL.format(store=store)
    p = Path(rel)
    return p if p.is_absolute() else root / p


def accounts(cfg: dict, root=None) -> dict:
    """**账号信息**（脱敏）—— 功能模块要"我是谁、哪家店"就问这儿。

    返回：

    ```python
    {"erp": {"user": …, "company": …, "source": "env|file|builtin", "has_password": bool},
     "cbg": {"user": …, "store": …, "login_env": …, "ready": bool},
     "store": {"code": …, "name": …}}
    ```

    ⚠ **一个密码都不回**（`describe_*` 那几个函数本来就只回 `has_*`）。
    """
    from ...paths import ROOT
    from ... import browser
    from ... import edition as _edition
    erp = None
    if not _edition.is_lifehall():
        # 生活馆包里 `erp.py` 不存在（edition.PRUNE）—— erp 留 None，
        # 下面两段云商凭据整段跳过（回"没有云商"的空档）。
        from ... import erp
    root = Path(root) if root else ROOT
    cfg = cfg or {}

    out = {"store": {"code": cfg.get("store_code") or "",
                     "name": cfg.get("erp_store_name") or ""}}
    if erp is None:
        out["erp"] = {"user": "", "why": "生活馆版没有云商"}
        out["erp_store"] = {"user": "", "why": "生活馆版没有云商"}
    else:
        try:
            d = erp.describe_credentials()
            src = "env" if d.get("used_from") else ("builtin" if d.get("builtin") else "file")
            out["erp"] = {"user": d.get("username") or ("（内置公司账号）" if d.get("builtin") else ""),
                          "company": d.get("company") or "",
                          "source": src,
                          "env_file": d.get("env_file") or "",
                          "used_from": d.get("used_from") or "",
                          "has_password": bool(d.get("has_password")),
                          "has_token": bool(d.get("has_token")),
                          "builtin": bool(d.get("builtin"))}
        except Exception as e:                                     # noqa: BLE001
            out["erp"] = {"user": "", "why": "%s: %s" % (type(e).__name__, e)}
        try:
            # ⚠ 云商有**两套**账号：主账号（`.secrets/erp.env`）和**门店账号**
            #   （`.secrets/erp-store.env`）。销售明细那一路走的是门店账号 ——
            #   只报主账号的话，门店明明配好了，自检却说"没有云商凭据"（实测踩过）。
            s = erp.describe_store_credentials()
            out["erp_store"] = {"user": s.get("username") or "", "who": s.get("who") or "",
                                "company": s.get("company") or "",
                                "env_file": s.get("env_file") or "",
                                "has_password": bool(s.get("has_password")),
                                "has_token": bool(s.get("has_token"))}
        except Exception as e:                                     # noqa: BLE001
            out["erp_store"] = {"user": "", "why": "%s: %s" % (type(e).__name__, e)}
    try:
        out["cbg"] = browser.describe_login(cfg, root)
    except Exception as e:                                     # noqa: BLE001
        out["cbg"] = {"username": "", "ready": False,
                      "why": "%s: %s" % (type(e).__name__, e)}
    return out


def state(cfg: dict, root=None) -> dict:
    """启动自检用：**三套登录各自什么状态、缺了会怎样**。

    ⚠ **不联网、不弹窗**（`ping` 是"真去问华为"，那是 `fetch` / 健康检查里
      「数据能不能算」的事）。这里只看**本地证据**：文件在不在、存了多久。
    """
    from ...paths import ROOT
    from ...session import CbgAuthError, CbgSession
    from ... import edition
    root = Path(root) if root else ROOT
    cfg = cfg or {}
    acc = accounts(cfg, root)
    items = []

    if not edition.is_lifehall():
        # ① 云商：凭据齐不齐（**两套账号任一可用就算通**）
        erp_acc = acc.get("erp") or {}
        st_acc = acc.get("erp_store") or {}
        usables = [n for n, a in (("主账号", erp_acc), ("门店账号", st_acc))
                   if a.get("has_password") or a.get("has_token") or a.get("builtin")]
        erp_ok = bool(usables)
        items.append(_item("erp", "云商账号", erp_ok,
                           why="" if erp_ok else "没有云商凭据（设置页填一次就行）",
                           need="取不到云商销售/库存，池C、池D 会空着",
                           detail="%s｜%s" % (erp_acc.get("user") or "—",
                                              st_acc.get("user") or "—"),
                           usable=usables))

    # ② 华为会话：文件在不在、鲜不鲜
    sp = session_path(cfg, root)
    ok, why, age_h = False, "", None
    if not sp.exists():
        why = "还没有会话文件（要人工登录一次）"
    else:
        age_h = (datetime.datetime.now().timestamp() - sp.stat().st_mtime) / 3600.0
        try:
            sess = CbgSession.load(sp)
            ok = bool(getattr(sess, "cookies", "") and getattr(sess, "csrf", ""))
            why = "" if ok else "会话文件里没有 cookie/csrf（重抓一份）"
        except Exception as e:                                 # noqa: BLE001
            why = "会话文件读不出来：%s" % e
    stale = age_h is not None and age_h > SESSION_WARN_DAYS * 24
    if ok and stale:
        why = "会话存了 %.0f 天了，可能快过期（到点会自己续期）" % (age_h / 24.0)
    items.append(_item("cbg-session", "华为会话", ok,
                       why=why, todo=not ok,
                       need="抓不到华为订单 → 报量核对和 POS 都跑不了",
                       detail=str(sp), age_hours=round(age_h, 1) if age_h is not None else None,
                       stale=bool(ok and stale)))

    # ③ 华为 SSO 账号：会话过期时能不能自己续
    cbg = acc.get("cbg") or {}
    ready = bool(cbg.get("ready"))
    items.append(_item("cbg-login", "华为登录账号", ready,
                       why="" if ready else "没存 SSO 账号 → 会话过期只能人工登录一次",
                       todo=not ready,
                       need="会话一过期，自动续期就失效（不影响今天跑，只影响自愈）",
                       detail=cbg.get("username") or ""))

    bad = [i for i in items if not i["ok"]]
    return {"items": items, "ok": not bad,
            "need_login": any(i["key"] == "cbg-session" and not i["ok"] for i in items),
            "login_url": cbg.get("login_url") or "",
            "session_file": str(sp),
            "why": "；".join(i["why"] for i in bad if i["why"])}


def _item(key, name, ok, *, why="", need="", todo=False, **extra) -> dict:
    """一条自检项。⚠ `need`（缺了会怎样）和 `why`（为什么缺）**分开写** ——

    门店看不懂"csrf 为空"，但看得懂"抓不到华为订单"。界面上两个都要有。
    """
    row = {"key": key, "name": name, "ok": bool(ok), "why": why, "need": need,
           "todo": bool(todo or not ok)}
    row.update(extra)
    return row


def session_age_days(cfg: dict, root=None) -> Optional[float]:
    """会话存了多少天 —— 健康面板 / 排查用。没有文件就 `None`。"""
    sp = session_path(cfg, root)
    if not sp.exists():
        return None
    return (datetime.datetime.now().timestamp() - sp.stat().st_mtime) / 86400.0
