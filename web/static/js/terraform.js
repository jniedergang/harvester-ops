/**
 * harvester-ops : l'onglet Terraform.
 *
 * Trois sous-onglets : Déclarations (la vue TFDeclView, v1.55.0 : liste,
 * déclaration choisie, plan lisible avant d'appliquer), Ressources du
 * cluster (tout ce que Terraform gère ici, déclaration par déclaration) et
 * Installation (CLI, provider, paquet airgap). Les déclarations sont gardées
 * par la console et ont chacune leur état Terraform (v1.54.0).
 */
const TF = (() => {
  const $  = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  function lang() {
    try { return localStorage.getItem('harvester_ops_language') || 'en'; }
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
    const cluster = $('#cluster-select')?.value || '';
    // Même voile flouté que les autres chargements. Posé sur la CARTE : le
    // corps est réécrit par le rendu et emporterait le voile avec lui.
    if (!window.Veil) return refreshInner(out, cluster);
    return window.Veil.during(out.closest('.card') || out, {
      message: window.i18n ? i18n.t('common.loadingNamed') : 'Loading {name}…',
      name: cluster || 'Terraform',
      delay: 250,
    }, () => refreshInner(out, cluster));
  }

  async function refreshInner(out, cluster) {
    const myRev = ++_refreshRev;

    let info;
    try { info = await fetch('/api/terraform/info').then(r => r.json()); }
    catch (e) {
      if (myRev !== _refreshRev) return;
      out.innerHTML = `<div class="summary-bar bad">${esc(e.message)}</div>`;
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
    if (window.TFDeclView) TFDeclView.mount($('#tf-decls-view'), cluster);
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
    const tfOk = !!info.terraform_available;
    const provOk = !!info.provider_binary;
    const summary = tfOk && provOk
      ? `<div class="summary-bar ok">${Icons.svg('ok', { size: 14 })} ${esc(i18n.t('tf.ui.ready', {
          flavor: info.terraform_flavor === 'opentofu' ? 'OpenTofu' : 'Terraform',
          version: String(info.provider_version || '').replace(/^v/, '') }))}</div>`
      : `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })} ${esc([
          tfOk ? '' : i18n.t('tf.ui.cliMissing'), provOk ? '' : i18n.t('tf.ui.provMissing')].filter(Boolean).join(' '))}</div>`;
    // v1.5.3: prefer `resources_detail` (carries has_sidecar + kind);
    // fall back to bare addresses for older API responses.
    const detail = Array.isArray(state.resources_detail)
      ? state.resources_detail
      : (state.resources || []).map(addr => ({ address: addr,
                                                local_name: (addr.split('.')[1] || ''),
                                                has_sidecar: false }));
    const stateRows = detail.length === 0
      ? `<tr><td colspan="3" class="empty-state">${esc(i18n.t('tf.ui.noLive'))}</td></tr>`
      : detail.map(r => {
          const owner = r.declaration_id
            ? `<button class="btn btn-sm tf-open-decl tip"
                       data-decl="${esc(r.declaration_id)}"
                       data-tip="${esc(i18n.t('tf.tip.openDecl', { name: r.declaration_name || '' }))}">${Icons.svg('edit')} ${esc(r.declaration_name || i18n.t('tf.ui.edit'))}</button>`
            : r.has_sidecar
            ? `<button class="btn btn-sm tf-edit-resource tip"
                       data-safe="${esc(r.local_name)}"
                       data-address="${esc(r.address)}"
                       data-tip="${esc(i18n.t('tf.tip.adoptResource'))}">${Icons.svg('edit')} ${esc(i18n.t('tf.ui.adopt'))}</button>`
            : `<span class="tf-no-sidecar tip" data-tip="${esc(i18n.t('tf.tip.noSidecar'))}">${esc(i18n.t('tf.ui.noSidecar'))}</span>`;
          return `
            <tr><td><code>${esc(r.address)}</code></td>
                <td>${r.declaration_id ? `<span class="badge ok">${esc(i18n.t('tf.ui.inDecl'))}</span>`
                                       : `<span class="badge">${esc(i18n.t('tf.ui.shared'))}</span>`}</td>
                <td class="tf-state-actions">
                  ${owner}
                  <button class="btn btn-sm btn-danger tf-destroy-resource tip"
                          data-address="${esc(r.address)}" data-decl="${esc(r.declaration_id || '')}"
                          data-tip="${esc(i18n.t('tf.tip.destroyResource'))}">${Icons.svg('delete')} ${esc(i18n.t('tf.ui.destroy'))}</button>
                </td>
            </tr>`;
        }).join('');
    return `
      ${summary}

      <nav class="sub-tabs sub-tabs-2nd" role="tablist" aria-label="Terraform">
        <button type="button" class="sub-tab tip" data-tf-tab="decls" role="tab"
                data-tip="${esc(i18n.t('tf.ui.t.decls'))}">${Icons.svg('bundle')} ${esc(i18n.t('tf.ui.decls'))}</button>
        <button type="button" class="sub-tab tip" data-tf-tab="live" role="tab"
                data-tip="${esc(i18n.t('tf.ui.t.live'))}">${Icons.svg('network')} ${esc(i18n.t('tf.ui.live'))}</button>
        <button type="button" class="sub-tab tip" data-tf-tab="install" role="tab"
                data-tip="${esc(i18n.t('tf.ui.t.install'))}">${Icons.svg('settings')} ${esc(i18n.t('tf.ui.install'))}</button>
      </nav>

      <section class="tf-subtab-content" data-tf-tab="decls" role="tabpanel">
        <div id="tf-decls-view" class="tfd-host"></div>
      </section>

      <section class="tf-subtab-content" data-tf-tab="live" role="tabpanel">
        <h4 style="margin-top:8px;">${esc(i18n.t('tf.ui.liveTitle', { cluster: cluster || '-' }))}</h4>
        <p class="form-hint">${esc(i18n.t('tf.ui.liveHint'))}</p>
        <table class="data-table">
          <thead><tr><th>${esc(i18n.t('tf.ui.address'))}</th><th>${esc(i18n.t('tf.ui.managedBy'))}</th><th></th></tr></thead>
          <tbody>${stateRows}</tbody>
        </table>
        <div id="tf-result" class="apply-result" style="margin-top:8px;"></div>
        <div class="apply-bar" style="margin-top:14px; gap:8px;">
          <button class="btn btn-secondary btn-sm tip" id="btn-tf-clean-stale"
                  data-tip="${esc(i18n.t('tf.tip.cleanStale'))}">
            ${Icons.svg('clean')} ${esc(i18n.t('tf.ui.cleanStale'))}
          </button>
          <button class="btn btn-danger btn-sm tip" id="btn-tf-destroy"
                  data-tip="${esc(i18n.t('tf.tip.destroyWorkspace'))}">
            ${Icons.svg('destroy')} ${esc(i18n.t('tf.ui.destroyAll'))}
          </button>
        </div>
      </section>

      <section class="tf-subtab-content" data-tf-tab="install" role="tabpanel">
        <h4 style="margin-top:8px;">${esc(i18n.t('tf.ui.installTitle'))}</h4>
        <table class="data-table" style="margin-bottom:14px;">
          <tbody>
            <tr><td>${esc(i18n.t('tf.ui.cli'))}</td>
                <td><code>${esc(info.terraform_bin)}</code>
                  ${info.terraform_available
                    ? `<span class="badge ok">${esc(i18n.t('tf.ui.available'))}</span>`
                    : `<span class="badge bad">${esc(i18n.t('tf.ui.missing'))}</span>`}</td></tr>
            <tr><td>${esc(i18n.t('tf.ui.provider'))}</td>
                <td>${info.provider_binary
                  ? `<code>${esc(info.provider_binary)}</code>
                     ${originBadge(info.provider_origin)}
                     <span class="form-hint">v${esc(String(info.provider_version).replace(/^v/, ''))},
                       ${Math.round((info.provider_binary_size||0)/1024/1024)} MB,
                       ${esc(info.provider_arch || '')}</span>`
                  : `<span class="badge bad">${esc(i18n.t('tf.ui.missing'))}</span>
                     <details class="tf-missing-where">
                       <summary>${esc(i18n.t('tf.whereLooked'))}</summary>
                       <p class="form-hint">${esc(i18n.t('tf.pointEnv'))}
                         <code>${esc(info.provider_env || '')}=/chemin/du/provider</code></p>
                       <ul class="form-hint">${(info.provider_searched || [])
                         .map(x => `<li><code>${esc(x)}</code></li>`).join('')}</ul>
                     </details>`}</td></tr>
            <tr><td>${esc(i18n.t('tf.ui.workspaces'))}</td>
                <td><code>${esc(info.workspaces_dir)}</code></td></tr>
            <tr><td>${esc(i18n.t('tf.ui.examples'))}</td>
                <td>${esc(i18n.t('tf.ui.nTypes', { n: (info.example_resources || []).length }))}</td></tr>
          </tbody>
        </table>

        ${renderProviderUpdate(info)}

        <p class="form-hint">${esc(i18n.t('tf.ui.bundleHint'))}</p>
        <button class="btn btn-secondary btn-sm tip" id="btn-tf-bundle-build"
                data-tip="${esc(i18n.t('tf.tip.bundleBuild'))}">
          ${Icons.svg('build')} ${esc(i18n.t('tf.ui.bundleBuild'))}
        </button>
      </section>`;
  }

  // -------------------------------------------------------------------------
  // Provider update
  //
  // Le provider livré avec le paquet vieillit plus vite que le toolkit :
  // une version de Harvester peut exiger un provider plus récent que celui
  // du tarball. Le remplacer devait jusqu'ici se faire à la main sur
  // l'hôte, ce qui n'a de sens ni pour un opérateur ni pour un site airgap.
  // -------------------------------------------------------------------------
  function originBadge(origin) {
    // Clés écrites en toutes lettres : construire `'tf.prov.origin.' + origin`
    // les rendrait invisibles au contrôle de parité i18n, qui ne lit que des
    // littéraux — piège déjà payé sur les filtres d'activité.
    const label = origin === 'managed' ? i18n.t('tf.prov.origin.managed')
                : origin === 'bundled' ? i18n.t('tf.prov.origin.bundled')
                : origin === 'custom'  ? i18n.t('tf.prov.origin.custom')
                : '';
    if (!label) return '';
    // `ok` seulement pour une mise à jour posée par l'opérateur : le
    // provider du livrable n'est pas une anomalie, il ne mérite pas une
    // couleur d'alerte.
    const cls = origin === 'managed' ? 'badge ok' : 'badge';
    return `<span class="${cls}">${esc(label)}</span>`;
  }

  function renderProviderUpdate(info) {
    if (!info.provider_can_install) {
      return `<div class="summary-bar warn">${Icons.svg('warn', { size: 14 })}
                ${esc(i18n.t('tf.prov.unavailable'))}</div>`;
    }
    const when = info.provider_installed_at
      ? new Date(info.provider_installed_at * 1000).toLocaleString()
      : '';
    const installed = (info.provider_origin === 'managed' && when)
      ? `<p class="form-hint">${esc(i18n.t('tf.prov.installedFrom'))}
           <code>${esc(info.provider_installed_source || '')}</code>
           <br>${esc(when)}
           ${info.provider_installed_sha256
             ? `· <code class="sha">${esc(info.provider_installed_sha256.slice(0, 16))}…</code>`
             : ''}</p>`
      : '';
    const revert = info.provider_origin === 'managed'
      ? `<button type="button" class="btn btn-danger btn-sm btn-ico tip"
                 id="btn-tf-prov-revert" data-tip="${esc(i18n.t('tf.tip.provRevert'))}">
           ${Icons.svg('undo')} ${esc(i18n.t('tf.prov.revert'))}
         </button>`
      : '';
    return `
      <div class="card" style="margin-bottom:14px;">
        <div class="card-header">
          <h2>${Icons.svg('download', { size: 18 })} ${esc(i18n.t('tf.prov.title'))}</h2>
        </div>
        <div class="card-body">
          <p class="form-hint">${esc(i18n.t('tf.prov.hint'))}</p>
          <!-- Deux voies exclusives. Le bouton de chacune vit DANS son bloc :
               posé entre les deux, il ne se rattachait visuellement ni à
               l'une ni à l'autre. -->
          <div class="tf-prov-ways">
            <form id="tf-prov-form" class="capi-form">
              <fieldset>
                <legend>${esc(i18n.t('tf.prov.netLegend'))}</legend>
                <label style="grid-column:1/-1;">${esc(i18n.t('tf.prov.source'))} *
                  <input name="source" required placeholder="1.7.3"
                         data-tip="${esc(i18n.t('tf.prov.sourceHint'))}" class="tip">
                  <span class="form-hint">${esc(i18n.t('tf.prov.sourceHint'))}</span></label>
                <label style="grid-column:1/-1;">${esc(i18n.t('tf.prov.sha'))}
                  <input name="sha256" pattern="[0-9a-fA-F]{64}"
                         placeholder="${esc(i18n.t('tf.optional'))}">
                  <span class="form-hint">${esc(i18n.t('tf.prov.shaHint'))}</span></label>
                <div class="tf-prov-go">
                  <button type="submit" class="btn btn-primary btn-sm btn-ico tip"
                          data-tip="${esc(i18n.t('tf.tip.provInstall'))}">
                    ${Icons.svg('download')} ${esc(i18n.t('tf.prov.go'))}
                  </button>
                </div>
              </fieldset>
            </form>
            <form id="tf-prov-file-form" class="capi-form">
              <fieldset>
                <legend>${esc(i18n.t('tf.prov.fileLegend'))}</legend>
                <label style="grid-column:1/-1;">${esc(i18n.t('tf.prov.file'))} *
                  <input name="file" type="file" required accept=".zip,application/zip">
                  <span class="form-hint">${esc(i18n.t('tf.prov.fileHint'))}</span></label>
                <div class="tf-prov-go">
                  <button type="submit" class="btn btn-secondary btn-sm btn-ico tip"
                          data-tip="${esc(i18n.t('tf.tip.provUpload'))}">
                    ${Icons.svg('install')} ${esc(i18n.t('tf.prov.fileGo'))}
                  </button>
                </div>
              </fieldset>
            </form>
          </div>
          ${installed}
          <p class="form-hint">${esc(i18n.t('tf.prov.reinit'))}</p>
          <p class="form-hint">${esc(i18n.t('tf.prov.managedDir'))}
            <code>${esc(info.provider_managed_dir || '')}</code></p>
          <div id="tf-prov-result" class="apply-result"></div>
          ${revert ? `<div class="apply-bar">${revert}</div>` : ''}
        </div>
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Declarations list
  // -------------------------------------------------------------------------
  // -------------------------------------------------------------------------
  // SSE log overlay (reused from v1.4.37)
  // -------------------------------------------------------------------------
  function followRun(runId, dryRun, kindLabel, cluster, ctx) {
    const overlay = ensureLogOverlay();
    overlay.classList.remove('hidden');
    overlay.dataset.runId = runId;
    overlay.querySelector('.tf-log-title').textContent = i18n.t('tf.ui.logTitle', {
      what: dryRun ? i18n.t('tf.ui.dryRun') : (ctx && ctx.isDestroy ? i18n.t('tf.ui.destroy') : i18n.t('tf.ui.apply')),
      name: `${kindLabel}/${ctx.name || '?'}`, cluster });
    const body = overlay.querySelector('.tf-log-body');
    body.innerHTML = '';
    const status = overlay.querySelector('.tf-log-status');
    status.textContent = i18n.t('tf.ui.running');
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
          append(cls, `[step] ${ev.step_id}: ${ev.status}${ev.message ? ' (' + ev.message + ')' : ''}`);
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
            status.innerHTML = Icons.svg('ok', { size: 14, cls: 'icon-ok' }) + ' ' + esc(dryRun
              ? i18n.t('tf.ui.planReady')
              : (isDestroy ? i18n.t('tf.ui.destroyDone') : i18n.t('tf.ui.applyDone')));
            status.className = 'tf-log-status ok';
            // l'issue est enregistrée par la console : on relit la déclaration
            if (ctx && ctx.declId) window.TFDecl.refresh(ctx.declId);
            if (!dryRun) setTimeout(refresh, 1500);
          } else {
            status.innerHTML = `${Icons.svg('fail', { size: 14, cls: 'icon-err' })} ${esc(i18n.t('tf.ui.failedExit', {
              what: dryRun ? i18n.t('tf.ui.dryRun') : (isDestroy ? i18n.t('tf.ui.destroy') : i18n.t('tf.ui.apply')), exit }))}`;
            status.className = 'tf-log-status err';
            if (ctx && ctx.declId) window.TFDecl.refresh(ctx.declId);
          }
        },
      },
      onStatus: (s) => {
        if (s.state === 'retry') {
          append('warn', i18n.t('tf.ui.streamRetry', { s: Math.round(s.delay / 1000), n: s.attempt }));
        } else if (s.state === 'dead') {
          append('err', i18n.t('tf.ui.streamDead', { n: s.attempts }));
          status.innerHTML = Icons.svg('fail', { size: 14, cls: 'icon-err' }) + ' ' + esc(i18n.t('tf.ui.streamLost'));
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
      const title = opts.title || i18n.t('tf.ui.confirmTitle');
      const message = opts.message || i18n.t('tf.ui.irreversible');
      const detail = opts.detail || '';
      const confirmLabel = opts.confirmLabel || i18n.t('tf.ui.destroy');
      const root = document.createElement('div');
      root.className = 'modal-overlay tf-confirm-overlay active';
      root.innerHTML = `
        <div class="modal modal-small tf-confirm-modal">
          <div class="modal-header">
            <div>
              <h3>${Icons.svg('destroy')} ${esc(title)}</h3>
              <div class="modal-subtitle">${esc(i18n.t('tf.ui.confirmNeeded'))}</div>
            </div>
            <button class="btn-close tf-confirm-cancel tip" data-tip="${i18n.t('common.cancel')}">×</button>
          </div>
          <div class="modal-body">
            <p class="tf-confirm-message">${esc(message)}</p>
            ${detail
              ? `<pre class="tf-confirm-detail">${esc(detail)}</pre>` : ''}
            <p class="tf-confirm-prompt">
              ${esc(i18n.t('tf.ui.typeToConfirm'))}
              <code class="tf-confirm-required">${esc(requiredText)}</code>
            </p>
            <input type="text" class="tf-confirm-input tip" aria-label="${esc(i18n.t('tf.ui.typeToConfirm'))}"
                   data-tip="${esc(i18n.t('tf.ui.typeToConfirm'))}"
                   autocomplete="off" autocapitalize="off" spellcheck="false">
            <div class="tf-confirm-actions">
              <button class="btn btn-secondary btn-sm tf-confirm-cancel">${esc(i18n.t('common.cancel'))}</button>
              <button class="btn btn-danger btn-sm tf-confirm-go"
                      disabled>${Icons.svg('destroy', { size: 14 })} ${esc(confirmLabel)}</button>
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
        <strong class="tf-log-title">${esc(i18n.t('tf.ui.log'))}</strong>
        <span class="tf-log-status"></span>
        <button type="button" class="btn-icon-sm tf-log-close tip" data-tip="${i18n.t('tf.tip.hideLog')}">×</button>
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
    if (result) result.textContent = i18n.t('tf.ui.starting');
    try {
      const r = await fetch('/api/terraform/bundle/build', { method: 'POST' });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(d.error)}</span>`; return; }
      if (result) result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(i18n.t('tf.ui.startedDock', { id: d.action_id }))}</span>`;
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`; }
  }
  // -------------------------------------------------------------------------
  // Provider: install / update / revert
  // -------------------------------------------------------------------------
  function provResult(html, bad) {
    const el = $('#tf-prov-result');
    if (!el) return;
    el.innerHTML = `<span style="color:var(--${bad ? 'danger' : 'accent'})">${
      Icons.svg(bad ? 'fail' : 'ok', { size: 14 })} ${html}</span>`;
  }

  function provStarted(d) {
    // Le dock suit l'action : l'opérateur voit le téléchargement avancer
    // sans rester sur cet écran.
    provResult(`${esc(i18n.t('tf.prov.started'))} <code>${esc(d.action_id)}</code>`);
    // Le provider actif ne change qu'à la fin ; on rafraîchit alors.
    pollProviderUntilDone(d.action_id);
  }

  async function pollProviderUntilDone(actionId, tries = 0) {
    if (tries > 240) return;                 // ~20 min, borne de sécurité
    try {
      const r = await fetch(`/api/action/${encodeURIComponent(actionId)}`);
      const d = await r.json();
      // `starting` est l'état initial d'un ActionRun, pas une fin : tout ce
      // qui n'est pas terminal signifie « encore en cours ».
      if (d.status === 'done') { refresh(); return; }
      if (d.status === 'error' || d.status === 'cancelled') {
        provResult(esc(d.error_summary || i18n.t('tf.prov.failed')), true);
        return;
      }
      setTimeout(() => pollProviderUntilDone(actionId, tries + 1), 5000);
    } catch {
      setTimeout(() => pollProviderUntilDone(actionId, tries + 1), 5000);
    }
  }

  async function installProvider(ev) {
    ev.preventDefault();
    const f = ev.target;
    const source = f.source.value.trim();
    const sha256 = f.sha256.value.trim();
    if (!source) return;
    provResult(esc(i18n.t('tf.prov.starting')));
    try {
      const r = await fetch('/api/terraform/provider/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, sha256 }),
      });
      const d = await r.json();
      if (!r.ok) { provResult(esc(d.error || 'error'), true); return; }
      provStarted(d);
    } catch (e) { provResult(esc(e.message), true); }
  }

  async function uploadProvider(ev) {
    ev.preventDefault();
    const f = ev.target;
    const file = f.file.files?.[0];
    if (!file) return;
    const fd = new FormData();
    fd.append('file', file);
    provResult(esc(i18n.t('tf.prov.uploading')));
    try {
      const r = await fetch('/api/terraform/provider/upload',
                            { method: 'POST', body: fd });
      const d = await r.json();
      if (!r.ok) { provResult(esc(d.error || 'error'), true); return; }
      provStarted(d);
    } catch (e) { provResult(esc(e.message), true); }
  }

  async function revertProvider() {
    if (!confirm(i18n.t('tf.prov.confirmRevert'))) return;
    try {
      const r = await fetch('/api/terraform/provider', { method: 'DELETE' });
      const d = await r.json();
      if (!r.ok) { provResult(esc(d.error || 'error'), true); return; }
      refresh();
    } catch (e) { provResult(esc(e.message), true); }
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
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(preview.error)}</span>`; return; }
    } catch (e) {
      if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`;
      return;
    }
    const list = preview.would_remove || [];
    if (list.length === 0) {
      if (result) result.innerHTML = `<span style="color:var(--text-dim)">${Icons.svg('ok', { size: 14 })} ${esc(i18n.t('tf.ui.alreadyClean'))}</span>`;
      return;
    }
    if (!confirm(i18n.t('tf.ui.confirmClean', { n: list.length, files: list.join('\n') }))) return;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/clean_stale`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: false }) });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(d.error)}</span>`; return; }
      if (result) result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(i18n.t('tf.ui.cleaned', { n: d.removed.length }))}</span>`;
      setTimeout(refresh, 800);
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`; }
  }
  async function destroyWorkspace() {
    const cluster = $('#cluster-select')?.value;
    if (!cluster) { alert(i18n.t('tf.ui.pickCluster')); return; }
    const ok = await confirmDestructive({
      title: i18n.t('tf.ui.destroyAllTitle', { cluster }),
      message: i18n.t('tf.ui.destroyAllMsg', { cluster }),
      requiredText: cluster,
      confirmLabel: i18n.t('tf.ui.destroyAll'),
    });
    if (!ok) return;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/destroy`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dry_run: false }) });
      const d = await r.json();
      if (!r.ok) { alert(i18n.t('tf.ui.failed', { msg: d.error || '?' })); return; }
      // v1.55.0 : le journal s'ouvre, comme pour les autres destructions
      // (on ne suivait celle-ci que dans le dock, audit D11).
      followRun(d.action_id, false, 'workspace', cluster, { name: cluster, isDestroy: true });
    } catch (e) { alert(e.message); }
  }

  /** Une ressource de l'espace partagé (d'avant la v1.54) entre dans une
   *  déclaration : celle dont elle porte le nom, sinon une nouvelle. Au
   *  prochain plan, la déclaration la reprend sans la recréer. */
  async function importResourceForEdit(safe, address) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster || !safe) return;
    const result = $('#tf-result');
    const say = (html) => { if (result) result.innerHTML = html; };
    try {
      const r = await fetch(
        `/api/terraform/${encodeURIComponent(cluster)}/sidecar/${encodeURIComponent(safe)}`);
      const meta = await r.json().catch(() => ({}));
      if (!r.ok) {
        say(`<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(meta.error || i18n.t('tf.ui.noSidecar'))}</span>`);
        return;
      }
      const kind = meta.kind;
      const spec = meta.spec || {};
      if (!kind || !window.TF_SCHEMA[kind]) {
        say(`<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(i18n.t('tf.ui.unsupported', { kind: kind || '?' }))}</span>`);
        return;
      }
      await window.TFDecl.ready;
      const declName = meta.declaration_name || '';
      let target = declName ? window.TFDecl.list(cluster).find(d => d.name === declName) : null;
      if (!target) {
        target = await window.TFDecl.createAsync(declName || i18n.t('tf.ui.adoptName', { address }), cluster);
      }
      let existing = (target.resources || []).find(x => x.kind === kind && x.spec?.name === spec.name);
      const res = existing
        ? window.TFDecl.replaceResourceSpec(target.id, existing.id, spec)
        : window.TFDecl.addResourceWithSpec(target.id, kind, spec);
      await window.TFDecl.flush(target.id);
      say(`<span style="color:var(--accent)">${Icons.svg('edit')} ${esc(i18n.t('tf.ui.adopted', { address, name: target.name }))}</span>`);
      activateSubtab('decls');
      if (window.TFDeclView) TFDeclView.select(target.id, res && res.id);
    } catch (e) {
      say(`<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`);
    }
  }

  async function destroySingleResource(address, declId) {
    const cluster = $('#cluster-select')?.value;
    if (!cluster || !address) return;
    const ok = await confirmDestructive({
      title: i18n.t('tf.ui.destroyResTitle', { address }),
      message: declId ? i18n.t('tf.ui.destroyResMsgDecl', { address, cluster })
                      : i18n.t('tf.ui.destroyResMsg', { address, cluster }),
      requiredText: address.split('.').pop(),
      confirmLabel: i18n.t('tf.ui.destroy'),
    });
    if (!ok) return;
    const result = $('#tf-result');
    if (result) result.textContent = i18n.t('tf.ui.starting');
    let runId;
    try {
      const r = await fetch(`/api/terraform/${encodeURIComponent(cluster)}/destroy_resource`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ address, dry_run: false, declaration_id: declId || undefined }) });
      const d = await r.json();
      if (!r.ok) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(d.error || '?')}</span>`; return; }
      runId = d.action_id;
      if (result) result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('pending', { size: 14 })} ${esc(i18n.t('tf.ui.startedDock', { id: runId }))}</span>`;
    } catch (e) { if (result) result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`; return; }
    // v1.52.1 : sans isDestroy, la fin s'annonçait « apply completed ».
    if (runId) followRun(runId, false, 'destroy', cluster, { name: address, isDestroy: true, declId });
  }

  // -------------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------------
  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-tf-refresh')) refresh();
      if (e.target.closest('#btn-tf-bundle-build')) {
        if (confirm(i18n.t('tf.ui.confirmBundle'))) buildBundle();
      }
      if (e.target.closest('#btn-tf-destroy')) destroyWorkspace();
      if (e.target.closest('#btn-tf-clean-stale')) cleanStale();
      if (e.target.closest('#btn-tf-prov-revert')) { e.preventDefault(); revertProvider(); }
      const dr = e.target.closest('.tf-destroy-resource');
      if (dr) destroySingleResource(dr.dataset.address, dr.dataset.decl);
      const od = e.target.closest('.tf-open-decl');
      if (od && window.TFDeclView) { activateSubtab('decls'); TFDeclView.select(od.dataset.decl); }
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
    document.addEventListener('submit', (e) => {
      if (e.target?.id === 'tf-prov-form') installProvider(e);
      if (e.target?.id === 'tf-prov-file-form') uploadProvider(e);
    });
  }

  return { init, refresh, confirmDestructive, followRun };
})();

document.addEventListener('DOMContentLoaded', TF.init);
window.TF = TF;
