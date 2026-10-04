# -*- coding: utf-8 -*-
"""POS 合规页的 HTTP 路由处理。"""

from __future__ import annotations


ROUTES = (("GET", "/api/pos"),)


def handle(app, method: str, path: str, query: dict, scope: dict, *, require):
    """处理 POS 合规路由；未匹配时返回 ``None``。"""
    if path == "/api/pos" and method == "GET":
        denied = require(scope, "pos", "view")
        if denied:
            return denied, 403
        return app.pos(scope), 200
    return None
