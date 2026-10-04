'use strict';

/* 门店数据平台 · 本地控制台 —— 公共层在 web/common/（base + nav），本文件是业务。 */

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

/* ─────────────── 进入流程选择页（2026-10-02 统一进入流程）───────────────
 *
 * 用户草案的三条入口：授权店 = ERP 登录；平台岗 = ERP 登录并确认平台权限；
 * 生活馆 = 输入并保存门店编码直接进入（**不要求 ERP 登录**）。
 * ⚠ 选择页只在后端 `/api/entry` 说 `needed` 时露脸 —— 判据（没选过 + 没就绪）
 *   全在后端，前端不自己猜；老机器永远见不到它。
 * ⚠ 选错可回：登录页底部「换进入方式」= DELETE /api/entry + 回三张卡。
 */
async function entryState() {
  try { return await api('/api/entry'); } catch (e) { return null; }  // 读不到别拦人
}

function showEntry(opts) {
  const mask = $('#entry-mask');
  if (!mask) return;
  const code = !!(opts && opts.code);
  $('#entry-options').hidden = code;
  $('#entry-code').hidden = !code;
  // 安装生活馆版只有编码表单；运行生活馆入口仍可返回三张选择卡。
  const installedLifehall = !!(setupState && setupState.lifehall);
  const back = $('#btn-entry-code-back');
  if (back) {
    back.hidden = installedLifehall && !(setupState && setupState.ready);
    back.textContent = installedLifehall ? '取消' : '返回';
  }
  const input = $('#entry-store-code');
  const savedCode = setupState && setupState.profile && setupState.profile.huawei_code;
  if (code && input) input.value = savedCode || '';
  const lead = $('#entry-lead');
  if (lead) lead.textContent = code
    ? '输入并保存门店编码即可进入。玲珑授权在进入后单独处理。'
    : '选一个入口。选错也没关系，登录页底部可以换进入方式。';
  const err = $('#entry-error');
  if (err) err.textContent = '';
  mask.hidden = false;
  document.body.classList.add('setup-locked');
}

function hideEntry() {
  const mask = $('#entry-mask');
  if (mask) mask.hidden = true;
  const setup = $('#setup-mask');
  if (!setup || setup.hidden) document.body.classList.remove('setup-locked');
}

/** 进主界面 —— 选择页三条路进来后走同一段（跟启动 IIFE 的尾巴一致）。 */
async function enterConsole() {
  loadBrowserInfo();
  loadHwLogin();
  // 先取后端身份与页面权限，再打开首个可见页；生活馆没有周度达成接口。
  await loadOverview();
  openFirstAvailablePage();
}

async function chooseEntry(kind) {
  const err = $('#entry-error');
  if (err) err.textContent = '';
  try {
    await api('/api/entry', { method: 'POST', body: { kind } });
  } catch (e) {
    if (err) err.textContent = '没存上：' + e.message;
    return;
  }
  if (kind === 'lifehall') {
    // 生活馆：留在选择页上填编码（后端 need="storecode" 也是回这一步）
    showEntry({ code: true });
    const box = $('#entry-store-code');
    if (box) box.focus();
    return;
  }
  hideEntry();
  const ok = await checkSetup();
  if (ok) enterConsole();
}

$('#entry-erp')?.addEventListener('click', () => chooseEntry('erp'));
$('#entry-platform')?.addEventListener('click', () => chooseEntry('platform'));
$('#entry-lifehall')?.addEventListener('click', () => chooseEntry('lifehall'));

$('#btn-entry-code-back')?.addEventListener('click', async () => {
  if (setupState && setupState.lifehall) {
    hideEntry();
    enterConsole();
    return;
  }
  try { await api('/api/entry', { method: 'DELETE' }); } catch (e) { /* 清不掉也回得去卡 */ }
  showEntry({ code: false });
});

$('#btn-entry-code')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  const msg = $('#entry-code-msg');
  const code = (($('#entry-store-code') || {}).value || '').trim();
  if (!code) { if (msg) msg.textContent = '先填门店编码'; return; }
  btn.disabled = true;
  if (msg) msg.textContent = '保存中…';
  try {
    // 入口保存只写店码；玲珑授权的校验、认店和会话处理留到后续操作。
    await api('/api/entry', { method: 'POST', body: { kind: 'lifehall', store_code: code } });
    hideEntry();
    const ok = await checkSetup();
    if (ok) enterConsole();
    else if (msg) msg.textContent = '编码存了，但还没就绪 —— 看一眼提示再试';
  } catch (e) {
    if (msg) msg.textContent = '没成：' + e.message;
  } finally { btn.disabled = false; }
});

