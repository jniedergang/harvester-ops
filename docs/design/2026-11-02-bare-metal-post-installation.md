# Post-installation bare-metal : contenu, clusters Cluster API, artefacts (conception)

État : conception, 02/11/2026. Rien n'est codé. Les décisions à prendre sont
en fin de document, numérotées à partir de 111.

## La demande

Demande du propriétaire du produit (02/11/2026). Étendre le Bare-metal avec :

1. **Le contenu après l'installation, par Terraform.** Réseaux, images,
   classes de stockage, VMs... posés sur le cluster Harvester fraîchement
   installé. Par le provider Terraform de Harvester embarqué et par les
   déclarations Terraform de la console.
2. **Cluster API en post-installation.** « Paramétrable : on doit pouvoir
   choisir d'installer 1 ou plusieurs clusters pilotés par Cluster API sur
   le cluster Harvester fraîchement déployé. Une fois les clusters déployés
   on doit pouvoir y mettre des conteneurs / Helm et autres. »
3. **Des artefacts sur les clusters Kubernetes créés par Cluster API.**
   Charts Helm et add-ons, manifestes et GitOps, images pour l'airgap,
   politiques et accès (RBAC, quotas, politiques réseau, comptes).

## Objectifs

- Passer d'une baie de serveurs nus à une plateforme utilisable en **un seul
  geste suivi**. Harvester installé, contenu posé, clusters Kubernetes créés,
  applications et règles en place.
- **Chaque étape est facultative et réglable.** On peut ne faire que le
  contenu Terraform, ou que les clusters, ou tout.
- **Reprise** après un échec, sans refaire ce qui est fait.
- **Réutilisable** : la même description sert pour dix sites.
- **Airgap de bout en bout** : rien n'est téléchargé d'Internet au moment du
  déploiement.
- **Rien de nouveau quand l'existant sait faire.** Chaque étape appelle un
  mécanisme déjà livré et vérifié en réel (voir plus bas).
- Hors objectifs : Cluster API sur les hôtes KVM (décision du 02/11/2026,
  pas de fournisseur fiable), montée de version de Kubernetes des clusters
  enfants, installation de Rancher.

## Ce qui existe et que l'on réutilise

