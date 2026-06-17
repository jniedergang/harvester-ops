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

  async function refresh() {
    if (!window.App) return;
    const cluster = window.App && document.querySelector('#cluster-select')?.value;
    if (!cluster) return;
    const out = $('#capi-status-body');
    if (!out) return;
    out.innerHTML = '<p class="form-hint">Loading…</p>';
    try {
      const d = await fetch(`/api/capi/${encodeURIComponent(cluster)}/diag`).then(r => r.json());
      render(out, d);
    } catch (e) {
      out.innerHTML = `<div class="summary-bar bad">${e.message}</div>`;
    }
  }

  function render(out, d) {
    const allInstalled = (d.components || []).every(c => c.installed);
    const summary = allInstalled
      ? '<div class="summary-bar ok">✓ CAPI/CAPHV stack fully installed</div>'
      : '<div class="summary-bar warn">⚠ Some components are missing — use Install stack to deploy them</div>';

    // Target Harvester version + bundle compatibility chip
    const cmp = d.bundle_compatibility;
    let compatChip = '';
    if (d.harvester_version) {
      compatChip += `<span class="phase Running" title="Detected Harvester version on the target cluster">Harvester ${d.harvester_version}</span> `;
    }
    if (cmp) {
      if (cmp.compatible) {
        compatChip += `<span class="badge ok" title="Active bundle supports the target Harvester version (${(cmp.supported_versions||[]).join(', ')})">✓ bundle compatible</span>`;
      } else {
        const sup = (cmp.supported_versions || []).join(', ') || 'unknown';
        compatChip += `<span class="badge warn" title="Active bundle supports ${sup} — target reports ${cmp.target_version || 'unknown'}">⚠ version mismatch (supported: ${sup})</span>`;
      }
    }

    const componentRows = (d.components || []).map(c => `
      <tr>
        <td>${c.installed ? '<span class="badge ok">✓</span>' : '<span class="badge fail">✗</span>'}</td>
        <td><strong>${c.label}</strong></td>
        <td>${c.version ? `<code title="${c.image || ''}">${c.version}</code>` : '<span class="form-hint">—</span>'}</td>
        <td><code>${c.details || '—'}</code></td>
      </tr>`).join('');

    const bundleSection = `
      <div class="apply-bar" style="margin-top: 16px; padding-top: 12px; flex-wrap: wrap; gap: 8px;">
        <button class="btn btn-secondary btn-sm" id="btn-capi-bundle-build"
                title="Pull every CAPI/CAPHV image and build a new timestamped airgap tar.gz (heavy, several minutes). The new bundle becomes active automatically.">
          🔨 Build new bundle
        </button>
        <button class="btn btn-secondary btn-sm" id="btn-capi-bundle-upload"
                title="Upload a pre-built bundle (.tar.gz) — useful when the build host has internet but the cluster doesn't">
          ⬆ Upload bundle…
        </button>
        <input type="file" id="capi-bundle-upload-input" accept=".tar.gz,.tgz" style="display:none">
        <span class="apply-result" id="capi-bundle-upload-result" style="margin-left: 8px;"></span>
        <button class="btn btn-primary btn-sm" id="btn-capi-install" ${d.bundle_available ? '' : 'disabled'}
                title="${d.bundle_available
                  ? 'Install the CAPI/CAPHV stack from the active airgap bundle (ssh + ctr import + kubectl apply)'
                  : 'No bundle available — build one first'}">
          📦 Install stack from active bundle
        </button>
        <button class="btn btn-danger btn-sm" id="btn-capi-uninstall"
                title="Remove every CAPI/CAPHV namespace, CRDs and container images from the Harvester nodes (cert-manager kept by default)">
          🗑 Uninstall stack
        </button>
        <label class="apply-dry" title="Validate the install plan without writing anything">
          <input type="checkbox" id="capi-install-dry" checked> Dry-run (preview only)
        </label>
        <span class="form-hint" style="flex-basis: 100%; margin-top: 6px;">
          ${d.bundle_available
            ? `<span style="color:var(--accent)">✓ Active bundle: <code>${d.active_bundle || 'capi-bundle.tar.gz'}</code></span>`
            : '<span style="color:var(--warn)">✗ No bundle — click <strong>Build new bundle</strong> first</span>'}
        </span>
        <span class="apply-result" id="capi-install-result" style="flex-basis: 100%;"></span>
      </div>
      <p class="form-hint" style="margin-top:6px;">
        Both actions stream progress to the bottom dock (look for <code>capi-bundle-build</code>
        and <code>capi-install</code>). Bundle build pulls ~1-2 GB of images. Install pushes them
        via SSH to every Harvester node, then applies manifests in order: cert-manager →
        cluster-api → cabp-rke2 → cacp-rke2 → caphv → ClusterClass.
      </p>
      <div id="capi-bundles-panel" style="margin-top: 18px;">
        <div class="loading-placeholder">Loading bundles…</div>
      </div>`;

    // Managed CAPI clusters table moved to its own sub-tab (🖥 Clusters K8S).
    // The Installation tab is just: stack components + bundles management.
    out.innerHTML = `
      ${summary}
      ${compatChip ? `<div style="margin-top:8px;">${compatChip}</div>` : ''}
      <h4 style="margin-top:14px;">Stack components</h4>
      <table class="data-table">
        <thead><tr><th></th><th>Component</th><th>Version</th><th>Details</th></tr></thead>
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
      panel.innerHTML = `<div class="summary-bar bad">Bundles list failed: ${e.message}</div>`;
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
      <div class="disk-usage" title="Disk hosting ${data.bundle_dir || 'dist/'}">
        <div class="disk-usage-line">
          <strong>${bundles.length}</strong> bundle${bundles.length>1?'s':''} ·
          <strong>${fmtSize(totalUsed)}</strong> used by bundles
          <span style="color:var(--text-dim)">(${bundlesPct}% of disk)</span>
        </div>
        <div class="disk-bar">
          <div class="fill fill-bundles" style="width:${bundlesPct}%" title="${fmtSize(totalUsed)} used by bundles"></div>
          <div class="fill fill-other"   style="width:${Math.max(0, diskUsedPct - bundlesPct)}%" title="${fmtSize(otherUsed)} used by other data"></div>
        </div>
        <div class="disk-usage-line" style="color:var(--text-dim);">
          <strong>${fmtSize(diskUsed)}</strong> / ${fmtSize(diskTotal)} disk used (${diskUsedPct}%)
          · <strong>${fmtSize(diskFree)}</strong> free
        </div>
      </div>`;

    if (bundles.length === 0) {
      panel.innerHTML = `<h4>Airgap bundles</h4>${diskBar}
        <div class="empty-state" style="padding:18px;">
          No bundle present in <code>${data.bundle_dir}</code>. Click
          <strong>Build new bundle</strong> above to create one.
        </div>`;
      return;
    }

    const rows = bundles.map(b => `
      <tr data-filename="${b.filename}">
        <td>
          <a href="#" class="bundle-inspect" data-filename="${b.filename}"
             title="View bundle contents and embedded component versions">
            <code>${b.filename}</code>
          </a>
        </td>
        <td>${fmtBundleDate(b.filename)}</td>
        <td>${fmtSize(b.size)}</td>
        <td><code class="sha">${(b.sha256 || '').slice(0, 12) || '—'}</code></td>
        <td>${b.is_active
              ? '<span class="badge ok" title="The active bundle is used by Install stack and bundle.sh">✓ active</span>'
              : `<button class="btn btn-secondary btn-sm bundle-select" data-filename="${b.filename}"
                         title="Make this bundle the active one (Install stack will use it)">
                   Activate
                 </button>`}
        </td>
        <td>
          <button class="btn btn-secondary btn-sm bundle-inspect"
                  data-filename="${b.filename}"
                  title="View bundle contents and embedded component versions">
            Inspect
          </button>
          <a class="btn btn-secondary btn-sm bundle-download"
             href="/api/capi/bundle/${encodeURIComponent(b.filename)}/download"
             title="Download this bundle (.tar.gz) — useful for airgap transfer"
             download>
            ⬇ Download
          </a>
          <button class="btn btn-secondary btn-sm bundle-delete"
                  data-filename="${b.filename}"
                  title="${b.is_active
                    ? 'Cannot delete the active bundle — activate another first'
                    : 'Permanently delete this bundle from disk'}"
                  ${b.is_active ? 'disabled' : ''}>
            Delete
          </button>
        </td>
      </tr>`).join('');

    panel.innerHTML = `
      <h4 style="margin-top:0;">Airgap bundles</h4>
      ${diskBar}
      <table class="data-table" style="margin-top:8px;">
        <thead><tr>
          <th>Filename</th>
          <th>Built</th>
          <th>Size</th>
          <th>SHA-256</th>
          <th>Active</th>
          <th>Actions</th>
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
      if (!r.ok) throw new Error(d.error || 'failed');
      // Only repaint the bundle list + the small "Active bundle: …" hint —
      // no full diag refresh, which made the whole CAPI panel re-render.
      await renderBundles();
      const hint = document.querySelector('#capi-status-body .apply-bar .form-hint');
      if (hint) {
        hint.innerHTML = `<span style="color:var(--accent)">✓ Active bundle: <code>${filename}</code></span>`;
      }
    } catch (e) {
      alert(`Failed to activate bundle: ${e.message}`);
    }
  }

  async function deleteBundle(filename) {
    if (!confirm(`Delete bundle "${filename}" permanently?\nThis frees disk but cannot be undone.`)) return;
    try {
      const r = await fetch(`/api/capi/bundle/${encodeURIComponent(filename)}`,
                            { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'failed');
      await renderBundles();
    } catch (e) {
      alert(`Failed to delete bundle: ${e.message}`);
    }
  }

  async function inspectBundle(filename) {
    let data;
    try {
      data = await fetch(`/api/capi/bundle/${encodeURIComponent(filename)}/inspect`)
                   .then(r => r.json());
      if (data.error) throw new Error(data.error);
    } catch (e) {
      alert(`Inspect failed: ${e.message}`);
      return;
    }
    const m = data.manifest || {};
    const components = (m.components || []);
    const images = (m.images || []);
    const files = (data.files || []);
    const createdAt = m.bundle?.created_at_iso || '(unknown — legacy bundle)';
    const host = m.bundle?.host || '—';
    const compatList = m.bundle?.compatible_harvester_versions || [];
    const notes = m.bundle?.notes || '';
    const compatBlock = compatList.length
      ? `<div class="summary-bar" style="margin-bottom:10px;">
           <strong>Compatible Harvester:</strong> ${compatList.map(v => `<code>${v}</code>`).join(', ')}
           ${notes ? `<details style="margin-top:6px;"><summary>Notes</summary><pre style="white-space:pre-wrap;margin:6px 0 0;">${notes.replace(/&/g,'&amp;').replace(/</g,'&lt;')}</pre></details>` : ''}
         </div>`
      : `<p class="form-hint">Bundle declares no Harvester compatibility list — install will not pre-check the target version.</p>`;
    const compTable = components.length
      ? `<table class="data-table"><thead><tr>
           <th>Component</th><th>Version</th><th>Images</th><th>Manifests</th>
         </tr></thead><tbody>${components.map(c => `
           <tr>
             <td><strong>${c.name}</strong></td>
             <td><code>${c.version || '—'}</code></td>
             <td>${c.image_count}</td>
             <td>${c.manifest_count}</td>
           </tr>`).join('')}</tbody></table>`
      : `<p class="form-hint">No component-version metadata
         (this bundle was built before bundle metadata was added — image tags below still show what's inside).</p>`;
    const imgList = images.length
      ? `<table class="data-table"><thead><tr><th>Component</th><th>Image</th><th>File</th></tr></thead><tbody>${
         images.map(i => `
           <tr>
             <td>${i.component || '—'}</td>
             <td><code>${i.name || i}</code></td>
             <td><code style="color:var(--text-dim)">${i.file || '—'}</code></td>
           </tr>`).join('')}</tbody></table>`
      : '<p class="form-hint">No image inventory in manifest.json.</p>';
    const fileList = files.length
      ? `<pre class="dock-action-log" style="display:block;max-height:220px;">${
          files.slice(0, 200).map(f =>
            `${(f.size || '').toString().padStart(10)}  ${f.name}${f.is_dir ? '/' : ''}`
          ).join('\n')}${files.length > 200 ? `\n... ${files.length - 200} more entries` : ''}</pre>`
      : '<p class="form-hint">Tar listing empty.</p>';
    const body = `
      <div style="padding: 12px 14px;">
        <div class="summary-bar ok" style="margin-bottom:14px;">
          <code>${filename}</code> · ${fmtSize(data.size)} · ${data.file_count} entries · built ${createdAt}${host !== '—' ? ' on '+host : ''}
        </div>
        ${compatBlock}
        <h4 style="margin-top:0;">Components</h4>
        ${compTable}
        <h4>Container images</h4>
        ${imgList}
        <h4>Tar contents (truncated to 200)</h4>
        ${fileList}
      </div>`;
    openBundlePanel(filename, body);
  }

  function openBundlePanel(filename, html) {
    // Use FloatingPanels if available, otherwise fall back to a modal overlay.
    if (window.FloatingPanels?.open) {
      window.FloatingPanels.open({
        id: `bundle-inspect-${filename}`,
        title: `Bundle · ${filename}`,
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
            <h3 id="bundle-inspect-title">Bundle</h3>
            <button class="btn-icon-sm" id="bundle-inspect-close" aria-label="Close">×</button>
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
    overlay.querySelector('#bundle-inspect-title').textContent = 'Bundle · ' + filename;
    overlay.querySelector('#bundle-inspect-body').innerHTML = html;
    overlay.style.display = 'flex';
  }

  async function uninstall(cluster, dryRun, keepCertManager) {
    const result = document.querySelector('#capi-install-result');
    if (result) result.textContent = 'starting uninstall…';
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/uninstall`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun, keep_cert_manager: keepCertManager }),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'failed'}</span>`;
        return;
      }
      if (result) {
        const verb = dryRun ? 'dry-run uninstall' : 'uninstall';
        const cm = keepCertManager ? '' : ' (incl. cert-manager)';
        result.innerHTML = `<span style="color:var(--accent)">✓ ${verb}${cm} started — see dock action <code>${d.action_id}</code></span>`;
      }
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function install(cluster, dryRun) {
    const result = document.querySelector('#capi-install-result');
    if (result) result.textContent = 'starting…';
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/install`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dry_run: dryRun }),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'failed'}</span>`;
        return;
      }
      if (result) {
        const verb = dryRun ? 'dry-run' : 'install';
        let line = `<span style="color:var(--accent)">✓ ${verb} started — see dock action <code>${d.action_id}</code></span>`;
        if (d.compatibility_warning) {
          const cw = d.compatibility_warning;
          const sup = (cw.supported_versions || []).join(', ') || '(unspecified)';
          line += `<br><span style="color:var(--warn)">⚠ Version mismatch noted: target Harvester ${cw.target_version || 'unknown'}, bundle supports ${sup}. The install will proceed — the warning is recorded in the action log.</span>`;
        }
        result.innerHTML = line;
      }
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function uploadBundle(file) {
    const result = document.querySelector('#capi-bundle-upload-result');
    if (result) result.innerHTML = `<span style="color:var(--text-dim)">⏳ uploading ${file.name} (${fmtSize(file.size)})…</span>`;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch('/api/capi/bundle/upload', { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'upload failed'}</span>`;
        return;
      }
      if (result) result.innerHTML = `<span style="color:var(--accent)">✓ uploaded as <code>${d.uploaded}</code> — set as active</span>`;
      await renderBundles();
      await refresh();
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  function init() {
    $('#btn-capi-refresh')?.addEventListener('click', refresh);
    $('#btn-capi-clusters-refresh')?.addEventListener('click', refreshClustersPanel);
    $('#btn-capi-k8s-refresh')?.addEventListener('click', refreshK8sClustersPanel);
    // Wizard interactions, all delegated.
    document.addEventListener('click', (e) => {
      if (e.target.closest('#capi-create-preview')) { e.preventDefault(); previewCluster(); }
      const det = e.target.closest('.capi-cluster-details');
      if (det) { e.preventDefault(); showClusterDetails(det.dataset.ns, det.dataset.name); }
      const sca = e.target.closest('.capi-cluster-scale');
      if (sca) { e.preventDefault(); scaleCluster(sca.dataset.ns, sca.dataset.name); }
      const del = e.target.closest('.capi-cluster-delete');
      if (del) { e.preventDefault(); deleteCluster(del.dataset.ns, del.dataset.name); }
      const kc  = e.target.closest('.capi-cluster-kubeconfig');
      if (kc)  { e.preventDefault(); downloadKubeconfig(kc.dataset.ns, kc.dataset.name); }
    });
    document.addEventListener('submit', (e) => {
      if (e.target?.id === 'capi-create-form') createCluster(e);
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
        if (cluster && confirm(`Launch CAPI install on "${cluster}"${dry ? ' (dry-run)' : ' (REAL)'}?`)) {
          install(cluster, dry);
        }
      }
      if (e.target.closest('#btn-capi-uninstall')) {
        const cluster = document.querySelector('#cluster-select')?.value;
        if (!cluster) return;
        const dry = document.querySelector('#capi-install-dry')?.checked ?? true;
        const keepCM = confirm(
          `Uninstall CAPI/CAPHV stack from "${cluster}"${dry ? ' (DRY-RUN)' : ' (REAL — destructive)'}?\n\n` +
          `OK = keep cert-manager (other workloads may need it)\n` +
          `Cancel = also remove cert-manager`);
        if (!confirm(`Confirm: ${dry ? 'dry-run' : 'REAL'} uninstall on "${cluster}"?`)) return;
        uninstall(cluster, dry, keepCM);
      }
      if (e.target.closest('#btn-capi-bundle-build')) {
        if (confirm('Build a new airgap bundle?\nThis pulls ~1-2 GB of images (several minutes), produces a timestamped tarball, and makes it the active bundle. Progress shows in the bottom dock.')) {
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
    if (result) result.textContent = 'starting bundle build…';
    let runId = null;
    try {
      const r = await fetch('/api/capi/bundle/build', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'failed'}</span>`;
        return;
      }
      runId = d.action_id;
      if (result) {
        result.innerHTML = `<span style="color:var(--accent)">⏳ bundle build running — see dock action <code>${runId}</code></span>`;
      }
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
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
            span.innerHTML = `<span style="color:var(--accent)">✓ bundle build completed (action <code>${runId}</code>) — refresh diagnostic to see "Bundle present"</span>`;
            setTimeout(refresh, 800);
          } else {
            span.innerHTML = `<span style="color:var(--danger)">✗ bundle build failed (exit ${d.exit_code ?? '?'}) — see dock action <code>${runId}</code> log for details</span>`;
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
  // Cluster creation panel (second sub-tab)
  // ---------------------------------------------------------------------
  let _harvesterInventory = null;

  async function loadHarvesterInventory(cluster) {
    if (_harvesterInventory) return _harvesterInventory;
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/inventory`);
      if (r.ok) {
        _harvesterInventory = await r.json();
        return _harvesterInventory;
      }
    } catch {}
    _harvesterInventory = { images: [], networks: [], ssh_keypairs: [], ip_pools: [], storage_classes: [] };
    return _harvesterInventory;
  }

  async function refreshClustersPanel() {
    // The 🛠 Création de clusters tab is now JUST the wizard form
    // (the list view moved to the dedicated 🖥 Clusters K8S tab).
    const out = document.querySelector('#capi-clusters-body');
    if (!out) return;
    out.innerHTML = `
      ${renderCreateForm()}
      <div id="capi-create-result" class="apply-result" style="margin-top:8px;"></div>`;
  }

  async function refreshK8sClustersPanel() {
    const out = document.querySelector('#capi-k8s-body');
    if (!out) return;
    const cluster = document.querySelector('#cluster-select')?.value;
    if (!cluster) { out.innerHTML = '<p class="form-hint">Select a Harvester cluster first.</p>'; return; }
    out.innerHTML = '<p class="form-hint">Loading…</p>';
    let diag;
    try { diag = await fetch(`/api/capi/${encodeURIComponent(cluster)}/diag`).then(r => r.json()); }
    catch (e) { out.innerHTML = `<div class="summary-bar bad">${e.message}</div>`; return; }

    const clusters = diag.capi_clusters || [];
    if (clusters.length === 0) {
      out.innerHTML = `<p class="empty-state">Aucun cluster CAPHV managé sur <code>${cluster}</code>. Va dans <strong>🛠 Création de clusters</strong> pour en créer un.</p>`;
      return;
    }
    const rows = clusters.map(c => `
      <tr>
        <td><code>${c.namespace}/${c.name}</code></td>
        <td><span class="phase ${c.phase === 'Provisioned' ? 'Running' : c.phase === 'Failed' ? 'Failed' : 'Pending'}">${c.phase}</span></td>
        <td>${c.ready ? '<span class="badge ok">✓</span>' : '<span class="badge warn">…</span>'}</td>
        <td><code>${c.clusterClass || '—'}</code></td>
        <td><code>${c.k8sVersion || '—'}</code></td>
        <td>
          <button class="btn btn-sm btn-secondary capi-cluster-details" data-ns="${c.namespace}" data-name="${c.name}" title="View detailed status, machines, conditions">Details</button>
          <button class="btn btn-sm btn-secondary capi-cluster-kubeconfig" data-ns="${c.namespace}" data-name="${c.name}" title="Download the cluster's kubeconfig">⬇ kubeconfig</button>
          <button class="btn btn-sm btn-secondary capi-cluster-scale" data-ns="${c.namespace}" data-name="${c.name}" title="Change worker replica count">↕ Scale</button>
          <button class="btn btn-sm btn-danger capi-cluster-delete" data-ns="${c.namespace}" data-name="${c.name}" title="Delete the cluster (CAPHV cleans up VMs)">🗑 Delete</button>
        </td>
      </tr>`).join('');
    out.innerHTML = `
      <table class="data-table">
        <thead><tr>
          <th>Name</th>
          <th>Phase</th>
          <th>Ready</th>
          <th>ClusterClass</th>
          <th>K8s</th>
          <th>Actions</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  function renderCreateForm() {
    // Sensible defaults — adjust to your network and cluster topology.
    return `
      <form id="capi-create-form" class="capi-form" autocomplete="off">
        <fieldset>
          <legend>Identité</legend>
          <label>Cluster name *
            <input type="text" name="name" required pattern="[a-z][-a-z0-9]*"
                   placeholder="my-cluster" title="lowercase + hyphens"></label>
          <label>Namespace
            <input type="text" name="namespace" placeholder="(défaut: même que le nom)"></label>
          <label>K8s version
            <input type="text" name="k8s_version" value="v1.31.14"></label>
          <label>ClusterClass
            <input type="text" name="class" value="harvester-rke2"></label>
        </fieldset>

        <fieldset>
          <legend>Sizing</legend>
          <label>Control-plane replicas
            <input type="number" name="cp_replicas" value="1" min="1" max="9"></label>
          <label>Worker replicas
            <input type="number" name="worker_replicas" value="1" min="0" max="50"></label>
          <label>CPU par VM
            <input type="number" name="cpu" value="2" min="1" max="64"></label>
          <label>Memory
            <input type="text" name="memory" value="4Gi" pattern="[0-9]+[KMGT]i" title="ex: 4Gi"></label>
          <label>Boot disk
            <input type="text" name="disk_size" value="40Gi" pattern="[0-9]+[KMGT]i" title="ex: 40Gi"></label>
          <label title="Format: size:storageClass (ex: 10Gi:longhorn)">Disque data (optionnel)
            <input type="text" name="extra_disk" placeholder="10Gi:harvester-longhorn"></label>
        </fieldset>

        <fieldset>
          <legend>Image &amp; SSH</legend>
          <label>Image VM *
            <input type="text" name="image" required
                   value="default/sles15-sp7-minimal-vm.x86_64-cloud-qu2.qcow2"
                   placeholder="namespace/displayName"></label>
          <label>SSH user
            <input type="text" name="ssh_user" value="sles"></label>
          <label>SSH KeyPair *
            <input type="text" name="ssh_keypair" required value="default/capi-ssh-key"></label>
        </fieldset>

        <fieldset>
          <legend>Réseau</legend>
          <label>VM Network *
            <input type="text" name="network" required value="default/untagged"></label>
          <label>Gateway *
            <input type="text" name="gateway" required value="10.0.0.1"></label>
          <label>Subnet mask *
            <input type="text" name="subnet_mask" required value="255.255.0.0"></label>
          <label>IPPool *
            <input type="text" name="ip_pool" required value="capi-vm-pool"></label>
          <label>DNS servers (csv)
            <input type="text" name="dns" value="1.1.1.1,8.8.8.8"></label>
          <label>CNI
            <select name="cni">
              <option value="calico" selected>calico</option>
              <option value="canal">canal</option>
              <option value="cilium">cilium</option>
              <option value="none">none (manual)</option>
            </select></label>
          <label>Pod CIDR
            <input type="text" name="pod_cidr" value="10.42.0.0/16"></label>
        </fieldset>

        <div class="apply-bar" style="margin-top:8px;">
          <label class="apply-dry" title="Génère le YAML sans l'appliquer">
            <input type="checkbox" name="dry_run" checked> Dry-run (preview only)</label>
          <button type="button" class="btn btn-secondary btn-sm" id="capi-create-preview"
                  title="Afficher le YAML qui sera appliqué (sans l'appliquer)">👁 Aperçu YAML</button>
          <button type="submit" class="btn btn-primary btn-sm"
                  title="Lancer la création (mode réel = applique vraiment)">🚀 Créer</button>
        </div>
      </form>`;
  }

  function readCreateForm() {
    const form = document.querySelector('#capi-create-form');
    if (!form) return null;
    const data = {};
    new FormData(form).forEach((v, k) => { if (v !== '') data[k] = v; });
    data.dry_run = !!form.querySelector('[name=dry_run]')?.checked;
    return data;
  }

  async function previewCluster() {
    const cluster = document.querySelector('#cluster-select')?.value;
    const result  = document.querySelector('#capi-create-result');
    const form    = readCreateForm();
    if (!cluster || !form) return;
    form.dry_run = true;
    if (result) result.innerHTML = '<span class="form-hint">Génération…</span>';
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/cluster-create`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'failed'}</span>`;
        return;
      }
      if (result) result.innerHTML = `<span style="color:var(--accent)">✓ Aperçu généré — action <code>${d.action_id}</code> (logs dans le dock)</span>`;
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function createCluster(ev) {
    ev.preventDefault();
    const cluster = document.querySelector('#cluster-select')?.value;
    const result  = document.querySelector('#capi-create-result');
    const form    = readCreateForm();
    if (!cluster || !form) return;
    if (!form.dry_run) {
      if (!confirm(`Créer le cluster "${form.name}" (${form.cp_replicas} CP + ${form.worker_replicas} W) sur "${cluster}" — RÉEL ?`)) return;
    }
    if (result) result.innerHTML = '<span class="form-hint">Lancement…</span>';
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/cluster-create`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      });
      const d = await r.json();
      if (!r.ok) {
        if (result) {
          const miss = (d.missing || []).join(', ');
          result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error}${miss ? ' — missing: ' + miss : ''}</span>`;
        }
        return;
      }
      if (result) {
        result.innerHTML = `<span style="color:var(--accent)">✓ ${form.dry_run ? 'dry-run' : 'création'} lancée — action <code>${d.action_id}</code> (suivez la progression dans le dock)</span>`;
      }
      setTimeout(refreshClustersPanel, 4000);
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function showClusterDetails(namespace, name) {
    const cluster = document.querySelector('#cluster-select')?.value;
    const panelId = `capi-cluster-${namespace}-${name}`;
    if (!window.FloatingPanels) { alert('FloatingPanels not loaded'); return; }
    const api = window.FloatingPanels.open({
      id: panelId, title: `🛠 ${namespace}/${name}`, width: 720, height: 540,
      bodyHtml: '<div style="padding:16px;">Loading…</div>',
    });
    try {
      const d = await fetch(`/api/capi/${encodeURIComponent(cluster)}/cluster/${namespace}/${name}/details`).then(r => r.json());
      if (d.error) throw new Error(d.error);
      const cond = (d.conditions || []).map(c => `
        <tr><td>${c.type}</td><td>${c.status}</td>
            <td>${c.reason || ''}</td>
            <td class="form-hint">${(c.message || '').slice(0, 120)}</td></tr>`).join('');
      const machRows = (d.machines || []).length === 0
        ? '<tr><td colspan="4" class="empty-state">No machines yet.</td></tr>'
        : d.machines.map(m => `
            <tr>
              <td><code>${m.name}</code></td>
              <td><span class="phase ${m.phase === 'Running' ? 'Running' : m.phase === 'Failed' ? 'Failed' : 'Pending'}">${m.phase}</span></td>
              <td>${m.nodeName || '—'}</td>
              <td><code>${m.k8sVersion || '—'}</code></td>
            </tr>`).join('');
      api.setBody(`
        <div style="padding:12px 14px;">
          <div class="summary-bar ${d.ready ? 'ok' : 'warn'}">
            <strong>${d.namespace}/${d.name}</strong> · phase: <strong>${d.phase}</strong> ·
            ready: ${d.ready ? '✓' : '…'} ·
            endpoint: <code>${(d.controlPlaneEndpoint||{}).host || '—'}:${(d.controlPlaneEndpoint||{}).port || '—'}</code>
          </div>
          <h4 style="margin-top:14px;">Conditions</h4>
          <table class="data-table"><thead><tr><th>Type</th><th>Status</th><th>Reason</th><th>Message</th></tr></thead><tbody>${cond}</tbody></table>
          <h4>Machines</h4>
          <table class="data-table"><thead><tr><th>Name</th><th>Phase</th><th>Node</th><th>K8s</th></tr></thead><tbody>${machRows}</tbody></table>
          <details style="margin-top:14px;">
            <summary>Topology spec</summary>
            <pre class="dock-action-log" style="display:block;max-height:200px;">${JSON.stringify(d.topology, null, 2).replace(/&/g,'&amp;').replace(/</g,'&lt;')}</pre>
          </details>
        </div>`);
    } catch (e) {
      api.setBody(`<div style="padding:16px;"><span style="color:var(--danger)">Failed: ${e.message}</span></div>`);
    }
  }

  async function scaleCluster(namespace, name) {
    const cluster = document.querySelector('#cluster-select')?.value;
    const n = prompt(`New worker replica count for ${namespace}/${name}?`, '2');
    if (n === null) return;
    const replicas = parseInt(n);
    if (!Number.isInteger(replicas) || replicas < 0) { alert('replicas must be a non-negative integer'); return; }
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/cluster/${namespace}/${name}/scale`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ replicas }),
      });
      const d = await r.json();
      if (!r.ok) { alert('Scale failed: ' + (d.error || 'unknown')); return; }
      setTimeout(refreshClustersPanel, 1500);
    } catch (e) { alert(e.message); }
  }

  async function deleteCluster(namespace, name) {
    const cluster = document.querySelector('#cluster-select')?.value;
    if (!confirm(`Delete cluster "${namespace}/${name}"? VMs will be deprovisioned by CAPHV. This is irreversible.`)) return;
    if (!confirm(`CONFIRM: actually delete ${namespace}/${name}?`)) return;
    try {
      const r = await fetch(`/api/capi/${encodeURIComponent(cluster)}/cluster/${namespace}/${name}`, { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok) { alert('Delete failed: ' + (d.error || 'unknown')); return; }
      setTimeout(refreshClustersPanel, 1500);
    } catch (e) { alert(e.message); }
  }

  async function downloadKubeconfig(namespace, name) {
    const cluster = document.querySelector('#cluster-select')?.value;
    window.open(`/api/capi/${encodeURIComponent(cluster)}/cluster/${namespace}/${name}/kubeconfig`, '_blank');
  }

  // Wire sub-tabs at DOMContentLoaded
  document.addEventListener('DOMContentLoaded', initSubtabs);

  return { init, refresh };
})();

document.addEventListener('DOMContentLoaded', CAPI.init);
window.CAPI = CAPI;
