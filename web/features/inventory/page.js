'use strict';

/* 库存盘点的同文档接入：库存引擎保持上游原样，这里只管理懒加载与扫码焦点。 */
const INV_PAGE_V = (() => {
  try {
    const src = (document.currentScript && document.currentScript.src) || '';
    const match = src.match(/[?&]v=(\d+)/);
    return match ? match[1] : '';
  } catch (e) { return ''; }
})();
let _invScriptsLoaded = false;

function focusInvScan() {
  const root = $('#subpanel-inventory');
  if (!root || !root.classList.contains('active')) return;
  const input = root.querySelector('#scan-input');
  const scanCard = root.querySelector('#scan-card');
  // 还没进扫码台就别抢焦点（设置表单还要点）
  if (input && scanCard && !scanCard.classList.contains('hidden')) {
    try { input.focus(); } catch (e) {}
  }
}

function loadInventoryAssets() {
  if (!_invScriptsLoaded) {
    _invScriptsLoaded = true;
    const version = INV_PAGE_V ? '?v=' + INV_PAGE_V : '';
    // 顺序有依赖，别调：core → api → store → xlsx → ui（async=false 保序）
    for (const name of ['core', 'api', 'store', 'xlsx', 'ui']) {
      const script = document.createElement('script');
      script.src = '/inventory/' + name + '.js' + version;
      script.async = false;
      document.body.appendChild(script);
    }
  }
  focusInvScan();
}

function onInventoryMouseMove() {
  focusInvScan();
}

function onInventoryKeyDown(event) {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  if (!$('#subpanel-inventory')?.classList.contains('active')) return;
  const tag = (event.target && event.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (event.key.length === 1) focusInvScan();
}

function mountInventoryPage() {
  $('#subpanel-inventory')?.addEventListener('mousemove', onInventoryMouseMove);
  document.addEventListener('keydown', onInventoryKeyDown);
}

function unmountInventoryPage() {
  $('#subpanel-inventory')?.removeEventListener('mousemove', onInventoryMouseMove);
  document.removeEventListener('keydown', onInventoryKeyDown);
}

registerPage('inventory', {
  mount: mountInventoryPage,
  load: loadInventoryAssets,
  unmount: unmountInventoryPage,
});
