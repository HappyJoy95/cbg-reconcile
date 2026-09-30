'use strict';

/* 门店数据平台 · 本地控制台
   无框架、无构建、无 CDN —— 门店电脑断网也能用。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const CONFIG_FIELDS = [
  // ⚠ 门店那三项（store_code / marker / erp_store_name）2026-09-18 **删掉了** ——
  //   它们不再由人填，而是「确认登录」匹配成功后**直接写进配置**。
  //   要看当前值：这张卡的头上有摘要，右下角状态抽屉里也有。
];   // ⚠ 「对账参数」那几项 2026-09-17 拿掉了（报量排查整步没了）：
//   lookback_days / report_lookahead_days 只剩 `cmd_check` 手动跑时读，前端不暴露；
//   page_size / pay_status / return_status 是**死配置** —— 从来没人把它们传进接口
//   （`dump.py` 用自己的 `--page-size`，而且明确不许传 payStatus/returnStatus）。

const state = { overview: null, report: null, sheet: null, since: 0, jobId: null,
                // ⚠ 原来这里还有个 `timer`（「跑一次」那套的轮询句柄）——
                //   2026-09-21 那张卡删掉之后它就没人赋值了，一起删。
                autoTimer: null };

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
    // ⚠ **403 = 后端门禁**（`web.setup_state`）—— 不是"这个接口出错"，
    //   而是"这台机器还没登录好"。这时候要把登录页顶出来，
    //   否则用户只会看到一句"失败了"，而不知道该干什么。
    if (r.status === 403 && data && data.setup) {
      showSetup(data.setup);
    }
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
  // ⚠ 单元格可以自带 class（`{html:…, cls:'t-red'}`）—— 达成率标色用得上：
  //   颜色要打在 **`td`** 上（整格变色），而 `cls` 参数是**按列**的，做不到逐格。
  const k = (raw && c.cls) || cls;
  return `<td class="${k || ''}">${raw ? c.html : esc(c)}</td>`;
}

function table(header, rows, cls = [], withHead = true) {
  if (!rows || !rows.length) return '<div class="empty">没有记录</div>';
  // ⚠ `withHead=false`：**不要那行表头**（用户 2026-09-20：「这一行不要，
  //   在最下面做个合计就行了」）—— 展开区就在主表下面，产品名上面已经有了。
  // ⚠ 表头也认 `{html: …}`（跟 `cell()` 同一套判断）：
  //   2026-09-19 达成表要在表头里换行（产品名 + 占比两行），
  //   而那时表头只会 `esc(h)` ⇒ 传对象进去渲染成 `[object Object]`，
  //   **整个表头就看不见了**（用户当场发现：「表头没显示出来呀」）。
  //   ⚠ 字符串**照样转义** —— 只多了一条"你自己包了 {html} 才当 HTML"的口子。
  const head = (header || []).map((h) =>
    `<th>${h && typeof h === 'object' && 'html' in h ? h.html : esc(h)}</th>`).join('');
  // ⚠ 行也可以带 class：`{ cls: 'film-sum', cells: [...] }` —— 分区「共计」红底行要用。
  //   普通行仍然是**纯数组**（老写法一个都不用改）。
  const body = rows.map((r) => {
    const isObj = r && typeof r === 'object' && !Array.isArray(r) && 'cells' in r;
    const cells = isObj ? (r.cells || []) : (r || []);
    const rcls = isObj && r.cls ? ` class="${esc(r.cls)}"` : '';
    return `<tr${rcls}>` + cells.map((c, i) => cell(c, cls[i])).join('') + '</tr>';
  }).join('');
  return `<table>${withHead ? `<thead><tr>${head}</tr></thead>` : ''}`
    + `<tbody>${body}</tbody></table>`;
}

/* ───────────────────────── 页签（一级 = 业务）─────────────────────────

   ⚠ 2026-09-18 前端融合改版（用户定）：一级页签从「一个功能一个」
   （报量查询 / POS 合规 / 运行 / 会话 / 设置）改成**按业务分家** ——
   **销售数据 / 五项合规**。三个原因：
     * 3.0.0 加了「销售达成」就 6 个，门店面对的是一排平铺的功能名，没有层级；
     * 一个业务页里放不下的功能用**二级标签**切；
     * **「运行」不再是页签** —— 进了右下角那个常驻抽屉（`setRunDrawer`），
       因为它要「一直在、且好找」，做成页签反而要求你先切过去。

   ⚠ 2026-09-19（用户）：「把左边儿一级标签的设置去掉吧，给它弄成鼠标移动到
   左下角门店那个页面的时候，**向上**出现设置里的那些东西」。
   ⇒ 结果**分两处**，别混：
     * **设置**（账号设置 / 人员设置 / 检查更新 / 通用 / 玲珑授权 / 定时器设置）
       → **左下角那几行就地变形**（见 `FOOT_GO`）；那个一级页签**没了**；
     * **模块自己的「设置」**（销售数据 / 五项合规各一条）→ **留在各自模块的
       下拉里**。用户随后纠正过：「**各个模块的设置还是要给模块**，
       只把通用设置做下来就行了」。
     判别标准：跟**业务**有关的设置留在模块；这台**电脑本身**的配置才在左下角。
     一级页签因此只剩**两个业务页**。

   见 `.dsh/docs/2026-09-18-3.0.0-开发目标.md` 四·五。 */

//: 面板 → 它的二级标签（顺序 = 界面上出现的顺序，也是**默认落在哪个**）。
//: ⚠ `sales` / `compliance` 的列表 = **左侧下拉菜单里的项**（HTML 里那份要对齐）。
//: ⚠ `settings` **没有一级页签** —— 这份列表只是"`switchTab` 认得哪些 key"，
//:   菜单在左下角那几行（`FOOT_GO` / HTML 里那份，顺序要跟这里一致）。
const SUBTABS = {
  // ⚠ 模块自己的「设置」**留在模块里** —— 用户 2026-09-19 的原话：
  //   「**各个模块的设置还是要给模块**，只把通用设置做下来就行了」。
  //   判别标准：跟**业务**有关的设置（这条推送推不推）留在模块；
  //   这台**电脑本身**的配置（登录态 / 人员 / 邮件 / 定时）才在左下角。
  // ⚠ 「历史记录」紧跟「周度目标达成情况」（用户 2026-09-20：在工作区的**周度目标达成情况下面**加个历史记录）
  sales: ['attain', 'attain-history', 'sales-settings'],
  // ⚠ 月度生意计划（M22）：跟「周度重点产品」平级，但**只读、不算目标达成**。
  plan: ['monthly', 'plan-settings'],
  compliance: ['pos', 'pools', 'compliance-settings'],
  // ⚠ 库存盘点（M16）：**已拆 iframe**，markup 在 `#subpanel-inventory` 里。
  //   列在这儿只是因为 `switchTab` 要认得这个 key（兜底卡片那条路要用）；
  //   正常入口是导航里那个二级项 —— 它带 `data-page`，点击直接开新标签页。
  inventory: ['inventory', 'inventory-settings'],
  // ⚠ 增值：防护膜 + 无忧会员权益（2026-09-22）—— 本地 erp_sales 现算，无定时步骤。
  valueadd: ['film', 'benefit', 'valueadd-settings'],
  // ⚠ 小工具（2026-09-22）：价签 / 工牌 —— 各一格 iframe，无定时步骤。
  //   2026-09-23 加 **串号追踪**（sn-trace）。
  tools: ['pricetag', 'badge', 'claim-pending', 'sn-trace'],
  // ⚠ 收银（2026-09-29 生活馆利润核算录入端）：**独立一级 tab**（不是 tools 子页），
  //   入口是 nav 里那个 `nav-lifehall` 直连块（full 版 CSS 藏）。
  //   tab / subtab 同 key —— `inventory` 先例。
  cashier: ['cashier'],
  // ⚠ 顺序 = 左下角浮层里的顺序（`linglong` 排头 —— 门店来这儿最常干的就是抓会话）
  // ⚠ 顺序 = 左下角那几行里的顺序（`FOOT_GO`）。
  //   ⚠ 这两项**现在都是独立二级页**（2026-09-20 定时器 / 2026-09-21 检查更新，
  //     用户：「检查更新单独做一个页面」）—— 所以都在这份清单里。
  // ⚠ 2026-09-20：「定时器设置」从"跳到卡片"变成**真的一个二级页** ⇒ 加进这份
  //   （它的 `data-foot` 键还叫 `scheduler`，落点是 `timer` —— 对不上的话
  //     `test_导航里的二级标签要和_SUBTABS_对得上` 会红）。
  // ⚠ 顺序 = **左下角那几行的顺序**（`data-foot`），有测试逐项比对。
  // ⚠ 2026-09-21（用户：「数据上报这个是好的，**放到左下角的系统设置**吧，
  //   一级标签改叫**数据交换**」）—— 它从顶部一级标签变成左下角那一行，
  //   页面落进「设置」这一档（`#subpanel-stores`）。
  settings: ['account', 'update', 'general', 'theme', 'stores', 'linglong', 'timer'],
};

//: 二级标签 → 切过去要拉什么。key 必须在 HTML 里有 `data-subtab="key"`。
//: ⚠ 纯说明性的标签（`*_settings`）故意留空 —— 别为了"对称"硬塞一个请求。
const SUBTAB_LOADERS = {
  // ⚠ 账号设置和人员设置 2026-09-21 合并成「账号与人员」（用户：「合并到一起吧」）——
  //   所以切到这一页要**两样都拉**（账号状态 + 本店人员）。
  account: () => { renderAccount(); loadStaff(); },
  attain: () => loadAttain(true),
  // 经营看板：读 `out/plan-<年>.json`（`python -m src.cli plan` 算好的那份）
  monthly: () => loadPlan(),
  'attain-history': () => loadAttainHistory(),
  pos: () => loadPos(),
  pools: () => loadOverview(),
  'sales-settings': () => loadAttainSettings(),
  'compliance-settings': () => loadNotifyPrefs('#notify-pref-list-compliance', ['pos', 'pools']),
  'plan-settings': () => loadNotifyPrefs('#notify-pref-list-plan', ['plan']),
  'inventory-settings': () => loadNotifyPrefs('#notify-pref-list-inventory', ['inventory']),
  // 库存盘点：**同文档**（已拆 iframe）—— 切过来懒注入 inventory/*.js + 聚焦扫码框
  // 切过来才注入脚本 + 把焦点交给扫码框（扫码枪要焦点），见下面的 `mountInventory`。
  inventory: () => mountInventory(),
  film: () => loadFilm(),
  benefit: () => loadBenefit(),
  'valueadd-settings': () => loadNotifyPrefs('#notify-pref-list-valueadd', ['film', 'benefit']),
  // 小工具：价签 / 工牌 —— **各一格 iframe**（#pricetag-frame / #badge-frame），
  // 懒挂载；切二级只显示对应 subpanel（switchTab 已管）
  pricetag: () => mountTools('pricetag'),
  badge: () => mountTools('badge'),
  'claim-pending': () => loadClaimPending(),
  'sn-trace': () => { /* 查询页：等用户输入，不自动打接口 */ },
  // 收银：一次 GET 带回当日流水 + 政策新鲜度 + 销售员历史
  cashier: () => loadCashier(),
  general: () => { loadConfig(); loadNotifyPrefs(); },
  // 主题设置**独立一页**（2026-09-22）—— 选中态刷到卡片 + 拉壁纸列表
  theme: () => { syncThemeUi(); loadWallpapers(); },
  // 检查更新**自己一页**了（用户 2026-09-21）—— 原来靠 loadConfig 顺手拉
  update: () => loadUpdate(),
  // ⚠ 「定时器设置」是**独立一页**（用户 2026-09-20），通用页不再顺带拉它
  timer: () => { loadTimer(); loadSchedulerBits(); },
  // 玲珑授权 = 原来的「会话」页（抓登录态那三件事）
  // 玲珑授权页 = 会话状态 + 抓取控件 + **门店编码**（2026-09-29 挪进来，
  // 用户：「把门店编码设置放到玲珑授权里面吧」）—— 切到这页顺手重画店码框
  // （拿最新 setupState 里的当前值，别显示上次编辑前的旧值）。
  linglong: () => { renderSession(); loadBrowserInfo(); loadHwLogin(); renderStoreCodeForms(); },
  // 每店一张卡（M20）—— 数据全部来自收信库（各店发来的邮件）
  stores: () => loadStores(),
};

//: ⚠⚠ **介绍页已经删了**（2026-09-19 用户改的口径）。
//:
//: 2026-09-18 的原话是「每个一级标签做一个默认的介绍页面，点击一级标签进介绍页」——
//: 用了一天之后用户说：「**B 吧，现在来看这个介绍页没用**」。
//: ⇒ 点一级标签 = 进**它第一个二级页**（跟左下角那几行一个路子：
//:   点一下就直接到该去的地方，中间不再垫一页"这块是干什么的"）。
//: ⚠ 所以**没有 `TAB_HOME` 了**，`#subpanel-*-home` 和那几张 `.intro-card` 也一并删掉。
//:   留着的话它们永远不会被激活，下次改前端的人还会以为"这里有个页面"。
function switchTab(tab, subtab) {
  const subs = SUBTABS[tab] || [];
  // ⚠ 不认识的 key 一律落到**第一个二级页** —— 传错了别做半截。
  const home = subs[0] || '';
  const known = subs.slice();
  subtab = subtab && known.indexOf(subtab) >= 0 ? subtab : home;
  $$('.tab').forEach((x) => x.classList.toggle('active', x.dataset.tab === tab
    && (!x.dataset.directSubtab || x.dataset.directSubtab === subtab)));
  $$('.panel').forEach((p) => p.classList.toggle('active', p.id === 'panel-' + tab));
  // 盘点已拆 iframe，正常跟 main 一起滚；不再需要 inv-open 收底部 padding
  // 小工具：**仅价签/工牌 iframe** 满高（tools-frame-mode）；
  //   权益领取走普通文档流，否则卡片下面会空一大截（2026-09-23）。
  const mainEl = document.querySelector('main');
  document.body.classList.toggle('lifehall-edition', !!(setupState && setupState.lifehall));
  mainEl?.classList.toggle('tools-open', tab === 'tools');
  mainEl?.classList.toggle(
    'tools-frame-mode',
    tab === 'tools' && (subtab === 'pricetag' || subtab === 'badge'));
  if (subtab) {
    const panel = $('#panel-' + tab);
    if (panel) {
      Array.from(panel.querySelectorAll('.subpanel')).forEach((p) =>
        p.classList.toggle('active', p.id === 'subpanel-' + subtab));
    }
  }
  // 菜单 / 设置浮层里把"当前停在哪一页"标出来 —— 展开时一眼能对上。
  // ⚠ 两处都扫：`sales` / `compliance` 的项在 `.nav-menu` 里，
  //   而 `settings` 那几条在左下角的 `.foot-item` 里（它们**不在导航里**）。
  //   二级标签的 key 全局唯一，所以不必按 tab 再筛一遍。
  Array.from($$('.nav-menu .subtab')).forEach((x) =>
    x.classList.toggle('current', x.dataset.subtab === subtab));
  closeNavMenus();
  closeFootMenu();
  // ⚠ 达成那块「谁卖的」悬浮层是 `position: fixed` 挂在 body 上的 ——
  //   切页必须立刻收掉，不然会浮在别的页面上（2026-09-22 用户报的串页 bug）。
  forceHideAttainPop();
  const load = SUBTAB_LOADERS[subtab];
  if (load) load();
}

/* ─────────────── 顶部导航：悬停**只展开菜单**，点击才跳 ───────────────

   用户 2026-09-18：「参考苹果这个，最上面几个，鼠标移动过去自动显示对应的二级标签」。
   所以是**两段式**：悬停看一眼有哪些二级页 → 点哪个才去哪儿。
   ⚠ **别改成"悬停即切换"** —— 鼠标扫过顶部就切页，误触率很高，
   而且门店那台机器上还可能是触摸屏（手指划过去就是一次 hover）。

   ⚠ **每个二级标签只有一份**（页内那排按钮 2026-09-18 就去掉了）——
   这一份在**顶部下拉菜单**里；而「设置」那几条在**左下角那几行**里（见 `FOOT_GO`）。
   两处各写一份的话，迟早有一处不同步（这个项目为"两份定义"栽过好几次）。 */

/*: 带过渡的显示 / 隐藏。**二级菜单和状态悬浮窗共用这一个**。
 *
 * ⚠ 为什么不能直接 `el.hidden = !open`：`display: none` **过渡不了**，
 *   所以收起是"啪"地消失。做法是动画挂 `.open` 类，`hidden` 只负责最终隐藏。
 * ⚠ 打开要**等两帧**再加类 —— 同一个 tick 里 display 从 none 变 block、
 *   又同时加类，浏览器只看到最终态，过渡根本不触发。
 * ⚠ 关的时候要**延迟**才 `hidden`（等动画播完），而且开一次要
 *   `clearTimeout` —— 不然"关到一半又打开"会被上一发的定时器藏掉。
 * ⚠ 计时器挂在**元素自己身上**（`el._animTimer`）：菜单有好几个，
 *   共用一个变量的话关 A 会被开 B 取消掉。
 */
function setVisible(el, open, ms) {
  if (!el) return;
  clearTimeout(el._animTimer);
  if (open) {
    el.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('open')));
  } else {
    el.classList.remove('open');
    el._animTimer = setTimeout(() => { el.hidden = true; }, ms || ANIM_MS);
  }
}
//: `--motion-fast` 的毫秒数 —— 跟 `theme.css` 里那个令牌对齐（有测试盯着）
const ANIM_MS = 160;
//: 悬浮窗用 `--motion-normal`（它动的是 transform + opacity，位移更大，慢一点才顺）
const DRAWER_ANIM_MS = 200;

//: 鼠标离开一级菜单后**等这么久**再收 —— 见下面的注释
const NAV_CLOSE_GRACE = 260;
let navCloseTimer = null;

function scheduleCloseNavMenus() {
  clearTimeout(navCloseTimer);
  navCloseTimer = setTimeout(() => closeNavMenus(), NAV_CLOSE_GRACE);
}
function cancelCloseNavMenus() {
  clearTimeout(navCloseTimer);
  navCloseTimer = null;
}

function closeNavMenus(keep) {
  clearTimeout(navCloseTimer);
  navCloseTimer = null;
  $$('.nav-item').forEach((item) => {
    if (item === keep) return;
    // ⚠ 2026-09-19 起**没有浮层了**（改成一级标签就地变形，照左下角那套）：
    //   收起 = 去掉 `.open`，高度由 CSS 过渡回 0 —— **不再碰 `hidden`**
    //   （`display: none` 过渡不了，那正是浮层时代要 `setVisible` 的原因）。
    item.classList.remove('open');
    const btn = item.querySelector('.tab');
    if (btn) btn.setAttribute('aria-expanded', 'false');
  });
}

function openNavMenu(item) {
  cancelCloseNavMenus();
  const btn = item.querySelector('.tab');
  closeNavMenus(item);                  // 别的先收（一次只开一个）
  item.classList.add('open');           // 该展开哪个由 CSS 的 `--nav-h` 说了算
  if (btn) btn.setAttribute('aria-expanded', 'true');
}

$$('.nav-item').forEach((item) => {
  // ⚠ 用户 2026-09-18：「鼠标在一级菜单悬停后移到二级菜单时，**慢一点就点不上**」。
  //   两个原因，都堵上：
  //   ① 菜单和导航之间那 8px **缝** —— 指针跨过去的一瞬间两边都不沾，
  //      `mouseleave` 立刻触发、菜单当场收掉（CSS 里给菜单加了个透明的"桥"补住）；
  //   ② 就算不走缝，斜着挪/挪得慢也容易碰到边 —— 所以**离开后不立刻收**，
  //      留 `NAV_CLOSE_GRACE` 毫秒宽限期，这段时间里回到菜单/导航就撤销。
  item.addEventListener('mouseenter', () => openNavMenu(item));
  item.addEventListener('mouseleave', () => scheduleCloseNavMenus());
  // ⚠ 光靠 hover 的话**触屏机上一辈子打不开** —— 聚焦也展开一份
  item.addEventListener('focusin', () => openNavMenu(item));
  const direct = item.querySelector('[data-direct-subtab]');
  if (direct) direct.addEventListener('click', () =>
    switchTab(item.dataset.tab, direct.dataset.directSubtab));
  Array.from(item.querySelectorAll('[data-subtab]')).forEach((b) =>
    b.addEventListener('click', () => switchTab(item.dataset.tab, b.dataset.subtab)));
});

/* ─────────────── 收银（生活馆利润核算录入端，2026-09-29）───────────────

   用户拍板（同日）：目前**没有生活馆的玲珑数据** ⇒ 先做单独录入；
   「拉玲珑 + 过滤备注1/2/3 + 改金额/销售员」等有数据再接（开发目标·七）。
   ⚠ 编码是主输入：识别编码 → `price_policy` 反查商品名（成本/返利顺带显示）；
     金额、销售员**手动匹配**（没有现成名单，销售员下拉靠历史积累）。
   ⚠ 政策刷新走 pmall A 案（活窗 → jar → 弹窗人工登录），接口会**阻塞到登录
     完成（最长 10 分钟）** —— 按钮必须禁用并说清，别让人以为卡死了狂点。 */

let _cashierEditing = 0;      // 正在改的流水 id（0 = 新录）
let _cashierAutoName = '';    // 最近一次反查自动带出的名 —— 用户手改过就不再覆盖
let _cashierBound = false;    // 事件只绑一次（loadCashier 每次进来都调）
let _cashierRows = {};        // id → 行（表格按钮回填表单用）

function cashierPad(n) { return String(n).padStart(2, '0'); }

function cashierNow() {
  // `datetime-local` 要 `YYYY-MM-DDTHH:MM` —— **本地时区**（门店机器 = 北京时间，
  // 跟 dump 按 CST 算年份同一条道理：别用 toISOString 那个 UTC，午夜会串天）
  const d = new Date();
  return `${d.getFullYear()}-${cashierPad(d.getMonth() + 1)}-${cashierPad(d.getDate())}`
    + `T${cashierPad(d.getHours())}:${cashierPad(d.getMinutes())}`;
}

function cashierToday() {
  const d = new Date();
  return `${d.getFullYear()}-${cashierPad(d.getMonth() + 1)}-${cashierPad(d.getDate())}`;
}

function cashierResetForm() {
  _cashierEditing = 0;
  _cashierAutoName = '';
  const sold = $('#cashier-sold-at');
  if (sold) sold.value = cashierNow();
  for (const id of ['cashier-scan', 'cashier-amount', 'cashier-note']) {
    const el = $('#' + id);
    if (el) el.value = '';
  }
  const qty = $('#cashier-qty');
  if (qty) qty.value = '1';
  const name = $('#cashier-name');
  if (name) name.value = '';
  const cancel = $('#cashier-cancel');
  if (cancel) cancel.hidden = true;
}

function cashierFill(r) {
  _cashierEditing = r.id;
  _cashierAutoName = '';          // 回填的是人写的名 —— 反查不许再覆盖它
  $('#cashier-sold-at').value = String(r.sold_at || '').slice(0, 16).replace(' ', 'T');
  $('#cashier-scan').value = r.goods_code || '';
  $('#cashier-name').value = r.goods_name || '';
  $('#cashier-qty').value = r.quantity != null ? r.quantity : 1;
  $('#cashier-amount').value = r.amount != null ? r.amount : '';
  $('#cashier-seller').value = r.seller || '';
  $('#cashier-note').value = r.note || '';
  $('#cashier-cancel').hidden = false;
  $('#cashier-sold-at').focus();
}

async function cashierLookup(focusAmount) {
  const codeEl = $('#cashier-scan');
  const code = (codeEl.value || '').trim();
  if (!code) return;
  try {
    const d = await api('/api/cashier/lookup?code=' + encodeURIComponent(code));
    if (!d.found || !d.row) {
      toast(`编码 ${code} 不在政策表 —— 先「刷新政策数据」，或手输商品名。`, 'bad');
      return;
    }
    const row = d.row;
    const name = String(row['商品名称'] || '');
    const nameEl = $('#cashier-name');
    if (name && (!nameEl.value || nameEl.value === _cashierAutoName)) {
      nameEl.value = name;
      _cashierAutoName = name;
    }
    if (focusAmount) $('#cashier-amount').focus();
  } catch (e) {
    toast('反查失败：' + e.message, 'bad');
  }
}

async function cashierSave() {
  const body = {
    sold_at: $('#cashier-sold-at').value,
    goods_code: ($('#cashier-scan').value || '').trim(),
    goods_name: ($('#cashier-name').value || '').trim(),
    quantity: $('#cashier-qty').value,
    amount: $('#cashier-amount').value,
    seller: ($('#cashier-seller').value || '').trim(),
    note: ($('#cashier-note').value || '').trim(),
  };
  if (_cashierEditing) body.id = _cashierEditing;
  if (!String(body.amount).trim()) {
    toast('实收金额还没填（金额是手动匹配的那项）', 'bad');
    $('#cashier-amount').focus();
    return;
  }
  try {
    await api('/api/cashier/entry-save', { method: 'POST', body });
    toast(_cashierEditing ? '已修改' : '已保存一笔', 'good');
    cashierResetForm();
    await loadCashier();
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
  }
}

async function cashierRemove(id) {
  if (!window.confirm('删除这笔流水？删了不能恢复。')) return;
  try {
    await api('/api/cashier/entry-delete', { method: 'POST', body: { id } });
    toast('已删除', 'good');
    if (_cashierEditing === id) cashierResetForm();
    await loadCashier();
  } catch (e) {
    toast('删除失败：' + e.message, 'bad');
  }
}

// ⚠ 拼 HTML 必须转义（坑 3）—— `esc` 已经做了，这里是空值兜底的同款包装
function esc2(s) { return esc(s == null ? '' : String(s)); }

function cashierPayViewHtml(r) {
  const pays = (r.payments || []);
  if (!pays.length) return '<div class="cc-muted">未记录</div>';
  const sum = pays.reduce((a, x) => a + (Number(x.amount) || 0), 0);
  const amt = Number(r.amount) || 0;
  const warn = Math.abs(sum - amt) > 0.009
    ? `<div class="pay-warn">已付 ¥${sum.toFixed(2)} ≠ 实收 ¥${amt.toFixed(2)}</div>` : '';
  return '<div class="cc-row">'
    + pays.map((p) => `<span>${esc2(p.method)} ¥${(Number(p.amount) || 0).toFixed(2)}</span>`)
      .join('、')
    + '</div>' + warn;
}

function cashierCardHtml(r) {
  const acc = (r.accessories || []);
  const sold = String(r.sold_at || '').slice(5, 16);
  const src = r.source === 'linglong'
    ? '<span class="cc-wait" title="修改只影响本机视图，不动华为原单">玲珑 '
      + esc2(r.external_id || '') + '</span>' : '';
  const accHtml = acc.length
    ? '<div class="cc-acc"><b>配件</b>'
      + acc.map((x) => `<div class="cc-acc-row"><span>${esc2(x.name)}</span>`
        + `<span>¥${(Number(x.amount) || 0).toFixed(2)}</span></div>`).join('')
      + '</div>'
    : '';
  return `<div class="cashier-card card" data-id="${r.id}">`
    + `<div class="cc-main">`
    + `<div class="cc-time">${esc2(sold)} ${src}</div>`
    + `<div class="cc-name">${esc2(r.goods_name) || '（没填名称）'}</div>`
    + `<div class="cc-meta">商品编码 ${esc2(r.goods_code) || '—'}`
    + `<br>SN ${esc2(r.sn) || '—'}</div>`
    + `<div class="cc-amount-row"><span class="cc-qty">数量 `
    + `${esc2(Math.round((Number(r.quantity) || 0) * 100) / 100)}</span>`
    + `<span class="cc-amount">¥${(Number(r.amount) || 0).toFixed(2)}</span></div>`
    + accHtml
    + `<div class="cc-actions">`
    + `<button class="btn ghost small" data-cc-edit="${r.id}">改</button>`
    + `</div>`
    + `</div>`
    + `<div class="cc-side">`
    + `<div class="cc-group"><div class="cc-group-title">销售信息</div>`
    + `<div class="cc-row"><span class="k">销售员</span>`
    + `<span class="v">${esc2(r.seller) || '—'}</span></div>`
    + `<div class="cc-row"><span class="k">备注</span>`
    + `<span class="v">${esc2(r.note) || '—'}</span></div></div>`
    + `<div class="cc-group cc-muted"><div class="cc-group-title">客户信息</div>`
    + `会员/手机号<span class="cc-wait">待接入</span></div>`
    + `<div class="cc-group"><div class="cc-group-title">支付方式</div>`
    + cashierPayViewHtml(r)
    + `</div>`
    + `</div>`
    + `<button class="cc-close" data-cc-close="${r.id}" title="删除这笔">✕</button>`
    + `</div>`;
}

