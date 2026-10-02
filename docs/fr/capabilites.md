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
- **Startup** — 5 étapes : démarrer le premier control-plane → démarrer
  le reste → attendre les nodes Ready → restaurer l'état du cluster →
  redémarrer les VMs en ordre de groupe inversé (parallèle dans un
  groupe). Les nodes avec un `wol_mac` en config s'allument par
  **Wake-on-LAN** (sans intervention) ; et seules les VMs que le shutdown
  a réellement arrêtées redémarrent, chacune avec sa run strategy
  d'origine : les VMs volontairement éteintes avant l'arrêt restent
  éteintes. « Tournait » se lit sur l'instance de la VM, pas seulement
  sur sa run strategy (1.48.1) : une VM éteinte depuis son propre système
  garde `RerunOnFailure`, le défaut de Harvester, et n'est plus relancée ;
  une VM `Manual` qui tournait est redémarrée par la sous-ressource
  `start`, rétablir sa stratégie ne suffisant pas à la lancer.
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

### Le menu d'actions d'une VM, comme dans Harvester (1.60.0)

Chaque VM de la liste a un bouton **⋮**. Il ouvre un menu rangé comme celui
de Harvester, construit d'après l'état réel de la VM (lu à l'ouverture) : un
geste qui ne s'applique pas est grisé et dit pourquoi, au lieu d'échouer
après le clic (redémarrage doux sans agent invité, migration sans autre nœud
prêt).

- **Alimentation** : Redémarrer (dans le délai de grâce), Redémarrage doux
  (l'invité redémarre lui-même, par l'agent invité), Pause / Reprise, Arrêt
  forcé (l'instance est coupée sans attendre l'invité).
- **Protection** : Prendre une sauvegarde (vers la cible), Prendre un
  instantané.
- **Disques** : ajouter un volume pendant que la VM tourne (un volume
  existant du même namespace qu'aucune VM n'utilise, en scsi, virtio ou
  sata), détacher un volume branché ainsi, éjecter un CD-ROM (et supprimer
  son volume une fois la VM arrêtée).
- **Migration** : Migrer, vers un nœud choisi ou n'importe lequel ;
  Abandonner la migration en cours.
- **Copie** : Cloner, avec ou sans les données (avec, chaque volume est un
  clone Longhorn de l'original ; sans, les volumes repartent de leur image,
  ou vides) ; les adresses MAC et IP ne sont pas recopiées et le cloud-init
  est copié dans un Secret à lui. Générer un template, ou une nouvelle
  version d'un template existant, avec ou sans les données (avec, chaque
  disque est d'abord exporté en image).
- **YAML** : Modifier le YAML, Télécharger le YAML (ci-dessous).
- **Supprimer** : choisir les volumes qui partent avec la VM (le disque
  système est coché, comme dans Harvester) ; son Secret cloud-init part
  aussi, sauf si une autre VM l'utilise.

La liste propose aussi **Redémarrer**, **Arrêt forcé** et **Migrer** sur les
VMs cochées. Chaque geste est une action suivie (dock, Activité), exécutée
par `harvester-resources vm <action>`, utilisable seul.

### Modifier une VM en marche, comme dans Harvester (1.61.0)

Le menu d'une VM porte aussi les gestes à chaud de Harvester. Chacun lit
d'abord l'état de la VM et se grise, avec la raison, quand il ne s'applique
pas.

- **Modifier CPU et mémoire** pendant que la VM tourne, jusqu'aux maximums
  posés à la création (« Activer le branchement à chaud CPU et mémoire »
  dans les réglages de calcul : un cœur par socket, maximums quatre fois les
  valeurs de départ s'ils ne sont pas donnés, limites égales aux maximums,
  1 Gio de mémoire au moins). KubeVirt applique le changement en déplaçant
  la VM à chaud ; la console attend que l'invité ait ses nouveaux CPU et ait
  pris la mémoire (son noyau doit connaître virtio-mem, comme les
  distributions récentes).
- **Insérer une image** dans un lecteur CD-ROM SATA vide, et l'**éjecter**,
  pendant que la VM tourne ; le lecteur reste, le volume de l'image est
  supprimé.
- **Ajouter une carte réseau** sur un réseau de VMs en pont, et la
  **débrancher** ; le changement est appliqué par une migration à chaud, ou
  au prochain redémarrage quand il n'y a pas d'autre nœud.
- **Migrer un volume** de la VM en marche vers un volume existant et inutilisé
  du namespace (autre classe, plus grand) ; **annuler** pendant la copie. Une
  fois la bascule de KubeVirt faite, l'annulation est refusée : revenir en
  arrière redémarrerait la VM sur l'ancienne copie.
- **Créer une planification** pour cette VM (la fenêtre Backups s'ouvre
  dessus) et poser son **quota d'instantanés**.
- **Ajouter un accès** : un mot de passe pour un compte, ou des clés SSH pour
  des comptes, posés dans l'invité par son agent au prochain redémarrage. Le
  mot de passe passe par un fichier privé, jamais sur une ligne de commande
  ni dans un journal.
- **Console série** dans un terminal, et **journaux** du pod virt-launcher de
  la VM (opérateurs).

La liste des VMs montre les CPU, la mémoire, les adresses IP et le nœud, se
trie sur chacun, et un filtre garde les VMs qui correspondent à un nom, une
IP, un nœud ou un label (`clé=valeur`). La fenêtre de réglages écrit les
champs de Harvester : nom affiché, description (la clé que lit Harvester),
système d'exploitation, stratégie de maintenance, mémoire réservée.

### Plus à la création et dans les réglages d'une VM (1.62.0)

- **IP statique** par carte réseau, écrite là où Harvester la lit
  (`static-ip.harvesterhci.io/<carte>`) ; la liste des VMs la montre en
  premier. Sur un réseau overlay (kube-ovn), Harvester en fait l'adresse
  kube-ovn de la carte, et l'invité la reçoit par DHCP si le sous-réseau le
  sert (vérifié sur harv1 : 10.62.0.50 dans l'invité 41 s après le
  démarrage) ; sur un réseau VLAN, elle est seulement montrée et se
  configure dans l'invité.
- **Labels, labels d'instance et annotations** dans la fenêtre de réglages,
  en lignes clé et valeur ; les clés que gèrent Harvester, KubeVirt et
  Kubernetes sont cachées et laissées intactes.
- **Fichier de réponses Windows** (autounattend.xml) à la création : gardé
  dans un Secret et donné au programme d'installation de Windows sur un
  CD-ROM SATA nommé `sysprep`, comme le fait Harvester.
- **Volumes de système de fichiers (virtiofs)** à la création : une
  ConfigMap, un Secret ou un ServiceAccount du namespace partagé avec
  l'invité (un de chaque au plus), monté par `mount -t virtiofs <nom>
  /mnt/<nom>`. Le noyau de l'invité doit connaître virtiofs (vérifié avec une
  image Tumbleweed ; une image Leap minimale ne l'a pas).

### Modifier et télécharger le YAML (1.60.0)

Tout objet que Harvester permet de modifier en YAML le peut, depuis sa
ligne : VMs, images, volumes, classes de stockage, clés SSH, secrets, réseaux
de VMs, templates, add-ons, planifications, sauvegardes et instantanés. Le
texte laisse de côté l'état et l'historique des champs ; **Vérifier** le fait
juger par le cluster sans rien changer (les contrôles de Harvester
répondent) ; **Enregistrer** remplace l'objet. Le texte garde la version de
l'objet : si quelqu'un l'a changé depuis l'ouverture, le cluster refuse au
lieu d'écraser son changement, et la fenêtre le dit. Les Secrets, les
réglages et les configurations d'add-on ne se lisent en YAML qu'en
administrateur, leur texte portant des valeurs. En ligne de commande :
`harvester-resources yaml`.

### Le cloud-init à la création d'une VM (corrigé en 1.60.0)

La section Cloud-init de la fenêtre de création était perdue : la VM était
créée sans. Le user-data et le network-data partent maintenant dans un
Secret par VM, référencé par la VM comme le fait Harvester, avec deux champs
du formulaire de Harvester : **Clés SSH** (leur clé publique est ajoutée à
`ssh_authorized_keys`, et la VM les affiche) et **Installer l'agent invité**
(coché par défaut). Dans les réglages d'une VM, enregistrer le cloud-init
est une action suivie, et marche maintenant aussi sur une VM qui n'en a pas
encore (un Secret est créé et branché) ou qui l'a en inline (rangé dans un
Secret).

### Sauvegardes, instantanés et leurs planifications (1.58.0)

Le bouton **Sauvegardes**, à droite du sélecteur d'espace de noms dans
Machines virtuelles, ouvre une fenêtre avec les quatre onglets du menu
« Backup and Snapshots » de Harvester, pour l'espace choisi ou tous. Elle
reste ouverte à côté des VMs et revient après un rechargement. La cible de
sauvegarde est rappelée en haut (type et adresse ; les clés d'une cible S3
n'arrivent jamais dans la page).

- **Planifications** : une VM sauvegardée ou mise en instantané chaque
  heure, jour ou semaine (ou selon un cron), combien de copies sont
  gardées, et après combien d'échecs d'affilée Harvester la suspend ; la
  planification est dite en clair (« chaque dimanche à 03:30 »). Suspendre,
  reprendre, supprimer (les copies restent). Un planning plus fréquent
  qu'une heure est refusé avant que Harvester ne le refuse.
- **Sauvegardes de VM** (sur la cible) et **Instantanés de VM** (dans le
  cluster) : état, taille, cible, planification d'origine. En prendre un
  maintenant, en restaurer un dans une nouvelle VM (en gardant au besoin
  les adresses MAC, refusées sur un réseau où l'originale tourne encore) ou
  par-dessus la VM d'origine (arrêtée ; confirmation), laissée arrêtée si
  on le veut ; supprimer.
- **Instantanés de volumes** : en restaurer un dans un nouveau volume ; un
  instantané de volume pris avec un instantané de VM part avec lui, pas
  seul.

Chaque geste est une action suivie dans le dock, par
`harvester-resources backup|schedule|volsnap` en ligne de commande. Vérifié
sur harv1 : un instantané d'une VM arrêtée (12 s), restauré dans une
nouvelle VM arrêtée (9 s), son instantané de volume restauré dans un nouveau
volume (lié), une sauvegarde vers le NAS (NFS), une planification
hebdomadaire créée, suspendue, reprise et supprimée, puis tout supprimé.

Ajouté en 1.68.0, ce qui manquait encore à la fenêtre face à Harvester :

- **Modifier** une planification : sa fréquence, les copies gardées et les
  échecs tolérés (la VM et le type restent, comme dans Harvester). La
  fenêtre part des valeurs actuelles ; le déclencheur de Harvester suit le
  nouveau rythme.
- **Gel du système de fichiers** à la prise d'une sauvegarde ou d'un
  instantané : avec l'agent invité, Harvester gèle les systèmes de fichiers
  de la VM pendant la copie, au plus ce délai (5 s à 5 min ; le défaut de
  Harvester est 1 s). « 0 s », sans limite, n'est pas proposé : Harvester
  n'appelle jamais le dégel lui-même, c'est le délai qui l'assure. À partir
  de Harvester 1.9 ; les versions plus anciennes l'ignorent, ce que dit le
  dock.
- **Anciens volumes** au remplacement d'une VM depuis une sauvegarde : les
  garder, ou les supprimer (confirmé à part : ils ne se récupèrent pas).
  Une restauration depuis un instantané les garde toujours, comme l'exige
  Harvester.

Vérifié : sur harv1, un instantané avec gel de 5 s d'une VM dont l'agent est
connecté (Harvester a gelé ses systèmes de fichiers, la VM n'est pas restée
gelée) et une planification hebdomadaire modifiée puis supprimée ; sur le
banc à trois nœuds, une VM remplacée depuis une sauvegarde NFS avec
suppression de ses anciens volumes.

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
- **Secret cloud-init :** celui qu'une copie par la console ou un import
  recrée appartient à la VM cible, comme chez Harvester : supprimer la VM
  le supprime (1.47.0).

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

**Récupérer un export, et l'amener ailleurs (1.47.0).** À la fin d'un
export, la fenêtre « Migrer » nomme l'archive et sa taille, avec trois
boutons : Télécharger (sur ce poste), Importer dans un cluster (ouvre le
formulaire d'import de cette archive) et Magasin d'exports. Sur le site qui
reçoit le fichier, le bouton **Déposer une archive** du magasin envoie un
`.hvx` du poste de l'exploitant dans le magasin de cette console :

- le fichier voyage dans le corps de la requête et s'écrit sur le disque
  au fur et à mesure, jamais en mémoire : un disque de plusieurs dizaines
  de Gio passe ;
- avant d'être acceptée, l'archive est vérifiée : complète, lisible, et
  chaque membre conforme à sa somme SHA-256. Une copie abîmée en route,
  même à la bonne taille, est refusée avec le membre en cause, et rien
  n'est gardé ;
- le magasin refuse un nom déjà présent, et dit qu'il manque de place avant
  de recevoir quoi que ce soit ;
- le dépôt est une action comme les autres : débit et temps restant dans la
  fenêtre et le dock, puis la vérification des sommes ; Annuler dans le
  dock l'arrête et supprime le fichier partiel.

Une fois déposée, l'archive s'importe comme toute autre (bouton d'import de
sa ligne). En ligne de commande, une archive est simplement un fichier :
`harvester-vm-transfer import --in <fichier>.hvx`.

**Suivre un transfert, et sa vitesse (1.46.0).** Le contrôle préalable
annonce ce qu'il y a à transférer : disques, taille, place réellement
occupée.

Une fois le transfert lancé, la fenêtre « Migrer » et le dock montrent :

- la phase en cours : gel des disques, téléchargement, import, sauvegarde,
  images ou restauration ;
- la quantité faite sur le total, et les octets réellement transmis ;
- le débit, le temps restant et le temps écoulé.

Chaque phase finie garde son bilan dans l'Activité. Quand tout est parti
mais que la cible écrit encore, l'affichage le dit. Pour une sauvegarde, la
quantité est la taille des volumes : une sauvegarde incrémentale en
transmet moins.

Un choix **Vitesse** dit ce qu'il coûte :

| Profil | Ce qu'il fait | Coût |
|---|---|---|
| Économe | un disque à la fois | le plus lent, ménage un cluster chargé |
| Normale (défaut) | tous les disques ensemble (Longhorn compresse chaque téléchargement sur un cœur) | CPU sur la source, réseau |
| Maximale | relève en plus de 2 à 8 les fils de sauvegarde et de restauration de Longhorn le temps du transfert, rétablis à la fin même en cas d'échec | CPU et réseau sur tous les nœuds des deux clusters, partagés avec toute autre sauvegarde en cours |

Un **plafond de débit** (Mio/s) limite ce que l'hôte de la console envoie,
tous disques confondus, pour ménager un lien de production.

Un transfert encaisse une API de cluster qui décroche un moment :

- les attentes réessaient ;
- un téléchargement coupé est resservi, ou réécrit depuis son début ;
- un retour arrière réessaie ses suppressions et nomme ce qu'il n'a pas
  pu retirer.

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

### Les sections de Harvester, un cluster à la fois (1.57.0)

Le menu latéral regroupe, sous Cluster, les menus que les opérateurs
connaissent de Harvester, pour le cluster choisi :

- **Stockage** : Volumes (la vue du stockage : répliques, santé,
  corrections), Images (source, taille, état, classe de stockage, VMs qui
  s'en servent) et Classes de stockage (répliques, récupération, liaison,
  extension, l'image pour laquelle une classe a été faite, combien de
  volumes l'utilisent).
- **Réseau** : Réseaux des VMs (un bloc par réseau), Réseaux de cluster,
  Équilibreurs de charge, Pools d'adresses et Réseaux d'hôte (1.65.0, voir
  plus bas), Réseaux overlay (les VPC et subnets kube-ovn, avec leurs
  formulaires) et Réseaux underlay (la fabrique : cartes physiques, switchs
  virtuels, LLDP). Ces vues ont quitté
  l'Aperçu, qui garde Métriques et la vue Cluster.
- **Add-ons** : chaque add-on de Harvester, son chart et sa version, son
  état, et Activer / Désactiver pour les administrateurs, suivi dans le dock
  (`harvester-resources addon` en ligne de commande). Un refus de Harvester
  se dit simplement : sur un cluster d'un seul nœud, le descheduler « cannot
  be enabled as not enough nodes exist in the cluster ». Vérifié sur harv1 :
  harvester-seeder activé (déployé en 21 s), puis désactivé.
- **Sécurité** : Secrets (type, nom des clés, VMs qui s'en servent ; les
  valeurs ne sont jamais lues dans la page ; les centaines de secrets que le
  cluster utilise pour lui-même restent cachés tant qu'on ne les demande
  pas) et Clés SSH (empreinte, validation, VMs qui les ont reçues).
- **Avancé** (1.67.0, voir plus bas) : Réglages (tous les réglages de
  Harvester, cible de sauvegarde comprise), Périphériques PCI, Périphériques
  USB et Réseaux SR-IOV (1.68.0) et Support (paquets de support de
  Harvester et kubeconfigs limités à un rôle).

Chaque liste se filtre par mots, se trie par n'importe quelle colonne (clic
sur l'en-tête, à nouveau pour inverser ; l'ordre est retenu), ouvre le
détail d'une ligne et ouvre une VM depuis son nom. Les listes se relisent
toutes les 10 s tant qu'elles sont à l'écran.

**Créer, modifier, supprimer (1.59.0)**, comme le permettent les menus de
Harvester, chacun dans une fenêtre qu'on garde ouverte ou qu'on réduit,
chaque geste étant une action suivie dans le dock (`harvester-resources
create|delete|sc-default|volume-expand|addon-values` en ligne de commande) :

- **Images** : une nouvelle image depuis une adresse http(s), avec sa classe
  de stockage (celle par défaut proposée), suivie jusqu'à son import ;
  supprimer une image qu'aucun volume n'utilise.
- **Classes de stockage** (administrateurs) : une nouvelle classe (répliques,
  délai d'une réplique périmée, localité des données, étiquettes de disque
  et de nœud, récupération, liaison, migration, extension) ; en faire la
  classe par défaut (Harvester refuse une seconde classe par défaut :
  l'ancienne est retirée d'abord, et remise si la nouvelle est refusée) ;
  supprimer une classe qu'aucun volume ni aucune image n'utilise.
- **Volumes** : un nouveau volume, vide ou depuis une image (sa classe `lh-`
  lue sur l'image, la taille au moins la taille virtuelle de l'image) ;
  agrandir un volume depuis son détail ; la suppression des volumes
  orphelins reste celle d'avant.
- **Réseaux des VMs** (administrateurs) : un nouveau réseau VLAN ou sans
  étiquette sur un réseau de cluster, route automatique (DHCP) ou manuelle ;
  supprimer un réseau qu'aucune VM n'utilise, depuis la liste des réseaux
  sans VM.
- **Clés SSH** : une nouvelle clé, collée ou lue dans un fichier .pub,
  suivie jusqu'à sa validation par Harvester ; supprimer.
- **Secrets** : un nouveau secret à plusieurs clés ; supprimer un secret
  qu'aucune VM n'utilise (les secrets du cluster lui-même ne sont jamais
  proposés).
- **Add-ons** (administrateurs) : la configuration (valeurs Helm, YAML) dans
  une fenêtre ; l'enregistrer redéploie un add-on activé. La lire est aussi
  réservé aux administrateurs, car elle peut porter des mots de passe.

Vérifié sur harv1, chaque geste contrôlé par kubectl puis défait : une image
importée par URL (28 s), un volume fait depuis elle et agrandi de 1 à 2 Gio,
la suppression de l'image refusée tant qu'elle servait, puis les deux
supprimés ; une classe de stockage créée, rendue par défaut puis rendue,
supprimée ; une clé SSH validée puis supprimée ; un secret à deux clés
(leurs noms listés, jamais leurs valeurs), supprimé ; un réseau VLAN 3999
créé et supprimé ; la configuration d'un add-on modifiée puis remise.

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
- **Réseaux kube-ovn : VPC, subnets et réseaux overlay, avec leurs
  formulaires (onglet VPC, 1.49.0).** Un bloc par VPC, dans la même
  disposition : à gauche ses subnets (plage, passerelle, réseau overlay,
  NAT, DHCP, occupation des adresses) et les VMs qui y ont une adresse ; à
  droite par où le VPC sort (NAT sortant par les nœuds dans le VPC par
  défaut, ses routes statiques et appairages, ou « isolé »). Ce qui cloche
  est dit au-dessus des blocs : un réseau overlay qu'aucun subnet ne sert
  (une VM qui s'y branche ne reçoit aucune adresse), un subnet dont le
  réseau a disparu, des plages qui se chevauchent, un subnet presque plein.
  - **Formulaires** (administrateurs) : un VPC (espaces de noms ; repliés,
    routes statiques et appairages) et un subnet (VPC, un /24 libre proposé
    loin des nœuds, des pods et des autres subnets, passerelle déduite,
    réseau overlay créé avec le subnet ou un existant sans subnet, NAT
    sortant proposé dans le VPC par défaut seulement, DHCP pour les VMs ;
    repliés, adresses exclues, subnet privé et réseaux autorisés, espaces
    de noms). Un contrôle préalable tourne pendant la saisie ; Enregistrer
    reste grisé tant que quelque chose bloque. Modifier un subnet garde sa
    plage, son VPC et son réseau, que kube-ovn ne sait pas changer.
  - **La suppression** est refusée tant qu'une VM ou un pod utilise le
    subnet (la vue dit lesquels, avant d'envoyer quoi que ce soit) ou
    qu'un VPC a encore des subnets ; le réseau overlay ne part avec son
    subnet que si la console l'a créé. Le VPC par défaut, ses subnets
    `ovn-default` et `join` et les subnets d'underlay restent en lecture
    seule.
  - Chaque changement est une action suivie dans le dock ; la ligne de
    commande fait de même : `harvester-network inventory | check | apply |
    delete`.
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

### Les hôtes, comme dans Harvester (1.62.0)

Dans la vue Cluster, le panneau d'un hôte a **Configurer...**, qui ouvre la
configuration d'hôte de Harvester dans une fenêtre, un onglet par sujet.
Chaque changement est une action suivie, par `harvester-resources host
<action>` (administrateurs).

Depuis la 1.68.1, la fenêtre a aussi les onglets de la page d'un hôte dans
Harvester, en lecture : **Essentiel** (IP, rôle, état, système, noyau,
moteur de conteneurs, kubelet, synchronisation de l'heure avec un
avertissement quand l'hôte n'est pas à l'heure, UUID, fabricant, modèle et
numéro de série quand ils sont connus, et trois jauges : CPU et mémoire
utilisés face à ce que VMs et pods peuvent obtenir, place Longhorn promise
sur l'hôte face à ses disques), **Instances** (les VMs qui y tournent,
ouvertes depuis leur nom), **Réseau** (les configurations de réseau de
cluster appliquées à l'hôte avec leurs VLAN et leur état, et ses cartes
réseau avec le bond ou le pont dont elles font partie) et **Événements** (ce
que Kubernetes a signalé sur l'hôte).

- **Général** : le nom affiché de l'hôte, l'adresse de sa console (un lien
  l'ouvre), ses labels (ceux du système, dont `cpumanager`, ceux de Rancher
  et de Longhorn, sont cachés et jamais touchés) et ses **tags d'hôte**, qui
  servent aux classes de stockage à placer les répliques.
- **Disques** : les disques que le gestionnaire de disques de Harvester a
  trouvés. Un disque entier, actif et non monté peut être **ajouté** au
  stockage (formaté, sauf s'il porte déjà de l'ext4 ou du XFS ; Longhorn V1,
  V2 ou un groupe de volumes LVM) ; un disque du stockage montre sa place
  libre, maximale et promise, prend des **tags de disque** et peut cesser
  d'accepter des répliques ; le **retirer** laisse Longhorn déplacer d'abord
  ses répliques (Harvester refuse s'il porte la seule copie saine d'un volume).
- **Huge pages** : les huge pages transparentes du noyau de l'hôte (activées,
  mémoire partagée, défragmentation).
- **KSM** : la fusion des pages mémoire identiques entre VMs (stop, run,
  prune ; paramètres standard, high ou personnalisés ; seuil de mémoire libre ;
  fusion entre nœuds NUMA). ksmtuned ne fusionne que lorsque la mémoire libre
  passe sous le seuil.
- **Hors bande** : le BMC de l'hôte par l'add-on harvester-seeder (adresse,
  port, utilisateur et mot de passe gardés dans un Secret, vérification du
  certificat, événements matériels). Comme dans Harvester, **éteindre,
  allumer et redémarrer** par le BMC ne sont offerts qu'à un hôte en
  maintenance.
- **Gestes** : **activer ou désactiver le CPU manager** (la politique
  statique, nécessaire aux CPU dédiés ; Harvester redémarre l'agent
  Kubernetes du nœud, les VMs continuent ; refusé tant qu'une VM à CPU dédiés
  y tourne), et **supprimer l'hôte** après avoir tapé son nom (jamais le
  dernier nœud).

Le seeder joint le **Redfish** d'un BMC **sur le port 443** quel que soit le
port donné (ce port est celui d'IPMI, 623), et **IPMI n'accepte que des mots
de passe de 20 octets au plus** ; la fenêtre dit les deux, et montre l'erreur
de connexion du seeder quand il ne joint pas le BMC.

Vérifié sur le cluster de test à trois nœuds : noms, labels et tags ; un
disque virtuel ajouté, étiqueté, sorti de la planification puis retiré ; huge
pages ; KSM en marche ; CPU manager activé puis désactivé ; l'accès hors bande
par un émulateur IPMI (virtualbmc), l'hôte éteint puis rallumé par lui en
maintenance ; un hôte supprimé, puis réinstallé dans le cluster. Un vrai BMC
(iLO, iDRAC) n'a pas encore été essayé par le seeder.

### Les namespaces (1.62.0)

**Namespaces**, à côté du sélecteur de namespace de l'onglet Machines
virtuelles, ouvre une fenêtre avec les namespaces du cluster : description,
VMs, volumes, quota d'instantanés, âge. Les namespaces du système sont
cachés par défaut et ne se suppriment pas. **Nouveau namespace** (nom,
description, labels) ; **modifier** (description, labels, annotations, et le
**quota d'instantanés** du namespace entier, gardé dans la
`default-resource-quota` de Harvester) ; **YAML** ; **supprimer** après avoir
tapé le nom, la fenêtre disant ce qui part avec.

### Projets Rancher et quotas (1.72.0)

La fenêtre **Namespaces** a une colonne **Projet** et un onglet **Projets**,
comme la page Projects/Namespaces de Harvester sous Rancher.

- Le projet d'un namespace est lu dans son annotation
  `field.cattle.io/projectId`, et comparé au cluster : un namespace dont
  l'annotation désigne un projet d'un autre cluster (un import précédent du
  cluster dans Rancher : 17 sur harv1 au moment d'écrire) est signalé, car
  Rancher le traite comme hors projet et ne lui applique aucun quota.
- Connectée par Rancher, la console lit les projets avec votre jeton : leurs
  noms, leurs quotas et leur usage, le défaut des namespaces, la limite par
  défaut des VMs. **Nouveau projet** et **modifier** : nom, description,
  quotas de ressources (chaque ligne une limite de projet et la part de
  chaque namespace ; CPU en cœurs ou millicœurs, mémoire et stockage en Gi
  ou Mi), et la limite donnée par défaut aux VMs qui n'en fixent pas. Les
  règles de Rancher sont vérifiées avant d'écrire (limite du projet et
  défaut des namespaces ensemble, sur les mêmes ressources, le défaut sous
  la limite ; requests sous les limits). Les projets Default et System de
  Rancher, et un projet qui contient encore des namespaces, ne se
  suppriment pas.
- **Déplacer** met un namespace dans un projet, ou le sort de tout projet
  (Rancher retire alors son quota). Un nouveau namespace peut être créé
  directement dans un projet. Dans un projet à quotas, **modifier** un
  namespace montre son propre quota, seulement pour les ressources que le
  projet limite et sous sa limite : au-delà, Rancher mettrait le quota à
  zéro et aucune VM ne pourrait démarrer. La console attend que Rancher
  l'applique.
- Tout passe par Rancher avec votre jeton, qui y applique donc vos droits.
  Connectée avec un compte de la console, la fenêtre montre le rangement
  tiré des annotations sans pouvoir le changer.

En ligne de commande, avec le kubeconfig d'une session Rancher :
`harvester-resources project create|update|delete|move|ns-quota`.

### Membres Rancher du cluster et de ses projets (1.73.0)

Sous les comptes du cluster (Réglages, **Comptes du cluster**), **Membres
dans Rancher** liste à qui Rancher donne des droits sur le cluster, avec le
rôle et le fournisseur (local, Keycloak...) ; dans la fenêtre Namespaces,
**Membres** sur un projet fait de même pour le projet. Un membre s'ajoute en
cherchant parmi les utilisateurs et groupes que Rancher connaît, puis en
choisissant un rôle du contexte (propriétaire, membre, lecture seule ou un
rôle plus fin). Le retrait est refusé pour le dernier propriétaire. L'API
de Rancher ne liste pas les liaisons de ses comptes système : elles ne sont
jamais proposées. Tout passe par Rancher avec votre jeton : Rancher vérifie
que vous pouvez donner ce rôle. Connectée avec un compte de la console, la
section invite à se connecter par Rancher.

En ligne de commande, avec le kubeconfig d'une session Rancher :
`harvester-resources member add|remove [--scope project --project p-xxxxx]`.

### Événements et utilisation du tableau de bord (1.62.0)

L'aperçu a un onglet **Événements** : les événements du cluster, rangés comme
sur le tableau de bord de Harvester (hôtes, VMs, volumes, images), avec leurs
nombres, les avertissements marqués, un filtre « avertissements seulement »
et une recherche ; relus toutes les 20 secondes. L'onglet Métriques montre
des jauges d'**utilisation** : CPU et mémoire mesurés maintenant
(metrics.k8s.io) face à la capacité des hôtes, avec ce que pods et VMs ont
réservé ; le stockage Longhorn écrit, et ce qui est promis aux volumes face à
ce qui peut l'être (sur-provisionnement compris).

### Les gestes sur les volumes et les images (1.63.0)

Dans la section Stockage, le panneau d'un volume a **Actions**, et chaque
ligne d'image a son bouton **Actions**, avec les menus de Harvester. Un geste
qui ne s'applique pas est grisé et dit pourquoi (volume utilisé par une VM,
cluster sans CDI, image pas prête ou hors Longhorn v1). Chacun est une action
suivie, par `harvester-resources volume|image <geste>`.

- **Volume** : **cloner** (avec ses données, Longhorn les copie ; ou un
  volume vide de même taille et même classe) ; **exporter en image** (le
  volume d'une VM en marche sur Longhorn v1, sinon arrêter la VM
  d'abord) ; **prendre un instantané** (avec la classe que nomme le réglage
  `csi-driver-config` de Harvester, possédé par le volume) ; **copier vers
  une autre classe** (la migration de données de Harvester : un DataVolume
  CDI, l'original reste) ; **annuler un agrandissement** qui ne peut pas
  aboutir (la demande est recréée à sa taille réelle sur le même volume,
  données gardées) ; **description** ; **supprimer** si aucune VM ne s'en
  sert.
- **Image** : **modifier** sa description et ses labels (Harvester fige le
  reste) ; **cloner** une image téléchargée depuis une adresse ;
  **chiffrer** vers une classe de stockage chiffrée, ou **déchiffrer** ;
  **télécharger** le fichier (gzip, comme le sert Harvester ; images
  Longhorn v1) ; **créer une VM** depuis elle (la fenêtre de création
  s'ouvre avec un disque fait de l'image, à sa classe et à sa taille).
- **Envoyer** un fichier d'image depuis le navigateur : la console le
  garde, puis l'offre une fois au cluster en HTTP (port 8092 par défaut,
  voir le guide d'installation), dont les nœuds le téléchargent ; le
  fichier est effacé ensuite. Une **somme SHA512** peut être donnée, ici et
  à la création d'une image depuis une adresse : Longhorn vérifie le
  fichier avec elle.

Vérifié sur harv1 : chaque geste de volume, une image modifiée,
téléchargée, clonée, chiffrée puis déchiffrée avec une classe chiffrée
jetable, une image CirrOS envoyée depuis le navigateur (22 s), une création
de VM ouverte depuis une image ; l'annulation d'un agrandissement figé sur le
banc à trois nœuds.

### Modèles, configurations cloud, classes de stockage, secrets, clés SSH (1.64.0)

- **Modèles** (à côté du sélecteur de namespace de l'onglet Machines
  virtuelles) : chaque modèle de VM et ses versions, la plus récente
  d'abord, marquées prêtes ou non (leurs images importées) et par défaut.
  **Lancer** ouvre la création de VM depuis une version (son cloud-init est
  recopié dans un Secret de la nouvelle VM, jamais partagé avec le modèle,
  comme le fait Harvester) ; **par défaut** ; **supprimer une version** (pas
  celle par défaut) ou le modèle entier ; YAML. Une version ne change
  jamais : on en fait une nouvelle depuis le menu d'une VM (Générer un
  modèle).
- **Configurations cloud** : les modèles cloud-init de Harvester (user-data
  et network-data, gardés en ConfigMaps étiquetées) : créer, modifier le
  texte et la description, supprimer, YAML.
- **Classes de stockage** : le formulaire de création propose le moteur
  (Longhorn v1 ; Longhorn v2 si son moteur de données est activé ; LVM si
  son add-on est installé, avec le nœud, le groupe de volumes et le type),
  le **chiffrement** avec le secret de la phrase secrète (et
  l'agrandissement à chaud des volumes chiffrés), une topologie permise, le
  mode de liaison, la politique de récupération et une description. Les
  classes Longhorn v1 chiffrées ont été essayées pour de vrai (un volume
  écrit et relu par un pod) ; les choix LVM et Longhorn v2 non, aucun des
  deux n'étant activé sur les clusters d'essai.
- **Secrets** : créés par type (Opaque, authentification basique, clé SSH,
  certificat TLS, registre, et le secret de **chiffrement** des classes de
  stockage) ; **nouvelles valeurs** pour un secret existant (les valeurs ne
  sont jamais montrées ; un champ vide garde l'actuelle ; un secret Opaque
  peut perdre une clé). Les valeurs passent par un fichier privé, jamais
  par une ligne de commande.
- **Clés SSH** : **modifier** la clé publique et la description ; Harvester
  calcule la nouvelle empreinte.

### Réseaux, comme dans Harvester (1.65.0)

La section Network suit le menu Networks de Harvester, en onglets : VM
Networks, Cluster Networks, Load Balancers, IP Pools, Host Networks, puis
les onglets Overlay et Underlay de kube-ovn. Tout changement est réservé
aux administrateurs, contrôlé avant de partir et suivi dans le dock.

- **Réseaux de cluster** : un bloc par réseau de cluster (son pont, son MTU,
  s'il est prêt) avec ses **configurations** : quelles cartes de quels hôtes
  forment son bond (tous les hôtes, un hôte, ou des hôtes par étiquettes),
  le mode du bond, miimon et le MTU, et l'état sur chaque hôte tel que
  l'agent de Harvester le donne. Seules les cartes présentes et libres sur
  tous les hôtes choisis sont proposées ; jamais celle de la gestion. Une
  configuration se modifie, se **déplace vers un autre réseau de cluster**
  (Migrate de Harvester) ou se supprime ; un réseau de cluster se supprime
  quand il n'a plus ni configuration ni réseau de VM. Une VM en marche sur
  le réseau bloque ce que Harvester refuserait (un nouveau lien, un hôte qui
  sort), pas un changement de description. L'agent d'un hôte peut signaler
  une première erreur puis réussir : la console attend une minute avant d'y
  voir un échec.
- **Réseaux de stockage, de migration des VMs et RWX** : trois tuiles en
  tête de l'onglet, chacune sur le réseau de gestion ou sur un VLAN dédié
  d'un réseau de cluster (plage, adresses exclues ; pour le stockage, un
  VLAN qui lui est réservé ; pour RWX, partagé avec le stockage). Le réseau
  de stockage est refusé tant qu'une VM tourne, comme Harvester l'exige ; la
  console attend ensuite que Harvester le dise appliqué (il arrête la
  supervision, attend que chaque volume soit détaché, puis donne le nouveau
  réseau à Longhorn).
- **Réseaux de VM** : VLAN, sans étiquette, et **trunk** (plages de VLAN
  comme `100-199, 300`) ; une route (DHCP, ou un réseau et une passerelle
  manuels, avec un serveur DHCP facultatif) et une description. **Modifier**
  sur le bloc d'un réseau change sa description et sa route, et son VLAN ou
  ses plages de trunk tant qu'aucune de ses VMs ne tourne. Harvester sonde
  la passerelle d'une route connue ; le résultat est montré.
- **Équilibreurs de charge** : devant les VMs en marche d'un namespace
  choisies par étiquettes (`clé=valeur[,valeur]` par ligne), avec une
  adresse du **DHCP** ou d'un **pool** (fixé à la création), des écouteurs
  (port, et port sur les VMs, TCP ou UDP) et une sonde TCP facultative. La
  liste montre l'adresse, les VMs derrière et l'état.
- **Pools d'adresses** : des plages (sous-réseau, première et dernière
  adresse, passerelle), un réseau de VM facultatif, une priorité et un
  namespace (`*` rend le pool global). Une adresse encore tenue par un
  équilibreur disparu se **libère** depuis la fenêtre du pool.
- **Réseaux d'hôte** : une interface `<réseau>-br.<VLAN>` sur chaque hôte
  choisi, en DHCP ou avec une adresse fixe par hôte (même sous-réseau),
  éventuellement underlay des réseaux overlay. Harvester ne pose aucune
  route.

En ligne de commande : `harvester-resources clusternetwork`, `netconfig`,
`vmnet`, `lb`, `ippool`, `hostnet` et `netsetting`.

Essayé pour de vrai sur le banc à trois nœuds : réseau de cluster et
configuration créés, MTU changé puis rétabli, configuration déplacée vers un
autre réseau de cluster et revenue, réseaux VLAN, trunk et sans étiquette
créés et modifiés (sonde de passerelle répondue), un réseau d'hôte fixe sur
les trois hôtes, le réseau de migration prouvé par une migration à chaud,
les réseaux de stockage et RWX appliqués VMs arrêtées puis rétablis, des
équilibreurs par DHCP et par pool joints en SSH depuis l'extérieur, une
adresse orpheline libérée.

### Réseaux overlay et underlay : NAT, réseaux fournisseurs, politiques (1.66.0)

Trois fenêtres complètent la partie kube-ovn du menu Networks de Harvester.
Chacune commence par la **santé de kube-ovn** : la base OVN, le contrôleur, et
les hôtes où l'agent réseau de kube-ovn n'est pas prêt. (Un hôte supprimé puis
revenu a laissé un jour la base OVN sans quorum pendant des heures, sans rien
dire ; les fenêtres le disent désormais d'abord.)

- **Réseaux fournisseurs** (onglet Underlay, bouton du même nom) : les réseaux
  fournisseurs (kube-ovn reprend une carte de chaque hôte dans un pont
  `br-<nom>`, avec ses adresses et ses routes ; une carte déjà prise dans un
  bond de Harvester, celle de la gestion comprise, est refusée), leurs VLANs
  (0 = sans étiquette), et les **réseaux externes** : le côté LAN des
  passerelles NAT, un subnet underlay au vrai préfixe du LAN et à sa
  passerelle, avec une plage d'adresses que personne d'autre n'utilise.
  Seule la première de la plage peut être tirée par kube-ovn (l'adresse
  propre de la passerelle) ; les IP externes prennent les suivantes.
- **NAT et Internet** (onglet Overlay) : les **passerelles NAT** d'une VPC (un
  pod dans un subnet de la VPC à une IP LAN, qui sort par un réseau externe ;
  la console ajoute la route par défaut de la VPC vers elle, ce que kube-ovn
  ne fait jamais, et la retire avec la passerelle), les **IP externes** (la
  prochaine adresse réservée est proposée), les règles **SNAT** et **DNAT**.
  kube-ovn fige ces objets une fois prêts : on les supprime et on les recrée,
  règles d'abord, puis IP externe, puis passerelle, un ordre que la console
  fait respecter.
  Avec kube-ovn avant 1.16.1 (Harvester 1.8), le pod d'une passerelle perd
  son propre réseau au profit de celui du locataire et plus rien ne revient
  par elle (issue kube-ovn 6632) : la console le répare à la création, signale
  une passerelle cassée de nouveau (kube-ovn la réécrit quand son conteneur
  redémarre) et propose **Réparer**. Harvester 1.9 embarque un kube-ovn sans
  ce défaut.
- **Politiques** (onglet Overlay) : des politiques réseau qui visent des VMs
  par leur nom, avec des règles d'entrée et de sortie (tout le monde, un
  réseau, un namespace ou des VMs, et des ports), et un mode souple, coché par
  défaut, qui garde le DHCP de kube-ovn. kube-ovn applique une politique à
  chaque interface kube-ovn des VMs visées, jamais à une carte sur un pont VLAN
  de Harvester. Une politique écrite avec des sélecteurs que le formulaire ne
  sait pas dire se modifie en YAML.

En ligne de commande : `harvester-network apply|delete --kind
provider|vlan|external|gateway|eip|snat|dnat|policy` et `harvester-network
state`.

Essayé pour de vrai sur le banc à trois nœuds : un réseau fournisseur sur la
seconde carte de chaque hôte, un VLAN sans étiquette, un réseau externe sur le
LAN qui garde six adresses ; une VM d'un subnet de VPC jointe en SSH depuis
l'extérieur par une IP externe et une règle DNAT ; une politique qui l'a
coupée puis, modifiée, l'a laissée passer ; tout retiré dans l'ordre de
kube-ovn.

### Réglages de Harvester, paquets de support et kubeconfigs (1.67.0)

La section **Avancé**, sous Cluster, porte le reste des menus Advanced et
Support de Harvester.

- **Réglages** : la quarantaine de réglages que montre l'interface de
  Harvester, rangés par groupe (général, réseau, sécurité, performances,
  sauvegarde, mise à jour, support, interface), filtrables par mots ou aux
  seuls modifiés. Chaque ligne dit si la valeur diffère du défaut et si
  Harvester l'a **appliquée** (pour un réglage doté d'un contrôleur : son
  annotation de hash est à jour et sa condition `configured` ne porte pas
  d'erreur, qui est affichée). Modifier ouvre une fenêtre typée (liste,
  nombre avec ses bornes, oui/non, texte, JSON, PEM) ; la valeur est
  contrôlée d'avance comme le ferait le webhook de Harvester (niveau de
  journalisation, rapport de hotplug de 1 à 20, surengagement d'au moins
  100 %, serveurs NTP sans `http://` ni doublon, délai d'arrêt, réserve
  mémoire, mémoire Longhorn v2, formes JSON et PEM), puis écrite et suivie
  dans le dock jusqu'à ce que Harvester l'applique. **Remettre au défaut**
  retire la valeur. Les réglages qui peuvent couper un accès (provision
  automatique des disques, qui les formate ; proxy, registre et rotation des
  certificats RKE2, qui réappliquent le plan RKE2 sur chaque nœud ; URL
  d'enregistrement Rancher, dont le retrait supprime l'agent Rancher ;
  certificat et options TLS ; interface externe) demandent de cocher « Je
  comprends le risque ». Les réglages de réseau de stockage, de migration et
  RWX ouvrent Réseau > Cluster Networks, où sont leurs contrôles ;
  `server-version` est en lecture seule ; `ssl-parameters` est signalé sans
  effet en Harvester 1.9 (remplacé par `traefik-default-tls-options`, qui est
  montré).
