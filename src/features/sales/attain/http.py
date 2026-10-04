"""周度达成页的 HTTP 路由处理。

身份门禁、通用范围判据和响应编码由公共入口注入；达成数据与拆分实现
继续由本功能模块的执行代码负责。
"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/attain"),
    ("POST", "/api/attain/export"),
    ("GET", "/api/attain/split"),
    ("PUT", "/api/attain/split"),
    ("GET", "/api/attain/history"),
    ("POST", "/api/attain/split/send"),
)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, read_json, scope_store_ok, forbid, register_export_result):
    """处理周度达成路由；未匹配时返回 `None`。"""
    if path == "/api/attain" and method == "GET":
        denied = require(scope, "attain", "view")
        if denied:
            return denied, 403
        # App.attain 会从当前身份重新过滤落盘数据，兼容历史全店快照。
        return app.attain(), 200

    if path == "/api/attain/export" and method == "POST":
        denied = require(scope, "attain", "export",
                         why="这个身份不能导出达成数据")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.attain_export(
            who=scope.get("who") or scope.get("account") or "",
            name=body.get("name") or "")
        result = register_export_result(app.root, "attain", result, scope)
        if not result.get("ok"):
            result = dict(result, error=result.get("why") or "导出失败")
        return result, 200 if result.get("ok") else 400

    if path == "/api/attain/split" and method == "GET":
        denied = require(scope, "attain", "view")
        if denied:
            return denied, 403
        store = (query.get("store") or [""])[0]
        period = (query.get("period") or [""])[0]
        roster = [name.strip() for name in
                  ((query.get("roster") or [""])[0] or "").split(",")
                  if name.strip()]
        if (query.get("all") or [""])[0] in ("1", "true", "yes"):
            return app.attain_split_all(roster), 200
        if not scope_store_ok(scope, store):
            return forbid(scope, "看目标拆分",
                          "只能看自己范围内的门店：%s" % (store or "（没给门店）")), 403
        return app.attain_split(store, period, roster), 200

    if path == "/api/attain/history" and method == "GET":
        denied = require(scope, "attain", "view")
        if denied:
            return denied, 403
        period = (query.get("period") or [""])[0]
        return app.attain_history(period), 200

    if path == "/api/attain/split/send" and method == "POST":
        body = read_json() or {}
        store = body.get("store") or ""
        denied = require(scope, "attain", "modify",
                         why="只有门店账号能发目标拆分（区长/平台只读）")
        if denied:
            return denied, 403
        if not scope_store_ok(scope, store):
            return forbid(scope, "发送给区长",
                          "只能发自己范围内的门店：%s" % (store or "（没给门店）")), 403
        result = app.attain_split_send(store, body.get("period") or "")
        return result, 200 if result.get("ok") else 400

    if path == "/api/attain/split" and method == "PUT":
        body = read_json() or {}
        store = body.get("store") or ""
        denied = require(scope, "attain", "modify",
                         why="只有门店账号能改目标拆分（区长/平台只读）")
        if denied:
            return denied, 403
        if not scope_store_ok(scope, store):
            return forbid(scope, "改目标拆分",
                          "只能改本店的目标：%s" % (store or "（没给门店）")), 403
        result = app.attain_split_save(store, body.get("period") or "",
                                       body.get("targets") or {})
        return result, 200 if result.get("ok") else 400

    return None
