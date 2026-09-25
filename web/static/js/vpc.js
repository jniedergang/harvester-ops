/**
 * harvester-ops : la vue VPC (kube-ovn), un bloc par VPC, lue de gauche à
 * droite comme les vues Fabrique et Réseau (v1.49.0).
 *
 *   subnets et leurs VMs  |  le routeur du VPC  |  par où l'on sort
 *
 * C'est aussi le « schéma vivant » des formulaires : il se relit toutes les
 * 8 s et montre l'effet d'une création ou d'une modification. L'écriture
 * passe par `harvester-network` côté serveur, en action suivie.
 * Voir docs/design/2026-09-26-reseaux-kubeovn.md.
 */
const VpcBoard = (() => {
  const REFRESH_MS = 8000;
  const { tr, esc, val, applyTips } = window.Board;
  const enc = encodeURIComponent;
  const DEFAULT_VPC = 'ovn-cluster';
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;
  let modal = null;

  // Un appel littéral par constat : le contrôle de parité des traductions
  // ne voit que ces formes.
  const FINDINGS = {
    'overlay-no-subnet': (v) => tr('vpc.fd.overlay-no-subnet', 'Overlay network {network} has no subnet: a VM attached to it gets no address.', v),
    'subnet-network-missing': (v) => tr('vpc.fd.subnet-network-missing', 'Subnet {subnet} serves {provider}, whose overlay network no longer exists.', v),
    'subnet-full': (v) => tr('vpc.fd.subnet-full', 'Subnet {subnet} is nearly full: {used} of {total} addresses in use.', v),
    'cidr-overlap': (v) => tr('vpc.fd.cidr-overlap', 'Subnets {subnet} and {other} overlap.', v),
    'invalid-name': (v) => tr('vpc.fd.invalid-name', 'Invalid name "{name}": lower case letters, digits and dashes.', v),
    'subnet-exists': (v) => tr('vpc.fd.subnet-exists', 'A subnet named {subnet} already exists.', v),
    'subnet-system': (v) => tr('vpc.fd.subnet-system', '{subnet} is managed by kube-ovn or Harvester: read only here.', v),
    'subnet-immutable': (v) => tr('vpc.fd.subnet-immutable', 'kube-ovn cannot change {fields} of an existing subnet.', v),
    'vpc-missing': (v) => tr('vpc.fd.vpc-missing', 'VPC {vpc} does not exist.', v),
    'invalid-cidr': (v) => tr('vpc.fd.invalid-cidr', '"{cidr}" is not a network (example: 10.200.0.0/24).', v),
    'cidr-too-small': (v) => tr('vpc.fd.cidr-too-small', '{cidr} is too small for a subnet.', v),
    'cidr-overlap-req': (v) => tr('vpc.fd.cidr-overlap-req', '{cidr} overlaps {other} ({other_cidr}).', v),
    'cidr-covers-nodes': (v) => tr('vpc.fd.cidr-covers-nodes', '{cidr} contains node addresses ({ips}).', v),
    'gateway-outside': (v) => tr('vpc.fd.gateway-outside', 'Gateway {gateway} is outside {cidr}.', v),
    'exclude-outside': (v) => tr('vpc.fd.exclude-outside', 'Excluded address {value} is outside {cidr}.', v),
    'network-both': (v) => tr('vpc.fd.network-both', 'Choose an existing overlay network or a new one, not both.', v),
    'network-missing': (v) => tr('vpc.fd.network-missing', 'Overlay network {network} does not exist.', v),
    'network-taken': (v) => tr('vpc.fd.network-taken', 'Overlay network {network} is already served by subnet {subnet}.', v),
    'network-exists': (v) => tr('vpc.fd.network-exists', 'A network named {network} already exists.', v),
    'invalid-network-name': (v) => tr('vpc.fd.invalid-network-name', 'Invalid network name "{network}": namespace/name.', v),
    'namespace-missing': (v) => tr('vpc.fd.namespace-missing', 'Namespace {namespace} does not exist.', v),
    'network-none': (v) => tr('vpc.fd.network-none', 'No overlay network: no VM will be able to use this subnet.', v),
    'nat-custom-vpc': (v) => tr('vpc.fd.nat-custom-vpc', 'kube-ovn only does outgoing NAT in the default VPC, not in {vpc}.', v),
    'no-nat': (v) => tr('vpc.fd.no-nat', 'Without outgoing NAT, the VMs only reach the cluster networks.', v),
    'vpc-isolated': (v) => tr('vpc.fd.vpc-isolated', 'VPC {vpc} has no route out: its VMs only see each other.', v),
    'invalid-allow': (v) => tr('vpc.fd.invalid-allow', '"{value}" is not a network.', v),
    'allow-ignored': (v) => tr('vpc.fd.allow-ignored', 'Allowed subnets only apply to a private subnet.', v),
    'no-dhcp': (v) => tr('vpc.fd.no-dhcp', 'Without DHCP, each VM must be given its address by hand (cloud-init).', v),
    'vpc-exists': (v) => tr('vpc.fd.vpc-exists', 'A VPC named {vpc} already exists.', v),
    'vpc-system': (v) => tr('vpc.fd.vpc-system', '{vpc} is the default VPC of kube-ovn: read only here.', v),
    'invalid-route': (v) => tr('vpc.fd.invalid-route', 'Route "{cidr} -> {next_hop}" is not valid.', v),
    'peer-missing': (v) => tr('vpc.fd.peer-missing', 'VPC {vpc} cannot be peered (missing, or this one).', v),
    'invalid-peer-ip': (v) => tr('vpc.fd.invalid-peer-ip', '"{value}" must be an address with its prefix (example: 10.255.0.1/30).', v),
    'subnet-in-use': (v) => tr('vpc.fd.subnet-in-use', 'Subnet {subnet} is still used by {users}.', v),
    'vpc-has-subnets': (v) => tr('vpc.fd.vpc-has-subnets', 'VPC {vpc} still has subnets: {subnets}.', v),
    'subnet-missing': (v) => tr('vpc.fd.subnet-missing', 'Subnet {subnet} does not exist.', v),
  };
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const LEVEL_RANK = { block: 0, warn: 1, ok: 2 };

  function findingText(f) {
    const v = Object.assign({}, f.facts || f);
    // Même code côté contrôle d'une demande et côté vue : le texte diffère.
    const code = f.code === 'cidr-overlap' && v.other_cidr ? 'cidr-overlap-req' : f.code;
    const fn = FINDINGS[code];
    return fn ? fn(v) : f.code;
  }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  function usage(s) {
    const total = (s.used || 0) + (s.available || 0);
    const pct = total ? Math.round(100 * s.used / total) : 0;
    return `<div class="vpc-usage tip" data-tip="${esc(tr('vpc.usageTip', '{used} of {total} addresses in use (gateway and excluded addresses count as used)', { used: s.used, total }))}">
      <div class="progress-mini"><div class="fill" style="width:${pct}%"></div></div>
      <small>${esc(s.used)} / ${esc(total)}</small></div>`;
  }

  function subnetCard(s) {
    const tags = [
      s.underlay ? tr('vpc.tag.underlay', 'underlay VLAN') : tr('vpc.tag.overlay', 'overlay'),
      s.nat ? tr('vpc.tag.nat', 'outgoing NAT') : null,
      s.dhcp ? 'DHCP' : null,
      s.private ? tr('vpc.tag.private', 'private') : null,
    ].filter(Boolean);
    const editable = !s.system;
    const actions = editable ? `
      <span class="vpc-actions">
        <button type="button" class="btn-icon-sm tip needs-admin" data-vpc-edit-subnet="${esc(s.name)}"
                data-tip-i18n="vpc.t.editSubnet" data-tip="Edit">${Icons.svg('edit', { size: 13 })}</button>
        <button type="button" class="btn-icon-sm tip needs-admin" data-vpc-delete-subnet="${esc(s.name)}"
                data-tip-i18n="vpc.t.deleteSubnet" data-tip="Delete">${Icons.svg('delete', { size: 13 })}</button>
      </span>` : `<span class="vsw-sub tip" data-tip-i18n="vpc.t.system" data-tip="read only">${esc(tr('vpc.system', 'system'))}</span>`;
    const vms = (s.consumers || []);
    return `<div class="vsw-pg vsw-link${s.ready ? '' : ' warn'}" data-subnet="${esc(s.name)}">
      <div class="vsw-pg-head">${val(s.name, 'vsw-name')} ${actions}</div>
      <div class="vsw-tags"><span>${esc(s.cidr || '?')}</span>${tags.map(x => `<span>${esc(x)}</span>`).join('')}</div>
      <div class="vsw-kv"><span>${esc(tr('fabric.gateway', 'Gateway'))}</span>${val(s.gateway || '-')}</div>
      <div class="vsw-kv"><span>${esc(tr('vpc.network', 'Network'))}</span>${s.network ? val(s.network)
        : `<span class="${s.provider === 'ovn' ? '' : 'warn'}">${esc(s.provider === 'ovn' ? tr('vpc.podNetwork', 'pods (primary)') : tr('vpc.noNetwork', 'none'))}</span>`}</div>
      ${usage(s)}
      ${vms.length ? `<div class="vsw-vms-title">${esc(tr('fabric.vms', 'Virtual machines'))}</div>
        <ul class="vsw-vms">${vms.slice(0, 8).map(c => `<li><span class="vsw-dot"></span>${esc(c.namespace)}/${esc(c.owner)}<small>${esc(c.address || '')}</small></li>`).join('')}
        ${vms.length > 8 ? `<li class="more">+${vms.length - 8}</li>` : ''}</ul>` : ''}
    </div>`;
  }

  function exitHtml(v) {
    if (v.name === DEFAULT_VPC) {
      const nat = v.subnet_list.filter(s => s.nat).map(s => s.name);
      return `<div class="vsw-internal">${esc(tr('vpc.exitDefault', 'Default VPC: subnets with outgoing NAT leave through the nodes.'))}
        <div class="vsw-kv"><span>NAT</span>${esc(nat.join(', ') || '-')}</div></div>`;
    }
    const routes = v.static_routes || [];
    const peers = v.peerings || [];
    if (!routes.length && !peers.length) {
      return `<div class="vsw-internal">${esc(tr('vpc.exitIsolated', 'Isolated: no route out, its VMs only see each other.'))}</div>`;
    }
    return `<div class="vsw-bond vsw-link"><div class="vsw-bond-head">${esc(tr('vpc.routes', 'Routes'))}</div>
      ${routes.map(r => `<div class="vsw-kv"><span>${esc(r.cidr)}</span>-> ${val(r.next_hop)}</div>`).join('')}
      ${peers.map(p => `<div class="vsw-kv"><span>${esc(tr('vpc.peer', 'peer'))}</span>${esc(p.remote)} (${esc(p.local_ip)})</div>`).join('')}
    </div>`;
  }

  function blockHtml(v) {
    const editable = !v.system;
    return `<section class="vsw vsw-net vpc-block${v.name === DEFAULT_VPC ? '' : ' vsw-overlay'}" data-vpc="${esc(v.name)}">
      <header class="vsw-head">
        <span class="vsw-kind">VPC</span>
        ${val(v.name, 'vsw-title')}
        <span class="vsw-sub">${esc(v.name === DEFAULT_VPC ? tr('vpc.defaultVpc', 'default VPC of kube-ovn') : (v.namespaces.length ? tr('vpc.namespaces', 'namespaces: {list}', { list: v.namespaces.join(', ') }) : tr('vpc.allNamespaces', 'all namespaces')))}</span>
        <span class="vsw-sub vsw-ports">${esc(tr('vpc.subnetCount', '{n} subnet(s)', { n: v.subnet_list.length }))}</span>
        <span class="vpc-actions">
          <button type="button" class="btn btn-secondary btn-sm tip needs-admin" data-vpc-new-subnet="${esc(v.name)}"
                  data-tip-i18n="vpc.t.newSubnetIn" data-tip="New subnet">${Icons.svg('add', { size: 13 })} ${esc(tr('vpc.newSubnet', 'Subnet'))}</button>
          ${editable ? `<button type="button" class="btn-icon-sm tip needs-admin" data-vpc-edit="${esc(v.name)}" data-tip-i18n="vpc.t.editVpc" data-tip="Edit">${Icons.svg('edit', { size: 13 })}</button>
          <button type="button" class="btn-icon-sm tip needs-admin" data-vpc-delete="${esc(v.name)}" data-tip-i18n="vpc.t.deleteVpc" data-tip="Delete">${Icons.svg('delete', { size: 13 })}</button>` : ''}
        </span>
      </header>
      <div class="vsw-body">
        <div class="vsw-col vsw-left">
          <div class="vsw-col-title">${esc(tr('vpc.subnets', 'Subnets and their VMs'))}</div>
          ${v.subnet_list.map(subnetCard).join('') || `<div class="vsw-empty">${esc(tr('vpc.noSubnet', 'no subnet yet'))}</div>`}
        </div>
        <div class="vsw-spine" aria-hidden="true"></div>
        <div class="vsw-col vsw-right">
          <div class="vsw-col-title">${esc(tr('netmap.exit', 'Leaves through'))}</div>
          ${exitHtml(v)}
        </div>
      </div>
    </section>`;
  }

  function findingsHtml(list) {
    if (!list.length) return '';
    return `<div class="vpc-findings">${list.map(f => `
      <div class="sto-finding ${LEVEL_CLASS[f.level] || 'sev-watch'}" data-code="${esc(f.code)}">
        <div class="sto-finding-title">${esc(findingText(f))}
          ${f.code === 'overlay-no-subnet' ? `<button type="button" class="btn btn-secondary btn-sm tip needs-admin" data-vpc-serve="${esc(f.network)}"
            data-tip-i18n="vpc.t.serve" data-tip="Create its subnet">${esc(tr('vpc.serve', 'Create its subnet'))}</button>` : ''}
        </div></div>`).join('')}</div>`;
  }

  function render(d) {
    const body = host && host.querySelector('.fabric-body');
    if (!body) return;
    if (!d.kubeovn) {
      body.innerHTML = `<p class="hint">${esc(tr('vpc.noKubeovn', 'kube-ovn is not enabled on this cluster (Harvester add-on kubeovn-operator).'))}</p>`;
      return;
    }
    const m = d.model || { vpcs: [], findings: [] };
    const scroll = body.scrollTop;
    body.innerHTML = findingsHtml(m.findings || []) + m.vpcs.map(blockHtml).join('');
    body.scrollTop = scroll;
    applyTips(body);
    const meta = host.querySelector('.fabric-meta');
    if (meta) {
      const n = m.vpcs.reduce((a, v) => a + v.subnet_list.length, 0);
      meta.textContent = `${m.vpcs.length} VPC · ${tr('vpc.subnetCount', '{n} subnet(s)', { n })}`;
    }
  }

  // -------------------------------------------------------------------------
  // Formulaires
  // -------------------------------------------------------------------------
  // La bulle arrive déjà traduite : le formulaire est reconstruit à chaque
  // ouverture, et un appel `tr('vpc.t.…')` littéral reste visible au
  // contrôle de parité des traductions.
  const field = (label, input, tip, extra = '') => {
    const attrs = `class="tip" data-tip="${esc(tip)}"`;
    return `<label class="tf-field"><span class="tf-label">${esc(label)} ${extra}</span>${input
      .replace('<input', `<input ${attrs}`).replace('<select', `<select ${attrs}`)
      .replace('<textarea', `<textarea ${attrs}`)}</label>`;
  };

  function subnetForm(d, current, preset) {
    const m = d.model;
    const s = current || {};
    const editing = !!current;
    const vpcs = m.vpcs.map(v => v.name);
    const vpc0 = s.vpc || preset.vpc || DEFAULT_VPC;
    const free = d.free_overlays || [];
    const net0 = s.network || preset.network || '';
    const nsOpts = (d.namespaces || []).filter(n => !/^(cattle-|kube-|harvester-|longhorn-|fleet-|rke2-|caphv-|capi-|cert-manager)/.test(n) || n === 'default');
    const netOptions = editing
      ? `<option value="existing:${esc(s.network || '')}" selected>${esc(s.network || tr('vpc.noNetwork', 'none'))}</option>`
      : `<option value="new">${esc(tr('vpc.newNetworkOpt', 'New overlay network, named after the subnet'))}</option>`
        + free.map(n => `<option value="existing:${esc(n)}" ${n === net0 ? 'selected' : ''}>${esc(n)}</option>`).join('');
    return `
      <div class="tf-form vpc-form" data-kind="subnet">
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.essentials', 'Essentials'))}</legend><div class="tf-args">
          ${field(tr('vpc.f.name', 'Name'), `<input data-x="name" type="text" value="${esc(s.name || '')}" ${editing ? 'disabled' : ''} placeholder="lab-a" autocomplete="off" spellcheck="false">`, tr('vpc.t.name', 'Lower case letters, digits and dashes.'))}
          ${field('VPC', `<select data-x="vpc" ${editing ? 'disabled' : ''}>${vpcs.map(v => `<option ${v === vpc0 ? 'selected' : ''}>${esc(v)}</option>`).join('')}</select>`, tr('vpc.t.vpc', 'The VPC routes between its subnets; subnets of different VPCs do not see each other.'))}
          ${field('CIDR', `<input data-x="cidr" type="text" value="${esc(s.cidr || d.suggested_cidr || '')}" ${editing ? 'disabled' : ''}>`, tr('vpc.t.cidr', 'Address range of the subnet. A free /24 is proposed, away from the nodes, the pods and the other subnets.'), editing ? '' : `<span class="capi-derived" data-x="cidr-derived">${esc(tr('vpc.proposed', 'proposed'))}</span>`)}
          ${field(tr('fabric.gateway', 'Gateway'), `<input data-x="gateway" type="text" value="${esc(s.gateway || '')}" ${editing ? 'disabled' : ''}>`, tr('vpc.t.gateway', 'Address of the subnet router, the first host by default. It is excluded from allocation.'), editing ? '' : `<span class="capi-derived" data-x="gw-derived">${esc(tr('vpc.derived', 'derived'))}</span>`)}
          ${field(tr('vpc.f.network', 'Overlay network'), `<select data-x="network_choice" ${editing ? 'disabled' : ''}>${netOptions}</select>`, tr('vpc.t.network', 'The network VMs attach to (a Harvester overlay network). One subnet per network.'))}
          ${editing ? '' : field(tr('vpc.f.newNetwork', 'New network (namespace/name)'), `<input data-x="new_network" type="text" value="">`, tr('vpc.t.newNetwork', 'Where the overlay network is created; VMs of every namespace can use it.'))}
        </div>
        <div class="tf-args">
          <label class="tf-field tf-type-bool tip" data-tip-i18n="vpc.t.nat" data-tip="Outgoing NAT"><input data-x="nat" type="checkbox" ${s.nat || (!editing && vpc0 === DEFAULT_VPC) ? 'checked' : ''}><span class="tf-label">${esc(tr('vpc.f.nat', 'Outgoing NAT (Internet access through the nodes)'))}</span></label>
          <label class="tf-field tf-type-bool tip" data-tip-i18n="vpc.t.dhcp" data-tip="DHCP"><input data-x="dhcp" type="checkbox" ${editing ? (s.dhcp ? 'checked' : '') : 'checked'}><span class="tf-label">${esc(tr('vpc.f.dhcp', 'DHCP for the VMs'))}</span></label>
        </div></fieldset>
        <details class="tf-block capi-advanced">
          <summary class="tip" data-tip-i18n="vpc.t.advanced" data-tip="Advanced">${esc(tr('capi.new.sec.advanced', 'Advanced options'))}</summary>
          <div class="tf-args">
            ${field(tr('vpc.f.exclude', 'Excluded addresses'), `<input data-x="exclude" type="text" value="${esc((s.exclude || []).join(', '))}" placeholder="10.200.0.2..10.200.0.9">`, tr('vpc.t.exclude', 'Addresses never given to a VM, separated by commas; a range is written a..b. The gateway is always excluded.'))}
            <label class="tf-field tf-type-bool tip" data-tip-i18n="vpc.t.private" data-tip="Private"><input data-x="private" type="checkbox" ${s.private ? 'checked' : ''}><span class="tf-label">${esc(tr('vpc.f.private', 'Private (refuse traffic from other subnets)'))}</span></label>
            ${field(tr('vpc.f.allow', 'Allowed subnets'), `<input data-x="allow" type="text" value="${esc((s.allow || []).join(', '))}" placeholder="10.200.1.0/24">`, tr('vpc.t.allow', 'Networks still allowed to reach a private subnet.'))}
            ${field(tr('vpc.f.namespaces', 'Reserved to namespaces'), `<select data-x="namespaces" multiple size="3">${nsOpts.map(n => `<option ${(s.namespaces || []).includes(n) ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`, tr('vpc.t.namespaces', 'Pods of these namespaces get their address from this subnet. Leave empty for VMs attached by network.'))}
          </div>
        </details>
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.check', 'Pre-check'))}</legend><div class="xfer-report" data-x="report"></div></fieldset>
      </div>`;
  }

  function vpcForm(d, current) {
    const v = current || {};
    const editing = !!current;
    const nsOpts = (d.namespaces || []).filter(n => !/^(cattle-|kube-|harvester-|longhorn-|fleet-|rke2-|caphv-|capi-|cert-manager)/.test(n) || n === 'default');
    const others = d.model.vpcs.map(x => x.name).filter(n => n !== v.name);
    return `
      <div class="tf-form vpc-form" data-kind="vpc">
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.essentials', 'Essentials'))}</legend><div class="tf-args">
          ${field(tr('vpc.f.name', 'Name'), `<input data-x="name" type="text" value="${esc(v.name || '')}" ${editing ? 'disabled' : ''} placeholder="lab" autocomplete="off" spellcheck="false">`, tr('vpc.t.vpcName', 'A VPC is an isolated router: its subnets see each other, not those of other VPCs.'))}
          ${field(tr('vpc.f.namespaces', 'Reserved to namespaces'), `<select data-x="namespaces" multiple size="3">${nsOpts.map(n => `<option ${(v.namespaces || []).includes(n) ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select>`, tr('vpc.t.vpcNamespaces', 'Namespaces whose pods use this VPC. Leave empty: VMs join it through their network.'))}
        </div></fieldset>
        <details class="tf-block capi-advanced" ${(v.static_routes || []).length || (v.peerings || []).length ? 'open' : ''}>
          <summary class="tip" data-tip-i18n="vpc.t.advanced" data-tip="Advanced">${esc(tr('capi.new.sec.advanced', 'Advanced options'))}</summary>
          <div class="tf-args">
            ${field(tr('vpc.f.routes', 'Static routes, one per line'), `<textarea data-x="routes" rows="3" placeholder="0.0.0.0/0 -> 10.255.0.2">${esc((v.static_routes || []).map(r => `${r.cidr} -> ${r.next_hop}`).join('\n'))}</textarea>`, tr('vpc.t.routes', 'network -> next hop. With a peering, lets the VPC reach the other one.'))}
            ${field(tr('vpc.f.peerings', 'Peerings, one per line'), `<textarea data-x="peerings" rows="2" placeholder="${esc(others[0] || DEFAULT_VPC)} @ 10.255.0.1/30">${esc((v.peerings || []).map(p => `${p.remote} @ ${p.local_ip}`).join('\n'))}</textarea>`, tr('vpc.t.peerings', 'other VPC @ local address/prefix of the link between the two routers. Declare it on both VPCs.'))}
          </div>
        </details>
        <fieldset class="tf-block"><legend>${esc(tr('capi.new.sec.check', 'Pre-check'))}</legend><div class="xfer-report" data-x="report"></div></fieldset>
      </div>`;
  }

  function readForm(root, kind) {
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    const multi = (x) => q(x) ? [...q(x).selectedOptions].map(o => o.value) : [];
    if (kind === 'vpc') {
      return {
        name: q('name').value.trim(),
        namespaces: multi('namespaces'),
        static_routes: q('routes').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
          const [cidr, hop] = l.split(/\s*-?>\s*/);
          return { cidr: cidr || '', next_hop: hop || '' };
        }),
        peerings: q('peerings').value.split('\n').map(l => l.trim()).filter(Boolean).map(l => {
          const [remote, ip] = l.split(/\s*@\s*/);
          return { remote: remote || '', local_ip: ip || '' };
        }),
      };
    }
    const choice = q('network_choice').value;
    const body = {
      name: q('name').value.trim(), vpc: q('vpc').value, cidr: q('cidr').value.trim(),
      gateway: q('gateway').value.trim(), exclude: q('exclude').value,
      nat: q('nat').checked, dhcp: q('dhcp').checked, private: q('private').checked,
      allow: q('allow').value, namespaces: multi('namespaces'),
    };
    if (choice.startsWith('existing:')) body.network = choice.slice(9);
    else body.new_network = (q('new_network') && q('new_network').value.trim()) || '';
    if (!body.network && !body.new_network) delete body.new_network;
    return body;
  }

  function openForm(kind, current, preset = {}) {
    const d = lastData;
    if (!d || !d.model) return;
    const editing = !!current;
    const title = kind === 'vpc'
      ? (editing ? tr('vpc.editVpcTitle', 'Edit VPC {name}', { name: current.name }) : tr('vpc.newVpcTitle', 'New VPC'))
      : (editing ? tr('vpc.editSubnetTitle', 'Edit subnet {name}', { name: current.name }) : tr('vpc.newSubnetTitle', 'New subnet'));
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active vpc-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${Icons.svg('network')} ${esc(title)}</h3>
          <div class="modal-subtitle">${esc(cluster)}</div></div>
          <button type="button" class="btn-close tip" data-vpc-close data-tip-i18n="common.cancel" data-tip="Cancel">×</button></div>
        <div class="modal-body">${kind === 'vpc' ? vpcForm(d, current) : subnetForm(d, current, preset)}</div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm" data-vpc-close>${esc(tr('common.cancel', 'Cancel'))}</button>
          <button type="button" class="btn btn-primary btn-sm tip" data-vpc-save disabled data-tip-i18n="vpc.t.save" data-tip="Save">${Icons.svg('ok', { size: 13 })} ${esc(editing ? tr('vpc.save', 'Save') : tr('vpc.create', 'Create'))}</button>
          <span class="apply-result" data-x="feedback"></span>
        </div>
      </div>`;
    document.body.appendChild(modal);
    applyTips(modal);
    const root = modal.querySelector('.vpc-form');
    const q = (x) => root.querySelector(`[data-x="${x}"]`);
    const seq = { n: 0, t: null };

    if (kind === 'subnet' && !editing) {
      // Passerelle déduite du CIDR, nouveau réseau nommé d'après le subnet,
      // NAT proposé dans le VPC par défaut seulement : tant qu'on n'y touche pas.
      const derive = () => {
        const m = /^(\d+)\.(\d+)\.(\d+)\.(\d+)\/(\d+)$/.exec(q('cidr').value.trim());
        if (m && !q('gateway').dataset.touched) {
          const n = ((+m[1] << 24) >>> 0) + (+m[2] << 16) + (+m[3] << 8) + (+m[4]);
          const bits = +m[5];
          const base = bits === 0 ? 0 : (n & (~0 << (32 - bits))) >>> 0;
          const g = base + 1;
          q('gateway').value = [g >>> 24, (g >> 16) & 255, (g >> 8) & 255, g & 255].join('.');
        }
        const nn = q('new_network');
        if (nn && !nn.dataset.touched) nn.value = `default/${q('name').value.trim() || 'network'}`;
        if (nn) nn.closest('.tf-field').hidden = q('network_choice').value !== 'new';
        q('nat').disabled = q('vpc').value !== DEFAULT_VPC;
        if (q('nat').disabled) q('nat').checked = false;
      };
      q('gateway').addEventListener('input', () => { q('gateway').dataset.touched = '1'; q('gw-derived').hidden = true; });
      q('cidr').addEventListener('input', () => { q('cidr-derived').hidden = true; });
      if (q('new_network')) q('new_network').addEventListener('input', () => { q('new_network').dataset.touched = '1'; });
      q('name').addEventListener('input', () => { q('name').value = q('name').value.toLowerCase().replace(/[^a-z0-9-]/g, '-'); });
      ['cidr', 'name', 'vpc', 'network_choice'].forEach(x => q(x).addEventListener('input', derive));
      ['vpc', 'network_choice'].forEach(x => q(x).addEventListener('change', derive));
      derive();
    }
    if (kind === 'vpc' && !editing) {
      q('name').addEventListener('input', () => { q('name').value = q('name').value.toLowerCase().replace(/[^a-z0-9-]/g, '-'); });
    }

    async function check() {
      const n = ++seq.n;
      const report = q('report');
      const body = readForm(root, kind);
      if (!body.name) {
        report.innerHTML = `<p class="tf-desc">${esc(tr('vpc.needName', 'Give it a name to run the pre-check.'))}</p>`;
        modal.querySelector('[data-vpc-save]').disabled = true;
        return;
      }
      report.classList.add('is-checking');
      try {
        const r = await fetch(`/api/kubeovn/${enc(cluster)}/check`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, spec: body, update: editing }) });
        const out = await r.json();
        if (n !== seq.n || !modal) return;
        if (!r.ok) throw new Error(out.error || `HTTP ${r.status}`);
        const items = (out.findings || []).slice().sort((a, b) => LEVEL_RANK[a.level] - LEVEL_RANK[b.level]);
        report.innerHTML = items.map(f => `<div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}" data-level="${esc(f.level)}">
            <div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('')
          + (out.blocked ? '' : `<div class="sto-finding sev-info" data-code="ok"><div class="sto-finding-title">${esc(tr('vpc.noBlocker', 'No blocker.'))}</div></div>`);
        modal.querySelector('[data-vpc-save]').disabled = !!out.blocked;
      } catch (e) {
        if (n !== seq.n || !modal) return;
        report.innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(tr('vpc.error', 'Error: {msg}', { msg: e.message }))}</div></div>`;
      } finally {
        report.classList.remove('is-checking');
      }
    }
    const later = () => { clearTimeout(seq.t); seq.t = setTimeout(check, 500); };
    root.querySelectorAll('input, select, textarea').forEach(el => {
      el.addEventListener('input', later);
      el.addEventListener('change', later);
    });

    modal.addEventListener('click', async (e) => {
      if (e.target.closest('[data-vpc-close]') || e.target === modal) { closeModal(); return; }
      if (!e.target.closest('[data-vpc-save]')) return;
      const btn = e.target.closest('[data-vpc-save]');
      btn.disabled = true;
      const body = readForm(root, kind);
      try {
        const r = await fetch(`/api/kubeovn/${enc(cluster)}/apply`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind, spec: body, update: editing }) });
        const out = await r.json();
        if (!r.ok) throw new Error(out.error || out.hint || `HTTP ${r.status}`);
        closeModal();
        follow(out.action_id, kind === 'vpc' ? tr('vpc.doneVpc', 'VPC {name} saved.', { name: body.name })
                                             : tr('vpc.doneSubnet', 'Subnet {name} saved.', { name: body.name }));
      } catch (err) {
        modal.querySelector('[data-x="feedback"]').innerHTML = `<span style="color:var(--danger)">${esc(tr('vpc.error', 'Error: {msg}', { msg: err.message }))}</span>`;
        btn.disabled = false;
      }
    });
    check();
  }

  function closeModal() {
    if (modal) modal.remove();
    modal = null;
  }

  // -------------------------------------------------------------------------
  // Suivi d'une action
  // -------------------------------------------------------------------------
  function say(html) {
    const fb = host && host.querySelector('.vpc-feedback');
    if (fb) fb.innerHTML = html;
  }

  function follow(actionId, doneText) {
    say(`${esc(tr('vpc.started', 'Running (action {id}), see the dock.', { id: actionId }))}`);
    if (!window.SSEReconnect) return;
    let lastError = '';
    const es = SSEReconnect.connect(`/api/stream/${enc(actionId)}`, {
      on: {
        step: (e) => {
          try {
            const s = JSON.parse(e.data);
            if (s.status !== 'error') return;
            // Le script écrit un refus sous la forme « code {faits} » : la
            // page le redit dans la langue de l'interface.
            const m = /^([a-z0-9-]+) (\{.*\})$/.exec(s.message || '');
            let facts = null;
            try { facts = m ? JSON.parse(m[2]) : null; } catch { facts = null; }
            lastError = facts && FINDINGS[m[1]] ? findingText({ code: m[1], facts })
                                               : (s.message || lastError);
          } catch { /* ligne illisible */ }
        },
        end: (e) => {
          let d = {};
          try { d = JSON.parse(e.data); } catch { /* fin sans détail */ }
          es.close();
          say(d.status === 'done'
            ? `<span style="color:var(--accent)">${Icons.svg('ok', { size: 13 })} ${esc(doneText)}</span>`
            : `<span style="color:var(--danger)">${Icons.svg('fail', { size: 13 })} ${esc(tr('vpc.failed', 'Failed: {msg}', { msg: lastError || d.error_summary || d.status || '?' }))}</span>`);
          refresh(true);
        },
      },
    });
  }

  async function remove(kind, name, network) {
    // Ce que la vue sait déjà : inutile d'envoyer une suppression refusée.
    const s = kind === 'subnet' ? findSubnet(name) : null;
    const v = kind === 'vpc' ? ((lastData && lastData.model.vpcs) || []).find(x => x.name === name) : null;
    const known = s && (s.consumers || []).length
      ? { code: 'subnet-in-use', facts: { subnet: name, users: s.consumers.map(c => `${c.namespace}/${c.owner}`).join(', ') } }
      : (v && v.subnet_list.length
        ? { code: 'vpc-has-subnets', facts: { vpc: name, subnets: v.subnet_list.map(x => x.name).join(', ') } } : null);
    if (known) {
      say(`<span style="color:var(--danger)">${Icons.svg('fail', { size: 13 })} ${esc(findingText(known))}</span>`);
      return;
    }
    const question = kind === 'vpc'
      ? tr('vpc.confirmVpc', 'Delete VPC {name}? It must have no subnet left.', { name })
      : tr('vpc.confirmSubnet', 'Delete subnet {name}? It must have no VM left.', { name });
    if (!confirm(question)) return;
    let withNet = false;
    if (kind === 'subnet' && network) {
      withNet = confirm(tr('vpc.confirmNetwork', 'Also delete its overlay network {network} (only if the console created it)?', { network }));
    }
    try {
      const r = await fetch(`/api/kubeovn/${enc(cluster)}/${kind}/${enc(name)}${withNet ? '?with_network=1' : ''}`, { method: 'DELETE' });
      const out = await r.json();
      if (!r.ok) throw new Error(out.error || out.hint || `HTTP ${r.status}`);
      follow(out.action_id, tr('vpc.deleted', '{name} deleted.', { name }));
    } catch (e) {
      say(`<span style="color:var(--danger)">${esc(tr('vpc.error', 'Error: {msg}', { msg: e.message }))}</span>`);
    }
  }

  // -------------------------------------------------------------------------
  // Cycle de vie
  // -------------------------------------------------------------------------
  function findSubnet(name) {
    for (const v of (lastData && lastData.model && lastData.model.vpcs) || []) {
      const s = v.subnet_list.find(x => x.name === name);
      if (s) return s;
    }
    return null;
  }

  async function refresh(fresh = false) {
    if (!cluster || !host || document.hidden) return;
    const asked = cluster;
    try {
      const r = await fetch(`/api/kubeovn/${enc(asked)}${fresh ? '?fresh=1' : ''}`);
      const d = await r.json();
      if (asked !== cluster) return;
      if (!r.ok || d.unreachable || d.error) {
        const b = host.querySelector('.fabric-body');
        if (b) b.innerHTML = `<p class="hint warn">${esc(d.unreachable
          ? tr('fabric.unreachable', 'Cluster unreachable') : (d.error || 'HTTP ' + r.status))}</p>`;
        return;
      }
      lastData = d;
      render(d);
    } catch (e) {
      const b = host.querySelector('.fabric-body');
      if (b && !lastData) b.innerHTML = `<p class="hint warn">${esc(e.message || e)}</p>`;
    }
  }

  function shell() {
    host.innerHTML = `
      <div class="fabric-toolbar">
        <span class="fabric-meta"></span>
        <span class="apply-result vpc-feedback"></span>
        <button type="button" class="btn btn-sm btn-primary tip needs-admin" data-vpc-new
                data-tip-i18n="vpc.t.newVpc" data-tip="New VPC">${Icons.svg('add', { size: 14 })} ${esc(tr('vpc.newVpc', 'New VPC'))}</button>
        <button type="button" class="btn btn-sm fabric-refresh tip"
                data-tip-i18n="topology.refreshTip">${Icons.svg('refresh', { size: 14 })} ${esc(tr('topology.refresh', 'Refresh'))}</button>
      </div>
      <div class="fabric-body board-full"></div>`;
    applyTips(host);
    if (window.CopyTo) CopyTo.wire(host);
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy]')) return;
      if (e.target.closest('.fabric-refresh')) { refresh(true); return; }
      if (e.target.closest('[data-vpc-new]')) { openForm('vpc'); return; }
      const t = (sel) => e.target.closest(sel);
      let el;
      if ((el = t('[data-vpc-new-subnet]'))) { openForm('subnet', null, { vpc: el.dataset.vpcNewSubnet }); return; }
      if ((el = t('[data-vpc-serve]'))) { openForm('subnet', null, { network: el.dataset.vpcServe }); return; }
      if ((el = t('[data-vpc-edit-subnet]'))) { openForm('subnet', findSubnet(el.dataset.vpcEditSubnet)); return; }
      if ((el = t('[data-vpc-delete-subnet]'))) {
        const s = findSubnet(el.dataset.vpcDeleteSubnet);
        remove('subnet', el.dataset.vpcDeleteSubnet, s && s.network);
        return;
      }
      if ((el = t('[data-vpc-edit]'))) {
        const v = lastData.model.vpcs.find(x => x.name === el.dataset.vpcEdit);
        openForm('vpc', v);
        return;
      }
      if ((el = t('[data-vpc-delete]'))) remove('vpc', el.dataset.vpcDelete);
    });
  }

  function start(clusterName) {
    const h = document.querySelector('.overview-subtab[data-subtab="vpc"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) lastData = null;
    cluster = clusterName;
    if (host !== h || !h.querySelector('.fabric-body')) { host = h; shell(); }
    if (!lastData) {
      host.querySelector('.fabric-body').innerHTML =
        `<p class="hint">${esc(tr('topology.loading', 'Loading...', { name: cluster }))}</p>`;
    }
    if (timer) clearInterval(timer);
    timer = setInterval(() => { if (!modal) refresh(); }, REFRESH_MS);
    return refresh();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return { start, stop, refresh, openForm, _findingText: findingText };
})();

if (typeof window !== 'undefined') window.VpcBoard = VpcBoard;