/** 平台岗的「确认」—— 后端会**再验一次**账号是不是平台岗（前端按钮只是入口）。 */
$('#btn-entry-confirm')?.addEventListener('click', async (ev) => {
  const btn = ev.currentTarget;
  const msg = $('#setup-confirm-msg');
  btn.disabled = true;
  if (msg) msg.textContent = '';
  try {
    await api('/api/entry', { method: 'POST', body: { kind: 'platform', confirm: 1 } });
    const ok = await checkSetup();
    if (ok) enterConsole();
  } catch (e) {
    if (msg) msg.textContent = '没成：' + e.message;
    toast('没成：' + e.message, 'bad');
  } finally { btn.disabled = false; }
});

/** 换进入方式（登录页底部）：清掉选择 → 回三张卡。 */
$('#btn-entry-switch')?.addEventListener('click', async () => {
  try { await api('/api/entry', { method: 'DELETE' }); } catch (e) { /* 忽略 */ }
  hideSetup();
  showEntry();
});

function showSetup(st) {
  setupState = st || setupState;
  if (setupState && setupState.entry_kind === 'lifehall') {
    hideSetup();
    if (setupState.need === 'storecode') showEntry({ code: true });
    else if (setupState.ready) {
      hideEntry();
      switchTab('tools');
    } else showEntry();
    return;
  }
  const mask = $('#setup-mask');
  if (!mask) return;
  syncLifehallSettings();
  renderStoreCodeForms();
  const p = (setupState && setupState.profile) || {};
  $('#setup-sub').textContent = p.erp_name
    ? `${p.erp_name}${p.marker ? ' · 标识 ' + p.marker : ''}`
    : '还没认出是哪家店';

  // ⭐ 平台岗确认步（进入流程 §4.5）：need="confirm" 时登录两步都让位
  const needConfirm = !!(setupState && setupState.need === 'confirm');
  const needEntryMismatch = !!(setupState && setupState.need === 'entry-mismatch');
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
  const needLL = (p.type || '') === 'experience';
  $('#setup-step-erp').hidden = needConfirm || needEntryMismatch;
  $('#setup-step-linglong').hidden = !needLL || needConfirm || needEntryMismatch;
  const llFullNote = $('#setup-linglong-full-note');
  if (llFullNote) llFullNote.hidden = false;
  // 这一步要露脸 ⇒ 把玲珑那套控件搬进来（不露脸就让它待在玲珑授权页）
  mountLinglong(needLL);
  $('#setup-linglong-num').textContent = needLL ? '2' : '';
  $('#setup-linglong-why').innerHTML = ll.ok ? '' : esc(ll.why || '');
  $('#setup-step-linglong').classList.toggle('done', !!ll.ok);
  $('#setup-lead').innerHTML = needLL
    ? '这台电脑要先把<b>门店的云商账号</b>和<b>玲珑</b>都登录好，才能进主界面。'
      + '<b>不登录、或者登录了但用不了，都不给用。</b>'
    : '这家店（<b>不在串号标识名单里</b>）不走玲珑 —— 只要把<b>云商账号</b>登录好就能用。';

  // ⚠ 2026-09-21（用户：「先把**体验店登录需要玲珑**这个跳过一下……我想看看
  //   **体验店的界面**」）—— 玲珑那步没过时，多给一个「先看看界面」。
  //   `preview_available` 由后端给（= 云商那步过了、只差玲珑），前端不自己判。
  //   生活馆版 2026-09-30 起也有（用户：「登录页加个跳过按钮，让我直接进入」）——
  //   生活馆凭编码进入，后端不给预览；体验店仍沿用原来的预览判据。
  const pv = $('#setup-step-preview');
  if (pv) pv.hidden = !(setupState && setupState.preview_available)
    || needConfirm || needEntryMismatch;

  // 确认步 / 入口选错提示（平台岗入口专属）
  const cf = $('#setup-step-confirm');
  if (cf) cf.hidden = !needConfirm;
  const mm = $('#setup-entry-mismatch');
  if (mm) {
    const platformOnStore = setupState && setupState.entry_mismatch_kind === 'platform-on-erp';
    mm.innerHTML = platformOnStore
      ? '当前账号被识别为<b>平台岗</b>，但本机选择的是<b>授权店</b>入口。请切换到平台岗入口并完成确认。'
      : '选的入口是<b>平台岗</b>，但当前账号不是平台岗 —— 换平台岗账号重新登录，或点底部「换进入方式」换个入口。';
    mm.hidden = !(setupState && setupState.entry_mismatch);
  }
  $('#btn-entry-switch').hidden = false;
  if (needConfirm) {
    $('#setup-lead').innerHTML =
      '这台电脑选的入口是<b>平台岗</b>，账号也认出来了 —— <b>最后确认一下</b>就能进。';
  } else if (needEntryMismatch) {
    $('#setup-lead').innerHTML =
      '当前账号和已选进入方式不匹配。为避免授权店和平台岗权限混用，请先切换进入方式。';
  }

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
    // 横幅点名的功能按版说 —— full 的「双平台对比 / POS 合规」生活馆根本没有
    el.textContent = st.lifehall
      ? `：${why} —— 界面能看，但**导入玲珑单 / 政策刷新这类要会话的功能`
        + `仍然进不来**（点了会照常报错，不会给假的数）。`
      : `：${why} —— 界面能看，但**抓取玲珑数据 / 双平台对比 / POS 合规`
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
  document.body.classList.toggle('lifehall-edition', !!(st && st.runtime_lifehall));
  syncLifehallSettings();
  renderStoreCodeForms();
  if (st && st.ready) { hideSetup(); return true; }
  // 生活馆入口还差门店编码 ⇒ 回选择页的编码那一步（别把人怼到 ERP 登录页）
  if (st && st.entry_kind === 'lifehall' && st.need === 'storecode') {
    hideSetup();
    showEntry({ code: true });
    return false;
  }
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
  if (setupState && setupState.entry_kind === 'lifehall') {
    // 生活馆不再有独立的“编码 + 玲珑登录”门。门店编码仍由首次入口收集，
    // 玲珑控件留在玲珑授权页；先刷新门禁状态，编码被清掉时回到编码页。
    try {
      const st = await api('/api/setup');
      if (st) setupState = st;
    } catch (e) {
      toast('读取进入状态失败：' + e.message, 'bad');
      return;
    }
    if (!setupState) { toast('读不到进入状态', 'bad'); return; }
    showSetup(setupState); // ready → 回控制台首页；缺编码 → 只显示编码表单。
    return;
  }
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
 *    真正的闸门在每个 `/api/* ─────────────── 状态悬浮窗（右下角常驻，任何页可开）───────────────

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

/* ═══════════ 增值两页的日期窗口（2026-09-29）═══════════
   形态 = **月份 select + 该月内截止日 select**（用户三选一里选的这个；**不跨月**），
   窗口 = 该月 1 号 ～ 所选那天；默认 = 今天（页面原行为一点不变）。
   用例：每月 1 号点「上月」→ 上月最后一天 = 看上月全月；月中把截止日选到 28 号。
⚠ 后端契约：`GET /api/film?end=YYYY-MM-DD` / `GET /api/benefit?end=…`；
   不给 = 今天，给了非法值后端回 400 + error（前端能显示原因）。
⚠ 导出 body 也带同一个 `end` —— 否则「页面看 8 月、导出 9 月」会静默对不上。 */
const _win = {};
const _pad2 = (n) => (n < 10 ? '0' : '') + n;
function _ymd(d) {
  return d.getFullYear() + '-' + _pad2(d.getMonth() + 1) + '-' + _pad2(d.getDate());
}
function _todayYm() {
  const t = new Date();
  return t.getFullYear() + '-' + _pad2(t.getMonth() + 1);
}
/** 当月往前 12 个月（含本月）—— 库是一年一个，够用了。 */
function _monthOpts() {
  const t = new Date(), out = [];
  for (let i = 0; i < 13; i++) {
    const d = new Date(t.getFullYear(), t.getMonth() - i, 1);
    out.push(d.getFullYear() + '-' + _pad2(d.getMonth() + 1));
  }
  return out;
}
function _daysIn(ym) {
  const p = String(ym).split('-');
  return new Date(Number(p[0]), Number(p[1]), 0).getDate();
}
/** 填「月份 + 截止日」两个 select（幂等：填过就跳过，`force` 才重建）。 */
function winInit(prefix, force) {
  const mSel = $('#' + prefix + '-month'), dSel = $('#' + prefix + '-day');
  if (!mSel || !dSel) return;
  if (!_win[prefix]) {
    _win[prefix] = { month: '', day: 0 };
    mSel.addEventListener('change', () => { winFillDays(prefix, 0); winReload(prefix); });
    dSel.addEventListener('change', () => {
      _win[prefix].day = Number(dSel.value);
      winReload(prefix);
    });
    $('#' + prefix + '-today')?.addEventListener('click', () => {
      _win[prefix] = { month: _todayYm(), day: new Date().getDate() };
      winInit(prefix, true);
      winReload(prefix);
    });
    $('#' + prefix + '-prev')?.addEventListener('click', () => {
      const t = new Date();
      const d = new Date(t.getFullYear(), t.getMonth() - 1, 1);
      const ym = d.getFullYear() + '-' + _pad2(d.getMonth() + 1);
      // 上月**最后一天** = 整月（进度封顶 100%、台量进度 = 整月目标）
      _win[prefix] = { month: ym, day: _daysIn(ym) };
      winInit(prefix, true);
      winReload(prefix);
    });
  }
  const st = _win[prefix];
  if (!force && mSel.options.length && st.month) return;
  const months = _monthOpts();
  mSel.innerHTML = months.map((m) => '<option value="' + m + '">'
    + m.slice(0, 4) + '年' + Number(m.slice(5)) + '月</option>').join('');
  mSel.value = months.indexOf(st.month) >= 0 ? st.month : _todayYm();
  winFillDays(prefix, st.day);
}
/** 按当前月份重填「几号」：`keepDay` 合法才沿用；过去月份默认**最后一天**、本月默认今天。 */
function winFillDays(prefix, keepDay) {
  const mSel = $('#' + prefix + '-month'), dSel = $('#' + prefix + '-day');
  if (!mSel || !dSel || !mSel.value) return;
  const ym = mSel.value, n = _daysIn(ym);
  const day = (keepDay >= 1 && keepDay <= n) ? keepDay
    : (ym === _todayYm() ? new Date().getDate() : n);
  let html = '';
  for (let i = 1; i <= n; i++) html += '<option value="' + i + '">' + i + ' 日</option>';
  dSel.innerHTML = html;
  dSel.value = String(day);
  _win[prefix] = { month: ym, day: day };
}
/** 当前窗口的截止日 `YYYY-MM-DD`（还没填好 = 今天）。 */
function winEnd(prefix) {
  const st = _win[prefix];
  if (!st || !st.month || !st.day) return _ymd(new Date());
  return st.month + '-' + _pad2(st.day);
}
function winReload(prefix) {
  if (prefix === 'film') loadFilm();
  else loadBenefit();
}

/* ── 明细下钻（增值两页 · 2026-09-29）──────────────────────────────
   点**门店行上每个能下钻的数字**（新机 / 贴膜达成 / 礼包达成 / 三份毛利 ·
   无忧 / Care+ / 合计 / 三份利润）→ 弹一张明细：销售单号 / 单据类型 / 商品名称 /
   数量 / 销售时间 / **毛利**，**含退货**（源里是负数，合计已冲减）。

   ⚠ 合口径与页面同一处：后端 `film/benefit.compute.row_kind()` 是「这行算哪个
     指标」的唯一判据，`drill_rows()` 查出来的合计必须等于那格数字
     （只有防护膜新机 ×0.9，其余不乘）。前端只负责**显示后端算好的合计**，
     自己不重算口径 —— 两头各写一份迟早走散（AGENTS 坑 12 同类）。
   ⚠ 只有**门店行**可点：区域/赛道/合计行不是「某家店」，没有单可列。 */
function drillCell(page, kind, store, txt) {
  return '<td class="num drill" data-drill="' + page + '" data-kind="' + kind + '"'
    + ' data-store="' + esc(encodeURIComponent(store || '')) + '"'
    + ' title="点一下看纳入统计的销售单（含退货）">' + esc(txt) + '</td>';
}

/** 明细里的数字：整数直接出、小数留 2 位（退货 −1 这类照原样显示负数）。 */
function drillQty(v) {
  const n = Number(v) || 0;
  return String(Math.abs(n - Math.round(n)) < 1e-9 ? Math.round(n) : n.toFixed(2));
}

function closeDrill() {
  const mask = $('#drill-mask');
  if (mask) mask.hidden = true;
}

async function openDrill(page, kind, store, end) {
  const mask = $('#drill-mask');
  if (!mask || !store || !kind) return;
  const title = $('#drill-title'), sub = $('#drill-sub'), body = $('#drill-body'),
    legend = $('#drill-legend'), sum = $('#drill-sum');
  if (title) title.textContent = '销售明细';
  if (sub) sub.textContent = store;
  if (legend) legend.textContent = '';
  if (sum) sum.textContent = '';
  if (body) {
    body.innerHTML = '<div class="film-loading"><span class="film-spin"></span>'
      + '正在从本地订单库读…</div>';
  }
  mask.hidden = false;
  try {
    const d = await api('/api/' + page + '/drill?store=' + encodeURIComponent(store)
      + '&kind=' + encodeURIComponent(kind)
      + (end ? '&end=' + encodeURIComponent(end) : ''));
    renderDrill(d, store);
  } catch (e) {
    // ⚠ 失败要留在弹窗里说清（403 的文案后端已带「没有权限：…」）
    if (body) body.innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
  }
}

function renderDrill(d, store) {
  const title = $('#drill-title'), sub = $('#drill-sub'), body = $('#drill-body'),
    legend = $('#drill-legend'), sum = $('#drill-sum');
  if (!body) return;
  if (!d || d.ok === false) {
    body.innerHTML = '<div class="empty">'
      + esc((d && (d.why || d.error)) || '读不出来') + '</div>';
    return;
  }
  const rows = d.rows || [];
  if (title) title.textContent = (d.label || '销售') + ' 明细';
  if (sub) {
    sub.textContent = (d.store || store || '')
      + ' · ' + (d.start || '') + ' ~ ' + (d.end || '');
  }
  let h = rows.length ? '' : '<div class="empty">这段时间没有纳入统计的销售单</div>';
  h += '<div class="table-scroll"><table class="film-table"><thead><tr>'
    + '<th>销售单号</th><th>单据类型</th><th>商品名称</th>'
    + '<th class="num">数量</th><th>销售时间</th><th class="num">毛利</th>'
    + '</tr></thead><tbody>';
  rows.forEach((r) => {
    const neg = Number(r.qty) < 0, np = Number(r.profit) < 0;
    h += '<tr>'
      + '<td>' + esc(r.no || '（无单号）') + '</td>'
      + '<td>' + esc(r.typ || '') + '</td>'
      // 名字长（含机型/颜色）—— 定死列宽后会省略，`title` 留着看全文
      + '<td title="' + esc(r.name || '') + '">' + esc(r.name || '') + '</td>'
      + '<td class="num' + (neg ? ' drill-neg' : '') + '">'
      + esc(drillQty(r.qty)) + '</td>'
      + '<td>' + esc(r.ts || '') + '</td>'
      + '<td class="num' + (np ? ' drill-neg' : '') + '">'
      + esc(drillQty(r.profit)) + '</td>'
      + '</tr>';
  });
  h += '</tbody></table></div>';
  body.innerHTML = h;
  if (legend) legend.textContent = d.note || '';
  // 底部合计：台/件 → 那个指标的件数（新机还有折算）；元 → 毛利合计
  const unit = d.unit || '台', field = d.field || 'qty',
    factor = Number(d.factor) || 1, label = d.label || '';
  let s = '共 ' + rows.length + ' 行 · ';
  if (field === 'qty') {
    s += '合计 ' + drillQty(d.total) + ' ' + unit;
    if (Math.abs(factor - 1) > 1e-9) {
      s += ' × ' + factor + ' = ' + drillQty(d.shown)
        + '（页面上的「' + label + '」）';
    } else {
      s += '（就是页面上的「' + label + '」）';
    }
  } else {
    s += '毛利合计 ' + drillQty(d.shown) + ' 元（就是页面上的「' + label + '」）';
  }
  if (sum) sum.textContent = s;
}

// 门店行上可下钻的数字可点 —— document 级委托（表每次 innerHTML 重画，同 film 门店名）
document.addEventListener('click', (e) => {
  const td = e.target && e.target.closest && e.target.closest('[data-drill]');
  if (!td) return;
  const page = td.getAttribute('data-drill') || '';
  const kind = td.getAttribute('data-kind') || '';
  const store = decodeURIComponent(td.getAttribute('data-store') || '');
  if (!store || !kind || (page !== 'film' && page !== 'benefit')) return;
  openDrill(page, kind, store, winEnd(page));
});

function bindDrillModal() {
  $('#drill-close')?.addEventListener('click', closeDrill);
  $('#drill-mask')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeDrill();
  });
}
bindDrillModal();          // 静态元素（弹窗骨架在 index.html），绑一次

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
    if (res.scope_restricted) {
      toast(res.skipped ? '收信已跳过'
        : (res.ok === false ? '收信失败，请稍后重试' : '收信完成，请查看授权范围内门店卡片'),
        res.ok === false ? 'bad' : 'ok');
    } else {
      toast(res.skipped ? ('跳过：' + res.skipped)
        : `收到 ${res.ok_packages || 0} 个包（${res.rows || 0} 行）`,
        res.ok === false ? 'bad' : 'ok');
    }
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
  const isLifehall = !!(setupState && setupState.runtime_lifehall);
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
    applyProfile(null);
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
  renderSession();
  // ⚠ 这里曾写 `renderSchedule(sch)` 而 `sch` 从未定义 —— loadOverview 每次收尾
  //   抛 ReferenceError（控制台实测）。schedule 在 overview 里，跟
  //   `loadSchedulerBits` 同一份（`o.schedule || {}`）。
  renderSchedule(o.schedule || {});
}


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
  if (el && !job.details_restricted && job.lines && job.lines.length) {
    if (el.textContent === '（还没有运行记录）') el.textContent = '';
    el.textContent += job.lines.join('\n') + '\n';
    el.scrollTop = el.scrollHeight;
    el.dataset.restrictedJob = '';
    return;
  }
  if (job.details_restricted && el && el.dataset.restrictedJob !== job.id) {
    el.textContent = '详细运行日志仅平台岗可查看；任务运行状态仍会显示。';
    el.dataset.restrictedJob = job.id || 'restricted';
  }
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

/** 设置 · **强制刷新**（2026-09-29 用户：「加个强制刷新按钮吧，
 *  按照新规则全部重写数据库」）—— 按当前口径重抓本年度销售明细、**整表重写**。
 *
 *  ⚠ 危险动作 ⇒ 二次确认（文案里说清"什么时候需要它 / 会不会丢数据"）；
 *  ⚠ 起后台任务 + 打开日志抽屉（跟「刷新」同一条 runner 路，同一把锁）；
 *  ⚠ 后端**不过 30 分钟冷却**（"强制"就是这个意思），但已经在跑会回 409。
 */
$('#btn-sales-rewrite')?.addEventListener('click', async () => {
  const btn = $('#btn-sales-rewrite');
  const msg = $('#rewrite-msg');
  const ok = confirm('按当前口径重抓本年度销售明细、整表重写数据库？\n\n'
    + '· 历史月份按现在的规则重新落库（比如 9-22 起才入库的贴膜/礼包）\n'
    + '· 要几分钟，进度在右下角「运行日志」抽屉里\n'
    + '· 抓失败或抓到 0 行，旧数据一行不动');
  if (!ok) return;
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/sales-rewrite', { method: 'POST', body: {} });
    toast(r.message || '开始重写…');
    if (msg) msg.textContent = r.message || '';
    setRunDrawer(true);
    await watchJob((r.job || {}).id || '');
    if (msg) msg.textContent += '（跑完了，看上面日志的退出码）';
  } catch (e) {
    // ⚠ 409 = 已经有一趟在跑 —— 说人话，别弹红的吓人
    //   （后端文案是「已经有一趟在跑了」，别去匹配「已经在跑」—— 匹不上就一直是红的）
    const busy = e.message.indexOf('有一趟') >= 0 || e.message.indexOf('已经在跑') >= 0;
    toast(e.message, busy ? '' : 'bad');
    if (msg) msg.textContent = e.message;
  } finally {
    if (btn) btn.disabled = false;
  }
});

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
  // 生活馆入口只用门店编码，不读取完整 ERP 版的华为自动登录配置。
  if (setupState && setupState.runtime_lifehall) return;
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
  if (!(setupState && setupState.runtime_lifehall)) return '';
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
  const lifehall = !!(setupState && setupState.runtime_lifehall);
  const markup = lifehall ? storeCodeBox() : '';
  const settingsHost = $('#store-code-settings');
  const settingsCard = $('#store-code-settings-card');
  if (settingsHost) settingsHost.innerHTML = markup;
  if (settingsCard) settingsCard.hidden = !lifehall;
  bindStoreCodeBtn();
}

function syncLifehallSettings() {
  const lifehall = !!(setupState && setupState.runtime_lifehall);
  const label = document.querySelector('[data-foot="general"] .foot-label');
  const lifehallCard = $('#lifehall-entry-settings-card');
  const lifehallCode = $('#lifehall-current-code');
  const service = $('#service-settings-card');
  const fullNote = $('#general-settings-full-note');
  if (label) label.textContent = lifehall ? '设置' : '推送设置';
  if (lifehallCard) lifehallCard.hidden = !lifehall;
  if (lifehallCode) {
    const code = setupState && setupState.profile && setupState.profile.huawei_code;
    lifehallCode.textContent = code || '尚未设置';
  }
  if (service) service.hidden = lifehall;
  for (const selector of ['#notify-pref-card', '#btn-sales-rewrite', '#mail-paths', '#wecom-paths']) {
    const card = document.querySelector(selector)?.closest('.card');
    if (card) card.hidden = lifehall;
  }
  if (fullNote) fullNote.hidden = lifehall;
}

$('#btn-lifehall-edit-code')?.addEventListener('click', () => {
  showEntry({ code: true });
  const input = $('#entry-store-code');
  if (input) { input.focus(); input.select(); }
});

$('#btn-lifehall-open-ll')?.addEventListener('click', () => {
  switchTab('settings', 'linglong');
});

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
    st.innerHTML = a.ownership_unverified
      ? `<span style="color:var(--warn)">${a.installed ? '已开启，但' : '发现'}同名启动项归属未确认</span>`
      : (a.installed
          ? '<span style="color:var(--ok)">✅ 已开启</span>'
          : '<span style="color:var(--warn)">未开启 —— 开机能自己跑起来才好天天不用管</span>');
  }
  if (addBtn) addBtn.hidden = !!a.installed || !!a.ownership_unverified;
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
    (a.ownership_message ? `<br><span style="color:var(--warn)">${esc(a.ownership_message)}</span>` : '') +
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
  const lifehall = !!(setupState && setupState.runtime_lifehall);
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
  if (!lifehall) { loadMail(); loadWecom(); }
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

// ⚠ 首屏先加载 `overview.role.pages` 并应用页面权限，再打开第一个可见页。
//   否则生活馆会先切到周度达成、触发被禁止的接口请求，之后才隐藏该页。
(async () => {
  // ⭐ **选择页比门禁还靠前**（2026-10-02 进入流程）：新装机器先选入口；
  //   `needed` 的判据在后端（没选过 + 没就绪），老机器/生活馆版都是 false。
  const ent = await entryState();
  if (ent && ent.needed) {
    showEntry();
    return;                              // 选完由 chooseEntry 接着走
  }
  // ⚠ **门禁在最前面**：没登录好就停在登录页，别的数据一个都不拉。
  //   拉到一半的 403 会顺带把登录页顶出来（`api()` 里那段），但那样
  //   控制台会先闪一下空页面 —— 先去问一次就没这问题。
  const ready = await checkSetup();
  // 两步都要用的：浏览器检测 + 华为账号（登录页那一步要显示）
  loadBrowserInfo();
  loadHwLogin();
  if (!ready) return;                    // 停在登录页

  // overview 下发 role.pages 并应用导航权限后，才打开首个可见页面。
  // 固定先切 sales 会在生活馆版过早请求 /api/attain，随后才被隐藏。
  await loadOverview();
  openFirstAvailablePage();
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
