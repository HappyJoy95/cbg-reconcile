"""权益领取页面归属与读取生命周期回归。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.features import registry


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def test_claim_page_has_its_own_registered_lifecycle_and_read_abort():
    page = WEB / "features" / "tools" / "claim" / "page.js"
    assert page.is_file(), "权益领取页面逻辑应归入自己的业务目录"
    source = page.read_text(encoding="utf-8")
    assert "registerPage('claim-pending'" in source
    assert "mountClaimPendingPage" in source
    assert "unmountClaimPendingPage" in source
    assert "signal: controller.signal" in source


def test_claim_page_script_loads_before_app_and_leaves_legacy_loader():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    tag = re.search(
        r'<script src="(/features/tools/claim/page\.js\?v=\d+)"></script>', html)
    assert tag, "index.html 应明确加载权益领取页面脚本"
    assert html.index(tag.group(0)) < html.index('<script src="/app.js?v=')
    assert "async function loadClaimPending" not in app
    assert "function bindClaimOnlineModal" not in app
    assert "function runClaimBatch" not in app


def test_claim_operations_remain_open_to_store_manager_and_platform():
    permissions = registry.page_perms()["claim-pending"]["ops"]
    expected = frozenset(("store", "manager", "platform"))
    assert permissions == {op: expected for op in ("view", "enter", "modify")}


def test_leaving_claim_page_aborts_pending_read_and_ignores_late_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node 未安装，跳过前端生命周期检查")
    script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
let page;
let finish;
let requestOptions;
const element = { addEventListener() {}, querySelectorAll() { return []; } };
const context = {
  AbortController,
  Set,
  registerPage: (key, lifecycle) => {
    assert.equal(key, 'claim-pending'); page = lifecycle;
  },
  $: () => element,
  $$: () => [],
  api: (_path, options) => {
    requestOptions = options;
    return new Promise((resolve) => { finish = resolve; });
  },
};
vm.runInNewContext(source, context);
page.mount();
(async () => {
  const pending = vm.runInContext("claimRead('/api/claim/pending')", context);
  assert(requestOptions.signal);
  page.unmount();
  assert.equal(requestOptions.signal.aborted, true);
  finish({ rows: [{ store: 'late response' }] });
  assert.equal(await pending, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    page = WEB / "features" / "tools" / "claim" / "page.js"
    result = subprocess.run([node, "-e", script, str(page)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
