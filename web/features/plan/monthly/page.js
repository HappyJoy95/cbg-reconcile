'use strict';

/* 月度生意计划只读看板。统计范围和业务口径由后端按当前身份处理。 */
let planPageMounted = false;
let planReadController = null;
let planPageEpoch = 0;
const planAnimationFrames = new Set();
const planAnimationTimers = new Set();

function planRequestFrame(callback) {
  let frame = null;
  frame = requestAnimationFrame(() => {
    planAnimationFrames.delete(frame);
    if (planPageMounted) callback();
  });
  planAnimationFrames.add(frame);
  return frame;
}

function planScheduleTimer(callback, delay) {
  let timer = null;
  timer = setTimeout(() => {
    planAnimationTimers.delete(timer);
    if (planPageMounted) callback();
  }, delay);
  planAnimationTimers.add(timer);
  return timer;
}

/* ═════════════════════ 月度生意计划（M22）═════════════════════

   用户 2026-09-21：「设计一个新模块，叫做**月度生意计划**，包含折叠机、FD、ND、
   穿戴、音频、平板、电脑几个大块，每个产品系列分开，统计每个店**当月和上个月同期**的
   销量、销售额、利润和环比升降」，随后补：「做个合并的，**点击列名可以展开**显示
   几个产品线」+「**三级也做啊，也做**」。

   ⚠ 交互是**列展开**（不是行展开）：行永远是"门店"，**点列名**就地展开成它下面的
     产品线 —— 块 → 系列 → 机型，三层。
   ⚠ 展开状态和当前指标记在 localStorage：点了展开再刷新，不该又合上。
   ⚠ 这一页**只读** `out/plan-<年>.json`（后端算好的那份）—— 看板不读数据库。
   ⚠ 数据范围是后端给的（门店=本店 / 区长=所辖 / 平台=名单内 28 家），
     前端**不参与筛选**，`d.rows` 就是该看的那几家。 */

const PLAN_KEY = 'cbg-plan';
const PLAN_METRICS = { qty: '销量', amount: '销售额', profit: '利润' };
//: 层级路径的分隔符 —— **别用 `/`**（产品名里可能有），用一个数据里绝不会出现的控制字符
const PLAN_SEP = '\u0001';

//: `open` = 展开的产品线（列）；`openStores` = **展开了"到人"的门店**（行）——
//: 用户 2026-09-21：「门店名点开可以**拆分到人**」。
//: `collapsedRegions` = 收成「区域汇总」的区（点区域名切换；默认空 = 只列门店）
const planState = { data: null, metric: 'amount', open: new Set(), openStores: new Set(),
                    collapsedRegions: new Set(),
                    //: 动画期：`{grow, shrink, extra, phase}`（null = 没在动画）
                    anim: null,
                    //: 上一次画出来的**列 path**（字符串数组）—— 算"这次新加了哪几列"
                    lastCols: [] };

(function planRestore() {
  try {
    const raw = localStorage.getItem(PLAN_KEY);
    if (!raw) return;
    const o = JSON.parse(raw) || {};
    if (PLAN_METRICS[o.metric]) planState.metric = o.metric;
    planState.open = new Set(o.open || []);
    planState.openStores = new Set(o.openStores || []);
    planState.collapsedRegions = new Set(o.collapsedRegions || []);
  } catch (e) { /* 隐私模式 / 坏数据：退回默认值，别让整页挂掉 */ }
})();

function planPersist() {
  try {
    localStorage.setItem(PLAN_KEY, JSON.stringify({
      metric: planState.metric, open: [...planState.open],
      openStores: [...planState.openStores],
      collapsedRegions: [...planState.collapsedRegions] }));
  } catch (e) { /* 同上 */ }
}

/** 区域名 → 可点按钮（`collapsed` = 现在收着，点了要展开）。 */
function planRegionBtn(region, collapsed) {
  return '<button type="button" class="region-toggle"'
    + ' data-plan-region="' + esc(encodeURIComponent(region)) + '"'
    + ' title="点一下' + (collapsed ? '展开门店明细' : '合并为区域汇总') + '">'
    + (collapsed ? '▸' : '▾') + ' ' + esc(region) + '</button>';
}

function planToggleRegion(region) {
  if (!planState.data) return;
  if (planState.collapsedRegions.has(region)) planState.collapsedRegions.delete(region);
  else planState.collapsedRegions.add(region);
  planPersist();
  renderPlan(planState.data);
}

// 值：台量取整；金额/利润上万折成「万」（扫一眼要的是量级，不是分）
function planFmt(v, metric) {
  const n = Number(v);
  if (v == null || !isFinite(n)) return '—';
  if (metric === 'qty') return String(Math.round(n));
  if (Math.abs(n) >= 10000) return (n / 10000).toFixed(1) + '万';
  return String(Math.round(n));
}

