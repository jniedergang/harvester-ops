# 2.7.0 : artefacts appliqués aux clusters enfants et « Défaire le déploiement » (plan de réalisation)

Conceptions : `2026-11-02-bare-metal-post-installation.md` (décisions 111 à
131 retenues, en particulier 123, 125, 127 et 128), plans de la 2.4.0
`2026-11-03-plans-deploiement-240-plan.md`, de la 2.5.0
`2026-11-05-plans-capi-250-plan.md` et de la 2.6.0
`2026-11-05-artefacts-registre-260-plan.md`. Point de départ : 2.6.0 (paquet
d'artefacts signé, registres déclarés, registre d'amorçage, publication,
charts tirés d'un registre par CAAPH, phase Registre). Une 2.6.1 (sonde du
réseau des VMs, décision 192) peut sortir avant : ce plan n'en dépend pas et
se rebase dessus.

## But

La 2.7.0 termine la chaîne du plan : ce qu'un paquet apporte arrive
réellement dans les clusters enfants, et ce qu'une exécution a créé se
défait dans l'ordre inverse.

1. **Phase Artefacts complète** (dernière phase de `ORDER`, aujourd'hui
   « coming in 2.7 ») :
   - **charts** d'un paquet publié dans un registre, installés par CAAPH
     (`HelmChartProxy`, mécanisme de Cluster API > Services, source « Depuis
     un registre » de la 2.6.0) ;
   - **manifestes** d'un paquet, livrés par un `ClusterResourceSet` en mode
     `Reconcile` (décision 123), **retrait suivi par la console** : Cluster
     API ne supprime jamais ce qu'un `ClusterResourceSet` a appliqué ;
   - **politiques et accès** : un **socle de politiques** fourni par la
     console, **décoché par défaut** (décision 127 : namespaces d'équipe,
     `ResourceQuota`, `LimitRange`, `NetworkPolicy` de refus par défaut et
     ouvertures), les éléments `policy` d'un paquet, des rôles par groupe, et
     des **comptes de service réservés à l'automatisation** (décision 125),
     avec des jetons courts par `TokenRequest`, jamais un jeton permanent.
2. **Les mêmes gestes hors plan** : appliquer un élément de paquet à un
   cluster enfant, le retirer, émettre un jeton d'automatisation, depuis
   Cluster API et en ligne de commande (parité).
3. **« Défaire le déploiement »** (décision 128) : sur une exécution `deploy:`
   (et la chaîne de ses reprises), dans l'ordre inverse : artefacts, clusters
   enfants, registre d'amorçage posé par le plan (facultatif, point 210),
   contenu Terraform. Jamais l'installation de Harvester, jamais la pile
   Cluster API. Plan de destruction montré avant, confirmation tapée, action
   suivie et reprenable, historique dans l'Activité et dans l'onglet
   Historique de Bare-metal.
4. **Reliquats repris** :
   - le bouton **Désinstaller** de Cluster API > Installation, refusé sur une
     pile Turtles depuis la 2.5.0 (D2), **réécrit pour la pile Turtles**
     (décision 178) : retrait sûr des objets `CAPIProvider` que la console a
     déclarés, par une sous-commande de `harvester-capi` ;
   - le **`HarvesterCluster` orphelin** (m7 de la revue finale 2.5.0) :
     constat bloquant au contrôle et retrait par `delete` ;
   - le **repli sur /tmp** des fichiers de travail en ligne de commande (m11
     de la revue finale 2.5.0) ;
   - les mineurs de la revue finale 2.6.0 marqués « later » (liste en tâche
     7, arbitrage au point 213).

GitOps (Fleet, Flux) et OIDC de l'API des clusters enfants restent pour la
2.8.0 : la sorte `gitops` reste « coming (2.8) ».

## Architecture

```
bin/lib/capi_manifests.py   nouveau (pur) : ConfigMap et ClusterResourceSet
                            d'un élément, étiquettes injectées, contrôle des
                            objets (sortes, namespaces, taille), objets à
                            retirer (ancien contenu moins nouveau), ordre de
                            suppression, état d'après les bindings
bin/lib/child_policies.py   nouveau (pur) : socle de politiques (namespaces
                            d'équipe, quotas, LimitRange, NetworkPolicy),
                            rôles par groupe, comptes d'automatisation ;
                            rend des manifestes livrés par capi_manifests
bin/lib/child_access.py     nouveau : kubeconfig d'un cluster enfant lu dans
                            le Secret `<cluster>-kubeconfig` (identité de la
                            personne), fichier 0600 de l'espace de travail,
                            effacé ; TokenRequest
bin/lib/work_dir.py         nouveau : espace de travail des outils de bin/
                            (m11), jamais /tmp
bin/lib/deploy_undo.py      nouveau (pur) : collecte de ce qu'une chaîne
                            d'exécutions a créé, plan de destruction, ordre,
                            moteur d'éléments (réutilise deploy_chain)
bin/lib/deploy_plans.py     + phase artefacts (format, contrôle, rendu),
                            artefacts dans registry_users / package_users
bin/lib/deploy_chain.py     COMING = () ; COMING_MESSAGE retiré du moteur
bin/lib/capi_stack.py       + uninstall_plan, uninstall (pile Turtles)
bin/lib/capi_cluster.py     + constat harvester-cluster-leftover
bin/lib/capi_services.py    + origine (plan, run, élément) sur le HCP
bin/lib/artefacts.py        policy rejoint KINDS ; gitops reste LATER_KINDS
bin/harvester-capi.py       + manifest-check, manifest-deploy,
                            manifest-remove, manifests, policy-render,
                            token, uninstall ; delete retire le
                            HarvesterCluster orphelin ; work_dir
bin/harvester-plans.py      + undo ACTION_ID [--dry-run] [--include ...]
bin/harvester-registry.py   work_dir (m11)
bin/harvester-artefacts.py  work_dir (m11)
web/tf_store.py             ORIGIN_OPTIONAL + "run"
web/app.py                  phase artefacts, gestes d'artefacts hors plan,
                            jeton, défaire (action deploy-undo:), route de
                            désinstallation Turtles, constats
web/static/js/              bm-plans.js (carte Artefacts, fenêtre Défaire),
                            capi-services.js (sortes, retrait), artefacts.js
                            (Appliquer), capi.js (Désinstaller), nouveau
                            capi-tokens.js, i18n.js
```

Ce qui est réutilisé, sans copie :

| Besoin | Mécanisme existant | Où |
|---|---|---|
| Chart d'un paquet dans un cluster enfant | `harvester-capi service-deploy` avec `source` {registry, package, version, item}, `capi_services.manifest`, secrets de lecture et CA | `bin/harvester-capi.py` `cmd_service_deploy`, `service_source` ; `bin/lib/capi_services.py` |
| Retrait d'un chart | `harvester-capi service-remove` (HCP supprimé, CAAPH désinstalle, Secrets du dernier service retirés, étiquette ôtée) | `bin/harvester-capi.py` `cmd_service_remove` |
| Livrer un manifeste par Cluster API | ConfigMap + `ClusterResourceSet` `strategy: Reconcile`, sélection par étiquette du `Cluster` | `bin/lib/capi_cluster.py` `kube_vip_docs` (1.52.0) |
| Taille d'un ConfigMap | `CM_LIMIT = 900 * 1024` | `bin/lib/capi_stack.py` |
| Namespaces système, limites mémoire | `hv_containers.is_system_namespace`, `SYSTEM_NAMESPACES`, `ensure_limits` | `bin/lib/hv_containers.py` (1.93.0) |
| Écritures vérifiées par le serveur | `Kube.create/patch(dry_run=True)` (`--dry-run=server`), `Kube.apply` | `bin/lib/kube.py` |
| Noms d'élément applicables | `capi_services.NAME_RE` (42 caractères au plus, nom du HCP) | `bin/lib/capi_services.py` |
| Contenu d'un paquet | `artefacts.check_index`, `load_package`, éléments `files` sous `manifests/<élément>/` | `bin/lib/artefacts.py` |
| Chaîne, phases à éléments, reprise, arrêt | `deploy_chain.run`, `Phase(items=..., before=...)`, `verify_item` | `bin/lib/deploy_chain.py` |
| Marques de ce qui a été créé | `undo: {kind: capi-cluster, ref}` (clusters, 2.5.0), `undo: {kind: registry, ref}` (registre, 2.6.0), recopiées à l'adoption par une reprise | `web/app.py` `_deploy_clusters_phase`, `_deploy_registry_phase` |
| Supprimer un cluster enfant | `harvester-capi delete` (`remove_cluster` : Cluster, identité, objets rendus, Secrets de registre, namespace créé par la console) | `bin/harvester-capi.py` |
| Détruire une déclaration | `_tf_decl_launch(..., "destroy-plan" / "destroy")`, puis `TF_DECLS.delete` refusé tant que l'état porte des ressources | `web/app.py` |
| Supprimer un registre d'amorçage | `harvester-registry delete [--delete-data]` (refus `registry-in-use`, volume gardé sans le nom tapé) | `bin/harvester-registry.py` |
| Verrou d'un cluster pendant un déroulé | `_deploy_cluster_lock`, `_deploy_busy`, `_DEPLOY_LAUNCH_LOCK` | `web/app.py` |
| Actions imbriquées | `_cli_action(..., parent=run.id)`, `_capi_launch(..., parent=...)`, `_deploy_wait_child`, `_deploy_wait_until` | `web/app.py` |
| Usurpation de la personne | `_identity_kubeconfig(kc, identity)` | `web/app.py` |
| Pile Turtles | `capi_stack.stack_status`, `provider_manifest` (étiquette `harvester-ops.io/managed`), `configmap_manifest`, `shim_state`, `SHIM_LABEL` | `bin/lib/capi_stack.py` |

