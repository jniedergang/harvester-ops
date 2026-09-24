# Capacités

harvester-ops est une console moderne pour exploiter un ensemble de
clusters SUSE Harvester. Elle repose sur trois piliers : **tous les
clusters dans une seule interface**, **les opérations courantes
automatisées** (VMs, maintenance des nœuds, arrêt et démarrage ordonnés,
Terraform, Cluster API, installations bare-metal), et **chaque événement
conservé**, qu'il vienne de la console ou d'ailleurs. Cette page fait le
tour de chaque domaine de capacité, son rôle, et où il vit (ligne de
commande ou console web).

Tout est **multi-cluster** (un seul `config.yaml` déclare N clusters) et
chaque opération mutative est **tracée comme une action** avec ses logs
live et un historique conservé.

---

## 1. Séquençage électrique (CLI + console)

Le cœur d'origine, et la seule partie nécessaire pour un déploiement
shutdown/startup pur. Disponible en bash auditable et reflété dans la
console.

- **Shutdown gracieux** — 8 étapes ordonnées : pre-flight → snapshot etcd
  → snapshot VM optionnel → arrêt VM ordonné → maintenance Longhorn →
  cordon → arrêt workers → arrêt control-plane. Un filet de sécurité en
  échec (snapshot etcd raté, volumes de VM encore attachés) **annule**
  la séquence en mode non interactif ; passer `--force` (ou cocher
  Forcer dans la console) pour poursuivre malgré tout. L'attente
  Longhorn ne suit que les volumes attachés par des VMs — les volumes
  tenus par des pods (monitoring, logs d'upgrade) s'arrêtent avec le
  node et sont listés comme ignorés.
- **Startup** — 5 étapes : démarrer le premier control Les nodes avec un `wol_mac` en
  config s'allument par **Wake-on-LAN** (sans intervention) ; et seules
  les VMs que le shutdown a réellement arrêtées redémarrent, chacune
  avec sa run strategy d'origine — les VMs volontairement éteintes
  avant l'arrêt restent éteintes.
-plane → démarrer
  le reste → attendre les nodes Ready → restaurer l'état du cluster →
  redémarrer les VMs en ordre de groupe inversé (parallèle dans un
  groupe).
- **Groupes d'ordonnancement VM** — les VMs s'arrêtent/redémarrent par
  groupes configurables : séquentiel *entre* groupes, parallèle *dans* un
  groupe, avec priorité par groupe.
- **Invariants garantis** : aucune perte de données Longhorn, aucune perte
  de quorum etcd en cours de shutdown, arrêt ACPI gracieux avant
  détachement du stockage.

→ Référence pas à pas complète : [procedure-operationnelle.md](procedure-operationnelle.md).

```bash
harvester-status   --cluster prod
harvester-shutdown --cluster prod --interactive
harvester-startup  --cluster prod
```

## 2. Cycle de vie VM (CLI partiel, console complet)

Gérer les machines virtuelles KubeVirt sans quitter la console.

- Liste VM par namespace avec `runStrategy` et phase VMI.
- Start/stop en masse via `runStrategy` (une VM ou tout un namespace).
- **Snapshots** — créer un `VirtualMachineBackup` (type=snapshot) par VM.
  **Restauration guidée** : la restauration propose de prendre d'abord
  un snapshot de sécurité de l'état courant et d'arrêter la VM
  automatiquement (les deux cochés par défaut), pour que « snapshoter
  l'état vivant, arrêter, revenir en arrière » soit une seule action
  tracée — et si le retour arrière était une erreur, le snapshot de
  sécurité ramène exactement où on en était.
- **Créer des machines virtuelles** : un bouton Créer dans l'onglet VMs
  ouvre un panneau portant *tous* les réglages d'une VM, parce qu'il rejoue
  les huit sections de l'éditeur au lieu de proposer un formulaire réduit :
  tout ce qui est éditable est réglable à la création. Nom, namespace,
  nombre d'instances (au-delà d'une, les noms sont numérotés web-01,
  web-02, et chaque instance reçoit ses propres PVC), et démarrage ou non
  après création. « Valider seulement » demande au cluster de vérifier le
  manifeste sans rien créer, et la configuration peut être enregistrée
  comme template Harvester réutilisable. L'éditeur de disques affiche la
  place restante par storage class, qui n'est pas l'espace « disponible »
  annoncé par Longhorn : son ordonnanceur applique deux contraintes à la fois
  et c'est la plus serrée qui décide, répliques comprises, et ce que les
  autres disques de la même VM réclament est soustrait. La taille d'un disque
  est proposée depuis la taille virtuelle de l'image, et la création peut
  partir d'un template Harvester existant. Un disque neuf naît amorçable
  depuis une image (les suivants en disques de données vierges), jamais en
  volume existant à attacher, qui est le cas rare et le seul sans taille ni
  storage class à choisir. Pour un disque d'image la storage class est
  affichée mais verrouillée : c'est celle de l'image, qui porte l'image de
  base, et en choisir une autre donnerait un disque vide. Les disques
  vierges la laissent libre.
- **Cette VM est-elle sur le bon réseau, par la bonne carte ?** La section
  Réseau de l'éditeur de VM porte un onglet Chemin de connexion à côté de
  l'éditeur d'interfaces. Il dessine la chaîne de la VM jusqu'au cuivre :
  vNIC, réseau attachable, bridge, bond, carte physique, chaque maillon
  portant ce qu'on sait de lui, et ce qui est déclaré tenu à part de ce qui
  tourne, parce qu'un écart entre les deux est justement ce qu'on cherche.
  Deux sondes ne partent qu'à la demande, chacune étant un aller-retour
  SSH : l'une résout le port hôte exact qui porte la VM en comparant sa MAC
  dans les espaces de noms réseau des pods, l'autre écoute brièvement sur la
  carte physique une annonce LLDP et dit quel switch et quel port ont
  répondu. Une VM arrêtée montre son chemin déclaré, annoncé comme tel.
  La sonde LLDP donne le nom du switch, le port (description et
  identifiant), l'adresse de gestion du switch, son châssis et sa
  description, un par ligne. Vérifiée sur de vraies trames avec le cluster
  de test à trois nœuds, dont le bridge de l'hôte émet du LLDP ; le switch
  du LAN de production n'en émet pas, et la sonde le dit après écoute.