| Brique | Livrée en | Ce qu'elle apporte ici |
|---|---|---|
| Installation bare-metal (Redfish, média virtuel, découverte, disques, configuration complète) | 1.76 à 1.78 | L'étape « installer » telle quelle, action `baremetal-install:<hôte>` |
| Profils multi-nœuds et séries | 1.80.0 | Variables `{{nom}}`, rendu côté serveur, série création puis jonctions, action parente `baremetal-batch:<cluster>`, secrets jamais rangés |
| Déclaration automatique du cluster installé (clé SSH propre, kubeconfig dans l'état) | 1.78.0 | Le cluster est joignable par la console dès la fin de l'installation |
| Déclarations Terraform (un état chacune, plan revu puis appliqué, révision, historique) | 1.54 et 1.55 | L'étape « contenu » : des déclarations ordinaires, modifiables ensuite dans l'onglet Terraform |
| Provider Terraform et OpenTofu embarqués | 1.51.0 | Aucun téléchargement de provider |
| Pile Cluster API par Turtles (RKE2, CAPHV, CAAPH en `CAPIProvider`, ConfigMaps du paquet, images par `ctr import`) | 1.48 et 1.52 | L'étape « gestion Cluster API » est le bouton Installer de l'onglet Installation |
| Création de cluster RKE2 (`harvester-capi check`, `create`, attente par la topologie, suppression propre) | 1.48.0 | L'étape « clusters », une fois par cluster |
| Services par `HelmChartProxy` de CAAPH, sélection par étiquette, kube-vip par `ClusterResourceSet` | 1.52.0 | L'étape « artefacts » pour les charts |
| Gestes Conteneurs (créer, échelle, redémarrer, supprimer, dry-run serveur, identité de la personne) | 1.93.0 | Les règles de sécurité et de namespaces système, reprises pour les manifestes |
| Poussée d'images par l'API de registre v2, bibliothèque standard (`bin/lib/oci_push.py`) | 1.75.0 | Copier des images dans un registre sans podman ni skopeo |
| Paquet signé et vérifié de la mise à jour de la console | 1.81.0 | Le format et la vérification du paquet d'artefacts |
| Réglage `containerd-registry` de Harvester | existant | Le miroir côté nœuds Harvester |

Ce que la navigation vient de décider, et dont la conception tient compte :

- **2.1.0 (en cours)** : Bare-metal en onglets Machines, Profils, Images,
  Historique.
- **2.2.0** : Terraform en onglets (Déclarations, Ressources, Historique,
  Installation) et Cluster API en onglets (Clusters, Services, Historique,
  Installation).
- **2.3.0** : cibles KVM dans Terraform par le provider `dmacvicar/libvirt`.
- Pas de Cluster API sur KVM pour l'instant.

Manques constatés en préparant ce document :

- Le schéma des formulaires Terraform ne connaît que VM, image et clé SSH,
  plus le HCL brut. Réseaux, réseaux de cluster, configurations VLAN,
  classes de stockage et pools d'adresses s'écrivent en HCL brut. Leur
  présence dans le provider 1.9.0 embarqué est à relire dans sa source avant
  le lot concerné.
- Terraform s'exécute dans le processus web, sans script de `bin/`. La
  chaîne aura une entrée en ligne de commande qui charge la console, comme
  `harvester-baremetal profile apply`.
- Le générateur `caphv-generate` nomme ses objets sans le nom du cluster :
  **un cluster enfant par namespace**. La chaîne en tient compte.
- Le secret d'identité de CAPHV porte le kubeconfig de la console.

## La chaîne de bout en bout

Une seule action suivie, `deploy:<cluster>`, avec une étape par phase. Chaque
phase est facultative. Chaque phase lance les actions déjà existantes comme
actions filles, visibles dans le dock et dans l'Activité.

```
installation Harvester -> disponibilité -> contenu Terraform
  -> gestion Cluster API -> N clusters RKE2 -> artefacts par cluster
```

| # | Phase | Ce qu'elle fait | Mécanisme réutilisé | Terminée quand |
|---|---|---|---|---|
| 1 | Installation | Un nœud, ou une série création puis jonctions | `baremetal-install`, `baremetal-batch` | Toutes les lignes `done` et le cluster déclaré |
| 2 | Disponibilité | Attend un cluster prêt à recevoir | Lectures existantes du tableau de bord | Voir ci-dessous |
| 3 | Contenu Terraform | Instancie les modèles du plan en déclarations, plan, application | `tf_store`, `apply_declaration` | Chaque déclaration appliquée, plan suivant vide |
| 4 | Gestion Cluster API | Déclare RKE2, CAPHV et CAAPH depuis le paquet embarqué | `harvester-capi install` | Trois ou quatre `CAPIProvider` prêts |
| 5 | Clusters | Crée N clusters RKE2 depuis des modèles | `harvester-capi check` puis `create` | Chaque cluster aux nombres de sa topologie, nœuds Ready |
| 6 | Artefacts | Registre, images, charts, manifestes, politiques, GitOps | `HelmChartProxy`, `ClusterResourceSet`, `oci_push` | Chaque artefact installé sur chaque cluster visé |

**La disponibilité** (phase 2) : l'API répond sur la VIP ; tous les nœuds
attendus sont Ready ; les `ManagedChart` de Harvester sont prêts ; Longhorn
voit chaque nœud ordonnançable ; la classe de stockage par défaut existe ;
`rke2-traefik` a son adresse (CAPHV la lit) ; Turtles et son cœur Cluster
API sont prêts. Délai borné et réglable, vingt minutes par défaut. Chaque
condition non remplie est nommée dans l'étape.

**Ordre imposé.** Les clusters enfants ont besoin d'un réseau, d'une image,
d'un pool d'adresses et d'une clé : la phase 3 peut les créer. Un modèle de
cluster désigne donc ces objets par des variables (`{{reseau_vms}}`), que la
phase 3 rend vraies.

**Échec.** La chaîne s'arrête à la phase en échec. Dans une phase à
plusieurs éléments (clusters, artefacts), les éléments indépendants
continuent, et la phase se dit partielle.

**Reprise.** « Reprendre » crée une nouvelle action liée à la précédente
(`resumes: <id>`). Elle revérifie chaque phase terminée par une lecture bon
marché (déclaration à jour, cluster aux bons nombres, release installée),
puis repart de la première phase non terminée. Toutes les phases sont
idempotentes par construction : les mécanismes réutilisés le sont déjà.

**Arrêt.** Transmis à l'action fille en cours, comme pour une série. Les
éléments non commencés sont sautés.

**Le plan appliqué à un cluster existant.** Les phases 2 à 6 ne dépendent
pas de l'installation. Un plan peut donc s'appliquer à n'importe quel
cluster Harvester déclaré. C'est aussi ce qui rend les essais réels
possibles sans réinstaller à chaque fois.

## Où se configure la post-installation

Trois possibilités :

- **A. Une section « post-installation » dans le profil bare-metal.** Simple,
  mais un profil décrit des machines, pas des plateformes. Deux sites aux
  machines différentes et au contenu identique copieraient la section.
  Inutilisable sur un cluster déjà installé.
- **B. Un objet séparé, le plan de déploiement**, réutilisable, que le profil
  désigne par son nom (un seul champ en plus dans le profil) et que l'on
  peut aussi appliquer seul à un cluster existant.
- **C. Les deux.** Deux endroits pour la même chose.

**Recommandation : B.** Le profil gagne un champ `plan` facultatif. La
fenêtre d'installation d'un nœud et la fenêtre de série proposent un plan.
La page d'hôte Harvester propose « Appliquer un plan ».

Emplacement dans l'interface : un onglet **Plans** dans Bare-metal, entre
Profils et Images. Les modèles de cluster ont leur place dans l'onglet
Clusters de Cluster API (2.2.0), et les modèles de déclaration dans l'onglet
Déclarations de Terraform : le plan les désigne, il ne les recopie pas.

## Modèle de données

Même rangement que les profils : un fichier YAML par objet dans le
répertoire d'état, 0600, nom RFC 1123, chemins relatifs, déplaçable avec la
console.

| Objet | Rangement | Contenu |
|---|---|---|
| Plan de déploiement | `<état>/plans.d/<nom>.yaml` | Variables déclarées, phases activées et leurs réglages, références aux modèles et artefacts, révision |
| Modèle de déclaration Terraform | `<état>/tf-templates.d/<nom>.yaml` | Les ressources d'une déclaration, au format de `tf_store`, chaînes à variables |
| Modèle de cluster | `<état>/capi-templates.d/<nom>.yaml` | La requête du formulaire de création (celle que lit `harvester-capi check`), chaînes à variables |
| Paquet d'artefacts | `<état>/artefacts.d/<nom>-<version>/` | Index, charts, images en couches OCI, manifestes, empreintes, signature |
| Exécution | base des actions existante | Action parente, phases, ce qui a été créé (noms seulement), lien de reprise |

Un plan, en résumé (illustration du modèle, pas du code) :

```yaml
name: site-standard
revision: 4
variables: [reseau_vms, vlan, pool_debut, pool_fin]
phases:
  readiness: {timeout_minutes: 20}
  terraform:
    templates: [reseaux-site, images-base, classes-stockage]
  capi:
    providers: {caaph: true}
  clusters:
    - template: rke2-petit
      name: "{{cluster}}-apps"
    - template: rke2-petit
      name: "{{cluster}}-outils"
  registry: {mode: seed}            # ou external, avec url et CA
  artefacts:
    - package: socle-2026.11
      items: [podinfo, ingress-politiques, quotas-equipes]
      clusters: ["{{cluster}}-apps"]
```

Règles :

- **Variables** : le moteur de substitution des profils (jetons neutres,
  types tirés du schéma, une ligne et 512 caractères au plus). Variables
  intégrées en plus de celles des profils : `cluster`, `vip`. Une variable
  absente est refusée avant toute action, avec son chemin.
- **Révision** : comme les déclarations. Une modification part avec la
  révision lue, 409 sinon.
- **Ce que la phase 3 produit** : des déclarations ordinaires nommées
  `<plan>-<modèle>` dans le cluster, avec leur propre état. Elles portent
  l'origine (plan, modèle, révision). On les modifie ensuite dans l'onglet
  Terraform comme les autres.
- **N clusters** : une liste explicite d'entrées, chacune un modèle et un
  nom. Un nombre avec un motif de nom (`{{cluster}}-k8s-{{n}}`) est un
  raccourci de saisie qui produit la même liste. Chaque cluster a son
  namespace, du nom du cluster.
- **Aucun secret dans ces fichiers.** Les secrets sont saisis au lancement
  et à la reprise (voir Sécurité).

## Artefacts : paquet et livraison airgap

### Le paquet

Un fichier `tar.gz` signé, au même format et avec la même vérification que
le paquet de mise à jour de la console :

- `artefacts.yaml` : l'index. Chaque élément a un nom, une sorte (`chart`,
  `manifest`, `policy`, `gitops`), une version, ses valeurs par défaut, et
  les images qu'il tire ;