function cashierRenderSummary(rows, day) {
  const n = rows.length;
  const qty = rows.reduce((a, r) => a + (Number(r.quantity) || 0), 0);
  const amt = rows.reduce((a, r) => a + (Number(r.amount) || 0), 0);
  const acc = rows.reduce((a, r) => a + (r.accessories || [])
    .reduce((b, x) => b + (Number(x.amount) || 0), 0), 0);
  const set = (id, v) => { const el = $('#' + id); if (el) el.textContent = v; };
  set('cashier-count', n);
  set('cashier-qty-sum', Math.round(qty * 100) / 100);
  set('cashier-amount-sum', amt.toFixed(2));
  set('cashier-acc-sum', acc.toFixed(2));
  const lab = $('#cashier-day-label');
  if (lab) lab.textContent = day || cashierToday();
}

function renderCashierCards(rows) {
  const host = $('#cashier-cards');
  _cashierRows = {};
  (rows || []).forEach((r) => { _cashierRows[r.id] = r; });
  if (!rows || !rows.length) {
    if (host) host.innerHTML = '<div class="empty">这天还没有流水</div>';
    return;
  }
  if (host) host.innerHTML = rows.map(cashierCardHtml).join('');
}

async function loadCashier() {
  bindCashierEvents();
  const dayEl = $('#cashier-day');
  if (dayEl && !dayEl.value) dayEl.value = cashierToday();
  const soldEl = $('#cashier-sold-at');
  if (soldEl && !soldEl.value) soldEl.value = cashierNow();
  try {
    const d = await api('/api/cashier/entries?day='
      + encodeURIComponent(cashierDayValue()));
    const meta = $('#cashier-meta');
    if (meta) {
      const p = d.policy || {};
      meta.textContent = p.rows
        ? `政策表 ${p.rows} 行 · ${p.fetched_at || '时间未知'} 更新`
        : '政策表还没刷新过（编码反查用）';
    }
    const dl = $('#cashier-sellers');
    if (dl) {
      dl.innerHTML = (d.sellers || [])
        .map((s) => `<option value="${esc(s)}"></option>`).join('');
    }
    renderCashierCards(d.rows || []);
    cashierRenderSummary(d.rows || [], cashierDayValue());
  } catch (e) {
    const host = $('#cashier-cards');
    if (host) host.innerHTML = '<p class="hint">读取失败：' + esc(e.message) + '</p>';
    // ⚠ 失败时汇总条不能留着上一天的 KPI —— 先对到请求日、再把四个数值
    //   改成占位（cashierRenderSummary([], day) 会写 0/0.00，随后被 '—' 覆盖）
    cashierRenderSummary([], cashierDayValue());
    ['cashier-count', 'cashier-qty-sum', 'cashier-amount-sum', 'cashier-acc-sum']
      .forEach((id) => { const el = $('#' + id); if (el) el.textContent = '—'; });
    toast('读取流水失败：' + e.message, 'bad');
  }
}

function cashierDayValue() {
  const el = $('#cashier-day');
  return (el && el.value) || cashierToday();
}

async function cashierRefreshPolicy() {
  const btn = $('#cashier-refresh');
  if (!btn || btn.disabled) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '更新中…（会话掉了会弹登录窗，最长 10 分钟）';
  try {
    const d = await api('/api/cashier/policy-refresh', { method: 'POST', body: {} });
    const last = (d.log && d.log.length) ? d.log[d.log.length - 1] : '';
    toast(`政策表已更新：${d.rows} 行` + (last ? `（${last}）` : ''), 'good');
    await loadCashier();
  } catch (e) {
    toast('政策更新失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function bindCashierEvents() {
  if (_cashierBound) return;
  _cashierBound = true;
  // 扫码枪 = 键盘：输完编码回车 → 立刻反查并把光标送进「实收金额」
  $('#cashier-scan').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      cashierLookup(true);
    }
  });
  $('#cashier-scan').addEventListener('blur', () => cashierLookup(false));
  $('#cashier-save').addEventListener('click', cashierSave);
  $('#cashier-cancel').addEventListener('click', cashierResetForm);
  $('#cashier-day').addEventListener('change', loadCashier);
  $('#cashier-refresh').addEventListener('click', cashierRefreshPolicy);
  // ⚠ 卡片容器（#cashier-cards）的点击委托**故意还没接** —— 编辑是 Task 10、
  //   支付/✕ 是 Task 11 的事。本任务只做视图态渲染，点了没反应是计划内中间态。
}

/* ─────────────── 库存盘点：已拆 iframe、并进同文档（2026-09-22）───────────────

   用户：「真拆掉 iframe，盘点内容并进 #subpanel-inventory」。
   markup 在 `index.html` 的 `.inv-root` 里；样式走 `inventory/style.css`
   （选择器挂在 `.inv-root` 下，和控制台 `.card` 等不互盖）。

   ⚠ 脚本仍**懒注入**：`ui.js` 的 `main()` 一跑就会问后端「账号配好没」、
     还会从 localStorage 恢复账面 —— 不点这一页就别加载。
   ⚠ 扫码枪焦点：同文档后不再 postMessage；切过来 / 鼠标回到面板时
     把焦点还给 `#scan-input`（`ui.js` 自己也有 keydown → focusScan）。
   ⚠ 版本号从 app.js 自己的 `?v=` 里读 —— 跟 index.html 同源。 */
const APP_V = (() => {
  try {
    const s = (document.currentScript && document.currentScript.src) || '';
    const m = s.match(/[?&]v=(\d+)/);
    return m ? m[1] : '';
  } catch (e) { return ''; }
})();
let _invScriptsLoaded = false;

function focusInvScan() {
  const root = $('#subpanel-inventory');
  if (!root || !root.classList.contains('active')) return;
  const si = root.querySelector('#scan-input');
  const scanCard = root.querySelector('#scan-card');
  // 还没进扫码台就别抢焦点（设置表单还要点）
  if (si && scanCard && !scanCard.classList.contains('hidden')) {
    try { si.focus(); } catch (e) {}
  }
}

function mountInventory() {
  if (!_invScriptsLoaded) {
    _invScriptsLoaded = true;
    const v = APP_V ? '?v=' + APP_V : '';
    // 顺序有依赖，别调：core → api → store → xlsx → ui（async=false 保序）
    for (const name of ['core', 'api', 'store', 'xlsx', 'ui']) {
      const s = document.createElement('script');
      s.src = '/inventory/' + name + '.js' + v;
      s.async = false;
      document.body.appendChild(s);
    }
  }
  focusInvScan();
}

/* 小工具（价签 / 工牌，2026-09-22）—— **两个 iframe 各装一格**（用户：拆开）。
   各自懒挂载；切二级只显示对应 subpanel（switchTab 已管）。 */
const TOOLS_SRC = '/tools/price-tag/index.html';
const _toolsMounted = { pricetag: false, badge: false };

function mountTools(tab) {
  const t = (tab === 'badge') ? 'badge' : 'pricetag';
  const f = $('#' + t + '-frame');
  if (!f) return;
  if (!_toolsMounted[t]) {
    f.src = TOOLS_SRC + '?embed=1&tab=' + t;
    _toolsMounted[t] = true;
  } else if (f.contentWindow) {
    // 已挂载再切回来：子页可能被「一键导入」改过内部 tab，同步回去
    try { f.contentWindow.postMessage({ ic: 'tools-tab', name: t }, location.origin); } catch (e) { /* 未就绪 */ }
  }
}
// 子页（价签 iframe）请求切到价签/工牌 —— 一键导入后要跳父页价签，
// 否则 badge 帧内部停在价签、embed 又藏了侧栏，切不回工牌（2026-09-22）。
window.addEventListener('message', (e) => {
  if (e.origin && e.origin !== location.origin) return;
  const d = e.data || {};
  if (d.ic === 'tools-goto' && (d.tab === 'pricetag' || d.tab === 'badge')) {
    switchTab('tools', d.tab);
  }
});

// 鼠标回到盘点面板就还焦点 —— 用户"想扫码"最直接的信号（同文档，直接给扫码框）
$('#subpanel-inventory')?.addEventListener('mousemove', () => {
  focusInvScan();
});
// 焦点在外、按键落在控制台时：同文档下 ui.js 的全局 keydown 会 focusScan；
// 这里只在扫码台可见时补一下（脚本还没加载完时是 no-op）
document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (!$('#subpanel-inventory')?.classList.contains('active')) return;
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (e.key.length === 1) focusInvScan();
});

// ⚠⚠ **一级标签不可点击**（用户 2026-09-19）。它跟左下角那几行是同一个角色：
//   **一行"信息 + 展开器"**（门店·姓名 → 账号设置），真正能点的只有下面那些二级项。
//   ⇒ 所以这里**没有 click 绑定**：一级标签只负责 hover / 聚焦时展开自己那几行。
//   ⚠ 别"顺手"加回 switchTab —— 加回来就又有"点一级标签去哪一页"这个问题了，
//     而用户已经明确说过那一页（介绍页）没用。
// 点空白处收菜单（它是浮层，留着挡视线）
// ⚠ 两处浮层各判各的"在不在自己身上" —— 用一个大判断的话，
//   点设置浮层里的一项会把**顶部导航**的菜单收掉（本来就没开，无害），
//   但点顶部的页签也会把设置浮层收掉（这就对了）。分开写才说得清谁该收。
document.addEventListener('click', (e) => {
  const t = e.target;
  if (!t.closest || !t.closest('.nav-item')) closeNavMenus();
  if (!t.closest || !t.closest('.side-foot')) closeFootMenu();
});

/* ─────────── 左下角：那几行**就地变形**成设置菜单 ───────────

   用户 2026-09-19 先要"鼠标移上去向上浮出一块菜单"，随后**改掉**：

   > 「不是现在弹出悬浮的这个方式，而是**左下角的这个签往上动** ——
   >  门店&姓名这一行变成**账号设置**，版本号这一行变成**检查更新**，
   >  会话导入情况变成**玲珑授权**。人员设置和通用**插缝出现**。
   >  未定时变成**定时器设置**，有新版本跟着版本号走。」

   ⇒ 所以**没有浮层了**：`.foot-item` 里两份内容（`.foot-info` / `.foot-label`）
     叠在同一格交叉淡入淡出，`.foot-gap` 那两条 0 高度撑开。
     **动画全在 CSS 里**（`.side-foot:hover` / `.side-foot.open`），
     JS 只负责加/去 `open` 这个类 —— 悬停和"键盘/触摸打开"走同一条路。

   ⚠ 跟顶部导航同一条规矩：**悬停只展开，点击才跳** ——
     别改成"悬停即切换"，鼠标扫过左下角就切页太容易误触。
   ⚠ 光靠 hover 的话**触屏 / 键盘一辈子打不开**：补 `focusin` + 一次 `click`。
   ⚠ 收起**留宽限期**（`NAV_CLOSE_GRACE`）：指针从这一块挪到里面某一行时，
     中间的 `mouseleave` 会闪一下，不宽限的话菜单会抖。 */

//: 左下角那一行 → 点了去哪儿。
//: `[面板, 二级标签, (要滚到的那张卡的 id)]` ——
//: ⚠ 「检查更新」「定时器设置」**不是独立页面**，它们是「通用」页里的**一张卡**，
//:   所以是"切过去 + 滚到它 + 闪一下"，不是再拆两个页面出来。
//: ⚠ key **必须**和 HTML 里 `.foot-item` 的 `data-foot` 一一对应（有测试盯着）。
//: ⚠ 用 `data-foot` 不用 `data-subtab` —— 后者是"真二级标签"的键空间
//:   （约定是"有子面板 + 有加载器"），而这里有两项只是跳到一张卡。
const FOOT_GO = {
  account: ['settings', 'account'],
  general: ['settings', 'general'],
  // 主题设置：独立二级页（用户 2026-09-22）
  theme: ['settings', 'theme'],
  // 数据交换（M20）—— 2026-09-21 从一级标签搬下来（用户定的），
  // ⚠ 它照旧**只有区长/平台看得见**（后端 `pages`），而且门店调接口是 403。
  stores: ['settings', 'stores'],
  linglong: ['settings', 'linglong'],
  // ⚠ 2026-09-21（用户：「检查更新单独做一个页面」）：从"跳到通用页里那张卡"
  //   改成**真的切到一个二级页** ⇒ 只有两元素（跟 `scheduler` 一样）。
  update: ['settings', 'update'],
  // ⚠ 2026-09-20（用户：「定时器设置单独出来一页」）：
  //   它以前是"跳到通用页里那张卡"（三元素：页签 + 二级 + 滚到哪个 id），
  //   现在是**真的切到一个二级页** ⇒ 只有两元素。
  scheduler: ['settings', 'timer'],
};

let footCloseTimer = null;

function openFootMenu() {
  clearTimeout(footCloseTimer);
  footCloseTimer = null;
  const foot = $('#side-foot');
  if (!foot) return;
  foot.classList.add('open');
  foot.setAttribute('aria-expanded', 'true');
}

function closeFootMenu() {
  clearTimeout(footCloseTimer);
  footCloseTimer = null;
  const foot = $('#side-foot');
  if (!foot) return;
  foot.classList.remove('open');
  foot.setAttribute('aria-expanded', 'false');
}

function scheduleCloseFootMenu() {
  clearTimeout(footCloseTimer);
  footCloseTimer = setTimeout(closeFootMenu, NAV_CLOSE_GRACE);
}

/*: 滚到某张卡并**闪一下** —— 不然切过去只看到一页卡片，不知道点的是哪一张。
 *  只在 `FOOT_GO` 里写了第三项时才调（现在只有检查更新 / 定时器设置）。 */
function flashCard(id) {
  const el = $('#' + id);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.add('flash');
  setTimeout(() => el.classList.remove('flash'), 1400);
}

(function wireFootMenu() {
  const foot = $('#side-foot');
  if (!foot) return;
  foot.addEventListener('mouseenter', openFootMenu);
  foot.addEventListener('mouseleave', scheduleCloseFootMenu);
  foot.addEventListener('focusin', openFootMenu);
  // 焦点离开这一块（去点别处）才收 —— `relatedTarget` 还在里面就不收，
  // 否则从这一块 Tab 到里面第一行的那一瞬间菜单就没了，够不着。
  foot.addEventListener('focusout', (e) => {
    if (!e.relatedTarget || !foot.contains(e.relatedTarget)) scheduleCloseFootMenu();
  });
  // 触屏没有 hover：点一下也算"我够到它了"
  foot.addEventListener('click', openFootMenu);
  // ⚠ 点完再补一个 `openFootMenu` —— `switchTab` 会把菜单收起来，而鼠标还停在这一块上，
  //   不补的话想连着点第二项得先把指针移开再移回来（跟顶部导航那次同一个毛病）。
  Array.from(foot.querySelectorAll('.foot-item[data-foot]')).forEach((b) => {
    const go = FOOT_GO[b.dataset.foot];
    if (!go) return;
    b.addEventListener('click', () => {
      switchTab(go[0], go[1]);
      if (go[2]) flashCard(go[2]);
      if (foot.matches(':hover')) openFootMenu();
    });
  });
})();

/* ───────────────── 彩蛋：门店名称连点 10 次 → 五子棋 ─────────────────
   3s 滑动窗口；只计数、不改抽屉其它行为。
   ⚠ 触发面是**右下角状态悬浮窗**里的「门店名称」那一行（用户 2026-09-22：
     「不是右下角的悬浮窗里的门店名吗」）—— 不是左下角 side-foot。
   ⚠ 行是 `renderStatus` 每次 `innerHTML` 重画的 ⇒ 必须**事件委托**到 #status-rows，
     不能绑死某个 .kv-row 节点（绑节点会在刷新后失效）。 */
const GOMOKU_EGG = { win: 3000, need: 10 };
const gomokuClicks = [];

function gomokuEggDue(now) {
  gomokuClicks.push(now);
  while (gomokuClicks.length && now - gomokuClicks[0] > GOMOKU_EGG.win) {
    gomokuClicks.shift();
  }
  if (gomokuClicks.length >= GOMOKU_EGG.need) {
    gomokuClicks.length = 0;
    return true;
  }
  return false;
}

function openGomoku() {
  const mask = $('#gomoku-mask');
  if (!mask || typeof mountGomoku !== 'function') return;
  mask.hidden = false;
  mountGomoku();
  $('#btn-gomoku-close')?.focus();
}

function closeGomoku() {
  const mask = $('#gomoku-mask');
  if (!mask || mask.hidden) return;
  if (typeof destroyGomoku === 'function') destroyGomoku();
  mask.hidden = true;
}

$('#status-rows')?.addEventListener('click', (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  const row = t.closest('.kv-row');
  if (!row) return;
  const k = row.querySelector('.kv-k');
  if (!k || (k.textContent || '').trim() !== '门店名称') return;
  if (gomokuEggDue(Date.now())) openGomoku();
});
$('#btn-gomoku-close')?.addEventListener('click', closeGomoku);
$('#gomoku-mask')?.addEventListener('click', (e) => {
  if (e.target && e.target.id === 'gomoku-mask') closeGomoku();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' && e.key !== 'Esc') return;
  const mask = $('#gomoku-mask');
  if (!mask || mask.hidden) return;
  closeGomoku();
});

/* ─────────────────── 账号设置（左下角「账号设置」那一行）───────────────────

   用户 2026-09-19：「门店&姓名这一行变成账号设置，点了右边显示**云商账号状态和
   信息**，可以在里面选择**退出登录**或者**切换账号**」。

   ⚠ 数据全部来自 `state.overview`（`store_account` + `profile` + `config.values`）——
     **一个网络请求都不发**（跟 POS / 达成那几页同一个规矩）。
   ⚠ 「退出登录」和「切换账号」调的是**同一个**接口（清登录数据）——
     区别只在**之后**：退出就停在登录页（门禁本来就拦着），
     切换把光标直接放进账号框，省得用户再点一下。
   ⚠ 清完之后**必须重新问一次 `/api/setup`**（`openSetup` 里就是那么做的）——
     不问的话 `setupState` 还是旧的"已登录"，登录页会显示成已经登好了。 */

function _kv(rows) {
  return rows.map(([k, v, cls]) =>
    '<div class="kv-row">'
    + `<span class="kv-k">${esc(k)}</span>`
    + `<span class="kv-v ${cls || ''}">${esc(v == null ? '—' : v)}</span>`
    + '</div>').join('');
}

function renderAccount() {
  const box = $('#account-box');
  if (!box) return;
  const o = state.overview;
  if (!o) { box.innerHTML = '<div class="hint">正在读取…</div>'; return; }
  const a = o.store_account || {};
  const p = o.profile || {};
  const v = (o.config && o.config.values) || {};

  const state_ = a.has_token ? ['已登录', 'ok']
    : (a.has_password ? ['配了账号，但没登录成功过', 'warn'] : ['还没配', 'bad']);
  $('#account-meta').textContent = a.exists ? '' : '（这台机器还没有门店账号文件）';
  box.innerHTML = _kv([
    ['登录账号', a.username || '—'],
    ['姓名', p.who || '—'],
    // ⚠ **不显示公司代码** —— 用户 2026-09-19：「公司代码就不用显示了，都是我们公司的」。
    //   （跟登录页那边一致：那里的「公司代码」输入框 2026-09-19 也拿掉了。）
    ['登录态', state_[0], state_[1]],
  ]);

  $('#account-store-box').innerHTML = _kv([
    ['门店', p.erp_name || v.erp_store_name || '—'],
    ['华为编码', p.huawei_code || v.store_code || '—'],
    ['串号标识', p.marker || '（这家店没有）'],
    ['身份', { platform: '平台岗（不绑门店）', experience: '体验店',
               partner: '合作店' }[p.type] || '—'],
  ]);
}

$('#btn-refresh-account')?.addEventListener('click', async () => {
  await loadOverview();
  renderAccount();
});

/* 「退出登录」/「切换账号」—— 都先清登录数据，再回登录页。
 * ⚠ 清失败**不能装作成功**：那会让人以为退出了，其实没有（这次修的就是这个）。 */
async function _logoutAndSetup(focusAccount) {
  const msg = $('#account-msg');
  if (msg) msg.textContent = '正在清除登录数据…';
  try {
    await api('/api/store-account/logout', { method: 'POST' });
  } catch (e) {
    if (msg) msg.innerHTML = `<span style="color:var(--bad)">清除失败：${esc(e.message)}</span>`;
    return;
  }
  if (msg) msg.textContent = '';
  await openSetup();
  if (focusAccount) $('#sa-username')?.focus();
}
$('#btn-account-logout')?.addEventListener('click', () => _logoutAndSetup(false));
$('#btn-account-switch')?.addEventListener('click', () => _logoutAndSetup(true));

/* ═══════════════════════ 登录门禁（M9）═══════════════════════

   用户 2026-09-18：「我打算做个登录机制，门店鉴权。第一次安装会进入登录页面，
   需要先登录云商再登录玲珑才可以进入正式页面。不登录或者登录失败不给用」。
   随后补了关键一条：「云商登录成功之后看是哪个店，**如果是我们串号标识里有的
   那十四家店需要登录玲珑，其余店不需要**……这就是做的账号、门店权限与内容的划分」。

   ⚠ **挡是后端挡的**（`/api/*` 除了白名单一律 403），这里只是把"该干什么"摆出来。
     所以别把它写成"前端判断不让点"—— 那样别的程序照样能调接口。
   ⚠ 第 ② 步**只有要玲珑的店才出现**（后端 `profile.needs_linglong` 说了算）。 */

let setupState = null;

function showSetup(st) {
  setupState = st || setupState;
  const mask = $('#setup-mask');
  if (!mask) return;
  syncLifehallSettings();
  renderStoreCodeForms();
  const p = (setupState && setupState.profile) || {};
  $('#setup-sub').textContent = p.erp_name
    ? `${p.erp_name}${p.marker ? ' · 标识 ' + p.marker : ''}`
    : '还没认出是哪家店';

  const LH = !!(setupState && setupState.lifehall);   // 生活馆版：只有玲珑一步
  const erp = (setupState && setupState.erp) || {};
  const ll = (setupState && setupState.linglong) || {};
  // ⚠ **好了就别说话** —— 用户 2026-09-19：「切换账号还告诉我 ✅ 门店配置已就绪。
  //   不需要这个东西」。勾（`.done` 把编号变成 ✓）已经说明了，再补一句纯属噪音。
  //   没好才说话，而且说的是**差什么**。
  $('#setup-erp-why').innerHTML = erp.ok ? '' : esc(erp.why || '');
  $('#setup-step-erp').classList.toggle('done', !!erp.ok);

  // ⚠ 合作店**连这一步都不显示**（他们不走玲珑）——
  //   显示成"可选的一步"会让人以为还得登，白折腾。
  // ⚠ 按**身份**判，不看 `needs_linglong` —— 第②步只对体验店出现
  //   （合作店不走玲珑，平台岗不属于任何一家店）。
  // ⚠ 生活馆版（LH）：第①步（云商）整段藏掉，玲珑变第 1 步；
  //   原判据 `p.type === 'experience'` 在生活馆没意义（认店可能失败、type=partner）。
  const needLL = LH || (p.type || '') === 'experience';
  $('#setup-step-erp').hidden = LH;
  $('#setup-step-linglong').hidden = !needLL;
  const llFullNote = $('#setup-linglong-full-note');
  const llLifehallNote = $('#setup-linglong-lifehall-note');
  if (llFullNote) llFullNote.hidden = LH;
  if (llLifehallNote) llLifehallNote.hidden = !LH;
  // 这一步要露脸 ⇒ 把玲珑那套控件搬进来（不露脸就让它待在玲珑授权页）
  mountLinglong(needLL);
  $('#setup-linglong-num').textContent = LH ? '1' : (needLL ? '2' : '');
  $('#setup-linglong-why').innerHTML = ll.ok ? '' : esc(ll.why || '');
  $('#setup-step-linglong').classList.toggle('done', !!ll.ok);
  $('#setup-lead').innerHTML = LH
    ? '这台电脑先把<b>玲珑</b>登录好就能用：<b>抓到会话就放行。</b>'
      + '（生活馆版没有云商那一步。）'
    : needLL
    ? '这台电脑要先把<b>门店的云商账号</b>和<b>玲珑</b>都登录好，才能进主界面。'
      + '<b>不登录、或者登录了但用不了，都不给用。</b>'
    : '这家店（<b>不在串号标识名单里</b>）不走玲珑 —— 只要把<b>云商账号</b>登录好就能用。';

  // ⚠ 2026-09-21（用户：「先把**体验店登录需要玲珑**这个跳过一下……我想看看
  //   **体验店的界面**」）—— 玲珑那步没过时，多给一个「先看看界面」。
  //   `preview_available` 由后端给（= 云商那步过了、只差玲珑），前端不自己判。
  //   生活馆版没有"跳过看界面"这回事（后端恒 False），连段一起藏。
  const pv = $('#setup-step-preview');
  if (pv) pv.hidden = LH || !(setupState && setupState.preview_available);

  mask.hidden = false;
  document.body.classList.add('setup-locked');
}

// 「跳过玲珑，先看界面」—— 只放行**界面**（后端 `set_preview`），
// 抓数/对比/POS 该失败还是失败；进来之后顶上一直挂着预览横幅。
$('#btn-setup-preview')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  const msg = $('#setup-preview-msg');
  if (msg) msg.textContent = '正在打开…';
  try {
    const r = await api('/api/setup/preview', { method: 'POST', body: { on: true } });
    if (msg) msg.textContent = r.message || '';
    toast(r.message || '已进入预览模式');
    hideSetup();
    await loadOverview();
    checkPreviewBanner();
  } catch (e) {
    if (msg) msg.textContent = '没成：' + e.message;
    toast('没成：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
});

/** 预览模式那条横幅 —— 一直挂着，直到用户点「退出预览」或玲珑真的登录上。 */
async function checkPreviewBanner() {
  const box = $('#preview-broken');
  if (!box) return;
  let st = null;
  try { st = await api('/api/setup'); } catch (e) { return; }
  if (!st || !st.preview) { box.hidden = true; return; }
  const why = ((st.linglong || {}).why) || '玲珑还没登录';
  const el = $('#preview-text');
  if (el) {
    el.textContent = `：${why} —— 界面能看，但**抓取玲珑数据 / 双平台对比 / POS 合规`
      + `这三件仍然要会话**（点了会照常报错，不会给假的数）。`;
  }
  box.hidden = false;
}

$('#btn-preview-off')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const r = await api('/api/setup/preview', { method: 'POST', body: { on: false } });
    toast(r.message || '已退出预览');
    const box = $('#preview-broken');
    if (box) box.hidden = true;
    await checkSetup();          // 退出预览 ⇒ 玲珑那步没做的话，回到登录页
  } catch (e) {
    toast('没成：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
});

/*: 玲珑那套抓取控件**只有一份 DOM**，按需在两个宿主之间搬。
 *
 * 用户 2026-09-19：「玲珑授权页面显示不全，输入账密和打开浏览器登录和静默登录那个没了」——
 * 那三个控件原来长在「通用设置 › 玲珑授权」页上，做登录门禁时被**只搬去了登录页**，
 * 那一页就只剩一句状态和一个跳转按钮。现在是**一份 DOM、两个宿主**：
 *
 *   * 平时住在 `#ll-home`（玲珑授权页里）；
 *   * 门禁打开、而且**这家店要走玲珑**时，整块搬进 `#ll-host`（登录页那一步里）。
 *
 * ⚠ **必须是"搬"（`appendChild`），不能是"复制"**：两处各写一份的话 ——
 *   ① id 会重复，`$('#hw-username')` 只命中第一个，另一处永远读不到值；
 *   ② 提示文案、按钮状态会各自漂移（这个项目为"同一份东西写两遍"栽过好几次）。
 * ⚠ `appendChild` 搬**不会丢事件监听**（元素本身没换）；`innerHTML` 重建才会丢 ——
 *   所以这里绝不能写成 `host.innerHTML = panel.outerHTML`。
 * ⚠ 合作店**不搬**：他们不走玲珑，登录页上连那一步都不显示（`hidden`），
 *   控件留在玲珑授权页里就行（那一页本身也被后端 `PAGE_RULES` 藏着）。
 */
function mountLinglong(intoSetup) {
  const panel = $('#ll-panel');
  if (!panel) return;
  const to = intoSetup ? $('#ll-host') : $('#ll-home');
  if (to && panel.parentNode !== to) to.appendChild(panel);
}

function hideSetup() {
  // 门禁收起来 ⇒ 控件搬回「玲珑授权」页，别留在已经藏掉的遮罩里
  mountLinglong(false);
  const mask = $('#setup-mask');
  if (mask) mask.hidden = true;
  document.body.classList.remove('setup-locked');
}

