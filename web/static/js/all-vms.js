/**
 * harvester-ops : vue « Machines virtuelles » (v1.85.0, racine #vms en 2.0.0)
 *
 * Toutes les VMs de la flotte, Harvester et KVM, dans un seul tableau lu
 * par /api/vms-all?kinds=harvester,kvm (chaque cluster avec l'identité de
 * la personne, chaque hôte KVM par SSH). À gauche, un panneau à deux modes,
 * comme vCenter : Dossiers (folders.js : l'arborescence partagée, « Toutes
 * les VMs » et « Non rangées ») et Hôtes (regroupement par famille puis par
 * hôte, en lecture seule). L'adresse dit le périmètre : #vms,
 * #vms/folder/<id>, #vms/folder/unfiled, #vms/host/<famille>[/<hôte>].
 *
 * Filtres : sources (puces cliquables, plusieurs à la fois), état,
 * namespace, texte libre ; tri sur chaque colonne ; filtres et tri retenus
 * par le navigateur. Une source éteinte, injoignable ou qui refuse la
 * lecture est dite sur sa puce au lieu de disparaître.
 *
 * Gestes : le menu d'une ligne suit sa famille (VMActions pour Harvester,
 * console et fenêtres de kvm-vm.js pour KVM). Ranger : glisser une ou
 * plusieurs lignes sur un dossier, ou « Déplacer vers un dossier » sur une
 * ligne ou la sélection ; la fenêtre montre les contrôles du serveur avant
 * le bouton. Chaque geste est une action suivie dans le dock.
 */
