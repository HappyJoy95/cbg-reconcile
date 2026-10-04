"""生活馆收银导出的下载凭据：来源、店码、内容一起绑定，不靠文件名猜业务。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ...modules.notify.export import export_dir


def _target(root, name):
    if (not name or '/' in name or '\\' in name or Path(name).name != name
            or not name.lower().endswith('.xlsx')):
        return None
    directory = export_dir(root).resolve()
    target = directory / name
    if target.is_symlink() or not target.is_file() or target.resolve().parent != directory:
        return None
    return target


def _record(root, name):
    key = hashlib.sha256(name.encode('utf-8')).hexdigest()
    return export_dir(root) / '.cashier-downloads' / (key + '.json')


def register(root, result, store_code):
    """仅由收银成功生成路径调用；登记失败不给可下载的成功结果。"""
    from . import ownership
    ownership.check(root, store_code)
    code = str(store_code or '').strip()
    target = _target(root, str(result.get('file') or ''))
    if not code or target is None or target.resolve() != Path(result.get('path') or '').resolve():
        raise ValueError('收银导出缺少本店编码或合法生成文件')
    doc = {'feature': 'cashier', 'entry_kind': 'lifehall', 'store_code': code,
           'file': target.name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    path = _record(root, target.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def authorized(root, name, store_code):
    """未知来源、其他店、内容变化、伪名和符号链接都拒绝。"""
    from . import ownership
    if ownership.failure(root, store_code):
        return False
    target = _target(root, name)
    if target is None:
        return False
    try:
        doc = json.loads(_record(root, name).read_text(encoding='utf-8'))
        return (isinstance(doc, dict) and doc.get('feature') == 'cashier'
                and doc.get('entry_kind') == 'lifehall' and doc.get('file') == name
                and bool(str(store_code or '').strip())
                and doc.get('store_code') == str(store_code).strip()
                and doc.get('sha256') == hashlib.sha256(target.read_bytes()).hexdigest())
    except (OSError, ValueError):
        return False
