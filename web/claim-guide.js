/* 权益领取方式 —— 分类筛选 + 搜索 */
(function () {
  const tabs = Array.from(document.querySelectorAll('nav.tabs [data-cat]'));
  const cards = Array.from(document.querySelectorAll('.card[data-cat]'));
  const q = document.getElementById('q');
  const empty = document.getElementById('empty');
  let cat = 'all';

  function apply() {
    const key = (q && q.value || '').trim().toLowerCase();
    let shown = 0;
    cards.forEach((el) => {
      const okCat = cat === 'all' || el.dataset.cat === cat;
      const text = (el.dataset.search || el.textContent || '').toLowerCase();
      const okQ = !key || text.indexOf(key) >= 0;
      const on = okCat && okQ;
      el.classList.toggle('hidden', !on);
      if (on) shown += 1;
    });
    if (empty) empty.style.display = shown ? 'none' : 'block';
  }

  tabs.forEach((btn) => {
    btn.addEventListener('click', () => {
      tabs.forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      cat = btn.dataset.cat;
      apply();
    });
  });
  if (q) q.addEventListener('input', apply);

  // 深链 #phone / #tablet …
  const hash = (location.hash || '').replace('#', '');
  if (hash) {
    const t = tabs.find((b) => b.dataset.cat === hash);
    if (t) t.click();
  }
  apply();
})();
