# -*- coding: utf-8 -*-
"""月度生意计划页的 HTTP 路由处理。"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/plan"),
    ("POST", "/api/plan/export"),
)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json, register_export_result):
    """处理月度计划路由；未匹配时返回 ``None``。"""
    if path == "/api/plan" and method == "GET":
        denied = require(scope, "monthly", "view")
        if denied:
            return denied, 403
        return app.plan(), 200

    if path == "/api/plan/export" and method == "POST":
        denied = require(scope, "monthly", "export",
                         why="这个身份不能导出月度生意计划")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.plan_export(
            who=scope.get("who") or scope.get("account") or "",
            name=body.get("name") or "")
        result = register_export_result(app.root, "monthly", result, scope)
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "导出失败")
        return result, 200 if result.get("ok") else 400

    return None
