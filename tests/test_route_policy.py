"""注册路由与数据范围的 HTTP 执行契约；全部使用隔离对象，不访问现场数据。"""

import ast
from pathlib import Path
import json
import sqlite3
from unittest import mock

import pytest

from src import web
from src.features.compliance.comparison import http as comparison_http
from src.features import registry


def scope(role="store", stores=("甲店",)):
    return {"role": role, "stores": set(stores) if stores is not None else None,
            "entry_kind": "platform" if role == "platform" else "erp",
            "runtime_lifehall": False, "who": "测试", "label": "测试身份"}


@pytest.mark.parametrize("data", ["", None, "unknown", "all"])
def test_untrusted_scope_name_never_means_global_access(data):
    rows = [{"store": "乙店"}]
    assert web.filter_scoped(scope("platform", None), rows, data) == []


@pytest.mark.parametrize("sc", [
    {}, {"role": "platform"}, {"role": "manager", "stores": None},
    {"role": "unknown", "stores": None}, {"role": "store", "stores": set()},
])
def test_missing_or_invalid_scope_does_not_authorize_store(sc):
    assert not web.scope_store_ok(sc, "乙店")
    assert web.filter_scoped(sc, [{"store": "乙店"}], "authorized") == []


@pytest.mark.parametrize("data", ["", None, "unknown", "all"])
def test_require_rejects_pages_without_a_valid_default_data_scope(data):
    rule = {"ops": {"view": frozenset({"platform"})}, "data": data}
    with mock.patch.dict(web.PERM_RULES, {"film": rule}):
        denied = web.require(scope("platform", None), "film", "view")
    assert denied and denied["forbidden"]


def test_business_audience_blocks_platform_page_from_erp_entry():
    sc = scope("platform", None)
    sc["entry_kind"] = "erp"

    denied = web.require(sc, "distribution", "view")

    assert denied and denied["forbidden"]
    assert "进入方式" in denied["error"]


def test_platform_identity_cannot_use_store_entry_even_on_shared_business_pages():
    sc = scope("platform", None)
    sc["entry_kind"] = "erp"

    denied = web.require(sc, "attain", "view")

    assert denied and denied["forbidden"]
    assert "进入方式" in denied["error"]


def test_menu_and_ops_follow_same_business_audience():
    sc = scope("platform", None)
    sc["entry_kind"] = "erp"
    root = Path("/temporary-route-policy-test")

    shaped = web._with_pages(dict(sc), root, [])

    assert "distribution" not in shaped["pages"]
    assert "distribution" not in shaped["ops"]


def test_matching_platform_entry_keeps_platform_feature():
    sc = scope("platform", None)
    sc["entry_kind"] = "platform"

    assert web.require(sc, "distribution", "view") is None
    assert "distribution" in web._with_pages(dict(sc), Path("/temporary-route-policy-test"), [])[
        "pages"]


def test_pages_without_view_operation_are_hidden_from_that_role():
    root = Path("/temporary-route-policy-test")
    store = scope("store", {"甲店"})
    manager = scope("manager", {"甲店"})
    platform = scope("platform", None)

    assert "cashier" in web.pages_for(store, root, [])
    assert "cashier" not in web.pages_for(manager, root, [])
    assert "cashier" not in web.pages_for(platform, root, [])


def test_business_read_uses_the_identity_snapshot_that_passed_route_guard(tmp_path):
    """身份在路由门禁后切换时，App 内部重读身份不能把范围从单店放大全区。"""
    app = web.App(root=tmp_path, config="config/store-test.yaml")
    app.config_path.parent.mkdir(parents=True)
    app.config_path.write_text("erp_store_name: 甲店\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "attain-2026.json").write_text(json.dumps({
        "exists": True, "period": "2026-W40", "columns": [], "weights": [],
        "rows": [
            {"store": "甲店", "erp_name": "甲店", "total": 1, "people": []},
            {"store": "乙店", "erp_name": "乙店", "total": 9, "people": []},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    handler = object.__new__(web.Handler)
    handler.app = app
    handler._json = lambda data, status=200: (status, data)
    actual_role_scope = web.role_scope
    calls = []

    def switch_to_platform_after_gate(target_app):
        calls.append(True)
        if len(calls) == 2:
            target_app.config_path.write_text("platform: true\n", encoding="utf-8")
        return actual_role_scope(target_app)

    with mock.patch.object(web, "role_scope", side_effect=switch_to_platform_after_gate), \
         mock.patch.object(web, "setup_state", return_value={"ready": True}), \
         mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True):
        status, body = handler._api("GET", "/api/attain", {})

    assert status == 200
    assert [row["store"] for row in body["rows"]] == ["甲店"]
    assert len(calls) == 2, "App 内部重新读取身份时也必须复用请求快照"


@pytest.mark.parametrize("sc", [
    {"role": "platform", "entry_kind": "platform"},
    {"role": "manager", "stores": None, "entry_kind": "erp"},
    {"role": "store", "stores": set(), "entry_kind": "erp"},
])
def test_invalid_store_grant_hides_all_business_pages_and_ops(sc):
    shaped = web._with_pages(dict(sc), Path("/temporary-route-policy-test"), [])

    assert shaped["pages"] == []
    assert shaped["ops"] == {}


def test_require_rejects_missing_store_grant_even_for_declared_operation():
    sc = {"role": "platform", "entry_kind": "platform"}

    denied = web.require(sc, "distribution", "view")

    assert denied and denied["forbidden"]
    assert "门店范围" in denied["error"]


def test_hidden_store_type_cannot_call_business_api_directly():
    sc = scope("store")
    sc["needs_linglong"] = False
    denied = web.require(sc, "pos", "view")
    assert denied and denied["forbidden"]


def test_refresh_cannot_start_a_step_for_a_hidden_page():
    sc = scope("store")
    sc["needs_linglong"] = False
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    with mock.patch.object(web.manager, "current", return_value=None), \
         mock.patch.object(web, "_cool_skip", return_value=(("dump",), [])), \
         mock.patch.object(web.manager, "start_steps") as start:
        (status, body), _ = api_request(sc, "/api/refresh", "POST",
                                        body={"page": "pools"}, app=app)
    assert status == 403 and body["forbidden"]
    start.assert_not_called()


def test_inventory_all_data_scope_is_only_the_serial_index_route():
    data = registry.route_data()
    assert data[("POST", "/api/inventory/index")] == "all"
    assert data[("GET", "/api/inventory/warehouses")] == "authorized"
    assert data[("POST", "/api/inventory/book")] == "authorized"
    assert data[("POST", "/api/inventory/transit")] == "authorized"
    assert registry.page_perms()["inventory"]["data"] == "authorized"


def _write_pos_payload(root, stores, order_stores, *, include_scope=True):
    (root / "config").mkdir(exist_ok=True)
    (root / "out").mkdir(exist_ok=True)
    store_rows = []
    for name, code in stores:
        store_rows.append("  - erp_name: %s\n    tdoc_name: %s\n    huawei_code: %s"
                          % (name, name, code))
    (root / "config" / "stores.yaml").write_text(
        "stores:\n" + "\n".join(store_rows) + "\n", encoding="utf-8")
    db = sqlite3.connect(str(root / "out" / "cbg-2026.db"))
    db.execute("CREATE TABLE orders (store_code TEXT, store_name TEXT)")
    db.execute("CREATE TABLE returns (store_code TEXT, store_name TEXT)")
    db.executemany("INSERT INTO orders VALUES (?, ?)", order_stores)
    db.commit()
    db.close()
    payload = {"year": 2026, "rows": [{"month": "2026-10", "orders": 99}]}
    if include_scope:
        payload["source_stores"] = [
            {"store_code": code, "store_name": name} for code, name in order_stores]
    (root / "out" / "pos-2026.json").write_text(json.dumps(payload), encoding="utf-8")


def test_pos_summary_is_hidden_if_source_database_contains_unauthorized_store(tmp_path):
    _write_pos_payload(tmp_path, [("甲店", "SCN-A"), ("乙店", "SCN-B")],
                       [("SCN-A", "甲店"), ("SCN-B", "乙店")])
    app = web.App(tmp_path, "config/store-X.yaml")

    (status, body), _ = api_request(scope("manager", {"甲店"}),
                                    "/api/pos", app=app)

    assert status == 200
    assert body["exists"] is False
    assert body["rows"] == []
    assert "授权范围" in body.get("hint", "")


def test_pos_summary_remains_available_when_every_source_store_is_authorized(tmp_path):
    _write_pos_payload(tmp_path, [("甲店", "SCN-A"), ("乙店", "SCN-B")],
                       [("SCN-A", "甲店")])
    app = web.App(tmp_path, "config/store-X.yaml")

    (status, body), _ = api_request(scope("manager", {"甲店"}),
                                    "/api/pos", app=app)

    assert status == 200
    assert body["exists"] is True
    assert body["rows"][0]["orders"] == 99


def test_pos_summary_fails_closed_when_scope_provenance_is_missing(tmp_path):
    _write_pos_payload(tmp_path, [("甲店", "SCN-A")], [("SCN-A", "甲店")],
                       include_scope=False)
    app = web.App(tmp_path, "config/store-X.yaml")

    (status, body), _ = api_request(scope("manager", {"甲店"}),
                                    "/api/pos", app=app)

    assert status == 200
    assert body["exists"] is False
    assert body["rows"] == []


def test_pos_scope_rejects_a_store_code_mapped_to_authorized_and_unauthorized_stores(tmp_path):
    app = object.__new__(web.App)
    app.root = tmp_path
    roster = [
        {"erp_name": "甲店", "tdoc_name": "甲店", "huawei_code": "SCN-A"},
        {"erp_name": "乙店", "tdoc_name": "乙店", "huawei_code": "SCN-A"},
    ]

    with mock.patch.object(web.config_io, "stores_table", return_value=roster):
        allowed = app._pos_source_in_scope(
            [{"store_code": "SCN-A", "store_name": "甲店"}],
            scope("manager", {"甲店"}),
        )

    assert allowed is False


def test_registry_rejects_all_route_scope_outside_confirmed_index():
    feature = registry.Feature(
        key="x", label="X", ops={"view": ("platform",)},
        children=[registry.Sub(key="xx", label="XX")],
        routes=(("GET", "/api/xx", "xx", "view"),),
        route_data={("GET", "/api/xx"): "all"},
    )
    with mock.patch.object(registry, "all_features", return_value=[feature]):
        problems = registry.validate()
    assert any("all" in msg for msg in problems)


def test_registry_rejects_route_scope_wider_than_its_page():
    route = ("GET", "/api/x", "x", "view")
    feature = registry.Feature(
        key="x", label="X", audience=("erp",), data="store",
        ops={"view": ("store",)}, routes=(route,),
        route_data={(route[0], route[1]): "authorized"},
    )
    with mock.patch.object(registry, "all_features", return_value=[feature]):
        problems = registry.validate()
    assert any("范围" in msg and "扩大" in msg for msg in problems)


def test_registry_rejects_route_scope_narrowing_that_handlers_do_not_enforce():
    route = ("GET", "/api/x", "x", "view")
    feature = registry.Feature(
        key="x", label="X", audience=("erp",), data="authorized",
        ops={"view": ("store",)}, routes=(route,),
        route_data={(route[0], route[1]): "store"},
    )
    with mock.patch.object(registry, "all_features", return_value=[feature]):
        problems = registry.validate()
    assert any("范围收窄" in msg and "尚未执行" in msg for msg in problems)


def api_request(scope_value, path, method="GET", *, body=None, query=None, app=None,
                read_bytes=None):
    app = app or mock.Mock(root=Path("/temporary-route-policy-test"))
    if not getattr(app, "root", None):
        app.root = Path("/temporary-route-policy-test")
    handler = object.__new__(web.Handler)
    handler.app = app
    handler._json = lambda data, status=200: (status, data)
    handler._read_json = lambda: body or {}
    handler._read_bytes = read_bytes or (lambda _limit: b"")
    with mock.patch.object(web, "role_scope", return_value=scope_value), \
         mock.patch.object(web, "setup_state", return_value={"ready": True}), \
         mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True):
        response = handler._api_unlocked(method, path, query or {})
    return response, app


