"""Effective local authorization for importing reports from the shared mailbox."""
from __future__ import annotations

import json
from pathlib import Path

from . import runtime


def store_codes(scope, root):
    """Map an already-resolved identity scope to safe Huawei store codes.

    `None` means an explicitly resolved platform-wide identity. Every other
    missing, invalid, or incomplete scope is represented by an empty set.
    """
    if (isinstance(scope, dict) and scope.get("role") == "platform"
            and scope.get("stores") is None):
        return None
    if not isinstance(scope, dict) or scope.get("role") != "manager":
        return frozenset()
    stores = scope.get("stores")
    if not isinstance(stores, (set, frozenset, list, tuple)):
        return frozenset()
    allowed_names = {str(name or "").strip() for name in stores if str(name or "").strip()}
    if not allowed_names:
        return frozenset()

    from ... import config_io
    try:
        rows = config_io.stores_table(root)
    except Exception:                                          # noqa: BLE001
        return frozenset()
    if not isinstance(rows, (list, tuple)) or not rows:
        return frozenset()

    grouped = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("huawei_code") or "").strip()
        if not code:
            continue
        aliases = {str(row.get(key) or "").strip()
                   for key in ("erp_name", "tdoc_name")
                   if str(row.get(key) or "").strip()}
        state = grouped.setdefault(code, True)
        if not aliases or not aliases.issubset(allowed_names):
            grouped[code] = False
    return frozenset(code for code, valid in grouped.items() if valid)


def local_store_codes(root=None, cfg=None, config_path=None):
    """Resolve the local account for CLI/timer imports without exposing credentials.

    The API path supplies its already-gated request scope directly. Unattended
    paths derive the same manager/platform distinction from the saved entry,
    store-login identity, manager roster, and store roster; uncertainty denies
    import instead of assuming platform scope.
    """
    from ...paths import ROOT
    root = Path(root) if root is not None else ROOT
    if runtime.is_lifehall(root):
        return frozenset()
    try:
        saved = json.loads((root / ".secrets/entry.json").read_text(encoding="utf-8"))
        entry_kind = str(saved.get("kind") or "") if isinstance(saved, dict) else ""
    except (OSError, ValueError):
        entry_kind = ""
    if entry_kind not in ("", "erp", "platform"):
        return frozenset()

    from ... import config_io, erp
    try:
        if cfg is None:
            path = Path(config_path or "config/store-SCN231409.yaml")
            if not path.is_absolute():
                path = root / path
            cfg = config_io.load_raw(path) or {}
        creds = erp.describe_store_credentials(root / erp.STORE_ENV_FILE)
        username = str(creds.get("username") or "").strip()
        who = str(creds.get("who") or "").strip()
        roster = config_io.stores_table(root)
        managers = config_io.managers_table(root)
        profile = config_io.store_profile(config_io.pick(cfg or {}), root)
    except Exception:                                          # noqa: BLE001
        return frozenset()

    matches = []
    for manager in managers or ():
        accounts = {str(account or "").strip().upper()
                    for account in (manager.get("accounts") or ()) if str(account or "").strip()}
        name = str(manager.get("name") or "").strip()
        matched = (username and username.upper() in accounts) or (
            not accounts and who and name == who)
        if matched:
            matches.append(manager)
    if matches:
        if entry_kind not in ("", "erp") or len(matches) != 1:
            return frozenset()
        names = set(config_io.stores_of_manager(matches[0], roster))
        # Match role_scope's alias expansion so a manager authorized by an ERP
        # name can also receive a store's Huawei/TDOC package code.
        for row in roster or ():
            aliases = {str(row.get(key) or "").strip()
                       for key in ("erp_name", "tdoc_name")
                       if str(row.get(key) or "").strip()}
            if aliases & names:
                names.update(aliases)
        return store_codes({"role": "manager", "stores": names}, root)

    if profile.get("type") == "platform" and entry_kind in ("", "platform"):
        return None
    return frozenset()
