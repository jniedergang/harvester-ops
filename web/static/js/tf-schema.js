/**
 * harvester-ops — Terraform resource schema (v1.4.36, Phase A)
 *
 * Single source of truth for which Terraform resources we expose in the
 * Automation > Terraform UI, and what arguments each one carries. Read
 * by tf-form.js (renderer) and tf-form.js readForm() (extractor).
 *
 * Adding a new resource:
 *   1. Add an entry under TF_SCHEMA.<kind>
 *   2. Add a handler in web/app.py (/api/terraform/<cluster>/apply
 *      dispatches on `kind`)
 *   3. (optional) add ref_endpoint dropdowns + a creatable: true flag
 *      to enable inline-create from another resource's form
 *
 * Argument types:
 *   text     — <input type="text">
 *   int      — <input type="number"> with min/max
 *   bool     — <input type="checkbox">
 *   enum     — <select> populated from enum_values
 *   ref      — <select> populated from GET <ref_endpoint>/<cluster>
 *   textarea — <textarea> (for YAML / cloud-init / HCL)
 *
 * Cross-resource references (ref):
 *   ref_endpoint     — '/api/<list>' (no trailing slash, no cluster suffix —
 *                      the renderer appends '/<currentCluster>')
 *   ref_value_field  — JSON key to use as the option's value (default 'name')
 *   ref_label_field  — JSON key to use as the option's display label
 *                      (default = ref_value_field)
 *   ref_namespaced   — if true, "<namespace>/<name>" is used as value
 *   creatable        — if true, a "+" button next to the dropdown opens a
 *                      mini-form for inline create (Phase B)
 *   multiple         — if true, render a multi-select / chip picker
 *
 * Nested blocks (Terraform repeated blocks like `disk { … } disk { … }`):
 *   nested.<key>.min/max — required range (renderer shows + Add / − Remove)
 *   nested.<key>.label   — i18n object for the section header
 *   nested.<key>.args    — same arg schema as top-level
 */

