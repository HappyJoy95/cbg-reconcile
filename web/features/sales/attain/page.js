'use strict';

/* 周度目标达成页面：当前周、只读历史、门店拆分、悬浮销售明细、刷新与导出。 */
let attainActivePage = '';
let attainRequestController = null;
let attainRequestEpoch = 0;

function attainBeginRequest(page) {
  if (attainRequestController) attainRequestController.abort();
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  attainRequestController = controller;
  return { page: page, controller: controller, epoch: ++attainRequestEpoch };
}

function attainRequestIsCurrent(request) {
  return attainActivePage === request.page && request.epoch === attainRequestEpoch;
}

function attainFinishRequest(request) {
  if (attainRequestController === request.controller) attainRequestController = null;
}

function mountAttainPage(page) {
  attainActivePage = page;
}

function unmountAttainPage() {
  attainActivePage = '';
  attainRequestEpoch += 1;
  if (attainRequestController) attainRequestController.abort();
  attainRequestController = null;
  clearTimeout(attainHideTimer);
  clearTimeout(attainShowTimer);
  forceHideAttainPop();
}

/* ────────────────────── 周度目标达成情况 ──────────────────────

   ⚠ 2026-09-19：**接口上了**（M4）。以前那条"点刷新才碰接口 + 没接上说一句
   关于 M4 的话"的占位**已经删掉** —— 留着的话，接口真坏了门店会看到一句
   关于开发进度的提示，看不懂也没法处理。现在读不到就照后端给的 `error` 说人话。

   `/api/attain` 的约定（M4 按这个实现，前端已按它渲染）：
     {
       exists: true,
       period: "2026-W38", start: "2026-09-14", end: "2026-09-20",
       data_until: "2026-09-17",       // 库里销售最新到哪天（周中会早于 end）
       columns: ["Mate70 Air", …],     // 产品列名
       weights: [0.15, 0.15, 0.1, …],  // 占比
       rows: [{store, targets: [...], actuals: [...], rates: [...], total}],
       missing_columns: ["…"],         // 映射没到位的列（留空 + 提示）
       error: "读不到目标：…"           // 读不到时**必须**给，不许给空 rows
     } */

async function loadAttain() {
  if (attainActivePage !== 'attain') return;
  const request = attainBeginRequest('attain');
  try {
    const result = await api('/api/attain', request.controller
      ? { signal: request.controller.signal } : {});
    if (attainRequestIsCurrent(request)) renderAttain(result);
  } catch (e) {
    if (e && e.name === 'AbortError' || !attainRequestIsCurrent(request)) return;
    // ⚠ 只有**请求本身失败**（网络 / 403）才走这儿；"还没算过"是
    //   `exists: False` + `error`，由 `renderAttain` 说人话。
    $('#attain-meta').textContent = '';
    $('#attain-cards').innerHTML = '';
    $('#attain-table').innerHTML =
      '<div class="empty">读不到达成数据：' + esc(e.message || e) + '</div>';
    toast('读取周度目标达成情况失败：' + e.message, 'bad');
  } finally {
    attainFinishRequest(request);
  }
}

//: 收起成「区域汇总」的那些区（点区域名切换）—— 默认空 = 全展开、不画共计行
//: （用户 2026-09-22 纠正：汇总行**不要常驻**，点区域名合并后才显示）
//: ⚠ 声明必须在 `renderAttain` **之前**（`let` 有 TDZ；函数声明会提升、变量不会）
let attainCollapsedRegions = new Set();
try {
  const raw = localStorage.getItem('cbg-attain-collapsed-regions');
  if (raw) attainCollapsedRegions = new Set(JSON.parse(raw) || []);
} catch (e) { /* 隐私模式 / 坏数据：退回全展开 */ }

function attainPersistRegions() {
  try {
    localStorage.setItem('cbg-attain-collapsed-regions',
                         JSON.stringify([...attainCollapsedRegions]));
  } catch (e) { /* 同上 */ }
}

/** 区域名 → 可点按钮（`collapsed` = 现在收着，点了要展开）。 */
function attainRegionBtn(region, collapsed) {
  return '<button type="button" class="film-region region-toggle"'
    + ' data-attain-region="' + esc(encodeURIComponent(region)) + '"'
    + ' title="点一下' + (collapsed ? '展开门店明细' : '合并为区域汇总') + '">'
    + (collapsed ? '▸' : '▾') + ' ' + esc(region) + '</button>';
}

function toggleAttainRegion(region) {
  if (attainCollapsedRegions.has(region)) attainCollapsedRegions.delete(region);
  else attainCollapsedRegions.add(region);
  attainPersistRegions();
  if (attainLast) renderAttain(attainLast);   // 整表重画（收起态在渲染里判）
}

