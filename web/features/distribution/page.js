'use strict';

/* 分销工作区：区域、机型、销售员和明细四个视角。 */
// ================================================================ 分销（2.3.0）
// 四个视角共用一个时间段 —— 存 localStorage（切换视角/刷新都还在）。
const DIST_ZONES = ['市南', '市北', '城阳', '胶州', '黄岛', '平度', '莱西', '即墨', '崂山'];
const DIST_PENDING = '待确认';

// ── 图表（2026-09-29 用户：「还需要饼状图、柱形图这些方便直观的图表」）
// ⚠ 无构建、无 CDN 是红线 ⇒ **手写**：环形图 = 内联 SVG，条形图 = CSS 宽度。
//   不引 echarts/chart.js（那要打包器或外网，门店断网就废了）。
const DIST_COLORS = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f',
  '#edc948', '#b07aa1', '#ff9da7', '#9c7559', '#bab0ac',
  '#5778a4', '#d37295'];

/** 环形（饼）图：`items` = [{label, value}]。按 |value| 定份额（负金额也算量），
 *  图例里显示原值。整圈只有一段时画个圆环 —— 单段弧起终点重合不会闭合。
 *  `subPrefix` = 圆心下半行的前缀（金额饼写「净额」、销量饼写「数量」）。 */
function distDonut(items, centerLabel, subPrefix) {
  const segs = (items || []).filter((x) => Math.abs(x.value) > 0);
  const total = segs.reduce((s, x) => s + Math.abs(x.value), 0);
  if (!total) return '<div class="empty">没有数据</div>';
  const size = 168, r = size / 2, inner = r * 0.6;
  let paths = '', angle = -Math.PI / 2;
  if (segs.length === 1) {
    paths = `<circle cx="${r}" cy="${r}" r="${(r + inner) / 2}" fill="none"`
      + ` stroke="${DIST_COLORS[0]}" stroke-width="${r - inner}"/>`;
  } else {
    paths = segs.map((x, i) => {
      const frac = Math.abs(x.value) / total;
      const a2 = angle + frac * 2 * Math.PI;
      const large = frac > 0.5 ? 1 : 0;
      const px = (rad, a) => (r + rad * Math.cos(a)).toFixed(2) + ','
        + (r + rad * Math.sin(a)).toFixed(2);
      const d = `M${px(r, angle)} A${r},${r} 0 ${large} 1 ${px(r, a2)}`
        + ` L${px(inner, a2)} A${inner},${inner} 0 ${large} 0 ${px(inner, angle)} Z`;
      angle = a2;
      const pct = (frac * 100).toFixed(1);
      return `<path d="${d}" fill="${DIST_COLORS[i % DIST_COLORS.length]}">`
        + `<title>${esc(x.label)} ${pct}%</title></path>`;
    }).join('');
  }
  const legend = segs.map((x, i) => {
    const frac = Math.abs(x.value) / total;
    return `<li><span class="dist-dot" style="background:${DIST_COLORS[i % DIST_COLORS.length]}"></span>`
      + `${esc(x.label)} <b>${distMoney(x.value)}</b>`
      + ` <span class="hint">${(frac * 100).toFixed(1)}%</span></li>`;
  }).join('');
  return `<div class="dist-donut-wrap">
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img"
         aria-label="${esc(centerLabel || '占比图')}">${paths}
      <text x="${r}" y="${r - 2}" text-anchor="middle" class="dist-donut-num">${esc(centerLabel || '')}</text>
      <text x="${r}" y="${r + 16}" text-anchor="middle" class="dist-donut-sub">${esc(subPrefix || '净额')} ${esc(distMoney(total))}</text>
    </svg>
    <ul class="dist-legend">${legend}</ul></div>`;
}

/** 横向条形图（单指标）：`items` = [{label, value}]，按 |value| 取前 `top` 档，
 *  剩下的并成「其他」（三级分类几十档，全画没人看）。 */
