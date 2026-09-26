/**
 * harvester-ops : la fabrique réseau d'un hôte, à la manière des vSwitch.
 *
 * Première version (v1.35.0 à 1.38.0) : un graphe Cytoscape empilé par
 * couches. Jugée « pas très lisible et pas pratique » à l'usage, avec
 * raison : l'information d'un même switch s'éparpillait sur toute la
 * surface, et les liens traversaient le schéma en diagonale.
 *
 * On reprend ici la présentation « Standard Switch » d'ESXi, que les
 * exploitants connaissent : UN BLOC PAR SWITCH, lu de gauche à droite.
 *
 *   réseaux (et leurs VMs)  |  switch  |  cartes physiques
 *
 * La correspondance avec Harvester :
 *   - un cluster network est un vSwitch à uplinks (bridge, bond, cartes) ;
 *   - un provider network kube-ovn aussi, avec ses subnets d'underlay ;
 *   - l'overlay OVN est un vSwitch INTERNE : il n'a aucun uplink, et c'est
 *     précisément ce qu'il faut voir. L'ancien graphe le dessinait comme
 *     s'il atteignait le cuivre.
 *
 * Tout est rendu en HTML : le texte se sélectionne, les boutons de copie
 * sont natifs, et il n'y a plus de diagonale.
 */
