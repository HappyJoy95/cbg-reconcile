'use strict';

/* 权益领取页面：只取消页面查询；用户主动提交的操作按原请求继续完成。 */
let claimPageMounted = false;
let claimPageEpoch = 0;
const claimReadControllers = new Set();

async function claimRead(path) {
  if (!claimPageMounted) return null;
  const epoch = claimPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  if (controller) claimReadControllers.add(controller);
  try {
    const result = await api(path, controller ? { signal: controller.signal } : {});
    return claimPageMounted && epoch === claimPageEpoch ? result : null;
  } catch (e) {
    if (!claimPageMounted || epoch !== claimPageEpoch || (e && e.name === 'AbortError')) {
      return null;
    }
    throw e;
  } finally {
    if (controller) claimReadControllers.delete(controller);
  }
}

/* ── 权益领取（小工具）：活动一览 / 待领清单（2026-09-22） ── */
let _claimActsAll = [];
let _claimShowExpired = false;

function renderClaimActivities() {
  const meta = $('#claim-act-meta');
  const showOut = !!$('#claim-act-show-expired')?.checked;
  _claimShowExpired = showOut;
  const list = (_claimActsAll || []).filter((a) => showOut || !a.expired);
  if (meta) {
    const nAct = (_claimActsAll || []).filter((a) => !a.expired).length;
    const nOut = (_claimActsAll || []).filter((a) => a.expired).length;
    meta.textContent = showOut
      ? (' · 全部 ' + _claimActsAll.length + ' 场（含过期 ' + nOut + '）')
      : (' · ' + nAct + ' 场进行中' + (nOut ? ' · 过期 ' + nOut + ' 已隐藏' : ''));
  }
  // ⚠ `table()` 要的是**数组行**（或 {cells:[…]}），不是对象 map。
  const head = ['品类', '活动', '机型', '活动时间', '赠送权益',
                '权益价值', '出险收费', '领取链接', '领取后还可办理'];
  // 列 class：领取链接加宽（用户：一个字符太挤）
  const colCls = ['claim-act-cat', 'claim-act-title', 'claim-act-match',
                  'claim-act-time', 'claim-act-benefit', 'claim-act-value',
                  'claim-act-fee', 'claim-act-url', 'claim-act-after'];
  const rows = list.map((a) => {
    const st = a.expired
      ? ' style="text-decoration:line-through;opacity:.65"'
      : '';
    const link = a.url && /^https?:/.test(a.url)
      ? { html: '<a href="' + esc(a.url) + '" target="_blank" rel="noopener"'
          + (a.expired ? ' style="text-decoration:line-through;opacity:.65"' : '')
          + '>打开</a>', cls: 'claim-act-url' }
      : { html: esc(a.url || ''), cls: 'claim-act-url' };
    return [
      { html: esc(a.category || ''), cls: 'claim-act-cat' },
      { html: '<b' + st + '>' + esc(a.title || a.id || '') + '</b>',
        cls: 'claim-act-title' },
      { html: esc((a.match || []).join(' / ')), cls: 'claim-act-match' },
      { html: esc((a.start || '…') + ' ~ ' + (a.end || '…')),
        cls: 'claim-act-time' },
      { html: esc(a.benefit || ''), cls: 'claim-act-benefit' },
      { html: esc(a.value || ''), cls: 'claim-act-value' },
      { html: esc(a.fee || ''), cls: 'claim-act-fee' },
      link,
      { html: esc(a.after_claim || ''), cls: 'claim-act-after' },
    ];
  });
  const el = $('#claim-act-table');
  if (el) {
    el.innerHTML = rows.length
      ? '<div class="table-scroll">' + table(head, rows, colCls) + '</div>'
      : '<p class="hint">没有活动 —— 检查 config/benefit-claim.yaml。</p>';
  }
}

async function loadClaimActivities() {
  try {
    const d = await claimRead('/api/claim/activities');
    if (!d || !claimPageMounted) return;
    _claimActsAll = d.activities || [];
    renderClaimActivities();
  } catch (e) {
    const el = $('#claim-act-table');
    if (el) el.innerHTML = '<p class="hint">读取失败：' + esc(e.message) + '</p>';
    toast('读取活动失败：' + e.message, 'bad');
  }
}

let _claimPendingRows = [];
/** 当前视图：pending=待领（默认）· claimed=已领取 */
let _claimView = 'pending';
/** 每页最多 50 条（用户 2026-09-23） */
const CLAIM_PAGE_SIZE = 50;
let _claimPage = 1;

// ── 批量多选 + 自动领取（2.2.4）──────────────────────────────
/** 已勾选的行，按 `status_key` 存 —— 表格是 `innerHTML` 重画的，
 *  不按 key 存的话翻页/筛选一刷就全没了（勾了半天白勾）。 */
