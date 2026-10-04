"""Business export provenance and scope checks for the shared download route.

Every generated workbook is tied to its registered page, generation scope, and
content hash. The download route still rechecks the page's current export op.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ...storage.export_paths import export_dir

_ROLES = frozenset(("store", "manager", "platform"))
_ENTRY_KINDS = frozenset(("erp", "platform", "lifehall"))


def _target(root, name):
    if (not name or "/" in name or "\\" in name or Path(name).name != name
            or not name.lower().endswith(".xlsx")):
        return None
    directory = export_dir(root).resolve()
    target = directory / name
    if target.is_symlink() or not target.is_file():
        return None
    try:
        if target.resolve().parent != directory:
            return None
    except OSError:
        return None
    return target


def _record_path(root, name):
    key = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return export_dir(root) / ".download-grants" / (key + ".json")


def _scope_stores(scope):
    if not isinstance(scope, dict) or scope.get("role") not in _ROLES:
        raise ValueError("导出缺少有效身份")
    stores = scope.get("stores")
    if scope["role"] == "platform":
        if stores is not None or _entry_kind(scope) != "platform":
            raise ValueError("平台导出缺少明确的全量范围")
        return None
    if not isinstance(stores, (set, frozenset, list, tuple)):
        raise ValueError("导出缺少有效门店范围")
    names = sorted({str(name).strip() for name in stores if str(name or "").strip()})
    if not names:
        raise ValueError("导出缺少有效门店范围")
    if _entry_kind(scope) not in ("erp", "lifehall"):
        raise ValueError("导出缺少有效进入方式")
    return names


def _entry_kind(scope):
    """Normalize pre-entry-selection installs using the server-recognized role."""
    kind = str(scope.get("entry_kind") or "").strip()
    if not kind:
        kind = "platform" if scope.get("role") == "platform" else "erp"
    role = scope.get("role")
    if ((role == "platform" and kind != "platform")
            or (role == "manager" and kind != "erp")
            or (role == "store" and kind not in ("erp", "lifehall"))):
        raise ValueError("身份与进入方式不匹配")
    if kind not in _ENTRY_KINDS:
        raise ValueError("导出缺少有效进入方式")
    return kind


def register(root, result, page, scope):
    """Record a successful business export; invalid output never gets a grant."""
    if not isinstance(result, dict) or not result.get("ok"):
        raise ValueError("导出结果无效")
    page = str(page or "").strip()
    if not page:
        raise ValueError("导出缺少业务页面")
    stores = _scope_stores(scope)
    name = str(result.get("file") or "")
    target = _target(root, name)
    if target is None:
        raise ValueError("导出文件不存在或文件名无效")
    try:
        if target.resolve() != Path(result.get("path") or "").resolve():
            raise ValueError("导出路径与文件名不匹配")
        content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as error:
        raise ValueError("无法校验导出文件：%s" % error)

    record = {
        "version": 1,
        "page": page,
        "role": scope["role"],
        "entry_kind": _entry_kind(scope),
        "stores": stores,
        "file": name,
        "sha256": content_hash,
    }
    path = _record_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".grant-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, str(path))
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def lookup(root, name):
    """Return a verified file grant, or None for unknown/tampered files."""
    if not isinstance(name, str):
        return None
    target = _target(root, name)
    if target is None:
        return None
    try:
        record = json.loads(_record_path(root, name).read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("version") != 1:
            return None
        if (record.get("file") != name or not str(record.get("page") or "").strip()
                or record.get("role") not in _ROLES
                or record.get("entry_kind") not in _ENTRY_KINDS):
            return None
        stores = record.get("stores")
        if record.get("role") == "platform":
            if stores is not None or record.get("entry_kind") != "platform":
                return None
        elif (not isinstance(stores, list)
              or not any(str(store or "").strip() for store in stores)):
            return None
        content = target.read_bytes()
        if record.get("sha256") != hashlib.sha256(content).hexdigest():
            return None
        return dict(record, target=target, content=content)
    except (OSError, ValueError, TypeError):
        return None


def scope_allows(record, scope):
    """A current grant may fetch only a file whose rows are within its scope."""
    if not isinstance(record, dict):
        return False
    try:
        current = _scope_stores(scope)
    except ValueError:
        return False
    generated = record.get("stores")
    if generated is None:
        return current is None
    if not isinstance(generated, list) or not generated:
        return False
    return current is None or set(generated) <= set(current)