function renderAttain(d) {
  const meta = $('#attain-meta');
  if (!d || !d.exists) {
    meta.textContent = '';
    $('#attain-cards').innerHTML = '';
    $('#attain-table').innerHTML =
      '<div class="empty">' + esc((d && (d.error || d.hint)) || '还没有数据') + '</div>';
    return;
  }
  const until = d.data_until && d.data_until !== d.end
    ? `　⚠ 数据截至 ${d.data_until}（周中累计，不是最终达成）` : '';
  // ⚠ 门店账号下后端只给本店那一行（`store_filter`）—— 页面上说一句，
  //   免得门店以为"怎么少了几家店"（用户 2026-09-21 报的是反过来的 bug）。
  meta.textContent = `${d.period || ''}　${d.start || ''} ~ ${d.end || ''}${until}`
    + (d.store_filter ? ` · 只看本店（${d.store_filter}）` : '');

  const gap = (d.missing_columns || []).length;
  $('#attain-cards').innerHTML =
    `<div class="kpi"><div class="k">门店数</div><div class="v">${(d.rows || []).length}</div></div>`
    + `<div class="kpi"><div class="k">产品列</div><div class="v">${(d.columns || []).length}`
    + (gap ? ` <span class="hint">（${gap} 列没映射）</span>` : '') + '</div></div>';

  // ⚠⚠ `=> ({ … })` 的**括号不能省**：`=> { html: … }` 会被解析成"函数体里一个 label"，
  //   返回值是 `undefined` ⇒ 每一格都变成空 `<th>` —— 表头**整排消失**，
  //   而表体没事（那边写的是 `return { html: … }`）。
  //   踩的时候表现是"表头怎么都不显示"，前后查了三轮（缓存 / 进程 / 宽度全查过），
  //   真正的原因就是少一对括号。
  const header = ['区域', '门店', '', ...(d.columns || []).map((c, i) => ({
    html: esc(c).replace(/\//g, '/<br>')
          + `<br><span class="hint">${((d.weights || [])[i] * 100).toFixed(0)}%</span>` })),
    '总达成率'];
  // ⚠ 2026-09-19（用户）：「这个界面**太拥挤了**。**双行显示，达成/目标 达成率**。
  //   上面产品类型和占比也**换行**显示」。
  //   ⇒ 每个产品格两行：第一行 `实际/目标`（台量），第二行 **达成率**；
  //     表头两行：产品名 + 它那一行的占比。
  //   ⚠ 想放 `<br>` 必须包成 `{html: …}` —— `table()` 对**字符串**单元格默认 `esc()`，
  //     直接写 `<br>` 会在页面上原样显示出来（AGENTS.md 坑 3，踩过三次）。
  //   ⚠ 产品名要 `esc()` 之后再拼（那是文档里来的文本）。
  attainLast = d;
  attainCols = d.columns || [];        // 面板表头用它（用户：换成产品名）
  const rawRows = d.rows || [];
  const by = {}, groups = [];
  rawRows.forEach((r, i) => {
    // ⚠ 跟后端 `region_sums` 同一把尺（strip + 空 → 其他）——
    //   文档 A 列 / yaml 混用时，同名不同键会「点了折不起来」
    const g = normalizeRegionName(r.region);
    if (!by[g]) { by[g] = []; groups.push(g); }
    by[g].push([r, i]);
  });
  // ⚠ 区域顺序跟月度 `PLAN_REGION_ORDER` / 后端 `REGION_ORDER` 对齐
  //   （服务站之后还有北区 / 西区 —— 原来 pref 只写了四个，那两个区会掉到最后）
  groups.sort((a, b) => {
    const pref = ['西北区', '市区', '南区', '服务站', '北区', '西区'];
    return (pref.indexOf(a) < 0 ? 99 : pref.indexOf(a))
         - (pref.indexOf(b) < 0 ? 99 : pref.indexOf(b)) || a.localeCompare(b);
  });
  // ⚠ `attainPeople` 的下标 = **tbody 里的行号**（悬停用 `sectionRowIndex` 查）——
  //   所以必须**按显示顺序**（含分区共计行）重建，不能沿用 `d.rows` 原始顺序。
  const rows = [];
  attainPeople = [];
  groups.forEach((g) => {
    const gCollapsed = attainCollapsedRegions.has(g)
      && !!(d.region_sums || {})[g];
    // 收起成区域汇总：**整区门店行不画**（含拆到人的子行），只留下面那行汇总
    // ⚠ 没算出 region_sums 时不当收起（收了会整区消失）
    if (!gCollapsed) by[g].forEach(([r, ri], gi) => {
    // ⚠ 2026-09-19（用户给的样式）：每格**三段**——
    //     达成 7
    //     目标 6
    //     116.7%
    //   「达成 / 目标」这两个字用 `.hint`（暗一档），数字正常色，达成率加粗。
    //   这样一眼能对上"卖了几台、目标几台、差了多远"，而不用心算 `7/6`。
    // ⚠ 2026-09-19（用户）：「**最左边的达成和目标不在第一个品类下面，单独出来**。
    //   **这个 2 和 3 数字大点**」。
    //   ⇒ 第一个品类的"台量"从它自己那一列里**提出来**，单独占一列（表头写「达成/目标」）；
    //     于是每个品类列只剩**达成率**，横着扫一列百分比最清楚；
    //     台量数字放大（`.attain-num`），"卖了几台"比"百分比"更该被一眼看到。
    // ⚠ 2026-09-19 **第四版**（用户逐版看过之后定的）：
    //   「**只挪两个数字**，**目标和达成两个字还在单独的一列里**」
    //   ⇒ 单独那一列**只留「达成 / 目标」两个字**（表头留空 —— 用户说红框那个不要），
    //     数字搬回**第一个产品列**里、跟它的达成率并排；放大照旧。
    // ⚠ 2026-09-19（用户）：「这个 7 和 6 中间**多一个浅灰的分割线**吧，**分割线要居中**」
    //   ⇒ 两个数字之间插一条 `.attain-sep`（高度 1px、宽度撑满这一小格、颜色走令牌）。
    // ⚠ 2026-09-20：目标数字包一层 `.num-box`（**固定 44px、居中**）——
    //   跟展开行的 `.split-edit` / `.split-in` **同宽**。
    //   三种状态（主表那行 / 展开静止 / 展开编辑）宽度一致 ⇒ 既不动、也一直对齐。
    const nums = (i) => `<span class="attain-pair">`
      + `<span class="attain-num">${r.actuals[i]}</span>`
      + `<span class="attain-sep"></span>`
      + `<span class="attain-num num-box">${r.targets[i]}</span></span>`;
    const rateOf = (i) => {
      const rate = (r.rates || [])[i];
      // ⚠ `class="rate"` 是给**固定槽位**用的（用户 2026-09-20：「**能不能等宽啊**」）：
      //   百分比宽度 35~53px 不等 ⇒ 色块跟着一格一个宽度（98~115px）。
      //   给这一格定死宽度 + 右对齐 ⇒ 所有色块等宽，而且百分比那一列对得齐。
      return rate == null ? '<b class="rate hint">—</b>'
                          : `<b class="rate">${(rate * 100).toFixed(1)}%</b>`;
    };
    // 第一列 = 只有标签（"达成 / 目标"），告诉右边那一摞数字是什么
    // ⚠ 2026-09-19（用户）：「前面的达成目标和后面的两行数**不在一个高度上**」——
    //   根因：两边的堆叠**总高不一样**。数字那边是「数字 + 分割线 + 数字」，
    //   标签这边只有两行字 ⇒ 同样的 `top` 起点下，第二行自然错开。
    //   ⇒ 标签这边也插一条**同样尺寸的分割线**（只是透明），两边节奏就一模一样了。
    const labels = { html: '<span class="attain-pair">'
                           + '<span class="hint">达成</span>'
                           + '<span class="attain-sep attain-sep-ghost"></span>'
                           + '<span class="hint">目标</span></span>' };
    // ⚠ 2026-09-19（用户）：小圆点**去掉了** ——「这样的话小圆点没必要出现了」。
    //   理由本来就成立：触发器是**整个格子**（对准 8px 太苛刻），
    //   圆点只是"从这里长出来"的视觉起点，那就不该常驻在那儿。
    //   ⇒ 起点改成**格子右侧那个位置**（原来圆点待的地方），人选不到也不影响。
    // ⚠ 2026-09-19（用户）：「图片中这种**完整的是一个格子**，不是只认数字。
    //   **这个区域内的空白区域也要**」⇒ 悬停面 = **整个 `<td>`**（含四周空白），
    //   而不是这几个数字拼出来的那个小盒子。
    //   ⇒ 行/列号不再往 DOM 上写：`td` 自己就有 `cellIndex`，
    //     行号取 `tr.sectionRowIndex`（tbody 里第几行）—— 一一对应，不用额外标记。
    const cells = (r.rates || []).map((rate, i) =>
      ({ html: `<span class="attain-cell">${nums(i)}${rateOf(i)}</span>`,
         cls: attainTier(rate, r.targets[i]) }));
    const total = r.total == null ? '—' : `${(r.total * 100).toFixed(1)}%`;
    // ⚠ 用户 2026-09-20：「**周度目标达成情况点一下门店名称，直接展开**，
    //   里面内容是**每个人的目标和达成情况**」（不用另做汇总页）。
    const name = `<button class="store-link" data-expand="${esc(r.store)}"`
      + ` title="点一下看每个人的目标与达成">${esc(r.store)}</button>`;
    attainPeople.push(r.people || []);
    rows.push([{ html: (gi === 0
          ? attainRegionBtn(g, false) : '') },
        { html: name }, labels, ...cells, { html: `<b>${total}</b>` }]);
    });
    // ⭐ 区汇总**默认不画**；点区域名收起该区后，只留这一行（用户 2026-09-22：
    //   「点击区域名合并，然后才显示汇总，不是现在这样」）。数用后端 region_sums。
    const sum = (d.region_sums || {})[g];
    if (gCollapsed && sum) {
      attainPeople.push([]);              // 汇总行没有"谁卖的"，占位防下标错位
      const sCells = (sum.rates || []).map((rate, i) => {
        const a = (sum.actuals || [])[i] || 0;
        const t = (sum.targets || [])[i] || 0;
        const html = `<span class="attain-cell"><span class="attain-pair">`
          + `<span class="attain-num">${a}</span>`
          + `<span class="attain-sep"></span>`
          + `<span class="attain-num num-box">${t}</span></span>`
          + (rate == null ? '<b class="rate hint">—</b>'
                          : `<b class="rate">${(rate * 100).toFixed(1)}%</b>`)
          + '</span>';
        return { html, cls: attainTier(rate, t) };
      });
      const sTotal = sum.total == null ? '—' : `${(sum.total * 100).toFixed(1)}%`;
      rows.push({ cls: 'film-sum', cells: [
        { html: attainRegionBtn(g, true) },
        { html: `<b>${esc(sum.store || ('共计：' + g))}</b>` },
        { html: '<span class="attain-pair">'
          + '<span class="hint">达成</span>'
          + '<span class="attain-sep attain-sep-ghost"></span>'
          + '<span class="hint">目标</span></span>' },
        ...sCells, { html: `<b>${sTotal}</b>` }] });
    }
  });
  // ⚠ 外面套一层可横向滚动的容器（见 CSS `.table-scroll`）：
  //   10 个产品列 + 门店 + 总达成率，窄屏上整张表比内容区还宽 ——
  //   不套的话右边那几列会被**裁掉**（看着就像"没显示出来"）。
  $('#attain-table').innerHTML = '<div class="table-scroll">' + table(header, rows) + '</div>'
    + (gap ? `<div class="banner warn" style="margin-top:10px">`
             + `这几列还没配编码，没计入总达成率：${esc((d.missing_columns || []).join('、'))}</div>`
           : '');
}

/* ─────────────── 历史记录（用户 2026-09-20 要的）───────────────

   用户原话：
   「加个**历史记录**功能，这一周过去之后，比如这一周 14-20 号，**21 号再获取达成**，
     就把**上一周的给锁住存档**，在工作区的**周度目标达成情况下面**加个历史记录」

   ⚠⚠ 这一页**只读**，而且必须是只读：后端唯一的写入口在 `attain.run()` 里
     换周那一下。这里若能改，"锁住"就不成立了 —— 而复盘引用的正是这一份。
   ⚠ 刻意**不接**「点门店名展开 / 改目标 / 发给区长」那条链：
     那些是"当前这一周"的动作（周中还能再卖、目标还能再调），存档没有这些后续。
   ⚠ 标色开关（`#attain-color`）**照旧生效** —— 它读的是同一个 `attainColorOn`，
     历史页和主表同色系，翻旧账时不会两套颜色。 */

let attainHistCur = '';       // 现在停在那一周（切回来时别跳回最新那周）

async function loadAttainHistory(period) {
  if (attainActivePage !== 'attain-history') return;
  const request = attainBeginRequest('attain-history');
  const opts = request.controller ? { signal: request.controller.signal } : {};
  const listEl = $('#attain-hist-list');
  const bodyEl = $('#attain-hist-body');
  try {
    const d = await api('/api/attain/history', opts);
    if (!attainRequestIsCurrent(request)) return;
    const items = (d && d.items) || [];
    if (!items.length) {
      $('#attain-hist-meta').textContent = '';
      listEl.innerHTML = '';
      bodyEl.innerHTML = '<div class="empty">还没有历史记录 —— 每到新的一周、'
        + '第一次获取达成时，上一周就会锁住存到这里。</div>';
      return;
    }
    // ⚠ 不给就停在**最新那一周**；给的那一周可能已经不在列表里了（换年份、被归档策略挪走），
    //   那种情况回落到最新 —— 别让右边空着而左边没选中项。
    let cur = period || attainHistCur;
    if (!items.some((x) => x.period === cur)) cur = items[0].period;
    attainHistCur = cur;
    listEl.innerHTML = items.map((x) => {
      const avg = x.avg == null ? '—' : (x.avg * 100).toFixed(1) + '%';
      // ⚠ 列表里只写"月-日"：左边这一栏只有 150px，写全 `2026-09-07 ~ 2026-09-13`
      //   会折成两行（实测）。年份在上面的期间里已经有了（`2026-W37`），
      //   完整日期在右边表头那行也写着 —— 这里省掉不影响对得上。
      const md = (d) => (d || '').slice(5) || '';
      return `<button class="hist-item${x.period === cur ? ' current' : ''}"`
        + ` data-period="${esc(x.period)}">`
        + `<span class="hist-p">${esc(x.period)}</span>`
        + `<span class="hist-d hint">${esc(md(x.start))} ~ ${esc(md(x.end))}</span>`
        + `<span class="hist-s hint">${x.stores} 家店　平均达成 ${avg}</span></button>`;
    }).join('');
    Array.from(listEl.querySelectorAll('[data-period]')).forEach((b) =>
      b.addEventListener('click', () => loadAttainHistory(b.dataset.period)));
    const detail = await api('/api/attain/history?period=' + encodeURIComponent(cur), opts);
    if (attainRequestIsCurrent(request)) renderAttainHistory(detail);
  } catch (e) {
    if (e && e.name === 'AbortError' || !attainRequestIsCurrent(request)) return;
    $('#attain-hist-meta').textContent = '';
    bodyEl.innerHTML = '<div class="empty">读不到历史记录：' + esc(e.message || e) + '</div>';
    toast('读取历史记录失败：' + e.message, 'bad');
  } finally {
    attainFinishRequest(request);
  }
}

function renderAttainHistory(d) {
  const meta = $('#attain-hist-meta');
  if (!d || !d.exists) {
    meta.textContent = '';
    $('#attain-hist-body').innerHTML =
      '<div class="empty">' + esc((d && (d.error || d.hint)) || '没有这一周的存档') + '</div>';
    return;
  }
  // ⚠ 「数据截至」在存档里**不用再提示周中**：那一周已经过完了，记的就是最终那份。
  //   但"存的是什么时候的数"仍然写出来 —— 归档时间戳就是它的来历。
  meta.textContent = `${d.period || ''}　${d.start || ''} ~ ${d.end || ''}`
    + `　🔒 ${d.locked_at ? d.locked_at + ' 归档' : '已锁住'}`
    + (d.data_until ? `　（数据截至 ${d.data_until}）` : '')
    + (d.store_filter ? ` · 只看本店（${d.store_filter}）` : '');
  const cols = d.columns || [];
  const header = ['门店', ...cols.map((c, i) => ({
    html: esc(c).replace(/\//g, '/<br>')
          + `<br><span class="hint">${(((d.weights || [])[i] || 0) * 100).toFixed(0)}%</span>` })),
    '总达成率'];
  const rows = (d.rows || []).map((r) => {
    const cells = cols.map((_, i) => {
      const rate = (r.rates || [])[i];
      // ⚠ 同上（`class="rate"`）：历史页那几个色块也要**等宽**，两处得一起改。
      const rateTxt = rate == null ? '<b class="rate hint">—</b>'
                                   : `<b class="rate">${(rate * 100).toFixed(1)}%</b>`;
      return { html: `<span class="attain-cell">`
        + `<span class="attain-pair"><span class="attain-num">${r.actuals[i]}</span>`
        + `<span class="attain-sep"></span>`
        + `<span class="attain-num">${r.targets[i]}</span></span>${rateTxt}</span>`,
        cls: attainTier(rate, (r.targets || [])[i]) };
    });
    return [{ html: esc(r.store) }, ...cells,
            { html: `<b>${r.total == null ? '—' : (r.total * 100).toFixed(1) + '%'}</b>` }];
  });
  $('#attain-hist-body').innerHTML = '<div class="table-scroll">' + table(header, rows) + '</div>';
}

/* ─────────────── 达成格：悬停出一块「谁卖的」（用户 2026-09-19 定的交互）───────────────

   用户原话：
   「能把上图中这样一个组作为一个格子嘛，鼠标移动上去会出现一个二级菜单，
     二级菜单出现**先从一个圆横向拉长成线，再纵向拉长**。具体填什么内容待会整理给你」
   → 内容定了：「**二级菜单显示这个达成销售的人是谁**」。
   → 起点：「**8px 可以，在达成率百分号右边，不挡着格子显示**」。
   → 方向：「**向上向下看窗口位置吧**，如果向下能完全展开就向下，
     如果不能完全展开，那就**上下一起**，下面到界面最下方，
     如果就在最下面，那就**只往上拉长**」。

   ⚠ 为什么用 `position: fixed`：这张表外面有 `.table-scroll`（`overflow-x: auto`），
     **溢出容器会把它里面的浮层裁掉** —— 挂在格子里做绝对定位会被切一半。
     固定定位按小圆点的屏幕坐标算，跟表格怎么滚没关系。

   ⚠ 两段动画**全靠 CSS**（`transition` + `transition-delay`）：
     宽度先动，高度等宽度差不多到位了再动 —— JS 只负责给最终尺寸和方向。 */

let attainPeople = [];          // 行 × 列 → [[[人名, 台量], …], …]
let attainHideTimer = null;
let attainPopEl = null;
let attainCell = null;      // 当前停在哪个目标格（点过或正在编辑）—— 方向键从它出发
let attainCols = [];
let attainLast = null;          // 上一次渲染用的数据（切开关时原地重画，不重新请求）
let attainColorOn = false;      // 「标色」开关（用户 2026-09-19 要的）
// 开关状态记在 localStorage：跟着 `cbg-side-collapsed` 那套惯例（读写都包 try）
try { attainColorOn = localStorage.getItem('cbg-attain-color') === '1'; } catch (e) {}
let attainPopPeople = [];       // 当前面板里那份名单（第三级弹窗按行号取商品名）
let attainPop2El = null;
let attainCurKey = '';          // 现在弹的是哪一格（`行:列`）—— 同格内移动不许重放动画         // ⚠ 动态建的元素**自己攥着引用**，别按 id 去查它 ——
                                //   项目有条测试"app.js 里出现的 id 必须在 index.html 里"
                                //   （那条是对的：静态 id 写错就是拼错）。

/** 达成率 → 底色档位。
 *
 *  | 达成率 | 底色 |
 *  |---|---|
 *  | < 60% | **绿** |
 *  | 60% ~ 80% | 橙 |
 *  | 80% ~ 100% | 浅绿 |
 *  | 100% ~ 120%（封顶） | **红** |
 *
 *  ⚠⚠ **档位边界和颜色都改过，别再"顺手改回去"**：
 *    * 2026-09-19（用户）：四档是他定的，那时是"低=红、高=绿"；
 *    * **2026-09-20（用户）：「红色和绿色反过来吧，100%-120% 是红色」**
 *      ⇒ 现在两头调了个个儿：**高达成标红、低达成标绿**。
 *    直觉上像是反的，但这是用户明确要的口径（他看的是"超标要注意"），
 *    `tests/test_attain.py::Test达成率标色开关` 钉着现在这一版。
 *  ⚠ 类的名字永远是**颜色**（`t-red` 就是红的），换档位只动这里的映射 ——
 *    别去把 `--rate-red` 的值改绿（那样令牌名就成了谎话，换主题的人会被坑）。
 *  ⚠⚠ **目标为 0 的那一格不上色** —— 用户明确说过：
 *    「注意 **0 目标的 100%** 还是现在这个底色就行」。
 *    口径上"目标 0 ⇒ 记 100%"，但那不是"卖得好"，是"没定目标"。
 *  ⚠ 没映射的列（rate 是 `null`）同样不上色。
 */
function attainTier(rate, target) {
  if (!attainColorOn || rate == null || !target) return '';
  if (rate < 0.6) return 't-g';
  if (rate < 0.8) return 't-orange';
  if (rate < 1) return 't-lg';
  return 't-red';
}

function attainPop() {
  if (!attainPopEl) {
    attainPopEl = document.createElement('div');
    attainPopEl.id = 'attain-pop';
    attainPopEl.className = 'attain-pop';
    document.body.appendChild(attainPopEl);
  }
  return attainPopEl;
}

/** 悬停出来那块面板 —— **圆 → 横线 → 竖面板**，方向按窗口空间自适应。
 *
 *  ⚠ 位置尺寸全走 **inline 样式**，不用 CSS 自定义属性：项目有条测试盯着
 *    "CSS 里用到的 `var(--x)` 必须在 theme.css 里定义"，而 `--pw/--ph` 是
 *    **运行时算出来的**，不该混进设计令牌表。
 *  ⚠ 两段动画仍然**全靠 CSS**：高度那一项在 `transition` 里带 `delay`，
 *    所以 JS 只写最终尺寸，浏览器自己"先横后竖"。
 *  ⚠ 200ms 宽限：鼠标扫过格子不该一路弹面板（用户要的是"停在上面看一眼"）。
 */
function showAttainPop(cell, ri, ci) {
  clearTimeout(attainHideTimer);
  const people = ((attainPeople[ri] || [])[ci]) || [];
  if (!people.length) return;
  const el = attainPop();
  // ⚠ 2026-09-19（用户）：「最上面一行『这一格是谁卖的』改成**表头那几个产品名**，
  //   这个**就不换行了**」⇒ 表头写那一列的产品名（斜杠不折行），
  //   太长就省略号，全名挂在 title 上（面板只有 200px 宽）。
  const colName = attainCols[ci] || '';
  attainPopPeople = people;
  el.innerHTML = `<div class="attain-pop-head" title="${esc(colName)}">${esc(colName)}</div>`
    // ⚠ 2026-09-19（用户）：「弹窗里面**每一条，鼠标移上去，再出现个二级弹窗**，
    //   里面是**卖的每个的商品名称**」⇒ 每条挂行号，悬停时再展开一层列商品名。
    + people.map(([who, qty], pi) =>
        `<div class="attain-pop-row" data-p="${pi}"><span>${esc(who)}</span><b>${qty}</b></div>`
      ).join('');

  const pw = 200;
  el.style.width = pw + 'px';                  // 先按最终宽度量高度
  el.style.height = 'auto';
  const ph = Math.min(el.scrollHeight, 260);   // ⚠ 太高就内部滚，别顶出屏幕
  el.classList.remove('open');

  const box = cell.getBoundingClientRect();
  // ⚠ 用户 2026-09-19：「弹出的**离得远了点，近一点**」⇒ 间距 6 → 2。
  const vh = window.innerHeight, gap = 2;
  const below = vh - box.bottom - gap, above = box.top - gap;
  // 用户定的三条：能向下就向下 / 不能就**上下一起**（下沿贴视口底）/ 在最下面就**只往上**
  let top;
  if (below >= ph) top = box.bottom + gap;
  else if (above >= ph) top = box.top - ph - gap;
  else top = Math.min(box.bottom + gap, vh - ph - gap);
  // 横向跟格子**左边缘对齐** —— 不再甩到格子右边（那也是"离得远"的一半原因）
  let left = box.left;
  if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
  top = Math.max(8, top); left = Math.max(8, left);

  // ⚠ 2026-09-19（用户）：**不要"圆 → 线 → 面板"那套了** ——
  //   「没有圆的话拉长的效果不好看了，改成**从格子的大小放大到这个弹出页面的界面**的动画吧」
  //   ⇒ 起点 = **那个格子本身的矩形**（位置和大小都照抄），终点 = 面板的位置和大小，
  //     四个量（left/top/width/height）**同时**过渡 ⇒ 看着就是"格子撑开成了面板"。
  //   ⚠ 所以 CSS 里那条"高度带 delay"的两段式要去掉（那是给圆点那版写的）。
  el.style.left = box.left + 'px';
  el.style.top = box.top + 'px';
  el.style.width = box.width + 'px';
  el.style.height = box.height + 'px';
  el.classList.add('open');
  requestAnimationFrame(() => {
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    el.style.width = pw + 'px';
    el.style.height = ph + 'px';
  });
}

/** 第三级弹窗：某个人**卖了哪些商品**（挂在那一行右边）。 */
function showAttainPop2(row) {
  const pi = Number(row.dataset.p);
  const rec = attainPopPeople[pi] || [];
  const items = rec[2] || [];
  if (!items.length) return;
  if (!attainPop2El) {
    attainPop2El = document.createElement('div');
    attainPop2El.className = 'attain-pop2';
    document.body.appendChild(attainPop2El);
  }
  // ⚠ 2026-09-19（用户）：「商品名里有斜线，**只要最右边的 / 右边的内容**吧」——
  //   商品名的前缀是同质的（`智能手机/华为/…`），只有最后一段是有效信息。
  //   全名留在 `title` 里（想要完整信息时鼠标停一下就有）。
  //   ⚠ 用 `lastIndexOf`（**最右边**那个斜杠），不是第一个。
  // ⚠ 2026-09-19（用户）：「`Mate X7 DEL-AL10(12GB+512GB) 全网通版 云锦白` 里面
  //   这个 `DEL-AL10 全网通版` 可以去掉，类似的结构也行，**但是我怕你处理失误**」。
  //   ⇒ 规则**先在真数据上跑过一遍**（当时库里 47 个商品名，逐个对过：
  //     没有一个被清空或清残），才写在这儿：
  //     ① 只留最右边那个 `/` 之后；
  //     ② 去掉「全网通版 / 网通版」；
  //     ③ 去掉型号码（两段都可能带数字，`DEL-AL10` / `AGS6-W00` / `CRS-AL00`）——
  //        `(?=[A-Z0-9-]*[A-Z])` 是保证这两段里**至少有一个字母**，
  //        免得误吃 `12GB+512GB` 这种纯数字加号的东西；
  //     ④ 收拾掉去掉之后剩下的零碎（`) -曜金黑` → `)-曜金黑`、多余空格）。
  //   ⚠ **颜色 / 容量 / 版本（典藏版 / 焕新版 / 昆仑玻璃）一律留着** —— 那些是有效信息。
  //   ⚠ 全名挂在 `title` 里，鼠标停一下还是能看到原始的。
  // ⚠ 2026-09-19（用户）：「商品名里有斜线，只要最右边的 / 右边的内容吧」
  //    + 「`DEL-AL10 全网通版` 可以去掉，类似的结构也行，**但是我怕你处理失误**」
  //    + 「**47 个不够吧**…库里那么多商品呢，这次是这些重点产品，以后呢」。
  //   ⇒ 规则**在全库 3049 个商品名上跑过一遍**才写在这儿：
  //     清空 0 个、退化回原名 3 个（`防尘罩`/`支架`/`理线架` 这种本来就短），其余正常。
  //   ① 去掉「全网通版 / 网通版」和型号码（`DEL-AL10` / `AGS6-W00` / `CRS-AL00`）；
  //      `(?=[A-Z0-9-]*[A-Z])` 保证那两段里**至少一个字母** ⇒ 不会误吃 `12GB+512GB`；
  //   ② 按 `/` 切开、丢掉空段（**这一步顺手解决了"最后一段是型号码、被去空"的情况**，
  //      例如 `摄像头/…/XT-H33C` → 清完剩 `小豚当家室外摄像头3K升级版`）；
  //   ③ 取尾巴；两条护栏把它往前接：
  //      * **太短**（`银河灰`、`32G` 这种颜色/容量）⇒ 接上一段；
  //      * **纯英文短码**（`RTX5070-`）⇒ 也接上一段；
  //   ④ 实在什么都剩不下 ⇒ **退回原名**（宁可显示长，也不能显示空）。
  //   ⚠ 颜色 / 容量 / 版本（典藏版、焕新版、昆仑玻璃）**一律留着**。
  //   ⚠ 全名挂在 `title` 里，鼠标停一下就能看到原始的。
  const stripNoise = (t) => t
    .replace(/\s*(全网通版|网通版)\s*/g, ' ')
    .replace(/\s*(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{2,6}-[A-Z0-9]{2,6}\s*/g, ' ')
    .replace(/\)\s*-/g, ')-')
    .replace(/\s+-/g, '-')
    .replace(/\s+\)/g, ')')
    .replace(/\s{2,}/g, ' ')
    .trim();
  // ⚠⚠ **丢掉的是开头两段（品类 / 品牌），后面全留着** ——
  //   2026-09-19 第一版是"只取最后一段"，于是 `智能手机/华为/Pura 80/Pura 80 Ultra`
  //   这种"产品名自己占一段"的名字会把 `Pura 80` 丢掉（用户当场发现：
  //   「第三列这个 pura80ultra 的也有问题，**pura 80 没了**」）。
  //   ⇒ 改成丢前缀之后，**永远不会丢产品名那一段**；全库 3049 个商品名验过：
  //     清空 0 个、不足 4 字 18 个（都是 `防尘罩`/`支架` 这种本来就短的名字）。
  const short = (n) => {
    const parts = stripNoise(String(n || '')).split('/').map((x) => x.trim()).filter(Boolean);
    if (!parts.length) return String(n || '');       // 兜底：绝不显示空
    return parts.length >= 3 ? parts.slice(2).join('/') : parts[parts.length - 1];
  };
  attainPop2El.innerHTML = items
    .map((n) => `<div title="${esc(n)}">${esc(short(n))}</div>`).join('');
  const box = row.getBoundingClientRect();
  const w = 280;               // 商品名很长，第三级比第一级宽
  let left = box.right + 4;
  if (left + w > window.innerWidth - 8) {
    // ⚠ 2026-09-19（用户）：「如果右边显示不开了，就**从二级的左边缘往左展开**」——
    //   不是"贴在那一行左边"（那会压住二级面板），而是**并到二级面板的外侧**。
    const main = attainPopEl ? attainPopEl.getBoundingClientRect() : box;
    left = main.left - w - 4;
  }
  attainPop2El.style.left = Math.max(8, left) + 'px';
  attainPop2El.style.top = Math.max(8, box.top - 4) + 'px';
  attainPop2El.style.width = w + 'px';
  attainPop2El.classList.add('open');
}

function hideAttainPop2() {
  if (attainPop2El) attainPop2El.classList.remove('open');
}

function scheduleHideAttainPop() {
  clearTimeout(attainHideTimer);
  attainHideTimer = setTimeout(() => {
    if (attainPopEl) attainPopEl.classList.remove('open');
    hideAttainPop2();
  }, 160);       // 宽限：从格子挪到面板的路上别闪掉
}

/** 立刻收掉（切页 / 点别处）—— 不走 160ms 宽限。 */
function forceHideAttainPop() {
  clearTimeout(attainHideTimer);
  clearTimeout(attainShowTimer);
  attainCurKey = '';
  if (attainPopEl) attainPopEl.classList.remove('open');
  hideAttainPop2();
}

// ⚠ 触发器是**整个格子**（`user 2026-09-19`：整个组当一个格子），不是那个 8px 圆点 ——
//   2026-09-19 用户把那个小圆点也去掉了（「小圆点没必要出现了」）—— 更对：
//   200ms 后才真弹 ⇒ 鼠标扫过一排格子不会一路弹。
let attainShowTimer = null;
document.addEventListener('mouseover', (e) => {
  if (attainActivePage !== 'attain') return;
  const t = e.target;
  if (!t || !t.closest) return;
  if (attainPopEl && attainPopEl.contains(t)) {          // 指到面板上：别收
    clearTimeout(attainHideTimer); clearTimeout(attainShowTimer);
    const row = t.closest('.attain-pop-row');            // 指到某一行 ⇒ 出第三级
    if (row) showAttainPop2(row); else hideAttainPop2();
    return;
  }
  if (attainPop2El && attainPop2El.contains(t)) return;  // 指到第三级上：也别收
  // ⚠ 触发面 = **整个单元格**（用户：「这个区域内的空白区域也要」）——
  //   所以认 `td`，再由它在表格里的位置反推行/列：
  //   第 0 列是门店、第 1 列是「达成/目标」标签，产品列从第 2 列开始。
  // ⚠⚠ 只在**达成那张表**里弹（2026-09-22）：`attainPeople` 会一直留着上一次的
  //   数据，别的页（防护膜表也有 cellIndex≥2）一悬停就会弹出「谁卖的」—— 串页。
  const td = t.closest('td');
  const inAttain = td && td.closest('#attain-table');
  if (inAttain && td.parentElement && td.parentElement.tagName === 'TR' && td.cellIndex >= 3) {
    const ri = td.parentElement.sectionRowIndex;
    const ci = td.cellIndex - 3;   // 区域 + 门店 + 达成/目标 标签
    const people = ((attainPeople[ri] || [])[ci]) || [];
    if (people.length) {
      // ⚠⚠ 用户 2026-09-19：「鼠标一动，虽然没出格子，但是这个动画还会**重新放一遍**」。
      //   根因：`mouseover` 是**按元素**触发的 —— 指针在格子里从数字挪到百分比，
      //   跨过 `.attain-num` / `.attain-sep` / `<b>` 的边界就再触发一次，
      //   于是每次都清定时器、重新等 200ms、再放一遍展开动画。
      //   ⇒ 记住"现在弹的是哪一格"，**同一格直接返回**。
      //   ⚠ 这条对所有悬停交互都适用：想"进入某区域只做一次"就得自己记区域。
      const key = ri + ':' + ci;
      if (key === attainCurKey) return;
      attainCurKey = key;
      clearTimeout(attainHideTimer);
      clearTimeout(attainShowTimer);
      attainShowTimer = setTimeout(() => showAttainPop(td, ri, ci), 200);
      return;
    }
  }
  attainCurKey = '';        // 指针不在任何"有人卖"的格子里了
  hideAttainPop2();
  if (attainPopEl && attainPopEl.classList.contains('open')) scheduleHideAttainPop();
});
document.addEventListener('click', () => {
  if (attainActivePage === 'attain') forceHideAttainPop();
});

/* 「目标拆分」**不再单独一页**（用户 2026-09-20 改的）——
   目标现在在「周度目标达成情况」里改：**点门店名就地展开**，只有**门店账号**能改。
   （下面这两个全局是那一页留下的，展开区还要用：域名册人员 + 期间。） */

let splitRoster = [];      // 云商在册人员姓名（默认拉一次，拉不到就只用"卖过的人"）
let splitPeriod = '';      // 当前期间（保存目标要用）
//: 当前展开那家店的成员名单（**按顺序**）—— 保存时 `data-m` 是个下标，要拿它换名字。
//: ⚠ 名单**来自后端那次响应**（不是前端自己拼的），换店/收起就跟着换。
let splitMembers = [];

/* 点门店名 → 就地展开**每个人的目标和达成**（用户 2026-09-20）。
   ⚠ 数据来自拆分那份（`/api/attain/split`）：目标是**人填的**、达成是**算出来的**，
     一格同时看到"分了多少 / 卖了多少 / 达成率"。
   ⚠ 2026-09-22：展开/收起跟**月度生意计划**同一套上下动画（高度压 `.attain-in`，
     见 `attainAnimate` / `style.css` 的 `.attain-anim`）。 */
//: 展开/收起**串行**——「保存目标」要 `await` 收起再 `await` 展开，
//: 连点两下也得排队；动画中途插一脚会把行删在半路（行高过渡直接落空）。
let attainToggleChain = Promise.resolve();

function toggleStoreDetail(btn) {
  const p = attainToggleChain.then(() => doToggleStoreDetail(btn));
  attainToggleChain = p.then(() => {}, () => {});     // 失败也别把队列卡死
  return p;
}

//: 收尾要**晚于** `--motion-slow`（.34s）+ 两层 rAF（≈32ms）—— 跟月度计划同一条
const ATTAIN_ANIM_MS = 480;

/** 格子内容包一层 `.attain-in` —— `tr` 的 height 过渡不了，行高只能压在这层上。 */
function attainWrapCells(tr) {
  Array.from(tr.children).forEach((td) => {
    // ⚠ 别用 `:scope >`（老 Chromium 上不稳）：只认**直接子级**那层，包过就跳过
    if (td.children.length === 1 && td.firstElementChild
        && td.firstElementChild.classList.contains('attain-in')) return;
    const d = document.createElement('div');
    d.className = 'attain-in';
    while (td.firstChild) d.appendChild(td.firstChild);
    td.appendChild(d);
  });
}

function attainApplyHeights(rows, heights) {
  rows.forEach((r, i) => {
    Array.from(r.children).forEach((c) => {
      const d = c.querySelector('.attain-in');
      if (d) d.style.height = heights[i] + 'px';
      c.classList.toggle('attain-row-hide', heights[i] === 0);
    });
  });
}

function attainMeasure(rows) {
  return rows.map((r) => {
    const hs = Array.from(r.children).map((c) => {
      const d = c.querySelector('.attain-in');
      return d ? Math.round(d.getBoundingClientRect().height) : 0;
    });
    // ⚠ `Math.max()` 空参是 `-Infinity`（全空格子时会写出 `height: -Infinitypx`）
    return hs.length ? Math.max.apply(null, hs) : 0;
  });
}

/* 一次上下动效的三步（跟月度 `planAnimate` 同一套，只是**不重画整表**——
   这边是直接插/删行）：
   ① 冻结：量自然高度；展开则先写 0 高（起点，必须先真的进过 DOM）
   ② 展开等两层 rAF 再放开；收起起点已在屏上，落实像素后立刻写 0
   ③ 到点收尾：收起的行 remove，展开的摘掉临时 class / 内联高度 */
function attainAnimate(rows, dir) {
  if (!rows.length) return Promise.resolve();
  const table = rows[0].closest('table');
  if (!table) {
    if (dir === 'shrink') rows.forEach((r) => r.remove());
    return Promise.resolve();
  }
  rows.forEach((r) => {
    r.classList.add('attain-anim');
    attainWrapCells(r);
  });
  table.classList.add('attain-frozen');
  const nats = attainMeasure(rows);
  // ⚠ 两个方向都要**写死起点像素**：展开写 0（长出来）；收起写自然高——
  //   `height: auto → 0` **过渡不了**（CSS 不把 auto 当可插值起点，会直接跳没）。
  attainApplyHeights(rows, dir === 'grow' ? nats.map(() => 0) : nats);
  return new Promise((resolve) => {
    const openTransition = () => {
      table.classList.remove('attain-frozen');
      void table.offsetWidth;                        // 起点像素先落地，再写终点
      attainApplyHeights(rows, dir === 'grow' ? nats : rows.map(() => 0));
      setTimeout(() => {
        if (dir === 'shrink') rows.forEach((r) => r.remove());
        else rows.forEach((r) => {
          r.classList.remove('attain-anim');
          Array.from(r.children).forEach((c) => {
            const d = c.querySelector('.attain-in');
            if (d) d.style.height = '';
            c.classList.remove('attain-row-hide');
          });
        });
        resolve();
      }, ATTAIN_ANIM_MS);
    };
    if (dir === 'grow') {
      // 展开：**两层 rAF** —— 0 高必须先真的画过一帧，否则起点和终点同一帧算样式，没动画。
      requestAnimationFrame(() => requestAnimationFrame(openTransition));
    } else {
      // 收起：起点本来就是屏幕上那份自然高 —— 落实像素后**立刻**写 0。
      //   再等两层 rAF 会让人觉得「点了卡一下才开始收」（用户：「收起来顿一下」）。
      openTransition();
    }
  });
}

async function doToggleStoreDetail(btn) {
  const store = btn.dataset.expand;
  const tr = btn.closest('tr');
  const tag = 'd-' + store.replace(/[^\w\u4e00-\u9fa5]/g, '');
  const mine = document.querySelectorAll('[data-detail="' + tag + '"]');
  if (mine.length) {                                   // 再点一次收起
    btn.classList.remove('open');
    await attainAnimate(Array.from(mine), 'shrink');
    return;
  }
  // 换一家店：上一家的行**立刻**收掉（不播收起动画——焦点已经在这次点击上了），
  // 同时清掉它的 `.open`，免得按钮状态和真实展开对不上。
  document.querySelectorAll('[data-expand].open').forEach((b) => {
    if (b !== btn) b.classList.remove('open');
  });
  document.querySelectorAll('[data-detail]').forEach((x) => x.remove());
  btn.classList.add('open');
  let d;
  try {
    d = await api('/api/attain/split?store=' + encodeURIComponent(store)
                  + '&period=' + encodeURIComponent((attainLast || {}).period || ''));
  } catch (e) {
    btn.classList.remove('open');                      // ⚠ 失败别把"展开中"留着
    toast('读不到拆分：' + e.message, 'bad');
    return;
  }
  if (!d.exists || !(d.members || []).length) {
    btn.classList.remove('open');
    toast((d && d.error) || '这家店这周还没有人分到目标', 'bad');
    return;
  }
  // ⚠ 名单是"门店在职全部"（后端补的）—— 读不到在册名单时说一句，
  //   免得门店以为"我们店就这几个人"（用户 2026-09-21 提过这条）。
  if (d.roster_source === 'none' && d.roster_error) {
    toast('在册名单没读到（' + d.roster_error + '）—— 这里只列了有数据的人', 'bad');
  }

  // ⚠⚠ 2026-09-20（用户连说三次"还是不齐"之后的最终做法）：
  //   **不再嵌第二张表**，把成员行**直接插进主表**（同一个 `<table>` 里）。
  //   嵌套表的列宽是**两套算法**，无论怎么配 CSS 都可能差几个像素
  //   （补空格子凑列数 / `table-layout: fixed` / 逐列定宽，我都试过，还在差）。
  //   插进同一张表之后，列由浏览器**同一套算法**算 ⇒ 从构造上不可能不齐。
  //   布局照主表：**(区域空格) | 成员 | 达成·目标 标签 | 各产品列 | (末列留空)**；
  //   末行是**目标合计**（用户：达成不合计）。
  //   ⚠ 2026-09-22 补了开头那个空格子：原来成员行比主表**少一列**（人名占了区域列），
  //     标签列和产品列整体左移一位 ⇒ 门店列（col2）和标签列（col3）没法分开调宽。
  //     补齐后两套行列位一一对应，门店列加宽才不会挤到产品列。
  const ncols = (((attainLast || {}).columns) || []).length + 4;
  // 保存时要按名字回填 —— 记下这一次的名单和期间（见 `splitMembers` / `splitPeriod`）
  splitMembers = (d.members || []).map((m) => m.name);
  splitPeriod = d.period || (attainLast || {}).period || '';
  const frag = document.createDocumentFragment();
  d.members.forEach((m, mi) => {
    const row = document.createElement('tr');
    row.className = 'attain-detail-row';
    row.dataset.detail = tag;
    const tds = ['<td></td>',                   // 区域列占位（跟主表对齐）
                 '<td>' + esc(m.name) + '</td>',
                 '<td><span class="attain-pair"><span class="hint">达成</span>'
                 + '<span class="attain-sep attain-sep-ghost"></span>'
                 + '<span class="hint">目标</span></span></td>'];
    (d.columns || []).forEach((_c, i) => {
      const t = (m.targets || [])[i] || 0, a = (m.actuals || [])[i] || 0;
      // ⚠ 用户 2026-09-20：「目标这个框，**能不能点击的时候才能改**，
      //   现在这么静态都这样太丑了」⇒ 平时就是**跟"达成"一样的数字**（没有任何框），
      //   **点一下**才变成输入框；失焦/回车变回数字，Esc 放弃。
      const tgt = d.can_edit === false
        ? '<span class="attain-num">' + t + '</span>'
        : '<span class="split-edit" data-m="' + mi + '" data-c="' + i + '"'
          + ' data-v="' + t + '" title="点一下改这个数">' + t + '</span>';
      tds.push('<td><span class="attain-cell"><span class="attain-pair">'
        + '<span class="attain-num">' + a + '</span>'
        + '<span class="attain-sep attain-sep-ghost"></span>' + tgt
        + '</span></span></td>');
    });
    tds.push('<td></td>');
    row.innerHTML = tds.join('');
    frag.appendChild(row);
  });
  const total = document.createElement('tr');
  total.className = 'attain-detail-row attain-detail-total';
  total.dataset.detail = tag;
  // ⚠ colspan=3：区域 + 门店 + 标签 三列合一（成员行补齐区域占位后跟着改）
  const tds = ['<td colspan="3">合计</td>'];
  (d.columns || []).forEach((_c, i) => {
    const t = d.members.reduce((x, m) => x + (((m.targets || [])[i]) || 0), 0);
    // ⚠⚠ 2026-09-20（用户：「**最下面合计还没对齐**」）：
    //   合计**合计的就是目标** ⇒ 让它**坐在"目标"那一行**上（上面留一个同样高度的空行）。
    //   这样它跟上面每个人的目标数字**同一个位置**，不用再去调像素 ——
    //   之前它是"单独一个数字"（没有上面那行占位），`.attain-pair` 居中之后位置自然不同。
    // ⚠ 2026-09-20（用户：「**上面空着**」）—— 之前为了让合计"坐在目标那一行"，
    //   上面留了一个空行占位，看着就是一片空白。⇒ 改成**单行紧凑**：
    //   只放数字，但仍然居中在**同一个宽盒子**（`.num-box` 56px）里 ⇒ **横向位置不变**。
    tds.push('<td><span class="attain-cell">'
             + '<b class="attain-num num-box">' + t + '</b></span></td>');
  });
  tds.push('<td></td>');
  total.innerHTML = tds.join('');
  frag.appendChild(total);
  // ⭐ 2026-09-21（M21）：**这份目标是谁给的、什么时候给的** —— 必须写出来
  //   （四·八验收 6）。区长/平台那边看到的目标**不是本机填的**，是店长发邮件来的：
  //   ⚠ 不写来源的话，区长会以为"这是系统算的"，而它其实是店长分到人头上的活。
  const src = d.source;
  if (src && src.kind === 'mail') {
    const row = document.createElement('tr');
    row.className = 'attain-detail-row';
    row.dataset.detail = tag;
    row.innerHTML = '<td colspan="' + ncols + '"><span class="hint">'
      + '📧 这份目标是 <b>' + esc(src.store_code || '') + '</b> 店发来的邮件里的'
      + (src.imported_at ? '（' + esc(src.imported_at) + ' 收到）' : '')
      + (src.from ? ' · 发件人：' + esc(src.from) : '')
      + (src.unverified ? ' · ⚠ 发件人身份<b>没核实</b>（包里没带云商登录名）' : '')
      + '　—— 只有店长能改，这边只读。</span></td>';
    frag.appendChild(row);
  }
  if (d.can_edit !== false && operationAllowed('attain', 'modify')) {
    const save = document.createElement('tr');
    save.className = 'attain-detail-row attain-detail-save';
    save.dataset.detail = tag;
    // ⚠ 两个按钮（用户 2026-09-20）：「门店设定好目标有个**保存**，还要有个**发送**按钮，
    //   把拆好的目标发送给区长的邮箱」—— 保存只管存，发送才发邮件。
    save.innerHTML = '<td colspan="' + ncols + '">'
      + '<button class="btn primary small" data-op="modify" data-op-page="attain" data-detail-save="' + esc(store) + '">保存目标</button>'
      + ' <button class="btn ghost small" data-op="modify" data-op-page="attain" data-detail-send="' + esc(store) + '">'
      + '发送给区长</button>'
      + '<span class="hint" data-detail-msg></span></td>';
    frag.appendChild(save);
  }
  tr.parentElement.insertBefore(frag, tr.nextSibling);
  const inserted = Array.from(document.querySelectorAll('[data-detail="' + tag + '"]'));
  applyOperationVisibility(tr.parentElement);
  await attainAnimate(inserted, 'grow');
}

/** 展开区的**合计行**跟着上面的目标数实时刷新（用户 2026-09-20：
 *  「最下面的合计，根据上面个人目标设定**可以实时刷新**」）。
 *
 *  ⚠ 只在**这一家店**的行里算（按 `data-detail` 标记认），别把别家店的值混进来。
 *  ⚠ 两种形态都要读：`.split-edit`（data-v，没在编辑）和 `.split-in`（value，正在编辑）。
 */
function refreshDetailTotal(tag) {
  const rows = Array.from(document.querySelectorAll('[data-detail="' + tag + '"]'));
  const total = rows.filter((r) => r.classList.contains('attain-detail-total'))[0];
  if (!total) return;
  const members = rows.filter((r) => !r.classList.contains('attain-detail-total')
                                 && !r.classList.contains('attain-detail-save'));
  // 合计行第一格是 `colspan=3`（区域+门店+标签），产品从 children[1] 起；
  // 成员行是 [区域空, 人名, 标签, 产品…]，产品从 children[3] 起 ⇒ 差 2 格：`c + 2`
  for (let c = 1; c < total.children.length - 1; c++) {
    let sum = 0;
    members.forEach((r) => {
      const td = r.children[c + 2];
      const el = td && td.querySelector('.split-edit, .split-in');
      if (el) sum += Number((el.classList.contains('split-edit') ? el.dataset.v : el.value) || 0);
    });
    const b = total.children[c] && total.children[c].querySelector('.attain-num');
    if (b) b.textContent = String(sum);
  }
}

// 「目标」那个数：**点一下才变成输入框**（用户 2026-09-20 要的）
document.addEventListener('click', (e) => {
  if (attainActivePage !== 'attain') return;
  const sp = e.target.closest && e.target.closest('.split-edit');
  if (!sp || sp.classList.contains('on')) return;
  const inp = document.createElement('input');
  inp.className = 'split-in';
  inp.type = 'number';
  inp.min = '0';
  inp.step = '1';
  inp.value = sp.dataset.v || '0';
  inp.dataset.m = sp.dataset.m;
  inp.dataset.c = sp.dataset.c;
  // ⚠ 展开区那一行**现在就拿**（`sp` 还在 DOM 里）—— 合计要跟着这个标记找。
  //   见 `back()` 里那段：换掉之后再回头问节点"你在哪一行"是拿不到的。
  const rowTag = (() => {
    const tr = sp.closest('tr');
    return (tr && tr.dataset.detail) || '';
  })();
  sp.classList.add('on');
  sp.replaceWith(inp);
  attainCell = inp;                 // ⚠ 记住落点：方向键要从这儿走
  inp.focus();
  inp.select();
  // ⚠⚠ **行必须在 `replaceWith` 之前拿**（2026-09-20 用户报的
  //   「周度这个合计的数**还是不会根据目标拆分改变**」就是这个）：
  //   `inp.replaceWith(span)` 之后 `inp` 已经**脱离 DOM**，
  //   而 `closest('tr')` 是"往上找祖先" —— 脱离的节点没有祖先 ⇒ 返回 `null`
  //   ⇒ `tag` 成了空串 ⇒ `refreshDetailTotal` 压根没被调用（合计一直停在旧值）。
  //   ⚠ 这类"先换掉节点、再回头问这个节点它在哪"的写法**不会报错**，只会静默不生效。
  const back = (keep) => {
    const v = Math.max(0, Number(inp.value || 0) || 0);
    const span = document.createElement('span');
    span.className = 'split-edit';
    span.dataset.m = inp.dataset.m;
    span.dataset.c = inp.dataset.c;
    span.dataset.v = String(keep ? v : Number(sp.dataset.v || 0));
    span.title = '点一下改这个数';
    span.textContent = span.dataset.v;
    inp.replaceWith(span);
    if (rowTag) refreshDetailTotal(rowTag);    // ⚠ 落定后再刷一次（Esc 那下要退回去）
  };
  // 回车 = 落定；Esc = 放弃（回到原来的数）—— 两条都靠 blur 收尾，
  // 所以"放不放弃"要用一个标记带过去（`back(keep)` 本来就有这个参数）。
  let cancel = false;
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { cancel = true; inp.blur(); }
    else if (e.key === 'Enter') { e.preventDefault(); inp.blur(); }
  });
  inp.addEventListener('blur', () => back(!cancel));
  // ⚠ 「实时」是**边打边刷**（用户 2026-09-20：「最下面的合计，根据上面个人目标设定
  //   **可以实时刷新**」）—— 只在落定时刷的话，敲数字的时候合计不动，看着像没生效。
  //   `input` 事件连原生那种上下小箭头也覆盖得到。
  inp.addEventListener('input', () => { if (rowTag) refreshDetailTotal(rowTag); });
  // 方向键统一在**文档级**处理（见下面 `attainKeyMove`）——
  // ⚠ 只在输入框上绑的话，"没点开这一格"时按键就没有落点（用户实测："还是不会切换"）。

});

