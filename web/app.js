'use strict';

/* 云商 ↔ 华为报量对账 · 本地控制台
   无框架、无构建、无 CDN —— 门店电脑断网也能用。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const CONFIG_FIELDS = [
  ['store_code', 'cfg-store_code'],
  ['marker', 'cfg-marker'],
  ['erp_store_name', 'cfg-erp_store_name'],
  ['check.lookback_days', 'cfg-lookback_days'],
  ['check.report_lookahead_days', 'cfg-report_lookahead_days'],
  ['check.page_size', 'cfg-page_size'],
  ['check.pay_status', 'cfg-pay_status'],
  ['check.return_status', 'cfg-return_status'],
];

const state = { overview: null, report: null, sheet: null, since: 0, jobId: null,
                timer: null, autoTimer: null };

/* ───────────────────────────── 小工具 ───────────────────────────── */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* 可能是文件流 */ }
  if (!r.ok) {
    // 服务端可能给 error（参数问题）或 message（操作失败），两个都认
    const msg = (data && (data.error || data.message)) || `HTTP ${r.status}`;
    throw new Error(msg);
  }
  return data;
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, kind = '') {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, kind === 'bad' ? 6000 : 3000);
}

function fmtTs(sec) {
  if (!sec) return '—';
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 单元格默认转义；要放原生 HTML（比如按钮）就传 {html: '...'}
function cell(c, cls) {
  const raw = c && typeof c === 'object' && 'html' in c;
  return `<td class="${cls || ''}">${raw ? c.html : esc(c)}</td>`;
}

function table(header, rows, cls = []) {
  if (!rows || !rows.length) return '<div class="empty">没有记录</div>';
  const head = (header || []).map((h) => `<th>${esc(h)}</th>`).join('');
  const body = rows.map((r) => '<tr>' + (r || []).map((c, i) => cell(c, cls[i])).join('') + '</tr>').join('');
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/* ───────────────────────────── 页签 ───────────────────────────── */

$$('.tab').forEach((b) => b.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x === b));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + b.dataset.tab));
  if (b.dataset.tab === 'reports') loadOverview();
  if (b.dataset.tab === 'pos') loadPos();
  if (b.dataset.tab === 'session') { renderSession(); loadBrowserInfo(); loadHwLogin(); }
  if (b.dataset.tab === 'settings') loadConfig();
}));

/* ───────────────────────────── POS 合规 ───────────────────────────── */

// ⚠ 不按达标线染色 —— **达标线还没定**，染了就是编一个阈值出来。
//   等用户给了线，再在 pct() 里加 .ok/.warn/.bad。
const pct = (v) => (v == null ? '—' : v.toFixed(2) + '%');
const money = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function posKpi(title, cur, appeal, den, provisional) {
  return `<div class="kpi"><div class="k">${esc(title)}${provisional ? ' ⚠暂定' : ''}</div>`
    + `<div class="v">${pct(cur)}</div>`
    + `<div class="k" style="margin-top:4px">申诉后 ${pct(appeal)} · 分母 ${money(den)}</div></div>`;
}

async function loadPos() {
  let d;
  try {
    d = await api('/api/pos');
  } catch (e) {
    toast('读取 POS 数据失败：' + e.message, 'bad');
    return;
  }
  const meta = $('#pos-meta');
  if (!d.exists) {
    meta.textContent = '';
    $('#pos-cards').innerHTML = '';
    $('#pos-table').innerHTML = '<div class="empty">' + esc(d.hint || d.error || '还没有数据') + '</div>';
    return;
  }
  renderPos(d);
}

function renderPos(d) {
  const rows = d.rows || [];
  $('#pos-meta').textContent =
    `${d.year} 年 · ${d.orders} 单 / 退货 ${d.returns} 张 · 算于 ${d.generated_at}`;

  // 最近一个有分数的月（最老的月可能因为全是国补而分母为 0）
  const withRate = rows.filter((r) => r.label && r.label.rate != null);
  const last = withRate[withRate.length - 1] || rows[rows.length - 1] || null;
  $('#pos-cards').innerHTML = last
    ? posKpi(last.month + ' 按标签', last.label.rate, last.label.ap_rate, last.label.den, last.provisional)
      + posKpi(last.month + ' 按备注', last.remark.rate, last.remark.ap_rate, last.remark.den, last.provisional)
    : '';

  const head = ['月份', '按标签', '按标签·申诉后', '按备注', '按备注·申诉后', '分母(标签)', '进分母单数'];
  const body = rows.map((r) => [
    r.month + (r.provisional ? ' ⚠暂定' : ''),
    pct(r.label.rate), pct(r.label.ap_rate),
    pct(r.remark.rate), pct(r.remark.ap_rate),
    money(r.label.den), r.label.orders,
  ]);
  $('#pos-table').innerHTML = table(head, body, ['', 'num', 'num', 'num', 'num', 'num', 'num']);
}

$('#btn-refresh-pos') && $('#btn-refresh-pos').addEventListener('click', loadPos);

/* ───────────────────────────── 总览 ───────────────────────────── */

async function loadOverview() {
  try {
    state.overview = await api('/api/overview');
  } catch (e) {
    toast('读取总览失败：' + e.message, 'bad');
    return;
  }
  const o = state.overview;

  const v = o.config.values || {};
  $('#store-line').textContent =
    `${v.erp_store_name || '（未配置门店）'} · 华为 ${v.store_code || '会话默认'} · 标识 ${v.marker || '—'}`;

  const s = o.session || {};
  const sp = $('#pill-session');
  if (!s.exists) { sp.className = 'pill bad'; sp.textContent = '会话未导入'; }
  else { sp.className = 'pill warn'; sp.textContent = '会话已导入'; }

  renderUpdate(o.update || {});
  const bl = $('#build-line');
  if (bl && o.build) bl.textContent = `v${o.version || ''} · ${o.build}`;

  const sch = o.schedule || {};
  const scp = $('#pill-schedule');
  if (sch.installed) {
    const n = (sch.tasks || []).length;
    scp.className = 'pill ok';
    scp.textContent = sch.time ? `每天 ${sch.time}${n > 1 ? ` 等 ${n} 个` : ''}` : `定时 ${n} 个`;
  } else { scp.className = 'pill'; scp.textContent = '未设定时'; }

  renderReports(o.reports || []);
  renderSession();
  renderSchedule(sch);
}

function renderReports(list) {
  const latest = list[0];
  $('#report-cards').innerHTML = latest ? `
    <div class="kpi bad"><div class="k">最新报告 · 未报量</div><div class="v">${latest.missing ?? '—'}</div></div>
    <div class="kpi ok"><div class="k">已报量</div><div class="v">${latest.matched ?? '—'}</div></div>
    <div class="kpi ${latest.reverse ? 'warn' : ''}"><div class="k">反向差异</div><div class="v">${latest.reverse ?? '—'}</div></div>
    <div class="kpi ${latest.reverse_unshipped ? 'bad' : ''}">
      <div class="k">调拨货查无出库</div><div class="v">${latest.reverse_unshipped ?? '—'}</div></div>
    <div class="kpi"><div class="k">报告日期</div><div class="v" style="font-size:17px">${esc(latest.date || latest.name.replace(/\D+/g, '').slice(0, 8) || '—')}</div></div>
  ` : '<div class="kpi"><div class="k">还没有报告</div><div class="v" style="font-size:15px">去「运行」页跑一次</div></div>';

  $('#report-count').textContent = list.length ? `共 ${list.length} 份（每次跑都存一份，不覆盖）` : '';
  if (!list.length) {
    $('#report-list').innerHTML = '<div class="empty">out/ 目录下还没有差异报告</div>';
    return;
  }
  const rows = list.map((r) => [
    r.date || r.name,
    r.missing, r.matched, r.reverse, r.reverse_transfer, r.reverse_unshipped,
    fmtTs(r.mtime),
    { html: `<button class="btn small" data-open="${esc(r.name)}">查看</button>
             <button class="btn small ghost" data-del="${esc(r.name)}">删除</button>` },
  ]);
  $('#report-list').innerHTML = table(
    ['目标日', '未报量', '已报量', '反向差异', '↳调拨货', '↳查无出库', '生成时间', ''],
    rows, ['mono', 'num', 'num', 'num', 'num', 'num', 'mono', '']);
  $$('#report-list [data-open]').forEach((b) =>
    b.addEventListener('click', () => openReport(b.dataset.open)));
  $$('#report-list [data-del]').forEach((b) =>
    b.addEventListener('click', () => removeReport(b.dataset.del)));
}