- `charts/` : les charts en `.tgz` ;
- `images/` : les images en disposition OCI (couches compressées telles que
  publiées) ;
- `manifests/` : les manifestes YAML ;
- `SHA256SUMS` et sa signature.

La signature est celle du client : sa clé publique se déclare dans les
Paramètres. Le paquet est à lui, pas à l'éditeur de la console.

Construction, sur une machine connectée : `harvester-artefacts pack
<description>`. Bibliothèque standard seulement. Les images sont recopiées
couche par couche par le code de `oci_push.py`, qui sait déjà le faire. Les
charts sont lus dans leur dépôt HTTP ou OCI. Import dans la console par
téléversement ou par `harvester-artefacts import`.

### Le registre

Les clusters enfants doivent tirer leurs images quelque part. Options :

- **Le registre du client** (Harbor, Quay, Distribution...) quand il existe.
  La console y pousse le paquet, avec ses identifiants.
- **Un registre d'amorçage sur le cluster Harvester lui-même**, posé par la
  console : un conteneur sur Harvester (charge bare-metal, GA en 1.9.1), un
  volume Longhorn, une adresse du pool Harvester par un service
  LoadBalancer. Son image est chargée par `ctr import`, comme les
  fournisseurs Cluster API.
- **Un registre servi par la console.** Il faudrait que chaque cluster
  enfant joigne la console, et la console deviendrait indispensable au
  fonctionnement des clusters. À écarter.