/** 方向键在目标格之间跳（用户 2026-09-20：「**方向键上下左右切换**上下左右的目标格子」）。
 *
 *  ⚠ 放在**文档级**（不是绑在输入框上）：绑输入框只有在"正在编辑"时才收得到键，
 *    用户没点开那一格时按方向键就"没反应"。这里以 `attainCell`（点过或正在编辑的那格）
 *    为落点，两种情况都能走。
 *  ⚠ 上下 = 换人（同一列），左右 = 换产品列（同一人）。
 */
document.addEventListener('keydown', (e) => {
  if (attainActivePage !== 'attain') return;
  // ⚠ 键名兼容两种写法：现代浏览器是 `ArrowLeft`，老的 Edge/Safari 报 `Left`。
  const K = { ArrowUp: 'up', Up: 'up', ArrowDown: 'down', Down: 'down',
              ArrowLeft: 'left', Left: 'left', ArrowRight: 'right', Right: 'right' };
  const dir = K[e.key];
  if (!dir) return;
  const ae = document.activeElement;
  const el = (ae && ae.classList && ae.classList.contains('split-in')) ? ae : attainCell;
  if (!el || !el.closest) return;
  const tr0 = el.closest('tr');
  if (!tr0 || !tr0.dataset.detail) return;              // 只在展开区里管
  e.preventDefault();

  // ⚠ **按 DOM 位置走**（不再依赖 `data-m/data-c`）：从"当前这个 `<td>`"出发，
  //   左右就是同一行的前后一格、上下就是同一列的前后一行。
  //   （用户报："上下调好了，但是左右还不行" —— 换成按格子找，两种情况一起解决。）
  const td0 = el.closest('td');
  const ci = td0.cellIndex + ({ left: -1, right: 1 }[dir] || 0);
  const rows = Array.from(document.querySelectorAll('[data-detail="' + tr0.dataset.detail + '"]'))
    .filter((r) => !r.classList.contains('attain-detail-total')
                   && !r.classList.contains('attain-detail-save'));
  const ri = rows.indexOf(tr0) + ({ up: -1, down: 1 }[dir] || 0);
  if (el.classList.contains('split-in')) el.blur();      // 先把当前这个数落定
  const row = rows[ri];
  if (!row || ci < 3) return;                            // 第 3 格起才是产品列（0=区域 1=人名 2=标签）
  const td = row.children[ci];
  const sp2 = td && td.querySelector('.split-edit');
  if (sp2) setTimeout(() => sp2.click(), 0);             // 下一帧再跳（否则焦点被丢）
});

