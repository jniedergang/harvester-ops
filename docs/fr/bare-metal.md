# Bare-metal : installer Harvester sur une machine vierge

L'onglet Bare-metal transforme un serveur vide en node Harvester
opérationnel sans que personne aille jusqu'à la baie, branche une clé USB
ou clique dans l'installeur. La console pilote la machine par son BMC.

Rien de magique ni de spécifique ici : c'est le mode d'installation
zéro-touch de Harvester, piloté de bout en bout par la console.

Les [profils de stockage facultatifs](baremetal-storage-onboarding.md) préparent
les interfaces dédiées et prérequis fournisseur pendant une jonction, puis laissent
l’hôte non planifiable pour validation. Le profil réseau neutre et l’exemple Pure non vérifié
n’installent pas de chart CSI et ne configurent pas les identifiants des baies.

---

## L'écran : cinq onglets (2.1.0, Plans en 2.4.0)

Automatisation > Bare-metal s'ouvre sur **Machines** ; les autres onglets
sont à côté, chacun avec son adresse :

| Onglet | Adresse | Contenu |
|---|---|---|
| **Machines** (par défaut) | `#automation/pxe/machines` (et `#automation/pxe`) | Les machines de la dernière découverte, une ligne chacune : matériel, état d'alimentation, disques, cartes réseau, et les gestes **Installer** (ou **Joindre à** *cluster* pendant une jonction), allumer, arrêt propre, arrêt forcé, redémarrer. **Découvrir...** ouvre la fenêtre de découverte ; **Relire** relit les mêmes BMC. Le bandeau de jonction et le bandeau « aucun ISO » sont au-dessus. |
| **Profils** | `#automation/pxe/profiles` | Les profils d'installation multi-nœuds et le lancement d'une série. |
| **Plans** (2.4.0) | `#automation/pxe/plans` | Plans de déploiement, modèles de déclaration et modèles de cluster (2.5.0) : contenu Terraform, pile Cluster API et clusters enfants appliqués après une installation ou à un cluster existant (voir plus bas). |
| **Images** | `#automation/pxe/images` | Le magasin d'ISO. |
| **Historique** | `#automation/pxe/history` | Les actions bare-metal de l'Activité, en cours et passées, chacune avec un lien vers son journal. |

**Rafraîchir**, dans l'en-tête de la carte, relit l'onglet ouvert. Rien ne
se perd en changeant d'onglet, en rechargeant la page ou en revenant le
lendemain : la console garde la dernière découverte (voir plus bas).

---

## Ce qu'il faut avant de commencer

| Prérequis | Pourquoi | Comment le vérifier |
|---|---|---|
| Le BMC parle Redfish | Tout passe par lui | La découverte liste le node |
| Le BMC expose un **média virtuel CD** | L'ISO est monté par le réseau | La ligne de la machine propose **Installer Harvester** ; sinon elle affiche *pas de média virtuel* |
| Le BMC sait amorcer sur `Cd` | La machine doit démarrer une fois sur cet ISO | Même indication que ci-dessus |
| La console est joignable **depuis le BMC** | C'est le BMC qui télécharge l'ISO | Un `curl` vers la console depuis une machine du réseau BMC |

Sur les iLO 4/5 de HPE, le média virtuel est une fonction **iLO
Advanced**. Sans cette licence le BMC n'expose aucun lecteur CD, et la
console le dit tout de suite plutôt que d'échouer trente secondes après
le début d'une installation.

La ligne de la machine indique aussi combien de disques le firmware sait
amorcer. S'il n'en voit aucun, la machine n'a pas de cible
d'installation exploitable et il n'y a rien à installer dessus.

---

## Le déroulé

```
console (votre serveur)                        machine cible
  ISO Harvester officiel ──remaster──► ISO patché
        │                                      │
        │  HTTP simple, jeton à usage unique
        ▼                                      ▼
  /pxe/iso/<jeton>.iso     ◄──── BMC : insertion du média virtuel (URL)
  /pxe/config/<jeton>.yaml ◄──── l'installeur y lit ses réponses
        │
        └─ Redfish : amorce unique sur Cd, mise sous tension, install auto
```

### 1. Amener un ISO dans le magasin

L'onglet **Images** télécharge un ISO **côté serveur, en flux**.
Le fichier ne transite jamais par votre navigateur : un ISO Harvester
pèse environ 7,6 Go, et un upload de cette taille depuis un navigateur
serait tamponné en mémoire à l'arrivée.

Collez l'URL de l'éditeur, lancez le téléchargement, suivez le
pourcentage dans le dock. Le magasin montre ce qui est sur le disque et
combien de place il reste.

Le téléchargement demande le rôle **admin**. La console vérifie l'URL
avant de se connecter, puis à chaque redirection :

- `http` et `https` seulement, 5 redirections au plus, pas de retour de
  `https` vers `http` ; les certificats TLS sont toujours vérifiés ;
- le nom est résolu une fois et la console se connecte à l'adresse
  vérifiée : une réponse DNS ne peut pas basculer vers une adresse interne ;
- les adresses de bouclage, de lien local, privées (RFC 1918, ULA, CGNAT),
  multicast, de diffusion, non spécifiées et réservées sont refusées, de même que les
  adresses de métadonnées de cloud (`169.254.169.254`, `fd00:ec2::254`) ;
- le fichier ne peut pas dépasser 20 Gio, et le téléchargement s'arrête
  quand le magasin descend sous 1 Gio d'espace libre.

Un site isolé sert souvent ses ISO depuis un miroir interne. Autorisez
son réseau dans `config.yaml` ; la console relit le réglage à chaque
téléchargement, sans redémarrage :

```yaml
iso_fetch:
  allow_private: ["10.20.0.0/16", "fd12:3456::/32"]   # miroirs internes
  max_bytes: 21474836480        # plafond de taille, 20 Gio par défaut
  min_free_bytes: 1073741824    # espace libre gardé dans le magasin, 1 Gio par défaut
```

Toutes les adresses vers lesquelles le nom du miroir résout doivent être
autorisées, IPv4 comme IPv6. Les adresses de métadonnées, multicast et non
spécifiées restent refusées même dans un réseau autorisé, de même que
`255.255.255.255` et l'adresse de diffusion d'un réseau IPv4 autorisé.

L'ISO n'est **volontairement pas** embarqué dans le tarball de release :
le toolkit entier pèse 135 Mo, l'ISO à lui seul cinquante fois plus.

### 2. Découvrir le BMC

Dans **Machines**, **Découvrir...** ouvre une fenêtre : saisissez une ou
plusieurs adresses de BMC, un utilisateur et un mot de passe. Les
identifiants ne sont **jamais stockés côté serveur** : ils restent dans la
page le temps de la session, et chaque action les renvoie. Une adresse de
BMC est un nom d'hôte ou une adresse IP, avec un port facultatif ; tout
autre chose (un chemin, un schéma, un utilisateur) est refusé avant toute
lecture.

Chaque BMC joignable revient sous forme de ligne : modèle, numéro de
série, BIOS, CPU et mémoire, disques, NICs avec leurs adresses MAC et
l'état du lien, état d'alimentation, et la possibilité ou non d'installer
la machine. Un BMC injoignable garde sa ligne, avec la raison.

**La console garde la dernière découverte** (2.1.0) : les adresses, la
date et ce que chaque BMC a répondu ; la liste est donc encore là après un
rechargement ou le lendemain. La ligne au-dessus de la liste donne sa
date ; **Relire** relit les mêmes BMC avec les identifiants de la page, et
ouvre la fenêtre pour les demander quand la page a été rechargée. L'état
d'alimentation affiché est celui lu à cette date (son info-bulle le dit).

- Rangée dans `<état>/baremetal-last-discovery.json` (mode 0600,
  remplacement atomique, relative au répertoire d'état, elle le suit
  quand il se déplace). Seuls les champs connus d'une machine sont
  gardés ; un identifiant, jamais.
- C'est un **cache de lecture**, comme le cache des dossiers : écrit
  pendant la lecture Redfish elle-même, sans action suivie, car rien ne
  change sur une machine ni sur un cluster. Une nouvelle découverte la
  remplace.
- La lire demande le rôle **admin** (`GET /api/bmc/last-discovery`) : elle
  nomme les BMC du parc et leurs numéros de série.