const _claimSelected = new Set();
/** 批量进行中：禁按钮 + 挡重入（连点两下 = 两轮并发提交）。 */
let _claimBatchRunning = false;
/** 条与条之间的间隔 —— 别把华为网关打成连环炮。 */
const CLAIM_BATCH_GAP_MS = 300;
/** 并行度（2026-09-29 用户：「改成两条并行吧」）。
 *  ⚠ **只上 2 条**：后端 `claim-status.json` 是整份读改写（已加进程内锁，但仍按
 *  "少而稳"来）；华为网关对同账号连环提交也怕限频。要再提速得先做服务端批量。 */
const CLAIM_BATCH_LANES = 2;

async function loadClaimPending() {
  try {
    const d = await claimRead('/api/claim/pending');
    if (!d || !claimPageMounted) return;
    const meta = $('#claim-pending-meta');
    if (meta) {
      const bits = [];
      if (d.as_of) bits.push('数据截至 ' + String(d.as_of).slice(0, 16));
      if (d.store_filter) bits.push(d.store_filter);
      meta.textContent = bits.length ? ' · ' + bits.join(' · ') : '';
    }
    const sum = $('#claim-pending-summary');
    if (sum && d.summary) {
      const s = d.summary;
      sum.innerHTML = '合计 <b>' + s.total + '</b> · 待领 <b>' + s.pending
        + '</b> · 已领 <b>' + s.claimed + '</b>'
        + (s.na ? ' · 不适用 <b>' + s.na + '</b>' : '')
        + ' · 已领率 ' + (Math.round((s.claimed_rate || 0) * 1000) / 10) + '%';
    }
    const scopeBox = $('#claim-pending-scope');
    if (scopeBox) {
      const bits = [];
      if (d.scope) bits.push('身份：' + d.scope);
      if (d.store_filter) bits.push('范围：' + d.store_filter);
      else if (d.scoped === false) bits.push('范围：全部门店');
      scopeBox.textContent = bits.join('　·　')
        + '　（待领清单按登录身份滤店，只能看自己范围内的）';
    }
    _claimPendingRows = d.rows || [];
    fillClaimFilterOpts(_claimPendingRows);
    renderClaimPendingTable(d.note || '');
    const leg = $('#claim-pending-legend');
    if (leg && d.note) leg.innerHTML = esc(d.note)
      + '<br>匹配口径：整机品类 + 商品名命中活动机型 + 支付日在赠送期内；'
      + '手提袋/周边/延保单、退货不进待领。状态落 <code>out/claim-status.json</code>。';
  } catch (e) {
    const el = $('#claim-pending-table');
    if (el) el.innerHTML = '<p class="hint">读取失败：' + esc(e.message) + '</p>';
    toast('读取待领失败：' + e.message, 'bad');
  }
}

function claimStatusHtml(r) {
  const st = r.status || 'pending';
  const label = r.status_label
    || (st === 'claimed' ? '已领' : st === 'na' ? '不适用' : '待领');
  const mod = st === 'claimed' ? 'claimed' : st === 'na' ? 'na' : 'pending';
  return '<span class="claim-st claim-st-' + mod + '">' + esc(label) + '</span>';
}

function fillClaimFilterOpts(rows) {
  const fill = (sel, allLabel, field, fmt) => {
    if (!sel) return;
    const prev = sel.value;
    const vals = [];
    (rows || []).forEach((r) => {
      const v = fmt ? fmt(r) : (r[field] || '');
      if (v && vals.indexOf(v) < 0) vals.push(v);
    });
    vals.sort((a, b) => String(a).localeCompare(String(b), 'zh'));
    const keep = ['<option value="">' + esc(allLabel) + '</option>'];
    vals.forEach((c) => {
      keep.push('<option value="' + esc(c) + '">' + esc(c) + '</option>');
    });
    sel.innerHTML = keep.join('');
    if (prev && vals.indexOf(prev) >= 0) sel.value = prev;
  };
  fill($('#claim-filter-cat'), '全部品类', null,
    (r) => r.category || '未分类');
  fill($('#claim-filter-store'), '全部门店', 'store');
}

function claimFilterRows(rows) {
  const cat = ($('#claim-filter-cat') || {}).value || '';
  const store = ($('#claim-filter-store') || {}).value || '';
  const q = (($('#claim-filter-model') || {}).value || '').trim().toLowerCase();
  return (rows || []).filter((r) => {
    // 待领 / 已领取 切换
    if (_claimView === 'claimed') {
      if (r.status !== 'claimed') return false;
    } else if (_claimView === 'pending') {
      if (r.status === 'claimed') return false;
    }
    if (cat && (r.category || '未分类') !== cat) return false;
    if (store && String(r.store || '') !== store) return false;
    if (q) {
      const hay = [
        r.name, r.activity_title, r.benefit, r.category, r.store, r.who,
      ].join(' ').toLowerCase();
      if (hay.indexOf(q) < 0) return false;
    }
    return true;
  });
}

function setClaimPage(n) {
  _claimPage = Math.max(1, n | 0);
  renderClaimPendingTable('');
}

