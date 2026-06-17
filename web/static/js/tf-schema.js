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
          { name: 'network_name', type: 'ref', required: false,
            ref_endpoint: '/api/networks', creatable: true,
            ref_namespaced: true,
            description: {
              en: 'NetworkAttachmentDefinition (leave empty for management).',
              fr: 'NetworkAttachmentDefinition (vide = management).',
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
          { name: 'type', type: 'enum', default: 'nocloud',
            enum_values: ['nocloud', 'configdrive'] },
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
};

// Expose globally for terraform.js, tf-form.js, and tests
window.TF_SCHEMA = TF_SCHEMA;
