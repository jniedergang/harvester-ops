/**
 * harvester-ops : onglet GitOps de Cluster API (2.8.0, tâche 9).
 *
 * Les entrées GitOps posées sur les clusters enfants (Flux par la console,
 * Fleet dans le Rancher de la session), leur outil, leur état et leur
 * origine ; le retrait d'une entrée ; la fenêtre « Ajouter une entrée » à
 * trois onglets (Paramètres, Vérifications, Résumé) : l'outil déduit est
 * montré (cluster importé dans Rancher : Fleet, sinon Flux), chaque saisie
 * relance la vérification (`gitops-check`, lecture seule) et le bouton
 * Appliquer reste grisé, avec la raison, tant qu'elle n'est pas revenue
 * sans bloquant pour les valeurs affichées. Les identifiants Git partent
 * une fois dans la demande et sont effacés du formulaire, jamais réaffichés.
 * Le serveur lance `harvester-capi gitops-deploy` / `gitops-remove` (parité
 * CLI, actions suivies). Voir docs/design/2026-11-07-gitops-oidc-280-plan.md.
 */
const CapiGitops = (() => {
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  const icon = (n, size) => (window.Icons ? Icons.svg(n, { size: size || 14 }) : '');
  const cluster = () => (window.App && App.managementCluster ? App.managementCluster() : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const NAME_RE = /^[a-z0-9]([-a-z0-9]{0,32}[a-z0-9])?$/;
  const REF_RE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
  const GIT_RE = /^(https|ssh):\/\/[^\s@/]+(@[^\s/]+)?(:\d{1,5})?\/\S+$/;
  let modal = null;
  let seqList = 0;

  // Un appel littéral par constat (contrôle de parité des traductions)
  const FINDINGS = {
    'gitops-name-too-long': (v) => tr('gop.fd.gitops-name-too-long', 'The entry name {name} is longer than {max} characters.', v),
    'gitops-source-unsupported': (v) => tr('gop.fd.gitops-source-unsupported', '{tool} does not take a {source} source: Flux takes an OCI artefact or Git, Fleet takes Git or a chart.', v),
    'gitops-flux-missing': (v) => tr('gop.fd.gitops-flux-missing', 'Flux is not installed and ready on this child cluster: apply the chart item {service} of a package first (Artefacts tab).', v),
    'gitops-namespace-not-team': (v) => tr('gop.fd.gitops-namespace-not-team', 'The namespace {namespace} is a system namespace: choose a team namespace.', v),
    'gitops-privileges-admin-only': (v) => tr('gop.fd.gitops-privileges-admin-only', 'Cluster privileges are reserved to administrators.', v),
    'gitops-fleet-admin-only': (v) => tr('gop.fd.gitops-fleet-admin-only', 'Fleet deploys with its agent, administrator of the child cluster: reserved to administrators.', v),
    'gitops-fleet-not-confined': (v) => tr('gop.fd.gitops-fleet-not-confined', 'Fleet is not confined to {namespace}: its agent can write anywhere in the child cluster.', v),
    'gitops-fleet-needs-rancher': (v) => tr('gop.fd.gitops-fleet-needs-rancher', 'Fleet needs the child cluster imported in Rancher (Access window of the cluster).', v),
    'gitops-secret-missing': (v) => tr('gop.fd.gitops-secret-missing', 'The credentials {secret} are missing.', v),
    'gitops-package-not-published': (v) => tr('gop.fd.gitops-package-not-published', 'The item {item} of {package} is not published in {registry}: publish the package first (Artefacts tab).', v),
    'gitops-flux-not-locked': (v) => tr('gop.fd.gitops-flux-not-locked', 'The Flux service {service} was installed without the multi-tenant lockdown: apply it again from the console.', v),
    'gitops-cluster-missing': (v) => tr('gop.fd.gitops-cluster-missing', 'The child cluster {cluster} does not exist.', v),
    'rancher-session-needed': (v) => tr('gop.fd.rancher-session-needed', 'Fleet lives in Rancher: sign in to the console through Rancher.', v),
    'child-admin-required': (v) => tr('gop.fd.child-admin-required', 'You cannot read the kubeconfig Secret of {cluster}: only an administrator of the child cluster poses GitOps entries on it.', v),
    'child-admin-unknown': (v) => tr('gop.fd.child-admin-unknown', 'Your right on {cluster} could not be checked: {message}', v),
    'gitops-invalid': (v) => tr('gop.fd.gitops-invalid', 'The entry is refused: {message}', v),
  };
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const findingText = (f) => {
    const fn = FINDINGS[f && f.code];
    const facts = Object.assign({}, (f && f.facts) || {});
    return fn ? fn(facts) : String((f && f.code) || '');
  };
  const findingsHtml = (fs) => (fs || []).map(f => `<div class="sto-finding ${LEVEL_CLASS[f.level] || 'sev-critical'}" data-code="${esc(f.code)}"
      data-level="${esc(f.level)}"><div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('');
  const toolLabel = (t) => (t === 'fleet' ? tr('gop.tool.fleet', 'Fleet') : t === 'flux' ? tr('gop.tool.flux', 'Flux') : tr('gop.tool.auto', 'Automatic'));

  async function json(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  const btn = (attrs, cls, ic, label, tip, disabled) => `<button type="button" class="btn btn-sm ${cls} tip" ${attrs}
      data-tip="${esc(tip)}" title="${esc(tip)}" aria-label="${esc(tip)}"${disabled ? ' disabled' : ''}>${icon(ic, 13)} ${esc(label)}</button>`;
  const field = (label, input, tip, extra) => `<label class="tf-field"${extra || ''}><span class="tf-label">${esc(label)}</span>${input
    .replace(/<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}" title="${esc(tip)}"`)}</label>`;

  // -------------------------------------------------------------------------
  // Liste
  // -------------------------------------------------------------------------
  function stateBadge(e) {
    const st = e.state || {};
    const ok = e.tool === 'fleet' ? !!st.ready : !!st.applied;
    const msg = st.message || '';
    const tip = ok ? tr('gop.t.stateOk', 'Delivered and up to date ({message})', { message: msg })
      : tr('gop.t.statePending', 'Not ready yet: {message}', { message: msg || '?' });
    return `<span class="badge ${ok ? 'ok' : 'warn'} tip gop-state" data-tip="${esc(tip)}" title="${esc(tip)}">${esc(ok
      ? tr('gop.state.ready', 'ready') : tr('gop.state.pending', 'pending'))}</span>`;
  }

  function originHtml(o) {
    o = o || {};
    const bits = [];
    if (o.plan) bits.push(`<span class="badge dim tip" data-tip="${esc(tr('gop.t.originPlan', 'Posed by the deployment plan {plan}', { plan: o.plan }))}">${esc(o.plan)}</span>`);
    if (o.run) {
      bits.push(`<a class="tip gop-run" href="#activity/action/${enc(o.run)}" data-tip="${esc(tr('gop.t.originRun', 'Open the run that posed it and its log'))}">${icon('logs', 12)} <code>${esc(o.run)}</code></a>`);
    }
    return bits.join(' ') || `<span class="form-hint tip" data-tip="${esc(tr('gop.t.originHand', 'Posed from this tab'))}">${esc(tr('gop.originHand', 'by hand'))}</span>`;
  }

  async function refresh() {
    const box = document.getElementById('capi-gitops-body');
    if (!box) return;
    const c = cluster();
    if (!c) { box.innerHTML = ''; return; }
    const mine = ++seqList;
    box.innerHTML = `<p class="form-hint">${esc(tr('common.loading', 'Loading…'))}</p>`;
    const r = await json('GET', `/api/capi/${enc(c)}/gitops`).catch(() => null);
    if (mine !== seqList) return;
    if (!r || !r.ok) {
      box.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('gop.loadError', 'The GitOps entries could not be read: {error}',
        { error: (r && r.d && r.d.error) || '?' }))}</div></div>`;
      return;
    }
    const entries = (r.d.entries || []).slice().sort((a, b) => `${a.namespace}/${a.cluster}/${a.entry}`
      .localeCompare(`${b.namespace}/${b.cluster}/${b.entry}`));
    const hint = r.d.session ? '' : `<p class="form-hint gop-no-session">${icon('info', 12)} ${esc(tr('gop.noSession',
      'Without a Rancher session, the Fleet entries are not listed: sign in to the console through Rancher to see them.'))}</p>`;
    if (!entries.length) {
      box.innerHTML = `${hint}<p class="empty-state">${esc(tr('gop.empty', 'No GitOps entry on the child clusters of {cluster} yet: add one to deliver a Git repository or an OCI artefact with Flux, or with Fleet when the child cluster is imported in Rancher.', { cluster: c }))}</p>`;
      return;
    }
    const rows = entries.map(e => {
      const ref = `${e.namespace}/${e.cluster}`;
      const fleet = e.tool === 'fleet';
      const off = !can('operator') || (fleet && !can('admin'));
      const tip = !can('operator') ? tr('gop.t.removeOperator', 'Removing an entry needs the operator role')
        : fleet && !can('admin') ? tr('gop.t.removeFleetAdmin', 'Fleet entries are removed by an administrator')
          : tr('gop.t.remove', 'Remove this entry: the objects it delivered are pruned, the namespaces stay');
      return `<tr class="gop-row" data-ref="${esc(ref)}" data-entry="${esc(e.entry)}" data-tool="${esc(e.tool)}">
        <td><code>${esc(ref)}</code></td>
        <td><strong>${esc(e.entry)}</strong>${e.name ? ` <span class="form-hint">${esc(e.name)}</span>` : ''}</td>
        <td><span class="badge ${fleet ? 'info' : 'dim'} tip gop-tool" data-tip="${esc(fleet
          ? tr('gop.t.toolFleet', 'Delivered by Fleet, in the Rancher that imported the child cluster')
          : tr('gop.t.toolFlux', 'Delivered by Flux, installed in the child cluster'))}">${esc(toolLabel(e.tool))}</span></td>
        <td>${stateBadge(e)}</td>
        <td>${originHtml(e.origin)}</td>
        <td>${btn('data-gop-remove', 'btn-danger', 'trash', tr('gop.remove', 'Remove'), tip, off)}</td></tr>`;
    }).join('');
    box.innerHTML = `${hint}<div class="gop-scroll" style="overflow-x:auto;"><table class="data-table gop-table"><thead><tr>
        <th>${esc(tr('gop.col.cluster', 'Child cluster'))}</th><th>${esc(tr('gop.col.entry', 'Entry'))}</th>
        <th>${esc(tr('gop.col.tool', 'Tool'))}</th><th>${esc(tr('gop.col.state', 'State'))}</th>
        <th>${esc(tr('gop.col.origin', 'Origin'))}</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  async function removeEntry(row) {
    const ref = row.dataset.ref;
    const entry = row.dataset.entry;
    if (!confirm(tr('gop.confirmRemove', 'Remove the GitOps entry {entry} from {cluster}? The objects it delivered are pruned; the namespaces stay.', { entry, cluster: ref }))) return;
    const [ns, name] = ref.split('/');
    const r = await json('DELETE', `/api/capi/${enc(cluster())}/gitops/${enc(ns)}/${enc(name)}/${enc(entry)}`);
    if (!r.ok) { alert(r.d.error || `HTTP ${r.status}`); return; }
    setTimeout(refresh, 1500);
  }

  // -------------------------------------------------------------------------
  // Fenêtre « Ajouter une entrée »
  // -------------------------------------------------------------------------
  function closeModal() {
    if (modal && modal._cleanup) modal._cleanup();
    if (modal) modal.remove();
    modal = null;
  }

  async function openAdd(prefill) {
    const pre = prefill || {};
    const c = cluster();
    if (!c) return;
    const [dg, rs, ar, rg] = await Promise.all([
      json('GET', `/api/capi/${enc(c)}/diag`).catch(() => null),
      json('GET', `/api/capi/${enc(c)}/rancher-status`).catch(() => null),
      json('GET', '/api/artefacts').catch(() => null),
      json('GET', '/api/registries').catch(() => null),
    ]);
    const children = ((dg && dg.ok && dg.d.capi_clusters) || []).map(x => `${x.namespace}/${x.name}`);
    const imported = {};
    const rsOk = !!(rs && rs.ok);
    ((rsOk && rs.d.clusters) || []).forEach(x => { imported[`${x.namespace}/${x.cluster}`] = !!x.imported; });
    const pkgs = (ar && ar.ok && ar.d.packages) || [];
    const regs = ((rg && rg.ok && rg.d.registries) || []).filter(x => (x.clusters || []).includes(c));
    const items = new Map();                       // nom@version -> [{name, kind}] | 'loading'
    const tabs = ['params', 'checks', 'summary'];
    const TAB = { params: tr('reg.tab.params', 'Parameters'), checks: tr('reg.tab.checks', 'Checks'), summary: tr('reg.tab.summary', 'Summary') };
    const TAB_TIP = {
      params: tr('gop.t.tabParams', 'Child cluster, tool, source and rights of the entry'),
      checks: tr('gop.t.tabChecks', 'What the console checked on the child cluster for these values'),
      summary: tr('gop.t.tabSummary', 'What will be posed, before Apply'),
    };
    const src0 = pre.source || {};
    const opt = (v, label, sel, dis) => `<option value="${esc(v)}"${sel ? ' selected' : ''}${dis ? ' disabled' : ''}>${esc(label)}</option>`;
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active reg-modal gop-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${icon('build')} ${esc(tr('gop.addTitle', 'Add a GitOps entry'))}</h3>
          <div class="modal-subtitle">${esc(tr('gop.addSub', 'On a child cluster of {cluster}', { cluster: c }))}</div></div>
          <button type="button" class="btn-close tip" data-gop-close data-tip="${esc(tr('common.cancel', 'Cancel'))}" title="${esc(tr('common.cancel', 'Cancel'))}">×</button></div>
        <div class="modal-body">
          <div class="sub-tabs sub-tabs-inline gop-tabs" role="tablist">${tabs.map((t, i) => `<button type="button" role="tab"
            class="sub-tab tip${i ? '' : ' active'}" data-gop-tab="${t}" data-tip="${esc(TAB_TIP[t])}" title="${esc(TAB_TIP[t])}">${esc(TAB[t])}${t === 'checks'
              ? ' <span class="badge dim" data-x="count"></span>' : ''}</button>`).join('')}</div>
          <section data-gop-pane="params"><div class="tf-args">
            ${field(tr('gop.f.cluster', 'Child cluster'), `<select data-f="cluster">${children.map(x => opt(x, x)).join('')}</select>`,
              tr('gop.t.cluster', 'A cluster created by Cluster API on this Harvester cluster (namespace/name)'))}
            ${field(tr('gop.f.name', 'Entry name'), `<input data-f="name" type="text" maxlength="34" spellcheck="false" autocomplete="off" placeholder="apps" value="${esc(pre.name || src0.item || '')}">`,
              tr('gop.t.name', 'Lower case letters, digits and dashes, 34 characters at most; names the objects of the entry'))}
            ${field(tr('gop.f.tool', 'Tool'), `<select data-f="tool">${['auto', 'flux', 'fleet'].map(t => opt(t, toolLabel(t), t === (pre.tool || 'auto'))).join('')}</select>`,
              tr('gop.t.tool', 'Automatic: Fleet if the child cluster is imported in Rancher, Flux otherwise'))}
            <p class="tf-desc gop-deduced" data-x="deduced" style="grid-column:1/-1;margin:0;"></p>
            ${field(tr('gop.f.namespace', 'Team namespace'), `<input data-f="namespace" type="text" spellcheck="false" autocomplete="off" placeholder="equipe-a" value="${esc(pre.namespace || '')}">`,
              tr('gop.t.namespace', 'The namespace of the child cluster the entry delivers into; never a system namespace'))}
            ${field(tr('gop.f.source', 'Source'), `<select data-f="source">${[
              ['oci', tr('gop.src.oci', 'OCI artefact of a published package (Flux)')],
              ['git', tr('gop.src.git', 'Git repository')],
              ['chart', tr('gop.src.chart', 'Chart of a published package (Fleet)')]].map(([v, l]) => opt(v, l, v === (src0.type || 'oci'))).join('')}</select>`,
              tr('gop.t.source', 'Where the tool reads what it delivers'))}
          </div>
          <fieldset class="tf-block" data-x="pkg-box"><legend>${esc(tr('gop.pkgLegend', 'Package'))}</legend><div class="tf-args">
            ${field(tr('gop.f.package', 'Package'), `<select data-f="package"><option value="">${esc(tr('gop.choose', 'Choose…'))}</option>${pkgs.map(p => {
              const k = `${p.name}@${p.version}`;
              return opt(k, `${p.name} ${p.version}`, src0.package === p.name && String(src0.version) === String(p.version));
            }).join('')}</select>`, tr('gop.t.package', 'An imported package (Artefacts tab)'))}
            ${field(tr('gop.f.item', 'Item'), '<select data-f="item"></select>',
              tr('gop.t.item', 'An item of the package: of kind gitops for an OCI artefact, chart for a chart'))}
            ${field(tr('gop.f.registry', 'Registry'), `<select data-f="registry">${regs.map(x => opt(x.name, `${x.name} (${x.endpoint || ''})`)).join('')}</select>`,
              tr('gop.t.registry', 'A registry attached to this Harvester cluster where the package is published'))}
          </div>${regs.length ? '' : `<p class="form-hint">${esc(tr('gop.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.'))}</p>`}</fieldset>
          <fieldset class="tf-block" data-x="git-box" hidden><legend>${esc(tr('gop.gitLegend', 'Git repository'))}</legend><div class="tf-args">
            ${field(tr('gop.f.url', 'Address'), '<input data-f="url" type="text" spellcheck="false" autocomplete="off" placeholder="ssh://git@git.example:22/team/apps.git">',
              tr('gop.t.url', 'https:// or ssh://, without credentials in the address'))}
            ${field(tr('gop.f.branch', 'Branch'), '<input data-f="branch" type="text" spellcheck="false" value="main">', tr('gop.t.branch', 'Branch read by the tool (main by default)'))}
            ${field(tr('gop.f.path', 'Path'), '<input data-f="path" type="text" spellcheck="false" value="./">', tr('gop.t.path', 'Directory of the repository delivered (./ by default)'))}
            ${field(tr('gop.f.creds', 'Credentials'), `<select data-f="creds">${opt('', tr('gop.creds.none', 'None (public repository)'))}${opt('ssh', tr('gop.creds.ssh', 'SSH key'))}${opt('basic', tr('gop.creds.basic', 'User and password or token'))}</select>`,
              tr('gop.t.creds', 'Sent once with Apply, kept in a Secret of the child cluster, never shown again'))}
          </div>
          <div class="tf-args" data-x="ssh-box" hidden>
            ${field(tr('gop.f.identity', 'Private key'), '<textarea data-c="identity" rows="3" spellcheck="false" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>',
              tr('gop.t.identity', 'Deploy key with read access; sent once, never shown again'))}
            ${field(tr('gop.f.knownHosts', 'Known hosts'), '<textarea data-c="known_hosts" rows="2" spellcheck="false"></textarea>',
              tr('gop.t.knownHosts', 'Every key type of the Git host (ssh-keyscan); one type only is refused by Flux and Fleet'))}
          </div>
          <div class="tf-args" data-x="basic-box" hidden>
            ${field(tr('gop.f.username', 'User'), '<input data-c="username" type="text" autocomplete="off" spellcheck="false">', tr('gop.t.username', 'Account with read access to the repository'))}
            ${field(tr('gop.f.password', 'Password or token'), '<input data-c="password" type="password" autocomplete="new-password">', tr('gop.t.password', 'Sent once, never shown again'))}
          </div></fieldset>
          <div class="tf-args">
            ${field(tr('gop.f.privileges', 'Rights'), `<select data-f="privileges">${opt('namespace', tr('gop.priv.namespace', 'The team namespace only (edit)'), true)}${opt('cluster',
              tr('gop.priv.cluster', 'The whole child cluster (cluster-admin, administrators)'), false, !can('admin'))}</select>`,
              can('admin') ? tr('gop.t.privileges', 'What the account of the entry may write: the team namespace, or the whole cluster')
                : tr('gop.t.privilegesAdmin', 'The whole cluster is reserved to administrators'))}
            ${field(tr('gop.f.interval', 'Interval (minutes)'), '<input data-f="interval" type="number" min="1" max="1440" value="10">',
              tr('gop.t.interval', 'How often the tool reads the source again, 1 to 1440 minutes'))}
            <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('gop.t.prune', 'Remove from the child cluster what disappears from the source'))}"><input data-f="prune" type="checkbox" checked><span class="tf-label">${esc(tr('gop.f.prune', 'Prune'))}</span></label>
          </div></section>
          <section data-gop-pane="checks" hidden><div class="xfer-report" data-x="report"><p class="form-hint">${esc(tr('gop.checking', 'Checking…'))}</p></div></section>
          <section data-gop-pane="summary" hidden><div data-x="summary"></div></section>
          <div class="xfer-live-line" data-x="progress" hidden></div>
        </div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-gop-close data-tip="${esc(tr('gop.t.close', 'Close this window'))}" title="${esc(tr('gop.t.close', 'Close this window'))}">${esc(tr('common.cancel', 'Cancel'))}</button>
          <span class="tip" data-x="go-wrap" data-tip="${esc(tr('gop.t.checkingGo', 'Waiting for the checks of these values'))}"><button type="button" class="btn btn-primary btn-sm" data-gop-go disabled>${icon('play', 13)} ${esc(tr('gop.apply', 'Apply'))}</button></span>
          <span class="apply-result" data-x="feedback"></span></div>
      </div>`;
    document.body.appendChild(modal);
    const m = modal;
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const q = (x) => m.querySelector(`[data-x="${x}"]`);
    const go = m.querySelector('[data-gop-go]');
    const state = { n: 0, t: null, launched: false };
    const show = (t) => {
      m.querySelectorAll('[data-gop-tab]').forEach(b => b.classList.toggle('active', b.dataset.gopTab === t));
      m.querySelectorAll('[data-gop-pane]').forEach(p => { p.hidden = p.dataset.gopPane !== t; });
    };
    m.querySelectorAll('[data-gop-tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.gopTab)));

    function deducedTool() {
      const ref = v('cluster').value;
      if (v('tool').value !== 'auto') return v('tool').value;
      if (!rsOk || !(ref in imported)) return null;
      return imported[ref] ? 'fleet' : 'flux';
    }
    function paintDeduced() {
      const ref = v('cluster').value;
      const t = deducedTool();
      const box = q('deduced');
      if (v('tool').value !== 'auto') { box.textContent = ''; return; }
      box.dataset.tool = t || '';
      box.textContent = !t ? tr('gop.deducedUnknown', 'Automatic: the import state of {cluster} in Rancher could not be read; the check decides.', { cluster: ref || '?' })
        : t === 'fleet' ? tr('gop.deducedFleet', 'Automatic: Fleet ({cluster} is imported in Rancher).', { cluster: ref })
          : tr('gop.deducedFlux', 'Automatic: Flux ({cluster} is not imported in Rancher).', { cluster: ref });
    }
    function paintItems() {
      const key = v('package').value;
      const sel = v('item');
      const want = v('source').value === 'chart' ? 'chart' : 'gitops';
      const list = items.get(key);
      const cur = sel.value || src0.item || '';
      if (!key) { sel.innerHTML = ''; return; }
      if (!Array.isArray(list)) {
        sel.innerHTML = `<option value="">${esc(tr('common.loading', 'Loading…'))}</option>`;
        if (!list) {
          items.set(key, 'loading');
          const [n, ver] = key.split('@');
          json('GET', `/api/artefacts/${enc(n)}/${enc(ver)}`).then(r => {
            items.set(key, r.ok ? ((r.d.index || {}).items || []).map(i => ({ name: i.name, kind: i.kind })) : []);
          }).catch(() => items.set(key, [])).then(() => { if (modal === m) { paintItems(); later(50); } });
        }
        return;
      }
      const offered = list.filter(i => i.kind === want);
      sel.innerHTML = offered.map(i => opt(i.name, `${i.name} (${i.kind})`, i.name === cur)).join('')
        || `<option value="">${esc(want === 'chart' ? tr('gop.noChartItem', 'No chart item in this package') : tr('gop.noGitopsItem', 'No gitops item in this package'))}</option>`;
    }
    function paintSource() {
      const t = v('source').value;
      q('pkg-box').hidden = t === 'git';
      q('git-box').hidden = t !== 'git';
      q('ssh-box').hidden = t !== 'git' || v('creds').value !== 'ssh';
      q('basic-box').hidden = t !== 'git' || v('creds').value !== 'basic';
      if (t !== 'git') paintItems();
    }

    /** Demande lue dans le formulaire, ou [null, raison] si incomplète. */
    function entryOf() {
      const ref = v('cluster').value;
      const name = v('name').value.trim();
      const ns = v('namespace').value.trim();
      if (!ref) return [null, tr('gop.r.cluster', 'Choose a child cluster.')];
      if (!NAME_RE.test(name)) return [null, tr('gop.r.name', 'The entry name: lower case letters, digits and dashes, 34 characters at most.')];
      if (!REF_RE.test(ns)) return [null, tr('gop.r.namespace', 'The team namespace: a lower case RFC 1123 name.')];
      const t = v('source').value;
      let source;
      if (t === 'git') {
        const url = v('url').value.trim();
        if (!GIT_RE.test(url) || (url.startsWith('https://') && url.split('/')[2].includes('@'))) {
          return [null, tr('gop.r.url', 'The Git address: https:// or ssh://, without credentials.')];
        }
        source = { type: 'git', url, branch: v('branch').value.trim() || 'main', path: v('path').value.trim() || './' };
        if (v('creds').value) source.secret = 'git';
      } else {
        const [pk, ver] = (v('package').value || '').split('@');
        if (!pk || !v('item').value) return [null, tr('gop.r.item', 'Choose a package and one of its items.')];
        if (!v('registry').value) return [null, tr('gop.noRegistry', 'No registry attached to this cluster: declare or pose one in the Registries tab.')];
        source = { type: t, package: pk, version: ver, item: v('item').value, registry: v('registry').value };
      }
      const interval = Number(v('interval').value);
      if (!Number.isInteger(interval) || interval < 1 || interval > 1440) return [null, tr('gop.r.interval', 'The interval: 1 to 1440 minutes.')];
      return [{ cluster: ref, entry: { name, tool: v('tool').value, namespace: ns, source, privileges: v('privileges').value,
        interval_minutes: interval, prune: v('prune').checked } }, ''];
    }
    function secretsOf(withValues) {
      const mode = v('source').value === 'git' ? v('creds').value : '';
      if (!mode) return {};
      if (!withValues) return { git: {} };
      const c2 = (k) => (m.querySelector(`[data-c="${k}"]`) || {}).value || '';
      return { git: mode === 'ssh' ? { identity: c2('identity'), known_hosts: c2('known_hosts') }
        : { username: c2('username'), password: c2('password') } };
    }
    function clearSecrets() { m.querySelectorAll('[data-c]').forEach(el => { el.value = ''; }); }

    function setGo(ok, why) {
      go.disabled = !ok || state.launched || !can('operator');
      const reason = !can('operator') ? tr('gop.t.operatorOnly', 'Posing a GitOps entry needs the operator role') : why;
      q('go-wrap').dataset.tip = reason;
      go.title = reason;
      go.setAttribute('aria-label', reason);
    }
    function paintSummary(body, tool) {
      const s = body.entry.source;
      const what = s.type === 'git' ? tr('gop.sum.git', 'Git {url}, branch {branch}, path {path}', s)
        : tr('gop.sum.pkg', '{type} {item} of {package} {version}, from {registry}', s);
      q('summary').innerHTML = `<ul class="reg-summary gop-summary">
        <li>${esc(tr('gop.sum.where', 'Entry {name} on {cluster}, into the namespace {namespace}', { name: body.entry.name, cluster: body.cluster, namespace: body.entry.namespace }))}</li>
        <li>${esc(tr('gop.sum.tool', 'Tool: {tool}', { tool: tool ? toolLabel(tool) : '?' }))}</li>
        <li>${esc(what)}</li>
        <li>${esc(body.entry.privileges === 'cluster' ? tr('gop.sum.privCluster', 'Rights: the whole child cluster (cluster-admin)')
          : tr('gop.sum.privNs', 'Rights: edit in {namespace} only', { namespace: body.entry.namespace }))}</li>
        <li>${esc(tr('gop.sum.interval', 'Read again every {n} minute(s); prune {prune}', { n: body.entry.interval_minutes,
          prune: body.entry.prune ? tr('gop.yes', 'yes') : tr('gop.no', 'no') }))}</li>
        ${s.secret ? `<li>${esc(tr('gop.sum.secret', 'Credentials sent once with Apply, never shown again'))}</li>` : ''}</ul>`;
    }

    async function runChecks() {
      const n = ++state.n;
      const [body, why] = entryOf();
      if (!body) {
        q('report').innerHTML = `<p class="form-hint">${esc(why)}</p>`;
        q('count').textContent = '';
        q('summary').innerHTML = `<p class="form-hint">${esc(why)}</p>`;
        setGo(false, why);
        return;
      }
      paintSummary(body, deducedTool());
      q('report').classList.add('is-checking');
      const r = await json('POST', `/api/capi/${enc(c)}/gitops`, Object.assign({ dry_run: true, secrets: secretsOf(false) }, body))
        .catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
      if (n !== state.n || modal !== m) return;
      q('report').classList.remove('is-checking');
      if (!r.ok) {
        const reason = r.d.error || `HTTP ${r.status}`;
        q('report').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(reason)}</div></div>`;
        q('count').textContent = '';
        setGo(false, tr('gop.t.blocked', 'Blocked: {reason}', { reason }));
        return;
      }
      const fs = r.d.findings || [];
      const blocks = fs.filter(f => f.level === 'block' || !f.level);
      q('report').innerHTML = findingsHtml(fs) || `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('gop.noBlocker', 'No blocker.'))}</div></div>`;
      q('count').textContent = String(blocks.length);
      if (r.d.tool) paintSummary(body, r.d.tool);
      if (r.d.blocked || blocks.length) setGo(false, tr('gop.t.blocked', 'Blocked: {reason}', { reason: blocks.length ? findingText(blocks[0]) : '?' }));
      else setGo(true, tr('gop.t.go', 'Pose the entry on the child cluster: a tracked action, followed in the dock'));
    }
    // Appliquer grisé dès une saisie, jusqu'aux constats de ces valeurs
    function later(ms) {
      clearTimeout(state.t);
      state.n++;
      setGo(false, tr('gop.t.checkingGo', 'Waiting for the checks of these values'));
      state.t = setTimeout(runChecks, ms);
    }
    m._cleanup = () => { clearTimeout(state.t); state.n++; };
    m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-gop-close]')) closeModal(); });
    m.addEventListener('input', (e) => { if (!e.target.dataset.c) later(500); });
    m.addEventListener('change', (e) => {
      if (['source', 'creds', 'package'].includes(e.target.dataset.f)) paintSource();
      if (['cluster', 'tool'].includes(e.target.dataset.f)) paintDeduced();
      // les identifiants ne changent rien aux constats (seul leur nom compte)
      if (!e.target.dataset.c) later(200);
    });
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      const [body] = entryOf();
      if (!body) return;
      m._cleanup();
      state.launched = true;
      go.disabled = true;
      const payload = Object.assign({ secrets: secretsOf(true) }, body);
      clearSecrets();                              // jamais réaffichés, même après un refus
      const r = await json('POST', `/api/capi/${enc(c)}/gitops`, payload)
        .catch((e) => ({ ok: false, status: 0, d: { error: e.message } }));
      const fb = q('feedback');
      if (!r.ok) {
        fb.textContent = r.d.error || `HTTP ${r.status}`;
        state.launched = false;
        setGo(false, tr('gop.t.retype', 'Refused: correct the values (the credentials must be typed again)'));
        later(200);
        return;
      }
      const line = q('progress');
      line.hidden = false;
      line.textContent = tr('gop.started', 'Posing (action {id}), see the dock.', { id: r.d.action_id });
      setTimeout(refresh, 800);
      if (!window.SSEReconnect || modal !== m) return;
      const es = SSEReconnect.connect(`/api/stream/${enc(r.d.action_id)}`, {
        on: {
          step: (ev) => { try { const st = JSON.parse(ev.data); if (st.message) line.textContent = st.message; } catch { /* ligne illisible */ } },
          end: (ev) => {
            let x = {};
            try { x = JSON.parse(ev.data); } catch { /* fin sans détail */ }
            es.close();
            line.textContent = x.status === 'done' ? tr('gop.done', 'Entry posed.') : tr('gop.failed', 'Posing the entry failed: see the dock.');
            setTimeout(refresh, 500);
          },
        },
      });
    });
    paintSource();
    paintDeduced();
    runChecks();
    return m;
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-gitops-add')) { openAdd(); return; }
      if (e.target.closest('#btn-gitops-refresh')) { refresh(); return; }
      const rm = e.target.closest('[data-gop-remove]');
      if (rm && !rm.disabled) removeEntry(rm.closest('.gop-row'));
    });
  }
  document.addEventListener('DOMContentLoaded', init);

  return { start: refresh, refresh, openAdd, _findingText: findingText };
})();
window.CapiGitops = CapiGitops;
