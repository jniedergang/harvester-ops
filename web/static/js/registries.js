/**
 * harvester-ops : registres des clusters enfants (v2.6.0, tâche 12).
 *
 * Onglet Registres de Cluster API (point 194) : les registres déclarés,
 * rattachés ou non au cluster Harvester choisi, leur détail à droite
 * (adresse, usage, miroirs, paquets publiés, qui les emploie, état), et
 * les gestes : registre d'amorçage (fenêtre à trois onglets Paramètres,
 * Vérifications, Résumé ; Lancer grisé avec la raison tant qu'un constat
 * bloque), déclaration d'un registre du client, rattachement (identifiants
 * jamais réaffichés), vérification, suppression (nom retapé pour supprimer
 * les données). Le serveur lance `harvester-registry` (parité CLI) ; les
 * rôles sont contrôlés côté serveur (point 195).
 * 2.6.1 : Vérifier sonde aussi depuis un réseau de VMs choisi (pod brève),
 * et Paramètres > Registres réserve le port d'écriture à la console.
 * Voir docs/design/2026-11-05-artefacts-registre-260-plan.md.
 */
const Registries = (() => {
  const enc = encodeURIComponent;
  const B = () => window.Board;
  const tr = (k, f, v) => B().tr(k, f, v);
  const esc = (v) => B().esc(v);
  const icon = (n, size) => (window.Icons ? Icons.svg(n, { size: size || 14 }) : '');
  const cluster = () => (window.App && App.managementCluster ? App.managementCluster() : '');
  const ROLE_RANK = { viewer: 1, operator: 2, admin: 3 };
  const can = (need) => (ROLE_RANK[window.App && App.role ? App.role() : null] || 0) >= ROLE_RANK[need];
  const bytes = (n) => (B().bytes ? B().bytes(n) : `${n} B`);
  let list = null;
  let selected = null;
  let modal = null;

  // Un appel littéral par constat (contrôle de parité des traductions)
  const FINDINGS = {
    'registry-unknown': (v) => tr('reg.fd.registry-unknown', 'The registry {registry} is not declared.', v),
    'registry-not-attached': (v) => tr('reg.fd.registry-not-attached', 'The registry {registry} has no accounts on this cluster: attach it.', v),
    'registry-unreachable': (v) => tr('reg.fd.registry-unreachable', 'The registry {registry} does not answer at {endpoint}.', v),
    'registry-auth-refused': (v) => tr('reg.fd.registry-auth-refused', 'The registry {registry} refuses the account(s) {accounts}.', v),
    'registry-cert-expiring': (v) => tr('reg.fd.registry-cert-expiring', 'The server certificate of {registry} expires in {days} day(s), on {not_after}: renew it before the nodes refuse it.', v),
    'registry-ca-invalid': (v) => tr('reg.fd.registry-ca-invalid', 'The certificate of {registry} is not signed by its declared CA.', v),
    'registry-pull-is-push': (v) => tr('reg.fd.registry-pull-is-push', 'The read account of {registry} is its write account: it will be written on every child node.', v),
    'registry-no-usage': (v) => tr('reg.fd.registry-no-usage', 'The registry {registry} serves neither images nor charts.', v),
    'registry-insecure': (v) => tr('reg.fd.registry-insecure', 'The certificate of {registry} is not checked.', v),
    'registry-address-invalid': (v) => tr('reg.fd.registry-address-invalid', 'The address {address} is not a valid IPv4 address.', v),
    'registry-address-in-pool': (v) => tr('reg.fd.registry-address-in-pool', 'The address {address} belongs to the pool {pool}.', v),
    'registry-address-answers': (v) => tr('reg.fd.registry-address-answers', 'Something already answers at {address}.', v),
    'registry-address-vip': (v) => tr('reg.fd.registry-address-vip', 'The address {address} is the VIP of the cluster.', v),
    'registry-room-short': (v) => tr('reg.fd.registry-room-short', 'Longhorn has no room for a volume of this size on {storage_class}.', v),
    'registry-replicas-nodes': (v) => tr('reg.fd.registry-replicas-nodes', 'The class {storage_class} asks for {replicas} replicas, the cluster has {nodes} node(s).', v),
    'registry-images-missing': (v) => tr('reg.fd.registry-images-missing', 'The registry images are missing from this installation.', v),
    'registry-tools-missing': (v) => tr('reg.fd.registry-tools-missing', 'Tools missing in the console: {tools}.', v),
    'registry-name-taken': (v) => tr('reg.fd.registry-name-taken', 'The name {registry} is already taken.', v),
    'registry-storage-class-unknown': (v) => tr('reg.fd.registry-storage-class-unknown', 'The storage class {storage_class} does not exist.', v),
    'cluster-unreachable': (v) => tr('reg.fd.cluster-unreachable', 'The Harvester cluster cannot be read.', v),
    'publish-tag-differs': (v) => tr('reg.fd.publish-tag-differs', '{reference} already exists with another content: nothing is overwritten.', v),
    'package-untrusted-strict': (v) => tr('reg.fd.package-untrusted-strict', 'Strict mode refuses {package} ({state}).', v),
    'publish-item-unknown': (v) => tr('reg.fd.publish-item-unknown', 'Unknown item(s): {items}.', v),
    'registry-vm-network-unchecked': (v) => tr('reg.fd.registry-vm-network-unchecked', 'Reachability of {endpoint} from the VM network is not checked: {reason}.', vmFacts(v)),
    'registry-vm-network-ok': (v) => tr('reg.fd.registry-vm-network-ok', '{endpoint} answers from the VM network {network} (probe address {address}, TLS handshake done).', vmFacts(v)),
    'registry-unreachable-from-vm-network': (v) => tr('reg.fd.registry-unreachable-from-vm-network', '{endpoint} cannot be reached from the VM network {network}: {failed}.', vmFacts(v)),
    'registry-write-port-open': (v) => tr('reg.fd.registry-write-port-open', 'The write port {endpoint} accepts every address: reserve it to the console in Settings > Registries.', v),
    // 2.8.0 : CA gardée, CA en transition, réglage des nœuds Harvester
    'registry-ca-key-missing': (v) => tr('reg.fd.registry-ca-key-missing', 'The CA of {registry} was not kept (posed before 2.8.0): renewing its certificate needs a new CA, in two steps.', v),
    'registry-ca-transition-pending': (v) => tr('reg.fd.registry-ca-transition-pending', 'A new CA of {registry} is in transition: child clusters that do not trust it yet: {clusters}.', v),
    'harvester-nodes-other-auth': (v) => tr('reg.fd.harvester-nodes-other-auth', '{hosts} keep a password in Harvester, which would erase it when the mirrors change: nothing written. Remove these credentials in Harvester first, then enter them again afterwards.', v),
    'harvester-nodes-mirror-conflict': (v) => tr('reg.fd.harvester-nodes-mirror-conflict', 'The Harvester nodes already mirror {origin} through {endpoints}: remove that entry first.', v),
    'harvester-rke2-restart': (v) => tr('reg.fd.harvester-rke2-restart', 'Harvester restarts RKE2 on every node to apply the setting {setting}.', v),
    // 2.8.1 (décision 242) : registre hébergé par ce cluster même, non bloquant
    'harvester-nodes-self-hosted': (v) => tr('reg.fd.harvester-nodes-self-hosted', '{registry} is hosted by {cluster} itself ({address}, {by}): its nodes may not reach this address, and pulls on the Harvester nodes may fall back to the origin or fail. Prefer a registry outside this cluster.', v),
    // revue 5-8 (I-7) et tâche 9 (constat 2) : refus rendus en JSON par l'outil
    'harvester-nodes-auth-inline': (v) => tr('reg.fd.harvester-nodes-auth-inline', 'After {seconds} s the read password of {registry} is still in the setting: Harvester did not move it into its auth-config Secret.', v),
    'harvester-nodes-value-unreadable': (v) => tr('reg.fd.harvester-nodes-value-unreadable', 'The current value of containerd-registry cannot be read: nothing is written.', v),
    'harvester-nodes-write-failed': (v) => tr('reg.fd.harvester-nodes-write-failed', 'The setting could not be written on the Harvester cluster.', v),
  };
  // revue 5-8 (I-6) : copie de la CA qui ne porte pas encore la nouvelle
  const PENDING = {
    renewing: () => tr('reg.pend.renewing', 'machine renewal not finished (Renew > Finish)'),
    nodes: () => tr('reg.pend.nodes', 'the nodes do not trust it yet (Renew > CA)'),
    'rke2-fetch': () => tr('reg.pend.rke2-fetch', 'RKE2 download without Internet (Renew > CA)'),
    'service-ca': () => tr('reg.pend.service-ca', 'CA of the chart services (Renew > CA)'),
    flux: (v) => tr('reg.pend.flux', 'Flux entry {entry} (deploy it again from the GitOps tab)', v),
    'harvester-nodes': () => tr('reg.pend.harvester-nodes', 'additional-ca of the Harvester nodes (Harvester nodes window)'),
  };
  function pendingText(p) {
    const r = String(p.reason || '');
    const [k, entry] = r.startsWith('flux:') ? ['flux', r.slice(5)] : [r, ''];
    return PENDING[k] ? PENDING[k]({ entry }) : r;
  }
  // 2.6.1 : raison d'une sonde impossible, étape qui a échoué (un appel
  // littéral par texte, contrôle de parité des traductions)
  const REASONS = {
    'no-network': (v) => tr('vmp.reason.no-network', 'no VM network chosen', v),
    'at-use': (v) => tr('vmp.reason.at-use', 'it is probed from the VM network when a child cluster or a plan that uses it is checked', v),
    'network-missing': (v) => tr('vmp.reason.network-missing', 'the network {network} was not found', v),
    'forbidden': (v) => tr('vmp.reason.forbidden', 'your rights do not allow the probe pod (namespace harvester-ops-probe)', v),
    'image-missing': (v) => tr('vmp.reason.image-missing', 'the probe image is not on the nodes (it comes with a bootstrap registry)', v),
    'no-address': (v) => tr('vmp.reason.no-address', 'the probe got no address on {network} (no DHCP lease, no IPAM)', v),
    'not-started': (v) => tr('vmp.reason.not-started', 'the probe pod did not start ({detail})', v),
    'timeout': (v) => tr('vmp.reason.timeout', 'the probe gave no result in time', v),
    'ipam-incomplete': (v) => tr('vmp.reason.ipam-incomplete', 'the address given by the IPAM of {network} comes without a gateway or DNS: the probe cannot judge', v),
  };
  const FAILED = {
    dns: () => tr('vmp.failed.dns', 'the name does not resolve with the DNS of the network'),
    route: () => tr('vmp.failed.route', 'no route (the network gives no gateway)'),
    tcp: () => tr('vmp.failed.tcp', 'no TCP connection'),
    tls: () => tr('vmp.failed.tls', 'TCP connects but the TLS handshake fails'),
  };
  function vmFacts(v) {
    const out = Object.assign({}, v || {});
    if (out.reason) out.reason = REASONS[out.reason] ? REASONS[out.reason](out) : out.reason;
    if (out.failed) out.failed = FAILED[out.failed] ? FAILED[out.failed]() : out.failed;
    return out;
  }
  const VM_CODES = ['registry-vm-network-unchecked', 'registry-vm-network-ok', 'registry-unreachable-from-vm-network'];
  const LEVEL_CLASS = { block: 'sev-critical', warn: 'sev-watch', ok: 'sev-info' };
  const findingText = (f) => {
    const fn = FINDINGS[f.code];
    const facts = Object.assign({}, f.facts || {});
    Object.keys(facts).forEach(k => { if (Array.isArray(facts[k])) facts[k] = facts[k].join(', '); });
    return fn ? fn(facts) : f.code;
  };
  const findingsHtml = (fs) => (fs || []).map(f => `<div class="sto-finding ${LEVEL_CLASS[f.level] || ''}" data-code="${esc(f.code)}"
      data-level="${esc(f.level)}"><div class="sto-finding-title">${esc(findingText(f))}</div></div>`).join('');

  async function json(method, url, body) {
    const r = await fetch(url, { method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined });
    const d = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, d };
  }

  function closeModal() {
    // revue finale m-1 : une vérification différée ne part plus après la fermeture
    if (modal && modal._cleanup) modal._cleanup();
    if (modal) modal.remove();
    modal = null;
  }

  function overlay(title, sub, bodyHtml, footHtml) {
    closeModal();
    modal = document.createElement('div');
    modal.className = 'modal-overlay active reg-modal';
    modal.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-header"><div><h3>${icon('containers')} ${esc(title)}</h3>
          <div class="modal-subtitle">${esc(sub)}</div></div>
          <button type="button" class="btn-close tip" data-reg-close data-tip="${esc(tr('common.cancel', 'Cancel'))}"
                  title="${esc(tr('common.cancel', 'Cancel'))}">×</button></div>
        <div class="modal-body">${bodyHtml}</div>
        <div class="modal-footer apply-bar" style="margin:0;">
          <button type="button" class="btn btn-secondary btn-sm" data-reg-close>${esc(tr('common.cancel', 'Cancel'))}</button>
          ${footHtml}<span class="apply-result" data-x="feedback"></span></div>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal || e.target.closest('[data-reg-close]')) closeModal();
    });
    return modal;
  }

  const field = (label, input, tip) => `<label class="tf-field"><span class="tf-label">${esc(label)}</span>${input
    .replace(/<(input|select|textarea)/, `<$1 class="tip" data-tip="${esc(tip)}" title="${esc(tip)}"`)}</label>`;
  const btn = (attrs, cls, ic, label, tip, disabled) => `<button type="button" class="btn btn-sm ${cls} tip" ${attrs}
      data-tip="${esc(tip)}" title="${esc(tip)}"${disabled ? ' disabled' : ''}>${icon(ic, 13)} ${esc(label)}</button>`;

  // -------------------------------------------------------------------------
  // Liste et détail
  // -------------------------------------------------------------------------
  async function refresh() {
    const box = document.getElementById('registries-body');
    if (!box) return;
    const c = cluster();
    const [r, lr] = await Promise.all([json('GET', '/api/registries').catch(() => null),
      c ? json('GET', `/api/registries/bootstrap/${enc(c)}/leftovers`).catch(() => null) : null]);
    list = r && r.ok ? (r.d.registries || []) : null;
    if (!list) { box.innerHTML = `<p class="form-hint">${esc(tr('reg.loadError', 'The registries could not be read.'))}</p>`; return; }
    // revue finale I-2 : restes d'un déploiement échoué (objets sans
    // déclaration), montrés avec Supprimer
    const left = lr && lr.ok ? (lr.d.leftovers || []) : [];
    list = list.concat(left.filter(x => !list.some(y => y.name === x.registry)).map(x => ({ name: x.registry, kind: 'leftover',
      cluster: x.cluster || c, endpoint: x.address || '', objects: x.objects || [], origin: x.origin || {}, usage: [], clusters: [] })));
    const rows = list.map(x => {
      const attached = (x.clusters || []).includes(c);
      if (x.kind === 'leftover') {
        return `<tr class="reg-row reg-leftover${selected === x.name ? ' selected' : ''}" data-reg="${esc(x.name)}" tabindex="0">
        <td><strong>${esc(x.name)}</strong></td>
        <td><span class="badge warn tip" data-tip="${esc(tr('reg.t.leftover', 'Objects left by a deployment that failed, without a declaration: delete them before a new try'))}">${esc(tr('reg.kind.leftover', 'Leftover'))}</span></td>
        <td><code>${esc(x.endpoint)}</code></td><td>-</td><td>-</td><td>-</td></tr>`;
      }
      return `<tr class="reg-row${selected === x.name ? ' selected' : ''}" data-reg="${esc(x.name)}" tabindex="0">
        <td><strong>${esc(x.name)}</strong></td>
        <td>${x.kind === 'bootstrap' ? `<span class="badge info">${esc(tr('reg.kind.bootstrap', 'Bootstrap'))}</span>`
          : `<span class="badge dim">${esc(tr('reg.kind.external', 'Client'))}</span>`}</td>
        <td><code>${esc(x.endpoint || '')}</code></td>
        <td>${esc((x.usage || []).join(', '))}</td>
        <td>${attached ? `<span class="badge ok">${esc(tr('reg.attached', 'attached'))}</span>`
          : `<span class="badge warn">${esc(tr('reg.notAttached', 'not attached'))}</span>`}</td>
        <td>${esc(x.published || 0)}</td></tr>`;
    }).join('');
    box.innerHTML = `<div class="art-layout">
        <div class="art-list">${rows ? `<table class="data-table reg-table"><thead><tr>
            <th>${esc(tr('reg.col.name', 'Registry'))}</th><th>${esc(tr('reg.col.kind', 'Kind'))}</th>
            <th>${esc(tr('reg.col.endpoint', 'Address'))}</th><th>${esc(tr('reg.col.usage', 'Serves'))}</th>
            <th>${esc(tr('reg.col.attached', 'On {cluster}', { cluster: c || '?' }))}</th>
            <th>${esc(tr('reg.col.published', 'Packages'))}</th></tr></thead><tbody>${rows}</tbody></table>`
          : `<p class="form-hint">${esc(tr('reg.empty', 'No registry yet: pose a bootstrap registry on the Harvester cluster, or declare the registry of your organisation.'))}</p>`}</div>
        <aside class="art-detail card" id="registries-detail" aria-live="polite">${selected ? ''
          : `<p class="form-hint">${esc(tr('reg.pick', 'Select a registry to see its address, mirrors, packages and state.'))}</p>`}</aside>
      </div>`;
    if (selected) showDetail(selected);
  }

  function showLeftover(box, x) {
    const plan = x.origin && x.origin.plan;
    box.innerHTML = `
      <div class="card-header"><h3>${esc(x.name)}</h3><div class="reg-actions">
        ${btn('data-reg-delete', 'btn-danger', 'trash', tr('reg.delete', 'Delete'), can('admin')
          ? tr('reg.t.deleteLeftover', 'Remove these objects from the cluster; the volume is kept unless you type the name')
          : tr('reg.t.adminDelete', 'Deleting needs the admin role'), !can('admin'))}
      </div></div>
      <div class="card-body">
        <p class="form-hint">${esc(plan ? tr('reg.leftoverPlan', 'Left by a failed deployment of the plan {plan}. A resume of this plan takes it over; Delete removes it.', { plan })
          : tr('reg.leftoverIntro', 'Left by a deployment that failed before the registry was ready: no declaration was saved. Delete removes these objects.'))}</p>
        <dl class="reg-facts"><dt>${esc(tr('reg.col.endpoint', 'Address'))}</dt><dd><code>${esc(x.endpoint || '-')}</code></dd>
          <dt>${esc(tr('reg.clusters', 'Attached to'))}</dt><dd><code>${esc(x.cluster)}</code></dd></dl>
        <h4>${esc(tr('reg.leftoverObjects', 'Objects left'))}</h4>
        <ul class="reg-used">${(x.objects || []).map(o => `<li><code>${esc(o)}</code></li>`).join('')}</ul>
      </div>`;
  }

  async function showDetail(name) {
    selected = name;
    document.querySelectorAll('.reg-row').forEach(r => r.classList.toggle('selected', r.dataset.reg === name));
    const box = document.getElementById('registries-detail');
    if (!box) return;
    const lx = (list || []).find(x => x.name === name && x.kind === 'leftover');
    if (lx) { showLeftover(box, lx); return; }
    const c = cluster();
    const r = await json('GET', `/api/registries/${enc(name)}?cluster=${enc(c)}`).catch(() => null);
    if (!r || !r.ok) { box.innerHTML = `<p class="form-hint">${esc(tr('reg.loadError', 'The registries could not be read.'))}</p>`; return; }
    const d = r.d;
    const boot = d.kind === 'bootstrap';
    const attached = (d.clusters || []).includes(c);
    const attachWhy = boot ? tr('reg.t.attachBoot', 'The accounts of a bootstrap registry are generated by the console: nothing to type.')
      : tr('reg.t.attach', 'Give the write and read accounts of this registry; they are kept in Secrets of the Harvester cluster, never shown again');
    const used = (d.used_by || []).map(u => `<li><code>${esc(u)}</code></li>`).join('');
    const pubs = (d.published || []).map(p => `<li>${esc(p.package)} ${esc(p.version)} <span class="form-hint">${esc(p.at || '')} ${esc(p.by || '')}</span></li>`).join('');
    box.innerHTML = `
      <div class="card-header"><h3>${esc(d.name)}</h3><div class="reg-actions">
        ${btn('data-reg-attach', 'btn-secondary', 'key', tr('reg.attach', 'Attach'), attachWhy, boot || !can('operator'))}
        ${btn('data-reg-check', 'btn-secondary', 'test', tr('reg.check', 'Check'), can('operator')
          ? tr('reg.t.check', 'Read the accounts on this cluster and probe the registry from the console, without writing')
          : tr('reg.t.checkOperator', 'Checking a registry needs the operator role'), !attached || !can('operator'))}
        ${boot ? btn('data-reg-renew', 'btn-secondary', 'refresh', tr('reg.renew', 'Renew the certificate'), !can('admin')
          ? tr('reg.t.renewAdmin', 'Renewing the certificate needs the admin role')
          : d.cluster !== c ? tr('reg.t.renewElsewhere', 'This registry lives on {cluster}: choose that cluster to renew its certificate', { cluster: d.cluster || '?' })
            : tr('reg.t.renew', 'Sign a new server certificate: parameters, checks, summary'), !can('admin') || d.cluster !== c) : ''}
        ${btn('data-reg-nodes', 'btn-secondary', 'node', tr('reg.nodes', 'Harvester nodes'), !can('admin')
          ? tr('reg.t.nodesAdmin', 'Changing the registry setting of the Harvester nodes needs the admin role')
          : !attached ? tr('reg.t.nodesAttach', 'Attach this registry to {cluster} first', { cluster: c || '?' })
            : tr('reg.t.nodes', 'Let the Harvester nodes themselves pull from this registry (setting containerd-registry): parameters, checks, summary'),
          !can('admin') || !attached)}
        ${btn('data-reg-delete', 'btn-danger', 'trash', tr('reg.delete', 'Delete'), boot
          ? tr('reg.t.deleteBoot', 'Remove the bootstrap registry from the cluster; its volume is kept unless you type its name')
          : tr('reg.t.forget', 'Forget this declaration (refused while a plan, a template or a child cluster uses it)'), !can('admin'))}
      </div></div>
      <div class="card-body">
        <dl class="reg-facts">
          <dt>${esc(tr('reg.col.endpoint', 'Address'))}</dt><dd><code>${esc(d.endpoint)}</code>${d.push_endpoint ? ` <span class="form-hint">${esc(tr('reg.pushEndpoint', 'writes on {endpoint}', { endpoint: d.push_endpoint }))}</span>` : ''}</dd>
          <dt>${esc(tr('reg.col.usage', 'Serves'))}</dt><dd>${esc((d.usage || []).join(', '))}</dd>
          <dt>${esc(tr('reg.mirrors', 'Mirrors'))}</dt><dd>${(d.mirrors || []).map(m => `<code>${esc(m)}</code>`).join(' ') || esc(tr('reg.noMirror', 'none'))}</dd>
          <dt>${esc(tr('reg.ca', 'CA'))}</dt><dd>${esc(d.ca_pem ? tr('reg.caOwn', 'its own CA') : tr('reg.caSystem', 'the system CAs'))}</dd>
          <dt>${esc(tr('reg.clusters', 'Attached to'))}</dt><dd>${(d.clusters || []).map(x => `<code>${esc(x)}</code>`).join(' ') || '-'}</dd>
          ${boot ? `<dt>${esc(tr('reg.volume', 'Volume'))}</dt><dd>${esc(d.size_gib)} GiB, ${esc(d.storage_class)}</dd>` : ''}
        </dl>
        <div data-x="state"></div>
        ${attached && can('operator') ? `<div class="reg-vmnet">${field(tr('reg.vmNet', 'VM network'),
          `<select data-x="vm-net"><option value="">${esc(tr('reg.vmNetNone', 'None: from the console and the Harvester cluster only'))}</option></select>`,
          tr('reg.t.vmNet', 'Also probe the registry from this VM network, as a VM would: a short-lived pod gets an address there (DHCP or the IPAM of the network), tests TCP and TLS, and is removed right after'))}</div>` : ''}
        <div data-x="check"></div>
        <h4>${esc(tr('reg.usedBy', 'Used by'))}</h4>
        ${used ? `<ul class="reg-used">${used}</ul>` : `<p class="form-hint">${esc(tr('reg.unused', 'Nothing uses it yet.'))}</p>`}
        ${d.used_by_error ? `<p class="form-hint">${esc(d.used_by_error)}</p>` : ''}
        <h4>${esc(tr('reg.published', 'Published packages'))}</h4>
        ${pubs ? `<ul>${pubs}</ul>` : `<p class="form-hint">${esc(tr('reg.nothingPublished', 'No package published yet (Artefacts tab).'))}</p>`}
      </div>`;
    const sel = box.querySelector('[data-x="vm-net"]');
    if (sel) {
      json('GET', `/api/networks/${enc(c)}`).then(nr => {
        const nets = nr && nr.ok && Array.isArray(nr.d) ? nr.d : [];
        nets.forEach(n => {
          const ref = `${n.namespace}/${n.name}`;
          const o = document.createElement('option');
          o.value = ref;
          o.textContent = n.vlan ? `${ref} (VLAN ${n.vlan})` : ref;
          sel.appendChild(o);
        });
      }).catch(() => {});
    }
    if (boot && d.cluster === c) {
      const st = await json('GET', `/api/registries/bootstrap/${enc(c)}/${enc(name)}`).catch(() => null);
      const el = box.querySelector('[data-x="state"]');
      if (el && st && st.ok) {
        const s = st.d;
        el.innerHTML = `<p class="reg-state">${s.ready ? `<span class="badge ok">${esc(tr('reg.ready', 'ready'))}</span>`
          : `<span class="badge warn">${esc(tr('reg.notReady', 'not ready'))}</span>`}
          ${s.volume && s.volume.used_bytes != null ? esc(tr('reg.used', '{used} used', { used: bytes(s.volume.used_bytes) })) : ''}
          ${s.repositories != null ? esc(tr('reg.repos', '{n} repositories', { n: s.repositories })) : ''}</p>${certLine(s.cert_not_after)}${findingsHtml(s.findings)}`;
        caKeyMissing[name] = (s.findings || []).some(f => f.code === 'registry-ca-key-missing');
      }
    }
  }

  async function check() {
    const box = document.querySelector('#registries-detail [data-x="check"]');
    if (!box || !selected) return;
    box.innerHTML = `<p class="form-hint">${esc(tr('reg.checking', 'Checking…'))}</p>`;
    const net = (document.querySelector('#registries-detail [data-x="vm-net"]') || {}).value || '';
    const r = await json('POST', `/api/registries/${enc(selected)}/check/${enc(cluster())}`, net ? { vm_network: net } : undefined);
    // le constat du réseau des VMs (2.6.1 : sonde ou raison) suit le verdict
    const fs = r.ok ? (r.d.findings || []) : [];
    const isVm = (f) => VM_CODES.includes(f.code);
    box.innerHTML = r.ok ? ((findingsHtml(fs.filter(f => !isVm(f))) || `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('reg.allGood', 'Reachable, CA valid, accounts accepted.'))}</div></div>`)
      + findingsHtml(fs.filter(isVm)))
      : `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(r.d.error || `HTTP ${r.status}`)}</div></div>`;
  }

  // -------------------------------------------------------------------------
  // 2.8.0 : échéance du certificat, Renouveler le certificat, Nœuds Harvester
  // -------------------------------------------------------------------------
  const CERT_WARN_DAYS = 30;              // registry_seed.CERT_WARN_DAYS
  const caKeyMissing = {};                // registre -> CA non gardée (constat du statut)
  const dateOf = (epoch) => new Date(epoch * 1000).toISOString().slice(0, 10);
  function certLine(notAfter) {
    if (notAfter == null) return '';
    const days = Math.floor((notAfter * 1000 - Date.now()) / 86400000);
    const warn = days < CERT_WARN_DAYS;
    const why = tr('reg.t.cert', 'End of validity of the server certificate; renew it before the nodes refuse it');
    return `<p class="reg-cert tip" data-x="cert" data-tip="${esc(why)}" title="${esc(why)}">${esc(tr('reg.cert', 'Certificate'))}
      <span class="badge ${warn ? 'warn' : 'ok'}">${esc(tr('reg.certUntil', 'valid until {date}, {days} day(s) left',
        { date: dateOf(notAfter), days: Math.max(days, 0) }))}</span></p>`;
  }

  /** Fenêtre à trois onglets Paramètres, Vérifications, Résumé (comme le
   *  registre d'amorçage) ; rend { m, show, setCount }. */
  function tabbedOverlay(title, sub, paramsHtml, launchLabel, launchTip) {
    const tabs = ['params', 'checks', 'summary'];
    const TAB = { params: tr('reg.tab.params', 'Parameters'), checks: tr('reg.tab.checks', 'Checks'), summary: tr('reg.tab.summary', 'Summary') };
    const m = overlay(title, sub, `
      <div class="sub-tabs sub-tabs-inline reg-boot-tabs" role="tablist">${tabs.map((t, i) => `<button type="button" role="tab"
        class="sub-tab tip${i ? '' : ' active'}" data-reg-tab="${t}" data-tip="${esc(TAB[t])}" title="${esc(TAB[t])}">${esc(TAB[t])}${t === 'checks'
          ? ' <span class="badge dim" data-x="count"></span>' : ''}</button>`).join('')}</div>
      <section data-reg-pane="params">${paramsHtml}</section>
      <section data-reg-pane="checks" hidden><div class="xfer-report" data-x="report"><p class="form-hint">${esc(tr('reg.checking', 'Checking…'))}</p></div></section>
      <section data-reg-pane="summary" hidden><div data-x="summary"></div></section>
      <div class="xfer-live-line" data-x="progress" hidden></div>`,
      `<span class="tip" data-x="launch-wrap" data-tip="${esc(launchTip)}">${btn('data-reg-launch', 'btn-primary', 'play', launchLabel, launchTip, true)}</span>`);
    const show = (t) => {
      m.querySelectorAll('[data-reg-tab]').forEach(b => b.classList.toggle('active', b.dataset.regTab === t));
      m.querySelectorAll('[data-reg-pane]').forEach(p => { p.hidden = p.dataset.regPane !== t; });
    };
    m.querySelectorAll('[data-reg-tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.regTab)));
    return m;
  }

  function setLaunch(m, ok, why) {
    const launch = m.querySelector('[data-reg-launch]');
    launch.disabled = !ok;
    launch.title = why;
    m.querySelector('[data-x="launch-wrap"]').dataset.tip = why;
  }

  /** Erreur de l'outil (« code {faits} » ou « code: texte ») : le constat
   *  traduit s'il est connu, sinon le texte tel quel. */
  function errorFinding(text) {
    const mm = /^([a-z0-9-]+)(?::\s*|\s+)(\{.*\})?/.exec(String(text || ''));
    if (mm && FINDINGS[mm[1]]) {
      let facts = {};
      try { facts = mm[2] ? JSON.parse(mm[2]) : {}; } catch { facts = {}; }
      return { code: mm[1], level: 'block', facts };
    }
    return null;
  }
  const errorHtml = (text) => {
    const f = errorFinding(text);
    return f ? findingsHtml([f]) : `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(text)}</div></div>`;
  };

  /** Suit une action jusqu'à sa fin ; la dernière ligne d'étape reste lisible. */
  function followAction(m, id, render, onEnd) {
    const line = m.querySelector('[data-x="progress"]');
    line.hidden = false;
    line.textContent = tr('reg.started', 'Started (action {id}), see the dock.', { id });
    if (!window.SSEReconnect) return;
    const es = SSEReconnect.connect(`/api/stream/${enc(id)}`, {
      on: {
        step: (ev) => { try { const st = JSON.parse(ev.data); if (st.message) line.textContent = render(st.message); } catch { /* ligne illisible */ } },
        end: (ev) => {
          let x = {};
          try { x = JSON.parse(ev.data); } catch { /* fin sans détail */ }
          es.close();
          if (x.status !== 'done') line.textContent = tr('reg.actionFailed', 'The action failed: see the dock ({id}).', { id });
          onEnd(x.status === 'done');
        },
      },
    });
  }

  function openRenewCert() {
    const name = selected;
    const c = cluster();
    const noKey = !!caKeyMissing[name];
    const m = tabbedOverlay(tr('reg.renewTitle', 'Renew the certificate'), tr('reg.attachSub', '{registry} on {cluster}', { registry: name, cluster: c }), `
      <div class="tf-args">
        ${field(tr('reg.f.days', 'Validity (days)'), '<input data-f="days" type="number" min="1" max="3650" value="730">',
          tr('reg.t.days', 'Validity of the new server certificate, from 1 to 3650 days (the CA lasts 3650 days)'))}
        <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('reg.t.newCa', 'Needed when the CA of the registry was not kept: a new CA is created and trusted by every child cluster before the switch'))}">
          <input data-f="newca" type="checkbox"${noKey ? ' checked' : ''}><span class="tf-label">${esc(tr('reg.newCa', 'New CA (two steps)'))}</span></label>
      </div>
      <div class="tf-desc reg-newca-help" data-x="newca-help"${noKey ? '' : ' hidden'}>
        <p>${esc(tr('reg.newCaIntro', 'Two steps, nothing cut on the child clusters:'))}</p>
        <ol><li>${esc(tr('reg.newCaStep1', 'This Renew creates a new CA, kept next to the old one (transition).'))}</li>
          <li>${esc(tr('reg.newCaStep2', 'Each child cluster that uses the registry is renewed with Renew > CA (Clusters tab): its machines then trust both CAs.'))}</li>
          <li>${esc(tr('reg.newCaStep3', 'A second Renew here switches the server certificate to the new CA.'))}</li></ol></div>
      <p class="tf-desc">${esc(tr('reg.renewIntro', 'The pod of the registry restarts with the new certificate: reads are cut for a few seconds. The files of the nodes do not change.'))}</p>`,
      tr('reg.renewGo', 'Renew'), tr('reg.t.checking', 'Waiting for the checks of these values'));
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const body = () => Object.assign({ cluster: c, days: Number(v('days').value) }, v('newca').checked ? { new_ca: true } : {});
    const seq = { n: 0, t: null };
    async function runChecks() {
      const n = ++seq.n;
      const b = body();
      const r = await json('POST', `/api/registries/${enc(name)}/renew-cert`, Object.assign({ dry_run: true }, b));
      if (n !== seq.n || modal !== m) return;
      const d = r.d || {};
      const fs = [];
      let blocked = true;
      let reason = '';
      if (!r.ok) {
        reason = d.error || `HTTP ${r.status}`;
        m.querySelector('[data-x="report"]').innerHTML = errorHtml(reason);
        const f = errorFinding(reason);
        if (f) reason = findingText(f);
      } else {
        if (d.code === 'registry-ca-key-missing') {
          fs.push({ code: d.code, level: 'block', facts: { registry: name } });
          caKeyMissing[name] = true;
          m.querySelector('[data-x="newca-help"]').hidden = false;
        } else if (d.code === 'registry-ca-transition-pending') {
          fs.push({ code: d.code, level: 'block', facts: { registry: name, clusters: (d.clusters || []).join(', ') || '-' } });
        }
        blocked = !!d.blocked || fs.length > 0;
        reason = fs.length ? findingText(fs[0]) : (blocked ? (d.error || '?') : '');
        m.querySelector('[data-x="report"]').innerHTML = findingsHtml(fs)
          || `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('reg.noBlocker', 'No blocker.'))}</div></div>`;
        if (d.code === 'registry-ca-transition-pending' && ((d.pending || []).length || (d.clusters || []).length)) {
          const items = (d.pending || []).length
            ? d.pending.map(x => `<li><code>${esc(x.where)}</code> ${esc(pendingText(x))}</li>`)
            : d.clusters.map(x => `<li><code>${esc(x)}</code></li>`);
          m.querySelector('[data-x="report"]').insertAdjacentHTML('beforeend', `<ul class="reg-used" data-x="pending">${items.join('')}</ul>`);
        }
      }
      m.querySelector('[data-x="count"]').textContent = String(blocked ? Math.max(fs.length, 1) : 0);
      const step1 = r.ok && !blocked && d.step === 'transition';
      m.querySelector('[data-x="summary"]').innerHTML = `<ul class="reg-summary">${!r.ok || blocked ? `<li>${esc(tr('reg.t.launchBlocked', 'Blocked: {reason}', { reason }))}</li>`
        : step1 ? `<li>${esc(tr('reg.sum.newCa', 'Step 1: a new CA is created in transition; renew each child cluster with Renew > CA, then come back to switch the certificate.'))}</li>`
          : `<li>${esc(tr('reg.sum.until', 'New server certificate valid until {date}', { date: d.not_after ? dateOf(d.not_after) : '?' }))}${d.new_ca
            ? ` ${esc(tr('reg.sum.switch', '(signed by the new CA: the switch)'))}` : ''}</li>
            <li>${esc(tr('reg.sum.cut', 'The registry pod restarts: reads cut for a few seconds'))}</li>`}</ul>`;
      setLaunch(m, r.ok && !blocked, r.ok && !blocked ? tr('reg.t.renewGo', 'Renew the certificate now (tracked action)')
        : tr('reg.t.launchBlocked', 'Blocked: {reason}', { reason }));
    }
    const later = (ms) => {
      clearTimeout(seq.t);
      seq.n++;
      setLaunch(m, false, tr('reg.t.checking', 'Waiting for the checks of these values'));
      seq.t = setTimeout(runChecks, ms);
    };
    m._cleanup = () => { clearTimeout(seq.t); seq.n++; };
    m.addEventListener('input', () => later(500));
    m.addEventListener('change', (e) => {
      if (e.target === v('newca')) m.querySelector('[data-x="newca-help"]').hidden = !v('newca').checked && !caKeyMissing[name];
      later(200);
    });
    m.querySelector('[data-reg-launch]').addEventListener('click', async (e) => {
      const go = e.currentTarget;
      if (go.disabled) return;
      go.disabled = true;
      m._cleanup();
      const r = await json('POST', `/api/registries/${enc(name)}/renew-cert`, body());
      const fb = m.querySelector('[data-x="feedback"]');
      if (!r.ok) { fb.textContent = r.d.error || `HTTP ${r.status}`; go.disabled = false; return; }
      followAction(m, r.d.action_id, (msg) => {
        const mm = /valid until (\S+), reads cut (\S+) s/.exec(msg);
        return mm ? tr('reg.renewDone', 'Certificate valid until {date}; reads cut {cut} s.', { date: mm[1], cut: mm[2] }) : msg;
      }, () => setTimeout(refresh, 800));
    });
    runChecks();
  }

  function openNodes() {
    const name = selected;
    const c = cluster();
    const m = tabbedOverlay(tr('reg.nodesTitle', 'Registry of the Harvester nodes'), tr('reg.attachSub', '{registry} on {cluster}', { registry: name, cluster: c }), `
      <div class="tf-args">
        ${field(tr('reg.f.nodesMode', 'Change'), `<select data-f="mode"><option value="merge">${esc(tr('reg.nodesMerge', 'Add this registry to the nodes'))}</option>
          <option value="remove">${esc(tr('reg.nodesRemove', 'Remove this registry from the nodes'))}</option></select>`,
          tr('reg.t.nodesMode', 'Add: its mirrors, CA and read account are merged into containerd-registry, the other entries are kept. Remove: only its entry leaves'))}
      </div>
      <p class="tf-desc">${esc(tr('reg.nodesIntro', 'The Harvester nodes themselves pull through this registry (setting containerd-registry, CA in additional-ca). The previous value is kept, masked, in the result of the action (Activity): it gives the way back.'))}</p>
      <label class="tf-field tf-type-bool tip reg-rke2-ok" data-tip="${esc(tr('reg.t.rke2Ok', 'Harvester restarts RKE2 on each node, one after the other, to apply the setting'))}">
        <input data-f="rke2" type="checkbox"><span class="tf-label">${esc(tr('reg.rke2Ok', 'I accept that Harvester restarts RKE2 on every node'))}</span></label>`,
      tr('reg.nodesGo', 'Apply'), tr('reg.t.checking', 'Waiting for the checks of these values'));
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const body = () => Object.assign({ cluster: c }, v('mode').value === 'remove' ? { remove: true } : {});
    const seq = { n: 0, t: null };
    let last = null;                       // { ok, blocked, changed, reason, sig }
    const sig = () => JSON.stringify(body());
    function paintLaunch() {
      if (!last || last.sig !== sig()) { setLaunch(m, false, tr('reg.t.checking', 'Waiting for the checks of these values')); return; }
      if (!last.ok || last.blocked) { setLaunch(m, false, tr('reg.t.launchBlocked', 'Blocked: {reason}', { reason: last.reason })); return; }
      if (!last.changed) { setLaunch(m, false, tr('reg.t.nodesNothing', 'Nothing to change: the nodes already have this value')); return; }
      if (!v('rke2').checked) { setLaunch(m, false, tr('reg.t.rke2Needed', 'Accept the restart of RKE2 on every node first')); return; }
      setLaunch(m, true, tr('reg.t.nodesGo', 'Write the setting on the Harvester cluster (tracked action)'));
    }
    async function runChecks() {
      const n = ++seq.n;
      const mine = sig();
      const r = await json('POST', `/api/registries/${enc(name)}/harvester-nodes`, Object.assign({ dry_run: true }, body()));
      if (n !== seq.n || modal !== m) return;
      const d = r.d || {};
      const rep = m.querySelector('[data-x="report"]');
      if (!r.ok) {
        const reason = d.error || `HTTP ${r.status}`;
        const f = errorFinding(reason);
        last = { ok: false, sig: mine, reason: f ? findingText(f) : reason };
        rep.innerHTML = errorHtml(reason);
        m.querySelector('[data-x="count"]').textContent = '1';
      } else {
        const fs = d.findings || [];
        const firstBlock = fs.find(f => f.level === 'block');
        last = { ok: true, sig: mine, blocked: !!d.blocked, changed: !!d.changed,
          reason: firstBlock ? findingText(firstBlock) : (d.error || '?') };
        rep.innerHTML = findingsHtml(fs) + (d.changed ? '' : `<div class="sto-finding sev-info" data-code="nothing"><div class="sto-finding-title">${esc(tr('reg.t.nodesNothing', 'Nothing to change: the nodes already have this value'))}</div></div>`)
          + `<h4>${esc(tr('reg.nodesPrevious', 'Current value (masked)'))}</h4><pre class="capi-yaml reg-setting" data-x="previous">${esc(d.previous || '-')}</pre>
             <h4>${esc(tr('reg.nodesNew', 'New value (masked)'))}</h4><pre class="capi-yaml reg-setting" data-x="value">${esc(d.value || '-')}</pre>
             <p class="form-hint">${esc(d.additional_ca_changed ? tr('reg.nodesCaChanged', 'The CA of the registry is added to the setting additional-ca.')
               : tr('reg.nodesCaSame', 'The setting additional-ca does not change.'))}</p>`;
        m.querySelector('[data-x="count"]').textContent = String(fs.filter(f => f.level === 'block').length);
      }
      m.querySelector('[data-x="summary"]').innerHTML = `<ul class="reg-summary">
        <li>${esc(v('mode').value === 'remove' ? tr('reg.sum.nodesRemove', 'The entry of {registry} leaves containerd-registry of {cluster}', { registry: name, cluster: c })
          : tr('reg.sum.nodesMerge', '{registry} is merged into containerd-registry of {cluster}; the other entries are kept', { registry: name, cluster: c }))}</li>
        <li>${esc(tr('reg.sum.rke2', 'Harvester restarts RKE2 on every node'))}</li>
        <li>${esc(tr('reg.sum.rollback', 'The previous value stays, masked, in the result of the action (Activity)'))}</li></ul>`;
      paintLaunch();
    }
    const later = (ms) => {
      clearTimeout(seq.t);
      seq.n++;
      paintLaunch();
      seq.t = setTimeout(runChecks, ms);
    };
    m._cleanup = () => { clearTimeout(seq.t); seq.n++; };
    m.addEventListener('change', (e) => { if (e.target === v('rke2')) paintLaunch(); else later(200); });
    m.querySelector('[data-reg-launch]').addEventListener('click', async (e) => {
      const go = e.currentTarget;
      if (go.disabled) return;
      go.disabled = true;
      m._cleanup();
      const r = await json('POST', `/api/registries/${enc(name)}/harvester-nodes`, body());
      const fb = m.querySelector('[data-x="feedback"]');
      if (!r.ok) { fb.textContent = r.d.error || `HTTP ${r.status}`; go.disabled = false; return; }
      followAction(m, r.d.action_id, (msg) => msg, () => setTimeout(refresh, 800));
    });
    runChecks();
  }

  // -------------------------------------------------------------------------
  // Identifiants (rattachement, déclaration) : jamais réaffichés
  // -------------------------------------------------------------------------
  function credsHtml() {
    return `<fieldset class="tf-block"><legend>${esc(tr('reg.accounts', 'Accounts'))}</legend><div class="tf-args">
      ${field(tr('reg.pushUser', 'Write account'), '<input data-c="push-user" type="text" autocomplete="off" spellcheck="false">',
        tr('reg.t.pushUser', 'Account the console publishes with; it stays in a Secret of the Harvester cluster'))}
      ${field(tr('reg.password', 'Password'), '<input data-c="push-pass" type="password" autocomplete="new-password">',
        tr('reg.t.password', 'Sent once, never shown again'))}
      ${field(tr('reg.pullUser', 'Read account'), '<input data-c="pull-user" type="text" autocomplete="off" spellcheck="false">',
        tr('reg.t.pullUser', 'Account the child clusters pull with; it is written on every child node. Empty: the write account is used, with a warning'))}
      ${field(tr('reg.password', 'Password'), '<input data-c="pull-pass" type="password" autocomplete="new-password">',
        tr('reg.t.password', 'Sent once, never shown again'))}
    </div></fieldset>`;
  }

  function readCreds(root) {
    const v = (k) => (root.querySelector(`[data-c="${k}"]`) || {}).value || '';
    if (!v('push-user')) return null;
    const out = { push: { username: v('push-user'), password: v('push-pass') } };
    if (v('pull-user')) out.pull = { username: v('pull-user'), password: v('pull-pass') };
    return out;
  }

  function clearCreds(root) {
    root.querySelectorAll('[data-c]').forEach(el => { el.value = ''; });
  }

  async function attach(name, creds, fb) {
    const r = await json('POST', `/api/registries/${enc(name)}/attach/${enc(cluster())}`, creds);
    if (!r.ok) { fb.textContent = r.d.error || `HTTP ${r.status}`; return false; }
    fb.textContent = (r.d.warnings || []).includes('registry-pull-is-push')
      ? findingText({ code: 'registry-pull-is-push', facts: { registry: name } })
      : tr('reg.attachStarted', 'Attaching (action {id}), see the dock.', { id: r.d.action_id });
    return true;
  }

  function openAttach() {
    const name = selected;
    const m = overlay(tr('reg.attachTitle', 'Attach a registry'), tr('reg.attachSub', '{registry} on {cluster}', { registry: name, cluster: cluster() }),
      `<p class="tf-desc">${esc(tr('reg.attachIntro', 'The accounts are posed as Secrets on the Harvester cluster. Only the read account reaches the child clusters.'))}</p>${credsHtml()}`,
      btn('data-reg-go', 'btn-primary', 'ok', tr('reg.attach', 'Attach'), tr('reg.t.attachGo', 'Pose the accounts on the Harvester cluster')));
    m.querySelector('[data-reg-go]').addEventListener('click', async () => {
      const creds = readCreds(m);
      const fb = m.querySelector('[data-x="feedback"]');
      clearCreds(m);                                 // jamais réaffichés, même après un refus
      if (!creds) { fb.textContent = tr('reg.needPush', 'The write account is required.'); return; }
      if (await attach(name, creds, fb)) setTimeout(refresh, 1500);
    });
  }

  function openDeclare() {
    const m = overlay(tr('reg.declareTitle', 'Declare a client registry'), tr('reg.declareSub', 'An existing registry of your organisation'),
      `<div class="tf-args">
        ${field(tr('reg.f.name', 'Name'), '<input data-f="name" type="text" maxlength="30" spellcheck="false" placeholder="harbor-client">',
          tr('reg.t.name', 'Lower case letters, digits and dashes, 30 characters at most'))}
        ${field(tr('reg.col.endpoint', 'Address'), '<input data-f="endpoint" type="text" spellcheck="false" placeholder="harbor.example:443">',
          tr('reg.t.endpoint', 'host[:port], without https:// nor path'))}
        ${field(tr('reg.mirrors', 'Mirrors'), '<input data-f="mirrors" type="text" spellcheck="false" placeholder="docker.io, ghcr.io">',
          tr('reg.t.mirrors', 'Origin registries it serves, comma separated; the child clusters pull their images from it'))}
        <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('reg.t.usageImages', 'The nodes pull images from it'))}"><input data-f="images" type="checkbox" checked><span class="tf-label">${esc(tr('reg.usage.images', 'Images'))}</span></label>
        <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('reg.t.usageCharts', 'CAAPH pulls charts from it'))}"><input data-f="charts" type="checkbox" checked><span class="tf-label">${esc(tr('reg.usage.charts', 'Charts'))}</span></label>
        ${field(tr('reg.ca', 'CA'), '<textarea data-f="ca" rows="4" spellcheck="false" placeholder="-----BEGIN CERTIFICATE-----"></textarea>',
          tr('reg.t.ca', 'Public CA certificate (PEM) of the registry; empty: the system CAs'))}
      </div>
      <p class="tf-desc">${esc(tr('reg.declareAttach', 'Optional: attach it to {cluster} at once.', { cluster: cluster() }))}</p>${credsHtml()}`,
      btn('data-reg-go', 'btn-primary', 'save', tr('reg.declare', 'Declare'), tr('reg.t.declareGo', 'Save the declaration (no secret), then attach it if accounts are given')));
    m.querySelector('[data-reg-go]').addEventListener('click', async () => {
      const v = (k) => m.querySelector(`[data-f="${k}"]`);
      const fb = m.querySelector('[data-x="feedback"]');
      const name = v('name').value.trim();
      const body = { kind: 'external', endpoint: v('endpoint').value.trim(),
        mirrors: v('mirrors').value.split(',').map(x => x.trim()).filter(Boolean),
        usage: ['images', 'charts'].filter(u => v(u).checked) };
      if (v('ca').value.trim()) body.ca_pem = v('ca').value;
      const creds = readCreds(m);
      clearCreds(m);
      const r = await json('PUT', `/api/registries/${enc(name)}`, body);
      if (!r.ok) {
        fb.textContent = r.d.errors ? r.d.errors.map(e => `${e[0]}: ${e[1]}`).join('; ') : (r.d.error || `HTTP ${r.status}`);
        return;
      }
      selected = name;
      if (creds) await attach(name, creds, fb);
      else closeModal();
      refresh();
    });
  }

  // -------------------------------------------------------------------------
  // Registre d'amorçage : Paramètres, Vérifications, Résumé
  // -------------------------------------------------------------------------
  async function openBootstrap() {
    const c = cluster();
    let classes = [];
    const inv = await json('GET', `/api/capi/${enc(c)}/inventory`).catch(() => null);
    if (inv && inv.ok) classes = (inv.d.storage_classes || []).map(x => x.name);
    const tabs = ['params', 'checks', 'summary'];
    const TAB = { params: tr('reg.tab.params', 'Parameters'), checks: tr('reg.tab.checks', 'Checks'), summary: tr('reg.tab.summary', 'Summary') };
    const m = overlay(tr('reg.bootTitle', 'Bootstrap registry'), tr('reg.bootSub', 'A registry posed by the console on {cluster}', { cluster: c }), `
      <div class="sub-tabs sub-tabs-inline reg-boot-tabs" role="tablist">${tabs.map((t, i) => `<button type="button" role="tab"
        class="sub-tab tip${i ? '' : ' active'}" data-reg-tab="${t}" data-tip="${esc(TAB[t])}">${esc(TAB[t])}${t === 'checks'
          ? ' <span class="badge dim" data-x="count"></span>' : ''}</button>`).join('')}</div>
      <section data-reg-pane="params"><div class="tf-args">
        ${field(tr('reg.f.name', 'Name'), '<input data-f="name" type="text" maxlength="30" spellcheck="false" value="amorcage">',
          tr('reg.t.name', 'Lower case letters, digits and dashes, 30 characters at most'))}
        ${field(tr('reg.f.address', 'Address'), '<input data-f="address" type="text" spellcheck="false" placeholder="172.16.2.76">',
          tr('reg.t.address', 'A free IPv4 address of the management network, outside the IP pools; served by the kube-vip of Harvester'))}
        ${field(tr('reg.f.size', 'Volume (GiB)'), '<input data-f="size" type="number" min="5" max="2048" value="20">',
          tr('reg.t.size', 'Size of the Longhorn volume, from 5 to 2048 GiB; checked against the room of Longhorn'))}
        ${field(tr('reg.f.class', 'Storage class'), classes.length
          ? `<select data-f="class">${classes.map(x => `<option>${esc(x)}</option>`).join('')}</select>`
          : '<input data-f="class" type="text" spellcheck="false">', tr('reg.t.class', 'Longhorn class of the volume (its replicas count in the room)'))}
        ${field(tr('reg.mirrors', 'Mirrors'), '<input data-f="mirrors" type="text" spellcheck="false" placeholder="ghcr.io, docker.io">',
          tr('reg.t.mirrors', 'Origin registries it serves, comma separated; the child clusters pull their images from it'))}
        ${field(tr('reg.f.dns', 'DNS name (optional)'), '<input data-f="dns" type="text" spellcheck="false">',
          tr('reg.t.dns', 'Added to the certificate next to the address'))}
      </div><p class="tf-desc">${esc(tr('reg.bootIntro', 'TLS always, two accounts (write and read) generated, never anonymous. The read account cannot write.'))}</p></section>
      <section data-reg-pane="checks" hidden><div class="xfer-report" data-x="report"><p class="form-hint">${esc(tr('reg.checking', 'Checking…'))}</p></div></section>
      <section data-reg-pane="summary" hidden><div data-x="summary"></div></section>`,
      `<span class="tip" data-x="launch-wrap">${btn('data-reg-launch', 'btn-primary', 'play', tr('reg.launch', 'Launch'),
        tr('reg.t.launch', 'Pose the registry on the Harvester cluster'), true)}</span>`);
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const show = (t) => {
      m.querySelectorAll('[data-reg-tab]').forEach(b => b.classList.toggle('active', b.dataset.regTab === t));
      m.querySelectorAll('[data-reg-pane]').forEach(p => { p.hidden = p.dataset.regPane !== t; });
    };
    m.querySelectorAll('[data-reg-tab]').forEach(b => b.addEventListener('click', () => show(b.dataset.regTab)));
    const body = () => ({ name: v('name').value.trim(), address: v('address').value.trim(),
      size_gib: Number(v('size').value), storage_class: v('class').value.trim(),
      mirrors: v('mirrors').value.split(',').map(x => x.trim()).filter(Boolean),
      ...(v('dns').value.trim() ? { dns_name: v('dns').value.trim() } : {}) });
    const seq = { n: 0, t: null };
    async function runChecks() {
      const n = ++seq.n;
      const b = body();
      const launch = m.querySelector('[data-reg-launch]');
      const wrap = m.querySelector('[data-x="launch-wrap"]');
      const r = await json('POST', `/api/registries/bootstrap/${enc(c)}`, Object.assign({ dry_run: true }, b));
      if (n !== seq.n || !modal) return;
      let blocked = true;
      let reason = '';
      if (!r.ok) {
        reason = r.d.errors ? r.d.errors.map(e => `${e[0]}: ${e[1]}`).join('; ') : (r.d.error || `HTTP ${r.status}`);
        m.querySelector('[data-x="report"]').innerHTML = `<div class="sto-finding sev-critical"><div class="sto-finding-title">${esc(reason)}</div></div>`;
      } else {
        const fs = r.d.findings || [];
        blocked = !!r.d.blocked;
        const first = fs.find(f => f.level === 'block');
        reason = first ? findingText(first) : '';
        m.querySelector('[data-x="report"]').innerHTML = findingsHtml(fs) || `<div class="sto-finding sev-info"><div class="sto-finding-title">${esc(tr('reg.noBlocker', 'No blocker.'))}</div></div>`;
        m.querySelector('[data-x="count"]').textContent = String(fs.filter(f => f.level === 'block').length);
      }
      launch.disabled = blocked || !can('admin');
      const why = !can('admin') ? tr('reg.t.adminOnly', 'Posing a bootstrap registry needs the admin role')
        : blocked ? tr('reg.t.launchBlocked', 'Blocked: {reason}', { reason }) : tr('reg.t.launch', 'Pose the registry on the Harvester cluster');
      wrap.dataset.tip = why;
      launch.title = why;
      m.querySelector('[data-x="summary"]').innerHTML = `<ul class="reg-summary">
        <li>${esc(tr('reg.sum.where', 'Namespace harvester-ops-registry of {cluster}, address {address}', { cluster: c, address: b.address || '?' }))}</li>
        <li>${esc(tr('reg.sum.volume', 'Longhorn volume of {size} GiB on {klass}', { size: b.size_gib, klass: b.storage_class || '?' }))}</li>
        <li>${esc(tr('reg.sum.accounts', 'Accounts push (write) and pull (read), random passwords kept in Secrets'))}</li>
        <li>${esc(tr('reg.sum.mirrors', 'Mirrors: {mirrors}', { mirrors: b.mirrors.join(', ') || '-' }))}</li></ul>`;
    }
    // revue finale m-1 : Lancer grisé dès une saisie, jusqu'aux constats de
    // ces valeurs ; aucune vérification différée après Lancer ou la fermeture
    const later = (ms) => {
      clearTimeout(seq.t);
      seq.n++;
      m.querySelector('[data-reg-launch]').disabled = true;
      seq.t = setTimeout(runChecks, ms);
    };
    m._cleanup = () => { clearTimeout(seq.t); seq.n++; };
    m.addEventListener('input', () => later(500));
    m.addEventListener('change', () => later(200));
    m.querySelector('[data-reg-launch]').addEventListener('click', async (e) => {
      if (e.currentTarget.disabled) return;
      e.currentTarget.disabled = true;
      m._cleanup();
      const b = body();
      const r = await json('POST', `/api/registries/bootstrap/${enc(c)}`, b);
      const fb = m.querySelector('[data-x="feedback"]');
      if (!r.ok) { fb.textContent = r.d.error || `HTTP ${r.status}`; e.currentTarget.disabled = false; return; }
      selected = b.name;
      closeModal();
      setTimeout(refresh, 1500);
    });
    runChecks();
  }

  // -------------------------------------------------------------------------
  // Suppression : nom retapé pour supprimer les données
  // -------------------------------------------------------------------------
  // Oubli d'un registre du client (revue finale m-4 : fenêtre de la page,
  // refus traduits ; I-3 : action suivie, progression dans le dock)
  const forgetError = (name, r) => {
    const code = r.d && r.d.code;
    if (code === 'registry-in-use') {
      return tr('reg.forget.inUse', 'Refused: {registry} is used by {users}.', { registry: name, users: (r.d.used_by || []).join(', ') });
    }
    if (code === 'registry-usage-unknown') {
      return tr('reg.forget.usageUnknown', 'Refused: what uses {registry} cannot be read ({error}).', { registry: name, error: r.d.error || '' });
    }
    if (code === 'registry-bootstrap') return tr('reg.forget.bootstrap', 'A bootstrap registry is removed with Delete, not forgotten.');
    if (code === 'registry-unknown') return tr('reg.forget.unknown', 'The registry {registry} is not declared any more.', { registry: name });
    return (r.d && r.d.error) || `HTTP ${r.status}`;
  };

  function openForget(name) {
    const m = overlay(tr('reg.forgetTitle', 'Forget a client registry'), name,
      `<p>${esc(tr('reg.confirmForget', 'Forget the declaration of {registry}? Its accounts are removed from the attached Harvester clusters. The registry itself is not touched.', { registry: name }))}</p>`,
      btn('data-reg-go', 'btn-danger', 'trash', tr('reg.forget', 'Forget'),
        tr('reg.t.forgetGo', 'Remove its accounts from each attached Harvester cluster, then the declaration; each step is in Activity')));
    const go = m.querySelector('[data-reg-go]');
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      go.disabled = true;
      const r = await json('DELETE', `/api/registries/${enc(name)}`);
      const fb = m.querySelector('[data-x="feedback"]');
      if (!r.ok) { fb.textContent = forgetError(name, r); go.disabled = false; return; }
      fb.textContent = tr('reg.forgetStarted', 'Forgetting (action {id}), see the dock.', { id: r.d.action_id });
      selected = null;
      setTimeout(refresh, 1500);
    });
  }

  function openDelete() {
    const name = selected;
    const d = (list || []).find(x => x.name === name) || {};
    if (d.kind !== 'bootstrap' && d.kind !== 'leftover') { openForget(name); return; }
    // revue finale m-2 : le cluster du registre, pas celui choisi dans la page
    const where = d.cluster || cluster();
    const m = overlay(tr('reg.deleteTitle', 'Delete a bootstrap registry'), name, `
      <p>${esc(tr('reg.deleteIntro', 'The registry leaves the cluster. Its volume is kept: a new deployment finds its images again.'))}</p>
      <label class="tf-field tf-type-bool tip" data-tip="${esc(tr('reg.t.deleteData', 'Also delete the volume and every image and chart it holds'))}">
        <input data-f="data" type="checkbox"><span class="tf-label">${esc(tr('reg.deleteData', 'Delete its data too'))}</span></label>
      <div data-x="confirm" hidden>${field(tr('reg.typeName', 'Type {registry} to confirm', { registry: name }),
        '<input data-f="typed" type="text" autocomplete="off" spellcheck="false">', tr('reg.t.typeName', 'The data cannot be recovered'))}</div>`,
      btn('data-reg-go', 'btn-danger', 'trash', tr('reg.delete', 'Delete'), tr('reg.t.deleteGo', 'Remove the registry from the cluster')));
    const v = (k) => m.querySelector(`[data-f="${k}"]`);
    const go = m.querySelector('[data-reg-go]');
    const sync = () => {
      m.querySelector('[data-x="confirm"]').hidden = !v('data').checked;
      go.disabled = v('data').checked && v('typed').value !== name;
    };
    m.addEventListener('input', sync);
    m.addEventListener('change', sync);
    go.addEventListener('click', async () => {
      if (go.disabled) return;
      const body = v('data').checked ? { delete_data: true, confirm: v('typed').value } : {};
      const r = await json('DELETE', `/api/registries/bootstrap/${enc(where)}/${enc(name)}`, body);
      const fb = m.querySelector('[data-x="feedback"]');
      if (!r.ok) { fb.textContent = r.d.error || `HTTP ${r.status}`; return; }
      selected = null;
      closeModal();
      setTimeout(refresh, 1500);
    });
    sync();
  }

  function init() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('#btn-registries-bootstrap')) { openBootstrap(); return; }
      if (e.target.closest('#btn-registries-declare')) { openDeclare(); return; }
      if (e.target.closest('#btn-registries-refresh')) { refresh(); return; }
      const row = e.target.closest('.reg-row');
      if (row) { showDetail(row.dataset.reg); return; }
      if (e.target.closest('[data-reg-attach]')) { openAttach(); return; }
      if (e.target.closest('[data-reg-check]')) { check(); return; }
      if (e.target.closest('[data-reg-renew]')) { openRenewCert(); return; }
      if (e.target.closest('[data-reg-nodes]')) { openNodes(); return; }
      if (e.target.closest('[data-reg-delete]')) openDelete();
    });
    document.addEventListener('keydown', (e) => {
      const row = e.target.closest && e.target.closest('.reg-row');
      if (row && e.key === 'Enter') showDetail(row.dataset.reg);
    });
  }

  document.addEventListener('DOMContentLoaded', init);
  // -------------------------------------------------------------------------
  // Paramètres > Registres (2.6.1) : port d'écriture réservé à la console
  // -------------------------------------------------------------------------
  async function loadSettings() {
    const box = document.getElementById('registries-settings-body');
    if (!box) return;
    const c = cluster();
    const r = await json('GET', `/api/registries/settings${c ? `?cluster=${enc(c)}` : ''}`).catch(() => null);
    if (!r || !r.ok) { box.innerHTML = `<p class="form-hint">${esc(tr('regset.loadError', 'The registry settings could not be read.'))}</p>`; return; }
    const s = r.d;
    const sugg = Object.entries(s.suggested || {}).map(([cl, a]) => btn(`data-regset-use="${esc(a)}"`, 'btn-secondary',
      'add', tr('regset.suggest', 'Use {address} (toward {cluster})', { address: a, cluster: cl }),
      tr('regset.t.suggest', 'The address the console leaves from toward this cluster; behind a NAT, give the translated address instead'))).join(' ');
    const where = Object.entries(s.bootstrap || {}).map(([cl, ns]) => `${cl} (${ns.join(', ')})`).join(', ');
    box.innerHTML = `
      <div class="form-row"><label for="regset-sources">${esc(tr('regset.sources', 'Networks of the console'))}</label>
        <input type="text" id="regset-sources" class="form-input tip" spellcheck="false" placeholder="172.16.1.11/32"
               value="${esc((s.write_sources || []).join(', '))}"
               data-tip="${esc(tr('regset.t.sources', 'IPv4 addresses or networks, separated by commas (a.b.c.d or a.b.c.d/n); empty: every address'))}"
               title="${esc(tr('regset.t.sources', 'IPv4 addresses or networks, separated by commas (a.b.c.d or a.b.c.d/n); empty: every address'))}">
        <div class="form-hint">${esc(tr('regset.open', 'Empty: the write port accepts every address.'))}</div>
        <div class="reg-actions">${sugg}
          ${btn('data-regset-save', 'btn-primary', 'save', tr('regset.save', 'Save'),
            tr('regset.t.save', 'Save, then bring every bootstrap registry to this setting (one tracked action per cluster)'))}</div></div>
      <p class="form-hint">${esc(where ? tr('regset.current', 'Applied on: {clusters}', { clusters: where }) : tr('regset.none', 'No bootstrap registry yet.'))}</p>
      <p class="form-hint">${esc(tr('regset.limits', 'This keeps other hosts of the network away from the write port. It does not replace the password of the write account, and it does not see through a NAT: the cluster must see the real address of the console.'))}</p>
      <div class="res-feedback" id="regset-feedback">${s.error ? esc(s.error) : ''}</div>`;
  }

  async function saveSettings() {
    const v = (document.getElementById('regset-sources') || {}).value || '';
    const r = await json('PUT', '/api/registries/settings', { write_sources: v.split(/[\s,]+/).filter(Boolean) });
    if (r.ok) await loadSettings();
    const fb = document.getElementById('regset-feedback');
    if (fb) fb.textContent = r.ok ? tr('regset.saved', 'Saved; {n} cluster(s) being updated (Activity).', { n: (r.d.actions || []).length })
      : (r.d.error || `HTTP ${r.status}`);
  }

  function initSettings() {
    document.addEventListener('click', (e) => {
      const use = e.target.closest('[data-regset-use]');
      if (use) {
        const inp = document.getElementById('regset-sources');
        if (inp) {
          const cur = inp.value.split(/[\s,]+/).filter(Boolean);
          if (!cur.includes(use.dataset.regsetUse)) cur.push(use.dataset.regsetUse);
          inp.value = cur.join(', ');
        }
        return;
      }
      if (e.target.closest('[data-regset-save]')) saveSettings();
    });
    document.querySelector('.settings-tab[data-stab="registries"]')?.addEventListener('click', loadSettings);
  }
  document.addEventListener('DOMContentLoaded', initSettings);

  return { start: refresh, refresh, openBootstrap, openDeclare, loadSettings, vmFacts, _findingText: findingText };
})();
window.Registries = Registries;