- **Les secrets n'arrivent jamais dans la page** : clé privée TLS, clés S3,
  mots de passe de registre, identifiants de proxy et jeton de l'URL d'import
  Rancher (il donne les identifiants de l'agent du cluster) s'affichent en `•••`.
  Laissés tels quels, le serveur remet les vrais depuis le cluster à
  l'enregistrement ; les clés S3 et les mots de passe de registre, que
  Harvester retire lui-même du réglage une fois appliqué, sont à ressaisir.
- **Cible de sauvegarde** : un formulaire NFS ou S3 (point d'accès,
  compartiment, région, clés, style virtual-hosted, intervalle de relecture)
  et **Tester**, qui demande à Harvester s'il joint la cible enregistrée (son
  propre contrôle de santé). Harvester se connecte à une nouvelle cible avant
  de l'accepter et refuse un changement pendant une sauvegarde ou une
  restauration.
- **Paquets de support** (onglet Support) : l'archive de diagnostic de
  Harvester. Nouveau demande une description (exigée par Harvester), un lien
  de ticket, des namespaces supplémentaires et les délais ; la collecte est
  suivie jusqu'au bout, puis l'archive se télécharge par l'API server et
  reste sur le cluster jusqu'à son expiration ou sa suppression. Le paquet
  anonymisé de la console reste dans la barre du haut.