async function removeReport(name) {
  if (!confirm(`删除这份报告？\n\n${name}\n\n删了就找不回来了（xlsx 和摘要一起删）。`)) return;
  try {
    const r = await api('/api/report?name=' + encodeURIComponent(name), { method: 'DELETE' });
    toast(r.message || '已删除', 'ok');
    if (state.report && state.report.name === name) $('#report-detail-card').hidden = true;
    loadOverview();
  } catch (e) {
    toast('删除失败：' + e.message, 'bad');
  }
}

async function openReport(name) {
  try {
    state.report = await api('/api/report?name=' + encodeURIComponent(name));
  } catch (e) { return toast('读报告失败：' + e.message, 'bad'); }
  const names = Object.keys(state.report.sheets || {});
  state.sheet = names[0];
  $('#report-detail-card').hidden = false;
  $('#report-detail-title').textContent = name;
  $('#btn-download').href = '/api/report/download?name=' + encodeURIComponent(name);
  renderSheetTabs(names);
  renderSheet();
  $('#report-detail-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSheetTabs(names) {
  $('#sheet-tabs').innerHTML = names.map((n) => {
    const rows = (state.report.sheets[n] || []).length - 1;
    return `<button class="subtab ${n === state.sheet ? 'active' : ''}" data-sheet="${esc(n)}">
      ${esc(n)} <span class="hint">(${rows})</span></button>`;
  }).join('');
  $$('#sheet-tabs [data-sheet]').forEach((b) => b.addEventListener('click', () => {
    state.sheet = b.dataset.sheet;
    renderSheetTabs(names);
    renderSheet();
  }));
}

function renderSheet() {
  const rows = state.report.sheets[state.sheet] || [];
  $('#sheet-body').innerHTML = table(rows[0] || [], rows.slice(1));
}

$('#btn-close-detail').addEventListener('click', () => { $('#report-detail-card').hidden = true; });
$('#btn-refresh-reports').addEventListener('click', loadOverview);

/* ───────────────────────────── 运行 ───────────────────────────── */

$('#run-mode').addEventListener('change', (e) => {
  $('#run-date').hidden = e.target.value !== 'date';
});
$('#btn-quick-run').addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === 'run'));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-run'));
  startRun();
});

async function startRun() {
  const mode = $('#run-mode').value;
  const body = { mode };
  if (mode === 'date') {
    if (!$('#run-date').value) return toast('先选个日期', 'bad');
    body.date = $('#run-date').value;
  }
  const lb = $('#run-lookback').value, la = $('#run-lookahead').value;
  if (lb !== '') body.lookback = Number(lb);
  if (la !== '') body.lookahead = Number(la);

  $('#run-log').textContent = '';
  state.since = 0; state.jobId = null;
  try {
    const job = await api('/api/run', { method: 'POST', body });
    state.jobId = job.id;
    appendLog(job);
    $('#btn-run').disabled = true; $('#btn-stop').disabled = false;
    $('#run-status').textContent = '运行中…';
    pollRun();
  } catch (e) {
    toast('启动失败：' + e.message, 'bad');
  }
}

function appendLog(job) {
  state.since = job.line_count;
  const el = $('#run-log');
  if (!job.lines || !job.lines.length) return;
  if (el.textContent === '（还没有运行记录）') el.textContent = '';
  el.textContent += job.lines.join('\n') + '\n';
  el.scrollTop = el.scrollHeight;
}

function pollRun() {
  clearTimeout(state.timer);
  state.timer = setTimeout(async () => {
    let job;
    try {
      job = await api(`/api/run?id=${state.jobId || ''}&since=${state.since}`);
    } catch (e) { return; }
    appendLog(job);
    if (job.running) return pollRun();

    $('#btn-run').disabled = false; $('#btn-stop').disabled = true;
    const code = job.exit_code;
    const label = { 0: '完成，无差异 ✅', 1: '会话已过期 ❌', 2: '取数失败 ❌',
                    3: '完成，有差异 ⚠️', 9: '程序出错（不是会话问题）❌' }[code] || `退出码 ${code}`;
    $('#run-status').textContent = `${label}（${job.elapsed}s）`;
    toast('对账结束：' + label, code === 0 ? 'ok' : (code === 3 ? '' : 'bad'));
    loadOverview();
  }, 900);
}

$('#btn-run').addEventListener('click', startRun);
$('#btn-stop').addEventListener('click', async () => {
  await api('/api/run/stop', { method: 'POST' });
  toast('已请求停止');
});

/* ───────────────── 华为账号（自动登录用） ───────────────── */

async function loadHwLogin() {
  let d;
  try { d = await api('/api/hwlogin'); } catch (e) { return; }
  $('#hw-username').value = d.username || '';
  $('#hw-password').value = '';                        // 密码绝不回显
  $('#hw-password').placeholder = d.has_password ? '已设置（留空＝不修改）' : '华为账号密码';
  $('#hw-status').innerHTML = d.ready
    ? '<span style="color:var(--ok)">已配置 · 可自动登录</span>'
    : (d.username ? '<span style="color:var(--warn)">缺密码</span>' : '');
  $('#hw-warn').innerHTML = d.ready ? ''
    : '<span class="hint">没配账号密码也能用 —— 抓取时会打开窗口等你手动登录。</span>';
}

$('#btn-hw-save').addEventListener('click', async () => {
  const body = { username: $('#hw-username').value.trim() };
  const pw = $('#hw-password').value;
  if (pw) body.password = pw;
  try {
    await api('/api/hwlogin', { method: 'PUT', body });
    $('#hw-password').value = '';
    toast('华为账号已保存，下次抓取会自动登录', 'ok');
    loadHwLogin();
  } catch (e) { toast('保存失败：' + e.message, 'bad'); }
});

/* ───────────────────── 自动抓 cookie（不用手抄） ───────────────────── */

async function loadBrowserInfo() {
  try {
    const b = await api('/api/session/auto/browser');
    $('#browser-line').innerHTML = b.found
      ? `检测到浏览器：<b>${esc(b.name)}</b> <span class="mono">${esc(b.path)}</span>`
      : '⚠️ 没找到 Edge / Chrome —— 自动抓取用不了，请走下面的「手抄 curl」';
    return b.found;
  } catch (e) { return false; }
}

async function startAuto(mode) {
  const label = mode === 'refresh' ? '静默续期' : '打开浏览器登录';
  try {
    await api('/api/session/auto', { method: 'POST', body: { mode } });
    toast(label + '：已开始');
    $('#auto-log').hidden = false;
    $('#auto-log').textContent = '';
    $('#auto-result').innerHTML = '';
    $('#btn-auto-login').disabled = true;
    $('#btn-auto-refresh').disabled = true;
    pollAuto();
  } catch (e) {
    toast(label + '失败：' + e.message, 'bad');
  }
}

function pollAuto() {
  clearTimeout(state.autoTimer);
  state.autoTimer = setTimeout(async () => {
    let job;
    try { job = await api('/api/session/auto'); } catch (e) { return; }
    const el = $('#auto-log');
    el.hidden = false;
    el.textContent = (job.steps || []).join('\n') + (job.running ? '\n…' : '');
    el.scrollTop = el.scrollHeight;
    // ⚠ 运行期间就要提示，别等结束了才说 —— 用户正在等的时候最需要知道
    //   "现在轮到你操作了"。只写进上面那个滚动日志是不够的（用户不会去翻）。
    if (job.running && job.need === 'captcha') {
      $('#auto-result').innerHTML =
        `<div class="banner warn">⚠️ 页面要<b>图形验证码</b> ——
           请到浏览器窗口里输一下，输完程序会自己继续。</div>`;
    }
    if (job.running) return pollAuto();

    $('#btn-auto-login').disabled = false;
    $('#btn-auto-refresh').disabled = false;
    // ⚠ 漏一个状态的话 cls/title 都取到 undefined，横幅渲染成空的 ——
    //   用户看到的还是"什么都没说"，正是这次要修的毛病。
    const cls = { ok: 'ok', saved: 'warn', error: 'bad',
                  need_captcha: 'warn' }[job.state] || '';
    const title = { ok: '✅ 抓到了，会话可用', saved: '⚠️ 抓到了，但自检没过',
                    error: '❌ 抓取失败',
                    need_captcha: '⚠️ 需要验证码 —— 请手动登录一次' }[job.state]
                  || job.state;
    $('#auto-result').innerHTML =
      `<div class="banner ${cls}">${title}${job.message ? '：' + esc(job.message) : ''}</div>`;
    if (job.state === 'ok') {
      toast('会话已自动更新', 'ok');
      $('#pill-session').className = 'pill ok';
      $('#pill-session').textContent = '会话有效';
    }
    loadOverview();
  }, 1200);
}

