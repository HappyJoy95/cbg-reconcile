"""无忧会员权益页的 HTTP 数据与路由处理。

身份解析、通用数据范围执行及响应编码由 `src.web` 注入；此模块只持有权益页的
过滤、汇总、取数、导出和路由形状，不反向依赖公共 HTTP 入口。
"""

from __future__ import annotations


ROUTES = (
    ("GET", "/api/benefit"),
    ("POST", "/api/benefit/export"),
    ("GET", "/api/benefit/drill"),
)


def filter_rows(data: dict, scope: dict, data_scope: str, *,
                filter_scoped, has_data_grant) -> dict:
    """按声明过滤门店/区域/人员/赛道，并从过滤后的行重算所有汇总。"""
    from . import metric

    stores = scope.get("stores")
    data["role"] = scope.get("role")
    data["scope"] = scope.get("label") or ""
    if (data_scope == "authorized" and has_data_grant(scope)
            and scope.get("role") == "platform" and stores is None):
        data["store_filter"] = ""
        return data

    store_rows = filter_scoped(scope, data.get("stores") or [], data_scope)
    data["stores"] = store_rows
    data["summary"] = metric.summarize_stores(store_rows)
    by_region = {}
    for row in store_rows:
        by_region.setdefault(row.get("region") or "未分组", []).append(row)
    order = [row.get("region") for row in (data.get("regions") or [])]
    new_regions = []
    seen = set()
    for region in order:
        if region in by_region and region not in seen:
            new_regions.append(metric.region_row(region, stores=by_region[region]))
            seen.add(region)
    for region, group in by_region.items():
        if region not in seen:
            new_regions.append(metric.region_row(region, stores=group))
    data["regions"] = new_regions
    data["summary_regions"] = metric.summarize_regions(new_regions)
    people = filter_scoped(scope, data.get("people") or [], data_scope)
    data["people"] = metric.rank_people(people)
    data["summary_people"] = metric.summarize_people(data["people"])
    tracks = []
    for track in (data.get("tracks") or []):
        rows = filter_scoped(scope, track.get("rows") or [], data_scope)
        tracks.append(dict(track, rows=rows,
                           paid=sum(row.get("paid") or 0 for row in rows)))
    data["tracks"] = tracks
    data["store_filter"] = "、".join(sorted(str(x) for x in stores if x))
    return data


def load(app, scope: dict, day=None, *, data_scope,
         filter_scoped, has_data_grant) -> dict:
    """读取无忧会员数据后，统一按声明范围过滤并重算汇总。"""
    from . import compute

    data = compute.load(app.root, stores=scope.get("stores"), day=day,
                        config_path=app.config_path, env_file=app.erp_env_file())
    return filter_rows(data, scope, data_scope, filter_scoped=filter_scoped,
                       has_data_grant=has_data_grant)


def export(app, scope: dict, who: str = "", name: str = "", day=None, *,
           data_scope, filter_scoped, has_data_grant) -> dict:
    """导出与页面相同日期窗口、相同身份过滤后的结果。"""
    from . import export as export_module

    data = load(app, scope, day=day, data_scope=data_scope,
                filter_scoped=filter_scoped, has_data_grant=has_data_grant)
    return export_module.export(app.root, data, who=who, name=name)


def drill(app, scope: dict, store: str, kind: str, day=None, *,
          scope_store_ok, forbid) -> dict:
    """按店读取明细前先校验授权范围。"""
    if not scope_store_ok(scope, store):
        return forbid(scope, "看销售明细", "只能看自己范围内的门店")
    from . import compute

    return compute.drill_rows(app.root, store=store, kind=kind, day=day)


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, parse_end_day, read_json, register_export_result):
    """处理权益页 HTTP 路由；未匹配时返回 `None`。"""
    if path == "/api/benefit" and method == "GET":
        denied = require(scope, "benefit", "view")
        if denied:
            return denied, 403
        end = parse_end_day((query.get("end") or [""])[0])
        return app.benefit(day=end), 200

    if path == "/api/benefit/export" and method == "POST":
        denied = require(scope, "benefit", "export")
        if denied:
            return denied, 403
        body = read_json() or {}
        result = app.benefit_export(
            who=scope.get("who") or scope.get("account") or "",
            name=body.get("name") or "",
            day=parse_end_day(body.get("end")))
        result = register_export_result(app.root, "benefit", result, scope)
        return result, 200

    if path == "/api/benefit/drill" and method == "GET":
        denied = require(scope, "benefit", "view")
        if denied:
            return denied, 403
        store = (query.get("store") or [""])[0]
        kind = (query.get("kind") or [""])[0]
        if not str(store).strip():
            return {"ok": False, "error": "没指定门店"}, 400
        if not str(kind).strip():
            return {"ok": False, "error": "没指定要看的指标"}, 400
        result = app.benefit_drill(
            store, kind, day=parse_end_day((query.get("end") or [""])[0]))
        if result.get("forbidden"):
            return result, 403
        if not result.get("ok"):
            return dict(result, error=result.get("why") or "读明细失败"), 400
        return result, 200

    return None