function renderClaimPendingTable(note) {
  const filtered = claimFilterRows(_claimPendingRows);
  const total = filtered.length;
  const pages = Math.max(1, Math.ceil(total / CLAIM_PAGE_SIZE));
  if (_claimPage > pages) _claimPage = pages;
  const start = (_claimPage - 1) * CLAIM_PAGE_SIZE;
  const pageRows = filtered.slice(start, start + CLAIM_PAGE_SIZE);

  const head = ['', '门店', '店员', '商品', '支付时间', '活动', '权益', '价值', '状态', '操作'];
  // 列 class：店员加宽、商品收窄（2026-09-23）；首列 = 批量多选（2.2.4）
  const colCls = ['claim-col-pick', 'claim-col-store', 'claim-col-who', 'claim-col-name',
                  'claim-col-ts', 'claim-col-act', 'claim-col-benefit',
                  'claim-col-value', 'claim-col-status', 'claim-col-acts'];
  const rows = pageRows.map(r => [
    { html: claimPickHtml(r), cls: 'claim-col-pick' },
    { html: esc(r.store || ''), cls: 'claim-col-store' },
    { html: esc(r.who || ''), cls: 'claim-col-who' },
    { html: '<b>' + esc(r.name || '') + '</b>'
      + (r.sn ? '<br><span class="hint">'
        + (r.sn_kind === 'imei' || /^\d{15}$/.test(r.sn)
          ? '86码 ' + esc(r.sn)
            + (r.claim_sn ? ' → SN ' + esc(r.claim_sn) : '')
          : esc(r.sn))
        + '</span>' : ''),
      cls: 'claim-col-name' },
    { html: esc(String(r.ts || '').slice(0, 16)), cls: 'claim-col-ts' },
    { html: esc(r.activity_title || ''), cls: 'claim-col-act' },
    { html: esc(r.benefit || ''), cls: 'claim-col-benefit' },
    { html: esc(r.value || ''), cls: 'claim-col-value' },
    { html: claimStatusHtml(r), cls: 'claim-col-status' },
    { html: claimActionsHtml(r), cls: 'claim-col-acts' },
  ]);

  const cnt = $('#claim-filter-count');
  if (cnt) {
    const viewLabel = _claimView === 'claimed' ? '已领取' : '待领';
    cnt.textContent = viewLabel + ' ' + total + ' 条'
      + (total > CLAIM_PAGE_SIZE
        ? ' · 本页 ' + pageRows.length
          + '（' + (start + 1) + '–' + (start + pageRows.length) + '）'
        : '');
  }
  const pageInfo = $('#claim-page-info');
  if (pageInfo) {
    pageInfo.textContent = total
      ? ('第 ' + _claimPage + '/' + pages + ' 页')
      : '—';
  }
  const prev = $('#claim-page-prev');
  const next = $('#claim-page-next');
  if (prev) prev.disabled = _claimPage <= 1 || total === 0;
  if (next) next.disabled = _claimPage >= pages || total === 0;

  // 视图 seg 高亮
  $$('#claim-status-seg .seg-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.claimView === _claimView);
  });

  const el = $('#claim-pending-table');
  if (!el) return;
  const emptyTip = _claimView === 'claimed'
    ? '没有已领取记录。'
    : (note || '本窗口没有待领记录。');
  el.innerHTML = pageRows.length
    ? '<div class="table-scroll">' + table(head, rows, colCls) + '</div>'
    : '<p class="hint">' + esc(
        total === 0 && (_claimPendingRows || []).length
          ? '筛选后没有记录 —— 换视图/门店/品类，或清空机型关键词。'
          : emptyTip) + '</p>';
  bindClaimActions(el);
  bindClaimBatchRows(el);        // 行首勾选（2.2.4）
  syncClaimBatchBar();           // 已选数 / 按钮状态跟着重画走
  // 翻页后滚回表顶
  const sc = el.querySelector('.table-scroll');
  if (sc) sc.scrollTop = 0;
}

/** 这一行能不能「在线领取」—— **单条按钮和批量勾选共用这一份判据**（2.2.4）。
 *
 * ⚠ 抽出来是因为原来只有 `claimActionsHtml` 里那一份：批量要是另写一份，
 *   迟早走散（AGENTS 坑 12 同类）—— 表现是「勾得上但点不了」或「能点却勾不上」，
 *   页面上看着只是偶发，查起来最费劲。
 * 返回：`online` 真 = 能自动领（`onlineSn` 就是提交用的 SN）；
 *       `reason` = 不能领的原因（给灰掉的勾当 title）。
 */