/** 问后端「登录好了没」。好了返回 true（并收起登录页），没好返回 false。 */
async function checkSetup() {
  let st;
  try { st = await api('/api/setup'); } catch (e) { return true; }  // 读不到就别拦着人
  if (st) setupState = st;
  document.body.classList.toggle('lifehall-edition', !!(st && st.lifehall));
  syncLifehallSettings();
  renderStoreCodeForms();
  if (st && st.ready) { hideSetup(); return true; }
  showSetup(st);
  return false;
}

/* ⚠ 原来这里有个「我已登录完，重新检查」—— 用户 2026-09-19 说不要。
   状态现在由「确认登录」成功后**自动**往下走（见 `renderStoreLookup` 尾巴）。 */

// 设置页那两个「重新登录」按钮 —— 只是把登录页顶出来
/* 设置页那两个「重新登录」按钮。
 *
 * ⚠ 它们**不能**调 `checkSetup()` —— 那个函数在"已经能用"时会**把登录页收起来**，
 *   于是一台正常的机器上点「重新登录」= 查完发现没问题 → 页面藏了 → 看着就是
 *   **"点了没反应"**（用户 2026-09-19 报的就是这个，我自己写出来的 bug）。
 *   手动打开的语义是"我要去换账号 / 重抓"，**跟 ready 没关系**。
 */
async function openSetup() {
  // ⚠ 用户 2026-09-19：「重新登陆这个操作应该是**清除掉门店登录数据**的」。
  //   所以它是"退出登录 + 打开登录页"，不是单纯把面板顶出来 ——
  //   不先清的话，登录页会显示"已登录"、而人以为自己已经退出了。
  try {
    await api('/api/store-account/logout', { method: 'POST' });
    toast('已退出登录，重新登录一次', 'ok');
  } catch (e) {
    toast('清除登录信息失败：' + e.message, 'bad');
  }
  try {
    const st = await api('/api/setup');
    if (st) setupState = st;
  } catch (e) { /* 读不到就照上次那份显示 */ }
  if (!setupState) { toast('读不到登录状态', 'bad'); return; }
  showSetup(setupState);
}

// ⚠ `#btn-open-setup`（门店卡里那个"重新登录 / 换账号"）跟着卡一起删了 ——
//   登录入口本来就还有两个：玲珑授权页的 `#btn-open-setup-ll` 和登录门禁本身。
$('#btn-open-setup-ll')?.addEventListener('click', openSetup);
/* ⚠ 「收起」按钮和 Esc 收起都没了（用户 2026-09-19：「下面这个收起 /
   我已登录完，重新检查 不要」）。想走就点「退出」—— 它关掉页面、后台照常跑。 */

/** 按**后端下发的身份**显隐 —— **一个循环，没有 per-功能的判断**。
 *
 *  用户 2026-09-19：「体验店也不是全开，体验店是开**体验店对应的**，合作店是开
 *  **合作店对应的**……你需要把这个**整理到一起**，每个一级标签和二级标签内容上
 *  都加上这个标记。为了以后的开发方便」。
 *
 *  ⚠⚠ 2026-09-21（M17，甲方案）：**可见性表从 HTML 搬到了后端**
 *    （`src/web.py` 的 `PAGE_RULES`，随 `/api/overview.role.pages` 下发）。
 *    原来是每个标签自己写 `data-types="experience platform"`，前端按 `profile.type`
 *    过滤 —— 那是**两份定义**（后端还有一套 `_can_for` 判能写什么），
 *    而"M17 之前每个 `/api/*` 都没鉴权"这件事就是被它遮住的：
 *    页面上藏了，接口照样给。现在**一份表**说了算，前端只渲染。
 *
 *  ⚠ key 就是标签自己的 `data-subtab` / `data-tab` / `data-foot` **值本身**
 *    （`compliance` / `pos` / `linglong`…）—— 前端**不记第二张对照表**，
 *    加新功能 = 后端 `PAGE_RULES` 里加一行，前端一个字都不用动。
 *  ⚠ 后端没给 `pages`（老后端 / 接口坏了）时**全部显示**（fail-open）：
 *    真正的闸门在每个 `/api/*`（`role_scope` + `forbid`），这一层只管"菜单别露"。
 *    藏错方向的话门店会连自己该用的页都找不到，那才是事故。
 *  ⚠ **别在这儿写 if (type === ...) / if (needs_linglong)** —— 那就是第二份可见性表了。
 *  ⚠⚠ 作用域**只有 `#sidebar`**（2026-09-26 2.2.1 bug #1 真踩过）：
 *    可见性表管的是"**菜单**别露"，而这三个属性在页面内容区**另有键空间** ——
 *    库存盘点结果区那排标签也叫 `data-tab="missing"/"extra"/…`（`inventory/ui.js`
 *    动态拼的），`role.pages` 里没有它们 ⇒ 全文档扫的话，每 30 秒那趟
 *    `loadOverview` 跑一次就把整排标签打成 `hidden`，下次重绘又造回来 ⇒
 *    用户看到「表外码那排标签有时会消失」。复现：`.dsh/tasks/repro-inv-tabs-hidden.py`。
 *    三类菜单 key（`data-tab`/`data-subtab`/`data-foot`）都只出现在侧栏里
 *    （`test_roles.py::Test可见性对照` 钉着键表，这里钉着作用域）。
 */
function applyProfile(role) {
  if (!role) return;
  const pages = Array.isArray(role.pages) ? role.pages : null;
  $$('#sidebar [data-tab], #sidebar [data-subtab], #sidebar [data-foot]').forEach((el) => {
    const key = el.dataset.subtab || el.dataset.tab || el.dataset.foot;
    el.hidden = !!pages && !!key && pages.indexOf(key) < 0;
  });
  // 生活馆等裁剪版：首屏固定的 'sales' 可能已被藏 —— 落到第一个可见页签。
  // ⚠ boot 里那次 `switchTab('sales')` 在 `loadOverview()`（本函数的调用点）**之前**，
  //   所以藏完页签时 'sales' 往往正亮着却已经看不见了 ⇒ 首屏一片空白。
  //   `$$` 返回数组（`Array.from(...)`），`find` 直接用；生活馆唯一一级页是 'tools'。
  if (pages) {
    const cur = $$('#sidebar .tab').find((x) => !x.hidden);
    const active = $('#sidebar .tab.active');
    if (active && (!cur || active.hidden)) switchTab(cur ? cur.dataset.tab : 'tools');
  }
  // 「导出 Excel」只给能导的身份（区长 / 平台）—— 用户 2026-09-21：
  //   「区长账号有导出为 excel 功能」「平台也要能导出」「**门店不用导出**」。
  // ⚠ 判据**来自后端**（`role.can`，唯一那一处在 `web._can_for`）——
  //   前端别自己写 `role === 'store'`，那就是第二份判据，迟早跟后端漂
  //   （漂的表现是"按钮藏了、接口还给"，或者反过来）。
  // ⚠ 这里只是"别让它露出来"：真发请求也会被 `/api/attain/export` 403 拦住。
  const exp = $('#btn-export-attain');
  if (exp) exp.hidden = !(role.can && role.can['attain.export']);
  // ⚠ 这个类名现在没有 CSS 用它了（可见性全走 `hidden`）—— 留着是因为
  //   别的会话/样式可能还在读它，而"顺手删干净"的风险比留个空类大。
  document.body.classList.toggle('no-linglong',
    !!pages && pages.indexOf('compliance') < 0);
}

/* ─────────────── 状态悬浮窗（右下角常驻，任何页可开）───────────────

   用户 2026-09-18：「运行日志要一直在、且好找」→ 先是整屏侧滑抽屉；
   同一天又改：「能不能做成**悬浮窗**。悬浮窗打开的话，这个图标还有，
   再点一下图标就关掉了。点旁边非悬浮窗的空白区域也可以关掉」。

   所以出口有**三个**（都得留着）：
   ① 再点一次把手 —— 它现在是**开关**，不再是"打开后就消失"的按钮；
   ② 点窗外的空白处；
   ③ Esc。
   ⚠ 原来打开时把把手藏了（`fab.hidden = open`）—— 那就没法"再点一下关掉"。
     现在**永远不藏**。 */

function setRunDrawer(open) {
  const drawer = $('#run-drawer');
  const fab = $('#btn-drawer');
  if (!drawer) return;
  // 悬浮窗的过渡比菜单长（它在动的是 transform + opacity，且位移更大）
  setVisible(drawer, open, DRAWER_ANIM_MS);
  if (fab) {
    // ⚠ 别动 `hidden` —— 打开时它也得在（用户明确要求"图标还有"）
    fab.setAttribute('aria-expanded', open ? 'true' : 'false');
    fab.title = open ? '收起状态' : '状态';
  }
}

$('#btn-drawer')?.addEventListener('click', () => {
  // ⚠ 判据带上 `.open`：收起动画还在播的时候 `hidden` 仍是 false，
  //   只看 `hidden` 的话这时候点一下会"再关一次"，等于点了没反应。
  const drawer = $('#run-drawer');
  const opening = drawer.hidden || !drawer.classList.contains('open');
  setRunDrawer(opening);
  if (opening) loadStatus();                    // 只在打开时拉一次，关的时候别白跑
});
$('#btn-status-refresh')?.addEventListener('click', loadStatus);
$('#btn-drawer-close')?.addEventListener('click', () => setRunDrawer(false));
// Esc 收起 —— 不用找按钮的出口
// ⚠ 彩蛋遮罩开着时**先别动抽屉**（两条 document 监听同级，拦不住彼此）
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape' && e.key !== 'Esc') return;
  if (!$('#gomoku-mask')?.hidden) return;
  setRunDrawer(false);
});

// 点窗外空白处收起。
// ⚠ 判据要**同时**排掉窗内和把手：只排窗内的话，点把手会先被这里关掉、
//   紧接着把手的 click 又把它打开 —— 看着就是"点了没反应"（这类"两个 handler
//   抢同一次点击"的坑，导航菜单那边也踩过）。
// ⚠ `closest` 之前先确认 `e.target` 是元素 —— 点纯文本节点时它没有 closest。
document.addEventListener('click', (e) => {
  const drawer = $('#run-drawer');
  if (!drawer || drawer.hidden || !drawer.classList.contains('open')) return;
  const t = e.target;
  if (!t || !t.closest) return;
  if (t.closest('#run-drawer') || t.closest('#btn-drawer')) return;
  setRunDrawer(false);
});

/* ────────────────────── 状态抽屉（本机状态）──────────────────────

   用户 2026-09-18：「点击后的悬窗显示目前门店的名称，编码，公司云商账号状态、
   玲珑会话状态，推送哪个通道是开着的，下面是运行一次按钮和日志窗口」。

   ⚠ **判断和措辞全在后端**（`/api/status` → `App.status_brief()`），
     前端只把 `{label, value, kind}` 画成行。放前端的话就没法用 pytest 测 ——
     而"公司云商账号到底算配好了没"恰好是最容易写错、又最不容易看出来的那种判断
     （`describe_credentials` 只读指定那个文件，"这个文件里没有" ≠ "没账号"）。 */

const STATUS_KIND = { ok: 'ok', warn: 'warn', bad: 'bad' };

function renderStatus(rows) {
  const box = $('#status-rows');
  if (!box) return;
  if (!rows || !rows.length) { box.innerHTML = '<div class="hint">读不到状态</div>'; return; }
  box.innerHTML = rows.map((r) => {
    const k = STATUS_KIND[r.kind] || '';
    return `<div class="kv-row">`
      + `<span class="kv-k">${esc(r.label || '')}</span>`
      + `<span class="kv-v ${k}">${esc(r.value == null ? '' : r.value)}</span>`
      + `</div>`;
  }).join('');
}

