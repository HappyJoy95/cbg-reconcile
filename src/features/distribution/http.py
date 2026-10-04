# -*- coding: utf-8 -*-
"""渠道分销看板的 HTTP 路由处理。"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/dist/board"),
    ("GET", "/api/dist/detail"),
    ("GET", "/api/dist/map"),
    ("POST", "/api/dist/fetch"),
    ("POST", "/api/dist/zone"),
    ("POST", "/api/dist/map/reset"),
    ("POST", "/api/dist/export"),
)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json, register_export_result):
    """处理分销路由；未匹配时返回 ``None``。"""
    if not path.startswith("/api/dist"):
        return None

    denied = require(scope, "distribution", "view",
                     why="这是平台岗看的页面")
    if denied:
        return denied, 403

    if path == "/api/dist/board" and method == "GET":
        return app.dist_board((query.get("kind") or ["region"])[0], query), 200

    if path == "/api/dist/detail" and method == "GET":
        result = app.dist_detail(query)
        if not result.get("ok"):
            return dict(result, error=result.get("why") or "读明细失败"), 400
        return result, 200

    if path == "/api/dist/map" and method == "GET":
        return app.dist_map(query), 200

    if path == "/api/dist/fetch" and method == "POST":
        denied = require(scope, "distribution", "modify",
                         why="这个身份不能拉取")
        if denied:
            return denied, 403
        result = app.dist_fetch(read_json() or {},
                                who=scope.get("who") or scope.get("account") or "")
        if not result.get("ok"):
            return dict(result, error=result.get("why") or "拉取失败"), 400
        return result, 200

    if path == "/api/dist/zone" and method == "POST":
        denied = require(scope, "distribution", "modify",
                         why="这个身份不能改区域映射")
        if denied:
            return denied, 403
        result = app.dist_confirm(read_json() or {},
                                  who=scope.get("who") or scope.get("account") or "")
        if not result.get("ok"):
            return dict(result, error=result.get("why") or "确认失败"), 400
        return result, 200

    if path == "/api/dist/map/reset" and method == "POST":
        denied = require(scope, "distribution", "modify",
                         why="这个身份不能改区域映射")
        if denied:
            return denied, 403
        return app.dist_reset_map(), 200

    if path == "/api/dist/export" and method == "POST":
        denied = require(scope, "distribution", "export")
        if denied:
            return denied, 403
        body = read_json() or {}
        period = {"start": [str(body.get("start") or "")],
                  "end": [str(body.get("end") or "")]}
        result = app.dist_export(
            period, who=scope.get("who") or scope.get("account") or "",
            name=str(body.get("name") or ""))
        result = register_export_result(app.root, "distribution", result, scope)
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "导出失败")
        return result, 200 if result.get("ok") else 400

    return {"error": "没有这个分销接口"}, 404
