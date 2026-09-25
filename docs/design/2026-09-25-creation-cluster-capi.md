# Créer un cluster RKE2 depuis la console (B1)

État : livré en 1.48.0, 25/09/2026. Demande de l'exploitant : « améliorer
l'ergonomie et l'expérience utilisateur de cette page [Automatisation,
Création de cluster]. Un maximum d'assistance par choix multiple et
d'explication quand on survole les éléments. Les éléments essentiels
regroupés, les calculs IP suggérés. Vérifier que toutes les options du
provider sont proposées, sans assommer l'utilisateur (masquer les options
étendues). » Puis : « on fait B1 en premier ».

## Ce que l'état des lieux a montré

Avant l'ergonomie, la page ne pouvait rien créer :

- **Le générateur manquait.** La console appelle `caphv-generate`, un script
  bash du dépôt CAPHV, ni installé ni empaqueté : chaque création répondait
  412.
- **La pile Cluster API de harv1 était cassée.** Installée il y a 120 jours
  par le paquet de la console (cœur CAPI v1.10.4, fournisseurs RKE2 v0.16.1,
  CAPHV v0.2.8, cert-manager), elle a été doublée lors du passage à
  Harvester 1.9 : **Harvester 1.9 embarque Rancher Turtles, qui installe son
  propre cœur CAPI (v1.13.3, `cattle-capi-system`)**. L'ancien cœur n'avait
  plus de droits, cert-manager avait disparu, et les certificats de
  webhook des fournisseurs avaient expiré le 25/08/2026.
- **Les versions ne se parlaient plus** : le générateur émet des gabarits
  RKE2 en `v1beta2`, que les anciens fournisseurs ne servaient pas ; la
  console attendait `status.controlPlaneReady` (v1beta1) alors que le cœur
  répond en v1beta2 (`status.initialization.*`), donc toute création aurait
  fini sur un délai dépassé.
- **Le formulaire** : valeurs par défaut invalides sur harv1 (réseau
  `default/untagged` inexistant, passerelle 10.0.0.1), aucun choix proposé
  alors que l'inventaire existe côté serveur (fonction jamais appelée),
  aperçu YAML qui n'affiche pas le YAML, résultat effacé 4 s après la
  création.
- **Sécurité** : les clés du formulaire devenaient des options du script sans
  liste blanche (`apply: true` lui aurait fait appliquer avec le kubeconfig
  du service) ; le kubeconfig d'un cluster créé se téléchargeait avec le
  rôle lecture seule.

## Décisions

### Le cluster de gestion : Turtles, là où il est

Le projet CAPHV le dit dans sa matrice de compatibilité : Rancher pilote la
version du cœur CAPI par Turtles, et CAPHV s'installe par un
`CAPIProvider`. Harvester 1.9 embarquant Turtles, **le cluster Harvester
lui-même sert de cluster de gestion**, comme la console le faisait déjà.

- La console n'installe **plus de cœur CAPI ni cert-manager** quand Turtles
  est présent. Elle déclare les fournisseurs manquants (RKE2 amorçage, RKE2
  plan de contrôle, CAPHV) en objets `CAPIProvider`.
- **Airgap** : les composants viennent de ConfigMaps créées par la console
  depuis son paquet (`fetchConfig.selector`), jamais d'Internet.
- **Certificats** : Turtles remplace ceux de cert-manager par les siens
  (condition `WranglerManagedCertificates`, annotation
  `need-a-cert.cattle.io/secret-name` sur les services de webhook), valables
  un an et renouvelés par Rancher. Vérifié sur harv1.
- **Versions retenues** (appairage de la matrice CAPHV) : cœur fourni par
  Turtles (v1.13.3), fournisseurs RKE2 v0.25.2 (contrat v1beta2), CAPHV
  v0.10.1.
- Réserve à dire dans la doc : Rancher intégré à Harvester n'est pas prévu
  pour gérer d'autres clusters ; la console n'importe donc **pas** les
  clusters créés dans ce Rancher (voir plus bas).

### L'ancienne installation

Une installation d'avant Turtles (espaces `capi-system`,
`capi-kubeadm-*`, `rke2-*`, `caphv-system` sans `CAPIProvider`) est
détectée et proposée au retrait : seulement ce qui appartient aux anciens
fournisseurs, jamais les CRD du cœur, que Turtles a reprises (annotations
`objectset.rio.cattle.io`), ni l'objet `fleet-local/local` de Rancher.
Fait à la main sur harv1 le 25/09/2026 après sauvegarde complète : deux
ClusterClass et leurs gabarits, six espaces de noms, quatre webhooks
kubeadm, douze CRD, six rôles et leurs liaisons.

### Le générateur