Avant d'agir, l'onglet vérifie le magasin : un bandeau dit **Aucun ISO
dans le magasin** quand il est vide et, pendant une jonction, **Aucun ISO
de la version X dans le magasin** quand aucun ISO n'a la version du
cluster (lue dans le cluster, `setting/server-version`, et comparée au
`harvester-release.yaml` de chaque ISO, ou à son nom). **Ouvrir Images**
mène au magasin.

> **Attention avec une machine éteinte.** Un BMC ne relit pas le matériel
> hors tension : il rejoue l'inventaire du **dernier POST**. Sur une
> machine qui n'a pas démarré depuis des mois, cet inventaire peut être
> vieux de plusieurs mois. Le préflight lit cet inventaire sans démarrer
> l'ancien système et vérifie l'identité de toute découverte sauvegardée.
> Un démarrage de découverte permet de rafraîchir les informations.

### 3. Renseigner le node

**Installer Harvester** ouvre un formulaire :

- **Image** : quel ISO du magasin.
- **Node** : nom d'hôte, adressage (statique ou DHCP), IP / masque /
  passerelle, VIP du cluster et son mode.
- **Réseau de gestion** : une ou plusieurs cartes, cochées parmi celles
  qui viennent d'être découvertes (plusieurs font un agrégat), le mode
  d'agrégat et miimon, le rythme LACP en 802.3ad, la politique de hachage
  dans les modes qui s'en servent, un VLAN facultatif.
- **Disques** (1.78.0) : dès que la machine a un inventaire (démarrage de
  découverte, plus bas), un tableau de ses disques : taille, modèle, série,
  média, bus, partitions déjà présentes, et le chemin stable que la console
  écrira (`by-path`, sinon un lien `by-id` vers le disque lui-même, jamais
  un `dm-*` de multipath ; un `sdX` nu seulement s'il n'y a rien d'autre, et
  signalé). Chaque disque reçoit un rôle : **système**, **données** (disque
  par défaut de Longhorn), **pool** avec une étiquette, **effacer
  seulement** ou **ignorer**, et sa propre case **Effacer**
  (`install.wipe_disks_list`), à côté de « effacer tous les disques ». Sans
  inventaire, le disque d'installation se saisit à la main (un chemin
  stable `/dev/disk/by-path/...` de préférence). **Lire les disques**
  montre ce que le BMC publie en Redfish (contrôleurs, RAID ou
  pass-through, volumes, disques), en lecture seule.
- **Nom du cluster** (création) : le nom sous lequel la console déclare le
  nouveau cluster, le nom d'hôte par défaut.
- **Système** : DNS, NTP, libellés du nœud (un `clé=valeur` par ligne),
  modules noyau.
- **Accès** : le token du cluster, le mot de passe OS, et éventuellement
  des clés publiques SSH.
- **YAML avancé** : tout le reste de ce que l'installeur accepte, fusionné
  avec le formulaire : `os.write_files` (connexions NetworkManager du
  réseau de stockage ou des autres, réglages systemd, sshd),
  `os.persistent_state_paths`, `os.sysctls`, `os.environment`,
  `system_settings`...

**Importer une configuration** prend un fichier d'installeur existant et
le découpe : ce que le formulaire montre le remplit, le reste va dans le
YAML avancé. Le jeton et le mot de passe du fichier restent sur le serveur
15 minutes (les champs disent « repris du fichier »), son `iso_url` est
remplacée par l'image que sert la console (la fenêtre le dit),
`install.automatic` est retiré (la console installe toujours sans
opérateur), et un fichier sans `bond_options` reçoit `active-backup`,
comme le ferait l'installeur. **Aperçu** montre le YAML exact que lira
l'installeur, secrets masqués.

Chaque clé est vérifiée contre le schéma de l'installeur de Harvester v1.9
avant toute mise sous tension. Une clé est refusée, avec son chemin (par
exemple `os.write_files[2].contnt`), quand elle est inconnue, mal typée,
posée à la fois par le formulaire et le YAML avancé, ou gardée par la
console (`install.iso_url`, `install.automatic`, `install.mode`,
`server_url`, `token`, `os.password`, `install.power_off`). Un `system_settings.ntp-servers` est refusé tant que le
champ NTP est rempli : l'installeur réécrit ce réglage depuis les serveurs
NTP et l'autre valeur serait perdue sans rien dire.

Avec un inventaire, le serveur vérifie aussi les disques avant toute mise
sous tension, et refuse en nommant le disque et la raison : un rôle donné
deux fois, pas de disque système, un disque trop petit (installeur v1.9 :
250 Gio pour un disque système seul, 180 Gio avec un disque de données,
50 Gio pour un disque de données ou de pool ;
`harvester.install.skipchecks=true` lève les tailles), un disque qui porte
des partitions ou un système de fichiers sans sa case Effacer (ou tout
effacer), une étiquette de pool qui n'est pas en minuscules, chiffres et
`-`, un disque que l'inventaire ne connaît pas. Les disques système et de
données sont formatés par l'installeur : cocher leur case Effacer lève le
contrôle, et ils sont retirés de la liste envoyée à l'installeur.

Le token et le mot de passe n'apparaissent jamais dans une réponse, dans
un libellé d'action, ni dans une ligne de log.

**Une machine qui porte déjà un système (2.2.0).** Avant le bouton
Installer, la fenêtre dit si un système semble présent sur la machine,
d'après ce que le BMC publie déjà (lecture seule, rien n'est changé) :

- une entrée de démarrage UEFI qui lance un chargeur `\EFI\...` depuis
  une partition de disque (clés USB, CD et entrées réseau écartés), nommée
  avec le chargeur et le disque, par exemple `L'entrée de démarrage
  « elemental-shim » lance \EFI\elemental\shim.efi depuis une partition
  de disque (HD.Emb.1.3)` ;
- la même entrée en tête de l'ordre de démarrage (« démarre d'abord sur ») ;
- un volume logique (RAID) sur un contrôleur.

Sur un iLO 4, ces signes viennent de `Systems/1/BIOS/Boot/` (l'iLO 4 ne
publie ni disque ni volume), au dernier démarrage de la machine ; sur un
iDRAC 9 ou un iLO 5, de `Systems/<id>/BootOptions` et `Boot.BootOrder`.
Quand un système semble présent, les signes sont listés et l'installation
demande de taper exactement le nom de la machine : son numéro de série, ou
l'adresse du BMC quand la série est inconnue ; Installer reste inactif tant
qu'il ne correspond pas. **Aucun signe** n'est dit que si les entrées de
démarrage ont pu être lues et qu'aucune ne mène à un système (un système
lancé en mode BIOS ancien n'y apparaîtrait pas) ; quand le BMC ne dit rien,
la fenêtre dit **Inconnu**, jamais vide. La console revérifie à
l'installation, sur sa propre lecture Redfish, et refuse en `409` avec
`fields: ["system_confirm"]` quand un système semble présent et que le nom
tapé (`system_confirm`) ne correspond pas. `POST /api/bmc/system-check`
(`{host, user, password}`, administrateur) rend `state` (`present`,
`absent`, `unknown`), `evidence` et `confirm_name`. Une entrée de démarrage
peut survivre à l'effacement d'un disque : le contrôle demande alors le nom
une fois de plus, ce qui est le côté sûr. Les séries d'un profil passent
par le même contrôle, machine par machine (2.2.1, plus bas).

### 4. Suivre l'installation

L'installation est une action trackée comme les autres : elle apparaît
dans le dock dès la confirmation et diffuse ses étapes.

