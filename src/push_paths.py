"""推送路径列表 —— 邮件 / 企微各一份，存 `.secrets/push-paths.json`。

2026-09-22 设置改版：界面上**去掉**发送时机、启用勾选、@所有人、传附件，
只保留「推到哪」；每条渠道改成**可增删的路径列表**，有几条发几条。

* **列表空** = 这条渠道没配（等同以前 `enabled: false`）。
* **老配置只读回落**：没有本文件时，从 `mail:` / `wecom:` + `.secrets/*.env`
  拼成**一条**路径 —— 升级后门店不用重填。
* 密码 / webhook 是凭据，所以整份 JSON 放 `.secrets/`（自更新不碰）。
"""

from __future__ import annotations

import json
import os
import secrets as _py_secrets
from pathlib import Path
from typing import List, Optional

from . import envfile

#: 相对项目根的路径
PUSH_PATHS_FILE = ".secrets/push-paths.json"


def paths_file(root=None) -> Path:
    return envfile.resolve(PUSH_PATHS_FILE, root)


def _new_id() -> str:
    return _py_secrets.token_hex(4)


def load_raw(root=None) -> dict:
    """读整份文件；没有 / 坏了都给 `{}`（调用方再走老配置回落）。"""
    p = paths_file(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("mail", "wecom"):
        rows = data.get(key)
        if isinstance(rows, list):
            out[key] = [r for r in rows if isinstance(r, dict)]
    return out


def has_file(root=None) -> bool:
    """**真有这个文件**才算「用列表模式」—— 没有就回落老配置（别写成空列表）。"""
    return paths_file(root).is_file()


def save(data: dict, root=None) -> Path:
    """整份写回。`data` 只认 `mail` / `wecom` 两个列表键。"""
    payload = {
        "mail": [r for r in (data.get("mail") or []) if isinstance(r, dict)],
        "wecom": [r for r in (data.get("wecom") or []) if isinstance(r, dict)],
    }
    p = paths_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)
    return p


def ensure_ids(rows: List[dict]) -> List[dict]:
    """给缺 `id` 的行补上（前端删除/测试要稳定 id）。"""
    for r in rows:
        if not r.get("id"):
            r["id"] = _new_id()
    return rows


def mail_row_from_legacy(mc) -> Optional[dict]:
    """老 `MailConfig` → 一条路径行；**没配好就不造行**（避免空壳路径）。"""
    if mc is None:
        return None
    if not (mc.host and mc.recipients and mc.from_addr):
        return None
    return {
        "id": _new_id(),
        "host": mc.host,
        "port": int(mc.port or 465),
        "security": mc.security or "ssl",
        "username": mc.username or "",
        "password": mc.password or "",
        "sender": mc.sender or "",
        "recipients": ", ".join(mc.recipients or []),
        "subject_prefix": mc.subject_prefix or "[报量对账]",
        "env_file": mc.env_file or "",
        "cc": ", ".join(mc.cc or []),
    }


def wecom_row_from_legacy(wc) -> Optional[dict]:
    if wc is None or not wc.key:
        return None
    return {
        "id": _new_id(),
        "webhook": wc.webhook or "",
        "mention_all": bool(wc.mention_all),
        "send_file": bool(wc.send_file),
        "env_file": wc.env_file or "",
    }


def migrate_from_legacy(cfg: dict, root=None) -> bool:
    """**一次性**把老配置收成路径列表。已有文件就不动。

    返回是否新建了文件。只在**真有可用路径**时写 —— 空列表也写的话，
    会把「还没配」误伤成「用列表模式且为空」，老配置回落就再也走不到了。
    """
    if has_file(root):
        return False
    from .integrations import mailer, wecom
    mc = mailer.load_mail_config(cfg, root)
    wc = wecom.load_wecom_config(cfg, root)
    mail_rows = ensure_ids([r] if (r := mail_row_from_legacy(mc)) else [])
    wecom_rows = ensure_ids([r] if (r := wecom_row_from_legacy(wc)) else [])
    if not mail_rows and not wecom_rows:
        return False
    save({"mail": mail_rows, "wecom": wecom_rows}, root)
    return True
