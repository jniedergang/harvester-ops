/**
 * harvester-ops : plans de déploiement (2.4.0).
 *
 * Onglet Plans de Bare-metal (#automation/pxe/plans) :
 *   - la liste des plans à gauche, le plan choisi à droite : nom,
 *     description, variables, une carte par phase avec son interrupteur
 *     (Registre et Artefacts grisés : à venir en 2.6+), délai de
 *     disponibilité, modèles Terraform choisis dans une liste et ordonnés
 *     par boutons monter/descendre, dépendances ; 2.5.0 : pile Cluster API
 *     (délai, versions du paquet actif) et clusters enfants (modèle, nom à
 *     variables, dépendance, raccourci « Plusieurs ») ;
 *   - les modèles de déclaration : liste, édition par les formulaires
 *     Terraform (TFForm / TF_SCHEMA, sans genre écrit en dur), retraits de
 *     secrets annoncés ;
 *   - 2.5.0 : les modèles de cluster (Cluster API), édités par le
 *     formulaire de création en mode modèle (CapiCreate.templateForm).
 *
 * Fenêtre « Déployer » (openDeploy) : Paramètres, Vérifications (constats de
 * la console, bloquants d'abord) et Résumé ; « Lancer » reste grisé tant
 * qu'un constat bloque. Ouverte depuis un plan, depuis la page d'un hôte
 * Harvester (cluster déjà choisi) ou pour reprendre un déroulé arrêté
 * (bouton « Reprendre » de l'Historique). L'action `deploy:<cluster>` est
 * suivie dans la fenêtre (étapes, actions filles liées) et dans le dock.
 *
 * Aucun secret n'est rangé : ceux d'une installation reprise sont saisis
 * dans la fenêtre et ne vivent que le temps de l'envoi.
 */
