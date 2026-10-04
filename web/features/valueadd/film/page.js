'use strict';

/* 防护膜达成页：渲染、读取、行展开、刷新和导出。 */

/* ═══════════ 增值 · 防护膜达成情况（2026-09-22）═══════════
   只读 `/api/film`（后端本地库现算）。 */
function filmPct(x, digits) {
  if (x == null || !isFinite(x)) return '—';
  // ⚠ 跟机率 / 毛利达成率 / 总达成率 = 2 位小数；礼包达成率 = 整数 %（源表截图）
  return (x * 100).toFixed(digits == null ? 2 : digits) + '%';
}

function filmFmt(n) {
  if (n == null || !isFinite(n)) return '—';
  // ⚠ 台数 / 金额 / 利润 —— 源表截图是**整数**（四舍五入），不带小数
  return String(Math.round(n));
}

/** 率类标色 —— **每列规则不同**（2026-09-22 用户按源表截图定的）：
 *
 *  | 列 | 规则 |
 *  |---|---|
 *  | 跟机率（目标 35%） | <35% 粉；≥35% 白（**不标蓝**，104% 也是白） |
 *  | 毛利达成率 | <60% 粉 · 60%~90% 白 · **≥90% 蓝**（和别的率不一样） |
 *  | 总达成率 / 礼包达成率 | <50% 粉 · 50%~100% 白 · ≥100% 蓝 |
 */
function filmRateCls(v, kind) {
  if (v == null || !isFinite(v)) return '';
  if (kind === 'attach') return v < 0.35 ? 'film-lo' : '';
  if (kind === 'profit') {
    if (v < 0.6) return 'film-lo';
    if (v >= 0.9) return 'film-hi';
    return '';
  }
  if (v < 0.5) return 'film-lo';
  if (v >= 1) return 'film-hi';
  return '';
}

/** 台均增值利润：跟**本店台均基线**比 —— <60% 红 · 60%~80% 白 · ≥80% 蓝。 */
function filmAddonCls(v, baseline) {
  const b = Number(baseline) || 0;
  if (!b || v == null || !isFinite(v)) return '';
  const r = v / b;
  if (r < 0.6) return 'film-lo';
  if (r >= 0.8) return 'film-hi';
  return '';
}

function filmLoading() {
  const box = $('#film-table');
  if (box) {
    box.innerHTML = '<div class="film-loading"><span class="film-spin"></span>'
      + '<span>正在计算防护膜达成…</span></div>';
  }
  const meta = $('#film-meta');
  if (meta) meta.textContent = '';
}

//: 点门店名拆到人 —— 展开态**跨重画保留**（跟 plan 的 `openStores` 同理）。
//: 人那几行每次 `renderFilm` 按 `isOpen` 直接画进 DOM（防护膜表简单，不做插行动画）。
const filmOpenStores = new Set();
let filmData = null;

