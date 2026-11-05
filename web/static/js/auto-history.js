/**
 * harvester-ops : onglet Historique d'un écran d'Automatisation.
 *
 * L'Activité filtrée sur les actions d'un écran : le filtre `action` de
 * l'Activité (sous-chaîne, côté serveur) choisit les lignes, puis seules
 * celles dont le verbe commence par un préfixe de l'écran sont gardées (une
 * VM nommée `capi-x` ne doit pas entrer dans l'historique de Cluster API).
 * Chaque ligne mène à l'action et à son journal (#activity/action/<id>).
 * Relue toutes les cinq secondes tant qu'une action tourne ET que l'onglet
 * reste affiché : quitter l'écran arrête la relecture, y revenir la relance.
 *
 * Bare-metal (2.1.0), Terraform et Cluster API (2.2.0) : un historique
 * chacun, créé par AutoHistory.create(options).
 */
const AutoHistory = (() => {
  const POLL_MS = 5000;
  const RUNNING = ['starting', 'running'];
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const tr = (k, v) => i18n.t(k, v);
  const fmtDate = (at) => {
    const t = Number(at);
    return t ? new Date(t * 1000).toLocaleString() : '';
  };

  function statusBadge(st) {
    const cls = st === 'done' ? 'ok' : (st === 'error' ? 'fail' : (RUNNING.includes(st) ? 'info' : 'dim'));
    return `<span class="badge ${cls}">${esc(st || '?')}</span>`;
  }

  /**
   * o = {
   *   box: () => Element            conteneur de l'onglet ;
   *   queries: ['tf-plan', ...]     valeurs du filtre `action` de l'Activité ;
   *   prefixes: [...]               verbes gardés (préfixes), `queries` par défaut ;
   *   only: (action) => bool        2.5.0 : filtre de plus (une action lue
   *                                 n'est gardée que s'il la retient) ;
   *   label: (action) => texte      libellé d'une action ;
   *   shown: () => bool             l'onglet est-il affiché ?
   *   cls: 'bm'                     préfixe des classes (bm-history-row...) ;
   *   hint, empty: () => texte      phrase d'en-tête, liste vide ;
   *   cluster: bool                 colonne Cluster (écrans d'un cluster de gestion) ;
   *   extra: (action) => html       geste de plus à côté du journal (2.4.0 :
   *                                 « Reprendre » d'un déroulé de plan), échappé
   *                                 par l'appelant.
   * }
   */
  function create(o) {
    let timer = null;
    let rev = 0;
    let byId = new Map();                 // actions de la dernière lecture
    const open = new Set();               // 2.5.0 : actions parentes dépliées
    const prefixes = o.prefixes || o.queries;
    const cls = o.cls || 'auto';
    const stop = () => { if (timer) { clearTimeout(timer); timer = null; } };
    const keep = (a) => prefixes.some(p => String(a.action || '').startsWith(p)) && (!o.only || o.only(a));

    async function render() {
      const box = o.box();
      if (!box) return;
      stop();
      const mine = ++rev;
      let runs = [];
      let failed = false;
      try {
        const lists = await Promise.all(o.queries.map(k =>
          fetch('/api/activity?limit=100&action=' + encodeURIComponent(k))
            .then(r => (r.ok ? r.json() : Promise.reject(r.status)))));
        const seen = new Set();
        lists.forEach(d => (d.in_progress || []).concat(d.actions_done || []).forEach(a => {
          if (!a || !a.id || seen.has(a.id) || !keep(a)) return;
          seen.add(a.id);
          runs.push(a);
        }));
      } catch (e) { failed = true; }
      // l'onglet a changé, ou une lecture plus récente est partie
      if (mine !== rev || !o.shown() || !document.body.contains(box)) return;
      byId = new Map(runs.map(a => [a.id, a]));
      // 2.5.0 (décision 174) : une fille dont le parent est dans la liste se
      // range sous lui (dépliable) ; sinon elle reste au premier niveau
      const kids = new Map();
      runs.forEach(a => {
        if (a.parent && byId.has(a.parent)) {
          if (!kids.has(a.parent)) kids.set(a.parent, []);
          kids.get(a.parent).push(a);
        }
      });
      kids.forEach(l => l.sort((a, b) => (a.started_at || 0) - (b.started_at || 0)));
      const top = runs.filter(a => !(a.parent && byId.has(a.parent)));
      const running = top.filter(a => RUNNING.includes(a.status));
      const past = top.filter(a => !RUNNING.includes(a.status));
      running.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
      past.sort((a, b) => (b.ended_at || b.started_at || 0) - (a.ended_at || a.started_at || 0));
      const row = (a, depth = 0) => {
        const target = String(a.action || '').split(':').slice(1).join(':');
        const dur = a.ended_at && a.started_at ? Math.max(0, a.ended_at - a.started_at) : null;
        const children = kids.get(a.id) || [];
        const isOpen = open.has(a.id);
        const toggle = children.length
          ? `<button type="button" class="btn-mini auto-history-expand" data-history-expand="${esc(a.id)}" aria-expanded="${isOpen}"
               title="${esc(tr(isOpen ? 'activity.collapseTip' : 'activity.expandTip', { n: children.length }))}">${Icons.svg(isOpen ? 'chevronDown' : 'chevronRight', { size: 12 })} ${children.length}</button> `
          : '';
        const sub = isOpen ? children.map(k => row(k, depth + 1)).join('') : '';
        return `<tr class="auto-history-row ${cls}-history-row${depth ? ' auto-history-child' : ''}" data-action-id="${esc(a.id)}"
                  ${depth ? `data-depth="${depth}" style="--act-depth:${depth}"` : ''}>
            <td>${statusBadge(a.status)}</td>
            <td class="auto-history-kind">${toggle}${esc(o.label(a))}</td>
            <td><code>${esc(target)}</code></td>
            ${o.cluster ? `<td>${esc(a.cluster || '')}</td>` : ''}
            <td>${esc(fmtDate(a.started_at))}</td>
            <td class="auto-num">${dur == null ? '' : esc(dur < 120 ? dur.toFixed(0) + ' s' : (dur / 60).toFixed(0) + ' min')}</td>
            <td><a class="tip auto-history-log ${cls}-history-log" href="#activity/action/${encodeURIComponent(a.id)}"
                   data-tip="${esc(tr('bmh.logTip'))}">${Icons.svg('logs', { size: 14 })} ${esc(tr('bmh.log'))}</a>${o.extra ? o.extra(a) : ''}</td>
          </tr>${sub}`;
      };
      const table = (list, title) => `<h3 class="auto-history-title ${cls}-history-title">${esc(title)} <span class="form-hint">${list.length}</span></h3>
          <div class="tbl-scroll"><table class="data-table auto-history ${cls}-history">
            <thead><tr><th>${esc(tr('bmh.col.status'))}</th><th>${esc(tr('bmh.col.kind'))}</th><th>${esc(tr('bmh.col.target'))}</th>
              ${o.cluster ? `<th>${esc(tr('col.cluster'))}</th>` : ''}
              <th>${esc(tr('bmh.col.started'))}</th><th>${esc(tr('bmh.col.duration'))}</th><th>${esc(tr('bmh.col.log'))}</th></tr></thead>
            <tbody>${list.map(a => row(a)).join('')}</tbody></table></div>`;
      box.innerHTML = `<p class="form-hint">${esc(o.hint())}</p>`
        + (failed ? `<div class="summary-bar bad">${esc(tr('bmh.failed'))}</div>` : '')
        + (!runs.length && !failed ? `<div class="empty-state auto-history-empty ${cls}-history-empty">${esc(o.empty())}</div>` : '')
        + (running.length ? table(running, tr('bmh.running')) : '')
        + (past.length ? table(past, tr('bmh.past')) : '');
      if (!box.dataset.nestBound) {
        box.dataset.nestBound = '1';
        box.addEventListener('click', (e) => {
          const b = e.target.closest('[data-history-expand]');
          if (!b || !box.contains(b)) return;
          e.preventDefault();
          e.stopPropagation();
          const id = b.dataset.historyExpand;
          if (open.has(id)) open.delete(id); else open.add(id);
          render();
        });
      }
      // une action en cours : relue tant que l'onglet reste affiché
      if (running.length) timer = setTimeout(() => { timer = null; if (o.shown()) render(); }, POLL_MS);
    }

    return { render, stop, get: (id) => byId.get(id) || null };
  }

  return { create, POLL_MS };
})();

window.AutoHistory = AutoHistory;
