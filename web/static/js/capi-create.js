/**
 * harvester-ops — créer un cluster RKE2 par Cluster API (v1.48.0).
 *
 * Demandé par l'exploitant : un maximum de choix proposés (listes tirées du
 * cluster), une explication au survol de chaque élément, l'essentiel
 * regroupé, les calculs d'adresses suggérés, toutes les options du
 * fournisseur mais les étendues repliées. Le serveur ne fait que lancer
 * `harvester-capi` (relevés, contrôle, aperçu, création) : ce module compose
 * la demande et rend les réponses dans la langue de l'interface.
 */
const CapiCreate = (() => {
  const esc = (v) => String(v == null ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const tr = (k, vars) => i18n.t(k, vars);
  const enc = encodeURIComponent;
  const bytes = (n) => (window.XferProgress ? XferProgress.bytes(n) : `${n} B`);
  const GIB = 1024 ** 3;

  const PRESETS = {
    small: { cpu: 2, memory: '4Gi' },
    medium: { cpu: 4, memory: '8Gi' },
    large: { cpu: 8, memory: '16Gi' },
  };

  // Un appel littéral par constat : le contrôle de parité des traductions
  // ne voit que ces formes.
  const FINDINGS = {
    'stack-not-ready': (v) => tr('capi.new.fd.stack-not-ready', v),
    'stack-legacy': (v) => tr('capi.new.fd.stack-legacy', v),
    'invalid': (v) => tr('capi.new.fd.invalid', v),
    'name-taken': (v) => tr('capi.new.fd.name-taken', v),
    'namespace-exists': (v) => tr('capi.new.fd.namespace-exists', v),
    'namespace-has-cluster': (v) => tr('capi.new.fd.namespace-has-cluster', v),
    'image-missing': (v) => tr('capi.new.fd.image-missing', v),
    'image-iso': (v) => tr('capi.new.fd.image-iso', v),
    'image-not-ready': (v) => tr('capi.new.fd.image-not-ready', v),
    'image-sles-repos': (v) => tr('capi.new.fd.image-sles-repos', v),
    'keypair-missing': (v) => tr('capi.new.fd.keypair-missing', v),
    'network-missing': (v) => tr('capi.new.fd.network-missing', v),
    'storage-class-missing': (v) => tr('capi.new.fd.storage-class-missing', v),
    'pool-missing': (v) => tr('capi.new.fd.pool-missing', v),
    'pool-short': (v) => tr('capi.new.fd.pool-short', v),
    'gateway-differs': (v) => tr('capi.new.fd.gateway-differs', v),
    'mask-differs': (v) => tr('capi.new.fd.mask-differs', v),
    'cidr-overlap': (v) => tr('capi.new.fd.cidr-overlap', v),
    'cp-even': (v) => tr('capi.new.fd.cp-even', v),
    'cp-single': (v) => tr('capi.new.fd.cp-single', v),
    'small-nodes': (v) => tr('capi.new.fd.small-nodes', v),
    'version-untested': (v) => tr('capi.new.fd.version-untested', v),
    'cni-tuning-ignored': (v) => tr('capi.new.fd.cni-tuning-ignored', v),
    'cpu-short': (v) => tr('capi.new.fd.cpu-short', v),
    'memory-short': (v) => tr('capi.new.fd.memory-short', v),
    'endpoint-dhcp': (v) => tr('capi.new.fd.endpoint-dhcp', v),
    'cluster-unreachable': (v) => tr('capi.new.fd.cluster-unreachable', v),
  };
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const LEVEL_RANK = { block: 0, warn: 1, ok: 2 };

  function findingText(f) {
    const v = Object.assign({}, f.facts || {});
    ['needed', 'free'].forEach(k => {
      if (f.code === 'memory-short' && k in v) v[k] = bytes(v[k]);
    });
    const fn = FINDINGS[f.code];
    return fn ? fn(v) : f.code;
  }

  function opts(list, selected, label = (v) => v, value = (v) => v) {
    return list.map(v => `<option value="${esc(value(v))}" ${value(v) === selected ? 'selected' : ''}>${esc(label(v))}</option>`).join('');
  }

  function poolLabel(p) {
    const ranges = (p.ranges || []).map(r => (r.start && r.end ? `${r.start}-${r.end.split('.').pop()}` : r.subnet)).join(', ');
    return tr('capi.new.poolOption', { name: p.name, ranges, free: p.available });
  }

  // ---------------------------------------------------------------------
  // Le formulaire
  // ---------------------------------------------------------------------
  function formHtml(inv, cluster) {
    const images = (inv.images || []).filter(i => !i.iso && i.ready);
    const keys = inv.keypairs || [];
    const nets = inv.networks || [];
    const pools = inv.pools || [];
    const scs = inv.storage_classes || [];
    const versions = inv.versions || [];
    const pool0 = pools[0] || {};
    const lastImage = (() => { try { return localStorage.getItem('harvester_ops_capi_image') || ''; } catch { return ''; } })();
    const img0 = images.find(i => i.ref === lastImage) || images[0] || {};
    const user0 = (inv.ssh_users || {})[img0.os] || 'sles';
    const verOpts = versions.map(v => (typeof v === 'string' ? { version: v } : v));
    const dns0 = (() => { try { return localStorage.getItem('harvester_ops_capi_dns') || ''; } catch { return ''; } })();
    return `
      <div class="tf-form capi-create" data-cluster="${esc(cluster)}">
        <p class="tf-desc">${esc(tr('capi.new.intro'))}</p>
        <div class="capi-stack-note" data-x="stack-note"></div>

        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.essentials'))}</legend>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.name'))}</span>
              <input data-x="name" type="text" placeholder="web-prod" autocomplete="off" spellcheck="false"
                     class="tip" data-tip="${esc(tr('capi.new.t.name'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.version'))}</span>
              <select data-x="k8s_version" class="tip" data-tip="${esc(tr('capi.new.t.version'))}">
                ${opts(verOpts, inv.default_version, v => v.tested ? `${v.version} (${tr('capi.new.tested')})` : v.version, v => v.version)}
              </select></label>
          </div>
        </fieldset>

        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.size'))}</legend>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cp'))}</span>
              <select data-x="cp_replicas" class="tip" data-tip="${esc(tr('capi.new.t.cp'))}">
                <option value="1">${esc(tr('capi.new.cp1'))}</option>
                <option value="3">${esc(tr('capi.new.cp3'))}</option>
                <option value="5">${esc(tr('capi.new.cp5'))}</option>
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.workers'))}</span>
              <input data-x="worker_replicas" type="number" min="0" max="50" value="1"
                     class="tip" data-tip="${esc(tr('capi.new.t.workers'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.preset'))}</span>
              <select data-x="preset" class="tip" data-tip="${esc(tr('capi.new.t.preset'))}">
                <option value="small">${esc(tr('capi.new.p.small'))}</option>
                <option value="medium">${esc(tr('capi.new.p.medium'))}</option>
                <option value="large">${esc(tr('capi.new.p.large'))}</option>
                <option value="custom">${esc(tr('capi.new.p.custom'))}</option>
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cpu'))}</span>
              <input data-x="cpu" type="number" min="1" max="64" value="2" class="tip" data-tip="${esc(tr('capi.new.t.cpu'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.memory'))}</span>
              <input data-x="memory" type="text" value="4Gi" list="capi-mem-sizes" class="tip" data-tip="${esc(tr('capi.new.t.memory'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.disk'))}</span>
              <input data-x="disk_size" type="text" value="40Gi" list="capi-disk-sizes" class="tip" data-tip="${esc(tr('capi.new.t.disk'))}"></label>
          </div>
          <datalist id="capi-mem-sizes">${['4Gi', '8Gi', '12Gi', '16Gi', '32Gi'].map(v => `<option value="${v}">`).join('')}</datalist>
          <datalist id="capi-disk-sizes">${['40Gi', '60Gi', '80Gi', '120Gi', '200Gi'].map(v => `<option value="${v}">`).join('')}</datalist>
        </fieldset>

        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.os'))}</legend>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.image'))}</span>
              <select data-x="image" class="tip" data-tip="${esc(tr('capi.new.t.image'))}">
                ${images.length ? opts(images, img0.ref, i => i.display_name + (i.virtual_size ? ` (${bytes(i.virtual_size)})` : ''), i => i.ref)
                                : `<option value="">${esc(tr('capi.new.noImage'))}</option>`}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.sshUser'))}</span>
              <input data-x="ssh_user" type="text" value="${esc(user0)}" class="tip" data-tip="${esc(tr('capi.new.t.sshUser'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.keypair'))}</span>
              <select data-x="ssh_keypair" class="tip" data-tip="${esc(tr('capi.new.t.keypair'))}">
                ${keys.length ? opts(keys, keys[0]) : `<option value="">${esc(tr('capi.new.noKeypair'))}</option>`}
              </select></label>
          </div>
        </fieldset>

        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.network'))}</legend>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.network'))}</span>
              <select data-x="network" class="tip" data-tip="${esc(tr('capi.new.t.network'))}">
                ${opts(nets, (nets.find(n => /production/.test(n.ref)) || nets[0] || {}).ref,
                       n => n.ref + (n.vlan ? ` (${tr('capi.new.vlan', { vlan: n.vlan })})` : ''), n => n.ref)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.pool'))}</span>
              <select data-x="ip_pool" class="tip" data-tip="${esc(tr('capi.new.t.pool'))}">
                ${opts(pools, pool0.name, poolLabel, p => p.name)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.gateway'))} <span class="capi-derived" data-x="gw-derived">${esc(tr('capi.new.fromPool'))}</span></span>
              <input data-x="gateway" type="text" value="${esc(pool0.gateway || '')}" class="tip" data-tip="${esc(tr('capi.new.t.gateway'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.mask'))} <span class="capi-derived" data-x="mask-derived">${esc(tr('capi.new.fromPool'))}</span></span>
              <input data-x="subnet_mask" type="text" value="${esc(pool0.mask || '')}" class="tip" data-tip="${esc(tr('capi.new.t.mask'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.dns'))}</span>
              <input data-x="dns" type="text" value="${esc(dns0)}" list="capi-dns" placeholder="${esc(pool0.gateway || '8.8.8.8')}"
                     class="tip" data-tip="${esc(tr('capi.new.t.dns'))}"></label>
          </div>
          <datalist id="capi-dns">${[pool0.gateway, dns0].filter(Boolean).map(v => `<option value="${esc(v)}">`).join('')}</datalist>
          <p class="tf-desc capi-summary" data-x="summary"></p>
        </fieldset>

        <details class="tf-block capi-advanced" data-x="advanced">
          <summary class="tip" data-tip="${esc(tr('capi.new.t.advanced'))}">${esc(tr('capi.new.sec.advanced'))}</summary>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.namespace'))}</span>
              <input data-x="namespace" type="text" placeholder="${esc(tr('capi.new.sameAsName'))}" class="tip" data-tip="${esc(tr('capi.new.t.namespace'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.targetNs'))}</span>
              <select data-x="target_namespace" class="tip" data-tip="${esc(tr('capi.new.t.targetNs'))}">
                ${opts((inv.namespaces || []).filter(n => !/^(cattle-|kube-|harvester-|longhorn-|fleet-|rke2-|caphv-|capi-|cert-manager)/.test(n) || n === 'default'), 'default')}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraPools'))}</span>
              <select data-x="ip_pool_refs" multiple size="2" class="tip" data-tip="${esc(tr('capi.new.t.extraPools'))}">
                ${opts(pools, '', poolLabel, p => p.name)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraNets'))}</span>
              <select data-x="extra_networks" multiple size="2" class="tip" data-tip="${esc(tr('capi.new.t.extraNets'))}">
                ${opts(nets, '', n => n.ref, n => n.ref)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraDisk'))}</span>
              <input data-x="extra_disk_size" type="text" placeholder="20Gi" class="tip" data-tip="${esc(tr('capi.new.t.extraDisk'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraDiskClass'))}</span>
              <select data-x="extra_disk_class" class="tip" data-tip="${esc(tr('capi.new.t.extraDiskClass'))}">
                ${opts(scs, (scs.find(s => s.default) || scs[0] || {}).name, s => s.name + (s.default ? ' *' : ''), s => s.name)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cni'))}</span>
              <select data-x="cni" class="tip" data-tip="${esc(tr('capi.new.t.cni'))}">
                ${opts(['calico', 'canal', 'cilium', 'none'], 'calico')}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.podCidr'))}</span>
              <input data-x="pod_cidr" type="text" value="10.42.0.0/16" class="tip" data-tip="${esc(tr('capi.new.t.podCidr'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.svcCidr'))}</span>
              <input data-x="service_cidr" type="text" value="10.43.0.0/16" class="tip" data-tip="${esc(tr('capi.new.t.svcCidr'))}"></label>
            <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('capi.new.t.rancher'))}"><input data-x="rancher_import" type="checkbox"><span class="tf-label">${esc(tr('capi.new.f.rancher'))}</span></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.fleetRepo'))}</span>
              <input data-x="fleet_repo" type="text" placeholder="https://" class="tip" data-tip="${esc(tr('capi.new.t.fleetRepo'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.fleetBranch'))}</span>
              <input data-x="fleet_branch" type="text" value="main" class="tip" data-tip="${esc(tr('capi.new.t.fleetBranch'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.mtu'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <input data-x="cni_mtu" type="number" min="576" max="9000" value="1500" class="tip" data-tip="${esc(tr('capi.new.t.mtu'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.encap'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <select data-x="cni_encapsulation" class="tip" data-tip="${esc(tr('capi.new.t.encap'))}">
                ${opts(['VXLANCrossSubnet', 'VXLAN', 'IPIP', 'IPIPCrossSubnet', 'None'], 'VXLANCrossSubnet')}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.bgp'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <select data-x="cni_bgp" class="tip" data-tip="${esc(tr('capi.new.t.bgp'))}">
                ${opts(['Disabled', 'Enabled'], 'Disabled')}
              </select></label>
          </div>
        </details>

        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.check'))}</legend>
          <div class="xfer-report" data-x="report"></div></fieldset>
        <div class="apply-bar" style="margin:0; padding:0; border:0;">
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="check" data-tip="${esc(tr('capi.new.t.check'))}">${Icons.svg('refresh')} <span>${esc(tr('capi.new.check'))}</span></button>
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="preview" data-tip="${esc(tr('capi.new.t.preview'))}">${Icons.svg('preview')} <span>${esc(tr('capi.new.preview'))}</span></button>
          <button type="button" class="btn btn-primary btn-sm tip" data-x="create" data-tip="${esc(tr('capi.new.t.create'))}" disabled>${Icons.svg('capi')} <span>${esc(tr('capi.new.create'))}</span></button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
        <fieldset class="tf-block xfer-live" data-x="live" hidden>
          <legend>${esc(tr('capi.new.sec.progress'))}</legend>
          <div class="xfer-live-line" data-x="live-line">${esc(tr('capi.new.waiting'))}</div>
          <div class="progress-mini xfer-live-bar"><div class="fill" data-x="live-bar" style="width:0%"></div></div>
          <div class="tf-desc" data-x="live-meta"></div>
          <div class="xfer-archive" data-x="done-box" hidden></div>
        </fieldset>
      </div>`;
  }

  // ---------------------------------------------------------------------
  // Comportement
  // ---------------------------------------------------------------------
  async function render(container, cluster) {
    container.innerHTML = `<p class="tf-desc">${esc(tr('capi.new.loading'))}</p>`;
    let inv;
    try {
      const r = await fetch(`/api/capi/${enc(cluster)}/inventory`);
      inv = await r.json();
      if (!r.ok) throw new Error(inv.error || `HTTP ${r.status}`);
    } catch (e) {
      container.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('capi.new.error', { msg: e.message }))}</div></div>`;
      return;
    }
    if (inv.unreachable) {
      container.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('capi.new.unreachable'))}</div></div>`;
      return;
    }
    container.innerHTML = formHtml(inv, cluster);
    wire(container, cluster, inv);
  }

  function wire(root, cluster, inv) {
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    const state = { seq: 0, running: false, timer: null, last: null };
    const poolByName = Object.fromEntries((inv.pools || []).map(p => [p.name, p]));
    const imageByRef = Object.fromEntries((inv.images || []).map(i => [i.ref, i]));

    stackNote(q('stack-note'), inv.stack);

    function body() {
      const b = {};
      root.querySelectorAll('[data-x]').forEach(el => {
        const x = el.dataset.x;
        if (!el.matches('input, select') || x === 'preset') return;
        if (el.type === 'checkbox') b[x] = el.checked;
        else if (el.multiple) b[x] = [...el.selectedOptions].map(o => o.value).filter(Boolean);
        else if (el.value !== '') b[x] = el.value;
      });
      if (!b.extra_disk_size) delete b.extra_disk_class;
      (b.ip_pool_refs || []).length || delete b.ip_pool_refs;
      if (b.ip_pool_refs && b.ip_pool && !b.ip_pool_refs.includes(b.ip_pool)) b.ip_pool_refs.unshift(b.ip_pool);
      (b.extra_networks || []).length || delete b.extra_networks;
      if (b.extra_networks) b.extra_networks = b.extra_networks.filter(n => n !== b.network);
      return b;
    }

    function summary() {
      const b = body();
      const vms = Number(b.cp_replicas || 1) + Number(b.worker_replicas || 0);
      const mem = parseQty(b.memory) * vms;
      const disk = (parseQty(b.disk_size) + parseQty(b.extra_disk_size)) * vms;
      const free = (poolByName[b.ip_pool] || {}).available;
      q('summary').textContent = tr('capi.new.summary', {
        vms, cpu: vms * Number(b.cpu || 0), memory: bytes(mem), disk: bytes(disk),
        addresses: vms + 1, free: free == null ? '?' : free,
      });
    }

    // Le pool choisi donne passerelle et masque ; ils restent modifiables,
    // et l'indication « déduit du pool » disparaît dès qu'on les change.
    function applyPool() {
      const p = poolByName[q('ip_pool').value] || {};
      if (p.gateway) { q('gateway').value = p.gateway; q('gw-derived').hidden = false; }
      if (p.mask) { q('subnet_mask').value = p.mask; q('mask-derived').hidden = false; }
      if (!q('dns').value && p.gateway) q('dns').placeholder = p.gateway;
    }
    q('gateway').addEventListener('input', () => { q('gw-derived').hidden = true; });
    q('subnet_mask').addEventListener('input', () => { q('mask-derived').hidden = true; });
    q('ip_pool').addEventListener('change', applyPool);

    q('image').addEventListener('change', () => {
      try { localStorage.setItem('harvester_ops_capi_image', q('image').value); } catch { /* sans stockage */ }
      const os = (imageByRef[q('image').value] || {}).os;
      const u = (inv.ssh_users || {})[os];
      if (u) q('ssh_user').value = u;
    });
    q('preset').addEventListener('change', () => {
      const p = PRESETS[q('preset').value];
      if (p) { q('cpu').value = p.cpu; q('memory').value = p.memory; }
    });
    ['cpu', 'memory'].forEach(x => q(x).addEventListener('input', () => { q('preset').value = 'custom'; }));
    q('name').addEventListener('input', () => {
      q('name').value = q('name').value.toLowerCase().replace(/[^a-z0-9-]/g, '-');
    });
    q('dns').addEventListener('change', () => {
      try { localStorage.setItem('harvester_ops_capi_dns', q('dns').value); } catch { /* sans stockage */ }
    });

    async function check() {
      const seq = ++state.seq;
      const report = q('report');
      if (report.querySelector('.sto-finding')) report.classList.add('is-checking');
      else report.innerHTML = `<p class="tf-desc">${esc(tr('capi.new.checking'))}</p>`;
      const b = body();
      if (!b.name) {
        report.classList.remove('is-checking');
        report.innerHTML = `<p class="tf-desc">${esc(tr('capi.new.needName'))}</p>`;
        q('create').disabled = true;
        return;
      }
      try {
        const r = await fetch(`/api/capi/${enc(cluster)}/cluster-check`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
        const d = await r.json();
        if (seq !== state.seq) return;
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        const items = (d.findings || []).slice().sort((a, c) => LEVEL_RANK[a.level] - LEVEL_RANK[c.level]);
        const list = items.map(f => `
          <div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}" data-level="${esc(f.level)}">
            <div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('');
        const ok = d.blocked ? '' : `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('capi.new.noBlocker'))}</div></div>`;
        report.innerHTML = list + ok;
        report.classList.remove('is-checking');
        q('create').disabled = !!d.blocked || state.running;
      } catch (e) {
        if (seq !== state.seq) return;
        report.classList.remove('is-checking');
        report.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('capi.new.error', { msg: e.message }))}</div></div>`;
      }
    }

    function later() {
      summary();
      clearTimeout(state.timer);
      state.timer = setTimeout(check, 600);
    }
    root.querySelectorAll('input[data-x], select[data-x]').forEach(el => {
      el.addEventListener('change', later);
      if (el.type === 'text' || el.type === 'number') el.addEventListener('input', summary);
    });
    q('check').addEventListener('click', check);
    q('preview').addEventListener('click', () => preview(cluster, body()));
    q('create').addEventListener('click', () => create());

    async function create() {
      const b = body();
      if (!confirm(tr('capi.new.confirm', { name: b.name, cp: b.cp_replicas, workers: b.worker_replicas, cluster }))) return;
      const fb = q('feedback');
      q('create').disabled = true;
      try {
        const r = await fetch(`/api/capi/${enc(cluster)}/cluster-create`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
        fb.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('capi.new.started', { id: d.action_id }))}</span>`;
        state.running = true;
        follow(d.action_id, d.cluster);
      } catch (e) {
        fb.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(tr('capi.new.error', { msg: e.message }))}</span>`;
        q('create').disabled = false;
      }
    }

    // Le suivi : les étapes du script (en anglais, lues telles quelles par
    // le dock) redites dans la langue de l'interface.
    function follow(actionId, ref) {
      const box = q('live');
      box.hidden = false;
      box.scrollIntoView({ block: 'nearest' });
      const t0 = Date.now();
      const tick = setInterval(() => {
        q('live-meta').textContent = tr('progress.elapsed', { t: XferProgress.duration((Date.now() - t0) / 1000) });
      }, 1000);
      if (!window.SSEReconnect) return;
      const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
        on: {
          step: (e) => {
            let s = {};
            try { s = JSON.parse(e.data); } catch { return; }
            const line = stepText(s);
            if (line) q('live-line').textContent = line.text;
            if (line && line.pct != null) q('live-bar').style.width = `${line.pct}%`;
          },
          end: (e) => {
            let d = {};
            try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
            clearInterval(tick);
            es.close();
            state.running = false;
            if (d.status === 'done') {
              q('live-line').textContent = tr('capi.new.done', { name: ref });
              q('live-bar').style.width = '100%';
              doneBlock(q('done-box'), cluster, ref);
            } else if (d.status === 'cancelled') {
              q('live-line').textContent = tr('capi.new.cancelled');
            } else {
              q('live-line').textContent = tr('capi.new.failed', { msg: d.error_summary || d.status || '?' });
              check();
            }
          },
        },
      });
    }

    applyPool();
    summary();
    check();
  }

  function stepText(s) {
    const m = /infrastructure (ready|pending), control plane (\d+)\/(\d+), workers (\d+)\/(\d+) \((\d+)%\)/.exec(s.message || '');
    if (m) {
      return {
        text: tr('capi.new.prog.line', {
          infra: m[1] === 'ready' ? tr('capi.new.prog.infraReady') : tr('capi.new.prog.infraPending'),
          cp: `${m[2]}/${m[3]}`, workers: `${m[4]}/${m[5]}` }),
        pct: Number(m[6]),
      };
    }
    if (s.step_id === 'check' && s.status === 'done') return { text: tr('capi.new.prog.checked'), pct: 2 };
    if (s.step_id === 'render') return { text: tr('capi.new.prog.rendered'), pct: 4 };
    if (s.step_id === 'apply') return { text: tr('capi.new.prog.applied'), pct: 6 };
    if (s.status === 'error') return { text: tr('capi.new.failed', { msg: s.message }), pct: null };
    return null;
  }

  function doneBlock(box, cluster, ref) {
    const [ns, name] = String(ref).split('/');
    box.hidden = false;
    box.innerHTML = `
      <div class="apply-bar" style="margin:6px 0 0; padding:0; border:0;">
        <a class="btn btn-secondary btn-sm tip" data-x="kubeconfig" href="/api/capi/${enc(cluster)}/cluster/${enc(ns)}/${enc(name)}/kubeconfig" download data-tip="${esc(tr('capi.new.t.kubeconfig'))}">${Icons.svg('download')} <span>${esc(tr('capi.new.kubeconfig'))}</span></a>
        <button type="button" class="btn btn-secondary btn-sm tip" data-x="open-list" data-tip="${esc(tr('capi.new.t.openList'))}">${Icons.svg('capi')} <span>${esc(tr('capi.new.openList'))}</span></button>
      </div>`;
    box.querySelector('[data-x="open-list"]').addEventListener('click', () => {
      document.querySelector('#tab-automation .sub-tab[data-capi-tab="k8s"]')?.click();
    });
  }

  function stackNote(box, stack) {
    if (!stack) { box.innerHTML = ''; return; }
    if (stack.ready) {
      const provs = (stack.providers || []).map(p => `${p.name} ${p.version}`).join(', ');
      box.innerHTML = `<p class="tf-desc">${Icons.svg('ok', { size: 12 })} ${esc(tr('capi.new.stackReady', { providers: provs }))}</p>`;
      return;
    }
    box.innerHTML = `
      <div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('capi.new.stackMissing', { missing: (stack.missing || []).join(', ') }))}
        <button type="button" class="btn btn-secondary btn-sm tip" data-x="go-install" data-tip="${esc(tr('capi.new.t.goInstall'))}">${esc(tr('capi.new.goInstall'))}</button></div></div>`;
    box.querySelector('[data-x="go-install"]').addEventListener('click', () => {
      document.querySelector('#tab-automation .sub-tab[data-capi-tab="install"]')?.click();
    });
  }

  async function preview(cluster, b) {
    const panel = FloatingPanels.open({
      id: `capi-preview-${cluster}`,
      title: tr('capi.new.previewTitle', { name: b.name || '?' }),
      icon: 'preview',
      bodyHtml: `<pre class="capi-yaml" data-x="yaml">${esc(tr('capi.new.checking'))}</pre>`,
      width: 760, height: 560,
    });
    const pre = panel.el.querySelector('[data-x="yaml"]');
    try {
      const r = await fetch(`/api/capi/${enc(cluster)}/cluster-preview`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
      pre.textContent = d.yaml;
    } catch (e) {
      pre.textContent = tr('capi.new.error', { msg: e.message });
    }
  }

  function parseQty(v) {
    const m = /^(\d+)(Mi|Gi|Ti)$/.exec(String(v || ''));
    if (!m) return 0;
    return Number(m[1]) * { Mi: 1024 ** 2, Gi: GIB, Ti: 1024 ** 4 }[m[2]];
  }

  return { render, _findingText: findingText, _stepText: stepText };
})();

window.CapiCreate = CapiCreate;