- **Kubeconfigs** (onglet Support) : plus sûrs que le téléchargement de
  Harvester, qui remet les droits d'un administrateur. Chaque kubeconfig est
  un compte de service lié à un seul rôle choisi (view, edit, admin,
  cluster-admin ou les rôles de Harvester), sur tout le cluster ou un
  namespace, avec un jeton qui expire (de 1 heure à 90 jours). Le formulaire
  prévient quand le rôle lit les Secrets : sur Harvester, `view` les lit, car
  Harvester y ajoute ses propres règles de lecture, données cloud-init
  comprises. Le fichier se télécharge une seule fois ; la console n'en garde
  aucune copie et ne journalise jamais le jeton. **Révoquer** supprime le
  compte : le jeton cesse aussitôt de fonctionner.

Réservé aux administrateurs, pour tout changement et pour tout l'onglet
Support (un paquet porte les journaux du cluster, un kubeconfig un jeton). En
ligne de commande : `harvester-resources setting set|reset|test-backup-target`,
`harvester-resources supportbundle create|delete` et
`harvester-resources kubeconfig create|revoke --out <fichier>`.

Essayé pour de vrai sur harv1 : niveau de journalisation, rapport de hotplug
(recopié dans KubeVirt par Harvester) et délai d'arrêt changés puis remis au
défaut ; cible de sauvegarde testée ; un paquet de support collecté en moins
de six minutes, téléchargé et supprimé ; un kubeconfig `view` sur `default`
qui a listé les VMs, a été refusé ailleurs et a cessé de fonctionner dès sa
révocation. Sur le banc à trois nœuds : une cible de sauvegarde NFS posée,
jointe par Longhorn, testée et retirée.

