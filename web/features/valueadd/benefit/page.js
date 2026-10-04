'use strict';

/* 无忧会员权益页：四视图渲染、读取、刷新和导出。 */

/* ── 无忧会员权益（增值 · benefit）：一页四视图（店 / 区 / 人 / 赛道） ── */
const benefitState = { view: 'stores', data: null };

function benefitPct(x, dig) {
  const v = Number(x) || 0;
  return (v * 100).toFixed(dig == null ? 1 : dig) + '%';
}
function benefitNum(x) {
  if (x == null || x === '') return '';
  const n = Number(x);
  if (!isFinite(n)) return String(x);
  return Math.abs(n - Math.round(n)) < 1e-9 ? String(Math.round(n)) : n.toFixed(2);
}
/** 台量目标 / 台量进度 / 新机 —— **只显示整数部分**（截断，不是四舍五入）。
 *  用户 2026-09-26：「台量目标、台量进度和新机都只显示整数部分吧」——
 *  这三列原走 benefitNum 给 toFixed(2)，列表里全是 45.83 这种小数。
 *  ⚠ 「整数部分」= Math.trunc：45.83 → 45；四舍五入会报 46，那不是一个意思。 */
function benefitInt(x) {
  if (x == null || x === '') return '';
  const n = Number(x);
  if (!isFinite(n)) return String(x);
  return String(Math.trunc(n));
}
function benefitMoney(x) {
  const n = Number(x) || 0;
  return n.toFixed(n && Math.abs(n - Math.round(n)) > 1e-9 ? 2 : 0);
}
function benefitRateCls(v, kind) {
  if (kind === 'overall') {
    if (v < 0.6) return 'film-lo';
    if (v >= 0.9) return 'film-hi';
    return '';
  }
  if (kind === 'attach') {
    if (v < 0.35) return 'film-lo';
    if (v >= 0.5) return 'film-hi';
    return '';
  }
  if (kind === 'goal') {
    if (v < 0.6) return 'film-lo';
    if (v >= 1) return 'film-hi';
    return '';
  }
  return '';
}
function benefitCell(v, kind, dig) {
  if (kind === 'pct') {
    return '<td class="num ' + benefitRateCls(Number(v) || 0, dig) + '">'
      + benefitPct(v, dig === 'overall' ? 1 : undefined) + '</td>';
  }
  if (kind === 'money') return '<td class="num">' + benefitMoney(v) + '</td>';
  if (kind === 'rate') return '<td class="num">' + benefitPct(v) + '</td>';
  if (kind === 'int') return '<td class="num">' + benefitInt(v) + '</td>';
  return '<td class="num">' + benefitNum(v) + '</td>';
}

function benefitLoading() {
  const box = $('#benefit-table');
  if (box) {
    box.innerHTML = '<div class="film-loading"><span class="film-spin"></span>'
      + '正在从本地订单库计算…</div>';
  }
  const meta = $('#benefit-meta');
  if (meta) meta.textContent = '';
}

function renderBenefit(d) {
  benefitState.data = d;
  const meta = $('#benefit-meta'), box = $('#benefit-table'),
    warn = $('#benefit-warn'), legend = $('#benefit-legend');
  if (!d || d.ok === false) {
    if (warn) warn.innerHTML = '<div class="empty">'
      + esc((d && (d.why || d.error)) || '算不出来') + '</div>';
    if (box) box.innerHTML = '';
    return;
  }
  if (warn) warn.innerHTML = '';
  if (meta) {
    const bits = [];
    if (d.start) bits.push(d.start + ' ~ ' + (d.end || ''));
    if (d.as_of) bits.push('数据截至 ' + d.as_of);
    if (d.store_filter) bits.push('仅：' + d.store_filter);
    meta.textContent = bits.join(' · ');
  }
  const view = benefitState.view;
  if (view === 'regions') renderBenefitRegions(d);
  else if (view === 'people') renderBenefitPeople(d);
  else if (view === 'tracks') renderBenefitTracks(d);
  else renderBenefitStores(d);
  if (legend) {
    legend.innerHTML = '新机 = 手机零售净 + 分销净（美团/抖音），<b>不乘 0.9</b>（与防护膜页不同）。'
      + '无忧 = 优选/超值/全能/旗舰 + 399/499 套装；Care+ = 延保服务净件。'
      + '总达成率 = 新机达成×0.3 + 连带达成×0.7。'
      + '名册 = 系统人店表（云商组织架构，不是 Excel）；'
      + '赛道 / 台量目标 / 区域在 <code>config/valueadd-benefit.yaml</code>。'
      + (d.note ? '　' + esc(d.note) : '');
  }
}

