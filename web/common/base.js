/* 公共显示层 · 请求 / 转义 / 提示 / 时间 / 表格（协议 v2 §4.4，2026-10-02）。
   ⚠ 从 app.js 整块搬出来的（只搬不改）—— app.js 与本文件按 index.html 的
   script 顺序拼成同一份前端；测试里 `tests` 目录也按同一顺序拼着读。 */

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

const state = { overview: null, since: 0, jobId: null,
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
  // ⚠ `good` 是老写法（收银那批调用点），CSS 只认 `ok`/`bad` ——
  //   不映射就成了没样式的裸 toast：`--ink-2` 底 + `--on-brand` 字，
  //   暮山蓝主题里俩都是近黑 ⇒ 1:1 看不见（用户 2026-09-30 截图那条「已修改」）。
  if (kind === 'good') kind = 'ok';
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast ' + kind;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, kind === 'bad' ? 6000 : 3000);
}

/** 区域名按后端汇总键规范化：去空格，空值统一归到「其他」。 */
function normalizeRegionName(s) {
  const t = String(s == null ? '' : s).trim();
  return t || '其他';
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