### Périphériques PCI, USB et SR-IOV (1.68.0)

Trois onglets de plus dans **Avancé**, comme dans Harvester (ils demandent
l'add-on `pcidevices-controller` ; sans lui les onglets le disent et ouvrent
les Add-ons) :

- **Périphériques PCI** : chaque périphérique PCI de chaque hôte, filtrable
  par mots, par hôte ou aux seuls passés : adresse, description,
  identifiants fabricant et produit, pilote en service (et celui qu'avait
  l'hôte), **groupe IOMMU** (tout le groupe part avec lui, ce qui est dit
  avant d'activer), VMs qui s'en servent. **Activer le passthrough** le
  détache de l'hôte pour les VMs, **Désactiver** le rend ; à l'unité ou par
  sélection. Un périphérique sans groupe IOMMU ne peut pas être passé ; un
  périphérique dont une VM se sert ne peut pas être rendu.
- **Périphériques USB** : de même pour l'USB (fabricant, produit, chemin).
- **Réseaux SR-IOV** : les cartes réseau SR-IOV non prises par un réseau de
  cluster, avec le nombre de **fonctions virtuelles** qu'elles exposent :
  activer avec N, désactiver (refusé tant qu'une fonction virtuelle est en
  passthrough) ; pour changer N, désactiver d'abord. Chaque fonction
  virtuelle devient un périphérique PCI, signalé comme tel, à passer comme
  les autres.

L'éditeur de VM propose les périphériques PCI et USB dans la même liste,
avec leur état de passthrough. Un périphérique USB prend le nom de son
USBDevice, c'est par là que Harvester sait qu'une VM s'en sert.

Un piège de Harvester 1.8, rencontré sur le banc : un périphérique retiré
d'une VM arrêtée reste inscrit dans l'annotation d'allocation de la VM, et
Harvester refuse alors de le rendre (« already in use with vm »). Harvester
1.9 refait cette annotation depuis la VM ; la console fait de même avant de
désactiver, et le dit dans le dock. L'allocation d'une VM en marche compte
toujours.

Les pages GPU (vGPU, SR-IOV GPU, MIG) ne sont pas livrées : aucun GPU que
Harvester sait gérer n'est sur les bancs, rien n'a donc pu être essayé en
réel.

En ligne de commande : `harvester-resources device pci-enable|pci-disable|
usb-enable|usb-disable --name <périphérique> [--name ...]` et
`harvester-resources device sriov --name <carte> --vfs N`.

Essayé pour de vrai sur le banc à trois nœuds (périphériques émulés : une
carte e1000e, une carte igb SR-IOV, une tablette QEMU) : l'e1000e passée à
une VM sur son hôte (le périphérique est dans le domaine libvirt de la VM),
refusée au retour tant qu'elle servait, rendue à son pilote ensuite ; deux
fonctions virtuelles créées sur l'igb, l'une passée puis rendue, le SR-IOV
refusé à l'arrêt tant qu'elle était prise, puis désactivé ; la tablette
passée à une VM d'un autre hôte puis rendue.

### Mettre à jour Harvester (1.69.0)

**Mettre à jour**, dans l'en-tête de l'Aperçu (et sur le réglage
`server-version`, comme dans Harvester), ouvre une fenêtre par cluster.

- **Avant** : la version actuelle, et deux chemins :
  - **une version** : un objet `Version` de Harvester. La liste dit celles
    que ce cluster peut atteindre, avec la raison que donnerait Harvester ;
    Harvester lui-même ne le vérifie qu'après avoir téléchargé l'ISO de
    8 Go. Une version s'ajoute depuis son `version.yaml` publié (la console
    le lit) ou se supprime. Quand le vérificateur de versions de Harvester
    est actif, il supprime à son prochain passage (toutes les heures) les
    versions qu'il ne propose pas lui-même : lancer peu après en avoir
    ajouté une ;
  - **un ISO de la console** (le magasin de Bare-metal) : le chemin airgap.
    La console lit la version de Harvester dans l'ISO, vérifie son SHA-512
    (Harvester ne le fait pas ; le fichier `.sha512` publié est utilisé
    quand il est posé à côté de l'ISO), le sert au cluster par son guichet à
    jeton, attend que Harvester l'ait importé, puis lance la mise à jour.

  Options : collecter les journaux de la mise à jour (Harvester les garde
  dans un volume de 1 Gio jusqu'au classement ; sans rancher-logging il
  déploie son propre collecteur), sauter le contrôle des volumes détachés à
  une réplique. Les refus que le webhook de Harvester opposerait sont dits
  d'avance (autre mise à jour en cours ou en nettoyage, hôtes pas prêts ou
  isolés, volumes dégradés à partir de trois hôtes, sauvegardes en cours,
  planifications non suspendues, add-ons en transition, charts de Harvester
  pas prêts). Les notes de version de la cible sont liées, et « J'ai lu et
  compris les instructions de mise à jour » est exigé, comme dans Harvester.
