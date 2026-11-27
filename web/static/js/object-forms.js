/**
 * harvester-ops — créer, modifier, supprimer les objets des sections (v1.59.0)
 *
 * Ce que l'interface de Harvester permet dans chaque menu, en fenêtres qu'on
 * peut garder ouvertes ou réduire : une image par URL, une classe de stockage
 * (et la classe par défaut), une clé SSH, un secret, un réseau de VMs, un
 * volume (créer, agrandir), la configuration d'un add-on, et la suppression
 * de ce que rien n'utilise. Chaque geste est une action suivie dans le dock.
 */
const ObjectForms = (() => {
  const tr = (k, p) => (window.i18n ? i18n.t(k, p) : k);
  const enc = encodeURIComponent;
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const icon = (n, size = 14) => (window.Icons ? Icons.svg(n, { size }) : '');

  // Clés en toutes lettres : le contrôle de parité ne lit que des littéraux.
  const TITLE = {
    image: () => tr('of.new.image'), storageclass: () => tr('of.new.storageclass'),
    sshkey: () => tr('of.new.sshkey'), secret: () => tr('of.new.secret'),
    network: () => tr('of.new.network'), volume: () => tr('of.new.volume'),
  };
  const HINT = {
    image: () => tr('of.hint.image'), storageclass: () => tr('of.hint.storageclass'),
    sshkey: () => tr('of.hint.sshkey'), secret: () => tr('of.hint.secret'),
    network: () => tr('of.hint.network'), volume: () => tr('of.hint.volume'),
  };
  const ICON = { image: 'cdrom', storageclass: 'disk', sshkey: 'key', secret: 'lock', network: 'network', volume: 'volume' };
  const DONE = {
    image: (n) => tr('of.done.image', { name: n }), storageclass: (n) => tr('of.done.storageclass', { name: n }),
    sshkey: (n) => tr('of.done.sshkey', { name: n }), secret: (n) => tr('of.done.secret', { name: n }),
    network: (n) => tr('of.done.network', { name: n }), volume: (n) => tr('of.done.volume', { name: n }),
  };

  async function call(method, url, body) {
    const r = await fetch(url, { method, headers: { 'Content-Type': 'application/json' },
                                 body: body ? JSON.stringify(body) : undefined });
    let d = {};
    try { d = await r.json(); } catch { /* sans corps */ }
    if (!r.ok) throw new Error(d.hint || d.error || `HTTP ${r.status}`);
    return d;
  }

  const getJSON = (url) => fetch(url).then(r => (r.ok ? r.json() : null)).catch(() => null);

  async function namespaces(cluster) {
    const d = await getJSON(`/api/namespaces/${enc(cluster)}`);
    const names = (Array.isArray(d) ? d : []).map(n => (typeof n === 'string' ? n : n.name)).filter(Boolean);
    return names.length ? names : ['default'];
  }

  const field = (name, label, input, tip) => `<label class="bk-field of-field" data-f="${name}">
      <span>${esc(label)}</span>${input.replace('<input', `<input class="tip" data-tip="${esc(tip || '')}"`)
        .replace('<select', `<select class="tip" data-tip="${esc(tip || '')}"`)
        .replace('<textarea', `<textarea class="tip" data-tip="${esc(tip || '')}"`)}</label>`;
  const opts = (list, sel) => list.map(v => (Array.isArray(v) ? v : [v, v]))
    .map(([v, l]) => `<option value="${esc(v)}" ${v === sel ? 'selected' : ''}>${esc(l)}</option>`).join('');

  // -- les champs de chaque formulaire -----------------------------------------------
  async function fields(kind, cluster, ctx) {
    const nsList = ['image', 'sshkey', 'secret', 'network', 'volume'].includes(kind) ? await namespaces(cluster) : [];
    const ns = field('namespace', tr('vms.namespace'),
      `<select name="namespace">${opts(nsList, ctx.namespace && nsList.includes(ctx.namespace) ? ctx.namespace : (nsList.includes('default') ? 'default' : nsList[0]))}</select>`,
      tr('of.t.namespace'));
    const name = (tip) => field('name', tr('bk.f.name'),
      `<input name="name" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" maxlength="63" autocomplete="off">`, tip || tr('bk.f.scheduleNameTip'));
    if (kind === 'image') {
      const sc = await getJSON(`/api/cluster-objects/${enc(cluster)}/storageclasses`);
      const classes = ((sc && sc.items) || []).filter(c => !c.image);
      const def = (classes.find(c => c.is_default) || {}).name;
      return ns + field('url', tr('of.f.url'), `<input name="url" type="url" required placeholder="https://…/image.qcow2">`, tr('of.t.url'))
        + field('display_name', tr('of.f.displayName'), `<input name="display_name" placeholder="${esc(tr('of.f.displayNamePh'))}">`, tr('of.t.displayName'))
        + field('storage_class', tr('res.col.class'), `<select name="storage_class">${opts(classes.map(c => [c.name, c.is_default ? `${c.name} (${tr('res.sc.default')})` : c.name]), def)}</select>`, tr('of.t.imageClass'))
        + field('checksum', tr('sta.f.checksum'), `<input name="checksum" maxlength="128" pattern="[0-9a-fA-F]{128}">`, tr('sta.tip.checksum'))
        + field('description', tr('of.f.description'), `<input name="description">`, tr('of.t.description'));
    }
    if (kind === 'storageclass') {
      // v1.64.0 : le formulaire de Harvester : moteur (Longhorn v1, v2, LVM),
      // chiffrement, topologies, description
      const o = await getJSON(`/api/storage-options/${enc(cluster)}`) || {};
      const nodes = [...new Set((o.lvm_groups || []).map(g => g.node))];
      return name(tr('of.t.scName'))
        + field('engine', tr('sc.f.engine'), `<select name="engine">${[['longhorn-v1', 'Longhorn v1'],
            ['longhorn-v2', 'Longhorn v2' + (o.longhorn_v2 ? '' : ` (${tr('sc.off')})`)],
            ['lvm', 'LVM' + (o.lvm ? '' : ` (${tr('sc.off')})`)]].map(([v, l]) =>
            `<option value="${v}" ${(v === 'longhorn-v2' && !o.longhorn_v2) || (v === 'lvm' && !o.lvm) ? 'disabled' : ''}>${esc(l)}</option>`).join('')}</select>`, tr('sc.t.engine'))
        + field('replicas', tr('res.col.replicas'), `<select name="replicas">${opts(['1', '2', '3'], '3')}</select>`, tr('of.t.replicas'))
        + field('stale_timeout', tr('of.f.stale'), `<input name="stale_timeout" type="number" min="1" max="10080" value="30">`, tr('of.t.stale'))
        + field('data_locality', tr('of.f.locality'), `<select name="data_locality">${opts([['disabled', tr('of.loc.disabled')], ['best-effort', tr('of.loc.best')]], 'disabled')}</select>`, tr('of.t.locality'))
        + field('disk_selector', tr('of.f.diskSelector'), `<input name="disk_selector" placeholder="ssd, nvme">`, tr('of.t.diskSelector'))
        + field('node_selector', tr('of.f.nodeSelector'), `<input name="node_selector" placeholder="storage">`, tr('of.t.nodeSelector'))
        + `<label class="bk-check tip" data-f="encrypted" data-tip="${esc(tr('sc.t.encrypted'))}"><input type="checkbox" name="encrypted"> <span>${esc(tr('sc.f.encrypted'))}</span></label>`
        + field('secret', tr('sc.f.secret'), `<select name="secret">${opts(o.crypto_secrets || [])}</select>`, tr('sc.t.secret'))
        + `<label class="bk-check tip" data-f="expand_online" data-tip="${esc(tr('sc.t.expandOnline'))}"><input type="checkbox" name="expand_online"> <span>${esc(tr('sc.f.expandOnline'))}</span></label>`
        + field('node', tr('sc.f.node'), `<select name="node">${opts(nodes)}</select>`, tr('sc.t.node'))
        + field('vg', tr('sc.f.vg'), `<select name="vg">${opts((o.lvm_groups || []).map(g => g.vg))}</select>`, tr('sc.t.vg'))
        // 2.10.1 : dm-thin par défaut ; striped déprécié, refusé à partir de 1.9.1
        + field('lvm_type', tr('sc.f.lvmType'), (() => {
          const refused = ((o.lvm_types || {}).striped) === 'refused';
          return `<select name="lvm_type"><option value="dm-thin" selected>dm-thin</option>
            <option value="striped" ${refused ? 'disabled' : ''}>${esc(refused ? tr('sc.lvmStripedRefused') : tr('sc.lvmStripedOld'))}</option></select>`;
        })(), tr('sc.t.lvmType'))
        + field('reclaim', tr('res.col.reclaim'), `<select name="reclaim">${opts(['Delete', 'Retain'], 'Delete')}</select>`, tr('of.t.reclaim'))
        + field('binding', tr('res.col.binding'), `<select name="binding">${opts(['Immediate', 'WaitForFirstConsumer'], 'Immediate')}</select>`, tr('of.t.binding'))
        + field('topology', tr('sc.f.topology'), `<input name="topology" placeholder="topology.kubernetes.io/zone=zone-a,zone-b">`, tr('sc.t.topology'))
        + field('description', tr('of.f.description'), `<input name="description">`, tr('of.t.description'))
        + `<label class="bk-check tip" data-f="migratable" data-tip="${esc(tr('of.t.migratable'))}"><input type="checkbox" name="migratable" checked> <span>${esc(tr('of.f.migratable'))}</span></label>`
        + `<label class="bk-check tip" data-tip="${esc(tr('of.t.expansion'))}"><input type="checkbox" name="expansion" checked> <span>${esc(tr('res.col.expansion'))}</span></label>`;
    }
    if (kind === 'sshkey') {
      return ns + name()
        + field('public_key', tr('res.d.publicKey'), `<textarea name="public_key" rows="4" required placeholder="ssh-ed25519 AAAA… user@host"></textarea>`, tr('of.t.publicKey'))
        + `<label class="bk-field"><span>${esc(tr('of.f.keyFile'))}</span><input type="file" name="key_file" accept=".pub,text/plain" class="tip" data-tip="${esc(tr('of.t.keyFile'))}"></label>`;
    }
    if (kind === 'secret') {
      // v1.64.0 : les types de secrets que l'interface de Rancher édite, et
      // le secret de chiffrement des classes de stockage
      return ns + name()
        + field('type', tr('res.col.type'), `<select name="type">${opts([['Opaque', 'Opaque'], ['kubernetes.io/basic-auth', tr('sec.basic')],
            ['kubernetes.io/ssh-auth', tr('sec.ssh')], ['kubernetes.io/tls', tr('sec.tls')],
            ['kubernetes.io/dockerconfigjson', tr('sec.registry')], ['crypto', tr('sec.crypto')]], 'Opaque')}</select>`, tr('sec.t.type'))
        + `<div class="of-kv" data-kv data-f="kv"><div class="of-kv-head"><span>${esc(tr('res.col.keys'))}</span>
            <button type="button" class="btn btn-sm btn-secondary tip" data-kv-add data-tip="${esc(tr('of.t.kvAdd'))}">${icon('add')} ${esc(tr('of.kvAdd'))}</button></div>
            <div class="of-kv-rows"></div></div>`
        + field('server', tr('sec.f.server'), `<input name="server" placeholder="quay.io">`, tr('sec.t.server'))
        + field('username', tr('sec.f.username'), `<input name="username" autocomplete="off">`, tr('sec.t.username'))
        + field('password', tr('sec.f.password'), `<input name="password" type="password" autocomplete="new-password">`, tr('sec.t.password'))
        + field('ssh_privatekey', tr('sec.f.sshKey'), `<textarea name="ssh_privatekey" rows="5"></textarea>`, tr('sec.t.sshKey'))
        + field('tls_crt', tr('sec.f.crt'), `<textarea name="tls_crt" rows="5"></textarea>`, tr('sec.t.crt'))
        + field('tls_key', tr('sec.f.key'), `<textarea name="tls_key" rows="5"></textarea>`, tr('sec.t.key'))
        + field('passphrase', tr('sec.f.passphrase'), `<input name="passphrase" type="password" autocomplete="new-password" minlength="8">`, tr('sec.t.passphrase'))
        + field('cipher', 'Cipher', `<select name="cipher">${opts(['aes-xts-plain64', 'aes-xts-plain', 'aes-cbc-plain64', 'aes-cbc-plain', 'aes-cbc-essiv:sha256'], 'aes-xts-plain64')}</select>`, tr('sec.t.cipher'));
    }
    if (kind === 'network') {
      const fab = await getJSON(`/api/network-fabric/${enc(cluster)}`);
      const cns = ((fab && fab.cluster_networks) || []).map(c => (typeof c === 'string' ? c : c.name)).filter(Boolean);
      return ns + name()
        + field('type', tr('res.col.type'), `<select name="type">${opts([['vlan', tr('of.net.vlan')], ['untagged', tr('of.net.untagged')],
            ['trunk', tr('na.trunk')]], 'vlan')}</select>`, tr('of.t.netType'))
        + field('vlan', tr('of.f.vlan'), `<input name="vlan" type="number" min="1" max="4094" required>`, tr('of.t.vlan'))
        // v1.65.0 : un trunk porte des plages de VLAN (« 100-199, 300 »)
        + field('ranges', tr('na.f.trunk'), `<input name="ranges" placeholder="100-199, 300">`, tr('na.t.trunk'))
        + field('cluster_network', tr('of.f.clusterNetwork'), `<select name="cluster_network">${opts(cns.length ? cns : ['mgmt'], 'mgmt')}</select>`, tr('of.t.clusterNetwork'))
        + field('route_mode', tr('of.f.route'), `<select name="route_mode">${opts([['auto', tr('of.route.auto')], ['manual', tr('of.route.manual')]], 'auto')}</select>`, tr('of.t.route'))
        + field('cidr', 'CIDR', `<input name="cidr" placeholder="192.168.10.0/24">`, tr('of.t.cidr'))
        + field('gateway', tr('of.f.gateway'), `<input name="gateway" placeholder="192.168.10.1">`, tr('of.t.gateway'))
        + field('dhcp_server', tr('na.f.dhcpServer'), `<input name="dhcp_server" placeholder="192.168.10.2">`, tr('na.t.dhcpServer'))
        + field('description', tr('of.f.description'), `<input name="description">`, tr('of.t.description'));
    }
    // volume
    const sc = await getJSON(`/api/cluster-objects/${enc(cluster)}/storageclasses`);
    const im = await getJSON(`/api/cluster-objects/${enc(cluster)}/images`);
    const classes = ((sc && sc.items) || []).filter(c => !c.image);
    const def = (classes.find(c => c.is_default) || {}).name;
    const images = ((im && im.items) || []).filter(i => i.state === 'ready');
    return ns + name()
      + field('size', tr('res.col.size'), `<input name="size" required value="10Gi" pattern="[1-9][0-9]*(Mi|Gi|Ti)">`, tr('of.t.size'))
      + field('source', tr('of.f.source'), `<select name="source">${opts([['empty', tr('of.src.empty')], ['image', tr('of.src.image')]], 'empty')}</select>`, tr('of.t.source'))
      + field('storage_class', tr('res.col.class'), `<select name="storage_class">${opts(classes.map(c => c.name), def)}</select>`, tr('of.t.volClass'))
      + field('image', tr('section.images'), `<select name="image">${opts(images.map(i => [`${i.namespace}/${i.name}`, i.display_name]))}</select>`, tr('of.t.volImage'));
  }

  function kvRow(k = '', v = '') {
    return `<div class="of-kv-row"><input name="kv-key" placeholder="${esc(tr('of.kvKey'))}" value="${esc(k)}" class="tip" data-tip="${esc(tr('of.t.kvKey'))}">
      <textarea name="kv-value" rows="2" placeholder="${esc(tr('of.kvValue'))}" class="tip" data-tip="${esc(tr('of.t.kvValue'))}">${esc(v)}</textarea>
      <button type="button" class="btn-icon-sm tip" data-kv-del data-tip="${esc(tr('of.t.kvDel'))}">×</button></div>`;
  }

  function sync(kind, form) {
    const val = (n) => form.querySelector(`[name="${n}"]`)?.value;
    const show = (n, on) => { const el = form.querySelector(`[data-f="${n}"]`); if (el) el.hidden = !on; };
    if (kind === 'network') {
      const trunk = val('type') === 'trunk';
      show('vlan', val('type') === 'vlan');
      form.querySelector('[name="vlan"]').required = val('type') === 'vlan';
      show('ranges', trunk);
      form.querySelector('[name="ranges"]').required = trunk;
      // comme Harvester : pas de route sur un trunk
      show('route_mode', !trunk);
      show('dhcp_server', !trunk);
      show('cidr', !trunk && val('route_mode') === 'manual');
      show('gateway', !trunk && val('route_mode') === 'manual');
    }
    if (kind === 'volume') {
      show('storage_class', val('source') === 'empty');
      show('image', val('source') === 'image');
    }
    if (kind === 'storageclass') {
      const eng = val('engine') || 'longhorn-v1';
      const lh = eng !== 'lvm';
      ['replicas', 'stale_timeout', 'data_locality', 'disk_selector', 'node_selector', 'encrypted', 'migratable'].forEach(n => show(n, lh));
      const enc2 = lh && form.querySelector('[name="encrypted"]')?.checked;
      show('secret', enc2);
      show('expand_online', enc2);
      ['node', 'vg', 'lvm_type'].forEach(n => show(n, eng === 'lvm'));
      show('topology', lh);
    }
    if (kind === 'secret') {
      const t = val('type') || 'Opaque';
      show('kv', t === 'Opaque');
      show('server', t === 'kubernetes.io/dockerconfigjson');
      show('username', t === 'kubernetes.io/basic-auth' || t === 'kubernetes.io/dockerconfigjson');
      show('password', t === 'kubernetes.io/basic-auth' || t === 'kubernetes.io/dockerconfigjson');
      show('ssh_privatekey', t === 'kubernetes.io/ssh-auth');
      show('tls_crt', t === 'kubernetes.io/tls');
      show('tls_key', t === 'kubernetes.io/tls');
      show('passphrase', t === 'crypto');
      show('cipher', t === 'crypto');
    }
  }

  function request(kind, form) {
    const f = new FormData(form);
    const o = {};
    for (const [k, v] of f.entries()) if (!['kv-key', 'kv-value', 'key_file'].includes(k)) o[k] = v;
    if (kind === 'storageclass') { o.migratable = !!f.get('migratable'); o.expansion = !!f.get('expansion'); }
    if (kind === 'secret') {
      const keys = f.getAll('kv-key'), vals = f.getAll('kv-value');
      const data = {};
      keys.forEach((k, i) => { if (String(k).trim()) data[String(k).trim()] = vals[i]; });
      const t = o.type || 'Opaque';
      if (t === 'crypto') return { name: o.name, namespace: o.namespace, type: 'crypto', passphrase: o.passphrase, cipher: o.cipher };
      const fields = t === 'Opaque' ? { data }
        : t === 'kubernetes.io/basic-auth' ? { username: o.username, password: o.password }
        : t === 'kubernetes.io/ssh-auth' ? { 'ssh-privatekey': o.ssh_privatekey }
        : t === 'kubernetes.io/tls' ? { 'tls.crt': o.tls_crt, 'tls.key': o.tls_key }
        : { server: o.server, username: o.username, password: o.password };
      return { name: o.name, namespace: o.namespace, type: t, fields };
    }
    if (kind === 'storageclass') {
      o.encrypted = !!f.get('encrypted');
      o.expand_online = !!f.get('expand_online');
      const topo = String(o.topology || '').trim();
      delete o.topology;
      if (topo.includes('=')) {
        const [key, values] = topo.split('=');
        o.topologies = [{ key: key.trim(), values: values.trim() }];
      }
    }
    if (kind === 'volume') {
      if (o.source === 'image') delete o.storage_class; else delete o.image;
      delete o.source;
    }
    if (kind === 'network' && o.type !== 'vlan') delete o.vlan;
    if (kind === 'network') {
      if (o.type === 'trunk') {
        o.ranges = window.NetAdmin ? NetAdmin.parseRanges(o.ranges) : [];
        ['route_mode', 'cidr', 'gateway', 'dhcp_server'].forEach(k => delete o[k]);
      } else delete o.ranges;
    }
    return o;
  }

  function followInto(root, actionId, doneText, onDone) {
    const msg = root.querySelector('.of-msg');
    if (window.Dock && Dock.poll) Dock.poll();
    msg.innerHTML = esc(tr('bk.started', { id: actionId }));
    if (!window.SSEReconnect) return;
    let last = '';
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => { try { const s = JSON.parse(e.data); if (s.message) { last = s.message; msg.textContent = s.message; } } catch { /* ligne illisible */ } },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          const ok = d.status === 'done';
          msg.innerHTML = ok ? `${icon('ok')} ${esc(doneText)}`
            : `<span class="res-error">${icon('fail')} ${esc(tr('res.error', { msg: d.error_summary || last || d.status || '?' }))}</span>`;
          if (onDone) onDone(ok);
        },
      },
    });
  }

  /** Une fenêtre « Nouveau … » (image, classe, clé, secret, réseau, volume). */
  async function openNew(kind, cluster, ctx = {}) {
    const panel = FloatingPanels.open({
      id: `new-${kind}-${cluster}`, icon: ICON[kind], width: 560, height: 560,
      title: `${TITLE[kind]()} · ${cluster}`,
      bodyHtml: `<form class="of-form" autocomplete="off"><p class="form-hint">${esc(HINT[kind]())}</p>
        <div class="of-fields"><p class="form-hint">${esc(tr('common.loading'))}</p></div>
        <div class="of-check"></div>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('bk.submitTip'))}">${icon('add')} ${esc(tr('of.create'))}</button></div>
        <div class="of-msg"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.ofReady) return;
    root.dataset.ofReady = '1';
    const form = root.querySelector('.of-form');
    form.querySelector('.of-fields').innerHTML = await fields(kind, cluster, ctx);
    if (kind === 'secret') form.querySelector('.of-kv-rows').innerHTML = kvRow('userdata');
    form.addEventListener('change', () => sync(kind, form));
    form.addEventListener('click', (e) => {
      if (e.target.closest('[data-kv-add]')) form.querySelector('.of-kv-rows').insertAdjacentHTML('beforeend', kvRow());
      const del = e.target.closest('[data-kv-del]');
      if (del && form.querySelectorAll('.of-kv-row').length > 1) del.closest('.of-kv-row').remove();
    });
    form.querySelector('[name="key_file"]')?.addEventListener('change', async (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) form.querySelector('[name="public_key"]').value = (await file.text()).trim();
    });
    sync(kind, form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      const spec = request(kind, form);
      try {
        // v1.64.0 : classe de stockage et secret typé ont leurs routes (les valeurs
        // d'un secret passent par un fichier privé, jamais une ligne de commande)
        const out = kind === 'storageclass' ? await call('POST', `/api/storageclass/${enc(cluster)}`, spec)
          : kind === 'secret' ? await call('POST', `/api/secret/${enc(cluster)}/${enc(spec.namespace || 'default')}/${enc(spec.name)}/do/create`, spec)
          : await call('POST', `/api/objects/${enc(cluster)}/${kind}`, { spec });
        followInto(root, out.action_id, DONE[kind](spec.name || spec.display_name || ''), (ok) => {
          btn.disabled = false;
          if (ok && ctx.onDone) ctx.onDone();
        });
      } catch (err) {
        btn.disabled = false;
        root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
    form.querySelector('input:not([type=hidden]), select, textarea')?.focus();
  }

  /** Supprimer un objet que rien n'utilise (le serveur le vérifie aussi). */
  async function remove(kind, cluster, row, onDone) {
    const label = row.namespace ? `${row.namespace}/${row.name}` : row.name;
    if (!confirm(tr('of.confirmDelete', { name: label }))) return;
    const q = row.namespace ? `?namespace=${enc(row.namespace)}` : '';
    const out = await call('DELETE', `/api/objects/${enc(cluster)}/${kind}/${enc(row.name)}${q}`);
    return out.action_id;
  }

  /** La configuration d'un add-on, dans une fenêtre (administrateurs). */
  async function openAddonValues(cluster, row, onDone) {
    const panel = FloatingPanels.open({
      id: `addon-values-${cluster}-${row.namespace}-${row.name}`, icon: 'settings', width: 720, height: 580,
      title: `${tr('of.addonValues', { name: row.name })} · ${cluster}`,
      bodyHtml: `<form class="of-form of-values"><p class="form-hint">${esc(tr('of.hint.addonValues'))}</p>
        <textarea name="values" class="of-yaml tip" spellcheck="false" data-tip="${esc(tr('of.t.values'))}">${esc(tr('common.loading'))}</textarea>
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('of.t.valuesSave'))}">${icon('save')} ${esc(tr('of.save'))}</button></div>
        <div class="of-msg"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.ofReady) return;
    root.dataset.ofReady = '1';
    const ta = root.querySelector('[name="values"]');
    try {
      const d = await call('GET', `/api/addons/${enc(cluster)}/${enc(row.namespace)}/${enc(row.name)}/values`);
      ta.value = d.values || '';
    } catch (err) {
      ta.value = '';
      root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
    }
    root.querySelector('form').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!confirm(tr('of.confirmValues', { name: row.name }))) return;
      try {
        const out = await call('POST', `/api/addons/${enc(cluster)}/${enc(row.namespace)}/${enc(row.name)}/values`, { values: ta.value });
        followInto(root, out.action_id, tr('of.done.values', { name: row.name }), onDone);
      } catch (err) {
        root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
  }

  /** Agrandir un volume (vue Stockage > Volumes). */
  async function expandVolume(cluster, ns, name, current, onDone) {
    const size = prompt(tr('of.expandPrompt', { name: `${ns}/${name}`, size: current || '?' }), current || '');
    if (!size || size === current) return null;
    const out = await call('POST', `/api/volumes/${enc(cluster)}/${enc(ns)}/${enc(name)}/expand`, { size });
    if (window.Dock && Dock.poll) Dock.poll();
    return out.action_id;
  }

  async function setDefaultClass(cluster, name) {
    if (!confirm(tr('of.confirmDefault', { name }))) return null;
    const out = await call('POST', `/api/storageclasses/${enc(cluster)}/${enc(name)}/default`);
    if (window.Dock && Dock.poll) Dock.poll();
    return out.action_id;
  }

  /** v1.64.0 : de nouvelles valeurs pour un secret (vide = garder). */
  function editSecret(cluster, row, onDone) {
    const panel = FloatingPanels.open({
      id: `edit-secret-${cluster}-${row.namespace}-${row.name}`, icon: 'lock', width: 520, height: 480,
      title: `${tr('sec.edit')} · ${row.namespace}/${row.name}`,
      bodyHtml: `<form class="of-form" autocomplete="off"><p class="form-hint">${esc(tr('sec.editHint', { type: row.type }))}</p>
        ${row.type === 'kubernetes.io/dockerconfigjson'
          ? field('server', tr('sec.f.server'), '<input name="server">', tr('sec.t.server'))
            + field('username', tr('sec.f.username'), '<input name="username">', tr('sec.t.username'))
            + field('password', tr('sec.f.password'), '<input name="password" type="password" autocomplete="new-password">', tr('sec.t.password'))
          : (row.keys || []).map(k => `<label class="bk-field"><span>${esc(k)}</span>
              <div class="sec-edit-row"><textarea data-sec-key="${esc(k)}" rows="2" placeholder="${esc(tr('sec.unchanged'))}" class="tip" data-tip="${esc(tr('sec.t.value'))}"></textarea>
              ${row.type === 'Opaque' ? `<label class="bk-check tip" data-tip="${esc(tr('sec.t.remove'))}"><input type="checkbox" data-sec-remove="${esc(k)}"> <span>${esc(tr('sec.remove'))}</span></label>` : ''}</div></label>`).join('')}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('bk.submitTip'))}">${icon('save')} ${esc(tr('hs.save'))}</button></div>
        <div class="of-msg"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.ofReady) return;
    root.dataset.ofReady = '1';
    const form = root.querySelector('form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const fields = {};
      form.querySelectorAll('[data-sec-key]').forEach(t => { if (t.value) fields[t.dataset.secKey] = t.value; });
      const remove = [...form.querySelectorAll('[data-sec-remove]:checked')].map(c => c.dataset.secRemove);
      if (remove.length) fields.remove = remove;
      ['server', 'username', 'password'].forEach(n => { const el = form.querySelector(`[name="${n}"]`); if (el && el.value) fields[n] = el.value; });
      try {
        const out = await call('POST', `/api/secret/${enc(cluster)}/${enc(row.namespace)}/${enc(row.name)}/do/update`, { fields });
        form.querySelectorAll('textarea, input[type=password]').forEach(el => { el.value = ''; });
        followInto(root, out.action_id, tr('sec.done.saved'), (ok) => { if (ok && onDone) onDone(); });
      } catch (err) {
        root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
  }

  /** v1.64.0 : changer la clé publique et la description d'une clé SSH. */
  function editSshKey(cluster, row, onDone) {
    const panel = FloatingPanels.open({
      id: `edit-sshkey-${cluster}-${row.namespace}-${row.name}`, icon: 'key', width: 520, height: 420,
      title: `${tr('key.edit')} · ${row.namespace}/${row.name}`,
      bodyHtml: `<form class="of-form" autocomplete="off">
        ${field('public_key', tr('res.d.publicKey'), `<textarea name="public_key" rows="4" required>${esc(row.public_key || '')}</textarea>`, tr('of.t.publicKey'))}
        ${field('description', tr('of.f.description'), `<input name="description" value="${esc(row.description || '')}">`, tr('of.t.description'))}
        <div class="bk-form-actions"><button type="submit" class="btn btn-sm btn-primary tip" data-tip="${esc(tr('bk.submitTip'))}">${icon('save')} ${esc(tr('hs.save'))}</button></div>
        <div class="of-msg"></div></form>`,
    });
    const root = panel.el;
    if (root.dataset.ofReady) return;
    root.dataset.ofReady = '1';
    const form = root.querySelector('form');
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        const out = await call('POST', `/api/sshkey/${enc(cluster)}/${enc(row.namespace)}/${enc(row.name)}/do/update`,
          { public_key: form.public_key.value, description: form.description.value });
        followInto(root, out.action_id, tr('key.done.saved'), (ok) => { if (ok && onDone) onDone(); });
      } catch (err) {
        root.querySelector('.of-msg').innerHTML = `<span class="res-error">${esc(err.message)}</span>`;
      }
    });
  }

  return { openNew, editSecret, editSshKey, remove, openAddonValues, expandVolume, setDefaultClass };
})();
window.ObjectForms = ObjectForms;