$('#btn-auto-login').addEventListener('click', () => startAuto('login'));
$('#btn-auto-refresh').addEventListener('click', () => startAuto('refresh'));

/* ───────────────────────────── 会话 ───────────────────────────── */

function renderSession() {
  const s = (state.overview && state.overview.session) || {};
  if (!s.exists) {
    $('#session-box').innerHTML =
      '<div class="banner bad">还没有会话。照下面的步骤抓一份 curl 导入。</div>';
    return;
  }
  // 三种状态，**别混成一个**：
  //   check_ok === true  → 自检过、通过
  //   check_ok === false → 自检过、没过
  //   没这个字段       → 从没自检过
  // （老版本这里是一句写死的"没验证过"，点完自检也不变 —— 用户报过。）
  let banner;
  if (s.error) {
    banner = `<div class="banner bad">${esc(s.error)}</div>`;
  } else if (s.check_ok === true) {
    banner = `<div class="banner ok">✅ 上次自检<b>通过</b>
      <span class="hint">· ${esc(fmtTs(s.checked_at))}</span>
      ${s.check_message ? `<br><span class="hint">${esc(s.check_message)}</span>` : ''}</div>`;
  } else if (s.check_ok === false) {
    banner = `<div class="banner bad">❌ 上次自检<b>没过</b>
      <span class="hint">· ${esc(fmtTs(s.checked_at))}</span>
      <br><span class="hint">${esc(s.check_message || '')}</span>
      <br><span class="hint">多半是会话过期了 —— 到上面点「刷新会话」重新抓一份。</span></div>`;
  } else {
    banner = '<div class="banner warn">会话文件在，但<b>没验证过是否还有效</b>'
             + ' —— 点右上「会话自检」</div>';
  }

  $('#session-box').innerHTML = banner + `
    <table><tbody>
      <tr><th>文件</th><td class="mono">${esc(s.path)}</td></tr>
      <tr><th>保存时间</th><td class="mono">${fmtTs(s.saved_at)}</td></tr>
      <tr><th>上次自检</th><td class="mono">${
        s.checked_at
          ? `${fmtTs(s.checked_at)}　${s.check_ok ? '✅ 通过' : '❌ 没过'}`
          : '— 还没自检过'}</td></tr>
      <tr><th>csrf</th><td class="mono">${esc(s.csrf || '—')}</td></tr>
      <tr><th>cookies</th><td class="mono">${esc((s.cookie_names || []).join(', ') || '—')}</td></tr>
    </tbody></table>`
    // 换过会话的话后端会作废旧记录（按指纹判断）—— 说明一句，免得用户以为界面在抽风
    + (s.check_stale
        ? '<p class="hint">（这份会话是重新抓的，<b>上一次的自检记录已经作废</b>，要重新自检一次。）</p>'
        : '');
}

$('#btn-ping').addEventListener('click', async () => {
  const btn = $('#btn-ping');
  btn.disabled = true; btn.textContent = '自检中…';
  try {
    const r = await api('/api/session/ping', { method: 'POST' });
    const p = r.ping || {};
    toast((p.ok ? '✅ ' : '❌ ') + p.message, p.ok ? 'ok' : 'bad');
    const pill = $('#pill-session');
    pill.className = 'pill ' + (p.ok ? 'ok' : 'bad');
    pill.textContent = p.ok ? '会话有效' : '会话失效';
    // ⚠ 必须重新拉一次 —— 自检结果（和时间）是后端记下来的，
    //   只改 toast 的话那个"没验证过"的横幅会一直挂着（用户报过这个）。
    await loadOverview();
  } catch (e) { toast('自检失败：' + e.message, 'bad'); }
  finally { btn.disabled = false; btn.textContent = '自检'; }
});

$('#btn-import').addEventListener('click', async () => {
  const curl = $('#curl-input').value.trim();
  if (!curl) return toast('先把 curl 粘进来', 'bad');
  const btn = $('#btn-import');
  btn.disabled = true; btn.textContent = '导入中…';
  try {
    const r = await api('/api/session', { method: 'POST', body: { curl } });
    const p = r.ping || {};
    $('#import-result').innerHTML = p.ok
      ? `<div class="banner ok">✅ 导入成功，会话有效：${esc(p.message)}<br>
           <span class="mono">${esc(r.cookies || '')}</span></div>`
      : `<div class="banner bad">⚠️ 已保存，但自检没过：${esc(p.message || '')}</div>`;
    toast(p.ok ? '会话导入成功' : '已保存，但自检没过', p.ok ? 'ok' : 'bad');
    $('#pill-session').className = 'pill ' + (p.ok ? 'ok' : 'bad');
    $('#pill-session').textContent = p.ok ? '会话有效' : '会话失效';
    loadOverview();
  } catch (e) {
    $('#import-result').innerHTML = `<div class="banner bad">解析失败：${esc(e.message)}</div>`;
    toast('导入失败', 'bad');
  } finally { btn.disabled = false; btn.textContent = '导入并自检'; }
});

$('#btn-clear-curl').addEventListener('click', () => {
  $('#curl-input').value = ''; $('#import-result').innerHTML = '';
});

/* ───────────────────────── 云商账号 ───────────────────────── */

async function loadErp() {
  let d;
  try { d = await api('/api/erp'); } catch (e) { return; }
  $('#erp-username').value = d.username || '';
  $('#erp-company').value = d.company || '';
  $('#erp-password').value = '';                       // 密码绝不回显
  $('#erp-password').placeholder = d.has_password ? '已设置（留空＝不修改）' : '云商登录密码';
  $('#erp-token').value = '';
  $('#erp-token').placeholder = d.has_token
    ? `已有 token ${d.token}（要换再填）`
    : '浏览器 F12 → 任意请求 → Authorization: Bearer 后面那串';
  $('#erp-status').innerHTML = !d.exists
    ? '<span style="color:var(--bad)">还没配</span>'
    : (d.has_password
        ? `<span style="color:var(--ok)">已配置</span>${d.has_token ? ' · token 已缓存' : ' · 还没换过 token'}`
        : '<span style="color:var(--warn)">缺密码</span>');
  // 实际生效的凭据来自别的文件时必须说清楚，否则人会以为改的是这个文件
  $('#erp-warn').innerHTML = (d.used_from && !d.has_password)
    ? `<span style="color:var(--warn)">⚠️ 这个文件里没有密码 —— 实际生效的凭据来自
       <span class="mono">${esc(d.used_from)}</span>。要在这台电脑上用，请把账号密码填在上面并保存。</span>`
    : '';
}

/** 把表单存下去。返回 true 表示成功。 */
async function saveErp(extra = {}) {
  const body = {
    username: $('#erp-username').value.trim(),
    company: $('#erp-company').value.trim(),
    ...extra,
  };
  const pw = $('#erp-password').value;
  if (pw) body.password = pw;                          // 空 = 不改
  try {
    await api('/api/erp', { method: 'PUT', body });
    $('#erp-password').value = '';
    return true;
  } catch (e) {
    $('#erp-msg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
    return false;
  }
}

$('#btn-erp-save').addEventListener('click', async () => {
  if (!(await saveErp())) return;
  $('#erp-msg').textContent = '已保存 ' + new Date().toLocaleTimeString();
  toast('云商账号已保存', 'ok');
  loadErp();
});

/* ---- 云商图形验证码 ---- */

function showCaptcha(image, message) {
  if (image) $('#erp-captcha-img').src = image;
  $('#erp-captcha').hidden = false;
  $('#erp-captcha-code').value = '';
  $('#erp-captcha-code').focus();
  $('#erp-captcha-msg').innerHTML = message
    ? `<span style="color:var(--warn)">${esc(message)}</span>` : '';
}

function hideCaptcha() {
  $('#erp-captcha').hidden = true;
  $('#erp-captcha-code').value = '';
  $('#erp-captcha-msg').textContent = '';
}

$('#btn-erp-captcha').addEventListener('click', async () => {
  const code = $('#erp-captcha-code').value.trim();
  if (!code) return toast('先把验证码填上', 'bad');
  const btn = $('#btn-erp-captcha');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    const r = await api('/api/erp/login/captcha', { method: 'POST', body: { code } });
    if (r.ok) {
      hideCaptcha();
      $('#erp-msg').innerHTML = `<span style="color:var(--ok)">✅ 登录成功`
        + `${r.who ? '：' + esc(r.who) : ''} · token ${esc(r.token || '')}（已保存）</span>`;
      toast('云商登录成功', 'ok');
      loadErp();
    } else if (r.need_captcha) {
      showCaptcha(r.image, r.message || '验证码不对，再试一次');
      toast('验证码不对，换一张再试', 'bad');
    } else {
      hideCaptcha();
      $('#erp-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(r.message || '失败')}</span>`;
      toast('登录失败', 'bad');
    }
  } catch (e) {
    $('#erp-captcha-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '提交验证码';
  }
});

$('#erp-captcha-code').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('#btn-erp-captcha').click();
});

