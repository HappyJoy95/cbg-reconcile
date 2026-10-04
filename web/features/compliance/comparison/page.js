'use strict';

/* 报量查询历史：仅在进入页面时读取，离页取消请求并释放刷新监听。 */
let comparisonPageMounted = false;
let comparisonHistoryController = null;
let comparisonDetailController = null;
let comparisonHistoryGeneration = 0;
let comparisonDetailGeneration = 0;
let comparisonRefreshTimer = null;
let comparisonRefreshHandler = null;
let comparisonCloseHandler = null;
let comparisonListHandler = null;
let comparisonSheetHandler = null;
const comparisonState = { report: null, sheet: null };

function mountComparisonPage() {
  comparisonPageMounted = true;
  const refresh = $('#btn-refresh-reports');
  const close = $('#btn-close-detail');
  const list = $('#report-list');
  const tabs = $('#sheet-tabs');
  if (refresh && !comparisonRefreshHandler) {
    comparisonRefreshHandler = (event) => refreshWithFetch('pools', event.currentTarget,
      async () => {
        await loadOverview();
        await loadPoolsHistory();
      });
    refresh.addEventListener('click', comparisonRefreshHandler);
  }
  if (close && !comparisonCloseHandler) {
    comparisonCloseHandler = () => { $('#report-detail-card').hidden = true; };
    close.addEventListener('click', comparisonCloseHandler);
  }
  if (list && !comparisonListHandler) {
    comparisonListHandler = (event) => {
      const button = event.target.closest('[data-day]');
      if (button && list.contains(button)) openPoolsDay(button.dataset.day);
    };
    list.addEventListener('click', comparisonListHandler);
  }
  if (tabs && !comparisonSheetHandler) {
    comparisonSheetHandler = (event) => {
      const button = event.target.closest('[data-sheet]');
      if (!button || !tabs.contains(button) || !comparisonState.report) return;
      comparisonState.sheet = button.dataset.sheet;
      renderSheetTabs(Object.keys(comparisonState.report.sheets || {}));
      renderSheet();
    };
    tabs.addEventListener('click', comparisonSheetHandler);
  }
  if (!comparisonRefreshTimer) {
    comparisonRefreshTimer = setInterval(() => loadPoolsHistory(), 30000);
  }
}

function unmountComparisonPage() {
  comparisonPageMounted = false;
  comparisonHistoryGeneration += 1;
  comparisonDetailGeneration += 1;
  if (comparisonHistoryController) comparisonHistoryController.abort();
  if (comparisonDetailController) comparisonDetailController.abort();
  comparisonHistoryController = null;
  comparisonDetailController = null;
  if (comparisonRefreshTimer) clearInterval(comparisonRefreshTimer);
  comparisonRefreshTimer = null;
  const refresh = $('#btn-refresh-reports');
  const close = $('#btn-close-detail');
  const list = $('#report-list');
  const tabs = $('#sheet-tabs');
  if (refresh && comparisonRefreshHandler) refresh.removeEventListener('click', comparisonRefreshHandler);
  if (close && comparisonCloseHandler) close.removeEventListener('click', comparisonCloseHandler);
  if (list && comparisonListHandler) list.removeEventListener('click', comparisonListHandler);
  if (tabs && comparisonSheetHandler) tabs.removeEventListener('click', comparisonSheetHandler);
  comparisonRefreshHandler = null;
  comparisonCloseHandler = null;
  comparisonListHandler = null;
  comparisonSheetHandler = null;
}

async function loadPoolsHistory() {
  if (!comparisonPageMounted) return;
  if (comparisonHistoryController) comparisonHistoryController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  comparisonHistoryController = controller;
  const generation = ++comparisonHistoryGeneration;
  let data = { days: [], years: [] };
  try {
    data = await api('/api/pools/history', controller ? { signal: controller.signal } : {});
  } catch (error) {
    if (!comparisonPageIsCurrent(generation, controller, 'history')) return;
    // 读不到就当没有 —— 历史看不了不该把整个控制台弄挂。
  } finally {
    if (comparisonHistoryController === controller) comparisonHistoryController = null;
  }
  if (!comparisonPageIsCurrent(generation, controller, 'history')) return;
  renderPoolsHistory(data);
}

function comparisonPageIsCurrent(generation, controller, kind) {
  return comparisonPageMounted
    && generation === (kind === 'history'
      ? comparisonHistoryGeneration : comparisonDetailGeneration)
    && !(controller && controller.signal.aborted);
}

