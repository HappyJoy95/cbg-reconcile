'use strict';

/* 收银录入页：数据与交互属于 cashier 功能；离页时取消尚未完成的读取。 */
let cashierPageMounted = false;
let cashierPageEpoch = 0;
const cashierReadControllers = new Set();

async function cashierRead(path) {
  if (!cashierPageMounted) return null;
  const epoch = cashierPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  if (controller) cashierReadControllers.add(controller);
  try {
    const result = await api(path, controller ? { signal: controller.signal } : {});
    return cashierPageMounted && epoch === cashierPageEpoch ? result : null;
  } catch (e) {
    if (!cashierPageMounted || epoch !== cashierPageEpoch || (e && e.name === 'AbortError')) {
      return null;
    }
    throw e;
  } finally {
    if (controller) cashierReadControllers.delete(controller);
  }
}

/* ─────────────── 收银（生活馆利润核算录入端，2026-09-29）───────────────

   用户拍板（同日）：目前**没有生活馆的玲珑数据** ⇒ 先做单独录入；
   「拉玲珑 + 过滤备注1/2/3 + 改金额/销售员」等有数据再接（开发目标·七）。
   ⚠ 编码是主输入：识别编码 → `price_policy` 反查商品名（成本/返利顺带显示）；
     金额、销售员**手动匹配**（没有现成名单，销售员下拉靠历史积累）。
   ⚠ 政策刷新走 pmall A 案（活窗 → jar → 弹窗人工登录），接口会**阻塞到登录
     完成（最长 10 分钟）** —— 按钮必须禁用并说清，别让人以为卡死了狂点。 */

let _cashierEditing = 0;      // 正在改的流水 id（**number**，0 = 没在改）
let _cashierEditAcc = [];     // 编辑中的配件草稿（深拷贝自原行，保存才提交）
let _cashierEditPay = [];     // 编辑中的支付草稿（同上 —— 取消修改要能还原）
let _cashierEditProd = [];    // 编辑中的商品行草稿（一张卡 = 一个订单）

//: 品类清单（= 后端 `store.CATEGORIES`，导出销售表的「品类」列就用它）
const cashierCATEGORIES = ['手机', '平板', '笔记本', '穿戴', '音频',
  '配件', '第三方配件', '服务'];