- **Migrer** (1.45.0) : une fenêtre, trois destinations. **Un autre nœud**
  du même cluster, c'est la migration à chaud (la VM continue de tourner,
  avec les contrôles de migrate-info) ; **un autre cluster** et **un
  fichier** sont décrits plus bas.
- **Console VNC** — console graphique complète dans le navigateur
  (noVNC via un relais WebSocket vers la sous-ressource `vnc` de
  KubeVirt). Montre tout le boot — firmware, GRUB, kernel — grâce à
  une reconnexion automatique qui s'attache dès que qemu expose
  l'affichage ; clavier/souris, Ctrl-Alt-Suppr et ajustement
  fenêtre / 1:1 inclus ; le bandeau de la console porte aussi les
  commandes électriques de la VM (démarrer / arrêt gracieux / reset
  dur, par la sous-ressource `restart` de la VM comme `virtctl restart`, qui
  redémarre quelle que soit la stratégie de démarrage) et des raccourcis
  vers les snapshots et les paramètres. Les consoles ouvertes sur une VM
  réinitialisée s'y rattachent seules à son retour. Accès protégé par tickets éphémères à usage
  unique délivrés par un endpoint authentifié ; le kubeconfig doit
  avoir `get virtualmachineinstances/vnc` (vérifié par la matrice de
  permissions).
  **Plusieurs personnes peuvent utiliser la même console en même temps.**
  KubeVirt n'accepte qu'une connexion VNC par VM et ferme la précédente
  quand une autre arrive : la console tient donc une seule connexion par
  VM et la partage entre tous les navigateurs. Chacun voit le même écran
  et peut taper et cliquer, et la ligne d'état dit combien de personnes le
  partagent (et lesquelles, quand des comptes sont configurés). Pour qu'un
  arrivant puisse rejoindre à tout moment, la connexion partagée utilise
  des encodages sans état (Hextile, Raw) ; l'extension clavier de QEMU est
  conservée, donc un clavier non américain comme l'AZERTY tape juste.
  Quand un autre client extérieur prend l'écran (l'interface Harvester par
  exemple), la console le dit et s'arrête au lieu de le reprendre en
  boucle ; **Reprendre la main** se reconnecte à la demande, et les autres
  consoles de la session rejoignent d'elles-mêmes. Avec la délégation
  d'identité, chaque personne qui rejoint est vérifiée par le cluster sous
  sa propre identité (`get virtualmachineinstances/vnc`), et la connexion
  vers KubeVirt porte l'usurpation.
- **Édition inline** — modifier CPU / mémoire et la charge cloud-init,
  puis appliquer. **Les disques et interfaces réseau ont des éditeurs
  visuels** (v1.8.0) : une carte par disque/NIC avec des listes
  alimentées par le cluster (PVC existants, images Harvester, storage
  classes, réseaux multus). Les nouveaux disques — vierges ou depuis
  une image — passent par le mécanisme `volumeClaimTemplates` de
  Harvester ; bus, ordre de boot, cdrom, attachement bridge/masquerade,
  modèle de NIC et MAC sont éditables, avec validation côté client et
  dry-run serveur. Un repli JSON brut reste disponible. Un onglet
  **Firmware** couvre le mode de démarrage (BIOS / UEFI / UEFI +
  Secure Boot, avec la fonctionnalité SMM que le Secure Boot exige),
  le TPM 2.0 avec état persistant, le type de machine et le numéro de
  série — ce sans quoi les invités récents comme Windows 11 ou SLE 16
  refusent de démarrer. **Calcul** ajoute les plafonds d'ajout à chaud
  (sockets CPU max, mémoire invité max), le modèle de CPU
  (host-model / host-passthrough) et, dans un repli, les réservations
  d'ordonnancement. **Général** édite le nom d'hôte invité et les tags
  Harvester (`tag.harvesterhci.io/*`). Les disques portent leurs options
  fines (numéro de série, mode de cache, partageable, lecture seule,
  thread d'E/S dédié) et les cartes réseau un ordre de boot pour le
  **démarrage PXE** — la séquence de boot est partagée entre disques et
  NICs, et une collision est attrapée avant l'apiserver. Un onglet
  **Placement** épingle la VM par sélecteur de node, ajoute des
  tolérances, et l'éloigne (ou la rapproche) des VMs portant un tag
  donné ; l'affinité de node que Harvester gère pour le réseau est
  affichée en lecture seule et laissée intacte, et le pinning CPU
  (placement dédié, thread émulateur isolé, passthrough NUMA) se trouve
  dans Calcul. L'onglet Firmware porte
  aussi les **périphériques** : console série, graphique, ballon
  mémoire, pointeur tablette USB et watchdog — chacun écrit uniquement
  s'il diffère du défaut KubeVirt.

**Passthrough PCI et SR-IOV** : le sélecteur liste les périphériques
découverts par Harvester sous la forme adresse, nœud et pilote ; seul un
périphérique réservé dans Harvester (pilote `vfio-pci`) est utilisable, et
la console n'en réserve jamais un elle-même. Vérifié sur le cluster de test
à trois nœuds avec du matériel émulé (IOMMU virtuel) : une carte réseau, et
une fonction virtuelle SR-IOV d'une autre carte, choisies dans l'éditeur,
sont arrivées sur le bus PCI de l'invité. Sur Harvester, le SR-IOV passe par
une telle fonction virtuelle en passthrough PCI. Aucun vrai GPU n'a été
essayé. **Livré mais non vérifié** (signalé dans l'UI, en style
d'avertissement) : les attachements d'interface macvtap et SR-IOV de
KubeVirt, qui exigent des composants que Harvester ne livre pas. Tout le
reste de cette page a été exercé pour de vrai. L'onglet
  Cloud-init gagne un **assistant** (v1.8.1) qui génère du YAML
  cloud-config et network-data v1 propres dans les éditeurs — nom
  d'hôte, utilisateurs (mot de passe, sudo sans mot de passe, clés SSH
  Harvester ou clés publiques brutes), paquets, commandes, adressage
  DHCP ou statique — on relit, puis on enregistre. Les champs à
  valeurs usuelles proposent un menu de suggestions qui ne bloque
  jamais la saisie libre, et les éléments ajoutés aux listes sont
  auto-nommés sans dupliquer un voisin (eth0 pris, le suivant est
  eth1).

La CLI expose le sous-ensemble start/stop via `harvester-status` /
`-shutdown -N <ns>`.

### Déplacer une VM vers un autre cluster, l'exporter, l'importer (1.45.0)

La fenêtre « Migrer » d'une VM (son bouton de migration, ou
`harvester-vm-transfer` en ligne de commande) la déplace ou la copie vers un
autre cluster déclaré, ou l'exporte dans une archive importable plus tard,
ailleurs. Console et ligne de commande emploient le même moteur et écrivent
la même archive.