function renderBenefitStores(d) {
  const box = $('#benefit-table');
  if (!box) return;
  // ⚠ 只画**有赛道**的店（用户 2026-09-22：李哥庄不计入赛道考核）——
  //   无赛道的店（如胶州李哥庄）仍参与「区域达成」汇总，但不进这张表。
  const rows = (d.stores || []).filter((r) => r.track);
  const s = d.summary || {};
  const tiers = d.tiers_meta || [];
  // ⚠ 按赛道分组展示（用户 2026-09-22：「门店达成按照赛道分组展示吧，现在混在一起」）
  //   顺序 A→B→C→D；组内按总达成率降序（一眼看谁在前面）。
  const TRACK_ORDER = ['A', 'B', 'C', 'D'];
  const by = {};
  rows.forEach((r) => {
    const k = r.track;
    if (!by[k]) by[k] = [];
    by[k].push(r);
  });
  const keys = TRACK_ORDER.filter((k) => by[k])
    .concat(Object.keys(by).filter((k) => TRACK_ORDER.indexOf(k) < 0));
  keys.forEach((k) => {
    by[k].sort((a, b) => (b.overall || 0) - (a.overall || 0)
      || String(a.store || '').localeCompare(String(b.store || '')));
  });

  // ⚠ 别再套 `max-height:70vh` —— 表在框里滚、卡片被压矮，框下面像给右下角
  //   状态球留了一大块空（2026-09-23 用户）。跟「周度目标达成」一致：
  //   横向 `.table-scroll`，纵向自然撑高、整页滚。
  let h = '<div class="table-scroll"><table class="film-table"><thead><tr>'
    + '<th>赛道</th><th>门店</th><th class="num">台量目标</th><th class="num">台量进度</th>'
    + '<th class="num">新机</th><th class="num">新机达成</th>'
    + '<th class="num">目标</th><th class="num">无忧</th><th class="num">Care+</th>'
    + '<th class="num">合计</th><th class="num">连带率</th><th class="num">连带达成</th>'
    + '<th class="num">总达成率</th>';
  tiers.forEach((t) => { h += '<th class="num">' + esc(t.label) + '</th>'; });
  h += '<th class="num">后返</th><th class="num">Care+利润</th><th class="num">权益利润</th>'
    + '<th class="num">利润合计</th><th class="num">台均</th><th class="num">店长奖</th>'
    + '</tr></thead><tbody>';

  const tr = (r, leadHtml, cls) => {
    // ⚠ 只有**门店行**上那几个数字可下钻（赛道小计 / 底部合计不是"某家店"）
    //   —— 同一套 `drillCell`，只换 `kind`；显示格式跟 `benefitCell` 对齐
    const isStore = !cls;
    const dnum = (mk, val, kind2) => (isStore
      ? drillCell('benefit', mk, r.store,
        kind2 === 'int' ? benefitInt(val)
          : kind2 === 'money' ? benefitMoney(val) : benefitNum(val))
      : benefitCell(val, kind2));
    let x = '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + leadHtml
      + '<td>' + esc(r.store || '') + '</td>'
      + benefitCell(r.day_target, 'int') + benefitCell(r.slot_progress, 'int')
      + dnum('new', r.new, 'int') + benefitCell(r.new_rate, 'pct', 'overall')
      + benefitCell(r.goal) + dnum('wuyou', r.wuyou) + dnum('care', r.care)
      + dnum('total', r.total)
      + benefitCell(r.attach, 'pct', 'attach')
      + benefitCell(r.attach_goal_rate, 'pct', 'goal')
      + benefitCell(r.overall, 'pct', 'overall');
    tiers.forEach((t) => { x += benefitCell((r.tiers || {})[t.key]); });
    // ⚠ 「利润合计」**不可点**：它是 后返 + 权益利润 + Care+利润（后返=台数×单价），
    //   不是这些单的毛利之和 —— 点开会对不上（同 benefit.compute.DRILL_KINDS 注）
    x += benefitCell(r.rebate, 'money') + dnum('care_profit', r.care_profit, 'money')
      + dnum('tier_profit', r.tier_profit, 'money')
      + benefitCell(r.profit_total, 'money')
      + benefitCell(r.avg_profit, 'money')
      + benefitCell(r.manager_bonus, 'money');
    return x + '</tr>';
  };
  const lead = (txt, n) => '<td class="region" rowspan="' + n + '">'
    + esc(txt) + '</td>';
  const emptyLead = '<td class="region"></td>';

  keys.forEach((k) => {
    const grp = by[k];
    grp.forEach((r, i) => {
      h += tr(r, i === 0 ? lead(k, grp.length) : '', '');
    });
    // 赛道小计 —— 比率用合计重算（跟 metric.summarize_stores 同口径）
    const sub = (typeof benefitSummarizeStores === 'function'
      ? benefitSummarizeStores(grp) : null) || { store: '共计：' };
    sub.store = '共计：' + k + '赛道';
    h += tr(sub, emptyLead, 'film-sum');
  });
  // 底部合计：**只合计有赛道的店**（与本表一致，不含李哥庄等）
  const total = (typeof benefitSummarizeStores === 'function'
    ? benefitSummarizeStores(rows) : s);
  if (total && total.store) h += tr(total, emptyLead, 'film-total');
  h += '</tbody></table></div>';
  box.innerHTML = h;
}

