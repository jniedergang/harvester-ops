/**
 * harvester-ops : dossiers de la console (navigation 2.0, tâche 15)
 *
 * Une arborescence partagée range VMs (Harvester, KVM), charges de
 * conteneurs et hôtes. Ce module donne :
 *   - le panneau de gauche de la vue des VMs et de la vue « Dossiers »
 *     (deux dossiers virtuels « Toutes » et « Non rangées », l'arborescence
 *     avec ses comptes, créer, renommer, supprimer en saisie sur place, et
 *     la cible des glisser-déposer) ;
 *   - la fenêtre « Déplacer vers un dossier » : recherche de dossier, puis
 *     les contrôles du serveur objet par objet (POST /api/folders/check)
 *     AVANT le bouton : l'objet sera écrit, seul le rangement de la console
 *     changera, ou le geste échouera (raison dite) ;
 *   - la fenêtre « Déplacer le contenu vers... » d'un dossier non vide, qui
 *     vide puis supprime en une action.
 *
 * Chaque geste est une action suivie (dock, Activité) lancée par le serveur
 * (bin/harvester-folders.py) ; le dock propose « Annuler » à la fin. Le rôle
 * (App.role) ne sert qu'à griser les contrôles, avec la raison ; le serveur
 * décide. Les comptes viennent des lignes que la personne voit : une VM
 * cachée par ses droits n'est jamais comptée.
 *
 * Fonctions pures (testées dans node) : index, subtree, within, rowsIn,
 * search, targets, hrefOf.
 */
