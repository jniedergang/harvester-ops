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

  // v1.53.0 : la création passe dans une fenêtre à menus, comme celle d'une
  // VM (demande de l'exploitant) : l'essentiel au premier menu, le reste
  // rangé par thème, le contrôle préalable à part avec un compteur par menu.
  const SECTIONS = ['essentials', 'nodes', 'network', 'storage', 'kubernetes', 'integrations', 'check'];
  const SEC_LABEL = {
    essentials: () => tr('capi.new.nav.essentials'),
    nodes: () => tr('capi.new.nav.nodes'),
    network: () => tr('capi.new.nav.network'),
    storage: () => tr('capi.new.nav.storage'),
    kubernetes: () => tr('capi.new.nav.kubernetes'),
    integrations: () => tr('capi.new.nav.integrations'),
    check: () => tr('capi.new.nav.check'),
  };
  const SEC_TIP = {
    essentials: () => tr('capi.new.nav.t.essentials'),
    nodes: () => tr('capi.new.nav.t.nodes'),
    network: () => tr('capi.new.nav.t.network'),
    storage: () => tr('capi.new.nav.t.storage'),
    kubernetes: () => tr('capi.new.nav.t.kubernetes'),
    integrations: () => tr('capi.new.nav.t.integrations'),
    check: () => tr('capi.new.nav.t.check'),
  };
  const SEC_ICON = { essentials: 'general', nodes: 'compute', network: 'network', storage: 'storage',
                     kubernetes: 'capi', integrations: 'plug', check: 'test' };
  // Le menu où se règle ce que dit chaque constat (compteur sur le menu).
  const FINDING_SEC = {
    'small-nodes': 'nodes', 'cpu-short': 'nodes', 'memory-short': 'nodes',
    'gateway-differs': 'network', 'mask-differs': 'network', 'endpoint-dhcp': 'network',
    'storage-class-missing': 'storage', 'cidr-overlap': 'kubernetes', 'cni-tuning-ignored': 'integrations',
  };

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
  // 2.5.0 (décision 173) : le même formulaire sert d'éditeur de modèle de
  // cluster (`opts.template`) : nom, namespace et namespace des VMs viennent
  // du plan et ne sont pas montrés ; chaque champ de référence propose aussi
  // « Variable... », qui le change en saisie `{{nom}}` ; les nombres passent
  // en texte pour recevoir une variable ; ni contrôle, ni création.
  const VAR = '__var__';
  const REF_FIELDS = ['image', 'ssh_keypair', 'network', 'ip_pool', 'ip_pool_refs', 'extra_networks', 'extra_disk_class'];
  const PLAN_FIELDS = ['name', 'namespace', 'target_namespace'];
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];

  function formHtml(inv, cluster, mode) {
    const T = !!(mode && mode.template);
    // une liste de référence : « Variable... » en dernier, en mode modèle
    const ref = (html) => (T ? `${html}<option value="${VAR}">${esc(tr('capi.tpl.variable'))}</option>` : html);
    const num = (attrs) => (T ? attrs.replace('type="number"', 'type="text" inputmode="numeric" data-int="1"')
      .replace(/ min="\d+"| max="\d+"/g, '') : attrs);
    const secs = T ? SECTIONS.filter(x => x !== 'check') : SECTIONS;
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
      <div class="capi-create capi-create-win${T ? ' capi-create-tpl' : ''}" data-cluster="${esc(cluster)}">
        <div class="capi-stack-note" data-x="stack-note"></div>
        <div class="vm-edit-layout vm-create-layout">
          <aside class="vm-edit-nav" role="tablist" aria-label="${esc(tr('capi.new.title'))}">
            ${secs.map(x => `
              <button type="button" role="tab" data-sec="${x}" class="tip" data-tip="${esc(SEC_TIP[x]())}">
                <span class="ic">${Icons.svg(SEC_ICON[x], { size: 14 })}</span>
                <span>${esc(SEC_LABEL[x]())}</span>
                <span class="capi-nav-count" data-count="${x}" hidden></span>
              </button>`).join('')}
          </aside>
          <div class="vm-edit-content tf-form">
        <section class="capi-sec" data-sec="essentials" role="tabpanel">
          <h3>${esc(SEC_LABEL.essentials())}</h3>
          <div class="tf-args">
            ${T ? '' : `<label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.name'))}</span>
              <input data-x="name" type="text" placeholder="web-prod" autocomplete="off" spellcheck="false"
                     class="tip" data-tip="${esc(tr('capi.new.t.name'))}"></label>`}
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.version'))}</span>
              <select data-x="k8s_version" class="tip" data-tip="${esc(tr('capi.new.t.version'))}">
                ${opts(verOpts, inv.default_version, v => v.tested ? `${v.version} (${tr('capi.new.tested')})` : v.version, v => v.version)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cp'))}</span>
              <select data-x="cp_replicas" class="tip" data-tip="${esc(tr('capi.new.t.cp'))}">
                <option value="1">${esc(tr('capi.new.cp1'))}</option>
                <option value="3">${esc(tr('capi.new.cp3'))}</option>
                <option value="5">${esc(tr('capi.new.cp5'))}</option>
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.workers'))}</span>
              <input ${num('data-x="worker_replicas" type="number" min="0" max="50"')} value="1"
                     class="tip" data-tip="${esc(tr('capi.new.t.workers'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.preset'))}</span>
              <select data-x="preset" class="tip" data-tip="${esc(tr('capi.new.t.preset'))}">
                <option value="small">${esc(tr('capi.new.p.small'))}</option>
                <option value="medium">${esc(tr('capi.new.p.medium'))}</option>
                <option value="large">${esc(tr('capi.new.p.large'))}</option>
                <option value="custom">${esc(tr('capi.new.p.custom'))}</option>
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.image'))}</span>
              <select data-x="image" data-ref="1" class="tip" data-tip="${esc(tr('capi.new.t.image'))}">
                ${ref(images.length ? opts(images, img0.ref, i => i.display_name + (i.virtual_size ? ` (${bytes(i.virtual_size)})` : ''), i => i.ref)
                                : `<option value="">${esc(tr('capi.new.noImage'))}</option>`)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.keypair'))}</span>
              <select data-x="ssh_keypair" data-ref="1" class="tip" data-tip="${esc(tr('capi.new.t.keypair'))}">
                ${ref(keys.length ? opts(keys, keys[0]) : `<option value="">${esc(tr('capi.new.noKeypair'))}</option>`)}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.network'))}</span>
              <select data-x="network" data-ref="1" class="tip" data-tip="${esc(tr('capi.new.t.network'))}">
                ${ref(opts(nets, (nets.find(n => /production/.test(n.ref)) || nets[0] || {}).ref,
                       n => n.ref + (n.vlan ? ` (${tr('capi.new.vlan', { vlan: n.vlan })})` : ''), n => n.ref))}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.pool'))}</span>
              <select data-x="ip_pool" data-ref="1" class="tip" data-tip="${esc(tr('capi.new.t.pool'))}">
                ${ref(opts(pools, pool0.name, poolLabel, p => p.name))}
              </select></label>
          </div>
          <p class="tf-desc capi-summary" data-x="summary"></p>
          <p class="tf-desc">${esc(tr('capi.new.intro'))}</p>
        </section>
        <section class="capi-sec" data-sec="nodes" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.nodes())}</h3>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cpu'))}</span>
              <input ${num('data-x="cpu" type="number" min="1" max="64"')} value="2" class="tip" data-tip="${esc(tr('capi.new.t.cpu'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.memory'))}</span>
              <input data-x="memory" type="text" value="4Gi" list="capi-mem-sizes" class="tip" data-tip="${esc(tr('capi.new.t.memory'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.disk'))}</span>
              <input data-x="disk_size" type="text" value="40Gi" list="capi-disk-sizes" class="tip" data-tip="${esc(tr('capi.new.t.disk'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.sshUser'))}</span>
              <input data-x="ssh_user" type="text" value="${esc(user0)}" class="tip" data-tip="${esc(tr('capi.new.t.sshUser'))}"></label>
          </div>
          <datalist id="capi-mem-sizes">${['4Gi', '8Gi', '12Gi', '16Gi', '32Gi'].map(v => `<option value="${v}">`).join('')}</datalist>
          <datalist id="capi-disk-sizes">${['40Gi', '60Gi', '80Gi', '120Gi', '200Gi'].map(v => `<option value="${v}">`).join('')}</datalist>
        </section>
        <section class="capi-sec" data-sec="network" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.network())}</h3>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.gateway'))} <span class="capi-derived" data-x="gw-derived">${esc(tr('capi.new.fromPool'))}</span></span>
              <input data-x="gateway" type="text" value="${esc(pool0.gateway || '')}" class="tip" data-tip="${esc(tr('capi.new.t.gateway'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.mask'))} <span class="capi-derived" data-x="mask-derived">${esc(tr('capi.new.fromPool'))}</span></span>
              <input data-x="subnet_mask" type="text" value="${esc(pool0.mask || '')}" class="tip" data-tip="${esc(tr('capi.new.t.mask'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.dns'))}</span>
              <input data-x="dns" type="text" value="${esc(dns0)}" list="capi-dns" placeholder="${esc(pool0.gateway || '8.8.8.8')}"
                     class="tip" data-tip="${esc(tr('capi.new.t.dns'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraPools'))}</span>
              <select data-x="ip_pool_refs" data-ref="1" multiple size="2" class="tip" data-tip="${esc(tr('capi.new.t.extraPools'))}">
                ${ref(opts(pools, '', poolLabel, p => p.name))}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraNets'))}</span>
              <select data-x="extra_networks" data-ref="1" multiple size="2" class="tip" data-tip="${esc(tr('capi.new.t.extraNets'))}">
                ${ref(opts(nets, '', n => n.ref, n => n.ref))}
              </select></label>
          </div>
          <datalist id="capi-dns">${[pool0.gateway, dns0].filter(Boolean).map(v => `<option value="${esc(v)}">`).join('')}</datalist>
        </section>
        <section class="capi-sec" data-sec="storage" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.storage())}</h3>
          <div class="tf-args">
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraDisk'))}</span>
              <input data-x="extra_disk_size" type="text" placeholder="20Gi" class="tip" data-tip="${esc(tr('capi.new.t.extraDisk'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.extraDiskClass'))}</span>
              <select data-x="extra_disk_class" data-ref="1" class="tip" data-tip="${esc(tr('capi.new.t.extraDiskClass'))}">
                ${ref(opts(scs, (scs.find(s => s.default) || scs[0] || {}).name, s => s.name + (s.default ? ' *' : ''), s => s.name))}
              </select></label>
          </div>
        </section>
        <section class="capi-sec" data-sec="kubernetes" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.kubernetes())}</h3>
          <div class="tf-args">
            ${T ? '' : `<label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.namespace'))}</span>
              <input data-x="namespace" type="text" placeholder="${esc(tr('capi.new.sameAsName'))}" class="tip" data-tip="${esc(tr('capi.new.t.namespace'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.targetNs'))}</span>
              <input data-x="target_namespace" type="text" disabled readonly class="tip" data-tip="${esc(tr('capi.new.t.targetNsForced'))}"
                     title="${esc(tr('capi.new.t.targetNsForced'))}" aria-label="${esc(tr('capi.new.t.targetNsForced'))}"></label>`}
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.cni'))}</span>
              <select data-x="cni" class="tip" data-tip="${esc(tr('capi.new.t.cni'))}">
                ${opts(['calico', 'canal', 'cilium', 'none'], 'calico')}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.podCidr'))}</span>
              <input data-x="pod_cidr" type="text" value="10.42.0.0/16" class="tip" data-tip="${esc(tr('capi.new.t.podCidr'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.svcCidr'))}</span>
              <input data-x="service_cidr" type="text" value="10.43.0.0/16" class="tip" data-tip="${esc(tr('capi.new.t.svcCidr'))}"></label>
          </div>
        </section>
        <section class="capi-sec" data-sec="integrations" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.integrations())}</h3>
          <div class="tf-args">
            <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('capi.new.t.rancher'))}"><input data-x="rancher_import" type="checkbox"><span class="tf-label">${esc(tr('capi.new.f.rancher'))}</span></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.fleetRepo'))}</span>
              <input data-x="fleet_repo" type="text" placeholder="https://" class="tip" data-tip="${esc(tr('capi.new.t.fleetRepo'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.fleetBranch'))}</span>
              <input data-x="fleet_branch" type="text" value="main" class="tip" data-tip="${esc(tr('capi.new.t.fleetBranch'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.mtu'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <input ${num('data-x="cni_mtu" type="number" min="576" max="9000"')} value="1500" class="tip" data-tip="${esc(tr('capi.new.t.mtu'))}"></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.encap'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <select data-x="cni_encapsulation" class="tip" data-tip="${esc(tr('capi.new.t.encap'))}">
                ${opts(['VXLANCrossSubnet', 'VXLAN', 'IPIP', 'IPIPCrossSubnet', 'None'], 'VXLANCrossSubnet')}
              </select></label>
            <label class="tf-field"><span class="tf-label">${esc(tr('capi.new.f.bgp'))} <span class="capi-derived">${esc(tr('capi.new.fleetOnly'))}</span></span>
              <select data-x="cni_bgp" class="tip" data-tip="${esc(tr('capi.new.t.bgp'))}">
                ${opts(['Disabled', 'Enabled'], 'Disabled')}
              </select></label>
          </div>
        </section>
        ${T ? '' : `<section class="capi-sec" data-sec="check" role="tabpanel" hidden>
          <h3>${esc(SEC_LABEL.check())}</h3>
          <div class="xfer-report" data-x="report"></div>
        </section>`}
          </div>
        </div>
        ${T ? '' : `<fieldset class="tf-block xfer-live" data-x="live" hidden>
          <legend>${esc(tr('capi.new.sec.progress'))}</legend>
          <div class="xfer-live-line" data-x="live-line">${esc(tr('capi.new.waiting'))}</div>
          <div class="progress-mini xfer-live-bar"><div class="fill" data-x="live-bar" style="width:0%"></div></div>
          <div class="tf-desc" data-x="live-meta"></div>
          <div class="xfer-archive" data-x="done-box" hidden></div>
        </fieldset>
        <div class="apply-bar capi-create-actions">
          <button type="button" class="btn btn-primary btn-sm tip" data-x="create" data-tip="${esc(tr('capi.new.t.create'))}" disabled>${Icons.svg('capi')} <span>${esc(tr('capi.new.create'))}</span></button>
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="check" data-tip="${esc(tr('capi.new.t.check'))}">${Icons.svg('refresh')} <span>${esc(tr('capi.new.check'))}</span></button>
          <button type="button" class="btn btn-secondary btn-sm tip" data-x="preview" data-tip="${esc(tr('capi.new.t.preview'))}">${Icons.svg('preview')} <span>${esc(tr('capi.new.preview'))}</span></button>
          <button type="button" class="btn-link capi-check-status tip" data-x="check-status" data-tip="${esc(tr('capi.new.t.checkStatus'))}"></button>
          ${(() => {
            const why = can('admin') ? tr('capi.tpl.t.saveAs') : tr('capi.tpl.t.saveAsAdmin');
            return `<span class="tip" data-tip="${esc(why)}"><button type="button" class="btn btn-secondary btn-sm" data-x="save-template"
              title="${esc(why)}" aria-label="${esc(why)}"${can('admin') ? '' : ' disabled'}>${Icons.svg('save')} <span>${esc(tr('capi.tpl.saveAs'))}</span></button></span>`;
          })()}
          <span class="apply-result" data-x="feedback"></span>
        </div>`}
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

    function show(sec) {
      root.querySelectorAll('.vm-edit-nav [data-sec]').forEach(b => {
        b.classList.toggle('active', b.dataset.sec === sec);
        b.setAttribute('aria-selected', b.dataset.sec === sec ? 'true' : 'false');
      });
      root.querySelectorAll('.capi-sec').forEach(x => { x.hidden = x.dataset.sec !== sec; });
    }
    root.querySelectorAll('.vm-edit-nav [data-sec]').forEach(b =>
      b.addEventListener('click', () => show(b.dataset.sec)));
    q('check-status').addEventListener('click', () => show('check'));
    show('essentials');

    // Un compteur par menu, et l'état du contrôle dans la barre d'actions,
    // visible quel que soit le menu ouvert.
    // Le menu de chaque champ, lu dans le formulaire : un refus qui nomme
    // son champ (`invalid`, option « dns »...) compte sur le bon menu.
    const fieldSec = {};
    root.querySelectorAll('.capi-sec').forEach(sec => sec.querySelectorAll('[data-x]').forEach(el => {
      fieldSec[el.dataset.x] = sec.dataset.sec;
    }));

    function counts(items, blocked) {
      const per = {};
      items.filter(f => f.level !== 'ok').forEach(f => {
        const sec = FINDING_SEC[f.code] || fieldSec[(f.facts || {}).option] || 'essentials';
        per[sec] = per[sec] || { n: 0, block: false };
        per[sec].n += 1;
        per[sec].block = per[sec].block || f.level === 'block';
      });
      root.querySelectorAll('[data-count]').forEach(el => {
        const c = per[el.dataset.count];
        el.hidden = !c;
        el.textContent = c ? String(c.n) : '';
        el.classList.toggle('is-block', !!(c && c.block));
      });
      const nb = items.filter(f => f.level === 'block').length;
      const nw = items.filter(f => f.level === 'warn').length;
      const st = q('check-status');
      st.textContent = blocked ? tr('capi.new.status.blocked', { n: nb, w: nw })
        : (nw ? tr('capi.new.status.warn', { w: nw }) : tr('capi.new.status.ok'));
      st.classList.toggle('is-block', !!blocked);
    }

    // décision 164 : les VMs vont dans le namespace du cluster (montré grisé,
    // posé par le serveur, jamais envoyé)
    const syncTargetNs = () => { q('target_namespace').value = q('namespace').value || q('name').value; };
    q('name').addEventListener('input', syncTargetNs);
    q('namespace').addEventListener('input', syncTargetNs);
    function body() {
      const b = readBody(root);
      delete b.target_namespace;
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
        q('check-status').textContent = tr('capi.new.needName');
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
        counts(items, d.blocked);
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
    // 2.5.0 : la demande courante devient un modèle de cluster (sans nom ni
    // namespaces, donnés par le plan), éditée dans l'onglet Plans
    q('save-template').addEventListener('click', () => {
      if (!can('admin') || !window.BMPlans) return;
      const spec = body();
      PLAN_FIELDS.forEach(k => delete spec[k]);
      BMPlans.openCapiTemplate(null, { spec, cluster });
    });

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
              if (window.CAPI && CAPI.refreshK8s) CAPI.refreshK8s();
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

  /** La demande lue dans le formulaire (création ou modèle). Un champ passé
   *  en variable est lu tel quel, une liste en variable découpée aux virgules. */
  function readBody(root) {
    const b = {};
    root.querySelectorAll('[data-x]').forEach(el => {
      const x = el.dataset.x;
      if (!el.matches('input, select') || x === 'preset') return;
      if (el.type === 'checkbox') b[x] = el.checked;
      else if (el.multiple) b[x] = [...el.selectedOptions].map(o => o.value).filter(v => v && v !== VAR);
      else if (el.dataset.multi) b[x] = el.value.split(',').map(v => v.trim()).filter(Boolean);
      else if (el.dataset.int && /^\d+$/.test(el.value.trim())) b[x] = Number(el.value.trim());
      else if (el.value !== '' && el.value !== VAR) b[x] = el.dataset.int ? el.value.trim() : el.value;
    });
    if (!b.extra_disk_size) delete b.extra_disk_class;
    (b.ip_pool_refs || []).length || delete b.ip_pool_refs;
    if (b.ip_pool_refs && b.ip_pool && !b.ip_pool_refs.includes(b.ip_pool)) b.ip_pool_refs.unshift(b.ip_pool);
    (b.extra_networks || []).length || delete b.extra_networks;
    if (b.extra_networks) b.extra_networks = b.extra_networks.filter(n => n !== b.network);
    return b;
  }

  // --- éditeur de modèle (2.5.0) ----------------------------------------
  /** Un champ de référence passe en saisie `{{nom}}` ; « Liste » le rend. */
  function toVariable(sel, value) {
    const wrap = document.createElement('span');
    wrap.className = 'capi-var-wrap';
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.dataset.x = sel.dataset.x;
    inp.dataset.ref = '1';
    inp.dataset.var = '1';
    if (sel.multiple) inp.dataset.multi = '1';
    inp.className = 'tip capi-var';
    inp.placeholder = sel.multiple ? '{{name}}, {{other}}' : '{{name}}';
    inp.spellcheck = false;
    inp.value = value || '';
    const tipText = sel.multiple ? tr('capi.tpl.t.variableList') : tr('capi.tpl.t.variableInput');
    inp.dataset.tip = tipText;
    inp.title = tipText;
    inp.setAttribute('aria-label', tipText);
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'btn btn-sm btn-secondary tip capi-var-back';
    back.dataset.tip = tr('capi.tpl.t.backToList');
    back.title = back.dataset.tip;
    back.setAttribute('aria-label', back.dataset.tip);
    back.textContent = tr('capi.tpl.backToList');
    sel.replaceWith(wrap);
    wrap.append(inp, back);
    back.addEventListener('click', () => {
      [...sel.options].forEach(o => { o.selected = false; });
      if (!sel.multiple) sel.selectedIndex = 0;
      wrap.replaceWith(sel);
    });
    return inp;
  }

  /** Pose une demande de modèle dans le formulaire : une valeur à variable
   *  passe le champ en saisie ; une référence absente du cluster choisi
   *  reste proposée, marquée. */
  function fillTemplate(root, spec) {
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    Object.entries(spec || {}).forEach(([k, v]) => {
      const el = q(k);
      if (!el || PLAN_FIELDS.includes(k)) return;
      const list = Array.isArray(v) ? v : null;
      const hasVar = (list || [v]).some(x => /\{\{/.test(String(x)));
      if (el.type === 'checkbox') { el.checked = !!v; return; }
      if (el.tagName === 'SELECT' && el.dataset.ref && hasVar) {
        toVariable(el, list ? list.join(', ') : String(v));
        return;
      }
      if (el.tagName === 'SELECT') {
        const want = (list || [v]).map(String);
        want.forEach(w => {
          if (![...el.options].some(o => o.value === w)) {
            const o = document.createElement('option');
            o.value = w;
            o.textContent = tr('capi.tpl.absent', { value: w });
            el.insertBefore(o, el.querySelector(`option[value="${VAR}"]`));
          }
        });
        [...el.options].forEach(o => { o.selected = want.includes(o.value); });
        return;
      }
      el.value = list ? list.join(', ') : String(v);
    });
    const pre = q('preset');
    if (pre) {
      const hit = Object.entries(PRESETS).find(([, p]) => String(p.cpu) === String(q('cpu').value) && p.memory === q('memory').value);
      pre.value = hit ? hit[0] : 'custom';
    }
  }

  /** L'éditeur de modèle : le formulaire de création en mode modèle, rempli
   *  de `spec`, propositions tirées de `inv` (cluster choisi, ou aucune).
   *  Rend { read } : la demande à enregistrer. */
  function templateForm(container, inv, cluster, spec) {
    container.innerHTML = formHtml(inv || {}, cluster || '', { template: true });
    const root = container.querySelector('.capi-create');
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    const poolByName = Object.fromEntries(((inv || {}).pools || []).map(p => [p.name, p]));
    const show = (sec) => {
      root.querySelectorAll('.vm-edit-nav [data-sec]').forEach(b => {
        b.classList.toggle('active', b.dataset.sec === sec);
        b.setAttribute('aria-selected', b.dataset.sec === sec ? 'true' : 'false');
      });
      root.querySelectorAll('.capi-sec').forEach(x => { x.hidden = x.dataset.sec !== sec; });
    };
    root.querySelectorAll('.vm-edit-nav [data-sec]').forEach(b => b.addEventListener('click', () => show(b.dataset.sec)));
    show('essentials');
    root.addEventListener('change', (e) => {
      const el = e.target;
      if (el.tagName === 'SELECT' && el.dataset.ref && [...el.selectedOptions].some(o => o.value === VAR)) {
        toVariable(el, '').focus();
        return;
      }
      if (el.dataset.x === 'preset' && PRESETS[el.value]) { q('cpu').value = PRESETS[el.value].cpu; q('memory').value = PRESETS[el.value].memory; }
      if (el.dataset.x === 'ip_pool') {
        const p = poolByName[el.value] || {};
        if (p.gateway) q('gateway').value = p.gateway;
        if (p.mask) q('subnet_mask').value = p.mask;
      }
    });
    fillTemplate(root, spec || {});
    return { read: () => { const b = readBody(root); PLAN_FIELDS.forEach(k => delete b[k]); return b; } };
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
      // 2.2.0 : l'onglet Clusters a son adresse ; y aller relit la liste
      if (window.Router) Router.go('#automation/capi/clusters');
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
      if (window.Router) Router.go('#automation/capi/installation');
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

  const PANEL_ID = 'capi-create';

  /** La fenêtre de création : repliable dans la barre des fenêtres, une
   *  seule à la fois (la rouvrir la ramène au premier plan). */
  function open(cluster) {
    if (!cluster) return null;
    if (document.getElementById('fp-' + PANEL_ID)) {
      return FloatingPanels.open({ id: PANEL_ID, icon: 'capi', title: tr('capi.new.title') });
    }
    const panel = FloatingPanels.open({
      id: PANEL_ID,
      title: `${tr('capi.new.title')} (${cluster})`,
      icon: 'capi',
      bodyHtml: '<div class="capi-create-host"></div>',
      width: 1060, height: 720,
      restoreSpec: { type: 'capi-create', args: { cluster } },
    });
    render(panel.el.querySelector('.capi-create-host'), cluster);
    return panel;
  }

  return { open, render, formHtml, templateForm, _findingText: findingText, _stepText: stepText };
})();

window.CapiCreate = CapiCreate;
if (typeof FloatingPanels !== 'undefined') {
  FloatingPanels.registerType('capi-create', (args) => CapiCreate.open(args.cluster));
}
