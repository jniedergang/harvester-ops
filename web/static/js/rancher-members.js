/**
 * harvester-ops : les membres Rancher d'un cluster et de ses projets (v1.73.0)
 *
 * Rancher donne les droits sur un cluster ou un projet par des liaisons de
 * rôle (utilisateur ou groupe, rôle du contexte). Ce composant les montre
 * avec leur fournisseur (local, Keycloak...), en ajoute après une recherche
 * dans les utilisateurs et groupes que Rancher connaît, et en retire ; tout
 * passe par Rancher avec le jeton de la personne. Les comptes système de
 * Rancher et le dernier propriétaire ne se retirent pas.
 */
const RancherMembers = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    return d;
  }

  // 2.8.0 : `child` (`ns/nom`) = un cluster enfant importé dans le Rancher
  // de la session (scope cluster seul) ; le serveur vérifie la marque
  function query(o) {
    if (o.scope === 'project') return `?scope=project&project=${enc(o.project)}`;
    return o.child ? `?scope=cluster&child=${enc(o.child)}` : '?scope=cluster';
  }
  const target = (o) => Object.assign({ scope: o.scope, project: o.project }, o.child ? { child: o.child } : {});

  /** Monte la liste des membres dans `host` ; o = {cluster, scope, project, child, title}.
   * `note` : le message de la dernière action, gardé à travers la relecture
   * (vu en réel : il disparaissait avec la liste qu'on redessinait). */
  async function render(host, o, note) {
    host.innerHTML = `<div class="rm-box"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
    let d;
    try {
      d = await call('GET', `/api/rancher-members/${enc(o.cluster)}${query(o)}`);
    } catch (err) {
      host.innerHTML = `<p class="res-error">${esc(err.message)}</p>`;
      return;
    }
    if (!d.managed) {
      host.innerHTML = `<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('info')} ${esc(tr('rm.local'))}</div></div>`;
      return;
    }
    const box = document.createElement('div');
    box.className = 'rm-box';
    box.innerHTML = `${o.title ? `<h4 class="hs-sub">${esc(o.title)}</h4>` : ''}
      <table class="data-table rm-table"><thead><tr><th>${esc(tr('rm.col.who'))}</th><th>${esc(tr('rm.col.provider'))}</th>
        <th>${esc(tr('rm.col.role'))}</th><th></th></tr></thead><tbody>
      ${d.members.map(m => `<tr data-rm="${esc(m.id)}">
        <td>${icon(m.kind === 'group' ? 'user' : 'user', 12)} <strong>${esc(m.name)}</strong>${m.login ? ` <span class="res-dim">${esc(m.login)}</span>` : ''}
          ${m.kind === 'group' ? ` <span class="badge">${esc(tr('rm.group'))}</span>` : ''}${m.system ? ` <span class="badge tip" data-tip="${esc(tr('rm.t.system'))}">${esc(tr('rm.system'))}</span>` : ''}</td>
        <td><code>${esc(m.provider || '–')}</code></td><td>${esc(m.role_name)}</td>
        <td class="nsw-acts"><button type="button" class="btn-icon-sm tip" data-rm-act="remove" ${m.system ? 'disabled' : ''}
          data-tip="${esc(m.system ? tr('rm.t.system') : tr('rm.t.remove'))}">${icon('trash')}</button></td></tr>`).join('')
        || `<tr><td colspan="4" class="res-dim">${esc(tr('rm.none'))}</td></tr>`}</tbody></table>
      <form class="of-form rm-add" autocomplete="off">
        <div class="rm-add-row">
          <input type="search" name="q" placeholder="${esc(tr('rm.searchPh'))}" class="tip" data-tip="${esc(tr('rm.t.search'))}">
          <select name="principal" class="tip" data-tip="${esc(tr('rm.t.principal'))}"><option value="">${esc(tr('rm.pick'))}</option></select>
          <select name="role" class="tip" data-tip="${esc(tr('rm.t.role'))}">${d.roles.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')}</select>
          <button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('rm.t.add'))}">${icon('add')} ${esc(tr('rm.add'))}</button>
        </div>
        <div class="of-msg" role="status"></div></form>`;
    host.innerHTML = '';
    host.appendChild(box);
    const f = box.querySelector('form');
    const msg = f.querySelector('.of-msg');
    if (note) msg.innerHTML = `<span>${icon(note.ok ? 'ok' : 'fail')} ${esc(note.text)}</span>`;
    let timer = null;
    f.q.addEventListener('input', () => {
      clearTimeout(timer);
      const q = f.q.value.trim();
      if (q.length < 2) return;
      timer = setTimeout(async () => {
        try {
          const res = await call('GET', `/api/rancher-principals/${enc(o.cluster)}?q=${enc(q)}`);
          f.principal.innerHTML = `<option value="">${esc(res.items.length ? tr('rm.pick') : tr('rm.noMatch'))}</option>`
            + res.items.map(p => `<option value="${esc(p.id)}">${esc(p.name)}${p.login ? ` (${esc(p.login)})` : ''} · ${esc(p.kind === 'group' ? tr('rm.group') : tr('rm.user'))} · ${esc(p.provider)}</option>`).join('');
          if (res.items.length === 1) f.principal.value = res.items[0].id;
        } catch (err) {
          msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
        }
      }, 350);
    });
    const done = (id, text) => {
      if (window.VMActions && VMActions.follow) {
        VMActions.follow(id, msg, text, (ok) => setTimeout(() => render(host, o, { ok, text: ok ? text : msg.textContent.trim() }), 800));
      } else setTimeout(() => render(host, o), 2500);
    };
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!f.principal.value) { msg.innerHTML = `<span class="res-error">${esc(tr('rm.pickFirst'))}</span>`; return; }
      try {
        const out = await call('POST', `/api/rancher-members/${enc(o.cluster)}/do/add`,
          Object.assign(target(o), { principal: f.principal.value, role: f.role.value }));
        done(out.action_id, tr('rm.done.added'));
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    box.querySelector('tbody').addEventListener('click', async (e) => {
      const b = e.target.closest('[data-rm-act="remove"]');
      if (!b) return;
      const m = d.members.find(x => x.id === b.closest('tr').dataset.rm);
      if (!m || !confirm(tr('rm.confirmRemove', { name: m.name, role: m.role_name }))) return;
      try {
        const out = await call('POST', `/api/rancher-members/${enc(o.cluster)}/do/remove`, Object.assign(target(o), { id: m.id }));
        done(out.action_id, tr('rm.done.removed'));
      } catch (err) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
  }

  return { render };
})();
window.RancherMembers = RancherMembers;
