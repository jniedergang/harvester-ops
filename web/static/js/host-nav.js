/**
 * harvester-ops : menu latéral des hôtes (navigation 2.0, étape 2)
 *
 * La liste des hôtes remplace le sélecteur de cluster : deux familles
 * (clusters Harvester, hyperviseurs KVM), repliables, chacune avec son
 * résumé (« 58 ok, 2 en alerte »), une pastille d'état et le nombre de VMs
 * par hôte, une étoile de favori (gardée par la console, par personne).
 * L'hôte ouvert se déplie sous son nom (un seul à la fois) avec son
 * sous-menu. Une recherche en tête (raccourci « / ») filtre les hôtes par
 * nom, étiquette et famille, et propose les VMs déjà lues par les écrans.
 *
 * Pas de plafond (décision 6 du 25/10/2026) : tous les hôtes sont listés.
 * Un hôte en alerte reste visible même dans une famille repliée, comme
 * l'hôte ouvert.
 *
 * Les entrées sont de vrais liens `<a href="#...">` : le routeur les
 * applique (clic milieu et copie de lien compris). Le menu ne lance aucune
 * lecture par hôte : il lit /api/hosts (état repris des lectures déjà
 * faites, voir web/app.py).
 *
 * Fonctions pures (testées dans node) : keyOf, isAlert, order, matches,
 * visible, summaryParts.
 */