let _cashierAutoName = '';    // 最近一次反查自动带出的名 —— 用户手改过就不再覆盖
let _cashierBound = false;    // 事件只绑一次（loadCashier 每次进来都调）
let _cashierRows = {};        // id → 行（卡片按钮按 dataset.id 查行用）

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
  for (const id of ['cashier-scan', 'cashier-sn', 'cashier-amount',
                    'cashier-note', 'cashier-category']) {
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

/* ⚠ 原来这里有个把行回填进**顶部表单**的老函数（cashierFill）——
   2026-09-30 卡内编辑（Task 10）拆掉了：编辑在卡里做，顶部表单只管新录。
   留着它必然出现"两处都能改、状态互相打架"，所以整个函数删干净
   （`tests/test_cashier.py::test_卡内编辑接上了` 断言那个调用串不在）。 */

async function cashierLookup(focusAmount) {
  const codeEl = $('#cashier-scan');
  const code = (codeEl.value || '').trim();
  if (!code) return;
  try {
    const d = await cashierRead('/api/cashier/lookup?code=' + encodeURIComponent(code));
    if (!d || !cashierPageMounted) return;
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
  // ⚠ 顶部表单**只管新录** —— 改已有那笔在卡里（cashierCardSave），别再往这儿塞 id
  // ⚠ 两段式（2026-09-30）：这里**不入账** —— status=staged 进今日清单，
  //   真正入库是汇总条那个「保存并记录」（cashierCommit）。
  const body = {
    sold_at: $('#cashier-sold-at').value,
    goods_code: ($('#cashier-scan').value || '').trim(),
    goods_name: ($('#cashier-name').value || '').trim(),
    sn: ($('#cashier-sn').value || '').trim(),
    category: ($('#cashier-category') || {}).value || '',
    quantity: $('#cashier-qty').value,
    amount: $('#cashier-amount').value,
    seller: ($('#cashier-seller').value || '').trim(),
    note: ($('#cashier-note').value || '').trim(),
    status: 'staged',
  };
  if (!String(body.amount).trim()) {
    toast('应收金额还没填（这里是这单的商品合计）', 'bad');
    $('#cashier-amount').focus();
    return;
  }
  try {
    await api('/api/cashier/entry-save', { method: 'POST', body });
    toast('已加入今日清单（还没入库）—— 结账时点「保存并记录」', 'good');
    cashierResetForm();
    await loadCashier();
  } catch (e) {
    toast('添加失败：' + e.message, 'bad');
  }
}

async function cashierRemove(id) {
  if (!window.confirm('删除这笔流水？删了不能恢复。')) return;
  try {
    await api('/api/cashier/entry-delete', { method: 'POST', body: { id } });
    toast('已删除', 'good');
    // ⚠ 只清**这张卡的编辑态**，别碰顶部表单（用户：删卡不许把上面
    //   填到一半的新单冲掉 —— 那是两码事）。别卡的编辑由 loadCashier 保。
    if (_cashierEditing === Number(id)) {
      _cashierEditing = 0;
      _cashierEditAcc = [];
      _cashierEditPay = [];
      _cashierEditProd = [];
    }
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
    ? `<div class="pay-warn">实收 ¥${sum.toFixed(2)} ≠ 应收 ¥${amt.toFixed(2)}</div>` : '';
  return '<div class="cc-row">'
    + pays.map((p) => `<span>${esc2(p.method)} ¥${(Number(p.amount) || 0).toFixed(2)}</span>`)
      .join('、')
    + '</div>' + warn;
}

function cashierCardHtml(r) {
  const acc = (r.accessories || []);
  const prods = (r.products || []);
  const sold = String(r.sold_at || '').slice(5, 16);
  const src = r.source === 'linglong'
    ? '<span class="cc-wait" title="修改只影响本机视图，不动华为原单">玲珑 '
      + esc2(r.external_id || '') + '</span>' : '';
  // 两段式：没点「保存并记录」的行挂个徽章（一眼看出哪些还没入账）
  const wait = r.status === 'staged'
    ? '<span class="cc-wait" title="还没入账 —— 结账时点汇总条的「保存并记录」">未入库</span>'
    : '';
  // 商品行（一张卡 = 一个订单）：品类**跟商品走**，有商品行就逐行列；
  // 没有（单商品老口径）才把卡片品类放 meta —— 卡片右侧不再放品类（第三轮）
  const catMeta = (!prods.length && r.category)
    ? `<br>品类 ${esc2(r.category)}` : '';
  const prodHtml = prods.length
    ? '<div class="cc-acc"><b>商品</b>'
      + prods.map((p) => `<div class="cc-acc-row">`
        + `<span>${p.category ? `[${esc2(p.category)}] ` : ''}${esc2(p.name)}`
        + `${p.code ? ` <span class="cc-muted">${esc2(p.code)}</span>` : ''}</span>`
        + `<span>×${esc2(Math.round((Number(p.quantity) || 0) * 100) / 100)}`
        + ` ¥${(Number(p.amount) || 0).toFixed(2)}</span></div>`).join('')
      + '</div>'
    : '';
  const accHtml = acc.length
    ? '<div class="cc-acc"><b>配件</b>'
      + acc.map((x) => `<div class="cc-acc-row"><span>${esc2(x.name)} `
        + `<span class="cc-muted">×${esc2(Math.round((Number(x.quantity) || 1) * 100) / 100)}</span></span>`
        + `<span>¥${(Number(x.amount) || 0).toFixed(2)}</span></div>`).join('')
      + '</div>'
    : '';
  // 左上角钱区（用户第 3 条）：应收 = Σ商品；实收 = Σ支付渠道之和
  const paid = (r.payments || [])
    .reduce((a, x) => a + (Number(x.amount) || 0), 0);
  return `<div class="cashier-card card" data-id="${r.id}">`
    + `<div class="cc-main">`
    + `<div class="cc-time">${esc2(sold)} ${wait} ${src}</div>`
    + `<div class="cc-money"><span class="cm-tag">应收</span>`
    + `<span class="cc-amount">¥${(Number(r.amount) || 0).toFixed(2)}</span>`
    + `<span class="cm-paid">实收 <b>¥${paid.toFixed(2)}</b></span>`
    + `<span class="cc-qty">数量 `
    + `${esc2(Math.round((Number(r.quantity) || 0) * 100) / 100)}</span></div>`
    + `<div class="cc-name">${esc2(r.goods_name) || '（没填名称）'}</div>`
    + `<div class="cc-meta">商品编码 ${esc2(r.goods_code) || '—'}`
    + `<br>SN ${esc2(r.sn) || '—'}${catMeta}</div>`
    + prodHtml
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

/* ───────────── 卡内编辑态（Task 10，支付块 Task 11 接上）─────────────

   ⚠ `_cashierEditing` 必须是 **number**：`data-cc-edit` 从 dataset 拿到的是
     string，直接赋进去的话 `_cashierEditing === r.id`（JSON number）**永远 false**
     ⇒ `.cc-editing` 永远不加 ⇒ `cashierEditState()` 查不到卡返回 null
     ⇒ 保存静默 no-op。所以入口 `cashierCardEdit(id)` 里 `Number(id)` 归一。
   ⚠ 支付块渲染自 `_cashierEditPay` 草稿（进编辑时深拷贝原行），增删先
     `cashierPayDraftFromDom()` 把 DOM 里没同步的字收回来，取消能还原。 */
function cashierCardEditHtml(r) {
  const acc = _cashierEditAcc;
  const inp = (f, v, type, w) => `<input data-f="${f}" type="${type || 'text'}"`
    + ` value="${esc2(v == null ? '' : v)}"${w ? ` style="width:${w}"` : ''}>`;
  // 商品行（一张卡 = 一个订单）：名/编码/SN/数量/金额/品类 六件套。
  // ⚠ 品类在**行上**（用户：品类跟单条商品走）—— 卡片右侧那个品类没了。
  const prodRow = (p, i) => `<div class="cc-prod" data-prod-i="${i}">`
    + `<input data-prod-f="name" value="${esc2(p.name)}" placeholder="商品名">`
    + `<input data-prod-f="code" value="${esc2(p.code)}" placeholder="编码" style="width:7em">`
    + `<input data-prod-f="sn" value="${esc2(p.sn)}" placeholder="SN" style="width:8em">`
    + `<input data-prod-f="quantity" type="number" step="any" min="0.01"`
    + ` value="${esc2(p.quantity == null ? '' : p.quantity)}" style="width:4.5em"`
    + ` title="数量">`
    + `<input data-prod-f="amount" type="number" step="0.01"`
    + ` value="${p.amount == null ? '' : esc2(p.amount)}" style="width:6em"`
    + ` title="金额">`
    + `<select data-prod-f="category" title="品类（跟这条商品走）">`
    + `<option value=""></option>`
    + cashierCATEGORIES.map((o) => `<option${o === p.category ? ' selected' : ''}>`
      + `${esc2(o)}</option>`).join('')
    + `</select>`
    + `<button class="btn ghost small" data-prod-del="${i}">删</button>`
    + `</div>`;
  return `<div class="cashier-card card cc-editing" data-id="${r.id}">`
    + `<div class="cc-main">`
    + `<div class="cc-time">编辑中</div>`
    // 左上角钱区（用户第 1+3 条）：名/编码/SN/数量那排输入框撤了 ——
    // 商品行里改，服务端照行派生顶层字段；「实收」挪到应收旁边 = Σ支付渠道
    + `<div class="cc-money">`
    + `<span class="cm-tag">应收</span>`
    + `<input data-f="amount" type="number" step="0.01" readonly`
    + ` value="${esc2(r.amount == null ? '' : r.amount)}"`
    + ` title="合计 = Σ商品金额（自动）">`
    + `<span class="cm-paid">实收 <b data-paid>¥0.00</b></span></div>`
    + `<div class="cc-acc" data-prod-blocks><b>商品</b>`
    + _cashierEditProd.map(prodRow).join('')
    + `<button class="btn ghost small" data-prod-add="1">+ 添加商品</button></div>`
    + `<div class="cc-acc"><b>配件区</b>`
    // ⚠ 配件行跟商品行**同一套栅格**（用户第 2 条）：配件名对齐商品名、
    //   金额对齐商品金额那一列 —— 所以类是 cc-prod + cc-acc-edit（列定位靠 CSS）
    + acc.map((x, i) => `<div class="cc-prod cc-acc-edit" data-acc-i="${i}">`
        + `<input class="ac-name" data-acc-f="name" value="${esc2(x.name)}"`
        + ` placeholder="配件名">`
        + `<input class="ac-qty" data-acc-f="quantity" type="number" step="any"`
        + ` min="0.01" value="${esc2(x.quantity == null ? '' : x.quantity)}"`
        + ` placeholder="数量">`
        + `<input class="ac-amt" data-acc-f="amount" type="number" step="0.01"`
        + ` value="${esc2(x.amount)}" placeholder="金额">`
        + `<button class="btn ghost small ac-x" data-acc-del="${i}">删</button>`
        + `</div>`).join('')
    + `<button class="btn ghost small" data-acc-add="1">+ 添加配件</button></div>`
    + `<div class="cc-actions">`
    + `<button class="btn primary small" data-cc-save="${r.id}">保存修改</button>`
    + `<button class="btn ghost small" data-cc-cancel="1">取消</button>`
    + `</div></div>`
    + `<div class="cc-side">`
    + `<div class="cc-group"><div class="cc-group-title">销售信息</div>`
    + `<div class="cc-row"><span class="k">销售员</span>${inp('seller', r.seller)}</div>`
    + `<div class="cc-row"><span class="k">备注</span>${inp('note', r.note)}</div>`
    + `<div class="cc-row"><span class="k">销售时间</span>`
    + `${inp('sold_at', String(r.sold_at || '').slice(0, 16).replace(' ', 'T'), 'datetime-local')}</div>`
    + `</div>`
    + `<div class="cc-group cc-muted"><div class="cc-group-title">客户信息</div>`
    + `会员/手机号<span class="cc-wait">待接入</span></div>`
    + `<div class="cc-group"><div class="cc-group-title">支付方式</div>`
    + `<div class="pay-blocks" data-pay-blocks>`
    + _cashierEditPay.map((p, i) => cashierPayEditBlock(p, i)).join('')
    + `</div>`
    + `<button class="btn ghost small" data-pay-add="1">+ 添加支付渠道</button>`
    + `<div class="pay-sum" data-pay-sum></div></div>`
    + `</div>`
    + `<button class="cc-close" data-cc-close="${r.id}" title="删除这笔">✕</button>`
    + `</div>`;
}

/** 从编辑态的卡里读出要提交的整条 —— 找不到卡就返回 null（调用方直接不发）。 */
function cashierEditState() {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card) return null;
  const r = _cashierRows[card.dataset.id] || {};
  const out = Object.assign(
    { id: r.id, source: r.source, external_id: r.external_id },
    cashierEditFields() || {});
  // ⚠ 配件**以输入框为准**：`_cashierEditAcc` 只在增删时改，人手打的字还只在 DOM 里。
  //   直接 `out.accessories = _cashierEditAcc` 会把刚敲的配件名静默丢掉。
  const accs = [];
  card.querySelectorAll('[data-acc-i]').forEach((row) => {
    const name = ((row.querySelector('[data-acc-f="name"]') || {}).value || '').trim();
    const amount = Number((row.querySelector('[data-acc-f="amount"]') || {}).value) || 0;
    const quantity = ((row.querySelector('[data-acc-f="quantity"]') || {}).value || '') || 1;
    accs.push({ name, amount, quantity });
  });
  _cashierEditAcc = accs;
  out.accessories = _cashierEditAcc;
  // ⚠ 支付**以输入框为准**（编辑态一定渲染了块）：没块时才回原行兜底 ——
  //   写死 `cashierPayRead(card)` 在没有块的中间态会读出 [] 把原数据抹了
  out.payments = card.querySelector('[data-pay-blocks]')
    ? cashierPayRead(card) : (r.payments || []);
  // ⚠ 商品行同样以输入框为准；**整行全空的丢掉**（手滑多点了一下「添加」，
  //   拿去后端会撞「第 N 条没有名字」白报一轮错）
  out.products = card.querySelector('[data-prod-blocks]')
    ? cashierProdRead(card).filter((p) => !cashierProdBlank(p))
    : (r.products || []);
  return out;
}

/** 编辑卡里 `[data-f]` 的当前值（→ 对象）。没有编辑卡返回 null。 */
function cashierEditFields() {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card) return null;
  const out = {};
  card.querySelectorAll('[data-f]').forEach((el) => { out[el.dataset.f] = el.value; });
  return out;
}

/** 重画编辑卡（配件增删后用）—— **把还没提交的输入原样写回去**，
 *  否则点一下「+ 添加配件」就把人刚敲的名称/金额整卡刷没了。 */
function cashierRerenderEdit() {
  const fields = cashierEditFields();
  renderCashierCards(Object.values(_cashierRows));
  if (!fields) return;
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card) return;
  card.querySelectorAll('[data-f]').forEach((el) => {
    if (fields[el.dataset.f] !== undefined) el.value = fields[el.dataset.f];
  });
  // 刷完要把派生值补上：商品行变了 → 实收/数量重算；支付合计/黄条同理
  cashierProdSync(card);
  cashierPaySync(card);
}

/* ───────────── 商品行（一张卡 = 一个订单，2026-09-30 第三轮）─────────────

   ⚠ 品类在**每一行商品**上（用户：「品类要跟单条商品走」）——
     卡片右侧不再有品类选择器；顶部录入口那个品类 = 新卡首商品的品类。
   ⚠ 有商品行时「数量 / 实收」是派生值（后端按 Σ 覆盖），界面只读 +
     `cashierProdSync` 实时刷 —— 避免"改了又自己弹回去"的错觉。 */
function cashierProdBlank(p) {
  return !String(p.name || '').trim() && !String(p.code || '').trim()
    && !String(p.sn || '').trim() && !String(p.category || '').trim()
    && !(Number(p.amount) > 0);
}

/** 读编辑卡里的商品行（原样，含空行 —— 空行过滤在 cashierEditState 做）。 */
function cashierProdRead(card) {
  const out = [];
  card.querySelectorAll('[data-prod-i]').forEach((row) => {
    const v = (f) => ((row.querySelector(`[data-prod-f="${f}"]`) || {}).value);
    out.push({
      name: String(v('name') || '').trim(),
      code: String(v('code') || '').trim(),
      sn: String(v('sn') || '').trim(),
      category: String(v('category') || '').trim(),
      quantity: String(v('quantity') || '').trim() || 1,
      amount: String(v('amount') || '').trim() || 0,
    });
  });
  return out;
}

/** 商品行的值收进草稿（增删前调 —— 人手打的字还在 DOM 里）。 */
function cashierProdDraftFromDom() {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card || !card.querySelector('[data-prod-blocks]')) return;
  _cashierEditProd = cashierProdRead(card);
}

