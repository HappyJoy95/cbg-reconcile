"""防护膜页的 HTTP 数据与路由处理。

HTTP 协议层只负责把请求交给这里；身份解析、通用范围判据和响应编码由
`src.web` 注入，避免业务 handler 反向依赖入口模块。
"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/film"),
    ("POST", "/api/film/export"),
    ("GET", "/api/film/drill"),
)


def filter_rows(data: dict, scope: dict, data_scope: str, own,
                filter_scoped) -> dict:
    """按注册声明过滤门店行，并只用过滤后的行重算汇总。"""
    from . import metric

    stores = scope.get("stores")
    data["role"] = scope.get("role")
    data["scope"] = scope.get("label") or ""
    rows = filter_scoped(scope, data.get("rows") or [], data_scope, own=own)
    data["rows"] = rows
    data["summary"] = metric.summarize(rows)
    data["store_filter"] = "、".join(sorted(str(x) for x in (stores or ()) if x))
    return data


def load(app, scope: dict, day=None, *, data_scope="authorized",
         filter_scoped) -> dict:
    """现算本地销售明细，使用调用方已经解析的身份范围。"""
    from . import compute

    data = compute.load(app.root, stores=scope.get("stores"), day=day)
    return filter_rows(data, scope, data_scope, app._own_names(), filter_scoped)


def export(app, scope: dict, who: str = "", name: str = "", day=None, *,
           data_scope="authorized", filter_scoped) -> dict:
    """导出与页面相同日期窗口、相同身份过滤后的结果。"""
    from . import export as export_module

    return export_module.export(
        app.root,
        load(app, scope, day=day, data_scope=data_scope,
             filter_scoped=filter_scoped),
        who=who, name=name)


def drill(app, scope: dict, store: str, kind: str, day=None, *,
          scope_store_ok, forbid) -> dict:
    """按店读取明细前先校验授权范围。"""
    if not scope_store_ok(scope, store):
        return forbid(scope, "看销售明细", "只能看自己范围内的门店")
    from . import compute

    return compute.drill_rows(app.root, store=store, kind=kind, day=day)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, parse_end_day, read_json, register_export_result):
    """处理防护膜 HTTP 路由；未匹配时返回 `None`。"""
    if path == "/api/film" and method == "GET":
        denied = require(scope, "film", "view")
        if denied:
            return denied, 403
        end = parse_end_day((query.get("end") or [""])[0])
        return app.film(day=end), 200

    if path == "/api/film/export" and method == "POST":
        denied = require(scope, "film", "export")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.film_export(
            who=scope.get("who") or scope.get("account") or "",
            name=body.get("name") or "",
            day=parse_end_day(body.get("end")))
        result = register_export_result(app.root, "film", result, scope)
        return result, 200

    if path == "/api/film/drill" and method == "GET":
        denied = require(scope, "film", "view")
        if denied:
            return denied, 403
        store = (query.get("store") or [""])[0]
        kind = (query.get("kind") or [""])[0]
        if not str(store).strip():
            return {"ok": False, "error": "没指定门店"}, 400
        if not str(kind).strip():
            return {"ok": False, "error": "没指定要看的指标"}, 400
        result = app.film_drill(
            store, kind, day=parse_end_day((query.get("end") or [""])[0]))
        if result.get("forbidden"):
            return result, 403
        if not result.get("ok"):
            return dict(result, error=result.get("why") or "读明细失败"), 400
        return result, 200

    return None