## Contraintes communes (valeurs exactes)

- **Nom applicable d'un élément** : `capi_services.NAME_RE`
  (`^[a-z0-9]([-a-z0-9]{0,40}[a-z0-9])?$`, 42 caractères au plus). Un élément
  au nom plus long (`artefacts.ITEM_RE` en permet 63) se lit et se publie,
  mais ne s'applique pas (`artefact-name-too-long`, bloquant).
- **Un élément appliqué à un cluster enfant `<ns>/<c>`** porte sur le
  cluster de gestion (Harvester), dans `<ns>` :
  - chart : un `HelmChartProxy` nommé `<élément>` (comme un service), étiquette
    du `Cluster` `harvester-ops.io/svc-<élément>: "on"` (`label_of`) ;
  - manifeste ou politique : un ConfigMap et un `ClusterResourceSet` nommés
    `art-<élément>`, étiquette du `Cluster` `harvester-ops.io/art-<élément>:
    "on"`, `apiVersion: addons.cluster.x-k8s.io/v1beta1` (celle de
    `kube_vip_docs`), `strategy: Reconcile`, `resources: [{kind: ConfigMap,
    name: art-<élément>}]` ;
  - sur chacun : étiquettes `harvester-ops.io/managed: "true"`,
    `harvester-ops.io/artefact-item: <élément>`,
    `harvester-ops.io/artefact-package: <paquet>` ; annotations
    `harvester-ops.io/artefact-version: <version>`,
    `harvester-ops.io/applied-by: <personne>` et, depuis un plan, les
    étiquettes d'origine de `capi_cluster.ORIGIN_LABELS` (`plan`) et
    l'annotation `harvester-ops.io/plan-run`.
- **Objets d'un manifeste** (rendus dans le ConfigMap) : chaque objet reçoit
  les étiquettes `harvester-ops.io/managed: "true"`,
  `harvester-ops.io/artefact-item: <élément>` et
  `harvester-ops.io/artefact-cluster: <c>`. Le retrait ne supprime QUE les
  objets qui les portent encore (point 203).
- **Taille** : données du ConfigMap `art-<élément>` au plus `CM_LIMIT`
  (921 600 octets), refus `manifest-too-big` au-delà.
- **Objets acceptés** (point 204) : toute sorte namespacée hors namespaces
  système du cluster enfant (`hv_containers.is_system_namespace`, dont
  `*-system` couvre `calico-system`, plus `tigera-operator` ajouté
  explicitement) ; sortes de portée cluster acceptées :
  `Namespace`, `ClusterRole`, `ClusterRoleBinding` (jamais vers
  `cluster-admin`, jamais un sujet `system:*`), `StorageClass`,
  `PriorityClass`, `IngressClass`. Refusées, avec `manifest-kind-refused` :
  `CustomResourceDefinition`, `APIService`,
  `MutatingWebhookConfiguration`, `ValidatingWebhookConfiguration`,
  `ValidatingAdmissionPolicy`, `ValidatingAdmissionPolicyBinding`, `Node`,
  `PersistentVolume`, `CSIDriver`, `RuntimeClass`, toute sorte de portée
  cluster absente de la liste. `List` dépliée, documents YAML multiples
  acceptés, 500 objets au plus par élément.
- **Socle de politiques** (point 206), valeurs par défaut, chacune réglable :
  - namespaces d'équipe : 1 à 16 par cluster, RFC 1123, jamais système,
    étiquette `harvester-ops.io/team: "true"` ;
  - `ResourceQuota` `hops-quota` : `requests.cpu: "4"`,
    `requests.memory: 8Gi`, `limits.cpu: "8"`, `limits.memory: 16Gi`,
    `pods: "50"`, `persistentvolumeclaims: "10"`,
    `requests.storage: 200Gi`, `services.loadbalancers: "2"` ;
  - `LimitRange` `hops-limits` (type `Container`) : `default` cpu `500m`,
    mémoire `512Mi` ; `defaultRequest` cpu `100m`, mémoire `128Mi` ; `max`
    cpu `"2"`, mémoire `4Gi` ;
  - `NetworkPolicy` : `hops-default-deny` (tous les pods, `Ingress` et
    `Egress`), `hops-allow-same-namespace` (entrée et sortie entre pods du
    namespace), `hops-allow-dns` (sortie vers
    `kubernetes.io/metadata.name: kube-system`, pods `k8s-app: kube-dns`,
    UDP et TCP 53), `hops-allow-ingress` (entrée depuis le contrôleur
    d'ingress du cluster enfant : namespace et étiquettes relevés en tâche
    1, constantes citées avec leur source).
- **Rôles par groupe** : `RoleBinding` dans un namespace d'équipe vers les
  `ClusterRole` intégrées `admin`, `edit` ou `view`, sujet `Group` (nom libre,
  une ligne, 128 caractères au plus). Aucune `ClusterRoleBinding` dans le
  socle.
- **Comptes d'automatisation** (point 208) : 16 par cluster au plus,
  `ServiceAccount` `<nom>` (RFC 1123, 63 caractères) dans un namespace
  d'équipe, `RoleBinding` `hops-sa-<nom>` vers `edit` ou `view`.
  `automountServiceAccountToken: false`. Aucun Secret de jeton
  (`kubernetes.io/service-account-token`) n'est jamais créé ; un tel Secret
  dans un manifeste est refusé (`manifest-sa-token-refused`). Jeton par
  `TokenRequest` : durée 600 à 86 400 secondes, 3 600 par défaut, rendu une
  fois dans la réponse, jamais rangé, jamais dans une étape, un journal ou
  `actions.db`.
- **Phase Artefacts du plan** : 32 entrées au plus, 128 éléments au plus une
  fois les clusters dépliés (`entrées x clusters`), délai par élément
  `timeout_minutes` 1 à 60, 15 par défaut (chart : passé à `service-deploy
  --timeout` ; manifeste : attente du binding appliqué).
- **Ordre d'application dans un cluster** (point 211) : socle, rôles et
  comptes, puis manifestes et politiques du paquet, puis charts, dans l'ordre
  des entrées ; `depends_on` entre entrées accepté. **Ordre de retrait** :
  l'inverse.
- **Défaire** : action `deploy-undo:<cluster>`, résultat `undoes: <id>`,
  identifiant de 12 hexadécimaux, sous le même verrou de cluster que
  `deploy:` (un déroulé et un défaire ne tournent jamais ensemble sur un
  cluster). Rôle **administrateur** ; confirmation = nom du cluster Harvester
  tapé. Délais : retrait d'un chart 900 s (défaut de `service-remove`),
  retrait d'un manifeste 300 s, suppression d'un cluster 2 400 s (défaut de
  `delete`), destruction Terraform sans délai propre (celui de l'action
  existante). Chaîne de reprises remontée sur 32 maillons au plus.
- **Désinstallation Turtles** : action `capi-uninstall:<cluster>` (libellé
  existant), `harvester-capi uninstall [--dry-run] [--remove-images]
  [--timeout 600]` ; ne touche qu'aux `CAPIProvider` qui portent
  `harvester-ops.io/managed: "true"` (posée par `provider_manifest`), jamais
  à `core/cluster-api` ni à Turtles.
- **Espace de travail** (m11) : `HARVESTER_OPS_WORK_DIR`, sinon
  `<répertoire d'état>/work` créé en 0700 ; jamais `/tmp` ni le répertoire
  courant. Un fichier de kubeconfig d'un cluster enfant y est écrit en
  0600 et effacé dans un `finally`.
- **Bibliothèque standard** pour `capi_manifests`, `child_policies`,
  `deploy_undo`, `work_dir` (preuve par `python3 -I -S`, comme `pack`) ;
  PyYAML reste exigé là où il l'est déjà (lecture des plans, rendu YAML).
