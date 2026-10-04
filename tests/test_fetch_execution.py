"""抓取步骤的实际执行入口由数据抓取能力模块持有。"""

import argparse
import contextlib
import datetime
import io
from types import SimpleNamespace
from unittest import mock

from src.features import registry
from src.modules.fetch import execution


def test_dump_steps_use_registered_fetch_execution_entrypoints():
    steps = {step.cmd: step for step in registry.BUILTIN_STEPS}

    for command in ("dump", "erp-dump"):
        run = steps[command].run
        assert callable(run), "%s 必须由注册表直接派发" % command
        assert run.__module__ == "src.modules.fetch.execution"


def _context(tmp_path, *, daily=True, has_db=True, args=None):
    session = tmp_path / "session.json"
    session.write_text("{}", encoding="utf-8")
    config = {"store_code": "SCN123456"}
    services = {
        "load_config": mock.Mock(return_value=config),
        "session_path": mock.Mock(return_value=session),
        "require_session": mock.Mock(return_value=(None, None)),
        "find_pos_db": mock.Mock(return_value=(tmp_path / "existing.db")
                                  if has_db else None),
        "record_fetch": mock.Mock(),
        "fetchers": {},
    }
    return SimpleNamespace(root=tmp_path, config="config/store-test.yaml",
                           args=args or argparse.Namespace(no_refresh=False,
                                                           verbose=False),
                           daily_step=daily, services=services)


def test_daily_dump_without_database_fetches_this_year_only(tmp_path, monkeypatch):
    ctx = _context(tmp_path, has_db=False,
                   args=argparse.Namespace(no_refresh=True, verbose=True,
                                           date="2026-09-10", days_ago=3,
                                           lookback=3, lookahead=1))
    captured = {}
    monkeypatch.setattr("src.dump.main", lambda argv: captured.update(argv=argv) or 0)
    pool_calls = []
    monkeypatch.setattr(execution, "run_pool_fetch",
                        lambda args, **kw: pool_calls.append(args.fetch) or 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: False)

    rc = execution.run_dump(ctx)

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--year") + 1] == str(datetime.date.today().year)
    assert "--month" not in argv
    assert "--all" not in argv
    assert "--date" not in argv and "--days-ago" not in argv
    ctx.services["require_session"].assert_called_once_with(
        {"store_code": "SCN123456"}, verbose=True, allow_refresh=False)
    assert pool_calls == [["lg-stock"]]
    ctx.services["record_fetch"].assert_called_once()


def test_daily_dump_with_existing_database_fetches_current_month(tmp_path, monkeypatch):
    ctx = _context(tmp_path, has_db=True)
    captured = {}
    monkeypatch.setattr("src.dump.main", lambda argv: captured.update(argv=argv) or 0)
    monkeypatch.setattr(execution, "run_pool_fetch", lambda args, **kw: 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: True)

    assert execution.run_dump(ctx) == 0
    argv = captured["argv"]
    assert argv[argv.index("--month") + 1] == "current"
    assert "--year" not in argv and "--all" not in argv


def test_non_daily_dump_preserves_explicit_year_month_and_all_precedence(tmp_path, monkeypatch):
    ctx = _context(tmp_path, daily=False, args=argparse.Namespace(
        year=2024, month="2026-02", all=True, no_refresh=False, verbose=False))
    captured = {}
    monkeypatch.setattr("src.dump.main", lambda argv: captured.update(argv=argv) or 0)
    monkeypatch.setattr(execution, "run_pool_fetch", lambda args, **kw: 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: True)

    assert execution.run_dump(ctx) == 0
    assert captured["argv"][captured["argv"].index("--year") + 1] == "2024"
    assert "--month" not in captured["argv"] and "--all" not in captured["argv"]


