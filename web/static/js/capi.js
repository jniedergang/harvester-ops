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
    const cluster = window.App && document.querySelector('#cluster-select')?.value;
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
        else render(out, d);
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

  function render(out, d) {
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
        <button class="btn btn-danger btn-sm tip" id="btn-capi-uninstall"
                data-tip="${esc(tr('capi.tip.uninstall'))}">
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

    // Managed CAPI clusters table moved to its own sub-tab (🖥 Clusters K8S).
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
        const cluster = document.querySelector('#cluster-select')?.value;
        const dry = document.querySelector('#capi-install-dry')?.checked ?? true;
        if (cluster && confirm(dry ? tr('capi.v.legacy.confirmInstallDry', { cluster })
                                   : tr('capi.v.legacy.confirmInstall', { cluster }))) {
          install(cluster, dry);
        }
      }
      if (e.target.closest('#btn-capi-uninstall')) {
        const cluster = document.querySelector('#cluster-select')?.value;
        if (!cluster) return;
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

  function selectAutomationSubtab(name) {
    // 1. Mark the matching sidebar child as active. Automation children
    //    carry only data-subtab (their data-tab is inherited from the head).
    $$('#tab-group-automation .tab-child').forEach(el =>
      el.classList.toggle('sub-active', el.dataset.subtab === name));
    // 2. Show only that content panel
    $$('#tab-automation > .sub-tab-content').forEach(x =>
      x.classList.toggle('active', x.dataset.subtab === name));
    // 3. The inline 📦/🛠 strip is only meaningful for capi
    const inline = document.querySelector('#tab-automation .sub-tabs-inline');
    if (inline) inline.classList.toggle('hidden', name !== 'capi');
    try { localStorage.setItem('harvester_ops_automation_subtab', name); } catch {}
    if (name === 'capi') refresh();
    else if (name === 'terraform' && window.TF) window.TF.refresh();
    else if (name === 'pxe' && window.BMC) window.BMC.render();
  }

  function selectCapiTab(name) {
    $$('#tab-automation .sub-tabs-inline .sub-tab').forEach(x =>
      x.classList.toggle('active', x.dataset.capiTab === name));
    $$('[data-subtab="capi"] .capi-tab-content').forEach(x =>
      x.classList.toggle('active', x.dataset.capiTab === name));
    try { localStorage.setItem('harvester_ops_capi_subtab', name); } catch {}
    if (name === 'clusters') refreshClustersPanel();
    else if (name === 'k8s') refreshK8sClustersPanel();
  }

  function initSubtabs() {
    // Sidebar children click → ensure parent is expanded + activate sub-tab.
    // The main-tab routing (to #tab-automation) is handled by app.js.
    document.addEventListener('click', (e) => {
      const child = e.target.closest('#tab-group-automation .tab-child');
      if (child) {
        document.querySelector('#tab-group-automation')?.classList.add('expanded');
        selectAutomationSubtab(child.dataset.subtab);
      }
      // Inline Cluster API sub-tabs
      const inlineBtn = e.target.closest('#tab-automation .sub-tabs-inline .sub-tab');
      if (inlineBtn) { e.preventDefault(); selectCapiTab(inlineBtn.dataset.capiTab); }
    });

    // Restore last selection
    try {
      const saved = localStorage.getItem('harvester_ops_automation_subtab');
      if (saved && ['capi', 'terraform', 'pxe'].includes(saved)) {
        selectAutomationSubtab(saved);
        document.querySelector('#tab-group-automation')?.classList.add('expanded');
      } else {
        selectAutomationSubtab('capi');
      }
      const savedCapi = localStorage.getItem('harvester_ops_capi_subtab');
      if (savedCapi && ['install', 'clusters', 'k8s'].includes(savedCapi)) {
        selectCapiTab(savedCapi);
      }
    } catch {
      selectAutomationSubtab('capi');
    }

    // When the user opens the Automation group (head click), refresh the
    // CAPI diag. Generic group toggle in app.js handles expand/collapse;
    // we don't touch the `expanded` class here.
    document.addEventListener('click', (e) => {
      if (e.target.closest('.tab-group-head[data-group="automation"]')) {
        setTimeout(refresh, 80);
      }
    });
  }

  // ---------------------------------------------------------------------
  // Cluster creation panel (second sub-tab) — v1.48.0 : capi-create.js
  // ---------------------------------------------------------------------
  async function refreshClustersPanel() {
    const out = document.querySelector('#capi-clusters-body');
    if (!out) return;
    const cluster = document.querySelector('#cluster-select')?.value;
    if (!cluster) { out.innerHTML = `<p class="form-hint">${i18n.t('capi.new.pickCluster')}</p>`; return; }
    if (!window.CapiCreate) { out.innerHTML = `<p class="form-hint">${i18n.t('migrate.reload')}</p>`; return; }
    return CapiCreate.render(out, cluster);
  }

  async function refreshK8sClustersPanel() {
    const out = document.querySelector('#capi-k8s-body');
    if (!out) return;
    const cluster = document.querySelector('#cluster-select')?.value;
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
      out.innerHTML = `<p class="empty-state">${esc(tr('capi.v.k8s.empty', { cluster, tab: tr('capi.tab.clusters') }))}</p>`;
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
    const cluster = document.querySelector('#cluster-select')?.value;
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
    const cluster = document.querySelector('#cluster-select')?.value;
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
    const cluster = document.querySelector('#cluster-select')?.value;
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
    const cluster = document.querySelector('#cluster-select')?.value;
    window.open(`/api/capi/${enc(cluster)}/cluster/${enc(namespace)}/${enc(name)}/kubeconfig`, '_blank');
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
    selectAutomationSubtab(sub);
    if (sub === 'capi') {
      let capiTab = null;
      try { capiTab = localStorage.getItem('harvester_ops_capi_subtab'); } catch {}
      // selectAutomationSubtab a déjà rendu le diagnostic ; les vues
      // « Création » et « Clusters K8S » ont leur propre chargement.
      if (capiTab && capiTab !== 'diag') selectCapiTab(capiTab);
    }
  }

  // Wire sub-tabs at DOMContentLoaded
  document.addEventListener('DOMContentLoaded', initSubtabs);

  return { init, refresh, reactivate };
})();

document.addEventListener('DOMContentLoaded', CAPI.init);
window.CAPI = CAPI;