// 环比：**五种形态各有各的写法**（后端给的 kind，前端不许自己拿 rate 判）
//   up/down/flat = 有百分比；new = 上月 0（「新增」）；zero = 两边都是 0；
//   bare = 上月是负数（"增长率"方向是反的 ⇒ 不给百分比）。
function planGrowthHtml(g) {
  const kind = (g && g.kind) || 'zero';
  const rate = g ? g.rate : null;
  if (kind === 'new') return '<span class="plan-g plan-new">新增</span>';
  if (kind === 'zero') return '<span class="plan-g plan-flat">—</span>';
  if (kind === 'bare') return '<span class="plan-g plan-flat" title="上月是负数（退货多于销售），不给百分比">—</span>';
  if (rate == null) return '<span class="plan-g plan-flat">—</span>';
  // ⚠ **取绝对值**：`rate` 是负数（掉）时，`↓` 和那个负号会一起显示成
  //   `↓-12.8%`（实测第一版就是这样，看着像打错了）。方向由箭头表达，数字只给大小。
  const pct = Math.abs(rate * 100).toFixed(1) + '%';
  if (kind === 'up') return '<span class="plan-g plan-up">↑' + pct + '</span>';
  if (kind === 'down') return '<span class="plan-g plan-down">↓' + pct + '</span>';
  return '<span class="plan-g plan-flat">→' + pct + '</span>';
}

// 路径 → 那一格的节点（块 / 系列 / 机型三层，按需要往下走）
function planNode(row, path) {
  const parts = path.split(PLAN_SEP);
  let node = (row.blocks || []).find((b) => b.name === parts[0]);
  if (node && parts[1]) node = (node.series || []).find((x) => x.name === parts[1]);
  if (node && parts[2]) node = (node.models || []).find((x) => x.name === parts[2]);
  return node || null;
}

// 列树：七块 → 系列 → 机型。**各店卖的东西不一样**，所以列要用所有店的**并集**
// （某店没卖的系列，它那一格显示「—」，而不是那一列在它这行消失）。
function planSeriesOf(d, block) {
  const series = new Map();
  (d.rows || []).forEach((r) => {
    const b = (r.blocks || []).find((x) => x.name === block);
    ((b && b.series) || []).forEach((s) => {
      if (!series.has(s.name)) series.set(s.name, new Set());
      const models = series.get(s.name);
      (s.models || []).forEach((m) => models.add(m.name));
    });
  });
  return [...series.entries()].map(([sn, models]) => ({
    name: sn, path: block + PLAN_SEP + sn,
    children: [...models].map((mn) => ({
      name: mn, path: block + PLAN_SEP + sn + PLAN_SEP + mn, children: [] })),
  }));
}

function planTreeOf(d) {
  return (d.blocks || []).map((b) => ({ name: b, path: b, children: planSeriesOf(d, b) }));
}

/* 把树摊成**一行里的列**：每个节点（一级块 / 二级系列 / 三级机型）**自己永远占一列**，
   展开了就把子节点**插在它右边**（还是同一行）。

   ⚠ 2026-09-21 用户：「**二级分类和三级分类和一级分类同一行**」——
     原来是**多行表头**（一级那格横跨它下面的几列），他不认这个形态。
   ⚠ 组自己那一列**照旧显示它自己的合计**（块总计 / 系列小计）——
     所以"合计 = 七个块相加"这条口径没变：**别把展开出来的子列再加一遍**。
   ⚠ 判别"展开没展开"只看 `planState.open`（记在 localStorage 里，刷新不丢）。 */
function planLayout(d, extraOpen) {
  const cols = [];
  const isOpen = (p) => planState.open.has(p) || !!(extraOpen && extraOpen.has(p));
  const walk = (nodes, depth) => (nodes || []).forEach((n) => {
    const kids = n.children || [];
    const open = isOpen(n.path);
    cols.push({ name: n.name, path: n.path, depth,
                hasKids: kids.length > 0, open: planState.open.has(n.path) });
    if (kids.length && open) walk(kids, depth + 1);
  });
  walk(planTreeOf(d), 0);
  return { cols };
}

/* 表头 —— **永远只有一行**：门店 | 一级 | 二级… | 三级… | 合计。

   ⚠ 一级/二级/三级都摊在同一行里，组名后面挂 ▸/▾ 点开点关（用户 2026-09-21 定的形态）。
   ⚠ 首尾两格（门店 / 合计）**必须有** —— 少了整张表会错位一格（第一版就栽在这）。
   ⚠ 组自己那格（depth===0）加粗一档：它是这一组的小计，跟明细要分得开。 */
/* 列宽 + 动画相位 → class（宽度只有 `.plan-col*` 那一处定义，见 `style.css`）
   ⚠ 这里只打**标记**（`plan-anim-grow/shrink`），**不在这里把它压成 0 宽** ——
     0 宽那一步要等"自然宽度量完"才能做（见 `planFreeze`）。 */
function planColCls(path) {
  const a = planState.anim;
  let cls = ' plan-col';
  if (!a) return cls;
  if (a.grow.has(path)) cls += ' plan-anim-grow';
  if (a.shrink.has(path)) cls += ' plan-anim-shrink';
  return cls;
}