**Le contrôle préalable.** Avant toute modification, les deux clusters sont
lus et chaque constat s'affiche dans la langue de l'interface, blocages en
tête ; « Lancer » reste grisé tant qu'il en reste un. Il vérifie :

- que la cible répond et fait tourner KubeVirt ;
- que le nom est libre et que le namespace existe (ou sera créé) ;
- que chaque réseau et chaque classe de stockage de la VM a un équivalent
  sur la cible ;
- la place allouable Longhorn. Plus de répliques demandées que de nœuds sur
  la cible donne un avertissement, pas un manque de place : les volumes
  tournent dégradés ;
- les adresses MAC déjà prises sur la cible : Harvester refuse un doublon,
  même pour une VM arrêtée ;
- les périphériques d'hôte et l'épinglage à un nœud, qui ne voyagent pas ;
- une version de Harvester plus ancienne sur la cible.

**Deux moteurs, choisis pour vous.**

- **Sauvegarde Harvester**, quand les deux clusters partagent leur cible de
  sauvegarde (NFS ou S3) et que la restauration peut réussir : chaque réseau
  garde son nom sur la cible et chaque classe de stockage y existe.
  Le déroulé :
  - une `VirtualMachineBackup` est prise ;
  - la cible est poussée à relire sa cible de sauvegarde (Harvester ne le
    fait que sur demande) ;
  - la VM est restaurée en nouvelle VM, et les images dont elle est née
    suivent.

  Seul ce moteur permet l'**arrêt court** : une première sauvegarde VM en
  marche, puis une seconde après l'arrêt, qui ne porte que ce qui a changé.
- **Copie par la console** sinon. Chaque disque est figé en image
  temporaire sur la source (la VM est arrêtée pour cela, et relancée
  aussitôt si elle doit rester en marche), lu en flux, puis importé sur la
  cible par un `DataVolume` CDI dans un volume ordinaire de la classe
  choisie. Les nœuds de la cible viennent chercher les disques sur l'hôte de
  la console, en HTTP, sur le port 8094 par défaut : l'ouvrir, ou régler
  `transfer: serve_address: hôte:port` dans `config.yaml`.

**États finaux, choisis par l'opérateur.**

- **Source :** laissée en marche (une copie, avec de nouvelles adresses
  MAC), arrêtée et annotée comme déplacée, ou supprimée avec ses volumes.
- **Cible :** démarrée ou laissée arrêtée.
- **Ordre des gestes :** la cible est vérifiée (en marche, ou volumes liés)
  avant que la source soit arrêtée pour de bon ou supprimée.
- **Retour arrière :** tout ce que le transfert crée porte l'étiquette
  `harvester-ops.io/transfer`. Un échec ou une annulation depuis le dock le
  supprime et remet la source dans son état de départ.
- **Provenance :** la VM cible garde une annotation
  `harvester-ops.io/transferred-from`.

**Le magasin d'exports.** Le bouton « Exports » de la vue Machines
virtuelles liste les archives, avec leur cluster et leur version d'origine,
leur date, leur taille, et si elles sont complètes. On peut en télécharger
une pour l'emporter vers un site isolé, l'importer dans un cluster déclaré,
ou la supprimer.

Une archive (`.hvx`) est un tar ordinaire qui contient :

- la VM nettoyée ;
- ses disques, tels que Harvester les sert (gzip) ;
- des sommes SHA-256, vérifiées pendant l'import.

Elle contient aussi les secrets cloud-init de la VM : elle est créée en 0600
et se garde comme un secret.

En ligne de commande :

```bash
harvester-vm-transfer check   --from prod --vm default/web-01 --to secours
harvester-vm-transfer migrate --from prod --vm default/web-01 --to secours \
    --mode short --source stopped --target started
harvester-vm-transfer export  --from prod --vm default/web-01 --out /srv/exports/
harvester-vm-transfer import  --to site-isole --in /srv/exports/web-01-20260925-002842.hvx \
    --namespace apps --create-namespace --map-net default/lan=apps/vlan10
```

Codes de sortie :

- 0 : terminé ;
- 1 : échec, transfert défait ;
- 2 : refusé par le contrôle préalable ;
- 3 : annulé, transfert défait.

Vérifié sur de vrais clusters (Harvester 1.8.2 et 1.9.0) : disques comparés
bit à bit après une copie, un export et un import entre versions.

## 3. Observabilité cluster (console)

