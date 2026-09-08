/**
 * harvester-ops — VM edit panel (stage 3: visual disk & network editors).
 *
 * Sections:
 *   - General   (description, runStrategy)
 *   - Compute   (CPU sockets/cores/threads, memory.guest)
 *   - Disks     (v1.8.0: card-based editor over volumes + devices.disks +
 *                the harvesterhci.io/volumeClaimTemplates annotation for
 *                NEW disks — blank or from a Harvester image. Raw JSON
 *                stays available behind an "advanced" fold.)
 *   - Network   (v1.8.0: card-based editor over networks + interfaces)
 *   - Cloud-init (separate Secret patch via /cloudinit endpoint)
 *   - Lifecycle (terminationGracePeriodSeconds, evictionStrategy)
 *
 * The card editors reuse the TFForm engine (schema-driven fields, ref
 * dropdowns fed by /api/{pvcs,images,storageclasses,networks}, repeatable
 * blocks). KubeVirt models disks/networks as PAIRS of arrays joined by
 * name (volumes[]<->devices.disks[], networks[]<->devices.interfaces[]);
 * the mappers below flatten for the form and rebuild FULL arrays for the
 * merge patch (merge patch replaces arrays wholesale). Volumes the form
 * does not understand (cloud-init, containerDisk, lun…) are carried
 * through untouched.
 */