(function (root) {
  const ALERT_STATES = ['unreachable', 'denied', 'error'];
  const KINDS = ['harvester', 'kvm'];

  const keyOf = (h) => `${h.kind}/${h.name}`;
  const isAlert = (h) => ALERT_STATES.includes(h && h.state);

  /** Favoris en tête, puis le nom (sans casse, puis exact) ; rend une copie. */
  function order(hosts) {
    return (hosts || []).slice().sort((a, b) =>
      (Number(!!b.favorite) - Number(!!a.favorite))
      || a.name.toLowerCase().localeCompare(b.name.toLowerCase())
      || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  }

  /** L'hôte correspond-il à la recherche ? Chaque mot doit se retrouver dans
   *  le nom, la famille, une étiquette (clé, valeur ou clé=valeur). */
  function matches(h, query) {
    const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return true;
    const labels = Object.entries(h.labels || {}).map(([k, v]) => `${k}=${v}`);
    const hay = [h.name, h.kind].concat(labels).join(' ').toLowerCase();
    return words.every((w) => hay.includes(w));
  }

  /**
   * Hôtes d'une famille à montrer, dans l'ordre : [{ key, reason }].
   * reason : 'favorite' | 'open' | 'alert' | 'rest'. Famille repliée : seuls
   * l'hôte ouvert et les hôtes en alerte restent (une alerte n'est jamais
   * cachée par un repli). Aucun plafond.
   */
  function visible(hosts, openKey, collapsed) {
    const out = [];
    order(hosts).forEach((h) => {
      const key = keyOf(h);
      const reason = h.favorite ? 'favorite' : key === openKey ? 'open' : isAlert(h) ? 'alert' : 'rest';
      if (collapsed && key !== openKey && !isAlert(h)) return;
      out.push({ key, reason });
    });
    return out;
  }

  /** Compteurs de la tête de famille à partir des hôtes eux-mêmes. */
  function summaryParts(hosts) {
    const s = { total: 0, ok: 0, alert: 0, unknown: 0 };
    (hosts || []).forEach((h) => {
      s.total += 1;
      if (isAlert(h)) s.alert += 1;
      else if (h.state === 'ok') s.ok += 1;
      else s.unknown += 1;
    });
    return s;
  }

  const pure = { keyOf, isAlert, order, matches, visible, summaryParts, ALERT_STATES, KINDS };
  if (typeof document === 'undefined') {
    if (typeof module !== 'undefined' && module.exports) module.exports = pure;
    return;
  }

  // ---------------------------------------------------------------------
  // Rendu
  // ---------------------------------------------------------------------
  const tr = (k, p) => (root.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (root.Icons ? Icons.svg(n, { size }) : '');
  const $ = (s) => document.querySelector(s);
  const REFRESH_MS = 30000;
  const COLLAPSE_KEY = (kind) => `harvester_ops_hnav_collapsed_${kind}`;
  const VM_RESULTS_MAX = 6;

  let data = null;           // dernière réponse de /api/hosts
  let query = '';
  let timer = null;
  let route = null;          // route courante (Router.current())
  let lastError = '';

  const ls = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* sans stockage */ } },
  };
  const isCollapsed = (kind) => ls.get(COLLAPSE_KEY(kind)) === '1';

  // clés littérales (contrôle de parité i18n)
  const FAMILY = () => ({ harvester: tr('hnav.fam.harvester'), kvm: tr('hnav.fam.kvm') });
  const STATE = () => ({
    ok: tr('hnav.state.ok'), unreachable: tr('hnav.state.unreachable'), denied: tr('hnav.state.denied'),
    error: tr('hnav.state.error'), unknown: tr('hnav.state.unknown'),
  });
  const SUB = {
    harvester: [
      ['overview', 'metrics', () => tr('hnav.sub.overview'), () => tr('hnav.sub.overviewTip')],
      ['vms', 'vm', () => tr('hnav.sub.vms'), () => tr('hnav.sub.vmsTip')],
      ['containers', 'container', () => tr('hnav.sub.containers'), () => tr('hnav.sub.containersTip')],
      ['imports', 'download', () => tr('hnav.sub.imports'), () => tr('hnav.sub.importsTip')],
      ['params', 'settings', () => tr('hnav.sub.params'), () => tr('hnav.sub.paramsTip')],
      ['dr', 'shield', () => tr('hnav.sub.dr'), () => tr('hnav.sub.drTip')],
    ],
    kvm: [
      ['overview', 'metrics', () => tr('hnav.sub.overview'), () => tr('hnav.sub.overviewKvmTip')],
      ['vms', 'vm', () => tr('hnav.sub.vms'), () => tr('hnav.sub.vmsKvmTip')],
      ['params', 'settings', () => tr('hnav.sub.params'), () => tr('hnav.sub.paramsKvmTip')],
      ['dr', 'shield', () => tr('hnav.sub.dr'), () => tr('hnav.sub.drKvmTip')],
    ],
  };

  const R = () => root.Routes;
  /** Adresse d'une entrée du sous-menu d'un hôte. */
  function subHref(kind, name, sub) {
    const r = { name: 'host', kind, host: name, sub: null, tab: null, pane: null, id: null };
    if (sub === 'imports') Object.assign(r, { sub, tab: 'vmimport' });
    else if (sub === 'params') Object.assign(r, { sub, tab: kind === 'kvm' ? 'system' : 'nodes' });
    else if (sub !== 'overview') r.sub = sub;
    return R().format(r);
  }

  /** Lien d'un hôte : le même écran que celui affiché quand on est déjà sur
   *  un hôte de la même famille (Paramètres > Stockage de A mène à
   *  Paramètres > Stockage de B, comme l'ancien sélecteur de cluster) ;
   *  sinon sa vue d'ensemble. */
  function hostHref(h) {
    if (route && route.name === 'host' && route.kind === h.kind && route.host && route.host !== h.name) {
      return R().format(Object.assign({}, route, { host: h.name }));
    }
    return subHref(h.kind, h.name, 'overview');
  }

  function openKey() {
    return route && route.name === 'host' && route.host ? `${route.kind}/${route.host}` : null;
  }

  function hostTip(h) {
    const parts = [`${h.name}: ${STATE()[h.state] || h.state}`];
    if (h.error) parts.push(h.error);
    if (typeof h.vm_count === 'number') parts.push(tr('hnav.tip.vms', { n: h.vm_count }));
    if (h.version) parts.push(h.version);
    return parts.join(', ');
  }

  function subMenu(h) {
    const cur = route && route.name === 'host' ? (route.sub || 'overview') : null;
    return `<div class="hnav-sub" role="group" aria-label="${esc(h.name)}">${SUB[h.kind].map(([sub, ic, label, tip]) => {
      const on = cur === sub || (sub === 'overview' && cur === 'overview');
      return `<a class="tab tab-child hnav-entry${on ? ' active' : ''}" data-hnav="sub" data-sub="${esc(sub)}"
        href="${esc(subHref(h.kind, h.name, sub))}" title="${esc(tip())}"${on ? ' aria-current="page"' : ''}>
        <span class="tab-icon">${icon(ic, 16)}</span> <span class="sidebar-label">${esc(label())}</span></a>`;
    }).join('')}</div>`;
  }

  function hostRow(h, open) {
    const st = h.state || 'unknown';
    const count = typeof h.vm_count === 'number' ? String(h.vm_count) : '';
    const countTip = count ? tr('hnav.vmCountTip', { n: count }) : tr('hnav.vmCountUnknown');
    const starTip = h.favorite ? tr('hnav.star.remove') : tr('hnav.star.add');
    return `<div class="hnav-host${open ? ' open' : ''}${isAlert(h) ? ' alert' : ''}" data-key="${esc(keyOf(h))}">
        <span class="hnav-dot st-${esc(st)}" title="${esc(STATE()[st] || st)}" aria-hidden="true"></span>
        <a class="hnav-host-name" data-hnav="host" href="${esc(hostHref(h))}"
           title="${esc(hostTip(h))}"${open ? ' aria-current="true"' : ''}>${esc(h.name)}</a>
        <span class="hnav-count sidebar-label" title="${esc(countTip)}">${esc(count)}</span>
        <button type="button" class="hnav-star sidebar-label${h.favorite ? ' on' : ''}" data-star="${esc(keyOf(h))}"
                aria-pressed="${h.favorite ? 'true' : 'false'}" title="${esc(starTip)}" aria-label="${esc(starTip)}">${icon('star', 13)}</button>
      </div>${open ? subMenu(h) : ''}`;
  }

  function familyBlock(kind, all) {
    const fam = FAMILY()[kind];
    const s = summaryParts(all);
    const collapsed = isCollapsed(kind) && !query;
    const list = all.filter((h) => matches(h, query));
    const okey = openKey();
    const shown = visible(list, okey, collapsed);
    const byKey = new Map(list.map((h) => [keyOf(h), h]));
    // la tête dit « 58 ok, 2 en alerte » ; les hôtes pas encore lus sont dans l'info-bulle
    const summary = tr('hnav.summary', { ok: s.ok, alert: s.alert });
    const summaryFull = s.unknown
      ? tr('hnav.summaryUnknown', { ok: s.ok, alert: s.alert, unknown: s.unknown }) : summary;
    const head = `<button type="button" class="hnav-fam-head" data-fam="${esc(kind)}" aria-expanded="${collapsed ? 'false' : 'true'}"
        title="${esc(tr('hnav.famTip', { family: fam, summary: summaryFull }))}">
        <span class="hnav-chev" aria-hidden="true">${icon('chevronDown', 12)}</span>
        <span class="tab-icon">${icon(kind === 'kvm' ? 'compute' : 'node')}</span>
        <span class="sidebar-label hnav-fam-name">${esc(fam)} <span class="hnav-fam-total">(${s.total})</span></span>
        <span class="sidebar-label hnav-fam-sum${s.alert ? ' has-alert' : ''}">${esc(summary)}</span></button>`;
    let body = shown.map((x) => hostRow(byKey.get(x.key), x.key === okey)).join('');
    if (query && !list.length) body = `<div class="hnav-empty">${esc(tr('hnav.noMatch'))}</div>`;
    else if (!all.length) body = `<div class="hnav-empty">${esc(tr('hnav.noHost'))}</div>`;
    return `<div class="hnav-fam${collapsed ? ' collapsed' : ''}" data-family="${esc(kind)}">${head}
      <div class="hnav-fam-hosts">${body}</div></div>`;
  }

  /** VMs déjà lues par les écrans (Toutes les VMs, KVM) qui correspondent. */
  function vmResults() {
    const words = query.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return [];
    const out = [];
    const fleet = root.AllVMs && AllVMs.cached ? AllVMs.cached() : null;
    ((fleet && fleet.vms) || []).forEach((v) => out.push({ kind: 'harvester', host: v.cluster, ns: v.namespace, name: v.name }));
    const kvm = root.KVM && KVM.cachedVms ? KVM.cachedVms() : [];
    (kvm || []).forEach((v) => out.push({ kind: 'kvm', host: v.host, ns: null, name: v.name }));
    return out.filter((v) => words.every((w) => `${v.name} ${v.host}`.toLowerCase().includes(w)))
      .sort((a, b) => a.name.localeCompare(b.name) || a.host.localeCompare(b.host))
      .slice(0, VM_RESULTS_MAX);
  }

  function vmHref(v) {
    return R().format({ name: 'host', kind: v.kind, host: v.host, sub: 'vms', tab: null, pane: null, id: null });
  }

  function renderResults() {
    const box = $('#hnav-results');
    if (!box) return;
    const vms = vmResults();
    box.hidden = !vms.length;
    box.innerHTML = vms.length ? `<div class="hnav-sec sidebar-label">${esc(tr('hnav.vmResults'))}</div>${vms.map((v, i) =>
      `<a class="tab tab-child hnav-entry hnav-vm" data-hnav="vm" data-vm="${i}" href="${esc(vmHref(v))}"
          title="${esc(tr('hnav.goVm', { name: v.name, host: v.host }))}">
        <span class="tab-icon">${icon('vm', 16)}</span> <span class="sidebar-label">${esc(v.name)} <span class="hnav-dim">${esc(v.host)}</span></span></a>`).join('')}` : '';
    box._vms = vms;
  }

  function renderFamilies() {
    const box = $('#hnav-families');
    if (!box) return;
    if (!data) {
      box.innerHTML = lastError
        ? `<div class="hnav-empty" role="status">${esc(tr('hnav.loadFailed', { error: lastError }))}</div>`
        : `<div class="hnav-empty">${esc(tr('common.loading'))}</div>`;
      return;
    }
    const hosts = data.hosts || [];
    box.innerHTML = KINDS.map((k) => familyBlock(k, hosts.filter((h) => h.kind === k))).join('');
  }

  /** Entrées racines : celle de la route courante est active. */
  function paintRoots() {
    const name = route ? route.name : null;
    const tab = route ? route.tab : null;
    const all = [...document.querySelectorAll('#host-nav [data-hnav-root]')];
    // une entrée à onglet (#dr/links, #automation/<x>) est active pour son
    // onglet ; l'entrée sans onglet de la même racine l'est pour le reste
    const tabbed = all.some((a) => a.dataset.hnavRoot === name && a.dataset.hnavTab && a.dataset.hnavTab === tab);
    all.forEach((a) => {
      const on = a.dataset.hnavRoot === name && (a.dataset.hnavTab ? a.dataset.hnavTab === tab : !tabbed);
      a.classList.toggle('active', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  }

  /** data = réponse de /api/hosts. */
  function render(d) {
    if (d) {
      data = d; lastError = '';
      // le bandeau de la page d'hôte reprend l'état lu ici
      if (root.HostPage && HostPage.render) HostPage.render();
    }
    const q = $('#hnav-q');
    if (q) q.setAttribute('aria-label', tr('hnav.search'));
    renderFamilies();
    renderResults();
    paintRoots();
  }

  /** Le routeur a changé d'écran : hôte ouvert et entrée active. */
  function sync(r) {
    route = r || (root.Router && Router.current()) || null;
    render();
  }

  async function load() {
    try {
      const r = await fetch('/api/hosts');
      const d = await r.json().catch(() => null);
      if (!r.ok || !d) throw new Error((d && (d.hint || d.error)) || `HTTP ${r.status}`);
      render(d);
    } catch (e) {
      lastError = e.message;
      if (!data) renderFamilies();
    }
  }

  // ---------------------------------------------------------------------
  // Gestes
  // ---------------------------------------------------------------------
  async function toggleStar(key, btn) {
    const h = data && (data.hosts || []).find((x) => keyOf(x) === key);
    if (!h) return;
    const want = !h.favorite;
    h.favorite = want;                       // tout de suite à l'écran
    render();
    try {
      const r = await fetch(`/api/me/favorites/${enc(h.kind)}/${enc(h.name)}`, { method: want ? 'PUT' : 'DELETE' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    } catch (e) {
      h.favorite = !want;
      render();
      const again = document.querySelector(`#host-nav [data-star="${CSS.escape(key)}"]`) || btn;
      if (again) again.title = tr('hnav.star.failed', { error: e.message });
    }
  }

  function toggleFamily(kind) {
    ls.set(COLLAPSE_KEY(kind), isCollapsed(kind) ? '0' : '1');
    renderFamilies();
  }

  function focusSearch() {
    const q = $('#hnav-q');
    if (!q) return;
    q.focus();
    q.select();
  }

  /** Entrée : le premier hôte trouvé, sinon la première VM. */
  function openFirst() {
    const first = document.querySelector('#hnav-families .hnav-host-name');
    if (query && first) { first.click(); return; }
    const vm = document.querySelector('#hnav-results .hnav-vm');
    if (vm) vm.click();
  }

  /** Aller à une VM : l'écran des VMs de son hôte, filtré sur elle. */
  function goVm(v) {
    if (v.kind === 'harvester' && root.App && App.focusVm) App.focusVm(v.host, v.ns, v.name);
    if (v.kind === 'kvm' && root.KVM && KVM.preset) KVM.preset({ host: v.host, text: v.name });
  }

  // « + Ajouter un hôte » : une fenêtre à deux choix
  let prevFocus = null;
  function openAdd() {
    const m = $('#host-add-modal');
    if (!m) return;
    prevFocus = document.activeElement;
    m.classList.add('active');
    m.setAttribute('aria-hidden', 'false');
    m.querySelector('[data-add-kind]')?.focus();
  }
  function closeAdd() {
    const m = $('#host-add-modal');
    if (!m) return;
    m.classList.remove('active');
    m.setAttribute('aria-hidden', 'true');
    if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) { /* élément parti */ } }
    prevFocus = null;
  }

  function init() {
    const nav = $('#host-nav');
    if (!nav) return;
    nav.addEventListener('click', (e) => {
      const star = e.target.closest('[data-star]');
      if (star) { e.preventDefault(); toggleStar(star.dataset.star, star); return; }
      const fam = e.target.closest('[data-fam]');
      if (fam) { e.preventDefault(); toggleFamily(fam.dataset.fam); return; }
      const vm = e.target.closest('[data-vm]');
      if (vm) {
        const v = ($('#hnav-results') || {})._vms?.[Number(vm.dataset.vm)];
        if (v) goVm(v);
      }
      if (e.target.closest('#hnav-add')) { e.preventDefault(); openAdd(); }
    });
    const q = $('#hnav-q');
    q?.addEventListener('input', () => { query = q.value.trim(); render(); });
    q?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); openFirst(); }
      if (e.key === 'Escape') { q.value = ''; query = ''; render(); q.blur(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      e.preventDefault();
      focusSearch();
    });
    const m = $('#host-add-modal');
    m?.addEventListener('click', (e) => {
      if (e.target === m || e.target.closest('[data-add-close]')) { closeAdd(); return; }
      const b = e.target.closest('[data-add-kind]');
      if (!b) return;
      closeAdd();
      if (b.dataset.addKind === 'harvester' && root.Clusters) Clusters.openAddForm();
      if (b.dataset.addKind === 'kvm' && root.KVM) KVM.openDeclare();
    });
    m?.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeAdd();
      if (e.key !== 'Tab') return;
      const f = [...m.querySelectorAll('button')].filter((x) => !x.disabled && x.offsetParent !== null);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    });
    render();
    load();
    timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  }

  document.addEventListener('DOMContentLoaded', init);
  root.HostNav = Object.assign({ render, sync, load, focusSearch, openAdd, closeAdd,
                                 _data: () => data }, pure);
})(typeof window !== 'undefined' ? window : globalThis);