- Harvester n'embarque aucun registre.

**Recommandation : le registre du client s'il est donné, sinon le registre
d'amorçage.** Logiciel du registre d'amorçage : **CNCF Distribution**
(Apache-2.0, le plus simple, OCI, charts Helm OCI acceptés). Zot (Apache-2.0)
est une alternative si un miroir intégré devient utile. Harbor est trop
lourd pour un registre d'amorçage. Règle anti-verrouillage tenue : logiciel
ouvert, auto-hébergeable, API de registre standard, remplaçable par le
registre du client sans rien changer aux clusters sauf l'adresse.

Le registre d'amorçage partage le sort du cluster Harvester. Les clusters
enfants tournent aussi sur ce cluster : ce n'est pas un nouveau point de
défaillance. Ses écritures sont réservées à la console (identifiant
généré, gardé dans un Secret du cluster Harvester). La lecture est ouverte
sur le réseau des VMs, ou protégée par un compte de lecture si le plan le
demande.

### Brancher les clusters sur le registre

- **Clusters enfants** : la configuration de registres privés de RKE2
  (miroirs `docker.io`, `registry.suse.com`... vers le registre, CA,
  identifiants par Secret) posée dans le gabarit d'amorçage RKE2 du
  cluster. Option du générateur ou post-traitement : à vérifier sur
  `caphv-generate` avant le lot.
- **Charts** : le `HelmChartProxy` vise le dépôt OCI du registre
  (`oci://...`), avec CA et identifiants par Secret. La prise en charge par
  CAAPH v0.6.4 est à vérifier en réel avant le lot.
- **Nœuds Harvester** (si des VMs du contenu tirent des images) : le réglage
  `containerd-registry`, que la console sait déjà écrire.

### Manifestes

Par un `ClusterResourceSet` en mode `Reconcile`, le mécanisme déjà employé
pour kube-vip. Piège connu de Cluster API : supprimer un
`ClusterResourceSet` ne supprime pas ce qu'il a appliqué. La console lit la
liste des objets appliqués (`ClusterResourceSetBinding`) et les supprime
elle-même au retrait de l'artefact, avec la même règle que les gestes
Conteneurs : dry-run serveur d'abord, namespaces système refusés.