def test_declared_route_permission_runs_before_business_handler():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    with mock.patch.object(registry, "route_perms", return_value={
            ("GET", "/api/claim/activities"): ("film", "export")}), \
         mock.patch.object(web.App, "claim_activities",
                           side_effect=AssertionError("business handler ran")):
        (status, body), _ = api_request(scope(), "/api/claim/activities", app=app)
    assert status == 403 and body["forbidden"]
    app.claim_activities.assert_not_called()


def test_unregistered_endpoint_under_business_api_prefix_is_not_dispatched():
    (status, body), _ = api_request(scope("platform", None), "/api/claim/future")
    assert status == 404
    assert body["not_found"]


def test_wrong_method_cannot_bypass_a_registered_route_gate():
    (status, body), _ = api_request(scope("platform", None),
                                    "/api/claim/activities", "DELETE")
    assert status == 404
    assert body["not_found"]


def test_every_literal_api_dispatch_path_has_an_explicit_route_policy():
    source = Path(web.__file__).with_name("app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in tree.body
                   if isinstance(node, ast.ClassDef) and node.name == "Handler")
    api = next(node for node in handler.body
               if isinstance(node, ast.FunctionDef) and node.name == "_api_unlocked")
    paths = set()
    for node in ast.walk(api):
        if not isinstance(node, ast.Compare):
            continue
        for value in [node.left] + node.comparators:
            if isinstance(value, ast.Constant):
                values = (value.value,)
            elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                values = tuple(item.value for item in value.elts
                               if isinstance(item, ast.Constant))
            else:
                continue
            paths.update(value for value in values
                         if isinstance(value, str) and value.startswith("/api/"))

    registered = {path for _, path in registry.route_perms()}
    registered.update(path for _, path in web.FOOT_ROUTE_RULES)
    registered.update(path for _, path in web.ENTRY_ROUTE_RULES)
    registered.update(path for _, path in web.PAYLOAD_ROUTE_RULES)
    registered.update(path for _, path in web.DEFERRED_ROUTE_RULES)

    assert paths <= registered, "missing policy for: %s" % sorted(paths - registered)


def test_non_business_system_route_keeps_existing_behavior():
    (status, _), _ = api_request(scope("platform", None), "/api/status")
    assert status == 200


def test_sales_rewrite_rechecks_its_registered_modify_permission():
    sc = scope("store", {"甲店"})
    with mock.patch.object(web, "require", return_value={"forbidden": True}) as require:
        denied = web.registered_route_guard(sc, "POST", "/api/sales-rewrite")

    assert denied == {"forbidden": True}
    require.assert_called_once_with(sc, "sales-settings", "modify", why="")


