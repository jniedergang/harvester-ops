/**
 * harvester-ops : plans de déploiement (2.4.0).
 *
 * Onglet Plans de Bare-metal (#automation/pxe/plans) :
 *   - la liste des plans à gauche, le plan choisi à droite : nom,
 *     description, variables, une carte par phase avec son interrupteur
 *     (Registre depuis la 2.6.0 ; Artefacts depuis la 2.7.0 : politiques
 *     et accès, éléments des paquets importés), délai de
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
 * Fenêtre « Défaire le déploiement » (openUndo, 2.7.0) : depuis l'Historique,
 * sur un déroulé fini ; Contenu, Plan de destruction, Confirmation (nom du
 * cluster tapé, second nom pour les données d'un registre) ; « Défaire »
 * grisé tant que le plan des cases ACTUELLES n'est pas revenu.
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
    gitops: () => tr('bmpl.phase.gitops'),
    // 2.10.0 : phase tirée de l'option `external_secrets` des clusters (sans interrupteur)
    eso: () => tr('bmpl.phase.eso'),
  };
  const PHASE_TIPS = {
    readiness: () => tr('bmpl.phaseTip.readiness'), terraform: () => tr('bmpl.phaseTip.terraform'),
    capi: () => tr('bmpl.phaseTip.capi'), clusters: () => tr('bmpl.phaseTip.clusters'),
    registry: () => tr('bmpl.phaseTip.registry'), artefacts: () => tr('bmpl.phaseTip.artefacts'),
    gitops: () => tr('bmpl.phaseTip.gitops'),
  };
  const FINDINGS = {
    'plan-invalid': () => tr('dpw.f.planInvalid'),
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
    // 2.6.0 : phase Registre
    'registry-name-taken': () => tr('dpw.f.registryNameTaken'),
    'registry-unknown': () => tr('dpw.f.registryUnknown'),
    'registry-admin': () => tr('dpw.f.registryAdmin'),
    'registry-check-failed': () => tr('dpw.f.registryCheckFailed'),
    'registry-package-too-big': () => tr('dpw.f.registryPackageTooBig'),
    'registry-address-differs': () => tr('dpw.f.registryAddressDiffers'),
    'artefact-package-missing': () => tr('dpw.f.artefactPackageMissing'),
    'package-untrusted-strict': () => tr('dpw.f.packageUntrustedStrict'),
    'registry-var-missing': () => tr('dpw.f.registryVarMissing'),
    'registry-exists': () => tr('dpw.f.registryExists'),
    'registry-later': () => tr('dpw.f.registryLater'),
    'publish-later': () => tr('dpw.f.publishLater'),
    'cluster-registry-later': () => tr('dpw.f.clusterRegistryLater'),
    'registry-address-answers': () => tr('dpw.f.registryAddressAnswers'),
    'registry-address-in-pool': () => tr('dpw.f.registryAddressInPool'),
    'registry-address-vip': () => tr('dpw.f.registryAddressVip'),
    'registry-room-short': () => tr('dpw.f.registryRoomShort'),
    'registry-not-attached': () => tr('dpw.f.registryNotAttached'),
    'registry-unreachable': () => tr('dpw.f.registryUnreachable'),
    'publish-tag-differs': () => tr('dpw.f.publishTagDiffers'),
    'cluster-registry-not-attached': () => tr('dpw.f.clusterRegistryNotAttached'),
    'cluster-registry-no-fallback-system': () => tr('dpw.f.clusterRegistryNoFallbackSystem'),
    'registry-vm-network-unchecked': () => tr('dpw.f.registryVmNetworkUnchecked'),
    'cluster-registry-vm-network-unchecked': () => tr('dpw.f.clusterRegistryVmNetworkUnchecked'),
    'cluster-registry-unreachable-from-vm-network': () => tr('dpw.f.clusterRegistryUnreachableFromVmNetwork'),
    'cluster-registry-vm-network-ok': () => tr('dpw.f.clusterRegistryVmNetworkOk'),
    'registry-cert-expiring': () => tr('dpw.f.registryCertExpiring'),
    // 2.7.0 : phase Artefacts (constats de la console et de manifest-check)
    'artefact-invalid': () => tr('dpw.f.artefactInvalid'),
    'artefact-not-published': () => tr('dpw.f.artefactNotPublished'),
    'artefact-taken': () => tr('dpw.f.artefactTaken'),
    'artefact-check-failed': () => tr('dpw.f.artefactCheckFailed'),
    'artefact-var-missing': () => tr('dpw.f.artefactVarMissing'),
    'caaph-missing': () => tr('dpw.f.caaphMissing'),
    'child-admin-required': () => tr('dpw.f.childAdminRequired'),
    'artefact-later': () => tr('dpw.f.artefactLater'),
    'artefact-images-not-published': () => tr('dpw.f.artefactImagesNotPublished'),
    'manifest-kind-refused': () => tr('dpw.f.manifestKindRefused'),
    'manifest-kind-unknown': () => tr('dpw.f.manifestKindUnknown'),
    'artefact-eso-before-chart': () => tr('dpw.f.manifestEsoBeforeChart'),
    'manifest-system-namespace': () => tr('dpw.f.manifestSystemNamespace'),
    'manifest-object-taken': () => tr('dpw.f.manifestObjectTaken'),
    'manifest-too-big': () => tr('dpw.f.manifestTooBig'),
    'manifest-sa-token-refused': () => tr('dpw.f.manifestSaTokenRefused'),
    'manifest-crb-admin': () => tr('dpw.f.manifestCrbAdmin'),
    'manifest-duplicate': () => tr('dpw.f.manifestDuplicate'),
    'manifest-no-name': () => tr('dpw.f.manifestNoName'),
    'manifest-too-many': () => tr('dpw.f.manifestTooMany'),
    'artefact-name-too-long': () => tr('dpw.f.artefactNameTooLong'),
    'artefact-name-reserved': () => tr('dpw.f.artefactNameReserved'),
    'artefact-item-not-manifest': () => tr('dpw.f.artefactItemNotManifest'),
    'team-namespace-taken': () => tr('dpw.f.teamNamespaceTaken'),
    'team-namespace-missing': () => tr('dpw.f.teamNamespaceMissing'),
    'manifest-cluster-missing': () => tr('dpw.f.manifestClusterMissing'),
    // 2.8.0 : phase GitOps, accès des clusters enfants (modèles), sans Internet
    'gitops-var-missing': () => tr('dpw.f.gitopsVarMissing'),
    'gitops-secret-missing': () => tr('dpw.f.gitopsSecretMissing'),
    'gitops-privileges-admin-only': () => tr('dpw.f.gitopsPrivilegesAdminOnly'),
    'gitops-fleet-admin-only': () => tr('dpw.f.gitopsFleetAdminOnly'),
    'gitops-fleet-not-confined': () => tr('dpw.f.gitopsFleetNotConfined'),
    'gitops-fleet-needs-rancher': () => tr('dpw.f.gitopsFleetNeedsRancher'),
    'gitops-source-unsupported': () => tr('dpw.f.gitopsSourceUnsupported'),
    'gitops-flux-in-plan': () => tr('dpw.f.gitopsFluxInPlan'),
    'gitops-flux-missing': () => tr('dpw.f.gitopsFluxMissing'),
    'gitops-flux-not-locked': () => tr('dpw.f.gitopsFluxNotLocked'),
    'gitops-later': () => tr('dpw.f.gitopsLater'),
    'gitops-package-not-published': () => tr('dpw.f.gitopsPackageNotPublished'),
    'gitops-check-failed': () => tr('dpw.f.gitopsCheckFailed'),
    'gitops-taken': () => tr('dpw.f.gitopsTaken'),
    // 2.10.0 : External Secrets des clusters du plan
    'eso-needs-openbao': () => tr('dpw.f.esoNeedsOpenbao'),
    'eso-admin-only': () => tr('dpw.f.esoAdminOnly'),
    'eso-no-namespace': () => tr('dpw.f.esoNoNamespace'),
    'eso-chart-not-in-plan': () => tr('dpw.f.esoChartNotInPlan'),
    'gitops-cluster-unknown': () => tr('dpw.f.gitopsClusterUnknown'),
    'gitops-namespace-not-team': () => tr('dpw.f.gitopsNamespaceNotTeam'),
    'gitops-name-too-long': () => tr('dpw.f.gitopsNameTooLong'),
    'gitops-invalid': () => tr('dpw.f.gitopsInvalid'),
    'gitops-cluster-missing': () => tr('dpw.f.gitopsClusterMissing'),
    'rancher-session-needed': () => tr('dpw.f.rancherSessionNeeded'),
    'rancher-session-in-chain': () => tr('dpw.f.rancherSessionInChain'),
    'rancher-import-conflict': () => tr('dpw.f.rancherImportConflict'),
    'rancher-name-invalid': () => tr('dpw.f.rancherNameInvalid'),
    'rancher-name-taken': () => tr('dpw.f.rancherNameTaken'),
    'rancher-agent-image-missing': () => tr('dpw.f.rancherAgentImageMissing'),
    'rancher-agent-image-unmirrored': () => tr('dpw.f.rancherAgentImageUnmirrored'),
    'rancher-agent-image-fallback': () => tr('dpw.f.rancherAgentImageFallback'),
    'oidc-invalid': () => tr('dpw.f.oidcInvalid'),
    'oidc-issuer-invalid': () => tr('dpw.f.oidcIssuerInvalid'),
    'oidc-audience-invalid': () => tr('dpw.f.oidcAudienceInvalid'),
    'oidc-claim-invalid': () => tr('dpw.f.oidcClaimInvalid'),
    'oidc-prefix-required': () => tr('dpw.f.oidcPrefixRequired'),
    'oidc-prefix-system': () => tr('dpw.f.oidcPrefixSystem'),
    'oidc-ca-unreadable': () => tr('dpw.f.oidcCaUnreadable'),
    'oidc-admin-group-invalid': () => tr('dpw.f.oidcAdminGroupInvalid'),
    'oidc-version-too-old': () => tr('dpw.f.oidcVersionTooOld'),
    'oidc-with-rancher': () => tr('dpw.f.oidcWithRancher'),
    'offline-needs-no-fallback': () => tr('dpw.f.offlineNeedsNoFallback'),
    'offline-rke2-missing': () => tr('dpw.f.offlineRke2Missing'),
    'offline-rke2-version-mismatch': () => tr('dpw.f.offlineRke2VersionMismatch'),
    'offline-disk-small': () => tr('dpw.f.offlineDiskSmall'),                       // 2.8.1
    'offline-node-packages': () => tr('dpw.f.offlineNodePackages'),                 // 2.8.1 (relecture I-1)
    'offline-later': () => tr('dpw.f.offlineLater'),                                // 2.8.1 (banc R12)
    'terraform-ref-later': () => tr('dpw.f.terraformRefLater'),                     // 2.8.2 (décision 246)
  };
  const LEVELS = {
    blocking: () => tr('dpw.lvl.blocking'), warning: () => tr('dpw.lvl.warning'), info: () => tr('dpw.lvl.info'),
  };
  const LEVEL_CLS = { blocking: 'fail', warning: 'warn', info: 'info' };
  const LEVEL_RANK = { blocking: 0, warning: 1, info: 2 };
  const text = (map, key) => (map[key] ? map[key]() : String(key == null ? '' : key));
  const findingText = (f) => text(FINDINGS, f && f.code);
  const blockingOf = (findings) => (findings || []).filter(f => f && f.level === 'blocking');
  const bytesOf = (n) => (window.XferProgress ? XferProgress.bytes(n) : `${n} B`);

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
  const AVAILABLE_DEFAULT = ['readiness', 'terraform', 'registry', 'capi', 'clusters', 'artefacts', 'gitops'];
  // 2.7.0 : phase Artefacts, bornes du serveur (deploy_plans.ARTEFACTS_TIMEOUT) ;
  // sortes applicables (images : phase Registre ; gitops : 2.8)
  const ARTEFACTS_TIMEOUT = [1, 60, 15];
  const APPLICABLE_KINDS = ['chart', 'manifest', 'policy'];
  const GROUP_ROLES = ['admin', 'edit', 'view'];
  const ACCOUNT_ROLES = ['edit', 'view'];
  // éléments d'un paquet importé : clé `nom@version` -> 'loading' | 'error' | [{name, kind}]
  const pkgItems = new Map();
  // 2.6.0 : phase Registre, bornes du serveur (minutes)
  const REGISTRY_TIMEOUT = [1, 60, 10];
  const PUBLISH_TIMEOUT = [5, 600, 60];
  let regData = { registries: [], packages: [] };
  const MAX_CLUSTERS = 16;
  // 2.8.0 : phase GitOps, bornes du serveur (deploy_plans.GITOPS_TIMEOUT,
  // MAX_GITOPS_ENTRIES ; child_gitops.NAME_RE, INTERVAL_*)
  const GITOPS_TIMEOUT = [1, 60, 15];
  const MAX_GITOPS = 16;
  const GITOPS_INTERVAL = [1, 1440, 10];
  const GITOPS_NAME_RE = /^[a-z0-9]([-a-z0-9]{0,32}[a-z0-9])?$/;
  const GITOPS_TOOLS = ['auto', 'flux', 'fleet'];
  const GITOPS_SOURCES = ['oci', 'git', 'chart'];
  // modèle de cluster -> import Rancher externe demandé (true/false), lu une fois
  const tplAccess = new Map();
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
    // 2.6.0 : registres déclarés et paquets importés (listes de la phase Registre)
    const [rg, ar] = await Promise.all([
      api('GET', '/api/registries').catch(() => ({ ok: false, d: {} })),
      api('GET', '/api/artefacts').catch(() => ({ ok: false, d: {} })),
    ]);
    regData = { registries: rg.ok ? (rg.d.registries || []) : [], packages: ar.ok ? (ar.d.packages || []) : [] };
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
    if (edit) { paintTemplates(); paintClusters(); paintRegistries(); paintArtefacts(); paintGitops(); }
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
        template: x.template, name: x.name || '', depends_on: (x.depends_on || []).slice(),
        registries: (x.registries || []).map(r => (typeof r === 'string' ? r : r.registry)).join(', ') })),
      registries: (((d.phases || {}).registry || {}).registries || []).map(r => Object.assign({}, r,
        { mirrors: (r.mirrors || []).join(', ') })),
      publish: (((d.phases || {}).registry || {}).publish || []).map(p => Object.assign({}, p)),
      // 2.7.0 : phase Artefacts ; les clés non éditées (quota, limits,
      // network d'un namespace d'équipe) sont gardées telles que lues
      artPolicies: (((d.phases || {}).artefacts || {}).policies || []).map(p => ({
        clusters: (p.clusters || []).join(', '), base: p.base === true,
        teams: (p.teams || []).map(t => t.namespace).join(', '),
        teamObjs: Object.fromEntries((p.teams || []).map(t => [t.namespace, Object.assign({}, t)])),
        roles: (p.roles || []).map(r => Object.assign({}, r)),
        accounts: (p.accounts || []).map(a => Object.assign({}, a)) })),
      artItems: (((d.phases || {}).artefacts || {}).items || []).map(x => ({
        package: x.package || '', version: String(x.version || ''), item: x.item || '', registry: x.registry || '',
        valuesText: x.values && typeof x.values === 'object' ? JSON.stringify(x.values, null, 2) : '',
        clusters: (x.clusters || []).join(', '), depends_on: (x.depends_on || []).slice() })),
      // 2.8.0 : phase GitOps ; les clés d'une entrée que la carte n'édite
      // pas sont gardées telles que lues (`raw`)
      gitops: (((d.phases || {}).gitops || {}).entries || []).map(gopFromDoc),
      shortcut: null,
    };
    lastMsg = '';
  }

  /** Entrée GitOps lue (format de deploy_plans) -> ligne éditée. */
  function gopFromDoc(e) {
    const src = (e && e.source) || {};
    return {
      raw: Object.assign({}, e || {}), name: (e && e.name) || '', tool: (e && e.tool) || 'auto',
      clusters: ((e && e.clusters) || []).join(', '), target_namespace: (e && e.target_namespace) || '',
      stype: src.type || 'oci', package: src.package || '', version: String(src.version || ''), item: src.item || '',
      registry: src.registry || '', url: src.url || '', branch: src.branch || '', path: src.path || '', secret: src.secret || '',
      privileges: (e && e.privileges) || 'namespace',
      interval: e && e.interval_minutes != null ? String(e.interval_minutes) : String(GITOPS_INTERVAL[2]),
      prune: !(e && e.prune === false),
    };
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
      : ['readiness', 'terraform', 'registry', 'capi', 'clusters', 'artefacts', 'gitops'];
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
    } else if (pid === 'registry') {
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.registryTimeout'))}">${esc(tr('bmpl.f.registryTimeout'))}
          <input type="number" name="registry_timeout" min="${REGISTRY_TIMEOUT[0]}" max="${REGISTRY_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : REGISTRY_TIMEOUT[2])}"></label>
        <label class="tip" data-tip="${esc(tr('bmpl.t.publishTimeout'))}">${esc(tr('bmpl.f.publishTimeout'))}
          <input type="number" name="publish_timeout" min="${PUBLISH_TIMEOUT[0]}" max="${PUBLISH_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.publish_timeout_minutes != null ? ph.publish_timeout_minutes : PUBLISH_TIMEOUT[2])}"></label>
        <h4 style="grid-column:1/-1;margin:6px 0 0;">${esc(tr('bmpl.registries'))}</h4>
        <ol class="bmpl-regs" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-reg-add" data-mode="bootstrap"
                  data-tip="${esc(tr('bmpl.t.regAddBootstrap'))}">${icon('add')} ${esc(tr('bmpl.regAddBootstrap'))}</button>
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-reg-add" data-mode="external"
                  data-tip="${esc(tr('bmpl.t.regAddExternal'))}">${icon('add')} ${esc(tr('bmpl.regAddExternal'))}</button>
        </div>
        <h4 style="grid-column:1/-1;margin:6px 0 0;">${esc(tr('bmpl.publications'))}</h4>
        <ol class="bmpl-pubs" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-pub-add"
                  data-tip="${esc(tr('bmpl.t.pubAdd'))}">${icon('add')} ${esc(tr('bmpl.pubAdd'))}</button>
        </div>`;
    } else if (pid === 'artefacts') {
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.artTimeout'))}">${esc(tr('bmpl.f.artTimeout'))}
          <input type="number" name="artefacts_timeout" min="${ARTEFACTS_TIMEOUT[0]}" max="${ARTEFACTS_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : ARTEFACTS_TIMEOUT[2])}"></label>
        <p class="form-hint bmpl-art-clusters-hint" style="grid-column:1/-1;"></p>
        <h4 style="grid-column:1/-1;margin:6px 0 0;">${esc(tr('bmpl.artPolicies'))}</h4>
        <ol class="bmpl-pols" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-pol-add"
                  data-tip="${esc(tr('bmpl.t.polAdd'))}">${icon('add')} ${esc(tr('bmpl.polAdd'))}</button>
        </div>
        <h4 style="grid-column:1/-1;margin:6px 0 0;">${esc(tr('bmpl.artItems'))}</h4>
        <ol class="bmpl-arts" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-art-add"
                  data-tip="${esc(tr('bmpl.t.artAdd'))}">${icon('add')} ${esc(tr('bmpl.artAdd'))}</button>
        </div>`;
    } else if (pid === 'gitops') {
      body = `<label class="tip" data-tip="${esc(tr('bmpl.t.gopTimeout'))}">${esc(tr('bmpl.f.gopTimeout'))}
          <input type="number" name="gitops_timeout" min="${GITOPS_TIMEOUT[0]}" max="${GITOPS_TIMEOUT[1]}" step="1"
                 value="${esc(ph && ph.timeout_minutes != null ? ph.timeout_minutes : GITOPS_TIMEOUT[2])}"></label>
        <p class="form-hint bmpl-gop-clusters-hint" style="grid-column:1/-1;"></p>
        <ol class="bmpl-gops" style="grid-column:1/-1;"></ol>
        <div class="apply-bar" style="grid-column:1/-1;">
          <button type="button" class="btn btn-secondary btn-sm btn-ico tip bmpl-gop-add"
                  data-tip="${esc(tr('bmpl.t.gopAdd', { max: MAX_GITOPS }))}">${icon('add')} ${esc(tr('bmpl.gopAdd'))}</button>
        </div>`;
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
    (edit.artItems || []).forEach((it, i) => {
      const before = edit.artItems.slice(0, i).map(x => x.item);
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
        <input class="bmpl-cl-regs tip" data-i="${i}" spellcheck="false" maxlength="200" value="${esc(it.registries || '')}"
               placeholder="${esc(tr('bmpl.clusterRegsPlaceholder'))}" aria-label="${esc(tr('bmpl.f.clusterRegs'))}" data-tip="${esc(tr('bmpl.t.clusterRegs'))}">
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
    paintArtHint();
    paintGitops();
  }

  // 2.6.0 : registres du plan et publications des paquets (phase Registre)
  function paintRegistries() {
    const ol = document.querySelector('#bm-plans .bmpl-regs');
    if (!ol) return;
    const rows = edit.registries;
    const inp = (i, k, v, tip, ph, type) => `<input class="bmpl-reg-f tip" data-i="${i}" data-k="${k}" spellcheck="false"
        ${type ? `type="${type}" min="5" max="2048"` : ''} value="${esc(v == null ? '' : v)}" placeholder="${esc(ph || '')}"
        aria-label="${esc(tip)}" data-tip="${esc(tip)}">`;
    ol.innerHTML = rows.length ? rows.map((r, i) => `<li class="bmpl-reg" data-i="${i}">
        ${inp(i, 'name', r.name, tr('bmpl.t.regName'), 'amorcage')}
        <span class="badge ${r.mode === 'bootstrap' ? 'info' : 'dim'}">${esc(r.mode === 'bootstrap' ? tr('bmpl.regBootstrap') : tr('bmpl.regExternal'))}</span>
        ${r.mode === 'bootstrap' ? `${inp(i, 'address', r.address, tr('bmpl.t.regAddress'), '{{registre_ip}}')}
          ${inp(i, 'size_gib', r.size_gib == null ? 20 : r.size_gib, tr('bmpl.t.regSize'), '20', 'number')}
          ${inp(i, 'storage_class', r.storage_class, tr('bmpl.t.regClass'), 'harv-rep1')}
          ${inp(i, 'mirrors', r.mirrors, tr('bmpl.t.regMirrors'), 'ghcr.io, docker.io')}`
          : `<span class="form-hint">${esc((regData.registries || []).some(x => x.name === r.name) ? tr('bmpl.regDeclared') : tr('bmpl.regUndeclared'))}</span>`}
        <button type="button" class="btn btn-sm btn-danger tip bmpl-reg-rm" data-i="${i}"
                aria-label="${esc(tr('bmpl.t.regRemove'))}" data-tip="${esc(tr('bmpl.t.regRemove'))}">${icon('close', { size: 12 })}</button>
      </li>`).join('') : `<li class="form-hint bmpl-no-reg">${esc(tr('bmpl.noRegistry'))}</li>`;
    const pol = document.querySelector('#bm-plans .bmpl-pubs');
    if (!pol) return;
    const regNames = [...new Set(rows.map(r => r.name).filter(Boolean).concat((regData.registries || []).map(x => x.name)))];
    const pkgs = regData.packages || [];
    pol.innerHTML = edit.publish.length ? edit.publish.map((p, i) => {
      const key = `${p.package}@${p.version}`;
      const known = pkgs.some(x => `${x.name}@${x.version}` === key);
      return `<li class="bmpl-pub" data-i="${i}">
        <select class="bmpl-pub-pkg tip" data-i="${i}" aria-label="${esc(tr('bmpl.t.pubPackage'))}" data-tip="${esc(tr('bmpl.t.pubPackage'))}">
          ${known || !p.package ? '' : `<option value="${esc(key)}" selected>${esc(`${p.package} ${p.version}`)} (${esc(tr('bmpl.pkgMissing'))})</option>`}
          ${pkgs.map(x => `<option value="${esc(`${x.name}@${x.version}`)}"${`${x.name}@${x.version}` === key ? ' selected' : ''}>${esc(`${x.name} ${x.version}`)}</option>`).join('')}</select>
        <select class="bmpl-pub-reg tip" data-i="${i}" aria-label="${esc(tr('bmpl.t.pubRegistry'))}" data-tip="${esc(tr('bmpl.t.pubRegistry'))}">
          ${regNames.map(n => `<option value="${esc(n)}"${n === p.registry ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select>
        <button type="button" class="btn btn-sm btn-danger tip bmpl-pub-rm" data-i="${i}"
                aria-label="${esc(tr('bmpl.t.pubRemove'))}" data-tip="${esc(tr('bmpl.t.pubRemove'))}">${icon('close', { size: 12 })}</button>
      </li>`;
    }).join('') : `<li class="form-hint bmpl-no-pub">${esc(tr('bmpl.noPublication'))}</li>`;
    const add = document.querySelector('#bm-plans .bmpl-pub-add');
    if (add) add.disabled = !pkgs.length || !regNames.length;
  }

  // 2.7.0 : phase Artefacts (politiques et accès, éléments de paquets)
  const listOf = (txt) => String(txt || '').split(',').map(x => x.trim()).filter(Boolean);
  const planClusterNames = () => (edit ? edit.clusters.map(x => x.name).filter(Boolean) : []);

  /** Éléments d'un paquet importé, lus une fois (`/api/artefacts/<nom>/<version>`). */
  function ensurePkgItems(name, version) {
    const key = `${name}@${version}`;
    if (!name || pkgItems.has(key)) return;
    pkgItems.set(key, 'loading');
    api('GET', `/api/artefacts/${enc(name)}/${enc(version)}`).then(r => {
      pkgItems.set(key, r.ok ? ((r.d.index || {}).items || []).map(i => ({ name: i.name, kind: i.kind })) : 'error');
    }).catch(() => { pkgItems.set(key, 'error'); }).then(() => {
      if (edit && document.querySelector('#bm-plans .bmpl-arts')) paintArtefacts();
      if (edit && document.querySelector('#bm-plans .bmpl-gops')) paintGitops();
    });
  }
  /** Sorte d'un élément d'une entrée, null si le paquet n'est pas lu. */
  function kindOf(it) {
    const list = pkgItems.get(`${it.package}@${it.version}`);
    if (!Array.isArray(list)) return null;
    const f = list.find(x => x.name === it.item);
    return f ? f.kind : null;
  }
  /** Éléments proposés : jamais `images` (phase Registre) ni `gitops` (2.8). */
  function offeredItems(it) {
    const list = pkgItems.get(`${it.package}@${it.version}`);
    return Array.isArray(list) ? list.filter(x => APPLICABLE_KINDS.includes(x.kind)) : null;
  }

  /** Noms de la phase Clusters proposés aux entrées (suivent la carte Clusters). */
  function paintArtHint() {
    const hint = document.querySelector('#bm-plans .bmpl-art-clusters-hint');
    if (!hint) return;
    const names = planClusterNames();
    hint.innerHTML = `${esc(tr('bmpl.artClustersHint'))} ${names.length ? names.map(n => `<code>${esc(n)}</code>`).join(' ')
      : `<em>${esc(tr('bmpl.artNoPlanCluster'))}</em>`}`;
  }

  // Repeindre retire le champ qui a le focus : son « change » de sortie
  // arrive PENDANT l'affectation d'innerHTML ; il ne doit pas repeindre
  // une seconde fois par-dessus (la valeur est déjà rangée).
  let artPainting = false;
  function paintArtefacts() {
    if (artPainting) return;
    artPainting = true;
    try { paintArtefactsNow(); } finally { artPainting = false; }
  }

  function paintArtefactsNow() {
    const ol = document.querySelector('#bm-plans .bmpl-arts');
    if (!ol || !edit) return;
    paintArtHint();
    // politiques et accès, une entrée par groupe de clusters
    const pol = document.querySelector('#bm-plans .bmpl-pols');
    if (pol) {
      const roleSel = (cls, i, j, val, roles, tip) => `<select class="${cls} tip" data-i="${i}" data-j="${j}" data-k="role"
          aria-label="${esc(tip)}" data-tip="${esc(tip)}">${roles.map(r => `<option value="${r}"${r === val ? ' selected' : ''}>${r}</option>`).join('')}</select>`;
      const nsSel = (cls, i, j, val, teams, tip) => `<select class="${cls} tip" data-i="${i}" data-j="${j}" data-k="namespace"
          aria-label="${esc(tip)}" data-tip="${esc(tip)}">${(val && !teams.includes(val) ? [val] : []).concat(teams)
            .map(n => `<option value="${esc(n)}"${n === val ? ' selected' : ''}>${esc(n)}</option>`).join('')
            || `<option value="">${esc(tr('bmpl.polNoTeam'))}</option>`}</select>`;
      pol.innerHTML = edit.artPolicies.length ? edit.artPolicies.map((p, i) => {
        const teams = listOf(p.teams);
        return `<li class="bmpl-pol" data-i="${i}">
          <input class="bmpl-pol-f tip" data-i="${i}" data-k="clusters" spellcheck="false" maxlength="400" value="${esc(p.clusters)}"
                 placeholder="{{cluster}}-apps" aria-label="${esc(tr('bmpl.t.polClusters'))}" data-tip="${esc(tr('bmpl.t.polClusters'))}">
          <label class="bm-check tip" data-tip="${esc(tr('bmpl.t.polBase'))}"><input type="checkbox" class="bmpl-pol-f" data-i="${i}" data-k="base"
                 ${p.base ? 'checked' : ''} aria-label="${esc(tr('bmpl.polBase'))}"> ${esc(tr('bmpl.polBase'))}</label>
          <input class="bmpl-pol-f tip" data-i="${i}" data-k="teams" spellcheck="false" maxlength="1100" value="${esc(p.teams)}"
                 placeholder="equipe-a, equipe-b" aria-label="${esc(tr('bmpl.t.polTeams'))}" data-tip="${esc(tr('bmpl.t.polTeams'))}">
          <button type="button" class="btn btn-sm btn-danger tip bmpl-pol-rm" data-i="${i}"
                  aria-label="${esc(tr('bmpl.t.polRemove'))}" data-tip="${esc(tr('bmpl.t.polRemove'))}">${icon('close', { size: 12 })}</button>
          <div class="bmpl-pol-sub" style="flex-basis:100%;">
            <span class="form-hint">${esc(tr('bmpl.polRoles'))}</span>
            <ul class="bmpl-pol-roles">${p.roles.map((r, j) => `<li class="bmpl-pol-role">
              ${nsSel('bmpl-pol-role-f', i, j, r.namespace, teams, tr('bmpl.t.polRoleNs'))}
              <input class="bmpl-pol-role-f tip" data-i="${i}" data-j="${j}" data-k="group" maxlength="128" spellcheck="false"
                     value="${esc(r.group || '')}" placeholder="devs" aria-label="${esc(tr('bmpl.t.polRoleGroup'))}" data-tip="${esc(tr('bmpl.t.polRoleGroup'))}">
              ${roleSel('bmpl-pol-role-f', i, j, r.role, GROUP_ROLES, tr('bmpl.t.polRoleRole'))}
              <button type="button" class="btn btn-sm btn-danger tip bmpl-pol-role-rm" data-i="${i}" data-j="${j}"
                      aria-label="${esc(tr('bmpl.t.polRoleRemove'))}" data-tip="${esc(tr('bmpl.t.polRoleRemove'))}">${icon('close', { size: 12 })}</button>
            </li>`).join('')}</ul>
            <button type="button" class="btn btn-sm btn-secondary btn-ico tip bmpl-pol-role-add" data-i="${i}" ${teams.length ? '' : 'disabled'}
                    data-tip="${esc(teams.length ? tr('bmpl.t.polRoleAdd') : tr('bmpl.t.polNeedTeam'))}">${icon('add', { size: 12 })} ${esc(tr('bmpl.polRoleAdd'))}</button>
            <span class="form-hint">${esc(tr('bmpl.polAccounts'))}</span>
            <ul class="bmpl-pol-accounts">${p.accounts.map((a, j) => `<li class="bmpl-pol-account">
              ${nsSel('bmpl-pol-acc-f', i, j, a.namespace, teams, tr('bmpl.t.polAccNs'))}
              <input class="bmpl-pol-acc-f tip" data-i="${i}" data-j="${j}" data-k="name" maxlength="63" spellcheck="false"
                     value="${esc(a.name || '')}" placeholder="ci" aria-label="${esc(tr('bmpl.t.polAccName'))}" data-tip="${esc(tr('bmpl.t.polAccName'))}">
              ${roleSel('bmpl-pol-acc-f', i, j, a.role, ACCOUNT_ROLES, tr('bmpl.t.polAccRole'))}
              <button type="button" class="btn btn-sm btn-danger tip bmpl-pol-acc-rm" data-i="${i}" data-j="${j}"
                      aria-label="${esc(tr('bmpl.t.polAccRemove'))}" data-tip="${esc(tr('bmpl.t.polAccRemove'))}">${icon('close', { size: 12 })}</button>
            </li>`).join('')}</ul>
            <button type="button" class="btn btn-sm btn-secondary btn-ico tip bmpl-pol-acc-add" data-i="${i}" ${teams.length ? '' : 'disabled'}
                    data-tip="${esc(teams.length ? tr('bmpl.t.polAccAdd') : tr('bmpl.t.polNeedTeam'))}">${icon('add', { size: 12 })} ${esc(tr('bmpl.polAccAdd'))}</button>
          </div>
        </li>`;
      }).join('') : `<li class="form-hint bmpl-no-pol">${esc(tr('bmpl.noPolicy'))}</li>`;
    }
    // éléments de paquets
    const pkgs = regData.packages || [];
    const regNames = [...new Set(edit.registries.map(r => r.name).filter(Boolean).concat((regData.registries || []).map(x => x.name)))];
    const items = edit.artItems;
    items.forEach(it => ensurePkgItems(it.package, it.version));
    ol.innerHTML = items.length ? items.map((it, i) => {
      const key = `${it.package}@${it.version}`;
      const known = pkgs.some(x => `${x.name}@${x.version}` === key);
      const offered = offeredItems(it);
      // paquet lu, aucun élément choisi : le premier applicable
      if (offered && !it.item && offered.length) it.item = offered[0].name;
      const kind = kindOf(it);
      const isChart = kind === 'chart' || (kind == null && !!(it.registry || it.valuesText));
      const itemOpts = offered == null
        ? `<option value="${esc(it.item)}" selected>${esc(it.item || tr('bmpl.artLoading'))}</option>`
        : (it.item && !offered.some(x => x.name === it.item) ? `<option value="${esc(it.item)}" selected>${esc(it.item)} (${esc(tr('bmpl.artItemMissing'))})</option>` : '')
          + offered.map(x => `<option value="${esc(x.name)}"${x.name === it.item ? ' selected' : ''}>${esc(x.name)} (${esc(x.kind)})</option>`).join('');
      const before = items.slice(0, i).map(x => x.item).filter(Boolean);
      const deps = before.length ? `<span class="bmpl-deps tip" data-tip="${esc(tr('bmpl.t.artDependsOn'))}">${esc(tr('bmpl.dependsOn'))}
          ${before.map(b => `<label class="bm-check"><input type="checkbox" class="bmpl-art-dep" data-i="${i}" value="${esc(b)}"
            ${it.depends_on.includes(b) ? 'checked' : ''}> <code>${esc(b)}</code></label>`).join(' ')}</span>` : '';
      return `<li class="bmpl-art" data-i="${i}" data-kind="${esc(kind || '')}">
        <select class="bmpl-art-pkg tip" data-i="${i}" aria-label="${esc(tr('bmpl.t.artPackage'))}" data-tip="${esc(tr('bmpl.t.artPackage'))}">
          ${known || !it.package ? '' : `<option value="${esc(key)}" selected>${esc(`${it.package} ${it.version}`)} (${esc(tr('bmpl.pkgMissing'))})</option>`}
          ${pkgs.map(x => `<option value="${esc(`${x.name}@${x.version}`)}"${`${x.name}@${x.version}` === key ? ' selected' : ''}>${esc(`${x.name} ${x.version}`)}</option>`).join('')}</select>
        <select class="bmpl-art-item tip" data-i="${i}" aria-label="${esc(tr('bmpl.t.artItem'))}" data-tip="${esc(tr('bmpl.t.artItem'))}">${itemOpts}</select>
        ${kind ? `<span class="badge dim bmpl-art-kind">${esc(kind)}</span>` : ''}
        ${isChart ? `<select class="bmpl-art-f bmpl-art-reg tip" data-i="${i}" data-k="registry" aria-label="${esc(tr('bmpl.t.artRegistry'))}"
                data-tip="${esc(tr('bmpl.t.artRegistry'))}"><option value="">${esc(tr('bmpl.artChooseRegistry'))}</option>
          ${(it.registry && !regNames.includes(it.registry) ? [it.registry] : []).concat(regNames)
            .map(n => `<option value="${esc(n)}"${n === it.registry ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select>` : ''}
        <input class="bmpl-art-f bmpl-art-clusters tip" data-i="${i}" data-k="clusters" spellcheck="false" maxlength="400" value="${esc(it.clusters)}"
               placeholder="{{cluster}}-apps, outils/outils" aria-label="${esc(tr('bmpl.t.artClusters'))}" data-tip="${esc(tr('bmpl.t.artClusters'))}">
        <button type="button" class="btn btn-sm btn-danger tip bmpl-art-rm" data-i="${i}"
                aria-label="${esc(tr('bmpl.t.artRemove'))}" data-tip="${esc(tr('bmpl.t.artRemove'))}">${icon('close', { size: 12 })}</button>
        ${isChart ? `<label class="tip" style="flex-basis:100%;" data-tip="${esc(tr('bmpl.t.artValues'))}">${esc(tr('bmpl.f.artValues'))}
          <textarea class="bmpl-art-f bmpl-art-values" data-i="${i}" data-k="valuesText" rows="3" spellcheck="false"
                    placeholder='{"replicaCount": 2}'>${esc(it.valuesText)}</textarea></label>` : ''}
        ${deps}
      </li>`;
    }).join('') : `<li class="form-hint bmpl-no-art">${esc(tr('bmpl.noArtefact'))}</li>`;
    const add = document.querySelector('#bm-plans .bmpl-art-add');
    if (add) {
      add.disabled = !pkgs.length;
      add.dataset.tip = pkgs.length ? tr('bmpl.t.artAdd') : tr('bmpl.t.artNoPackage');
    }
  }

  /** Refus de la phase Artefacts avant l'envoi (texte), ou ''. */
  function artefactsRefusal(on) {
    for (const it of edit.artItems) {
      const name = it.item || '?';
      if (!it.package || !it.item) return tr('bmpl.artItemRequired');
      if (it.valuesText.trim()) {
        let v;
        try { v = JSON.parse(it.valuesText); } catch { v = null; }
        if (!v || typeof v !== 'object' || Array.isArray(v)) return tr('bmpl.artValuesInvalid', { item: name });
      }
      if (on && kindOf(it) === 'chart' && !it.registry) return tr('bmpl.artRegistryRequired', { item: name });
      if (on && !listOf(it.clusters).length) return tr('bmpl.artClustersRequired', { item: name });
    }
    if (on && edit.artPolicies.some(p => !listOf(p.clusters).length)) return tr('bmpl.polClustersRequired');
    return '';
  }

  /** Phase Artefacts telle qu'enregistrée (format de deploy_plans). */
  function collectArtefacts(on, timeout) {
    return {
      enabled: on, timeout_minutes: timeout,
      policies: edit.artPolicies.map(p => {
        const teams = listOf(p.teams);
        return { clusters: listOf(p.clusters), base: !!p.base,
          teams: teams.map(ns => Object.assign({}, p.teamObjs[ns] || {}, { namespace: ns })),
          roles: p.roles.map(r => ({ namespace: r.namespace, group: (r.group || '').trim(), role: r.role })),
          accounts: p.accounts.map(a => ({ namespace: a.namespace, name: (a.name || '').trim(), role: a.role })) };
      }),
      items: edit.artItems.map(it => {
        const kind = kindOf(it);
        const out = { package: it.package, version: it.version, item: it.item, clusters: listOf(it.clusters) };
        // sorte inconnue (paquet absent) : registre et valeurs gardés tels quels
        if (it.registry && (kind === 'chart' || kind == null)) out.registry = it.registry;
        if (it.valuesText.trim() && (kind === 'chart' || kind == null)) out.values = JSON.parse(it.valuesText);
        if (it.depends_on.length) out.depends_on = it.depends_on.slice();
        return out;
      }),
    };
  }

  // =========================================================================
  // 2.8.0 : phase GitOps (entrées Flux ou Fleet posées sur les clusters)
  // =========================================================================
  /** Le modèle de cluster demande-t-il l'import dans un Rancher externe ?
   *  true/false, null tant que non lu (lu une fois, puis la carte se repeint). */
  function templateImports(name) {
    if (!name) return null;
    const v = tplAccess.get(name);
    if (v === true || v === false) return v;
    if (v === undefined) {
      tplAccess.set(name, 'loading');
      api('GET', `/api/baremetal/capi-templates/${enc(name)}`).then(r => {
        tplAccess.set(name, r.ok ? ((r.d.spec || {}).rancher_external === true) : 'error');
      }).catch(() => { tplAccess.set(name, 'error'); }).then(() => {
        if (edit && document.querySelector('#bm-plans .bmpl-gops')) paintGitops();
      });
    }
    return null;
  }

  /** Outils prévus d'une entrée : ['fleet'], ['flux'], ['?'] (décidé au
   *  contrôle : cluster hors du plan ou modèle non lu), ou plusieurs. */
  function gopTools(e) {
    if (e.tool !== 'auto') return [e.tool];
    const out = new Set();
    listOf(e.clusters).forEach(c => {
      const it = edit.clusters.find(x => x.name === c);
      const imp = it ? templateImports(it.template) : null;
      out.add(imp === true ? 'fleet' : imp === false ? 'flux' : '?');
    });
    return out.size ? [...out] : ['?'];
  }
  const GOP_TOOL_TEXT = {
    fleet: () => tr('bmpl.gopToolFleet'), flux: () => tr('bmpl.gopToolFlux'), '?': () => tr('bmpl.gopToolLater'),
  };
  const GOP_TOOL_OPT = {
    auto: () => tr('bmpl.gopTool.auto'), flux: () => tr('bmpl.gopTool.flux'), fleet: () => tr('bmpl.gopTool.fleet'),
  };
  const GOP_SRC_OPT = {
    oci: () => tr('bmpl.gopSrc.oci'), git: () => tr('bmpl.gopSrc.git'), chart: () => tr('bmpl.gopSrc.chart'),
  };
  const GOP_PRIV_OPT = { namespace: () => tr('bmpl.gopPriv.namespace'), cluster: () => tr('bmpl.gopPriv.cluster') };

  let gopPainting = false;
  function paintGitops() {
    if (gopPainting) return;
    gopPainting = true;
    try { paintGitopsNow(); } finally { gopPainting = false; }
  }

  function paintGitopsNow() {
    const ol = document.querySelector('#bm-plans .bmpl-gops');
    if (!ol || !edit) return;
    const hint = document.querySelector('#bm-plans .bmpl-gop-clusters-hint');
    if (hint) {
      const names = planClusterNames();
      hint.innerHTML = `${esc(tr('bmpl.gopClustersHint'))} ${names.length ? names.map(n => `<code>${esc(n)}</code>`).join(' ')
        : `<em>${esc(tr('bmpl.artNoPlanCluster'))}</em>`}`;
    }
    const pkgs = regData.packages || [];
    const regNames = [...new Set(edit.registries.map(r => r.name).filter(Boolean).concat((regData.registries || []).map(x => x.name)))];
    // `tip` : texte déjà traduit (un appel littéral par clé, contrôle de parité)
    const f = (i, k, v, tip, ph, extra) => `<input class="bmpl-gop-f tip" data-i="${i}" data-k="${k}" spellcheck="false"
        ${extra || ''} value="${esc(v == null ? '' : v)}" placeholder="${esc(ph || '')}" aria-label="${esc(tip)}" data-tip="${esc(tip)}">`;
    const sel = (i, k, v, opts, map, tip) => `<select class="bmpl-gop-f tip" data-i="${i}" data-k="${k}" aria-label="${esc(tip)}"
        data-tip="${esc(tip)}">${opts.map(o => `<option value="${esc(o)}"${o === v ? ' selected' : ''}>${esc(map[o]())}</option>`).join('')}</select>`;
    ol.innerHTML = edit.gitops.length ? edit.gitops.map((e, i) => {
      const tools = gopTools(e);
      let src = '';
      if (e.stype === 'git') {
        src = `${f(i, 'url', e.url, tr('bmpl.t.gopUrl'), 'ssh://git@git.example:22/team/apps.git', 'maxlength="2048"')}
          ${f(i, 'branch', e.branch, tr('bmpl.t.gopBranch'), 'main', 'maxlength="100"')}
          ${f(i, 'path', e.path, tr('bmpl.t.gopPath'), './', 'maxlength="200"')}
          ${f(i, 'secret', e.secret, tr('bmpl.t.gopSecret'), tr('bmpl.gopSecretPh'), 'maxlength="63"')}`;
      } else {
        const want = e.stype === 'oci' ? 'gitops' : 'chart';
        const key = `${e.package}@${e.version}`;
        if (e.package) ensurePkgItems(e.package, e.version);
        const list = pkgItems.get(key);
        const offered = Array.isArray(list) ? list.filter(x => x.kind === want) : null;
        if (offered && !e.item && offered.length) e.item = offered[0].name;
        const known = pkgs.some(x => `${x.name}@${x.version}` === key);
        const itemOpts = offered == null
          ? `<option value="${esc(e.item)}" selected>${esc(e.item || (e.package ? tr('bmpl.artLoading') : tr('bmpl.gopPickPackage')))}</option>`
          : (e.item && !offered.some(x => x.name === e.item) ? `<option value="${esc(e.item)}" selected>${esc(e.item)} (${esc(tr('bmpl.artItemMissing'))})</option>` : '')
            + (offered.length ? offered.map(x => `<option value="${esc(x.name)}"${x.name === e.item ? ' selected' : ''}>${esc(x.name)}</option>`).join('')
              : (e.item ? '' : `<option value="">${esc(e.stype === 'oci' ? tr('bmpl.gopNoGitopsItem') : tr('bmpl.gopNoChartItem'))}</option>`));
        src = `<select class="bmpl-gop-pkg tip" data-i="${i}" aria-label="${esc(tr('bmpl.t.gopPackage'))}" data-tip="${esc(tr('bmpl.t.gopPackage'))}">
            ${e.package ? '' : `<option value="" selected>${esc(tr('bmpl.gopPickPackage'))}</option>`}
            ${known || !e.package ? '' : `<option value="${esc(key)}" selected>${esc(`${e.package} ${e.version}`)} (${esc(tr('bmpl.pkgMissing'))})</option>`}
            ${pkgs.map(x => `<option value="${esc(`${x.name}@${x.version}`)}"${`${x.name}@${x.version}` === key ? ' selected' : ''}>${esc(`${x.name} ${x.version}`)}</option>`).join('')}</select>
          <select class="bmpl-gop-f tip" data-i="${i}" data-k="item" aria-label="${esc(tr('bmpl.t.gopItem'))}" data-tip="${esc(tr('bmpl.t.gopItem'))}">${itemOpts}</select>
          ${f(i, 'registry', e.registry, tr('bmpl.t.gopRegistry'), '{{registre}}', `list="bmpl-gop-regs" maxlength="128"`)}`;
      }
      return `<li class="bmpl-gop" data-i="${i}" data-source="${esc(e.stype)}">
        <div class="bmpl-gop-line">
          ${f(i, 'name', e.name, tr('bmpl.t.gopName'), 'apps', 'maxlength="34"')}
          ${sel(i, 'tool', e.tool, GITOPS_TOOLS, GOP_TOOL_OPT, tr('bmpl.t.gopTool'))}
          <span class="badge info tip bmpl-gop-tool" data-tool="${esc(tools.join(','))}" data-tip="${esc(tr('bmpl.t.gopDeduced'))}">${esc(tools.map(t => GOP_TOOL_TEXT[t]()).join(' / '))}</span>
          ${f(i, 'clusters', e.clusters, tr('bmpl.t.gopClusters'), '{{cluster}}-apps', 'maxlength="400"')}
          ${f(i, 'target_namespace', e.target_namespace, tr('bmpl.t.gopNamespace'), 'equipe-a', 'maxlength="63"')}
          <button type="button" class="btn btn-sm btn-danger tip bmpl-gop-rm" data-i="${i}"
                  aria-label="${esc(tr('bmpl.t.gopRemove'))}" data-tip="${esc(tr('bmpl.t.gopRemove'))}">${icon('close', { size: 12 })}</button>
        </div>
        <div class="bmpl-gop-line">
          ${sel(i, 'stype', e.stype, GITOPS_SOURCES, GOP_SRC_OPT, tr('bmpl.t.gopSource'))}
          ${src}
        </div>
        <div class="bmpl-gop-line">
          ${sel(i, 'privileges', e.privileges, ['namespace', 'cluster'], GOP_PRIV_OPT, tr('bmpl.t.gopPrivileges'))}
          <label class="tip bmpl-gop-num" data-tip="${esc(tr('bmpl.t.gopInterval'))}">${esc(tr('bmpl.f.gopInterval'))}
            <input type="number" class="bmpl-gop-f" data-i="${i}" data-k="interval" min="${GITOPS_INTERVAL[0]}" max="${GITOPS_INTERVAL[1]}"
                   step="1" value="${esc(e.interval)}" aria-label="${esc(tr('bmpl.f.gopInterval'))}"></label>
          <label class="bm-check tip" data-tip="${esc(tr('bmpl.t.gopPrune'))}"><input type="checkbox" class="bmpl-gop-f" data-i="${i}" data-k="prune"
                 ${e.prune ? 'checked' : ''} aria-label="${esc(tr('bmpl.gopPrune'))}"> ${esc(tr('bmpl.gopPrune'))}</label>
        </div>
      </li>`;
    }).join('') + `<datalist id="bmpl-gop-regs">${regNames.map(n => `<option value="${esc(n)}">`).join('')}</datalist>`
      : `<li class="form-hint bmpl-no-gop">${esc(tr('bmpl.noGitops'))}</li>`;
    const add = document.querySelector('#bm-plans .bmpl-gop-add');
    if (add) add.disabled = edit.gitops.length >= MAX_GITOPS;
  }

  /** Refus de la phase GitOps avant l'envoi (texte), ou ''. */
  function gitopsRefusal(on) {
    for (const e of edit.gitops) {
      const name = e.name || '?';
      if (!GITOPS_NAME_RE.test(e.name)) return tr('bmpl.gopNameBad', { max: 34 });
      if (edit.gitops.filter(x => x.name === e.name).length > 1) return tr('bmpl.gopNameTwice', { name });
      const n = Number(e.interval);
      if (!Number.isInteger(n) || n < GITOPS_INTERVAL[0] || n > GITOPS_INTERVAL[1]) {
        return tr('bmpl.gopIntervalBad', { name, min: GITOPS_INTERVAL[0], max: GITOPS_INTERVAL[1] });
      }
      if (!on) continue;
      if (!listOf(e.clusters).length) return tr('bmpl.gopClustersRequired', { name });
      if (!e.target_namespace.trim()) return tr('bmpl.gopNamespaceRequired', { name });
      if (e.stype === 'git') {
        if (!/^(https|ssh):\/\/\S+$/.test(e.url.trim())) return tr('bmpl.gopUrlBad', { name });
      } else if (!e.package || !e.item || !e.registry.trim()) {
        return tr('bmpl.gopSourceRequired', { name });
      }
    }
    return '';
  }

  /** Phase GitOps telle qu'enregistrée (format de deploy_plans). */
  function collectGitops(on, timeout) {
    return {
      enabled: on, timeout_minutes: timeout,
      entries: edit.gitops.map(e => {
        const src = e.stype === 'git'
          ? Object.assign({ type: 'git', url: e.url.trim() }, e.branch.trim() ? { branch: e.branch.trim() } : {},
            e.path.trim() ? { path: e.path.trim() } : {}, e.secret.trim() ? { secret: e.secret.trim() } : {})
          : { type: e.stype, package: e.package, version: e.version, item: e.item, registry: e.registry.trim() };
        return Object.assign({}, e.raw, {
          name: e.name.trim(), tool: e.tool, clusters: listOf(e.clusters), target_namespace: e.target_namespace.trim(),
          source: src, privileges: e.privileges, interval_minutes: Number(e.interval), prune: !!e.prune,
        });
      }),
    };
  }

  /** Saisie dans la carte GitOps ; true si l'élément en relevait. */
  function gitopsChange(t) {
    const pkg = t.closest('#bm-plans .bmpl-gop-pkg');
    if (pkg) {
      const e = edit.gitops[Number(pkg.dataset.i)];
      if (!e) return true;
      const k = pkg.value.lastIndexOf('@');
      Object.assign(e, k < 0 ? { package: '', version: '' } : { package: pkg.value.slice(0, k), version: pkg.value.slice(k + 1) }, { item: '' });
      paintGitops();
      return true;
    }
    const gf = t.closest('#bm-plans .bmpl-gop-f');
    if (!gf) return false;
    const e = edit.gitops[Number(gf.dataset.i)];
    if (!e) return true;
    const k = gf.dataset.k;
    e[k] = gf.type === 'checkbox' ? gf.checked : gf.value;
    // une autre source : ses champs propres repartent de zéro
    if (k === 'stype') Object.assign(e, { package: '', version: '', item: '', registry: '', url: '', branch: '', path: '', secret: '' });
    if (['stype', 'tool', 'clusters'].includes(k)) paintGitops();
    return true;
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
          items: edit.clusters.map(it => {
            const regs = (it.registries || '').split(',').map(x => x.trim()).filter(Boolean);
            return Object.assign({ template: it.template, name: it.name.trim() },
              it.depends_on.length ? { depends_on: it.depends_on.slice() } : {}, regs.length ? { registries: regs } : {});
          }) };
        if (edit.shortcut) Object.assign(phases.clusters, edit.shortcut);
      } else if (pid === 'registry') {
        phases.registry = { enabled: on('registry'),
          timeout_minutes: Number(form.querySelector('[name="registry_timeout"]').value),
          publish_timeout_minutes: Number(form.querySelector('[name="publish_timeout"]').value),
          registries: edit.registries.map(r => (r.mode === 'bootstrap'
            ? Object.assign({ name: r.name.trim(), mode: 'bootstrap', address: (r.address || '').trim(), size_gib: Number(r.size_gib) || 20,
              storage_class: (r.storage_class || '').trim(),
              mirrors: (r.mirrors || '').split(',').map(x => x.trim()).filter(Boolean) }, r.dns_name ? { dns_name: r.dns_name } : {})
            : { name: r.name.trim(), mode: 'external' })),
          publish: edit.publish.filter(p => p.package && p.registry).map(p => ({ package: p.package, version: p.version, registry: p.registry })) };
      } else if (pid === 'artefacts') {
        phases.artefacts = collectArtefacts(on('artefacts'), Number(form.querySelector('[name="artefacts_timeout"]').value));
      } else if (pid === 'gitops') {
        phases.gitops = collectGitops(on('gitops'), Number(form.querySelector('[name="gitops_timeout"]').value));
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
    for (const [field, [lo, hi]] of [['capi_timeout', CAPI_TIMEOUT], ['clusters_timeout', CLUSTERS_TIMEOUT],
      ['registry_timeout', REGISTRY_TIMEOUT], ['publish_timeout', PUBLISH_TIMEOUT], ['artefacts_timeout', ARTEFACTS_TIMEOUT],
      ['gitops_timeout', GITOPS_TIMEOUT]]) {
      const el = form.querySelector(`[name="${field}"]`);
      const v = el ? Number(el.value) : lo;
      if (!Number.isInteger(v) || v < lo || v > hi) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('bmpl.badTimeoutRange', { min: lo, max: hi }))}</div>`;
        return;
      }
    }
    // 2.7.0 : registre d'un chart et valeurs JSON contrôlés avant l'envoi
    if (form.querySelector('[name="artefacts_timeout"]')) {
      const why = artefactsRefusal(!!form.querySelector('.bmpl-phase-on[data-phase="artefacts"]:checked'));
      if (why) {
        msg.innerHTML = `<div class="bm-refusal bmpl-art-refusal">${icon('fail', { size: 14 })} ${esc(why)}</div>`;
        msg.scrollIntoView({ block: 'nearest' });
        return;
      }
    }
    // 2.8.0 : entrées GitOps contrôlées avant l'envoi
    if (form.querySelector('[name="gitops_timeout"]')) {
      const why = gitopsRefusal(!!form.querySelector('.bmpl-phase-on[data-phase="gitops"]:checked'));
      if (why) {
        msg.innerHTML = `<div class="bm-refusal bmpl-gop-refusal">${icon('fail', { size: 14 })} ${esc(why)}</div>`;
        msg.scrollIntoView({ block: 'nearest' });
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
  // Suivi d'une action de déroulé (Déployer, Défaire) : étapes et filles liées
  // =========================================================================
  function stepLabel(stepId) {
    const sid = String(stepId || '');
    if (sid.startsWith('phase-')) return text(PHASES, sid.slice(6));
    if (sid.startsWith('tf-')) return tr('dpw.stepTemplate', { name: sid.slice(3) });
    if (sid.startsWith('ready-')) return tr('dpw.stepReady', { code: sid.slice(6) });
    if (sid.startsWith('k8s-')) return tr('dpw.stepCluster', { name: sid.slice(4) });
    if (sid === 'capi-providers') return tr('dpw.stepProviders');
    // 2.7.0 : éléments de la phase Artefacts et du défaire
    if (sid.startsWith('art-')) return tr('dpw.stepArtefact', { name: sid.slice(4) });
    if (sid.startsWith('pol-')) return tr('dpw.stepPolicies', { name: sid.slice(4) });
    if (sid.startsWith('undo-')) return tr('udw.stepUndo', { name: sid.slice(5) });
    return sid;
  }

  /** Suit `actionId` dans le bloc `.dpw-follow` de `form` ; `s.sse` gardé
   *  pour la fermeture de la fenêtre. */
  function followRun(form, s, actionId, onEnd) {
    const $ = (sel) => form.querySelector(sel);
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
          if (onEnd) onEnd();
        },
      },
    });
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
      // 2.8.0 : secrets du plan demandés par la phase GitOps (noms lus dans
      // le plan ; valeurs seulement dans les champs, jamais rangées)
      gopSecrets: [], gopPlan: null,
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
            <fieldset class="dpw-gop-secrets" hidden>
              <legend>${esc(tr('dpw.fs.gopSecrets'))}</legend>
              <p class="form-hint" style="grid-column:1/-1;">${esc(tr('dpw.gopSecretsHint'))}</p>
              <div class="dpw-gop-secrets-box" style="grid-column:1/-1;"></div>
            </fieldset>
            ${resume ? `<div class="dpw-kept" hidden>
              <p class="form-hint">${icon('lock', { size: 13 })} <span class="dpw-kept-text">${esc(tr('dpw.secretsKept'))}</span></p>
              <div class="apply-bar">
                <button type="button" class="btn btn-secondary btn-sm tip dpw-type-secrets" data-tip="${esc(tr('dpw.t.typeSecrets'))}">${esc(tr('dpw.typeSecrets'))}</button>
                <button type="button" class="btn btn-secondary btn-sm tip dpw-forget needs-admin" data-tip="${esc(tr('dpw.t.forgetSecrets'))}">${icon('delete', { size: 12 })} ${esc(tr('dpw.forgetSecrets'))}</button>
              </div></div>` : ''}
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
    const sig = () => JSON.stringify([s.cluster, s.plan, s.variables, s.updateDiffering, Object.keys(gopValues()).sort()]);

    // --- 2.8.0 : secrets Git de la phase GitOps ------------------------------
    /** Secrets complets saisis : {nom: {identity[, known_hosts]} | {username, password}}. */
    function gopValues() { return gopRead(form); }
    function gopClear() { gopWipe(form); }
    function paintGopSecrets() {
      const fs = $('.dpw-gop-secrets');
      fs.hidden = !s.gopSecrets.length;
      const box = $('.dpw-gop-secrets-box');
      const have = [...box.querySelectorAll('.dpw-gop-secret')].map(b => b.dataset.gopSecret);
      if (JSON.stringify(have) === JSON.stringify(s.gopSecrets)) return;   // les saisies restent
      box.innerHTML = gopSecretsHtml(s.gopSecrets);
    }
    /** Noms des secrets demandés par la phase GitOps du plan choisi. */
    async function loadGopSecrets() {
      const name = s.plan;
      if (!name) { s.gopSecrets = []; paintGopSecrets(); return; }
      let doc = null;
      try { const r = await api('GET', `/api/baremetal/plans/${enc(name)}`); doc = r.ok ? r.d : null; } catch { doc = null; }
      if (name !== s.plan || !form.isConnected) return;
      s.gopSecrets = gopSecretNames(doc);
      paintGopSecrets();
    }

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
        ${(sum.registries || []).length ? `<table class="data-table dpw-registries">
          <thead><tr><th>${esc(tr('dpw.col.registry'))}</th><th>${esc(tr('dpw.col.address'))}</th><th>${esc(tr('dpw.col.size'))}</th><th>${esc(tr('dpw.col.state'))}</th></tr></thead>
          <tbody>${sum.registries.map(r => `<tr><td><code class="dpw-registry-name">${esc(r.name)}</code>
            <span class="badge dim">${esc(r.mode === 'bootstrap' ? tr('bmpl.regBootstrap') : tr('bmpl.regExternal'))}</span></td>
            <td><code>${esc(r.address || '')}</code></td><td>${r.size_gib ? esc(`${r.size_gib} GiB`) : ''}</td>
            <td>${r.exists ? `<span class="badge ok">${esc(tr('dpw.registryPresent'))}</span>` : `<span class="badge info">${esc(tr('dpw.registryNew'))}</span>`}</td></tr>`).join('')}</tbody></table>` : ''}
        ${(sum.publish || []).length ? `<table class="data-table dpw-publish">
          <thead><tr><th>${esc(tr('dpw.col.package'))}</th><th>${esc(tr('dpw.col.registry'))}</th><th>${esc(tr('dpw.col.size'))}</th></tr></thead>
          <tbody>${sum.publish.map(p => `<tr><td><code class="dpw-package-name">${esc(`${p.package} ${p.version}`)}</code></td>
            <td><code>${esc(p.registry)}</code></td><td>${p.size == null ? esc(tr('bmpl.pkgMissing')) : esc(bytesOf(p.size))}</td></tr>`).join('')}</tbody></table>` : ''}
        ${sum.capi ? `<p class="dpw-capi">${esc(tr('dpw.capiSummary'))} ${(sum.capi.providers || []).map(v => `<code>${esc(v)}</code>`).join(' ')}
          <span class="badge ${sum.capi.ready ? 'ok' : 'info'}">${esc(sum.capi.ready ? tr('dpw.capiReady') : tr('dpw.capiInstall'))}</span></p>` : ''}
        ${(sum.clusters || []).length ? `<table class="data-table dpw-clusters">
          <thead><tr><th>${esc(tr('dpw.col.cluster'))}</th><th>${esc(tr('dpw.col.template'))}</th><th>${esc(tr('dpw.col.vms'))}</th><th>${esc(tr('dpw.col.state'))}</th></tr></thead>
          <tbody>${sum.clusters.map(c => `<tr><td><code class="dpw-cluster-name">${esc(c.name)}</code></td><td><code>${esc(c.template)}</code></td>
            <td>${c.vms == null ? '' : esc(c.vms)}</td>
            <td>${c.exists ? `<span class="badge ok">${esc(tr('dpw.clusterPresent'))}</span>` : `<span class="badge info">${esc(tr('dpw.clusterNew'))}</span>`}</td></tr>`).join('')}</tbody></table>` : ''}
        ${(sum.artefacts || []).length ? `<h4 class="dpw-artefacts-title">${esc(tr('bmpl.phase.artefacts'))}</h4><table class="data-table dpw-artefacts">
          <thead><tr><th>${esc(tr('dpw.col.item'))}</th><th>${esc(tr('dpw.col.kind'))}</th><th>${esc(tr('dpw.col.cluster'))}</th>
            <th>${esc(tr('dpw.col.package'))}</th><th>${esc(tr('dpw.col.registry'))}</th></tr></thead>
          <tbody>${sum.artefacts.map(a => `<tr data-id="${esc(a.id)}"><td><code class="dpw-artefact-item">${esc(a.type === 'policies' ? tr('dpw.artPolicies') : a.item)}</code></td>
            <td><span class="badge dim">${esc(a.type)}</span></td><td><code>${esc(a.ref)}</code></td>
            <td>${a.package ? `<code>${esc(`${a.package} ${a.version || ''}`)}</code>` : ''}</td>
            <td>${a.registry ? `<code>${esc(a.registry)}</code>` : ''}</td></tr>`).join('')}</tbody></table>` : ''}
        ${(sum.gitops || []).length ? `<h4 class="dpw-gitops-title">${esc(tr('bmpl.phase.gitops'))}</h4><table class="data-table dpw-gitops">
          <thead><tr><th>${esc(tr('dpw.col.entry'))}</th><th>${esc(tr('dpw.col.cluster'))}</th><th>${esc(tr('dpw.col.tool'))}</th>
            <th>${esc(tr('dpw.col.source'))}</th></tr></thead>
          <tbody>${sum.gitops.map(g => `<tr data-id="${esc(g.id)}"><td><code class="dpw-gitops-entry">${esc(g.entry)}</code></td>
            <td><code>${esc(g.ref)}</code></td>
            <td>${g.tool ? `<span class="badge info">${esc(g.tool === 'fleet' ? tr('bmpl.gopToolFleet') : g.tool === 'flux' ? tr('bmpl.gopToolFlux') : g.tool)}</span>`
              : `<span class="badge dim dpw-gitops-later">${esc(tr('bmpl.gopToolLater'))}</span>`}</td>
            <td><span class="badge dim">${esc(g.source)}</span></td></tr>`).join('')}</tbody></table>` : ''}`;
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
      const gsec = gopValues();
      if (Object.keys(gsec).length) body.gitops_secrets = gsec;
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
    function follow(actionId) {
      s.launched = actionId;
      paintLaunch();
      followRun(form, s, actionId);
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
        const gsec = gopValues();
        if (Object.keys(gsec).length) body.gitops_secrets = gsec;
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
        const gsec = gopValues();
        if (Object.keys(gsec).length) body.gitops_secrets = gsec;
        r = await api('POST', `/api/deploy/${enc(s.cluster)}`, body);
      }
      out.textContent = '';
      if (r.ok && r.d.action_id) {
        gopClear();                               // jamais réaffichés après le départ
        // constats rendus au départ (plan changé depuis le déroulé repris...)
        const notes = (r.d.findings || []).filter(f => f && f.level !== 'info');
        msg.innerHTML = okLine(dryRun ? tr('dpw.dryStarted') : tr('dpw.started')) + (notes.length ? findingsHtml(notes) : '')
          + (r.d.secrets_source === 'openbao' ? `<p class="form-hint" data-dpw-source="openbao">${icon('lock', { size: 12 })} ${esc(tr('dpw.secretsFromOpenbao'))}</p>` : '');
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
      // 2.9.0 (décision 141) : OpenBao ne répond pas, la reprise redemande
      // les secrets à taper (comportement 2.4.0)
      if (r.status === 503 && r.d.secrets_prompt) {
        const git = (r.d.fields || []).includes('gitops_secrets');
        if (!git) { s.needSecrets = true; $('.dpw-secrets').hidden = false; }
        const kept = $('.dpw-kept');
        if (kept) kept.hidden = true;
        setTab('params');
        msg.innerHTML = `<div class="bm-refusal" data-dpw-prompt="${git ? 'gitops' : 'install'}">${icon('warn', { size: 14 })} ${esc(tr('dpw.openbaoDown', { code: r.d.code || '?' }))}</div>`;
        const first = form.querySelector(git ? '.dpw-gop-secret' : '[data-dpw-secret]');
        if (first) first.focus();
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
      if (r.d.code === 'renew-running' && r.d.running) {
        // revue 5-8 I-9 : un renouvellement de machines tient ce cluster
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.renewRunning'))} ${actionLink(r.d.running)}</div>`;
        return;
      }
      msg.innerHTML = refusal(r.d, r.status);
    }

    form.addEventListener('click', (e) => {
      const t = e.target.closest('[data-dpw-tab]');
      if (t) { setTab(t.dataset.dpwTab); return; }
      if (e.target.closest('.dpw-recheck')) { runCheck(); return; }
      if (e.target.closest('.dpw-dry')) { launch(true); return; }
      if (e.target.closest('.dpw-type-secrets')) {
        s.needSecrets = true;
        $('.dpw-secrets').hidden = false;
        const first = form.querySelector('[data-dpw-secret]');
        if (first) first.focus();
        return;
      }
      if (e.target.closest('.dpw-forget')) { forgetSecrets(); }
    });
    // 2.9.0 (décision 141, revue m7) : secrets gardés dans OpenBao jusqu'à la
    // fin du déroulé ; un administrateur peut les oublier tout de suite
    async function forgetSecrets() {
      if (!confirm(tr('dpw.forgetConfirm', { id: resume.id }))) return;
      const r = await api('POST', `/api/deploy/${enc(s.cluster)}/forget-secrets`, { action_id: resume.id });
      const msg = $('.dpw-msg');
      if (r.ok && r.d.action_id) {
        msg.innerHTML = okLine(tr('dpw.forgetStarted'));
        $('.dpw-kept').hidden = true;
        s.needSecrets = !!res.install;
        $('.dpw-secrets').hidden = !s.needSecrets;
        if (window.Dock && Dock.poll) Dock.poll();
      } else {
        msg.innerHTML = refusal(r.d, r.status);
      }
    }
    if (resume) {
      api('GET', '/api/secrets/banner').then((b) => {
        if (!b.ok || !b.d.active || !form.isConnected) return;
        const kept = $('.dpw-kept');
        if (kept) kept.hidden = false;
        if (res.install) { s.needSecrets = false; $('.dpw-secrets').hidden = true; }
        if (!can('admin')) { const f = $('.dpw-forget'); if (f) f.hidden = true; }
      }).catch(() => {});
    }
    form.addEventListener('change', (e) => {
      if (e.target.classList.contains('dpw-update')) { s.updateDiffering = e.target.checked; runCheck(); return; }
      if (gopKindChange(e.target)) { later(); return; }
      if (e.target.name === 'cluster') { s.cluster = e.target.value; later(); }
      if (e.target.name === 'plan') {
        s.plan = e.target.value;
        const p = planOf(s.plan);
        const keep = {};
        (p ? p.variables || [] : []).forEach(v => { if (s.variables[v] != null) keep[v] = s.variables[v]; });
        s.variables = keep;
        paintVars();
        loadGopSecrets();
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
      if (e.target.closest('.dpw-gop-secret')) later();
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
    loadGopSecrets();
    // Reprise sans plan connu : la console revérifie seule
    if (resume && !s.plan) { s.check = { sig: sig(), error: tr('dpw.noPlanKnown') }; paintChecks(); } else runCheck();
    paintChecks();
    return panel;
  }

  // =========================================================================
  // Fenêtre « Défaire le déploiement » (2.7.0, décision 128)
  // =========================================================================
  // ordre de retrait de deploy_undo.KINDS_ORDER (2.8.0 : gitops, rancher-import ; 2.10.0 : eso)
  const UNDO_KINDS = ['gitops', 'eso', 'chart', 'manifest', 'policies', 'rancher-import', 'capi-cluster', 'registry', 'tf-declaration'];
  const UNDO_KIND_TEXT = {
    chart: () => tr('udw.kind.chart'), manifest: () => tr('udw.kind.manifest'), policies: () => tr('udw.kind.policies'),
    'capi-cluster': () => tr('udw.kind.capiCluster'), registry: () => tr('udw.kind.registry'),
    'tf-declaration': () => tr('udw.kind.tfDeclaration'),
    gitops: () => tr('udw.kind.gitops'), 'rancher-import': () => tr('udw.kind.rancherImport'),
    eso: () => tr('udw.kind.eso'),
  };
  const UNDO_LIMITS = {
    'volumes-claimed': () => tr('udw.limit.volumesClaimed'), 'lb-dhcp': () => tr('udw.limit.lbDhcp'),
    'ccm-kubernetes': () => tr('udw.limit.ccmKubernetes'), 'release-namespace': () => tr('udw.limit.releaseNamespace'),
  };
  const UNDONE = {
    done: () => tr('udw.badge.done'), partial: () => tr('udw.badge.partial'),
    error: () => tr('udw.badge.error'), cancelled: () => tr('udw.badge.cancelled'),
  };
  const RUNNING_STATES = ['starting', 'running'];
  // note du serveur (deploy_undo.MODIFIED) : déclaration changée depuis
  const MODIFIED_NOTE = 'changed since';

  /** Pourquoi un élément est proposé décoché ('' s'il est coché par défaut). */
  function undoOffReason(u) {
    if (u.kind === 'registry') return tr('udw.t.registryOff');
    if (u.proven === false) return tr('udw.t.notProven');
    if (String(u.note || '').includes(MODIFIED_NOTE)) return tr('udw.t.modified');
    return u.default === false ? tr('udw.t.off') : '';
  }

  /** Bouton « Défaire » d'un déroulé fini (pas un essai, pas un défaire :
   *  point 214, il n'y a pas de défaire du défaire). Administrateur. */
  function undoButton(a) {
    if (!a || !String(a.action || '').startsWith('deploy:') || a.dry_run || RUNNING_STATES.includes(a.status)) return '';
    const res = a.result || {};
    const admin = can('admin');
    const done = res.undone_status === 'done';
    const tp = !admin ? tr('udw.t.adminOnly') : done ? tr('udw.t.alreadyUndone') : tr('udw.t.undoBtn');
    return ` <button type="button" class="btn btn-sm btn-danger btn-ico tip bm-deploy-undo" data-action-id="${esc(a.id)}"
      ${admin && !done ? '' : 'disabled'} title="${esc(tp)}" data-tip="${esc(tp)}" aria-label="${esc(tp)}">${icon('undo', { size: 12 })} ${esc(tr('udw.undo'))}</button>`;
  }

  /** Badge « Défait » / « Défait en partie » d'un déroulé, lien vers le défaire. */
  function undoneBadge(a) {
    const res = (a && a.result) || {};
    if (!a || !String(a.action || '').startsWith('deploy:') || !res.undone_status) return '';
    const cls = res.undone_status === 'done' ? 'ok' : res.undone_status === 'error' ? 'fail' : 'warn';
    const tp = tr('udw.t.badge', { id: res.undone_by || '' });
    return ` <a class="badge ${cls} tip bm-undone" data-undone="${esc(res.undone_status)}" href="#activity/action/${enc(res.undone_by || '')}"
      data-tip="${esc(tp)}">${esc(text(UNDONE, res.undone_status))}</a>`;
  }

  function objText(o) {
    if (o == null) return '';
    if (typeof o !== 'object') return String(o);
    if (o.kind || o.name) return `${o.kind || ''} ${o.namespace ? `${o.namespace}/` : ''}${o.name || ''}`.trim();
    return JSON.stringify(o);
  }

  /** 2.8.2 : Bundle d'agent que Rancher laisse dans Fleet (essai
   *  `agent_bundle` de rancher-unimport) ; rien s'il n'y en a pas. */
  function undoBundleHtml(b) {
    if (!b || b.present === false) return '';
    const v = { name: b.name || '' };
    const [cls, txt] = !b.name ? ['udw-bundle-not-looked', tr('udw.agentBundleNotLooked')]
      : b.delete ? ['udw-bundle-goes', tr('udw.agentBundleGoes', v)]
        : b.present ? ['udw-bundle-kept', tr('udw.agentBundleKept', v)] : ['udw-bundle-unread', tr('udw.agentBundleUnread', v)];
    return `<p class="form-hint tip udw-bundle ${cls}" data-tip="${esc(tr('udw.t.agentBundle'))}">${esc(txt)}</p>`;
  }

  /** Ce qui partira pour un élément du plan de destruction (échappé). */
  function undoDetailsHtml(u) {
    const d = u.details || {};
    const li = (arr) => (arr || []).map(x => `<li><code>${esc(objText(x))}</code></li>`).join('');
    if (d.error) return `<div class="bm-refusal">${icon('fail', { size: 13 })} ${esc(tr('udw.detailError'))} ${esc(d.error)}</div>`;
    if (d.present === false) return `<p class="form-hint udw-gone">${esc(tr('udw.gone'))}</p>`;
    // revue 4-8 C1 : recréé depuis par un autre déroulé ou à la main, jamais touché
    if (d.not_ours) {
      return `<div class="bm-refusal bm-warning udw-not-ours">${icon('warn', { size: 13 })} ${esc(d.recreated_by
        ? tr('udw.notOursRun', { id: d.recreated_by }) : tr('udw.notOurs'))}</div>`;
    }
    if (u.kind === 'chart') {
      return `<p>${esc(tr('udw.release', { name: d.release || '', ns: d.release_namespace || '' }))}
        ${d.chart ? `<code>${esc(`${d.chart} ${d.version || ''}`)}</code>` : ''}</p>`;
    }
    if (u.kind === 'manifest' || u.kind === 'policies') {
      return `${d.cluster_present === false ? `<p class="form-hint">${esc(tr('udw.objectsGone'))}</p>` : ''}
        ${(d.delete || []).length ? `<p>${esc(tr('udw.objectsDeleted', { n: d.delete.length }))}</p><ul class="bm-paths udw-delete">${li(d.delete)}</ul>` : ''}
        ${(d.kept || []).length ? `<p>${esc(tr('udw.objectsKept'))}</p><ul class="bm-paths udw-kept">${li(d.kept)}</ul>` : ''}
        ${(d.refused || []).length ? `<div class="bm-refusal">${icon('fail', { size: 13 })} ${esc(tr('udw.objectsRefused'))}<ul class="bm-paths">${li(d.refused)}</ul></div>` : ''}`;
    }
    if (u.kind === 'capi-cluster') {
      return `<p>${esc(tr('udw.clusterGoes', { machines: d.machines || 0, ns: d.target_namespace || '' }))}</p>
        <p class="form-hint udw-child-data">${esc(tr('udw.childData'))}</p>
        ${(d.vms || []).length || (d.volumes || []).length ? `<p class="form-hint udw-approx">${esc(tr('udw.approx'))}</p>` : ''}
        ${(d.vms || []).length ? `<p class="form-hint">${esc(tr('udw.vms'))}</p><ul class="bm-paths">${li(d.vms)}</ul>` : ''}
        ${(d.volumes || []).length ? `<p class="form-hint">${esc(tr('udw.volumes'))}</p><ul class="bm-paths">${li(d.volumes)}</ul>` : ''}
        ${(d.loadbalancers || []).length ? `<p class="form-hint">${esc(tr('udw.loadbalancers'))}</p><ul class="bm-paths">${li(d.loadbalancers)}</ul>` : ''}
        ${d.namespace_removed ? `<p class="form-hint">${esc(tr('udw.namespaceRemoved'))}</p>` : ''}`;
    }
    if (u.kind === 'gitops') {
      return `<p class="udw-gitops">${esc(tr('udw.gitopsGoes', { tool: d.tool || '?', name: d.name || u.ref }))}</p>`;
    }
    if (u.kind === 'eso') {
      // 2.10.0 : les Secrets synchronisés restent à leur dernière valeur
      return `<p class="udw-eso">${esc(tr('udw.esoGoes', { namespaces: (d.namespaces || []).join(', ') || '–', n: d.externalsecrets || 0 }))}</p>
        ${d.note ? `<p class="form-hint udw-eso-note">${esc(d.note)}</p>` : ''}`;
    }
    if (u.kind === 'rancher-import') {
      return `<p class="udw-rancher">${esc(tr('udw.rancherGoes', { name: d.name || '', rancher: d.rancher || '' }))}</p>
        ${undoBundleHtml(d.agent_bundle)}
        ${(d.delete || []).length ? `<p>${esc(tr('udw.objectsDeleted', { n: d.delete.length }))}</p><ul class="bm-paths udw-delete">${li(d.delete)}</ul>` : ''}
        ${(d.kept || []).length ? `<p>${esc(tr('udw.objectsKept'))}</p><ul class="bm-paths udw-kept">${li(d.kept)}</ul>` : ''}
        ${(d.refused || []).length ? `<div class="bm-refusal">${icon('fail', { size: 13 })} ${esc(tr('udw.objectsRefused'))}<ul class="bm-paths">${li(d.refused)}</ul></div>` : ''}`;
    }
    if (u.kind === 'registry') {
      return `<p>${esc(tr('udw.registryGoes', { address: d.address || '' }))}</p>
        <p class="form-hint">${esc(tr('udw.registryVolume', { volume: d.volume || '' }))}</p>`;
    }
    // revue finale 2.7.0 (I1) : un cluster de la chaîne qui reste garde ce contenu
    const carried = (d.carried_by || []).length
      ? `<div class="bm-refusal bm-warning udw-carried">${icon('warn', { size: 13 })} ${esc(tr('udw.carriedBy', { clusters: d.carried_by.join(', ') }))}</div>`
      : '';
    return `${carried}<p>${esc(tr('udw.declGoes', { name: d.name || u.ref, n: (d.resources || []).length }))}</p>
      ${(d.resources || []).length ? `<ul class="bm-paths udw-resources">${li(d.resources)}</ul>` : ''}`;
  }

  /** a : l'action `deploy:<cluster>` à défaire (ligne de l'Historique). */
  async function openUndo(a) {
    if (!a || !String(a.action || '').startsWith('deploy:')) return null;
    const id = `deploy-undo-${a.id}`;
    if (document.getElementById(`fp-${id}`)) FloatingPanels.close(id);
    const cluster = String(a.action).split(':').slice(1).join(':');
    const s = {
      items: null, chosen: new Set(), plan: null, fetching: false, error: '', timer: null, closed: false,
      typed: '', deleteData: new Set(), regTyped: {}, launched: null, launching: false, sse: null,
    };
    const stop = () => { clearTimeout(s.timer); s.timer = null; };
    const tabBtn = (t, label, tp, ic) => `<button type="button" class="sub-tab tip${t === 'content' ? ' active' : ''}" role="tab"
        aria-selected="${t === 'content'}" data-udw-tab="${t}" data-tip="${esc(tp)}">${icon(ic, { size: 13 })} <span>${esc(label)}</span></button>`;
    const panel = FloatingPanels.open({
      id, icon: 'undo', title: `${tr('udw.title')} · ${cluster}`,
      width: Math.min(860, window.innerWidth - 120), height: Math.max(480, Math.min(780, window.innerHeight - 80)),
      onClose: () => {
        // aucune requête après la fermeture (minuterie effacée, garde `closed`)
        s.closed = true;
        stop();
        if (s.sse) { try { s.sse.close(); } catch { /* fermé */ } }
      },
      bodyHtml: `
        <form class="capi-form udw-form" style="padding:14px;" autocomplete="off" novalidate>
          <p class="form-hint">${esc(tr('udw.hint', { id: a.id, plan: (a.result || {}).plan || '' }))}</p>
          <div class="sub-tabs sub-tabs-inline udw-tabs" role="tablist" aria-label="${esc(tr('udw.title'))}">
            ${tabBtn('content', tr('udw.tab.content'), tr('udw.t.tab.content'), 'edit')}
            ${tabBtn('plan', tr('udw.tab.plan'), tr('udw.t.tab.plan'), 'notes')}
            ${tabBtn('confirm', tr('udw.tab.confirm'), tr('udw.t.tab.confirm'), 'warn')}
          </div>
          <div data-udw-pane="content" role="tabpanel"><div class="udw-content"></div></div>
          <div data-udw-pane="plan" role="tabpanel" hidden><div class="udw-plan" aria-live="polite"></div></div>
          <div data-udw-pane="confirm" role="tabpanel" hidden>
            <fieldset>
              <legend>${esc(tr('udw.fs.confirm'))}</legend>
              <label class="tip" style="grid-column:1/-1;" data-tip="${esc(tr('udw.t.typeName'))}">${esc(tr('udw.typeName'))}
                <code class="udw-cluster-name">${esc(cluster)}</code>
                <input class="udw-typed" autocomplete="off" spellcheck="false" aria-label="${esc(tr('udw.t.typeName'))}"></label>
              <div class="udw-registries" style="grid-column:1/-1;"></div>
            </fieldset>
          </div>
          <div class="dpw-msg udw-msg" role="status"></div>
          <div class="apply-bar udw-actions">
            <span class="tip udw-launch-wrap" data-tip="">
              <button type="submit" class="btn btn-danger btn-sm btn-ico udw-launch" disabled data-tip="">${icon('undo')} ${esc(tr('udw.undo'))}</button></span>
          </div>
          <div class="dpw-follow" hidden>
            <h4>${esc(tr('dpw.progress'))} <span class="dpw-follow-link"></span> <span class="dpw-follow-status"></span></h4>
            <ul class="bm-paths dpw-steps"></ul>
          </div>
        </form>`,
    });
    const form = panel.el.querySelector('.udw-form');
    if (form.dataset.bound) return panel;
    form.dataset.bound = '1';
    const $ = (sel) => form.querySelector(sel);
    const sig = () => JSON.stringify([...s.chosen].sort());
    const selectedRegs = () => (s.items || []).filter(u => u.kind === 'registry' && s.chosen.has(u.id))
      .map(u => u.ref.split('/').slice(1).join('/'));

    function setTab(t) {
      form.querySelectorAll('[data-udw-tab]').forEach(b => {
        const on = b.dataset.udwTab === t;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      form.querySelectorAll('[data-udw-pane]').forEach(p => { p.hidden = p.dataset.udwPane !== t; });
    }

    function launchState() {
      if (!can('admin')) return [false, tr('udw.t.adminOnly')];
      if (s.launched || s.launching) return [false, tr('dpw.t.launched')];
      if (s.error) return [false, tr('udw.t.planFailed')];
      if (s.fetching || !s.plan || s.plan.sig !== sig()) return [false, tr('udw.t.waitPlan')];
      if (!s.chosen.size) return [false, tr('udw.t.empty')];
      if (s.typed.trim() !== cluster) return [false, tr('udw.t.typeName')];
      const reg = selectedRegs().find(n => s.deleteData.has(n) && (s.regTyped[n] || '').trim() !== n);
      if (reg) return [false, tr('udw.t.typeRegistry', { name: reg })];
      return [true, tr('udw.t.launch')];
    }
    function paintLaunch() {
      const [ok, why] = launchState();
      const btn = $('.udw-launch');
      btn.disabled = !ok;
      btn.title = why;
      btn.setAttribute('aria-label', why);
      btn.dataset.tip = why;
      $('.udw-launch-wrap').dataset.tip = why;
    }
    function paintContent() {
      const box = $('.udw-content');
      if (!s.items) {
        box.innerHTML = s.error ? `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(s.error)}</div>
          <button type="button" class="btn btn-secondary btn-sm tip udw-retry" data-tip="${esc(tr('udw.t.retry'))}" title="${esc(tr('udw.t.retry'))}">${icon('refresh', { size: 12 })} ${esc(tr('udw.retry'))}</button>`
          : `<p class="form-hint">${esc(tr('udw.loading'))}</p>`;
        return;
      }
      if (!s.items.length) { box.innerHTML = `<p class="form-hint udw-nothing">${esc(tr('udw.nothing'))}</p>`; return; }
      const res = a.result || {};
      box.innerHTML = (res.undone_by ? `<div class="bm-imported udw-again">${icon('info', { size: 13 })} ${esc(tr('udw.again'))} ${actionLink(res.undone_by)}</div>` : '')
        + UNDO_KINDS.filter(k => s.items.some(u => u.kind === k)).map(k => `<div class="udw-group" data-kind="${esc(k)}">
          <h4>${esc(text(UNDO_KIND_TEXT, k))}</h4>
          <ul class="bm-paths">${s.items.filter(u => u.kind === k).map(u => {
            const off = undoOffReason(u);
            const tp = off || tr('udw.t.item');
            return `<li class="udw-item-row"><label class="bm-check tip" data-tip="${esc(tp)}">
              <input type="checkbox" class="udw-item" value="${esc(u.id)}" ${s.chosen.has(u.id) ? 'checked' : ''}${s.launched ? ' disabled' : ''}
                     aria-label="${esc(u.ref)}"> <code class="udw-ref">${esc(u.ref)}</code></label>
              ${u.proven === false ? `<span class="badge warn">${esc(tr('udw.notProven'))}</span>` : ''}
              ${String(u.note || '').includes(MODIFIED_NOTE) ? `<span class="badge warn">${esc(tr('udw.modified'))}</span>` : ''}
              ${off ? `<span class="form-hint udw-off">${esc(off)}</span>` : ''}
              ${u.run ? `<span class="form-hint">${esc(tr('udw.createdBy'))} ${actionLink(u.run)}</span>` : ''}</li>`;
          }).join('')}</ul></div>`).join('');
    }
    function paintPlan() {
      const box = $('.udw-plan');
      if (s.error) {
        box.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(s.error)}</div>
          <button type="button" class="btn btn-secondary btn-sm tip udw-retry" data-tip="${esc(tr('udw.t.retry'))}" title="${esc(tr('udw.t.retry'))}">${icon('refresh', { size: 12 })} ${esc(tr('udw.retry'))}</button>`;
        return;
      }
      if (s.fetching || !s.plan || s.plan.sig !== sig()) { box.innerHTML = `<p class="form-hint udw-wait">${esc(tr('udw.planWait'))}</p>`; return; }
      const sel = (s.plan.items || []).filter(u => u.selected);
      box.innerHTML = (sel.length ? `<ol class="udw-steps">${sel.map(u => `<li class="udw-plan-item" data-kind="${esc(u.kind)}">
          <strong>${esc(text(UNDO_KIND_TEXT, u.kind))}</strong> <code>${esc(u.ref)}</code>${undoDetailsHtml(u)}</li>`).join('')}</ol>`
        : `<p class="form-hint">${esc(tr('udw.t.empty'))}</p>`)
        + ((s.plan.limits || []).length ? `<div class="bm-refusal bm-warning udw-limits">${icon('warn', { size: 14 })} ${esc(tr('udw.limits'))}
          <ul class="bm-paths">${s.plan.limits.map(l => `<li data-code="${esc(l.code)}">${esc(UNDO_LIMITS[l.code] ? UNDO_LIMITS[l.code]() : l.detail || l.code)}</li>`).join('')}</ul></div>` : '');
    }
    function paintRegs() {
      const regs = selectedRegs();
      [...s.deleteData].forEach(n => { if (!regs.includes(n)) s.deleteData.delete(n); });
      $('.udw-registries').innerHTML = regs.map(n => `<div class="udw-reg" data-registry="${esc(n)}">
        <label class="bm-check tip" data-tip="${esc(tr('udw.t.deleteData'))}"><input type="checkbox" class="udw-del-data" value="${esc(n)}"
          ${s.deleteData.has(n) ? 'checked' : ''}> ${esc(tr('udw.deleteData'))} <code>${esc(n)}</code></label>
        ${s.deleteData.has(n) ? `<label class="tip" data-tip="${esc(tr('udw.t.typeRegistry', { name: n }))}">${esc(tr('udw.typeRegistry'))}
          <input class="udw-reg-typed" data-registry="${esc(n)}" autocomplete="off" spellcheck="false" value="${esc(s.regTyped[n] || '')}"
                 aria-label="${esc(tr('udw.t.typeRegistry', { name: n }))}"></label>` : ''}</div>`).join('');
    }
    const paintAll = () => { paintContent(); paintPlan(); paintRegs(); paintLaunch(); };

    async function fetchPlan(initial) {
      stop();
      if (s.closed || s.launched) return;
      const mine = sig();
      s.fetching = true;
      s.error = '';
      paintPlan();
      paintLaunch();
      const body = { action_id: a.id };
      if (!initial) body.include = [...s.chosen];
      let r;
      try { r = await api('POST', `/api/deploy/${enc(cluster)}/undo/plan`, body); }
      catch (e) { r = { ok: false, status: 0, d: { error: e.message } }; }
      if (s.closed || !form.isConnected) return;
      if (!initial && mine !== sig()) return;          // les cases ont changé depuis
      s.fetching = false;
      if (!r.ok) {
        s.error = r.d.error || String(r.status);
        paintAll();
        return;
      }
      if (initial) {
        s.items = r.d.items || [];
        s.chosen = new Set(s.items.filter(u => u.selected).map(u => u.id));
      }
      s.plan = Object.assign({ sig: sig() }, r.d);
      paintAll();
    }
    const later = () => {
      stop();
      s.plan = null;
      paintPlan();
      paintLaunch();
      if (!s.closed) s.timer = setTimeout(() => fetchPlan(false), 450);
    };

    async function launch() {
      const [ok] = launchState();
      if (!ok) return;
      stop();
      const msg = $('.udw-msg');
      const regs = selectedRegs().filter(n => s.deleteData.has(n));
      const body = { action_id: a.id, confirm: s.typed.trim(), include: [...s.chosen], delete_data: regs };
      if (regs.length) body.confirm_registry = Object.fromEntries(regs.map(n => [n, (s.regTyped[n] || '').trim()]));
      // revue 4-8 I4 : un seul envoi à la fois ; une erreur réseau est dite
      s.launching = true;
      paintLaunch();
      let r;
      try { r = await api('POST', `/api/deploy/${enc(cluster)}/undo`, body); }
      catch (e) { r = { ok: false, status: 0, d: { error: e.message || String(e) } }; }
      s.launching = false;
      if (s.closed || !form.isConnected) return;
      paintLaunch();
      if (r.ok && r.d.action_id) {
        s.launched = r.d.action_id;
        msg.innerHTML = okLine(tr('udw.started'));
        paintAll();
        followRun(form, s, r.d.action_id, () => {
          if (window.BMC && BMC.renderHistory && BMC.tab && BMC.tab() === 'history') BMC.renderHistory();
        });
        return;
      }
      if (r.d.code === 'deploy-running' && r.d.running) {
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.running'))} ${actionLink(r.d.running)}</div>`;
        return;
      }
      if (r.d.code === 'renew-running' && r.d.running) {
        // revue 5-8 I-9 : un renouvellement de machines tient ce cluster
        msg.innerHTML = `<div class="bm-refusal">${icon('fail', { size: 14 })} ${esc(tr('dpw.renewRunning'))} ${actionLink(r.d.running)}</div>`;
        return;
      }
      msg.innerHTML = refusal(r.d, r.status);
    }

    form.addEventListener('click', (e) => {
      const t = e.target.closest('[data-udw-tab]');
      if (t) setTab(t.dataset.udwTab);
      if (e.target.closest('.udw-retry')) fetchPlan(!s.items);
    });
    form.addEventListener('change', (e) => {
      const c = e.target.closest('.udw-item');
      if (c) {
        if (c.checked) s.chosen.add(c.value); else s.chosen.delete(c.value);
        paintRegs();
        later();
        return;
      }
      const d = e.target.closest('.udw-del-data');
      if (d) {
        if (d.checked) s.deleteData.add(d.value); else s.deleteData.delete(d.value);
        paintRegs();
        paintLaunch();
      }
    });
    form.addEventListener('input', (e) => {
      if (e.target.classList.contains('udw-typed')) { s.typed = e.target.value; paintLaunch(); return; }
      const rt = e.target.closest('.udw-reg-typed');
      if (rt) { s.regTyped[rt.dataset.registry] = rt.value; paintLaunch(); }
    });
    form.addEventListener('submit', (ev) => { ev.preventDefault(); launch(); });

    paintAll();
    await fetchPlan(true);
    return panel;
  }

  // =========================================================================
  // 2.8.0 : secrets Git de la phase GitOps (fenêtre Déployer, et revue 5-8
  // I-10 : champ Plan des fenêtres d'installation et de série)
  // =========================================================================
  /** Noms des secrets que demandent les entrées Git de la phase GitOps d'un plan. */
  function gopSecretNames(doc) {
    const ph = ((doc || {}).phases || {}).gitops || {};
    return ph.enabled ? [...new Set((ph.entries || []).map(e => ((e || {}).source || {}).secret).filter(Boolean))].sort() : [];
  }
  function gopSecretsHtml(names) {
    return names.map(n => `<div class="dpw-gop-secret" data-gop-secret="${esc(n)}">
          <h5><code>${esc(n)}</code></h5>
          <label class="tip" data-tip="${esc(tr('dpw.t.gopKind'))}">${esc(tr('dpw.f.gopKind'))}
            <select data-gop-k="kind" aria-label="${esc(tr('dpw.f.gopKind'))}">
              <option value="ssh">${esc(tr('dpw.gopKind.ssh'))}</option><option value="basic">${esc(tr('dpw.gopKind.basic'))}</option></select></label>
          <label class="tip dpw-gop-ssh" data-tip="${esc(tr('dpw.t.gopIdentity'))}">${esc(tr('dpw.f.gopIdentity'))}
            <textarea data-gop-k="identity" rows="3" spellcheck="false" autocomplete="off" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                      aria-label="${esc(tr('dpw.f.gopIdentity'))}"></textarea></label>
          <label class="tip dpw-gop-ssh" data-tip="${esc(tr('dpw.t.gopKnownHosts'))}">${esc(tr('dpw.f.gopKnownHosts'))}
            <textarea data-gop-k="known_hosts" rows="2" spellcheck="false" autocomplete="off" aria-label="${esc(tr('dpw.f.gopKnownHosts'))}"></textarea></label>
          <label class="tip dpw-gop-basic" data-tip="${esc(tr('dpw.t.gopUser'))}" hidden>${esc(tr('dpw.f.gopUser'))}
            <input data-gop-k="username" autocomplete="off" spellcheck="false" aria-label="${esc(tr('dpw.f.gopUser'))}"></label>
          <label class="tip dpw-gop-basic" data-tip="${esc(tr('dpw.t.gopPassword'))}" hidden>${esc(tr('dpw.f.gopPassword'))}
            <input data-gop-k="password" type="password" autocomplete="new-password" aria-label="${esc(tr('dpw.f.gopPassword'))}"></label>
        </div>`).join('');
  }
  /** Secrets complets saisis : {nom: {identity[, known_hosts]} | {username, password}}. */
  function gopRead(root) {
    const out = {};
    root.querySelectorAll('.dpw-gop-secret').forEach(b => {
      const v = (k) => ((b.querySelector(`[data-gop-k="${k}"]`) || {}).value || '');
      const kind = (b.querySelector('[data-gop-k="kind"]') || {}).value;
      if (kind === 'basic') {
        if (v('username') && v('password')) out[b.dataset.gopSecret] = { username: v('username'), password: v('password') };
      } else if (v('identity').trim()) {
        out[b.dataset.gopSecret] = Object.assign({ identity: v('identity') }, v('known_hosts').trim() ? { known_hosts: v('known_hosts') } : {});
      }
    });
    return out;
  }
  function gopWipe(root) {
    root.querySelectorAll('.dpw-gop-secret textarea, .dpw-gop-secret input').forEach(el => { el.value = ''; });
  }
  /** Bascule clé SSH / compte d'un bloc ; vrai si l'événement le concernait. */
  function gopKindChange(t) {
    const gk = t.closest('.dpw-gop-secret [data-gop-k="kind"]');
    if (!gk) return false;
    const b = gk.closest('.dpw-gop-secret');
    b.querySelectorAll('.dpw-gop-ssh').forEach(x => { x.hidden = gk.value !== 'ssh'; });
    b.querySelectorAll('.dpw-gop-basic').forEach(x => { x.hidden = gk.value !== 'basic'; });
    return true;
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
    const ctl = { plan: initial || '', values: {}, plans: [], loaded: false, gopNames: [] };
    // revue 5-8 I-10 : secrets Git de la phase GitOps du plan, saisis ici
    // (jamais rangés, effacés après l'envoi) ; hors du conteneur repeint
    const gop = document.createElement('div');
    gop.className = 'bmpl-pick-gop';
    gop.style.gridColumn = '1 / -1';           // toute la largeur de la fenêtre, comme le choix du plan
    gop.hidden = true;
    box.insertAdjacentElement('afterend', gop);
    async function loadGop() {
      const name = ctl.plan;
      if (!withVars || !name) { ctl.gopNames = []; paintGop(); return; }
      let doc = null;
      try { const r = await api('GET', `/api/baremetal/plans/${enc(name)}`); doc = r.ok ? r.d : null; } catch { doc = null; }
      if (name !== ctl.plan || !box.isConnected) return;
      ctl.gopNames = gopSecretNames(doc);
      paintGop();
    }
    function paintGop() {
      gop.hidden = !ctl.gopNames.length;
      gop.innerHTML = ctl.gopNames.length ? `<fieldset class="dpw-gop-secrets"><legend>${esc(tr('dpw.fs.gopSecrets'))}</legend>
        <p class="form-hint" style="grid-column:1/-1;">${esc(tr('dpw.gopSecretsHint'))}</p>
        <div class="dpw-gop-secrets-box" style="grid-column:1/-1;">${gopSecretsHtml(ctl.gopNames)}</div></fieldset>` : '';
    }
    gop.addEventListener('change', (e) => { gopKindChange(e.target); });
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
      if (e.target.matches('[data-pp="plan"]')) { ctl.plan = e.target.value; ctl.values = {}; paintPicker(); loadGop(); }
    });
    box.addEventListener('input', (e) => {
      const v = e.target.closest('[data-pp-var]');
      if (v) ctl.values[v.dataset.ppVar] = v.value;
    });
    ctl.value = () => {
      if (!ctl.plan) return null;
      const vars = {};
      Object.entries(ctl.values).forEach(([k, v]) => { if (v) vars[k] = v; });
      if (!withVars) return { plan: ctl.plan };
      const gsec = gopRead(gop);
      return Object.assign({ plan: ctl.plan, plan_variables: vars }, Object.keys(gsec).length ? { gitops_secrets: gsec } : {});
    };
    /** Efface les secrets Git saisis (après l'envoi). */
    ctl.clearSecrets = () => gopWipe(gop);
    paintPicker();
    loadGop();
    api('GET', '/api/baremetal/plans').then(r => {
      ctl.plans = r.ok ? (r.d.plans || []) : [];
      ctl.loaded = r.ok;
      if (box.isConnected) paintPicker();
    }).catch(() => {});
    return ctl;
  }

  /** Saisies de la carte Artefacts ; vrai si l'événement la concernait. */
  function artefactsChange(t) {
    const pf = t.closest('#bm-plans .bmpl-pol-f');
    if (pf) {
      const p = edit.artPolicies[Number(pf.dataset.i)];
      if (!p) return true;
      if (pf.dataset.k === 'base') { p.base = pf.checked; return true; }
      p[pf.dataset.k] = pf.value;
      if (pf.dataset.k === 'teams') {
        // rôles et comptes d'un namespace retiré : leur liste se repeint
        const teams = listOf(p.teams);
        p.roles.concat(p.accounts).forEach(x => { if (!teams.includes(x.namespace)) x.namespace = teams[0] || ''; });
        paintArtefacts();
      }
      return true;
    }
    const sub = t.closest('#bm-plans .bmpl-pol-role-f, #bm-plans .bmpl-pol-acc-f');
    if (sub) {
      const p = edit.artPolicies[Number(sub.dataset.i)];
      const row = p && (sub.classList.contains('bmpl-pol-role-f') ? p.roles : p.accounts)[Number(sub.dataset.j)];
      if (row) row[sub.dataset.k] = sub.value;
      return true;
    }
    const af = t.closest('#bm-plans .bmpl-art-f');
    if (af) {
      const it = edit.artItems[Number(af.dataset.i)];
      if (it) it[af.dataset.k] = af.value;
      return true;
    }
    const pkg = t.closest('#bm-plans .bmpl-art-pkg');
    if (pkg) {
      const it = edit.artItems[Number(pkg.dataset.i)];
      if (!it) return true;
      const k = pkg.value.lastIndexOf('@');
      Object.assign(it, { package: pkg.value.slice(0, k), version: pkg.value.slice(k + 1), item: '', registry: '', valuesText: '' });
      fixDeps();
      paintArtefacts();
      return true;
    }
    const sel = t.closest('#bm-plans .bmpl-art-item');
    if (sel) {
      const it = edit.artItems[Number(sel.dataset.i)];
      if (!it) return true;
      const old = it.item;
      it.item = sel.value;
      // une autre sorte : registre et valeurs ne valent que pour un chart
      if (kindOf(it) !== 'chart') { it.registry = ''; it.valuesText = ''; }
      edit.artItems.forEach(x => { x.depends_on = x.depends_on.map(d => (d === old ? it.item : d)); });
      fixDeps();
      paintArtefacts();
      return true;
    }
    const dep = t.closest('#bm-plans .bmpl-art-dep');
    if (dep) {
      const it = edit.artItems[Number(dep.dataset.i)];
      if (!it) return true;
      it.depends_on = it.depends_on.filter(x => x !== dep.value);
      if (dep.checked) it.depends_on.push(dep.value);
      return true;
    }
    return false;
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
      const ud = e.target.closest('.bm-deploy-undo');
      if (ud && !ud.disabled) {
        e.preventDefault();
        const a = window.BMC && BMC.historyAction ? BMC.historyAction(ud.dataset.actionId) : null;
        if (a) openUndo(a);
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
      // 2.6.0 : phase Registre
      if ((el = e.target.closest('.bmpl-reg-add'))) {
        edit.registries.push(el.dataset.mode === 'bootstrap'
          ? { name: `amorcage${edit.registries.length ? `-${edit.registries.length + 1}` : ''}`, mode: 'bootstrap', address: '',
            size_gib: 20, storage_class: '', mirrors: '' }
          : { name: '', mode: 'external' });
        paintRegistries();
        return;
      }
      if ((el = e.target.closest('.bmpl-reg-rm'))) { edit.registries.splice(i(el), 1); paintRegistries(); return; }
      if (e.target.closest('.bmpl-pub-add')) {
        const p0 = (regData.packages || [])[0];
        const r0 = (edit.registries[0] || {}).name || ((regData.registries || [])[0] || {}).name;
        if (p0 && r0) { edit.publish.push({ package: p0.name, version: p0.version, registry: r0 }); paintRegistries(); }
        return;
      }
      if ((el = e.target.closest('.bmpl-pub-rm'))) { edit.publish.splice(i(el), 1); paintRegistries(); return; }
      // 2.7.0 : phase Artefacts
      if (e.target.closest('.bmpl-pol-add')) {
        edit.artPolicies.push({ clusters: planClusterNames()[0] || '', base: false, teams: '', teamObjs: {}, roles: [], accounts: [] });
        paintArtefacts();
        return;
      }
      if ((el = e.target.closest('.bmpl-pol-rm'))) { edit.artPolicies.splice(i(el), 1); paintArtefacts(); return; }
      if ((el = e.target.closest('.bmpl-pol-role-add, .bmpl-pol-acc-add'))) {
        const p = edit.artPolicies[i(el)];
        const ns = p ? listOf(p.teams)[0] : '';
        if (!p || !ns) return;
        if (el.classList.contains('bmpl-pol-role-add')) p.roles.push({ namespace: ns, group: '', role: 'view' });
        else p.accounts.push({ namespace: ns, name: '', role: 'view' });
        paintArtefacts();
        return;
      }
      if ((el = e.target.closest('.bmpl-pol-role-rm, .bmpl-pol-acc-rm'))) {
        const p = edit.artPolicies[i(el)];
        if (p) (el.classList.contains('bmpl-pol-role-rm') ? p.roles : p.accounts).splice(Number(el.dataset.j), 1);
        paintArtefacts();
        return;
      }
      if (e.target.closest('.bmpl-art-add')) {
        const p0 = (regData.packages || [])[0];
        if (p0) {
          edit.artItems.push({ package: p0.name, version: p0.version, item: '', registry: '', valuesText: '',
            clusters: planClusterNames()[0] || '', depends_on: [] });
          paintArtefacts();
        }
        return;
      }
      if ((el = e.target.closest('.bmpl-art-rm'))) { edit.artItems.splice(i(el), 1); fixDeps(); paintArtefacts(); return; }
      // 2.8.0 : phase GitOps
      if (e.target.closest('.bmpl-gop-add')) {
        if (edit.gitops.length < MAX_GITOPS) {
          edit.gitops.push(gopFromDoc({ name: '', clusters: planClusterNames().slice(0, 1) }));
          paintGitops();
        }
        return;
      }
      if ((el = e.target.closest('.bmpl-gop-rm'))) { edit.gitops.splice(i(el), 1); paintGitops(); return; }
      if (e.target.closest('.bmpl-sc-ok')) { takeShortcut(box.querySelector('.bmpl-cl-shortcut')); return; }
      if (e.target.closest('.bmpl-sc-clear')) { edit.shortcut = null; paintClusters(); return; }
      if (e.target.closest('.bmpl-add-tpl-go')) {
        const sel = box.querySelector('.bmpl-add-tpl');
        if (sel && sel.value) { edit.items.push({ template: sel.value, depends_on: [] }); paintTemplates(); }
      }
    });
    document.addEventListener('change', (e) => {
      if (edit && artefactsChange(e.target)) return;
      if (edit && gitopsChange(e.target)) return;
      const cdep = e.target.closest('#bm-plans .bmpl-cl-dep');
      if (cdep && edit) {
        const it = edit.clusters[Number(cdep.dataset.i)];
        if (!it) return;
        it.depends_on = it.depends_on.filter(x => x !== cdep.value);
        if (cdep.checked) it.depends_on.push(cdep.value);
        return;
      }
      const rf = e.target.closest('#bm-plans .bmpl-reg-f');
      if (rf && edit) {
        const r = edit.registries[Number(rf.dataset.i)];
        if (r) r[rf.dataset.k] = rf.dataset.k === 'size_gib' ? Number(rf.value) : rf.value;
        if (rf.dataset.k === 'name') paintRegistries();
        return;
      }
      const pp = e.target.closest('#bm-plans .bmpl-pub-pkg, #bm-plans .bmpl-pub-reg');
      if (pp && edit) {
        const p = edit.publish[Number(pp.dataset.i)];
        if (!p) return;
        if (pp.classList.contains('bmpl-pub-reg')) p.registry = pp.value;
        else { const [n, v] = pp.value.split('@'); p.package = n; p.version = v; }
        return;
      }
      const cregs = e.target.closest('#bm-plans .bmpl-cl-regs');
      if (cregs && edit) {
        const it = edit.clusters[Number(cregs.dataset.i)];
        if (it) it.registries = cregs.value;
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

  return { init, render, openDeploy, openUndo, openTemplate, openCapiTemplate, resumeButton, undoButton, undoneBadge,
    clusterLink, mountPicker, findingsHtml, findingText, actionLink, linkIds };
})();

document.addEventListener('DOMContentLoaded', BMPlans.init);
window.BMPlans = BMPlans;