const Fabric = (() => {
  const REFRESH_MS = 8000;
  // Au-delà, une liste de VMs sous un réseau cesse d'être lisible : on
  // montre les premières et on compte le reste.
  const VMS_SHOWN = 6;
  let cluster = null;
  let host = null;
  let timer = null;
  let lastData = null;
  let selected = null;             // {node, name} de la carte détaillée
  const expanded = new Set();      // réseaux dont on a déplié toutes les VMs
  // Débit, duplex, MTU et compteurs viennent du nœud (/sys, `ip`), pas de
  // l'API : un aller-retour SSH. On le fait UNE fois par nœud et par
  // session, pas à chaque rafraîchissement.
  const detail = new Map();        // node -> {phys, stats, links}
  // Sonde LLDP par carte (« node/iface » -> {busy, text}). Tenue ici et non
  // dans le panneau : il est redessiné toutes les 8 s, et la sonde écoute
  // jusqu'à 35 s (réponse perdue, relevé sur harvlab).
  const lldpState = new Map();
  const detailPending = new Set();

  // Outils communs aux trois vues (board.js).
  const { tr, esc, val, applyTips } = window.Board;

  // -------------------------------------------------------------------------
  // Modèle : regrouper par SWITCH
  // -------------------------------------------------------------------------
  function buildModel(d) {
    const links = d.links || [];
    const nodes = (d.nodes || []).map(n => n.name);
    const networks = d.networks || [];
    const ovn = d.kubeovn || {};
    const vms = d.vms || [];

    const childrenOf = (node, name) =>
      links.filter(l => l.node === node && l.master === name);

    // Chaîne d'uplink sous un switch : bond puis cartes. Les veth sont les
    // ports des charges, jamais un chemin vers l'extérieur.
    function uplinksOf(node, sw) {
      return childrenOf(node, sw)
        .filter(l => l.layer !== 5)
        .map(l => ({ link: l, members: l.type === 'bond'
          ? childrenOf(node, l.name).filter(c => c.layer !== 5) : [] }));
    }
    const portsOf = (node, sw) =>
      childrenOf(node, sw).filter(l => l.layer === 5).length;

    const key = (n) => n.namespace + '/' + n.name;
    const vmsOn = (nadKeys) => vms.filter(vm =>
      (vm.networks || []).some(n => n.network && nadKeys.includes(n.network)));

    // Le bridge qui réalise un cluster network : un NAD le nomme, on le lit
    // dans la donnée plutôt que de le déduire de la convention `<cn>-br`.
    const bridgeOfCn = {};
    networks.forEach(n => {
      if (n.cluster_network && n.bridge) bridgeOfCn[n.cluster_network] = n.bridge;
    });
    const vcOfCn = {};
    (d.vlan_configs || []).forEach(v => { vcOfCn[v.cluster_network] = v; });

    const blocks = [];
    const placed = new Set();

    (d.cluster_networks || []).forEach(cn => {
      const bridge = bridgeOfCn[cn.name];
      const pgs = networks.filter(n => n.cluster_network === cn.name
                                     && n.fabric === 'classic');
      pgs.forEach(n => placed.add(key(n)));
      const vc = vcOfCn[cn.name] || null;
      blocks.push({
        kind: 'classic', id: 'cn-' + cn.name,
        title: bridge || cn.name, clusterNetwork: cn.name,
        facts: vc ? [vc.bond_mode, vc.mtu ? 'MTU ' + vc.mtu : ''] : [],
        portGroups: pgs.map(n => ({
          name: n.name, namespace: n.namespace, ready: n.ready,
          tags: [n.vlan ? 'VLAN ' + n.vlan : tr('fabric.untagged', 'untagged')],
          vms: vmsOn([key(n)]) })),
        perNode: nodes.map(node => ({
          node,
          uplinks: bridge ? uplinksOf(node, bridge) : [],
          ports: bridge ? portsOf(node, bridge) : 0 })),
      });
    });

    const subnetsByVlan = {};
    (ovn.subnets || []).forEach(s => {
      if (s.vlan) (subnetsByVlan[s.vlan] = subnetsByVlan[s.vlan] || []).push(s);
    });
    // Un subnet et ses NADs partagent le même `provider` : c'est par là
    // qu'une VM rejoint un subnet kube-ovn.
    const nadsOfSubnet = (s) => networks.filter(n =>
      n.provider && s.provider && n.provider === s.provider);
    const subnetGroup = (s, tags) => {
      const nads = nadsOfSubnet(s);
      nads.forEach(n => placed.add(key(n)));
      const nadKeys = nads.map(key);
      return { name: s.name, cidr: s.cidr, gateway: s.gateway,
               tags: tags.filter(Boolean), nads: nadKeys, vms: vmsOn(nadKeys) };
    };

    (ovn.provider_networks || []).forEach(pn => {
      const vlans = (ovn.vlans || []).filter(v => v.provider_network === pn.name);
      const pgs = [];
      vlans.forEach(v => (subnetsByVlan[v.name] || []).forEach(s => {
        // VLAN 0 dans kube-ovn veut dire « sans étiquette ».
        const tag = v.id ? 'VLAN ' + v.id : tr('fabric.untagged', 'untagged');
        pgs.push(subnetGroup(s, [tag, s.vpc ? 'VPC ' + s.vpc : '']));
      }));
      blocks.push({
        kind: 'underlay', id: 'pn-' + pn.name, title: pn.name,
        facts: ['kube-ovn provider network'],
        ready: pn.ready, portGroups: pgs,
        perNode: nodes.map(node => {
          const nic = links.find(l => l.node === node
                                   && l.name === pn.default_interface);
          return { node, ports: 0,
                   uplinks: nic ? [{ link: nic, members: [] }] : [],
                   missing: nic ? null : pn.default_interface };
        }),
      });
    });

    // L'overlay est un switch INTERNE : aucun uplink physique. C'est le
    // point que l'ancien graphe trahissait, en le dessinant relié au cuivre.
    const overlay = (ovn.subnets || []).filter(s => s.overlay);
    const pgsOverlay = overlay.map(s => subnetGroup(s,
      [s.nat ? 'NAT' : '', s.vpc ? 'VPC ' + s.vpc : '']));
    const loose = networks.filter(n => n.fabric === 'ovn' && !placed.has(key(n)));
    loose.forEach(n => placed.add(key(n)));
    const pgsLoose = loose.map(n => ({
      name: n.name, namespace: n.namespace,
      tags: [tr('fabric.noSubnet', 'no subnet bound')], warn: true,
      vms: vmsOn([key(n)]) }));
    if (pgsOverlay.length || pgsLoose.length) {
      blocks.push({
        kind: 'overlay', id: 'overlay', title: tr('fabric.overlay', 'OVN overlay'),
        facts: [tr('fabric.overlayNote', 'encapsulated over the node network')],
        portGroups: pgsOverlay.concat(pgsLoose), perNode: [],
      });
    }

    // Cartes rattachées à rien : les montrer évite de croire qu'on a tout vu.
    const attached = new Set();
    blocks.forEach(b => b.perNode.forEach(pn => pn.uplinks.forEach(u => {
      attached.add(pn.node + '/' + u.link.name);
      u.members.forEach(c => attached.add(pn.node + '/' + c.name));
    })));
    const unused = links.filter(l => l.layer === 0
                                  && !attached.has(l.node + '/' + l.name));
    // Le réseau de pod n'a pas de NAD : il sort par le routage du nœud
    // (NAT), pas par un bridge. Le dire vaut mieux que de le cacher.
    const podVms = vms.filter(vm => (vm.networks || []).some(n => n.pod));
    return { blocks, unused, nodes, podVms };
  }

  // -------------------------------------------------------------------------
  // Rendu
  // -------------------------------------------------------------------------
  function physOf(node, name) {
    return ((detail.get(node) || {}).phys || {})[name] || null;
  }

  function speedOf(node, name) {
    const p = physOf(node, name);
    if (!p || !p.speed_mbps) return '';
    const dup = p.duplex && p.duplex !== 'unknown'
      ? ' ' + p.duplex.charAt(0).toUpperCase() + p.duplex.slice(1) : '';
    return p.speed_mbps + dup;
  }

  function nicHtml(node, l, inBond) {
    const up = l.state === 'up';
    const speed = speedOf(node, l.name);
    const isSel = selected && selected.node === node && selected.name === l.name;
    return `<div class="vsw-nic ${up ? 'up' : 'down'}${inBond ? ' in-bond' : ' vsw-link'}${isSel ? ' selected' : ''} tip"
                 data-node="${esc(node)}" data-nic="${esc(l.name)}"
                 data-tip-i18n="fabric.nicTip" tabindex="0" role="button">
      <span class="vsw-dot" aria-hidden="true"></span>
      ${val(l.name, 'vsw-name')}
      <span class="vsw-speed">${esc(speed || (up ? '' : tr('fabric.noLink', 'no link')))}</span>
    </div>`;
  }

  function uplinkHtml(node, u) {
    const l = u.link;
    if (l.type !== 'bond') return nicHtml(node, l, false);
    return `<div class="vsw-bond vsw-link">
      <div class="vsw-bond-head tip" data-node="${esc(node)}" data-nic="${esc(l.name)}"
           data-tip-i18n="fabric.nicTip" tabindex="0" role="button">
        ${val(l.name, 'vsw-name')}
        <small>${esc(tr('fabric.bond', 'bond'))}</small>
      </div>
      ${u.members.map(c => nicHtml(node, c, true)).join('')
        || `<div class="vsw-empty warn">${esc(tr('fabric.noMember', 'no member adapter reported'))}</div>`}
    </div>`;
  }

  function vmListHtml(list, id) {
    if (!list.length) return '';
    // Les VMs démarrées d'abord : ce sont elles qui ont du trafic, et
    // celles qu'on cherche quand un réseau pose problème.
    const sorted = [...list].sort((a, b) =>
      ((b.status === 'Running') - (a.status === 'Running'))
      || (a.namespace + '/' + a.name).localeCompare(b.namespace + '/' + b.name));
    const all = expanded.has(id);
    const shown = sorted.slice(0, all ? sorted.length : VMS_SHOWN).map(vm => {
      const run = vm.status === 'Running';
      return `<li class="${run ? 'running' : 'stopped'}">
        <span class="vsw-dot" aria-hidden="true"></span>
        ${val(vm.namespace + '/' + vm.name)}
        <small>${esc(vm.status || '')}</small></li>`;
    }).join('');
    // Deux gabarits littéraux plutôt qu'une clé calculée : le contrôle de
    // parité des traductions ne voit que les clés écrites en toutes lettres.
    const toggle = all
      ? `<button type="button" class="vsw-more tip" data-vsw-more="${esc(id)}"
                 data-tip-i18n="fabric.vmsLessTip">${esc(tr('fabric.vmsLess', 'show less'))}</button>`
      : `<button type="button" class="vsw-more tip" data-vsw-more="${esc(id)}"
                 data-tip-i18n="fabric.vmsMoreTip">+ ${list.length - VMS_SHOWN}</button>`;
    const more = list.length > VMS_SHOWN ? `<li class="more">${toggle}</li>` : '';
    return `<div class="vsw-vms-title">${esc(tr('fabric.vms', 'Virtual machines'))} (${list.length})</div>
      <ul class="vsw-vms">${shown}${more}</ul>`;
  }

  function portGroupHtml(pg) {
    const id = pg.namespace ? pg.namespace + '/' + pg.name : pg.name;
    return `<div class="vsw-pg vsw-link${pg.warn || pg.ready === false ? ' warn' : ''}">
      <div class="vsw-pg-head">
        ${val(id, 'vsw-name')}
        ${pg.ready === false ? `<span class="vsw-badge warn">${esc(tr('fabric.notReady', 'not ready'))}</span>` : ''}
      </div>
      <div class="vsw-tags">${pg.tags.map(t => `<span>${esc(t)}</span>`).join('')}</div>
      ${pg.cidr ? `<div class="vsw-kv"><span>CIDR</span>${val(pg.cidr)}</div>` : ''}
      ${pg.gateway ? `<div class="vsw-kv"><span>${esc(tr('fabric.gateway', 'Gateway'))}</span>${val(pg.gateway)}</div>` : ''}
      ${(pg.nads || []).length ? `<div class="vsw-kv"><span>${esc(tr('fabric.via', 'Attached through'))}</span>
          <span class="vsw-list">${pg.nads.map(n => val(n)).join('')}</span></div>` : ''}
      ${vmListHtml(pg.vms || [], 'pg:' + id)}
    </div>`;
  }

  function blockHtml(b) {
    const left = b.portGroups.map(portGroupHtml).join('')
      || `<div class="vsw-empty">${esc(tr('fabric.noNetwork', 'no network on this switch'))}</div>`;
    const ports = b.perNode.reduce((a, pn) => a + (pn.ports || 0), 0);
    let right;
    if (b.kind === 'overlay') {
      right = `<div class="vsw-internal">${esc(tr('fabric.noUplink',
        'No physical adapter: this switch is internal to the cluster.'))}</div>`;
    } else {
      const many = b.perNode.length > 1;
      right = b.perNode.map(pn => `
        <div class="vsw-node-uplinks">
          ${many ? `<div class="vsw-nodename">${val(pn.node)}</div>` : ''}
          ${pn.uplinks.map(u => uplinkHtml(pn.node, u)).join('')
            || (pn.missing
              ? `<div class="vsw-empty warn">${esc(pn.missing)} : ${esc(tr('fabric.notReported', 'not reported by the node'))}</div>`
              : `<div class="vsw-empty warn">${esc(tr('fabric.noUplinkReported', 'no uplink reported'))}</div>`)}
        </div>`).join('');
    }
    const kindLabel = b.kind === 'overlay'
      ? tr('fabric.internalSwitch', 'Internal switch')
      : tr('fabric.virtualSwitch', 'Virtual switch');
    return `
      <section class="vsw vsw-${b.kind}" data-block="${esc(b.id)}">
        <header class="vsw-head">
          <span class="vsw-kind">${esc(kindLabel)}</span>
          ${val(b.title, 'vsw-title')}
          ${b.clusterNetwork ? `<span class="vsw-sub">${esc(tr('fabric.clusterNetwork', 'cluster network'))} ${val(b.clusterNetwork)}</span>` : ''}
          ${(b.facts || []).filter(Boolean).map(f => `<span class="vsw-sub">${esc(f)}</span>`).join('')}
          ${b.ready === false ? `<span class="vsw-badge warn">${esc(tr('fabric.notReady', 'not ready'))}</span>` : ''}
          ${ports ? `<span class="vsw-sub vsw-ports">${ports === 1
            ? esc(tr('fabric.portOne', '1 workload port'))
            : ports + ' ' + esc(tr('fabric.ports', 'workload ports'))}</span>` : ''}
        </header>
        <div class="vsw-body">
          <div class="vsw-col vsw-left">
            <div class="vsw-col-title">${esc(tr('fabric.colNetworks', 'Networks'))}</div>
            ${left}
          </div>
          <div class="vsw-spine" aria-hidden="true"></div>
          <div class="vsw-col vsw-right">
            <div class="vsw-col-title">${esc(tr('fabric.colAdapters', 'Physical adapters'))}</div>
            ${right}
          </div>
        </div>
      </section>`;
  }

  function noticeHtml(d) {
    if (d.full_linkmonitor) {
      return `<div class="fabric-notice ok"><span>${esc(tr('fabric.noticePresent',
        'Full link monitor in place: Open vSwitch bridges and workload ports are visible.'))}</span>
        <button type="button" class="btn btn-small tip" data-tip-i18n="fabric.noticeRemoveTip"
                data-fabric-monitor="remove">${esc(tr('fabric.noticeRemove', 'Remove it'))}</button></div>`;
    }
    return `<div class="fabric-notice warn"><span>${esc(tr('fabric.noticeMissing',
      'Open vSwitch bridges and workload ports are missing: Harvester does not publish them. A link monitor would, and it only reads.'))}</span>
      <button type="button" class="btn btn-small tip" data-tip-i18n="fabric.noticeInstallTip"
              data-fabric-monitor="add">${esc(tr('fabric.noticeInstall', 'Install the link monitor'))}</button></div>`;
  }

  function render(d) {
    if (!host) return;
    const m = buildModel(d);
    const body = host.querySelector('.fabric-body');
    if (!body) return;
    const unused = m.unused.length ? `
      <section class="vsw vsw-unused">
        <header class="vsw-head"><span class="vsw-kind">${esc(tr('fabric.unusedAdapters', 'Adapters on no switch'))}</span></header>
        <div class="vsw-unused-list">${m.unused.map(l => nicHtml(l.node, l, false)).join('')}</div>
      </section>` : '';
    const pod = m.podVms.length ? `
      <section class="vsw vsw-pod">
        <header class="vsw-head"><span class="vsw-kind">${esc(tr('fabric.podNetwork', 'Pod network'))}</span>
          <span class="vsw-sub">${esc(tr('fabric.podNote', 'no bridge: leaves through the node routing (NAT)'))}</span></header>
        <div class="vsw-pod-body">${vmListHtml(m.podVms, 'pod')}</div>
      </section>` : '';
    // Préserver le défilement : un rafraîchissement ne doit pas renvoyer
    // l'exploitant en haut de la page.
    const scroll = body.scrollTop;
    body.innerHTML = noticeHtml(d)
      + (m.nodes.length === 1
          ? `<div class="fabric-host-title">${esc(tr('fabric.host', 'Host'))} ${val(m.nodes[0])}</div>` : '')
      + m.blocks.map(blockHtml).join('') + pod + unused;
    body.scrollTop = scroll;
    applyTips(body);
    const meta = host.querySelector('.fabric-meta');
    if (meta) {
      const nics = (d.links || []).filter(l => l.layer === 0).length;
      meta.textContent = `${m.blocks.length} ${tr('fabric.switches', 'switches')} · `
        + `${nics} ${tr('fabric.adapters', 'physical adapters')}`;
    }
    fetchDetailOnce(m.nodes);
  }

  // Détail des cartes : un SSH par nœud et par session, puis on complète.
  async function fetchDetailOnce(nodes) {
    for (const node of nodes) {
      if (detail.has(node) || detailPending.has(node)) continue;
      detailPending.add(node);
      const asked = cluster;
      try {
        const r = await fetch(`/api/network-fabric/${encodeURIComponent(asked)}`
                              + `/node/${encodeURIComponent(node)}`);
        const j = r.ok ? await r.json() : null;
        if (asked !== cluster) return;
        // Un échec est retenu aussi : sans SSH, on ne réessaie pas à chaque
        // rafraîchissement.
        const byName = {};
        ((j && j.links) || []).forEach(l => { byName[l.name] = l; });
        detail.set(node, { phys: (j && j.phys) || {}, stats: (j && j.stats) || {},
                           links: byName, failed: !j });
        if (lastData) render(lastData);
        if (selected && selected.node === node) showDetail(node, selected.name);
      } catch {
        detail.set(node, { phys: {}, stats: {}, links: {}, failed: true });
      } finally {
        detailPending.delete(node);
      }
    }
  }

  // -------------------------------------------------------------------------
  // Détail d'une carte
  // -------------------------------------------------------------------------
  function showDetail(node, name) {
    const side = host && host.querySelector('.fabric-detail');
    if (!side || !lastData) return;
    selected = { node, name };
    host.querySelectorAll('.vsw-nic, .vsw-bond-head').forEach(el => el.classList.toggle('selected',
      el.dataset.node === node && el.dataset.nic === name));
    const l = (lastData.links || []).find(x => x.node === node && x.name === name) || {};
    const det = detail.get(node) || {};
    const p = (det.phys || {})[name] || {};
    const s = (det.stats || {})[name] || {};
    const k = (det.links || {})[name] || {};
    const pending = !detail.has(node);
    const rows = [
      [tr('fabric.d.state', 'State'), l.state],
      ['MAC', l.mac],
      [tr('fabric.d.master', 'Master'), l.master || '-'],
      [tr('fabric.d.node', 'Node'), node],
      [tr('fabric.speed', 'Speed'), p.speed_mbps ? p.speed_mbps + ' Mb/s' : '-'],
      ['Duplex', p.duplex || '-'],
      ['MTU', k.mtu == null ? '-' : String(k.mtu)],
      // Le mode d'agrégation décide de la redondance : c'est ce qu'on vient
      // lire sur un bond, avant même son débit.
      ...(k.bond_mode ? [[tr('fabric.bondMode', 'Bond mode'), k.bond_mode],
                         ['miimon', k.bond_miimon == null ? '-' : k.bond_miimon + ' ms']] : []),
      [tr('fabric.carrierChanges', 'Carrier changes'),
       p.carrier_changes == null ? '-' : String(p.carrier_changes)],
      [tr('fabric.rx', 'Received'), s.rx_bytes == null ? '-'
        : `${window.Board.bytes(s.rx_bytes)} · ${s.rx_errors || 0} err · ${s.rx_dropped || 0} drop`],
      [tr('fabric.tx', 'Sent'), s.tx_bytes == null ? '-'
        : `${window.Board.bytes(s.tx_bytes)} · ${s.tx_errors || 0} err · ${s.tx_dropped || 0} drop`],
    ];
    side.innerHTML = `<h3>${esc(name)}</h3>`
      + (pending ? `<p class="hint">${esc(tr('fabric.detailLoading', 'Reading the node...'))}</p>` : '')
      + (det.failed ? `<p class="hint warn">${esc(tr('fabric.detailFailed',
          'The node did not answer over SSH: speed, MTU and counters are unknown.'))}</p>` : '')
      + `<dl class="kv">`
      + rows.map(([key, v]) => {
          const t = v == null ? '-' : String(v);
          return `<dt>${esc(key)}</dt><dd>${esc(t)}${window.CopyTo ? CopyTo.button(t) : ''}</dd>`;
        }).join('')
      + `</dl>`
      + (l.layer === 0 ? lldpBlock(node, name) : '');
    applyTips(side);
  }

  function lldpBlock(node, name) {
    const st = lldpState.get(`${node}/${name}`) || {};
    return `<button type="button" class="btn btn-small tip" data-fabric-lldp
                    data-tip-i18n="fabric.lldpTip" ${st.busy ? 'disabled' : ''}
                    data-node="${esc(node)}" data-iface="${esc(name)}">`
      + `${esc(tr('vm.edit.netPathLldp', 'Identify the switch (LLDP)'))}</button>`
      + `<div class="fabric-lldp-out">${esc(st.text || '')}</div>`;
  }

  /** Redessine le détail s'il montre encore cette carte. */
  function repaintIfShown(node, name) {
    if (selected && selected.node === node && selected.name === name) showDetail(node, name);
  }

  async function probeLldp(btn) {
    const { node, iface } = btn.dataset;
    const key = `${node}/${iface}`;
    lldpState.set(key, { busy: true, text: tr('vm.edit.netPathListening', 'Listening for LLDP...') });
    repaintIfShown(node, iface);
    let text;
    try {
      const r = await fetch(`/api/network-fabric/${encodeURIComponent(cluster)}`
        + `/node/${encodeURIComponent(node)}/lldp`
        + `?iface=${encodeURIComponent(iface)}`).then(x => x.json());
      text = r.found
        ? Board.lldpText(r.fields)
        : (r.hint || r.error || tr('vm.edit.netPathNoLldp', 'No LLDP frame.'));
    } catch (e) { text = String(e.message || e); }
    lldpState.set(key, { busy: false, text });
    repaintIfShown(node, iface);
  }

  async function toggleMonitor(btn) {
    btn.disabled = true;
    const remove = btn.dataset.fabricMonitor === 'remove';
    try {
      const r = await fetch(`/api/network-fabric/${encodeURIComponent(cluster)}/linkmonitor`,
        { method: remove ? 'DELETE' : 'POST', headers: { 'Content-Type': 'application/json' } });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.error || ('HTTP ' + r.status));
      }
      // Le contrôleur met quelques secondes à publier les liens.
      setTimeout(refresh, 2500);
    } catch (e) {
      btn.disabled = false;
      const n = btn.closest('.fabric-notice');
      if (n) n.insertAdjacentHTML('beforeend',
        `<small class="fabric-notice-err">${esc(e.message || e)}</small>`);
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
      // Une réponse d'un autre cluster, arrivée après une bascule, ne doit
      // pas repeindre la vue du nouveau.
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
      if (selected) showDetail(selected.node, selected.name);
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
      <div class="fabric-layout">
        <div class="fabric-body"></div>
        <aside class="fabric-detail"><p class="hint">${esc(tr('fabric.detailHint',
          'Click an adapter to read its speed, MTU and counters.'))}</p></aside>
      </div>`;
    applyTips(host);
    if (window.CopyTo) CopyTo.wire(host);
    host.addEventListener('click', (e) => {
      if (e.target.closest('[data-copy]')) return;
      if (e.target.closest('.fabric-refresh')) { refresh(); return; }
      const mon = e.target.closest('[data-fabric-monitor]');
      if (mon) { toggleMonitor(mon); return; }
      const lldp = e.target.closest('[data-fabric-lldp]');
      if (lldp) { probeLldp(lldp); return; }
      const more = e.target.closest('[data-vsw-more]');
      if (more) {
        const id = more.dataset.vswMore;
        if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
        if (lastData) render(lastData);
        return;
      }
      const nic = e.target.closest('[data-nic]');
      if (nic) showDetail(nic.dataset.node, nic.dataset.nic);
    });
    host.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const nic = e.target.closest('[data-nic]');
      if (nic) { e.preventDefault(); showDetail(nic.dataset.node, nic.dataset.nic); }
    });
  }

  function start(clusterName) {
    const h = document.querySelector('[data-board="fabric"] .topology-host');
    if (!h) return Promise.resolve();
    if (cluster !== clusterName) {
      detail.clear(); expanded.clear(); lastData = null; selected = null;
    }
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

if (typeof window !== 'undefined') window.Fabric = Fabric;
