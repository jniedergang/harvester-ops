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

### Toutes les VMs de tous les clusters en une liste (1.85.0)

**Virtual machines** est un groupe de deux entrées : **VMs du cluster
courant** (les VMs du cluster choisi en haut, par namespace, comme avant) et
**VMs de tous les clusters**, toutes les VMs de tous les clusters déclarés
dans un tableau avec une colonne **Cluster**.

- Chaque cluster est lu en parallèle, avec l'identité de la personne
  connectée. Un cluster éteint, injoignable, qui refuse la lecture ou sans
  accès dans cette session est dit tel quel sur sa puce, sans retenir les
  autres.
- Un cluster que Rancher cache à la personne n'a pas de puce et son nom
  n'apparaît jamais. La vue dit seulement combien de clusters ne sont pas
  affichés (« Clusters non affichés : 2 »), comme le menu des hôtes. La
  même règle vaut pour Conteneurs, GPU et Migrations de toute la flotte.
- **Filtres** : puces de clusters (un ou plusieurs, ou Tous les clusters),
  état (en marche, arrêtées, en démarrage, en pause, en échec, autre),
  namespace, et texte libre (nom, IP, nœud, cluster, étiquette `clé=valeur`).
  **Tri** sur chaque colonne, dans les deux sens. Filtres et tri sont
  retenus par le navigateur. La liste se rafraîchit toutes les 15 secondes
  tant qu'elle est à l'écran.
- **Gestes**, avec le cluster de la ligne : démarrer ou arrêter, console,
  snapshots, migration, réglages, notes, et le menu d'actions complet. La
  **sélection peut mêler plusieurs clusters** : démarrer, arrêter,
  redémarrer, arrêt forcé et migrer partent cluster par cluster, chaque
  geste étant une action suivie.

Vérifié en réel sur cinq clusters (deux physiques, deux imbriqués, un
éteint) : 19 VMs lues en 40 ms grâce aux lectures partagées, une VM de
`harvlab` arrêtée depuis sa ligne puis redémarrée par une action groupée,
`kubectl` confirmant chaque étape.

### Une ligne commune aux VMs Harvester et KVM (2.0.0)

`GET /api/vms-all?kinds=harvester,kvm` liste les VMs de tous les clusters et
de tous les hôtes KVM en une seule liste (sans `kinds`, la réponse reste
celle de la 1.85.0). Chaque ligne garde ses champs et reçoit les mêmes champs
communs quelle que soit sa famille : `kind` (`harvester` ou `kvm`), `source`
(cluster ou hôte), `namespace` (null pour KVM), `name`, `phase`, `cpu`,
`memory_bytes`, `node`, `ips`, `labels`, `uuid`, `key`
(`harvester/<cluster>/<ns>/<nom>`, `kvm/<uuid>` : un domaine renommé garde sa
clé), `folder` et `folder_source` (`vm` quand l'objet le porte, `console`
quand seule la console le garde, null). Les états de libvirt deviennent des
phases : running, idle et blocked donnent `Running`, shut off `Stopped`,
paused et pmsuspended `Paused`, crashed `Failed`, in shutdown `Stopping`, tout
autre `Unknown`. `sources` donne un état par cluster et par hôte ; un hôte KVM
dont les dossiers n'ont pas tous pu être lus le dit dans son `error`, et ces
VMs gardent le dossier que la console connaissait. Une annotation de dossier
qui n'est pas un chemin valide est ignorée et listée dans `folder_warnings`.
Vérifié en réel sur harv1, gpudemo et node2 : 21 VMs Harvester et 16 VMs KVM,
UUID identiques à `virsh list --all --uuid --name`.

### Les dossiers en ligne de commande (2.0.0)

`bin/harvester-folders.py` crée, renomme, déplace et supprime les dossiers,
et y range des objets (VMs, charges de conteneurs, hôtes). Les fenêtres de
la console (à venir) le lancent en action suivie ; il tourne aussi seul, sur
un hôte airgap, avec la seule bibliothèque standard.

```
harvester-folders.py [--db F] [--json] [--as PERSONNE] [--role viewer|operator|admin] [--id=ACTION] COMMANDE
  tree
  create --parent <id ou /> --name N
  rename --id I --name N [--spec F]          renomme, puis réécrit chaque objet rangé dedans
  move-folder --id I --parent <id ou /> [--spec F]
  delete --id I [--spec F]                   refusé tant que quelque chose y est rangé
  assign --to <chemin ou unfiled> --spec F   une ligne de journal par objet
  resume --action A --spec F                 lignes pas encore faites (interruption, hôte éteint)
  undo --action A --spec F                   retour à l'état d'avant ; conflits dits
  apply-rename --id I --spec F               objets restés sous l'ancien chemin
  write-back --folder I --spec F             rangements de la console que la personne peut désormais écrire
  empty-into --folder I --to <id ou unfiled> --spec F
                                             déplace le contenu (objets, sous-dossiers), puis supprime
```

Avant d'écrire, la commande relit le dossier que l'objet porte en ce moment
(`kubectl get`, `virsh metadata`, la déclaration) : le journal garde cette
valeur exacte, qu'Annuler remet même si l'objet a été rangé hors de la
console depuis la dernière lecture. Annuler et Reprendre relisent aussi
l'objet : s'il porte maintenant un dossier qui n'est ni celui laissé par
l'action ni sa cible (rangé ailleurs hors de la console, ou annotation
retirée), il est laissé tel quel et compté comme conflit. Un objet que la
personne ne peut pas lire, ou qui n'existe plus, n'est jamais rangé (sa
ligne échoue).
`empty-into` déplace les objets d'un dossier vers la cible, ses sous-dossiers
sous elle (sous la racine pour `unfiled`), puis supprime le dossier dans la
même action ; rien ne bouge si un sous-dossier ne peut pas y aller (nom pris,
bornes), et le dossier reste tant que quelque chose y est rangé. Annuler
recrée le dossier et remet tout ; si un dossier ne peut pas revenir (nom
repris depuis, ancien parent disparu), ses objets restent avec lui et sont
comptés comme conflits. Reprendre rejoue les lignes de dossier pas encore
faites : le déplacement d'un sous-dossier coupé par un arrêt de la console,
ou la suppression finale une fois le dernier objet parti. Un objet que la personne ne voit pas (un
cluster que Rancher lui cache, un espace de noms qu'elle ne lit pas) n'est
jamais nommé dans les étapes, la sortie ou le `--json` d'un renommage, d'un
déplacement, d'un vidage, d'une annulation ou d'une reprise : ces objets sont
comptés dans une seule étape (« 2 other objects not shown to you: 0 done, 2
failed »). Depuis la console, `visible` dans la demande liste les clés des
lectures de flotte de la personne (plus les objets qu'elle a choisis pour
`assign`) ; un objet qu'elle a pu relire pendant l'action est nommé lui
aussi. Sans `visible` (ligne de commande d'un exploitant), tout est nommé.

La demande (`--spec`) liste les objets, chacun avec `kind` (`harvester`,
`container`, `kvm`, `host`) et ses champs (`cluster`, `namespace`, `name`,
`workload_kind`, `host`, `domain`, `uuid`, `family`), `write` (faux quand la
personne n'a pas le droit d'écrire l'objet) et, depuis la console, le
kubeconfig préparé pour la personne par cluster. L'écriture sur l'objet : un
patch d'annotation contrôlé d'abord à blanc par le serveur (un refus du
webhook fait échouer cette ligne, les autres continuent), `virsh metadata`
avec `--config` et `--live` si le domaine tourne (un domaine transitoire est
écrit à chaud et gardé aussi par la console), le champ `folder` de la
déclaration de l'hôte. Quand la personne n'a pas le droit d'écrire l'objet,
ou que la RBAC refuse le patch (`cannot patch resource`), le rangement est
gardé par la console seule ; un refus de webhook ou de quota, même en code
403, fait échouer la ligne avec sa raison ; un objet qui porte un dossier qu'elle ne peut pas changer le garde.
Tant qu'un renommage ou un déplacement a des objets à réécrire (en cours,
ou en échec pour un objet que la personne ne voyait pas), une lecture de
flotte montre ces objets sous le nouveau chemin et ne recrée jamais l'ancien
dossier ; `apply-rename` les réécrit et retire un ancien dossier resté vide.
Un lecteur lit l'arborescence et ne change rien ; sans `--role`, la commande tourne en lecteur. Codes de sortie : 0 fait,
1 refus ou un objet en échec, 2 demande invalide (nom, chemin, profondeur,
longueur, objet inconnu). Avec `--json`, toute sortie en erreur écrit un seul
objet `{"ok": false, "error", "kind"}` sur stdout ; une erreur inattendue
n'est dite que par son type, sans trace ni chemin.

Vérifié en réel : sur harv1, une VM arrêtée puis en marche et un Deployment
rangés, déplacés, remis par Annuler puis sortis du dossier (annotation relue
par `kubectl`, génération de la VM et VMI inchangées, aucune condition de
redémarrage ; la génération d'un Deployment monte de un à tout changement
d'annotation, comme le fait Kubernetes, sans nouveau ReplicaSet), un compte
de service au rôle view gardé dans la console, puis écrit sur les objets
avec un kubeconfig d'opérateur ; sur node2, un domaine de test sans disque
rangé arrêté puis en marche, le dossier gardé après un redémarrage et par un
clone, puis retiré ; les 15 autres domaines identiques à l'octet près.

### Vue Machines virtuelles, dossiers et vue Dossiers (2.0.0)

**Machines virtuelles** (`#vms`) liste toutes les VMs de la flotte,
Harvester et KVM, depuis `/api/vms-all?kinds=harvester,kvm` : hôte,
pastille de famille, espace de noms, état, CPU, mémoire, IP, nœud et
dossier, avec une pastille **console** quand seule la console connaît le
rangement (la personne ne peut pas écrire l'objet) et une pastille **DR**
vers le contrat de réplication qui protège la VM. Puces de sources, filtres
d'état, d'espace de noms et de texte et tri par colonne sont repris de la
1.85. Le tableau dessine les 300 premières lignes de la liste filtrée et
triée ; **Afficher 300 de plus** ajoute les suivantes. La sélection, **Tout
cocher** et le glisser portent sur toute la liste filtrée, pas seulement sur
les lignes dessinées. Le filtre de texte s'applique 150 ms après la dernière
touche, et une relecture qui rapporte la même liste ne redessine rien
(sinon, seules les lignes qui ont changé sont refaites). Mesuré dans
Chromium sans affichage : à 2 000 VMs, un rendu sans changement prend
environ 0,1 s et un rendu complet (nouveau tri) environ 0,6 s ; à 10 000 VMs
environ 0,15 s et 0,9 s (avant : 3,6 s à chaque relecture et à chaque
touche, à 2 000). Le menu d'une ligne suit sa famille : celui des VMs Harvester, ou pour
une VM KVM la console, les instantanés, le clonage, la suppression et
**Ouvrir sur son hôte** (démarrer et arrêter sont sur la page de l'hôte).
Démarrer, arrêter, redémarrer, arrêt forcé et migrer une sélection
s'appliquent à ses VMs Harvester.

À gauche, un panneau à deux modes, comme vCenter. **Dossiers** : Toutes les
VMs, Non rangées (non modifiables) et l'arborescence partagée avec, pour
chaque dossier, les VMs que vous voyez dedans (sous-dossiers compris ; une
VM cachée par vos droits n'est jamais comptée). Un dossier recréé par une
lecture de flotte depuis les objets qui le nomment porte la pastille
**depuis les objets**. **+ Dossier**, **Renommer** et **Supprimer** se font
en saisie sur place (Entrée valide, Échap annule, aucune fenêtre du
navigateur). Supprimer un dossier vide se confirme dans le panneau ; un
dossier non vide ouvre **Déplacer le contenu de...** : choisir un dossier
cible ou Non rangées, ses objets et sous-dossiers y vont, puis le dossier
est supprimé, en une action qu'Annuler défait. **Hôtes** : les VMs
regroupées par famille puis par hôte, en lecture seule ;
`#vms/host/<famille>/<hôte>` l'ouvre sur un hôte. `#vms/host/kvm` garde la
vue de toutes les VMs KVM et leurs gestes.

Ranger : glisser une ligne, ou une sélection, sur un dossier (Non rangées
les en sort), ou **Déplacer vers un dossier** sur une ligne ou la
sélection, avec recherche de dossier. La fenêtre montre les contrôles du
serveur objet par objet avant son bouton : écrit sur l'objet, gardé par la
console seulement, ou échouera (raison dite) ; les objets qui échoueraient
sont laissés de côté. Ranger un hôte (depuis l'index des hôtes) propose
**Ranger aussi ses N VMs**, décoché : un hôte ne range jamais ses VMs de
lui-même. Chaque geste est une action suivie ; le dock propose **Annuler**
à sa fin, et l'onglet Activité ensuite (grisé avec la raison pour un lecteur). Un lecteur voit les dossiers et leur contenu, chaque contrôle de
rangement est grisé avec la raison, et les lignes ne se glissent pas.

**Dossiers** (`#folders`, `#folders/<id>`, première entrée du menu latéral)
montre le contenu d'un dossier en trois groupes (VMs, conteneurs, hôtes),
chacun ouvrant sa vue filtrée sur le dossier : `#vms/folder/<id>`,
`#containers/folder/<id>` et `#hosts/folder/<id>`, sous-dossiers compris.
Conteneurs et l'index des hôtes ont une colonne Dossier et **Déplacer vers
un dossier** (menu d'une charge pour les Deployments, StatefulSets,
DaemonSets et CronJobs ; un bouton par hôte).

Vérifié en réel sur harv1 et node2 avec une console de l'arbre de
développement (port propre, configuration copiée, état à part, comptes
admin, opérateur et lecteur temporaires) : une VM de test arrêtée rangée par
glisser-déposer, un domaine KVM de test et un Deployment par le menu, puis
`/Essais` renommé `/Essais2` (trois objets réécrits), annulé depuis le dock,
et vidé vers Non rangées (son sous-dossier remonté à la racine) puis
supprimé, ce vidage annulé (dossier recréé) ; une annotation posée avec
kubectl dans le dos de la console remise exactement par Annuler ; un hôte
déclaré par la console et un cluster de config.yaml (console seulement)
rangés puis remis ; un lecteur avec chaque contrôle grisé et un 403 sur un
appel direct. Chaque écriture relue avec kubectl et virsh ; la génération de
la VM n'a jamais changé, celle du Deployment a monté de un par changement
d'annotation (comportement de Kubernetes), rien d'autre n'a changé ; les 15
autres domaines de node2 identiques à l'octet près.

### Routes des dossiers et contrôles avant geste (2.0.0)

La console lance `harvester-folders.py` en action suivie, avec le rôle de
la personne (`--role`, jamais le défaut) et le kubeconfig préparé pour elle
par cluster (usurpation et Rancher compris ; jamais dans une réponse ni dans
la ligne de commande montrée). Les routes sont sous `/api/folders`, pas sous
`/api/clusters` (réservé aux admins) : tout changement demande le rôle
opérateur, la lecture est ouverte à tous.

| Route | Rôle |
|---|---|
| `GET /api/folders` | l'arborescence, chaque dossier avec son `origin` (`console`, ou `objects` quand une lecture de flotte l'a créé depuis les objets qui le nomment) et ses `counts` `{vms, containers, hosts}`, sous-dossiers compris, limités à ce que la personne a vu dans ses propres lectures de flotte (`null` : pas encore lu) |
| `POST /api/folders` | créer (`parent_id`, `name`) ; un frère du même nom répond 409 `{conflicts}` avant que rien ne parte |
| `PATCH /api/folders/<id>` | renommer (`name`) ou déplacer (`parent_id`, `null` pour la racine), un changement à la fois ; ses objets sont réécrits dans la même action. Les dossiers sont triés par nom : `position` est refusé. Un frère du même nom (au nouveau nom ou sous le nouveau parent) répond 409 `{conflicts}` avant que rien ne parte |
| `DELETE /api/folders/<id>` | supprimer un dossier vide ; un dossier non vide répond 409 avec ce que la personne voit (`count_visible`, `subfolders`, et `hidden` sans nombre) |
| `POST /api/folders/<id>/empty-into` | `target_id` (un dossier ou `unfiled`) : déplacer le contenu puis supprimer, en une action ; un sous-dossier dont le nom est déjà pris dans la cible répond 409 avant que rien ne bouge |
| `POST /api/folders/assign` | `objects` (clés ; une VM KVM donne aussi son `host`) et `to` (identifiant de dossier ou `unfiled`) |
| `POST /api/folders/check` | les contrôles montrés avant un déplacement, objet par objet, rien n'est écrit |
| `POST /api/folders/actions/<id>/undo` | annuler une action de dossiers terminée |
| `POST /api/folders/actions/<id>/resume` | reprendre une action de dossiers interrompue : les lignes pas encore faites (en attente ou en échec) sont réécrites, jamais celles faites ; 409 tant qu'elle tourne |
| `POST /api/folders/<id>/write-back` | écrire sur les objets les rangements du dossier gardés par la console seule |

Chaque changement répond tout de suite `202 {"action_id"}` ; le dock montre
l'action, et un rangement, un renommage, un déplacement, un vidage ou une
annulation terminés proposent **Annuler** (`undo.path` dans l'action), dans
le dock puis dans l'onglet Activité, aussi après un redémarrage de la console
(`undo` gardé dans actions.db). Tout opérateur peut annuler le geste d'un
autre ; l'annulation dit de qui était le geste et qui l'annule (première
étape, et `result` `{"undoes", "of", "by"}`). Une action finie en erreur ou
arrêtée par l'arrêt de la console propose **Reprendre** dans le dock et
dans l'Activité (`resume.path` dans l'action, posé par le serveur) : elle
finit seulement les objets que l'action n'a pas atteints, et la reprise
d'une reprise vise l'action d'origine. Un domaine KVM est désigné par son
UUID, que virsh accepte : un domaine renommé est retrouvé.

`POST /api/folders/check` répond `{"objects": [{"key", "write", "outcome",
"reason", "check"}]}`. `outcome` dit ce que fera le déplacement : `object`
(le dossier est écrit sur l'objet), `console` (seul le rangement de la
console change) ou `fail` (le geste échouera pour cet objet, raison dite).
VMs et charges Harvester : `kubectl auth can-i patch` avec le kubeconfig de
la personne (une SelfSubjectAccessReview, `check: ssar`), demandé une fois
par cluster, espace de noms et ressource pour plusieurs objets, puis par nom
seulement si la réponse n'est pas « yes » ; un cluster qui ne répond pas est
dit sans l'interroger. Pour un compte local de la console sans délégation
d'identité, ce kubeconfig est celui de la console : la raison dit alors que
la console écrit l'objet avec sa propre identité, et non que la personne le
peut. La fenêtre garde le contrôle de chaque objet tant qu'elle est ouverte :
changer de cible ne redemande rien. KVM : hôte joint et domaine persistant
(un `dominfo` par domaine dans un seul appel SSH par hôte, `check: virsh`) ;
un domaine transitoire est gardé par la console. Hôte : sa déclaration
(`check: decl`) ; un cluster de `config.yaml` est rangé dans la console
seulement.

Les lectures de flotte donnent aussi le dossier des charges de conteneurs
(`key`, `folder`, `folder_source` sur `/api/containers/<cluster>` et
`/api/containers-all` pour les Deployments, StatefulSets, DaemonSets et
CronJobs) et des hôtes (`/api/hosts` : le champ `folder` de la déclaration,
sinon le rangement de la console). Une lecture de flotte crée chaque
dossier nommé par des objets, sans plafond ; ces dossiers portent l'origine
`objects`, montrée par une pastille dans le panneau. Mesuré : 2 000 de ces
dossiers créés en une lecture en 0,3 s (0,08 s pour une lecture sans
changement), `GET /api/folders` avec 2 044 dossiers et 2 000 objets en
0,05 s, le panneau dessiné en 0,1 s environ dans le navigateur.

### Charges de conteneurs (1.91.0)

Harvester 1.9.1 passe en GA les conteneurs bare-metal : des charges
Kubernetes ordinaires déployées sur le cluster Harvester lui-même, à côté
des VM (expérimental depuis Harvester 1.2). **Containers** est un groupe de
deux entrées, juste sous Virtual machines : **Conteneurs du cluster choisi**
(le cluster choisi en haut) et **Conteneurs de tous les clusters**, tous les
clusters déclarés dans un tableau avec une colonne **Cluster**.

- **Gestes (1.93.0)** : créer, mettre à l'échelle, redémarrer et supprimer,
  chacun une action suivie (dock, Activité) exécutée par
  `bin/harvester-containers.py`. Voir plus bas.
- **Ce qui est listé** : Deployments, StatefulSets, DaemonSets, Jobs,
  CronJobs et pods seuls, lus en une passe par cluster. **Les namespaces
  système sont masqués** : `kube-*`, `cattle-*`, `harvester-*`,
  `longhorn-system`, `fleet-*`, `cluster-fleet-*`, `*-system` (contrôleurs
  de Rancher Turtles et de Cluster API), `kubeovn-*`, les `user-?????` de
  Rancher, `cdi`, `local` (`default` est montré). Les
  pods des VM (`kubevirt.io=virt-launcher`) et des imports CDI sont écartés.
  Un pod est rangé sous sa charge (pod, ReplicaSet, Deployment ; pod, Job,
  CronJob) ; un pod seul est une charge à lui.
- **Une ligne par charge** : nom, type, namespace, cluster, prêts / voulus,
  images, GPU demandés (somme des `nvidia.com/*` et `amd.com/gpu` des
  conteneurs d'un pod), âge, état (`Running`, `Progressing`, `Degraded`,
  `Failed`, `Completed`, `Suspended`). **On déplie une ligne** pour voir ses
  pods : nœud, état, redémarrages, âge.
- **Menu de la ligne** : le YAML (fenêtre en lecture seule, ou
  téléchargement) et les **journaux d'un pod**, les 500 dernières lignes au
  plus, conteneur au choix.
- **Filtres et tri** comme la vue de toutes les VM : texte (nom, image, pod,
  nœud, étiquette), cluster, namespace, type, état ; chaque colonne se trie
  dans les deux sens.
- Un bandeau dit, sans alarmer, que la fonction est expérimentale avant
  Harvester 1.9.1, que les namespaces système sont masqués et que les
  conteneurs vont dans d'autres namespaces que les VM.
- Un cluster éteint, injoignable ou qui refuse la lecture est dit, jamais
  une liste vide muette. Un cluster qui ne refuse qu'un type est listé avec
  ce qui a été lu, et le refus est dit.

API : `GET /api/containers/<cluster>`, `GET /api/containers-all`,
`GET /api/containers/<cluster>/<namespace>/pod/<pod>/logs?container=&tail=` ;
le YAML passe par la route générique `/api/yaml/<cluster>/<type>/<nom>`
(types `deployment`, `statefulset`, `daemonset`, `replicaset`, `job`,
`cronjob`, `pod`, jamais modifiables). En ligne de commande :
`bin/harvester-containers.py list [--cluster NOM | --kubeconfig FICHIER] [--json]`
donne la même lecture, tous les clusters déclarés par défaut.

Vérifié en lecture sur harv1 (Harvester 1.9.0) : 118 pods lus, namespaces
système et pods de VM écartés, les quatre contrôleurs Cluster API de la
console listés avec leur pod. L'essai avec une charge déployée pour
l'occasion revient au mainteneur.

#### Gestes sur les charges de conteneurs (1.93.0)

- **Créer** : le bouton **Créer** des deux vues ouvre une fenêtre à deux
  onglets. **Formulaire** : cluster, nom, namespace, type (Deployment ou
  Job), image, nombre de pods (un Job les lance tous en parallèle),
  commande, variables (une ligne `NOM=valeur` chacune), GPU demandés et leur
  ressource (`nvidia.com/gpu` par défaut), limite CPU, limite mémoire.
  **YAML** : coller le manifeste d'un Deployment, StatefulSet, DaemonSet,
  Job, CronJob ou Pod. Une case crée le namespace s'il manque ; sans elle,
  un namespace absent est refusé. Le champ GPU dit combien de GPU de
  conteneurs sont libres sur le cluster choisi.
- **Chaque conteneur a une limite mémoire.** 256Mi si aucune n'est donnée,
  avec des demandes de mémoire et de CPU assorties (100m de CPU par
  défaut). Un manifeste collé garde ce qu'il dit et ne reçoit que ce qui
  manque.
- **Menu de la ligne** : **Mettre à l'échelle** (Deployment, StatefulSet,
  ReplicaSet ; 0 arrête sans supprimer), **Redémarrer** (redémarrage
  progressif d'un Deployment, StatefulSet ou DaemonSet : l'annotation
  `kubectl.kubernetes.io/restartedAt`, comme `kubectl rollout restart`),
  **Supprimer** (tout type, après une confirmation dans la page). Une entrée
  qu'un type ne prend pas est grisée et dit pourquoi.
- **Sûreté** : les namespaces système sont refusés, par la console puis par
  le script. Le formulaire est contrôlé avant tout envoi. Le serveur
  contrôle chaque écriture d'abord (`--dry-run=server` : webhooks, quotas,
  droits) ; rien n'est écrit si ce contrôle échoue, et un namespace créé
  pour une charge refusée est retiré. Les écritures se font avec votre
  identité.
- À la fin de l'action, la liste est relue.

API : `POST /api/containers/<cluster>/create` (`{spec: {...}}` ou
`{yaml: "..."}`, `create_namespace`), `POST
/api/containers/<cluster>/<namespace>/<type>/<nom>/scale` (`{replicas}`),
`POST .../restart`, `DELETE /api/containers/<cluster>/<namespace>/<type>/<nom>`.
Chacune répond `202 {action_id}` tout de suite. En ligne de commande :
`bin/harvester-containers.py create|scale|restart|delete`, avec `--cluster`
ou `--kubeconfig`, `--dry-run` pour le seul contrôle du serveur, et
`create --file MANIFESTE` ou les options du formulaire (`--name`,
`--namespace`, `--kind`, `--image`, `--replicas`, `--command`, `--env`,
`--gpus`, `--gpu-resource`, `--cpu`, `--memory`, `--create-namespace`).

Vérifié en réel sur harv1 (Harvester 1.9.0), dans un namespace d'essai créé
pour l'occasion puis supprimé : un Deployment et un Job créés en ligne de
commande, un Deployment et un pod créés par les routes de la console, mise
à l'échelle à 2 puis 0, redémarrage progressif (nouveaux pods, anciens
partis), une création refusée par le contrôle du serveur (nom déjà pris),
une mise à l'échelle dans `kube-system` refusée, puis toutes les charges
supprimées.

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

### Hôtes : menu latéral, index et pages d'hôte (2.0.0)

Un hôte est un cluster Harvester entier ou un hyperviseur KVM. Depuis la
2.0.0, la console s'organise autour des hôtes au lieu d'un cluster choisi ;
la correspondance de chaque écran 1.x avec sa nouvelle adresse est dans le
CHANGELOG (2.0.0) et dans la procédure opérationnelle.

- **Menu latéral.** Sous **Hôtes**, chaque cluster Harvester et chaque
  hôte KVM en deux familles repliables, chacune avec un résumé d'une ligne
  (`58 ok, 2 en alerte`). Chaque hôte a une pastille d'état, son nombre de
  VMs, une étoile (favoris gardés par personne par la console, retrouvés
  depuis un autre poste) et, pour un hôte Harvester, une pastille de ses
  alertes de Disaster Recovery. Pas de plafond : tous les hôtes sont
  listés, et un hôte en alerte reste visible dans une famille repliée.
  L'hôte ouvert déplie son sous-menu (Vue d'ensemble, Machines virtuelles,
  Conteneurs, Imports et migrations, Paramètres, Disaster Recovery). La
  recherche (`/`) filtre les hôtes par nom, étiquette et famille et propose
  les VMs déjà lues. **Ajouter un hôte** propose Harvester ou KVM. Autour :
  Dossiers, Machines virtuelles, Conteneurs, Disaster Recovery,
  Migrations, Ressources partagées, Automatisation et Activité, tous de
  vrais liens.
- **Index des hôtes** (`#hosts`, `#hosts/harvester`, `#hosts/kvm`,
  `#hosts/folder/<id>`) : un tableau de tous les hôtes avec famille, état,
  version, étiquettes, VMs, dossier et favori ; tri et filtre sur chaque
  colonne, **Déplacer vers un dossier** sur une sélection. La famille KVM
  ajoute Déclarer un hôte, Tester et Retirer par ligne, et trois onglets
  sur tous les hôtes KVM : Capacité, Pools et Réseaux.
- **Page d'un hôte Harvester** (`#harvester/<c>`) : un bandeau (famille,
  nom, état, version, nombre de VMs, sous-menu, Activité de cet hôte)
  au-dessus des sections existantes. Vue d'ensemble = métriques avec le
  bloc Événements dessous ; Paramètres = Nœuds, Stockage, Réseau, Modules,
  Sécurité (avec les Comptes du cluster), Monitoring et journaux, Avancé,
  Mise à jour, Alimentation (Démarrage, Arrêt) et Connexion ; Imports et
  migrations = Import de VM et Migrations VMware. Cliquer un autre hôte
  Harvester garde l'écran.
- **Page d'un hôte KVM** (`#kvm/<h>`) : voir la section 10.

Chaque écran a une adresse après `#` : un lien ouvre l'écran exact et F5 le
rend. Les jetons 1.x (`#storage`, `#allvms`, `#kvmhosts`...) ouvrent encore
leur écran.

### Les sections de Harvester, un cluster à la fois (1.57.0)

Le menu latéral regroupe, sous Cluster, les menus que les opérateurs
connaissent de Harvester, pour le cluster choisi (depuis la 2.0.0 : les
Paramètres de chaque hôte Harvester, voir plus haut) :

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
  l'Aperçu. Depuis la 2.0.0 elles sont dans les onglets Paramètres de
  l'hôte, et la vue Cluster est Paramètres > Nœuds.
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

Sous les comptes du cluster (Paramètres > Sécurité > **Comptes du cluster**
de l'hôte depuis la 2.0.0, avant dans les Réglages), **Membres
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

La Vue d'ensemble de l'hôte montre un bloc **Événements** sous les métriques
(un onglet avant la 2.0.0 ; `#harvester/<c>/overview/events` y fait
défiler) : les événements du cluster, rangés comme
sur le tableau de bord de Harvester (hôtes, VMs, volumes, images), avec leurs
nombres, les avertissements marqués, un filtre « avertissements seulement »
et une recherche ; relus toutes les 20 secondes. Les métriques montrent
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

### Inventaire des GPU (1.92.0, Shared resources depuis 1.93.0)

**Shared resources** est un groupe de premier niveau du menu, juste après
Containers : ce que VM et conteneurs se partagent. Les GPU d'un nœud servent
les deux (passthrough, vGPU, vGPU adossé à MIG pour les VM ; GPU entiers
pour les conteneurs). Sa première entrée, **GPU**, liste les GPU de tous les
clusters déclarés. La vue ne suit pas le cluster choisi en haut : elle montre
toute la flotte, avec un filtre **Cluster** (tous par défaut) et une colonne
Cluster. Depuis 1.93.0, elle n'est plus dans Cluster > Advanced. C'est en
lecture seule. Une unité
est ce qu'une réservation donne (Bookings, 1.94.0) :

- **GPU entier** : un périphérique PCI de classe `0300` (VGA) ou `0302` (3D),
  NVIDIA (`10de`) ou AMD (`1002`), donné à une VM en passthrough. Un GPU
  intégré Intel n'est pas compté. Les fonctions virtuelles d'un GPU SR-IOV
  ne comptent pas comme des GPU entiers.
- **vGPU** : un `VGPUDevice`, avec son type de vGPU (`NVIDIA A2-4Q`) et son
  GPU parent (le label `harvesterhci.io/parentSRIOVGPUDevice`, ou l'adresse
  du parent et le nœud).
- **Instances MIG** : une ligne par GPU qui porte une `MigConfiguration`.
  Elle montre les instances créées par profil, combien on peut encore en
  créer, et l'état de synchronisation (`synced`, `out-of-sync` avec le
  message de Harvester, `disabled`). Une instance est utilisée par un vGPU
  adossé à MIG : les instances utilisées d'un GPU sont ses vGPU utilisés.
- **GPU des conteneurs** : les ressources `nvidia.com/*` et `amd.com/gpu`
  d'un nœud marqué `harvesterhci.io/gpu-baremetal-workloads=true`. Les noms
  de ressource des passthrough et des vGPU, que les nœuds annoncent aussi
  pour KubeVirt, sont écartés.

Chaque unité a un état : **libre**, **utilisée** ou **désactivée**. Un GPU
entier est désactivé quand son passthrough est coupé, ou quand il est
découpé en vGPU, partitionné par MIG ou donné aux conteneurs ; la bulle dit
lequel. Le consommateur est une VM (son spec et l'annotation d'allocation
que Harvester pose au démarrage) ou un pod (ses demandes de GPU). Une VM
ouvre ses réglages ; un pod ouvre All clusters containers, filtré sur lui.
Les pods des namespaces système comptent aussi, marqués, sans lien (la vue
Containers les masque).

En tête, une carte par type : total, utilisées, libres. Une unité
désactivée compte dans le total, pas dans les libres. Filtres : cluster,
nœud, type, état, texte. Les cartes suivent les
filtres de cluster et de nœud.

Rien n'est muet. Sans l'add-on `pcidevices-controller`, la vue le dit et
renvoie vers Add-ons ; les GPU des conteneurs restent listés. Un type de
ressource vGPU ou MIG absent est dit « non disponible », jamais une erreur.
Un cluster sans GPU dit « Aucun GPU sur ce cluster » quand il est le filtre
choisi. Un cluster éteint, qui refuse la lecture ou sans l'add-on est dit.

API : `GET /api/gpus/<cluster>`, `GET /api/gpus-all`, lues avec votre
identité. Ligne de commande :
`bin/harvester-gpus.py list [--cluster NOM | --kubeconfig FICHIER] [--json]`.

**Ce qui est vérifié, et ce qui ne l'est pas.** Les cas vGPU, MIG et GPU de
conteneurs sont vérifiés sur les formes des objets de Harvester (les
ressources `devices.harvesterhci.io/v1beta1` et les contrôleurs de
harvester/pcidevices), reproduites en fixtures de tests. Ils ne sont **pas
vérifiés sur du vrai matériel vGPU ou MIG** : nos bancs n'en ont pas. La
lecture réelle a été faite sur harv1 (Harvester 1.9.0, add-on activé, tous
les types de ressource présents, un GPU intégré Intel) : la vue dit
« Aucun GPU sur ce cluster ».

### Réservations de GPU (1.94.0)

**Shared resources > Bookings** réserve des unités GPU d'un cluster sur un
créneau, pour une personne ou un groupe. **Une réservation retient le
créneau dans la console ; elle n'est pas encore appliquée sur le
cluster** : rien n'est attaché, rien n'est arrêté en fin de créneau. La
vue et le formulaire le disent. Les quotas (1.95.0, plus bas) sont
contrôlés par la console à la création d'une réservation, pas sur le
cluster.

Ce qui se réserve :

- une **unité précise** de l'inventaire : un GPU entier ou un vGPU (une
  unité, un créneau) ;