function cashierProdAdd() {
  cashierProdDraftFromDom();
  _cashierEditProd.push({ name: '', code: '', sn: '', quantity: 1,
                          amount: '', category: '' });
  cashierRerenderEdit();
}

function cashierProdDel(i) {
  cashierProdDraftFromDom();
  _cashierEditProd.splice(Number(i), 1);
  cashierRerenderEdit();
}

//: 派生值：**应收 = Σ(数量 × 金额)** —— 商品/配件的金额框都是单价（用户：
//: 「不是数量*金额之和吗，2台的时候不动啊」）。件数仍只算商品数量。
function cashierProdSync(card) {
  if (!card) return;
  const rows = card.querySelectorAll('[data-prod-i]');
  if (!rows.length) {
    // 商品行被删光 ⇒ 派生值清空（留着旧 Σ 会"没商品却有合计"，保存时后端
    // 走老口径拿空金额报「金额得是数字」，比静默存一笔空单强）
    const a0 = card.querySelector('[data-f="amount"]');
    if (a0) a0.value = '';
    return;
  }
  const qtyOf = (q) => (q && String(q.value).trim() !== '') ? (Number(q.value) || 0) : 1;
  let sum = 0;
  rows.forEach((row) => {
    const a = row.querySelector('[data-prod-f="amount"]');
    const q = row.querySelector('[data-prod-f="quantity"]');
    sum += qtyOf(q) * (Number(a ? a.value : 0) || 0);
  });
  card.querySelectorAll('[data-acc-i]').forEach((row) => {
    const a = row.querySelector('[data-acc-f="amount"]');
    const q = row.querySelector('[data-acc-f="quantity"]');
    sum += qtyOf(q) * (Number(a ? a.value : 0) || 0);
  });
  const aEl = card.querySelector('[data-f="amount"]');
  if (aEl) aEl.value = (Math.round(sum * 100) / 100).toFixed(2);
}