const AllVMs = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 15000;
  // revue I-3 : la vue d'accueil ne dessine que les PAGE premières lignes de
  // la liste filtrée et triée (« Show 300 more » en ajoute) ; la sélection,
  // « tout cocher » et le glisser portent sur des clés, pas sur le DOM
  const PAGE = 300;
  const SEARCH_MS = 150;
  const STORE = 'harvester_ops_allvms';
  const PANEL_STORE = 'harvester_ops_vms_panel';
  const KINDS = ['harvester', 'kvm'];

  let host = null;
  let timer = null;
  let data = null;
  let loading = false;
  let builtLang = '';                   // langue du squelette (il porte des libellés)
  let scope = { id: null, kind: null, host: null };
  let dr = null;                        // { uid -> id du contrat }, lu une fois
  let limit = PAGE;                     // lignes dessinées
  let listKey = '';                     // filtres + tri + périmètre du dernier rendu
  let lastRaw = null;                   // corps de la dernière lecture (relecture identique : rien)
  let searchTimer = null;
  let drawn = { keys: [], html: [] };   // lignes dessinées (clé, HTML) : seules celles qui changent sont refaites
  const selection = new Set();          // clés des lignes (`key`)
  const prefs = { clusters: [], phase: '', ns: '', text: '', sort: 'name', dir: 'asc' };

  function loadPrefs() {
    try { Object.assign(prefs, JSON.parse(localStorage.getItem(STORE) || '{}')); } catch { /* réglages illisibles */ }
    if (!Array.isArray(prefs.clusters)) prefs.clusters = [];
  }
  function savePrefs() {
    try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch { /* stockage indisponible */ }
  }
  const panelMode = () => {
    if (scope.kind) return 'hosts';
    try { return localStorage.getItem(PANEL_STORE) === 'hosts' ? 'hosts' : 'folders'; } catch { return 'folders'; }
  };

  const src = (v) => v.source || v.cluster || v.host || '';
  const keyOf = (v) => v.key;

  /** Lignes de la réponse, ancienne (1.85.0) ou nouvelle forme : famille,
   *  source et clé toujours posées. */
  function normalize(d) {
    const vms = (d && d.vms) || [];
    vms.forEach((v) => {
      v.kind = v.kind || 'harvester';
      v.source = src(v);
      if (!v.key) v.key = `harvester/${v.source}/${v.namespace}/${v.name}`;
    });
    const sources = Array.isArray(d.sources) ? d.sources
      : (d.clusters || []).map((c) => ({ kind: 'harvester', source: c.cluster, state: c.state, count: c.count, error: c.error }));
    return Object.assign({}, d, { vms, sources });
  }

  // Familles d'état du filtre : la phase de la VMI, ramenée à ce qu'un
  // exploitant cherche (« qu'est-ce qui tourne, qu'est-ce qui est arrêté »).
  const BUCKET = { Running: 'running', Stopped: 'stopped', Paused: 'paused', Failed: 'failed',
                   Pending: 'starting', Scheduling: 'starting', Scheduled: 'starting', Starting: 'starting' };
  const bucket = (v) => BUCKET[v.phase || 'Stopped'] || 'other';
  const PHASES = [
    ['running', () => tr('avm.phase.running')], ['stopped', () => tr('avm.phase.stopped')],
    ['starting', () => tr('avm.phase.starting')], ['paused', () => tr('avm.phase.paused')],
    ['failed', () => tr('avm.phase.failed')], ['other', () => tr('avm.phase.other')],
  ];
  const STATE_LABEL = {
    Running: () => tr('vm.state.Running'), Stopped: () => tr('vm.state.Stopped'), Paused: () => tr('vm.state.Paused'),
    Pending: () => tr('vm.state.Pending'), Starting: () => tr('vm.state.Starting'), Failed: () => tr('vm.state.Failed'),
    Unknown: () => tr('vm.state.Unknown'),
  };
  const phaseLabel = (p) => (STATE_LABEL[p] ? STATE_LABEL[p]() : p);

  // Mémoire « 4Gi », « 2048Mi », « 8G » (Harvester) ou en octets (KVM).
  const UNITS = { Ki: 2 ** 10, Mi: 2 ** 20, Gi: 2 ** 30, Ti: 2 ** 40, K: 1e3, M: 1e6, G: 1e9, T: 1e12 };
  function bytes(v) {
    if (typeof v.memory_bytes === 'number') return v.memory_bytes;
    const r = /^(\d+(?:\.\d+)?)([KMGT]i?)?$/.exec(String(v.memory || ''));
    return r ? parseFloat(r[1]) * (UNITS[r[2]] || 1) : -1;
  }
  function memText(v) {
    if (v.memory) return v.memory;
    const b = v.memory_bytes;
    if (typeof b !== 'number') return '–';
    const u = ['B', 'Ki', 'Mi', 'Gi', 'Ti'];
    let i = 0, x = b;
    while (x >= 1024 && i < u.length - 1) { x /= 1024; i += 1; }
    return `${Number.isInteger(x) ? x : x.toFixed(1)}${u[i]}`;
  }
  const ipKey = (v) => ((v.ips || [])[0] || '').split('.').map((x) => x.padStart(3, '0')).join('.');
  const SORT = {
    cluster: src, kind: (v) => v.kind, namespace: (v) => v.namespace || '', name: (v) => v.name,
    phase: (v) => v.phase || 'Stopped', cpu: (v) => v.cpu || 0, memory: bytes,
    ip: ipKey, node: (v) => v.node || '', folder: (v) => v.folder || '￿',
  };

  /** Lignes du périmètre de l'adresse (dossier, non rangées, famille, hôte). */
  function inScope(rows) {
    let out = rows;
    if (scope.kind) out = out.filter((v) => v.kind === scope.kind && (!scope.host || src(v) === scope.host));
    if (scope.id === 'unfiled') out = out.filter((v) => !v.folder);
    else if (scope.id != null) {
      const path = window.Folders ? Folders.pathOf(scope.id) : null;
      out = path ? out.filter((v) => Folders.within(v.folder, path)) : [];
    }
    return out;
  }

  function visible() {
    if (!data) return [];
    const words = prefs.text.toLowerCase().split(/\s+/).filter(Boolean);
    const rows = inScope(data.vms).filter((v) => {
      if (prefs.clusters.length && !prefs.clusters.includes(src(v))) return false;
      if (prefs.phase && bucket(v) !== prefs.phase) return false;
      if (prefs.ns && v.namespace !== prefs.ns) return false;
      if (!words.length) return true;
      const hay = [v.name, v.namespace || '', src(v), v.kind, v.node || '', v.folder || '', ...(v.ips || []),
        ...Object.entries(v.labels || {}).map(([k, val]) => `${k}=${val}`)].join(' ').toLowerCase();
      return words.every((w) => hay.includes(w));
    });
    const get = SORT[prefs.sort] || SORT.name;
    const dir = prefs.dir === 'desc' ? -1 : 1;
    return rows.sort((a, b) => {
      const x = get(a); const y = get(b);
      const c = typeof x === 'number' && typeof y === 'number' ? x - y
        : String(x).localeCompare(String(y), undefined, { numeric: true, sensitivity: 'base' });
      return (c || a.name.localeCompare(b.name) || src(a).localeCompare(src(b))) * dir;
    });
  }

  // -- squelette (monté une fois : la saisie du filtre n'est jamais perdue) --
  function skeleton() {
    const th = (k, label, tip, cls = '') => `<th class="sortable tip ${cls}" data-avm-sort="${k}" data-tip="${esc(tip)}"
        title="${esc(tip)}"><span>${esc(label)}</span> <span class="sort-arrow"></span></th>`;
    host.innerHTML = `<div class="vmf-layout">
      <aside class="vmf-side" data-avm="panel" aria-label="${esc(tr('vmf.panelLabel'))}"></aside>
      <div class="vmf-main">
      <div class="vmf-scope" data-avm="scope"></div>
      <div class="avm-clusters" data-avm="clusters" role="group" aria-label="${esc(tr('avm.filter.clusterTip'))}"></div>
      <div class="avm-toolbar">
        <label class="avm-field tip" data-tip="${esc(tr('avm.filter.phaseTip'))}">
          <span>${esc(tr('avm.filter.phase'))}</span>
          <select data-avm="phase">
            <option value="">${esc(tr('avm.phase.all'))}</option>
            ${PHASES.map(([v, l]) => `<option value="${v}">${esc(l())}</option>`).join('')}
          </select>
        </label>
        <label class="avm-field tip" data-tip="${esc(tr('avm.filter.nsTip'))}">
          <span>${esc(tr('col.namespace'))}</span>
          <select data-avm="ns"><option value="">${esc(tr('avm.filter.nsAll'))}</option></select>
        </label>
        <input type="search" class="avm-search tip" data-avm="text" autocomplete="off"
               placeholder="${esc(tr('avm.filter.textPh'))}" data-tip="${esc(tr('avm.filter.textTip'))}">
        <button type="button" class="btn btn-sm tip" data-avm="refresh" data-tip="${esc(tr('avm.refreshTip'))}"
                aria-label="${esc(tr('avm.refreshTip'))}">${icon('refresh')}</button>
        <span class="avm-count" data-avm="count"></span>
      </div>
      <div class="bulk-toolbar avm-bulk" data-avm="bulk" hidden>
        <span class="bulk-count" data-avm="selcount"></span>
        <span data-avm="bulkfolder"></span>
        <button class="btn btn-sm btn-primary tip" data-avm-bulk="start" data-tip="${esc(tr('avm.bulk.startTip'))}">
          <span class="icon-green">${icon('play')}</span> ${esc(tr('namespaces.bulkStart'))}</button>
        <button class="btn btn-sm btn-danger tip" data-avm-bulk="stop" data-tip="${esc(tr('avm.bulk.stopTip'))}">
          <span class="icon-red">${icon('stop')}</span> ${esc(tr('namespaces.bulkStop'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="restart" data-tip="${esc(tr('vma.bulk.restartTip'))}">
          ${icon('restart')} ${esc(tr('vma.restart'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="force-stop" data-tip="${esc(tr('vma.bulk.forceStopTip'))}">
          ${icon('power')} ${esc(tr('vma.forceStop'))}</button>
        <button class="btn btn-sm btn-secondary tip" data-avm-bulk="migrate" data-tip="${esc(tr('vma.bulk.migrateTip'))}">
          ${icon('migrate')} ${esc(tr('vma.migrate'))}</button>
        <button class="btn btn-sm tip" data-avm-bulk="clear" data-tip="${esc(tr('avm.bulk.clearTip'))}">
          ${esc(tr('namespaces.clearSelection'))}</button>
      </div>
      <table class="data-table ns-vm-table avm-table">
        <thead><tr>
          <th class="check-col"><input type="checkbox" data-avm="all" class="tip"
              data-tip="${esc(tr('vms.tip.checkAll'))}" title="${esc(tr('vms.tip.checkAll'))}"></th>
          ${th('cluster', tr('vmf.col.host'), tr('vmf.tip.sortHost'))}
          ${th('kind', tr('vmf.col.family'), tr('vmf.tip.sortFamily'))}
          ${th('namespace', tr('col.namespace'), tr('avm.tip.sortNs'))}
          ${th('name', tr('col.vm'), tr('vms.tip.sortName'))}
          ${th('phase', tr('col.phase'), tr('vms.tip.sortPhase'))}
          ${th('cpu', tr('col.cpu'), tr('vms.tip.sortCpu'), 'num')}
          ${th('memory', tr('col.memory'), tr('vms.tip.sortMemory'), 'num')}
          ${th('ip', tr('col.ip'), tr('vms.tip.sortIp'))}
          ${th('node', tr('col.node'), tr('vms.tip.sortNode'))}
          ${th('folder', tr('vmf.col.folder'), tr('vmf.tip.sortFolder'))}
          <th>${esc(tr('col.actions'))}</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <p class="avm-more"><button type="button" class="btn btn-sm btn-secondary tip" data-avm="more" hidden></button></p>
      <p class="hint avm-empty" data-avm="empty" hidden></p>
      <p class="hint vmf-viewer" data-avm="viewer" hidden></p>
      <pre class="live-log avm-log" data-avm="log" hidden></pre>
      </div></div>`;
    const q = (s) => host.querySelector(s);
    q('[data-avm="phase"]').value = prefs.phase;
    q('[data-avm="text"]').value = prefs.text;
    q('[data-avm="phase"]').addEventListener('change', (e) => { prefs.phase = e.target.value; savePrefs(); render(); });
    q('[data-avm="ns"]').addEventListener('change', (e) => { prefs.ns = e.target.value; savePrefs(); render(); });
    // la recherche attend la fin de la frappe : un rendu par lettre figeait la vue
    q('[data-avm="text"]').addEventListener('input', (e) => {
      prefs.text = e.target.value;
      savePrefs();
      clearTimeout(searchTimer);
      searchTimer = setTimeout(render, SEARCH_MS);
    });
    q('[data-avm="more"]').addEventListener('click', showMore);
    q('[data-avm="refresh"]').addEventListener('click', () => load({ fresh: true }));
    q('[data-avm="all"]').addEventListener('change', (e) => {
      for (const v of visible()) { if (e.target.checked) selection.add(keyOf(v)); else selection.delete(keyOf(v)); }
      render();
    });
    host.querySelectorAll('[data-avm-sort]').forEach((h) => h.addEventListener('click', () => {
      const k = h.dataset.avmSort;
      if (prefs.sort === k) prefs.dir = prefs.dir === 'asc' ? 'desc' : 'asc';
      else { prefs.sort = k; prefs.dir = 'asc'; }
      savePrefs(); render();
    }));
    host.querySelectorAll('[data-avm-bulk]').forEach((b) => b.addEventListener('click', () => bulk(b.dataset.avmBulk)));
    q('[data-avm="bulkfolder"]').addEventListener('click', (e) => {
      if (e.target.closest('[data-avm-move]') && !e.target.closest('button').disabled) moveRows([...selection]);
    });
    q('[data-avm="clusters"]').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-avm-cluster]');
      if (!chip) return;
      const c = chip.dataset.avmCluster;
      if (c === '*') prefs.clusters = [];
      else if (prefs.clusters.includes(c)) prefs.clusters = prefs.clusters.filter((x) => x !== c);
      else prefs.clusters = [...prefs.clusters, c];
      savePrefs(); render();
    });
    host.querySelector('tbody').addEventListener('change', (e) => {
      const cb = e.target.closest('input[data-avm-key]');
      if (!cb) return;
      if (cb.checked) selection.add(cb.dataset.avmKey); else selection.delete(cb.dataset.avmKey);
      cb.closest('tr').classList.toggle('selected', cb.checked);
      renderBulk();
    });
    host.querySelector('tbody').addEventListener('click', onRowAction);
  }

  // -- rendu ------------------------------------------------------------------
  const CL_STATE = {
    unreachable: ['warn', () => tr('avm.cl.unreachable'), () => tr('avm.cl.unreachableTip')],
    timeout: ['warn', () => tr('avm.cl.unreachable'), () => tr('avm.cl.unreachableTip')],
    denied: ['fail', () => tr('avm.cl.denied'), () => tr('avm.cl.deniedTip')],
    auth: ['fail', () => tr('avm.cl.denied'), () => tr('avm.cl.deniedTip')],
    hostkey: ['fail', () => tr('avm.cl.denied'), () => tr('avm.cl.deniedTip')],
    error: ['fail', () => tr('avm.cl.error'), () => tr('avm.cl.errorTip')],
    unknown: ['warn', () => tr('avm.cl.unknown'), () => tr('avm.cl.unknownTip')],
  };

  function renderClusters() {
    const box = host.querySelector('[data-avm="clusters"]');
    const all = !prefs.clusters.length;
    const chips = (data ? data.sources : []).filter((c) => !scope.kind || c.kind === scope.kind).map((c) => {
      const on = prefs.clusters.includes(c.source);
      const st = c.state === 'ok' ? null : (CL_STATE[c.state] || CL_STATE.error);
      const tip = st ? `${st[2]()}${c.error ? ` (${c.error})` : ''}` : tr('avm.cl.tip', { n: c.count || 0 });
      return `<button type="button" class="avm-chip tip${on ? ' active' : ''}${st ? ` avm-chip-${st[0]}` : ''}"
          data-avm-cluster="${esc(c.source)}" data-avm-kind="${esc(c.kind)}" aria-pressed="${on}" data-tip="${esc(tip)}">
          ${icon(c.kind === 'kvm' ? 'compute' : 'node', 13)} <span>${esc(c.source)}</span>
          ${st ? `<span class="badge ${st[0]}">${esc(st[1]())}</span>` : `<span class="avm-chip-n">${c.count || 0}</span>`}
        </button>`;
    }).join('');
    box.innerHTML = `<button type="button" class="avm-chip tip${all ? ' active' : ''}" data-avm-cluster="*"
        aria-pressed="${all}" data-tip="${esc(tr('avm.filter.allClustersTip'))}">${esc(tr('avm.filter.allClusters'))}</button>${chips}`;
  }

  function renderNamespaces() {
    const sel = host.querySelector('[data-avm="ns"]');
    const names = [...new Set((data ? data.vms : [])
      .filter((v) => v.namespace && (!prefs.clusters.length || prefs.clusters.includes(src(v)))).map((v) => v.namespace))].sort();
    if (prefs.ns && !names.includes(prefs.ns)) names.unshift(prefs.ns);
    sel.innerHTML = `<option value="">${esc(tr('avm.filter.nsAll'))}</option>`
      + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    sel.value = prefs.ns;
  }

  function scopeTitle() {
    if (scope.kind) {
      if (scope.host) return scope.host;
      return scope.kind === 'kvm' ? tr('hnav.fam.kvm') : tr('hnav.fam.harvester');
    }
    if (scope.id === 'unfiled') return tr('fld.unfiled');
    if (scope.id != null) return Folders.pathOf(scope.id) || tr('vmf.folderGone');
    return tr('vmf.allVms');
  }

  function renderScope() {
    const box = host.querySelector('[data-avm="scope"]');
    const n = data ? inScope(data.vms).length : null;
    const gone = scope.id != null && scope.id !== Folders.UNFILED && Folders.get() && !Folders.pathOf(scope.id);
    box.innerHTML = `<h2 class="vmf-scope-title">${icon(scope.kind ? (scope.kind === 'kvm' ? 'compute' : 'node') : (scope.id != null ? 'folder' : 'vm'), 16)}
        <span>${esc(scopeTitle())}</span>${n == null ? '' : ` <span class="badge tip" data-tip="${esc(tr('vmf.countTip'))}">${esc(n)}</span>`}</h2>
      ${gone ? `<p class="hint warn">${icon('warn', 13)} ${esc(tr('vmf.folderGoneHint'))}</p>` : ''}`;
  }

  function renderPanel() {
    const el = host.querySelector('[data-avm="panel"]');
    if (!el || !window.Folders) return;
    const rows = data ? data.vms : [];
    const hosts = (data ? data.sources : []).map((s) => ({
      kind: s.kind, name: s.source, state: s.state === 'ok' ? 'ok' : (CL_STATE[s.state] ? (s.state === 'unknown' ? 'unknown' : 'unreachable') : 'error'),
      count: rows.filter((v) => v.kind === s.kind && src(v) === s.source).length,
    }));
    Folders.panel(el, {
      base: 'vms', current: scope.kind ? null : scope.id, mode: panelMode(), hosts,
      currentHost: scope.kind ? (scope.host ? `${scope.kind}/${scope.host}` : scope.kind) : null,
      rows: data ? rows : null,
      allLabel: tr('vmf.allVms'), allTip: tr('vmf.allVmsTip'),
      unfiledLabel: tr('fld.unfiled'), unfiledTip: tr('vmf.unfiledTip'),
      onMode: (m) => {
        try { localStorage.setItem(PANEL_STORE, m); } catch { /* sans stockage */ }
        if (m === 'folders' && scope.kind && window.Router) Router.go('#vms');
        else renderPanel();
      },
      onDrop: (keys, target) => moveRows(keys, target),
      onChanged: () => load({ fresh: true }),
    });
  }

  function folderCell(v) {
    if (!v.folder) return `<span class="res-dim">${esc(tr('fld.unfiledShort'))}</span>`;
    const f = window.Folders ? Folders.byPath(v.folder) : null;
    const link = f ? `<a href="#vms/folder/${enc(f.id)}" class="tip" title="${esc(tr('vmf.openFolderTip'))}">${esc(v.folder)}</a>`
      : `<span class="tip" title="${esc(tr('vmf.folderNotInTree'))}">${esc(v.folder)}</span>`;
    const consoleOnly = v.folder_source === 'console'
      ? ` <span class="badge warn vmf-console tip" data-tip="${esc(tr('vmf.consoleOnlyTip', { host: src(v) }))}" title="${esc(tr('vmf.consoleOnlyTip', { host: src(v) }))}">${esc(tr('vmf.consoleOnly'))}</span>` : '';
    return link + consoleOnly;
  }

  function row(v) {
    const k = keyOf(v);
    const phase = v.phase || 'Stopped';
    const kvm = v.kind === 'kvm';
    const halted = v.runStrategy === 'Halted';
    const b = (attr, ic, tip, cls = '', disabled = false) => `<button type="button" class="btn-icon-action ${cls} tip" data-avm-act="${attr}"
        data-tip="${esc(tip)}" aria-label="${esc(tip)}"${disabled ? ' disabled' : ''}>${ic}</button>`;
    const ips = v.ips || [];
    const plan = dr && v.uuid ? dr[v.uuid] : null;
    const op = window.Folders && Folders.canOperate();
    const fam = kvm ? 'KVM' : 'Harvester';
    const hostCell = kvm ? `<a class="avm-cluster" href="#kvm/${enc(src(v))}/vms" title="${esc(tr('vmf.hostTip', { host: src(v) }))}">${esc(src(v))}</a>`
      : `<span class="avm-cluster">${esc(src(v))}</span>`;
    return `<tr data-avm-row="${esc(k)}" class="${selection.has(k) ? 'selected' : ''}">
      <td class="check-col"><input type="checkbox" data-avm-key="${esc(k)}" ${selection.has(k) ? 'checked' : ''}
          aria-label="${esc(`${src(v)}/${v.namespace ? `${v.namespace}/` : ''}${v.name}`)}"></td>
      <td>${hostCell}</td>
      <td><span class="badge dim vmf-family vmf-family-${esc(v.kind)}">${esc(fam)}</span></td>
      <td>${v.namespace ? esc(v.namespace) : '<span class="res-dim">–</span>'}</td>
      <td data-avm-col="name">${esc(v.name)}${v.agent_connected === 'True' ? ` <span class="agent-dot ok tip" data-tip="${esc(tr('vm.tooltip.agentOk'))}">${icon('dotOn', 10)}</span>` : ''}${plan ? ` <a class="badge info vmf-dr tip" href="#dr/${enc(plan)}" data-tip="${esc(tr('vmf.drTip'))}" title="${esc(tr('vmf.drTip'))}">DR</a>` : ''}</td>
      <td><span class="phase ${esc(phase)} tip" data-tip="${esc(kvm ? (v.state || phase) : tr('avm.tip.strategy', { s: v.runStrategy || '?' }))}">${esc(phaseLabel(phase))}</span></td>
      <td class="num">${v.cpu ? esc(v.cpu) : '–'}</td>
      <td class="num">${esc(memText(v))}</td>
      <td>${ips.length ? `<code>${esc(ips[0])}</code>${ips.length > 1 ? ` <span class="res-dim tip" data-tip="${esc(ips.slice(1).join(', '))}">+${ips.length - 1}</span>` : ''}` : '<span class="res-dim">–</span>'}</td>
      <td>${v.node ? esc(v.node) : '<span class="res-dim">–</span>'}</td>
      <td class="vmf-folder">${folderCell(v)}</td>
      <td class="vm-actions-cell">
        ${kvm ? '' : (halted ? b('start', `<span class="icon-green">${icon('play')}</span>`, tr('vm.tooltip.start'), 'start')
                 : b('stop', `<span class="icon-red">${icon('stop')}</span>`, tr('vm.tooltip.stop'), 'stop'))}
        ${b('console', icon('console'), tr('vm.tooltip.console'), 'console')}
        ${kvm ? '' : b('edit', icon('settings'), tr('vm.tooltip.edit'), 'edit')}
        ${kvm ? '' : b('notes', icon('notes'), tr('vm.tooltip.notes'), 'notes')}
        ${b('folder', icon('folder'), op ? tr('fld.moveOneTip') : tr('fld.operatorOnly'), 'folder', !op)}
        ${b('more', icon('more'), kvm ? tr('vmf.kvmMenuTip') : tr('vma.moreTip'), 'more')}
      </td></tr>`;
  }

  function renderBulk(vis = visible()) {
    const bar = host.querySelector('[data-avm="bulk"]');
    // une VM sortie de la liste (supprimée, source éteinte) quitte la sélection
    const known = new Set((data ? data.vms : []).map(keyOf));
    for (const k of [...selection]) if (!known.has(k)) selection.delete(k);
    bar.hidden = !selection.size;
    const rows = (data ? data.vms : []).filter((v) => selection.has(keyOf(v)));
    const sources = new Set(rows.map(src));
    host.querySelector('[data-avm="selcount"]').textContent =
      tr('avm.selected', { n: selection.size, c: sources.size });
    const fb = host.querySelector('[data-avm="bulkfolder"]');
    if (window.Folders) fb.innerHTML = Folders.moveButton('data-avm-move="1"', tr('fld.move'));
    const all = host.querySelector('[data-avm="all"]');
    all.checked = vis.length > 0 && vis.every((v) => selection.has(keyOf(v)));
  }

  function render() {
    if (!host) return;
    renderPanel();
    renderScope();
    renderClusters();
    renderNamespaces();
    const rows = visible();
    const tbody = host.querySelector('tbody');
    // une autre liste (filtre, tri, périmètre) repart des PAGE premières lignes
    const key = JSON.stringify([prefs.clusters, prefs.phase, prefs.ns, prefs.text, prefs.sort, prefs.dir, scope]);
    if (key !== listKey) { listKey = key; limit = PAGE; }
    draw(tbody, rows.slice(0, limit));
    if (window.Folders) {
      Folders.draggableRows(tbody, 'tr[data-avm-row]', (tr_) => {
        const k = tr_.dataset.avmRow;
        return selection.has(k) ? [...selection] : [k];
      });
    }
    host.querySelectorAll('[data-avm-sort]').forEach((h) => {
      const on = h.dataset.avmSort === prefs.sort;
      h.classList.toggle('active-sort', on);
      h.querySelector('.sort-arrow').innerHTML = on ? icon(prefs.dir === 'asc' ? 'arrowUp' : 'arrowDown', 12) : '';
      h.setAttribute('aria-sort', on ? (prefs.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
    renderMore(rows.length);
    const total = data ? inScope(data.vms).length : 0;
    host.querySelector('[data-avm="count"]').textContent = data ? tr('avm.count', { n: rows.length, total }) : tr('common.loading');
    const empty = host.querySelector('[data-avm="empty"]');
    empty.hidden = !data || rows.length > 0;
    empty.textContent = total ? tr('avm.emptyFiltered') : (scope.id != null ? tr('vmf.emptyFolder') : tr('avm.empty'));
    const viewer = host.querySelector('[data-avm="viewer"]');
    viewer.hidden = !window.Folders || Folders.canOperate();
    viewer.textContent = tr('vmf.viewerNote');
    renderBulk(rows);
  }

  // glisser une ligne (ou la sélection qui la contient) sur un dossier :
  // les lignes portent draggable, un seul écouteur sur le tableau
  function rowsHtml(list) {
    const drag = window.Folders && Folders.canOperate();
    return list.map((v) => (drag ? row(v).replace('<tr ', '<tr draggable="true" ') : row(v)));
  }

  /** Dessine `list` : mêmes lignes dans le même ordre, seules celles dont le
   *  HTML a changé sont remplacées (une relecture où une VM change d'état ne
   *  refait pas les 300 lignes) ; sinon le corps entier. */
  function draw(tbody, list) {
    const keys = list.map(keyOf);
    const html = rowsHtml(list);
    const same = keys.length === drawn.keys.length && tbody.rows.length === keys.length
      && keys.every((k, i) => k === drawn.keys[i]);
    if (same) {
      for (let i = 0; i < html.length; i += 1) if (html[i] !== drawn.html[i]) tbody.rows[i].outerHTML = html[i];
    } else {
      tbody.innerHTML = html.join('');
    }
    drawn = { keys, html };
  }

  function renderMore(n) {
    const b = host.querySelector('[data-avm="more"]');
    const next = Math.min(PAGE, n - limit);
    b.hidden = next <= 0;
    if (next <= 0) return;
    b.textContent = tr('avm.more', { n: next });
    const tip = tr('avm.moreTip', { shown: limit, total: n });
    b.title = tip;
    b.dataset.tip = tip;
  }

  /** Ajoute les PAGE lignes suivantes sans redessiner les précédentes. */
  function showMore() {
    const rows = visible();
    const from = limit;
    limit += PAGE;
    const more = rows.slice(from, limit);
    const html = rowsHtml(more);
    host.querySelector('tbody').insertAdjacentHTML('beforeend', html.join(''));
    drawn = { keys: drawn.keys.concat(more.map(keyOf)), html: drawn.html.concat(html) };
    renderMore(rows.length);
    renderBulk(rows);
  }

  // -- ranger -------------------------------------------------------------------
  const objectOf = (v) => ({
    key: v.key, host: v.kind === 'kvm' ? src(v) : undefined,
    label: `${v.name} (${v.kind === 'kvm' ? 'KVM' : 'Harvester'} · ${src(v)}${v.namespace ? `/${v.namespace}` : ''})`,
  });

  function moveRows(keys, target) {
    if (!data || !window.Folders) return;
    const objs = data.vms.filter((v) => keys.includes(keyOf(v))).map(objectOf);
    if (!objs.length) return;
    Folders.openMove(objs, { target: target === undefined ? null : target, onDone: () => load({ fresh: true }) });
  }

  // -- gestes -------------------------------------------------------------------
  function findRow(el) {
    const tr_ = el.closest('[data-avm-row]');
    if (!tr_ || !data) return null;
    return data.vms.find((v) => keyOf(v) === tr_.dataset.avmRow) || null;
  }

  async function setStrategy(v, target) {
    const r = await fetch(`/api/vm/${enc(v.cluster)}/${enc(v.ns)}/${enc(v.name)}/runStrategy`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ runStrategy: target }),
    });
    if (!r.ok) {
      let d = {};
      try { d = await r.json(); } catch { /* sans corps */ }
      throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    }
  }

  const later = (ms = 1200) => setTimeout(() => load(), ms);

  // le menu d'une VM KVM : console, fenêtres de kvm-vm.js, page de l'hôte
  let kmenu = null;
  function closeKvmMenu() {
    if (!kmenu) return;
    kmenu.el.remove();
    document.removeEventListener('pointerdown', kmenu.outside, true);
    document.removeEventListener('keydown', kmenu.keys, true);
    if (kmenu.anchor && kmenu.anchor.isConnected) kmenu.anchor.focus();
    kmenu = null;
  }
  function openKvmMenu(anchor, v) {
    if (kmenu && kmenu.anchor === anchor) { closeKvmMenu(); return; }
    closeKvmMenu();
    const el = document.createElement('div');
    el.className = 'vma-menu kvm-menu vmf-kvm-menu';
    el.setAttribute('role', 'menu');
    el.setAttribute('aria-label', tr('kvm.menuLabel', { name: v.name }));
    const item = (act, ic, label, tip) => `<button type="button" class="vma-item tip${act === 'delete' ? ' danger' : ''}" role="menuitem"
        data-vmf-kvm="${act}" data-tip="${esc(tip)}"><span class="vma-ic">${icon(ic)}</span> ${esc(label)}</button>`;
    el.innerHTML = `<div class="vma-head">${icon('vm', 13)} <strong>${esc(v.name)}</strong> <span class="res-dim">${esc(src(v))} · ${esc(v.state || v.phase || '')}</span></div>
      <div class="vma-group">
        ${item('console', 'console', tr('kvm.console'), tr('kvm.consoleTip'))}
        ${item('snapshots', 'snapshot', tr('kvm.op.snapshots'), tr('kvm.op.snapshotsTip'))}
        ${item('clone', 'clone', tr('kvm.op.clone'), tr('kvm.op.cloneTip'))}
        ${item('delete', 'trash', tr('kvm.op.delete'), tr('kvm.op.deleteTip'))}</div>
      <div class="vma-group">${item('page', 'compute', tr('vmf.kvmOpenHost'), tr('vmf.kvmOpenHostTip', { host: src(v) }))}</div>`;
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const wd = el.offsetWidth || 260, h = el.offsetHeight || 200;
    let left = r.right - wd, top = r.bottom + 4;
    if (left < 8) left = 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    el.style.left = `${left + window.scrollX}px`;
    el.style.top = `${top + window.scrollY}px`;
    kmenu = {
      el, anchor,
      outside: (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) closeKvmMenu(); },
      keys: (e) => { if (e.key === 'Escape') { e.preventDefault(); closeKvmMenu(); } },
    };
    document.addEventListener('pointerdown', kmenu.outside, true);
    document.addEventListener('keydown', kmenu.keys, true);
    el.querySelector('.vma-item')?.focus();
    el.addEventListener('click', (e) => {
      const b = e.target.closest('[data-vmf-kvm]');
      if (!b) return;
      const act = b.dataset.vmfKvm;
      closeKvmMenu();
      if (act === 'console' && window.VMConsole && VMConsole.openKvm) VMConsole.openKvm(src(v), v.name);
      else if (act === 'snapshots' && window.KVMVm) KVMVm.openSnapshots(src(v), v.name);
      else if (act === 'clone' && window.KVMVm) KVMVm.openClone(src(v), v.name);
      else if (act === 'delete' && window.KVMVm) KVMVm.openDelete(src(v), v.name);
      else if (act === 'page' && window.Router) Router.go(`#kvm/${enc(src(v))}/vms`);
    });
  }

  function onRowAction(e) {
    const btn = e.target.closest('[data-avm-act]');
    if (!btn || btn.disabled) return;
    const row_ = findRow(btn);
    if (!row_) return;
    const act = btn.dataset.avmAct;
    if (act === 'folder') { moveRows([keyOf(row_)]); return; }
    if (row_.kind === 'kvm') {
      if (act === 'console' && window.VMConsole && VMConsole.openKvm) VMConsole.openKvm(src(row_), row_.name);
      else if (act === 'more') openKvmMenu(btn, row_);
      return;
    }
    const v = { cluster: src(row_), ns: row_.namespace, name: row_.name };
    if (act === 'start' || act === 'stop') {
      setStrategy(v, act === 'start' ? 'Always' : 'Halted').then(() => later(600))
        .catch((err) => alert(tr('avm.actFailed', { vm: `${v.cluster}/${v.ns}/${v.name}`, msg: err.message })));
    } else if (act === 'console' && window.VMConsole) VMConsole.open(v.cluster, v.ns, v.name);
    else if (act === 'snapshot' && window.VMSnapshots) VMSnapshots.open(v.cluster, v.ns, v.name);
    else if (act === 'migrate' && window.VMMigrate) VMMigrate.open(v.cluster, v.ns, v.name);
    else if (act === 'edit' && window.VMEdit) VMEdit.open(v.cluster, v.ns, v.name);
    else if (act === 'notes' && window.Notes) Notes.open('vm', v.cluster, v.ns, v.name);
    else if (act === 'more' && window.VMActions) VMActions.open(btn, v.cluster, v.ns, v.name, { onDone: () => later(800) });
  }

  async function bulk(action) {
    if (action === 'clear') { selection.clear(); render(); return; }
    if (!selection.size || !data) return;
    // gestes de cycle de vie des VMs Harvester ; une VM KVM se pilote depuis son hôte
    const rows = data.vms.filter((v) => selection.has(keyOf(v)));
    const harv = rows.filter((v) => v.kind !== 'kvm');
    const skipped = rows.length - harv.length;
    const byCluster = new Map();
    for (const v of harv) {
      if (!byCluster.has(src(v))) byCluster.set(src(v), []);
      byCluster.get(src(v)).push(`${v.namespace}/${v.name}`);
    }
    const label = { start: tr('namespaces.bulkStart'), stop: tr('namespaces.bulkStop'), restart: tr('vma.restart'),
                    'force-stop': tr('vma.forceStop'), migrate: tr('vma.migrate') }[action] || action;
    const log = host.querySelector('[data-avm="log"]');
    if (!harv.length) { log.hidden = false; log.textContent = tr('vmf.bulkKvmSkipped', { n: skipped }); return; }
    if (!confirm(tr('avm.confirm', { action: label, n: harv.length, c: byCluster.size }))) return;
    log.hidden = false;
    log.textContent = skipped ? `${tr('vmf.bulkKvmSkipped', { n: skipped })}\n` : '';
    for (const [cluster, refs] of byCluster) {
      if (action === 'start' || action === 'stop') {
        for (const ref of refs) {
          const [ns, name] = ref.split('/');
          const line = document.createElement('div');
          line.textContent = `${cluster}/${ns}/${name}: ${label}… `;
          log.appendChild(line);
          try {
            await setStrategy({ cluster, ns, name }, action === 'start' ? 'Always' : 'Halted');
            line.insertAdjacentHTML('beforeend', icon('ok'));
          } catch (err) {
            line.insertAdjacentHTML('beforeend', `${icon('fail')} ${esc(err.message)}`);
          }
        }
      } else if (window.VMActions) {
        const head = document.createElement('div');
        head.className = 'avm-log-cluster';
        head.textContent = cluster;
        log.appendChild(head);
        const part = document.createElement('div');
        log.appendChild(part);
        await VMActions.bulk(cluster, refs, action, part);
      }
    }
    later(1500);
  }

  // -- cycle ----------------------------------------------------------------------
  async function loadDr() {
    if (dr) return;
    dr = {};
    try {
      const r = await fetch('/api/replication/plans');
      const d = r.ok ? await r.json() : {};
      (d.plans || []).forEach((p) => (p.resources || []).forEach((res) => { if (res.resource_uid) dr[res.resource_uid] = p.id; }));
    } catch { /* sans réplication : aucune pastille */ }
    renderSoon();
  }

  async function load(opts = {}) {
    if (!host || loading) return;
    loading = true;
    try {
      const r = await fetch(`/api/vms-all?kinds=harvester,kvm${opts.fresh ? '&fresh=1' : ''}`);
      if (r.ok) {
        const raw = await r.text();
        // relecture identique (signature = le corps) : rien ne se redessine
        if (raw === lastRaw && data) { loading = false; return; }
        data = normalize(JSON.parse(raw));
        lastRaw = raw;
      }
    } catch { /* console injoignable : on garde la dernière liste */ }
    loading = false;
    render();
  }

  /** Monte la vue. `s` : périmètre de l'adresse { id, kind, host }. */
  function start(el, s) {
    if (!el) return;
    const next = Object.assign({ id: null, kind: null, host: null }, s || {});
    const lang = window.i18n ? i18n.currentLang : '';
    if (host !== el || builtLang !== lang) {
      host = el;
      builtLang = lang;
      loadPrefs();
      skeleton();
    }
    if (next.id !== scope.id || next.kind !== scope.kind || next.host !== scope.host) selection.clear();
    scope = next;
    render();
    if (window.Folders) Folders.load();
    load();
    loadDr();
    clearInterval(timer);
    timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  }

  function stop() {
    clearInterval(timer);
    timer = null;
    closeKvmMenu();
  }

  // les événements en rafale (arborescence relue, rôle, contrats DR) ne
  // redessinent qu'une fois par image
  let pending = 0;
  function renderSoon() {
    if (pending) return;
    pending = requestAnimationFrame(() => { pending = 0; render(); });
  }
  document.addEventListener('hops:folders', () => { if (host && host.isConnected && timer) renderSoon(); });
  document.addEventListener('hops:folders-changed', () => { if (host && timer) load({ fresh: true }); });
  document.addEventListener('hops:role', () => { if (host && timer) renderSoon(); });

  /** Dernière lecture de /api/vms-all (ou null) : la recherche du menu des
   *  hôtes y trouve les VMs sans relancer de lecture. */
  const cached = () => data;
  const currentScope = () => Object.assign({}, scope);
  return { start, stop, load, cached, scope: currentScope, _visible: visible };
})();

window.AllVMs = AllVMs;
