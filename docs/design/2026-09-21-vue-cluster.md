# Vue Cluster : hôtes, VMs et maintenance des nœuds

Conception validée le 21/09/2026. Livraison prévue : v1.43.0.

## Contexte

La vue Cluster de l'Aperçu est le dernier graphe Cytoscape de la console :
des boîtes de VM de 130 x 50 pixels portant le seul nom. L'exploitant veut
voir chaque VM avec ses vCPU, sa mémoire, ses disques et ses réseaux, et le
détail complet au survol quand la place manque. Les trois autres vues
(Fabrique, Réseau, Stockage) sont passées en blocs HTML et ont été
validées ; celle-ci suit.

Au passage : les boutons « cordon » et « drain » du panneau d'un nœud
échouent tous deux (« not yet implemented », rien côté serveur). Décision de
l'exploitant : les réparer tous les deux dans ce chantier.

## Données

`/api/topology/<cluster>` reste le point d'accès, toujours un seul appel
kubectl groupé (`TOPOLOGY_KINDS`), auquel s'ajoutent les
`persistentvolumeclaims` (taille des disques) et les `replicas.longhorn.io`
(règle de la dernière réplique saine, pour la maintenance).

Relevé sur harv1 (Harvester 1.8) :
- CPU : `domain.cpu` (`cores`, `sockets`, `threads`) ; vCPU = produit.
- Mémoire : `domain.memory.guest` (`4Gi`), égale à `resources.limits.memory`.
  Les `requests` (2730Mi) sont une réservation surallouée, pas la taille.
- Disques : `domain.devices.disks` + `volumes` (PVC ou `cloudInitNoCloud`) ;
  taille lue sur le PVC.
- Système invité : `vmi.status.guestOSInfo.prettyName` (agent requis).
- Nœud : `allocatable.cpu` en millicœurs (`7020m`), mémoire en `Ki`.

### Par VM (ajouts au réducteur `_topology_vm`)
`vcpu`, `memory` (octets), `disks` (nom, type, amorçage, PVC, taille,
storage class), `disk_total`, `nics` (même forme que la Fabrique : réseau,
MAC, adresses, interface invitée, état du lien), `guest_os`.

### Par hôte
`cpu_allocatable` (cœurs), `memory_allocatable` (octets), `vcpu_allocated`
et `memory_allocated` (somme des VMs en marche sur l'hôte), `schedulable`,
`maintenance` (`null`, `requested`, `running`, `completed`, d'après les
annotations de Harvester).

## L'écran

Module `web/static/js/cluster-map.js`, même grammaire que les trois autres
vues (`board.js`).

- **Un bloc par hôte** : nom, état (prêt, isolé, en maintenance), rôles,
  adresse, et deux jauges « vCPU alloués / disponibles » et « mémoire
  allouée / disponible ». Au-delà de 100 % la jauge le montre
  (surallocation).
- **Les VMs en cartes**, en grille adaptée à la largeur : pastille d'état,
  nom, « 2 vCPU · 4 Gio », disques (« 20 Gio » ou « 2 disques · 60 Gio »),
  réseaux, première adresse. Ce qui ne tient pas est tronqué.
- **Calque au survol ou au focus clavier** : le détail complet (chaque
  disque avec taille, classe et amorçage ; chaque carte réseau avec MAC et
  toutes ses adresses ; système invité ; stratégie de démarrage).
- **Bloc « Arrêtées / non planifiées »**, grisé.
- **Barre d'outils** : filtre (nom, adresse, réseau), rafraîchir, verrou
  des actions destructives.
- **Au clic sur une VM** : le panneau d'actions existant (notes, éditer,
  console, snapshots, migrer, démarrer / arrêter, supprimer derrière le
  verrou).
- **Au clic sur un hôte** : notes, isoler / réintégrer, entrer / sortir du
  mode maintenance.

## Isoler, réintégrer

`spec.unschedulable` du nœud, comme l'action de Harvester. Action tracée,
réversible, sans verrou destructif mais avec confirmation.

Relevé en réel sur harv1 : le webhook de Harvester
(`pkg/webhook/resources/node/validator.go`, `validateCordonAndMaintenanceMode`)
refuse d'isoler ou de mettre en maintenance le **dernier nœud disponible**
(aucun autre nœud ni isolé ni porteur de `maintain-status`). La console
applique cette règle en amont : bouton désactivé avec la raison, 409 côté
serveur, refus « last-available-node » au contrôle de maintenance.