`caphv-generate` (bash, dépôt CAPHV) est **livré dans `bin/`** de la
console, figé sur un commit connu, avec sa provenance. C'est l'outil que la
documentation CAPHV décrit ; le réécrire ferait diverger deux sources.
Parité CLI : l'opérateur peut l'appeler lui-même.

- **Liste blanche** des options transmises ; `--apply` n'est jamais passé :
  la console applique elle-même, avec le kubeconfig du cluster choisi.
- Post-traitement : l'étiquette `cluster-api.cattle.io/rancher-auto-import`
  que le générateur pose toujours est retirée, sauf si l'exploitant demande
  l'import dans un Rancher externe (option avancée, désactivée par défaut).

### Les défauts de CAPHV v0.10.1 sur Harvester 1.9

Trois défauts trouvés en créant pour de vrai, contournés par la console et
corrigés dans une branche de CAPHV (`fix/harvester-1.9-capi-1.13`, à
proposer en amont) :

1. **La VIP** : CAPHV la lit dans l'annotation `kube-vip.io/loadbalancerIPs`
   du Service `kube-system/ingress-expose`, que Harvester 1.9 n'a plus (la
   VIP est l'adresse d'équilibrage de `kube-system/rke2-traefik`). La
   console crée un Service de remplacement étiqueté
   `harvester-ops.io/shim=caphv-ingress-expose`, seulement quand l'original
   manque.
2. **L'état des machines** : CAPHV écrit `status.ready` sans
   `status.initialization.provisioned`, alors que l'étiquette de contrat
   `cluster.x-k8s.io/v1beta2` de sa CRD fait lire ce second champ au cœur
   v1.13 : les machines restaient « Provisioning ». Le `CAPIProvider` de la
   console porte un correctif (`spec.patches`) qui retire l'étiquette de la
   seule CRD `harvestermachines` ; le cœur retombe sur le contrat v1beta1 et
   lit `status.ready`. Une retouche à la main de la CRD est défaite par
   Turtles, d'où le correctif porté par le fournisseur.
3. **Les gabarits** : le générateur les écrit en `v1alpha1` ; la conversion
   y ajoute `spec.template.metadata: {}`, que le schéma refuse quand la
   topologie recopie le gabarit. La console réécrit les objets Harvester et
   leurs références en `v1beta1`.

### Le formulaire

Regroupé en quatre blocs toujours visibles, un bloc replié :

| Bloc | Contenu | Aide |
|---|---|---|
| L'essentiel | nom, version de Kubernetes | nom rendu valide pendant la frappe ; versions éprouvées marquées |
| La taille | plan de contrôle 1/3/5, workers, gabarit, CPU, mémoire, disque | gabarits petit/moyen/grand ; tailles proposées |
| Système et accès | image, utilisateur SSH, paire de clés | images du cluster (sans ISO ni image en cours), SUSE d'abord, dernière choisie retenue ; utilisateur déduit du système |
| Réseau | réseau, pool, passerelle, masque, DNS | VLAN affiché ; plages et adresses libres du pool ; passerelle et masque tirés du pool (« déduit du pool » tant qu'on n'y touche pas) ; DNS retenu |
| Options avancées (repliées) | espaces de noms, pools et réseaux en plus, disque de données, CNI, CIDR pods et services, import Rancher, Fleet (MTU, encapsulation, BGP) | les réglages CNI signalés « avec Fleet seulement » |

Toutes les options du générateur sont proposées, plus trois que seule la
console sait poser (espace de noms des VMs, réseaux en plus, CIDR des
services). Une ligne de synthèse donne le total (VMs, vCPU, mémoire,
disque, adresses nécessaires et libres).

