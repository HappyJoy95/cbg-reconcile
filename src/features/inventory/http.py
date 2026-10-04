# -*- coding: utf-8 -*-
"""库存盘点页的 HTTP 路由处理。"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/inventory/ready"),
    ("GET", "/api/inventory/warehouses"),
    ("POST", "/api/inventory/book"),
    ("POST", "/api/inventory/transit"),
    ("POST", "/api/inventory/index"),
    ("POST", "/api/inventory/export"),
)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json, read_bytes, max_upload,
           inventory_warehouse_allowed, scope_store_ok, forbid):
    """处理盘点接口；全仓索引只保留在其单独声明的 POST 路由。"""
    if path == "/api/inventory/ready" and method == "GET":
        denied = require(scope, "inventory", "view")
        if denied:
            return denied, 403
        return app.inventory_ready(), 200

    if path == "/api/inventory/warehouses" and method == "GET":
        denied = require(scope, "inventory", "view")
        if denied:
            return denied, 403
        result = app.inventory_warehouses()
        if result.get("ok"):
            warehouses = [w for w in (result.get("warehouses") or ())
                          if inventory_warehouse_allowed(scope, w)]
            allowed_ids = {str(w.get("Id") or w.get("id") or "")
                           for w in warehouses}
            result = dict(result)
            result["warehouses"] = warehouses
            if str(result.get("default_store_id") or "") not in allowed_ids:
                result["default_store_id"] = ""
                result["default_store_name"] = ""
        return result, 200

    if path in ("/api/inventory/book", "/api/inventory/transit") \
            and method == "POST":
        denied = require(scope, "inventory", "view")
        if denied:
            return denied, 403
        body = read_json() or {}
        store_id = str(body.get("storeId") or "").strip()
        warehouses = app.inventory_warehouses()
        allowed = (warehouses.get("ok") and any(
            str(w.get("Id") or w.get("id") or "") == store_id
            and inventory_warehouse_allowed(scope, w)
            for w in (warehouses.get("warehouses") or ())))
        if not allowed:
            what = "inventory:book" if path.endswith("/book") else "inventory:transit"
            return forbid(scope, what, "目标仓库不在当前授权范围内"), 403
        day = str(body.get("date") or "")
        if path.endswith("/book"):
            return app.inventory_book(day, store_id), 200
        return app.inventory_transit(day, store_id), 200

    if path == "/api/inventory/index" and method == "POST":
        denied = require(scope, "inventory", "view")
        if denied:
            return denied, 403
        body = read_json() or {}
        return app.inventory_index(str(body.get("date") or "")), 200

    if path == "/api/inventory/export" and method == "POST":
        denied = require(scope, "inventory", "export")
        if denied:
            return denied, 403
        store = (query.get("store") or [""])[0]
        if not scope_store_ok(scope, store):
            return forbid(scope, "inventory:export",
                          "目标门店不在当前授权范围内"), 403
        try:
            data = read_bytes(max_upload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}, 400
        result = app.inventory_export(
            (query.get("name") or [""])[0], data, store=store,
            date=(query.get("date") or [""])[0])
        return result, 200

    return None
