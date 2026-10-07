/**
 * harvester-ops : migrations VMware par Forklift, onglet d'un cluster (v1.76.0)
 *
 * Trois onglets de la section « Migrations VMware » :
 * - Préparation : Forklift sur le cluster, l'importeur de disques (CDI),
 *   l'image VDDK, les sources et l'intervalle des copies incrémentales,
 *   dans l'ordre où il faut les faire, chacun avec son état et son geste ;
 * - Sources vCenter : un bloc par fournisseur vSphere de Forklift, ajout et
 *   modification en fenêtre (un vCenter de VM Import se reprend sans
 *   ressaisir son mot de passe, lu par le serveur) ;
 * - Inventaire : ce que Forklift voit d'un vCenter, avec la raison de refus
 *   par VM (CBT, outils VMware) et une sélection pour composer une vague ;
 * - Vagues : un bloc par vague avec ses gestes selon l'état (lancer,
 *   basculer maintenant ou à une date, revenir à la source, clore,
 *   supprimer), la fenêtre de composition et la fenêtre de suivi ; ou,
 *   au choix (v1.80.0), toutes les vagues en couloirs sur un axe du temps
 *   (ForkliftLanes, forklift-lanes.js).
 * Toute écriture passe par l'outil harvester-forklift, en action suivie.
 */
