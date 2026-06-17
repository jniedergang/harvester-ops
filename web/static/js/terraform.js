/**
 * harvester-ops — Terraform tab (v1.5.0)
 *
 * Resource creation is now organised as *Declarations* (named bundles of
 * mixed resources). The UI is a 2-level tree:
 *
 *   1) Declarations list  (top of the tab)
 *      Each row: name • #resources • last status • Open / Apply / Delete
 *
 *   2) Active declaration view  (under the list)
 *      Each resource is a card carrying one button per "section" of its
 *      schema (Specs, Disks, Networks, Cloud-init, …). Click → overlay
 *      with the section's form (FloatingPanel). Save → button colour
 *      tracks validation state (✓ green / ! red / · grey).
 *
 *      Apply on the whole declaration: posts /api/terraform/<c>/apply_declaration.
 *      Dry-run / Apply both stream into the same SSE overlay we already had.
 *
 * Backward compat: the legacy single-resource POST /apply endpoint stays
 * intact (used by the tests). The "raw HCL" flow is just a 1-section
 * resource inside a declaration.
 */
const TF = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  function lang() {
    try { return localStorage.getItem('harvester_ops_lang') || 'en'; }
    catch { return 'en'; }
  }
  function t(o) {
    if (!o) return '';
    if (typeof o === 'string') return o;
    return o[lang()] || o.en || '';
  }
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // -------------------------------------------------------------------------
  // Top-level refresh: re-fetch /info + /state, then re-render everything.
  //
  // v1.5.4: tagged with a monotonic `_refreshRev` so a slower refresh
  // can't clobber the DOM written by a newer one. The user can click
  // the Terraform sub-tab in rapid succession and end up with N
  // overlapping refreshes; whichever finishes last would otherwise
  // win and overwrite a partially-edited state from the freshest one.
  // -------------------------------------------------------------------------
  let _refreshRev = 0;

  async function refresh() {
    const out = $('#tf-status-body');
    if (!out) return;
    const myRev = ++_refreshRev;
    const cluster = $('#cluster-select')?.value || '';
    out.innerHTML = '<p class="form-hint">Loading…</p>';

    let info;
    try { info = await fetch('/api/terraform/info').then(r => r.json()); }
    catch (e) {
      if (myRev !== _refreshRev) return;
      out.innerHTML = `<div class="summary-bar bad">${e.message}</div>`;
      return;
    }
    let state = { initialized: false, resources: [], resources_detail: [],
                  resource_count: 0 };
    if (cluster) {
      try {
        state = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/state`)
                        .then(r => r.json());
      } catch {}
    }
    // A newer refresh started while we awaited — drop our output.
    if (myRev !== _refreshRev) return;

    out.innerHTML = renderShell(info, state, cluster);
    renderDeclarations(cluster);
    activateSubtab(getSavedSubtab());
  }

  const SUBTAB_KEY = 'harvester_ops_tf_subtab';
  function getSavedSubtab() {
    try { return localStorage.getItem(SUBTAB_KEY) || 'decls'; }
    catch { return 'decls'; }
  }
  function setSavedSubtab(name) {
    try { localStorage.setItem(SUBTAB_KEY, name); } catch {}
  }
  function activateSubtab(name) {
    if (!['live', 'decls', 'install'].includes(name)) name = 'decls';
    document.querySelectorAll('#tf-status-body .sub-tab[data-tf-tab]')
      .forEach(btn => btn.classList.toggle('active',
                                            btn.dataset.tfTab === name));
    document.querySelectorAll('#tf-status-body .tf-subtab-content[data-tf-tab]')
      .forEach(pane => pane.classList.toggle('active',
                                                pane.dataset.tfTab === name));
    setSavedSubtab(name);
  }

  function renderShell(info, state, cluster) {
    const tfOk = info.terraform_available;
    const provOk = !!info.provider_binary;
    const summary = (tfOk && provOk)
      ? `<div class="summary-bar ok">✓ Terraform <code>${info.terraform_bin}</code> ·
         provider <code>${info.provider_version}</code> ·
         ${state.initialized ? state.resource_count + ' resource(s) in state'
                             : 'workspace not initialized'}</div>`
      : `<div class="summary-bar warn">⚠ ${tfOk ? '' : 'terraform CLI missing — '}${
            provOk ? '' : 'provider binary missing'}</div>`;
    // v1.5.3: prefer `resources_detail` (carries has_sidecar + kind);
    // fall back to bare addresses for older API responses.
    const detail = Array.isArray(state.resources_detail)
      ? state.resources_detail
      : (state.resources || []).map(addr => ({ address: addr,
                                                local_name: (addr.split('.')[1] || ''),
                                                has_sidecar: false }));
    const stateRows = detail.length === 0
      ? '<tr><td colspan="3" class="empty-state">No Terraform-managed resources yet.</td></tr>'
      : detail.map(r => {
          const editBtn = r.has_sidecar
            ? `<button class="btn btn-sm tf-edit-resource"
                       data-safe="${esc(r.local_name)}"
                       data-address="${esc(r.address)}"
                       title="Load the resource's spec back into a new declaration for editing.">✎ Edit</button>`
            : `<span class="tf-no-sidecar" title="No sidecar JSON — was created via the legacy /apply path. Edit not available.">no sidecar</span>`;
          return `
            <tr><td><code>${esc(r.address)}</code></td>
                <td><span class="badge ok">tracked</span></td>
                <td class="tf-state-actions">
                  ${editBtn}
                  <button class="btn btn-sm btn-danger tf-destroy-resource"
                          data-address="${esc(r.address)}"
                  >🗑 Destroy</button>
                </td>
            </tr>`;
        }).join('');
    return `
      ${summary}

      <nav class="sub-tabs sub-tabs-2nd" role="tablist" aria-label="Terraform sections">
        <button type="button" class="sub-tab" data-tf-tab="decls"
                role="tab">📦 Declarations</button>
        <button type="button" class="sub-tab" data-tf-tab="live"
                role="tab">🌐 Live resources</button>
        <button type="button" class="sub-tab" data-tf-tab="install"
                role="tab">⚙ Installation</button>
      </nav>

      <section class="tf-subtab-content" data-tf-tab="decls" role="tabpanel">
        <p class="form-hint">A declaration bundles one or more Harvester
          resources (VMs, images, SSH keys…) that are applied together
          via Terraform. Click <strong>Open</strong> to edit a
          declaration in a floating overlay — you can open several at
          once and arrange them side by side.</p>
        <div id="tf-decl-list" class="tf-decl-list"></div>
      </section>

      <section class="tf-subtab-content" data-tf-tab="live" role="tabpanel">
        <h4 style="margin-top:8px;">Resources in state (cluster: ${esc(cluster || '—')})</h4>
        <p class="form-hint">Resources currently tracked by Terraform on
          this cluster. Click ✎ Edit to reopen one in a fresh declaration
          (requires a sidecar — only resources created via a declaration
          carry one).</p>
        <table class="data-table">
          <thead><tr><th>Address</th><th>Status</th><th></th></tr></thead>
          <tbody>${stateRows}</tbody>
        </table>
        <div id="tf-result" class="apply-result" style="margin-top:8px;"></div>
        <div class="apply-bar" style="margin-top:14px; gap:8px;">
          <button class="btn btn-secondary btn-sm" id="btn-tf-clean-stale"
                  title="Remove .tf files that aren't in state (failed creates from previous applies). Cluster resources untouched.">
            🧹 Clean stale files
          </button>
          <button class="btn btn-danger btn-sm" id="btn-tf-destroy"
                  title="terraform destroy the entire workspace for this cluster">
            🧨 Destroy workspace
          </button>
        </div>
      </section>

      <section class="tf-subtab-content" data-tf-tab="install" role="tabpanel">
        <h4 style="margin-top:8px;">Terraform installation</h4>
        <table class="data-table" style="margin-bottom:14px;">
          <tbody>
            <tr><td>Terraform CLI</td>
                <td><code>${esc(info.terraform_bin)}</code>
                  ${info.terraform_available
                    ? '<span class="badge ok">available</span>'
                    : '<span class="badge bad">missing</span>'}</td></tr>
            <tr><td>Harvester provider</td>
                <td>${info.provider_binary
                  ? `<code>${esc(info.provider_binary)}</code>
                     <span class="form-hint">v${esc(info.provider_version)},
                       ${Math.round((info.provider_binary_size||0)/1024/1024)} MB</span>`
                  : '<span class="badge bad">missing</span>'}</td></tr>
            <tr><td>Workspace dir</td>
                <td><code>${esc(info.workspaces_dir)}</code></td></tr>
            <tr><td>Examples bundled</td>
                <td>${(info.example_resources || []).length} resource type(s)</td></tr>
          </tbody>
        </table>
        <p class="form-hint">The airgap bundle packages the Terraform CLI,
          the Harvester provider and the bundled examples into a single
          tar.gz that can be transferred to an offline host.</p>
        <button class="btn btn-secondary btn-sm" id="btn-tf-bundle-build"
                title="Bundle terraform + provider + examples into a tar.gz for airgap transfer">
          🔨 Build airgap bundle
        </button>
      </section>`;
  }

  // -------------------------------------------------------------------------
  // Declarations list
  // -------------------------------------------------------------------------
  function renderDeclarations(cluster) {
    const root = $('#tf-decl-list');
    if (!root) return;
    if (cluster === undefined) cluster = $('#cluster-select')?.value || '';
    const decls = window.TFDecl.list().filter(d => !d.cluster || d.cluster === cluster);
    if (decls.length === 0) {
      root.innerHTML = `
        <div class="tf-decl-empty">
          <p>No declarations yet. Create one to start grouping resources.</p>
          <button class="btn btn-primary btn-sm" id="btn-tf-decl-new">+ New declaration</button>
        </div>`;
    } else {
      const activeId = window.TFDecl.getActive()?.id || '';
      root.innerHTML = `
        ${decls.map(d => renderDeclRow(d, d.id === activeId)).join('')}
        <button class="btn btn-secondary btn-sm" id="btn-tf-decl-new"
                style="margin-top:6px;">+ New declaration</button>`;
    }
    root.addEventListener('click', onDeclListClick);
  }

  function renderDeclRow(decl, isActive) {
    const totals = decl.resources.length;
    let summary = '';
    if (window.TFForm && totals > 0) {
      const incomplete = decl.resources.filter(r => {
        const v = window.TFForm.validateAll(r.spec, r.kind);
        return !v.valid;
      }).length;
      summary = incomplete > 0
        ? `<span class="tf-decl-summary tf-decl-summary--warn">⚠ ${incomplete}/${totals} incomplete</span>`
        : `<span class="tf-decl-summary tf-decl-summary--ok">✓ ${totals} ready</span>`;
    } else {
      summary = `<span class="tf-decl-summary">empty</span>`;
    }
    const status = decl.last_applied_status
      ? `<span class="tf-decl-applied tf-decl-applied--${esc(decl.last_applied_status)}"
              title="Last apply: ${esc(decl.last_applied_at)}"
        >last: ${esc(decl.last_applied_status)}</span>` : '';
    return `
      <div class="tf-decl-item ${isActive ? 'tf-decl-item--active' : ''}"
           data-decl-id="${esc(decl.id)}">
        <div class="tf-decl-item__head">
          <strong class="tf-decl-item__name">${esc(decl.name)}</strong>
          ${summary}${status}
        </div>
        <div class="tf-decl-item__actions">
          <button class="btn btn-sm tf-decl-open"  data-id="${esc(decl.id)}">Open</button>
          <button class="btn btn-sm tf-decl-dryrun"
                  data-id="${esc(decl.id)}" data-dry="1">Dry-run</button>
          <button class="btn btn-sm btn-primary tf-decl-apply"
                  data-id="${esc(decl.id)}" data-dry="0">Apply ▶</button>
          <button class="btn btn-sm btn-danger tf-decl-destroy"
                  data-id="${esc(decl.id)}"
                  title="terraform destroy every resource of this declaration that's currently in state, then clean its .tf + .json files.">🧨 Destroy</button>
          <button class="btn btn-sm tf-decl-delete"
                  data-id="${esc(decl.id)}"
                  title="Remove this declaration from the local list only — cluster resources are NOT touched.">🗑</button>
        </div>
      </div>`;
  }

  function onDeclListClick(e) {
    const cluster = $('#cluster-select')?.value || '';
    if (e.target.closest('#btn-tf-decl-new')) {
      const name = prompt('Name for the new declaration:', `decl-${Date.now().toString(36)}`);
      if (!name) return;
      const decl = window.TFDecl.create(name, cluster);
      renderDeclarations(cluster);
      return;
    }
    const open = e.target.closest('.tf-decl-open');
    if (open) {
      window.TFDecl.setActive(open.dataset.id);
      if (window.TFDeclPanel) window.TFDeclPanel.open(open.dataset.id);
      renderDeclarations(cluster);
      return;
    }
    const apply  = e.target.closest('.tf-decl-apply');
    const dry    = e.target.closest('.tf-decl-dryrun');
    if (apply || dry) {
      const id = (apply || dry).dataset.id;
      const dryRun = !!dry;
      applyDeclaration(id, dryRun);
      return;
    }
    const destroy = e.target.closest('.tf-decl-destroy');
    if (destroy) {
      destroyDeclaration(destroy.dataset.id);
      return;
    }
    const del = e.target.closest('.tf-decl-delete');
    if (del) {
      const d = window.TFDecl.get(del.dataset.id);
      if (!d) return;
      if (!confirm(`Delete declaration "${d.name}"? This only removes the local definition — no cluster resource is touched.`)) return;
      window.TFDecl.remove(del.dataset.id);
      renderDeclarations(cluster);
      return;
    }
  }

  // v1.5.5: the active-declaration content moved into a FloatingPanel
  // (tf-decl-panel.js). The sub-tab "Declarations" only carries the
  // list of declarations now; the edit surface is the overlay panel.

  // -------------------------------------------------------------------------
  // Apply / Dry-run a declaration
  // -------------------------------------------------------------------------
  async function applyDeclaration(declId, dryRun) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster) { alert('Select a cluster first.'); return; }
    const decl = window.TFDecl.get(declId);
    if (!decl) return;
    if (decl.resources.length === 0) {
      alert('This declaration has no resources to apply.');
      return;
    }

    // Validate every resource
    const invalid = [];
    decl.resources.forEach((r, i) => {
      const v = window.TFForm.validateAll(r.spec, r.kind);
      if (!v.valid) {
        invalid.push(`#${i + 1} ${r.kind} "${r.spec?.name || ''}" — missing: ${
            Object.entries(v.sections)
              .filter(([_, s]) => !s.valid)
              .map(([id, s]) => `${id}(${s.missing.join(', ')})`)
              .join('; ')}`);
      }
    });
    if (invalid.length > 0) {
      alert(`Declaration "${decl.name}" has incomplete resources:\n\n${invalid.join('\n')}`);
      return;
    }

    if (!dryRun) {
      if (!confirm(`Apply declaration "${decl.name}" (${decl.resources.length} resources) on cluster "${cluster}" — REAL?`)) return;
    }

    const result = $('#tf-result');
    if (result) result.textContent = 'starting…';

    let runId;
    try {
      const r = await fetch(
        `/api/terraform/${encodeURIComponent(cluster)}/apply_declaration`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            declaration: {
              name: decl.name,
              resources: decl.resources.map(x => ({ kind: x.kind, spec: x.spec })),
            },
            dry_run: dryRun,
          }) });
      const d = await r.json();
      if (!r.ok) {
        const detail = (d.errors || []).map(e =>
          `#${e.index + 1} ${e.kind || '?'} "${e.name || ''}": ${e.error}`).join('\n');
        if (result) result.innerHTML =
          `<span style="color:var(--danger)">✗ ${d.error}${detail ? '\n' + esc(detail) : ''}</span>`;
        return;
      }
      runId = d.action_id;
      if (result) result.innerHTML =
        `<span style="color:var(--accent)">⏳ ${dryRun ? 'plan' : 'apply'} running — dock action <code>${runId}</code></span>`;
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      return;
    }
    if (runId) followRun(runId, dryRun, 'declaration', cluster,
                          { name: decl.name, declId });
  }

  async function destroyDeclaration(declId) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster) { alert('Select a cluster first.'); return; }
    const decl = window.TFDecl.get(declId);
    if (!decl) return;
    if (decl.resources.length === 0) {
      alert('This declaration has no resources to destroy.');
      return;
    }
    // v1.5.2: typed confirmation instead of a click-through confirm().
    // We force the user to type the declaration's exact name — much
    // harder to dismiss by reflex than a generic "OUI".
    const ok = await confirmDestructive({
      title: `Destroy declaration "${decl.name}"`,
      message:
        `Every resource of this declaration currently in state will be ` +
        `destroyed on cluster "${cluster}". Their .tf + .json files will ` +
        `be removed from the workspace. Resources not in state are skipped.`,
      detail:
        decl.resources.map((r, i) =>
          `#${i + 1} ${r.kind}  ${r.spec?.name || ''}`).join('\n'),
      requiredText: decl.name,
      confirmLabel: `🧨 Destroy "${decl.name}"`,
    });
    if (!ok) return;

    const result = $('#tf-result');
    if (result) result.textContent = 'starting destroy…';
    let runId;
    try {
      const r = await fetch(
        `/api/terraform/${encodeURIComponent(cluster)}/destroy_declaration`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            declaration: {
              name: decl.name,
              resources: decl.resources.map(x => ({ kind: x.kind, spec: x.spec })),
            },
            dry_run: false,
          }) });
      const d = await r.json();
      if (!r.ok) {
        const detail = (d.errors || []).map(e =>
          `#${e.index + 1} ${e.kind || '?'} "${e.name || ''}": ${e.error}`).join('\n');
        if (result) result.innerHTML =
          `<span style="color:var(--danger)">✗ ${d.error}${detail ? '\n' + esc(detail) : ''}</span>`;
        return;
      }
      runId = d.action_id;
      if (result) result.innerHTML =
        `<span style="color:var(--accent)">⏳ destroy running — dock action <code>${runId}</code></span>`;
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      return;
    }
    if (runId) followRun(runId, false, 'destroy-decl', cluster,
                          { name: decl.name, declId, isDestroy: true });
  }

  // -------------------------------------------------------------------------
  // SSE log overlay (reused from v1.4.37)
  // -------------------------------------------------------------------------
  function followRun(runId, dryRun, kindLabel, cluster, ctx) {
    const overlay = ensureLogOverlay();
    overlay.classList.remove('hidden');
    overlay.dataset.runId = runId;
    overlay.querySelector('.tf-log-title').textContent =
      `${dryRun ? 'Dry-run' : 'Apply'} — ${kindLabel}/${ctx.name || '?'} on ${cluster}`;
    const body = overlay.querySelector('.tf-log-body');
    body.innerHTML = '';
    const status = overlay.querySelector('.tf-log-status');
    status.textContent = 'running…';
    status.className = 'tf-log-status running';
    const append = (cls, text) => {
      const line = document.createElement('div');
      line.className = `tf-log-line ${cls || ''}`;
      line.textContent = text;
      body.appendChild(line);
      body.scrollTop = body.scrollHeight;
    };
    SSEReconnect.connect(`/api/stream/${encodeURIComponent(runId)}`, {
      on: {
        step: (e) => {
          const ev = JSON.parse(e.data);
          const cls = ev.status === 'done' ? 'ok'
                    : ev.status === 'error' ? 'err'
                    : ev.status === 'skipped' ? 'dim' : 'info';
          append(cls, `[step] ${ev.step_id} → ${ev.status}${ev.message ? ' — ' + ev.message : ''}`);
        },
        log: (e) => {
          const ev = JSON.parse(e.data);
          const msg = (ev.message || '').replace(/\u001b\[[0-9;]*m/g, '');
          if (!msg.trim()) return;
          const cls = /error/i.test(msg) ? 'err'
                    : /warning|warn/i.test(msg) ? 'warn'
                    : /destroy|will be created/i.test(msg) ? 'ok' : '';
          append(cls, msg);
        },
        status: (e) => {
          const ev = JSON.parse(e.data);
          append(ev.status === 'done' ? 'ok' : 'warn',
                 `[status] ${ev.status}${ev.exit_code !== undefined ? ' (exit ' + ev.exit_code + ')' : ''}`);
        },
        end: (e) => {
          let exit = 0;
          try { exit = JSON.parse(e.data).exit_code ?? 0; } catch {}
          const isDestroy = !!(ctx && ctx.isDestroy);
          if (exit === 0) {
            status.textContent = dryRun
              ? '✓ plan ready (no changes applied)'
              : (isDestroy ? '✓ destroy completed' : '✓ apply completed');
            status.className = 'tf-log-status ok';
            if (ctx && ctx.declId) {
              window.TFDecl.markApplied(ctx.declId,
                isDestroy ? 'destroyed' : 'done');
              if (window.TFDeclPanel) window.TFDeclPanel.refresh(ctx.declId);
            }
            if (!dryRun) setTimeout(refresh, 1500);
          } else {
            status.textContent = `✗ ${dryRun ? 'plan' : (isDestroy ? 'destroy' : 'apply')} failed (exit ${exit})`;
            status.className = 'tf-log-status err';
            if (ctx && ctx.declId) window.TFDecl.markApplied(ctx.declId, 'error');
          }
        },
      },
      onStatus: (s) => {
        if (s.state === 'retry') {
          append('warn', `[stream] connection lost — retrying in ${Math.round(s.delay/1000)}s (${s.attempt}/5)`);
        } else if (s.state === 'dead') {
          append('err', `[stream] reconnect failed after ${s.attempts} attempts — open Activity to see the recorded log`);
          status.textContent = '✗ stream lost (log preserved)';
          status.className = 'tf-log-status err';
        }
      },
    });
  }

  // -------------------------------------------------------------------------
  // confirmDestructive — modal that requires the user to type a specific
  // phrase (defaults to "OUI") before the action can fire. Used for every
  // destroy entry point so a stray double-click can't nuke a cluster.
  //
  // Returns a Promise<boolean>. Resolves false on Cancel / Escape / click-
  // outside, true on Confirm (only enabled once the typed text matches).
  // -------------------------------------------------------------------------
  function confirmDestructive(opts) {
    return new Promise((resolve) => {
      const requiredText = opts.requiredText || 'OUI';
      const title = opts.title || 'Confirm destruction';
      const message = opts.message || 'This action is irreversible.';
      const detail = opts.detail || '';
      const confirmLabel = opts.confirmLabel || '🧨 Destroy';
      const root = document.createElement('div');
      root.className = 'modal-overlay tf-confirm-overlay active';
      root.innerHTML = `
        <div class="modal modal-small tf-confirm-modal">
          <div class="modal-header">
            <div>
              <h3>🧨 ${esc(title)}</h3>
              <div class="modal-subtitle">Confirmation required</div>
            </div>
            <button class="btn-close tf-confirm-cancel" title="Cancel">×</button>
          </div>
          <div class="modal-body">
            <p class="tf-confirm-message">${esc(message)}</p>
            ${detail
              ? `<pre class="tf-confirm-detail">${esc(detail)}</pre>` : ''}
            <p class="tf-confirm-prompt">
              To confirm, type
              <code class="tf-confirm-required">${esc(requiredText)}</code>
              below:
            </p>
            <input type="text" class="tf-confirm-input"
                   autocomplete="off" autocapitalize="off" spellcheck="false">
            <div class="tf-confirm-actions">
              <button class="btn btn-secondary btn-sm tf-confirm-cancel">Cancel</button>
              <button class="btn btn-danger btn-sm tf-confirm-go"
                      disabled>${esc(confirmLabel)}</button>
            </div>
          </div>
        </div>`;
      document.body.appendChild(root);
      const input = root.querySelector('.tf-confirm-input');
      const goBtn = root.querySelector('.tf-confirm-go');
      const close = (ok) => {
        root.remove();
        document.removeEventListener('keydown', onKey);
        resolve(!!ok);
      };
      const onKey = (e) => {
        if (e.key === 'Escape') close(false);
        if (e.key === 'Enter' && !goBtn.disabled) close(true);
      };
      document.addEventListener('keydown', onKey);
      root.addEventListener('click', (e) => {
        if (e.target === root) close(false);
        if (e.target.closest('.tf-confirm-cancel')) close(false);
        if (e.target.closest('.tf-confirm-go') && !goBtn.disabled) close(true);
      });
      input.addEventListener('input', () => {
        goBtn.disabled = input.value !== requiredText;
      });
      // Focus the input so the user can start typing right away
      setTimeout(() => input.focus(), 0);
    });
  }

  function ensureLogOverlay() {
    let el = document.querySelector('#tf-log-overlay');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'tf-log-overlay';
    el.className = 'tf-log-overlay hidden';
    el.innerHTML = `
      <div class="tf-log-head">
        <strong class="tf-log-title">Terraform log</strong>
        <span class="tf-log-status"></span>
        <button type="button" class="btn-icon-sm tf-log-close" title="Hide log">×</button>
      </div>
      <pre class="tf-log-body"></pre>`;
    document.body.appendChild(el);
    el.querySelector('.tf-log-close').addEventListener('click', () =>
      el.classList.add('hidden'));
    return el;
  }

  // -------------------------------------------------------------------------
  // Workspace-wide actions (bundle / destroy / clean-stale / destroy-resource)
  // -------------------------------------------------------------------------
  async function buildBundle() {
    const result = $('#tf-result');
    if (result) result.textContent = 'starting bundle build…';
    try {
      const r = await fetch('/api/terraform/bundle/build', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error}</span>`; return; }
      if (result) result.innerHTML = `<span style="color:var(--accent)">✓ build started — see dock action <code>${d.action_id}</code></span>`;
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`; }
  }
  async function cleanStale() {
    const cluster = $('#cluster-select')?.value;
    if (!cluster) return;
    const result = $('#tf-result');
    let preview;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/clean_stale`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: true }) });
      preview = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${preview.error}</span>`; return; }
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
      return;
    }
    const list = preview.would_remove || [];
    if (list.length === 0) {
      if (result) result.innerHTML = `<span style="color:var(--text-dim)">✓ workspace is already clean</span>`;
      return;
    }
    if (!confirm(`Remove ${list.length} stale .tf file(s)?\n\n${list.join('\n')}\n\n(No cluster resource is touched.)`)) return;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/clean_stale`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: false }) });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error}</span>`; return; }
      if (result) result.innerHTML = `<span style="color:var(--accent)">✓ removed ${d.removed.length} stale file(s)</span>`;
      setTimeout(refresh, 800);
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`; }
  }
  async function destroyWorkspace() {
    const cluster = $('#cluster-select')?.value;
    if (!cluster) { alert('Select a cluster first.'); return; }
    const ok = await confirmDestructive({
      title: `Destroy entire workspace on "${cluster}"`,
      message:
        `This runs terraform destroy on the WHOLE workspace for ` +
        `cluster "${cluster}". Every Terraform-managed resource on the ` +
        `cluster will be deleted, regardless of which declaration owns ` +
        `it. This action is irreversible.`,
      requiredText: cluster,
      confirmLabel: `🧨 Destroy workspace`,
    });
    if (!ok) return;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/destroy`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: false }) });
      const d = await r.json();
      if (!r.ok) { alert('Destroy failed: ' + (d.error || 'unknown')); return; }
      const result = $('#tf-result');
      if (result) result.innerHTML = `<span style="color:var(--accent)">✓ destroy started — see dock action <code>${d.action_id}</code></span>`;
    } catch (e) { alert(e.message); }
  }
  async function importResourceForEdit(safe, address) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster || !safe) return;
    const result = $('#tf-result');
    try {
      const r = await fetch(
        `/api/terraform/${encodeURIComponent(cluster)}/sidecar/${encodeURIComponent(safe)}`);
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        if (result) result.innerHTML =
          `<span style="color:var(--danger)">✗ ${d.error || 'no sidecar found'}</span>`;
        return;
      }
      const meta = await r.json();
      const kind = meta.kind;
      const spec = meta.spec || {};
      if (!kind || !window.TF_SCHEMA[kind]) {
        if (result) result.innerHTML =
          `<span style="color:var(--danger)">✗ unsupported kind: ${esc(kind || '?')}</span>`;
        return;
      }
      // Re-use the declaration named after the sidecar's
      // declaration_name when it already exists locally — otherwise
      // create a new "Imported: <addr>" declaration.
      let target = null;
      const declName = meta.declaration_name || '';
      if (declName) {
        target = window.TFDecl.list().find(
          d => d.name === declName && d.cluster === cluster);
      }
      if (!target) {
        const importName = `Edit ${address}`;
        target = window.TFDecl.create(importName, cluster);
      }
      // If the same resource already lives in the declaration (by
      // spec.name), don't double it — set its spec to the freshly
      // fetched sidecar.
      let existing = (target.resources || []).find(
        x => x.kind === kind && x.spec?.name === spec.name);
      if (existing) {
        window.TFDecl.replaceResourceSpec(target.id, existing.id, spec);
      } else {
        window.TFDecl.addResourceWithSpec(target.id, kind, spec);
      }
      window.TFDecl.setActive(target.id);
      if (result) result.innerHTML =
        `<span style="color:var(--accent)">✎ loaded ${esc(address)} into declaration "${esc(target.name)}" — scroll up</span>`;
      // Re-render the panel so the active declaration view shows the
      // imported card.
      refresh();
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`;
    }
  }

  async function destroySingleResource(address) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster || !address) return;
    const ok = await confirmDestructive({
      title: `Destroy resource ${address}`,
      message:
        `terraform destroy -target=${address} on cluster "${cluster}", ` +
        `then remove its .tf file from the workspace.`,
      requiredText: address.split('.').pop(),
      confirmLabel: `🧨 Destroy ${address}`,
    });
    if (!ok) return;
    const result = $('#tf-result');
    if (result) result.textContent = 'starting destroy…';
    let runId;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/destroy_resource`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address, dry_run: false }) });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${d.error || 'failed'}</span>`; return; }
      runId = d.action_id;
      if (result) result.innerHTML = `<span style="color:var(--accent)">⏳ destroy running — dock action <code>${runId}</code></span>`;
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">✗ ${e.message}</span>`; return; }
    if (runId) followRun(runId, false, 'destroy', cluster, { name: address });
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------
  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-tf-refresh')) refresh();
      if (e.target.closest('#btn-tf-bundle-build')) {
        if (confirm('Build a Terraform airgap bundle (~150MB)?')) buildBundle();
      }
      if (e.target.closest('#btn-tf-destroy')) destroyWorkspace();
      if (e.target.closest('#btn-tf-clean-stale')) cleanStale();
      const dr = e.target.closest('.tf-destroy-resource');
      if (dr) destroySingleResource(dr.dataset.address);
      const ed = e.target.closest('.tf-edit-resource');
      if (ed) importResourceForEdit(ed.dataset.safe, ed.dataset.address);
      const tab = e.target.closest('#tf-status-body .sub-tab[data-tf-tab]');
      if (tab) { activateSubtab(tab.dataset.tfTab); return; }
    });
    // Refresh when the Terraform sub-tab becomes active
    document.addEventListener('click', (e) => {
      if (e.target.closest('#tab-automation .sub-tab[data-subtab="terraform"]') ||
          e.target.closest('.tab-child[data-subtab="terraform"]')) {
        setTimeout(refresh, 50);
      }
    });
  }

  return {
    init, refresh,
    renderDeclarations,
    applyDeclaration, destroyDeclaration,
  };
})();

document.addEventListener('DOMContentLoaded', TF.init);
window.TF = TF;
