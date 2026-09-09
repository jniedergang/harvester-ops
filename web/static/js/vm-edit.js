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
    { id: 'firmware',  label: () => tr('vm.edit.firmware', 'Firmware'),    icon: '🔧' },
    { id: 'disks',     label: () => tr('vm.edit.disks', 'Disks'),          icon: '💾' },
    { id: 'network',   label: () => tr('vm.edit.network', 'Network'),      icon: '🔌' },
    { id: 'cloudinit', label: () => tr('vm.edit.cloudinit', 'Cloud-init'), icon: '☁️' },
    { id: 'placement', label: () => tr('vm.edit.placement', 'Placement'),  icon: '📍' },
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

  // v1.8.3 : premier nom libre pour une nouvelle carte de liste
  // (eth0 pris -> eth1…) — sans quoi +Add dupliquait le nom d'un
  // voisin en rejouant le default du schéma.
  function nextFree(prefix, start, used) {
    const have = new Set((used || []).filter(Boolean));
    for (let n = start; n < start + 99; n++) {
      if (!have.has(prefix + n)) return prefix + n;
    }
    return prefix + start;
  }
  function nextFreeDev(used) {
    const have = new Set((used || []).filter(Boolean));
    for (const c of 'bcdefghijklmnopqrstuvwxyz') {
      if (!have.has('/dev/vd' + c)) return '/dev/vd' + c;
    }
    return '/dev/vdb';
  }

  const DISK_SCHEMA = {
    id: 'vm-disks',
    nested: {
      disk: {
        min: 0, max: 16,
        label: { en: 'Disks', fr: 'Disques' },
        itemTitle: (v) => `💾 ${v.name || tr('vm.edit.newDisk', 'new disk')}`
          + `${v.bus ? ' — ' + v.bus : ''}${v.size ? ' · ' + v.size : ''}`
          + `${v.boot_order > 0 ? ' · boot #' + v.boot_order : ''}`,
        newItem: (items) => ({ name: nextFree('disk-', 1, items.map(i => i.name)) }),
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
            suggest: ['10Gi', '20Gi', '40Gi', '80Gi', '100Gi', '200Gi'],
            label: { en: 'Size (new disk)', fr: 'Taille (nouveau disque)' },
            description: { en: 'e.g. 10Gi — for image sources, at least the image size',
                           fr: 'ex. 10Gi — pour une image, au moins la taille de l’image' } },
          { name: 'storage_class', type: 'ref', ref_endpoint: '/api/storageclasses',
            label: { en: 'Storage class', fr: 'Storage class' },
            description: { en: 'Blank disks only (image disks inherit theirs); empty = cluster default',
                           fr: 'Disques vierges seulement (hérité pour les images) ; vide = défaut du cluster' } },
          { name: 'serial', type: 'text', validate: /^[A-Za-z0-9_.+-]{0,36}$/,
            label: { en: 'Serial', fr: 'Numéro de série' },
            description: { en: 'Shown to the guest (/dev/disk/by-id) — handy to identify a disk from inside the VM',
                           fr: 'Vu par l’invité (/dev/disk/by-id) — pratique pour identifier un disque depuis la VM' } },
          { name: 'cache', type: 'enum', enum_values: ['none', 'writethrough', 'writeback'],
            label: { en: 'Cache mode', fr: 'Mode de cache' },
            description: { en: 'Empty = hypervisor default. none is the safest for shared storage',
                           fr: 'Vide = défaut de l’hyperviseur. none est le plus sûr sur stockage partagé' } },
          { name: 'shareable', type: 'bool', default: false,
            label: { en: 'Shareable', fr: 'Partageable' },
            description: { en: 'Allows several VMs to attach this volume (clustering) — the guests must coordinate writes',
                           fr: 'Permet à plusieurs VMs d’attacher ce volume (clustering) — aux invités de coordonner les écritures' } },
          { name: 'readonly', type: 'bool', default: false,
            label: { en: 'Read-only', fr: 'Lecture seule' },
            description: { en: 'The guest cannot write to it (typical for an ISO)',
                           fr: 'L’invité ne peut pas y écrire (typique d’une ISO)' } },
          { name: 'io_thread', type: 'bool', default: false,
            label: { en: 'Dedicated I/O thread', fr: 'Thread d’E/S dédié' },
            description: { en: 'A thread of its own for this disk — helps a latency-sensitive workload',
                           fr: 'Un thread rien que pour ce disque — utile pour une charge sensible à la latence' } },
        ],
      },
    },
  };

  // v1.12.0 — tags Harvester (labels tag.harvesterhci.io/<clé>) éditables
  // comme dans l'UI Harvester : ils servent au filtrage et aux inventaires.
  const TAG_SCHEMA = {
    id: 'vm-tags',
    nested: {
      tag: {
        min: 0, max: 20,
        label: { en: 'Tags', fr: 'Tags' },
        itemTitle: (v) => `🏷 ${v.key || tr('vm.edit.newTag', 'new tag')}${v.value ? ' = ' + v.value : ''}`,
        newItem: (items) => ({ key: nextFree('tag-', 1, items.map(i => i.key)) }),
        args: [
          { name: 'key', type: 'text', required: true, validate: /^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$/,
            suggest: ['app', 'purpose', 'case', 'ssh-user', 'owner', 'env'],
            label: { en: 'Key', fr: 'Clé' },
            description: { en: 'Stored as tag.harvesterhci.io/<key>',
                           fr: 'Stocké en tag.harvesterhci.io/<clé>' } },
          { name: 'value', type: 'text',
            label: { en: 'Value', fr: 'Valeur' },
            description: { en: 'Free text (Kubernetes label value rules)',
                           fr: 'Texte libre (règles de valeur de label Kubernetes)' } },
        ],
      },
    },
  };

  // v1.13.0 — placement. On n'écrit QUE nodeSelector et les règles pod :
  // la nodeAffinity est gérée par Harvester (contrainte réseau) et serait
  // écrasée si on la republiait — le merge patch préserve les clés sœurs.
  const NODESEL_SCHEMA = {
    id: 'vm-nodeselector',
    nested: {
      sel: {
        min: 0, max: 10,
        label: { en: 'Node selector', fr: 'Sélecteur de node' },
        itemTitle: (v) => `📍 ${v.key || tr('vm.edit.newSel', 'new constraint')}${v.value ? ' = ' + v.value : ''}`,
        newItem: () => ({ key: 'kubernetes.io/hostname' }),
        args: [
          { name: 'key', type: 'text', required: true,
            suggest: ['kubernetes.io/hostname', 'topology.kubernetes.io/zone',
                      'node-role.kubernetes.io/control-plane'],
            label: { en: 'Node label', fr: 'Label de node' },
            description: { en: 'The VM only schedules on nodes carrying this label',
                           fr: 'La VM ne se place que sur les nodes portant ce label' } },
          { name: 'value', type: 'text',
            label: { en: 'Value', fr: 'Valeur' },
            description: { en: 'e.g. the node hostname', fr: 'ex. le nom d’hôte du node' } },
        ],
      },
    },
  };

  const AFFINITY_SCHEMA = {
    id: 'vm-affinity',
    nested: {
      rule: {
        min: 0, max: 10,
        label: { en: 'VM placement rules', fr: 'Règles de placement VM' },
        itemTitle: (v) => `${v.kind === 'attract' ? '🧲' : '🚧'} `
          + `${v.kind === 'attract' ? tr('vm.edit.affNear', 'near') : tr('vm.edit.affAway', 'away from')} `
          + `${v.key || 'tag'}=${v.value || '?'}${v.hard ? ' (strict)' : ''}`,
        newItem: () => ({ kind: 'avoid', key: 'app' }),
        args: [
          { name: 'kind', type: 'enum', default: 'avoid', enum_values: ['avoid', 'attract'],
            label: { en: 'Rule', fr: 'Règle' },
            description: { en: 'avoid = do not co-locate (HA pairs) · attract = co-locate (latency)',
                           fr: 'avoid = ne pas co-localiser (paires HA) · attract = co-localiser (latence)' } },
          { name: 'key', type: 'text', required: true,
            suggest: ['app', 'purpose', 'case'],
            label: { en: 'Tag key', fr: 'Clé du tag' },
            description: { en: 'Matches other VMs carrying tag.harvesterhci.io/<key>',
                           fr: 'Vise les autres VMs portant tag.harvesterhci.io/<clé>' } },
          { name: 'value', type: 'text', required: true,
            label: { en: 'Tag value', fr: 'Valeur du tag' },
            description: { en: 'The value those VMs carry', fr: 'La valeur que portent ces VMs' } },
          { name: 'hard', type: 'bool', default: false,
            label: { en: 'Strict', fr: 'Stricte' },
            description: { en: 'Strict = the VM stays unschedulable if the rule cannot be met; otherwise it is only a preference',
                           fr: 'Stricte = la VM reste non planifiable si la règle ne peut pas être respectée ; sinon simple préférence' } },
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
          + `${v.type ? ' — ' + v.type : ''}${v.network ? ' · ' + v.network : ''}`
          + `${v.boot_order > 0 ? ' · boot #' + v.boot_order : ''}`,
        newItem: (items) => ({ name: nextFree('nic-', 1, items.map(i => i.name)) }),
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
          { name: 'boot_order', type: 'int', default: 0, min: 0, max: 64,
            label: { en: 'Boot order', fr: 'Ordre de boot' },
            description: { en: '0 = not bootable. Set 1 to PXE-boot from this NIC (shares the numbering with the disks)',
                           fr: '0 = pas de boot. Mettre 1 pour démarrer en PXE sur cette carte (numérotation partagée avec les disques)' } },
          { name: 'mac', type: 'text', validate: MAC_RE,
            label: { en: 'MAC address', fr: 'Adresse MAC' },
            description: { en: 'Empty = auto-generated. Changing it may break DHCP leases',
                           fr: 'Vide = auto-générée. La changer peut casser les baux DHCP' } },
        ],
      },
    },
  };

  // =========================================================================
  // Cloud-init assistant (v1.8.1) — a one-way GENERATOR: the forms below
  // produce cloud-config / network-data v1 YAML into the expert textareas
  // (which stay the saved truth). We deliberately do not parse existing
  // YAML back into the form — that would need a full YAML parser and
  // would lie about arbitrary user content.
  // =========================================================================
  const CI_USER_SCHEMA = {
    id: 'ci-userdata',
    sections: [
      { id: 'identity', label: { en: 'Identity & access', fr: 'Identité & accès' },
        args: ['hostname', 'fqdn', 'timezone', 'locale', 'keyboard',
               'ssh_pwauth', 'disable_root', 'expire_passwords'] },
      { id: 'users', label: { en: 'Users', fr: 'Utilisateurs' }, nested: 'user' },
      { id: 'packages', label: { en: 'Packages', fr: 'Paquets' },
        args: ['package_update', 'package_upgrade', 'package_reboot', 'packages'] },
      { id: 'storage', label: { en: 'Storage', fr: 'Stockage' },
        args: ['growpart'] },
      { id: 'fs', label: { en: 'Extra disks', fr: 'Disques additionnels' }, nested: 'fs' },
      { id: 'files', label: { en: 'Files', fr: 'Fichiers' }, nested: 'file' },
      { id: 'misc', label: { en: 'System', fr: 'Système' },
        args: ['ntp_servers', 'ca_certs', 'bootcmd', 'runcmd'] },
    ],
    args: [
      { name: 'hostname', type: 'text',
        label: { en: 'Hostname', fr: 'Nom d’hôte' },
        description: { en: 'Sets the guest hostname at first boot',
                       fr: 'Définit le nom d’hôte de l’invité au premier boot' } },
      { name: 'fqdn', type: 'text',
        label: { en: 'FQDN', fr: 'FQDN' },
        description: { en: 'e.g. vm1.home.lo (also sets manage_etc_hosts)',
                       fr: 'ex. vm1.home.lo (active aussi manage_etc_hosts)' } },
      { name: 'timezone', type: 'text',
        suggest: ['Europe/Paris', 'Europe/Berlin', 'Europe/London', 'UTC', 'America/New_York', 'Asia/Tokyo'],
        label: { en: 'Timezone', fr: 'Fuseau horaire' },
        description: { en: 'e.g. Europe/Paris', fr: 'ex. Europe/Paris' } },
      { name: 'locale', type: 'text',
        suggest: ['fr_FR.UTF-8', 'en_US.UTF-8', 'de_DE.UTF-8', 'C.UTF-8'],
        label: { en: 'Locale', fr: 'Locale' },
        description: { en: 'e.g. fr_FR.UTF-8', fr: 'ex. fr_FR.UTF-8' } },
      { name: 'keyboard', type: 'text',
        suggest: ['fr', 'us', 'de', 'gb', 'es', 'it'],
        label: { en: 'Keyboard layout', fr: 'Disposition clavier' },
        description: { en: 'e.g. fr, us, de', fr: 'ex. fr, us, de' } },
      { name: 'ssh_pwauth', type: 'bool', default: false,
        label: { en: 'Allow SSH password auth', fr: 'Autoriser SSH par mot de passe' },
        description: { en: 'Required for password logins over SSH',
                       fr: 'Requis pour se connecter en SSH par mot de passe' } },
      { name: 'disable_root', type: 'bool', default: true,
        label: { en: 'Disable root login', fr: 'Désactiver le login root' },
        description: { en: 'cloud-init default is true', fr: 'true par défaut côté cloud-init' } },
      { name: 'expire_passwords', type: 'bool', default: false,
        label: { en: 'Expire passwords at first login', fr: 'Expirer les mots de passe au premier login' },
        description: { en: 'Forces every user to change their password',
                       fr: 'Force chaque utilisateur à changer son mot de passe' } },
      { name: 'package_update', type: 'bool', default: false,
        label: { en: 'Refresh package index', fr: 'Rafraîchir l’index des paquets' },
        description: { en: 'apt/zypper/dnf refresh at first boot',
                       fr: 'refresh apt/zypper/dnf au premier boot' } },
      { name: 'package_upgrade', type: 'bool', default: false,
        label: { en: 'Upgrade packages', fr: 'Mettre à jour les paquets' },
        description: { en: 'Full package upgrade at first boot (slower)',
                       fr: 'Mise à jour complète au premier boot (plus lent)' } },
      { name: 'package_reboot', type: 'bool', default: false,
        label: { en: 'Reboot if required', fr: 'Redémarrer si nécessaire' },
        description: { en: 'package_reboot_if_required after upgrades',
                       fr: 'package_reboot_if_required après mise à jour' } },
      { name: 'packages', type: 'textarea', rows: 3,
        label: { en: 'Packages (one per line)', fr: 'Paquets (un par ligne)' },
        description: { en: 'Installed at first boot', fr: 'Installés au premier boot' } },
      { name: 'growpart', type: 'bool', default: true,
        label: { en: 'Grow root partition', fr: 'Étendre la partition racine' },
        description: { en: 'Expand / to fill the (resized) root disk',
                       fr: 'Étend / pour occuper le disque racine (redimensionné)' } },
      { name: 'ntp_servers', type: 'text',
        suggest: ['pool.ntp.org', '0.pool.ntp.org, 1.pool.ntp.org', 'time.cloudflare.com'],
        label: { en: 'NTP servers (comma-separated)', fr: 'Serveurs NTP (séparés par des virgules)' },
        description: { en: 'Enables the ntp module', fr: 'Active le module ntp' } },
      { name: 'ca_certs', type: 'textarea', rows: 3,
        label: { en: 'Trusted CA certificates (PEM)', fr: 'Certificats CA de confiance (PEM)' },
        description: { en: 'Added to the guest trust store (e.g. your internal CA)',
                       fr: 'Ajoutés au magasin de confiance de l’invité (ex. votre CA interne)' } },
      { name: 'bootcmd', type: 'textarea', rows: 2,
        label: { en: 'Early boot commands (one per line)', fr: 'Commandes de début de boot (une par ligne)' },
        description: { en: 'Run very early, on every boot',
                       fr: 'Exécutées très tôt, à chaque boot' } },
      { name: 'runcmd', type: 'textarea', rows: 3,
        label: { en: 'Commands (one per line)', fr: 'Commandes (une par ligne)' },
        description: { en: 'Shell commands run once at the end of first boot',
                       fr: 'Commandes shell exécutées une fois en fin de premier boot' } },
    ],
    nested: {
      user: {
        min: 0, max: 8,
        label: { en: 'Users', fr: 'Utilisateurs' },
        itemTitle: (v) => `👤 ${v.name || tr('vm.edit.ci.newUser', 'new user')}${v.sudo ? ' · sudo' : ''}`,
        args: [
          { name: 'name', type: 'text', required: true, validate: K8S_NAME_RE,
            label: { en: 'Username', fr: 'Nom d’utilisateur' },
            description: { en: 'The account to create', fr: 'Le compte à créer' } },
          { name: 'password', type: 'text',
            label: { en: 'Password', fr: 'Mot de passe' },
            description: { en: 'Stored as plain text in the cloud-init Secret — prefer SSH keys',
                           fr: 'Stocké en clair dans le Secret cloud-init — préférez les clés SSH' } },
          { name: 'sudo', type: 'bool', default: true,
            label: { en: 'Passwordless sudo', fr: 'sudo sans mot de passe' },
            description: { en: 'ALL=(ALL) NOPASSWD:ALL', fr: 'ALL=(ALL) NOPASSWD:ALL' } },
          { name: 'groups', type: 'text',
            suggest: ['wheel', 'sudo', 'docker', 'wheel, docker'],
            label: { en: 'Groups (comma-separated)', fr: 'Groupes (séparés par des virgules)' },
            description: { en: 'e.g. wheel, docker', fr: 'ex. wheel, docker' } },
          { name: 'shell', type: 'text', default: '/bin/bash',
            suggest: ['/bin/bash', '/bin/sh', '/usr/bin/zsh', '/usr/bin/fish'],
            label: { en: 'Shell', fr: 'Shell' },
            description: { en: 'Login shell', fr: 'Shell de connexion' } },
          { name: 'ssh_key', type: 'ref', ref_endpoint: '/api/sshkeys',
            ref_value_field: 'public_key', ref_label_field: 'name',
            label: { en: 'SSH key (Harvester)', fr: 'Clé SSH (Harvester)' },
            description: { en: 'A KeyPair stored in Harvester', fr: 'Un KeyPair stocké dans Harvester' } },
          { name: 'ssh_key_extra', type: 'textarea', rows: 2,
            label: { en: 'Extra public keys (one per line)', fr: 'Clés publiques en plus (une par ligne)' },
            description: { en: 'Raw ssh-ed25519/ssh-rsa lines', fr: 'Lignes ssh-ed25519/ssh-rsa brutes' } },
        ],
      },
      fs: {
        min: 0, max: 8,
        label: { en: 'Extra disks (format & mount)', fr: 'Disques additionnels (formater & monter)' },
        itemTitle: (v) => `🗄 ${v.device || '/dev/vdb'} → ${v.mount_point || '?'}${v.filesystem ? ' (' + v.filesystem + ')' : ''}`,
        newItem: (items) => ({ device: nextFreeDev(items.map(i => i.device)) }),
        args: [
          { name: 'device', type: 'text', required: true, default: '/dev/vdb',
            suggest: ['/dev/vdb', '/dev/vdc', '/dev/vdd', '/dev/sdb', '/dev/sdc'],
            label: { en: 'Device', fr: 'Périphérique' },
            description: { en: 'First extra virtio disk is /dev/vdb, then /dev/vdc…',
                           fr: 'Premier disque virtio additionnel : /dev/vdb, puis /dev/vdc…' } },
          { name: 'filesystem', type: 'enum', default: 'ext4', enum_values: ['ext4', 'xfs', 'btrfs'],
            label: { en: 'Filesystem', fr: 'Système de fichiers' },
            description: { en: 'Created at first boot (existing data untouched: overwrite=false)',
                           fr: 'Créé au premier boot (données existantes préservées : overwrite=false)' } },
          { name: 'mount_point', type: 'text', required: true,
            suggest: ['/data', '/srv', '/var/lib/data', '/mnt/data'],
            label: { en: 'Mount point', fr: 'Point de montage' },
            description: { en: 'e.g. /data', fr: 'ex. /data' } },
        ],
      },
      file: {
        min: 0, max: 8,
        label: { en: 'Files (write_files)', fr: 'Fichiers (write_files)' },
        itemTitle: (v) => `📄 ${v.path || tr('vm.edit.ci.newFile', 'new file')}`,
        args: [
          { name: 'path', type: 'text', required: true,
            label: { en: 'Path', fr: 'Chemin' },
            description: { en: 'Absolute path in the guest', fr: 'Chemin absolu dans l’invité' } },
          { name: 'permissions', type: 'text', default: '0644', validate: /^0[0-7]{3}$/,
            suggest: ['0644', '0755', '0600', '0400', '0700'],
            label: { en: 'Permissions', fr: 'Permissions' },
            description: { en: 'Octal, e.g. 0644 / 0755', fr: 'Octal, ex. 0644 / 0755' } },
          { name: 'content', type: 'textarea', rows: 4,
            label: { en: 'Content', fr: 'Contenu' },
            description: { en: 'Written verbatim', fr: 'Écrit tel quel' } },
        ],
      },
    },
  };

  const CI_NET_SCHEMA = {
    id: 'ci-netdata',
    args: [
      { name: 'dns', type: 'text',
        label: { en: 'DNS servers (comma-separated)', fr: 'Serveurs DNS (séparés par des virgules)' },
        description: { en: 'Global resolvers (type: nameserver)', fr: 'Résolveurs globaux (type: nameserver)' } },
      { name: 'search', type: 'text',
        label: { en: 'Search domains (comma-separated)', fr: 'Domaines de recherche (séparés par des virgules)' },
        description: { en: 'e.g. home.lo', fr: 'ex. home.lo' } },
    ],
    nested: {
      nic: {
        min: 0, max: 4,
        label: { en: 'Interfaces', fr: 'Interfaces' },
        itemTitle: (v) => `🔌 ${v.iface || 'eth0'} — ${v.mode || 'dhcp'}${v.address ? ' · ' + v.address : ''}`,
        newItem: (items) => ({ iface: nextFree('eth', 0, items.map(i => i.iface)) }),
        args: [
          { name: 'iface', type: 'text', required: true, default: 'eth0',
            suggest: ['eth0', 'eth1', 'ens3', 'ens4', 'enp1s0'],
            label: { en: 'Interface name', fr: 'Nom d’interface' },
            description: { en: 'As seen by the guest (eth0, ens3…)',
                           fr: 'Vu par l’invité (eth0, ens3…)' } },
          { name: 'mode', type: 'enum', default: 'dhcp', enum_values: ['dhcp', 'static'],
            label: { en: 'Addressing', fr: 'Adressage' },
            description: { en: 'dhcp or static', fr: 'dhcp ou statique' } },
          { name: 'address', type: 'text', validate: /^[0-9.]+\/[0-9]+$/,
            label: { en: 'Address (CIDR)', fr: 'Adresse (CIDR)' },
            description: { en: 'e.g. 172.16.3.50/16', fr: 'ex. 172.16.3.50/16' } },
          { name: 'gateway', type: 'text', validate: /^[0-9.]+$/,
            label: { en: 'Gateway', fr: 'Passerelle' },
            description: { en: 'e.g. 172.16.0.1', fr: 'ex. 172.16.0.1' } },
          { name: 'mtu', type: 'int', min: 576, max: 9216,
            label: { en: 'MTU', fr: 'MTU' },
            description: { en: 'Empty = default (1500)', fr: 'Vide = défaut (1500)' } },
        ],
      },
    },
  };

  /** Minimal YAML string quoting for the narrow structures WE generate. */
  function yamlStr(s) {
    s = String(s);
    if (/^[A-Za-z0-9._\/-]+$/.test(s)) return s;
    return "'" + s.replace(/'/g, "''") + "'";
  }

  /** Emit a YAML literal block (|) with the given indentation. */
  function yamlBlock(text, indent) {
    const pad = ' '.repeat(indent);
    return '|\n' + String(text).replace(/\s+$/, '').split('\n')
      .map(l => pad + l).join('\n');
  }

  function linesOf(text) {
    return String(text || '').split('\n').map(l => l.trim()).filter(Boolean);
  }

  function csvOf(text) {
    return String(text || '').split(',').map(s => s.trim()).filter(Boolean);
  }

  function genUserData(spec) {
    const L = ['#cloud-config'];
    if (spec.hostname) L.push(`hostname: ${yamlStr(spec.hostname)}`);
    if (spec.fqdn) {
      L.push(`fqdn: ${yamlStr(spec.fqdn)}`);
      L.push('manage_etc_hosts: true');
    }
    if (spec.timezone) L.push(`timezone: ${yamlStr(spec.timezone)}`);
    if (spec.locale) L.push(`locale: ${yamlStr(spec.locale)}`);
    if (spec.keyboard) {
      L.push('keyboard:');
      L.push(`  layout: ${yamlStr(spec.keyboard)}`);
    }
    if (spec.ssh_pwauth) L.push('ssh_pwauth: true');
    if (spec.disable_root === false) L.push('disable_root: false');
    if (spec.expire_passwords) {
      L.push('chpasswd:');
      L.push('  expire: true');
    }
    if (spec.package_update) L.push('package_update: true');
    if (spec.package_upgrade) L.push('package_upgrade: true');
    if (spec.package_reboot) L.push('package_reboot_if_required: true');
    if (spec.growpart === false) {
      L.push('growpart:');
      L.push('  mode: off');
    }
    const users = spec.user || [];
    if (users.length) {
      L.push('users:');
      users.forEach(u => {
        L.push(`  - name: ${yamlStr(u.name || '')}`);
        L.push(`    shell: ${yamlStr(u.shell || '/bin/bash')}`);
        if (u.sudo) L.push(`    sudo: ${yamlStr('ALL=(ALL) NOPASSWD:ALL')}`);
        const groups = csvOf(u.groups);
        if (groups.length) L.push(`    groups: ${yamlStr(groups.join(', '))}`);
        if (u.password) {
          L.push(`    plain_text_passwd: ${yamlStr(u.password)}`);
          L.push('    lock_passwd: false');
        }
        const keys = [];
        if (u.ssh_key) keys.push(u.ssh_key);
        keys.push(...linesOf(u.ssh_key_extra));
        if (keys.length) {
          L.push('    ssh_authorized_keys:');
          keys.forEach(k => L.push(`      - ${yamlStr(k)}`));
        }
      });
    }
    const pkgs = linesOf(spec.packages);
    if (pkgs.length) {
      L.push('packages:');
      pkgs.forEach(p => L.push(`  - ${yamlStr(p)}`));
    }
    const fsItems = spec.fs || [];
    if (fsItems.length) {
      L.push('fs_setup:');
      fsItems.forEach(f => {
        L.push(`  - device: ${yamlStr(f.device || '')}`);
        L.push(`    filesystem: ${yamlStr(f.filesystem || 'ext4')}`);
        L.push('    overwrite: false');
      });
      L.push('mounts:');
      fsItems.forEach(f => {
        L.push(`  - [${yamlStr(f.device || '')}, ${yamlStr(f.mount_point || '')}, ${yamlStr(f.filesystem || 'ext4')}, defaults, '0', '2']`);
      });
    }
    const files = spec.file || [];
    if (files.length) {
      L.push('write_files:');
      files.forEach(f => {
        L.push(`  - path: ${yamlStr(f.path || '')}`);
        L.push(`    permissions: '${(f.permissions || '0644').replace(/'/g, '')}'`);
        L.push(`    content: ${yamlBlock(f.content || '', 6)}`);
      });
    }
    const ntp = csvOf(spec.ntp_servers);
    if (ntp.length) {
      L.push('ntp:');
      L.push('  enabled: true');
      L.push('  servers:');
      ntp.forEach(s2 => L.push(`    - ${yamlStr(s2)}`));
    }
    if (spec.ca_certs && String(spec.ca_certs).trim()) {
      L.push('ca_certs:');
      L.push('  trusted:');
      L.push(`    - ${yamlBlock(spec.ca_certs, 6)}`);
    }
    const boots = linesOf(spec.bootcmd);
    if (boots.length) {
      L.push('bootcmd:');
      boots.forEach(c => L.push(`  - ${yamlStr(c)}`));
    }
    const cmds = linesOf(spec.runcmd);
    if (cmds.length) {
      L.push('runcmd:');
      cmds.forEach(c => L.push(`  - ${yamlStr(c)}`));
    }
    return L.join('\n') + '\n';
  }

  function genNetworkData(spec) {
    const nics = (spec.nic && spec.nic.length)
      ? spec.nic : [{ iface: 'eth0', mode: 'dhcp' }];
    const L = ['version: 1', 'config:'];
    nics.forEach(n => {
      L.push('  - type: physical');
      L.push(`    name: ${yamlStr(n.iface || 'eth0')}`);
      if (n.mtu) L.push(`    mtu: ${n.mtu}`);
      L.push('    subnets:');
      if (n.mode === 'static') {
        if (!n.address) throw new Error(tr('vm.edit.ci.errAddr', 'a CIDR address is required for static addressing') + ` (${n.iface || 'eth0'})`);
        L.push('      - type: static');
        L.push(`        address: ${yamlStr(n.address)}`);
        if (n.gateway) L.push(`        gateway: ${yamlStr(n.gateway)}`);
      } else {
        L.push('      - type: dhcp');
      }
    });
    const dns = csvOf(spec.dns);
    const search = csvOf(spec.search);
    if (dns.length || search.length) {
      L.push('  - type: nameserver');
      if (dns.length) {
        L.push('    address:');
        dns.forEach(d => L.push(`      - ${yamlStr(d)}`));
      }
      if (search.length) {
        L.push('    search:');
        search.forEach(d => L.push(`      - ${yamlStr(d)}`));
      }
    }
    return L.join('\n') + '\n';
  }

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
        // v1.13.0 : options fines par disque
        serial: d.serial || '',
        cache: d.cache || '',
        shareable: !!d.shareable,
        readonly: !!(d[devKey] || {}).readonly,
        io_thread: !!d.dedicatedIOThread,
      });
    });
    // Orphan volumes (no matching disk entry) ride along untouched.
    volumes.forEach(v => { if (!usedVols.has(v.name)) passthrough.volumes.push(v); });
    return { items, passthrough };
  }

  // v1.13.0 : bootOrder est une séquence GLOBALE partagée par les disques
  // ET les cartes réseau (appris en vrai : le webhook Harvester répond
  // « Boot order ... already set for a different device »). Chaque éditeur
  // ne voit que sa moitié — on confronte donc l'autre moitié de la spec.
  function bootOrdersTakenBy(vm, kind) {
    const dev = ((((vm.spec || {}).template || {}).spec || {}).domain || {}).devices || {};
    const src = kind === 'disks' ? (dev.disks || []) : (dev.interfaces || []);
    const out = new Map();
    src.forEach(d => { if (d.bootOrder > 0) out.set(d.bootOrder, d.name); });
    return out;
  }

  function checkBootOrder(order, name, taken, otherLabel) {
    if (!(order > 0)) return;
    const clash = taken.get(order);
    if (clash) {
      throw new Error(tr('vm.edit.errBootDup',
        'boot order already used by another device') + `: #${order} — ${otherLabel} "${clash}"`);
    }
    taken.set(order, name);
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
    const takenBoot = bootOrdersTakenBy(vm, 'interfaces');
    passthrough.disks.forEach(d => { if (d.bootOrder > 0) takenBoot.set(d.bootOrder, d.name); });
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
      checkBootOrder(item.boot_order, item.name, takenBoot,
                     tr('vm.edit.devNic', 'interface'));
      if (item.boot_order > 0) d.bootOrder = item.boot_order;
      const devKey = item.device === 'cdrom' ? 'cdrom' : 'disk';
      d[devKey] = { bus: item.bus || 'virtio' };
      // v1.13.0 : options fines — omises quand elles valent le défaut,
      // pour ne pas alourdir la spec ni figer des choix implicites.
      if (item.readonly) d[devKey].readonly = true;
      const serial = (item.serial || '').trim();
      if (serial) {
        if (!/^[A-Za-z0-9_.+-]{1,36}$/.test(serial)) {
          throw new Error(tr('vm.edit.errSerial',
            'disk serial: up to 36 chars, letters/digits/_.+- only') + `: ${item.name}`);
        }
        d.serial = serial;
      }
      if (item.cache) d.cache = item.cache;
      if (item.shareable) d.shareable = true;
      if (item.io_thread) d.dedicatedIOThread = true;
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
        // v1.13.0 : ordre de boot réseau (PXE)
        boot_order: itf.bootOrder ?? 0,
      });
    });
    networks.forEach(n => { if (!used.has(n.name)) passthrough.networks.push(n); });
    return { items, passthrough };
  }

  function formNetsToPatch(items, passthrough, vm) {
    const seen = new Set(passthrough.interfaces.map(i => i.name));
    const interfaces = [...passthrough.interfaces];
    const takenBoot = bootOrdersTakenBy(vm, 'disks');
    passthrough.interfaces.forEach(i => { if (i.bootOrder > 0) takenBoot.set(i.bootOrder, i.name); });
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
      checkBootOrder(item.boot_order, item.name, takenBoot,
                     tr('vm.edit.devDisk', 'disk'));
      if (item.model) itf.model = item.model;
      if (item.mac) itf.macAddress = item.mac;
      // bootOrder partage la MÊME séquence que les disques : une VM qui
      // doit démarrer en PXE met son NIC à 1 et repousse le disque.
      if (item.boot_order > 0) itf.bootOrder = item.boot_order;
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
      return FloatingPanels.open({ id: panelId, title,
        headerActions: consoleAction(cluster, namespace, name) });
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
      headerActions: consoleAction(cluster, namespace, name),
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
  // v1.13.0 : raccourci vers la console dans le bandeau du panneau Edit
  // (symétrique du bouton ⚙ que la console offre déjà vers l'éditeur).
  // ⚠️ le global est VMConsole (casse déjà payée en 1.7.1).
  function consoleAction(cluster, namespace, name) {
    return [{
      label: Icons.svg('console'),
      tip: tr('vm.edit.openConsole', 'Open the VNC console for this VM'),
      onClick: () => window.VMConsole && window.VMConsole.open(cluster, namespace, name),
    }];
  }

  function renderSectionHtml(id, vm, cluster) {
    const spec     = vm.spec || {};
    const template = (spec.template || {}).spec || {};
    const domain   = template.domain || {};
    const annot    = (vm.metadata?.annotations) || {};

    switch (id) {
      case 'general': return renderGeneral(vm, spec, annot, cluster);
      case 'compute': return renderCompute(domain);
      case 'firmware': return renderFirmware(domain);
      case 'placement': return renderPlacement(vm, template, cluster);
      case 'lifecycle': return renderLifecycle(template);
      case 'disks':   return renderDisksSection(vm, cluster);
      case 'network': return renderNetworkSection(vm, cluster);
      case 'cloudinit': return renderCloudInit(cluster);
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

  const TAG_PREFIX = 'tag.harvesterhci.io/';

  /** labels du template -> lignes {key,value} pour l'éditeur de tags */
  function vmTagsToForm(vm) {
    const labels = (((vm.spec || {}).template || {}).metadata || {}).labels || {};
    return Object.keys(labels)
      .filter(k => k.startsWith(TAG_PREFIX))
      .sort()
      .map(k => ({ key: k.slice(TAG_PREFIX.length), value: labels[k] }));
  }

  function renderGeneral(vm, spec, annot, cluster) {
    const description = annot['harvesterhci.io/description'] || annot['description'] || '';
    const hostname = ((spec.template || {}).spec || {}).hostname || '';
    const tagItems = vmTagsToForm(vm);
    return `
      <h3>${esc(tr('vm.edit.general', 'General'))}</h3>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fName', 'Name'))}</label>
        <input type="text" value="${esc(vm.metadata.name)}" readonly>
      </div>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fNamespace', 'Namespace'))}</label>
        <input type="text" value="${esc(vm.metadata.namespace)}" readonly>
      </div>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fDescription', 'Description'))}</label>
        <textarea data-field="annot.description" rows="2">${esc(description)}</textarea>
      </div>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fRunStrategy', 'Run strategy'))}</label>
        <select data-field="spec.runStrategy">
          ${['Always','RerunOnFailure','Manual','Halted'].map(v =>
            `<option value="${v}" ${spec.runStrategy === v ? 'selected' : ''}>${v}</option>`).join('')}
        </select>
      </div>
      <div class="form-row">
        <label>${esc(tr('vm.edit.hostname', 'Guest hostname'))}</label>
        <input type="text" data-field="spec.hostname" value="${esc(hostname)}"
               placeholder="${esc(vm.metadata.name)}">
        <span class="form-hint">${esc(tr('vm.edit.hostnameHint', 'Empty = the VM name is used'))}</span>
      </div>
      <h3>🏷 ${esc(tr('vm.edit.tags', 'Tags'))}</h3>
      <p class="form-hint">${esc(tr('vm.edit.tagsHint', 'Harvester tags (labels tag.harvesterhci.io/<key>) — also shown in the Harvester UI.'))}</p>
      <div class="vm-edit-cards" data-cards="tags">
        ${TFForm.render(TAG_SCHEMA, cluster, { tag: tagItems }, { hideHeader: true })}
      </div>
      ${applyBar('general')}`;
  }

  function renderCompute(domain) {
    const cpu = domain.cpu || {};
    const mem = domain.memory || {};
    const res = (domain.resources || {}).requests || {};
    const lim = (domain.resources || {}).limits || {};
    return `
      <h3>${esc(tr('vm.edit.computeTitle', 'Compute resources'))}</h3>
      <div class="grid-2">
        <div class="form-row">
          <label>${esc(tr('vm.edit.fSockets', 'CPU sockets'))}</label>
          <input type="number" min="1" data-field="cpu.sockets" value="${cpu.sockets ?? 1}">
        </div>
        <div class="form-row">
          <label>${esc(tr('vm.edit.fCores', 'CPU cores'))}</label>
          <input type="number" min="1" data-field="cpu.cores" value="${cpu.cores ?? 1}">
        </div>
        <div class="form-row">
          <label>${esc(tr('vm.edit.fThreads', 'Threads per core'))}</label>
          <input type="number" min="1" data-field="cpu.threads" value="${cpu.threads ?? 1}">
        </div>
        <div class="form-row">
          <label>${esc(tr('vm.edit.fMemory', 'Memory (guest)'))}</label>
          <input type="text" data-field="memory.guest" value="${esc(mem.guest || '')}" placeholder="4Gi">
        </div>
      </div>
      <h3>${esc(tr('vm.edit.hotplug', 'Hot-plug ceilings'))}</h3>
      <p class="form-hint">${esc(tr('vm.edit.hotplugHint', 'Set these ABOVE the current values to allow adding CPU or memory without a reboot later. They cannot be lowered while the VM runs.'))}</p>
      <div class="grid-2">
        <div class="form-row">
          <label>${esc(tr('vm.edit.maxSockets', 'Max CPU sockets (hot-plug)'))}</label>
          <input type="number" min="0" data-field="cpu.maxSockets" value="${cpu.maxSockets ?? ''}"
                 placeholder="${cpu.sockets ?? 1}">
        </div>
        <div class="form-row">
          <label>${esc(tr('vm.edit.maxGuest', 'Max guest memory (hot-plug)'))}</label>
          <input type="text" data-field="memory.maxGuest" value="${esc(mem.maxGuest || '')}"
                 placeholder="${esc(mem.guest || '8Gi')}">
        </div>
      </div>
      <h3>${esc(tr('vm.edit.cpuModel', 'CPU model'))}</h3>
      <div class="form-row">
        <label>${esc(tr('vm.edit.cpuModel', 'CPU model'))}</label>
        <input type="text" data-field="cpu.model" value="${esc(cpu.model || '')}"
               list="dl-cpu-model" autocomplete="off" placeholder="(default)">
        <datalist id="dl-cpu-model">
          <option value="host-model"></option>
          <option value="host-passthrough"></option>
          <option value="Skylake-Server"></option>
          <option value="Cascadelake-Server"></option>
        </datalist>
        <span class="form-hint">${esc(tr('vm.edit.cpuModelHint', 'host-passthrough is fastest but blocks live migration to a different CPU; host-model is the usual compromise. Empty = cluster default.'))}</span>
      </div>
      <details class="vm-edit-adv">
        <summary>${esc(tr('vm.edit.resAdvanced', 'Scheduling reservations (advanced)'))}</summary>
        <p class="form-hint">${esc(tr('vm.edit.resHint', 'What Kubernetes actually schedules. Harvester derives these from the overcommit ratio — override only if you know why. Empty = leave as-is.'))}</p>
        <div class="grid-2">
          <div class="form-row">
            <label>limits.cpu</label>
            <input type="text" data-field="res.limits.cpu" value="${esc(lim.cpu || '')}" placeholder="2">
          </div>
          <div class="form-row">
            <label>limits.memory</label>
            <input type="text" data-field="res.limits.memory" value="${esc(lim.memory || '')}" placeholder="4Gi">
          </div>
          <div class="form-row">
            <label>requests.cpu</label>
            <input type="text" data-field="res.requests.cpu" value="${esc(res.cpu || '')}" placeholder="125m">
          </div>
          <div class="form-row">
            <label>requests.memory</label>
            <input type="text" data-field="res.requests.memory" value="${esc(res.memory || '')}" placeholder="2730Mi">
          </div>
        </div>
      </details>
      <p class="form-hint">${esc(tr('vm.edit.computeHint', 'Changing CPU/memory while the VM is running may require a reboot for the guest to see the new values.'))}</p>
      ${applyBar('compute')}`;
  }

  // v1.12.0 — Firmware : UEFI / Secure Boot / TPM / type de machine.
  // Débloque les invités modernes (Windows 11, SLE 16) qui refusent de
  // booter en BIOS hérité ou exigent un TPM 2.0.
  function renderFirmware(domain) {
    const fw   = domain.firmware || {};
    const boot = fw.bootloader || {};
    const efi  = boot.efi || null;
    const mode = efi ? (efi.secureBoot === false ? 'uefi' : 'uefi-sb') : 'bios';
    const tpm  = (domain.devices || {}).tpm || null;
    const machine = (domain.machine || {}).type || '';
    const opt = (v, cur, label) =>
      `<option value="${v}" ${cur === v ? 'selected' : ''}>${label}</option>`;
    return `
      <h3>🔧 ${esc(tr('vm.edit.firmware', 'Firmware'))}</h3>
      <div class="form-row">
        <label>${esc(tr('vm.edit.bootMode', 'Boot mode'))}</label>
        <select data-field="fw.mode">
          ${opt('bios', mode, 'BIOS (legacy)')}
          ${opt('uefi', mode, 'UEFI')}
          ${opt('uefi-sb', mode, 'UEFI + Secure Boot')}
        </select>
        <span class="form-hint">${esc(tr('vm.edit.bootModeHint', 'Windows 11 and recent SLES/openSUSE images need UEFI; Secure Boot additionally enables the SMM feature. Switching mode on an installed guest usually makes it unbootable — set it before the first install.'))}</span>
      </div>
      <div class="form-row">
        <label class="opt-row">
          <input type="checkbox" data-field="fw.tpm" ${tpm ? 'checked' : ''}>
          <span>${esc(tr('vm.edit.tpm', 'Attach a TPM 2.0 device'))}</span>
        </label>
        <span class="form-hint">${esc(tr('vm.edit.tpmHint', 'Required by Windows 11. Persistent keeps the TPM state across reboots (needed for BitLocker).'))}</span>
      </div>
      <div class="form-row">
        <label class="opt-row">
          <input type="checkbox" data-field="fw.tpmPersistent" ${(tpm && tpm.persistent) ? 'checked' : ''}>
          <span>${esc(tr('vm.edit.tpmPersistent', 'Persistent TPM state'))}</span>
        </label>
      </div>
      <div class="grid-2">
        <div class="form-row">
          <label>${esc(tr('vm.edit.machineType', 'Machine type'))}</label>
          <input type="text" data-field="fw.machine" value="${esc(machine)}"
                 list="dl-machine" autocomplete="off" placeholder="q35">
          <datalist id="dl-machine">
            <option value="q35"></option>
            <option value="pc"></option>
          </datalist>
        </div>
        <div class="form-row">
          <label>${esc(tr('vm.edit.fwSerial', 'Firmware serial number'))}</label>
          <input type="text" data-field="fw.serial" value="${esc(fw.serial || '')}"
                 placeholder="(auto)">
          <span class="form-hint">${esc(tr('vm.edit.fwSerialHint', 'Some licences are bound to it. Empty = leave as-is.'))}</span>
        </div>
      </div>
      ${restartBanner()}
      ${applyBar('firmware')}`;
  }

  /** nodeSelector -> lignes, et règles pod -> lignes éditables */
  function vmPlacementToForm(vm) {
    const t = (((vm.spec || {}).template || {}).spec) || {};
    const sel = Object.entries(t.nodeSelector || {}).map(([key, value]) => ({ key, value }));
    const aff = t.affinity || {};
    const rules = [];
    const harvest = (block, kind) => {
      if (!block) return;
      (block.requiredDuringSchedulingIgnoredDuringExecution || []).forEach(term => {
        const m = ((term.labelSelector || {}).matchLabels) || {};
        Object.entries(m).forEach(([k, v]) => rules.push({
          kind, hard: true,
          key: k.startsWith(TAG_PREFIX) ? k.slice(TAG_PREFIX.length) : k, value: v,
        }));
      });
      (block.preferredDuringSchedulingIgnoredDuringExecution || []).forEach(w => {
        const m = (((w.podAffinityTerm || {}).labelSelector || {}).matchLabels) || {};
        Object.entries(m).forEach(([k, v]) => rules.push({
          kind, hard: false,
          key: k.startsWith(TAG_PREFIX) ? k.slice(TAG_PREFIX.length) : k, value: v,
        }));
      });
    };
    harvest(aff.podAntiAffinity, 'avoid');
    harvest(aff.podAffinity, 'attract');
    return { sel, rules };
  }

  /** lignes -> blocs podAffinity / podAntiAffinity (null si vide) */
  function formPlacementToAffinity(rules) {
    const mk = (list) => {
      const hard = list.filter(r => r.hard).map(r => ({
        labelSelector: { matchLabels: { [TAG_PREFIX + r.key]: String(r.value) } },
        topologyKey: 'kubernetes.io/hostname',
      }));
      const soft = list.filter(r => !r.hard).map(r => ({
        weight: 100,
        podAffinityTerm: {
          labelSelector: { matchLabels: { [TAG_PREFIX + r.key]: String(r.value) } },
          topologyKey: 'kubernetes.io/hostname',
        },
      }));
      if (!hard.length && !soft.length) return null;
      const out = {};
      out.requiredDuringSchedulingIgnoredDuringExecution = hard;
      out.preferredDuringSchedulingIgnoredDuringExecution = soft;
      return out;
    };
    return {
      podAntiAffinity: mk(rules.filter(r => r.kind !== 'attract')),
      podAffinity: mk(rules.filter(r => r.kind === 'attract')),
    };
  }

  function renderPlacement(vm, template, cluster) {
    const { sel, rules } = vmPlacementToForm(vm);
    const managed = ((template.affinity || {}).nodeAffinity) || null;
    return `
      <h3>📍 ${esc(tr('vm.edit.placement', 'Placement'))}</h3>
      <p class="form-hint">${esc(tr('vm.edit.nodeSelHint', 'Pin the VM to nodes carrying these labels. Empty = the scheduler is free.'))}</p>
      <div class="vm-edit-cards" data-cards="nodesel">
        ${TFForm.render(NODESEL_SCHEMA, cluster, { sel }, { hideHeader: true })}
      </div>
      <h3>${esc(tr('vm.edit.affinityTitle', 'Rules relative to other VMs'))}</h3>
      <p class="form-hint">${esc(tr('vm.edit.affinityHint', 'Keep a VM away from its twin (HA pair) or next to a VM it talks to a lot. Matching is done on Harvester tags, so tag both VMs first.'))}</p>
      <div class="vm-edit-cards" data-cards="affinity">
        ${TFForm.render(AFFINITY_SCHEMA, cluster, { rule: rules }, { hideHeader: true })}
      </div>
      ${managed ? `
        <details class="vm-edit-adv">
          <summary>${esc(tr('vm.edit.managedAffinity', 'Node affinity managed by Harvester (read-only)'))}</summary>
          <p class="form-hint">${esc(tr('vm.edit.managedAffinityHint', 'Harvester derives this from the networks the VM is attached to. It is left untouched when you save.'))}</p>
          <pre>${esc(JSON.stringify(managed, null, 2))}</pre>
        </details>` : ''}
      ${restartBanner()}
      ${applyBar('placement')}`;
  }

  function renderLifecycle(template) {
    return `
      <h3>${esc(tr('vm.edit.lifecycle', 'Lifecycle'))}</h3>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fGrace', 'ACPI shutdown grace period (seconds)'))}</label>
        <input type="number" min="0" data-field="lifecycle.terminationGracePeriodSeconds" value="${template.terminationGracePeriodSeconds ?? 180}">
      </div>
      <div class="form-row">
        <label>${esc(tr('vm.edit.fEviction', 'Eviction strategy'))}</label>
        <select data-field="lifecycle.evictionStrategy">
          <option value="">(none)</option>
          <option value="LiveMigrate"      ${template.evictionStrategy === 'LiveMigrate' ? 'selected' : ''}>LiveMigrate</option>
          <option value="External"         ${template.evictionStrategy === 'External' ? 'selected' : ''}>External</option>
          <option value="LiveMigrateIfPossible" ${template.evictionStrategy === 'LiveMigrateIfPossible' ? 'selected' : ''}>LiveMigrateIfPossible</option>
        </select>
      </div>
      ${applyBar('lifecycle')}`;
  }

  function renderCloudInit(cluster) {
    return `
      <h3>Cloud-init</h3>
      <p class="form-hint">Edit user-data and network-data. Saved to the VM's cloud-init Secret.</p>
      <details class="vm-edit-adv vm-edit-ci-wizard">
        <summary>🧙 ${esc(tr('vm.edit.ci.wizard', 'Assistant — generate the YAML below'))}</summary>
        <p class="form-hint">${esc(tr('vm.edit.ci.wizardHint',
          'Fill in what you need, then Generate: the editors below are replaced with clean cloud-config / network-data v1 YAML. Review, then Save.'))}</p>
        <h4>${esc(tr('vm.edit.ci.userTitle', 'System (user-data)'))}</h4>
        <div class="vm-edit-ci-userform">
          ${CI_USER_SCHEMA.sections.map(sec => `
            <h5 class="vm-edit-ci-sec">${esc(TFForm && window.i18n ? (sec.label[i18n.currentLang] || sec.label.en) : sec.label.en)}</h5>
            ${TFForm.render(CI_USER_SCHEMA, cluster, {}, { hideHeader: true, sectionId: sec.id })}`).join('')}
        </div>
        <div class="apply-bar">
          <button class="btn btn-sm btn-primary" data-action="gen-userdata">${esc(tr('vm.edit.ci.genUser', 'Generate user-data'))}</button>
          <span class="apply-result" data-section="ci-user"></span>
        </div>
        <h4>${esc(tr('vm.edit.ci.netTitle', 'Network (network-data)'))}</h4>
        <div class="vm-edit-ci-netform">${TFForm.render(CI_NET_SCHEMA, cluster, {}, { hideHeader: true })}</div>
        <div class="apply-bar">
          <button class="btn btn-sm btn-primary" data-action="gen-netdata">${esc(tr('vm.edit.ci.genNet', 'Generate network-data'))}</button>
          <span class="apply-result" data-section="ci-net"></span>
        </div>
      </details>
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

      // v1.8.1 — assistant wiring: two generator forms feeding the editors.
      // Container divs (the user form spans several section renders, so
      // there are multiple .tf-form roots inside — wire/read on the parent).
      const userForm = sectionEl.querySelector('.vm-edit-ci-userform');
      const netForm = sectionEl.querySelector('.vm-edit-ci-netform');
      TFForm.wire(userForm, CI_USER_SCHEMA, cluster);
      TFForm.wire(netForm, CI_NET_SCHEMA, cluster);
      // Per-NIC card: address/gateway only make sense in static mode.
      const syncNet = () => {
        netForm.querySelectorAll('.tf-block-item').forEach(item => {
          const mode = item.querySelector('[name$=".mode"]')?.value || 'dhcp';
          ['address', 'gateway'].forEach(f => {
            const el = item.querySelector(`[name$=".${f}"]`);
            const wrap = el && el.closest('.tf-field');
            if (wrap) wrap.style.display = mode === 'static' ? '' : 'none';
          });
        });
      };
      syncNet();
      netForm.addEventListener('change', syncNet);
      const nicList = netForm.querySelector('.tf-block-list');
      if (nicList) new MutationObserver(syncNet).observe(nicList, { childList: true });

      const generate = (kind) => {
        const isUser = kind === 'user';
        const target = sectionEl.querySelector(`[data-ci="${isUser ? 'userData' : 'networkData'}"]`);
        const result = sectionEl.querySelector(`.apply-result[data-section="ci-${isUser ? 'user' : 'net'}"]`);
        try {
          const spec = TFForm.read(isUser ? userForm : netForm,
                                   isUser ? CI_USER_SCHEMA : CI_NET_SCHEMA,
                                   { emitEmptyLists: true });
          const yaml = isUser ? genUserData(spec) : genNetworkData(spec);
          if (target.value.trim()
              && !confirm(tr('vm.edit.ci.confirmReplace',
                'Replace the current editor content with the generated YAML?'))) return;
          target.value = yaml;
          result.innerHTML = `<span style="color:var(--accent)">✓ ${esc(tr('vm.edit.ci.generated', 'generated — review below, then Save'))}</span>`;
        } catch (e) {
          result.innerHTML = `<span style="color:var(--danger)">✗ ${esc(e.message)}</span>`;
        }
      };
      sectionEl.querySelector('[data-action="gen-userdata"]').addEventListener('click', () => generate('user'));
      sectionEl.querySelector('[data-action="gen-netdata"]').addEventListener('click', () => generate('net'));
      return;
    }

    // v1.12.0 : l'onglet General embarque l'éditeur de tags (cartes TFForm)
    if (sectionId === 'general') {
      const tagsEditor = sectionEl.querySelector('.vm-edit-cards .tf-form');
      if (tagsEditor) TFForm.wire(tagsEditor, TAG_SCHEMA, cluster);
    }

    if (sectionId === 'placement') {
      const selEditor = sectionEl.querySelector('[data-cards="nodesel"] .tf-form');
      const affEditor = sectionEl.querySelector('[data-cards="affinity"] .tf-form');
      if (selEditor) TFForm.wire(selEditor, NODESEL_SCHEMA, cluster);
      if (affEditor) TFForm.wire(affEditor, AFFINITY_SCHEMA, cluster);
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
      case 'general': {
        // v1.12.0 : un merge patch FUSIONNE les labels — pour retirer un
        // tag il faut explicitement le mettre à null, sinon il survit.
        const editor = get('.vm-edit-cards .tf-form');
        const labels = {};
        if (editor) {
          const rows = (TFForm.read(editor, TAG_SCHEMA, { emitEmptyLists: true }).tag) || [];
          const kept = new Set();
          rows.forEach(t => {
            const k = (t.key || '').trim();
            if (!k) return;
            if (!/^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$/.test(k)) {
              throw new Error(tr('vm.edit.errTagKey', 'invalid tag key') + `: "${k}"`);
            }
            if (kept.has(k)) {
              throw new Error(tr('vm.edit.errTagDup', 'duplicate tag key') + `: ${k}`);
            }
            kept.add(k);
            labels[TAG_PREFIX + k] = String(t.value ?? '');
          });
          vmTagsToForm(vm).forEach(prev => {
            if (!kept.has(prev.key)) labels[TAG_PREFIX + prev.key] = null;
          });
        }
        return {
          metadata: {
            annotations: { 'harvesterhci.io/description': val('annot.description') || '' },
          },
          spec: {
            runStrategy: val('spec.runStrategy'),
            template: {
              metadata: { labels },
              spec: { hostname: (val('spec.hostname') || '').trim() || null },
            },
          },
        };
      }
      case 'compute': {
        const cpu = {
          sockets: val('cpu.sockets'),
          cores:   val('cpu.cores'),
          threads: val('cpu.threads'),
        };
        // Champs optionnels : vide = ne pas imposer (null retire la clé).
        const maxSockets = val('cpu.maxSockets');
        cpu.maxSockets = maxSockets > 0 ? maxSockets : null;
        cpu.model = (val('cpu.model') || '').trim() || null;
        const memory = { guest: val('memory.guest') };
        memory.maxGuest = (val('memory.maxGuest') || '').trim() || null;
        const pick = (f) => (val(f) || '').trim() || null;
        const resources = {};
        const limits = { cpu: pick('res.limits.cpu'), memory: pick('res.limits.memory') };
        const requests = { cpu: pick('res.requests.cpu'), memory: pick('res.requests.memory') };
        if (limits.cpu || limits.memory) resources.limits = limits;
        if (requests.cpu || requests.memory) resources.requests = requests;
        const domain = { cpu, memory };
        if (Object.keys(resources).length) domain.resources = resources;
        return { spec: { template: { spec: { domain } } } };
      }
      case 'firmware': {
        const mode = val('fw.mode') || 'bios';
        const tpmOn = !!get('[data-field="fw.tpm"]')?.checked;
        const tpmPersist = !!get('[data-field="fw.tpmPersistent"]')?.checked;
        const machine = (val('fw.machine') || '').trim();
        const serial = (val('fw.serial') || '').trim();
        const firmware = {};
        if (mode === 'bios') {
          firmware.bootloader = null;          // retire l'EFI -> BIOS hérité
        } else {
          firmware.bootloader = { efi: { secureBoot: mode === 'uefi-sb' } };
        }
        if (serial) firmware.serial = serial;
        const domain = {
          firmware,
          // Secure Boot EXIGE la fonctionnalité SMM côté KubeVirt ; sans
          // elle l'apiserver refuse la VM. On la retire en sortant du
          // Secure Boot pour ne pas laisser de résidu.
          features: { smm: mode === 'uefi-sb' ? { enabled: true } : null },
          devices: { tpm: tpmOn ? (tpmPersist ? { persistent: true } : {}) : null },
        };
        if (machine) domain.machine = { type: machine };
        return { spec: { template: { spec: { domain } } } };
      }
      case 'placement': {
        const selEditor = sectionEl.querySelector('[data-cards="nodesel"] .tf-form');
        const affEditor = sectionEl.querySelector('[data-cards="affinity"] .tf-form');
        const prev = vmPlacementToForm(vm);
        // nodeSelector : map -> une clé retirée doit partir à null.
        const nodeSelector = {};
        const kept = new Set();
        ((selEditor ? TFForm.read(selEditor, NODESEL_SCHEMA, { emitEmptyLists: true }).sel : []) || [])
          .forEach(r => {
            const k = (r.key || '').trim();
            if (!k) return;
            kept.add(k);
            nodeSelector[k] = String(r.value ?? '');
          });
        prev.sel.forEach(p => { if (!kept.has(p.key)) nodeSelector[p.key] = null; });
        const rules = ((affEditor ? TFForm.read(affEditor, AFFINITY_SCHEMA, { emitEmptyLists: true }).rule : []) || [])
          .filter(r => (r.key || '').trim() && String(r.value ?? '').trim());
        const affinity = formPlacementToAffinity(rules);
        return { spec: { template: { spec: { nodeSelector, affinity } } } };
      }
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
    _mappers: {
      vmTagsToForm, vmPlacementToForm, formPlacementToAffinity, vmDisksToForm, formDisksToPatch, vmNetsToForm, formNetsToPatch,
                genUserData, genNetworkData },
    _schemas: { DISK_SCHEMA, NET_SCHEMA, CI_USER_SCHEMA, CI_NET_SCHEMA, TAG_SCHEMA,
                 NODESEL_SCHEMA, AFFINITY_SCHEMA },
  };
})();

if (typeof window !== 'undefined') window.VMEdit = VMEdit;
if (typeof FloatingPanels !== 'undefined') {
  FloatingPanels.registerType('vm-edit', (args) =>
    VMEdit.open(args.cluster, args.namespace, args.name));
}
