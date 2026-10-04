'use strict';

/* POS 合规页面：只读后端已计算的年度结果，生命周期由公共 nav 注册表管理。 */

let posPageMounted = false;
let posPageEpoch = 0;
let posPageReadController = null;
let posRefreshHandler = null;

// ⚠ 不按达标线染色 —— **达标线还没定**，染了就是编一个阈值出来。
//   等用户给了线，再在 pct() 里加 .ok/.warn/.bad。
const pct = (v) => (v == null ? '—' : v.toFixed(2) + '%');
const money = (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function posKpi(title, cur, appeal, den, provisional, extra) {
  return `<div class="kpi"><div class="k">${esc(title)}${provisional ? ' ⚠暂定' : ''}</div>`
    + `<div class="v">${pct(cur)}</div>`
    + `<div class="k" style="margin-top:4px">申诉后 ${pct(appeal)} · 分母 ${money(den)}</div>`
    + (extra ? `<div class="k" style="margin-top:2px">${esc(extra)}</div>` : '')
    + '</div>';
}

function mountPosPage() {
  posPageMounted = true;
  const btn = $('#btn-refresh-pos');
  if (btn && !posRefreshHandler) {
    posRefreshHandler = (e) =>
      refreshWithFetch('pos', e.currentTarget, () => loadPos());
    btn.addEventListener('click', posRefreshHandler);
  }
}

function unmountPosPage() {
  posPageMounted = false;
  posPageEpoch += 1;
  if (posPageReadController) posPageReadController.abort();
  posPageReadController = null;
  const btn = $('#btn-refresh-pos');
  if (btn && posRefreshHandler) btn.removeEventListener('click', posRefreshHandler);
  posRefreshHandler = null;
}

async function loadPos() {
  if (!posPageMounted) return;
  if (posPageReadController) posPageReadController.abort();
  const epoch = ++posPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  posPageReadController = controller;
  try {
    const d = await api('/api/pos', controller ? { signal: controller.signal } : {});
    if (!posPageMounted || epoch !== posPageEpoch) return;
    const meta = $('#pos-meta');
    if (!d.exists) {
      meta.textContent = '';
      $('#pos-cards').innerHTML = '';
      $('#pos-table').innerHTML = '<div class="empty">' + esc(d.hint || d.error || '还没有数据') + '</div>';
      return;
    }
    renderPos(d);
  } catch (e) {
    if (!posPageMounted || epoch !== posPageEpoch || (e && e.name === 'AbortError')) return;
    toast('读取 POS 数据失败：' + e.message, 'bad');
  } finally {
    if (posPageReadController === controller) posPageReadController = null;
  }
}

function renderPos(d) {
  const rows = d.rows || [];
  $('#pos-meta').textContent =
    `${d.year} 年 · ${d.orders} 单 / 退货 ${d.returns} 张 · 算于 ${d.generated_at}`;

  // ⭐ **官方口径在前**（PPT《POS合规：计算逻辑及方法》）——它跟财经那份成绩
  //   是一个算法（返利按它结算）；我们自己的旧口径跟在后面当对照。
  //   ⚠ 老 `pos-<年>.json`（没有 `official`）时回落到旧口径，**不假装是官方**。
  const off = (r) => (r.official || {}).label || {};
  const withRate = rows.filter((r) => off(r).rate != null || (r.label && r.label.rate != null));
  const last = withRate[withRate.length - 1] || rows[rows.length - 1] || null;
  $('#pos-cards').innerHTML = last
    ? posKpi(last.month + ' 官方 · 按标签',
             off(last).rate != null ? off(last).rate : last.label.rate,
             last.label.ap_rate, off(last).total != null ? off(last).total : last.label.den,
             last.provisional,
             off(last).rate != null
               ? `旧口径 ${pct(last.label.rate)}（扣退货、整月汇总）· ${off(last).days || '—'} 天`
               : '')
      + posKpi(last.month + ' 按备注',
               (last.official || {}).remark ? last.official.remark.rate : last.remark.rate,
               last.remark.ap_rate, last.remark.den, last.provisional)
    : '';

  const head = ['月份', '官方(标签)', '官方(备注)', '我们(标签)', '我们(备注)',
                '申诉后(标签)', '分母/单数'];
  const body = rows.map((r) => [
    r.month + (r.provisional ? ' ⚠暂定' : ''),
    pct(off(r).rate), pct(((r.official || {}).remark || {}).rate),
    pct(r.label.rate), pct(r.remark.rate),
    pct(r.label.ap_rate),
    `${money(r.label.den)} / ${r.label.orders}`,
  ]);
  $('#pos-table').innerHTML = table(head, body,
    ['', 'num', 'num', 'num', 'num', 'num', 'num']);
  const note = $('#pos-note');
  if (note) {
    const d = off(last || {});
    note.textContent = (d.rate == null) ? '' :
      `官方口径 = 现金+记账 扣减 · 不扣退货 · 先按天算再取日均值（本月 ${d.days} 天）；`
      + `同月汇总法算出来是 ${pct(d.rate_sum)}（差 ${(d.rate - d.rate_sum).toFixed(2)} 个点）`;
  }
}

registerPage('pos', {
  mount: mountPosPage,
  unmount: unmountPosPage,
  load: loadPos,
  refresh: loadPos,
});
