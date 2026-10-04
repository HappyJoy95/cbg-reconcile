# -*- coding: utf-8 -*-
"""统一进入流程（2026-10-02，开发目标 §4.5）—— 选择页判据、三条入口、平台确认。

钉三件事：

1. **选择页只给该给的人看**：没选过 + 没就绪才 `needed`；老机器（已就绪）、
   选过的、生活馆版（EDITION）一律不露 —— 打断既有机器是这功能最坏的失败方式；
2. **三条入口各自的就绪判据在后端**：授权店 = 原有两步；平台岗 = ERP + **显式确认**
   （确认时后端再验账号）；生活馆 = **只认门店编码**，不要求 ERP 登录；
3. **前端只照 `need` 渲染**（三张卡 / 确认步 / 编码步），不自己猜状态。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import edition, web                                     # noqa: E402

INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = "\n".join((ROOT / "web" / _p).read_text(encoding="utf-8")
                   for _p in ("common/base.js", "common/nav.js", "app.js"))


def _without_edition_env():
    return {k: v for k, v in __import__("os").environ.items()
            if k != "CBG_EDITION"}


def _write_store(root: Path, text: str) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    cfg = root / "config" / "store-X.yaml"
    cfg.write_text(text, encoding="utf-8")
    return cfg


class _AppCase(unittest.TestCase):
    """不起 HTTP，直接喂 `web.App`（setup_state / entry_state 的单测层）。"""

    STORE_YAML = 'erp_store_name: "青岛城阳万达店"\n'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg_path = _write_store(self.root, self.STORE_YAML)
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(self.root, "config/store-X.yaml")
        self.app.server = None

    def reload_app(self):
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(self.root, "config/store-X.yaml")
        self.app.server = None


class Test选择页判据(_AppCase):
    def test_新装机器要露脸(self):
        """没选过 + setup 没就绪（没 token、名单里也没这家店）⇒ needed。"""
        st = web.entry_state(self.app)
        self.assertEqual(st["kind"], "")
        self.assertTrue(st["needed"], "新装机器必须先选入口")

    def test_已就绪的老机器不露脸(self):
        """升级上来的机器（已经能用、没选过入口）⇒ 绝对不许被打断。"""
        with mock.patch.object(web, "setup_state",
                               lambda app: {"ready": True, "need": ""}):
            st = web.entry_state(self.app)
        self.assertFalse(st["needed"], "老机器见不到选择页")

    def test_选过就不露脸(self):
        web.save_entry(self.app, "erp")
        st = web.entry_state(self.app)
        self.assertEqual(st["kind"], "erp")
        self.assertFalse(st["needed"])

    def test_生活馆版永远不露脸(self):
        with mock.patch.dict(__import__("os").environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            st = web.entry_state(self.app)
        edition.reload()
        self.assertFalse(st["needed"])
        self.assertTrue(st["lifehall_edition"])
        self.assertEqual(st["kind"], "lifehall")

    def test_坏文件当没选(self):
        f = web.entry_file(self.app)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(web.entry_info(self.app)["kind"], "")
        web.clear_entry(self.app)
        self.assertFalse(f.exists())


class Test三条入口的就绪判据(_AppCase):
    def test_生活馆_没有编码就没就绪(self):
        _write_store(self.root, 'erp_store_name: "青岛城阳万达店"\n')
        self.reload_app()
        web.save_entry(self.app, "lifehall")
        st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "storecode")
        self.assertEqual(st["entry_kind"], "lifehall")

    def test_生活馆_存了编码就进_不要求ERP(self):
        """用户草案：「输入并保存门店编码，直接进入」—— 没 token 也放行。"""
        _write_store(self.root, 'erp_store_name: "青岛城阳万达店"\n'
                                'store_code: "SCN999999"\n')
        self.reload_app()
        web.save_entry(self.app, "lifehall")
        st = web.setup_state(self.app)
        self.assertTrue(st["ready"], st)
        self.assertEqual(st["need"], "")
        # 「不要求 ERP **登录**」—— 没 token 也放行（识别出店名是配置给的，
        # 跟登没登录无关；这里钉的是"没登录也能进"）
        self.assertFalse(st["erp"]["has_token"], "没有登录过云商也放行")

    def test_平台岗_差确认时need_confirm(self):
        _write_store(self.root, 'erp_store_name: "平台岗"\n')
        self.reload_app()
        web.save_entry(self.app, "platform")
        st = web.setup_state(self.app)
        self.assertEqual(st["need"], "confirm")
        self.assertFalse(st["ready"])
        self.assertTrue(st.get("entry_confirm"))

    def test_平台岗_确认后就绪(self):
        _write_store(self.root, 'erp_store_name: "平台岗"\n')
        self.reload_app()
        web.save_entry(self.app, "platform", confirmed=True)
        st = web.setup_state(self.app)
        self.assertTrue(st["ready"], st)
        self.assertEqual(st["need"], "")

    def test_平台岗_账号不是平台被拦下(self):
        """选了平台岗但这台机器认出来是普通门店 ⇒ 拦 + entry_mismatch 提示。"""
        web.save_entry(self.app, "platform")
        with mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"has_token": True, "username": "sl"}):
            st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertTrue(st.get("entry_mismatch"))
        self.assertEqual(st["need"], "erp")

    def test_平台账号不能从授权店入口继承全量身份(self):
        web.save_entry(self.app, "erp")
        profile = {"platform": True, "show_all": True, "type": "platform",
                   "erp_name": "平台岗", "huawei_code": "", "needs_linglong": False}
        with mock.patch.object(web.config_io, "store_profile", return_value=profile), \
             mock.patch.object(web, "describe_store_credentials",
                               return_value={"has_token": True, "username": "platform"}):
            st = web.setup_state(self.app)
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "entry-mismatch")
        self.assertEqual(st.get("entry_mismatch_kind"), "platform-on-erp")

    def test_授权店入口不改变原有判据(self):
        web.save_entry(self.app, "erp")
        with mock.patch.object(web, "setup_state", wraps=web.setup_state):
            st = web.setup_state(self.app)
        self.assertEqual(st["entry_kind"], "erp")
        # 没 token、没就绪 —— 跟没选入口时一致
        self.assertFalse(st["ready"])
        self.assertEqual(st["need"], "erp")


class Test运行身份与入口一致(_AppCase):
    def test_生活馆入口不继承旧平台或区长权限(self):
        """full 安装选择生活馆后，旧 ERP 账号画像不得继续决定角色或范围。"""
        _write_store(self.root, 'erp_store_name: "青岛城阳万达店"\n'
                                'store_code: "SCN999999"\n')
        self.reload_app()
        web.save_entry(self.app, "lifehall")
        with mock.patch.object(web.App, "_profile_with_who", lambda self, cfg: {
                "who": "平台岗", "kind": "platform", "platform": True,
                "show_all": True, "type": "platform", "erp_name": "平台岗"}), \
             mock.patch.object(web, "describe_store_credentials",
                               lambda *a, **k: {"username": "district-manager"}), \
             mock.patch.object(web.config_io, "managers_table", lambda root: [{
                 "name": "区长", "accounts": ["district-manager"],
                 "stores": ["青岛其他门店"], "region": "西北区"}]), \
             mock.patch.object(web.config_io, "stores_table", lambda root: [
                 {"erp_name": "青岛城阳万达店", "tdoc_name": "城阳万达"},
                 {"erp_name": "青岛其他门店", "tdoc_name": "其他门店"}]), \
             mock.patch.object(web.config_io, "store_profile", lambda values, root: {
                 "erp_name": "平台岗", "kind": "平台岗", "platform": True,
                 "needs_linglong": False}):
            scope = web.role_scope(self.app)
        self.assertEqual(scope["role"], web.ROLE_STORE)
        self.assertEqual(scope["stores"], {"青岛城阳万达店"})
        self.assertNotIn("青岛其他门店", scope["stores"])
        self.assertEqual(scope["entry_kind"], "lifehall")
        self.assertEqual(scope["who"], "")
        self.assertEqual(scope["account"], "")
        self.assertEqual(scope["kind"], "")
        self.assertTrue(scope["needs_linglong"])


class _Server:
    """真起 HTTP —— 门禁 / SETUP_ALLOW / store-code 那道 404 都要走真的。"""

    def __init__(self, root: Path):
        _write_store(root, 'erp_store_name: "青岛城阳万达店"\n')
        with mock.patch.object(web.service, "find_running", lambda *a, **k: None):
            self.app = web.App(root, "config/store-X.yaml")
        self.app.server = None
        web.Handler.app = self.app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def request(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=20)
        payload = json.dumps(body) if body is not None else None
        c.request(method, path, body=payload,
                  headers={"Content-Type": "application/json"} if payload else {})
        r = c.getresponse()
        raw = r.read().decode("utf-8")
        c.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Test接口与门禁(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.srv = _Server(self.root)
        self.addCleanup(self.srv.close)

    def test_选择页接口在登录门禁白名单里(self):
        """没选入口的机器 setup 必然没就绪 —— /api/entry 还得 200，否则永远选不了。"""
        st, d = self.srv.request("GET", "/api/setup")
        self.assertEqual(st, 200)
        self.assertFalse(d["ready"])
        st, d = self.srv.request("GET", "/api/entry")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["needed"])

    def test_存取删(self):
        st, d = self.srv.request("POST", "/api/entry", {"kind": "erp"})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/entry")
        self.assertEqual(d["kind"], "erp")
        self.assertFalse(d["needed"])
        st, d = self.srv.request("DELETE", "/api/entry")
        self.assertTrue(d["cleared"])
        st, d = self.srv.request("GET", "/api/entry")
        self.assertEqual(d["kind"], "")

    def test_不认识的入口给400(self):
        st, d = self.srv.request("POST", "/api/entry", {"kind": "god"})
        self.assertEqual(st, 400)
        self.assertIn("不认识", d["error"])

    def test_平台确认_账号不对给400(self):
        st, _ = self.srv.request("POST", "/api/entry", {"kind": "platform"})
        self.assertEqual(st, 200)                        # 选卡总是能存
        st, d = self.srv.request("POST", "/api/entry",
                                 {"kind": "platform", "confirm": 1})
        self.assertEqual(st, 400, d)
        self.assertIn("不是平台岗", d["error"])

    def test_store_code门_没选生活馆入口时404(self):
        """full 版默认没有手输编码这一步 —— 选了生活馆入口才开。"""
        st, d = self.srv.request("POST", "/api/session/store-code",
                                 {"store_code": "SCN328987"})
        self.assertEqual(st, 404, d)

    def test_生活馆入口端到端_编码即进入(self):
        st, d = self.srv.request("POST", "/api/entry", {"kind": "lifehall"})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("POST", "/api/entry",
                                 {"kind": "lifehall", "store_code": "自填店码"})
        self.assertEqual(st, 200, d)
        st, d = self.srv.request("GET", "/api/setup")
        self.assertEqual(st, 200)
        self.assertTrue(d["ready"], d)
        self.assertEqual(d["need"], "")
        self.assertEqual(d["entry_kind"], "lifehall")

    def test_入口存编码不认店不挪会话_两种安装方式(self):
        from src import store_identity
        import os
        for installed in ("full", "lifehall"):
            with self.subTest(installed=installed):
                with mock.patch.dict(os.environ, {"CBG_EDITION": installed}):
                    edition.reload()
                    original = self.srv.app.session_path()
                    original.parent.mkdir(parents=True, exist_ok=True)
                    original.write_text('old session', encoding="utf-8")
                    with mock.patch.object(store_identity, "set_store_code",
                                           side_effect=AssertionError("入口不做玲珑认店")), \
                            mock.patch.object(self.srv.app, "session_info",
                                              side_effect=AssertionError("入口不查会话")):
                        st, d = self.srv.request("POST", "/api/entry",
                            {"kind": "lifehall", "store_code": "  a  "})
                    self.assertEqual(st, 200, d)
                    self.assertEqual(web.config_io.load_raw(self.srv.app.config_path)["store_code"], "a")
                    self.assertEqual(original.read_text(encoding="utf-8"), "old session")
                    if original != self.srv.app.session_path():
                        self.assertFalse(self.srv.app.session_path().exists())
                    self.assertEqual(web.config_io.load_raw(self.srv.app.config_path)["erp_store_name"], "青岛城阳万达店")
            edition.reload()

    def test_空编码不保存不放行(self):
        st, d = self.srv.request("POST", "/api/entry",
                                 {"kind": "lifehall", "store_code": "   "})
        self.assertEqual(st, 400, d)
        self.assertNotIn("store_code", web.config_io.load_raw(self.srv.app.config_path))
        self.assertEqual(web.entry_info(self.srv.app)["kind"], "")

    def test_独立生活馆版不能切成ERP或平台入口(self):
        import os
        with mock.patch.dict(os.environ, {"CBG_EDITION": "lifehall"}):
            edition.reload()
            for kind in ("erp", "platform"):
                st, d = self.srv.request("POST", "/api/entry", {"kind": kind})
                self.assertEqual(st, 400, d)
            self.assertEqual(web.entry_info(self.srv.app)["kind"], "")
        edition.reload()


class Test前端钉子(unittest.TestCase):
    def test_两版编码门禁和提交实际执行(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 未安装，跳过前端执行检查")
        script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const entry = source.slice(source.indexOf('function showEntry(opts)'),
                          source.indexOf('/** 平台岗的「确认」'));
const gate = source.slice(source.indexOf('async function checkSetup()'),
                          source.indexOf('/* ⚠ 原来这里有个「我已登录完'));
const reauth = source.slice(source.indexOf('async function openSetup()'),
                            source.indexOf('/** 按**后端下发的身份'));
(async () => {
  for (const installed of [false, true]) {
    const nodes = {};
    const calls = [];
    let authOpened = 0;
    const get = (id) => nodes[id] ||= { hidden: true, value: '', textContent: '',
      classList: { add(){}, remove(){} },
      addEventListener(kind, handler) { this[kind] = handler; } };
    let state = { lifehall: installed, entry_kind: 'lifehall', ready: false,
                  need: 'storecode', profile: {} };
    const context = {
      $: get, setupState: null,
      document: { body: { classList: { add(){}, remove(){}, toggle(){} } } },
      syncLifehallSettings(){}, renderStoreCodeForms(){},
      hideSetup(){ get('#setup-mask').hidden = true; },
      showSetup(){ authOpened++; },
      toast(){},
      loadBrowserInfo(){}, loadHwLogin(){}, switchTab(){}, loadOverview(){},
      openFirstAvailablePage(){},
      async api(path, opts) {
        calls.push({ path, opts });
        if (path === '/api/setup') return state;
        assert.equal(path, '/api/entry');
        assert.equal(opts.body.kind, 'lifehall');
        assert.equal(opts.body.store_code, '自填店码');
        state = { ...state, ready: true, need: '' };
        return { ok: true };
      }
    };
    vm.createContext(context);
    vm.runInContext(entry + '\n' + gate + '\n' + reauth, context);
    assert.equal(await context.checkSetup(), false);
    assert.equal(get('#entry-mask').hidden, false);
    assert.equal(get('#entry-code').hidden, false);
    assert.equal(get('#entry-options').hidden, true);
    assert.equal(get('#btn-entry-code-back').hidden, installed);
    assert.equal(authOpened, 0);
    get('#entry-store-code').value = '  自填店码  ';
    await get('#btn-entry-code').click({ currentTarget: get('#btn-entry-code') });
    assert.equal(get('#entry-mask').hidden, true);
    assert.equal(calls.filter(c => c.path === '/api/entry').length, 1);
    assert.equal(get('#btn-entry-code').disabled, false);
    assert.equal(authOpened, 0);
    assert(calls.every(c => ['/api/entry', '/api/setup'].includes(c.path)));
  }
})().catch(e => { console.error(e); process.exit(1); });
"""
        result = subprocess.run([node, "-e", script, str(ROOT / "web" / "app.js")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_生活馆缺编码回编码页_不再显示授权登录门(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 未安装，跳过前端执行检查")
        script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const setup = source.slice(source.indexOf('function showSetup(st)'),
                          source.indexOf('// 「跳过玲珑，先看界面」'));
const entry = source.slice(source.indexOf('function showEntry(opts)'),
                          source.indexOf('function hideEntry()'));
const hide = source.slice(source.indexOf('function hideSetup()'),
                         source.indexOf('/** 问后端「登录好了没」'));
(() => {
  const nodes = {};
  const classes = new Set();
  const get = (id) => nodes[id] ||= { hidden: true, value: '', textContent: '',
    classList: { add(){}, remove(){}, toggle(){} } };
  const context = {
    $: get, setupState: { entry_kind: 'lifehall', ready: false, need: 'storecode' },
    document: { body: { classList: { add(v){classes.add(v);},
      remove(v){classes.delete(v);} } } },
    hideSetup(){ get('#setup-mask').hidden = true; },
    syncLifehallSettings(){}, renderStoreCodeForms(){}, mountLinglong(){}, esc(v){return v;}
  };
  vm.createContext(context);
  vm.runInContext(entry + '\n' + hide + '\n' + setup, context);
  context.showSetup(context.setupState);
  assert.equal(get('#setup-mask').hidden, true);
  assert.equal(get('#entry-mask').hidden, false);
  assert.equal(get('#entry-code').hidden, false);
  assert.equal(classes.has('setup-locked'), true);
})()
"""
        result = subprocess.run([node, "-e", script, str(ROOT / "web" / "app.js")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('id="btn-setup-return"', INDEX_HTML)

    def test_生活馆重新登录不再打开旧登录门(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node 未安装，跳过前端执行检查")
        script = r"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const setup = source.slice(source.indexOf('function showSetup(st)'),
                           source.indexOf('// 「跳过玲珑，先看界面」'));
const entry = source.slice(source.indexOf('function showEntry(opts)'),
                           source.indexOf('function hideEntry()'));
const hide = source.slice(source.indexOf('function hideSetup()'),
                          source.indexOf('/** 问后端「登录好了没」'));
const reauth = source.slice(source.indexOf('async function openSetup()'),
                            source.indexOf('/** 按**后端下发的身份'));
(async () => {
  const nodes = {};
  const calls = [];
  let state = { entry_kind: 'lifehall', ready: true, need: '' };
  const get = (id) => nodes[id] ||= { hidden: true, value: '', textContent: '',
    classList: { add(){}, remove(){}, toggle(){} },
    addEventListener(kind, handler) { this[kind] = handler; } };
  const context = {
    $: get, setupState: { entry_kind: 'lifehall', ready: true },
    api: async (path) => { calls.push(path); return state; },
    mountLinglong(){}, syncLifehallSettings(){}, renderStoreCodeForms(){},
    hideEntry(){ get('#entry-mask').hidden = true; },
    esc(v){ return v; },
    switchTab(tab){ calls.push('tab:' + tab); }, toast(){},
    document: { body: { classList: { add(){}, remove(){} } } }
  };
  vm.createContext(context);
  vm.runInContext(entry + '\n' + hide + '\n' + setup + '\n' + reauth, context);
  await context.openSetup();
  assert.equal(get('#setup-mask').hidden, true);
  assert.equal(get('#entry-mask').hidden, true);
  assert(calls.includes('tab:tools'));
  assert.deepEqual(calls.filter(x => x === '/api/setup'), ['/api/setup']);

  // 编码若已被清除，刷新状态后只回到编码表单，不开玲珑登录门。
  state = { entry_kind: 'lifehall', ready: false, need: 'storecode' };
  await context.openSetup();
  assert.equal(get('#setup-mask').hidden, true);
  assert.equal(get('#entry-mask').hidden, false);
  assert.equal(get('#entry-code').hidden, false);
  assert.deepEqual(calls.filter(x => x === '/api/setup'), ['/api/setup', '/api/setup']);
})().catch(e => { console.error(e); process.exit(1); });
""";
        result = subprocess.run([node, "-e", script, str(ROOT / "web" / "app.js")],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_选择页三张卡在HTML里(self):
        for i in ("entry-mask", "entry-erp", "entry-platform", "entry-lifehall",
                  "entry-code", "btn-entry-code", "entry-store-code"):
            self.assertIn('id="%s"' % i, INDEX_HTML, i)

    def test_登录门用底部换入口代替退出(self):
        erp_start = INDEX_HTML.index('<section class="setup-step" id="setup-step-erp">')
        erp_end = INDEX_HTML.index('id="setup-step-linglong"', erp_start)
        erp_step = INDEX_HTML[erp_start:erp_end]
        self.assertIn('id="btn-sa-save"', erp_step)
        self.assertNotIn('id="btn-sa-quit"', erp_step)

        switch = INDEX_HTML.index('id="btn-entry-switch"')
        foot_start = INDEX_HTML.rfind('<div class="setup-foot">', 0, switch)
        foot_end = INDEX_HTML.index('</div>', switch) + len('</div>')
        self.assertGreaterEqual(foot_start, 0)
        self.assertIn('换进入方式', INDEX_HTML[foot_start:foot_end])

    def test_确认步与换入口在登录页里(self):
        self.assertIn('id="setup-step-confirm"', INDEX_HTML)
        self.assertIn('id="btn-entry-confirm"', INDEX_HTML)
        self.assertIn('id="btn-entry-switch"', INDEX_HTML)
        self.assertIn('id="setup-entry-mismatch"', INDEX_HTML)

    def test_前端按need渲染不自己猜(self):
        for frag in ("entryState()", "chooseEntry(", "need === 'confirm'",
                     "need === 'entry-mismatch'", "platform-on-erp",
                     "entry_kind === 'lifehall'", "setup-entry-mismatch"):
            self.assertIn(frag, APP_JS, frag)

    def test_选择页排在门禁前(self):
        i_ent = APP_JS.index("const ent = await entryState()")
        i_gate = APP_JS.index("**门禁在最前面**")   # 启动 IIFE 里那句注释（唯一）
        self.assertLess(i_ent, i_gate, "选择页必须比门禁先问，否则新装机器先撞登录页")


if __name__ == "__main__":
    unittest.main()