- **Le réseau de l'hôte, un switch virtuel à la fois (Fabrique).** La
  vue Réseau ci-dessous regarde le réseau depuis les VMs ; celle-ci le lit
  comme un exploitant ESXi lit un Standard Switch : un bloc par switch, de
  gauche à droite, avec les réseaux et leurs VMs à gauche, le switch au
  milieu et les cartes physiques à droite.
  - Un **cluster network** est un switch qui porte le nom de son bridge
    (lu dans les réseaux attachables, pas déduit d'une convention de
    nommage). Son en-tête reprend la politique du VlanConfig (mode de bond,
    MTU) et le nombre de ports de charges ; son uplink est le bond qui
    tient ses cartes.
  - Un **provider network kube-ovn** est aussi un switch : ses subnets sont
    à gauche avec leur VLAN (le VLAN 0 s'affiche « sans étiquette »), CIDR,
    passerelle, VPC et le réseau attachable par lequel une VM les rejoint ;
    sa carte à droite.
  - L'**overlay OVN** est un switch interne : en pointillé, sans carte
    physique, ce qu'il est. Un réseau d'overlay lié à aucun subnet est
    signalé.
  - Chaque réseau liste les **VMs qui y sont branchées**, les démarrées
    d'abord, le reste se dépliant à la demande. Les VMs du réseau de pod
    sont listées à part : ce réseau n'a pas de bridge et sort par le
    routage du nœud.
  - Chaque carte montre son état et, comme ESXi, son débit et son duplex
    (« 1000 Full »). Une carte sans lien est en rouge avec un câble
    pointillé. Les cartes rattachées à aucun switch restent affichées, pour
    que rien ne manque sans le dire.
  - Cliquer une carte ou un bond ouvre son détail : MAC, maître, débit,
    duplex, MTU, changements de porteuse, compteurs de trafic et d'erreurs,
    mode de bond et miimon, plus la sonde LLDP sur une carte physique. Ce
    que seul le nœud sait est lu en SSH une fois par nœud et par session,
    pas à chaque rafraîchissement.
  - Chaque nom, adresse et CIDR a son bouton de copie.
  Harvester ne publie pas ses bridges Open vSwitch : sans aide, les ports
  de charges côté kube-ovn restent invisibles. C'est dit plutôt que deviné,
  et la vue propose de poser un `LinkMonitor` en lecture seule qui les
  révèle, par une action tracée et retirable depuis le même bandeau. La vue
  fonctionne aussi sur un cluster sans l'addon kube-ovn.

- **Vue Cluster, un bloc par hôte** (même disposition que les autres
  vues). Chaque hôte montre son état (prêt, isolé, en maintenance), ses
  rôles, son adresse et deux jauges : vCPU et mémoire donnés à ses VMs en
  marche, face à ce que l'hôte peut donner. Au-delà de 100 %, la jauge dit
  que l'hôte est surchargé. Chaque VM est une carte qui dit ce qu'elle
  consomme : vCPU, mémoire, disques (« 20 GiB » ou « 2 disques · 60 GiB »),
  réseaux et première adresse. Survoler une carte, ou lui donner le focus
  au clavier, affiche le détail complet : chaque disque avec sa taille, sa
  storage class et son rang d'amorçage, chaque carte réseau avec sa MAC et
  toutes ses adresses, le système invité et les interfaces que seul
  l'invité connaît, la stratégie de démarrage. Les VMs arrêtées sont
  regroupées à part. Un filtre réduit les cartes par nom, namespace,
  adresse, MAC, réseau ou système invité. Cliquer une VM ouvre ses actions
  (notes, éditer, console, snapshots, migrer, démarrer, arrêter, supprimer
  derrière le verrou destructif). Un seul appel kubectl groupé par
  rafraîchissement, tailles des disques comprises.
- **Isoler, réintégrer, mettre un nœud en maintenance**, depuis le panneau
  d'un hôte. Isoler et réintégrer posent le `spec.unschedulable` du nœud,
  comme Harvester, en actions tracées avec confirmation. Le webhook
  d'admission de Harvester refuse d'isoler, ou de mettre en maintenance, le
  dernier nœud encore disponible (aucun autre nœud ni isolé ni en
  maintenance) : la console applique la même règle en amont ; sur un tel
  nœud, le bouton Isoler est affiché désactivé avec la raison, et le
  serveur refuse par un 409 au lieu de lancer une action vouée à l'échec.
  La **maintenance** est celle de Harvester : la console la demande comme le
  fait l'interface de Harvester (annotation `harvesterhci.io/drain-requested`),
  et c'est le contrôleur de Harvester qui draine le nœud. Avant toute
  demande, la console montre ce qui se passerait, avec les règles de
  Harvester :
  - un nœud qui est le seul plan de contrôle est refusé, comme un nœud du
    plan de contrôle quand un autre est déjà en maintenance ;
  - les VMs qui **migreront** ;
  - celles qui **ne le peuvent pas**, et pourquoi (la dernière réplique saine
    d'un de leurs volumes est sur ce nœud, KubeVirt les dit non migrables à
    chaud, ou aucun autre nœud ne satisfait leurs règles de placement). Tant
    qu'il y en a, la maintenance est refusée sauf forçage ; le forçage est
    derrière le verrou destructif, arrête ces VMs, et elles **restent
    arrêtées** ensuite ;
  - celles que le **drain arrêtera** : KubeVirt ne migre une VM à l'éviction
    que si sa stratégie d'éviction le demande (`LiveMigrate` ou
    `LiveMigrateIfPossible`, ou le défaut du cluster). L'interface de
    Harvester la pose, mais les VMs créées par kubectl ou Terraform souvent
    pas. Pour chacune, la console dit si elle revient sur un autre nœud
    (stratégie de démarrage `Always` : un redémarrage, pas une migration) ou
    reste arrêtée, et comment la faire migrer ;
  - celles dont un **volume n'est pas sain** : Longhorn ne migre pas un
    volume dont une réplique attend sa reconstruction, et la maintenance peut
    alors durer bien plus longtemps ;
  - les **volumes attachés utilisés par des pods** dont la seule réplique
    saine est sur le nœud : Longhorn ne la lâche pas et le drain attend
    indéfiniment (le contrôle de Harvester ne regarde que les volumes des
    VMs) ;
  - les VMs étiquetées pour être arrêtées pendant la maintenance.
  L'action tracée suit Harvester jusqu'à ce que le nœud soit en maintenance
  (10 minutes au plus) et rapporte un refus de son contrôleur. Sortir de
  maintenance rend le nœud de nouveau planifiable et redémarre les VMs
  étiquetées pour redémarrer après. Entrer et sortir de maintenance
  demandent le rôle `admin`. Les VMs créées depuis la console reçoivent
  désormais `LiveMigrateIfPossible`, comme celles de l'interface de
  Harvester. **Vérifié sur un cluster de test à trois nœuds** : isoler et
  réintégrer ; le refus d'isoler le dernier nœud disponible (la console et
  le webhook de Harvester le refusent de même) ; la maintenance avec et sans
  forçage, où chaque VM a fini comme annoncé (migrée avec la même instance,
  redémarrée ailleurs, arrêtée) ; le refus pour plan de contrôle occupé ; la
  sortie de maintenance.