document.addEventListener('click', (e) => {
  if (attainActivePage !== 'attain') return;
  const reg = e.target.closest && e.target.closest('[data-attain-region]');
  if (reg) {
    toggleAttainRegion(decodeURIComponent(reg.dataset.attainRegion));
    return;
  }
  const btn = e.target.closest && e.target.closest('[data-expand]');
  if (btn) toggleStoreDetail(btn);
});

/* ⚠⚠ 2026-09-21：「保存目标 / 发送给区长」这两个按钮**一直没接线** ——
   按钮画出来了、点了什么都不发生（用户问「发送到区长这个功能做好了吗」才发现）。
   后端两条接口都是好的（`PUT /api/attain/split` / `POST /api/attain/split/send`），
   差的只是这两段。

   拆成两个按钮是用户 2026-09-20 定的：「门店设定好目标有个**保存**，
   还要有个**发送**按钮，把拆好的目标发送给区长的邮箱」——
   **保存只管保存**（想先存档、回头再发），发送才发邮件。 */

/** 把展开区里**当前**填的目标收集成 `{成员: [每列台量…]}`。 */
function collectSplitTargets() {
  const get = (el) => (el.classList.contains('split-edit') ? el.dataset.v : el.value);
  const out = {};
  document.querySelectorAll('[data-detail] [data-m][data-c]').forEach((el) => {
    const mi = Number(el.dataset.m), ci = Number(el.dataset.c);
    const who = splitMembers[mi];
    if (!who || !(ci >= 0)) return;
    if (!out[who]) out[who] = [];
    out[who][ci] = Number(get(el) || 0);
  });
  // ⚠ 后端要的是**定长数组**（跟产品列数一致）—— 中间没填的补 0，
  //   不然 `set_targets` 会按"短一截"截断，出现"填了却没存上"。
  const n = (((attainLast || {}).columns) || []).length;
  Object.keys(out).forEach((who) => {
    for (let i = 0; i < n; i++) if (out[who][i] == null) out[who][i] = 0;
    out[who] = out[who].slice(0, n);
  });
  return out;
}

