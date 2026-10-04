"""运行身份锁的真实互斥与失败恢复；只有临时根、短进程，无业务网络。"""
import subprocess
import sys
import threading
from unittest import mock

import pytest

from src.modules.auth import runtime_guard


def test_threads_cannot_unlock_another_holder(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    def holder():
        with runtime_guard.guard(tmp_path) as acquired:
            assert acquired
            entered.set()
            assert release.wait(5)
    worker = threading.Thread(target=holder)
    worker.start()
    assert entered.wait(5)
    try:
        with runtime_guard.guard(tmp_path) as acquired:
            assert not acquired
        with runtime_guard.guard(tmp_path) as acquired:
            assert not acquired
    finally:
        release.set()
        worker.join(5)
    with runtime_guard.guard(tmp_path) as acquired:
        assert acquired


def test_process_lock_contention_and_crash_recovery(tmp_path):
    code = '''
import sys
from src.modules.auth.runtime_guard import guard
with guard(sys.argv[1]) as acquired:
    print('locked' if acquired else 'failed', flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', code, str(tmp_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with runtime_guard.guard(tmp_path) as acquired:
            assert not acquired
        child.kill()
        child.wait(timeout=5)
        with runtime_guard.guard(tmp_path) as acquired:
            assert acquired
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


def test_lock_handle_not_inherited_and_failure_releases_thread_lock(tmp_path):
    with mock.patch.object(runtime_guard, '_file_lock', side_effect=OSError('busy')):
        with runtime_guard.guard(tmp_path) as acquired:
            assert not acquired
    import os
    original = runtime_guard._file_lock
    def checked(handle, acquire):
        assert not os.get_inheritable(handle.fileno())
        return original(handle, acquire)
    with mock.patch.object(runtime_guard, '_file_lock', side_effect=checked):
        with runtime_guard.guard(tmp_path) as acquired:
            assert acquired
    assert (tmp_path / '.secrets/runtime-context.lock').stat().st_size >= 1


def test_windows_byte_lock_always_seeks_zero_and_unlocks_same_byte():
    handle = mock.Mock()
    handle.fileno.return_value = 72
    msvcrt = mock.Mock(LK_NBLCK=2, LK_UNLCK=0)
    with mock.patch.object(runtime_guard.os, 'name', 'nt'), \
         mock.patch.dict(sys.modules, {'msvcrt': msvcrt}):
        runtime_guard._file_lock(handle, True)
        runtime_guard._file_lock(handle, False)
    assert handle.seek.call_args_list == [mock.call(0), mock.call(0)]
    assert msvcrt.locking.call_args_list == [mock.call(72,2,1), mock.call(72,0,1)]


def test_business_export_holds_runtime_identity_guard(tmp_path):
    from contextlib import contextmanager
    from src import web

    @contextmanager
    def busy_guard(_root):
        yield False

    handler = object.__new__(web.Handler)
    handler.app = mock.Mock(root=tmp_path)
    handler._json = lambda body, status=200: (status, body)
    with mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True), \
         mock.patch.object(runtime_guard, "guard", busy_guard), \
         mock.patch.object(web.Handler, "_api_unlocked",
                           side_effect=AssertionError("export bypassed identity lock")):
        status, body = handler._api("POST", "/api/film/export", {})

    assert status == 409
    assert "身份" in body["error"]


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/erp"),
    ("POST", "/api/erp/login"),
    ("POST", "/api/erp/login/captcha"),
    ("POST", "/api/store-account"),
    ("POST", "/api/session/ping"),
    ("POST", "/api/session/auto"),
    ("PUT", "/api/hwlogin"),
])
def test_identity_credential_and_capture_writes_hold_runtime_guard(
        tmp_path, method, path):
    from contextlib import contextmanager
    from src import web

    @contextmanager
    def busy_guard(_root):
        yield False

    handler = object.__new__(web.Handler)
    handler.app = mock.Mock(root=tmp_path)
    handler.app._cashier_import_lock.locked.return_value = False
    handler.app._cashier_policy_lock.locked.return_value = False
    handler._json = lambda body, status=200: (status, body)
    with mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True), \
         mock.patch.object(runtime_guard, "guard", busy_guard), \
         mock.patch.object(web.manager, "current", return_value=None), \
         mock.patch.object(web.timer, "current", return_value=None), \
         mock.patch.object(web.capture_job, "running", False), \
         mock.patch.object(web.Handler, "_api_unlocked",
                           side_effect=AssertionError("identity write bypassed identity lock")):
        status, body = handler._api(method, path, {})

    assert status == 409
    assert "身份" in body["error"]


@pytest.mark.parametrize("method,path", [
    ("POST", "/api/erp"),
    ("POST", "/api/erp/login"),
    ("POST", "/api/erp/login/captcha"),
    ("POST", "/api/store-account"),
    ("POST", "/api/session"),
    ("POST", "/api/session/ping"),
    ("POST", "/api/session/auto"),
    ("PUT", "/api/config"),
    ("PUT", "/api/hwlogin"),
])
def test_identity_writes_recheck_scope_after_acquiring_runtime_guard(
        tmp_path, method, path):
    from contextlib import contextmanager
    from src import web

    @contextmanager
    def acquired_guard(_root):
        yield True

    handler = object.__new__(web.Handler)
    handler.app = mock.Mock(root=tmp_path)
    handler.app._cashier_import_lock.locked.return_value = False
    handler.app._cashier_policy_lock.locked.return_value = False
    handler._json = lambda body, status=200: (status, body)
    with mock.patch.object(web, "lifehall_gone", return_value=False), \
         mock.patch.object(web.runtime, "api_available", return_value=True), \
         mock.patch.object(runtime_guard, "guard", acquired_guard), \
         mock.patch.object(web.manager, "current", return_value=None), \
         mock.patch.object(web.timer, "current", return_value=None), \
         mock.patch.object(web.capture_job, "running", False), \
         mock.patch.object(web, "role_scope",
                           side_effect=AssertionError("identity write reused stale scope")), \
         mock.patch.object(web.Handler, "_api_unlocked",
                           return_value=(200, {"ok": True})) as dispatch:
        status, body = handler._api(method, path, {})

    assert status == 200 and body["ok"]
    dispatch.assert_called_once_with(method, path, {})