function renderFilm(d) {
  filmData = d;
  const meta = $('#film-meta'), box = $('#film-table'), warn = $('#film-warn');
  const legend = $('#film-legend'), title = $('#film-title');
  if (!box) return;
  if (!d || d.ok === false) {
    if (title) title.innerHTML = '防护膜达成情况 <span class="hint" id="film-meta"></span>';
    if (warn) warn.innerHTML = d && d.why
      ? '<div class="empty">' + esc(d.why) + '</div>' : '';
    box.innerHTML = '';
    if (legend) legend.textContent = '';
    return;
  }
  // 标题跟源表：`9月防护膜数据达成-截止到21日` + 右侧时间进度
  const start = String(d.start || ''), end = String(d.end || '');
  let mon = '', day = '';
  try {
    mon = start ? String(parseInt(start.slice(5, 7), 10)) : '';
    day = end ? String(parseInt(end.slice(8, 10), 10)) : '';
  } catch (e) { /* ignore */ }
  const prog = d.progress != null ? (d.progress * 100).toFixed(1) + '%' : '';
  // 数据截止至几号几点 —— 取导出里最大的支付时间（跟 plan.data_as_of 同理，
  // 但带**时分**；抓数断了要写停在哪一刻，别写"今天"）。
  const asOf = d.data_as_of || '';
  let asOfTxt = '';
  if (asOf) {
    const mm = String(parseInt(asOf.slice(5, 7), 10));
    const dd = String(parseInt(asOf.slice(8, 10), 10));
    const hm = asOf.length >= 16 ? asOf.slice(11, 16) : '';
    asOfTxt = '数据截止至 ' + mm + '月' + dd + '日' + (hm ? ' ' + hm : '');
  }
  if (title) {
    title.innerHTML = esc((mon ? mon + '月' : '') + '防护膜数据达成'
      + (day ? '-截止到' + day + '日' : ''))
      + ' <span class="hint" id="film-meta"></span>'
      + (asOfTxt ? ' <span class="hint">　' + esc(asOfTxt) + '</span>' : '')
      + (prog ? ' <span class="hint">　时间进度 ' + esc(prog) + '</span>' : '');
  }
  const meta2 = $('#film-meta');
  if (meta2) meta2.textContent = d.scope ? '· ' + d.scope : '';
  if (warn) warn.innerHTML = d.error ? '<div class="empty">' + esc(d.error) + '</div>' : '';

  // 按源表列序；区域合并单元格 + 每区「共计」+ 底「合计」
  // ⚠ 表头**两行**（2026-09-22 用户：「首行可以两行显示，现在有点拥挤」）——
  //   长名按源表断开（`防护膜单张\n平均利润`），短名保持一行。
  const head = ['区域', '门店', '新机<br>销售', '目标', '达成', '防护膜单张<br>平均利润',
                '跟机率<br>35%', '毛利<br>目标', '台均利润<br>基线', '零售毛利<br>达成',
                '礼包毛利<br>达成', '总毛利<br>达成', '毛利<br>达成率', '总<br>达成率',
                '礼包<br>套餐', '达成', '礼包<br>达成率', '台均增值<br>利润'];
  const rows = d.rows || [];
  const by = {};
  const order = [];
  rows.forEach(function (r) {
    const g = r.region || '其他';
    if (!by[g]) { by[g] = []; order.push(g); }
    by[g].push(r);
  });
  // 源表区序（我们名单：西北区 / 市区 / 南区）
  const pref = ['西北区', '市区', '南区', '服务站'];
  order.sort(function (a, b) {
    const ia = pref.indexOf(a), ib = pref.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
  });

  function cellNum(v, cls) {
    return '<td class="num' + (cls ? ' ' + cls : '') + '">' + esc(filmFmt(v)) + '</td>';
  }
  function cellRate(v, kind) {
    const dig = (kind === 'gift') ? 0 : 2;
    const k = (kind === 'gift') ? 'rate' : kind;
    return '<td class="num ' + filmRateCls(v, k) + '">' + filmPct(v, dig) + '</td>';
  }
  function bodyTr(r, regionHtml, kind) {
    // kind: '' 店行 | 'person' 人行（缩进）| 'sum' 区共计（红底）| 'total' 总合计
    const cls = kind === 'sum' ? 'film-sum'
      : (kind === 'total' ? 'film-total'
        : (kind === 'person' ? 'film-person' : ''));
    // 门店名可点（拆到人）—— ⚠ 属性直接拼在这一格上，别用 replace 补（坑 12）
    const ppl = kind === '' ? (r.people || []) : [];
    const isOpen = filmOpenStores.has(r.store);
    const storeAttr = ppl.length
      ? ' data-film-store="' + esc(encodeURIComponent(r.store)) + '"'
        + ' class="film-store-open"'
        + ' title="点一下' + (isOpen ? '收起' : '拆到人') + '（' + ppl.length + ' 人）"'
      : '';
    const caret = ppl.length
      ? '<span class="plan-caret">' + (isOpen ? '▾' : '▸') + '</span>' : '';
    // 人行：门店格显示人名（缩进）；区域格空
    const storeHtml = kind === 'person'
      ? '<span class="plan-who">' + esc(r.name || '') + '</span>'
      : esc(r.store || '') + caret;
    // ⚠ 只有**门店行**上那几个数字可下钻（区域共计 / 总合计 / 人行不是"某家店"）
    //   —— 同一套 `drillCell`，只换 `kind`；格子形状跟 `cellNum` 一模一样（同列数）
    function dnum(mk, v) {
      return kind === '' ? drillCell('film', mk, r.store, filmFmt(v)) : cellNum(v);
    }
    return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + regionHtml
      + '<td' + storeAttr + '>' + storeHtml + '</td>'
      + dnum('new', r.new) + cellNum(r.target) + dnum('film', r.done)
      + cellNum(r.unit_profit)
      + cellRate(r.attach, 'attach')
      + cellNum(r.profit_target) + cellNum(r.baseline)
      + dnum('film_profit', r.film_profit) + dnum('gift_profit', r.gift_profit)
      + dnum('total_profit', r.total_profit)
      + cellRate(r.profit_rate, 'profit')
      + cellRate(r.total_rate, 'rate')
      + cellNum(r.gift_pkg) + dnum('gift', r.gift_done)
      + cellRate(r.gift_rate, 'gift')
      + '<td class="num ' + filmAddonCls(r.avg_addon, r.baseline) + '">'
      + esc(filmFmt(r.avg_addon)) + '</td>'
      + '</tr>';
  }

  // ⚠ 别再套 `max-height:70vh` —— 跟无忧会员权益同一坑（2026-09-23）：
  //   表在框里滚、卡片被压矮，框下面像给右下角状态球留了一大块空。
  //   跟「周度目标达成」一致：横向 `.table-scroll`，纵向自然撑高、整页滚。
  let h = '<div class="table-scroll"><table class="film-table">'
    + '<thead><tr>'
    + head.map(function (t) { return '<th>' + t + '</th>'; }).join('')
    + '</tr></thead><tbody>';
  order.forEach(function (g) {
    const grp = by[g];
    // ⚠ rowspan 要算上**展开出来的人行**（插在店行之间，只数店会错位）
    const totalRows = grp.reduce(function (a, r) {
      return a + 1 + (filmOpenStores.has(r.store) ? (r.people || []).length : 0);
    }, 0);
    grp.forEach(function (r, i) {
      const reg = i === 0
        ? '<td class="region" rowspan="' + totalRows + '">' + esc(g) + '</td>'
        : '';
      h += bodyTr(r, reg, '');
      // 点开的店：紧跟着画人行（区域格**不画** —— 已被上面 rowspan 盖住）
      if (filmOpenStores.has(r.store)) {
        (r.people || []).forEach(function (pp) {
          h += bodyTr(pp, '', 'person');
        });
      }
    });
    // 该区共计 —— 用 metric 同口径在前端合计（summary 是全量的）
    const sub = { store: '共计：' };
    ['new', 'target', 'done', 'profit_target', 'film_profit', 'gift_profit',
     'total_profit', 'gift_pkg', 'gift_done'].forEach(function (k) {
      sub[k] = grp.reduce(function (a, r) { return a + (Number(r[k]) || 0); }, 0);
    });
    sub.unit_profit = sub.done ? sub.film_profit / sub.done : 0;
    sub.attach = sub.new ? sub.done / sub.new : 0;
    sub.avg_addon = sub.new ? sub.total_profit / sub.new : 0;
    sub.profit_rate = sub.profit_target ? sub.total_profit / sub.profit_target : 0;
    sub.gift_rate = sub.gift_pkg ? sub.gift_done / sub.gift_pkg : 0;
    sub.total_rate = sub.profit_rate * 0.5 + sub.gift_rate * 0.5;
    sub.baseline = sub.new ? sub.profit_target / sub.new : 0;
    h += bodyTr(sub, '<td class="region"></td>', 'sum');
  });
  // ⚠ 总合计**白底**（2026-09-22 用户）—— 红底会跟上面各区「共计」分不开
  if (d.summary) h += bodyTr(d.summary, '<td class="region"></td>', 'total');
  h += '</tbody></table></div>';
  box.innerHTML = h;
  if (legend) {
    legend.textContent = '新机已按 ×0.9 折算；跟机率目标 35%；'
      + '总达成率 = 毛利达成率×50% + 礼包达成率×50%'
      + (d.note ? '。' + d.note : '');
  }
}