/* 冻结列宽 —— **"列宽固定 + 往右推的动画"能不能成立，全靠这一步**。

   ⚠⚠ 踩过（2026-09-21，两轮）：`.plan-col-hide { width: 0 }` 写对了、class 也确实挂上了，
     但量出来那些"0 宽"的列是 **101~103px** —— 表是 `width: max-content`，
     它的 max-content **仍然算上了那几个格子的文字**，多出来的宽度又被按比例摊回列上。
     结果：新展开的列**点完就已经是终态**，过渡根本没有起点，
     看着和"没写动画"一模一样（第一轮就这么白折腾了一回）。
   ⇒ 三步，全在同一帧里做完（浏览器只画最后一帧，中间那步不会闪）：
     ① **量自然宽度**：此刻所有列都在 DOM 里、且都是满宽（收起的那几列靠 `planLayout`
        的 `extraOpen` 留在 DOM），量到的就是每列"本来多宽" ——
        ⚠ 别拿 class 里的 108 当准：表头文字长的列会自己宽一点（实测有过 125、门店列 146）。
     ② **写死起点**：新展开的列 → 0；表和**每个格**都写显式像素 ⇒ 列宽之和 == 表宽，
        浏览器没有余量可摊。
     ③ 第二帧（`planAnimStep`）把显式宽度换成**终点**宽度（要收的列 → 0）。
        两张过渡（表宽 / 列宽）同一条曲线、同一个时长 ⇒ 严格同步，中间不会多出或少掉一截。
   ⚠ 收尾**不再重画整张表**（原来那样）：收起来的列直接 `remove()`（见 `planClean`）——
     弱机器上一次重画就是一次看得见的卡顿（用户 2026-09-21：「配置不好的看着卡」）。
   ⚠⚠ 冻的那一帧**必须把过渡关掉**（`.plan-frozen`）：为了量自然宽度得先读一次
     `getBoundingClientRect()`，这一读就让这些格子有了"计算样式"，接下来写 0 宽
     **自己就会走一遍过渡** —— 第二帧再改终点，起点已经走掉一半了
     （实测：展开时新列一上来就是 ~110 宽，等于没有动画；收起那一侧反而正常，
      因为"要收的列"起点终点都是自然宽度，压根没变）。 */
function planFreeze(tbl, anim) {
  tbl.classList.add('plan-frozen');                 // 关过渡：这一帧的宽度变化不该被看见
  const head = Array.from(tbl.querySelectorAll('thead tr')[0].children);
  const nat = head.map((c) => Math.round(c.getBoundingClientRect().width));
  anim.growIdx = []; anim.shrinkIdx = [];
  head.forEach((c, i) => {
    if (c.classList.contains('plan-anim-grow')) anim.growIdx.push(i);
    else if (c.classList.contains('plan-anim-shrink')) anim.shrinkIdx.push(i);
  });
  const w0 = nat.slice(), w1 = nat.slice();
  anim.growIdx.forEach((i) => { w0[i] = 0; });
  anim.shrinkIdx.forEach((i) => { w1[i] = 0; });
  anim.w1 = w1;
  anim.pin0 = w0.reduce((a, b) => a + b, 0);
  anim.pin1 = w1.reduce((a, b) => a + b, 0);
  tbl.style.width = anim.pin0 + 'px';
  head.forEach((c, i) => { c.style.width = w0[i] + 'px'; });
  planColHide(tbl, anim.growIdx, []);
}

/* 逐列挂 / 摘 `.plan-col-hide` —— **所有行都要挂，表体不能漏**。

   ⚠ 那个类的 `padding-left/right: 0` 是"宽度真能到 0"的另一半：
     `box-sizing: border-box` 下 `width: 0` 压不掉那 20px 内边距，列宽会停在 20px。
   ⚠ 表体也要挂：列宽只有**第一行**说了算（`table-layout: fixed`），
     但表体格的 20px 内边距会**溢到隔壁列**上去（底色、下边框跟着跑）。 */
function planColHide(tbl, add, del) {
  if (!add.length && !del.length) return;
  Array.from(tbl.querySelectorAll('tr')).forEach((tr) => {
    add.forEach((i) => { const c = tr.children[i]; if (c) c.classList.add('plan-col-hide'); });
    del.forEach((i) => { const c = tr.children[i]; if (c) c.classList.remove('plan-col-hide'); });
  });
}

/* ─────────────── 门店 → 人 那几行的**上下动画**（用户 2026-09-21：「上下的动画加上」）

   ⚠⚠ `tr` 的 `height` **过渡不了**（实测：给它写 `height: 0`，行高照样 53px）——
     表格行高是**内容顶出来的**，`height` 只是个下限。
     ⇒ 高度压在每个格子里那层 `.plan-in` 上（那层的高度才是"内容高度"），
       格子自己的上下内边距也要一起压（见 `style.css` 的 `.plan-row-hide`）。
   ⚠ 跟列宽同一套三步：量自然高度 → 写死起点 → 第二帧换成终点。
     起点：展开 = 0（长出来）、收起 = 自然高（缩回去，收尾才真从 DOM 里摘掉）。 */
function planRowsFreeze(tbl, anim) {
  const rows = Array.from(tbl.querySelectorAll('tr.plan-anim-row'));
  if (!rows.length) return;
  tbl.classList.add('plan-frozen');
  // 一行里最高那个格子的内容高度 = 这一行的"内容高度"（行高 = 它 + 上下内边距）
  anim.rowNat = rows.map((r) => Math.max.apply(null, Array.from(r.children).map((c) => {
    const d = c.querySelector('.plan-in');
    return d ? Math.round(d.getBoundingClientRect().height) : 0;
  })));
  if (anim.rows.dir === 'grow') planRowsApply(rows, anim.rowNat.map(() => 0));
}

