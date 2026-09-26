/**
 * harvester-ops — ce que le cluster a refusé de montrer (v1.56.0)
 *
 * Vu en réel avec un compte « membre du cluster » de Rancher : l'aperçu
 * affichait « 0 nœud », la topologie aucune VM, et le sélecteur aucun
 * cluster, sans un mot. Le serveur joint maintenant à ses réponses les
 * lectures que la RBAC a refusées (en-tête `X-Cluster-Denied`) et, pour une
 * session Rancher, la raison de chaque cluster absent. Ce bandeau le dit.
 */
const AccessNotice = (() => {
  const $ = (s) => document.querySelector(s);
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const escapeHtml = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const denied = new Map();      // clé -> refus
  let hidden = [];               // clusters absents et pourquoi
  let dismissed = new Set();     // clés déjà vues quand on a fermé le bandeau
  let who = { auth: 'open' };

  const REASON = {
    'no-access': (n) => tr('access.reason.noAccess', { name: n }),
    'not-found': (n) => tr('access.reason.notFound', { name: n }),
  };

  function keyOf(d) { return [d.verb, d.resource, d.group, d.namespace].join('|'); }

  // Un même refus revient d'un espace de noms à l'autre (vu en réel : huit
  // « get deployments » pour les huit espaces de Cluster API) : une ligne par
  // ressource, avec ses espaces de noms.
  function grouped() {
    const g = new Map();
    for (const d of denied.values()) {
      const k = [d.verb, d.resource, d.group].join('|');
      if (!g.has(k)) g.set(k, { verb: d.verb, resource: d.resource, group: d.group, namespaces: [], cluster: false });
      const e = g.get(k);
      if (d.namespace) e.namespaces.push(d.namespace); else e.cluster = true;
    }
    return [...g.values()];
  }

  function itemText(e) {
    const res = e.group ? `${e.resource} (${e.group})` : e.resource;
    if (e.cluster || !e.namespaces.length) return tr('access.item.cluster', { verb: e.verb, resource: res });
    const shown = e.namespaces.slice(0, 3).join(', ');
    const more = e.namespaces.length > 3 ? ' ' + tr('access.more', { n: e.namespaces.length - 3 }) : '';
    return tr('access.item.ns', { verb: e.verb, resource: res, namespace: shown + more });
  }

  function render() {
    const el = $('#access-notice');
    if (!el) return;
    const keys = [...denied.keys(), ...hidden.map(h => 'cluster:' + h.name)];
    const fresh = keys.filter(k => !dismissed.has(k));
    if (!fresh.length) { el.hidden = true; el.innerHTML = ''; return; }
    const user = who.user || '';
    const intro = who.auth === 'rancher' ? tr('access.byRancher', { user })
      : (who.delegation_active && who.cluster_user) ? tr('access.byCluster', { user: who.cluster_user })
      : tr('access.generic');
    const hint = who.auth === 'rancher' ? tr('access.hint.rancher') : tr('access.hint.cluster');
    const parts = [];
    if (denied.size) {
      parts.push(`<p class="an-intro">${escapeHtml(intro)}</p>
        <ul class="an-list">${grouped().map(e => `<li><code>${escapeHtml(itemText(e))}</code></li>`).join('')}</ul>`);
    }
    if (hidden.length) {
      parts.push(`<p class="an-intro">${escapeHtml(tr('access.clustersTitle'))}</p>
        <ul class="an-list">${hidden.map(h => `<li>${escapeHtml((REASON[h.reason] || REASON['not-found'])(h.name))}</li>`).join('')}</ul>`);
    }
    el.innerHTML = `
      <span class="an-icon" aria-hidden="true">${window.Icons ? Icons.svg('lock', { size: 16 }) : ''}</span>
      <div class="an-body">
        <strong class="an-title">${escapeHtml(tr('access.title'))}</strong>
        ${parts.join('')}
        <p class="an-hint">${escapeHtml(hint)}</p>
      </div>
      <button type="button" class="btn-icon-sm an-close tip" data-tip="${escapeHtml(tr('access.dismissTip'))}"
              aria-label="${escapeHtml(tr('common.close'))}">×</button>`;
    el.hidden = false;
    el.querySelector('.an-close').addEventListener('click', () => {
      keys.forEach(k => dismissed.add(k));
      render();
    });
  }

  /** Refus joints à une réponse (en-tête X-Cluster-Denied). */
  function report(items) {
    let added = false;
    for (const d of items || []) {
      if (!d || !d.resource) continue;
      const k = keyOf(d);
      if (!denied.has(k)) { denied.set(k, d); added = true; }
    }
    if (added) render();
  }

  /** Clusters qu'une session Rancher ne peut pas ouvrir, et pourquoi. */
  function clusters(list) {
    // Un cluster que la console ne joint pas (éteint) n'est pas une question
    // de droits : le dire ici, avec le conseil de demander un rôle, égarait
    // (vu en réel : l'administrateur de Rancher voyait ses bancs éteints
    // listés comme « cachés »). Il reviendra quand il sera allumé.
    hidden = (Array.isArray(list) ? list : []).filter(h => h.reason !== 'unidentified');
    render();
  }

  /** Changement de cluster : les refus du précédent ne valent plus. */
  function reset() {
    denied.clear();
    dismissed = new Set([...dismissed].filter(k => k.startsWith('cluster:')));
    render();
  }

  function setIdentity(d) { who = d || who; render(); }

  return { report, clusters, reset, setIdentity };
})();
window.AccessNotice = AccessNotice;
