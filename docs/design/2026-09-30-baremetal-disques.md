# Disques d'une installation bare-metal : choisir, vérifier, répartir (1.78.0)

## Le besoin

Choisir les disques d'une machine nue est la partie la plus difficile à
prédire d'une installation :

- `sda`, `vda`, `nvme0n1` dépendent du pilote et de l'ordre de détection, qui
  peut changer d'un démarrage à l'autre ; seuls les chemins stables
  (`/dev/disk/by-id/...`, `/dev/disk/by-path/...`) désignent sûrement le même
  disque, et l'installeur les résout (`EvalSymlinks`, `pkg/config/cos.go`,
  `pkg/console/validator.go`) ;
- le BMC ne voit pas toujours les disques : l'iDRAC 9 publie modèle, taille,
  série, média, contrôleur et volumes ; l'iLO 4 des XL170r n'en publie aucun
  (constaté, `_bmc_storage`) ;
- derrière une carte RAID, ce que Linux appelle `sda` est un volume logique ;
- un exploitant veut plusieurs pools de disques de données (niveaux de
  stockage), ce que l'installeur de Harvester amont ne sait pas faire (un
  seul `data_disk`).

## Décisions (validées le 30/09/2026)

| # | Sujet | Retenu |
|---|---|---|
| 1 | Désigner les disques | Liste de choix par caractéristiques ; la console écrit le chemin stable ; saisie libre en repli |
| 2 | BMC qui ne publie pas les disques | Démarrage de découverte : l'ISO démarre, renvoie l'inventaire vu par Linux, s'éteint |
| 3 | Pools | Créés par la console juste après l'installation, avec les primitives existantes (tout Harvester) ; `dataDiskPools` de l'installeur le jour où il sera dans Harvester amont |
| 4 | RAID matériel | Lecture seule : contrôleur, mode, volumes, conseils ; pas de création (aucun banc pour la vérifier) |
| 5 | Effacement | `wipe_disks_list` en plus de « tout effacer », avertissement sur les disques qui portent déjà des partitions |
| 6 | Banc | `bmcfg` sur node2, gardé pour ce chantier |

## Ce que l'essai réel a déjà appris (banc node2, ISO v1.9.0)

Un démarrage de découverte a été fait à la main avant cette conception :
la ligne de commande noyau porte
`systemd.run="/bin/bash -c 'test -e /etc/initrd-release || curl ... | bash'"`,
avec `systemd.run_success_action=none` ; le script récupéré lance `lsblk`,
lit `/dev/disk/by-id` et `/dev/disk/by-path` et `ip -j link`, POSTe le tout
puis fait `systemctl poweroff`. Résultat : inventaire reçu en 105 s, machine
éteinte d'elle-même. Quatre vérités payées :

1. `systemd.run=` s'exécute aussi DANS l'initrd (le générateur tourne avant
   et après la bascule de racine) : sans le garde `/etc/initrd-release`, la
   commande « réussit » à vide dans l'initrd (pas de curl) et éteint la
   machine avant le vrai système.
2. Les guillemets de la ligne de commande sont fragiles : `virt-install
   --boot kernel_args=` les retire (l'essai est passé par le XML du
   domaine), et grub a ses propres règles d'échappement. D'où le choix de
   ne pas en avoir besoin (section 2).
3. Le système live lance `multipathd`, qui prend les disques SATA et SCSI :
   des liens `by-id` (dont `wwn-...`) mènent à `dm-0`, pas au disque. La
   console ne propose jamais un lien vers un `dm-*`.
4. Une console série de VM écrite directement dans un fichier bloquait le
   démarrage de l'ISO ; `--serial pty,log.file=` convient (banc seulement).

## Conception

### 1. Inventaire des disques d'une machine

Deux sources, gardées côté console par machine (clé : numéro de série du
système lu par Redfish), avec leur date :

- **Redfish** (déjà lu par `_bmc_storage`) : contrôleurs, mode (RAID ou
  pass-through), disques physiques (modèle, taille, série, média,
  identifiants durables), volumes logiques. Ne donne pas le nom Linux.
- **Découverte** : ce que Linux voit (disques et volumes logiques, taille,
  modèle, série, WWN, rotation, bus, partitions et systèmes de fichiers
  existants, tous les liens `by-id`/`by-path` et leur cible), plus les
  cartes réseau (nom, MAC, état du lien, vitesse).

La fenêtre dit d'où vient ce qu'elle montre, et depuis quand.

### 2. Démarrage de découverte

