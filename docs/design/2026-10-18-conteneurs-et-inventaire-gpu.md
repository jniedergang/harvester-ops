# Section « Containers » et inventaire GPU (1.91.0, 1.92.0)

Date : 18/10/2026. Décisions de ju du 16 au 18/10/2026 : faire les deux
chantiers, la partie GPU étant théorique faute de GPU sur nos bancs, mais
l'expérience et l'interface sont travaillées dès maintenant ; on met en place
sur les recommandations, on ajuste ensuite.

## Pourquoi

- Harvester 1.9.1 passe en GA les conteneurs bare-metal : des charges
  Kubernetes ordinaires déployées directement sur le cluster Harvester, à côté
  des VM (harvester/harvester#5820, expérimental depuis Harvester 1.2 avec le
  drapeau Rancher `harvester-baremetal-container-workload`). harvester-ops
  doit les montrer comme il montre les VM.
- La réservation de GPU (besoin client) commence par savoir quels GPU, vGPU et
  instances MIG existent, et qui les utilise, VM comme conteneurs.

Règles amont reprises : aucune charge dans `cattle-system`,
`harvester-system`, `longhorn-system` ; conteneurs et VM dans des namespaces
séparés ; pas de vGPU pour les conteneurs avant Harvester 1.10.0 (limite
connue, commentaire 4697917218 de harvester/harvester#5321).

## Chantier 1 : section « Containers » (1.91.0)

### Menu

Un groupe « Containers », juste sous « Virtual Machines », sur le même modèle :

- **Current cluster's containers** (onglet `containers`) : les charges du
  cluster choisi en haut ;
- **All clusters containers** (onglet `allcontainers`) : les charges de tous
  les clusters déclarés, avec une colonne Cluster, comme « All clusters VMs ».

### Ce qui est une charge de conteneurs

Lu en une passe par cluster (Deployments, StatefulSets, DaemonSets, Jobs,
CronJobs, Pods), dans les namespaces qui ne sont pas des namespaces système :

- exclus par nom : `kube-*`, `cattle-*`, `harvester-*`, `longhorn-system`,
  `fleet-*`, `cdi`, `local`, `default` n'est PAS exclu ;
- exclus par nature : les pods des VM (`kubevirt.io=virt-launcher`) et les
  pods des imports et transferts (CDI, `cdi.kubevirt.io` en propriétaire) ;
- un pod qui appartient à une charge (ownerReferences) est rangé sous elle ;
  un pod seul est une charge à lui.

Une liste unique, constante partagée, `SYSTEM_NAMESPACES` côté serveur, avec
un test.

### Vue

Une ligne par charge : nom, type, namespace, cluster (vue de tous les
clusters), prêt / voulu, images, GPU demandés (somme des `nvidia.com/*` et
`amd.com/gpu` des conteneurs), âge, état (`Running`, `Progressing`,
`Degraded`, `Failed`, `Completed`, `Suspended`). On déplie une ligne pour voir
ses pods : nœud, état, redémarrages, âge. Menu de la ligne : YAML (lecture) et
journaux d'un pod (les 500 dernières lignes, conteneur au choix).

Filtres et tri comme la vue de toutes les VM : texte, cluster, namespace,
type, état. Un bandeau dit, sans alarmer : la fonction est expérimentale avant
Harvester 1.9.1 ; les namespaces système sont masqués ; les conteneurs vont
dans d'autres namespaces que les VM.

Un cluster éteint, injoignable ou qui refuse la lecture est dit (même
mécanisme que les VM), jamais une liste vide muette.

### Hors périmètre de 1.91.0 (à ajuster ensuite)

Créer, modifier, mettre à l'échelle, redémarrer ou supprimer une charge ;
clusters virtuels k3k ; Fleet. La lecture vient d'abord, les gestes suivront,
chacun comme action suivie et par un script de `bin/` (parité CLI).

### API

- `GET /api/containers/<cluster>` : `{workloads: [...], denials}` ;
- `GET /api/containers-all` : `{clusters: [{cluster, state, count}], workloads}`,
  lecture parallèle avec l'identité de la personne, comme `/api/vms-all` ;
- `GET /api/containers/<cluster>/<ns>/pod/<name>/logs?container=&tail=` ;
- YAML : la route générique existante si elle couvre ces types, sinon une
  route de lecture dédiée.

Le modèle d'une charge vit dans `bin/lib/hv_containers.py` (fonctions pures,
testées sans cluster) ; `bin/harvester-containers.py list` donne la même
lecture en ligne de commande (`--json`).

## Chantier 2 : inventaire GPU (1.92.0)

### Unités

Une unité est ce qu'on pourra réserver plus tard :

| Unité | Source Harvester | Exemple |
|---|---|---|
| GPU entier | `PCIDevice` de classe 0300/0302, vendeur NVIDIA ou AMD | H100 en passthrough |
| GPU SR-IOV et ses vGPU | `SRIOVGPUDevice`, `VGPUDevice` (`vGPUTypeName`) | `NVIDIA_A2-4Q` |
| Instance MIG | `MIGConfiguration` (profils demandés et disponibles) | `3g.40gb` |
| Ressource GPU d'un nœud | `status.allocatable` (`nvidia.com/*`, `amd.com/gpu`) | conteneurs |