function claimRowUi(r) {
  const rawSn = String(r.sn || '');
  const claimSn = String(r.claim_sn || '');
  const url = String(r.url || '').trim();
  const canOpen = /^https?:\/\//i.test(url);
  // 86码（15位纯数字）≠ SN：有 claim_sn 才谈在线领
  const isImei = r.sn_kind === 'imei' || /^\d{15}$/.test(rawSn);
  const onlineSn = claimSn || (isImei ? '' : rawSn);
  // ⭐ 没有有效领取链接（空 / 「输出中」/「不涉及」）→ **在线也灰**（用户 2026-09-23：
  //   「没链接的，手动领取灰的，自动领取也应该灰色」）
  const online = !!(canOpen && onlineSn && onlineSn.indexOf('nosn:') !== 0
    && r.status !== 'claimed');
  let reason = '';
  if (r.status === 'claimed') reason = '已领取';
  else if (!canOpen) reason = '该活动暂无有效领取链接（' + (url || '空') + '）';
  else if (isImei && !claimSn) {
    reason = '云商串号是86码(IMEI)，库存也没反查到真SN —— 请手动领取/标已领';
  } else if (onlineSn.indexOf('nosn:') === 0) reason = '这条没有真 SN，不能在线领';
  return { canOpen, isImei, onlineSn, online, noUrl: !canOpen,
           reason: online ? '' : (reason || '不可在线领取') };
}

function claimActionsHtml(r) {
  const k = esc(r.status_key || '');
  const aid = esc(r.activity_id || '');
  const url = String(r.url || '').trim();
  const ui = claimRowUi(r);
  const canOpen = ui.canOpen;
  const isImei = ui.isImei;
  const onlineSn = ui.onlineSn;
  const online = ui.online;
  const noUrl = ui.noUrl;
  // 两行（用户 2026-09-23）：
  //   第 1 行：在线领取 · 标已领
  //   第 2 行：手动领取（第 3 个）· 不适用
  let row1 = '';
  let row2 = '';
  if (r.status !== 'claimed' && (isImei || noUrl) && !online) {
    const tip = noUrl
      ? '该活动暂无有效领取链接（' + (url || '空') + '）—— 在线/手动都不可用，可「标已领」或「不适用」'
      : '云商串号是86码(IMEI)，库存也没反查到真SN —— 请手动领取/标已领';
    row1 += '<button class="btn ghost small" disabled'
      + ' title="' + esc(tip) + '">在线领取</button>';
  } else if (online) {
    row1 += '<button class="btn ghost small" data-claim-online="1"'
      + ' data-sn="' + esc(onlineSn) + '" data-aid="' + aid + '" data-key="' + k + '"'
      + ' data-name="' + esc(r.name || '') + '"'
      + ' data-title="' + esc(r.activity_title || '') + '"'
      + ' data-store="' + esc(r.store || '') + '">在线领取</button>';
  }
  if (r.status === 'claimed') {
    // 已领取：只留撤销，并贴操作列**最右**（用户 2026-09-23）
    row1 = '<button class="btn ghost small" data-claim-key="' + k
      + '" data-claim-st="pending">撤销</button>';
    row2 = '';
    return '<div class="claim-acts claim-acts-end">'
      + '<div class="claim-acts-row">' + row1 + '</div></div>';
  } else {
    row1 += (row1 ? ' ' : '')
      + '<button class="btn ghost small" data-claim-key="' + k
      + '" data-claim-st="claimed">标已领</button>';
    // 第 3 个：手动领取
    if (canOpen) {
      row2 += '<button class="btn ghost small" data-claim-manual="1"'
        + ' data-url="' + esc(url) + '"'
        + ' title="打开华为官方领取页：' + esc(url) + '">手动领取</button>';
    } else {
      row2 += '<button class="btn ghost small" disabled'
        + ' title="' + esc(url || '该活动暂无在线领取链接') + '">手动领取</button>';
    }
    row2 += ' <button class="btn ghost small" data-claim-key="' + k
      + '" data-claim-st="na">不适用</button>';
  }
  if (!row1 && !row2) return '';
  // 待领/已领操作块都贴操作列**最右**（用户 2026-09-23）
  return '<div class="claim-acts claim-acts-end">'
    + (row1 ? '<div class="claim-acts-row">' + row1 + '</div>' : '')
    + (row2 ? '<div class="claim-acts-row">' + row2 + '</div>' : '')
    + '</div>';
}

// ── 批量多选的行首勾选 / 工具条 / 批量执行（2.2.4）──────────────

/** 行首那个勾：**能自动领的才可点**，否则 disabled + title 写清为什么灰。 */
function claimPickHtml(r) {
  const k = String(r.status_key || '');
  if (!k) return '';
  const ui = claimRowUi(r);
  if (!ui.online) {
    return '<input type="checkbox" class="claim-pick" disabled'
      + ' title="' + esc(ui.reason) + '" aria-label="' + esc(ui.reason) + '">';
  }
  const on = _claimSelected.has(k) ? ' checked' : '';
  return '<input type="checkbox" class="claim-pick" data-key="' + esc(k) + '"' + on
    + ' aria-label="选中这条：' + esc(r.name || '') + '">';
}