Le **contrôle préalable** (`harvester-capi check`) tourne 600 ms après la
dernière modification et rend des constats codés, traduits par la page :
bloquants (pile absente, nom pris, espace de noms qui contient déjà un
cluster, image absente, ISO ou pas prête, clé, réseau, pool ou classe de
stockage absents, pas assez d'adresses, CIDR qui se chevauchent, option
invalide) et avertissements (passerelle ou masque différents du pool, un
seul ou un nombre pair de plans de contrôle, petites VMs, version jamais
éprouvée, réglages CNI ignorés sans Fleet, CPU ou mémoire justes, espace
de noms existant, reste d'ancienne installation). Le calcul de place suit
le surengagement de Harvester (`overcommit-config`) : une VM de 2 vCPU et
4 Gio y demande 125 m de CPU et 3,3 Go.

**Adresses** : une par machine plus une, pour la machine de remplacement
d'une mise à jour progressive. Le compteur `available` des pools Harvester
dérive (24 libres sur 16 relevé sur harv1) : la console compte d'après la
table des adresses allouées.

**Images** : référencées par le nom de l'objet (`default/image-nhtf9`), pas
le nom affiché, qui peut porter des espaces (« Rocky Linux 8 GenericCloud »
était refusé) ou être partagé par deux images. CAPHV accepte les deux.

**Un cluster par espace de noms** : le générateur nomme ses objets sans le
nom du cluster (ClusterClass `harvester-rke2`, secret `hv-identity-secret`,
compléments) ; un second cluster les écraserait. Le contrôle le refuse.

**Suppression** : les objets rendus portent l'étiquette
`harvester-ops.io/generated` ; quand le dernier cluster d'un espace de
noms est supprimé, ils partent avec lui (vu sur harv1 : le secret
d'identité, qui porte un kubeconfig du cluster Harvester, survivait au
cluster), puis l'espace de noms s'il a été créé par la console.

## Essai réel sur harv1 (25/09/2026)

Harvester v1.9.0, Turtles v0.27, cœur Cluster API v1.13.3.

1. **Installation depuis l'onglet Installation** sur un harv1 remis à zéro
   (fournisseurs, espaces de noms et service de remplacement retirés) :
   images poussées par SSH, trois `CAPIProvider` prêts, service de
   remplacement recréé, étiquette de contrat retirée. Environ deux minutes.
2. **Premier cluster depuis le formulaire** (1 plan de contrôle, 1 worker,
   v1.34.11, SLES 15 SP7) : les deux nœuds Ready, kubeconfig téléchargé
   utilisable. Deux constats :
   - la console a annoncé « disponible » quand le plan de contrôle était
     prêt, **quatre minutes avant que le worker existe** : la topologie ne
     crée la MachineDeployment qu'après l'initialisation du plan de
     contrôle, et `Available` est déjà vraie dans l'intervalle ;
   - **ingress-nginx bloqué en ContainerCreating** (`iptables: executable
     file not found`) : l'image SLES n'est pas enregistrée, n'a aucun
     dépôt, et cloud-init n'a pu installer ni `iptables` ni
     `qemu-guest-agent`. D'où l'avertissement `image-sles-repos`.
3. **Second cluster** (openSUSE Leap 15.6, mêmes réglages) : tout tourne,
   ingress compris ; un volume demandé dans le nouveau cluster est fourni
   par Harvester (PVC `pvc-<id>` dans l'espace de noms des VMs), écrit, puis
   libéré. Troisième constat : « disponible » annoncé **28 s avant que le
   nœud du worker soit Ready** (il comptait dès son inscription, et un repli
   ignorait une condition `Available` encore fausse). L'attente exige
   désormais les nombres de la topologie, chaque nœud Ready, et `Available`
   quand le cœur la publie.
4. **Les corrections CAPHV destinées à l'amont**, éprouvées sans aucun
   contournement de la console : image corrigée chargée sur harv1 et
   désignée au `CAPIProvider` (`spec.deployment.containers[].imageUrl`),
   correctif retiré (l'étiquette de contrat revient sur la CRD), service de
   remplacement supprimé, cluster rendu par le générateur brut (gabarits
   `v1alpha1`) : `Available`, machines `provisioned=true`, gabarits clonés
   par la topologie, fournisseur de cloud pointant sur
   `https://172.16.3.100:6443` trouvée sur `rke2-traefik`.
5. **Retour à la configuration livrée** depuis l'onglet Installation :
   l'étiquette de contrat est **restée** sur la CRD. Turtles a réappliqué
   les composants corrigés « depuis son cache » sans ôter une étiquette déjà
   posée, et le statut se disait prêt. L'installation vérifie désormais
   l'étiquette et la retire, le statut signale `compatibility/contract-label`
   tant qu'elle est là. Éprouvé : relancée sur ce harv1, l'installation l'a
   retirée.
6. **Suppressions depuis la liste** : espace de noms, VMs et adresses du
   pool rendus à chaque fois. Durées : VM du plan de contrôle en ~1 min,
   plan de contrôle prêt en ~4-5 min, worker prêt en ~9-10 min.

Constats annexes : le compteur `available` des pools Harvester dérive (24
sur 16), une image au nom affiché avec espaces était refusée, le secret
d'identité survivait au cluster, la liste affichait « … » pour un cluster
disponible (elle lit maintenant les compteurs du statut). Tous corrigés et
couverts par des tests rejoués.

## Limites connues

- Les volumes réclamés dans un cluster supprimé avec des demandes encore
  liées restent dans Harvester.
- Pas de montée de version de Kubernetes.
- Le secret d'identité porte le kubeconfig passé au générateur (celui de la
  console) : quiconque lit les secrets de l'espace de noms du cluster
  obtient cet accès. Une identité dédiée et restreinte pour CAPHV reste à
  faire.