- **Pendant** : chaque étape avec son heure (journaux, ISO téléchargé avec
  son pourcentage, dépôt, images préchargées, services système, hôtes,
  terminé), l'état de chaque hôte (un hôte mis en pause par le mode manuel
  d'`upgrade-config` peut être repris), ce qu'apporte la nouvelle version.
  Le lancement est une action suivie jusqu'au bout dans le dock ; l'API du
  cluster disparaît pendant le redémarrage de Kubernetes et des hôtes, et la
  console continue de suivre (jusqu'à 45 minutes sans réponse). **Suivre**
  rattache une nouvelle action à une mise à jour en cours, après un
  redémarrage de la console par exemple.
- **Après** : la réussite, ou la cause de l'échec (Harvester la met dans la
  raison de la condition Completed, ou dans la première étape en échec, ou
  dans un hôte) ; **Journaux** empaquette et télécharge les journaux de la
  mise à jour ; **Classer** laisse Harvester retirer le collecteur de
  journaux et son volume. **Abandonner** supprime la mise à jour tant que
  Harvester l'accepte, avant qu'elle ne touche aux hôtes (Harvester nettoie
  alors, journaux et image ISO compris).

Réservé aux administrateurs. En ligne de commande : `harvester-resources
upgrade version-add|version-delete|start|follow|logs|dismiss|abort|resume-node`.

### Surveillance et journaux (1.70.0)

**Monitoring & Logging**, dans le menu Cluster, réunit ce que Harvester
répartit entre ses pages Monitoring et Logging, en quatre onglets.

- **Métriques** : sans rancher-monitoring, ce que le cluster sait à l'instant
  (metrics-server) : le CPU et la mémoire de chaque hôte, et pour chaque VM en
  marche son CPU en cœurs et en part de ses vCPU, et sa mémoire. Avec
  rancher-monitoring, Prometheus ajoute le CPU, la mémoire, le disque et le
  réseau du cluster, et par VM la part de CPU, la mémoire utilisée, le trafic
  réseau et disque. Le CPU d'une VM selon Prometheus est le temps que passe
  l'invité sur ses vCPU (les tableaux de bord de VM de Harvester le divisent
  par 1000 et affichent presque zéro) ; la vue de l'instant compte le
  conteneur de la VM, QEMU compris, et affiche plus (sur harvlab, 0,2 % contre
  3,8 % pour des VMs au repos). Les add-ons sont à un clic quand la
  surveillance est éteinte.
- **Alertes** : les objets AlertmanagerConfig des namespaces, avec leurs
  receivers (webhook, Slack, e-mail, PagerDuty, Opsgenie, Microsoft Teams) et
  leur route (regroupement, délais, filtres). Les valeurs secrètes d'un
  receiver se saisissent dans le formulaire et deviennent un Secret du
  namespace ; seule la référence est gardée. Les événements que Kubernetes
  enregistre sur une configuration (Alertmanager qui la refuse, par exemple)
  sont montrés avec elle.
- **Flux** : les objets Flow (un namespace) et ClusterFlow (tout le cluster)
  de l'opérateur de journaux, de trois natures comme dans Harvester :
  journaux, audit (le journal d'audit du serveur d'API, par le
  `harvester-kube-audit-log-ref` de Harvester) et événements (les événements
  Kubernetes que recueille le collecteur d'événements de Harvester). Le
  formulaire ne propose que les sorties qu'un flux peut utiliser (celles de
  son namespace, celles du cluster, les sorties d'audit pour un flux d'audit),
  avec des règles de sélection (labels, hôtes, namespaces, inclure ou
  exclure) et des filtres en YAML.
- **Sorties** : les objets Output et ClusterOutput, avec un formulaire par
  cible (Elasticsearch, OpenSearch, Loki, Splunk HEC, syslog, Kafka, forward,
  S3, HTTP, fichier, null). Les champs secrets fonctionnent comme pour les
  receivers. Une sortie utilisée par un flux ne peut pas être supprimée tant
  que le flux ne change pas. Le chemin d'une sortie fichier doit contenir
  `${tag}` (fluentd découpe son tampon par tag) ; le formulaire en propose un.

Chaque objet montre l'état que lui donne l'opérateur : appliqué, inactif
(aucun flux ne s'en sert encore), problèmes (avec les mots de l'opérateur),
en attente. L'enregistrement attend l'opérateur, puis le contrôle de
configuration de fluentd : quand fluentd refuse la nouvelle configuration, il
garde l'ancienne et rien ne change dans l'acheminement des journaux, alors la
console le dit avec l'erreur de fluentd au lieu de « enregistré ». Un
contrôle en échec d'un Logging s'affiche aussi au-dessus des listes.
Enregistrer un objet inchangé n'écrit rien.

La lecture est ouverte à tous les rôles ; la modification est réservée aux
administrateurs. En ligne de commande : `harvester-resources monlog
output-apply|output-delete|flow-apply|flow-delete|amc-apply|amc-delete`.

### Importer des VMs depuis VMware, OpenStack ou une archive OVA (1.71.0)

**VM Import**, dans la section VM Import / Export du menu (1.77.0 ; sous
Cluster avant), pilote l'add-on vm-import-controller
de Harvester (à activer d'abord dans Add-ons ; la section dit quand il est
éteint).

- **Sources**, un onglet par fournisseur :
  - **VMware** : l'adresse SDK du vCenter (`https://vcenter/sdk`), le nom
    exact du datacenter et un compte autorisé à exporter des VMs ;
  - **OpenStack** : l'adresse Keystone, la région, l'utilisateur, le mot de
    passe, le projet et le domaine, et les nouveaux essais d'envoi ;
  - **OVA** : l'adresse d'un fichier `.ova` en HTTP ou HTTPS, avec un
    utilisateur, un mot de passe ou un certificat d'autorité facultatifs, et
    le délai de téléchargement (600 s par défaut, il couvre tout le
    téléchargement : à augmenter pour une grosse archive, 0 pour aucune
    limite).

  Les identifiants saisis deviennent un Secret du namespace de la source
  (nommé `<source>-creds` sauf autre nom), avec la clé que le contrôleur
  attend pour chaque type (la clé du certificat d'autorité diffère entre
  les trois) ; un Secret existant peut servir à la place. Chaque source
  montre si le contrôleur l'a jointe (prête), n'a pas pu (pas prête), ou
  ne l'a jamais vérifiée (Secret absent, identifiants refusés). Harvester ne
  garde la raison que dans le journal du contrôleur : **Pourquoi** en
  montre les lignes d'erreur pour cette source. Harvester ne revérifie
  jamais une source prête : **Revérifier** la recrée. Une source utilisée
  par un import en cours ne peut être ni modifiée ni supprimée ; supprimer
  une source supprime aussi le Secret que la console a créé pour elle.
- **Imports** : la VM à importer (son nom dans vCenter, le nom ou l'ID du
  serveur dans OpenStack, le nom à lui donner pour une OVA), le namespace
  cible, la classe de stockage de ses disques, et la correspondance des
  cartes réseau de la source avec des réseaux de VM. Une carte sans ligne
  est supprimée ; sans aucune ligne, la VM reçoit une seule carte sur le
  réseau des pods. Avancé : modèle de carte et bus de disque par défaut,
  contrôles de la source à passer, et pour VMware le dossier, le délai
  d'arrêt de l'invité et une extinction forcée (nécessaire sans les VMware
  Tools). Le formulaire montre le nom que la VM prendra dans Harvester.

  Ce sur quoi le contrôleur bouclerait sans rien dire est refusé avant
  d'écrire : un nom de VM invalide dans Harvester, des noms d'image de plus
  de 63 caractères (`vm-import-<import>-<disque>`), un réseau associé deux
  fois ou vers un réseau qui n'existe pas, une VM de même nom qui ne vient
  pas d'un import. Chaque import montre son étape (contrôles, export depuis
  la source, images, VM créée, VM en marche), la progression de l'image de
  chaque disque et un lien vers la VM une fois créée. Un import est suivi
  jusqu'à la VM en marche dans le dock ; un import bloqué sans changer
  d'état pendant cinq minutes est arrêté avec la raison du contrôleur.
  Supprimer un import terminé laisse la VM et ses images ; avant, ses images
  partent avec lui et une VM déjà créée reste.

Vérifié en réel : une OVA importée de bout en bout sur harv1, et les sources
VMware contre vcsim (le simulateur de vCenter des tests du contrôleur).
L'export d'une VM depuis un vrai vCenter ou OpenStack n'est pas vérifié :
aucun n'est disponible sur les bancs d'essai.

La lecture est ouverte à tous les rôles ; la modification, et le journal du
contrôleur, sont réservés aux administrateurs. En ligne de commande :
`harvester-resources vmimport source-apply|source-recheck|source-delete|
import-create|import-follow|import-delete`.

### Migrations VMware par Forklift : installation, image VDDK, fournisseur vCenter (1.75.0)

Harvester 1.9 ne livre pas Forklift. `harvester-forklift install` pose
cert-manager (depuis le paquet Cluster API de la console), l'add-on
expérimental forklift-operator (chart 1.9.0, images `v1.8.2` par défaut,
`--image-tag` pour une autre) et le ForkliftController sur le cluster,
dans cet ordre ; les images viennent de `registry.rancher.com/harvester`
(prévoir un miroir en airgap). Si Harvester porte déjà son propre add-on
forklift-operator (attendu depuis la 1.9.1), la console l'active tel quel
et ne réécrit jamais son chart ni ses valeurs. Ce chemin n'est pas encore
vérifié en réel : aucun banc ne fait tourner un Harvester qui livre cet
add-on.

**Migrations VMware**, dans la section VM Import / Export du menu, juste
après VM Import, ouvre
trois onglets :

- **Préparation**, trois étapes dans l'ordre où il faut les faire, chacune
  avec son état et son bouton : Forklift (installer ou reprendre, avec le
  détail relu : cert-manager, add-on, opérateur, contrôleur, composants,
  et l'accès à l'inventaire, le compte de service `harvester-ops-inventory`
  par lequel la console lit l'inventaire de Forklift ; il est posé en fin
  d'installation, si bien qu'une installation arrêtée plus tôt, ou un
  Forklift installé autrement, le montre manquant et propose Reprendre) ;
  l'image VDDK (l'archive de VMware est déposée une fois dans le magasin
  de la console et sert à tous les clusters ; l'image cible est proposée
  d'après le réglage `containerd-registry` de Harvester, dont les
  identifiants pour ce registre sont réutilisables sans être montrés,
  Harvester 1.9 les gardant dans un secret de fleet-local, ou peuvent être
  saisis à la place ; le cluster retient la dernière image poussée et le
  formulaire la reproposera) ; et les sources vCenter, avec le nombre de
  celles qui sont prêtes et un lien vers l'onglet suivant. Ce qui est en
  cours de saisie dans l'étape VDDK (image, compte du registre, choix de
  l'archive) et un dépôt en cours sont gardés pendant que l'onglet se
  relit de lui-même ; Actualiser relit tout. Un lien depuis VM Import >
  VMware ouvre cet onglet.
- **Sources vCenter** : un bloc par fournisseur vSphere, son état (en
  vérification, prêt, ou refusé avec le message de Forklift), son image
  VDDK s'il en a une, et le nombre de plans de migration qui l'utilisent.
  Ajouter une source peut reprendre un vCenter déjà déclaré dans VM
  Import sans ressaisir son mot de passe, lu par le serveur ; en saisir
  une demande l'adresse, un utilisateur, un mot de passe et un certificat
  (certificat d'autorité, ou non vérifié). Modifier une source ne
  redemande jamais le mot de passe sauf s'il est saisi, et garde le
  réglage TLS sauf s'il est changé. La suppression est refusée tant qu'un
  plan de migration utilise la source. Une nouvelle source ne remplace
  jamais un fournisseur existant du même nom : la fenêtre dit que le nom
  est pris. Tout fournisseur fait par `harvester-forklift`, depuis l'onglet
  ou en ligne de commande, porte l'étiquette de la console
  `harvester-ops.io/managed` et peut être modifié ; un fournisseur vSphere
  fait par un autre outil n'est jamais modifié (son bouton Modifier est
  désactivé et `provider-apply` refuse son nom), il peut seulement être lu
  et supprimé. Une fenêtre reste liée au cluster pour lequel elle a été
  ouverte, même si l'onglet passe à un autre cluster avant l'enregistrement.
- **Inventaire**, en lecture seule : choisir une source, puis les VMs, les
  réseaux ou les datastores, chercher par nom, et pour les VMs un filtre
  sur celles qui peuvent migrer à chaud (Changed Block Tracking actif).
  Chaque VM montre ses CPU, sa mémoire, ses disques, si le CBT est actif,
  et les points d'attention de Forklift traduits en texte clair
  (critique, avertissement, information).

