"""收信导入范围在 HTTP、CLI 与定时入口共用 fail-closed 规则。"""

import json
from types import SimpleNamespace
from unittest import mock

from src.modules.auth import inbox_scope


def _rows():
    return [
        {"erp_name": "甲店", "tdoc_name": "甲店短名", "huawei_code": "SCN-A",
         "region": "一区"},
        {"erp_name": "乙店", "tdoc_name": "乙店短名", "huawei_code": "SCN-B",
         "region": "二区"},
        {"erp_name": "丙店", "tdoc_name": "丙店短名", "huawei_code": "SCN-SHARED",
         "region": "一区"},
        {"erp_name": "丁店", "tdoc_name": "丁店短名", "huawei_code": "SCN-SHARED",
         "region": "二区"},
    ]


def test_manager_scope_only_allows_store_codes_with_every_alias_in_region(monkeypatch, tmp_path):
    from src import config_io

    monkeypatch.setattr(config_io, "stores_table", lambda _root: _rows())
    manager_scope = {"role": "manager", "stores": {"甲店", "甲店短名", "丙店", "丙店短名"}}

    allowed = inbox_scope.store_codes(manager_scope, tmp_path)

    assert allowed == frozenset({"SCN-A"})


def test_missing_manager_scope_and_roster_fail_closed(monkeypatch, tmp_path):
    from src import config_io

    monkeypatch.setattr(config_io, "stores_table", lambda _root: [])

    assert inbox_scope.store_codes({"role": "manager", "stores": set()}, tmp_path) == frozenset()
    assert inbox_scope.store_codes({"role": "manager", "stores": {"甲店"}}, tmp_path) == frozenset()
    assert inbox_scope.store_codes({"role": "unknown", "stores": None}, tmp_path) == frozenset()


def test_local_manager_entry_uses_saved_verified_account_and_authorized_region(monkeypatch, tmp_path):
    from src import config_io, erp

    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    (secrets / "entry.json").write_text(json.dumps({"kind": "erp"}), encoding="utf-8")
    monkeypatch.setattr(config_io, "stores_table", lambda _root: _rows())
    monkeypatch.setattr(config_io, "managers_table", lambda _root: [
        {"name": "區長甲", "accounts": ["MGR-1"], "regions": ["一区"]},
    ])
    monkeypatch.setattr(config_io, "store_profile", lambda _cfg, _root: {"type": "experience"})
    monkeypatch.setattr(erp, "describe_store_credentials", lambda _path: {
        "username": "mgr-1", "who": "區長甲"})

    allowed = inbox_scope.local_store_codes(tmp_path, {})

    assert allowed == frozenset({"SCN-A"})


def test_local_platform_scope_requires_matching_platform_entry(monkeypatch, tmp_path):
    from src import config_io, erp

    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    (secrets / "entry.json").write_text(json.dumps({"kind": "platform"}), encoding="utf-8")
    monkeypatch.setattr(config_io, "stores_table", lambda _root: _rows())
    monkeypatch.setattr(config_io, "managers_table", lambda _root: [])
    monkeypatch.setattr(config_io, "store_profile", lambda _cfg, _root: {"type": "platform"})
    monkeypatch.setattr(erp, "describe_store_credentials", lambda _path: {"username": "", "who": ""})

    assert inbox_scope.local_store_codes(tmp_path, {}) is None

    (secrets / "entry.json").write_text(json.dumps({"kind": "erp"}), encoding="utf-8")
    assert inbox_scope.local_store_codes(tmp_path, {}) == frozenset()


def test_registered_inbox_step_passes_local_scope_to_importer(monkeypatch, tmp_path):
    from src.app import report_inbox
    from src.features import registry

    allowed = frozenset({"SCN-A"})
    monkeypatch.setattr(inbox_scope, "local_store_codes", lambda *args, **kwargs: allowed)
    run = mock.Mock(return_value={"ok": True, "ok_packages": 1, "stores": ["SCN-A"]})
    monkeypatch.setattr(report_inbox, "run", run)
    ctx = SimpleNamespace(root=tmp_path, config="config/store-test.yaml", emit=lambda _msg: None)

    assert registry._run_report_inbox(ctx) is True
    assert run.call_args.kwargs["allowed_store_codes"] == allowed
