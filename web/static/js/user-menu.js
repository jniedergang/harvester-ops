/**
 * harvester-ops — menu du compte (v1.56.0)
 *
 * En haut à droite, à côté des réglages : qui est connecté, comment (Rancher,
 * compte local, console ouverte), son rôle sur la console, ce que les
 * clusters voient de lui, la fin de sa session, et la déconnexion.
 *
 * Se déconnecter d'un compte LOCAL : l'authentification HTTP Basic n'a pas de
 * session, c'est le navigateur qui garde le mot de passe. On lui fait retenir
 * un compte factice (voir /logout/local), et il redemande le mot de passe.
 */
const UserMenu = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const escapeHtml = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const PSEUDO_USER = 'harvester-ops-signed-out';
  let who = null;

  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const AUTH = {
    rancher: () => tr('user.auth.rancher'),
    local: () => tr('user.auth.local'),
    open: () => tr('user.auth.open'),
  };
  const ROLE_DESC = {
    admin: () => tr('role.desc.admin'),
    operator: () => tr('role.desc.operator'),
    viewer: () => tr('role.desc.viewer'),
  };

  function clusterLine(d) {
    if (d.auth === 'rancher') return tr('role.cluster.rancher');
    if (!d.delegation_active) return tr('role.cluster.shared');
    if (d.cluster_user) return tr('role.cluster.as', { user: d.cluster_user });
    return tr('role.cluster.none');
  }

  function displayName(d) {
    return (d.session && d.session.name) || d.user || tr('user.anonymous');
  }

  function initials(name) {
    const parts = String(name).replace(/@.*$/, '').split(/[\s._-]+/).filter(Boolean);
    return ((parts[0] || '?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
  }

  function render() {
    const menu = $('#user-menu');
    if (!menu || !who) return;
    const d = who;
    const name = displayName(d);
    const facts = [
      [tr('user.role'), `<strong>${escapeHtml(d.role || 'admin')}</strong> · ${escapeHtml((ROLE_DESC[d.role] || ROLE_DESC.admin)())}`],
      [tr('user.cluster'), escapeHtml(clusterLine(d))],
    ];
    if (d.auth === 'rancher' && d.session && d.session.expires) {
      const until = new Date(d.session.expires * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' });
      facts.push([tr('user.session'), escapeHtml(tr('user.sessionUntil', { time: until }))]);
    }
    if (d.auth === 'rancher' && d.session && (d.session.groups || []).length) {
      facts.push([tr('user.groups'), d.session.groups.map(g => `<code>${escapeHtml(g)}</code>`).join(' ')]);
    }
    const canSignOut = d.auth === 'rancher' || d.auth === 'local';
    menu.innerHTML = `
      <div class="um-head">
        <span class="um-avatar" aria-hidden="true">${escapeHtml(initials(name))}</span>
        <div class="um-who">
          <div class="um-name">${escapeHtml(name)}</div>
          <div class="um-auth">${escapeHtml((AUTH[d.auth] || AUTH.open)())}${d.user && d.user !== name ? ` · <code>${escapeHtml(d.user)}</code>` : ''}</div>
        </div>
      </div>
      <dl class="um-facts">${facts.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join('')}</dl>
      <div class="um-actions">
        ${d.password_managed ? `<button type="button" class="um-item tip" role="menuitem" data-um="password" data-tip="${escapeHtml(tr('accounts.changeTip'))}">
          ${window.Icons ? Icons.svg('key', { size: 14 }) : ''}<span>${escapeHtml(tr('accounts.myPassword'))}</span></button>` : ''}
        ${d.role === 'admin' && d.auth !== 'open' ? `<button type="button" class="um-item tip" role="menuitem" data-um="console-accounts" data-tip="${escapeHtml(tr('accounts.menuTip'))}">
          ${window.Icons ? Icons.svg('user', { size: 14 }) : ''}<span>${escapeHtml(tr('accounts.title'))}</span></button>` : ''}
        <button type="button" class="um-item tip" role="menuitem" data-um="accounts" data-tip="${escapeHtml(tr('user.accountsTip'))}">
          ${window.Icons ? Icons.svg('shield', { size: 14 }) : ''}<span>${escapeHtml(tr('user.accounts'))}</span></button>
        <button type="button" class="um-item tip" role="menuitem" data-um="language" data-tip="${escapeHtml(tr('user.languageTip'))}">
          ${window.Icons ? Icons.svg('languages', { size: 14 }) : ''}<span>${escapeHtml(tr('user.language'))}</span></button>
        <button type="button" class="um-item tip" role="menuitem" data-um="versions" data-tip="${escapeHtml(tr('versions.openTip'))}">
          ${window.Icons ? Icons.svg('news', { size: 14 }) : ''}<span>${escapeHtml(tr('versions.open'))}</span></button>
        ${canSignOut ? `
        <button type="button" class="um-item um-signout tip" role="menuitem" data-um="signout" id="user-signout"
                data-tip="${escapeHtml(d.auth === 'rancher' ? tr('session.logoutTip') : tr('user.signoutLocalTip'))}">
          ${window.Icons ? Icons.svg('logout', { size: 14 }) : ''}<span>${escapeHtml(tr('user.signout'))}</span></button>`
        : `<p class="um-note">${escapeHtml(tr('user.openNote'))}</p>`}
      </div>`;
    const ini = $('#user-initials');
    if (ini) {
      ini.textContent = d.auth === 'open' ? '' : initials(name);
      ini.hidden = d.auth === 'open';
      $('#btn-user')?.classList.toggle('has-initials', d.auth !== 'open');
    }
  }

  function setOpen(open) {
    const menu = $('#user-menu');
    const btn = $('#btn-user');
    if (!menu || !btn) return;
    if (open) render();
    menu.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) menu.querySelector('.um-item')?.focus();
  }

  async function signOut() {
    const d = who || {};
    // v1.57.0 : une session (Rancher ou compte local) se ferme côté serveur ;
    // un navigateur qui s'était authentifié en HTTP Basic (avant la 1.57)
    // doit en plus oublier le mot de passe qu'il garde.
    try { await fetch('/logout', { method: 'POST' }); } catch { /* on part quand même */ }
    if (d.auth_via === 'basic') {
      await new Promise((resolve) => {
        const x = new XMLHttpRequest();
        x.open('GET', '/logout/local', true, PSEUDO_USER, String(Date.now()));
        x.onloadend = resolve;
        x.send();
      });
    }
    window.location.href = '/login?signed_out=1';
  }

  function act(what) {
    setOpen(false);
    if (what === 'password' && window.ConsoleAccounts) ConsoleAccounts.openPasswordChange();
    else if (what === 'console-accounts' && typeof Settings !== 'undefined') Settings.openTab('accounts');
    else if (what === 'accounts' && typeof Settings !== 'undefined') Settings.openTab('husers');
    else if (what === 'language' && typeof Settings !== 'undefined') Settings.openTab('language');
    else if (what === 'versions') window.Versions?.open();
    else if (what === 'signout') signOut();
  }

  function update(d) {
    who = d;
    render();
  }

  function init() {
    const btn = $('#btn-user');
    const menu = $('#user-menu');
    if (!btn || !menu) return;
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      setOpen(menu.hidden);
    });
    menu.addEventListener('click', (e) => {
      const it = e.target.closest('[data-um]');
      if (it) act(it.dataset.um);
    });
    document.addEventListener('click', (e) => {
      if (!menu.hidden && !e.target.closest('.user-menu-wrap')) setOpen(false);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !menu.hidden) { setOpen(false); btn.focus(); }
    });
  }

  return { init, update, signOut, open: () => setOpen(true), close: () => setOpen(false) };
})();
window.UserMenu = UserMenu;
