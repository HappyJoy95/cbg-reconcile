"""报量查询的 HTTP 路由与授权范围过滤。"""

from __future__ import annotations

import json
from pathlib import Path


ROUTES = (
    ("GET", "/api/pools/history"),
    ("POST", "/api/pools-notify/clear"),
    ("GET", "/api/report"),
    ("DELETE", "/api/report"),
    ("GET", "/api/report/download"),
)

POOL_STORE_KEYS = ("云商门店", "玲珑门店")


class FileResponse:
    """经业务授权后交给公共 HTTP 层发送的本地文件。"""

    def __init__(self, path, content_type, filename):
        self.path = Path(path)
        self.content_type = content_type
        self.filename = filename


def report_target(out_dir, name):
    """只解析 out/ 直属的 xlsx 报告；拒绝目录穿越和符号链接越界。"""
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    base = Path(out_dir).resolve()
    target = (base / name).resolve()
    if target.parent != base or target.suffix.lower() != ".xlsx" or not target.is_file():
        return None
    return target


def report_summary(target):
    """历史报告归属来自旁车摘要；缺失或损坏时有限范围身份默认拒绝。"""
    try:
        data = Path(target).with_suffix(".json").read_text(encoding="utf-8")
        data = json.loads(data)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def report_in_scope(scope, summary, root=None, *, scope_store_ok,
                    has_data_grant) -> bool:
    """平台可读全量报告；其他角色必须由报告门店落在已解析授权范围内。"""
    if not has_data_grant(scope):
        return False
    if scope.get("role") == "platform" and scope.get("stores") is None:
        return True
    if not isinstance(summary, dict):
        return False
    store = str(summary.get("store") or "").strip()
    if not scope_store_ok(scope, store):
        return False
    code = str(summary.get("store_code") or "").strip()
    if not code:
        return True
    if root is None:
        return False
    try:
        from .... import config_io
        rows = config_io.stores_table(root)
    except Exception:                                      # noqa: BLE001
        return False
    aliases = set()
    for row in rows:
        if str(row.get("huawei_code") or "").strip() == code:
            aliases.update(str(row.get(key) or "").strip()
                           for key in ("erp_name", "tdoc_name")
                           if str(row.get(key) or "").strip())
    return bool(aliases and store in aliases
                and all(scope_store_ok(scope, alias) for alias in aliases))


def reports_in_scope(scope, reports, root=None, *, require, scope_store_ok,
                     has_data_grant):
    """概览中的旧报告摘要按页面权限与报告门店双重收窄。"""
    if require(scope, "pools", "view"):
        return []
    return [item for item in (reports or ())
            if isinstance(item, dict) and report_in_scope(
                scope, item, root, scope_store_ok=scope_store_ok,
                has_data_grant=has_data_grant)]


def scope_detail(scope: dict, detail: dict, *, scope_store_ok,
                 has_data_grant) -> dict:
    """按授权门店过滤四池明细；来源不明或跨授权边界的行不返回。"""
    out = dict(detail or {})
    if (has_data_grant(scope) and scope.get("role") == "platform"
            and scope.get("stores") is None):
        return out
    valid_scope = has_data_grant(scope)
    for key in ("AD", "BC"):
        rows = []
        for row in (out.get(key) or []):
            names = [str(row.get(field) or "").strip() for field in POOL_STORE_KEYS]
            names = [name for name in names if name]
            if valid_scope and names and all(scope_store_ok(scope, name) for name in names):
                rows.append(row)
        out[key] = rows
    counts = dict(out.get("counts") or {})
    for key in ("AD", "BC"):
        counts[key] = len(out.get(key) or [])
    for key in ("AC", "BD", "BC_样机"):
        counts[key] = None
    out["counts"] = counts
    out["scoped"] = True
    return out


def scope_days(scope: dict, root, year=None, *, pools_history,
               scope_store_ok, has_data_grant) -> list:
    """按当前范围重算历史列表，隐藏无法按店重算的全公司计数。"""
    if not has_data_grant(scope):
        return []
    if scope.get("role") == "platform" and scope.get("stores") is None:
        return pools_history.days(root, year)
    if year:
        years = [int(year)]
    else:
        import datetime as _dt
        years = [_dt.date.today().year]
    out = []
    for current_year in years:
        for day, record in pools_history.load(root, current_year).items():
            scoped = scope_detail(scope, {
                "counts": (record or {}).get("counts") or {},
                "AD": (record or {}).get("AD") or [],
                "BC": (record or {}).get("BC") or [],
            }, scope_store_ok=scope_store_ok, has_data_grant=has_data_grant)
            ad_count = len(scoped.get("AD") or [])
            bc_count = len(scoped.get("BC") or [])
            if not ad_count and not bc_count:
                continue
            counts = scoped.get("counts") or {}
            out.append({"date": str(day), "AD": ad_count, "BC": bc_count,
                        "AC": counts.get("AC"), "BD": counts.get("BD"),
                        "BC_样机": counts.get("BC_样机")})
    out.sort(key=lambda row: row["date"], reverse=True)
    return out


