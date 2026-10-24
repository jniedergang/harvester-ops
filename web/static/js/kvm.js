/**
 * harvester-ops : section KVM (v1.96.0).
 *
 * Les hyperviseurs KVM/libvirt déclarés dans la console, à côté des clusters
 * Harvester. Quatre vues, une par onglet du groupe KVM :
 *   - Hôtes : un bloc par hôte (CPU, mémoire, versions, VMs par état,
 *     étiquettes, erreur classée quand il est injoignable), Tester (opérateur),
 *     Retirer (admin, confirmation dans la page), « Déclarer un hôte » (admin).
 *   - Machines virtuelles : toutes les VMs de tous les hôtes, filtres, tri,
 *     menu des six gestes (grisés quand l'état ne s'y prête pas, la table des
 *     refus vient du serveur, celle du script), arrêt forcé confirmé dans la
 *     page, détail dépliable.
 *   - Stockage : deux sous-onglets. « Pools et volumes » : pools de chaque
 *     hôte (créer, démarrer, arrêter, rafraîchir, autostart, supprimer) et
 *     leurs volumes (créer, redimensionner, supprimer). « Images » : le
 *     catalogue de chaque hôte (téléverser, télécharger depuis une URL,
 *     vérifier, créer une VM, supprimer, pools d'images).
 *   - Réseaux : réseaux libvirt de chaque hôte (créer, démarrer, arrêter,
 *     autostart, supprimer), ponts et cartes de l'hôte en lecture.
 *   Les fenêtres de ces deux vues sont dans kvm-storage.js ; les gestes
 *   simples (démarrer, arrêter...) sont suivis dans la ligne d'état de la vue.
 *
 * Toute écriture est une action suivie que le serveur lance par
 * bin/harvester-kvm.py ; on la suit par SSEReconnect (dock, Activity) et la
 * fin porte le résultat du script (`result.output` : points du test,
 * `confirmed`, `gone`, `suggest`).
 */