- **Vue Réseau, un bloc par réseau** (même disposition que la Fabrique).
  À gauche, chaque VM branchée sur ce réseau avec ce qu'elle y a vraiment :
  nom de l'interface, MAC, adresses, nom de l'interface dans l'invité,
  état du lien, modèle et liaison, les VMs démarrées d'abord. Les
  interfaces que seul l'agent invité connaît (docker0 et consorts) sont
  listées à part : elles ne sortent par aucun réseau du cluster. À droite,
  par où le réseau sort : le bridge, son bond et ses cartes ; pour un
  underlay kube-ovn, le subnet, sa passerelle et sa carte ; pour l'overlay
  et le réseau de pod, rien de physique, et c'est dit. Les réseaux sans VM
  sont listés en bas. Elle lit la même donnée que la Fabrique : aucun
  appel de plus.
- **Vue Stockage, lue comme un datastore** : un bloc par moteur de
  stockage. À gauche, chaque storage class avec sa politique (répliques,
  sort à la libération, image source), la place qu'elle peut encore
  allouer (le même chiffre que le panneau de création de VM) et ses
  volumes rangés par VM dans l'ordre d'amorçage, disques CD-ROM et ISO
  signalés. Les volumes montés par des pods et ceux que personne ne
  réclame sont regroupés à part. À droite, les disques des nœuds avec une
  jauge de ce qui est écrit et de ce qui est promis, la place restante et
  le nombre de répliques. Cliquer un volume ou un disque ouvre son détail
  (claim, VM, pods, image, taille demandée et écrite, santé, placement des
  répliques). Un **volume orphelin** (réclamé par aucune VM, monté par
  aucun pod, connu de Longhorn et non attaché) peut être **supprimé depuis
  son détail**, derrière le verrou destructif et une confirmation, par une
  action tracée. Quand la dernière charge qui l'a utilisé peut revenir (un
  volume de StatefulSet), le détail le dit d'abord. Le serveur revérifie
  avant de supprimer et refuse un claim que monte un pod en cours. Un seul
  appel kubectl groupé.
- **Volumes dégradés, expliqués et corrigés.** Un bandeau en tête de la vue
  Stockage compte les volumes qui demandent de l'attention (en panne,
  dégradés, à risque de démarrer dégradés) avec leur cause principale, et
  ouvre le plus urgent. Le détail de chaque volume dit pourquoi, d'après ce
  que rapporte Longhorn : plus aucune réplique saine, reconstruction
  désactivée (un arrêt gracieux la laisse coupée si le démarrage n'a pas pu
  la rétablir), reconstruction en cours avec son pourcentage, nouvelle
  réplique en préparation, réplique en échec en attente de réutilisation,
  pas assez de nœuds pour le nombre de répliques, aucun disque avec la
  place, réplique sur un nœud ou un disque indisponible. Chaque cause vient
  avec la marche à suivre, et trois d'entre elles avec une correction en un
  clic : ramener le nombre de répliques à ce que le cluster peut porter,
  réactiver la reconstruction, reconstruire tout de suite une réplique en
  échec. Chaque correction montre sa commande kubectl équivalente, demande
  confirmation, et s'exécute en action tracée qui guette l'effet pendant
  une minute et le dit quand rien n'a changé. Le serveur relit le cluster
  et refait le diagnostic avant d'agir, avec des valeurs qu'il calcule
  lui-même ; il ne touche jamais un volume en panne, ne descend jamais sous
  une réplique, ne supprime jamais la dernière copie saine, et ne réactive
  pas la reconstruction pendant un arrêt ou un démarrage du cluster. Un
  nœud **tombé, ou revenu sans son disque encore prêt**, n'est pas un nœud
  qui manque : ses répliques sont dites indisponibles, sans correction en un
  clic, puisque Longhorn les reprend au retour du nœud ; reconstruire tout
  de suite ou réduire le nombre de répliques changerait une panne passagère
  en données ou en redondance perdues. Un volume détaché qui a une réplique
  sur un tel nœud est montré à risque. Vérifié sur un cluster de test à
  trois nœuds en coupant l'alimentation d'un nœud : un volume sans plus
  aucune copie saine (montré en panne, sans correction proposée) est revenu
  seul avec le nœud ; la correction « reconstruire maintenant » a été
  appliquée depuis la console à une réplique en échec sur un nœud sain, et
  Longhorn en a reconstruit une neuve. Les volumes Longhorn dont le PVC a
  été supprimé sont montrés aussi, signalés comme tels.
- **Métriques d'overview** : nodes, VMs en marche, nombre de volumes
  Longhorn et limite de rebuild, table des nodes.
- **`/metrics` Prometheus** — compteurs/durées d'actions, gauge in-flight,
  issues des appels kubectl **ventilées par cluster**.
- **`/healthz/ready`** — readiness probe renvoyant 503 si la config, les
  clusters ou la base d'actions sont en défaut (`/healthz` pour la
  liveness).