def test_dump_failure_is_recorded_and_does_not_fetch_linglong_stock(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr("src.dump.main", lambda argv: 7)
    pool_calls = []
    monkeypatch.setattr(execution, "run_pool_fetch",
                        lambda *a, **kw: pool_calls.append(a) or 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: False)

    assert execution.run_dump(ctx) == 7
    assert pool_calls == []
    call = ctx.services["record_fetch"].call_args
    assert call.args[:2] == ("dump", False)


def test_daily_dump_failure_explains_why_following_steps_are_skipped(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr("src.dump.main", lambda argv: 7)
    monkeypatch.setattr(execution, "run_pool_fetch", lambda *a, **kw: 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: False)
    err = io.StringIO()

    with contextlib.redirect_stderr(err):
        assert execution.run_dump(ctx) == 7

    message = err.getvalue()
    assert "跳过后续分析" in message
    assert "拿旧库计算" in message
    assert "先解决第 1 步" in message


def test_lifehall_dump_skips_full_version_stock_pool(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    monkeypatch.setattr("src.dump.main", lambda argv: 0)
    pool_calls = []
    monkeypatch.setattr(execution, "run_pool_fetch",
                        lambda *a, **kw: pool_calls.append(a) or 0)
    monkeypatch.setattr("src.modules.auth.runtime.is_lifehall", lambda root: True)

    assert execution.run_dump(ctx) == 0
    assert pool_calls == []


def test_erp_dump_requests_both_pools_and_preserves_partial_failure(tmp_path, monkeypatch):
    ctx = _context(tmp_path)
    seen = {}
    monkeypatch.setattr(execution, "run_pool_fetch",
                        lambda args, **kw: seen.update(fetch=args.fetch) or 2)
    monkeypatch.setattr("src.app.data_state.data_state", lambda root: {
        "sources": [
            {"key": "erp-stock", "state": "ok", "state_label": "新鲜"},
            {"key": "erp-sales", "state": "failed", "state_label": "失败",
             "why": "桩错误"},
        ]})

    assert execution.run_erp_dump(ctx) == 2
    assert seen["fetch"] == ["erp-stock", "erp-sales"]


def test_cli_fetch_commands_are_thin_adapters_to_execution_module(monkeypatch):
    from src import cli

    captured = {}
    monkeypatch.setattr(execution, "run_dump",
                        lambda ctx: captured.setdefault("dump", ctx) and 0)
    monkeypatch.setattr(execution, "run_erp_dump",
                        lambda ctx: captured.setdefault("erp", ctx) and 0)
    dump_args = argparse.Namespace(config="config/store-test.yaml", month="current")
    erp_args = argparse.Namespace(config="config/store-test.yaml")

    assert cli.cmd_dump(dump_args) == 0
    assert cli.cmd_erp_dump(erp_args) == 0
    assert captured["dump"].args is dump_args
    assert captured["erp"].args is erp_args
    assert captured["dump"].services["fetchers"].keys() == {
        "lg-stock", "erp-stock", "erp-sales"}
    assert captured["erp"].services["fetchers"].keys() == {
        "lg-stock", "erp-stock", "erp-sales"}


def test_pool_fetch_calls_each_collector_with_its_declared_signature(tmp_path, monkeypatch):
    from src import dump as dumpmod, pools as poolmod

    conn = mock.Mock()
    conn.close = mock.Mock()
    monkeypatch.setattr(dumpmod, "connect", lambda path: conn)
    monkeypatch.setattr(dumpmod, "year_db", lambda out, year: tmp_path / "fallback.db")
    monkeypatch.setattr(dumpmod, "_run_migrations", lambda conn: None)
    monkeypatch.setattr(poolmod, "ensure", lambda conn: None)
    monkeypatch.setattr(poolmod, "purge_snapshots", lambda conn: {})
    calls = []
    services = {
        "find_pos_db": lambda: tmp_path / "isolated.db",
        "load_config": lambda path: {"store_code": "SCN123456"},
        "record_fetch": mock.Mock(),
        "fetchers": {
            "lg-stock": lambda cfg, db, args: calls.append(
                ("lg-stock", cfg, db, args)) or 0,
            "erp-stock": lambda db, args: calls.append(
                ("erp-stock", db, args)) or 2,
        },
    }
    args = argparse.Namespace(config="config/store-test.yaml",
                              fetch=["lg-stock", "erp-stock"])

    rc = execution.run_pool_fetch(args, services=services, root=tmp_path)

    assert rc == 2
    assert [call[0] for call in calls] == ["lg-stock", "erp-stock"]
    assert calls[0][1] == {"store_code": "SCN123456"}
    assert calls[0][2] is conn and calls[1][1] is conn
    assert [call.args[:2] for call in services["record_fetch"].call_args_list] == [
        ("lg-stock", True), ("erp-stock", False)]
    conn.close.assert_called_once_with()