function splitMsg(btn, text, bad) {
  const box = btn.closest('tr') && btn.closest('tr').querySelector('[data-detail-msg]');
  if (box) box.innerHTML = bad ? `<span style="color:var(--bad)">${esc(text)}</span>`
                              : `<span style="color:var(--ok)">${esc(text)}</span>`;
}

document.addEventListener('click', async (e) => {
  if (attainActivePage !== 'attain') return;
  const save = e.target.closest && e.target.closest('[data-detail-save]');
  if (save) {
    const store = save.dataset.detailSave;
    save.disabled = true;
    try {
      const r = await api('/api/attain/split', {
        method: 'PUT',
        body: { store, period: splitPeriod, targets: collectSplitTargets() } });
      splitMsg(save, r.message || '已保存');
      toast(r.message || '已保存', 'ok');
      // 合计/差额跟着变 —— **收起再展开**（按最新数据重画这一家店）：
      //   ⚠ 要 `await`：`toggleStoreDetail` 是异步的，连点两下会在第一趟还没
      //     插完行时就开始第二趟（两边都去拉接口、还可能插重）。
      //   ⚠ 按 `dataset` 找按钮，别拼 `[data-expand="店名"]` 选择器 ——
      //     店名里有引号/中文都得转义，`CSS.escape` 在老 Edge 上还没有。
      const head = Array.from(document.querySelectorAll('[data-expand]'))
        .filter((b) => b.dataset.expand === store)[0];
      if (head) {
        await toggleStoreDetail(head);      // 收起
        await toggleStoreDetail(head);      // 重新展开（拿到新的合计/差额）
      }
    } catch (err) {
      splitMsg(save, '保存失败：' + err.message, true);
      toast('保存失败：' + err.message, 'bad');
    } finally {
      save.disabled = false;
    }
    return;
  }

  const send = e.target.closest && e.target.closest('[data-detail-send]');
  if (send) {
    const store = send.dataset.detailSend;
    send.disabled = true;
    splitMsg(send, '正在发…');
    try {
      const r = await api('/api/attain/split/send', {
        method: 'POST', body: { store, period: splitPeriod } });
      // ⚠ 「没发出去」不是错误：目标已经存好了，邮件只是投递方式 ——
      //   所以有话直说（谁没配邮箱 / 邮件没开），别报成"失败"。
      if (r.sent) splitMsg(send, r.message || ('已发给 ' + (r.to || []).join('、')));
      else splitMsg(send, r.why || '没发出去', true);
      toast(r.sent ? (r.message || '已发送') : ('没发出去：' + (r.why || '？')),
            r.sent ? 'ok' : 'bad');
    } catch (err) {
      splitMsg(send, '发送失败：' + err.message, true);
      toast('发送失败：' + err.message, 'bad');
    } finally {
      send.disabled = false;
    }
  }
});


