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