## 4. Cluster API — clusters RKE2 downstream (console)

Provisionner et opérer des clusters Kubernetes downstream sur Harvester
via le Cluster API Provider Harvester (CAPHV).

- **Installer la stack depuis un bundle airgap** — cert-manager, CAPI
  core, les providers RKE2 bootstrap/control-plane, CAPHV et une
  ClusterClass — avec progression pas à pas dans le dock.
- **Créer des clusters** via un wizard guidé (sizing, image, SSH, réseau,
  CNI), avec aperçu YAML (dry-run) avant apply. Les manifestes sont
  produits par l'outil `caphv-generate`, qui n'est pas livré dans le
  tarball : sans lui sur l'hôte, la création répond que l'outil manque.
- **Opérer** les clusters managés : scaler (patch la topologie),
  télécharger le kubeconfig, voir spec/conditions/machines, supprimer. Les
  montées de version Kubernetes ne sont pas implémentées.
- **Gestion des bundles** — bundles airgap horodatés avec marqueur actif,
  inspect, upload, download, et contrôle de compatibilité avec la version
  Harvester.

## 5. Terraform — infrastructure as code (console)

Piloter le provider Terraform pour Harvester depuis des déclarations
sauvegardées.

- **Déclarations** — bundles nommés et persistés de N ressources
  hétérogènes (VMs, images VM, clés SSH, HCL brut), éditées section par
  section (Specs / Disques / Réseaux / Cloud-init) et appliquées en un
  coup.
- **Apply / destroy** avec streaming live du plan et de l'apply ; modale
  de confirmation typée sur chaque point d'entrée de destroy.
- **Éditer les ressources déployées** — chaque ressource appliquée écrit
  un sidecar JSON pour recharger et éditer sa spec d'origine depuis le
  sous-onglet Live.
- **Mettre à jour le provider depuis la console** : installer une autre
  version de `terraform-provider-harvester` sans accès shell à l'hôte.
  Au choix : une version (la release officielle est téléchargée puis
  comparée au `SHA256SUMS` publié), une URL vers un miroir interne, ou le
  téléversement de l'archive depuis un poste sans aucun accès réseau
  sortant. Le sous-onglet Install nomme le binaire actif, sa version et sa
  provenance, et un clic rend la main à celui du livrable. Chaque
  workspace est réinitialisé à son prochain apply ; le state Terraform
  n'est jamais touché. La même installation se fait en CLI :
  `bin/harvester-provider-install.py 1.7.3 --dest <rep>`.

## 6. Bare-metal (console)

Transformer un serveur vierge en node Harvester opérationnel sans y
toucher. Marche à suivre complète dans **[bare-metal.md](bare-metal.md)**.

- **Découverte BMC / Redfish** — pointer un ou plusieurs endpoints BMC et
  lire les profils des nodes : modèle, numéro de série, BIOS, mémoire,
  NICs avec leurs MAC, état d'alimentation, disques amorçables, et la
  possibilité ou non d'installer la machine. Fonctionne sur iLO 4/5,
  iDRAC 9 et Redfish standard ; les chemins System et Manager sont
  résolus selon le constructeur au lieu d'être supposés.
- **Actions d'alimentation** via Redfish, avec la même résolution
  dynamique des chemins.
- **Magasin d'images d'installation** — télécharger un ISO Harvester côté
  serveur, en flux, avec un pourcentage suivi. Les 7,6 Go de l'image ne
  passent jamais par le navigateur et ne sont pas livrés dans le tarball.
- **Installation sans opérateur** — choisir un ISO, renseigner le node
  (nom d'hôte, disque d'installation, NIC de management, adressage, VIP,
  DNS, token, mot de passe OS), et la console remasterise l'ISO pour
  l'installation zéro-touch, le publie derrière un jeton à usage unique,
  le monte en média virtuel, programme une amorce unique sur `Cd` et
  allume la machine. Suivi étape par étape dans le dock, du préflight
  jusqu'à l'API Harvester qui répond sur la VIP.
- **Préflight contre l'inventaire périmé** — un BMC éteint rejoue
  l'inventaire de son *dernier POST*, parfois vieux de plusieurs mois.
  L'installation allume la machine et lit le matériel réel avant de
  décider.
- **Arguments noyau supplémentaires** pour les cas que les valeurs par
  défaut ne couvrent pas : passer outre les contrôles matériels, ou
  renvoyer toute l'installation sur la console série du BMC, seul moyen
  de regarder une installation sans opérateur se dérouler.
- **Aucun identifiant stocké** — l'utilisateur et le mot de passe du BMC
  restent dans la page le temps de la session ; le token du cluster et le
  mot de passe OS n'apparaissent jamais dans une réponse, un libellé
  d'action ou une ligne de log.

## 7. Support aux opérations (CLI + console)

- **Un cluster éteint le dit.** Un cluster déclaré dont le serveur d'API ne
  répond pas est reconnu en deux secondes environ, et la console nomme
  l'adresse injoignable au lieu de tourner dans le vide jusqu'à 75 secondes
  puis d'abandonner sans rien expliquer. Les réponses qui arrivent après une
  bascule sont jetées : l'échec d'un cluster mort ne peut plus s'afficher
  comme l'état d'un cluster sain.
- **Config multi-cluster** — déclarer les clusters dans `config.yaml` ;
  ajouter / éditer / supprimer et uploader kubeconfig + clé SSH depuis la
  console ; tests de connexion kubeconfig et SSH.
- **Notes collaboratives** — notes rich-text synchronisées en live
  (Yjs + Tiptap) attachées par cluster et par node, synchronisées entre
  onglets et opérateurs.
- **Support bundles** — collecter logs et état cluster dans un tarball
  avec **anonymisation** (placeholders stables comme `<<NODE-1>>`,
  `<<IP-NODE-1>>`) ; un outil de dé-anonymisation séparé inverse
  l'opération depuis la table de mapping pour le passage au support.

## 8. Droits et rôles