function renderPoolsHistory(data) {
  const list = data.days || [];
  const latest = list[0];
  // ⚠ 空状态不能再叫人去点已删除的「跑一次」卡；可等每天定时任务或点本页刷新。
  $('#report-cards').innerHTML = latest ? `
    <div class="kpi bad"><div class="k">最新 · AD 玲珑报了、云商没报</div><div class="v">${latest.AD ?? '—'}</div></div>
    <div class="kpi bad"><div class="k">最新 · BC 云商报了、玲珑没报</div><div class="v">${latest.BC ?? '—'}</div></div>
    <div class="kpi ok"><div class="k">AC 都卖了</div><div class="v">${latest.AC ?? '—'}</div></div>
    <div class="kpi ok"><div class="k">BD 都没卖</div><div class="v">${latest.BD ?? '—'}</div></div>
    <div class="kpi"><div class="k">记录日期</div><div class="v" style="font-size:17px">${esc(latest.date || '—')}</div></div>
  ` : '<div class="kpi"><div class="k">还没有报量查询记录</div>'
      + '<div class="v" style="font-size:15px">每天 21:00 自动跑一趟，跑完就有了</div></div>';

  $('#report-count').textContent = list.length ? `共 ${list.length} 天` : '';
  if (!list.length) {
    $('#report-list').innerHTML = '<div class="empty">还没有记录 —— '
      + '每天 21:00 那趟跑完就有了（急着看就点上面的「刷新」）</div>';
    return;
  }
  const hot = (n) => (n ? { html: `<b style="color:var(--hot)">${n}</b>` } : 0);
  const rows = list.map((row) => [
    row.date, hot(row.AD), hot(row.BC), row.AC, row.BD,
    { html: `<button class="btn small" data-day="${esc(row.date)}">查看</button>` },
  ]);
  $('#report-list').innerHTML = table(
    ['日期', 'AD 玲珑报了云商没报', 'BC 云商报了玲珑没报', 'AC 都卖了', 'BD 都没卖', ''],
    rows, ['mono', 'num', 'num', 'num', 'num', '']);
}

async function openPoolsDay(day) {
  if (!comparisonPageMounted) return;
  if (comparisonDetailController) comparisonDetailController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  comparisonDetailController = controller;
  const generation = ++comparisonDetailGeneration;
  try {
    const data = await api('/api/pools/history?date=' + encodeURIComponent(day),
      controller ? { signal: controller.signal } : {});
    if (!comparisonPageIsCurrent(generation, controller, 'detail')) return;
    $('#report-detail-title').textContent = `${day} · 报量查询`;
    let html = '';
    for (const [key, label] of [['AD', 'AD · 玲珑报了、云商没报（云商该出库没出）'],
                                ['BC', 'BC · 云商报了、玲珑没报（门店该报量没报）']]) {
      const rows = data[key] || [];
      html += `<h3 style="margin:14px 0 6px">${label} —— ${rows.length} 台</h3>`;
      if (!rows.length) { html += '<div class="empty">没有</div>'; continue; }
      html += table(
        ['串号', '机型', '门店', '单号', '时间', '金额'],
        rows.map((row) => [
          row.sn,
          row['玲珑机型'] || row['云商机型'] || '',
          row['玲珑门店'] || row['云商门店'] || row['玲珑仓'] || row['云商仓'] || '',
          row['云商单号'] || row['玲珑单号'] || '',
          String(row['云商时间'] || row['玲珑时间'] || '').slice(0, 16),
          row['云商金额'] ?? row['玲珑金额'] ?? '',
        ]), ['mono', '', '', 'mono', 'mono', 'num']);
    }
    $('#sheet-tabs').innerHTML = '';
    $('#sheet-body').innerHTML = html;
    $('#report-detail-card').hidden = false;
  } catch (error) {
    if (!comparisonPageIsCurrent(generation, controller, 'detail')) return;
    toast('读取报量查询明细失败：' + error.message, 'bad');
  } finally {
    if (comparisonDetailController === controller) comparisonDetailController = null;
  }
}

async function removeReport(name) {
  if (!confirm(`删除这份报告？\n\n${name}\n\n删了就找不回来了（xlsx 和摘要一起删）。`)) return;
  try {
    const result = await api('/api/report?name=' + encodeURIComponent(name), { method: 'DELETE' });
    toast(result.message || '已删除', 'ok');
    if (comparisonState.report && comparisonState.report.name === name) {
      $('#report-detail-card').hidden = true;
    }
    await loadOverview();
    await loadPoolsHistory();
  } catch (error) {
    toast('删除失败：' + error.message, 'bad');
  }
}

async function openReport(name) {
  if (comparisonDetailController) comparisonDetailController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  comparisonDetailController = controller;
  const generation = ++comparisonDetailGeneration;
  try {
    comparisonState.report = await api('/api/report?name=' + encodeURIComponent(name),
      controller ? { signal: controller.signal } : {});
  } catch (error) {
    if (comparisonPageIsCurrent(generation, controller, 'detail')) {
      toast('读报告失败：' + error.message, 'bad');
    }
    return;
  }
  if (!comparisonPageIsCurrent(generation, controller, 'detail')) return;
  const names = Object.keys(comparisonState.report.sheets || {});
  comparisonState.sheet = names[0];
  $('#report-detail-card').hidden = false;
  $('#report-detail-title').textContent = name;
  $('#btn-download').href = '/api/report/download?name=' + encodeURIComponent(name);
  renderSheetTabs(names);
  renderSheet();
  $('#report-detail-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderSheetTabs(names) {
  $('#sheet-tabs').innerHTML = names.map((name) => {
    const rows = (comparisonState.report.sheets[name] || []).length - 1;
    return `<button class="subtab ${name === comparisonState.sheet ? 'active' : ''}" data-sheet="${esc(name)}">
      ${esc(name)} <span class="hint">(${rows})</span></button>`;
  }).join('');
}

function renderSheet() {
  const rows = comparisonState.report.sheets[comparisonState.sheet] || [];
  $('#sheet-body').innerHTML = table(rows[0] || [], rows.slice(1));
}

registerPage('pools', {
  mount: mountComparisonPage,
  unmount: unmountComparisonPage,
  load: loadPoolsHistory,
  refresh: loadPoolsHistory,
});
