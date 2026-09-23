/* 库存盘点 - **后端客户端**（2026-09-20 接进 cbg-reconcile 时的 M16 那一环）
 *
 * 上游那一版叫 `IC.erp`：浏览器拿着 token 直连云商（apireport / apicommon / api 三个域）。
 * 现在这一页**不碰云商**，全部走本项目控制台后端：
 *
 *   /api/inventory/ready       后端认到的是谁、有没有云商账号（不联网）
 *   /api/inventory/warehouses  仓库列表 + 本店默认哪个仓
 *   /api/inventory/book        账面（在库 + 在途，串号级）
 *   /api/inventory/transit     在途兜底
 *   /api/inventory/index       全库串号索引（只回六列，见后端 book.py）
 *   /api/inventory/export      导出并推送（请求体就是那份 xlsx）
 *
 * ⚠ **行是原样回来的**：`book` 那些键（`Imei` / `ProCount_OnTransfer` / `OldFlag` …）
 *   正是 `core.js` 读的那套 ⇒ 口径（归一化 / uid / 在途拆分）一行没动，全在页面里。
 * ⚠ 这里**不做任何重算** —— 只做三件后端不该管的事：
 *   ① 仓库行 PascalCase → 页面用的 camelCase；② 把 `{ok:false,why}` 变成 Error；
 *   ③ 全库索引拼成 Map、在途按仓再过滤一次（这两条是上游原有逻辑，照搬）。
 */