/** 行内勾选的事件 —— 每次重画重新绑（跟 `bindClaimActions` 一个套路）。 */
function bindClaimBatchRows(el) {
  el.querySelectorAll('input.claim-pick:not([disabled])').forEach((cb) => {
    cb.addEventListener('change', () => {
      const key = cb.getAttribute('data-key') || '';
      if (!key) return;
      if (cb.checked) _claimSelected.add(key);
      else _claimSelected.delete(key);
      syncClaimBatchBar();
    });
  });
}

/** 当前页里**能自动领**的 key —— 「选本页」只作用这些（用户拍板：只选当前页）。 */
function claimPageSelectableKeys() {
  const filtered = claimFilterRows(_claimPendingRows);
  const start = (_claimPage - 1) * CLAIM_PAGE_SIZE;
  return filtered.slice(start, start + CLAIM_PAGE_SIZE)
    .map((r) => {
      const k = String(r.status_key || '');
      return (k && claimRowUi(r).online) ? k : '';
    })
    .filter(Boolean);
}

/** 工具条状态：已选数 / 按钮可用 /「选本页」勾态。 */
function syncClaimBatchBar() {
  const n = _claimSelected.size;
  const info = $('#claim-batch-info');
  if (info && !_claimBatchRunning) info.textContent = n ? '已选 ' + n + ' 条' : '未选中';
  const btn = $('#claim-batch-claim');
  if (btn) btn.disabled = _claimBatchRunning || n === 0;
  const clr = $('#claim-batch-clear');
  if (clr) clr.disabled = _claimBatchRunning || n === 0;
  const all = $('#claim-select-page');
  if (all) {
    const page = claimPageSelectableKeys();
    const picked = page.filter((k) => _claimSelected.has(k)).length;
    all.checked = page.length > 0 && picked === page.length;
    all.disabled = _claimBatchRunning || page.length === 0;
    all.title = page.length
      ? '选中本页 ' + page.length + ' 条可自动领取的'
      : '本页没有可自动领取的行';
  }
}

/** 工具条三个按钮（静态元素，绑一次）。 */
function bindClaimBatchBar() {
  $('#claim-select-page')?.addEventListener('change', (e) => {
    if (_claimBatchRunning) return;
    const keys = claimPageSelectableKeys();
    if (e.currentTarget.checked) keys.forEach((k) => _claimSelected.add(k));
    else keys.forEach((k) => _claimSelected.delete(k));   // 只动本页，别页选的留着
    renderClaimPendingTable();
  });
  $('#claim-batch-clear')?.addEventListener('click', () => {
    if (_claimBatchRunning) return;
    _claimSelected.clear();
    renderClaimPendingTable();
  });
  $('#claim-batch-claim')?.addEventListener('click', runClaimBatch);
}

/** 批量结果面板 —— **失败要逐条看得见**（静默失败这个项目最怕）。 */
function renderClaimBatchResult(out, skipped, total) {
  const box = $('#claim-batch-result');
  if (!box) return;
  const lines = out.fails.slice(0, 8).map((f) =>
    '<br><span class="hint">· ' + esc(String(f.name || '').slice(0, 40)) + '：'
    + esc(f.why || '') + '</span>');
  box.hidden = false;
  box.innerHTML = '<div class="banner ' + (out.fail ? 'bad' : 'ok') + '">'
    + '<b>批量自动领取 ' + total + ' 条：成功 ' + out.ok
    + ' · 华为已领 ' + out.already + ' · 失败 ' + out.fail
    + (skipped ? ' · 跳过 ' + skipped + ' 条（不在当前筛选或已不可领）' : '') + '</b>'
    + lines.join('')
    + (out.fails.length > 8
      ? '<br><span class="hint">… 还有 ' + (out.fails.length - 8) + ' 条失败没列</span>' : '')
    + '<br><span class="hint">失败的还留在勾选里 —— 再点一次「批量自动领取」就能重试。</span>'
    + '</div>';
}

/** 把待办行分成 N 条车道 —— ⚠ **同一个 SN 的两行必须落在同一条车道**：
 *  同一台机器并发查询/提交会互撞（华为可能回 E05、或两次提交互相顶掉）。
 *  做法：按 SN 分组（组内保持勾选顺序）→ 组**轮流**分车道。
 */
function claimBatchLanes(rows, lanes) {
  const groups = [];
  const seen = new Map();
  (rows || []).forEach((r) => {
    const sn = String(claimRowUi(r).onlineSn || r.status_key || '');
    if (!seen.has(sn)) {
      seen.set(sn, groups.length);
      groups.push([]);
    }
    groups[seen.get(sn)].push(r);
  });
  const out = [];
  for (let i = 0; i < lanes; i++) out.push([]);
  groups.forEach((g, i) => {
    out[i % lanes].push(...g);      // 展平进车道
  });
  return out;
}

