/**
 * harvester-ops — onglet Bare-metal (BMC / Redfish + installation Harvester).
 *
 * Trois blocs :
 *   1. le magasin d'ISO (téléchargement côté serveur, en flux) ;
 *   2. la découverte Redfish des BMC ;
 *   3. l'installation zéro-touch d'une machine découverte, via média virtuel.
 *
 * Les identifiants BMC ne sont jamais persistés côté serveur. Ils sont
 * gardés en mémoire du module le temps de la session d'écran : avant, les
 * actions les relisaient dans le formulaire de découverte, qui se vide au
 * moindre re-render — les commandes partaient alors avec un mot de passe
 * vide et échouaient en 401 silencieux.
 */
const BMC = (() => {
  const $ = (s) => document.querySelector(s);
  // Le second argument est passé tel quel à i18n.t : c'est lui qui porte les
  // valeurs de substitution ({host}, {device}...). L'avaler ici affichait les
  // accolades brutes dans les confirmations.
  const tr = (k, params) => (window.i18n ? i18n.t(k, params) : k);
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  let creds = { user: '', password: '' };   // mémoire volatile, jamais envoyée ailleurs
  let lastNodes = [];

  // -------------------------------------------------------------------------
  // Magasin d'ISO
  // -------------------------------------------------------------------------
  async function renderIsoStore() {
    const box = $('#bmc-iso-store');
    if (!box) return;
    let d = { isos: [], disk_free: 0 };
    try {
      d = await fetch('/api/isos').then(r => r.json());
    } catch (e) { /* affiché vide */ }
    const gib = (n) => (n / 1073741824).toFixed(1);
    const rows = (d.isos || []).map(i => `
      <tr>
        <td><code>${esc(i.name)}</code></td>
        <td>${gib(i.size)} GiB</td>
        <td><code class="sha">${esc((i.sha256 || '').slice(0, 12) || '—')}</code></td>
        <td><button class="btn btn-sm btn-danger bmc-iso-del tip" data-name="${esc(i.name)}"
                    data-tip="${esc(tr('bmc.iso.deleteTip'))}">${Icons.svg('trash')}</button></td>
      </tr>`).join('') ||
      `<tr><td colspan="4" class="empty-state">${esc(tr('bmc.iso.empty'))}</td></tr>`;
    box.innerHTML = `
      <div class="card">
        <div class="card-header"><h2>${Icons.svg('cdrom', { size: 18 })} ${esc(tr('bmc.iso.title'))}</h2>
          <span class="form-hint">${gib(d.disk_free || 0)} GiB ${esc(tr('bmc.iso.free'))}</span>
        </div>
        <div class="card-body">
          <p class="form-hint">${esc(tr('bmc.iso.hint'))}</p>
          <form id="bmc-iso-form" class="capi-form">
            <fieldset>
              <legend>${esc(tr('bmc.iso.fetch'))}</legend>
              <label style="grid-column:1/-1;">${esc(tr('bmc.iso.url'))}
                <input name="url" type="url" required
                       placeholder="https://releases.rancher.com/harvester/v1.8.2/harvester-v1.8.2-amd64.iso"></label>
            </fieldset>
            <div class="apply-bar">
              <button type="submit" class="btn btn-primary btn-sm btn-ico tip"
                      data-tip="${esc(tr('bmc.iso.fetchTip'))}">${Icons.svg('download')} ${esc(tr('bmc.iso.fetchGo'))}</button>
            </div>
          </form>
          <table class="data-table" style="margin-top:10px;">
            <thead><tr><th>${esc(tr('bmc.iso.name'))}</th><th>${esc(tr('bmc.iso.size'))}</th>
                       <th>sha256</th><th>${esc(tr('col.actions') || 'Actions')}</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>`;
  }

  // -------------------------------------------------------------------------
  // Découverte
  // -------------------------------------------------------------------------
  // `force` reconstruit tout ; sinon on garde ce qui est déjà à l'écran et on
  // se contente de rafraîchir le magasin d'ISO. Une découverte prend une
  // minute : la perdre parce qu'on revient sur l'onglet serait pénible.
  function render(force) {
    const out = $('#bmc-body');
    if (!out) return;
    if (!force && out.querySelector('#bmc-discover-form')) {
      renderIsoStore();
      return;
    }
    out.innerHTML = `
      <div id="bmc-iso-store"></div>
      <div class="card" style="margin-top:12px;">
        <div class="card-header"><h2>${Icons.svg('search', { size: 18 })} ${esc(tr('bmc.discovery'))}</h2></div>
        <div class="card-body">
          <form id="bmc-discover-form" class="capi-form">
            <fieldset>
              <legend>${esc(tr('bmc.targets'))}</legend>
              <label style="grid-column:1/-1;">${esc(tr('bmc.hosts'))} *
                <textarea name="hosts" rows="2" required
                          placeholder="192.0.2.10, 192.0.2.11"></textarea></label>
              <label>${esc(tr('bmc.user'))} *
                <input name="user" required value="${esc(creds.user || 'admin')}"></label>
              <label>${esc(tr('bmc.password'))} *
                <input name="password" type="password" required></label>
            </fieldset>
            <div class="apply-bar">
              <button type="submit" class="btn btn-primary btn-sm btn-ico tip"
                      data-tip="${esc(tr('bmc.discoverTip'))}">${Icons.svg('search')} ${esc(tr('bmc.discover'))}</button>
            </div>
          </form>
          <p class="form-hint">${esc(tr('bmc.credsHint'))}</p>
        </div>
      </div>
      <div id="bmc-discover-result" style="margin-top:12px;"></div>`;
    renderIsoStore();
  }

  async function discover(ev) {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    const hosts = String(fd.get('hosts') || '')
      .split(/[\s,;]+/).map(h => h.trim()).filter(Boolean);
    creds = { user: fd.get('user') || '', password: fd.get('password') || '' };
    const out = $('#bmc-discover-result');
    if (out) out.innerHTML = `<div class="summary-bar">${esc(tr('bmc.discovering'))}</div>`;
    try {
      const r = await fetch('/api/bmc/discover', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hosts, ...creds }),
      });
      const d = await r.json();
      if (!r.ok) { if (out) out.innerHTML = `<div class="summary-bar bad">${esc(d.error)}</div>`; return; }
      lastNodes = d.nodes || [];
      if (out) out.innerHTML = renderResults(lastNodes);
    } catch (e) {
      if (out) out.innerHTML = `<div class="summary-bar bad">${esc(e.message)}</div>`;
    }
  }

  function renderResults(nodes) {
    const okCount = nodes.filter(n => n.ok).length;
    const errCount = nodes.length - okCount;
    let html = `<div class="summary-bar ${errCount ? 'warn' : 'ok'}">
        ${okCount} / ${nodes.length} BMC ${esc(tr('bmc.reachable'))}${errCount ? ` · ${errCount} ${esc(tr('bmc.unreachable'))}` : ''}
      </div>`;
    nodes.forEach(n => {
      if (!n.ok) {
        html += `<div class="card" style="margin-top:10px;"><div class="card-header">
          <h2><code>${esc(n.host)}</code> — <span class="err">${esc(n.error || '')}</span></h2>
        </div></div>`;
        return;
      }
      const nics = (n.nics || []).map(x => `
        <tr><td>${esc(x.name)}</td><td><code>${esc(x.mac || '—')}</code></td>
            <td>${esc(x.status || '—')}</td>
            <td>${x.speed_mbps ? x.speed_mbps + ' Mbps' : '—'}</td></tr>`).join('');
      const disks = (n.uefi_targets || []).filter(x => x.startsWith('HD.')).length;
      // L'installation n'est proposée que si la machine sait vraiment le
      // faire : lecteur virtuel CD et amorce CD pilotables.
      const installable = !!n.virtualmedia_path
        && (n.boot_targets || []).includes('Cd');
      html += `<div class="card" style="margin-top:10px;">
        <div class="card-header">
          <h2><code>${esc(n.host)}</code> — ${esc(n.model || '')}
            <span class="badge ${n.power_state === 'On' ? 'ok' : 'warn'}">${esc(n.power_state || '?')}</span></h2>
          <span class="form-hint">SN <code>${esc(n.serial || '—')}</code> · BIOS <code>${esc(n.bios_version || '—')}</code>
            · ${esc(n.memory_gib || '?')} GiB · ${disks} ${esc(tr('bmc.disks'))}</span>
        </div>
        <div class="card-body">
          <div class="apply-bar" style="margin-bottom:8px;">
            <button class="btn btn-sm btn-secondary btn-ico bmc-power tip" data-host="${esc(n.host)}" data-action="On"
                    data-tip="${esc(tr('bmc.tip.on'))}"><span class="icon-green">${Icons.svg('power')}</span> ${esc(tr('bmc.pw.on'))}</button>
            <button class="btn btn-sm btn-secondary btn-ico bmc-power tip" data-host="${esc(n.host)}" data-action="GracefulShutdown"
                    data-tip="${esc(tr('bmc.tip.gracefulOff'))}">${Icons.svg('stop')} ${esc(tr('bmc.pw.gracefulOff'))}</button>
            <button class="btn btn-sm btn-danger btn-ico bmc-power tip" data-host="${esc(n.host)}" data-action="ForceOff"
                    data-tip="${esc(tr('bmc.tip.forceOff'))}">${Icons.svg('power')} ${esc(tr('bmc.pw.forceOff'))}</button>
            <button class="btn btn-sm btn-secondary btn-ico bmc-power tip" data-host="${esc(n.host)}" data-action="GracefulRestart"
                    data-tip="${esc(tr('bmc.tip.restart'))}">${Icons.svg('restart')} ${esc(tr('bmc.pw.restart'))}</button>
            ${installable
              ? `<button class="btn btn-sm btn-primary btn-ico bmc-install tip" data-host="${esc(n.host)}"
                         data-tip="${esc(tr('bmc.installTip'))}">${Icons.svg('install')} ${esc(tr('bmc.install'))}</button>`
              : `<span class="badge warn tip" data-tip="${esc(tr('bmc.noMediaTip'))}">${esc(tr('bmc.noMedia'))}</span>`}
          </div>
          <table class="data-table">
            <thead><tr><th>NIC</th><th>MAC</th><th>${esc(tr('bmc.state'))}</th><th>${esc(tr('bmc.speed'))}</th></tr></thead>
            <tbody>${nics}</tbody>
          </table>
        </div>
      </div>`;
    });
    return html;
  }

  // -------------------------------------------------------------------------
  // Installation
  // -------------------------------------------------------------------------
  // Modes d'agrégat acceptés par l'installeur (pkg/config, bond_options.mode)
  // Modes où le noyau tient compte de xmit_hash_policy.
  const XMIT_MODES = ['802.3ad', 'balance-xor', 'balance-tlb', 'balance-alb'];
  const BOND_MODES = ['active-backup', 'balance-tlb', 'balance-alb', '802.3ad',
                      'balance-rr', 'balance-xor', 'broadcast'];
  const LACP_RATES = ['slow', 'fast'];
  const XMIT_POLICIES = ['layer2', 'layer2+3', 'layer3+4', 'encap2+3', 'encap3+4', 'vlan+srcmac'];
  // Champs facultatifs qu'un import vide quand le fichier ne les donne pas :
  // sinon une valeur saisie avant l'import s'ajouterait au fichier en silence.
  const IMPORT_CLEARED = ['data_disk', 'dns', 'ntp', 'labels', 'modules', 'ssh_keys',
                          'vlan_id', 'bond_lacp_rate', 'bond_xmit_hash_policy', 'server_url'];
  const IMPORT_MAX = 256 * 1024;

  // Remarques d'un import, en phrases. Table littérale : chaque clé reste
  // visible des tests de traduction.
  function noteText(code) {
    switch (code) {
      case 'iso-url-replaced': return tr('bmc.note.isoUrl');
      case 'join-detected': return tr('bmc.note.join');
      case 'automatic-ignored': return tr('bmc.note.automatic');
      case 'power-off-ignored': return tr('bmc.note.powerOff');
      case 'bond-default-active-backup': return tr('bmc.note.bondDefault');
      case 'wipe-list-advanced': return tr('bmc.note.wipeAdvanced');
      default: return code;
    }
  }

  // Raison d'un refus de chemin (unknown, reserved, conflict, type:<x>,
  // range:<a-b>, format:<f>...), en phrase.
  function reasonText(code) {
    const c = String(code || '');
    const [kind, detail] = [c.split(':')[0], c.slice(c.indexOf(':') + 1)];
    switch (kind) {
      case 'unknown': return tr('bmc.reason.unknown');
      case 'duplicate': return tr('bmc.reason.duplicate');
      case 'unknown-setting': return tr('bmc.reason.unknownSetting');
      case 'reserved': return tr('bmc.reason.reserved');
      case 'conflict': return tr('bmc.reason.conflict');
      case 'superseded': return tr('bmc.reason.superseded');
      case 'yaml': return tr('bmc.reason.yaml');
      case 'type': return tr('bmc.reason.type', { type: detail });
      case 'range': return tr('bmc.reason.range', { range: detail });
      case 'format': return tr('bmc.reason.format', { format: detail });
      // contrôles des disques (1.78.0, baremetal_disks.check_disk_roles)
      case 'role-twice': return tr('bmc.reason.roleTwice');
      case 'no-os': return tr('bmc.reason.noOs');
      case 'too-small': return tr('bmc.reason.tooSmall', { gib: detail });
      case 'has-data': return tr('bmc.reason.hasData');
      case 'pool-tag': return tr('bmc.reason.poolTag');
      case 'unknown-disk': return tr('bmc.reason.unknownDisk');
      case 'no-identity': return tr('bmc.reason.noIdentity');
      default: return c || tr('bmc.reason.refused');
    }
  }

  // Liste lisible des chemins refusés : `chemin` : raison.
  function refusalHtml(title, fields, reasons) {
    const items = (fields || []).map(p => `<li><code>${esc(p)}</code> : ${
      esc(reasonText((reasons || {})[p]))}</li>`).join('');
    return `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(title)}
      <ul class="bm-paths">${items}</ul></div>`;
  }

  const optionsHtml = (values, selected) => values.map(v =>
    `<option value="${esc(v)}"${v === selected ? ' selected' : ''}>${esc(v)}</option>`).join('');

  const normMac = (v) => String(v || '').trim().toLowerCase().replace(/-/g, ':');

  // -------------------------------------------------------------------------
  // Disques (1.78.0) : inventaire du démarrage de découverte, un rôle par
  // disque. Les valeurs viennent de la machine : toujours échappées.
  // -------------------------------------------------------------------------
  const GIB = 1073741824;
  // Tailles minimales de l'installeur v1.9 (mêmes valeurs que
  // web/baremetal_disks.py) ; le serveur refait les contrôles.
  const MIN_OS_ALONE = 250, MIN_OS_WITH_DATA = 180, MIN_DATA = 50;
  const POOL_TAG_RE = /^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$/;
  const ROLES = ['ignore', 'os', 'data', 'pool', 'wipe'];
  const POLL_MS = 3000;

  // Libellés littéraux : chaque clé reste visible des tests de traduction.
  function roleLabel(role) {
    switch (role) {
      case 'os': return tr('bmc.disk.role.os');
      case 'data': return tr('bmc.disk.role.data');
      case 'pool': return tr('bmc.disk.role.pool');
      case 'wipe': return tr('bmc.disk.role.wipe');
      default: return tr('bmc.disk.role.ignore');
    }
  }

  const gibOf = (d) => Math.floor((Number(d.size_bytes) || 0) / GIB);

  function mediaOf(d) {
    if (String(d.transport || '').toLowerCase() === 'nvme') return 'NVMe';
    return d.rotational ? 'HDD' : 'SSD';
  }

  // Tous les noms qui désignent un disque (chemin stable, liens, /dev/<nom>),
  // comme `_index` côté serveur.
  function diskNames(d) {
    const l = d.links || {};
    return [d.stable_path, '/dev/' + (d.name || ''), ...(l.by_id || []), ...(l.by_path || [])]
      .filter(Boolean);
  }

  // Cadre RAID, lecture seule : seulement quand le BMC publie son stockage.
  function raidHtml(storage) {
    const ctrls = (storage && storage.controllers) || [];
    if (!ctrls.length) return '';
    const blocks = ctrls.map(c => {
      const vols = c.volumes || [];
      // pass-through : aucun niveau RAID ni création de volume possible, ou
      // des volumes qui ne sont que des disques bruts
      const passthrough = (!(c.raid_types || []).length && !c.can_create_volume)
        || (vols.length > 0 && vols.every(v => v.volume_type === 'RawDevice'));
      const rows = vols.map(v => `<tr><td>${esc(v.name || v.id)}</td><td>${esc(v.raid_type || v.volume_type || '')}</td>
          <td>${v.capacity_bytes ? Math.floor(v.capacity_bytes / GIB) + ' GiB' : ''}</td><td>${esc(v.health || '')}</td></tr>`).join('');
      return `<div class="bm-raid-ctrl">
          <div><strong>${esc(c.name || c.id)}</strong> ${esc(c.model || '')}
            <span class="badge ${passthrough ? 'ok' : 'warn'}">${esc(passthrough ? tr('bmc.raid.passthrough') : tr('bmc.raid.raid'))}</span>
            <span class="form-hint">${(c.drives || []).length} ${esc(tr('bmc.disks'))}</span></div>
          ${rows ? `<table class="data-table bm-raid-vols"><thead><tr><th>${esc(tr('bmc.raid.volume'))}</th>
            <th>${esc(tr('bmc.raid.type'))}</th><th>${esc(tr('bmc.disk.col.size'))}</th><th>${esc(tr('bmc.state'))}</th></tr></thead>
            <tbody>${rows}</tbody></table>` : `<span class="form-hint">${esc(tr('bmc.raid.noVolume'))}</span>`}
        </div>`;
    }).join('');
    return `<fieldset class="bm-raid">
        <legend>${esc(tr('bmc.raid.title'))}</legend>
        ${blocks}
        <p class="form-hint">${esc(tr('bmc.raid.advice'))}</p>
      </fieldset>`;
  }

  async function openInstall(host) {
    const node = lastNodes.find(n => n.host === host) || {};
    let isos = [];
    try { isos = (await fetch('/api/isos').then(r => r.json())).isos || []; } catch (e) { /* vide */ }
    if (!isos.length) { alert(tr('bmc.needIso')); return; }
    // La valeur est la MAC, pas le nom Redfish : sur ces machines les deux
    // cartes s'appellent « System Ethernet Interface », et de toute façon
    // Redfish ignore le nom que Linux donnera à l'interface. Plusieurs cartes
    // cochées forment un agrégat (v1.77.0).
    const nicBoxes = (node.nics || []).map((n, k) => {
      const state = n.status ? ' · ' + esc(n.status) : '';
      return `<label class="bm-check"><input type="checkbox" name="mgmt_nic" value="${esc(n.mac)}"${k === 0 ? ' checked' : ''}>
        NIC ${k + 1} · <code>${esc(n.mac)}</code>${state} <span class="bm-nic-linux form-hint"></span></label>`;
    }).join('');
    const isoOpts = isos.map(i => `<option value="${esc(i.name)}">${esc(i.name)}</option>`).join('');
    const tipAttr = (text) => `class="tip" data-tip="${esc(text)}"`;

    const panel = FloatingPanels.open({
      id: `bm-install-${host}`,
      title: `${tr('bmc.install')} · ${host}`,
      // Défilement interne : la fenêtre tient dans un écran de 900 px ; le
      // tableau des disques (1.78.0) demande de la largeur.
      width: Math.max(720, Math.min(1000, window.innerWidth - 180)), height: Math.max(480, Math.min(860, window.innerHeight - 80)),
      bodyHtml: `
        <form id="bm-install-form" class="capi-form" style="padding:14px;" autocomplete="off">
          <p class="form-hint vm-edit-unverified">${esc(tr('bmc.installWarn'))}</p>
          <div class="apply-bar bm-config-tools">
            <button type="button" class="btn btn-sm btn-secondary btn-ico tip" data-bm="import"
                    data-tip="${esc(tr('bmc.importTip'))}">${Icons.svg('upload')} ${esc(tr('bmc.import'))}</button>
            <input type="file" data-bm="file" accept=".yaml,.yml,.txt,text/yaml,application/x-yaml,text/plain" hidden>
            <button type="button" class="btn btn-sm btn-secondary btn-ico tip" data-bm="preview"
                    data-tip="${esc(tr('bmc.previewTip'))}">${Icons.svg('preview')} ${esc(tr('bmc.preview'))}</button>
          </div>
          <div class="bm-config-msg" id="bm-config-msg" role="status"></div>
          <input type="hidden" name="mode" value="create">
          <input type="hidden" name="server_url" value="">
          <fieldset>
            <legend>${esc(tr('bmc.fs.image'))}</legend>
            <label style="grid-column:1/-1;">${esc(tr('bmc.f.iso'))} *
              <select name="iso" required>${isoOpts}</select></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.node'))}</legend>
            <label>${esc(tr('bmc.f.hostname'))} *
              <input name="hostname" required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?"
                     value="harvester-node1"></label>
            <label>${esc(tr('bmc.f.method'))}
              <select name="method" id="bm-method">
                <option value="static">static</option><option value="dhcp">dhcp</option></select></label>
            <label class="bm-static">${esc(tr('bmc.f.ip'))} *
              <input name="ip" required placeholder="192.0.2.10"></label>
            <label class="bm-static">${esc(tr('bmc.f.mask'))} *
              <input name="subnet_mask" required value="255.255.255.0"></label>
            <label class="bm-static">${esc(tr('bmc.f.gateway'))} *
              <input name="gateway" required placeholder="192.0.2.1"></label>
            <label class="bm-create">${esc(tr('bmc.f.vip'))} *<input name="vip" required placeholder="192.0.2.100"></label>
            <label class="bm-create" ${tipAttr(tr('bmc.tip.vipMode'))}>${esc(tr('bmc.f.vipMode'))}
              <select name="vip_mode">${optionsHtml(['static', 'dhcp'], 'static')}</select></label>
            <label class="bm-create-opt" ${tipAttr(tr('bmc.tip.clusterName'))}>${esc(tr('bmc.f.clusterName'))}
              <input name="cluster_name" pattern="[a-z0-9]([-a-z0-9.]{0,59}[a-z0-9])?" maxlength="61"
                     value="harvester-node1"></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.mgmt'))}</legend>
            <div class="bm-nics tip" data-tip="${esc(tr('bmc.tip.nics'))}">
              <span class="bm-nics-title">${esc(tr('bmc.f.mgmtNics'))} *</span>
              ${nicBoxes}
            </div>
            <label ${tipAttr(tr('bmc.tip.bondMode'))}>${esc(tr('bmc.f.bondMode'))}
              <select name="bond_mode" id="bm-bond-mode">
                <option value="" data-unset hidden>${esc(tr('bmc.f.bondUnset'))}</option>
                ${optionsHtml(BOND_MODES, 'balance-tlb')}</select></label>
            <label ${tipAttr(tr('bmc.tip.miimon'))}>${esc(tr('bmc.f.miimon'))}
              <input name="bond_miimon" type="number" min="0" step="1" value="100"></label>
            <label class="bm-lacp" ${tipAttr(tr('bmc.tip.lacp'))}>${esc(tr('bmc.f.lacpRate'))}
              <select name="bond_lacp_rate"><option value=""></option>${optionsHtml(LACP_RATES, '')}</select></label>
            <label class="bm-xmit" ${tipAttr(tr('bmc.tip.xmit'))}>${esc(tr('bmc.f.xmitHash'))}
              <select name="bond_xmit_hash_policy"><option value=""></option>${optionsHtml(XMIT_POLICIES, '')}</select></label>
            <label ${tipAttr(tr('bmc.tip.vlan'))}>${esc(tr('bmc.f.vlan'))}
              <input name="vlan_id" type="number" min="0" max="4094" step="1" placeholder="200"></label>
          </fieldset>
          <fieldset class="bm-disks-fs">
            <legend>${esc(tr('bmc.fs.disks'))}</legend>
            <div class="bm-disks-head">
              <span class="form-hint bm-inv-source" id="bm-inv-source"></span>
              <span class="bm-disks-tools">
                <button type="button" class="btn btn-sm btn-secondary btn-ico tip" data-bm="discover-boot"
                        data-tip="${esc(tr('bmc.disk.discoverTip'))}">${Icons.svg('search')} ${esc(tr('bmc.disk.discover'))}</button>
                <button type="button" class="btn btn-sm btn-secondary btn-ico tip" data-bm="read-bmc"
                        data-tip="${esc(tr('bmc.disk.readBmcTip'))}">${Icons.svg('disk')} ${esc(tr('bmc.disk.readBmc'))}</button>
                <button type="button" class="btn btn-sm btn-secondary btn-ico tip" data-bm="inv-reload"
                        data-tip="${esc(tr('bmc.disk.reloadTip'))}">${Icons.svg('refresh')} ${esc(tr('bmc.disk.reload'))}</button>
                <label class="bm-check tip" data-tip="${esc(tr('bmc.disk.freeTextTip'))}">
                  <input type="checkbox" data-bm="free-text"> ${esc(tr('bmc.disk.freeText'))}</label>
              </span>
            </div>
            <div class="bm-disc-status" id="bm-disc-status" role="status"></div>
            <div class="bm-disks" id="bm-disks"></div>
            <div class="bm-pools" id="bm-pools"></div>
            <label class="bm-free">${esc(tr('bmc.f.device'))} *
              <input name="device" required value="/dev/sda"></label>
            <label class="bm-free" ${tipAttr(tr('bmc.tip.dataDisk'))}>${esc(tr('bmc.f.dataDisk'))}
              <input name="data_disk" placeholder="/dev/sdb"></label>
            <div class="bm-wipe" style="grid-column:1/-1;">
              <label class="bm-check tip" data-tip="${esc(tr('bmc.tip.wipe'))}">
                <input type="checkbox" name="wipe_all_disks"> ${esc(tr('bmc.f.wipe'))}</label>
              <span class="form-hint vm-edit-unverified bm-wipe-warn">${Icons.svg('warn', { size: 12 })} ${esc(tr('bmc.f.wipeWarn'))}</span>
            </div>
          </fieldset>
          <div id="bm-raid-box">${raidHtml(node.storage)}</div>
          <fieldset>
            <legend>${esc(tr('bmc.fs.system'))}</legend>
            <label>${esc(tr('bmc.f.dns'))}<input name="dns" autocomplete="off" placeholder="9.9.9.9, 1.1.1.1"></label>
            <label>${esc(tr('bmc.f.ntp'))}<input name="ntp" autocomplete="off" placeholder="0.suse.pool.ntp.org, 1.suse.pool.ntp.org"></label>
            <label style="grid-column:1/-1;" ${tipAttr(tr('bmc.tip.labels'))}>${esc(tr('bmc.f.labels'))}
              <textarea name="labels" rows="2" placeholder="topology.kubernetes.io/zone=zone-a"></textarea></label>
            <label style="grid-column:1/-1;" ${tipAttr(tr('bmc.tip.modules'))}>${esc(tr('bmc.f.modules'))}
              <input name="modules" autocomplete="off" placeholder="rbd, nbd"></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.access'))}</legend>
            <label>${esc(tr('bmc.f.token'))} *<input name="token" type="password" autocomplete="new-password" required></label>
            <label>${esc(tr('bmc.f.ospw'))} *<input name="password" type="password" autocomplete="new-password" required></label>
            <label style="grid-column:1/-1;">${esc(tr('bmc.f.sshkeys'))}
              <textarea name="ssh_keys" rows="2" placeholder="ssh-ed25519 AAAA..."></textarea></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.advanced'))}</legend>
            <label style="grid-column:1/-1;">${esc(tr('bmc.f.extraArgs'))}
              <input name="extra_args" placeholder="console=ttyS1,115200 harvester.install.skipchecks=true">
              <span class="form-hint">${esc(tr('bmc.f.extraArgsHint'))}</span></label>
            <label style="grid-column:1/-1;" ${tipAttr(tr('bmc.tip.advYaml'))}>${esc(tr('bmc.f.advYaml'))}
              <textarea name="advanced_yaml" class="adv-code bm-adv-yaml" rows="10" spellcheck="false"
                        autocapitalize="off" placeholder="os:&#10;  write_files:&#10;  - path: /etc/example.conf&#10;    content: |&#10;      key=value"></textarea>
              <span class="form-hint">${esc(tr('bmc.f.advYamlHint'))}</span></label>
          </fieldset>
          <div class="apply-bar">
            <button type="submit" class="btn btn-primary btn-sm btn-ico tip"
                    data-tip="${esc(tr('bmc.installTip'))}">${Icons.svg('install')} ${esc(tr('bmc.installGo'))}</button>
            <span class="apply-result" id="bm-install-result"></span>
          </div>
        </form>`,
    });
    // Fenêtre déjà ouverte : FloatingPanels la ramène au premier plan sans
    // refaire son contenu ; rebrancher les écouteurs doublerait chaque envoi.
    if (panel.el.dataset.bmBound) return;
    panel.el.dataset.bmBound = '1';

    const form = panel.el.querySelector('#bm-install-form');
    const field = (name) => form.querySelector(`[name="${name}"]`);
    const msg = panel.el.querySelector('#bm-config-msg');
    // Secrets d'un fichier importé : gardés par le serveur, désignés par cet
    // identifiant ; le navigateur ne les voit jamais.
    let importId = null;
    const fromFile = { token: false, password: false };

    // --- Disques (1.78.0) ----------------------------------------------------
    // L'état des rôles est gardé par chemin stable : relire l'inventaire ne
    // perd pas les choix faits.
    let inv = null;                 // {source, at, disks, nics} ou null
    let invSeen = false;
    const roles = {};               // chemin stable -> {role, tag, wipe}
    const replicas = {};            // étiquette de pool -> répliques
    let serverIssues = {};          // chemin stable -> [raisons du serveur]
    let pendingWipe = [];           // chemins d'un fichier importé pas encore placés
    let discovering = false;
    let redfish = null;             // stockage lu par le BMC, {controllers, at}
    const freeBox = panel.el.querySelector('[data-bm="free-text"]');
    const disksBox = panel.el.querySelector('#bm-disks');
    const poolsBox = panel.el.querySelector('#bm-pools');
    const discStatus = panel.el.querySelector('#bm-disc-status');
    const discBtn = panel.el.querySelector('[data-bm="discover-boot"]');
    const disks = () => (inv && inv.disks) || [];
    const useTable = () => disks().length > 0;
    const stateOf = (d) => roles[d.stable_path]
      || (roles[d.stable_path] = { role: 'ignore', tag: '', wipe: false });
    const findDisk = (p) => {
      const v = String(p || '').trim();
      return v ? disks().find(d => diskNames(d).includes(v)) || null : null;
    };
    const skipchecks = () => /(^|\s)harvester\.install\.skipchecks=true(\s|$)/
      .test(form.querySelector('[name="extra_args"]').value || '');
    const pick = (role) => disks().find(d => stateOf(d).role === role) || null;

    function setRole(d, role) {
      // un seul disque système, un seul disque de données
      if (role === 'os' || role === 'data') {
        disks().forEach(x => { if (x !== d && stateOf(x).role === role) stateOf(x).role = 'ignore'; });
      }
      const st = stateOf(d);
      st.role = role;
      if (role === 'pool' && !st.tag) st.tag = mediaOf(d).toLowerCase();
    }

    // Contrôles côté navigateur, à titre d'indication : le serveur décide.
    function localIssues() {
      const out = {};
      const add = (d, code) => { (out[d.stable_path] = out[d.stable_path] || []).push(code); };
      const withData = freeBox.checked ? !!field('data_disk').value.trim() : !!pick('data');
      const wipeAll = field('wipe_all_disks').checked;
      const skip = skipchecks();
      disks().forEach(d => {
        const st = stateOf(d);
        if (!['os', 'data', 'pool'].includes(st.role)) return;
        if (!skip) {
          const need = st.role === 'os' ? (withData ? MIN_OS_WITH_DATA : MIN_OS_ALONE) : MIN_DATA;
          if (gibOf(d) < need) add(d, `too-small:${need}`);
        }
        if (st.role === 'pool' && !POOL_TAG_RE.test(st.tag)) add(d, 'pool-tag');
        if (st.role === 'pool' && !d.serial && !d.wwn) add(d, 'no-identity');
        if (d.has_data && !st.wipe && !wipeAll) add(d, 'has-data');
      });
      return out;
    }

    function poolGroups() {
      const g = {};
      disks().forEach(d => {
        const st = stateOf(d);
        if (st.role === 'pool') (g[st.tag] = g[st.tag] || []).push(d);
      });
      return g;
    }

    const fmtAt = (at) => {
      const t = Number(at);
      return t ? new Date(t * 1000).toLocaleString() : '';
    };

    function renderDisks() {
      const src = panel.el.querySelector('#bm-inv-source');
      src.textContent = inv
        ? tr('bmc.disk.source', { source: tr('bmc.disk.sourceDiscovery'), at: fmtAt(inv.at) })
        : redfish
          ? tr('bmc.disk.source', { source: tr('bmc.disk.sourceRedfish'), at: fmtAt(redfish.at) })
          : tr('bmc.disk.noInventory');
      if (!useTable()) {
        disksBox.innerHTML = redfish ? redfishTable() : `<p class="form-hint">${esc(tr('bmc.disk.none'))}</p>`;
        syncDisks();
        return;
      }
      const rows = disks().map((d, i) => {
        const st = stateOf(d);
        const parts = d.partitions || [];
        const fs = [...new Set(parts.map(x => x.fstype).concat([d.fstype]).filter(Boolean))];
        const partsText = parts.length || d.fstype
          ? `${parts.length}${fs.length ? ' (' + fs.join(', ') + ')' : ''}` : tr('bmc.disk.noParts');
        const dataBadge = d.has_data
          ? ` <span class="badge warn tip bm-badge-data" data-tip="${esc(tr('bmc.disk.hasDataTip'))}">${esc(tr('bmc.disk.hasData'))}</span>` : '';
        const kernelBadge = d.stable_kind === 'kernel'
          ? ` <span class="badge warn tip bm-badge-kernel" data-tip="${esc(tr('bmc.disk.kernelNameTip'))}">${esc(tr('bmc.disk.kernelName'))}</span>` : '';
        const opts = ROLES.map(r => `<option value="${r}"${st.role === r ? ' selected' : ''}>${esc(roleLabel(r))}</option>`).join('');
        return `<tr class="bm-disk-row" data-i="${i}" data-path="${esc(d.stable_path)}">
            <td class="bm-num">${gibOf(d)} GiB</td>
            <td>${esc(d.model || '')}</td>
            <td class="bm-serial"><code>${esc(d.serial || '')}</code></td>
            <td>${esc(mediaOf(d))}</td>
            <td>${esc(d.transport || '')}</td>
            <td>${esc(partsText)}${dataBadge}</td>
            <td class="bm-disk-path"><code>${esc(d.stable_path)}</code>${kernelBadge}</td>
            <td class="bm-disk-role">
              <select class="tip" data-disk-role data-tip="${esc(tr('bmc.disk.roleTip'))}">${opts}</select>
              <input class="bm-pool-tag tip" data-disk-tag maxlength="32" autocomplete="off" spellcheck="false"
                     placeholder="${esc(tr('bmc.disk.tagPlaceholder'))}" value="${esc(st.tag)}" data-tip="${esc(tr('bmc.disk.tagTip'))}"></td>
            <td><label class="bm-check tip" data-tip="${esc(tr('bmc.disk.wipeTip'))}"><input type="checkbox" data-disk-wipe></label></td>
          </tr>
          <tr class="bm-disk-notes" data-notes="${i}" hidden><td colspan="9"></td></tr>`;
      }).join('');
      disksBox.innerHTML = `<table class="data-table bm-disk-table">
          <thead><tr><th>${esc(tr('bmc.disk.col.size'))}</th><th>${esc(tr('bmc.disk.col.model'))}</th>
            <th>${esc(tr('bmc.disk.col.serial'))}</th><th>${esc(tr('bmc.disk.col.media'))}</th>
            <th>${esc(tr('bmc.disk.col.bus'))}</th><th>${esc(tr('bmc.disk.col.parts'))}</th>
            <th>${esc(tr('bmc.disk.col.path'))}</th><th>${esc(tr('bmc.disk.col.role'))}</th>
            <th>${esc(tr('bmc.disk.col.wipe'))}</th></tr></thead>
          <tbody>${rows}</tbody></table>`;
      syncDisks();
    }

    // Disques listés par le BMC (sans découverte) : information seulement.
    // Redfish ne donne pas le nom que Linux leur donnera : aucun chemin n'est
    // proposé, le choix passe par un démarrage de découverte ou la saisie libre.
    function redfishTable() {
      const ctrls = redfish.controllers || [];
      const drives = ctrls.flatMap(c => (c.drives || []).map(d => ({ ...d, ctrl: c.name || c.id })));
      if (!ctrls.length) return `<p class="form-hint bm-redfish-none">${Icons.svg('warn', { size: 12 })} ${esc(tr('bmc.disk.redfishNone'))}</p>`;
      if (!drives.length) return `<p class="form-hint bm-redfish-none">${Icons.svg('warn', { size: 12 })} ${esc(tr('bmc.disk.redfishNoDrives'))}</p>`;
      const media = (d) => (String(d.protocol || '').toLowerCase() === 'nvme' ? 'NVMe' : (d.media || ''));
      const rows = drives.map(d => `<tr class="bm-redfish-row">
          <td class="bm-num">${d.capacity_bytes ? Math.floor(d.capacity_bytes / GIB) + ' GiB' : ''}</td>
          <td>${esc(d.model || '')}</td>
          <td class="bm-serial"><code>${esc(d.serial || '')}</code></td>
          <td>${esc(media(d))}</td>
          <td>${esc(d.protocol || '')}</td>
          <td>${esc(d.ctrl || '')}</td>
          <td class="bm-disk-path"></td>
        </tr>`).join('');
      return `<table class="data-table bm-disk-table bm-redfish-table">
          <thead><tr><th>${esc(tr('bmc.disk.col.size'))}</th><th>${esc(tr('bmc.disk.col.model'))}</th>
            <th>${esc(tr('bmc.disk.col.serial'))}</th><th>${esc(tr('bmc.disk.col.media'))}</th>
            <th>${esc(tr('bmc.disk.col.bus'))}</th><th>${esc(tr('bmc.disk.col.controller'))}</th>
            <th>${esc(tr('bmc.disk.col.path'))}</th></tr></thead>
          <tbody>${rows}</tbody></table>
        <p class="form-hint bm-redfish-only">${Icons.svg('warn', { size: 12 })} ${esc(tr('bmc.disk.redfishOnly'))}</p>`;
    }

    function renderPools() {
      if (!useTable()) { poolsBox.innerHTML = ''; return; }
      const g = poolGroups();
      const lines = Object.keys(g).sort().map(t => `<div class="bm-pool-line" data-pool="${esc(t)}">
          <span>${Icons.svg('storage', { size: 14 })} ${esc(tr('bmc.disk.poolLine', { tag: t || '?', count: g[t].length, cls: 'longhorn-' + t }))}</span>
          <label class="bm-check tip" data-tip="${esc(tr('bmc.disk.replicasTip'))}">${esc(tr('bmc.disk.replicas'))}
            <input type="number" min="1" max="3" step="1" data-replicas="${esc(t)}" value="${esc(replicas[t] || 1)}"></label>
        </div>`).join('');
      const hasOs = freeBox.checked ? !!field('device').value.trim() : !!pick('os');
      poolsBox.innerHTML = (lines ? `<div class="bm-pools-title">${esc(tr('bmc.disk.pools'))}</div>${lines}` : '')
        + (hasOs ? '' : `<div class="bm-disk-hint bm-no-os">${Icons.svg('warn', { size: 12 })} ${esc(reasonText('no-os'))}</div>`)
        + (skipchecks() ? `<div class="form-hint">${esc(tr('bmc.disk.skipchecks'))}</div>` : '');
    }

    // État visible des lignes, sans reconstruire le tableau (le curseur
    // reste dans le champ d'étiquette pendant la saisie).
    function syncDisks() {
      const table = useTable();
      if (!table) freeBox.checked = true;
      freeBox.disabled = !table;
      const free = freeBox.checked;
      panel.el.querySelectorAll('.bm-free').forEach(l => { l.hidden = !free; });
      field('device').required = free;
      const local = localIssues();
      disks().forEach((d, i) => {
        const row = disksBox.querySelector(`tr[data-i="${i}"]`);
        if (!row) return;
        const st = stateOf(d);
        const sel = row.querySelector('[data-disk-role]');
        sel.querySelectorAll('option[value="os"], option[value="data"]').forEach(o => { o.disabled = free; });
        sel.value = st.role;
        const tag = row.querySelector('[data-disk-tag]');
        tag.hidden = st.role !== 'pool';
        tag.classList.toggle('bm-invalid', st.role === 'pool' && !POOL_TAG_RE.test(st.tag));
        const wipe = row.querySelector('[data-disk-wipe]');
        wipe.disabled = st.role === 'ignore' || st.role === 'wipe';
        wipe.checked = st.role === 'wipe' || (st.role !== 'ignore' && st.wipe);
        const srv = serverIssues[d.stable_path] || [];
        const hints = (local[d.stable_path] || []).filter(c => !srv.includes(c));
        const items = srv.map(c => `<span class="bm-disk-refused">${Icons.svg('fail', { size: 12 })} ${esc(reasonText(c))}</span>`)
          .concat(hints.map(c => `<span class="bm-disk-hint">${Icons.svg('warn', { size: 12 })} ${esc(reasonText(c))}</span>`));
        const notes = row.nextElementSibling;
        notes.hidden = !items.length;
        notes.firstElementChild.innerHTML = items.join(' ');
        row.classList.toggle('bm-disk-bad', srv.length > 0);
        row.classList.toggle('bm-disk-used', st.role !== 'ignore');
      });
      renderPools();
    }

    // Nom Linux et état du lien des cartes, quand la découverte les a vus.
    function renderNicNames() {
      const nics = (inv && inv.nics) || [];
      panel.el.querySelectorAll('.bm-nics [name="mgmt_nic"]').forEach(c => {
        const span = c.parentElement.querySelector('.bm-nic-linux');
        if (!span) return;
        const n = nics.find(x => normMac(x.mac) === normMac(c.value));
        span.textContent = n
          ? `${n.name || ''}${n.state ? ' · ' + n.state : ''}${n.speed ? ' · ' + n.speed + ' Mbps' : ''}` : '';
      });
    }

    // Chemins à effacer d'un fichier importé : cochés sur leur disque.
    function applyPendingWipe() {
      pendingWipe = pendingWipe.filter(p => {
        const d = findDisk(p);
        if (!d) return true;
        const st = stateOf(d);
        if (st.role === 'ignore') st.role = 'wipe';
        else st.wipe = true;
        return false;
      });
    }

    async function loadInventory() {
      let d = null;
      try {
        const r = await fetch(`/api/baremetal/inventory/${encodeURIComponent(host)}`);
        if (r.ok) d = await r.json();
      } catch (e) { d = null; }
      inv = d && Array.isArray(d.disks) ? d : null;
      serverIssues = {};
      // premier inventaire reçu : le tableau remplace la saisie libre
      if (useTable() && !invSeen) {
        invSeen = true;
        freeBox.checked = false;
      }
      applyPendingWipe();
      renderDisks();
      renderNicNames();
      return inv;
    }

    // Démarrage de découverte : action suivie, le tableau se remplit à la fin.
    async function discoverBoot() {
      if (discovering) return;
      if (!creds.password) { alert(tr('bmc.needCreds')); return; }
      if (!confirm(tr('bmc.disk.discoverConfirm', { host }))) return;
      discovering = true;
      discBtn.disabled = true;
      discStatus.textContent = '…';
      const stop = (html) => {
        discovering = false;
        discBtn.disabled = false;
        discStatus.innerHTML = html;
      };
      let d = {};
      try {
        const r = await fetch('/api/baremetal/discover', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bmc_host: host, bmc_user: creds.user, bmc_password: creds.password,
                                 iso: field('iso').value }),
        });
        d = await r.json().catch(() => ({}));
        if (!r.ok) {
          stop(`<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(d.error || r.status)}</span>`);
          return;
        }
      } catch (e) {
        stop(`<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`);
        return;
      }
      const id = d.action_id;
      discStatus.innerHTML = `<span class="spinner-inline"></span> ${esc(tr('bmc.disk.discoverStarted', { id }))}`;
      // Suivi abandonné après 5 échecs de lecture de l'action d'affilée
      // (404, réseau) : l'action reste visible dans le dock.
      let failures = 0;
      const tick = async () => {
        // fenêtre fermée : on cesse de suivre (l'action continue, dans le dock)
        if (!document.body.contains(panel.el)) { discovering = false; return; }
        let a = null;
        try {
          const r = await fetch(`/api/action/${encodeURIComponent(id)}`);
          a = r.ok ? await r.json() : null;
        } catch (e) { a = null; }
        if (!a) {
          failures += 1;
          if (failures >= 5) {
            stop(`<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.disk.discoverLost', { id }))}</span>`);
            msg.innerHTML = discStatus.innerHTML;
            return;
          }
          setTimeout(tick, POLL_MS);
          return;
        }
        failures = 0;
        const st = a.status;
        if (st === 'done') {
          await loadInventory();
          stop(`<span class="bm-imported">${Icons.svg('ok', { size: 14 })} ${esc(tr('bmc.disk.discoverDone'))}</span>`);
          return;
        }
        if (st && !['starting', 'running'].includes(st)) {
          stop(`<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.disk.discoverFailed', { status: st }))}
            ${esc(a.error_summary || '')}</span>`);
          return;
        }
        setTimeout(tick, POLL_MS);
      };
      setTimeout(tick, 1000);
    }

    // Refus du serveur rangés sur les lignes : [chemin ou « serial X », raison].
    function markServerIssues(pairs) {
      serverIssues = {};
      (pairs || []).forEach(([label, code]) => {
        const l = String(label || '');
        const d = findDisk(l) || disks().find(x => (x.serial && l === `serial ${x.serial}`)
                                                   || (x.wwn && l === `WWN ${x.wwn}`));
        if (d) (serverIssues[d.stable_path] = serverIssues[d.stable_path] || []).push(String(code));
      });
      syncDisks();
    }

    disksBox.addEventListener('change', (e) => {
      const row = e.target.closest('tr[data-i]');
      if (!row) return;
      const d = disks()[Number(row.dataset.i)];
      if (!d) return;
      if (e.target.matches('[data-disk-role]')) setRole(d, e.target.value);
      else if (e.target.matches('[data-disk-wipe]')) stateOf(d).wipe = e.target.checked;
      else return;
      delete serverIssues[d.stable_path];
      syncDisks();
    });
    disksBox.addEventListener('input', (e) => {
      if (!e.target.matches('[data-disk-tag]')) return;
      const d = disks()[Number(e.target.closest('tr[data-i]').dataset.i)];
      if (!d) return;
      stateOf(d).tag = e.target.value.trim();
      delete serverIssues[d.stable_path];
      syncDisks();
    });
    poolsBox.addEventListener('input', (e) => {
      const t = e.target.dataset && e.target.dataset.replicas;
      if (t != null) replicas[t] = e.target.value;
    });
    freeBox.addEventListener('change', () => {
      if (freeBox.checked) {
        // les choix du tableau passent dans les champs de saisie libre
        const os = pick('os');
        const dd = pick('data');
        if (os) { field('device').value = os.stable_path; stateOf(os).role = 'ignore'; }
        field('data_disk').value = dd ? dd.stable_path : '';
        if (dd) stateOf(dd).role = 'ignore';
      } else {
        const os = findDisk(field('device').value);
        const dd = findDisk(field('data_disk').value);
        if (os) setRole(os, 'os');
        if (dd && dd !== os) setRole(dd, 'data');
      }
      syncDisks();
    });
    ['device', 'data_disk', 'extra_args'].forEach(n => field(n).addEventListener('input', syncDisks));
    field('wipe_all_disks').addEventListener('change', syncDisks);
    panel.el.querySelector('[data-bm="inv-reload"]').addEventListener('click', () => loadInventory());
    const bmcBtn = panel.el.querySelector('[data-bm="read-bmc"]');
    bmcBtn.addEventListener('click', readBmcStorage);

    // Stockage publié par le BMC (Redfish) : tableau d'information et cadre RAID.
    async function readBmcStorage() {
      if (!creds.password) { alert(tr('bmc.needCreds')); return; }
      bmcBtn.disabled = true;
      discStatus.textContent = '…';
      try {
        const r = await fetch('/api/bmc/storage', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host, user: creds.user, password: creds.password }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) {
          discStatus.innerHTML = `<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(d.error || r.status)}</span>`;
          return;
        }
        redfish = { ...d, at: Date.now() / 1000 };
        const ctrls = d.controllers || [];
        const count = ctrls.reduce((n, c) => n + (c.drives || []).length, 0);
        panel.el.querySelector('#bm-raid-box').innerHTML = raidHtml(d);
        discStatus.innerHTML = ctrls.length
          ? `<span class="bm-imported">${Icons.svg('ok', { size: 14 })} ${esc(tr('bmc.disk.redfishRead', { count }))}</span>`
          : `<span class="bm-disk-hint">${Icons.svg('warn', { size: 12 })} ${esc(tr('bmc.disk.redfishNone'))}</span>`;
        renderDisks();
      } catch (e) {
        discStatus.innerHTML = `<span class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`;
      } finally {
        bmcBtn.disabled = false;
      }
    }
    discBtn.addEventListener('click', discoverBoot);

    // Nom du cluster créé : le nom d'hôte tant qu'il n'a pas été changé.
    let clusterNameEdited = false;
    field('hostname').addEventListener('input', () => {
      if (!clusterNameEdited) field('cluster_name').value = field('hostname').value;
    });
    field('cluster_name').addEventListener('input', () => { clusterNameEdited = true; });

    // En DHCP les trois champs statiques n'ont plus de sens : les cacher
    // ET lever leur `required`, sinon le formulaire refuse de partir sur des
    // champs invisibles, sans dire lesquels. Même règle pour la VIP d'un
    // nœud qui rejoint un cluster (fichier importé en `join`), et pour les
    // options propres à 802.3ad. La politique de hachage vaut aussi pour
    // balance-xor, balance-tlb et balance-alb : le noyau s'en sert dans ces
    // modes, et une valeur importée n'y est pas jetée en silence.
    const method = panel.el.querySelector('#bm-method');
    const bondMode = panel.el.querySelector('#bm-bond-mode');
    const syncAll = () => {
      const stat = method.value === 'static';
      panel.el.querySelectorAll('.bm-static').forEach(l => {
        l.hidden = !stat;
        l.querySelector('input').required = stat;
      });
      const creating = field('mode').value !== 'join';
      panel.el.querySelectorAll('.bm-create').forEach(l => {
        l.hidden = !creating;
        const input = l.querySelector('input');
        if (input) input.required = creating;
      });
      panel.el.querySelectorAll('.bm-create-opt').forEach(l => { l.hidden = !creating; });
      const lacp = bondMode.value === '802.3ad';
      panel.el.querySelectorAll('.bm-lacp').forEach(l => { l.hidden = !lacp; });
      const xmit = XMIT_MODES.includes(bondMode.value);
      panel.el.querySelectorAll('.bm-xmit').forEach(l => { l.hidden = !xmit; });
      for (const name of ['token', 'password']) {
        const input = field(name);
        input.required = !fromFile[name];
        input.placeholder = fromFile[name] ? tr('bmc.fromFile') : '';
      }
    };
    method.addEventListener('change', syncAll);
    bondMode.addEventListener('change', syncAll);
    syncAll();
    renderDisks();
    loadInventory();

    // Corps envoyé à l'aperçu et à l'installation.
    function collect() {
      const fd = new FormData(form);
      fd.delete('mgmt_nic');
      const body = Object.fromEntries(fd.entries());
      body.mgmt_interfaces = [...form.querySelectorAll('[name="mgmt_nic"]:checked')].map(c => c.value);
      body.wipe_all_disks = field('wipe_all_disks').checked;
      if (body.bond_mode !== '802.3ad') delete body.bond_lacp_rate;
      if (!XMIT_MODES.includes(body.bond_mode)) delete body.bond_xmit_hash_policy;
      if (body.mode === 'join') {
        delete body.vip;
        delete body.vip_mode;
        delete body.cluster_name;
      } else {
        delete body.server_url;
        if (!String(body.cluster_name || '').trim()) delete body.cluster_name;
      }
      // Disques choisis dans le tableau : chemins stables ; les pools
      // portent série et WWN, que le runner retrouve après l'installation.
      if (useTable()) {
        if (!freeBox.checked) {
          body.device = (pick('os') || {}).stable_path || '';
          body.data_disk = (pick('data') || {}).stable_path || '';
        }
        // « Effacer » coché sur le disque système ou de données est envoyé :
        // il lève le contrôle `has-data` du serveur, qui retire ensuite ces
        // deux disques de la liste rendue (l'installeur les formate lui-même
        // et vide une liste qui les contient)
        const wipe = disks().filter(d => {
          const st = stateOf(d);
          return st.role === 'wipe' || (['os', 'data', 'pool'].includes(st.role) && st.wipe);
        }).map(d => d.stable_path);
        const extra = pendingWipe.filter(p => !wipe.includes(p));
        if (wipe.length || extra.length) body.wipe_disks_list = wipe.concat(extra);
        const g = poolGroups();
        const pools = Object.keys(g).sort().map(t => ({
          tag: t,
          replicas: Math.max(1, parseInt(replicas[t], 10) || 1),
          disks: g[t].map(d => ({ serial: d.serial || '', wwn: d.wwn || '', path: d.stable_path })),
        }));
        if (pools.length) body.pools = pools;
      } else if (pendingWipe.length) {
        body.wipe_disks_list = pendingWipe.slice();
      }
      if (importId) body.import_id = importId;
      return body;
    }

    function setSelect(sel, value) {
      const v = String(value);
      if (![...sel.options].some(o => o.value === v)) {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = v;
        sel.appendChild(o);
      }
      sel.value = v;
      // l'option « non renseigné » n'apparaît que si un import l'a demandée
      const unset = sel.querySelector('option[data-unset]');
      if (unset && v === '') unset.hidden = false;
    }

    // Cartes du fichier : une MAC découverte est cochée ; un nom (ens1f0...)
    // ou une MAC inconnue de Redfish est ajouté, coché, pour être gardé.
    function setNics(values) {
      const box = panel.el.querySelector('.bm-nics');
      box.querySelectorAll('.bm-nic-extra').forEach(e => e.remove());
      const boxes = [...box.querySelectorAll('[name="mgmt_nic"]')];
      boxes.forEach(c => { c.checked = false; });
      (values || []).forEach(v => {
        const hit = boxes.find(c => normMac(c.value) === normMac(v));
        if (hit) { hit.checked = true; return; }
        const l = document.createElement('label');
        l.className = 'bm-check bm-nic-extra';
        l.innerHTML = `<input type="checkbox" name="mgmt_nic" value="${esc(v)}" checked>
          <code>${esc(v)}</code> <span class="form-hint">${esc(tr('bmc.f.nicFromFile'))}</span>`;
        box.appendChild(l);
      });
    }

    function fillFromImport(d) {
      const f = d.form || {};
      IMPORT_CLEARED.forEach(k => { const el = field(k); if (el) el.value = ''; });
      field('wipe_all_disks').checked = false;
      field('mode').value = 'create';
      for (const [k, v] of Object.entries(f)) {
        if (k === 'mgmt_interfaces') { setNics(v); continue; }
        const el = field(k);
        if (!el) continue;
        if (el.type === 'checkbox') el.checked = !!v;
        else if (el.tagName === 'SELECT') setSelect(el, v == null ? '' : v);
        else el.value = v == null ? '' : String(v);
      }
      field('advanced_yaml').value = d.advanced || '';
      if (!clusterNameEdited) field('cluster_name').value = field('hostname').value;
      importDisks(f);
      importId = d.import_id || null;
      fromFile.token = !!d.has_token;
      fromFile.password = !!d.has_password;
      // un secret saisi avant l'import céderait la place au fichier : le vider
      if (fromFile.token) field('token').value = '';
      if (fromFile.password) field('password').value = '';
      syncAll();
    }

    // Disques du fichier placés dans le tableau quand l'inventaire les
    // connaît ; sinon saisie libre.
    function importDisks(f) {
      disks().forEach(d => { roles[d.stable_path] = { role: 'ignore', tag: '', wipe: false }; });
      serverIssues = {};
      pendingWipe = Array.isArray(f.wipe_disks_list) ? f.wipe_disks_list.map(String) : [];
      if (useTable() && f.device) {
        const os = findDisk(f.device);
        const dd = f.data_disk ? findDisk(f.data_disk) : null;
        if (os && (!f.data_disk || (dd && dd !== os))) {
          setRole(os, 'os');
          if (dd) setRole(dd, 'data');
          freeBox.checked = false;
        } else {
          freeBox.checked = true;
        }
      }
      applyPendingWipe();
      syncDisks();
    }

    async function importText(name, text) {
      msg.innerHTML = '…';
      try {
        const r = await fetch('/api/baremetal/config/parse', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, bmc_host: host }),
        });
        const d = await r.json().catch(() => ({}));
        if (r.status === 413) {
          msg.innerHTML = `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.importTooLarge', { max: Math.round((d.max_bytes || IMPORT_MAX) / 1024) }))}</div>`;
          return;
        }
        if (!r.ok) {
          msg.innerHTML = refusalHtml(tr('bmc.importRefused'), d.fields || d.errors || [], {});
          return;
        }
        fillFromImport(d);
        const notes = (d.notes || []).map(c => `<li>${esc(noteText(c))}</li>`).join('');
        msg.innerHTML = `<div class="bm-imported">${Icons.svg('ok', { size: 14 })} ${esc(tr('bmc.imported', { name }))}
          ${notes ? `<ul class="bm-notes">${notes}</ul>` : ''}</div>`;
      } catch (e) {
        msg.innerHTML = `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</div>`;
      }
    }

    const fileInput = panel.el.querySelector('[data-bm="file"]');
    panel.el.querySelector('[data-bm="import"]').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      if (file.size > IMPORT_MAX) {
        msg.innerHTML = `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.importTooLarge', { max: IMPORT_MAX / 1024 }))}</div>`;
        return;
      }
      const reader = new FileReader();
      reader.onload = () => importText(file.name, String(reader.result || ''));
      reader.onerror = () => {
        msg.innerHTML = `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(file.name)}</div>`;
      };
      reader.readAsText(file);
    });

    // Refus commun à l'aperçu et à l'installation.
    function refusalFor(d, status) {
      // refus par disque : [disque, raison] (pools) ou chemin -> raison
      // (« invalid disks », contrôles des rôles avec l'inventaire)
      if (Array.isArray(d.reasons) || d.error === 'invalid disks') {
        const pairs = Array.isArray(d.reasons) ? d.reasons
          : (d.fields || Object.keys(d.reasons || {})).map(p => [p, (d.reasons || {})[p]]);
        markServerIssues(pairs);
        const items = pairs.map(([p, c]) => `<li>${p ? `<code>${esc(p)}</code> : ` : ''}${esc(reasonText(c))}</li>`).join('');
        return `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.disk.refused'))}
          <ul class="bm-paths">${items}</ul></div>`;
      }
      if (d.error === 'invalid configuration') {
        return refusalHtml(tr('bmc.refused'), d.fields || [], d.reasons || {});
      }
      if (d.error === 'import expired') {
        importId = null;
        fromFile.token = fromFile.password = false;
        syncAll();
        return `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.importExpired'))}</div>`;
      }
      return `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(d.error || status)} ${esc((d.fields || []).join(', '))}</div>`;
    }

    panel.el.querySelector('[data-bm="preview"]').addEventListener('click', async () => {
      msg.innerHTML = '…';
      try {
        const r = await fetch('/api/baremetal/config/preview', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          // l'hôte permet au serveur de contrôler les disques sur l'inventaire
          body: JSON.stringify({ ...collect(), bmc_host: host }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { msg.innerHTML = refusalFor(d, r.status); return; }
        msg.innerHTML = '';
        const pv = FloatingPanels.open({
          id: `bm-preview-${host}`,
          title: `${tr('bmc.previewTitle')} · ${host}`,
          width: 640, height: Math.max(360, Math.min(720, window.innerHeight - 120)),
          bodyHtml: '',
        });
        pv.setBody(`<textarea class="adv-code bm-preview-text tip" readonly spellcheck="false"
          data-tip="${esc(tr('bmc.tip.previewText'))}"></textarea>`);
        // .value, jamais innerHTML : le YAML reste du texte
        pv.body.querySelector('.bm-preview-text').value = d.yaml || '';
      } catch (e) {
        msg.innerHTML = `<div class="bm-refusal">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</div>`;
      }
    });

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const body = collect();
      const res = panel.el.querySelector('#bm-install-result');
      if (!body.mgmt_interfaces.length) {
        res.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(tr('bmc.needNic'))}</span>`;
        return;
      }
      if (!body.device) {
        res.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(reasonText('no-os'))}</span>`;
        return;
      }
      body.bmc_host = host;
      body.bmc_user = creds.user;
      body.bmc_password = creds.password;
      if (!body.bmc_password) { alert(tr('bmc.needCreds')); return; }
      if (!confirm(tr('bmc.confirmInstall', { host, device: body.device }))) return;
      res.textContent = '…';
      try {
        const r = await fetch('/api/baremetal/install', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          res.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('bmc.started'))} ${esc(d.action_id)}</span>`;
        } else {
          msg.innerHTML = refusalFor(d, r.status);
          res.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(d.error || r.status)}</span>`;
          msg.scrollIntoView({ block: 'nearest' });
        }
      } catch (e) {
        res.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`;
      }
    });
  }

  async function fetchIso(ev) {
    ev.preventDefault();
    const fd = new FormData(ev.target);
    try {
      const r = await fetch('/api/iso/fetch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: fd.get('url') }),
      });
      const d = await r.json();
      alert(r.ok ? `${tr('bmc.iso.started')} ${d.action_id}` : (d.error || r.status));
      if (r.ok) setTimeout(renderIsoStore, 1500);
    } catch (e) { alert(e.message); }
  }

  async function deleteIso(name) {
    if (!confirm(tr('bmc.iso.confirmDelete', { name }))) return;
    await fetch(`/api/iso/${encodeURIComponent(name)}`, { method: 'DELETE' });
    renderIsoStore();
  }

  async function power(host, action) {
    if (!creds.password) { alert(tr('bmc.needCreds')); return; }
    if (!confirm(tr('bmc.confirmPower', { action, host }))) return;
    try {
      const r = await fetch(`/api/bmc/${encodeURIComponent(host)}/power`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, ...creds }),
      });
      const d = await r.json();
      alert(r.ok ? `${tr('bmc.dispatched')} ${d.action_id}` : (d.error || 'error'));
    } catch (e) { alert(e.message); }
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-bmc-refresh')) render(true);
      const p = e.target.closest('.bmc-power');
      if (p) { e.preventDefault(); power(p.dataset.host, p.dataset.action); }
      const i = e.target.closest('.bmc-install');
      if (i) { e.preventDefault(); openInstall(i.dataset.host); }
      const del = e.target.closest('.bmc-iso-del');
      if (del) { e.preventDefault(); deleteIso(del.dataset.name); }
      // Viser le LIEN de navigation, pas n'importe quel `data-subtab="pxe"` :
      // le panneau de contenu porte le même attribut, si bien qu'un simple
      // clic sur un bouton de l'onglet reconstruisait tout 50 ms plus tard
      // et effaçait le résultat de la découverte qui venait de s'afficher.
      if (e.target.closest('#tab-automation .sub-tab[data-subtab="pxe"]')
          || e.target.closest('.tab-child[data-subtab="pxe"]')) {
        setTimeout(render, 50);
      }
    });
    document.addEventListener('submit', (e) => {
      if (e.target?.id === 'bmc-discover-form') discover(e);
      if (e.target?.id === 'bmc-iso-form') fetchIso(e);
    });
  }

  return { init, render };
})();

document.addEventListener('DOMContentLoaded', BMC.init);
window.BMC = BMC;
