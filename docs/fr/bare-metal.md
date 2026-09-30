# Bare-metal : installer Harvester sur une machine vierge

L'onglet Bare-metal transforme un serveur vide en node Harvester
opérationnel sans que personne aille jusqu'à la baie, branche une clé USB
ou clique dans l'installeur. La console pilote la machine par son BMC.

Rien de magique ni de spécifique ici : c'est le mode d'installation
zéro-touch de Harvester, piloté de bout en bout par la console.

---

## Ce qu'il faut avant de commencer

| Prérequis | Pourquoi | Comment le vérifier |
|---|---|---|
| Le BMC parle Redfish | Tout passe par lui | La découverte liste le node |
| Le BMC expose un **média virtuel CD** | L'ISO est monté par le réseau | La carte du node propose **Installer Harvester** ; sinon elle affiche *pas de média virtuel* |
| Le BMC sait amorcer sur `Cd` | La machine doit démarrer une fois sur cet ISO | Même indication que ci-dessus |
| La console est joignable **depuis le BMC** | C'est le BMC qui télécharge l'ISO | Un `curl` vers la console depuis une machine du réseau BMC |

Sur les iLO 4/5 de HPE, le média virtuel est une fonction **iLO
Advanced**. Sans cette licence le BMC n'expose aucun lecteur CD, et la
console le dit tout de suite plutôt que d'échouer trente secondes après
le début d'une installation.

La carte du node indique aussi combien de disques le firmware sait
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

**Images d'installation** télécharge un ISO **côté serveur, en flux**.
Le fichier ne transite jamais par votre navigateur : un ISO Harvester
pèse environ 7,6 Go, et un upload de cette taille depuis un navigateur
serait tamponné en mémoire à l'arrivée.

Collez l'URL de l'éditeur, lancez le téléchargement, suivez le
pourcentage dans le dock. Le magasin montre ce qui est sur le disque et
combien de place il reste.

L'ISO n'est **volontairement pas** embarqué dans le tarball de release :
le toolkit entier pèse 135 Mo, l'ISO à lui seul cinquante fois plus.

### 2. Découvrir le BMC

Saisissez une ou plusieurs adresses de BMC, un utilisateur et un mot de
passe. Les identifiants ne sont **jamais stockés côté serveur** : ils
restent dans la page le temps de la session, et chaque action les
renvoie.

Chaque BMC joignable revient sous forme de carte : modèle, numéro de
série, BIOS, mémoire, NICs avec leurs adresses MAC, état d'alimentation,
et la possibilité ou non d'installer la machine.

> **Attention avec une machine éteinte.** Un BMC ne relit pas le matériel
> hors tension : il rejoue l'inventaire du **dernier POST**. Sur une
> machine qui n'a pas démarré depuis des mois, cet inventaire peut être
> vieux de plusieurs mois. L'installation commence donc par un préflight
> qui allume la machine et lit la réalité avant de décider quoi que ce
> soit.

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

### 4. Suivre l'installation

L'installation est une action trackée comme les autres : elle apparaît
dans le dock dès la confirmation et diffuse ses étapes.

| Étape | Ce qui se passe |
|---|---|
| `preflight` | Allume la machine si besoin, attend le POST là où le BMC le publie (HPE), vérifie qu'il y a un disque : l'inventaire de découverte d'abord, puis les cibles d'amorce UEFI de HPE, puis les disques Redfish ; sans aucune de ces sources, un avertissement (l'installeur vérifie lui-même son disque) |
| `remaster` | Patche l'ISO pour qu'il s'installe sans opérateur (voir plus bas) |
| `serve` | Publie l'ISO et la configuration derrière des jetons à usage unique |
| `bmc-insert` | Monte l'ISO en média virtuel, et attend la réponse du BMC (jusqu'à 15 min, `HARVESTER_OPS_BM_INSERT_WAIT`) |
| `bmc-boot` | Programme une amorce **unique** sur `Cd` |
| `power` | Redémarre la machine sur l'ISO |
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

Si le déroulé échoue avant le début de l'installation, une machine que le
préflight a allumée est éteinte à nouveau ; une machine trouvée allumée
est laissée telle quelle.

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

---

## Dépannage

**« pas de média virtuel » sur la carte du node.** Le BMC n'expose aucun
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
