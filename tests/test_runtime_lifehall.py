"""FULL 安装选生活馆：旧账号、旧定时配置不得扩大运行能力。临时根，无外部 IO。"""
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent

from src import cli, edition, run_daily, startup, web
from src.app import data_state
from src.features.tools.claim.pending import compute
from src.modules import auth, notify, timer
from src.modules.auth import runtime
from src.modules.timer import once


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CBG_EDITION", "full")
    (tmp_path / "config").mkdir()
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets/entry.json").write_text('{"kind":"lifehall"}')
    (tmp_path / ".secrets/erp-store.env").write_text('USERNAME=old-manager\nTOKEN=old-token\n')
    (tmp_path / "config/store-X.yaml").write_text('store_code: SCN1\nerp_store_name: 旧ERP店\nplatform: true\n')
    with mock.patch.object(web.service, "find_running", return_value=None):
        value = web.App(tmp_path, "config/store-X.yaml")
    value.server = None
    return value


def handler(app, body=None):
    h = object.__new__(web.Handler)
    h.app = app
    h._read_json = lambda: body or {}
    h._json = lambda data, status=200: (status, data)
    return h


def test_root_aware_runtime_leaves_installer_unchanged(app, tmp_path):
    assert runtime.is_lifehall(app.root)
    assert edition.value(app.root) == "full"
    other = tmp_path / "other"
    other.mkdir()
    assert not runtime.is_lifehall(other)
    (other / "EDITION").write_text("lifehall")
    with mock.patch.dict('os.environ', {"CBG_EDITION": ""}):
        assert runtime.is_lifehall(other)
    (app.root / '.secrets/entry.json').write_text('[]')
    assert not runtime.is_lifehall(app.root)