## GitOps

Options :

- **Fleet**, quand les clusters enfants sont importés dans un Rancher externe
  (option d'import déjà présente à la création). Fleet y est déjà, sans rien
  installer. Le Rancher intégré à Harvester n'est pas prévu pour gérer
  d'autres clusters : la console ne s'en sert pas.
- **Flux**, installé dans chaque cluster enfant comme un artefact ordinaire
  (chart par CAAPH). CNCF, Apache-2.0, en tirage depuis le cluster, sait lire
  des artefacts OCI : il peut suivre le registre d'amorçage en airgap, sans
  serveur Git.
- **Argo CD** : plus lourd (interface, base Redis), et un serveur Git reste à
  fournir en airgap.

**Recommandation** : pas de GitOps par défaut ; les artefacts sont livrés par
CAAPH et les `ClusterResourceSet`, réconciliés en continu, ce qui couvre le
besoin déclaratif. GitOps en option par élément `gitops` : **Fleet si le
cluster est importé dans un Rancher externe, Flux sinon.** La console pose
l'outil et sa source (dépôt Git du client ou artefact OCI du registre),
puis montre son état. Elle ne réécrit pas ce que l'outil sait faire.

## Politiques et accès

Des éléments `policy` du paquet, rendus en manifestes et livrés par
`ClusterResourceSet` :

- **Namespaces d'équipe** avec `ResourceQuota` et `LimitRange` ;
- **NetworkPolicy** : refus par défaut dans les namespaces d'équipe, plus
  les ouvertures déclarées (DNS, ingress) ;
- **RBAC** : rôles et liaisons par groupe ;
- **Comptes** : voir ci-dessous.

Un socle est proposé dans le catalogue, décoché par défaut : il change le
comportement des applications et se choisit en connaissance de cause.

**Accès humain aux clusters enfants.** Options : comptes de service et
kubeconfig ; OIDC de l'API vers le fournisseur d'identité du client ; import
dans un Rancher externe. **Recommandation** : Rancher externe s'il est
présent (droits gérés dans Rancher, connexion déjà relayée par la console
depuis 1.50.0) ; sinon OIDC de l'API (arguments de l'API RKE2 dans le modèle
de cluster) ; les comptes de service sont réservés à l'automatisation, avec
des jetons courts (TokenRequest), jamais un jeton permanent remis à une
personne.

## Sécurité

- **Rien dans argv.** Secrets par entrée standard ou fichier 0600 dans
  l'espace de travail, effacé à la fin, comme les profils et Terraform.
  Variables Terraform sensibles par fichier de variables 0600.
- **Secrets du plan** (identifiants du registre du client, mot de passe du
  cluster, jeton) : saisis au lancement, gardés en mémoire du déroulé,
  redemandés à la reprise. Une fois posés, ils vivent seulement en Secrets
  Kubernetes sur le cluster qui en a besoin. Ni le libellé, ni les étapes, ni
  le résultat n'en portent.
- **Kubeconfig des clusters enfants** : le Secret `<cluster>-kubeconfig` que
  Cluster API garde sur le cluster Harvester. La console le lit avec
  l'identité de la personne. Règle d'accès alignée sur le RBAC : qui peut
  lire ce Secret est administrateur du cluster enfant ; la console n'agit
  dans un cluster enfant que pour une personne qui a ce droit sur le
  namespace du cluster. Jamais écrit dans l'état de la console. Le
  téléchargement reste réservé aux administrateurs.
- **Identité de CAPHV** : aujourd'hui le kubeconfig de la console. À
  remplacer, dans le lot Cluster API, par un compte de service propre à
  chaque cluster enfant, limité à son namespace et aux objets dont CAPHV a
  besoin. Prérequis avant de créer des clusters en série.
- **Identité de la personne** pour toute écriture sur le cluster Harvester
  (usurpation portée dans le kubeconfig, mécanisme existant).
- **Registre d'amorçage** : écritures réservées à la console ; CA de la
  console ou du client ; identifiants de lecture distincts des identifiants
  d'écriture. La configuration de registres de RKE2 écrit les identifiants
  dans un fichier des nœuds : n'y mettre qu'un compte de lecture.