Nouvelle action suivie `baremetal-discover:<hôte>`, calquée sur
l'installation : remasterisation de l'ISO en y déposant le script de
découverte (avec l'adresse et le jeton de dépôt, rien d'autre), et une
ligne de commande noyau sans `harvester.install.*` qui ne porte qu'un
chemin, sans espace ni guillemet :
`systemd.run=<point de montage du média>/discover.sh
systemd.run_success_action=none`. Le script sort tout de suite dans
l'initrd (`/etc/initrd-release`) et éteint la machine lui-même une fois
l'inventaire déposé. Le point de montage du média dans le système live est
à confirmer au premier essai (`/run/initramfs/live` pour un live dracut) ;
le banc démarre pour cela l'ISO remasterisée comme un vrai CD, en UEFI,
pas en démarrage direct du noyau. Publication
par le serveur d'artefacts (`pxe_server.py`) avec deux jetons à usage unique
(le script, le dépôt de l'inventaire ; un POST refusé au-delà de 1 Mio),
insertion en média virtuel, démarrage unique sur le CD, mise sous tension,
attente de l'inventaire (15 min au plus), vérification de l'extinction,
éjection. Le script envoyé ne porte aucun secret. L'ISO de découverte est
mise en cache à côté de l'ISO d'origine (même empreinte = pas de nouvelle
remasterisation).

### 3. Chemin stable écrit par la console

Pour chaque disque vu par la découverte, un seul chemin proposé, dans cet
ordre : `by-path` (emplacement PCI, stable tant que le câblage ne change
pas), sinon un `by-id` qui mène au disque lui-même (`nvme-eui.*`,
`nvme-<modèle>_<série>`, `wwn-*`, `ata-*`, `scsi-*`), jamais un lien vers un
`dm-*`, jamais un `sdX` nu sauf s'il n'existe rien d'autre (et la fenêtre le
dit). Sans découverte, un disque Redfish est proposé avec son numéro de
série et un chemin `by-id` déduit marqué « à confirmer ».

### 4. La fenêtre

Le cadre Disques devient un tableau des disques (taille, modèle, série,
média, bus, contrôleur, partitions existantes) avec un rôle par disque :
système, données (disque Longhorn par défaut), pool `<étiquette>`, effacer,
ignorer. Boutons « Lire les disques » (Redfish) et « Démarrage de
découverte ». La saisie libre du disque système reste possible. Le cadre
RAID montre contrôleur, mode et volumes, et un conseil (volume miroir pour
le système, disques de données en pass-through).

### 5. Contrôles avant installation (serveur)

- un disque système, au plus un disque de données, chaque disque dans un
  seul rôle ;
- tailles : 250 Gio pour un disque système seul, 180 Gio si un disque de
  données est posé, 50 Gio pour un disque de données ou de pool (valeurs de
  l'installeur v1.9, `SingleDiskMinSizeGiB`, `MultipleDiskMinSizeGiB`,
  `HardMinDataDiskSizeGiB`) ; `skipchecks` les lève, et la fenêtre le dit ;
- un disque qui porte des partitions ou un système de fichiers n'est
  accepté pour un rôle qu'avec l'effacement coché pour lui
  (`wipe_disks_list`) ou « tout effacer » ;
- une étiquette de pool : minuscules, chiffres et `-`.

### 6. Pools après l'installation

Les pools ne vont pas à l'installeur. Après l'étape « l'API répond », une
étape `pools` :

- retrouve chaque disque de pool parmi les BlockDevices de node-disk-manager
  du nœud, par série ou WWN (le nom noyau peut avoir changé), et refuse
  d'avancer s'il ne le trouve pas ;
- le provisionne dans Longhorn formaté, avec l'étiquette du pool
  (`hv_host.disk_add` + `lh_node_patch`, déjà utilisés par la vue Hôtes) ;
- crée une classe de stockage `longhorn-<étiquette>` par pool
  (`diskSelector: <étiquette>`, répliques au choix, 1 par défaut sur un
  nœud seul), si elle n'existe pas ;
- pour un nœud qui rejoint un cluster, seulement le provisionnement (les
  classes existent déjà).

### 7. En ligne de commande

`harvester-resources host disk-add` existe ; ajouter l'option d'étiquette si
elle manque, et une commande `pools-apply` qui prend la description des
pools en JSON, pour qu'un pipeline refasse la même chose.

## Vérification

- **Tests** : choix du chemin stable (multipath, NVMe, sans lien), lecture
  de l'inventaire (fichier réel du banc, anonymisé), contrôles de rôles et de
  tailles, jetons de découverte (usage unique, taille maximale), étape pools
  (BlockDevices simulés, correspondance par série), e2e de la fenêtre.
- **En réel sur le banc** : une VM à cinq disques de bus variés (virtio,
  SATA, SCSI, NVMe, un disque déjà partitionné) : démarrage de découverte par
  le chemin de la console (média virtuel remplacé par le démarrage direct du
  banc), installation avec disque système par chemin stable, disque de
  données, deux pools, puis vérification : disques Longhorn étiquetés,
  classes présentes, un volume de chaque classe posé sur le bon disque ;
  refus d'un disque partitionné sans effacement.
- **Sur une lame physique**, sur accord : découverte de node4 (iLO 4, lame
  vide) par le vrai média virtuel, pour vérifier que l'inventaire remonte là
  où Redfish n'a rien.
- **Non vérifiable ici** : création de volumes RAID (pas faite), inventaire
  Redfish d'un contrôleur RAID (seul le HBA de node5 est disponible).

## Hors périmètre

- Création de volumes RAID par Redfish.
- Profils multi-nœuds à variables (chantier déjà reporté).
