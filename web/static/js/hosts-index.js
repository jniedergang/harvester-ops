/**
 * harvester-ops : index des hôtes (navigation 2.0, tâche 8)
 *
 * Un tableau des deux familles (clusters Harvester, hyperviseurs KVM) :
 * famille, nom, état, version, étiquettes, nombre de VMs, dossier, favori.
 * Tri par colonne, filtres par famille, état, étiquette, version et
 * dossier. Mêmes gestes que le menu des hôtes : ouvrir (lien réel), étoile
 * de favori. `#hosts/kvm` reprend ce qu'offrait KVM > Hôtes : Déclarer un
 * hôte, Tester et Retirer par ligne (Retirer confirmé dans la ligne).
 *
 * Source unique : GET /api/hosts (aucune lecture par hôte). Le rôle de la
 * session (App.role) ne sert qu'à griser les boutons ; le serveur décide.
 *
 * Fonction pure testée dans node : view(hosts, prefs) -> lignes filtrées et
 * triées, et facets(hosts) -> valeurs des filtres.
 */
(function (root) {
  const KINDS = ['harvester', 'kvm'];
  // tri par état : les alertes d'abord, puis non lus, puis ok
  const STATE_ORDER = ['unreachable', 'error', 'denied', 'unknown', 'ok'];
  const ALERT = ['unreachable', 'denied', 'error'];
  const UNFILED = '-';                       // valeur du filtre « non rangé »
  const SORTS = ['kind', 'name', 'state', 'version', 'labels', 'vms', 'folder', 'favorite'];

  const labelPairs = (h) => Object.entries(h.labels || {}).map(([k, v]) => `${k}=${v}`).sort();
  const cmp = (a, b) => (a < b ? -1 : a > b ? 1 : 0);

  function sortKey(h, col) {
    switch (col) {
      case 'kind': return KINDS.indexOf(h.kind);
      case 'state': { const i = STATE_ORDER.indexOf(h.state); return i < 0 ? STATE_ORDER.length : i; }
      case 'version': return h.version || '\uffff';          // sans version : à la fin
      case 'labels': return labelPairs(h).join(',') || '\uffff';
      case 'vms': return typeof h.vm_count === 'number' ? h.vm_count : -1;
      case 'folder': return h.folder || '\uffff';
      case 'favorite': return h.favorite ? 0 : 1;
      default: return String(h.name || '').toLowerCase();
    }
  }

  /** Lignes à montrer. prefs = { kind, state, label, version, folder, sort, dir }.
   *  `state` vaut un état ou 'alert' (les trois états en alerte). */
  function view(hosts, prefs) {
    const p = Object.assign({ kind: '', state: '', label: '', version: '', folder: '', sort: 'name', dir: 1 }, prefs || {});
    const col = SORTS.includes(p.sort) ? p.sort : 'name';
    const dir = p.dir < 0 ? -1 : 1;
    return (hosts || []).filter((h) => (!p.kind || h.kind === p.kind)
      && (!p.state || (p.state === 'alert' ? ALERT.includes(h.state) : h.state === p.state))
      && (!p.label || labelPairs(h).includes(p.label))
      && (!p.version || (h.version || '') === p.version)
      && (!p.folder || (p.folder === UNFILED ? !h.folder : h.folder === p.folder)))
      .sort((a, b) => cmp(sortKey(a, col), sortKey(b, col)) * dir
        || cmp(String(a.name).toLowerCase(), String(b.name).toLowerCase()) || cmp(a.kind, b.kind));
  }

  /** Valeurs présentes pour chaque filtre (menus déroulants). */
  function facets(hosts) {
    const uniq = (xs) => [...new Set(xs.filter(Boolean))].sort();
    const hs = hosts || [];
    return {
      states: STATE_ORDER.filter((s) => hs.some((h) => h.state === s)),
      labels: uniq([].concat(...hs.map(labelPairs))),
      versions: uniq(hs.map((h) => h.version)),
      folders: uniq(hs.map((h) => h.folder)),
    };
  }

  const pure = { view, facets, STATE_ORDER, SORTS, UNFILED };
  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    return;
  }

  // ---------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------
  const tr = (k, p) => (root.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (root.Icons ? Icons.svg(n, { size }) : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };

  let cur = null;   // { el, prefs, data, error, pending }

  const FAMILY = () => ({ harvester: tr('hnav.fam.harvester'), kvm: tr('hnav.fam.kvm') });
  const STATE = () => ({
    ok: tr('hnav.state.ok'), unreachable: tr('hnav.state.unreachable'), denied: tr('hnav.state.denied'),
    error: tr('hnav.state.error'), unknown: tr('hnav.state.unknown'),
  });
  const STATE_BADGE = { ok: 'ok', unreachable: 'fail', error: 'fail', denied: 'warn', unknown: 'dim' };
  const COLS = () => [
    ['kind', tr('hix.col.family')], ['name', tr('hix.col.name')], ['state', tr('hix.col.state')],
    ['version', tr('hix.col.version')], ['labels', tr('hix.col.labels')], ['vms', tr('hix.col.vms')],
    ['folder', tr('hix.col.folder')], ['favorite', tr('hix.col.favorite')],
  ];

  const role = () => (root.App && App.role ? App.role() : null);
  const can = (need) => (ROLE_RANK[role()] || 0) >= ROLE_RANK[need];
  const hostHref = (h) => root.Routes.format({ name: 'host', kind: h.kind, host: h.name, sub: null, tab: null, pane: null, id: null });

  function start(el, opts) {
    stop();
    if (!el) return Promise.resolve();
    const o = opts || {};
    cur = { el, data: null, error: '', pending: null, msgs: new Map(),
            prefs: { kind: KINDS.includes(o.kind) ? o.kind : '', state: '', label: '', version: '', folder: '', sort: 'name', dir: 1 } };
    shell();
    // la liste du menu est déjà lue : l'afficher tout de suite, puis relire
    const known = root.HostNav && HostNav._data ? HostNav._data() : null;
    if (known) { cur.data = known; render(); }
    return reload();
  }

  function stop() { cur = null; }

  /** Famille filtrée (l'adresse #hosts/<famille> la suit). */
  const kind = () => (cur ? cur.prefs.kind : '');

  function shell() {
    const sel = (f, tip, all) => `<select class="tip" data-hix-filter="${f}" title="${esc(tip)}" data-tip="${esc(tip)}" aria-label="${esc(tip)}"><option value="">${esc(all)}</option></select>`;
    cur.el.innerHTML = `<div class="card na-card hix-card">
        <div class="res-tools">
          ${sel('kind', tr('hix.f.kindTip'), tr('hix.f.allKinds'))}
          ${sel('state', tr('hix.f.stateTip'), tr('hix.f.allStates'))}
          ${sel('label', tr('hix.f.labelTip'), tr('hix.f.allLabels'))}
          ${sel('version', tr('hix.f.versionTip'), tr('hix.f.allVersions'))}
          ${sel('folder', tr('hix.f.folderTip'), tr('hix.f.allFolders'))}
          <span class="res-count"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-hix="refresh" title="${esc(tr('hix.refreshTip'))}" data-tip="${esc(tr('hix.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          <button type="button" class="btn btn-sm btn-primary tip" data-hix="add">${icon('add')} <span></span></button>
        </div>
        <div class="res-feedback" data-hix="feedback" role="status" aria-live="polite"></div>
        <div data-hix="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.el.querySelector('.hix-card');
    card.addEventListener('click', onClick);
    card.querySelectorAll('[data-hix-filter]').forEach((s) => s.addEventListener('change', () => {
      cur.prefs[s.dataset.hixFilter] = s.value;
      render();
      if (s.dataset.hixFilter === 'kind' && root.Router) Router.syncFromScreen();
    }));
  }

  async function reload() {
    const c = cur;
    if (!c) return;
    try {
      const r = await fetch('/api/hosts');
      const d = await r.json().catch(() => null);
      if (!r.ok || !d) throw new Error((d && (d.hint || d.error)) || `HTTP ${r.status}`);
      if (c !== cur) return;
      c.data = d;
      c.error = '';
      if (root.HostNav && HostNav.render) HostNav.render(d);
    } catch (e) {
      if (c !== cur) return;
      c.error = e.message;
    }
    render();
  }

  function fillSelect(s, values, labelOf, current) {
    const want = [''].concat(values).join('|');
    if ([...s.options].map((o) => o.value).join('|') !== want) {
      const first = s.options[0] ? s.options[0].outerHTML : '<option value=""></option>';
      s.innerHTML = first + values.map((v) => `<option value="${esc(v)}">${esc(labelOf(v))}</option>`).join('');
    }
    s.value = values.includes(current) ? current : '';
    return s.value;
  }

  function render() {
    if (!cur || !cur.el.isConnected) return;
    const body = cur.el.querySelector('[data-hix="body"]');
    const add = cur.el.querySelector('[data-hix="add"]');
    // #hosts/kvm : « Déclarer un hôte » de KVM > Hôtes ; sinon le choix des deux familles
    const kvmOnly = cur.prefs.kind === 'kvm';
    const addTip = !can('admin') ? tr('kvm.adminOnly') : kvmOnly ? tr('kvm.declareTip') : tr('hnav.addTip');
    add.querySelector('span').textContent = kvmOnly ? tr('kvm.declare') : tr('hnav.add');
    add.disabled = !can('admin');
    add.title = addTip;
    add.dataset.tip = addTip;
    if (!cur.data) {
      body.innerHTML = cur.error ? `<p class="res-error" role="status">${icon('fail')} ${esc(tr('hnav.loadFailed', { error: cur.error }))}</p>`
        : `<p class="form-hint">${esc(tr('common.loading'))}</p>`;
      return;
    }
    const hosts = cur.data.hosts || [];
    const f = facets(hosts);
    const p = cur.prefs;
    const sels = (n) => cur.el.querySelector(`[data-hix-filter="${n}"]`);
    p.kind = fillSelect(sels('kind'), KINDS, (k) => FAMILY()[k], p.kind);
    const states = f.states.some((s) => ALERT.includes(s)) ? ['alert'].concat(f.states) : f.states;
    p.state = fillSelect(sels('state'), states, (s) => (s === 'alert' ? tr('hix.f.alert') : STATE()[s] || s), p.state);
    p.label = fillSelect(sels('label'), f.labels, (l) => l, p.label);
    p.version = fillSelect(sels('version'), f.versions, (v) => v, p.version);
    p.folder = fillSelect(sels('folder'), [UNFILED].concat(f.folders), (v) => (v === UNFILED ? tr('hix.unfiled') : v), p.folder);
    const rows = view(hosts, p);
    cur.el.querySelector('.res-count').textContent = tr('hix.count', { n: rows.length, total: hosts.length });
    if (!hosts.length) { body.innerHTML = `<p class="form-hint hix-empty">${esc(tr('hnav.noHost'))}</p>`; return; }
    if (!rows.length) { body.innerHTML = `<p class="form-hint hix-empty">${esc(tr('hix.noMatch'))}</p>`; return; }
    const th = ([k, label]) => `<th data-sort="${k}" class="tip${p.sort === k ? (p.dir > 0 ? ' sort-asc' : ' sort-desc') : ''}"
        title="${esc(tr('hix.sortTip'))}" data-tip="${esc(tr('hix.sortTip'))}"${p.sort === k ? ` aria-sort="${p.dir > 0 ? 'ascending' : 'descending'}"` : ''}>${esc(label)}</th>`;
    body.innerHTML = `<table class="data-table sortable hix-table"><thead><tr>${COLS().map(th).join('')}<th></th></tr></thead>
      <tbody>${rows.map(rowHtml).join('')}</tbody></table>`;
    // le message d'une action (Tester, Retirer) est un élément gardé par
    // hôte : remis dans sa ligne à chaque rendu, l'action en cours continue
    // d'y écrire
    cur.msgs.forEach((el, key) => rowOf(key)?.querySelector('.hix-msg')?.replaceWith(el));
  }

  function rowHtml(h) {
    const key = `${h.kind}/${h.name}`;
    const st = h.state || 'unknown';
    const stText = STATE()[st] || st;
    const labels = labelPairs(h).map((l) => `<span class="badge dim">${esc(l)}</span>`).join(' ');
    const starTip = h.favorite ? tr('hnav.star.remove') : tr('hnav.star.add');
    let acts = '';
    if (h.kind === 'kvm') {
      const tTip = can('operator') ? tr('kvm.testTip') : tr('kvm.operatorOnly');
      const rTip = can('admin') ? tr('kvm.removeTip') : tr('kvm.adminOnly');
      acts = cur.pending === key ? `<span class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(tr('kvm.removeConfirm', { name: h.name }))}">
          <span>${esc(tr('kvm.removeConfirm', { name: h.name }))}</span>
          <button type="button" class="btn btn-sm btn-danger tip" data-hix="remove-yes" data-key="${esc(key)}" title="${esc(tr('kvm.removeYesTip'))}" data-tip="${esc(tr('kvm.removeYesTip'))}">${icon('trash')} ${esc(tr('kvm.removeYes'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-hix="remove-no" title="${esc(tr('kvm.confirmNoTip'))}" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></span>`
        : `<button type="button" class="btn btn-sm btn-secondary tip" data-hix="test" data-key="${esc(key)}" ${can('operator') ? '' : 'disabled'} title="${esc(tTip)}" data-tip="${esc(tTip)}">${icon('test')} ${esc(tr('kvm.test'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip" data-hix="remove" data-key="${esc(key)}" ${can('admin') ? '' : 'disabled'} title="${esc(rTip)}" data-tip="${esc(rTip)}">${icon('trash')} ${esc(tr('kvm.remove'))}</button>`;
    }
    return `<tr data-hix-row="${esc(key)}" data-state="${esc(st)}">
        <td><span class="badge hp-family">${esc(h.kind === 'kvm' ? 'KVM' : 'Harvester')}</span></td>
        <td><a class="hix-name tip" href="${esc(hostHref(h))}" title="${esc(tr('hix.openTip', { name: h.name }))}">${esc(h.name)}</a></td>
        <td><span class="badge ${STATE_BADGE[st] || 'dim'}" title="${esc(h.error ? `${stText}: ${h.error}` : stText)}">${esc(stText)}</span></td>
        <td>${esc(h.version || '–')}</td>
        <td class="hix-labels">${labels || '<span class="res-dim">–</span>'}</td>
        <td>${typeof h.vm_count === 'number' ? esc(h.vm_count) : '<span class="res-dim">–</span>'}</td>
        <td>${h.folder ? esc(h.folder) : `<span class="res-dim">${esc(tr('hix.unfiled'))}</span>`}</td>
        <td><button type="button" class="hnav-star${h.favorite ? ' on' : ''}" data-hix="star" data-key="${esc(key)}"
              aria-pressed="${h.favorite ? 'true' : 'false'}" title="${esc(starTip)}" aria-label="${esc(starTip)}">${icon('star', 13)}</button></td>
        <td class="hix-acts">${acts}<div class="kvm-tile-msg hix-msg" role="status"></div></td></tr>`;
  }

  const findHost = (key) => ((cur && cur.data && cur.data.hosts) || []).find((h) => `${h.kind}/${h.name}` === key);
  const rowOf = (key) => [...cur.el.querySelectorAll('[data-hix-row]')].find((r) => r.dataset.hixRow === key);
  /** Message d'action d'un hôte : un élément unique par clé, posé dans sa ligne. */
  function msgOf(key) {
    let el = cur.msgs.get(key);
    if (!el) {
      el = document.createElement('div');
      el.className = 'kvm-tile-msg hix-msg';
      el.setAttribute('role', 'status');
      cur.msgs.set(key, el);
      rowOf(key)?.querySelector('.hix-msg')?.replaceWith(el);
    }
    return el;
  }

  async function toggleStar(key, btn) {
    const h = findHost(key);
    if (!h) return;
    const want = !h.favorite;
    h.favorite = want;
    render();
    try {
      const r = await fetch(`/api/me/favorites/${enc(h.kind)}/${enc(h.name)}`, { method: want ? 'PUT' : 'DELETE' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
      if (root.HostNav && HostNav.load) HostNav.load();
    } catch (e) {
      h.favorite = !want;
      render();
      const again = cur && cur.el.querySelector(`[data-hix="star"][data-key="${CSS.escape(key)}"]`);
      if (again || btn) (again || btn).title = tr('hnav.star.failed', { error: e.message });
    }
  }

  function onClick(e) {
    const th = e.target.closest('th[data-sort]');
    if (th && cur) {
      const k = th.dataset.sort;
      cur.prefs.dir = cur.prefs.sort === k ? -cur.prefs.dir : 1;
      cur.prefs.sort = k;
      render();
      return;
    }
    const b = e.target.closest('[data-hix]');
    if (!b || !cur || b.disabled) return;
    const act = b.dataset.hix;
    const key = b.dataset.key;
    const h = key ? findHost(key) : null;
    if (act === 'refresh') { reload(); return; }
    if (act === 'add') {
      if (cur.prefs.kind === 'kvm' && root.KVM) KVM.openDeclare();
      else if (root.HostNav) HostNav.openAdd();
      return;
    }
    if (act === 'star') { toggleStar(key, b); return; }
    if (!h) return;
    if (act === 'test' && root.KVM) { KVM.testHost(h.name, msgOf(key)); return; }
    if (act === 'remove') { cur.pending = key; render(); cur.el.querySelector('[data-hix="remove-no"]')?.focus(); return; }
    if (act === 'remove-no') { cur.pending = null; render(); return; }
    if (act === 'remove-yes' && root.KVM) {
      const c = cur;
      b.disabled = true;
      KVM.removeHost(h.name, msgOf(key), (ok) => {
        if (c !== cur) return;
        c.pending = null;
        if (ok) setTimeout(reload, 900);
        else render();
      });
    }
  }

  // le rôle arrive après le premier rendu (whoami) : les boutons suivent
  document.addEventListener('hops:role', () => render());

  root.HostsIndex = Object.assign({ start, stop, reload, kind }, pure);
})(typeof window !== 'undefined' ? window : globalThis);