En ligne de commande : `harvester-forklift status|install|vddk-image|
provider-apply|provider-delete|inventory`. `install` prend
`--chart-version` et `--image-tag`, et soit `--cert-manager-manifest`
soit `--cert-manager-from-bundle` (le paquet Cluster API de la console).
`vddk-image` construit l'image d'amorçage depuis l'archive de VMware et
la pousse, identifiants de registre sur l'entrée standard ou dans un
fichier privé (`--spec`) ; avec `--cluster` ou `--kubeconfig`, elle note
aussi l'image poussée sur le cluster (ConfigMap
`forklift/harvester-ops-vddk`), pour qu'un formulaire ultérieur la
repropose. `provider-apply` lit sa demande en JSON (`url`, `user`,
`password`, `insecure` ou `cacert`, `vddk_image`) sur l'entrée standard ou
`--spec`, pose l'accès à l'inventaire s'il manque, et refuse un nom déjà
pris par un fournisseur qu'il n'a pas fait (le fournisseur vSphere d'un
autre outil, ou le fournisseur `host` de Forklift lui-même) ;
`provider-delete` refuse un fournisseur qui n'est pas un vCenter, et celui
qu'un plan utilise ; `inventory` lit `vms`, `networks` ou `datastores` tels
que Forklift les voit.

**Airgap.** La console tire elle-même l'image de base VDDK
(`registry.suse.com/bci/bci-busybox:16.0`), depuis son propre hôte : en
airgap, `HARVESTER_OPS_VDDK_BASE`, posée dans `/etc/harvester-ops/env`,
désigne un miroir de cette image (`--base` en ligne de commande). Cette
image de base, ou son miroir, est toujours tirée de façon anonyme en
HTTPS, reconnue par les autorités de certification système de l'image de
la console : un miroir servi en HTTP simple, ou qui exigerait des
identifiants, n'est pas encore pris en charge. Le registre cible (où
l'image VDDK construite est poussée) est reconnu de la même façon par ces
autorités : un registre signé par une autorité interne n'est pas encore
pris en charge, mais le HTTP simple et des identifiants y fonctionnent
tous les deux. Seul le manifeste de cert-manager vient du paquet Cluster
API de la console ; ses images, comme celles de Forklift, sont tirées par
le cluster : les mettre en miroir. Le cluster doit aussi pouvoir tirer
depuis le registre où l'image VDDK est poussée (Advanced >
containerd-registry).

Vérifié en réel sur le banc harvlab2, contre le vCenter imbriqué de
vmwlab, depuis l'onglet : c'était aussi la première vérification réelle
d'une source VMware de VM Import contre un vrai vCenter, pas seulement
vcsim. Les vagues de migration à chaud, la bascule, le retour arrière et
la vue globale sont arrivés en 1.76.0 (section suivante).

La lecture est ouverte à tous les rôles ; la modification est réservée
aux administrateurs, et chaque écriture (Forklift installé, image VDDK
poussée, source ajoutée, modifiée ou supprimée) est une action suivie,
dans le dock et dans Activity.

### Migrations VMware à chaud : vagues, bascule, retour arrière, vue globale (1.76.0)

Une **vague** est un lot de VMs d'une même source vCenter migrées à chaud
ensemble : Forklift copie leurs disques pendant qu'elles tournent, puis
prend des copies incrémentales (Changed Block Tracking) jusqu'à la
**bascule**, où chaque source est arrêtée, la dernière copie faite et la
VM démarrée sur Harvester. La coupure se limite à cette dernière étape.

**Préparation**, deux étapes de plus :

- **Importeur de disques (CDI)**, en étape 2. L'importeur CDI livré avec
  Harvester (`registry.suse.com/suse/sles/16.0/cdi-importer:1.65.0`) n'a
  pas le greffon VDDK de nbdkit : toute copie de disque VMware échoue
  (harvester/harvester#11773, toujours là dans les 1.9.1-rc1 et rc2).
  L'étape dit quelle image le cluster utilise (SUSE sans VDDK, amont, ou
  autre) et propose de passer à l'importeur amont de la même version
  (`quay.io/kubevirt/cdi-importer:v1.65.0`, ou son miroir en airgap). Le
  réglage se fait sur le déploiement `harvester-system/cdi-operator`
  (`IMPORTER_IMAGE`, `OVIRT_POPULATOR_IMAGE`) ; l'image d'origine est notée
  une fois, en annotation, et « Revenir à l'image d'origine » la remet.
  Une mise à jour de Harvester peut remettre son propre importeur :
  revérifier cette étape après chaque mise à jour.
- **Intervalle entre les copies incrémentales**, sous les étapes : en
  minutes, de 5 à 1440 (60 par défaut dans Forklift). Il vaut pour tout le
  cluster et toutes les vagues ouvertes (`controller_precopy_interval` du
  ForkliftController) ; l'enregistrer redémarre le contrôleur Forklift, et
  une copie déjà prévue n'est pas replanifiée.

**Inventaire** : une colonne VMware Tools (en marche ou arrêtés) et une
colonne « Migration à chaud » qui dit si la VM est éligible et, sinon,
pourquoi (CBT inactif, VMware Tools arrêtés : sans eux la bascule ne peut
pas arrêter la source, VM déjà prise par une autre vague ouverte). Les VMs
éligibles se cochent, puis **Composer une vague** ouvre la fenêtre de
composition.

**Composer une vague** : un nom (minuscules, chiffres et `-`, 40
caractères au plus), le namespace cible, pour chaque réseau vCenter
utilisé par ces VMs un réseau de VM de Harvester ou le réseau des pods,
pour chaque datastore une classe de stockage, et deux options :
conserver les IP statiques, et la **copie brute** (sans conversion de
l'invité). La conversion est faite par défaut ; la copie brute raccourcit
la coupure mais l'invité doit déjà avoir les pilotes virtio (la plupart
des Linux, pas Windows) : elle est cochée d'office quand toutes les VMs
sont sous Linux, et un invité Windows coché le signale. Une VM déjà prise
par une vague ouverte, sur ce cluster ou sur un autre cluster déclaré
dans la console, est refusée (une même VM est reconnue par son vCenter et
son identifiant `vm-NN`) ; un cluster injoignable est nommé dans la
réponse, sans bloquer la composition.

**Vagues**, nouvel onglet : un bloc par vague, avec son état (prête à
lancer, en validation, refusée par Forklift, copie en cours, bascule
prévue, bascule en cours, migrée, en échec, revenue à la source, close),
son namespace cible, ses VMs, la prochaine copie et la bascule prévue.
Les gestes :

- **Lancer** : une première copie complète des disques, puis des copies
  incrémentales à l'intervalle réglé.
- **Basculer maintenant** ou **Planifier la bascule** (heure du
  navigateur) ; les copies continuent jusque-là, une bascule planifiée
  peut être avancée.
- **Revenir à la source**, pour toute la vague ou une VM, seulement une
  fois la bascule de cette VM amorcée : la console vérifie d'abord qu'elle
  joint le vCenter, arrête la VM Harvester (retrouvée par son nom, ou par
  les étiquettes `vmID` et `plan` que pose Forklift), puis rallume la
  source par le vCenter. Relancé, il ne refait rien de ce qui est déjà
  fait. Si VMware arrête la source sur une question à la mise sous tension
  (par exemple un fichier de port série déjà présent), la console le dit,
  avec la question et ses choix, au lieu de la déclarer rallumée, et laisse
  cette VM non revenue : y répondre dans le vCenter, puis relancer le retour
  arrière (1.84.0).
- **Clore** : termine la vague, pour que ses VMs puissent entrer dans une
  autre ; ni les VMs Harvester ni les sources ne sont touchées. Coché, il
  retire aussi les instantanés `forklift-migration-precopy` que Forklift
  laisse sur les sources après une tentative en échec (possible aussi plus
  tard sur une vague close).
- **Supprimer** : retire le plan, ses migrations et ses correspondances ;
  les VMs migrées restent.
- **Suivre** ouvre une fenêtre par vague : pour chaque VM, l'étape
  (traduite), la progression du disque, le nombre de copies, la durée de
  la dernière et le temps jusqu'à la prochaine, et l'erreur avec une
  piste quand elle est connue (importeur sans VDDK, image VDDK erronée,
  VMware Tools absents).

Les objets d'une vague (NetworkMap, StorageMap, Plan avec `warm: true`,
Migration) vivent dans le namespace `forklift`, étiquetés
`harvester-ops.io/managed` et `harvester-ops.io/wave`.