def test_physical_lifehall_prune_dump_success_does_not_import_pools(tmp_path):
    """生活馆包没有 pools.py；成功抓玲珑后也必须能正常结束，不再落回四池模块。"""
    shutil.copytree(ROOT / "src", tmp_path / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for rel in edition.PRUNE:
        if not rel.startswith("src/"):
            continue
        target = tmp_path / rel
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    (tmp_path / "EDITION").write_text("lifehall", encoding="utf-8")

    script = r'''
import argparse
import importlib.util
from pathlib import Path
from unittest import mock

from src import cli
from src.features import registry
from src.paths import ROOT

assert importlib.util.find_spec("src.pools") is None
assert {step.cmd for step in registry.all_steps()} == {"dump", "autoupdate"}
session = cli.session_path({})
session.parent.mkdir(parents=True, exist_ok=True)
session.write_text("stub", encoding="utf-8")
args = argparse.Namespace(config="config/store.yaml", month="", all=False,
                          year=0, no_refresh=False, verbose=False)
record = mock.Mock()
pools = mock.Mock(side_effect=AssertionError("pools must not run"))
with mock.patch.object(cli, "load_config", return_value={}), \
     mock.patch.object(cli, "require_session", return_value=(object(), None)), \
     mock.patch.object(cli, "_record_fetch", record), \
     mock.patch.object(cli, "cmd_pools", pools), \
     mock.patch("src.dump.main", side_effect=[0, 7]):
    assert cli.cmd_dump(args) == cli.EXIT_OK
    assert cli.cmd_dump(args) == 7
assert record.call_args_list[0][0][:2] == ("dump", True)
assert record.call_args_list[1][0][:2] == ("dump", False)
pools.assert_not_called()
assert "src.pools" not in __import__("sys").modules
'''
    env = dict(os.environ)
    env["CBG_EDITION"] = ""
    result = subprocess.run([sys.executable, "-c", script], cwd=str(tmp_path),
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_uses_edition_as_the_single_lifehall_page_allowlist():
    assert runtime.LIFEHALL_PAGES is edition.LIFEHALL_PAGES


def test_scope_pages_ops_and_require_ignore_old_identity(app):
    with mock.patch.object(web.App, '_profile_with_who', side_effect=AssertionError('ERP profile read')), \
         mock.patch.object(web, 'describe_store_credentials', side_effect=AssertionError('ERP credential read')), \
         mock.patch.object(web.config_io, 'stores_table', side_effect=AssertionError('ERP roster')), \
         mock.patch.object(web.config_io, 'store_profile', side_effect=AssertionError('ERP identity')):
        scope = web.role_scope(app)
    assert scope['role'] == 'store'
    assert scope['account'] == ''
    assert set(scope['pages']) == set(runtime.LIFEHALL_PAGES)
    assert set(scope['ops']) <= set(runtime.LIFEHALL_PAGES)
    assert web.require(scope, 'monthly', 'view') is not None
    assert runtime.page_available('cashier', app.root)
    # 未迁移的收银操作仍维持原有路由权限，不杜撰 ops 声明。
    with mock.patch.dict(web.PERM_RULES, {'monthly': {'ops': {'view': frozenset(('store',))}}}):
        assert web.require(scope, 'monthly', 'view') is not None


def test_lifehall_code_only_is_a_single_store_permission_scope(app):
    """生活馆按本机编码就绪；权限解析不能额外依赖旧 ERP 店名或名单。"""
    app.config_path.write_text('store_code: SCN1\n', encoding='utf-8')
    with mock.patch.object(web.config_io, 'stores_table', side_effect=AssertionError('不识别门店')):
        scope = web.role_scope(app)
    assert scope['role'] == 'store'
    assert scope['stores'] == set()
    assert set(scope['pages']) == set(runtime.LIFEHALL_PAGES)
    assert 'claim-pending' in scope['ops']
    assert web.require(scope, 'claim-pending', 'view') is None
    assert web.require(scope, 'cashier', 'view') is None
    # 编码只授予本机单店入口，不变成门店名单里的任意名称授权。
    assert not web.scope_store_ok(scope, '任意别家店')
    assert web.filter_scoped(scope, [{'store': '任意别家店'}], 'authorized') == []
    with mock.patch.object(web.App, 'claim_pending', lambda self: {'ok': True, 'rows': []}):
        assert handler(app)._api('GET', '/api/claim/pending', {})[0] == 200

    app.config_path.write_text('store_code: \n', encoding='utf-8')
    with mock.patch.object(web.config_io, 'stores_table', side_effect=AssertionError('不识别门店')):
        empty = web.role_scope(app)
    assert empty['pages'] == []
    assert web.require(empty, 'claim-pending', 'view') is not None


def test_lifehall_old_erp_logout_cannot_clear_the_saved_entry_code(app):
    app.config_path.write_text('store_code: SCN1\n', encoding='utf-8')

    status, _ = handler(app)._api('POST', '/api/store-account/logout', {})

    assert status == 404
    assert 'store_code: SCN1' in app.config_path.read_text(encoding='utf-8')


@pytest.mark.parametrize('path', [
    '/api/erp', '/api/store-account', '/api/timer', '/api/timer/order',
    '/api/schedule', '/api/schedule/run', '/api/schedule/replace', '/api/report/inbox',
    '/api/pos', '/api/pools', '/api/attain', '/api/film', '/api/benefit',
    '/api/dist/board', '/api/sales-rewrite', '/api/mail/test', '/api/wecom/test',
    '/api/future-business', '/api/session/future-business',
])
def test_direct_unavailable_api_rejected_before_setup(app, path):
    with mock.patch.object(web, 'setup_state', side_effect=AssertionError('setup reached')):
        assert handler(app)._api('POST', path, {})[0] == 404


def test_refresh_rejects_hidden_page_and_dispatches_only_dump(app):
    with mock.patch.object(web.manager, 'current', return_value=None), \
         mock.patch.object(web, '_cool_skip', side_effect=lambda steps, root: (steps, ())), \
         mock.patch.object(web.manager, 'start_steps', return_value=mock.Mock()) as start:
        assert handler(app, {'page':'pools'})._api('POST', '/api/refresh', {})[0] == 404
        assert not start.called
        assert handler(app, {'page':'claim-pending'})._api('POST', '/api/refresh', {})[0] == 200
        assert start.call_args[0][2] == ('dump',)


def test_auth_sources_status_startup_do_not_read_erp(app):
    from src import erp
    with mock.patch.object(erp, 'describe_credentials', side_effect=AssertionError('ERP read')), \
         mock.patch.object(erp, 'describe_store_credentials', side_effect=AssertionError('ERP read')):
        assert auth.accounts({}, app.root)['erp']['user'] == ''
        assert 'erp' not in [x['key'] for x in auth.state({}, app.root)['items']]
    assert all(not x['key'].startswith('erp') for x in data_state.probes(app.root))
    assert [x[0] for x in startup.tasks(app.root)] == ['检查更新']
    with mock.patch.object(web, 'describe_credentials', side_effect=AssertionError('ERP status')):
        assert all('云商' not in r['label'] for r in app.status_brief()['rows'])


def test_business_push_disabled_even_with_old_configuration(app):
    with mock.patch.dict(notify._SENDERS, {'mail': mock.Mock(side_effect=AssertionError('sent'))}):
        assert notify.send('mail', {}, cfg={}, root=app.root, feature='pos')['state'] == 'disabled'
        assert notify.send('mail', {}, cfg={}, root=app.root)['state'] == 'disabled'


def test_periodic_and_once_dispatch_preserves_forbidden_registrations(app):
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    for cmd in ('dump', 'erp-dump', 'report', 'report-inbox'):
        assert once.register(app.root, cmd, at=now)['ok']
    before = once.load(app.root)
    with mock.patch.object(timer, 'due', return_value=[]), \
         mock.patch.object(timer, 'current', return_value={}):
        result = timer.tick(app.root, now=now, spawn=mock.Mock(), busy=lambda:False)
    assert result['ran'] == []
    assert once.load(app.root) == before
    assert {x['cmd'] for x in timer.tasks(app.root)} == {'autoupdate'}
    assert runtime.step_available('dump', app.root)
    assert not runtime.step_available('dump', app.root, recurring=True)
    assert runtime.step_available('autoupdate', app.root, recurring=True)


def test_daily_rejects_old_full_business_before_external_calls(app):
    with mock.patch.object(cli, 'ROOT', app.root), \
         mock.patch.object(cli, 'load_config', side_effect=AssertionError('config execution reached')):
        assert run_daily.main(['--steps', 'erp-dump,report']) == cli.EXIT_USAGE
        assert run_daily.main(['--steps', 'dump', '--wake-slot','2026-10-02 21:00']) == cli.EXIT_USAGE


def test_claim_source_is_linglong_and_filters_code_without_erp_fallback(app):
    db = app.root / 'claim.db'
    with sqlite3.connect(str(db)) as conn:
        conn.executescript('''
            CREATE TABLE orders(document_no TEXT, store_code TEXT, store_name TEXT,
                consumer_guide_name TEXT, doc_create_time TEXT, pay_status INT, return_status INT);
            CREATE TABLE order_lines(document_no TEXT, item_name TEXT, quantity INT, sn TEXT);
            CREATE TABLE returns(related_doc_no TEXT);
        ''')
        conn.executemany('INSERT INTO orders VALUES(?,?,?,?,?,?,?)', [
            ('A','SCN1','玲珑本店','张三','2026-10-01 12:00',2,0),
            ('B','SCN2','旧库他店','李四','2026-10-01 12:00',2,0)])
        conn.executemany('INSERT INTO order_lines VALUES(?,?,?,?)', [
            ('A','手机/华为Pura 70',1,'1234567890ABCDEF'),
            ('B','手机/华为Pura 70',1,'1234567890ABCDE0')])
    acts = [{'id':'test', 'title':'测试', 'match':['Pura 70'], 'start':'2026-10-01','end':'2026-10-31'}]
    with mock.patch.object(compute, 'find_db', return_value=db), \
         mock.patch.object(compute.catalog, 'load_activities', return_value=acts), \
         mock.patch.object(compute, 'load_sales', side_effect=AssertionError('ERP fallback')), \
         mock.patch.object(compute, 'load_stock_sn_map', side_effect=AssertionError('ERP stock')):
        result = app.claim_pending()
    assert result['ok']
    assert [x['store'] for x in result['rows']] == ['玲珑本店']


def test_full_mode_capabilities_and_data_source_unchanged(app):
    web.save_entry(app, 'erp')
    assert runtime.api_available('/api/sales-rewrite', app.root)
    assert runtime.step_available('report', app.root, recurring=True)
    assert any(x['key'].startswith('erp') for x in data_state.probes(app.root))
    assert len(startup.tasks(app.root)) > 1


def test_general_routes_allow_only_general_writes(app):
    # ERP 身份字段和推送开关既不能重写也不能回显，运行控制不接受自定义任务。
    h = handler(app, {'values': {'erp_store_name':'别店', 'platform':True}})
    before = app.config_path.read_bytes()
    assert h._api('PUT', '/api/config', {})[0] == 400
    assert app.config_path.read_bytes() == before
    assert set(handler(app)._api('GET', '/api/config', {})[1]) <= {'store_code','timezone'}
    assert handler(app, {'key':'plat:mail', 'enabled':True})._api('PUT','/api/notify-pref',{})[0] == 400
    assert set(handler(app)._api('GET','/api/notify-pref',{})[1]['prefs']) == {
        'plat:mail', 'plat:wecom'}
    assert handler(app, {'steps':['erp-dump']})._api('POST','/api/run',{})[0] == 410
    # 服务管理只注册开机自启，无 daily/schedule 配置转发。
    with mock.patch.object(web.autostart, 'install', return_value={'ok':True}) as install:
        assert handler(app, {'steps':['erp-dump'], 'daily':'report'})._api('POST','/api/autostart',{})[0] == 200
        install.assert_called_once_with(app.root, elevated=None)


def test_once_allowed_dispatch_does_not_expire_forbidden_records(app):
    now = datetime.datetime.now().replace(second=0, microsecond=0)
    once.register(app.root, 'report', at=now-datetime.timedelta(days=7))
    once.register(app.root, 'autoupdate', at=now)
    result = once.take_due(app.root, now=now)
    assert [x['cmd'] for x in result] == ['autoupdate']
    assert [x['cmd'] for x in once.load(app.root)] == ['report']


def test_overview_skips_erp_business_schedule_sources(app):
    with mock.patch.object(web, 'describe_store_credentials', side_effect=AssertionError('ERP')), \
         mock.patch.object(web.App, '_profile_with_who', side_effect=AssertionError('ERP')), \
         mock.patch.object(web.App, '_selfheal_runner_script', side_effect=AssertionError('runner rewrite')), \
         mock.patch.object(web.schedule, 'status', side_effect=AssertionError('schedule')), \
         mock.patch.object(web.timer, 'next_run', side_effect=AssertionError('timer')), \
         mock.patch.object(web, 'list_reports', side_effect=AssertionError('business reports')), \
         mock.patch.object(web.App, 'boot_state', return_value={}), \
         mock.patch.object(web.App, '_data_state_with_dismiss', return_value={}):
        out = app.overview()
    assert out['store_account'] == {}
    assert out['schedule'] == {}
    assert out['timer'] == {}
    assert out['role']['runtime_lifehall']


def test_manual_linglong_config_needs_no_erp_roster(app):
    # 根目录没有 stores.yaml，保存店码之后仍可读取抓取配置。
    with mock.patch.object(web.config_io, 'stores_table', side_effect=AssertionError('roster')):
        cfg = cli.load_config(app.config_path, root=app.root)
    assert cfg['store_code'] == 'SCN1'
    assert cfg['_experience'] == set()


def test_explicit_root_installer_lifehall_gate_and_lock(app, monkeypatch):
    monkeypatch.delenv('CBG_EDITION', raising=False)
    (app.root / '.secrets/entry.json').unlink()
    (app.root / 'EDITION').write_text('lifehall')
    with mock.patch.object(web, 'describe_store_credentials', side_effect=AssertionError('ERP')), \
         mock.patch.object(web.config_io, 'store_profile', side_effect=AssertionError('ERP')):
        assert web.setup_state(app)['ready']
        assert web.setup_state(app)['runtime_lifehall']
        assert web.entry_state(app)['lifehall_edition']
        assert handler(app, {'kind':'erp'})._api('POST','/api/entry',{})[0] == 400
    with mock.patch('src.store_identity.set_store_code', return_value={'ok':True}) as save:
        assert handler(app, {'store_code':'SCN1'})._api('POST','/api/session/store-code',{})[0] == 200
        assert save.called


@pytest.mark.parametrize('installer', [False, True])
def test_claim_explicit_erp_source_cannot_override_lifehall(app, monkeypatch, installer):
    if installer:
        monkeypatch.delenv('CBG_EDITION', raising=False)
        (app.root / '.secrets/entry.json').unlink()
        (app.root / 'EDITION').write_text('lifehall')
    with mock.patch.object(compute, 'load_sales', side_effect=AssertionError('ERP fallback')), \
         mock.patch.object(compute, 'load_stock_sn_map', side_effect=AssertionError('ERP')):
        with pytest.raises(ValueError, match='生活馆'):
            compute.load(app.root, source='erp')


def test_lifehall_dump_does_not_dispatch_into_pruned_comparison_module(app, monkeypatch):
    """生活馆保留 dump 抓取；后续不能调用裁剪掉的四池对比模块。"""
    from types import SimpleNamespace
    from src import dump as dumpmod

    session = app.root / '.secrets/session.json'
    session.write_text('{}', encoding='utf-8')
    monkeypatch.setattr(cli, 'ROOT', app.root)
    monkeypatch.setattr(cli, 'load_config', lambda _path: {'store_code': 'SCN1'})
    monkeypatch.setattr(cli, 'session_path', lambda _cfg: session)
    monkeypatch.setattr(cli, 'require_session', lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(cli, '_record_fetch', lambda *args, **kwargs: None)
    monkeypatch.setattr(dumpmod, 'main', lambda _argv: 0)
    monkeypatch.setattr(cli, 'cmd_pools', lambda _args: pytest.fail(
        '生活馆 dump 不应导入或派发到已裁剪的 src.pools'))

    result = cli.cmd_dump(SimpleNamespace(config='config/store-X.yaml', month='',
                                           all=False, year=0, verbose=False,
                                           no_refresh=True))

    assert result == cli.EXIT_OK


def test_allowed_config_write_does_not_echo_old_erp_fields(app):
    status, out = handler(app, {'values': {'timezone':'Asia/Shanghai'}})._api('PUT','/api/config',{})
    assert status == 200
    assert set(out['values']) <= {'store_code','timezone'}


def test_lifehall_download_requires_generated_cashier_provenance(app):
    import io
    from src.features.cashier import exporter, downloads
    directory = app.root / 'out/exports'
    directory.mkdir(parents=True)
    old = directory / '收银-old-full.xlsx'
    old.write_bytes(b'old full business export')
    assert handler(app)._api('GET','/api/export/download',{'name':[old.name]})[0] == 403

    def export(root, month, who=''):
        target = directory / '收银-new.xlsx'
        target.write_bytes(b'cashier generated workbook')
        return {'ok':True, 'file':target.name, 'path':str(target)}
    with mock.patch.object(exporter, 'export', side_effect=export):
        result = app.cashier_export({'month':'2026-10'})
    assert result['ok']
    assert downloads.authorized(app.root, result['file'], 'SCN1')
    h = handler(app)
    h.send_response = mock.Mock()
    h.send_header = mock.Mock()
    h.end_headers = mock.Mock()
    h.wfile = io.BytesIO()
    assert h._api('GET','/api/export/download',{'name':[result['file']]}) is None
    h.send_response.assert_called_once_with(200)
    assert h.wfile.getvalue() == b'cashier generated workbook'
    # 知道真文件名和另取一个看似收银的名也没有作用；店码和摘要都绑定生成记录。
    assert not downloads.authorized(app.root, result['file'], 'SCN2')
    assert handler(app)._api('GET','/api/export/download',{'name':['../'+result['file']]})[0] == 403
    copied = directory / '收银-forged.xlsx'
    copied.write_bytes(h.wfile.getvalue())
    assert handler(app)._api('GET','/api/export/download',{'name':[copied.name]})[0] == 403
    web.config_io.update(app.config_path, {'store_code':'SCN2'})
    assert handler(app)._api('GET','/api/export/download',{'name':[result['file']]})[0] == 403
    web.config_io.update(app.config_path, {'store_code':'SCN1'})
    (directory / result['file']).write_bytes(b'replaced with full business export')
    assert handler(app)._api('GET','/api/export/download',{'name':[result['file']]})[0] == 403


def test_claim_runtime_requires_explicit_local_store_code(app):
    with mock.patch.object(compute, 'load_sales_linglong', side_effect=AssertionError('unscoped read')):
        with pytest.raises(ValueError, match='门店编码'):
            compute.load(app.root)


@pytest.mark.parametrize('path,method,body', [
    ('/api/entry','POST',{'kind':'erp'}), ('/api/entry','DELETE',{}),
    ('/api/session/store-code','POST',{'store_code':'SCN2'}),
    ('/api/config','PUT',{'values':{'timezone':'UTC'}}),
])
def test_busy_task_blocks_context_changes_without_file_changes(app,path,method,body):
    before_entry = (app.root / '.secrets/entry.json').read_bytes()
    before_config = app.config_path.read_bytes()
    for manager_busy,timer_busy in [(mock.Mock(),{}),(None,{'steps':['erp-dump']})]:
        with mock.patch.object(web.manager,'current',return_value=manager_busy), \
             mock.patch.object(web.timer,'current',return_value=timer_busy):
            assert handler(app,body)._api(method,path,{})[0] == 409
    assert (app.root / '.secrets/entry.json').read_bytes() == before_entry
    assert app.config_path.read_bytes() == before_config


def test_running_daily_guard_blocks_entry_and_ordinary_reads_do_not_lock(app):
    from src.modules.auth import runtime_guard
    with runtime_guard.guard(app.root) as acquired:
        assert acquired
        assert handler(app,{'kind':'erp'})._api('POST','/api/entry',{})[0] == 409
        assert handler(app)._api('GET','/api/setup',{})[0] == 200
        assert handler(app)._api('GET','/api/entry',{})[0] == 200


def test_daily_rechecks_runtime_before_next_step(app):
    from types import SimpleNamespace
    web.save_entry(app,'erp')
    next_step = mock.Mock(return_value=0)
    def first(ctx):
        # 模拟旁路文件变更；HTTP切换受guard保护，但派发仍必须复验。
        web.save_entry(app,'lifehall')
        return 0
    declarations = {name:SimpleNamespace(run=fn, label=name,record=True,partner_ok=True)
                    for name,fn in [('film',first),('benefit',next_step)]}
    with mock.patch.object(cli,'ROOT',app.root), \
         mock.patch.object(cli,'load_config',return_value={'store_code':'SCN1'}), \
         mock.patch.object(cli,'store_profile_of',return_value={'needs_linglong':True}), \
         mock.patch.object(run_daily,'step_decl',side_effect=lambda name:declarations[name]):
        assert run_daily.main(['--steps','film,benefit']) == cli.EXIT_USAGE
    assert not next_step.called


def _cashier_row():
    return {'sold_at':'2026-10-01 12:00:00','goods_code':'X','goods_name':'本店商品',
            'quantity':1,'amount':100,'seller':'张三'}


def test_legacy_unbound_cashier_rows_cannot_gain_current_owner(app):
    from src.features.cashier import store, ownership, exporter
    web.save_entry(app,'erp')
    assert store.save_entry(app.root,_cashier_row())['ok']
    web.save_entry(app,'lifehall')
    web.config_io.update(app.config_path, {'store_code':'NEWSTORE'})
    with mock.patch.object(exporter,'export',side_effect=AssertionError('legacy export')):
        out = app.cashier_export({'month':'2026-10'})
    assert not out['ok'] and '归属' in out['why']
    assert not app.cashier_entry_save(_cashier_row())['ok']
    assert not app.cashier_commit({'day':'2026-10-01'})['ok']
    with sqlite3.connect(str(store.db_path(app.root))) as conn:
        assert conn.execute('SELECT count(*) FROM sale_entries').fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='cashier_owner'").fetchone()


def test_first_scoped_cashier_write_binds_and_full_mode_cannot_pollute(app):
    from src.features.cashier import store, downloads
    assert not store.save_entry(app.root,_cashier_row())['ok'] # 无可信scope不能直接盖章
    assert app.cashier_entry_save(_cashier_row())['ok']
    result = app.cashier_export({'month':'2026-10'})
    assert result['ok']
    assert downloads.authorized(app.root,result['file'],'SCN1')
    web.config_io.update(app.config_path,{'store_code':'SCN2'})
    assert not app.cashier_entry_save(_cashier_row())['ok']
    assert not app.cashier_export({'month':'2026-10'})['ok']
    assert not downloads.authorized(app.root,result['file'],'SCN2')
    web.save_entry(app,'erp')
    assert not store.save_entry(app.root,_cashier_row())['ok']
    assert not store.save_entry(app.root,_cashier_row(),store_code='SCN2')['ok']
    assert not app.cashier_entry_save(_cashier_row())['ok']
    with sqlite3.connect(str(store.db_path(app.root))) as conn:
        assert conn.execute('SELECT store_code FROM cashier_owner').fetchone()[0] == 'SCN1'
        assert conn.execute('SELECT count(*) FROM sale_entries').fetchone()[0] == 1


@pytest.mark.parametrize('method,args', [
    ('cashier_entry_save',(_cashier_row(),)), ('cashier_entry_delete',(1,)),
    ('cashier_exclude',(1,)), ('cashier_commit',({'day':'2026-10-01'},)),
    ('cashier_import',({'day':'2026-10-01'},)), ('cashier_policy_refresh',()),
    ('cashier_import_settings',({'blacklist':['x']},True)),
])
def test_bound_cashier_all_mutations_reject_different_store_before_io(app,method,args):
    from src.features.cashier import store
    assert app.cashier_entry_save(_cashier_row())['ok']
    web.config_io.update(app.config_path,{'store_code':'OTHER'})
    with mock.patch.object(web.App,'cbg_client',side_effect=AssertionError('network client')), \
         mock.patch('src.pmall.ensure_session',side_effect=AssertionError('policy network')):
        result = getattr(app,method)(*args)
    assert not result['ok'] and '归属' in result['why']
    with sqlite3.connect(str(store.db_path(app.root))) as conn:
        assert conn.execute('SELECT count(*) FROM sale_entries').fetchone()[0] == 1


@pytest.mark.parametrize('function,args', [
    ('save_entry',(_cashier_row(),)), ('delete_entry',(1,)), ('exclude_entry',(1,)),
    ('commit_entries',('2026-10-01',)), ('entries_from_orders',('2026-10-01',)),
    ('save_policy',([{'商品编码':'x'}],)),
])
def test_bound_cashier_low_level_full_calls_require_matching_scope(app,function,args):
    from src.features.cashier import store
    assert app.cashier_entry_save(_cashier_row())['ok']
    web.save_entry(app,'erp')
    result = getattr(store,function)(app.root,*args)
    assert not result['ok'] and '归属' in result['why']
    result = getattr(store,function)(app.root,*args,store_code='OTHER')
    assert not result['ok'] and '归属' in result['why']


def test_full_store_lookup_identity_write_cannot_overlap_entry_switch(app):
    web.save_entry(app,'erp')
    before = (app.root / '.secrets/entry.json').read_bytes()
    def login(*args,**kwargs):
        status,_ = handler(app,{'kind':'lifehall','store_code':'OTHER'})._api('POST','/api/entry',{})
        assert status == 409
        return {'ok':True}
    with mock.patch.object(web,'store_lookup',side_effect=login):
        assert handler(app,{'code':'x'})._api('POST','/api/store-account/lookup',{})[0] == 200
    assert (app.root / '.secrets/entry.json').read_bytes() == before
    assert web.config_io.load_raw(app.config_path)['store_code'] == 'SCN1'


def test_background_capture_holds_runtime_context_for_whole_worker(app):
    import threading
    from src.modules.auth import runtime_guard
    started = threading.Event()
    release = threading.Event()
    def body(*args):
        started.set()
        assert release.wait(5)
    with mock.patch.object(web,'_capture_worker_locked',side_effect=body):
        thread = threading.Thread(target=web._capture_worker,args=(app,False))
        thread.start()
        assert started.wait(5)
        try:
            assert handler(app,{'kind':'erp'})._api('POST','/api/entry',{})[0] == 409
        finally:
            release.set()
            thread.join(5)
    with runtime_guard.guard(app.root) as acquired:
        assert acquired


def test_startup_refresh_holds_same_context_guard(app):
    def startup_task(*args):
        assert handler(app, {'kind':'erp'})._api('POST','/api/entry',{})[0] == 409
        return 'checked'
    with mock.patch.object(startup,'tasks',return_value=[('检查更新',startup_task)]):
        assert startup.run_once(app,force=True,say=lambda *args:None)['ran']


@pytest.mark.parametrize('kind', ['lifehall', 'erp'])
def test_cashier_lookup_keeps_goods_code_separate_from_bound_store(app, kind):
    from src.features.cashier import store
    assert app.cashier_entry_save(_cashier_row())['ok']
    assert store.save_policy(app.root, [{'商品编码':'GOODS-42', '商品名称':'测试商品'}],
                             store_code='SCN1')['ok']
    web.save_entry(app, kind)
    result = app.cashier_lookup('GOODS-42')
    assert result['ok'] and result['found']
    assert not app.cashier_lookup('SCN1')['found']


def test_startup_busy_leaves_mark_absent_and_can_retry(app):
    from src.modules.auth import runtime_guard
    with runtime_guard.guard(app.root) as acquired:
        assert acquired
        assert not startup.run_once(app, say=lambda *args: None)['ran']
    assert startup.last_run(app.root) == ''
    with mock.patch.object(startup, 'tasks', return_value=[('检查更新', lambda app: 'checked')]):
        assert startup.run_once(app, say=lambda *args: None)['ran']
    assert startup.last_run(app.root)


def test_delayed_full_writer_rechecks_owner_in_actual_write_transaction(app):
    import threading
    from src.features.cashier import store, ownership
    web.save_entry(app, 'erp')
    # 旧 FULL 的前置检查无 owner；它不能作为稍后写入的授权。
    assert ownership.failure(app.root, None, write=True) is None
    entered, release = threading.Event(), threading.Event()
    original = store._clean_entry
    results = []
    def pause(data, entry_id):
        if data.get('goods_code') == 'OLD_PRIVATE_SALE':
            entered.set()
            assert release.wait(5)
        return original(data, entry_id)
    old = dict(_cashier_row(), goods_code='OLD_PRIVATE_SALE')
    with mock.patch.object(store, '_clean_entry', side_effect=pause):
        writer = threading.Thread(target=lambda: results.append(store.save_entry(app.root, old)))
        writer.start()
        assert entered.wait(5)
        try:
            web.save_entry(app, 'lifehall')
            assert app.cashier_entry_save(_cashier_row())['ok']
        finally:
            release.set()
            writer.join(5)
    assert not writer.is_alive()
    assert len(results) == 1 and not results[0]['ok']
    with sqlite3.connect(str(store.db_path(app.root))) as conn:
        assert conn.execute('SELECT store_code FROM cashier_owner').fetchone()[0] == 'SCN1'
        assert conn.execute("SELECT count(*) FROM sale_entries WHERE goods_code='OLD_PRIVATE_SALE'").fetchone()[0] == 0
    assert app.cashier_export({'month':'2026-10'})['ok']


@pytest.mark.parametrize('function,args', [
    ('save_entry',(_cashier_row(),)), ('delete_entry',(1,)), ('exclude_entry',(1,)),
    ('commit_entries',('2026-10-01',)), ('entries_from_orders',('2026-10-01',)),
    ('save_policy',([{'商品编码':'x'}],)),
])
def test_cashier_mutation_checks_owner_on_same_open_write_transaction(app,function,args):
    from src.features.cashier import store, ownership
    original = ownership.check
    checked = []
    def check(root, store_code=None, write=False, conn=None):
        assert conn is not None and conn.in_transaction
        checked.append(conn)
        return original(root, store_code, write, conn=conn)
    with mock.patch.object(ownership, 'check', side_effect=check):
        getattr(store, function)(app.root, *args, store_code='SCN1')
    assert len(checked) == 1