function distBars(items, opts = {}) {
  const top = opts.top || 12;
  let list = (items || []).slice().sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
  if (list.length > top) {
    const rest = list.slice(top);
    list = list.slice(0, top).concat([{
      label: `其他（${rest.length} 档）`,
      value: rest.reduce((s, x) => s + x.value, 0),
    }]);
  }
  if (!list.length) return '<div class="empty">没有数据</div>';
  const max = Math.max(...list.map((x) => Math.abs(x.value))) || 1;
  const rows = list.map((x, i) => {
    const w = Math.abs(x.value) / max * 100;
    const neg = x.value < 0;
    // ⚠ 负值**不给行内 background** —— 行内样式会盖掉 `.neg` 的红色，
    //   那"负金额标红"就白写了（负值本身就是异常信号，要看得见）。
    const bg = neg ? '' : `background:${DIST_COLORS[i % DIST_COLORS.length]}`;
    return `<div class="dist-bar-row">`
      + `<span class="dist-bar-label" title="${esc(x.label)}">${esc(x.label)}</span>`
      + `<span class="dist-bar-track">`
      + `<span class="dist-bar-fill${neg ? ' neg' : ''}" style="width:${w.toFixed(1)}%;${bg}"></span></span>`
      + `<span class="dist-bar-val${neg ? ' neg' : ''}">${esc(distMoney(x.value))}</span></div>`;
  }).join('');
  return `<div class="dist-bars">${rows}</div>`;
}

/** 销售额 / 销量 **两张单指标图并排**（2026-09-29 用户：「分开吧，一左一右，
 *  现在柱状图做在一起很怪」）。`items` = [{label, value, qty}] ——
 *  各自按自己的指标排序取 top（销量榜和金额榜的名次本来就可能不同）。 */
function distBarsDuo(items, opts = {}) {
  if (!(items || []).length) return '';
  const cap = (t, body) => `<div class="dist-bars-half">`
    + `<h4 class="dist-chart-cap">${t}</h4>${body}</div>`;
  return `<div class="dist-bars-pair">`
    + cap('按销售额', distBars(items.map((x) => ({ label: x.label, value: x.value })), opts))
    + cap('按销量', distBars(items.map((x) =>
        ({ label: x.label, value: Number(x.qty || 0) })), opts))
    + `</div>`;
}

function distRange() {
  const s = $('#dist-start'), e = $('#dist-end');
  return { start: s ? s.value : '', end: e ? e.value : '' };
}
function distQuery() {
  const r = distRange();
  const q = new URLSearchParams();
  if (r.start) q.set('start', r.start);
  if (r.end) q.set('end', r.end);
  return q.toString();
}
function distSaveRange() {
  try { localStorage.setItem('dist-range', JSON.stringify(distRange())); } catch (e) { /* 隐私模式 */ }
}
function distInitRange() {
  const s = $('#dist-start'), e = $('#dist-end');
  if (!s || !e) return;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('dist-range') || 'null'); } catch (err) { saved = null; }
  const today = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const endDefault = (saved && saved.end) || iso(today);
  const first = new Date(today.getFullYear(), today.getMonth(), 1);
  const startDefault = (saved && saved.start) || iso(first);
  s.value = startDefault; e.value = endDefault;
}
let activeDistView = 'region';
let distPageMounted = false;
let distPageEpoch = 0;
const distReadControllers = new Set();

async function distRead(path) {
  const epoch = distPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  if (controller) distReadControllers.add(controller);
  try {
    const result = await api(path, controller ? { signal: controller.signal } : {});
    return distPageMounted && epoch === distPageEpoch ? result : null;
  } catch (e) {
    if (!distPageMounted || epoch !== distPageEpoch || (e && e.name === 'AbortError')) return null;
    throw e;
  } finally {
    if (controller) distReadControllers.delete(controller);
  }
}