$('#btn-erp-token-save').addEventListener('click', async () => {
  const token = $('#erp-token').value.trim();
  if (!token) return toast('先把 token 粘进来', 'bad');
  try {
    await api('/api/erp', { method: 'PUT', body: { token } });
    $('#erp-token').value = '';
    toast('token 已保存', 'ok');
    loadErp();
  } catch (e) { toast('保存失败：' + e.message, 'bad'); }
});

$('#btn-erp-login').addEventListener('click', async () => {
  const btn = $('#btn-erp-login');
  btn.disabled = true; btn.textContent = '登录中…';
  $('#erp-msg').textContent = '';
  hideCaptcha();
  try {
    // 用**输入框里的值**测，不先保存 —— 打错的密码不该被存下来
    const r = await api('/api/erp/login', {
      method: 'POST',
      body: {
        username: $('#erp-username').value.trim(),
        company: $('#erp-company').value.trim(),
        password: $('#erp-password').value,
      },
    });
    if (r.ok) {
      $('#erp-msg').innerHTML = `<span style="color:var(--ok)">✅ 登录成功`
        + `${r.who ? '：' + esc(r.who) : ''} · token ${esc(r.token || '')}（已保存）</span>`;
      $('#erp-password').value = '';
      hideCaptcha();
      toast('云商登录成功', 'ok');
    } else if (r.need_captcha) {
      showCaptcha(r.image, r.message);
      $('#erp-msg').innerHTML = '';
    } else {
      $('#erp-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(r.message || '登录失败')}`
        + `<br><span class="hint">没有保存任何改动 —— 请核对账号密码后重试</span></span>`;
      toast('云商登录失败', 'bad');
    }
    loadErp();
  } catch (e) {
    $('#erp-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '测试登录';
  }
});

/* ───────────────────────── 邮件推送 ───────────────────────── */

const MAIL_FIELDS = [
  ['host', 'mail-host'], ['port', 'mail-port'], ['security', 'mail-security'],
  ['sender', 'mail-sender'], ['recipients', 'mail-recipients'],
  ['subject_prefix', 'mail-subject_prefix'], ['when', 'mail-when'],
];

function mailForm() {
  const body = { enabled: $('#mail-enabled').checked };
  MAIL_FIELDS.forEach(([k, id]) => { body[k] = $('#' + id).value.trim(); });
  const pw = $('#mail-password').value;
  if (pw) body.password = pw;
  body.username = $('#mail-username').value.trim();
  return body;
}

async function loadMail() {
  let d;
  try { d = await api('/api/mail'); } catch (e) { return; }
  $('#mail-enabled').checked = !!d.enabled;
  MAIL_FIELDS.forEach(([k, id]) => { $('#' + id).value = d[k] == null ? '' : d[k]; });
  $('#mail-username').value = d.username || '';
  $('#mail-password').value = '';
  $('#mail-password').placeholder = d.has_password ? '已设置（留空＝不修改）' : '多数邮箱要填「授权码」';

  const sel = $('#mail-preset');
  if (sel.options.length <= 1 && d.presets) {
    d.presets.forEach((p, i) => sel.add(new Option(p.name, String(i))));
    sel.addEventListener('change', () => {
      const p = d.presets[Number(sel.value)];
      if (!p) return;
      $('#mail-host').value = p.host;
      $('#mail-port').value = p.port;
      $('#mail-security').value = p.security;
      $('#mail-hint').textContent = p.hint || '';
    });
  }
  $('#mail-status').innerHTML = !d.enabled
    ? '<span class="hint">未启用</span>'
    : (d.ready
        ? '<span style="color:var(--ok)">已启用</span>'
        : `<span style="color:var(--warn)">缺 ${esc((d.problems || []).join('、'))}</span>`);
  $('#mail-warn').innerHTML = (d.enabled && !d.ready)
    ? `<span style="color:var(--warn)">⚠️ 配置不全（${esc((d.problems || []).join('、'))}），
       跑完不会发邮件</span>`
    : '';
}

$('#btn-mail-save').addEventListener('click', async () => {
  try {
    await api('/api/mail', { method: 'PUT', body: mailForm() });
    $('#mail-password').value = '';
    $('#mail-msg').textContent = '已保存 ' + new Date().toLocaleTimeString();
    toast('邮件配置已保存', 'ok');
    loadMail();
  } catch (e) {
    $('#mail-msg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
  }
});

$('#btn-mail-test').addEventListener('click', async () => {
  const btn = $('#btn-mail-test');
  btn.disabled = true; btn.textContent = '发送中…';
  $('#mail-msg').textContent = '';
  try {
    // 用输入框里的值测，**不先保存** —— 填错不该把好配置覆盖掉
    const r = await api('/api/mail/test', { method: 'POST', body: mailForm() });
    $('#mail-msg').innerHTML = r.ok
      ? `<span style="color:var(--ok)">✅ ${esc(r.message)}（配置没保存，记得点「保存」）</span>`
      : `<span style="color:var(--bad)">❌ ${esc(r.message || '发送失败')}</span>`;
    toast(r.ok ? '测试邮件已发出' : '测试邮件发送失败', r.ok ? 'ok' : 'bad');
  } catch (e) {
    $('#mail-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '发送测试邮件';
  }
});

/* ───────────────────────── 企微群推送 ───────────────────────── */

function wecomForm() {
  const body = {
    enabled: $('#wecom-enabled').checked,
    mention_all: $('#wecom-mention_all').checked,
    send_file: $('#wecom-send_file').checked,
    when: $('#wecom-when').value,
  };
  const hook = $('#wecom-webhook').value.trim();
  if (hook) body.webhook = hook;          // 空 = 不改
  return body;
}

async function loadWecom() {
  let d;
  try { d = await api('/api/wecom'); } catch (e) { return; }
  $('#wecom-enabled').checked = !!d.enabled;
  $('#wecom-mention_all').checked = !!d.mention_all;
  $('#wecom-send_file').checked = !!d.send_file;
  $('#wecom-when').value = d.when || 'always';
  $('#wecom-webhook').value = '';                       // 凭据绝不回显
  $('#wecom-webhook').placeholder = d.has_webhook
    ? `已配置（${d.webhook_key}）—— 要换再粘整条地址`
    : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…';
  $('#wecom-status').innerHTML = !d.enabled
    ? '<span class="hint">未启用</span>'
    : (d.ready
        ? '<span style="color:var(--ok)">已启用</span>'
        : `<span style="color:var(--warn)">缺 ${esc((d.problems || []).join('、'))}</span>`);
  $('#wecom-warn').innerHTML = (d.enabled && !d.ready)
    ? `<span style="color:var(--warn)">⚠️ 配置不全（${esc((d.problems || []).join('、'))}），
       跑完不会推送</span>`
    : '';
}

$('#btn-wecom-save').addEventListener('click', async () => {
  try {
    await api('/api/wecom', { method: 'PUT', body: wecomForm() });
    $('#wecom-webhook').value = '';
    $('#wecom-msg').textContent = '已保存 ' + new Date().toLocaleTimeString();
    toast('企微配置已保存', 'ok');
    loadWecom();
  } catch (e) {
    $('#wecom-msg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
  }
});

$('#btn-wecom-test').addEventListener('click', async () => {
  const btn = $('#btn-wecom-test');
  btn.disabled = true; btn.textContent = '推送中…';
  $('#wecom-msg').textContent = '';
  try {
    // 用输入框里的值测，**不先保存**
    const r = await api('/api/wecom/test', { method: 'POST', body: wecomForm() });
    $('#wecom-msg').innerHTML = r.ok
      ? `<span style="color:var(--ok)">✅ ${esc(r.message)}（配置没保存，记得点「保存」）</span>`
      : `<span style="color:var(--bad)">❌ ${esc(r.message || '推送失败')}</span>`;
    toast(r.ok ? '测试消息已推送，去群里看看' : '推送失败', r.ok ? 'ok' : 'bad');
  } catch (e) {
    $('#wecom-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '推送测试消息';
  }
});

/* ───────────────────────── 后台服务 / 开机自启 ───────────────────────── */

async function loadService() {
  let d;
  try { d = await api('/api/service'); } catch (e) { return; }
  $('#svc-status').innerHTML = d.running
    ? `<span style="color:var(--ok)">后台运行中</span> · 端口 ${d.port}`
    : '<span style="color:var(--bad)">未运行</span>';
  const a = d.autostart || {};
  $('#svc-autostart').checked = !!a.installed;
  // 注册方式分两种。**普通权限是正常状态**，管理员是要劝退的 ——
  // 管理员身份会让「自动抓会话」失败（浏览器拒绝以管理员运行），所以那边标黄。
  const how = a.mode === 'runkey'
    ? '<span style="color:var(--ok)">✅ 注册表启动项 · 普通权限 · 不需要管理员</span>'
    : (a.mode === 'task'
        ? '<span style="color:var(--warn)">⚠️ 计划任务 · <b>以管理员身份运行</b>（会让「自动抓会话」失败）</span>'
        : '');
  $('#svc-detail').innerHTML =
    (a.installed ? `注册方式：${how}<br>开机命令：<span class="mono">${esc(a.registered || a.command || '')}</span>` : '') +
    // ⚠ 这里以前写的是"改成管理员身份：右键 install.bat 以管理员身份运行" ——
    //   **方向反了**。管理员身份不是升级，是会把抓会话弄坏的降级。
    (a.installed && a.mode === 'task'
      ? '<br><span class="hint">建议改回<b>普通权限</b>：把上面那个「启动方式」选成「普通权限」，'
        + '点「保存」，然后停掉服务、用普通权限双击 <code>start.bat</code>。'
        + '<br>（对账本身不受影响；普通权限完全够用）'
        + '<br>⚠️ 如果「保存」之后提示"旧的提权任务没删掉"，'
        + '就右键 <code>install.bat</code> →「以管理员身份运行」再操作一次 —— '
        + '删那条任务需要管理员权限。</span>'
      : '') +
    (a.stale ? '<br><span style="color:var(--warn)">⚠️ 注册的还是老路径（项目挪过位置？）—— 重新保存一次</span>' : '') +
    (a.boot_script_exists ? '' : '<br><span style="color:var(--bad)">⚠️ 缺少 boot.py，没法注册开机自启</span>') +
    // ⚠ 别再说"自动抓会话不受影响" —— 那是错的。服务以管理员跑时，
    //   Edge / Chrome 拒绝以管理员运行：进程把命令行交棒出去就自己退 0，
    //   我们给的 --user-data-dir / --remote-debugging-port 落不到活着的实例上，
    //   调试端口永远没人监听。实测报错就是「Edge 启动后立刻退出（退出码 0）」，
    //   而链接跑到了用户原来那个浏览器里。这是**必坏**，不是"可能不稳定"。
    (a.self_elevated
      ? '<br><span style="color:var(--bad)">⚠️ 当前服务是<b>管理员身份</b>在跑 —— '
        + '<b>「自动抓会话」会失败</b>（Edge / Chrome 拒绝以管理员运行）。'
        + '对账本身不受影响。'
        // ⚠ 如果这台电脑**根本没法不管理员**（内置 Administrator 账户 / UAC 关着），
        //   就别说"改成普通权限"了 —— 改了也没用，只会让人白试一轮。
        //   后端查得到就直接把原因和解法摆出来。
        + (a.always_admin_reason
            // ⚠ 后端给的是**纯文本**（带 \n），要 `pre-line` 才保留换行；
            //   别在这儿套 Markdown —— esc() 只转义 HTML，`**` 会原样显示出来。
            ? '<br><span style="color:var(--bad);white-space:pre-line;display:block">'
              + esc(a.always_admin_reason) + '</span>'
            : '按上面那条改回普通权限即可。')
        + '</span>'
      : '');
  // 「以管理员身份修复」只在**确实需要管理员**时露出来：
  // 注册方式还是计划任务（旧版本留下的提权任务没删掉）——
  // 删它必须有管理员权限，而这是**一次性**的，所以只弹这一次 UAC。
  // ⚠ 平时藏起来：这个项目绝大多数操作都不该提权，摆一个常驻按钮会误导人。
  // ⚠ 服务**本身就是管理员**时要把这个按钮藏起来 —— 那个按钮的全部意义
  //   是"弹一次 UAC 去删掉旧的提权任务"，而服务已经是管理员说明
  //   它**本来就有权限**（普通「保存」就能删），而且这台机器上 UAC 可能
  //   根本弹不出来（内置 Administrator 账户）。摆着它只会让人反复点、反复没反应
  //   —— 实测就是这么被卡住的。
  $('#svc-repair-row').hidden = !(a.installed && a.mode === 'task' && !a.self_elevated);
}

$('#btn-svc-repair').addEventListener('click', async () => {
  const btn = $('#btn-svc-repair');
  btn.disabled = true;
  $('#svc-msg').textContent = '已弹出 UAC 窗口 —— 请点「是」，跑完那个黑窗口会停住等你按回车…';
  try {
    const r = await api('/api/elevate', { method: 'POST', body: { what: 'autostart' } });
    $('#svc-msg').textContent = (r.ok ? '✅ ' : '❌ ') + (r.message || '');
    toast(r.ok ? '已修复' : (r.message || '没成'), r.ok ? 'ok' : 'bad');
    loadService();
    loadOverview();
  } catch (e) {
    $('#svc-msg').textContent = '❌ ' + e.message;
    toast('修复失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
});

$('#btn-svc-save').addEventListener('click', async () => {
  const enabled = $('#svc-autostart').checked;
  // ⚠ **永远不带 `elevated`** —— 这个项目现在全程普通权限。
  //   以前这里读 `#svc-mode` 下拉，而那个选项是死的：选"以管理员身份"只是让
  //   当前这个普通权限的进程去执行 `schtasks /create /rl HIGHEST`，必然失败，
  //   **而且永远不会弹 UAC**（那条路上根本没有提权代码）。
  //   真需要管理员的地方走"按需提权"，只弹一次，见 src/elevate.py。
  try {
    const r = await api('/api/autostart', { method: 'POST', body: { enabled } });
    $('#svc-msg').textContent = (r.ok ? '✅ ' : '❌ ') + (r.message || '');
    toast(r.ok ? (enabled ? '已注册开机自启' : '已取消开机自启') : (r.message || '失败'),
          r.ok ? 'ok' : 'bad');
    loadService();
  } catch (e) {
    $('#svc-msg').textContent = '❌ ' + e.message;
    toast('保存失败：' + e.message, 'bad');
  }
});

/* ───────────────────────────── 检查更新 ───────────────────────────── */

// 缓存里的时间戳 → "3 天前"。给人看的，不求精确。
function formatAgo(ts) {
  if (!ts) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 90) return '刚刚';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

function renderUpdate(u) {
  const box = $('#update-box');
  const pill = $('#pill-update');
  const apply = $('#btn-update-apply');
  if (!box) return;
  const cur = (state.overview && state.overview.version) || '?';
  u = u || {};

  // 这个结果多半是**后台每天自动查一次**留下的（门店同事不会主动点按钮）。
  // 所以要说清"上次查是什么时候" —— 否则报错时人不知道该不该信它。
  const when = formatAgo(u.checked_at);

  if (u.error) {
    box.innerHTML = `<div class="banner warn">查不了更新：${esc(u.error)}
      <br><span class="hint">不影响对账。门店电脑连不上 GitHub 是正常的 —— 可以手动拷包升级。
      ${when ? `上次成功查到是 ${when}。` : ''}</span></div>`;
    apply.hidden = true;
    if (pill) pill.hidden = true;
    return;
  }
  if (!u.latest) {
    box.innerHTML = `<div class="banner">现在是 <b>v${esc(cur)}</b>。
      <span class="hint">后台每天自动查一次，也可以点「检查更新」现在查。</span></div>`;
    apply.hidden = true;
    if (pill) pill.hidden = true;
    return;
  }
  if (u.has_update) {
    box.innerHTML = `<div class="banner warn">🆕 有新版本：<b>v${esc(u.latest)}</b>
      <span class="hint">（现在是 v${esc(cur)}${when ? `，${when}查到的` : ''}）</span>
      <br><span class="hint">更新只会覆盖代码，门店配置 / 账号会话 / 历史报告都不会动。
      更新完控制台会自己重启，刷新一下页面即可。</span></div>`;
    apply.hidden = false;
    if (pill) { pill.hidden = false; pill.className = 'pill warn'; pill.textContent = '有新版本'; }
  } else {
    box.innerHTML = `<div class="banner ok">✅ 已是最新版 <b>v${esc(cur)}</b>
      ${when ? `<span class="hint">（${when}查过）</span>` : ''}</div>`;
    apply.hidden = true;
    if (pill) pill.hidden = true;
  }
}

function updateMsg(html, kind) {
  const el = $('#update-msg');
  if (el) el.innerHTML = html || '';
}

/* ─────────────────────── 历史版本 / 回退 ─────────────────────── */

// 提交时间（ISO）→ 本地短格式。只给人看个大概，不追求精确。
function shortDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return esc(iso.slice(0, 10));
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadHistory() {
  const btn = $('#btn-history-load');
  const list = $('#history-list');
  const msg = $('#history-msg');
  if (!list) return;
  btn.disabled = true; btn.textContent = '读取中…';
  if (msg) msg.textContent = '';
  try {
    const res = await api('/api/update?history=1');
    if (!res.ok) {
      list.innerHTML = `<div class="banner warn">读不到历史版本：${esc(res.message || '未知原因')}
        <br><span class="hint">通常是 GitHub 连不上或被限流了（匿名每小时 60 次）。歇一会儿再试。</span></div>`;
      return;
    }
    const cur = res.current || '';
    renderHistory(res.versions || [], cur);
  } catch (e) {
    if (msg) msg.textContent = '读取失败：' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = '重新读取';
  }
}

function renderHistory(versions, cur) {
  const list = $('#history-list');
  if (!versions.length) {
    list.innerHTML = '<div class="empty">没读到带版本号的提交</div>';
    return;
  }
  const rows = versions.map((v) => {
    const isCur = v.version === cur;
    // ⚠ 想放 HTML 必须包成 `{html: ...}` —— `table()` 对**字符串**单元格默认转义。
    //   这里第一版把带 `<b>` 的字符串直接当单元格传，结果页面上原样显示了
    //   `<b>v1.4.6</b>` 这串标签（门店就是这么看到它的）。
    const label = { html: isCur
      ? `<b>v${esc(v.version)}</b> <span class="pill ok">当前</span>`
      : `<b>v${esc(v.version)}</b>` };
    return [
      label,
      esc(shortDate(v.date)),
      { html: `<span class="mono">${esc(v.short)}</span>` },
      { html: isCur ? '<span class="hint">—</span>'
                    : `<button class="btn ghost small" data-rollback="${esc(v.sha)}"
                         data-ver="${esc(v.version)}">回退</button>` },
    ];
  });
  list.innerHTML = `<p class="hint">当前 v${esc(cur)}，下面是最近有版本号的提交（新的在上）：</p>`
    + table(['版本', '提交时间', 'commit', ''], rows);
}

async function doRollback(sha, ver) {
  if (!confirm(`回退到 v${ver}？\n\n`
    + `只覆盖代码 —— 门店配置、账号会话、历史报告都不会动。\n`
    + `回退完控制台会自动重启。`)) return;
  const msg = $('#history-msg');
  if (msg) msg.textContent = `正在下载 v${ver}…`;
  try {
    const res = await api('/api/update', { method: 'POST', body: { ref: sha } });
    if (msg) msg.textContent = res.message || (res.ok ? '已回退' : '回退失败');
    if (res.ok) {
      toast(`已回退到 v${ver}，控制台正在重启…`, 'ok');
      setTimeout(() => location.reload(), 4000);
    }
  } catch (e) {
    if (msg) msg.textContent = '回退失败：' + e.message;
  }
}

$('#btn-history-load') && $('#btn-history-load').addEventListener('click', loadHistory);

// ⚠ 这些按钮是**动态渲染**出来的，绑不到具体元素上 —— 用事件委托，
//   并且先判断 target 有没有那个属性（点表格空白处时 dataset 会报错）。
$('#history-list') && $('#history-list').addEventListener('click', (ev) => {
  const btn = ev.target.closest && ev.target.closest('[data-rollback]');
  if (!btn) return;
  doRollback(btn.dataset.rollback, btn.dataset.ver);
});

$('#btn-update-check') && $('#btn-update-check').addEventListener('click', async () => {
  const btn = $('#btn-update-check');
  btn.disabled = true; btn.textContent = '检查中…';
  updateMsg('<span class="hint">正在问 GitHub…</span>');
  try {
    const u = await api('/api/update?force=1');
    renderUpdate(u);
    updateMsg(u.error ? '' : '<span class="hint">刚查过</span>');
  } catch (e) {
    updateMsg(`<span style="color:var(--bad)">${esc(e.message)}</span>`);
  } finally {
    btn.disabled = false; btn.textContent = '检查更新';
  }
});

$('#btn-update-apply') && $('#btn-update-apply').addEventListener('click', async () => {
  const cur = (state.overview && state.overview.version) || '?';
  if (!confirm('现在更新代码？\n\n门店配置、账号会话、历史报告都不会动。\n更新完控制台会自动重启。')) return;
  const btn = $('#btn-update-apply');
  btn.disabled = true; btn.textContent = '更新中…';
  updateMsg('<span class="hint">正在下载并覆盖代码…</span>');
  try {
    const r = await api('/api/update', { method: 'POST', body: {} });
    if (!r.ok) {
      updateMsg(`<span style="color:var(--bad)">${esc(r.message || '失败')}</span>`);
      btn.disabled = false; btn.textContent = '立即更新';
      return;
    }
    const n = (r.count || 0);
    updateMsg(`<span style="color:var(--ok)">✅ 已更新 v${esc(cur)} → v${esc(r.to || '?')}，`
              + `改了 ${n} 个文件</span>`);
    // 服务马上会自己关掉再起来 —— 等它回来再刷新页面
    setTimeout(() => {
      const t = setInterval(async () => {
        try { await api('/api/health'); clearInterval(t); location.reload(); } catch (e) {}
      }, 2000);
    }, 3000);
  } catch (e) {
    updateMsg(`<span style="color:var(--bad)">${esc(e.message)}</span>`);
    btn.disabled = false; btn.textContent = '立即更新';
  }
});

/* ───────────────────────────── 设置 ───────────────────────────── */

async function loadConfig() {
  let v;
  try { v = await api('/api/config'); } catch (e) { return toast(e.message, 'bad'); }
  CONFIG_FIELDS.forEach(([key, id]) => {
    const el = $('#' + id);
    if (el) el.value = v[key] == null ? '' : v[key];
  });
  if (state.overview) renderSchedule(state.overview.schedule || {});
  loadErp();
  loadMail();
  loadWecom();
  loadService();
}

// 收集设置页所有可改字段 → 提交。门店卡片和页面底部各有一个按钮，共用这段。
async function saveConfig(msgEl) {
  const values = {};
  CONFIG_FIELDS.forEach(([key, id]) => {
    const el = $('#' + id);
    if (!el) return;
    const val = el.value.trim();
    if (val === '') { values[key] = ''; return; }
    if (key.startsWith('check.')) {
      const n = Number(val);
      if (Number.isNaN(n)) return;          // 数字填错了就跳过，别提交个 NaN
      values[key] = n;
    } else values[key] = val;
  });
  try {
    const res = await api('/api/config', { method: 'PUT', body: { values } });
    const when = new Date().toLocaleTimeString();
    $('#config-msg').textContent = '已保存 ' + when;
    if (msgEl) msgEl.textContent = '已保存 ' + when;
    toast('配置已保存（注释保留了）', 'ok');
    loadOverview();
    return res;
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
    if (msgEl) msgEl.textContent = '保存失败';
  }
}

// ⚠ 门店那三行在**设置页最上面**，而底部那个总保存按钮隔了一整屏 ——
//   实测有门店改完门店配置找不到保存按钮。所以门店卡片里单独放一个。
$('#btn-save-store') && $('#btn-save-store').addEventListener('click', async () => {
  const btn = $('#btn-save-store');
  const msg = $('#store-msg');
  btn.disabled = true; btn.textContent = '保存中…';
  if (msg) msg.textContent = '';
  await saveConfig(msg);
  btn.disabled = false; btn.textContent = '保存门店配置';
  // 顺手提醒一句：会话是按店存的，改了编码就得重新登录。
  // （不判断"到底改没改" —— 那要比较保存前后的值，而 loadOverview 是异步的，
  //   比出来会时对时错。宁可每次都提醒，也别给一句会骗人的话。）
  if (msg && $('#cfg-store_code').value.trim()) {
    msg.textContent = '已保存。若改过华为门店编码，请到「会话」页重新登录一次';
  }
});

$('#btn-save-config').addEventListener('click', () => saveConfig(null));

// 每行的操作按钮。⚠ table() 的单元格默认**转义** —— 想放原生 HTML 必须包 {html:}
function schedRowButtons(t) {
  const full = esc(t.full_name || t.name);      // 发给后端的（Windows 上是 \TaskName）
  const label = esc(t.name);                     // 给人看的
  return { html:
    `<button class="btn ghost small nowrap" data-sched-run="${full}" data-sched-label="${label}">执行</button>`
    + ` <button class="btn ghost small nowrap" data-sched-del="${full}" data-sched-label="${label}">删除</button>` };
}

function renderSchedule(sch) {
  const box = $('#schedule-box');
  if (!box) return;
  const tasks = sch.tasks || [];
  if (!tasks.length) {
    box.innerHTML = `<div class="banner bad">
      还没有定时任务，得手动跑才对账。选个时间点右边「注册计划任务」。
      ${sch.error ? `<br><span class="hint">${esc(sch.error)}</span>` : ''}
    </div>`;
    return;
  }
  // 每个任务一行、各自一个删除按钮 —— 删的是**这一行**，不是全部
  // ⚠ table() 的单元格默认**转义**，要放原生 HTML 必须包成 {html: ...}，
  //   否则用户会看到 `<span class="hint">` 这种源码（踩过一次）
  const rows = tasks.map((t) => [
    t.name,
    // ⚠ 读不到时间**不等于**没设时间：任务多半是**以管理员身份建**的，
    //   所有者是 Administrators，而服务现在是普通权限（过滤令牌）→ /query 被拒。
    //   原来只写一句"时间没读出来"，用户看到的就是
    //   "没有管理员权限就看不到定时任务的设置"。这里把原因和修法一起给出来。
    t.time ? `每天 ${t.time}` : { html: t.unreadable ? '<span class="hint" style="color:var(--warn)">读不到（权限不够）</span>' : '<span class="hint">时间没读出来</span>' },
    t.enabled === false ? { html: '<span class="bad-text">已停用</span>' } : '已启用',
    { html: `<span class="hint mono">${esc((t.command || '').slice(-70))}</span>` },
    schedRowButtons(t),
  ]);
  // 老版本留空注册叫「CBG报量对账」（没有时间后缀）。它和新的带时间任务会**并存**，
  // 两条都会每天跑一遍 —— 所以必须提示，不能装作没看见。
  const legacy = tasks.filter((t) => t.name === sch.task_name);
  box.innerHTML = `<div class="banner ok">
      ✅ 已注册 <b>${tasks.length}</b> 个定时任务 · 平台 ${esc(sch.platform)}
      ${tasks.length > 1 ? '<br><span class="hint">删除只删你点的那一行，别的任务不动。</span>' : ''}
    </div>`
    + `<p class="hint" style="margin:-4px 0 10px 0">要再加一个：改上面的时间（或填个任务名），
        再点一次「添加定时任务」。同一时间重复添加是<b>覆盖</b>，不同时间就是两个任务。</p>`
    + (sch.script_rebuilt ? `<div class="banner info">
        🔧 运行脚本（<code>run.bat</code>）是旧版留下的，已按当前版本<b>自动重建</b> ——
        之前那个黑窗、日志格式的问题就是它造成的。明天到点跑的就是新的了。
      </div>` : '')
    + (tasks.some((t) => t.unreadable) ? `<div class="banner warn">
        ⚠️ 有一个定时任务**读不到详情**（时间和命令都是空的）。
        <br><span class="hint">它不是"没设"，而是**以前用管理员身份建的** ——
        任务归 <code>Administrators</code> 所有，而现在服务是<b>普通权限</b>，
        所以连查都查不动。这不影响它到点自己跑。</span>
        <br><span class="hint">要能看、能删、能改：点下面这个按钮，
        它会<b>弹一次 UAC</b>，用管理员权限把这条任务<b>按上面「执行时间」里的值重新注册一遍</b>。
        <br>重注册之后 Windows 那边还是读不到详情（任务归 <code>Administrators</code> 所有），
        但<b>注册用的参数我们记了一份</b>，所以时间、命令照常显示、删除照常可用。</span>
        <br><span class="hint">⚠ 点之前先确认上面的<b>「执行时间」</b>是你想要的 —— 修复就是拿它去重建的。</span>
        <br><button class="btn" id="btn-sched-fix">以管理员身份修复定时任务</button>
        <span class="hint" id="btn-sched-fix-msg"></span>
      </div>` : '')
    + (legacy.length ? `<div class="banner warn">
        ⚠️ 表里有一条<b>旧任务「${esc(sch.task_name)}」</b>（名字里没有执行时间）——
        它也会每天跑一次，等于<b>一天对账两遍</b>。
        不需要的话点这一行的「删除」。
      </div>` : '')
    + table(['任务名', '执行时间', '状态', '跑什么', ''], rows, ['', '', '', '', 'right']);
  if (tasks[0] && tasks[0].time) {
    $('#sched-time').value = tasks[0].time;
    syncSchedPlaceholder();      // ⚠ 程序赋值不会触发 input 事件，得手动同步一次
  }
  const nameBox = $('#sched-name');
  if (nameBox && !nameBox.value && tasks.length === 1 && tasks[0].name !== sch.task_name) {
    nameBox.value = tasks[0].name;
  }

  // 「以管理员身份修复定时任务」—— 只在真读不到详情时才出现。
  // ⚠ 每次重渲染都要重新绑（上面刚 innerHTML 覆盖过）。
  const fixBtn = document.getElementById('btn-sched-fix');
  if (fixBtn) {
    fixBtn.addEventListener('click', async () => {
      fixBtn.disabled = true;
      const msg = document.getElementById('btn-sched-fix-msg');
      if (msg) msg.textContent = '已弹出 UAC 窗口 —— 请点「是」…';
      // ⚠ 发**叶子名**（`t.name`），不是 `full_name` ——
      //   full_name 带反斜杠，后端注册时会当非法字符拒掉（400），
      //   表现是"点了完全没反应"。这正是这个按钮以前的样子。
      const target = tasks.find((t) => t.unreadable) || tasks[0] || {};
      try {
        const r = await api('/api/elevate', {
          method: 'POST',
          body: { what: 'schedule', time: $('#sched-time').value || '21:00',
                  days_ago: Number($('#sched-days-ago').value),
                  name: target.name || '' },
        });
        const text = r.ok
          ? '✅ 已注册。注册用的参数我们记下来了，界面上照常显示时间和命令。'
          : ('❌ ' + (r.message || '没成'));
        if (msg) msg.textContent = text;
        toast(r.ok ? '定时任务已修复' : '没成', r.ok ? 'ok' : 'bad');
        loadOverview();
      } catch (e) {
        if (msg) msg.textContent = '❌ ' + e.message;
      } finally {
        fixBtn.disabled = false;
      }
    });
  }
}

// 盯着 out/run.log，把计划任务跑的过程显示出来。
// 结束标志：日志里出现 exit=N（run.bat 自己在最后追加的）。
async function watchRunLog(triggeredAt) {
  const box = () => $('#runlog-box');
  const deadline = Date.now() + 5 * 60 * 1000;      // 对账一般 1~2 分钟，给足 5 分钟
  let last = '';
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 2000));
    let d;
    try { d = await api('/api/runlog'); } catch (e) { continue; }
    const el = box();
    if (!el) return;                                 // 用户切走了，别再轮询
    if (d.exists && d.mtime > triggeredAt) {
      last = (d.truncated ? '…（只显示最后若干行）\n' : '') + (d.lines || []).join('\n');
      el.textContent = last;
      el.scrollTop = el.scrollHeight;
    }
    if (d.exit !== null && d.exit !== undefined && d.mtime > triggeredAt) {
      // 跑完了 —— 把"推没推、为什么没推"直接摆出来
      const push = [];
      if (d.wecom) push.push(d.wecom);
      if (d.mail) push.push(d.mail);
      const good = d.exit === 0 || d.exit === 3;     // 3 = 有差异，也算正常跑完
      // ⚠ 光看退出码没用：用户报过"退出码 120"，而 120 不在我们任何约定里。
      //   真正有用的是**日志里有没有对账的输出** —— 一行都没有就说明
      //   Python 根本没跑起来（路径不对 / 假 python），跟"对账失败"是两回事。
      let head;
      if (good) {
        head = `<div class="banner ok">✅ 跑完了（退出码 ${d.exit}${
          d.exit === 3 ? '：<b>有差异</b>，报告已生成' : '：没有差异'}）
          <br><span class="hint">跟到点自动跑的结果完全一样。</span></div>`;
      } else if (!d.has_output) {
        head = `<div class="banner bad">❌ 退出码 ${d.exit}，而且日志里<b>只有退出码、没有对账的任何输出</b>
          <br><span class="hint">这说明<b>对账程序根本没跑起来</b>，不是对账本身失败。最常见的两个原因：</span>
          <br><span class="hint">1. <code>run.bat</code> 里的 Python 路径不对（升级时目录挪过？）——
            到上面「添加定时任务」重注册一次，会重建这个脚本</span>
          <br><span class="hint">2. <code>python</code> 指向了 Microsoft Store 的那个"假 python"——
            双击 <code>run-now.bat</code> 会在屏幕上直接报出来</span></div>`;
      } else {
        head = `<div class="banner bad">❌ 跑完了但<b>没成功</b>（退出码 ${d.exit}）
          <br><span class="hint">1=会话过期（去「会话」页刷新）· 2=取数失败 · 9=程序出错${
            [0, 1, 2, 3, 9].includes(d.exit) ? '' : ` · <b>${d.exit} 不在约定里，看下面日志</b>`}</span></div>`;
      }
      // 推送结果**逐行保留自己的记号**（✅ / 跳过），整块用中性底色 ——
      // 用绿色的话"邮件跳过"也会跟着变绿，反而误导
      const pushBox = push.length
        ? `<div class="banner info">${push.map(esc).join('<br>')}</div>`
        : '<div class="banner warn">日志里没看到推送相关的行 —— 可能没配邮件/企微。</div>';
      const parent = el.parentElement;
      parent.insertAdjacentHTML('afterbegin', head + pushBox);
      // 把"正在跑"那条撤掉 —— 都跑完了还挂着会让人以为还在跑
      const prog = document.getElementById('runlog-progress');
      if (prog) prog.remove();
      toast(good ? '跑完了' : '跑完了但有错', good ? 'ok' : 'bad');
      loadOverview();                                // 报告列表要刷新
      return;
    }
  }
  const el = box();
  if (el) el.textContent = last + '\n\n（等了 5 分钟还没结束，去 out\\run.log 看吧）';
}

// 事件委托：表是每次重渲染的，直接绑在按钮上会丢
document.addEventListener('click', async (ev) => {
  // ---- 立即执行这一行
  const runBtn = ev.target.closest('[data-sched-run]');
  if (runBtn) {
    const label = runBtn.dataset.schedLabel || runBtn.dataset.schedRun;
    runBtn.disabled = true;
    const box = $('#sched-result');
    try {
      const r = await api('/api/schedule/run', {
        method: 'POST', body: { name: runBtn.dataset.schedRun },
      });
      if (!r.ok) {
        box.innerHTML = `<div class="banner bad">触发「${esc(label)}」失败：${esc(r.message || '')}</div>`;
        toast('触发失败', 'bad');
        return;
      }
      // ⚠ 触发是个**黑盒** —— schtasks 在后台另起一个进程，界面上什么都看不到。
      //   用户唯一能观察到的就是"点了没反应/没推送"。所以这里盯着日志把它显示出来。
      box.innerHTML = `<div class="banner ok" id="runlog-progress">▶️ 已触发「${esc(label)}」，正在跑…
        <br><span class="hint">跟到点自动跑走的是同一条路，所以这也能验证任务配得对不对。
        下面会实时显示它跑到哪了。</span></div>
        <pre class="runlog" id="runlog-box">（等它开始写日志…）</pre>`;
      toast('已触发，正在跑', 'ok');
      await watchRunLog(Date.now() / 1000);
    } finally {
      runBtn.disabled = false;
    }
    return;
  }

  const btn = ev.target.closest('[data-sched-del]');
  if (!btn) return;
  const name = btn.dataset.schedDel;                 // 发给后端的（Windows 上是 \TaskName）
  const label = btn.dataset.schedLabel || name;      // 给人看的
  if (!confirm(`删除定时任务「${label}」？\n\n只删这一个，别的任务不动。`)) return;
  btn.disabled = true;
  try {
    const r = await api('/api/schedule?name=' + encodeURIComponent(name), { method: 'DELETE' });
    $('#sched-result').innerHTML = r.ok
      ? `<div class="banner warn">已删除「${esc(label)}」。</div>`
      : `<div class="banner bad">删除「${esc(label)}」失败：${esc(r.message || '')}</div>`;
    toast(r.ok ? '已删除' : '删除失败', r.ok ? 'ok' : 'bad');
    loadOverview();
  } catch (e) {
    btn.disabled = false;
    toast('删除失败：' + e.message, 'bad');
  }
});

// 让「留空 = …」跟着时间输入实时变 —— 用户才知道留空到底会注册成什么名字
function syncSchedPlaceholder() {
  const box = $('#sched-name');
  const tm = $('#sched-time');
  if (!box || !tm) return;
  const [hh, mm] = (tm.value || '21:00').split(':');
  box.placeholder = `留空 = CBG报量对账-${Number(hh)}点${mm}`;
}
$('#sched-time').addEventListener('input', syncSchedPlaceholder);
$('#sched-time').addEventListener('change', syncSchedPlaceholder);
syncSchedPlaceholder();

$('#btn-sched-install').addEventListener('click', async () => {
  const time = $('#sched-time').value || '21:00';
  const name = ($('#sched-name').value || '').trim();
  const daysAgo = Number($('#sched-days-ago').value);
  try {
    const r = await api('/api/schedule', {
      method: 'POST',
      body: { time, days_ago: daysAgo, name },
    });
    // ⚠ 注册失败多半是**权限**一条：这条定时任务以前可能是管理员身份的
    //   服务建的，普通权限覆盖不了（`schtasks ... /f` 拒绝访问）。
    //   所以失败时直接把「以管理员身份重试」摆在眼前 —— 而不是丢一条
    //   要用户自己去开管理员命令行的命令（那个也留着，作为退路）。
    const retry = !r.ok
      ? `<br><button class="btn" id="btn-sched-elevate">以管理员身份重试</button>
         <span class="hint">只弹这一次 UAC，注册完就结束，不影响别的操作</span>`
      : '';
    $('#sched-result').innerHTML = r.ok
      ? `<div class="banner ok">✅ 已注册「${esc(r.task || name || '')}」，每天 ${esc(r.time)} 跑。
           <br><span class="hint mono">${esc(r.script || '')}</span></div>`
      : `<div class="banner bad">注册失败：${esc(r.message || '')}
           ${retry}
           ${r.manual ? `<br><span class="hint">也可以拿管理员权限手动跑：</span>
             <br><span class="mono">${esc(r.manual)}</span>` : ''}</div>`;
    const elev = $('#btn-sched-elevate');
    if (elev) {
      elev.addEventListener('click', async () => {
        elev.disabled = true;
        $('#sched-result').innerHTML =
          '<div class="banner warn">已弹出 UAC 窗口 —— 请点「是」，跑完那个黑窗口会停住等你按回车…</div>';
        try {
          const e2 = await api('/api/elevate', {
            method: 'POST',
            body: { what: 'schedule', time, days_ago: daysAgo, name },
          });
          $('#sched-result').innerHTML = e2.ok
            ? `<div class="banner ok">✅ 已注册（管理员权限）。
                 <br><span class="hint">提权建的任务归 <code>Administrators</code> 所有，
                 Windows 那边读不到详情 —— 但注册参数我们记了一份，
                 界面上的时间和命令照常显示。</span></div>`
            : `<div class="banner bad">还是没成：${esc(e2.message || '')}</div>`;
          toast(e2.ok ? '已注册定时任务' : '注册失败', e2.ok ? 'ok' : 'bad');
          loadOverview();
        } catch (err) {
          $('#sched-result').innerHTML = `<div class="banner bad">${esc(err.message)}</div>`;
        }
      });
    }
    toast(r.ok ? '已注册定时任务' : '注册失败', r.ok ? 'ok' : 'bad');
    loadOverview();
  } catch (e) { toast('注册失败：' + e.message, 'bad'); }
});

/* ───────────────────────────── 启动 ───────────────────────────── */

loadOverview();
loadBrowserInfo();
loadHwLogin();
// 刷新页面时如果抓取还在跑，接着显示进度（别让用户以为丢了）
api('/api/session/auto').then((j) => {
  if (j && j.running) {
    $('#btn-auto-login').disabled = true;
    $('#btn-auto-refresh').disabled = true;
    pollAuto();
  }
}).catch(() => {});
setInterval(() => {
  // 已经有日志在轮询时不打扰；只用来刷新会话/定时状态徽章
  if (!state.timer) loadOverview();
}, 30000);