/** 读一组支付块 → `{method, amount}[]`。空金额的块不进（保存时不提交半截）。 */
function cashierPayRead(card) {
  const out = [];
  card.querySelectorAll('[data-pay-block]').forEach((b) => {
    const method = (b.querySelector('[data-pay-method]') || {}).value || '';
    const amount = Number((b.querySelector('[data-pay-amount]') || {}).value);
    if (method && amount) out.push({ method, amount });
  });
  return out;
}

/* ───────────── 组合支付方块（Task 11）─────────────

   ⚠ 支付方式**存原始字符串不是枚举**：清单以后增删名字不坏老数据 ——
     老行里的名字照原样显示，不在清单里的也能显示。
   ⚠ 实收（Σ支付渠道）合计 ≠ 应收只弹黄条**不拦截保存**（用户定的软提醒）。 */
const cashierPAY_METHODS = ['助手', 'C扫B', 'POS', '现金', '公对公',
  '国补实付', '国补优惠', '预收款', '企业微信', '支付宝直连', '微信直连'];

function cashierPayEditBlock(p, i) {
  const opts = cashierPAY_METHODS.map((m) => `<option value="${esc2(m)}"`
    + `${m === p.method ? ' selected' : ''}>${esc2(m)}</option>`).join('');
  return `<div class="pay-block" data-pay-block="${i}">`
    + `<button class="pb-x" data-pay-del="${i}" title="去掉这种方式">✕</button>`
    + `<div class="pb-name"><select data-pay-method>${opts}</select></div>`
    + `<input data-pay-amount type="number" step="0.01" placeholder="金额"`
    + ` value="${p.amount != null ? esc2(p.amount) : ''}">`
    + `</div>`;
}