- un **nombre d'unités d'un modèle** sur le cluster : des GPU entiers d'un
  modèle, des vGPU d'un type (`NVIDIA H100-20C`), des instances MIG d'un
  profil (`MIG 3g.40gb`), des GPU de conteneurs d'une ressource
  (`nvidia.com/gpu`).

Une réservation a un titulaire (vous par défaut, un autre compte de la
console, ou un groupe : un rôle de la console ou un nom de groupe Rancher,
en texte libre), un début et une fin (la fin après le début, 90 jours au
plus, pas déjà passée), une cible facultative (une VM ou une charge de
conteneurs du cluster) et une note. Son état est calculé : **à venir**,
**en cours**, **terminée** ou **annulée**.

**Les conflits sont refusés à la création**, avec la raison :

- la même unité réservée sur des créneaux qui se chevauchent ;
- à un instant du créneau, plus d'unités d'un type et d'un modèle
  réservées que le cluster n'en a. Une réservation d'une unité précise
  compte dans son modèle.

Les créneaux sont semi-ouverts : un créneau qui finit à 13 h et un autre
qui commence à 13 h ne se chevauchent pas. La capacité vient de
l'inventaire GPU du cluster, lu avec votre identité : les unités qui ne
sont pas désactivées ; pour le MIG, les instances **créées** du profil
(une instance possible mais pas créée ne se donne à personne) ; pour les
conteneurs, la ressource allouable des nœuds non cordonnés. « Libres
maintenant » est la capacité moins le plus grand des deux nombres : unités
utilisées, unités réservées maintenant. Un vGPU adossé à MIG et l'instance
MIG derrière lui sont comptés à part (limite connue).

Vues :

- **Calendrier** : une ligne par type et modèle réservable de chaque
  cluster affiché, réservé ou non (une ligne vide : rien de réservé cette
  semaine), avec le nombre d'unités libres maintenant (« Libres maintenant : 1
  sur 2 »). Plus une ligne pour un modèle qui n'a plus que des
  réservations. La semaine à partir du lundi, les créneaux en barres
  (empilées quand elles se chevauchent, les vôtres soulignées), une marque
  pour maintenant, semaine précédente et suivante. Le filtre par cluster
  s'applique aux lignes.
- **Mes réservations** : celles que vous avez créées ou dont vous êtes
  titulaire.
- **Toutes les réservations** : toutes, avec **Annuler** pour celles que
  vous pouvez annuler, confirmé dans la page.

**Nouvelle réservation** ouvre un formulaire : cluster, type, quoi
(n'importe quelle unité d'un modèle, avec combien sont libres maintenant,
ou une unité précise), nombre, titulaire, début et fin à votre heure locale
(par défaut : dans une heure, pour quatre heures), cible, note. Le créneau
est contrôlé pendant la saisie et le formulaire dit pourquoi il serait
refusé. Une fois la réservation acceptée, le formulaire se ferme : l'action
reste dans le dock et une notification dit quand elle est finie. Un refus
garde le formulaire ouvert avec la raison.

Ailleurs :

- **Shared resources > GPU** dit sur chaque unité si elle est réservée
  maintenant ou dans la journée, et un bouton **Book** ouvre le formulaire
  prérempli (l'unité pour un GPU ou un vGPU, le profil ou la ressource
  sinon). Une réservation de cette unité précise dit « Réservée par
  <titulaire> jusqu'au <fin> ». Une réservation par nombre ne prend aucune
  unité en particulier : chaque unité du modèle dit « 1 sur 2 réservées
  maintenant » (en orange quand le modèle est entièrement réservé), les
  titulaires sont dans la bulle.
- Le formulaire de création de charge (GPU de conteneurs) et les réglages
  d'une VM (section passthrough : GPU, vGPU, MIG) disent ce qui est libre
  maintenant, ce qui est réservé maintenant, si vous tenez une réservation,
  avec un lien vers Bookings. Ces indications sont en lecture seule.

Droits : un lecteur lit ; un opérateur crée et annule les réservations
qu'il a créées ou dont il est titulaire ; un admin annule celles de tout le
monde. Créer et annuler sont des actions suivies (dock, Activité). Une
réservation annulée reste dans l'historique.

Stockage : une base SQLite de la console, `bookings.db` à côté de
`notes.db` (ou `HARVESTER_OPS_BOOKINGS_DB`), en mode WAL. La console la
lit ; seule la ligne de commande écrit, et elle refait le contrôle des
conflits dans sa transaction d'écriture : deux réservations lancées en même
temps ne peuvent pas passer toutes les deux.

API : `GET /api/bookings` (`?cluster=`, `?scope=mine`),
`GET /api/bookings/options/<cluster>`, `POST /api/bookings/check` (rien
n'est écrit), `POST /api/bookings` (202 avec l'identifiant de l'action, 409
avec les raisons), `POST /api/bookings/<id>/cancel`.

Ligne de commande : `bin/harvester-bookings.py list|check|create|cancel`.
La console lit l'inventaire GPU avec votre identité et le passe dans la
demande (`inventory` de `--spec`) : le script n'a pas besoin du cluster.
Seul, le script prend `--inventory FICHIER` (le JSON de
`harvester-gpus.py list --json`), ou lit le cluster par `--kubeconfig`, ou
le cluster déclaré du même nom.