const Forklift = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const REFRESH_MS = 10000;
  const KINDS = ['prep', 'sources', 'inventory', 'waves'];
  const size = (n) => {
    if (!n) return '–';
    const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
  };

  // { cluster, host, kind, data, store, timer, invToken, dirty, uploading, uploadNote }
  // dirty : les formulaires de la Préparation en cours de saisie (Set de
  // 'vddk', 'cdi', 'precopy') ; uploading : une archive part. La relecture
  // de fond ne redessine jamais un formulaire en cours de saisie (ni l'étape
  // VDDK pendant un envoi), comme la fenêtre de mise à jour.
  let cur = null;
  let lastInv = null;   // { cluster, source, namespace, kind, q, warm } : retrouvé au retour sur l'onglet (U4)

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) {
      const e = new Error(d.hint || d.error || `HTTP ${r.status}`);
      if (d.skipped) e.skipped = d.skipped;
      throw e;
    }
    return d;
  }
  const getJSON = (url) => fetch(url).then(r => (r.ok ? r.json() : null)).catch(() => null);

  function follow(id, into, text, onDone) {
    if (window.Dock && Dock.poll) Dock.poll();
    if (window.VMActions && VMActions.follow) VMActions.follow(id, into, text, onDone);
    else if (into) into.textContent = tr('bk.started', { id });
  }

  // -- cycle de vie ---------------------------------------------------------
  function start(cluster, host) {
    stop();
    const kind = KINDS.includes(host.dataset.fk) ? host.dataset.fk : 'prep';
    cur = { cluster, host, kind, data: null, store: null, dirty: new Set(), uploading: false, uploadNote: '' };
    // v1.75.0 : le bouton « Ajouter un vCenter » n'a sa place que sur l'onglet Sources
    const newBtn = kind === 'sources'
      ? `<button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="new-source" data-tip="${esc(tr('fk.t.newSource'))}">${icon('add')} ${esc(tr('fk.newSource'))}</button>`
      : kind === 'waves'
        ? `<button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="waves-compose" data-tip="${esc(tr('fk.t.wavesCompose'))}">${icon('build')} ${esc(tr('fk.composeWave'))}</button>` : '';
    host.innerHTML = `<div class="card na-card fk-card">
        <div class="res-tools"><span class="res-count"></span>${newBtn}
          <button type="button" class="btn btn-sm btn-secondary tip" data-fk="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="res-feedback" data-fk="feedback"></div>
        <div data-fk="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = host.querySelector('.fk-card');
    card.addEventListener('click', onClick);
    card.addEventListener('change', onChange);
    card.addEventListener('input', onInput);
    cur.timer = setInterval(backgroundRefresh, REFRESH_MS);
    cur.ready = load();
    return cur.ready;
  }

  function stop() {
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  /** La relecture de fond (toutes les REFRESH_MS). L'inventaire ouvre un
   *  tunnel vers le cluster : pas de relecture automatique pour lui. */
  function backgroundRefresh() {
    if (cur && cur.kind !== 'inventory' && cur.host.isConnected && !cur.host.closest('[hidden]')) return load();
    return Promise.resolve();
  }

  async function load() {
    if (!cur) return;
    const c = cur;
    // les destinations (réseaux, classes, namespaces) ne sont lues que par la
    // fenêtre de composition : la liste des vagues ne s'en sert jamais
    const [d, store] = await Promise.all([getJSON(`/api/forklift/${enc(c.cluster)}`),
                                          c.kind === 'prep' ? getJSON('/api/forklift-vddk') : Promise.resolve(c.store)]);
    if (c !== cur) return;
    // une saisie ou un envoi en cours : un aléa de la relecture de fond
    // (réseau, cluster injoignable) ne doit jamais effacer le formulaire
    if ((c.dirty.size || c.uploading) && (!d || d.error || d.unreachable)) return;
    c.data = d;
    c.store = store;
    render();
    if (d && !d.error && !d.unreachable) paintFollows(c.cluster, d);
  }

  function render() {
    if (!cur || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-fk="body"]');
    const d = cur.data;
    // (fix) « Ajouter un vCenter » n'a de sens qu'une fois Forklift prêt : le
    // bouton du bandeau existe dès l'ouverture de l'onglet, avant les données.
    const newBtn = cur.host.querySelector('[data-fk="new-source"]');
    if (newBtn) {
      const ready = !!(d && !d.error && !d.unreachable && d.install && d.install.ready);
      newBtn.disabled = !ready;
      newBtn.setAttribute('data-tip', ready ? tr('fk.t.newSource') : tr('fk.needInstall'));
    }
    if (!d || d.error || d.unreachable) {
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc((d && d.error) || tr('fabric.unreachable'))}</div></div>`;
      return;
    }
    if (cur.kind === 'prep') {
      const steps = prepSteps(d);
      if ((cur.dirty.size || cur.uploading) && body.querySelector('[data-fk-step="vddk"]')) {
        // une saisie ou un envoi en cours n'est jamais effacé : chaque partie
        // se redessine, sauf les formulaires en cours de saisie (et l'étape
        // VDDK pendant un envoi), laissés tels quels
        const keep = (k) => cur.dirty.has(k) || (k === 'vddk' && cur.uploading);
        const parts = [['forklift', '[data-fk-step="forklift"]', steps.one], ['cdi', '[data-fk-step="cdi"]', steps.cdi],
                       ['vddk', '[data-fk-step="vddk"]', steps.vddk], ['sources', '[data-fk-step="sources"]', steps.sources],
                       ['precopy', '[data-fk-precopy]', steps.precopy]];
        for (const [k, sel, html] of parts) {
          const el = body.querySelector(sel);
          if (el && !keep(k)) el.outerHTML = html;
        }
      } else {
        body.innerHTML = steps.one + steps.cdi + steps.vddk + steps.sources + steps.precopy;
      }
    } else if (cur.kind === 'sources') body.innerHTML = sourcesView(d);   // U4
    else if (cur.kind === 'waves') { paintWaves(body, d); tick(); }   // W6, couloirs v1.80.0
    else inventoryView(body, d);                                        // U4
  }

  // -- Préparation ------------------------------------------------------------
  function installLine(st) {
    const parts = [
      ['cert-manager', st.cert_manager, tr('fk.t.certManager')],
      [tr('fk.addon'), st.addon === 'ready', st.addon_message],
      [tr('fk.operator'), st.operator, tr('fk.t.operator')],
      [tr('fk.controller'), st.controller, tr('fk.t.controller')],
      [tr('fk.components'), st.controller && !st.components_missing.length,
       st.components_missing.length ? tr('fk.missing', { list: st.components_missing.join(', ') }) : tr('fk.t.components')],
      // le compte qui lit l'inventaire : posé en fin d'installation, il
      // manque si elle s'est arrêtée avant, ou si Forklift a été posé autrement
      [tr('fk.invAccess'), !!st.inventory_access, tr('fk.t.invAccess')],
    ];
    return parts.map(([label, ok, tip]) => `<span class="fk-part tip" data-tip="${esc(tip)}">${icon(ok ? 'ok' : 'fail', 12)} ${esc(label)}</span>`).join(' ');
  }

  function stepBox(id, n, title, stateHtml, bodyHtml) {
    return `<div class="fk-step" data-fk-step="${id}"><div class="fk-step-head"><span class="fk-step-n">${n}</span>
      <b>${esc(title)}</b> ${stateHtml}</div>${bodyHtml}</div>`;
  }

  // -- Importeur CDI (v1.76.0) -------------------------------------------
  const CDI_BADGE = { 'suse-no-vddk': ['fail', () => tr('fk.cdi.suse')], upstream: ['ok', () => tr('fk.cdi.upstream')],
                      other: ['warn', () => tr('fk.cdi.other')] };

  // L'image amont que l'outil poserait vraiment : `quay.io/.../cdi-importer:v<version>`.
  // La page ne porte pas la version de CDI (elle vient d'une sous-commande à
  // part, `cdi-importer` côté CLI) : on la devine dans le tag de l'image en
  // place quand l'importeur courant est déjà l'un des deux connus (upstream,
  // ou SUSE sans VDDK, ex. `v1.65.0`) ; sinon un exemple neutre, sans version.
  const UPSTREAM_CDI_IMAGE = 'quay.io/kubevirt/cdi-importer';
  function upstreamCdiExample(d) {
    if (d.cdi_version) return `${UPSTREAM_CDI_IMAGE}:v${String(d.cdi_version).replace(/^v/, '')}`;
    const c = d.cdi_importer || {};
    if (c.kind === 'upstream' || c.kind === 'suse-no-vddk') {
      const m = /:v?(\d+\.\d+\.\d+)$/.exec(c.image || '');
      if (m) return `${UPSTREAM_CDI_IMAGE}:v${m[1]}`;
    }
    return UPSTREAM_CDI_IMAGE;
  }

  function cdiStep(d) {
    const c = d.cdi_importer || { image: '', kind: 'other', original: '' };
    const [cls, label] = CDI_BADGE[c.kind] || CDI_BADGE.other;
    const orig = c.original || '';
    return stepBox('cdi', 2, tr('fk.step.cdi'), badge(cls, label(), c.image),
      `<p class="form-hint">${esc(tr('fk.cdiHint'))}</p>
       ${c.kind === 'suse-no-vddk' ? `<div class="sto-finding sev-critical"><div class="sto-finding-title">${icon('warn')} ${esc(tr('fk.cdi.warnSuse'))}</div></div>` : ''}
       <p class="form-hint">${esc(tr('fk.cdi.upgradeWarn'))}</p>
       <div class="fk-form">
         ${field('cdi_image', tr('fk.f.cdiImage'), `<input name="cdi_image" placeholder="${esc(upstreamCdiExample(d))}">`, tr('fk.t.cdiImage'))}
         <div class="fk-source-actions">
           <button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="cdi-upstream" ${c.kind === 'upstream' ? 'disabled' : ''}
             data-tip="${esc(tr('fk.t.cdiUpstream'))}">${icon('download')} ${esc(tr('fk.cdi.useUpstream'))}</button>
           <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-fk="cdi-original" ${orig ? '' : 'disabled'}
             data-tip="${esc(orig ? tr('fk.t.cdiOriginal', { image: orig }) : tr('fk.t.cdiNoOriginal'))}">${icon('undo')} ${esc(tr('fk.cdi.useOriginal'))}</button>
         </div>
       </div>`);
  }

  // -- Intervalle des copies incrémentales (v1.76.0) -----------------------
  function precopyBox(d) {
    // la route rend toujours la valeur (défaut du contrôleur compris)
    const minutes = d.precopy_interval;
    return `<div class="fk-precopy" data-fk-precopy>
      <div class="fk-step-head"><b>${esc(tr('fk.step.precopy'))}</b> ${badge('info', tr('fk.precopy.current', { minutes }))}</div>
      <p class="form-hint">${esc(tr('fk.precopyHint'))}</p>
      <div class="fk-form">
        ${field('precopy_minutes', tr('fk.f.precopyMinutes'), `<input name="precopy_minutes" type="number" min="5" max="1440" value="${esc(minutes)}">`, tr('fk.t.precopyMinutes'))}
        <button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="precopy-save" data-tip="${esc(tr('fk.t.precopySave'))}">${icon('save')} ${esc(tr('fk.precopy.save'))}</button>
        <span class="form-hint" data-fk="precopy-msg"></span>
      </div></div>`;
  }

  /** Les étapes de la Préparation, séparément : la relecture de fond peut
   *  n'en redessiner qu'une partie. */
  function prepSteps(d) {
    const st = d.install;
    // un add-on déployé ne dit pas une installation finie (composants ou
    // accès à l'inventaire manquants) : « incomplet », pas « prêt »
    const addonState = { ready: ['warn', tr('fk.st.incomplete')], deploying: ['warn', tr('fk.st.deploying')],
                         failed: ['fail', tr('fk.st.failed')], disabled: ['warn', tr('fk.st.disabled')],
                         absent: ['warn', tr('fk.st.absent')] };
    const [cls, txt] = st.ready ? ['ok', tr('fk.st.ready')] : (addonState[st.addon] || ['warn', st.addon]);
    const canInstall = st.cert_manager || d.bundle;
    const one = stepBox('forklift', 1, tr('fk.step.forklift'), badge(cls, txt, st.addon_message),
      `<p class="form-hint">${esc(tr('fk.forkliftHint'))}</p><div class="fk-parts">${installLine(st)}</div>
       ${d.harvester_addon ? `<p class="form-hint">${esc(tr('fk.harvesterAddon'))}</p>` : ''}
       ${st.ready ? '' : `<button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="install" ${canInstall ? '' : 'disabled'}
          data-tip="${esc(canInstall ? tr('fk.t.install') : tr('fk.t.noBundle'))}">${icon('download')} ${esc(st.addon === 'absent' ? tr('fk.install') : tr('fk.resume'))}</button>`}`);
    const cdi = cdiStep(d);
    const v = d.vddk;
    const archives = (cur.store && cur.store.archives) || [];
    const pick = (v && archives.some(a => a.name === v.archive)) ? v.archive : (archives[0] && archives[0].name) || '';
    const vddk = stepBox('vddk', 3, tr('fk.step.vddk'),
      v ? badge('ok', v.image, tr('fk.t.vddkDone', { digest: v.digest.slice(0, 19), when: v.pushed_at })) : badge('warn', tr('fk.st.todo')),
      `<p class="form-hint">${esc(tr('fk.vddkHint'))}</p>
       <div class="fk-form">
         ${field('archive', tr('fk.f.archive'), `<select name="archive">${archives.length ? opts(archives.map(a => [a.name, `${a.name} (${size(a.size)})`]), pick) : `<option value="">${esc(tr('fk.noArchive'))}</option>`}</select>`, tr('fk.t.archive'))}
         <div class="fk-upload">
           <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-fk="upload-vddk" data-tip="${esc(tr('fk.t.upload'))}">${icon('upload')} ${esc(tr('fk.upload'))}</button>
           <input type="file" accept=".tar.gz" data-fk="upload-file" hidden>
           <span class="form-hint" data-fk="upload-line">${cur.uploadNote || ''}</span>
         </div>
         ${field('image', tr('fk.f.image'), `<input name="image" value="${esc((v && v.image) || d.registry.image)}" placeholder="registry.lan/harvops/vddk:8.0.3">`,
                 d.registry.host ? tr('fk.t.imageHint', { host: d.registry.host }) : tr('fk.t.image'))}
         <label class="fk-check tip" data-tip="${esc(tr('fk.t.plainHttp'))}"><input type="checkbox" name="plain_http" ${d.registry.plain_http ? 'checked' : ''}> ${esc(tr('fk.f.plainHttp'))}</label>
         ${d.registry.auth ? `<label class="fk-check tip" data-tip="${esc(tr('fk.t.clusterAuth', { host: d.registry.host }))}"><input type="checkbox" name="use_cluster_auth" checked> ${esc(tr('fk.f.clusterAuth'))}</label>` : ''}
         <div data-fk="reg-creds" ${d.registry.auth ? 'hidden' : ''}>
           ${field('username', tr('fk.f.regUser'), '<input name="username" autocomplete="off">', tr('fk.t.regUser'))}
           ${field('password', tr('fk.f.regPassword'), '<input name="password" type="password" autocomplete="new-password">', tr('fk.t.regPassword'))}
         </div>
         <button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="push-vddk" ${archives.length ? '' : 'disabled'} data-tip="${esc(tr('fk.t.push'))}">${icon('upload')} ${esc(tr('fk.push'))}</button>
       </div>`);
    const ready = d.providers.filter(p => p.ready === true).length;
    const sources = stepBox('sources', 4, tr('fk.step.sources'),
      d.providers.length ? badge(ready ? 'ok' : 'warn', tr('fk.st.sources', { ready, total: d.providers.length })) : badge('warn', tr('fk.st.todo')),
      `<p class="form-hint">${esc(tr('fk.sourcesHint'))}</p>
       <button type="button" class="btn btn-sm btn-secondary tip" data-fk="goto-sources" data-tip="${esc(tr('fk.t.gotoSources'))}">${icon('cloud')} ${esc(tr('section.fkSources'))}</button>`);
    return { one, cdi, vddk, sources, precopy: precopyBox(d) };
  }

  // -- Sources vCenter (U4) ----------------------------------------------------
  function sourcesView(d) {
    if (!d.install.ready) {
      return `<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(tr('fk.needInstall'))}</div>
        <button type="button" class="btn btn-sm btn-secondary tip" data-fk="goto-prep" data-tip="${esc(tr('fk.t.gotoPrep'))}">${icon('settings')} ${esc(tr('section.fkPrep'))}</button></div>`;
    }
    cur.host.querySelector('.res-count').textContent = tr('fk.count', { n: d.providers.length });
    if (!d.providers.length) return `<p class="form-hint">${esc(tr('fk.noSource'))}</p>`;
    const state = (p) => (p.ready === true ? badge('ok', tr('fk.src.ready'), p.message)
      : p.ready === false ? badge('fail', tr('fk.src.refused'), p.message) : badge('warn', tr('fk.src.checking'), p.message));
    return `<div class="fk-sources">${d.providers.map(p => `<div class="fk-source" data-fk-source="${esc(p.name)}" data-fk-ns="${esc(p.namespace)}">
        <div class="fk-step-head"><b>${esc(p.name)}</b> ${state(p)}</div>
        <div class="form-hint">${esc(p.url)}</div>
        ${p.ready === false ? `<div class="res-error">${esc(p.message)}</div>` : ''}
        <div class="form-hint">${esc(tr('fk.src.vddk', { image: p.vddk_image || tr('fk.src.noVddk') }))}</div>
        <div class="form-hint">${esc(tr('fk.src.plans', { n: p.plans.length }))}</div>
        <div class="fk-source-actions">
          <button type="button" class="btn btn-sm btn-secondary tip" data-fk="inv-source" data-name="${esc(p.name)}" data-ns="${esc(p.namespace)}" ${p.ready === true ? '' : 'disabled'} data-tip="${esc(tr('fk.t.inventory'))}">${icon('general')} ${esc(tr('section.fkInventory'))}</button>
          <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-fk="edit-source" data-name="${esc(p.name)}" data-ns="${esc(p.namespace)}" ${p.managed ? '' : 'disabled'} data-tip="${esc(p.managed ? tr('fk.t.edit') : tr('fk.t.notManaged'))}">${icon('edit')} ${esc(tr('fk.edit'))}</button>
          <button type="button" class="btn btn-sm btn-danger tip needs-admin" data-fk="del-source" data-name="${esc(p.name)}" data-ns="${esc(p.namespace)}" ${p.plans.length ? 'disabled' : ''} data-tip="${esc(p.plans.length ? tr('fk.t.delUsed', { plans: p.plans.join(', ') }) : tr('fk.t.del'))}">${icon('trash')} ${esc(tr('fk.del'))}</button>
        </div></div>`).join('')}</div>`;
  }

  function sourceForm(p) {
    const edit = !!p;
    // la fenêtre appartient au cluster pour lequel elle s'ouvre : l'onglet
    // peut passer à un autre cluster avant l'envoi
    const cluster = cur.cluster;
    const d = cur.data;
    const vddk = (p && p.vddk_image) || (d.vddk && d.vddk.image) || '';
    const froms = edit ? [] : d.vmimport_sources || [];
    const form = win(cluster, `fk-src-${cluster}-${edit ? p.name : 'new'}`, edit ? tr('fk.editSource', { name: p.name }) : tr('fk.newSource'),
      `<p class="form-hint">${esc(tr('fk.sourceHint'))}</p>
      ${froms.length ? field('from', tr('fk.f.from'), `<select name="from"><option value="">${esc(tr('fk.from.none'))}</option>${
        froms.map(s => `<option value="${esc(`${s.namespace}/${s.name}`)}">${esc(`${s.namespace}/${s.name} (${s.endpoint})`)}</option>`).join('')}</select>`, tr('fk.t.from')) : ''}
      ${field('name', tr('bk.f.name'), `<input name="name" required value="${esc(edit ? p.name : '')}" ${edit ? 'readonly' : ''}>`, tr('fk.t.name'))}
      <div data-fk-typed>
        ${field('url', tr('fk.f.url'), `<input name="url" required placeholder="vcenter.lan" value="${esc(edit ? p.url : '')}">`, tr('fk.t.url'))}
        ${field('user', tr('vi.f.user'), '<input name="user" autocomplete="off" placeholder="administrator@vsphere.local">', edit ? tr('fk.t.userKeep') : tr('fk.t.user'))}
        ${field('password', tr('vi.f.password'), `<input name="password" type="password" autocomplete="new-password" ${edit ? `placeholder="${esc(tr('fk.unchanged'))}"` : 'required'}>`, edit ? tr('fk.t.passwordKeep') : tr('fk.t.password'))}
        <fieldset class="fk-tls"><legend>${esc(tr('fk.f.tls'))}</legend>
          ${edit ? `<label class="fk-check tip" data-tip="${esc(tr('fk.t.tlsKeep'))}"><input type="radio" name="tls" value="keep" checked> ${esc(tr('fk.tls.keep'))}</label>` : ''}
          <label class="fk-check tip" data-tip="${esc(tr('fk.t.tlsCa'))}"><input type="radio" name="tls" value="ca" ${edit ? '' : 'checked'}> ${esc(tr('fk.tls.ca'))}</label>
          <label class="fk-check tip" data-tip="${esc(tr('fk.t.tlsInsecure'))}"><input type="radio" name="tls" value="insecure"> ${esc(tr('fk.tls.insecure'))}</label>
          ${field('cacert', tr('vi.f.ca'), '<textarea name="cacert" rows="4" class="adv-code" placeholder="-----BEGIN CERTIFICATE-----"></textarea>', tr('fk.t.cacert'))}
        </fieldset>
      </div>
      <p class="form-hint" data-fk-from-hint hidden>${esc(tr('fk.fromHint'))}</p>
      ${field('vddk_image', tr('fk.f.vddk'), `<input name="vddk_image" value="${esc(vddk)}">`, tr('fk.t.vddk'))}`, 600);
    const sync = () => {
      const from = form.querySelector('[name="from"]');
      const on = !!(from && from.value);
      form.querySelector('[data-fk-typed]').hidden = on;
      form.querySelector('[data-fk-from-hint]').hidden = !on;
      form.querySelectorAll('[data-fk-typed] [required]').forEach(x => { x.disabled = on; });
      const ca = form.querySelector('[name="tls"]:checked').value === 'ca';
      form.querySelector('[data-f="cacert"]').hidden = !ca;
      if (on && !form.querySelector('[name="name"]').value) form.querySelector('[name="name"]').value = from.value.split('/')[1];
    };
    form.addEventListener('change', sync);
    sync();
    submitWith(form, (f) => {
      const v = (n) => { const x = f.querySelector(`[name="${n}"]`); return x ? x.value.trim() : ''; };
      const spec = { name: v('name') };
      // un fournisseur peut vivre hors de « forklift » (fait par la CLI) :
      // une modification le garde là où il est, jamais recréé ailleurs
      if (edit) spec.namespace = p.namespace;
      const from = v('from');
      if (from) {
        const [namespace, sname] = from.split('/');
        spec.from_vmimport = { namespace, name: sname };
      } else {
        spec.url = v('url');
        spec.user = v('user');
        spec.password = f.querySelector('[name="password"]').value;
        const tls = f.querySelector('[name="tls"]:checked').value;
        if (tls === 'insecure') spec.insecure = true;
        else if (tls === 'ca') { if (v('cacert')) spec.cacert = v('cacert'); else throw new Error(tr('fk.needCa')); }
        // « keep » : ni cacert ni insecure, le serveur reprend le réglage TLS du secret du fournisseur
        if (edit) spec.keep_credentials = true;
      }
      if (v('vddk_image')) spec.vddk_image = v('vddk_image');
      return spec;
    }, 'provider-apply', (s) => tr('fk.done.source', { name: s.name }), cluster);
  }

  // -- Inventaire (U4) ----------------------------------------------------
  const CONCERN = () => ({
    'Changed Block Tracking (CBT) not enabled': tr('fk.c.cbt'),
    'Empty Host Name': tr('fk.c.hostName'),
    'Unsupported operating system detected': tr('fk.c.os'),
    'CPU/Memory hotplug detected': tr('fk.c.hotplug'),
    'Disk serial numbers may be truncated': tr('fk.c.serial'),
    'Shareable disk detected': tr('fk.c.shareable'),
    'RDM disk detected': tr('fk.c.rdm'),
    'VM snapshot detected': tr('fk.c.snapshot'),
  });
  const concernText = (c) => {
    const m = /^Disk - (\S+) does not have CBT enabled$/.exec(c.label || '');
    return m ? tr('fk.c.diskCbt', { disk: m[1] }) : (CONCERN()[c.label] || c.label);
  };
  const SEV = { Critical: 'fail', Warning: 'warn', Information: 'info' };
  // Clés en toutes lettres : le contrôle de parité ne lit que des littéraux.
  const INV_KINDS = ['vms', 'networks', 'datastores'];
  const INV_LABEL = { vms: () => tr('fk.inv.vms'), networks: () => tr('fk.inv.networks'), datastores: () => tr('fk.inv.datastores') };
  const INV_TIP = { vms: () => tr('fk.t.inv.vms'), networks: () => tr('fk.t.inv.networks'), datastores: () => tr('fk.t.inv.datastores') };

  /** Pourquoi une VM ne peut pas entrer dans une vague à chaud, même règle
   *  que `vm_warm_blockers` de la bibliothèque : CBT absent, ou allumée sans
   *  outils VMware en marche (la bascule ne peut alors pas arrêter la
   *  source). Une VM sans raison est éligible. */
  function vmBlockers(row) {
    const out = [];
    if (!row.cbt) out.push(tr('fk.warm.cbt'));
    if (row.power === 'poweredOn' && !row.tools) out.push(tr('fk.warm.tools'));
    return out;
  }

  /** Les VMs cochées de la source courante, avec la source à laquelle elles
   *  appartiennent (W6 : fenêtre « Composer une vague »). */
  function selectedVms() {
    if (!lastInv || lastInv.kind !== 'vms' || !lastInv.selected || !lastInv.selected.size) return [];
    const rows = (cur && cur.invRows) || [];
    return rows.filter(r => lastInv.selected.has(String(r.id || r.name)))
               .map(r => ({ ...r, namespace: lastInv.namespace, source: lastInv.source }));
  }

  function openInventory(name, namespace) {
    lastInv = { cluster: cur ? cur.cluster : (window.App && App.getCurrentCluster()), source: name,
               namespace: namespace || 'forklift', kind: 'vms', q: '', warm: false, selected: new Set() };
    Sections.open('forklift', 'inventory');
  }

  async function inventoryView(body, d) {
    const ready = d.providers.filter(p => p.ready === true);
    if (!ready.length) {
      body.innerHTML = `<p class="form-hint">${esc(tr('fk.inv.noSource'))}</p>`;
      return;
    }
    if (!lastInv || lastInv.cluster !== cur.cluster
        || !ready.some(p => p.name === lastInv.source && p.namespace === lastInv.namespace)) {
      lastInv = { cluster: cur.cluster, source: ready[0].name, namespace: ready[0].namespace, kind: 'vms', q: '', warm: false, selected: new Set() };
    }
    if (!lastInv.selected) lastInv.selected = new Set();
    const s = lastInv;
    // une source hors « forklift » se distingue dans la liste (deux vCenters
    // peuvent porter le même nom dans des namespaces différents)
    const srcOpts = ready.map(p => [`${p.namespace}/${p.name}`, p.namespace === 'forklift' ? p.name : `${p.name} (${p.namespace})`]);
    body.innerHTML = `<div class="fk-inv-tools">
        ${field('source', tr('fk.inv.source'), `<select name="source">${opts(srcOpts, `${s.namespace}/${s.source}`)}</select>`, tr('fk.t.invSource'))}
        <div class="sub-tabs sub-tabs-inline">${INV_KINDS.map(k => `<button type="button" class="sub-tab tip ${k === s.kind ? 'active' : ''}" data-fk="inv-kind" data-kind="${k}" data-tip="${esc(INV_TIP[k]())}">${esc(INV_LABEL[k]())}</button>`).join('')}</div>
        <input name="q" class="tip" data-tip="${esc(tr('fk.t.search'))}" placeholder="${esc(tr('fk.search'))}" value="${esc(s.q)}">
        ${s.kind === 'vms' ? `<label class="fk-check tip" data-tip="${esc(tr('fk.t.warmOnly'))}"><input type="checkbox" name="warm_only" ${s.warm ? 'checked' : ''}> ${esc(tr('fk.warmOnly'))}</label>` : ''}
        <button type="button" class="btn btn-sm btn-secondary tip" data-fk="inv-refresh" data-tip="${esc(tr('fk.t.invRefresh'))}">${icon('refresh')}</button>
        ${s.kind === 'vms' ? `<span class="form-hint" data-fk="inv-selected-count">${esc(tr('fk.inv.selected', { n: s.selected.size }))}</span>
        <button type="button" class="btn btn-sm btn-primary tip needs-admin" data-fk="compose-wave" ${s.selected.size ? '' : 'disabled'}
          data-tip="${esc(s.selected.size ? tr('fk.t.composeWave') : tr('fk.t.composeNone'))}">${icon('build')} ${esc(tr('fk.composeWave'))}</button>` : ''}
      </div><div data-fk="inv-out"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`;
    const c = cur;
    // jeton de requête : changer de source ou de sorte pendant qu'une
    // réponse plus lente est en vol ne doit jamais l'écraser après coup
    const token = cur.invToken = (cur.invToken || 0) + 1;
    let res;
    try { res = await call('GET', `/api/forklift/${enc(c.cluster)}/inventory/${enc(s.source)}/${s.kind}?namespace=${enc(s.namespace)}`); }
    catch (err) {
      if (c !== cur || token !== cur.invToken) return;
      body.querySelector('[data-fk="inv-out"]').innerHTML = `<p class="res-error">${esc(err.message)}</p>`;
      return;
    }
    if (c !== cur || token !== cur.invToken) return;
    c.invRows = res.rows || [];
    paintInventory();
  }

  /** Le nombre de VMs cochées et l'état du bouton « Composer une vague »,
   *  sans redessiner tout le bandeau (la sélection change à chaque coche). */
  function syncComposeUI() {
    const count = cur.host.querySelector('[data-fk="inv-selected-count"]');
    const btn = cur.host.querySelector('[data-fk="compose-wave"]');
    if (!count || !btn) return;
    const n = (lastInv.selected && lastInv.selected.size) || 0;
    count.textContent = tr('fk.inv.selected', { n });
    btn.disabled = !n;
    btn.setAttribute('data-tip', n ? tr('fk.t.composeWave') : tr('fk.t.composeNone'));
  }

  function paintInventory() {
    const out = cur.host.querySelector('[data-fk="inv-out"]');
    if (!out) return;
    const s = lastInv;
    if (s.kind === 'vms') syncComposeUI();
    const q = s.q.toLowerCase();
    let rows = (cur.invRows || []).filter(r => !q || `${r.name} ${r.path || ''} ${r.guest || ''}`.toLowerCase().includes(q));
    // voulu : « à chaud seulement » ne filtre que sur le CBT, la colonne d'éligibilité dit en plus les outils VMware
    if (s.kind === 'vms' && s.warm) rows = rows.filter(r => r.cbt);
    cur.host.querySelector('.res-count').textContent = tr('fk.inv.count', { n: rows.length });
    if (s.kind === 'networks') {
      out.innerHTML = `<table class="data-table res-table" data-fk="inv-table"><thead><tr><th>${esc(tr('fk.inv.name'))}</th><th>${esc(tr('fk.inv.path'))}</th></tr></thead>
        <tbody>${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${esc(r.path)}</td></tr>`).join('')}</tbody></table>`;
      return;
    }
    if (s.kind === 'datastores') {
      out.innerHTML = `<table class="data-table res-table" data-fk="inv-table"><thead><tr><th>${esc(tr('fk.inv.name'))}</th><th>${esc(tr('fk.inv.capacity'))}</th><th>${esc(tr('fk.inv.free'))}</th></tr></thead>
        <tbody>${rows.map(r => `<tr><td>${esc(r.name)}</td><td>${size(r.capacity)}</td><td>${size(r.free)}</td></tr>`).join('')}</tbody></table>`;
      return;
    }
    out.innerHTML = `<table class="data-table res-table" data-fk="inv-table"><thead><tr>
        <th></th><th>${esc(tr('fk.inv.vm'))}</th><th>${esc(tr('fk.inv.power'))}</th><th>${esc(tr('fk.inv.os'))}</th>
        <th>${esc(tr('fk.inv.cpuMem'))}</th><th>${esc(tr('fk.inv.disks'))}</th><th>${esc(tr('fk.inv.warm'))}</th>
        <th>${esc(tr('fk.inv.tools'))}</th><th>${esc(tr('fk.inv.concerns'))}</th><th>${esc(tr('fk.inv.reason'))}</th></tr></thead>
      <tbody>${rows.map(r => {
        const total = (r.disks || []).reduce((a, x) => a + (x.capacity || 0), 0);
        const cs = r.concerns || [];
        const shown = cs.filter(c => !/^Disk - /.test(c.label || '')).slice(0, 2);
        const rest = cs.length - shown.length;
        const id = String(r.id || r.name);
        const blockers = vmBlockers(r);
        const checked = s.selected.has(id);
        return `<tr data-vm="${esc(r.name)}">
          <td><input type="checkbox" class="tip" data-fk="vm-select" value="${esc(id)}" ${checked ? 'checked' : ''} ${blockers.length ? 'disabled' : ''}
            data-tip="${esc(blockers.length ? tr('fk.t.eligibleNo', { reasons: blockers.join('; ') }) : tr('fk.t.select'))}"></td>
          <td class="tip" data-tip="${esc(r.path || '')}">${esc(r.name)}</td>
          <td>${esc(r.power === 'poweredOn' ? tr('fk.inv.on') : r.power === 'poweredOff' ? tr('fk.inv.off') : r.power || '')}</td>
          <td class="fk-guest">${esc(r.guest || '')}</td><td>${esc(`${r.cpus || '?'} / ${size((r.memory_mib || 0) * 1048576)}`)}</td>
          <td>${esc(`${(r.disks || []).length} · ${size(total)}`)}</td>
          <td>${r.cbt ? `<span class="tip" data-fk-warm="yes" data-tip="${esc(tr('fk.t.cbtOn'))}">${icon('ok')} ${esc(tr('fk.inv.cbtOn'))}</span>`
                      : `<span class="tip" data-fk-warm="no" data-tip="${esc(tr('fk.t.cbtOff'))}">${icon('fail')} ${esc(tr('fk.inv.cbtOff'))}</span>`}</td>
          <td>${r.tools ? `<span class="tip" data-fk-tools="yes" data-tip="${esc(tr('fk.t.toolsOn'))}">${icon('ok')} ${esc(tr('fk.inv.toolsOn'))}</span>`
                        : `<span class="tip" data-fk-tools="no" data-tip="${esc(tr('fk.t.toolsOff'))}">${icon('fail')} ${esc(tr('fk.inv.toolsOff'))}</span>`}</td>
          <td class="fk-concerns">${shown.map(c => `<span class="tip" data-tip="${esc(c.label)}">${icon(SEV[c.category] || 'info', 12)} ${esc(concernText(c))}</span>`).join(' ')}
            ${rest > 0 ? `<span class="badge tip" data-tip="${esc(cs.map(concernText).join('\n'))}">+${rest}</span>` : ''}</td>
          <td class="fk-reason" data-fk-eligible="${blockers.length ? 'no' : 'yes'}">${blockers.length
              ? `<span class="tip" data-tip="${esc(blockers.join('\n'))}">${icon('warn', 12)} ${esc(blockers.join('; '))}</span>`
              : `<span class="tip" data-tip="${esc(tr('fk.t.eligibleYes'))}">${icon('ok', 12)} ${esc(tr('fk.inv.eligibleYes'))}</span>`}</td></tr>`;
      }).join('')}</tbody></table>`;
  }

  // -- Vagues (W6) ------------------------------------------------------------
  // Clés en toutes lettres : le contrôle de parité ne lit que des littéraux.
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
    return `<span data-fk-state="${esc(w.state)}">${badge(cls, label(), w.message || '')}</span>`;
  };
  // une migration tourne : ni clôture ni suppression (l'outil refuse aussi)
  const RUNNING = ['copying', 'cutover-scheduled', 'cutting-over'];
  // même liste que ROLLBACK_STATES de l'outil, moins la vague déjà revenue
  const ROLLBACK = ['succeeded', 'failed'];

  const fmtDur = (sec) => {
    const s = Math.max(0, Math.round(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    if (h) return `${h} h ${String(m).padStart(2, '0')} min`;
    if (m) return `${m} min ${String(r).padStart(2, '0')} s`;
    return `${r} s`;
  };
  const fmtWhen = (iso) => {
    const t = Date.parse(iso || '');
    return Number.isFinite(t) ? new Date(t).toLocaleString() : '';
  };
  /** Un instant à venir, avec son compte à rebours (tenu à jour par tick). */
  const countdown = (iso) => `<span class="fk-when">${esc(fmtWhen(iso))} (<span data-fk-at="${esc(iso)}">${esc(left(iso))}</span>)</span>`;
  function left(iso) {
    const ms = Date.parse(iso || '') - Date.now();
    return ms > 0 ? tr('fk.w.in', { left: fmtDur(ms / 1000) }) : tr('fk.w.due');
  }
  let ticker = null;
  /** Les comptes à rebours visibles, une fois par seconde, tant qu'il y en a. */
  function tick() {
    const els = document.querySelectorAll('[data-fk-at]');
    els.forEach((el) => { el.textContent = left(el.dataset.fkAt); });
    if (!els.length && ticker) { clearInterval(ticker); ticker = null; }
    else if (els.length && !ticker) ticker = setInterval(tick, 1000);
  }

  /** Les deux erreurs connues de Forklift, avec leur remède (le texte de
   *  Forklift reste montré tel quel à côté). L'importeur SUSE (sans VDDK) se
   *  répare en changeant d'importeur (étape 2) ; un autre importeur (déjà
   *  upstream ou inconnu) a plutôt une image VDDK à corriger (étape 3). */
  function errorHint(msg) {
    if (/unable to connect to vddk data source|nbdkit/i.test(msg || '')) {
      const kind = cur && cur.data && cur.data.cdi_importer && cur.data.cdi_importer.kind;
      return kind === 'suse-no-vddk' ? tr('fk.w.hintVddk') : tr('fk.w.hintVddkImage');
    }
    if (/VMware Tools is not running/i.test(msg || '')) return tr('fk.w.hintTools');
    return '';
  }
  const errorHtml = (msg) => {
    if (!msg) return '';
    const hint = errorHint(msg);
    return `<div class="res-error" data-fk-error>${esc(msg)}</div>${hint ? `<div class="form-hint" data-fk-hint>${icon('info', 12)} ${esc(hint)}</div>` : ''}`;
  };

  function waveButtons(w) {
    const btn = (act, cls, ic, label, tip) => `<button type="button" class="btn btn-sm ${cls} tip${act === 'wave-follow' ? '' : ' needs-admin'}" data-fk="${act}" data-wave="${esc(w.name)}" data-tip="${esc(tip)}">${icon(ic)} ${esc(label)}</button>`;
    const out = [btn('wave-follow', 'btn-secondary', 'activity', tr('fk.w.follow'), tr('fk.t.wFollow'))];
    if (w.state === 'ready' || w.state === 'failed') out.push(btn('wave-start', 'btn-primary', 'play', tr('fk.w.start'), tr('fk.t.wStart')));
    if (w.state === 'copying' || w.state === 'cutover-scheduled') {
      out.push(btn('wave-cutover', 'btn-primary', 'switch', tr('fk.w.cutoverNow'), tr('fk.t.wCutoverNow')));
      out.push(btn('wave-schedule', 'btn-secondary', 'timer', tr('fk.w.schedule'), tr('fk.t.wSchedule')));
    }
    // un rollback ne se propose qu'une fois la bascule au moins amorcée : un
    // échec pendant la copie (avant toute bascule) ne se défait pas
    if (ROLLBACK.includes(w.state) && w.cutover_started) out.push(btn('wave-rollback', 'btn-danger', 'undo', tr('fk.w.rollback'), tr('fk.t.wRollback')));
    if (!RUNNING.includes(w.state) && w.state !== 'closed') out.push(btn('wave-close', 'btn-secondary', 'clean', tr('fk.w.close'), tr('fk.t.wClose')));
    if (w.state === 'closed') out.push(btn('wave-clean-snapshots', 'btn-secondary', 'clean', tr('fk.w.cleanSnapshotsBtn'), tr('fk.t.wCleanSnapshots')));
    if (!RUNNING.includes(w.state)) out.push(btn('wave-delete', 'btn-danger', 'trash', tr('fk.w.delete'), tr('fk.t.wDelete')));
    return out.join('');
  }

  function wavesView(d) {
    if (!d.install.ready) {
      return `<div class="sto-finding sev-action"><div class="sto-finding-title">${icon('warn')} ${esc(tr('fk.needInstall'))}</div>
        <button type="button" class="btn btn-sm btn-secondary tip" data-fk="goto-prep" data-tip="${esc(tr('fk.t.gotoPrep'))}">${icon('settings')} ${esc(tr('section.fkPrep'))}</button></div>`;
    }
    const waves = d.waves || [];
    cur.host.querySelector('.res-count').textContent = tr('fk.w.count', { n: waves.length });
    if (!waves.length) {
      return `<p class="form-hint">${esc(tr('fk.w.none'))}</p>
        <button type="button" class="btn btn-sm btn-secondary tip" data-fk="goto-inventory" data-tip="${esc(tr('fk.t.inventory'))}">${icon('general')} ${esc(tr('section.fkInventory'))}</button>`;
    }
    return `<div class="fk-sources">${waves.map(w => `<div class="fk-source" data-fk-wave="${esc(w.name)}">
        <div class="fk-step-head"><b>${esc(w.name)}</b> ${stateBadge(w)}</div>
        <div class="form-hint">${esc(tr('fk.w.target', { ns: w.target_namespace }))} · ${esc(tr('fk.w.vms', { n: (w.vms || []).length }))}</div>
        ${w.next_precopy ? `<div class="form-hint" data-fk="wave-next">${esc(tr('fk.w.nextCopy'))} ${countdown(w.next_precopy)}</div>` : ''}
        ${w.state === 'cutover-scheduled' && w.cutover ? `<div class="form-hint" data-fk="wave-cutover-at">${esc(tr('fk.w.cutoverAt'))} ${countdown(w.cutover)}</div>` : ''}
        ${w.message ? errorHtml(w.message) : ''}
        <div class="fk-source-actions">${waveButtons(w)}</div></div>`).join('')}</div>`;
  }

  // v1.80.0 : Blocs ou Couloirs, retenu par le navigateur (Blocs par défaut)
  const VIEW_KEY = 'harvester_ops_fk_waves_view';
  function wavesMode() {
    try { return localStorage.getItem(VIEW_KEY) === 'lanes' ? 'lanes' : 'blocks'; } catch { return 'blocks'; }
  }
  function viewToggle(mode) {
    const tab = (m, ic, label, tip) => `<button type="button" class="sub-tab tip${mode === m ? ' active' : ''}" role="tab"
        aria-selected="${mode === m}" data-fk="waves-mode" data-mode="${m}" data-tip="${esc(tip)}">${icon(ic)} <span>${esc(label)}</span></button>`;
    return `<div class="sub-tabs sub-tabs-inline fk-view-toggle" role="tablist" data-fk="waves-view">
        ${tab('blocks', 'general', tr('fkl.view.blocks'), tr('fkl.t.blocks'))}${tab('lanes', 'metrics', tr('fkl.view.lanes'), tr('fkl.t.lanes'))}</div>`;
  }

  /** L'onglet Vagues : bascule Blocs/Couloirs puis la vue choisie. En
   *  couloirs, seuls les couloirs se redessinent à chaque relecture (la
   *  barre d'outils et sa fenêtre de maintenance restent en place). */
  function paintWaves(body, d) {
    if (!d.install.ready || !(d.waves || []).length) { body.innerHTML = wavesView(d); return; }
    const mode = wavesMode();
    if (!body.querySelector(':scope > [data-fk="waves-view"]')) {
      body.innerHTML = `${viewToggle(mode)}<div data-fk="waves-content"></div>`;
    } else {
      body.querySelector(':scope > [data-fk="waves-view"]').outerHTML = viewToggle(mode);
    }
    const content = body.querySelector('[data-fk="waves-content"]');
    if (mode === 'blocks' || !window.ForkliftLanes) { content.innerHTML = wavesView(d); return; }
    cur.host.querySelector('.res-count').textContent = tr('fk.w.count', { n: d.waves.length });
    const cluster = cur.cluster;
    ForkliftLanes.paint(content, d.waves.map((w) => ({ cluster, wave: w })),
      { scope: cluster, showCluster: false, stateBadge, onOpen: (c, wave) => followWave(c, wave) });
  }

  /** Les gestes d'une vague, confirmés quand ils touchent aux VMs. */
  function waveAction(act, w, cluster) {
    const wave = w.name;
    if (act === 'wave-follow') return followWave(cluster, wave);
    if (act === 'wave-start') return post('wave-start', { wave }, tr('fk.done.waveStart', { wave }), null, cluster);
    if (act === 'wave-cutover') {
      if (!confirm(tr('fk.confirm.cutover', { wave }))) return;
      return post('wave-cutover', { wave }, tr('fk.done.cutover', { wave }), null, cluster);
    }
    if (act === 'wave-schedule') return scheduleForm(cluster, w);
    if (act === 'wave-rollback') {
      if (!confirm(tr('fk.confirm.rollback', { wave }))) return;
      return post('wave-rollback', { wave }, tr('fk.done.rollback', { wave }), null, cluster);
    }
    if (act === 'wave-close') return closeForm(cluster, w);
    if (act === 'wave-clean-snapshots') return post('wave-close', { wave, clean_snapshots: true }, tr('fk.done.close', { wave }), null, cluster);
    if (act === 'wave-delete') {
      if (!confirm(tr('fk.confirm.delete', { wave }))) return;
      return post('wave-delete', { wave }, tr('fk.done.waveDelete', { wave }), null, cluster);
    }
  }

  function scheduleForm(cluster, w) {
    const wave = w.name;
    // valeur proposée : la bascule déjà prévue, sinon dans une heure (heure locale)
    const at = new Date(Date.parse(w.cutover || '') || Date.now() + 3600e3);
    const local = new Date(at.getTime() - at.getTimezoneOffset() * 60e3).toISOString().slice(0, 16);
    const form = win(cluster, `fk-wave-sched-${cluster}-${wave}`, tr('fk.w.scheduleTitle', { wave }),
      `<p class="form-hint">${esc(tr('fk.w.scheduleHint'))}</p>
       ${field('at', tr('fk.f.cutoverAt'), `<input name="at" type="datetime-local" required value="${esc(local)}">`, tr('fk.t.cutoverAt'))}`,
      320, [tr('fk.w.scheduleSubmit'), tr('fk.t.wSchedule')]);
    if (form.dataset.fkBound) return;
    form.dataset.fkBound = '1';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = form.querySelector('.of-msg');
      const t = Date.parse(form.querySelector('[name="at"]').value);   // heure locale du navigateur
      if (!Number.isFinite(t)) { msg.innerHTML = `<span class="res-error">${esc(tr('fk.w.needTime'))}</span>`; return; }
      const iso = new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
      if (!confirm(tr('fk.confirm.cutoverAt', { wave, when: new Date(t).toLocaleString() }))) return;
      await post('wave-cutover', { wave, at: iso }, tr('fk.done.cutover', { wave }), msg, cluster);
    });
  }

  function closeForm(cluster, w) {
    const wave = w.name;
    const form = win(cluster, `fk-wave-close-${cluster}-${wave}`, tr('fk.w.closeTitle', { wave }),
      `<p class="form-hint">${esc(tr('fk.w.closeHint'))}</p>
       <label class="fk-check tip" data-tip="${esc(tr('fk.t.cleanSnapshots'))}"><input type="checkbox" name="clean_snapshots" checked> ${esc(tr('fk.w.cleanSnapshots'))}</label>`,
      300, [tr('fk.w.closeSubmit'), tr('fk.t.wClose')]);
    if (form.dataset.fkBound) return;
    form.dataset.fkBound = '1';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const clean = form.querySelector('[name="clean_snapshots"]').checked;
      await post('wave-close', { wave, clean_snapshots: clean }, tr('fk.done.close', { wave }), form.querySelector('.of-msg'), cluster);
    });
  }

  // -- Suivi d'une vague (fenêtre) -------------------------------------------------
  const follows = new Map();   // `${cluster}/${wave}` -> { panel, cluster, wave, timer }

  // DiskTransfer/Cutover : `completed`/`total` sont des Mio (montrés en
  // taille) ; ImageConversion/VirtualMachineCreation : l'étape suffit, le
  // pourcentage rendu par Forklift n'y veut rien dire (0/1 fini vu en réel)
  const SIZED_STEPS = new Set(['DiskTransfer', 'Cutover']);
  const STEP_ONLY_STEPS = new Set(['ImageConversion', 'VirtualMachineCreation']);
  function progressCell(v) {
    const p = v.progress || { done: 0, total: 0 };
    if (STEP_ONLY_STEPS.has(v.step_name)) return `<span data-fk="vm-pct">–</span>`;
    if (SIZED_STEPS.has(v.step_name)) {
      const done = size(p.done * 1048576), total = size(p.total * 1048576);
      return `<progress class="tip" max="${esc(p.total || 1)}" value="${esc(p.done)}" data-tip="${esc(`${done} / ${total}`)}"></progress> <span data-fk="vm-pct">${esc(done)} / ${esc(total)}</span>`;
    }
    const pct = p.total ? Math.floor(100 * p.done / p.total) : 0;
    return `<progress class="tip" max="${esc(p.total || 1)}" value="${esc(p.done)}" data-tip="${esc(`${p.done} / ${p.total}`)}"></progress> <span data-fk="vm-pct">${pct} %</span>`;
  }

  // Les noms d'étape (`step_name`) sont ceux du pipeline Forklift, en
  // anglais quel que soit la langue de la console : traduits ici plutôt que
  // montrés tels quels (bibliothèque tierce). Une étape inconnue retombe sur
  // le texte brut envoyé par le serveur.
  const STEP_NAME_I18N = {
    Initialize: 'fk.stepName.initialize', DiskTransfer: 'fk.stepName.diskTransfer',
    DiskAllocation: 'fk.stepName.diskAllocation', Cutover: 'fk.stepName.cutover',
    ImageConversion: 'fk.stepName.imageConversion', VirtualMachineCreation: 'fk.stepName.virtualMachineCreation',
  };

  // Entre deux copies incrémentales (`CopyingPaused`), le pipeline garde
  // souvent son étape courante affichée telle quelle (« final copy 0/... »),
  // ce qui se lit comme une bascule commencée alors que Forklift attend
  // simplement son prochain tour : on le dit avec le prochain moment de
  // copie quand il est connu.
  function stepText(v) {
    // v1.87.0 : copie arrêtée faute de volume (place, attachement), dite
    if (v.blocked) return `<span class="fk-blocked tip" data-tip="${esc(v.blocked)}">${icon('warn', 12)} ${esc(tr('fk.w.blocked'))}</span>`;
    if (v.step_name === 'CopyingPaused') return `${esc(tr('fk.w.copyingPaused'))}${v.next_precopy ? ` ${countdown(v.next_precopy)}` : ''}`;
    const key = STEP_NAME_I18N[v.step_name];
    return key ? esc(tr(key)) : esc(v.step || v.phase || '–');
  }

  /** Une vague close ou revenue à la source : la VM qu'on y a fait revenir
   *  ne montre plus la dernière étape Forklift (une copie ou une bascule qui
   *  n'a plus cours), mais qu'elle est repartie sur son hôte d'origine. */
  function vmStepText(w, v) {
    if ((w.state === 'closed' || w.state === 'rolled-back') && v.rolled_back) return esc(tr('fk.stepName.rolledBack'));
    return stepText(v);
  }

  function followBody(w) {
    if (!w) return `<p class="form-hint" data-fk-gone>${esc(tr('fk.w.gone'))}</p>`;
    // un rollback ne se propose qu'une fois la bascule de CETTE VM amorcée
    const canRollback = (v) => ROLLBACK.includes(w.state) && v.cutover_started && !v.rolled_back;
    const rows = (w.vms || []).map((v) => {
      const last = v.last_precopy && v.last_precopy.seconds != null ? fmtDur(v.last_precopy.seconds) : '–';
      return `<tr data-fk-vm="${esc(v.id)}">
        <td class="tip" data-tip="${esc(v.id)}">${esc(v.name || v.id)}${v.rolled_back ? ` ${badge('warn', tr('fk.w.rolledBack'))}` : ''}</td>
        <td class="tip" data-tip="${esc(v.step_name || '')}">${vmStepText(w, v)}</td>
        <td>${progressCell(v)}</td>
        <td data-fk="vm-copies">${esc(v.precopies)}</td>
        <td data-fk="vm-last">${esc(last)}</td>
        <td data-fk="vm-next">${v.next_precopy ? countdown(v.next_precopy) : '–'}</td>
        <td>${errorHtml(v.error)}${canRollback(v)
          ? `<button type="button" class="btn btn-sm btn-danger tip needs-admin" data-fk-vm-rollback="${esc(v.id)}" data-name="${esc(v.name || v.id)}" data-tip="${esc(tr('fk.t.wRollbackVm'))}">${icon('undo')} ${esc(tr('fk.w.rollbackVm'))}</button>` : ''}</td></tr>`;
    }).join('');
    return `<div class="fk-step-head">${stateBadge(w)} <span class="form-hint">${esc(tr('fk.w.target', { ns: w.target_namespace }))}</span></div>
      ${w.state === 'cutover-scheduled' && w.cutover ? `<p class="form-hint" data-fk="follow-cutover">${esc(tr('fk.w.cutoverAt'))} ${countdown(w.cutover)}</p>` : ''}
      ${w.message ? errorHtml(w.message) : ''}
      <table class="data-table res-table" data-fk="follow-table"><thead><tr>
        <th>${esc(tr('fk.w.col.vm'))}</th><th>${esc(tr('fk.w.col.step'))}</th><th>${esc(tr('fk.w.col.progress'))}</th>
        <th>${esc(tr('fk.w.col.copies'))}</th><th>${esc(tr('fk.w.col.lastCopy'))}</th><th>${esc(tr('fk.w.col.nextCopy'))}</th>
        <th>${esc(tr('fk.w.col.error'))}</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  function paintFollow(f, d) {
    if (!f.panel.el.isConnected) return;
    const w = (d.waves || []).find(x => x.name === f.wave);
    f.wave_state = w;
    // seul le contenu se redessine : la ligne d'une action suivie reste en place
    const box = f.panel.body.querySelector('[data-fk="follow-content"]');
    if (box) box.innerHTML = followBody(w);
    tick();
  }

  /** Relecture d'un onglet : les fenêtres de suivi de ce cluster en profitent. */
  function paintFollows(cluster, d) {
    follows.forEach((f) => { if (f.cluster === cluster) paintFollow(f, d); });
  }

  function followWave(cluster, wave) {
    const key = `${cluster}/${wave}`;
    const had = follows.get(key);
    const panel = FloatingPanels.open({ id: `fk-wave-follow-${cluster}-${wave}`, icon: 'general', width: 900, height: 460,
      title: `${tr('fk.w.followTitle', { wave })} · ${cluster}`,
      bodyHtml: `<div data-fk="follow-content"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        <div class="of-msg" role="status" data-fk="follow-msg"></div>`,
      onClose: () => { const f = follows.get(key); if (f) clearInterval(f.timer); follows.delete(key); } });
    if (had && had.panel.el.isConnected) return had;
    const f = { panel, cluster, wave, wave_state: null };
    follows.set(key, f);
    panel.body.addEventListener('click', (e) => {
      const b = e.target.closest('[data-fk-vm-rollback]');
      if (!b) return;
      const vm = b.dataset.name;
      if (!confirm(tr('fk.confirm.rollbackVm', { vm, wave }))) return;
      post('wave-rollback', { wave, vms: [b.dataset.fkVmRollback] }, tr('fk.done.rollbackVm', { vm }),
           panel.body.querySelector('[data-fk="follow-msg"]'), cluster);
    });
    const refresh = async () => {
      if (!panel.el.isConnected) { clearInterval(f.timer); follows.delete(key); return; }
      // l'onglet de ce cluster relit déjà : il nourrit la fenêtre (paintFollows)
      if (cur && cur.cluster === cluster && cur.kind !== 'inventory' && cur.host.isConnected
          && !cur.host.closest('[hidden]') && cur.data) return;
      const d = await getJSON(`/api/forklift/${enc(cluster)}`);
      if (d && !d.error && !d.unreachable) paintFollow(f, d);
    };
    f.timer = setInterval(refresh, REFRESH_MS);
    const known = cur && cur.cluster === cluster && cur.data && !cur.data.error && !cur.data.unreachable ? cur.data : null;
    if (known) paintFollow(f, known);
    else getJSON(`/api/forklift/${enc(cluster)}`).then((d) => { if (d && !d.error && !d.unreachable) paintFollow(f, d); });
    return f;
  }

  // -- Composer une vague (fenêtre) ------------------------------------------------
  const LINUX_RE = /linux|debian|ubuntu|centos|red ?hat|rhel|suse|sles|rocky|alma|fedora/i;
  const WINDOWS_RE = /windows/i;

  /** Les VMs cochées dans l'inventaire -> fenêtre de composition : réseaux
   *  et datastores de ces VMs, chacun vers une destination du cluster. */
  async function composeWave(vms) {
    if (!vms || !vms.length || !cur) return;
    const cluster = cur.cluster;
    const { source, namespace } = vms[0];
    const panelId = `fk-wave-new-${cluster}`;
    const form = win(cluster, panelId, tr('fk.w.composeTitle'), `<div data-fk="compose-body"><p class="form-hint">${esc(tr('common.loading'))}</p></div>`,
      680, [tr('fk.w.composeSubmit'), tr('fk.t.composeSubmit')]);
    const inv = (kind) => call('GET', `/api/forklift/${enc(cluster)}/inventory/${enc(source)}/${kind}?namespace=${enc(namespace)}`)
      .then(r => r.rows || []).catch(() => []);
    const [targets, nets, stores] = await Promise.all([getJSON(`/api/forklift/${enc(cluster)}?targets=1`), inv('networks'), inv('datastores')]);
    if (!form.isConnected) return;
    const box = form.querySelector('[data-fk="compose-body"]');
    if (!targets || targets.error || targets.unreachable) {
      box.innerHTML = `<p class="res-error">${esc((targets && targets.error) || tr('fabric.unreachable'))}</p>`;
      return;
    }
    const netName = Object.fromEntries(nets.map(n => [n.id, n.name]));
    const stoName = Object.fromEntries(stores.map(s => [s.id, s.name]));
    const netIds = [...new Set(vms.flatMap(v => v.networks || []))];
    const stoIds = [...new Set(vms.flatMap(v => (v.disks || []).map(x => x.datastore).filter(Boolean)))];
    const nads = targets.nads || [];
    const classes = targets.classes || [];
    const nsList = targets.namespaces || [];
    // présélection : un réseau de VM qui porte le nom du réseau vCenter
    const nadFor = (name) => nads.find(n => n.split('/')[1].toLowerCase() === String(name || '').toLowerCase()) || 'pod';
    const linux = vms.every(v => LINUX_RE.test(v.guest || '') && !WINDOWS_RE.test(v.guest || ''));
    const windows = vms.some(v => WINDOWS_RE.test(v.guest || ''));
    const defNs = nsList.includes('default') ? 'default' : nsList[0] || '';
    box.innerHTML = `<p class="form-hint">${esc(tr('fk.w.composeHint'))}</p>
      ${field('name', tr('fk.f.waveName'), '<input name="name" required maxlength="40" pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" placeholder="wave-1">', tr('fk.t.waveName'))}
      ${field('target_namespace', tr('fk.f.targetNs'), `<select name="target_namespace">${opts(nsList, defNs)}</select>`, tr('fk.t.targetNs'))}
      <fieldset class="fk-tls"><legend>${esc(tr('fk.w.selectedVms', { source }))}</legend>
        <ul class="fk-wave-vms" data-fk="compose-vms">${vms.map(v => `<li data-vm-id="${esc(v.id)}">${esc(v.name)} <span class="form-hint">${esc(v.guest || '')}</span></li>`).join('')}</ul></fieldset>
      <fieldset class="fk-tls"><legend>${esc(tr('fk.w.networks'))}</legend>
        <p class="form-hint">${esc(tr('fk.w.networksHint'))}</p>
        ${netIds.length ? netIds.map(id => field(`net-${id}`, netName[id] || id,
          `<select data-fk-net="${esc(id)}"><option value="pod" ${nadFor(netName[id]) === 'pod' ? 'selected' : ''}>${esc(tr('fk.w.pod'))}</option>${opts(nads, nadFor(netName[id]))}</select>`,
          tr('fk.t.netMap', { name: netName[id] || id }))).join('') : `<p class="form-hint">${esc(tr('fk.w.noNetwork'))}</p>`}
      </fieldset>
      <fieldset class="fk-tls"><legend>${esc(tr('fk.w.storages'))}</legend>
        <p class="form-hint">${esc(tr('fk.w.storagesHint'))}</p>
        ${stoIds.length ? stoIds.map(id => field(`sto-${id}`, stoName[id] || id,
          `<select data-fk-sto="${esc(id)}">${opts(classes, targets.default_class || classes[0] || '')}</select>`,
          tr('fk.t.stoMap', { name: stoName[id] || id }))).join('') : `<p class="form-hint">${esc(tr('fk.w.noStorage'))}</p>`}
      </fieldset>
      <fieldset class="fk-tls"><legend>${esc(tr('fk.w.options'))}</legend>
        <label class="fk-check tip" data-tip="${esc(tr('fk.t.rawCopy'))}"><input type="checkbox" name="skip_conversion" ${linux || windows ? 'checked' : ''}> ${esc(tr('fk.f.rawCopy'))}</label>
        <p class="form-hint">${esc(tr('fk.w.rawCopyHint'))}</p>
        ${linux ? `<p class="form-hint" data-fk="raw-linux">${icon('info', 12)} ${esc(tr('fk.w.rawCopyLinux'))}</p>` : ''}
        ${windows ? `<p class="form-hint" data-fk="raw-windows">${icon('warn', 12)} ${esc(tr('fk.w.rawCopyWindows'))}</p>
        <p class="form-hint" data-fk="raw-windows-ip">${icon('info', 12)} ${esc(tr('fk.w.rawCopyWindowsIp'))}</p>` : ''}
        <label class="fk-check tip" data-tip="${esc(tr('fk.t.compatMode'))}"><input type="checkbox" name="compat_mode" ${windows ? 'checked' : linux ? '' : 'disabled'}> ${esc(tr('fk.f.compatMode'))}</label>
        <label class="fk-check tip" data-tip="${esc(tr('fk.t.staticIps'))}"><input type="checkbox" name="preserve_static_ips"> ${esc(tr('fk.f.staticIps'))}</label>
      </fieldset>`;
    // sans classe de stockage utilisable, la vague ne pourra jamais créer de
    // volume sur ce cluster : la fenêtre le dit et empêche l'envoi
    const submitBtn = form.querySelector('button[type="submit"]');
    if (!classes.length) {
      box.insertAdjacentHTML('afterbegin', `<div class="sto-finding sev-critical" data-fk="no-class"><div class="sto-finding-title">${icon('warn')} ${esc(tr('fk.w.noClass'))}</div></div>`);
      if (submitBtn) { submitBtn.disabled = true; submitBtn.setAttribute('data-tip', tr('fk.t.noClass')); }
    } else if (submitBtn) {
      submitBtn.disabled = false;
    }
    const raw = box.querySelector('[name="skip_conversion"]');
    const compat = box.querySelector('[name="compat_mode"]');
    raw.addEventListener('change', () => { compat.disabled = !raw.checked; if (!raw.checked) compat.checked = false; });
    // la fenêtre rouverte avec d'autres VMs garde son écouteur : la source
    // est relue sur le formulaire à l'envoi, jamais celle de la première ouverture
    form.dataset.fkSource = source;
    form.dataset.fkSourceNs = namespace;
    if (form.dataset.fkBound) return;
    form.dataset.fkBound = '1';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = form.querySelector('.of-msg');
      const q = (sel) => form.querySelector(sel);
      const spec = {
        name: q('[name="name"]').value.trim(),
        target_namespace: q('[name="target_namespace"]').value,
        provider: { namespace: form.dataset.fkSourceNs, name: form.dataset.fkSource },
        vms: [...form.querySelectorAll('[data-vm-id]')].map(li => li.dataset.vmId),
        networks: [...form.querySelectorAll('[data-fk-net]')].map(s => ({ source: s.dataset.fkNet, destination: s.value })),
        storages: [...form.querySelectorAll('[data-fk-sto]')].map(s => ({ source: s.dataset.fkSto, storage_class: s.value })),
        skip_conversion: q('[name="skip_conversion"]').checked,
        compat_mode: q('[name="compat_mode"]').checked,
        preserve_static_ips: q('[name="preserve_static_ips"]').checked,
      };
      const out = await post('wave-apply', { spec }, tr('fk.done.waveApply', { wave: spec.name }), msg, cluster);
      // des clusters injoignables n'ont pas pu dire s'ils tenaient déjà ces VMs
      if (out && out.skipped && out.skipped.length) {
        msg.insertAdjacentHTML('beforeend', `<div class="form-hint">${esc(tr('fk.w.skipped', { list: out.skipped.join(', ') }))}</div>`);
      }
    });
  }

  // -- Gestes -----------------------------------------------------------------
  function onClick(e) {
    const b = e.target.closest('[data-fk]');
    if (!b || !cur) return;
    const act = b.dataset.fk;
    if (!cur.data && act !== 'refresh') {
      // clic avant l'arrivée des données : on le rejoue une fois lues
      const c = cur;
      return c.ready && c.ready.then(() => { if (c === cur && cur.data) onClick(e); }, () => {});
    }
    if (act === 'refresh') {
      // un clic explicite relit tout, saisie comprise (sauf un envoi en cours)
      cur.dirty.clear();
      return load();
    }
    if (act === 'install') return post('install', {}, tr('fk.done.install'));
    if (act === 'goto-sources') return Sections.open('forklift', 'sources');
    if (act === 'goto-prep') return Sections.open('forklift', 'prep');
    if (act === 'upload-vddk') return cur.host.querySelector('[data-fk="upload-file"]').click();
    if (act === 'push-vddk') {
      const box = b.closest('[data-fk-step="vddk"]');
      const val = (n) => { const x = box.querySelector(`[name="${n}"]`); return x ? x.value.trim() : ''; };
      const chk = (n) => { const x = box.querySelector(`[name="${n}"]`); return !!(x && x.checked); };
      const body = { archive: val('archive'), image: val('image'), plain_http: chk('plain_http') };
      if (chk('use_cluster_auth')) body.use_cluster_auth = true;
      else if (val('username')) { body.username = val('username'); body.password = box.querySelector('[name="password"]').value; }
      const c = cur;
      // poussée envoyée : le formulaire redevient celui du cluster
      return post('vddk-image', body, tr('fk.done.push', { image: body.image })).then((ok) => { if (ok && c === cur) c.dirty.delete('vddk'); });
    }
    if (act === 'cdi-upstream') {
      const box = b.closest('[data-fk-step="cdi"]');
      const img = box.querySelector('[name="cdi_image"]').value.trim();
      const body = { mode: 'upstream' };
      if (img) body.image = img;
      const c = cur;
      return post('cdi-importer', body, tr('fk.done.cdiUpstream')).then((ok) => { if (ok && c === cur) c.dirty.delete('cdi'); });
    }
    if (act === 'cdi-original') {
      const c = cur;
      return post('cdi-importer', { mode: 'original' }, tr('fk.done.cdiOriginal')).then((ok) => { if (ok && c === cur) c.dirty.delete('cdi'); });
    }
    if (act === 'precopy-save') {
      const box = b.closest('[data-fk-precopy]');
      const msg = box.querySelector('[data-fk="precopy-msg"]');
      const minutes = parseInt(box.querySelector('[name="precopy_minutes"]').value, 10);
      if (!Number.isFinite(minutes) || minutes < 5 || minutes > 1440) {
        if (msg) msg.innerHTML = `<span class="res-error">${esc(tr('fk.precopy.range'))}</span>`;
        return;
      }
      if (msg) msg.innerHTML = '';
      const c = cur;
      return post('precopy-interval', { minutes }, tr('fk.done.precopy', { minutes })).then((ok) => { if (ok && c === cur) c.dirty.delete('precopy'); });
    }
    if (act === 'new-source') return sourceForm(null);
    const name = b.dataset.name;
    const ns = b.dataset.ns;
    // deux fournisseurs peuvent porter le même nom dans des namespaces
    // différents (un fait par la CLI, hors « forklift ») : les deux comptent
    const prov = name && (cur.data.providers || []).find(p => p.name === name && p.namespace === ns);
    if (act === 'edit-source' && prov) return sourceForm(prov);
    if (act === 'del-source' && prov) {
      if (!confirm(tr('fk.confirm.del', { name }))) return;
      return post('provider-delete', { name, namespace: prov.namespace }, tr('ml.done.delete', { name }));
    }
    if (act === 'inv-source' && prov) return openInventory(name, prov.namespace);
    if (act === 'inv-kind') { lastInv.kind = b.dataset.kind; return render(); }
    if (act === 'inv-refresh') return render();
    if (act === 'compose-wave' || act === 'waves-compose') {
      const vms = selectedVms();
      if (vms.length) return composeWave(vms);
      // rien de coché : les VMs se choisissent dans l'inventaire
      return Sections.open('forklift', 'inventory');
    }
    if (act === 'goto-inventory') return Sections.open('forklift', 'inventory');
    if (act === 'waves-mode') {
      try { localStorage.setItem(VIEW_KEY, b.dataset.mode === 'lanes' ? 'lanes' : 'blocks'); } catch { /* navigation privée */ }
      return render();
    }
    const wave = b.dataset.wave && (cur.data.waves || []).find(w => w.name === b.dataset.wave);
    if (wave) return waveAction(act, wave, cur.cluster);
  }

  /** Une saisie dans un formulaire de la Préparation : VDDK (pas le choix
   *  d'un fichier à déposer, qui lance l'envoi), miroir de l'importeur CDI,
   *  intervalle des copies. */
  const DIRTY_PARTS = [['vddk', '[data-fk-step="vddk"]'], ['cdi', '[data-fk-step="cdi"]'], ['precopy', '[data-fk-precopy]']];
  function touchPrep(t) {
    if (!cur || !t.closest || t.dataset.fk === 'upload-file') return;
    for (const [k, sel] of DIRTY_PARTS) if (t.closest(sel)) cur.dirty.add(k);
  }

  function onChange(e) {
    const t = e.target;
    touchPrep(t);
    if (t.name === 'use_cluster_auth') {
      const box = t.closest('.fk-form');
      const creds = box && box.querySelector('[data-fk="reg-creds"]');
      if (creds) creds.hidden = t.checked;
    } else if (t.dataset.fk === 'upload-file' && t.files[0]) {
      upload(t.files[0]);
    } else if (t.name === 'source' && lastInv) {
      const i = t.value.indexOf('/');
      lastInv.namespace = t.value.slice(0, i);
      lastInv.source = t.value.slice(i + 1);
      lastInv.selected = new Set();   // une autre source n'a pas les mêmes identifiants de VM
      render();
    } else if (t.name === 'warm_only' && lastInv) {
      lastInv.warm = t.checked;
      paintInventory();
    } else if (t.dataset.fk === 'vm-select' && lastInv) {
      if (!lastInv.selected) lastInv.selected = new Set();
      if (t.checked) lastInv.selected.add(t.value); else lastInv.selected.delete(t.value);
      paintInventory();
    }
  }

  function onInput(e) {
    touchPrep(e.target);
    if (e.target.name === 'q' && lastInv) {
      lastInv.q = e.target.value;
      paintInventory();
    }
  }

  function upload(file) {
    const c = cur;
    // la ligne est relue à chaque fois : la Préparation a pu être redessinée
    const line = (html) => {
      const el = c.host.querySelector('[data-fk="upload-line"]');
      if (el) el.innerHTML = html;
    };
    if (!/^VMware-vix-disklib-\d+\.\d+\.\d+-\d+\.x86_64\.tar\.gz$/.test(file.name)) {
      c.uploadNote = `<span class="res-error">${esc(tr('fk.badArchive'))}</span>`;
      line(c.uploadNote);
      return;
    }
    // fin de l'envoi : la note reste dite, une saisie en cours n'est
    // jamais effacée ; seule la liste des archives se rafraîchit, et
    // seulement en cas de réussite (la nouvelle archive doit y apparaître)
    const done = (html) => {
      c.uploading = false;
      c.uploadNote = html;
      line(html);
    };
    c.uploading = true;
    c.uploadNote = '';
    line(esc(tr('fk.uploading', { pct: 0 })));
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/forklift-vddk/${enc(file.name)}`);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (e) => { line(esc(tr('fk.uploading', { pct: Math.floor(100 * e.loaded / (e.total || file.size)) }))); };
    xhr.upload.onload = () => { line(esc(tr('fk.verifying'))); };
    xhr.onload = () => {
      let d = {};
      try { d = JSON.parse(xhr.responseText); } catch { /* sans corps */ }
      if (window.Dock && Dock.poll) Dock.poll();
      if (xhr.status === 201) {
        done(`${icon('ok')} ${esc(tr('fk.uploaded', { name: file.name }))}`);
        if (c === cur) refreshArchives(file.name);
      } else {
        done(`<span class="res-error">${esc(d.error || `HTTP ${xhr.status}`)}</span>`);
      }
    };
    xhr.onerror = () => done(`<span class="res-error">${esc(tr('fk.uploadFailed'))}</span>`);
    xhr.onabort = xhr.onerror;
    xhr.send(file);
  }

  /** Fin d'un envoi réussi : seuls les choix de l'archive (et sa sélection)
   *  sont rafraîchis, jamais le reste du formulaire en cours de saisie. */
  async function refreshArchives(pick) {
    const c = cur;
    const store = await getJSON('/api/forklift-vddk');
    if (!c || c !== cur) return;
    c.store = store;
    const archives = (store && store.archives) || [];
    const box = c.host.querySelector('[data-fk-step="vddk"]');
    const sel = box && box.querySelector('[name="archive"]');
    if (!sel) return;
    const value = archives.some(a => a.name === pick) ? pick : (archives[0] && archives[0].name) || '';
    sel.innerHTML = archives.length ? opts(archives.map(a => [a.name, `${a.name} (${size(a.size)})`]), value)
                                    : `<option value="">${esc(tr('fk.noArchive'))}</option>`;
    const pushBtn = box.querySelector('[data-fk="push-vddk"]');
    if (pushBtn) pushBtn.disabled = !archives.length;
  }

  // -- Actions ------------------------------------------------------------
  /** `cluster` : celui d'une fenêtre, figé à son ouverture ; sinon celui de
   *  l'onglet. L'onglet n'est relu que s'il montre encore ce cluster. */
  async function post(action, body, doneText, into, cluster) {
    const c = cur;
    const target = cluster || (c && c.cluster);
    const msg = into || (c && c.host.querySelector('[data-fk="feedback"]'));
    const same = () => !!c && c === cur && c.cluster === target;
    try {
      const out = await call('POST', `/api/forklift/${enc(target)}/do/${action}`, body);
      follow(out.action_id, msg, doneText, () => { if (same()) setTimeout(load, 1500); });
      if (same()) setTimeout(load, 2500);
      return out;
    } catch (err) {
      if (msg) {
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
        // des clusters injoignables n'ont pas pu dire s'ils tenaient déjà ces VMs
        if (err.skipped && err.skipped.length) {
          msg.insertAdjacentHTML('beforeend', `<div class="form-hint">${esc(tr('fk.w.skipped', { list: err.skipped.join(', ') }))}</div>`);
        }
      }
      return null;
    }
  }

  // -- Fenêtres (posées ici pour U4 : sources vCenter) -------------------------
  function win(cluster, id, title, bodyHtml, height = 640, submit = null) {
    // submit : [libellé, bulle] d'un bouton qui n'enregistre pas un objet
    const [label, tip] = submit || [tr('na.save'), tr('bk.submitTip')];
    const panel = FloatingPanels.open({ id, icon: 'upload', width: 720, height, title: `${title} · ${cluster}`,
      bodyHtml: `<form class="of-form" autocomplete="off">${bodyHtml}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tip)}">${icon('ok')} ${esc(label)}</button></div>
        <div class="of-msg" role="status"></div></form>` });
    return panel.el.querySelector('.of-form');
  }
  const field = (name, label, input, tip, cls = '') => `<label class="bk-field of-field ${cls}" data-f="${esc(name)}"><span>${esc(label)}</span>${
    input.replace(/^<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip || '')}"`)}</label>`;
  const opts = (list, sel) => list.map(v => (Array.isArray(v) ? v : [v, v]))
    .map(([v, l]) => `<option value="${esc(v)}" ${String(v) === String(sel) ? 'selected' : ''}>${esc(l)}</option>`).join('');

  function submitWith(form, build, action, doneText, cluster) {
    // FloatingPanels.open rend le même formulaire tant que la fenêtre n'a
    // pas été fermée : la reprendre (édition rouverte) ne doit jamais
    // poser un second écouteur, sous peine d'un Save qui envoie deux POST.
    if (form.dataset.fkBound) return;
    form.dataset.fkBound = '1';
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const msg = form.querySelector('.of-msg');
      let spec;
      try { spec = build(form); } catch (err) { msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`; return; }
      await post(action, { spec }, doneText(spec), msg, cluster);
    });
  }

  return { start, stop, openInventory, backgroundRefresh, selectedVms, composeWave, follow: followWave, tick };
})();
window.Forklift = Forklift;
