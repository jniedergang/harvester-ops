/**
 * harvester-ops : routeur des adresses (navigation 2.0)
 *
 * Écoute le chargement et `hashchange`, lit l'adresse avec Routes, et montre
 * l'écran par un seul point d'entrée (apply) : le cluster d'abord, puis
 * l'onglet, puis le pane. Depuis la 2.0.0 c'est la seule façon d'ouvrir un
 * écran (l'ancien menu est retiré) : l'adresse vide ouvre #vms (le dernier
 * écran de la 1.x une seule fois), un hôte invisible ouvre l'index des
 * hôtes avec un message, une adresse illisible #vms avec un message.
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
    const t = (tab, extra) => Object.assign({ tab, cluster: null, pane: null, overview: null, automation: null,
                                              kvm: null, kvmTab: null, hostsKind: null, activityHost: null }, extra || {});
    switch (route.name) {
      case 'host': {
        if (route.kind === 'kvm') {
          // 2.0.0 : page d'un hôte KVM, les vues KVM avec l'hôte imposé
          const k = { kvm: route.host };
          if (route.sub === 'vms') return t('kvmvms', k);
          if (route.sub === 'dr') return t('kvmdr', k);
          if (route.sub === 'params') {
            if (route.tab === 'pools') return t('kvmstorage', k);
            if (route.tab === 'networks') return t('kvmnetworks', k);
            return t('kvmparams', Object.assign(k, { kvmTab: route.tab }));
          }
          return t('kvmhosts', k);          // vue d'ensemble : la tuile de l'hôte
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
            // étape 2 : Mise à jour (contenu de la fenêtre Upgrade) et Connexion
            if (route.tab === 'upgrade') return t('hupgrade', c);
            if (route.tab === 'conn') return t('hconn', c);
            return null;
          default: return null;
        }
      }
      case 'vms':
        if (route.kind === 'kvm') return t('kvmvms');
        if (route.kind === 'harvester' && route.host) return t('namespaces', { cluster: route.host });
        return t('allvms');                  // racine, dossier, famille Harvester
      case 'folders': return t('allvms');
      case 'containers': return t('allcontainers');
      // 2.0.0 (tâche 8) : l'index des hôtes, une famille en option ; les
      // dossiers d'hôtes arrivent à l'étape 3 (index entier en attendant)
      case 'hosts': return t('hosts', { hostsKind: route.kind || null });
      case 'dr': return t(route.tab === 'links' ? 'drlinks' : 'drplans');
      case 'migrations': return t('forkliftglobal');
      case 'gpus': case 'bookings': case 'usage': return t(route.name);
      case 'automation': return t('automation', { automation: route.tab || 'capi' });
      // 2.0.0 (tâche 9) : Activité filtrée sur un hôte (filtre `cluster`
      // existant ; les actions d'un hôte KVM portent l'étiquette kvm:<hôte>)
      case 'activity':
        return t('activity', { activityHost: route.host ? (route.kind === 'kvm' ? `kvm:${route.host}` : route.host) : null });
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
    const K = (f) => Rt.format(Object.assign({ name: 'host', kind: 'kvm', host: s.kvm,
                                               sub: null, tab: null, pane: null, id: null }, f));
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
      case 'hupgrade': return H({ sub: 'params', tab: 'upgrade' });
      case 'hconn': return H({ sub: 'params', tab: 'conn' });
      case 'namespaces': return H({ sub: 'vms' });
      case 'containers': return H({ sub: 'containers' });
      case 'allvms': return '#vms';
      case 'allcontainers': return '#containers';
      case 'gpus': case 'bookings': case 'usage': return '#' + s.tab;
      case 'activity':
        if (s.activityHost && /^kvm:/.test(s.activityHost) && Rt.validHost('kvm', s.activityHost.slice(4))) {
          return Rt.format({ name: 'activity', kind: 'kvm', host: s.activityHost.slice(4) });
        }
        if (s.activityHost && s.activityIsCluster) return Rt.format({ name: 'activity', kind: 'harvester', host: s.activityHost });
        return '#activity';
      case 'forkliftglobal': return '#migrations';
      case 'hosts': return '#hosts' + (s.hostsKind === 'harvester' || s.hostsKind === 'kvm' ? '/' + s.hostsKind : '');
      case 'kvmhosts': return s.kvm ? K({}) : '#hosts/kvm';
      case 'kvmvms': return s.kvm ? K({ sub: 'vms' }) : '#vms/host/kvm';
      case 'kvmstorage': return s.kvm ? K({ sub: 'params', tab: 'pools' }) : '#' + s.tab;
      case 'kvmnetworks': return s.kvm ? K({ sub: 'params', tab: 'networks' }) : '#' + s.tab;
      case 'kvmparams': return s.kvm ? K({ sub: 'params', tab: Rt.KVM_PARAMS.includes(s.kvmTab) ? s.kvmTab : 'system' }) : null;
      case 'kvmdr': return s.kvm ? K({ sub: 'dr' }) : null;
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
    del(k) { try { localStorage.removeItem(k); } catch (e) { /* sans stockage */ } },
  };

  // 2.0.0 (décision 2) : l'adresse vide ouvre #vms. À la toute première
  // ouverture de la 2.0.0 dans ce navigateur, le dernier écran de l'ancien
  // menu (onglet retenu par la 1.x) est rouvert une seule fois, puis oublié.
  const DEFAULT_HASH = '#vms';
  const NAV2_SEEN = 'harvester_ops_nav2_seen';
  const OLD_TAB = 'harvester_ops_current_tab';

  /** Adresse d'une adresse vide (pur sauf localStorage, testé en e2e). */
  function emptyTarget() {
    if (ls.get(NAV2_SEEN)) return DEFAULT_HASH;
    ls.set(NAV2_SEEN, '1');
    const tab = ls.get(OLD_TAB);
    ls.del(OLD_TAB);
    if (!tab || !/^[a-z]{2,32}$/.test(tab)) return DEFAULT_HASH;
    const h = screenHash({
      tab, cluster: ls.get('harvester_ops_current_cluster'), pane: null,
      overview: ls.get('harvester_ops_overview_subtab') || 'metrics',
      automation: ls.get('harvester_ops_automation_subtab') || 'capi',
    });
    return h || DEFAULT_HASH;
  }

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

  /** 2.0.0 : un hôte que la personne ne voit pas (retiré, hors de ses
   *  droits) ou un ancien jeton qui demande un hôte alors qu'aucun n'est
   *  visible : l'index des hôtes, et le bandeau dit pourquoi (jamais une
   *  page vide). Rend la route de l'index. */
  async function toHosts(raw, name, reason) {
    const r = R().parse('#hosts');
    history.replaceState(history.state, '', '#hosts');
    await App._nav.showRoute(Object.assign({}, resolve(r), { tabFirst: false }));
    if (root.AccessNotice && AccessNotice.route) AccessNotice.route({ address: '#' + raw, name, reason });
    return r;
  }

  /** Adresse illisible : l'écran par défaut (#vms), et le bandeau
   *  d'AccessNotice le dit (jamais une page vide). */
  async function fallback(raw, name, reason) {
    history.replaceState(history.state, '', DEFAULT_HASH);
    await App._nav.showRoute(Object.assign({}, resolve(R().parse(DEFAULT_HASH)), { tabFirst: false }));
    if (root.AccessNotice && AccessNotice.route) AccessNotice.route({ address: '#' + raw, name, reason });
  }

  async function apply(hash) {
    const Rt = R();
    let raw = String(hash || '').replace(/^#/, '');
    if (!raw) {
      const target = emptyTarget();
      history.replaceState(history.state, '', target);
      raw = target.slice(1);
    }
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
      if (t && t.notice === 'no-host') return toHosts(raw, null, 'no-host');
      if (!t) { await fallback(raw, null, 'unknown'); return R().parse(DEFAULT_HASH); }
      route = t;
      rewrite = true;
    }
    if (route.host) {
      const v = await hostVisible(route.kind, route.host);
      if (!v.visible) return toHosts(raw, route.host, v.reason);
    }
    const target = resolve(route);
    if (!target) { await fallback(raw, null, 'unknown'); return R().parse(DEFAULT_HASH); }
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
      // étape 2 : le menu des hôtes suit l'écran (hôte ouvert, entrée active)
      if (root.HostNav && HostNav.sync) HostNav.sync(current);
      if (root.HostPage && HostPage.show) HostPage.show(current);
    };
    // une navigation qui lève est journalisée : la promesse rendue par go()
    // ne rejette jamais (aucun appelant n'a de gestionnaire) et la file continue
    queue = queue.then(run, run).catch((e) => { console.error('[router]', e); });
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

  /** L'écran a changé par un clic : l'adresse le suit, sans entrée
   *  d'historique, pour que F5 rende l'écran exact. */
  function syncFromScreen() {
    if (!started || applying) return;
    const h = screenNow();
    if (!h || h === location.hash) return;
    history.replaceState(history.state, '', h);
    current = R().parse(h);
    if (root.HostNav && HostNav.sync) HostNav.sync(current);
    if (root.HostPage && HostPage.show) HostPage.show(current);
  }

  // Étape 2 : adresse ouverte avant la connexion, gardée par la page de
  // connexion (login.html) le temps de se connecter
  const AFTER_LOGIN = 'harvester_ops_after_login';

  /** Remet dans la barre d'adresse l'écran demandé avant la connexion, s'il
   *  est encore valable et que rien d'autre n'est demandé. */
  function restoreAfterLogin() {
    let h = null;
    try { h = sessionStorage.getItem(AFTER_LOGIN); sessionStorage.removeItem(AFTER_LOGIN); } catch (e) { return; }
    if (!h || location.hash || !R().acceptable(h)) return;
    history.replaceState(history.state, '', bareUrl() + h);
  }

  /** Appelé par App.init APRÈS bind() : applique l'adresse du chargement. */
  function start() {
    if (started) return queue;
    started = true;
    restoreAfterLogin();
    // première ouverture de la 2.0.0 par un lien profond : l'écran de la
    // 1.x est oublié tout de suite, il ne ressort pas des jours plus tard
    // sur la première adresse vide
    if (location.hash) { ls.set(NAV2_SEEN, '1'); ls.del(OLD_TAB); }
    root.addEventListener('hashchange', () => { enqueue(location.hash); });
    return enqueue(location.hash);
  }

  const api = {
    go, start, syncFromScreen, resolve, screenHash,
    current: () => current,
    /** Promesse de la fin des navigations en cours (tests). */
    idle: () => queue,
  };
  root.Router = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