/** 把编辑卡里还没同步的支付块收进草稿（增删前调，别把空金额的块丢了）。 */
function cashierPayDraftFromDom() {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card || !card.querySelector('[data-pay-blocks]')) return;
  const out = [];
  card.querySelectorAll('[data-pay-block]').forEach((b) => {
    const method = (b.querySelector('[data-pay-method]') || {}).value || '';
    const raw = (b.querySelector('[data-pay-amount]') || {}).value;
    if (method) out.push({ method, amount: raw === '' ? '' : Number(raw) });
  });
  _cashierEditPay = out;
}

function cashierPayAdd() {
  cashierPayDraftFromDom();
  _cashierEditPay.push({ method: cashierPAY_METHODS[0], amount: '' });
  cashierRerenderEdit();
}

function cashierPayDel(i) {
  cashierPayDraftFromDom();
  _cashierEditPay.splice(Number(i), 1);
  cashierRerenderEdit();
}

//: 软提醒：已付合计 ≠ 应收 ⇒ 黄条；**不拦截保存**（用户定）。
//: 实收（= Σ支付渠道，用户第 3 条）就显示在左上角那个 [data-paid] 上。
function cashierPaySync(card) {
  const r = _cashierRows[card.dataset.id] || {};
  const pays = cashierPayRead(card);
  const sum = pays.reduce((a, x) => a + (Number(x.amount) || 0), 0);
  const amtEl = card.querySelector('[data-f="amount"]');
  const amt = Number(amtEl ? amtEl.value : r.amount) || 0;
  const paid = card.querySelector('[data-paid]');
  if (paid) paid.textContent = `¥${sum.toFixed(2)}`;
  const el = card.querySelector('[data-pay-sum]');
  if (!el) return;
  const warn = (pays.length && Math.abs(sum - amt) > 0.009)
    ? `<div class="pay-warn">实收 ¥${sum.toFixed(2)} ≠ 应收 ¥${amt.toFixed(2)}（仍可保存）</div>`
    : '';
  el.innerHTML = `实收 ¥${sum.toFixed(2)} / 应收 ¥${amt.toFixed(2)}` + warn;
}

