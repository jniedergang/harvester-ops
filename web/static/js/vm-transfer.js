/**
 * harvester-ops — déplacer une VM vers un autre cluster, l'exporter dans
 * un fichier, importer une archive (v1.45.0).
 *
 * La fenêtre « Migrer » (vm-migrate.js) propose trois destinations ; les
 * deux dernières sont rendues ici. Le serveur ne fait que relayer le script
 * `harvester-vm-transfer` : ce module compose la demande, affiche le
 * contrôle préalable dans la langue de l'interface, et lance l'action,
 * suivie ensuite dans le dock.
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

  function reportHtml(d) {
    const engine = d.engine === 'backup'
      ? tr('transfer.engine.backup')
      : tr('transfer.engine.file', { reason: (REASONS[d.reason] || (() => ''))() });
    const items = (d.findings || []).filter(f => f.code !== 'engine');
    const list = items.map(f => `
      <div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}" data-level="${esc(f.level)}">
        <div class="sto-finding-title">${esc(findingText(f))}</div>
      </div>`).join('');
    const ok = d.blocked ? '' : `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('transfer.noBlocker'))}</div></div>`;
    return `<p class="tf-desc xfer-engine">${esc(engine)}</p>${list}${ok}`;
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
          ${kind === 'migrate' ? `
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.forceFile'))}"><input data-x="engine_file" type="checkbox"><span class="tf-label">${esc(tr('transfer.forceFile'))}</span></label>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('transfer.keepBackups'))}"><input data-x="keep_backups" type="checkbox"><span class="tf-label">${esc(tr('transfer.keepBackups'))}</span></label>` : ''}
        </div>
        ${toCluster ? `<fieldset class="tf-block xfer-mappings"><legend>${esc(tr('transfer.mappings'))}</legend><div class="tf-args" data-x="maps"></div></fieldset>` : ''}
        <fieldset class="tf-block"><legend>${esc(tr('transfer.findings'))}</legend>
          <div class="xfer-report" data-x="report"></div></fieldset>
        <div class="apply-bar" style="margin:0; padding:0; border:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="check" data-tip="${esc(tr('transfer.checkTip'))}">${Icons.svg('refresh')} <span>${esc(tr('transfer.check'))}</span></button>
          <button type="button" class="btn btn-primary btn-sm tip" data-x="start" data-tip="${esc(tr('transfer.startTip'))}" disabled>${Icons.svg(kind === 'export' ? 'download' : kind === 'import' ? 'upload' : 'migrate')} <span>${esc((kind === 'export' ? tr('transfer.startExport') : kind === 'import' ? tr('transfer.startImport') : tr('transfer.start')))}</span></button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;

    const q = (x) => container.querySelector(`[data-x="${x}"]`);
    const state = { seq: 0, mapsFor: null };

    function syncMac() {
      const src = q('source'), mac = q('keep_mac');
      if (!src || !mac) return;
      const running = src.value === 'running';
      mac.disabled = running;
      if (running) mac.checked = false;
    }

    function body() {
      const b = {};
      ['to', 'namespace', 'name', 'mode', 'source', 'target'].forEach(k => {
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
      report.innerHTML = `<p class="tf-desc">${esc(tr('transfer.checking'))}</p>`;
      q('start').disabled = true;
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
        const mode = q('mode');
        if (mode) {
          const short = mode.querySelector('option[value="short"]');
          short.disabled = d.engine !== 'backup';
          if (short.disabled && mode.value === 'short') mode.value = 'stop';
        }
        q('start').disabled = !!d.blocked;
      } catch (e) {
        if (seq !== state.seq) return;
        report.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('transfer.error', { msg: e.message }))}</div></div>`;
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
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(tr('transfer.error', { msg: e.message }))}</span>`;
        q('start').disabled = false;
      }
    }

    container.querySelectorAll('select[data-x], input[data-x]').forEach(el => {
      el.addEventListener('change', (ev) => { syncMac(); check(ev); });
    });
    q('check').addEventListener('click', () => check());
    q('start').addEventListener('click', start);
    syncMac();
    check({ target: { dataset: { x: 'to' } } });
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
          <span class="apply-result" data-x="free"></span>
        </div>
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

  return { render, openStore, openImport, _findingText: findingText };
})();

window.VMTransfer = VMTransfer;
if (window.FloatingPanels) {
  FloatingPanels.registerType('vm-exports', (args) => VMTransfer.openStore(args.cluster));
  FloatingPanels.registerType('vm-import', (args) => VMTransfer.openImport(args.file));
}