- **Paquet d'artefacts** : refusé sans signature valide ni empreintes
  conformes.
- `@requires_auth` et limite de débit sur chaque route qui écrit ; rôle
  opérateur pour lancer, administrateur pour les plans et les paquets.

## Expérience

Principes du propriétaire du produit : vérifier avant d'agir, bulles d'aide
claires, interface légère, onglets et fenêtres en overlay, traçabilité et
retour arrière.

### Éditer un plan

Onglet Plans : liste à gauche, plan à droite, une section par phase avec son
interrupteur. Les modèles et les éléments d'artefacts se choisissent dans des
listes, jamais retapés. Aperçu du rendu pour un cluster donné. Bulle d'aide
sur chaque contrôle, cinq langues.

### Lancer

Fenêtre overlay « Déployer », en trois onglets : **Paramètres** (cluster ou
série, plan, variables, secrets), **Vérifications**, **Résumé** (ce qui sera
créé, durée estimée d'après les mesures réelles : installation environ 20
min, pile Cluster API 2 min, plan de contrôle 4 à 5 min, worker 9 à 10 min).

L'onglet Vérifications rend des constats codés, bloquants ou avertissements,
regroupés par phase :

- **Capacité pour N clusters** : somme des VMs de chaque modèle, avec le
  surengagement de Harvester (`overcommit-config`), comparée au CPU et à la
  mémoire ordonnançables. Stockage : l'allouable Longhorn sous ses deux
  contraintes (surprovisionnement et place réelle, la plus serrée décide),
  répliques sur des nœuds distincts (trois répliques demandées sur un nœud
  unique : bloquant).
- **Adresses** : par cluster, une par machine plus une (mise à jour
  progressive), plus les services LoadBalancer prévus par les artefacts.
  Compté d'après les adresses allouées, pas d'après le compteur `available`
  des pools, qui dérive. VIP du cluster Harvester libre (hors pools, sans
  réponse).
- **Registre joignable depuis le réseau des VMs** : un pod bref attaché à ce
  réseau interroge `/v2/` (ce que verront les VMs, pas ce que voit la
  console). Registre du client : CA et identifiants acceptés.
- **Paquet** : signature, empreintes, images présentes pour chaque élément
  choisi.
- **Pile** : versions du paquet Cluster API embarqué, Harvester 1.9 ou plus
  pour la phase Cluster API.
- **Droits** de la personne sur chaque namespace qui sera écrit (accès
  évalués par le serveur).

Une vérification qui dépend d'une phase antérieure (une image que la phase 3
créera) est marquée « vérifiée au début de la phase N ». Elle est rejouée à
ce moment, avant toute écriture de la phase.

### Suivre

L'action parente montre ses phases et, dépliées, ses actions filles. Le dock
et l'Activité les gardent. L'onglet Historique de Bare-metal les filtre.

### Défaire

- **Par élément** : retirer un artefact d'un cluster (étiquette retirée,
  release désinstallée par CAAPH ; objets d'un `ClusterResourceSet`
  supprimés par la console) ; supprimer un cluster enfant (geste existant,
  namespace et secret d'identité compris) ; détruire une déclaration
  (geste existant).
- **« Défaire le déploiement »** : l'ordre inverse (artefacts, clusters,
  contenu Terraform) sur ce que l'exécution a créé, avec confirmation tapée
  et le plan de destruction affiché avant. La pile Cluster API reste.
  L'installation de Harvester ne se défait pas.