**Ce qui est vérifié.** Essayé pour de vrai sur le cluster GPU simulé
`gpudemo` (banc kwok, objets pcidevices de forme réelle) : capacités lues
dans son inventaire, un vGPU précis refusé sur un créneau qui chevauche et
accepté bout à bout, trois `MIG 3g.40gb` refusées (deux créées), un vGPU
désactivé refusé, création et annulation en actions suivies. Les
réservations ne sont pas vérifiées sur du vrai matériel GPU.

### Usage des GPU et quotas (1.95.0)

**Shared resources > Usage** compte les GPU-heures d'une période (cette
semaine, ce mois, le mois précédent, ou une plage de jours) et règle les
quotas. Deux mesures, côte à côte, par titulaire :

- **Réservé** : unités x durée des réservations de la période, coupées à
  ses bornes. Une réservation annulée compte de son début jusqu'à son
  annulation si le créneau avait commencé, et pas du tout sinon.
- **Consommé** : les unités GPU réellement tenues, relevées par la console.
  Un échantillonneur de fond lit l'inventaire GPU de chaque cluster déclaré
  toutes les 5 minutes (`HARVESTER_OPS_USAGE_INTERVAL`, en secondes, de 30
  à 3600) avec le kubeconfig de la console (lecture seule, comme les
  surveillances de cluster, et avec le collecteur de la vue GPU), et range
  un relevé par unité tenue dans `usage.db` (à côté de `bookings.db`, ou
  `HARVESTER_OPS_USAGE_DB` ; relevés gardés 400 jours). Un relevé vaut le
  temps écoulé depuis le relevé précédent du même consommateur sur la même
  unité quand ce dernier date d'au plus deux intervalles, un intervalle
  sinon : un trou plus long (console arrêtée, cluster injoignable) n'est
  pas comblé. Un cluster injoignable, éteint ou illisible est noté et
  passé. `HARVESTER_OPS_USAGE=0` coupe l'échantillonneur.

Un consommateur (une VM, ou un pod) est rattaché à une personne ou un
groupe par une réservation qui le **cible** (la VM, ou la charge de
conteneurs dont le pod fait partie) sur le même cluster à cet instant.
Sinon il reste **sans réservation**, rangé sous son namespace. L'écart
entre les deux mesures est montré : le **gaspillage** est le temps réservé
et pas utilisé (réservation par réservation, sur la partie déjà passée),
le **hors règle** l'usage sans réservation, ou au-delà du nombre d'unités
réservées.

La vue : quatre totaux, un graphique des GPU-heures par jour (réservé en
barres cerclées, consommé en barres pleines, jours de votre fuseau), et un
tableau par titulaire (réservé, consommé, quota du mois avec une barre de
progression et le reste, gaspillage, hors règle). Une ligne se déplie sur
son détail par cluster et type d'unité. **CSV** télécharge le même rapport
depuis la console (une ligne par titulaire, puis une par cluster et type).
La vue dit quand l'échantillonneur est passé pour la dernière fois et
quels clusters il n'a pas pu lire, et le dit quand rien n'a encore été
relevé.

