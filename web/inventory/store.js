/* 库存盘点 - 会话持久化（冻结账面 + 可变状态分开存）
 * 归入 globalThis.IC.store
 *
 * 为什么要拆开：
 *  - 账面（book）在一次盘点里是不变的，建账/更新账面时才写一次，可能是几百 KB ~ 1MB；
 *  - 状态（state：扫码、实盘数、确认、备注）每次操作都要写，必须很小。
 * 合在一起会导致"每扫一次码就序列化并写入 1MB"，既卡顿又更容易触发配额上限。
 *
 * 存储键：
 *  ic.book.v2    当前会话的冻结账面
 *  ic.book.prev  上一版账面（更新账面时保留，可回滚）
 *  ic.state.v2   可变状态（含指向的 sessionId / bookVersion）
 *  ic.legacy.v1  从旧版本迁过来的原始数据（只保留备份，不参与恢复）
 */
globalThis.IC = globalThis.IC || {};
(function () {
  const IC = globalThis.IC;

  const KEYS = {
    book: 'ic.book.v2',
    bookPrev: 'ic.book.prev.v2',
    state: 'ic.state.v2',
    legacy: 'ic.legacy.v1',
    legacySession: 'ic.session.v1',
    cfg: 'ic.cfg.v2',
  };

  const status = {
    lastSaveAt: 0,
    lastError: '',
    lastKind: '', // book | state
  };

  function raw() {
    try {
      return globalThis.localStorage || null;
    } catch (e) {
      return null;
    }
  }

  /** 存储是否可用（有些环境禁用 localStorage） */
  function available() {
    const ls = raw();
    if (!ls) return false;
    try {
      ls.setItem('__ic_probe', '1');
      ls.removeItem('__ic_probe');
      return true;
    } catch (e) {
      return false;
    }
  }

  function readJSON(key) {
    const ls = raw();
    if (!ls) return null;
    try {
      const t = ls.getItem(key);
      return t ? JSON.parse(t) : null;
    } catch (e) {
      return null;
    }
  }

  /** 写：失败一定抛出（配额/禁用），由界面明确告知使用者 */
  function writeJSON(key, value) {
    const ls = raw();
    if (!ls) {
      const e = new Error('浏览器禁用了本地存储，无法保存盘点进度');
      e.storage = 'unavailable';
      throw e;
    }
    const text = JSON.stringify(value);
    try {
      ls.setItem(key, text);
      status.lastSaveAt = Date.now();
      status.lastError = '';
      return text.length;
    } catch (e) {
      const name = (e && e.name) || '';
      // 区分三种情况，界面提示要能指导下一步动作
      let kind = 'error';
      let msg = '保存失败：' + ((e && e.message) || '未知原因');
      if (name === 'QuotaExceededError' || name === 'NS_ERROR_DOM_QUOTA_REACHED') {
        kind = 'quota';
        msg = '本机存储空间不足，盘点进度没能保存';
      } else if (name === 'SecurityError' || name === 'InvalidStateError' || /denied|disabled|blocked/i.test((e && e.message) || '')) {
        kind = 'unavailable';
        msg = '浏览器禁用了本地存储（隐私模式或站点设置），盘点进度无法保存';
      }
      status.lastError = kind === 'quota' ? '配额不足' : kind === 'unavailable' ? '存储被禁用' : (e && e.message) || '写入失败';
      const err = new Error(msg);
      err.storage = kind;
      err.cause = e;
      throw err;
    }
  }

  function remove(key) {
    const ls = raw();
    if (!ls) return;
    try {
      ls.removeItem(key);
    } catch (e) {
      /* 忽略 */
    }
  }

  /* ---------------- 账面 ---------------- */

  /**
   * 保存冻结账面
   * @param {object} book {sessionId, companycode, storeId, storeName, date, version, fetchedAt, items:[...]}
   */
  function saveBook(book) {
    const payload = Object.assign({}, book, { savedAt: Date.now() });
    const size = writeJSON(KEYS.book, payload);
    status.lastKind = 'book';
    return size;
  }

  function loadBook() {
    return readJSON(KEYS.book);
  }

  /** 把当前账面挪到「上一版」并写入新账面（更新账面时用，可回滚） */
  function replaceBook(book) {
    const cur = loadBook();
    if (cur) writeJSON(KEYS.bookPrev, Object.assign({}, cur, { archivedAt: Date.now() }));
    return saveBook(book);
  }

  function loadPrevBook() {
    return readJSON(KEYS.bookPrev);
  }

  function clearBook() {
    remove(KEYS.book);
    remove(KEYS.bookPrev);
  }

  /* ---------------- 状态 ---------------- */

  function saveState(state) {
    const size = writeJSON(KEYS.state, Object.assign({}, state, { savedAt: Date.now() }));
    status.lastKind = 'state';
    return size;
  }

  function loadState() {
    return readJSON(KEYS.state);
  }

  function clearState() {
    remove(KEYS.state);
  }

  /** 状态是否属于给定的会话与账面版本 */
  function stateMatches(state, sessionId, bookVersion) {
    if (!state) return false;
    if (state.sessionId !== sessionId) return false;
    if (bookVersion !== undefined && state.bookVersion !== bookVersion) return false;
    return true;
  }

  /* ---------------- 旧版本迁移 ---------------- */

  /** 读取旧版（本书面账缺失）记录，读完挪到备份键，不再参与恢复 */
  function takeLegacy() {
    const old = readJSON(KEYS.legacySession);
    if (!old) return null;
    try {
      writeJSON(KEYS.legacy, { migratedAt: Date.now(), data: old });
      remove(KEYS.legacySession);
    } catch (e) {
      /* 备份失败也别删原数据 */
    }
    return old;
  }

  function loadLegacyBackup() {
    return readJSON(KEYS.legacy);
  }

  /* ---------------- 备份下载 / 用量 ---------------- */

  function bytesOf(key) {
    const ls = raw();
    if (!ls) return 0;
    try {
      const t = ls.getItem(key);
      return t ? t.length : 0;
    } catch (e) {
      return 0;
    }
  }

  function usage() {
    return {
      book: bytesOf(KEYS.book),
      bookPrev: bytesOf(KEYS.bookPrev),
      state: bytesOf(KEYS.state),
      legacy: bytesOf(KEYS.legacy),
    };
  }

  /** 会话备份（账面 + 状态 + 统计），存不下来时让使用者先下载保命 */
  function buildBackup(session) {
    return {
      kind: 'inventory-check-backup',
      schema: 2,
      exportedAt: new Date().toISOString(),
      session: session || null,
    };
  }

  function safeName(s) {
    return String(s || '').replace(/[\\/:*?"<>|]/g, '_');
  }

  IC.store = {
    KEYS,
    status,
    available,
    saveBook,
    loadBook,
    replaceBook,
    loadPrevBook,
    clearBook,
    saveState,
    loadState,
    clearState,
    stateMatches,
    takeLegacy,
    loadLegacyBackup,
    usage,
    buildBackup,
    safeName,
    readJSON,
    writeJSON,
    remove,
  };
})();
