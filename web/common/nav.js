/* 公共显示层 · 导航 / 页面注册 / 可见性（协议 v2 §4.4，2026-10-02）。
   SUBTABS 是导航键表；迁移中的页面用 PAGE_REGISTRY，旧页面暂由 SUBTAB_LOADERS 兼容；
   applyProfile 按后端 role.pages / role.ops 显隐，权限信息缺失时隐藏菜单和操作。 */

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
  // 库存入口直达盘点；下拉只放库存设置。
  inventory: ['inventory', 'inventory-settings'],
  // ⚠ 增值：防护膜 + 无忧会员权益（2026-09-22）—— 本地 erp_sales 现算，无定时步骤。
  valueadd: ['film', 'benefit', 'valueadd-settings'],
  // ⚠ 小工具（2026-09-22）：价签 / 工牌 —— 各一格 iframe，无定时步骤。
  tools: ['pricetag', 'badge', 'claim-pending'],
  // 分销四种视角在同一工作区内切换；导航只保留一个入口。
  distribution: ['dist-region'],
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
  'sales-settings': () => loadAttainSettings(),
  'compliance-settings': () => loadNotifyPrefs('#notify-pref-list-compliance', ['pos', 'pools']),
  'plan-settings': () => loadNotifyPrefs('#notify-pref-list-plan', ['plan']),
  'inventory-settings': () => loadNotifyPrefs('#notify-pref-list-inventory', ['inventory']),
  // 库存盘点在 web/features/inventory/page.js 注册页面生命周期。
  'valueadd-settings': () => loadNotifyPrefs('#notify-pref-list-valueadd', ['film', 'benefit']),
  // 小工具：价签 / 工牌 —— **各一格 iframe**（#pricetag-frame / #badge-frame），
  // 懒挂载；切二级只显示对应 subpanel（switchTab 已管）
  pricetag: () => mountTools('pricetag'),
  badge: () => mountTools('badge'),
  // 权益领取待领页在 web/features/tools/claim/page.js 注册页面生命周期。
  // 收银由 web/features/cashier/page.js 注册完整页面生命周期。
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

//: 新拆出的页面可声明完整生命周期；未迁移页面暂用上面的兼容 loader。
//: `load` 每次进入都执行；`mount` 只在切入时执行一次；`unmount` 负责释放页面资源；
//: `refresh` 是页面按钮的刷新入口。异步请求由页面持有并在 unmount 时取消。
const PAGE_REGISTRY = Object.create(null);
let _activePageKey = '';

function registerPage(key, lifecycle) {
  if (!key || !lifecycle || typeof lifecycle.load !== 'function') {
    throw new Error('页面注册必须提供 key 和 load：' + key);
  }
  if (PAGE_REGISTRY[key]) throw new Error('页面重复注册：' + key);
  PAGE_REGISTRY[key] = lifecycle;
}

function activatePage(key) {
  if (_activePageKey !== key) {
    const previous = PAGE_REGISTRY[_activePageKey];
    if (previous && typeof previous.unmount === 'function') previous.unmount();
    _activePageKey = key;
    const next = PAGE_REGISTRY[key];
    if (next && typeof next.mount === 'function') next.mount();
  }
  const page = PAGE_REGISTRY[key];
  if (page) return page.load();
  const legacyLoad = SUBTAB_LOADERS[key];
  if (legacyLoad) return legacyLoad();
}

function refreshPage(key, ...args) {
  const page = PAGE_REGISTRY[key];
  if (page && typeof page.refresh === 'function') return page.refresh(...args);
  const legacyLoad = SUBTAB_LOADERS[key];
  if (legacyLoad) return legacyLoad(...args);
}

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
  document.body.classList.toggle('lifehall-edition', !!(setupState && setupState.runtime_lifehall));
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
  activatePage(subtab);
}