(function (root) {
  const UNFILED = 'unfiled';

  /** {byId, byPath, children} d'une arborescence [{id, parent_id, name, path}]. */
  function index(tree) {
    const byId = new Map(), byPath = new Map(), children = new Map();
    (tree || []).forEach((f) => {
      byId.set(String(f.id), f);
      byPath.set(f.path, f);
      const p = f.parent_id == null ? '' : String(f.parent_id);
      if (!children.has(p)) children.set(p, []);
      children.get(p).push(f);
    });
    children.forEach((l) => l.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
      || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)));
    return { byId, byPath, children };
  }

  /** Identifiants du dossier et de ses descendants. */
  function subtree(tree, id) {
    const idx = index(tree);
    const out = [];
    const walk = (k) => { out.push(k); (idx.children.get(k) || []).forEach((c) => walk(String(c.id))); };
    if (idx.byId.has(String(id))) walk(String(id));
    return out;
  }

  const within = (path, prefix) => !!path && (path === prefix || path.startsWith(prefix + '/'));

  /** Lignes rangées dans le dossier `path` (sous-dossiers compris) ;
   *  `unfiled` : les lignes sans dossier ; null : toutes. */
  function rowsIn(rows, path) {
    if (path == null) return (rows || []).slice();
    if (path === UNFILED) return (rows || []).filter((r) => !r.folder);
    return (rows || []).filter((r) => within(r.folder, path));
  }

  /** Dossiers dont le chemin contient chaque mot de la recherche. */
  function search(tree, query) {
    const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
    return (tree || []).filter((f) => words.every((w) => f.path.toLowerCase().includes(w)))
      .sort((a, b) => a.path.localeCompare(b.path));
  }

  /** Cibles possibles pour vider le dossier `id` : tout sauf lui et ses
   *  descendants, plus « Non rangées ». */
  function targets(tree, id) {
    const skip = new Set(subtree(tree, id));
    return [UNFILED].concat((tree || []).filter((f) => !skip.has(String(f.id)))
      .sort((a, b) => a.path.localeCompare(b.path)).map((f) => String(f.id)));
  }

  /** Adresse d'un nœud du panneau : base `vms` ou `folders` ; id null =
   *  « Toutes », `unfiled` = « Non rangées ». */
  function hrefOf(base, id) {
    if (id == null || id === '') return '#' + base;
    return base === 'folders' ? `#folders/${encodeURIComponent(id)}` : `#${base}/folder/${encodeURIComponent(id)}`;
  }

  /** Comptes par chemin, sous-dossiers compris, en un passage : chaque
   *  ligne compte pour son dossier et ses ancêtres. Rend { all, unfiled,
   *  byPath: Map }. Linéaire en lignes × profondeur (une grande
   *  arborescence reste rapide). */
  function counts(rows) {
    const byPath = new Map();
    let unfiled = 0;
    (rows || []).forEach((r) => {
      if (!r.folder) { unfiled += 1; return; }
      let p = r.folder;
      while (p) {
        byPath.set(p, (byPath.get(p) || 0) + 1);
        const i = p.lastIndexOf('/');
        p = i > 0 ? p.slice(0, i) : '';
      }
    });
    return { all: (rows || []).length, unfiled, byPath };
  }

  const pure = { index, subtree, within, rowsIn, search, targets, hrefOf, counts, UNFILED };
  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    return;
  }

  // ---------------------------------------------------------------------
  // État partagé
  // ---------------------------------------------------------------------
  const tr = (k, p) => (root.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (root.Icons ? Icons.svg(n, { size }) : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const role = () => (root.App && App.role ? App.role() : null);
  const canOperate = () => (ROLE_RANK[role()] || 0) >= ROLE_RANK.operator;
  const DRAG_TYPE = 'application/x-hops-keys';

  let tree = null;
  let loading = null;
  let lastError = '';
  let editing = null;          // { mode: 'new', parent } | { mode: 'rename', id }
  let confirmDelete = null;    // id du dossier dont la suppression est demandée
  const panels = new Set();    // panneaux montés (re-rendus à chaque changement)

  function notify() {
    panels.forEach((p) => { if (p.el.isConnected) render(p); else panels.delete(p); });
    document.dispatchEvent(new CustomEvent('hops:folders'));
  }

  /** Relit l'arborescence (une seule lecture à la fois). */
  let attempted = false;
  function load() {
    if (loading) return loading;
    attempted = true;
    loading = fetch('/api/folders').then(async (r) => {
      const d = await r.json().catch(() => null);
      if (!r.ok || !d) throw new Error((d && (d.hint || d.error)) || `HTTP ${r.status}`);
      tree = d.tree || [];
      lastError = '';
    }).catch((e) => { lastError = e.message; })
      .finally(() => { loading = null; notify(); });
    return loading;
  }

  const get = () => tree;
  // index de l'arborescence courante, refait seulement quand elle change
  let idxMemo = { tree: null, idx: null };
  function idx() {
    if (idxMemo.tree !== tree) idxMemo = { tree, idx: index(tree || []) };
    return idxMemo.idx;
  }
  const byId = (id) => (tree ? idx().byId.get(String(id)) : null) || null;
  const byPath = (path) => (tree ? idx().byPath.get(path) : null) || null;
  const pathOf = (id) => (id === UNFILED ? UNFILED : (byId(id) || {}).path || null);

  async function send(method, url, body) {
    const r = await fetch(url, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(d.hint || d.error || `HTTP ${r.status}`);
      err.status = r.status;
      err.data = d;
      throw err;
    }
    return d;
  }

  /** Suit une action jusqu'à sa fin : promesse de { ok, run }. */
  function follow(actionId) {
    if (root.Dock && Dock.poll) Dock.poll();
    return new Promise((resolve) => {
      if (!root.SSEReconnect) { resolve({ ok: true, run: {} }); return; }
      const es = SSEReconnect.connect(`/api/stream/${encodeURIComponent(actionId)}`, {
        on: {
          end: (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
            es.close();
            resolve({ ok: d.status === 'done', run: d });
          },
        },
        onStatus: (s) => { if (s.state === 'dead') resolve({ ok: false, run: { error_summary: 'stream lost' } }); },
      });
    });
  }

  function toast(text, bad) {
    let box = document.getElementById('fld-toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'fld-toast';
      box.className = 'fld-toast';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.textContent = text;
    box.classList.toggle('bad', !!bad);
    box.classList.add('show');
    clearTimeout(box._t);
    box._t = setTimeout(() => box.classList.remove('show'), 6000);
  }

  /** Lance un geste (réponse 202) puis le suit ; à la fin, l'arborescence
   *  est relue et `after(ok)` appelé. */
  async function gesture(method, url, body, doneText, after) {
    const out = await send(method, url, body);
    toast(tr('fld.started'));
    follow(out.action_id).then(({ ok, run }) => {
      toast(ok ? doneText : tr('fld.failed', { error: run.error_summary || run.status || '?' }), !ok);
      // l'arborescence relue d'abord : `after` y retrouve un dossier créé
      load().then(() => { if (after) after(ok, out.action_id); });
    });
    return out.action_id;
  }

  // ---------------------------------------------------------------------
  // Panneau
  // ---------------------------------------------------------------------
  /**
   * Monte un panneau dans `el`. opts = {
   *   base: 'vms' | 'folders', current: id | 'unfiled' | null,
   *   rows: [{ folder }] | null,   lignes que la personne voit (comptes ; null : pas lues)
   *   allLabel, unfiledLabel, allTip, unfiledTip,
   *   hosts: [{ kind, name, state, count }] (mode Hôtes, vue des VMs seulement),
   *   mode: 'folders' | 'hosts', currentHost: 'kind/name' | 'kind' | null,
   *   onMode(mode), onDrop(keys, target)
   * }
   */
  function panel(el, opts) {
    let p = [...panels].find((x) => x.el === el);
    if (!p) {
      p = { el, opts };
      panels.add(p);
      bindPanel(p);
    }
    p.opts = opts;
    render(p);
    // une seule lecture d'office (une erreur ne relance pas en boucle)
    if (!tree && !loading && !attempted) load();
    return p;
  }

  function nodeHtml(p, f, depth, ctx) {
    const o = p.opts;
    const id = String(f.id);
    const kids = ctx.idx.children.get(id) || [];
    const active = String(o.current) === id;
    const n = ctx.counts ? (ctx.counts.byPath.get(f.path) || 0) : null;
    const isEdit = editing && editing.mode === 'rename' && String(editing.id) === id;
    const origin = f.origin === 'objects'
      ? ` <span class="badge dim fld-origin tip" data-tip="${esc(tr('fld.originTip'))}" title="${esc(tr('fld.originTip'))}">${esc(tr('fld.origin'))}</span>` : '';
    const label = isEdit
      ? `<input type="text" class="fld-input" data-fld-edit="${esc(id)}" value="${esc(f.name)}" maxlength="63"
           aria-label="${esc(tr('fld.renameInput'))}" title="${esc(tr('fld.renameInputTip'))}">`
      : `<span class="fld-name">${esc(f.name)}</span>${origin}`;
    const tip = tr('fld.nodeTip', { path: f.path });
    let h = `<li role="treeitem" aria-expanded="${kids.length ? 'true' : 'false'}" aria-level="${depth}">
      <a class="fld-node${active ? ' active' : ''}" href="${esc(hrefOf(o.base, id))}" data-fld-drop="${esc(id)}"
         data-fld-id="${esc(id)}" title="${esc(tip)}" ${active ? 'aria-current="page"' : ''}>${icon('folder', 14)} ${label}
        ${n == null ? '' : `<span class="fld-count">${esc(n)}</span>`}</a>`;
    const adding = editing && editing.mode === 'new' && String(editing.parent) === id;
    if (kids.length || adding) {
      h += `<ul role="group">${kids.map((c) => nodeHtml(p, c, depth + 1, ctx)).join('')}${adding ? newRow() : ''}</ul>`;
    }
    return h + '</li>';
  }

  const newRow = () => `<li role="treeitem"><div class="fld-node fld-new">${icon('folder', 14)}
      <input type="text" class="fld-input" data-fld-new="1" maxlength="63" placeholder="${esc(tr('fld.newPh'))}"
        aria-label="${esc(tr('fld.newPh'))}" title="${esc(tr('fld.newInputTip'))}"></div></li>`;

  function hostsHtml(p) {
    const o = p.opts;
    const fam = { harvester: tr('hnav.fam.harvester'), kvm: tr('hnav.fam.kvm') };
    const total = o.rows ? o.rows.length : null;
    let h = `<ul class="fld-tree" role="tree">
      <li role="treeitem"><a class="fld-node virtual${!o.currentHost ? ' active' : ''}" href="#vms" title="${esc(tr('fld.allHostsTip'))}">
        ${icon('vm', 14)} <span class="fld-name">${esc(o.allLabel)}</span>${total == null ? '' : `<span class="fld-count">${esc(total)}</span>`}</a></li>`;
    ['harvester', 'kvm'].forEach((kind) => {
      const hs = (o.hosts || []).filter((x) => x.kind === kind);
      if (!hs.length) return;
      const n = hs.reduce((s, x) => s + (x.count || 0), 0);
      // la famille est un intitulé (comme vCenter), ses hôtes sont des liens
      h += `<li role="treeitem" aria-expanded="true"><div class="fld-node virtual fld-family${o.currentHost === kind ? ' active' : ''}"
          title="${esc(tr('fld.familyTip', { family: fam[kind] }))}">${icon(kind === 'kvm' ? 'compute' : 'node', 14)}
          <span class="fld-name">${esc(fam[kind])}</span><span class="fld-count">${esc(n)}</span></div><ul role="group">`
        + hs.map((x) => {
          const key = `${kind}/${x.name}`;
          const href = `#vms/host/${kind}/${encodeURIComponent(x.name)}`;
          return `<li role="treeitem"><a class="fld-node${o.currentHost === key ? ' active' : ''}" href="${esc(href)}"
              title="${esc(tr('fld.hostTip', { name: x.name }))}"><span class="hnav-dot st-${esc(x.state || 'unknown')}"></span>
              <span class="fld-name">${esc(x.name)}</span><span class="fld-count">${esc(x.count || 0)}</span></a></li>`;
        }).join('') + '</ul></li>';
    });
    return h + '</ul>';
  }

  function render(p) {
    const o = p.opts;
    const op = canOperate();
    const why = tr('fld.operatorOnly');
    const cur = o.current != null && o.current !== UNFILED ? byId(o.current) : null;
    const modes = o.hosts ? `<div class="sub-tabs sub-tabs-inline fld-modes" role="tablist">
        <button type="button" role="tab" class="sub-tab${o.mode !== 'hosts' ? ' active' : ''}" data-fld-mode="folders"
          aria-selected="${o.mode !== 'hosts'}" title="${esc(tr('fld.modeFoldersTip'))}">${esc(tr('fld.modeFolders'))}</button>
        <button type="button" role="tab" class="sub-tab${o.mode === 'hosts' ? ' active' : ''}" data-fld-mode="hosts"
          aria-selected="${o.mode === 'hosts'}" title="${esc(tr('fld.modeHostsTip'))}">${esc(tr('fld.modeHosts'))}</button></div>` : '';
    let body;
    if (o.mode === 'hosts' && o.hosts) {
      body = hostsHtml(p);
    } else if (!tree) {
      body = lastError ? `<p class="res-error">${icon('fail')} ${esc(tr('fld.loadFailed', { error: lastError }))}</p>`
        : `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
    } else {
      const ctx = { idx: idx(), counts: o.rows ? counts(o.rows) : null };
      const all = ctx.counts ? ctx.counts.all : null;
      const unf = ctx.counts ? ctx.counts.unfiled : null;
      const roots = ctx.idx.children.get('') || [];
      body = `<ul class="fld-tree" role="tree" aria-label="${esc(tr('fld.treeLabel'))}">
        <li role="treeitem"><a class="fld-node virtual${o.current == null ? ' active' : ''}" href="${esc(hrefOf(o.base, null))}"
            title="${esc(o.allTip)}">${icon('vm', 14)} <span class="fld-name">${esc(o.allLabel)}</span>
            ${all == null ? '' : `<span class="fld-count">${esc(all)}</span>`}</a></li>
        <li role="treeitem"><a class="fld-node virtual${o.current === UNFILED ? ' active' : ''}" href="${esc(hrefOf(o.base, UNFILED))}"
            data-fld-drop="${UNFILED}" title="${esc(o.unfiledTip)}">${icon('folder', 14)} <span class="fld-name">${esc(o.unfiledLabel)}</span>
            ${unf == null ? '' : `<span class="fld-count">${esc(unf)}</span>`}</a></li>
        ${roots.map((f) => nodeHtml(p, f, 1, ctx)).join('')}
        ${editing && editing.mode === 'new' && editing.parent == null ? newRow() : ''}
      </ul>
      ${roots.length ? '' : `<p class="form-hint fld-empty">${esc(tr('fld.noFolder'))}</p>`}`;
      const tTip = (ok, t) => (op ? (ok ? t : tr('fld.openFirst')) : why);
      body += `<div class="fld-tools">
          <button type="button" class="btn btn-sm tip" data-fld-tool="new" ${op ? '' : 'disabled'}
            title="${esc(op ? (cur ? tr('fld.newInTip', { path: cur.path }) : tr('fld.newRootTip')) : why)}">${icon('add', 12)} ${esc(tr('fld.new'))}</button>
          <button type="button" class="btn btn-sm tip" data-fld-tool="rename" ${op && cur ? '' : 'disabled'}
            title="${esc(tTip(cur, cur ? tr('fld.renameTip', { path: cur.path }) : ''))}">${esc(tr('fld.rename'))}</button>
          <button type="button" class="btn btn-sm tip" data-fld-tool="delete" ${op && cur ? '' : 'disabled'}
            title="${esc(tTip(cur, cur ? tr('fld.deleteTip', { path: cur.path }) : ''))}">${esc(tr('fld.delete'))}</button>
        </div>`;
      if (confirmDelete && cur && String(confirmDelete) === String(cur.id)) {
        body += `<div class="fld-confirm" role="group" aria-label="${esc(tr('fld.deleteConfirm', { path: cur.path }))}">
            <span>${esc(tr('fld.deleteConfirm', { path: cur.path }))}</span>
            <div class="btn-row"><button type="button" class="btn btn-sm btn-danger tip" data-fld-tool="delete-yes"
              title="${esc(tr('fld.deleteYesTip'))}">${esc(tr('fld.delete'))}</button>
            <button type="button" class="btn btn-sm btn-secondary tip" data-fld-tool="delete-no"
              title="${esc(tr('fld.keepTip'))}">${esc(tr('fld.keep'))}</button></div></div>`;
      }
      if (!op) body += `<p class="form-hint fld-viewer">${esc(tr('fld.viewerNote'))}</p>`;
    }
    const msg = p.el.querySelector('.fld-msg');
    // une saisie en cours survit à un nouveau rendu (relecture en fond)
    const typing = p.el.querySelector('[data-fld-edit], [data-fld-new]');
    const typed = typing ? typing.value : null;
    p.el.innerHTML = `<div class="card fld-panel">${modes}<div class="fld-body">${body}</div>
      <div class="fld-msg" role="status" aria-live="polite"></div></div>`;
    if (msg && msg.textContent) p.el.querySelector('.fld-msg').replaceWith(msg);
    const input = p.el.querySelector('[data-fld-edit], [data-fld-new]');
    if (input) {
      if (typed != null) input.value = typed;
      input.focus();
      if (editing && !editing.shown) { input.select(); editing.shown = true; }
    }
  }

  function say(p, text, bad) {
    const m = p.el.querySelector('.fld-msg');
    if (m) m.innerHTML = text ? `<span class="${bad ? 'res-error' : ''}">${icon(bad ? 'fail' : 'ok')} ${esc(text)}</span>` : '';
  }

  function bindPanel(p) {
    const el = p.el;
    el.addEventListener('click', (e) => {
      const m = e.target.closest('[data-fld-mode]');
      if (m) { if (p.opts.onMode) p.opts.onMode(m.dataset.fldMode); return; }
      const t = e.target.closest('[data-fld-tool]');
      if (!t || t.disabled) return;
      const cur = p.opts.current != null && p.opts.current !== UNFILED ? byId(p.opts.current) : null;
      const act = t.dataset.fldTool;
      if (act === 'new') { editing = { mode: 'new', parent: cur ? String(cur.id) : null }; notify(); }
      else if (act === 'rename' && cur) { editing = { mode: 'rename', id: String(cur.id) }; notify(); }
      else if (act === 'delete' && cur) askDelete(p, cur);
      else if (act === 'delete-no') { confirmDelete = null; notify(); }
      else if (act === 'delete-yes' && cur) doDelete(p, cur);
    });
    el.addEventListener('keydown', (e) => {
      const input = e.target.closest('[data-fld-edit], [data-fld-new]');
      if (!input) return;
      if (e.key === 'Escape') { e.preventDefault(); editing = null; notify(); }
      if (e.key === 'Enter') { e.preventDefault(); submitEdit(p, input); }
    });
    el.addEventListener('focusout', (e) => {
      const input = e.target.closest('[data-fld-edit], [data-fld-new]');
      // quitter le champ sans valider l'abandonne (Entrée valide) ; un
      // nouveau rendu qui remplace le champ garde la saisie
      if (!input || input.dataset.sent) return;
      setTimeout(() => {
        const a = document.activeElement;
        if (!editing || (a && a.matches && a.matches('[data-fld-edit], [data-fld-new]'))) return;
        editing = null;
        notify();
      }, 150);
    });
    // glisser-déposer : un nœud du panneau reçoit des lignes
    el.addEventListener('dragover', (e) => {
      const n = e.target.closest('[data-fld-drop]');
      if (!n || !canOperate() || !hasKeys(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      el.querySelectorAll('.fld-node.drop').forEach((x) => { if (x !== n) x.classList.remove('drop'); });
      n.classList.add('drop');
    });
    el.addEventListener('dragleave', (e) => {
      const n = e.target.closest('[data-fld-drop]');
      if (n && !n.contains(e.relatedTarget)) n.classList.remove('drop');
    });
    el.addEventListener('drop', (e) => {
      const n = e.target.closest('[data-fld-drop]');
      if (!n) return;
      e.preventDefault();
      n.classList.remove('drop');
      let keys = [];
      try { keys = JSON.parse(e.dataTransfer.getData(DRAG_TYPE) || '[]'); } catch { keys = []; }
      if (keys.length && canOperate() && p.opts.onDrop) p.opts.onDrop(keys, n.dataset.fldDrop);
    });
  }

  const hasKeys = (e) => [...(e.dataTransfer && e.dataTransfer.types || [])].includes(DRAG_TYPE);

  /** Rend une ligne glissable : `keys()` donne les clés à déplacer. */
  function draggable(rowEl, keys) {
    if (!canOperate()) return;
    rowEl.setAttribute('draggable', 'true');
    rowEl.addEventListener('dragstart', (e) => startDrag(e, keys()));
  }

  function startDrag(e, k) {
    e.dataTransfer.setData(DRAG_TYPE, JSON.stringify(k));
    e.dataTransfer.setData('text/plain', k.join('\n'));
    e.dataTransfer.effectAllowed = 'move';
  }

  /** Lignes glissables d'un tableau par un seul écouteur sur `container` :
   *  `rowSel` désigne une ligne, `keysOf(ligne)` ses clés (une grande liste
   *  n'attache pas un écouteur par ligne). */
  function draggableRows(container, rowSel, keysOf) {
    if (container.dataset.fldDrag) return;
    container.dataset.fldDrag = '1';
    container.addEventListener('dragstart', (e) => {
      const r = e.target.closest && e.target.closest(rowSel);
      if (!r || !canOperate()) return;
      startDrag(e, keysOf(r));
    });
  }

  async function submitEdit(p, input) {
    const name = input.value.trim();
    if (!name) { editing = null; notify(); return; }
    input.dataset.sent = '1';
    input.disabled = true;
    const ed = editing;
    try {
      if (ed.mode === 'new') {
        const parent = ed.parent == null ? null : Number(ed.parent);
        const parentPath = parent == null ? '' : pathOf(parent);
        await gesture('POST', '/api/folders', { parent_id: parent, name }, tr('fld.created', { name }), (ok) => {
          if (!ok) return;
          const f = byPath(`${parentPath}/${name}`);
          if (f && root.Router) Router.go(hrefOf(p.opts.base, String(f.id)));
        });
      } else {
        const f = byId(ed.id);
        await gesture('PATCH', `/api/folders/${encodeURIComponent(ed.id)}`, { name },
          tr('fld.renamed', { from: f ? f.path : '', name }), () => { if (p.opts.onChanged) p.opts.onChanged(); });
      }
      editing = null;
      notify();
    } catch (err) {
      delete input.dataset.sent;
      input.disabled = false;
      input.focus();
      say(p, err.message, true);
    }
  }

  function askDelete(p, f) {
    const n = p.opts.rows ? rowsIn(p.opts.rows, f.path).length : null;
    // contenu visible ou sous-dossiers : la fenêtre « déplacer le contenu vers »
    if ((idx().children.get(String(f.id)) || []).length || n) { openEmptyInto(f, p); return; }
    confirmDelete = String(f.id);
    notify();
  }

  async function doDelete(p, f) {
    confirmDelete = null;
    try {
      await gesture('DELETE', `/api/folders/${encodeURIComponent(f.id)}`, undefined, tr('fld.deleted', { path: f.path }),
        (ok) => { if (ok && root.Router) Router.go(hrefOf(p.opts.base, f.parent_id == null ? null : String(f.parent_id))); });
      notify();
    } catch (err) {
      notify();
      // non vide pour quelqu'un (objets qu'elle ne voit pas) : la fenêtre le dit
      if (err.status === 409) openEmptyInto(f, p, err.data);
      else say(p, err.message, true);
    }
  }

  // ---------------------------------------------------------------------
  // Fenêtre « Déplacer le contenu vers... » (dossier non vide)
  // ---------------------------------------------------------------------
  function targetListHtml(ids, name, chosen) {
    return ids.map((id) => {
      const label = id === UNFILED ? tr('fld.unfiled') : pathOf(id);
      return `<label class="fld-target tip" title="${esc(id === UNFILED ? tr('fld.unfiledTargetTip') : tr('fld.targetTip', { path: label }))}">
        <input type="radio" name="${name}" value="${esc(id)}"${String(chosen) === id ? ' checked' : ''}>
        ${icon('folder', 13)} <span>${esc(label)}</span></label>`;
    }).join('') || `<p class="form-hint">${esc(tr('fld.noTarget'))}</p>`;
  }

  function openEmptyInto(f, p, refusal) {
    if (!root.FloatingPanels) return;
    const ids = targets(tree, f.id);
    const hidden = refusal && refusal.hidden ? `<p class="hint warn">${icon('warn', 13)} ${esc(tr('fld.hiddenContent'))}</p>` : '';
    const panelApi = FloatingPanels.open({
      id: 'fld-empty', icon: 'folder', width: 520, height: 460, title: tr('fld.emptyTitle', { path: f.path }),
      bodyHtml: `<div class="fld-win" data-fld-win="empty">
        <p>${esc(tr('fld.emptyIntro', { path: f.path }))}</p>${hidden}
        <input type="search" class="res-filter tip" data-fld-search placeholder="${esc(tr('fld.searchPh'))}" title="${esc(tr('fld.searchTip'))}">
        <div class="fld-targets" data-fld-targets>${targetListHtml(ids, 'fld-empty-to', null)}</div>
        <div class="bk-form-actions"><button type="button" class="btn btn-sm btn-danger tip" data-fld-empty-go disabled
          title="${esc(tr('fld.emptyGoTip'))}">${esc(tr('fld.emptyGo'))}</button></div>
        <div class="of-msg" role="status"></div></div>`,
    });
    const win = panelApi.el || document.getElementById('fp-fld-empty');
    const box = win.querySelector('[data-fld-win="empty"]');
    if (!box || box.dataset.ready === String(f.id)) return;
    box.dataset.ready = String(f.id);
    const go = box.querySelector('[data-fld-empty-go]');
    box.addEventListener('change', () => { go.disabled = !box.querySelector('input[name="fld-empty-to"]:checked'); });
    box.querySelector('[data-fld-search]').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      box.querySelector('[data-fld-targets]').innerHTML = targetListHtml(
        ids.filter((id) => (id === UNFILED ? tr('fld.unfiled') : pathOf(id)).toLowerCase().includes(q)), 'fld-empty-to', null);
      go.disabled = true;
    });
    go.addEventListener('click', async () => {
      const to = box.querySelector('input[name="fld-empty-to"]:checked');
      if (!to) return;
      go.disabled = true;
      try {
        await gesture('POST', `/api/folders/${encodeURIComponent(f.id)}/empty-into`, { target_id: to.value === UNFILED ? UNFILED : Number(to.value) },
          tr('fld.emptied', { path: f.path }), (ok) => {
            if (p && p.opts.onChanged) p.opts.onChanged();
            if (ok && root.Router) Router.go(hrefOf(p ? p.opts.base : 'vms', to.value === UNFILED ? UNFILED : to.value));
          });
        FloatingPanels.close('fld-empty');
      } catch (err) {
        go.disabled = false;
        box.querySelector('.of-msg').innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
  }

  // ---------------------------------------------------------------------
  // Fenêtre « Déplacer vers un dossier » : contrôles avant le bouton
  // ---------------------------------------------------------------------
  const OUTCOME = () => ({
    object: ['ok', tr('fld.outcome.object')], console: ['warn', tr('fld.outcome.console')],
    fail: ['fail', tr('fld.outcome.fail')],
  });

  /**
   * objects : [{ key, label, host? }] ; opts : { target: id | 'unfiled' | null,
   * extra: { label, objects } (case « ranger aussi ses N VMs », décochée),
   * onDone(ok) }.
   */
  function openMove(objects, opts) {
    const o = opts || {};
    if (!root.FloatingPanels || !objects || !objects.length) return;
    if (!canOperate()) { toast(tr('fld.operatorOnly'), true); return; }
    FloatingPanels.close('fld-move');
    const extra = o.extra && o.extra.objects && o.extra.objects.length ? o.extra : null;
    const api = FloatingPanels.open({
      id: 'fld-move', icon: 'folder', width: 620, height: 560,
      title: tr('fld.moveTitle', { n: objects.length }),
      bodyHtml: `<div class="fld-win" data-fld-win="move">
        <div class="fld-step"><strong>${esc(tr('fld.moveStep1'))}</strong>
          <input type="search" class="res-filter tip" data-fld-search placeholder="${esc(tr('fld.searchPh'))}" title="${esc(tr('fld.searchTip'))}">
          <div class="fld-targets" data-fld-targets></div></div>
        ${extra ? `<label class="bk-check tip fld-extra" title="${esc(tr('fld.extraTip'))}">
            <input type="checkbox" data-fld-extra> <span>${esc(extra.label)}</span></label>` : ''}
        <div class="fld-step"><strong>${esc(tr('fld.moveStep2'))}</strong>
          <div class="fld-checks" data-fld-checks><p class="form-hint">${esc(tr('fld.pickTarget'))}</p></div></div>
        <div class="bk-form-actions"><button type="button" class="btn btn-sm btn-primary tip" data-fld-apply disabled
          title="${esc(tr('fld.applyTip'))}">${icon('folder', 13)} ${esc(tr('fld.apply'))}</button></div>
        <div class="of-msg" role="status"></div></div>`,
    });
    const win = api.el || document.getElementById('fp-fld-move');
    const box = win.querySelector('[data-fld-win="move"]');
    const state = { target: o.target == null ? null : String(o.target), checks: null, seq: 0 };
    const list = () => objects.concat(extra && box.querySelector('[data-fld-extra]')?.checked ? extra.objects : []);
    const labelOf = new Map(objects.concat(extra ? extra.objects : []).map((x) => [x.key, x.label || x.key]));

    function drawTargets(q) {
      const ids = [UNFILED].concat(search(tree || [], q).map((f) => String(f.id)));
      box.querySelector('[data-fld-targets]').innerHTML = targetListHtml(ids, 'fld-move-to', state.target);
    }

    async function check() {
      const seq = ++state.seq;
      const apply = box.querySelector('[data-fld-apply]');
      apply.disabled = true;
      const out = box.querySelector('[data-fld-checks]');
      if (state.target == null) { out.innerHTML = `<p class="form-hint">${esc(tr('fld.pickTarget'))}</p>`; return; }
      out.innerHTML = `<p class="form-hint">${esc(tr('fld.checking'))}</p>`;
      try {
        const objs = list();
        const d = await send('POST', '/api/folders/check', {
          objects: objs.map((x) => (x.host ? { key: x.key, host: x.host } : { key: x.key })),
          to: state.target === UNFILED ? UNFILED : Number(state.target),
        });
        if (seq !== state.seq) return;
        state.checks = d.objects || [];
        const oc = OUTCOME();
        const counts = { object: 0, console: 0, fail: 0 };
        state.checks.forEach((c) => { counts[c.outcome] = (counts[c.outcome] || 0) + 1; });
        out.innerHTML = `<p class="fld-summary">${esc(tr('fld.summary', counts))}</p>
          <table class="data-table fld-check-table"><thead><tr><th>${esc(tr('fld.col.object'))}</th><th>${esc(tr('fld.col.result'))}</th>
            <th>${esc(tr('fld.col.reason'))}</th></tr></thead><tbody>${state.checks.map((c) => `<tr data-fld-check="${esc(c.key)}" data-outcome="${esc(c.outcome)}">
              <td>${esc(labelOf.get(c.key) || c.key)}</td>
              <td><span class="badge ${esc((oc[c.outcome] || ['dim'])[0])}">${esc((oc[c.outcome] || ['', c.outcome])[1])}</span></td>
              <td class="fld-reason">${esc(c.reason)}</td></tr>`).join('')}</tbody></table>`;
        apply.disabled = !state.checks.some((c) => c.outcome !== 'fail');
        apply.title = apply.disabled ? tr('fld.applyNone') : tr('fld.applyTip');
      } catch (err) {
        if (seq !== state.seq) return;
        out.innerHTML = `<p class="res-error">${icon('fail')} ${esc(err.message)}</p>`;
      }
    }

    drawTargets('');
    box.querySelector('[data-fld-search]').addEventListener('input', (e) => drawTargets(e.target.value));
    box.addEventListener('change', (e) => {
      if (e.target.name === 'fld-move-to') { state.target = e.target.value; check(); }
      if (e.target.matches('[data-fld-extra]')) check();
    });
    box.querySelector('[data-fld-apply]').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      const send_ = list().filter((x) => (state.checks || []).some((c) => c.key === x.key && c.outcome !== 'fail'));
      const dest = state.target === UNFILED ? tr('fld.unfiled') : pathOf(state.target);
      try {
        await gesture('POST', '/api/folders/assign', {
          objects: send_.map((x) => (x.host ? { key: x.key, host: x.host } : { key: x.key })),
          to: state.target === UNFILED ? UNFILED : Number(state.target),
        }, tr('fld.moved', { n: send_.length, path: dest }), (ok) => { if (o.onDone) o.onDone(ok); });
        FloatingPanels.close('fld-move');
      } catch (err) {
        btn.disabled = false;
        box.querySelector('.of-msg').innerHTML = `<span class="res-error">${icon('fail')} ${esc(err.message)}</span>`;
      }
    });
    if (state.target != null) check();
    box.querySelector('[data-fld-search]').focus();
  }

  /** Bouton « Déplacer vers un dossier » (grisé pour un lecteur, avec la raison). */
  function moveButton(attrs, label) {
    const op = canOperate();
    return `<button type="button" class="btn btn-sm btn-secondary tip" ${attrs} ${op ? '' : 'disabled'}
      title="${esc(op ? tr('fld.moveTip') : tr('fld.operatorOnly'))}">${icon('folder', 13)} ${esc(label || tr('fld.move'))}</button>`;
  }

  // le rôle arrive après le premier rendu (whoami) : les contrôles suivent
  document.addEventListener('hops:role', () => notify());
  // une annulation depuis le dock : l'arborescence est relue à la fin
  document.addEventListener('hops:undo', (e) => {
    follow(e.detail.undo).then(() => { load(); document.dispatchEvent(new CustomEvent('hops:folders-changed')); });
  });

  // ---------------------------------------------------------------------
  // Objets d'un hôte (case « ranger aussi ses N VMs »)
  // ---------------------------------------------------------------------
  let fleetVms = null;
  /** VMs de la flotte (dernière lecture de la vue des VMs, sinon une lecture). */
  async function vmsRows() {
    const c = root.AllVMs && AllVMs.cached ? AllVMs.cached() : null;
    if (c && c.vms) return c.vms;
    if (fleetVms) return fleetVms;
    try {
      const r = await fetch('/api/vms-all?kinds=harvester,kvm');
      const d = r.ok ? await r.json() : {};
      fleetVms = (d.vms || []).map((v) => Object.assign({ kind: 'harvester', source: v.cluster }, v));
    } catch { fleetVms = []; }
    return fleetVms;
  }

  const vmObject = (v) => ({
    key: v.key, host: v.kind === 'kvm' ? (v.source || v.host) : undefined,
    label: `${v.name} (${v.kind === 'kvm' ? 'KVM' : 'Harvester'} · ${v.source || v.cluster || v.host}${v.namespace ? `/${v.namespace}` : ''})`,
  });

  /** Ranger un hôte : sa déclaration ; la case « ranger aussi ses N VMs »
   *  est proposée décochée (pas d'héritage). */
  async function openHostMove(h, onDone) {
    const rows = (await vmsRows()).filter((v) => v.key && (v.kind || 'harvester') === h.kind
      && (v.source || v.cluster || v.host) === h.name);
    const label = `${h.name} (${h.kind === 'kvm' ? 'KVM' : 'Harvester'})`;
    openMove([{ key: `host/${h.kind}/${h.name}`, label }], {
      extra: rows.length ? { label: tr('fld.extraLabel', { n: rows.length }), objects: rows.map(vmObject) } : null,
      onDone,
    });
  }

  root.Folders = Object.assign({
    load, get, byId, byPath, pathOf, panel, openMove, openEmptyInto, openHostMove, draggable, draggableRows,
    moveButton, follow,
    canOperate, vmObject, DRAG_TYPE,
  }, pure);

  // ---------------------------------------------------------------------
  // Vue « Dossiers » (#folders, #folders/<id>) : trois groupes
  // ---------------------------------------------------------------------
  const FoldersView = (() => {
    let el = null;
    let current = null;
    let rows = { vms: null, containers: null, hosts: null };
    let timer = null;

    async function fetchAll() {
      const get_ = async (url, pick) => {
        try {
          const r = await fetch(url);
          return r.ok ? pick(await r.json()) : [];
        } catch { return []; }
      };
      const [vms, cts, hosts] = await Promise.all([
        get_('/api/vms-all?kinds=harvester,kvm', (d) => (d.vms || []).map((v) => Object.assign({ kind: 'harvester', source: v.cluster }, v))),
        get_('/api/containers-all', (d) => (d.workloads || []).filter((w) => w.key)),
        get_('/api/hosts', (d) => (d.hosts || []).map((h) => Object.assign({ key: `host/${h.kind}/${h.name}` }, h))),
      ]);
      rows = { vms, containers: cts, hosts };
      fleetVms = vms;
      draw();
    }

    function group(kind, list, path) {
      const base = { vms: 'vms', containers: 'containers', hosts: 'hosts' }[kind];
      const title = { vms: tr('fview.vms'), containers: tr('fview.containers'), hosts: tr('fview.hosts') }[kind];
      const open = current != null ? hrefOf(base, current) : `#${base}`;
      const op = canOperate();
      const items = list == null ? null : rowsIn(list, path);
      const line = (x) => {
        let name, where;
        if (kind === 'vms') { name = x.name; where = `${x.kind === 'kvm' ? 'KVM' : 'Harvester'} · ${x.source || x.cluster}${x.namespace ? `/${x.namespace}` : ''}`; }
        else if (kind === 'containers') { name = x.name; where = `${x.kind} · ${x.cluster}/${x.namespace}`; }
        else { name = x.name; where = x.kind === 'kvm' ? 'KVM' : 'Harvester'; }
        const consoleOnly = x.folder_source === 'console'
          ? ` <span class="badge warn tip" title="${esc(tr('vmf.consoleOnlyTip', { host: x.source || x.cluster || x.name }))}">${esc(tr('vmf.consoleOnly'))}</span>` : '';
        return `<li class="fview-item" data-fview-key="${esc(x.key)}"><span class="fview-name">${esc(name)}</span>
          <span class="res-dim">${esc(where)}</span>${x.folder && x.folder !== path ? ` <span class="res-dim">${esc(x.folder)}</span>` : ''}${consoleOnly}
          <button type="button" class="btn-icon-action tip" data-fview-move="${esc(kind)}" ${op ? '' : 'disabled'}
            data-tip="${esc(op ? tr('fld.moveOneTip') : tr('fld.operatorOnly'))}" aria-label="${esc(op ? tr('fld.moveOneTip') : tr('fld.operatorOnly'))}">${icon('folder', 13)}</button></li>`;
      };
      return `<div class="card fview-group" data-fview-group="${kind}"><div class="card-header"><h3>${esc(title)}
          ${items == null ? '' : `<span class="badge">${esc(items.length)}</span>`}</h3>
          <a class="btn btn-sm btn-secondary tip" href="${esc(open)}" title="${esc(tr('fview.openTip'))}">${esc(tr('fview.open'))}</a></div>
        <div class="card-body">${items == null ? `<p class="form-hint">${esc(tr('common.loading'))}</p>`
          : items.length ? `<ul class="fview-list">${items.slice(0, 200).map(line).join('')}</ul>${items.length > 200
            ? `<p class="form-hint">${esc(tr('fview.more', { n: items.length - 200 }))}</p>` : ''}`
            : `<p class="form-hint">${esc(tr('fview.empty'))}</p>`}</div></div>`;
    }

    function draw() {
      if (!el || !el.isConnected) return;
      const path = current == null ? null : (current === UNFILED ? UNFILED : pathOf(current));
      const all = [].concat(rows.vms || [], rows.containers || [], rows.hosts || []);
      const known = rows.vms != null;
      if (!el.querySelector('.vmf-layout')) {
        el.innerHTML = `<div class="vmf-layout"><aside class="vmf-side" data-fview="panel"></aside>
          <div class="vmf-main"><div class="vmf-scope" data-fview="scope"></div><div class="fview-groups" data-fview="groups"></div></div></div>`;
        el.addEventListener('click', onClick);
      }
      panel(el.querySelector('[data-fview="panel"]'), {
        base: 'folders', current, mode: 'folders',
        rows: known ? all : null,
        allLabel: tr('fview.all'), allTip: tr('fview.allTip'), unfiledLabel: tr('fld.unfiled'), unfiledTip: tr('fview.unfiledTip'),
        onDrop: null, onChanged: () => fetchAll(),
      });
      const gone = current != null && current !== UNFILED && tree && !path;
      el.querySelector('[data-fview="scope"]').innerHTML = `<h2 class="vmf-scope-title">${icon('folder', 16)}
        <span>${esc(current == null ? tr('fview.all') : current === UNFILED ? tr('fld.unfiled') : (path || tr('vmf.folderGone')))}</span></h2>
        ${gone ? `<p class="hint warn">${icon('warn', 13)} ${esc(tr('vmf.folderGoneHint'))}</p>` : ''}`;
      el.querySelector('[data-fview="groups"]').innerHTML = gone ? ''
        : ['vms', 'containers', 'hosts'].map((k) => group(k, rows[k], path)).join('');
    }

    function onClick(e) {
      const b = e.target.closest('[data-fview-move]');
      if (!b || b.disabled) return;
      const key = b.closest('[data-fview-key]').dataset.fviewKey;
      const kind = b.dataset.fviewMove;
      const x = (rows[kind] || []).find((r) => r.key === key);
      if (!x) return;
      if (kind === 'hosts') { openHostMove(x, () => fetchAll()); return; }
      const obj = kind === 'vms' ? vmObject(x) : { key: x.key, label: `${x.name} (${x.kind} · ${x.cluster}/${x.namespace})` };
      openMove([obj], { onDone: () => fetchAll() });
    }

    function start(host, id) {
      el = host;
      current = id == null ? null : String(id);
      load();
      draw();
      fetchAll();
      clearInterval(timer);
      timer = setInterval(() => { if (!document.hidden) fetchAll(); }, 30000);
    }

    function stop() { clearInterval(timer); timer = null; }

    document.addEventListener('hops:folders', () => { if (timer) draw(); });
    document.addEventListener('hops:folders-changed', () => { if (timer) fetchAll(); });
    return { start, stop, current: () => current };
  })();
  root.FoldersView = FoldersView;
})(typeof window !== 'undefined' ? window : globalThis);