/** 卡片 ✕：**按来源分叉** —— 玲珑单软排除（原单不动、再导不回来），手工单真删。 */
async function cashierCardClose(id) {
  const r = _cashierRows[id];
  if (!r) return;
  if (r.source === 'linglong') {
    if (!window.confirm('从当日视图排除这张玲珑单？\n（不会删华为那边的原单，'
      + '再点「导入」也不会把它加回来）')) return;
    try {
      await api('/api/cashier/exclude', { method: 'POST', body: { id: r.id } });
      toast('已排除（华为原单没动）', 'good');
      if (_cashierEditing === Number(r.id)) cashierCardCancel();
      await loadCashier();
    } catch (e) {
      toast('排除失败：' + e.message, 'bad');
    }
    return;
  }
  await cashierRemove(id);          // 手工单：真删（确认 + entry-delete 在它里面）
}

function cashierCardEdit(id) {
  const r = _cashierRows[id];
  if (!r) return;
  _cashierEditing = Number(id);          // ⚠ dataset 是 string，归一成 number 才 === r.id
  _cashierEditAcc = JSON.parse(JSON.stringify(r.accessories || []));
  _cashierEditPay = JSON.parse(JSON.stringify(r.payments || []));
  // 商品行**打开就铺出来**（第三轮定的）：老卡没有商品行 ⇒ 把顶层字段补成
  // 第一行（首编保存后导出就以卡内行为准；没编过的老卡仍回退查 order_lines）
  // ⚠ 金额框是**单价**：老卡顶层 amount 是合计 ⇒ 除以数量还原单价
  //   （打开又保存不会把应收翻倍；改数量就是真的 数量 × 单价）
  _cashierEditProd = JSON.parse(JSON.stringify(r.products || []));
  if (!_cashierEditProd.length) {
    const q0 = Number(r.quantity) > 0 ? Number(r.quantity) : 1;
    _cashierEditProd = [{
      name: r.goods_name || '', code: r.goods_code || '', sn: r.sn || '',
      quantity: r.quantity == null ? 1 : r.quantity,
      amount: Math.round(((Number(r.amount) || 0) / q0) * 100) / 100,
      category: r.category || '',
    }];
  }
  renderCashierCards(Object.values(_cashierRows));
}

function cashierCardCancel() {
  _cashierEditing = 0;
  _cashierEditAcc = [];
  _cashierEditPay = [];
  _cashierEditProd = [];
  renderCashierCards(Object.values(_cashierRows));
}

function cashierAccAdd() {
  cashierEditState();            // 先把输入框里还没同步的字收进草稿
  _cashierEditAcc.push({ name: '', amount: 0, quantity: 1 });
  cashierRerenderEdit();
}

function cashierAccDel(i) {
  cashierEditState();            // 同上，别把其它配件刚敲的字刷掉
  _cashierEditAcc.splice(Number(i), 1);
  cashierRerenderEdit();
}