**Quotas**, par personne ou par groupe : un plafond de GPU-heures
**réservées** par mois calendaire (UTC) et, facultatif, un nombre maximal
d'unités réservées en même temps (tous clusters). Un administrateur les
règle dans la fenêtre **Quotas** de la vue (ou par le bouton **Quota**
d'une ligne) ; lecteurs et opérateurs les voient. Ils s'appliquent à la
création d'une réservation : elle est refusée, avec la raison, par le même
chemin que les conflits (le formulaire le dit pendant la saisie, la ligne
de commande refait le contrôle dans sa transaction d'écriture). Les
réservations déjà faites restent. Rien n'est appliqué sur les clusters. Le
quota est celui du titulaire : un opérateur qui réserve pour un groupe
consomme le quota du groupe.

Limites connues : une instance MIG est tenue par un vGPU adossé à MIG, son
usage compte donc comme ce vGPU (type `vgpu`) ; une unité tenue par un
consommateur que l'inventaire ne nomme pas n'est pas comptée ; les jours
du graphique suivent le décalage horaire du navigateur au moment de la
demande.

Droits : tout le monde lit le rapport et les quotas ; régler ou retirer un
quota demande le rôle admin et est une action suivie (dock, Activity).

API : `GET /api/usage?from=&to=&tz=` (bornes ISO 8601, par défaut le mois
en cours en UTC ; `tz` en minutes à l'est d'UTC), `GET /api/usage.csv`
(mêmes paramètres), `GET /api/usage/quotas`, `POST /api/usage/quotas`
(`{holder: {kind, name}, monthly_hours, max_units}`, 202 avec l'action),
`DELETE /api/usage/quotas` (`{holder}`).

Ligne de commande : `bin/harvester-bookings.py usage [--from --to] [--tz]
[--json | --csv]` et `bin/harvester-bookings.py quota list|set|delete`
(`set --holder USER | --group NOM [--monthly-hours H] [--max-units N]`).

**Ce qui est vérifié.** L'échantillonneur a tourné pour de vrai sur le
cluster GPU simulé `gpudemo` (banc kwok) avec des bases temporaires : il a
relevé les unités tenues par les VM `prod/train`, `ml/notebook`, `ml/big`
et les pods du Deployment `ml/trainer` (deux GPU, charge retrouvée depuis
le pod), noté un kubeconfig absent comme inconnu, et le rapport a rattaché
`ml/notebook` et `ml/trainer` à leurs réservations, les deux autres à leur
namespace. Une réservation au-delà du quota mensuel d'un groupe a été
refusée avec sa raison. Non vérifié sur du vrai matériel GPU.

### Mettre à jour Harvester (1.69.0)

**Mettre à jour**, dans l'en-tête de l'Aperçu (et sur le réglage
`server-version`, comme dans Harvester), ouvre une fenêtre par cluster.
Depuis la 2.0.0 le même contenu est aussi l'onglet Paramètres > **Mise à
jour** de l'hôte, où mène le bouton de la Vue d'ensemble.

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

**VM Import**, dans Imports et migrations d'un hôte Harvester depuis la
2.0.0 (`#harvester/<c>/imports/vmimport` ; section VM Import / Export du
menu depuis la 1.77.0, sous Cluster avant), pilote l'add-on vm-import-controller
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

**Migrations VMware**, dans Imports et migrations d'un hôte Harvester
depuis la 2.0.0 (`#harvester/<c>/imports/forklift`), juste après VM Import,
ouvre
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
des Linux) : elle est cochée d'office quand toutes les VMs sont sous
Linux. Quand un invité Windows est choisi, c'est la copie brute **en mode
compatibilité** (disques SATA, cartes e1000e, que Windows pilote sans
pilote supplémentaire) qui est cochée, la voie vérifiée en réel en
1.87.0 : une VM Windows Server 2025 (40 Gio, BIOS, LSI SAS, e1000e) passée à
chaud de vCenter à Harvester 1.9 en 2 min 03 s de bascule, qui a démarré en
gardant son MAC. La conversion de l'invité du Forklift livré pour Harvester
1.9 (virt-v2v 2.7.7) échoue sur ce Windows en lisant son registre
(`Hivex.Error`), une fois la source arrêtée ; décocher la copie brute pour
la tenter sur un Windows plus ancien. En copie brute, Windows voit une
nouvelle carte réseau : une IP fixe ne suit pas, l'invité repasse en DHCP.
Installer ensuite les pilotes virtio pour de meilleures performances disque
et réseau. Une VM déjà prise
par une vague ouverte, sur ce cluster ou sur un autre cluster déclaré
dans la console, est refusée (une même VM est reconnue par son vCenter et
son identifiant `vm-NN`) ; un cluster injoignable est nommé dans la
réponse, sans bloquer la composition.
Les disques doivent aussi tenir (1.87.0) : pour chaque classe de stockage
cible, la console additionne les disques de la vague et les compare à la
place que l'ordonnanceur de Longhorn accorde à un volume neuf : la marge de
sur-provisionnement, tant que chaque disque reste au-dessus de la place libre
minimale (un volume neuf ne contient encore rien, sa taille n'est pas prise
sur la place libre), une réplique par nœud, un volume dégradé quand il y a
moins de nœuds que de répliques. Une vague qui ne tient pas
est refusée avec les chiffres, à la composition puis au lancement de sa
première migration. Pendant la copie, un disque encore à 0 % parce que
Kubernetes ou Longhorn ne fournit pas son volume affiche **Copie en attente de
stockage**, avec la raison lue dans les événements du volume et du pod
d'import, au lieu d'un « copie des disques » sans fin.

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

**Migrations (tous clusters)**, entrée de flotte **Migrations** du menu
latéral depuis la 2.0.0 (`#migrations` ; troisième entrée de la section VM
Import / Export depuis la 1.77.0, à côté d'Activity avant) : toutes
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

**Cluster de gestion (2.0.0).** Les pages Cluster API et Terraform disent
dans leur en-tête depuis quel cluster elles travaillent (**Cluster de
gestion**) : le dernier hôte Harvester visité, sauf si vous en choisissez un
autre dans le menu à côté. Ce choix est gardé par le navigateur et ne change
pas l'hôte ouvert.

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
  Rôle admin ; les adresses internes sont refusées à chaque redirection
  sauf autorisation par `iso_fetch.allow_private` (voir le guide bare-metal).
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

- **Les comptes du cluster** (Paramètres > Sécurité > Comptes du cluster de
  l'hôte, aussi depuis le menu du compte) : les comptes
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
  `users.management.cattle.io` (`user-2fhwx`), pas le login. Paramètres >
  Sécurité > Comptes du cluster de l'hôte les liste.
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

## 10. Hyperviseurs KVM (console + CLI)

### Hôtes, VMs, stockage et réseaux (1.96.0)

La console gère aussi des hôtes KVM (libvirt), à côté des clusters
Harvester. Un hôte KVM reste ce qu'il est : il ne devient pas un cluster,
et libvirt reste la seule source de vérité. Aucun agent n'est installé sur
l'hôte. La console lance `virsh` sur l'hôte par SSH et lit ce qu'il rend.
**KVM** est un groupe de quatre entrées dans le menu latéral, après Shared
resources. Depuis la 2.0.0 le groupe n'existe plus : ces quatre vues sont
les onglets de l'index de la famille KVM (`#hosts/kvm`) et la liste des VMs
KVM (`#vms/host/kvm`), voir « Vues de tous les hôtes KVM » plus bas.

- **Hôtes** : un bloc par hôte. Nom de l'hôte, `utilisateur@adresse`, CPU
  (nombre, modèle, fréquence, sockets), mémoire (totale, libre,
  disponible), versions de libvirt et de QEMU, VMs par état, étiquettes. Un
  filtre par étiquette. **Tester** relance le test de connexion, une ligne
  par point. **Retirer** oublie l'hôte après une confirmation dans la
  page ; rien ne change sur l'hôte.
- **Machines virtuelles** : toutes les VMs de tous les hôtes dans un
  tableau. Nom, hôte, état, vCPU, mémoire, et un badge transitoire ou
  démarrage automatique. Filtres par hôte, état et texte ; chaque colonne
  se trie. **Déplier une ligne** donne son détail : vCPU, mémoire,
  persistante ou transitoire, démarrage automatique, épinglage CPU, machine
  et micrologiciel, disques, cartes, affichages.
- **Stockage** : un bloc par hôte avec ses pools libvirt (état, démarrage
  automatique, capacité, allocation, disponible) et, dépliés, leurs
  volumes. Un pool inactif le dit.
- **Réseaux** : un bloc par hôte avec ses réseaux libvirt (état, mode de
  transfert, pont, adresses avec la plage DHCP, démarrage automatique,
  transitoire).
- Un hôte injoignable, ou qui refuse la clé ou le compte, est montré avec
  la raison. Les autres hôtes sont lus quand même, en parallèle. La lecture
  de l'ensemble a une échéance, 25 s par défaut
  (`HARVESTER_OPS_KVM_READ_DEADLINE`) : un hôte qui n'a pas répondu à temps
  est montré en `timeout`, et les autres sont montrés.
- **Actualiser** relit les hôtes au plus une fois toutes les 5 s pour une
  même vue : un second clic dans les 5 s, ou dix personnes sur la même vue,
  reçoivent la lecture qui vient d'être faite. Stockage et Réseaux lisent
  chaque hôte une seule fois.

**Déclarer un hôte.** La fenêtre **Déclarer un hôte** prend l'adresse et
le port, puis relève les empreintes de la clé de l'hôte. Vous choisissez
celle à épingler : rien n'est présélectionné, et changer l'adresse vide la
liste. Puis le nom, l'utilisateur, le fichier de clé privée (sans phrase de
passe) et les étiquettes (`clé=valeur`). La clé est contrôlée avant toute
connexion. Le test de connexion s'affiche point par point : SSH, `virsh`,
droits sur `qemu:///system`, `qemu-img`, et affichages VNC des VMs en
marche joignables depuis le réseau sans mot de passe. Un affichage qui
écoute sur `127.0.0.1` n'est pas exposé. Une déclaration en échec ne laisse
rien derrière elle.

**L'empreinte de la clé d'hôte est épinglée.** La console se connecte avec
`StrictHostKeyChecking=yes` et la seule ligne épinglée. Un hôte dont la clé
a changé est refusé, jamais accepté en silence. Une connexion SSH par hôte
est réutilisée un court moment, pour qu'une lecture ne paie pas une
poignée de main par commande. Une lecture coupée par la connexion elle-même
(connexion partagée fermée, ssh en code 255) est retentée une fois, une
demi-seconde plus tard, sur une connexion neuve. Une écriture n'est jamais
retentée : on ne sait pas si elle a eu lieu, et l'action dit que la
connexion a été perdue.

**Lectures en parallèle (1.101.2).** Une lecture de nombreux objets (la
définition de chaque VM, chaque volume de chaque pool) lance 8 commandes
`virsh` à la fois sur l'hôte, chacune avec sa sortie, son code et son délai.
Les écritures restent l'une après l'autre, dans l'ordre. Sur un hôte de
15 VMs et 63 volumes, le relevé du stockage est passé de 24,9 s à 5 s
environ. Le catalogue d'images ne retient plus les fenêtres Créer et
Modifier : la fenêtre s'ouvre tout de suite, et l'onglet Disques affiche
« Chargement des images de l'hôte... » jusqu'à son arrivée, ou dit pourquoi
il n'a pas pu être lu. Regarder la section KVM ou un hôte prépare le relevé
en arrière-plan : le catalogue est en général prêt à l'ouverture d'une
fenêtre. Rien ne part quand personne ne regarde.

**Gestes de cycle de vie.** Le menu d'une VM propose **Démarrer**,
**Arrêter**, **Redémarrer**, **Arrêt forcé**, **Suspendre** et
**Reprendre**. Un geste que l'état n'autorise pas est grisé et dit
pourquoi. Chaque geste est une action suivie (dock, Activité) qui suit
l'état réel du domaine jusqu'à l'état attendu :

- **Arrêter** demande à l'invité de s'arrêter (ACPI) et attend `shut off`,
  120 s par défaut. Un invité qui ne s'arrête pas à temps fait échouer
  l'action, et la console propose l'**Arrêt forcé**.
- **Arrêt forcé** se confirme dans la page. Il arrête le domaine tout de
  suite, comme on débranche la prise.
- **Redémarrer** attend que le domaine tourne de nouveau. libvirt ne peut
  pas prouver que l'invité a redémarré : la console dit « redémarrage
  demandé, la VM tourne », et le résultat porte `confirmed: false`.
- Un domaine **transitoire** (créé sans définition) disparaît quand il
  s'arrête. Arrêter ou forcer l'arrêt le signale alors disparu, comme une
  réussite.
- **Reprendre** ne vaut que pour une VM en pause, ou suspendue par son
  invité.

**Annuler.** **Annuler** dans le dock arrête le script. Une déclaration
d'hôte annulée en cours est défaite : ni déclaration, ni clé, ni ligne
épinglée ne restent, et l'action finit `cancelled` avec « nothing saved ».
Un geste annulé a peut-être déjà atteint libvirt : l'action le dit et ne
prétend pas avoir rien défait. Annuler une action demande le rôle qu'il a
fallu pour la lancer : un opérateur n'annule pas la déclaration d'hôte
lancée par un administrateur.

**Droits.** Un lecteur lit. Un opérateur lance les gestes, arrêt forcé
compris, et teste un hôte. Un administrateur relève les clés d'hôte,
déclare et retire les hôtes. Le nom d'hôte `scan` est réservé par la
console.

**Stockage des déclarations.** Un fichier par hôte, `kvm.d/<nom>.yaml`
dans le répertoire d'état, écrit en JSON (du YAML valide), droits 0600. La
clé privée est copiée dans `ssh/<nom>_id` et la ligne épinglée dans
`ssh/<nom>_known`. Les chemins sont relatifs au répertoire d'état. Retirer
un hôte supprime ces trois fichiers et eux seuls.

**Prérequis sur un hôte** : libvirt (démons modulaires ou `libvirtd`), un
compte membre du groupe `libvirt` (ou root) joignable en SSH par clé.
`qemu-img` est facultatif ; le test le signale. OpenSSH 9.8 et suivants
pénalisent les connexions qui ne s'authentifient pas (`PerSourcePenalties`),
et le relevé de la clé d'hôte en est une. Juste après un relevé, l'hôte
peut couper les nouvelles connexions une quinzaine de secondes. La
déclaration d'un hôte refait une fois son test SSH après 20 s.

**Pas dans la 1.96.0** : console graphique (VNC), instantanés et clonage,
ajoutés en 1.98.0, et création et modification de VMs, d'images, de
pools, de volumes et de réseaux, ajoutées en 1.100.0 (plus bas). Migration
à chaud entre hôtes, maintenance d'un hôte et supervision restent à
venir.

API : `GET /api/kvm/hosts`, `GET /api/kvm/hosts/<host>` (VMs, pools,
volumes, réseaux), `GET /api/kvm/vms`, `GET /api/kvm/vms/<host>/<domain>`,
`POST /api/kvm/hosts/scan` (`{address, port}`), `POST /api/kvm/hosts`
(multipart, 256 Kio au plus : `payload` avec nom, adresse, port, utilisateur, empreinte et
étiquettes, et `key`), `DELETE /api/kvm/hosts/<host>`,
`POST /api/kvm/hosts/<host>/test`,
`POST /api/kvm/vms/<host>/<domain>/<gesture>` (`{timeout}` facultatif, de
1 à 3600 s). Les écritures répondent `202 {action_id}` tout de suite.
`GET /api/kvm/hosts?declared=1` liste les déclarations sans interroger les
hôtes ; `?fresh=1` sur une lecture est suivi une fois toutes les 5 s par
lecture.

Ligne de commande : `bin/harvester-kvm.py host scan --address A [--port P]`,
`host add --name N --address A --user U --ssh-key FICHIER --fingerprint
SHA256:... [--port P] [--label k=v]`, `host test N`, `host list`,
`host remove N`, `inventory [--host N] [--details]`,
`vm list [--host N]`, `vm show --host N --name D`,
`vm start|shutdown|reboot|destroy|suspend|resume --host N --name D
[--timeout S]`. `--json` sur chaque commande, `--state-dir` (par défaut
`HARVESTER_OPS_STATE_DIR`). La console lance ce script pour chaque
écriture. Codes de sortie : 0 fait, 1 échec, 2 refusé avant l'hôte,
3 annulé (SIGTERM ou SIGHUP ; une déclaration d'hôte est défaite, un geste a pu déjà atteindre libvirt).

**Ce qui est vérifié.** Vérifié en réel sur un hôte de banc (openSUSE,
libvirt 12.7.0, QEMU 10.2.2) : déclaré depuis la fenêtre, ses 15 domaines
existants, 9 pools et 3 réseaux lus et laissés intacts. Sur un domaine
d'essai créé pour l'occasion (cirros) : démarrer, suspendre, reprendre,
redémarrer, arrêter, arrêt forcé et redémarrage par la console, chacun
suivi jusqu'à l'état vu par `virsh domstate`. L'invité a ignoré la demande
ACPI et l'arrêt a proposé l'arrêt forcé. Un domaine transitoire arrêté de
force a été signalé disparu, comme une réussite. Domaine d'essai en marche,
son affichage VNC sur `127.0.0.1` a été signalé non exposé. Une seconde
déclaration du même hôte a été ajoutée, testée puis retirée, ses fichiers
supprimés. Un invité SLES 16 qui suit l'ACPI est arrivé à `shut off` 2,4 s
après la demande (action finie en 3,5 s). Une déclaration annulée depuis la
console pendant son test de connexion, déjà enregistrée, a été défaite :
aucun fichier de déclaration ni de clé n'est resté.

### La page d'un hôte KVM (2.0.0)

Chaque hôte KVM a sa page, ouverte depuis le menu des hôtes ou par son
adresse `#kvm/<hôte>`. Un bandeau montre la famille (KVM), le nom, l'état,
la version de libvirt et le nombre de VMs, le sous-menu de l'hôte et
**Activité de cet hôte**.

- **Vue d'ensemble** (`#kvm/<hôte>`) : le bloc de l'hôte (CPU, mémoire,
  versions, VMs par état, étiquettes), avec **Tester**, **Créer une VM** et
  **Retirer**.
- **Machines virtuelles** (`.../vms`) : le tableau des VMs de cet hôte
  seulement. Le filtre d'hôte est imposé et masqué ; l'état, le texte, le
  tri et tous les gestes sont ceux du tableau de la flotte.
- **Paramètres** en cinq onglets. **Système** : nom de la machine, URI
  libvirt, versions de libvirt et de QEMU, CPU, mémoire, nombre de
  domaines, en une lecture de l'hôte. **Pools** et **Réseaux** : les vues
  de stockage et de réseaux avec cet hôte seulement. **Étiquettes** : les
  étiquettes de la déclaration. **Connexion** : adresse, port et
  utilisateur SSH, URI et empreinte de la clé d'hôte épinglée (jamais le
  chemin de la clé) ; **Tester** (opérateur) et **Retirer** (administrateur,
  confirmé dans la page, puis l'index des hôtes KVM s'ouvre).
- **Disaster Recovery** (`.../dr`) : dit que la réplication des domaines
  KVM n'est pas encore disponible.

Un hôte qui ne répond pas garde un bloc en erreur, avec la raison classée.
Étiquettes et Connexion ne lisent que la déclaration : elles restent
lisibles. Un hôte KVM et un cluster Harvester peuvent porter le même nom :
la famille fait partie de l'adresse, ils ne se mélangent jamais.

**Vues de tous les hôtes KVM.** Le groupe KVM de la 1.x montrait tous les
hôtes sur un écran. Rien n'en est perdu : l'index de la famille KVM
(`#hosts/kvm`, la famille KVM du menu latéral) a quatre onglets.

- **Hôtes** (`#hosts/kvm`) : le tableau des hôtes, avec **Déclarer un
  hôte**, et **Tester** et **Retirer** par ligne.
- **Capacité** (`#hosts/kvm/capacity`) : le bloc de chaque hôte côte à
  côte (CPU, mémoire, versions, VMs par état, étiquettes), avec le filtre
  par étiquette.
- **Pools** (`#hosts/kvm/pools`) : les pools de tous les hôtes, un bloc
  par hôte, avec le filtre d'hôte et le sous-onglet **Images** (catalogue
  de tous les hôtes).
- **Réseaux** (`#hosts/kvm/networks`) : les réseaux libvirt de tous les
  hôtes, un bloc par hôte.

Le nom de chaque hôte dans ces vues ouvre sa page, sur l'onglet
correspondant. Chaque geste reste disponible aux deux endroits. Les
anciennes adresses `#kvmstorage` et `#kvmnetworks` ouvrent les onglets
Pools et Réseaux, et `#kvmhosts` le tableau des hôtes KVM.

**Modifier les étiquettes** (administrateur). **Modifier les étiquettes**
dans Paramètres > Étiquettes ouvre une fenêtre, une ligne par étiquette.
Des contrôles passent avant **Appliquer** : noms valides, aucun nom donné
deux fois, valeurs de 253 caractères au plus sans caractère de contrôle,
32 étiquettes au plus, et la liste de ce qui est ajouté, modifié et retiré.
Appliquer reste grisé avec la raison tant qu'un contrôle échoue ou que rien
ne change. Le jeu entier est remplacé dans une action suivie (dock,
Activité), qui garde les étiquettes d'avant. Seule la déclaration de la
console change : rien n'est demandé à l'hôte. La même chose en ligne de
commande : `harvester-kvm.py host label --host <hôte> --label clé=valeur
...` ou `--clear`. Les autres boutons suivent le rôle : un lecteur voit
Tester grisé, un opérateur voit Retirer et Modifier les étiquettes grisés,
chacun avec la raison.

### Console, instantanés, clonage et suppression d'une VM (1.98.0)

Le menu d'une VM KVM gagne un second groupe : **Console**, **Instantanés**,
**Cloner** et **Supprimer**. Chaque entrée est grisée, avec la raison,
quand le rôle, l'état ou la définition de la VM ne le permet pas. Les
raisons viennent du serveur, pas de la page.

**Console graphique (VNC).** **Console** ouvre la même fenêtre que pour
une VM Harvester, avec noVNC. La console n'ouvre pas l'affichage au
réseau. Elle l'atteint par la connexion SSH de l'hôte (`ssh -W`, sur la
clé d'hôte épinglée) : un affichage qui écoute sur `127.0.0.1` fonctionne.
Plusieurs personnes qui regardent la même VM partagent une seule connexion
à l'affichage ; la fenêtre dit qui regarde. Le tunnel se ferme avec la
dernière fenêtre. L'adresse et le port sont relus à chaque connexion : une
VM redémarrée sur un nouveau port est retrouvée. Une VM arrêtée laisse la
fenêtre en attente jusqu'à son démarrage.
- **Mot de passe.** Quand l'affichage a un mot de passe VNC, la console le
  lit dans la définition et répond elle-même au défi. Le mot de passe ne
  sort jamais de cette connexion : aucune réponse d'API, aucun journal,
  aucune étape d'action ni aucun navigateur ne le reçoit. Le ticket dit
  seulement qu'un mot de passe existe. Un mot de passe refusé est dit, et
  la fenêtre cesse de réessayer.
- **Refus dits, sans nouvel essai** : affichage SPICE seul (non pris en
  charge dans cette version), affichage sur une socket UNIX (propriété de
  root et de qemu, le compte SSH ne peut pas l'atteindre), aucun affichage,
  affichage sans écoute, et affichage qui écoute sur une adresse qui n'est
  pas l'une de celles de l'hôte. Un affichage qui ne propose que des types
  de sécurité que la console ne parle pas (VeNCrypt, SASL, TLS) est nommé,
  sans nouvel essai.
- La fenêtre a **Démarrer**, **Arrêter**, **Arrêt forcé** (confirmé dans la
  page) et **Instantanés**. Rouvrir une console déjà ouverte ramène sa
  fenêtre au lieu d'ouvrir une seconde connexion. Cela vaut aussi pour les
  consoles Harvester.
- 8 spectateurs de console au plus en même temps dans toute la console,
  consoles Harvester comprises (429 au-delà). Retirer un hôte ferme ses
  consoles.

**Instantanés.** La fenêtre **Instantanés** liste les instantanés internes
de la VM : nom, instantané courant, date, contenu (disques seuls, ou
disques et mémoire), emplacement et parent. **Créer** propose
`<vm>-AAAAMMJJ-HHMM` et accepte une description facultative. Sur une VM
en marche, l'instantané garde aussi la mémoire, et l'invité est mis en
pause pendant l'enregistrement : la fenêtre le dit, avec la taille de la
mémoire. **Revenir** demande une confirmation qui dit que l'état non
enregistré est perdu. Revenir, sur une VM en marche, à un instantané pris
VM arrêtée éteint la VM : la confirmation relit l'état et le dit.
**Supprimer** nomme les instantanés enfants, rattachés au parent de
l'instantané supprimé.
- Instantanés internes seulement : chaque disque inscriptible doit être un
  fichier ou un volume qcow2, et les variables UEFI (nvram) aussi. Un
  disque raw, bloc ou réseau, un disque transitoire ou un périphérique PCI
  ou USB attaché désactive **Créer** et dit quel disque. Une VM en pause ou
  en cours d'arrêt, une VM transitoire et un nom déjà pris sont refusés.
- Les instantanés externes sont affichés « non gérés par la console »,
  sans bouton. La console ne revient jamais avec `--force` : quand libvirt
  le demande, la console le dit et s'arrête.

**Cloner.** Une copie complète d'une VM arrêtée et persistante, sur le
même hôte. La fenêtre montre la copie avant toute création : chaque disque
avec sa destination et la taille à copier, la place libre, les disques
gardés partagés, et ce qui est régénéré. **Cloner** reste désactivé tant
que l'aperçu ne correspond pas au nom saisi ou contient un refus.
- Chaque disque est copié **à côté de sa source** : dans le même pool de
  stockage, comme nouveau volume, ou dans le même répertoire. La
  destination est `<nouveau nom>-<cible>` avec l'extension de la source.
  Elle ne se choisit pas dans cette version. La copie est aplatie : elle ne
  dépend pas de l'image de base de la source.
- La copie reçoit un nouvel uuid, de nouvelles adresses MAC, un nouveau
  fichier de variables UEFI (créé par libvirt au premier démarrage), un
  nouvel identifiant de génération, et perd numéros de série de disque,
  WWN et numéros de série SMBIOS. Un port VNC fixe devient automatique.
  Les entrées de démarrage UEFI et l'état du vTPM ne sont pas copiés :
  l'aperçu le signale.
- Les disques en lecture seule et les CD-ROM restent partagés. Un disque
  bloc ou réseau, un disque partageable, un disque utilisé par une autre
  VM, un périphérique PCI ou USB, une étiquette de sécurité statique et un
  vTPM à état externe sont refusés.
- Une copie hors pool tourne en arrière-plan sur l'hôte et montre sa
  progression. **Annuler** dans le dock arrête la copie et retire tout ce
  qui a été créé. Une copie qui échoue est défaite de la même façon, et ce
  qui n'a pas pu être retiré est nommé.
- Une copie dans un pool (`vol-create-from`) continue dans libvirt après
  une annulation, et libvirt refuse de la supprimer tant qu'elle dure.
  Depuis la 1.100.0, l'action reste en cours, les deux noms tenus, jusqu'à
  la fin de la copie (jusqu'à 15 min) ; la copie est alors supprimée.
  Avant, l'action se fermait aussitôt et la copie restait.

**Supprimer.** **Supprimer** retire une VM arrêtée et persistante. Il
faut le rôle administrateur. La fenêtre montre d'abord un aperçu, puis
demande le nom exact de la VM.
- Sans **Supprimer aussi les disques que possède cette VM**, seule la définition
  part, avec les métadonnées de ses instantanés, son état sauvegardé et
  son fichier de variables UEFI. Les disques restent.
- Avec, un disque n'est supprimé que si la console prouve qu'aucune autre
  VM ne s'en sert. Elle lit tous les pools et volumes de l'hôte, les
  définitions et les chaînes de disques de toutes les autres VMs, et
  compare les fichiers par périphérique et inode : un lien symbolique ne
  cache pas un disque partagé. Un disque est gardé, avec sa raison, quand
  il est partagé avec une autre VM, image de base du disque d'une autre VM,
  en lecture seule, un périphérique bloc, sur un stockage partagé (NFS,
  Ceph, Gluster et semblables : un autre hôte pourrait s'en servir), ou
  absent.
- **Fichiers illisibles.** Quand le compte SSH ne peut pas lire un fichier
  d'une autre VM (permission refusée), la console ne peut pas prouver que
  ce fichier ne dépend pas d'un disque de cette VM. La suppression est
  refusée tant que vous n'avez pas coché chacun de ces fichiers dans la
  fenêtre. Seul un refus de droits peut être reconnu ainsi : une lecture
  libvirt en échec ou une erreur d'entrée-sortie refuse toujours.
- **L'aperçu est ce qui est supprimé.** La fenêtre envoie une empreinte de
  la suppression montrée. Si l'hôte a changé depuis (une autre VM utilise
  maintenant un disque, la VM a été redéfinie), la suppression s'arrête
  avec « l'hôte a changé depuis l'aperçu » et montre le nouvel aperçu. Un
  disque devenu partagé entre-temps est gardé et signalé comme écarté.

**Une opération à la fois par VM.** Un geste, un instantané, un clonage
ou une suppression sur une VM est refusé tant qu'une autre opération y
tourne (409 dans la console, sortie 2 `busy` du script). Ce verrou couvre
désormais aussi les gestes de cycle de vie de la 1.96. Un clonage tient
aussi le nom de la copie.

**Les lectures coupées par la connexion sont refaites une fois.** Sur un
hôte de banc en OpenSSH 10.2p1, le serveur SSH s'interrompt parfois quand
plusieurs sessions tournent en même temps sur une connexion partagée (son
journal dit `mm_request_receive: read: bad msg_len` et
`rtype N != type 115`). La connexion partagée tombe, et chaque commande en
cours finit avec le statut ssh 255. La console rejoue une lecture une fois
sur une connexion neuve. Elle ne rejoue jamais une écriture : un
instantané, un clonage ou une suppression coupé ainsi dit « connexion
perdue », et vous vérifiez le résultat avant de recommencer. Les aperçus
d'un clonage ou d'une suppression sont limités à 20 par minute et à 2 en
même temps par hôte.

**Droits.** Un lecteur lit les instantanés et les aperçus. Un opérateur
ouvre la console, crée, restaure et supprime des instantanés, et clone.
Un administrateur supprime une VM, et seul un administrateur peut annuler
une suppression.

API : `POST /api/kvm/vms/<host>/<domain>/console-ticket` (`{ticket,
ws_path, expires_in, password}` ; 409 quand la VM ne tourne pas, 422 avec
`reason` pour un affichage que la console ne peut pas ouvrir),
`GET /api/kvm/vms/<host>/<domain>/console-status`, websocket
`/ws/kvm/vnc/<host>/<domain>?ticket=...` ;
`GET /api/kvm/vms/<host>/<domain>/snapshots`,
`POST /api/kvm/vms/<host>/<domain>/snapshots` (`{name, description}`),
`POST /api/kvm/vms/<host>/<domain>/snapshots/<snapshot>/revert`,
`DELETE /api/kvm/vms/<host>/<domain>/snapshots/<snapshot>` ;
`GET /api/kvm/vms/<host>/<domain>/clone-plan?name=C`,
`POST /api/kvm/vms/<host>/<domain>/clone` (`{name}`) ;
`GET /api/kvm/vms/<host>/<domain>/delete-plan?remove_storage=1&acknowledge=CHEMIN`,
`DELETE /api/kvm/vms/<host>/<domain>` (`{confirm, expect_delete,
remove_storage, acknowledge_unreadable}`, `expect_delete` est le `digest`
de l'aperçu). Les écritures répondent `202 {action_id}`. `/api/kvm/vms`
donne à chaque VM une table `operations` (rôle, états, persistance) pour
le menu.

Ligne de commande : `vm display --host N --name D` (adresse, port,
présence d'un mot de passe, et la commande `ssh -L` pour un visualiseur
local), `snapshot list|create|revert|delete --host N --name D
[--snapshot=S] [--description=T] [--timeout S]`, `clone --host N --name D
--new-name=C [--dry-run] [--timeout S]`, `vm delete --host N --name D
[--remove-storage] [--acknowledge-unreadable=CHEMIN ...]
[--expect-delete=EMPREINTE] [--dry-run] [--timeout S] [--survey-timeout S]`.
Donner les valeurs en `--opt=valeur` quand elles peuvent commencer par un
tiret. Codes de sortie comme plus haut, plus `busy` (sortie 2) quand une
autre opération tient la VM et `plan-changed` (sortie 1) quand l'hôte ne
correspond plus à l'aperçu.

**Ce qui est vérifié.** Sur l'hôte de banc (libvirt 12.7.0, OpenSSH
10.2p1), sur des domaines d'essai créés pour l'occasion (cirros,
`qemu-img`), chaque étape contrôlée par `virsh` : console ouverte par le
tunnel, partagée par deux navigateurs sur un seul tunnel, fermée avec la
dernière fenêtre ; vrai mot de passe VNC de QEMU résolu par la console, le
mot de passe absent de toute réponse d'API, du journal de la console et de
l'Activité ; instantanés pris VM arrêtée et en marche, retours dans les
deux sens (une VM en marche ramenée à un instantané pris arrêtée est
éteinte), parent supprimé et enfant rattaché au grand-parent ; disque raw
refusé avec sa raison ; instantané d'une VM UEFI ; clonage dans un pool et
hors pool, avec nouvel uuid, sans chaîne de base, nouveaux fichier de
variables UEFI et identifiant de génération, démarré et ouvert dans la
console ; une copie de 7,4 Gio annulée n'a rien laissé ; suppression
refusée à cause d'un fichier illisible d'une autre VM, puis faite après
l'avoir reconnu ; un disque partagé par lien symbolique et l'image de base
d'une autre VM gardés ; un hôte modifié refusé par `plan-changed`. Sous
8 lecteurs en parallèle pendant 60 s, la chute du serveur SSH décrite plus
haut est arrivée 2 à 4 fois par minute, sans aucune lecture en échec. Non
vérifié : un hôte qui exige `--force` pour revenir à un instantané.


### Créer et modifier des VMs, images, pools et réseaux (1.100.0)

**Rien n'est installé sur l'hôte.** La console fabrique elle-même, en
Python, le XML du domaine, les définitions de volume, de pool et de réseau
et l'ISO cloud-init, et les passe à `virsh` par l'entrée standard
(`define --validate`, `attach-device`, `vol-create`, `net-define`,
`pool-define`). Ni `virt-install`, ni `genisoimage`, ni `cloud-localds`
sur l'hôte. Prérequis inchangés : libvirt, `qemu-img` et un compte du
groupe `libvirt`.

**Créer une VM.** **Créer une VM** dans la barre des VMs KVM, sur la carte
d'un hôte ou sur une image du catalogue ouvre la fenêtre des VMs
Harvester : mêmes onglets (Général, Calcul, Micrologiciel, Disques,
Réseau, Cloud-init, Revue), mêmes icônes, même assistant cloud-init.
- En-tête : hôte, nom, nombre d'instances (1 à 20, nommées `<nom>-01`,
  `-02`...), démarrer après création, agent invité, clés SSH (les
  KeyPairs des clusters que vous pouvez lire, regroupées par cluster, plus
  des clés collées) et modèle.
- Les choix viennent de l'hôte (`virsh capabilities` et
  `domcapabilities`, par famille de machine) : q35 ou pc, BIOS ou UEFI,
  Secure Boot, TPM (émulé par `swtpm`), mode CPU (host-model,
  host-passthrough, custom avec les modèles utilisables), bus de disque,
  modèles vidéo. Un choix que l'hôte ne peut pas donner est grisé avec sa
  raison (« the host has no TPM emulator (swtpm) », « Secure Boot needs
  the q35 machine type »).
- vCPU et mémoire peuvent rester sous leur maximum pour des changements à
  chaud plus tard (un socket par vCPU, comme Harvester). Emplacements PCIe
  libres pour l'ajout à chaud sur q35 : 4 par défaut, de 0 à 16.
- Disques : vierge, depuis une image (lié par défaut : un qcow2 posé sur
  l'image du même pool ; ou copie complète, agrandie à la taille
  demandée), volume existant, ISO, ou lecteur de CD-ROM vide. La place
  libre du pool s'affiche sous les cartes. Cartes : réseau libvirt, pont
  de l'hôte, ou macvtap sur une carte de l'hôte avec son mode. La console
  tire la MAC de chaque carte laissée vide (`52:54:00:...`, contrôlée
  contre toutes les cartes de l'hôte) et l'écrit dans la définition. Une
  carte ajoutée plus tard reçoit la sienne de la même façon.
- Affichage VNC, avec un mot de passe tiré par la console (8 caractères,
  la limite de VNC). Le mot de passe n'est jamais affiché.
- Cloud-init : user-data et network-config écrits à la main ou par
  l'assistant, nom d'hôte invité, pool du seed. L'agent invité et les clés
  SSH sont ajoutés à un `#cloud-config` (clés de premier niveau
  `ssh_authorized_keys`, `packages` et `runcmd`) ; ce que la console ne
  peut pas fusionner sans risque (listes en accolades sur plusieurs
  lignes, ancres, plusieurs documents) est refusé avec sa raison et laissé
  tel quel. Le seed est une ISO NoCloud (étiquette `cidata`, Joliet)
  fabriquée par la console ; son identifiant d'instance est l'uuid de la
  VM.
- L'assistant écrit un network-config version 2 qui retrouve chaque carte
  par sa MAC (`match: macaddress`). Chaque entrée désigne une carte de
  l'onglet Réseau (1 = la première). La carte n'est renommée dans
  l'invité (`set-name`) que si vous donnez un nom. Le nom que l'image
  donne à la carte (`eth0`, `enp1s0`) ne compte donc plus. **Générer le
  network-data** tire la MAC des cartes qui n'en ont pas et l'écrit dans
  leur carte. C'est refusé pour plusieurs instances, qui ne peuvent pas
  partager une MAC : créez-les en DHCP, puis réglez chaque VM par
  Cloud-init. Dans la fenêtre Modifier, les entrées désignent les cartes
  existantes.
- **Revue** demande le plan au serveur : refus, avertissements, volumes à
  créer, seed, et le XML avec `passwd='***'`. **Créer** lance ensuite les
  étapes : volumes, seed, `define --validate`, autostart, démarrage,
  contrôle. Le plan est recalculé à ce moment (l'aperçu n'est jamais
  cru), et les pools sont relus avant chaque instance. Un échec retire ce
  que la console a créé pour cette instance et nomme ce qui n'a pas pu
  l'être ; les instances déjà créées restent et sont listées.
- Annuler une création ou un clonage attend l'outil : une copie complète
  que libvirt construit encore est attendue (jusqu'à 15 min), puis
  supprimée.
- Les modèles gardent le formulaire (pas le XML) dans la console, communs
  à tous les hôtes, avec un propriétaire. Mots de passe, clés et user-data
  sont retirés à l'enregistrement, et la fenêtre le dit.

**Modifier une VM.** **Modifier** dans le menu d'une VM KVM ouvre la même
fenêtre, remplie depuis la définition inactive. Chaque champ dit avant
l'envoi ce qui se passera sur cette VM dans son état actuel : appliqué
maintenant, au prochain démarrage, ou refusé. **Revue** montre la
classification exacte du serveur. Seuls les champs changés partent.

| Changement sur une VM en marche | Quand | Commande |
|---|---|---|
| Nombre de vCPU jusqu'au maximum | maintenant (une baisse que QEMU refuse attend le prochain démarrage, et c'est dit) | `setvcpus --live --config` |
| Mémoire courante jusqu'au maximum | maintenant, par le ballon de l'invité ; `dommemstat` est relu | `setmem --live --config` |
| Maximum de vCPU et de mémoire, topologie, mode et modèle CPU, type de machine, micrologiciel, Secure Boot, TPM, vidéo, VNC et son mot de passe, préréglage d'OS, emplacements d'ajout à chaud, agent invité, tablette, RNG, actions d'alimentation, bus, cache, numéro de série, ordre de démarrage d'un disque, modèle, genre et source d'une carte | prochain démarrage | `define` ciblé |
| Ajouter un disque virtio, ou scsi avec un contrôleur | maintenant si un emplacement PCIe est libre sur q35, sinon au prochain démarrage avec la raison | `attach-device --live --config` |
| Ajouter un disque SATA ou un lecteur de CD-ROM | prochain démarrage (pas d'enfichage à chaud SATA sur QEMU) | `attach-device --config` |
| Retirer un disque virtio ou scsi | maintenant ; si l'invité ne le libère pas en 30 s, il quitte la définition et disparaît au prochain démarrage. Son volume est gardé | `detach-device --live --config` |
| Retirer un disque ou une carte ajouté pour le prochain démarrage seulement | maintenant, dans la définition seule | `define` ciblé |
| Changer ou éjecter le média d'un CD-ROM | maintenant | `update-device`, `change-media --eject` |
| Ajouter une carte | maintenant si un emplacement PCIe est libre (q35), sinon au prochain démarrage | `attach-device --live --config` |
| Retirer une carte (par sa MAC) | maintenant | `detach-interface --live --config` |
| Titre, description, démarrage automatique | maintenant | `desc`, `autostart` |
| Cloud-init | le nouveau seed est lu au prochain démarrage | nouveau volume de seed + `update-device` |
| Renommer | VM arrêtée et sans instantané seulement | `domrename` |

- Sur une VM arrêtée, tout passe par un seul `define` ciblé.
- La mémoire courante n'apparaît jamais en attente du prochain démarrage.
  QEMU démarre au maximum, et un invité sans pilote de ballon y reste :
  la valeur vive peut donc différer de la définition pour toujours.
- « Prochain démarrage » veut dire un arrêt puis un démarrage : un
  redémarrage dans l'invité ne recrée pas le processus QEMU. La fenêtre
  et la liste des VMs affichent **restart needed** avec les champs en
  attente, et **Arrêter puis démarrer** le fait.
- La modification porte l'empreinte de toute la définition lue à
  l'ouverture de la fenêtre. Si la VM a été changée ailleurs entre-temps,
  la modification est refusée (`changed`) ; **Relire** rafraîchit les
  champs non touchés et garde ce que vous avez saisi.
- Ce que le formulaire ne gère pas reste octet pour octet : épinglage CPU,
  `<cputune>`, ordonnancement temps réel, `<numatune>`, `<hostdev>`,
  `<qemu:commandline>`, métadonnées d'autres outils, `<seclabel>`,
  contrôleurs ajoutés à la main. La fenêtre les liste en lecture seule
  (« kept as is »).
- Ballon actif ou non, console série, chien de garde, numéro de série du
  micrologiciel et fils d'E/S des disques se règlent à la création
  seulement. Une VM transitoire s'ouvre en lecture seule.
- **Une seule entrée d'édition dans le menu (1.101.3).** Sous les entrées
  d'alimentation, de console, d'instantanés, de clonage et de suppression,
  le menu d'une VM KVM n'a plus qu'une entrée d'édition, **Modifier**. Les
  changements à chaud se font dans cette fenêtre : CPU et mémoire, ajouter
  ou retirer un disque, insérer ou éjecter le média d'un CD-ROM, ajouter ou
  retirer une carte, cloud-init (avec une nouvelle exécution). Chaque champ
  dit maintenant, prochain démarrage ou refusé avant l'envoi, et le
  résultat le dit par changement. Les routes `/hot/<op>` et
  `harvester-kvm vm <geste>` restent pour les scripts.
- **Cloud-init** d'une VM existante : le seed est relu (rôle opérateur),
  régénéré dans un nouveau volume et mis à la place ;
  **Rejouer cloud-init au prochain démarrage** lui donne un nouvel identifiant
  d'instance. Un user-data vide qui effacerait l'actuel exige
  **Vider le user-data**.

**Images.** **Stockage > Images** liste, par hôte, les volumes des pools
d'images déclarés, ou sans eux les volumes qu'aucune VM n'utilise comme
disque (une image sur laquelle reposent des disques liés reste listée).
Colonnes : nom, pool, format et badge ISO, taille virtuelle, usage
(« base de N disque(s) », « utilisée par N »), SHA-256, URL d'origine
(sans identifiants, paramètres ni fragment).
- **Téléverser** depuis le navigateur : le fichier est reçu une fois par
  la console (progression, débit, temps restant), son SHA-256 comparé à
  celui que vous donnez, son en-tête qcow2 lu, puis envoyé à un ou
  plusieurs hôtes sur une connexion SSH propre (`vol-upload` par l'entrée
  standard, plafond de débit en Mbit/s au choix) et contrôlé sur chaque
  hôte par `sha256sum`. **Annuler** marche dans les deux phases et ne
  laisse rien.
- **Télécharger depuis une URL** : par la console, avec la garde
  d'adresses du magasin d'ISO (chaque redirection recontrôlée, adresses
  internes refusées sauf si `iso_fetch.allow_private` les autorise,
  adresses de métadonnées cloud toujours refusées). Une URL `http` simple
  exige un SHA-256.
- Les envois et les téléchargements sont plafonnés par
  `iso_fetch.max_bytes` (20 Gio par défaut), contrôlé sur la taille
  annoncée et sur les octets reçus.
- Refusé avant toute écriture : un qcow2 qui nomme un fichier de base ou
  un fichier de données externe, ou qui annonce plus de 16 Tio.
- **Vérifier** recalcule le SHA-256 sur l'hôte. **Créer une VM depuis
  cette image** ouvre la fenêtre de création avec l'image en premier
  disque. **Supprimer** est refusé tant que l'image sert de base ou est
  utilisée. **Pools d'images** (administrateur) dit quels pools portent
  les images.

**Volumes.** Créer (vierge ou lié à une image du même pool), agrandir
(refusé tant qu'une VM en marche utilise le volume), supprimer
(administrateur). Suppression et agrandissement reprennent la preuve de la
1.98 qu'aucune autre VM n'utilise le volume, et la suppression porte
l'empreinte de son aperçu. Les volumes d'un pool NFS (`netfs`), images
téléversées là comprises, ne peuvent être ni supprimés ni agrandis par la
console : le stockage est partagé, cette preuve ne peut pas être faite.

**Pools** (administrateur, sauf rafraîchir). Créer un pool `dir` ou NFS.
Refusés : un répertoire système (`/etc`, `/usr`...), le répertoire d'un
autre pool (comparé par chemin et par périphérique et inode : un lien
compte), un pool d'un autre type imbriqué avec lui. Démarrer, arrêter
(refusé tant qu'une VM en marche utilise un de ses volumes), rafraîchir
(opérateur), autostart, supprimer. La suppression est refusée tant
qu'une définition de VM cite un volume du pool ; **Retirer aussi
le répertoire vide** ne vaut que pour un pool vide. La console ne crée jamais
de pool d'elle-même.

**Réseaux** (administrateur). Créer un réseau NAT, routé ou isolé (le
premier `192.168.X.0/24` libre est proposé, plage DHCP à partir de `.2`),
ou un réseau sur un pont existant de l'hôte. Un sous-réseau qui chevauche
une adresse ou une route de l'hôte, ou un autre réseau libvirt, est
refusé. L'arrêt est refusé tant qu'une VM en marche y a une carte, la
suppression tant qu'une définition le cite. **Ponts et cartes** montre les
ponts, cartes physiques, adresses et routes de l'hôte.

**Droits.** Lecteur : toutes les lectures. Opérateur : créer et modifier
une VM, gestes à chaud, créer, téléverser, vérifier ou agrandir un volume
ou une image, rafraîchir un pool, lire le cloud-init d'une VM, modèles.
Administrateur : supprimer un volume ou une image, tous les gestes de pool
et de réseau, pools d'images.

**Secrets.** Le mot de passe VNC et le user-data n'apparaissent dans
aucune réponse d'API (sauf la lecture du cloud-init par un opérateur),
aucun journal, aucune étape d'action, aucune ligne de commande ni dans
l'Activité. Les specs parviennent à l'outil par un fichier 0600 du
répertoire de travail de l'action, supprimé à la fin.

API : `GET /api/kvm/hosts/<hôte>/caps?machine=q35|pc`, `.../images`,
`.../links`, `.../pools`, `.../networks`, `.../macs?count=N` (1 à 16 MAC
libres tirées pour les cartes d'une VM à créer, jamais gardées en cache) ;
`GET /api/kvm/vms/<hôte>/<domaine>/form` (vue, réglages non gérés, champs
en attente, empreinte), `GET .../cloudinit` (opérateur) ;
`POST /api/kvm/hosts/<hôte>/vm-plan` (`{spec}`),
`POST /api/kvm/vms/<hôte>` (`{spec}`),
`POST /api/kvm/vms/<hôte>/<domaine>/edit-plan` et
`PATCH /api/kvm/vms/<hôte>/<domaine>` (`{spec, expect_config}`),
`POST /api/kvm/vms/<hôte>/<domaine>/hot/<op>` (`cpumem`, `add-volume`,
`remove-volume`, `insert-cdrom`, `eject-image`, `add-nic`, `remove-nic`,
`cloudinit`) ; `POST /api/kvm/hosts/<hôte>/images/upload` (corps brut),
`POST /api/kvm/uploads/<jeton>/cancel`, `POST .../images/fetch`,
`POST .../images/<pool>/<volume>/verify`,
`DELETE .../images/<pool>/<volume>` ; `POST .../volumes`,
`POST .../volumes/<pool>/<volume>/resize`,
`GET|DELETE .../volumes/<pool>/<volume>[/delete-plan]` ;
`POST .../pools`, `POST .../pools/<pool>/start|stop|refresh|autostart`,
`GET|DELETE .../pools/<pool>[/delete-plan]` ; `POST .../networks`,
`POST .../networks/<réseau>/start|stop|autostart`,
`GET|DELETE .../networks/<réseau>[/delete-plan]` ; `PUT .../image-pools` ;
`GET|POST /api/kvm/templates`, `GET|DELETE /api/kvm/templates/<nom>`.
Toute suppression exige l'empreinte `expect_delete` de son aperçu.
`/api/kvm/vms` donne à chaque VM ses champs en attente (`pending`) et une
table `edit_operations`.

Ligne de commande (`bin/harvester-kvm.py`, valeurs en `--opt=valeur`) :
`host caps`, `host image-pools`, `vm create --spec-file=F [--dry-run]` (ou
les options courtes `--name --vcpus --memory-mib --disk= --nic= ...`),
`vm edit --spec-file=F --expect-config=EMPREINTE [--dry-run]`,
`vm cpumem`, `vm add-volume|add-nic --spec-file=F`,
`vm remove-volume|eject-image --target=T`, `vm insert-cdrom`,
`vm remove-nic --mac=M`, `vm cloudinit [--rerun]`,
`image list|verify|upload|download-url|delete`,
`vol create|upload|resize|delete`,
`pool define|start|stop|refresh|autostart|delete`,
`net define|start|stop|autostart|delete|links`,
`template list|save|delete`. Les suppressions prennent `--expect-delete=`
de leur `--dry-run`.

**OpenSSH et la reprise des lectures.** Aucun plantage de `sshd-session`
sous plusieurs sessions multiplexées (1.98.0) n'a été observé depuis
OpenSSH 10.6p1 sur l'hôte du banc (1303 lectures multiplexées
simultanées, 0 échec). La reprise unique d'une lecture coupée par le
transport est gardée comme filet de sécurité.

**Ce qui est vérifié.** Sur l'hôte du banc (libvirt 12.8.0, QEMU 11.1.2,
OpenSSH 10.6p1), objets de test seulement, chaque étape contrôlée par
`virsh` et dans l'invité, l'inventaire de l'hôte identique avant et après
(hormis la liste gardée par libvirt du pool parent d'un pool imbriqué
supprimé) : `vol-upload` depuis un tuyau (octets
identiques, format sondé qcow2) ; pool et réseau NAT créés par la console,
un réseau sur le sous-réseau du LAN refusé ; cirros 0.6.2, openSUSE
Tumbleweed et une ISO Alpine téléversées avec leurs sommes publiées
contrôlées sur l'hôte ; une VM BIOS q35 avec un disque lié à cirros et un
mot de passe VNC créée dans la fenêtre, sa console ouverte ; une VM UEFI
Secure Boot avec TPM depuis une copie complète de Tumbleweed, avec un
utilisateur et une clé SSH : SSH dans l'invité avec la clé injectée,
`cloud-init status` done, nom d'hôte, Secure Boot actif, `/dev/tpm0` ;
une VM Tumbleweed créée dans la fenêtre dont l'adresse statique vient du
network-config de l'assistant, seed jamais retouché à la main : la carte,
nommée `enp1s0` par l'image, retrouvée par la MAC tirée par la console,
l'invité joint à cette adresse sans bail DHCP ; trois instances ; une
annulation pendant une copie complète qui n'a rien laissé ; un clonage
annulé pendant une copie dans un pool, l'action restée en cours jusqu'à
la fin de la copie par libvirt, puis rien de laissé ; une carte ajoutée
à une VM arrêtée avec une MAC tirée par la console ; vCPU de 1 à 2
et mémoire de 1 à 2 Gio vus dans l'invité ; quatre disques virtio ajoutés
à chaud, le cinquième et le sixième en attente d'un emplacement PCIe avec
la raison, tous retirés ; une carte ajoutée puis retirée ; type de
machine et maximum de mémoire changés au prochain démarrage, bandeau,
arrêt puis démarrage ; une ISO insérée puis éjectée ; une VM faite à la
main avec épinglage, `<cputune>`, `<numatune>`, métadonnées étrangères et
`<qemu:commandline>` modifiée, son XML identique (C14N) hors des champs
changés ; suppression avec stockage (seed, variables UEFI et vTPM
retirés, image gardée) ; refus sur image, réseau et pool ; tout retiré
par la console ; les mots de passe VNC et le mot de passe cloud-init
introuvables dans les réponses, journaux et traces d'actions. Non
vérifié : cartes macvtap, pools NFS, disques SCSI, baisse de vCPU, invité
sans pilote de ballon.

---

## 11. Reprise après sinistre : réplication asynchrone des VMs (CLI + API de la console)

### Plans de réplication, protection, bascule (1.99.0)

harvester-ops garde des copies de secours de VMs sur un second cluster
Harvester et les y bascule quand le premier site s'arrête, volontairement
ou non. Elle s'appuie sur ce que Harvester et Longhorn ont déjà : une cible
de sauvegarde commune aux deux clusters, des sauvegardes récurrentes
Longhorn sur la source, et des **volumes de secours** Longhorn (standby)
sur la cible, qui appliquent chaque nouvelle sauvegarde dès qu'elle arrive.
Rien n'est installé sur les clusters. Cette version livre le moteur, la
ligne de commande `harvester-replication` et les routes de la console.
Les écrans (contrats, fenêtre de création, vue d'un contrat, bascule,
liens, alertes) sont décrits plus bas dans « Écrans Disaster Recovery » ;
les exercices de reprise viendront avec leur moteur.

**Le contrat, c'est le plan.** Un plan nomme un cluster source, un cluster
cible, les VMs (une liste `ns/nom`, ou un sélecteur d'étiquettes dans un
namespace), un **objectif de RPO** (l'âge maximal de la copie sur la
cible), et les correspondances (réseaux, classes de stockage, namespaces,
images, noms de PVC, noms des VMs cibles). Il porte aussi ses options :
cloud-init `keep` (la VM cible garde le `firmware.uuid` de la source, donc
cloud-init ne repasse pas) ou `rerun` ; MAC `keep` ou `new` ; la VM cible
`started` ou `stopped` après une bascule ; qui suit le plan (`console`,
`cli` ou `off`) ; la tolérance d'écart entre les disques d'une VM (10 s) ;
et chaque attente, réglable par plan. Par défaut : cloud-init gardé, MAC
gardée, VM démarrée après la bascule, suivie par la console. Le plan est
gardé par la console dans `replication.db` (répertoire d'état). Le créer
n'écrit rien sur aucun cluster.

**RPO visé et RPO tenu.** Le RPO tenu par une VM est l'âge du plus ancien
point appliqué sur la cible parmi ses disques. `status` montre les deux,
par VM et par plan, et signale une VM en retard, en dérive (un passage qui
dure plus d'un intervalle, ou une spec modifiée), dégradée (une sauvegarde
en erreur, un volume de secours dégradé, un disque qui ne se sauvegarde
jamais) ou dont la source ne répond pas. Une VM arrêtée dont le dernier
point est postérieur à son arrêt est à jour (RPO 0).

**D'abord mesurer.** `measure start` pose une tâche de sauvegarde sur la
source seulement, à un intervalle d'essai, sans toucher à la cible. Après
quelques passages, `measure stop` la retire et `measure report` donne, par
intervalle candidat, les octets envoyés par passage, le débit et le RPO que
chaque intervalle tiendrait, avec un intervalle recommandé. `measure
formula` prévoit la même chose depuis des compteurs d'écriture, sans
cluster. Les chiffres du banc : trois écritures de 8 Mio toutes les 3
minutes sur un disque de 2 Gio envoient 25 Mo par passage, et la formule et
la mesure s'accordent à 8,5 Ko près.

**Protéger.** `protect` fait le pré-contrôle complet, puis pose sur la
source une tâche récurrente Longhorn par VM (`concurrency` = ses disques)
à l'intervalle choisi (donné, mesuré, ou celui du plan), une première
sauvegarde de chaque volume qui n'en a aucune, et sur la cible un volume de
secours par disque créé depuis la dernière sauvegarde, les backing images
qui manquent à la cible (restaurées depuis la cible de sauvegarde), et un
**carnet de reprise** par VM : un Secret qui porte la VM cible, ses secrets
cloud-init et ses volumes. La restauration initiale est suivie avec sa
progression (la taille des données de la sauvegarde, telle que Longhorn la
publie). `protect` se relance : sur un plan protégé, il ajoute ce qui
manque, réécrit un carnet dont le contenu a changé, et avec `--retain N`
met à jour la rétention de la tâche récurrente (seul moyen de la changer ;
`plan set --retain` refuse et renvoie ici). `--interval` change
l'intervalle de la même façon. `protect --dry-run` contrôle et demande aux
serveurs d'API un essai à blanc de chaque objet, sans rien écrire.

**Le suiveur.** Après chaque passage de sauvegarde, le suiveur demande à la
cible de relire la cible de sauvegarde pour ces volumes (il n'attend pas la
relecture de Longhorn toutes les 5 minutes). Il attend que tous les disques
d'une VM aient fini le même passage (la **barrière**, la moitié de
l'intervalle au plus), pour que les disques d'une VM soient appliqués
ensemble, puis vérifie que chaque volume de secours l'a appliqué. Il range
chaque passage (octets envoyés, durée de sauvegarde, retard sur la cible,
écart entre disques, RPO tenu), écrit un avertissement une fois quand une
condition apparaît et une fois quand elle disparaît, et garde des
statistiques par VM. Sur le banc, un passage était appliqué 13 à 46 s après
la fin de sa sauvegarde (médiane 16 s), et le RPO tenu a été au plus de
382 s pour un objectif de 600 s. La seule écriture du suiveur sur un
cluster est la demande de relecture. Une modification de la spec d'une VM
protégée (disques, tailles, classes, réseaux, MAC, firmware uuid, secret
cloud-init, processeur et mémoire) est une **dérive** : `status` avertit,
et `protect` réécrit le carnet. Toute autre différence entre la VM et son
carnet est signalée `carnet-stale` (rien n'est écrit).

**Bascule planifiée.** Les deux sites répondent. Pour chaque VM : le
suiveur est mis en pause pour le plan, la VM source est arrêtée,
l'étiquette de tâche est retirée et la tâche supprimée (la clôture), une
sauvegarde finale de chaque disque est prise et appliquée sur la cible,
puis les volumes de secours sont **activés**, les PV, PVC, secrets
cloud-init et la VM sont créés sur la cible depuis le carnet, et la VM
démarre. Rien n'est perdu : la cible porte l'état de la source à son arrêt.
Jusqu'à l'activation, une erreur ou une annulation remet tout en place (VM
source redémarrée, tâche et étiquettes rendues, plan `protected`).
L'activation est le point de non-retour. Sur le banc, une bascule
planifiée a pris 83 s (arrêt 16 s, sauvegarde finale 18 s, relecture
finale 14 s, démarrage de la VM 18 s).

**Bascule non planifiée.** La source ne répond pas (`--unplanned`,
confirmation exigée). La cible relit la cible de sauvegarde une dernière
fois (borné par `unplanned_sync`), chaque volume de secours doit avoir
appliqué la dernière sauvegarde qu'il connaît (`catch-up`, borné par
`unplanned_catchup`), puis les points sont lus par disque. Une VM dont les
disques sont écartés de plus que la tolérance est refusée sauf avec
`--accept-skew` ; un disque sans aucun point appliqué refuse la VM.
Ensuite, activation et mise en route comme ci-dessus. Le résultat dit, par
VM, à partir de quand les écritures sont perdues (`lost_after`, le plus
ancien point de ses disques) et l'écart. Si la source répond de nouveau
pendant la bascule, sa VM est arrêtée au mieux. Avec une source qui répond
encore, `--force-unplanned` l'arrête d'abord (clôture), puis bascule de la
même façon. `check --phase failover-unplanned` montre, sans écrire, les
points que chaque VM recevrait, l'écart, et les disques encore en
rattrapage.

**Reprise.** Une bascule interrompue après l'activation (annulation, arrêt
brutal, redémarrage de la console) reste en l'état : plan en `error`, rien
n'est défait. `resume` la termine, avec la cible seule si la source a
disparu. Il termine aussi une protection, une mesure ou une déprotection
interrompue. La confirmation donnée au départ d'une bascule vaut pour sa
reprise.

**Un arrêt de la console n'est pas une annulation (décision 68).** Un
redémarrage de la console, un redémarrage de l'hôte ou une mise à jour
forcée arrête une protection en cours (restauration initiale comprise),
une mesure ou une déprotection à son prochain point sûr, et ne défait
rien. La commande reste `interrupted` : le plan garde son état
(`protecting`, `measuring`, `unprotecting`), et l'étape atteinte figure
dans le résultat (`step`), au journal et dans son exécution. `resume` la
continue. Il ne recopie pas ce qui est déjà là : Longhorn continue de
restaurer pendant que la console est arrêtée, et la protection reprise
attend les mêmes volumes de secours (le bilan dit ce qui était déjà
restauré). Seule l'annulation d'une personne (bouton du dock, SIGTERM ou
Ctrl-C en ligne de commande) défait une protection. Une annulation de la
protection reprise défait toute la protection, comme une annulation avant
l'arrêt. Une bascule pas encore activée est défaite à l'arrêt de la
console, comme pour une annulation : avant son activation elle est tout ou
rien, et la VM source redémarre. Une commande arrêtée avant sa première
écriture rend `not-started` : la relancer.

**Déprotection, et décision 47.** `unprotect --yes` retire sur la cible les
volumes de secours, carnets et backing images restaurées des VMs jamais
activées, et sur la source les étiquettes de tâche, les tâches et les
instantanés du plan. Une VM dont la bascule a activé un volume garde
**tout** sur la cible (volumes activés, volumes de secours restants, PV,
PVC, la VM, secrets cloud-init, backing images utilisées) : abandonner une
bascule ne supprime rien qui porte des données. Ce qui est gardé est listé
dans `left`, avec les sélecteurs d'étiquettes pour le retrouver
(`left_selectors`). Son carnet est gardé tant que la VM cible n'existe pas
(c'est la seule recette qui reste), et supprimé une fois la VM en route.
Le plan reste listé `unprotected` avec son historique ; un plan brouillon
ou déprotégé peut ensuite être supprimé (son journal est gardé).
`unprotect --delete-backups --yes` (administrateur) supprime aussi les
sauvegardes du plan sur les deux clusters, mais est refusé
(`backups-in-use`) tant qu'une VM du plan est tenue par une bascule. Les
actions listées par la console et la ligne de commande ne la disent
permise que si la déprotection l'accepterait (sinon raison
`backups-in-use`).

**Purger les sauvegardes après une bascule terminée** reste manuel dans
cette version. Longhorn range les étiquettes du plan des sauvegardes de la
tâche récurrente dans `spec.labels` (source) et `status.labels` (cible),
pas dans `metadata.labels` : un sélecteur d'étiquettes ne les trouve pas.
Sur le cluster source, après avoir vérifié la liste :

```
kubectl -n longhorn-system get backups.longhorn.io -o json \
  | jq -r --arg p rp-XXXXXXXXXXXX '.items[]
      | select((.metadata.labels["harvester-ops.io/replication-plan"]
                // .spec.labels["harvester-ops.io/replication-plan"]
                // .status.labels["harvester-ops.io/replication-plan"]) == $p)
      | .metadata.name'
```

puis supprimer ces noms avec `kubectl -n longhorn-system delete
backups.longhorn.io <nom> ...`. Longhorn les retire de la cible de
sauvegarde, et le cluster cible les abandonne à sa relecture suivante. Un
CR Backup dont le BackupVolume a été supprimé peut rester en `Completed` :
le supprimer à la main. Un volume activé par une bascule ne dépend plus
d'aucune sauvegarde.

**Ce qui est créé, et où.** Chaque objet porte
`harvester-ops.io/replication-plan=<id du plan>`, plus
`harvester-ops.io/replication-generation` et `harvester-ops.io/replication-vm`
(uid de la VM source) quand il appartient à une VM.

| Cluster | Objets |
|---|---|
| Source | RecurringJob `longhorn-system/hr-<plan>-<vm>` (`task: backup`) ; étiquettes `recurring-job.longhorn.io/<tâche>: enabled` sur chaque volume Longhorn de la VM (plus `recurring-job-group.longhorn.io/default: enabled` quand le volume n'était dans le groupe par défaut qu'implicitement, retirée par la déprotection) ; Snapshots et Backups `hr-<empreinte>-first` et `-final` ; à une bascule, les annotations `harvester-ops.io/replication-stopped` puis `harvester-ops.io/replication-failed-over-to` sur la VM, qui est arrêtée |
| Cible | Volumes de secours `longhorn-system/hr-<vm>-<disque>-<empreinte>` (40 caractères au plus, la limite de Longhorn) ; BackingImages restaurées depuis la cible de sauvegarde quand elles manquent ; namespace `harvester-ops-replication` (jamais supprimé) avec un Secret carnet `hr-<plan>-<vm>` par VM ; à une bascule, un PV par disque (au nom du volume de secours, `Retain`, lié à son PVC par `claimRef`), les PVC, les secrets cloud-init, le namespace si `--create-namespace`, et la VM |

Le carnet porte une copie des secrets cloud-init de la VM (souvent des mots
de passe et des clés SSH). Garder `harvester-ops-replication` hors de tout
projet Rancher, lisible par les administrateurs seulement.

**Rôles.** Un lecteur lit les plans, les lignes, les statistiques et les
événements. Un opérateur crée et supprime les plans, mesure, protège,
change les réglages, reprend et déprotège. Une bascule, planifiée ou non,
exige un administrateur, de même que `unprotect --delete-backups`. La
reprise exige un administrateur quand la commande interrompue était une
bascule ou une déprotection avec suppression des sauvegardes. Annuler une
action exige le rôle qui l'a lancée.

**Confirmations.** Une bascule sans `--vm` exige `--all-vms --yes` ; une
bascule non planifiée exige `--yes` ; `--accept-skew` ne va qu'avec
`--unplanned` ; `unprotect` exige `--yes`. Dans la console, une bascule hors
essai à blanc et toute déprotection exigent `confirm` égal au nom du plan.

**Codes de sortie** de `harvester-replication` : 0 fait ; 1 échec (dont
`undo-timeout`, un retour arrière à court de temps, et `interrupted` après
une erreur passé l'activation) ; 2 usage ou refus (blocage du pré-contrôle,
confirmation absente, état qui ne permet pas la commande, `state-changed`
quand le plan a changé entre la lecture et le verrou, `open-run` quand une
bascule est ouverte sur un autre périmètre) ; 3 annulé par un signal
(`cancelled` : défait, ou `interrupted` : rien n'est défait, `resume`
termine), ou arrêté par l'arrêt de la console (SIGUSR2 : `interrupted`
avec `step` et `resume_required`, ou `not-started` quand rien n'a été
écrit) ; 4 `status` avec un objectif dépassé (son JSON dit quand même
`ok: true`). Avec `--json`, exactement un objet part sur stdout ; les
étapes partent sur stderr en `STEP_EVENT|<étape>|<statut>|<message>`. Aucun
chemin de fichier n'apparaît jamais dans une sortie ni dans le journal.

**Délais et arrêt de la console.** Chaque attente a une valeur par défaut
(`probe` 15 s, `final_backup` 6 h, `initial_restore` 12 h, `resync_apply`
600 s, `activate` 300 s, `vm_start` 900 s, `unplanned_sync` 120 s,
`unplanned_catchup` 600 s, `client` 30 s par appel kubectl...), surchargée
par plan (`plan set --timeout NOM=S`, à chaud) et par commande (`--timeout
NOM=S`). Tout retour arrière a une échéance unique, `undo_total` (240 s) :
passée, plus rien n'est tenté et ce qui reste est listé. Arrêter ou
redémarrer la console envoie SIGUSR2 à chaque commande de réplication en
cours, qui veut dire « arrête au prochain point sûr et garde l'état ». Une
protection, une mesure ou une déprotection s'arrête entre deux étapes ou au
prochain relevé d'une attente, en au plus un appel kubectl (`client`, 30 s
par défaut ; au-delà, la commande s'arrête là où elle est, toujours sans
rien défaire). La console l'attend `client + 15` s, puis la tue. Une
bascule pas encore activée est défaite dans son échéance `undo_total`, et
une annulation déjà demandée par une personne garde son échéance de retour
arrière : seules ces deux-là peuvent encore consommer le budget d'arrêt.
L'unité systemd attend jusqu'à 600 s (`podman stop -t 600`,
`TimeoutStopSec=630s`) ; la console sort tout de suite quand aucune
commande de réplication ne tourne. Un plan suivi par la console ne peut pas
avoir `undo_total + client` au-delà de 570 s (refusé avec `stop-budget`).
Une console lancée hors du conteneur par sa propre unité systemd a besoin
de `KillMode=mixed` : avec `control-group`, la valeur par défaut, systemd
envoie aussi SIGTERM à chaque enfant, ce qui est une annulation.

**Le processus de suivi.** La console lance un suiveur enfant,
`harvester-replication follow --all --console`, pour les plans dont
l'option `follow` vaut `console`, jamais dans un processus lecteur. Elle le
relance après 5, 15, 30, 60, 120 puis 300 s quand il s'arrête de lui-même
(600 s de marche remettent l'attente à zéro). Son journal est
`logs/replication-follow.log`, renommé en `.1` au-delà de 10 Mio pendant
qu'il tourne (l'enfant rouvre son fichier sur SIGUSR1). Un seul suiveur par
répertoire d'état : une seconde console sur le même état indique
`elsewhere` avec le pid du détenteur (verrou `replication-follow.lock`).
Interrupteurs : `HARVESTER_OPS_REPLICATION_FOLLOW=0` au démarrage (aucun
suiveur), `replication: {follow: false}` dans `config.yaml` (relu toutes
les 2 s : l'enfant est arrêté, et relancé quand l'interrupteur revient), et
les options `follow` et `paused` de chaque plan. Chaque plan est tenu par
un seul suiveur à la fois au moyen d'un bail : `follow --plan P
--take-over` depuis un terminal le reprend, et la console le reprend à son
tour suivant une fois libéré.

**Métriques** (`/metrics`) : `harvester_ops_replication_follower_up`,
`harvester_ops_replication_follower_restarts_total`,
`harvester_ops_replication_plans_followed`, et par plan
`harvester_ops_replication_rpo_held_seconds{plan,source,target}` et
`harvester_ops_replication_rpo_objective_seconds{plan,source,target}`. Le
registre est relu au plus toutes les 30 secondes, quel que soit le nombre de
relevés. Les actions de réplication apparaissent dans
`harvester_ops_actions_total` en `replication:<op>`, sans l'id du plan.

**Où faire tourner la console.** `check` ajoute un conseil
`console-placement` : la console qui suit et bascule un plan va sur le
site de secours ou hors des deux sites, jamais seulement sur le site
source.

**Limites.** Volumes Longhorn v1 seulement : disques de VM (`rwx` +
`migratable`) et `rwo` ; ni Longhorn v2, ni volumes chiffrés, ni volumes
`rwx` non migrables (refusés par le contrôle). Les disques en mode bloc
ont été vérifiés ; un PVC en mode système de fichiers n'est pas refusé mais
n'a pas été vérifié. Les deux clusters doivent déjà utiliser la même cible
de sauvegarde. Chaque disque est sauvegardé pour lui-même : il n'existe pas
d'instantané atomique de plusieurs volumes, la barrière ne fait que réduire
l'écart, et l'écart est montré et contrôlé à la bascule. Une VM avec un
disque conteneur, hôte ou vide est répliquée sans ces disques (signalé). Le
retour au premier site, les exercices et le gel de l'invité ne sont pas
dans cette version (les réseaux d'exercice se déclarent déjà, voir
plus bas). Une sauvegarde Longhorn est cohérente comme
après une coupure de courant (l'invité n'est pas gelé). Un volume de
secours occupe sa place sur la cible dès la protection, pas seulement le
jour de la bascule. Une activation est définitive : pour protéger de
nouveau, le volume de secours est recréé et tout est recopié.

**Ce qui a été vérifié pour de vrai** (harvlab, Harvester 1.8.2 / Longhorn
1.11.2, vers harvlab2, Harvester 1.9.0 / Longhorn 1.12.1, 24/10/2026) :
mesure, protection (43 s pour deux VMs), suiveur pendant 35 minutes,
bascule planifiée à l'octet près, bascule non planifiée source injoignable
avec écart refusé puis accepté, protection annulée sans reste, bascule
interrompue après l'activation puis reprise, déprotection avec la décision
47, suppression des sauvegardes, supervision du suiveur (reprise du bail,
kill -9, arrêt de la console). Rejoué sur les mêmes bancs le soir même :
un disque d'image sans correspondance d'image placé par la correspondance
des classes, `protect --retain` sur un plan protégé et son annulation
(huit signaux à des étapes différentes, tâche et ligne du plan remises à
chaque fois), un changement de processeur et de mémoire qui lève
`plan-drift` et `carnet-stale` puis protect qui réécrit le carnet (la VM
basculée a démarré à la nouvelle taille), aucun `rpo-drift` après une
protection qui a repris une sauvegarde de 6 minutes, la taille de la
restauration initiale égale à ce que Longhorn a restauré (4 Kio d'écart
sur 348 Mio), le contrôle non planifié source perdue (points, écart,
rattrapage en cours), `point_at` dans les deux modes et `lost_after` pour
la non planifiée seulement, et `unprotect-delete-backups` refusé sur un
plan dont une VM est activée. Le 24/10/2026 au soir, la décision 68 par
une seconde console : un disque de données de 2 Gio (données
incompressibles), une protection lancée depuis la console, la console
arrêtée pendant la restauration initiale (sortie en 1 s au lieu d'un
retour arrière) : plan toujours `protecting`, étape `initial-restore`
rangée, volumes de secours et tâche gardés sur les deux clusters, Activité
qui affiche « interrupted by the console shutdown » ; la reprise depuis la
console redémarrée a atteint `protected` avec les mêmes volumes de secours
(mêmes uid), la seconde fois avec 1,1 Gio sur 2,0 Gio déjà restaurés par
Longhorn pendant l'arrêt, finie 11 s plus tard. Une annulation depuis le
dock pendant la restauration initiale a toujours tout défait. Faits
Longhorn sur lesquels elle repose :
l'étiquette `backup-volume` d'un Backup est le nom du volume ; les
étiquettes de la tâche récurrente sont dans `spec.labels` et
`status.labels`, jamais dans `metadata.labels` ; un instantané ou une
sauvegarde d'un volume détaché dit d'abord « waiting for the volume to be
attached », puis se prend ; après l'activation, Longhorn 1.12.1 ne publie
plus la sauvegarde appliquée, donc les points lus juste avant l'activation
font foi (si Longhorn finit une restauration pendant l'activation, le point
réel est plus récent que celui annoncé : `lost_after` se trompe trop tôt,
jamais trop tard). **Non vérifié** : la taille des requêtes à travers
Rancher pour de grands plans (Rancher était injoignable), le sens Longhorn
1.12 vers 1.11 par le produit (vérifié à la main sur le banc de conception
seulement), les sauvegardes en erreur, la migration à chaud pendant la
protection, une bascule non planifiée forcée sur une source joignable, une
sauvegarde en vol pendant le rattrapage d'une bascule non planifiée, un
BackupVolume cible supprimé pendant un passage, SIGTERM pendant un kubectl
lent, des nœuds suspendus pendant une bascule, un arrêt gracieux de la
cible (harvlab2) pendant la protection, un arrêt de la console pendant une
mesure ou une déprotection (couvert par les tests seulement), un PVC en
mode système de fichiers, et les refus du contrôle
pour la capacité, un nom de volume pris et une MAC prise. Le contrôle non
planifié à blanc juge `caught-up` d'après les sauvegardes que la cible voit
à cet instant, qui peuvent avoir 1 à 4 minutes de retard sur la source ; la
vraie bascule resynchronise d'abord.

### Options de reprise : vagues, actions annexes, taille, réseau, classe par disque

Un plan porte aussi un bloc **reprise** (`recovery`), posé à la création ou
changé ensuite (`recovery` dans la console, `--spec FICHIER` en ligne de
commande), et deux genres de correspondances de plus. Ce qui a été joué
pour de vrai sur les bancs est listé à la fin de la section des écrans
Disaster Recovery.

**Classe de stockage par disque** (`disk_classes`, `{"ns/vm/disque":
"classe"}`, disque = le nom du disque KubeVirt). Elle l'emporte sur les
correspondances d'images et de classes pour ce disque. Une classe absente
de la cible bloque le contrôle (`storage-class-unmapped`, avec le fait
`disk`). Un disque qui a sa classe ne reçoit pas l'annotation d'image de la
cible (l'image désignerait une autre classe). Elle change ce que pose
protect : elle ne se change que sur un plan brouillon.

**Réseaux d'exercice** (`drill_networks`, `{"ns/réseau": "ns/réseau"}`) :
rangés et contrôlés comme les correspondances de réseaux. Les exercices
viendront dans une version suivante.

**Vagues** (10 au plus) : `{"name", "vms": ["ns/nom", ...], "gate":
{"kind", "seconds"}}`. Après l'activation, la bascule démarre les VMs vague
par vague. Une VM listée dans aucune vague part dans une dernière vague,
implicite. Chaque vague passe une condition avant la suivante : `running`
(par défaut, les VMs tournent), `agent` (l'agent invité répond, condition
`AgentConnected` de la VMI, pendant au plus `seconds`, 300 par défaut, de
10 à 3600) ou `delay` (`seconds` après le démarrage des VMs, de 1 à
3600). Une condition `agent` non remplie à temps avertit
(`wave-gate-timeout`) et la bascule continue. Une VM laissée arrêtée
(`start: stopped`) n'attend aucune condition. Une VM listée deux fois est
refusée. Un plan par sélecteur peut nommer des VMs qui n'existent pas
encore. Quand une partie seulement d'un plan bascule, une vague sans
aucune de ses VMs est sautée, avec ses actions. Le journal reçoit
`wave-start`, `wave-passed` ou `wave-gate-timeout` (étape `wave`) pour
chaque vague.

**Actions annexes HTTP** (20 au plus) : `{"name", "at", "method", "url",
"headers", "secret_headers", "body", "timeout_s"}`. `at` vaut `start`
(après l'activation, avant la première vague), `wave:<n>` (après le
passage de la vague n ; n peut être la dernière vague, implicite) ou
`end`. La méthode est GET, POST (par défaut), PUT, PATCH ou DELETE ; le
corps fait au plus 16 Kio ; le délai va de 1 à 60 s (15 par défaut) pour l'appel entier, pas pour chaque lecture, pour
qu'un arrêt de la console atteigne le prochain point sûr bien avant son
budget d'arrêt. L'adresse commence par `http://` ou `https://`, sans
identifiants : ils vont dans `secret_headers`.

Chaque appel passe par la même garde anti-SSRF que le téléchargement
d'ISO : le nom est résolu une fois et la connexion part vers l'adresse
vérifiée ; bouclage, lien local, adresses privées, CGNAT, multidiffusion et
réservées sont refusés, sauf autorisation dans `config.yaml` par
`replication.hooks.allow_private` (liste de CIDR, comme
`iso_fetch.allow_private`) ; les adresses de métadonnées de cloud restent
refusées même autorisées. Les cibles des actions annexes sont souvent
internes (API DNS, répartiteur) : déclarez-y leurs réseaux. Une
redirection n'est jamais suivie (l'appel échoue en le disant, avec l'adresse visée masquée) : un en-tête
secret n'atteint jamais une autre adresse. L'adresse est aussi contrôlée à
la création ou au changement du plan : une adresse interne répond 400
`hook-address` tout de suite (un nom qui ne se résout pas encore est
revérifié à l'appel). Une action en échec (refus, délai dépassé,
statut 400 ou plus) avertit (`hook-failed`) et la bascule continue :
l'activation est définitive. Chaque appel écrit son statut et sa durée au
journal (étape `hook`, `hook-ok` ou `hook-failed`), jamais la valeur d'un
en-tête ni le corps de la réponse. Une bascule reprise après une
interruption ne rappelle pas une action déjà appelée. `hook test` appelle
une action tout de suite.

**En-têtes secrets** : rangés dans `replication.db`, montrés `********`
partout ailleurs : plan lu par la console, `plan show --json`, sorties de
`plan create` et `plan set`, journal et Activité. Renvoyer `********` à
une modification garde la valeur rangée de cet en-tête, seulement si
l'action garde son nom, sa méthode et son adresse rangée exacte (schéma,
hôte, port, chemin et requête). Sinon le changement est refusé avec
`secret-missing` (« l'adresse a changé » ou « la méthode a changé,
saisissez de nouveau les valeurs des en-têtes secrets ») : un opérateur ne
peut ni envoyer ailleurs un secret rangé, ni faire un autre appel avec le
droit d'un autre. Un `********` sans valeur rangée (action nouvelle ou
renommée) est refusé de même. L'adresse se montre avec son schéma, son
hôte, son port et le premier segment de son chemin seulement, la même pour
tous les rôles : le reste du chemin devient `/********` (un seul segment
devient `/********`) et la requête `?********`, car les webhooks entrants
y portent leur jeton (`https://hooks.slack.com/services/T0/B0/XXX` se
montre `https://hooks.slack.com/services/********`). C'est vrai dans toute
lecture, sortie, ligne du journal et d'Activité, et dans le libellé des
actions : renvoyer cette adresse masquée garde celle rangée ; la modifier
demande de la retaper en entier. Ils ne passent jamais par une ligne de
commande : la console les donne au script dans son fichier privé.

**Taille au secours** (`resize`, `{"ns/vm": {"cpu": 1..256, "memory":
"<n>Mi|<n>Gi"}}`). La VM cible est écrite comme Harvester l'écrit : vCPU
dans `domain.cpu.cores` et `resources.limits.cpu`, mémoire dans
`resources.limits.memory`, `requests` de la source retirées (le mutateur
de VM de la cible les calcule depuis son réglage de surallocation) et
`domain.memory.guest` qui garde l'écart limite - invité de la source (ou
l'annotation `harvesterhci.io/reservedMemory`). Une VM à branchement à
chaud du processeur et de la mémoire compte ses vCPU en `sockets` et garde
la mémoire en `memory.guest` (cas non joué sur un cluster).

**Configuration réseau** (`network_data`, `{"ns/vm": "<network-config
cloud-init, 16 Kio au plus>"}`) : demande `cloud_init: rerun`. Le volume
cloud-init de la VM cible (NoCloud ou ConfigDrive) la porte en ligne
(`networkData`) et perd sa référence au secret des données réseau ; une VM
sans volume cloud-init en reçoit un (NoCloud).

**Protection d'office** (`auto_protect`, vrai ou faux) : rangée pour les
plans par sélecteur ; son côté console est décrit avec le suiveur.

**Les changer.** `PATCH .../settings` et `plan set` changent `recovery`
(chaque clé donnée remplace la valeur rangée), `cloud_init`, `mac` et
`start` dans tout état sauf `failing_over`, les correspondances
`networks` et `drill_networks` dans les mêmes états, et les autres
correspondances sur un plan brouillon seulement (sinon `state-forbids`).
Taille, configuration réseau, cloud-init, MAC, démarrage et
correspondance de réseaux changent le carnet de reprise. Seule la valeur
compte : renvoyer les mêmes valeurs, ne changer que les actions annexes,
les vagues ou l'accord sur un secours dégradé, ou revenir aux valeurs
protégées (Annuler) ne marque rien. Sur un plan protégé dont une valeur du
carnet a vraiment changé, la réponse porte `protect_needed: true`, et le
protect suivant réécrit le carnet. D'ici là, le plan garde les clés changées
(`carnet_stale` dans `GET /api/replication/plans/<id>`), le suiveur
signale aussi chaque VM dont le carnet diffère de ce que protect écrirait
aujourd'hui, et les deux se montrent par l'alerte `carnet-stale`. Une
bascule refuse alors de partir (constat `carnet-stale`, bloquant, qui
nomme les clés changées et les VMs), sauf si on lui dit de prendre les
réglages précédents : `accept_stale_carnet: true` dans la console,
`--accept-stale-carnet` en ligne de commande (le constat devient un
avertissement). Un protect réussi efface la marque. Une bascule
interrompue avant toute activation de volume refait le contrôle quand on
la termine : si les réglages sont passés en retard entre-temps (vu par le
suiveur, ou bascule partie sous 1.99), `resume P --accept-stale-carnet` la
termine avec les réglages précédents, de même que `failover P
--accept-stale-carnet` avec les mêmes VMs (elle reprend l'exécution
ouverte). Aucun des deux drapeaux n'est accepté pour une autre commande
interrompue (`bad-option`).

**Répliques du secours.** Au protect, une classe cible qui demande plus de
répliques (`numberOfReplicas` de la StorageClass, 3 en son absence, comme
Longhorn) que la cible n'a de nœuds Longhorn planifiables bloque le
contrôle (`standby-replicas-exceed-nodes`, avec les classes qui tiennent
dans `suggest`) : chaque volume de secours resterait dégradé et l'alerte
`standby-degraded` ne s'éteindrait jamais. Un secours dégradé peut être un
choix (site de secours d'un seul nœud) : la clé de reprise
`accept_degraded_standby: true` (la case « Accepter des volumes de secours
dégradés » de l'onglet Réglages) change le constat en avertissement, pour
tous les protect suivants. En ligne de commande, `protect P
--accept-degraded-standby` et `resume P --accept-degraded-standby`
l'acceptent pour cette exécution seulement. Le refus le dit en une phrase
(console en cinq langues, ligne de commande et journal en anglais).
Changement de comportement depuis 1.99.0, où c'était un avertissement : un
protect interrompu sous 1.99 sur une telle classe est refusé à la reprise
tant que l'un des deux n'est pas donné.

**À blanc.** `failover --dry-run` (et `check --phase failover`) liste les
vagues (`index`, `name`, `vms`, `gate`, `implicit` pour la dernière) et
les actions (`name`, `at`, `method`, `url`), sans rien appeler. Une vraie
bascule rend les appels dans `hook_results`.

**Candidates.** `candidates --cluster C [--namespace NS [--selector
k=v]] --json`, et `GET /api/replication/candidates/<cluster>` dans la
console (lecteur, kubeconfig de la personne), listent les VMs d'un cluster
source qu'un plan peut protéger : vCPU, mémoire, réseaux, genre de
cloud-init, étiquettes, en marche ou non, et par disque le PVC, la taille,
la place occupée, la classe, l'image et l'éligibilité. Raisons :
`not-longhorn`, `longhorn-v2`, `encrypted`, `rwx-not-migratable`,
`containerDisk`, `hostDisk`, `emptyDisk`, `no-disk`. Un disque que la
réplication ne recopie pas (disque conteneur) est montré inéligible sans
rendre la VM inéligible. `plans` liste les plans qui tiennent déjà la VM
(tout état sauf `unprotected`).

La procédure d'exploitation (première protection, bascule, déprotection)
est dans [procedure-operationnelle.md](procedure-operationnelle.md#12-reprise-après-sinistre-réplication-asynchrone).

### Données de suivi, alertes, notifications et essais de lien

Ce sont les lectures et les réglages derrière les écrans de la
réplication. La console et la ligne de commande appellent les mêmes
fonctions, et montrent donc les mêmes objets.

**Lignes de plan.** Chaque ligne de plan (`GET /api/replication/plans`, la
`row` d'un plan, `plan list --json`, `plan show --json`) porte aussi
`transfer` (le dernier passage de n'importe quelle VM : date, octets
envoyés, débits d'envoi et d'application ; octets et passages des
dernières 24 heures), `pending_vms` (les VMs d'un plan à sélecteur qui
portent l'étiquette sans être encore protégées, en `ns/nom`), `alerts` (le
nombre d'alertes en cours du plan) et `reach` (si le suiveur a lu la source
et la cible à son dernier relevé : `true`, `false`, ou `null` jamais lu ;
la pire valeur des VMs du plan, avec la date du relevé le plus récent).

**Séries des graphiques.**
`GET /api/replication/plans/<id>/series?range=1h|24h|7d|30d` (`resource=<uid>`
facultatif) regroupe les statistiques par passage en tranches de 60 s,
600 s, 3600 s et 21600 s : pire RPO tenu et pire arriéré, débits moyens
(pondérés par les passages pour les lignes réduites à l'heure), octets
envoyés et nombre de passages. Les tranches vides sont omises.

**Plans à sélecteur.** À chaque relecture des specs (toutes les 10 minutes
par défaut, `follow_spec`), le suiveur liste les VMs source du namespace du
sélecteur. Une VM qui porte l'étiquette sans être dans le plan est écrite
une fois au journal (`selector-new-vm`, avertissement) et gardée dans les
`pending_vms` du plan jusqu'à sa protection ou la perte de l'étiquette.
Une VM du plan qui a perdu l'étiquette est écrite une fois
(`selector-vm-left`, information) : elle reste protégée jusqu'à la
déprotection. Un redémarrage du suiveur ne réécrit ni l'un ni l'autre. Le
suiveur ne protège jamais rien.

**Protection automatique.** Avec l'option de plan `auto_protect: true`
(rangée par les réglages du plan), un plan à sélecteur protégé qui a des
VMs en attente et aucune commande en cours reçoit un `protect` lancé par
la console elle-même : action suivie `replication:<plan>:protect`, auteur
`console`, avec les kubeconfigs de la console, au plus une fois toutes les
10 minutes par plan. Seule la console qui tient le verrou du suivi le
fait. Une protection lancée ainsi qui échoue (VM étiquetée qu'on ne peut
pas protéger : classe absente sur la cible, volume chiffré, disque de
conteneur seul...) n'est pas relancée à chaque tour : l'attente double à
chaque échec (10 min, 20 min, 40 min...) jusqu'à 6 heures, et un seul
événement `auto-protect-failed` (avertissement, envoyé aussi aux
notifications, avec les constats bloquants) est écrit pour toute la série,
avec l'alerte en cours du même nom. La série repart quand les VMs en
attente ou le plan changent, et un protect réussi l'efface. L'état vit
dans `replication.db` : un redémarrage de la console ne relance pas tout
de suite. `auto_protect: true` est refusé sur un plan qui liste ses VMs
(il n'a pas de nouvelle VM à protéger). Interrupteur, relu chaque minute : `replication: {auto_protect:
false}` dans `config.yaml`.

**Alertes en cours.** `GET /api/replication/alerts?host=<cluster>` et
`harvester-replication alerts [--host H] --json` calculent les alertes sur
l'état présent, erreurs d'abord : `rpo-exceeded`, `disk-never-backs-up`,
`standby-degraded`, `plan-drift`, `selector-new-vm`, `carnet-stale`,
`auto-protect-failed` (avertissements),
`source-unreachable`, `backup-error`, `plan-error` (plan en `error`) et
`source-vm-running-after-failover` (erreurs). Cette dernière vient d'un
drapeau de santé du suiveur : après une bascule, il surveille la VM source
et la signale tant qu'elle tourne à nouveau. `since` est la date du
dernier événement de ce genre au journal (ou la date où une VM en attente
a été vue la première fois). `host` garde les plans où ce cluster est
source ou cible. Ranger une VM protégée dans un dossier, la déplacer vers
un autre dossier ou renommer son dossier ne lève ni `plan-drift` ni
`carnet-stale` : l'annotation de dossier n'est pas un réglage de reprise.

**Notifications externes.** Matrix (API client-serveur, un id de salon et
un jeton d'accès), Slack (webhook entrant), un webhook JSON générique
(en-têtes en clair et en-têtes secrets) et le courriel (SMTP avec STARTTLS
ou TLS implicite sur le port 465, identification facultative ; un mot de
passe exige l'un des deux, il ne part donc jamais en clair, `implicit_tls`
dans le réglage). Le réglage vit dans `replication.db` :
`enabled`, `min_level` (`warn` ou `error`), `codes` (`null` pour tous les
codes d'alerte ci-dessus, plus les résultats de bascule `failed-over` et
`interrupted` et le retour sous l'objectif `rpo-recovered`), et les canaux.
`failed-over` et `rpo-recovered` partent quel que soit `min_level`, quand
ils sont choisis. Le suiveur de la console les envoie : à chaque tour, les
nouveaux événements du journal qui passent le filtre sont groupés en un
message par canal, envoyé depuis un fil à part (10 s par appel, 3 essais) :
un serveur de courriel lent ne retarde jamais le suiveur. La position dans
le journal avance une fois le lot traité ; un canal en échec après trois
essais est écrit au journal (`notify-failed`). Un lot encore en vol à
l'arrêt du suiveur est renvoyé à son redémarrage. Le premier tour après une
mise à jour part de la fin du journal (rien d'ancien n'est envoyé). Un
suiveur lancé à la main n'envoie qu'avec `follow --all --notify`. Les
secrets (jeton Matrix, URL Slack, valeurs des en-têtes secrets, mot de
passe du courriel) s'affichent `********` à chaque lecture (API, sortie de
la ligne de commande, journal, Activité), et l'adresse d'un webhook
masquée comme celle d'une action annexe (premier segment du chemin
seulement, `?********` pour la requête). Renvoyer `********` garde la valeur rangée : un écran peut
renvoyer le réglage qu'il a lu, mais seulement vers le même serveur : un
jeton Matrix n'est gardé que si le serveur garde son schéma, son hôte et
son port, les en-têtes secrets d'un webhook que pour la même adresse exacte (chemin
et requête compris), un
mot de passe de courriel que pour le même serveur SMTP et le même port
(`secret-missing` sinon). Chaque appel passe par la garde anti-SSRF
(adresses internes refusées sauf autorisation par
`replication.notifications.allow_private`, redirections jamais suivies,
serveur SMTP contrôlé de même) ; un refus n'est pas réessayé. Les adresses
sont aussi contrôlées à l'enregistrement du réglage (`notify-address`). Le réglage
arrive à la ligne de commande par un fichier privé (`notify set --spec
FICHIER`), jamais par ses arguments.

**Liens entre clusters.** `GET /api/replication/links` et `link list
--json` rendent une entrée par couple source et cible qui porte un plan ou
un essai : ses plans, les chiffres passifs tirés des passages de ses plans
(derniers débits d'envoi et d'application, médianes et totaux sur 24 heures,
une série par heure) et le dernier essai actif. L'essai actif (`POST
/api/replication/links/test`, `link test --from A --to B --size-mib N`,
64 à 4096 Mio, 512 par défaut) mesure ce que la réplication emprunte
vraiment : une pod temporaire sur la source écrit N Mio de données
aléatoires dans la cible de sauvegarde NFS commune, puis une pod sur la
cible les relit et les efface. Les durées sont lues dans le journal des
pods. Les deux pods prennent l'image longhorn-manager du cluster même
(déjà sur chaque nœud, rien à tirer, compatible airgap) et tournent dans
`longhorn-system`. Pods et fichier sont toujours retirés, en échec, en
annulation et à l'arrêt de la console ; un fichier dont l'effacement n'est
pas confirmé est retiré par une pod de nettoyage, et le résultat le dit si
même cela échoue. L'essai est refusé avec `backup-target-differs` quand les
deux clusters n'utilisent pas la même cible de sauvegarde, et
`not-supported` pour une cible autre que NFS. Il tourne avec les
kubeconfigs de la personne pour les deux clusters, en action suivie, un à
la fois par couple, et son résultat est gardé (table `link_tests`). La pod
de lecture retire aussi le répertoire de l'essai
(`harvester-ops-linktest/`) du partage, sauf si un autre essai y a encore
son fichier. Vérifié en réel les 24 et 25/10/2026 entre harvlab
(Harvester 1.8.2) et harvlab2 (1.9.0) sur le partage NFS du banc : 256 Mio
écrits à 99 Mio/s, relus à 103 Mio/s, pods retirées.

**Rôles** de ces routes : toute lecture est en lecteur, sauf le réglage des
notifications (opérateur, même masqué : il dit où partent les alertes). Le
changer et envoyer un message d'essai demandent administrateur. Un essai de
lien demande opérateur.

API : lectures `GET /api/replication/plans?side=any|source|target&host=`,
`GET /api/replication/plans/<id>` (plan, ligne, état, volumes, dernières
exécutions, verrou, bail, `carnet_stale`),
`GET /api/replication/plans/<id>/stats?resource=&since=&limit=`,
`GET /api/replication/hosts/<harvester ou kvm>/<hôte>/resources?side=` (2.0.0 ;
l'ancienne forme `GET /api/replication/hosts/<cluster>/resources` répond encore
pour un cluster Harvester ; un hôte KVM rend une liste vide avec `available: false`),
`GET /api/replication/resources/<uid>/events?limit=&before=`,
`GET /api/replication/follower`,
`GET /api/replication/candidates/<cluster>?namespace=&selector=` (403
`source-denied`, 404 `unknown-cluster`),
`GET /api/replication/plans/<id>/events?limit=&before=&level=warn` (le
journal du plan et de ses VMs),
`GET /api/replication/plans/<id>/series?range=&resource=`,
`GET /api/replication/alerts?host=`, `GET /api/replication/links`,
`GET /api/replication/notify` (opérateur, secrets masqués) ; écritures
(réponse `202 {action_id}`) `PUT /api/replication/notify` (administrateur),
`POST /api/replication/notify/test` (`{channel}`, administrateur),
`POST /api/replication/links/test` (`{from, to, size_mib}`),
`POST /api/replication/plans` (aussi `map_disk_class`, `map_drill_net` et
`recovery {waves, hooks, network_data, resize, auto_protect,
accept_degraded_standby}` ; une adresse d'action annexe interne répond 400
`hook-address`),
`POST /api/replication/plans/<id>/check` (`{phase, force_unplanned}`),
`POST /api/replication/plans/<id>/measure` (`{op: start|stop, interval}`),
`POST /api/replication/plans/<id>/protect` (`{interval, retain, dry_run}`),
`POST /api/replication/plans/<id>/failover` (`{vms | all_vms, mode,
force_unplanned, accept_skew, accept_stale_carnet, dry_run, confirm}`,
administrateur),
`POST /api/replication/plans/<id>/resume`,
`POST /api/replication/plans/<id>/unprotect` (`{confirm, delete_backups}`),
`PATCH /api/replication/plans/<id>/settings` (`{rpo, paused, follow,
skew_tolerance, timeouts, recovery, mappings, cloud_init, mac, start}` ;
`interval` et `retain` répondent `not-settable`, un changement que l'état
interdit répond `state-forbids`, la réponse ajoute `protect_needed: true`
quand le carnet d'un plan protégé change),
`POST /api/replication/plans/<id>/hooks/test` (`{name}`, opérateur),
`DELETE /api/replication/plans/<id>`. Chaque écriture
lance la ligne de commande avec les kubeconfigs de la personne (403
`target-denied` ou `source-denied` quand la session ne voit pas un
cluster, jamais celui de la console).

Ligne de commande : `harvester-replication plan create|list|show|set|delete`,
`check P [--phase protect|failover|failover-unplanned]`, `measure
start|stop|report P`, `measure formula`, `protect P [--dry-run]
[--interval D] [--retain N] [--accept-degraded-standby]`, `follow (--plan P | --all) [--once]
[--take-over]`, `status [P] [--host H]`, `resources --host H [--side S]`,
`events (--resource UID | --plan P | --host H) [--before ID|DATE]`,
`failover P (--vm ns/nom ... | --all-vms) [--unplanned]
[--force-unplanned] [--accept-skew] [--accept-stale-carnet] [--yes]
[--dry-run]`, `resume P [--accept-degraded-standby | --accept-stale-carnet]`,
`unprotect P --yes [--delete-backups]`, `hook test P --name N`,
`candidates --cluster C [--namespace NS [--selector k=v]]`, `alerts [--host
H]`, `notify show|set --spec FICHIER|test --channel ID`, `link list`, `link
test --from A --to B [--size-mib N]`. `plan create` prend aussi
`--map-disk-class ns/vm/disque=classe` et `--map-drill-net S=D` ; `plan
set` prend `--cloud-init`, `--mac` et `--start` ; les objets `recovery` et
`mappings` ne passent que par `--spec FICHIER`. P est un id ou un nom de
plan. Les
commandes qui parlent aux clusters prennent `--from-kubeconfig`,
`--to-kubeconfig` et `--timeout NOM=S` ; toutes prennent `--json`.

---

### Écrans Disaster Recovery

La console a un groupe de menu racine **Disaster Recovery** avec deux
entrées, **Contrats** et **Liens**, et une entrée **Disaster Recovery** dans
le groupe de menu de chaque cluster Harvester. Depuis la 2.0.0 ce sont des
entrées du menu latéral, chacune avec son adresse : **Disaster Recovery**
(`#dr`) et **Liens** (`#dr/links`) à la racine, et **Disaster Recovery**
dans le sous-menu de chaque hôte (`#harvester/<c>/dr` ; `#kvm/<h>/dr` dit
que la réplication des domaines KVM n'existe pas encore). Un contrat a sa
propre adresse, `#dr/<id>` : elle ouvre les contrats avec la fenêtre du
contrat dessus, un clic sur un contrat l'écrit, fermer la fenêtre rend
`#dr`, et F5 la rouvre. Depuis le Disaster Recovery d'un hôte, la fenêtre
s'ouvre sur la page de l'hôte, qui garde son adresse. Dans le journal d'un
contrat, l'icône Activité d'un événement mène à `#activity/action/<id>` :
l'Activité filtrée sur cette action, son détail ouvert. Ce filtre n'est pas
enregistré : vos propres filtres de l'Activité reviennent quand vous quittez
l'adresse ou changez un filtre. Dans **Machines
virtuelles**, une VM tenue par un contrat porte une pastille **DR** qui
mène au contrat ; elle est relue à chaque ouverture de la vue et chaque
minute. Les boutons suivent les actions que le serveur rend avec chaque
ligne de contrat (rôle, permis, raison) : **Reprendre la commande**
n'apparaît que lorsque la commande `resume` a quelque chose à finir, grisé
avec le rôle qu'il faut (admin pour finir une bascule). Chaque geste lance
la ligne de commande `harvester-replication` en action suivie (dock et Activité) ;
les écrans n'écrivent jamais par un autre chemin.

**Contrats.** Tous les contrats, en cartes ou en tableau (la bascule est
gardée par navigateur). Chaque contrat montre : son état (le pire de ses
VMs quand il est protégé), une jauge du RPO tenu face à l'objectif (le
repère est l'objectif ; la barre passe à l'orange au-delà de 80 % et au
rouge au-delà de l'objectif), le sens source -> secours avec un point par
cluster (vert : il répond au suiveur, rouge : il ne répond pas, gris : pas
encore lu), le nombre de VMs et leurs états, les VMs qui portent
l'étiquette d'une règle sans être encore protégées, le dernier événement et
le dernier transfert (données envoyées par le dernier passage, débit
d'envoi vers la cible de sauvegarde, débit d'application sur le secours, et
la quantité envoyée en 24 heures). Une recherche et un filtre d'état
resserrent la liste. Les alertes actives sont en tête. Le bouton
**Notifications** ouvre le réglage des notifications externes (salon
Matrix, webhook entrant Slack, courriel par SMTP ou webhook JSON générique ;
un administrateur les modifie, les secrets ne sont plus jamais montrés, une
valeur masquée laissée telle quelle garde le secret rangé ; chaque canal a
un bouton **Envoyer un test**).

**Fenêtre Nouveau contrat.** Cinq blocs sur une page, chacun dit s'il est
prêt, et rien n'est écrit avant **Créer** :
1. *Quoi et vers où* : nom, clusters source et de secours, VMs choisies
   dans une liste (celles qui ne se répliquent pas sont grisées avec la
   raison) ou par une règle d'étiquette `clé=valeur` dans un namespace.
   Avec une règle, **Protéger automatiquement les nouvelles VMs qui reçoivent l'étiquette**
   laisse la console protéger une VM qui reçoit l'étiquette plus tard
   (sinon une alerte vous attend).
2. *Objectif* : objectif de RPO (5, 15, 60 minutes ou toute valeur), points
   gardés et phase de mesure. **Mesurer d'abord** crée le contrat et lance
   la mesure ; **Protéger directement** crée et protège tout de suite et
   affiche un avertissement avec l'intervalle estimé, un RPO atteignable
   qui ignore le volume des changements, et la durée de la première copie
   complète d'après le débit connu du lien.
3. *Correspondances* : une ligne par réseau des VMs choisies. Un réseau
   absent du secours bloque la création tant qu'il n'est pas associé à un
   réseau existant ou créé là-bas par **Créer sur la cible** (nom, VLAN et
   réseau de cluster modifiables ; c'est la création de réseau habituelle,
   administrateur). Quand le secours n'a aucun réseau de VM, la liste dit
   **Aucun réseau de VM sur <cluster>** et la ligne mène à **Créer sur la
   cible**. Le réseau de gestion (pod) n'est pas proposé : le moteur
   n'associe un réseau de VM qu'à un autre réseau de VM du secours (une
   NetworkAttachmentDefinition), et `check` refuse tout le reste. La
   colonne **Réseau d'exercice** est gardée pour les
   exercices à venir. Le stockage se choisit **disque par disque** ; la
   classe de même nom est proposée quand elle existe sur le secours. Une
   classe qui demande plus de répliques que le secours n'a de nœuds
   planifiables est marquée *Dégradé* : la fenêtre la nomme, propose
   **Utiliser <classe>** pour une classe qui tient, ou **Accepter des
   volumes de secours dégradés** (un choix délibéré, dit dans la bulle
   d'aide) ; tant qu'aucun n'est fait, Créer reste verrouillé. Un namespace
   absent là-bas est associé à un autre ou créé à la bascule.
4. *Reprise* : vagues de démarrage (glisser les VMs d'une vague à l'autre,
   ou le menu de chaque VM ; chaque vague a une condition de passage : VMs
   en marche, agent invité connecté dans un délai, ou en marche puis un
   délai), cloud-init gardé ou rejoué avec une configuration réseau de
   secours par VM, adresses MAC gardées ou nouvelles, CPU et mémoire par VM
   sur le secours, VMs démarrées ou arrêtées après la bascule, et actions
   annexes HTTP (moment, méthode, URL, en-têtes, un en-tête secret masqué
   une fois gardé, corps, délai). Une action annexe s'essaie une fois le
   contrat créé. Une action renommée, ou dont l'adresse ou la méthode a
   changé, redemande son secret ; les autres en-têtes secrets posés en ligne
   de commande sont gardés.
5. *Résumé et vérifications préalables* : votre rôle, les clusters source et de
   secours lisibles avec vos droits, chaque correspondance présente, le
   débit mesuré du lien ; la place libre et la cible de sauvegarde sont
   contrôlées par le contrôle préalable de la protection. Puis ce qui
   bloque encore, la ligne de commande équivalente (sans secret) et le
   bouton de création.

**Fenêtre d'un contrat.** Ouverte depuis une carte ou une ligne, avec les
gestes que le contrat permet (un bouton grisé dit pourquoi : rôle, ou
sauvegardes tenues) : Vérifier, Mesurer, Arrêter la mesure, Protéger,
Pause, Reprendre le suivi, Reprendre la commande (après une interruption)
et « Bascule... ». Quatre onglets :
- *Vue d'ensemble* : RPO tenu, intervalle, prochain passage, passages en
  attente, écart, débits d'envoi et d'application, données envoyées en 24
  heures, une ligne par VM avec ses raisons, et les dernières bascules et
  déprotections ;
- *Graphiques* : RPO tenu face à l'objectif, débits d'envoi et
  d'application, et données envoyées par période, sur 24 heures par
  défaut, ou 1 heure, 7 jours, 30 jours (le survol montre les valeurs) ;
- *Journal* : chaque opération du contrat et de ses VMs, la plus récente
  d'abord, avertissements seuls à la demande, avec le lien vers l'action
  d'Activité ;
- *Réglages* : objectif, tolérance d'écart, suiveur et pause (à chaud),
  intervalle et rétention (appliqués en protégeant de nouveau), le bloc
  Reprise de la fenêtre de création (gardé pour la prochaine bascule ; un
  contrat protégé réécrit ses carnets à la prochaine protection), et la zone
  dangereuse (déprotéger, déprotéger et supprimer les sauvegardes, supprimer
  le contrat, chacun avec le nom du contrat tapé). Un changement enregistré
  propose **Annuler**, qui renvoie les valeurs d'avant dans une nouvelle
  action suivie ; la pause et sa levée aussi. Annuler renvoie les valeurs
  exactes (un objectif de 90 s revient à 90 s, pas arrondi aux minutes) ; un
  changement qui touche un secret d'action annexe ou une adresse masquée ne
  propose pas Annuler et dit pourquoi. Tant que les réglages de reprise sont
  plus récents que la dernière protection, la vue d'ensemble montre un
  bandeau qui les nomme, avec **Protéger de nouveau**.

**Fenêtre de bascule.** Réservée aux administrateurs : pour un autre rôle
elle s'ouvre en lecture et dit pourquoi. Elle liste les étapes du mode
choisi (planifiée : pause du suiveur, arrêt des VMs source, clôture,
sauvegarde finale, application, activation comme point de non-retour,
objets, vagues, actions annexes ; non planifiée : dernière lecture de la
cible de sauvegarde, rattrapage, points et écart, activation, objets,
vagues, actions annexes, clôture tardive), ce qui peut être perdu, et des
contrôles : rôle, état du contrat, joignabilité des deux clusters, et un
essai à blanc lancé à l'ouverture. Quand les réglages de reprise ont changé
depuis la dernière protection, la fenêtre les nomme et propose **Protéger
d'abord** (puis l'essai à blanc se relance) ou **Basculer avec les réglages
précédents** (choix explicite) ; le bouton reste verrouillé tant qu'aucun
n'est fait. Les VMs peuvent être restreintes, et le bouton se déverrouille
une fois le nom du contrat tapé.

**Liens.** Un bloc par paire de clusters ayant un contrat ou un essai : les
deux trajets source -> cible de sauvegarde -> secours avec le débit médian
des vraies sauvegardes sur 24 heures (le trajet le plus lent est marqué),
le dernier passage, une courbe de 24 heures par trajet, et le **test
actif** : un pod temporaire écrit des données aléatoires (256 Mio à 2 Gio)
vers la cible de sauvegarde NFS depuis la source et un pod les relit depuis
le secours, tous deux supprimés ensuite (opérateur). Une paire sans contrat
s'essaie avant d'en créer un.

**Disaster Recovery d'un cluster.** Les contrats où ce cluster est source
ou secours, avec un badge de son rôle et un filtre (tous, comme source,
comme secours), et la liste de ses VMs tenues par un contrat.

**Alertes.** Objectif dépassé, disque qui ne se sauvegarde jamais, VM
source rallumée après une bascule, sauvegarde en erreur, volume de secours
dégradé, source injoignable, spec d'une VM changée, contrat en erreur,
nouvelle VM qui répond à une règle, réglages de reprise plus récents que la
dernière protection, protection automatique en échec. Elles s'affichent en pastille sur les
entrées Disaster Recovery du menu (racine et cluster courant) et, depuis
la 2.0.0, sur la ligne de chaque hôte Harvester du menu latéral (une alerte
compte pour sa source et pour son secours ; la pastille mène au Disaster
Recovery de cet hôte), dans
le bloc **Événements** de la Vue d'ensemble de l'hôte (groupe Disaster Recovery,
même quand le cluster ne répond pas), et partent vers les notifications
réglées.

Pas dans cette version : les exercices de reprise (la colonne réseau
d'exercice est seulement gardée).

**Vérifié en réel** le 25/10/2026, de harvlab (Harvester 1.8.2) vers
harvlab2 (1.9.0, un nœud), par une console de cette version, avec un
récepteur HTTP local sur l'hôte de la console :
- actions annexes : les quatre sortes d'appel (POST avec un en-tête secret
  et un corps, PUT, GET) ont atteint le récepteur à `start`, après la vague
  1 et à `end` d'une vraie bascule ; une action qui répond par une
  redirection a échoué en le disant et la cible de la redirection n'a rien
  reçu ; une adresse de bouclage, de métadonnées ou privée a été refusée à
  l'enregistrement (`hook-address`) hors de la liste autorisée ; un secret
  masqué envoyé avec un autre hôte a été refusé (`secret-missing`) ; la
  requête de l'adresse n'est apparue ni dans les sorties, ni au journal, ni
  dans `actions.db` ;
- notifications : un canal webhook a reçu le message d'essai et les
  messages du suiveur lui-même (`selector-new-vm`, `auto-protect-failed`) ;
  un canal qui redirige a échoué une fois, sans nouvel essai ; un serveur
  SMTP en bouclage et un mot de passe sans TLS ont été refusés à
  l'enregistrement ;
- protection automatique : une VM nouvellement étiquetée a été protégée par
  la console d'elle-même ; une VM étiquetée dont la classe de disque manque
  sur la cible a échoué, attendu 10 puis 20 minutes, avec un seul événement
  au journal et une alerte ; retirer l'étiquette a clos la série ;
- reprise : la vague 1 a attendu l'agent invité (openSUSE Leap 15.6 avec
  qemu-guest-agent, 22 s), CPU et mémoire valaient 2 vCPU / 2 Gio au
  secours (topologie relue dans l'invité), la configuration réseau a été
  appliquée par le cloud-init rejoué (l'adresse ajoutée vue dans l'invité
  par l'agent) ;
- répliques du secours : la fenêtre de création a marqué
  `harvester-longhorn` (3 répliques, un nœud) et **Utiliser harv-rep1** l'a
  corrigé ;
- réglages en retard : une taille changée sur le plan protégé a bloqué
  l'essai à blanc de la bascule (`carnet-stale`, clés et VM nommées),
  l'accord l'a changé en avertissement, protect l'a effacé ; le bandeau et
  la fenêtre de bascule l'ont montré ;
- essai de lien : 64 Mio à 97 / 103 Mio/s, et le répertoire de l'essai
  avait disparu du partage ensuite.

Non vérifié sur un cluster : les notifications vers un vrai salon Matrix,
Slack ou un serveur de courriel (testées sur des serveurs locaux), l'ajout
à chaud de CPU et de mémoire à la reprise, la reprise d'une bascule
interrompue après l'accord sur des réglages en retard, et le
glisser-déposer à la vraie souris (tests de bout en bout seulement). Les
corrections de la seconde revue (marque de retard jugée sur les valeurs,
masquage du chemin des adresses, secret rangé gardé pour la même requête
seulement, `--accept-degraded-standby` et les drapeaux de `resume`) sont
couvertes par des tests unitaires et de bout en bout, pas jouées sur un
cluster.

## Ce qui nécessite quoi

| Vous voulez… | Il vous faut |
|---|---|
| Juste un shutdown/startup sûr | Les scripts CLI seuls (pas d'UI, pas de podman) |
| Un dashboard + gestion VM | La console web |
| Provisionner des clusters downstream | Console web + un bundle airgap CAPHV |
| Des VMs gérées par Terraform | Console web + le binaire du provider Terraform |
| Un fonctionnement 100 % hors-ligne | Le tarball : wheels bundlés + image OCI |
| Des hôtes KVM et leurs VMs | La console web ou `bin/harvester-kvm.py`, et un accès SSH par clé à un compte du groupe `libvirt` de chaque hôte |
| Des copies de secours de VMs sur un second cluster | `bin/harvester-replication.py` (ou les routes de la console), deux clusters Harvester qui partagent une cible de sauvegarde |

Voir [architecture.md](architecture.md) pour la façon dont les surfaces se
posent sur le moteur partagé, et [installation.md](installation.md) pour
démarrer.