Jusqu'à la 1.30.0, la console vérifiait un mot de passe et rien d'autre :
tout compte entré pouvait éteindre un cluster, supprimer une VM ou détruire
un workspace Terraform.

- **Trois rôles**, déclarés dans `/etc/harvester-ops/roles.yaml` : `viewer`
  lit, `operator` fait le travail mutatif courant (VMs, snapshots, applies
  Terraform), `admin` y ajoute ce qui coupe un service ou change la
  configuration de l'outil : séquençage électrique, maintenance des nœuds,
  déclarations de cluster, bare-metal, magasin d'ISO et provider Terraform.
- **Appliqué au centre, en refus par défaut.** Toute requête qui modifie
  quelque chose exige au moins `operator`, et une liste explicite de chemins
  exige `admin`. Un point d'entrée ajouté demain est protégé sans que
  personne ait à y penser, et un test parcourt toute la table des routes pour
  vérifier qu'aucun n'échappe au garde.
- **Un refus dit ce qui manque**, en nommant le rôle requis et celui qu'on a,
  au lieu d'un 403 nu.
- **Des rôles exigent des identités.** Sans htpasswd, personne n'est
  distinguable : rien n'est bridé, et la console le dit plutôt que de mettre
  discrètement tout le monde, exploitant compris, en lecture seule.

- **Les comptes du cluster** (Réglages > Comptes du cluster) : les comptes
  déclarés sur le cluster Harvester sélectionné, leur activation et qui
  détient l'administration, d'un seul regard. L'administration s'accorde et
  se retire, les comptes s'activent et se désactivent. Les délégations
  orphelines sont signalées : un sujet détenant cluster-admin sans compte
  derrière lui signifie que le compte a été supprimé et sa délégation non,
  et en recréer un portant le même identifiant la lui rendrait en silence.
  La création d'un compte local avec mot de passe n'est volontairement pas
  offerte : Harvester le range sous forme de clé dérivée dont cette console
  ne devinera pas le schéma.

### L'identité que voit le cluster (1.32.0)

Les rôles ci-dessus sont appliqués par la console. Seuls, ils ne changeaient
rien pour le cluster : le kubeconfig partagé vaut `system:admin`, groupe
`system:masters`, un superutilisateur câblé dans l'apiserver qui
court-circuite entièrement la RBAC. Quel que soit l'humain au clavier, le
cluster n'appliquait aucune règle propre.

Chaque compte de la console peut désormais être associé à une identité de
cluster, que tous les appels kubectl portent : la RBAC de Harvester
s'applique enfin.

```yaml
# /etc/harvester-ops/roles.yaml
default_role: viewer
identity:
  delegate: true          # éteint par défaut : une mise à jour ne change rien
  deny_unmapped: true     # un compte sans correspondance est refusé (défaut)
users:
  alice: operator                     # forme courte, toujours valide
  bob:
    role: admin
    cluster_user: user-2fhwx          # l'identifiant Harvester, pas le login
    cluster_groups: [harvester-admins]
```

- `cluster_user` est l'**identifiant** de l'objet
  `users.management.cattle.io` (`user-2fhwx`), pas le login. Réglages >
  Comptes du cluster les liste.
- **Un compte sans correspondance est refusé** tant que la délégation est
  active, plutôt que de retomber sur le kubeconfig administrateur partagé,
  qui est précisément ce que la délégation supprime. `deny_unmapped: false`
  pour l'autoriser.
- **Un refus du cluster se lit comme un refus** : 403, avec la raison donnée
  par le cluster et l'identité refusée, au lieu d'une erreur serveur opaque.
- Le badge de rôle du menu dit lequel des trois états s'applique : délégué,
  non délégué, ou délégué sans identité.
- **Parité CLI.** Les scripts prennent la même identité dans
  l'environnement :

```bash
HARVESTER_OPS_AS=user-2fhwx HARVESTER_OPS_AS_GROUPS=harvester-admins \
  ./bin/harvester-shutdown.sh --cluster harv1 --dry-run
```

Vérifier ce qu'un cluster accorde réellement à une identité avant de s'y
fier :

```bash
kubectl --kubeconfig <kc> auth can-i list virtualmachines.kubevirt.io -A \
  --as user-2fhwx
```

**Ce que ce n'est toujours pas.** La console DÉTIENT le kubeconfig
administrateur : c'est une frontière que le cluster applique, pas un coffre,
et un défaut de cette couche redonnerait les pleins pouvoirs. L'identité est
déclarée localement, non prouvée par un fournisseur d'identité ; la faire
venir d'OIDC est la suite.

## 9. Transversal

- **Un menu latéral qui rend l'écran.** Le menu de gauche est un rail
  d'icônes de 56 px qui se déplie par-dessus la page sous le pointeur et se
  replie quand il s'en va. C'est un calque, pas une colonne : l'ouvrir ne
  redimensionne jamais la zone de travail, si bien que le panneau de détail
  de la topologie, les canvas et les tableaux ne sautent pas sous les yeux.
  Il s'épingle depuis son pied de menu quand on veut les libellés en
  permanence, et l'épinglage est mémorisé.
- **Une barre qui liste toutes les fenêtres ouvertes.** Consoles, réglages
  de VM, snapshots, migrations et notes gardent chacun une puce au-dessus
  du dock tant qu'ils sont ouverts, et pas seulement une fois minimisés. Un
  clic range la fenêtre, un autre la rappelle ; une fenêtre cachée derrière
  une autre revient au premier plan. Les fenêtres d'une même machine
  s'empilent sous son nom, écrit une fois : trois fenêtres sur une VM
  coûtent une entrée au lieu de trois qui répètent `default/leap156`.
- **Des chargements qui se ressemblent partout** : un onglet lent (le
  diagnostic Cluster API interroge le cluster et peut prendre dix secondes)
  floute sa carte derrière un voile nommé au lieu de la vider, avec un
  seuil anti-clignotement pour qu'une réponse rapide n'affiche rien.
