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
};

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
