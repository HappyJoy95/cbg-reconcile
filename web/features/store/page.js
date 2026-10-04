'use strict';

/* 门店人员状态页：仍由左下角“账号与人员”入口承载，数据读取和写入归 store 功能。 */

let staffRows = [];
let staffPageMounted = false;
let staffPageEpoch = 0;
let staffReadController = null;

function renderStaff(d) {
  // 区长/平台看到的是门店上报的只读状态表。
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
}

function renderStaffFromMail(d) {
  const box = $('#staff-list');
  const stores = d.stores || [];
  ['#btn-staff-all', '#btn-staff-none', '#btn-staff-save'].forEach((sel) => {
    const el = $(sel);
    if (el) el.hidden = true;
  });
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
      <div class="hint">这份是 <b>${esc(s.report_date || '')}</b> 的邮件里的${
        s.imported_at ? `（${esc(s.imported_at)} 收到）` : ''} —— 只有门店能改，这边只读</div>
      <div class="staff-list-inner">${(s.people || []).map((p) => `
        <div class="staff-row">
          <span class="staff-name">${p.active ? '' : '（已离职）'}${
            esc(p.real || '（没写姓名）')}</span>
          <span class="hint mono">${esc(p.account || '')}</span>
        </div>`).join('')}</div>
    </div>`).join('');
}

function setAllStaff(on) {
  $$('#staff-list [data-staff]').forEach((b) => { b.checked = on; });
  staffRows.forEach((p) => { p.active = on; });
  $('#staff-meta').textContent = `共 ${staffRows.length} 人 · 在职 ${on ? staffRows.length : 0} 人`;
}

function waitText(secs) {
  const n = Math.max(0, Math.round(Number(secs) || 0));
  if (n < 60) return `${n} 秒`;
  const m = n / 60;
  return `${Number.isInteger(m) ? m : m.toFixed(1)} 分钟`;
}

function staffChange(event) {
  const input = event.target;
  if (!input || !input.matches || !input.matches('[data-staff]')) return;
  const row = staffRows[Number(input.dataset.staff)];
  if (!row) return;
  row.active = input.checked;
  const n = staffRows.filter((p) => p.active).length;
  $('#staff-meta').textContent = `共 ${staffRows.length} 人 · 在职 ${n} 人`;
}

async function loadStaff() {
  if (!staffPageMounted) return;
  if (staffReadController) staffReadController.abort();
  const epoch = ++staffPageEpoch;
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  staffReadController = controller;
  const box = $('#staff-list');
  box.innerHTML = '<div class="hint">正在读云商的用户名单…</div>';
  try {
    const d = await api('/api/staff', controller ? { signal: controller.signal } : {});
    if (!staffPageMounted || epoch !== staffPageEpoch) return;
    if (!d.ok) {
      box.innerHTML = `<div class="banner warn">${esc(d.error || '读不到')}</div>`;
      return;
    }
    renderStaff(d);
  } catch (e) {
    if (!staffPageMounted || epoch !== staffPageEpoch || (e && e.name === 'AbortError')) return;
    box.innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
  } finally {
    if (staffReadController === controller) staffReadController = null;
  }
}

async function saveStaff() {
  const btn = $('#btn-staff-save');
  const msg = $('#staff-msg');
  const excluded = staffRows.filter((p) => !p.active).map((p) => p.account);
  const epoch = staffPageEpoch;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    const d = await api('/api/staff', { method: 'PUT', body: { excluded } });
    if (staffPageMounted && epoch === staffPageEpoch) {
      renderStaff(d);
      const secs = Number(d.report_in || 0);
      msg.innerHTML = '已保存 ' + new Date().toLocaleTimeString()
        + `（剔除 ${excluded.length} 人）`
        + (secs ? `　·　<b>${waitText(secs)}后自动上报</b>给区长和中台`
                  + '<span class="hint">（这期间再点保存也只会发一封）</span>' : '');
      toast(secs ? `已保存，${secs} 秒后自动上报` : '人员设置已保存', 'ok');
    }
  } catch (e) {
    if (staffPageMounted && epoch === staffPageEpoch) {
      msg.textContent = '保存失败：' + e.message;
      toast('保存失败：' + e.message, 'bad');
    }
  } finally {
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

function staffAllClick() { setAllStaff(true); }
function staffNoneClick() { setAllStaff(false); }
function staffSaveClick() { saveStaff(); }

function mountStaffPage() {
  if (staffPageMounted) return;
  staffPageMounted = true;
  $('#btn-staff-all')?.addEventListener('click', staffAllClick);
  $('#btn-staff-none')?.addEventListener('click', staffNoneClick);
  $('#btn-staff-save')?.addEventListener('click', staffSaveClick);
  $('#staff-list')?.addEventListener('change', staffChange);
}

function unmountStaffPage() {
  staffPageMounted = false;
  staffPageEpoch += 1;
  if (staffReadController) staffReadController.abort();
  staffReadController = null;
  $('#btn-staff-all')?.removeEventListener('click', staffAllClick);
  $('#btn-staff-none')?.removeEventListener('click', staffNoneClick);
  $('#btn-staff-save')?.removeEventListener('click', staffSaveClick);
  $('#staff-list')?.removeEventListener('change', staffChange);
}

async function loadAccountPage() {
  renderAccount();
  await loadStaff();
}

registerPage('account', {
  mount: mountStaffPage,
  load: loadAccountPage,
  unmount: unmountStaffPage,
});
