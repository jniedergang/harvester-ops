/**
 * harvester-ops : Réglages > Connexion par Rancher (v1.79.0)
 *
 * Les Rancher par lesquels on se connecte à la console se règlent ici, à
 * chaud, plusieurs possibles : ajout, modification, suppression, test de
 * connexion, enregistrement de l'authentification unique (SSO) dans Rancher
 * par un administrateur de ce Rancher, et installation du chart Harvester
 * RBAC suivie comme une action dans le dock.
 *
 * Les identifiants d'administrateur ne servent qu'à la requête en cours : ils
 * ne restent ni dans la page (champs vidés dès l'envoi) ni dans le navigateur
 * (aucun stockage local). Ceux du chart RBAC restent en mémoire le temps de
 * la fenêtre, pour installer après avoir lu l'état, et sont oubliés à sa
 * fermeture.
 *
 * API : /api/rancher/servers (GET, POST), /api/rancher/servers/<id> (PUT,
 * DELETE), .../test, .../sso/register, .../sso/unregister, .../rbac/status,
 * .../rbac/install (202 + action_id).
 */
const RancherServers = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n) => (window.Icons ? Icons.svg(n, { size: 14 }) : '');
  // Clés en toutes lettres : le contrôle de parité ne lit que des littéraux.
  const ROLE = {
    viewer: () => tr('role.name.viewer'),
    operator: () => tr('role.name.operator'),
    admin: () => tr('role.name.admin'),
  };
  const roleName = (r) => (ROLE[r] ? ROLE[r]() : r);
  let data = null;

  async function call(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  function byId(id) {
    return ((data && data.servers) || []).find(s => s.id === id) || null;
  }

  function say(html, bad) {
    const el = $('#rsrv-feedback');
    if (el) el.innerHTML = bad ? `<span class="res-error">${html}</span>` : html;
  }

  // -- la liste -----------------------------------------------------------------
  function card(s) {
    const id = esc(s.id);
    const origin = s.origin === 'config' ? 'config' : 'console';
    const ro = !s.editable;
    const dis = ro ? ' disabled aria-disabled="true"' : '';
    const sso = s.sso || {};
    const tls = s.insecure ? tr('rsrv.tls.insecure') : (s.has_ca ? tr('rsrv.tls.ca') : tr('rsrv.tls.system'));
    const mutating = `
      <button type="button" class="btn btn-sm btn-secondary tip" data-rsrv="edit" data-id="${id}"
        data-tip="${esc(tr('rsrv.editTip'))}"${dis}>${icon('edit')} ${esc(tr('rsrv.edit'))}</button>
      <button type="button" class="btn btn-sm btn-secondary tip" data-rsrv="${sso.enabled ? 'sso-off' : 'sso-on'}" data-id="${id}"
        data-tip="${esc(sso.enabled ? tr('rsrv.ssoOffTip') : tr('rsrv.ssoOnTip'))}"${dis}>${icon(sso.enabled ? 'unlock' : 'lock')} ${esc(sso.enabled ? tr('rsrv.ssoOff') : tr('rsrv.ssoOn'))}</button>
      <button type="button" class="btn btn-sm btn-danger tip" data-rsrv="delete" data-id="${id}"
        data-tip="${esc(tr('rsrv.deleteTip'))}"${dis}>${icon('trash')} ${esc(tr('rsrv.delete'))}</button>`;
    return `
      <div class="cluster-card rsrv-card" data-id="${id}">
        <div class="cluster-card-head">
          <div>
            <h5>${esc(s.label)} <span class="badge cluster-origin tip" data-origin="${origin}"
              data-tip="${esc(origin === 'config' ? tr('rsrv.origin.configTip') : tr('rsrv.origin.consoleTip'))}">${esc(origin === 'config' ? tr('clusters.origin.config') : tr('clusters.origin.console'))}</span></h5>
            <div class="form-hint"><code>${esc(s.url)}</code></div>
            <div class="rsrv-meta">
              <span class="badge ${sso.enabled ? 'ok' : ''} tip rsrv-sso" data-on="${sso.enabled ? '1' : '0'}"
                data-tip="${esc(sso.enabled ? tr('rsrv.ssoStateOnTip', { id: sso.client_id || '' }) : tr('rsrv.ssoStateOffTip'))}">${esc(sso.enabled ? tr('rsrv.ssoStateOn') : tr('rsrv.ssoStateOff'))}</span>
              <span class="badge ${s.direct_enabled ? 'ok' : ''} tip rsrv-direct" data-on="${s.direct_enabled ? '1' : '0'}"
                data-tip="${esc(tr('rsrv.directStateTip'))}">${esc(s.direct_enabled ? tr('rsrv.directOn') : tr('rsrv.directOff'))}</span>
              <span class="badge tip rsrv-role" data-tip="${esc(tr('rsrv.roleStateTip'))}">${esc(tr('rsrv.roleState', { role: roleName(s.default_role) }))}</span>
              <span class="badge tip" data-tip="${esc(tr('rsrv.hoursStateTip'))}">${esc(tr('rsrv.hoursState', { n: s.session_hours }))}</span>
              <span class="badge ${s.insecure ? 'warn' : ''} tip" data-tip="${esc(tr('rsrv.tlsStateTip'))}">${esc(tls)}</span>
            </div>
          </div>
          <div class="cluster-card-actions">
            <button type="button" class="btn btn-sm btn-secondary tip" data-rsrv="test" data-id="${id}"
              data-tip="${esc(tr('rsrv.testTip'))}">${icon('test')} ${esc(tr('rsrv.test'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip" data-rsrv="rbac" data-id="${id}"
              data-tip="${esc(tr('rsrv.rbacTip'))}">${icon('shield')} ${esc(tr('rsrv.rbac'))}</button>
            ${ro ? `<span class="cluster-readonly rsrv-readonly tip" data-tip="${esc(tr('rsrv.readOnlyTip'))}">${mutating}</span>` : mutating}
          </div>
        </div>
        <div class="cluster-card-result rsrv-result" data-id="${id}" hidden></div>
      </div>`;
  }

  function render() {
    const out = $('#rsrv-list');
    if (!out || !data) return;
    const list = data.servers || [];
    out.innerHTML = list.length
      ? list.map(card).join('')
      : `<p class="form-hint rsrv-empty">${esc(tr('rsrv.empty'))}</p>`;
    if (window.i18n) i18n.applyTranslations();
  }

  async function load() {
    const out = $('#rsrv-list');
    if (!out) return;
    out.innerHTML = `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    try {
      data = await call('GET', '/api/rancher/servers');
      render();
    } catch (e) {
      out.innerHTML = `<p class="form-hint res-error">${esc(e.message)}</p>`;
    }
  }

  function resultEl(id) {
    const el = document.querySelector(`.rsrv-result[data-id="${CSS.escape(id)}"]`);
    if (el) el.hidden = false;
    return el;
  }

  // -- tester ---------------------------------------------------------------------
  async function test(id) {
    const el = resultEl(id);
    if (!el) return;
    el.innerHTML = esc(tr('common.loading'));
    try {
      const d = await call('POST', `/api/rancher/servers/${enc(id)}/test`);
      if (!d.ok) {
        el.innerHTML = `<div class="summary-bar bad">${icon('fail')} ${esc(d.error || tr('rsrv.testFailed'))}</div>`;
        return;
      }
      const provs = (d.providers || []).map(p => `<span class="badge ${p.enabled ? 'ok' : ''} tip rsrv-provider"
          data-tip="${esc(p.password ? tr('rsrv.providerPasswordTip') : tr('rsrv.providerSsoTip'))}">${esc(p.id)}${p.enabled ? '' : ' (' + esc(tr('rsrv.providerOff')) + ')'}</span>`).join(' ');
      el.innerHTML = `<div class="summary-bar ok">${icon('ok')} ${esc(tr('rsrv.testOk', { version: d.version || '?' }))}</div>
        <div class="rsrv-providers"><span class="form-hint">${esc(tr('rsrv.providers'))}</span> ${provs || esc(tr('rsrv.noProvider'))}</div>`;
    } catch (e) {
      el.innerHTML = `<div class="summary-bar bad">${icon('fail')} ${esc(e.message)}</div>`;
    }
  }

  // -- supprimer --------------------------------------------------------------------
  async function remove(id) {
    const s = byId(id);
    if (!s) return;
    if (!confirm(tr('rsrv.deleteConfirm', { name: s.label }))) return;
    try {
      await call('DELETE', `/api/rancher/servers/${enc(id)}`);
      say(esc(tr('rsrv.deleted', { name: s.label })));
      load();
    } catch (e) {
      say(esc(e.message), true);
    }
  }

  // -- fenêtre modale (au-dessus des réglages, comme celles des comptes) ------------
  function modal(id, title, bodyHtml, onClose) {
    const old = document.getElementById(id);
    if (old) old.remove();
    const m = document.createElement('div');
    m.className = 'modal-overlay active rsrv-modal';
    m.id = id;
    m.setAttribute('role', 'dialog');
    m.setAttribute('aria-modal', 'true');
    m.innerHTML = `
      <div class="modal rsrv-window">
        <div class="modal-header"><h3>${esc(title)}</h3>
          <button type="button" class="btn-close tip" data-x="close" data-tip="${esc(tr('common.close'))}" aria-label="${esc(tr('common.close'))}">×</button></div>
        <div class="modal-body">${bodyHtml}</div>
      </div>`;
    document.body.appendChild(m);
    const close = () => { m.remove(); if (onClose) onClose(); };
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-x="close"]')) close(); });
    m.addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } });
    return { el: m, close };
  }

  // -- ajouter / modifier -----------------------------------------------------------
  function openForm(id) {
    const s = id ? byId(id) : null;
    const roles = ['viewer', 'operator', 'admin'];
    const v = s || { label: '', url: 'https://', insecure: false, has_ca: false, default_role: 'viewer', session_hours: 8, direct_enabled: true };
    const w = modal('rsrv-form-modal', s ? tr('rsrv.editTitle', { name: s.label }) : tr('rsrv.addTitle'), `
      <form class="login-form rsrv-form" autocomplete="off">
        <label class="login-field"><span>${esc(tr('rsrv.f.label'))}</span>
          <input type="text" name="label" required maxlength="60" value="${esc(v.label)}" class="tip" data-tip="${esc(tr('rsrv.f.labelTip'))}"></label>
        <label class="login-field"><span>${esc(tr('rsrv.f.url'))}</span>
          <input type="url" name="url" required pattern="https?://.+" value="${esc(v.url)}" class="tip" data-tip="${esc(tr('rsrv.f.urlTip'))}"
            autocapitalize="none" spellcheck="false"></label>
        <label class="login-field"><span>${esc(tr('rsrv.f.ca'))}</span>
          <textarea name="ca" rows="5" spellcheck="false" class="tip rsrv-ca" data-tip="${esc(tr('rsrv.f.caTip'))}"
            placeholder="-----BEGIN CERTIFICATE-----" ${v.insecure ? 'disabled' : ''}></textarea>
          ${v.has_ca ? `<small class="form-hint rsrv-ca-kept">${esc(tr('rsrv.f.caKept'))}</small>` : ''}</label>
        <label class="opt-row tip" data-tip="${esc(tr('rsrv.f.insecureTip'))}">
          <input type="checkbox" name="insecure" ${v.insecure ? 'checked' : ''}> <span>${esc(tr('rsrv.f.insecure'))}</span></label>
        <div class="rsrv-form-grid">
          <label class="login-field"><span>${esc(tr('rsrv.f.role'))}</span>
            <select name="default_role" class="tip" data-tip="${esc(tr('rsrv.f.roleTip'))}">
              ${roles.map(r => `<option value="${r}" ${r === v.default_role ? 'selected' : ''}>${esc(roleName(r))}</option>`).join('')}
            </select></label>
          <label class="login-field"><span>${esc(tr('rsrv.f.hours'))}</span>
            <input type="number" name="session_hours" min="1" max="24" required value="${esc(v.session_hours)}" class="tip" data-tip="${esc(tr('rsrv.f.hoursTip'))}"></label>
        </div>
        <label class="opt-row tip" data-tip="${esc(tr('rsrv.f.directTip'))}">
          <input type="checkbox" name="direct_enabled" ${v.direct_enabled ? 'checked' : ''}> <span>${esc(tr('rsrv.f.direct'))}</span></label>
        <div class="rsrv-msg"></div>
        <div class="rsrv-form-actions">
          <button type="submit" class="btn btn-primary btn-sm tip" data-tip="${esc(tr('rsrv.f.saveTip'))}">${icon('save')} ${esc(tr('rsrv.f.save'))}</button>
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="close" data-tip="${esc(tr('rsrv.f.cancelTip'))}">${esc(tr('rsrv.f.cancel'))}</button>
        </div>
      </form>`);
    const form = w.el.querySelector('form');
    const ca = form.querySelector('[name="ca"]');
    form.querySelector('[name="insecure"]').addEventListener('change', (e) => { ca.disabled = e.target.checked; });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = w.el.querySelector('.rsrv-msg');
      const hours = parseInt(form.session_hours.value, 10);
      if (!(hours >= 1 && hours <= 24)) {
        msg.innerHTML = `<span class="res-error">${esc(tr('rsrv.f.hoursBad'))}</span>`;
        return;
      }
      const body = {
        label: form.label.value.trim(),
        url: form.url.value.trim(),
        insecure: form.insecure.checked,
        default_role: form.default_role.value,
        session_hours: hours,
        direct_enabled: form.direct_enabled.checked,
      };
      // un CA vide en modification garde celui qui est enregistré
      if (!body.insecure && ca.value.trim()) body.ca = ca.value.trim();
      try {
        if (s) await call('PUT', `/api/rancher/servers/${enc(s.id)}`, body);
        else await call('POST', '/api/rancher/servers', body);
        w.close();
        say(esc(s ? tr('rsrv.saved', { name: body.label }) : tr('rsrv.added', { name: body.label })));
        load();
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    form.label.focus();
  }

  // -- identifiants d'administrateur (SSO et chart RBAC) ----------------------------
  function credsHtml(submitLabel, submitTip) {
    return `
      <p class="login-note rsrv-creds-note">${esc(tr('rsrv.credsNote'))}</p>
      <form class="login-form rsrv-creds" autocomplete="off">
        <label class="login-field"><span>${esc(tr('rsrv.c.provider'))}</span>
          <select name="provider" class="tip" data-tip="${esc(tr('rsrv.c.providerTip'))}">
            <option value="local">local</option></select></label>
        <label class="login-field"><span>${esc(tr('rsrv.c.user'))}</span>
          <input type="text" name="admin_user" required autocomplete="off" autocapitalize="none" spellcheck="false"
            class="tip" data-tip="${esc(tr('rsrv.c.userTip'))}"></label>
        <label class="login-field"><span>${esc(tr('rsrv.c.password'))}</span>
          <input type="password" name="admin_password" required autocomplete="new-password"
            class="tip" data-tip="${esc(tr('rsrv.c.passwordTip'))}"></label>
        <button type="submit" class="btn btn-primary btn-sm tip" data-tip="${esc(submitTip)}">${esc(submitLabel)}</button>
      </form>
      <div class="rsrv-msg"></div>`;
  }

  // Les fournisseurs à mot de passe de ce Rancher, lus par le test de connexion
  async function fillProviders(form, id) {
    try {
      const d = await call('POST', `/api/rancher/servers/${enc(id)}/test`);
      const pw = (d.providers || []).filter(p => p.password && p.enabled);
      if (!pw.length || !document.body.contains(form)) return;
      const sel = form.querySelector('[name="provider"]');
      const cur = sel.value;
      sel.innerHTML = pw.map(p => `<option value="${esc(p.id)}" ${p.id === cur ? 'selected' : ''}>${esc(p.id)}</option>`).join('');
    } catch { /* on garde « local » */ }
  }

  // Lit les identifiants puis vide le champ du mot de passe : il ne reste pas
  // dans la page après l'envoi.
  function takeCreds(form) {
    const creds = {
      admin_user: form.admin_user.value.trim(),
      admin_password: form.admin_password.value,
      provider: form.provider.value,
    };
    form.admin_password.value = '';
    return creds;
  }

  function openSso(id, enable) {
    const s = byId(id);
    if (!s) return;
    const w = modal('rsrv-sso-modal',
      enable ? tr('rsrv.ssoOnTitle', { name: s.label }) : tr('rsrv.ssoOffTitle', { name: s.label }),
      `<p class="form-hint">${esc(enable ? tr('rsrv.ssoOnIntro') : tr('rsrv.ssoOffIntro'))}</p>`
        + credsHtml(enable ? tr('rsrv.ssoOn') : tr('rsrv.ssoOff'), enable ? tr('rsrv.ssoOnTip') : tr('rsrv.ssoOffTip')));
    const form = w.el.querySelector('form');
    fillProviders(form, id);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = w.el.querySelector('.rsrv-msg');
      const creds = takeCreds(form);
      msg.innerHTML = esc(tr('common.loading'));
      try {
        const d = await call('POST', `/api/rancher/servers/${enc(id)}/sso/${enable ? 'register' : 'unregister'}`, creds);
        msg.innerHTML = `<span class="rsrv-done">${icon('ok')} ${esc(enable
          ? tr('rsrv.ssoOnDone', { id: d.client_id || '' }) : tr('rsrv.ssoOffDone'))}</span>`;
        form.hidden = true;
        load();
        setTimeout(w.close, 1500);
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    form.admin_user.focus();
  }

  // -- chart Harvester RBAC -----------------------------------------------------------
  function rbacStatusHtml(st) {
    const yes = tr('rsrv.yes');
    const no = tr('rsrv.no');
    return `
      <table class="perm-table rsrv-rbac-status">
        <tr><td>${esc(tr('rsrv.rbac.installed'))}</td><td data-k="installed">${esc(st.installed ? yes : no)}</td></tr>
        <tr><td>${esc(tr('rsrv.rbac.version'))}</td><td data-k="version">${esc(st.version || '-')}</td></tr>
        <tr><td>${esc(tr('rsrv.rbac.available'))}</td><td data-k="available">${esc(st.available_version || '-')}</td></tr>
        <tr><td>${esc(tr('rsrv.rbac.compatible'))}</td><td data-k="compatible">${esc(st.compatible ? yes : no)}</td></tr>
      </table>
      ${st.reason ? `<p class="form-hint rsrv-rbac-reason">${esc(st.reason)}</p>` : ''}`;
  }

  function openRbac(id) {
    const s = byId(id);
    if (!s) return;
    // identifiants gardés en mémoire le temps de la fenêtre, jamais dans la page
    let creds = null;
    const w = modal('rsrv-rbac-modal', tr('rsrv.rbacTitle', { name: s.label }),
      `<p class="form-hint">${esc(tr('rsrv.rbacIntro'))}</p>`
        + credsHtml(tr('rsrv.rbac.check'), tr('rsrv.rbac.checkTip'))
        + '<div class="rsrv-rbac-body"></div>',
      () => { creds = null; });
    const form = w.el.querySelector('form');
    const msg = w.el.querySelector('.rsrv-msg');
    const body = w.el.querySelector('.rsrv-rbac-body');
    fillProviders(form, id);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      creds = takeCreds(form);
      msg.innerHTML = esc(tr('common.loading'));
      body.innerHTML = '';
      try {
        const st = await call('POST', `/api/rancher/servers/${enc(id)}/rbac/status`, creds);
        msg.innerHTML = '';
        form.hidden = true;
        const upToDate = st.installed && st.version && st.version === st.available_version;
        const canInstall = st.compatible && !upToDate;
        let installTip = tr('rsrv.rbac.installTip');
        if (upToDate) installTip = tr('rsrv.rbac.upToDateTip');
        else if (!canInstall) installTip = tr('rsrv.rbac.incompatibleTip');
        body.innerHTML = rbacStatusHtml(st) + `
          <div class="rsrv-form-actions">
            <button type="button" class="btn btn-primary btn-sm tip" data-rsrv-rbac="install" ${canInstall ? '' : 'disabled'}
              data-tip="${esc(installTip)}">${icon('install')} ${esc(st.installed ? tr('rsrv.rbac.upgrade') : tr('rsrv.rbac.install'))}</button>
          </div>
          <div class="rsrv-rbac-progress"></div>`;
      } catch (err) {
        creds = null;
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    body.addEventListener('click', async (e) => {
      const btn = e.target.closest('[data-rsrv-rbac="install"]');
      if (!btn || !creds) return;
      btn.disabled = true;
      const prog = body.querySelector('.rsrv-rbac-progress');
      const sent = creds;
      creds = null;
      try {
        const d = await call('POST', `/api/rancher/servers/${enc(id)}/rbac/install`, sent);
        const done = tr('rsrv.rbac.done', { name: s.label });
        if (window.VMActions && VMActions.follow) VMActions.follow(d.action_id, prog, done);
        else {
          if (window.Dock && Dock.poll) Dock.poll();
          prog.textContent = tr('rsrv.rbac.started', { id: d.action_id });
        }
      } catch (err) {
        prog.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    form.admin_user.focus();
  }

  // -- branchements ---------------------------------------------------------------------
  function onClick(e) {
    const btn = e.target.closest('[data-rsrv]');
    if (!btn || btn.disabled) return;
    const id = btn.dataset.id;
    switch (btn.dataset.rsrv) {
      case 'add': openForm(null); break;
      case 'refresh': load(); break;
      case 'edit': openForm(id); break;
      case 'delete': remove(id); break;
      case 'test': test(id); break;
      case 'sso-on': openSso(id, true); break;
      case 'sso-off': openSso(id, false); break;
      case 'rbac': openRbac(id); break;
      default: break;
    }
  }

  function init() {
    const pane = $('#stab-rancher');
    if (!pane) return;
    pane.addEventListener('click', onClick);
    document.querySelector('.settings-tab[data-stab="rancher"]')?.addEventListener('click', load);
  }

  document.addEventListener('DOMContentLoaded', init);
  return { load, openForm, openSso, openRbac };
})();
window.RancherServers = RancherServers;