## Mode maintenance (le « drain » de Harvester)

Mécanique relevée dans le code de Harvester v1.8.0
(`pkg/api/node/formatter.go`, `pkg/util/drainhelper/helper.go`,
`pkg/controller/master/nodedrain/nodedrain_controller.go`) :

- **Entrer** : poser l'annotation `harvesterhci.io/drain-requested=true`
  (et `harvesterhci.io/drain-forced=true` pour forcer). Le contrôleur de
  Harvester isole le nœud, migre les VMs, puis pose
  `harvesterhci.io/maintain-status` à `running` puis `completed`. Il refait
  lui-même la vérification quand l'annotation est posée directement, et
  retire les annotations s'il refuse.
- **Sortir** : `spec.unschedulable=false`, retrait du taint
  `kubevirt.io/drain` et des trois annotations, puis redémarrage des VMs
  arrêtées par la maintenance (label `harvesterhci.io/maintain-mode-strategy`
  et annotation du nom de nœud).

**Contrôle préalable** (mêmes règles que Harvester, affichées avant de
confirmer) :
- nœud de plan de contrôle ou etcd : refus si le cluster n'en a qu'un ; en
  haute disponibilité, refus si les 3 ne sont pas tous hors maintenance ;
- VMs non migrables : condition `LiveMigratable=False` du VMI, avec sa
  raison ;
- VMs dont un volume a sa dernière réplique saine sur ce nœud ;
- VMs marquées pour être arrêtées pendant la maintenance.

Sans forçage, s'il existe des VMs non migrables, la console refuse comme
Harvester le ferait. Le forçage (case explicite, derrière le verrou
destructif) arrête ces VMs.

**Suivi** : action tracée qui observe les annotations et le nombre de VMs
restant sur le nœud, jusqu'à `completed` (10 minutes au plus), et qui
rapporte un refus du contrôleur (annotations retirées sans `maintain-status`).

**Vérification réelle** : sur harv1 (un seul nœud) seuls les refus sont
vérifiables. Le 22/09/2026, sur le banc à trois nœuds `harvlab`
(`tests/bench/harvlab`), tout a été exercé : isoler, réintégrer, le refus du
dernier nœud disponible, la maintenance avec et sans forçage, le refus pour
plan de contrôle occupé, la sortie.

Ce que le réel a appris et que le contrôle préalable dit désormais :
- le drain n'**arrête** pas une VM seulement quand elle est non migrable :
  toute VM sans stratégie d'éviction par migration (`LiveMigrate`,
  `LiveMigrateIfPossible`, ou défaut du cluster) est arrêtée, puis relancée
  ailleurs si sa stratégie de démarrage est `Always` ;
- en forçant, Harvester n'arrête que les VMs non migrables, et elles restent
  arrêtées ; l'étiquette « arrêter pendant la maintenance » n'est honorée
  que sans forçage ;
- Longhorn annule la migration d'un volume dont une réplique attend sa
  reconstruction : la maintenance piétine jusqu'à la fin de celle-ci.

## Ménage

La vue Cluster était le dernier usage de Cytoscape : `topology.js` et
`web/static/vendor/cytoscape/` (552 Ko) sont retirés, avec les tests qui
portaient sur le canevas.

## Tests

- Réducteurs : vCPU (produit, défauts, repli sur `resources`), mémoire
  (guest, limits, requests en dernier recours), disques et tailles, cartes
  réseau, système invité ; sommes par hôte, surallocation, état de
  maintenance.
- Contrôle préalable de maintenance : plan de contrôle unique, HA à 3,
  VM non migrable, dernière réplique, VMs marquées ; forçage.
- Points d'accès : isoler / réintégrer, entrer / sortir, refus quand
  l'état a changé, actions tracées.
- Navigateur : blocs, jauges, cartes, calque au survol, filtre, panneau
  d'actions, bulles d'aide, français.
- Réel sur harv1 : captures ; éditeur et console ouverts depuis une carte ;
  refus d'isoler le dernier nœud (webhook de Harvester, constaté) ; refus
  du mode maintenance (plan de contrôle unique).