/* 页面刷新按钮在页面生命周期入口处统一接线。 */

$('#btn-export-attain')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#attain-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/attain/export', { method: 'POST', body: {} });
    const url = '/api/export/download?name=' + encodeURIComponent(r.file);
    triggerDownload(url, r.file);                 // ⭐ 直接给浏览器，别让人去 out/ 里找
    if (box) {
      box.hidden = false;
      box.innerHTML = `已下载 <b>${esc(r.file)}</b>（${r.rows} 行 · ${(r.sheets || []).length} 张表）`
        + ` —— 没弹出保存框的话，去浏览器<b>「下载」</b>里找　`
        + `<a class="btn ghost small" href="${url}">再下一次</a>`
        + `<span class="hint">（服务端也留了一份：<code>${esc(r.rel)}</code>）</span>`;
    }
    toast('已导出，正在下载 ' + r.file, 'ok');
  } catch (err) {
    if (box) { box.hidden = false; box.textContent = '导出失败：' + err.message; }
    toast('导出失败：' + err.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
});

// 「标色」开关：**原地重画**（不重新请求接口 —— 数据没变，只是换底色）
(() => {
  const box = $('#attain-color');
  if (!box) return;
  box.checked = attainColorOn;
  box.addEventListener('change', () => {
    attainColorOn = !!box.checked;
    try { localStorage.setItem('cbg-attain-color', attainColorOn ? '1' : '0'); } catch (e) {}
    if (attainLast) renderAttain(attainLast);
  });
})();



function refreshAttain(button) {
  return refreshWithFetch('attain', button, () => loadAttain());
}

function refreshAttainHistory() {
  attainHistCur = '';
  return loadAttainHistory();
}

registerPage('attain', {
  mount: () => mountAttainPage('attain'),
  load: loadAttain,
  unmount: unmountAttainPage,
  refresh: refreshAttain,
});

registerPage('attain-history', {
  mount: () => mountAttainPage('attain-history'),
  load: loadAttainHistory,
  unmount: unmountAttainPage,
  refresh: refreshAttainHistory,
});

$('#btn-refresh-attain')?.addEventListener('click', (e) =>
  refreshPage('attain', e.currentTarget));
$('#btn-refresh-attain-hist')?.addEventListener('click', () =>
  refreshPage('attain-history'));