async function cashierCardSave(id) {
  const body = cashierEditState();
  if (!body) return;
  body.id = Number(id);
  if (!String(body.amount).trim()) {
    toast('应收金额还没填', 'bad');
    return;
  }
  try {
    await api('/api/cashier/entry-save', { method: 'POST', body });
    toast('已修改', 'good');
    _cashierEditing = 0;
    _cashierEditAcc = [];
    _cashierEditPay = [];
    _cashierEditProd = [];
    await loadCashier();
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
  }
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
  // 两段式：汇总条上「保存并记录」把待入库的条数亮出来（没待入库就干干净净）
  const staged = rows.filter((r) => r.status === 'staged').length;
  const cb = $('#cashier-commit');
  if (cb) {
    cb.textContent = staged ? `保存并记录（${staged} 笔待入库）` : '保存并记录';
    cb.title = staged
      ? `把今天 ${staged} 笔未入库的记录正式写进账`
      : '把今天还没入库的记录正式写进账（现在没有待入库的）';
  }
}

/** 「保存并记录」—— 当天暂存 → 已入库（两段式第二段）。 */
async function cashierCommit() {
  try {
    const d = await api('/api/cashier/commit',
      { method: 'POST', body: { day: cashierDayValue() } });
    if (!d.saved) {
      toast('这天没有待入库的记录', 'bad');
      return;
    }
    toast(`已入库 ${d.saved} 笔`, 'good');
    await loadCashier();
  } catch (e) {
    toast('入库失败：' + e.message, 'bad');
  }
}