/* 给这几行写高度：`0` 时连内边距一起压掉（不然那 16px 还在） */
function planRowsApply(rows, heights) {
  rows.forEach((r, i) => {
    Array.from(r.children).forEach((c) => {
      const d = c.querySelector('.plan-in');
      if (d) d.style.height = heights[i] + 'px';
      c.classList.toggle('plan-row-hide', heights[i] === 0);
    });
  });
}

function planRowHeights(a, rows) {
  return a.rows.dir === 'grow' ? a.rowNat : rows.map(() => 0);
}

/* 第二帧：**只改尺寸和 class，绝对不要重画**。

   ⚠⚠ 踩过：第一版在第二帧又 `renderPlan()` 了一遍 —— 重画是**换元素**，
     新元素一上来就是终态宽度，浏览器没有"从 0 到 108"这回事，
     于是新列是**蹦**出来的（实测：点完立刻量，新列已经是 w108）。
     ⇒ 起点（0 宽）先真的进 DOM，第二帧只把显式值换成终点，
       同一个元素才会走 CSS 过渡。 */
function planAnimStep() {
  const a = planState.anim;
  if (!a) return;
  a.phase = 1;
  const tbl = document.querySelector('#plan-table table');
  if (!tbl) return;
  // ⚠ 顺序不能动：先**开过渡**、读一次布局让起点落地（此时还是冻结那份尺寸），
  //   再写终点 —— 这样过渡才是"从起点到终点"，而不是从半路出发。
  tbl.classList.remove('plan-frozen');
  void tbl.offsetWidth;
  if (a.w1 != null) {
    tbl.style.width = a.pin1 + 'px';
    Array.from(tbl.querySelectorAll('thead tr')[0].children)
      .forEach((c, i) => { c.style.width = a.w1[i] + 'px'; });
    planColHide(tbl, a.shrinkIdx, a.growIdx);
  }
  if (a.rows) {
    const rows = Array.from(tbl.querySelectorAll('tr.plan-anim-row'));
    planRowsApply(rows, planRowHeights(a, rows));
  }
}

/* 收尾：**摘掉临时的 class / 内联尺寸，该消失的列和行直接 remove**。

   ⚠ 原来这里是"整表重画一次" —— 干净，但弱机器上就是一次可见的卡顿
     （2280 个格子重新 parse 一遍）。现在只动那几列 / 那几行。
   ⚠ 判据是"现在还压着的那几列 / 那几行"（`plan-col-hide` / `plan-row-hide`）——
     展开的那一侧第二帧就把 class 摘了，所以这里只会摘到**收起**的那些。
   ⚠ 行必须先摘（`tr`），列后摘：`children[i]` 的下标不能在中途变。 */
function planClean() {
  const tbl = document.querySelector('#plan-table table');
  if (!tbl) return;
  Array.from(tbl.querySelectorAll('tr.plan-anim-row')).forEach((r) => {
    if (r.querySelector('td.plan-row-hide')) { r.remove(); return; }   // 收起来的行：真没了
    r.classList.remove('plan-anim-row');
    Array.from(r.children).forEach((c) => {
      const d = c.querySelector('.plan-in');
      if (d) d.style.height = '';
    });
  });
  const head = Array.from(tbl.querySelectorAll('thead tr')[0].children);
  const drop = head.map((c, i) => (c.classList.contains('plan-col-hide') ? i : -1))
    .filter((i) => i >= 0);
  if (drop.length) {
    Array.from(tbl.querySelectorAll('tr')).forEach((tr) => {
      drop.slice().reverse().forEach((i) => { const c = tr.children[i]; if (c) c.remove(); });
    });
  }
  tbl.style.width = '';
  Array.from(tbl.querySelectorAll('thead tr')[0].children).forEach((c) => {
    c.style.width = '';
    c.classList.remove('plan-col-hide', 'plan-anim-grow', 'plan-anim-shrink');
  });
  Array.from(tbl.querySelectorAll('td.plan-col')).forEach((c) => {
    c.classList.remove('plan-col-hide', 'plan-anim-grow', 'plan-anim-shrink');
  });
}

/* 一次动效的**统一三步**（列宽 / 行高都走它）：
   ① 画起始态（`renderPlan` 里 `planFreeze` / `planRowsFreeze` 把尺寸冻住）
   ② 两层 rAF 之后放开到终点（只改尺寸和 class，不重画）
   ③ 到点收尾（`planClean`） */
//: 收尾要**晚于** `--motion-slow`（.34s）+ 两层 rAF 的等待（≈32ms），不然会把过渡掐在半路
const PLAN_ANIM_MS = 480;

function planAnimate(anim) {
  planState.anim = anim;
  renderPlan(planState.data);                                   // ① 起始态
  // ⚠⚠ **两层 rAF，不能省成一层**：一层的话"冻结态"和"终点态"落在**同一帧**里，
  //   浏览器只在帧末算一次样式，过渡的起点就变成"点之前那份自然宽度"（实测：新列
  //   一上来就是 108/109，看着像没动画）。两层 rAF 中间必然夹着一次绘制，
  //   起点（0 宽 / 0 高）真的被提交过，第二帧才从 0 开始长。
  planRequestFrame(() => planRequestFrame(() => {
    if (planState.anim !== anim) return;                        // 中途又点了一下：让新的那次接管
    planAnimStep();                                             // ② 过渡
  }));
  planScheduleTimer(() => {
    if (planState.anim !== anim) return;
    planState.anim = null;
    planClean();                                                // ③ 收尾（不重画）
  }, PLAN_ANIM_MS);
}