Pour chaque unité : cluster, nœud, type, parent (le GPU physique d'un vGPU ou
d'une instance MIG), état (`libre`, `utilisée`, `désactivée`), et le
consommateur : une VM (`devices.gpus`, `devices.hostDevices`) ou un pod
(requêtes `nvidia.com/*`). Le consommateur est générique : `{kind: vm|pod,
cluster, namespace, name}`, pour que la réservation compte VM et conteneurs de
la même façon.

### Vue

Un sous-onglet « GPU » dans Cluster > Advanced, à côté de « PCI Devices » et
« USB Devices ». Il porte un sélecteur « Ce cluster / Tous les clusters » (la
vue de toute la flotte est le point de départ de la réservation). En tête,
une carte par type d'unité : total, utilisées, libres. Dessous, le tableau
des unités, filtrable par cluster, nœud, type, état ; le consommateur est un
lien vers la VM ou la charge de conteneurs.

Un cluster sans GPU dit « aucun GPU sur ce cluster », pas une vue vide.

### Vérification

Faute de GPU vGPU ou MIG sur nos bancs, l'inventaire est vérifié sur des
objets réels de Harvester relevés dans sa documentation et sa source (CRD de
pcidevices-controller), reproduits en fixtures de tests. Ce qui n'est pas
vérifié en réel est dit dans le CHANGELOG et la documentation. Le seul passage
réel possible : un cluster sans GPU (vue « aucun GPU »), et les PCI émulés du
banc harvlab (`harvlab.sh pci`) pour un GPU entier simulé si un périphérique
de classe 0300 y figure.

## Tests

- `hv_containers.py`, `hv_gpu.py` : fonctions pures, cas en tableau, cas à
  plusieurs éléments, chemins d'erreur (objets incomplets, refus RBAC).
- Routes : client de test Flask, refus dits, rien de muet.
- e2e : la navigation, les filtres, le dépliage, les journaux, sur la console
  de test avec API simulée.
- Réel : harv1, en déployant une charge de conteneurs de test dans un
  namespace dédié, puis en la supprimant.

## Suite décidée le 18/10/2026, après une première évaluation

ju ne pouvait rien évaluer en lecture seule : aucun conteneur à créer, aucun
GPU à réserver. Décisions :

### 1.93.0 : section « Shared resources » et gestes des Containers

- **Shared resources**, section de premier niveau du menu, après Containers.
  Elle n'appartient ni aux VM ni aux conteneurs : les GPU d'un nœud servent
  les deux (passthrough, vGPU, vGPU adossé à MIG pour les VM ; GPU entier
  pour les conteneurs, vGPU à partir de Harvester 1.10). Entrées : **GPU**
  (l'inventaire de 1.92.0, déplacé, vue de la flotte avec filtre par cluster),
  **Bookings** (1.94.0), plus tard **Usage** (décompte, quotas).
- L'onglet Advanced > GPU est retiré ; Advanced ne gardera que les gestes de
  parité avec Harvester (activer un vGPU, régler le MIG) quand ils viendront.
- **Gestes des Containers**, chacun une action suivie (ActionRun, dock,
  Activity) et une commande de `bin/harvester-containers.py` (parité CLI) :
  créer une charge (formulaire : nom, namespace, type Deployment / Job, image,
  nombre de pods, commande, variables, GPU demandés, limites CPU et mémoire ;
  ou par YAML), mettre à l'échelle, redémarrer (rollout restart), supprimer.
  Refus dans les namespaces système ; écritures avec l'identité de la
  personne ; contrôle à blanc du serveur avant d'écrire.

### 1.94.0 : Bookings, version 1

- Réserver une ou plusieurs unités (GPU entier, vGPU d'un type, instances MIG
  d'un profil, GPU de conteneurs) sur un cluster, pour une personne ou un
  groupe, sur un créneau (début, fin), avec une cible facultative (VM ou
  charge de conteneurs) et une note.
- Conflits refusés à la création (même unité, créneaux qui se chevauchent,
  ou plus d'unités d'un type que le cluster n'en a). Calendrier par unité et
  par semaine, « mes réservations », annuler.
- Stockage SQLite de la console (comme les notes), sans écriture sur les
  clusters : l'application réelle (attacher, quotas, arrêt en fin de créneau)
  vient plus tard et n'est pas promise par la version 1.
- Dans les formulaires de VM (GPU, vGPU) et de création de charge (GPU
  demandés) : disponibilité, réservations en cours, et lien vers l'unité.

Choix de la réalisation (livrée en 1.94.0) :

- Modèle pur `bin/lib/hv_bookings.py` (validation, capacité, conflits par
  balayage, états, droits), registre `bin/lib/bookings_db.py`
  (`bookings.db` à côté de `notes.db`, WAL), ligne de commande
  `bin/harvester-bookings.py` qui seule écrit.
- La console lit l'inventaire GPU avec l'identité de la personne et le passe
  au script dans la demande ; le script refait le contrôle des conflits dans
  sa transaction d'écriture (`BEGIN IMMEDIATE`). Seul, il lit le cluster.
- Unité précise pour les GPU entiers et les vGPU ; MIG et GPU de conteneurs
  par nombre. Une unité précise compte dans la capacité de son modèle.
- Capacité MIG = instances CRÉÉES du profil (pas le maximum possible : on ne
  donne pas une instance qui n'existe pas). Unités désactivées et nœuds
  cordonnés hors capacité.
- Créneaux semi-ouverts, 90 jours au plus, pas déjà passés.
- Limite connue : un vGPU adossé à MIG et son instance MIG sont comptés à
  part.

### Banc : cluster de démonstration simulé avec des GPU

Un cluster kwok (`tests/bench/kwok`) portant les CRD de pcidevices et des
objets GPU d'exemple (GPU entiers, vGPU, MIG) et des nœuds qui annoncent
`nvidia.com/gpu` : l'inventaire et la réservation s'évaluent dans la vraie
interface sans matériel. Déclaré dans la console de dev seulement.

## 1.95.0 : Usage (décompte et quotas), décidé le 19/10/2026

Troisième entrée de « Shared resources », après GPU et Bookings. Faisable sans
matériel : tout part de l'inventaire et des réservations.

### Deux mesures, côte à côte

- **Réservé** : les GPU-heures des réservations (nombre d'unités x durée),
  par personne et par groupe, sur une période (cette semaine, ce mois, le
  mois précédent, une plage au choix). Calculé depuis `bookings.db`, les
  réservations annulées ne comptent qu'à partir de leur création jusqu'à
  leur annulation si le créneau avait commencé.
- **Consommé** : les GPU-heures réellement tenues, relevées par la console.
  Un échantillonneur côté serveur lit l'inventaire de chaque cluster toutes
  les 5 minutes (réglable, `HARVESTER_OPS_USAGE_INTERVAL`) et note, pour
  chaque unité utilisée, son consommateur (VM ou pod, namespace) dans
  `usage.db`. Les heures se comptent par intervalle. Le consommateur est
  rattaché à une personne ou un groupe par la réservation qui le cible
  (cible de la réservation = la VM ou la charge), sinon par son namespace
  (« sans réservation »).
- L'écart entre les deux est montré : réservé et pas utilisé (gaspillage),
  utilisé sans réservation (hors règle).

### Quotas

- Un quota par personne ou par groupe : un plafond de GPU-heures réservées
  par mois et, facultatif, un nombre maximal d'unités tenues en même temps.
  Réglés par un administrateur dans la vue Usage, gardés dans `bookings.db`.
- Appliqués à la création d'une réservation : refusée au-delà du quota, avec
  la raison (même chemin que les conflits, script `harvester-bookings.py`).
  Rien n'est appliqué sur les clusters.

### Vue

Un tableau par détenteur : réservé, consommé, quota et reste, barre de
progression, et le détail par cluster et par type d'unité ; un graphique des
GPU-heures par jour sur la période. Export CSV.

### CLI

`bin/harvester-bookings.py usage [--from --to] [--json]` et
`bin/harvester-bookings.py quota set|list|delete`.

### Vérification

Unitaires (calcul des heures aux bornes, annulations, rattachement par cible
et par namespace, quotas). Réel : l'échantillonneur sur `gpudemo` relève les
unités tenues par `prod/train`, `ml/notebook`, `ml/big` et `ml/trainer`.

### Choix de la réalisation (livrée en 1.95.0)

- Modèle pur `bin/lib/hv_usage.py`, registre des relevés
  `bin/lib/usage_db.py` (tables `samples` et `runs`, WAL, 400 jours) ;
  quotas dans une nouvelle table `quotas` de `bookings.db` (migration
  additive).
- Un relevé vaut l'écart avec le relevé précédent du même consommateur sur
  la même unité, au plus deux intervalles ; un trou plus long n'est pas
  comblé. L'intervalle est rangé avec chaque relevé : changer le réglage ne
  fausse pas l'historique.
- Rattachement par la cible seulement (la VM, ou la charge du pod, retrouvée
  par ses propriétaires : un ReplicaSet `<deployment>-<hash>` remonte au
  Deployment). Entre plusieurs réservations, celle du même type d'unité,
  puis la plus ancienne.
- Gaspillage compté réservation par réservation, sur la partie déjà passée.
  Hors règle : l'usage sans réservation, et l'usage au-delà du nombre
  d'unités réservées (plafonné par réservation et par tour).
- Quota mensuel par mois calendaire UTC, sur les heures RÉSERVÉES (une
  réservation annulée compte ce qui a été tenu) ; unités simultanées sur
  tous les clusters, réservations non annulées. Le quota est celui du
  titulaire, pas de la personne qui réserve.
- Une instance MIG est tenue par un vGPU adossé à MIG : son usage est relevé
  comme ce vGPU (les unités MIG ne sont pas relevées, pour ne pas compter
  deux fois).
- L'échantillonneur lit avec le kubeconfig déclaré de la console, sans
  identité de personne, et ne tourne que dans le processus principal.
