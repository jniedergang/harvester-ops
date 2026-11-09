/**
 * harvester-ops — Automation → Cluster API view.
 *
 * Stage 1 (now): diagnostic of the CAPI/CAPHV stack on the selected
 * Harvester cluster + list of CAPI clusters managed there.
 *
 * Stage 2 (next): install the bundle (action tracked), create cluster wizard,
 * topology view, scaling, K8s upgrades.
 */
const CAPI = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  // v1.48.0 : tous les textes de ces vues passent par la traduction (des
  // libellés anglais et quelques phrases en français dur y restaient).
  const tr = (k, v) => i18n.t(k, v);

  // Cluster de gestion (navigation 2.0) : plus de lecture du sélecteur
  const mgmtCluster = () => (window.App && App.managementCluster ? App.managementCluster() : '');

  // Le diagnostic CAPI interroge le cluster : plusieurs secondes sur un
  // cluster lent. Un « Loading… » qui vide la carte donnait l'impression
  // que l'onglet s'était cassé. Même voile flouté que les autres
  // chargements, posé sur la CARTE : le corps est réécrit par le rendu et
  // emporterait le voile avec lui.
  function veiled(bodyEl, cluster, work) {
    if (!window.Veil || !bodyEl) return work();
    return window.Veil.during(bodyEl.closest('.card') || bodyEl, {
      message: window.i18n ? i18n.t('common.loadingNamed') : 'Loading {name}…',
      name: cluster,
      delay: 250,
    }, work);
  }

  async function refresh() {
    if (!window.App) return;
    const cluster = window.App && App.managementCluster();
    if (!cluster) return;
    const out = $('#capi-status-body');
    if (!out) return;
    return veiled(out, cluster, async () => {
      try {
        // v1.48.0 : Harvester 1.9 embarque Rancher Turtles et son cœur
        // Cluster API. La vue d'avant (déploiements capi-system...) y aurait
        // dit « composants manquants » alors que tout est là.
        const [d, stack] = await Promise.all([
          fetch(`/api/capi/${encodeURIComponent(cluster)}/diag`).then(r => r.json()),
          fetch(`/api/capi/${encodeURIComponent(cluster)}/stack`).then(r => r.json()).catch(() => ({})),
        ]);
        if (stack && stack.turtles && stack.core) renderTurtles(out, cluster, stack, d);
        else render(out, d, stack);
      } catch (e) {
        out.innerHTML = `<div class="summary-bar bad">${esc(e.message)}</div>`;
      }
    });
  }

  function renderTurtles(out, cluster, st, d) {
    const head = st.ready
      ? `<div class="summary-bar ok">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.stack.ready'))}</div>`
      : `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${esc(tr('capi.stack.missing', { missing: (st.missing || []).join(', ') }))}</div>`;
    const by = (p) => (p.type === 'core' ? tr('capi.stack.by.turtles')
      : p.managed_by_console ? tr('capi.stack.by.console') : tr('capi.stack.by.other'));
    const rows = [st.core].concat(st.providers || []).filter(Boolean).map(p => `
      <tr>
        <td>${p.ready ? '<span class="badge ok">' + Icons.svg('ok', { size: 14 }) + '</span>'
                      : '<span class="badge warn">' + Icons.svg('pending', { size: 14 }) + '</span>'}</td>
        <td><strong>${esc(p.type)}/${esc(p.name)}</strong></td>
        <td><code>${esc(p.version || '-')}</code></td>
        <td>${esc(p.phase || '')}</td>
        <td class="form-hint">${esc(by(p))}</td>
      </tr>`).join('') + (st.optional || []).filter(o => !o.ready).map(o => `
      <tr>
        <td><span class="badge warn">${Icons.svg('pending', { size: 14 })}</span></td>
        <td><strong>${esc(o.provider)}</strong></td>
        <td><code>${esc(o.version || '-')}</code></td>
        <td>${esc(tr('capi.stack.optionalMissing'))}</td>
        <td class="form-hint">${esc(tr('capi.stack.optional'))}</td>
      </tr>`).join('');
    const shim = st.shim || {};
    const shimLine = shim.present && shim.ours
      ? `<p class="form-hint">${Icons.svg('info', { size: 12 })} ${esc(tr('capi.stack.shim'))}</p>`
      : (shim.needed ? `<p class="form-hint">${Icons.svg('warn', { size: 12 })} ${esc(tr('capi.stack.shimMissing'))}</p>` : '');
    const legacy = st.legacy ? `
      <div class="sto-finding sev-watch" style="margin-top:10px;"><div class="sto-finding-title">
        ${esc(tr('capi.stack.legacy', { namespaces: (st.legacy_namespaces || []).join(', ') }))}
        <button type="button" class="btn btn-danger btn-sm tip" id="btn-capi-legacy" data-tip="${esc(tr('capi.stack.t.removeLegacy'))}">${Icons.svg('delete')} ${esc(tr('capi.stack.removeLegacy'))}</button>
      </div></div>` : '';
    const bundle = (st.bundle || {});
    const bundleLine = bundle.active
      ? (bundle.turtles ? esc(tr('capi.stack.bundle', { name: bundle.active }))
                        : `<span style="color:var(--warn)">${esc(tr('capi.stack.bundleNoTurtles'))}</span>`)
      : `<span style="color:var(--warn)">${esc(tr('capi.stack.bundleNone'))}</span>`;
    out.innerHTML = `
      ${head}
      <div style="margin-top:8px;">
        <span class="phase Running tip" data-tip="${esc(tr('capi.tip.harvVersion'))}">Harvester ${esc(st.harvester_version || '?')}</span>
        <span class="form-hint">${esc(tr('capi.stack.turtlesCore', { version: (st.core || {}).version || '?' }))}</span>
      </div>
      <h4 style="margin-top:14px;">${esc(tr('capi.stack.title'))}</h4>
      <table class="data-table">
        <thead><tr><th></th><th>${esc(tr('capi.stack.col.provider'))}</th><th>${esc(tr('capi.stack.col.version'))}</th>
          <th>${esc(tr('capi.stack.col.state'))}</th><th>${esc(tr('capi.stack.col.by'))}</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      ${shimLine}
      ${legacy}
      <div class="apply-bar" style="margin-top:16px; padding-top:12px; flex-wrap:wrap; gap:8px;">
        <button class="btn btn-primary btn-sm tip" id="btn-capi-turtles-install" ${bundle.turtles ? '' : 'disabled'}
                data-tip="${esc(tr('capi.stack.t.install'))}">${Icons.svg('bundle')} ${esc(tr('capi.stack.install'))}</button>
        <button class="btn btn-secondary btn-sm tip" id="btn-capi-bundle-build" data-tip="${esc(tr('capi.tip.bundleBuild'))}">${Icons.svg('build')} ${esc(tr('capi.v.bundle.build'))}</button>
        <button class="btn btn-secondary btn-sm tip" id="btn-capi-bundle-upload" data-tip="${esc(tr('capi.tip.bundleUpload'))}">${Icons.svg('upload')} ${esc(tr('capi.v.bundle.upload'))}</button>
        <input type="file" id="capi-bundle-upload-input" accept=".tar.gz,.tgz" style="display:none">
        <button class="btn btn-danger btn-sm tip" id="btn-capi-uninstall" data-turtles="1"
                data-tip="${esc(tr('capi.tip.uninstallTurtles'))}">${Icons.svg('delete')} ${esc(tr('capi.v.legacy.uninstall'))}</button>
        <span class="apply-result" id="capi-bundle-upload-result"></span>
        <span class="form-hint" style="flex-basis:100%;">${bundleLine}</span>
        <span class="apply-result" id="capi-install-result" style="flex-basis:100%;"></span>
      </div>
      <div id="capi-bundles-panel" style="margin-top:18px;"><div class="loading-placeholder">${esc(tr('capi.v.bundles.loading'))}</div></div>`;
    renderBundles();
    const result = () => out.querySelector('#capi-install-result');
    const post = async (url, body, confirmText) => {
      if (!confirm(confirmText)) return;
      try {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                     body: JSON.stringify(body || {}) });
        const res = await r.json();
        if (!r.ok) throw new Error(res.error || `HTTP ${r.status}`);
        result().innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.stack.started', { id: res.action_id }))}</span>`;
      } catch (e) {
        result().innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(tr('capi.stack.error', { msg: e.message }))}</span>`;
      }
    };
    out.querySelector('#btn-capi-turtles-install')?.addEventListener('click', () =>
      post(`/api/capi/${encodeURIComponent(cluster)}/install`, { dry_run: false },
           tr('capi.stack.confirmInstall', { cluster })));
    out.querySelector('#btn-capi-legacy')?.addEventListener('click', () =>
      post(`/api/capi/${encodeURIComponent(cluster)}/cleanup-legacy`, { dry_run: false },
           tr('capi.stack.confirmLegacy', { cluster })));
  }

  function render(out, d, stack) {
    // Cluster déclaré mais hors tension : le dire, et surtout ne rien
    // conclure sur la pile.
    if (d.unreachable) {
      out.innerHTML = `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${
        esc(tr('overview.clusterUnreachable', { name: d.cluster || '' }))} <code>${esc(d.endpoint || '')}</code></div>`;
      return;
    }
    // ⚠️ `[].every()` vaut TRUE : sans le contrôle de longueur, une réponse
    // sans composants affichait « stack fully installed » en vert sur un
    // cluster qui ne répondait même pas.
    const comps = d.components || [];
    const allInstalled = comps.length > 0 && comps.every(c => c.installed);
    const summary = allInstalled
      ? `<div class="summary-bar ok">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.legacy.allInstalled'))}</div>`
      : `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${esc(tr('capi.v.legacy.someMissing'))}</div>`;

    // Target Harvester version + bundle compatibility chip
    const cmp = d.bundle_compatibility;
    let compatChip = '';
    if (d.harvester_version) {
      compatChip += `<span class="phase Running tip" data-tip="${esc(tr('capi.tip.harvVersion'))}">Harvester ${esc(d.harvester_version)}</span> `;
    }
    if (cmp) {
      if (cmp.compatible) {
        compatChip += `<span class="badge ok tip" data-tip="${esc(tr('capi.tip.compatOk', { versions: (cmp.supported_versions || []).join(', ') }))}">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.compat.ok'))}</span>`;
      } else {
        const sup = (cmp.supported_versions || []).join(', ') || tr('capi.v.unknown');
        compatChip += `<span class="badge warn tip" data-tip="${esc(tr('capi.tip.compatKo', { supported: sup, target: cmp.target_version || tr('capi.v.unknown') }))}">${Icons.svg('warn', { size: 14 })} ${esc(tr('capi.v.compat.mismatch', { supported: sup }))}</span>`;
      }
    }

    const componentRows = comps.map(c => `
      <tr>
        <td>${c.installed ? '<span class="badge ok">' + Icons.svg('ok', { size: 14 }) + '</span>' : '<span class="badge fail">' + Icons.svg('fail', { size: 14 }) + '</span>'}</td>
        <td><strong>${esc(c.label)}</strong></td>
        <td>${c.version ? `<code title="${esc(c.image || '')}">${esc(c.version)}</code>` : '<span class="form-hint">—</span>'}</td>
        <td><code>${esc(c.details || '—')}</code></td>
      </tr>`).join('');

    // Revue 2.5.0 (D2) : l'ancien retrait casserait une pile Turtles ;
    // 2.7.0 : sur elle, le bouton ouvre la fenêtre du retrait Turtles
    const turtles = !!(stack && stack.turtles);
    const bundleSection = `
      <div class="apply-bar" style="margin-top: 16px; padding-top: 12px; flex-wrap: wrap; gap: 8px;">
        <button class="btn btn-secondary btn-sm tip" id="btn-capi-bundle-build"
                data-tip="${esc(tr('capi.tip.bundleBuild'))}">
          ${Icons.svg('build')} ${esc(tr('capi.v.bundle.build'))}
        </button>
        <button class="btn btn-secondary btn-sm tip" id="btn-capi-bundle-upload"
                data-tip="${esc(tr('capi.tip.bundleUpload'))}">
          ${Icons.svg('upload')} ${esc(tr('capi.v.bundle.upload'))}
        </button>
        <input type="file" id="capi-bundle-upload-input" accept=".tar.gz,.tgz" style="display:none">
        <span class="apply-result" id="capi-bundle-upload-result" style="margin-left: 8px;"></span>
        <button class="btn btn-primary btn-sm tip" id="btn-capi-install" ${d.bundle_available ? '' : 'disabled'}
                data-tip="${esc(d.bundle_available ? tr('capi.tip.install') : tr('capi.tip.installNoBundle'))}">
          ${Icons.svg('bundle')} ${esc(tr('capi.v.legacy.install'))}
        </button>
        <button class="btn btn-danger btn-sm tip" id="btn-capi-uninstall" ${turtles ? 'data-turtles="1"' : ''}
                data-tip="${esc(turtles ? tr('capi.tip.uninstallTurtles') : tr('capi.tip.uninstall'))}">
          ${Icons.svg('delete')} ${esc(tr('capi.v.legacy.uninstall'))}
        </button>
        <label class="apply-dry tip" data-tip="${esc(tr('capi.tip.dryRun'))}">
          <input type="checkbox" id="capi-install-dry" checked> ${esc(tr('capi.v.legacy.dryRun'))}
        </label>
        <span class="form-hint" style="flex-basis: 100%; margin-top: 6px;">
          ${d.bundle_available
            ? `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.bundle.activeLabel'))} <code>${esc(d.active_bundle || 'capi-bundle.tar.gz')}</code></span>`
            : `<span style="color:var(--warn)">${Icons.svg('fail', { size: 14 })} ${esc(tr('capi.v.bundle.noneBuild'))}</span>`}
        </span>
        <span class="apply-result" id="capi-install-result" style="flex-basis: 100%;"></span>
      </div>
      <p class="form-hint" style="margin-top:6px;">${esc(tr('capi.v.legacy.hint'))}</p>
      <div id="capi-bundles-panel" style="margin-top: 18px;">
        <div class="loading-placeholder">${esc(tr('capi.v.bundles.loading'))}</div>
      </div>`;

    // Managed CAPI clusters table: its own tab (Clusters, 2.2.0).
    // The Installation tab is just: stack components + bundles management.
    out.innerHTML = `
      ${summary}
      ${compatChip ? `<div style="margin-top:8px;">${compatChip}</div>` : ''}
      <h4 style="margin-top:14px;">${esc(tr('capi.v.legacy.components'))}</h4>
      <table class="data-table">
        <thead><tr><th></th><th>${esc(tr('capi.v.col.component'))}</th><th>${esc(tr('capi.v.col.version'))}</th><th>${esc(tr('capi.v.col.details'))}</th></tr></thead>
        <tbody>${componentRows}</tbody>
      </table>
      ${bundleSection}`;
    renderBundles();
  }

  function fmtSize(bytes) {
    if (!bytes) return '0';
    const u = ['B','KB','MB','GB','TB'];
    let i = 0;
    while (bytes >= 1024 && i < u.length - 1) { bytes /= 1024; i++; }
    return `${bytes.toFixed(i < 2 ? 0 : 1)} ${u[i]}`;
  }
  function fmtBundleDate(filename) {
    // capi-bundle-YYYYMMDD-HHMMSS-<sha>.tar.gz → human
    const m = filename.match(/capi-bundle-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
    if (!m) return '—';
    return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]} UTC`;
  }

  async function renderBundles() {
    const panel = $('#capi-bundles-panel');
    if (!panel) return;
    let data;
    try {
      data = await fetch('/api/capi/bundles').then(r => r.json());
    } catch (e) {
      panel.innerHTML = `<div class="summary-bar bad">${esc(tr('capi.v.bundles.listFailed', { msg: e.message }))}</div>`;
      return;
    }
    const bundles = data.bundles || [];
    const totalUsed = data.total_used || 0;
    const diskFree = data.disk_free || 0;
    const diskTotal = data.disk_total || 0;
    const diskUsed = Math.max(0, diskTotal - diskFree);
    const diskUsedPct  = diskTotal ? Math.round((diskUsed  / diskTotal) * 100) : 0;
    const bundlesPct   = diskTotal ? Math.round((totalUsed / diskTotal) * 100) : 0;
    // The bar shows two stacked segments:
    //   blue   = disk space used by harvester-ops bundles
    //   grey   = everything else already on the disk
    // and the remainder is free. Numbers below are explicit so MB vs GB
    // confusion is impossible.
    const otherUsed = Math.max(0, diskUsed - totalUsed);
    const diskBar = `
      <div class="disk-usage tip" data-tip="${esc(tr('capi.tip.disk', { dir: data.bundle_dir || 'dist/' }))}">
        <div class="disk-usage-line">
          ${esc(tr('capi.v.disk.bundles', { count: bundles.length, size: fmtSize(totalUsed) }))}
          <span style="color:var(--text-dim)">${esc(tr('capi.v.disk.share', { pct: bundlesPct }))}</span>
        </div>
        <div class="disk-bar">
          <div class="fill fill-bundles" style="width:${bundlesPct}%" title="${esc(tr('capi.v.disk.byBundles', { size: fmtSize(totalUsed) }))}"></div>
          <div class="fill fill-other"   style="width:${Math.max(0, diskUsedPct - bundlesPct)}%" title="${esc(tr('capi.v.disk.byOther', { size: fmtSize(otherUsed) }))}"></div>
        </div>
        <div class="disk-usage-line" style="color:var(--text-dim);">
          ${esc(tr('capi.v.disk.usage', { used: fmtSize(diskUsed), total: fmtSize(diskTotal), pct: diskUsedPct, free: fmtSize(diskFree) }))}
        </div>
      </div>`;

    if (bundles.length === 0) {
      panel.innerHTML = `<h4>${esc(tr('capi.v.bundles.title'))}</h4>${diskBar}
        <div class="empty-state" style="padding:18px;">${esc(tr('capi.v.bundles.empty', { dir: data.bundle_dir || 'dist/' }))}</div>`;
      return;
    }

    const rows = bundles.map(b => `
      <tr data-filename="${esc(b.filename)}">
        <td>
          <a href="#" class="bundle-inspect tip" data-filename="${esc(b.filename)}"
             data-tip="${esc(tr('capi.tip.inspect'))}">
            <code>${esc(b.filename)}</code>
          </a>
        </td>
        <td>${esc(fmtBundleDate(b.filename))}</td>
        <td>${esc(fmtSize(b.size))}</td>
        <td><code class="sha">${esc((b.sha256 || '').slice(0, 12) || '—')}</code></td>
        <td>${b.is_active
              ? `<span class="badge ok tip" data-tip="${esc(tr('capi.tip.activeBundle'))}">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.bundle.activeBadge'))}</span>`
              : `<button class="btn btn-secondary btn-sm bundle-select tip" data-filename="${esc(b.filename)}"
                         data-tip="${esc(tr('capi.tip.activate'))}">
                   ${esc(tr('capi.v.bundle.activate'))}
                 </button>`}
        </td>
        <td>
          <button class="btn btn-secondary btn-sm bundle-inspect tip"
                  data-filename="${esc(b.filename)}"
                  data-tip="${esc(tr('capi.tip.inspect'))}">
            ${esc(tr('capi.v.bundle.inspect'))}
          </button>
          <a class="btn btn-secondary btn-sm bundle-download tip"
             href="/api/capi/bundle/${encodeURIComponent(b.filename)}/download"
             data-tip="${esc(tr('capi.tip.download'))}"
             download>
            ${Icons.svg('download')} ${esc(tr('capi.v.bundle.download'))}
          </a>
          <button class="btn btn-secondary btn-sm bundle-delete tip"
                  data-filename="${esc(b.filename)}"
                  data-tip="${esc(b.is_active ? tr('capi.v.bundle.deleteActive') : tr('capi.v.bundle.deleteTip'))}"
                  ${b.is_active ? 'disabled' : ''}>
            ${esc(tr('capi.v.delete'))}
          </button>
        </td>
      </tr>`).join('');

    panel.innerHTML = `
      <h4 style="margin-top:0;">${esc(tr('capi.v.bundles.title'))}</h4>
      ${diskBar}
      <table class="data-table" style="margin-top:8px;">
        <thead><tr>
          <th>${esc(tr('capi.v.col.filename'))}</th>
          <th>${esc(tr('capi.v.col.built'))}</th>
          <th>${esc(tr('capi.v.col.size'))}</th>
          <th>SHA-256</th>
          <th>${esc(tr('capi.v.col.active'))}</th>
          <th>${esc(tr('capi.v.col.actions'))}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function selectBundle(filename) {
    try {
      const r = await fetch('/api/capi/bundle/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || tr('capi.v.failed'));
      // Only repaint the bundle list + the small "Active bundle: …" hint —
      // no full diag refresh, which made the whole CAPI panel re-render.
      await renderBundles();
      const hint = document.querySelector('#capi-status-body .apply-bar .form-hint');
      if (hint) {
        hint.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.bundle.activeLabel'))} <code>${esc(filename)}</code></span>`;
      }
    } catch (e) {
      alert(tr('capi.v.bundle.activateFailed', { msg: e.message }));
    }
  }

  async function deleteBundle(filename) {
    if (!confirm(tr('capi.v.bundle.confirmDelete', { name: filename }))) return;
    try {
      const r = await fetch(`/api/capi/bundle/${encodeURIComponent(filename)}`,
                            { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || tr('capi.v.failed'));
      await renderBundles();
    } catch (e) {
      alert(tr('capi.v.bundle.deleteFailed', { msg: e.message }));
    }
  }

  async function inspectBundle(filename) {
    let data;
    try {
      data = await fetch(`/api/capi/bundle/${encodeURIComponent(filename)}/inspect`)
                   .then(r => r.json());
      if (data.error) throw new Error(data.error);
    } catch (e) {
      alert(tr('capi.v.bundle.inspectFailed', { msg: e.message }));
      return;
    }
    const m = data.manifest || {};
    const components = (m.components || []);
    const images = (m.images || []);
    const files = (data.files || []);
    const createdAt = m.bundle?.created_at_iso || tr('capi.v.inspect.unknownDate');
    const host = m.bundle?.host || '';
    const compatList = m.bundle?.compatible_harvester_versions || [];
    const notes = m.bundle?.notes || '';
    const compatBlock = compatList.length
      ? `<div class="summary-bar" style="margin-bottom:10px;">
           <strong>${esc(tr('capi.v.inspect.compatible'))}</strong> ${compatList.map(v => `<code>${esc(v)}</code>`).join(', ')}
           ${notes ? `<details style="margin-top:6px;"><summary>${esc(tr('capi.v.inspect.notes'))}</summary><pre style="white-space:pre-wrap;margin:6px 0 0;">${esc(notes)}</pre></details>` : ''}
         </div>`
      : `<p class="form-hint">${esc(tr('capi.v.inspect.noCompat'))}</p>`;
    const compTable = components.length
      ? `<table class="data-table"><thead><tr>
           <th>${esc(tr('capi.v.col.component'))}</th><th>${esc(tr('capi.v.col.version'))}</th>
           <th>${esc(tr('capi.v.col.images'))}</th><th>${esc(tr('capi.v.col.manifests'))}</th>
         </tr></thead><tbody>${components.map(c => `
           <tr>
             <td><strong>${esc(c.name)}</strong></td>
             <td><code>${esc(c.version || '—')}</code></td>
             <td>${esc(c.image_count)}</td>
             <td>${esc(c.manifest_count)}</td>
           </tr>`).join('')}</tbody></table>`
      : `<p class="form-hint">${esc(tr('capi.v.inspect.noMeta'))}</p>`;
    const imgList = images.length
      ? `<table class="data-table"><thead><tr><th>${esc(tr('capi.v.col.component'))}</th><th>${esc(tr('capi.v.col.image'))}</th><th>${esc(tr('capi.v.col.file'))}</th></tr></thead><tbody>${
         images.map(i => `
           <tr>
             <td>${esc(i.component || '—')}</td>
             <td><code>${esc(i.name || i)}</code></td>
             <td><code style="color:var(--text-dim)">${esc(i.file || '—')}</code></td>
           </tr>`).join('')}</tbody></table>`
      : `<p class="form-hint">${esc(tr('capi.v.inspect.noImages'))}</p>`;
    const fileList = files.length
      ? `<pre class="dock-action-log" style="display:block;max-height:220px;">${
          esc(files.slice(0, 200).map(f =>
            `${(f.size || '').toString().padStart(10)}  ${f.name}${f.is_dir ? '/' : ''}`
          ).join('\n'))}${files.length > 200 ? '\n' + esc(tr('capi.v.inspect.more', { count: files.length - 200 })) : ''}</pre>`
      : `<p class="form-hint">${esc(tr('capi.v.inspect.emptyTar'))}</p>`;
    const built = host ? tr('capi.v.inspect.summaryHost', { size: fmtSize(data.size), count: data.file_count, date: createdAt, host })
                       : tr('capi.v.inspect.summary', { size: fmtSize(data.size), count: data.file_count, date: createdAt });
    const body = `
      <div style="padding: 12px 14px;">
        <div class="summary-bar ok" style="margin-bottom:14px;">
          <code>${esc(filename)}</code> · ${esc(built)}
        </div>
        ${compatBlock}
        <h4 style="margin-top:0;">${esc(tr('capi.v.inspect.components'))}</h4>
        ${compTable}
        <h4>${esc(tr('capi.v.inspect.images'))}</h4>
        ${imgList}
        <h4>${esc(tr('capi.v.inspect.contents'))}</h4>
        ${fileList}
      </div>`;
    openBundlePanel(filename, body);
  }

  function openBundlePanel(filename, html) {
    const title = tr('capi.v.inspect.title', { name: filename });
    // Use FloatingPanels if available, otherwise fall back to a modal overlay.
    if (window.FloatingPanels?.open) {
      window.FloatingPanels.open({
        id: `bundle-inspect-${filename}`,
        title,
        bodyHtml: html,
        width: 720, height: 560,
      });
      return;
    }
    let overlay = document.querySelector('#bundle-inspect-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'bundle-inspect-overlay';
      overlay.className = 'overlay';
      overlay.innerHTML = `
        <div class="overlay-box" style="width:720px;max-width:95vw;max-height:90vh;overflow:auto;">
          <div class="overlay-header">
            <h3 id="bundle-inspect-title"></h3>
            <button class="btn-icon-sm tip" id="bundle-inspect-close" aria-label="${esc(tr('capi.v.close'))}" data-tip="${esc(tr('capi.v.close'))}">×</button>
          </div>
          <div id="bundle-inspect-body"></div>
        </div>`;
      document.body.appendChild(overlay);
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay || e.target.id === 'bundle-inspect-close') {
          overlay.style.display = 'none';
        }
      });
    }
    overlay.querySelector('#bundle-inspect-title').textContent = title;
    overlay.querySelector('#bundle-inspect-body').innerHTML = html;
    overlay.style.display = 'flex';
  }

  const failLine = (msg) => `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(msg)}</span>`;

  async function uninstall(cluster, dryRun, keepCertManager) {
    const result = document.querySelector('#capi-install-result');
    if (result) result.textContent = tr('capi.v.legacy.uninstallStarting');
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/uninstall`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun, keep_cert_manager: keepCertManager }),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = failLine(d.error || tr('capi.v.failed'));
        return;
      }
      if (result) {
        const line = dryRun ? tr('capi.v.legacy.uninstallStartedDry', { id: d.action_id })
                            : tr('capi.v.legacy.uninstallStarted', { id: d.action_id });
        const cm = keepCertManager ? '' : ' ' + tr('capi.v.legacy.withCertManager');
        result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(line + cm)}</span>`;
      }
    } catch (e) {
      if (result) result.innerHTML = failLine(e.message);
    }
  }

  // -------------------------------------------------------------------------
  // 2.7.0 (décision 215) : retrait de la pile Turtles posée par la console.
  // Fenêtre : ce qui part, les refus, ce qui reste ; images en option
  // décochée ; nom du cluster retapé. Le bouton reste grisé tant que le plan
  // n'est pas revenu sans refus ou que le nom ne correspond pas. Une réponse
  // arrivée après la fermeture n'agit plus (génération), la minuterie du
  // message d'attente est effacée à la fermeture et au lancement.
  // -------------------------------------------------------------------------
  let unGen = 0;
  let unTimer = null;
  const UN_FINDINGS = {
    'capi-clusters-present': (v) => tr('capi.un.fd.capi-clusters-present', v),
    'caaph-services-present': (v) => tr('capi.un.fd.caaph-services-present', v),
    'capi-harvesterclusters-present': (v) => tr('capi.un.fd.capi-harvesterclusters-present', v),
    'capi-plan-running': (v) => tr('capi.un.fd.capi-plan-running', v),
    'capi-images-in-use': (v) => tr('capi.un.fd.capi-images-in-use', v),
  };
  const UN_REASONS = {
    'core': () => tr('capi.un.reason.core'),
    'turtles': () => tr('capi.un.reason.turtles'),
    'not-managed': () => tr('capi.un.reason.not-managed'),
  };

  function closeUninstall() {
    unGen += 1;
    if (unTimer) { clearTimeout(unTimer); unTimer = null; }
    document.querySelector('#capi-uninstall-modal')?.remove();
  }

  function uninstallPlanHtml(p) {
    const refusals = (p.refusals || []).map(f => `<div class="sto-finding sev-critical" data-code="${esc(f.code)}"><div class="sto-finding-title">${
      esc(UN_FINDINGS[f.code] ? UN_FINDINGS[f.code](Object.assign({}, f.facts || {})) : f.code)}</div></div>`).join('');
    const provs = (p.providers || []).map(x => `<li><strong>${esc(x.type)}/${esc(x.name)}</strong> <code>${esc(x.version || '')}</code>
      <span class="form-hint">${esc(x.namespace || '')}</span></li>`).join('');
    const kept = (p.kept || []).map(k => `<li><strong>${esc(k.provider)}</strong> <span class="form-hint">${
      esc(UN_REASONS[k.reason] ? UN_REASONS[k.reason]() : k.reason)}</span></li>`).join('');
    return `${refusals ? `<h4>${esc(tr('capi.un.refusals'))}</h4>${refusals}` : ''}
      <h4>${esc(tr('capi.un.remove'))}</h4>
      ${provs ? `<ol data-x="providers">${provs}</ol>` : `<p class="form-hint">${esc(tr('capi.un.none'))}</p>`}
      ${(p.configmaps || []).length ? `<p class="form-hint">${esc(tr('capi.un.configmaps', { n: p.configmaps.length }))}</p>` : ''}
      ${p.shim ? `<p class="form-hint">${esc(tr('capi.un.shim'))}</p>` : ''}
      ${(p.images_in_use || []).length ? `<p class="form-hint" data-x="images-in-use">${esc(tr('capi.un.imagesInUse', {
        list: p.images_in_use.map(x => `${x.image} (${(x.used_by || []).join(', ')})`).join('; ') }))}</p>` : ''}
      <h4>${esc(tr('capi.un.kept'))}</h4>
      <ul data-x="kept">${kept}<li>${esc(tr('capi.un.keptCrds'))}</li></ul>`;
  }

  function openUninstall(cluster) {
    closeUninstall();
    const gen = unGen;
    const m = document.createElement('div');
    m.className = 'modal-overlay active';
    m.id = 'capi-uninstall-modal';
    m.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="capi-un-title">
        <div class="modal-header"><div><h3 id="capi-un-title">${Icons.svg('delete')} ${esc(tr('capi.un.title', { cluster }))}</h3>
          <div class="modal-subtitle">${esc(tr('capi.un.sub'))}</div></div>
          <button type="button" class="btn-close tip" data-x="close" data-tip="${esc(tr('common.cancel'))}" aria-label="${esc(tr('common.cancel'))}">×</button></div>
        <div class="modal-body">
          <div data-x="plan"></div>
          <label class="apply-dry tip" data-tip="${esc(tr('capi.un.t.images'))}">
            <input type="checkbox" data-x="images"> ${esc(tr('capi.un.images'))}</label>
          <div class="form-row"><label>${esc(tr('capi.un.confirm'))}</label>
            <input type="text" data-x="name" autocomplete="off" spellcheck="false" class="tip"
                   data-tip="${esc(tr('capi.un.t.confirm'))}" placeholder="${esc(cluster)}"></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="close" data-tip="${esc(tr('common.cancel'))}">${esc(tr('common.cancel'))}</button>
          <button type="button" class="btn btn-danger btn-sm tip" data-x="go" disabled data-tip="${esc(tr('capi.un.t.go'))}" title="${esc(tr('capi.un.t.go'))}">${Icons.svg('delete')} ${esc(tr('capi.un.go'))}</button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(m);
    const q = (x) => m.querySelector(`[data-x="${x}"]`);
    let plan = null;
    // revue 4-8 m8 : administrateur seulement (le serveur refuse aussi)
    const isAdmin = () => !!(window.App && App.role && App.role() === 'admin');
    const sync = () => {
      q('go').disabled = !(isAdmin() && plan && !plan.blocked && (plan.providers || []).length && q('name').value === cluster);
      const tp = isAdmin() ? tr('capi.un.t.go') : tr('capi.un.t.adminOnly');
      q('go').dataset.tip = tp;
      q('go').title = tp;
    };
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-x="close"]')) closeUninstall(); });
    m.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeUninstall(); });
    q('name').addEventListener('input', sync);
    unTimer = setTimeout(() => {
      unTimer = null;
      if (gen === unGen && !plan) q('plan').innerHTML = `<p class="form-hint">${esc(tr('capi.un.loading'))}</p>`;
    }, 250);
    fetch(`/api/capi/${encodeURIComponent(cluster)}/uninstall/plan`).then(async (r) => {
      const d = await r.json().catch(() => ({}));
      if (gen !== unGen) return;                         // fenêtre fermée entre-temps
      if (unTimer) { clearTimeout(unTimer); unTimer = null; }
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      plan = d;
      q('plan').innerHTML = uninstallPlanHtml(d);
      // 2.7.1 : seules les images chargées par la console, et aucune employée ailleurs
      const why = !(d.images || []).length ? tr('capi.un.t.imagesNone')
        : (d.images_in_use || []).length ? tr('capi.un.t.imagesBusy') : '';
      if (why) {
        q('images').checked = false;
        q('images').disabled = true;
        q('images').closest('label').dataset.tip = why;
      }
      sync();
    }).catch((e) => {
      if (gen !== unGen) return;
      q('plan').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('capi.un.failed', { msg: e.message }))}</div></div>`;
    });
    q('go').addEventListener('click', async () => {
      if (q('go').disabled) return;
      q('go').disabled = true;
      if (unTimer) { clearTimeout(unTimer); unTimer = null; }
      try {
        const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/uninstall`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: q('name').value, remove_images: q('images').checked }),
        });
        const d = await r.json().catch(() => ({}));
        if (gen !== unGen) return;
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        closeUninstall();
        if (typeof Dock !== 'undefined' && Dock.poll) Dock.poll();
        const res = document.querySelector('#capi-install-result');
        if (res) res.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.un.started', { id: d.action_id }))}</span>`;
      } catch (e) {
        if (gen !== unGen) return;
        q('feedback').innerHTML = failLine(e.message);
        sync();
      }
    });
    q('name').focus();
  }

  async function install(cluster, dryRun) {
    const result = document.querySelector('#capi-install-result');
    if (result) result.textContent = tr('capi.v.starting');
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/install`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun }),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = failLine(d.error || tr('capi.v.failed'));
        return;
      }
      if (result) {
        const started = dryRun ? tr('capi.v.legacy.installStartedDry', { id: d.action_id })
                               : tr('capi.v.legacy.installStarted', { id: d.action_id });
        let line = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(started)}</span>`;
        if (d.compatibility_warning) {
          const cw = d.compatibility_warning;
          const sup = (cw.supported_versions || []).join(', ') || tr('capi.v.unspecified');
          line += `<br><span style="color:var(--warn)">${Icons.svg('warn', { size: 14 })} ${esc(tr('capi.v.legacy.mismatch', { target: cw.target_version || tr('capi.v.unknown'), supported: sup }))}</span>`;
        }
        result.innerHTML = line;
      }
    } catch (e) {
      if (result) result.innerHTML = failLine(e.message);
    }
  }

  async function uploadBundle(file) {
    const result = document.querySelector('#capi-bundle-upload-result');
    if (result) result.innerHTML = `<span style="color:var(--text-dim)">${Icons.svg('pending', { size: 14 })} ${esc(tr('capi.v.upload.running', { name: file.name, size: fmtSize(file.size) }))}</span>`;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch('/api/capi/bundle/upload', { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = failLine(d.error || tr('capi.v.upload.failed'));
        return;
      }
      if (result) result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.upload.done', { name: d.uploaded }))}</span>`;
      await renderBundles();
      await refresh();
    } catch (e) {
      if (result) result.innerHTML = failLine(e.message);
    }
  }

  function init() {
    $('#btn-capi-refresh')?.addEventListener('click', refresh);
    $('#btn-capi-k8s-refresh')?.addEventListener('click', refreshK8sClustersPanel);
    $('#btn-capi-create')?.addEventListener('click', openCreate);
    $('#btn-capi-services-refresh')?.addEventListener('click', () => window.CapiServices && CapiServices.refresh(true));
    // Wizard interactions, all delegated.
    document.addEventListener('click', (e) => {
      const det = e.target.closest('.capi-cluster-details');
      if (det) { e.preventDefault(); showClusterDetails(det.dataset.ns, det.dataset.name); }
      const sca = e.target.closest('.capi-cluster-scale');
      if (sca) { e.preventDefault(); scaleCluster(sca.dataset.ns, sca.dataset.name); }
      const del = e.target.closest('.capi-cluster-delete');
      if (del) { e.preventDefault(); deleteCluster(del.dataset.ns, del.dataset.name); }
      const kc  = e.target.closest('.capi-cluster-kubeconfig');
      if (kc)  { e.preventDefault(); downloadKubeconfig(kc.dataset.ns, kc.dataset.name); }
      const acc = e.target.closest('.capi-cluster-access');
      if (acc) { e.preventDefault(); openAccess(acc.dataset.ns, acc.dataset.name); }
      const ren = e.target.closest('.capi-cluster-renew');
      if (ren) { e.preventDefault(); openRenew(ren.dataset.ns, ren.dataset.name); }
    });
    // Upload input change handler — delegated wiring done once
    document.addEventListener('change', (e) => {
      if (e.target?.id === 'capi-bundle-upload-input' && e.target.files?.[0]) {
        uploadBundle(e.target.files[0]);
        e.target.value = '';   // allow re-upload of the same file later
      }
    });
    // Install button is added dynamically by render(); use event delegation
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-capi-install')) {
        const cluster = mgmtCluster();
        const dry = document.querySelector('#capi-install-dry')?.checked ?? true;
        if (cluster && confirm(dry ? tr('capi.v.legacy.confirmInstallDry', { cluster })
                                   : tr('capi.v.legacy.confirmInstall', { cluster }))) {
          install(cluster, dry);
        }
      }
      if (e.target.closest('#btn-capi-uninstall')) {
        const cluster = mgmtCluster();
        if (!cluster) return;
        if (e.target.closest('#btn-capi-uninstall').dataset.turtles) { openUninstall(cluster); return; }
        const dry = document.querySelector('#capi-install-dry')?.checked ?? true;
        const keepCM = confirm(dry ? tr('capi.v.legacy.confirmUninstallDry', { cluster })
                                   : tr('capi.v.legacy.confirmUninstall', { cluster }));
        if (!confirm(dry ? tr('capi.v.legacy.confirmUninstall2Dry', { cluster })
                         : tr('capi.v.legacy.confirmUninstall2', { cluster }))) return;
        uninstall(cluster, dry, keepCM);
      }
      if (e.target.closest('#btn-capi-bundle-build')) {
        if (confirm(tr('capi.v.bundle.confirmBuild'))) {
          buildBundle();
        }
      }
      if (e.target.closest('#btn-capi-bundle-upload')) {
        const input = document.querySelector('#capi-bundle-upload-input');
        if (input) input.click();
      }
      const sel = e.target.closest('.bundle-select');
      if (sel) {
        e.preventDefault();
        selectBundle(sel.dataset.filename);
      }
      const del = e.target.closest('.bundle-delete');
      if (del && !del.disabled) {
        e.preventDefault();
        deleteBundle(del.dataset.filename);
      }
      const insp = e.target.closest('.bundle-inspect');
      if (insp) {
        e.preventDefault();
        inspectBundle(insp.dataset.filename);
      }
    });
  }

  async function buildBundle() {
    const result = document.querySelector('#capi-install-result');
    if (result) result.textContent = tr('capi.v.build.starting');
    let runId = null;
    try {
      const r = await fetch('/api/capi/bundle/build', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = failLine(d.error || tr('capi.v.failed'));
        return;
      }
      runId = d.action_id;
      if (result) {
        result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('pending', { size: 14 })} ${esc(tr('capi.v.build.running', { id: runId }))}</span>`;
      }
    } catch (e) {
      if (result) result.innerHTML = failLine(e.message);
      return;
    }
    // Follow the action and update label on completion
    if (!runId) return;
    SSEReconnect.connect(`/api/stream/${runId}`, {
      on: {
        end: (ev) => {
          const d = JSON.parse(ev.data);
          const span = document.querySelector('#capi-install-result');
          if (!span) return;
          if (d.status === 'done') {
            span.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.v.build.done', { id: runId }))}</span>`;
            setTimeout(refresh, 800);
          } else {
            span.innerHTML = failLine(tr('capi.v.build.failed', { code: d.exit_code ?? '?', id: runId }));
          }
        },
      },
    });
  }

  /** 2.0.0 : en-tête « Cluster de gestion » de Cluster API et Terraform
   *  (déclarations comprises) ; masqué sur Bare-metal, qui choisit son
   *  cluster à la jonction. La valeur est celle qu'utilisent les appels
   *  (App.managementCluster). */
  function paintMgmt(name) {
    const bar = $('#mgmt-cluster-bar');
    const pick = $('#mgmt-cluster-pick');
    if (!bar || !pick || !window.App) return;
    const list = App.listClusters();
    const cur = App.managementCluster();
    bar.hidden = name === 'pxe' || !list.length;
    const want = list.join('|');
    if (pick.dataset.list !== want) {
      pick.innerHTML = list.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
      pick.dataset.list = want;
    }
    if (cur) pick.value = cur;
  }

  /** Montre le panneau d'un sous-onglet d'Automatisation et sa bande
   *  d'onglets (data-only-for), sans rien relire. */
  function paintAutomationSubtab(name) {
    $$('#tab-automation > .sub-tab-content').forEach(x =>
      x.classList.toggle('active', x.dataset.subtab === name));
    // Chaque bande d'onglets appartient à un sous-onglet (data-only-for) :
    // Cluster API, Terraform (2.2.0) et Bare-metal (2.1.0)
    $$('#tab-automation .sub-tabs-inline[data-only-for]').forEach(x =>
      x.classList.toggle('hidden', x.dataset.onlyFor !== name));
  }

  function selectAutomationSubtab(name) {
    // 2.0.0 : l'entrée du menu (#automation/<x>) est peinte par HostNav.
    paintAutomationSubtab(name);
    try { localStorage.setItem('harvester_ops_automation_subtab', name); } catch {}
    paintMgmt(name);
    // Navigation 2.0 : l'adresse porte le sous-onglet (#automation/<x>)
    if (window.Router) Router.syncFromScreen();
    if (name === 'capi') { paintTabs(); renderTab(); }
    else if (name === 'terraform' && window.TF) window.TF.refresh();
    else if (name === 'pxe' && window.BMC) window.BMC.render();
  }

  // -------------------------------------------------------------------------
  // 2.2.0 : onglets de Cluster API (#automation/capi/<onglet>), dans l'ordre
  // Clusters (défaut), Services, GitOps (2.8.0), Secrets externes (2.10.0), Registres et Artefacts
  // (2.6.0), Historique, Installation
  // -------------------------------------------------------------------------
  const TABS = ['clusters', 'services', 'gitops', 'secrets', 'registries', 'artefacts', 'history', 'installation'];
  // Anciens noms internes (onglets d'avant la 2.2.0, et « Création de
  // clusters » de la v1.53.0, devenue une fenêtre de Clusters)
  const OLD_TABS = { k8s: 'clusters', install: 'installation' };
  let tab = 'clusters';

  function paintTabs() {
    const strip = $('#tab-automation .capi-tabs');
    if (strip) strip.setAttribute('aria-label', tr('capi.tabs'));
    $$('#tab-automation .sub-tab[data-capi-tab]').forEach((b) => {
      const on = b.dataset.capiTab === tab;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    $$('[data-subtab="capi"] .capi-tab-content').forEach(x =>
      x.classList.toggle('active', x.dataset.capiTab === tab));
  }

  /** Onglet demandé (adresse, clic) ; `render: false` = seulement retenu,
   *  le rendu viendra du routeur (reactivate). */
  function setTab(name, opts) {
    const o = opts || {};
    const n = OLD_TABS[name] || name;
    tab = TABS.includes(n) ? n : 'clusters';
    if (o.render === false) return;
    paintTabs();
    renderTab();
    if (o.sync && window.Router) Router.syncFromScreen();
  }

  function renderTab() {
    if (tab !== 'history') capiHistory.stop();
    if (tab === 'clusters') { refreshK8sClustersPanel(); paintStackBanner(); }
    else if (tab === 'services') { if (window.CapiServices) CapiServices.start(); }
    else if (tab === 'gitops') { if (window.CapiGitops) CapiGitops.start(); }
    else if (tab === 'secrets') { if (window.CapiEso) CapiEso.start(); }
    else if (tab === 'registries') { if (window.Registries) Registries.start(); }
    else if (tab === 'artefacts') { if (window.Artefacts) Artefacts.start(); }
    else if (tab === 'history') capiHistory.render();
    else refresh();
  }

  // Historique : l'Activité filtrée sur les actions Cluster API (capi-*)
  const historyShown = () => tab === 'history'
    && !!document.querySelector('#tab-automation.active > .sub-tab-content[data-subtab="capi"].active');

  // Libellé d'une action (verbe de l'Activité) ; table littérale, visible des
  // tests de traduction
  function kindLabel(action) {
    const verb = String(action || '').split(':', 1)[0];
    switch (verb) {
      case 'capi-install': return tr('capi.hist.kind.install');
      case 'capi-uninstall': return tr('capi.hist.kind.uninstall');
      case 'capi-cleanup-legacy': return tr('capi.hist.kind.cleanup');
      case 'capi-bundle-build': return tr('capi.hist.kind.bundle');
      case 'capi-cluster-create': return tr('capi.hist.kind.create');
      case 'capi-cluster-scale': return tr('capi.hist.kind.scale');
      case 'capi-cluster-delete': return tr('capi.hist.kind.delete');
      case 'capi-service-deploy': return tr('capi.hist.kind.deploy');
      case 'capi-service-remove': return tr('capi.hist.kind.remove');
      // 2.8.0 : accès, GitOps et renouvellement des machines
      case 'capi-rancher-import': return tr('capi.hist.kind.rancherImport');
      case 'capi-rancher-unimport': return tr('capi.hist.kind.rancherUnimport');
      case 'capi-gitops': return tr('capi.hist.kind.gitops');
      case 'capi-gitops-remove': return tr('capi.hist.kind.gitopsRemove');
      // 2.10.0 : External Secrets
      case 'capi-eso': return tr('capi.hist.kind.eso');
      case 'capi-eso-remove': return tr('capi.hist.kind.esoRemove');
      case 'capi-eso-namespace': return tr('capi.hist.kind.esoNamespace');
      case 'capi-eso-secret': return tr('capi.hist.kind.esoSecret');
      case 'capi-renew': return tr('capi.hist.kind.renew');
      case 'capi-renew-abandon': return tr('capi.hist.kind.renewAbandon');
      default: return verb;
    }
  }

  const capiHistory = AutoHistory.create({
    box: () => $('#capi-history-body'), queries: ['capi-'], cls: 'capi', shown: historyShown, cluster: true,
    label: (a) => kindLabel(a.action), hint: () => tr('capi.hist.hint'), empty: () => tr('capi.hist.empty'),
  });

  // Vérifier avant d'agir : la pile Cluster API du cluster de gestion. Rien
  // n'est affirmé quand la lecture échoue ou que le cluster ne répond pas.
  let bannerRev = 0;
  async function paintStackBanner() {
    const box = $('#capi-stack-banner');
    if (!box) return;
    const cluster = mgmtCluster();
    const mine = ++bannerRev;
    if (!cluster) { box.innerHTML = ''; return; }
    // une lecture plus récente, ou un autre cluster de gestion : on s'arrête
    // là (jamais de seconde lecture sur l'ancien cluster)
    const stale = () => mine !== bannerRev || tab !== 'clusters' || mgmtCluster() !== cluster;
    let missing = null;
    try {
      const st = await fetch(`/api/capi/${enc(cluster)}/stack`).then(r => (r.ok ? r.json() : null));
      if (stale()) return;
      if (st && st.turtles && st.core) {
        missing = st.ready ? [] : (st.missing || []);
      } else {
        // Harvester sans Turtles : l'ancienne installation (diagnostic)
        const d = await fetch(`/api/capi/${enc(cluster)}/diag`).then(r => (r.ok ? r.json() : null));
        const comps = (d && !d.unreachable && d.components) || [];
        if (comps.length) missing = comps.filter(c => !c.installed).map(c => c.label);
        else if (st && !st.turtles) missing = st.missing || [];
      }
    } catch (e) { missing = null; }
    if (stale()) return;
    box.innerHTML = missing && missing.length
      ? `<div class="ct-banner auto-banner-warn capi-stack-missing" role="status">${Icons.svg('warn', { size: 14 })}
           <span>${esc(tr('capi.banner.missing', { cluster, missing: missing.join(', ') }))}</span>
           <a href="#automation/capi/installation" class="btn btn-sm btn-secondary tip" data-capi-goto="installation"
              data-tip="${esc(tr('capi.banner.openTip'))}">${esc(tr('capi.banner.open'))}</a></div>`
      : '';
  }

  function initSubtabs() {
    // 2.0.0 : changer de cluster de gestion relit la page affichée, sans
    // toucher à l'hôte ouvert
    $('#mgmt-cluster-pick')?.addEventListener('change', (e) => {
      if (window.App && App.setManagementCluster) App.setManagementCluster(e.target.value);
    });
    document.addEventListener('click', (e) => {
      // Onglets de Cluster API (bande de l'en-tête) et liens vers un onglet
      const b = e.target.closest('#tab-automation .sub-tab[data-capi-tab]');
      if (b) { e.preventDefault(); setTab(b.dataset.capiTab, { sync: true }); return; }
      const g = e.target.closest('[data-capi-goto]');
      if (g) { e.preventDefault(); setTab(g.dataset.capiGoto, { sync: true }); return; }
      if (e.target.closest('#btn-capi-history-refresh')) capiHistory.render();
    });
    // Le sous-onglet retenu est seulement peint : l'écran est rendu par le
    // routeur (reactivate), jamais au chargement d'un autre écran.
    let saved = null;
    try { saved = localStorage.getItem('harvester_ops_automation_subtab'); } catch {}
    paintAutomationSubtab(['capi', 'terraform', 'pxe'].includes(saved) ? saved : 'capi');
    paintTabs();
  }

  // ---------------------------------------------------------------------
  // Création d'un cluster : une fenêtre à menus (capi-create.js, v1.53.0)
  // ---------------------------------------------------------------------
  function openCreate() {
    const cluster = mgmtCluster();
    if (!cluster || !window.CapiCreate) return;
    CapiCreate.open(cluster);
  }

  async function refreshK8sClustersPanel() {
    const out = document.querySelector('#capi-k8s-body');
    if (!out) return;
    const cluster = mgmtCluster();
    if (!cluster) { out.innerHTML = `<p class="form-hint">${esc(tr('capi.new.pickCluster'))}</p>`; return; }
    return veiled(out, cluster, () => k8sClustersInner(out, cluster));
  }

  // Classe d'affichage d'une phase : seulement des valeurs connues, jamais
  // la chaîne reçue du cluster.
  const phaseClass = (phase, ok) => (phase === ok ? 'Running' : phase === 'Failed' ? 'Failed' : 'Pending');
  const enc = encodeURIComponent;

  async function k8sClustersInner(out, cluster) {
    let diag;
    try { diag = await fetch(`/api/capi/${enc(cluster)}/diag`).then(r => r.json()); }
    catch (e) { out.innerHTML = `<div class="summary-bar bad">${esc(e.message)}</div>`; return; }

    const clusters = diag.capi_clusters || [];
    if (clusters.length === 0) {
      out.innerHTML = `<p class="empty-state">${esc(tr('capi.v.k8s.empty', { cluster, tab: tr('capi.k8s.create') }))}</p>`;
      return;
    }
    const rows = clusters.map(c => {
      const ref = `data-ns="${esc(c.namespace)}" data-name="${esc(c.name)}"`;
      return `
      <tr>
        <td><code>${esc(c.namespace)}/${esc(c.name)}</code></td>
        <td><span class="phase ${phaseClass(c.phase, 'Provisioned')}">${esc(c.phase)}</span></td>
        <td>${c.ready ? '<span class="badge ok">' + Icons.svg('ok', { size: 14 }) + '</span>' : '<span class="badge warn">…</span>'}</td>
        <td><code>${esc(c.clusterClass || '—')}</code></td>
        <td><code>${esc(c.k8sVersion || '—')}</code></td>
        <td>
          <button class="btn btn-sm btn-secondary capi-cluster-details tip" ${ref} data-tip="${esc(tr('capi.tip.clusterDetails'))}">${esc(tr('capi.v.details'))}</button>
          <button class="btn btn-sm btn-secondary capi-cluster-kubeconfig tip" ${ref} data-tip="${esc(tr('capi.tip.clusterKubeconfig'))}">${Icons.svg('download')} kubeconfig</button>
          <button class="btn btn-sm btn-secondary capi-cluster-access tip" ${ref} data-tip="${esc(tr('capi.acc.t.open'))}">${Icons.svg('key')} ${esc(tr('capi.acc.open'))}</button>
          <button class="btn btn-sm btn-secondary capi-cluster-renew tip" ${ref} data-tip="${esc(tr('capi.ren.t.open'))}">${Icons.svg('refresh')} ${esc(tr('capi.ren.open'))}</button>
          <button class="btn btn-sm btn-secondary capi-cluster-scale tip" ${ref} data-tip="${esc(tr('capi.tip.clusterScale'))}">${Icons.svg('moveVertical')} ${esc(tr('capi.v.scale'))}</button>
          <button class="btn btn-sm btn-danger capi-cluster-delete tip" ${ref} data-tip="${esc(tr('capi.tip.clusterDelete'))}">${Icons.svg('delete')} ${esc(tr('capi.v.delete'))}</button>
        </td>
      </tr>`;
    }).join('');
    out.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>${esc(tr('capi.v.col.name'))}</th>
          <th>${esc(tr('capi.v.col.phase'))}</th>
          <th>${esc(tr('capi.v.col.ready'))}</th>
          <th>ClusterClass</th>
          <th>K8s</th>
          <th>${esc(tr('capi.v.col.actions'))}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function showClusterDetails(namespace, name) {
    const cluster = mgmtCluster();
    const panelId = `capi-cluster-${namespace}-${name}`;
    if (!window.FloatingPanels) { alert(tr('migrate.reload')); return; }
    const api = window.FloatingPanels.open({
      id: panelId, title: `${namespace}/${name}`, icon: 'tools', width: 720, height: 540,
      bodyHtml: `<div style="padding:16px;">${esc(tr('common.loading'))}</div>`,
    });
    try {
      const d = await fetch(`/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/details`).then(r => r.json());
      if (d.error) throw new Error(d.error);
      const cond = (d.conditions || []).map(c => `
        <tr><td>${esc(c.type)}</td><td>${esc(c.status)}</td>
            <td>${esc(c.reason || '')}</td>
            <td class="form-hint">${esc((c.message || '').slice(0, 120))}</td></tr>`).join('');
      const machRows = (d.machines || []).length === 0
        ? `<tr><td colspan="4" class="empty-state">${esc(tr('capi.v.details.noMachines'))}</td></tr>`
        : d.machines.map(m => `
            <tr>
              <td><code>${esc(m.name)}</code></td>
              <td><span class="phase ${phaseClass(m.phase, 'Running')}">${esc(m.phase)}</span></td>
              <td>${esc(m.nodeName || '—')}</td>
              <td><code>${esc(m.k8sVersion || '—')}</code></td>
            </tr>`).join('');
      const ep = d.controlPlaneEndpoint || {};
      api.setBody(`
        <div style="padding:12px 14px;">
          <div class="summary-bar ${d.ready ? 'ok' : 'warn'}">
            <strong>${esc(d.namespace)}/${esc(d.name)}</strong> · ${esc(tr('capi.v.details.phase'))} <strong>${esc(d.phase)}</strong> ·
            ${esc(tr('capi.v.details.ready'))} ${d.ready ? Icons.svg('ok', { size: 14 }) : '…'} ·
            ${esc(tr('capi.v.details.endpoint'))} <code>${esc(ep.host || '—')}:${esc(ep.port || '—')}</code>
          </div>
          ${d.identity ? `<p class="capi-identity tip" data-identity="${esc(d.identity)}" data-tip="${esc(d.identity === 'restricted'
            ? tr('capi.v.details.t.identityRestricted') : tr('capi.v.details.t.identityConsole'))}">${Icons.svg(d.identity === 'restricted' ? 'ok' : 'warn', { size: 13 })}
            ${esc(d.identity === 'restricted' ? tr('capi.v.details.identityRestricted') : tr('capi.v.details.identityConsole'))}</p>` : ''}
          <h4 style="margin-top:14px;">${esc(tr('capi.v.details.conditions'))}</h4>
          <table class="data-table"><thead><tr><th>${esc(tr('capi.v.col.type'))}</th><th>${esc(tr('capi.v.col.status'))}</th><th>${esc(tr('capi.v.col.reason'))}</th><th>${esc(tr('capi.v.col.message'))}</th></tr></thead><tbody>${cond}</tbody></table>
          <h4>${esc(tr('capi.v.details.machines'))}</h4>
          <table class="data-table"><thead><tr><th>${esc(tr('capi.v.col.name'))}</th><th>${esc(tr('capi.v.col.phase'))}</th><th>${esc(tr('capi.v.col.node'))}</th><th>K8s</th></tr></thead><tbody>${machRows}</tbody></table>
          <details style="margin-top:14px;">
            <summary>${esc(tr('capi.v.details.topology'))}</summary>
            <pre class="dock-action-log" style="display:block;max-height:200px;">${esc(JSON.stringify(d.topology, null, 2))}</pre>
          </details>
        </div>`);
    } catch (e) {
      api.setBody(`<div style="padding:16px;"><span style="color:var(--danger)">${esc(tr('capi.v.failedMsg', { msg: e.message }))}</span></div>`);
    }
  }

  async function scaleCluster(namespace, name) {
    const cluster = mgmtCluster();
    const n = prompt(tr('capi.v.scale.prompt', { name: `${namespace}/${name}` }), '2');
    if (n === null) return;
    const replicas = parseInt(n, 10);
    if (!Number.isInteger(replicas) || replicas < 0) { alert(tr('capi.v.scale.invalid')); return; }
    try {
      const r = await fetch(`/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/scale`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replicas }),
      });
      const d = await r.json();
      if (!r.ok) { alert(tr('capi.v.scale.failed', { msg: d.error || tr('capi.v.unknown') })); return; }
      // v1.48.0 : c'est la liste qui change, pas le formulaire de création
      setTimeout(refreshK8sClustersPanel, 1500);
    } catch (e) { alert(e.message); }
  }

  async function deleteCluster(namespace, name) {
    const cluster = mgmtCluster();
    const ref = `${namespace}/${name}`;
    if (!confirm(tr('capi.v.remove.confirm', { name: ref }))) return;
    if (!confirm(tr('capi.v.remove.confirm2', { name: ref }))) return;
    try {
      const r = await fetch(`/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}`, { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok) { alert(tr('capi.v.remove.failed', { msg: d.error || tr('capi.v.unknown') })); return; }
      setTimeout(refreshK8sClustersPanel, 1500);
    } catch (e) { alert(e.message); }
  }

  async function downloadKubeconfig(namespace, name) {
    const cluster = mgmtCluster();
    window.open(`/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/kubeconfig`, '_blank');
  }

  // ---------------------------------------------------------------------
  // 2.8.0 (tâche 9) : accès humain d'un cluster enfant (import dans le
  // Rancher de la session, OIDC de l'API, membres dans Rancher) et
  // renouvellement de ses machines. Fenêtres à onglets, comme Déployer ;
  // les boutons qui écrivent restent grisés, raison dans la bulle, tant que
  // la vérification n'est pas revenue.
  // ---------------------------------------------------------------------
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const tabsHtml = (tabs, label) => `<div class="sub-tabs sub-tabs-inline capi-win-tabs" role="tablist" aria-label="${esc(label)}">
    ${tabs.map(([k, text, tip, ic], i) => `<button type="button" role="tab" class="sub-tab tip${i ? '' : ' active'}" data-win-tab="${k}"
      aria-selected="${i ? 'false' : 'true'}" data-tip="${esc(tip)}">${Icons.svg(ic, { size: 13 })} <span>${esc(text)}</span></button>`).join('')}</div>`;
  function showWinTab(root, k) {
    root.querySelectorAll('[data-win-tab]').forEach(b => {
      b.classList.toggle('active', b.dataset.winTab === k);
      b.setAttribute('aria-selected', b.dataset.winTab === k ? 'true' : 'false');
    });
    root.querySelectorAll('[data-win-pane]').forEach(x => { x.hidden = x.dataset.winPane !== k; });
  }
  /** Bouton qui écrit : grisé avec sa raison (bulle sur l'enveloppe, le
   *  bouton grisé ne reçoit pas le survol). */
  function gate(wrap, btn, ok, why) {
    btn.disabled = !ok;
    btn.title = why;
    btn.setAttribute('aria-label', why);
    wrap.dataset.tip = why;
  }
  /** Suit une action jusqu'à sa fin : ligne de progression, puis `done`. */
  function followAction(id, line, done) {
    line.hidden = false;
    line.textContent = tr('capi.acc.started', { id });
    if (!window.SSEReconnect) return;
    const es = SSEReconnect.connect(`/api/stream/${enc(id)}`, {
      on: {
        step: (e) => { try { const st = JSON.parse(e.data); if (st.message) line.textContent = st.message; } catch { /* ligne illisible */ } },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          line.textContent = d.status === 'done' ? tr('capi.acc.actionDone', { id })
            : tr('capi.acc.actionFailed', { id, msg: d.error_summary || d.status || '?' });
          if (done) done(d.status === 'done');
        },
      },
    });
  }
  async function jsonCall(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
                                 body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  // --- Accès -------------------------------------------------------------
  async function openAccess(namespace, name) {
    const cluster = mgmtCluster();
    const ref = `${namespace}/${name}`;
    if (!window.FloatingPanels || !cluster) return;
    const id = `capi-access-${namespace}-${name}`;
    if (document.getElementById(`fp-${id}`)) FloatingPanels.close(id);
    const panel = FloatingPanels.open({
      id, title: tr('capi.acc.title', { name: ref }), icon: 'key', width: Math.min(760, Math.max(300, window.innerWidth - 120)), height: 480,
      bodyHtml: `<div class="capi-access-win" style="padding:12px 14px;">
        ${tabsHtml([['rancher', tr('capi.acc.tab.rancher'), tr('capi.acc.t.tabRancher'), 'plug'],
                    ['oidc', tr('capi.acc.tab.oidc'), tr('capi.acc.t.tabOidc'), 'key'],
                    ['members', tr('capi.acc.tab.members'), tr('capi.acc.t.tabMembers'), 'user']], tr('capi.acc.title', { name: ref }))}
        <section data-win-pane="rancher" role="tabpanel"><p class="form-hint">${esc(tr('common.loading'))}</p></section>
        <section data-win-pane="oidc" role="tabpanel" hidden><p class="form-hint">${esc(tr('common.loading'))}</p></section>
        <section data-win-pane="members" role="tabpanel" hidden><p class="form-hint">${esc(tr('common.loading'))}</p></section>
      </div>`,
    });
    const root = panel.el.querySelector('.capi-access-win');
    root.querySelectorAll('[data-win-tab]').forEach(b => b.addEventListener('click', () => showWinTab(root, b.dataset.winTab)));
    const pane = (k) => root.querySelector(`[data-win-pane="${k}"]`);

    async function load() {
      const [rs, dt] = await Promise.all([
        jsonCall('GET', `/api/capi/${enc(cluster)}/rancher-status`).catch(e => ({ ok: false, d: { error: e.message } })),
        jsonCall('GET', `/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/details`).catch(e => ({ ok: false, d: { error: e.message } })),
      ]);
      if (!root.isConnected) return;
      const session = rs.ok ? (rs.d.session || null) : null;
      const row = rs.ok ? ((rs.d.clusters || []).find(x => x.namespace === namespace && x.cluster === name) || null) : null;
      paintRancher(rs, session, row);
      paintOidc(dt);
      paintMembers(session, row);
    }

    function paintRancher(rs, session, row) {
      const box = pane('rancher');
      if (!rs.ok) { box.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(rs.d.error || `HTTP ${rs.status}`)}</div></div>`; return; }
      const imported = !!(row && row.imported);
      const ours = imported && session && row.rancher === session;
      const turtles = !!(row && row.turtles);
      const state = !imported ? `<span class="badge dim">${esc(tr('capi.acc.notImported'))}</span>`
        : row.ready ? `<span class="badge ok">${esc(tr('capi.acc.ready'))}</span>`
          : `<span class="badge warn">${esc(row.state || tr('capi.acc.notReady'))}</span>`;
      const run = imported && row.origin && row.origin.run;
      box.innerHTML = `
        <dl class="reg-facts capi-access-facts">
          <dt>${esc(tr('capi.acc.session'))}</dt><dd>${session ? `<code>${esc(session)}</code>` : esc(tr('capi.acc.noSession'))}</dd>
          <dt>${esc(tr('capi.acc.state'))}</dt><dd>${state}</dd>
          ${imported ? `<dt>${esc(tr('capi.acc.where'))}</dt><dd><code>${esc(row.rancher)}</code> · <code>${esc(row.name)}</code> <span class="form-hint">${esc(row.cid)}</span></dd>` : ''}
          ${run ? `<dt>${esc(tr('capi.acc.origin'))}</dt><dd><a class="tip" href="#activity/action/${enc(run)}" data-tip="${esc(tr('capi.acc.t.origin'))}"><code>${esc(run)}</code></a></dd>` : ''}
        </dl>
        ${turtles ? `<div class="sto-finding sev-watch" data-code="turtles"><div class="sto-finding-title">${esc(tr('capi.acc.turtles'))}</div></div>` : ''}
        ${imported && session && !ours ? `<div class="sto-finding sev-watch" data-code="other-rancher"><div class="sto-finding-title">${esc(tr('capi.acc.otherRancher', { rancher: row.rancher }))}</div></div>` : ''}
        <p class="form-hint">${esc(tr('capi.acc.rancherIntro'))}</p>
        <div class="apply-bar" style="margin:8px 0 0;">
          <span class="tip" data-x="import-wrap"><button type="button" class="btn btn-primary btn-sm" data-x="import">${Icons.svg('upload')} ${esc(tr('capi.acc.import'))}</button></span>
          <span class="tip" data-x="unimport-wrap"><button type="button" class="btn btn-danger btn-sm" data-x="unimport">${Icons.svg('delete')} ${esc(tr('capi.acc.unimport'))}</button></span>
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="reload" data-tip="${esc(tr('capi.acc.t.reload'))}">${Icons.svg('refresh')} ${esc(tr('capi.acc.reload'))}</button>
          <span class="apply-result" data-x="line" hidden></span>
        </div>`;
      const q = (x) => box.querySelector(`[data-x="${x}"]`);
      const why = !can('operator') ? tr('capi.acc.t.operator')
        : !session ? tr('capi.acc.t.noSession') : turtles ? tr('capi.acc.t.turtlesConflict')
          : imported ? tr('capi.acc.t.already') : tr('capi.acc.t.import', { rancher: session });
      gate(q('import-wrap'), q('import'), can('operator') && !!session && !imported && !turtles, why);
      const whyUn = !can('operator') ? tr('capi.acc.t.operator') : !imported ? tr('capi.acc.t.notImported')
        : !session ? tr('capi.acc.t.noSession') : !ours ? tr('capi.acc.otherRancher', { rancher: row.rancher }) : tr('capi.acc.t.unimport');
      gate(q('unimport-wrap'), q('unimport'), can('operator') && imported && !!ours, whyUn);
      q('reload').addEventListener('click', load);
      const go = async (method, url, body, confirmText) => {
        if (!confirm(confirmText)) return;
        q('import').disabled = true;
        q('unimport').disabled = true;
        const r = await jsonCall(method, url, body);
        const line = q('line');
        if (!r.ok) {
          line.hidden = false;
          line.textContent = r.d.code === 'rancher-session-needed' ? tr('capi.acc.t.noSession') : (r.d.error || `HTTP ${r.status}`);
          return;
        }
        followAction(r.d.action_id, line, () => setTimeout(load, 800));
      };
      q('import').addEventListener('click', () => !q('import').disabled && go('POST', `/api/capi/${enc(cluster)}/rancher-import`,
        { cluster: ref }, tr('capi.acc.confirmImport', { name: ref, rancher: session || '' })));
      q('unimport').addEventListener('click', () => !q('unimport').disabled && go('DELETE',
        `/api/capi/${enc(cluster)}/rancher-import/${enc(namespace)}/${enc(name)}`, null, tr('capi.acc.confirmUnimport', { name: ref })));
    }

    function paintOidc(dt) {
      const box = pane('oidc');
      if (!dt.ok) { box.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(dt.d.error || `HTTP ${dt.status}`)}</div></div>`; return; }
      const o = dt.d.oidc || null;
      const row = (k, label) => (o && o[k] ? `<dt>${esc(label)}</dt><dd><code>${esc(o[k])}</code></dd>` : '');
      box.innerHTML = o ? `
        <dl class="reg-facts capi-access-facts">
          ${row('issuer_url', tr('capi.oidc.f.issuer'))}${row('audience', tr('capi.oidc.f.audience'))}
          ${row('username_prefix', tr('capi.oidc.f.usernamePrefix'))}${row('groups_prefix', tr('capi.oidc.f.groupsPrefix'))}
          ${row('admin_group', tr('capi.oidc.f.adminGroup'))}
        </dl>
        <p class="form-hint">${esc(tr('capi.acc.oidcKubeconfigNote'))}</p>
        <div class="apply-bar" style="margin:8px 0 0;">
          <span class="tip" data-x="dl-wrap"><button type="button" class="btn btn-primary btn-sm" data-x="dl">${Icons.svg('download')} ${esc(tr('capi.acc.oidcKubeconfig'))}</button></span>
          <span class="apply-result" data-x="dl-line"></span>
        </div>`
        : `<p class="form-hint" data-x="no-oidc">${esc(tr('capi.acc.noOidc'))}</p>`;
      const btn = box.querySelector('[data-x="dl"]');
      if (!btn) return;
      gate(box.querySelector('[data-x="dl-wrap"]'), btn, can('operator'),
        can('operator') ? tr('capi.acc.t.oidcKubeconfig') : tr('capi.acc.t.operator'));
      btn.addEventListener('click', async () => {
        if (btn.disabled) return;
        const line = box.querySelector('[data-x="dl-line"]');
        const r = await jsonCall('GET', `/api/capi/${enc(cluster)}/oidc-kubeconfig/${enc(namespace)}/${enc(name)}`);
        if (!r.ok || typeof r.d.kubeconfig !== 'string') { line.textContent = r.d.error || `HTTP ${r.status}`; return; }
        // le fichier ne porte aucun identifiant (greffon kubelogin) ; il
        // n'est jamais affiché, seulement remis
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([r.d.kubeconfig], { type: 'application/yaml' }));
        a.download = `${name}-oidc.kubeconfig`;
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
        line.textContent = tr('capi.acc.oidcDownloaded', { file: a.download });
      });
    }

    function paintMembers(session, row) {
      const box = pane('members');
      const ours = row && row.imported && session && row.rancher === session && row.present !== false;
      if (!ours || !window.RancherMembers) {
        box.innerHTML = `<p class="form-hint" data-x="no-members">${esc(!session ? tr('capi.acc.t.noSession') : tr('capi.acc.membersNeedImport'))}</p>`;
        return;
      }
      box.innerHTML = '<div data-x="members"></div>';
      RancherMembers.render(box.querySelector('[data-x="members"]'),
        { cluster, scope: 'cluster', child: ref, title: tr('capi.acc.membersTitle', { name: row.name }) });
    }

    load();
    return panel;
  }

  // --- Renouveler les machines (point 233) --------------------------------
  const RENEW_WHATS = ['registries', 'oidc', 'ca'];
  const RENEW_ERRORS = {
    'renew-busy': () => tr('capi.ren.err.busy'),
    'renew-nothing': () => tr('capi.ren.err.nothing'),
    'renew-plan-running': () => tr('capi.ren.err.planRunning'),
    'renew-cluster-missing': () => tr('capi.ren.err.clusterMissing'),
    'renew-class-missing': () => tr('capi.ren.err.classMissing'),
    'renew-template-missing': () => tr('capi.ren.err.templateMissing'),
    'child-admin-required': () => tr('capi.ren.err.childAdmin'),
    // revue 5-8 (I-1, I-9) : renouvellement pas fini, abandon, verrou du cluster
    'renew-not-running': () => tr('capi.ren.err.notRunning'),
    'renew-marker-unreadable': () => tr('capi.ren.err.markerUnreadable'),
    'renew-abandon-admin-only': () => tr('capi.ren.err.abandonAdmin'),
    'capi-uninstall-running': () => tr('capi.ren.err.uninstallRunning'),
  };
  const RENEW_FINDINGS = {
    'renew-pool-missing': (v) => tr('capi.ren.fd.renew-pool-missing', v),
    'renew-pool-short': (v) => tr('capi.ren.fd.renew-pool-short', v),
    'cpu-short': (v) => tr('capi.ren.fd.cpu-short', v),
    'memory-short': (v) => tr('capi.ren.fd.memory-short', Object.assign({}, v, {
      needed: window.XferProgress ? XferProgress.bytes(v.needed) : v.needed,
      free: window.XferProgress ? XferProgress.bytes(v.free) : v.free })),
  };
  const renewErrorText = (d) => {
    const code = d.code || (/^([a-z][a-z0-9-]+):/.exec(d.error || '') || [])[1];
    return RENEW_ERRORS[code] ? RENEW_ERRORS[code]() : (d.error || '?');
  };

  async function openRenew(namespace, name) {
    const cluster = mgmtCluster();
    const ref = `${namespace}/${name}`;
    if (!window.FloatingPanels || !cluster || !window.CapiCreate) return;
    const id = `capi-renew-${namespace}-${name}`;
    if (document.getElementById(`fp-${id}`)) FloatingPanels.close(id);
    const [inv, dt] = await Promise.all([
      jsonCall('GET', `/api/capi/${enc(cluster)}/inventory`).catch(() => ({ ok: false, d: {} })),
      jsonCall('GET', `/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/details`).catch(() => ({ ok: false, d: {} })),
    ]);
    const cur = (dt.ok && dt.d.oidc) || null;
    const whatTip = { registries: tr('capi.ren.t.registries'), oidc: tr('capi.ren.t.oidc'), ca: tr('capi.ren.t.ca') };
    const whatLabel = { registries: tr('capi.ren.what.registries'), oidc: tr('capi.ren.what.oidc'), ca: tr('capi.ren.what.ca') };
    const panel = FloatingPanels.open({
      id, title: tr('capi.ren.title', { name: ref }), icon: 'refresh', width: Math.min(860, Math.max(300, window.innerWidth - 120)),
      height: Math.max(420, Math.min(680, window.innerHeight - 80)),
      bodyHtml: `<div class="capi-renew-win capi-create" style="padding:12px 14px;">
        ${tabsHtml([['params', tr('capi.ren.tab.params'), tr('capi.ren.t.tabParams'), 'edit'],
                    ['diff', tr('capi.ren.tab.diff'), tr('capi.ren.t.tabDiff'), 'preview'],
                    ['confirm', tr('capi.ren.tab.confirm'), tr('capi.ren.t.tabConfirm'), 'ok']], tr('capi.ren.title', { name: ref }))}
        <section data-win-pane="params" role="tabpanel" class="tf-form">
          <p class="tf-desc">${esc(tr('capi.ren.intro'))}</p>
          <fieldset class="tf-block"><legend>${esc(tr('capi.ren.what'))}</legend><div class="tf-args">
            ${RENEW_WHATS.map(w => `<label class="tf-field tf-type-bool tip" data-tip="${esc(whatTip[w])}"><input type="checkbox" data-w="${w}"><span class="tf-label">${esc(whatLabel[w])}</span></label>`).join('')}
          </div></fieldset>
          <fieldset class="tf-block" data-w-box="registries" hidden><legend>${esc(whatLabel.registries)}</legend>
            <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('capi.ren.t.reread'))}"><input type="radio" name="ren-regs" data-reg-mode="reread" checked><span class="tf-label">${esc(tr('capi.ren.reread'))}</span></label>
            <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('capi.ren.t.replace'))}"><input type="radio" name="ren-regs" data-reg-mode="replace"><span class="tf-label">${esc(tr('capi.ren.replace'))}</span></label>
            <div data-reg-editor hidden>${CapiCreate.registriesHtml(inv.ok ? inv.d : {}, false)}</div>
          </fieldset>
          <fieldset class="tf-block" data-w-box="oidc" hidden><legend>${esc(whatLabel.oidc)}</legend>
            <p class="tf-desc">${esc(cur ? tr('capi.ren.oidcCurrent') : tr('capi.ren.oidcNone'))}</p>
            ${CapiCreate.oidcHtml(!!cur)}
          </fieldset>
          <fieldset class="tf-block" data-w-box="ca" hidden><legend>${esc(whatLabel.ca)}</legend>
            <p class="tf-desc">${esc(tr('capi.ren.caIntro'))}</p></fieldset>
          <div class="tf-args"><label class="tf-field"><span class="tf-label">${esc(tr('capi.ren.timeout'))}</span>
            <input type="number" data-x="timeout" min="1" max="180" value="60" class="tip" data-tip="${esc(tr('capi.ren.t.timeout'))}"></label></div>
        </section>
        <section data-win-pane="diff" role="tabpanel" hidden><div class="xfer-report" data-x="diff"><p class="form-hint">${esc(tr('capi.ren.pickWhat'))}</p></div></section>
        <section data-win-pane="confirm" role="tabpanel" hidden>
          <ul class="reg-summary" data-x="summary"></ul>
          <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('capi.ren.t.understand'))}"><input type="checkbox" data-x="understand"><span class="tf-label">${esc(tr('capi.ren.understand'))}</span></label>
        </section>
        <div class="bm-refusal bm-warning capi-ren-unfinished" data-x="unfinished" hidden>
          <p>${Icons.svg('warn', { size: 13 })} ${esc(tr('capi.ren.unfinished'))}</p>
          <div class="apply-bar">
            <span class="tip" data-x="finish-wrap"><button type="button" class="btn btn-secondary btn-sm" data-x="finish">${Icons.svg('play')} ${esc(tr('capi.ren.finish'))}</button></span>
            <span class="tip" data-x="abandon-wrap"><button type="button" class="btn btn-danger btn-sm" data-x="abandon">${Icons.svg('close')} ${esc(tr('capi.ren.abandon'))}</button></span>
          </div>
        </div>
        <div class="apply-bar" style="margin-top:10px;">
          <span class="tip" data-x="go-wrap"><button type="button" class="btn btn-primary btn-sm" data-x="go" disabled>${Icons.svg('refresh')} ${esc(tr('capi.ren.go'))}</button></span>
          <span class="apply-result" data-x="line" hidden></span>
        </div>
      </div>`,
    });
    const root = panel.el.querySelector('.capi-renew-win');
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    root.querySelectorAll('[data-win-tab]').forEach(b => b.addEventListener('click', () => showWinTab(root, b.dataset.winTab)));
    const st = { seq: 0, timer: null, diff: null, sig: '', launched: false };
    const regs = CapiCreate.wireRegistries(root.querySelector('[data-reg-editor]'), inv.ok ? inv.d : {}, false, () => later());
    if (cur) CapiCreate.fillOidc(root, cur);
    const whats = () => RENEW_WHATS.filter(w => root.querySelector(`[data-w="${w}"]`).checked);
    function body() {
      const ws = whats();
      const b = { what: ws, timeout: Math.max(1, Math.min(180, Number(q('timeout').value) || 60)) * 60 };
      if (ws.includes('registries') && root.querySelector('[data-reg-mode="replace"]').checked) {
        b.registries = CapiCreate.readRegistries(root.querySelector('[data-reg-editor]'));
      }
      if (ws.includes('oidc')) b.oidc = CapiCreate.readOidc(root);
      return b;
    }
    function paintGo() {
      const d = st.diff;
      const fresh = d && st.sig === JSON.stringify(body());
      const changed = fresh && d.ok && !d.blocked && ((d.changes || []).length || d.secrets_changed || d.access_changed);
      const why = !can('operator') ? tr('capi.acc.t.operator')
        : st.launched ? tr('capi.ren.t.launched')
          : !whats().length ? tr('capi.ren.pickWhat')
            : !fresh ? tr('capi.ren.t.checking')
              : !d.ok ? tr('capi.ren.t.blocked', { reason: renewErrorText(d) })
                : d.blocked ? tr('capi.ren.t.blocked', { reason: d.reason || tr('capi.ren.err.nothing') })
                  : !changed ? tr('capi.ren.err.nothing')
                    : !q('understand').checked ? tr('capi.ren.t.needUnderstand') : tr('capi.ren.t.go');
      gate(q('go-wrap'), q('go'), can('operator') && !st.launched && !!changed && q('understand').checked, why);
      paintUnfinished();
    }
    // revue 5-8 (I-1, m-2) : un renouvellement pas fini (marque du Cluster)
    // se reprend (opérateur) ou s'abandonne sans rien supprimer (admin)
    function paintUnfinished() {
      const d = st.diff;
      const code = d && !d.ok ? (d.code || (/^([a-z][a-z0-9-]+):/.exec(d.error || '') || [])[1]) : '';
      const busy = code === 'renew-busy' || code === 'renew-marker-unreadable';
      q('unfinished').hidden = !busy && !st.unfinishedLaunched;
      const fin = can('operator') && !st.launched && code === 'renew-busy';
      gate(q('finish-wrap'), q('finish'), fin, !can('operator') ? tr('capi.acc.t.operator')
        : st.launched ? tr('capi.ren.t.launched') : tr('capi.ren.t.finish'));
      const ab = can('admin') && !st.launched && busy;
      gate(q('abandon-wrap'), q('abandon'), ab, !can('admin') ? tr('capi.ren.t.abandonAdmin')
        : st.launched ? tr('capi.ren.t.launched') : tr('capi.ren.t.abandon'));
    }
    async function launchMode(mode) {
      if (!confirm(mode === 'abandon' ? tr('capi.ren.confirmAbandon', { name: ref }) : tr('capi.ren.confirmFinish', { name: ref }))) return;
      st.launched = true;
      st.unfinishedLaunched = true;
      paintGo();
      const t = Math.max(1, Math.min(180, Number(q('timeout').value) || 60)) * 60;
      const r = await jsonCall('POST', `/api/capi/${enc(cluster)}/renew/${enc(namespace)}/${enc(name)}`,
        mode === 'abandon' ? { mode } : { mode, timeout: t });
      const line = q('line');
      if (!r.ok) {
        st.launched = false;
        line.hidden = false;
        line.textContent = renewErrorText(r.d);
        paintGo();
        return;
      }
      followAction(r.d.action_id, line, () => setTimeout(refreshK8sClustersPanel, 800));
    }
    function paintDiff() {
      const d = st.diff;
      const box = q('diff');
      if (!d) { box.innerHTML = `<p class="form-hint">${esc(whats().length ? tr('capi.ren.checking') : tr('capi.ren.pickWhat'))}</p>`; return; }
      if (!d.ok) {
        box.innerHTML = `<div class="sto-finding sev-critical" data-code="error"><div class="sto-finding-title">${esc(renewErrorText(d))}</div></div>`;
        return;
      }
      const ch = d.changes || [];
      const fs = (d.findings || []).map(f => `<div class="sto-finding ${f.level === 'block' ? 'sev-critical' : 'sev-watch'}" data-code="${esc(f.code)}">
          <div class="sto-finding-title">${esc(RENEW_FINDINGS[f.code] ? RENEW_FINDINGS[f.code](Object.assign({}, f.facts || {})) : f.code)}</div></div>`).join('');
      box.innerHTML = (!ch.length && !d.secrets_changed && !d.access_changed)
        ? `<div class="sto-finding sev-watch" data-code="renew-nothing"><div class="sto-finding-title">${esc(tr('capi.ren.err.nothing'))}</div></div>`
        : `<p>${esc(tr('capi.ren.revision', { n: d.revision }))}</p>
          ${ch.map(c => `<div class="capi-ren-change"><strong><code>${esc(c.template)}</code></strong> <span class="badge dim">${esc(c.kind || '')}</span>
            <ul class="bm-paths">${(c.paths || []).slice(0, 12).map(x => `<li><code>${esc(x)}</code></li>`).join('')}
            ${(c.paths || []).length > 12 ? `<li class="form-hint">${esc(tr('capi.ren.more', { n: c.paths.length - 12 }))}</li>` : ''}</ul></div>`).join('')}
          ${d.secrets_changed ? `<p class="form-hint">${Icons.svg('info', { size: 12 })} ${esc(tr('capi.ren.secretsChanged'))}</p>` : ''}
          ${d.access_changed ? `<p class="form-hint" data-access-changed>${Icons.svg('info', { size: 12 })} ${esc(tr('capi.ren.accessChanged'))}</p>` : ''}
          ${fs}`;
    }
    function paintSummary() {
      const d = st.diff;
      const ws = whats();
      q('summary').innerHTML = `
        <li>${esc(tr('capi.ren.sum.what', { what: ws.map(w => whatLabel[w]).join(', ') || '-' }))}</li>
        ${d && d.ok && d.revision ? `<li>${esc(tr('capi.ren.sum.revision', { n: d.revision, count: (d.changes || []).length }))}</li>` : ''}
        <li>${esc(tr('capi.ren.sum.rolling'))}</li>
        <li>${esc(tr('capi.ren.sum.cut'))}</li>
        <li>${esc(tr('capi.ren.sum.address'))}</li>
        <li>${esc(tr('capi.ren.sum.rollback'))}</li>`;
    }
    async function check() {
      const n = ++st.seq;
      const b = body();
      st.diff = null;
      paintDiff();
      paintSummary();
      paintGo();
      if (!b.what.length) return;
      const local = b.what.includes('oidc') ? CapiCreate.oidcLocalErrors(root) : [];
      if (local.length) {
        st.sig = JSON.stringify(b);
        st.diff = { ok: false, d: {}, code: local[0].code, error: CapiCreate._findingText(local[0]) };
        q('diff').innerHTML = `<div class="sto-finding sev-critical" data-code="${esc(local[0].code)}"><div class="sto-finding-title">${esc(CapiCreate._findingText(local[0]))}</div></div>`;
        paintGo();
        return;
      }
      const r = await jsonCall('POST', `/api/capi/${enc(cluster)}/renew/${enc(namespace)}/${enc(name)}`, Object.assign({ dry_run: true }, b))
        .catch(e => ({ ok: false, status: 0, d: { error: e.message } }));
      if (n !== st.seq || !root.isConnected) return;
      st.sig = JSON.stringify(b);
      st.diff = r.ok ? Object.assign({ ok: true }, r.d) : Object.assign({ ok: false }, r.d);
      paintDiff();
      paintSummary();
      paintGo();
    }
    const later = () => {
      clearTimeout(st.timer);
      st.seq++;
      st.diff = null;
      paintGo();
      st.timer = setTimeout(check, 500);
    };
    root.querySelectorAll('[data-w]').forEach(c => c.addEventListener('change', () => {
      root.querySelector(`[data-w-box="${c.dataset.w}"]`).hidden = !c.checked;
      later();
    }));
    root.querySelectorAll('[data-reg-mode]').forEach(r => r.addEventListener('change', () => {
      root.querySelector('[data-reg-editor]').hidden = !root.querySelector('[data-reg-mode="replace"]').checked;
      later();
    }));
    const oon = root.querySelector('[data-w-box="oidc"] [data-o-on]');
    if (oon) oon.addEventListener('change', () => { root.querySelector('[data-w-box="oidc"] [data-o-box]').hidden = !oon.checked; later(); });
    root.querySelectorAll('[data-o]').forEach(el => {
      el.addEventListener('input', () => CapiCreate.oidcLocalErrors(root));
      el.addEventListener('change', later);
    });
    q('timeout').addEventListener('change', later);
    q('understand').addEventListener('change', paintGo);
    q('finish').addEventListener('click', () => { if (!q('finish').disabled) launchMode('finish'); });
    q('abandon').addEventListener('click', () => { if (!q('abandon').disabled) launchMode('abandon'); });
    q('go').addEventListener('click', async () => {
      if (q('go').disabled) return;
      const b = body();
      if (!confirm(tr('capi.ren.confirm', { name: ref }))) return;
      st.launched = true;
      paintGo();
      const r = await jsonCall('POST', `/api/capi/${enc(cluster)}/renew/${enc(namespace)}/${enc(name)}`, b);
      const line = q('line');
      if (!r.ok) {
        st.launched = false;
        line.hidden = false;
        line.textContent = renewErrorText(r.d);
        paintGo();
        return;
      }
      followAction(r.d.action_id, line, () => setTimeout(refreshK8sClustersPanel, 800));
    });
    regs.set([]);
    paintSummary();
    paintGo();
    return panel;
  }

  /**
   * Rejoue l'activation du sous-onglet courant (v1.20.0).
   *
   * Appelé au changement de cluster : l'onglet Automation ne se
   * reconstruisait qu'au clic, si bien qu'en restant dessus on continuait
   * de lire le diagnostic CAPI du cluster précédent. Passer par les mêmes
   * sélecteurs que le clic évite d'avoir deux chemins de rendu à tenir
   * en cohérence.
   */
  async function reactivate() {
    let sub = 'capi';
    try { sub = localStorage.getItem('harvester_ops_automation_subtab') || 'capi'; } catch {}
    // 2.2.0 : l'onglet de Cluster API vient de l'adresse (setTab), plus
    // d'un choix mémorisé
    selectAutomationSubtab(sub);
  }

  // Wire sub-tabs at DOMContentLoaded
  document.addEventListener('DOMContentLoaded', initSubtabs);

  return { init, refresh, reactivate, setTab, tab: () => tab, refreshK8s: refreshK8sClustersPanel, openCreate, openUninstall,
    openAccess, openRenew };
})();

document.addEventListener('DOMContentLoaded', CAPI.init);
window.CAPI = CAPI;
