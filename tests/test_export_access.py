"""远程下载只能取回当前页面授权范围内、由业务导出器生成的文件。"""

from src.modules.auth import export_access


def _scope(role, stores):
    return {
        "role": role,
        "stores": None if stores is None else set(stores),
        "entry_kind": "platform" if role == "platform" else "erp",
    }


def _result(root, name="attain.xlsx", content=b"xlsx bytes"):
    target = root / "out" / "exports" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {"ok": True, "file": name, "path": str(target)}


def test_lookup_requires_registered_file_and_preserves_page(tmp_path):
    assert export_access.lookup(tmp_path, "missing.xlsx") is None
    result = _result(tmp_path)
    export_access.register(tmp_path, result, "attain", _scope("manager", {"甲店"}))

    record = export_access.lookup(tmp_path, result["file"])

    assert record["page"] == "attain"
    assert record["target"] == tmp_path / "out" / "exports" / result["file"]


def test_download_scope_must_be_contained_by_current_grant(tmp_path):
    result = _result(tmp_path)
    export_access.register(tmp_path, result, "attain", _scope("manager", {"甲店", "乙店"}))
    record = export_access.lookup(tmp_path, result["file"])

    assert not export_access.scope_allows(record, _scope("manager", {"甲店"}))
    assert export_access.scope_allows(record, _scope("manager", {"甲店", "乙店"}))
    assert export_access.scope_allows(record, _scope("platform", None))


def test_platform_export_cannot_be_downloaded_by_narrower_scope(tmp_path):
    result = _result(tmp_path)
    export_access.register(tmp_path, result, "attain", _scope("platform", None))
    record = export_access.lookup(tmp_path, result["file"])

    assert not export_access.scope_allows(record, _scope("manager", {"甲店", "乙店"}))


def test_changed_file_is_not_downloadable(tmp_path):
    result = _result(tmp_path)
    export_access.register(tmp_path, result, "attain", _scope("manager", {"甲店"}))
    (tmp_path / "out" / "exports" / result["file"]).write_bytes(b"replaced")

    assert export_access.lookup(tmp_path, result["file"]) is None


def test_invalid_or_empty_store_scope_cannot_register(tmp_path):
    result = _result(tmp_path)

    try:
        export_access.register(tmp_path, result, "attain", _scope("manager", set()))
    except ValueError:
        pass
    else:
        raise AssertionError("empty scope must not create a downloadable grant")


def test_download_resolver_rejects_paths_and_non_xlsx(tmp_path):
    assert export_access.lookup(tmp_path, "../attain.xlsx") is None
    assert export_access.lookup(tmp_path, "attain.csv") is None