/** 当月导出（销售表 + 政策表）—— 拿 file 走浏览器下载。 */
async function cashierExport() {
  const btn = $('#cashier-export');
  if (!btn || btn.disabled) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '导出中…';
  try {
    const month = cashierDayValue().slice(0, 7);      // 看哪个月就导哪个月
    const r = await api('/api/cashier/export',
      { method: 'POST', body: { month } });
    triggerDownload('/api/export/download?name=' + encodeURIComponent(r.file),
                    r.file);
    toast(`已导出 ${r.file}（${r.rows} 行）`, 'good');
  } catch (e) {
    toast('导出失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function renderCashierCards(rows) {
  const host = $('#cashier-cards');
  _cashierRows = {};
  (rows || []).forEach((r) => { _cashierRows[r.id] = r; });
  if (!rows || !rows.length) {
    if (host) host.innerHTML = '<div class="empty">这天还没有流水</div>';
    return;
  }
  if (host) {
    host.innerHTML = rows.map((r) => (_cashierEditing === r.id
      ? cashierCardEditHtml(r) : cashierCardHtml(r))).join('');
    // 编辑卡刚画出来 ⇒ 派生值与支付合计/黄条要立刻有值（不等用户敲第一个字）
    const editing = host.querySelector('.cashier-card.cc-editing');
    if (editing) {
      cashierProdSync(editing);
      cashierPaySync(editing);
    }
  }
}

async function loadCashier() {
  if (!cashierPageMounted) return;
  bindCashierEvents();
  // ⚠ 重画前把**没做完的编辑**收走（用户：删别的卡 / 刷新不许冲掉
  //   正在编辑的卡）—— 草稿先从 DOM 同步，画回来再把标量输入放回去。
  const stash = _cashierEditing ? cashierEditState() : null;
  if (stash) {
    _cashierEditAcc = stash.accessories || [];
    _cashierEditProd = stash.products || [];
    _cashierEditPay = stash.payments || [];
  }
  const dayEl = $('#cashier-day');
  if (dayEl && !dayEl.value) dayEl.value = cashierToday();
  const soldEl = $('#cashier-sold-at');
  if (soldEl && !soldEl.value) soldEl.value = cashierNow();
  try {
    const d = await cashierRead('/api/cashier/entries?day='
      + encodeURIComponent(cashierDayValue()));
    if (!d || !cashierPageMounted) return;
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
    if (stash) cashierRestoreEdit(stash);
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

/** 把 stash 里的标量输入（应收/销售员/备注/时间）写回刚重画的编辑卡。 */
function cashierRestoreEdit(st) {
  const card = document.querySelector('.cashier-card.cc-editing');
  if (!card) return;
  card.querySelectorAll('[data-f]').forEach((el) => {
    const v = st[el.dataset.f];
    if (v !== undefined && v !== null) el.value = v;
  });
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

/** 一键导入当天玲珑销售单 —— 拉单几十秒，跑的时候按钮禁用防连点。 */
async function cashierImport() {
  const btn = $('#cashier-import');
  if (!btn || btn.disabled) return;
  const day = cashierDayValue();
  if (!window.confirm(`导入 ${day} 的玲珑销售单？\n已导过的不会重复，`
    + `备注命中排除词的不进卡。`)) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '导入中…（拉单要一会儿）';
  try {
    const d = await api('/api/cashier/import', { method: 'POST', body: { day } });
    toast(`导入完成：新增 ${d.imported} 笔`
      + (d.skipped_blacklist ? `，排除词命中 ${d.skipped_blacklist}` : '')
      + (d.skipped_dup ? `，已存在 ${d.skipped_dup}` : '')
      + (d.imported ? '（还没入库，点「保存并记录」）' : ''), 'good');
    await loadCashier();
  } catch (e) {
    toast('导入失败：' + e.message, 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/** 排除关键词行内设置：展开时现读后端（别信页面上残留的旧值）。 */
function cashierBlacklistOpen() {
  const row = $('#cashier-blacklist-row');
  if (!row) return;
  row.hidden = !row.hidden;
  if (row.hidden) return;
  cashierRead('/api/cashier/import-settings').then((d) => {
    if (!d) return;
    const inp = $('#cashier-blacklist-input');
    if (inp) inp.value = (d.blacklist || []).join(', ');
    const hint = $('#cashier-blacklist-hint');
    if (hint) hint.textContent = '改完点保存，立即生效';
  }).catch((e) => { toast('读设置失败：' + e.message, 'bad'); });
}

async function cashierBlacklistSave() {
  const inp = $('#cashier-blacklist-input');
  const words = String((inp && inp.value) || '').split(/[,，]/)
    .map((s) => s.trim()).filter(Boolean);
  try {
    const d = await api('/api/cashier/import-settings',
      { method: 'PUT', body: { blacklist: words } });
    toast(`已保存 ${d.blacklist.length} 个排除词`, 'good');
    const row = $('#cashier-blacklist-row');
    if (row) row.hidden = true;
  } catch (e) {
    toast('保存失败：' + e.message, 'bad');
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
  // 「取消修改」= 取消**卡内编辑**（顶部表单只管新录，没有"改"这个状态了）
  $('#cashier-cancel').addEventListener('click', cashierCardCancel);
  $('#cashier-day').addEventListener('change', loadCashier);
  $('#cashier-refresh').addEventListener('click', cashierRefreshPolicy);
  $('#cashier-commit').addEventListener('click', cashierCommit);
  $('#cashier-export').addEventListener('click', cashierExport);
  $('#cashier-import').addEventListener('click', cashierImport);
  $('#cashier-blacklist-open').addEventListener('click', cashierBlacklistOpen);
  $('#cashier-blacklist-save').addEventListener('click', cashierBlacklistSave);
  // ⚠ 卡片是 innerHTML 重画的 ⇒ 只能**委托**到容器上（绑节点一次重画就没了）。
  //   属性名一律 `data-cc-*` / `data-acc-*` —— 别用旧表格的 data-edit/data-del。
  $('#cashier-cards').addEventListener('click', (e) => {
    const t = e.target;
    if (t.dataset.ccEdit) { cashierCardEdit(t.dataset.ccEdit); return; }
    if (t.dataset.ccSave) { cashierCardSave(t.dataset.ccSave); return; }
    if (t.dataset.ccCancel) { cashierCardCancel(); return; }
    if (t.dataset.accAdd) { cashierAccAdd(); return; }
    if (t.dataset.accDel !== undefined && t.dataset.accDel !== '') {
      cashierAccDel(t.dataset.accDel); return;
    }
    if (t.dataset.prodAdd) { cashierProdAdd(); return; }
    if (t.dataset.prodDel !== undefined && t.dataset.prodDel !== '') {
      cashierProdDel(t.dataset.prodDel); return;
    }
    if (t.dataset.payAdd) { cashierPayAdd(); return; }
    if (t.dataset.payDel !== undefined && t.dataset.payDel !== '') {
      cashierPayDel(t.dataset.payDel); return;
    }
    if (t.dataset.ccClose) { cashierCardClose(t.dataset.ccClose); return; }
  });
  // 商品行/支付金额一变 → 刷「应收」派生值 + 左上角「实收」与黄条（软提醒）
  $('#cashier-cards').addEventListener('input', (e) => {
    const card = e.target.closest && e.target.closest('.cashier-card.cc-editing');
    if (!card) return;
    cashierProdSync(card);
    cashierPaySync(card);
  });
}


function mountCashierPage() {
  cashierPageMounted = true;
  bindCashierEvents();
}

function unmountCashierPage() {
  cashierPageMounted = false;
  cashierPageEpoch += 1;
  cashierReadControllers.forEach((controller) => controller.abort());
  cashierReadControllers.clear();
}

registerPage('cashier', {
  mount: mountCashierPage,
  load: loadCashier,
  unmount: unmountCashierPage,
});
