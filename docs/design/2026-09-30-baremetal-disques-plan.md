# Plan : disques d'une installation bare-metal (1.78.0)

Conception : `docs/design/2026-09-30-baremetal-disques.md`.

## Global Constraints

- Version cible 1.78.0 ; commits `feat(1.78.0): ...` / `fix(1.78.0): ...` en
  anglais, orientés pourquoi. VERSION et CHANGELOG bumpés à la release
  (tâche 5), pas avant.
- Aucune mention d'outil ni d'attribution dans les commits, le code, la doc.
  Pas de tiret cadratin ni de flèche Unicode dans tout contenu ajouté.
- Code et noms en anglais, commentaires et interface en français ; chaque
  chaîne d'interface dans les cinq langues (EN, FR, DE, ES, IT) de
  `web/static/js/i18n.js`.
- Vanilla JS, pas de dépendance nouvelle ; `escapeHtml` (`esc`) avant tout
  `.innerHTML` ; une info-bulle sur chaque contrôle.
- `@requires_auth` + `@_rate_limit("…")` (limite valide pour
  `limits.parse_many`) sur chaque nouvelle route ; toute opération mutative
  crée un ActionRun suivi dans le dock.
- Jamais de secret dans une réponse, un libellé d'action, un journal ; le
  script de découverte ne porte ni jeton de cluster ni mot de passe, seulement
  l'adresse et le jeton de dépôt à usage unique.
- Parité CLI/UI : la découverte et les pools ont leur commande en ligne ;
  l'UI ne contourne pas les outils `bin/`.
- Tailles minimales de l'installeur v1.9 : 250 Gio (disque système seul),
  180 Gio (système avec disque de données), 50 Gio (données ou pool) ;
  `skipchecks` les lève.
- Ordre du chemin stable : `by-path`, puis `by-id` vers le disque lui-même
  (`nvme-eui.*`, `nvme-*`, `wwn-*`, `ata-*`, `scsi-*`), jamais une cible
  `dm-*`, `sdX` nu seulement en dernier recours et signalé.
- Étiquette de pool : `^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$`.
- Aucune donnée client ni identifiant réel de matériel dans les fixtures :
  séries, WWN et MAC génériques.
- `python3 -m pytest tests/api/ -q` vert avant chaque commit.

### Task 1 : inventaire, chemin stable et contrôles des rôles

Fichiers : créer `web/baremetal_disks.py` ; tests
`tests/api/test_bm_disks_178.py` ; fixture
`tests/api/fixtures/bm_disks_178/discovery.txt` (inventaire au format du
script de découverte, disques virtio, SATA, SCSI, NVMe, un disque
partitionné, liens multipath vers `dm-*`, cartes réseau), anonymisée.

- `parse_discovery(text) -> dict` : `{"disks": [...], "nics": [...]}` ;
  disque = `name, size_bytes, model, serial, wwn, rotational, transport,
  type (disk|raid|...), partitions: [{name, fstype, size_bytes}],
  has_data (bool), links: {by_id: [...], by_path: [...]}, stable_path,
  stable_kind ("by-path"|"by-id"|"kernel")` ; carte = `name, mac, state,
  speed`. Format d'entrée : sections `== lsblk` (JSON de `lsblk -J -b -O`),
  `== links` (lignes `<lien> <cible>`), `== nics` (JSON de `ip -j link`),
  défini aussi par le script de la tâche 2 : c'est le contrat entre les deux.
- `stable_path(disk) -> (path, kind)` selon l'ordre des Global Constraints.
- `check_disk_roles(disks, roles, skipchecks, wipe_all) -> list[(path,
  reason)]` : `roles` = `{"os": id, "data": id|None, "pools": {tag:
  [ids]}, "wipe": [ids]}` (id = chemin stable) ; raisons : `role-twice`,
  `no-os`, `too-small:<Gio>`, `has-data` (partitions sans effacement),
  `pool-tag`, `unknown-disk`.
- `install_fields(disks, roles) -> dict` : `device`, `data_disk`,
  `wipe_disks_list` pour la configuration (fusion avec
  `harvester_install_schema.build_form_config` par les champs de formulaire
  existants `device`, `data_disk`, et un nouveau champ `wipe_disks_list`
  accepté par `build_form_config`).
- Tests : chaque ordre de chemin, multipath exclu, NVMe, rien de stable,
  rôles et tailles, `skipchecks`, `has-data`, étiquettes.

### Task 2 : démarrage de découverte