const TF_SCHEMA = {

  // ---------------------------------------------------------------------
  // harvester_virtualmachine — the headliner. Covers cpu/memory, disks
  // (image + storage class), networks (NAD), ssh_keys, cloudinit.
  // ---------------------------------------------------------------------
  vm: {
    label: { en: 'Virtual machine', fr: 'Machine virtuelle' },
    description: {
      en: 'A KubeVirt VM scheduled on the Harvester cluster.',
      fr: 'Une VM KubeVirt planifiée sur le cluster Harvester.',
    },
    // v1.5.0: sections drive the UI's button-strip on each resource
    // card. Click a button → FloatingPanel opens the form filtered to
    // that section's args (or nested block). Validation per section
    // sets the button color (green/red/grey).
    sections: [
      { id: 'specs', label: { en: 'Specs', fr: 'Specs' },
        args: ['name', 'namespace', 'cpu', 'memory', 'run_strategy',
               'hostname', 'efi', 'secure_boot', 'ssh_keys', 'description'] },
      { id: 'disks', label: { en: 'Disks', fr: 'Disques' },
        nested: 'disk' },
      { id: 'networks', label: { en: 'Networks', fr: 'Réseaux' },
        nested: 'network_interface' },
      { id: 'cloudinit', label: { en: 'Cloud-init', fr: 'Cloud-init' },
        nested: 'cloudinit' },
    ],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true,
        validate: /^[a-z0-9][-a-z0-9]{0,61}[a-z0-9]?$/,
        description: {
          en: 'DNS-1123 label (lowercase letters, digits, hyphens).',
          fr: 'Nom DNS-1123 (minuscules, chiffres, traits d\'union).',
        }
      },
      { name: 'namespace', type: 'ref', required: false, default: 'default',
        ref_endpoint: '/api/namespaces', creatable: true,
        description: {
          en: 'Kubernetes namespace the VM lives in.',
          fr: 'Namespace Kubernetes où vit la VM.',
        }
      },
      { name: 'cpu', type: 'int', required: false, default: 1, min: 1, max: 64,
        description: {
          en: 'Number of vCPUs.',
          fr: 'Nombre de vCPUs.',
        }
      },
      { name: 'memory', type: 'text', required: false, default: '2Gi',
        validate: /^[0-9]+(Mi|Gi|Ti)$/,
        description: {
          en: 'Memory size (Mi/Gi/Ti suffix).',
          fr: 'Taille mémoire (suffixe Mi/Gi/Ti).',
        }
      },
      { name: 'run_strategy', type: 'enum', required: false, default: 'RerunOnFailure',
        enum_values: ['Always', 'Manual', 'Halted', 'RerunOnFailure'],
        description: {
          en: 'How KubeVirt should keep the VM alive.',
          fr: 'Politique de relance de la VM par KubeVirt.',
        }
      },
      { name: 'hostname', type: 'text', required: false,
        description: {
          en: 'Optional hostname inside the guest (defaults to name).',
          fr: 'Hostname optionnel à l\'intérieur de la VM (défaut = name).',
        }
      },
      { name: 'efi', type: 'bool', required: false, default: false,
        description: {
          en: 'Boot in EFI mode instead of legacy BIOS.',
          fr: 'Démarrer en EFI au lieu du BIOS legacy.',
        }
      },
      { name: 'secure_boot', type: 'bool', required: false, default: false,
        description: {
          en: 'Enable secure boot (requires EFI).',
          fr: 'Active le secure boot (nécessite EFI).',
        }
      },
      { name: 'ssh_keys', type: 'ref', required: false, multiple: true,
        ref_endpoint: '/api/sshkeys', creatable: true,
        ref_namespaced: true,
        description: {
          en: 'Harvester SSH key(s) injected via cloud-init.',
          fr: 'Clé(s) SSH Harvester injectée(s) via cloud-init.',
        }
      },
      { name: 'description', type: 'textarea', required: false, rows: 2,
        description: {
          en: 'Free-text description for humans.',
          fr: 'Description libre.',
        }
      },
    ],
    nested: {
      disk: {
        min: 1, max: 8,
        label: { en: 'Disks', fr: 'Disques' },
        args: [
          { name: 'name', type: 'text', required: true, default: 'rootdisk' },
          { name: 'type', type: 'enum', default: 'disk',
            enum_values: ['disk', 'cd-rom'] },
          { name: 'bus', type: 'enum', default: 'virtio',
            enum_values: ['virtio', 'sata', 'scsi'] },
          { name: 'size', type: 'text', default: '20Gi',
            validate: /^[0-9]+(Mi|Gi|Ti)$/ },
          { name: 'boot_order', type: 'int', default: 1, min: 0, max: 64 },
          { name: 'image', type: 'ref', required: false,
            ref_endpoint: '/api/images', creatable: true,
            ref_namespaced: true,
            ref_label_field: 'display_name',
            description: {
              en: 'Source image (leave empty for blank data disk).',
              fr: 'Image source (vide = disque vierge).',
            }
          },
          { name: 'storage_class_name', type: 'ref', required: false,
            ref_endpoint: '/api/storageclasses',
            description: {
              en: 'Storage class (default: cluster default).',
              fr: 'Storage class (défaut : celle par défaut du cluster).',
            }
          },
          // v1.52.1 : sans lui, une VM détruite laissait ses disques
          // (volume de 10 Gi resté Bound, vu à l'audit du 26/09/2026).
          { name: 'auto_delete', type: 'bool', default: true,
            description: {
              en: 'Delete this disk when the VM is destroyed. Unchecked: the volume stays in Harvester.',
              fr: 'Supprimer ce disque quand la VM est détruite. Décoché : le volume reste dans Harvester.',
            }
          },
        ],
      },
      network_interface: {
        min: 1, max: 8,
        label: { en: 'Network interfaces', fr: 'Interfaces réseau' },
        args: [
          { name: 'name', type: 'text', required: true, default: 'nic-1' },
          { name: 'type', type: 'enum', default: 'bridge',
            enum_values: ['bridge', 'masquerade'] },
          { name: 'model', type: 'enum', default: 'virtio',
            enum_values: ['virtio', 'e1000', 'e1000e', 'ne2k_pco',
                          'pcnet', 'rtl8139'] },
          // Couplé au type, et c'est tout l'enjeu : le provider DÉDUIT le
          // type de ce champ (vide -> masquerade, renseigné -> bridge).
          // L'afficher « optionnel » à côté d'un type `bridge` laissait
          // fabriquer la seule combinaison que le provider ne produit
          // jamais seul : un bridge sans réseau où l'attacher.
          { name: 'network_name', type: 'ref', required: false,
            required_when: { field: 'type', equals: 'bridge' },
            ref_endpoint: '/api/networks', creatable: true,
            ref_namespaced: true,
            description: {
              en: 'Where the interface is bridged. Required for type=bridge; leave empty for masquerade, which uses the management network.',
              fr: "Réseau sur lequel l'interface est bridgée. Obligatoire si type=bridge ; vide pour masquerade, qui utilise le réseau de management.",
            }
          },
          { name: 'wait_for_lease', type: 'bool', default: false,
            description: {
              en: 'Wait for a DHCP lease before reporting Ready.',
              fr: 'Attendre un lease DHCP avant de signaler Ready.',
            }
          },
        ],
      },
      cloudinit: {
        // v1.4.38: enforce min:1 so the cloud-init block is always
        // present. Without it KubeVirt boots the image bare and the
        // user lands on a SLES install menu / unprovisioned tty.
        min: 1, max: 1,
        label: { en: 'Cloud-init', fr: 'Cloud-init' },
        args: [
          // v1.52.1 : les valeurs exactes du provider (noCloud,
          // configDrive, constantes de harvester/pkg/builder). Les
          // minuscules d'avant faisaient refuser toute VM au plan.
          { name: 'type', type: 'enum', default: 'noCloud',
            enum_values: ['noCloud', 'configDrive'] },
          { name: 'user_data', type: 'textarea', rows: 8,
            default:
              '#cloud-config\n' +
              '# SUSE / openSUSE cloud images expect this header on line 1.\n' +
              '# ssh_keys = [...] on the VM resource are injected automatically;\n' +
              '# add packages, users, write_files, runcmd as needed.\n' +
              'hostname: my-vm\n' +
              'package_update: true\n' +
              'runcmd:\n' +
              '  - echo "Provisioned by harvester-ops"\n',
            description: {
              en: 'Inline cloud-init user-data YAML. Required for unattended boot.',
              fr: 'YAML cloud-init user-data inline. Indispensable pour un boot non-interactif.',
            }
          },
          { name: 'network_data', type: 'textarea', rows: 4,
            description: {
              en: 'Inline cloud-init network-data YAML (DHCP by default — leave blank).',
              fr: 'YAML cloud-init network-data inline (DHCP par défaut — laisser vide).',
            }
          },
          { name: 'user_data_secret_name', type: 'ref',
            ref_endpoint: '/api/cloudinits', creatable: true,
            ref_namespaced: true,
            description: {
              en: 'Use an existing Secret instead of inline user-data.',
              fr: 'Utilise un Secret existant au lieu de user-data inline.',
            }
          },
        ],
      },
    },
  },

  // ---------------------------------------------------------------------
  // harvester_image — source for VM root disks
  // ---------------------------------------------------------------------
  image: {
    label: { en: 'VM image', fr: 'Image VM' },
    description: {
      en: 'A bootable image (qcow2 / raw / ISO) registered with Harvester.',
      fr: 'Image bootable (qcow2 / raw / ISO) enregistrée auprès de Harvester.',
    },
    sections: [
      { id: 'specs', label: { en: 'Specs', fr: 'Specs' },
        args: ['name', 'namespace', 'display_name', 'source_type',
               'url', 'storage_class_name', 'checksum'] },
    ],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true,
        validate: /^[a-z0-9][-a-z0-9]{0,61}[a-z0-9]?$/,
        description: {
          en: 'Internal name (DNS-1123).',
          fr: 'Nom interne (DNS-1123).',
        }
      },
      { name: 'namespace', type: 'ref', required: false, default: 'default',
        ref_endpoint: '/api/namespaces', creatable: true },
      { name: 'display_name', type: 'text', required: true, force_new: true,
        description: {
          en: 'Display name shown in the Harvester UI and in dropdowns.',
          fr: 'Nom d\'affichage visible dans l\'UI Harvester et les dropdowns.',
        }
      },
      { name: 'source_type', type: 'enum', required: true, default: 'download',
        enum_values: ['download', 'upload', 'export_volume', 'clone'],
        force_new: true,
        description: {
          en: 'Where Harvester pulls the image from.',
          fr: 'D\'où Harvester récupère l\'image.',
        }
      },
      { name: 'url', type: 'text', required: false, force_new: true,
        description: {
          en: 'HTTPS URL of the qcow2/raw/ISO (source_type=download only).',
          fr: 'URL HTTPS du qcow2/raw/ISO (uniquement source_type=download).',
        }
      },
      { name: 'storage_class_name', type: 'ref', required: false,
        ref_endpoint: '/api/storageclasses', force_new: true },
      { name: 'checksum', type: 'text', required: false, force_new: true,
        description: {
          en: 'SHA-512 checksum (optional, integrity check).',
          fr: 'Somme SHA-512 (optionnel, contrôle d\'intégrité).',
        }
      },
    ],
  },

  // ---------------------------------------------------------------------
  // harvester_ssh_key — public key reusable across VMs
  // ---------------------------------------------------------------------
  ssh_key: {
    label: { en: 'SSH key', fr: 'Clé SSH' },
    description: {
      en: 'A public key Harvester can inject into VMs via cloud-init.',
      fr: 'Une clé publique injectable dans les VMs via cloud-init.',
    },
    sections: [
      { id: 'specs', label: { en: 'Specs', fr: 'Specs' },
        args: ['name', 'namespace', 'public_key'] },
    ],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true,
        validate: /^[a-z0-9][-a-z0-9]{0,61}[a-z0-9]?$/ },
      { name: 'namespace', type: 'ref', required: false, default: 'default',
        ref_endpoint: '/api/namespaces', creatable: true },
      { name: 'public_key', type: 'textarea', required: true, rows: 4,
        validate: /^(ssh-(rsa|ed25519|dss|ecdsa)|ecdsa-sha2-) /,
        description: {
          en: 'Full public key in OpenSSH format (ssh-rsa AAA…, ssh-ed25519 AAA…).',
          fr: 'Clé publique complète en format OpenSSH (ssh-rsa AAA…, ssh-ed25519 AAA…).',
        }
      },
    ],
  },

  // raw HCL escape hatch — kept for parity with the legacy form
  raw: {
    label: { en: 'Raw HCL', fr: 'HCL brut' },
    description: {
      en: 'Free-form Terraform HCL (advanced — bypasses the schema).',
      fr: 'HCL Terraform libre (avancé — court-circuite le schéma).',
    },
    sections: [
      { id: 'specs', label: { en: 'HCL', fr: 'HCL' },
        args: ['tf'] },
    ],
    args: [
      { name: 'tf', type: 'textarea', required: true, rows: 16,
        description: {
          en: 'Raw .tf content. Will be applied as-is.',
          fr: 'Contenu .tf brut. Appliqué tel quel.',
        }
      },
    ],
  },
  // ---------------------------------------------------------------------
  // 2.3.0 : ressources des déclarations qui visent un hôte KVM (provider
  // dmacvicar/libvirt). `ref_url` porte {target} (l'hôte) et `ref_list_key`
  // la liste à lire dans la réponse des routes /api/kvm/hosts/<hôte>/...
  // ---------------------------------------------------------------------
  kvm_vm: {
    family: 'kvm',
    label: { en: 'Virtual machine (KVM)' },
    sections: [
      { id: 'specs', label: { en: 'General' },
        args: ['name', 'vcpu', 'cpu_mode', 'memory_mib', 'running', 'autostart', 'description'] },
      { id: 'disk', label: { en: 'Disk' }, args: ['pool', 'base_image', 'base_format', 'disk_gib'] },
      { id: 'network', label: { en: 'Network' }, args: ['network_type', 'network', 'bridge'] },
      { id: 'cloudinit', label: { en: 'First boot' }, args: ['ssh_key', 'user_data'] },
    ],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true, validate: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$/ },
      { name: 'vcpu', type: 'int', default: 1, min: 1, max: 64 },
      { name: 'cpu_mode', type: 'enum', required: true, default: 'host-model', enum_values: ['host-model', 'host-passthrough'] },
      { name: 'memory_mib', type: 'int', default: 1024, min: 64, max: 1048576 },
      { name: 'running', type: 'bool', default: true },
      { name: 'autostart', type: 'bool', default: false },
      { name: 'description', type: 'textarea', rows: 2 },
      { name: 'pool', type: 'ref', required: true, ref_url: '/api/kvm/hosts/{target}/pools', ref_list_key: 'pools' },
      { name: 'base_image', type: 'ref', ref_url: '/api/kvm/hosts/{target}/images', ref_list_key: 'images',
        ref_value_field: 'path', ref_label_field: 'volume' },
      { name: 'base_format', type: 'enum', required: true, default: 'qcow2', enum_values: ['qcow2', 'raw'] },
      { name: 'disk_gib', type: 'int', default: 10, min: 1, max: 65536 },
      { name: 'network_type', type: 'enum', required: true, default: 'network', enum_values: ['network', 'bridge'] },
      { name: 'network', type: 'ref', ref_url: '/api/kvm/hosts/{target}/networks', ref_list_key: 'networks',
        required_when: { field: 'network_type', equals: 'network' } },
      { name: 'bridge', type: 'text', validate: /^[a-zA-Z0-9._-]{1,15}$/,
        required_when: { field: 'network_type', equals: 'bridge' } },
      { name: 'ssh_key', type: 'textarea', rows: 2 },
      { name: 'user_data', type: 'textarea', rows: 8 },
    ],
  },

  kvm_volume: {
    family: 'kvm',
    label: { en: 'Volume (KVM)' },
    sections: [{ id: 'specs', label: { en: 'General' },
                 args: ['name', 'pool', 'capacity_gib', 'format', 'base_image', 'base_format'] }],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true, validate: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$/ },
      { name: 'pool', type: 'ref', required: true, ref_url: '/api/kvm/hosts/{target}/pools', ref_list_key: 'pools' },
      { name: 'capacity_gib', type: 'int', default: 10, min: 1, max: 65536 },
      { name: 'format', type: 'enum', required: true, default: 'qcow2', enum_values: ['qcow2', 'raw'] },
      { name: 'base_image', type: 'ref', ref_url: '/api/kvm/hosts/{target}/images', ref_list_key: 'images',
        ref_value_field: 'path', ref_label_field: 'volume' },
      { name: 'base_format', type: 'enum', required: true, default: 'qcow2', enum_values: ['qcow2', 'raw'] },
    ],
  },

  kvm_network: {
    family: 'kvm',
    label: { en: 'Network (KVM)' },
    sections: [{ id: 'specs', label: { en: 'General' },
                 args: ['name', 'mode', 'address', 'prefix', 'dhcp', 'dhcp_start', 'dhcp_end', 'autostart'] }],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true, validate: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$/ },
      { name: 'mode', type: 'enum', required: true, default: 'isolated', enum_values: ['isolated', 'nat', 'route'] },
      { name: 'address', type: 'text', validate: /^\d{1,3}(\.\d{1,3}){3}$/ },
      { name: 'prefix', type: 'int', default: 24, min: 8, max: 30 },
      { name: 'dhcp', type: 'bool', default: true },
      { name: 'dhcp_start', type: 'text', validate: /^\d{1,3}(\.\d{1,3}){3}$/ },
      { name: 'dhcp_end', type: 'text', validate: /^\d{1,3}(\.\d{1,3}){3}$/ },
      { name: 'autostart', type: 'bool', default: true },
    ],
  },

  kvm_rke2: {
    family: 'kvm',
    label: { en: 'RKE2 on libvirt VMs (template)' },
    sections: [
      { id: 'specs', label: { en: 'Cluster' }, args: ['name', 'servers', 'agents', 'rke2_channel', 'rke2_version'] },
      { id: 'machines', label: { en: 'Machines' }, args: ['vcpu', 'cpu_mode', 'memory_mib', 'disk_gib', 'pool', 'base_image', 'base_format'] },
      { id: 'network', label: { en: 'Network' }, args: ['network', 'address_start', 'prefix', 'gateway', 'dns'] },
      { id: 'access', label: { en: 'Access' }, args: ['ssh_user', 'ssh_key'] },
    ],
    args: [
      { name: 'name', type: 'text', required: true, force_new: true, validate: /^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$/ },
      { name: 'servers', type: 'int', default: 1, min: 1, max: 7 },
      { name: 'agents', type: 'int', default: 0, min: 0, max: 20 },
      { name: 'rke2_channel', type: 'text', default: 'stable', suggest: ['stable', 'latest'],
        validate: /^(stable|latest|testing|v1\.\d{2})$/ },
      { name: 'rke2_version', type: 'text', validate: /^v1\.\d{2}\.\d{1,2}\+rke2r\d{1,2}$/ },
      { name: 'vcpu', type: 'int', default: 2, min: 1, max: 64 },
      { name: 'cpu_mode', type: 'enum', required: true, default: 'host-model', enum_values: ['host-model', 'host-passthrough'] },
      { name: 'memory_mib', type: 'int', default: 4096, min: 2048, max: 1048576 },
      { name: 'disk_gib', type: 'int', default: 30, min: 10, max: 65536 },
      { name: 'pool', type: 'ref', required: true, ref_url: '/api/kvm/hosts/{target}/pools', ref_list_key: 'pools' },
      { name: 'base_image', type: 'ref', required: true, ref_url: '/api/kvm/hosts/{target}/images', ref_list_key: 'images',
        ref_value_field: 'path', ref_label_field: 'volume' },
      { name: 'base_format', type: 'enum', required: true, default: 'qcow2', enum_values: ['qcow2', 'raw'] },
      { name: 'network', type: 'ref', required: true, ref_url: '/api/kvm/hosts/{target}/networks', ref_list_key: 'networks' },
      { name: 'address_start', type: 'text', required: true, validate: /^\d{1,3}(\.\d{1,3}){3}$/ },
      { name: 'prefix', type: 'int', default: 24, min: 8, max: 30 },
      { name: 'gateway', type: 'text', required: true, validate: /^\d{1,3}(\.\d{1,3}){3}$/ },
      { name: 'dns', type: 'text' },
      { name: 'ssh_user', type: 'text', default: 'rke2', validate: /^[a-z_][a-z0-9_-]{0,31}$/ },
      { name: 'ssh_key', type: 'textarea', required: true, rows: 2 },
    ],
  },
};