/** 批量自动领取：**一次确认 → 两条车道并行直提 → 汇总**（并发度 = `CLAIM_BATCH_LANES`）。
 *
 * ⚠ 走的是**单条** `POST /api/claim/submit` —— SN/status_key 范围校验、86 码拦截、
 *   无链接拦截、成功后自动标已领，全部复用那条路（坑 18：不为批量开第二条提交路径）。
 * ⚠ 一条失败**不中断**（网关超时 12s 也算失败，本车道继续）；成功的从选择里去掉、
 *   失败的**留着**，跑完可以直接再点一次重试。
 * ⚠ 并行写的是同一份 `out/claim-status.json`（整份读改写）—— 后端已加**进程内锁**，
 *   没那把锁上并行 = 后写的把先写的盖掉，表现为「领成功了却还在待领」。
 */
async function runClaimBatch() {
  if (_claimBatchRunning) return;
  const visible = claimFilterRows(_claimPendingRows);
  const byKey = new Map(visible.map((r) => [String(r.status_key || ''), r]));
  const picked = [..._claimSelected];
  const todo = picked.map((k) => byKey.get(k)).filter((r) => r && claimRowUi(r).online);
  const skipped = picked.length - todo.length;
  if (!todo.length) {
    toast('选中的这些不在当前筛选里、或已不能自动领取', 'bad');
    return;
  }
  const ok = confirm('对勾选的 ' + todo.length + ' 条逐条自动领取？\n\n'
    + '· 会真提交华为领取接口（不可撤回）\n'
    + '· 某条失败会继续下一条，跑完给汇总\n'
    + (skipped ? '· 有 ' + skipped + ' 条不在当前筛选或已不可领，会被跳过\n' : '')
    + '· 大约每条 1 秒上下，条数多时要多等一会儿');
  if (!ok) return;

  _claimBatchRunning = true;
  syncClaimBatchBar();
  const btn = $('#claim-batch-claim');
  const oldTxt = btn ? btn.textContent : '';
  const info = $('#claim-batch-info');
  const out = { ok: 0, already: 0, fail: 0, fails: [] };
  // ⚠ **两条并行**（2026-09-29 用户：「改成两条并行吧」）——
  //   `claimBatchLanes` 保证**同一个 SN 的两行在同一车道**（同机并发查/提会互撞，
  //   华为可能回 E05 或顶掉彼此）；进度用共享计数，失败按 `order` 排回去。
  const lanes = claimBatchLanes(todo, CLAIM_BATCH_LANES);
  const order = new Map(todo.map((r, i) => [r, i]));   // 并行回来是乱序的，留顺序号
  let done = 0;
  const tick = () => {
    done++;
    if (btn) btn.textContent = '领取中 ' + done + '/' + todo.length;
    if (info) {
      info.textContent = '领取中 ' + done + '/' + todo.length
        + ' · 成功 ' + out.ok + ' · 失败 ' + out.fail;
    }
  };
  /** 单条提交：成功 / 华为已领 → 移出勾选；失败 → 记原因**并留在勾选里**。 */
  const submit = async (r) => {
    const key = String(r.status_key || '');
    try {
      const res = await api('/api/claim/submit', {
        method: 'POST',
        body: {
          sn: claimRowUi(r).onlineSn,
          activity_id: r.activity_id || '',
          status_key: key,
        },
      });
      if (res && res.ok) {
        out.ok++;
        _claimSelected.delete(key);
      } else if (res && (res.already_claimed || res.local_marked
                         || res.popup_key === 'popupInfo3')) {
        // 华为说「已经领取过」= 对门店就是已领（后端多半已把本机状态标掉）
        out.already++;
        _claimSelected.delete(key);
      } else {
        out.fail++;
        out.fails.push({ order: order.get(r) || 0, name: r.name || r.sn,
                         why: (res && (res.why || res.popup)) || '失败' });
      }
    } catch (e) {
      out.fail++;
      out.fails.push({ order: order.get(r) || 0, name: r.name || r.sn, why: e.message });
    }
    tick();                       // 不管成败都算跑完一条（进度别卡住）
  };
  const runLane = async (rows) => {
    for (let i = 0; i < rows.length; i++) {
      await submit(rows[i]);
      if (i < rows.length - 1) {
        await new Promise((resolve) => setTimeout(resolve, CLAIM_BATCH_GAP_MS));
      }
    }
  };
  try {
    await Promise.all(lanes.map((rows) => runLane(rows)));
  } finally {
    _claimBatchRunning = false;
    if (btn) btn.textContent = oldTxt;
    syncClaimBatchBar();
  }
  // 两条车道回来是乱序的 → 失败清单按**勾选顺序**排回去，读起来才顺
  out.fails.sort((a, b) => (a.order || 0) - (b.order || 0));
  renderClaimBatchResult(out, skipped, todo.length);
  toast('批量领取完成：成功 ' + (out.ok + out.already)
    + '（华为已领 ' + out.already + '）· 失败 ' + out.fail
    + (skipped ? ' · 跳过 ' + skipped : ''), out.fail ? '' : 'ok');
  await loadClaimPending();      // 重画：成功的变已领，失败的按 key 恢复勾选
}

let _claimOnlineCtx = null;
let _claimAlreadyPending = null;