function planHeadHtml(cols) {
  const cells = cols.map((c) => {
    const cls = 'plan-th plan-lv' + Math.min(c.depth + 1, 3)
      + (c.depth === 0 ? ' plan-grp' : '') + (c.hasKids ? ' plan-th-open' : '')
      + planColCls(c.path);
    const caret = c.hasKids
      ? '<span class="plan-caret">' + (c.open ? '▾' : '▸') + '</span>' : '';
    const attr = c.hasKids
      ? ' data-plan-toggle="' + esc(encodeURIComponent(c.path)) + '"'
        + ' title="点一下' + (c.open ? '收起' : '展开') + '下一级"'
      : '';
    return '<th class="' + cls + '"' + attr + '>' + esc(c.name) + caret + '</th>';
  }).join('');
  return '<tr><th class="plan-th plan-lead plan-col">区域</th>'
    + '<th class="plan-th plan-lead plan-col plan-col-store">门店</th>' + cells
    + '<th class="plan-th plan-lead plan-col plan-col-total">合计</th></tr>';
}

function planCellHtml(row, col, wrap) {
  const node = planNode(row, col.path);
  const m = planState.metric;
  const cur = node ? (node.cur || {})[m] : 0;
  const neg = Number(cur) < 0;
  // ⚠ `wrap`：人那几行要做上下动画 ⇒ 内容包一层 `.plan-in`（行高压在它身上，
  //   因为 `tr` 的 `height` 过渡不了，见 `planRowsFreeze`）。别的行不用包。
  const inner = '<span class="plan-v">' + planFmt(cur, m) + '</span>'
    + planGrowthHtml(node ? (node.growth || {})[m] : null);
  return '<td class="plan-cell plan-lv' + Math.min(col.depth + 1, 3)
    + (col.depth === 0 ? ' plan-grp' : ' plan-sub')
    + planColCls(col.path)
    + (neg ? ' plan-neg' : '') + '">'
    + (wrap ? '<div class="plan-in">' + inner + '</div>' : inner) + '</td>';
}

