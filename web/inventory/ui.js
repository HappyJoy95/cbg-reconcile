/* 库存盘点 - 界面逻辑
 * 依赖 IC.core / IC.api / IC.xlsx
 *
 * ⚠ 2026-09-20 接进 cbg-reconcile（M16）时改过三处，**别照抄上游那份**：
 *   1. `IC.erp` → **`IC.api`**：取数不再由浏览器直连云商，改由本项目后端
 *      （`/api/inventory/*`）去拉 —— 所以这一页里**没有任何 token / 密码**；
 *   2. 新增「导出并推送」`exportAndPush()`：把**同一份** xlsx POST 给后端推出去
 *      （落盘 + 邮件带附件 + 企微汇总）；
 *   3. 登录那一整块（账号密码 / 验证码 / 自动续期）删了 ——
 *      账号在控制台「通用设置 › 云商账号」里配，这一页只显示"后端认到的是谁"。
 *
 * ⚠ 口径一行没动：`core.js` / `store.js` / `xlsx.js` 是原样搬来的（上游 `npm test` 那
 *   521 条断言验的就是它们）。**要改它们请回上游改、跑完测试再拷回来** —— 见 AGENTS.md。
 */
globalThis.IC = globalThis.IC || {};
(function () {
  const IC = globalThis.IC;
  const core = IC.core;
  const api = IC.api; // ⚠ 是 api 不是 erp：它调的是**本项目后端**
  const xlsx = IC.xlsx;

  const LS_CFG = 'ic.cfg.v2'; // 现在只存界面偏好（声音/索引/在途）—— 凭据一律不进浏览器
  const LS_CFG_OLD = 'ic.cfg.v1';
  const LS_SESSION = 'ic.session.v1';

  const $ = (s) => document.querySelector(s);
  const esc = (v) =>
    String(v === null || v === undefined ? '' : v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');

  /* ---------------- 状态 ---------------- */
  const state = {
    cfg: Object.assign({}, api.DEFAULT_CFG),
    warehouses: [],
    date: core.todayStr(),
    storeId: '',
    storeName: '',
    st: null,
    tab: 'missing',
    lastResult: null,
    busy: false,
    sound: true,
    loadGlobalIndex: true,
    loadInTransit: true, // 拉取「在途（待入库）」
    inTransitInfo: null, // {fetched, kept, droppedByStore, error}
    giStatus: 'idle', // idle | loading | ready | error
    giCount: 0,
    giError: '',
    search: {},
    showAll: {},
    lastScanKey: '',
    lastScanAt: 0,
    stats: null,
    // 后端身份（`api.ready()` 填进来）—— 这一页**自己不再登录**
    identity: null, // {ok, username, company, builtin, store:{erp_name,…}, why}
    loginStatus: '', // 用来在顶部/设置区显示"后端认到的是谁、有没有问题"
    loginStatusKind: '', // ok | err | warn | loading
  };

  /* ---------------- 本地存储 ---------------- */
  function lsGet(key) {
    try {
      return JSON.parse(localStorage.getItem(key) || 'null');
    } catch (e) {
      return null;
    }
  }
  function lsSet(key, val) {
    try {
      localStorage.setItem(key, JSON.stringify(val));
      return true;
    } catch (e) {
      return false;
    }
  }

  const store = IC.store;

  /**
   * 立即把会话状态写盘（不再延迟）。
   * 延迟保存会让最后几笔在"快速关闭页面"时丢掉，代价是每次操作多一次小写入——
   * 账面是大对象、单独存在 book 键里，状态很小，所以这里同步写没有性能问题。
   */
  function saveSession() {
    if (!state.st) return true;
    if (pendingSaveTimer) {
      clearTimeout(pendingSaveTimer);
      pendingSaveTimer = null;
    }
    try {
      store.saveState(state.st.toJSON());
      setSaveStatus('saved');
      return true;
    } catch (e) {
      setSaveStatus('error', e);
      return false;
    }
  }

  let pendingSaveTimer = null;
  /** 清空会话时要把没执行的保存任务取消掉，否则旧任务会把数据又写回来 */
  function cancelPendingSave() {
    if (pendingSaveTimer) {
      clearTimeout(pendingSaveTimer);
      pendingSaveTimer = null;
    }
  }

  function setSaveStatus(kind, err) {
    const el = $('#save-status');
    if (!el) return;
    if (kind === 'idle') {
      el.className = 'save-status';
      el.textContent = '';
      el.title = '';
      const b = $('#btn-backup');
      if (b) b.classList.remove('need');
      return;
    }
    if (kind === 'saved') {
      el.className = 'save-status ok';
      el.textContent = '已保存 ' + core.fmtTime(Date.now()).slice(11);
      el.title = '盘点进度已写入本机浏览器';
    } else if (kind === 'saving') {
      el.className = 'save-status';
      el.textContent = '保存中…';
    } else {
      const quota = err && err.storage === 'quota';
      el.className = 'save-status err';
      el.textContent = quota ? '保存失败：本机存储空间不足' : '保存失败：' + ((err && err.message) || '未知原因');
      el.title = '进度没能写入本机。请点「下载会话备份」保存到文件，再关闭页面。';
      const btn = $('#btn-backup');
      if (btn) btn.classList.add('need');
    }
  }

  /** 会话备份：账面 + 状态 + 统计，存不下来时用它保命 */
  function downloadBackup() {
    if (!state.st) return;
    const book = store.loadBook();
    const payload = store.buildBackup({
      state: state.st.toJSON(),
      book: book,
      stats: state.st.stats(),
    });
    const name = store.safeName(`盘点会话备份_${state.st.storeName}_${state.st.date}_${core.fmtTime(Date.now()).replace(/[: ]/g, '-')}`) + '.json';
    xlsx.download(name, new TextEncoder().encode(JSON.stringify(payload, null, 1)), 'application/json');
    toast('已下载会话备份，可凭它找回本次盘点');
  }
  function saveCfg() {
    // ⚠ 只存**界面偏好**。凭据（token / 账号 / 密码 / 商家编码）**一个都不进浏览器** ——
    //   那些在控制台的 `.secrets/erp.env` 里，这一页要用时问后端。
    //   （上游那份是"页面自己登录、自己存 token"，接进控制台后那条路整个删了。）
    const c = {
      sound: state.sound,
      loadGlobalIndex: state.loadGlobalIndex,
      loadInTransit: state.loadInTransit,
    };
    const ok = lsSet(LS_CFG, c);
    if (!ok) setLoginStatus('界面偏好保存失败（浏览器存储不可用或已满），本次设置只在当前页面有效', 'err');
    return ok;
  }

  /* ---------------- 后端身份（这一页不再自己登录） ---------------- */

  function setLoginStatus(msg, kind) {
    state.loginStatus = msg || '';
    state.loginStatusKind = kind || '';
    const el = $('#login-status');
    if (!el) return;
    el.className = 'login-status ' + (kind || '');
    el.textContent = msg || '';
  }

  function requireLogin(msg) {
    openSettings(msg || '请先去控制台配云商账号', 'err');
  }

  /**
   * 问后端："这台机器的云商账号配好了吗、是哪家店"。
   * 成功时把身份写进 `state.identity`，并让 `api.isConfigured()` 变成 true。
   */
  async function reloadIdentity(opt) {
    opt = opt || {};
    if (!opt.silent) setLoginStatus('正在读取后端账号…', 'loading');
    let d = null;
    try {
      d = await api.ready();
    } catch (e) {
      d = { ok: false, why: '读不到后端接口：' + (e && e.message ? e.message : e) };
    }
    state.identity = d;
    state.cfg.ready = !!d.ok;
    state.cfg.companycode = core.s(d.company) || '';
    state.cfg.username = core.s(d.username) || '';
    state.cfg.erpName = core.s(d.store && d.store.erp_name) || '';
    const who = state.cfg.username || '（后端没给用户名）';
    if (d.ok) {
      setLoginStatus('云商账号：' + who + (state.cfg.erpName ? ' · ' + state.cfg.erpName : ''), 'ok');
    } else {
      setLoginStatus(d.why || '后端没配云商账号', 'err');
    }
    // ⚠ 那两个只读输入框（账号 / companycode）2026-09-21 删了 ——
    //   用户：「接口设置这个页面就不要了」。账号那一行现在就是 `#login-status` 这句。
    return d;
  }

  /**
   * 包住所有取数调用。
   *
   * ⚠ 以前这里要处理"token 失效 → 用本地账号密码自动重登"；现在**没有 token** ——
   *   后端（`ErpClient.call()`）自己会拿 `.secrets/erp.env` 那份账号重登一次。
   *   这一层只剩一件事：后端没配账号时**明确拦住并说清去哪儿配**。
   */
  async function withAuth(fn) {
    if (!api.isConfigured(state.cfg)) {
      requireLogin('这台机器还没配云商账号：去控制台「通用设置 › 云商账号」填一次，'
                 + '再回这一页点「重新读取」');
      const e = new Error('后端还没配云商账号，已阻止发送请求');
      e.notConfigured = true;
      throw e;
    }
    return await fn();
  }

  function openSettings(msg, kind) {
    const box = $('#setup');
    if (box) box.classList.remove('hidden');
    if (msg) setLoginStatus(msg, kind || 'err');
  }

  /* ---------------- 提示 / 加载 ---------------- */
  let toastTimer = null;
  function toast(msg, isErr) {
    const el = $('#inv-toast') || $('#toast');
    if (!el) return;
    el.textContent = msg;
    el.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (el.className = 'toast'), isErr ? 6000 : 2600);
  }

  function busy(msg, sub) {
    const el = $('#loading');
    $('#loading-msg').textContent = msg || '处理中…';
    $('#loading-sub').textContent = sub || '';
    el.classList.remove('hidden');
  }
  function unbusy() {
    $('#loading').classList.add('hidden');
  }

  /* ---------------- 声音 ---------------- */
  let actx = null;
  function beep(kind) {
    if (!state.sound) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      actx = actx || new AC();
      if (actx.state === 'suspended') actx.resume();
      const t = actx.currentTime;
      const seq =
        kind === 'ok'
          ? [[1180, 0.07, 0]]
          : kind === 'dup'
          ? [
              [780, 0.06, 0],
              [780, 0.06, 0.1],
            ]
          : [
              [340, 0.18, 0],
              [260, 0.22, 0.16],
            ];
      seq.forEach(([f, d, delay]) => {
        const o = actx.createOscillator();
        const g = actx.createGain();
        o.type = 'sine';
        o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t + delay);
        g.gain.exponentialRampToValueAtTime(0.25, t + delay + 0.012);
        g.gain.exponentialRampToValueAtTime(0.0001, t + delay + d);
        o.connect(g);
        g.connect(actx.destination);
        o.start(t + delay);
        o.stop(t + delay + d + 0.03);
      });
    } catch (e) {
      /* 忽略音频异常 */
    }
  }

  /* ---------------- 仓库列表 ---------------- */
  async function loadWarehouses() {
    busy('正在读取仓库列表…', '接口：/api/inventory/warehouses');
    try {
      const list = await withAuth(() => api.fetchWarehouses(state.cfg));
      state.warehouses = list;
      // 默认选中**本店那个仓**（后端按门店名单匹配出来的）——
      // 仓名跟门店名只差一个字，不默认选好很容易盘到隔壁店的货。
      // ⚠ 只在用户还没选过的时候预设（他手动换过就别抢回来）。
      if (!state.storeId && state.cfg.defaultStoreId) {
        state.storeId = String(state.cfg.defaultStoreId);
        state.storeName = state.cfg.defaultStoreName || '';
        const sel = $('#store');
        if (sel) sel.value = state.storeId;
      }
      renderStoreOptions();
      toast(`已读取 ${list.length} 个仓库`
            + (state.cfg.defaultStoreName ? `，默认选中本店仓：${state.cfg.defaultStoreName}` : ''));
    } catch (e) {
      if (!api.isTokenError(e)) toast(e.message, true);
    } finally {
      unbusy();
    }
  }

  function renderStoreOptions() {
    const filter = core.s($('#store-filter') ? $('#store-filter').value : '');
    const sel = $('#store');
    const cur = state.storeId || sel.value;
    const f = filter.toUpperCase();
    const opts = ['<option value="">请选择仓库…</option>'];
    state.warehouses.forEach((w) => {
      const label = `${w.name}${w.branchName && w.branchName !== w.name ? '（' + w.branchName + '）' : ''}`;
      if (f && label.toUpperCase().indexOf(f) < 0) return;
      opts.push(`<option value="${esc(w.id)}"${String(w.id) === String(cur) ? ' selected' : ''}>${esc(label)}</option>`);
    });
    sel.innerHTML = opts.join('');
    if (cur && !state.warehouses.some((w) => String(w.id) === String(cur))) sel.value = '';
  }

  /* ---------------- 拉取库存 ---------------- */
  function setSessionHeader() {
    if (!state.st) {
      $('#session-info').innerHTML = '<span class="pill">尚未开始盘点</span>';
      return;
    }
    const gi =
      state.giStatus === 'loading'
        ? '<span class="pill loading">全库索引 加载中…</span>'
        : state.giStatus === 'ready'
        ? `<span class="pill on">全库索引 ${state.giCount} 个串号</span>`
        : state.giStatus === 'error'
        ? `<span class="pill loading">全库索引 失败</span>`
        : '<span class="pill">全库索引 未加载</span>';
    const tr = state.inTransitInfo;
    const trPill = !tr
      ? ''
      : tr.error
      ? `<span class="pill loading" title="${esc(tr.error)}">在途未拉到</span>`
      : `<span class="pill${tr.kept ? ' on' : ''}" title="${esc(tr.source || '')}">在途待入库 ${tr.kept}</span>`;
    $('#session-info').innerHTML =
      `<span class="pill on">${esc(state.st.storeName)}</span>` +
      `<span class="pill">${esc(state.st.date)}</span>` +
      `<span class="pill">开始于 ${esc(core.fmtTime(state.st.startedAt))}</span>` +
      trPill +
      gi;
  }

  async function loadInventory(opt) {
    opt = opt || {};
    // 后端没配云商账号就不要谈选仓库，先把"去哪儿配"摆出来
    if (!api.isConfigured(state.cfg)) {
      requireLogin('这台机器还没配云商账号：去控制台「通用设置 › 云商账号」配一次，再回来拉库存');
      toast('这台机器还没配云商账号', true);
      return;
    }
    const date = core.s($('#date').value) || core.todayStr();
    const storeId = core.s($('#store').value);
    if (!storeId) {
      toast('请先选择仓库（门店）', true);
      return;
    }
    const wh = state.warehouses.find((w) => String(w.id) === storeId);
    const storeName = wh ? wh.name : storeId;

    if (state.st && !opt.keepScans) {
      const cur = state.st;
      if (cur.hasContent() && (cur.storeId !== storeId || cur.date !== date)) {
        // 别只看扫码：手工确认、实盘数量、备注同样是要丢的东西
        const parts = [];
        if (cur.scans.length) parts.push(`${cur.scans.length} 条扫码`);
        if (Object.keys(cur.manualFound).length) parts.push(`${Object.keys(cur.manualFound).length} 条手工确认`);
        if (Object.keys(cur.manualQty).length) parts.push(`${Object.keys(cur.manualQty).length} 条实盘数量`);
        if (Object.keys(cur.manualNotes).length) parts.push(`${Object.keys(cur.manualNotes).length} 条备注`);
        if (cur.pendingReview.length) parts.push(`${cur.pendingReview.length} 条待核实`);
        if (
          !confirm(
            `当前盘点（${cur.storeName} ${cur.date}）还有：${parts.join('、')}。\n` +
              `切换到「${storeName} ${date}」会把这些记录从本机清掉（可先点「下载会话备份」留存）。\n确定继续？`
          )
        )
          return;
      }
    }

    state.busy = true;
    busy('正在拉取库存…', `${storeName} · ${date}`);
    try {
      const res = await withAuth(() =>
        api.fetchInventory(state.cfg, {
          date: date,
          storeId: storeId,
          onProgress: (done, total) => busy('正在拉取库存…', `${storeName} · ${date} · ${done}/${total} 行`),
        })
      );
      let items = core.normalizeAll(res.rows, storeId);

      // 在途（待入库）：先从**同一张库存表**读（outCol 里的 ProCount_OnTransfer），
      // 一次请求、同一套完整性校验，最可信；读不到再用库存明细接口兜底。
      state.inTransitInfo = null;
      const fromBook = items.filter((i) => i.inTransit).length;
      if (fromBook > 0) {
        state.inTransitInfo = { fetched: fromBook, kept: fromBook, droppedByStore: 0, error: '', source: '库存表在途列' };
      } else if (state.loadInTransit) {
        busy('正在拉取在途（待入库）…', storeName + ' · ' + date);
        try {
          const tr = await withAuth(() =>
            api.fetchInTransit(state.cfg, {
              date: date,
              storeId: storeId,
              storeName: storeName,
              branchId: wh ? wh.branchId : '',
              branchName: wh ? wh.branchName : '',
            })
          );
          const merged = core.mergeInTransit(items, tr.rows, storeId, storeName);
          items = merged.items;
          state.inTransitInfo = {
            fetched: tr.fetched,
            kept: merged.added.length,
            droppedByStore: tr.droppedByStore,
            error: '',
            source: '库存明细接口（兜底）',
          };
        } catch (e) {
          // 这里不单独弹提示：紧接着还有一条「已拉取…」的汇总提示会把它顶掉，
          // 统一放到汇总提示里带上，保证用户一定看得到。
          state.inTransitInfo = { fetched: 0, kept: 0, droppedByStore: 0, error: e.message, source: '' };
        }
      }

      let st;
      if (opt.keepScans && state.st && state.st.storeId === storeId && state.st.date === date) {
        st = state.st;
        st.items = items;
        st.rebuildItemIndex();
      } else {
        st = new core.Stocktake({
          date: date,
          storeId: storeId,
          storeName: storeName,
          companycode: state.cfg.companycode,
          items: items,
          bookVersion: 1,
          bookFetchedAt: Date.now(),
        });
      }
      // 冻结账面：写一次，之后续盘、导出、离线都读它，不再回头拉 ERP
      const bookPayload = {
        sessionId: st.sessionId,
        companycode: state.cfg.companycode,
        storeId: storeId,
        storeName: storeName,
        date: date,
        version: st.bookVersion,
        fetchedAt: st.bookFetchedAt,
        items: core.bookFromItems(items),
      };
      try {
        store.saveBook(bookPayload);
      } catch (e) {
        setSaveStatus('error', e);
        toast('账面保存失败：' + e.message + '（盘点可以继续，但请尽快点「下载会话备份」）', true);
      }
      const prevGlobal = state.st && state.st.globalIndex;
      state.st = st;
      state.storeId = storeId;
      state.storeName = storeName;
      state.date = date;
      if (prevGlobal && st.globalIndex !== prevGlobal) st.setGlobalIndex(prevGlobal);
      state.tab = 'missing';
      state.showAll = {};
      state.search = {};
      // 换了会话/日期/商家，旧索引一律作废（含还在飞的请求）
      if (state.giToken && state.giToken !== indexToken(st)) {
        state.giStatus = 'idle';
        state.giCount = 0;
        state.giToken = '';
      }

      $('#setup').classList.add('hidden');
      $('#scan-card').classList.remove('hidden');
      $('#results-card').classList.remove('hidden');

      const s = st.stats();
      const ti = state.inTransitInfo;
      let msg = `已拉取 ${st.storeName}：${st.items.length} 行（有串号 ${s.shouldCount} 台 / 无串号 ${s.nonSerialRows} 行）`;
      let msgIsErr = false;
      if (ti) {
        if (ti.error) {
          msg += `；在途清单未拉到：${ti.error}（不影响在库盘点）`;
          msgIsErr = true;
        } else if (ti.kept) {
          msg += `；在途待入库 ${ti.kept} 台（来自${ti.source || '库存表'}）`;
          if (ti.droppedByStore) msg += `（另有 ${ti.droppedByStore} 台不属于本仓已过滤）`;
        } else {
          msg += '；本店没有在途（待入库）';
        }
      }
      toast(msg, msgIsErr);
      render();
      focusScan();
      saveSession();

      if (state.loadGlobalIndex && state.giStatus !== 'ready' && state.giStatus !== 'loading') {
        loadGlobalIndexBg();
      }
    } catch (e) {
      toast('拉取失败：' + e.message, true);
    } finally {
      state.busy = false;
      unbusy();
    }
  }

  /** 索引的目标身份：商家 + 盘点日期 + 会话号，三者任一变化，索引即失效 */
  function indexToken(st) {
    return [state.cfg.companycode, st.date, st.sessionId].join('|');
  }

  async function loadGlobalIndexBg() {
    if (!state.st) return;
    // 没登录就别试：只会多一条噪音错误提示，还会把刚弹的提示盖掉
    if (!api.isConfigured(state.cfg)) {
      state.giStatus = 'idle';
      setSessionHeader();
      return;
    }
    const st = state.st;
    const token = indexToken(st);
    const forSession = st.sessionId;
    state.giStatus = 'loading';
    state.giToken = token;
    state.giSessionId = forSession;
    setSessionHeader();
    renderScanCard();
    let map = null;
    let err = null;
    try {
      map = await withAuth(() => api.fetchGlobalIndex(state.cfg, { date: st.date }));
    } catch (e) {
      err = e;
    }
    // ★ 关键：请求返回时，如果会话/日期/商家已经变了，这份索引一律丢弃，
    //   否则旧会话的结果会污染新会话的「表外码归属」判断。
    const cur = state.st;
    if (!cur || indexToken(cur) !== token || cur.sessionId !== forSession) {
      return;
    }
    if (err) {
      state.giStatus = 'error';
      state.giError = err.message;
      if (!api.isTokenError(err)) {
        toast('全库索引加载失败：' + err.message + '（不影响盘点，表外码将只提示"归属尚未核实"）', true);
      }
    } else {
      cur.setGlobalIndex(map);
      state.giCount = map.size;
      state.giStatus = 'ready';
      state.giToken = token;
      toast(`全库串号索引已就绪：${map.size} 个串号（可识别非本店库存）`);
    }
    setSessionHeader();
    render();
  }

  /* ---------------- 扫码 ---------------- */
  function focusScan() {
    const el = $('#scan-input');
    if (el && !$('#scan-card').classList.contains('hidden')) {
      // ⚠ `preventScroll`（2026-09-21 跟"点确认跳回顶部"一起加的）：
      //   扫码框在**页面顶部**，而 `focus()` 默认会把它滚进视口。
      //   ⚠ **诚实记一笔**：实测那次跳的是表格容器（见 `render()` 上面那段），
      //     焦点这一半**没测出跳**（焦点确实给了扫码框，但页面没动）——
      //     所以这是**防御**，不是当期修复：别等哪天布局一变
      //     （扫码卡片挪出去、窗口更矮）再犯同一个毛病。
      //   ⚠ 它只影响"要不要滚"，**不影响焦点** —— 扫码枪照样连续扫。
      el.focus({ preventScroll: true });
      el.select();
    }
  }

  function handleScanInput(rawValue) {
    if (!state.st) return;
    const raw = core.s(rawValue);
    if (!raw) return;

    // 防抖：扫码枪偶发连发两次同一个码（两次间隔通常 <100ms）。
    // 窗口给到 600ms，慢机器上渲染耗时也不会漏判；人手重扫一般都在 1 秒以上，不受影响。
    const now = Date.now();
    const keyNow = core.normCode(raw);
    if (keyNow === state.lastScanKey && now - state.lastScanAt < 600) return;
    state.lastScanKey = keyNow;
    state.lastScanAt = now;

    // 支持一次粘贴多个码（用逗号/分号分隔）
    const parts = raw.split(/[,;，；\r\n]+/).map(core.s).filter(Boolean);
    let last = null;
    parts.forEach((p) => {
      last = state.st.scan(p, Date.now());
    });

    state.lastResult = last;
    beep(last.status === 'ok' ? 'ok' : last.status === 'dup' ? 'dup' : 'err');
    render();
    saveSession();
  }

  function undoLast() {
    if (!state.st || !state.st.scans.length) {
      toast('没有可撤销的记录');
      return;
    }
    const r = state.st.undo();
    state.lastResult = null;
    toast(`已撤销：${r ? r.code : ''}`);
    render();
    saveSession();
    focusScan();
  }

  /* ---------------- 渲染 ---------------- */

  /* ⚠⚠ 滚动位置要**自己兜**（2026-09-21 用户报的：
     「未扫到 / 无串号商品点击手动确认后会自动刷新到顶部」）。
     **真浏览器实测过**（`.dsh/tasks/verify-inv-scroll.py`）：
     跳的是**表格容器**的滚动（`.table-wrap` 的 scrollTop **260 → 0**），
     **不是**页面滚动（window 的 scrollY 一直是 260）—— 因为 `renderResults()`
     是整个 `body.innerHTML = …` **重建**，新造的那个 `.table-wrap` 理所当然从 0 开始。
     ⇒ 重建前后把**页面**和每个 `.table-wrap` 的滚动位置接回去。
     ⚠ 换回修复前那一版复现过：`wrap` 260 → **0**；接回去之后 260 → 260 ✓。
     ⚠ `.table-wrap` 是按**序号**接的 —— 它是 `buildTable()` 新造的元素，
       旧的那个已经没了，拿不到引用。 */
  function snapshotScroll() {
    const wraps = [];
    document.querySelectorAll('.table-wrap').forEach((el) => wraps.push(el.scrollTop));
    return { win: window.scrollY || document.documentElement.scrollTop || 0, wraps: wraps };
  }

  function restoreScroll(sc) {
    if (!sc) return;
    const wraps = document.querySelectorAll('.table-wrap');
    for (let i = 0; i < wraps.length && i < sc.wraps.length; i++) {
      if (sc.wraps[i]) wraps[i].scrollTop = sc.wraps[i];
    }
    if (sc.win) window.scrollTo(0, sc.win);
  }

  function render() {
    const sc = snapshotScroll();
    setSessionHeader();
    renderScanCard();
    renderResults();
    restoreScroll(sc);
  }

  function renderScanCard() {
    const st = state.st;
    if (!st) return;
    const s = st.stats();
    state.stats = s;
    $('#scan-store').textContent = `${st.storeName} · ${st.date}`;

    const g = $('#stats');
    g.innerHTML = [
      stat('应盘（有串号）', s.shouldCount, ''),
      stat('已盘到', s.foundCount, 'ok'),
      stat('未扫到', s.missingCount, s.missingCount ? 'danger' : 'ok'),
      stat('其中手工确认', s.manualCount, s.manualCount ? 'warn' : ''),
      stat('表外码', s.extraCount, s.extraCount ? 'info' : ''),
      stat('无串号商品', s.nonSerialRows, ''),
      stat('在途待入库', s.inTransitCount, s.inTransitCount ? 'warn' : ''),
    ].join('');

    $('#progress-bar').style.width = s.progress + '%';
    $('#progress-text').textContent =
      `${s.foundCount} / ${s.shouldCount} 台（${s.progress}%）` +
      (s.manualCount ? ` · 扫码 ${s.scannedCount} + 手工 ${s.manualCount}` : '');
    $('#progress-extra').textContent =
      `扫描 ${s.totalScans} 次` + (s.dupCount ? ` · 重复 ${s.dupCount} 次` : '');

    // 最近扫描
    const recent = st.scans.slice(-6).reverse();
    $('#recent').innerHTML = recent.length
      ? recent
          .map((r, i) => {
            const it = r.itemKey ? st.itemByKey.get(r.itemKey) : null;
            const label = core.STATUS_LABEL[r.status] || r.status;
            const absIndex = st.scans.length - 1 - i; // 该条在流水里的下标
            return `<div class="item"><span class="tag ${r.status}">${esc(label)}</span><span class="code">${esc(
              r.code
            )}</span><span>${esc(it ? cut(it.name, 34) : r.owners && r.owners.length ? '属于 ' + r.owners.map((o) => o.store).join('、') : '')}</span><span style="margin-left:auto">${esc(
              core.fmtTime(r.ts).slice(11)
            )}</span><button class="mini-x" data-act="unscan-at" data-key="${absIndex}" title="只删这一条">✕</button></div>`;
          })
          .join('')
      : '<div class="item">暂无扫描记录</div>';

    const fb = $('#feedback');
    const r = state.lastResult;
    if (!r) {
      fb.className = 'feedback';
      fb.innerHTML =
        '<span class="icon">📷</span><div><div class="fb-main">等待扫码</div><div class="fb-sub">把光标放在上面的输入框，用扫码枪扫 SN 或 IMEI 条码即可</div></div>';
    } else {
      const it = r.item;
      const icon = r.status === 'ok' ? '✅' : r.status === 'dup' ? '↺' : r.status === 'foreign' ? '⚠️' : '❌';
      const title = core.STATUS_LABEL[r.status] || r.status;
      const sub = [];
      sub.push('码：' + r.code);
      if (it) sub.push('商品：' + it.name);
      if (it && it.serials.length > 1) sub.push('该机串号：' + it.serials.join(' / '));
      if (r.message) sub.push(r.message);
      if (r.owners && r.owners.length) sub.push('归属：' + r.owners.map((o) => o.store + ' · ' + cut(o.name, 30)).join(' ｜ '));
      fb.className = 'feedback ' + r.status;
      fb.innerHTML =
        `<span class="icon">${icon}</span><div><div class="fb-main">${esc(title)}</div><div class="fb-sub">${esc(
          sub.join(' · ')
        )}</div></div>`;
    }
  }

  function stat(k, v, cls) {
    return `<div class="stat ${cls}"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`;
  }

  function cut(s, n) {
    s = String(s || '');
    return s.length > n ? s.slice(0, n) + '…' : s;
  }

  const TABS = [
    { id: 'review', label: '待核实' },
    { id: 'transit', label: '在途待入库' },
    { id: 'missing', label: '未扫到' },
    { id: 'extra', label: '表外码' },
    { id: 'found', label: '已扫明细' },
    { id: 'nonserial', label: '无串号商品' },
    { id: 'summary', label: '汇总' },
  ];

  function visibleTabs() {
    const st = state.st;
    const pending = st && st.pendingReview ? st.pendingReview.length : 0;
    // 「待核实」只在真有需要人工处理的条目时出现，平时不占位置
    const transit = st && st.inTransitItems ? st.inTransitItems().length : 0;
    return TABS.filter((t) => {
      if (t.id === 'review') return pending > 0;
      if (t.id === 'transit') return transit > 0;
      return true;
    });
  }

  function tabCount(id) {
    const st = state.st;
    if (!st) return 0;
    if (id === 'review') return (st.pendingReview || []).length;
    if (id === 'transit') return st.inTransitItems().length;
    if (id === 'missing') return st.missing().length;
    if (id === 'extra') return st.extras().length;
    if (id === 'found') return st.found().length;
    // 确认过的已经移到「已扫明细」，角标显示剩下待确认的行数
    if (id === 'nonserial') return st.nonSerialItems().filter((it) => !it.manualAt).length;
    return 0;
  }

  /**
   * 重绘前把表格里输入框的内容和焦点存下来，重绘后恢复。
   * 为什么需要：全库索引在后台加载完成时会触发一次整表重绘，
   * 如果没有这一步，用户正在输入、还没失焦提交的实盘数量会被直接冲掉。
   */
  function snapshotInputs() {
    const body = $('#tab-body');
    if (!body) return null;
    const ae = document.activeElement;
    const snap = { values: Object.create(null), focus: null };
    body.querySelectorAll('input[data-kind]').forEach((el) => {
      const kind = el.getAttribute('data-kind');
      const key = el.getAttribute('data-key');
      snap.values[kind + '\u0001' + key] = el.value;
      if (el === ae) {
        snap.focus = {
          kind: kind,
          key: key,
          start: el.selectionStart,
          end: el.selectionEnd,
        };
      }
    });
    return snap;
  }

  function restoreInputs(snap) {
    if (!snap) return;
    const body = $('#tab-body');
    if (!body) return;
    body.querySelectorAll('input[data-kind]').forEach((el) => {
      const k = el.getAttribute('data-kind') + '\u0001' + el.getAttribute('data-key');
      if (snap.values[k] !== undefined && snap.values[k] !== el.value) el.value = snap.values[k];
    });
    if (snap.focus) {
      const el = body.querySelector(
        `input[data-kind="${snap.focus.kind}"][data-key="${snap.focus.key}"]`
      );
      if (el) {
        // ⚠ 同理（见 `focusScan`）：恢复输入框焦点时别顺手把页面滚走
        el.focus({ preventScroll: true });
        try {
          if (snap.focus.start !== null && snap.focus.start !== undefined) {
            el.setSelectionRange(snap.focus.start, snap.focus.end);
          }
        } catch (e) {}
      }
    }
  }

  function renderResults() {
    const st = state.st;
    if (!st) return;
    const snap = snapshotInputs();

    $('#tabs').innerHTML = visibleTabs().map((t) => {
      const n = tabCount(t.id);
      const hot = (t.id === 'missing' || t.id === 'extra' || t.id === 'review') && n > 0 ? ' hot' : '';
      const noBadge = t.id === 'summary';
      return `<button data-tab="${t.id}" class="${state.tab === t.id ? 'active' : ''}">${esc(t.label)}${
        noBadge ? '' : `<span class="badge${hot}">${n}</span>`
      }</button>`;
    }).join('');

    const body = $('#tab-body');
    if (state.tab === 'summary') {
      body.innerHTML = renderSummary();
      return;
    } else {
      const t = buildTable(state.tab);
      const demoFilter = state.tab === 'missing' ? `<button data-act="filter-demo">只看样机/演示机</button>` : '';
      let bulk = '';
      if (state.tab === 'nonserial') {
        const pending = st.pendingNonSerial().length; // 插完 DOM 后 refreshNonSerialBar 会按筛选精确刷新
        bulk = `<button data-act="confirm-all" class="primary"${
          pending ? '' : ' disabled'
        }>✓ 一键确认已填数量（${pending}）</button>`;
      }
      const tip =
        state.tab === 'missing'
          ? `<div class="hint">样机、演示机盒子找不到扫不了码的：先在「确认备注」写明原因，再点 <b>✓ 标记已找到</b>，这台就计入已盘（会在「已扫明细」里标成“手工确认”，导出报表也有记录）。<br>注意：<b>在途（待入库）的商品不在这个清单里</b>——货还没到店，本来就不该扫到；它们单独在「在途待入库」页，如果货实际到了，扫它就会变成「已扫到（货已到）」。</div>`
          : state.tab === 'found'
          ? `<div class="hint">扫错的可以<b>单独撤销</b>：每行右边的「撤销这一扫」只删这一台的记录，其它记录和手工确认都不受影响；撤销后它会回到「未扫到」。扫码台下面的「最近扫描」每条后面也有 ✕，可以随手删掉刚扫错的那一条。</div>`
          : state.tab === 'extra'
          ? `<div class="hint">这些是本店账面上没有的码。「删除这条」会删掉该码的全部扫描记录（含重复扫的）。如果是真实存在的货，建议先写备注留痕再处理。</div>`
          : state.tab === 'nonserial'
          ? (() => {
              return `<div class="hint">整箱配件、促销品这类没有串号、扫不了码的：<b>✓ 标记已找到</b> 表示账面数量全在（实盘数量自动等于账面、差异 0），确认后这行就移到「已扫明细」，不在这里占位置了；<b>数量有出入就直接改实盘数量</b>，系统自动算差异。<br>录数量时按 <b>Tab</b> 或 <b>回车</b> 跳到<b>下一行的实盘数量</b>（Shift+Tab 回上一行）；填完一批可以点 <b>✓ 一键确认已填数量</b> 批量确认。<br><b>这里的数量只对「在库」</b>：同一个商品如果还有在途，会在「在途待入库」单独列一条，两边各算各的。</div>
              <div class="hint" id="ns-progress" style="color:var(--text-2)">${nonSerialProgressHtml()}</div>`;
            })()
          : '';
      body.innerHTML =
        `<div class="toolbar"><input type="text" id="search" placeholder="在本页结果中搜索…" value="${esc(
          state.search[state.tab] || ''
        )}" style="width:260px">${demoFilter}${bulk}<span class="spacer"></span>` +
        `<button data-act="export-tab">导出本表 CSV</button></div>` +
        tip +
        `<div class="table-wrap">${t.html}</div>` +
        (t.moreHtml || '');
    }
    restoreInputs(snap); // 正在输入的内容与焦点原样恢复
    refreshNonSerialBar(); // DOM 已就绪，按当前筛选/输入框内容精确刷新按钮与进度
  }

  function buildTable(tab) {
    const st = state.st;
    const kw = core.s(state.search[tab]).toUpperCase();
    let headers = [];
    let rows = [];

    if (tab === 'transit') {
      headers = ['#', '商品名称', '串号', '账面数量', '来源仓', '收货单号', '状态'];
      rows = st.inTransitItems().map((it, i) => {
        const info = it.inTransitInfo || {};
        const arrived = !!(it.matchedCode || it.manualAt);
        return [
          String(i + 1),
          it.name,
          it.serials.join(' / ') || (it.hasSerial ? '—' : '无串号（按数量）'),
          String(it.qty),
          info.fromStore || '—',
          info.receivingCode || '—',
          {
            html: arrived ? '<span class="tag ok">已扫到（货已到）</span>' : '<span class="tag dup">待入库</span>',
            text: arrived ? '已扫到（货已到）' : '待入库',
          },
        ];
      });
    } else if (tab === 'review') {
      headers = ['#', '类型', '原标识（旧账面）', '内容', '原因'];
      rows = (st.pendingReview || []).map((r, i) => [
        String(i + 1),
        r.kind || '',
        r.key || '',
        r.value === undefined || r.value === null ? '' : String(r.value),
        r.reason || '旧版本记录无法确认对应商品，需人工核对',
      ]);
    } else if (tab === 'missing') {
      headers = [
        '#',
        '商品名称',
        '账面串号（任一即可）',
        '分类',
        '品牌',
        '串号标识',
        '商品编码',
        '确认备注',
        '操作',
      ];
      rows = st.missing().map((it, i) => [
        String(i + 1),
        it.name,
        it.serials.join(' / '),
        [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
        it.brand,
        it.tag,
        it.proId,
        {
          input: {
            kind: 'mnote',
            key: it.key,
            value: st.manualNote(it.key),
            placeholder: '如：样机在展台、无盒',
          },
        },
        {
          html: `<button class="mini-btn" data-act="confirm" data-key="${esc(it.key)}" title="实在扫不到码时，人工核对后点这里计入已盘">✓ 标记已找到</button>`,
          text: '',
        },
      ]);
    } else if (tab === 'extra') {
      headers = ['#', '扫到的码', '判定', '归属仓库', '归属商品', '次数', '首次扫描时间', '备注', '操作'];
      rows = st.extras().map((r, i) => [
        String(i + 1),
        r.code,
        core.STATUS_LABEL[r.status],
        r.owners.map((o) => o.store).join('、') || '—',
        r.owners.length ? r.owners.map((o) => o.name).join(' ｜ ') : '—',
        String(r.times),
        core.fmtTime(r.ts),
        { input: { kind: 'note', key: r.code, value: st.remarks[r.code] || '', placeholder: '如：调拨未入账 / 扫错' } },
        {
          html: `<button class="mini-btn ghost-btn" data-act="unscan-code" data-key="${esc(
            r.code
          )}" title="删掉这个码的全部扫描记录（${r.times} 条）">删除这条</button>`,
          text: '',
        },
      ]);
    } else if (tab === 'found') {
      headers = ['#', '商品名称', '来源', '扫到的码', '该机全部串号', '分类', '盘点时间', '备注', '操作'];
      rows = st
        .found()
        .slice()
        .sort((a, b) => (a.matchedAt || a.manualAt) - (b.matchedAt || b.manualAt))
        .map((it, i) => {
          const manual = !it.matchedCode && !!it.manualAt;
          const noSerial = !it.hasSerial;
          return [
            String(i + 1),
            it.name,
            {
              html: manual
                ? '<span class="tag dup">手工确认</span>'
                : '<span class="tag ok">扫码</span>',
              text: manual ? '手工确认' : '扫码',
            },
            it.matchedCode || (noSerial ? `无串号（账面 ${it.qty}）` : '—'),
            noSerial ? '—' : it.serials.join(' / '),
            [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
            core.fmtTime(it.matchedAt || it.manualAt),
            manual
              ? {
                  input: { kind: 'mnote', key: it.key, value: st.manualNote(it.key), placeholder: '备注' },
                }
              : st.manualNote(it.key) || '',
            manual
              ? {
                  html: `<button class="mini-btn ghost-btn" data-act="unconfirm" data-key="${esc(
                    it.key
                  )}" title="点错了？撤销后这台回到「未扫到」">撤销确认</button>`,
                  text: '',
                }
              : {
                  html: `<button class="mini-btn ghost-btn" data-act="unscan" data-key="${esc(
                    it.key
                  )}" title="只删掉这一台的扫码记录，其它记录不受影响">撤销这一扫</button>`,
                  text: '',
                },
          ];
        });
    } else if (tab === 'nonserial') {
      // 确认过的已经进「已扫明细」了，这里只留还没确认的，作为待办清单
      headers = ['#', '商品名称', '分类', '串号标识', '账面数量', '实盘数量', '差异', '备注', '操作'];
      rows = st
        .nonSerialRows()
        .filter((r) => !r.confirmed)
        .map((r, i) => [
        String(i + 1),
        r.item.name,
        [r.item.cat1, r.item.cat2, r.item.cat3].filter(Boolean).join('/'),
        r.item.tag,
        String(r.book),
        { input: { kind: 'qty', key: r.item.key, value: r.actual === null ? '' : String(r.actual) } },
        r.diff === null ? '未盘' : r.diff === 0 ? '0' : (r.diff > 0 ? '+' : '') + r.diff,
        { input: { kind: 'note', key: r.item.key, value: r.remark, placeholder: '备注' } },
        {
          html: `<button class="mini-btn" data-act="confirm" data-key="${esc(
            r.item.key
          )}" title="数量没错就点这里，表示账面数量全在；有差异请直接改实盘数量">✓ 标记已找到</button>`,
          text: '',
        },
      ]);
    }

    if (kw) {
      rows = rows.filter((r) =>
        r.some((c) => {
          const v = c && typeof c === 'object' ? (c.input ? c.input.value : c.text || '') : c;
          return String(v === null || v === undefined ? '' : v).toUpperCase().indexOf(kw) >= 0;
        })
      );
    }

    if (!rows.length) {
      const txt =
        tab === 'missing'
          ? '<span class="big">🎉</span>没有未扫到的商品'
          : tab === 'extra'
          ? '<span class="big">👍</span>没有表外码'
          : tab === 'nonserial'
          ? '<span class="big">🎉</span>无串号商品都确认完了（确认过的都在「已扫明细」里）'
          : tab === 'review'
          ? '没有需要人工核实的条目'
          : tab === 'transit'
          ? '这个门店没有在途（待入库）商品'
          : '暂无数据';
      return { html: `<div class="empty">${txt}</div>` };
    }

    const LIMIT = 400;
    const showAll = state.showAll[tab];
    const visible = showAll ? rows : rows.slice(0, LIMIT);
    let moreHtml = '';
    if (rows.length > LIMIT && !showAll) {
      moreHtml = `<div class="hint">共 ${rows.length} 条，当前显示前 ${LIMIT} 条。<button data-act="show-all" style="margin-left:8px">显示全部</button></div>`;
    }

    const head = headers.map((h) => `<th>${esc(h)}</th>`).join('');
    const body = visible
      .map((r) => {
        const tds = r
          .map((c) => {
            if (c && typeof c === 'object' && c.input) {
              const i = c.input;
              const cls = i.kind === 'qty' ? 'qty-input' : 'note-input';
              return `<td><input class="${cls}" data-kind="${i.kind}" data-key="${esc(i.key)}" value="${esc(
                i.value
              )}" placeholder="${esc(i.placeholder || '')}" inputmode="${i.kind === 'qty' ? 'numeric' : 'text'}"></td>`;
            }
            if (c && typeof c === 'object' && c.html) return `<td class="act-cell">${c.html}</td>`;
            const txt = String(c === null || c === undefined ? '' : c);
            const cls = /^[0-9A-Za-z\-\/]{6,}$/.test(txt) && /[0-9]/.test(txt) ? ' class="mono"' : '';
            return `<td${cls}>${esc(txt)}</td>`;
          })
          .join('');
        return `<tr>${tds}</tr>`;
      })
      .join('');

    return {
      html: `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`,
      moreHtml: moreHtml,
    };
  }

  function renderSummary() {
    const st = state.st;
    const s = st.stats();
    const okRate = s.shouldCount ? ((s.foundCount / s.shouldCount) * 100).toFixed(1) : '0.0';
    const items = [
      ['账面总行数', st.items.length],
      ['账面在库数量', s.bookOnHand],
      ['账面总数量（含在途）', s.bookTotal],
      ['应盘（有串号）', s.shouldCount],
      ['已盘到', s.foundCount],
      ['其中：扫码盘到', s.scannedCount],
      ['其中：手工确认', s.manualCount],
      ['未扫到', s.missingCount],
      ['盘点完成率', okRate + '%'],
      ['表外码（去重）', s.extraCount],
      ['表外码扫描次数', s.extraScans],
      ['重复扫描次数', s.dupCount],
      ['扫描总次数', s.totalScans],
      ['在途（待入库）台数', s.inTransitCount],
      ['其中已扫到（货已到）', s.inTransitArrivedCount],
      ['无串号商品行数', s.nonSerialRows],
      ['无串号账面数量', s.nonSerialBook],
      ['无串号已手工盘过行数', s.nonSerialCounted + ' / ' + s.nonSerialRows],
      ['无串号已盘部分账面', s.nonSerialBookCounted],
      ['无串号已盘部分实盘', s.nonSerialActual],
      ['无串号差异（仅已盘部分）', s.nonSerialDiff],
    ];
    return (
      `<div class="sum-list">` +
      items
        .map(
          ([k, v]) =>
            `<div class="sum-item"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`
        )
        .join('') +
      `</div>` +
      `<div class="hint">无串号商品的差异只统计<b>已经手工盘过的行</b>，未盘的行不计入，避免盘到一半显示成大面积盘亏。<br>盘点对象：<b>${esc(st.storeName)}</b>　快照日期：<b>${esc(st.date)}</b>　开始时间：<b>${esc(
        core.fmtTime(st.startedAt)
      )}</b>` +
      (state.giStatus === 'ready'
        ? `<br>全库串号索引：<b>已加载 ${state.giCount} 个串号</b>，表外码可显示归属仓库。`
        : `<br>全库串号索引：<b>${
            state.giStatus === 'loading' ? '加载中…' : state.giStatus === 'error' ? '加载失败（' + esc(state.giError) + '）' : '未加载'
          }</b>，表外码只能提示"本店账面没有"。`) +
      `</div>`
    );
  }

  /* ---------------- 导出 ---------------- */
  function fileBase() {
    const st = state.st;
    const d = st ? st.date : core.todayStr();
    const n = st ? st.storeName : '库存';
    return `库存盘点_${n}_${d}`.replace(/[\\/:*?"<>|]/g, '_');
  }

  function sheetsForExport() {
    const st = state.st;
    const s = st.stats();
    const sheets = [];
    sheets.push({
      name: '汇总',
      widths: [22, 20],
      rows: [
        ['项目', '数值'],
        ['盘点仓库', st.storeName],
        ['快照日期', st.date],
        ['开始时间', core.fmtTime(st.startedAt)],
        ['导出时间', core.fmtTime(Date.now())],
        ['账面总行数', st.items.length],
        ['账面在库数量', s.bookOnHand],
        ['账面总数量（含在途）', s.bookTotal],
        ['应盘（有串号）', s.shouldCount],
        ['已盘到', s.foundCount],
        ['其中扫码盘到', s.scannedCount],
        ['其中手工确认', s.manualCount],
        ['未扫到', s.missingCount],
        ['盘点完成率', (s.shouldCount ? ((s.foundCount / s.shouldCount) * 100).toFixed(1) : '0.0') + '%'],
        ['表外码（去重）', s.extraCount],
        ['表外码扫描次数', s.extraScans],
        ['重复扫描次数', s.dupCount],
        ['扫描总次数', s.totalScans],
        ['在途（待入库）台数', s.inTransitCount],
        ['其中已扫到（货已到）', s.inTransitArrivedCount],
        ['无串号商品行数', s.nonSerialRows],
        ['无串号账面数量', s.nonSerialBook],
        ['无串号已手工盘过行数', s.nonSerialCounted + ' / ' + s.nonSerialRows],
        ['无串号已盘部分账面', s.nonSerialBookCounted],
        ['无串号已盘部分实盘', s.nonSerialActual],
        ['无串号差异（仅已盘部分）', s.nonSerialDiff],
      ],
    });
    sheets.push({
      name: '未扫到',
      widths: [46, 34, 22, 10, 12, 12],
      rows: [['商品名称', '账面串号（任一即可）', '分类', '品牌', '串号标识', '商品编码']].concat(
        st.missing().map((it) => [
          it.name,
          it.serials.join(' / '),
          [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
          it.brand,
          it.tag,
          it.proId,
        ])
      ),
    });
    sheets.push({
      name: '表外串号',
      widths: [26, 14, 22, 40, 8, 20, 26],
      rows: [['扫到的码', '判定', '归属仓库', '归属商品', '次数', '首次扫描时间', '备注']].concat(
        st.extras().map((r) => [
          r.code,
          core.STATUS_LABEL[r.status],
          r.owners.map((o) => o.store).join('、'),
          r.owners.map((o) => o.name).join(' ｜ '),
          r.times,
          core.fmtTime(r.ts),
          st.remarks[r.code] || '',
        ])
      ),
    });
    sheets.push({
      name: '已扫明细',
      widths: [46, 12, 26, 34, 22, 20, 26],
      rows: [
        ['商品名称', '来源', '扫到的码', '该机全部串号', '分类', '盘点时间', '备注'],
      ].concat(
        st
          .found()
          .slice()
          .sort((a, b) => (a.matchedAt || a.manualAt) - (b.matchedAt || b.manualAt))
          .map((it) => [
            it.name,
            it.matchedCode ? '扫码' : '手工确认',
            it.matchedCode || (it.hasSerial ? '' : '（无串号，账面 ' + it.qty + '）'),
            it.serials.join(' / '),
            [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
            core.fmtTime(it.matchedAt || it.manualAt),
            st.manualNote(it.key) || st.remarks[it.key] || '',
          ])
      ),
    });
    sheets.push({
      name: '在途待入库',
      widths: [46, 34, 12, 20, 20, 16, 20],
      rows: [['商品名称', '串号', '账面在途数量', '来源仓', '收货单号', '状态', '预计到货']].concat(
        st.inTransitItems().map((it) => {
          const info = it.inTransitInfo || {};
          const arrived = !!(it.matchedCode || it.manualAt);
          return [
            it.name,
            it.serials.join(' / '),
            it.qty,
            info.fromStore || '',
            info.receivingCode || '',
            arrived ? '已扫到（货已到）' : '待入库',
            info.expectDate || '',
          ];
        })
      ),
    });
    sheets.push({
      name: '手工确认',
      widths: [46, 12, 34, 12, 22, 12, 20, 30],
      rows: [
        ['商品名称', '类型', '全部串号', '账面数量', '分类', '串号标识', '确认时间', '确认备注'],
      ].concat(
        st.manualList().map((it) => [
          it.name,
          it.hasSerial ? '有串号' : '无串号',
          it.serials.join(' / '),
          it.qty,
          [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
          it.tag,
          core.fmtTime(it.manualAt),
          st.manualNote(it.key) || st.remarks[it.key] || '',
        ])
      ),
    });
    sheets.push({
      name: '无串号商品',
      widths: [46, 22, 12, 12, 12, 12, 14, 20, 26],
      rows: [
        ['商品名称', '分类', '串号标识', '账面数量', '实盘数量', '差异', '是否确认', '确认时间', '备注'],
      ].concat(
        st.nonSerialRows().map((r) => [
          r.item.name,
          [r.item.cat1, r.item.cat2, r.item.cat3].filter(Boolean).join('/'),
          r.item.tag,
          r.book,
          r.actual === null ? '' : r.actual,
          r.diff === null ? '' : r.diff,
          r.confirmed ? '已确认找到' : '',
          r.confirmed ? core.fmtTime(r.item.manualAt) : '',
          r.remark,
        ])
      ),
    });
    sheets.push({
      name: '账面全量',
      widths: [46, 34, 22, 10, 10, 14, 20, 26],
      rows: [['商品名称', '全部串号', '分类', '在库', '在途', '盘点方式', '扫到的码', '备注']].concat(
        st.items.map((it) => [
          it.name,
          it.serials.join(' / '),
          [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
          it.inTransit ? 0 : it.qty,
          it.inTransit ? it.qty : it.qtyOnTransfer || 0,
          it.inTransit
            ? it.matchedCode || it.manualAt
              ? '在途·已扫到'
              : '在途待入库'
            : !it.hasSerial
            ? it.manualAt
              ? '无串号·已确认'
              : '无串号'
            : it.matchedCode
            ? '扫码盘到'
            : it.manualAt
            ? '手工确认'
            : '未扫到',
          it.matchedCode,
          st.manualNote(it.key) || st.remarks[it.key] || '',
        ])
      ),
    });
    return sheets;
  }

  /**
   * 「一键确认已填数量」会处理哪些行：当前列表里填了实盘数量、还没确认的行。
   * 有搜索条件时只算筛选出来的行，避免把看不见的行也提交了。
   */
  function bulkPendingKeys() {
    const st = state.st;
    if (!st) return [];
    const kw = core.s(state.search[state.tab]);
    const inputs = new Map(
      [...document.querySelectorAll('#tab-body .qty-input')].map((el) => [el.getAttribute('data-key'), el])
    );
    const out = [];
    st.nonSerialItems()
      .filter((it) => !it.manualAt)
      .forEach((it) => {
        const el = inputs.get(it.key);
        if (kw && !el) return; // 被搜索条件筛掉的行不算
        const raw = el
          ? el.value.trim()
          : st.manualQty[it.key] === undefined
          ? ''
          : String(st.manualQty[it.key]);
        if (raw !== '' && !isNaN(Number(raw))) out.push(it.key);
      });
    return out;
  }

  /** 无串号商品页的进度文案（口径：已确认 = 真正点过确认的行） */
  function nonSerialProgressHtml() {
    const st = state.st;
    if (!st) return '';
    const s = st.stats();
    const confirmed = st.manualNonSerial().length;
    const left = s.nonSerialRows - confirmed;
    const filled = bulkPendingKeys().length;
    return (
      `无串号商品共 <b>${s.nonSerialRows}</b> 行，已确认 <b>${confirmed}</b> 行（已移到「已扫明细」），` +
      `列表里还剩 <b>${left}</b> 行待确认，其中 <b>${filled}</b> 行已填数量可一键提交。`
    );
  }

  /** 只刷新「一键确认」按钮和进度文案，不动表格，保证输入焦点不丢 */
  function refreshNonSerialBar() {
    if (!state.st || state.tab !== 'nonserial') return;
    const n = bulkPendingKeys().length;
    const btn = document.querySelector('[data-act="confirm-all"]');
    if (btn) {
      btn.textContent = `✓ 一键确认已填数量（${n}）`;
      btn.disabled = n === 0;
    }
    const prog = document.getElementById('ns-progress');
    if (prog) prog.innerHTML = nonSerialProgressHtml();
  }

  /**
   * 提交实盘数量：只更新这一行的差异单元格和统计卡片，
   * 不整表重绘 —— 重绘会把焦点弄丢，没法连续录入。
   */
  function commitQty(el) {
    if (!state.st) return;
    const key = el.getAttribute('data-key');
    const it = state.st.itemByKey.get(key);
    if (!it) return;
    const v = String(el.value || '').trim();
    state.st.setManualQty(key, v);
    saveSession();

    const actual = v === '' ? null : Number(v);
    const diff = actual === null ? null : actual - it.qty;
    const tr = el.closest ? el.closest('tr') : null;
    if (tr && tr.children[6]) {
      tr.children[6].textContent = diff === null ? '未盘' : diff === 0 ? '0' : (diff > 0 ? '+' : '') + diff;
    }
    if (state.stats) renderScanCard();
    refreshNonSerialBar();
  }

  /** 提交当前正在编辑的表格输入框（切页/重绘前兜底，避免没失焦的值丢掉） */
  function commitFocusedInput() {
    const el = document.activeElement;
    if (!el || !el.getAttribute || !state.st) return;
    const kind = el.getAttribute('data-kind');
    if (!kind) return;
    const key = el.getAttribute('data-key');
    if (kind === 'qty') commitQty(el);
    else if (kind === 'note') {
      state.st.remarks[key] = el.value;
      saveSession();
    } else if (kind === 'mnote') {
      state.st.setManualNote(key, el.value);
      saveSession();
    }
  }

  /** 把焦点移到上/下一行的同一类输入框（跳过普通单元格） */
  function moveSameColumn(el, dir) {
    const kind = el.getAttribute('data-kind');
    const list = [...document.querySelectorAll('#tab-body input[data-kind="' + kind + '"]')];
    const i = list.indexOf(el);
    if (i < 0) return;
    const next = list[i + dir];
    if (next) {
      next.focus();
      try {
        next.select();
      } catch (e) {}
      return;
    }
    if (dir > 0) {
      // 最后一行的下一格：回到扫码框，接着扫码
      focusScan();
      toast('这一列填完了，继续扫码吧');
    } else {
      el.focus();
      try {
        el.select();
      } catch (e) {}
    }
  }

  function exportXlsx() {
    if (!state.st) return;
    try {
      const bytes = xlsx.build(sheetsForExport());
      xlsx.download(fileBase() + '.xlsx', bytes);
      toast(`已导出 ${fileBase()}.xlsx`);
    } catch (e) {
      toast('导出失败：' + e.message, true);
    }
  }

  /**
   * 「导出并推送」—— 把**同一份** xlsx POST 给后端（M16 接进来的那一环）。
   *
   * 后端做两件事：落到 `out/inventory/`，然后推邮件（带这个附件）+ 企微（汇总一条）。
   * ⚠ 推送走的是控制台里配好的通道（通用设置 › 邮件 / 企业微信），这一页不碰它们。
   * ⚠ **推送失败 ≠ 导出失败**：文件已经落在那台机器上了，界面上逐渠道说清成没成。
   */
  async function exportAndPush() {
    if (!state.st) return;
    let bytes;
    try {
      bytes = xlsx.build(sheetsForExport());
    } catch (e) {
      toast('导出失败：' + e.message, true);
      return;
    }
    const name = fileBase();
    busy('正在推送给后端…', name + '.xlsx（7 张表）');
    try {
      const r = await api.exportAndPush(bytes, {
        name: name,
        store: state.st.storeName,
        date: state.st.date,
      });
      const mail = r && r.mail ? r.mail : {};
      const wecom = r && r.wecom ? r.wecom : {};
      const parts = [];
      parts.push('邮件' + (mail.ok ? '✅' : '❌ ' + (mail.why || '')));
      parts.push('企微' + (wecom.ok ? '✅' : '❌ ' + (wecom.why || '')));
      const okAny = mail.ok || wecom.ok;
      toast(
        (okAny ? '已推送' : '没推出去') + '：' + parts.join('　') + `　（清单存在 ${r && r.name ? r.name : name + '.xlsx'}）`,
        !okAny
      );
    } catch (e) {
      toast('推送失败：' + e.message, true);
    } finally {
      unbusy();
    }
  }

  function exportCsv() {
    if (!state.st) return;
    const sheets = sheetsForExport();
    const rows = [];
    sheets.forEach((sh, i) => {
      if (i) rows.push([]);
      rows.push(['【' + sh.name + '】']);
      sh.rows.forEach((r) => rows.push(r));
    });
    const csv = '\ufeff' + core.toCSV(rows);
    xlsx.download(fileBase() + '.csv', new TextEncoder().encode(csv), 'text/csv;charset=utf-8');
    toast('已导出 CSV');
  }

  function exportTabCsv() {
    if (!state.st) return;
    const st = state.st;
    let rows = [];
    if (state.tab === 'missing') {
      rows = [['商品名称', '账面串号', '分类', '品牌', '串号标识', '商品编码', '确认备注']].concat(
        st.missing().map((it) => [
          it.name,
          it.serials.join(' / '),
          [it.cat1, it.cat2, it.cat3].filter(Boolean).join('/'),
          it.brand,
          it.tag,
          it.proId,
          st.manualNote(it.key),
        ])
      );
    } else if (state.tab === 'extra') {
      rows = [['扫到的码', '判定', '归属仓库', '次数', '首次扫描时间', '备注']].concat(
        st.extras().map((r) => [r.code, core.STATUS_LABEL[r.status], r.owners.map((o) => o.store).join('、'), r.times, core.fmtTime(r.ts), st.remarks[r.code] || ''])
      );
    } else if (state.tab === 'found') {
      rows = [['商品名称', '来源', '扫到的码', '该机全部串号', '盘点时间', '备注']].concat(
        st
          .found()
          .map((it) => [
            it.name,
            it.matchedCode ? '扫码' : '手工确认',
            it.matchedCode,
            it.serials.join(' / '),
            core.fmtTime(it.matchedAt || it.manualAt),
            st.manualNote(it.key),
          ])
      );
    } else if (state.tab === 'nonserial') {
      rows = [['商品名称', '账面数量', '实盘数量', '差异', '是否确认', '备注']].concat(
        st.nonSerialRows().map((r) => [
          r.item.name,
          r.book,
          r.actual === null ? '' : r.actual,
          r.diff === null ? '' : r.diff,
          r.confirmed ? '已确认找到' : '',
          r.remark,
        ])
      );
    }
    const csv = '\ufeff' + core.toCSV(rows);
    const name = TABS.find((x) => x.id === state.tab);
    xlsx.download(`${fileBase()}_${name ? name.label : state.tab}.csv`, new TextEncoder().encode(csv), 'text/csv;charset=utf-8');
    toast('已导出本表 CSV');
  }

  /* ---------------- 更新账面（独立操作） ---------------- */

  let pendingBookUpdate = null;

  /**
   * 更新账面：重新从 ERP 拉一次，但**不覆盖**当前会话——
   * 先算出差异（新增/移除/数量变化/不唯一），让人确认后再建立新账面版本。
   * 旧账面会保留为「上一版」。
   */
  async function updateBook() {
    const st = state.st;
    if (!st) return;
    if (!api.isConfigured(state.cfg)) {
      requireLogin('更新账面需要先登录');
      return;
    }
    if (!confirm('更新账面会重新从 ERP 拉取库存。\n· 当前账面会保留为「上一版」\n· 扫码记录按串号重新核对\n· 手工数量/确认/备注只在不唯一的条目上转人工核实\n\n确定继续？')) return;

    busy('正在拉取新账面…', st.storeName + ' · ' + st.date);
    try {
      const res = await withAuth(() =>
        api.fetchInventory(state.cfg, { date: st.date, storeId: st.storeId, label: '库存查询' })
      );
      let newItems = core.normalizeAll(res.rows, st.storeId);
      const transitInBook = newItems.filter((i) => i.inTransit).length;
      if (transitInBook === 0 && state.loadInTransit) {
        try {
          const tr = await withAuth(() =>
            api.fetchInTransit(state.cfg, {
              date: st.date,
              storeId: st.storeId,
              storeName: st.storeName,
            })
          );
          newItems = core.mergeInTransit(newItems, tr.rows, st.storeId, st.storeName).items;
        } catch (e) {
          toast('更新账面时在途清单没拉到：' + e.message + '（本次不含在途）', true);
        }
      }
      const oldBook = core.bookFromItems(st.items);
      const newBook = core.bookFromItems(newItems);
      const diff = core.diffBooks(oldBook, newBook);
      if (!diff.added.length && !diff.removed.length && !diff.changed.length && !diff.ambiguous.length) {
        toast(`账面没有变化（${diff.newCount} 行，与本地冻结账面一致）`);
        return;
      }
      pendingBookUpdate = { newItems: newItems, newBook: newBook, diff: diff };
      renderBookDiff(diff);
    } catch (e) {
      if (api.isIncomplete(e)) toast(e.message, true);
      else if (!api.isTokenError(e)) toast('更新账面失败：' + e.message, true);
    } finally {
      unbusy();
    }
  }

  function renderBookDiff(diff) {
    const card = $('#book-diff-card');
    const body = $('#book-diff-body');
    if (!card || !body) return;
    const line = (label, n, cls) => `<div class="sum-item"><div class="k">${esc(label)}</div><div class="v ${cls || ''}">${n}</div></div>`;
    const list = (title, arr, fmt) =>
      arr.length
        ? `<div class="hint" style="margin-top:10px"><b>${esc(title)}（${arr.length}）</b></div>
           <div class="table-wrap" style="max-height:220px"><table><tbody>${arr
             .slice(0, 200)
             .map((x) => `<tr><td>${fmt(x)}</td></tr>`)
             .join('')}</tbody></table></div>`
        : '';
    body.innerHTML =
      `<div class="sum-list">` +
      line('账面行数', diff.oldCount + ' → ' + diff.newCount) +
      line('新增', diff.added.length, 'diff-pos') +
      line('移除', diff.removed.length, diff.removed.length ? 'diff-neg' : '') +
      line('数量变化', diff.changed.length, diff.changed.length ? 'diff-neg' : '') +
      line('不唯一（转人工）', diff.ambiguous.length, diff.ambiguous.length ? 'diff-neg' : '') +
      line('不变', diff.sameCount) +
      `</div>` +
      list('新增', diff.added, (x) => esc(x.name) + ' <span class="tag">' + esc(x.serials.join(' / ') || '无串号') + '</span>') +
      list(
        '移除',
        diff.removed,
        (x) => esc(x.name) + ' <span class="tag">' + esc(x.serials.join(' / ') || '无串号') + '</span>'
      ) +
      list(
        '数量变化',
        diff.changed,
        (x) => esc(x.name) + ` <span class="tag">${x.oldQty} → ${x.newQty}</span>`
      ) +
      list(
        '不唯一（不会自动继承确认状态）',
        diff.ambiguous,
        (x) => esc(x.name) + ` <span class="tag">旧 ${x.oldCount} 条 / 新 ${x.newCount} 条</span>`
      ) +
      `<div class="hint">应用后：扫码记录按串号重新核对；手工数量/确认/备注只保留在两版都唯一对应的条目上，
       其余进入「待核实」由人工处理。旧账面会保留为上一版。</div>`;
    card.classList.remove('hidden');
  }

  function applyBookUpdate() {
    const st = state.st;
    const upd = pendingBookUpdate;
    if (!st || !upd) return;
    const diff = upd.diff;
    const newItems = upd.newItems;
    const next = new core.Stocktake({
      sessionId: st.sessionId,
      companycode: state.cfg.companycode,
      date: st.date,
      storeId: st.storeId,
      storeName: st.storeName,
      items: newItems,
      startedAt: st.startedAt,
      bookVersion: (st.bookVersion || 1) + 1,
      bookFetchedAt: Date.now(),
    });

    // 1) 扫码记录：按串号重放（串号是稳定身份，安全）
    const scans = st.scans.slice();
    next.scans = [];
    next.scanByCode = new Map();
    scans.forEach((r) => next.scan(r.code, r.ts));

    // 2) 手工状态：只在「两版都唯一存在」的 uid 上继承，其余转「待核实」
    const newByUid = new Map(newItems.map((i) => [i.uid, i]));
    const dupBase = new Set();
    newItems.forEach((i) => i.dupUid && dupBase.add(i.uid.split('#')[0]));
    st.items.forEach((i) => i.dupUid && dupBase.add(i.uid.split('#')[0]));
    const review = (st.pendingReview || []).slice();
    const carry = (map, kind, apply) => {
      Object.keys(map || {}).forEach((uid) => {
        const target = newByUid.get(uid);
        const base = uid.split('#')[0];
        if (target && !dupBase.has(base)) {
          apply(target, map[uid]);
        } else {
          review.push({
            kind: kind,
            key: uid,
            value: typeof map[uid] === 'object' ? map[uid].ts || '' : map[uid],
            reason: target ? '账面里该商品有多条，无法确定是哪一条' : '该商品已不在新账面里',
          });
        }
      });
    };
    const qtyMap = st.manualQty;
    const foundMap = st.manualFound;
    const noteMap = st.manualNotes;
    carry(qtyMap, '无串号实盘数量', (it, v) => {
      next.manualQty[it.uid] = v;
    });
    carry(foundMap, '手工确认', (it, v) => {
      next.manualFound[it.uid] = { ts: (v && v.ts) || Date.now() };
    });
    carry(noteMap, '确认备注', (it, v) => {
      next.manualNotes[it.uid] = v;
    });
    next.applyManualState();
    next.pendingReview = review;

    // 3) 冻结新账面（旧账面自动留成上一版）
    try {
      store.replaceBook({
        sessionId: next.sessionId,
        companycode: state.cfg.companycode,
        storeId: next.storeId,
        storeName: next.storeName,
        date: next.date,
        version: next.bookVersion,
        fetchedAt: next.bookFetchedAt,
        items: core.bookFromItems(newItems),
      });
    } catch (e) {
      setSaveStatus('error', e);
      toast('新账面保存失败：' + e.message, true);
    }

    state.st = next;
    pendingBookUpdate = null;
    const card = $('#book-diff-card');
    if (card) card.classList.add('hidden');
    saveSession();
    render();
    toast(
      `账面已更新到第 ${next.bookVersion} 版：新增 ${diff.added.length}、移除 ${diff.removed.length}、` +
        `数量变化 ${diff.changed.length}、转待核实 ${diff.ambiguous.length}` +
        (review.length ? `（待核实共 ${review.length} 条）` : '')
    );
  }

  /* ---------------- 恢复上次盘点 ---------------- */
  /**
   * 恢复上次盘点：**只读本地冻结账面**，不重新拉 ERP。
   * 断网、token 过期都能继续盘、继续导出。
   */
  async function tryResume() {
    const book = store.loadBook();
    if (book && book.items && book.items.length) {
      return resumeFromBook(book);
    }
    // 旧版本（本书面账缺失）：保留备份后按串号重新核对，人工记录进「待核实」
    const legacy = store.takeLegacy();
    if (legacy && legacy.storeId) {
      return resumeFromLegacy(legacy);
    }
    return false;
  }

  /**
   * 加载会话备份（JSON）：换机器、清了浏览器数据、或者从别人那台接手时用。
   * 校验通过后写进本机，然后走和续盘完全一样的恢复路径。
   */
  async function loadBackupFile(file) {
    if (!file) return;
    busy('正在加载会话备份…', file.name);
    try {
      const text = await file.text();
      let j = null;
      try {
        j = JSON.parse(text);
      } catch (e) {
        throw new Error('文件不是有效的 JSON');
      }
      if (!j || j.kind !== 'inventory-check-backup') {
        throw new Error('这不是本工具导出的会话备份文件');
      }
      const sess = j.session || {};
      const book = sess.book;
      if (!book || !Array.isArray(book.items) || !book.items.length) {
        throw new Error('备份里没有账面数据（可能是不完整的文件）');
      }
      if (!book.sessionId || !book.storeId) {
        throw new Error('备份里的账面信息不完整');
      }
      // 当前还有进行中的盘点时先确认
      commitFocusedInput();
      if (state.st && state.st.hasContent()) {
        if (!confirm('本机已有进行中的盘点，加载备份会替换掉它（可先点「下载会话备份」留存）。继续？')) return;
      }
      cancelPendingSave();
      store.saveBook(book);
      if (sess.state) store.saveState(sess.state);
      const okResume = resumeFromBook(book);
      if (!okResume) throw new Error('备份内容无法恢复');
      setSaveStatus('saved');
      toast(`已加载会话备份：${book.storeName} ${book.date}（账面 ${book.items.length} 行）`);
      saveSession();
    } catch (e) {
      toast('加载失败：' + e.message, true);
      openSettings('加载会话备份失败：' + e.message, 'err');
    } finally {
      unbusy();
      const inp = $('#backup-file');
      if (inp) inp.value = ''; // 允许重复选同一个文件
    }
  }

  /** 用本地冻结账面恢复（含只有手工确认/只有备注、还没扫码的会话） */
  function resumeFromBook(book) {
    const saved = store.loadState();
    const st = new core.Stocktake({
      sessionId: book.sessionId,
      companycode: book.companycode,
      date: book.date,
      storeId: book.storeId,
      storeName: book.storeName,
      bookVersion: book.version || 1,
      bookFetchedAt: book.fetchedAt || 0,
      items: core.itemsFromBook(book.items, book.storeId, book.storeName),
    });
    const matched = store.stateMatches(saved, st.sessionId, st.bookVersion);
    if (matched) {
      st.restore(saved);
    } else if (saved) {
      // 账面版本对不上：本地状态不能直接套用（uid 可能不存在），保留备份并提示
      try {
        store.writeJSON(store.KEYS.legacy, { at: Date.now(), reason: 'book-version-mismatch', state: saved });
      } catch (e) {
        /* 忽略 */
      }
      toast('本地状态与当前账面版本不一致，已备份到「待核实」，请核对后重新盘点', true);
    }

    state.st = st;
    state.giStatus = 'idle';
    state.giCount = 0;
    state.giToken = '';
    state.date = book.date;
    state.storeId = book.storeId;
    state.storeName = book.storeName;
    $('#date').value = book.date;
    renderStoreOptions();
    $('#store').value = book.storeId;
    $('#setup').classList.add('hidden');
    $('#scan-card').classList.remove('hidden');
    $('#results-card').classList.remove('hidden');
    render();
    focusScan();
    setSaveStatus('saved');
    const fetched = book.fetchedAt ? core.fmtTime(book.fetchedAt) : '未知';
    toast(
      `已从本地账面恢复：${st.storeName} ${st.date}（账面 ${st.items.length} 行，拉取于 ${fetched}，` +
        `${st.scans.length} 条扫码记录）`
    );
    if (state.loadGlobalIndex && state.giStatus !== 'ready' && api.isConfigured(state.cfg)) {
      loadGlobalIndexBg();
    }
    // 注意：除了上面这条可选的索引查询，续盘本身不做任何网络请求 —— 断网也能完成。
    // 需要换店时，用户打开「接口设置」会自动去拉一次仓库列表（见 bind）。
    return true;
  }

  /**
   * 旧版本记录迁移：
   * 旧数据没有保存原始账面，无法保证手工记录与当前商品对应。
   * 这里先保留备份，然后重新拉一次账面，**只按串号重新核对扫码记录**；
   * 手工数量、确认、备注一律列入「待核实」，不自动继承。
   */
  async function resumeFromLegacy(legacy) {
    busy('正在迁移旧版本记录…', `${legacy.storeName || ''} ${legacy.date || ''}`);
    try {
      if (!api.isConfigured(state.cfg)) {
        openSettings('检测到旧版本盘点记录，但需要先登录才能重建账面并核对串号', 'warn');
        return false;
      }
      const res = await withAuth(() =>
        api.fetchInventory(state.cfg, { date: legacy.date, storeId: legacy.storeId, label: '库存查询' })
      );
      const items = core.normalizeAll(res.rows, legacy.storeId);
      const st = new core.Stocktake({
        date: legacy.date,
        storeId: legacy.storeId,
        storeName: legacy.storeName,
        companycode: state.cfg.companycode,
        items: items,
        startedAt: legacy.startedAt,
        bookVersion: 1,
        bookFetchedAt: Date.now(),
      });
      // 只重放扫码（按串号，安全）
      const scans = legacy.scans || [];
      scans.forEach((r) => st.scan(r.code, r.ts));

      // 手工记录无法确认对应关系 → 全部进待核实
      const review = [];
      Object.keys(legacy.manualFound || {}).forEach((k) =>
        review.push({ kind: '手工确认', key: k, ts: (legacy.manualFound[k] || {}).ts || 0 })
      );
      Object.keys(legacy.manualQty || {}).forEach((k) =>
        review.push({ kind: '无串号实盘数量', key: k, value: legacy.manualQty[k] })
      );
      Object.keys(legacy.manualNotes || {}).forEach((k) =>
        review.push({ kind: '确认备注', key: k, value: legacy.manualNotes[k] })
      );
      st.pendingReview = review;

      store.saveBook({
        sessionId: st.sessionId,
        companycode: state.cfg.companycode,
        storeId: legacy.storeId,
        storeName: legacy.storeName,
        date: legacy.date,
        version: 1,
        fetchedAt: st.bookFetchedAt,
        items: core.bookFromItems(items),
      });
      state.st = st;
      state.date = legacy.date;
      state.storeId = legacy.storeId;
      state.storeName = legacy.storeName;
      $('#date').value = legacy.date;
      renderStoreOptions();
      $('#store').value = legacy.storeId;
      $('#setup').classList.add('hidden');
      $('#scan-card').classList.remove('hidden');
      $('#results-card').classList.remove('hidden');
      render();
      saveSession();
      toast(
        `旧记录已迁移：按串号核对回 ${scans.length} 条扫码；${review.length} 条手工记录（数量/确认/备注）进入「待核实」，请人工核对`,
        true
      );
      return true;
    } catch (e) {
      toast('旧记录迁移失败：' + e.message + '（原始数据已备份本机，不会丢）', true);
      return false;
    } finally {
      unbusy();
    }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    // 初始化**界面偏好** —— 凭据不在这儿（那是后端的事，见文件头第 3 条）
    const saved = lsGet(LS_CFG);
    if (saved) {
      if (typeof saved.sound === 'boolean') state.sound = saved.sound;
      if (typeof saved.loadGlobalIndex === 'boolean') state.loadGlobalIndex = saved.loadGlobalIndex;
      if (typeof saved.loadInTransit === 'boolean') state.loadInTransit = saved.loadInTransit;
    } else {
      // 旧版本（含上游单文件版）留下的配置：只带走偏好，**凭据一概不读**，
      // 顺手把它从本机抹掉 —— 那是上一版的 token / 密码，这一版用不着也不该留。
      const old = lsGet(LS_CFG_OLD);
      if (old) {
        if (typeof old.sound === 'boolean') state.sound = old.sound;
        if (typeof old.loadGlobalIndex === 'boolean') state.loadGlobalIndex = old.loadGlobalIndex;
      }
      if (saved && (saved.token || saved.password || saved.account)) {
        setLoginStatus('已清掉上一版存在本机浏览器里的云商 token / 密码 —— '
                     + '这一版账号统一在控制台「通用设置 › 云商账号」里配', 'warn');
      }
      saveCfg();
    }
    $('#date').value = core.todayStr();
    $('#opt-sound').checked = state.sound;
    $('#opt-index').checked = state.loadGlobalIndex;
    $('#opt-transit').checked = state.loadInTransit;

    $('#btn-load-wh').onclick = loadWarehouses;
    $('#store-filter').oninput = renderStoreOptions;
    $('#btn-start').onclick = () => loadInventory();

    // ---- 后端身份：没有账号密码框了，只有一个「重新读取」 ----
    $('#btn-relogin').onclick = () => {
      reloadIdentity({}).then((d) => {
        if (d && d.ok) loadWarehouses();
      });
    };
    $('#btn-settings').onclick = () => {
      $('#setup').classList.toggle('hidden');
      if ($('#setup').classList.contains('hidden')) return;
      // 打开设置时才按需拉仓库列表（续盘本身不联网）
      if (!state.warehouses.length && api.isConfigured(state.cfg)) loadWarehouses();
      else $('#date').focus();
    };
    $('#btn-gi').onclick = () => {
      state.giStatus = 'idle';
      loadGlobalIndexBg();
    };
    $('#btn-new').onclick = () => {
      // 先把正在编辑的内容提交掉，再判断"有没有内容"
      commitFocusedInput();
      const st = state.st;
      if (st && st.hasContent() && !confirm('确定要结束本次盘点吗？\n扫码、手工确认、实盘数量和备注都会从本机清除。')) return;
      cancelPendingSave(); // 取消没执行的保存任务，避免它把数据又写回来
      store.clearState();
      store.clearBook();
      state.st = null;
      state.lastResult = null;
      state.giStatus = 'idle';
      state.giCount = 0;
      state.pendingReview = [];
      $('#setup').classList.remove('hidden');
      $('#scan-card').classList.add('hidden');
      $('#results-card').classList.add('hidden');
      const diffCard = $('#book-diff-card');
      if (diffCard) diffCard.classList.add('hidden');
      setSessionHeader();
      setSaveStatus('idle');
      toast('已结束本次盘点，可重新选择仓库开始');
    };

    $('#btn-apply-book').onclick = () => applyBookUpdate();
    $('#btn-cancel-book').onclick = () => {
      pendingBookUpdate = null;
      const card = $('#book-diff-card');
      if (card) card.classList.add('hidden');
      toast('已放弃本次账面更新，当前账面未变');
    };

    // 下载会话备份（保存失败时的保命通道）
    $('#btn-backup').onclick = downloadBackup;

    // 加载未完成盘点（开头那个按钮）
    $('#btn-load-backup').onclick = () => $('#backup-file').click();
    $('#backup-file').addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) loadBackupFile(f);
    });

    // 更新账面：明确的独立操作，保留旧账面为上一版
    $('#btn-update-book').onclick = () => updateBook();

    // 配置项：只有**界面偏好**（凭据由后端管，这一页没有可配的账号字段）
    $('#opt-sound').addEventListener('change', (e) => {
      state.sound = e.target.checked;
      saveCfg();
    });
    $('#opt-index').addEventListener('change', (e) => {
      state.loadGlobalIndex = e.target.checked;
      saveCfg();
    });
    $('#opt-transit').addEventListener('change', (e) => {
      state.loadInTransit = e.target.checked;
      saveCfg();
    });

    // 扫码输入
    const si = $('#scan-input');
    si.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const v = si.value;
        si.value = '';
        handleScanInput(v);
      } else if (e.key === 'Escape') {
        si.value = '';
        state.lastResult = null;
        renderScanCard();
      }
    });
    si.addEventListener('blur', () => {
      // 稍后自动抢回焦点，保证扫码枪随时可用
      setTimeout(() => {
        if (!$('#scan-card').classList.contains('hidden') && !state.busy) {
          const ae = document.activeElement;
          const tag = ae ? ae.tagName : '';
          if (tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA' && tag !== 'BUTTON') si.focus();
        }
      }, 120);
    });

    $('#btn-undo').onclick = undoLast;

    // 「最近扫描」里每条后面的 ✕（它在扫码卡片里，不在结果卡片上，所以要单独委托）
    $('#scan-card').addEventListener('click', (e) => {
      const act = e.target.closest && e.target.closest('[data-act="unscan-at"]');
      if (!act || !state.st) return;
      const idx = Number(act.getAttribute('data-key'));
      const rec = state.st.removeScan(idx);
      if (!rec) return;
      toast(`已删除这一条：${rec.code}`);
      state.lastResult = null;
      saveSession();
      render();
    });
    $('#btn-export').onclick = exportXlsx;
    $('#btn-export-csv').onclick = exportCsv;
    // ⭐ 「导出并推送」—— 接进控制台后新加的那个按钮（M16）。
    // 跟上面那个用**同一份 xlsx 字节**，只是一条存本机、一条推出去。
    $('#btn-push').onclick = () => exportAndPush();
    $('#btn-print').onclick = () => window.print();

    // 结果区事件委托
    $('#results-card').addEventListener('click', (e) => {
      const tabBtn = e.target.closest('[data-tab]');
      if (tabBtn) {
        commitFocusedInput(); // 切页前先把没失焦的输入提交掉
        const next = tabBtn.getAttribute('data-tab');
        const same = state.tab === next;
        state.tab = next;
        renderResults();
        // 点的是当前这一页时不抢焦点（否则正在填数字的人会被弹回扫码框）
        if (!same) focusScan();
        return;
      }
      const act = e.target.closest('[data-act]');
      if (act) {
        const a = act.getAttribute('data-act');
        if (a === 'show-all') {
          state.showAll[state.tab] = true;
          renderResults();
        } else if (a === 'export-tab') {
          exportTabCsv();
        } else if (a === 'confirm') {
          const key = act.getAttribute('data-key');
          const it = state.st.confirmFound(key);
          if (it) {
            toast(`已手工确认：${cut(it.name, 24)}`);
            beep('ok');
          }
          saveSession();
          render();
          focusScan();
        } else if (a === 'confirm-all') {
          const keys = bulkPendingKeys();
          if (!keys.length) {
            toast('没有「填了实盘数量但还没确认」的行');
            return;
          }
          if (
            !confirm(
              `将把已填实盘数量的 ${keys.length} 行无串号商品标记为已确认，\n确认后这些行会移到「已扫明细」（可逐条撤销）。确定继续？`
            )
          )
            return;
          const done = state.st.confirmAllFilledNonSerial(Date.now(), keys);
          saveSession();
          render();
          focusScan();
          toast(`已确认 ${done.length} 行无串号商品`);
        } else if (a === 'unscan') {
          const key = act.getAttribute('data-key');
          const it = state.st.itemByKey.get(key);
          const rec = state.st.undoScanForItem(key);
          if (rec) {
            toast(`已撤销这一扫：${rec.code}${it ? '（' + cut(it.name, 20) + '）' : ''}`);
            saveSession();
            render();
            focusScan();
          } else {
            toast('没有找到这台的扫码记录');
          }
        } else if (a === 'unscan-code') {
          const code = act.getAttribute('data-key');
          const n = state.st.removeCode(code);
          toast(`已删除 ${code} 的 ${n} 条扫描记录`);
          saveSession();
          render();
          focusScan();
        } else if (a === 'unscan-at') {
          const idx = Number(act.getAttribute('data-key'));
          const rec = state.st.removeScan(idx);
          if (rec) {
            toast(`已删除这一条：${rec.code}`);
            state.lastResult = null;
            saveSession();
            render();
          }
        } else if (a === 'unconfirm') {
          const key = act.getAttribute('data-key');
          const it = state.st.unconfirmFound(key);
          if (it) toast(`已撤销手工确认：${cut(it.name, 24)}`);
          saveSession();
          render();
          focusScan();
        } else if (a === 'filter-demo') {
          state.search[state.tab] = state.search[state.tab] === '样' ? '' : '样';
          renderResults();
        }
      }
    });
    $('#results-card').addEventListener('input', (e) => {
      // 正在填实盘数量：按钮上的待提交行数跟着走
      if (e.target.getAttribute && e.target.getAttribute('data-kind') === 'qty') {
        refreshNonSerialBar();
        return;
      }
      if (e.target.id === 'search') {
        state.search[state.tab] = e.target.value;
        const pos = e.target.selectionStart;
        renderResults();
        const el = $('#search');
        if (el) {
          el.focus();
          try {
            el.setSelectionRange(pos, pos);
          } catch (err) {}
        }
      }
    });
    $('#results-card').addEventListener('change', (e) => {
      const t = e.target;
      if (!state.st) return;
      const kind = t.getAttribute && t.getAttribute('data-kind');
      if (!kind) return;
      const key = t.getAttribute('data-key');
      if (kind === 'qty') {
        commitQty(t);
      } else if (kind === 'note') {
        state.st.remarks[key] = t.value;
        saveSession();
      } else if (kind === 'mnote') {
        state.st.setManualNote(key, t.value);
        saveSession();
      }
    });

    // 表格里的输入框：Tab / 回车 跳到「下一行的同一列」，方便一列一列往下填
    $('#results-card').addEventListener('keydown', (e) => {
      const el = e.target;
      if (!el || !el.getAttribute || !el.getAttribute('data-kind')) return;
      if (e.key !== 'Tab' && e.key !== 'Enter') return;
      e.preventDefault();
      if (el.getAttribute('data-kind') === 'qty') commitQty(el);
      moveSameColumn(el, e.shiftKey ? -1 : 1);
    });

    // 全局按键：不在输入框里打字时，自动把焦点送回扫码框
    document.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const tag = e.target && e.target.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
      if (e.key.length === 1) focusScan();
    });
  }

  /* ---------------- 嵌在控制台（2026-09-22 已拆 iframe）----------------
     markup 在 `index.html` 的 `.inv-root` 里，同文档、直接吃 `body[data-theme]`。
     下面的 postMessage 兼容层**故意留着**：单独打开 `/inventory.html` 时
     仍可能被别的窗口通知；同文档场景下收不到也无妨。 */

  /** `?embed=1`：藏掉自己的大标题和「回控制台」—— 嵌在控制台里不用再自我介绍。
      同文档路径由 CSS `#subpanel-inventory .inv-root` 负责，不看这个 class。 */
  function applyEmbed() {
    let embed = false;
    try {
      embed = new URLSearchParams(location.search).get('embed') === '1';
    } catch (e) {}
    // 同文档时别往 body 上加 embed —— 会盖掉整个控制台
    if (embed && !document.getElementById('subpanel-inventory')) {
      document.body.classList.toggle('embed', embed);
    }
    return embed || !!document.getElementById('subpanel-inventory');
  }

  /**
   * 套上控制台那套主题（`body[data-theme]`）。
   * 同文档时父页已经设好；这里留给跨标签页 localStorage / 单独打开。
   * 空 / 'default' = 删属性。
   */
  function applyThemeName(name) {
    if (name) document.body.setAttribute('data-theme', String(name));
    else document.body.removeAttribute('data-theme');
  }

  function onParentTheme(e) {
    if (e.origin && e.origin !== location.origin) return;
    const d = e.data || {};
    if (!d || d.ic !== 'theme') return;
    applyThemeName(d.name || '');
  }

  /** 兼容：若仍有父页转按键（单独打开的旧路径），补进扫码框。 */
  function onParentKey(e) {
    if (e.origin && e.origin !== location.origin) return;      // 只认同源
    const d = e.data || {};
    if (!d || d.ic !== 'scan-key') return;
    const si = $('#scan-input');
    if (!si || $('#scan-card').classList.contains('hidden')) return;   // 还没进扫码台，不接
    focusScan();                    // ⚠ 先抢焦点（它内部会 select()，所以补字必须在后）
    if (typeof d.key === 'string' && d.key.length === 1) {
      si.value += d.key;
      try {
        si.setSelectionRange(si.value.length, si.value.length);
      } catch (err) {}
    }
  }

  /* ---------------- 启动 ----------------
     ⚠ 脚本是**懒注入**的（app.js 的 mountInventory）—— 注入时
       `DOMContentLoaded` 往往已经过去了，必须自己看 readyState 再跑 main。 */
  async function main() {
    fillFootVer();          // 页脚那行版本号（用户自查「拿到的是新版吗」）
    applyEmbed();
    bind();
    setSessionHeader();
    window.addEventListener('message', onParentKey);
    // 主题：同文档已生效；storage 留给跨标签页 / 单独打开
    window.addEventListener('message', onParentTheme);
    window.addEventListener('storage', (ev) => {
      if (ev.key === 'cbg-theme') applyThemeName(ev.newValue || '');
    });

    // ⚠ 顺序有讲究：**先问后端身份**（快、能失败得明白），再谈续盘。
    //   反过来的话，后端没配账号时用户看到的是一屏"还没登录"，却不知道该去哪儿配。
    await reloadIdentity({ silent: true });

    // 从本地冻结账面恢复：这一步不联网，断网/后端账号过期都能继续盘
    const resumed = await tryResume();
    if (resumed) return;

    if (!api.isConfigured(state.cfg)) {
      // 后端没配云商账号：不发任何取数请求，把"去哪儿配"摆出来
      $('#setup').classList.remove('hidden');
      // ⚠ 那个折叠的「接口设置」块删了（2026-09-21），不用再把它展开 ——
      //   账号那一行现在直接就在这张卡里，`setLoginStatus` 写着话。
      const box = $('#login-status');
      if (box && !box.textContent) {
        setLoginStatus('这台机器还没配云商账号 —— 去控制台「通用设置 › 云商账号」填一次，'
                     + '回来点「重新读取」', 'err');
      }
      return;
    }

    // 没有可恢复的会话才需要仓库列表（用来开新盘点）
    try {
      state.warehouses = await withAuth(() => api.fetchWarehouses(state.cfg));
      renderStoreOptions();
      if (state.loginStatusKind === 'ok') setLoginStatus(state.loginStatus, 'ok');
    } catch (e) {
      if (e.notConfigured) {
        /* 已在上面的分支处理 */
      } else {
        toast('仓库列表读取失败：' + e.message, true);
      }
    }
    if (!state.st) $('#setup').classList.remove('hidden');
  }

  // 供测试/排查使用
  /* ⚠⚠ 自己的 `?v=` 要**在顶层**读（2026-09-21）：
     `document.currentScript` 只在脚本**同步执行**时有值，等到 DOMContentLoaded
     已经是 null 了 —— 所以现在抓、到 `main()` 里再填 DOM。 */
  const SELF_V = (() => {
    try {
      const src = (document.currentScript && document.currentScript.src) || '';
      const m = src.match(/[?&]v=(\d+)/);
      return m ? 'v' + m[1] : '';
    } catch (e) { return ''; }
  })();

  window.ICUI = {
    loadBackupFile: (f) => loadBackupFile(f),
    updateBook: () => updateBook(),
    applyBookUpdate: () => applyBookUpdate(),
    downloadBackup: () => downloadBackup(),
    exportAndPush: () => exportAndPush(),
    state: () => state,
  };

  function fillFootVer() {
    const el = $('#foot-ver');
    if (!el) return;
    el.textContent = SELF_V
      ? '前端资源 ' + SELF_V + ' · 这一页是这次加载的版本'
      : '⚠ 前端资源没带版本号（拿到旧文件的典型症状）';
  }

  if (document.readyState === 'loading') {
    window.addEventListener('DOMContentLoaded', main);
  } else {
    // 懒注入（拆 iframe 后）：DOM 已经好了，DOMContentLoaded 过了也得跑
    main();
  }
})();