def test_attain_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features.sales import attain as attain_package

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.sales.attain.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))

    with mock.patch.object(attain_package, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.sales.attain.http": feature_http}):
        response, _ = api_request(scope(), "/api/attain", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_claim_routes_are_dispatched_to_business_http_module():
    import sys
    import types
    from src.features.tools import claim

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.tools.claim.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))

    with mock.patch.object(claim, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.tools.claim.http": feature_http}):
        response, _ = api_request(scope(), "/api/claim/pending", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_claim_http_route_inventory_matches_registered_claim_routes():
    from src.features.tools.claim import http as claim_http

    registered = {route for route in registry.route_perms()
                  if route[1].startswith("/api/claim/")}

    assert set(claim_http.ROUTES) == registered


@pytest.mark.parametrize(("prefix", "module_name"), [
    ("/api/film", "src.features.valueadd.film.http"),
    ("/api/benefit", "src.features.valueadd.benefit.http"),
    ("/api/attain", "src.features.sales.attain.http"),
    ("/api/plan", "src.features.plan.monthly.http"),
    ("/api/claim", "src.features.tools.claim.http"),
    ("/api/dist", "src.features.distribution.http"),
    ("/api/inventory", "src.features.inventory.http"),
    ("/api/pos", "src.features.compliance.pos.http"),
    ("/api/cashier", "src.features.cashier.http"),
])
def test_feature_http_inventory_matches_registered_routes(prefix, module_name):
    import importlib

    module = importlib.import_module(module_name)
    registered = {route for route in registry.route_perms()
                  if route[1] == prefix or route[1].startswith(prefix + "/")}

    assert set(module.ROUTES) == registered


def test_claim_submit_already_claimed_remains_http_success():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.claim_submit_online.return_value = {
        "ok": False, "already_claimed": True, "why": "已经领取过"}

    (status, body), _ = api_request(
        scope(), "/api/claim/submit", "POST", body={"sn": "ABC"}, app=app)

    assert status == 200
    assert body["already_claimed"] is True
    assert body["error"] is None


@pytest.mark.parametrize("sc", [
    scope("store", {"甲店"}),
    scope("manager", {"甲店"}),
    scope("platform", None),
])
def test_claim_external_submit_keeps_all_user_confirmed_roles(sc):
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.claim_submit_online.return_value = {"ok": True, "submitted": 1}

    (status, body), _ = api_request(
        sc, "/api/claim/submit", "POST",
        body={"sn": "SYNTHETIC-SN", "activity_id": "synthetic"}, app=app)

    assert status == 200
    assert body["ok"] is True
    app.claim_submit_online.assert_called_once()


def test_unverified_store_account_write_cannot_promote_store_to_manager(tmp_path):
    """用户名参与区长识别，因此未验证的账号保存不能改写授权身份。"""
    config_dir = tmp_path / "config"
    secrets = tmp_path / ".secrets"
    config_dir.mkdir()
    secrets.mkdir()
    (config_dir / "store-test.yaml").write_text(
        "erp_store_name: 甲店\nstore_code: SCN-A\n", encoding="utf-8")
    (config_dir / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n    tdoc_name: 甲店\n    huawei_code: SCN-A\n"
        "  - erp_name: 乙店\n    tdoc_name: 乙店\n    huawei_code: SCN-B\n",
        encoding="utf-8")
    (config_dir / "managers.yaml").write_text(
        "managers:\n"
        "  - name: 乙店区长\n    accounts: [SL-BOSS]\n    stores: [乙店]\n",
        encoding="utf-8")
    (secrets / "entry.json").write_text('{"kind":"erp"}', encoding="utf-8")
    app = web.App(tmp_path, "config/store-test.yaml")
    assert web.role_scope(app)["role"] == "store"

    handler = object.__new__(web.Handler)
    handler.app = app
    handler._json = lambda data, status=200: (status, data)
    handler._read_json = lambda: {"username": "SL-BOSS"}
    with mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True), \
         mock.patch.object(web, "setup_state", return_value={"ready": True}), \
         mock.patch.object(web.manager, "current", return_value=None), \
         mock.patch.object(web.timer, "current", return_value=None), \
         mock.patch.object(web.capture_job, "running", False):
        status, body = handler._api("POST", "/api/store-account", {})

    assert web.role_scope(app)["role"] == "store"
    assert status == 410, body
    assert body["ok"] is False


def test_monthly_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features.plan import monthly

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.plan.monthly.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))

    with mock.patch.object(monthly, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.plan.monthly.http": feature_http}):
        response, _ = api_request(scope(), "/api/plan", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_distribution_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features import distribution

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.distribution.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))

    with mock.patch.object(distribution, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.distribution.http": feature_http}):
        response, _ = api_request(scope("platform", None), "/api/dist/board", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_inventory_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features import inventory

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.inventory.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))

    with mock.patch.object(inventory, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.inventory.http": feature_http}):
        response, _ = api_request(scope(), "/api/inventory/ready", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_pos_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features.compliance import pos

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.compliance.pos.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))
    sc = scope("platform", None)
    sc["entry_kind"] = "platform"
    sc["pages"] = ["pos"]

    with mock.patch.object(pos, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.compliance.pos.http": feature_http}):
        response, _ = api_request(sc, "/api/pos", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()


def test_cashier_route_is_dispatched_to_business_http_module():
    import sys
    import types
    from src.features import cashier

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    feature_http = types.ModuleType("src.features.cashier.http")
    feature_http.handle = mock.Mock(return_value=({"delegated": True}, 209))
    sc = scope()
    sc["pages"] = ["cashier"]

    with mock.patch.object(cashier, "http", feature_http, create=True), \
         mock.patch.dict(sys.modules, {"src.features.cashier.http": feature_http}):
        response, _ = api_request(sc, "/api/cashier/entries", app=app)

    assert response == (209, {"delegated": True})
    feature_http.handle.assert_called_once()
    app.attain.assert_not_called()


def test_overview_report_summaries_are_limited_to_current_store_scope():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {"reports": [
        {"name": "mine.xlsx", "store": "甲店", "missing_sns": ["OWN-SN"]},
        {"name": "other.xlsx", "store": "乙店", "missing_sns": ["OTHER-SN"]},
        {"name": "legacy.xlsx", "missing_sns": ["UNKNOWN-SN"]},
    ]}

    sc = scope("store", {"甲店"})
    sc["needs_linglong"] = True
    (status, body), _ = api_request(sc, "/api/overview", app=app)

    assert status == 200
    assert [r["name"] for r in body["reports"]] == ["mine.xlsx"]


def test_overview_hides_machine_wide_data_state_details_from_limited_roles():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {
        "reports": [],
        "data_state": {
            "ok": False, "worst": "stale", "worst_label": "数据过期",
            "lines": ["✅ 云商销售：正常（数据到 2026-10-03，987654 行）",
                      "🕒 玲珑库存：过期（数据只到 2026-09-20）"],
            "fingerprint": "stale|erp-sales|stale|2026-09-20",
            "dismissed": False,
        },
    }

    status, body = api_request(scope("store", {"甲店"}), "/api/overview", app=app)[0]

    assert status == 200
    assert body["data_state"] == {
        "ok": False, "worst_label": "数据过期", "dismissed": False,
    }


def test_platform_overview_keeps_full_data_state_details():
    details = {
        "ok": False, "worst": "stale", "worst_label": "数据过期",
        "lines": ["✅ 云商销售：正常（数据到 2026-10-03，987654 行）"],
        "fingerprint": "stale|erp-sales|stale|2026-09-20", "dismissed": False,
    }
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {"reports": [], "data_state": details}

    status, body = api_request(scope("platform", None), "/api/overview", app=app)[0]

    assert status == 200
    assert body["data_state"] == details


def test_limited_roles_receive_redacted_boot_checks_on_both_api_paths():
    """Health details combine whole-database stats, run failures and local recipients."""
    sensitive = "BOOT-SENSITIVE-MARKER"
    boot = {
        "ok": False, "allow_start": True,
        "items": [{"group": "data", "group_label": "数据", "state": "failed",
                   "why": sensitive, "level": "warning",
                   "detail": {"rows": 987654, "db": "/private/out/cbg.db"}}],
        "blocking": [],
        "warnings": [{"group": "notify", "group_label": "推送", "state": "missing",
                       "why": "收件人：" + sensitive, "level": "warning",
                       "detail": {"recipients": [sensitive]}}],
        "todos": [],
    }
    manager = scope("manager", {"甲店"})

    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.boot_state.return_value = boot
    status, direct = api_request(manager, "/api/boot", app=app)[0]
    assert status == 200
    assert sensitive not in json.dumps(direct, ensure_ascii=False)
    assert direct["items"][0]["state"] == "failed"
    assert direct["warnings"][0]["state"] == "missing"

    app.overview.return_value = {"reports": [], "boot": boot}
    status, overview = api_request(manager, "/api/overview", app=app)[0]
    assert status == 200
    assert sensitive not in json.dumps(overview, ensure_ascii=False)
    assert overview["boot"]["items"][0]["state"] == "failed"


def test_public_boot_endpoint_redacts_diagnostics_even_for_platform_machine():
    marker = "PLATFORM-BOOT-DIAGNOSTIC"
    boot = {"items": [{"group": "data", "state": "failed", "why": marker,
                       "detail": {"rows": 987654}}], "blocking": [],
            "warnings": [], "todos": []}
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.boot_state.return_value = boot

    status, body = api_request(scope("platform", None), "/api/boot", app=app)[0]

    assert status == 200
    assert marker not in json.dumps(body, ensure_ascii=False)
    assert "detail" not in body["items"][0]


def test_platform_overview_keeps_full_boot_diagnostics():
    marker = "PLATFORM-BOOT-DIAGNOSTIC"
    boot = {"items": [{"group": "data", "state": "failed", "why": marker,
                       "detail": {"rows": 987654}}], "blocking": [],
            "warnings": [], "todos": []}
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {"reports": [], "boot": boot}

    status, body = api_request(scope("platform", None), "/api/overview", app=app)[0]

    assert status == 200
    assert marker in json.dumps(body, ensure_ascii=False)
    assert body["boot"]["items"][0]["detail"]["rows"] == 987654


def test_store_runlog_does_not_expose_company_wide_serial_details(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "run.log").write_text(
        "=== 2026-10-03 09:00:00 开始 ===\n"
        "★ AD 玲珑报了云商没报：1 台\n"
        "  串号 COMPANY-SENSITIVE-SN\n"
        "      门店       乙店\n"
        "exit=0\n", encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out)

    (status, body), _ = api_request(scope("store", {"甲店"}), "/api/runlog", app=app)

    assert status == 200
    assert body["exit"] == 0
    assert body["lines"] == []
    assert body["details_restricted"] is True


def test_platform_runlog_keeps_company_wide_serial_details(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "run.log").write_text("串号 COMPANY-SENSITIVE-SN\nexit=0\n", encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out)

    (status, body), _ = api_request(scope("platform", None), "/api/runlog", app=app)

    assert status == 200
    assert any("COMPANY-SENSITIVE-SN" in line for line in body["lines"])


def test_limited_run_poll_hides_raw_cross_store_process_output():
    raw = {
        "id": "job-private", "running": True, "exit_code": None,
        "lines": ["乙店 COMPANY-SENSITIVE-SN"], "line_count": 1, "since": 0,
        "elapsed": 1.2, "command": "python -m src.cli daily --steps pools",
        "what": "wake", "what_label": "内置定时器",
    }
    job = mock.Mock()
    job.snapshot.return_value = raw

    with mock.patch.object(web.manager, "get", return_value=job):
        (status, body), _ = api_request(
            scope("manager", {"甲店"}), "/api/run",
            query={"id": ["job-private"], "since": ["0"]})

    assert status == 200
    assert body["running"] is True
    assert body["lines"] == []
    assert body["line_count"] == 0
    assert body["command"] == ""
    assert body["details_restricted"] is True
    assert "乙店" not in repr(body)
    assert "COMPANY-SENSITIVE-SN" not in repr(body)


def test_platform_run_poll_keeps_full_process_output():
    raw = {
        "id": "job-private", "running": True, "exit_code": None,
        "lines": ["乙店 COMPANY-SENSITIVE-SN"], "line_count": 1, "since": 0,
        "elapsed": 1.2, "command": "python -m src.cli daily --steps pools",
        "what": "wake", "what_label": "内置定时器",
    }
    job = mock.Mock()
    job.snapshot.return_value = raw

    with mock.patch.object(web.manager, "get", return_value=job):
        (status, body), _ = api_request(
            scope("platform", None), "/api/run",
            query={"id": ["job-private"], "since": ["0"]})

    assert status == 200
    assert body["lines"] == ["乙店 COMPANY-SENSITIVE-SN"]
    assert "details_restricted" not in body


def test_limited_overview_hides_embedded_run_process_output():
    raw = {
        "id": "job-private", "running": False, "exit_code": 1,
        "lines": ["乙店 COMPANY-SENSITIVE-SN"], "line_count": 1, "since": 0,
        "elapsed": 1.2, "command": "python -m src.cli daily --steps pools",
        "what": "wake", "what_label": "内置定时器",
    }
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {"reports": [], "run": raw}

    (status, body), _ = api_request(
        scope("manager", {"甲店"}), "/api/overview", app=app)

    assert status == 200
    assert body["run"]["lines"] == []
    assert body["run"]["line_count"] == 0
    assert body["run"]["command"] == ""
    assert body["run"]["details_restricted"] is True
    assert "乙店" not in repr(body)
    assert "COMPANY-SENSITIVE-SN" not in repr(body)


def test_platform_overview_keeps_embedded_run_process_output():
    raw = {
        "id": "job-private", "running": False, "exit_code": 1,
        "lines": ["乙店 COMPANY-SENSITIVE-SN"], "line_count": 1, "since": 0,
        "elapsed": 1.2, "command": "python -m src.cli daily --steps pools",
        "what": "wake", "what_label": "内置定时器",
    }
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.overview.return_value = {"reports": [], "run": raw}

    (status, body), _ = api_request(
        scope("platform", None), "/api/overview", app=app)

    assert status == 200
    assert body["run"]["lines"] == ["乙店 COMPANY-SENSITIVE-SN"]
    assert "details_restricted" not in body["run"]


def test_claim_status_audit_identity_comes_from_server_scope_not_request_body():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.claim_status_set.return_value = {"ok": True}
    sc = scope("store", {"甲店"})
    sc.update({"who": "真实门店账号", "account": "store-user"})

    (status, body), _ = api_request(
        sc, "/api/claim/status", "POST",
        body={"key": "sn:OWN#activity", "status": "claimed",
              "by": "伪造的平台岗", "note": "人工确认"},
        app=app)

    assert status == 200
    assert body["ok"] is True
    kwargs = app.claim_status_set.call_args.kwargs
    assert kwargs["by"] == "真实门店账号"
    assert kwargs["by"] != "伪造的平台岗"


def test_manager_inbox_result_hides_out_of_scope_store_metadata(tmp_path):
    app = mock.Mock(root=tmp_path, config="config/store-test.yaml")
    incoming = {
        "ok": True, "skipped": "", "mails": 2, "ok_packages": 2,
        "skipped_packages": 1, "rows": 987654,
        "stores": ["SCN-A", "SCN-B"], "problems": ["SCN-B · private failure"],
        "splits": ["SCN-B 2026-W40（12 人）"],
    }

    def fake_run(_root, *, emit, **_kwargs):
        emit("收到 SCN-A 和 SCN-B，共 987654 行")
        return incoming

    with mock.patch("src.app.report_inbox.run", side_effect=fake_run):
        (status, body), _ = api_request(
            scope("manager", {"甲店"}), "/api/report/inbox", "POST", app=app)

    assert status == 200
    assert body["result"] == {"ok": True, "scope_restricted": True}
    assert body["log"] == ""


def test_manager_inbox_import_is_limited_to_unambiguous_authorized_store_codes(tmp_path):
    app = mock.Mock(root=tmp_path, config="config/store-test.yaml")
    rows = [
        {"erp_name": "甲店", "tdoc_name": "甲店短名", "huawei_code": "SCN-A"},
        {"erp_name": "乙店", "tdoc_name": "乙店短名", "huawei_code": "SCN-B"},
        {"erp_name": "丙店", "tdoc_name": "丙店短名", "huawei_code": "SCN-SHARED"},
        {"erp_name": "丁店", "tdoc_name": "丁店短名", "huawei_code": "SCN-SHARED"},
    ]
    manager = scope("manager", {"甲店", "甲店短名", "丙店", "丙店短名"})
    captured = {}

    def fake_run(_root, *, emit, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "stores": ["SCN-A", "SCN-SHARED"]}

    with mock.patch("src.web.config_io.stores_table", return_value=rows), \
         mock.patch("src.app.report_inbox.run", side_effect=fake_run):
        (status, body), _ = api_request(manager, "/api/report/inbox", "POST", app=app)

    assert status == 200
    assert captured["allowed_store_codes"] == frozenset({"SCN-A"})
    assert body["result"] == {"ok": True, "scope_restricted": True}


def test_manager_inbox_fails_closed_when_store_roster_cannot_be_read(tmp_path):
    app = mock.Mock(root=tmp_path, config="config/store-test.yaml")
    manager = scope("manager", {"甲店"})
    run = mock.Mock(return_value={"ok": True})

    with mock.patch("src.web.config_io.stores_table", side_effect=OSError("unavailable")), \
         mock.patch("src.app.report_inbox.run", run):
        (status, _), _ = api_request(manager, "/api/report/inbox", "POST", app=app)

    assert status == 200
    assert run.call_args.kwargs["allowed_store_codes"] == frozenset()


def test_platform_inbox_result_keeps_full_diagnostics(tmp_path):
    app = mock.Mock(root=tmp_path, config="config/store-test.yaml")
    incoming = {"ok": True, "stores": ["SCN-A"], "rows": 10}

    with mock.patch("src.app.report_inbox.run", return_value=incoming):
        (status, body), _ = api_request(
            scope("platform", None), "/api/report/inbox", "POST", app=app)

    assert status == 200
    assert body["result"] == incoming


def test_platform_inbox_import_keeps_explicit_global_scope(tmp_path):
    app = mock.Mock(root=tmp_path, config="config/store-test.yaml")
    run = mock.Mock(return_value={"ok": True})

    with mock.patch("src.app.report_inbox.run", run):
        (status, _), _ = api_request(
            scope("platform", None), "/api/report/inbox", "POST", app=app)

    assert status == 200
    assert run.call_args.kwargs["allowed_store_codes"] is None


def test_report_store_card_rejects_inbox_store_name_conflicting_with_authorized_code(tmp_path):
    from src.app import report_inbox

    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-A\n"
        "  - erp_name: 乙店\n"
        "    tdoc_name: 乙店短名\n"
        "    huawei_code: SCN-B\n", encoding="utf-8")
    app = object.__new__(web.App)
    app.root = tmp_path
    conflicting = [{
        "store_code": "SCN-A", "store_name": "乙店", "known": True,
        "rows_total": 987654, "tables": {"staff": {"rows": 88}},
    }]
    sc = scope("manager", {"甲店", "甲店短名"})

    with mock.patch.object(report_inbox, "cards", return_value=conflicting), \
         mock.patch.object(report_inbox, "store_days", return_value=[
             {"report_date": "2026-10-02", "rows_total": 987654}]) as store_days:
        (list_status, listing), _ = api_request(sc, "/api/report/stores", app=app)
        (detail_status, detail), _ = api_request(
            sc, "/api/report/store", query={"code": ["SCN-A"]}, app=app)

    assert list_status == 200
    card = listing["stores"][0]
    assert card["known"] is False
    assert "rows_total" not in card and "tables" not in card
    assert "乙店" not in card["store_name"]
    assert detail_status == 403
    assert detail["forbidden"]
    store_days.assert_not_called()


def test_report_card_code_shared_by_authorized_and_other_store_is_fail_closed(tmp_path):
    from src.app import report_inbox

    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-SHARED\n"
        "  - erp_name: 乙店\n"
        "    tdoc_name: 乙店短名\n"
        "    huawei_code: SCN-SHARED\n", encoding="utf-8")
    app = object.__new__(web.App)
    app.root = tmp_path
    sc = scope("manager", {"甲店", "甲店短名"})

    with mock.patch.object(report_inbox, "cards", return_value=[{
            "store_code": "SCN-SHARED", "store_name": "乙店", "known": True,
            "rows_total": 987654}]), \
         mock.patch.object(report_inbox, "store_days", return_value=[
             {"report_date": "2026-10-02", "rows_total": 987654}]) as store_days:
        (list_status, listing), _ = api_request(sc, "/api/report/stores", app=app)
        (detail_status, detail), _ = api_request(
            sc, "/api/report/store", query={"code": ["SCN-SHARED"]}, app=app)

    assert list_status == 200
    assert listing["stores"][0]["known"] is False
    assert "rows_total" not in listing["stores"][0]
    assert detail_status == 403
    assert detail["forbidden"]
    store_days.assert_not_called()


