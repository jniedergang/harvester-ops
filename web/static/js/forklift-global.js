/**
 * harvester-ops : vue globale « Migrations (tous clusters) » (v1.76.0)
 *
 * Un tableau lecture seule, à côté d'Activity dans le menu latéral (pas sous
 * Cluster) : chaque VM VMware prise dans une vague de Forklift, quel que
 * soit le cluster cible, avec son vCenter, la vague, l'étape, la dernière
 * copie et la bascule. Une ligne d'en-tête par cluster (Forklift prêt,
 * importeur, sources) ; un cluster injoignable est montré tel quel, sans
 * bloquer les autres. Un clic sur une vague bascule vers ce cluster et
 * ouvre son onglet Vagues.
 *
 * Seconde vue (v1.80.0) : les mêmes vagues en couloirs sur un axe du temps
 * commun, un couloir par cluster et vague (ForkliftLanes) ; un clic ouvre
 * la fenêtre de suivi de la vague. Tableau ou Couloirs, retenu par le
 * navigateur.
 *
 * Le vCenter d'une vague : celui du fournisseur que porte son plan
 * (`wave.provider`, espace et nom), retrouvé dans la liste des fournisseurs
 * du cluster ; à défaut (fournisseur supprimé), tous ceux du cluster.
 */
const ForkliftGlobal = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const getJSON = (url) => fetch(url).then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const REFRESH_MS = 15000;

  let host = null;
  let timer = null;
  let data = null;
  const filters = { status: '', vcenter: '' };
  const VIEW_KEY = 'harvester_ops_fkg_view';
  const viewMode = () => {
    try { return localStorage.getItem(VIEW_KEY) === 'lanes' ? 'lanes' : 'table'; } catch { return 'table'; }
  };
  function viewToggle() {
    const mode = viewMode();
    const tab = (m, ic, label, tip) => `<button type="button" class="sub-tab tip${mode === m ? ' active' : ''}" role="tab"
        aria-selected="${mode === m}" data-fkg="view" data-mode="${m}" data-tip="${esc(tip)}">${icon(ic)} <span>${esc(label)}</span></button>`;
    return `${tab('table', 'doc', tr('fkl.view.table'), tr('fkl.t.table'))}${tab('lanes', 'metrics', tr('fkl.view.lanes'), tr('fkl.t.lanesAll'))}`;
  }

  // Mêmes libellés d'état que l'onglet Vagues d'un cluster (Forklift.js) :
  // clés littérales identiques, pour ne pas dupliquer les traductions.
  const WAVE_STATE = {
    ready: ['info', () => tr('fk.w.st.ready')], pending: ['warn', () => tr('fk.w.st.pending')],
    invalid: ['fail', () => tr('fk.w.st.invalid')], copying: ['info', () => tr('fk.w.st.copying')],
    'cutover-scheduled': ['warn', () => tr('fk.w.st.cutoverScheduled')],
    'cutting-over': ['warn', () => tr('fk.w.st.cuttingOver')], succeeded: ['ok', () => tr('fk.w.st.succeeded')],
    failed: ['fail', () => tr('fk.w.st.failed')], 'rolled-back': ['warn', () => tr('fk.w.st.rolledBack')],
    closed: ['', () => tr('fk.w.st.closed')],
  };
  const stateBadge = (w) => {
    const [cls, label] = WAVE_STATE[w.state] || ['warn', () => w.state];
    return `<span data-fkg-state="${esc(w.state)}">${badge(cls, label(), w.message || '')}</span>`;
  };
  // Regroupement des dix états en quatre familles pour le filtre : une
  // vague en cours de validation ou de copie compte comme « En cours ».
  const STATE_BUCKET = {
    pending: 'progress', ready: 'progress', copying: 'progress',
    'cutover-scheduled': 'cutover', 'cutting-over': 'cutover',
    succeeded: 'finished', 'rolled-back': 'finished', closed: 'finished',
    failed: 'failed', invalid: 'failed',
  };
  const CDI_LABEL = {
    'suse-no-vddk': ['fail', () => tr('fk.cdi.suse')], upstream: ['ok', () => tr('fk.cdi.upstream')],
    other: ['warn', () => tr('fk.cdi.other')],
  };
  const STATUS_OPTIONS = [
    ['progress', () => tr('fkg.filter.inProgress')],
    ['cutover', () => tr('fkg.filter.cutover')],
    ['finished', () => tr('fkg.filter.finished')],
    ['failed', () => tr('fkg.filter.failed')],
  ];

  const fmtWhen = (iso) => {
    const t = Date.parse(iso || '');
    return Number.isFinite(t) ? new Date(t).toLocaleString() : '';
  };
  /** Un instant à venir, avec son compte à rebours (comme l'onglet Vagues,
   *  sans le tenir à jour ici : cette vue se relit déjà toutes les 15 s). */
  const fmtDur = (sec) => {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return `${h} h ${String(m).padStart(2, '0')} min`;
    if (m) return `${m} min ${String(r).padStart(2, '0')} s`;
    return `${r} s`;
  };
  // Une bascule immédiate pose aussi `spec.cutover` (à l'instant du clic) :
  // vue un peu plus tard, cette date est déjà passée, et restait montrée
  // « (due now) » indéfiniment. Passé l'instant, ce n'était pas « prévu à
  // maintenant » mais une bascule sans délai : on le dit ainsi ; seule une
  // bascule programmée encore à venir garde le compte à rebours.
  const whenWithCountdown = (iso) => {
    const ms = Date.parse(iso || '') - Date.now();
    const left = ms > 0 ? tr('fk.w.in', { left: fmtDur(ms / 1000) }) : tr('fkg.cutoverImmediate');
    return `${esc(fmtWhen(iso))} (${esc(left)})`;
  };
  const hostOf = (url) => {
    try { return new URL(url).host; } catch { return String(url || '').replace(/^https?:\/\//, '').split('/')[0]; }
  };
  const uniq = (arr) => Array.from(new Set(arr));

  function fillSelect(sel, options, current, allLabelFn) {
    if (!sel) return;
    sel.innerHTML = [`<option value="">${esc(allLabelFn())}</option>`]
      .concat(options.map(([v, labelFn]) => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>${esc(labelFn())}</option>`))
      .join('');
  }

  // -- cycle de vie -----------------------------------------------------------
  function start(host_) {
    stop();
    host = host_;
    if (!host) return Promise.resolve();
    host.innerHTML = `<div class="card na-card fkg-card">
        <div class="res-tools">
          <span class="res-count"></span>
          <select data-fkg="f-status" class="tip" data-tip="${esc(tr('fkg.filter.statusTip'))}" aria-label="Status"></select>
          <select data-fkg="f-vcenter" class="tip" data-tip="${esc(tr('fkg.filter.vcenterTip'))}" aria-label="vCenter"></select>
          <button type="button" class="btn btn-sm btn-secondary tip" data-fkg="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="res-feedback" data-fkg="feedback"></div>
        <div data-fkg="clusters" class="fk-sources"></div>
        <div class="sub-tabs sub-tabs-inline fk-view-toggle" role="tablist" data-fkg="views"></div>
        <div data-fkg="table"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
      </div>`;
    const card = host.querySelector('.fkg-card');
    card.addEventListener('click', onClick);
    card.addEventListener('change', onChange);
    timer = setInterval(backgroundRefresh, REFRESH_MS);
    return load();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
    host = null;
    data = null;
  }

  /** Relecture de fond : seulement tant que la vue est montée (arrêtée par
   *  App.setTab en quittant l'onglet) et l'onglet du navigateur visible. */
  function backgroundRefresh() {
    if (document.hidden || !host || !host.isConnected) return Promise.resolve();
    return load();
  }

  async function load() {
    const h = host;
    const d = await getJSON('/api/forklift-global');
    if (h !== host) return;   // la vue a été quittée pendant la requête
    data = d || { clusters: [] };
    render();
  }

  // -- rendu --------------------------------------------------------------
  function allRows(clusters) {
    const rows = [];
    clusters.forEach((c) => {
      const vcenterHosts = uniq((c.providers || []).map((p) => hostOf(p.url)).filter(Boolean));
      (c.waves || []).forEach((w) => {
        const own = (c.providers || []).find((p) => w.provider && p.name === w.provider.name
                                                  && (p.namespace || '') === (w.provider.namespace || ''));
        const hosts = own && hostOf(own.url) ? [hostOf(own.url)] : vcenterHosts;
        (w.vms || []).forEach((vm) => rows.push({ cluster: c.cluster, wave: w, vm, vcenterHosts: hosts }));
      });
    });
    return rows;
  }

  function matchesFilters(row) {
    if (filters.status) {
      const bucket = STATE_BUCKET[row.wave.state] || 'progress';
      if (bucket !== filters.status) return false;
    }
    if (filters.vcenter && !row.vcenterHosts.includes(filters.vcenter)) return false;
    return true;
  }

  function clusterHeader(c) {
    const hosts = uniq((c.providers || []).map((p) => hostOf(p.url)).filter(Boolean));
    const stBadge = !c.reachable ? badge('fail', tr('fkg.unreachable'), tr('fkg.unreachable'))
      : c.forklift_ready ? badge('ok', tr('fkg.ready'), '')
      : badge('warn', tr('fkg.notInstalled'), '');
    const cdi = c.reachable && CDI_LABEL[c.cdi_importer_kind];
    const importerBadge = cdi ? badge(cdi[0], cdi[1](), '') : '';
    const sourcesTxt = hosts.length ? `${tr('fkg.sources', { n: hosts.length })} : ${hosts.join(', ')}` : tr('fkg.noSources');
    const wavesTxt = tr('fkg.waves', { n: (c.waves || []).length });
    return `<div class="fk-source" data-fkg-clusterhead="${esc(c.cluster)}">
        <div class="fk-step-head"><b>${esc(c.cluster)}</b> ${stBadge} ${importerBadge}</div>
        <div class="form-hint">${esc(sourcesTxt)} · ${esc(wavesTxt)}</div>
      </div>`;
  }

  // Mêmes clés que l'onglet Vagues (Forklift.js) : les noms d'étape du
  // pipeline Forklift sont en anglais quelle que soit la langue de la
  // console, traduits ici plutôt que montrés tels quels. Une étape inconnue
  // retombe sur le texte brut envoyé par le serveur.
  const STEP_NAME_I18N = {
    Initialize: 'fk.stepName.initialize', DiskTransfer: 'fk.stepName.diskTransfer',
    DiskAllocation: 'fk.stepName.diskAllocation', Cutover: 'fk.stepName.cutover',
    ImageConversion: 'fk.stepName.imageConversion', VirtualMachineCreation: 'fk.stepName.virtualMachineCreation',
  };

  // Même lecture que l'onglet Vagues : entre deux copies incrémentales
  // (`CopyingPaused`), l'étape brute affichée par Forklift se lit comme une
  // bascule commencée ; on le dit, avec le prochain moment de copie connu.
  function stepText(vm) {
    if (vm.step_name === 'CopyingPaused') return `${esc(tr('fk.w.copyingPaused'))}${vm.next_precopy ? ` (${whenWithCountdown(vm.next_precopy)})` : ''}`;
    const key = STEP_NAME_I18N[vm.step_name];
    return key ? esc(tr(key)) : esc(vm.step || '–');
  }

  /** Une vague close ou revenue à la source : la VM qu'on y a fait revenir
   *  ne montre plus sa dernière étape Forklift, mais qu'elle est repartie
   *  sur son hôte d'origine. */
  function vmStepText(wave, vm) {
    if (vm.blocked) return `<span class="fk-blocked tip" data-tip="${esc(vm.blocked)}">${icon('warn', 12)} ${esc(tr('fk.w.blocked'))}</span>`;
    if ((wave.state === 'closed' || wave.state === 'rolled-back') && vm.rolled_back) return esc(tr('fk.stepName.rolledBack'));
    return stepText(vm);
  }

  function vmRow(row) {
    const { cluster, wave, vm, vcenterHosts } = row;
    const vcenter = vcenterHosts.length ? vcenterHosts.join(', ') : '–';
    const last = vm.last_precopy && vm.last_precopy.end ? esc(fmtWhen(vm.last_precopy.end)) : '–';
    const cutover = wave.cutover ? whenWithCountdown(wave.cutover)
      : vm.rolled_back ? esc(tr('fk.w.st.rolledBack')) : '–';
    const step = vm.error
      ? `<span class="tip" data-tip="${esc(vm.error)}">${icon('warn', 12)} ${vmStepText(wave, vm)}</span>`
      : vmStepText(wave, vm);
    return `<tr class="tip" data-fkg-cluster="${esc(cluster)}" data-fkg-wave="${esc(wave.name)}"
          data-tip="${esc(tr('fkg.rowTip', { cluster }))}">
        <td>${esc(vm.name || vm.id)}</td>
        <td>${esc(vcenter)}</td>
        <td>${esc(cluster)}</td>
        <td>${esc(wave.name)} ${stateBadge(wave)}</td>
        <td>${step}</td>
        <td>${last}</td>
        <td>${cutover}</td>
      </tr>`;
  }

  function render() {
    if (!host || !host.isConnected || !data) return;
    const card = host.querySelector('.fkg-card');
    if (!card) return;
    const clusters = data.clusters || [];
    const allHosts = uniq(clusters.flatMap((c) => (c.providers || []).map((p) => hostOf(p.url)).filter(Boolean)));
    fillSelect(card.querySelector('[data-fkg="f-status"]'), STATUS_OPTIONS, filters.status, () => tr('fkg.filter.all'));
    fillSelect(card.querySelector('[data-fkg="f-vcenter"]'), allHosts.map((h) => [h, () => h]), filters.vcenter,
               () => tr('fkg.filter.allVcenters'));
    card.querySelector('[data-fkg="clusters"]').innerHTML = clusters.map(clusterHeader).join('');
    card.querySelector('[data-fkg="views"]').innerHTML = viewToggle();
    const tableHost = card.querySelector('[data-fkg="table"]');
    if (viewMode() === 'lanes' && window.ForkliftLanes) {
      // une vague par couloir, avec les mêmes filtres que le tableau
      const lanes = [];
      allRows(clusters).filter(matchesFilters).forEach((r) => {
        if (!lanes.some((l) => l.cluster === r.cluster && l.wave.name === r.wave.name)) lanes.push({ cluster: r.cluster, wave: r.wave });
      });
      card.querySelector('.res-count').textContent = tr('fkl.count', { n: lanes.length });
      ForkliftLanes.paint(tableHost, lanes, { scope: '*all', showCluster: true, stateBadge, onOpen: openFollow });
      if (window.Forklift && Forklift.tick) Forklift.tick();
      return;
    }
    const rows = allRows(clusters).filter(matchesFilters);
    card.querySelector('.res-count').textContent = tr('fkg.count', { n: rows.length });
    tableHost.innerHTML = rows.length
      ? `<table class="data-table"><thead><tr>
            <th>${esc(tr('fkg.col.vm'))}</th><th>${esc(tr('fkg.col.vcenter'))}</th>
            <th>${esc(tr('fkg.col.cluster'))}</th><th>${esc(tr('fkg.col.wave'))}</th>
            <th>${esc(tr('fkg.col.step'))}</th><th>${esc(tr('fkg.col.lastCopy'))}</th>
            <th>${esc(tr('fkg.col.cutover'))}</th></tr></thead>
          <tbody>${rows.map(vmRow).join('')}</tbody></table>`
      : `<p class="form-hint">${esc(tr('fkg.none'))}</p>`;
  }

  // -- gestes ---------------------------------------------------------------
  function gotoWave(cluster) {
    const sel = document.querySelector('#cluster-select');
    if (sel && sel.value !== cluster) {
      sel.value = cluster;
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    }
    if (window.Sections) Sections.open('forklift', 'waves');
  }

  /** Clic sur un couloir : la fenêtre de suivi de la vague, sans quitter la vue. */
  function openFollow(cluster, wave) {
    if (window.Forklift && Forklift.follow) Forklift.follow(cluster, wave);
    else gotoWave(cluster);
  }

  function onClick(e) {
    if (e.target.closest('[data-fkg="refresh"]')) { load(); return; }
    const v = e.target.closest('[data-fkg="view"]');
    if (v) {
      try { localStorage.setItem(VIEW_KEY, v.dataset.mode === 'lanes' ? 'lanes' : 'table'); } catch { /* navigation privée */ }
      render();
      return;
    }
    const row = e.target.closest('[data-fkg-wave]');
    if (row) gotoWave(row.dataset.fkgCluster);
  }

  function onChange(e) {
    const t = e.target;
    if (t.matches('[data-fkg="f-status"]')) { filters.status = t.value; render(); }
    else if (t.matches('[data-fkg="f-vcenter"]')) { filters.vcenter = t.value; render(); }
  }

  return { start, stop };
})();
window.ForkliftGlobal = ForkliftGlobal;
