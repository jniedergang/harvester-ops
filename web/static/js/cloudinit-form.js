/**
 * harvester-ops : assistant cloud-init commun (prochaine version KVM).
 *
 * Extrait de vm-edit.js sans changer son comportement pour Harvester :
 * schémas du formulaire (CI_USER_SCHEMA, CI_NET_SCHEMA), générateurs YAML
 * (genUserData, genNetworkData), rendu et câblage de l'assistant. Pur, sans
 * dépendance à KubeVirt : les VMs Harvester (vm-edit.js, qui réexporte ces
 * fonctions) et les VMs KVM (kvm-form.js) s'en servent.
 *
 * Ce qui reste propre à une famille passe par des options :
 *   - `userSchema({ harvesterKey })` : le champ « SSH key (Harvester) » d'un
 *     utilisateur lit les KeyPairs d'un cluster ; côté KVM il n'y a pas de
 *     cluster, les clés viennent de l'en-tête de la fenêtre ;
 *   - `wireWizard(..., { confirmReplace })` : la confirmation avant
 *     d'écraser l'éditeur (Harvester garde sa boîte du navigateur, KVM
 *     confirme dans la page) ;
 *   - `withKeysAndAgent()` : clés SSH et agent invité ajoutés au user-data,
 *     ce que le serveur fait pour Harvester (hv.with_ssh_keys).
 */