let filmRequestController = null;
let filmRequestEpoch = 0;
let filmIsMounted = false;

function mountFilm() {
  filmIsMounted = true;
}

function unmountFilm() {
  filmIsMounted = false;
  filmRequestEpoch += 1;
  if (filmRequestController) filmRequestController.abort();
  filmRequestController = null;
}

async function loadFilm() {
  if (!filmIsMounted) return;
  winInit('film');                      // 日期窗口（默认今天；改了选择会重进这里）
  filmLoading();
  if (filmRequestController) filmRequestController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  filmRequestController = controller;
  const requestEpoch = ++filmRequestEpoch;
  try {
    const result = await api('/api/film?end=' + encodeURIComponent(winEnd('film')),
      controller ? { signal: controller.signal } : {});
    if (!filmIsMounted || requestEpoch !== filmRequestEpoch) return;
    renderFilm(result);
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (!filmIsMounted || requestEpoch !== filmRequestEpoch) return;
    renderFilm({ ok: false, why: '读取防护膜达成失败：' + e.message });
  } finally {
    if (filmRequestController === controller) filmRequestController = null;
  }
}

// 点**门店名** = 拆到人 / 收起（document 级委托 —— 表每次 innerHTML 重画，
// 绑在 #film-table 上理论上也行，但绑 document 避免「元素还没挂上 / 被换掉」这类哑火）
document.addEventListener('click', (e) => {
  const td = e.target && e.target.closest && e.target.closest('[data-film-store]');
  if (!td) return;
  const store = decodeURIComponent(td.getAttribute('data-film-store') || '');
  if (!store) return;
  if (filmOpenStores.has(store)) filmOpenStores.delete(store);
  else filmOpenStores.add(store);
  if (filmData && filmData.ok !== false) renderFilm(filmData);
});

function refreshFilm(button) {
  return refreshWithFetch('film', button, () => loadFilm());
}

registerPage('film', {
  mount: mountFilm,
  load: loadFilm,
  unmount: unmountFilm,
  refresh: refreshFilm,
});

// 刷新 = **先抓云商销售导出、再算一遍**（跟月度计划同一条路，`REFRESH_STEPS.film`）。
// ⚠ 不自动跑（`whens=()`）—— 门店手动拉就行（2026-09-22 用户）。
$('#btn-refresh-film')?.addEventListener('click', (e) =>
  refreshPage('film', e.currentTarget));

$('#btn-export-film')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#film-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/film/export', { method: 'POST', body: { end: winEnd('film') } });
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