def scope_years(scope: dict, root, *, pools_history, scope_store_ok,
                has_data_grant) -> list:
    """只列出当前授权范围确实含有明细的年份。"""
    if not has_data_grant(scope):
        return []
    if scope.get("role") == "platform" and scope.get("stores") is None:
        return pools_history.years(root)
    return [year for year in pools_history.years(root)
            if scope_days(scope, root, year, pools_history=pools_history,
                          scope_store_ok=scope_store_ok,
                          has_data_grant=has_data_grant)]


def handle(app, method: str, path: str, query: dict, scope: dict, *,
           require, scope_store_ok, has_data_grant,
           forbid, load_report=None, delete_report=None):
    """处理报量查询历史与推送记忆接口；未匹配的路由返回 ``None``。"""
    if path == "/api/pools/history" and method == "GET":
        denied = require(scope, "pools", "view")
        if denied:
            return denied, 403
        from .... import pools_history

        year = (query.get("year") or [""])[0]
        day = (query.get("date") or [""])[0].strip()
        selected_year = int(year) if str(year).isdigit() else None
        if day:
            result = pools_history.detail(app.root, day, selected_year)
            return scope_detail(scope, result, scope_store_ok=scope_store_ok,
                                has_data_grant=has_data_grant), 200
        return {"years": scope_years(
                    scope, app.root, pools_history=pools_history,
                    scope_store_ok=scope_store_ok, has_data_grant=has_data_grant),
                "days": scope_days(
                    scope, app.root, selected_year, pools_history=pools_history,
                    scope_store_ok=scope_store_ok, has_data_grant=has_data_grant)}, 200

    if path == "/api/pools-notify/clear" and method == "POST":
        denied = require(scope, "compliance-settings", "modify")
        if denied:
            return denied, 403
        from .... import pools_notify

        had = pools_notify.clear(app.root)
        return {"ok": True, "had": had,
                "message": "已清除推送记忆" if had else "本来就没有推送记忆"}, 200

    if path == "/api/report" and method == "GET":
        denied = require(scope, "pools", "view")
        if denied:
            return denied, 403
        target = report_target(app.out_dir, (query.get("name") or [""])[0])
        if target is None:
            return {"error": "没有这份报告"}, 404
        summary = report_summary(target)
        if not report_in_scope(scope, summary, app.root,
                               scope_store_ok=scope_store_ok,
                               has_data_grant=has_data_grant):
            return forbid(scope, "读取报量报告",
                          "只能查看当前授权门店的报告"), 403
        if load_report is None:
            from ....report import load_report as load_report_fn
        else:
            load_report_fn = load_report
        report = load_report_fn(target)
        if summary is not None:
            report["summary"] = summary
        return {"name": target.name, **report}, 200

    if path == "/api/report" and method == "DELETE":
        denied = require(scope, "pools", "modify")
        if denied:
            return denied, 403
        target = report_target(app.out_dir, (query.get("name") or [""])[0])
        if target is None:
            return {"error": "没有这份报告"}, 404
        if not report_in_scope(scope, report_summary(target), app.root,
                               scope_store_ok=scope_store_ok,
                               has_data_grant=has_data_grant):
            return forbid(scope, "删除报量报告",
                          "只能删除当前授权门店的报告"), 403
        if delete_report is None:
            from ....report import delete_report as delete_report_fn
        else:
            delete_report_fn = delete_report
        ok, message = delete_report_fn(app.out_dir, target.name)
        return {"ok": ok, "message": message}, 200 if ok else 400

    if path == "/api/report/download" and method == "GET":
        denied = require(scope, "pools", "export")
        if denied:
            return denied, 403
        target = report_target(app.out_dir, (query.get("name") or [""])[0])
        if target is None:
            return {"error": "没有这份报告"}, 404
        if not report_in_scope(scope, report_summary(target), app.root,
                               scope_store_ok=scope_store_ok,
                               has_data_grant=has_data_grant):
            return forbid(scope, "下载报量报告",
                          "只能下载当前授权门店的报告"), 403
        return FileResponse(
            target,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            target.name)

    return None