- **Il cesse de redemander la même chose au cluster.** Chaque invocation de
  kubectl paie un démarrage de processus complet avant même de toucher au
  réseau, alors la console regroupe ce qu'elle peut : la surveillance de
  cluster interroge ses cinq types de ressources en un seul appel, et le
  script de statut en fait deux groupés au lieu de cinq. Un onglet de
  navigateur caché cesse d'interroger (y revenir rafraîchit aussitôt), et la
  surveillance ralentit après cinq minutes sans requête humaine. Les relevés
  de supervision sur `/metrics` et `/healthz` ne comptent pas pour un
  humain, sinon la console ne serait jamais au repos. Réglages :
  `HARVESTER_OPS_WATCH_IDLE_AFTER` (défaut 300 s) et
  `HARVESTER_OPS_WATCH_IDLE_INTERVAL` (défaut 120 s).
- **Tracking d'actions + dock** — un dock bas persistant montre les
  actions en cours et récentes sur chaque onglet, avec streaming live des
  steps/logs en SSE (reconnexion automatique). Une action en échec porte
  l'erreur sous-jacente (dernière ligne stderr `kubectl` / script) dans le
  dock, la table Activité et le panneau de détails — jamais un simple
  `exit 1`.
- **Les changements faits hors de la console apparaissent aussi.** Une
  surveillance relève les namespaces, les images de VM (avec la progression
  du téléversement), les réseaux, les claims de volume et les VMs (avec
  leurs changements d'état) de chaque cluster, et chaque changement fait
  ailleurs (interface de Harvester, kubectl, Rancher) devient une action
  terminée dans le dock et l'onglet Activité. Sa dernière photo est gardée
  sur disque, à côté de l'historique des actions (`watch/` à côté de la
  base d'actions, ou `HARVESTER_OPS_WATCH_STATE_DIR`) : ce qui a changé
  pendant l'arrêt de la console, ou pendant qu'un cluster était
  injoignable, est signalé au premier tour suivant, et marqué comme tel ;
  un téléversement d'image encore en cours est de nouveau suivi. Seul ce
  qui sert à comparer est gardé (noms et quelques champs d'état), lisible
  par le seul compte du service.
- **Activité filtrable** — l'onglet Activité filtre par cluster, statut et
  type d'action, avec une recherche libre sur les identifiants, les actions
  et les messages d'erreur. Les filtres s'appliquent à tout l'historique en
  SQL, pas à la page déjà affichée : les échecs d'un cluster peu actif sont
  donc trouvés quand même, et le compteur dit combien d'entrées sont
  affichées sur combien existent.
- **Historique d'actions durable** — les 500 derniers runs (avec leurs
  événements step/log) sont persistés en SQLite et resservis par l'onglet
  Activité et son replay de détails, y compris après redémarrage de l'UI.
  L'éviction mémoire n'affecte que l'attachement SSE live, jamais
  l'historique visible.
- **Changement de cluster qui change vraiment de cluster.** Choisir un
  autre cluster floute la page derrière un voile de chargement qui le
  nomme, recharge la vue affichée, puis rend la main. Cela compte parce
  que le défaut est silencieux : les chiffres du cluster précédent
  restent à l'écran et se lisent comme ceux du nouveau, ce qui est la
  façon dont on finit par agir sur la mauvaise machine.
- **Chaque log dit sur quel cluster il porte.** Les journaux CLI
  s'ouvrent sur un en-tête (version, action, cluster, hôte, utilisateur,
  kubeconfig) et préfixent chaque ligne par `[cluster]` : une ligne
  recopiée dans un ticket dit encore de quoi elle parle. Les échecs
  `kubectl` côté serveur nomment aussi le cluster, et les métriques se
  ventilent par cluster.
- **Les vues lentes floutent leur propre zone.** La même transition, mais
  cantonnée : la topologie d'un gros cluster ne floute que son panneau le
  temps de charger, en nommant ce qu'elle va chercher, le reste de la
  page restant net et utilisable. Rien ne s'affiche en dessous de 250 ms,
  et jamais sur un rafraîchissement de fond.
- **Internationalisation** — cinq langues complètes (EN, FR, DE, ES, IT),
  avec un test de parité qui casse la build sur une clé manquante.
- **Icônes** — un seul jeu SVG monochrome, [Lucide](https://lucide.dev)
  (licence ISC, vendoré dans le tarball, jamais chargé depuis le réseau),
  héritant de `currentColor` : il couvre tous les thèmes et les deux
  modes, y compris la topologie où les mêmes glyphes sont dessinés comme
  images de nœud. Il remplace les emoji, qui mélangeaient images en
  couleur pleine et glyphes fins, variaient selon la plateforme et se
  lisaient mal à la taille d'un bouton. `scripts/gen-icons.py` régénère
  le jeu depuis la release épinglée.
- **Thèmes** — 5 thèmes de couleur × sombre/clair. Le thème SUSE par
  défaut suit l'identité suse.com — palette pin/jade et la fonte
  officielle SUSE, vendorée dans le tarball (airgap, licence OFL).
- **Accessibilité** — focus clavier visible, focus trap de dialogue,
  tooltips sur chaque contrôle (désactivables globalement), entièrement
  localisés en **cinq langues** (EN, FR, DE, ES, IT) sur toutes les
  surfaces, onglets avancés compris.

---

## Ce qui nécessite quoi

| Vous voulez… | Il vous faut |
|---|---|
| Juste un shutdown/startup sûr | Les scripts CLI seuls (pas d'UI, pas de podman) |
| Un dashboard + gestion VM | La console web |
| Provisionner des clusters downstream | Console web + un bundle airgap CAPHV |
| Des VMs gérées par Terraform | Console web + le binaire du provider Terraform |
| Un fonctionnement 100 % hors-ligne | Le tarball : wheels bundlés + image OCI |

Voir [architecture.md](architecture.md) pour la façon dont les surfaces se
posent sur le moteur partagé, et [installation.md](installation.md) pour
démarrer.