const CloudInitForm = (() => {
  const tr = (k, fb) => {
    try { const v = window.i18n && i18n.t(k); return v && v !== k ? v : fb; }
    catch { return fb; }
  };
  const esc = (s) => String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  const K8S_NAME_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

  // premier nom libre (eth0 pris : eth1...), comme dans vm-edit.js
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

  // =========================================================================
  // Cloud-init assistant (v1.8.1), a one-way GENERATOR: the forms below
  // produce cloud-config / network-data v1 YAML into the expert textareas
  // (which stay the saved truth). We deliberately do not parse existing
  // YAML back into the form: that would need a full YAML parser and
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
        itemTitle: (v) => ({ icon: 'user', text:
          `${v.name || tr('vm.edit.ci.newUser', 'new user')}${v.sudo ? ' · sudo' : ''}` }),
        args: [
          { name: 'name', type: 'text', required: true, validate: K8S_NAME_RE,
            label: { en: 'Username', fr: 'Nom d’utilisateur' },
            description: { en: 'The account to create', fr: 'Le compte à créer' } },
          { name: 'password', type: 'text',
            label: { en: 'Password', fr: 'Mot de passe' },
            description: { en: 'Stored as plain text in the cloud-init Secret, prefer SSH keys',
                           fr: 'Stocké en clair dans le Secret cloud-init, préférez les clés SSH' } },
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
        itemTitle: (v) => ({ icon: 'storage', text:
          `${v.device || '/dev/vdb'} -> ${v.mount_point || '?'}${v.filesystem ? ' (' + v.filesystem + ')' : ''}` }),
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
        itemTitle: (v) => ({ icon: 'doc', text:
          `${v.path || tr('vm.edit.ci.newFile', 'new file')}` }),
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
        itemTitle: (v) => ({ icon: 'plug', text:
          `${v.iface || 'eth0'} (${v.mode || 'dhcp'})${v.address ? ' · ' + v.address : ''}` }),
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


  /** Le schéma des utilisateurs : celui de Harvester (champ « SSH key
   *  (Harvester) » qui lit les KeyPairs du cluster), ou sans ce champ quand
   *  il n'y a pas de cluster (KVM : clés choisies dans l'en-tête). */
  function userSchema(opts = {}) {
    if (opts.harvesterKey !== false) return CI_USER_SCHEMA;
    const user = CI_USER_SCHEMA.nested.user;
    return Object.assign({}, CI_USER_SCHEMA, {
      id: 'ci-userdata-nokey',
      nested: Object.assign({}, CI_USER_SCHEMA.nested, {
        user: Object.assign({}, user, { args: user.args.filter(a => a.name !== 'ssh_key') }),
      }),
    });
  }

  /** L'assistant (formulaires et boutons « Générer »), même balisage que
   *  dans l'éditeur Harvester. `opts.schema` : schéma des utilisateurs,
   *  `opts.tips` : bulles d'aide sur les boutons. */
  function renderWizard(cluster, opts = {}) {
    const schema = opts.schema || CI_USER_SCHEMA;
    const tip = (k, fb) => (opts.tips ? ` tip" data-tip="${esc(tr(k, fb))}` : '');
    return `
      <details class="vm-edit-adv vm-edit-ci-wizard">
        <summary>${Icons.svg('wizard')} ${esc(tr('vm.edit.ci.wizard', 'Assistant, generate the YAML below'))}</summary>
        <p class="form-hint">${esc(tr('vm.edit.ci.wizardHint',
          'Fill in what you need, then Generate: the editors below are replaced with clean cloud-config / network-data v1 YAML. Review, then Save.'))}</p>
        <h4>${esc(tr('vm.edit.ci.userTitle', 'System (user-data)'))}</h4>
        <div class="vm-edit-ci-userform">
          ${schema.sections.map(sec => `
            <h5 class="vm-edit-ci-sec">${esc(TFForm && window.i18n ? (sec.label[i18n.currentLang] || sec.label.en) : sec.label.en)}</h5>
            ${TFForm.render(schema, cluster, {}, { hideHeader: true, sectionId: sec.id })}`).join('')}
        </div>
        <div class="apply-bar">
          <button class="btn btn-sm btn-primary${tip('ci.genUserTip', 'Writes the user-data editor below from this form')}" data-action="gen-userdata">${esc(tr('vm.edit.ci.genUser', 'Generate user-data'))}</button>
          <span class="apply-result" data-section="ci-user"></span>
        </div>
        <h4>${esc(tr('vm.edit.ci.netTitle', 'Network (network-data)'))}</h4>
        <div class="vm-edit-ci-netform">${TFForm.render(CI_NET_SCHEMA, cluster, {}, { hideHeader: true })}</div>
        <div class="apply-bar">
          <button class="btn btn-sm btn-primary${tip('ci.genNetTip', 'Writes the network-data editor below from this form')}" data-action="gen-netdata">${esc(tr('vm.edit.ci.genNet', 'Generate network-data'))}</button>
          <span class="apply-result" data-section="ci-net"></span>
        </div>
      </details>`;
  }

  /** Câble l'assistant rendu par `renderWizard` dans `sectionEl` : les deux
   *  formulaires, les champs statiques d'une carte, les boutons « Générer ».
   *  `opts.confirmReplace()` rend vrai ou faux (ou une promesse) avant
   *  d'écraser un éditeur non vide. */
  function wireWizard(sectionEl, cluster, opts = {}) {
    const schema = opts.schema || CI_USER_SCHEMA;
    const confirmReplace = opts.confirmReplace || (() => true);
    // Container divs (the user form spans several section renders, so
    // there are multiple .tf-form roots inside, so wire/read on the parent).
    const userForm = sectionEl.querySelector('.vm-edit-ci-userform');
    const netForm = sectionEl.querySelector('.vm-edit-ci-netform');
    TFForm.wire(userForm, schema, cluster);
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

    // bulles sur + Add / − Remove de TFForm (fenêtres KVM : bulle obligatoire)
    if (opts.tips) {
      const tipButtons = () => sectionEl.querySelectorAll('.vm-edit-ci-wizard .tf-block-add, .vm-edit-ci-wizard .tf-block-remove').forEach(b => {
        if (b.dataset.tip) return;
        b.classList.add('tip');
        b.dataset.tip = b.classList.contains('tf-block-add')
          ? tr('ci.addTip', 'Add an entry to this list') : tr('ci.removeTip', 'Remove this entry');
      });
      tipButtons();
      sectionEl.querySelectorAll('.vm-edit-ci-wizard .tf-block-list').forEach(l =>
        new MutationObserver(tipButtons).observe(l, { childList: true }));
    }

    const generate = async (kind) => {
      const isUser = kind === 'user';
      const target = sectionEl.querySelector(`[data-ci="${isUser ? 'userData' : 'networkData'}"]`);
      const result = sectionEl.querySelector(`.apply-result[data-section="ci-${isUser ? 'user' : 'net'}"]`);
      try {
        const spec = TFForm.read(isUser ? userForm : netForm,
                                 isUser ? schema : CI_NET_SCHEMA,
                                 { emitEmptyLists: true });
        const yaml = isUser ? genUserData(spec) : genNetworkData(spec);
        if (target.value.trim() && !(await confirmReplace(result))) return;
        target.value = yaml;
        result.innerHTML = `<span style="color:var(--accent)">${Icons.svg('ok', { size: 14 })} ${esc(tr('vm.edit.ci.generated', 'generated, review below, then Save'))}</span>`;
      } catch (e) {
        result.innerHTML = `<span style="color:var(--danger)">${Icons.svg('fail', { size: 14 })} ${esc(e.message)}</span>`;
      }
    };
    sectionEl.querySelector('[data-action="gen-userdata"]').addEventListener('click', () => generate('user'));
    sectionEl.querySelector('[data-action="gen-netdata"]').addEventListener('click', () => generate('net'));
  }

  /** Une clé publique SSH plausible (type connu, une seule ligne). */
  const SSH_KEY_RE = /^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com) [A-Za-z0-9+/=]+( [^\r\n]*)?$/;

  /** Clés SSH et agent invité ajoutés à un user-data `#cloud-config`, comme
   *  le fait le serveur pour Harvester : `ssh_authorized_keys` complété (une
   *  clé déjà là n'est pas répétée), `qemu-guest-agent` ajouté aux paquets et
   *  démarré par `runcmd`. Un user-data qui n'est pas un `#cloud-config` (script
   *  shell, MIME) est rendu tel quel : rien ne s'y insère sans le casser ; un
   *  user-data vide devient un `#cloud-config`. Rend {text, skipped}. */
  function withKeysAndAgent(userData, keys, agent) {
    const want = [...new Set((keys || []).map(k => String(k).trim()).filter(Boolean))];
    let text = String(userData || '');
    if (!want.length && !agent) return { text, skipped: false };
    if (!text.trim()) text = '#cloud-config\n';
    if (!/^#cloud-config[ \t]*(\r?\n|$)/.test(text)) return { text: String(userData || ''), skipped: true };
    if (!text.endsWith('\n')) text += '\n';
    const lines = text.split('\n');
    const topIndex = (key) => lines.findIndex(l => new RegExp(`^${key}:\\s*$`).test(l));
    const insertItems = (key, items) => {
      if (!items.length) return;
      const i = topIndex(key);
      if (i < 0) {
        if (lines[lines.length - 1] === '') lines.pop();
        lines.push(`${key}:`, ...items.map(v => `  - ${v}`), '');
        return;
      }
      let j = i + 1;
      while (j < lines.length && /^(\s+|-)/.test(lines[j]) && lines[j].trim() !== '') j += 1;
      lines.splice(j, 0, ...items.map(v => `  - ${v}`));
    };
    const quote = (v) => "'" + String(v).replace(/'/g, "''") + "'";
    const present = new Set(lines.map(l => l.replace(/^\s*-\s*/, '').replace(/^'(.*)'$/, '$1').trim()));
    insertItems('ssh_authorized_keys', want.filter(k => !present.has(k)).map(quote));
    if (agent) {
      if (!present.has('qemu-guest-agent')) insertItems('packages', ['qemu-guest-agent']);
      const start = 'systemctl enable --now qemu-guest-agent';
      if (!lines.some(l => l.includes(start))) insertItems('runcmd', [quote(start)]);
    }
    return { text: lines.join('\n'), skipped: false };
  }

  return { CI_USER_SCHEMA, CI_NET_SCHEMA, genUserData, genNetworkData, userSchema,
           renderWizard, wireWizard, withKeysAndAgent, SSH_KEY_RE };
})();

if (typeof window !== 'undefined') window.CloudInitForm = CloudInitForm;