**Migrations (tous clusters)**, troisième entrée de la section VM Import /
Export (1.77.0 ; à côté d'Activity avant) : toutes
les VMs de toutes les vagues de tous les clusters déclarés, dans un seul
tableau (VM VMware, vCenter, cluster cible, vague, étape, dernière copie,
bascule), filtrable par état et par vCenter, avec un bloc par cluster
(Forklift installé ou non, image d'importeur, sources, vagues). Un
cluster injoignable est montré tel quel, sans bloquer les autres ; la vue
est relue au plus toutes les 15 secondes par personne.

**Couloirs** (1.80.0) : l'onglet Vagues passe de **Blocs** à **Couloirs**
(le choix est retenu par le navigateur, Blocs par défaut). Les couloirs
placent toutes les vagues du cluster sur un même axe du temps, un
couloir par vague : une ligne pour maintenant, une marque par copie déjà
faite (la première copie complète, puis les incrémentales ; le survol
d'une marque donne son début, sa fin et sa durée), la prochaine copie,
la bascule prévue avec son compte à rebours, la fenêtre de bascule une
fois passée, et l'état de la vague. L'axe va du début de la plus
ancienne vague à deux heures après maintenant (ou 30 minutes après la
dernière bascule prévue) ; les boutons 6 h, 24 h, 7 jours et Ajuster
changent l'étendue. Une fenêtre de maintenance peut être tracée sur
l'axe (début et fin, gardés dans ce navigateur seulement, par cluster) :
une aide visuelle, rien n'est envoyé au cluster. Un clic sur un couloir
ouvre la fenêtre de suivi de la vague. Migrations (tous clusters) offre
les mêmes couloirs, un par cluster et par vague.

Avant de composer, lancer ou basculer une vague, la console vérifie qu'aucune
adresse MAC de ses VMs n'est déjà portée par une VM du cluster de destination
(1.83.2). Harvester refuse une MAC en double même sur une VM arrêtée, et
Forklift ne le découvre qu'en créant la VM, après que la bascule a arrêté la
source : le geste est refusé, avec la VM qui porte l'adresse. Si l'inventaire
ne répond pas, le contrôle est sauté et c'est dit.

En ligne de commande : `harvester-forklift wave-apply` (la vague en JSON
sur l'entrée standard ou `--spec` : `name`, `target_namespace`,
`provider`, `vms`, `networks`, `storages`, `skip_conversion`,
`preserve_static_ips`), `wave-start`, `wave-cutover [--at <RFC 3339>]`,
`wave-status`, `waves`, `wave-rollback [--vm vm-NN]`,
`wave-close [--clean-snapshots]`, `wave-delete`,
`cdi-importer [--show|--upstream [--image <miroir>]|--original]` et
`precopy-interval <minutes>`. Le retour arrière et le retrait des
instantanés parlent au vCenter avec les identifiants de la source
(REST pour l'alimentation, SOAP pour les instantanés, que l'API REST de
vCenter 8.0 n'expose pas).

**Mesuré en réel** sur harvlab2, contre le vCenter 8.0.1 imbriqué de
vmwlab, Debian 11 avec un disque de 10 Gio : coupure de **6 min 24 s**
avec conversion de l'invité (dont environ 4 min 30 de conversion),
**1 min 44 s** en copie brute ; retour arrière : 17 s pour arrêter la VM
Harvester, puis le démarrage de la source (environ 5 min sur ce banc
imbriqué). Deux vagues menées depuis la console : bascule immédiate et
planifiée, copie brute et conversion, refus d'une VM sans VMware Tools et
d'une VM déjà prise, retour arrière relancé sans effet, clôture qui a
retiré trois instantanés Forklift du vrai vCenter, importeur changé puis
noté, intervalle modifié.

**Limites.**

- Sans les VMware Tools en marche dans l'invité, la bascule ne peut pas
  arrêter la source et échoue ; l'inventaire le dit avant.
- L'importeur CDI amont remplace une image SUSE : en airgap, le mettre en
  miroir, et revérifier après chaque mise à jour de Harvester.
- L'inventaire lit Forklift au niveau de détail 4 (outils, instantanés,
  UUID) : plus lourd qu'avant sur un gros vCenter.
- Le refus d'une VM déjà prise sur un **autre** cluster est testé, mais
  pas vérifié en réel : un seul banc fait tourner Forklift.
- La bascule dépend du vCenter : sur le banc imbriqué, un démarrage de VM
  a mis plus d'une minute à répondre, d'où une attente de 5 minutes avant
  de conclure à un échec.

### Stockage LVM et téléchargement des images CDI (1.74.0)

- **Les images sur une classe hors Longhorn v1** (LVM, Longhorn v2, stockage
  tiers) sont désormais créées comme Harvester les crée : avec le backend
  `cdi`. Jusque-là la console demandait une image de fond Longhorn quelle
  que soit la classe, et Harvester rangeait l'image en silence dans une
  classe Longhorn au lieu de celle choisie (vu en réel sur harvlab2 avec une
  classe LVM). Les envois depuis le navigateur suivent la même règle.
- **Télécharger une image CDI** : Harvester copie d'abord le volume dans un
  fichier qcow2 compressé, par un downloader temporaire. **Télécharger** le
  dit, suit cette préparation comme une action, puis récupère le fichier
  qcow2 ; Harvester retire ensuite le downloader. Une image Longhorn v1 se
  télécharge toujours directement, compressée en gzip.
- **Vérifié en réel** sur harvlab2 (Harvester 1.9.0, add-on LVM
  expérimental de Harvester, disque virtuel de réserve) : un disque confié à
  LVM depuis la fenêtre de l'hôte (groupe de volumes actif), une classe LVM
  créée par le formulaire, une image importée dessus en image CDI avec son
  volume LVM, et téléchargée en qcow2.

En ligne de commande : `harvester-resources image prepare-download|download`.

## 4. Cluster API : clusters RKE2 en aval (console + CLI)

Créer et exploiter des clusters Kubernetes dont les nœuds sont des VMs
Harvester, par Cluster API et le Cluster API Provider Harvester (CAPHV). La
console lance `harvester-capi` à chaque étape, que la CLI peut lancer
aussi.

### Installer la pile (1.48.0)

- **Harvester v1.9 et suivants** embarquent Rancher Turtles, qui fait déjà
  tourner le cœur de Cluster API. La console déclare à Turtles les
  fournisseurs RKE2 (amorçage et plan de contrôle) et Harvester (objets
  `CAPIProvider`) depuis le paquet airgap, sans accès à Internet, et charge
  leurs images sur les nœuds par SSH. L'onglet Installation donne chaque
  fournisseur avec sa version et son état.
- **Des correctifs de compatibilité pour CAPHV v0.10.1 sur Harvester v1.9**
  sont posés par l'installation et affichés sur le même onglet : un Service
  `kube-system/ingress-expose` qui porte la VIP (Harvester v1.9 l'a retiré
  et CAPHV le lit encore), un correctif pour que le cœur de Cluster API lise
  l'état des HarvesterMachine tel que CAPHV l'écrit, et des gabarits
  générés passés en `v1beta1`. Ils disparaîtront quand CAPHV livrera les
  corrections.
- **Les restes d'une installation antérieure** à côté de Turtles (un second
  cœur Cluster API, des webhooks aux certificats expirés) sont repérés ; un
  administrateur peut les retirer depuis l'onglet Installation. Rien de ce
  qui appartient à Turtles n'est touché, et le retrait refuse tant qu'il
  existe d'autres clusters que le cluster local.
- **Les versions antérieures de Harvester** gardent l'installation
  d'avant : cert-manager, cœur Cluster API et fournisseurs, tout depuis le
  paquet.

### Créer un cluster (1.48.0, fenêtre à menus en 1.53.0)

Le bouton **Créer un cluster** de l'onglet Clusters K8S ouvre une fenêtre
qu'on peut replier dans la barre des fenêtres, le temps de vérifier un
réglage ailleurs, et rouvrir telle quelle. Elle est organisée en menus,
comme la création d'une VM, et remplie à partir du cluster lui-même :

- **Essentiel**, le menu d'ouverture, qui suffit pour créer : le nom, la
  version de Kubernetes (les versions créées pour de vrai avec le paquet
  sont marquées « testée »), 1, 3 ou 5 nœuds de plan de contrôle, le nombre
  de workers, un gabarit petit / moyen / grand, l'image (sans les ISO ni
  les images en cours de téléchargement, les images SUSE d'abord, la
  dernière choisie retenue), la paire de clés, le réseau des VMs avec son
  VLAN et le pool d'adresses avec ses plages et ses adresses libres.
- **Nœuds** : CPU, mémoire et disque sur mesure, l'utilisateur SSH suggéré
  d'après le système de l'image.
- **Réseau** : la passerelle et le masque tirés du pool (et marqués comme
  tels tant qu'on ne les change pas), le serveur DNS (retenu), des pools et
  réseaux supplémentaires.
- **Stockage** : un disque de données et sa classe de stockage.
- **Kubernetes** : espaces de noms des objets du cluster et des VMs, le
  CNI, les CIDR des pods et des services.
- **Intégrations** : l'import dans Rancher, les compléments Fleet avec MTU,
  encapsulation et BGP.
- **Contrôle** : le détail du contrôle préalable ; chaque menu porte le
  nombre de constats qui le concernent, et la barre d'actions en donne le
  résumé quel que soit le menu ouvert.

Chaque contrôle s'explique au survol. Un **contrôle préalable** tourne
pendant la saisie et dit, dans la langue de l'interface, ce qui bloque (pas
assez d'adresses libres, CIDR qui se chevauchent, image absente ou pas
prête, espace de noms qui contient déjà un cluster, pile non
installée...) et ce qui mérite un coup d'œil (un seul nœud de plan de
contrôle ou un nombre pair, mémoire ou CPU justes, une version jamais créée
avec le paquet, une adresse d'API qui vient du DHCP). Le bouton Créer reste
grisé tant que quelque chose bloque. **Aperçu** montre les manifestes, le
secret d'identité masqué.

La création est une action : la page et le dock la suivent
(infrastructure, plan de contrôle a/b, workers c/d), et elle se termine sur
le téléchargement du kubeconfig. L'annuler retire ce qu'elle a créé.

### Exploiter les clusters

- Liste, détails (spec, conditions, machines), mise à l'échelle des
  workers, téléchargement du kubeconfig, suppression.
- **La suppression ne laisse rien derrière elle (1.48.0)** : les VMs
  d'abord, puis les objets que la console a générés à côté du cluster
  (ClusterClass, gabarits, compléments, et le secret d'identité, qui porte
  un kubeconfig du cluster Harvester) dès qu'aucun autre cluster de
  l'espace de noms ne s'en sert, puis l'espace de noms si la console l'a
  créé.
- Les montées de version de Kubernetes ne sont pas implémentées.
- Les volumes que réclament les applications du nouveau cluster sont des
  volumes Harvester (PVC nommées `pvc-<id>` dans l'espace de noms des VMs).
  Supprimer leurs demandes dans le cluster les libère ; un cluster supprimé
  avec des demandes encore liées les laisse derrière lui.
- **Les images SLES ont besoin d'un enregistrement ou d'un dépôt local** :
  cloud-init installe `iptables` et `qemu-guest-agent` sur chaque nœud, et
  sans eux les pods qui publient un port d'hôte (ingress-nginx) ne
  démarrent pas. Le contrôle préalable le signale. L'image cloud openSUSE
  Leap 15.6 fonctionne telle quelle.

### Services sur les clusters créés (1.52.0)

L'onglet Services déploie des charts Helm sur les clusters créés par
Cluster API, par le fournisseur d'add-ons Helm de Cluster API (CAAPH
v0.6.4) :

- **Le fournisseur** est dans le paquet airgap et s'installe depuis
  l'onglet Installation avec les autres. Il est facultatif : sans lui, on
  crée toujours des clusters, et l'onglet Services dit ce qui manque.
- **Un catalogue prêt à l'emploi** : un serveur DNS (CoreDNS avec
  résolveurs amont et enregistrements locaux, joignable sur une adresse de
  répartiteur de charge), une application témoin (podinfo, une page qui dit
  quel cluster la sert), et un chart Helm libre (dépôt, chart, version,
  valeurs).
- **Un formulaire** remplit les réglages, propose la version de chart
  essayée et montre les valeurs que recevra le chart. Le contrôle préalable
  tourne pendant la saisie : fournisseur absent, cluster inconnu, nom,
  espace de noms, dépôt ou YAML invalides, version laissée libre, service
  existant qui sera mis à jour.
- **Déployer** déclare un `HelmChartProxy` à côté du cluster et étiquette
  le `Cluster` pour que le service le retienne ; CAAPH installe le chart,
  et l'action suit la release (une `HelmReleaseProxy` par cluster) jusqu'à
  ce qu'elle soit prête. **Retirer** un service le supprime : CAAPH
  désinstalle la release de chaque cluster, et l'étiquette disparaît.
- **L'adresse du service** est donnée au répartiteur de charge du cluster
  créé par le DHCP du réseau des VMs, ou prise dans un pool d'IP Harvester.
  Elle est annoncée par **kube-vip**, que CAPHV n'installe pas : la console
  le pose dans chaque cluster qu'elle crée (repris du chart du fournisseur
  de cloud Harvester, sur les nœuds du plan de contrôle), et dans un
  cluster plus ancien avec son premier service ; le contrôle préalable le
  signale.
- **Pas encore** : les charts et leurs images viennent d'Internet (le
  cluster de gestion télécharge le chart, le cluster créé tire les images) ;
  les servir depuis le paquet viendra ensuite. Les serveurs DHCP et NTP ne
  sont pas au catalogue.

### Paquets et CLI

- Paquets airgap horodatés avec marqueur actif, inspection, dépôt,
  téléchargement, et contrôle de compatibilité avec la version de
  Harvester.
- `harvester-capi status | install | cleanup-legacy | inventory | check |
  render | create | delete | services | service-check | service-deploy |
  service-remove`, chacune avec `--cluster` ou `--kubeconfig` ; `create` et
  `service-deploy` sortent en 2 quand le contrôle préalable bloque. Les manifestes sont
  produits par `caphv-generate`, livré avec la console (repris de CAPHV à
  un commit fixé, voir `bin/caphv-generate.PROVENANCE`).

## 5. Terraform — infrastructure as code (console)

Piloter le provider Terraform pour Harvester depuis des déclarations
sauvegardées.

- **Déclarations** : des groupes nommés de ressources (VMs, images de VM,
  clés SSH, code Terraform écrit à la main) appliquées ensemble, chacune
  avec son propre état Terraform (1.54.0). Elles sont gardées par la
  console (partagées entre opérateurs, sauvegardées avec elle) ; celles
  qu'un navigateur gardait encore sont reprises à la première visite.
- **Une seule vue (1.55.0)** : la liste des déclarations à gauche, chacune
  avec son état (jamais appliquée, à jour, N à appliquer, modifiée,
  erreur) ; la déclaration choisie à droite, renommée sur place (nom
  unique dans le cluster, rien de déployé ne bouge) et décrite. Trois
  onglets :
  - **Ressources** : une carte par ressource avec son résumé, son adresse
    Terraform et son état (à créer, déployée, N réglages à changer, à
    remplacer, retirée et détruite au prochain apply, incomplète) ;
  - **Code** : le code Terraform que produit la déclaration, fichier par
    fichier, et un export en `.tf` ;
  - **Historique** : ses plans, applies et destructions, qui les a lancés
    et ce qui a changé.
- **Des formulaires dans la vue** : une ressource s'ouvre section par
  section (Général, Disques, Réseaux, Premier démarrage pour une VM), avec
  des libellés lisibles dans les cinq langues, le nom Terraform en petit
  et une bulle d'aide sur chaque champ, et un contrôle pendant la saisie.
  Enregistrer ne touche pas au cluster.
- **Un plan qui se lit avant d'appliquer** : « Prévisualiser le plan »
  calcule, sur cette seule déclaration, ce qui serait créé, modifié
  (réglage par réglage, avant et après), remplacé (et pourquoi) ou détruit,
  valeurs sensibles masquées. « Appliquer ce plan » applique exactement ce
  plan ; si la déclaration a changé depuis, la console refuse et demande un
  nouveau plan. Le dernier plan reste accessible par le bouton
  « Appliquer ».
- Une ressource retirée d'une déclaration est détruite par son prochain
  apply (le plan le dit) ; une ressource détruite à l'unité depuis
  l'onglet des ressources du cluster sort de sa déclaration. Détruire une
  déclaration demande de taper son nom ; une déclaration qui a des
  ressources déployées ne peut pas être supprimée. Deux opérateurs qui
  modifient la même déclaration ne s'écrasent pas : le second est prévenu
  et voit la version courante.
- Une VM détruite emporte ses disques, sauf si « supprimé avec la VM » est
  décoché sur le disque (1.52.1).
- **Ressources du cluster** : tout ce que Terraform gère sur le cluster,
  déclaration par déclaration ; une ressource de l'espace partagé
  (appliquée avant la 1.54) peut être adoptée par une déclaration, qui la
  reprend à son prochain plan sans la recréer.
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
  DNS, NTP, token, mot de passe OS ; le gestionnaire de mots de passe du
  navigateur ne remplit jamais ces champs), et la console remasterise l'ISO pour
  l'installation zéro-touch, le publie derrière un jeton à usage unique,
  le monte en média virtuel, programme une amorce unique sur `Cd` et
  allume la machine. Suivi étape par étape dans le dock, du préflight
  jusqu'à l'API Harvester qui répond sur la VIP.
