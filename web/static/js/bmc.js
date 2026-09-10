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
        <div class="card-header"><h2>💿 ${esc(tr('bmc.iso.title'))}</h2>
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
        <div class="card-header"><h2>🔍 ${esc(tr('bmc.discovery'))}</h2></div>
        <div class="card-body">
          <form id="bmc-discover-form" class="capi-form">
            <fieldset>
              <legend>${esc(tr('bmc.targets'))}</legend>
              <label style="grid-column:1/-1;">${esc(tr('bmc.hosts'))} *
                <textarea name="hosts" rows="2" required
                          placeholder="172.16.1.33, 172.16.1.34"></textarea></label>
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
  async function openInstall(host) {
    const node = lastNodes.find(n => n.host === host) || {};
    let isos = [];
    try { isos = (await fetch('/api/isos').then(r => r.json())).isos || []; } catch (e) { /* vide */ }
    if (!isos.length) { alert(tr('bmc.needIso')); return; }
    // La valeur est la MAC, pas le nom Redfish : sur ces machines les deux
    // cartes s'appellent « System Ethernet Interface », et de toute façon
    // Redfish ignore le nom que Linux donnera à l'interface.
    const nicOpts = (node.nics || []).map((n, k) => {
      const state = n.status ? ' · ' + esc(n.status) : '';
      return `<option value="${esc(n.mac)}">NIC ${k + 1} · ${esc(n.mac)}${state}</option>`;
    }).join('');
    const isoOpts = isos.map(i => `<option value="${esc(i.name)}">${esc(i.name)}</option>`).join('');

    const panel = FloatingPanels.open({
      id: `bm-install-${host}`,
      title: `${tr('bmc.install')} · ${host}`,
      width: 620, height: 640,
      bodyHtml: `
        <form id="bm-install-form" class="capi-form" style="padding:14px;">
          <p class="form-hint vm-edit-unverified">${esc(tr('bmc.installWarn'))}</p>
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
            <label>${esc(tr('bmc.f.device'))} *
              <input name="device" required value="/dev/sda"></label>
            <label>${esc(tr('bmc.f.mgmt'))} *
              <select name="mgmt_interface" required>${nicOpts}</select></label>
            <label>${esc(tr('bmc.f.method'))}
              <select name="method" id="bm-method">
                <option value="static">static</option><option value="dhcp">dhcp</option></select></label>
            <label class="bm-static">${esc(tr('bmc.f.ip'))} *
              <input name="ip" required placeholder="192.0.2.10"></label>
            <label class="bm-static">${esc(tr('bmc.f.mask'))} *
              <input name="subnet_mask" required value="255.255.255.0"></label>
            <label class="bm-static">${esc(tr('bmc.f.gateway'))} *
              <input name="gateway" required placeholder="192.0.2.1"></label>
            <label>${esc(tr('bmc.f.vip'))} *<input name="vip" required placeholder="192.0.2.100"></label>
            <label>${esc(tr('bmc.f.dns'))}<input name="dns" placeholder="9.9.9.9, 1.1.1.1"></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.access'))}</legend>
            <label>${esc(tr('bmc.f.token'))} *<input name="token" type="password" required></label>
            <label>${esc(tr('bmc.f.ospw'))} *<input name="password" type="password" required></label>
            <label style="grid-column:1/-1;">${esc(tr('bmc.f.sshkeys'))}
              <textarea name="ssh_keys" rows="2" placeholder="ssh-ed25519 AAAA..."></textarea></label>
          </fieldset>
          <fieldset>
            <legend>${esc(tr('bmc.fs.advanced'))}</legend>
            <label style="grid-column:1/-1;">${esc(tr('bmc.f.extraArgs'))}
              <input name="extra_args" placeholder="console=ttyS1,115200 harvester.install.skipchecks=true">
              <span class="form-hint">${esc(tr('bmc.f.extraArgsHint'))}</span></label>
          </fieldset>
          <div class="apply-bar">
            <button type="submit" class="btn btn-primary btn-sm btn-ico tip"
                    data-tip="${esc(tr('bmc.installTip'))}">${Icons.svg('install')} ${esc(tr('bmc.installGo'))}</button>
            <span class="apply-result" id="bm-install-result"></span>
          </div>
        </form>`,
    });

    // En DHCP les trois champs statiques n'ont plus de sens : les cacher
    // ET lever leur `required`, sinon le formulaire refuse de partir sur des
    // champs invisibles, sans dire lesquels.
    const method = panel.el.querySelector('#bm-method');
    const syncMethod = () => {
      const stat = method.value === 'static';
      panel.el.querySelectorAll('.bm-static').forEach(l => {
        l.hidden = !stat;
        l.querySelector('input').required = stat;
      });
    };
    method.addEventListener('change', syncMethod);
    syncMethod();

    panel.el.querySelector('#bm-install-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const body = Object.fromEntries(fd.entries());
      body.bmc_host = host;
      body.bmc_user = creds.user;
      body.bmc_password = creds.password;
      if (!body.bmc_password) { alert(tr('bmc.needCreds')); return; }
      if (!confirm(tr('bmc.confirmInstall', { host, device: body.device }))) return;
      const res = panel.el.querySelector('#bm-install-result');
      res.textContent = '…';
      try {
        const r = await fetch('/api/baremetal/install', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const d = await r.json();
        res.innerHTML = r.ok
          ? `<span style="color:var(--accent)">✓ ${esc(tr('bmc.started'))} ${esc(d.action_id)}</span>`
          : `<span style="color:var(--danger)">✗ ${esc(d.error || r.status)} ${esc((d.fields || []).join(', '))}</span>`;
      } catch (e) {
        res.innerHTML = `<span style="color:var(--danger)">✗ ${esc(e.message)}</span>`;
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