function openClaimOnlineModal(ctx) {
  _claimOnlineCtx = ctx;
  const mask = $('#claim-online-mask');
  if (!mask) return;
  $('#claim-online-name').textContent = ctx.name || '—';
  $('#claim-online-sn').textContent = ctx.sn || '—';
  $('#claim-online-act').textContent = ctx.title || ctx.aid || '—';
  $('#claim-online-store').textContent = ctx.store || '—';
  const box = $('#claim-online-result');
  if (box) box.innerHTML = '';
  const ok = $('#claim-online-ok');
  if (ok) {
    ok.disabled = false;
    ok.textContent = '确认领取';
    ok.hidden = false;
    delete ok.dataset.mode;
  }
  _claimAlreadyPending = null;
  const cancel = $('#claim-online-cancel');
  if (cancel) { cancel.textContent = '取消'; cancel.disabled = false; }
  const hint = $('#claim-online-hint');
  if (hint) hint.hidden = false;
  mask.hidden = false;
}

function closeClaimOnlineModal() {
  const mask = $('#claim-online-mask');
  if (mask) mask.hidden = true;
  _claimOnlineCtx = null;
  _claimAlreadyPending = null;
}

function bindClaimOnlineModal() {
  $('#claim-online-cancel')?.addEventListener('click', closeClaimOnlineModal);
  $('#claim-online-mask')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeClaimOnlineModal();
  });
  $('#claim-online-ok')?.addEventListener('click', async () => {
    const okBtn = $('#claim-online-ok');
    // 「已领取」态：点确认 → 标已领（若后端还没标）→ 关窗
    if (okBtn && okBtn.dataset.mode === 'already') {
      // ⚠ 防连点（2026-09-26 bug #3：「点确认太快会一直弹出」）——
      //   这个分支原来**既不 disabled 也没有 in-flight 守卫**，连点 N 下 =
      //   N 个并发 POST + N 次 toast + N 次 loadClaimPending 重画表格。
      //   正常提交分支进门就 disable，这里漏了；两头现在一致。
      if (okBtn.disabled) return;
      okBtn.disabled = true;
      const pend = _claimAlreadyPending;
      const ctx0 = _claimOnlineCtx;
      try {
        if (pend && pend.key) {
          const r = await api('/api/claim/status', {
            method: 'POST',
            body: { key: pend.key, status: 'claimed' },
          });
          if (r && !r.ok) {
            toast((r.why) || '标记失败', 'bad');
            okBtn.disabled = false;   // 失败要还回来，否则卡死
            return;
          }
        }
        toast(pend && pend.tip ? pend.tip : '已标为已领', 'ok');
        closeClaimOnlineModal();
        loadClaimPending();
        if (ctx0) { /* closed */ }
      } catch (e) {
        toast('标记失败：' + e.message, 'bad');
        okBtn.disabled = false;       // 异常同理
      }
      return;
    }
    const ctx = _claimOnlineCtx;
    if (!ctx) return;
    const box = $('#claim-online-result');
    const hint = $('#claim-online-hint');
    const ok = okBtn;
    if (ok) { ok.disabled = true; ok.textContent = '提交中…'; }
    if (hint) hint.hidden = true;
    try {
      const r = await api('/api/claim/submit', {
        method: 'POST',
        body: {
          sn: ctx.sn,
          activity_id: ctx.aid || '',
          status_key: ctx.key || '',
        },
      });
      if (r && r.ok) {
        const okTxt = r.popup || r.desc || '恭喜，您已成功领取权益';
        if (box) {
          box.innerHTML = '<div class="banner ok"><b>' + esc(okTxt) + '</b><br>'
            + 'SN：<code>' + esc(ctx.sn) + '</code>'
            + '<br>机型：' + esc(ctx.name || '')
            + '<br><span class="hint">本机待领已标为「已领」。</span></div>';
        }
        if (ok) { ok.hidden = true; delete ok.dataset.mode; }
        const cancel = $('#claim-online-cancel');
        if (cancel) cancel.textContent = '关闭';
        toast(okTxt, 'ok');
        loadClaimPending();
      } else if (r && (r.already_claimed || r.popup_key === 'popupInfo3'
                       || r.local_marked || /已经领取/.test(String(r.why || r.popup || '')))) {
        // 官网「已经领取过」→ 不要「重试」；给「确认」，点了标已领并关窗
        // （后端在 already_claimed 时已回 200，且多半已 local_marked）
        const tip = (r.popup || r.why || '您的设备已经领取过权益，无法再领取');
        if (box) {
          box.innerHTML = '<div class="banner ok"><b>' + esc(tip) + '</b>'
            + '<br><span class="hint">点「确认」把这条标为已领</span>'
            + '<br><span class="hint">SN：' + esc(ctx.sn) + '</span></div>';
        }
        _claimAlreadyPending = {
          key: ctx.key || '',
          tip: tip,
        };
        if (ok) {
          ok.hidden = false;
          ok.disabled = false;
          ok.textContent = '确认';
          ok.dataset.mode = 'already';
        }
        const cancel = $('#claim-online-cancel');
        if (cancel) cancel.textContent = '取消';
      } else {
        // ⭐ 优先官网同款 popupInfo 文案（与 consumer.huawei.com 一致）
        const badTxt = (r && (r.popup || r.why)) || '未知错误';
        if (box) {
          box.innerHTML = '<div class="banner bad">'
            + '<b>' + esc(badTxt) + '</b>'
            + (r && r.popup_key ? '<br><span class="hint">官网文案 ' + esc(r.popup_key) + '</span>' : '')
            + '<br><span class="hint">SN：' + esc(ctx.sn) + '</span></div>';
        }
        if (ok) { ok.disabled = false; ok.textContent = '重试'; }
        toast(badTxt, 'bad');
      }
    } catch (e) {
      if (box) {
        box.innerHTML = '<div class="banner bad"><b>领取失败</b><br>'
          + esc(e.message) + '</div>';
      }
      if (ok) { ok.disabled = false; ok.textContent = '重试'; }
    }
  });
}

