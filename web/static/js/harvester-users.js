/**
 * harvester-ops — comptes du cluster Harvester (v1.31.0)
 *
 * Harvester modélise ses comptes en deux objets : un `User` porte le login
 * et l'activation, un `ClusterRoleBinding` ordinaire porte l'administration.
 * Ce panneau montre les deux ensemble, ce que l'UI Harvester ne fait pas
 * d'un seul regard.
 *
 * Ce qu'il ne fait PAS, et le dit : poser un mot de passe local. Celui-ci
 * vit dans un secret séparé, sous forme de clé dérivée de 32 octets avec
 * son sel, dont deviner l'algorithme poserait au mieux des comptes
 * incapables de se connecter.
 */
const HUsers = (() => {
  function tr(key, fallback) {
    return (window.i18n && i18n.t(key) !== key) ? i18n.t(key) : fallback;
  }
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function cluster() {
    return document.querySelector('#cluster-select')?.value || '';
  }

  async function refresh() {
    const body = document.querySelector('#husers-body');
    if (!body) return;
    const c = cluster();
    if (!c) { body.innerHTML = `<p class="form-hint">${esc(tr('husers.noCluster', 'Select a cluster first.'))}</p>`; return; }
    body.innerHTML = `<p class="form-hint">${esc(tr('common.loading', 'Loading...'))}</p>`;
    let d;
    try {
      const r = await fetch(`/api/harvester-users/${encodeURIComponent(c)}`);
      d = await r.json();
      if (!r.ok) {
        body.innerHTML = `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${
          esc(d.hint || d.error || 'error')}</div>`;
        return;
      }
    } catch (e) {
      body.innerHTML = `<div class="summary-bar bad">${esc(e.message)}</div>`;
      return;
    }
    if (d.unreachable) {
      body.innerHTML = `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${
        esc(tr('husers.unreachable', 'Cluster unreachable'))} <code>${esc(d.endpoint || '')}</code></div>`;
      return;
    }
    body.innerHTML = render(d);
    body.querySelectorAll('[data-act]').forEach(b =>
      b.addEventListener('click', () => act(b.dataset.uid, b.dataset.act, b.dataset.val)));
  }

  function render(d) {
    const rows = (d.users || []).map(u => `
      <tr${u.system ? ' class="huser-system"' : ''}>
        <td><code>${esc(u.username || '—')}</code>${
          u.system ? ` <span class="badge">${esc(tr('husers.system', 'system'))}</span>` : ''}</td>
        <td class="form-hint">${esc(u.display_name || '')}</td>
        <td>${u.enabled
          ? `<span class="badge ok">${esc(tr('husers.enabled', 'enabled'))}</span>`
          : `<span class="badge warn">${esc(tr('husers.disabled', 'disabled'))}</span>`}</td>
        <td>${u.is_admin
          ? `<span class="badge ok">${esc(tr('husers.admin', 'administrator'))}</span>`
          : '<span class="form-hint">—</span>'}</td>
        <td class="huser-actions">${u.system ? `<span class="form-hint">${
            esc(tr('husers.untouchable', 'not managed here'))}</span>` : `
          <button class="btn btn-sm btn-secondary tip" data-uid="${esc(u.id)}"
                  data-act="enabled" data-val="${u.enabled ? '0' : '1'}"
                  data-tip="${esc(tr('husers.tip.toggle', 'Enable or disable this account'))}">
            ${u.enabled ? esc(tr('husers.disable', 'Disable')) : esc(tr('husers.enable', 'Enable'))}
          </button>
          <button class="btn btn-sm ${u.is_admin ? 'btn-danger' : 'btn-secondary'} tip"
                  data-uid="${esc(u.id)}" data-act="admin" data-val="${u.is_admin ? '0' : '1'}"
                  data-tip="${esc(tr('husers.tip.admin', 'Grant or revoke cluster administration'))}">
            ${u.is_admin ? esc(tr('husers.revoke', 'Revoke admin')) : esc(tr('husers.grant', 'Make admin'))}
          </button>`}</td>
      </tr>`).join('');

    const orphans = (d.other_admins || []).filter(o => o.orphan);
    const groups = (d.other_admins || []).filter(o => !o.orphan);
    return `
      <table class="data-table">
        <thead><tr>
          <th>${esc(tr('husers.login', 'Login'))}</th>
          <th>${esc(tr('husers.name', 'Name'))}</th>
          <th>${esc(tr('husers.state', 'State'))}</th>
          <th>${esc(tr('husers.rights', 'Rights'))}</th>
          <th></th>
        </tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="empty-state">${
          esc(tr('husers.none', 'No account'))}</td></tr>`}</tbody>
      </table>
      ${orphans.length ? `
        <div class="summary-bar warn" style="margin-top:10px;">
          ${Icons.svg('warn', { size: 14 })} ${esc(tr('husers.orphans',
            'These accounts no longer exist but still hold cluster administration. Recreating an account with the same id would silently give it back.'))}
          <ul class="form-hint">${orphans.map(o =>
            `<li><code>${esc(o.name)}</code> — <code>${esc((o.bindings || []).join(', '))}</code></li>`).join('')}</ul>
        </div>` : ''}
      ${groups.length ? `
        <p class="form-hint" style="margin-top:10px;">${esc(tr('husers.groups',
          'Also holding administration, through an external identity provider:'))}
          ${groups.map(o => `<code>${esc(o.name)}</code>`).join(' ')}</p>` : ''}
      <p class="form-hint">${esc(tr('husers.serviceAccounts',
        'Service accounts holding administration:'))} ${d.service_account_admins || 0}</p>
      <p class="form-hint">${esc(tr('husers.noPasswords',
        'Creating a local account with a password is not offered here: Harvester stores it as a derived key whose scheme this console will not guess. Use the Harvester UI for that.'))}</p>`;
  }

  async function act(uid, field, value) {
    const c = cluster();
    if (!c || !uid) return;
    const body = {};
    body[field] = value === '1';
    if (field === 'admin' && value === '1'
        && !confirm(tr('husers.confirmGrant',
                       'Give this account full administration of the cluster?'))) return;
    try {
      const r = await fetch(`/api/harvester-users/${encodeURIComponent(c)}/${encodeURIComponent(uid)}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) {
        // Un refus explicite (409 sur un binding qu'on n'a pas posé) doit
        // se lire, pas disparaître.
        alert(d.hint || d.error || 'error');
        return;
      }
    } catch (e) { alert(e.message); return; }
    refresh();
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('.settings-tab[data-stab="husers"]')) setTimeout(refresh, 60);
    });
  }

  return { init, refresh };
})();

if (typeof window !== 'undefined') window.HUsers = HUsers;
document.addEventListener('DOMContentLoaded', HUsers.init);
