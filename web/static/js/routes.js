/**
 * harvester-ops : grammaire des adresses (navigation 2.0, étape 1)
 *
 * Module pur : aucun accès au DOM ni au réseau. Il lit une adresse `#...`
 * en objet route, l'écrit à l'inverse, et traduit les anciens jetons
 * (`#storage`, `#allvms`, `#kvmhosts`...) qui restent compris pour toujours.
 * Chargé par le navigateur (window.Routes) et par node pour les tests
 * (module.exports).
 *
 * Route : { name, kind, host, sub, tab, pane, id }, les champs absents à
 * null ; { name: 'unknown', raw } pour une adresse illisible.
 */
(function (root) {
  // Même règle que le serveur pour les deux familles : cluster_decl.NAME_RE
  // (déclaration d'un cluster, web/app.py _validate_cluster_payload) et
  // kvm_decl.NAME_RE (hôte KVM, comme kvm.js). Pas RFC 1123 : un cluster peut
  // s'appeler `Lab_1` (un test vérifie l'égalité avec les règles Python).
  const HOST_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$/;
  // Identifiant de dossier ou de contrat (rp-<12 hex>, entier, `unfiled`)
  const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

  const KINDS = ['harvester', 'kvm'];
  // Onglets de Paramètres (identifiants de la maquette, HPARAMS et KPARAMS)
  const HARVESTER_PARAMS = ['nodes', 'storage', 'network', 'addons', 'security', 'monlog',
                            'advanced', 'upgrade', 'power', 'conn'];
  const KVM_PARAMS = ['system', 'pools', 'networks', 'labels', 'conn'];
  // Sous-onglets admis par onglet : clés de pane de Sections.DEF (un test
  // vérifie qu'elles n'ont pas divergé), plus power/startup|shutdown.
  const HARVESTER_PANES = {
    nodes: [],
    storage: ['volumes', 'images', 'classes'],
    network: ['vmnets', 'clusternets', 'lbs', 'pools', 'hostnets', 'overlay', 'underlay'],
    addons: ['list'],
    security: ['secrets', 'sshkeys'],
    monlog: ['metrics', 'alerts', 'flows', 'outputs'],
    advanced: ['settings', 'pci', 'usb', 'sriov', 'support'],
    upgrade: [],
    power: ['startup', 'shutdown'],
    conn: [],
  };
  // Imports et migrations : panes de Sections.DEF.vmimport et .forklift
  const IMPORT_PANES = {
    vmimport: ['imports', 'vmware', 'openstack', 'ova'],
    forklift: ['prep', 'sources', 'inventory', 'waves'],
  };
  const AUTOMATION = ['capi', 'terraform', 'pxe'];
  const ROOTS = ['migrations', 'gpus', 'bookings', 'usage'];

  // Anciens jetons -> nouvelle adresse. {L} = dernier hôte Harvester visité
  // (sinon le premier visible), {LK} = même chose pour KVM. Les jetons qui
  // sont aussi des adresses valides (#gpus, #activity...) y figurent pour
  // que la table soit complète. `containers` : racine de flotte (décision 3
  // du 25/10/2026), comme l'adresse neuve. clusterdr, drplans, drlinks et
  // vmio viennent de la 1.101.0 (après la rédaction de la grammaire).
  const LEGACY = {
    overview: '#harvester/{L}',
    cluster: '#harvester/{L}',
    storage: '#harvester/{L}/params/storage',
    network: '#harvester/{L}/params/network',
    addons: '#harvester/{L}/params/addons',
    security: '#harvester/{L}/params/security',
    monlog: '#harvester/{L}/params/monlog',
    advanced: '#harvester/{L}/params/advanced',
    startup: '#harvester/{L}/params/power/startup',
    shutdown: '#harvester/{L}/params/power/shutdown',
    namespaces: '#harvester/{L}/vms',
    allvms: '#vms',
    containers: '#containers',
    allcontainers: '#containers',
    kvmhosts: '#hosts',
    'kvm-hosts': '#hosts',
    kvmvms: '#vms/host/kvm',
    kvmstorage: '#kvm/{LK}/params/pools',
    kvmnetworks: '#kvm/{LK}/params/networks',
    vmimport: '#harvester/{L}/imports/vmimport',
    forklift: '#harvester/{L}/imports/forklift',
    vmio: '#harvester/{L}/imports/vmimport',
    forkliftglobal: '#migrations',
    automation: '#automation/capi',
    'automation/capi': '#automation/capi',
    'automation/terraform': '#automation/terraform',
    'automation/pxe': '#automation/pxe',
    clusterdr: '#harvester/{L}/dr',
    drplans: '#dr',
    drlinks: '#dr/links',
    gpus: '#gpus',
    bookings: '#bookings',
    usage: '#usage',
    activity: '#activity',
  };

  function route(name, f) {
    return Object.assign({ name, kind: null, host: null, sub: null, tab: null, pane: null, id: null }, f || {});
  }

  function unknown(raw) { return { name: 'unknown', raw: raw }; }

  function validHost(kind, name) {
    if (typeof name !== 'string') return false;
    return KINDS.indexOf(kind) >= 0 && HOST_NAME_RE.test(name);
  }

  function validId(id) { return typeof id === 'string' && ID_RE.test(id); }

  /** Lit le reste d'une adresse d'hôte (`#harvester/<c>/...`, `#kvm/<h>/...`). */
  function hostRoute(kind, host, rest) {
    const base = { kind, host };
    if (!rest.length) return route('host', base);
    const [a, b, c, ...more] = rest;
    if (more.length) return null;
    if (rest.length === 1 && (a === 'vms' || a === 'dr' || (kind === 'harvester' && a === 'containers'))) {
      return route('host', Object.assign(base, { sub: a }));
    }
    if (kind === 'harvester') {
      if (a === 'overview' && b === 'events' && c === undefined) {
        return route('host', Object.assign(base, { sub: 'overview', tab: 'events' }));
      }
      if (a === 'imports' && IMPORT_PANES[b] && (c === undefined || IMPORT_PANES[b].includes(c))) {
        return route('host', Object.assign(base, { sub: 'imports', tab: b, pane: c === undefined ? null : c }));
      }
      if (a === 'params' && HARVESTER_PARAMS.includes(b)
          && (c === undefined || HARVESTER_PANES[b].includes(c))) {
        return route('host', Object.assign(base, { sub: 'params', tab: b, pane: c === undefined ? null : c }));
      }
      return null;
    }
    if (a === 'params' && KVM_PARAMS.includes(b) && c === undefined) {
      return route('host', Object.assign(base, { sub: 'params', tab: b }));
    }
    return null;
  }

  /** Une adresse de la grammaire, ou null. */
  function grammar(segs) {
    const [head, ...rest] = segs;
    const n = rest.length;
    switch (head) {
      case 'vms':
        if (!n) return route('vms');
        if (rest[0] === 'folder' && n === 2 && validId(rest[1])) return route('vms', { id: rest[1] });
        if (rest[0] === 'host' && KINDS.includes(rest[1])) {
          if (n === 2) return route('vms', { kind: rest[1] });
          if (n === 3 && validHost(rest[1], rest[2])) return route('vms', { kind: rest[1], host: rest[2] });
        }
        return null;
      case 'folders':
        if (!n) return route('folders');
        return n === 1 && validId(rest[0]) ? route('folders', { id: rest[0] }) : null;
      case 'containers':
        if (!n) return route('containers');
        return n === 2 && rest[0] === 'folder' && validId(rest[1]) ? route('containers', { id: rest[1] }) : null;
      case 'hosts':
        if (!n) return route('hosts');
        if (n === 1 && KINDS.includes(rest[0])) return route('hosts', { kind: rest[0] });
        return n === 2 && rest[0] === 'folder' && validId(rest[1]) ? route('hosts', { id: rest[1] }) : null;
      case 'harvester':
      case 'kvm':
        if (!n || !validHost(head, rest[0])) return null;
        return hostRoute(head, rest[0], rest.slice(1));
      case 'dr':
        if (!n) return route('dr');
        if (n !== 1) return null;
        // `links` ne peut pas être un contrat : leurs identifiants sont rp-<hex>
        if (rest[0] === 'links') return route('dr', { tab: 'links' });
        return validId(rest[0]) ? route('dr', { id: rest[0] }) : null;
      case 'automation':
        return n === 1 && AUTOMATION.includes(rest[0]) ? route('automation', { tab: rest[0] }) : null;
      case 'activity':
        if (!n) return route('activity');
        if (n === 3 && rest[0] === 'host' && validHost(rest[1], rest[2])) {
          return route('activity', { kind: rest[1], host: rest[2] });
        }
        return null;
      default:
        if (ROOTS.includes(head) && !n) return route(head);
        return null;
    }
  }

  /** `#...` -> route ; { name: 'unknown', raw } si illisible. */
  function parse(hash) {
    const raw = String(hash == null ? '' : hash).replace(/^#/, '');
    if (!raw) return unknown(raw);
    let segs;
    try { segs = raw.split('/').map(decodeURIComponent); } catch (e) { return unknown(raw); }
    if (segs.some((s) => s === '')) return unknown(raw);
    return grammar(segs) || unknown(raw);
  }

  /** route -> `#...`, chaque segment passé par encodeURIComponent. */
  function format(r) {
    if (!r || !r.name) return '#';
    // adresse brute, NON validée : à afficher (échappée), jamais à écrire
    // dans l'historique (pushState) ni à interpoler dans du HTML
    if (r.name === 'unknown') return '#' + (r.raw || '');
    const e = encodeURIComponent;
    const parts = [];
    switch (r.name) {
      case 'host':
        parts.push(r.kind, e(r.host));
        if (r.sub) parts.push(r.sub);
        if (r.tab) parts.push(r.tab);
        if (r.pane) parts.push(r.pane);
        break;
      case 'vms':
        parts.push('vms');
        if (r.id) parts.push('folder', e(r.id));
        else if (r.kind) { parts.push('host', r.kind); if (r.host) parts.push(e(r.host)); }
        break;
      case 'folders':
        parts.push('folders');
        if (r.id) parts.push(e(r.id));
        break;
      case 'containers':
      case 'hosts':
        parts.push(r.name);
        if (r.id) parts.push('folder', e(r.id));
        else if (r.kind && r.name === 'hosts') parts.push(r.kind);
        break;
      case 'dr':
        parts.push('dr');
        if (r.tab) parts.push(r.tab);
        else if (r.id) parts.push(e(r.id));
        break;
      case 'automation':
        parts.push('automation', r.tab || 'capi');
        break;
      case 'activity':
        parts.push('activity');
        if (r.kind && r.host) parts.push('host', r.kind, e(r.host));
        break;
      default:
        parts.push(r.name);
    }
    return '#' + parts.join('/');
  }

  function pick(last, list) {
    const l = Array.isArray(list) ? list : [];
    return l.includes(last) ? last : (l[0] || null);
  }

  /**
   * Ancien jeton -> route ; null si le jeton est vide ou inconnu (le
   * routeur choisit alors l'écran par défaut). Sans hôte visible de la
   * famille demandée : { name: 'hosts', notice: 'no-host' }.
   * ctx = { lastHarvester, lastKvm, harvester: [...], kvm: [...] }.
   * La table est relue à chaque appel (api.LEGACY), jamais copiée.
   */
  function fromLegacy(token, ctx) {
    const t = String(token == null ? '' : token).replace(/^#/, '');
    if (!t) return null;
    const table = api.LEGACY || {};
    if (!Object.prototype.hasOwnProperty.call(table, t)) return null;
    let target = table[t];
    const c = ctx || {};
    if (target.includes('{L}')) {
      const l = pick(c.lastHarvester, c.harvester);
      if (!l) return { name: 'hosts', notice: 'no-host' };
      target = target.replace('{L}', encodeURIComponent(l));
    }
    if (target.includes('{LK}')) {
      const lk = pick(c.lastKvm, c.kvm);
      if (!lk) return { name: 'hosts', notice: 'no-host' };
      target = target.replace('{LK}', encodeURIComponent(lk));
    }
    const r = parse(target);
    return r.name === 'unknown' ? null : r;
  }

  const api = {
    parse, format, fromLegacy, validHost,
    LEGACY, HARVESTER_PARAMS, KVM_PARAMS, HARVESTER_PANES, IMPORT_PANES, AUTOMATION,
  };
  root.Routes = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
