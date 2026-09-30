# Configuration d'installation bare-metal complète (1.77.0)

## Le besoin

Un exploitant déploie ses nœuds Harvester avec sa propre configuration
d'installation, produite par un générateur maison (une par nœud, valeurs
substituées). La fenêtre « Installer Harvester » de l'onglet Bare-metal n'en
couvre qu'une petite partie : nom d'hôte, un disque, UNE carte de gestion,
DHCP ou IP statique, VIP, DNS, NTP (1.76.1), jeton, mot de passe, clés SSH.
Sa configuration ne passe pas. Ce qui manque, relevé sur son fichier :

- interface de gestion en agrégat de deux cartes (802.3ad), avec
  `lacp_rate`, `xmit_hash_policy`, `miimon`, et un `vlan_id` ;
- `os.write_files` : les connexions NetworkManager des autres réseaux
  (agrégat de stockage en MTU 9000, VLAN, routes statiques), un réglage
  systemd de sshd, un `sshd_config.d` ;
- `install.data_disk`, `install.wipe_all_disks`, disque désigné par
  `/dev/disk/by-path/...` ;
- `os.persistent_state_paths`, `os.modules`, `os.labels` ;
- `system_settings` (`auto-disk-provision-paths`) ;
- son propre `iso_url` (miroir interne).

L'installeur de Harvester connaît tous ces champs (`pkg/config/config.go` :
`writeFiles`, `persistentStatePaths`, `modules`, `labels`, `sysctls`,
`environment`, `afterInstallChrootCommands`, `wipeAllDisks`, `vlanId`,
`bondOptions`, `systemSettings`). Rien ne bloque côté Harvester.

## Décisions (validées le 30/09/2026)

| # | Sujet | Retenu |
|---|---|---|
| 1 | Approche | Hybride : formulaire enrichi + section « YAML avancé » + import d'un fichier + aperçu du YAML final |
| 2 | Profils multi-nœuds à variables | Chantier suivant |
| 3 | `iso_url` du fichier importé | Toujours remplacée par l'ISO servie par la console, et la fenêtre le dit |
| 4 | Clé inconnue du schéma | Refusée, avec son chemin (une clé mal placée coûte une réinstallation : `server_url` sous `install` jusqu'en 1.44.1) |
| 5 | Auto-remplissage du navigateur, champ NTP | Corrigés en 1.76.1 |
| 6 | Vérification réelle | Nœud imbriqué sur node2 d'abord ; une lame physique ensuite, sur accord |

## Conception

### Le formulaire gagne les champs courants

- **Interface de gestion** : une ou plusieurs cartes (cases à cocher sur les
  cartes découvertes par Redfish, désignées par MAC ; ou noms saisis),
  mode d'agrégat (`active-backup`, `balance-tlb`, `802.3ad`, ...), et pour
  802.3ad `lacp_rate` et `xmit_hash_policy` ; `miimon` (100 par défaut) ;
  `vlan_id` facultatif.
- **Disques** : disque d'installation (texte libre, un chemin `by-path`
  accepté), disque de données facultatif, « effacer tous les disques ».
- **Système** : libellés du nœud (une ligne `clé=valeur` chacun), modules
  noyau (séparés par des virgules).

### Section « YAML avancé »

Un éditeur de texte pour tout le reste : `os.write_files`,
`os.persistent_state_paths`, `os.sysctls`, `os.environment`,
`os.after_install_chroot_commands`, `system_settings`, etc. Il est fusionné
avec ce que produit le formulaire :

- une clé que le formulaire pose ET que le YAML avancé pose aussi est
  REFUSÉE avec son chemin, jamais écrasée en silence d'un côté ou de
  l'autre ;
- quatre clés restent à la console et sont refusées dans le YAML avancé :
  `install.iso_url` (l'ISO servie par la console), `install.automatic`,
  `install.mode` et `server_url` (portés par le choix créer/rejoindre),
  `token` et `os.password` (champs secrets du formulaire) ;
- chaque chemin est validé contre le schéma de l'installeur de la version
  visée (liste de clés tirée de `pkg/config/config.go` à l'étiquette
  v1.9.0 de harvester-installer, noms en snake_case comme les accepte
  `rename.go`), récursivement dans `os` et `install`, et sur le type
  attendu (liste, dictionnaire, texte, booléen).

### Import d'un fichier

« Importer une configuration » lit un fichier YAML choisi dans le
navigateur et l'envoie au serveur, qui le découpe :

- les champs que le formulaire sait montrer le remplissent ;
- le reste va dans le YAML avancé, tel quel ;
- `install.iso_url` est retiré, avec un message qui dit qu'il sera
  remplacé ;
- les clés inconnues sont refusées avec leur chemin, rien n'est rempli ;
- **aucun secret ne revient au navigateur** : le jeton et le mot de passe
  du fichier sont gardés côté serveur, dans une entrée de courte durée
  (15 min, par personne), désignée par un identifiant ; les champs secrets
  du formulaire disent « repris du fichier » et restent modifiables. Même
  règle que l'image VDDK et les sources vCenter.

### Aperçu

« Aperçu » rend le YAML exact qui sera servi à l'installeur, jeton et mot de
passe masqués, avec les mêmes refus que le lancement. Le lancement refait
toute la validation côté serveur.

### Serveur

- `_harvester_install_config(opts)` construit désormais un dictionnaire
  (plus des lignes à la main), fusionne le YAML avancé, valide, puis
  sérialise. La sérialisation garde les blocs `content: |` lisibles.
- Nouvelles routes, `@requires_auth` + limite de débit :
  `POST /api/baremetal/config/parse` et
  `POST /api/baremetal/config/preview`. `POST /api/baremetal/install`
  accepte les nouveaux champs, `advanced_yaml` et `import_id`.
- Le schéma vit dans `web/harvester_install_schema.py`, avec la version
  d'où il vient, et un test qui échoue si une clé de la fenêtre n'y est pas.

## Vérification

- **Tests** : découpage, fusion, refus (clé inconnue, clé réservée,
  conflit, mauvais type), masquage de l'aperçu, secrets jamais renvoyés,
  sur une copie anonymisée du fichier d'exploitant (les noms, adresses et
  identifiants propres au client n'entrent pas dans le dépôt public).
  e2e : import, aperçu, champs remplis, messages.
- **En réel** : installation d'un nœud imbriqué sur node2 par
  `harvlab.sh` (même fonction de configuration), quatre cartes virtio :
  agrégat de gestion de deux cartes en `active-backup` (un pont Linux ne
  négocie pas LACP : 802.3ad est rendu et testé, pas vérifié en réel),
  agrégat de stockage en `write_files` sur un réseau libvirt isolé, un
  VLAN, disque de données, libellés, modules, chemins persistants, un
  réglage système. Vérifié sur le nœud installé : `nmcli`, `lsmod`,
  libellés du nœud Kubernetes, réglage Harvester, disque de données vu par
  Longhorn. Puis, sur accord, une lame physique par le média virtuel de
  l'iLO.
- **Non vérifiable ici** : LACP réel (pas de commutateur qui le négocie
  sur le banc), `vlan_id` de gestion sur un réseau étiqueté (possible
  seulement si un pont à VLAN est monté sur node2). La doc le dira.

## Hors périmètre

- Profils multi-nœuds à variables (chantier suivant).
- Déploiement par PXE : le média virtuel reste la voie de la console.