// 2.3.0 : chaque type de ressource dit sa famille de cibles ; le code libre
// (`raw`) reste sur Harvester.
['vm', 'image', 'ssh_key', 'raw'].forEach((k) => { TF_SCHEMA[k].family = 'harvester'; });

// ---------------------------------------------------------------------------
// v1.55.0 : libellés lisibles et cinq langues pour tout le schéma (audit
// D14/D15 : champs affichés sous leur nom Terraform brut, textes seulement
// en anglais et en français, pas de bulle sur plusieurs champs). Clés :
// `kind`, `kind#section`, `kind.arg`, `kind.bloc` (libellé du bloc imbriqué),
// `kind.bloc.arg`. Valeurs : [en, fr, it, es, de] pour `label` et `description`.
// ---------------------------------------------------------------------------
const TF_SCHEMA_I18N = {
  'vm': {
    label: ['Virtual machine', 'Machine virtuelle', 'Macchina virtuale', 'Máquina virtual', 'Virtuelle Maschine'],
    description: ['A KubeVirt VM scheduled on the Harvester cluster.', 'Une VM KubeVirt planifiée sur le cluster Harvester.',
      'Una VM KubeVirt pianificata sul cluster Harvester.', 'Una VM KubeVirt planificada en el clúster Harvester.',
      'Eine KubeVirt-VM, die auf dem Harvester-Cluster eingeplant wird.'] },
  'vm#specs': { label: ['General', 'Général', 'Generale', 'General', 'Allgemein'] },
  'vm#disks': { label: ['Disks', 'Disques', 'Dischi', 'Discos', 'Festplatten'] },
  'vm#networks': { label: ['Networks', 'Réseaux', 'Reti', 'Redes', 'Netzwerke'] },
  'vm#cloudinit': { label: ['First boot (cloud-init)', 'Premier démarrage (cloud-init)', 'Primo avvio (cloud-init)',
    'Primer arranque (cloud-init)', 'Erster Start (cloud-init)'] },
  'vm.name': {
    label: ['Name', 'Nom', 'Nome', 'Nombre', 'Name'],
    description: ['Name of the VM in Harvester: lower case letters, digits and dashes.', 'Nom de la VM dans Harvester : minuscules, chiffres et tirets.',
      'Nome della VM in Harvester: minuscole, cifre e trattini.', 'Nombre de la VM en Harvester: minúsculas, cifras y guiones.',
      'Name der VM in Harvester: Kleinbuchstaben, Ziffern und Bindestriche.'] },
  'vm.namespace': {
    label: ['Namespace', 'Espace de noms', 'Namespace', 'Namespace', 'Namespace'],
    description: ['Harvester namespace the VM lives in.', "Espace de noms Harvester de la VM.",
      'Namespace Harvester in cui vive la VM.', 'Namespace de Harvester donde vive la VM.', 'Harvester-Namespace der VM.'] },
  'vm.cpu': {
    label: ['Processors', 'Processeurs', 'Processori', 'Procesadores', 'Prozessoren'],
    description: ['Number of virtual processors.', 'Nombre de processeurs virtuels.', 'Numero di processori virtuali.',
      'Número de procesadores virtuales.', 'Anzahl virtueller Prozessoren.'] },
  'vm.memory': {
    label: ['Memory', 'Mémoire', 'Memoria', 'Memoria', 'Arbeitsspeicher'],
    description: ['Memory of the VM, with a unit: 2Gi, 512Mi.', "Mémoire de la VM, avec son unité : 2Gi, 512Mi.",
      "Memoria della VM, con l'unità: 2Gi, 512Mi.", 'Memoria de la VM, con su unidad: 2Gi, 512Mi.',
      'Arbeitsspeicher der VM, mit Einheit: 2Gi, 512Mi.'] },
  'vm.run_strategy': {
    label: ['At start', 'Au démarrage', "All'avvio", 'Al arrancar', 'Beim Start'],
    description: ['How the VM is kept running: always, restarted after a failure, stopped, or left to you.',
      'Comment la VM est tenue en marche : toujours, relancée après une panne, arrêtée, ou laissée à vos soins.',
      "Come la VM viene mantenuta in esecuzione: sempre, riavviata dopo un guasto, ferma o lasciata all'utente.",
      'Cómo se mantiene la VM en marcha: siempre, relanzada tras un fallo, detenida o a su criterio.',
      'Wie die VM am Laufen gehalten wird: immer, nach einem Fehler neu gestartet, angehalten oder Ihnen überlassen.'] },
  'vm.hostname': {
    label: ['Host name', "Nom d'hôte", 'Nome host', 'Nombre de host', 'Hostname'],
    description: ['Host name inside the VM; the VM name when empty.', "Nom d'hôte dans la VM ; le nom de la VM s'il est vide.",
      "Nome host all'interno della VM; il nome della VM se vuoto.", 'Nombre de host dentro de la VM; el de la VM si está vacío.',
      'Hostname in der VM; leer bedeutet der Name der VM.'] },
  'vm.efi': {
    label: ['EFI firmware', 'Micrologiciel EFI', 'Firmware EFI', 'Firmware EFI', 'EFI-Firmware'],
    description: ['Boot in EFI mode instead of the legacy BIOS.', "Démarrer en EFI plutôt qu'avec l'ancien BIOS.",
      'Avviare in modalità EFI invece del BIOS legacy.', 'Arrancar en modo EFI en lugar del BIOS heredado.',
      'Im EFI-Modus statt mit dem alten BIOS starten.'] },
  'vm.secure_boot': {
    label: ['Secure boot', 'Démarrage sécurisé', 'Avvio protetto', 'Arranque seguro', 'Sicherer Start'],
    description: ['Secure boot; needs the EFI firmware.', 'Démarrage sécurisé ; demande le micrologiciel EFI.',
      'Avvio protetto; richiede il firmware EFI.', 'Arranque seguro; requiere el firmware EFI.',
      'Sicherer Start; erfordert die EFI-Firmware.'] },
  'vm.ssh_keys': {
    label: ['SSH keys', 'Clés SSH', 'Chiavi SSH', 'Claves SSH', 'SSH-Schlüssel'],
    description: ['Harvester SSH keys given to the default user of the image at first boot.',
      "Clés SSH Harvester données à l'utilisateur par défaut de l'image au premier démarrage.",
      "Chiavi SSH Harvester date all'utente predefinito dell'immagine al primo avvio.",
      'Claves SSH de Harvester entregadas al usuario por defecto de la imagen en el primer arranque.',
      'Harvester-SSH-Schlüssel für den Standardbenutzer des Images beim ersten Start.'] },
  'vm.description': {
    label: ['Description', 'Description', 'Descrizione', 'Descripción', 'Beschreibung'],
    description: ['Free text shown in Harvester.', 'Texte libre affiché dans Harvester.', 'Testo libero mostrato in Harvester.',
      'Texto libre mostrado en Harvester.', 'Freitext, in Harvester angezeigt.'] },
  'vm.disk': { label: ['Disks', 'Disques', 'Dischi', 'Discos', 'Festplatten'] },
  'vm.disk.name': {
    label: ['Disk name', 'Nom du disque', 'Nome del disco', 'Nombre del disco', 'Name der Festplatte'],
    description: ['Name of the disk inside the VM definition.', 'Nom du disque dans la définition de la VM.',
      'Nome del disco nella definizione della VM.', 'Nombre del disco en la definición de la VM.',
      'Name der Festplatte in der VM-Definition.'] },
  'vm.disk.type': {
    label: ['Type', 'Type', 'Tipo', 'Tipo', 'Typ'],
    description: ['A disk, or a CD-ROM drive.', 'Un disque, ou un lecteur de CD-ROM.', 'Un disco o un lettore CD-ROM.',
      'Un disco o una unidad de CD-ROM.', 'Eine Festplatte oder ein CD-ROM-Laufwerk.'] },
  'vm.disk.bus': {
    label: ['Bus', 'Bus', 'Bus', 'Bus', 'Bus'],
    description: ['virtio is the fastest; sata or scsi for systems without its drivers.',
      'virtio est le plus rapide ; sata ou scsi pour les systèmes sans ses pilotes.',
      'virtio è il più veloce; sata o scsi per i sistemi senza i suoi driver.',
      'virtio es el más rápido; sata o scsi para sistemas sin sus controladores.',
      'virtio ist am schnellsten; sata oder scsi für Systeme ohne dessen Treiber.'] },
  'vm.disk.size': {
    label: ['Size', 'Taille', 'Dimensione', 'Tamaño', 'Größe'],
    description: ['Size of the disk, with a unit: 20Gi.', 'Taille du disque, avec son unité : 20Gi.',
      "Dimensione del disco, con l'unità: 20Gi.", 'Tamaño del disco, con su unidad: 20Gi.', 'Größe der Festplatte, mit Einheit: 20Gi.'] },
  'vm.disk.boot_order': {
    label: ['Boot order', 'Ordre de démarrage', 'Ordine di avvio', 'Orden de arranque', 'Startreihenfolge'],
    description: ['1 boots first; 0 never boots from this disk.', 'Le 1 démarre en premier ; 0 ne démarre jamais sur ce disque.',
      '1 si avvia per primo; 0 non si avvia mai da questo disco.', 'El 1 arranca primero; 0 nunca arranca desde este disco.',
      '1 startet zuerst; 0 startet nie von dieser Festplatte.'] },
  'vm.disk.image': {
    label: ['Image', 'Image', 'Immagine', 'Imagen', 'Image'],
    description: ['Image the disk starts from; empty for a blank data disk.', "Image dont part le disque ; vide pour un disque de données vierge.",
      "Immagine da cui parte il disco; vuoto per un disco dati vuoto.", 'Imagen de la que parte el disco; vacío para un disco de datos en blanco.',
      'Image, von dem die Festplatte ausgeht; leer für eine leere Datenfestplatte.'] },
  'vm.disk.storage_class_name': {
    label: ['Storage class', 'Classe de stockage', 'Classe di archiviazione', 'Clase de almacenamiento', 'Speicherklasse'],
    description: ["For a blank disk; a disk made from an image takes the image's class.",
      "Pour un disque vierge ; un disque tiré d'une image prend la classe de l'image.",
      "Per un disco vuoto; un disco da immagine prende la classe dell'immagine.",
      'Para un disco en blanco; un disco creado desde una imagen toma la clase de la imagen.',
      'Für eine leere Festplatte; eine Festplatte aus einem Image übernimmt dessen Klasse.'] },
  'vm.disk.auto_delete': {
    label: ['Delete with the VM', 'Supprimé avec la VM', 'Eliminato con la VM', 'Eliminado con la VM', 'Mit der VM löschen'],
    description: ['Delete this disk when the VM is destroyed. Unchecked: the volume stays in Harvester.',
      'Supprimer ce disque quand la VM est détruite. Décoché : le volume reste dans Harvester.',
      'Eliminare questo disco quando la VM viene distrutta. Deselezionato: il volume resta in Harvester.',
      'Eliminar este disco cuando se destruye la VM. Sin marcar: el volumen se queda en Harvester.',
      'Diese Festplatte löschen, wenn die VM zerstört wird. Ohne Haken bleibt das Volume in Harvester.'] },
  'vm.network_interface': { label: ['Network interfaces', 'Interfaces réseau', 'Interfacce di rete', 'Interfaces de red', 'Netzwerkschnittstellen'] },
  'vm.network_interface.name': {
    label: ['Interface name', "Nom de l'interface", "Nome dell'interfaccia", 'Nombre de la interfaz', 'Name der Schnittstelle'],
    description: ['Name of the interface inside the VM definition.', "Nom de l'interface dans la définition de la VM.",
      "Nome dell'interfaccia nella definizione della VM.", 'Nombre de la interfaz en la definición de la VM.',
      'Name der Schnittstelle in der VM-Definition.'] },
  'vm.network_interface.type': {
    label: ['Connection', 'Raccordement', 'Collegamento', 'Conexión', 'Anbindung'],
    description: ['bridge: on a VM network (a Harvester network); masquerade: behind the node, on the management network.',
      "bridge : sur un réseau de VMs (un réseau Harvester) ; masquerade : derrière le nœud, sur le réseau de management.",
      'bridge: su una rete di VM (una rete Harvester); masquerade: dietro il nodo, sulla rete di gestione.',
      'bridge: en una red de VMs (una red de Harvester); masquerade: detrás del nodo, en la red de gestión.',
      'bridge: in einem VM-Netz (ein Harvester-Netz); masquerade: hinter dem Knoten, im Verwaltungsnetz.'] },
  'vm.network_interface.model': {
    label: ['Card model', 'Modèle de carte', 'Modello di scheda', 'Modelo de tarjeta', 'Kartenmodell'],
    description: ['virtio is the fastest; e1000 for systems without its drivers.', 'virtio est le plus rapide ; e1000 pour les systèmes sans ses pilotes.',
      'virtio è il più veloce; e1000 per i sistemi senza i suoi driver.', 'virtio es el más rápido; e1000 para sistemas sin sus controladores.',
      'virtio ist am schnellsten; e1000 für Systeme ohne dessen Treiber.'] },
  'vm.network_interface.network_name': {
    label: ['Network', 'Réseau', 'Rete', 'Red', 'Netzwerk'],
    description: ['Where the interface is bridged. Required for bridge; empty for masquerade, which uses the management network.',
      "Réseau sur lequel l'interface est bridgée. Obligatoire pour bridge ; vide pour masquerade, qui utilise le réseau de management.",
      "Rete su cui l'interfaccia è in bridge. Obbligatoria per bridge; vuota per masquerade, che usa la rete di gestione.",
      'Red a la que se une la interfaz. Obligatoria para bridge; vacía para masquerade, que usa la red de gestión.',
      'Netz, an das die Schnittstelle gebrückt wird. Pflicht für bridge; leer für masquerade, das das Verwaltungsnetz nutzt.'] },
  'vm.network_interface.wait_for_lease': {
    label: ['Wait for an address', 'Attendre une adresse', "Attendere un indirizzo", 'Esperar una dirección', 'Auf eine Adresse warten'],
    description: ['Wait until the interface has an address before calling the VM ready.', "Attendre que l'interface ait une adresse avant de dire la VM prête.",
      "Attendere che l'interfaccia abbia un indirizzo prima di dichiarare pronta la VM.", 'Esperar a que la interfaz tenga una dirección antes de dar la VM por lista.',
      'Warten, bis die Schnittstelle eine Adresse hat, bevor die VM als bereit gilt.'] },
  'vm.cloudinit': { label: ['First boot (cloud-init)', 'Premier démarrage (cloud-init)', 'Primo avvio (cloud-init)', 'Primer arranque (cloud-init)', 'Erster Start (cloud-init)'] },
  'vm.cloudinit.type': {
    label: ['Source', 'Source', 'Origine', 'Origen', 'Quelle'],
    description: ['How the VM reads its configuration: noCloud suits the usual Linux images.', 'Comment la VM lit sa configuration : noCloud convient aux images Linux courantes.',
      'Come la VM legge la sua configurazione: noCloud va bene per le immagini Linux comuni.', 'Cómo lee la VM su configuración: noCloud sirve para las imágenes Linux habituales.',
      'Wie die VM ihre Konfiguration liest: noCloud passt zu den üblichen Linux-Images.'] },
  'vm.cloudinit.user_data': {
    label: ['Configuration', 'Configuration', 'Configurazione', 'Configuración', 'Konfiguration'],
    description: ['cloud-init configuration (YAML starting with #cloud-config): packages, users, commands.',
      'Configuration cloud-init (YAML qui commence par #cloud-config) : paquets, utilisateurs, commandes.',
      'Configurazione cloud-init (YAML che inizia con #cloud-config): pacchetti, utenti, comandi.',
      'Configuración cloud-init (YAML que empieza por #cloud-config): paquetes, usuarios, comandos.',
      'cloud-init-Konfiguration (YAML, beginnend mit #cloud-config): Pakete, Benutzer, Befehle.'] },
  'vm.cloudinit.network_data': {
    label: ['Network configuration', 'Configuration réseau', 'Configurazione di rete', 'Configuración de red', 'Netzwerkkonfiguration'],
    description: ['cloud-init network configuration; empty for DHCP.', 'Configuration réseau cloud-init ; vide pour le DHCP.',
      'Configurazione di rete cloud-init; vuota per il DHCP.', 'Configuración de red cloud-init; vacía para DHCP.',
      'cloud-init-Netzwerkkonfiguration; leer für DHCP.'] },
  'vm.cloudinit.user_data_secret_name': {
    label: ['From a secret', "Depuis un secret", 'Da un secret', 'Desde un secreto', 'Aus einem Secret'],
    description: ['An existing Harvester cloud-init secret, instead of the configuration above.', 'Un secret cloud-init existant dans Harvester, au lieu de la configuration ci-dessus.',
      'Un secret cloud-init esistente in Harvester, al posto della configurazione sopra.', 'Un secreto cloud-init existente en Harvester, en lugar de la configuración de arriba.',
      'Ein vorhandenes cloud-init-Secret in Harvester statt der obigen Konfiguration.'] },
  'image': {
    label: ['VM image', 'Image de VM', 'Immagine VM', 'Imagen de VM', 'VM-Image'],
    description: ['A bootable image (qcow2, raw or ISO) registered in Harvester.', 'Une image amorçable (qcow2, raw ou ISO) enregistrée dans Harvester.',
      'Un\'immagine avviabile (qcow2, raw o ISO) registrata in Harvester.', 'Una imagen arrancable (qcow2, raw o ISO) registrada en Harvester.',
      'Ein bootfähiges Image (qcow2, raw oder ISO), in Harvester registriert.'] },
  'image#specs': { label: ['General', 'Général', 'Generale', 'General', 'Allgemein'] },
  'image.name': {
    label: ['Name', 'Nom', 'Nome', 'Nombre', 'Name'],
    description: ['Internal name: lower case letters, digits and dashes.', 'Nom interne : minuscules, chiffres et tirets.',
      'Nome interno: minuscole, cifre e trattini.', 'Nombre interno: minúsculas, cifras y guiones.', 'Interner Name: Kleinbuchstaben, Ziffern und Bindestriche.'] },
  'image.namespace': {
    label: ['Namespace', 'Espace de noms', 'Namespace', 'Namespace', 'Namespace'],
    description: ['Harvester namespace of the image.', "Espace de noms Harvester de l'image.", "Namespace Harvester dell'immagine.",
      'Namespace de Harvester de la imagen.', 'Harvester-Namespace des Images.'] },
  'image.display_name': {
    label: ['Display name', "Nom affiché", 'Nome visualizzato', 'Nombre visible', 'Anzeigename'],
    description: ['Name shown in Harvester and in the lists of the console.', 'Nom montré dans Harvester et dans les listes de la console.',
      'Nome mostrato in Harvester e negli elenchi della console.', 'Nombre mostrado en Harvester y en las listas de la consola.',
      'In Harvester und in den Listen der Konsole angezeigter Name.'] },
  'image.source_type': {
    label: ['Source', 'Source', 'Origine', 'Origen', 'Quelle'],
    description: ['Where Harvester gets the image: download from an address, upload, a volume, a clone.',
      "D'où Harvester tire l'image : téléchargement depuis une adresse, dépôt, un volume, un clone.",
      "Da dove Harvester prende l'immagine: download da un indirizzo, caricamento, un volume, un clone.",
      'De dónde obtiene Harvester la imagen: descarga desde una dirección, subida, un volumen, un clon.',
      'Woher Harvester das Image bezieht: Download von einer Adresse, Upload, ein Volume, ein Klon.'] },
  'image.url': {
    label: ['Address', 'Adresse', 'Indirizzo', 'Dirección', 'Adresse'],
    description: ['HTTPS address of the qcow2, raw or ISO file (download only).', 'Adresse HTTPS du fichier qcow2, raw ou ISO (téléchargement seulement).',
      'Indirizzo HTTPS del file qcow2, raw o ISO (solo download).', 'Dirección HTTPS del archivo qcow2, raw o ISO (solo descarga).',
      'HTTPS-Adresse der qcow2-, raw- oder ISO-Datei (nur Download).'] },
  'image.storage_class_name': {
    label: ['Storage class', 'Classe de stockage', 'Classe di archiviazione', 'Clase de almacenamiento', 'Speicherklasse'],
    description: ['Storage class of the disks made from this image.', 'Classe de stockage des disques tirés de cette image.',
      'Classe di archiviazione dei dischi creati da questa immagine.', 'Clase de almacenamiento de los discos creados desde esta imagen.',
      'Speicherklasse der aus diesem Image erzeugten Festplatten.'] },
  'image.checksum': {
    label: ['SHA-512 checksum', 'Somme SHA-512', 'Checksum SHA-512', 'Suma SHA-512', 'SHA-512-Prüfsumme'],
    description: ['Checked after the download (optional).', 'Vérifiée après le téléchargement (facultatif).', 'Verificato dopo il download (facoltativo).',
      'Se comprueba tras la descarga (opcional).', 'Nach dem Download geprüft (optional).'] },
  'ssh_key': {
    label: ['SSH key', 'Clé SSH', 'Chiave SSH', 'Clave SSH', 'SSH-Schlüssel'],
    description: ['A public key Harvester can give to VMs at first boot.', 'Une clé publique que Harvester peut donner aux VMs au premier démarrage.',
      'Una chiave pubblica che Harvester può dare alle VM al primo avvio.', 'Una clave pública que Harvester puede dar a las VMs en el primer arranque.',
      'Ein öffentlicher Schlüssel, den Harvester VMs beim ersten Start geben kann.'] },
  'ssh_key#specs': { label: ['General', 'Général', 'Generale', 'General', 'Allgemein'] },
  'ssh_key.name': {
    label: ['Name', 'Nom', 'Nome', 'Nombre', 'Name'],
    description: ['Name of the key in Harvester: lower case letters, digits and dashes.', 'Nom de la clé dans Harvester : minuscules, chiffres et tirets.',
      'Nome della chiave in Harvester: minuscole, cifre e trattini.', 'Nombre de la clave en Harvester: minúsculas, cifras y guiones.',
      'Name des Schlüssels in Harvester: Kleinbuchstaben, Ziffern und Bindestriche.'] },
  'ssh_key.namespace': {
    label: ['Namespace', 'Espace de noms', 'Namespace', 'Namespace', 'Namespace'],
    description: ['Harvester namespace of the key.', 'Espace de noms Harvester de la clé.', 'Namespace Harvester della chiave.',
      'Namespace de Harvester de la clave.', 'Harvester-Namespace des Schlüssels.'] },
  'ssh_key.public_key': {
    label: ['Public key', 'Clé publique', 'Chiave pubblica', 'Clave pública', 'Öffentlicher Schlüssel'],
    description: ['The whole public key in OpenSSH format (ssh-ed25519 AAAA..., ssh-rsa AAAA...).', 'La clé publique entière au format OpenSSH (ssh-ed25519 AAAA..., ssh-rsa AAAA...).',
      'La chiave pubblica intera in formato OpenSSH (ssh-ed25519 AAAA..., ssh-rsa AAAA...).', 'La clave pública completa en formato OpenSSH (ssh-ed25519 AAAA..., ssh-rsa AAAA...).',
      'Der vollständige öffentliche Schlüssel im OpenSSH-Format (ssh-ed25519 AAAA..., ssh-rsa AAAA...).'] },
  'raw': {
    label: ['Free code', 'Code libre', 'Codice libero', 'Código libre', 'Freier Code'],
    description: ['Terraform code written by hand, applied as is.', 'Code Terraform écrit à la main, appliqué tel quel.',
      'Codice Terraform scritto a mano, applicato così com\'è.', 'Código Terraform escrito a mano, aplicado tal cual.',
      'Von Hand geschriebener Terraform-Code, unverändert angewendet.'] },
  'raw#specs': { label: ['Code', 'Code', 'Codice', 'Código', 'Code'] },
  'raw.tf': {
    label: ['Terraform code', 'Code Terraform', 'Codice Terraform', 'Código Terraform', 'Terraform-Code'],
    description: ['One or more resource blocks. The resource takes the name of the first block.', 'Un ou plusieurs blocs resource. La ressource prend le nom du premier bloc.',
      'Uno o più blocchi resource. La risorsa prende il nome del primo blocco.', 'Uno o varios bloques resource. El recurso toma el nombre del primer bloque.',
      'Ein oder mehrere resource-Blöcke. Die Ressource übernimmt den Namen des ersten Blocks.'] },
  // 2.3.0 : ressources des déclarations KVM
  "kvm_vm": { label: ["Virtual machine (KVM)", "Machine virtuelle (KVM)", "Macchina virtuale (KVM)", "Máquina virtual (KVM)", "Virtuelle Maschine (KVM)"], description: ["A libvirt VM on the KVM host, with its disk and, if asked, its cloud-init disk.", "Une VM libvirt sur l'hôte KVM, avec son disque et, si demandé, son disque cloud-init.", "Una VM libvirt sull'host KVM, con il suo disco e, se richiesto, il suo disco cloud-init.", "Una VM libvirt en el host KVM, con su disco y, si se pide, su disco cloud-init.", "Eine libvirt-VM auf dem KVM-Host, mit ihrer Festplatte und auf Wunsch ihrer cloud-init-Festplatte."] },
  "kvm_vm#specs": { label: ["General", "Général", "Generale", "General", "Allgemein"] },
  "kvm_vm#disk": { label: ["Disk", "Disque", "Disco", "Disco", "Festplatte"] },
  "kvm_vm#network": { label: ["Network", "Réseau", "Rete", "Red", "Netzwerk"] },
  "kvm_vm#cloudinit": { label: ["First boot (cloud-init)", "Premier démarrage (cloud-init)", "Primo avvio (cloud-init)", "Primer arranque (cloud-init)", "Erster Start (cloud-init)"] },
  "kvm_vm.name": { label: ["Name", "Nom", "Nome", "Nombre", "Name"], description: ["Name of the VM in libvirt; its disk is named <name>-disk.qcow2.", "Nom de la VM dans libvirt ; son disque se nomme <nom>-disk.qcow2.", "Nome della VM in libvirt; il suo disco si chiama <nome>-disk.qcow2.", "Nombre de la VM en libvirt; su disco se llama <nombre>-disk.qcow2.", "Name der VM in libvirt; ihre Festplatte heißt <Name>-disk.qcow2."] },
  "kvm_vm.vcpu": { label: ["Processors", "Processeurs", "Processori", "Procesadores", "Prozessoren"], description: ["Number of virtual processors.", "Nombre de processeurs virtuels.", "Numero di processori virtuali.", "Número de procesadores virtuales.", "Anzahl virtueller Prozessoren."] },
  "kvm_vm.cpu_mode": { label: ["CPU model", "Modèle de processeur", "Modello di CPU", "Modelo de CPU", "CPU-Modell"], description: ["host-model: the closest model the host offers, migratable; host-passthrough: the host CPU as is. Recent systems (SLES 16, Leap 16) need one of them.", "host-model : le modèle le plus proche que propose l'hôte, migrable ; host-passthrough : le processeur de l'hôte tel quel. Les systèmes récents (SLES 16, Leap 16) en ont besoin.", "host-model: il modello più vicino offerto dall'host, migrabile; host-passthrough: la CPU dell'host così com'è. I sistemi recenti (SLES 16, Leap 16) ne hanno bisogno.", "host-model: el modelo más cercano que ofrece el host, migrable; host-passthrough: la CPU del host tal cual. Los sistemas recientes (SLES 16, Leap 16) lo necesitan.", "host-model: das nächstliegende Modell des Hosts, migrierbar; host-passthrough: die CPU des Hosts unverändert. Neuere Systeme (SLES 16, Leap 16) brauchen eines davon."] },
  "kvm_rke2.cpu_mode": { label: ["CPU model", "Modèle de processeur", "Modello di CPU", "Modelo de CPU", "CPU-Modell"], description: ["host-model: the closest model the host offers, migratable; host-passthrough: the host CPU as is. Recent systems (SLES 16, Leap 16) need one of them.", "host-model : le modèle le plus proche que propose l'hôte, migrable ; host-passthrough : le processeur de l'hôte tel quel. Les systèmes récents (SLES 16, Leap 16) en ont besoin.", "host-model: il modello più vicino offerto dall'host, migrabile; host-passthrough: la CPU dell'host così com'è. I sistemi recenti (SLES 16, Leap 16) ne hanno bisogno.", "host-model: el modelo más cercano que ofrece el host, migrable; host-passthrough: la CPU del host tal cual. Los sistemas recientes (SLES 16, Leap 16) lo necesitan.", "host-model: das nächstliegende Modell des Hosts, migrierbar; host-passthrough: die CPU des Hosts unverändert. Neuere Systeme (SLES 16, Leap 16) brauchen eines davon."] },
  "kvm_vm.memory_mib": { label: ["Memory (MiB)", "Mémoire (Mio)", "Memoria (MiB)", "Memoria (MiB)", "Arbeitsspeicher (MiB)"], description: ["Memory of the VM in MiB: 1024 = 1 GiB.", "Mémoire de la VM en Mio : 1024 = 1 Gio.", "Memoria della VM in MiB: 1024 = 1 GiB.", "Memoria de la VM en MiB: 1024 = 1 GiB.", "Arbeitsspeicher der VM in MiB: 1024 = 1 GiB."] },
  "kvm_vm.running": { label: ["Started", "Démarrée", "Avviata", "Arrancada", "Gestartet"], description: ["Start the VM once created.", "Démarrer la VM une fois créée.", "Avviare la VM una volta creata.", "Arrancar la VM una vez creada.", "Die VM nach dem Anlegen starten."] },
  "kvm_vm.autostart": { label: ["Start with the host", "Démarre avec l'hôte", "Si avvia con l'host", "Arranca con el host", "Mit dem Host starten"], description: ["Start the VM when the host boots.", "Démarrer la VM au démarrage de l'hôte.", "Avviare la VM all'avvio dell'host.", "Arrancar la VM cuando arranca el host.", "Die VM beim Start des Hosts starten."] },
  "kvm_vm.description": { label: ["Description", "Description", "Descrizione", "Descripción", "Beschreibung"], description: ["Free text kept in the libvirt definition.", "Texte libre gardé dans la définition libvirt.", "Testo libero conservato nella definizione libvirt.", "Texto libre guardado en la definición libvirt.", "Freitext in der libvirt-Definition."] },
  "kvm_vm.pool": { label: ["Pool", "Pool", "Pool", "Pool", "Pool"], description: ["Storage pool of the host that receives the disk.", "Pool de stockage de l'hôte qui reçoit le disque.", "Pool di archiviazione dell'host che riceve il disco.", "Pool de almacenamiento del host que recibe el disco.", "Speicherpool des Hosts, der die Festplatte aufnimmt."] },
  "kvm_vm.base_image": { label: ["Base image", "Image de base", "Immagine di base", "Imagen base", "Basis-Image"], description: ["Image of the host catalogue the disk starts from (copy on write); empty for a blank disk.", "Image du catalogue de l'hôte dont part le disque (copie sur écriture) ; vide pour un disque vierge.", "Immagine del catalogo dell'host da cui parte il disco (copia in scrittura); vuoto per un disco vuoto.", "Imagen del catálogo del host de la que parte el disco (copia en escritura); vacío para un disco en blanco.", "Image aus dem Katalog des Hosts, von dem die Festplatte ausgeht (Copy-on-Write); leer für eine leere Festplatte."] },
  "kvm_vm.base_format": { label: ["Base image format", "Format de l'image", "Formato dell'immagine", "Formato de la imagen", "Format des Images"], description: ["Format of the base image: qcow2 for cloud images.", "Format de l'image de base : qcow2 pour les images cloud.", "Formato dell'immagine di base: qcow2 per le immagini cloud.", "Formato de la imagen base: qcow2 para las imágenes cloud.", "Format des Basis-Images: qcow2 für Cloud-Images."] },
  "kvm_vm.disk_gib": { label: ["Disk size (GiB)", "Taille du disque (Gio)", "Dimensione del disco (GiB)", "Tamaño del disco (GiB)", "Festplattengröße (GiB)"], description: ["Size of the disk; at least the size of the base image.", "Taille du disque ; au moins celle de l'image de base.", "Dimensione del disco; almeno quella dell'immagine di base.", "Tamaño del disco; al menos el de la imagen base.", "Größe der Festplatte; mindestens die des Basis-Images."] },
  "kvm_vm.network_type": { label: ["Connection", "Raccordement", "Collegamento", "Conexión", "Anbindung"], description: ["network: a libvirt network of the host; bridge: a bridge of the host (br0...).", "network : un réseau libvirt de l'hôte ; bridge : un pont de l'hôte (br0...).", "network: una rete libvirt dell'host; bridge: un bridge dell'host (br0...).", "network: una red libvirt del host; bridge: un puente del host (br0...).", "network: ein libvirt-Netz des Hosts; bridge: eine Bridge des Hosts (br0...)."] },
  "kvm_vm.network": { label: ["Network", "Réseau", "Rete", "Red", "Netzwerk"], description: ["libvirt network of the host the VM is plugged into.", "Réseau libvirt de l'hôte où la VM est branchée.", "Rete libvirt dell'host a cui è collegata la VM.", "Red libvirt del host a la que se conecta la VM.", "libvirt-Netz des Hosts, an das die VM angeschlossen wird."] },
  "kvm_vm.bridge": { label: ["Bridge", "Pont", "Bridge", "Puente", "Bridge"], description: ["Bridge of the host for the bridge connection (br0...).", "Pont de l'hôte pour le raccordement bridge (br0...).", "Bridge dell'host per il collegamento bridge (br0...).", "Puente del host para la conexión bridge (br0...).", "Bridge des Hosts für die Anbindung bridge (br0...)."] },
  "kvm_vm.ssh_key": { label: ["SSH public key", "Clé SSH publique", "Chiave SSH pubblica", "Clave SSH pública", "Öffentlicher SSH-Schlüssel"], description: ["Given to the default user of the image when the configuration below is empty.", "Donnée à l'utilisateur par défaut de l'image quand la configuration ci-dessous est vide.", "Data all'utente predefinito dell'immagine quando la configurazione sotto è vuota.", "Se da al usuario por defecto de la imagen cuando la configuración de abajo está vacía.", "Wird dem Standardbenutzer des Images gegeben, wenn die Konfiguration unten leer ist."] },
  "kvm_vm.user_data": { label: ["Configuration", "Configuration", "Configurazione", "Configuración", "Konfiguration"], description: ["cloud-init configuration (YAML starting with #cloud-config), on a cloud-init disk.", "Configuration cloud-init (YAML qui commence par #cloud-config), sur un disque cloud-init.", "Configurazione cloud-init (YAML che inizia con #cloud-config), su un disco cloud-init.", "Configuración cloud-init (YAML que empieza por #cloud-config), en un disco cloud-init.", "cloud-init-Konfiguration (YAML, beginnend mit #cloud-config), auf einer cloud-init-Festplatte."] },
  "kvm_volume": { label: ["Volume (KVM)", "Volume (KVM)", "Volume (KVM)", "Volumen (KVM)", "Volume (KVM)"], description: ["A volume in a storage pool of the KVM host.", "Un volume dans un pool de stockage de l'hôte KVM.", "Un volume in un pool di archiviazione dell'host KVM.", "Un volumen en un pool de almacenamiento del host KVM.", "Ein Volume in einem Speicherpool des KVM-Hosts."] },
  "kvm_volume#specs": { label: ["General", "Général", "Generale", "General", "Allgemein"] },
  "kvm_volume.name": { label: ["Name", "Nom", "Nome", "Nombre", "Name"], description: ["File name of the volume in its pool (data.qcow2).", "Nom de fichier du volume dans son pool (data.qcow2).", "Nome del file del volume nel suo pool (data.qcow2).", "Nombre de archivo del volumen en su pool (data.qcow2).", "Dateiname des Volumes in seinem Pool (data.qcow2)."] },
  "kvm_volume.pool": { label: ["Pool", "Pool", "Pool", "Pool", "Pool"], description: ["Storage pool of the host that receives the disk.", "Pool de stockage de l'hôte qui reçoit le disque.", "Pool di archiviazione dell'host che riceve il disco.", "Pool de almacenamiento del host que recibe el disco.", "Speicherpool des Hosts, der die Festplatte aufnimmt."] },
  "kvm_volume.capacity_gib": { label: ["Size (GiB)", "Taille (Gio)", "Dimensione (GiB)", "Tamaño (GiB)", "Größe (GiB)"], description: ["Size of the volume.", "Taille du volume.", "Dimensione del volume.", "Tamaño del volumen.", "Größe des Volumes."] },
  "kvm_volume.format": { label: ["Format", "Format", "Formato", "Formato", "Format"], description: ["qcow2 (grows as it fills) or raw.", "qcow2 (grandit en se remplissant) ou raw.", "qcow2 (cresce man mano) o raw.", "qcow2 (crece al llenarse) o raw.", "qcow2 (wächst beim Füllen) oder raw."] },
  "kvm_volume.base_image": { label: ["Base image", "Image de base", "Immagine di base", "Imagen base", "Basis-Image"], description: ["Image of the host catalogue the disk starts from (copy on write); empty for a blank disk.", "Image du catalogue de l'hôte dont part le disque (copie sur écriture) ; vide pour un disque vierge.", "Immagine del catalogo dell'host da cui parte il disco (copia in scrittura); vuoto per un disco vuoto.", "Imagen del catálogo del host de la que parte el disco (copia en escritura); vacío para un disco en blanco.", "Image aus dem Katalog des Hosts, von dem die Festplatte ausgeht (Copy-on-Write); leer für eine leere Festplatte."] },
  "kvm_volume.base_format": { label: ["Base image format", "Format de l'image", "Formato dell'immagine", "Formato de la imagen", "Format des Images"], description: ["Format of the base image: qcow2 for cloud images.", "Format de l'image de base : qcow2 pour les images cloud.", "Formato dell'immagine di base: qcow2 per le immagini cloud.", "Formato de la imagen base: qcow2 para las imágenes cloud.", "Format des Basis-Images: qcow2 für Cloud-Images."] },
  "kvm_network": { label: ["Network (KVM)", "Réseau (KVM)", "Rete (KVM)", "Red (KVM)", "Netzwerk (KVM)"], description: ["A libvirt network of the KVM host.", "Un réseau libvirt de l'hôte KVM.", "Una rete libvirt dell'host KVM.", "Una red libvirt del host KVM.", "Ein libvirt-Netz des KVM-Hosts."] },
  "kvm_network#specs": { label: ["General", "Général", "Generale", "General", "Allgemein"] },
  "kvm_network.name": { label: ["Name", "Nom", "Nome", "Nombre", "Name"], description: ["Name of the network in libvirt.", "Nom du réseau dans libvirt.", "Nome della rete in libvirt.", "Nombre de la red en libvirt.", "Name des Netzes in libvirt."] },
  "kvm_network.mode": { label: ["Mode", "Mode", "Modalità", "Modo", "Modus"], description: ["isolated: VMs only see each other; nat: they go out through the host; route: routed by the host.", "isolated : les VMs ne voient qu'elles-mêmes ; nat : elles sortent par l'hôte ; route : routées par l'hôte.", "isolated: le VM vedono solo sé stesse; nat: escono tramite l'host; route: instradate dall'host.", "isolated: las VMs solo se ven entre ellas; nat: salen por el host; route: enrutadas por el host.", "isolated: die VMs sehen nur einander; nat: sie gehen über den Host hinaus; route: vom Host geroutet."] },
  "kvm_network.address": { label: ["Host address", "Adresse de l'hôte", "Indirizzo dell'host", "Dirección del host", "Adresse des Hosts"], description: ["IPv4 address of the host on this network (the gateway of the VMs); required for nat and route.", "Adresse IPv4 de l'hôte sur ce réseau (la passerelle des VMs) ; obligatoire pour nat et route.", "Indirizzo IPv4 dell'host su questa rete (il gateway delle VM); obbligatorio per nat e route.", "Dirección IPv4 del host en esta red (la puerta de enlace de las VMs); obligatoria para nat y route.", "IPv4-Adresse des Hosts in diesem Netz (das Gateway der VMs); Pflicht für nat und route."] },
  "kvm_network.prefix": { label: ["Prefix", "Préfixe", "Prefisso", "Prefijo", "Präfix"], description: ["Prefix length of the subnet: 24 = 255.255.255.0.", "Longueur du préfixe du sous-réseau : 24 = 255.255.255.0.", "Lunghezza del prefisso della sottorete: 24 = 255.255.255.0.", "Longitud del prefijo de la subred: 24 = 255.255.255.0.", "Präfixlänge des Subnetzes: 24 = 255.255.255.0."] },
  "kvm_network.dhcp": { label: ["DHCP", "DHCP", "DHCP", "DHCP", "DHCP"], description: ["Hand out addresses to the VMs.", "Distribuer des adresses aux VMs.", "Distribuire indirizzi alle VM.", "Repartir direcciones a las VMs.", "Adressen an die VMs vergeben."] },
  "kvm_network.dhcp_start": { label: ["DHCP from", "DHCP de", "DHCP da", "DHCP desde", "DHCP von"], description: ["First address handed out; the tenth of the subnet when empty.", "Première adresse distribuée ; la dixième du sous-réseau si vide.", "Primo indirizzo distribuito; il decimo della sottorete se vuoto.", "Primera dirección repartida; la décima de la subred si está vacía.", "Erste vergebene Adresse; leer bedeutet die zehnte des Subnetzes."] },
  "kvm_network.dhcp_end": { label: ["DHCP to", "DHCP à", "DHCP a", "DHCP hasta", "DHCP bis"], description: ["Last address handed out; the last of the subnet when empty.", "Dernière adresse distribuée ; la dernière du sous-réseau si vide.", "Ultimo indirizzo distribuito; l'ultimo della sottorete se vuoto.", "Última dirección repartida; la última de la subred si está vacía.", "Letzte vergebene Adresse; leer bedeutet die letzte des Subnetzes."] },
  "kvm_network.autostart": { label: ["Start with the host", "Démarre avec l'hôte", "Si avvia con l'host", "Arranca con el host", "Mit dem Host starten"], description: ["Start the network when the host boots.", "Démarrer le réseau au démarrage de l'hôte.", "Avviare la rete all'avvio dell'host.", "Arrancar la red cuando arranca el host.", "Das Netz beim Start des Hosts starten."] },
  "kvm_rke2": { label: ["RKE2 on libvirt VMs (template)", "RKE2 sur des VMs libvirt (modèle)", "RKE2 su VM libvirt (modello)", "RKE2 en VMs libvirt (plantilla)", "RKE2 auf libvirt-VMs (Vorlage)"], description: ["N servers and M agents from a cloud image; cloud-init installs RKE2 (it needs Internet access from the VMs; airgap not covered).", "N serveurs et M agents tirés d'une image cloud ; cloud-init installe RKE2 (il faut un accès Internet depuis les VMs ; airgap non couvert).", "N server e M agent da un'immagine cloud; cloud-init installa RKE2 (serve l'accesso a Internet dalle VM; airgap non coperto).", "N servidores y M agentes desde una imagen cloud; cloud-init instala RKE2 (requiere acceso a Internet desde las VMs; airgap no cubierto).", "N Server und M Agents aus einem Cloud-Image; cloud-init installiert RKE2 (die VMs brauchen Internetzugang; Airgap nicht abgedeckt)."] },
  "kvm_rke2#specs": { label: ["Cluster", "Cluster", "Cluster", "Clúster", "Cluster"] },
  "kvm_rke2#machines": { label: ["Machines", "Machines", "Macchine", "Máquinas", "Maschinen"] },
  "kvm_rke2#network": { label: ["Network", "Réseau", "Rete", "Red", "Netzwerk"] },
  "kvm_rke2#access": { label: ["Access", "Accès", "Accesso", "Acceso", "Zugang"] },
  "kvm_rke2.name": { label: ["Prefix", "Préfixe", "Prefisso", "Prefijo", "Präfix"], description: ["Prefix of the node names: <prefix>-server-0, <prefix>-agent-0... Lower case letters, digits and dashes.", "Préfixe des noms des nœuds : <préfixe>-server-0, <préfixe>-agent-0... Minuscules, chiffres et tirets.", "Prefisso dei nomi dei nodi: <prefisso>-server-0, <prefisso>-agent-0... Minuscole, cifre e trattini.", "Prefijo de los nombres de los nodos: <prefijo>-server-0, <prefijo>-agent-0... Minúsculas, cifras y guiones.", "Präfix der Knotennamen: <Präfix>-server-0, <Präfix>-agent-0... Kleinbuchstaben, Ziffern und Bindestriche."] },
  "kvm_rke2.servers": { label: ["Servers", "Serveurs", "Server", "Servidores", "Server"], description: ["Control plane nodes: 1, 3, 5 or 7 (etcd needs a majority).", "Nœuds du plan de contrôle : 1, 3, 5 ou 7 (etcd a besoin d'une majorité).", "Nodi del piano di controllo: 1, 3, 5 o 7 (etcd ha bisogno di una maggioranza).", "Nodos del plano de control: 1, 3, 5 o 7 (etcd necesita una mayoría).", "Knoten der Steuerungsebene: 1, 3, 5 oder 7 (etcd braucht eine Mehrheit)."] },
  "kvm_rke2.agents": { label: ["Agents", "Agents", "Agent", "Agentes", "Agents"], description: ["Worker nodes, 0 to 20.", "Nœuds de travail, de 0 à 20.", "Nodi di lavoro, da 0 a 20.", "Nodos de trabajo, de 0 a 20.", "Arbeitsknoten, 0 bis 20."] },
  "kvm_rke2.rke2_channel": { label: ["RKE2 channel", "Canal RKE2", "Canale RKE2", "Canal RKE2", "RKE2-Kanal"], description: ["Release channel: stable, latest, or a minor version (v1.33).", "Canal de version : stable, latest, ou une version mineure (v1.33).", "Canale di rilascio: stable, latest o una versione minore (v1.33).", "Canal de versiones: stable, latest o una versión menor (v1.33).", "Release-Kanal: stable, latest oder eine Minor-Version (v1.33)."] },
  "kvm_rke2.rke2_version": { label: ["RKE2 version", "Version RKE2", "Versione RKE2", "Versión RKE2", "RKE2-Version"], description: ["Exact version (v1.33.5+rke2r1); takes precedence over the channel.", "Version exacte (v1.33.5+rke2r1) ; passe avant le canal.", "Versione esatta (v1.33.5+rke2r1); ha la precedenza sul canale.", "Versión exacta (v1.33.5+rke2r1); tiene prioridad sobre el canal.", "Genaue Version (v1.33.5+rke2r1); hat Vorrang vor dem Kanal."] },
  "kvm_rke2.vcpu": { label: ["Processors", "Processeurs", "Processori", "Procesadores", "Prozessoren"], description: ["Number of virtual processors.", "Nombre de processeurs virtuels.", "Numero di processori virtuali.", "Número de procesadores virtuales.", "Anzahl virtueller Prozessoren."] },
  "kvm_rke2.memory_mib": { label: ["Memory (MiB)", "Mémoire (Mio)", "Memoria (MiB)", "Memoria (MiB)", "Arbeitsspeicher (MiB)"], description: ["Memory of each node in MiB, 2048 at least (4096 advised for a server).", "Mémoire de chaque nœud en Mio, 2048 au moins (4096 conseillés pour un serveur).", "Memoria di ogni nodo in MiB, almeno 2048 (4096 consigliati per un server).", "Memoria de cada nodo en MiB, al menos 2048 (4096 aconsejados para un servidor).", "Arbeitsspeicher jedes Knotens in MiB, mindestens 2048 (4096 empfohlen für einen Server)."] },
  "kvm_rke2.disk_gib": { label: ["Disk size (GiB)", "Taille du disque (Gio)", "Dimensione del disco (GiB)", "Tamaño del disco (GiB)", "Festplattengröße (GiB)"], description: ["Size of the disk; at least the size of the base image.", "Taille du disque ; au moins celle de l'image de base.", "Dimensione del disco; almeno quella dell'immagine di base.", "Tamaño del disco; al menos el de la imagen base.", "Größe der Festplatte; mindestens die des Basis-Images."] },
  "kvm_rke2.pool": { label: ["Pool", "Pool", "Pool", "Pool", "Pool"], description: ["Storage pool of the host that receives the disk.", "Pool de stockage de l'hôte qui reçoit le disque.", "Pool di archiviazione dell'host che riceve il disco.", "Pool de almacenamiento del host que recibe el disco.", "Speicherpool des Hosts, der die Festplatte aufnimmt."] },
  "kvm_rke2.base_image": { label: ["Base image", "Image de base", "Immagine di base", "Imagen base", "Basis-Image"], description: ["Cloud image with cloud-init (openSUSE Leap, SLES, Ubuntu...) from the host catalogue.", "Image cloud avec cloud-init (openSUSE Leap, SLES, Ubuntu...) du catalogue de l'hôte.", "Immagine cloud con cloud-init (openSUSE Leap, SLES, Ubuntu...) dal catalogo dell'host.", "Imagen cloud con cloud-init (openSUSE Leap, SLES, Ubuntu...) del catálogo del host.", "Cloud-Image mit cloud-init (openSUSE Leap, SLES, Ubuntu...) aus dem Katalog des Hosts."] },
  "kvm_rke2.base_format": { label: ["Base image format", "Format de l'image", "Formato dell'immagine", "Formato de la imagen", "Format des Images"], description: ["Format of the base image: qcow2 for cloud images.", "Format de l'image de base : qcow2 pour les images cloud.", "Formato dell'immagine di base: qcow2 per le immagini cloud.", "Formato de la imagen base: qcow2 para las imágenes cloud.", "Format des Basis-Images: qcow2 für Cloud-Images."] },
  "kvm_rke2.network": { label: ["Network", "Réseau", "Rete", "Red", "Netzwerk"], description: ["libvirt network of the host the VM is plugged into.", "Réseau libvirt de l'hôte où la VM est branchée.", "Rete libvirt dell'host a cui è collegata la VM.", "Red libvirt del host a la que se conecta la VM.", "libvirt-Netz des Hosts, an das die VM angeschlossen wird."] },
  "kvm_rke2.address_start": { label: ["First address", "Première adresse", "Primo indirizzo", "Primera dirección", "Erste Adresse"], description: ["Static address of the first server; the next nodes take the following ones. Outside the DHCP range.", "Adresse fixe du premier serveur ; les nœuds suivants prennent les suivantes. Hors de la plage DHCP.", "Indirizzo statico del primo server; i nodi seguenti prendono i successivi. Fuori dalla gamma DHCP.", "Dirección fija del primer servidor; los nodos siguientes toman las siguientes. Fuera del rango DHCP.", "Feste Adresse des ersten Servers; die weiteren Knoten nehmen die folgenden. Außerhalb des DHCP-Bereichs."] },
  "kvm_rke2.prefix": { label: ["Prefix", "Préfixe", "Prefisso", "Prefijo", "Präfix"], description: ["Prefix length of the subnet: 24 = 255.255.255.0.", "Longueur du préfixe du sous-réseau : 24 = 255.255.255.0.", "Lunghezza del prefisso della sottorete: 24 = 255.255.255.0.", "Longitud del prefijo de la subred: 24 = 255.255.255.0.", "Präfixlänge des Subnetzes: 24 = 255.255.255.0."] },
  "kvm_rke2.gateway": { label: ["Gateway", "Passerelle", "Gateway", "Puerta de enlace", "Gateway"], description: ["Default route of the nodes (the host address on a nat network).", "Route par défaut des nœuds (l'adresse de l'hôte sur un réseau nat).", "Instradamento predefinito dei nodi (l'indirizzo dell'host su una rete nat).", "Ruta por defecto de los nodos (la dirección del host en una red nat).", "Standardroute der Knoten (die Adresse des Hosts in einem nat-Netz)."] },
  "kvm_rke2.dns": { label: ["DNS servers", "Serveurs DNS", "Server DNS", "Servidores DNS", "DNS-Server"], description: ["One to three addresses, comma separated; the gateway when empty.", "Une à trois adresses, séparées par des virgules ; la passerelle si vide.", "Da uno a tre indirizzi, separati da virgole; il gateway se vuoto.", "De una a tres direcciones, separadas por comas; la puerta de enlace si está vacío.", "Ein bis drei Adressen, durch Kommas getrennt; leer bedeutet das Gateway."] },
  "kvm_rke2.ssh_user": { label: ["User", "Utilisateur", "Utente", "Usuario", "Benutzer"], description: ["User created on each node, with sudo, for SSH.", "Utilisateur créé sur chaque nœud, avec sudo, pour SSH.", "Utente creato su ogni nodo, con sudo, per SSH.", "Usuario creado en cada nodo, con sudo, para SSH.", "Auf jedem Knoten angelegter Benutzer, mit sudo, für SSH."] },
  "kvm_rke2.ssh_key": { label: ["SSH public key", "Clé SSH publique", "Chiave SSH pubblica", "Clave SSH pública", "Öffentlicher SSH-Schlüssel"], description: ["One public key in OpenSSH format (ssh-ed25519 AAAA...).", "Une clé publique au format OpenSSH (ssh-ed25519 AAAA...).", "Una chiave pubblica in formato OpenSSH (ssh-ed25519 AAAA...).", "Una clave pública en formato OpenSSH (ssh-ed25519 AAAA...).", "Ein öffentlicher Schlüssel im OpenSSH-Format (ssh-ed25519 AAAA...)."] },
};

(function applySchemaI18n() {
  const LANGS = ['en', 'fr', 'it', 'es', 'de'];
  const obj = (arr) => Object.fromEntries(LANGS.map((l, i) => [l, arr[i]]));
  const put = (target, entry) => {
    if (!target || !entry) return;
    if (entry.label) target.label = obj(entry.label);
    if (entry.description) target.description = obj(entry.description);
  };
  Object.entries(TF_SCHEMA).forEach(([kind, sch]) => {
    put(sch, TF_SCHEMA_I18N[kind]);
    (sch.sections || []).forEach(sec => put(sec, TF_SCHEMA_I18N[`${kind}#${sec.id}`]));
    (sch.args || []).forEach(arg => put(arg, TF_SCHEMA_I18N[`${kind}.${arg.name}`]));
    Object.entries(sch.nested || {}).forEach(([nkey, ndef]) => {
      put(ndef, TF_SCHEMA_I18N[`${kind}.${nkey}`]);
      (ndef.args || []).forEach(arg => put(arg, TF_SCHEMA_I18N[`${kind}.${nkey}.${arg.name}`]));
    });
  });
})();

// Expose globally for terraform.js, tf-form.js, and tests
window.TF_SCHEMA = TF_SCHEMA;
