/**
 * harvester-ops — le menu Advanced de Harvester : Settings et Support (v1.67.0)
 *
 * Settings : tous les réglages de Harvester, rangés par groupe, avec leur
 * état (modifié, appliqué par Harvester ou en erreur), une fenêtre de
 * modification typée (liste, nombre, oui/non, texte, JSON, PEM, cible de
 * sauvegarde NFS ou S3), l'avertissement des réglages qui peuvent couper un
 * accès, la remise au défaut et le test de la cible de sauvegarde. Les secrets
 * (clé privée TLS, clés S3, mots de passe de registre et de proxy) arrivent
 * masqués et ne repartent jamais : le serveur les remet depuis le cluster.
 *
 * Support : les paquets de support de Harvester (créer, suivre, télécharger,
 * supprimer) et les kubeconfigs délivrés (un compte limité au rôle choisi,
 * un jeton qui expire, un fichier téléchargé une seule fois, la révocation).
 */
const Advanced = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 13) => (window.Icons ? Icons.svg(n, { size }) : '');
  const REFRESH_MS = 10000;
  const MASK = '•••';
  const DURATIONS = ['1h', '8h', '24h', '7d', '30d', '90d'];

  let cur = null;      // { cluster, host, pane, timer, data, group, filter, modifiedOnly }

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    return d;
  }
  async function getJSON(url) {
    try {
      const r = await fetch(url);
      if (r.status === 403) return { forbidden: true };
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }
  const badge = (cls, text, tip) =>
    `<span class="badge ${cls}${tip ? ' tip' : ''}"${tip ? ` data-tip="${esc(tip)}"` : ''}>${esc(text)}</span>`;
  const field = (name, label, input, tip) => `<label class="bk-field of-field" data-f="${name}">
      <span>${esc(label)}</span>${input.replace('<input', `<input class="tip" data-tip="${esc(tip || '')}"`)
        .replace('<select', `<select class="tip" data-tip="${esc(tip || '')}"`)
        .replace('<textarea', `<textarea class="tip" data-tip="${esc(tip || '')}"`)}</label>`;
  const opts = (list, sel) => list.map(v => (Array.isArray(v) ? v : [v, v]))
    .map(([v, l, dis]) => `<option value="${esc(v)}" ${String(v) === String(sel) ? 'selected' : ''} ${dis ? 'disabled' : ''}>${esc(l)}</option>`).join('');
  const val = (form, n) => form.querySelector(`[name="${n}"]`)?.value ?? '';
  const checked = (form, n) => !!form.querySelector(`[name="${n}"]`)?.checked;
  const show = (form, n, on) => { const el = form.querySelector(`[data-f="${n}"]`); if (el) el.hidden = !on; };
  const when = (iso) => { try { return iso ? new Date(iso).toLocaleString() : '–'; } catch { return iso; } };
  const size = (n) => (n > 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : n > 1024 ? `${(n / 1024).toFixed(0)} KiB` : `${n || 0} B`);

  function follow(id, into, text, onDone) {
    if (window.Dock && Dock.poll) Dock.poll();
    if (window.VMActions && VMActions.follow) VMActions.follow(id, into, text, onDone);
    else if (into) into.textContent = tr('bk.started', { id });
  }

  // -- libellés (clés littérales : le contrôle de parité les lit) --------------
  const GROUP = () => ({ general: tr('adv.g.general'), network: tr('adv.g.network'), security: tr('adv.g.security'),
                         performance: tr('adv.g.performance'), backup: tr('adv.g.backup'), upgrade: tr('adv.g.upgrade'),
                         support: tr('adv.g.support'), ui: tr('adv.g.ui'), hidden: tr('adv.g.hidden') });
  const DANGER = () => ({ disks: tr('adv.danger.disks'), rke2: tr('adv.danger.rke2'), rancher: tr('adv.danger.rancher'),
                          tls: tr('adv.danger.tls'), ui: tr('adv.danger.ui') });
  const DESC = () => ({
    'log-level': tr('adv.d.logLevel'),
    'default-vm-termination-grace-period-seconds': tr('adv.d.grace'),
    'vm-force-reset-policy': tr('adv.d.forceReset'),
    'ntp-servers': tr('adv.d.ntp'),
    'auto-disk-provision-paths': tr('adv.d.autoDisk'),
    'http-proxy': tr('adv.d.proxy'),
    'containerd-registry': tr('adv.d.registry'),
    'cluster-registration-url': tr('adv.d.registration'),
    'rancher-cluster': tr('adv.d.rancherCluster'),
    'storage-network': tr('adv.d.storageNet'),
    'vm-migration-network': tr('adv.d.migrationNet'),
    'rwx-network': tr('adv.d.rwxNet'),
    'ssl-certificates': tr('adv.d.sslCerts'),
    'ssl-parameters': tr('adv.d.sslParams'),
    'traefik-default-tls-options': tr('adv.d.tlsOptions'),
    'additional-ca': tr('adv.d.additionalCa'),
    'cluster-pod-security-standard': tr('adv.d.pss'),
    'auto-rotate-rke2-certs': tr('adv.d.rotateCerts'),
    'kubeconfig-default-token-ttl-minutes': tr('adv.d.tokenTtl'),
    'overcommit-config': tr('adv.d.overcommit'),
    'additional-guest-memory-overhead-ratio': tr('adv.d.memOverhead'),
    'max-hotplug-ratio': tr('adv.d.hotplug'),
    'kubevirt-migration': tr('adv.d.kvMigration'),
    'instance-manager-resources': tr('adv.d.imResources'),
    'longhorn-v2-data-engine-enabled': tr('adv.d.v2Enabled'),
    'longhorn-v2-data-engine-hugepage-enabled': tr('adv.d.v2Hugepage'),
    'longhorn-v2-data-engine-memory-size': tr('adv.d.v2Memory'),
    'csi-driver-config': tr('adv.d.csiDriver'),
    'csi-online-expand-validation': tr('adv.d.csiExpand'),
    'backup-target': tr('adv.d.backupTarget'),
    'server-version': tr('adv.d.serverVersion'),
    'upgrade-checker-enabled': tr('adv.d.checkerEnabled'),
    'upgrade-checker-url': tr('adv.d.checkerUrl'),
    'release-download-url': tr('adv.d.releaseUrl'),
    'upgrade-config': tr('adv.d.upgradeConfig'),
    'support-bundle-image': tr('adv.d.sbImage'),
    'support-bundle-namespaces': tr('adv.d.sbNamespaces'),
    'support-bundle-file-name': tr('adv.d.sbFileName'),
    'support-bundle-timeout': tr('adv.d.sbTimeout'),
    'support-bundle-expiration': tr('adv.d.sbExpiration'),
    'support-bundle-node-collection-timeout': tr('adv.d.sbNodeTimeout'),
    'ui-source': tr('adv.d.uiSource'),
    'ui-index': tr('adv.d.uiIndex'),
  });

  // -- cycle de vie (appelé par Sections) -------------------------------------
  function start(cluster, host) {
    stop();
    const pane = host.dataset.adv === 'support' ? 'support' : 'settings';
    cur = { cluster, host, pane, data: null, group: '', filter: '', modifiedOnly: false };
    if (pane === 'settings') shellSettings(); else shellSupport();
    cur.timer = setInterval(() => {
      if (cur && cur.host.isConnected && !cur.host.closest('[hidden]')) load();
    }, REFRESH_MS);
    return load();
  }

  function stop() {
    if (cur && cur.timer) clearInterval(cur.timer);
    cur = null;
  }

  async function load() {
    if (!cur) return;
    const c = cur;
    const url = c.pane === 'settings' ? `/api/hv-settings/${enc(c.cluster)}` : `/api/hv-support/${enc(c.cluster)}`;
    const d = await getJSON(url);
    if (c !== cur) return;
    c.data = d;
    if (c.pane === 'settings') renderSettings(); else renderSupport();
  }

  function say(html) {
    const fb = cur && cur.host.querySelector('[data-adv="feedback"]');
    if (fb) fb.innerHTML = html;
  }

  function blocked(body, d) {
    if (!d) { body.innerHTML = `<p class="res-error">${esc(tr('fabric.unreachable'))}</p>`; return true; }
    if (d.forbidden) { body.innerHTML = `<p class="form-hint">${esc(tr('adv.adminOnly'))}</p>`; return true; }
    if (d.unreachable || d.error) {
      body.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(d.error || tr('fabric.unreachable'))}</div></div>`;
      return true;
    }
    return false;
  }

  // -- Settings ---------------------------------------------------------------
  function shellSettings() {
    const g = GROUP();
    cur.host.innerHTML = `<div class="card na-card adv-card">
        <div class="res-tools">
          <input type="search" class="res-filter tip" data-adv="filter" data-tip="${esc(tr('adv.t.filter'))}" placeholder="${esc(tr('adv.filterPh'))}">
          <span class="res-count"></span>
          <label class="res-system tip" data-tip="${esc(tr('adv.t.modifiedOnly'))}"><input type="checkbox" data-adv="modified"> <span>${esc(tr('adv.modifiedOnly'))}</span></label>
          <button type="button" class="btn btn-sm btn-secondary tip" data-adv="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="adv-groups" role="tablist">
          <button type="button" class="res-chip adv-chip is-on tip" data-adv="group" data-group="" data-tip="${esc(tr('adv.t.allGroups'))}">${esc(tr('adv.g.all'))}</button>
          ${['general', 'network', 'security', 'performance', 'backup', 'upgrade', 'support', 'ui'].map(k =>
            `<button type="button" class="res-chip adv-chip tip" data-adv="group" data-group="${k}" data-tip="${esc(tr('adv.t.group', { group: g[k] }))}">${esc(g[k])}</button>`).join('')}
        </div>
        <div class="res-feedback" data-adv="feedback"></div>
        <div data-adv="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    const card = cur.host.querySelector('.adv-card');
    card.addEventListener('click', onClick);
    card.querySelector('[data-adv="filter"]').addEventListener('input', (e) => { cur.filter = e.target.value; renderSettings(); });
    card.querySelector('[data-adv="modified"]').addEventListener('change', (e) => { cur.modifiedOnly = e.target.checked; renderSettings(); });
  }

  function shownValue(s) {
    if (!s.value) return `<span class="res-dim tip" data-tip="${esc(tr('adv.t.default'))}">${esc(s.default ? tr('adv.defaultIs', { value: short(s.default) }) : tr('adv.empty'))}</span>`;
    return `<code class="adv-val tip" data-tip="${esc(s.value)}">${esc(short(s.value))}</code>`;
  }
  const short = (v) => (String(v).length > 90 ? `${String(v).slice(0, 88)}…` : String(v));

  function stateOf(s) {
    const out = [];
    if (s.modified) out.push(badge('info', tr('adv.modified'), tr('adv.t.modified')));
    if (s.applied === 'ok') out.push(badge('ok', tr('adv.applied'), tr('adv.t.applied')));
    else if (s.applied === 'pending') out.push(badge('info', tr('na.pending'), tr('adv.t.pending')));
    else if (s.applied === 'error') out.push(badge('fail', tr('na.failed'), s.message));
    // 2.10.1 : sans effet selon le contrôleur d'ingress lu sur le cluster
    if (s.inert) out.push(badge('warn', tr('adv.inert'), s.name === 'traefik-default-tls-options' ? tr('adv.t.inertNginx') : tr('adv.t.inert')));
    if (s.danger) out.push(badge('warn', tr('adv.careful'), DANGER()[s.danger]));
    return out.join(' ');
  }

  function actions(s) {
    // v1.69.0 : « Upgrade » sur server-version, comme dans Harvester
    if (s.name === 'server-version') {
      return `<button type="button" class="btn btn-sm btn-secondary tip" data-adv="upgrade" data-tip="${esc(tr('upg.t.open'))}">${icon('upload')} ${esc(tr('upg.open'))}</button>`;
    }
    if (s.readonly) return `<span class="res-dim tip" data-tip="${esc(tr('adv.t.readonly'))}">${esc(tr('adv.readonly'))}</span>`;
    if (s.elsewhere) {
      return `<button type="button" class="btn btn-sm btn-secondary tip" data-adv="elsewhere" data-tip="${esc(tr('adv.t.elsewhere'))}">${icon('network')} ${esc(tr('adv.openNetwork'))}</button>`;
    }
    const canReset = !!s.value;
    return `<button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-adv="edit" data-tip="${esc(tr('adv.t.edit'))}">${icon('edit')}</button>
      <button type="button" class="btn btn-sm btn-secondary tip needs-admin" data-adv="reset" ${canReset ? '' : 'disabled'}
        data-tip="${esc(canReset ? tr('adv.t.reset') : tr('adv.t.isDefault'))}">${icon('undo')}</button>
      ${s.name === 'backup-target' ? `<button type="button" class="btn btn-sm btn-secondary tip" data-adv="test" ${s.value ? '' : 'disabled'}
        data-tip="${esc(tr('adv.t.test'))}">${icon('test')} ${esc(tr('adv.test'))}</button>` : ''}`;
  }

  function renderSettings() {
    if (!cur || cur.pane !== 'settings' || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-adv="body"]');
    const d = cur.data;
    if (blocked(body, d)) return;
    const desc = DESC();
    const g = GROUP();
    const q = cur.filter.trim().toLowerCase();
    const items = (d.items || []).filter(s => (!cur.group || s.group === cur.group) && (!cur.modifiedOnly || s.modified)
      && (!q || `${s.name} ${s.value} ${desc[s.name] || ''}`.toLowerCase().includes(q)));
    cur.host.querySelector('.res-count').textContent = tr('adv.count', { n: items.length });
    cur.host.querySelectorAll('.adv-chip').forEach(b => b.classList.toggle('is-on', b.dataset.group === cur.group));
    if (!items.length) { body.innerHTML = `<p class="form-hint">${esc(tr('adv.none'))}</p>`; return; }
    body.innerHTML = `<table class="data-table adv-table"><thead><tr><th>${esc(tr('adv.col.setting'))}</th><th>${esc(tr('adv.col.value'))}</th>
        <th>${esc(tr('adv.col.state'))}</th><th></th></tr></thead><tbody>${items.map(s => `<tr data-setting="${esc(s.name)}">
          <td><strong>${esc(s.name)}</strong> <span class="res-dim">${esc(g[s.group] || s.group)}</span>
            ${desc[s.name] ? `<div class="res-desc">${esc(desc[s.name])}</div>` : ''}</td>
          <td>${shownValue(s)}</td><td>${stateOf(s)}</td><td class="tpl-acts">${actions(s)}</td></tr>`).join('')}</tbody></table>`;
  }

  async function onClick(e) {
    const b = e.target.closest('[data-adv]');
    if (!b || !cur) return;
    const act = b.dataset.adv;
    const c = cur.cluster;
    const again = () => setTimeout(load, 1500);
    if (act === 'refresh') return load();
    if (act === 'group') { cur.group = b.dataset.group; return renderSettings(); }
    if (act === 'upgrade' && window.Upgrade) return Upgrade.open(c);
    const s = ((cur.data && cur.data.items) || []).find(x => x.name === b.closest('[data-setting]')?.dataset.setting);
    if (act === 'elsewhere' && window.Sections) return Sections.open('network', 'clusternets');
    if (act === 'edit' && s) return s.kind === 'backup' ? openBackupTarget(c, s, again) : openSetting(c, s, again);
    if (act === 'reset' && s) {
      const warn = s.name === 'cluster-registration-url' ? tr('adv.confirm.resetRegistration')
        : s.name === 'backup-target' ? tr('adv.confirm.resetBackup') : tr('adv.confirm.reset', { name: s.name });
      if (!confirm(warn)) return;
      return run(c, 'hv-settings', 'reset', { name: s.name }, tr('adv.done.reset', { name: s.name }), again);
    }
    if (act === 'test') return run(c, 'hv-settings', 'test', {}, tr('adv.done.test'), null);
    // Support
    const bundle = ((cur.data && cur.data.bundles) || []).find(x => x.name === b.closest('[data-bundle]')?.dataset.bundle);
    const kc = ((cur.data && cur.data.kubeconfigs) || []).find(x => x.name === b.closest('[data-kc]')?.dataset.kc);
    if (act === 'new-bundle') return openBundle(c, again);
    if (act === 'delete-bundle' && bundle) {
      if (!confirm(tr('adv.confirm.deleteBundle', { name: bundle.name }))) return;
      return run(c, 'hv-support', 'bundle-delete', { name: bundle.name }, tr('adv.done.deleteBundle', { name: bundle.name }), again);
    }
    if (act === 'new-kc') return openKubeconfig(c, again);
    if (act === 'revoke-kc' && kc) {
      if (!confirm(tr('adv.confirm.revoke', { name: kc.name }))) return;
      return run(c, 'hv-support', 'kc-revoke', { name: kc.name }, tr('adv.done.revoke', { name: kc.name }), again);
    }
  }

  async function run(cluster, base, action, body, doneText, onDone) {
    try {
      const out = await call('POST', `/api/${base}/${enc(cluster)}/do/${action}`, body);
      const into = cur && cur.host.querySelector('[data-adv="feedback"]');
      follow(out.action_id, into, doneText, (ok) => { if (ok && onDone) onDone(); });
    } catch (err) {
      say(`<span class="res-error">${esc(err.message)}</span>`);
    }
  }

  // -- fenêtres ---------------------------------------------------------------
  /** Une fenêtre à formulaire dans la grammaire d'ObjectForms ; `request`
   *  rend la demande, `danger` exige une case « je comprends » cochée. */
  function formWindow({ id, icon: ic, title, hint, width = 620, height = 560, fields, submit, onReady, request,
                        post, doneText, cluster, onDone, danger = '', extra = '', afterDone }) {
    const panel = FloatingPanels.open({
      id, icon: ic, width, height, title: `${title} · ${cluster}`,
      bodyHtml: `<form class="of-form" autocomplete="off"><p class="form-hint">${esc(hint)}</p>
        ${danger ? `<div class="sto-finding sev-action adv-danger"><div class="sto-finding-title">${icon('warn')} ${esc(danger)}</div>
          <label class="bk-check tip" data-tip="${esc(tr('adv.t.understand'))}"><input type="checkbox" name="understand"> <span>${esc(tr('adv.understand'))}</span></label></div>` : ''}
        <div class="of-fields">${fields}</div>${extra}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('bk.submitTip'))}">${icon('ok')} ${esc(submit)}</button></div>
        <div class="of-msg" role="status"></div></form>`,
    });
    const root = panel.el;
    const form = root.querySelector('.of-form');
    if (onReady) onReady(form, root);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      const msg = root.querySelector('.of-msg');
      if (danger && !checked(form, 'understand')) {
        msg.innerHTML = `<span class="res-error">${esc(tr('adv.needUnderstand'))}</span>`;
        return;
      }
      let body;
      try { body = request(form); } catch (err) { msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`; return; }
      btn.disabled = true;
      try {
        const out = await post(body);
        follow(out.action_id, msg, doneText(body), (ok) => {
          btn.disabled = false;
          if (ok && afterDone) afterDone(out, msg);
          if (ok && onDone) onDone();
        });
      } catch (err) {
        btn.disabled = false;
        msg.innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    return root;
  }

  const postSetting = (cluster) => (body) => call('POST', `/api/hv-settings/${enc(cluster)}/do/set`, body);

  function pretty(v) {
    try { return JSON.stringify(JSON.parse(v), null, 2); } catch { return v; }
  }

  function openSetting(cluster, s, onDone) {
    const start = s.value || s.default || '';
    const tip = DESC()[s.name] || '';
    let input;
    if (s.kind === 'enum') input = `<select name="value">${opts(s.choices, start)}</select>`;
    else if (s.kind === 'bool') input = `<select name="value">${opts([['true', tr('adv.yes')], ['false', tr('adv.no')]], start || 'false')}</select>`;
    else if (s.kind === 'number') {
      input = `<input name="value" type="number" step="1" value="${esc(start)}" ${s.min != null ? `min="${s.min}"` : ''} ${s.max != null ? `max="${s.max}"` : ''}>`;
    } else if (s.kind === 'json' || s.kind === 'pem') {
      input = `<textarea name="value" rows="14" class="adv-code" spellcheck="false">${esc(s.kind === 'json' ? pretty(start) : start)}</textarea>`;
    } else input = `<input name="value" value="${esc(start)}">`;
    const masked = String(s.value || '').includes(MASK);
    formWindow({
      id: `adv-set-${cluster}-${s.name}`, icon: 'settings', cluster, onDone, height: s.kind === 'json' || s.kind === 'pem' ? 620 : 420,
      title: tr('adv.editTitle', { name: s.name }), hint: tip,
      danger: s.danger ? DANGER()[s.danger] : '',
      fields: field('value', tr('adv.col.value'), input, tr('adv.t.value'))
        + (s.default ? `<p class="form-hint">${esc(tr('adv.defaultIs', { value: short(s.default) }))}</p>` : '')
        + (masked ? `<p class="form-hint">${icon('lock')} ${esc(tr('adv.maskedHint'))}</p>` : ''),
      submit: tr('na.save'),
      request: (f) => {
        let v = val(f, 'value');
        if (s.kind === 'json' && v.trim()) {
          try { v = JSON.stringify(JSON.parse(v)); } catch { throw new Error(tr('adv.badJson')); }
        }
        if (!String(v).trim()) throw new Error(tr('adv.emptyIsReset'));
        return { name: s.name, value: v };
      },
      post: postSetting(cluster),
      doneText: () => tr('adv.done.set', { name: s.name }),
    });
  }

  function openBackupTarget(cluster, s, onDone) {
    let v = {};
    try { v = JSON.parse(s.value || '{}') || {}; } catch { v = {}; }
    const type = v.type || 'nfs';
    const endpoint = String(v.endpoint || '').replace(/^nfs:\/\//, '');
    formWindow({
      id: `adv-bt-${cluster}`, icon: 'snapshot', cluster, onDone, height: 640,
      title: tr('adv.bt.title'), hint: tr('adv.d.backupTarget'),
      fields: field('type', tr('adv.bt.type'), `<select name="type">${opts([['nfs', 'NFS'], ['s3', 'S3']], type)}</select>`, tr('adv.bt.t.type'))
        + field('endpoint', tr('adv.bt.endpoint'), `<input name="endpoint" value="${esc(type === 'nfs' ? endpoint : v.endpoint || '')}"
            placeholder="172.16.0.5:/volume1/BACKUP/harvester/">`, tr('adv.bt.t.endpoint'))
        + field('bucketName', tr('adv.bt.bucket'), `<input name="bucketName" value="${esc(v.bucketName || '')}">`, tr('adv.bt.t.bucket'))
        + field('bucketRegion', tr('adv.bt.region'), `<input name="bucketRegion" value="${esc(v.bucketRegion || '')}" placeholder="us-east-1">`, tr('adv.bt.t.region'))
        + field('accessKeyId', tr('adv.bt.keyId'), '<input name="accessKeyId" autocomplete="off">', tr('adv.bt.t.keys'))
        + field('secretAccessKey', tr('adv.bt.secret'), '<input name="secretAccessKey" type="password" autocomplete="new-password">', tr('adv.bt.t.keys'))
        + `<label class="bk-check tip" data-f="virtualHostedStyle" data-tip="${esc(tr('adv.bt.t.vhost'))}"><input type="checkbox" name="virtualHostedStyle" ${v.virtualHostedStyle ? 'checked' : ''}> <span>${esc(tr('adv.bt.vhost'))}</span></label>`
        + field('refresh', tr('adv.bt.refresh'), `<input name="refresh" type="number" min="0" value="${esc(v.refreshIntervalInSeconds || 0)}">`, tr('adv.bt.t.refresh')),
      extra: `<p class="form-hint">${esc(tr('adv.bt.checked'))}</p>`,
      submit: tr('na.save'),
      onReady: (form) => {
        const sync = () => {
          const s3 = val(form, 'type') === 's3';
          ['bucketName', 'bucketRegion', 'accessKeyId', 'secretAccessKey', 'virtualHostedStyle'].forEach(n => show(form, n, s3));
          form.querySelector('[name="endpoint"]').placeholder = s3 ? 'https://s3.example.lan:9000' : '172.16.0.5:/volume1/BACKUP/harvester/';
        };
        form.addEventListener('change', sync);
        sync();
      },
      request: (f) => {
        const t = val(f, 'type');
        const spec = { type: t, endpoint: val(f, 'endpoint').trim(), refreshIntervalInSeconds: Number(val(f, 'refresh') || 0) };
        if (t === 'nfs') {
          if (!spec.endpoint) throw new Error(tr('adv.bt.needEndpoint'));
          spec.endpoint = /^nfs:\/\//.test(spec.endpoint) ? spec.endpoint : `nfs://${spec.endpoint}`;
        } else {
          Object.assign(spec, { bucketName: val(f, 'bucketName').trim(), bucketRegion: val(f, 'bucketRegion').trim(),
                                accessKeyId: val(f, 'accessKeyId').trim(), secretAccessKey: val(f, 'secretAccessKey'),
                                virtualHostedStyle: checked(f, 'virtualHostedStyle') });
          if (!spec.accessKeyId || !spec.secretAccessKey) throw new Error(tr('adv.bt.needKeys'));
          if (!spec.bucketName || !spec.bucketRegion) throw new Error(tr('adv.bt.needBucket'));
        }
        return { name: 'backup-target', value: JSON.stringify(spec) };
      },
      post: postSetting(cluster),
      doneText: () => tr('adv.done.set', { name: 'backup-target' }),
    });
  }

  // -- Support ----------------------------------------------------------------
  function shellSupport() {
    cur.host.innerHTML = `<div class="card na-card adv-card">
        <div class="res-tools">
          <span class="res-count"></span>
          <button type="button" class="btn btn-sm btn-secondary tip" data-adv="refresh" data-tip="${esc(tr('res.refreshTip'))}">${icon('refresh')} ${esc(tr('overview.refresh'))}</button>
        </div>
        <div class="res-feedback" data-adv="feedback"></div>
        <div data-adv="body"><p class="form-hint">${esc(tr('common.loading'))}</p></div></div>`;
    cur.host.querySelector('.adv-card').addEventListener('click', onClick);
  }

  function bundleState(r) {
    if (r.state === 'ready') return badge('ok', tr('adv.sb.ready'));
    if (r.state === 'error') return badge('fail', tr('na.failed'));
    return `${badge('info', tr('adv.sb.collecting'))} <progress class="adv-progress" max="100" value="${Number(r.progress) || 0}"></progress> ${esc(r.progress || 0)} %`;
  }

  function renderSupport() {
    if (!cur || cur.pane !== 'support' || !cur.host.isConnected) return;
    const body = cur.host.querySelector('[data-adv="body"]');
    const d = cur.data;
    if (blocked(body, d)) return;
    const c = cur.cluster;
    cur.host.querySelector('.res-count').textContent = '';
    const bundles = d.bundles.length ? `<table class="data-table"><thead><tr><th>${esc(tr('bk.f.name'))}</th><th>${esc(tr('of.f.description'))}</th>
        <th>${esc(tr('adv.col.state'))}</th><th>${esc(tr('adv.sb.file'))}</th><th>${esc(tr('adv.col.created'))}</th><th></th></tr></thead><tbody>
        ${d.bundles.map(r => `<tr data-bundle="${esc(r.name)}"><td><strong>${esc(r.name)}</strong></td><td>${esc(r.description)}</td>
          <td>${bundleState(r)}</td><td>${r.filename ? `<code>${esc(r.filename)}</code> <span class="res-dim">${esc(size(r.filesize))}</span>` : '–'}</td>
          <td>${esc(when(r.created))}</td>
          <td class="tpl-acts">${r.state === 'ready' ? `<a class="btn btn-sm btn-secondary tip" data-adv="download-bundle" download
              href="/api/hv-support/${enc(c)}/bundle/${enc(r.name)}/download" data-tip="${esc(tr('adv.sb.t.download'))}">${icon('download')}</a>` : ''}
            <button type="button" class="btn btn-sm btn-danger tip needs-admin" data-adv="delete-bundle" data-tip="${esc(tr('adv.sb.t.delete'))}">${icon('trash')}</button></td></tr>`).join('')}
        </tbody></table>` : `<p class="form-hint na-pad">${esc(tr('adv.sb.none'))}</p>`;
    const kcs = d.kubeconfigs.length ? `<table class="data-table"><thead><tr><th>${esc(tr('bk.f.name'))}</th><th>${esc(tr('adv.kc.role'))}</th>
        <th>${esc(tr('adv.kc.scope'))}</th><th>${esc(tr('adv.kc.expires'))}</th><th>${esc(tr('of.f.description'))}</th><th></th></tr></thead><tbody>
        ${d.kubeconfigs.map(r => `<tr data-kc="${esc(r.name)}"><td><strong>${esc(r.name)}</strong></td><td><code>${esc(r.role)}</code></td>
          <td>${esc(r.scope === '*' ? tr('adv.kc.wholeCluster') : r.scope)}</td>
          <td>${esc(when(r.expires))} ${r.expired ? badge('warn', tr('adv.kc.expired'), tr('adv.kc.t.expired')) : ''}</td>
          <td>${esc(r.description)}</td>
          <td class="tpl-acts"><button type="button" class="btn btn-sm btn-danger tip needs-admin" data-adv="revoke-kc" data-tip="${esc(tr('adv.kc.t.revoke'))}">${icon('trash')} ${esc(tr('adv.kc.revoke'))}</button></td></tr>`).join('')}
        </tbody></table>` : `<p class="form-hint na-pad">${esc(tr('adv.kc.none'))}</p>`;
    body.innerHTML = `<div class="card tpl-card">
        <div class="tpl-head">${icon('bundle')} <span class="tpl-title"><strong>${esc(tr('adv.sb.title'))}</strong>
          <span class="res-desc">${esc(tr('adv.sb.hint'))}</span></span>
          <span class="tpl-acts"><button type="button" class="btn btn-sm btn-primary tip needs-admin" data-adv="new-bundle" data-tip="${esc(tr('adv.sb.t.new'))}">${icon('add')} ${esc(tr('adv.sb.new'))}</button></span></div>
        ${bundles}</div>
      <div class="card tpl-card">
        <div class="tpl-head">${icon('key')} <span class="tpl-title"><strong>${esc(tr('adv.kc.title'))}</strong>
          <span class="res-desc">${esc(tr('adv.kc.hint'))}</span></span>
          <span class="tpl-acts"><button type="button" class="btn btn-sm btn-primary tip needs-admin" data-adv="new-kc" data-tip="${esc(tr('adv.kc.t.new'))}">${icon('add')} ${esc(tr('adv.kc.new'))}</button></span></div>
        ${kcs}</div>`;
  }

  function openBundle(cluster, onDone) {
    const nss = (cur && cur.data && cur.data.namespaces) || [];
    formWindow({
      id: `adv-sb-${cluster}`, icon: 'bundle', cluster, onDone, height: 600,
      title: tr('adv.sb.new'), hint: tr('adv.sb.formHint'),
      fields: field('description', tr('of.f.description'), '<textarea name="description" rows="3" required></textarea>', tr('adv.sb.t.description'))
        + field('issue_url', tr('adv.sb.issue'), '<input name="issue_url" type="url" placeholder="https://">', tr('adv.sb.t.issue'))
        + `<div class="bk-field of-field" data-f="namespaces"><span>${esc(tr('adv.sb.namespaces'))}</span>
            <div class="adv-nss tip" data-tip="${esc(tr('adv.sb.t.namespaces'))}">${nss.length ? nss.map(n =>
              `<label class="bk-check"><input type="checkbox" name="ns" value="${esc(n)}"> <code>${esc(n)}</code></label>`).join('')
              : `<span class="res-dim">${esc(tr('adv.sb.noNs'))}</span>`}</div></div>`
        + field('timeout', tr('adv.sb.timeout'), '<input name="timeout" type="number" min="0" placeholder="50">', tr('adv.sb.t.timeout'))
        + field('expiration', tr('adv.sb.expiration'), '<input name="expiration" type="number" min="0" placeholder="30">', tr('adv.sb.t.expiration'))
        + field('node_timeout', tr('adv.sb.nodeTimeout'), '<input name="node_timeout" type="number" min="0" placeholder="30">', tr('adv.sb.t.nodeTimeout')),
      submit: tr('of.create'),
      request: (f) => {
        const description = val(f, 'description').trim();
        if (!description) throw new Error(tr('adv.sb.needDescription'));
        return { spec: { description, issue_url: val(f, 'issue_url').trim(),
                         namespaces: [...f.querySelectorAll('[name="ns"]:checked')].map(x => x.value),
                         timeout: val(f, 'timeout'), expiration: val(f, 'expiration'), node_timeout: val(f, 'node_timeout') } };
      },
      post: (body) => call('POST', `/api/hv-support/${enc(cluster)}/do/bundle-create`, body),
      doneText: () => tr('adv.sb.done'),
      afterDone: (out, msg) => {
        if (out.name) {
          msg.insertAdjacentHTML('beforeend', ` <a class="btn btn-sm btn-primary" download
            href="/api/hv-support/${enc(cluster)}/bundle/${enc(out.name)}/download">${icon('download')} ${esc(tr('adv.sb.download'))}</a>`);
        }
      },
    });
  }

  function openKubeconfig(cluster, onDone) {
    const d = (cur && cur.data) || {};
    const roles = (d.roles || []).filter(r => r.exists);
    const reads = new Set(roles.filter(r => r.reads_secrets).map(r => r.name));
    formWindow({
      id: `adv-kc-${cluster}`, icon: 'key', cluster, onDone, height: 600,
      title: tr('adv.kc.new'), hint: tr('adv.kc.formHint'),
      fields: field('name', tr('bk.f.name'), '<input name="name" required maxlength="40" pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?">', tr('adv.kc.t.name'))
        + field('description', tr('of.f.description'), '<input name="description" maxlength="500">', tr('of.t.description'))
        + field('role', tr('adv.kc.role'), `<select name="role">${opts(roles.map(r => r.name), 'view')}</select>`, tr('adv.kc.t.role'))
        + field('namespace', tr('adv.kc.scope'), `<select name="namespace">${opts([['', tr('adv.kc.wholeCluster')], ...(d.namespaces || [])], '')}</select>`, tr('adv.kc.t.scope'))
        + field('duration', tr('adv.kc.duration'), `<select name="duration">${opts(DURATIONS, '24h')}</select>`, tr('adv.kc.t.duration')),
      extra: `<div class="sto-finding sev-action adv-secrets" hidden><div class="sto-finding-title">${icon('warn')} ${esc(tr('adv.kc.readsSecrets'))}</div></div>
        <p class="form-hint">${icon('lock')} ${esc(tr('adv.kc.once'))}</p>`,
      submit: tr('adv.kc.create'),
      onReady: (form, root) => {
        const sync = () => {
          root.querySelector('.adv-secrets').hidden = !reads.has(val(form, 'role'));
          const admin = val(form, 'role') === 'cluster-admin';
          const ns = form.querySelector('[name="namespace"]');
          if (admin) ns.value = '';
          ns.disabled = admin;
        };
        form.addEventListener('change', sync);
        sync();
      },
      request: (f) => {
        const name = val(f, 'name').trim();
        if (!/^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/.test(name)) throw new Error(tr('adv.kc.badName'));
        return { name, description: val(f, 'description').trim(), role: val(f, 'role'),
                 namespace: val(f, 'namespace'), duration: val(f, 'duration') };
      },
      post: (body) => call('POST', `/api/hv-support/${enc(cluster)}/do/kc-create`, body),
      doneText: (b) => tr('adv.kc.done', { name: b.name }),
      afterDone: async (out, msg) => {
        if (!out.download) return;
        try {
          const r = await fetch(`/api/hv-support/${enc(cluster)}/kubeconfig/${enc(out.download)}`);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const blob = await r.blob();
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = `${cluster}-kubeconfig.yaml`;
          document.body.appendChild(a);
          a.click();
          setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
          msg.insertAdjacentHTML('beforeend', ` <span class="res-dim">${esc(tr('adv.kc.downloaded'))}</span>`);
        } catch (err) {
          msg.insertAdjacentHTML('beforeend', ` <span class="res-error">${esc(err.message)}</span>`);
        }
      },
    });
  }

  return { start, stop };
})();
window.Advanced = Advanced;