- **Rôles** : lecture viewer ; appliquer ou retirer un élément, émettre un
  jeton, socle et comptes = opérateur ET droit de lire le Secret
  `<cluster>-kubeconfig` du namespace du cluster enfant (règle de la
  conception : qui lit ce Secret est administrateur du cluster enfant,
  évaluée par `SelfSubjectAccessReview` avec l'identité de la personne) ;
  Défaire et Désinstaller = administrateur.
- **Textes publics** : ni tiret cadratin ni flèche Unicode ; cinq langues
  pour toute chaîne nouvelle ; info-bulle sur chaque contrôle.

## Constats préalables (lus dans le code le 07/11/2026)

- `deploy_chain.COMING = ("artefacts",)`, `deploy_plans.AVAILABLE` ne
  contient pas `artefacts`, `check_plan_warnings` refuse une phase
  `artefacts` activée (« coming in 2.7: set enabled: false ») et garde
  telle quelle une phase désactivée (« format de 2.7 non figé »). Le format
  se fige ici ; un plan de 2.6.0 qui porte une phase `artefacts`
  désactivée aux clés libres reçoit le même traitement que les phases
  Cluster API de 2.4.0 en 2.5.0 (avertissement, clés inconnues retirées).
- Marques de création existantes : `clusters` (`created`, `undo:
  {kind: capi-cluster, ref: <ns>/<nom>}`, `namespace_created`) et
  `registry` (`created`, `undo: {kind: registry, ref: <cluster>/<nom>}`),
  recopiées par une reprise qui adopte. **La phase Terraform n'en porte
  aucune** : `created` n'y est rempli qu'en essai (liste des déclarations
  d'essai). L'origine d'une déclaration (`tf_store.ORIGIN_KEYS`) n'a pas
  d'identifiant d'exécution.
- `harvester-capi service-deploy` vise un cluster par étiquette, un HCP par
  nom et par namespace ; dans un plan, chaque cluster a son namespace
  (`<nom>/<nom>`), donc un HCP par cluster et par élément. m-15 de la revue
  2.6.0 (même nom de service pour deux clusters d'un namespace) reste vrai
  hors plan.
- `ClusterResourceSetBinding` (un par cluster, du nom du cluster) liste les
  **ressources** (ConfigMaps, Secrets) appliquées et leur empreinte, pas les
  objets qu'elles contiennent : les objets se déduisent du ConfigMap que la
  console a écrit. À confirmer en tâche 1 sur la version de Cluster API de
  Turtles (Harvester 1.9.0) : comportement de `Reconcile` à un ConfigMap
  modifié (objets retirés laissés en place), à un CRS supprimé (binding
  nettoyé ou non).
- Le bouton Désinstaller de Cluster API > Installation lance
  `_capi_uninstall_runner` dans le processus web (namespaces supprimés,
  CRD supprimées, images effacées par SSH), sans script de `bin/` (contraire
  à la parité), refusé sur une pile Turtles (`turtles-stack`, 409, revue
  2.5.0 D2) et grisé dans `capi.js`.
- `_work_file` (harvester-capi), `tempfile.mkdtemp(dir=os.environ.get(
  "HARVESTER_OPS_WORK_DIR"))` de `harvester-capi` (`components_from`),
  `harvester-registry` (`deploy`) et `harvester-artefacts` (`publish`)
  retombent sur `/tmp` sans la variable (m11). La console la pose toujours
  (`web/app.py`, `_capi_base`).
- `registry_seed` génère la CA dans un répertoire de travail et ne la garde
  pas : renouveler le certificat du registre d'amorçage (m-9 de la revue
  4-8 de 2.6.0) demanderait une nouvelle CA et donc de nouveaux fichiers sur
  les nœuds des clusters enfants (point 213).

## Tâche 1 : essais préalables sur le banc (avant tout code)

Fichiers : aucun dans le dépôt ; compte rendu ajouté en fin de ce plan
(section « Essais préalables »), au même commit que la tâche 2.

Sur harvlab2 (point 201), pile Cluster API de la 2.6.0, un cluster enfant
1+1 créé pour l'occasion (`rke2-banc`), chaque essai remis en état :

1. **ClusterResourceSet en Reconcile** : ConfigMap de deux objets (un
   `ConfigMap` et un `Deployment` dans un namespace de test), CRS
   `art-essai`, étiquette posée sur le `Cluster`. Relever : délai
   d'application, contenu du `ClusterResourceSetBinding`, gestionnaire de
   champs des objets dans le cluster enfant (application côté serveur ou
   création), effet d'un ConfigMap modifié (objet changé, objet retiré du
   ConfigMap), d'un objet modifié à la main dans le cluster enfant (réécrit
   ou non), de l'étiquette retirée du `Cluster`, du CRS supprimé (binding).
2. **CAAPH au retrait** : HCP d'un chart publié supprimé, release
   désinstallée, namespace de la release laissé ou non (`createNamespace`).
3. **Réseau du cluster enfant** : CNI effectivement posé par `caphv-generate`
   (Calico par CRS ou canal de RKE2), application d'une `NetworkPolicy` de
   refus par défaut (un `wget` entre deux namespaces échoue, le DNS passe
   avec `hops-allow-dns`), contrôleur d'ingress de RKE2 : namespace,
   étiquettes de ses pods (pour `hops-allow-ingress`).
4. **TokenRequest** : durée minimale et maximale acceptées par l'API du
   cluster enfant (`--service-account-max-token-expiration` de RKE2),
   jeton refusé après expiration.
5. **Accès au cluster enfant** : Secret `<c>-kubeconfig` lu avec l'identité
   d'une personne opérateur (usurpation), adresse de l'API joignable depuis
   node1 (répartiteur du cluster enfant sur le réseau des VMs).
6. **CAPIProvider supprimé** (sur la pile du banc, à la fin, puis
   réinstallée par le bouton Installer) : ordre de retrait que Turtles
   accepte, déploiements et namespaces retirés ou non, CRD gardées ou non,
   ConfigMaps du paquet laissés ; cluster enfant présent au moment du
   retrait : le webhook de Turtles le refuse-t-il ? (attendu : la console
   refuse avant).
7. **HarvesterCluster orphelin** (m7) : un `HarvesterCluster` `<ns>/<nom>`
   créé à la main sans `Cluster` ; `harvester-capi create` du même nom
   échoue comme décrit en 2.5.0 ; il se supprime sans finaliseur bloquant
   tant que CAPHV n'a pas de `Cluster` propriétaire.