def test_report_store_skips_hide_unattributed_and_other_store_diagnostics(tmp_path):
    from src.app import report_inbox

    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-A\n"
        "  - erp_name: 乙店\n"
        "    tdoc_name: 乙店短名\n"
        "    huawei_code: SCN-B\n", encoding="utf-8")
    app = object.__new__(web.App)
    app.root = tmp_path
    sc = scope("manager", {"甲店", "甲店短名"})
    raw_skips = [
        {"store_code": "SCN-A", "at": "2026-10-03 10:00:00",
         "file": "甲店包.db", "why": "bad column"},
        {"store_code": "SCN-B", "at": "2026-10-03 10:01:00",
         "file": "乙店-PRIVATE.db", "why": "乙店内部错误 PRIVATE-987654"},
        {"store_code": "", "at": "2026-10-03 10:02:00",
         "file": "UNKNOWN-PRIVATE.db", "why": "未知门店 PRIVATE-123456"},
    ]

    with mock.patch.object(report_inbox, "cards", return_value=[
            {"store_code": "SCN-A", "store_name": "甲店", "known": True}]), \
         mock.patch.object(report_inbox, "skips", return_value=raw_skips):
        listing = app._stores_cards(sc)

    skips = listing["inbox"]["skips"]
    assert len(skips) == 1
    assert skips[0]["at"] == "2026-10-03 10:00:00"
    assert "SCN-B" not in repr(skips)
    assert "乙店" not in repr(skips)
    assert "PRIVATE" not in repr(skips)
    assert "甲店包.db" not in repr(skips)


def test_platform_store_skips_keep_full_diagnostics(tmp_path):
    from src.app import report_inbox

    app = object.__new__(web.App)
    app.root = tmp_path
    raw_skips = [{"store_code": "SCN-B", "file": "乙店-PRIVATE.db",
                  "why": "乙店内部错误 PRIVATE-987654"}]

    with mock.patch("src.web.config_io.stores_table", return_value=[]), \
         mock.patch.object(report_inbox, "cards", return_value=[]), \
         mock.patch.object(report_inbox, "skips", return_value=raw_skips):
        listing = app._stores_cards(scope("platform", None))

    assert listing["inbox"]["skips"] == raw_skips


