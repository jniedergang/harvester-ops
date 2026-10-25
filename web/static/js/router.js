/**
 * harvester-ops : routeur des adresses (navigation 2.0, étape 1)
 *
 * Écoute le chargement et `hashchange`, lit l'adresse avec Routes, et montre
 * l'écran ACTUEL le plus proche par un seul point d'entrée (apply) : le
 * cluster d'abord, puis l'onglet, puis le pane. L'interface ne change pas
 * encore (le nouveau menu arrive à l'étape 2) : les adresses de la 2.0
 * (#hosts, #vms/folder/..., #kvm/<h>...) ouvrent l'écran voisin, et
 * l'adresse vide restaure le dernier écran comme avant.
 *
 * resolve() et screenHash() sont purs (testés dans node) ; le reste parle à
 * App, Sections et AccessNotice.
 */
(function (root) {
  const R = () => root.Routes || (typeof require === 'function' ? require('./routes.js') : null);

  // Sections de Harvester tenues par Sections.DEF (pane retenu par section)
  const SECTION_TABS = ['storage', 'network', 'addons', 'security', 'monlog', 'advanced', 'vmimport', 'forklift'];

  /**
   * route -> écran actuel { tab, cluster, pane, overview, automation } ;
   * null pour une route inconnue. `cluster` à null : le cluster courant
   * reste (écrans de flotte).
   */
  function resolve(route) {
    if (!route || !route.name) return null;
    const t = (tab, extra) => Object.assign({ tab, cluster: null, pane: null, overview: null, automation: null }, extra || {});
    switch (route.name) {
      case 'host': {
        if (route.kind === 'kvm') {
          if (route.sub === 'vms') return t('kvmvms');
          if (route.sub === 'dr') return t('drplans');
          if (route.sub === 'params' && route.tab === 'pools') return t('kvmstorage');
          if (route.sub === 'params' && route.tab === 'networks') return t('kvmnetworks');
          return t('kvmhosts');    // vue d'ensemble, system, labels, conn
        }
        const c = { cluster: route.host };
        switch (route.sub) {
          case null: case undefined: return t('overview', Object.assign(c, { overview: 'metrics' }));
          case 'overview': return t('overview', Object.assign(c, { overview: 'events' }));
          case 'vms': return t('namespaces', c);
          case 'containers': return t('containers', c);
          case 'dr': return t('clusterdr', c);
          case 'imports': return t(route.tab, Object.assign(c, { pane: route.pane || null }));
          case 'params':
            if (route.tab === 'nodes') return t('overview', Object.assign(c, { overview: 'cluster' }));
            if (route.tab === 'power') return t(route.pane === 'shutdown' ? 'shutdown' : 'startup', c);
            if (SECTION_TABS.includes(route.tab)) return t(route.tab, Object.assign(c, { pane: route.pane || null }));
            // upgrade, conn : pas d'écran à eux avant l'étape 2, l'aperçu
            return t('overview', Object.assign(c, { overview: 'metrics' }));
          default: return null;
        }
      }
      case 'vms':
        if (route.kind === 'kvm') return t('kvmvms');
        if (route.kind === 'harvester' && route.host) return t('namespaces', { cluster: route.host });
        return t('allvms');                  // racine, dossier, famille Harvester
      case 'folders': return t('allvms');
      case 'containers': return t('allcontainers');
      case 'hosts': return route.kind === 'harvester' ? t('overview') : t('kvmhosts');
      case 'dr': return t(route.tab === 'links' ? 'drlinks' : 'drplans');
      case 'migrations': return t('forkliftglobal');
      case 'gpus': case 'bookings': case 'usage': return t(route.name);
      case 'automation': return t('automation', { automation: route.tab || 'capi' });
      case 'activity': return t('activity');
      default: return null;
    }
  }

  /**
   * Écran affiché -> adresse. s = { tab, cluster, pane, overview, automation }.
   * Sans cluster, l'ancien jeton (compris pour toujours). Stockage et Réseaux
   * KVM gardent leur ancien jeton : ils montrent tous les hôtes à la fois,
   * aucune adresse de la grammaire ne les décrit avant l'étape 2.
   */
  function screenHash(s) {
    if (!s || !s.tab) return null;
    const Rt = R();
    const H = (f) => (s.cluster ? Rt.format(Object.assign({ name: 'host', kind: 'harvester', host: s.cluster,
                                                            sub: null, tab: null, pane: null, id: null }, f)) : '#' + s.tab);
    switch (s.tab) {
      case 'overview':
        if (s.overview === 'cluster') return H({ sub: 'params', tab: 'nodes' });
        if (s.overview === 'events') return H({ sub: 'overview', tab: 'events' });
        return H({});
      case 'storage': case 'network': case 'addons': case 'security': case 'monlog': case 'advanced':
        return H({ sub: 'params', tab: s.tab, pane: s.pane || null });
      case 'vmimport': case 'forklift': return H({ sub: 'imports', tab: s.tab, pane: s.pane || null });
      case 'clusterdr': return H({ sub: 'dr' });
      case 'startup': case 'shutdown': return H({ sub: 'params', tab: 'power', pane: s.tab });
      case 'namespaces': return H({ sub: 'vms' });
      case 'containers': return H({ sub: 'containers' });
      case 'allvms': return '#vms';
      case 'allcontainers': return '#containers';
      case 'gpus': case 'bookings': case 'usage': case 'activity': return '#' + s.tab;
      case 'forkliftglobal': return '#migrations';
      case 'kvmhosts': return '#hosts/kvm';
      case 'kvmvms': return '#vms/host/kvm';
      case 'kvmstorage': case 'kvmnetworks': return '#' + s.tab;
      case 'drplans': return '#dr';
      case 'drlinks': return '#dr/links';
      case 'automation': return '#automation/' + (Rt.AUTOMATION.includes(s.automation) ? s.automation : 'capi');
      default: return null;
    }
  }

  // ---------------------------------------------------------------------
  // Navigateur
  // ---------------------------------------------------------------------
  const LAST_KVM = 'harvester_ops_last_kvm';
  let started = false;
  let applying = 0;
  let current = null;
  let queue = Promise.resolve();

  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* sans stockage */ } },
  };

  function screenNow() { return root.App && App._nav ? screenHash(App._nav.screen()) : null; }

  function bareUrl() { return location.pathname + location.search; }

  /** Hôtes KVM déclarés (déclarations seules, sans SSH) ; relus à chaque besoin. */
  function kvmHosts() {
    return fetch('/api/kvm/hosts?declared=1')
      .then((r) => (r.ok ? r.json() : { hosts: [] }))
      .then((d) => (d.hosts || []).map((h) => h.name).filter(Boolean))
      .catch(() => []);
  }

  /** L'hôte de l'adresse est-il visible pour cette session ? */
  async function hostVisible(kind, name) {
    if (kind === 'kvm') {
      return (await kvmHosts()).includes(name) ? { visible: true } : { visible: false, reason: 'unknown' };
    }
    if (App.listClusters().includes(name)) return { visible: true };
    const info = await App.clustersInfo();
    if ((info.clusters || []).includes(name)) return { visible: true };
    const h = (info.hidden || []).find((x) => x.name === name);
    return { visible: false, reason: h ? h.reason : 'unknown' };
  }

  // Jeton d'avant sans hôte visible : l'onglet d'avant, tel quel (rien ne
  // change à l'étape 1 pour une console sans cluster ou sans hôte KVM).
  const LEGACY_TAB = { cluster: 'overview', vmio: 'vmimport' };

  /** Adresse illisible, hôte inconnu ou hors des droits : l'écran par
   *  défaut, et le bandeau d'AccessNotice le dit (jamais une page vide). */
  async function fallback(raw, name, reason) {
    history.replaceState(history.state, '', bareUrl());
    await App._nav.restoreFromStorage();
    if (root.AccessNotice && AccessNotice.route) AccessNotice.route({ address: '#' + raw, name, reason });
  }

  async function apply(hash) {
    const Rt = R();
    const raw = String(hash || '').replace(/^#/, '');
    if (!raw) { await App._nav.restoreFromStorage(); return null; }
    let route = Rt.parse('#' + raw);
    let rewrite = false;
    if (route.name === 'unknown') {
      const template = Object.prototype.hasOwnProperty.call(Rt.LEGACY, raw) ? Rt.LEGACY[raw] : '';
      const ctx = {
        lastHarvester: App.getCurrentCluster() || ls.get('harvester_ops_current_cluster'),
        harvester: App.listClusters(),
        lastKvm: ls.get(LAST_KVM),
        kvm: template.includes('{LK}') ? await kvmHosts() : [],
      };
      const t = Rt.fromLegacy(raw, ctx);
      if (t && t.notice === 'no-host') {
        await App._nav.showRoute({ tab: LEGACY_TAB[raw] || raw });
        return null;
      }
      if (!t) { await fallback(raw, null, 'unknown'); return null; }
      route = t;
      rewrite = true;
    }
    if (route.host) {
      const v = await hostVisible(route.kind, route.host);
      if (!v.visible) { await fallback(raw, route.host, v.reason); return null; }
    }
    const target = resolve(route);
    if (!target) { await fallback(raw, null, 'unknown'); return null; }
    if (target.pane && root.Sections && Sections.remember) Sections.remember(target.tab, target.pane);
    if (target.automation) ls.set('harvester_ops_automation_subtab', target.automation);
    if (route.kind === 'kvm' && route.host) ls.set(LAST_KVM, route.host);
    await App._nav.showRoute(Object.assign({}, target, { tabFirst: root.__routerOrder === 'tab-first' }));
    // ancien jeton : l'adresse devient celle de l'écran
    if (rewrite) {
      const h = screenNow();
      if (h) { history.replaceState(history.state, '', h); return R().parse(h); }
    }
    return route;
  }

  // Les navigations passent l'une après l'autre : deux clics rapides ne
  // mélangent pas leurs clusters.
  function enqueue(hash) {
    const run = async () => {
      applying += 1;
      let route = null;
      try { route = await apply(hash); } finally { applying -= 1; }
      if (!route) { const h = screenNow(); route = h ? R().parse(h) : null; }
      current = route;
    };
    queue = queue.then(run, run);
    return queue;
  }

  /** Va à une adresse (chaîne `#...` ou route). Une entrée d'historique est
   *  ajoutée, sauf { replace: true } ; Précédent revient à l'écran d'avant. */
  function go(target, opts) {
    const o = opts || {};
    const h = typeof target === 'string' ? '#' + target.replace(/^#/, '') : R().format(target);
    if (h !== location.hash) {
      if (o.replace) history.replaceState(history.state, '', h);
      else {
        // l'entrée quittée garde l'adresse de son écran (Précédent y revient)
        if (!location.hash) {
          const here = screenNow();
          if (here) history.replaceState(history.state, '', here);
        }
        history.pushState(null, '', h);
      }
    }
    return enqueue(h);
  }

  /** Changement du sélecteur de cluster (en place jusqu'à l'étape 2). */
  function selectCluster(name) {
    if (!name) return Promise.resolve();
    const s = App._nav.screen();
    const h = screenHash(Object.assign({}, s, { cluster: name }));
    const r = h ? R().parse(h) : null;
    if (r && r.name === 'host' && r.kind === 'harvester') return go(r);
    return App._nav.setCluster(name);
  }

  /** L'écran a changé par un clic : l'adresse le suit, sans entrée
   *  d'historique, pour que F5 rende l'écran exact. */
  function syncFromScreen() {
    if (!started || applying) return;
    const h = screenNow();
    if (!h || h === location.hash) return;
    history.replaceState(history.state, '', h);
    current = R().parse(h);
  }

  /** Appelé par App.init APRÈS bind() : applique l'adresse du chargement. */
  function start() {
    if (started) return queue;
    started = true;
    root.addEventListener('hashchange', () => { enqueue(location.hash); });
    return enqueue(location.hash);
  }

  const api = {
    go, start, selectCluster, syncFromScreen, resolve, screenHash,
    current: () => current,
    /** Promesse de la fin des navigations en cours (tests). */
    idle: () => queue,
  };
  root.Router = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