async function loadStatus() {
  const box = $('#status-rows');
  if (box && !box.dataset.loaded) box.innerHTML = '<div class="hint">正在读取…</div>';
  try {
    const d = await api('/api/status');
    renderStatus(d && d.rows);
    applyProfile(state.overview && state.overview.role);   // 行是刚画的，藏一次
    if (box) box.dataset.loaded = '1';
  } catch (e) {
    if (box) box.innerHTML = `<div class="hint">读不到状态：${esc(e.message)}</div>`;
  }
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
  try {
    renderAttain(await api('/api/attain'));
  } catch (e) {
    // ⚠ 只有**请求本身失败**（网络 / 403）才走这儿；"还没算过"是
    //   `exists: False` + `error`，由 `renderAttain` 说人话。
    $('#attain-meta').textContent = '';
    $('#attain-cards').innerHTML = '';
    $('#attain-table').innerHTML =
      '<div class="empty">读不到达成数据：' + esc(e.message || e) + '</div>';
    toast('读取周度目标达成情况失败：' + e.message, 'bad');
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
    const g = planNormRegion(r.region);
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
  const listEl = $('#attain-hist-list');
  const bodyEl = $('#attain-hist-body');
  try {
    const d = await api('/api/attain/history');
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
    renderAttainHistory(await api('/api/attain/history?period=' + encodeURIComponent(cur)));
  } catch (e) {
    $('#attain-hist-meta').textContent = '';
    bodyEl.innerHTML = '<div class="empty">读不到历史记录：' + esc(e.message || e) + '</div>';
    toast('读取历史记录失败：' + e.message, 'bad');
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
  forceHideAttainPop();
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
  if (d.can_edit !== false) {                          // 只有**门店账号**能改
    const save = document.createElement('tr');
    save.className = 'attain-detail-row attain-detail-save';
    save.dataset.detail = tag;
    // ⚠ 两个按钮（用户 2026-09-20）：「门店设定好目标有个**保存**，还要有个**发送**按钮，
    //   把拆好的目标发送给区长的邮箱」—— 保存只管存，发送才发邮件。
    save.innerHTML = '<td colspan="' + ncols + '">'
      + '<button class="btn primary small" data-detail-save="' + esc(store) + '">保存目标</button>'
      + ' <button class="btn ghost small" data-detail-send="' + esc(store) + '">'
      + '发送给区长</button>'
      + '<span class="hint" data-detail-msg></span></td>';
    frag.appendChild(save);
  }
  tr.parentElement.insertBefore(frag, tr.nextSibling);
  const inserted = Array.from(document.querySelectorAll('[data-detail="' + tag + '"]'));
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

/** 区域名规范化 —— **跟后端 `region_sums` 同一把尺**（strip + 空 → 其他）。
 *  ⚠ 不统一会出现「显示同名、键不同 ⇒ 点了折不起来」（用户 2026-09-22：
 *    「前面名都一样当成不同区域折不起来了」）。 */
function planNormRegion(s) {
  const t = String(s == null ? '' : s).trim();
  return t || '其他';
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
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (planState.anim !== anim) return;                        // 中途又点了一下：让新的那次接管
    planAnimStep();                                             // ② 过渡
  }));
  setTimeout(() => {
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
    const i = PLAN_REGION_ORDER.indexOf(planNormRegion(regions0[store]));
    return i < 0 ? 99 : i;
  };
  const rows = (d.rows || []).map((r, i) => [r, i])
    .sort((a, b) => rankOf(a[0].store) - rankOf(b[0].store) || a[1] - b[1])
    .map((x) => x[0]);
  // 按区域切组（排序后同区已相邻）—— ⚠ 键用 `planNormRegion`，跟 region_sums 一致
  const regionGroups = [];
  rows.forEach((r) => {
    const reg = planNormRegion(regions0[r.store]);
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
    // ⚠ 键走 `planNormRegion`（跟分组、region_sums 同一把尺）——
    //   否则显示一样、Set 里却是两个键，点了折不起来。
    const regText = planNormRegion(row.is_sum
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
    // ⚠ 汇总键已经跟分组键同一把尺（`planNormRegion` / 后端 strip）；
    //   落盘 `region_sums` 为 null 时接口层会重算 —— 这里再兜一层：
    //   没有汇总也**照样收成一行空汇总不行**，继续画门店（宁可不折，也别整区消失）。
    const sums = d.region_sums || {};
    const sum = sums[grp.reg] || sums[planNormRegion(grp.reg)];
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
    return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + regionHtml
      + '<td' + storeAttr + '>' + storeHtml + '</td>'
      + cellNum(r.new) + cellNum(r.target) + cellNum(r.done)
      + cellNum(r.unit_profit)
      + cellRate(r.attach, 'attach')
      + cellNum(r.profit_target) + cellNum(r.baseline)
      + cellNum(r.film_profit) + cellNum(r.gift_profit) + cellNum(r.total_profit)
      + cellRate(r.profit_rate, 'profit')
      + cellRate(r.total_rate, 'rate')
      + cellNum(r.gift_pkg) + cellNum(r.gift_done)
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

async function loadFilm() {
  filmLoading();
  try {
    renderFilm(await api('/api/film'));
  } catch (e) {
    renderFilm({ ok: false, why: '读取防护膜达成失败：' + e.message });
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
    let x = '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
      + leadHtml
      + '<td>' + esc(r.store || '') + '</td>'
      + benefitCell(r.day_target, 'int') + benefitCell(r.slot_progress, 'int')
      + benefitCell(r.new, 'int') + benefitCell(r.new_rate, 'pct', 'overall')
      + benefitCell(r.goal) + benefitCell(r.wuyou) + benefitCell(r.care)
      + benefitCell(r.total)
      + benefitCell(r.attach, 'pct', 'attach')
      + benefitCell(r.attach_goal_rate, 'pct', 'goal')
      + benefitCell(r.overall, 'pct', 'overall');
    tiers.forEach((t) => { x += benefitCell((r.tiers || {})[t.key]); });
    x += benefitCell(r.rebate, 'money') + benefitCell(r.care_profit, 'money')
      + benefitCell(r.tier_profit, 'money') + benefitCell(r.profit_total, 'money')
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
    const d = await api('/api/claim/activities');
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
    const d = await api('/api/claim/pending');
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

async function loadBenefit() {
  benefitLoading();
  try {
    renderBenefit(await api('/api/benefit'));
  } catch (e) {
    renderBenefit({ ok: false, why: '读取无忧会员权益失败：' + e.message });
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
  refreshWithFetch('benefit', e.currentTarget, () => loadBenefit()));
$('#btn-export-benefit')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#benefit-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/benefit/export', { method: 'POST', body: {} });
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

async function loadPlan() {
  try {
    renderPlan(await api('/api/plan'));
  } catch (e) {
    $('#plan-meta').textContent = '';
    $('#plan-warn').innerHTML = '';
    $('#plan-table').innerHTML =
      '<div class="empty">读不到月度数据：' + esc(e.message || e) + '</div>';
    toast('读取月度生意计划失败：' + e.message, 'bad');
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

/* ── 串号追踪（小工具 · 2026-09-23）：86码/SN → 库存+销售全程 ── */
function snTraceCodes(codes) {
  if (!codes || !codes.length) return '';
  return codes.map(c => '<code>' + esc(c) + '</code>').join(' ');
}

function renderSnTrace(d) {
  const box = $('#sn-trace-result');
  const meta = $('#sn-trace-meta');
  const scope = $('#sn-trace-scope');
  if (scope) {
    const bits = [];
    if (d.scope) bits.push('身份：' + d.scope);
    if (d.store_filter) bits.push('销售范围：' + d.store_filter);
    else if (d.scoped === false) bits.push('销售范围：全部门店');
    scope.textContent = bits.join('　·　');
  }
  if (!box) return;
  if (!d.ok) {
    box.innerHTML = '<p class="hint">' + esc(d.why || '查询失败') + '</p>';
    if (meta) meta.textContent = '';
    return;
  }
  if (meta) {
    const bits = [];
    if (d.as_of) bits.push('库存快照至 ' + d.as_of);
    bits.push('命中库存 ' + (d.stock_total || 0) + ' · 销售 ' + (d.sales_total || 0));
    meta.textContent = ' · ' + bits.join(' · ');
  }

  let h = '';
  h += '<div style="margin-bottom:10px;padding:10px;border:1px solid var(--line);border-radius:8px">';
  h += '<div><b>查询：</b>' + esc(d.query) + '　<span class="hint">(' + esc(d.query_label || '') + ')</span></div>';
  if (d.sn) {
    h += '<div><b>真 SN：</b><code>' + esc(d.sn) + '</code></div>';
  } else if (d.query_kind === 'imei') {
    h += '<div class="hint">真 SN：本地库尚未反查到</div>';
  }
  if (d.imeis && d.imeis.length) {
    h += '<div><b>关联 86 码：</b>' + snTraceCodes(d.imeis) + '</div>';
  }
  const rel = (d.related || []).filter(c => c !== d.query && c !== d.sn
    && !(d.imeis || []).includes(c));
  if (rel.length) {
    h += '<div class="hint"><b>其它关联码：</b>' + snTraceCodes(rel.slice(0, 12))
      + (rel.length > 12 ? ' …' : '') + '</div>';
  }
  if (d.note) h += '<div class="hint" style="margin-top:4px">' + esc(d.note) + '</div>';
  h += '</div>';

  h += '<h3 style="margin:12px 0 6px;font-size:14px">库存轨迹'
    + '<span class="hint"> · ' + (d.stock || []).length
    + (d.stock_total > (d.stock || []).length ? ' / ' + d.stock_total : '') + '</span></h3>';
  if (!(d.stock || []).length) {
    h += '<p class="hint">库存快照没有命中（可能已卖出出库、或超出保留天数）。</p>';
  } else {
    h += '<div class="table-scroll" style="max-height:240px;overflow:auto"><table><thead><tr>'
      + '<th>快照日</th><th>仓</th><th>商品</th><th>状态</th><th>串号列</th>'
      + '</tr></thead><tbody>';
    for (const r of d.stock) {
      const serials = [];
      if (r.imei) serials.push('imei=' + r.imei);
      if (r.sub_imei) serials.push('副=' + r.sub_imei);
      if (r.sub_imei1 && r.sub_imei1 !== r.sub_imei) serials.push('副3=' + r.sub_imei1);
      if (r.sn && r.sn !== r.imei) serials.push('sn=' + r.sn);
      h += '<tr><td>' + esc(r.date || '') + '</td>'
        + '<td>' + esc(r.store || '') + '</td>'
        + '<td>' + esc(r.name || '') + '</td>'
        + '<td>' + esc(r.status || '') + '</td>'
        + '<td class="hint">' + esc(serials.join(' · ') || (r.codes || []).join(' ')) + '</td></tr>';
    }
    h += '</tbody></table></div>';
  }

  h += '<h3 style="margin:14px 0 6px;font-size:14px">销售轨迹'
    + '<span class="hint"> · ' + (d.sales || []).length
    + (d.sales_total > (d.sales || []).length ? ' / ' + d.sales_total : '') + '</span></h3>';
  if (!(d.sales || []).length) {
    h += '<p class="hint">销售明细没有命中（或都在身份范围外）。</p>';
  } else {
    h += '<div class="table-scroll" style="max-height:240px;overflow:auto"><table><thead><tr>'
      + '<th>支付时间</th><th>门店</th><th>店员</th><th>商品</th><th>单号</th><th>串号</th>'
      + '</tr></thead><tbody>';
    for (const r of d.sales) {
      h += '<tr><td>' + esc(String(r.ts || '').slice(0, 16)) + '</td>'
        + '<td>' + esc(r.store || '') + '</td>'
        + '<td>' + esc(r.who || '') + '</td>'
        + '<td>' + esc(r.name || '') + '</td>'
        + '<td class="hint">' + esc(r.doc || '') + '</td>'
        + '<td class="hint">' + esc((r.codes || []).join(' ')) + '</td></tr>';
    }
    h += '</tbody></table></div>';
  }

  box.innerHTML = h;
}

async function runSnTrace(code) {
  const q = String(code || '').trim();
  const box = $('#sn-trace-result');
  if (!q) {
    if (box) box.innerHTML = '<p class="hint">请输入 86 码或 SN。</p>';
    return;
  }
  if (box) box.innerHTML = '<p class="hint">查询中…</p>';
  try {
    const d = await api('/api/sn-trace?code=' + encodeURIComponent(q));
    renderSnTrace(d);
  } catch (e) {
    if (box) box.innerHTML = '<p class="hint">查询失败：' + esc(e.message) + '</p>';
    toast('串号追踪失败：' + e.message, 'bad');
  }
}

$('#sn-trace-form')?.addEventListener('submit', (e) => {
  e.preventDefault();
  runSnTrace($('#sn-trace-input')?.value || '');
});
$('#btn-sn-trace')?.addEventListener('click', (e) => {
  e.preventDefault();
  runSnTrace($('#sn-trace-input')?.value || '');
});
$('#btn-refresh-sn-trace')?.addEventListener('click', (e) => {
  const code = $('#sn-trace-input')?.value || '';
  refreshWithFetch('sn-trace', e.currentTarget, () => runSnTrace(code));
});

$('#btn-export-film')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  const box = $('#film-export-result');
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = '导出中…';
  try {
    const r = await api('/api/film/export', { method: 'POST', body: {} });
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

/* 导出为 Excel（用户 2026-09-21：「区长账号有导出为 excel 功能，这个落在
   数据推送模块吧，功能模块调用导出为 excel 来把自己的数据生成为 excel 到本地」）。

   ⚠ 四件事：① **导出的是后端按你的身份过滤过的那一份**（区长=所辖、门店=本店），
     前端不参与筛选 —— 它手里这份 `attainLast` 只是用来画表的；
   ② ⭐ **点完直接调浏览器下载**（用户当天补的：「导出为 excel 我需要**调浏览器下载**，
     **保存到 out 门店和区长找不到**」）—— 文件落到浏览器自己的"下载"目录，
     这才是门店/区长拿得到的地方；
   ③ 服务端 `out/exports/` 那一份**照样留着**，但只是"留个底"（运行记录 + 本机想翻的时候
     有），不再让用户去那儿找文件；
   ④ 失败要说**为什么**（`why`），别只说"导出失败"。 */

/* 触发浏览器下载：造一个 `<a download>` 点一下。
   ⚠ 用 `a.download` 而不是 `location.href = …` —— 后者在有些浏览器里会**导航走**
     （整个控制台被换成一个 xlsx），而这里是 `no-store` 的本地服务，回来还得重新加载。
   ⚠ `download` 只对**同源**生效（我们是同源）；文件名以后端 `Content-Disposition`
   的 `filename*` 为准，这里写 `a.download` 只是兜底。 */
function triggerDownload(url, name) {
  const a = document.createElement('a');
  a.href = url;
  a.download = name || '';
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

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

/* ─────────────── 数据交换 › 每店一张卡（M20，2026-09-21）───────────────

   用户 C3：「**每店一张卡**」；2026-09-21 又定：「放到**左下角的系统设置**，
   一级标签改叫**数据交换**」⇒ 它是左下角那一行，落在「设置」页里。数据**全部来自邮件**（各店发来的 SQLite 附件，
   落进 `in/report.db`）—— 不是这台机器抓的，所以每张卡都要写清
   「这份是哪天、什么时候收的」（四·八验收 6）。

   ⚠ 三条别踩：
     ① **没上报的店也要出卡**（后端按名单给，`known=false`）——
        "哪几家没报"是这个页面最该回答的问题，不列出来等于看不见；
     ② **收信失败时保持原样**：这里只渲染后端给的东西，绝不因为"这次没收上来"
        把卡片清空或改成"—"（那会让人以为数据丢了）；
     ③ 数字全部 `esc()`（这个项目为"表格里放 HTML"踩过三次）。 */

async function loadStores() {
  const box = $('#store-cards');
  if (box && !box.dataset.loaded) box.innerHTML = '<div class="hint">正在读取…</div>';
  let d;
  try {
    d = await api('/api/report/stores');
  } catch (e) {
    // ⚠ 读不到**不能清空**（保持上一次的画面），只把原因说出来
    if (box) box.innerHTML = `<div class="hint">读不到多店视图：${esc(e.message)}</div>`;
    return;
  }
  renderStores(d);
}

function renderStores(d) {
  const box = $('#store-cards');
  if (!box) return;
  box.dataset.loaded = '1';
  const stores = d.stores || [];
  const today = d.today || '';
  $('#stores-meta').textContent =
    `${d.scope && d.scope.label ? d.scope.label + ' · ' : ''}${stores.length} 家店`
    + (today ? ` · 今天 ${today}` : '');
  if (!stores.length) {
    box.innerHTML = '<div class="empty">你的范围里没有门店</div>';
  } else {
    box.innerHTML = stores.map((s) => storeCard(s)).join('');
  }

  // 收信本身的问题（没配收信 / 收不下的包 / 列集变了）—— 有才露出来
  const inbox = d.inbox || {};
  const skips = inbox.skips || [];
  const prob = $('#inbox-problems-card');
  const pb = $('#inbox-problems');
  const lines = [];
  if (!inbox.configured) {
    lines.push(`<div class="banner warn">这台机器<b>没配收信</b>（IMAP）：${
      esc(inbox.why || '')} —— 下面这些卡是<b>以前收到过</b>的数据。</div>`);
  }
  if (skips.length) {
    lines.push(table(['时间', '文件', '为什么没落库'],
                     skips.map((k) => [k.at || '', k.file || '', k.why || ''])));
  }
  if (prob) prob.hidden = lines.length === 0;
  if (pb) pb.innerHTML = lines.join('');
  $('#inbox-problems-meta').textContent = skips.length ? `${skips.length} 条` : '';
}

function storeCard(s) {
  const stale = (s.stale_days === null || s.stale_days === undefined) ? null : s.stale_days;
  const kind = (stale === null || stale > 1) ? ' stale' : '';
  const t = s.tables || {};
  // 各表行数（只在收到过的时候显示）—— 「订单/明细/付款/退货/在库」
  const nums = s.known ? [
    ['订单', (t.orders || {}).rows],
    ['明细', (t.order_lines || {}).rows],
    ['付款', (t.payments || {}).rows],
    ['退货', (t.returns || {}).rows],
    ['在库', (t.lg_stock || {}).rows],
  ].map(([k, v]) => `<span>${k}<b>${v == null ? '—' : v}</b></span>`).join('') : '';
  const src = s.known
    ? `这份来自 <b>${esc(s.report_date || '')}</b> 的邮件`
      + (s.imported_at ? `（${esc(s.imported_at)} 收下）` : '')
      + (stale ? ` · <b>${stale} 天前</b>` : ' · 今天')
      + (s.version ? ` · 门店程序 v${esc(s.version)}` : '')
    : `⚠ ${esc(s.why || '从来没收到过这家店的上报')}`;
  const run = (s.run && s.run.at)
    ? `<div class="store-src">本机那趟：${s.run.ok ? '✅ 跑成' : '⚠ 没跑成'} `
      + `${esc(s.run.at)}${s.run.why ? ' · ' + esc(s.run.why) : ''}</div>`
    : '';
  const cols = (s.cols_changed || []).length
    ? `<div class="store-src">⚠ 列集变了：${esc((s.cols_changed || []).join('、'))}</div>` : '';
  return `<div class="store-card${kind}" data-code="${esc(s.store_code)}" tabindex="0"
       title="点开看这家店最近几天收到过什么">
    <div class="store-head">
      <span class="store-name">${esc(s.store_name || '（名单里没这个名字）')}</span>
      <span class="store-code">${esc(s.store_code)}</span>
    </div>
    <div class="store-src">${src}</div>
    ${nums ? `<div class="store-nums">${nums}</div>` : ''}
    ${run}${cols}
  </div>`;
}

/** 点一张卡 → 这家店最近几天收到过什么（后端已经按范围筛过，不在范围里回 403）。 */
async function openStore(code) {
  const card = $('#store-detail-card');
  const box = $('#store-detail');
  if (!card || !box) return;
  card.hidden = false;
  $('#store-detail-title').textContent = code;
  box.innerHTML = '<div class="hint">正在读取…</div>';
  let d;
  try {
    d = await api('/api/report/store?code=' + encodeURIComponent(code));
  } catch (e) {
    box.innerHTML = `<div class="hint">${esc(e.message)}</div>`;
    return;
  }
  const s = d.store || {};
  $('#store-detail-title').textContent = `${s.store_name || ''}（${code}）`;
  const days = d.days || [];
  const rows = days.map((x) => [
    x.report_date || '',
    String(x.rows_total == null ? '' : x.rows_total),
    x.imported_at || '',
    x.version ? 'v' + x.version : '',
    (x.cols_changed && x.cols_changed !== '[]') ? '⚠ 列集变了' : '',
    esc(x.file || ''),
  ]);
  const t = s.tables || {};
  const perTable = Object.keys(t).map((k) => `${esc(k)} ${(t[k] || {}).rows || 0}`).join(' · ');
  box.innerHTML = `<p class="hint">
      最后一次：<b>${esc(s.report_date || '—')}</b> 的邮件，共 ${s.rows_total || 0} 行
      ${s.generated_at ? '（门店生成于 ' + esc(s.generated_at) + '）' : ''}<br>
      各表行数：${perTable || '—'}
    </p>`
    + table(['日期', '行数', '什么时候收的', '门店版本', '', '文件'], rows);
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

$('#store-cards')?.addEventListener('click', (e) => {
  const c = e.target.closest('.store-card');
  if (c && c.dataset.code) openStore(c.dataset.code);
});
$('#store-cards')?.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const c = e.target.closest('.store-card');
  if (c && c.dataset.code) { e.preventDefault(); openStore(c.dataset.code); }
});
$('#btn-refresh-stores')?.addEventListener('click', () => loadStores());
$('#btn-close-store-detail')?.addEventListener('click', () => {
  const card = $('#store-detail-card');
  if (card) card.hidden = true;
});
$('#btn-fetch-inbox')?.addEventListener('click', async () => {
  const b = $('#btn-fetch-inbox');
  b.disabled = true;
  b.textContent = '收信中…';
  try {
    const r = await api('/api/report/inbox', { method: 'POST' });
    const res = (r && r.result) || {};
    toast(res.skipped ? ('跳过：' + res.skipped)
      : `收到 ${res.ok_packages || 0} 个包（${res.rows || 0} 行）`,
      res.ok === false ? 'bad' : 'ok');
    await loadStores();
  } catch (e) {
    toast('收信失败：' + e.message, 'bad');
  } finally {
    b.disabled = false;
    b.textContent = '收一次';
  }
});

/** 「周度重点产品 › 设置」—— **只讲这条推送**：推不推、走哪个通道。 */
async function loadAttainSettings() {
  const box = $('#attain-set-body');
  if (!box) return;
  let d = {};
  try { d = await api('/api/attain'); } catch (e) { d = {}; }
  const n = d.notify || {};
  const on = n.push_on !== false;
  const pv = n.preview || {};
  box.innerHTML =
    '<div class="form-row" style="margin-bottom:8px">'
    + '<label class="switch"><input type="checkbox" id="pref-attain"'
    + (on ? ' checked' : '') + ' data-pref="attain">'
    + '<span class="switch-track"><span class="switch-knob"></span></span>'
    + '<span>推送周度目标达成</span></label>'
    + '</div>'
    + table(['项', '现在'], [
      ['本条推送', on ? '会推' : '已关闭（这台机器不推）'],
      ['邮件', n.mail_enabled ? '通道已配' : '没配（左侧「通用设置 → 邮件」）'],
      ['企业微信', n.wecom_enabled ? '通道已配' : '没配（左侧「通用设置 → 企业微信」）'],
      ['上次算', d.computed_at || '—'],
      ['期间', (d.period || '—') + '　' + (d.start || '') + ' ~ ' + (d.end || '')],
    ])
    + '<h3 class="hint" style="margin:14px 0 6px">推送内容预览</h3>'
    + '<div style="padding:10px 12px;background:var(--hover);border-radius:8px;'
    + 'font-family:var(--mono);font-size:12px;line-height:1.55;white-space:pre-wrap">'
    + esc(pv.head || '（还没有数据）') + '\n\n'
    + esc((pv.lines || []).join('\n'))
    + '</div>'
    + '<p class="hint">⚠ 功能开关只管「这条推不推」；邮件/企微发不发看「通用设置」。</p>';
}
/** 功能推送开关列表 —— `boxSel` 画到哪；`only` 只画这几个 key（空 = 全部）。
 *  各模块设置页 / 「推送设置」总览**同一套开关**（`notify.prefs`）。 */
async function loadNotifyPrefs(boxSel, only) {
  const box = $(boxSel || '#notify-pref-list');
  if (!box) return;
  const isLifehall = !!(setupState && setupState.lifehall);
  if (box.id === 'notify-pref-list') {
    const title = $('#notify-pref-title');
    const summary = $('#notify-pref-summary');
    const fullNote = $('#notify-pref-full-note');
    const lifehallNote = $('#notify-pref-lifehall-note');
    if (title) title.textContent = isLifehall ? '业务推送' : '功能推送';
    if (summary) summary.textContent = isLifehall
      ? '仅显示生活馆实际可推送项' : '默认关闭 · 与各模块 › 设置同步';
    if (fullNote) fullNote.hidden = isLifehall;
    if (lifehallNote) lifehallNote.hidden = !isLifehall;
  }
  let d = {};
  try { d = await api('/api/notify-pref'); } catch (e) {
    box.innerHTML = '<div class="hint">读不到：' + esc(e.message) + '</div>';
    return;
  }
  const prefs = d.prefs || {};
  let keys = Object.keys(prefs).filter((k) => prefs[k].kind === 'feature');
  if (only && only.length) keys = keys.filter((k) => only.indexOf(k) >= 0);
  if (!keys.length) {
    box.innerHTML = isLifehall
      ? '<div class="hint">生活馆版当前没有单独配置的业务推送项。</div>'
      : '<div class="hint">（这个模块还没有可推的内容）</div>';
    return;
  }
  box.innerHTML = keys.map(function (k) {
    const p = prefs[k];
    return '<div class="form-row" style="margin-bottom:6px">'
      + '<label class="switch"><input type="checkbox" data-pref="' + esc(k) + '"'
      + (p.enabled ? ' checked' : '') + '>'
      + '<span class="switch-track"><span class="switch-knob"></span></span>'
      + '<span>推送' + esc(p.label) + '</span></label></div>';
  }).join('');
}
$('#btn-refresh-attain-set')?.addEventListener('click', loadAttainSettings);
// 业务推送开关（设置页上的滑块）—— 文档级委托，动态渲染也认
document.addEventListener('change', async (e) => {
  const t = e.target;
  if (!t || !t.dataset || !t.dataset.pref) return;
  const key = t.dataset.pref;
  t.disabled = true;
  try {
    const r = await api('/api/notify-pref', {
      method: 'PUT', body: { key: key, enabled: !!t.checked },
    });
    toast((r && r.message) || '已保存');
  } catch (err) {
    t.checked = !t.checked;           // 没存上就拨回去
    toast('保存失败：' + err.message, 'bad');
  } finally {
    t.disabled = false;
  }
});
$('#btn-refresh-timer')?.addEventListener('click', loadTimer);

/* ─────────────── 定时器任务表（用户 2026-09-20）───────────────

   用户原话：
   「要给个接口，让**各个模块设置什么时间点唤醒**、以及唤醒做什么。然后定时器
     看到到点了就做这个。设置要可以设置**日期、时间，每周几，每个月几号**这种」

   ⚠ 这张表**完全由后端读出来**（`/api/timer` → `timer.tasks()`）：
     "做什么"和默认时间来自功能模块自己的声明（`Step.whens`），改过的存这台机器上。
     **前端不许自己写一份步骤清单或默认时间** —— 那就成了两份定义，迟早对不上。
   ⚠ 控件按频率**显隐**（每周→周几、每月→几号、指定日期→日期框），
     但那几格**一直在 DOM 里**（只是 `hidden`），取值时按频率挑用得上的那个。
   ⚠ 按钮和格子都用 `data-timer-*` 标好，事件走**文档级委托**（表格是动态渲染的）。 */

const FREQ_OPTIONS = [['hourly', '每小时'], ['daily', '每天'], ['workdays', '工作日'],
                      ['weekly', '每周几'], ['monthly', '每月几号'], ['once', '指定日期']];
const WD_NAMES = ['一', '二', '三', '四', '五', '六', '日'];

//: 任务表要渲染到哪些容器：`[容器 id, 只看哪个模块（空 = 全部）]`。
//: ⚠ **同一份数据、三个入口**（独立页 + 两个模块设置页）—— 所以只有一个 `loadTimer()`：
//:   各写一份的话，"在 A 页改完、B 页还显示旧值"是迟早的事。
const TIMER_CONTAINERS = [
  ['timer-tasks', ''],                       // 设置 › 定时器设置：全部
  ['timer-tasks-sales', 'sales'],            // 周度重点产品 › 设置
  ['timer-tasks-compliance', 'compliance'],  // 五项合规 › 设置
  ['timer-tasks-valueadd', 'valueadd'],      // 增值 › 设置（2026-09-22）
];

async function loadTimer() {
  const boxes = TIMER_CONTAINERS.map(([id]) => $('#' + id)).filter(Boolean);
  if (!boxes.length) return;
  let d = {};
  try {
    d = await api('/api/timer');
  } catch (e) {
    boxes.forEach((b) => {
      b.innerHTML = '<div class="empty">读不到定时器设置：' + esc(e.message || e) + '</div>';
    });
    return;
  }
  renderTimer(d);
}

function renderTimer(d) {
  const all = (d && d.tasks) || [];
  TIMER_CONTAINERS.forEach(([id, owner]) => {
    const box = $('#' + id);
    if (!box) return;
    const tasks = owner ? all.filter((t) => t.owner_key === owner) : all;
    if (!tasks.length) {
      box.innerHTML = '<div class="empty">这个模块还没有哪一步声明了唤醒时刻</div>';
      return;
    }
    // ⚠ 模块页里**不重复「功能」那一列**（整页都是同一个模块的），独立页里才要 ——
    //   不然一列全是「五项合规、五项合规…」。
    // ⚠ 表头是**纯文本**（`table()` 对字符串默认转义，坑 3），这儿不需要 `{html:…}`。
    const rows = tasks.map((t, i) => timerRow(t, !owner, i, tasks.length));
    // ⚠ 2026-09-20：「跑不跑」那一列**去掉了** —— 它是「自动化跑什么」的产物，
    //   而那个设置取消了 ⇒ 这一列永远写着同一句话，白占宽度。
    // ⚠ 第一列是**那个开关滑块**（用户 2026-09-20：「模块的什么时候自动跑这个功能，
    //   左边加个开关滑块吧，**开了就注册到定时器，不开就不注册**」）。
    // ⚠ 第二列是「顺序」（↑↓ 两个箭头，鼠标移上去才显形）—— **只在独立页**有：
    //   模块页那张表只显示本模块的几步，在那儿调顺序只能表达"这几步之间"的顺序，
    //   整份名单是残缺的（发过去会把别的模块的步骤挤出原来的位置）。
    const head = owner
      ? ['', '', '做什么', '什么时候叫醒（频率 · 时间 · 周几/几号）', '下次', '']
      : ['', '', '功能', '做什么', '什么时候叫醒（频率 · 时间 · 周几/几号）', '下次', ''];
    const cls = owner ? ['sw-cell', 'mover-cell', '', '', 'nowrap-cell', 'right']
                      : ['sw-cell', 'mover-cell', '', '', '', 'nowrap-cell', 'right'];
    // 「唤醒设置读出来有问题」那条只在独立页说一次（模块页重复三遍没意义）
    const warn = (!owner && (d.problems || []).length)
      ? `<div class="banner warn" style="margin-top:8px">唤醒设置读出来有问题（这几步先用默认时间）：<br>${
          (d.problems || []).map((p) => esc(p)).join('<br>')}</div>` : '';
    box.innerHTML = table(head, rows, cls) + warn;
  });
  renderTimerHero((d && d.next_run) || {}, (d && d.state) || {});
  renderWakeLog((d && d.history) || []);
  const win = $('#timer-window');
  if (win && d && d.knobs) win.textContent = String(d.knobs.window_minutes || 30);
  renderTimerState((d && d.state) || {});
  // 两个模块页的标题上也写一句"下次什么时候"（各自那几步里最早的那个）
  [['timer-meta-sales', 'sales'], ['timer-meta-compliance', 'compliance']]
    .forEach(([id, owner]) => {
      const el = $('#' + id);
      if (!el) return;
      const mine = all.filter((t) => t.owner_key === owner && t.enabled
                                  && t.next_text && t.next_text !== '—');
      el.textContent = mine.length ? '下次 ' + mine.map((t) => t.next_text).sort()[0] : '';
    });
}

/** 定时器设置页里那两块「系统计划任务 / 自动化跑什么」——
 *  数据在总览里（`/api/overview`），所以先确保总览拉到过。
 *  ⚠ 它原来挂在「通用」页的 `loadConfig()` 上；卡片搬走之后**必须跟着搬**，
 *    否则新页面上那两块永远是空的（而"空"看着像"还没注册任务"）。 */
async function loadSchedulerBits() {
  if (!state.overview) await loadOverview();
  const o = state.overview || {};
  // ⚠ 只剩「旧的系统计划任务」那块了（「自动化跑什么」2026-09-20 取消）
  renderSchedule(o.schedule || {});
}

/** 顶上那行**大字**：下一次**什么时候**、**唤醒了什么**（用户 2026-09-20）。
 *
 *  ⚠ 措辞全用后端给的（`next_run.at_text` / `.label`）——
 *    前端不自己算"下一步是哪个"（定时器算的那份才算数，两处各算必然分叉）。
 */
function renderTimerHero(nxt, st) {
  const box = $('#timer-hero');
  if (!box) return;
  if (st && st.running) {
    // ⚠ 正在跑的时候，"下次"不是最该看的 —— 这一眼该看到"它现在在跑"
    box.innerHTML = '<div class="timer-hero-k">此刻</div>'
      + '<div class="timer-hero-time">正在跑…</div>'
      + '<div class="timer-hero-what">跑完会自动回到"下一次"。</div>';
    return;
  }
  if (!nxt || !nxt.at) {
    box.innerHTML = '<div class="timer-hero-none">没有哪一步设了唤醒时刻 —— '
      + '不会自己跑（要到下面加一个时间点）</div>';
    return;
  }
  box.innerHTML = '<div class="timer-hero-k">下一次自动跑</div>'
    + `<div class="timer-hero-time">${esc(nxt.at_text || nxt.at)}</div>`
    + `<div class="timer-hero-what">${esc(nxt.label || '')}</div>`;
}

/** ⭐ 执行日志表（用户 2026-09-20：「记录什么时间唤醒了什么，成功了没」）。 */
function renderWakeLog(rows) {
  const box = $('#wake-log');
  if (!box) return;
  const meta = $('#wake-log-meta');
  if (meta) meta.textContent = rows.length ? `最近 ${rows.length} 次` : '';
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有执行记录 —— 到点跑过一次之后就有了'
      + '（手动跑不算，这里只记定时器叫醒的那几趟）。</div>';
    return;
  }
  const body = rows.map((w) => [
    // ⚠ 用 `{html:…}` 的格子才不会被转义（AGENTS.md 坑 3）
    { html: `<span class="mono">${esc(w.slot || w.at || '')}</span>` },
    { html: esc(w.label || '') },
    w.ok
      ? { html: '<b style="color:var(--ok)">成功</b>' }
      : { html: `<b style="color:var(--bad)">失败</b>`
                + (w.why ? `<br><span class="hint">${esc(w.why)}</span>` : '') },
    { html: w.seconds == null ? '—' : esc(String(w.seconds)) + ' 秒' },
  ]);
  box.innerHTML = table(['时间点', '唤醒了什么', '结果', '用时'], body,
                        ['nowrap-cell', '', '', 'nowrap-cell']);
}

/** 状态那一行：**此刻在不在跑 / 今天跑完没 / 上次什么结果**。
 *
 *  ⚠ 三个分开说，别揉成一句 —— 门店报"今天没跑"时，要一眼分清是
 *    "还没到点"、"正在跑"、还是"跑了但失败"。
 */
function renderTimerState(st) {
  const box = $('#timer-state');
  if (!box) return;
  const last = st.last || {};
  const bits = [];
  bits.push(st.running
    ? '<span class="bad-text">此刻正在跑</span>'
    : '<span class="hint">此刻没在跑</span>');
  bits.push(st.ran_today
    ? '<b>今天已经跑完了</b>'
    : '<span class="hint">今天还没跑（还没到点，或者错过了就不补）</span>');
  if (last.slot) {
    const what = (last.steps || []).join('+') || '—';
    bits.push((last.ok ? '上次 ✅ ' : '上次 ❌ ')
      + esc(last.slot) + ' ' + esc(what)
      + (last.ok ? '' : '（退出码 ' + esc(String(last.exit_code)) + '）'));
  } else {
    bits.push('<span class="hint">还没有跑过</span>');
  }
  box.innerHTML = '<span class="hint">到点由<b>服务自己</b>叫醒'
    + '（不用 Windows 计划任务）——</span> ' + bits.join('　·　')
    + ((st.problems || []).length
        ? '<br><span class="bad-text">唤醒设置有问题：'
          + (st.problems || []).map((p) => esc(p)).join('；') + '</span>' : '');
}

/** 一行 —— 六个格子：功能 / 做什么 / 跑不跑 / 什么时候 / 下次 / 按钮。 */
function timerRow(t, withOwner, idx, total) {
  const w = (t.whens && t.whens[0]) || { kind: 'daily', time: '21:00' };
  const wds = w.weekdays || [];
  // ⚠ 「工作日」在后端就是 `weekly` + 周一~周五 —— 界面上单列一个选项，
  //   否则用户得自己去点五个小圆点（用户明确要的"工作日"就是它）。
  const workdays = w.kind === 'weekly' && wds.length === 5 && wds.every((x) => x <= 5);
  const freq = workdays ? 'workdays' : w.kind;
  const picks = WD_NAMES.map((n, i) =>
    `<button type="button" class="wd-pick${wds.indexOf(i + 1) >= 0 ? ' on' : ''}"`
    + ` data-wd="${i + 1}">${n}</button>`).join('');
  const opts = FREQ_OPTIONS.map(([k, label]) =>
    `<option value="${k}"${k === freq ? ' selected' : ''}>${label}</option>`).join('');
  const extras =
    `<span data-extra="weekly"${freq === 'weekly' ? '' : ' hidden'}> `
      + `<span class="wd-picks">${picks}</span></span>`
    + `<span data-extra="workdays"${freq === 'workdays' ? '' : ' hidden'}>`
      + `<span class="hint">周一 ~ 周五</span></span>`
    + `<span data-extra="monthly"${freq === 'monthly' ? '' : ' hidden'}> `
      + `<input type="number" data-day min="0" max="31"`
      + ` value="${Number(w.day == null ? 1 : w.day)}">`
      + `<span class="hint"> 几号（0 = 最后一天）</span></span>`
    + `<span data-extra="once"${freq === 'once' ? '' : ' hidden'}> `
      + `<input type="date" data-date value="${esc(w.date || '')}"></span>`
    // ⚠ **每小时没有"几点"**，只有"每小时的第几分钟" —— 后端 `When.minute`
    //   是独立字段，别拿 `time` 的小时那半截去凑（那种约定读代码的人一定看错）。
    + `<span data-extra="hourly"${freq === 'hourly' ? '' : ' hidden'}> `
      + `<input type="number" data-minute min="0" max="59"`
      + ` value="${Number(w.minute == null ? 0 : w.minute)}">`
      + `<span class="hint"> 每小时的第几分钟</span></span>`;
  // ⚠ 归属（哪个模块的）**后端给**（`registry.step_owner`）——
  //   这儿以前写死过 `t.cmd === 'dump' ? '数据抓取' : …`，那是**第二份定义**：
  //   哪天某一步换了模块，界面上会指错地方，而只有肉眼能发现。
  // ⭐ 那个开关滑块（左边第一格）。复用「标色」那个 `.switch` 组件（滑块的样子统一）：
  //   `input:checked + .switch-track` 管位移，`data-timer-toggle="<cmd>"` 给事件委托认。
  //   ⚠ 它是**唯一**的"跑不跑"入口 —— 关掉之后那一行的时间控件也一并禁用（见下面）。
  //   ⚠⭐ **必做的那几步锁死**（用户 2026-09-20：「自动更新和数据抓取模块不允许关闭」）：
  //     滑块画成 checked + disabled，鼠标放上去说清"为什么关不掉"。
  //     ⚠ 这只是"看起来不能点" —— **后端也会拒**（`timer.set_enabled` 抛错），
  //       两边都要有：前端画灰挡不住 curl 和老缓存页面。
  // ⭐ 2026-09-21（用户：「**这个第一个开不了，是啥情况**」）——
  //   `whens=()` 的步骤**没有自己的唤醒时刻**：它只能跟着每天那趟整批跑。
  //   原来这儿照样画了一个开关 ⇒ 它永远是关的、而且**点不动**
  //   （后端没有时刻可注册）⇒ 看起来像"这功能坏了"。
  //   ⇒ 这一类行**不给开关、不给时间控件**，改一句话说清它跟谁跑。
  //   ⚠ 现在**没有这样的步骤**了（「上报数据」2026-09-21 晚拿到了自己的 21:15）——
  //     这个分支是给"以后真要加一个只跟整批的步骤"留的，别顺手删。
  const follows = t.declared === false;
  const lockWhy = t.required
    ? '必做的一步，关不掉 —— 不抓数本地库永远是旧的，后面的分析只是拿旧数据在算（时间可以改）'
    : '';
  const sw = follows ? { html:
    '<span class="hint" title="这一步没有自己的唤醒时刻 —— 只能跟着每天那趟'
    + '整批跑（命令行 `daily`），所以没有开关">跟整批</span>' } : { html:
    `<label class="switch${t.required ? ' switch-locked' : ''}"`
    + ` title="${t.required ? lockWhy
        : (t.enabled ? '已注册到定时器：到点会叫它'
                     : '没注册：到点不会叫它（时间点留着）')}">`
    + `<input type="checkbox" data-timer-toggle="${esc(t.cmd)}"`
    + `${t.enabled ? ' checked' : ''}`
    + `${t.required ? ' disabled' : ''}>`
    + '<span class="switch-track"><span class="switch-knob"></span></span></label>' };
  // ⭐ **顺序**（用户 2026-09-21：「相同时间执行的任务，按照定时器这个列表从上到下执行，
  //   然后定时器列表给个调顺序的功能」）——鼠标移到这一行才显形（平时不占视线）。
  //   ⚠ 两个箭头是 disabled 而不是"藏起来"：首尾两行也得看得出"这儿到底能不能动"。
  //   ⚠ 只有独立页（`withOwner`）给这一格 —— 模块页的名单是残缺的，见上面那段。
  //   ⭐ 2026-09-23：两个数据抓取**钉在最前**（后端 `set_order` 也会强制）——
  //     箭头留着但禁用：`data-timer-move` 还在，整表调顺序时名单才不会漏掉它们。
  const fetchLock = t.cmd === 'dump' || t.cmd === 'erp-dump';
  const mover = !withOwner ? { html: '' } : { html:
    '<span class="row-mover">'
    + `<button class="btn ghost small" data-timer-move="up" data-cmd="${esc(t.cmd)}"`
    + ` title="${fetchLock
        ? '两个数据抓取固定排最前（时间也保持一致）'
        : '上移（同一时刻到点的几步按这张表从上到下跑）'}"`
    + `${fetchLock || idx <= 0 ? ' disabled' : ''}>↑</button>`
    + `<button class="btn ghost small" data-timer-move="down" data-cmd="${esc(t.cmd)}"`
    + ` title="${fetchLock
        ? '两个数据抓取固定排最前（时间也保持一致）'
        : '下移（同一时刻到点的几步按这张表从上到下跑）'}"`
    + `${fetchLock || idx >= (total || 1) - 1 ? ' disabled' : ''}>↓</button>`
    + '</span>' };
  const cells = [
    mover,
    // ⚠ 「这台机器改过」那行小字 2026-09-21 去掉了（用户：「这个东西不用要了」）——
    //   「默认」按钮也没了，标着它只会让人困惑"那我怎么改回去"。
    { html: esc(t.label) },
    // ⚠ 关掉的那一行**控件禁用、但不隐藏**：时间点还在（再打开就是它），
    //   藏起来的话人会以为"关掉把设置也清了"。
    follows ? { html: '<span class="hint">没有自己的时间 —— 跟着每天那趟'
                      + '整批一起跑（`daily`，到点由定时器叫）</span>' }
            : { html: `<span class="${t.enabled ? '' : 'timer-off'}">`
            + `<select data-freq title="多久叫醒一次"${t.enabled ? '' : ' disabled'}>`
            + `${opts}</select> `
            + `<input type="time" data-time value="${esc(w.time || '21:00')}"`
            + `${t.enabled ? '' : ' disabled'}`
            // 每小时那一档没有"几点"，时间框藏起来（取值时也不读它）
            + `${freq === 'hourly' ? ' hidden' : ''}>` + extras + '</span>' },
    { html: follows ? '<span class="hint">跟整批</span>'
            : (t.enabled
               ? `<span class="hint">${esc(t.next_text || '—')}</span>`
               : '<span class="hint">没注册</span>') },
    // ⚠ 2026-09-21（用户：「**我们不需要默认设置，去掉这个按钮就行了**」）——
    //   这里原来还有一个「默认」按钮（`data-timer-reset`，"恢复成模块声明的默认时间"）。
    //   去掉的是**按钮**：`PUT /api/timer {whens: []}` 那条路后端仍然认
    //   （老缓存页面点它照样是"恢复默认"，提示语也对，见 `timer.set_whens`）——
    //   只是界面上不再给这个入口了。
    //   ⚠ 「这台机器改过」那行小字**留着**：它说的是"这个时间不是模块给的默认值"，
    //     跟有没有那个按钮无关（去掉按钮 ≠ 把改过的痕迹也藏起来）。
    { html: follows ? ''
            : `<button class="btn small primary" data-timer-save="${esc(t.cmd)}">保存</button>`
            + `<span class="hint" data-timer-msg></span>` },
  ];
  // 独立页那一份多一列「功能」；模块页里整页同一个模块，不重复
  const rest = withOwner ? [{ html: `<b>${esc(t.owner_label || '')}</b>` }].concat(cells) : cells;
  return [sw].concat(rest);
}

/** 把某一行的控件读成后端要的形状（`When` 的 `as_dict`）。
 *
 *  ⚠ 读不出来**抛错**（周几一天没选、指定日期空着）—— 不许悄悄拼一个默认值：
 *    那会变成"界面显示每周一、实际在每天跑"。
 */
function timerRowValue(tr) {
  const freq = (tr.querySelector('[data-freq]') || {}).value || 'daily';
  const time = (tr.querySelector('[data-time]') || {}).value || '21:00';
  // ⚠ 每小时那一档**只读 `minute`**（它没有"几点"）——
  //   顺手把 time 也带上就会存进一个用不着的 21:00，看着像"每天 21 点"。
  if (freq === 'hourly') {
    const m = Number((tr.querySelector('[data-minute]') || {}).value || 0);
    if (!(m >= 0 && m <= 59)) throw new Error('每小时的第几分钟要在 0~59');
    return { kind: 'hourly', minute: m };
  }
  if (freq === 'workdays') return { kind: 'weekly', weekdays: [1, 2, 3, 4, 5], time: time };
  if (freq === 'weekly') {
    const wds = Array.from(tr.querySelectorAll('.wd-pick.on'))
      .map((b) => Number(b.dataset.wd)).sort((a, b) => a - b);
    if (!wds.length) throw new Error('每周几至少要选一天');
    return { kind: 'weekly', weekdays: wds, time: time };
  }
  if (freq === 'monthly') {
    const day = Number((tr.querySelector('[data-day]') || {}).value || 0);
    if (!(day >= 0 && day <= 31)) throw new Error('每月几号要在 0~31（0 = 最后一天）');
    return { kind: 'monthly', day: day, time: time };
  }
  if (freq === 'once') {
    const date = (tr.querySelector('[data-date]') || {}).value || '';
    if (!date) throw new Error('「指定日期」要选一个日期');
    return { kind: 'once', date: date, time: time };
  }
  return { kind: 'daily', time: time };
}

/** 切频率 → 把对应的那几格露出来（其余藏起来，取值时按频率挑）。 */
function timerSyncExtra(tr) {
  const freq = (tr.querySelector('[data-freq]') || {}).value || 'daily';
  Array.from(tr.querySelectorAll('[data-extra]')).forEach((el) => {
    el.hidden = el.dataset.extra !== freq;
  });
  // ⚠ 「每小时」没有"几点" ⇒ 把时间框藏起来（不然会看着像"每天 21 点"）。
  const clock = tr.querySelector('[data-time]');
  if (clock) clock.hidden = (freq === 'hourly');
}

/** 这个控件在**哪一张表**里 —— 定时器那张表页面里有**三份**（独立页 + 两个模块页）。
 *
 * ⚠⚠ 2026-09-21 实测踩到：不限定的话 `document.querySelector('[data-timer-save="pos"]')`
 *   匹到的是**文档里第一份**，而那多半在**另一页那张隐藏的表**里 ——
 *   于是"已保存"写进了一个用户根本看不见的地方（页面上像什么都没发生，
 *   实测截图里那句就是空的）。
 */
function timerScope(el) {
  const box = el && el.closest && el.closest('[id^="timer-tasks"]');
  return box ? box.id : '';
}

/** 把一句话写到**那一行**的确认位。
 *
 * ⚠ 两个坑一起兜：
 *   ① 按 `cmd` + **表**找行，不存元素 —— `loadTimer()` 会把整张表重画一遍，
 *      之前抓到的那个 `[data-timer-msg]` 已经是**被扔掉的那个节点**了；
 *   ② 必须限定在哪一张表里（见 `timerScope`）—— 三张表长得一模一样，
 *      不限定就会写到隐藏的那张里去。
 */
function timerSay(cmd, text, scope) {
  const root = (scope && document.getElementById(scope)) || document;
  const b = root.querySelector('[data-timer-save="' + cmd + '"]');
  const m = b && b.closest('tr') && b.closest('tr').querySelector('[data-timer-msg]');
  if (m) m.textContent = text;
}

async function timerSave(cmd, whens, btn) {
  const scope = timerScope(btn);
  try {
    const res = await api('/api/timer', { method: 'PUT', body: { cmd: cmd, whens: whens } });
    // ⚠⚠ 反馈**只写在那一行里**，不弹 toast（2026-09-21 用户：「单击保存会弹出来
    //   一些奇怪的东西，**会突然消失**」）——
    //   那句 toast 飘在页面中间、两三秒就没了：既不知道是哪一行的，也来不及看。
    //   行里这句贴着「保存」按钮，**留着不动**，直到你下次点这一行。
    // ⚠⚠ 2026-09-21 用户：「**✅ 没改动：本来就是每天 21:00（下一趟 …）就这句，
    //   不要显示了**」——点「保存」但一个字没动时，**什么都不显示**：
    //   没发生任何事，就不该说话（这句话原来还又长又重复：时间和下次旁边那两列
    //   本来就写着）。`changed` 是后端算的"真改了没"。
    if (res.changed === false) {
      await loadTimer();
      return;
    }
    const text = '✅ ' + (res.message || '已保存');
    timerSay(cmd, text, scope);
    await loadTimer();                 // 重新读一遍：下次时间要跟着变
    timerSay(cmd, text, scope);        // ⚠ 重画会把行里那句抹掉 ⇒ 画完再写回去
  } catch (e) {
    timerSay(cmd, '❌ ' + e.message, scope);   // 失败也写在行里（红字），不弹窗
  }
}

// 文档级委托：表格是动态渲染的，绑在行上会随重画一起丢
document.addEventListener('change', (e) => {
  const sel = e.target.closest && e.target.closest('#timer-tasks [data-freq]');
  if (sel) timerSyncExtra(sel.closest('tr'));
});

// ⭐ **那个开关滑块**（用户 2026-09-20：「开了就注册到定时器，不开就不注册」）。
//   ⚠ 拨完**重新拉一遍**（不自己在本地翻转状态）—— "注册没注册"的真相在后端，
//     本地翻转的话，后端拒绝（比如认不出的 cmd）时界面会显示成"开着的"。
document.addEventListener('change', async (e) => {
  const box = e.target.closest && e.target.closest('[data-timer-toggle]');
  if (!box) return;
  const cmd = box.dataset.timerToggle;
  box.disabled = true;
  try {
    const res = await api('/api/timer', {
      method: 'PUT', body: { cmd: cmd, enabled: !!box.checked } });
    // ⚠ 也写**行里**、不弹 toast（跟「保存」一个道理，见 `timerSave` 那段）——
    //   拨开关的反馈本来就看得见（滑块位置），再加一句解释就够，别飘一个窗。
    const text = '✅ ' + (res.message || '已保存');
    const scope = timerScope(box);
    timerSay(cmd, text, scope);
    await loadTimer();
    timerSay(cmd, text, scope);       // 重画之后再写回去
    loadSchedulerBits();          // 顶上"下一次"可能变了（那一步刚注册/注销）
  } catch (err) {
    // ⚠ 必做那几步后端会**拒**（400「关不掉」）—— 那不是 bug，是设计：
    //   把话原样给用户看，并把滑块拨回去。
    timerSay(cmd, '❌ ' + err.message, timerScope(box));
    box.checked = !box.checked;   // 失败就拨回去（别让它显示成"改成了"）
  } finally {
    box.disabled = false;
  }
});
document.addEventListener('click', (e) => {
  const wd = e.target.closest && e.target.closest('#timer-tasks .wd-pick');
  if (wd) {
    // 点一下切换选中（不用原生 multi-select：Mac/Win 上长得完全不一样）
    wd.classList.toggle('on');
    return;
  }
  const save = e.target.closest && e.target.closest('[data-timer-save]');
  if (save) {
    let val;
    try {
      val = timerRowValue(save.closest('tr'));
    } catch (err) {
      toast(err.message, 'bad');
      return;
    }
    timerSave(save.dataset.timerSave, [val], save);
    return;
  }
  // ⚠ 「默认」按钮 2026-09-21 去掉了（用户：「不需要默认设置」）——
  //   它的分支（`timerSave(cmd, [], btn)`）也一起删，别留一段没人走得到的代码。
});

/* ⭐ **调执行顺序**（用户 2026-09-21：「**相同时间执行的任务，按照定时器这个列表
   从上到下执行**，然后定时器列表给个调顺序的功能」）。

   ⚠ 发的是**整张表的顺序**（不是"把某一步上移一格"）：后端不用猜"现在什么顺序"，
     也不会因为两次点击之间别人改过而错位。
   ⚠ 顺序名单只取**有箭头的那几行**（`follows` 的行没有时刻，本来就不参与）——
     也就是"定时器设置"那一页看到的全部步骤。
   ⚠ 改完**重新拉一遍**（不自己在本地换行）：顺序是后端说了算，
     本地换了但后端没存上的话，界面会显示成一个假的顺序。 */
document.addEventListener('click', async (e) => {
  const mv = e.target.closest && e.target.closest('[data-timer-move]');
  if (!mv || mv.disabled) return;
  const tr = mv.closest('tr');
  const tb = tr && tr.closest('tbody');
  if (!tb) return;
  const cmds = Array.from(tb.querySelectorAll('tr'))
    .map((r) => {
      const b = r.querySelector('[data-timer-move]');
      return b ? b.dataset.cmd : '';
    }).filter(Boolean);
  const i = cmds.indexOf(mv.dataset.cmd);
  const j = mv.dataset.timerMove === 'up' ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= cmds.length) return;
  const swap = cmds[i]; cmds[i] = cmds[j]; cmds[j] = swap;
  mv.disabled = true;
  try {
    const res = await api('/api/timer', { method: 'PUT', body: { order: cmds } });
    // ⚠ 也不弹窗：顺序变了**表里自己看得见**（那行跑到别处去了），
    //   再补一句"已上移/已下移"写到那一行就够。
    const scope = timerScope(mv);
    await loadTimer();
    timerSay(mv.dataset.cmd,
             '✅ ' + (mv.dataset.timerMove === 'up' ? '已上移' : '已下移'), scope);
  } catch (err) {
    timerSay(mv.dataset.cmd, '❌ ' + err.message, timerScope(mv));
    loadTimer();
  }
});

/* ───────────────────────────── POS 合规 ───────────────────────────── */

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

$('#btn-refresh-pos') && $('#btn-refresh-pos').addEventListener('click', (e) =>
  refreshWithFetch('pos', e.currentTarget, () => loadPos()));

/* ───────────────────────────── 总览 ───────────────────────────── */

/* 上次升级没走完 → 横幅 + 两个修复按钮（阶段 1.4d）。
   ⚠ 判据**全在后端**（`overview.update_pending`）：前端不自己判断
   "有没有断在半路"，那种状态放前端一定会漂（跟 `legacy_prompt` 一个道理）。 */
function renderUpdateBroken(j) {
  const box = $('#update-broken');
  if (!box) return;
  const st = (j && j.state) || '';
  if (!st) { box.hidden = true; return; }
  const done = (j.done || []).length;
  const el = $('#ub-text');
  if (el) {
    el.textContent = `（目标是 v${j.to || '?'}，已经改了 ${done} 个文件`
      + `${j.error ? '：' + j.error : ''}）`;
  }
  box.hidden = false;
}

async function doUpdateRepair(how, btn) {
  const label = btn ? btn.textContent : '';
  if (!confirm(how === 'restore'
    ? '退回升级前的代码？\n\n门店配置、账号会话、历史报告都不会动。'
    : '用上次下载好的包重新铺一遍？\n\n不用联网。')) return;
  if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
  try {
    const r = await api('/api/update', { method: 'POST', body: { repair: how } });
    if (!r.ok) {
      toast(r.message || '没成功', 'bad');
      if (btn) { btn.disabled = false; btn.textContent = label; }
      return;
    }
    toast(r.message || '好了，控制台正在重启…', 'ok');
    setTimeout(() => {
      const t = setInterval(async () => {
        try { await api('/api/health'); clearInterval(t); location.reload(); } catch (e) {}
      }, 2000);
    }, 3000);
  } catch (e) {
    toast('处理失败：' + e.message, 'bad');
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

$('#btn-update-repair') && $('#btn-update-repair').addEventListener('click', (ev) => {
  doUpdateRepair('rerun', ev.currentTarget);
});
$('#btn-update-restore') && $('#btn-update-restore').addEventListener('click', (ev) => {
  doUpdateRepair('restore', ev.currentTarget);
});

/* 数据没到位 → 一条黄横幅（M14 / 阶段 3.3）。
   ⚠ 判据**全在后端**（`overview.data_state`）：前端不自己判断"数据够不够新"，
   那种判据放前端一定会跟后端漂（跟 `update_pending` / `legacy_prompt` 一个道理）。 */
function renderDataState(ds) {
  const box = $('#data-broken');
  if (!box) return;
  // ⚠ `dismissed` 由**后端**判（比对"关掉过的指纹"和"现在这条的指纹"）——
  //   前端不自己记，那种状态放前端一定会漂（跟"弹过没"一个道理）。
  if (!ds || ds.ok || ds.dismissed) { box.hidden = true; return; }
  const bad = (ds.lines || []).filter((s) => s.indexOf('✅') !== 0);
  const el = $('#ds-text');
  if (el) {
    el.textContent = '：' + (bad[0] || ds.worst_label || '数据没到位')
      + (bad.length > 1 ? `（另有 ${bad.length - 1} 项）` : '');
  }
  box.hidden = false;
}

// 「收起来」：只关**当前这一条**（后端记指纹）—— 再出新的会重新弹出来
$('#btn-ds-dismiss') && $('#btn-ds-dismiss').addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  try {
    const r = await api('/api/data-state/dismiss', { method: 'POST' });
    toast(r.message || '收起来了');
    const box = $('#data-broken');
    if (box) box.hidden = true;
  } catch (e) {
    toast('没收起来：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
});

/* **启动自检**（用户 2026-09-19 的启动流程：先健康，再启动）。
   ⚠ 只有 `blocking`（硬门槛：升级断在半路 / 库结构 / 注册表）才挂这条红横幅 ——
     警告和待办**一律不出现**：没配邮箱、没登录都是常态，把它们一起挂上来，
     门店两天就对这个横幅免疫了，真出事时反而不看。
   ⚠ 判据同样**全在后端**（`overview.boot`）—— 前端只显示。 */
function renderBoot(boot) {
  const box = $('#boot-broken');
  if (!box) return;
  const bad = (boot && boot.blocking) || [];
  if (!bad.length) { box.hidden = true; return; }
  const el = $('#bs-text');
  if (el) {
    el.textContent = '：' + (bad[0].why || '程序自己坏了')
      + (bad.length > 1 ? `（另有 ${bad.length - 1} 项）` : '');
  }
  box.hidden = false;
}

async function loadOverview() {
  try {
    state.overview = await api('/api/overview');
  } catch (e) {
    toast('读取总览失败：' + e.message, 'bad');
    return;
  }
  const o = state.overview;

  // ⚠ 先看"上次升级有没有断在半路" —— 它跟当前在哪一页无关，所以放在总览里，
  //   30 秒那趟轮询也会刷新它（`loadConfig` 只管设置页那几块）。
  renderUpdateBroken(o.update_pending);
  renderDataState(o.data_state);
  renderBoot(o.boot);

  const v = o.config.values || {};
  const prof = o.profile || {};
  // 左下角那行 = **哪家店 · 华为编码 · （有标识才写）· 谁登的**
  //
  // ⚠ 用户 2026-09-19 两条：
  //   ① 「没有标识的门店就不显示标识-了」—— 合作店本来就没有串号标识，
  //      挂个破折号纯是噪音，**没有就不写这一段**（不是写个 `—`）。
  //   ② 「显示门店的同时也显示账号人员姓名吧」—— 最后那个是
  //      **登录这台机器的人**（云商账号的姓名），不是店员。
  //      后端从 `.secrets/erp-store.env` 的 `ERP_WHO` 读出来放进 `profile.who`。
  // ⚠ 平台岗那家是**虚拟门店** —— 它不绑任何一家店，没有华为编码，
  //   写「华为 会话默认」会让人以为没配好（用户 2026-09-19 定的虚拟门店）。
  // ⚠ 店名**优先取画像里的**（`profile.erp_name`），不是配置里那个 ——
  //   平台岗是**虚拟门店**：老配置里 `erp_store_name` 是空的（只写了 `platform: true`），
  //   画像会把它填成「平台岗」。只看配置的话左下角会显示「（未配置门店）」，
  //   看着像没配好（实测踩到）。
  const bits = [prof.erp_name || v.erp_store_name || '（未配置门店）'];
  if (!prof.platform) bits.push('华为 ' + (v.store_code || '会话默认'));
  if ((v.marker || '').trim()) bits.push('标识 ' + String(v.marker).trim());
  if ((prof.who || '').trim()) bits.push(String(prof.who).trim());
  $('#store-line').textContent = bits.join(' · ');

  const s = o.session || {};
  const sp = $('#pill-session');
  if (!s.exists) { sp.className = 'pill bad'; sp.textContent = '会话未导入'; }
  else { sp.className = 'pill warn'; sp.textContent = '会话已导入'; }

  applyProfile(o.role);
  renderUpdate(o.update || {});
  checkPreviewBanner();          // 预览模式那条横幅（没开就藏着）
  const bl = $('#build-line');
  if (bl && o.build) bl.textContent = `v${o.version || ''} · ${o.build}`;

  // ⭐ 左下角「定时」那个小标 = **内置定时器的下一次**（用户 2026-09-21：
  //   「左下角未设定时改成真实时间吧」）。
  //   ⚠ 原来读的是 `schedule.installed`（**Windows 计划任务**）—— 那个兜底
  //     2026-09-20 整个撤掉了，于是它永远显示「未设定时」，而实际上定时器里
  //     排着一整张表。⇒ 现在读 `overview.timer.next_run`（后端算好的）。
  //   ⚠ 显示的是**到点真会跑的那几步**（后端 `next_daily`）而不是"下一次任意步骤" ——
  //     自动更新每小时都跑，拿它当小标的话永远显示"一小时内"，门店看不出东西。
  //   ⚠ 后端那边**不按 `default` 筛**（2026-09-21 用户：「我把数据交换改到 21:00，
  //     这个位置不加上啊」）—— 只要那一刻真会跑（比如上报数据调到 21:00），
  //     它就该出现在这里。
  const scp = $('#pill-schedule');
  const t = o.timer || {};
  const daily = t.next_daily || {};
  const nxt = t.next_run || {};
  if (daily.at_text) {
    scp.className = 'pill ok';
    scp.textContent = `下次 ${daily.at_text}`;
    scp.title = `每天那趟：${daily.at} ${daily.label || ''}`
      + (nxt.at_text ? `\n下一次任意步骤：${nxt.at_text} ${nxt.label || ''}` : '');
  } else if (nxt.at_text) {
    scp.className = 'pill ok';
    scp.textContent = `下次 ${nxt.at_text}`;
    scp.title = `下一次自动跑：${nxt.at} ${nxt.label || ''}`.trim();
  } else {
    scp.className = 'pill warn';
    scp.textContent = '未设定时';
    scp.title = '定时器里没有任何一步设了唤醒时刻';
  }

  // ⚠ 这一页原来是「双平台数据对比」（云商串号标识判据）—— 那个判据**已被证伪**，
  //   2026-09-17 用户选的是「形态留着、内容换成报量查询的历史记录」。
  renderPoolsHistory();
  renderSession();
  // ⚠ 这里曾写 `renderSchedule(sch)` 而 `sch` 从未定义 —— loadOverview 每次收尾
  //   抛 ReferenceError（控制台实测）。schedule 在 overview 里，跟
  //   `loadSchedulerBits` 同一份（`o.schedule || {}`）。
  renderSchedule(o.schedule || {});
}

async function renderPoolsHistory() {
  // 历史在 out/pools-<年>.json（`src/pools_history.py` 落盘），单独取一次。
  let data = { days: [], years: [] };
  try {
    data = await api('/api/pools/history');
  } catch (e) {
    // 读不到就当没有 —— 历史看不了不该把整个控制台弄挂
  }
  const list = data.days || [];
  const latest = list[0];
  // ⚠ 空状态别再叫人去点「运行」页那个按钮了 —— 2026-09-21 那张卡删了，
  //   照着做会扑空。现在能做的只有两件：等每天 21:00 那趟，或点本页「刷新」
  //   （`/api/refresh` 会真跑 `dump,erp-dump,pools` 这几步）。
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
  const rows = list.map((r) => [
    r.date, hot(r.AD), hot(r.BC), r.AC, r.BD,
    { html: `<button class="btn small" data-day="${esc(r.date)}">查看</button>` },
  ]);
  $('#report-list').innerHTML = table(
    ['日期', 'AD 玲珑报了云商没报', 'BC 云商报了玲珑没报', 'AC 都卖了', 'BD 都没卖', ''],
    rows, ['mono', 'num', 'num', 'num', 'num', '']);
  $$('#report-list [data-day]').forEach((b) =>
    b.addEventListener('click', () => openPoolsDay(b.dataset.day)));
}

async function openPoolsDay(day) {
  const d = await api('/api/pools/history?date=' + encodeURIComponent(day));
  $('#report-detail-title').textContent = `${day} · 报量查询`;
  let html = '';
  for (const [k, label] of [['AD', 'AD · 玲珑报了、云商没报（云商该出库没出）'],
                            ['BC', 'BC · 云商报了、玲珑没报（门店该报量没报）']]) {
    const rows = d[k] || [];
    html += `<h3 style="margin:14px 0 6px">${label} —— ${rows.length} 台</h3>`;
    if (!rows.length) { html += '<div class="empty">没有</div>'; continue; }
    html += table(
      ['串号', '机型', '门店', '单号', '时间', '金额'],
      rows.map((r) => [
        r.sn,
        r['玲珑机型'] || r['云商机型'] || '',
        r['玲珑门店'] || r['云商门店'] || r['玲珑仓'] || r['云商仓'] || '',
        r['云商单号'] || r['玲珑单号'] || '',
        String(r['云商时间'] || r['玲珑时间'] || '').slice(0, 16),
        r['云商金额'] ?? r['玲珑金额'] ?? '',
      ]), ['mono', '', '', 'mono', 'mono', 'num']);
  }
  $('#sheet-body').innerHTML = html;
  $('#report-detail-card').hidden = false;
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
// 报量查询（双平台数据对比）：两边都要抓
$('#btn-refresh-reports').addEventListener('click', (e) =>
  refreshWithFetch('pools', e.currentTarget, () => loadOverview()));

/* ───────────────────────────── 运行 ───────────────────────────── */

// ⚠ 2026-09-21（用户：「**右下角的跑一次可以去掉了**」）——
//   这一块原来有「跑一次」那张卡的一整套接线，**卡删了，接线也一起删干净**：
//     `startRun`（POST `/api/run`）· `pollRun`（盯到跑完）·
//     `EXIT_LABELS`（把退出码翻成人话，只有它在用）· `setRunButtons`（禁用按钮）。
//   留着就是一段**永远走不到的死代码**，而且里面每一句"取 `btn-stop` /
//   `run-status` 这两个元素"的写法都会让 `TestFrontendWiring::
//   test_every_referenced_id_exists_in_html` 变红 —— 那条测试是**按正则扫源码**的，
//   查的是"app.js 有没有引用这个 id"，**判空（`if (st)`）也照样红，
//   连写在注释里都算**（本次就踩了一次）。
//   ⚠ 手动跑整趟改走命令行：`python -m src.cli daily`（后端 `POST /api/run` 还在、
//     各页「刷新」也照走 `runner`，只是界面上不再有"跑一趟"这个按钮）。
//   ⚠ 下面 `appendLog` / `watchJob` **留着** —— 各页「刷新」（先抓一次新数据）
//     的进度就贴在「运行日志」那个抽屉里（用户 2026-09-18：「运行日志要一直在、且好找」）。

function appendLog(job) {
  state.since = job.line_count;
  const el = $('#run-log');
  if (!job.lines || !job.lines.length) return;
  if (el.textContent === '（还没有运行记录）') el.textContent = '';
  el.textContent += job.lines.join('\n') + '\n';
  el.scrollTop = el.scrollHeight;
}

/** ⭐ 「刷新」= **先抓一次新数据、再重读这一页**（用户 2026-09-20：
 *  「周度重点的刷新按钮，还有 pos 合规和报量查询的刷新按钮**需要单独调用一次抓取新数据**」）。
 *
 *  ⚠ 抓数是**一两分钟**的活：所以起的是后台任务（跟手动「跑一次」同一条路），
 *    页面上给"在抓"的反馈 + 把这个抽屉打开让人看得到进度，
 *    跑完（不管成没成）都重读这一页 —— 失败时读到的还是旧数据，界面会说清。
 *  ⚠ 已经在跑的时候后端回 409，这里把它当"正常情况"说人话（不弹红 toast 吓人）。
 */
async function refreshWithFetch(page, btn, reload) {
  const oldText = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '抓新数据中…'; }
  try {
    const r = await api('/api/refresh', { method: 'POST', body: { page: page } });
    toast(r.message || '正在抓新数据…');
    setRunDrawer(true);                    // 日志就在这个抽屉里，打开它
    await watchJob((r.job || {}).id || '');
  } catch (e) {
    // ⚠ 409 = "已经有一趟在跑" —— 那不是错误，是"等它跑完"
    toast(e.message, e.message.indexOf('已经在跑') >= 0 ? '' : 'bad');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = oldText; }
    await reload();
  }
}

/** 盯着一个后台任务直到结束（把日志贴进抽屉）。返回退出码，出错给 -1。 */
async function watchJob(jobId) {
  if (!jobId) return -1;
  state.jobId = jobId;
  state.since = 0;
  const log = $('#run-log');
  if (log) log.textContent = '';
  for (;;) {
    let job;
    try { job = await api(`/api/run?id=${jobId}&since=${state.since}`); }
    catch (e) { return -1; }
    appendLog(job);
    if (!job.running) {
      const n = job.exit_code;
      const fine = (n === 0 || n === 3);
      toast(fine ? '抓新数据跑完了' : `抓新数据没跑成（退出码 ${n}）—— 上面有日志`,
            fine ? 'ok' : 'bad');
      state.jobId = null;          // 盯完了 ⇒ 底下那个 30 秒轮询可以照常刷徽章了
      return n;
    }
    await new Promise((r) => setTimeout(r, 900));
  }
}

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

/* 生活馆：手输门店编码（用户 2026-09-27：「加一个手动输入门店编码吧，
 * 现在匹配不起来拉不到会话」）。
 * ⚠ 只在生活馆显示 —— full 版的店码由云商登录匹配出来，手输会盖掉那个判据
 *   （README 坑：一个账号看多个店时，店码填错不报错、只会静默算错）。
 * ⚠ 校验和写入只有后端一份（store_identity.set_store_code），前端只管展示。 */
function storeCodeBox() {
  if (!(setupState && setupState.lifehall)) return '';
  const cur = ((setupState && setupState.profile) || {}).huawei_code || '';
  return `<div class="store-code-box">
    <b>门店编码</b>
    <input class="mono" data-store-code-input spellcheck="false"
           placeholder="如 SCN328987" value="${esc(cur)}">
    <button type="button" class="btn small" data-save-store-code>保存</button>
    <span class="hint">认不出店就手填 —— 保存后抓会话会<b>跳过认店</b>，
      直接按这家店自检；名单里有会自动带出店名。</span>
  </div>`;
}

function bindStoreCodeBtn() {
  document.querySelectorAll('[data-save-store-code]').forEach((btn) => {
    btn.onclick = async () => {
      const box = btn.closest('.store-code-box');
      const input = box && box.querySelector('[data-store-code-input]');
      const code = ((input && input.value) || '').trim();
      if (!code) return toast('先填门店编码', 'bad');
      btn.disabled = true;
      try {
        const r = await api('/api/session/store-code',
                            { method: 'PUT', body: { store_code: code } });
        toast(r.found
            ? `已保存：${r.store_code}（${r.store_name}）`
            : `已保存店码 ${r.store_code}（名单里没这家店，店名留空）`, 'ok');
        if (r.moved) toast('会话文件已按新店码改名', 'ok');
        try { setupState = (await api('/api/setup')) || setupState; } catch (e) { /* 照旧显示 */ }
        renderStoreCodeForms();
        if (setupState && setupState.ready) loadOverview();
      } catch (e) {
        toast('保存失败：' + e.message, 'bad');
      } finally { btn.disabled = false; }
    };
  });
}

function renderStoreCodeForms() {
  const lifehall = !!(setupState && setupState.lifehall);
  const markup = lifehall ? storeCodeBox() : '';
  const loginHost = $('#setup-store-code-host');
  const settingsHost = $('#store-code-settings');
  const settingsCard = $('#store-code-settings-card');
  if (loginHost) {
    loginHost.innerHTML = markup;
    loginHost.hidden = !lifehall;
  }
  if (settingsHost) settingsHost.innerHTML = markup;
  if (settingsCard) settingsCard.hidden = !lifehall;
  bindStoreCodeBtn();
}

function syncLifehallSettings() {
  const lifehall = !!(setupState && setupState.lifehall);
  const label = document.querySelector('[data-foot="general"] .foot-label');
  const service = $('#service-settings-card');
  const fullNote = $('#general-settings-full-note');
  if (label) label.textContent = lifehall ? '设置' : '推送设置';
  if (service) service.hidden = lifehall;
  if (fullNote) fullNote.hidden = lifehall;
}

function renderSession() {
  const box = $('#session-box');
  if (!box) return;                 // 模板里没有这块 —— 别让它炸在启动路径上
  const s = (state.overview && state.overview.session) || {};
  if (!s.exists) {
    box.innerHTML = '<div class="banner bad">还没有会话。照下面的步骤抓一份 curl 导入。</div>';
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

  box.innerHTML = banner + `
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

/* ─────────────────── 人员设置（本店有谁，谁还在职）───────────────────

   用户 2026-09-18：「通用设置里面加一个人员设置，登录到门店后自动加载挂在门店下
   所有在职员工，当然也加上在职的复选框，门店可以选择剔除掉**已离职但是状态没更新**
   的员工」。这批人就是以后**「默认表」自动填人名**的来源。

   ⚠ 复选框是**唯一**的手段：云商 `UserList` 的 `Status` 实测 239 个账号**全是 3**，
     区分不出离职。
   ⚠ 提交的是**被剔除的那一批**（不在职），不是"在职"那一批 ——
     存"在职"的话，以后新入职的人会默认变成"不在职"。
   ⚠ 登录名大小写不统一（后端已统一按大写比对），前端**原样显示**别自己改。 */

let staffRows = [];

function renderStaff(d) {
  // ⭐ 2026-09-21：**区长/平台看到的是"门店发来的状态表"**（只读）。
  //   用户：「区长/平台**不能改**别家店的这份名单，**读取门店发送的状态表**吧」
  //   ⇒ 后端给的是 `stores: [{店名, 日期, 人…}]`，一个复选框都不给。
  if (d.from_mail) {
    renderStaffFromMail(d);
    return;
  }
  staffRows = d.people || [];
  $('#staff-branch').textContent = d.branch_id == null ? '—' : String(d.branch_id);
  const n = staffRows.filter((p) => p.active).length;
  $('#staff-meta').textContent = staffRows.length
    ? `共 ${staffRows.length} 人 · 在职 ${n} 人` : '';
  const box = $('#staff-list');
  if (!staffRows.length) {
    box.innerHTML = '<div class="empty">这家店的机构下没有账号 —— '
      + '要么云商里还没建，要么门店还没登录过</div>';
    return;
  }
  box.innerHTML = staffRows.map((p, i) => `
    <label class="staff-row">
      <input type="checkbox" data-staff="${i}"${p.active ? ' checked' : ''}>
      <span class="staff-name">${esc(p.real || '（没写姓名）')}</span>
      <span class="hint mono">${esc(p.account || '')}</span>
      <span class="hint">${esc(p.phone || '')}</span>
    </label>`).join('');
  $$('#staff-list [data-staff]').forEach((b) => b.addEventListener('change', () => {
    staffRows[Number(b.dataset.staff)].active = b.checked;
    const k = staffRows.filter((x) => x.active).length;
    $('#staff-meta').textContent = `共 ${staffRows.length} 人 · 在职 ${k} 人`;
  }));
}

/** 区长 / 平台：**每店一张人员状态表**（只读）—— 数据是那家店发过来的。 */
function renderStaffFromMail(d) {
  const box = $('#staff-list');
  const stores = d.stores || [];
  // 只读 ⇒ 那三个"改"的控件全收起来（别的店的名单不归你看，更不归你改）
  ['#btn-staff-all', '#btn-staff-none', '#btn-staff-save'].forEach((sel) => {
    const el = $(sel);
    if (el) el.hidden = true;
  });
  // 标题也换 —— 区长/平台这一页**不是"本店人员"**（那是别家店发来的）
  const h2 = $('#staff-list')?.closest('.card')?.querySelector('h2');
  if (h2 && h2.firstChild) h2.firstChild.textContent = '各店人员状态（门店发来的） ';
  $('#staff-branch').textContent = '—';
  $('#staff-meta').textContent = stores.length
    ? `${stores.length} 家店 · 共 ${d.count || 0} 人 · 在职 ${d.active_count || 0} 人`
    : '';
  if (!stores.length) {
    box.innerHTML = `<div class="empty">${esc(d.hint || '还没收到过门店的人员状态表')}</div>`;
    return;
  }
  box.innerHTML = stores.map((s) => `
    <div class="staff-store">
      <div class="staff-store-head">
        <b>${esc(s.store_name || '（名单里没这个名字）')}</b>
        <span class="hint mono">${esc(s.store_code || '')}</span>
        <span class="hint">· 共 ${s.count || 0} 人 · 在职 ${s.active_count || 0} 人</span>
      </div>
      <div class="hint">📧 这份是 <b>${esc(s.report_date || '')}</b> 的邮件里的${
        s.imported_at ? `（${esc(s.imported_at)} 收到）` : ''} —— 只有门店能改，这边只读</div>
      <div class="staff-list-inner">${(s.people || []).map((p) => `
        <div class="staff-row">
          <span class="staff-name">${p.active ? '' : '（已离职）'}${
            esc(p.real || '（没写姓名）')}</span>
          <span class="hint mono">${esc(p.account || '')}</span>
        </div>`).join('')}</div>
    </div>`).join('');
}

async function loadStaff() {
  const box = $('#staff-list');
  box.innerHTML = '<div class="hint">正在读云商的用户名单…</div>';
  let d;
  try { d = await api('/api/staff'); } catch (e) {
    box.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
    return;
  }
  if (!d.ok) {
    box.innerHTML = `<div class="banner warn">${esc(d.error || '读不到')}</div>`;
    return;
  }
  renderStaff(d);
}

function setAllStaff(on) {
  $$('#staff-list [data-staff]').forEach((b) => { b.checked = on; });
  staffRows.forEach((p) => { p.active = on; });
  $('#staff-meta').textContent = `共 ${staffRows.length} 人 · 在职 ${on ? staffRows.length : 0} 人`;
}

/** 还要等多久 → 给人看的话。⚠ 300 秒要写成「5 分钟」——
 *  不然提示里写「300 秒后自动上报」，门店得自己拿计算器（实测第一版就是直接印秒数）。 */
function waitText(secs) {
  const n = Math.max(0, Math.round(Number(secs) || 0));
  if (n < 60) return `${n} 秒`;
  const m = n / 60;
  return `${Number.isInteger(m) ? m : m.toFixed(1)} 分钟`;
}

$('#btn-staff-all')?.addEventListener('click', () => setAllStaff(true));
$('#btn-staff-none')?.addEventListener('click', () => setAllStaff(false));
$('#btn-staff-save')?.addEventListener('click', async () => {
  const btn = $('#btn-staff-save');
  const msg = $('#staff-msg');
  // ⚠ 存的是**被剔除的**那一批
  const excluded = staffRows.filter((p) => !p.active).map((p) => p.account);
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const d = await api('/api/staff', { method: 'PUT', body: { excluded } });
    renderStaff(d);
    // ⭐ 保存**顺带**把上报发出去（用户 2026-09-21：「保存即发…连点几次保存
    //   合并成一封。**那还是 5 分钟吧**」）—— 后端过一个窗口（默认 5 分钟）发
    //   **完整上报包**（人员表照旧在里面），窗口内再点保存只**重新计时**
    //   ⇒ 一串点击只发一封。
    //   ⚠ 所以这里要**说清"还在等"**：不写的话，门店点完看不到东西，
    //     会以为没保存成功（而这个项目最怕的就是"看着没反应"）。
    const secs = Number(d.report_in || 0);
    msg.innerHTML = '已保存 ' + new Date().toLocaleTimeString()
      + `（剔除 ${excluded.length} 人）`
      + (secs ? `　·　<b>${waitText(secs)}后自动上报</b>给区长和中台`
                + '<span class="hint">（这期间再点保存也只会发一封）</span>' : '');
    toast(secs ? `已保存，${secs} 秒后自动上报` : '人员设置已保存', 'ok');
  } catch (e) {
    msg.textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false; btn.textContent = '保存';
  }
});

/* ─────────────── 门店登录（认「这台机器是哪家店」，再匹配配置）───────────────

   用户 2026-09-18：「加个门店的登录设置吧，主要是读取这个账号的门店信息。
   来匹配不同门店的设置，当然公司云商账号也是加密保留的，门店云商账号可以不加密」。

   ⚠ 跟公司账号**完全分开**：公司账号是内置混淆的、界面上不显示、取数用它；
     门店账号明文存、界面上可填、**只**用来读本店档案。
   ⚠ 一天之内这个功能被删过又请回来 —— 上午删是因为"没有用途"，
     现在有了（匹配门店配置）。那段"别再把它加回来"的注释已经删掉，
     免得下一个人照着它把功能又拆了。 */

function saForm() {
  // ⚠ 「公司代码」那一格 2026-09-19 去掉了（用户：「公司编码这个东西
  //   登录页面也不需要有」）—— 后端有默认值（`DEFAULT_COMPANY`），
  //   登录时用不用得上由它自己填。
  const body = { username: $('#sa-username').value.trim() };
  const pw = $('#sa-password').value;
  if (pw) body.password = pw;          // 空 = 不改
  return body;
}

async function loadStoreAccount() {
  let d;
  try { d = await api('/api/store-account'); } catch (e) { return; }
  $('#sa-username').value = d.username || '';
  $('#sa-password').value = '';                        // 密码绝不回显
  $('#sa-password').placeholder = d.has_password
    ? '已设置（留空＝不修改）' : '门店自己的云商登录密码';
  $('#sa-status').innerHTML = !d.exists || !d.has_password
    ? '<span class="hint">还没配</span>'
    : (d.has_token
        ? '<span style="color:var(--ok)">已配置</span> · token 已缓存'
        : '<span style="color:var(--warn)">已配置</span> · 还没换过 token');
  // ⚠ 按钮 2026-09-19 改名叫「确认登录」了 —— 文案要跟着改，
  //   不然会指着一个不存在的按钮（这个项目为"改了一处漏了另一处"栽过好几次）。
  $('#sa-warn').innerHTML = d.has_password ? ''
    : '<span class="hint">把门店自己的云商账号密码填上，点「确认登录」。</span>';
}

/* 「确认登录」—— 用户 2026-09-19：「保存账号那个按钮内容改成**确认登录**，
 * 实际操作是**获取下登录信息看看账密是不是对**，如果对了就保存下来并且获取门店信息
 * 判断是否需要玲珑登录进入下一步」。
 *
 * ⚠ 所以顺序是 **先验证、通过了才落盘**（后端 `store_lookup` 支持临时账密）——
 *   先存再验的话，密码打错一个字母会把好密码覆盖掉，
 *   而失败提示只说"登录失败"，人根本不知道是保存造成的。 */
$('#btn-sa-save')?.addEventListener('click', async () => {
  const f = saForm();
  if (!f.username || !$('#sa-password').value) {
    $('#sa-msg').innerHTML = '<span style="color:var(--bad)">账号和密码都要填</span>';
    return;
  }
  await storeLookup('', f);
});

/* 「退出」—— 关掉这个浏览器页面，**后台服务照常跑**。
 * ⚠ `window.close()` 只能关掉"脚本打开的"窗口，直接开的标签页关不掉 ——
 *   关不掉就退而求其次提示一句，别让人对着一个没反应的按钮发呆。 */
$('#btn-sa-quit')?.addEventListener('click', () => {
  window.close();
  setTimeout(() => {
    const msg = $('#setup-msg');
    if (msg) {
      msg.innerHTML = '关不掉就手动关这个标签页吧 —— '
        + '<b>后台服务照常在跑</b>，对账不受影响。';
    }
  }, 250);
});

/* ⚠ `applyMatchedStore()` 2026-09-18 删掉了 —— 那三行输入框没了。
   用户：「门店登录完，是不是上面那个门店信息设置就不用了」+「直接写」：
   匹配成功由**后端直接写进配置**，前端不再回填表单。 */

/* ⚠ 2026-09-21（用户：「通用设置里第一块门店去掉」）——
   原来这儿有 `renderStoreSummary()`（往 `#store-summary` 里写"这家店是谁"），
   跟着那张卡一起删了：左下角那行（`#store-line`）本来就写着店名+编码+标识，
   左边「账号设置」页也看得到 —— 三处说同一件事。 */


function renderStoreLookup(r) {
  const box = $('#sa-msg');
  if (!r.ok) {
    box.innerHTML = `<span style="color:var(--bad)">${esc(r.error || '读不到')}</span>`;
    return;
  }
  const m = r.matched || {};
  const bits = [`本店是<b>「${esc(m.erp_name || '')}」</b>`];
  bits.push(m.huawei_code ? `编码 <b>${esc(m.huawei_code)}</b>`
                          : '<span style="color:var(--warn)">没有华为编码</span>');
  if (m.marker) bits.push(`串号标识 <b>${esc(m.marker)}</b>`);
  box.innerHTML = bits.join(' · ')
    + `<br><span class="hint">${m.kind ? esc(m.kind) + ' · ' : ''}${esc(m.huawei_name || '')}</span>`
    + '<br><span class="hint">已写进门店配置。</span>';

  if (!m.found) {
    // ⚠ 名单是随程序走的 —— 补一家店，14 台机器下次升级都会更新到。
    //   让人在这台机器上手填，等于 14 台各填一遍、还会填得不一样。
    box.innerHTML += '<br><span style="color:var(--warn)">⚠ 这家店<b>不在门店名单里</b>'
      + '（<code>config/stores.yaml</code>）—— 只写进了云商门店名，'
      + '<b>华为编码是空的</b>。请把这家店补进名单（名单随程序更新，'
      + '补一次所有机器都会拿到），否则查华为订单会退化成"按会话自身门店查"。</span>';
  }
  if (r.rehint) {
    // ⚠ 华为会话是按**门店编码**存的文件名 —— 编码一变，等于换了一份会话
    box.innerHTML += '<br><span style="color:var(--warn)">⚠ 华为门店编码变了（'
      + `${esc(r.store_code_from || '空')} → ${esc(r.store_code_to || '空')}）：`
      + '会话是按编码存的文件（<code>.secrets/cbg-&lt;编码&gt;.json</code>），'
      + '换编码等于换一份会话 —— 到「玲珑授权」页用这个店的账号<b>重新抓一次</b>。</span>';
  }
  $('#sa-password').value = '';          // 存下去了就别在框里留着
  toast('已登录：' + (m.erp_name || ''), 'ok');
  loadStoreAccount();
  loadOverview();          // 卡片头的摘要 / 状态抽屉里的门店那几行要跟着刷新
  // ⚠ 登录成功后**自动往下走**（用户：「如果对了就保存下来并且获取门店信息
  //   判断是否需要玲珑登录进入下一步」）—— 不然人还得自己点一下才知道好了没。
  setTimeout(async () => { await checkSetup(); }, 600);
}

async function storeLookup(code, creds) {
  const msg = $('#sa-msg');
  const btn = $('#btn-sa-save');
  btn.disabled = true; btn.textContent = '登录中…';
  msg.textContent = '';
  try {
    // ⚠ 账密**跟着这次请求走**（临时）—— 后端验证通过了才落盘
    const body = { ...(creds || {}) };
    if (code) body.code = code;
    const r = await api('/api/store-account/lookup', { method: 'POST', body });
    if (r.need_captcha) {
      // ⚠ 验证码跟**那一次会话**绑定 —— 后端把 client 留着了，
      //   这里只管把图显示出来、把用户填的字原样送回去。
      $('#sa-captcha').hidden = false;
      $('#sa-captcha-img').src = r.image || '';
      $('#sa-captcha-msg').textContent = '';
      msg.textContent = '要图形验证码 —— 填下面那张图';
      $('#sa-captcha-code').focus();
      return;
    }
    $('#sa-captcha').hidden = true;
    renderStoreLookup(r);
  } catch (e) {
    msg.innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  } finally {
    btn.disabled = false; btn.textContent = '确认登录';
  }
}


$('#btn-sa-captcha')?.addEventListener('click', () => {
  const code = ($('#sa-captcha-code').value || '').trim();
  if (!code) { $('#sa-captcha-msg').textContent = '先把验证码填上'; return; }
  storeLookup(code);
});

/* ───────────────────────── 云商账号的两条线 ─────────────────────────

   2026-09-18 一天之内来回走了两趟，**最终形态**是：

   | | 公司账号 | 门店账号 |
   |---|---|---|
   | 用途 | **取数**（销售明细 / 在库 / 四池） | **只**用来认"这台机器是哪家店" |
   | 存放 | `.secrets/erp.env` + **内置兜底**（混淆） | `.secrets/erp-store.env`（明文） |
   | 界面 | ❌ 不显示、不用配 | ✅ 只读 —— 在**登录页**里填（左下角「通用设置 › 通用」有入口） |

   ⚠ 中间删过一轮门店账号，理由是"没必要" —— 那时确实**没有用途**（实测也证明
     取数上它没有任何优势：销售明细两边一模一样）。后来用户给了它一个明确用途
     （读本店档案 → 匹配门店配置），于是又请回来了。
   ⚠ **别再因为它中途被删过就把上面那张卡也拆掉** —— 先看用户当时说没说要它。 */

/* ───────────────────────── 邮件推送（路径列表 · 2026-09-22） ─────────────────────────
   一张可增删的路径列表；没有 when / 启用勾选。有几条路径就发几条。 */

let mailPaths = [];      // 当前编辑中的数组（未保存也在内存里）
let mailPresets = [];

function mailRowHtml(p, i) {
  const pwPh = p.has_password ? '已设置（留空＝不修改）' : '多数邮箱要填「授权码」';
  const bad = (p.problems && p.problems.length)
    ? `<div class="hint" style="color:var(--warn)">缺 ${esc(p.problems.join('、'))}</div>` : '';
  return `
  <div class="path-item" data-i="${i}" style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px">
    <div class="form-row" style="align-items:center">
      <b>路径 ${i + 1}</b>
      ${p.ready ? '<span class="hint" style="color:var(--ok)">就绪</span>' : '<span class="hint" style="color:var(--warn)">未就绪</span>'}
      <span style="flex:1"></span>
      <button class="btn" data-act="test" type="button">发送测试</button>
      <button class="btn" data-act="del" type="button">删除</button>
    </div>
    <div class="form-row"><label>SMTP 服务器</label>
      <input data-f="host" value="${esc(p.host || '')}" placeholder="smtp.qq.com" autocomplete="off">
      <label style="min-width:auto">端口</label>
      <input type="number" data-f="port" style="min-width:90px" value="${esc(p.port || 465)}" placeholder="465">
      <select data-f="security" style="min-width:130px">
        <option value="ssl"${p.security === 'ssl' ? ' selected' : ''}>SSL</option>
        <option value="starttls"${p.security === 'starttls' ? ' selected' : ''}>STARTTLS</option>
        <option value="none"${p.security === 'none' ? ' selected' : ''}>不加密</option>
      </select>
      <label style="min-width:auto">快速填充</label>
      <select data-act="preset" style="min-width:160px">
        <option value="">—— 常见邮箱 ——</option>
        ${mailPresets.map((q, j) => `<option value="${j}">${esc(q.name)}</option>`).join('')}
      </select>
    </div>
    <div class="form-row"><label>账号</label>
      <input data-f="username" value="${esc(p.username || '')}" placeholder="发件邮箱地址" autocomplete="off">
      <label style="min-width:auto">密码 / 授权码</label>
      <input type="password" data-f="password" value="" placeholder="${esc(pwPh)}" autocomplete="new-password">
    </div>
    <div class="form-row"><label>收件人</label>
      <input data-f="recipients" value="${esc(p.recipients || '')}" placeholder="多个用逗号分隔，例如 a@x.com, b@y.com"></div>
    <div class="form-row"><label>发件人</label>
      <input data-f="sender" value="${esc(p.sender || '')}" placeholder="留空 = 用账号" autocomplete="off">
      <label style="min-width:auto">主题前缀</label>
      <input data-f="subject_prefix" style="min-width:130px" value="${esc(p.subject_prefix || '[报量对账]')}"></div>
    ${bad}
  </div>`;
}

function collectMailPaths() {
  const out = [];
  document.querySelectorAll('#mail-paths .path-item').forEach((el) => {
    const i = Number(el.dataset.i);
    const prev = mailPaths[i] || {};
    const g = (f) => {
      const n = el.querySelector(`[data-f="${f}"]`);
      return n ? String(n.value || '').trim() : '';
    };
    const row = {
      id: prev.id || '',
      host: g('host'),
      port: g('port') || 465,
      security: g('security') || 'ssl',
      username: g('username'),
      password: g('password'),   // 空 = 保留原密码（后端处理）
      sender: g('sender'),
      recipients: g('recipients'),
      subject_prefix: g('subject_prefix') || '[报量对账]',
      cc: prev.cc || '',
      env_file: prev.env_file || '',
    };
    out.push(row);
  });
  return out;
}

function renderMailPaths() {
  const box = $('#mail-paths');
  if (!mailPaths.length) {
    box.innerHTML = '<div class="hint" style="padding:8px 0">还没有邮件路径 —— 点「新增」加一条；列表空 = 这台机器不发邮件。</div>';
  } else {
    box.innerHTML = mailPaths.map(mailRowHtml).join('');
  }
  const n = mailPaths.length;
  $('#mail-status').innerHTML = n
    ? `<span style="color:var(--ok)">${n} 条路径</span>`
    : '<span class="hint">未配置</span>';
  const bad = mailPaths.filter((p) => !p.ready).length;
  $('#mail-warn').innerHTML = bad
    ? `<span style="color:var(--warn)">⚠️ ${bad} 条路径配置不全，跑完那几条不会发</span>`
    : '';
}

$('#mail-paths').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const item = btn.closest('.path-item');
  if (!item) return;
  const i = Number(item.dataset.i);
  const act = btn.dataset.act;
  if (act === 'del') {
    mailPaths = collectMailPaths();
    mailPaths.splice(i, 1);
    renderMailPaths();
    return;
  }
  if (act === 'test') {
    const rows = collectMailPaths();
    const row = rows[i];
    if (!row) return;
    btn.disabled = true; btn.textContent = '发送中…';
    try {
      const r = await api('/api/mail/test', { method: 'POST', body: row });
      $('#mail-msg').innerHTML = r.ok
        ? `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`
        : `<span style="color:var(--bad)">❌ ${esc(r.message || '发送失败')}</span>`;
      toast(r.ok ? '测试邮件已发出' : '测试邮件发送失败', r.ok ? 'ok' : 'bad');
    } catch (er) {
      $('#mail-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(er.message)}</span>`;
    } finally {
      btn.disabled = false; btn.textContent = '发送测试';
    }
  }
});

$('#mail-paths').addEventListener('change', (e) => {
  const sel = e.target.closest('select[data-act="preset"]');
  if (!sel || sel.value === '') return;
  const p = mailPresets[Number(sel.value)];
  const item = sel.closest('.path-item');
  if (!p || !item) return;
  item.querySelector('[data-f="host"]').value = p.host;
  item.querySelector('[data-f="port"]').value = p.port;
  item.querySelector('[data-f="security"]').value = p.security;
});

async function loadMail() {
  let d;
  try { d = await api('/api/mail'); } catch (e) { return; }
  mailPaths = Array.isArray(d.paths) ? d.paths : [];
  mailPresets = d.presets || [];
  renderMailPaths();
}

$('#btn-mail-add').addEventListener('click', () => {
  mailPaths = collectMailPaths();
  mailPaths.push({
    id: '', host: '', port: 465, security: 'ssl',
    username: '', has_password: false, password: '',
    sender: '', recipients: '', subject_prefix: '[报量对账]',
    cc: '', ready: false, problems: [],
  });
  renderMailPaths();
  const items = document.querySelectorAll('#mail-paths .path-item');
  const last = items[items.length - 1];
  if (last) last.querySelector('[data-f="host"]').focus();
});

$('#btn-mail-save').addEventListener('click', async () => {
  try {
    const paths = collectMailPaths();
    const r = await api('/api/mail', { method: 'PUT', body: { paths } });
    $('#mail-msg').textContent = '已保存 ' + new Date().toLocaleTimeString();
    toast('邮件路径已保存', 'ok');
    // 用返回值刷新（密码字段清掉）
    mailPaths = Array.isArray(r.paths) ? r.paths : paths;
    mailPresets = r.presets || mailPresets;
    renderMailPaths();
  } catch (e) {
    $('#mail-msg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
  }
});

/* ───────────────────────── 企微群推送（路径列表 · 2026-09-22） ───────────────────────── */

let wecomPaths = [];

function wecomRowHtml(p, i) {
  const hookPh = p.has_webhook
    ? `已配置（${esc(p.webhook_key || '')}）—— 要换再粘整条地址`
    : 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…';
  const bad = (p.problems && p.problems.length)
    ? `<div class="hint" style="color:var(--warn)">缺 ${esc(p.problems.join('、'))}</div>` : '';
  return `
  <div class="path-item" data-i="${i}" style="border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:10px">
    <div class="form-row" style="align-items:center">
      <b>路径 ${i + 1}</b>
      ${p.has_webhook ? `<span class="hint" style="color:var(--ok)">${esc(p.webhook_key || '已配')}</span>` : '<span class="hint" style="color:var(--warn)">未配置</span>'}
      <span style="flex:1"></span>
      <button class="btn" data-act="test" type="button">推送测试</button>
      <button class="btn" data-act="del" type="button">删除</button>
    </div>
    <div class="form-row"><label>webhook 地址</label>
      <input data-f="webhook" style="min-width:420px" autocomplete="off" value=""
             placeholder="${hookPh}"></div>
    ${bad}
  </div>`;
}

function collectWecomPaths() {
  const out = [];
  document.querySelectorAll('#wecom-paths .path-item').forEach((el) => {
    const i = Number(el.dataset.i);
    const prev = wecomPaths[i] || {};
    const n = el.querySelector('[data-f="webhook"]');
    const hook = n ? String(n.value || '').trim() : '';
    out.push({
      id: prev.id || '',
      webhook: hook,           // 空 = 保留原 webhook（后端处理）
      mention_all: false,      // 界面已撤；代码内默认
      send_file: true,
      env_file: prev.env_file || '',
      has_webhook: prev.has_webhook,
      webhook_key: prev.webhook_key,
    });
  });
  return out;
}

function renderWecomPaths() {
  const box = $('#wecom-paths');
  if (!wecomPaths.length) {
    box.innerHTML = '<div class="hint" style="padding:8px 0">还没有企微路径 —— 点「新增」加一条；列表空 = 不推企微。</div>';
  } else {
    box.innerHTML = wecomPaths.map(wecomRowHtml).join('');
  }
  const n = wecomPaths.length;
  $('#wecom-status').innerHTML = n
    ? `<span style="color:var(--ok)">${n} 条路径</span>`
    : '<span class="hint">未配置</span>';
  const bad = wecomPaths.filter((p) => !p.ready && !p.has_webhook).length;
  $('#wecom-warn').innerHTML = bad
    ? `<span style="color:var(--warn)">⚠️ ${bad} 条路径没填 webhook，跑完那几条不会推</span>`
    : '';
}

$('#wecom-paths').addEventListener('click', async (e) => {
  const btn = e.target.closest('button[data-act]');
  if (!btn) return;
  const item = btn.closest('.path-item');
  if (!item) return;
  const i = Number(item.dataset.i);
  const act = btn.dataset.act;
  if (act === 'del') {
    wecomPaths = collectWecomPaths();
    wecomPaths.splice(i, 1);
    renderWecomPaths();
    return;
  }
  if (act === 'test') {
    const rows = collectWecomPaths();
    const row = rows[i];
    if (!row) return;
    // webhook 界面不回显：没粘新的就靠 id 让后端从已存路径取
    if (!row.webhook && !(wecomPaths[i] || {}).has_webhook) {
      $('#wecom-msg').innerHTML = '<span style="color:var(--bad)">❌ 先填 webhook 再测</span>';
      return;
    }
    btn.disabled = true; btn.textContent = '推送中…';
    try {
      const r = await api('/api/wecom/test', { method: 'POST', body: row });
      $('#wecom-msg').innerHTML = r.ok
        ? `<span style="color:var(--ok)">✅ ${esc(r.message)}</span>`
        : `<span style="color:var(--bad)">❌ ${esc(r.message || '推送失败')}</span>`;
      toast(r.ok ? '测试消息已推送' : '推送失败', r.ok ? 'ok' : 'bad');
    } catch (er) {
      $('#wecom-msg').innerHTML = `<span style="color:var(--bad)">❌ ${esc(er.message)}</span>`;
    } finally {
      btn.disabled = false; btn.textContent = '推送测试';
    }
  }
});
async function loadWecom() {
  let d;
  try { d = await api('/api/wecom'); } catch (e) { return; }
  wecomPaths = Array.isArray(d.paths) ? d.paths : [];
  renderWecomPaths();
}

$('#btn-wecom-add').addEventListener('click', () => {
  wecomPaths = collectWecomPaths();
  wecomPaths.push({
    id: '', webhook: '', has_webhook: false, webhook_key: '',
    mention_all: false, send_file: true, ready: false, problems: [],
  });
  renderWecomPaths();
  const items = document.querySelectorAll('#wecom-paths .path-item');
  const last = items[items.length - 1];
  if (last) last.querySelector('[data-f="webhook"]').focus();
});

$('#btn-wecom-save').addEventListener('click', async () => {
  try {
    const paths = collectWecomPaths();
    const r = await api('/api/wecom', { method: 'PUT', body: { paths } });
    $('#wecom-msg').textContent = '已保存 ' + new Date().toLocaleTimeString();
    toast('企微路径已保存', 'ok');
    wecomPaths = Array.isArray(r.paths) ? r.paths : paths;
    renderWecomPaths();
  } catch (e) {
    $('#wecom-msg').textContent = '保存失败：' + e.message;
    toast('保存失败：' + e.message, 'bad');
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
  // ⚠ 2026-09-21（用户：「后台服务只显示**是否开机自动启动**，如果否就多显示一个
  //   **添加开机自动启动**，要 uac 的那种」）—— 从"复选框 + 保存"改成"状态 + 按钮"。
  const st = $('#svc-autostart-state');
  const addBtn = $('#btn-autostart-add');
  if (st) {
    st.innerHTML = a.installed
      ? '<span style="color:var(--ok)">✅ 已开启</span>'
      : '<span style="color:var(--warn)">未开启 —— 开机能自己跑起来才好天天不用管</span>';
  }
  if (addBtn) addBtn.hidden = !!a.installed;
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

/* 「添加开机自动启动」—— **先试普通权限，失败才弹 UAC**。
   ⚠ 顺序不能反：这个项目**默认全程普通权限**（不需要管理员），而且
     **以管理员身份跑会把「自动抓会话」弄坏**（Edge/Chrome 拒绝以管理员运行）——
     `autostart` 默认那条（注册表 Run 项）正是普通权限。
   ⚠ 用户 2026-09-21 说「要 uac 的那种」= 失败时要能弹 UAC 去装（计划任务那条路），
     不是"一上来就提权"。所以：普通 → 失败 → 提权重试一次，并把话说清。 */
$('#btn-autostart-add')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true;
  const msg = $('#svc-msg');
  if (msg) msg.textContent = '正在添加…';
  try {
    let r = await api('/api/autostart', { method: 'POST', body: { enabled: true } });
    if (!r.ok) {
      if (msg) msg.textContent = '普通权限没成 —— 正在弹 UAC（请点「是」，跑完按回车）…';
      r = await api('/api/elevate', { method: 'POST', body: { what: 'autostart' } });
    }
    if (msg) msg.textContent = (r.ok ? '✅ ' : '❌ ') + (r.message || '');
    toast(r.ok ? '已添加开机自动启动' : (r.message || '没成'), r.ok ? 'ok' : 'bad');
    loadService();
  } catch (e) {
    if (msg) msg.textContent = '❌ ' + e.message;
    toast('添加失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
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

/** 「检查更新」这一页要什么 —— 更新状态在总览里（`overview.update`），
 *  升级记录也在里面（`overview.upgrades`）；历史版本列表按需点按钮才拉。
 *
 *  ⚠ 2026-09-21（用户：「检查更新单独做一个页面」）之前它是通用页里的一张卡，
 *    靠 `loadConfig()` 顺手画 —— 现在自己一页，就得自己负责拉数据。
 */
async function loadUpdate() {
  await loadOverview();          // 里面就会 renderUpdate + renderUpgrades
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

/* 历史版本 / 按 commit 回退已移除（用户 2026-09-23：版本回退没必要）。
   升级仍走 main 最新；半截失败用 repair / restore 备份。 */

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
  const lifehall = !!(setupState && setupState.lifehall);
  syncLifehallSettings();
  renderStoreCodeForms();
  CONFIG_FIELDS.forEach(([key, id]) => {
    const el = $('#' + id);
    if (el) el.value = v[key] == null ? '' : v[key];
  });
  // ⚠ 「系统计划任务 / 自动化跑什么」两块 2026-09-20 搬去了**定时器设置页**
  //   （用户：「定时器设置单独出来一页」）—— 它们的渲染在 `loadSchedulerBits()`。
  if (state.overview) renderWhatsNew(state.overview.whatsnew);
  if (state.overview) renderLegacyPrompt(state.overview.legacy_prompt);
  if (state.overview) renderUpgrades(state.overview.upgrades);
  if (!lifehall) loadStoreAccount();
  loadMail();
  loadWecom();
  if (!lifehall) loadService();
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
    // ⚠ `config-msg` 随「对账参数」版块一起删了（2026-09-17）——
    //   提示统一走调用方传进来的 `msgEl`（门店卡片那边传的是 `#store-msg`）。
    if (msgEl) msgEl.textContent = '已保存 ' + when;
    toast('配置已保存（注释保留了）', 'ok');
    loadOverview();
    return res;
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
    if (msgEl) msgEl.textContent = '保存失败';
  }
}

/* ⚠ `btn-save-store`（「保存门店配置」）2026-09-18 **删掉了** ——
   门店那三项不再由人填，也就没有"保存"这个动作：
   「确认登录」匹配成功时后端**直接写进配置**（用户：「直接写」）。
   要确认当前值：卡片头上那行摘要，或者右下角状态抽屉里的「门店名称 / 编码 / 标识」。

   ⚠ `saveConfig()` **还留着** —— 页面别的字段还在用它（虽然现在只剩它自己按 msgEl 提示）。
   `btn-save-config` 更早就随「对账参数」版块删了。 */

// 旧任务的「删除」按钮。
// ⚠ 2026-09-20（用户：「把兜底去掉吧，不用系统的计划任务」）起**没有「执行」**了：
//   那条任务现在只做"确保服务在跑"，点它等于白跑一趟（还让人以为是在跑对账）。
// ⚠ `data-sched-del` 发的是 `full_name`（Windows 上带反斜杠），
//   `data-sched-label` 是给人看的 —— 弹窗写成「删除「\TaskName」？」很难看。
function schedDeleteButton(t) {
  const full = esc(t.full_name || t.name);
  const label = esc(t.name);
  return { html:
    `<button class="btn ghost small nowrap" data-sched-del="${full}"`
    + ` data-sched-label="${label}">删除</button>` };
}

/* ⚠ 2026-09-20（用户）：「**自动化跑什么 … 这些去掉吧，也不用设置了**」——
   这一段（`renderAutomation` / `pickedAutomation` / 保存按钮）**整个删了**。
   跑哪几步不再是设置：声明了唤醒时刻的步骤**一律都跑**（后端 `timer.tasks()` 里也是这么给的）。
   留着这段的话，它会去渲染一组**界面上已经不存在的**复选框（`#automation-box` 没了）——
   `$$('#automation-box [data-auto]')` 拿到空数组，看着不报错，其实什么都不干。 */

/* 「上报 bug」—— 把现场日志打包发出去。
   ⚠ 界面上必须把**包的路径**显示出来：自动发送失败是常态
   （要报的 bug 很可能就是"推送坏了"），那时候用户得能自己把文件发出去。 */
$('#btn-clear-pools-notify')?.addEventListener('click', async () => {
  const msg = $('#clear-pools-notify-msg');
  try {
    const r = await api('/api/pools-notify/clear', { method: 'POST', body: {} });
    // ⚠ 如实显示"本来就没有" —— 永远回"已清除"的话，用户分不清
    //   是清成功了还是按钮压根没生效。
    msg.textContent = r.message || (r.had ? '已清除' : '本来就没有');
  } catch (e) {
    msg.textContent = '失败：' + e.message;
  }
});

$('#btn-report-bug')?.addEventListener('click', async () => {
  const btn = $('#btn-report-bug');
  const msg = $('#report-bug-msg');
  const box = $('#report-bug-result');
  btn.disabled = true;
  msg.textContent = '正在收集并发送（最多约半分钟）…';
  box.innerHTML = '';
  try {
    const r = await api('/api/report-bug', { method: 'POST', body: {} });
    if (!r.ok) {
      msg.textContent = '';
      box.innerHTML = `<div class="banner bad">❌ ${esc(r.message || '上报失败')}</div>`;
      return;
    }
    msg.textContent = '';
    const line = (k, v) => `<div class="form-row" style="margin:4px 0">
      <span class="hint" style="min-width:64px">${k}</span>
      <span>${esc(v || '—')}</span></div>`;
    box.innerHTML = `<div class="banner ${r.sent && r.sent.length ? 'ok' : 'warn'}">
        ${r.sent && r.sent.length
          ? `✅ 已通过 <b>${esc(r.sent.join('、'))}</b> 发出`
          : '⚠️ 自动发送没成功 —— <b>这也可能正是你要报的那个 bug</b>'}
      </div>`
      + line('邮件', r.mail) + line('企微', r.wecom)
      + `<div class="form-row" style="margin:8px 0 0 0">
           <span class="hint" style="min-width:64px">包</span>
           <span class="mono" style="word-break:break-all">${esc(r.path)}</span>
         </div>
         <p class="hint" style="margin:6px 0 0 0">
           大小 ${r.size_kb} KB，含 ${(r.entries || []).length} 个文件：
           ${esc((r.entries || []).join('、'))}<br>
           ⚠ 包里<b>没有凭据</b>（<code>.secrets\</code> 整个没进包），
           但有业务数据（门店名 / 串号 / 金额）。<br>
           ${r.sent && r.sent.length ? '' :
             '<b>把上面那个文件直接发给开发者就行。</b>'}
         </p>`;
  } catch (e) {
    msg.textContent = '';
    box.innerHTML = `<div class="banner bad">❌ 上报失败：${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

/* ───────────────── 更新日志弹窗（每版只弹一次）─────────────────
   用户 2026-09-16："每次更新第一次启动加个更新日志的弹窗，
   然后给门店强调一下要做啥"。

   ⚠ 决定弹不弹的是**后端**（`overview.whatsnew` 为 null 就不弹）——
     前端不自己判断"这个版本看过没"，那种状态放前端一定会漂。 */
let wnShowing = null;

/* ⚠ **弹出来就算看过**（用户 2026-09-18 定）。
   原来只在点正中那个「知道了」时记 `seen`，于是另外三条关掉的路**都不记**：

     ① 点弹窗外面那块灰的   ② 点待办里的「去运行 / 去设置」
     ③ 弹窗开着直接刷新页面

   于是下次打开控制台**又弹一次**。②最容易被踩 ——
   门店看到的第一条待办旁边就挂着「去运行」。 */
async function markWhatsNewSeen(version) {
  if (!version) return;
  try {
    await api('/api/whatsnew/seen', { method: 'POST', body: { version } });
  } catch (e) { /* 记不上就下次再弹一次，不值得打扰用户 */ }
}

/* `force=true` = 从「设置 → 检查更新 → 看这一版的更新说明」翻回来重看的。
   ⚠ 那时候**不再记一次**（`seen` 早就是这一版了），而且**绕过 `wnShowing`** ——
   它就是用来"我已经关掉但还想再看一眼"的。 */
function renderWhatsNew(wn, force) {
  const mask = $('#whatsnew-mask');
  if (!mask) return;
  if (!wn || (!force && wnShowing === wn.version)) return;   // 没有 / 已经弹过这一版
  wnShowing = wn.version;
  $('#wn-head').textContent = `已更新到 v${wn.version}`;
  $('#wn-title').textContent = wn.title || '';
  $('#wn-highlights').innerHTML = (wn.highlights || [])
    .map((x) => `<li>${inlineMd(x)}</li>`).join('');
  const todo = wn.todo || [];
  $('#wn-todo-sec').hidden = !todo.length;
  // 「要做什么」**必须比「改了什么」显眼** —— 门店不看改动没关系，
  // 漏做那几步会真的出问题（比如旧定时任务没删 = 一天跑两遍）
  $('#wn-todo').innerHTML = todo.map((t, i) => `
    <div class="todo-item">
      <div class="todo-num">${i + 1}</div>
      <div class="todo-text">${inlineMd(t.text)}</div>
      ${t.go ? `<button class="btn small todo-go" data-go="${esc(t.go)}">
                  ${esc(t.go_label || '去看看')}</button>` : ''}
    </div>`).join('');
  $$('#wn-todo [data-go]').forEach((b) => b.addEventListener('click', () => {
    closeWhatsNew();          // 记 seen 在弹出来那一步已经做过了（见上）
    goto(b.dataset.go);
  }));
  mask.hidden = false;
  if (!force) markWhatsNewSeen(wn.version);
}

function closeWhatsNew() {
  const mask = $('#whatsnew-mask');
  if (mask) mask.hidden = true;
  // ⚠ 关掉「已更新」之后，把被它挡住的**老任务提示**放出来 ——
  //   两个弹窗的触发时机完全一样，同时弹会叠两层遮罩（见 renderLegacyPrompt）。
  if (lgWaiting) {
    const lp = lgWaiting;
    lgWaiting = null;
    renderLegacyPrompt(lp);
  }
}

async function ackWhatsNew() {
  // 显示时已经记过一次；这里**再记一次是补记** —— 那一次可能网断了/写失败了。
  const v = wnShowing;
  closeWhatsNew();
  await markWhatsNewSeen(v);
}

/* 弹窗里的文字允许 **粗体** 和 `代码`（后端写的是 markdown 风格）。
   ⚠ 先 esc 再替换 —— 反过来的话就是自己给自己开了个 XSS 口子。
   （`table()` 那个「忘了包 {html:}」的坑是"显示成源码"，这个是"注进去"，两回事。） */
function inlineMd(s) {
  return esc(String(s || ''))
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

/* 跳到某个标签页 —— 弹窗里「去设置 / 去运行」那些按钮用 */
//: whatsnew 的 `go` → 到底跳哪儿（一级页签 + 二级标签）。
//: ⚠ 设置那几条（`settings` / `general` / `linglong`）**在那儿没有一级页签** ——
//:   它们全在左下角浮层里。这里仍然写成 `['settings', '<二级>']`：
//:   `settings` 是**面板**的名字（`#panel-settings`），`switchTab` 认它。
//:   写成 `['linglong']` 那种"当成一级页签切"才会点了没反应（`panel-linglong` 不存在）。
const GO_TARGETS = {
  sales: ['sales'],
  compliance: ['compliance'],
  // ⚠ M16（2026-09-20）：库存盘点也是真页签 —— 3.0.0 那条「去库存盘点看一眼」
  //   的待办靠它跳（`goto('inventory')` → `switchTab('inventory')` → 落它唯一那个
  //   二级项，`SUBTAB_LOADERS` 顺手把 iframe 挂上）。
  //   不在表里的话 `GO_TARGETS[tab] || [tab]` 会兜成 `switchTab('inventory')`，
  //   其实也能落对 —— 但 `test_whatsnew` 那条"每个 go 都在前端跳转表里"会红，
  //   而它的存在是为了"别让待办按钮点了没反应"，所以照规矩登记。
  inventory: ['inventory'],
  settings: ['settings', 'general'],
  general: ['settings', 'general'],
  // 数据交换（M20）—— 2026-09-21 从一级标签搬下来（用户定的），
  // ⚠ 它照旧**只有区长/平台看得见**（后端 `pages`），而且门店调接口是 403。
  stores: ['settings', 'stores'],
  linglong: ['settings', 'linglong'],
};

function goto(tab) {
  // ⚠ 「运行」**不是页签**（2026-09-18 起它进了常驻抽屉）——
  //   而 whatsnew 里好几条待办是 `go: "run"`（去跑一次 / 看日志）。
  //   不特判的话 `panel-run` 会被当成面板显示，但抽屉本身还是 hidden，
  //   门店点完**什么都看不见**（这正是 2.x 踩过的"点了没反应"）。
  if (tab === 'run') { setRunDrawer(true); return; }
  const target = GO_TARGETS[tab] || [tab];
  switchTab(target[0], target[1]);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

$('#btn-wn-ok')?.addEventListener('click', ackWhatsNew);
$('#whatsnew-mask')?.addEventListener('click', (e) => {
  // 点灰底只关窗 —— **不用再记 seen 了**：弹出来那一步已经记过（见 renderWhatsNew）。
  // （原来这里写着"点遮罩也算知道了，但不点按钮就不会记"，自相矛盾，
  //   实际效果就是"点遮罩关掉，下次打开又弹"。）
  if (e.target === $('#whatsnew-mask')) closeWhatsNew();
});

/* 「设置 → 检查更新 → 看这一版的更新说明」——
   ⚠ 取的是**升级那天弹给门店的那一份存档**，不是现算的（见 `/api/whatsnew`）。
   现算的话 `seen` 已经是这一版了，「要做什么」那一段会是空的。 */
$('#btn-whatsnew-show')?.addEventListener('click', async () => {
  const btn = $('#btn-whatsnew-show');
  btn.disabled = true;
  try {
    const r = await api('/api/whatsnew');
    if (r && r.body) renderWhatsNew(r.body, true);
    else toast('这一版没有更新说明', 'bad');
  } catch (e) {
    toast('读不到：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
  }
});

/* 老定时任务：**更新后第一次打开控制台主动弹一次**（用户 2026-09-17 选的方案 C）。

   ⚠ 决定弹不弹的是**后端**（`overview.legacy_prompt.show`）——
     前端不自己记"弹过没"，那种状态放前端一定会漂。
   ⚠ 只有**用户点按钮**才提权（服务是无人值守的，绝不能自动弹 UAC）。 */
let lgShowing = false;
// 「已更新」那个弹窗开着的时候，先把老任务提示**存这儿**（见下）。
let lgWaiting = null;

function renderLegacyPrompt(lp) {
  const mask = $('#legacy-mask');
  if (!mask) return;
  if (!lp || !lp.show || lgShowing) return;
  // ⚠ **两个弹窗别同时弹。** 它俩的触发时机**完全一样**（升级后第一次打开控制台），
  //   而 `.modal-mask` 都是 `position:fixed; inset:0; z-index:200` ——
  //   同时显示 = **两层遮罩叠在一起**（背景发黑），而且 DOM 靠后的
  //   「已更新」压在上面，用户根本不知道底下还压着一个。
  //   所以：先让「已更新」说完，**关掉之后**再弹这个。
  if (!$('#whatsnew-mask').hidden) { lgWaiting = lp; return; }
  lgShowing = true;
  const names = lp.names || [];
  $('#lg-sub').textContent = `v${lp.version || ''}`;
  $('#lg-why').innerHTML =
    `表里有 <b>${names.length}</b> 条<b>老名字</b>的定时任务：`
    + names.map((n) => `<code>${esc(n)}</code>`).join('、')
    + `<br>任务改过名（现在叫「${esc(lp.task_name || '')}」），而 Windows 上`
    + `<b>不同名就是并存、不是覆盖</b> —— 这几条会<b>各自每天跑一遍</b>。`
    + `<br><span class="hint">点「一键处理」会<b>先把新的建好</b>、`
    + `确认建成之后<b>才</b>去删老的。需要管理员权限时会弹一次 UAC，`
    + `你自己点一下就行（不会自动弹）。</span>`;
  mask.hidden = false;
}

function closeLegacyPrompt() {
  const mask = $('#legacy-mask');
  if (mask) mask.hidden = true;
}

async function ackLegacyPrompt() {
  closeLegacyPrompt();
  try {
    await api('/api/schedule/legacy-prompt/seen', { method: 'POST', body: {} });
  } catch (e) { /* 记不上就下次再弹一次，不值得打扰用户 */ }
}

$('#btn-legacy-later')?.addEventListener('click', ackLegacyPrompt);

$('#btn-legacy-fix')?.addEventListener('click', async () => {
  const btn = $('#btn-legacy-fix');
  const box = $('#lg-result');
  btn.disabled = true;
  box.innerHTML = '<div class="banner">正在处理…'
    + '<span class="hint">如果弹出 UAC 授权框，点「是」</span></div>';
  try {
    const r = await api('/api/schedule/replace', { method: 'POST', body: {} });
    // ⚠ 后端已经把话说清楚了（尤其是"新任务没建成、老的一条都没动"），
    //   **原样显示**，别在这儿另编一句 —— 编错一句就会让人以为"删干净了"。
    box.innerHTML = `<div class="banner ${r.ok ? 'ok' : 'bad'}">`
      + `${esc(r.message || '')}</div>`;
    if (r.ok) {
      btn.textContent = '已处理';
      setTimeout(ackLegacyPrompt, 1500);
    }
  } catch (e) {
    box.innerHTML = `<div class="banner bad">失败：${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

/* 升级记录 —— 「什么时候升的级、从哪一版升上来的」。
   ⚠ 只显示**真升过级**的那几条（第一条例是 `from: ""`，那是刚装上）。 */
function renderUpgrades(list) {
  const box = $('#upgrade-history');
  if (!box) return;
  const items = list || [];
  if (!items.length) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="hint" style="line-height:1.9"><b>升级记录</b><br>'
    + items.slice(-6).reverse().map((h) =>
        `· v${esc(h.from)} → <b>v${esc(h.to)}</b>
         <span class="hint">${esc(h.at || '')}</span>`).join('<br>')
    + '</div>';
}

function renderSchedule(sch) {
  const box = $('#schedule-box');
  if (!box) return;
  const tasks = (sch && sch.tasks) || [];
  // ⚠⚠ 2026-09-20（用户：「**把兜底去掉吧，不用系统的计划任务**」）：
  //   这块从"注册/管理一条 Windows 计划任务"变成了**只做一件事** ——
  //   老门店机器上当年注册的那条任务，现在没用了，提示删掉。
  //   * **没有任务时什么都不显示**（以前会挂一条"还没注册定时任务，得手动跑才对账"，
  //     而现在那句话是错的：到点由服务里的定时器跑，服务靠开机自启常驻）；
  //   * 有任务时给一句话 + 一个「删除」，**没有「添加」也没有「执行」**：
  //     添加是产品不再提供的动作，执行只会让它去"确保服务在跑"（白跑一趟）。
  if (!tasks.length) {
    box.innerHTML = (sch && sch.error)
      ? `<p class="hint">（读旧的系统计划任务时出错：${esc(sch.error)}—— 不影响自动跑）</p>`
      : '';
    return;
  }
  const rows = tasks.map((t) => [
    { html: `<span class="mono">${esc(t.name)}</span>` },
    t.time ? `每天 ${t.time}` : { html: '<span class="hint">时间读不出来</span>' },
    schedDeleteButton(t),
  ]);
  box.innerHTML = `<div class="banner warn">
      ⚠️ 这台机器上还留着 <b>${tasks.length}</b> 条<b>旧的系统计划任务</b> ——
      <b>现在不需要了</b>（到点由上面的定时器跑，服务靠开机自启常驻）。
      <br><span class="hint">留着它只会到点白跑一趟（它现在只做"确保服务在跑"）。
      建议删掉：点右边「删除」，只删这一条，别的系统任务不动。</span>
    </div>`
    + table(['任务名', '执行时间', ''], rows, ['', '', 'right'])
    + (tasks.some((t) => t.unreadable) ? `<div class="banner warn">
        ⚠️ 有一条**读不到详情**（时间和命令都是空的）—— 它不是"没设"，
        而是**以前用管理员身份建的**（任务归 <code>Administrators</code> 所有，
        而服务现在是普通权限）⇒ 普通权限**删不掉**它。
        <br><button class="btn" data-sched-del-admin="${esc((tasks.find((t) => t.unreadable) || {}).name || '')}">
          以管理员身份删除</button>
        <span class="hint">只弹这一次 UAC；删掉之后就不再有系统计划任务了</span>
      </div>` : '');
}

// 删不掉的那条（管理员建的）：**提权删** —— 只弹一次 UAC。
// ⚠ 发的是**叶子名**（`t.name`），不是 `full_name`：后者带反斜杠，
//   后端当非法字符拒掉（400），表现是"点了完全没反应"（老版本真这样）。
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest && ev.target.closest('[data-sched-del-admin]');
  if (!btn) return;
  const name = btn.dataset.schedDelAdmin || '';
  if (!name) return;
  if (!confirm(`用管理员权限删掉「${name}」？\n\n会弹一次 UAC，请点「是」。\n`
               + `删的是那条**旧的系统计划任务** —— 现在的自动跑不受影响。`)) return;
  btn.disabled = true;
  const box = $('#sched-result');
  if (box) box.innerHTML = '<div class="banner warn">已弹出 UAC 窗口 —— 请点「是」…</div>';
  try {
    const r = await api('/api/elevate', { method: 'POST',
                                          body: { what: 'schedule-remove', name } });
    if (box) {
      box.innerHTML = r.ok
        ? '<div class="banner ok">✅ 已删掉那条旧的系统计划任务。</div>'
        : `<div class="banner bad">还是没成：${esc(r.message || '')}</div>`;
    }
    toast(r.ok ? '已删除' : '没成', r.ok ? 'ok' : 'bad');
    loadOverview();
  } catch (e) {
    if (box) box.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
});

// 删旧任务：**事件委托**（那块每次重渲染，绑在按钮上会丢）。
// ⚠ 2026-09-20 起这张表**只用来删旧的** —— 不再有「执行」（那条任务现在只做
//   "确保服务在跑"，点了等于白跑一趟）也不再有「添加」（产品不再注册计划任务）。
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest && ev.target.closest('[data-sched-del]');
  if (!btn) return;
  const name = btn.dataset.schedDel;                 // 发给后端的（Windows 上是 \TaskName）
  const label = btn.dataset.schedLabel || name;      // 给人看的
  if (!confirm(`删除旧的系统计划任务「${label}」？\n\n`
               + `现在的自动跑不受影响（到点由服务里的定时器跑）。\n`
               + `只删这一个，别的系统任务不动。`)) return;
  btn.disabled = true;
  try {
    const r = await api('/api/schedule?name=' + encodeURIComponent(name), { method: 'DELETE' });
    const box = $('#sched-result');
    if (box) {
      box.innerHTML = r.ok
        ? `<div class="banner ok">已删除旧的系统计划任务「${esc(label)}」——现在的自动跑不受影响。</div>`
        : `<div class="banner bad">删除「${esc(label)}」失败：${esc(r.message || '')}`
          + `（多半要管理员权限，见下面那句）</div>`;
    }
    toast(r.ok ? '已删除' : '删除失败', r.ok ? 'ok' : 'bad');
    loadOverview();
  } catch (e) {
    btn.disabled = false;
    toast('删除失败：' + e.message, 'bad');
  }
});

/* ─────────────────── 左侧栏折叠（收起来给内容区腾地方）───────────────────

   用户 2026-09-18：「加个折叠的按钮，可以把左侧的标签栏折叠隐藏和展开」。
   ⚠ 开关本身**在侧栏外面**（固定定位，见 index.html）——
   放里面的话一收起它自己也被藏了，就再也展不开。
   状态记在 localStorage：收起是"我要看宽表格"的意思，刷新一次就弹回来很烦。 */

const SIDE_KEY = 'cbg-side-collapsed';

function setSidebarCollapsed(collapsed) {
  document.body.classList.toggle('side-collapsed', collapsed);
  const btn = $('#btn-sidebar');
  if (btn) {
    // 箭头方向靠 CSS 转（`.side-collapsed .side-toggle svg { rotate(180deg) }`）——
    // JS 只管语义（title / aria），不碰像素
    btn.title = (collapsed ? '展开' : '收起') + '左侧栏';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }
  try {
    localStorage.setItem(SIDE_KEY, collapsed ? '1' : '0');
  } catch (e) { /* 无痕/禁用存储：记不住就记不住，不影响用 */ }
}

let sidebarCollapsed = false;
try {
  sidebarCollapsed = localStorage.getItem(SIDE_KEY) === '1';
} catch (e) { /* 同上 */ }
setSidebarCollapsed(sidebarCollapsed);   // ⚠ 首屏就得应用，不然会"闪一下再收起"

$('#btn-sidebar')?.addEventListener('click', () =>
  setSidebarCollapsed(!document.body.classList.contains('side-collapsed')));

/* ───────────────────────── 主题一键切换 ─────────────────────────
   机制固定：body[data-theme] + localStorage，**不加后端接口**
   （执行规范 7.2；modules/theme 不提供 /api/theme）。
   ⚠ 首屏同步应用、不等任何请求 —— 等 /api/* 回来再应用会闪一下浅色。
   ⚠ 一套主题的色值只在 `web/themes/<名>.css`（一主题一文件，用户 2026-09-22）。
   ⭐ 日夜自动切换（2026-09-22）：白天/夜晚各用哪套**门店在主题设置里选**；
      同样只记 localStorage。检查间隔 **约 15 分钟**（用户嫌每分钟太狠）。 */

const THEME_KEY = 'cbg-theme';
const THEME_AUTO_KEY = 'cbg-theme-auto';
const THEME_DAY_KEY = 'cbg-theme-day';
const THEME_NIGHT_KEY = 'cbg-theme-night';
/** 选中的壁纸**文件名** —— 只记这一处，后端不存「当前壁纸」 */
const WALLPAPER_KEY = 'cbg-wallpaper';
//: 显示方式：cover | contain | repeat（三档，不拉伸）
const WALLPAPER_FIT_KEY = 'cbg-wallpaper-fit';
const WALLPAPER_FITS = ['cover', 'contain', 'repeat'];
/** 照片主题的 data-theme 值（和 modules/theme.PHOTO_THEME / themes/photo.css 对齐） */
const PHOTO_THEME = 'photo';
/** 日夜再判的间隔 —— 刻意放宽（用户 2026-09-22：「每分钟检查有点狠了」） */
const THEME_CHECK_MS = 15 * 60 * 1000;

/** 日出日落（由侧栏天气那次 Open-Meteo 请求填；没拿到就用本地时段兜底） */
let themeSunrise = null;
let themeSunset = null;
let themeCheckTimer = null;

function currentThemeName() {
  return document.body.dataset.theme || 'default';
}

function lsGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

function lsSet(key, val) {
  try {
    if (val == null || val === '') localStorage.removeItem(key);
    else localStorage.setItem(key, val);
  } catch (e) { /* 无痕/禁用存储：记不住就记不住 */ }
}

/** 把当前主题刷到「主题设置」页的两张卡 + 下拉上（切页时也调）。 */
function syncThemeUi() {
  const cur = currentThemeName();
  const sel = $('#theme-select');
  if (sel && sel.value !== cur) sel.value = cur;
  $$('[data-theme-pick]').forEach((b) => {
    const on = (b.dataset.themePick || 'default') === cur;
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

/**
 * 套主题。
 * `opts.manual` = **用户在上面那组卡片/下拉里点的** ⇒ 顺手把下面的
 * 「日夜自动切换」关掉（2026-09-22 用户：手动切了就别再被自动盖回去）。
 * 自动调度 / 首屏恢复**不要**传 manual —— 否则自动开一次就把自己关了。
 */
function setTheme(name, opts) {
  const manual = !!(opts && opts.manual);
  const n = (name && name !== 'default') ? String(name) : '';
  if (n) document.body.dataset.theme = n;
  else delete document.body.dataset.theme;
  lsSet(THEME_KEY, n);
  applyWallpaper();
  if (manual && themePrefAuto()) {
    lsSet(THEME_AUTO_KEY, '0');
    const auto = $('#theme-auto');
    if (auto) auto.checked = false;
    renderThemeAutoStatus('（手动切主题，已关掉自动）');
  }
  syncThemeUi();
  // 盘点已并进同文档 —— body[data-theme] 直接生效，不用 postMessage
  // 小工具两个 iframe 仍吃不到父页 CSS 变量 —— 一起通知
  for (const id of ['#pricetag-frame', '#badge-frame']) {
    try {
      const tf = $(id);
      if (tf && tf.contentWindow) {
        tf.contentWindow.postMessage({ ic: 'theme', name: n }, location.origin);
      }
    } catch (e) { /* 未挂载 */ }
  }
}

function themePrefAuto() {
  return lsGet(THEME_AUTO_KEY) === '1';
}

function themePrefDay() {
  return lsGet(THEME_DAY_KEY) || 'default';
}

function themePrefNight() {
  return lsGet(THEME_NIGHT_KEY) || 'dark';
}

/** 现在算不算「白天」：有日出日落用它们，否则本地 06:00–18:00 兜底。 */
function themeIsDay() {
  if (themeSunrise && themeSunset) {
    const now = Date.now();
    return now >= themeSunrise.getTime() && now < themeSunset.getTime();
  }
  const h = new Date().getHours();
  return h >= 6 && h < 18;
}

function themeTargetName() {
  return themeIsDay() ? themePrefDay() : themePrefNight();
}

function renderThemeAutoStatus(extra) {
  const el = $('#theme-auto-status');
  if (!el) return;
  const bits = [];
  if (!themePrefAuto()) {
    bits.push('自动切换已关闭。');
  } else {
    const day = themePrefDay();
    const night = themePrefNight();
    bits.push(themeIsDay()
      ? `当前白天时段 →「${day}」`
      : `当前夜晚时段 →「${night}」`);
    bits.push(`白天「${day}」· 夜晚「${night}」`);
    bits.push('约 15 分钟检查一次，切回本页或回前台时也会立刻判。');
  }
  if (extra) bits.push(extra);
  el.textContent = bits.join(' ');
}

/** 自动开着才按映射覆盖当前主题（关掉则完全不碰手动选择）。 */
function applyThemeSchedule() {
  if (!themePrefAuto()) {
    renderThemeAutoStatus();
    return;
  }
  setTheme(themeTargetName());
  renderThemeAutoStatus();
}

function startThemeSchedule() {
  if (themeCheckTimer) clearInterval(themeCheckTimer);
  themeCheckTimer = setInterval(applyThemeSchedule, THEME_CHECK_MS);
}

// 回前台立刻补判一次（睡醒跨过日出日落时不用干等下一轮）
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) applyThemeSchedule();
});

try {
  setTheme(lsGet(THEME_KEY) || 'default');
} catch (e) {
  setTheme('default');
}

// 手动选主题（下拉 + 风格卡）⇒ 关掉日夜自动（setTheme 里判 manual）
$('#theme-select')?.addEventListener('change', (e) =>
  setTheme(e.target.value, { manual: true }));
// 风格卡：点一下就切（和下拉共用 setTheme，不会出现两份状态）
$$('[data-theme-pick]').forEach((b) => {
  b.addEventListener('click', () =>
    setTheme(b.dataset.themePick, { manual: true }));
});

/* —— 主题设置页：自动切换 + 白天/夜晚映射 —— */
(function bindThemeAuto() {
  const auto = $('#theme-auto');
  const day = $('#theme-day');
  const night = $('#theme-night');
  if (!auto || !day || !night) return;

  auto.checked = themePrefAuto();
  day.value = themePrefDay();
  night.value = themePrefNight();

  auto.addEventListener('change', () => {
    lsSet(THEME_AUTO_KEY, auto.checked ? '1' : '0');
    if (auto.checked) applyThemeSchedule();
    else renderThemeAutoStatus();
  });
  day.addEventListener('change', () => {
    lsSet(THEME_DAY_KEY, day.value || 'default');
    if (themePrefAuto()) applyThemeSchedule();
    else renderThemeAutoStatus();
  });
  night.addEventListener('change', () => {
    lsSet(THEME_NIGHT_KEY, night.value || 'default');
    if (themePrefAuto()) applyThemeSchedule();
    else renderThemeAutoStatus();
  });
  renderThemeAutoStatus();
})();

startThemeSchedule();
if (themePrefAuto()) applyThemeSchedule();

/* ───────────────── 自定义壁纸（自定义照片主题）─────────────────
   文件：GET/POST/DELETE /api/wallpaper（**只管文件**，不提供 /api/theme）。
   选中态：localStorage `cbg-wallpaper` = 文件名 —— 后端不记「当前是哪张」。
   应用：仅当 data-theme === photo 时把图铺到 body（applyWallpaper）。 */

function currentWallpaper() {
  return lsGet(WALLPAPER_KEY) || '';
}

function wallpaperUrl(name) {
  return '/wallpaper/' + encodeURIComponent(name);
}

/** 把选中壁纸刷到 body（只在照片主题下生效）。切主题 / 选图 / 删图都会调。 */
//: 照片主题抽色 —— 选中壁纸后从图里抽主色，只改**强调/底色调**，
//: 文字与卡片仍走 photo.css 的半透明浅底（花照片上也要能读）。
//: 结果写在 body 内联 CSS 变量上；清壁纸 / 切走 photo 主题时抹掉。
function clearWallpaperPalette() {
  const s = document.body.style;
  // ⚠ --bg 必须清：抽色时 setProperty 写过，不清会**盖住换掉后的主题底色**
  ['--bg', '--brand', '--brand-dark', '--brand-wash', '--focus-ring',
   '--glow-fab', '--glow-fab-hover', '--photo-accent'].forEach((k) => s.removeProperty(k));
}

/** 画到小 canvas，按色相桶取「最显眼」主色 + 平均色（当底调）。 */
function extractWallpaperPalette(img) {
  const W = 48, H = 48;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.drawImage(img, 0, 0, W, H);
  let data;
  try { data = ctx.getImageData(0, 0, W, H).data; } catch (e) { return null; } // 跨域等
  let rSum = 0, gSum = 0, bSum = 0, n = 0;
  // 色相桶：饱和、亮度适中的像素计票
  const buckets = new Array(12).fill(0);
  const pick = [0, 0, 0];
  let best = 0;
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2];
    const a = data[i + 3];
    if (a < 200) continue;
    rSum += r; gSum += g; bSum += b; n++;
    const max = Math.max(r, g, b), min = Math.min(r, g, b);
    const sat = max === 0 ? 0 : (max - min) / max;
    const lum = (r * 0.299 + g * 0.587 + b * 0.114) / 255;
    if (sat < 0.22 || lum < 0.12 || lum > 0.88) continue;
    let h = 0;
    if (max !== min) {
      if (max === r) h = ((g - b) / (max - min)) % 6;
      else if (max === g) h = (b - r) / (max - min) + 2;
      else h = (r - g) / (max - min) + 4;
      h = ((h * 60) + 360) % 360;
    }
    const bi = Math.min(11, Math.floor(h / 30));
    // 权重：饱和 × 距灰
    const w = sat * Math.abs(lum - 0.5) * 2;
    buckets[bi] += w;
    if (buckets[bi] > best) {
      best = buckets[bi];
      pick[0] = r; pick[1] = g; pick[2] = b;
    }
  }
  if (!n || best < 0.5) return null;
  const avg = [Math.round(rSum / n), Math.round(gSum / n), Math.round(bSum / n)];
  return { accent: pick, avg: avg };
}

function rgbCss(r, g, b, a) {
  if (a == null) return 'rgb(' + r + ',' + g + ',' + b + ')';
  return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
}

function darken(r, g, b, f) {
  return [Math.round(r * f), Math.round(g * f), Math.round(b * f)];
}

function applyWallpaperPalette(img) {
  clearWallpaperPalette();
  const pal = extractWallpaperPalette(img);
  if (!pal) return;
  const [ar, ag, ab] = pal.accent;
  // 品牌色：保证可读 —— 太亮就压暗一档
  const lum = (ar * 0.299 + ag * 0.587 + ab * 0.114) / 255;
  let br = ar, bg = ag, bb = ab;
  if (lum > 0.62) {
    const d = darken(br, bg, bb, 0.55);
    br = d[0]; bg = d[1]; bb = d[2];
  } else if (lum < 0.18) {
    br = Math.min(255, Math.round(br + 70));
    bg = Math.min(255, Math.round(bg + 70));
    bb = Math.min(255, Math.round(bb + 70));
  }
  const s = document.body.style;
  s.setProperty('--brand', rgbCss(br, bg, bb));
  s.setProperty('--brand-dark', rgbCss.apply(null, darken(br, bg, bb, 0.82).concat([])));
  s.setProperty('--brand-wash', rgbCss(br, bg, bb, 0.10));
  s.setProperty('--focus-ring', rgbCss(br, bg, bb, 0.35));
  s.setProperty('--photo-accent', rgbCss(ar, ag, ab));
  s.setProperty('--glow-fab',
    'drop-shadow(0 5px 14px ' + rgbCss(br, bg, bb, 0.38) + ')');
  s.setProperty('--glow-fab-hover',
    'drop-shadow(0 7px 18px ' + rgbCss(br, bg, bb, 0.48) + ')');
  // 页面底：用平均色轻微染色，仍半透明 —— 照片透出来、又不抢字
  const [mr, mg, mb] = pal.avg;
  s.setProperty('--bg', rgbCss(mr, mg, mb, 0.55));
}

function currentWallpaperFit() {
  const v = localStorage.getItem(WALLPAPER_FIT_KEY) || 'cover';
  return WALLPAPER_FITS.indexOf(v) >= 0 ? v : 'cover';
}

function setWallpaperFit(fit) {
  const v = WALLPAPER_FITS.indexOf(fit) >= 0 ? fit : 'cover';
  lsSet(WALLPAPER_FIT_KEY, v);
  applyWallpaper();
}

function applyWallpaper() {
  const name = currentWallpaper();
  const on = currentThemeName() === PHOTO_THEME && !!name;
  // 三档 class 始终跟 fit 对齐（含无图时也清干净）
  document.body.classList.remove(
    'wallpaper-fit-cover', 'wallpaper-fit-contain', 'wallpaper-fit-repeat');
  if (on) {
    const url = wallpaperUrl(name);
    document.body.style.backgroundImage = 'url("' + url + '")';
    document.body.classList.add('has-wallpaper');
    document.body.classList.add('wallpaper-fit-' + currentWallpaperFit());
    // 异步抽色：图没画上之前先用 photo.css 默认色（会闪一下，可接受）
    const img = new Image();
    img.onload = () => {
      // 换图途中又切了主题/壁纸则丢弃
      if (currentThemeName() !== PHOTO_THEME) return;
      if (currentWallpaper() !== name) return;
      applyWallpaperPalette(img);
    };
    img.onerror = () => clearWallpaperPalette();
    img.src = url;
  } else {
    document.body.style.backgroundImage = '';
    document.body.classList.remove('has-wallpaper');
    clearWallpaperPalette();
  }
  const fitSel = document.getElementById('wallpaper-fit');
  if (fitSel) fitSel.value = currentWallpaperFit();
}

function setWallpaper(name) {
  lsSet(WALLPAPER_KEY, name || '');
  applyWallpaper();
  renderWallpaperStatus();
  // 图标网格上的选中态
  $$('.wallpaper-item').forEach((el) => {
    el.classList.toggle('is-on', !!name && el.dataset.wpName === name);
  });
}

function renderWallpaperStatus(extra) {
  const el = $('#wallpaper-status');
  if (!el) return;
  const name = currentWallpaper();
  const bits = [];
  if (currentThemeName() === PHOTO_THEME) {
    bits.push(name ? '当前背景：' + name : '当前是照片主题，但还没选图 —— 下面点一张或先上传。');
  } else {
    bits.push(name ? '已记住 ' + name + '（切到「自定义照片主题」后显示）' : '还没选壁纸。');
  }
  if (extra) bits.push(extra);
  el.textContent = bits.join(' ');
}

function renderWallpapers(items) {
  const box = $('#wallpaper-grid');
  if (!box) return;
  const sel = currentWallpaper();
  if (!items || !items.length) {
    box.innerHTML = '<p class="hint" style="grid-column:1/-1">还没有壁纸 —— 上面选一张图上传。</p>';
    return;
  }
  box.innerHTML = items.map((w) => {
    const ok = w.ok !== false;
    const on = ok && w.name === sel;
    return '<div class="wallpaper-item' + (on ? ' is-on' : '') + '" role="listitem"' +
      ' data-wp-name="' + esc(w.name) + '">' +
      (ok
        ? '<img src="' + esc(wallpaperUrl(w.name)) + '" alt="" loading="lazy">'
        : '<div class="wp-meta" style="height:88px;align-items:center;justify-content:center;color:var(--muted)">格式不认</div>') +
      '<div class="wp-meta">' +
      '<span class="wp-name" title="' + esc(w.name) + '">' + esc(w.name) + '</span>' +
      (ok ? '<button type="button" class="wp-use" data-wp-use="' + esc(w.name) + '">' +
        (on ? '使用中' : '用这个') + '</button>' : '') +
      '<button type="button" class="wp-del" data-wp-del="' + esc(w.name) + '" title="删除">删</button>' +
      '</div></div>';
  }).join('');
}

async function loadWallpapers() {
  try {
    const d = await api('/api/wallpaper');
    renderWallpapers(d.items || []);
    renderWallpaperStatus();
  } catch (e) {
    const el = $('#wallpaper-status');
    if (el) el.textContent = '读壁纸列表失败：' + e.message;
  }
}

async function uploadWallpaper() {
  const input = $('#wallpaper-file');
  const file = input && input.files && input.files[0];
  if (!file) { toast('先选一张图片', 'bad'); return; }
  const dot = file.name.lastIndexOf('.');
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';
  const allow = ['.png', '.jpg', '.jpeg', '.webp', '.gif'];
  if (allow.indexOf(ext) < 0) {
    toast('只认 ' + allow.join(' / '), 'bad');
    return;
  }
  if (file.size > 5 * 1024 * 1024) {
    toast('图片超过 5MB', 'bad');
    return;
  }
  const btn = $('#btn-wallpaper-upload');
  if (btn) btn.disabled = true;
  try {
    // ⚠ 请求体 = 图片本身（和盘点导出一个路子），文件名走 query
    const r = await fetch('/api/wallpaper?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    });
    let data = null;
    try { data = await r.json(); } catch (e) { /* 非 JSON */ }
    if (!r.ok) {
      throw new Error((data && (data.error || data.message)) || ('HTTP ' + r.status));
    }
    if (data && data.name) setWallpaper(data.name);
    toast('已上传', 'ok');
    if (input) input.value = '';
    await loadWallpapers();
  } catch (e) {
    toast('上传失败：' + e.message, 'bad');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteWallpaper(name) {
  if (!name) return;
  if (!window.confirm('删除壁纸「' + name + '」？')) return;
  try {
    await api('/api/wallpaper?name=' + encodeURIComponent(name), { method: 'DELETE' });
    if (currentWallpaper() === name) setWallpaper('');
    toast('已删除', 'ok');
    await loadWallpapers();
  } catch (e) {
    toast('删除失败：' + e.message, 'bad');
  }
}


// 显示方式三档 —— 改完立刻 applyWallpaper
$('#wallpaper-fit')?.addEventListener('change', (e) => {
  setWallpaperFit(e.currentTarget.value);
});
$('#btn-wallpaper-upload')?.addEventListener('click', () => { uploadWallpaper(); });
$('#btn-wallpaper-clear')?.addEventListener('click', () => {
  setWallpaper('');
  toast('已取消壁纸', 'ok');
});
$('#wallpaper-grid')?.addEventListener('click', (e) => {
  const t = e.target;
  if (!(t instanceof Element)) return;
  const use = t.closest('[data-wp-use]');
  if (use) {
    const name = use.getAttribute('data-wp-use') || '';
    setWallpaper(name);
    // 只刷新按钮文案 / 选中框，不整表重绘（img 不用重下）
    $$('.wallpaper-item').forEach((el) => {
      const on = el.dataset.wpName === name;
      el.classList.toggle('is-on', on);
      const btn = el.querySelector('[data-wp-use]');
      if (btn) btn.textContent = on ? '使用中' : '用这个';
    });
    toast('已选用壁纸', 'ok');
    return;
  }
  const del = t.closest('[data-wp-del]');
  if (del) deleteWallpaper(del.getAttribute('data-wp-del') || '');
});

// 首屏：恢复照片主题的背景（setTheme 里已调 applyWallpaper，这里兜底空图状态）
applyWallpaper();

/* ───────────────── 侧栏天气 + 日出日落（主题用） ─────────────────
   位置：左下角门店信息旁 `#foot-weather`（用户 2026-09-22）。
   接口：Open-Meteo（免 Key）。失败只影响那一小段文案，**不挡主题**。
   ⚠ 与主题检查同一节奏拉（约 15 分钟）—— 不另开每分钟轮询。 */

const WMO_ZH = {
  0: '晴', 1: '大部晴朗', 2: '局部多云', 3: '阴',
  45: '雾', 48: '雾凇',
  51: '毛毛雨', 53: '毛毛雨', 55: '毛毛雨', 56: '冻毛毛雨', 57: '冻毛毛雨',
  61: '小雨', 63: '中雨', 65: '大雨', 66: '冻雨', 67: '冻雨',
  71: '小雪', 73: '中雪', 75: '大雪', 77: '米雪',
  80: '小阵雨', 81: '阵雨', 82: '强阵雨', 85: '阵雪', 86: '强阵雪',
  95: '雷阵雨', 96: '雷阵雨冰雹', 99: '强雷暴冰雹',
};

function wmoZh(code) {
  return WMO_ZH[code] != null ? WMO_ZH[code] : '天气未知';
}

function parseLocalISO(s) {
  if (!s) return null;
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], 0, 0);
}

function fmtHM(d) {
  if (!d) return '--:--';
  const h = d.getHours();
  const m = d.getMinutes();
  return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}

function geoCacheKey() { return 'cbg-geo'; }

function readGeoCache() {
  try {
    const raw = localStorage.getItem(geoCacheKey());
    if (!raw) return null;
    const o = JSON.parse(raw);
    if (!o || o.lat == null || o.lng == null) return null;
    // 7 天过期
    if (o.at && Date.now() - o.at > 7 * 24 * 3600 * 1000) return null;
    return o;
  } catch (e) { return null; }
}

function writeGeoCache(lat, lng) {
  try {
    localStorage.setItem(geoCacheKey(), JSON.stringify({ lat: lat, lng: lng, at: Date.now() }));
  } catch (e) { /* 忽略 */ }
}

function fetchWithTimeout(url, ms) {
  const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
  const timer = setTimeout(() => { if (ctrl) ctrl.abort(); }, ms || 8000);
  return fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
    .finally(() => clearTimeout(timer));
}

function ipGeo() {
  // 控制台跑在 http://127.0.0.1 —— ip-api 免费且带 CORS；失败回落默认上海
  return fetchWithTimeout(
    'http://ip-api.com/json/?fields=status,lat,lon&lang=zh-CN', 6000)
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error('ip http'))))
    .then((d) => {
      if (!d || d.status !== 'success' || d.lat == null) throw new Error('ip empty');
      return { lat: d.lat, lng: d.lon };
    });
}

function resolveGeo() {
  const cached = readGeoCache();
  if (cached) return Promise.resolve(cached);
  // 门店机器固定：优先浏览器定位（用户可能拒绝）→ IP → 上海
  const tryGeo = (navigator.geolocation)
    ? new Promise((resolve, reject) => {
      navigator.geolocation.getCurrentPosition(
        (pos) => resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude }),
        reject,
        { enableHighAccuracy: false, timeout: 5000, maximumAge: 7 * 24 * 3600 * 1000 },
      );
    })
    : Promise.reject(new Error('no geolocation'));
  return tryGeo
    .catch(() => ipGeo())
    .catch(() => ({ lat: 31.23, lng: 121.47 }))
    .then((g) => { writeGeoCache(g.lat, g.lng); return g; });
}

async function loadFootWeather() {
  const el = $('#foot-weather');
  const lifehallEl = $('#lifehall-weather');
  try {
    const geo = await resolveGeo();
    const url = 'https://api.open-meteo.com/v1/forecast'
      + '?latitude=' + geo.lat + '&longitude=' + geo.lng
      + '&current=temperature_2m,weather_code'
      + '&daily=sunrise,sunset'
      + '&timezone=auto&forecast_days=1';
    const data = await fetchWithTimeout(url, 10000).then((r) => {
      if (!r.ok) throw new Error('weather http ' + r.status);
      return r.json();
    });
    const cur = data.current || {};
    const daily = data.daily || {};
    themeSunrise = parseLocalISO((daily.sunrise || [])[0]);
    themeSunset = parseLocalISO((daily.sunset || [])[0]);

    const temp = (cur.temperature_2m == null || Number.isNaN(+cur.temperature_2m))
      ? null : Math.round(+cur.temperature_2m);
    const cond = wmoZh(cur.weather_code);
    if (el) {
      el.hidden = false;
      // 胶囊文案：「25° 大部晴朗」；title 里带日出日落
      el.textContent = (temp == null ? '' : temp + '° ') + cond;
      el.title = '天气 · 日出 ' + fmtHM(themeSunrise) + ' · 日落 ' + fmtHM(themeSunset);
    }
    if (lifehallEl) {
      lifehallEl.hidden = false;
      lifehallEl.textContent = (temp == null ? '' : temp + '° ') + cond
        + ' · 日出 ' + fmtHM(themeSunrise) + ' · 日落 ' + fmtHM(themeSunset);
      lifehallEl.title = '天气 · 日出 ' + fmtHM(themeSunrise) + ' · 日落 ' + fmtHM(themeSunset);
    }
    // 拿到真实日出日落后再按映射判一次（可能刚跨过边界）
    if (themePrefAuto()) applyThemeSchedule();
    else renderThemeAutoStatus();
  } catch (e) {
    // 天气挂了：主题仍可用（时段兜底）；侧栏不硬塞错误字
    if (el) { el.hidden = true; el.textContent = ''; }
    if (lifehallEl) {
      lifehallEl.hidden = false;
      lifehallEl.textContent = '天气暂不可用 · 日出 --:-- · 日落 --:--';
      lifehallEl.title = '天气服务暂不可用；联网后自动显示天气与日出日落时间';
    }
    renderThemeAutoStatus(
      '天气暂不可用（' + (e && e.message ? e.message : e) + '），日夜改按本地时段判断。');
  }
}

loadFootWeather();
setInterval(loadFootWeather, THEME_CHECK_MS);

/* ───────────────────────────── 启动 ───────────────────────────── */

// ⚠ **首屏要按"当前亮着的那个页签"加载** —— 2026-09-18 改版前这里写死的是
//   `loadOverview()`（那时默认页是报量查询）。默认页换成「销售」之后，
//   那样写的结果是：报量查询那页的数据白拉一遍、**销售页却一片空白**
//   （连空状态都没渲染出来，看着像页面坏了）。实测截图抓到过。
(async () => {
  // ⚠ **门禁在最前面**：没登录好就停在登录页，别的数据一个都不拉。
  //   拉到一半的 403 会顺带把登录页顶出来（`api()` 里那段），但那样
  //   控制台会先闪一下空页面 —— 先去问一次就没这问题。
  const ready = await checkSetup();
  // 两步都要用的：浏览器检测 + 华为账号（登录页那一步要显示）
  loadBrowserInfo();
  loadHwLogin();
  if (!ready) return;                    // 停在登录页

  // ⚠ 一级标签不可点之后，首屏落在**第一个一级标签的第一个二级页**
  //   （`switchTab` 不带 subtab 时就是落 `SUBTABS[tab][0]`）。
  switchTab('sales');

// ⚠ `loadOverview()` 还得留着 —— 它不只是报量查询那页的数据，**侧边栏左下角的
//   门店名 / 会话·定时徽章也是它填的**。第一次改的时候顺手删了它，
//   结果左下角一直停在「加载中…」、徽章也没字（截图抓到的）。
//   现在两个都要：`switchTab` 管**当前页的内容**，`loadOverview` 管**这一圈的常驻状态**。
  loadOverview();
})();

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
  // ⚠ 2026-09-21：原来这里认的是 `state.timer`（「跑一次」那套用的）——
  //   那套接线随那张卡一起删了 ⇒ 这个字段**再没有人赋值**，条件永远是真。
  //   改成认 `state.jobId`（`watchJob` 盯着一个后台任务时是非空）——
  //   意思没变："正在盯一趟的时候别去打扰它"。
  if (!state.jobId) loadOverview();
}, 30000);