globalThis.IC = globalThis.IC || {};
(function () {
  const IC = globalThis.IC;
  const core = IC.core;

  //: 页面配置对象 —— 只有"后端认到的那点身份"和界面偏好，**没有任何凭据**。
  //: ⚠ `ready` 是 `isConfigured()` 唯一的判据（后端配好云商账号了没）。
  const DEFAULT_CFG = {
    ready: false,
    companycode: '',
    username: '',
    erpName: '',
  };

  function isConfigured(cfg) {
    return !!(cfg && cfg.ready);
  }

  function assertConfigured(cfg) {
    if (!isConfigured(cfg)) {
      const e = backendError('这台机器还没配云商账号 —— 去控制台「通用设置 › 云商账号」配一次');
      e.notConfigured = true;
      throw e;
    }
  }

  /** 后端业务失败（`ok:false`）或 HTTP 出错。 */
  function backendError(msg) {
    const e = new Error(msg || '后端没给原因');
    e.backend = true;
    return e;
  }

  function incomplete(what, detail) {
    const e = new Error(what + (detail ? '：' + detail : ''));
    e.incomplete = true;
    return e;
  }

  async function readJSON(resp, what) {
    let data = null;
    let text = '';
    try {
      text = await resp.text();
      data = text ? JSON.parse(text) : null;
    } catch (e) {
      data = null;
    }
    if (!resp.ok) {
      const why = (data && (data.error || data.why || data.message)) || text.slice(0, 200);
      throw backendError(`${what}失败（HTTP ${resp.status}）${why ? '：' + why : ''}`);
    }
    if (!data || typeof data !== 'object') throw backendError(`${what}失败：后端返回的不是 JSON`);
    // ⚠ 业务失败一律 **200 + ok:false + why**（后端那边的约定）——
    //   不转成 Error 的话，页面上会变成"成功地拿到 0 行"，那是"看着很合理的空"。
    if (data.ok === false) throw backendError((data.why || data.error || '后端说没成'));
    return data;
  }

  async function getJSON(path, what) {
    return readJSON(await fetch(path, { headers: { Accept: 'application/json' } }), what);
  }

  async function postJSON(path, body, what) {
    return readJSON(
      await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body || {}),
      }),
      what
    );
  }

  /** 问后端：云商账号配好了吗、是哪家店、后端认到的是谁。 */
  async function ready() {
    const d = await getJSON('/api/inventory/ready', '读取后端账号');
    return d;
  }

  /** 仓库列表 → `[{id, name, branchName, branchId}]`（页面按这套 camelCase 用）。
   *
   *  ⚠ 顺手把后端的 `default_store_id`（**本店那个仓**，后端按门店名单匹配出来的）
   *    记进 `cfg.defaultStoreId` —— 页面拿它做下拉的默认选中，
   *    少一次"手滑选了隔壁店"的机会（选错的表现是"账面看着正常、盘的是别人家的货"）。
   */
  async function fetchWarehouses(cfg) {
    assertConfigured(cfg);
    const d = await getJSON('/api/inventory/warehouses', '读取仓库列表');
    const list = Array.isArray(d.warehouses) ? d.warehouses : [];
    cfg.defaultStoreId = core.s(d.default_store_id);
    cfg.defaultStoreName = core.s(d.default_store_name);
    cfg.erpName = core.s(d.erp_store_name) || cfg.erpName;
    return list
      .map((x) => ({
        id: String(x.Id),
        name: core.s(x.Name),
        branchName: core.s(x.BranchName),
        branchId: core.s(x.BranchId),
      }))
      .filter((x) => x.id && x.name)
      .sort((a, b) => a.id.localeCompare(b.id));
  }

  /**
   * 账面（在库 + 在途，串号级）。
   * @returns {Promise<{rows:Array, totalRows:number, pages:number, empty:boolean}>}
   */
  async function fetchInventory(cfg, opt) {
    assertConfigured(cfg);
    opt = opt || {};
    const what = opt.label || '库存查询';
    const d = await postJSON('/api/inventory/book',
      { date: opt.date || '', storeId: opt.storeId ? String(opt.storeId) : '' }, what);
    const rows = Array.isArray(d.rows) ? d.rows : [];
    const totalRows = Number(d.total);
    // ⚠ 行数自检在这儿再兜一次：后端已经自检过（拉不全就报错），
    //   但这一层是"页面拿到的到底是不是全量"的最后一道 —— 少了会被判成表外码。
    if (Number.isFinite(totalRows) && rows.length !== totalRows) {
      throw incomplete(what, `拿到 ${rows.length} 行，后端说应有 ${totalRows} 行`);
    }
    if (typeof opt.onProgress === 'function') {
      try {
        opt.onProgress(rows.length, totalRows);
      } catch (e) {}
    }
    return { rows: rows, totalRows: totalRows, pages: Number(d.pages) || 1, empty: rows.length === 0 };
  }

  /**
   * 在途（待入库）兜底 —— 账面的在途列拿不到时才走这条。
   *
   * 拿回来再**按仓过滤一次**（照上游原样）：接口没带仓库信息时**不丢弃**
   * （宁可多展示也不漏），带了但不是本仓的才丢。
   */
  async function fetchInTransit(cfg, opt) {
    assertConfigured(cfg);
    opt = opt || {};
    const d = await postJSON('/api/inventory/transit',
      { date: opt.date || '', storeId: opt.storeId ? String(opt.storeId) : '' }, '在途查询');
    const rows = Array.isArray(d.rows) ? d.rows : [];
    const storeId = core.s(opt.storeId);
    const storeName = core.s(opt.storeName);
    const kept = [];
    let droppedByStore = 0;
    rows.forEach((r) => {
      const sid = core.s(r.StoreId || r.storeId);
      const sname = core.s(r.StoreName || r.Store || r.storeName);
      const matchId = storeId && sid && sid === storeId;
      const matchName = storeName && sname && sname === storeName;
      if (!sid && !sname) {
        kept.push(r);
        return;
      }
      if (matchId || matchName) kept.push(r);
      else droppedByStore++;
    });
    return { rows: kept, fetched: rows.length, droppedByStore: droppedByStore, totalRows: rows.length };
  }

  /** 全公司串号索引 → `Map<码, [{store, name}]>`（「表外码」据此说归属）。 */
  async function fetchGlobalIndex(cfg, opt) {
    assertConfigured(cfg);
    opt = opt || {};
    const d = await postJSON('/api/inventory/index', { date: opt.date || '' }, '全库索引');
    const rows = Array.isArray(d.rows) ? d.rows : [];
    const map = new Map();
    rows.forEach((raw) => {
      const store = core.s(raw.Store);
      const name = core.s(raw.ProName);
      const it = core.normalizeItem(raw, '', 0);
      it.serials.forEach((code) => {
        const k = core.normCode(code);
        if (!k) return;
        if (!map.has(k)) map.set(k, []);
        const arr = map.get(k);
        if (!arr.some((x) => x.store === store)) arr.push({ store: store, name: name });
      });
    });
    return map;
  }

  /**
   * 「导出并推送」—— 请求体就是那份 xlsx（`Uint8Array` 可以直接当 body）。
   * 名字 / 门店 / 日期走 query，后端拿它们落盘、写推送文案。
   */
  async function exportAndPush(bytes, opt) {
    opt = opt || {};
    const q = new URLSearchParams({
      name: opt.name || '库存盘点',
      store: opt.store || '',
      date: opt.date || '',
    });
    const resp = await fetch('/api/inventory/export?' + q.toString(), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      },
      body: bytes,
    });
    const d = await readJSON(resp, '导出并推送');
    return d;
  }

  IC.api = {
    DEFAULT_CFG,
    isConfigured,
    assertConfigured,
    ready,
    fetchWarehouses,
    fetchInventory,
    fetchInTransit,
    fetchGlobalIndex,
    exportAndPush,
    backendError,
    incomplete,
    // 没有 token 这回事了 —— 留着这两个名字是因为 ui.js 里还有几处判断，
    // 它们现在恒为 false/恒等（后端自己会拿 `.secrets/erp.env` 那份账号重登一次）。
    isTokenError: () => false,
    isIncomplete: (e) => !!(e && e.incomplete),
  };
})();