function renderPlan(d) {
  const meta = $('#plan-meta'), box = $('#plan-table'), warn = $('#plan-warn');
  const unk = $('#plan-unknown');
  if (unk) unk.innerHTML = '';
  if (!d || !d.exists) {
    meta.textContent = '';
    warn.innerHTML = '';
    box.innerHTML = '<div class="empty">'
      + esc((d && (d.error || d.hint)) || '还没有数据') + '</div>';
    return;
  }
  planState.data = d;
  const cur = (d.period || {}).cur || {}, prev = (d.period || {}).prev || {};
  const gap = d.data_as_of && d.data_as_of !== cur.end
    ? '　⚠ 数据截至 ' + d.data_as_of : '';
  meta.textContent = '本月 ' + (cur.start || '?') + ' ~ ' + (cur.end || '?')
    + '　上月同期 ' + (prev.start || '?') + ' ~ ' + (prev.end || '?') + gap
    + (d.store_filter ? '　· 只看 ' + d.store_filter : '');

  // 顶部警示（上月数据不够 / 数据落后 / 多串号拆行…）—— **不许静默**，
  // 这几条正是"数看着很正常但其实有问题"的那种。
  warn.innerHTML = (d.warnings || []).length
    ? '<div class="banner warn" style="margin-bottom:10px">'
      + (d.warnings || []).map((w) => esc(w)).join('<br>') + '</div>' : '';

  // 指标切换按钮的选中态（渲染时同步一次，免得跟 localStorage 里那份不一致）
  $$('#plan-metric .seg-btn').forEach((b) => {
    const on = b.dataset.metric === planState.metric;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });

  const anim = planState.anim;
  // 「门店 → 人」的上下动画（用户 2026-09-21：「上下的动画加上」）：
  // ⚠ **收起**时那几行要留在 DOM 里才收得动 ⇒ 这家店这一帧临时当成展开的
  //   （跟列那边 `extraOpen` 是同一个套路）。
  const animRows = anim && anim.rows ? anim.rows : null;
  // ⚠ 动画期把"正在收起"的子列**留在 DOM 里**（父节点临时当成展开的）——
  //   不然它们下一帧就没了，宽度过渡直接落空（看着就是"啪一下"）。
  const { cols } = planLayout(d, anim ? anim.extra : null);
  const finalPaths = planLayout(d).cols.map((c) => c.path);
  // ⭐ 按**区域**排好序（用户 2026-09-22）—— 后端 `stores_in_scope` 已排，
  //   这里再兜一层：老 plan-*.json 快照 / 接口过滤后的 rows 也要同一口径。
  //   顺序跟增值 film 的 `REGION_ORDER` 一致；区内保持后端给的相对顺序。
  const PLAN_REGION_ORDER = ['西北区', '市区', '南区', '服务站', '北区', '西区'];
  const regions0 = d.regions || {};
  const rankOf = (store) => {
    const i = PLAN_REGION_ORDER.indexOf(normalizeRegionName(regions0[store]));
    return i < 0 ? 99 : i;
  };
  const rows = (d.rows || []).map((r, i) => [r, i])
    .sort((a, b) => rankOf(a[0].store) - rankOf(b[0].store) || a[1] - b[1])
    .map((x) => x[0]);
  // 按区域切组（排序后同区已相邻）—— ⚠ 键用 `normalizeRegionName`，跟 region_sums 一致
  const regionGroups = [];
  rows.forEach((r) => {
    const reg = normalizeRegionName(regions0[r.store]);
    const last = regionGroups[regionGroups.length - 1];
    if (last && last.reg === reg) last.rows.push(r);
    else regionGroups.push({ reg, rows: [r] });
  });
  const renderStoreRow = (r) => {
    const ppl = r.people || [];
    const isOpen = planState.openStores.has(r.store);
    // ⚠ `open` = "这一帧要不要画人那几行"（收起动画期间**要**，不然没得收）；
    //   `isOpen` = 真实状态 —— 箭头和提示语必须按它画，否则收完了还挂着 ▾
    //   （收尾不再重画整张表了，画错就没人改回来 —— 见 `planClean`）。
    const open = isOpen
      || !!(animRows && animRows.dir === 'shrink' && animRows.store === r.store);
    // 「合计」= 七个块**同一层**相加（不含展开出来的系列/机型 —— 那是下一层）。
    // ⚠ 人那一行走**同一个函数** ⇒ 合计口径自动一致（人的数加起来 = 店里那一行）。
    // ⚠ 门店那一格的**可点标记直接拼**，不要再"先拼好 HTML 再 `.replace()` 换掉开头"——
    //   踩过（2026-09-21，坑 12 那一类）：锚串写的是 `<td class="plan-store">`，
    //   而加了列宽 class 之后实际拼出来的是 `<td class="plan-store plan-col plan-col-store">`，
    //   `replace` **一个都没替上、也不报错** ⇒ 门店名没有 ▸、也点不开（拆到人整个失效），
    //   而源码里看着"这段逻辑在"。要改这里，改完**去页面上点一下门店名**。
    const caret = ppl.length ? '<span class="plan-caret">' + (isOpen ? '▾' : '▸') + '</span>' : '';
    const attr = ppl.length
      ? ' data-plan-store="' + esc(encodeURIComponent(r.store)) + '"'
        + ' title="点一下' + (isOpen ? '收起' : '拆到人') + '（' + ppl.length + ' 人）"'
      : '';
    let html = one(r, esc(r.store) + caret, '', attr);
    if (open) {
      // ⚠ 这几行要做**上下动画** ⇒ 打标记 + 内容包一层 `.plan-in`（见 `planRowsFreeze`）。
      //   收起时它们也在 DOM 里（`animRows` 把这家店临时当成展开的），不然没得收。
      const animRow = animRows && animRows.store === r.store ? ' plan-anim-row' : '';
      html += ppl.map((pp) => one(
        pp, '<span class="plan-who">' + esc(pp.name) + '</span>',
        'plan-person' + animRow, '', true)).join('');
    }
    return html;
  };
  const one = (row, nameHtml, cls, storeAttr, wrap) => {
    const tds = cols.map((c) => planCellHtml(row, c, wrap)).join('');
    const tot = (row.blocks || []).reduce(
      (a, b) => a + Number(((b.cur || {})[planState.metric]) || 0), 0);
    const inName = wrap ? '<div class="plan-in">' + nameHtml + '</div>' : nameHtml;
    const inTot = '<span class="plan-v">' + planFmt(tot, planState.metric) + '</span>'
      // ⭐ 合计也要环比（用户 2026-09-21：「**合计也加上和上个月同期对比**」）——
      //   ⚠ 这一行的 `growth` **是后端给的**（`metric.total_growth`：七个块相加后算的），
      //     别在前端拿七块的百分比平均、也别拿展开出来的系列/机型加（双重计算）。
      + planGrowthHtml((row.growth || {})[planState.metric]);
    // ⚠ 区域：汇总行（is_sum）直接用 `row.region`，不查 regions 表
    //   （store 是「共计：…」，regions 表里没有它）。
    // ⭐ 区域名可点：收起 ⇒ ▸（展开明细）；展开 ⇒ ▾（合并为区域汇总）。
    //   汇总行只在该区收起时才画 ⇒ `is_sum` ⇒ 已收起。
    // ⚠ 键走 `normalizeRegionName`（跟分组、region_sums 同一把尺）——
    //   否则显示一样、Set 里却是两个键，点了折不起来。
    const regText = normalizeRegionName(row.is_sum
      ? (row.region || '')
      : ((((planState.data || {}).regions || {})[row.store]) || ''));
    // 无区域的不画按钮（空键点了也没处折）——「其他」是后端兜底键，可以画
    const reg = regText && regText !== '其他'
      ? planRegionBtn(regText, !!row.is_sum)
      : (row.is_sum && regText === '其他' ? planRegionBtn(regText, true) : esc(regText));
    // ⚠ `cls` 前面要留空格 —— 原来是 `'plan-store' + cls` 直接粘，
    //   传 `'film-sum'` 会拼成 `plan-storefilm-sum`（class 对不上、样式全丢）。
    return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + '<td class="plan-lead plan-col">' + reg + '</td>'
      + '<td class="plan-store'
      + (cls ? ' ' + cls : '') + ' plan-col plan-col-store' + (storeAttr ? ' plan-store-open' : '') + '"'
      + (storeAttr || '') + '>'
      + inName + '</td>' + tds
      + '<td class="plan-cell plan-total plan-col plan-col-total">'
      + (wrap ? '<div class="plan-in">' + inTot + '</div>' : inTot) + '</td></tr>';
  };
  const body = regionGroups.map((grp) => {
    // ⭐ 区汇总**默认不画**；点区域名收起该区后只留一行（用户 2026-09-22：
    //   「点击区域名合并，然后才显示汇总，不是现在这样」）。
    // ⚠ 汇总键已经跟分组键同一把尺（`normalizeRegionName` / 后端 strip）；
    //   落盘 `region_sums` 为 null 时接口层会重算 —— 这里再兜一层：
    //   没有汇总也**照样收成一行空汇总不行**，继续画门店（宁可不折，也别整区消失）。
    const sums = d.region_sums || {};
    const sum = sums[grp.reg] || sums[normalizeRegionName(grp.reg)];
    if (planState.collapsedRegions.has(grp.reg) && sum) {
      return one(sum, esc(sum.store || ('共计：' + grp.reg)),
                 'film-sum', '', false);
    }
    return grp.rows.map(renderStoreRow).join('');
  }).join('');
  box.innerHTML = '<table class="plan-table"><thead>' + planHeadHtml(cols)
    + '</thead><tbody>' + body + '</tbody></table>';
  // 动画的第一帧（同一帧里把尺寸冻住，见 `planFreeze` / `planRowsFreeze`）：
  // 列宽 —— 0 宽那几列才是真的 0、其余列一动不动；行高 —— 那几行才是真的 0 高。
  if (anim && (anim.grow.size || anim.shrink.size)) {
    planFreeze(box.querySelector('table'), anim);
  }
  if (animRows) planRowsFreeze(box.querySelector('table'), anim);
  // 记下**最终**那套列（不含动画期临时留着的），下次点的时候拿它当"改之前"
  planState.lastCols = finalPaths;

  const legend = $('#plan-legend');
  if (legend) {
    legend.innerHTML = '看的是 <b>' + esc(PLAN_METRICS[planState.metric]) + '</b>'
      + '（每格上行是本月，下行是环比）　·　点列名上的 <b>▸</b> 展开下一级'
      + '（<b>二级、三级都往右边接，表头就一行</b>）'
      + '　·　点<b>门店名</b>拆到人（人那几行在最左边列，缩进一档）'
      + '　·　点<b>区域名</b>合并成一行区域汇总'
      + '　·　共 <b>' + rows.length + '</b> 家店';
  }
  // 没认出来的品类词 / 明确不纳入的系列 —— **必须报出来**（静默归并 = 口径悄悄变了）
  if (unk) {
    const kw = Object.entries(d.unknown || {});
    const sk = Object.entries(d.skipped || {});
    let html = '';
    if (kw.length) {
      html += '⚠ <b>有认不出的品类</b>：'
        + kw.map(([k, v]) => esc(k) + '（' + v + ' 行）').join('、')
        + ' —— 这些<b>没算进任何一块</b>，要补映射表。';
    }
    if (sk.length) {
      html += (html ? '<br>' : '') + '另有不纳入统计的：'
        + sk.map(([k, v]) => esc(k) + '（' + v + ' 行）').join('、');
    }
    unk.innerHTML = html;
  }
}

