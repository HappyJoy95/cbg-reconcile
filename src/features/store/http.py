"""人员设置及门店上报人员状态的 HTTP 处理。公共层负责统一路由门禁与 JSON 响应。"""

from __future__ import annotations

from . import staff


def handle(method, path, scope, *, root, config_path, env_file,
           read_json, forbid, audit, scope_store_ok):
    if path != "/api/staff":
        return None

    if method == "GET":
        if scope.get("role") != "store":
            return staff.inbox_state(root, scope,
                                     scope_store_ok=scope_store_ok), 200
        return staff.staff_state(root, config_path, env_file), 200

    if method not in ("PUT", "POST"):
        return None

    body = read_json()
    excluded = body.get("excluded") if isinstance(body, dict) else None
    if not isinstance(excluded, list):
        return {"error": "excluded 得是一个数组"}, 400

    # 路由声明先拦；这里保留原有能力复核，防止旧调用路径绕过门禁。
    if not (scope.get("can") or {}).get("staff.write"):
        return forbid(scope, "改人员设置", "这个身份不能改人员设置"), 403

    staff.save_excluded(root, excluded)
    audit("人员设置", scope, rows=len(excluded))
    scheduled = {}
    if scope.get("role") == "store":
        scheduled = staff.schedule_report(
            root, who=scope.get("who") or scope.get("account") or "")
    return {"ok": True, "saved": True,
            "report_in": scheduled.get("in", 0),
            **staff.staff_state(root, config_path, env_file)}, 200