Fichiers : `bin/harvester-iso-remaster.sh` (option `--add-file
<src>:<chemin-dans-iso>:<mode>` et `--kernel-args "<...>"` qui remplace
l'appel zéro-touch ; garder l'usage actuel intact),
`web/pxe_server.py` (jeton de type `inventory` qui accepte un POST, 1 Mio
max, usage unique, fichier écrit en 0600), `web/app.py` (action
`baremetal-discover`, route `POST /api/baremetal/discover`, route `GET
/api/baremetal/inventory/<host>`, magasin des inventaires sous l'état de la
console, un fichier par série système), un script `bin/lib/discover.sh.tpl`
(gabarit du script déposé dans l'ISO). Tests
`tests/api/test_bm_discover_178.py`.

- Ligne de commande noyau : `systemd.run=/run/initramfs/live/discover.sh
  systemd.run_success_action=none systemd.run_failure_action=none` +
  arguments supplémentaires de l'opérateur, jamais `harvester.install.*`.
- Script : sortie immédiate si `/etc/initrd-release` existe ; sinon
  `lsblk -J -b -O`, liens `/dev/disk/by-id` et `by-path` (`readlink -f`),
  `ip -j link` ; envoi par `curl --retry 60 --retry-delay 5
  --retry-all-errors` ; puis `systemctl poweroff`.
- Étapes suivies : remaster (mis en cache par empreinte de l'ISO source +
  version du gabarit), serve, média virtuel, démarrage unique, mise sous
  tension, attente de l'inventaire (15 min), attente de l'extinction
  (5 min, puis extinction forcée signalée), éjection, analyse par
  `parse_discovery`, enregistrement. Annulable (`run._cancel`).
- En ligne de commande : `bin/harvester-baremetal.py discover` (ou la
  commande existante qui porte l'installation si elle existe : la réutiliser).
- Tests : jeton inventaire (usage unique, taille, type), gabarit sans
  secret, remasterisation avec fichier ajouté (mini-ISO fabriqué comme les
  tests existants du remaster), enregistrement et lecture de l'inventaire.

### Task 3 : pools après l'installation

Fichiers : `bin/lib/hv_host.py` (étiquettes de disque si absentes),
`bin/harvester-resources.py` (`host disk-add --tag`, `pools-apply --spec`),
`web/app.py` (étape `pools` du runner d'installation, après l'attente de
l'API ; champ `pools` accepté par la route), tests
`tests/api/test_bm_pools_178.py`.

- Correspondance BlockDevice : par série ou WWN
  (`status.deviceStatus.details`), jamais par nom noyau seul ; refus clair
  si introuvable.
- Provisionnement formaté + étiquette ; classe `longhorn-<tag>`
  (`diskSelector: <tag>`, `numberOfReplicas` fourni, 1 par défaut) créée si
  absente, jamais modifiée si elle existe avec un autre sélecteur (refus).
- Mode rejoindre : provisionnement seulement.
- Tests : BlockDevices simulés (fixtures), idempotence, refus, commande en
  ligne.

### Task 4 : fenêtre

Fichiers : `web/static/js/bmc.js`, `web/static/js/i18n.js`,
`web/static/css/style.css` si besoin, tests
`tests/e2e/test_bm_disks_178.py`.

- Cadre Disques : tableau (taille, modèle, série, média, bus, contrôleur,
  partitions, chemin stable) avec un rôle par disque (système, données,
  pool + étiquette, effacer, ignorer), source et date de l'inventaire,
  boutons « Lire les disques » (Redfish, déjà disponible par la découverte
  BMC) et « Démarrage de découverte » (action suivie, le tableau se
  remplit à la fin). Saisie libre du disque système conservée. Cadre RAID en
  lecture seule avec conseil. Refus des contrôles affichés par disque.
- Les cartes découvertes donnent leur nom Linux dans le choix de
  l'interface de gestion.
- Répliques par pool (1 par défaut).
- e2e : routes simulées, tableau rempli depuis un inventaire, rôles, refus
  `has-data` et `too-small`, corps envoyé (`device`, `data_disk`,
  `wipe_disks_list`, `pools`).

### Task 5 : vérification réelle, doc, release

(Faite par le contrôleur.)

- Banc : VM à cinq disques (virtio, SATA, SCSI, NVMe, un partitionné)
  démarrée sur l'ISO de découverte remasterisée comme un vrai CD UEFI ;
  inventaire reçu ; installation avec disque système par chemin stable,
  disque de données, deux pools ; vérification des disques Longhorn
  étiquetés, des classes, d'un volume par classe sur le bon disque ; refus
  d'un disque partitionné sans effacement.
- Sur accord de ju : découverte de node4 par le vrai média virtuel.
- Doc EN/FR (`docs/en|fr/bare-metal.md`, `capabilities`), parité,
  CHANGELOG, VERSION, suites complètes, paquet essayé par l'unité, Gitea
  puis GitHub, release.