const VMEdit = (() => {
  const tr = (k, fb) => {
    try { const v = window.i18n && i18n.t(k); return v && v !== k ? v : fb; }
    catch { return fb; }
  };

  const SECTIONS = [
    { id: 'general',   label: () => tr('vm.edit.general', 'General'),      icon: '📋' },
    { id: 'compute',   label: () => tr('vm.edit.compute', 'Compute'),      icon: '🧠' },
    { id: 'disks',     label: () => tr('vm.edit.disks', 'Disks'),          icon: '💾' },
    { id: 'network',   label: () => tr('vm.edit.network', 'Network'),      icon: '🔌' },
    { id: 'cloudinit', label: () => tr('vm.edit.cloudinit', 'Cloud-init'), icon: '☁️' },
    { id: 'lifecycle', label: () => tr('vm.edit.lifecycle', 'Lifecycle'),  icon: '🔁' },
  ];

  // Per-open refresh callbacks. v1.6.x stored this on the panel API object
  // but looked it up on the DOM node — Reset and post-Apply refresh were
  // silently dead. A module registry keyed by panel id is unambiguous.
  const refreshers = new Map();

  // =========================================================================
  // Schemas for the TFForm engine (NOT registered in TF_SCHEMA — passed as
  // objects; test_tf_schema.py asserts every TF_SCHEMA kind has an HCL
  // backend branch, which these editors do not have or need).
  // =========================================================================
  const K8S_NAME_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
  const MAC_RE = /^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$/;

  const DISK_SCHEMA = {
    id: 'vm-disks',
    nested: {
      disk: {
        min: 0, max: 16,
        label: { en: 'Disks', fr: 'Disques' },
        itemTitle: (v) => `💾 ${v.name || tr('vm.edit.newDisk', 'new disk')}`
          + `${v.bus ? ' — ' + v.bus : ''}${v.size ? ' · ' + v.size : ''}`
          + `${v.boot_order > 0 ? ' · boot #' + v.boot_order : ''}`,
        args: [
          { name: 'name', type: 'text', required: true, validate: K8S_NAME_RE,
            label: { en: 'Name', fr: 'Nom' },
            description: { en: 'Joins the volume and the guest device (RFC 1123)',
                           fr: 'Relie le volume au périphérique invité (RFC 1123)' } },
          { name: 'device', type: 'enum', default: 'disk', enum_values: ['disk', 'cdrom'],
            label: { en: 'Device', fr: 'Périphérique' },
            description: { en: 'How the guest sees it', fr: 'Ce que voit l’invité' } },
          { name: 'bus', type: 'enum', default: 'virtio', enum_values: ['virtio', 'sata', 'scsi'],
            label: { en: 'Bus', fr: 'Bus' },
            description: { en: 'virtio is fastest; sata/scsi for guests without virtio drivers',
                           fr: 'virtio est le plus rapide ; sata/scsi pour les invités sans pilotes virtio' } },
          { name: 'boot_order', type: 'int', default: 0, min: 0, max: 64,
            label: { en: 'Boot order', fr: 'Ordre de boot' },
            description: { en: '0 = not part of the boot order',
                           fr: '0 = hors de l’ordre de boot' } },
          { name: 'source', type: 'enum', default: 'pvc',
            enum_values: ['pvc', 'image', 'blank'],
            label: { en: 'Volume source', fr: 'Source du volume' },
            description: { en: 'pvc = attach an existing volume · image = NEW disk from a Harvester image · blank = NEW empty disk',
                           fr: 'pvc = attacher un volume existant · image = NOUVEAU disque depuis une image Harvester · blank = NOUVEAU disque vierge' } },
          { name: 'pvc', type: 'ref', ref_endpoint: '/api/pvcs', ref_namespaced: true,
            label: { en: 'Existing PVC', fr: 'PVC existant' },
            description: { en: 'Must live in the VM’s namespace',
                           fr: 'Doit être dans le namespace de la VM' } },
          { name: 'image', type: 'ref', ref_endpoint: '/api/images', ref_namespaced: true,
            ref_label_field: 'display_name',
            label: { en: 'VM image', fr: 'Image VM' },
            description: { en: 'The new PVC inherits the image’s storage class',
                           fr: 'Le nouveau PVC hérite de la storage class de l’image' } },
          { name: 'size', type: 'text', validate: /^[0-9]+(Mi|Gi|Ti)$/,
            label: { en: 'Size (new disk)', fr: 'Taille (nouveau disque)' },
            description: { en: 'e.g. 10Gi — for image sources, at least the image size',
                           fr: 'ex. 10Gi — pour une image, au moins la taille de l’image' } },
          { name: 'storage_class', type: 'ref', ref_endpoint: '/api/storageclasses',
            label: { en: 'Storage class', fr: 'Storage class' },
            description: { en: 'Blank disks only (image disks inherit theirs); empty = cluster default',
                           fr: 'Disques vierges seulement (hérité pour les images) ; vide = défaut du cluster' } },
        ],
      },
    },
  };

  const NET_SCHEMA = {
    id: 'vm-networks',
    nested: {
      nic: {
        min: 0, max: 8,
        label: { en: 'Network interfaces', fr: 'Interfaces réseau' },
        itemTitle: (v) => `🔌 ${v.name || tr('vm.edit.newNic', 'new interface')}`
          + `${v.type ? ' — ' + v.type : ''}${v.network ? ' · ' + v.network : ''}`,
        args: [
          { name: 'name', type: 'text', required: true, validate: K8S_NAME_RE,
            label: { en: 'Name', fr: 'Nom' },
            description: { en: 'Joins the network and the guest interface',
                           fr: 'Relie le réseau à l’interface invité' } },
          { name: 'type', type: 'enum', default: 'bridge', enum_values: ['bridge', 'masquerade'],
            label: { en: 'Binding', fr: 'Attachement' },
            description: { en: 'bridge = L2 on a VLAN network (Harvester default) · masquerade = NAT on the pod network',
                           fr: 'bridge = L2 sur un réseau VLAN (défaut Harvester) · masquerade = NAT sur le réseau des pods' } },
          { name: 'network', type: 'ref', ref_endpoint: '/api/networks', ref_namespaced: true,
            label: { en: 'Network (multus)', fr: 'Réseau (multus)' },
            description: { en: 'NetworkAttachmentDefinition — bridge mode only',
                           fr: 'NetworkAttachmentDefinition — mode bridge uniquement' } },
          { name: 'model', type: 'enum', default: 'virtio',
            enum_values: ['virtio', 'e1000', 'e1000e', 'rtl8139'],
            label: { en: 'Model', fr: 'Modèle' },
            description: { en: 'virtio needs guest drivers; e1000 for legacy guests',
                           fr: 'virtio requiert des pilotes invité ; e1000 pour les invités anciens' } },
          { name: 'mac', type: 'text', validate: MAC_RE,
            label: { en: 'MAC address', fr: 'Adresse MAC' },
            description: { en: 'Empty = auto-generated. Changing it may break DHCP leases',
                           fr: 'Vide = auto-générée. La changer peut casser les baux DHCP' } },
        ],
      },
    },
  };

  // =========================================================================
  // Mappers — KubeVirt pairs of arrays <-> flat form items
  // =========================================================================
  const VCT_ANNOTATION = 'harvesterhci.io/volumeClaimTemplates';

  function vmDisksToForm(vm) {
    const template = ((vm.spec || {}).template || {}).spec || {};
    const disks = ((template.domain || {}).devices || {}).disks || [];
    const volumes = template.volumes || [];
    const volByName = Object.fromEntries(volumes.map(v => [v.name, v]));
    const usedVols = new Set();
    const items = [];
    const passthrough = { disks: [], volumes: [] };

    disks.forEach(d => {
      const vol = volByName[d.name];
      if (vol) usedVols.add(d.name);
      const devKey = d.cdrom ? 'cdrom' : (d.disk ? 'disk' : null);
      const editable = devKey && vol && vol.persistentVolumeClaim
        && !vol.cloudInitNoCloud && !vol.cloudInitConfigDrive;
      if (!editable) {
        passthrough.disks.push(d);
        if (vol) passthrough.volumes.push(vol);
        return;
      }
      items.push({
        name: d.name,
        device: devKey,
        bus: (d[devKey] || {}).bus || 'virtio',
        boot_order: d.bootOrder ?? 0,
        source: 'pvc',
        pvc: `${vm.metadata.namespace}/${vol.persistentVolumeClaim.claimName}`,
      });
    });
    // Orphan volumes (no matching disk entry) ride along untouched.
    volumes.forEach(v => { if (!usedVols.has(v.name)) passthrough.volumes.push(v); });
    return { items, passthrough };
  }

  function formDisksToPatch(items, passthrough, vm) {
    const ns = vm.metadata.namespace;
    const seen = new Set(passthrough.disks.map(d => d.name));
    let existingVCT = [];
    try {
      existingVCT = JSON.parse(((vm.metadata || {}).annotations || {})[VCT_ANNOTATION] || '[]');
    } catch { existingVCT = []; }

    const volumes = [...passthrough.volumes];
    const disks = [...passthrough.disks];
    const newVCT = [];
    const keepClaims = new Set();

    items.forEach(item => {
      if (!item.name || !K8S_NAME_RE.test(item.name)) {
        throw new Error(tr('vm.edit.errName', 'disk name must be a valid RFC 1123 label') + `: "${item.name || ''}"`);
      }
      if (seen.has(item.name)) {
        throw new Error(tr('vm.edit.errDup', 'duplicate disk name') + `: ${item.name}`);
      }
      seen.add(item.name);
      const d = { name: item.name };
      if (item.boot_order > 0) d.bootOrder = item.boot_order;
      d[item.device === 'cdrom' ? 'cdrom' : 'disk'] = { bus: item.bus || 'virtio' };
      disks.push(d);

      if (item.source === 'pvc' || !item.source) {
        if (!item.pvc) throw new Error(tr('vm.edit.errPvc', 'select an existing PVC for disk') + ` ${item.name}`);
        const slash = item.pvc.indexOf('/');
        const pvcNs = slash > 0 ? item.pvc.slice(0, slash) : ns;
        const claim = slash > 0 ? item.pvc.slice(slash + 1) : item.pvc;
        if (pvcNs !== ns) {
          throw new Error(tr('vm.edit.errPvcNs', 'the PVC must live in the VM namespace') + ` (${ns}): ${item.pvc}`);
        }
        keepClaims.add(claim);
        volumes.push({ name: item.name, persistentVolumeClaim: { claimName: claim } });
        return;
      }

      // NEW disk → PVC created by Harvester via the volumeClaimTemplates
      // annotation (same mechanism as the Harvester UI).
      if (!item.size) throw new Error(tr('vm.edit.errSize', 'a size is required for a new disk') + ` (${item.name})`);
      const claim = `${vm.metadata.name}-${item.name}-${Math.random().toString(36).slice(2, 7)}`;
      const t = {
        metadata: { name: claim, annotations: {} },
        spec: {
          accessModes: ['ReadWriteMany'],
          resources: { requests: { storage: item.size } },
          volumeMode: 'Block',
        },
      };
      if (item.source === 'image') {
        if (!item.image) throw new Error(tr('vm.edit.errImage', 'select a VM image for disk') + ` ${item.name}`);
        const imgName = item.image.split('/').pop();
        t.metadata.annotations['harvesterhci.io/imageId'] = item.image;
        // Harvester provisions one storage class per image: longhorn-<image>
        t.spec.storageClassName = `longhorn-${imgName}`;
      } else if (item.storage_class) {
        t.spec.storageClassName = item.storage_class;
      }
      newVCT.push(t);
      keepClaims.add(claim);
      volumes.push({ name: item.name, persistentVolumeClaim: { claimName: claim } });
    });

    // Keep annotation entries whose PVC is still referenced; drop the ones
    // belonging to removed disks; append the new templates.
    const finalVCT = existingVCT
      .filter(t => t && t.metadata && keepClaims.has(t.metadata.name))
      .concat(newVCT);

    return {
      metadata: { annotations: { [VCT_ANNOTATION]: JSON.stringify(finalVCT) } },
      spec: { template: { spec: {
        volumes,
        domain: { devices: { disks } },
      } } },
    };
  }

  function vmNetsToForm(vm) {
    const template = ((vm.spec || {}).template || {}).spec || {};
    const interfaces = ((template.domain || {}).devices || {}).interfaces || [];
    const networks = template.networks || [];
    const netByName = Object.fromEntries(networks.map(n => [n.name, n]));
    const used = new Set();
    const items = [];
    const passthrough = { interfaces: [], networks: [] };

    interfaces.forEach(itf => {
      const net = netByName[itf.name];
      if (net) used.add(itf.name);
      const type = itf.bridge ? 'bridge' : (itf.masquerade ? 'masquerade' : null);
      if (!type || !net) {           // sriov / slirp / broken pair → untouched
        passthrough.interfaces.push(itf);
        if (net) passthrough.networks.push(net);
        return;
      }
      items.push({
        name: itf.name,
        type,
        network: net.multus ? net.multus.networkName : '',
        model: itf.model || 'virtio',
        mac: itf.macAddress || '',
      });
    });
    networks.forEach(n => { if (!used.has(n.name)) passthrough.networks.push(n); });
    return { items, passthrough };
  }

  function formNetsToPatch(items, passthrough, vm) {
    const seen = new Set(passthrough.interfaces.map(i => i.name));
    const interfaces = [...passthrough.interfaces];
    const networks = [...passthrough.networks];

    items.forEach(item => {
      if (!item.name || !K8S_NAME_RE.test(item.name)) {
        throw new Error(tr('vm.edit.errNicName', 'interface name must be a valid RFC 1123 label') + `: "${item.name || ''}"`);
      }
      if (seen.has(item.name)) {
        throw new Error(tr('vm.edit.errNicDup', 'duplicate interface name') + `: ${item.name}`);
      }
      seen.add(item.name);
      if (item.mac && !MAC_RE.test(item.mac)) {
        throw new Error(tr('vm.edit.errMac', 'invalid MAC address') + `: ${item.mac}`);
      }
      const itf = { name: item.name };
      if (item.model) itf.model = item.model;
      if (item.mac) itf.macAddress = item.mac;
      const net = { name: item.name };
      if (item.type === 'masquerade') {
        itf.masquerade = {};
        net.pod = {};
      } else {
        if (!item.network) {
          throw new Error(tr('vm.edit.errNet', 'a multus network is required for a bridge interface') + ` (${item.name})`);
        }
        itf.bridge = {};
        net.multus = { networkName: item.network };
      }
      interfaces.push(itf);
      networks.push(net);
    });

    return { spec: { template: { spec: {
      networks,
      domain: { devices: { interfaces } },
    } } } };
  }

  // =========================================================================
  // Panel lifecycle
  // =========================================================================
  async function open(cluster, namespace, name) {
    const panelId = `vm-edit-${cluster}-${namespace}-${name}`;
    const existing = document.getElementById('fp-' + panelId);
    const title = `⚙ ${tr('vm.edit.title', 'Edit')} — ${namespace}/${name}`;
    if (existing) {
      // Already open: FloatingPanels restores/focuses it. Re-running the
      // setup below would stack duplicate nav listeners (v1.6.x bug).
      return FloatingPanels.open({ id: panelId, title });
    }
    const html = `
      <div class="vm-edit-layout">
        <aside class="vm-edit-nav">
          ${SECTIONS.map(s => `
            <button data-section="${s.id}">
              <span class="ic">${s.icon}</span>
              <span>${esc(s.label())}</span>
            </button>`).join('')}
        </aside>
        <main class="vm-edit-content">
          <div class="vm-edit-loading">${esc(tr('vm.edit.loading', 'Loading VM spec…'))}</div>
        </main>
      </div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml: html,
      width: 980,
      height: 640,
      restoreSpec: { type: 'vm-edit', args: { cluster, namespace, name } },
      onClose: () => refreshers.delete(panelId),
    });

    let activeSection = 'general';
    let vmSpec = null;
    const navBtns = panel.el.querySelectorAll('.vm-edit-nav button');
    const setActive = (id) => {
      activeSection = id;
      navBtns.forEach(b => b.classList.toggle('active', b.dataset.section === id));
      renderSection();
    };
    navBtns.forEach(b => b.addEventListener('click', () => setActive(b.dataset.section)));
    navBtns[0].classList.add('active');

    const renderSection = () => {
      const content = panel.el.querySelector('.vm-edit-content');
      if (!vmSpec) { content.innerHTML = '<div class="vm-edit-loading">Loading…</div>'; return; }
      content.innerHTML = '';
      const sectionEl = document.createElement('section');
      sectionEl.className = 'vm-edit-section active';
      sectionEl.dataset.section = activeSection;
      sectionEl.innerHTML = renderSectionHtml(activeSection, vmSpec, cluster);
      content.appendChild(sectionEl);
      wireSection(sectionEl, activeSection, cluster, namespace, name, () => vmSpec);
    };

    const refresh = async () => {
      try {
        vmSpec = await fetch(`/api/vm/${enc(cluster)}/${enc(namespace)}/${enc(name)}`).then(r => {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        });
        renderSection();
      } catch (e) {
        panel.el.querySelector('.vm-edit-content').innerHTML =
          `<div class="vm-edit-section active"><div class="summary-bar bad">✗ ${esc(e.message)}</div></div>`;
      }
    };
    refreshers.set(panelId, refresh);
    await refresh();
    return panel;
  }

  // =========================================================================
  // Section renderers
  // =========================================================================
  function renderSectionHtml(id, vm, cluster) {
    const spec     = vm.spec || {};
    const template = (spec.template || {}).spec || {};
    const domain   = template.domain || {};
    const annot    = (vm.metadata?.annotations) || {};

    switch (id) {
      case 'general': return renderGeneral(vm, spec, annot);
      case 'compute': return renderCompute(domain);
      case 'lifecycle': return renderLifecycle(template);
      case 'disks':   return renderDisksSection(vm, cluster);
      case 'network': return renderNetworkSection(vm, cluster);
      case 'cloudinit': return renderCloudInit();
      default: return '';
    }
  }

  function restartBanner() {
    return `<p class="form-hint vm-edit-banner">⚠ ${esc(tr('vm.edit.restartHint',
      'Changes apply at the next VM restart (the running instance keeps the old layout — use the console reset button).'))}</p>`;
  }

  function renderDisksSection(vm, cluster) {
    const { items, passthrough } = vmDisksToForm(vm);
    const template = ((vm.spec || {}).template || {}).spec || {};
    const raw = { volumes: template.volumes || [],
                  disks: ((template.domain || {}).devices || {}).disks || [] };
    const locked = passthrough.volumes
      .filter(v => v.cloudInitNoCloud || v.cloudInitConfigDrive)
      .map(v => `<div class="vm-edit-locked">☁️ <code>${esc(v.name)}</code> — ${esc(tr('vm.edit.cloudinitVol', 'cloud-init volume, managed by the Cloud-init tab'))}</div>`)
      .join('');
    return `
      <h3>${esc(tr('vm.edit.disksTitle', 'Disks & Volumes'))}</h3>
      ${restartBanner()}
      <div class="vm-edit-cards" data-editor="disks">
        ${TFForm.render(DISK_SCHEMA, cluster, { disk: items }, { hideHeader: true })}
      </div>
      ${locked}
      <details class="vm-edit-adv">
        <summary>${esc(tr('vm.edit.advanced', 'Advanced (raw JSON)'))}</summary>
        <textarea class="yaml-editor" data-yaml="disks" spellcheck="false">${esc(toYaml(raw))}</textarea>
      </details>
      ${applyBar('disks')}`;
  }

  function renderNetworkSection(vm, cluster) {
    const { items } = vmNetsToForm(vm);
    const template = ((vm.spec || {}).template || {}).spec || {};
    const raw = { networks: template.networks || [],
                  interfaces: ((template.domain || {}).devices || {}).interfaces || [] };
    return `
      <h3>${esc(tr('vm.edit.netTitle', 'Network interfaces'))}</h3>
      ${restartBanner()}
      <div class="vm-edit-cards" data-editor="network">
        ${TFForm.render(NET_SCHEMA, cluster, { nic: items }, { hideHeader: true })}
      </div>
      <details class="vm-edit-adv">
        <summary>${esc(tr('vm.edit.advanced', 'Advanced (raw JSON)'))}</summary>
        <textarea class="yaml-editor" data-yaml="network" spellcheck="false">${esc(toYaml(raw))}</textarea>
      </details>
      ${applyBar('network')}`;
  }

  function renderGeneral(vm, spec, annot) {
    const description = annot['harvesterhci.io/description'] || annot['description'] || '';
    return `
      <h3>General</h3>
      <div class="form-row">
        <label>Name</label>
        <input type="text" value="${esc(vm.metadata.name)}" readonly>
      </div>
      <div class="form-row">
        <label>Namespace</label>
        <input type="text" value="${esc(vm.metadata.namespace)}" readonly>
      </div>
      <div class="form-row">
        <label>Description (annotation harvesterhci.io/description)</label>
        <textarea data-field="annot.description" rows="2">${esc(description)}</textarea>
      </div>
      <div class="form-row">
        <label>Run strategy</label>
        <select data-field="spec.runStrategy">
          ${['Always','RerunOnFailure','Manual','Halted'].map(v =>
            `<option value="${v}" ${spec.runStrategy === v ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>
      ${applyBar('general')}`;
  }

  function renderCompute(domain) {
    const cpu = domain.cpu || {};
    const mem = domain.memory || {};
    const res = (domain.resources || {}).requests || {};
    return `
      <h3>Compute resources</h3>
      <div class="grid-2">
        <div class="form-row">
          <label>CPU sockets</label>
          <input type="number" min="1" data-field="cpu.sockets" value="${cpu.sockets ?? 1}">
        </div>
        <div class="form-row">
          <label>CPU cores</label>
          <input type="number" min="1" data-field="cpu.cores" value="${cpu.cores ?? 1}">
        </div>
        <div class="form-row">
          <label>Threads per core</label>
          <input type="number" min="1" data-field="cpu.threads" value="${cpu.threads ?? 1}">
        </div>
        <div class="form-row">
          <label>Memory (guest, e.g. 4Gi / 4096Mi)</label>
          <input type="text" data-field="memory.guest" value="${esc(mem.guest || '')}" placeholder="4Gi">
        </div>
      </div>
      <h3>Current resources</h3>
      <pre>${esc(JSON.stringify({ requests: res }, null, 2))}</pre>
      <p class="form-hint">Changing CPU/memory while the VM is running may require a reboot for the guest to see the new values.</p>
      ${applyBar('compute')}`;
  }

  function renderLifecycle(template) {
    return `
      <h3>Lifecycle</h3>
      <div class="form-row">
        <label>terminationGracePeriodSeconds (seconds the guest has to ACPI shut down)</label>
        <input type="number" min="0" data-field="lifecycle.terminationGracePeriodSeconds" value="${template.terminationGracePeriodSeconds ?? 180}">
      </div>
      <div class="form-row">
        <label>evictionStrategy</label>
        <select data-field="lifecycle.evictionStrategy">
          <option value="">(none)</option>
          <option value="LiveMigrate"      ${template.evictionStrategy === 'LiveMigrate' ? 'selected' : ''}>LiveMigrate</option>
          <option value="External"         ${template.evictionStrategy === 'External' ? 'selected' : ''}>External</option>
          <option value="LiveMigrateIfPossible" ${template.evictionStrategy === 'LiveMigrateIfPossible' ? 'selected' : ''}>LiveMigrateIfPossible</option>
        </select>
      </div>
      ${applyBar('lifecycle')}`;
  }

  function renderCloudInit() {
    return `
      <h3>Cloud-init</h3>
      <p class="form-hint">Edit user-data and network-data. Saved to the VM's cloud-init Secret.</p>
      <div class="form-row">
        <label>user-data (YAML / shell script)</label>
        <textarea class="yaml-editor tall" data-ci="userData" spellcheck="false"></textarea>
      </div>
      <div class="form-row">
        <label>network-data (YAML)</label>
        <textarea class="yaml-editor" data-ci="networkData" spellcheck="false"></textarea>
      </div>
      <div class="form-hint" id="ci-source"></div>
      <div class="apply-bar">
        <button class="btn btn-primary btn-sm" data-action="apply-cloudinit">Save cloud-init</button>
        <button class="btn btn-secondary btn-sm" data-action="reload-cloudinit">Reload</button>
        <span class="apply-result" data-section="cloudinit"></span>
      </div>`;
  }

  function applyBar(section) {
    return `
      <div class="apply-bar">
        <label class="apply-dry">
          <input type="checkbox" data-dry-run> ${esc(tr('vm.edit.dryRun', 'Dry-run (validate only)'))}
        </label>
        <button class="btn btn-primary btn-sm" data-action="apply" data-section="${section}">${esc(tr('vm.edit.apply', 'Apply changes'))}</button>
        <button class="btn btn-secondary btn-sm" data-action="reset" data-section="${section}">${esc(tr('vm.edit.reset', 'Reset'))}</button>
        <span class="apply-result" data-section="${section}"></span>
      </div>`;
  }

  // =========================================================================
  // Apply wiring
  // =========================================================================

  /** Show/hide the source-dependent fields of every disk card. */
  function syncDiskSourceFields(rootEl) {
    rootEl.querySelectorAll('.tf-block-item').forEach(item => {
      const src = item.querySelector('[name$=".source"]')?.value || 'pvc';
      const show = { pvc: src === 'pvc', image: src === 'image',
                     size: src !== 'pvc', storage_class: src === 'blank' };
      Object.entries(show).forEach(([field, visible]) => {
        const el = item.querySelector(`[name$=".${field}"]`);
        const wrap = el && el.closest('.tf-field');
        if (wrap) wrap.style.display = visible ? '' : 'none';
      });
    });
  }

  function syncNicTypeFields(rootEl) {
    rootEl.querySelectorAll('.tf-block-item').forEach(item => {
      const type = item.querySelector('[name$=".type"]')?.value || 'bridge';
      const el = item.querySelector('[name$=".network"]');
      const wrap = el && el.closest('.tf-field');
      if (wrap) wrap.style.display = type === 'bridge' ? '' : 'none';
    });
  }

  function wireSection(sectionEl, sectionId, cluster, namespace, name, getVM) {
    if (sectionId === 'cloudinit') {
      loadCloudInit(sectionEl, cluster, namespace, name);
      sectionEl.querySelector('[data-action="apply-cloudinit"]').addEventListener('click', () =>
        applyCloudInit(sectionEl, cluster, namespace, name));
      sectionEl.querySelector('[data-action="reload-cloudinit"]').addEventListener('click', () =>
        loadCloudInit(sectionEl, cluster, namespace, name));
      return;
    }

    if (sectionId === 'disks' || sectionId === 'network') {
      const editor = sectionEl.querySelector('.vm-edit-cards .tf-form');
      const schema = sectionId === 'disks' ? DISK_SCHEMA : NET_SCHEMA;
      TFForm.wire(editor, schema, cluster);
      const sync = sectionId === 'disks' ? syncDiskSourceFields : syncNicTypeFields;
      sync(editor);
      editor.addEventListener('change', () => sync(editor));
      // New cards appear via +Add after this wire() — keep them in sync too.
      new MutationObserver(() => sync(editor)).observe(
        editor.querySelector('.tf-block-list'), { childList: true });
    }

    const applyBtn = sectionEl.querySelector('[data-action="apply"]');
    if (applyBtn) {
      applyBtn.addEventListener('click', async () => {
        const dryRun = !!sectionEl.querySelector('[data-dry-run]')?.checked;
        const result = sectionEl.querySelector('.apply-result');
        result.textContent = 'applying…';
        try {
          const patch = buildPatch(sectionEl, sectionId, getVM());
          const res = await fetch(`/api/vm/${enc(cluster)}/${enc(namespace)}/${enc(name)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ patch, dry_run: dryRun }),
          });
          const d = await res.json();
          if (!res.ok) {
            result.innerHTML = `<span style="color:var(--danger)">✗ ${esc((d.detail || d.error || 'failed').slice(0, 300))}</span>`;
            return;
          }
          result.innerHTML = dryRun
            ? `<span style="color:var(--accent)">✓ ${esc(tr('vm.edit.dryOk', 'dry-run OK'))}</span>`
            : `<span style="color:var(--accent)">✓ ${esc(tr('vm.edit.applied', 'applied'))}</span>`;
          if (!dryRun) {
            const refresh = refreshers.get(`vm-edit-${cluster}-${namespace}-${name}`);
            if (refresh) setTimeout(refresh, 600);
          }
        } catch (e) {
          result.innerHTML = `<span style="color:var(--danger)">✗ ${esc(e.message)}</span>`;
        }
      });
    }
    const resetBtn = sectionEl.querySelector('[data-action="reset"]');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        const refresh = refreshers.get(`vm-edit-${cluster}-${namespace}-${name}`);
        if (refresh) refresh();
      });
    }
  }

  function buildPatch(sectionEl, sectionId, vm) {
    const get = (selector) => sectionEl.querySelector(selector);
    const val = (field) => {
      const el = get(`[data-field="${field}"]`);
      return el ? (el.type === 'number' ? Number(el.value) : el.value) : undefined;
    };
    switch (sectionId) {
      case 'general':
        return {
          metadata: {
            annotations: { 'harvesterhci.io/description': val('annot.description') || '' },
          },
          spec: { runStrategy: val('spec.runStrategy') },
        };
      case 'compute':
        return {
          spec: { template: { spec: { domain: {
            cpu: {
              sockets: val('cpu.sockets'),
              cores:   val('cpu.cores'),
              threads: val('cpu.threads'),
            },
            memory: { guest: val('memory.guest') },
          } } } },
        };
      case 'lifecycle':
        return {
          spec: { template: { spec: {
            terminationGracePeriodSeconds: val('lifecycle.terminationGracePeriodSeconds'),
            evictionStrategy: val('lifecycle.evictionStrategy') || null,
          } } },
        };
      case 'disks': {
        const adv = get('.vm-edit-adv');
        if (adv && adv.open) {
          const obj = fromYaml(get('[data-yaml="disks"]').value);
          return { spec: { template: { spec: {
            volumes: obj.volumes || [],
            domain: { devices: { disks: obj.disks || [] } },
          } } } };
        }
        const editor = get('.vm-edit-cards .tf-form');
        const read = TFForm.read(editor, DISK_SCHEMA, { emitEmptyLists: true });
        const { passthrough } = vmDisksToForm(vm);
        return formDisksToPatch(read.disk || [], passthrough, vm);
      }
      case 'network': {
        const adv = get('.vm-edit-adv');
        if (adv && adv.open) {
          const obj = fromYaml(get('[data-yaml="network"]').value);
          return { spec: { template: { spec: {
            networks: obj.networks || [],
            domain: { devices: { interfaces: obj.interfaces || [] } },
          } } } };
        }
        const editor = get('.vm-edit-cards .tf-form');
        const read = TFForm.read(editor, NET_SCHEMA, { emitEmptyLists: true });
        const { passthrough } = vmNetsToForm(vm);
        return formNetsToPatch(read.nic || [], passthrough, vm);
      }
      default:
        return {};
    }
  }

  async function loadCloudInit(sectionEl, cluster, namespace, name) {
    const out = sectionEl.querySelector('#ci-source');
    out.textContent = 'loading…';
    try {
      const d = await fetch(`/api/vm/${enc(cluster)}/${enc(namespace)}/${enc(name)}/cloudinit`).then(r => r.json());
      sectionEl.querySelector('[data-ci="userData"]').value    = d.userData || '';
      sectionEl.querySelector('[data-ci="networkData"]').value = d.networkData || '';
      out.innerHTML = d.source === 'secret'
        ? `source: Secret <code>${esc(d.secretName)}</code>`
        : d.source === 'inline'
          ? `<span style="color:var(--warn)">inline cloud-init (read-only)</span>`
          : '<span style="color:var(--text-dim)">no cloud-init configured</span>';
    } catch (e) {
      out.innerHTML = `<span style="color:var(--danger)">✗ ${esc(e.message)}</span>`;
    }
  }

  async function applyCloudInit(sectionEl, cluster, namespace, name) {
    const result = sectionEl.querySelector('.apply-result');
    result.textContent = 'saving…';
    const body = {
      userData:    sectionEl.querySelector('[data-ci="userData"]').value,
      networkData: sectionEl.querySelector('[data-ci="networkData"]').value,
    };
    try {
      const res = await fetch(`/api/vm/${enc(cluster)}/${enc(namespace)}/${enc(name)}/cloudinit`,
        { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await res.json();
      if (!res.ok) {
        result.innerHTML = `<span style="color:var(--danger)">✗ ${esc(d.error || res.status)}</span>`;
      } else {
        result.innerHTML = `<span style="color:var(--accent)">✓ saved to Secret ${esc(d.secret)}</span>`;
      }
    } catch (e) {
      result.innerHTML = `<span style="color:var(--danger)">✗ ${esc(e.message)}</span>`;
    }
  }

  // The "YAML" editors are actually pretty-printed JSON (we don't bundle
  // js-yaml); the advanced fold keeps that historical contract.
  function toYaml(obj) {
    return JSON.stringify(obj, null, 2);
  }
  function fromYaml(text) {
    return JSON.parse(text);
  }
  function enc(s) { return encodeURIComponent(s); }
  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  return {
    open,
    // exported for tests (pure functions, no DOM)
    _mappers: { vmDisksToForm, formDisksToPatch, vmNetsToForm, formNetsToPatch },
    _schemas: { DISK_SCHEMA, NET_SCHEMA },
  };
})();

if (typeof window !== 'undefined') window.VMEdit = VMEdit;
if (typeof FloatingPanels !== 'undefined') {
  FloatingPanels.registerType('vm-edit', (args) =>
    VMEdit.open(args.cluster, args.namespace, args.name));
}
