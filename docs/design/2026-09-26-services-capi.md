# Déployer des services sur les clusters créés par Cluster API (B2)

État : livré en v1.52.0, essayé en réel sur harv1 le 26/09/2026. Sous-projet B du plan bare-metal : après la
création de clusters RKE2 (B1, v1.48.0), y déployer des services. Demande de
l'exploitant : « on enchaîne sur B2 ». Le plan d'origine citait MarineNat
(la démo navale, déjà tournante sur K3s sous Rancher) et des services
d'infrastructure (DNS, DHCP, NTP).

## Le mécanisme : Cluster API, par son fournisseur d'add-ons Helm

Cluster API a un fournisseur d'add-ons officiel, **CAAPH**
(cluster-api-addon-provider-helm, v0.6.4, contrat v1beta2 comme le cœur
v1.13 de Harvester 1.9). Turtles l'installe comme les autres fournisseurs
(`CAPIProvider`, type `addon`, nom `helm`), depuis le paquet airgap ; ses
certificats de webhook sont pris en charge par Turtles, comme pour CAPHV.

- Un **service** est un `HelmChartProxy` sur le cluster de gestion : un
  chart, sa version, ses valeurs (gabarits Go acceptés, par exemple le nom
  du cluster), et un sélecteur de clusters.
- CAAPH installe le chart (une `HelmReleaseProxy` par cluster retenu), le
  met à jour quand le service change, et le désinstalle d'un cluster qui ne
  correspond plus.
- **Viser un cluster** : la console pose sur le `Cluster` l'étiquette
  `harvester-ops.io/svc-<service>: "on"`, que sélectionne le service. Retirer
  un service d'un cluster, c'est retirer l'étiquette.

C'est déclaratif, réconcilié en continu, et ça reste lisible sans la console
(`kubectl get helmchartproxies,helmreleaseproxies`).

## Première étape

- **CAAPH dans le paquet et à l'installation** (onglet Installation), comme
  fournisseur facultatif : son absence n'empêche pas de créer un cluster.
- **Onglet Services** (Automatisation, Cluster API) : un catalogue de
  services prêts à l'emploi, plus « chart libre » (dépôt, chart, version,
  valeurs) ; par service, les clusters où il tourne et son état (installé,
  révision, erreur).
- **Catalogue de départ** :
  - **DNS** : CoreDNS en serveur (redirecteurs, zone locale), exposé par un
    service LoadBalancer qui prend une adresse du pool Harvester (le
    fournisseur de cloud Harvester est déjà dans chaque cluster créé) ;
  - **podinfo** : une application témoin, pour vérifier la chaîne ;
  - **MarineNat** : à ajouter dès qu'un chart existe (étape suivante, avec
    l'exploitant).
- Parité CLI : `harvester-capi services list | deploy | remove`, action
  suivie dans le dock.

## Hors de cette étape, dit comme tel

- **Airgap des charts** : CAAPH télécharge les charts depuis le cluster de
  gestion ; un dépôt Helm servi par la console depuis le paquet viendra
  ensuite. Les images des charts restent à tirer par les clusters créés
  (Internet ou miroir de registre).
- **DHCP et NTP** : un serveur DHCP doit être sur le réseau des VMs, donc
  dans le réseau de l'hôte d'un nœud ou par Multus ; NTP n'a pas de chart
  officiel. À concevoir à part.

## kube-vip : sans lui, aucun service n'a d'adresse

Le fournisseur de cloud Harvester que CAPHV pose dans chaque cluster créé
(v0.2.5, en manifestes par un ClusterResourceSet) confie l'annonce de
l'adresse d'un service LoadBalancer à kube-vip
(`kube-vip.io/loadbalancerIPs: 0.0.0.0` en mode DHCP). CAPHV ne pose pas
kube-vip : l'adresse reste « pending », Helm attend dix minutes et la
release échoue. Le chart `harvester-cloud-provider` qu'emploie Rancher, lui,
l'embarque.

La console pose donc kube-vip elle-même, rendu du sous-chart kube-vip de
`harvester-cloud-provider` 0.2.15 avec ses valeurs
(`bin/lib/kube-vip.yaml`, image `rancher/mirrored-kube-vip-kube-vip-iptables:v1.2.3`,
sur les nœuds du plan de contrôle), par un ClusterResourceSet
`crs-kube-vip` au même sélecteur que le fournisseur de cloud
(`ccm: external`) :
- dans chaque cluster créé à partir de la v1.52.0 ;
- dans un cluster plus ancien, au premier service qu'on y déploie (le
  contrôle préalable l'annonce).

## Essai réel (harv1, 26/09/2026)

- CAAPH installé depuis l'onglet Installation, par Turtles, image chargée
  sur le nœud depuis le paquet : `addon/helm` Ready.
- Cluster `svc-essai` créé depuis le formulaire (Leap 15.6, un plan de
  contrôle, un worker) : disponible en 16 min.
- **podinfo** déployé depuis l'onglet Services : pod en marche, mais adresse
  « pending » et release en échec après dix minutes, d'où kube-vip. Une fois
  kube-vip posé, CAAPH a refait la release (révision 2) et podinfo répondait
  sur 172.16.11.225 (bail DHCP du réseau) avec le message rendu par le
  gabarit : « Déployé par harvester-ops sur svc-essai ».
- kube-vip retiré à la main, puis **CoreDNS** déployé depuis l'onglet : le
  contrôle préalable a annoncé kube-vip, la console l'a posé
  (`crs-kube-vip`), CoreDNS installé en 27 s sur 172.16.11.71. Réponses :
  enregistrements locaux, noms internes par Pi-hole, noms d'Internet.
- **Mise à jour** (ajout de TCP au DNS) : l'action finissait en 6 s sur
  l'ancienne release, encore prête. Corrigé : on attend que CAAPH ait lu la
  nouvelle version du service puis refait la release (générations
  observées) ; revu en réel, révision 2 attendue.
- Ajouter TCP 53 à côté d'UDP 53 dans une release existante ne passe pas :
  Kubernetes fusionne les ports par numéro. Une installation neuve a les
  deux ; le catalogue les pose dès le départ (`use_tcp`).
- **Retrait** depuis l'onglet : releases désinstallées du cluster, services
  supprimés, étiquettes ôtées. Réinstallation : DNS en UDP et en TCP sur
  172.16.10.200.
- Cluster supprimé depuis l'onglet Clusters K8S.

## Limites constatées, à remonter

- **Objets LoadBalancer laissés sur Harvester en mode DHCP** : quand un
  service du cluster créé disparaît, le fournisseur de cloud journalise
  « Deleted load balancer » mais l'objet reste (reproduit avec v0.2.5 et
  v0.2.8, pas en mode pool). En DHCP il ne tient aucune adresse : c'est du
  désordre, pas une fuite.
- **Nom de cluster** : CAPHV lance le fournisseur de cloud sans
  `--cluster-name` ; tous les objets LoadBalancer des clusters créés portent
  `cluster: kubernetes` (le fournisseur l'écrit lui-même en avertissement),
  ce qui empêche de les rattacher à leur cluster, et donc de les nettoyer à
  la suppression d'un cluster (en mode pool, les adresses resteraient
  prises).
- Les charts et leurs images viennent d'Internet (voir plus haut).