Critère de passage : 1, 3 et 5 réussis. Un `Reconcile` qui supprimerait de
lui-même un objet retiré du ConfigMap simplifie la tâche 2 (le constat est
gardé, le code de retrait reste pour la suppression de l'élément). Un échec
de 5 (API du cluster enfant injoignable depuis la console) est soumis avant
de coder : le retrait des objets passerait alors par un `Job` dans le
cluster enfant, autre conception.

## Tâche 2 : manifestes par ClusterResourceSet, retrait suivi (`capi_manifests`)

Fichiers : nouveaux `bin/lib/capi_manifests.py`, `bin/lib/child_access.py`,
`bin/lib/work_dir.py` ; `bin/harvester-capi.py` (sous-commandes
`manifest-check`, `manifest-deploy`, `manifest-remove`, `manifests` ;
`_work_file` et `components_from` sur `work_dir`) ; nouveaux
`tests/api/test_capi_manifests_270.py`, `tests/api/test_child_access_270.py`,
`tests/api/test_work_dir_270.py`, `tests/api/test_capi_manifests_cli_270.py`.

Interfaces :

```
capi_manifests.parse(files: {nom: texte}) -> [objet]          # YAML multi-documents, List dépliée
capi_manifests.check(objs, cluster, policy=False) -> [finding]
    # codes : manifest-kind-refused, manifest-system-namespace,
    # manifest-too-big, manifest-sa-token-refused, manifest-crb-admin,
    # manifest-duplicate (même sorte/ns/nom deux fois), manifest-no-name,
    # manifest-too-many (> 500)
capi_manifests.stamp(objs, item, cluster) -> [objet]          # étiquettes injectées, copie
capi_manifests.docs(ns, cluster, item, package, version, objs, origin=None, by="") -> [ConfigMap, CRS]
capi_manifests.removed(old_objs, new_objs) -> [(apiVersion, kind, ns, name)]
capi_manifests.delete_order(refs) -> [ref]                    # namespacés d'abord, Namespace et portée cluster en dernier
capi_manifests.state(crs, bindings, cm) -> {"applied": bool, "hash", "clusters": [...], "message"}
child_access.kubeconfig(kube, ns, cluster) -> chemin 0600     # contexte : with child_access.session(...) as path
child_access.token(kube_child, ns, sa, seconds) -> str        # TokenRequest, jamais écrit
work_dir.path(state_dir=None) -> Path                         # 0700, jamais /tmp
```

`harvester-capi manifest-deploy --spec F` (spec : `cluster: <ns>/<c>`,
`package`, `version`, `item`, `origin` facultatif) : lit l'élément dans le
paquet du répertoire d'état, `check`, puis lit l'ancien ConfigMap s'il
existe, applique ConfigMap et CRS (`Kube.apply`), pose l'étiquette du
`Cluster`, attend que le binding porte l'empreinte du nouveau ConfigMap
(`--timeout`), puis supprime dans le cluster enfant les objets de
`removed(ancien, nouveau)` qui portent encore les étiquettes de la console
(dry-run serveur de chaque suppression d'abord). Étapes : `check`, `apply`,
`reconcile`, `prune`. `manifest-remove --name <ns>/<c>/<élément>` : CRS
supprimé d'abord (plus de réapplication), étiquette du `Cluster` retirée,
objets du ConfigMap supprimés dans le cluster enfant dans `delete_order`,
un par un, seulement s'ils portent les étiquettes, puis ConfigMap. Un objet
introuvable compte comme retiré ; un objet sans étiquette est « gardé :
modifié à la main » (étape, code `kept-relabelled`) ; un cluster enfant
absent (supprimé) : seuls CRS et ConfigMap sont retirés, étape « objets
partis avec le cluster ». `manifests --json` : état de chaque élément
appliqué (CRS, binding, empreinte, clusters). Les quatre prennent
`--state-dir`, `--harvester-name`, `--kubeconfig` ou `--cluster`, et
l'identité de la personne par le kubeconfig usurpé que la console passe.

Tests (tableaux paramétrés) :

| Cas | Attendu |
|---|---|
| YAML à trois documents, un vide | deux objets |
| `List` de deux objets | deux objets |
| chaque sorte refusée de la liste (paramétré) | `manifest-kind-refused` |
| `ClusterRoleBinding` vers `cluster-admin`, sujet `system:masters` | `manifest-crb-admin` |
| objet dans `kube-system`, `cattle-system`, `calico-system`, `tigera-operator` | `manifest-system-namespace` |
| Secret `kubernetes.io/service-account-token` | `manifest-sa-token-refused` |
| contenu de 921 601 octets / 921 600 | refusé / accepté |
| 501 objets | `manifest-too-many` |
| même objet deux fois | `manifest-duplicate` |
| `removed` : trois anciens, deux nouveaux dont un renommé | deux références |
| `delete_order` : Namespace, Deployment, ClusterRole, ConfigMap | namespacés, puis ClusterRole, puis Namespace |
| `stamp` sur un objet qui porte déjà des étiquettes | étiquettes d'origine gardées, trois ajoutées |
| CLI `manifest-remove` : objet sans étiquette dans le cluster enfant | étape `kept-relabelled`, code 0, objet présent |
| CLI : cluster enfant absent | CRS et ConfigMap retirés, aucune lecture du kubeconfig |
| CLI : dry-run serveur refusé (webhook) | rien supprimé, code 1 |
| `work_dir` sans variable, état donné | `<état>/work`, mode 0700 |
| `work_dir` : `/tmp` refusé même donné en variable | `ValueError` |
| `child_access.session` : exception dans le bloc | fichier effacé |

Mutations nommées (chacune doit faire échouer un test) : « suppression sans
contrôle d'étiquette », « CRS gardé avant les objets », « ordre inversé
(Namespace d'abord) », « liste de refus amputée de CRD », « taille comparée
en caractères », « kubeconfig laissé », « repli /tmp rétabli », « prune
avant reconcile ».

## Tâche 3 : politiques et accès (`child_policies`)

Fichiers : nouveau `bin/lib/child_policies.py` ; `bin/lib/artefacts.py`
(`policy` passe dans `KINDS`, `LATER_KINDS = ("gitops",)`) ;
`bin/harvester-capi.py` (`policy-render`, `token`) ; nouveaux
`tests/api/test_child_policies_270.py`, `tests/api/test_capi_token_270.py` ;
`tests/api/test_artefacts_260.py` (sorte `policy` devenue applicable).

Interfaces :

```
child_policies.normalize(raw) -> spec | ValueError            # bornes et défauts des Contraintes
child_policies.render(spec) -> {"base": [objets], "roles": [objets], "accounts": [objets]}
child_policies.ingress_selector() -> dict                     # constante relevée en tâche 1, source citée
```

Spec : `{base: bool, teams: [{namespace, quota: {...}, limits: {...},
network: {default_deny: bool, allow_ingress: bool}}], roles: [{namespace,
group, role: admin|edit|view}], accounts: [{namespace, name, role:
edit|view}]}`. `base: false` (défaut) : aucun objet du socle, même si des
équipes sont données (rôles et comptes restent possibles). Le rendu passe
par `capi_manifests` comme un élément nommé `hops-policies` (même CRS,
même retrait). Les éléments `policy` d'un paquet sont des manifestes
ordinaires vérifiés avec `check(..., policy=True)` (même liste de sortes).

`harvester-capi token --name <ns>/<c> --account <namespace>/<compte>
--seconds N` : lit le kubeconfig du cluster enfant (identité de la
personne), contrôle que le compte porte les étiquettes de la console,
`TokenRequest`, écrit le jeton sur la sortie standard seulement (jamais en
étape) ; la console le lit dans le flux et le rend dans la réponse de la
route, une fois. Étape journalisée : « token issued for <ns>/<compte> on
<c>, N s, by <personne> ».

Tests :

| Cas | Attendu |
|---|---|
| `base: false`, deux équipes | aucun objet de base |
| `base: true`, une équipe, défauts | Namespace, ResourceQuota, LimitRange, 4 NetworkPolicy aux valeurs exactes |
| quota `requests.cpu: "-1"`, `pods: "abc"` | `ValueError` nommant le champ |
| 17 namespaces d'équipe | refus |
| namespace `kube-public` | refus |
| rôle `cluster-admin` | refus (admin, edit, view seulement) |
| compte sans namespace d'équipe déclaré | refus |
| `default_deny: false` | ni `hops-default-deny` ni ouvertures |
| jeton 599 s / 86 401 s | refus |
| jeton : compte sans étiquette de la console | refus `account-not-managed` |
| sortie de `token` | le jeton absent de stderr et des étapes |

Mutations : « socle coché par défaut », « jeton écrit en étape », « Secret
de jeton permanent rendu », « DNS non ouvert », « rôle cluster-admin
accepté ».

## Tâche 4 : phase Artefacts des plans

Fichiers : `bin/lib/deploy_plans.py` (`AVAILABLE` += `artefacts`,
`_check_artefacts_phase`, rendu des entrées, `registry_users` et
`package_users` comptent la phase Artefacts, littéraux et
`{{variables}}` en « peut-être employé », m-11) ; `bin/lib/deploy_chain.py`
(`COMING = ()`) ; `web/tf_store.py` (`ORIGIN_OPTIONAL = ("template_by",
"run")`) ; `web/app.py` (`_deploy_artefacts_phase`,
`_deploy_artefacts_prepare`, marques de création Terraform) ;
`bin/harvester-plans.py` (docstring, `check` montre les constats) ;
nouveaux `tests/api/test_deploy_plans_270.py`,
`tests/api/test_deploy_artefacts_270.py` ; mises à jour de
`tests/api/test_deploy_chain_240.py`, `test_deploy_plans_240.py`,
`test_deploy_plans_250.py`, `test_deploy_plans_260.py` (plus de « coming »).

Format (illustration) :

```yaml
phases:
  artefacts:
    enabled: true
    timeout_minutes: 15
    policies:                       # socle et accès, par cluster
      - clusters: ["{{cluster}}-apps"]
        base: false                 # décoché par défaut (127)
        teams: [{namespace: equipe-a}]
        accounts: [{namespace: equipe-a, name: ci, role: edit}]
    items:
      - package: socle
        version: "2026.11"
        item: podinfo               # chart : publié dans `registry`
        registry: "{{registre}}"
        values: {replicaCount: 2}   # par-dessus les valeurs de l'élément
        clusters: ["{{cluster}}-apps"]
      - package: socle
        version: "2026.11"
        item: ingress-politiques    # manifeste ou politique
        clusters: ["{{cluster}}-apps", "outils/outils"]
        depends_on: []
```

Règles : `clusters` désigne un cluster de la phase Clusters du même plan
(nom rendu) ou un cluster enfant existant `<ns>/<nom>` ; `registry` exigé
pour un chart, refusé pour un manifeste ; `values` refusées pour un
manifeste, contrôlées par les règles de secrets du plan (`scrub`, refus
`secret-looking value`) ; sorte `images` refusée (« publiée par la phase
Registre ») ; sorte `gitops` refusée (« coming in 2.8 »). Variables
résolues avant toute action.

Phase dans la console : un élément par (entrée, cluster), identifiant
`art-<cluster>-<élément>` (et `pol-<cluster>` pour les politiques),
préfixe d'étape `art-`, dépendances : `pol-<cluster>` avant les éléments du
même cluster, `depends_on` entre entrées. `before` rejoue : CAAPH prêt
(chart), paquet présent et confiance en mode strict, chart publié dans le
registre (`artefact-not-published`, bloquant), images de l'élément
publiées dans un registre du cluster (`artefact-images-not-published` :
avertissement, bloquant si le cluster a `no_fallback`), objets contrôlés
(`capi_manifests.check`), nom applicable, droit de la personne sur le
Secret `<c>-kubeconfig`, élément du même nom posé par une autre origine
(`artefact-taken`, bloquant). `run_item` lance en action fille
`capi-service-deploy:<ns>/<c>:<élément>` (chart), `capi-manifest-deploy:
<ns>/<c>:<élément>` (manifeste, politique) ou `capi-policies:<ns>/<c>`
(socle), puis consigne `created: true` et `undo: {kind: chart|manifest|
policies, ref: <ns>/<c>/<élément>}` quand l'objet porte l'annotation
`plan-run` de CE déroulé (ou d'un maillon de sa chaîne de reprises : recopie
à l'adoption, comme les clusters). `verify_item` : HCP prêt à la version
(chart) ; binding à l'empreinte du ConfigMap (manifeste).

Marques Terraform (prérequis du Défaire) : `_deploy_terraform_phase`
consigne `created: true`, `undo: {kind: tf-declaration, ref:
<identifiant>}` quand la déclaration est créée par le déroulé (`origin.run`
posé), recopiés à l'adoption d'une reprise comme pour les clusters.

Tests :

| Cas | Attendu |
|---|---|
| phase activée, une entrée chart sans `registry` | refus nommant `phases.artefacts.items[0].registry` |
| entrée manifeste avec `values` | refus |
| sorte `images` / `gitops` | refus avec le message dit |
| 33 entrées ; 129 éléments une fois dépliés | refus |
| `clusters` vers un nom absent du plan et pas `<ns>/<nom>` | `artefact-cluster-unknown` |
| valeur `password: x` dans `values` | `secret-looking value` |
| plan de 2.6.0 à phase désactivée aux clés libres | chargé, avertissement, clés retirées à l'enregistrement |
| deux clusters, deux entrées : identifiants et dépendances | `pol-` d'abord par cluster, ordre des entrées |
| reprise : élément fait vérifié puis sauté ; élément ajouté exécuté | comme la phase Terraform |
| un cluster en échec dans la phase Clusters | ses éléments `skipped`, les autres poursuivis, phase `partial` |
| `before` : chart non publié | phase en erreur avant toute écriture |
| déclaration créée par le déroulé | `created`, `undo` tf-declaration, `origin.run` |
| déclaration adoptée par une reprise | marques recopiées depuis l'entrée précédente |
| `registry_users` : entrée `{{registre}}` | listée « peut-être » (m-11) |
| `package_users` : paquet dans la phase Artefacts | listé, `package-in-use` à la suppression |

Mutations : « artefacts laissé dans COMING », « marque created posée sans
contrôle de l'annotation », « chart non publié accepté », « politiques
après les charts », « origin.run oublié », « variables non rendues dans
clusters ».

## Tâche 5 : « Défaire le déploiement » (`deploy_undo`)

Fichiers : nouveau `bin/lib/deploy_undo.py` ; `web/app.py`
(`_undo_collect`, `_undo_plan`, `_undo_start`, routes
`POST /api/deploy/<cluster>/undo/plan` et `POST /api/deploy/<cluster>/undo`,
`ADMIN_ONLY_SUFFIXES` += `/undo`) ; `bin/harvester-plans.py` (`undo
ACTION_ID [--dry-run] [--include registry,unproven] --confirm NOM`) ;
nouveaux `tests/api/test_deploy_undo_270.py`,
`tests/api/test_deploy_undo_routes_270.py`,
`tests/api/test_harvester_plans_undo_270.py`.

Interfaces :

```
deploy_undo.chain(run_id, read) -> [résultat]       # remonte `resumes`, 32 maillons au plus, boucle refusée
deploy_undo.collect(results) -> [Undo]               # union, dernier état gagnant
    Undo = {kind, ref, phase, run, proven: bool, default: bool, note}
deploy_undo.KINDS_ORDER = ("chart", "manifest", "policies", "capi-cluster", "registry", "tf-declaration")
deploy_undo.plan(undos, include) -> [Undo]           # ordre de retrait, dépendances : les éléments
                                                     # d'un cluster enfant avant ce cluster
deploy_undo.phases(plan, runners) -> [deploy_chain.Phase]   # une phase par sorte, éléments
```

Règles :

- **Ce qui est défait** : ce que la chaîne a créé (`created: true` et
  `undo`). Une ressource adoptée (« already present, adopted » sans marque)
  n'est jamais touchée. Un artefact appliqué à un cluster enfant que le
  défaire supprime est retiré quand même avant (objets du cluster partis
  avec lui, mais CRS, ConfigMap et HCP vivent sur Harvester).
- **Registre d'amorçage posé par le plan** : proposé décoché (point 210) ;
  coché, `harvester-registry delete` sans `--delete-data`, sauf seconde
  confirmation tapée du nom du registre.
- **Exécutions d'avant 2.7.0** (point 212) : une déclaration Terraform sans
  marque mais dont l'origine est ce plan et ce cluster est proposée
  décochée, avec la mention « création par ce déroulé non prouvée »
  (`proven: false`).
- **Déclaration modifiée depuis** (révision différente de
  `revision_applied`) : proposée décochée, avertissement.
- **Plan de destruction** (route `undo/plan`, CLI `--dry-run`) : pour chaque
  élément, ce qui partira : objets du cluster enfant (dry-run serveur),
  release, machines et VMs et volumes du cluster, ressources de l'état
  Terraform (`destroy-plan`, action fille en essai), objets et volume du
  registre. Limites dites : volumes encore réclamés, objets LoadBalancer en
  DHCP (harvester/harvester#11767), nom `kubernetes` du fournisseur de cloud
  (CAPHV #277).
- **Déroulé** : action `deploy-undo:<cluster>`, moteur `deploy_chain` (une
  phase par sorte dans `KINDS_ORDER`, un élément par objet), actions filles
  `capi-service-remove:`, `capi-manifest-remove:`, `capi-cluster-delete:`,
  `registry-delete:`, `tf-destroy:`. Un élément en échec n'arrête que ce qui
  en dépend (le cluster dont un artefact n'a pas pu être retiré reste, avec
  la raison) ; la phase est `partial`. Le registre attend tous les clusters
  choisis et chaque déclaration Terraform attend les clusters et le
  registre choisis (revue 4-8 I2, décision du 07/11/2026 : un cluster ou un
  registre resté là garde le contenu qui peut le porter). Arrêt transmis entre éléments.
  **Reprise** : relancer le défaire sur la même exécution ; les éléments
  déjà partis sont vérifiés absents et sautés (`verify_item` = lecture
  d'absence).
- **Historique** : l'action `deploy:` d'origine reçoit `undone_by: <id>` et
  `undone_at` dans son résultat ; l'onglet Historique de Bare-metal montre
  « Défait (partiellement) » ; un déroulé défait se relance par « Appliquer
  le plan » (nouvelle exécution) : il n'y a pas de « défaire le défaire »
  (point 214).
- **Garde** : verrou de cluster de `deploy:` ; refus si un déroulé tourne
  (`deploy-running`) ; refus d'un essai (`dry_run`) comme cible ; refus
  sans confirmation exacte (`confirm-name`, 400).

Tests :

| Cas | Attendu |
|---|---|
| chaîne de trois reprises, cluster créé au premier maillon | un Undo capi-cluster |
| boucle de `resumes` | refus `undo-chain-loop` |
| élément adopté sans marque | absent de la collecte |
| déclaration sans marque, origine du plan | `proven: false`, `default: false` |
| registre posé par le plan | `default: false` |
| ordre : artefacts, clusters, registre, Terraform | `KINDS_ORDER` |
| retrait d'un chart en échec | son cluster `skipped` (« depends on »), Terraform `skipped` aussi (revue 4-8 I2), `partial` |
| reprise du défaire : cluster déjà parti | vérifié absent, sauté |
| route sans `confirm` exact | 400 `confirm-name` |
| route par un opérateur | 403 |
| défaire pendant un déroulé du cluster | 409 `deploy-running` |
| cible = essai | 400 `undo-dry-run-target` |
| `undone_by` écrit sur l'exécution d'origine | présent, persistant dans `actions.db` |
| CLI `undo --dry-run` | plan JSON, aucune action fille d'écriture |

Mutations : « ordre Terraform avant clusters », « élément adopté défait »,
« registre coché par défaut », « données du registre supprimées sans second
nom », « défaire sans verrou de cluster », « dépendance chart vers cluster
ignorée ».

## Tâche 6 : gestes d'artefacts hors plan et jetons

Fichiers : `web/app.py` (routes `POST /api/capi/<cluster>/artefact` (chart
ou manifeste selon la sorte), `DELETE
/api/capi/<cluster>/artefact/<namespace>/<name>/<item>`,
`POST /api/capi/<cluster>/policies`, `POST
/api/capi/<cluster>/token`, `GET /api/capi/<cluster>/artefacts/applied`) ;
`_artefacts_required_role` ; nouveaux
`tests/api/test_capi_artefact_routes_270.py`,
`tests/api/test_capi_token_routes_270.py`.

Chaque route qui écrit : `@requires_auth`, `@_rate_limit` (limite valide
contrôlée par `limits.parse_many`, piège connu de flask-limiter), action
suivie créée avant tout geste (`_cli_action`, outil de `bin/`), identité de
la personne (`_identity_kubeconfig`), droit sur le Secret `<c>-kubeconfig`
contrôlé par le serveur avant le lancement (`child-admin-required`, 403).
La route du jeton répond 200 avec `{token, expires_at}` (en-têtes
`Cache-Control: no-store`), son action garde l'étape sans valeur ; le jeton
n'est jamais dans `actions.db`, les journaux ni le flux SSE.

Tests : un cas par route et par refus (viewer 403, opérateur sans droit
sur le Secret 403, nom invalide 400, élément inconnu 404, paquet non publié
409), jeton absent de `actions.db` et du journal (recherche de la valeur
dans la base et dans le fichier de log après l'appel), `Cache-Control`.
Mutations : « contrôle du droit retiré », « jeton consigné dans le résultat
de l'action », « limite de débit invalide ».

## Tâche 7 : reliquats (désinstallation Turtles, HarvesterCluster orphelin, revue 2.6.0)

Fichiers : `bin/lib/capi_stack.py` (`uninstall_plan`, `uninstall`) ;
`bin/harvester-capi.py` (`uninstall`, `delete` et `check`) ;
`bin/lib/capi_cluster.py` (constat) ; `web/app.py` (route
`/api/capi/<cluster>/uninstall` : pile Turtles par `harvester-capi
uninstall` en `_cli_action` ; le runner historique reste pour une pile
d'avant Turtles, inchangé) ; `bin/harvester-registry.py`,
`bin/harvester-artefacts.py` (`work_dir`) ; les fichiers des mineurs ;
nouveaux `tests/api/test_capi_uninstall_turtles_270.py`,
`tests/api/test_capi_hc_leftover_270.py` ; mise à jour de
`tests/api/test_capi_uninstall_turtles_250.py` (le bouton n'est plus grisé
sur une pile Turtles).

**Désinstallation Turtles (décision 178)** :

```
capi_stack.uninstall_plan(kube) -> {"providers": [...], "refusals": [finding], "kept": [...]}
    # refus : capi-clusters-present (tout Cluster, tous namespaces, listés),
    #         caaph-services-present (tout HelmChartProxy, si CAAPH est retiré),
    #         capi-plan-running (déroulé deploy: sur ce cluster)
    # gardés et nommés : CAPIProvider sans harvester-ops.io/managed, core, Turtles
capi_stack.uninstall(kube, step, dry_run=False, timeout=600)
    # ordre : CAAPH, CAPHV, contrôle de plan RKE2, amorçage RKE2 ;
    # chaque CAPIProvider supprimé, attente de la disparition de ses
    # déploiements, puis ConfigMaps du paquet (MANAGED), puis service de
    # compatibilité (SHIM_LABEL) ; CRD jamais supprimées
```

`--remove-images` : images du paquet effacées des nœuds par
`node_images` (par SSH, comme l'import), facultatif, décoché. Bouton
réactivé sur une pile Turtles, fenêtre overlay avec la liste des refus et
de ce qui reste, confirmation tapée du nom du cluster.

**HarvesterCluster orphelin (m7)** : `check` rend `harvester-cluster-
leftover` (bloquant) quand `harvesterclusters` `<ns>/<nom>` existe sans
`Cluster` `<ns>/<nom>` ; message : « supprimez-le par Supprimer ou
`harvester-capi delete` ». `remove_cluster` : Cluster absent et
HarvesterCluster présent, il est supprimé, attente 120 s ; encore là (un
finaliseur), étape en erreur qui nomme le finaliseur ; la console ne retire
jamais un finaliseur.

**Mineurs de la revue finale 2.6.0** (arbitrage au point 213) : m-9 (test de
route de l'usage à la suppression d'un registre d'amorçage), m-11 (traité
en tâche 4), m-12 (heuristique `system_ready_registries` documentée comme
telle), m-13 (`registries: []` explicite d'une entrée de plan remplace celle
du modèle), m-15 (même nom de service pour un second cluster d'un namespace
refusé, `service-name-taken`), m-16 (`chart_meta` borné à 1 Mio, cache par
date de modification de l'état), m-17 (ordre `elif` de
`disk-smaller-than-image` : les deux constats rendus), M6 (`SHA256SUMS` de
plus de 4 Mio : code `sums-too-large` au lieu d'un échec muet), m-1 de la
revue 4-8 (descripteur sans `size` refusé par `pack`), m-12 de la revue 4-8
(registre injoignable : code de sortie et message clairs au lieu d'un
`TypeError`), constat `registry-cert-expiring` (certificat du registre
d'amorçage à moins de 60 jours, avertissement dans Registres et dans les
vérifications du plan).

Tests (un par mineur, chacun échoue avant correction) ; désinstallation :

| Cas | Attendu |
|---|---|
| un Cluster présent dans un namespace quelconque | refus `capi-clusters-present`, rien supprimé |
| un HCP présent | refus `caaph-services-present` |
| CAPIProvider sans étiquette de la console | gardé, nommé |
| `core/cluster-api` | jamais dans la liste |
| `--dry-run` | liste, aucune suppression |
| ordre de suppression | CAAPH, CAPHV, CP RKE2, amorçage RKE2 |
| déploiement qui ne disparaît pas dans le délai | erreur, suivants non touchés |
| HarvesterCluster sans Cluster | `harvester-cluster-leftover` bloquant |
| `delete` : HC à finaliseur persistant | étape nommant le finaliseur, finaliseur intact |

Mutations : « Cluster présent ignoré », « core retiré », « CRD supprimées »,
« finaliseur retiré », « ordre inversé ».

## Tâche 8 : interface

Fichiers : `web/static/js/bm-plans.js` (carte Artefacts active : éléments
choisis dans des listes depuis les paquets importés, clusters de la phase
Clusters ou existants, valeurs, socle décoché ; fenêtre Déployer : constats
et résumé des artefacts ; fenêtre **Défaire** depuis l'Historique),
`web/static/js/capi-services.js` (colonne Sorte : chart, manifeste,
politiques ; retrait d'un manifeste ; état du binding),
`web/static/js/artefacts.js` (bouton **Appliquer** d'un élément : fenêtre
Paramètres, Vérifications, Résumé), nouveau `web/static/js/capi-tokens.js`
(comptes d'automatisation d'un cluster enfant, **Émettre un jeton** : durée,
jeton montré une fois avec un bouton Copier, puis effacé de la page),
`web/static/js/capi.js` (Désinstaller réactivé sur une pile Turtles,
fenêtre overlay), `web/static/js/i18n.js` (cinq langues),
`web/templates/index.html` (script ajouté) ; e2e nouveaux
`tests/e2e/test_bm_plans_artefacts_270.py`, `tests/e2e/test_deploy_undo_270.py`,
`tests/e2e/test_capi_artefacts_270.py`, `tests/e2e/test_capi_tokens_270.py`,
`tests/e2e/test_capi_uninstall_270.py` ; mises à jour de
`tests/api/test_nav_inventory_200.py` si un emplacement change, et des
listes d'onglets.

Fenêtre **Défaire le déploiement** (overlay, trois onglets comme Déployer) :
**Contenu** (éléments par sorte, cases, celles décochées par défaut
expliquées en info-bulle : registre, création non prouvée, déclaration
modifiée), **Plan de destruction** (rendu de `undo/plan`, limites dites),
**Confirmation** (nom du cluster tapé ; second nom pour les données du
registre). Bouton Défaire grisé tant que le plan de destruction n'est pas
revenu ou que le nom ne correspond pas, et après toute modification des
cases jusqu'au nouveau plan (leçon de m-1 de la revue 2.6.0 : minuterie
effacée à la fermeture et au lancement). Tout passe par `escapeHtml` ;
SSE par `SSEReconnect.connect()`.

Tests e2e sur routes simulées : carte Artefacts (sorte `images` absente du
choix, registre exigé pour un chart, socle décoché), fenêtre Défaire (cases
par défaut, bouton grisé sans plan ni nom, aucun envoi tardif), jeton
montré une fois et absent après fermeture, Désinstaller avec refus listés,
noms hostiles échappés. Mutations : « bouton Défaire actif sans nom »,
« jeton gardé dans le DOM », « échappement retiré ».

## Tâche 9 : essais réels, documentation, version

Fichiers : `VERSION` (2.7.0), `CHANGELOG.md`, `docs/en/artefacts.md` et
`docs/fr/artefacts.md` (manifestes, politiques, comptes, jetons, gestes,
« Not in 2.7.0 »), `docs/en/bare-metal.md` et `docs/fr/bare-metal.md`
(phase Artefacts, Défaire), `docs/en/operating-procedure.md` et
`docs/fr/procedure-operationnelle.md`, `docs/en/troubleshooting.md` et
`docs/fr/depannage.md` (objet gardé « modifié à la main », binding non
appliqué, retrait bloqué par un finaliseur, HarvesterCluster orphelin),
`docs/en/capabilities.md` et `docs/fr/capabilites.md` (rôles),
`tools/parity/data.py` puis `python3 tools/parity/gen.py --docs`, mémoire
du projet. Suites complètes avant le commit de version :
`python3 -m pytest tests/api/ -q -n 12` et `python3 -m pytest tests/e2e/ -q -n 8`,
sans échec ni test instable.

### Bancs

| Banc | État | Sert à | Ne sert pas à |
|---|---|---|---|
| harvlab2 | v1.9.0, 1 nœud imbriqué, 32 Gio, disque Longhorn de 48 Gio, pile Cluster API de la 2.6.0, éteint | toute la preuve de la 2.7.0 avec **un** cluster enfant 1+1 | deux clusters enfants à la fois (mémoire) |
| harv4 | v1.9.0, bare-metal, 128 Gio, pile installée en 2.5.0, éteint | un artefact sur deux clusters et leur défaire en une exécution (R9) | rien de nécessaire à la preuve centrale |
| harv1 | v1.9.0, porte Rancher Manager et mlm | rien en écriture | |

Place sur harvlab2 : registre d'amorçage 8 Gio, deux disques de 8 Gio,
image d'environ 1 Gio (relever d'abord les volumes laissés par la 2.6.0) ;
mémoire : cluster 1+1 (2 vCPU et 4 Gio chacun) et registre dans 32 Gio.

### Préparatifs (règles du homelab)

- node2 allumé par son iLO (172.16.1.32) ; node1 + node2 seulement. harvlab2
  démarré : `HARVLAB_NAME=harvlab2 HARVLAB_NODES=1 harvlab.sh start`.
  `harvester-capi status` : pile prête aux versions du paquet.
- Adresses : registre d'amorçage **172.16.2.76** (déjà réservée dans NetBox
  et Pi-hole en 2.6.0) ; cluster enfant dans le pool 172.16.2.200-205 du
  banc (aucune adresse nouvelle ; toute adresse ajoutée est enregistrée dans
  NetBox et Pi-hole avant usage).
- Réglage du banc pendant les créations : `storage-minimal-available-percentage`
  de 25 à 15, remis à 25 après chacune (comme en 2.6.0).
- Paquet `banc-270` 1.0.0 construit sur node1 par `harvester-artefacts pack`
  (chart podinfo, son image, un manifeste de deux objets, un élément
  `policy` d'une `NetworkPolicy`), signé par la clé de test de Vault
  `secret/infra/harvops-artefacts-test` ; une version 1.0.1 dont le
  manifeste a un objet de moins.
- Pas de coupure d'Internet ni de faux registre du client : l'airgap est
  prouvé en 2.6.0 et la 2.7.0 n'y change rien.
- Après chaque essai : rien ne reste sur le banc qui n'y était pas (le
  défaire et la désinstallation en sont la preuve, relevée par `kubectl`) ;
  `harvlab.sh tidy` ; à la fin, `harvlab.sh stop` pour harvlab2,
  `sudo systemctl poweroff` de node2, `PowerState: Off` lu par l'iLO.

### Déroulé (ce que chaque vérification réelle demande)

| # | Vérification | Banc et ressources | Vérifié côté cluster par |
|---|---|---|---|
| R1 | Plan complet sur harvlab2 existant : Terraform (un réseau de VM et une clé SSH, déclaration légère), registre d'amorçage, pile (déjà là, sautée), un cluster 1+1, phase Artefacts (socle d'une équipe, compte `ci`, manifeste, politique, chart podinfo) | harvlab2 32 Gio, 172.16.2.76, 2 adresses du pool, ~25 Gio Longhorn | `kubectl --kubeconfig <enfant>` : namespace d'équipe, quota, LimitRange, 4 NetworkPolicy, release podinfo, objets du manifeste avec étiquettes ; CRS et binding sur harvlab2 |
| R2 | Reprise : arrêt pendant la phase Artefacts, reprise, éléments vérifiés et sautés | même déroulé | étapes « already done (checked) », aucun doublon |
| R3 | Réconciliation : élément passé de 1.0.0 à 1.0.1 (un objet de moins), puis un objet modifié à la main (étiquette retirée), puis retrait de l'élément | même cluster enfant | objet retiré supprimé ; objet sans étiquette gardé et nommé ; après retrait, plus aucun objet étiqueté, CRS et ConfigMap absents |
| R4 | Politiques en vrai : pod au-delà du quota refusé, conteneur sans limite reçoit les défauts, `wget` vers un autre namespace refusé, DNS résolu, ingress joint par le contrôleur | même cluster enfant | événements du namespace, `kubectl exec` |
| R5 | Jeton : 600 s, `kubectl --token` liste les pods du namespace d'équipe, refusé ailleurs, refusé après expiration ; jeton introuvable dans `actions.db` et le journal | même cluster enfant | `grep` de la valeur sur la base et le journal (empreinte seulement affichée) |
| R6 | Défaire : plan de destruction puis défaire (registre décoché) : artefacts retirés, cluster supprimé, déclarations détruites et retirées ; Harvester et pile intacts ; puis second défaire, registre coché, données gardées | même banc | `kubectl get` des HCP, CRS, Cluster, namespaces `<c>` et `<c>-vms`, PVC, services LoadBalancer laissés (relevés et nommés), état Terraform vide, registre absent et PVC présent ; `harvester-capi status` prêt |
| R7 | Défaire interrompu pendant la suppression du cluster, relancé : termine sans erreur | nouveau déroulé R1 réduit (cluster et un manifeste) | idem R6 |
| R8 | Désinstallation Turtles : refus avec un cluster présent (avant R6), puis après R6 : `--dry-run`, retrait réel, CAPIProvider de la console absents, core et Turtles intacts ; **puis réinstallation par le bouton Installer** et pile prête (état d'origine du banc) | harvlab2, aucun cluster enfant | `kubectl get capiproviders -A`, déploiements des namespaces des fournisseurs |
| R9 | HarvesterCluster orphelin : créé à la main, `check` bloque, `delete` le retire | harvlab2 | `kubectl get harvesterclusters -A` |
| R10 | Deux clusters enfants dans un plan, un manifeste et un chart sur les deux, défaire des deux | **harv4** (node4 allumé par son iLO 172.16.1.34, `harvester-startup.sh --cluster harv4`, deux clusters 1+1, adresses du pool de harv4 relues dans NetBox), sur accord | idem R1 et R6, par cluster |

Non vérifiable sur nos bancs, à écrire comme tel : plus de deux clusters
enfants, Rancher externe important les clusters enfants, une personne
Rancher restreinte qui émet un jeton (essai par usurpation seulement).

## Points à trancher

201. **Bancs et accords.** Recommandation : harvlab2 seul pour R1 à R9
     (node2 allumé quelques heures, harvlab2 à 32 Gio comme en 2.5.0 et
     2.6.0, 172.16.2.76 déjà réservée, réglage Longhorn temporaire) ;
     **harv4 pour R10 seulement si vous l'acceptez** (coût énergétique de
     node4 sur une demi-journée), sinon R10 écrit « non vérifié ».
202. **Où vivent les gestes de manifeste, politiques et jeton.** Options :
     A sous-commandes de `harvester-capi`, à côté des services ; B nouvel
     outil `harvester-child`. Recommandation : A (même identité, mêmes
     options, un seul outil pour ce que Cluster API pose).
203. **Suivi du retrait.** Options : A étiquettes injectées dans chaque objet
     et contenu du ConfigMap, suppression des seuls objets encore étiquetés ;
     B tout ce que liste le ConfigMap, étiquettes ou non. Recommandation : A
     (un objet repris à la main n'est jamais supprimé en silence ; il est
     nommé « gardé »).
204. **Objets permis dans un manifeste.** Recommandation : namespacé hors
     namespaces système ; portée cluster limitée à Namespace, ClusterRole,
     ClusterRoleBinding (jamais `cluster-admin` ni sujet `system:*`),
     StorageClass, PriorityClass, IngressClass ; CRD et webhooks refusés
     (un opérateur s'installe par un chart).
205. **Accès au cluster enfant pour retirer et émettre un jeton.**
     Recommandation : Secret `<c>-kubeconfig` lu avec l'identité de la
     personne, qui doit avoir ce droit (règle de la conception), fichier
     0600 effacé ; jamais rangé dans l'état de la console.
206. **Origine du socle de politiques.** Options : A catalogue intégré de la
     console, paramétré, décoché par défaut, plus les éléments `policy` des
     paquets ; B uniquement des éléments `policy` de paquets.
     Recommandation : A, avec les valeurs par défaut des Contraintes
     (quotas 4/8 CPU, 8/16 Gio, 50 pods ; LimitRange 100m/128Mi à 500m/512Mi,
     max 2 CPU et 4 Gio ; refus réseau par défaut avec DNS, même namespace et
     ingress ouverts).
207. **Socle et namespaces existants.** Recommandation : le socle ne pose ses
     objets que dans les namespaces d'équipe déclarés, jamais dans `default`
     ni un namespace existant non créé par lui ; un namespace d'équipe déjà
     présent sans l'étiquette de la console bloque (`team-namespace-taken`).
208. **Comptes d'automatisation et jetons.** Recommandation : comptes vers
     `edit` ou `view` seulement, dans un namespace d'équipe ; jetons
     `TokenRequest` de 10 min à 24 h, 1 h par défaut, montrés une fois,
     jamais rangés ; aucun Secret de jeton permanent, refusé même dans un
     manifeste.
209. **Emplacement dans l'interface.** Recommandation : Services de Cluster
     API liste charts, manifestes et politiques (colonne Sorte) ; bouton
     Appliquer dans Artefacts ; comptes et jetons dans un panneau du cluster
     enfant (onglet Clusters) ; Défaire depuis l'Historique de Bare-metal et
     l'action dans l'Activité.
210. **Registre d'amorçage dans le Défaire.** Options : A proposé décoché,
     données gardées sauf second nom tapé ; B jamais (geste séparé) ; C
     coché. Recommandation : A (la 2.6.0 a ajouté cette phase entre la
     conception et ce lot ; la conception exclut seulement l'installation et
     la pile).
211. **Ordre d'application et de retrait.** Recommandation : socle, rôles et
     comptes, manifestes et politiques, charts ; retrait inverse ; les
     artefacts d'un cluster enfant avant ce cluster, les clusters avant le
     registre, le registre avant Terraform (une classe de stockage ou un
     réseau du contenu peut porter le registre et les clusters).
212. **Exécutions d'avant 2.7.0 et déclarations modifiées.** Recommandation :
     déclaration de l'origine du plan sans preuve de création, ou modifiée
     depuis l'application, proposée décochée avec la raison ; jamais
     détruite d'office.
213. **Reliquats de la revue finale 2.6.0.** Recommandation : dans la 2.7.0
     m-9, m-11, m-12 (documentation), m-13, m-15, m-16, m-17, M6, m-1 et
     m-12 de la revue 4-8, et un avertissement `registry-cert-expiring` à
     60 jours ; plus tard (2.8.0) m-14 (format des Secrets), la restriction
     du port 5443 à l'adresse de la console (réglage nouveau) et le
     renouvellement du certificat du registre d'amorçage (la CA n'est pas
     gardée : conception à part) ; M1 à M5, M9 à M11 documentés.
214. **Retour arrière du Défaire.** Recommandation : pas de « défaire le
     défaire » ; relancer le plan recrée ce qui a été retiré (les volumes du
     registre gardés sont repris) ; un défaire interrompu se relance et
     reprend.
215. **Désinstallation de la pile Turtles (178).** Recommandation : retrait
     des seuls `CAPIProvider` posés par la console (CAAPH, CAPHV, RKE2),
     refusé tant qu'un Cluster ou un HelmChartProxy existe, ConfigMaps du
     paquet et service de compatibilité retirés, CRD gardées, images des
     nœuds en option décochée ; le runner d'avant Turtles reste pour les
     piles d'avant Harvester 1.9, inchangé.
216. **HarvesterCluster orphelin (m7).** Recommandation : constat bloquant au
     contrôle ; retrait par Supprimer et `harvester-capi delete` quand aucun
     Cluster ne le porte ; jamais de finaliseur retiré par la console.
217. **Hors 2.7.0, venus des décisions 190, 191 et 196.** Recommandation :
     « Changer les registres » d'un cluster existant (renouvellement des
     machines), amorçage sans aucun Internet (image de VM préparée) et
     branchement de `containerd-registry` sur les registres déclarés
     passent en 2.8.0 ou plus tard ; la 2.7.0 reste sur la conception
     (artefacts, Défaire, reliquats).

## Décisions (07/11/2026)

Les recommandations des points 201 à 217 sont retenues. Pour R10, harv4 est utilisé à la demande (règle du propriétaire du produit du 07/11/2026 : les ressources de l'infra s'allument quand on en a besoin et s'éteignent après).

## Essais préalables (tâche 1)

Faits le 07/11/2026 sur harvlab2 (Harvester v1.9.0, Turtles et Cluster API
v1.13.3, CAAPH v0.6.4, CAPHV v0.10.1, RKE2 v0.25.2), node2 allumé pour
l'occasion. Cluster enfant `rke2-banc/rke2-banc` 1+1 (openSUSE Leap 15.6,
disque de 10 Gio, réseau `default/lab-net`) créé par `harvester-capi
create` du worktree (pool `plan-test-pool` 172.16.2.200-205, plage déjà
réservée dans NetBox ; `storage-minimal-available-percentage` abaissé de
25 à 15 pendant les essais puis remis) : disponible en 610 s, supprimé en
238 s. Les outils de la tâche 2 et 3 ont été exercés sur ce cluster en plus
des essais bruts. Critère de passage : 1, 3 et 5 réussis.

1. **ClusterResourceSet en Reconcile** (réussi).
   - Application en 1 à 3 s après l'étiquette posée sur le `Cluster` (ou
     après un ConfigMap modifié).
   - Le `ClusterResourceSetBinding` (servi en `v1beta2`, nom du cluster)
     liste par CRS la ressource et son empreinte : `{clusterResourceSetName,
     resources: [{kind: ConfigMap, name, applied, hash, lastAppliedTime}]}`.
     L'empreinte est `sha256:` + sha256 des valeurs du ConfigMap, clés
     triées : recalculée à l'identique par `capi_manifests.content_hash`
     (vérifié sur `art-essai` et `kube-vip-addon`).
   - Les objets sont créés puis mis à jour par le client de Cluster API
     (gestionnaire de champs `manager`, opération `Update`), pas par une
     application côté serveur. Les objets en JSON, un document par objet
     séparé par `---`, sont acceptés.
   - ConfigMap modifié : les objets restants sont réécrits (une valeur
     changée à la main est écrasée), **un objet retiré du ConfigMap reste
     dans le cluster enfant** : le retrait suivi de la console est
     nécessaire, y compris à la mise à jour.
   - Objet modifié ou supprimé à la main sans changement du ConfigMap :
     rien n'est réécrit ni recréé (relevé à 90 s ; un `manifest-deploy` du
     même contenu ne recrée pas non plus un objet supprimé, l'empreinte
     étant la même). Une empreinte égale ne prouve donc pas que tous les
     objets sont présents : `verify_item` (tâche 4) doit le savoir.
   - Étiquette retirée du `Cluster` : l'entrée du binding reste, les objets
     restent. CRS supprimé : l'entrée du binding part, les objets restent.
   - **Cluster API pose sur le ConfigMap une référence de propriétaire vers
     le CRS** dès qu'il l'applique : supprimer le CRS supprimait aussi le
     ConfigMap (la liste des objets à retirer). `manifest-remove` supprime
     donc le CRS en `--cascade=orphan` (vérifié : le ConfigMap reste, sans
     propriétaire) et lit le ConfigMap avant toute écriture.
   - `apiVersion: addons.cluster.x-k8s.io/v1beta1` est acceptée avec un
     avertissement de dépréciation (`use v1beta2`) ; gardée comme
     `kube_vip_docs` (point à reprendre quand la v1beta1 sera retirée).
2. **CAAPH au retrait** (réussi). `service-remove` d'un `podinfo` : release
   désinstallée en 7 s (plus aucun objet ni Secret de release), **le
   namespace `podinfo` créé par `createNamespace` reste**, vide. Le Défaire
   (tâche 5) le nomme dans le plan de destruction.
3. **Réseau du cluster enfant** (réussi). CNI = Calico posé par le chart
   `rke2-calico` de RKE2 (opérateur Tigera, namespaces `tigera-operator` et
   `calico-system`), configuré par le CRS `crs-calico-chart-config` de
   `caphv-generate`. Avec le socle (`manifest-deploy` du socle, équipe
   `equipe-a` refus par défaut, `equipe-b` sans refus) : `wget` dans le
   même namespace passe, vers `equipe-b` et depuis `equipe-b` vers
   `equipe-a` échoue (délai), le DNS répond grâce à `hops-allow-dns`. Un
   conteneur sans ressources reçoit les défauts du LimitRange (100m/128Mi
   demandés, 500m/512Mi limites). Contrôleur d'ingress : DaemonSet
   `rke2-ingress-nginx-controller` du namespace `kube-system`, pods
   étiquetés `app.kubernetes.io/name: rke2-ingress-nginx` et
   `app.kubernetes.io/component: controller` (ports d'hôte, pas de réseau
   d'hôte) : constantes de `child_policies`. Une Ingress de `equipe-a` est
   jointe depuis node1 sur les deux nœuds ; sans `hops-allow-ingress`, plus
   de réponse.
4. **TokenRequest** (réussi). L'API refuse moins de 600 s (« may not
   specify a duration less than 10 minutes ») et n'a **aucun maximum** sur
   ce RKE2 (172 800 s et un an accordés tels quels) : la borne de 86 400 s
   n'existe que dans la console. Jeton de 600 s : liste les comptes du
   namespace d'équipe, refusé dans `kube-system` (Forbidden) ; expiration
   annoncée 13:21:13, encore accepté à 13:21:55, refusé (Unauthorized) dès
   13:22:19 : l'API tolère environ une minute de décalage d'horloge, à dire
   dans la documentation des jetons (« valable N s, plus une minute au
   plus »). Le jeton n'est apparu dans aucune étape ni sur stderr.
5. **Accès au cluster enfant** (réussi). Le Secret `rke2-banc-kubeconfig`
   (type Opaque, clé `value`) est refusé à un opérateur usurpé sans droit
   (`--as spike-op --as-group spike-ops`, Forbidden), lu dès qu'une Role
   lui donne `get` sur ce seul Secret ; la `SelfSubjectAccessReview`
   répond pareil. **Piège** : `kubectl create -f` d'une
   SelfSubjectAccessReview avec une identité qui ne peut pas lister les
   CRD échoue côté client (validation) : passer `--validate=false`. Le
   kubeconfig vise l'adresse du répartiteur du cluster enfant, prise en
   DHCP sur le réseau des VMs (172.16.11.123, puis 172.16.11.87), joignable
   depuis node1 ; `kubectl delete --dry-run=server` y fonctionne, et les
   ressources se nomment `Sorte.version.groupe` (`ConfigMap` seul pour le
   groupe principal : `ConfigMap.v1` est refusé).
6. **CAPIProvider supprimé** (réussi, puis pile réinstallée). Aucun webhook
   de Turtles ne refuse la suppression d'un `CAPIProvider` (essai à blanc
   côté serveur accepté pour CAAPH, CAPHV, RKE2 et même `core/cluster-api`)
   alors qu'un cluster enfant existe : **la console doit refuser
   elle-même** (tâche 7). Retrait réel de CAAPH (`caaph-system/helm`),
   cluster enfant présent, aucun HelmChartProxy : objet parti
   immédiatement ; en 45 s, déploiement, webhooks, Secrets et ConfigMap de
   configuration retirés ; **gardés** : le namespace `caaph-system`, les CRD
   `helmchartproxies` et `helmreleaseproxies`, le ConfigMap du paquet
   `v0.6.4` (étiquette `harvester-ops.io/managed`). Le cluster enfant n'a
   rien vu. Réinstallation par `harvester-capi install --components`
   (ce que lance le bouton Installer, sans `--push-images` : images déjà
   sur le nœud) : pile prête en 30 s, aux versions du paquet.
7. **HarvesterCluster orphelin** (m7). Créé à la main sans `Cluster` :
   CAPHV ne pose **aucun finaliseur** (« ClusterOwner not yet set, waiting
   for CAPI ») et il se supprime à l'instant. Créé sous le nom d'un cluster
   à venir puis `harvester-capi create` du même nom : **la topologie
   l'adopte** (référence de propriétaire vers le nouveau `Cluster`, spec
   réécrite) et le cluster devient disponible normalement ; `check` ne le
   voit pas. L'échec décrit en 2.5.0 vient donc d'un reste qui porte encore
   une référence vers l'ancien `Cluster` (suppression bloquée, finaliseur)
   : le constat `harvester-cluster-leftover` de la tâche 7 vise ce cas
   (HarvesterCluster sans `Cluster` vivant, ou propriétaire d'un autre
   uid), un orphelin sans propriétaire n'étant qu'un avertissement.

Écarts au plan retenus pendant la tâche 2 (à valider par le propriétaire
du produit) :

- `capi_manifests.check(objs, cluster, item=None)` au lieu de
  `policy=False` : la liste des sortes est la même pour `manifest` et
  `policy` (Contraintes), le paramètre n'aurait rien changé (zéro
  redondance) ; `item` donné, la taille est mesurée sur le contenu rangé,
  étiquettes comprises.
- Un `Namespace` livré n'est supprimé que s'il ne porte aucune charge ni
  aucun volume d'ailleurs (objet sans propriétaire et sans les étiquettes
  de l'élément) : étape `kept-in-use`. Sans cela, retirer le socle
  supprimait le travail des équipes dans leurs namespaces (vu sur le banc :
  pods de test gardés).
- `child_access.session` rend le chemin du kubeconfig, comme le plan ; le
  jeton est obtenu par `kubectl create --raw .../token` (réponse JSON avec
  l'expiration), jamais par un argument de commande.