const BMPlans = (() => {
  const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const icon = (n, o) => (window.Icons ? Icons.svg(n, o) : '');
  const enc = encodeURIComponent;
  const lang = () => { try { return localStorage.getItem('harvester_ops_language') || 'en'; } catch { return 'en'; } };
  const st = (o) => (!o ? '' : typeof o === 'string' ? o : (o[lang()] || o.en || ''));
  const NAME_PATTERN = '[a-z0-9]([-a-z0-9]*[a-z0-9])?';
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  // rôle de la session (whoami) : ne sert qu'à griser, le serveur décide
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const RESUMABLE = ['error', 'cancelled', 'interrupted'];
  const HEX_ID = /\b[0-9a-f]{12}\b/g;

  // Un appel littéral par texte : le contrôle de parité des traductions ne
  // voit que ces formes.
  const PHASES = {
    install: () => tr('bmpl.phase.install'), readiness: () => tr('bmpl.phase.readiness'),
    terraform: () => tr('bmpl.phase.terraform'), capi: () => tr('bmpl.phase.capi'),
    clusters: () => tr('bmpl.phase.clusters'), registry: () => tr('bmpl.phase.registry'),
    artefacts: () => tr('bmpl.phase.artefacts'), plan: () => tr('bmpl.phase.plan'),
  };
  const PHASE_TIPS = {
    readiness: () => tr('bmpl.phaseTip.readiness'), terraform: () => tr('bmpl.phaseTip.terraform'),
    capi: () => tr('bmpl.phaseTip.capi'), clusters: () => tr('bmpl.phaseTip.clusters'),
    registry: () => tr('bmpl.phaseTip.registry'), artefacts: () => tr('bmpl.phaseTip.artefacts'),
  };
  const FINDINGS = {
    'plan-invalid': () => tr('dpw.f.planInvalid'),
    'phase-not-available': () => tr('dpw.f.phaseNotAvailable'),
    'template-missing': () => tr('dpw.f.templateMissing'),
    'var-missing': () => tr('dpw.f.varMissing'),
    'var-invalid': () => tr('dpw.f.varInvalid'),
    'decl-name-taken': () => tr('dpw.f.declNameTaken'),
    'tf-missing': () => tr('dpw.f.tfMissing'),
    'cluster-unreachable': () => tr('dpw.f.clusterUnreachable'),
    'template-newer': () => tr('dpw.f.templateNewer'),
    'decl-differs': () => tr('dpw.f.declDiffers'),
    'plan-changed': () => tr('dpw.f.planChanged'),
    'decl-exists': () => tr('dpw.f.declExists'),
    'readiness-later': () => tr('dpw.f.readinessLater'),
    'admin-kind': () => tr('dpw.f.adminKind'),
    'forbidden-construct': () => tr('dpw.f.forbiddenConstruct'),
    // 2.5.0 : phases Cluster API et Clusters
    'capi-bundle-missing': () => tr('dpw.f.capiBundleMissing'),
    'capi-bundle-no-turtles': () => tr('dpw.f.capiBundleNoTurtles'),
    'capi-needs-harvester-1.9': () => tr('dpw.f.capiNeedsHarvester19'),
    'capi-later': () => tr('dpw.f.capiLater'),
    'capi-version-differs': () => tr('dpw.f.capiVersionDiffers'),
    'capi-stack-missing': () => tr('dpw.f.capiStackMissing'),
    'capi-install-admin': () => tr('dpw.f.capiInstallAdmin'),
    'capi-template-missing': () => tr('dpw.f.capiTemplateMissing'),
    'cluster-var-missing': () => tr('dpw.f.clusterVarMissing'),
    'cluster-name-taken': () => tr('dpw.f.clusterNameTaken'),
    'cluster-exists': () => tr('dpw.f.clusterExists'),
    'identity-rules-unknown': () => tr('dpw.f.identityRulesUnknown'),
    'identity-name-reserved': () => tr('dpw.f.identityNameReserved'),
    'target-namespace-forced': () => tr('dpw.f.targetNamespaceForced'),
    'cloud-provider-binding-taken': () => tr('dpw.f.cloudProviderBindingTaken'),
    'vm-namespace-taken': () => tr('dpw.f.vmNamespaceTaken'),
    'vm-namespace-too-long': () => tr('dpw.f.vmNamespaceTooLong'),
    'refs-later': () => tr('dpw.f.refsLater'),
    'cluster-failed': () => tr('dpw.f.clusterFailed'),
    'capi-check-failed': () => tr('dpw.f.capiCheckFailed'),
    'fleet-name-twice': () => tr('dpw.f.fleetNameTwice'),
    'fleet-cpu-short': () => tr('dpw.f.fleetCpuShort'),
    'fleet-memory-short': () => tr('dpw.f.fleetMemoryShort'),
    'fleet-storage-short': () => tr('dpw.f.fleetStorageShort'),
    'fleet-storage-unknown': () => tr('dpw.f.fleetStorageUnknown'),
    'fleet-replicas-nodes': () => tr('dpw.f.fleetReplicasNodes'),
    'fleet-pool-short': () => tr('dpw.f.fleetPoolShort'),
    'fleet-dhcp-leases': () => tr('dpw.f.fleetDhcpLeases'),
    'fleet-sizes': () => tr('dpw.f.fleetSizes'),
    'rights-missing': () => tr('dpw.f.rightsMissing'),
    'rights-unchecked': () => tr('dpw.f.rightsUnchecked'),
  };
  const LEVELS = {
    blocking: () => tr('dpw.lvl.blocking'), warning: () => tr('dpw.lvl.warning'), info: () => tr('dpw.lvl.info'),
  };
  const LEVEL_CLS = { blocking: 'fail', warning: 'warn', info: 'info' };
  const LEVEL_RANK = { blocking: 0, warning: 1, info: 2 };
  const text = (map, key) => (map[key] ? map[key]() : String(key == null ? '' : key));
  const findingText = (f) => text(FINDINGS, f && f.code);
  const blockingOf = (findings) => (findings || []).filter(f => f && f.level === 'blocking');

  async function api(method, url, body) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  // Refus du serveur : [où, raison] par ligne, jamais une valeur
  function refusal(d, status) {
    const pairs = Array.isArray(d.errors) ? d.errors : (d.fields || []).map(f => [f, '']);
    const items = pairs.map(([w, r]) => `<li><code>${esc(w)}</code>${r ? ' : ' + esc(r) : ''}</li>`).join('');
    return `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(d.error || status)}
      ${items ? `<ul class="bm-paths">${items}</ul>` : ''}</div>`;
  }
  const okLine = (t) => `<div class="bm-imported">${icon('ok', { size: 14 })} ${esc(t)}</div>`;
  const varsOf = (s) => String(s || '').split(/[,\s]+/).map(v => v.trim()).filter(Boolean);

  /** Lien vers une action et son journal (#activity/action/<id>). */
  function actionLink(id) {
    return `<a class="tip dpw-action-link" href="#activity/action/${enc(id)}"
      data-tip="${esc(tr('dpw.t.actionLink'))}">${icon('logs', { size: 13 })} <code>${esc(id)}</code></a>`;
  }
  /** Texte d'étape échappé, chaque identifiant d'action (12 hexadécimaux) lié. */
  function linkIds(message) {
    return esc(message).replace(HEX_ID, (id) => `<a class="tip dpw-child" href="#activity/action/${id}"
      data-tip="${esc(tr('dpw.t.childLink'))}">${id}</a>`);
  }

  /** Constats de la console regroupés par phase, bloquants d'abord. Code
   *  inconnu : montré tel quel, échappé, avec son détail. */
  function findingsHtml(findings) {
    const list = (findings || []).filter(Boolean);
    if (!list.length) return `<p class="form-hint dpw-no-finding">${icon('ok', { size: 13 })} ${esc(tr('dpw.noFinding'))}</p>`;
    const groups = new Map();
    list.forEach(f => {
      const k = f.phase || 'plan';
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(f);
    });
    const rank = (fs) => Math.min(...fs.map(f => LEVEL_RANK[f.level] ?? 3));
    return [...groups.entries()].sort((a, b) => rank(a[1]) - rank(b[1])).map(([phase, fs]) => {
      fs.sort((a, b) => (LEVEL_RANK[a.level] ?? 3) - (LEVEL_RANK[b.level] ?? 3));
      return `<div class="dpw-group" data-phase="${esc(phase)}">
        <h4 class="dpw-group-title">${esc(text(PHASES, phase))}</h4>
        <ul class="bm-paths dpw-findings-list">${fs.map(f => `<li class="dpw-finding dpw-${esc(f.level || 'info')}" data-code="${esc(f.code)}">
          <span class="badge ${LEVEL_CLS[f.level] || 'dim'}">${esc(text(LEVELS, f.level || 'info'))}</span>
          <strong>${esc(findingText(f))}</strong>${f.detail ? ` <code class="dpw-detail">${esc(f.detail)}</code>` : ''}</li>`).join('')}</ul>
      </div>`;
    }).join('');
  }

  // =========================================================================
  // Onglet Plans
  // =========================================================================
  let plansData = { plans: [], phases: [], available: [], builtins: [] };
  let tplData = { templates: [], kinds: [] };
  let ctplData = { templates: [] };       // 2.5.0 : modèles de cluster
  let bundleProviders = null;             // fournisseurs du paquet actif (null : non lu)
  const AVAILABLE_DEFAULT = ['readiness', 'terraform', 'capi', 'clusters'];
  const MAX_CLUSTERS = 16;
  const CAPI_TIMEOUT = [1, 60, 10];
  const CLUSTERS_TIMEOUT = [5, 180, 30];
  // plan affiché : { name, revision, isNew, doc, items, clusters, shortcut }
  let edit = null;
  let lastMsg = '';

  async function loadLists() {
    const [p, t, c, b] = await Promise.all([
      api('GET', '/api/baremetal/plans').catch(() => ({ ok: false, d: {} })),
      api('GET', '/api/baremetal/tf-templates').catch(() => ({ ok: false, d: {} })),
      api('GET', '/api/baremetal/capi-templates').catch(() => ({ ok: false, d: {} })),
      api('GET', '/api/capi/bundles').catch(() => ({ ok: false, d: {} })),
    ]);
    if (p.ok) plansData = Object.assign({ plans: [], phases: [], available: [], builtins: [] }, p.d);
    if (t.ok) tplData = Object.assign({ templates: [], kinds: [] }, t.d);
    if (c.ok) ctplData = Object.assign({ templates: [] }, c.d);
    bundleProviders = b.ok && Array.isArray(b.d.active_providers) ? b.d.active_providers : null;
    return p.ok && t.ok && c.ok;
  }

  async function render() {
    const box = document.querySelector('#bm-plans');
    if (!box) return;
    const ok = await loadLists();
    if (!box.isConnected) return;
    paint(box, ok);
  }

  function paint(box, ok) {
    box = box || document.querySelector('#bm-plans');
    if (!box) return;
    const list = plansData.plans || [];
    const cards = list.map(p => `
      <button type="button" class="tfd-card bmpl-card tip ${edit && edit.name === p.name && !edit.isNew ? 'is-active' : ''}"
              data-plan="${esc(p.name)}" data-tip="${esc(tr('bmpl.t.card'))}">
        <span class="tfd-card-top"><strong>${esc(p.name)}</strong>${p.invalid
          ? `<span class="badge fail">${esc(tr('bmpl.invalid'))}</span>` : `<span class="badge dim">r${esc(p.revision)}</span>`}</span>
        ${p.description ? `<span class="tfd-card-desc">${esc(p.description)}</span>` : ''}
        <span class="tfd-card-meta">${esc((p.phases_enabled || []).map(x => text(PHASES, x)).join(' · ') || tr('bmpl.noPhase'))}</span>
      </button>`).join('');
    box.innerHTML = `
      ${ok ? '' : `<div class="summary-bar bad">${esc(tr('bmpl.loadFailed'))}</div>`}
      <div class="tfd bmpl">
        <aside class="tfd-list" aria-label="${esc(tr('bmpl.title'))}">
          <div class="tfd-list-head"><strong style="flex:1">${icon('build', { size: 16 })} ${esc(tr('bmpl.title'))}</strong>
            <button type="button" class="btn btn-primary btn-sm bmpl-new tip needs-admin" aria-label="${esc(tr('bmpl.new'))}"
                    data-tip="${esc(tr('bmpl.t.new'))}">${icon('add', { size: 14 })}</button></div>
          <div class="tfd-cards">${cards || `<p class="tfd-empty">${esc(tr('bmpl.empty'))}</p>`}</div>
          <p class="tfd-foot">${esc(tr('bmpl.foot'))}</p>
        </aside>
        <section class="tfd-main bmpl-main">${edit ? editorHtml() : `<div class="tfd-placeholder">
            <p>${esc(tr('bmpl.pick'))}</p>
            <button type="button" class="btn btn-primary btn-sm bmpl-new tip needs-admin" data-tip="${esc(tr('bmpl.t.new'))}">${icon('add', { size: 14 })} ${esc(tr('bmpl.new'))}</button>
          </div>`}</section>
      </div>
      ${templatesHtml()}
      ${capiTemplatesHtml()}`;
    if (edit) { paintTemplates(); paintClusters(); }
  }

  // --- éditeur d'un plan ---------------------------------------------------
  function blankPlan() {
    return { description: '', variables: [], phases: { readiness: { enabled: true, timeout_minutes: 20 },
      terraform: { enabled: true, templates: [] } } };
  }

  function startEdit(name, doc, revision) {
    const d = doc || blankPlan();
    edit = {
      name: name || '', revision: revision || 0, isNew: !name, doc: d,
      items: (((d.phases || {}).terraform || {}).templates || []).map(x => ({
        template: x.template, depends_on: (x.depends_on || []).slice() })),
      clusters: (((d.phases || {}).clusters || {}).items || []).map(x => ({
        template: x.template, name: x.name || '', depends_on: (x.depends_on || []).slice() })),
      shortcut: null,
    };
    lastMsg = '';
  }

  async function openPlan(name) {
    const r = await api('GET', `/api/baremetal/plans/${enc(name)}`);
    if (!r.ok) { lastMsg = refusal(r.d, r.status); paint(); return; }
    if (r.d.invalid) {
      startEdit(name, blankPlan(), 0);
      edit.invalid = true;
      lastMsg = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.invalidFile'))}</div>`;
    } else {
      startEdit(name, r.d, r.d.revision);
    }
    paint();
  }

  function editorHtml() {
    const d = edit.doc;
    const phases = plansData.phases && plansData.phases.length ? plansData.phases
      : ['readiness', 'terraform', 'capi', 'clusters', 'registry', 'artefacts'];
    const available = plansData.available && plansData.available.length ? plansData.available : AVAILABLE_DEFAULT;
    const tip = (t) => `class="tip" data-tip="${esc(t)}"`;
    return `
      <form class="capi-form bmpl-form" style="padding:14px;" autocomplete="off" novalidate>
        <div class="bmpl-msg" role="status">${lastMsg}</div>
        <fieldset>
          <legend>${esc(tr('bmpl.fs.plan'))}</legend>
          <label ${tip(tr('bmpl.t.name'))}>${esc(tr('bmpl.f.name'))} *
            <input name="name" required pattern="${NAME_PATTERN}" maxlength="63" value="${esc(edit.name)}"${edit.isNew ? '' : ' readonly'}></label>
          <label ${tip(tr('bmpl.t.description'))}>${esc(tr('bmpl.f.description'))}
            <input name="description" maxlength="500" value="${esc(d.description || '')}"></label>
          <label style="grid-column:1/-1;" ${tip(tr('bmpl.t.variables'))}>${esc(tr('bmpl.f.variables'))}
            <input name="variables" placeholder="vlan, replicas" value="${esc((d.variables || []).join(', '))}"></label>
          <p class="form-hint" style="grid-column:1/-1;">${esc(tr('bmpl.builtins'))}
            ${(plansData.builtins && plansData.builtins.length ? plansData.builtins : ['cluster', 'vip']).map(v => `<code>{{${esc(v)}}}</code>`).join(' ')}</p>
        </fieldset>
        ${phases.map(pid => phaseCard(pid, available.includes(pid))).join('')}
        <div class="apply-bar bmpl-actions">
          <button type="submit" class="btn btn-primary btn-sm btn-ico tip needs-admin bmpl-save"
                  data-tip="${esc(tr('bmpl.t.save'))}">${icon('save')} ${esc(tr('bmpl.save'))}</button>
          ${edit.isNew ? '' : `<button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-apply"
                  data-tip="${esc(tr('bmpl.t.apply'))}">${icon('play')} ${esc(tr('bmpl.apply'))}</button>
          <button type="button" class="btn btn-danger btn-sm btn-ico tip needs-admin bmpl-delete"
                  data-tip="${esc(tr('bmpl.t.delete'))}">${icon('trash')} ${esc(tr('bmpl.delete'))}</button>
          <span class="form-hint">${esc(tr('bmpl.revision', { n: edit.revision }))}</span>`}
        </div>
      </form>`;
  }

  function phaseCard(pid, available) {
    const ph = (edit.doc.phases || {})[pid];
    const on = available && !!(ph && ph.enabled);
    const label = text(PHASES, pid);
    const comingTip = tr('bmpl.t.coming');
    let body = '';
    if (pid === 'readiness') {
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.timeout'))}">${esc(tr('bmpl.f.timeout'))}
          <input type="number" name="timeout_minutes" min="1" max="180" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : 20)}"></label>`;
    } else if (pid === 'terraform') {
      body = `<ol class="bmpl-tpls" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <select class="bmpl-add-tpl tip" aria-label="${esc(tr('bmpl.addTemplate'))}" data-tip="${esc(tr('bmpl.t.pickTemplate'))}"></select>
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-add-tpl-go"
                  data-tip="${esc(tr('bmpl.t.addTemplate'))}">${icon('add')} ${esc(tr('bmpl.addTemplate'))}</button>
        </div>`;
    } else if (pid === 'capi') {
      const provs = bundleProviders;
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.capiTimeout'))}">${esc(tr('bmpl.f.capiTimeout'))}
          <input type="number" name="capi_timeout" min="${CAPI_TIMEOUT[0]}" max="${CAPI_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : CAPI_TIMEOUT[2])}"></label>
        <div class="bmpl-capi-versions tip" style="grid-column:1/-1;" data-tip="${esc(tr('bmpl.t.capiNoUpgrade'))}">
          <span class="form-hint">${esc(tr('bmpl.capiBundle'))}</span>
          ${provs && provs.length ? provs.map(v => `<code class="bmpl-capi-provider">${esc(v)}</code>`).join(' ')
            : `<span class="badge warn">${esc(tr('bmpl.capiNoBundle'))}</span>`}
          <p class="form-hint">${icon('info', { size: 12 })} ${esc(tr('bmpl.t.capiNoUpgrade'))}</p></div>`;
    } else if (pid === 'clusters') {
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.clustersTimeout'))}">${esc(tr('bmpl.f.clustersTimeout'))}
          <input type="number" name="clusters_timeout" min="${CLUSTERS_TIMEOUT[0]}" max="${CLUSTERS_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : CLUSTERS_TIMEOUT[2])}"></label>
        <p class="form-hint" style="grid-column:1/-1;">${esc(tr('bmpl.clustersHint', { max: MAX_CLUSTERS }))}</p>
        <ol class="bmpl-clusters" style="grid-column:1/-1;"></ol>
        <div class="bmpl-cl-pending" style="grid-column:1/-1;"></div>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-cl-add"
                  data-tip="${esc(tr('bmpl.t.clusterAdd'))}">${icon('add')} ${esc(tr('bmpl.clusterAdd'))}</button>
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-cl-several"
                  data-tip="${esc(tr('bmpl.t.clusterSeveral'))}">${icon('add')} ${esc(tr('bmpl.clusterSeveral'))}</button>
        </div>
        <fieldset class="bmpl-cl-shortcut" style="grid-column:1/-1;" hidden>
          <legend>${esc(tr('bmpl.clusterSeveral'))}</legend>
          <label class="tip" data-tip="${esc(tr('bmpl.t.clusterTemplate'))}">${esc(tr('bmpl.f.clusterTemplate'))}
            <select class="bmpl-sc-tpl"></select></label>
          <label class="tip" data-tip="${esc(tr('bmpl.t.clusterCount'))}">${esc(tr('bmpl.f.clusterCount'))}
            <input type="number" class="bmpl-sc-count" min="1" max="${MAX_CLUSTERS}" step="1" value="3"></label>
          <label class="tip" style="grid-column:1/-1;" data-tip="${esc(tr('bmpl.t.clusterPattern'))}">${esc(tr('bmpl.f.clusterPattern'))}
            <input class="bmpl-sc-pattern" spellcheck="false" value="{{cluster}}-k8s-{{n}}"></label>
          <div class="apply-bar" style="grid-column:1/-1;">
            <button type="button" class="btn btn-primary btn-sm tip bmpl-sc-ok" data-tip="${esc(tr('bmpl.t.clusterSeveralOk'))}">${esc(tr('bmpl.clusterSeveralOk'))}</button>
            <span class="bmpl-sc-msg form-hint" role="status"></span>
          </div>
        </fieldset>`;
    }
    // une phase = un cadre du formulaire, son interrupteur dans la légende
    return `
      <fieldset class="bmpl-phase${available ? '' : ' bmpl-phase-coming'}" data-phase="${esc(pid)}">
        <legend><label class="bm-check tip" data-tip="${esc(available ? text(PHASE_TIPS, pid) : comingTip)}">
          <input type="checkbox" class="bmpl-phase-on" data-phase="${esc(pid)}" ${on ? 'checked' : ''}${available ? '' : ' disabled'}
                 aria-label="${esc(label)}"> <strong>${esc(label)}</strong></label></legend>
        <p class="form-hint" style="grid-column:1/-1;margin:0;">${esc(text(PHASE_TIPS, pid))}
          ${available ? '' : ` <span class="badge dim tip bmpl-coming" data-tip="${esc(comingTip)}">${esc(tr('bmpl.coming'))}</span>`}</p>
        ${available ? body : ''}
      </fieldset>`;
  }

  // Modèles de la phase Terraform, dans l'ordre d'exécution
  function paintTemplates() {
    const ol = document.querySelector('#bm-plans .bmpl-tpls');
    if (!ol) return;
    const known = new Set((tplData.templates || []).map(t => t.name));
    const items = edit.items;
    ol.innerHTML = items.length ? items.map((it, i) => {
      const before = items.slice(0, i).map(x => x.template);
      const deps = before.length ? `<span class="bmpl-deps tip" data-tip="${esc(tr('bmpl.t.dependsOn'))}">${esc(tr('bmpl.dependsOn'))}
          ${before.map(b => `<label class="bm-check"><input type="checkbox" class="bmpl-dep" data-i="${i}" value="${esc(b)}"
            ${it.depends_on.includes(b) ? 'checked' : ''}> <code>${esc(b)}</code></label>`).join(' ')}</span>` : '';
      return `<li class="bmpl-tpl" data-tpl="${esc(it.template)}">
        <code class="bmpl-tpl-name">${esc(it.template)}</code>
        ${known.has(it.template) ? '' : `<span class="badge fail tip" data-tip="${esc(tr('bmpl.t.tplMissing'))}">${esc(tr('bmpl.tplMissing'))}</span>`}
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-up" data-i="${i}" ${i === 0 ? 'disabled' : ''}
                aria-label="${esc(tr('bmpl.t.up'))}" data-tip="${esc(tr('bmpl.t.up'))}">${icon('arrowUp', { size: 12 })}</button>
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-down" data-i="${i}" ${i === items.length - 1 ? 'disabled' : ''}
                aria-label="${esc(tr('bmpl.t.down'))}" data-tip="${esc(tr('bmpl.t.down'))}">${icon('arrowDown', { size: 12 })}</button>
        <button type="button" class="btn btn-sm btn-danger tip bmpl-rm" data-i="${i}"
                aria-label="${esc(tr('bmpl.t.removeTpl'))}" data-tip="${esc(tr('bmpl.t.removeTpl'))}">${icon('close', { size: 12 })}</button>
        ${deps}
      </li>`;
    }).join('') : `<li class="form-hint bmpl-no-tpl">${esc(tr('bmpl.noTemplate'))}</li>`;
    const sel = document.querySelector('#bm-plans .bmpl-add-tpl');
    if (sel) {
      const used = new Set(items.map(x => x.template));
      const free = (tplData.templates || []).filter(t => !t.invalid && !used.has(t.name));
      sel.innerHTML = free.length ? free.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('')
        : `<option value="">${esc(tr('bmpl.noFreeTemplate'))}</option>`;
      sel.disabled = !free.length;
    }
  }

  // ordre changé ou modèle retiré : une dépendance doit rester avant
  function fixDeps() {
    edit.items.forEach((it, i) => {
      const before = edit.items.slice(0, i).map(x => x.template);
      it.depends_on = it.depends_on.filter(d => before.includes(d));
    });
    edit.clusters.forEach((it, i) => {
      const before = edit.clusters.slice(0, i).map(x => x.name);
      it.depends_on = it.depends_on.filter(d => before.includes(d));
    });
  }

  // 2.5.0 : clusters enfants, dans l'ordre de création (un à la fois)
  function paintClusters() {
    const ol = document.querySelector('#bm-plans .bmpl-clusters');
    if (!ol) return;
    const tpls = (ctplData.templates || []).filter(t => !t.invalid);
    const known = new Set(tpls.map(t => t.name));
    const items = edit.clusters;
    const tplOpts = (sel) => (sel && !known.has(sel) ? `<option value="${esc(sel)}" selected>${esc(sel)} (${esc(tr('bmpl.tplMissing'))})</option>` : '')
      + tpls.map(t => `<option value="${esc(t.name)}"${t.name === sel ? ' selected' : ''}>${esc(t.name)}</option>`).join('');
    ol.innerHTML = items.length ? items.map((it, i) => {
      const before = items.slice(0, i).map(x => x.name).filter(Boolean);
      const deps = before.length ? `<span class="bmpl-deps tip" data-tip="${esc(tr('bmpl.t.clusterDependsOn'))}">${esc(tr('bmpl.dependsOn'))}
          ${before.map(b => `<label class="bm-check"><input type="checkbox" class="bmpl-cl-dep" data-i="${i}" value="${esc(b)}"
            ${it.depends_on.includes(b) ? 'checked' : ''}> <code>${esc(b)}</code></label>`).join(' ')}</span>` : '';
      return `<li class="bmpl-cl" data-i="${i}">
        <select class="bmpl-cl-tpl tip" data-i="${i}" aria-label="${esc(tr('bmpl.f.clusterTemplate'))}"
                data-tip="${esc(tr('bmpl.t.clusterTemplate'))}">${tplOpts(it.template)}</select>
        <input class="bmpl-cl-name tip" data-i="${i}" spellcheck="false" maxlength="80" value="${esc(it.name)}"
               placeholder="{{cluster}}-apps" aria-label="${esc(tr('bmpl.f.clusterName'))}" data-tip="${esc(tr('bmpl.t.clusterName'))}">
        ${it.template && !known.has(it.template) ? `<span class="badge fail tip" data-tip="${esc(tr('bmpl.t.ctplMissing'))}">${esc(tr('bmpl.tplMissing'))}</span>` : ''}
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-cl-up" data-i="${i}" ${i === 0 ? 'disabled' : ''}
                aria-label="${esc(tr('bmpl.t.up'))}" data-tip="${esc(tr('bmpl.t.up'))}">${icon('arrowUp', { size: 12 })}</button>
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-cl-down" data-i="${i}" ${i === items.length - 1 ? 'disabled' : ''}
                aria-label="${esc(tr('bmpl.t.down'))}" data-tip="${esc(tr('bmpl.t.down'))}">${icon('arrowDown', { size: 12 })}</button>
        <button type="button" class="btn btn-sm btn-danger tip bmpl-cl-rm" data-i="${i}"
                aria-label="${esc(tr('bmpl.t.clusterRemove'))}" data-tip="${esc(tr('bmpl.t.clusterRemove'))}">${icon('close', { size: 12 })}</button>
        ${deps}
      </li>`;
    }).join('') : `<li class="form-hint bmpl-no-cluster">${esc(tr('bmpl.noCluster'))}</li>`;
    const sc = edit.shortcut;
    const pend = document.querySelector('#bm-plans .bmpl-cl-pending');
    if (pend) {
      pend.innerHTML = sc ? `<div class="bm-imported bmpl-sc-pending">${icon('info', { size: 13 })}
        ${esc(tr('bmpl.clusterSeveralPending', { n: sc.count, pattern: sc.name_pattern, template: sc.template }))}
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-sc-clear" aria-label="${esc(tr('bmpl.t.clusterSeveralClear'))}"
                data-tip="${esc(tr('bmpl.t.clusterSeveralClear'))}">${icon('close', { size: 12 })}</button></div>` : '';
    }
    const scSel = document.querySelector('#bm-plans .bmpl-sc-tpl');
    if (scSel) scSel.innerHTML = tpls.length ? tplOpts(sc ? sc.template : '') : `<option value="">${esc(tr('bmpl.noClusterTemplate'))}</option>`;
    const add = document.querySelector('#bm-plans .bmpl-cl-add');
    const several = document.querySelector('#bm-plans .bmpl-cl-several');
    const full = items.length + (sc ? sc.count : 0) >= MAX_CLUSTERS;
    [add, several].forEach(b => { if (b) b.disabled = full || !tpls.length; });
  }

  // Raccourci « Plusieurs » : le serveur le développe à l'enregistrement
  // (après les entrées déjà listées) ; ici, seulement les refus évidents
  function takeShortcut(box) {
    const msg = box.querySelector('.bmpl-sc-msg');
    const template = box.querySelector('.bmpl-sc-tpl').value;
    const count = Number(box.querySelector('.bmpl-sc-count').value);
    const pattern = box.querySelector('.bmpl-sc-pattern').value.trim();
    if (!template) { msg.textContent = tr('bmpl.noClusterTemplate'); return; }
    if (!Number.isInteger(count) || count < 1 || edit.clusters.length + count > MAX_CLUSTERS) {
      msg.textContent = tr('bmpl.clusterCountBad', { max: MAX_CLUSTERS - edit.clusters.length });
      return;
    }
    if (!pattern.includes('{{n}}')) { msg.textContent = tr('bmpl.clusterPatternBad'); return; }
    edit.shortcut = { template, count, name_pattern: pattern };
    msg.textContent = '';
    box.hidden = true;
    paintClusters();
  }

  function collect(form) {
    const phases = {};
    const available = plansData.available && plansData.available.length ? plansData.available : AVAILABLE_DEFAULT;
    const on = (pid) => !!form.querySelector(`.bmpl-phase-on[data-phase="${pid}"]:checked`);
    available.forEach(pid => {
      if (pid === 'readiness') {
        const n = Number(form.querySelector('[name="timeout_minutes"]').value);
        phases.readiness = { enabled: on('readiness'), timeout_minutes: n };
      } else if (pid === 'terraform') {
        phases.terraform = { enabled: on('terraform'), templates: edit.items.map(it => (it.depends_on.length
          ? { template: it.template, depends_on: it.depends_on.slice() } : { template: it.template })) };
      } else if (pid === 'capi') {
        phases.capi = { enabled: on('capi'), timeout_minutes: Number(form.querySelector('[name="capi_timeout"]').value) };
      } else if (pid === 'clusters') {
        phases.clusters = { enabled: on('clusters'), timeout_minutes: Number(form.querySelector('[name="clusters_timeout"]').value),
          items: edit.clusters.map(it => Object.assign({ template: it.template, name: it.name.trim() },
            it.depends_on.length ? { depends_on: it.depends_on.slice() } : {})) };
        if (edit.shortcut) Object.assign(phases.clusters, edit.shortcut);
      } else {
        phases[pid] = { enabled: on(pid) };
      }
    });
    // phases de 2.5+ : gardées telles que lues (toujours désactivées)
    Object.entries(edit.doc.phases || {}).forEach(([pid, ph]) => {
      if (!available.includes(pid)) phases[pid] = Object.assign({}, ph, { enabled: false });
    });
    return { description: form.description.value, variables: varsOf(form.variables.value), phases };
  }

  async function save(form) {
    const msg = form.querySelector('.bmpl-msg');
    const name = form.name.value.trim();
    if (!new RegExp(`^${NAME_PATTERN}$`).test(name)) {
      msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badName'))}</div>`;
      return;
    }
    const t = Number(form.querySelector('[name="timeout_minutes"]') ? form.querySelector('[name="timeout_minutes"]').value : 20);
    if (!Number.isInteger(t) || t < 1 || t > 180) {
      msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badTimeout'))}</div>`;
      return;
    }
    // 2.5.0 : délais des phases Cluster API et Clusters, bornes du serveur
    for (const [field, [lo, hi]] of [['capi_timeout', CAPI_TIMEOUT], ['clusters_timeout', CLUSTERS_TIMEOUT]]) {
      const el = form.querySelector(`[name="${field}"]`);
      const v = el ? Number(el.value) : lo;
      if (!Number.isInteger(v) || v < lo || v > hi) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badTimeoutRange', { min: lo, max: hi }))}</div>`;
        return;
      }
    }
    const body = collect(form);
    if (!edit.isNew) body.revision = edit.revision;
    const r = await api('PUT', `/api/baremetal/plans/${enc(name)}`, body);
    if (r.status === 409) {
      // quelqu'un d'autre a écrit entre-temps : la saisie reste dans le formulaire
      msg.innerHTML = `<div class="bm-refusal bmpl-conflict">${icon('warn', { size: 14 })} ${esc(r.d.current_revision == null
        ? tr('bmpl.conflictUnreadable') : tr('bmpl.conflict', { n: r.d.current_revision }))}
        <button type="button" class="btn btn-sm btn-secondary tip bmpl-reload" data-tip="${esc(tr('bmpl.t.reload'))}">${icon('refresh', { size: 12 })} ${esc(tr('bmpl.reload'))}</button></div>`;
      msg.scrollIntoView({ block: 'nearest' });
      return;
    }
    if (!r.ok) { msg.innerHTML = refusal(r.d, r.status); msg.scrollIntoView({ block: 'nearest' }); return; }
    startEdit(name, r.d, r.d.revision);
    // clés libres d'une ancienne phase désactivée retirées : dit, pas tu
    const warns = Array.isArray(r.d.warnings) ? r.d.warnings : [];
    lastMsg = okLine(tr('bmpl.saved', { n: r.d.revision })) + (warns.length
      ? `<div class="bm-refusal bm-warning bmpl-warnings">${icon('warn', { size: 14 })} ${esc(tr('bmpl.saveWarnings'))}
          <ul class="bm-paths">${warns.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div>` : '');
    await loadLists();
    paint();
  }

  async function removePlan(name) {
    if (!confirm(tr('bmpl.confirmDelete', { name }))) return;
    const r = await api('DELETE', `/api/baremetal/plans/${enc(name)}`);
    if (!r.ok && r.status !== 404) {
      lastMsg = r.d.code === 'deploy-running'
        ? `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.deleteRunning'))} ${actionLink(r.d.running)}</div>`
        : refusal(r.d, r.status);
      paint();
      return;
    }
    edit = null;
    lastMsg = '';
    await render();
  }

  // --- modèles de déclaration ----------------------------------------------
  function kindLabel(kind) {
    const s = (window.TF_SCHEMA || {})[kind];
    return s ? st(s.label) : kind;
  }

  function templatesHtml() {
    const rows = (tplData.templates || []).map(t => `
      <tr data-template="${esc(t.name)}">
        <td><code class="bmpl-tpl-row-name">${esc(t.name)}</code>${t.invalid ? ` <span class="badge fail">${esc(tr('bmpl.invalid'))}</span>` : ''}</td>
        <td>${esc(t.description || '')}</td>
        <td>${(t.kinds || []).map(k => esc(kindLabel(k))).join(', ')}</td>
        <td>${(t.variables || []).map(v => `<code>{{${esc(v)}}}</code>`).join(' ')}</td>
        <td class="bm-profile-actions">
          <button type="button" class="btn btn-sm btn-secondary tip bmpl-tpl-edit" data-name="${esc(t.name)}"
                  aria-label="${esc(tr('bmpl.t.tplEdit'))}" data-tip="${esc(tr('bmpl.t.tplEdit'))}">${icon('edit')}</button>
          <button type="button" class="btn btn-sm btn-danger tip needs-admin bmpl-tpl-del" data-name="${esc(t.name)}"
                  aria-label="${esc(tr('bmpl.t.tplDelete'))}" data-tip="${esc(tr('bmpl.t.tplDelete'))}">${icon('trash')}</button>
        </td>
      </tr>`).join('') || `<tr><td colspan="5" class="empty-state">${esc(tr('bmpl.tplEmpty'))}</td></tr>`;
    return `
      <div class="card bmpl-templates" style="margin-top:14px;">
        <div class="card-header"><h2>${icon('notes', { size: 18 })} ${esc(tr('bmpl.tplTitle'))}</h2>
          <button type="button" class="btn btn-sm btn-primary btn-ico tip needs-admin bmpl-tpl-new"
                  data-tip="${esc(tr('bmpl.t.tplNew'))}">${icon('add')} ${esc(tr('bmpl.tplNew'))}</button>
        </div>
        <div class="card-body">
          <p class="form-hint">${esc(tr('bmpl.tplHint', { v: '{{name}}' }))}</p>
          <table class="data-table" id="bmpl-tpl-table">
            <thead><tr><th>${esc(tr('bmpl.f.name'))}</th><th>${esc(tr('bmpl.f.description'))}</th>
              <th>${esc(tr('bmpl.col.kinds'))}</th><th>${esc(tr('bmpl.f.variables'))}</th><th>${esc(tr('col.actions'))}</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }

  // Un modèle porte des variables `{{nom}}` partout : un nombre ou une
  // référence doivent pouvoir les recevoir. Les champs numériques passent
  // en texte, les listes de références en saisie libre.
  function openFields(root) {
    root.querySelectorAll('input[type="number"]').forEach(inp => {
      inp.type = 'text';
      inp.setAttribute('inputmode', 'numeric');
      inp.removeAttribute('min');
      inp.removeAttribute('max');
      inp.dataset.bmplInt = '1';
    });
    root.querySelectorAll('select[data-ref-endpoint], select[data-ref-url]').forEach(sel => {
      if (sel.multiple) return;
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.name = sel.name;
      inp.id = sel.id;
      inp.value = sel.dataset.refSelected || '';
      inp.className = 'bmpl-ref';
      inp.setAttribute('spellcheck', 'false');
      if (sel.required) inp.required = true;
      sel.replaceWith(inp);
    });
  }

  // Lecture d'une ressource : TFForm.read, les nombres qui portent une
  // variable gardés en texte (marqueur numérique le temps de la lecture)
  function readResource(root, kind) {
    const marks = new Map();
    const touched = [];
    let k = 0;
    root.querySelectorAll('[data-bmpl-int]').forEach(inp => {
      if (/^-?\d+$/.test(inp.value.trim()) || !inp.value.trim()) return;
      const mark = 900000000 + (k++);
      marks.set(mark, inp.value.trim());
      touched.push([inp, inp.value]);
      inp.value = String(mark);
    });
    const spec = TFForm.read(root, kind) || {};
    touched.forEach(([inp, v]) => { inp.value = v; });
    const walk = (o) => {
      if (Array.isArray(o)) return o.map(walk);
      if (o && typeof o === 'object') { Object.keys(o).forEach(x => { o[x] = walk(o[x]); }); return o; }
      return typeof o === 'number' && marks.has(o) ? marks.get(o) : o;
    };
    return walk(spec);
  }

  async function openTemplate(name) {
    let t = { name: '', description: '', variables: [], resources: [], removed: [], revision: 0 };
    if (name) {
      const r = await api('GET', `/api/baremetal/tf-templates/${enc(name)}`);
      if (!r.ok) { alert(r.d.error || r.status); return; }
      if (r.d.invalid) { alert(tr('bmpl.invalidFile')); return; }
      t = r.d;
    }
    const kinds = (tplData.kinds || []).filter(k => (window.TF_SCHEMA || {})[k]);
    const tip = (x) => `class="tip" data-tip="${esc(x)}"`;
    const panel = FloatingPanels.open({
      id: `bm-tpl-${name || 'new'}`,
      title: name ? `${tr('bmpl.tplEditTitle')} · ${name}` : tr('bmpl.tplNewTitle'),
      width: Math.min(860, window.innerWidth - 160), height: Math.max(480, Math.min(840, window.innerHeight - 80)),
      bodyHtml: `
        <form class="capi-form bmpl-tpl-form" style="padding:14px;" autocomplete="off" novalidate>
          <p class="form-hint">${esc(tr('bmpl.tplEditorHint', { v: '{{name}}' }))}</p>
          <div class="bmpl-tpl-msg" role="status"></div>
          <fieldset>
            <legend>${esc(tr('bmpl.fs.template'))}</legend>
            <label ${tip(tr('bmpl.t.tplName'))}>${esc(tr('bmpl.f.name'))} *
              <input name="name" data-bmpl="name" required pattern="${NAME_PATTERN}" maxlength="63" value="${esc(t.name)}"${name ? ' readonly' : ''}></label>
            <label ${tip(tr('bmpl.t.description'))}>${esc(tr('bmpl.f.description'))}
              <input name="description" data-bmpl="description" maxlength="500" value="${esc(t.description || '')}"></label>
            <label style="grid-column:1/-1;" ${tip(tr('bmpl.t.tplVariables'))}>${esc(tr('bmpl.f.variables'))}
              <input name="variables" data-bmpl="variables" placeholder="vlan, replicas" value="${esc((t.variables || []).join(', '))}"></label>
          </fieldset>
          <div class="bmpl-res-list"></div>
          <div class="apply-bar">
            <select class="bmpl-add-kind tip" aria-label="${esc(tr('bmpl.addResource'))}" data-tip="${esc(tr('bmpl.t.kind'))}">
              ${kinds.map(k => `<option value="${esc(k)}">${esc(kindLabel(k))}</option>`).join('')}</select>
            <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-add-res"
                    data-tip="${esc(tr('bmpl.t.addResource'))}">${icon('add')} ${esc(tr('bmpl.addResource'))}</button>
          </div>
          <div class="apply-bar">
            <button type="submit" class="btn btn-primary btn-sm btn-ico tip needs-admin"
                    data-tip="${esc(tr('bmpl.t.tplSave'))}">${icon('save')} ${esc(tr('bmpl.save'))}</button>
            <span class="form-hint bmpl-tpl-rev">${name ? esc(tr('bmpl.revision', { n: t.revision })) : ''}</span>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.bmpl-tpl-form');
    if (form.dataset.bound) return;
    form.dataset.bound = '1';
    const msg = form.querySelector('.bmpl-tpl-msg');
    // champs du modèle par data-bmpl : `form.name` désignerait aussi les
    // champs « name » des ressources Terraform
    const own = (k) => form.querySelector(`[data-bmpl="${k}"]`);
    const listBox = form.querySelector('.bmpl-res-list');
    let resources = (t.resources || []).map(r => ({ id: r.id, kind: r.kind, spec: JSON.parse(JSON.stringify(r.spec || {})) }));
    let revision = t.revision || 0;
    let saved = !!name;
    if ((t.removed || []).length) msg.innerHTML = removedHtml(t.removed, true);

    function readAll() {
      listBox.querySelectorAll('.bmpl-res').forEach(el => {
        const r = resources.find(x => x.id === el.dataset.resId);
        const root = el.querySelector('.tf-form');
        if (r && root) r.spec = readResource(root, r.kind);
      });
    }
    function paintRes() {
      listBox.innerHTML = resources.length ? resources.map(r => `
        <fieldset class="bmpl-res" data-res-id="${esc(r.id)}">
          <legend>${esc(kindLabel(r.kind))}
            <button type="button" class="btn btn-sm btn-danger tip bmpl-rm-res" aria-label="${esc(tr('bmpl.t.removeRes'))}"
                    data-tip="${esc(tr('bmpl.t.removeRes'))}">${icon('close', { size: 12 })}</button></legend>
          <div style="grid-column:1/-1;">${TFForm.render(r.kind, '', r.spec, { hideHeader: true })}</div>
        </fieldset>`).join('') : `<p class="form-hint bmpl-no-res">${esc(tr('bmpl.noResource'))}</p>`;
      listBox.querySelectorAll('.bmpl-res').forEach(el => {
        const r = resources.find(x => x.id === el.dataset.resId);
        const root = el.querySelector('.tf-form');
        if (!r || !root) return;
        openFields(root);
        TFForm.wire(root, r.kind, '');
      });
    }
    paintRes();
    form.addEventListener('click', (e) => {
      if (e.target.closest('.bmpl-add-res')) {
        const kind = form.querySelector('.bmpl-add-kind').value;
        if (!kind) return;
        readAll();
        const id = Math.random().toString(16).slice(2, 14).padEnd(12, '0');
        resources.push({ id, kind, spec: {} });
        paintRes();
        return;
      }
      const rm = e.target.closest('.bmpl-rm-res');
      if (rm) {
        readAll();
        const id = rm.closest('.bmpl-res').dataset.resId;
        resources = resources.filter(x => x.id !== id);
        paintRes();
      }
    });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const tname = own('name').value.trim();
      if (!new RegExp(`^${NAME_PATTERN}$`).test(tname)) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badName'))}</div>`;
        return;
      }
      readAll();
      const body = { description: own('description').value, variables: varsOf(own('variables').value),
        resources: resources.map(r => ({ id: r.id, kind: r.kind, spec: r.spec })) };
      if (saved) body.revision = revision;
      const r = await api('PUT', `/api/baremetal/tf-templates/${enc(tname)}`, body);
      if (r.status === 409) {
        msg.innerHTML = `<div class="bm-refusal bmpl-conflict">${icon('warn', { size: 14 })} ${esc(r.d.current_revision == null
          ? tr('bmpl.conflictUnreadable') : tr('bmpl.tplConflict', { n: r.d.current_revision }))}</div>`;
        return;
      }
      if (!r.ok) { msg.innerHTML = refusal(r.d, r.status); msg.scrollIntoView({ block: 'nearest' }); return; }
      revision = r.d.revision;
      saved = true;
      own('name').readOnly = true;
      form.querySelector('.bmpl-tpl-rev').textContent = tr('bmpl.revision', { n: revision });
      // ce que la console a retiré (secrets) est dit, chemin par chemin
      resources = (r.d.resources || []).map(x => ({ id: x.id, kind: x.kind, spec: x.spec || {} }));
      paintRes();
      msg.innerHTML = okLine(tr('bmpl.tplSaved', { n: revision })) + ((r.d.removed || []).length ? removedHtml(r.d.removed, false) : '');
      await loadLists();
      paint();
    });
  }

  function removedHtml(paths, before) {
    return `<div class="bm-refusal bm-warning bmpl-removed">${icon('warn', { size: 14 })} ${esc(before
      ? tr('bmpl.removedBefore') : tr('bmpl.removedNow'))}
      <ul class="bm-paths">${paths.map(p => `<li><code>${esc(p)}</code></li>`).join('')}</ul></div>`;
  }

  async function removeTemplate(name) {
    if (!confirm(tr('bmpl.confirmTplDelete', { name }))) return;
    const r = await api('DELETE', `/api/baremetal/tf-templates/${enc(name)}`);
    if (!r.ok && r.status !== 404) alert(r.d.error || r.status);
    await render();
  }

  // --- modèles de cluster (2.5.0) -----------------------------------------
  function capiTemplatesHtml() {
    const rows = (ctplData.templates || []).map(t => `
      <tr data-ctemplate="${esc(t.name)}">
        <td><code class="bmpl-ctpl-row-name">${esc(t.name)}</code>${t.invalid ? ` <span class="badge fail">${esc(tr('bmpl.invalid'))}</span>` : ''}</td>
        <td>${esc(t.description || '')}</td>
        <td>${(t.variables || []).map(v => `<code>{{${esc(v)}}}</code>`).join(' ')}</td>
        <td>${t.vms == null ? '' : esc(t.vms)}</td>
        <td class="bm-profile-actions">
          <button type="button" class="btn btn-sm btn-secondary tip bmpl-ctpl-edit" data-name="${esc(t.name)}"
                  aria-label="${esc(tr('bmpl.t.ctplEdit'))}" data-tip="${esc(tr('bmpl.t.ctplEdit'))}">${icon('edit')}</button>
          <button type="button" class="btn btn-sm btn-danger tip needs-admin bmpl-ctpl-del" data-name="${esc(t.name)}"
                  aria-label="${esc(tr('bmpl.t.ctplDelete'))}" data-tip="${esc(tr('bmpl.t.ctplDelete'))}">${icon('trash')}</button>
        </td>
      </tr>`).join('') || `<tr><td colspan="5" class="empty-state">${esc(tr('bmpl.ctplEmpty'))}</td></tr>`;
    return `
      <div class="card bmpl-capi-templates" style="margin-top:14px;">
        <div class="card-header"><h2>${icon('capi', { size: 18 })} ${esc(tr('bmpl.ctplTitle'))}</h2>
          <button type="button" class="btn btn-sm btn-primary btn-ico tip needs-admin bmpl-ctpl-new"
                  data-tip="${esc(tr('bmpl.t.ctplNew'))}">${icon('add')} ${esc(tr('bmpl.ctplNew'))}</button>
        </div>
        <div class="card-body">
          <p class="form-hint">${esc(tr('bmpl.ctplHint', { v: '{{name}}' }))}</p>
          <table class="data-table" id="bmpl-ctpl-table">
            <thead><tr><th>${esc(tr('bmpl.f.name'))}</th><th>${esc(tr('bmpl.f.description'))}</th>
              <th>${esc(tr('bmpl.f.variables'))}</th><th>${esc(tr('bmpl.col.vms'))}</th><th>${esc(tr('col.actions'))}</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }

  /** Éditeur d'un modèle de cluster : le formulaire de création en mode
   *  modèle (décision 173). `prefill` = { spec, cluster } : la demande
   *  courante de la fenêtre de création (« Enregistrer comme modèle »). */
  async function openCapiTemplate(name, prefill) {
    let t = { name: '', description: '', variables: [], spec: (prefill && prefill.spec) || {}, revision: 0 };
    if (name) {
      const r = await api('GET', `/api/baremetal/capi-templates/${enc(name)}`);
      if (!r.ok) { alert(r.d.error || r.status); return null; }
      if (r.d.invalid) { alert(tr('bmpl.invalidFile')); return null; }
      t = r.d;
    }
    const id = `bm-ctpl-${name || 'new'}`;
    if (prefill && document.getElementById(`fp-${id}`)) FloatingPanels.close(id);
    const clusters = (window.App && App.listClusters ? App.listClusters() : []).slice();
    let cluster = (prefill && prefill.cluster) || (window.App && App.getCurrentCluster ? App.getCurrentCluster() : '') || clusters[0] || '';
    if (cluster && !clusters.includes(cluster)) clusters.unshift(cluster);
    const tip = (x) => `class="tip" data-tip="${esc(x)}"`;
    const panel = FloatingPanels.open({
      id, icon: 'capi',
      title: name ? `${tr('bmpl.ctplEditTitle')} · ${name}` : tr('bmpl.ctplNewTitle'),
      width: Math.min(1080, window.innerWidth - 120), height: Math.max(520, Math.min(860, window.innerHeight - 60)),
      bodyHtml: `
        <form class="capi-form bmpl-ctpl-form" style="padding:14px;" autocomplete="off" novalidate>
          <p class="form-hint">${esc(tr('bmpl.ctplEditorHint', { v: '{{name}}' }))}</p>
          <div class="bmpl-ctpl-msg" role="status"></div>
          <fieldset>
            <legend>${esc(tr('bmpl.fs.ctemplate'))}</legend>
            <label ${tip(tr('bmpl.t.ctplName'))}>${esc(tr('bmpl.f.name'))} *
              <input data-ctpl="name" required pattern="${NAME_PATTERN}" maxlength="63" value="${esc(t.name)}"${name ? ' readonly' : ''}></label>
            <label ${tip(tr('bmpl.t.description'))}>${esc(tr('bmpl.f.description'))}
              <input data-ctpl="description" maxlength="500" value="${esc(t.description || '')}"></label>
            <label ${tip(tr('bmpl.t.ctplVariables'))}>${esc(tr('bmpl.f.variables'))}
              <input data-ctpl="variables" placeholder="vlan, image" value="${esc((t.variables || []).join(', '))}"></label>
            <label ${tip(tr('bmpl.t.ctplCluster'))}>${esc(tr('bmpl.f.ctplCluster'))}
              <select data-ctpl="cluster">${clusters.length ? clusters.map(c => `<option value="${esc(c)}"${c === cluster ? ' selected' : ''}>${esc(c)}</option>`).join('')
                : `<option value="">${esc(tr('bmpl.ctplNoCluster'))}</option>`}</select></label>
          </fieldset>
          <div class="bmpl-ctpl-note"></div>
          <div class="bmpl-ctpl-host"></div>
          <div class="apply-bar">
            <button type="submit" class="btn btn-primary btn-sm btn-ico tip needs-admin bmpl-ctpl-save"
                    data-tip="${esc(tr('bmpl.t.ctplSave'))}">${icon('save')} ${esc(tr('bmpl.save'))}</button>
            <span class="form-hint bmpl-ctpl-rev">${name ? esc(tr('bmpl.revision', { n: t.revision })) : ''}</span>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.bmpl-ctpl-form');
    if (form.dataset.bound) return panel;
    form.dataset.bound = '1';
    const own = (k) => form.querySelector(`[data-ctpl="${k}"]`);
    const msg = form.querySelector('.bmpl-ctpl-msg');
    const host = form.querySelector('.bmpl-ctpl-host');
    let ctl = null;
    let revision = t.revision || 0;
    let saved = !!name;

    async function load(c) {
      const spec = ctl ? ctl.read() : t.spec;
      let inv = {};
      const note = form.querySelector('.bmpl-ctpl-note');
      note.innerHTML = '';
      host.innerHTML = `<p class="form-hint">${esc(tr('capi.new.loading'))}</p>`;
      if (c) {
        try {
          const r = await fetch(`/api/capi/${enc(c)}/inventory`);
          inv = await r.json();
          if (!r.ok || inv.unreachable) throw new Error(inv.error || String(r.status));
        } catch (e) {
          inv = {};
          note.innerHTML = `<div class="bm-refusal bm-warning">${icon('warn', { size: 14 })} ${esc(tr('bmpl.ctplNoInventory', { cluster: c }))}</div>`;
        }
      }
      if (!form.isConnected) return;
      ctl = CapiCreate.templateForm(host, inv, c, spec);
    }
    own('cluster').addEventListener('change', (e) => { cluster = e.target.value; load(cluster); });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const tname = own('name').value.trim();
      if (!new RegExp(`^${NAME_PATTERN}$`).test(tname)) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badName'))}</div>`;
        return;
      }
      if (!ctl) return;
      const body = { description: own('description').value, variables: varsOf(own('variables').value), spec: ctl.read() };
      if (saved) body.revision = revision;
      const r = await api('PUT', `/api/baremetal/capi-templates/${enc(tname)}`, body);
      if (r.status === 409) {
        msg.innerHTML = `<div class="bm-refusal bmpl-conflict">${icon('warn', { size: 14 })} ${esc(r.d.current_revision == null
          ? tr('bmpl.conflictUnreadable') : tr('bmpl.ctplConflict', { n: r.d.current_revision }))}</div>`;
        msg.scrollIntoView({ block: 'nearest' });
        return;
      }
      if (!r.ok) { msg.innerHTML = refusal(r.d, r.status); msg.scrollIntoView({ block: 'nearest' }); return; }
      revision = r.d.revision;
      saved = true;
      own('name').readOnly = true;
      form.querySelector('.bmpl-ctpl-rev').textContent = tr('bmpl.revision', { n: revision });
      msg.innerHTML = okLine(tr('bmpl.ctplSaved', { n: revision }));
      await loadLists();
      paint();
    });
    await load(cluster);
    return panel;
  }

  async function removeCapiTemplate(name) {
    if (!confirm(tr('bmpl.confirmCtplDelete', { name }))) return;
    const r = await api('DELETE', `/api/baremetal/capi-templates/${enc(name)}`);
    if (r.status === 409 && r.d.code === 'template-in-use') {
      alert(tr('bmpl.ctplInUse', { plans: (r.d.plans || []).join(', ') }));
    } else if (!r.ok && r.status !== 404) {
      alert(r.d.error || r.status);
    }
    await render();
  }

  // =========================================================================
  // Fenêtre Déployer (et reprise d'un déroulé)
  // =========================================================================
  /**
   * o = { cluster, plan }        appliquer un plan à un cluster déclaré ;
   * o = { resume: action }       reprendre un déroulé `deploy:` arrêté.
   */
  async function openDeploy(o) {
    o = o || {};
    const resume = o.resume || null;
    const res = (resume && resume.result) || {};
    const id = resume ? `deploy-resume-${resume.id}` : 'deploy-plan';
    if (document.getElementById(`fp-${id}`)) FloatingPanels.close(id);
    const s = {
      cluster: resume ? String(resume.action || '').split(':').slice(1).join(':') : (o.cluster || ''),
      plan: resume ? (res.plan || '') : (o.plan || ''),
      variables: Object.assign({}, resume ? (res.variables || {}) : {}),
      plans: [], check: null, checking: false, tab: 'params', timer: null,
      needSecrets: !!(resume && res.install), machines: null, typed: {}, sse: null, launched: null,
      updateDiffering: false,
    };
    const tip = (x) => `class="tip" data-tip="${esc(x)}"`;
    const tabBtn = (t, label, tp, ic) => `<button type="button" class="sub-tab tip${t === 'params' ? ' active' : ''}" role="tab"
        aria-selected="${t === 'params'}" data-dpw-tab="${t}" data-tip="${esc(tp)}">${icon(ic, { size: 13 })} <span>${esc(label)}</span>
        ${t === 'checks' ? '<span class="dpw-count"></span>' : ''}</button>`;
    const panel = FloatingPanels.open({
      id, icon: 'build',
      title: resume ? `${tr('dpw.resumeTitle')} · ${s.cluster}` : tr('dpw.title'),
      width: Math.min(860, window.innerWidth - 120), height: Math.max(480, Math.min(780, window.innerHeight - 80)),
      onClose: () => { if (s.sse) { try { s.sse.close(); } catch { /* fermé */ } } clearTimeout(s.timer); },
      bodyHtml: `
        <form class="capi-form dpw-form" style="padding:14px;" autocomplete="off" novalidate>
          <p class="form-hint">${esc(resume ? tr('dpw.resumeHint', { id: resume.id }) : tr('dpw.hint'))}</p>
          <div class="sub-tabs sub-tabs-inline dpw-tabs" role="tablist" aria-label="${esc(tr('dpw.title'))}">
            ${tabBtn('params', tr('dpw.tab.params'), tr('dpw.t.tab.params'), 'edit')}
            ${tabBtn('checks', tr('dpw.tab.checks'), tr('dpw.t.tab.checks'), 'ok')}
            ${tabBtn('summary', tr('dpw.tab.summary'), tr('dpw.t.tab.summary'), 'notes')}
          </div>
          <div data-dpw-pane="params" role="tabpanel">
            <fieldset>
              <legend>${esc(tr('dpw.fs.target'))}</legend>
              <label ${tip(tr('dpw.t.cluster'))}>${esc(tr('dpw.f.cluster'))} *
                <select name="cluster"${resume ? ' disabled' : ''}></select></label>
              <label ${tip(tr('dpw.t.plan'))}>${esc(tr('dpw.f.plan'))} *
                <select name="plan"${resume ? ' disabled' : ''}></select></label>
            </fieldset>
            <fieldset class="dpw-vars">
              <legend>${esc(tr('dpw.fs.variables'))}</legend>
              <div class="dpw-vars-box" style="grid-column:1/-1;"></div>
            </fieldset>
            <fieldset class="dpw-secrets"${s.needSecrets ? '' : ' hidden'}>
              <legend>${esc(tr('dpw.fs.secrets'))}</legend>
              <p class="form-hint" style="grid-column:1/-1;">${esc(tr('dpw.secretsAgain'))}</p>
              <label ${tip(tr('bmp.tip.token'))}>${esc(tr('bmp.f.token'))} *
                <input data-dpw-secret="token" type="password" autocomplete="new-password"></label>
              <label ${tip(tr('bmp.tip.osPassword'))}>${esc(tr('bmp.f.osPassword'))}
                <input data-dpw-secret="password" type="password" autocomplete="new-password"></label>
              <label ${tip(tr('bmp.tip.bmcPassword'))}>${esc(tr('bmp.f.bmcPassword'))} *
                <input data-dpw-secret="bmc_password" type="password" autocomplete="new-password"></label>
            </fieldset>
            <fieldset class="dpw-system" hidden>
              <legend>${esc(tr('bmp.sys.legend'))}</legend>
              <div class="bm-system dpw-system-box" style="grid-column:1/-1;" role="status"></div>
            </fieldset>
          </div>
          <div data-dpw-pane="checks" role="tabpanel" hidden>
            <div class="dpw-findings" aria-live="polite"></div>
            <!-- une déclaration du plan diffère : gardée, sauf demande de mise à jour -->
            <label class="bm-check tip dpw-update-wrap" hidden data-tip="${esc(tr('dpw.t.updateDiffering'))}">
              <input type="checkbox" class="dpw-update" aria-label="${esc(tr('dpw.updateDiffering'))}"> ${esc(tr('dpw.updateDiffering'))}</label>
            <div class="apply-bar"><button type="button" class="btn btn-secondary btn-sm btn-ico tip dpw-recheck"
              data-tip="${esc(tr('dpw.t.recheck'))}">${icon('refresh')} ${esc(tr('dpw.recheck'))}</button></div>
          </div>
          <div data-dpw-pane="summary" role="tabpanel" hidden><div class="dpw-summary"></div></div>
          <div class="dpw-msg" role="status"></div>
          <div class="apply-bar dpw-actions">
            ${resume ? '' : `<button type="button" class="btn btn-secondary btn-sm btn-ico tip dpw-dry" disabled
                data-tip="${esc(tr('dpw.t.dry'))}">${icon('preview')} ${esc(tr('dpw.dry'))}</button>`}
            <span class="tip dpw-launch-wrap" data-tip="">
              <button type="submit" class="btn btn-primary btn-sm btn-ico dpw-launch" disabled>${icon('play')} ${esc(resume ? tr('dpw.resume') : tr('dpw.launch'))}</button></span>
            <span class="dpw-result"></span>
          </div>
          <div class="dpw-follow" hidden>
            <h4>${esc(tr('dpw.progress'))} <span class="dpw-follow-link"></span> <span class="dpw-follow-status"></span></h4>
            <ul class="bm-paths dpw-steps"></ul>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.dpw-form');
    if (form.dataset.bound) return panel;
    form.dataset.bound = '1';
    const $ = (sel) => form.querySelector(sel);
    const sig = () => JSON.stringify([s.cluster, s.plan, s.variables, s.updateDiffering]);

    function setTab(t) {
      s.tab = t;
      form.querySelectorAll('[data-dpw-tab]').forEach(b => {
        const on = b.dataset.dpwTab === t;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      form.querySelectorAll('[data-dpw-pane]').forEach(p => { p.hidden = p.dataset.dpwPane !== t; });
    }

    function planOf(name) { return s.plans.find(p => p.name === name) || null; }
    function paintSelects() {
      const clusters = (window.App && App.listClusters ? App.listClusters() : []).slice();
      if (s.cluster && !clusters.includes(s.cluster)) clusters.unshift(s.cluster);
      $('[name="cluster"]').innerHTML = `<option value="">${esc(tr('dpw.choose'))}</option>`
        + clusters.map(c => `<option value="${esc(c)}"${c === s.cluster ? ' selected' : ''}>${esc(c)}</option>`).join('');
      const names = s.plans.map(p => p.name);
      if (s.plan && !names.includes(s.plan)) names.unshift(s.plan);
      $('[name="plan"]').innerHTML = `<option value="">${esc(tr('dpw.choose'))}</option>`
        + names.map(n => {
          const p = planOf(n);
          return `<option value="${esc(n)}"${n === s.plan ? ' selected' : ''}${p && p.invalid ? ' disabled' : ''}>${esc(n)}${!p
            ? ' (' + esc(tr('bmpl.tplMissing')) + ')' : p.invalid ? ' (' + esc(tr('bmpl.invalid')) + ')' : ''}</option>`;
        }).join('');
    }
    function paintVars() {
      const p = planOf(s.plan);
      const names = p ? (p.variables || []) : Object.keys(s.variables);
      $('.dpw-vars').hidden = !names.length;
      $('.dpw-vars-box').innerHTML = names.map(v => `<label class="tip" data-tip="${esc(tr('dpw.t.variable', { name: v }))}"><code>{{${esc(v)}}}</code>
          <input data-dpw-var="${esc(v)}" value="${esc(s.variables[v] || '')}"${resume ? ' readonly' : ''} spellcheck="false"></label>`).join('');
    }

    function launchState() {
      if (!s.cluster || !s.plan) return [false, tr('dpw.t.needTarget')];
      if (!can('operator')) return [false, tr('dpw.t.viewer')];
      if (s.launched) return [false, tr('dpw.t.launched')];
      if (s.checking || !s.check || s.check.sig !== sig()) return [false, tr('dpw.t.checking')];
      if (s.check.error) return resume ? [true, tr('dpw.t.resume')] : [false, tr('dpw.t.checkFailed')];
      const b = blockingOf(s.check.findings)[0];
      if (b) return [false, tr('dpw.t.blocked', { reason: findingText(b) })];
      return [true, resume ? tr('dpw.t.resume') : tr('dpw.t.launch')];
    }
    function paintLaunch() {
      const [ok, why] = launchState();
      const btn = $('.dpw-launch');
      btn.disabled = !ok;
      btn.title = why;
      btn.setAttribute('aria-label', why);
      $('.dpw-launch-wrap').dataset.tip = why;
      const dry = $('.dpw-dry');
      if (dry) {
        dry.disabled = !ok;
        dry.title = ok ? tr('dpw.t.dry') : why;
        dry.dataset.tip = dry.title;
      }
    }
    function paintChecks() {
      const box = $('.dpw-findings');
      const count = $('.dpw-count');
      if (s.checking || (!s.check && s.cluster && s.plan)) {
        box.innerHTML = `<p class="form-hint">${esc(tr('dpw.checking'))}</p>`;
        count.textContent = '';
      } else if (!s.check) {
        box.innerHTML = `<p class="form-hint">${esc(tr('dpw.t.needTarget'))}</p>`;
        count.textContent = '';
      } else if (s.check.error) {
        box.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.checkFailed'))} ${esc(s.check.error)}</div>`;
        count.textContent = '';
      } else {
        box.innerHTML = findingsHtml(s.check.findings);
        const differs = (s.check.findings || []).some(f => f && f.code === 'decl-differs');
        $('.dpw-update-wrap').hidden = !(differs || s.updateDiffering);
        const n = blockingOf(s.check.findings).length;
        count.className = `dpw-count badge ${n ? 'fail' : 'ok'}`;
        count.textContent = String(n);
      }
      const sum = (s.check && s.check.summary) || null;
      $('.dpw-summary').innerHTML = !sum ? `<p class="form-hint">${esc(tr('dpw.noSummary'))}</p>` : `
        <p>${esc(tr('dpw.estimate', { n: sum.estimate_minutes || 0 }))}</p>
        ${(sum.declarations || []).length ? `<table class="data-table dpw-decls">
          <thead><tr><th>${esc(tr('dpw.col.declaration'))}</th><th>${esc(tr('dpw.col.template'))}</th><th>${esc(tr('dpw.col.state'))}</th></tr></thead>
          <tbody>${sum.declarations.map(d => `<tr><td><code>${esc(d.name)}</code></td><td><code>${esc(d.template)}</code></td>
            <td>${!d.exists ? `<span class="badge info">${esc(tr('dpw.declNew'))}</span>`
              : d.origin_match ? `<span class="badge ok">${esc(tr('dpw.declReused'))}</span>`
                : `<span class="badge fail">${esc(tr('dpw.declTaken'))}</span>`}</td></tr>`).join('')}</tbody></table>`
          : `<p class="form-hint">${esc(tr('dpw.noDecl'))}</p>`}
        ${sum.capi ? `<p class="dpw-capi">${esc(tr('dpw.capiSummary'))} ${(sum.capi.providers || []).map(v => `<code>${esc(v)}</code>`).join(' ')}
          <span class="badge ${sum.capi.ready ? 'ok' : 'info'}">${esc(sum.capi.ready ? tr('dpw.capiReady') : tr('dpw.capiInstall'))}</span></p>` : ''}
        ${(sum.clusters || []).length ? `<table class="data-table dpw-clusters">
          <thead><tr><th>${esc(tr('dpw.col.cluster'))}</th><th>${esc(tr('dpw.col.template'))}</th><th>${esc(tr('dpw.col.vms'))}</th><th>${esc(tr('dpw.col.state'))}</th></tr></thead>
          <tbody>${sum.clusters.map(c => `<tr><td><code class="dpw-cluster-name">${esc(c.name)}</code></td><td><code>${esc(c.template)}</code></td>
            <td>${c.vms == null ? '' : esc(c.vms)}</td>
            <td>${c.exists ? `<span class="badge ok">${esc(tr('dpw.clusterPresent'))}</span>` : `<span class="badge info">${esc(tr('dpw.clusterNew'))}</span>`}</td></tr>`).join('')}</tbody></table>` : ''}`;
      paintLaunch();
    }

    async function runCheck() {
      clearTimeout(s.timer);
      if (!s.cluster || !s.plan) { s.check = null; paintChecks(); return; }
      const mine = sig();
      s.checking = true;
      paintChecks();
      let r;
      const body = { plan: s.plan, variables: s.variables };
      if (s.updateDiffering) body.update_differing = true;
      try { r = await api('POST', `/api/deploy/${enc(s.cluster)}/check`, body); }
      catch (e) { r = { ok: false, status: 0, d: { error: e.message } }; }
      if (mine !== sig()) return;                 // les paramètres ont changé depuis
      s.checking = false;
      s.check = r.ok ? Object.assign({ sig: mine }, r.d) : { sig: mine, error: r.d.error || String(r.status) };
      paintChecks();
    }
    const later = () => { clearTimeout(s.timer); s.check = null; paintChecks(); s.timer = setTimeout(runCheck, 450); };

    // --- reprise : secrets, noms tapés ---------------------------------------
    function secrets() {
      const out = {};
      form.querySelectorAll('[data-dpw-secret]').forEach(i => { if (i.value) out[i.dataset.dpwSecret] = i.value; });
      return out;
    }
    function paintSystem() {
      const fs = $('.dpw-system');
      fs.hidden = !s.machines;
      if (!s.machines) return;
      $('.dpw-system-box').innerHTML = s.machines.map(m => (m.state === 'present' ? `
        <div class="ct-banner auto-banner-warn bm-system-present">${icon('warn', { size: 14 })}
          <div><strong>${esc(m.bmc_host)}</strong> ${esc(tr('bmc.sys.present'))}
            <ul class="bm-system-evidence">${(m.evidence || []).map(e => `<li>${esc(window.BMC ? BMC.evidenceText(e) : e.code)}</li>`).join('')}</ul>
            <label class="bm-system-confirm tip" data-tip="${esc(tr('bmp.sys.typeTip'))}">${esc(tr('bmc.sys.typeName'))}
              <code class="bm-system-name">${esc(m.confirm_name)}</code>
              <input data-dpw-sys="${esc(m.bmc_host)}" autocomplete="off" spellcheck="false" value="${esc(s.typed[m.bmc_host] || '')}"
                     title="${esc(tr('bmp.sys.typeTip'))}" aria-label="${esc(tr('bmp.sys.typeTip'))}"></label></div></div>`
        : `<div class="ct-banner bm-system-absent">${icon('info', { size: 14 })} <span><strong>${esc(m.bmc_host)}</strong>
            ${esc(m.state === 'absent' ? tr('bmc.sys.absent') : tr('bmc.sys.unknown'))}</span></div>`)).join('');
    }

    // --- suivi ---------------------------------------------------------------
    function stepLabel(stepId) {
      const sid = String(stepId || '');
      if (sid.startsWith('phase-')) return text(PHASES, sid.slice(6));
      if (sid.startsWith('tf-')) return tr('dpw.stepTemplate', { name: sid.slice(3) });
      if (sid.startsWith('ready-')) return tr('dpw.stepReady', { code: sid.slice(6) });
      if (sid.startsWith('k8s-')) return tr('dpw.stepCluster', { name: sid.slice(4) });
      if (sid === 'capi-providers') return tr('dpw.stepProviders');
      return sid;
    }
    function follow(actionId) {
      s.launched = actionId;
      paintLaunch();
      $('.dpw-follow').hidden = false;
      $('.dpw-follow-link').innerHTML = actionLink(actionId);
      const ul = $('.dpw-steps');
      ul.innerHTML = '';
      if (typeof Dock !== 'undefined' && Dock.poll) Dock.poll();
      if (window.BMC && BMC.renderHistory && BMC.tab && BMC.tab() === 'history') BMC.renderHistory();
      if (!window.SSEReconnect) return;
      s.sse = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
        on: {
          step: (ev) => {
            let d = {};
            try { d = JSON.parse(ev.data); } catch { return; }
            if (!form.isConnected) return;
            let li = [...ul.children].find(x => x.dataset.step === d.step_id);
            if (!li) {
              li = document.createElement('li');
              li.className = 'dpw-step';
              li.dataset.step = d.step_id || '';
              ul.appendChild(li);
            }
            const cls = d.status === 'done' ? 'ok' : d.status === 'error' ? 'fail' : d.status === 'partial' ? 'warn'
              : d.status === 'running' ? 'info' : 'dim';
            li.innerHTML = `<span class="badge ${cls}">${esc(d.status || '')}</span> <strong>${esc(stepLabel(d.step_id))}</strong>
              ${d.message ? `<span class="dpw-step-msg">${linkIds(d.message)}</span>` : ''}`;
          },
          status: (ev) => {
            let d = {};
            try { d = JSON.parse(ev.data); } catch { return; }
            if (form.isConnected) $('.dpw-follow-status').innerHTML = d.status ? `<span class="badge dim">${esc(d.status)}</span>` : '';
          },
          end: () => {
            if (s.sse) { try { s.sse.close(); } catch { /* fermé */ } }
            if (typeof Dock !== 'undefined' && Dock.poll) Dock.poll();
          },
        },
      });
    }

    async function launch(dryRun) {
      const msg = $('.dpw-msg');
      const out = $('.dpw-result');
      const [ok] = launchState();
      if (!ok) return;
      let r;
      if (resume) {
        const body = { action_id: resume.id };
        const sec = secrets();
        if (Object.keys(sec).length) body.secrets = sec;
        if (s.machines) {
          const typed = {};
          s.machines.filter(m => m.state === 'present').forEach(m => { typed[m.bmc_host] = (s.typed[m.bmc_host] || '').trim(); });
          body.system_confirm = typed;
        }
        if (!confirm(tr('dpw.confirmResume', { id: resume.id, cluster: s.cluster }))) return;
        out.textContent = '…';
        r = await api('POST', `/api/deploy/${enc(s.cluster)}/resume`, body);
      } else {
        if (!confirm(dryRun ? tr('dpw.confirmDry', { plan: s.plan, cluster: s.cluster })
          : tr('dpw.confirmLaunch', { plan: s.plan, cluster: s.cluster }))) return;
        out.textContent = '…';
        const body = { plan: s.plan, variables: s.variables, dry_run: !!dryRun };
        if (s.updateDiffering) body.update_differing = true;
        r = await api('POST', `/api/deploy/${enc(s.cluster)}`, body);
      }
      out.textContent = '';
      if (r.ok && r.d.action_id) {
        // constats rendus au départ (plan changé depuis le déroulé repris...)
        const notes = (r.d.findings || []).filter(f => f && f.level !== 'info');
        msg.innerHTML = okLine(dryRun ? tr('dpw.dryStarted') : tr('dpw.started')) + (notes.length ? findingsHtml(notes) : '');
        follow(r.d.action_id);
        return;
      }
      if (r.d.code === 'blocking-findings') {
        s.check = { sig: sig(), findings: r.d.findings || [], summary: r.d.summary || null };
        paintChecks();
        setTab('checks');
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.refusedFindings'))}</div>`;
        return;
      }
      if (r.d.code === 'secrets-required') {
        s.needSecrets = true;
        $('.dpw-secrets').hidden = false;
        setTab('params');
        msg.innerHTML = `<div class="bm-refusal">${icon('warn', { size: 14 })} ${esc(tr('dpw.needSecrets'))}</div>`;
        const first = form.querySelector('[data-dpw-secret]');
        if (first) first.focus();
        return;
      }
      if (r.status === 409 && (r.d.fields || []).includes('system_confirm')) {
        s.machines = r.d.machines || [];
        paintSystem();
        setTab('params');
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmp.sys.refused', { n: (r.d.refused || []).length }))}</div>`;
        return;
      }
      if (r.d.code === 'deploy-running' && r.d.running) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.running'))} ${actionLink(r.d.running)}</div>`;
        return;
      }
      msg.innerHTML = refusal(r.d, r.status);
    }

    form.addEventListener('click', (e) => {
      const t = e.target.closest('[data-dpw-tab]');
      if (t) { setTab(t.dataset.dpwTab); return; }
      if (e.target.closest('.dpw-recheck')) { runCheck(); return; }
      if (e.target.closest('.dpw-dry')) { launch(true); }
    });
    form.addEventListener('change', (e) => {
      if (e.target.classList.contains('dpw-update')) { s.updateDiffering = e.target.checked; runCheck(); return; }
      if (e.target.name === 'cluster') { s.cluster = e.target.value; later(); }
      if (e.target.name === 'plan') {
        s.plan = e.target.value;
        const p = planOf(s.plan);
        const keep = {};
        (p ? p.variables || [] : []).forEach(v => { if (s.variables[v] != null) keep[v] = s.variables[v]; });
        s.variables = keep;
        paintVars();
        later();
      }
    });
    form.addEventListener('input', (e) => {
      const v = e.target.closest('[data-dpw-var]');
      if (v && !resume) {
        if (v.value) s.variables[v.dataset.dpwVar] = v.value; else delete s.variables[v.dataset.dpwVar];
        later();
      }
      const y = e.target.closest('[data-dpw-sys]');
      if (y) s.typed[y.dataset.dpwSys] = y.value;
    });
    form.addEventListener('submit', (ev) => { ev.preventDefault(); launch(false); });

    try {
      const r = await api('GET', '/api/baremetal/plans');
      s.plans = r.ok ? (r.d.plans || []) : [];
    } catch { s.plans = []; }
    if (!form.isConnected) return panel;
    paintSelects();
    paintVars();
    paintSystem();
    // Reprise sans plan connu : la console revérifie seule
    if (resume && !s.plan) { s.check = { sig: sig(), error: tr('dpw.noPlanKnown') }; paintChecks(); } else runCheck();
    paintChecks();
    return panel;
  }

  // =========================================================================
  // Historique : « Reprendre » sur un déroulé arrêté
  // =========================================================================
  /** 2.5.0 : un cluster enfant créé par un plan, lien vers Cluster API > Clusters. */
  function clusterLink(a) {
    if (!a || !a.parent || !String(a.action || '').startsWith('capi-cluster-create:')) return '';
    return ` <a class="tip bmpl-cluster-link" href="#automation/capi/clusters" data-tip="${esc(tr('bmh.t.clusterLink'))}">${icon('capi', { size: 12 })} ${esc(tr('bmh.clusterLink'))}</a>`;
  }

  function resumeButton(a) {
    if (!a || !String(a.action || '').startsWith('deploy:') || !RESUMABLE.includes(a.status) || a.dry_run) return '';
    const ok = can('operator');
    const tp = ok ? tr('dpw.t.resumeBtn') : tr('dpw.t.viewer');
    return ` <button type="button" class="btn btn-sm btn-secondary btn-ico tip bm-deploy-resume" data-action-id="${esc(a.id)}"
      ${ok ? '' : 'disabled'} title="${esc(tp)}" data-tip="${esc(tp)}">${icon('play', { size: 12 })} ${esc(tr('dpw.resume'))}</button>`;
  }

  // =========================================================================
  // Champ « Plan » des fenêtres d'installation et de série
  // =========================================================================
  /** Monte le choix d'un plan dans `box` ; `initial` : plan du profil (un
   *  plan absent est signalé) ; `withVars` : ses variables à saisir. Aucun
   *  champ nommé (FormData de la fenêtre ne le voit pas). */
  function mountPicker(box, initial, withVars) {
    const ctl = { plan: initial || '', values: {}, plans: [], loaded: false };
    function paintPicker() {
      const names = ctl.plans.filter(p => !p.invalid).map(p => p.name);
      const missing = ctl.plan && ctl.loaded && !names.includes(ctl.plan);
      const p = ctl.plans.find(x => x.name === ctl.plan);
      box.innerHTML = `
        <label class="tip" data-tip="${esc(tr('bmpl.pick.tip'))}">${esc(tr('bmpl.pick.label'))}
          <select data-pp="plan">
            <option value="">${esc(tr('bmpl.pick.none'))}</option>
            ${missing ? `<option value="${esc(ctl.plan)}" selected>${esc(ctl.plan)} (${esc(tr('bmpl.tplMissing'))})</option>` : ''}
            ${names.map(n => `<option value="${esc(n)}"${n === ctl.plan ? ' selected' : ''}>${esc(n)}</option>`).join('')}
          </select></label>
        ${missing ? `<p class="bm-refusal bm-warning bmpl-pick-missing">${icon('warn', { size: 13 })} ${esc(tr('bmpl.pick.missing', { name: ctl.plan }))}</p>` : ''}
        ${withVars && p && (p.variables || []).length ? `<div class="bmpl-pick-vars">${p.variables.map(v => `
          <label class="tip" data-tip="${esc(tr('dpw.t.variable', { name: v }))}"><code>{{${esc(v)}}}</code>
            <input data-pp-var="${esc(v)}" value="${esc(ctl.values[v] || '')}" spellcheck="false"></label>`).join('')}</div>` : ''}
        ${ctl.plan ? `<p class="form-hint">${esc(tr('bmpl.pick.hint'))}</p>` : ''}`;
    }
    box.addEventListener('change', (e) => {
      if (e.target.matches('[data-pp="plan"]')) { ctl.plan = e.target.value; ctl.values = {}; paintPicker(); }
    });
    box.addEventListener('input', (e) => {
      const v = e.target.closest('[data-pp-var]');
      if (v) ctl.values[v.dataset.ppVar] = v.value;
    });
    ctl.value = () => {
      if (!ctl.plan) return null;
      const vars = {};
      Object.entries(ctl.values).forEach(([k, v]) => { if (v) vars[k] = v; });
      return withVars ? { plan: ctl.plan, plan_variables: vars } : { plan: ctl.plan };
    };
    paintPicker();
    api('GET', '/api/baremetal/plans').then(r => {
      ctl.plans = r.ok ? (r.d.plans || []) : [];
      ctl.loaded = r.ok;
      if (box.isConnected) paintPicker();
    }).catch(() => {});
    return ctl;
  }

  // =========================================================================
  function init() {
    document.addEventListener('click', (e) => {
      const rs = e.target.closest('.bm-deploy-resume');
      if (rs && !rs.disabled) {
        e.preventDefault();
        const a = window.BMC && BMC.historyAction ? BMC.historyAction(rs.dataset.actionId) : null;
        if (a) openDeploy({ resume: a });
        return;
      }
      const box = e.target.closest('#bm-plans');
      if (!box) return;
      let el;
      if (e.target.closest('.bmpl-new')) { startEdit('', null, 0); paint(); return; }
      if ((el = e.target.closest('.bmpl-card'))) { openPlan(el.dataset.plan); return; }
      if (e.target.closest('.bmpl-tpl-new')) { openTemplate(null); return; }
      if ((el = e.target.closest('.bmpl-tpl-edit'))) { openTemplate(el.dataset.name); return; }
      if ((el = e.target.closest('.bmpl-tpl-del'))) { removeTemplate(el.dataset.name); return; }
      if (e.target.closest('.bmpl-ctpl-new')) { openCapiTemplate(null); return; }
      if ((el = e.target.closest('.bmpl-ctpl-edit'))) { openCapiTemplate(el.dataset.name); return; }
      if ((el = e.target.closest('.bmpl-ctpl-del'))) { removeCapiTemplate(el.dataset.name); return; }
      if (!edit) return;
      if (e.target.closest('.bmpl-reload')) { openPlan(edit.name); return; }
      if (e.target.closest('.bmpl-apply')) { openDeploy({ plan: edit.name, cluster: window.App ? App.getCurrentCluster() : '' }); return; }
      if (e.target.closest('.bmpl-delete')) { removePlan(edit.name); return; }
      const i = (x) => Number(x.dataset.i);
      if ((el = e.target.closest('.bmpl-up'))) {
        const k = i(el);
        if (k > 0) { [edit.items[k - 1], edit.items[k]] = [edit.items[k], edit.items[k - 1]]; fixDeps(); paintTemplates(); }
        return;
      }
      if ((el = e.target.closest('.bmpl-down'))) {
        const k = i(el);
        if (k < edit.items.length - 1) { [edit.items[k + 1], edit.items[k]] = [edit.items[k], edit.items[k + 1]]; fixDeps(); paintTemplates(); }
        return;
      }
      if ((el = e.target.closest('.bmpl-rm'))) { edit.items.splice(i(el), 1); fixDeps(); paintTemplates(); return; }
      // 2.5.0 : clusters enfants
      const cl = edit.clusters;
      if ((el = e.target.closest('.bmpl-cl-up'))) {
        const k = i(el);
        if (k > 0) { [cl[k - 1], cl[k]] = [cl[k], cl[k - 1]]; fixDeps(); paintClusters(); }
        return;
      }
      if ((el = e.target.closest('.bmpl-cl-down'))) {
        const k = i(el);
        if (k < cl.length - 1) { [cl[k + 1], cl[k]] = [cl[k], cl[k + 1]]; fixDeps(); paintClusters(); }
        return;
      }
      if ((el = e.target.closest('.bmpl-cl-rm'))) { cl.splice(i(el), 1); fixDeps(); paintClusters(); return; }
      if (e.target.closest('.bmpl-cl-add')) {
        const t0 = (ctplData.templates || []).find(t => !t.invalid);
        if (t0 && cl.length < MAX_CLUSTERS) { cl.push({ template: t0.name, name: `{{cluster}}-k8s-${cl.length + 1}`, depends_on: [] }); paintClusters(); }
        return;
      }
      if (e.target.closest('.bmpl-cl-several')) {
        const fs = box.querySelector('.bmpl-cl-shortcut');
        if (fs) fs.hidden = !fs.hidden;
        return;
      }
      if (e.target.closest('.bmpl-sc-ok')) { takeShortcut(box.querySelector('.bmpl-cl-shortcut')); return; }
      if (e.target.closest('.bmpl-sc-clear')) { edit.shortcut = null; paintClusters(); return; }
      if (e.target.closest('.bmpl-add-tpl-go')) {
        const sel = box.querySelector('.bmpl-add-tpl');
        if (sel && sel.value) { edit.items.push({ template: sel.value, depends_on: [] }); paintTemplates(); }
      }
    });
    document.addEventListener('change', (e) => {
      const cdep = e.target.closest('#bm-plans .bmpl-cl-dep');
      if (cdep && edit) {
        const it = edit.clusters[Number(cdep.dataset.i)];
        if (!it) return;
        it.depends_on = it.depends_on.filter(x => x !== cdep.value);
        if (cdep.checked) it.depends_on.push(cdep.value);
        return;
      }
      const ctpl = e.target.closest('#bm-plans .bmpl-cl-tpl');
      if (ctpl && edit) {
        const it = edit.clusters[Number(ctpl.dataset.i)];
        if (it) { it.template = ctpl.value; paintClusters(); }
        return;
      }
      const cname = e.target.closest('#bm-plans .bmpl-cl-name');
      if (cname && edit) {
        // un nom changé : les dépendances qui le désignaient le suivent
        const k = Number(cname.dataset.i);
        const it = edit.clusters[k];
        if (!it) return;
        const old = it.name;
        it.name = cname.value.trim();
        edit.clusters.slice(k + 1).forEach(x => { x.depends_on = x.depends_on.map(d => (d === old ? it.name : d)); });
        fixDeps();
        paintClusters();
        return;
      }
      const dep = e.target.closest('#bm-plans .bmpl-dep');
      if (dep && edit) {
        const it = edit.items[Number(dep.dataset.i)];
        if (!it) return;
        it.depends_on = it.depends_on.filter(x => x !== dep.value);
        if (dep.checked) it.depends_on.push(dep.value);
      }
    });
    document.addEventListener('submit', (e) => {
      if (e.target.matches && e.target.matches('#bm-plans .bmpl-form')) { e.preventDefault(); save(e.target); }
    });
  }

  return { init, render, openDeploy, openTemplate, openCapiTemplate, resumeButton, clusterLink, mountPicker,
    findingsHtml, findingText, actionLink, linkIds };
})();

document.addEventListener('DOMContentLoaded', BMPlans.init);
window.BMPlans = BMPlans;
