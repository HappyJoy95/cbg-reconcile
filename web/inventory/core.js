/* 库存盘点 - 核心逻辑（纯逻辑，无 DOM、无网络，可在 Node 里单测）
 * 归入 globalThis.IC.core
 */
globalThis.IC = globalThis.IC || {};
(function () {
  const IC = globalThis.IC;

  /* ---------------- 基础工具 ---------------- */

  // 转字符串并去首尾空白
  function s(v) {
    return v === null || v === undefined ? '' : String(v).trim();
  }

  // 串号码归一化：去掉所有空白（含全角空格），转大写
  // 目的：容忍扫码枪带入的空格/换行，以及大小写差异
  function normCode(v) {
    return s(v).replace(/[\s\u3000]+/g, '').toUpperCase();
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  // 格式化时间戳
  function fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(
      d.getMinutes()
    )}:${p(d.getSeconds())}`;
  }

  function todayStr(ts) {
    const d = ts ? new Date(ts) : new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  /* ---------------- 数据归一化 ---------------- */

  // 一行 ERP 库存记录 -> 盘点项
  // 接口字段映射：Imei=串号1、Imei2=串号2、Imei3=串号3、SubImei=三个串号空格拼接
  function normalizeItem(raw, storeId, idx) {
    const serials = [];
    const push = (v) => {
      const c = s(v);
      if (c && serials.indexOf(c) < 0) serials.push(c);
    };
    push(raw.Imei);
    push(raw.Imei2);
    push(raw.Imei3);
    // SubImei 兜底：某些行 Imei2/Imei3 为空，但 SubImei 里带着全部串号
    s(raw.SubImei)
      .split(/[\s,;，；]+/)
      .forEach(push);

    const rowId = raw.RowId === undefined || raw.RowId === null ? idx : raw.RowId;
    return {
      key: 'r' + rowId,
      rowId: rowId,
      storeId: storeId,
      store: s(raw.Store),
      name: s(raw.ProName),
      cat1: s(raw.Category1),
      cat2: s(raw.Category2),
      cat3: s(raw.Category3),
      cat4: s(raw.Category4),
      brand: s(raw.Brand),
      model: s(raw.Model),
      tag: s(raw.OldFlag), // 串号标识：样/新/J/K 等
      proId: s(raw.ProId), // 商品编码
      code69: s(raw.SNCode), // 69码
      priceLabel: s(raw.PriceLabel), // 价格标签
      qty: num(raw.ProCount), // 在库数量
      qtyOnTransfer: num(raw.ProCount_OnTransfer), // 在途数量（同表另取一列）
      serials: serials,
      hasSerial: serials.length > 0,
      // 在途（待入库）：这行账挂在（目标）门店上，但实物还没到。
      // 判定规则：本店在库为 0、但在途数量 > 0 —— 在库优先，避免把同时在库的行误判成在途。
      inTransit: false,
      inTransitInfo: null,
      // 盘点结果（扫描后填充）
      matchedCode: '',
      matchedAt: 0,
      extraSerialHits: [], // 同一台机器的其它串号被扫到时记录在此
      manualAt: 0, // 手工确认「已找到」的时间（扫不到码的样机等）
    };
  }

  /**
   * 归一化 + 在途拆分（方案 A：在库配在库、在途配在途）
   *  - 在库为 0、在途 > 0        → 整行就是在途，生成一条在途条目
   *  - 在库 > 0、在途 > 0、无串号 → 拆成两条：在库那条走实盘数量核对，在途那条进「在途待入库」
   *  - 在库 > 0、在途 > 0、有串号 → 按业务确认不会出现；真出现以在库为准，
   *                                在途数量保留在条目上（导出可见），不生成重复条目、不撞 uid
   */
  function normalizeAll(rows, storeId) {
    const out = [];
    (rows || []).forEach((r, i) => {
      const it = normalizeItem(r, storeId, i);
      const transitQty = it.qtyOnTransfer;
      if (transitQty <= 0) {
        out.push(it);
        return;
      }
      if (it.qty <= 0) {
        it.inTransit = true;
        it.qty = transitQty; // 在途行用它自己的数量，别显示 0
        it.inTransitInfo = { from: '账面在途列', receivingCode: '' };
        out.push(it);
        return;
      }
      // 两态并存
      out.push(it); // 在库那条照旧
      if (!it.hasSerial) out.push(transitTwinOf(it)); // 无串号：在途单独成条
    });
    return assignUids(out);
  }

  /**
   * 盘点的「身份」标识（uid）：跨次查询稳定，不依赖报表行号 RowId。
   *  - 有串号：主串号（任一次拉取都能对上）
   *  - 无串号：仓库 + 商品编码 + 完整分组属性（名称/分类/标识）
   * 同身份出现多条时，uid 追加 #2/#3 去重，并把 dupUid 标为 true；
   * 这种「匹配不唯一」的条目在更新账面时一律不自动继承状态，交人工核实。
   */
  function itemUid(it) {
    if (it.hasSerial) return 'sn:' + normCode(it.serials[0]);
    // 无串号按商品属性；在途那条用 nst: 前缀，和「同一个商品的在库条目」区分开
    // （方案 A：在库配在库、在途配在途，两条各自独立）
    return (
      (it.inTransit ? 'nst:' : 'ns:') +
      [it.storeId, it.proId, it.cat1, it.cat2, it.cat3, it.cat4, it.name, it.tag].join('|')
    );
  }

  /** 由在库条目派生一条「在途」条目（数量取在途数量，uid 用 nst: 前缀） */
  function transitTwinOf(it) {
    return {
      key: '',
      uid: '',
      baseUid: '',
      dupUid: false,
      rowId: it.rowId + '-T',
      storeId: it.storeId,
      store: it.store,
      name: it.name,
      cat1: it.cat1,
      cat2: it.cat2,
      cat3: it.cat3,
      cat4: it.cat4,
      brand: it.brand,
      model: it.model,
      tag: it.tag,
      proId: it.proId,
      code69: it.code69,
      priceLabel: it.priceLabel,
      qty: it.qtyOnTransfer,
      qtyOnTransfer: 0,
      serials: [],
      hasSerial: false,
      inTransit: true,
      inTransitInfo: { from: '账面在途列', receivingCode: '', fromTwin: true },
      matchedCode: '',
      matchedAt: 0,
      extraSerialHits: [],
      manualAt: 0,
    };
  }

  function assignUids(items) {
    const seen = Object.create(null);
    const counts = Object.create(null);
    items.forEach((it) => {
      it.baseUid = itemUid(it);
      counts[it.baseUid] = (counts[it.baseUid] || 0) + 1;
    });
    items.forEach((it) => {
      const n = (seen[it.baseUid] = (seen[it.baseUid] || 0) + 1);
      it.uid = n === 1 ? it.baseUid : it.baseUid + '#' + n;
      it.dupUid = counts[it.baseUid] > 1; // 同身份有多条 → 不唯一
      it.key = it.uid; // 所有可变状态都挂 uid，不再用 RowId
    });
    return items;
  }

  /** 冻结账面条目：只保留不可变字段（状态另存，便于按 uid 关联） */
  function bookRecord(it) {
    return {
      inTransit: !!it.inTransit, // 在途（待入库）：账面已挂到本店，但货还没到
      inTransitInfo: it.inTransitInfo || null, // 来源仓 / 收货单号等
      uid: it.uid,
      rowId: it.rowId,
      name: it.name,
      c1: it.cat1,
      c2: it.cat2,
      c3: it.cat3,
      c4: it.cat4,
      brand: it.brand,
      model: it.model,
      tag: it.tag,
      proId: it.proId,
      code69: it.code69,
      priceLabel: it.priceLabel,
      qty: it.qty,
      qtyOnTransfer: it.qtyOnTransfer || 0,
      serials: it.serials,
      dupUid: it.dupUid,
    };
  }

  function bookFromItems(items) {
    return items.map(bookRecord);
  }

  /** 从冻结账面还原盘点条目 */
  function itemsFromBook(book, storeId, storeName) {
    return (book || []).map((r) => ({
      key: r.uid,
      uid: r.uid,
      baseUid: r.uid.split('#')[0],
      dupUid: !!r.dupUid,
      rowId: r.rowId,
      storeId: storeId,
      store: storeName,
      name: r.name,
      cat1: r.c1,
      cat2: r.c2,
      cat3: r.c3,
      cat4: r.c4,
      brand: r.brand,
      model: r.model,
      tag: r.tag,
      proId: r.proId,
      code69: r.code69,
      priceLabel: r.priceLabel,
      qty: r.qty,
      qtyOnTransfer: r.qtyOnTransfer || 0,
      serials: (r.serials || []).slice(),
      hasSerial: !!(r.serials && r.serials.length),
      inTransit: !!r.inTransit,
      inTransitInfo: r.inTransitInfo || null,
      matchedCode: '',
      matchedAt: 0,
      extraSerialHits: [],
      manualAt: 0,
    }));
  }

  /**
   * 把「在途（待入库）」行并进账面。
   * 这些货已挂在目标门店账上但还没到店，所以必须带出来、但**不能算进"必须扫到"的应盘数**。
   * 串号已经在在库账面上的（例如货已到、ERP 还没换状态）以在库为准，不重复加。
   */
  function mergeInTransit(items, transitRows, storeId, storeName) {
    const have = new Set();
    items.forEach((it) => it.serials.forEach((c) => have.add(normCode(c))));
    const added = [];
    (transitRows || []).forEach((raw, i) => {
      const serials = [];
      const push = (v) => {
        const c = s(v);
        if (c && serials.indexOf(c) < 0) serials.push(c);
      };
      push(raw.Imei);
      push(raw.Imei2);
      push(raw.Imei3);
      s(raw.SubImei)
        .split(/[\s,;，；]+/)
        .forEach(push);
      if (serials.length && serials.some((c) => have.has(normCode(c)))) return; // 已在库，跳过
      const it = {
        inTransit: true,
        inTransitInfo: {
          fromStore: s(raw.FromStoreName || raw.OutStoreName || raw.SourceStoreName || ''),
          receivingCode: s(raw.ReceivingCode || raw.BillCode || raw.Code || ''),
          expectDate: s(raw.ExpectDate || raw.ArrivalDate || raw.CreateTime || ''),
        },
        rowId: s(raw.RowId) || 'T' + (i + 1),
        storeId: storeId,
        store: storeName,
        name: s(raw.ProName || raw.ProductName || raw.Name),
        cat1: s(raw.Category1 || raw.Category),
        cat2: s(raw.Category2),
        cat3: s(raw.Category3),
        cat4: s(raw.Category4),
        brand: s(raw.Brand),
        model: s(raw.Model),
        tag: s(raw.OldFlag),
        proId: s(raw.ProId || raw.ProductId),
        code69: s(raw.SNCode),
        priceLabel: s(raw.PriceLabel),
        qty: num(raw.ProCount) || 1,
        serials: serials,
        hasSerial: serials.length > 0,
        matchedCode: '',
        matchedAt: 0,
        extraSerialHits: [],
        manualAt: 0,
      };
      added.push(it);
    });
    const all = items.concat(added);
    assignUids(all);
    return { items: all, added: added };
  }

  /**
   * 比对两版账面（用于「更新账面」评审）
   * 按 uid 关联；不唯一的条目（dupUid）一律算 ambiguous，不自动继承状态。
   */
  function diffBooks(oldBook, newBook) {
    const idx = (book) => {
      const m = new Map();
      (book || []).forEach((r) => {
        const base = r.uid.split('#')[0];
        if (!m.has(base)) m.set(base, []);
        m.get(base).push(r);
      });
      return m;
    };
    const o = idx(oldBook);
    const n = idx(newBook);
    const added = [];
    const removed = [];
    const changed = [];
    const ambiguous = [];
    const same = [];

    n.forEach((list, base) => {
      const oldList = o.get(base) || [];
      if (list.length > 1 || oldList.length > 1) {
        ambiguous.push({ base: base, oldCount: oldList.length, newCount: list.length, name: list[0].name });
        return;
      }
      if (!oldList.length) {
        added.push(list[0]);
        return;
      }
      const a = oldList[0];
      const b = list[0];
      if (a.qty !== b.qty) changed.push({ uid: b.uid, name: b.name, oldQty: a.qty, newQty: b.qty });
      else same.push(b);
    });
    o.forEach((list, base) => {
      if (!n.has(base) && list.length === 1) removed.push(list[0]);
    });
    return {
      added: added,
      removed: removed,
      changed: changed,
      ambiguous: ambiguous,
      sameCount: same.length,
      oldCount: (oldBook || []).length,
      newCount: (newBook || []).length,
    };
  }

  // 扫描结果状态
  const OK = 'ok'; // 盘到（本店账上有，首次）
  const DUP = 'dup'; // 重复扫（该码或该机器已经盘到过）
  const FOREIGN = 'foreign'; // 本店账上没有，但全公司别的仓库有
  const UNKNOWN = 'unknown'; // 全公司都查不到

  const STATUS_LABEL = {
    ok: '已盘到',
    dup: '重复扫描',
    foreign: '非本店库存',
    unknown: '查无此码',
  };

  /* ---------------- 盘点会话（引擎） ---------------- */

  let sessionSeq = 0;
  function newSessionId() {
    sessionSeq++;
    return (
      's' +
      Date.now().toString(36) +
      '-' +
      sessionSeq.toString(36) +
      '-' +
      Math.random().toString(36).slice(2, 6)
    );
  }

  class Stocktake {
    /**
     * @param {object} opt {date, storeId, storeName, items}
     */
    constructor(opt) {
      opt = opt || {};
      this.schema = 2; // 数据格式版本
      this.sessionId = s(opt.sessionId) || newSessionId();
      this.date = s(opt.date);
      this.storeId = s(opt.storeId);
      this.storeName = s(opt.storeName);
      this.startedAt = opt.startedAt || Date.now();
      this.bookVersion = opt.bookVersion || 1; // 账面版本号
      this.bookFetchedAt = opt.bookFetchedAt || 0; // 账面实际拉取时间
      this.companycode = s(opt.companycode);
      this.pendingReview = opt.pendingReview || []; // 更新账面后需要人工核实的条目
      this.items = opt.items || [];
      this.scans = []; // 扫码流水（含重复/异常）
      this.codeHits = new Map(); // 归一化码 -> 盘点项 key
      this.scanByCode = new Map(); // 归一化码 -> 扫描记录下标
      this.itemByKey = new Map(); // key -> item
      this.manualQty = Object.create(null); // 无串号商品实盘数量 key->number
      this.remarks = Object.create(null); // 备注 key/code -> string
      this.manualFound = Object.create(null); // 手工确认「找到了」的商品 key->{ts}
      this.manualNotes = Object.create(null); // 手工确认备注 key->string
      this.autoQty = Object.create(null); // 实盘数量是确认时自动带出的（撤销时该清掉）key->true
      this.globalIndex = null; // 归一化码 -> [{store, name}]（全库索引，可选）
      this.rebuildItemIndex();
    }

    rebuildItemIndex() {
      this.itemByKey = new Map();
      this.codeHits = new Map();
      this.items.forEach((it) => {
        this.itemByKey.set(it.key, it);
        it.serials.forEach((c) => {
          const k = normCode(c);
          if (!k) return;
          if (!this.codeHits.has(k)) this.codeHits.set(k, []);
          const arr = this.codeHits.get(k);
          if (arr.indexOf(it.key) < 0) arr.push(it.key);
        });
      });
    }

    /** 全库串号索引：用于识别"本店没有但别店有"的码 */
    setGlobalIndex(map) {
      this.globalIndex = map || null;
      // 索引到位后，回填此前判为查无此码的流水
      this.scans.forEach((sc) => {
        if (sc.status === UNKNOWN) {
          const owners = this.lookupGlobal(sc.code);
          if (owners.length) {
            sc.status = FOREIGN;
            sc.owners = owners;
          }
        }
      });
    }

    lookupGlobal(code) {
      if (!this.globalIndex) return [];
      const hit = this.globalIndex.get(normCode(code));
      return hit ? hit.slice(0) : [];
    }

    /**
     * 处理一次扫码
     * @returns {object} 扫描结果 {status, code, item, owners, message, ...}
     */
    scan(rawCode, ts) {
      const raw = s(rawCode);
      const code = normCode(raw);
      const time = ts || Date.now();
      const res = { raw: raw, code: code, ts: time, item: null, owners: [] };

      if (!code) {
        res.status = UNKNOWN;
        res.message = '空码，已忽略';
        return res;
      }

      const prevIdx = this.scanByCode.get(code);
      const keys = this.codeHits.get(code) || [];

      if (prevIdx !== undefined) {
        // 同一个码被扫第二次
        const prev = this.scans[prevIdx];
        res.status = DUP;
        res.item = prev.item || null;
        res.message = `重复扫描（首次 ${fmtTime(prev.ts)}）`;
        res.owners = prev.owners || [];
      } else if (keys.length) {
        const it = this.itemByKey.get(keys[0]);
        if (!it) {
          res.status = UNKNOWN;
          res.message = '库存项已失效';
        } else if (it.matchedCode) {
          // 该台机器的另一个串号已经盘到过
          res.status = DUP;
          res.item = it;
          res.message = `同一台机器已盘到（本码 ${it.matchedCode}，首次 ${fmtTime(it.matchedAt)}）`;
          it.extraSerialHits.push({ code: code, ts: time });
        } else {
          it.matchedCode = code;
          it.matchedAt = time;
          // 之前如果有手工确认，现在有扫码实证了，以扫码为准。
          // 记录保留在 manualFound 里：万一这次扫码被撤销，手工确认还能恢复。
          if (it.manualAt) it.manualAt = 0;
          res.status = OK;
          res.item = it;
          res.message = '已盘到';
        }
        if (keys.length > 1) res.dupKeys = keys.slice(1);
      } else {
        const owners = this.lookupGlobal(code);
        res.owners = owners;
        if (owners.length) {
          res.status = FOREIGN;
          res.message = `本店账面没有该码，属于：${owners.map((o) => o.store).join('、')}`;
        } else {
          res.status = UNKNOWN;
          // 索引没就绪时不能下"全公司都没有"的结论，只能说本店账面没有
          res.message = this.globalIndex
            ? '全公司账面均无此码'
            : '本店账面没有该码，归属尚未核实';
        }
      }

      const rec = {
        code: code,
        raw: raw,
        ts: time,
        status: res.status,
        itemKey: res.item ? res.item.key : '',
        owners: res.owners || [],
        note: '',
      };
      this.scans.push(rec);
      if (prevIdx === undefined) this.scanByCode.set(code, this.scans.length - 1);
      res.seq = this.scans.length;
      res.record = rec;
      return res;
    }

    /** 撤销最后一笔扫描（不论类型：正常/重复/表外码都可撤销） */
    undo() {
      if (!this.scans.length) return null;
      const last = this.scans[this.scans.length - 1];
      return this.removeScan(this.scans.length - 1) ? last : null;
    }

    /**
     * 用给定的流水重建盘点状态。
     * 为什么整条重放而不是原地改：一次扫码会影响「商品是否已盘到」「是否重复」
     * 「其它串号命中」等多处状态，重放是唯一能保证一致的做法（几百条毫秒级）。
     */
    replayFrom(list) {
      this.scans = [];
      this.scanByCode = new Map();
      this.items.forEach((it) => {
        it.matchedCode = '';
        it.matchedAt = 0;
        it.extraSerialHits = [];
      });
      (list || []).forEach((r) => this.scan(r.code, r.ts));
      this.applyManualState();
    }

    /** 删除指定的一笔扫描（按流水下标）——「撤销上一扫」和逐条撤销都走这里 */
    removeScan(index) {
      const rec = this.scans[index];
      if (!rec) return null;
      const rest = this.scans.slice(0, index).concat(this.scans.slice(index + 1));
      this.replayFrom(rest);
      return rec;
    }

    /**
     * 单独撤销某一台机器的扫码记录（「已扫明细」里那一行）
     * 优先删掉让它变成「已盘到」的那次扫码；没有就删它的任意一条记录。
     */
    undoScanForItem(uid) {
      let idx = this.scans.findIndex((r) => r.itemKey === uid && r.status === OK);
      if (idx < 0) idx = this.scans.findIndex((r) => r.itemKey === uid);
      if (idx < 0) return null;
      return this.removeScan(idx);
    }

    /** 删掉某个码的全部扫描记录（表外码、重复扫码都能单独删）；返回删掉几条 */
    removeCode(code) {
      const c = normCode(code);
      const rest = this.scans.filter((r) => r.code !== c);
      const removed = this.scans.length - rest.length;
      if (!removed) return 0;
      this.replayFrom(rest);
      return removed;
    }

    /** 清空所有扫码流水（不影响库存数据） */
    clearScans() {
      this.scans = [];
      this.scanByCode = new Map();
      this.items.forEach((it) => {
        it.matchedCode = '';
        it.matchedAt = 0;
        it.extraSerialHits = [];
      });
    }

    /* ---- 手工确认（样机没盒子、条码扫不出来时用） ---- */

    /**
     * 手工确认「找到了」：计入已盘，但不属于扫码实证，
     * 会单独统计并可写备注（谁、为什么），便于事后追溯。
     * 有串号的是「样机没盒子」这类；无串号的是整箱配件、促销品这类扫不了码的，
     * 确认时实盘数量默认等于账面数量（有差异再改实盘数量）。
     */
    confirmFound(key, ts) {
      const it = this.itemByKey.get(key);
      if (!it) return null;
      if (it.matchedCode) return null; // 已经扫码盘到，无需再手工确认
      if (it.manualAt) return it; // 已经确认过：幂等，保留最早那次确认时间
      const time = ts || Date.now();
      this.manualFound[key] = { ts: time };
      it.manualAt = time;
      if (!it.hasSerial && this.manualQty[key] === undefined) {
        this.manualQty[key] = it.qty; // 无串号：默认「账面的都在」
        this.autoQty[key] = true; // 标记为自动带出，撤销时清掉；手填的数量不会被清
      }
      return it;
    }

    /**
     * 一键确认：把所有「已经填了实盘数量」且还没确认的无串号商品批量标记为找到。
     * 没填数量的一律不动（避免把没盘的当成盘过了）。
     * @returns {Array} 被确认的条目
     */
    confirmAllFilledNonSerial(ts, keys) {
      const time = ts || Date.now();
      const pool =
        keys && keys.length
          ? keys.map((k) => this.itemByKey.get(k)).filter(Boolean)
          : this.nonSerialItems();
      const list = pool.filter(
        (it) => !it.hasSerial && !it.manualAt && this.manualQty[it.key] !== undefined
      );
      list.forEach((it) => this.confirmFound(it.key, time));
      return list;
    }

    /** 有多少无串号商品已经填了实盘数量但还没确认 */
    pendingNonSerial() {
      return this.nonSerialItems().filter((it) => !it.manualAt && this.manualQty[it.key] !== undefined);
    }

    /** 取消手工确认（数量若是确认时自动带出的，一并清掉；手填的保留） */
    unconfirmFound(key) {
      const it = this.itemByKey.get(key);
      delete this.manualFound[key];
      if (it) {
        it.manualAt = 0;
        if (this.autoQty[key]) {
          delete this.manualQty[key];
          delete this.autoQty[key];
        }
      }
      return it;
    }

    isManualFound(key) {
      const it = this.itemByKey.get(key);
      return !!(it && it.manualAt);
    }

    manualNote(key) {
      return this.manualNotes[key] || '';
    }

    setManualQty(key, value) {
      const t = s(value);
      if (t === '') {
        delete this.manualQty[key];
      } else {
        this.manualQty[key] = num(t);
        delete this.autoQty[key]; // 手填的，撤销确认时保留
      }
      return this.manualQty[key];
    }

    setManualNote(key, note) {
      const t = s(note);
      if (t) this.manualNotes[key] = t;
      else delete this.manualNotes[key];
      return t;
    }

    /** 把 manualFound 记录派生到条目上（已扫码盘到的不再算手工确认） */
    applyManualState() {
      this.items.forEach((it) => {
        const rec = this.manualFound[it.key];
        it.manualAt = rec && !it.matchedCode ? rec.ts || 0 : 0;
      });
    }

    /* ---- 结果集 ---- */

    /** 有串号且**在库**（在途的不算"必须扫到"） */
    serialItems() {
      return this.items.filter((it) => it.hasSerial && !it.inTransit);
    }

    /** 在途（待入库）条目：账面已挂到本店，但货还没到 */
    inTransitItems() {
      return this.items.filter((it) => it.inTransit);
    }

    /** 在途里已经扫到的（货实际到了） */
    inTransitArrived() {
      return this.items.filter((it) => it.inTransit && (it.matchedCode || it.manualAt));
    }

    /** 无串号的**在库**商品（在途那条不算：它不参与实盘数量核对） */
    nonSerialItems() {
      return this.items.filter((it) => !it.hasSerial && !it.inTransit);
    }

    /** 扫码盘到的 */
    scanned() {
      return this.items.filter((it) => it.matchedCode);
    }

    /** 手工确认且未扫码的（含有串号样机与无串号商品） */
    manualList() {
      return this.items.filter((it) => it.manualAt && !it.matchedCode);
    }

    /** 有串号的手工确认（用于「已盘到」的构成拆分） */
    manualSerial() {
      return this.items.filter((it) => it.hasSerial && it.manualAt && !it.matchedCode);
    }

    /** 无串号里已确认找到的 */
    manualNonSerial() {
      return this.items.filter((it) => !it.hasSerial && it.manualAt);
    }

    /** 已盘明细 = 扫码盘到 + 手工确认（含有串号与无串号） */
    found() {
      return this.items.filter((it) => it.matchedCode || it.manualAt);
    }

    /** 应盘未盘：在库、有串号、既没扫到也没手工确认（在途的不算，它本来就不在店里） */
    missing() {
      return this.items.filter((it) => it.hasSerial && !it.inTransit && !it.matchedCode && !it.manualAt);
    }

    /** 表外串号：扫到但本店账上没有的（含全公司都没有的） */
    extras() {
      const seen = new Map();
      this.scans.forEach((sc, i) => {
        if (sc.status !== FOREIGN && sc.status !== UNKNOWN) return;
        if (seen.has(sc.code)) {
          seen.get(sc.code).times++;
          return;
        }
        seen.set(sc.code, {
          code: sc.code,
          raw: sc.raw,
          ts: sc.ts,
          status: sc.status,
          owners: sc.owners || [],
          times: 1,
          seq: i,
        });
      });
      return [...seen.values()];
    }

    /** 无串号商品的账实对照（手动确认找到的，实盘数量按账面计） */
    nonSerialRows() {
      return this.nonSerialItems().map((it) => {
        const entered = this.manualQty[it.key];
        let actual = entered === undefined || entered === null || entered === '' ? null : num(entered);
        if (actual === null && it.manualAt) actual = it.qty; // 确认找到 = 账面的都在
        return {
          item: it,
          book: it.qty,
          actual: actual,
          diff: actual === null ? null : actual - it.qty,
          confirmed: !!it.manualAt,
          remark: this.remarks[it.key] || '',
        };
      });
    }

    /** 这个会话是否有值得保存/恢复的内容（只看扫码是不够的） */
    hasContent() {
      return !!(
        this.scans.length ||
        Object.keys(this.manualQty).length ||
        Object.keys(this.manualFound).length ||
        Object.keys(this.manualNotes).length ||
        Object.keys(this.remarks).length ||
        this.pendingReview.length
      );
    }

    stats() {
      const serial = this.serialItems(); // 只含在库（不含在途）
      // 「应盘 / 已盘 / 进度」只统计在库且有串号的机器；
      // 在途（待入库）与无串号商品各自单独统计，不混进进度
      const transit = this.inTransitItems();
      const transitArrived = this.inTransitArrived();
      const found = serial.filter((it) => it.matchedCode || it.manualAt);
      const scanned = serial.filter((it) => it.matchedCode);
      const manual = this.manualSerial();
      const extraList = this.extras();
      const nonSerial = this.nonSerialItems();
      const nsRows = this.nonSerialRows();
      const nsCounted = nsRows.filter((r) => r.actual !== null);
      const bookNonSerial = nonSerial.reduce((a, it) => a + it.qty, 0);
      // 无串号商品的差异只比较「已经盘过的行」，未盘的行不参与，
      // 否则盘到一半会显示成大面积盘亏，容易误判。
      const nsBookCounted = nsCounted.reduce((a, r) => a + r.book, 0);
      const nsActual = nsCounted.reduce((a, r) => a + (r.actual || 0), 0);
      return {
        shouldCount: serial.length, // 应盘（有串号件数）
        foundCount: found.length, // 已盘（扫码 + 手工确认）
        scannedCount: scanned.length, // 其中：扫码盘到
        manualCount: manual.length, // 其中：手工确认
        missingCount: serial.length - found.length, // 未盘
        progress: serial.length ? Math.round((found.length / serial.length) * 1000) / 10 : 0,
        extraCount: extraList.length, // 表外码（去重）
        extraScans: extraList.reduce((a, r) => a + r.times, 0),
        dupCount: this.scans.filter((x) => x.status === DUP).length,
        totalScans: this.scans.length,
        nonSerialRows: nonSerial.length,
        nonSerialBook: bookNonSerial, // 无串号商品账面合计
        nonSerialCounted: nsCounted.length, // 已手工盘过的行数
        nonSerialBookCounted: nsBookCounted, // 已盘部分的账面数
        nonSerialActual: nsActual, // 已盘部分的实盘数
        nonSerialDiff: nsActual - nsBookCounted, // 已盘部分差异
        inTransitCount: transit.length,
        inTransitArrivedCount: transitArrived.length,
        bookTotal: this.items.reduce((a, it) => a + it.qty, 0), // 含在途
        bookOnHand: this.items.filter((it) => !it.inTransit).reduce((a, it) => a + it.qty, 0),
        bookSerial: serial.length,
      };
    }

    /* ---- 序列化（用于本地续盘） ---- */

    toJSON() {
      return {
        v: 2,
        schema: 2,
        sessionId: this.sessionId,
        companycode: this.companycode,
        bookVersion: this.bookVersion,
        bookFetchedAt: this.bookFetchedAt,
        date: this.date,
        storeId: this.storeId,
        storeName: this.storeName,
        startedAt: this.startedAt,
        pendingReview: this.pendingReview,
        scans: this.scans.map((r) => ({ code: r.code, ts: r.ts, note: r.note || '' })),
        manualQty: this.manualQty,
        remarks: this.remarks,
        manualFound: this.manualFound,
        manualNotes: this.manualNotes,
        autoQty: this.autoQty,
      };
    }

    /** 用保存的流水恢复盘点状态（先在新的库存数据上重放） */
    restore(data) {
      if (!data) return;
      this.startedAt = data.startedAt || this.startedAt;
      if (data.sessionId) this.sessionId = data.sessionId;
      if (data.companycode) this.companycode = data.companycode;
      if (data.bookVersion) this.bookVersion = data.bookVersion;
      if (data.bookFetchedAt) this.bookFetchedAt = data.bookFetchedAt;
      if (Array.isArray(data.pendingReview)) this.pendingReview = data.pendingReview.slice();
      this.manualQty = Object.assign(Object.create(null), data.manualQty || {});
      this.remarks = Object.assign(Object.create(null), data.remarks || {});
      this.manualFound = Object.assign(Object.create(null), data.manualFound || {});
      this.manualNotes = Object.assign(Object.create(null), data.manualNotes || {});
      this.autoQty = Object.assign(Object.create(null), data.autoQty || {});
      this.applyManualState();
      (data.scans || []).forEach((r) => {
        const res = this.scan(r.code, r.ts);
        if (res.record && r.note) res.record.note = r.note;
      });
      // 扫码重放可能把手工确认升级成扫码实证，同步一次
      this.applyManualState();
    }
  }

  /* ---------------- CSV 导出 ---------------- */

  function csvCell(v) {
    const t = v === null || v === undefined ? '' : String(v);
    return /[",\n\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  }

  function toCSV(rows) {
    return rows.map((r) => r.map(csvCell).join(',')).join('\r\n');
  }

  IC.core = {
    s,
    num,
    normCode,
    fmtTime,
    todayStr,
    normalizeItem,
    normalizeAll,
    assignUids,
    itemUid,
    transitTwinOf,
    bookRecord,
    bookFromItems,
    itemsFromBook,
    mergeInTransit,
    diffBooks,
    Stocktake,
    newSessionId,
    toCSV,
    STATUS: { OK, DUP, FOREIGN, UNKNOWN },
    STATUS_LABEL,
  };
})();
