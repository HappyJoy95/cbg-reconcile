"""本机运行策略：安装版与用户选择入口共同约束可用能力。

不改安装版、更新分支或用户存下来的任务/凭据；每次按明确根目录读取。
"""
from __future__ import annotations

import json
from pathlib import Path
from ... import edition
from ...paths import ROOT

# 页面名单只由安装 Edition 持有；运行时入口复用它，避免菜单和 API 各有一份准入表。
LIFEHALL_PAGES = edition.LIFEHALL_PAGES

# 明确列可用 API，新增业务路由未接运行能力时默认拒绝。
# 不用前缀放行：授权和更新保留，未知子路由不会顺便获得能力。
LIFEHALL_APIS = frozenset((
    "/api/health", "/api/boot", "/api/setup", "/api/setup/preview",
    "/api/entry", "/api/shutdown",
    "/api/status", "/api/overview", "/api/data-state/dismiss", "/api/notify-pref", "/api/runlog", "/api/run", "/api/run/stop",
    "/api/session", "/api/session/ping", "/api/session/store-code",
    "/api/session/auto", "/api/session/auto/browser", "/api/hwlogin",
    "/api/wallpaper", "/api/config", "/api/service", "/api/autostart",
    "/api/update", "/api/whatsnew", "/api/whatsnew/seen", "/api/report-bug",
    "/api/refresh", "/api/claim/activities", "/api/claim/pending",
    "/api/claim/status", "/api/claim/query", "/api/claim/submit",
    "/api/cashier/entries", "/api/cashier/entry-save", "/api/cashier/entry-delete",
    "/api/cashier/lookup", "/api/cashier/policy-refresh", "/api/cashier/import",
    "/api/cashier/exclude", "/api/cashier/import-settings", "/api/cashier/commit",
    "/api/cashier/export", "/api/export/download",
))


def is_lifehall(root=None) -> bool:
    base = Path(root) if root is not None else ROOT
    if edition.is_lifehall(base):
        return True
    try:
        entry = json.loads((base / ".secrets/entry.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(entry, dict) and entry.get("kind") == "lifehall"


def page_available(key, root=None, lifehall=None) -> bool:
    restricted = is_lifehall(root) if lifehall is None else lifehall
    return not restricted or key in LIFEHALL_PAGES


def step_available(cmd, root=None, recurring=False) -> bool:
    if not is_lifehall(root):
        return True
    return cmd == "autoupdate" or (cmd == "dump" and not recurring)


def api_available(path, root=None) -> bool:
    return not is_lifehall(root) or path in LIFEHALL_APIS