- Limites dites dans la fenêtre : volumes encore réclamés par un cluster
  enfant supprimé, objets LoadBalancer laissés par le fournisseur de cloud
  en mode DHCP (harvester/harvester#11767).

## Essais réels

Règle du projet : chaque phase exercée pour de vrai, vérifiée côté cluster,
puis remise en l'état.

Moyens : node2 a 125 Gio et 28 threads. harvlab prend 60 Gio et 24 vCPU,
harvlab2 24 Gio. Les VMs des clusters enfants sont des VMs dans des VMs
(KubeVirt imbriqué, déjà éprouvé sur harvlab).

Ce qui est réaliste :

| Essai | Banc | Contenu |
|---|---|---|
| Chaîne complète, installation comprise | Banc Redfish de node2 (émulateur sushy, VM `bmcfg-n1` portée à 8 vCPU et 32 Gio), harvlab2 éteint | Installation d'un nœud, contenu Terraform (réseau, image, clé), pile Cluster API, **un cluster enfant d'un plan de contrôle et d'un worker** (2 vCPU, 4 Gio chacun), registre d'amorçage, podinfo, un manifeste, le socle de politiques |
| Plan sur un cluster existant à trois nœuds | harvlab (60 Gio) | Phases 2 à 6, répliques Longhorn sur trois nœuds, vérifications de capacité au bord, reprise après un échec provoqué (worker arrêté pendant la phase 5), « Défaire le déploiement » |
| Phases 4 à 6 seules | harv1 | La pile y est déjà ; registre du client simulé par un second registre ; Flux suivant un artefact OCI |
| Plusieurs clusters enfants | harv4 (node4, 128 Gio), sur accord | Deux ou trois clusters 1+1 en une exécution |

Budget de node2 pour les deux premiers essais : 60 + 32 = 92 Gio, jamais en
même temps que harvlab2. Après chaque essai : `harvlab.sh tidy` (le disque
des images de node2 s'est déjà rempli à 100 %).

Non vérifiable sur nos bancs, à dire comme tel : LACP réel, plus de trois
clusters enfants, un registre Harbor de client.

Tests automatisés à chaque lot : rendu des plans et des modèles (variable
absente, type, valeur à syntaxe YAML), ordre et reprise des phases (phase
terminée sautée, échec partiel, arrêt), constats de vérification (un cas par
code, tableaux paramétrés, cas à plusieurs clusters), secrets jamais rangés
ni renvoyés, routes (authentification, rôles, limites), mutations
volontaires pour prouver que les tests détectent, e2e de l'onglet Plans et de
la fenêtre Déployer sur des routes simulées.

## Découpage en versions (après 2.3.0)

| Version | Apporte |
|---|---|
| 2.4.0 | Onglet Plans, plans de déploiement et modèles de déclaration ; moteur de la chaîne (action `deploy:<cluster>`, phases, arrêt, reprise) ; phases Disponibilité et Contenu Terraform ; plan appliqué à un cluster existant et après une installation ou une série ; champ `plan` du profil ; ligne de commande `harvester-deploy` ; formulaires Terraform pour réseau, classe de stockage et pool d'adresses si le provider les porte |
| 2.5.0 | Phases Gestion Cluster API et Clusters ; modèles de cluster enregistrés depuis le formulaire de création ; vérifications de capacité et d'adresses pour N clusters ; identité CAPHV restreinte par cluster ; suppression des contournements CAPHV si sa release qui contient la PR #276 est sortie |
| 2.6.0 | Paquet d'artefacts (format, signature du client, `harvester-artefacts pack` et `import`) ; registre d'amorçage ou registre du client ; registres privés RKE2 dans les clusters enfants ; charts tirés du registre par CAAPH |
| 2.7.0 | Phase Artefacts complète : charts, manifestes par `ClusterResourceSet` avec retrait propre, politiques et socle, comptes d'automatisation ; « Défaire le déploiement » |
| 2.8.0 | GitOps (Fleet si import Rancher externe, Flux sinon) ; OIDC de l'API des clusters enfants |

Chaque lot est livrable seul et utile seul. 2.4.0 sert déjà sans Cluster API.

## Risques

- **Performances des bancs** : trois niveaux de virtualisation sur node2.
  Les durées mesurées seront plus longues qu'en production, et un délai trop
  court ferait échouer la chaîne. Délais réglables, mesures publiées.
- **CAPHV** : trois défauts contournés aujourd'hui. Le contournement de la VIP
  casserait la version corrigée tant qu'il existe. Le changement de version
  du paquet embarqué doit le retirer.
- **CAAPH et OCI** : dépôt OCI, CA et identifiants non éprouvés en v0.6.4.
  Repli : dépôt Helm HTTP servi par le registre d'amorçage.
- **Retrait des manifestes** : Cluster API ne supprime pas ce qu'un
  `ClusterResourceSet` a appliqué. Tout repose sur le suivi de la console ;
  un objet modifié à la main peut rester.
- **Objets LoadBalancer orphelins** et nom de cluster `kubernetes` dans le
  fournisseur de cloud (CAPHV #277) : le défaire ne peut pas tout rattacher.
- **Capacité d'un cluster mononœud** : la tentation de mettre trop de
  clusters enfants sur un seul nœud. Les vérifications bloquent, elles ne
  conseillent pas seulement.
- **Taille des paquets d'artefacts** : plusieurs gigaoctets. Import en flux,
  vérification d'empreinte pendant l'écriture, place disque vérifiée avant.
- **Secrets à la reprise** : une reprise des heures plus tard redemande les
  secrets ; un opérateur qui ne les a plus est bloqué. Le dire au lancement.
- **Une chaîne trop longue** pour une seule action : l'Activité doit rester
  lisible. Phases repliées, actions filles dépliables.

## Décisions à prendre

| # | Sujet | Options | Ma recommandation |
|---|---|---|---|
| 111 | Où se configure la post-installation | A section du profil ; B plan de déploiement séparé, désigné par le profil ; C les deux | B |
| 112 | Emplacement des plans dans l'interface | A onglet Plans de Bare-metal ; B entrée racine Automatisation ; C page d'hôte seulement | A, plus « Appliquer un plan » sur la page d'hôte Harvester |
| 113 | Plan applicable à un cluster existant | A oui ; B après installation seulement | A |
| 114 | Forme du contenu Terraform | A modèles instanciés en déclarations ordinaires ; B déclaration cachée propre au plan ; C HCL libre dans le plan | A |
| 115 | Reprise après échec | A repartir de la première phase non terminée, après revérification ; B tout relancer ; C pas de reprise | A |
| 116 | Comportement en cas d'échec | A arrêt de la chaîne, éléments indépendants d'une phase poursuivis ; B arrêt immédiat de tout ; C tout poursuivre | A |
| 117 | Versions de Harvester pour la phase Cluster API | A 1.9 et plus (Turtles) ; B aussi 1.7 et 1.8 (ancienne pile) | A |
| 118 | Description des clusters enfants | A modèles nommés, enregistrés depuis le formulaire de création ; B spécification en ligne dans le plan | A |
| 119 | Registre des clusters enfants | A celui du client seulement ; B registre d'amorçage sur Harvester ; C registre servi par la console ; D Harbor posé par la console | A s'il est donné, sinon B |
| 120 | Logiciel du registre d'amorçage | A CNCF Distribution ; B Zot ; C Harbor | A |
| 121 | Format et signature du paquet d'artefacts | A tar.gz signé, format du paquet de mise à jour, clé du client ; B clé de l'éditeur ; C dossiers bruts sans signature | A |
| 122 | Outil de construction du paquet | A `harvester-artefacts pack`, bibliothèque standard ; B exiger helm et skopeo | A |
| 123 | Livraison des manifestes | A `ClusterResourceSet` en Reconcile, retrait suivi par la console ; B chart enveloppe par CAAPH ; C application directe par la console | A |
| 124 | GitOps | A aucun par défaut, Fleet si import Rancher externe, Flux sinon ; B Flux partout ; C Argo CD | A |
| 125 | Accès humain aux clusters enfants | A Rancher externe si présent, sinon OIDC de l'API, comptes de service pour l'automatisation seulement ; B kubeconfig de compte de service remis aux personnes | A |
| 126 | Identité de CAPHV | A compte de service restreint par cluster enfant, prérequis de 2.5.0 ; B garder le kubeconfig de la console | A |
| 127 | Socle de politiques (quotas, refus réseau par défaut, LimitRange) | A proposé, décoché par défaut ; B coché par défaut ; C absent | A |
| 128 | Retour arrière global | A « Défaire le déploiement » en ordre inverse, installation et pile exclues ; B gestes par élément seulement | A |
| 129 | Secrets du plan | A saisis au lancement et redemandés à la reprise, puis seulement en Secrets Kubernetes ; B gardés chiffrés par la console | A |
| 130 | Bancs de vérification | A banc Redfish de node2 (chaîne complète, 1 cluster 1+1) et harvlab (cluster existant), harv4 sur accord pour plusieurs clusters ; B harv4 d'abord | A |
| 131 | Découpage | A 2.4.0 à 2.8.0 comme ci-dessus ; B moins de lots, plus gros | A |