/** 赛道小计 —— 与后端 `metric.summarize_stores` 同口径（比率用合计重算）。 */
function benefitSummarizeStores(rows) {
  const sum = (k) => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0);
  const newN = sum('new');
  const wuyou = sum('wuyou');
  const care = sum('care');
  const total = wuyou + care;
  const goal = sum('goal');
  const slot = sum('slot_progress');
  const newRate = slot ? newN / slot : 0;
  const attachGoal = goal ? total / goal : 0;
  const tiers = {};
  (rows[0] && rows[0].tiers ? Object.keys(rows[0].tiers) : []).forEach((k) => {
    tiers[k] = rows.reduce((a, r) => a + (Number((r.tiers || {})[k]) || 0), 0);
  });
  const tp = sum('tier_profit');
  const cp = sum('care_profit');
  return {
    store: '合计', track: '', day_target: sum('day_target'),
    slot_progress: slot, new_retail: sum('new_retail'), new_online: sum('new_online'),
    new: newN, new_rate: newRate, goal, wuyou, care, total,
    attach: newN ? total / newN : 0, attach_goal_rate: attachGoal,
    overall: newRate * 0.3 + attachGoal * 0.7,
    tiers, tier_count: wuyou, rebate: sum('rebate'),
    care_profit: cp, tier_profit: tp, profit_total: sum('profit_total'),
    avg_profit: newN ? (tp + cp) / newN : 0,
    manager_bonus: sum('manager_bonus'),
  };
}

