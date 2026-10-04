# -*- coding: utf-8 -*-
"""收银录入页的 HTTP 路由处理。"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/cashier/entries"),
    ("GET", "/api/cashier/lookup"),
    ("GET", "/api/cashier/import-settings"),
    ("PUT", "/api/cashier/import-settings"),
    ("POST", "/api/cashier/entry-save"),
    ("POST", "/api/cashier/entry-delete"),
    ("POST", "/api/cashier/policy-refresh"),
    ("POST", "/api/cashier/import"),
    ("POST", "/api/cashier/exclude"),
    ("POST", "/api/cashier/commit"),
    ("POST", "/api/cashier/export"),
)

_OPS = {
    ("GET", "/api/cashier/entries"): "view",
    ("GET", "/api/cashier/lookup"): "view",
    ("GET", "/api/cashier/import-settings"): "view",
    ("PUT", "/api/cashier/import-settings"): "modify",
    ("POST", "/api/cashier/entry-save"): "enter",
    ("POST", "/api/cashier/entry-delete"): "modify",
    ("POST", "/api/cashier/policy-refresh"): "modify",
    ("POST", "/api/cashier/import"): "enter",
    ("POST", "/api/cashier/exclude"): "modify",
    ("POST", "/api/cashier/commit"): "enter",
    ("POST", "/api/cashier/export"): "export",
}


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json, register_export_result, failure_status):
    """处理收银路由；未匹配时返回 ``None``。"""
    if not path.startswith("/api/cashier"):
        return None

    operation = _OPS.get((method, path))
    if operation is None:
        return {"error": "接口不存在"}, 404
    denied = require(scope, "cashier", operation,
                     why="收银是门店本机的操作")
    if denied:
        return denied, 403

    if path == "/api/cashier/entries":
        result = app.cashier_entries((query.get("day") or [""])[0])
        message = "读取失败"
    elif path == "/api/cashier/entry-save":
        result = app.cashier_entry_save(read_json() or {})
        message = "保存失败"
    elif path == "/api/cashier/entry-delete":
        result = app.cashier_entry_delete((read_json() or {}).get("id"))
        message = "删除失败"
    elif path == "/api/cashier/lookup":
        result = app.cashier_lookup((query.get("code") or [""])[0])
        message = "反查失败"
    elif path == "/api/cashier/policy-refresh":
        result = app.cashier_policy_refresh()
        message = "更新失败"
    elif path == "/api/cashier/import":
        result = app.cashier_import(read_json() or {})
        message = "导入失败"
    elif path == "/api/cashier/exclude":
        result = app.cashier_exclude((read_json() or {}).get("id"))
        message = "排除失败"
    elif path == "/api/cashier/import-settings" and method == "GET":
        result = app.cashier_import_settings()
        message = "读取失败"
    elif path == "/api/cashier/import-settings" and method == "PUT":
        result = app.cashier_import_settings(read_json() or {}, save=True)
        message = "保存失败"
    elif path == "/api/cashier/commit":
        result = app.cashier_commit(read_json() or {})
        message = "入库失败"
    elif path == "/api/cashier/export":
        result = app.cashier_export(
            read_json() or {},
            who=scope.get("who") or scope.get("account") or "")
        result = register_export_result(app.root, "cashier", result, scope)
        message = "导出失败"
    else:  # All declared paths are handled above; keep unexpected paths closed.
        return {"error": "接口不存在"}, 404

    if not result.get("ok"):
        result = dict(result, error=result.get("why") or message)
        return result, failure_status(result)
    return result, 200
