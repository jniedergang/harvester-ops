/**
 * harvester-ops : la vue Réseau, un bloc par réseau, lue de gauche à droite.
 *
 *   VMs et leurs cartes virtuelles  |  le réseau  |  par où il sort
 *
 * La Fabrique regarde l'hôte (switchs, uplinks, cartes) ; celle-ci regarde
 * les VMs : quelle adresse a telle VM sur tel réseau, avec quelle MAC, par
 * quelle interface invitée, et le lien est-il monté. C'est la question
 * qu'on se pose avant un ping qui ne répond pas.
 *
 * Même donnée que la Fabrique (`/api/network-fabric`), donc aucun appel de
 * plus au cluster : les VMs y viennent avec leurs interfaces vivantes.
 */
const NetMap = (() => {
  const REFRESH_MS = 8000;
  const { tr, esc, val, applyTips } = window.Board;
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;

  // -------------------------------------------------------------------------
  // Modèle : un bloc par réseau attachable, plus le réseau de pod
  // -------------------------------------------------------------------------
  function buildModel(d) {
    const links = d.links || [];
    const nets = d.networks || [];
    const vms = d.vms || [];
    const ovn = d.kubeovn || {};
    const key = (n) => n.namespace + '/' + n.name;

    const subnetOf = (nad) => (ovn.subnets || []).find(s =>
      s.provider && nad.provider && s.provider === nad.provider);
    const vlanOf = (s) => (ovn.vlans || []).find(v => v.name === s.vlan);
    const pnOf = (v) => (ovn.provider_networks || []).find(p => p.name === v.provider_network);
    const childrenOf = (node, name) =>
      links.filter(l => l.node === node && l.master === name && l.layer !== 5);

    // Par où sort le réseau : la chaîne bridge -> bond -> cartes pour un
    // réseau classique, le subnet et sa carte pour un underlay kube-ovn,
    // rien pour l'overlay (il est encapsulé sur le réseau des nœuds).
    function exitOf(nad) {
      if (nad.fabric === 'classic') {
        const nodes = [...new Set(links.filter(l => l.name === nad.bridge).map(l => l.node))];
        return {
          kind: 'bridge', bridge: nad.bridge, clusterNetwork: nad.cluster_network,
          perNode: nodes.map(node => ({
            node,
            uplinks: childrenOf(node, nad.bridge).map(l => ({
              link: l, members: l.type === 'bond' ? childrenOf(node, l.name) : [] })),
          })),
        };
      }
      const s = subnetOf(nad);
      if (!s) return { kind: 'unbound' };
      if (s.overlay) return { kind: 'overlay', subnet: s };
      const v = vlanOf(s);
      const pn = v && pnOf(v);
      const cards = pn ? links.filter(l => l.name === pn.default_interface) : [];
      return { kind: 'underlay', subnet: s, vlan: v, pn, cards };
    }

    function tagOf(nad) {
      if (nad.fabric === 'classic') {
        return nad.vlan ? 'VLAN ' + nad.vlan : tr('fabric.untagged', 'untagged');
      }
      const s = subnetOf(nad);
      if (!s) return tr('fabric.noSubnet', 'no subnet bound');
      if (s.overlay) return tr('fabric.overlay', 'OVN overlay');
      const v = vlanOf(s);
      return v && v.id ? 'VLAN ' + v.id : tr('fabric.untagged', 'untagged');
    }

    // Une VM par réseau, avec SES cartes sur ce réseau seulement.
    const attachOf = (target, pod) => vms
      .map(vm => ({ vm, nics: (vm.networks || []).filter(n =>
        pod ? n.pod : n.network === target) }))
      .filter(x => x.nics.length)
      .sort((a, b) => ((b.vm.status === 'Running') - (a.vm.status === 'Running'))
        || (a.vm.namespace + a.vm.name).localeCompare(b.vm.namespace + b.vm.name));

    const blocks = nets.map(nad => ({
      id: 'nad-' + key(nad), title: key(nad), nad, tag: tagOf(nad),
      ready: nad.ready, exit: exitOf(nad), attached: attachOf(key(nad), false),
    }));
    const pod = attachOf(null, true);
    if (pod.length) {
      blocks.push({ id: 'pod', title: tr('fabric.podNetwork', 'Pod network'),
                    pod: true, tag: 'masquerade', exit: { kind: 'pod' }, attached: pod });
    }
    // Les réseaux sans VM restent listés, en bas et repliés : on vient
    // surtout voir ceux qui portent quelque chose.
    const used = blocks.filter(b => b.attached.length);
    const idle = blocks.filter(b => !b.attached.length);
    return { used, idle };
  }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  // Une ligne par carte virtuelle : c'est ce qu'on parcourt des yeux en
  // cherchant une adresse. En cartes empilées, 14 VMs arrêtées occupaient
  // deux écrans pour dire « pas d'adresse ».
  function nicRows(n) {
    const ips = (n.ips || []).map(ip => val(ip)).join('');
    const link = n.link_state
      ? `<span class="vsw-state ${n.link_state === 'up' ? 'up' : 'down'}">${esc(n.link_state)}</span>` : '';
    return `<div class="net-nic">
      <b class="net-nic-name">${esc(n.nic)}</b>
      <span class="net-guest">${n.guest_iface ? esc(n.guest_iface) : ''}</span>
      <span class="net-mac">${n.mac ? val(n.mac) : '<span class="muted">-</span>'}</span>
      <span class="net-ips">${ips}</span>
      <small class="net-model">${esc([n.model, n.binding].filter(Boolean).join(' · '))}</small>
      ${link}
    </div>`;
  }

  function vmCard(a) {
    const vm = a.vm;
    const run = vm.status === 'Running';
    const id = vm.namespace + '/' + vm.name;
    const others = (vm.guest_only || []).filter(g => (g.ips || []).length);
    return `<div class="vsw-pg vsw-link net-vm ${run ? 'running' : 'stopped'}">
      <div class="vsw-pg-head">
        <span class="vsw-dot" aria-hidden="true"></span>
        ${val(id, 'vsw-name')}
        <small>${esc(vm.status || '')}</small>
        <span class="net-right">
          ${vm.node ? `<small class="net-node">${esc(vm.node)}</small>` : ''}
          <button type="button" class="vsw-more tip" data-netmap-edit="${esc(id)}"
                  data-tip-i18n="netmap.editTip">${esc(tr('netmap.edit', 'edit'))}</button>
        </span>
      </div>
      ${a.nics.map(nicRows).join('')}
      ${others.length ? `<div class="net-guest-only">${esc(tr('netmap.guestOnly', 'Also inside the guest'))} :
        ${others.map(g => `${esc(g.iface || '?')} ${(g.ips || []).map(ip => val(ip)).join(' ')}`).join(' · ')}</div>` : ''}
    </div>`;
  }

  function linkBox(l, inBond) {
    const up = l.state === 'up';
    return `<div class="vsw-nic ${up ? 'up' : 'down'}${inBond ? ' in-bond' : ' vsw-link'}">
      <span class="vsw-dot" aria-hidden="true"></span>
      ${val(l.name, 'vsw-name')}
      <span class="vsw-speed">${esc(up ? tr('netmap.linkUp', 'up') : tr('fabric.noLink', 'no link'))}</span>
    </div>`;
  }

  function exitHtml(b) {
    const e = b.exit;
    if (e.kind === 'pod') {
      return `<div class="vsw-internal">${esc(tr('fabric.podNote',
        'no bridge: leaves through the node routing (NAT)'))}</div>`;
    }
    if (e.kind === 'overlay') {
      return `<div class="vsw-internal">${esc(tr('netmap.overlayExit',
        'Internal to the cluster (OVN overlay), no physical adapter.'))}
        <div class="vsw-kv"><span>${esc(tr('netmap.subnet', 'Subnet'))}</span>${val(e.subnet.name)}</div>
        <div class="vsw-kv"><span>CIDR</span>${val(e.subnet.cidr)}</div></div>`;
    }
    if (e.kind === 'unbound') {
      return `<div class="vsw-empty warn">${esc(tr('fabric.noSubnet', 'no subnet bound'))}</div>`;
    }
    if (e.kind === 'underlay') {
      return `<div class="vsw-bond vsw-link">
          <div class="vsw-bond-head">${val(e.subnet.name, 'vsw-name')}
            <small>${esc(e.pn ? tr('netmap.providerNetwork', 'provider network') + ' ' + e.pn.name : 'kube-ovn')}</small></div>
          <div class="vsw-kv"><span>CIDR</span>${val(e.subnet.cidr)}</div>
          <div class="vsw-kv"><span>${esc(tr('fabric.gateway', 'Gateway'))}</span>${val(e.subnet.gateway)}</div>
          ${e.cards.map(c => linkBox(c, true)).join('')
            || `<div class="vsw-empty warn">${esc(tr('fabric.noUplinkReported', 'no uplink reported'))}</div>`}
        </div>`;
    }
    // Réseau classique : le switch, puis sa chaîne d'uplinks.
    const many = e.perNode.length > 1;
    const chain = e.perNode.map(pn => `
      ${many ? `<div class="vsw-nodename">${val(pn.node)}</div>` : ''}
      ${pn.uplinks.map(u => u.link.type === 'bond'
        ? `<div class="vsw-bond in-switch"><div class="vsw-bond-head">${val(u.link.name, 'vsw-name')}
             <small>${esc(tr('fabric.bond', 'bond'))}</small></div>
             ${u.members.map(m => linkBox(m, true)).join('')}</div>`
        : linkBox(u.link, true)).join('')
        || `<div class="vsw-empty warn">${esc(tr('fabric.noUplinkReported', 'no uplink reported'))}</div>`}`).join('');
    return `<div class="vsw-bond vsw-link net-switch">
      <div class="vsw-bond-head">${val(e.bridge || '?', 'vsw-name')}
        <small>${esc(tr('fabric.virtualSwitch', 'Virtual switch'))}${e.clusterNetwork
          ? ' · ' + esc(tr('fabric.clusterNetwork', 'cluster network')) + ' ' + esc(e.clusterNetwork) : ''}</small>
      </div>
      ${chain || `<div class="vsw-empty warn">${esc(tr('fabric.noUplinkReported', 'no uplink reported'))}</div>`}
      <button type="button" class="vsw-more tip" data-netmap-fabric
              data-tip-i18n="netmap.toFabricTip">${esc(tr('netmap.toFabric', 'see it in Fabric'))}</button>
    </div>`;
  }

  function blockHtml(b) {
    return `<section class="vsw vsw-net${b.pod ? ' vsw-pod' : ''}" data-block="${esc(b.id)}">
      <header class="vsw-head">
        <span class="vsw-kind">${esc(tr('netmap.network', 'Network'))}</span>
        ${b.pod ? `<b class="vsw-title">${esc(b.title)}</b>` : val(b.title, 'vsw-title')}
        <span class="vsw-sub">${esc(b.tag)}</span>
        ${b.ready === false ? `<span class="vsw-badge warn">${esc(tr('fabric.notReady', 'not ready'))}</span>` : ''}
        <span class="vsw-sub vsw-ports">${b.attached.length} VM</span>
      </header>
      <div class="vsw-body">
        <div class="vsw-col vsw-left">
          <div class="vsw-col-title">${esc(tr('fabric.vms', 'Virtual machines'))}</div>
          ${b.attached.map(vmCard).join('')}
        </div>
        <div class="vsw-spine" aria-hidden="true"></div>
        <div class="vsw-col vsw-right">
          <div class="vsw-col-title">${esc(tr('netmap.exit', 'Leaves through'))}</div>
          ${exitHtml(b)}
        </div>
      </div>
    </section>`;
  }

  function render(d) {
    const body = host && host.querySelector('.fabric-body');
    if (!body) return;
    const m = buildModel(d);
    const idle = m.idle.length ? `
      <section class="vsw vsw-unused">
        <header class="vsw-head"><span class="vsw-kind">${esc(tr('netmap.idle', 'Networks with no VM'))}</span></header>
        <div class="vsw-unused-list">${m.idle.map(b => `<div class="vsw-pg" data-block="${esc(b.id)}">
          ${b.pod ? esc(b.title) : val(b.title, 'vsw-name')}
          <div class="vsw-tags"><span>${esc(b.tag)}</span></div></div>`).join('')}</div>
      </section>` : '';
    const scroll = body.scrollTop;
    body.innerHTML = (m.used.map(blockHtml).join('') + idle)
      || `<p class="hint">${esc(tr('netmap.none', 'No network on this cluster.'))}</p>`;
    body.scrollTop = scroll;
    applyTips(body);
    const meta = host.querySelector('.fabric-meta');
    if (meta) {
      const n = (d.vms || []).filter(v => (v.networks || []).length).length;
      meta.textContent = `${m.used.length + m.idle.length} ${tr('netmap.networks', 'networks')} · ${n} VM`;
    }
  }

  // -------------------------------------------------------------------------
  // Cycle de vie
  // -------------------------------------------------------------------------
  async function refresh() {
    if (!cluster || !host || document.hidden) return;
    const asked = cluster;
    try {
      const r = await fetch(`/api/network-fabric/${encodeURIComponent(asked)}`);
      const d = await r.json();
      if (asked !== cluster) return;
      if (!r.ok || d.unreachable || d.error) {
        const b = host.querySelector('.fabric-body');
        if (b) b.innerHTML = `<p class="hint warn">${esc(d.unreachable
          ? tr('fabric.unreachable', 'Cluster unreachable')
          : (d.error || 'HTTP ' + r.status))}</p>`;
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
    const icon = window.Icons ? Icons.svg('refresh', { size: 14 }) : '';
    host.innerHTML = `
      <div class="fabric-toolbar">
        <span class="fabric-meta"></span>
        <button type="button" class="btn btn-sm fabric-refresh tip"
                data-tip-i18n="topology.refreshTip">${icon} ${esc(tr('topology.refresh', 'Refresh'))}</button>
      </div>
      <div class="fabric-body board-full"></div>`;
    applyTips(host);
    if (window.CopyTo) CopyTo.wire(host);
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy]')) return;
      if (e.target.closest('.fabric-refresh')) { refresh(); return; }
      const edit = e.target.closest('[data-netmap-edit]');
      if (edit && window.VMEdit) {
        const [ns, name] = edit.dataset.netmapEdit.split('/');
        window.VMEdit.open(cluster, ns, name);
        return;
      }
      // v1.57.0 : la fabrique est l'onglet Underlay de la section Network
      if (e.target.closest('[data-netmap-fabric]') && window.Sections) {
        window.Sections.open('network', 'underlay');
      }
    });
  }

  function start(clusterName) {
    const h = document.querySelector('[data-board="network"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) lastData = null;
    cluster = clusterName;
    if (host !== h || !h.querySelector('.fabric-body')) { host = h; shell(); }
    if (!lastData) {
      host.querySelector('.fabric-body').innerHTML =
        `<p class="hint">${esc(tr('topology.loading', 'Loading...', { name: cluster }))}</p>`;
    }
    if (timer) clearInterval(timer);
    timer = setInterval(refresh, REFRESH_MS);
    return refresh();
  }

  function stop() {
    if (timer) clearInterval(timer);
    timer = null;
  }

  return { start, stop, refresh, _buildModel: buildModel };
})();

if (typeof window !== 'undefined') window.NetMap = NetMap;