function renderBenefitRegions(d) {
  const box = $('#benefit-table');
  if (!box) return;
  const rows = d.regions || [];
  const s = d.summary_regions || {};
  let h = '<table class="film-table"><thead><tr>'
    + '<th>区域</th><th class="num">门店数</th><th class="num">新机</th>'
    + '<th class="num">区域目标</th><th class="num">无忧</th><th class="num">Care+</th>'
    + '<th class="num">合计</th><th class="num">连带率</th><th class="num">达成率</th>'
    + '<th class="num">后返</th><th class="num">权益利润</th><th class="num">利润合计</th>'
    + '<th class="num">台均</th></tr></thead><tbody>';
  const tr = (r, cls) => '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
    + '<td>' + esc(r.region || '') + '</td>'
    + benefitCell(r.stores) + benefitCell(r.new, 'int') + benefitCell(r.goal)
    + benefitCell(r.wuyou) + benefitCell(r.care) + benefitCell(r.total)
    + benefitCell(r.attach, 'pct', 'attach')
    + benefitCell(r.goal_rate, 'pct', 'goal')
    + benefitCell(r.rebate, 'money') + benefitCell(r.tier_profit, 'money')
    + benefitCell(r.profit_total, 'money') + benefitCell(r.avg_profit, 'money')
    + '</tr>';
  rows.forEach(r => { h += tr(r); });
  if (s && s.region) h += tr(s, 'film-total');
  h += '</tbody></table>';
  box.innerHTML = h
    + '<div class="hint" style="margin-top:8px">区长 PK：目标 = 新机×15%；达成率分子只算无忧。'
    + '政策（公司投 500 / 连带&lt;15% 区长投 500 / ≥20% 不用投 / 第一名且≥20% 拿全部）只展示，不代扣。</div>';
}

function renderBenefitPeople(d) {
  const box = $('#benefit-table');
  if (!box) return;
  const rows = d.people || [];
  const s = d.summary_people || {};
  const tiers = d.tiers_meta || [];
  let h = '<table class="film-table"><thead><tr>'
    + '<th class="num">名</th><th>门店</th><th>职位</th><th>姓名</th>'
    + '<th class="num">主机</th><th class="num">配比率</th>';
  tiers.forEach(t => { h += '<th class="num">' + esc(t.label) + '</th>'; });
  h += '<th class="num">Care+</th><th class="num">合计</th>'
    + '<th class="num">后返</th><th class="num">Care+利润</th><th class="num">权益利润</th>'
    + '<th class="num">利润合计</th><th class="num">台均</th>'
    + '<th class="num">奖金</th><th class="num">月度增收</th></tr></thead><tbody>';
  const tr = (r, cls) => {
    let x = '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + '<td class="num">' + esc(r.rank == null ? '' : String(r.rank)) + '</td>'
      + '<td>' + esc(r.store || '') + '</td>'
      + '<td>' + esc(r.title || '') + '</td>'
      + '<td>' + esc(r.name || '') + '</td>'
      + benefitCell(r.new, 'int') + benefitCell(r.attach_ratio, 'pct', 'attach');
    tiers.forEach(t => { x += benefitCell((r.tiers || {})[t.key]); });
    x += benefitCell(r.care) + benefitCell(r.total)
      + benefitCell(r.rebate, 'money') + benefitCell(r.care_profit, 'money')
      + benefitCell(r.tier_profit, 'money') + benefitCell(r.profit_total, 'money')
      + benefitCell(r.avg_profit, 'money') + benefitCell(r.bonus, 'money')
      + benefitCell(r.bonus_month, 'money');
    return x + '</tr>';
  };
  rows.forEach(r => { h += tr(r); });
  if (s && s.name) h += tr(s, 'film-total');
  h += '</tbody></table>';
  box.innerHTML = h;
}