/** 等后端 `role.pages` 已应用后，打开第一个可见入口及其第一个可见子页。 */
function openFirstAvailablePage() {
  const button = $$('#sidebar .tab').find((x) => !x.hidden);
  if (!button) return false;

  const tab = button.dataset.tab;
  const direct = button.dataset.directSubtab || '';
  const item = button.closest('.nav-item');
  const sub = item && Array.from(item.querySelectorAll('[data-subtab]'))
    .find((x) => !x.hidden);
  const key = direct || (sub && sub.dataset.subtab) || '';

  // applyProfile 可能已经在发现旧页被隐藏时切到了这个入口；避免重复加载。
  const active = $('#sidebar .tab.active');
  const current = $$('#sidebar .nav-menu .subtab.current').find((x) => !x.hidden);
  const currentKey = active && active.dataset.directSubtab
    ? active.dataset.directSubtab : (current && current.dataset.subtab) || '';
  if (active === button && currentKey === key) return true;

  switchTab(tab, key || undefined);
  return true;
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


/*`（`role_scope` + `forbid`），这一层只管"菜单别露"。
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
let activeRoleOps = null;
let activeRolePages = [];

function operationAllowed(page, op) {
  const pageOps = activeRoleOps && page && Array.isArray(activeRoleOps[page])
    ? activeRoleOps[page] : [];
  return !!(op && activeRolePages.indexOf(page) >= 0 && pageOps.indexOf(op) >= 0);
}

function applyOperationVisibility(root) {
  const controls = root && root.querySelectorAll
    ? Array.from(root.querySelectorAll('[data-op]')) : $$('[data-op]');
  controls.forEach((el) => {
    el.hidden = !operationAllowed(el.dataset.opPage, el.dataset.op);
  });
}

function applyProfile(role) {
  const validProfile = !!(role && typeof role === 'object'
    && ['store', 'manager', 'platform'].indexOf(role.role) >= 0
    && Array.isArray(role.pages)
    && role.ops && typeof role.ops === 'object' && !Array.isArray(role.ops));
  const pages = validProfile ? role.pages : [];
  activeRolePages = pages;
  activeRoleOps = validProfile && role.ops && typeof role.ops === 'object'
    ? role.ops : null;
  // 页面内容也按同一份 pages 收窄；overview 失败或权限载荷不完整时，
  // 不让上一次身份留下的业务数据继续留在可见面板里。
  $$('.panel, .subpanel').forEach((el) => {
    const id = el.id || '';
    if (id.indexOf('subpanel-') === 0) {
      const key = el.dataset.pageKey || id.slice('subpanel-'.length);
      el.hidden = !validProfile || !key || pages.indexOf(key) < 0;
    } else {
      el.hidden = !validProfile;
    }
  });
  $$('#sidebar [data-tab], #sidebar [data-subtab], #sidebar [data-foot]').forEach((el) => {
    const key = el.dataset.subtab || el.dataset.tab || el.dataset.foot;
    el.hidden = !validProfile || !key || pages.indexOf(key) < 0;
  });
  // 入口刚变成不可见时，切到当前身份可见的页面；启动流程也会在拿到 role.pages
  // 后调用 openFirstAvailablePage，确保首个业务请求不会先打到隐藏页面。
  if (validProfile) {
    const cur = $$('#sidebar .tab').find((x) => !x.hidden);
    const active = $('#sidebar .tab.active');
    if (active && (!cur || active.hidden)) openFirstAvailablePage();
  }
  // ⭐ 注册协议 v2（2026-10-02）：带 `data-op` 的控件按后端下发的 `role.ops`
  //   显隐 —— 声明在注册表（`Sub.ops`），后端 `require()` 判的是同一份。
  // ⚠ **fail-closed**：`role.ops` 缺失 / 这一页没声明这个操作 ⇒ 藏起来。
  //   藏错的代价是"按钮没了"（接口也 403，不会更糟）；放开才是安全洞。
  // ⚠ `data-op-page` 必须写（试点只有 film 一个）：控件离页面 key 太远时
  //   没法可靠地推断归属，宁可显式标。
  applyOperationVisibility();
  // ⚠ 这个类名现在没有 CSS 用它了（可见性全走 `hidden`）—— 留着是因为
  //   别的会话/样式可能还在读它，而"顺手删干净"的风险比留个空类大。
  document.body.classList.toggle('no-linglong',
    !validProfile || pages.indexOf('compliance') < 0);
}