function bindClaimActions(rootEl) {
  if (!rootEl) return;
  rootEl.querySelectorAll('button[data-claim-manual]').forEach(btn => {
    btn.addEventListener('click', () => {
      const url = btn.getAttribute('data-url') || '';
      if (!/^https?:\/\//i.test(url)) {
        toast('这个活动没有可用的领取链接', 'bad');
        return;
      }
      // 新开官方页 —— 门店帮客户在浏览器里手领
      window.open(url, '_blank', 'noopener,noreferrer');
    });
  });
  rootEl.querySelectorAll('button[data-claim-online]').forEach(btn => {
    btn.addEventListener('click', () => {
      openClaimOnlineModal({
        sn: btn.getAttribute('data-sn') || '',
        aid: btn.getAttribute('data-aid') || '',
        key: btn.getAttribute('data-key') || '',
        name: btn.getAttribute('data-name') || '',
        title: btn.getAttribute('data-title') || '',
        store: btn.getAttribute('data-store') || '',
      });
    });
  });
  rootEl.querySelectorAll('button[data-claim-key]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const key = btn.getAttribute('data-claim-key');
      const st = btn.getAttribute('data-claim-st');
      try {
        const r = await api('/api/claim/status', {
          method: 'POST',
          body: { key: key, status: st },
        });
        if (r && r.ok) {
          toast('已更新状态', 'ok');
          loadClaimPending();
        } else {
          toast((r && r.why) || '更新失败', 'bad');
        }
      } catch (e) {
        toast('更新失败：' + e.message, 'bad');
      }
    });
  });
}

let _claimEventsBound = false;

function bindClaimPendingEvents() {
  if (_claimEventsBound) return;
  _claimEventsBound = true;
  // 权益领取：活动一览 = 待领页内小按钮 → 悬浮窗；待领先抓云商再重算
  $('#btn-open-claim-acts')?.addEventListener('click', () => {
    const mask = $('#claim-acts-mask');
    if (mask) mask.hidden = false;
    loadClaimActivities();
  });
  $('#claim-act-show-expired')?.addEventListener('change', () => renderClaimActivities());
  $('#claim-acts-close')?.addEventListener('click', () => {
    const mask = $('#claim-acts-mask');
    if (mask) mask.hidden = true;
  });
  $('#claim-acts-mask')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) e.currentTarget.hidden = true;
  });
  $('#btn-refresh-claim-pending')?.addEventListener('click', (e) =>
    refreshWithFetch('claim-pending', e.currentTarget, () => loadClaimPending()));
  $$('#claim-status-seg .seg-btn').forEach((b) => {
    b.addEventListener('click', () => {
      _claimView = b.dataset.claimView === 'claimed' ? 'claimed' : 'pending';
      _claimPage = 1;
      renderClaimPendingTable('');
    });
  });
  $('#claim-page-prev')?.addEventListener('click', () => setClaimPage(_claimPage - 1));
  $('#claim-page-next')?.addEventListener('click', () => setClaimPage(_claimPage + 1));
  $('#claim-filter-cat')?.addEventListener('change', () => { _claimPage = 1; renderClaimPendingTable(''); });
  $('#claim-filter-store')?.addEventListener('change', () => { _claimPage = 1; renderClaimPendingTable(''); });
  $('#claim-filter-model')?.addEventListener('input', () => { _claimPage = 1; renderClaimPendingTable(''); });
  bindClaimOnlineModal();
  bindClaimBatchBar();      // 批量多选工具条（2.2.4）—— 静态元素，绑一次
}


function mountClaimPendingPage() {
  claimPageMounted = true;
  bindClaimPendingEvents();
}

function unmountClaimPendingPage() {
  claimPageMounted = false;
  claimPageEpoch += 1;
  claimReadControllers.forEach((controller) => controller.abort());
  claimReadControllers.clear();
}

registerPage('claim-pending', {
  mount: mountClaimPendingPage,
  load: loadClaimPending,
  unmount: unmountClaimPendingPage,
});
