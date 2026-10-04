# -*- coding: utf-8 -*-
"""权益领取功能的 HTTP 路由处理。

身份与范围的总门禁由公共路由注册表执行；此处负责把各领取接口交给本功能，
并保留应用层对串号、活动和状态键的细粒度校验。
"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/claim/activities"),
    ("GET", "/api/claim/pending"),
    ("POST", "/api/claim/status"),
    ("POST", "/api/claim/query"),
    ("POST", "/api/claim/submit"),
)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json):
    """处理权益领取路由；未匹配时返回 ``None``。"""
    if path == "/api/claim/activities" and method == "GET":
        denied = require(scope, "claim-pending", "view")
        if denied:
            return denied, 403
        return app.claim_activities(), 200

    if path == "/api/claim/pending" and method == "GET":
        denied = require(scope, "claim-pending", "view")
        if denied:
            return denied, 403
        return app.claim_pending(), 200

    if path == "/api/claim/status" and method == "POST":
        denied = require(scope, "claim-pending", "modify")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.claim_status_set(
            str(body.get("key") or ""),
            str(body.get("status") or ""),
            by=str(scope.get("who") or scope.get("account") or ""),
            note=str(body.get("note") or ""),
        )
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "更新状态失败")
            # 越权用 403（跟 forbid 同一档），参数/业务失败仍 400。
            return result, 403 if result.get("forbidden") else 400
        return result, 200

    if path == "/api/claim/query" and method == "POST":
        denied = require(scope, "claim-pending", "view")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.claim_query(str(body.get("sn") or ""),
                                 str(body.get("activity_id") or ""))
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "查询失败")
            return result, 403 if result.get("forbidden") else 400
        return result, 200

    if path == "/api/claim/submit" and method == "POST":
        denied = require(scope, "claim-pending", "enter")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.claim_submit_online(
            str(body.get("sn") or ""),
            str(body.get("activity_id") or ""),
            status_key=str(body.get("status_key") or ""),
        )
        # 已领取要回 200，前端据此标记状态；其他失败保留原有 403/400 判据。
        if result.get("already_claimed"):
            return dict(result, error=None), 200
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "领取失败")
            return result, 403 if result.get("forbidden") else 400
        return result, 200

    return None
