/**
 * harvester-ops — déplacer une VM vers un autre cluster, l'exporter dans
 * un fichier, importer une archive (v1.45.0).
 *
 * La fenêtre « Migrer » (vm-migrate.js) propose trois destinations ; les
 * deux dernières sont rendues ici. Le serveur ne fait que relayer le script
 * `harvester-vm-transfer` : ce module compose la demande, affiche le
 * contrôle préalable dans la langue de l'interface, et lance l'action,
 * suivie ensuite dans le dock.
 *
 * v1.47.0 : la fin d'un export désigne son archive (télécharger, importer),
 * et le magasin accepte une archive déposée depuis ce poste.
 */
const VMTransfer = (() => {
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const tr = (k, vars) => i18n.t(k, vars);
  const enc = encodeURIComponent;

  function fmtBytes(n) {
    if (n == null || isNaN(n)) return '?';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
  }

  // Les faits chiffrés en octets sont rendus lisibles ; le reste tel quel.
  const BYTE_FACTS = ['needed', 'allocatable', 'free'];
  function findingText(f) {
    const facts = Object.assign({}, f.facts || {});
    BYTE_FACTS.forEach(k => { if (k in facts) facts[k] = fmtBytes(facts[k]); });
    if (Array.isArray(facts.devices)) facts.devices = facts.devices.join(', ');
    const fn = FINDINGS[f.code];
    return fn ? fn(facts) : f.code;
  }

  // Un appel littéral par constat : le contrôle de parité des traductions ne
  // voit que ces formes, pas une clé composée à l'exécution.
  const FINDINGS = {
    'target-unreachable': (v) => tr('transfer.finding.target-unreachable', v),
    'kubevirt-missing': (v) => tr('transfer.finding.kubevirt-missing', v),
    'version-older': (v) => tr('transfer.finding.version-older', v),
    'namespace-missing': (v) => (v.create ? tr('transfer.finding.namespace-created', v)
                                          : tr('transfer.finding.namespace-missing', v)),
    'vm-name-taken': (v) => tr('transfer.finding.vm-name-taken', v),
    'network-unmapped': (v) => tr('transfer.finding.network-unmapped', v),
    'storage-class-unmapped': (v) => tr('transfer.finding.storage-class-unmapped', v),
    'capacity-short': (v) => tr('transfer.finding.capacity-short', v),
    'devices-removed': (v) => tr('transfer.finding.devices-removed', v),
    'node-affinity-removed': (v) => tr('transfer.finding.node-affinity-removed', v),
    'image-conflict': (v) => tr('transfer.finding.image-conflict', v),
    'image-missing-source': (v) => tr('transfer.finding.image-missing-source', v),
    'image-sync-unsupported': (v) => tr('transfer.finding.image-sync-unsupported', v),
    'cdi-missing': (v) => tr('transfer.finding.cdi-missing', v),
    'source-room-short': (v) => tr('transfer.finding.source-room-short', v),
    'store-room-short': (v) => tr('transfer.finding.store-room-short', v),
    'short-mode-needs-backup': (v) => tr('transfer.finding.short-mode-needs-backup', v),
    'secrets-in-archive': (v) => tr('transfer.finding.secrets-in-archive', v),
    'hostname-duplicate': (v) => tr('transfer.finding.hostname-duplicate', v),
    'mac-in-use': (v) => tr('transfer.finding.mac-in-use', v),
    'replicas-degraded': (v) => tr('transfer.finding.replicas-degraded', v),
  };
  const REASONS = {
    'source-no-target': () => tr('transfer.reason.source-no-target'),
    'target-no-target': () => tr('transfer.reason.target-no-target'),
    'different-targets': () => tr('transfer.reason.different-targets'),
    'forced': () => tr('transfer.reason.forced'),
    'network-renamed': () => tr('transfer.reason.network-renamed'),
    'class-missing': () => tr('transfer.reason.class-missing'),
    'file-requested': () => tr('transfer.reason.file-requested'),
    'shared-target': () => tr('transfer.reason.shared-target'),
  };

  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };

  // v1.47.2 : un export ne lit qu'un cluster, un import une archive et un
  // cluster ; « Lecture des deux clusters » ne valait que pour un transfert.
  const CHECKING = {
    migrate: () => tr('transfer.checking'),
    export: () => tr('transfer.checkingExport'),
    import: () => tr('transfer.checkingImport'),
  };
  const CHECK_TIP = {
    migrate: () => tr('transfer.checkTip'),
    export: () => tr('transfer.checkTipExport'),
    import: () => tr('transfer.checkTipImport'),
  };

  function reportHtml(d) {
    const engine = d.engine === 'backup'
      ? tr('transfer.engine.backup')
      : tr('transfer.engine.file', { reason: (REASONS[d.reason] || (() => ''))() });
    const a = d.amount;
    const amount = a ? `<p class="tf-desc xfer-amount">${esc(tr('transfer.amount', { disks: a.disks,
      size: XferProgress.bytes(a.size) }) + (a.used != null
        ? ' ' + tr('transfer.amountUsed', { used: XferProgress.bytes(a.used) }) : ''))}</p>` : '';
    const items = (d.findings || []).filter(f => f.code !== 'engine');
    const list = items.map(f => `
      <div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}" data-level="${esc(f.level)}">
        <div class="sto-finding-title">${esc(findingText(f))}</div>
      </div>`).join('');
    const ok = d.blocked ? '' : `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('transfer.noBlocker'))}</div></div>`;
    return `<p class="tf-desc xfer-engine">${esc(engine)}</p>${amount}${list}${ok}`;
  }

  function options(values, selected, withEmpty) {
    const opts = withEmpty ? [`<option value="">${esc(tr('transfer.map.choose'))}</option>`] : [];
    (values || []).forEach(v => {
      opts.push(`<option value="${esc(v)}" ${v === selected ? 'selected' : ''}>${esc(v)}</option>`);
    });
    return opts.join('');
  }

  // -------------------------------------------------------------------------
  // Formulaire commun : vers un cluster (déplacement ou import) ou un fichier
  // -------------------------------------------------------------------------
  //   ctx.kind   'migrate' | 'export' | 'import'
  //   ctx.cluster, ctx.namespace, ctx.name  la VM (migrate/export)
  //   ctx.file                               l'archive (import)
  //   ctx.clusters                           clusters déclarés
  async function renderForm(container, ctx) {
    const kind = ctx.kind;
    const targets = (ctx.clusters || []).filter(c => kind === 'import' || c !== ctx.cluster);
    if (kind !== 'export' && !targets.length) {
      container.innerHTML = `<p class="empty-state">${esc(tr('transfer.noTarget'))}</p>`;
      return;
    }
    const toCluster = kind !== 'export';
    const srcState = kind === 'import' ? '' : `
      <label class="tf-field"><span class="tf-label">${esc(tr('transfer.source'))}</span>
        <select data-x="source" class="tip" data-tip="${esc(tr('transfer.source'))}">
          <option value="${kind === 'export' ? 'running' : 'stopped'}">${esc((kind === 'export' ? tr('transfer.source.running') : tr('transfer.source.stopped')))}</option>
          <option value="${kind === 'export' ? 'stopped' : 'running'}">${esc((kind === 'export' ? tr('transfer.source.stopped') : tr('transfer.source.running')))}</option>
          ${kind === 'migrate' ? `<option value="deleted">${esc(tr('transfer.source.deleted'))}</option>` : ''}
        </select></label>`;
    container.innerHTML = `
      <div class="tf-form xfer-form" data-kind="${esc(kind)}">
        ${toCluster ? `
        <div class="tf-args">
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.target'))}</span>
            <select data-x="to" class="tip" data-tip="${esc(tr('transfer.targetTip'))}">${options(targets, targets[0])}</select></label>
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.namespace'))}</span>
            <input data-x="namespace" type="text" value="${esc(ctx.namespace || '')}" class="tip" data-tip="${esc(tr('transfer.namespace'))}"></label>
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.name'))}</span>
            <input data-x="name" type="text" value="${esc(ctx.name || '')}" class="tip" data-tip="${esc(tr('transfer.name'))}"></label>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.createNs'))}"><input data-x="create_namespace" type="checkbox"><span class="tf-label">${esc(tr('transfer.createNs'))}</span></label>
        </div>` : ''}
        <div class="tf-args">
          ${kind === 'migrate' ? `
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.mode'))}</span>
            <select data-x="mode" class="tip" data-tip="${esc(tr('transfer.mode.shortTip'))}">
              <option value="stop">${esc(tr('transfer.mode.stop'))}</option>
              <option value="short">${esc(tr('transfer.mode.short'))}</option>
            </select></label>` : ''}
          ${srcState}
          ${toCluster ? `
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.targetState'))}</span>
            <select data-x="target" class="tip" data-tip="${esc(tr('transfer.targetState'))}">
              <option value="started">${esc(tr('transfer.targetState.started'))}</option>
              <option value="stopped">${esc(tr('transfer.targetState.stopped'))}</option>
            </select></label>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.keepMacTip'))}"><input data-x="keep_mac" type="checkbox" checked><span class="tf-label">${esc(tr('transfer.keepMac'))}</span></label>` : ''}
          ${toCluster ? `
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.speed'))}</span>
            <select data-x="speed" class="tip" data-tip="${esc(tr('transfer.speed.tip'))}">
              <option value="normal">${esc(tr('transfer.speed.normal'))}</option>
              <option value="eco">${esc(tr('transfer.speed.eco'))}</option>
              <option value="max">${esc(tr('transfer.speed.max'))}</option>
            </select></label>
          <label class="tf-field"><span class="tf-label">${esc(tr('transfer.bandwidth'))}</span>
            <input data-x="bandwidth" type="number" min="1" step="1" placeholder="-"
                   class="tip" data-tip="${esc(tr('transfer.bandwidthTip'))}"></label>` : ''}
          ${kind === 'migrate' ? `
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.forceFile'))}"><input data-x="engine_file" type="checkbox"><span class="tf-label">${esc(tr('transfer.forceFile'))}</span></label>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.keepBackups'))}"><input data-x="keep_backups" type="checkbox"><span class="tf-label">${esc(tr('transfer.keepBackups'))}</span></label>` : ''}
        </div>
        ${toCluster ? `<fieldset class="tf-block xfer-mappings"><legend>${esc(tr('transfer.mappings'))}</legend><div class="tf-args" data-x="maps"></div></fieldset>` : ''}
        <fieldset class="tf-block"><legend>${esc(tr('transfer.findings'))}</legend>
          <div class="xfer-report" data-x="report"></div></fieldset>
        <div class="apply-bar" style="margin:0; padding:0; border:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="check" data-tip="${esc(CHECK_TIP[kind]())}">${Icons.svg('refresh')} <span>${esc(tr('transfer.check'))}</span></button>
          <button type="button" class="btn btn-primary btn-sm tip" data-x="start" data-tip="${esc(tr('transfer.startTip'))}" disabled>${Icons.svg(kind === 'export' ? 'download' : kind === 'import' ? 'upload' : 'migrate')} <span>${esc((kind === 'export' ? tr('transfer.startExport') : kind === 'import' ? tr('transfer.startImport') : tr('transfer.start')))}</span></button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
        <fieldset class="tf-block xfer-live" data-x="live" hidden>
          <legend>${esc(tr('progress.title'))}</legend>
          <div class="xfer-live-line" data-x="live-line">${esc(tr('progress.waiting'))}</div>
          <div class="progress-mini xfer-live-bar"><div class="fill" data-x="live-bar" style="width:0%"></div></div>
          <div class="tf-desc" data-x="live-meta"></div>
          <ul class="xfer-live-done" data-x="live-done"></ul>
          <div class="xfer-archive" data-x="archive" hidden></div>
        </fieldset>
      </div>`;

    const q = (x) => container.querySelector(`[data-x="${x}"]`);
    // `running` : une action lancée d'ici n'est pas finie. Un contrôle qui
    // arrive après le lancement (déclenché par un champ quitté) réactivait
    // « Lancer » pendant le transfert (vu en réel, v1.47.2).
    const state = { seq: 0, mapsFor: null, running: false };

    function syncMac() {
      const src = q('source'), mac = q('keep_mac');
      if (!src || !mac) return;
      const running = src.value === 'running';
      mac.disabled = running;
      if (running) mac.checked = false;
    }

    function body() {
      const b = {};
      ['to', 'namespace', 'name', 'mode', 'source', 'target', 'speed', 'bandwidth'].forEach(k => {
        const el = q(k); if (el && el.value) b[k] = el.value.trim();
      });
      const cb = (k) => { const el = q(k); return !!(el && el.checked); };
      if (q('keep_mac')) b.keep_mac = cb('keep_mac');
      if (q('create_namespace')) b.create_namespace = cb('create_namespace');
      if (cb('keep_backups')) b.keep_backups = true;
      if (cb('engine_file')) b.engine = 'file';
      if (toCluster) {
        b.networks = {}; b.storage_classes = {};
        container.querySelectorAll('[data-map]').forEach(sel => {
          b[sel.dataset.map][sel.dataset.src] = sel.value || null;
        });
      }
      return b;
    }

    function renderMaps(d) {
      const box = q('maps');
      if (!box) return;
      const nets = (d.target && d.target.networks) || [];
      const scs = (d.target && d.target.storage_classes) || [];
      const m = d.mappings || { networks: {}, storage_classes: {} };
      const rows = [];
      Object.keys(m.networks || {}).forEach(src => rows.push(`
        <label class="tf-field"><span class="tf-label">${esc(tr('transfer.map.network', { src }))}</span>
          <select data-map="networks" data-src="${esc(src)}" class="tip" data-tip="${esc(tr('transfer.map.tip'))}">${options(nets, m.networks[src], true)}</select></label>`));
      Object.keys(m.storage_classes || {}).forEach(src => rows.push(`
        <label class="tf-field"><span class="tf-label">${esc(tr('transfer.map.class', { src }))}</span>
          <select data-map="storage_classes" data-src="${esc(src)}" class="tip" data-tip="${esc(tr('transfer.map.tip'))}">${options(scs, m.storage_classes[src], true)}</select></label>`));
      box.innerHTML = rows.join('');
      box.closest('fieldset').style.display = rows.length ? '' : 'none';
      box.querySelectorAll('[data-map]').forEach(sel => sel.addEventListener('change', check));
    }

    function checkUrl() {
      return kind === 'import'
        ? `/api/exports/${enc(ctx.file)}/check`
        : `/api/vm/${enc(ctx.cluster)}/${enc(ctx.namespace)}/${enc(ctx.name)}/transfer/check`;
    }

    async function check(ev) {
      // changer de cluster cible remet les correspondances à zéro
      const fresh = ev && ev.target && ev.target.dataset && ev.target.dataset.x === 'to';
      const seq = ++state.seq;
      const report = q('report');
      // L'ancien rapport reste affiché, estompé : le remplacer par une ligne
      // faisait remonter « Lancer » pendant le clic (appui sur le bouton,
      // relâchement à côté), et le clic était perdu.
      if (report.querySelector('.sto-finding')) report.classList.add('is-checking');
      else report.innerHTML = `<p class="tf-desc">${esc(CHECKING[kind]())}</p>`;
      // « Lancer » n'est PAS grisé pendant qu'un contrôle se refait : quitter
      // un champ pour cliquer dessus relance le contrôle (événement change),
      // et un bouton grisé à cet instant perd le clic. Le script refait le
      // contrôle avant d'agir et refuse ce qui bloque : aucun risque.
      const b = body();
      if (fresh) { b.networks = {}; b.storage_classes = {}; }
      try {
        const r = await fetch(checkUrl(), { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                            body: JSON.stringify(b) });
        const d = await r.json();
        if (seq !== state.seq) return;            // un contrôle plus récent a suivi
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        // Les correspondances suivent le cluster de la RÉPONSE : une réponse
        // pour un nouveau cluster peut arriver après qu'un contrôle plus
        // ancien a été écarté (vécu : on change de cluster puis on retouche
        // le nom, et les listes restaient celles du cluster d'avant).
        const shownFor = d.target && d.target.cluster;
        if (toCluster && (fresh || state.mapsFor !== shownFor
                          || !container.querySelector('[data-map]'))) {
          renderMaps(d);
          state.mapsFor = shownFor;
        } else if (toCluster) {
          // une liste restée vide prend la valeur que le serveur a retenue
          container.querySelectorAll('[data-map]').forEach(sel => {
            const v = ((d.mappings || {})[sel.dataset.map] || {})[sel.dataset.src];
            if (!sel.value && v && [...sel.options].some(o => o.value === v)) sel.value = v;
          });
        }
        report.innerHTML = reportHtml(d);
        report.classList.remove('is-checking');
        const mode = q('mode');
        if (mode) {
          const short = mode.querySelector('option[value="short"]');
          short.disabled = d.engine !== 'backup';
          if (short.disabled && mode.value === 'short') mode.value = 'stop';
        }
        q('start').disabled = !!d.blocked || state.running;
      } catch (e) {
        if (seq !== state.seq) return;
        report.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('transfer.error', { msg: e.message }))}</div></div>`;
        report.classList.remove('is-checking');
      }
    }

    async function start() {
      const b = body();
      if (b.source === 'deleted' && !confirm(tr('transfer.confirmDelete'))) return;
      const fb = q('feedback');
      q('start').disabled = true;
      const url = kind === 'import'
        ? `/api/exports/${enc(ctx.file)}/import`
        : `/api/vm/${enc(ctx.cluster)}/${enc(ctx.namespace)}/${enc(ctx.name)}/transfer`;
      try {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(b) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        fb.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('transfer.started', { id: d.action_id }))}</span>`;
        state.running = true;
        follow(d.action_id);
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(tr('transfer.error', { msg: e.message }))}</span>`;
        q('start').disabled = false;
      }
    }

    // Suivi en direct de l'action lancée : phase en cours, quantités, débit,
    // temps restant ; les phases finies gardent leur bilan.
    function follow(actionId) {
      const box = q('live');
      box.hidden = false;
      // la fenêtre ne défilait pas vers le suivi : il restait sous le bord
      // (vu sur la capture d'un vrai transfert)
      box.scrollIntoView({ block: 'nearest' });
      const phases = {};
      const draw = (cur) => {
        if (cur) {
          q('live-line').textContent = XferProgress.text(cur);
          q('live-bar').style.width = XferProgress.pct(cur) + '%';
          const meta = [tr('progress.elapsed', { t: XferProgress.duration(cur.elapsed) })];
          const n = XferProgress.note(cur);
          if (n) meta.push(n);
          q('live-meta').textContent = meta.join(' · ');
        }
        q('live-done').innerHTML = Object.values(phases).filter(p => p.final)
          .map(p => `<li>${Icons.svg('ok', { size: 12 })} ${esc(XferProgress.text(p))}</li>`).join('');
      };
      if (!window.SSEReconnect) return;
      const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
        on: {
          progress: (e) => {
            const snap = JSON.parse(e.data);
            phases[snap.phase] = snap;
            draw(snap);
          },
          end: (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch (_) { /* fin sans détail */ }
            state.running = false;
            Object.values(d.progress || {}).forEach(p => { phases[p.phase] = p; });
            draw(null);
            const line = q('live-line');
            if (d.status === 'done') {
              line.textContent = tr('progress.finished');
              q('live-bar').style.width = '100%';
              if (d.result && d.result.archive) archiveBlock(q('archive'), d.result.archive);
            } else if (d.status === 'cancelled') {
              line.textContent = tr('progress.cancelled');
            } else {
              line.textContent = tr('progress.failed', { msg: d.error_summary || d.status || '?' });
            }
            es.close();
            // Échec ou annulation : tout a été défait, on peut relancer après
            // un contrôle frais. Réussi : « Lancer » reste grisé jusqu'au
            // prochain contrôle demandé.
            if (d.status !== 'done') check();
          },
        },
      });
    }

    container.querySelectorAll('select[data-x], input[data-x]').forEach(el => {
      el.addEventListener('change', (ev) => { syncMac(); check(ev); });
    });
    q('check').addEventListener('click', () => check());
    q('start').addEventListener('click', start);
    syncMac();
    check({ target: { dataset: { x: 'to' } } });
  }

  // v1.47.0 : la fin d'un export désigne l'archive produite, avec ce qu'on
  // en fait ensuite ; avant, l'exploitant devait deviner où elle était.
  async function archiveBlock(box, name) {
    box.hidden = false;
    box.innerHTML = `
      <div class="xfer-archive-name">${Icons.svg('bundle', { size: 14 })} ${esc(tr('transfer.archive.ready'))} <code>${esc(name)}</code> <span class="tf-desc" data-x="archive-size"></span></div>
      <div class="apply-bar" style="margin:6px 0 0; padding:0; border:0;">
        <a class="btn btn-secondary btn-sm tip" data-x="archive-download" href="/api/exports/${enc(name)}/download" download data-tip="${esc(tr('transfer.archive.downloadTip'))}">${Icons.svg('download')} <span>${esc(tr('transfer.archive.download'))}</span></a>
        <button type="button" class="btn btn-secondary btn-sm tip" data-x="archive-import" data-tip="${esc(tr('transfer.archive.importTip'))}">${Icons.svg('upload')} <span>${esc(tr('transfer.archive.import'))}</span></button>
        <button type="button" class="btn btn-secondary btn-sm tip" data-x="archive-store" data-tip="${esc(tr('transfer.archive.storeTip'))}">${Icons.svg('bundle')} <span>${esc(tr('transfer.archive.store'))}</span></button>
      </div>`;
    box.querySelector('[data-x="archive-import"]').addEventListener('click', () => openImport(name));
    box.querySelector('[data-x="archive-store"]').addEventListener('click', () => openStore());
    try {
      const d = await fetch('/api/exports').then(r => r.json());
      const e = (d.exports || []).find(x => x.name === name);
      if (e) box.querySelector('[data-x="archive-size"]').textContent = `· ${XferProgress.bytes(e.size)}`;
    } catch (_) { /* la taille n'est qu'un plus */ }
  }

  // Débit et temps restant d'un envoi, mesurés sur les dix dernières
  // secondes, dans la forme des points de progression du serveur.
  function uploadMeter(total) {
    const t0 = Date.now();
    const samples = [[t0, 0]];
    return (loaded) => {
      const now = Date.now();
      samples.push([now, loaded]);
      while (samples.length > 2 && now - samples[1][0] >= 10000) samples.shift();
      const dt = (now - samples[0][0]) / 1000;
      const rate = dt > 0.5 ? (loaded - samples[0][1]) / dt : null;
      return { phase: 'upload', item: null, done: loaded, total, wire: 0, rate,
               eta: rate ? (total - loaded) / rate : null, elapsed: (now - t0) / 1000,
               final: false };
    };
  }

  // v1.47.0 : déposer une archive dans le magasin, depuis ce poste. Le
  // fichier part tel quel dans le corps de la requête (le serveur l'écrit
  // au fil de l'eau), puis le serveur vérifie ses sommes avant de l'accepter.
  function uploadArchive(file, box, done) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,200}\.hvx$/.test(file.name)) {
      box.hidden = false;
      box.querySelector('[data-x="up-line"]').textContent = tr('transfer.store.uploadBadName', { name: file.name });
      return null;
    }
    box.hidden = false;
    const line = box.querySelector('[data-x="up-line"]');
    const bar = box.querySelector('[data-x="up-bar"]');
    const meta = box.querySelector('[data-x="up-meta"]');
    const cancel = box.querySelector('[data-x="up-cancel"]');
    const meter = uploadMeter(file.size);
    let es = null, finished = false, serverEnd = null;
    line.textContent = tr('transfer.store.uploadStarting', { name: file.name });
    bar.style.width = '0%';
    meta.textContent = '';
    cancel.hidden = false;

    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/exports/${enc(file.name)}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => {
      if (finished) return;
      const snap = meter(e.loaded);
      if (e.loaded < file.size) line.textContent = XferProgress.text(snap);
      bar.style.width = XferProgress.pct(snap) + '%';
      meta.textContent = tr('progress.elapsed', { t: XferProgress.duration(snap.elapsed) });
    };
    // Tout est parti : le serveur vérifie les sommes (suivi par l'action).
    xhr.upload.onload = () => {
      if (finished) return;
      line.textContent = tr('transfer.store.verifying');
      bar.style.width = '0%';
    };
    const finish = (ok, msg) => {
      if (finished) return;
      finished = true;
      if (es) es.close();
      cancel.hidden = true;
      line.innerHTML = `<span style="color:var(${ok ? '--accent' : '--danger'})">${Icons.svg(ok ? 'ok' : 'fail', { size: 14 })} ${esc(msg)}</span>`;
      if (ok) bar.style.width = '100%';
      done(ok);
    };

    // L'action du serveur, suivie dès le début : la vérification des sommes
    // (la même progression que le dock), et une fin décidée ailleurs, par
    // « Annuler » du dock. Le navigateur ne voit pas toujours le serveur
    // fermer la connexion au milieu d'un envoi : la fenêtre continuait
    // d'afficher un envoi que le serveur avait arrêté (vu en réel, 1.47.2).
    async function followRun() {
      if (!window.SSEReconnect) return;
      const action = 'vm-archive-upload:' + file.name;
      let run = null;
      for (let i = 0; i < 20 && !run && !finished; i++) {
        try {
          const d = await fetch(`/api/activity?action=${enc(action)}`).then(r => r.json());
          run = (d.in_progress || []).find(a => a.action === action) || null;
        } catch (_) { /* nouvel essai */ }
        if (!run) await new Promise(r => setTimeout(r, 500));
      }
      if (!run || finished) return;
      es = SSEReconnect.connect(`/api/stream/${enc(run.id)}`, {
        on: {
          progress: (ev) => {
            const snap = JSON.parse(ev.data);
            if (snap.phase !== 'verify' || finished) return;
            line.textContent = XferProgress.text(snap);
            bar.style.width = XferProgress.pct(snap) + '%';
          },
          end: (ev) => {
            if (es) es.close();
            let d = {};
            try { d = JSON.parse(ev.data); } catch (_) { /* fin sans détail */ }
            // réussie : la réponse de la requête dit le reste
            if (d.status === 'done' || xhr.readyState === 4) return;
            serverEnd = d;
            xhr.abort();
          },
        },
      });
    }

    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch (_) { /* réponse sans corps */ }
      if (xhr.status === 201) {
        finish(true, tr('transfer.store.uploaded', { name: d.archive || file.name }));
      } else if (xhr.status === 507) {
        finish(false, tr('transfer.store.uploadNoRoom', { need: XferProgress.bytes(d.need),
                                                          free: XferProgress.bytes(d.free) }));
      } else if (d.error === 'cancelled') {
        finish(false, tr('transfer.store.uploadCancelled'));
      } else {
        finish(false, tr('transfer.store.uploadFailed', { msg: d.error || `HTTP ${xhr.status}` }));
      }
    };
    xhr.onerror = () => finish(false, tr('transfer.store.uploadFailed', { msg: tr('transfer.store.uploadLost') }));
    // arrêté ici (bouton Annuler de la fenêtre) ou par la fin de l'action
    xhr.onabort = () => {
      if (serverEnd && serverEnd.status !== 'cancelled') {
        finish(false, tr('transfer.store.uploadFailed', { msg: serverEnd.error_summary || serverEnd.status }));
      } else {
        finish(false, tr('transfer.store.uploadCancelled'));
      }
    };
    cancel.onclick = () => xhr.abort();
    xhr.send(file);
    followRun();
    return xhr;
  }

  async function clusterNames() {
    try {
      const d = await fetch('/api/clusters').then(r => r.json());
      return (d.clusters || []).map(c => c.name);
    } catch (_) {
      return [];
    }
  }

  // Appelé par la fenêtre « Migrer » pour les destinations cluster et fichier.
  async function render(container, ctx) {
    const clusters = await clusterNames();
    return renderForm(container, Object.assign({ clusters }, ctx));
  }

  // -------------------------------------------------------------------------
  // Magasin d'exports
  // -------------------------------------------------------------------------
  function openStore(cluster) {
    const panel = FloatingPanels.open({
      id: 'vm-exports',
      title: tr('transfer.store.title'),
      icon: 'bundle',
      bodyHtml: `<div class="migrate-panel xfer-store">
        <div class="apply-bar" style="margin:0 0 10px; padding:0; border:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="refresh" data-tip="${esc(tr('transfer.refreshTip'))}">${Icons.svg('refresh')} <span>${esc(tr('transfer.refresh'))}</span></button>
          <button type="button" class="btn btn-primary btn-sm tip" data-x="upload" data-tip="${esc(tr('transfer.store.uploadTip'))}">${Icons.svg('upload')} <span>${esc(tr('transfer.store.upload'))}</span></button>
          <input type="file" data-x="upload-file" accept=".hvx" hidden>
          <span class="apply-result" data-x="free"></span>
        </div>
        <fieldset class="tf-block xfer-live" data-x="up-box" hidden>
          <legend>${esc(tr('transfer.store.uploadTitle'))}</legend>
          <div class="xfer-live-line" data-x="up-line"></div>
          <div class="progress-mini xfer-live-bar"><div class="fill" data-x="up-bar" style="width:0%"></div></div>
          <div class="apply-bar" style="margin:4px 0 0; padding:0; border:0;">
            <span class="tf-desc" data-x="up-meta"></span>
            <button type="button" class="btn btn-secondary btn-sm tip" data-x="up-cancel" data-tip="${esc(tr('transfer.store.uploadCancelTip'))}" hidden>${Icons.svg('close')} <span>${esc(tr('transfer.store.uploadCancel'))}</span></button>
          </div>
        </fieldset>
        <table class="data-table" data-x="table">
          <thead><tr><th>${esc(tr('transfer.store.col.file'))}</th><th>${esc(tr('transfer.store.col.vm'))}</th>
            <th>${esc(tr('transfer.store.col.from'))}</th><th>${esc(tr('transfer.store.col.date'))}</th>
            <th>${esc(tr('transfer.store.col.size'))}</th><th></th></tr></thead>
          <tbody></tbody>
        </table></div>`,
      width: 820,
      height: 460,
      restoreSpec: { type: 'vm-exports', args: { cluster } },
    });
    const root = panel.el;
    // déjà ouvert : FloatingPanels rend la même fenêtre ; la brancher une
    // seconde fois doublerait chaque geste (deux dépôts du même fichier)
    if (root.dataset.storeReady) {
      root.querySelector('[data-x="refresh"]').click();
      return;
    }
    root.dataset.storeReady = '1';
    const tbody = root.querySelector('[data-x="table"] tbody');

    async function refresh() {
      try {
        const d = await fetch('/api/exports').then(r => r.json());
        root.querySelector('[data-x="free"]').textContent =
          d.free != null ? tr('transfer.store.free', { free: fmtBytes(d.free) }) : '';
        const rows = (d.exports || []).map(e => {
          const state = e.complete
            ? `<span class="badge ok">${esc(tr('transfer.store.complete'))}</span>`
            : `<span class="badge warn tip" data-tip="${esc(tr('transfer.store.incompleteTip'))}">${esc(tr('transfer.store.incomplete'))}</span>`;
          const vm = e.vm ? `${esc(e.namespace)}/${esc(e.vm)}` : '?';
          const from = e.cluster ? `${esc(e.cluster)} <span class="tf-desc">${esc(e.version || '')}</span>` : '?';
          return `<tr data-file="${esc(e.name)}">
            <td><code>${esc(e.name)}</code> ${state}</td><td>${vm}</td><td>${from}</td>
            <td>${e.created ? esc(new Date(e.created).toLocaleString()) : '?'}</td>
            <td>${esc(fmtBytes(e.size))}</td>
            <td class="vm-actions-cell">
              <a class="btn-icon-action tip" aria-label="${esc(tr('transfer.store.download'))}" data-tip="${esc(tr('transfer.store.downloadTip'))}" href="/api/exports/${enc(e.name)}/download" download>${Icons.svg('download')}</a>
              <button type="button" class="btn-icon-action tip" data-act="import" aria-label="${esc(tr('transfer.store.import'))}" data-tip="${esc(tr('transfer.store.importTip'))}" ${e.complete ? '' : 'disabled'}>${Icons.svg('upload')}</button>
              <button type="button" class="btn-icon-action tip" data-act="delete" aria-label="${esc(tr('transfer.store.delete'))}" data-tip="${esc(tr('transfer.store.deleteTip'))}">${Icons.svg('trash')}</button>
            </td></tr>`;
        });
        tbody.innerHTML = rows.join('') ||
          `<tr><td colspan="6" class="empty-state">${esc(tr('transfer.store.empty'))}</td></tr>`;
        tbody.querySelectorAll('tr[data-file]').forEach(tr => {
          const file = tr.dataset.file;
          tr.querySelector('[data-act="import"]').addEventListener('click', () => openImport(file));
          tr.querySelector('[data-act="delete"]').addEventListener('click', async () => {
            if (!confirm(tr('transfer.store.confirmDelete', { name: file }))) return;
            await fetch(`/api/exports/${enc(file)}`, { method: 'DELETE' });
            refresh();
          });
        });
      } catch (e) {
        tbody.innerHTML = `<tr><td colspan="6" style="color:var(--danger)">${esc(tr('transfer.error', { msg: e.message }))}</td></tr>`;
      }
    }
    root.querySelector('[data-x="refresh"]').addEventListener('click', refresh);
    const picker = root.querySelector('[data-x="upload-file"]');
    const upBtn = root.querySelector('[data-x="upload"]');
    upBtn.addEventListener('click', () => picker.click());
    picker.addEventListener('change', () => {
      const file = picker.files && picker.files[0];
      picker.value = '';
      if (!file) return;
      const xhr = uploadArchive(file, root.querySelector('[data-x="up-box"]'), (ok) => {
        upBtn.disabled = false;
        if (ok) refresh();
      });
      if (xhr) upBtn.disabled = true;
    });
    refresh();
  }

  async function openImport(file) {
    const panel = FloatingPanels.open({
      id: `vm-import-${file}`,
      title: tr('transfer.import.title', { file }),
      icon: 'upload',
      bodyHtml: '<div class="migrate-panel" data-x="body"></div>',
      width: 780,
      height: 600,
      restoreSpec: { type: 'vm-import', args: { file } },
    });
    let manifest = {};
    try {
      const d = await fetch('/api/exports').then(r => r.json());
      manifest = (d.exports || []).find(e => e.name === file) || {};
    } catch (_) { /* le formulaire se contente des valeurs vides */ }
    await render(panel.el.querySelector('[data-x="body"]'),
                 { kind: 'import', file, namespace: manifest.namespace, name: manifest.vm });
  }

  return { render, openStore, openImport, _findingText: findingText, _uploadMeter: uploadMeter };
})();

window.VMTransfer = VMTransfer;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-exports', (args) => VMTransfer.openStore(args.cluster));
  FloatingPanels.registerType('vm-import', (args) => VMTransfer.openImport(args.file));
}