async function loadPlan() {
  if (!planPageMounted) return;
  if (planReadController) planReadController.abort();
  const epoch = ++planPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  planReadController = controller;
  try {
    const d = await api('/api/plan', controller ? { signal: controller.signal } : {});
    if (!planPageMounted || epoch !== planPageEpoch) return;
    renderPlan(d);
  } catch (e) {
    if (!planPageMounted || epoch !== planPageEpoch || (e && e.name === 'AbortError')) return;
    $('#plan-meta').textContent = '';
    $('#plan-warn').innerHTML = '';
    $('#plan-table').innerHTML =
      '<div class="empty">读不到月度数据：' + esc(e.message || e) + '</div>';
    toast('读取月度生意计划失败：' + e.message, 'bad');
  } finally {
    if (planReadController === controller) planReadController = null;
  }
}

// 指标切换：**只重画**（数据没变，不重新请求）
$$('#plan-metric .seg-btn').forEach((b) => b.addEventListener('click', () => {
  planState.metric = b.dataset.metric;
  planPersist();
  if (planState.data) renderPlan(planState.data);
}));

// 点列名展开 / 收起（事件委托 —— 表头是每次重画的，绑不到具体元素上）
$('#plan-table')?.addEventListener('click', (e) => {
  // ① 点**区域名** = 合并成区域汇总 / 展开门店（用户 2026-09-22）
  const reg = e.target.closest('[data-plan-region]');
  if (reg) {
    planToggleRegion(decodeURIComponent(reg.dataset.planRegion));
    return;
  }
  // ② 点**门店名** = 拆到人 / 收起（用户 2026-09-21）
  const td = e.target.closest('[data-plan-store]');
  if (td) {
    planToggleStore(decodeURIComponent(td.dataset.planStore));   // 带上下动画
    return;
  }
  // ③ 点产品线列名 = 展开 / 收起下一级（**带往右推的动画**）
  const th = e.target.closest('[data-plan-toggle]');
  if (!th) return;
  planToggleCol(decodeURIComponent(th.dataset.planToggle));
});

