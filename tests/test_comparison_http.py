"""报量查询 API 路由应归属五项合规的 comparison 子模块。"""

from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def test_comparison_handler_owns_history_and_notification_clear_routes():
    handler = ROOT / "src" / "features" / "compliance" / "comparison" / "http.py"
    assert handler.is_file(), "报量查询 HTTP handler 应与业务功能放在一起"
    source = handler.read_text(encoding="utf-8")
    assert "def handle(" in source
    assert '("GET", "/api/pools/history")' in source
    assert '("POST", "/api/pools-notify/clear")' in source
    assert "from src.web" not in source

    web = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
    assert "comparison_http.handle(" in web
    assert 'if path == "/api/pools/history"' not in web
    assert 'if path == "/api/pools-notify/clear"' not in web


def test_comparison_handler_owns_report_read_delete_and_download_routes():
    handler = ROOT / "src" / "features" / "compliance" / "comparison" / "http.py"
    source = handler.read_text(encoding="utf-8")
    for route in ('("GET", "/api/report")',
                  '("DELETE", "/api/report")',
                  '("GET", "/api/report/download")'):
        assert route in source

    web = (ROOT / "src" / "http" / "app.py").read_text(encoding="utf-8")
    assert "comparison_http.handle(" in web
    assert 'if path == "/api/report" and method' not in web
    assert 'if path == "/api/report/download" and method' not in web


def test_report_operations_recheck_store_scope_before_read_write_or_download(tmp_path):
    from src.features.compliance.comparison import http as comparison_http
    from src import web

    out = tmp_path / "out"
    out.mkdir()
    target = out / "report.xlsx"
    target.write_bytes(b"authorized workbook")
    target.with_suffix(".json").write_text('{"store":"甲店"}', encoding="utf-8")
    app = SimpleNamespace(root=tmp_path, out_dir=out)
    store_scope = {"role": "store", "stores": {"乙店"}}
    load_report = mock.Mock(side_effect=AssertionError("must reject before reading"))
    delete_report = mock.Mock(side_effect=AssertionError("must reject before deleting"))
    callbacks = {
        "require": lambda *_args, **_kwargs: None,
        "scope_store_ok": lambda _scope, store: store in {"乙店"},
        "has_data_grant": lambda _scope: True,
        "forbid": web.forbid,
        "load_report": load_report,
        "delete_report": delete_report,
    }

    for method, path in (("GET", "/api/report"),
                         ("GET", "/api/report/download"),
                         ("DELETE", "/api/report")):
        result = comparison_http.handle(
            app, method, path, {"name": [target.name]}, store_scope, **callbacks)
        assert result[1] == 403
        assert result[0]["forbidden"]

    assert target.exists()
    load_report.assert_not_called()
    delete_report.assert_not_called()


def test_authorized_report_download_is_a_confined_file_response(tmp_path):
    from src.features.compliance.comparison import http as comparison_http

    out = tmp_path / "out"
    out.mkdir()
    target = out / "report.xlsx"
    target.write_bytes(b"authorized workbook")
    target.with_suffix(".json").write_text('{"store":"甲店"}', encoding="utf-8")
    app = SimpleNamespace(root=tmp_path, out_dir=out)
    store_scope = {"role": "store", "stores": {"甲店"}}

    result = comparison_http.handle(
        app, "GET", "/api/report/download", {"name": [target.name]}, store_scope,
        require=lambda *_args, **_kwargs: None,
        scope_store_ok=lambda _scope, store: store == "甲店",
        has_data_grant=lambda _scope: True,
        forbid=lambda *_args, **_kwargs: {"forbidden": True},
    )

    assert isinstance(result, comparison_http.FileResponse)
    assert result.path == target
    assert result.path.read_bytes() == b"authorized workbook"