const KVM = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 30000;
  const VIEWS = ['hosts', 'vms', 'storage', 'networks'];
  const GESTURES = ['start', 'shutdown', 'reboot', 'suspend', 'resume', 'destroy'];
  const NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$/;
  const USER_RE = /^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,63}$/;
  const LABEL_RE = /^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,62}$/;

  // Clés en toutes lettres : le contrôle de parité i18n ne lit que des littéraux.
  const STATE_LABEL = {
    running: () => tr('kvm.state.running'), idle: () => tr('kvm.state.idle'),
    paused: () => tr('kvm.state.paused'), 'in shutdown': () => tr('kvm.state.inShutdown'),
    'shut off': () => tr('kvm.state.shutOff'), crashed: () => tr('kvm.state.crashed'),
    pmsuspended: () => tr('kvm.state.pmsuspended'), blocked: () => tr('kvm.state.blocked'),
    'no state': () => tr('kvm.state.noState'),
  };
  const STATE_BADGE = { running: 'ok', idle: 'ok', paused: 'warn', pmsuspended: 'warn', blocked: 'warn',
                        'in shutdown': 'info', 'shut off': 'dim', crashed: 'fail', 'no state': 'dim' };
  const STATE_ORDER = ['running', 'idle', 'blocked', 'paused', 'pmsuspended', 'in shutdown', 'crashed', 'shut off', 'no state'];
  const KIND_LABEL = {
    unreachable: () => tr('kvm.kind.unreachable'), hostkey: () => tr('kvm.kind.hostkey'),
    auth: () => tr('kvm.kind.auth'), denied: () => tr('kvm.kind.denied'), notfound: () => tr('kvm.kind.notfound'),
    timeout: () => tr('kvm.kind.timeout'), failed: () => tr('kvm.kind.failed'), invalid: () => tr('kvm.kind.invalid'),
    usage: () => tr('kvm.kind.usage'), busy: () => tr('kvm.kind.busy'),
  };
  const GESTURE_LABEL = {
    start: () => tr('kvm.g.start'), shutdown: () => tr('kvm.g.shutdown'), reboot: () => tr('kvm.g.reboot'),
    suspend: () => tr('kvm.g.suspend'), resume: () => tr('kvm.g.resume'), destroy: () => tr('kvm.g.destroy'),
  };
  const GESTURE_TIP = {
    start: () => tr('kvm.g.startTip'), shutdown: () => tr('kvm.g.shutdownTip'), reboot: () => tr('kvm.g.rebootTip'),
    suspend: () => tr('kvm.g.suspendTip'), resume: () => tr('kvm.g.resumeTip'), destroy: () => tr('kvm.g.destroyTip'),
  };
  const GESTURE_ICON = { start: 'play', shutdown: 'power', reboot: 'restart', suspend: 'pause', resume: 'play', destroy: 'stop' };
  const CHECK_BADGE = { ok: 'ok', warn: 'warn', error: 'fail', skipped: 'dim' };
  const CHECK_STATUS = {
    ok: () => tr('kvm.check.ok'), warn: () => tr('kvm.check.warn'), error: () => tr('kvm.check.error'),
    skipped: () => tr('kvm.check.skipped'), running: () => tr('kvm.check.running'), done: () => tr('kvm.check.ok'),
  };

  const stateLabel = (s) => (STATE_LABEL[s] ? STATE_LABEL[s]() : (s || '?'));
  const kindLabel = (k) => (KIND_LABEL[k] ? KIND_LABEL[k]() : (k || '?'));
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const stateBadge = (s) => badge(STATE_BADGE[s] || 'dim', stateLabel(s));
  const dim = (t) => `<span class="res-dim">${esc(t)}</span>`;

  function bytes(n) {
    if (typeof n !== 'number' || !Number.isFinite(n)) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  const kib = (k) => (typeof k === 'number' ? bytes(k * 1024) : '–');

  /** Un geste est-il possible dans cet état ? Table des refus du serveur
   *  (celle du script) ; reprendre ne vaut que pour une VM en pause. */
  function gestureWhy(gestures, g, state) {
    const refused = (gestures && gestures[g]) || [];
    // la table des refus est celle du script (servie par la console) : pas
    // de règle à part ici
    if (refused.includes(state)) return tr('kvm.why.state', { state: stateLabel(state) });
    return '';
  }

  // -- requêtes -------------------------------------------------------------------
  async function getJson(url) {
    try {
      const r = await fetch(url);
      const d = await r.json().catch(() => null);
      if (!d) return { error: `HTTP ${r.status}`, status: r.status };
      if (!r.ok) return Object.assign({ status: r.status }, d, { error: d.error || `HTTP ${r.status}` });
      return d;
    } catch (e) { return { error: e.message }; }
  }

  async function send(method, url, body, isForm) {
    const opts = { method };
    if (isForm) opts.body = body;
    else { opts.headers = { 'Content-Type': 'application/json' }; opts.body = body === undefined ? undefined : JSON.stringify(body); }
    const r = await fetch(url, opts);
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) {
      const err = new Error(d.hint || d.error || `HTTP ${r.status}`);
      err.data = d;
      throw err;
    }
    return d;
  }

  /** Suit une action : `onStep(step)` à chaque étape, `onEnd(ok, run)` à la
   *  fin, avec l'action entière (son `result.output` compris). */
  function follow(actionId, onStep, onEnd) {
    if (window.Dock && Dock.poll) Dock.poll();
    if (!window.SSEReconnect) { onEnd(false, { error_summary: 'no SSE' }); return; }
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => { try { onStep(JSON.parse(e.data)); } catch { /* ligne illisible */ } },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          onEnd(d.status === 'done', d);
        },
      },
    });
  }

  // -- cycle de vie (appelé par App.setTab) -------------------------------------
  let cur = null;   // { view, host, timer, data, details, prefs, busy }

  function start(view, host) {
    stop();
    if (!host || !VIEWS.includes(view)) return Promise.resolve();
    cur = { view, host, data: null, details: {}, pools: {}, images: {}, links: {}, openLinks: new Set(),
            prefs: { host: '', state: '', text: '', label: '', sort: 'name', dir: 1, sub: 'pools' },
            busy: 0, open: new Set() };
    shell();
    cur.timer = setInterval(() => {
      if (cur && cur.host.isConnected && !document.hidden && !cur.busy && !menu) load();
    }, REFRESH_MS);
    return load();
  }

  function stop() {
    closeMenu();
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  function shell() {
    const v = cur.view;
    const filters = v === 'vms' ? `
        <select class="tip" data-kvm-filter="host" data-tip="${esc(tr('kvm.f.hostTip'))}"><option value="">${esc(tr('kvm.f.allHosts'))}</option></select>
        <select class="tip" data-kvm-filter="state" data-tip="${esc(tr('kvm.f.stateTip'))}"><option value="">${esc(tr('kvm.f.allStates'))}</option>
          ${STATE_ORDER.map((s) => `<option value="${esc(s)}">${esc(stateLabel(s))}</option>`).join('')}</select>
        <input type="search" class="res-filter tip" data-kvm-filter="text" placeholder="${esc(tr('kvm.f.text'))}" data-tip="${esc(tr('kvm.f.textTip'))}">`
      : v === 'hosts' ? `<select class="tip" data-kvm-filter="label" data-tip="${esc(tr('kvm.f.labelTip'))}"><option value="">${esc(tr('kvm.f.allLabels'))}</option></select>`
        // Stockage et Réseaux : un hôte ou tous ; Stockage : pools ou images
        : `${v === 'storage' ? `<div class="sub-tabs sub-tabs-inline kvm-subtabs" role="tablist">
            <button type="button" class="sub-tab tip active" role="tab" data-kvm="sub" data-sub="pools" data-tip="${esc(tr('kvms.subPoolsTip'))}">${icon('storage')} <span>${esc(tr('kvms.subPools'))}</span></button>
            <button type="button" class="sub-tab tip" role="tab" data-kvm="sub" data-sub="images" data-tip="${esc(tr('kvms.subImagesTip'))}">${icon('disk')} <span>${esc(tr('kvms.subImages'))}</span></button></div>` : ''}
          <select class="tip" data-kvm-filter="host" data-tip="${esc(tr('kvms.hostFilterTip'))}"><option value="">${esc(tr('kvm.f.allHosts'))}</option></select>`;
    const declare = v === 'hosts' ? `<button type="button" class="btn btn-sm btn-primary tip" data-kvm="declare" data-tip="${esc(tr('kvm.declareTip'))}">${icon('add')} ${esc(tr('kvm.declare'))}</button>`
      // prochaine version : créer une VM (kvm-form.js), hôte choisi dans la fenêtre
      : v === 'vms' ? `<button type="button" class="btn btn-sm btn-primary tip" data-kvm="create-vm" data-tip="${esc(tr('kvmf.createVmTip'))}">${icon('add')} ${esc(tr('kvmf.createVm'))}</button>` : '';
    cur.host.innerHTML = `<div class="card na-card kvm-card" data-kvm-view="${esc(v)}">
        <div class="res-tools">${filters}
          <span class="res-count"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-kvm="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
          ${declare}
        </div>
        <div class="res-feedback kvm-feedback" data-kvm="feedback" role="status" aria-live="polite"></div>
        <div data-kvm="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.host.querySelector('.kvm-card');
    card.addEventListener('click', onClick);
    card.querySelectorAll('[data-kvm-filter]').forEach((el) => {
      el.addEventListener(el.tagName === 'INPUT' ? 'input' : 'change', () => {
        cur.prefs[el.dataset.kvmFilter] = el.value;
        render();
        // Stockage et Réseaux : seul l'hôte choisi est lu, les autres le sont au retour à « tous »
        if (el.dataset.kvmFilter === 'host' && (cur.view === 'storage' || cur.view === 'networks')) load();
      });
    });
  }

  async function load(fresh) {
    if (!cur) return;
    const c = cur;
    const q = fresh ? '?fresh=1' : '';
    const perHostView = c.view === 'storage' || c.view === 'networks';
    // Stockage et Réseaux : les déclarations (sans SSH), puis le détail de
    // chaque hôte, une lecture par hôte et non l'inventaire entier en plus
    const url = c.view === 'vms' ? `/api/kvm/vms${q}`
      : perHostView ? '/api/kvm/hosts?declared=1' : `/api/kvm/hosts${q}`;
    const d = await getJson(url);
    if (c !== cur) return;
    c.data = d;
    if (perHostView) {
      render();
      const names = (d.hosts || []).filter((h) => !h.error && (!c.prefs.host || h.name === c.prefs.host)).map((h) => h.name);
      if (c.view === 'storage' && c.prefs.sub === 'images') {
        // catalogue : relevé gardé par hôte (jusqu'à ~20 s la première fois),
        // chaque bloc s'affiche dès que son hôte a répondu
        await Promise.all(names.map(async (n) => {
          const [img, pools] = await Promise.all([getJson(`/api/kvm/hosts/${enc(n)}/images${q}`),
                                                  getJson(`/api/kvm/hosts/${enc(n)}/pools`)]);
          if (c !== cur) return;
          c.images[n] = img;
          c.pools[n] = pools;
          render();
        }));
        return;
      }
      const got = await Promise.all(names.map((n) => getJson(`/api/kvm/hosts/${enc(n)}${q}`)));
      if (c !== cur) return;
      names.forEach((n, i) => { c.details[n] = got[i]; });
      if (c.view === 'storage') {
        // type et cible de chaque pool (absents de l'inventaire) : lecture à
        // part, une vue sans elle reste utilisable
        const pools = await Promise.all(names.map((n) => getJson(`/api/kvm/hosts/${enc(n)}/pools`)));
        if (c !== cur) return;
        names.forEach((n, i) => { c.pools[n] = pools[i]; });
      }
      if (c.view === 'networks') await Promise.all([...c.openLinks].filter((n) => names.includes(n)).map((n) => loadLinks(n)));
    }
    render();
  }

  async function loadLinks(name) {
    const c = cur;
    const d = await getJson(`/api/kvm/hosts/${enc(name)}/links`);
    if (c === cur) c.links[name] = d;
  }

  const role = () => (cur && cur.data && cur.data.role) || 'viewer';
  const canOperate = () => ['operator', 'admin'].includes(role());
  const isAdmin = () => role() === 'admin';

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-kvm="body"]');
    const d = cur.data;
    const decl = cur.host.querySelector('[data-kvm="declare"]');
    if (decl) {
      decl.disabled = !isAdmin();
      decl.dataset.tip = isAdmin() ? tr('kvm.declareTip') : tr('kvm.adminOnly');
    }
    const mk = cur.host.querySelector('.res-tools [data-kvm="create-vm"]');
    if (mk) {
      mk.disabled = !canOperate() || !window.KVMForm;
      mk.dataset.tip = canOperate() ? tr('kvmf.createVmTip') : tr('kvm.operatorOnly');
    }
    if (!d || d.error) {
      body.innerHTML = `<p class="res-error">${icon('fail')} ${esc(d && d.error ? d.error : tr('fabric.unreachable'))}</p>`;
      return;
    }
    const warn = (d.warnings || []).map((w) => `<p class="res-error">${icon('warn')} ${esc(w)}</p>`).join('');
    if (cur.view === 'hosts') body.innerHTML = warn + hostsHtml(d);
    else if (cur.view === 'vms') body.innerHTML = warn + vmsHtml(d);
    else if (cur.view === 'storage' && cur.prefs.sub === 'images') body.innerHTML = warn + perHost(d, imagesHtml, imagesHead, cur.images);
    else if (cur.view === 'storage') body.innerHTML = warn + perHost(d, storageHtml, storageHead);
    else body.innerHTML = warn + perHost(d, networksHtml, networksHead);
  }

  const emptyHosts = () => `<p class="form-hint kvm-empty">${esc(tr('kvm.noHosts'))}</p>`;

  function errorBadge(err) {
    return badge('fail', kindLabel(err.kind), err.message || '');
  }

  // -- Hôtes ------------------------------------------------------------------------
  function hostsHtml(d) {
    const hosts = d.hosts || [];
    const labels = [...new Set(hosts.flatMap((h) => Object.entries(h.labels || {}).map(([k, v]) => `${k}=${v}`)))].sort();
    const sel = cur.host.querySelector('[data-kvm-filter="label"]');
    if (sel) {
      const want = [''].concat(labels).join('|');
      if ([...sel.options].map((o) => o.value).join('|') !== want) {
        sel.innerHTML = `<option value="">${esc(tr('kvm.f.allLabels'))}</option>`
          + labels.map((l) => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
      }
      if (!labels.includes(cur.prefs.label)) cur.prefs.label = '';
      sel.value = cur.prefs.label;
    }
    const shown = hosts.filter((h) => !cur.prefs.label
      || Object.entries(h.labels || {}).some(([k, v]) => `${k}=${v}` === cur.prefs.label));
    cur.host.querySelector('.res-count').textContent = tr('kvm.hostCount', { n: shown.length });
    if (!hosts.length) return emptyHosts();
    return `<div class="na-tiles kvm-hosts">${shown.map(hostTile).join('')}</div>`;
  }

  function hostTile(h) {
    const where = `${h.user ? `${h.user}@` : ''}${h.address || ''}${h.port && h.port !== 22 ? `:${h.port}` : ''}`;
    const labels = Object.entries(h.labels || {}).map(([k, v]) => badge('dim', `${k}=${v}`)).join(' ');
    const test = `<button type="button" class="btn btn-sm btn-secondary tip" data-kvm="test" data-host="${esc(h.name)}"
        ${canOperate() ? '' : 'disabled'} data-tip="${esc(canOperate() ? tr('kvm.testTip') : tr('kvm.operatorOnly'))}">${icon('test')} ${esc(tr('kvm.test'))}</button>`;
    const remove = `<button type="button" class="btn btn-sm btn-secondary tip" data-kvm="remove" data-host="${esc(h.name)}"
        ${isAdmin() ? '' : 'disabled'} data-tip="${esc(isAdmin() ? tr('kvm.removeTip') : tr('kvm.adminOnly'))}">${icon('trash')} ${esc(tr('kvm.remove'))}</button>`;
    let body;
    if (h.error) {
      body = `<div class="res-error kvm-host-error">${esc(h.error.message)}</div>`;
    } else {
      const cpu = h.cpu || {}, mem = h.memory || {};
      const counts = {};
      (h.vms || []).forEach((v) => { counts[v.state] = (counts[v.state] || 0) + 1; });
      const byState = STATE_ORDER.filter((s) => counts[s]).concat(Object.keys(counts).filter((s) => !STATE_ORDER.includes(s)))
        .map((s) => badge(STATE_BADGE[s] || 'dim', `${counts[s]} ${stateLabel(s)}`)).join(' ');
      const partly = (h.errors || []).length
        ? badge('warn', tr('kvm.partly', { n: h.errors.length }), h.errors.map((e) => `${e.command}: ${e.message}`).join(' · ')) : '';
      body = `<div class="kvm-host-facts">
          <div><span class="tip" data-tip="${esc(tr('kvm.t.cpu'))}">${icon('compute', 12)}</span> ${esc(tr('kvm.cpus', { n: cpu.cpus == null ? '?' : cpu.cpus }))}
            ${cpu.cpu_model ? dim(`${cpu.cpu_model}${cpu.mhz ? ` · ${cpu.mhz} MHz` : ''}${cpu.sockets ? ` · ${tr('kvm.sockets', { n: cpu.sockets })}` : ''}`) : ''}</div>
          <div><span class="tip" data-tip="${esc(tr('kvm.t.memory'))}">${icon('metrics', 12)}</span> ${esc(tr('kvm.memory', { total: kib(mem.total_kib), free: kib(mem.free_kib), available: kib(mem.available_kib) }))}</div>
          <div>${dim(tr('kvm.versions', { libvirt: h.libvirt_version || '?', qemu: h.qemu_version || '?' }))}</div>
          <div class="kvm-vmcount">${esc(tr('kvm.vmCount', { n: (h.vms || []).length }))} ${byState} ${partly}</div>
        </div>`;
    }
    const state = h.error ? errorBadge(h.error) : badge('ok', tr('kvm.reachable'), tr('kvm.reachableTip'));
    return `<div class="na-tile kvm-host${h.error ? ' is-error' : ''}" data-kvm-host="${esc(h.name)}">
        <div class="na-tile-title">${icon('node')} <strong>${esc(h.name)}</strong> ${state}</div>
        <div class="res-dim">${esc(h.hostname && h.hostname !== h.name ? `${h.hostname} · ${where}` : where)}</div>
        ${body}
        ${labels ? `<div class="kvm-labels">${labels}</div>` : ''}
        <div class="kvm-tile-actions">${test} ${h.error ? '' : `<button type="button" class="btn btn-sm btn-secondary tip" data-kvm="create-vm" data-host="${esc(h.name)}"
          ${canOperate() ? '' : 'disabled'} data-tip="${esc(canOperate() ? tr('kvmf.createVmHostTip', { host: h.name }) : tr('kvm.operatorOnly'))}">${icon('add')} ${esc(tr('kvmf.createVm'))}</button>`} ${remove}</div>
        <div class="of-msg kvm-tile-msg" role="status"></div>
      </div>`;
  }

  function tileOf(name) {
    return [...cur.host.querySelectorAll('[data-kvm-host]')].find((t) => t.dataset.kvmHost === name);
  }

  function checksHtml(checks) {
    return `<ul class="kvm-checks">${(checks || []).map((c) => `<li data-check="${esc(c.check)}">
        ${badge(CHECK_BADGE[c.status] || 'dim', CHECK_STATUS[c.status] ? CHECK_STATUS[c.status]() : c.status)}
        <strong>${esc(c.check)}</strong> ${esc(c.message)}</li>`).join('')}</ul>`;
  }

  async function testHost(name) {
    const tile = tileOf(name);
    const msg = tile && tile.querySelector('.kvm-tile-msg');
    if (!msg) return;
    // la vue de départ : un changement d'onglet pendant le test en crée une
    // autre, dont le compteur ne doit pas être touché
    const c = cur;
    c.busy += 1;
    msg.textContent = tr('kvm.testing');
    try {
      const out = await send('POST', `/api/kvm/hosts/${enc(name)}/test`, {});
      follow(out.action_id, (s) => { if (s.message) msg.textContent = s.message; }, (ok, run) => {
        const o = (run.result && run.result.output) || {};
        msg.innerHTML = `<span class="${ok ? '' : 'res-error'}">${icon(ok ? 'ok' : 'fail')} ${esc(ok ? tr('kvm.testOk') : (o.error || run.error_summary || tr('kvm.testFailed')))}</span>${checksHtml(o.checks)}`;
        c.busy = Math.max(0, c.busy - 1);
      });
    } catch (e) {
      msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(e.message)}</span>`;
      c.busy = Math.max(0, c.busy - 1);
    }
  }

  function confirmRemove(name) {
    const tile = tileOf(name);
    if (!tile) return;
    cur.busy += 1;
    const box = tile.querySelector('.kvm-tile-actions');
    box.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(tr('kvm.removeConfirm', { name }))}">
        <span>${esc(tr('kvm.removeConfirm', { name }))}</span>
        <button type="button" class="btn btn-sm btn-danger tip" data-kvm="remove-yes" data-host="${esc(name)}" data-tip="${esc(tr('kvm.removeYesTip'))}">${icon('trash')} ${esc(tr('kvm.removeYes'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-kvm="confirm-no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
    box.querySelector('[data-kvm="confirm-no"]').focus();
  }

  async function doRemove(name) {
    const tile = tileOf(name);
    const msg = tile.querySelector('.kvm-tile-msg');
    tile.querySelectorAll('.kvm-confirm button').forEach((b) => { b.disabled = true; });
    const c = cur;
    try {
      const out = await send('DELETE', `/api/kvm/hosts/${enc(name)}`);
      follow(out.action_id, (s) => { if (s.message) msg.textContent = s.message; }, (ok, run) => {
        msg.innerHTML = `<span class="${ok ? '' : 'res-error'}">${icon(ok ? 'ok' : 'fail')} ${esc(ok ? tr('kvm.removed', { name }) : (run.error_summary || '?'))}</span>`;
        c.busy = Math.max(0, c.busy - 1);
        if (c === cur) setTimeout(() => load(true), ok ? 900 : 0);
      });
    } catch (e) {
      msg.innerHTML = `<span class="res-error">${icon('fail')} ${esc(e.message)}</span>`;
      c.busy = Math.max(0, c.busy - 1);
    }
  }

  // -- Machines virtuelles ----------------------------------------------------------
  const SORT = {
    name: (v) => v.name.toLowerCase(), host: (v) => v.host, state: (v) => STATE_ORDER.indexOf(v.state),
    vcpus: (v) => v.vcpus || 0, memory: (v) => v.memory_kib || 0,
  };
  const vmKey = (v) => `${v.host}/${v.name}`;

  function vmsHtml(d) {
    const hosts = d.hosts || [];
    const sel = cur.host.querySelector('[data-kvm-filter="host"]');
    const names = hosts.map((h) => h.name);
    const want = [''].concat(names).join('|');
    if ([...sel.options].map((o) => o.value).join('|') !== want) {
      sel.innerHTML = `<option value="">${esc(tr('kvm.f.allHosts'))}</option>`
        + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    }
    if (!names.includes(cur.prefs.host)) cur.prefs.host = '';
    sel.value = cur.prefs.host;
    const p = cur.prefs;
    const text = p.text.trim().toLowerCase();
    const rows = (d.vms || []).filter((v) => (!p.host || v.host === p.host) && (!p.state || v.state === p.state)
      && (!text || v.name.toLowerCase().includes(text) || v.host.toLowerCase().includes(text)));
    const key = SORT[p.sort] || SORT.name;
    rows.sort((a, b) => {
      const x = key(a), y = key(b);
      return (x < y ? -1 : x > y ? 1 : 0) * p.dir || a.name.localeCompare(b.name) || a.host.localeCompare(b.host);
    });
    cur.host.querySelector('.res-count').textContent = tr('kvm.vmShown', { n: rows.length, total: (d.vms || []).length });
    const bad = hosts.filter((h) => h.state !== 'ok').map((h) => `<span class="kvm-host-state">${icon('node', 12)} ${esc(h.name)} ${badge('fail', kindLabel(h.state), h.error)}</span>`).join(' ');
    if (!hosts.length) return emptyHosts();
    const th = (k, label) => `<th${k ? ` data-sort="${k}" class="tip${p.sort === k ? (p.dir > 0 ? ' sort-asc' : ' sort-desc') : ''}" data-tip="${esc(tr('kvm.sortTip'))}"` : ''}>${esc(label)}</th>`;
    const table = rows.length ? `<table class="data-table sortable kvm-vms"><thead><tr>${th('', '')}
        ${th('name', tr('kvm.col.name'))}${th('host', tr('kvm.col.host'))}${th('state', tr('kvm.col.state'))}
        ${th('vcpus', tr('kvm.col.vcpus'))}${th('memory', tr('kvm.col.memory'))}${th('', tr('kvm.col.flags'))}${th('', '')}</tr></thead><tbody>
      ${rows.map((v) => vmRow(v)).join('')}</tbody></table>` : `<p class="form-hint kvm-empty">${esc(tr('kvm.noVms'))}</p>`;
    return `${bad ? `<div class="kvm-host-states">${bad}</div>` : ''}${table}`;
  }

  function vmRow(v) {
    const k = vmKey(v);
    const restart = window.KVMForm && KVMForm.restartFor(v);
    const flags = [v.persistent ? '' : badge('info', tr('kvm.transient'), tr('kvm.transientTip')),
      v.autostart ? badge('dim', tr('kvm.autostart'), tr('kvm.autostartTip')) : '',
      restart ? badge('warn', tr('vmf.restartNeeded'), tr('kvmf.restartListTip', { fields: restart.join(', ') })) : ''].join(' ');
    const open = cur.open.has(k);
    return `<tr data-kvm-vm="${esc(k)}" data-state="${esc(v.state)}">
        <td class="check-col"><button type="button" class="btn-icon-action tip" data-kvm="detail" data-vm="${esc(k)}" aria-expanded="${open}"
          data-tip="${esc(tr('kvm.detailTip'))}" aria-label="${esc(tr('kvm.detailTip'))}">${icon(open ? 'chevronDown' : 'chevronRight', 13)}</button></td>
        <td class="kvm-vm-name">${esc(v.name)}</td>
        <td>${esc(v.host)}</td><td>${stateBadge(v.state)}</td>
        <td>${v.vcpus == null ? '–' : esc(v.vcpus)}</td><td>${esc(kib(v.memory_kib))}</td><td>${flags}</td>
        <td class="kvm-vm-acts"><button type="button" class="btn-icon-action tip" data-kvm="menu" data-vm="${esc(k)}" aria-haspopup="menu" aria-expanded="false"
          data-tip="${esc(tr('kvm.menuTip'))}" aria-label="${esc(tr('kvm.menuLabel', { name: v.name }))}">${icon('more')}</button></td></tr>
      ${open ? `<tr class="kvm-detail-row" data-kvm-detail="${esc(k)}"><td colspan="8"><div class="kvm-detail">${esc(tr('common.loading'))}</div></td></tr>` : ''}`;
  }

  const findVm = (k) => ((cur.data && cur.data.vms) || []).find((v) => vmKey(v) === k);

  async function toggleDetail(k) {
    if (cur.open.has(k)) cur.open.delete(k); else cur.open.add(k);
    render();
    if (cur.open.has(k)) fillDetails();
  }

  async function fillDetails() {
    const c = cur;
    for (const k of [...c.open]) {
      const v = findVm(k);
      if (!v) continue;
      const d = await getJson(`/api/kvm/vms/${enc(v.host)}/${enc(v.name)}`);
      if (c !== cur) return;
      const row = [...c.host.querySelectorAll('[data-kvm-detail]')].find((r) => r.dataset.kvmDetail === k);
      if (row) row.querySelector('.kvm-detail').innerHTML = detailHtml(d);
    }
  }

  function detailHtml(d) {
    if (d.error) return `<span class="res-error">${icon('fail')} ${esc(typeof d.error === 'string' ? d.error : `${kindLabel(d.error.kind)}: ${d.error.message}`)}</span>`;
    const li = (label, value) => `<div><span class="res-dim">${esc(label)}</span> ${value}</div>`;
    const disks = (d.disks || []).map((x) => `<li>${esc(x.target || '?')} · ${esc(x.device || '')} · ${esc(x.source || '–')}${x.format ? ` (${esc(x.format)}${x.bus ? `, ${esc(x.bus)}` : ''})` : ''}</li>`).join('');
    const nics = (d.nics || []).map((x) => `<li>${esc(x.mac || '?')} · ${esc(x.type || '')} ${esc(x.source || '')}${x.model ? ` (${esc(x.model)})` : ''}</li>`).join('');
    const gfx = (d.graphics || []).map((x) => `<li>${esc(x.type || '?')} ${esc(x.listen || '')}${x.port ? `:${esc(x.port)}` : ''}${x.autoport ? ` · ${esc(tr('kvm.d.autoport'))}` : ''}</li>`).join('');
    const yes = (b) => esc(b ? tr('kvm.yes') : tr('kvm.no'));
    return `<div class="kvm-detail-grid">
        ${li(tr('kvm.col.vcpus'), esc(d.vcpus == null ? '–' : d.vcpus))}
        ${li(tr('kvm.col.memory'), esc(kib(d.memory_kib)))}
        ${li(tr('kvm.d.persistent'), yes(d.persistent))}
        ${li(tr('kvm.autostart'), yes(d.autostart))}
        ${li(tr('kvm.d.pinned'), yes(d.cpu_pinned))}
        ${li(tr('kvm.d.machine'), esc([d.machine, d.firmware].filter(Boolean).join(' · ') || '–'))}
      </div>
      <div class="kvm-detail-lists">
        <div><strong>${esc(tr('kvm.d.disks'))}</strong><ul>${disks || `<li>${dim('–')}</li>`}</ul></div>
        <div><strong>${esc(tr('kvm.d.nics'))}</strong><ul>${nics || `<li>${dim('–')}</li>`}</ul></div>
        <div><strong>${esc(tr('kvm.d.graphics'))}</strong><ul>${gfx || `<li>${dim('–')}</li>`}</ul></div>
      </div>`;
  }

  // v1.98.0 : second groupe du menu (instantanés, cloner, supprimer). La
  // table des conditions (rôle, états, persistance) est servie par le
  // serveur (`operations`), comme celle des gestes.
  const OPERATIONS = ['snapshots', 'clone', 'delete'];
  const OP_OPEN = { snapshots: 'openSnapshots', clone: 'openClone', delete: 'openDelete' };
  const OP_LABEL = { snapshots: () => tr('kvm.op.snapshots'), clone: () => tr('kvm.op.clone'), delete: () => tr('kvm.op.delete') };
  const OP_TIP = { snapshots: () => tr('kvm.op.snapshotsTip'), clone: () => tr('kvm.op.cloneTip'), delete: () => tr('kvm.op.deleteTip') };
  const OP_ICON = { snapshots: 'snapshot', clone: 'clone', delete: 'trash' };
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };

  /** Pourquoi une opération est grisée ('' : possible), d'après la table
   *  servie par le serveur. */
  function operationWhy(operations, op, v, have) {
    const t = operations && operations[op];
    if (!t) return tr('kvm.op.unavailable');
    if ((ROLE_RANK[have] || 0) < (ROLE_RANK[t.role] || 3)) return t.role === 'admin' ? tr('kvm.adminOnly') : tr('kvm.operatorOnly');
    if (t.persistent && !v.persistent) return tr('kvm.why.transient');
    if (t.states && !t.states.includes(v.state)) {
      return tr('kvm.why.needsState', { state: stateLabel(v.state), need: t.states.map(stateLabel).join(', ') });
    }
    return '';
  }

  function opItem(op, v) {
    const why = !window.KVMVm ? tr('kvm.op.unavailable') : operationWhy(cur.data.operations, op, v, role());
    return `<button type="button" class="vma-item tip${op === 'delete' ? ' danger' : ''}" role="menuitem" data-kvm-op="${op}"
        data-tip="${esc(why || OP_TIP[op]())}"${why ? ' aria-disabled="true"' : ''}>
        <span class="vma-ic">${icon(OP_ICON[op])}</span> ${esc(OP_LABEL[op]())}</button>`;
  }

  // le menu d'une VM (même allure que celui des VMs Harvester)
  let menu = null;
  function closeMenu() {
    if (!menu) return;
    menu.el.remove();
    document.removeEventListener('pointerdown', menu.outside, true);
    document.removeEventListener('keydown', menu.keys, true);
    if (menu.anchor && menu.anchor.isConnected) { menu.anchor.setAttribute('aria-expanded', 'false'); menu.anchor.focus(); }
    menu = null;
  }

  function openMenu(anchor, v) {
    if (menu && menu.anchor === anchor) { closeMenu(); return; }
    closeMenu();
    const gestures = cur.data.gestures || {};
    const el = document.createElement('div');
    el.className = 'vma-menu kvm-menu';
    el.setAttribute('role', 'menu');
    el.setAttribute('aria-label', tr('kvm.menuLabel', { name: v.name }));
    const item = (g) => {
      const why = !canOperate() ? tr('kvm.operatorOnly') : gestureWhy(gestures, g, v.state);
      return `<button type="button" class="vma-item tip${g === 'destroy' ? ' danger' : ''}" role="menuitem" data-kvm-gesture="${g}"
        data-tip="${esc(why || GESTURE_TIP[g]())}"${why ? ' aria-disabled="true"' : ''}>
        <span class="vma-ic">${icon(GESTURE_ICON[g])}</span> ${esc(GESTURE_LABEL[g]())}</button>`;
    };
    el.innerHTML = `<div class="vma-head">${icon('vm', 13)} <strong>${esc(v.name)}</strong> <span class="res-dim">${esc(v.host)} · ${esc(stateLabel(v.state))}</span></div>
      <div class="vma-group">${GESTURES.map(item).join('')}</div>
      <div class="vma-group"><button type="button" class="vma-item tip" role="menuitem" data-kvm-console="1"
        data-tip="${esc(canOperate() ? tr('kvm.consoleTip') : tr('kvm.operatorOnly'))}"${canOperate() ? '' : ' aria-disabled="true"'}>
        <span class="vma-ic">${icon('console')}</span> ${esc(tr('kvm.console'))}</button>
        ${OPERATIONS.map((op) => opItem(op, v)).join('')}</div>
      ${window.KVMForm ? KVMForm.menuGroupHtml(v, cur.data.edit_operations, role()) : ''}`;
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const wd = el.offsetWidth || 260, h = el.offsetHeight || 200;
    let left = r.right - wd, top = r.bottom + 4;
    if (left < 8) left = 8;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
    el.style.left = `${left + window.scrollX}px`;
    el.style.top = `${top + window.scrollY}px`;
    anchor.setAttribute('aria-expanded', 'true');
    menu = {
      el, anchor,
      outside: (e) => { if (!el.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) closeMenu(); },
      keys: (e) => {
        if (e.key === 'Escape') { e.preventDefault(); closeMenu(); return; }
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          const items = [...el.querySelectorAll('.vma-item')];
          const i = items.indexOf(document.activeElement);
          const n = e.key === 'ArrowDown' ? (i + 1) % items.length : (i - 1 + items.length) % items.length;
          e.preventDefault();
          items[n]?.focus();
        }
      },
    };
    document.addEventListener('pointerdown', menu.outside, true);
    document.addEventListener('keydown', menu.keys, true);
    el.querySelector('.vma-item:not([aria-disabled])')?.focus();
    // prochaine version : disques et cartes de la VM lus à l'ouverture du menu
    const hotGroup = el.querySelector('[data-kvm-hot-group]');
    if (hotGroup) KVMForm.fillMenuDevices(hotGroup, v, cur.data.edit_operations, role());
    el.addEventListener('click', (e) => {
      if (window.KVMForm && KVMForm.onMenuClick(e, v, closeMenu)) return;
      const c = e.target.closest('[data-kvm-console]');
      if (c) {
        if (c.getAttribute('aria-disabled')) return;
        closeMenu();
        // v1.98.0 : console VNC par la connexion SSH de l'hôte
        if (window.VMConsole && VMConsole.openKvm) VMConsole.openKvm(v.host, v.name);
        return;
      }
      const o = e.target.closest('[data-kvm-op]');
      if (o) {
        if (o.getAttribute('aria-disabled')) return;
        closeMenu();
        // v1.98.0 : instantanés, clonage, suppression (kvm-vm.js)
        const fn = window.KVMVm && OP_OPEN[o.dataset.kvmOp] && KVMVm[OP_OPEN[o.dataset.kvmOp]];
        if (fn) fn(v.host, v.name);
        return;
      }
      const b = e.target.closest('[data-kvm-gesture]');
      if (!b || b.getAttribute('aria-disabled')) return;
      closeMenu();
      ask(v, b.dataset.kvmGesture);
    });
  }

  /** Un geste ; l'arrêt forcé demande une confirmation dans la page. */
  function ask(v, g) {
    const fb = cur.host.querySelector('[data-kvm="feedback"]');
    if (g !== 'destroy') { gesture(v, g); return; }
    cur.busy += 1;
    fb.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(tr('kvm.destroyConfirm', { name: v.name, host: v.host }))}">
        <span>${icon('warn')} ${esc(tr('kvm.destroyConfirm', { name: v.name, host: v.host }))}</span>
        <button type="button" class="btn btn-sm btn-danger tip" data-kvm="destroy-yes" data-tip="${esc(tr('kvm.destroyYesTip'))}">${icon('stop')} ${esc(tr('kvm.destroyYes'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-kvm="confirm-no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
    cur.pending = v;
    fb.querySelector('[data-kvm="confirm-no"]').focus();
  }

  function doneText(v, g, o) {
    if (o.gone) return tr('kvm.done.gone', { name: v.name });
    if (g === 'reboot' && o.confirmed === false) return tr('kvm.done.reboot', { name: v.name });
    return tr('kvm.done.state', { name: v.name, state: stateLabel(o.state) });
  }

  async function gesture(v, g) {
    const fb = cur.host.querySelector('[data-kvm="feedback"]');
    fb.textContent = tr('kvm.sending', { gesture: GESTURE_LABEL[g](), name: v.name });
    const c = cur;
    c.busy += 1;
    try {
      const out = await send('POST', `/api/kvm/vms/${enc(v.host)}/${enc(v.name)}/${enc(g)}`, {});
      follow(out.action_id, (s) => { if (s.message && cur) fb.textContent = s.message; }, (ok, run) => {
        c.busy = Math.max(0, c.busy - 1);
        if (c !== cur) return;
        const o = (run.result && run.result.output) || {};
        if (ok) {
          fb.innerHTML = `<span>${icon('ok')} ${esc(doneText(v, g, o))}</span>`;
        } else {
          const sugg = (o.suggest || []).filter((x) => GESTURES.includes(x));
          fb.innerHTML = `<span class="res-error">${icon('fail')} ${esc(o.error || run.error_summary || '?')}</span>
            ${sugg.map((x) => `<button type="button" class="btn btn-sm btn-secondary tip" data-kvm="suggest" data-gesture="${esc(x)}" data-vm="${esc(vmKey(v))}"
              data-tip="${esc(GESTURE_TIP[x]())}">${icon(GESTURE_ICON[x])} ${esc(GESTURE_LABEL[x]())}</button>`).join(' ')}`;
        }
        load(true);
      });
    } catch (e) {
      c.busy = Math.max(0, c.busy - 1);
      fb.innerHTML = `<span class="res-error">${icon('fail')} ${esc(e.message)}</span>`;
    }
  }

  // -- Stockage et Réseaux : un bloc par hôte ------------------------------------
  /** Un bloc par hôte (filtre « un hôte ou tous »). `fn(det, name)` rend le
   *  contenu à partir de `src[nom]` (détail de l'hôte par défaut), `head(name)`
   *  les boutons du bloc. */
  function perHost(d, fn, head, src) {
    const all = d.hosts || [];
    const sel = cur.host.querySelector('[data-kvm-filter="host"]');
    if (sel) {
      const names = all.map((h) => h.name);
      const want = [''].concat(names).join('|');
      if ([...sel.options].map((o) => o.value).join('|') !== want) {
        sel.innerHTML = `<option value="">${esc(tr('kvm.f.allHosts'))}</option>`
          + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
      }
      if (!names.includes(cur.prefs.host)) cur.prefs.host = '';
      sel.value = cur.prefs.host;
    }
    const hosts = all.filter((h) => !cur.prefs.host || h.name === cur.prefs.host);
    cur.host.querySelector('.res-count').textContent = tr('kvm.hostCount', { n: hosts.length });
    if (!hosts.length) return emptyHosts();
    const from = src || cur.details;
    return hosts.map((h) => {
      let inner;
      const det = from[h.name];
      if (h.error) inner = `<p class="res-error">${esc(h.error.message)}</p>`;
      else if (!det) inner = `<p class="form-hint">${esc(tr(src ? 'kvms.surveying' : 'common.loading'))}</p>`;
      else if (det.error) inner = `<p class="res-error">${esc(typeof det.error === 'string' ? det.error : det.error.message)}</p>`;
      else inner = fn(det, h.name);
      return `<section class="kvm-host-block" data-kvm-block="${esc(h.name)}">
          <h3>${icon('node')} ${esc(h.name)} ${h.error ? errorBadge(h.error) : (det && det.error && typeof det.error === 'object' ? errorBadge(det.error) : '')}</h3>
          ${!h.error && head ? `<div class="kvm-block-actions">${head(h.name)}</div>` : ''}
          ${inner}</section>`;
    }).join('');
  }

  const pct = (a, c) => (c ? `${Math.round((a / c) * 100)} %` : '–');

  /** Bouton d'action d'un bloc ou d'une ligne : grisé avec la raison
   *  (rôle, état) ; `attrs` : {pool, vol, net, verb, enable} posés en data-. */
  function act(kind, label, ic, tip, why, host, attrs = {}, cls = 'btn btn-sm btn-secondary') {
    const data = Object.entries(attrs).map(([k, v]) => ` data-${k}="${esc(v)}"`).join('');
    return `<button type="button" class="${cls} tip" data-kvm="${esc(kind)}" data-host="${esc(host)}"${data}${why ? ' disabled' : ''}
      data-tip="${esc(why || tip)}" aria-label="${esc(label)}">${icon(ic)}${cls.includes('btn-icon-action') ? '' : ` ${esc(label)}`}</button>`;
  }
  const mini = (kind, label, ic, tip, why, host, attrs) => act(kind, label, ic, tip, why, host, attrs, 'btn-icon-action');
  const needOp = () => (canOperate() ? '' : tr('kvm.operatorOnly'));
  const needAdmin = () => (isAdmin() ? '' : tr('kvm.adminOnly'));

  function poolInfo(host, name) {
    const d = cur.pools[host];
    return ((d && d.pools) || []).find((p) => p.name === name) || {};
  }

  function storageHead(host) {
    return `${act('pool-create', tr('kvms.pool.create'), 'add', tr('kvms.pool.createTip'), needAdmin(), host)}
      ${act('vol-create', tr('kvms.vol.create'), 'disk', tr('kvms.vol.createTip'), needOp(), host)}`;
  }

  function storageHtml(det, host) {
    const pools = det.pools || [];
    if (!pools.length) return `<p class="form-hint">${esc(tr('kvm.noPools'))}</p>`;
    return `<table class="data-table kvm-pools"><thead><tr><th>${esc(tr('kvm.col.pool'))}</th><th>${esc(tr('kvm.col.state'))}</th>
        <th>${esc(tr('kvms.col.type'))}</th><th>${esc(tr('kvm.col.capacity'))}</th><th>${esc(tr('kvm.col.allocation'))}</th><th>${esc(tr('kvm.col.available'))}</th>
        <th>${esc(tr('kvm.col.volumes'))}</th><th>${esc(tr('kvms.col.actions'))}</th></tr></thead><tbody>
      ${pools.map((p) => poolRow(p, host)).join('')}
      </tbody></table>`;
  }

  function poolRow(p, host) {
    const info = poolInfo(host, p.name);
    const running = p.state === 'running';
    const a = { pool: p.name };
    const acts = [running
      ? mini('pool-verb', tr('kvms.stop'), 'stop', tr('kvms.pool.stopTip'), needAdmin(), host, { ...a, verb: 'stop' })
      : mini('pool-verb', tr('kvms.start'), 'play', tr('kvms.pool.startTip'), needAdmin(), host, { ...a, verb: 'start' }),
    mini('pool-verb', tr('kvms.refresh'), 'refresh', tr('kvms.pool.refreshTip'), needOp() || (running ? '' : tr('kvms.pool.needRunning')), host, { ...a, verb: 'refresh' }),
    mini('pool-verb', p.autostart ? tr('kvms.autostartOff') : tr('kvms.autostartOn'), 'power',
      p.autostart ? tr('kvms.pool.autostartOffTip') : tr('kvms.pool.autostartOnTip'), needAdmin(), host, { ...a, verb: 'autostart', enable: p.autostart ? '0' : '1' }),
    mini('pool-delete', tr('kvms.delete'), 'trash', tr('kvms.pool.deleteTip'), needAdmin(), host, a)].join('');
    return `<tr data-kvm-pool="${esc(p.name)}"><td><strong class="${info.target_path ? 'tip' : ''}"${info.target_path ? ` data-tip="${esc(info.target_path)}"` : ''}>${esc(p.name)}</strong>
          ${p.autostart ? badge('dim', tr('kvm.autostart'), tr('kvm.poolAutostartTip')) : ''}</td>
        <td>${badge(running ? 'ok' : 'dim', running ? tr('kvm.active') : tr('kvm.inactive'))}</td>
        <td>${info.type ? esc(info.type) : dim('–')}</td>
        <td>${esc(bytes(p.capacity))}</td><td>${esc(bytes(p.allocation))} ${dim(pct(p.allocation, p.capacity))}</td><td>${esc(bytes(p.available))}</td>
        <td>${(p.volumes || []).length ? `<details><summary class="tip" data-tip="${esc(tr('kvm.volumesTip'))}">${esc(tr('kvm.volCount', { n: p.volumes.length }))}</summary>
          <table class="data-table kvm-vols"><thead><tr><th>${esc(tr('kvm.col.volume'))}</th><th>${esc(tr('kvm.col.type'))}</th>
            <th>${esc(tr('kvm.col.capacity'))}</th><th>${esc(tr('kvm.col.allocation'))}</th><th>${esc(tr('kvms.col.actions'))}</th></tr></thead><tbody>
            ${p.volumes.map((x) => volRow(x, p, host)).join('')}</tbody></table></details>`
    : dim(running ? tr('kvm.volCount', { n: 0 }) : tr('kvm.poolInactive'))}</td>
        <td class="kvm-row-acts">${acts}</td></tr>`;
  }

  function volRow(x, p, host) {
    // le répertoire d'un pool imbriqué n'est pas un disque : aucune action
    const acts = x.type === 'dir' ? dim(tr('kvms.vol.dir'))
      : [mini('vol-resize', tr('kvms.resize.go'), 'edit', tr('kvms.vol.resizeTip'), needOp(), host, { pool: p.name, vol: x.name }),
        mini('vol-delete', tr('kvms.delete'), 'trash', tr('kvms.vol.deleteTip'), needAdmin(), host, { pool: p.name, vol: x.name })].join('');
    return `<tr data-kvm-vol="${esc(x.name)}"><td><span class="tip" data-tip="${esc(x.path || '')}">${esc(x.name)}</span></td><td>${esc(x.type || '')}</td>
      <td>${esc(bytes(x.capacity))}</td><td>${esc(bytes(x.allocation))}</td><td class="kvm-row-acts">${acts}</td></tr>`;
  }

  // -- Images ---------------------------------------------------------------------------
  function imagesHead(host) {
    const det = cur.images[host];
    const ready = det && !det.error ? '' : tr('kvms.img.waitSurvey');
    return `${act('img-upload', tr('kvms.up.open'), 'upload', tr('kvms.up.openTip'), needOp() || ready, host)}
      ${act('img-fetch', tr('kvms.dl.open'), 'download', tr('kvms.dl.openTip'), needOp() || ready, host)}
      ${act('img-pools', tr('kvms.ip.open'), 'storage', tr('kvms.ip.openTip'), needAdmin() || ready, host)}`;
  }

  function imagesHtml(det, host) {
    const imgs = det.images || [];
    const notes = [];
    if ((det.image_pools || []).length) notes.push(tr('kvms.img.fromPools', { pools: det.image_pools.join(', ') }));
    else notes.push(tr('kvms.img.fromFree'));
    if ((det.missing_pools || []).length) notes.push(tr('kvms.img.missing', { pools: det.missing_pools.join(', ') }));
    if ((det.unknown || []).length) notes.push(tr('kvms.img.unknown', { n: det.unknown.length }));
    const head = `<p class="form-hint kvm-img-notes">${esc(notes.join(' · '))}</p>`;
    if (!imgs.length) return `${head}<p class="form-hint">${esc(tr('kvms.img.none'))}</p>`;
    return `${head}<table class="data-table kvm-images"><thead><tr><th>${esc(tr('kvms.img.name'))}</th><th>${esc(tr('kvm.col.pool'))}</th>
        <th>${esc(tr('kvms.img.format'))}</th><th>${esc(tr('kvms.img.size'))}</th><th>${esc(tr('kvms.img.users'))}</th>
        <th>${esc(tr('kvms.img.checksum'))}</th><th>${esc(tr('kvms.img.origin'))}</th><th>${esc(tr('kvms.col.actions'))}</th></tr></thead><tbody>
      ${imgs.map((i) => imageRow(i, host, det.unknown || [])).join('')}</tbody></table>`;
  }

  /** `failed` : lectures manquées du relevé (`unknown` du catalogue : délai
   *  dépassé, erreur d'E/S), jamais acceptables ; sans elles, « non prouvé »
   *  vient de fichiers illisibles pour le compte SSH, acceptables un par un
   *  dans l'aperçu de suppression. */
  function unprovenTip(failed) {
    return failed.length ? tr('kvms.img.unprovenFailTip', { n: failed.length }) : tr('kvms.img.unprovenTip');
  }

  function imageRow(i, host, failed = []) {
    const key = `${i.pool}/${i.volume}`;
    // `unknown` (kvm_ops.UNKNOWN) en fin de liste : une lecture a manqué, rien
    // n'est prouvé ; ce n'est pas un disque de plus
    const backList = (i.backing_users || []).filter((x) => x !== 'unknown');
    const unproven = (i.backing_users || []).includes('unknown');
    const backers = backList.length;
    const users = (i.used_by || []).length;
    const sha = i.sha256 ? `<code class="tip" data-tip="${esc(tr('kvms.img.shaTip', { sha: i.sha256, date: i.date || '?', author: i.author || '?' }))}">${esc(i.sha256.slice(0, 12))}…</code>`
      : dim(tr('kvms.img.noSha'));
    const origin = window.KVMStorage ? KVMStorage.publicUrl(i.url || '') : '';
    const a = { pool: i.pool, vol: i.volume };
    const acts = [mini('img-verify', tr('kvms.img.verify'), 'refresh', tr('kvms.img.verifyTip'), needOp(), host, a),
      mini('img-create-vm', tr('kvms.img.createVm'), 'vm', tr('kvms.img.createVmTip'), needOp() || (window.KVMForm ? '' : tr('kvm.operatorOnly')), host, a),
      mini('img-delete', tr('kvms.delete'), 'trash', backers ? tr('kvms.img.deleteBaseTip', { n: backers }) : tr('kvms.img.deleteTip'), needAdmin(), host, a)].join('');
    return `<tr data-kvm-image="${esc(key)}"><td><span class="tip" data-tip="${esc(i.path || '')}">${esc(i.volume)}</span></td><td>${esc(i.pool)}</td>
      <td>${esc(i.format || '?')} ${i.iso ? badge('info', 'ISO', tr('kvms.img.isoTip')) : ''}</td><td>${esc(bytes(i.capacity))}</td>
      <td>${backers ? badge('warn', tr('kvms.img.baseOf', { n: backers }), backList.join(', ')) : ''}
        ${users ? badge('dim', tr('kvms.img.usedBy', { n: users }), (i.used_by || []).join(', ')) : ''}
        ${unproven ? badge(failed.length ? 'warn' : 'info', tr('kvms.img.unproven'), unprovenTip(failed)) : ''}${!backers && !users && !unproven ? dim('–') : ''}</td>
      <td>${sha}</td><td>${origin ? `<span class="kvm-path tip" data-tip="${esc(origin)}">${esc(origin.length > 48 ? `${origin.slice(0, 45)}…` : origin)}</span>` : dim('–')}</td>
      <td class="kvm-row-acts">${acts}</td></tr>`;
  }

  // -- Réseaux --------------------------------------------------------------------------
  function networksHead(host) {
    const open = cur.openLinks.has(host);
    return `${act('net-create', tr('kvms.net.create'), 'add', tr('kvms.net.createTip'), needAdmin(), host)}
      ${act('net-links', open ? tr('kvms.links.hide') : tr('kvms.links.show'), 'network', tr('kvms.links.tip'), '', host)}`;
  }

  function networksHtml(det, host) {
    const nets = det.networks || [];
    const links = cur.openLinks.has(host) ? linksHtml(host) : '';
    if (!nets.length) return `<p class="form-hint">${esc(tr('kvm.noNetworks'))}</p>${links}`;
    return `<table class="data-table kvm-nets"><thead><tr><th>${esc(tr('kvm.col.network'))}</th><th>${esc(tr('kvm.col.state'))}</th>
        <th>${esc(tr('kvm.col.forward'))}</th><th>${esc(tr('kvm.col.bridge'))}</th><th>${esc(tr('kvm.col.ips'))}</th><th>${esc(tr('kvm.col.flags'))}</th>
        <th>${esc(tr('kvms.col.actions'))}</th></tr></thead><tbody>
      ${nets.map((n) => netRow(n, host)).join('')}
      </tbody></table>${links}`;
  }

  function netRow(n, host) {
    const a = { net: n.name };
    const acts = [n.active
      ? mini('net-verb', tr('kvms.stop'), 'stop', tr('kvms.net.stopTip'), needAdmin(), host, { ...a, verb: 'stop' })
      : mini('net-verb', tr('kvms.start'), 'play', tr('kvms.net.startTip'), needAdmin(), host, { ...a, verb: 'start' }),
    mini('net-verb', n.autostart ? tr('kvms.autostartOff') : tr('kvms.autostartOn'), 'power',
      n.autostart ? tr('kvms.net.autostartOffTip') : tr('kvms.net.autostartOnTip'), needAdmin() || (n.persistent === false ? tr('kvms.net.transient') : ''), host,
      { ...a, verb: 'autostart', enable: n.autostart ? '0' : '1' }),
    mini('net-delete', tr('kvms.delete'), 'trash', tr('kvms.net.deleteTip'), needAdmin(), host, a)].join('');
    return `<tr data-kvm-net="${esc(n.name)}"><td><strong>${esc(n.name)}</strong></td>
        <td>${badge(n.active ? 'ok' : 'dim', n.active ? tr('kvm.active') : tr('kvm.inactive'))}</td>
        <td>${esc(n.forward || '–')}</td><td>${esc(n.bridge || '–')}</td>
        <td>${(n.ips || []).map((ip) => `${esc(ip.address || '')}${ip.prefix != null ? `/${esc(ip.prefix)}` : ''}${(ip.dhcp_ranges || []).length
    ? ` ${dim(tr('kvm.dhcp', { start: ip.dhcp_ranges[0].start, end: ip.dhcp_ranges[0].end }))}` : ''}`).join('<br>') || '–'}</td>
        <td>${n.autostart ? badge('dim', tr('kvm.autostart'), tr('kvm.netAutostartTip')) : ''} ${n.persistent ? '' : badge('info', tr('kvm.transient'), tr('kvm.transientNetTip'))}</td>
        <td class="kvm-row-acts">${acts}</td></tr>`;
  }

  /** Ponts et cartes physiques de l'hôte (lecture, `ip -j`). */
  function linksHtml(host) {
    const d = cur.links[host];
    if (!d) return `<div class="kvm-links"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
    if (d.error) return `<div class="kvm-links"><p class="res-error">${esc(d.error)}</p></div>`;
    return `<div class="kvm-links" data-kvm-links="${esc(host)}"><h4>${esc(tr('kvms.links.bridges'))}</h4>
      ${(d.bridges || []).length ? `<ul class="kvm-op-list">${d.bridges.map((b) => `<li><strong>${esc(b.name)}</strong> ${dim(b.state || '?')}</li>`).join('')}</ul>` : dim(tr('kvms.links.none'))}
      <h4>${esc(tr('kvms.links.cards'))}</h4>
      ${(d.physical || []).length ? `<ul class="kvm-op-list">${d.physical.map((c) => `<li><strong>${esc(c.name)}</strong> ${dim(c.state || '?')} ${c.master ? dim(tr('kvms.links.inBridge', { bridge: c.master })) : ''}</li>`).join('')}</ul>` : dim(tr('kvms.links.none'))}
      <h4>${esc(tr('kvms.links.networks'))}</h4>
      ${(d.networks || []).length ? `<ul class="kvm-op-list">${d.networks.map((x) => `<li><code>${esc(x.network)}</code> ${dim(`${x.source || ''} ${x.dev || ''}`)}</li>`).join('')}</ul>` : dim(tr('kvms.links.none'))}</div>`;
  }

  // -- gestes simples sur un pool ou un réseau ---------------------------------------------
  const VERB_LABEL = {
    start: () => tr('kvms.start'), stop: () => tr('kvms.stop'), refresh: () => tr('kvms.refresh'),
    autostart: () => tr('kvms.autostart'),
  };

  /** Arrêter un pool ou un réseau sert à toutes les VMs de l'hôte : confirmé
   *  dans la page. Les autres gestes partent tout de suite. */
  function askObject(kind, host, name, verb, enable) {
    if (verb !== 'stop') { objectGesture(kind, host, name, verb, enable); return; }
    const fb = cur.host.querySelector('[data-kvm="feedback"]');
    cur.busy += 1;
    cur.pendingObj = { kind, host, name, verb };
    const text = kind === 'pool' ? tr('kvms.pool.stopConfirm', { name, host }) : tr('kvms.net.stopConfirm', { name, host });
    fb.innerHTML = `<div class="bkg-confirm kvm-confirm" role="group" aria-label="${esc(text)}">
        <span>${icon('warn')} ${esc(text)}</span>
        <button type="button" class="btn btn-sm btn-danger tip" data-kvm="obj-yes" data-tip="${esc(tr('kvms.stopYesTip'))}">${icon('stop')} ${esc(tr('kvms.stop'))}</button>
        <button type="button" class="btn btn-sm btn-secondary tip" data-kvm="confirm-no" data-tip="${esc(tr('kvm.confirmNoTip'))}">${esc(tr('kvm.confirmNo'))}</button></div>`;
    fb.querySelector('[data-kvm="confirm-no"]').focus();
  }

  async function objectGesture(kind, host, name, verb, enable) {
    const fb = cur.host.querySelector('[data-kvm="feedback"]');
    const label = VERB_LABEL[verb] ? VERB_LABEL[verb]() : verb;
    fb.textContent = tr('kvms.sending', { verb: label, name });
    const c = cur;
    c.busy += 1;
    const url = `/api/kvm/hosts/${enc(host)}/${kind === 'pool' ? 'pools' : 'networks'}/${enc(name)}/${enc(verb)}`;
    try {
      const out = await send('POST', url, verb === 'autostart' ? { enable: !!enable } : {});
      follow(out.action_id, (s) => { if (s.message && cur === c) fb.textContent = s.message; }, (ok, run) => {
        c.busy = Math.max(0, c.busy - 1);
        if (c !== cur) return;
        const o = (run.result && run.result.output) || {};
        if (ok) fb.innerHTML = `<span>${icon('ok')} ${esc(tr('kvms.verbDone', { verb: label, name }))}</span>`;
        else {
          fb.innerHTML = `<span class="res-error">${icon('fail')} ${esc(o.error || run.error_summary || '?')}</span>${
            (o.refusals || []).length ? `<ul class="kvm-op-list">${o.refusals.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}`;
        }
        load(true);
      });
    } catch (e) {
      c.busy = Math.max(0, c.busy - 1);
      fb.innerHTML = `<span class="res-error">${icon('fail')} ${esc(e.message)}</span>`;
    }
  }

  async function verifyImage(host, pool, vol) {
    const fb = cur.host.querySelector('[data-kvm="feedback"]');
    fb.textContent = tr('kvms.img.verifying', { name: `${pool}/${vol}` });
    const c = cur;
    c.busy += 1;
    try {
      const out = await send('POST', `/api/kvm/hosts/${enc(host)}/images/${enc(pool)}/${enc(vol)}/verify`, {});
      follow(out.action_id, (s) => { if (s.message && cur === c) fb.textContent = s.message; }, (ok, run) => {
        c.busy = Math.max(0, c.busy - 1);
        if (c !== cur) return;
        const o = (run.result && run.result.output) || {};
        let text;
        if (!ok) text = o.error || run.error_summary || '?';
        else if (o.skipped) text = tr('kvms.img.verifySkipped', { name: `${pool}/${vol}`, why: o.skipped });
        else if (o.match === false) text = tr('kvms.img.verifyMismatch', { name: `${pool}/${vol}`, sha: o.sha256 || '?' });
        else if (o.match === true) text = tr('kvms.img.verifyOk', { name: `${pool}/${vol}`, sha: o.sha256 || '?' });
        else text = tr('kvms.img.verifyNoRef', { name: `${pool}/${vol}`, sha: o.sha256 || '?' });
        const good = ok && o.match !== false;
        fb.innerHTML = `<span class="${good ? '' : 'res-error'}">${icon(good ? 'ok' : 'fail')} ${esc(text)}</span>`;
      });
    } catch (e) {
      c.busy = Math.max(0, c.busy - 1);
      fb.innerHTML = `<span class="res-error">${icon('fail')} ${esc(e.message)}</span>`;
    }
  }

  /** Contexte des fenêtres d'envoi : hôtes joignables, pools actifs de
   *  l'hôte, pools d'images déclarés. */
  function sendContext(host) {
    const pools = ((cur.pools[host] && cur.pools[host].pools) || []).filter((p) => p.state === 'running').map((p) => p.name);
    const img = cur.images[host] || {};
    return { hosts: ((cur.data && cur.data.hosts) || []).filter((h) => !h.error).map((h) => h.name),
             pools: pools.length ? pools : (img.pools || []), imagePools: img.image_pools || [] };
  }

  function storageClick(b) {
    const S = window.KVMStorage;
    const act = b.dataset.kvm;
    const host = b.dataset.host;
    if (!S) return false;
    const det = cur.details[host] || {};
    const poolOf = (name) => (det.pools || []).find((p) => p.name === name);
    const imageOf = () => ((cur.images[host] || {}).images || []).find((i) => i.pool === b.dataset.pool && i.volume === b.dataset.vol);
    switch (act) {
      case 'pool-create': S.openPoolCreate(host); return true;
      case 'vol-create': S.openVolumeCreate(host, { pools: det.pools || [], pool: b.dataset.pool }); return true;
      case 'pool-verb': askObject('pool', host, b.dataset.pool, b.dataset.verb, b.dataset.enable === '1'); return true;
      case 'pool-delete': S.openDelete('pool', host, { name: b.dataset.pool }); return true;
      case 'vol-resize': {
        const v = ((poolOf(b.dataset.pool) || {}).volumes || []).find((x) => x.name === b.dataset.vol);
        if (v) S.openVolumeResize(host, b.dataset.pool, v);
        return true;
      }
      case 'vol-delete': S.openDelete('volume', host, { pool: b.dataset.pool, name: b.dataset.vol }); return true;
      case 'img-upload': S.openUpload(host, sendContext(host)); return true;
      case 'img-fetch': S.openFetch(host, sendContext(host)); return true;
      case 'img-pools': S.openImagePools(host, { pools: ((cur.pools[host] || {}).pools || []).map((p) => p.name),
                                                 current: (cur.images[host] || {}).image_pools || [] }); return true;
      case 'img-verify': verifyImage(host, b.dataset.pool, b.dataset.vol); return true;
      case 'img-create-vm': {
        const i = imageOf();
        if (i && window.KVMForm) KVMForm.openCreate(host, { image: { pool: i.pool, volume: i.volume, iso: !!i.iso, format: i.format, capacity: i.capacity } });
        return true;
      }
      case 'img-delete': S.openDelete('image', host, { pool: b.dataset.pool, name: b.dataset.vol }); return true;
      case 'net-create': S.openNetworkCreate(host); return true;
      case 'net-verb': askObject('network', host, b.dataset.net, b.dataset.verb, b.dataset.enable === '1'); return true;
      case 'net-delete': S.openDelete('network', host, { name: b.dataset.net }); return true;
      default: return false;
    }
  }

  // -- clics --------------------------------------------------------------------------
  function onClick(e) {
    const th = e.target.closest('th[data-sort]');
    if (th && cur) {
      const k = th.dataset.sort;
      cur.prefs.dir = cur.prefs.sort === k ? -cur.prefs.dir : 1;
      cur.prefs.sort = k;
      render();
      return;
    }
    const b = e.target.closest('[data-kvm]');
    if (!b || !cur || b.disabled) return;
    const act = b.dataset.kvm;
    if (act === 'refresh') { load(true); return; }
    if (act === 'sub') {
      cur.prefs.sub = b.dataset.sub === 'images' ? 'images' : 'pools';
      cur.host.querySelectorAll('[data-kvm="sub"]').forEach((x) => x.classList.toggle('active', x.dataset.sub === cur.prefs.sub));
      render();
      load();
      return;
    }
    if (act === 'net-links') {
      const h = b.dataset.host;
      if (cur.openLinks.has(h)) cur.openLinks.delete(h);
      else { cur.openLinks.add(h); loadLinks(h).then(render); }
      render();
      return;
    }
    if (act === 'obj-yes') {
      const o = cur.pendingObj;
      cur.pendingObj = null;
      cur.busy = Math.max(0, cur.busy - 1);
      if (o) objectGesture(o.kind, o.host, o.name, o.verb);
      return;
    }
    if (storageClick(b)) return;
    if (act === 'declare') { openDeclare(); return; }
    if (act === 'create-vm') { if (window.KVMForm) KVMForm.openCreate(b.dataset.host || ''); return; }
    if (act === 'test') { testHost(b.dataset.host); return; }
    if (act === 'remove') { confirmRemove(b.dataset.host); return; }
    if (act === 'remove-yes') { doRemove(b.dataset.host); return; }
    if (act === 'confirm-no') {
      cur.busy = Math.max(0, cur.busy - 1);
      cur.pending = null;
      cur.pendingObj = null;
      const fb = cur.host.querySelector('[data-kvm="feedback"]');
      if (fb.contains(b)) fb.innerHTML = '';
      render();
      return;
    }
    if (act === 'destroy-yes') {
      const v = cur.pending;
      cur.pending = null;
      cur.busy = Math.max(0, cur.busy - 1);
      if (v) gesture(v, 'destroy');
      return;
    }
    if (act === 'detail') { toggleDetail(b.dataset.vm); return; }
    if (act === 'menu') { const v = findVm(b.dataset.vm); if (v) openMenu(b, v); return; }
    if (act === 'suggest') { const v = findVm(b.dataset.vm); if (v) ask(v, b.dataset.gesture); }
  }

  // -- la fenêtre « Déclarer un hôte » ---------------------------------------------
  const field = (label, input, tip) => `<label class="bk-field"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}"`)}</label>`;

  function openDeclare() {
    if (!window.FloatingPanels) return null;
    const panel = FloatingPanels.open({
      id: 'kvm-declare', icon: 'node', width: 620, height: 760, title: tr('kvm.declare'),
      bodyHtml: `<form class="of-form kvm-declare" autocomplete="off" novalidate>
        <p class="form-hint">${esc(tr('kvm.dc.hint'))}</p>
        <div class="ct-create-row">
          ${field(tr('kvm.dc.address'), '<input name="address" maxlength="253" required>', tr('kvm.dc.addressTip'))}
          ${field(tr('kvm.dc.port'), '<input type="number" name="port" min="1" max="65535" value="22">', tr('kvm.dc.portTip'))}
        </div>
        <div class="bk-form-actions kvm-scan-row"><button type="button" class="btn btn-sm btn-secondary tip" data-kvm-dc="scan"
          data-tip="${esc(tr('kvm.dc.scanTip'))}">${icon('key')} ${esc(tr('kvm.dc.scan'))}</button></div>
        <fieldset class="kvm-fps" data-kvm-dc="fps"><legend>${esc(tr('kvm.dc.fps'))}</legend>
          <p class="form-hint">${esc(tr('kvm.dc.fpsEmpty'))}</p></fieldset>
        <div class="ct-create-row">
          ${field(tr('kvm.dc.name'), '<input name="name" maxlength="61" required>', tr('kvm.dc.nameTip'))}
          ${field(tr('kvm.dc.user'), '<input name="user" maxlength="64" required>', tr('kvm.dc.userTip'))}
        </div>
        ${field(tr('kvm.dc.key'), '<input type="file" name="key" required>', tr('kvm.dc.keyTip'))}
        ${field(tr('kvm.dc.labels'), '<textarea name="labels" rows="2" maxlength="2000" placeholder="site=cave"></textarea>', tr('kvm.dc.labelsTip'))}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-kvm-dc="submit"
          data-tip="${esc(tr('kvm.dc.goTip'))}">${icon('add')} ${esc(tr('kvm.dc.go'))}</button></div>
        <div class="of-msg" data-kvm-dc="msg" role="status" aria-live="polite"></div>
        <ul class="kvm-steps" data-kvm-dc="steps"></ul>
      </form>`,
    });
    const root = panel.el;
    if (root.dataset.kvmReady) return root;
    root.dataset.kvmReady = '1';
    const f = root.querySelector('form');
    const msg = root.querySelector('[data-kvm-dc="msg"]');
    const fps = root.querySelector('[data-kvm-dc="fps"]');
    const steps = root.querySelector('[data-kvm-dc="steps"]');
    const say = (html) => { msg.innerHTML = html; };
    const bad = (t) => say(`<span class="res-error">${icon('fail')} ${esc(t)}</span>`);
    const clearFps = () => { fps.innerHTML = `<legend>${esc(tr('kvm.dc.fps'))}</legend><p class="form-hint">${esc(tr('kvm.dc.fpsEmpty'))}</p>`; };
    f.elements.address.addEventListener('input', clearFps);
    f.elements.port.addEventListener('input', clearFps);

    root.querySelector('[data-kvm-dc="scan"]').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const address = f.elements.address.value.trim();
      const port = Number(f.elements.port.value || 22);
      if (!address) { bad(tr('kvm.dc.needAddress')); return; }
      btn.disabled = true;
      say(esc(tr('kvm.dc.scanning', { address })));
      try {
        const d = await send('POST', '/api/kvm/hosts/scan', { address, port });
        fps.innerHTML = `<legend>${esc(tr('kvm.dc.fps'))}</legend><p class="form-hint">${esc(tr('kvm.dc.fpsPick'))}</p>
          ${(d.keys || []).map((k) => `<label class="kvm-fp"><input type="radio" name="fingerprint" value="${esc(k.fingerprint)}" class="tip"
            data-tip="${esc(tr('kvm.dc.fpTip'))}"> <strong>${esc(k.type)}</strong> <code>${esc(k.fingerprint)}</code></label>`).join('')}`;
        say('');
      } catch (err) {
        bad(err.message);
      } finally { btn.disabled = false; }
    });

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fp = f.querySelector('input[name="fingerprint"]:checked');
      const name = f.elements.name.value.trim();
      const user = f.elements.user.value.trim();
      const port = Number(f.elements.port.value || 22);
      if (!f.elements.address.value.trim()) { bad(tr('kvm.dc.needAddress')); return; }
      if (!fp) { bad(tr('kvm.dc.needFp')); return; }
      if (!NAME_RE.test(name)) { bad(tr('kvm.dc.badName')); return; }
      if (!USER_RE.test(user)) { bad(tr('kvm.dc.badUser')); return; }
      if (!f.elements.key.files || !f.elements.key.files.length) { bad(tr('kvm.dc.needKey')); return; }
      const labels = {};
      for (const line of f.elements.labels.value.split(/\n|,/).map((x) => x.trim()).filter(Boolean)) {
        const i = line.indexOf('=');
        const k = i > 0 ? line.slice(0, i).trim() : '';
        if (!LABEL_RE.test(k)) { bad(tr('kvm.dc.badLabel', { label: line })); return; }
        labels[k] = line.slice(i + 1).trim();
      }
      const fd = new FormData();
      fd.append('payload', JSON.stringify({ name, address: f.elements.address.value.trim(), port, user, fingerprint: fp.value, labels }));
      fd.append('key', f.elements.key.files[0]);
      const go = root.querySelector('[data-kvm-dc="submit"]');
      go.disabled = true;
      steps.innerHTML = '';
      say(esc(tr('kvm.dc.sending')));
      try {
        const out = await send('POST', '/api/kvm/hosts', fd, true);
        say(esc(tr('bk.started', { id: out.action_id })));
        follow(out.action_id, (s) => {
          let li = [...steps.children].find((x) => x.dataset.step === s.step_id);
          if (!li) { li = document.createElement('li'); li.dataset.step = s.step_id || ''; steps.appendChild(li); }
          const st = s.status || 'running';
          li.className = `kvm-step is-${st}`;
          li.innerHTML = `${badge(CHECK_BADGE[st] || (st === 'done' ? 'ok' : 'info'), CHECK_STATUS[st] ? CHECK_STATUS[st]() : st)} ${esc(s.message || '')}`;
        }, (ok, run) => {
          const o = (run.result && run.result.output) || {};
          if (ok) {
            say(`<span>${icon('ok')} ${esc(tr('kvm.dc.added', { name }))}</span>`);
            if (cur && cur.view === 'hosts') load(true);
          } else {
            bad(o.error || run.error_summary || tr('kvm.dc.failed'));
            go.disabled = false;
          }
        });
      } catch (err) {
        bad(err.message);
        go.disabled = false;
      }
    });
    return root;
  }

  // busy : gestes en cours de la vue affichée (un compteur négatif bloquerait
  // l'actualisation automatique), lu par les tests
  // follow : partagé avec kvm-vm.js (fenêtres instantanés, clonage, suppression)
  return { start, stop, load, follow, openDeclare, gestureWhy, operationWhy, stateLabel, busy: () => (cur ? cur.busy : null) };
})();

if (typeof window !== 'undefined') window.KVM = KVM;