| Étape | Ce qui se passe |
|---|---|
| `preflight` | Lit l'inventaire BMC sans allumer l'ancien système, vérifie l'identité de découverte et la présence d'un disque : l'inventaire de découverte d'abord, puis les cibles d'amorce UEFI de HPE, puis les disques Redfish ; sans aucune de ces sources, un avertissement (l'installeur vérifie lui-même son disque) |
| `remaster` | Patche l'ISO pour qu'il s'installe sans opérateur (voir plus bas) |
| `serve` | Publie l'ISO et la configuration derrière des jetons à usage unique |
| `power-off` | Éteint la machine (tout état autre que `Off`, transitoire ou illisible compris, reçoit un arrêt franc) avant le montage du média et le réglage de l'amorce |
| `bmc-insert` | Monte l'ISO en média virtuel, et attend la réponse du BMC (jusqu'à 15 min, `HARVESTER_OPS_BM_INSERT_WAIT`) |
| `bmc-boot` | Programme une amorce **unique** sur `Cd` |
| `power` | Vérifie que la machine est toujours éteinte et que la cible relue est `Cd`, puis l'allume une seule fois. Un BMC qui ne relit pas `Once` (l'émulateur sushy-tools répond toujours `Continuous`) reçoit un avertissement, pas un refus |
| `wait-install` | L'installeur **éteint** la machine quand il a fini (`install.power_off`, posé par la console) |
| `boot-disk` | Éjecte le média, programme une amorce unique sur le disque, rallume la machine |
| `wait-api` | Attend l'API Harvester sur la VIP |
| `declare` | Lit le kubeconfig du nouveau cluster par SSH et déclare le cluster dans la console |
| `pools` | Crée les pools de disques (seulement s'il y en a) |

Pourquoi l'extinction (1.78.0) : certains BMC n'appliquent pas l'amorce
unique et gardent le CD virtuel en premier ; la machine revenait alors sur
l'installeur après chaque installation, en boucle (vu sur un banc Redfish).
L'installeur s'éteignant, la console sait que l'installation est finie et
démarre elle-même le disque, quoi que le BMC fasse de l'amorce unique.

Le préflight et la préparation de l'ISO ne changent pas l'alimentation.
Une fois la préparation terminée, la machine est éteinte avant le montage
du média. Si le montage échoue (par exemple le BMC ne joint pas l'adresse
de l'ISO), une machine trouvée allumée est rallumée. Une fois l'amorce
touchée, la console ne rallume jamais la machine sans avoir confirmé la
cible `Cd` : l'allumer à ce moment-là pourrait lancer l'installeur.

**Le nouveau cluster est déclaré tout seul** (création). La console crée
pour lui une paire de clés ed25519 (`ssh/<nom>_id`, 0600, dans son
répertoire d'état, `/var/lib/harvester-ops` pour le service packagé, là
où elle range aussi les clusters déclarés depuis Paramètres > Clusters), pose
la clé publique dans la configuration d'installation, lit
`/etc/rancher/rke2/rke2.yaml` par SSH en `rancher` à travers la VIP (clé
d'hôte retenue au premier contact dans `ssh/<nom>_known_hosts`), l'écrit
en 0600 avec son serveur sur `https://<VIP>:6443`, et déclare le cluster
avec ce kubeconfig (`kubeconfigs/<nom>.yaml`), cette clé et le nœud dans
`clusters.d/<nom>.yaml` de ce même répertoire, jamais dans `config.yaml`,
en lecture seule. Les chemins de la déclaration sont relatifs au
répertoire d'état : le copier déplace le cluster avec la console (voir
« Déplacer harvops vers un autre hôte » dans le guide d'installation). La
clé reste : la console s'en
sert pour l'arrêt et le démarrage gracieux. Un nœud qui rejoint plus tard
ce cluster reçoit la même clé publique.

**Les pools de disques** sont créés juste après la réponse de l'API, avec
ce que Harvester offre déjà après une installation : chaque disque de pool
est retrouvé parmi les BlockDevices de node-disk-manager par sa série ou
son WWN (jamais par son nom noyau, qui peut changer d'un démarrage à
l'autre), provisionné dans Longhorn, formaté et étiqueté du pool, et
chaque pool reçoit une classe de stockage `longhorn-<étiquette>`
(`diskSelector: <étiquette>`, répliques au choix, 1 par défaut, un
avertissement quand plus d'une est demandée sur un nœud seul). Une classe
existante avec un autre sélecteur est refusée avant qu'aucun disque ne
soit formaté. Un disque qui est le disque système ou de données, ou qui
porte les partitions du système, n'est jamais pris. En mode rejoindre, les
disques sont provisionnés et les classes laissées telles quelles. La même
chose en ligne de commande :
`harvester-resources pools-apply --cluster <c> --node <n> --spec <json>`
(et `host disk-add --tag`).

### Ajouter un nœud à un cluster existant (API seulement)

L'onglet crée un nouveau cluster. `POST /api/baremetal/install` accepte
aussi `"mode": "join"` avec l'adresse du cluster à rejoindre
(`"server_url": "https://<VIP>:443"`) au lieu d'une VIP. Ce cluster doit
être déclaré dans la console : l'action se termine quand le nouveau nœud
est **prêt** dans ce cluster (étape `wait-node`), pas quand une API
répond, puisque celle du cluster répondait déjà. La configuration produite
pour une jonction a été vérifiée en installant deux nœuds dans un cluster
de test à trois nœuds ; le déroulé par Redfish en mode jonction n'a pas
encore été exécuté sur du vrai matériel.

Depuis la 1.88.1, l'attente du nœud et la création des pools de disques se
font avec vos propres droits sur le cluster rejoint (identité déléguée, ou
votre session Rancher), résolus au lancement de l'installation. Avant, le
fil de l'installation retombait sur le kubeconfig de la console. Une
personne sans droit de lire les nœuds ou d'écrire les périphériques de
stockage de ce cluster voit maintenant ces étapes refusées.

### Lire les disques d'abord : le démarrage de découverte (fenêtre, API et CLI)

Certains BMC ne publient aucun disque (un iLO 4 n'en publie aucun) : la
console ne peut alors pas dire sur quel disque installer.
`POST /api/baremetal/discover` (`bmc_host`, `bmc_user`, `bmc_password`,
`iso`, `extra_args` facultatif) démarre une fois l'ISO Harvester avec un
petit script de la console, et attend que la machine renvoie ce que Linux
voit : disques avec leurs liens stables `by-path`/`by-id`, partitions,
cartes réseau. La machine s'éteint ensuite d'elle-même.
`GET /api/baremetal/inventory/<bmc_host>` rend l'inventaire analysé
(`source`, `at`, `system_serial`, `disks`, `nics`), ou 404. Dans la
fenêtre d'installation, **Démarrage de découverte** la lance comme action
suivie et remplit le tableau des disques à l'arrivée de l'inventaire ; les
cartes de gestion montrent alors leur nom Linux.

- Le script est sur l'ISO elle-même (`/discover.sh`, monté en
  `/run/initramfs/live`) ; la ligne noyau ne porte que son chemin
  (`systemd.run=`) et jamais `harvester.install.*` : rien n'est installé.
  Il ne porte aucun secret, seulement l'adresse de dépôt, dont le jeton
  n'accepte qu'un envoi de 1 Mio au plus, et seulement pendant une
  découverte.
- Chaque découverte remasterise sa propre ISO (une minute environ) dans le
  répertoire de travail de la console, avec un jeton de dépôt neuf, la sert
  pour elle seule et l'efface à la fin : prévoir la taille de l'ISO en
  espace libre.
- Le script envoie aussi le numéro de série et l'UUID DMI de la machine.
  La console les compare à ceux que donne le BMC : un inventaire venu d'une
  autre machine est refusé et n'est pas enregistré, les deux valeurs dans
  le message. Si la machine ne donne que des valeurs de remplissage
  (fréquent sur une VM : vide, « Not Specified »), l'UUID sert ; si rien ne
  peut être comparé, l'inventaire est enregistré avec une étape
  d'avertissement. L'installation le revérifie avant de rien allumer : si le BMC
  mène maintenant à une autre machine (lame changée, BMC réadressé), elle
  s'arrête et demande une nouvelle découverte, plutôt que de choisir les
  disques d'après l'inventaire d'une autre machine.
- Une action à la fois par BMC : une découverte ou une installation sur un
  BMC déjà piloté par une autre découverte ou installation est refusée
  (409).
- Attentes : 15 min pour l'inventaire, puis 5 min pour que la machine
  s'éteigne d'elle-même ; au-delà elle est éteinte de force et l'étape le
  dit. Le média virtuel est éjecté dans tous les cas (un échec d'éjection
  est signalé par une étape d'avertissement : l'éjecter depuis le BMC).
- Les inventaires sont gardés par numéro de série du système (lu par
  Redfish), en 0600, sous `~/.local/share/harvester-ops/inventory`
  (`HARVESTER_OPS_INVENTORY_DIR`).
- Même déroulé en ligne de commande, mot de passe jamais en argument :
  `harvester-baremetal discover --bmc <hôte> --user <compte> --password-file <fichier 0600> --iso <chemin>`
  (ou `--password-stdin`).

### Plusieurs nœuds identiques : profils d'installation et séries (1.80.0)

Les profils sont dans l'onglet **Profils** (2.1.0).

Un **profil** est une configuration d'installation nommée : les champs de
la fenêtre d'installation, écrits en YAML, et le YAML avancé, où toute
valeur peut porter une variable `{{nom}}`. Variables intégrées :
`{{hostname}}`, `{{ip}}` (adresse de gestion), `{{mgmt_mac}}` et `{{vip}}` ;
un profil déclare les siennes (`storage_ip`, `admin_ip`...). Elles valent
partout, y compris dans les keyfiles NetworkManager et les extraits `sshd`
d'`os.write_files` :

```yaml
os:
  write_files:
  - path: /etc/NetworkManager/system-connections/storage.nmconnection
    permissions: '0600'
    content: |
      [ipv4]
      method=manual
      address1={{storage_ip}}/24
```

Le YAML avancé est lu **avant** le remplacement des variables, puis chaque
valeur entre telle quelle dans sa chaîne : une valeur qui porte `: ` ou `#`
ne casse pas le document, et une variable seule sur un champ entier du
schéma de l'installeur (`vlan_id: {{vlan}}`) devient un entier. Une valeur
tient sur une ligne, 512 caractères au plus, sans caractère de contrôle.

- Sous-section **Profils** de l'onglet Bare-metal : liste, **Nouveau
  profil**, modification, suppression. L'éditeur prend les champs en YAML
  (un gabarit avec les variables intégrées est proposé), le YAML avancé, et
  peut partir d'un fichier de configuration d'installation (son jeton et
  son mot de passe sont jetés). L'enregistrement contrôle le profil contre
  le schéma de l'installeur avec des valeurs d'essai : une clé inconnue ou
  réservée, ou une variable non déclarée, est refusée à ce moment, pas au
  lancement d'une série.
- Les profils sont rangés dans le répertoire d'état de la console,
  `<état>/profiles.d/<nom>.yaml` (mode 0600, nom RFC 1123), et se déplacent
  avec lui comme les clusters qu'elle déclare. **Aucun secret n'y est
  jamais rangé** : jeton du cluster, mot de passe de l'OS et mots de passe
  des BMC sont refusés dans un profil.
- **Installer une série** : nom du cluster, VIP, ISO (celle du profil par
  défaut), jonctions simultanées (2 par défaut, 4 au plus), les secrets, et
  le tableau des machines : une ligne par machine (hôte du BMC,
  utilisateur, mot de passe du BMC propre à la ligne si besoin, une colonne
  par variable). **Remplir le tableau** lit un CSV collé dont la première
  ligne nomme les colonnes (`bmc_host,bmc_user,hostname,ip,...`, séparateur
  virgule, point-virgule ou tabulation) ; une colonne `bmc_password` est
  ignorée : les mots de passe se saisissent dans le tableau ou une fois pour
  toutes les lignes. Jeton et mot de passe de l'OS peuvent aussi venir d'un
  fichier de configuration (même cache serveur de 15 minutes que la fenêtre
  d'installation).
- **Contrôler et prévisualiser** remplace les variables de chaque ligne et
  passe les mêmes contrôles qu'une installation (schéma, contrôles des
  disques sur l'inventaire de découverte du BMC quand il existe), sans
  aucun secret ; chaque ligne montre son mode ou la raison de son refus, et
  son YAML exact (jeton masqué). Une variable absente est nommée avec sa
  ligne.
- **Lancer la série** recontrôle chaque ligne, et rien n'est allumé si une
  seule échoue. La ligne 1 crée le cluster par l'action d'installation
  habituelle ; les autres rejoignent `https://<vip>:443` une fois la ligne 1
  **terminée** (cluster déclaré dans la console, sa clé SSH générée donnée
  aux nœuds qui le rejoignent), deux à la fois. Une création ratée arrête
  la série : les autres lignes sont sautées. Une action parente
  `baremetal-batch:<cluster>` montre chaque machine comme une étape (nom
  d'hôte, BMC, mode, identifiant de son installation) ; chaque machine est
  sa propre action `baremetal-install:<hostname>` dans le dock. Arrêter la
  série arrête l'installation en cours et saute les lignes non commencées.
- **Machines qui portent déjà un système (2.2.1).** La fenêtre de série
  fait le contrôle de la fenêtre d'installation (plus haut) sur chaque
  machine du tableau : **Vérifier les machines** lit chaque BMC, en lecture
  seule, et montre une bannière par machine, `Ligne <n> · <BMC>`, avec
  **présent** et ses signes, **Aucun signe**, ou **Inconnu** dit tel quel.
  Pour chaque machine où un système semble présent, tapez exactement son
  nom (numéro de série, ou adresse du BMC quand la série est inconnue) ; un
  nom faux est signalé. **Lancer la série** relit d'abord les machines si
  le tableau a changé depuis la dernière lecture, et ne part pas tant qu'un
  nom manque ou est faux. La console relit ensuite elle-même chaque BMC,
  après les contrôles des lignes et avant que rien ne soit allumé, et
  refuse toute la série en `409`, avec `fields: ["system_confirm"]`,
  `refused` (hôtes des BMC) et `machines` (chaque machine avec sa ligne,
  son état, ses signes, le nom à taper et `confirmed`) quand une machine où
  un système semble présent n'a pas son nom ; rien ne part, et la fenêtre
  montre ce que la console a lu. « Inconnu » n'est pas un refus, comme pour
  une machine seule. Les noms tapés passent dans `rows[].system_confirm` et
  n'atteignent jamais les options d'installation.
- API : `GET|POST /api/baremetal/profiles`, `GET|PUT|DELETE
  /api/baremetal/profiles/<nom>`, `POST .../from-config`,
  `POST .../<nom>/csv`, `POST .../<nom>/render`, `POST .../<nom>/batch`
  (`cluster_name`, `vip`, `iso`, `rows`, `token`, `password`,
  `bmc_password`, `import_id`, `concurrency` ; `rows[].system_confirm`,
  2.2.1), `POST .../<nom>/system-check` (même corps, lecture seule : rend
  `machines`, 2.2.1). La lecture est ouverte à
  tous les rôles, l'écriture demande **admin**.
- Ligne de commande, même code (la série tourne dans la commande), secrets
  jamais en argument :

  ```
  harvester-baremetal profile list
  harvester-baremetal profile show rack-a
  harvester-baremetal profile apply rack-a --nodes nodes.csv \
      --cluster-name rack-a --vip 10.0.0.100 --secrets-file secrets.yaml
  ```

  `secrets.yaml` (mode 0600, ou le même YAML sur l'entrée standard avec
  `--secrets-stdin`) : `token`, `password` (facultatif), `bmc_password`
  (commun) et `bmc_passwords` (par hôte de BMC). Le CSV ne porte aucun mot
  de passe. `--state-dir` désigne le répertoire d'état de la console,
  `--port` le port du serveur d'artefacts quand une console écoute déjà sur
  8091. Quand un système semble présent sur une machine, la commande
  affiche chaque machine avec son état et ses signes, ne lance rien et sort
  en 2 ; `--confirm-system <hôte du BMC>=<nom>` (une fois par machine, le
  nom tel qu'affiché) la confirme (2.2.1).
- Vérifié par les tests automatiques (substitution, magasin, ordre, refus,
  secrets) et sur la fenêtre avec des routes simulées. **Faite en réel
  (1.83.2)** sur deux machines derrière deux BMC Redfish (banc bmcfg) : la
  ligne 1 a créé le cluster, la ligne 2 l'a rejoint, les deux nœuds `Ready`,
  le cluster déclaré tout seul avec ses deux nœuds. Cet essai a trouvé trois
  défauts, corrigés en 1.83.2 : `skipchecks: true` n'atteignait pas
  l'installeur, une ligne en échec ne disait pas pourquoi, et le nœud qui
  rejoint manquait à la déclaration.
  **Vérifié en réel (2.2.1), en lecture seule** : une fenêtre de série avec
  node3 (iLO 4, `172.16.1.33`) et node4 (iLO 4, `172.16.1.34`) a montré les
  deux comme présents, node3 avec `sles-secureboot` (`\EFI\sles\shim.efi`
  sur `HD.Emb.5.2`) et le nom `USE6236S00`, node4 avec `elemental-shim` et
  `sles-secureboot` et le nom `USE6236RY3` ; un nom faux a été signalé et la
  série n'est pas partie. La série n'a jamais été envoyée.

### Plans de déploiement : le contenu Terraform après l'installation (2.4.0)

Un **plan de déploiement** est une liste de phases nommée et réutilisable,
que la console déroule en une seule action suivie, `deploy:<cluster>`. On
l'applique à un cluster Harvester déjà déclaré, ou on l'enchaîne après une
installation bare-metal. En 2.4.0, les phases sont :

| Phase | Ce qu'elle fait |
|---|---|
| **install** | Seulement quand le plan part de Bare-metal : l'installation d'une machine ou la série, avec la même lecture des BMC (garde 132) que sans plan. |
| **readiness** (disponibilité) | Attend que le cluster soit utilisable : nœuds Ready, objets `ManagedChart` prêts, nœuds et volumes Longhorn sains, une classe de stockage par défaut. Relue toutes les 15 s, 20 minutes par défaut (`phases.readiness.timeout_minutes`, de 1 à 180). |
| **terraform** | Fait de chaque **modèle de déclaration** du plan une déclaration Terraform ordinaire, `<plan>-<modèle>`, puis la planifie et l'applique. |
| capi, clusters | Disponibles depuis la 2.5.0 : voir « Plans de déploiement : Cluster API et clusters enfants » plus bas. |
| registry, artefacts | Présentes dans le modèle, grisées avec « à venir (2.6+) ». Un fichier de plan qui en active une est refusé. |

**Où.** L'onglet **Plans** (`#automation/pxe/plans`, entre Profils et
Images) liste les plans à gauche et en ouvre un à droite : une carte par
phase avec son interrupteur, les modèles de la phase Terraform choisis dans
une liste (jamais retapés), leur ordre (boutons monter et descendre) et une
dépendance facultative par modèle. La partie **Modèles de déclaration** de
l'onglet édite les modèles avec les formulaires Terraform (VM, image, clé
SSH, réseau, classe de stockage, pool d'adresses, code libre). Les fichiers
sont gardés dans `<état>/plans.d/<nom>.yaml` et
`<état>/tf-templates.d/<nom>.yaml`, en 0600, écrits par remplacement
atomique, avec une révision : un enregistrement sur une révision périmée
reçoit 409 et garde la saisie. **Enregistrer comme modèle**, dans Terraform >
Déclarations, fait d'une déclaration Harvester un modèle.

**Variables.** Un modèle écrit `{{nom}}` là où une valeur change d'un
cluster à l'autre ; le plan liste les variables qu'il demande. `cluster` et
`vip` sont intégrées (la `vip` d'un cluster existant est l'adresse de son
kubeconfig, si c'en est une). Une valeur tient sur une ligne de 512
caractères au plus, sans caractère de contrôle. Une chaîne qui n'est qu'une
variable prend le type du champ du formulaire (un VLAN devient un nombre).
Dans le code libre, une valeur est placée selon sa place : dans une chaîne
ou un heredoc, elle est échappée et arrive comme du texte ; hors chaîne,
seuls un nombre et `true`/`false` passent ; puis le résultat est relu : une
valeur qui changerait la structure du modèle (marqueur de heredoc, `$` ou
`%` suivi du `{` de la valeur suivante) est refusée avec son nom.

**Aucun secret dans un modèle.** Tout ce qui passe à Terraform finit dans
son état. L'enregistrement d'un modèle retire les valeurs des clés au nom de
secret (et dit quels chemins il a retirés), garde les clés publiques et les
*noms* de Secrets Kubernetes (`user_data_secret_name`), retire un cloud-init
qui parle de mot de passe, et refuse le code libre qui affecte un secret,
des identifiants dans une adresse ou une fonction qui cache une valeur
(`base64decode`, `sensitive`). Le même contrôle est rejoué sur le modèle
rendu : une valeur qui ressemble à un secret est refusée elle aussi. Ce
contrôle est lexical (noms de clés de secret, lignes de mot de passe dans
`user_data`, identifiants dans une adresse) : un jeton donné comme valeur
d'une variable au nom neutre, par exemple `echo {{x}}` dans `user_data`,
n'est pas détecté. Les valeurs d'un plan ne sont pas faites pour les
secrets, car elles sont gardées dans le résultat du déroulé. Garder le
secret dans un Secret Kubernetes et le désigner par les champs
`*_secret_name`.

**Fenêtre Déployer.** **Appliquer un plan** sur la page d'un hôte
Harvester, ou **Déployer** dans l'onglet Plans, ouvre une fenêtre à trois
onglets : **Paramètres** (cluster, plan, variables), **Vérifications**
(constats regroupés par phase, bloquants d'abord) et **Résumé**
(déclarations créées ou reprises, durée estimée). **Lancer** reste grisé
tant qu'un constat bloque, avec la raison dans sa bulle. **Essai à blanc**
déroule la même chaîne mais ne fait que planifier : il crée les
déclarations, les planifie puis les supprime ; rien n'est écrit sur le
cluster.

La fenêtre d'installation d'une machine et la fenêtre de série d'un profil
ont un champ **Plan** facultatif, prérempli par le `plan` du profil, avec
les variables de ce plan ; avec un plan, l'action ouverte est `deploy:` et
l'installation en est la première phase.

Constats : `plan-invalid`, `phase-not-available`, `template-missing`,
`var-missing`, `var-invalid`, `decl-name-taken`, `tf-missing`,
`cluster-unreachable`, `admin-kind`, `forbidden-construct` (bloquants) ;
`template-newer`, `decl-differs`, `plan-changed` (avertissements) ;
`decl-exists`, `readiness-later` (informations).

**Suivre un déroulé.** L'action montre une étape par phase
(`phase-<id>`), des sous-étapes de disponibilité (`ready-<code>`) et une
étape par modèle (`tf-<modèle>`), chacune avec l'identifiant de son action
fille (l'installation, le plan et l'apply Terraform) en lien. États : fait,
erreur, partiel (un modèle a échoué, les indépendants ont continué), arrêté.
Un modèle est indépendant sauf s'il vient après un modèle en échec qu'il
désigne dans sa dépendance.

**Jamais de destruction.** Si le plan Terraform d'un modèle compte une
suppression ou un remplacement, il n'est pas appliqué : le modèle finit en
erreur avec `plan-destroys` et un lien vers la déclaration à revoir. Une
déclaration qui existe déjà, faite par le même plan et le même modèle, est
seulement planifiée et appliquée. Si son contenu diffère de ce que donnent
les valeurs, elle est gardée et le déroulé le dit (`kept: values differ from
the declaration`), sauf si **Mettre à jour les déclarations dont les valeurs
diffèrent** a été coché au lancement.

**Arrêter et reprendre.** **Arrêter** (ou le dock) arrête le déroulé : une
installation en cours est arrêtée, une exécution Terraform va à son terme,
les modèles non commencés sont sautés (`cancelled`). Un arrêt pendant le
dernier modèle n'a plus rien à sauter : le déroulé finit `done`, avec
`cancel_requested` dans son résultat. **Reprendre** (onglet Historique, sur
un déroulé en erreur, partiel, arrêté ou interrompu) lance une nouvelle
action `deploy:` qui relit le résultat de la première, revérifie à peu de
frais chaque phase terminée (cluster déclaré et nœuds présents,
disponibilité rejouée, déclaration présente à la révision appliquée) et
repart de la première phase non terminée. Un déroulé dont la console a
redémarré est marqué interrompu et se reprend. Une série dont tous les
nœuds ont rejoint n'est pas réinstallée ; sinon seules les lignes dont le
nom d'hôte n'est pas un nœud sont réinstallées, avec la lecture des BMC et
les secrets redemandés. Une installation d'une seule machine ne se reprend
pas : la relancer.

**Secrets.** En 2.4.0, seule la phase d'installation emploie des secrets
(jeton du cluster, mot de passe de l'OS, mots de passe des BMC). Ils sont
saisis au lancement, gardés en mémoire le temps du déroulé, effacés à la fin
quelle qu'en soit l'issue, et redemandés à la reprise. Ils n'apparaissent
jamais dans le libellé de l'action, ses étapes, son résultat, ni dans les
fichiers de plans et de modèles. Un plan appliqué à un cluster existant ne
demande aucun secret.

**Rôles.**

| Geste | Rôle |
|---|---|
| Lire les plans et les modèles (un lecteur voit `(hidden)` à la place du cloud-init) | lecteur |
| Écrire ou supprimer un plan ou un modèle | admin |
| Appliquer un plan à un cluster existant, l'arrêter, le reprendre | opérateur |
| Une chaîne avec installation, ou sa reprise | admin |
| Un plan dont la phase capi doit installer la pile Cluster API (un opérateur peut le lancer quand la pile est déjà installée aux versions du paquet : la phase ne fait alors que la vérifier) | admin |
| Un plan dont les modèles créent des réseaux de VM, des classes de stockage, des pools d'adresses (ou, en code libre, des réseaux de cluster, des configurations VLAN, des équilibreurs, des réglages, des périphériques PCI ou SR-IOV) | admin |

Un opérateur peut lancer un plan dont les modèles portent du code libre : ce
code a été écrit par un admin, l'opérateur ne donne que des valeurs, qui
sont neutralisées. La déclaration produite consigne qui a écrit la révision
du modèle (`origin.template_by`). Hors de la chaîne, cette déclaration reste
du code libre et seul un admin la planifie, l'applique ou la détruit depuis
Terraform > Déclarations. Le serveur décide ; la fenêtre montre le constat
`admin-kind` et garde Lancer grisé.

Installer la pile Cluster API est réservé aux administrateurs, dans un plan
comme dans Cluster API > Installation. Quand la pile est à installer, un
opérateur reçoit le constat `capi-install-admin` et Lancer reste grisé ; la
phase capi revérifie le rôle à son début, si bien qu'une reprise ou une pile
retirée entre-temps est refusée de la même façon.

**Un cluster à la fois.** Une seule action `deploy:` par cluster (409
`deploy-running`) ; un arrêt ou un démarrage du cluster la bloque, et un
plan en cours bloque un arrêt. Le verrou est un fichier
(`<état>/deploy.d/<cluster>.lock`) partagé par la console et la ligne de
commande.

**Vu en réel (2.4.0).** Sur harvlab : modèles de réseau, de classe de
stockage et de pool d'adresses, un échec laissé `partial`, une correction
dans Terraform > Déclarations, une reprise qui a sauté les modèles faits, un
arrêt et sa reprise, un essai à blanc sans rien écrire, les rôles opérateur
et admin. Sur un banc Redfish (sushy-tools sur une machine KVM) : un
Harvester d'un nœud installé par un plan, puis la disponibilité et un
modèle, 36 minutes en tout (installation 35 min, disponibilité 76 s,
Terraform 4 s). Pas encore vu : une série avec plan, la garde des BMC dans
un plan, la reprise d'une phase d'installation.

**Ligne de commande.** `bin/harvester-plans.py` charge la console et déroule
la même chaîne : `plan list|show|save|delete`,
`template list|show|save|delete`, `check --cluster C [--vars FICHIER] PLAN`,
`apply --cluster C [--vars FICHIER] [--dry-run] [--update-declarations]
PLAN`, `resume [--secrets-file F | --secrets-stdin] [--confirm-system
HÔTE=NOM] ACTION`, et `install --profile P --nodes FICHIER --cluster-name N
--vip IP PLAN` pour une série avec plan. `--json` écrit un document à chaque
sortie. Codes de sortie : 0 fait, 1 erreur ou partiel, 3 arrêté. La ligne de
commande a les droits du répertoire d'état (elle ne contrôle pas les rôles).

**Revenir à une 2.3.x.** La mise à jour vers la 2.4.0 est additive et
laisse les déclarations existantes telles quelles. Un retour à une 2.3.x
(onglet Mise à jour) se fait sans plantage, avec trois limites : la 2.3.x
liste les déclarations de réseau de VM, de classe de stockage et de pool
d'adresses mais ne sait ni les planifier, ni les appliquer, ni les détruire
(leurs objets restent sur le cluster, sans gestion) ; un profil enregistré
en 2.3.x perd son champ `plan` ; les plans, les modèles et les déroulés
sont ignorés jusqu'au retour de la 2.4.0. Avant de revenir en arrière,
détruire les déclarations de ces trois genres, ou les accepter sans
gestion.

### Plans de déploiement : Cluster API et clusters enfants (2.5.0)

Après le contenu Terraform, un plan sait déclarer la pile Cluster API et
créer des clusters RKE2 enfants, sur Harvester 1.9 ou plus seulement.

| Phase | Ce qu'elle fait |
|---|---|
| **capi** | Déclare la pile (amorçage et plan de contrôle RKE2, CAPHV, CAAPH) depuis le paquet actif, par la même commande que le bouton **Installer les fournisseurs** (`harvester-capi install`, images poussées sur les nœuds). Une pile déjà prête aux versions du paquet est laissée telle quelle (rien de déclaré, aucune action fille). 10 minutes par défaut (`phases.capi.timeout_minutes`, de 1 à 60). |
| **clusters** | Crée les clusters enfants du plan **l'un après l'autre**, chacun depuis un **modèle de cluster** nommé, par la même commande que la fenêtre de création (`harvester-capi create`). Une action fille `capi-cluster-create:<ns>/<nom>` par cluster. 30 minutes par cluster par défaut (`phases.clusters.timeout_minutes`, de 5 à 180). 16 clusters au plus par plan. |

**Modèles de cluster.** Un modèle de cluster porte une demande du
formulaire de création Cluster API, sans nom ni namespaces (le plan les
donne). Les valeurs peuvent employer des `{{variables}}`, comme les modèles
de déclaration. Ils sont rangés dans `<état>/capi-templates.d/<nom>.yaml`,
mode 0600, avec une révision. La partie **Modèles de cluster** de l'onglet
Plans les liste et les édite dans le formulaire de création (mode modèle,
**Variable...** dans chaque champ de référence), et **Enregistrer comme
modèle** dans la fenêtre de création de Cluster API > Clusters garde la
demande remplie. Un modèle employé par un plan enregistré ne peut pas être
supprimé (409 `template-in-use`). Lecture : rôle lecteur ; écriture : rôle
administrateur.

**Clusters d'un plan.** La carte Clusters liste les entrées dans l'ordre :
nom du cluster (aussi son namespace, 40 caractères au plus une fois rendu,
`{{cluster}}` et variables du plan admis), modèle et « dépend de ». Le
raccourci **Plusieurs** ajoute `count` clusters depuis un modèle, nommés par
un motif qui contient `{{n}}` (1, 2, 3...) ; ils sont ajoutés à la fin de la
liste à l'enregistrement du plan. Dans un fichier de plan :

```yaml
phases:
  capi: {enabled: true, timeout_minutes: 20}
  clusters:
    enabled: true
    timeout_minutes: 45
    items:
      - {name: site1-apps, template: rke2-petit}
    template: rke2-petit        # raccourci, développé à l'enregistrement
    count: 3
    name_pattern: "site1-k{{n}}"
```

**Une identité restreinte par cluster enfant.** Jusqu'en 2.4.0, CAPHV
agissait sur Harvester avec le kubeconfig de la console. Un cluster créé par
un plan, ou par la fenêtre de création, reçoit maintenant son propre compte
de service, avec les seuls droits qu'emploie CAPHV v0.10.1, relevés appel
par appel dans sa source (voir Capacités, section 4). Ses VMs vont dans un
namespace à elles, `<nom>-vms`, créé et supprimé avec le cluster (un modèle
qui fixe un autre namespace de VMs est refusé). La console crée l'identité, le compte du fournisseur de cloud et
sa liaison avec l'identité de la personne qui lance l'exécution ; le
contrôle vérifie d'avance, en un seul lot de `SelfSubjectAccessReview`, que
cette personne peut créer, modifier et supprimer chacun de ces objets et
détient chaque droit qu'ils accordent.

**Contrôles avant d'agir.** En plus des constats de la 2.4.0, pour tous les
clusters du plan ensemble et en une seule lecture du cluster : CPU et
mémoire libres, place Longhorn pour chaque disque (répliques sur des nœuds
distincts), adresses libres de chaque pool, droits de la personne, version
de Harvester, version de la pile, et les règles de la fenêtre de création
pour chaque cluster.

- Bloquants : `capi-bundle-missing`, `capi-bundle-no-turtles`,
  `capi-needs-harvester-1.9`, `capi-version-differs` (un plan ne met jamais
  une pile à niveau, même un plan qui n'a que la phase clusters : la mise à
  niveau se fait dans Cluster API > Installation), `capi-stack-missing`,
  `capi-install-admin` (la pile est à installer et le rôle n'est pas admin),
  `capi-template-missing`, `cluster-var-missing`, `cluster-name-taken`,
  `identity-rules-unknown`, `identity-name-reserved`,
  `target-namespace-forced`, `vm-namespace-taken`, `cloud-provider-binding-taken`,
  `capi-check-failed`, `fleet-name-twice`, `fleet-cpu-short`,
  `fleet-memory-short`, `fleet-storage-short`, `fleet-replicas-nodes`,
  `fleet-pool-short`, `rights-missing`.
- Avertissements : `rights-unchecked`, `cluster-failed`, et les
  avertissements de la fenêtre de création (un seul nœud de plan de
  contrôle, version non éprouvée...).
- Information : `cluster-exists`, `capi-later`, `refs-later`,
  `fleet-dhcp-leases`, `fleet-sizes`.

Un manque que la fenêtre de création signale seulement en avertissement
bloque un plan : la chaîne tourne sans surveillance.

**Suivre une exécution.** Étapes `phase-capi` (sous-étape `capi-providers`)
et `phase-clusters` (une `k8s-<nom>` par cluster). Les actions filles sont
rangées sous l'action `deploy:` dans l'Activité (déplier la ligne) et dans
le dock (le parent et sa fille en cours). Le résultat consigne, par
cluster, si cette exécution l'a créé, la révision du modèle et ses actions
filles.

**Arrêt, reprise, échec.** Un arrêt laisse le cluster en cours de création
aller à son terme et saute les suivants. Un arrêt pendant que l'exécution
attend un cluster qu'elle a repris met fin à cette attente aussitôt.
Annuler l'action fille elle-même supprime ce cluster. **Reprendre**
revérifie la pile, puis prend chaque cluster à son tour : un cluster déjà
fait par ce plan et pas en échec est attendu ; un cluster en échec reste en
erreur (`cluster-failed`) : supprimez-le, puis reprenez. Un plan ne supprime
jamais un cluster de lui-même.

**Supprimer un cluster enfant.** Depuis Cluster API > Clusters, comme
avant. Le `Cluster` part d'abord ; une fois disparu, les objets de
l'identité sont retirés un par un, par leur nom, puis ce que la console a
généré et le namespace qu'elle a créé. Si un objet de l'identité ne peut
être ni lu ni supprimé (un droit manquant, par exemple), la suppression se
termine en erreur et garde le namespace : corrigez la cause et supprimez à
nouveau, la suppression reprend là.

**Ligne de commande.** `harvester-plans capi-template
list|show|save|delete` ; `check` et `apply` couvrent les deux phases.
`harvester-capi create --identity restricted|console` (restreinte par
défaut ; console seulement pour un diagnostic), `--origin
plan=P,template=M,run=ID`, `harvester-capi check --many --spec FICHIER`
(une liste JSON, contrôlée d'un bloc), `harvester-capi state --name
ns/nom`.

**Vu en réel (2.5.0).** Voir le CHANGELOG de la 2.5.0 pour les bancs, les
durées et ce qui n'est pas vérifié.

### Historique des actions bare-metal (2.1.0)

L'onglet **Historique** liste les actions bare-metal de l'Activité :
installations et jonctions (`baremetal-install`), séries
(`baremetal-batch`), démarrages de découverte (`baremetal-discover`) et
gestes d'alimentation (`bmc-power`), celles en cours d'abord. Il se sert
du filtre d'action de l'Activité, côté serveur : les actions anciennes
sont trouvées aussi. Chaque ligne mène à l'action dans l'Activité
(`#activity/action/<id>`), où s'ouvre son journal. Tant qu'une action
tourne, l'onglet la relit toutes les cinq secondes.

### Redfish : ce que la 1.78.0 a changé

Vérifié contre un émulateur Redfish pilotant une machine imbriquée, au-delà
de l'iLO 4 de HPE des premières installations :

- le média virtuel est celui du **gestionnaire du système visé**
  (`Links.ManagedBy`), pas le premier gestionnaire que le BMC liste ; un BMC
  qui gère plusieurs systèmes monterait sinon l'ISO d'une autre machine ;
- les liens `VirtualMedia` que publie le BMC sont suivis (Redfish 2020.4 et
  suivants placent le média virtuel sous le système) ; le chemin historique
  `<gestionnaire>/VirtualMedia/` n'est qu'un repli ;
- un BMC écrit `hôte:port` fonctionne ;
- une insertion de média qui ne répond qu'une fois l'image téléchargée est
  attendue ; l'état « inséré » du lecteur n'est pas cru, un BMC pouvant
  l'afficher avant que l'image soit là ;
- le préflight ne refuse plus un BMC qui n'est pas un iLO.

---

## Pourquoi l'ISO est remasterisé

Le mode sans opérateur de Harvester s'active depuis la **ligne de
commande du noyau** :

```
harvester.install.automatic=true
harvester.install.config_url=<url>
ip=dhcp rd.neednet=1
```

Un média virtuel monte une image telle quelle ; il n'offre aucun moyen
d'y ajouter des arguments noyau. D'où la remasterisation.

**Pourquoi les arguments réseau comptent.** L'installeur va chercher sa
configuration à `config_url` *avant* de configurer le moindre réseau. Une
installation PXE a déjà un réseau, monté par le paramètre `ip=` du noyau.
Le même ISO démarré depuis un média virtuel n'en a aucun : sans
`ip=dhcp rd.neednet=1`, l'installeur reste là indéfiniment sans jamais
émettre une seule requête, et sans rien afficher qui l'explique. Ces deux
arguments sont toujours injectés.

**Comment l'injection se fait.** L'ISO officiel n'a qu'une configuration
grub porteuse des entrées de menu ; celle de l'EFI ne fait que la
charger. Ses entrées se terminent déjà par `${extra_iso_cmdline}`, une
variable qu'elle source depuis `/boot/grub2/harvester.cfg`. Poser cette
seule variable suffit, et cela vaut pour toutes les entrées et les deux
firmwares à la fois. Sur un ISO plus ancien dépourvu de cette variable,
les arguments sont ajoutés directement aux lignes de noyau.

**Comment l'image est reconstruite.** Elle ne l'est pas : elle est
recopiée avec `xorriso -boot_image any replay`, qui rejoue le dispositif
d'amorçage d'origine. C'est essentiel, parce que l'amorce El Torito de
cet ISO est **UEFI seulement et cachée** (ce n'est pas un fichier de
l'arborescence) : une image reconstruite par `mkisofs` ne démarrerait
pas du tout. La copie évite aussi de déballer 7,7 Go : l'étape prend
environ deux minutes.

Trois choses sont vérifiées sur l'image produite, chacune échouant
silencieusement sinon :

- le label de volume est toujours **`COS_LIVE`** : le noyau monte son
  système de fichiers racine via `root=live:CDLABEL=COS_LIVE`, donc
  perdre le label produit un ISO qui démarre puis ne se retrouve plus ;
- l'amorce El Torito a survécu ;
- les arguments d'installation sont bien dans l'image.

Si l'ISO source ne porte aucune entrée grub, la remasterisation refuse au
lieu de produire une image silencieusement inerte.

## Regarder une installation qui ne dit rien

L'installeur tourne sur la première console de la machine. Rien n'en
remonte vers la console qui l'a lancée : une installation bloquée
ressemble exactement à une installation lente. Deux arguments noyau
aident, tous deux disponibles dans la section **Avancé** du formulaire :

- `console=ttyS1,115200` renvoie l'installation sur le port série virtuel
  du BMC, auquel on peut ensuite s'attacher. Vérifier quel port COM votre
  BMC expose : sur les iLO 4 de HPE c'est `Com2`, d'où `ttyS1`, et le
  réglage BIOS `SerialConsolePort` doit d'abord être mis à `Virtual`.
- `harvester.install.skipchecks=true` passe outre les contrôles matériels.
  Dans un profil d'installation, `skipchecks: true` l'ajoute (1.83.2).
  Sans lui, une machine qui échoue à un prérequis minimal abandonne
  l'installation et ne le dit que sur cette console invisible.

---

## Pourquoi un second serveur HTTP

La console sert en **HTTPS avec un certificat auto-signé** et exige une
authentification sur tout. Un BMC qui va chercher un ISO ne sait faire ni
l'un ni l'autre : il ne s'authentifie pas, et il ne fait pas confiance à
ce certificat.

Le serveur d'artefacts est donc un listener séparé et minimal, en HTTP
simple (port 8091 par défaut). Ce n'est pas un serveur de fichiers : il
n'expose que deux chemins, chacun adressé par un jeton aléatoire à usage
unique et à durée de vie limitée, révoqué dès la fin de l'installation.

```
GET /pxe/iso/<jeton>.iso
GET /pxe/config/<jeton>.yaml
```

Il gère les requêtes `Range` de HTTP, parce que les BMC téléchargent les
images de plusieurs gigaoctets par morceaux.

Ce port doit être joignable **depuis le réseau du BMC**. Sur un hôte avec
pare-feu, pensez à l'ouvrir.

La console prend le port dans `HARVESTER_OPS_PXE_PORT` (8091 par défaut).
Elle n'en choisit jamais un autre d'elle-même : l'adresse donnée au BMC et
à l'installeur doit être celle que vous avez ouverte. Si le port est pris,
le déroulé échoue à son étape `serve` avec un message qui nomme le port,
avant toute action sur l'alimentation. Le listener démarre avec la
première installation ou découverte et s'arrête à la fin de la dernière.

Tout échec d'un déroulé, prévu ou non, termine l'action en échec. La
configuration servie (elle porte le jeton du cluster), l'ISO
remasterisée et les jetons sont effacés ; le message de l'étape ne donne
que le type de l'erreur, jamais un chemin ni un secret.

---

## Dépannage

**L'installeur a lu sa configuration, puis plus rien (1.80.0).** Vu sur une
vraie lame aux cartes à 1 Gbit/s : les contrôles matériels de l'installeur
de Harvester refusent une installation automatique sur une carte de gestion
sous 10 Gbit/s (et sur trop peu de mémoire ou de disque), le disent
seulement sur la console de la machine, et ne demandent jamais l'image
d'installation. La console le remarque désormais dix minutes après la
lecture de la configuration (aucune demande de l'image hors du BMC) et
s'arrête avec cette explication, au lieu d'attendre une heure ; l'aperçu et
le préflight préviennent déjà quand l'inventaire de découverte montre une
carte de gestion sous 10 Gbit/s. Si la machine vous convient, ajouter
`harvester.install.skipchecks=true` aux arguments noyau supplémentaires.

**« pas de média virtuel » sur la ligne de la machine.** Le BMC n'expose aucun
lecteur CD ou ne sait pas amorcer dessus. Sur iLO, vérifier la licence
Advanced. Aucun contournement depuis la console : sans média virtuel, la
machine doit être installée autrement.

**La découverte renvoie un inventaire vide.** La machine est éteinte et
n'a jamais POSTé depuis le dernier redémarrage du BMC. L'allumer,
attendre le POST, redécouvrir.

**Le BMC ne va jamais chercher l'ISO.** Il n'atteint pas le serveur
d'artefacts : vérifier la route depuis le réseau du BMC vers la console,
et le pare-feu de cet hôte.

**La machine démarre l'ISO mais attend un opérateur.** Les arguments
noyau ne sont pas arrivés dans le chargeur réellement utilisé
(typiquement : EFI patché et pas le BIOS hérité, ou l'inverse). L'étape
de remasterisation indique combien de `grub.cfg` elle a patchés ; un ISO
Harvester sain en donne au moins deux.

**L'installation se déroule mais l'API ne répond jamais sur la VIP.** La
VIP est sur un autre sous-réseau que l'interface de management, ou elle
est déjà prise. Vérifier qu'elle est libre avant de lancer.

---

Voir [capabilites.md](capabilites.md) pour le reste du toolkit et
[architecture.md](architecture.md) pour l'articulation des surfaces.

### Réseau initial et identité des interfaces agrégées

L'installeur télécharge sa configuration avant d'en appliquer le réseau.
La console dérive donc les arguments dracut des paramètres rendus : cartes,
bond, VLAN, IPv4 statique ou DHCP, passerelle et DNS. L'aperçu expose
`boot_network_args` ; un argument réseau contradictoire est refusé avant
la création de l'action.

Sur une console avec plusieurs réseaux, le média virtuel utilise l'adresse
routée vers le BMC ; la configuration et l'image d'installation utilisent
celle routée vers l'IP de gestion statique prévue. L'API accepte deux
surcharges : `advertise_host` fixe l'adresse des deux côtés, comme avant, et
`installer_advertise_host` celle de l'installeur seulement. L'adresse de
l'installeur n'est déduite de la route vers l'IP statique prévue que si
aucune n'est posée ; en DHCP, elle reprend celle du média. Chaque réseau doit
pouvoir joindre son adresse HTTP et son port.

Les membres d'un bond peuvent partager leur MAC courante. Pour éviter les
renommages udev (`rename4`, puis `interface not found`), les règles utilisent
`bonding_slave/perm_hwaddr` pour les membres déjà agrégés. Les noms sont
communs au démarrage et au YAML ; des règles équivalentes sont conservées
pour le système installé, sans modifier les MAC sélectionnées. Ces noms
(`hvbootN`) et leurs arguments `ifname=` ne sont écrits que dans la
configuration servie par l'ISO remasterisée, qui porte les arguments de
démarrage correspondants. Une configuration rendue pour un autre démarrage
(votre propre PXE, un banc imbriqué) garde les MAC sans ces noms.

Le correctif initrd n'est ajouté que si le bond compte au moins deux
membres désignés par leur MAC : un membre seul ne partage sa MAC avec
personne. Il accepte uniquement le générateur ifname vérifié, identique
dans Harvester 1.8.2 et 1.9.0. Un générateur inconnu est refusé pendant la préparation de
l'ISO, avant toute modification d'alimentation. Une autre version d'ISO
demande une vérification préalable. Les contrôles matériels restent actifs.
Le timeout sans téléchargement de l'image n'identifie pas l'erreur réelle :
consulter la console de l'installeur.

## Cartes VM/client facultatives lors de la jonction

Après l'import du YAML de jonction, activer **Configurer les cartes VM après la
jonction** dans **Réseau VM/client**, sélectionner une configuration réseau comme
modèle et choisir les cartes client par MAC. Les cartes de gestion restent
séparées. Le modèle fournit le réseau de cluster et les attributs du bond,
notamment son mode et son MTU. Ses noms de cartes et son sélecteur sont remplacés.

L'aperçu présente le YAML d'installation puis un plan distinct exécuté après la
jonction. Ce plan appartient à la console, pas au schéma YAML de l'installeur.
Cette option concerne uniquement la jonction à un cluster déclaré. Désactivée,
elle ne provoque aucune lecture API supplémentaire.

Limiter d'abord les configurations générales, par exemple `vm-bond`, à leurs
hôtes existants dans **Réseaux**, en conservant leur couverture. La console refuse
les sélecteurs qui incluraient aussi le nouvel hôte ; elle ne modifie jamais les
configurations existantes.

Après la jonction, la console vérifie à nouveau les labels réels du nœud, le
modèle et le réseau. LinkMonitor doit résoudre chaque MAC en une seule carte
physique libre. Un lien inactif produit un avertissement sans bloquer la création
du bond. L’adresse MAC du bond du modèle n’est pas copiée. Les pools de disques
sont configurés avant cette étape réseau facultative.
La console valide la création avec un dry-run serveur, crée une VlanConfig propre
à cet hôte puis attend son VlanStatus prêt. La gestion, le stockage et le mode
maintenance restent inchangés.

Une erreur de création ou d'attente laisse Harvester installé. Vérifier la
configuration indiquée dans **Réseaux** avant d'utiliser l'hôte pour des VM.
Aucune réinstallation, nouvelle tentative d'une création incertaine ni suppression
d'un bond potentiellement actif n'est effectuée automatiquement.