- **La configuration complète de l'installeur (1.77.0).** Au-delà des
  champs de base, la fenêtre pose :
  - une interface de gestion en agrégat de plusieurs cartes (cartes
    découvertes cochées par MAC, ou noms), le mode d'agrégat, miimon, et en
    802.3ad le rythme LACP ; la politique de hachage en 802.3ad,
    balance-xor, balance-tlb et balance-alb ; un VLAN facultatif ;
  - un disque de données et « effacer tous les disques » ;
  - les libellés du nœud (un `clé=valeur` par ligne) et les modules noyau ;
  - une section **YAML avancé** pour tout le reste de ce que l'installeur
    accepte : `os.write_files` (connexions NetworkManager des autres
    réseaux, réglages systemd, sshd), `os.persistent_state_paths`,
    `os.sysctls`, `os.environment`, `system_settings`, etc.

  **Importer une configuration** lit un fichier d'installeur existant :
  les champs que le formulaire connaît le remplissent, le reste va dans le
  YAML avancé, le jeton et le mot de passe du fichier restent sur le
  serveur (les champs disent « repris du fichier »), et son `iso_url` est
  remplacée par l'image que la console sert au BMC. **Aperçu** montre le
  YAML exact que recevra l'installeur, secrets masqués. Chaque clé est
  vérifiée contre le schéma de l'installeur (Harvester v1.9) avant toute
  mise sous tension, et refusée avec son chemin quand elle est inconnue,
  mal typée, posée deux fois (par le formulaire et le YAML avancé) ou
  gardée par la console (`install.iso_url`, `install.automatic`,
  `install.mode`, `server_url`,
  `token`, `os.password`). Un `system_settings.ntp-servers` est refusé tant
  que le champ NTP est rempli : l'installeur le remplacerait par ce champ
  sans rien dire (vu sur une installation réelle).

  Vérifié en réel sur un nœud imbriqué de Harvester v1.9.0 installé avec
  une configuration de même forme que celle d'un exploitant : agrégat de
  gestion de deux cartes (active-backup), agrégat de stockage en MTU 9000
  avec un VLAN et une route statique écrits par `write_files`, disque de
  données pris comme disque par défaut de Longhorn, libellés du nœud,
  modules, un chemin persistant, un sysctl, tous encore en place après un
  redémarrage. Pas vérifié en réel : LACP (802.3ad, aucun commutateur du
  banc ne le négocie) et un VLAN de gestion étiqueté.
- **Disques choisis et vérifiés (1.78.0).** Un démarrage de découverte lit
  ce que Linux voit sur la machine (disques, liens stables, partitions,
  cartes réseau), même quand le BMC ne publie rien (iLO 4) ; la fenêtre
  montre alors un tableau des disques avec un rôle chacun (système,
  données, pool, effacer, ignorer), écrit des chemins stables, et le
  serveur refuse un disque trop petit, portant des données sans son
  effacement, ou utilisé deux fois. Plusieurs **pools de disques** (niveaux
  de stockage) sont créés juste après l'installation, chacun avec sa classe
  de stockage, sur tout Harvester. Le nouveau cluster est **déclaré tout
  seul dans la console**, avec sa propre clé SSH. L'installeur s'éteint à
  la fin et la console démarre elle-même le disque : un BMC qui ignore
  l'amorce unique ne boucle plus. Vérifié de bout en bout sur un nœud
  imbriqué piloté par un émulateur Redfish (cinq disques virtio, SATA,
  SCSI et NVMe) ; sur une lame physique, le chemin d'installation a été
  vérifié pour la dernière fois en 1.19 (iLO 4).
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

### Se connecter par Rancher (1.50.0)

Quand la console est déclarée dans Rancher Manager (2.12 et suivants, voir
le guide d'installation), sa page de connexion propose **« Se connecter
avec Rancher »** à côté des comptes locaux. Une personne déjà connectée à
Rancher entre sans rien saisir ; sinon Rancher affiche sa propre page de
connexion, compte local ou Keycloak.

- **Les droits viennent de Rancher, sans copie** : chaque geste sur un
  cluster passe par le mandataire de Rancher (`/k8s/clusters/<id>`) avec le
  jeton de la personne, et Rancher applique ses droits d'utilisateur, de
  groupe et de projet. Vérifié : un « membre du cluster » Rancher a été
  refusé par harv1 lui-même (`User "u-t286c" cannot get resource
  "virtualmachines"`), l'administrateur non.
- La console retrouve quel cluster Rancher est lequel en comparant l'UID de
  `kube-system` des deux côtés, ou les UID des nœuds pour qui ne lit pas
  `kube-system` (un « membre du cluster » Rancher, 1.56.0), ou
  `rancher_cluster` dans la configuration. Un cluster connu, Rancher est
  encore interrogé, compte par compte, pour savoir si la personne peut
  l'ouvrir. Les clusters que Rancher ne montre pas à la personne sont
  écartés, et toute requête qui en nomme un est refusée.
- **Rôle dans la console** : administrateur pour les administrateurs de
  Rancher et les groupes de `admin_groups`, `default_role` (opérateur) pour
  les autres.
- **Reste aux comptes locaux** : l'arrêt et le démarrage d'un cluster (un
  cluster éteint ne passe plus par Rancher, et Rancher peut tourner sur le
  cluster qu'on éteint), et le paquet de diagnostic pour les
  non-administrateurs (il lit tous les clusters avec le compte de la
  console).
- **Les jetons ne quittent jamais le serveur** : le navigateur n'a qu'un
  identifiant de session aléatoire (cookie HttpOnly) ; le jeton d'accès est
  renouvelé avant son expiration et écrit dans le fichier de jeton de la
  session, que ses kubeconfigs désignent (`tokenFile`). kubectl le lit à
  chaque appel et un client qui tourne (le provider Terraform) le relit
  chaque minute : un apply plus long qu'un jeton de dix minutes continue
  (1.56.0). Se déconnecter fait oublier la
  session ; Rancher ne laisse pas un jeton OIDC se révoquer lui-même, il
  expire à la durée de la session. Se déconnecter de Rancher ferme aussi la
  session de la console à son renouvellement suivant.

### Rancher réglés dans l'interface, connexion directe, chart Harvester RBAC (1.79.0)

Paramètres > Connexion par Rancher (administrateurs) règle un ou plusieurs
Rancher, tout de suite et sans redémarrage ; la section `rancher:` de
`config.yaml` fonctionne toujours et s'y montre en lecture seule.

- **Tester** lit la version de Rancher (`/rancherversion`) et ses
  fournisseurs d'authentification (`/v3-public/authProviders`), et dit
  lesquels prennent un mot de passe.
- **Connexion directe** : identifiant et mot de passe d'un fournisseur à
  mot de passe (local, LDAP, OpenLDAP, Active Directory, FreeIPA), envoyés à
  Rancher, jamais gardés. Le jeton Rancher obtenu sert comme celui de
  l'authentification unique (mandataire de Rancher, droits de Rancher,
  `default_role` dans la console, administrateurs de Rancher
  administrateurs de la console). Il dure la session, n'est pas renouvelé,
  et est supprimé dans Rancher à la déconnexion (`POST
  /v3/tokens?action=logout` : vérifié sur Rancher 2.14.1, un jeton ne peut
  pas se supprimer lui-même par `DELETE`). Un refus ne dit pas si le compte
  existe.
- **Enregistrement de l'authentification unique** : avec les identifiants
  d'un administrateur de Rancher, demandés une fois et non gardés, la
  console crée son `OIDCClient`, lit le secret généré dans
  `cattle-oidc-client-secrets` et le garde (0600), puis supprime le jeton de
  l'administrateur. Désenregistrer supprime le client dans Rancher.
- **Chart Harvester RBAC** : état (absent, installé, version), et
  installation dans le cluster `local` de Rancher en action suivie, refusée
  en disant pourquoi quand l'exigence du chart sur Rancher ou Kubernetes
  n'est pas remplie. Les nouveaux rôles sont listés à la fin.
- Rien de secret ne part vers le navigateur : ni secret de client, ni mot
  de passe, ni jeton, ni chemin de fichier.

### La connexion est obligatoire (1.57.0)

Il n'y a plus de console ouverte : sans session, toute page mène à la page
de connexion (jamais l'invite de mot de passe du navigateur) et tout appel
d'API est refusé. Les comptes locaux se connectent par un formulaire (cookie
de session HttpOnly, douze heures) ; une écriture portée par un cookie de
session doit venir de la console même. Sans aucun compte, le premier
démarrage crée le premier administrateur avec un jeton lu sur le disque du
serveur (voir le guide d'installation). Les administrateurs gèrent les
comptes dans Réglages > Comptes de la console (créer, rôle, réinitialiser un
mot de passe, supprimer ; le dernier administrateur ne peut pas être
retiré) ; chacun change son propre mot de passe depuis le menu du compte ;
réinitialiser un mot de passe ou supprimer un compte ferme ses sessions.

### Votre compte et la déconnexion (1.56.0)

Le bouton du compte, en haut à droite à côté des réglages, ouvre un menu
qui dit qui est connecté et comment (Rancher, compte local, ou console
ouverte sans connexion), le rôle dans la console et ce qu'il permet, ce que
voient les clusters (droits Rancher, identité de cluster déléguée, ou
kubeconfig partagé), la fin d'une session Rancher et ses groupes. Il mène
aux comptes du cluster, à la langue et à l'historique des versions, et
déconnecte :

- une session Rancher est oubliée par la console ;
- un compte local se déconnecte aussi, bien que l'authentification HTTP
  Basic n'ait pas de session : le navigateur garde le mot de passe et le
  renvoie ; la console lui fait donc retenir à la place un compte factice,
  accepté sur ce seul chemin (`/logout/local`). La page suivante redemande
  le mot de passe. Vérifié dans Chromium.
- une console ouverte n'a rien dont se déconnecter, et le menu le dit.

### Ce que le cluster a refusé (1.56.0)

Une vue que le cluster (ou Rancher) ne laisse lire qu'en partie n'affiche
plus une liste vide sans un mot. Les lectures refusées par la RBAC partent
avec la réponse (en-tête `X-Cluster-Denied` : verbe, ressource, groupe
d'API, espace de noms) et un bandeau au-dessus de la page les liste,
regroupées par ressource, avec ce qu'il faut demander : un rôle sur le
cluster ou sur le projet à un administrateur de Rancher, ou un droit pour
l'identité déléguée. Une session Rancher apprend aussi pourquoi un cluster
de la console manque : Rancher n'y donne pas accès à ce compte, ou aucun
cluster que Rancher lui montre n'est celui-là. Vu en réel avec un « membre
du cluster » Rancher sur harv1 : l'aperçu montrait 0 nœud et la liste des
clusters était vide ; il montre maintenant le nœud, et dit que les
machines virtuelles et les volumes Longhorn sont refusés. Un type refusé ne
cache plus non plus les types permis dans l'état d'un cluster (la lecture
groupée est reprise type par type).

## 9. Transversal

- **Historique des versions.** Un clic sur le numéro de version (menu
  latéral, menu du compte, Réglages > À propos) liste ce que chaque version
  a apporté, d'après les notes de version livrées avec la console : la plus
  récente d'abord, celle installée signalée, filtrées par mots, ou réduites
  aux ajouts ou aux corrections. Les notes sont en anglais.
- **Mise à jour depuis l'interface (1.82.0).** La même fenêtre a un onglet
  Mise à jour : vérifier la source en ligne (publications GitHub ou miroir
  interne) et télécharger, ou fournir une archive et sa signature pour un site
  isolé, puis installer. L'agent de mise à jour de l'hôte vérifie la
  signature, installe, redémarre la console et remet la version précédente
  tout seul si la nouvelle ne répond pas. Voir
  [installation.md](installation.md#mettre-harvops-à-jour-1820).

- **Un menu latéral qui rend l'écran.** Le menu de gauche est un rail
  d'icônes de 56 px qui se déplie par-dessus la page sous le pointeur et se
  replie quand il s'en va. C'est un calque, pas une colonne : l'ouvrir ne
  redimensionne jamais la zone de travail, si bien que le panneau de détail
  de la topologie, les canvas et les tableaux ne sautent pas sous les yeux.
  Il s'épingle depuis son pied de menu quand on veut les libellés en
  permanence : épinglé, il devient une colonne, et la page, le dock et la
  barre des fenêtres se décalent une fois pour lui faire place, si bien que
  rien ne reste caché dessous. L'épinglage est mémorisé.
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
- **Lectures partagées et dimensionnement (1.81.0)** : les écrans qui se
  rafraîchissent seuls sont lus une fois pour tous ceux qui ont la même
  identité et le même rôle, et oubliés à chaque écriture ; chiffres mesurés
  et ressources recommandées dans [dimensionnement.md](dimensionnement.md).
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