function setDistView(view, load) {
  if (!['region', 'model', 'salesman', 'detail'].includes(view)) return;
  activeDistView = view;
  $$('.dist-view-tab').forEach((b) => {
    const active = b.dataset.distView === view;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  $$('.dist-view').forEach((p) => {
    const active = p.id === 'dist-view-' + view;
    p.classList.toggle('active', active);
    p.hidden = !active;
  });
  if (load !== false) loadDistView(view);
}
function loadDistView(view) {
  if (!distPageMounted) return;
  if (view === 'detail') return loadDistDetail();
  return loadDistBoard(view);
}
function reloadDistView() {
  return loadDistView(activeDistView);
}
$$('.dist-view-tab').forEach((b) => b.addEventListener('click', () => {
  setDistView(b.dataset.distView);
}));
function distMoney(v) {
  return v == null ? '—' : Number(v).toLocaleString('zh-CN',
    { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}
function distNote(d) {
  const el = $('#dist-range-note');
  if (!el) return;
  const bits = [];
  if (d.range_note) bits.push(`<b>${esc(d.range_note)}</b>`);
  bits.push(`${esc(d.start)} ~ ${esc(d.end)} · ${esc(d.store || '')}`);
  if (d.fetched === false) {
    bits.push('<b>这段时间还没拉过</b> —— 点上面「拉取」从云商取数');
  } else if (d.covered) {
    bits.push(`已拉取（数据覆盖 ${esc(d.covered.from)} ~ ${esc(d.covered.to)}）`);
  }
  el.innerHTML = bits.join('　·　');
}

async function loadDistBoard(kind) {
  const boxId = { region: '#dist-region-board', model: '#dist-model-cat1',
                  salesman: '#dist-salesman-board' }[kind];
  const box = $(boxId);
  if (box) box.innerHTML = '加载中…';
  // 换区间 / 重画 ⇒ 上次下钻那批行作废（否则新窗口会点出旧窗口的单）
  distDrillRows.clear();
  try {
    const d = await distRead(`/api/dist/board?kind=${kind}&${distQuery()}`);
    if (!d) return;
    distNote(d);
    if (kind === 'region') renderDistRegion(d);
    else if (kind === 'model') renderDistModel(d);
    else renderDistSalesman(d);
  } catch (e) {
    if (!distPageMounted || (e && e.name === 'AbortError')) return;
    if (box) box.innerHTML = `<div class="empty">${esc('读取失败：' + e.message)}</div>`;
  }
}

function renderDistRegion(d) {
  const b = d.board || {};
  const meta = $('#dist-region-meta');
  const t = b.totals || { rows: 0, amount: 0, qty: 0 };
  if (meta) meta.textContent =
    `${t.rows} 行 · 净额 ${distMoney(t.amount)} · 销量 ${distMoney(t.qty)}`;
  const box = $('#dist-region-board');
  if (box) {
    // 列序跟图序一致：销售额（左饼）在前、销量（右饼）在后（用户 2026-09-29）
    box.innerHTML = table(['区域', '行数', '金额', '销量'],
      (b.zones || []).map((z) => [z.zone, z.rows, distMoney(z.amount), distMoney(z.qty)]));
  }
  // 两个饼并排：左=按销售额、右=按销量（2026-09-29 用户：销售额销量合到一起）
  const chart = $('#dist-region-chart');
  if (chart) {
    const zones = (b.zones || []).filter((z) => z.rows > 0);
    if (zones.length) {
      chart.innerHTML =
        `<div class="dist-donut-pair">`
        + `<div><h4 class="dist-chart-cap">按销售额</h4>${distDonut(
            zones.map((z) => ({ label: z.zone, value: z.amount })),
            '区域占比', '净额')}</div>`
        + `<div><h4 class="dist-chart-cap">按销量</h4>${distDonut(
            zones.map((z) => ({ label: z.zone, value: z.qty })),
            '区域占比', '数量')}</div>`
        + `</div>`;
    } else {
      chart.innerHTML = d.fetched === false
        ? '<div class="empty">这段时间还没拉过 —— 点上面「拉取」从云商取数</div>' : '';
    }
  }
  // 待确认队列（按客户）—— 下拉就在这儿，确认动作挨着队列
  const pend = b.pending || [];
  const pc = $('#dist-pending-count');
  if (pc) pc.textContent = pend.length ? `（${pend.length} 个客户）` : '（全部识别完了）';
  const pbox = $('#dist-region-pending');
  if (pbox) {
    if (!pend.length) {
      pbox.innerHTML = '<div class="empty">没有待确认的客户</div>';
    } else {
      const opts = (z) => DIST_ZONES.map((x) =>
        `<option value="${esc(x)}">${esc(x)}</option>`).join('');
      pbox.innerHTML = table(
        ['客户', '行数', '金额', '备注样例', '定为'],
        // ⚠ 行里的字符串**别自己 esc** —— `cell()` 会转义，双重转义会把
        //   客户名里的 `&` 显示成 `&amp;`（坑 3 的反面）。
        pend.map((p) => [p.customer, p.rows, distMoney(p.amount),
          p.sample_remark || '—',
          { html: `<select data-dist-customer="${esc(p.customer)}" aria-label="选区">`
              + `<option value="">（选一个区）</option>${opts()}</select>` }]));
      pbox.querySelectorAll('select[data-dist-customer]').forEach((sel) => {
        sel.addEventListener('change', async (ev) => {
          const cust = ev.target.dataset.distCustomer;
          const zone = ev.target.value;
          if (!zone) return;
          try {
            await api('/api/dist/zone', { method: 'POST',
              body: { customer: cust, zone } });
            toast(`已确认：${cust} → ${zone}`, 'ok');
            loadDistBoard('region');
          } catch (err) {
            toast('确认失败：' + err.message, 'bad');
          }
        });
      });
    }
  }
  // 已确认列表（可改：点区名下拉改 / 清除）
  const maps = b.mapping || [];
  const mc = $('#dist-map-count');
  if (mc) mc.textContent = maps.length ? `（${maps.length} 个）` : '（还没有）';
  const mbox = $('#dist-map-list');
  if (mbox) {
    if (!maps.length) mbox.innerHTML = '<div class="empty">还没有确认过任何客户</div>';
    else {
      mbox.innerHTML = table(['客户', '区域', '操作'], maps.map((m) => [
        m.customer, m.zone,
        { html: `<button class="btn ghost small" data-dist-clear="${esc(m.customer)}"`
            + ' title="清回待确认">清除</button>' }]));
      mbox.querySelectorAll('button[data-dist-clear]').forEach((btn) => {
        btn.addEventListener('click', async (ev) => {
          const cust = ev.target.dataset.distClear;
          try {
            await api('/api/dist/zone', { method: 'POST',
              body: { customer: cust, zone: '' } });
            toast(`已清除：${cust}`, 'ok');
            loadDistBoard('region');
          } catch (err) { toast('清除失败：' + err.message, 'bad'); }
        });
      });
    }
  }
}

function distCatTable(rows, field) {
  const list = rows || [];
  const total = list.reduce((s, r) => s + Number(r.amount || 0), 0);
  if (!list.length) return '<div class="empty">没有记录</div>';
  // ⚠ 列序 = **图序**（销售额在左、销量在右）—— 用户 2026-09-29：
  //   「饼状图销售额在左边，列表里销售额在右边，不能对齐吗」⇒ 表跟图走。
  // ⚠ 2026-09-30：**分类名可点** → 就地展开该档的销售单（用户：
  //   「每个机型点开是具体的销售单信息、金额、数量信息」）。
  // ⚠ 手拼、不再走 `table()`：要在行后插明细行，`table()` 一次拼完做不到。
  const head = ['分类', '行数', '金额', '数量', '占比'];
  let h = '<table><thead><tr>'
    + head.map((t) => `<th>${t}</th>`).join('') + '</tr></thead><tbody>';
  list.forEach((r) => {
    const pct = total ? (Number(r.amount || 0) / total) : 0;
    const key = distDrillKey(field, r.key);
    h += '<tr>'
      + `<td>${distDrillCell(key, { kind: 'rows', f: field, v: r.key, label: r.key })}</td>`
      + `<td>${esc(String(r.rows))}</td>`
      + `<td>${esc(distMoney(r.amount))}</td>`
      + `<td>${esc(distMoney(r.qty))}</td>`
      + `<td>${(pct * 100).toFixed(1)}%</td>`
      + '</tr>';
  });
  return h + '</tbody></table>';
}

/** 「未分类」的展开明细（商品级）—— 用户 2026-09-29：「剩下的未分类加个展开」。
 *  用原生 `<details>`：零 JS、零状态管理，展开态丢了也不影响别的。
 *  ⚠ 列表来自 `cat3_unclassified` —— `fix_cat3` 已在后端先跑过（Pura X View
 *  已挪进自己的档），这里剩的全是真·未分类（促销品等）。 */
function distUnclosedDetails(items) {
  const list = items || [];
  if (!list.length) return '';
  const rowsN = list.reduce((s, r) => s + Number(r.rows || 0), 0);
  const body = table(['商品', '行数', '金额', '数量'],
    list.map((r) => [r.key, r.rows, distMoney(r.amount), distMoney(r.qty)]));
  return `<details class="dist-unc"><summary>未分类里都是什么？`
    + `<span class="hint">（${rowsN} 行 / ${list.length} 种商品，点开看）</span></summary>`
    + body + `</details>`;
}

function renderDistModel(d) {
  const b = d.board || {};
  const meta = $('#dist-model-meta');
  const t = b.totals || { rows: 0, amount: 0, qty: 0 };
  if (meta) meta.textContent =
    `${t.rows} 行 · 净额 ${distMoney(t.amount)} · 销量 ${distMoney(t.qty)}`;
  const c1 = $('#dist-model-cat1'), c3 = $('#dist-model-cat3');
  // ⚠ field 点名 —— 下钻按**这一列**筛（后端 `DRILL_FIELDS` 白名单同名）
  if (c1) c1.innerHTML = distCatTable(b.cat1, '一级分类');
  // 三级分类表 + 「未分类」展开（点开看是些什么商品）
  if (c3) {
    c3.innerHTML = distCatTable(b.cat3, '三级分类')
      + distUnclosedDetails(b.cat3_unclassified);
  }
  // 条形图：销售额 / 销量 **两张并排**（一左一右），超出 top 档并成「其他」
  const g1 = $('#dist-model-cat1-chart'), g3 = $('#dist-model-cat3-chart');
  if (g1) g1.innerHTML = d.fetched === false ? '' : distBarsDuo(
    (b.cat1 || []).map((r) => ({ label: r.key, value: r.amount, qty: r.qty })),
    { top: 12 });
  if (g3) g3.innerHTML = d.fetched === false ? '' : distBarsDuo(
    (b.cat3 || []).map((r) => ({ label: r.key, value: r.amount, qty: r.qty })),
    { top: 12 });
}

function renderDistSalesman(d) {
  const b = d.board || {};
  const meta = $('#dist-salesman-meta');
  const t = b.totals || { rows: 0, amount: 0, qty: 0 };
  if (meta) meta.textContent =
    `${t.rows} 行 · 净额 ${distMoney(t.amount)} · 销量 ${distMoney(t.qty)}`;
  const chart = $('#dist-salesman-chart');
  const people = b.people || [];
  if (chart) {
    chart.innerHTML = (d.fetched !== false && people.length)
      ? distBarsDuo(people.slice(0, 12).map((p) =>
          ({ label: p.name, value: p.amount, qty: p.qty })), { top: 12 }) : '';
  }
  const box = $('#dist-salesman-board');
  if (!box) return;
  if (!people.length) {
    box.innerHTML = d.fetched === false
      ? '<div class="empty">这段时间还没拉过 —— 点上面「拉取」从云商取数</div>'
      : '<div class="empty">没有记录</div>';
    return;
  }
  // 每人一行，品类占比用 100% 堆叠条（行内拼 —— 不引图表库，红线：无构建步骤）
  const rows = people.map((p) => {
    const bar = (p.mix || []).filter((m) => m.pct > 0).map((m) =>
      `<span title="${esc(m.cat)} ${(m.pct * 100).toFixed(1)}%"`
      + ` style="display:inline-block;height:12px;border-radius:3px;`
      + `width:${Math.max(2, m.pct * 100)}%;background:hsl(${(m.cat.length * 47) % 360} 60% 55%)"></span>`
    ).join('');
    const mixTxt = (p.mix || []).slice(0, 4).map((m) =>
      `${esc(m.cat)} ${(m.pct * 100).toFixed(0)}%`).join('、');
    // ⚠ 2026-09-30：**人名可点** → 展开"品类折叠"，品类再点开是销售单
    //   （用户：「每个人名这一行点开是不同品类占比的折叠，点开品类名是
    //    具体各个品类的销售单信息、金额和数量」）。`kind: 'mix'` = 这一层
    //   展开成品类表；品类那层的 cell 由 `distMixTable` 生成（`kind: 'rows'`）。
    const key = distDrillKey('店员', p.name);
    // 列序跟图序一致：总销售额（左图）在前、数量（右图）在后（用户 2026-09-29）
    return [{ html: distDrillCell(key, { kind: 'mix', f: '店员', v: p.name, label: p.name }) },
      p.rows, distMoney(p.amount), distMoney(p.qty),
      { html: `<div style="min-width:160px" title="${esc(mixTxt)}">${bar}`
          + `<div class="hint" style="font-size:11px">${mixTxt}</div></div>` }];
  });
  box.innerHTML = table(['销售员（店员）', '行数', '总销售额', '数量', '品类占比'], rows);
}

/* ── 分销 · 行内下钻（2026-09-30）────────────────────────
   机型行点开 = 该档的销售单；销售员行点开 = **品类折叠**；品类名再点开 = 该品类的
   销售单（单号 / 时间 / 类型 / 商品 / 数量 / 金额 / 客户）—— 用户 2026-09-30。

   ⚠ **每个 base key 只请求一次**：品类那层是**本地聚合 + 本地筛**（父那份行
     已经在 `distDrillRows` 里），零请求；机型每档一次。
   ⚠ 鉴权走 `/api/dist/detail` 那道统一 403（仅平台岗）—— 前端不做权限判断，
     列名也由后端 `DRILL_FIELDS` 白名单把关（拼错会 400，不会静默给全表）。
   ⚠ 展开态**不跨重画**（切页 / 刷新就收起）；换时间段在 `loadDistBoard` 里
     清缓存 —— 否则旧区间那批行会被当成新区间点开的结果。 */
const distDrillRows = new Map();

function distDrillKey(...parts) {
  return parts.filter((x) => x != null && x !== '').join('|');
}

/** 可点的那格（`span` 撑满整格，见 CSS `.dist-drill-open`）。 */
function distDrillCell(key, o) {
  return `<span class="dist-drill-open" data-dist-key="${esc(key)}"`
    + ` data-dist-kind="${esc(o.kind || 'rows')}" data-dist-f="${esc(o.f || '')}"`
    + ` data-dist-v="${esc(o.v == null ? '' : String(o.v))}"`
    + (o.f2 ? ` data-dist-f2="${esc(o.f2)}" data-dist-v2="${esc(o.v2 == null ? '' : String(o.v2))}"` : '')
    + `>${esc(o.label == null ? '' : String(o.label))}</span>`;
}

/** 明细小表 —— 销售单本身。 */
function distDrillTable(rows) {
  const head = ['支付时间', '单号', '类型', '商品', '数量', '金额', '客户'];
  const body = (rows || []).map((r) => {
    const nq = Number(r['数量']) < 0, na = Number(r['金额']) < 0;
    return '<tr>'
      + `<td class="nowrap">${esc(r['支付时间'] || '')}</td>`
      + `<td class="mono">${esc(r['单号'] || '')}</td>`
      + `<td>${esc(r['单据类型'] || '')}</td>`
      + `<td>${esc(r['商品名称'] || '')}</td>`
      + `<td class="num${nq ? ' dist-drill-neg' : ''}">${esc(distMoney(r['数量']))}</td>`
      + `<td class="num${na ? ' dist-drill-neg' : ''}">${esc(distMoney(r['金额']))}</td>`
      + `<td>${esc(r['客户/顾客'] || '')}</td>`
      + '</tr>';
  }).join('');
  if (!body) return '<div class="empty">这一档没有销售单</div>';
  return `<table><thead><tr>${head.map((t) => `<th>${t}</th>`).join('')}</tr></thead>`
    + `<tbody>${body}</tbody></table>`;
}

/** 品类折叠 —— 把该店员的行按**一级分类**聚合成表（列跟机型表同构）。 */
function distMixTable(rows, parentKey, f, v) {
  const agg = new Map();
  (rows || []).forEach((r) => {
    const k = String(r['一级分类'] == null ? '' : r['一级分类']).trim() || '（空）';
    const a = agg.get(k) || { key: k, rows: 0, qty: 0, amount: 0, abs: 0 };
    a.rows += 1;
    a.qty += Number(r['数量']) || 0;
    a.amount += Number(r['金额']) || 0;
    a.abs += Math.abs(Number(r['金额']) || 0);
    agg.set(k, a);
  });
  const list = [...agg.values()].sort((x, y) => y.abs - x.abs);
  const sumAbs = list.reduce((s, a) => s + a.abs, 0) || 1;
  const head = ['分类', '行数', '金额', '数量', '占比'];
  const body = list.map((a) => {
    // 品类那层的 cell：**父的 f/v**（店员）+ 第二级（一级分类）—— 后端两个参数正好
    const cell = distDrillCell(distDrillKey(parentKey, '一级分类', a.key),
      { kind: 'rows', f, v, f2: '一级分类', v2: a.key, label: a.key });
    return '<tr>'
      + `<td>${cell}</td>`
      + `<td>${esc(String(a.rows))}</td>`
      + `<td>${esc(distMoney(a.amount))}</td>`
      + `<td>${esc(distMoney(a.qty))}</td>`
      // 占比按**绝对值**算 —— 跟看板那根堆叠条同一口径（退单是负金额）
      + `<td>${((a.abs / sumAbs) * 100).toFixed(1)}%</td>`
      + '</tr>';
  }).join('');
  if (!body) return '<div class="empty">这一档没有销售单</div>';
  return `<table><thead><tr>${head.map((t) => `<th>${t}</th>`).join('')}</tr></thead>`
    + `<tbody>${body}</tbody></table>`;
}

function distDrillStrip(key) {
  document.querySelectorAll('tr.dist-drill').forEach((tr) => {
    if (tr.getAttribute('data-parent') === key) tr.remove();
  });
}

async function distDrillToggle(el) {
  const key = el.getAttribute('data-dist-key') || '';
  if (!key || el.classList.contains('dist-loading')) return;
  const opened = [...document.querySelectorAll('tr.dist-drill')]
    .some((tr) => tr.getAttribute('data-parent') === key);
  if (opened) {                       // 再点一次 = 收起（连子孙一起没 —— 子在父 DOM 里）
    distDrillStrip(key);
    el.classList.remove('open');
    return;
  }
  const f = el.getAttribute('data-dist-f') || '';
  const v = el.getAttribute('data-dist-v') || '';
  const f2 = el.getAttribute('data-dist-f2') || '';
  const v2 = el.getAttribute('data-dist-v2') || '';
  const kind = el.getAttribute('data-dist-kind') || 'rows';
  const tr = el.closest('tr');
  if (!tr || !f) return;
  tr.insertAdjacentHTML('afterend',
    `<tr class="dist-drill" data-parent="${esc(key)}"><td colspan="5">`
    + '<span class="hint">读取中…</span></td></tr>');
  el.classList.add('open', 'dist-loading');
  try {
    const base = distDrillKey(f, v);
    let rows = distDrillRows.get(base);
    if (!rows) {
      const q = new URLSearchParams(distQuery());
      q.set('field', f);
      q.set('value', v);
      const d = await distRead(`/api/dist/detail?${q}`);
      if (!d) return;
      if (d && d.ok === false) throw new Error(d.why || d.error || '读取失败');
      rows = (d && d.rows) || [];
      distDrillRows.set(base, rows);       // 品类那层要用它本地筛
    }
    let html;
    if (kind === 'mix') {
      html = distMixTable(rows, key, f, v);
    } else {
      let hit = rows;
      if (f2) {
        hit = rows.filter((r) =>
          (String(r[f2] == null ? '' : r[f2]).trim() || '（空）') === v2);
      }
      html = distDrillTable(hit);
    }
    const ph = [...document.querySelectorAll('tr.dist-drill')]
      .find((x) => x.getAttribute('data-parent') === key);
    if (ph) ph.innerHTML = `<td colspan="5">${html}</td>`;
  } catch (e) {
    if (!distPageMounted || (e && e.name === 'AbortError')) return;
    // ⚠ 失败要出声 —— 静默收起 = 用户以为「点了没反应」（这个项目最怕的失败）
    distDrillStrip(key);
    el.classList.remove('open');
    toast('下钻读取失败：' + e.message, 'bad');
  } finally {
    el.classList.remove('dist-loading');
  }
}

// 可点那格的委托 —— document 级（表每次 innerHTML 重画，同 film / 增值那两处）
document.addEventListener('click', (e) => {
  if (!distPageMounted) return;
  const el = e.target && e.target.closest && e.target.closest('[data-dist-key]');
  if (!el) return;
  distDrillToggle(el);
});

async function loadDistDetail() {
  const box = $('#dist-detail-table');
  if (box) box.innerHTML = '加载中…';
  try {
    const d = await distRead(`/api/dist/detail?${distQuery()}`);
    if (!d) return;
    distNote(d);
    const meta = $('#dist-detail-meta');
    if (meta) meta.textContent = `${d.total} 行${d.truncated ? '（只显示前 ' + d.rows.length + ' 行）' : ''}`;
    // 区域筛选下拉：全部 + 九区 + 待确认（只在有值时保留当前选中）
    const sel = $('#dist-zone-filter');
    if (sel) {
      const cur = sel.value;
      const zones = DIST_ZONES.concat([DIST_PENDING]);
      sel.innerHTML = '<option value="">全部区域</option>'
        + zones.map((z) => `<option value="${esc(z)}">${esc(z)}</option>`).join('');
      sel.value = cur;
      sel.onchange = () => loadDistDetail();
    }
    const zone = sel ? sel.value : '';
    let rows = d.rows || [];
    if (zone) rows = rows.filter((r) => r._zone === zone);
    // 待确认提示
    const pend = rows.filter((r) => r._zone === DIST_PENDING);
    const ph = $('#dist-pending-hint');
    if (ph) {
      ph.innerHTML = pend.length
        ? `<b>${pend.length} 行待确认</b>（客户名还没定区）—— 到「区域看板」按客户确认一次，这里以后就自动有区了。`
        : '全部行都已识别出区域。';
    }
    if (!rows.length) {
      if (box) box.innerHTML = d.fetched === false
        ? '<div class="empty">这段时间还没拉过 —— 点上面「拉取」从云商取数</div>'
        : '<div class="empty">没有记录</div>';
      return;
    }
    if (box) {
      // ⚠ 行内字符串不自己 esc（`cell()` 转义）；只有 `{html:…}` 那格自己负责
      box.innerHTML = table(
        ['区域', '支付时间', '单号', '类型', '商品', '一级', '三级', '数量', '金额', '店员', '客户', '备注'],
        rows.map((r) => [
          { html: `<b>${esc(r._zone)}</b>` },
          r['支付时间'] || '', r['单号'] || '', r['单据类型'] || '',
          r['商品名称'] || '', r['一级分类'] || '', r['三级分类'] || '',
          distMoney(r['数量']), distMoney(r['金额']),
          r['店员'] || '', r['客户/顾客'] || '', r['备注'] || '']));
    }
  } catch (e) {
    if (!distPageMounted || (e && e.name === 'AbortError')) return;
    if (box) box.innerHTML = `<div class="empty">${esc('读取失败：' + e.message)}</div>`;
  }
}

// 工具条：应用（重读当前页）/ 拉取（长任务）/ 导出
$('#btn-dist-apply')?.addEventListener('click', () => {
  distSaveRange();
  reloadDistView();
});
$('#btn-dist-fetch')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const r = distRange();
  if (!r.start || !r.end) { toast('先选时间段', 'bad'); return; }
  btn.disabled = true;
  $('#dist-status').textContent = '拉取中…（30 天约半分钟，别关页面）';
  try {
    const res = await api('/api/dist/fetch', { method: 'POST',
      body: { start: r.start, end: r.end } });
    toast(`拉取完成：${res.rows} 行分销单`, 'ok');
    distSaveRange();
    reloadDistView();
  } catch (err) {
    toast('拉取失败：' + err.message, 'bad');
  } finally {
    btn.disabled = false;
    $('#dist-status').textContent = '';
  }
});
$('#btn-dist-export')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const r = distRange();
    const res = await api('/api/dist/export', { method: 'POST',
      body: { start: r.start, end: r.end } });
    if (res.ok) {
      const el = $('#dist-range-note');
      toast('已导出：' + (res.rel || res.file || ''), 'ok');
      if (el) el.innerHTML += `　·　<a href="/api/export/download?name=`
        + `${encodeURIComponent(res.file || '')}">下载 ${esc(res.file || '')}</a>`;
    }
  } catch (err) {
    toast('导出失败：' + err.message, 'bad');
  } finally { btn.disabled = false; }
});
$('#btn-dist-map-reset')?.addEventListener('click', async () => {
  if (!window.confirm('清空全部客户→区的映射？所有客户会回到「待确认」。')) return;
  try {
    const res = await api('/api/dist/map/reset', { method: 'POST' });
    toast(`已重置 ${res.removed} 条映射`, 'ok');
    loadDistBoard('region');
  } catch (err) { toast('重置失败：' + err.message, 'bad'); }
});
['#btn-refresh-dist-region', '#btn-refresh-dist-model', '#btn-refresh-dist-salesman']
  .forEach((sel) => $(sel)?.addEventListener('click', () => {
    distSaveRange();
    loadDistBoard(sel.replace('#btn-refresh-dist-', ''));
  }));
$('#btn-refresh-dist-detail')?.addEventListener('click', () => {
  distSaveRange(); loadDistDetail();
});
// 时间段改了存一份（四页共用）
['#dist-start', '#dist-end'].forEach((sel) => $(sel)?.addEventListener('change', distSaveRange));
function mountDistPage() {
  distPageMounted = true;
  distInitRange();
}

function unmountDistPage() {
  distPageMounted = false;
  distPageEpoch += 1;
  distReadControllers.forEach((controller) => controller.abort());
  distReadControllers.clear();
  distDrillRows.clear();
}

registerPage('dist-region', {
  mount: mountDistPage,
  unmount: unmountDistPage,
  load: () => loadDistView(activeDistView),
  refresh: reloadDistView,
});