function renderBenefitTracks(d) {
  const box = $('#benefit-table');
  if (!box) return;
  const tracks = d.tracks || [];
  let h = '<table class="film-table"><thead><tr>'
    + '<th>赛道</th><th class="num">奖金池</th><th class="num">名次</th><th>门店</th>'
    + '<th class="num">总达成率</th><th class="num">连带率</th>'
    + '<th class="num">应分</th><th>≥90%</th><th class="num">负激励</th>'
    + '<th class="num">实发</th></tr></thead><tbody>';
  tracks.forEach(t => {
    (t.rows || []).forEach((r, i) => {
      h += '<tr>'
        + '<td>' + (i === 0 ? esc(t.track || '') : '') + '</td>'
        + '<td class="num">' + (i === 0 ? benefitMoney(t.pool) : '') + '</td>'
        + benefitCell(r.rank) + '<td>' + esc(r.store || '') + '</td>'
        + benefitCell(r.overall, 'pct', 'overall')
        + benefitCell(r.attach, 'pct', 'attach')
        + benefitCell(r.share, 'money')
        + '<td>' + (r.gate_ok ? '是' : '<span style="color:var(--bad)">否</span>') + '</td>'
        + benefitCell(r.fine, 'money') + benefitCell(r.paid, 'money')
        + '</tr>';
    });
    h += '<tr class="film-sum"><td colspan="9">'
      + esc(t.track) + ' 赛道合计（连带达成 ' + benefitPct(t.attach_goal)
      + (t.fined ? ' · 已负激励 ' + benefitMoney(t.fine) : ' · 未触发负激励')
      + '）</td><td class="num">' + benefitMoney(t.paid) + '</td></tr>';
  });
  h += '</tbody></table>';
  box.innerHTML = h
    + '<div class="hint" style="margin-top:8px">前三按 50/30/20 分池；'
    + '该店<b>总达成率 ≥90%</b> 才发该名次；赛道连带达成率 &lt;50% 扣一笔 300。'
    + '未发放部分公司收回。</div>';
}

let benefitRequestController = null;
let benefitRequestEpoch = 0;
let benefitIsMounted = false;

function mountBenefit() {
  benefitIsMounted = true;
}

function unmountBenefit() {
  benefitIsMounted = false;
  benefitRequestEpoch += 1;
  if (benefitRequestController) benefitRequestController.abort();
  benefitRequestController = null;
}

async function loadBenefit() {
  if (!benefitIsMounted) return;
  winInit('benefit');                   // 日期窗口（默认今天；改了选择会重进这里）
  benefitLoading();
  if (benefitRequestController) benefitRequestController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  benefitRequestController = controller;
  const requestEpoch = ++benefitRequestEpoch;
  try {
    const result = await api('/api/benefit?end=' + encodeURIComponent(winEnd('benefit')),
      controller ? { signal: controller.signal } : {});
    if (!benefitIsMounted || requestEpoch !== benefitRequestEpoch) return;
    renderBenefit(result);
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (!benefitIsMounted || requestEpoch !== benefitRequestEpoch) return;
    renderBenefit({ ok: false, why: '读取无忧会员权益失败：' + e.message });
  } finally {
    if (benefitRequestController === controller) benefitRequestController = null;
  }
}

$$('#benefit-seg .seg-btn').forEach((b) => b.addEventListener('click', () => {
  $$('#benefit-seg .seg-btn').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  benefitState.view = b.dataset.bview;
  if (benefitState.data) renderBenefit(benefitState.data);
  else loadBenefit();
}));

$('#btn-refresh-benefit')?.addEventListener('click', (e) =>
  refreshPage('benefit', e.currentTarget));
$('#btn-export-benefit')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#benefit-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/benefit/export', { method: 'POST', body: { end: winEnd('benefit') } });
    const url = '/api/export/download?name=' + encodeURIComponent(r.file);
    triggerDownload(url, r.file);
    if (box) {
      box.hidden = false;
      box.innerHTML = '已下载 <b>' + esc(r.file) + '</b>（' + (r.rows || 0) + ' 行 · '
        + (r.sheets || []).length + ' 张表）—— 没弹出保存框的话，去浏览器<b>「下载」</b>里找　'
        + '<a class="btn ghost small" href="' + url + '">再下一次</a>'
        + '<span class="hint">（服务端也留了一份：<code>' + esc(r.rel || '') + '</code>）</span>';
    }
    toast('已导出，正在下载 ' + r.file, 'ok');
  } catch (err) {
    if (box) {
      box.hidden = false;
      box.innerHTML = '<span style="color:var(--bad)">' + esc(err.message) + '</span>';
    }
    toast('导出失败：' + err.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

function refreshBenefit(button) {
  return refreshWithFetch('benefit', button, () => loadBenefit());
}

registerPage('benefit', {
  mount: mountBenefit,
  load: loadBenefit,
  unmount: unmountBenefit,
  refresh: refreshBenefit,
});