def test_report_sidecar_rejects_store_name_and_code_conflict(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-A\n"
        "  - erp_name: 乙店\n"
        "    tdoc_name: 乙店短名\n"
        "    huawei_code: SCN-B\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    target = out / "cross-store.xlsx"
    target.write_bytes(b"test workbook placeholder")
    target.with_suffix(".json").write_text(
        json.dumps({"store": "甲店", "store_code": "SCN-B"}), encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out)
    sc = scope("manager", {"甲店", "甲店短名"})
    sc["pages"] = ["pools"]

    with mock.patch.object(web, "load_report", return_value={"sheets": {}}):
        (status, body), _ = api_request(sc, "/api/report",
                                        query={"name": [target.name]}, app=app)

    assert status == 403 and body["forbidden"]


def test_report_sidecar_with_authorized_store_name_and_code_remains_readable(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-A\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    out.mkdir()
    target = out / "own-store.xlsx"
    target.write_bytes(b"test workbook placeholder")
    target.with_suffix(".json").write_text(
        json.dumps({"store": "甲店", "store_code": "SCN-A"}), encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out)
    sc = scope("manager", {"甲店", "甲店短名"})
    sc["pages"] = ["pools"]

    with mock.patch.object(web, "load_report", return_value={"sheets": {}}):
        (status, body), _ = api_request(sc, "/api/report",
                                        query={"name": [target.name]}, app=app)

    assert status == 200 and body["name"] == target.name


@pytest.mark.parametrize("role", ["store", "manager"])
def test_attain_history_index_recomputes_store_count_and_average_for_scope(tmp_path, role):
    import json
    from src.features.sales.attain import attain

    archive = attain.archive_dir(tmp_path)
    archive.mkdir(parents=True)
    payload = {
        "exists": True, "period": "2026-W39", "start": "2026-09-21",
        "end": "2026-09-27", "locked_at": "2026-09-28 08:00:00",
        "rows": [
            {"store": "甲店", "erp_name": "甲店", "total": 0.2},
            {"store": "乙店", "erp_name": "乙店", "total": 0.8},
        ],
    }
    attain.archive_path(tmp_path, "2026-W39").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    app = object.__new__(web.App)
    app.root = tmp_path
    sc = scope(role, {"甲店"})
    sc["needs_linglong"] = True

    (status, body), _ = api_request(sc, "/api/attain/history", app=app)

    assert status == 200
    assert body["items"][0]["stores"] == 1
    assert body["items"][0]["avg"] == 0.2


def test_attain_history_rejects_period_path_traversal(tmp_path):
    import json

    # Synthetic sentinel outside attain-history; never create or inspect real secrets.
    (tmp_path / "out" / "attain-history").mkdir(parents=True)
    (tmp_path / "outside.json").write_text(json.dumps({
        "exists": True, "period": "outside", "rows": [
            {"store": "甲店", "total": 1.0, "sensitive": "sentinel"},
        ],
    }), encoding="utf-8")
    app = object.__new__(web.App)
    app.root = tmp_path

    (status, body), _ = api_request(scope("platform", None),
                                    "/api/attain/history",
                                    query={"period": ["../../outside"]}, app=app)

    assert status == 200
    assert not body["exists"]
    assert body["rows"] == []
    assert all(row.get("sensitive") != "sentinel" for row in body["rows"])


def test_report_detail_rejects_other_store_and_unknown_owner(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    for name in ("other.xlsx", "legacy.xlsx"):
        (out_dir / name).write_bytes(b"workbook")
    (out_dir / "other.json").write_text('{"store":"乙店"}', encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)

    with mock.patch.object(web, "load_report",
                           side_effect=AssertionError("denied report must not be loaded")):
        sc = scope("store", {"甲店"})
        sc["needs_linglong"] = True
        other, _ = api_request(sc, "/api/report",
                               query={"name": ["other.xlsx"]}, app=app)
        legacy, _ = api_request(sc, "/api/report",
                                query={"name": ["legacy.xlsx"]}, app=app)

    assert other[0] == 403 and other[1]["forbidden"]
    assert legacy[0] == 403 and legacy[1]["forbidden"]


def test_report_detail_allows_current_store_owner(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "mine.xlsx").write_bytes(b"workbook")
    (out_dir / "mine.json").write_text('{"store":"甲店"}', encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)
    sc = scope("store", {"甲店"})
    sc["needs_linglong"] = True

    with mock.patch.object(web, "load_report", return_value={
            "summary": {"store": "甲店"}, "sheets": {}}):
        response, _ = api_request(sc, "/api/report",
                                  query={"name": ["mine.xlsx"]}, app=app)

    assert response[0] == 200
    assert response[1]["summary"]["store"] == "甲店"


def test_report_detail_rejects_corrupt_owner_metadata_before_reading_workbook(tmp_path):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "unknown.xlsx"
    target.write_bytes(b"workbook")
    target.with_suffix(".json").write_text("{broken", encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)
    sc = scope("store", {"甲店"})
    sc["needs_linglong"] = True

    with mock.patch.object(web, "load_report",
                           side_effect=AssertionError("must reject before loading xlsx")):
        response, _ = api_request(sc, "/api/report",
                                  query={"name": [target.name]}, app=app)

    assert response[0] == 403 and response[1]["forbidden"]


def test_platform_can_read_report_when_sidecar_is_corrupt(tmp_path):
    from src.reconcile import ReconcileResult
    from src.report import write_report

    out_dir = tmp_path / "out"
    target = write_report(out_dir, ReconcileResult(),
                          {"门店": "甲店", "目标日": "2026-10-03"},
                          "2026-10-03", "甲店")
    target.with_suffix(".json").write_text("{broken", encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)

    response, _ = api_request(scope("platform", None), "/api/report",
                              query={"name": [target.name]}, app=app)

    assert response[0] == 200
    assert response[1]["summary"]["_derived"]


def test_report_download_and_delete_reject_other_store(tmp_path):
    import io
    import json

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = out_dir / "other.xlsx"
    target.write_bytes(b"workbook")
    target.with_suffix(".json").write_text(
        json.dumps({"store": "乙店"}), encoding="utf-8")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)
    handler = object.__new__(web.Handler)
    handler.app = app
    handler._json = lambda data, status=200: (status, data)
    handler.send_response = mock.Mock()
    handler.send_header = mock.Mock()
    handler.end_headers = mock.Mock()
    handler.wfile = io.BytesIO()

    sc = scope("store", {"甲店"})
    sc["needs_linglong"] = True
    with mock.patch.object(web, "role_scope", return_value=sc), \
         mock.patch.object(web, "setup_state", return_value={"ready": True}), \
         mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True):
        download = handler._api_unlocked(
            "GET", "/api/report/download", {"name": [target.name]})
        delete = handler._api_unlocked(
            "DELETE", "/api/report", {"name": [target.name]})

    assert download[0] == 403 and download[1]["forbidden"]
    assert delete[0] == 403 and delete[1]["forbidden"]
    assert target.exists(), "越权删除不能碰其他门店的报告"


def test_report_detail_cannot_escape_to_a_sibling_directory(tmp_path):
    out_dir = tmp_path / "out"
    sibling = tmp_path / "out-archive"
    out_dir.mkdir()
    sibling.mkdir()
    (sibling / "own.xlsx").write_bytes(b"workbook")
    app = mock.Mock(root=tmp_path, out_dir=out_dir)
    sc = scope("platform", None)
    sc["entry_kind"] = "platform"

    with mock.patch.object(web, "load_report", return_value={
            "summary": {"store": "甲店"}, "sheets": {}}):
        response, _ = api_request(sc, "/api/report",
                                  query={"name": ["../out-archive/own.xlsx"]},
                                  app=app)

    assert response[0] == 404


def test_staff_routes_have_explicit_foot_policy_and_reject_entry_mismatch():
    rules = web.FOOT_ROUTE_RULES
    assert rules[("GET", "/api/staff")]["op"] == "view"
    assert rules[("POST", "/api/staff")]["op"] == "modify"
    assert rules[("POST", "/api/staff")]["data"] == "store"

    sc = scope("platform", None)
    sc["entry_kind"] = "erp"
    denied = web.registered_route_guard(sc, "GET", "/api/staff")
    assert denied and denied["forbidden"]


def test_staff_wrong_method_cannot_bypass_foot_route_policy():
    denied = web.registered_route_guard(scope(), "DELETE", "/api/staff")
    assert denied and denied["not_found"]


def test_linglong_session_system_routes_follow_visible_page_permissions():
    root = Path("/temporary-route-policy-test")
    partner = scope("store")
    partner["needs_linglong"] = False
    partner = web._with_pages(partner, root, [])
    assert "linglong" not in partner["pages"]
    for method, path in (("GET", "/api/session"),
                         ("POST", "/api/session"),
                         ("DELETE", "/api/session"),
                         ("POST", "/api/session/ping"),
                         ("GET", "/api/session/auto"),
                         ("POST", "/api/session/auto"),
                         ("GET", "/api/session/auto/browser"),
                         ("GET", "/api/hwlogin"),
                         ("PUT", "/api/hwlogin"),
                         ("POST", "/api/hwlogin")):
        denied = web.registered_route_guard(partner, method, path)
        assert denied and denied["forbidden"], (method, path)

    experience = scope("store")
    experience["needs_linglong"] = True
    experience = web._with_pages(experience, root, [])
    assert "linglong" in experience["pages"]
    assert web.registered_route_guard(experience, "POST", "/api/session/auto") is None


def test_lifehall_code_only_can_use_its_linglong_authorization_routes():
    sc = web._with_pages({"role": "store", "stores": set(),
                          "entry_kind": "lifehall", "runtime_lifehall": True,
                          "store_code": "SCN1"}, Path("/temporary-route-policy-test"), [])
    assert "linglong" in sc["pages"]
    assert web.registered_route_guard(sc, "POST", "/api/session/auto") is None


def test_lifehall_store_code_route_is_open_only_during_selected_lifehall_setup():
    first_visit = {"role": "store", "stores": set(), "entry_kind": "lifehall",
                   "runtime_lifehall": True, "store_code": ""}

    assert web.registered_route_guard(first_visit, "PUT", "/api/session/store-code",
                                      setup_ready=False) is None
    denied_after_setup = web.registered_route_guard(
        first_visit, "PUT", "/api/session/store-code", setup_ready=True)
    assert denied_after_setup and denied_after_setup["forbidden"]
    wrong_entry = dict(first_visit, entry_kind="erp", runtime_lifehall=False)
    denied_wrong_entry = web.registered_route_guard(
        wrong_entry, "PUT", "/api/session/store-code", setup_ready=False)
    assert denied_wrong_entry and denied_wrong_entry["forbidden"]
    wrong_method = web.registered_route_guard(
        first_visit, "DELETE", "/api/session/store-code", setup_ready=False)
    assert wrong_method and wrong_method["not_found"]


def test_lifehall_store_code_route_requires_the_saved_single_store_grant_after_setup():
    root = Path("/temporary-route-policy-test")
    lifehall = web._with_pages({"role": "store", "stores": set(),
                                "entry_kind": "lifehall", "runtime_lifehall": True,
                                "store_code": "SCN1"}, root, [])

    assert "general" in lifehall["pages"]
    assert web.registered_route_guard(lifehall, "PUT", "/api/session/store-code",
                                      setup_ready=True, root=root) is None


def test_entry_selection_routes_are_explicit_public_login_routes():
    empty_scope = {"role": "", "stores": set(), "entry_kind": ""}
    for method in ("GET", "POST", "DELETE"):
        assert (method, "/api/entry") in web.ENTRY_ROUTE_RULES
        assert web.ENTRY_ROUTE_RULES[(method, "/api/entry")]["public"] is True
        assert web.registered_route_guard(empty_scope, method, "/api/entry",
                                          setup_ready=False) is None
    for method, path in (("GET", "/api/health"), ("GET", "/api/boot"),
                         ("GET", "/api/setup"), ("POST", "/api/setup/preview")):
        assert web.ENTRY_ROUTE_RULES[(method, path)]["public"] is True


def test_every_literal_api_handler_route_has_an_explicit_policy_classification():
    tree = ast.parse(Path(web.__file__).with_name("app.py").read_text(encoding="utf-8"))
    handled = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        paths, methods = set(), set()
        for compare in ast.walk(node.test):
            if not isinstance(compare, ast.Compare) or not isinstance(compare.left, ast.Name):
                continue
            if compare.left.id == "path":
                for value in compare.comparators:
                    if (isinstance(value, ast.Constant) and isinstance(value.value, str)
                            and value.value.startswith("/api/")):
                        paths.add(value.value)
                    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                        paths.update(item.value for item in value.elts
                                     if isinstance(item, ast.Constant)
                                     and isinstance(item.value, str)
                                     and item.value.startswith("/api/"))
            elif compare.left.id == "method":
                for value in compare.comparators:
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        methods.add(value.value)
                    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
                        methods.update(item.value for item in value.elts
                                       if isinstance(item, ast.Constant)
                                       and isinstance(item.value, str))
        handled.update((method, path) for method in methods for path in paths)

    classified = (set(registry.route_perms()) | set(web.FOOT_ROUTE_RULES)
                  | set(web.ENTRY_ROUTE_RULES) | set(web.DEFERRED_ROUTE_RULES)
                  | set(web.PAYLOAD_ROUTE_RULES))
    assert handled <= classified, sorted(handled - classified)
    assert registry.validate() == []


def test_route_policy_classes_are_disjoint():
    """同一路由不能同时被不同门禁类别接管，避免优先级掩盖另一份声明。"""
    classes = {
        "business": set(registry.route_perms()),
        "system": set(web.FOOT_ROUTE_RULES),
        "entry": set(web.ENTRY_ROUTE_RULES),
        "deferred": set(web.DEFERRED_ROUTE_RULES),
        "payload": set(web.PAYLOAD_ROUTE_RULES),
    }
    names = list(classes)
    for i, name in enumerate(names):
        for other in names[i + 1:]:
            assert not classes[name] & classes[other], (name, other,
                                                        sorted(classes[name] & classes[other]))


def test_system_and_entry_route_declarations_are_complete():
    for route, rule in web.FOOT_ROUTE_RULES.items():
        assert rule.get("page") in web.PAGE_RULES, route
        assert rule.get("op") in registry.OPS, route
        assert rule.get("op") in rule.get("ops", {}), route
        assert rule.get("audience"), route
        assert rule.get("data") in registry.DATA_SCOPES, route
    for route, rule in web.ENTRY_ROUTE_RULES.items():
        if rule.get("public"):
            continue
        assert rule.get("page") in web.PAGE_RULES, route
        assert rule.get("roles"), route
        assert rule.get("audience"), route


def test_erp_credential_routes_require_an_erp_or_platform_entry():
    root = Path("/temporary-route-policy-test")
    erp_setup = {"role": "store", "stores": set(), "entry_kind": "erp",
                 "runtime_lifehall": False, "pages": [], "ops": {}}
    assert web.registered_route_guard(erp_setup, "GET", "/api/store-account",
                                      setup_ready=False, root=root) is None
    lifehall = web._with_pages({"role": "store", "stores": set(),
                                "entry_kind": "lifehall", "runtime_lifehall": True,
                                "store_code": "SCN1"}, root, [])
    denied = web.registered_route_guard(lifehall, "POST", "/api/store-account/logout",
                                        setup_ready=True, root=root)
    assert denied and denied["forbidden"]


def test_erp_credential_api_never_returns_account_token_or_local_paths():
    """旧的诊断接口没有前端调用方，也不能泄露共享云商账号和令牌片段。"""
    sensitive = {
        "env_file": "/private/.secrets/erp.env",
        "exists": True,
        "username": "shared-company-account",
        "company": "COMPANY-42",
        "has_password": True,
        "has_token": True,
        "token": "SECRET123…7890",
        "used_from": "/private/central.env",
        "builtin": False,
    }
    sc = scope("store", {"甲店"})
    sc["pages"] = ["account"]
    app = mock.Mock(root=Path("/temporary-credential-route-test"))
    app.erp_env_file.return_value = "/private/.secrets/erp.env"

    with mock.patch.object(web, "describe_credentials", return_value=sensitive):
        status, body = api_request(sc, "/api/erp", app=app)[0]

    assert status == 200
    assert {key: body[key] for key in ("exists", "has_password", "has_token", "builtin")} == {
        "exists": True, "has_password": True, "has_token": True, "builtin": False,
    }
    assert not ({"username", "company", "token", "env_file", "used_from"} & set(body))


def test_erp_credential_save_response_does_not_echo_secret_metadata():
    sensitive = {
        "env_file": "/private/.secrets/erp.env", "exists": True,
        "username": "shared-company-account", "company": "COMPANY-42",
        "has_password": True, "has_token": True, "token": "SECRET123…7890",
        "used_from": "/private/central.env", "builtin": False,
    }
    sc = scope("store", {"甲店"})
    sc["pages"] = ["account"]
    app = mock.Mock(root=Path("/temporary-credential-route-test"))
    app.erp_env_file.return_value = "/private/.secrets/erp.env"
    app.config = "config/store-test.yaml"

    with mock.patch.object(web, "describe_credentials", return_value=sensitive), \
         mock.patch.object(web, "load_credentials", return_value={"ERP_USERNAME": "old"}), \
         mock.patch.object(web, "save_credentials"):
        status, body = api_request(
            sc, "/api/erp", "POST", body={"username": "new"}, app=app)[0]

    assert status == 200
    assert body["ok"] is True
    assert body["has_token"] is True
    assert not ({"username", "company", "token", "env_file", "used_from"} & set(body))


def test_erp_login_response_keeps_success_status_without_echoing_token_or_path():
    sensitive = {
        "env_file": "/private/.secrets/erp.env", "exists": True,
        "username": "shared-company-account", "company": "COMPANY-42",
        "has_password": True, "has_token": True, "token": "SECRET123…7890",
        "used_from": "/private/central.env", "builtin": False,
    }
    sc = scope("store", {"甲店"})
    sc["pages"] = ["account"]
    app = mock.Mock(root=Path("/temporary-credential-route-test"))
    app.erp_env_file.return_value = "/private/.secrets/erp.env"
    client = mock.Mock()
    client.login_and_verify.return_value = {"token": "NEW-SECRET-TOKEN", "who": "测试登录人"}

    with mock.patch.object(web, "load_credentials", return_value={"username": "old"}), \
         mock.patch.object(web, "ErpClient", return_value=client), \
         mock.patch.object(web, "save_credentials"), \
         mock.patch.object(web, "describe_credentials", return_value=sensitive), \
         mock.patch.object(web.pending_login, "reset"):
        status, body = api_request(
            sc, "/api/erp/login", "POST", body={"username": "new", "password": "test"},
            app=app)[0]

    assert status == 200
    assert body["ok"] is True and body["saved"] is True
    assert body["who"] == "测试登录人"
    assert body["has_token"] is True
    assert not ({"username", "company", "token", "env_file", "used_from"} & set(body))


def test_machine_page_routes_are_registered_and_hidden_pages_deny_direct_calls():
    root = Path("/temporary-route-policy-test")
    lifehall = web._with_pages({"role": "store", "stores": set(),
                                "entry_kind": "lifehall", "runtime_lifehall": True,
                                "store_code": "SCN1"}, root, [])

    assert "general" in lifehall["pages"]
    assert web.registered_route_guard(lifehall, "GET", "/api/config",
                                      setup_ready=True, root=root) is None
    assert "scheduler" not in lifehall["pages"]
    denied = web.registered_route_guard(lifehall, "GET", "/api/timer",
                                        setup_ready=True, root=root)
    assert denied and denied["forbidden"]


def test_linglong_setup_routes_are_open_before_setup_but_still_check_page_afterward():
    first_visit = {"role": "store", "stores": set(), "entry_kind": "erp",
                   "runtime_lifehall": False, "needs_linglong": False,
                   "pages": [], "ops": {}}
    assert web.registered_route_guard(first_visit, "GET", "/api/session/auto",
                                      setup_ready=False) is None
    hidden_after_setup = web.registered_route_guard(
        first_visit, "GET", "/api/session/auto", setup_ready=True)
    assert hidden_after_setup and hidden_after_setup["forbidden"]


def test_lifehall_cannot_use_linglong_setup_routes_before_saving_its_store_code():
    first_visit = {"role": "store", "stores": set(), "entry_kind": "lifehall",
                   "runtime_lifehall": True, "store_code": "", "pages": [], "ops": {}}

    denied = web.registered_route_guard(first_visit, "GET", "/api/session",
                                        setup_ready=False)

    assert denied and denied["forbidden"]


def test_export_download_has_a_deferred_explicit_route_policy():
    good = web.registered_route_guard(scope("manager", {"甲店"}),
                                      "GET", "/api/export/download")
    assert good is None
    denied = web.registered_route_guard({"role": "manager", "stores": None},
                                        "GET", "/api/export/download")
    assert denied and denied["forbidden"]
    wrong_method = web.registered_route_guard(scope("manager", {"甲店"}),
                                              "POST", "/api/export/download")
    assert wrong_method and wrong_method["not_found"]


def test_every_nonpublic_registered_route_rejects_an_empty_identity():
    """新增精确路由不能只靠菜单隐藏；没有有效身份时全部默认拒绝。"""
    public = {key for key, rule in web.ENTRY_ROUTE_RULES.items()
              if rule.get("public")}
    deferred = set(web.DEFERRED_ROUTE_RULES)
    protected = (set(registry.route_perms())
                 | set(web.FOOT_ROUTE_RULES)
                 | (set(web.ENTRY_ROUTE_RULES) - public)
                 | deferred)
    assert protected

    for method, path in sorted(protected):
        denied = web.registered_route_guard({}, method, path, setup_ready=True)
        assert denied and denied.get("forbidden"), (method, path, denied)


def test_registered_business_and_system_routes_match_role_audience_matrix(tmp_path):
    """逐条执行路由声明：身份、进入方式与操作角色有任一不匹配都要拒绝。"""
    full_root = tmp_path / "full"
    full_root.mkdir()
    lifehall_root = tmp_path / "lifehall"
    secrets_dir = lifehall_root / ".secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "entry.json").write_text('{"kind":"lifehall"}', encoding="utf-8")
    routes = {}
    for (method, path), (page, op) in registry.route_perms().items():
        rule = web.PERM_RULES[page]
        routes[(method, path)] = (page, rule["ops"].get(op, ()), rule["audience"])
    for (method, path), rule in web.FOOT_ROUTE_RULES.items():
        routes[(method, path)] = (
            rule["page"], rule["ops"].get(rule["op"], ()), rule["audience"])

    role_entries = {
        "store": {"erp", "lifehall"},
        "manager": {"erp"},
        "platform": {"platform"},
    }
    checked = 0
    for (method, path), (page, roles, audiences) in sorted(routes.items()):
        for role in ("store", "manager", "platform"):
            for audience in registry.AUDIENCES:
                lifehall = audience == "lifehall"
                root = lifehall_root if lifehall else full_root
                scope_value = {
                    "role": role,
                    "stores": None if role == "platform" else {"甲店"},
                    "entry_kind": audience,
                    "runtime_lifehall": lifehall,
                    "store_code": "SCN123" if lifehall else "",
                    "pages": list(web.PAGE_RULES),
                    "who": "测试", "label": "测试身份",
                }
                expected = (
                    role in roles
                    and audience in audiences
                    and audience in role_entries[role]
                    and (not lifehall or page in web.runtime.LIFEHALL_PAGES)
                    and (not lifehall or (
                        web.runtime.api_available(path, root)
                        and not web.lifehall_gone(path, root)))
                )
                api_open = (not lifehall or (
                    web.runtime.api_available(path, root)
                    and not web.lifehall_gone(path, root)))
                denied = (web.registered_route_guard(
                    scope_value, method, path, setup_ready=True, root=root)
                    if api_open else {"forbidden": True, "edition_blocked": True})
                assert (denied is None) == expected, (
                    method, path, page, role, audience, expected, denied)
                if not expected:
                    assert denied and denied.get("forbidden"), (
                        method, path, role, audience, denied)
                checked += 1
    assert checked == len(routes) * 3 * len(registry.AUDIENCES)


@pytest.mark.parametrize(("method", "path", "body", "query"), [
    ("POST", "/api/schedule/run", {"name": "\\Microsoft\\Windows\\Update"}, {}),
    ("DELETE", "/api/schedule", {}, {"name": ["\\Microsoft\\Windows\\Update"]}),
])
def test_schedule_api_cannot_run_or_delete_unregistered_system_tasks(
        method, path, body, query):
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    schedule_status = {"tasks": [{"name": "CBG报量对账-21点00",
                                   "full_name": "\\CBG报量对账-21点00"}]}
    with mock.patch.object(web.schedule, "status", return_value=schedule_status), \
            mock.patch.object(web.schedule, "run_now") as run_now, \
            mock.patch.object(web.schedule, "remove") as remove:
        (status, payload), _ = api_request(
            scope("store", {"甲店"}), path, method, body=body, query=query, app=app)

    assert status == 404
    assert payload["error"] == "只能操作本程序已登记的定时任务"
    assert run_now.call_count == 0
    assert remove.call_count == 0


@pytest.mark.parametrize(("method", "path", "body", "query"), [
    ("POST", "/api/schedule/run", {"name": "\\CBG报量对账-21点00"}, {}),
    ("DELETE", "/api/schedule", {}, {"name": ["\\CBG报量对账-21点00"]}),
])
def test_schedule_api_still_operates_on_listed_tasks(method, path, body, query):
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    task = {"name": "CBG报量对账-21点00", "full_name": "\\CBG报量对账-21点00"}
    with mock.patch.object(web.schedule, "status", return_value={"tasks": [task]}), \
            mock.patch.object(web.schedule, "run_now", return_value={"ok": True}) as run_now, \
            mock.patch.object(web.schedule, "remove", return_value={"ok": True}) as remove:
        (status, payload), _ = api_request(
            scope("store", {"甲店"}), path, method, body=body, query=query, app=app)

    assert status == 200
    assert payload["ok"] is True
    if method == "POST":
        run_now.assert_called_once_with("\\CBG报量对账-21点00")
        remove.assert_not_called()
    else:
        remove.assert_called_once_with("\\CBG报量对账-21点00", app.root)
        run_now.assert_not_called()


def test_elevated_schedule_remove_rejects_unlisted_task_before_uac():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    listed = {"tasks": [{"name": "CBG报量对账-21点00",
                          "full_name": "\\CBG报量对账-21点00"}]}
    with mock.patch.object(web.schedule, "status", return_value=listed), \
            mock.patch.object(web.elevate, "is_admin", return_value=False), \
            mock.patch.object(web.elevate, "run_elevated") as run_elevated:
        (status, payload), _ = api_request(
            scope("store", {"甲店"}), "/api/elevate", "POST",
            body={"what": "schedule-remove",
                  "name": "\\Microsoft\\Windows\\Update"}, app=app)

    assert status == 404
    assert payload["error"] == "只能操作本程序已登记的定时任务"
    run_elevated.assert_not_called()


def test_unverified_unreadable_schedule_task_cannot_be_run_or_removed():
    root = Path("/temporary-route-policy-test")
    task = {"name": "CBG报量对账", "full_name": "\\CBG报量对账",
            "ownership_unverified": True, "unreadable": True}
    with mock.patch.object(web.schedule, "status",
                           return_value={"tasks": [task]}):
        assert not web._schedule_task_is_listed(root, "\\CBG报量对账")


@pytest.mark.parametrize(("method", "path", "body"), [
    ("POST", "/api/schedule/run", {}),
    ("DELETE", "/api/schedule", {}),
])
def test_unnamed_schedule_action_requires_the_default_task_to_be_listed(method, path, body):
    """兼容省略 name 时也只能触碰清单内的默认任务。"""
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    with mock.patch.object(web.schedule, "status", return_value={"tasks": []}), \
            mock.patch.object(web.schedule, "run_now") as run_now, \
            mock.patch.object(web.schedule, "remove") as remove:
        (status, payload), _ = api_request(scope(), path, method, body=body, app=app)

    assert status == 404
    assert payload["error"] == "只能操作本程序已登记的定时任务"
    run_now.assert_not_called()
    remove.assert_not_called()


def test_lifehall_entry_is_denied_every_business_route_outside_its_page_set():
    """生活馆直调接口的范围必须与生活馆页面集合一致。"""
    from src import edition

    lifehall = scope("store", {"生活馆本店"})
    lifehall.update(entry_kind="lifehall", runtime_lifehall=True)
    checked = 0
    for (method, path), (page, _op) in sorted(registry.route_perms().items()):
        if page in edition.LIFEHALL_PAGES:
            continue
        checked += 1
        denied = web.registered_route_guard(lifehall, method, path, setup_ready=True)
        assert denied and denied.get("forbidden"), (method, path, page, denied)
    assert checked > 0


def test_inventory_warehouse_list_contains_only_authorized_stores():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.inventory_warehouses.return_value = {
        "ok": True,
        "warehouses": [
            {"Id": "1", "Name": "甲店", "BranchName": "甲店"},
            {"Id": "2", "Name": "乙店", "BranchName": "乙店"},
            {"Id": "3", "Name": "甲店", "BranchName": "乙店"},
        ],
        "default_store_id": "2", "default_store_name": "乙店",
    }
    (status, body), _ = api_request(scope("store", {"甲店"}),
                                    "/api/inventory/warehouses", app=app)
    assert status == 200
    assert [row["Id"] for row in body["warehouses"]] == ["1"]
    assert body["default_store_id"] == ""


def test_inventory_book_rejects_an_unauthorized_target_before_fetch():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.inventory_warehouses.return_value = {
        "ok": True,
        # 仓库名虽碰巧与甲店同名，所属门店是乙店时仍必须拒绝。
        "warehouses": [{"Id": "2", "Name": "甲店", "BranchName": "乙店"}],
    }
    (status, body), _ = api_request(scope("store", {"甲店"}),
                                    "/api/inventory/book", "POST",
                                    body={"storeId": "2"}, app=app)
    assert status == 403 and body["forbidden"]
    app.inventory_book.assert_not_called()


def test_inventory_book_allows_an_authorized_target():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.inventory_warehouses.return_value = {
        "ok": True,
        "warehouses": [{"Id": "1", "Name": "甲店", "BranchName": "甲店"}],
    }
    app.inventory_book.return_value = {"ok": True, "rows": []}
    (status, body), _ = api_request(scope("store", {"甲店"}),
                                    "/api/inventory/book", "POST",
                                    body={"storeId": "1"}, app=app)
    assert status == 200 and body["ok"]
    app.inventory_book.assert_called_once_with("", "1")


def test_inventory_transit_rejects_an_unauthorized_target_before_fetch():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.inventory_warehouses.return_value = {
        "ok": True,
        "warehouses": [{"Id": "2", "Name": "乙店", "BranchName": "乙店"}],
    }
    (status, body), _ = api_request(scope("store", {"甲店"}),
                                    "/api/inventory/transit", "POST",
                                    body={"storeId": "2"}, app=app)
    assert status == 403 and body["forbidden"]
    app.inventory_transit.assert_not_called()


def test_pools_history_index_does_not_leak_other_store_days_or_company_counts(tmp_path):
    from src import pools_history

    pools_history.save_day(
        tmp_path, "2026-09-30", {"AD": 2, "BC": 1, "AC": 7, "BD": 9},
        [{"sn": "own", "云商门店": "甲店"},
         {"sn": "other", "云商门店": "乙店"}],
        [{"sn": "unknown"}],
    )
    pools_history.save_day(
        tmp_path, "2025-09-30", {"AD": 1, "BC": 0},
        [{"sn": "other-only", "云商门店": "乙店"}], [],
    )
    sc = scope("manager", ["甲店"])

    from src import pools_history
    assert comparison_http.scope_years(
        sc, tmp_path, pools_history=pools_history,
        scope_store_ok=web.scope_store_ok,
        has_data_grant=web._scope_has_data_grant) == [2026]
    days = comparison_http.scope_days(
        sc, tmp_path, 2026, pools_history=pools_history,
        scope_store_ok=web.scope_store_ok,
        has_data_grant=web._scope_has_data_grant)
    assert days == [{"date": "2026-09-30", "AD": 1, "BC": 0,
                     "AC": None, "BD": None, "BC_样机": None}]


def test_pools_history_http_index_uses_scoped_list_helpers(tmp_path):
    from src import pools_history

    pools_history.save_day(
        tmp_path, "2026-09-30", {"AD": 1, "BC": 0, "AC": 17, "BD": 8},
        [{"sn": "own", "云商门店": "甲店"}], [],
    )
    sc = scope("manager", ["甲店"])
    sc["pages"] = ["pools"]
    app = mock.Mock(root=tmp_path)

    (status, body), _ = api_request(sc, "/api/pools/history",
                                    query={"year": ["2026"]}, app=app)

    assert status == 200
    assert body["years"] == [2026]
    assert body["days"] == [{"date": "2026-09-30", "AD": 1, "BC": 0,
                              "AC": None, "BD": None, "BC_样机": None}]


def test_pools_row_with_conflicting_source_stores_is_excluded():
    detail = {
        "counts": {"AD": 1, "BC": 0, "AC": 0, "BD": 0},
        "AD": [{"sn": "cross-store", "云商门店": "乙店", "玲珑门店": "甲店"}],
        "BC": [],
    }

    scoped = comparison_http.scope_detail(
        scope("manager", {"甲店"}), detail,
        scope_store_ok=web.scope_store_ok,
        has_data_grant=web._scope_has_data_grant)

    assert scoped["AD"] == []
    assert scoped["counts"]["AD"] == 0


def test_staff_inbox_rejects_conflicting_store_code_and_name(tmp_path):
    from src.app import report_inbox

    config = tmp_path / "config"
    config.mkdir()
    (config / "stores.yaml").write_text(
        "stores:\n"
        "  - erp_name: 甲店\n"
        "    tdoc_name: 甲店短名\n"
        "    huawei_code: SCN-A\n"
        "  - erp_name: 乙店\n"
        "    tdoc_name: 乙店短名\n"
        "    huawei_code: SCN-B\n",
        encoding="utf-8",
    )
    app = object.__new__(web.App)
    app.root = tmp_path
    sc = scope("manager", {"甲店", "甲店短名"})
    rows = [("SCN-B", {"store_name": "甲店", "rows": [{"name": "乙店员工"}]})]

    with mock.patch.object(report_inbox, "tables_of", return_value=rows):
        result = app._staff_from_inbox(sc)

    assert result["stores"] == []


def test_manager_staff_http_does_not_load_local_erp_configuration(tmp_path):
    from src.app import report_inbox

    app = mock.Mock(root=tmp_path, config_path=tmp_path / "store.yaml")
    app.erp_env_file.side_effect = AssertionError("manager inbox read touched local ERP config")
    authorized = scope("manager", {"甲店", "甲店短名"})
    rows = [("SCN-A", {"store_name": "甲店", "rows": [{"name": "甲员工", "active": True}]})]
    stores = [{"erp_name": "甲店", "tdoc_name": "甲店短名", "huawei_code": "SCN-A"}]

    with mock.patch.object(web.config_io, "stores_table", return_value=stores), \
         mock.patch.object(report_inbox, "tables_of", return_value=rows):
        response, _ = api_request(authorized, "/api/staff", app=app)

    assert response[0] == 200
    assert response[1]["stores"][0]["store_name"] == "甲店"
    app.erp_env_file.assert_not_called()


def test_inventory_export_rejects_unauthorized_store_before_reading_upload():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    read_bytes = mock.Mock(return_value=b"PK")
    (status, body), handler_app = api_request(
        scope("store", {"甲店"}), "/api/inventory/export", "POST",
        query={"store": ["乙店"]}, app=app, read_bytes=read_bytes)
    assert status == 403 and body["forbidden"]
    read_bytes.assert_not_called()
    assert handler_app.inventory_export.call_count == 0


def test_inventory_serial_index_can_return_full_company_rows():
    app = mock.Mock(root=Path("/temporary-route-policy-test"))
    app.inventory_index.return_value = {"ok": True, "rows": [{"Store": "乙店"}]}
    (status, body), _ = api_request(scope("store", {"甲店"}),
                                    "/api/inventory/index", "POST", app=app)
    assert status == 200
    assert body["rows"] == [{"Store": "乙店"}]
    app.inventory_index.assert_called_once_with("")


def test_store_cannot_change_identity_or_promote_itself_via_config_api(tmp_path):
    """身份与授权范围只能由已验证的登录流程写入，不能借通用设置自改。"""
    app = web.App(root=tmp_path, config="config/store.yaml")
    app.config_path.parent.mkdir(parents=True)
    app.config_path.write_text(
        'erp_store_name: 甲店\nstore_code: CODE-A\nmarker: A\n'
        'erp_branch_id: BRANCH-A\nplatform: false\n', encoding="utf-8")

    updates = (
        {"platform": True},
        {"erp_store_name": "平台岗"},
        {"erp_branch_id": "BRANCH-B"},
        {"store_code": "CODE-B"},
        {"marker": "B"},
    )
    identities = (scope("store", {"甲店"}), scope("manager", {"甲店"}),
                  scope("platform", None))
    for identity in identities:
        for values in updates:
            (status, body), _ = api_request(
                identity, "/api/config", "PUT",
                body={"values": values}, app=app)
            assert status == 400, (identity["role"], values, body)

    (status, body), _ = api_request(
        scope("store", {"甲店"}), "/api/config", "PUT",
        body={"values": {"timezone": "Asia/Shanghai"}}, app=app)
    assert status == 200 and body["ok"]

    saved = web.config_io.load_raw(app.config_path)
    assert saved == {
        "erp_store_name": "甲店", "store_code": "CODE-A", "marker": "A",
        "erp_branch_id": "BRANCH-A", "platform": False,
        "timezone": "Asia/Shanghai",
    }
    assert web.role_scope(app)["role"] == web.ROLE_STORE