/* 展开 / 收起一列 —— 两帧 + 一次收尾，宽度由 CSS 过渡（用户 2026-09-21：
   「展开和收起来的时候**有往右移动的动画**」）。

   ⚠ 三帧的顺序不能省：
     ① 先画"新列 0 宽、要收的列原宽"（这是**起始态**，必须真的进过 DOM，
        直接改 class 不会触发过渡）；
     ② 下一帧（`requestAnimationFrame`）放开 ⇒ 过渡跑起来；
     ③ `220ms` 后把收起的列真正从 DOM 里去掉（那时它已经 0 宽，看不出来）。
   ⚠ 中途中不要 `planState.anim = null`：那时候收起的那几列还在 DOM 里，
     得等 ③ 那次重画才干净。 */
function planToggleCol(path) {
  if (!planState.data) return;
  const before = planState.lastCols || [];   // ⚠ 存的就是 path 字符串
  if (planState.open.has(path)) planState.open.delete(path);
  else planState.open.add(path);
  planPersist();
  const final = planLayout(planState.data).cols.map((c) => c.path);
  const beforeSet = new Set(before), finalSet = new Set(final);
  const gone = before.filter((p) => !finalSet.has(p));
  // 收起时：它那几个子列的父节点，动画期**临时当成展开的**（见 `planLayout` 的 extraOpen）
  const extra = new Set(gone.map((p) => p.split(PLAN_SEP).slice(0, -1).join(PLAN_SEP)));
  planAnimate({ grow: new Set(final.filter((p) => !beforeSet.has(p))),
                shrink: new Set(gone), extra, phase: 0, rows: null });
}

/* 点**门店名** = 拆到人 / 收起 —— 走同一套动效（上下那一维，用户 2026-09-21：
   「上下的动画加上」）。⚠ 列**不动**（`grow` / `shrink` 都空），所以 `planFreeze`
   那一支不会跑，只有 `planRowsFreeze` 管高度。 */
function planToggleStore(store) {
  if (!planState.data) return;
  const open = planState.openStores.has(store);
  if (open) planState.openStores.delete(store);
  else planState.openStores.add(store);
  planPersist();
  planAnimate({ grow: new Set(), shrink: new Set(), extra: new Set(), phase: 0,
                rows: { store, dir: open ? 'shrink' : 'grow' } });
}

// 刷新 = **先抓云商新数据、再重算这一步**（步骤名单在后端 `REFRESH_STEPS.monthly`）
// 刷新 = **先抓云商销售导出、再算一遍**（跟月度计划同一条路，`REFRESH_STEPS.film`）。
// ⚠ 不自动跑（`whens=()`）—— 门店手动拉就行（2026-09-22 用户）。
$('#btn-refresh-film')?.addEventListener('click', (e) =>
  refreshWithFetch('film', e.currentTarget, () => loadFilm()));
$('#btn-refresh-plan')?.addEventListener('click', (e) =>
  refreshWithFetch('monthly', e.currentTarget, () => loadPlan()));

// 导出为 Excel —— 导的是**后端按你身份过滤过的那一份**（前端手里这份只是用来画表的）
$('#btn-export-plan')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#plan-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/plan/export', { method: 'POST', body: {} });
    const url = '/api/export/download?name=' + encodeURIComponent(r.file);
    triggerDownload(url, r.file);
    if (box) {
      box.hidden = false;
      box.innerHTML = '已下载 <b>' + esc(r.file) + '</b>（' + r.rows + ' 行 · '
        + (r.sheets || []).length + ' 张表）—— 没弹出保存框的话，去浏览器<b>「下载」</b>里找　'
        + '<a class="btn ghost small" href="' + url + '">再下一次</a>'
        + '<span class="hint">（服务端也留了一份：<code>' + esc(r.rel) + '</code>）</span>';
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

// ⭐ 这三个「刷新」都是**先抓新数据、再重读这一页**（用户 2026-09-20）。
//   ⚠ 各页要抓的步骤不一样（后端 `REFRESH_STEPS` 说了算）：
//     达成读云商销售明细 / POS 读玲珑单据 / 双平台两边都要。
$('#btn-refresh-attain')?.addEventListener('click', (e) =>
  refreshWithFetch('attain', e.currentTarget, () => loadAttain(true)));
// ⚠ 刷新按钮**不带 period** ⇒ 回到最新那一周（用户预期：刷新 = 看最新的）
$('#btn-refresh-attain-hist')?.addEventListener('click',
  () => { attainHistCur = ''; loadAttainHistory(); });


function mountPlanPage() {
  planPageMounted = true;
}

function unmountPlanPage() {
  planPageMounted = false;
  planPageEpoch += 1;
  if (planReadController) planReadController.abort();
  planReadController = null;
  planAnimationFrames.forEach((frame) => cancelAnimationFrame(frame));
  planAnimationFrames.clear();
  planAnimationTimers.forEach((timer) => clearTimeout(timer));
  planAnimationTimers.clear();
  planState.anim = null;
  planState.data = null;
  planState.lastCols = [];
}

registerPage('monthly', {
  mount: mountPlanPage,
  unmount: unmountPlanPage,
  load: loadPlan,
  refresh: loadPlan,
});
