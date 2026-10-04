"""收银页面归属与生命周期回归。"""

import re
import shutil
import subprocess
from pathlib import Path

from src.features import registry


ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def test_cashier_page_owns_its_registered_lifecycle_and_cancellable_reads():
    page = WEB / "features" / "cashier" / "page.js"
    assert page.is_file(), "收银页面逻辑应归入自己的业务目录"
    source = page.read_text(encoding="utf-8")
    assert "registerPage('cashier'" in source
    assert "mountCashierPage" in source
    assert "unmountCashierPage" in source
    assert "signal: controller.signal" in source


def test_cashier_page_script_loads_before_app_and_leaves_legacy_loader():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    app = (WEB / "app.js").read_text(encoding="utf-8")
    tag = re.search(r'<script src="(/features/cashier/page\.js\?v=\d+)"></script>', html)
    assert tag, "index.html 应明确加载收银页面脚本"
    assert html.index(tag.group(0)) < html.index('<script src="/app.js?v=')
    assert "function cashierResetForm" not in app
    assert "async function loadCashier" not in app
    assert "function bindCashierEvents" not in app


def test_cashier_permission_declaration_remains_store_only():
    page = registry.page_perms()["cashier"]
    assert page["ops"] == {
        "view": frozenset(("store",)),
        "enter": frozenset(("store",)),
        "modify": frozenset(("store",)),
        "export": frozenset(("store",)),
    }


def test_leaving_cashier_page_aborts_pending_read_and_ignores_late_result():
    node = shutil.which("node")
    if not node:
        return
    script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
let page;
let finish;
let requestOptions;
const context = {
  AbortController,
  Set,
  registerPage: (key, lifecycle) => { assert.equal(key, 'cashier'); page = lifecycle; },
  $: () => ({ addEventListener() {} }),
  api: (_path, options) => {
    requestOptions = options;
    return new Promise((resolve) => { finish = resolve; });
  },
};
vm.runInNewContext(source, context);
page.mount();
(async () => {
  const pending = vm.runInContext("cashierRead('/api/cashier/entries')", context);
  assert(requestOptions.signal);
  page.unmount();
  assert.equal(requestOptions.signal.aborted, true);
  finish({ rows: [{ store: 'late response' }] });
  assert.equal(await pending, null);
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
    page = WEB / "features" / "cashier" / "page.js"
    result = subprocess.run([node, "-e", script, str(page)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
