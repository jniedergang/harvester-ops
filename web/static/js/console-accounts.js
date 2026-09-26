/**
 * harvester-ops — les comptes de la console (v1.57.0)
 *
 * Réglages > Comptes de la console (administrateurs) : la liste des comptes
 * locaux, leur rôle, la création, la réinitialisation d'un mot de passe, la
 * suppression. Et, pour chacun, « Changer mon mot de passe » depuis le menu
 * du compte. Les comptes du htpasswd de l'installeur sont montrés mais se
 * gèrent sur le serveur.
 */
const ConsoleAccounts = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const ROLE = {
    viewer: () => tr('role.name.viewer'),
    operator: () => tr('role.name.operator'),
    admin: () => tr('role.name.admin'),
  };
  // Clés en toutes lettres : le contrôle de parité ne lit que des littéraux.
  const ERR = {
    'password-short': () => tr('accounts.err.short'),
    'password-name': () => tr('accounts.err.name'),
    'name-invalid': () => tr('accounts.err.invalid'),
    'name-taken': () => tr('accounts.err.taken'),
    'last-admin': () => tr('accounts.err.lastAdmin'),
    'self': () => tr('accounts.err.self'),
    'bad-current': () => tr('accounts.err.current'),
    'not-managed': () => tr('accounts.err.notManaged'),
  };
  const errText = (d, status) => (ERR[d.code] ? ERR[d.code]() : (d.error || `HTTP ${status}`));
  let data = null;

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(errText(d, r.status));
    return d;
  }

  function say(html, bad) {
    const el = $('#accounts-feedback');
    if (el) el.innerHTML = bad ? `<span class="res-error">${html}</span>` : html;
  }

  function roleSelect(a) {
    return `<select class="acc-role tip" data-user="${esc(a.name)}" data-tip="${esc(tr('accounts.roleTip'))}"
        ${a.source !== 'console' ? 'disabled' : ''}>
      ${data.roles.map(r => `<option value="${esc(r)}" ${r === a.role ? 'selected' : ''}>${esc((ROLE[r] || (() => r))())}</option>`).join('')}
    </select>`;
  }

  function render() {
    const body = $('#accounts-body');
    if (!body || !data) return;
    const rows = data.accounts.map(a => `
      <tr data-user="${esc(a.name)}">
        <td><strong>${esc(a.name)}</strong>${a.name === data.me ? ` <span class="badge info">${esc(tr('accounts.you'))}</span>` : ''}
          <div class="res-dim">${esc(a.source === 'console' ? tr('accounts.fromConsole') : tr('accounts.fromInstaller'))}</div></td>
        <td>${roleSelect(a)}</td>
        <td class="res-dim">${a.password_changed ? esc(new Date(a.password_changed * 1000).toLocaleDateString()) : '–'}</td>
        <td class="acc-actions">${a.source === 'console' ? `
          <button type="button" class="btn btn-sm btn-secondary acc-reset tip" data-user="${esc(a.name)}"
            data-tip="${esc(tr('accounts.resetTip'))}">${esc(tr('accounts.reset'))}</button>
          <button type="button" class="btn btn-sm btn-danger acc-delete tip" data-user="${esc(a.name)}"
            data-tip="${esc(tr('accounts.deleteTip'))}" ${a.name === data.me ? 'disabled' : ''}>${esc(tr('accounts.delete'))}</button>`
          : `<span class="res-dim tip" data-tip="${esc(tr('accounts.installerTip'))}">${esc(tr('accounts.onServer'))}</span>`}</td>
      </tr>`).join('');
    body.innerHTML = `
      <table class="data-table acc-table"><thead><tr>
        <th>${esc(tr('accounts.col.name'))}</th><th>${esc(tr('accounts.col.role'))}</th>
        <th>${esc(tr('accounts.col.password'))}</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      <h5 class="acc-new-title">${esc(tr('accounts.new'))}</h5>
      <form class="acc-new" autocomplete="off">
        <input type="text" name="name" required placeholder="${esc(tr('accounts.namePh'))}" class="tip"
               data-tip="${esc(tr('accounts.nameTip'))}" autocapitalize="none" spellcheck="false">
        <input type="password" name="password" required minlength="${data.min_password}" autocomplete="new-password"
               placeholder="${esc(tr('accounts.passwordPh', { n: data.min_password }))}" class="tip" data-tip="${esc(tr('accounts.passwordTip'))}">
        <select name="role" class="tip" data-tip="${esc(tr('accounts.roleTip'))}">
          ${data.roles.map(r => `<option value="${esc(r)}" ${r === 'viewer' ? 'selected' : ''}>${esc((ROLE[r] || (() => r))())}</option>`).join('')}
        </select>
        <button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('accounts.createTip'))}">${esc(tr('accounts.create'))}</button>
      </form>`;
  }

  async function load() {
    const body = $('#accounts-body');
    if (!body) return;
    body.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    try {
      data = await call('GET', '/api/users');
      render();
    } catch (e) {
      body.innerHTML = `<p class="form-hint">${esc(e.message)}</p>`;
    }
  }

  async function onClick(e) {
    const reset = e.target.closest('.acc-reset');
    const del = e.target.closest('.acc-delete');
    try {
      if (reset) {
        const user = reset.dataset.user;
        passwordModal({
          title: tr('accounts.resetTitle', { name: user }), current: false,
          send: (pw) => call('PATCH', `/api/users/${enc(user)}`, { password: pw }),
          done: tr('accounts.resetDone', { name: user }),
        });
      } else if (del) {
        const user = del.dataset.user;
        if (!confirm(tr('accounts.deleteConfirm', { name: user }))) return;
        await call('DELETE', `/api/users/${enc(user)}`);
        say(esc(tr('accounts.deleted', { name: user })));
        load();
      }
    } catch (err) {
      say(esc(err.message), true);
    }
  }

  async function onChange(e) {
    const sel = e.target.closest('.acc-role');
    if (!sel) return;
    try {
      await call('PATCH', `/api/users/${enc(sel.dataset.user)}`, { role: sel.value });
      say(esc(tr('accounts.roleDone', { name: sel.dataset.user, role: (ROLE[sel.value] || (() => sel.value))() })));
    } catch (err) {
      say(esc(err.message), true);
      load();
    }
  }

  async function onSubmit(e) {
    const form = e.target.closest('.acc-new');
    if (!form) return;
    e.preventDefault();
    const f = new FormData(form);
    try {
      await call('POST', '/api/users', { name: f.get('name'), password: f.get('password'), role: f.get('role') });
      say(esc(tr('accounts.created', { name: f.get('name') })));
      load();
    } catch (err) {
      say(esc(err.message), true);
    }
  }

  // -- saisir un mot de passe (le sien, ou celui d'un compte à réinitialiser) ------
  function passwordModal({ title, current, send, done }) {
    const old = $('#my-password-modal');
    if (old) old.remove();
    const m = document.createElement('div');
    m.className = 'modal-overlay active';
    m.id = 'my-password-modal';
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    m.innerHTML = `
      <div class="modal my-password">
        <div class="modal-header"><h3>${esc(title)}</h3>
          <button class="btn-close" data-x="close" title="${esc(tr('common.close'))}" aria-label="${esc(tr('common.close'))}">×</button></div>
        <form class="modal-body login-form" autocomplete="off">
          ${current ? `<label class="login-field"><span>${esc(tr('accounts.current'))}</span>
            <input type="password" name="current" required autocomplete="current-password"></label>` : ''}
          <label class="login-field"><span>${esc(tr('accounts.newPassword'))}</span>
            <input type="password" name="new" required minlength="12" autocomplete="new-password"></label>
          <label class="login-field"><span>${esc(tr('setup.confirm'))}</span>
            <input type="password" name="confirm" required minlength="12" autocomplete="new-password"></label>
          <p class="login-note">${esc(tr('setup.rule'))}</p>
          <div class="my-password-msg"></div>
          <button type="submit" class="btn btn-primary tip" data-tip="${esc(tr('accounts.changeTip'))}">${esc(tr('accounts.change'))}</button>
        </form>
      </div>`;
    document.body.appendChild(m);
    const close = () => m.remove();
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-x="close"]')) close(); });
    m.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    m.querySelector('form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      const msg = m.querySelector('.my-password-msg');
      if (f.get('new') !== f.get('confirm')) { msg.innerHTML = `<span class="res-error">${esc(tr('setup.err.mismatch'))}</span>`; return; }
      try {
        await send(f.get('new'), f.get('current'));
        msg.innerHTML = esc(done);
        say(esc(done));
        setTimeout(close, 1200);
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    m.querySelector('input').focus();
  }

  function openPasswordChange() {
    passwordModal({
      title: tr('accounts.myPassword'), current: true,
      send: (pw, cur) => call('POST', '/api/me/password', { current: cur, new: pw }),
      done: tr('accounts.changed'),
    });
  }

  function init() {
    const pane = $('#stab-accounts');
    if (!pane) return;
    pane.addEventListener('click', onClick);
    pane.addEventListener('change', onChange);
    pane.addEventListener('submit', onSubmit);
    document.querySelector('.settings-tab[data-stab="accounts"]')?.addEventListener('click', load);
  }

  document.addEventListener('DOMContentLoaded', init);
  return { load, openPasswordChange };
})();
window.ConsoleAccounts = ConsoleAccounts;
