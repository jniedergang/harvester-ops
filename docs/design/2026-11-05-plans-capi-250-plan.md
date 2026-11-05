# 2.5.0 : phases Cluster API et clusters enfants des plans (plan de réalisation)

Conceptions : `2026-11-02-bare-metal-post-installation.md` (décisions 111 à
131 retenues) et plan de la 2.4.0 `2026-11-03-plans-deploiement-240-plan.md`
(décisions 146 à 161 retenues). Point de départ : 2.4.0 (plans de
déploiement, moteur de chaîne, phases Disponibilité et Contenu Terraform).

## But

Un plan de déploiement sait désormais, après le contenu Terraform :

1. **déclarer la pile Cluster API** (fournisseurs RKE2, CAPHV et CAAPH) depuis
   le paquet embarqué, par le chemin déjà livré `harvester-capi install`, sur
   un Harvester 1.9 ou plus seulement (décision 117) ;
2. **créer N clusters RKE2 enfants** depuis des **modèles de cluster nommés**,
   enregistrés depuis le formulaire de création existant (décision 118) ;
3. **vérifier avant d'agir** : capacité (CPU, mémoire, stockage allouable)
   pour l'ensemble des N clusters, adresses des pools, version de Harvester,
   pile, droits ;
4. donner à **chaque cluster enfant sa propre identité CAPHV restreinte**, à
   la place du kubeconfig `system:masters` de la console (décision 126) ;
5. montrer les **actions filles imbriquées** sous leur action parente dans
   l'Activité, avec un vrai champ `parent` dans `actions.db` (décision 158) ;
6. supprimer proprement un cluster enfant (identité comprise) et consigner ce
   que l'exécution a créé, pour le « Défaire le déploiement » de la 2.7.0.

Registre, paquet d'artefacts et phase Artefacts restent désactivés, avec la
mention « à venir (2.6+) ».

## Architecture

```
bin/lib/capi_identity.py   identité CAPHV restreinte : règles par version de
                           CAPHV, objets RBAC, kubeconfig du compte (pur)
bin/lib/capi_cluster.py    + check_many (N demandes, une lecture), constats
                           de flotte ; check inchangé pour une demande
bin/lib/longhorn_room.py   + fits (placement cumulé de volumes, répliques
                           sur des nœuds distincts)
bin/lib/deploy_plans.py    + capi-templates.d, phases capi et clusters
bin/lib/deploy_ready.py    + constats Cluster API (décision 155)
bin/lib/deploy_chain.py    COMING réduit à registry, artefacts
bin/harvester-capi.py      + create --identity, --origin ; state ; check --many ;
                           delete retire l'identité
web/app.py                 _capi_launch (lancement hors requête), phases
                           capi et clusters, routes des modèles, champ parent
bin/harvester-plans.py     + capi-template list|show|save|delete
web/static/js/             bm-plans.js, capi-create.js, capi.js, app.js,
                           auto-history.js, dock.js, i18n.js
```

Les phases appellent ce qui existe, sans copie :

| Phase | Mécanisme réutilisé | Action fille |
|---|---|---|
| capi | `harvester-capi install --components <paquet actif> [--push-images]` (même commande que `POST /api/capi/<cluster>/install` en mode Turtles), `capi_stack.stack_status` pour la vérification | `capi-install:<cluster>` |
| clusters | `harvester-capi create --spec ... --components ...` (même commande que `POST /api/capi/<cluster>/cluster-create`), `capi_cluster.normalize`, `validate`, `check`, `cluster_state` | `capi-cluster-create:<ns>/<nom>`, une par cluster |
| suppression d'un enfant | `harvester-capi delete --name ns/nom` (route existante `DELETE /api/capi/<cluster>/cluster/<ns>/<nom>`) | `capi-cluster-delete:<ns>/<nom>` |

Le moteur reste celui de la 2.4.0 (`deploy_chain.run`, fil lancé par
`_deploy_start`, décision 146). La phase clusters est une phase à éléments
(un élément par cluster), comme la phase Terraform : ordre, dépendances,
arrêt entre éléments, partiel et reprise élément par élément sont déjà
écrits et testés.

## Contraintes communes (valeurs exactes)

- **Étapes** : `phase-capi`, `phase-clusters` ; sous-étapes `capi-providers`
  (une seule, l'action fille porte le détail) et `k8s-<nom>` pour chaque
  cluster (`item_step = "k8s-"`, `item_key = "cluster"`). `registry` et
  `artefacts` émises `skipped` avec le message `coming in 2.6+`.
- **Résultat** (ajouts au format de la 2.4.0, noms et identifiants seulement) :

  ```json
  {"id": "capi", "status": "done", "actions": ["a1b2c3d4e5f6"],
   "providers": ["bootstrap/rke2 v0.25.2", "controlPlane/rke2 v0.25.2",
                 "infrastructure/harvester v0.10.1", "addon/helm v0.6.4"],
   "installed": true}
  {"id": "clusters", "status": "partial",
   "items": [{"cluster": "site1-apps", "ref": "site1-apps/site1-apps",
              "template": "rke2-petit", "template_revision": 3,
              "namespace_created": true, "identity": "restricted",
              "created": true, "status": "done", "actions": ["..."],
              "undo": {"kind": "capi-cluster", "ref": "site1-apps/site1-apps"}}]}
  ```

  `installed` vaut `false` quand la pile était déjà prête (rien déclaré par
  cette exécution). `created` vaut `false` pour un cluster repris (même
  origine, déjà présent). `undo` n'est posé que si `created` : c'est ce que
  lira la 2.7.0, et rien d'autre n'est écrit pour elle (pas de fonction sans
  appelant, règle de la décision 151).
- **Délais** : pile, 10 minutes par défaut (`phases.capi.timeout_minutes`,
  entier de 1 à 60 ; passé à `install --timeout` en secondes) ; cluster, 30
  minutes par défaut (`phases.clusters.timeout_minutes`, entier de 5 à 180 ;
  passé à `create --timeout`). Mesures de référence (harv1, 25/09/2026) :
  pile 2 min, plan de contrôle prêt 4 min, worker 9 à 10 min. Sur un banc
  imbriqué, compter deux à trois fois plus.
- **Nombre de clusters** : 16 au plus par plan (`MAX_CLUSTERS = 16`). Création
  **séquentielle** (point à trancher 170).
- **Nom d'un cluster enfant** : `capi_cluster.NAME_RE` (40 caractères au plus,
  commence par une lettre), après rendu des variables. Namespace = nom
  (`normalize` le fait déjà) : **un cluster enfant par namespace**, imposé
  par `caphv-generate` qui nomme ses objets sans le nom du cluster.
- **Namespace des VMs** : pour un cluster à identité restreinte,
  `target_namespace` = namespace du cluster (point à trancher 164). Un
  modèle qui fixe une autre valeur est refusé (`target-namespace-forced`).
- **Origine** posée sur l'objet `Cluster` par `harvester-capi create
  --origin` : étiquettes `harvester-ops.io/plan=<plan>`,
  `harvester-ops.io/plan-template=<modèle>`, annotation
  `harvester-ops.io/plan-run=<action_id>`. Sert à la reprise (cluster
  reconnu) et au refus d'un nom déjà pris par autre chose
  (`cluster-name-taken`).
- **Harvester 1.9 ou plus** (117) : lu dans le réglage `server-version`
  (`stack_status.harvester_version`). En dessous : constat bloquant
  `capi-needs-harvester-1.9`. Plan lancé avec une installation : la version
  n'est connue qu'une fois le cluster installé, le constat est marqué
  « vérifié au début de la phase » (`capi-later`) et rejoué alors.
- **Pile déjà présente dans une autre version** que le paquet actif :
  constat bloquant `capi-version-differs`, jamais de mise à niveau silencieuse
  par la chaîne (point à trancher 171).
- **CAPHV embarqué** : v0.10.1 (`scripts/capi-components.yaml`, entrée
  `turtles-caphv`), générateur au commit `5373a21` (après v0.10.1,
  `bin/caphv-generate.PROVENANCE`). Les trois contournements restent
  (service `ingress-expose` de remplacement, retrait de l'étiquette de
  contrat, post-traitement des gabarits). La PR CAPHV #276 qui les rend
  inutiles est dans les étiquettes de préversion `v1.10.0-dev.0` (21/10/2026)
  à `v1.10.0-dev.4` (27/10/2026), dans aucune version stable au 05/11/2026
  (point à trancher 163).
- **Secrets** : les phases capi et clusters n'en demandent aucun. Le jeton de
  l'identité restreinte est créé sur le cluster Harvester, recopié par le
  générateur dans le secret d'identité du namespace du cluster, et transite
  par un fichier 0600 de l'espace de travail (`HARVESTER_OPS_WORK_DIR`),
  effacé dans un `finally`. Jamais dans argv, une étape, le résultat,
  `actions.db`, stdout ou stderr.
- **Identité de la personne** : figée au déclenchement (kubeconfig résolu
  dans la requête, `run.cluster_user`), comme en 2.4.0. Toutes les écritures
  de la console sur le cluster Harvester (namespace, objets RBAC de
  l'identité, manifestes du cluster) partent avec elle. Écrire des rôles
  exige de détenir les droits qu'ils donnent : la vérification `rights`
  le contrôle avant (accès évalués par le serveur, `SelfSubjectAccessReview`).
- **Rôles console** : modèles de cluster, lecture = lecteur, écriture =
  administrateur (comme les modèles de déclaration, mêmes préfixes de
  routes) ; appliquer un plan à phases Cluster API = opérateur (règle 148),
  à condition que l'identité Harvester de la personne ait les droits
  contrôlés par `rights`.
- **Interface** : `FloatingPanels`, `.sub-tabs-inline`, `.btn`, `.card` ;
  `escapeHtml` sur tout nom ; bulle d'aide sur chaque contrôle ; cinq langues,
  écart de `test_i18n` à zéro ; SSE par `SSEReconnect.connect()`.
- **Contenu public** : ni tiret cadratin ni flèche Unicode ; aucune mention
  d'outillage dans les commits, le CHANGELOG et la doc.

## Constats préalables (faits le 05/11/2026)

### Ce que CAPHV v0.10.1 fait avec son identité

Lecture de la source à l'étiquette `v0.10.1` (dépôt CAPHV, clientset typé
`HarvesterClient`, tous les appels relevés hors tests). Le cluster Harvester
étant aussi le cluster de gestion, « identité » désigne le kubeconfig du
secret désigné par `HarvesterCluster.spec.identitySecret`, relu à chaque
réconciliation (`util/util.go:115`).

| Ressource (groupe) | Portée | Verbes | Où (source v0.10.1) |
|---|---|---|---|
| `virtualmachines` (kubevirt.io) | namespace des VMs | get, create, delete | `harvestermachine_controller.go:381, 761, 1611, 1628` |
| `virtualmachineinstances` (kubevirt.io) | namespace des VMs | get | `getIPAddressesFromVMI` (`:404`) |
| `secrets` | namespace des VMs | get, create, update, delete | `:952 à 965`, `:1594` (cloud-init) ; `util/cloud_provider.go:136, 160` (jeton du fournisseur de cloud) |
| `services` | namespace des VMs | get, create, update, delete | `harvestercluster_controller.go:378, 430, 573, 758` |
| `serviceaccounts` | namespace des VMs | get (create à `:81` jamais appelé : compte créé d'avance, relu en tâche 2) | `util/cloud_provider.go:69, 81, 126` |
| `persistentvolumeclaims` | namespace des VMs | list, delete | `harvestermachine_controller.go:1661, 1674` |
| `loadbalancers` (loadbalancer.harvesterhci.io) | namespace des VMs | get, create, delete | `harvestercluster_controller.go:693, 722, 1398` |
| `virtualmachineimages` (harvesterhci.io) | namespace de l'image | list | `harvestermachine_controller.go:843` |
| `keypairs` (harvesterhci.io) | namespace de la clé | get | `:874` |
| `deployments` (apps) | `harvester-system` | get (`harvester`) | `harvestercluster_controller.go:1327` |
| `services` | `kube-system` | get (`ingress-expose` ; `rke2-traefik` pour #276) | `util/cloud_provider.go:166` |
| `namespaces` | cluster | get (create si absent, jamais appelé : namespace créé par la console, relu en tâche 2) | `harvestercluster_controller.go:314, 317` |
| `ippools` (loadbalancer.harvesterhci.io) | cluster | get, update (allocation des adresses des machines dans `status.allocated` par `Update`) | `harvestermachine_controller.go:1404, 1447, 1504, 1528` |
| `ippools` | cluster | **delete inconditionnel** de `<ns>-<nom>-ippool` à la suppression | `harvestercluster_controller.go:774` |
| `ippools` | cluster | create, delete (pools en ligne, modes que la console n'emploie pas) | `:1205, 739, 795, 1453` |
| `nodes` | cluster | list (domaines de panne, échec seulement journalisé) | `failure_domains.go:89` |
| `clusterrolebindings` (rbac) | cluster | get, create, liaison au ClusterRole `harvesterhci.io:cloudprovider` | `util/cloud_provider.go:92, 116` |

Conséquences pour l'identité restreinte (tâche 2) :

- **Le delete de `<ns>-<nom>-ippool` est inconditionnel** et un refus
  (Forbidden) n'est pas un NotFound : sans ce droit, CAPHV relance la
  suppression du cluster indéfiniment (`RequeueAfter`), le cluster ne
  disparaît jamais. Le nom est calculé par `GenerateRFC1035Name`
  (`util/util.go:190` : jonction par `-`, minuscules, caractères hors
  `[a-z0-9-]` remplacés, tirets de bord retirés, préfixe `a-` si le premier
  caractère n'est pas une lettre, coupé à 63) ; la console le recalcule à
  l'identique pour un `resourceNames`.
- **Les liaisons au rôle du fournisseur de cloud** : créer une
  `ClusterRoleBinding` vers `harvesterhci.io:cloudprovider` demanderait le
  verbe `bind` sur ce rôle, donc le pouvoir de le donner à n'importe quel
  compte. CAPHV fait d'abord un `get` et s'arrête si la liaison existe : la
  console crée elle-même le compte `<nom>` et la liaison `<nom>` (noms
  attendus par `GetCloudConfigB64(..., scope.Cluster.Name, TargetNamespace)`,
  `harvestercluster_controller.go:860`), avec l'identité de la personne.
  L'identité restreinte n'a alors besoin que de `get` (point à trancher 166).
  Bénéfice de plus : ces objets portent l'étiquette de la console et
  partent avec le cluster, alors qu'aujourd'hui CAPHV les laisse (relevé
  harv1 en lecture le 05/11 : aucune liaison `cloudprovider` restante, à
  revérifier après chaque essai).
- **Les nœuds du cluster enfant** (`Nodes().Patch`, pods etcd) passent par le
  kubeconfig du cluster enfant, pas par l'identité Harvester : hors du rôle.
- **Rien d'autre que le tableau** : une version de CAPHV non relue n'a pas de
  règles ; la console refuse alors l'identité restreinte
  (`identity-rules-unknown`), plutôt que de deviner.

### La capacité de harv1 (lecture seule, 05/11/2026)

Harvester v1.9.0, un nœud, allouable 7020m CPU et 62,6 Gio, demandes des pods
6,08 CPU et 26,3 Gio, surengagement `{"cpu":1600,"memory":150,"storage":200}`.
VMs : `mlm` en marche (`RerunOnFailure`), `rancher-manager` et les 15 autres
arrêtées ce jour-là. Pile Cluster API présente et prête (RKE2 v0.25.2,
CAPHV v0.10.1, CAAPH v0.6.4, cœur v1.13.3). Un cluster 1+1 de 2 vCPU et
4 Gio demande environ 6,0 Gio et 0,25 CPU à l'ordonnanceur
(`capi_cluster.requests_needed`) et 8 Gio de mémoire réelle d'invité.

### Le paquet et les versions

`scripts/capi-components.yaml` : RKE2 v0.25.2, CAPHV v0.10.1 (contournement
`ingress-expose`), CAAPH v0.6.4 (`optional: true`), versions Kubernetes
proposées v1.34.11 (éprouvée), v1.35.8, v1.36.4, v1.31.14 (éprouvée). La
création d'un cluster passe par le kubeconfig donné à `caphv-generate`
(`--harvester-kubeconfig`, `capi_cluster.to_argv`) : c'est le seul point à
changer pour l'identité restreinte (`render(spec, identity_kubeconfig)` dans
`bin/harvester-capi.py` prend déjà le chemin en paramètre).

## Tâche 1 : actions imbriquées (`parent`)

Fichiers : `web/app.py` (`ActionRun.__init__`, `to_dict`, schéma et
`_actions_persist`, `_actions_db_recent`, `api_activity`, rétention des 500
lignes, `_cli_action`, `track_action`, `_tf_decl_launch`, `_bm_batch_launch`,
`_bm_track`), `web/static/js/app.js` (Activité), `web/static/js/dock.js`,
`web/static/js/auto-history.js`, `web/static/js/i18n.js`,
`tests/api/test_actions_persistence.py`, `tests/api/test_activity_filters.py`,
nouveau `tests/api/test_action_parent_250.py`, nouveau
`tests/e2e/test_activity_nested_250.py`.

- `ActionRun.parent = None` ; colonne additive `parent TEXT` (migration
  `ALTER TABLE ... ADD COLUMN`, comme `undo`), index `actions_parent(parent)`.
  `to_dict` rend `parent` et, pour une action en mémoire, `children`
  (identifiants, ordre de lancement).
- Le parent se pose **à la création** de l'action fille, avant qu'elle soit
  visible : paramètre `parent=None` ajouté à `_cli_action` (via `setup`),
  `track_action`, `_tf_decl_launch`, `_bm_batch_launch`, `_bm_track`. Posé par
  la chaîne `deploy:` sur toutes ses filles, et par la série
  `baremetal-batch:` sur ses installations (trois niveaux possibles :
  `deploy` > `baremetal-batch` > `baremetal-install`). Les étapes gardent
  l'identifiant de la fille (lien existant, inchangé).
- `GET /api/activity` : sans filtre, ne rend que les actions de premier
  niveau, chacune avec `children_count` ; `?parent=<id>` rend les filles
  d'une action (même forme), avec contrôle d'identifiant (12 hexadécimaux).
  Avec un filtre (`cluster`, `status`, `action`, `q`), liste à plat comme
  aujourd'hui, chaque fille portant `parent` (lien « fait partie de »).
- Rétention : la purge des 500 lignes compte les actions de premier niveau
  et supprime les filles avec leur parent, jamais une fille seule d'un
  parent gardé.
- Activité : une action à filles se déplie (bouton à bulle d'aide), filles
  chargées à la demande et rendues avec le même gabarit de ligne, en retrait.
  Dock : le parent seul, avec en sous-titre la fille en cours (nom
  d'action, échappé). Historique de Bare-metal (`auto-history.js`) : mêmes
  règles. Point à trancher 174.

Tests (tableaux) : migration d'une base 2.4.0 (lignes gardées, `parent`
NULL, elles restent de premier niveau) ; parent posé par chaque lanceur ;
`?parent=` d'un identifiant invalide refusé (400) ; liste sans filtre sans
filles, avec `children_count` juste ; filtre `q` qui trouve une fille et rend
son `parent` ; purge : 501 parents dont un avec trois filles, les filles du
parent gardé restent ; échappement d'un nom de fille hostile dans l'Activité
et le dock. e2e sur routes simulées : dépliage, trois niveaux, dock.
Mutations : parent posé après l'ajout à `ACTIONS` (une lecture concurrente
voit une fille orpheline : le test de course doit échouer), purge qui compte
les filles, `escapeHtml` retiré du nom de fille.

Vérification réelle : par la tâche 7 (chaîne réelle sur le banc), Activité et
dock relus pendant et après, puis après un redémarrage de la console de dev
(`systemctl --user restart harvester-ops-dev`) : l'imbrication revient de la
base.

## Tâche 2 : identité CAPHV restreinte par cluster (`capi_identity`)

Fichiers : nouveau `bin/lib/capi_identity.py`, `bin/harvester-capi.py`
(`cmd_create`, `cmd_delete`, `cmd_check`), `bin/lib/capi_cluster.py`
(`GENERATED_KINDS`, constats), nouveau `tests/api/test_capi_identity_250.py`,
`tests/api/test_capi_cli.py`, `tests/api/test_capi_cluster_logic.py`.

`capi_identity` (bibliothèque standard, pur) :
- `RULES = {"v0.10.1": {...}}` : le tableau des constats préalables, par
  version de CAPHV, chaque règle avec sa source (`"src":
  "harvestermachine_controller.go:761"`). Clé = version du fournisseur
  `infrastructure/harvester` du paquet actif (`turtles.json`).
- `rfc1035_name(parts)` : recopie de `GenerateRFC1035Name` (source citée).
- `docs(spec, version) -> [dict]` : objets à poser avec l'identité de la
  personne, tous étiquetés `harvester-ops.io/generated: "true"`,
  `harvester-ops.io/caphv-cluster: <nom>`,
  `harvester-ops.io/caphv-namespace: <ns>` :
  - `ServiceAccount caphv-identity` dans `<ns>` ;
  - `Role caphv-identity` dans `<ns>` (VMs, VMI, secrets, services, comptes,
    PVC, load balancers) et sa `RoleBinding` ;
  - `Role caphv-identity-<ns>` dans le namespace de l'image (list
    `virtualmachineimages`) et dans celui de la clé (get `keypairs`,
    `resourceNames` = la clé), fusionnés si c'est le même namespace ;
  - `Role caphv-identity-<ns>` dans `harvester-system` (get `deployments`
    `harvester`) et dans `kube-system` (get `services` `ingress-expose` ;
    `rke2-traefik` n'est lu qu'à partir de la PR #276, relu en tâche 2),
    avec leurs liaisons ;
  - `ClusterRole` et `ClusterRoleBinding` `harvester-ops-caphv-<ns>` :
    get `namespaces` (`<ns>`), get et update `ippools` (`ip_pool` et
    `ip_pool_refs`), delete `ippools` (`rfc1035_name([ns, nom, "ippool"])`),
    list `nodes`, get `clusterrolebindings` (`<nom>`) ;
  - `ServiceAccount <nom>` dans `<ns>` et `ClusterRoleBinding <nom>` vers
    `harvesterhci.io:cloudprovider` (compte du fournisseur de cloud, créés
    d'avance, point à trancher 166) ;
  - `Secret caphv-identity-token` de type
    `kubernetes.io/service-account-token` (point à trancher 167).
- `kubeconfig(server, ca_b64, token) -> str` : un contexte, un utilisateur à
  jeton ; `server` = `https://<adresse de rke2-traefik>:6443` (adresse lue
  comme `capi_stack.shim_state`, jamais l'URL d'un relais Rancher que porte
  le kubeconfig d'une session SSO).
- `access_reviews(spec, version) -> [(namespace, verb, group, resource, name)]`
  : ce que la personne doit pouvoir accorder ; sert la vérification
  `rights` (tâche 4).

`harvester-capi` :
- `create --identity restricted|console` (`restricted` par défaut, point à
  trancher 165) : namespace créé s'il manque (étiqueté `MANAGED`, comme
  aujourd'hui par le rendu), `kube.apply(capi_identity.docs(...))`, attente
  du jeton peuplé (30 s au plus, relu toutes les secondes), kubeconfig écrit
  en 0600 dans `HARVESTER_OPS_WORK_DIR`, `render(spec, ce_fichier)`, fichier
  effacé dans un `finally` ; puis la suite actuelle (application, attente).
  Annulation ou échec avant l'application des manifestes : objets de
  l'identité retirés avec le namespace créé (`_undo` étendu).
- `create --origin plan=<p>,template=<m>,run=<id>` : étiquettes et annotation
  d'origine posées sur le `Cluster` (`cc.postprocess` reçoit l'origine en
  argument, jamais par la demande, qui reste une liste blanche).
- `delete` : **ordre imposé** : le `Cluster` d'abord, attente de sa
  disparition (CAPHV a besoin de son identité pour supprimer VMs, PVC, load
  balancer et pool), puis les objets d'identité (sélecteur
  `harvester-ops.io/caphv-cluster=<nom>,harvester-ops.io/caphv-namespace=<ns>`,
  `ClusterRole` et `ClusterRoleBinding` compris, objets de portée cluster que
  la suppression du namespace n'emporte pas), puis le reste comme
  aujourd'hui (gabarits générés, namespace `MANAGED`).
- `check` ajoute `identity-rules-unknown` (bloquant si `--identity
  restricted` et version sans règles) et `target-namespace-forced`.
- Les clusters existants à identité de la console ne changent pas (point à
  trancher 168) ; `details` (route existante
  `/api/capi/<cluster>/cluster/<ns>/<nom>/details`) dit `identity:
  restricted|console` d'après la présence du compte étiqueté.

Tests (tableaux) : `rfc1035_name` contre des cas relevés de la source
(majuscules, `.`, chiffre en tête, 70 caractères) ; `docs` pour une demande
minimale, avec image et clé dans le même namespace et dans deux namespaces,
avec trois pools ; chaque règle du tableau présente avec ses verbes exacts,
aucun `*`, aucun `create` de `clusterrolebindings`, aucun `bind` ; version
inconnue = refus ; `kubeconfig` sans l'URL du kubeconfig source ; `create`
simulé : jeton absent du stdout, du stderr et des étapes, fichier effacé
après succès, échec du rendu, annulation ; `delete` simulé : ordre `Cluster`
puis identité, objets de portée cluster supprimés, objets d'un voisin
gardés. Mutations nommées : `M-ippool-delete` (droit de suppression du pool
généré retiré : le test de règle doit échouer), `M-bind` (le rôle accorde
`create clusterrolebindings` au lieu de la création d'avance),
`M-delete-order` (identité supprimée avant le `Cluster`), `M-token-log`
(jeton écrit dans une étape).

Vérification réelle (banc de la tâche 7) : création 1+1 avec identité
restreinte ; `kubectl auth can-i --list
--as=system:serviceaccount:<ns>:caphv-identity -n <ns>` identique au tableau ;
`can-i get virtualmachines -n default` et `can-i create clusterrolebindings`
à `no` ; journal du contrôleur CAPHV
(`kubectl logs -n caphv-system deploy/<contrôleur> --since=30m`) sans aucun
`forbidden` pendant la création et la suppression ; suppression complète
(aucune VM, PVC, `ClusterRole`, `ClusterRoleBinding` étiquetés restants ; objet
LoadBalancer en DHCP laissé : limite connue harvester/harvester#11767, notée).
**Mutation réelle**, une fois sur le banc : droit `delete ippools` retiré du
`ClusterRole` d'un cluster d'essai, suppression bloquée constatée
(`Cluster` en `Deleting` après 10 minutes), droit remis, suppression qui
aboutit : preuve que la règle est nécessaire.

## Tâche 3 : modèles de cluster (`capi-templates.d`)

Fichiers : `bin/lib/deploy_plans.py`, `web/app.py` (routes),
`bin/harvester-plans.py`, `tests/api/test_deploy_plans_240.py` (cas
ajoutés), nouveau `tests/api/test_capi_templates_250.py`,
`tests/api/test_harvester_plans_cli_240.py` (cas ajoutés).

- Constantes : `CAPI_TEMPLATE_DIR = "capi-templates.d"`,
  `CAPI_TEMPLATE_FORBIDDEN = ("name", "namespace", "target_namespace")`
  (donnés par l'entrée du plan et la règle du namespace).
- Modèle : `{name, revision, description, variables, spec}` ; `spec` = la
  demande du formulaire de création (`capi_cluster.KNOWN` moins les clés
  interdites), chaînes à variables `{{nom}}`. Rangement, révision, 0600,
  0700, remplacement atomique : les fonctions communes de `deploy_plans`
  (`_save`, `_load`, `_list`, `_delete`) avec `check_capi_template`.
- `check_capi_template(doc)` : clés inconnues refusées (liste blanche de
  `capi_cluster`) ; variables utilisées déclarées ou intégrées (`cluster`,
  `vip`) ; valeurs sans variable passées par `capi_cluster.validate` sur une
  demande d'essai (nom factice) pour refuser tôt ce qui ne passerait jamais ;
  `fleet_repo` contrôlé par la règle d'URL des modèles de déclaration
  (`_URL_USERINFO_RE`, `_URL_QUERY_SECRET_RE` : pas d'identifiant dans une
  URL).
- `render_capi_template(t, values, name) -> spec` : `render_advanced` des
  profils (jetons neutres), entiers pour `cp_replicas`, `worker_replicas`,
  `cpu`, `cni_mtu` (type tiré de la liste de `capi_cluster.normalize`),
  puis `name`, `namespace = name`, `target_namespace = name`, puis
  `capi_cluster.normalize`. Variable absente : erreur avec son chemin
  (`clusters[site1-apps].template[rke2-petit].spec.network`).
- Routes, mêmes formes, rôles et limites que `tf-templates` :
  `GET /api/baremetal/capi-templates`, `GET/PUT/DELETE
  /api/baremetal/capi-templates/<nom>` (`30/minute` en écriture, 409
  `{current_revision}`), `DELETE` refusé (409) si un plan enregistré le
  désigne.
- CLI : `harvester-plans capi-template list | show <nom> | save <nom> --file
  m.yaml [--revision N] | delete <nom>`.

Tests (tableaux) : un cas par refus (clé inconnue, clé interdite, variable
non déclarée, `cpu` à 0 sans variable, URL à identifiant) ; rendu : `{{n}}`
seul sur `cpu` devenu entier, valeur à syntaxe YAML gardée telle quelle,
variable absente avec chemin, nom rendu de 41 caractères refusé ; révision
périmée ; suppression d'un modèle désigné refusée ; CLI. Mutations : clé
`target_namespace` acceptée, rendu par substitution puis relecture YAML.

Vérification réelle : modèle `rke2-banc` enregistré depuis la fenêtre de
création du banc (tâche 8), relu par la CLI.

## Tâche 4 : capacité, adresses et droits pour N clusters

Fichiers : `bin/lib/capi_cluster.py` (`check_many`), `bin/lib/longhorn_room.py`
(`fits`), `bin/harvester-capi.py` (`check --many`, lectures Longhorn et
images ajoutées à `inventory`), `tests/api/test_capi_cluster_logic.py`,
`tests/api/test_longhorn_room.py`, nouveau `tests/api/test_capi_fleet_250.py`.

- `inventory` lit en plus : `nodes.longhorn.io`, réglages Longhorn
  `storage-over-provisioning-percentage` et `storage-minimal-available-percentage`
  (même lecture que `_longhorn_numbers` du transfert), `storageclasses`
  complètes, et pour chaque image `status.storageClassName` (jamais un nom
  fabriqué, règle apprise : `lh-<uuid>` pour les images récentes).
- `longhorn_room.fits(lh_nodes, over, minimal, volumes) -> {"fits": bool,
  "short": [...]}` : placement glouton (le plus gros volume d'abord, chaque
  réplique sur le nœud qui a le plus de place parmi ceux qui n'en portent
  pas déjà une), sur la place par disque que calcule `storage_room` (calcul
  prudent, celui de la création de VM : les disques des VMs s'écrivent).
  Un volume par disque de VM : racine (`disk_size`, classe de l'image),
  disque supplémentaire (`extra_disk_size`, `extra_disk_class`).
- `check_many(specs, facts) -> findings` : `check(spec, facts)` pour chaque
  demande (règles du formulaire, inchangées), puis les constats de flotte :

| Code | Niveau | Règle |
|---|---|---|
| `fleet-name-twice` | bloquant | deux entrées rendent le même nom |
| `fleet-cpu-short` | bloquant | somme de `requests_needed` > CPU libre (`free_cpu_m`) |
| `fleet-memory-short` | bloquant | somme de `requests_needed` > mémoire libre |
| `fleet-storage-short` | bloquant | `fits` faux ; détail : volume, classe, nœuds |
| `fleet-replicas-nodes` | bloquant | une classe demande plus de répliques que de nœuds Longhorn ordonnançables (3 répliques sur un nœud) |
| `fleet-pool-short` | bloquant | par pool, somme de `addresses_needed` des clusters qui y puisent > places d'après `status.allocated` (`pool_info.available`, jamais le compteur `available` de Harvester) |
| `fleet-dhcp-leases` | information | une adresse DHCP par cluster pour le répartiteur de l'API (mode DHCP, `endpoint-dhcp`) |
| `fleet-sizes` | information | totaux : VMs, vCPU, mémoire d'invité, disques, adresses |

  Les constats `cpu-short` et `memory-short` de `check` restent des
  avertissements pour le formulaire (une personne décide) ; la chaîne, qui
  tourne sans surveillance, bloque sur les constats de flotte (point à
  trancher 169). Un plan d'un seul cluster passe aussi par `check_many` :
  une seule règle de capacité pour la chaîne.
- Services LoadBalancer prévus par des artefacts : zéro en 2.5.0 (phase
  Artefacts en 2.7.0), le compte est déjà un paramètre de `check_many`
  (`extra_addresses`), à zéro.
- **Droits** (`rights`, bloquant) : pour chaque objet que posera la chaîne
  (namespace, objets de `capi_identity.docs`, manifestes du cluster), un
  `SelfSubjectAccessReview` avec l'identité de la personne
  (`kubectl auth can-i <verbe> <ressource> -n <ns> [--subresource]`, lancé
  par `harvester-capi check`), constat `rights-missing` avec la liste.
- `harvester-capi check --many --spec F` : `F` est une liste JSON de demandes ;
  une seule lecture d'`inventory` ; sortie `{findings, blocked, amount}`.

Tests (tableaux, cas à plusieurs clusters) : trois clusters qui tiennent ;
le troisième qui ne tient plus en mémoire, en CPU, en stockage (un nœud,
deux nœuds, trois nœuds dont un fermé) ; trois répliques sur un nœud ; deux
clusters sur le même pool qui le dépassent ensemble mais pas seuls ; pool
au compteur `available` faux (24 sur 16, cas réel de harv1) ; même nom deux
fois ; demandes valides une à une mais invalides ensemble ; droits
manquants. Mutations : `M-sum` (capacité contrôlée cluster par cluster au
lieu de la somme : le cas « deux qui dépassent ensemble » doit échouer),
`M-same-node` (deux répliques placées sur le même nœud), `M-available`
(compteur de Harvester au lieu de `status.allocated`).

Vérification réelle : `harvester-plans check` sur le banc avec N = 1 (passe),
puis N choisi pour dépasser la mémoire libre (bloqué, rien créé) ; sur harv1
en lecture seule, N = 1 et N = 6.

## Tâche 5 : format des plans 2.5 et constats de disponibilité Cluster API

Fichiers : `bin/lib/deploy_plans.py`, `bin/lib/deploy_chain.py`,
`bin/lib/deploy_ready.py`, `tests/api/test_deploy_plans_240.py`,
`tests/api/test_deploy_chain_240.py`, `tests/api/test_deploy_ready_240.py`,
nouveau `tests/api/test_deploy_plans_250.py`, jeux d'essai
`tests/fixtures/deploy/` (service `rke2-traefik`, `capiproviders`) relevés
sur harv1 en lecture.

- `AVAILABLE = ("readiness", "terraform", "capi", "clusters")` ;
  `deploy_chain.COMING = ("registry", "artefacts")`, message
  `coming in 2.6+` (les deux modules, même chaîne).
- Phases normalisées :

  ```yaml
  capi: {enabled: true, timeout_minutes: 10}
  clusters:
    enabled: true
    timeout_minutes: 30
    items:
      - {template: rke2-petit, name: "{{cluster}}-apps"}
      - {template: rke2-petit, name: "{{cluster}}-outils", depends_on: ["{{cluster}}-apps"]}
    # raccourci de saisie, développé en `items` à l'enregistrement :
    # {template: rke2-petit, count: 3, name_pattern: "{{cluster}}-k8s-{{n}}"}
  ```

  `n` est une variable intégrée de la seule forme `name_pattern` (1 à
  `count`). `clusters` activé sans `capi` activé : accepté (pile déjà
  présente), la vérification `capi-stack-missing` le dira au lancement.
  `depends_on` désigne un nom d'entrée placé avant (règle de la phase
  Terraform). 16 entrées au plus.
- **Plans écrits en 2.4.0** : une phase `capi` ou `clusters` désactivée y
  garde des clés libres (format non figé alors). Désactivée, elle reste
  acceptée et ses clés inconnues sont ignorées et retirées au prochain
  enregistrement, avec un avertissement dans la réponse ; activée, la règle
  stricte s'applique.
- `deploy_ready.evaluate(reads, expected_nodes, phases)` : si `capi` ou
  `clusters` est dans `phases`, lectures ajoutées `server_version`,
  `traefik` (`kube-system/rke2-traefik`) et `capi_providers`, codes
  `harvester-too-old` (moins de v1.9), `rke2-traefik-no-address` (aucune
  adresse dans `status.loadBalancer.ingress`, ce que lit CAPHV), et
  `turtles-not-ready` (aucun `CAPIProvider` lisible, ou cœur pas prêt :
  règle de `capi_stack.stack_status`, appelée, pas recopiée).

Tests : un cas par refus nouveau (délai hors bornes, nom rendu invalide,
`count` à 0 et à 17, `name_pattern` sans `{{n}}`, `depends_on` vers une
entrée placée après) ; plan 2.4.0 relu (fichier réel de la tâche 6 de la
2.4.0 recopié en jeu d'essai) ; chaque code de disponibilité, plus « tout
prêt » ; codes Cluster API absents si la phase est désactivée. Mutations :
`M-coming` (`capi` laissé dans `COMING` : la chaîne doit échouer au test de
phase disponible), `M-ready-copy` (règle du cœur recopiée au lieu
d'appeler `stack_status` : test d'identité de règle).

Vérification réelle : `harvester-plans check` sur harv1 (pile prête, constats
vides) ; sur le banc avant la phase capi (`turtles-not-ready` absent, pile
manquante dite par `capi-stack-missing`).

## Tâche 6 : phases capi et clusters dans la console

Fichiers : `web/app.py` (`_cli_action`, nouveau `_capi_launch`,
`_deploy_phases`, nouveaux `_deploy_capi_phase` et `_deploy_clusters_phase`,
`_deploy_prepare` et ses constats, `_deploy_previous`),
`tests/api/test_deploy_runner_240.py`, `tests/api/test_deploy_routes_240.py`,
nouveau `tests/api/test_deploy_capi_250.py`.

- **Lancement hors requête** : `_capi_launch(cluster, kc, sub, args, spec=None,
  parent=None, cluster_user=None, label=...) -> (run, err)`, qui ne lit ni
  `request`, ni `current_user()`, ni `current_cluster_identity()`, et
  construit la même commande que `_capi_base` (même script, même
  `--kubeconfig`). `_capi_base` et les routes existantes l'emploient
  (comportement et réponses inchangés : `test_capi_cli.py`,
  `test_capi_create_view.py` passent sans changement). Même découpe que
  `_tf_decl_launch` en 2.4.0.
- **Phase capi** (`_deploy_capi_phase`) : lecture `harvester-capi status
  --components <paquet>` ; pile prête aux versions du paquet = `done`,
  `installed: false`, aucune action fille ; sinon `harvester-capi install
  --cluster C --components <paquet actif> [--push-images si des nœuds sont
  déclarés]`, attente par `_deploy_wait_child`, relecture. `verify` : pile
  prête aux versions du paquet. Essai (`dry_run`) : lecture seule, message
  `would declare <n> provider(s)`.
- **Phase clusters** (`_deploy_clusters_phase`), un élément par entrée :
  1. au début de la phase, rendu de toutes les entrées et `check --many`
     rejoué (les objets que la phase Terraform vient de créer existent
     maintenant) ; un constat bloquant arrête la phase avant toute écriture,
     codes consignés ;
  2. par élément : `Cluster` déjà présent avec cette origine (plan et modèle)
     = repris (`created: false`), attente reprise s'il n'est pas prêt et pas
     en échec ; présent sans cette origine = `cluster-name-taken` ; présent
     en échec (`cluster_state.failed`) = `cluster-failed`, à supprimer puis
     reprendre (point à trancher 176) ; absent = `create --identity
     restricted --origin ... --timeout <s>`, action fille, attente ;
  3. `verify_item` : `harvester-capi state --name ns/nom --json` (nouvelle
     sous-commande, une lecture, `cluster_state` existant) : présent, même
     origine, `ready`.
  Arrêt de la chaîne : la création en cours va à son terme (règle des
  éléments du moteur), les suivantes sont `skipped` ; la personne peut
  annuler l'action fille elle-même depuis le dock, ce qui supprime ce
  cluster (comportement existant de `create`). Point à trancher 175.
  Essai : `create --dry-run` (contrôle et rendu, sans identité créée : le
  rendu reçoit un kubeconfig factice sans jeton), aucun objet écrit.
- **Constats de `check`** ajoutés (codes, niveau, phase) : `capi-bundle-missing`
  (bloquant, aucun paquet actif), `capi-bundle-no-turtles` (bloquant),
  `capi-needs-harvester-1.9` (bloquant), `capi-later` (information, plan
  avec installation), `capi-version-differs` (bloquant),
  `capi-stack-missing` (bloquant si `clusters` activé sans `capi` et pile
  absente), `capi-template-missing` (bloquant), `cluster-var-missing`
  (bloquant, avec chemin), `cluster-name-taken` (bloquant),
  `cluster-exists` (information : repris), `identity-rules-unknown`
  (bloquant), puis tous ceux de `check --many` (tâche 4) rangés dans la
  phase `clusters`. Les constats qui dépendent d'objets créés par la phase
  Terraform (réseau, image, pool désignés par variable et absents) passent
  en `refs-later` (information) et sont rejoués au début de la phase.
- **Résumé** de `check` : `clusters: [{name, template, vms, exists}]`,
  `estimate_minutes` : pile 2 si absente, puis par cluster 5 + 10 si au moins
  un worker (mesures de harv1), en séquence.
- **Identité de la console pour un cluster créé par la chaîne** : comme en
  2.4.0 (`_deploy_console_kc`), les phases capi et clusters d'un cluster que
  la chaîne vient d'installer emploient le kubeconfig de la console.

Tests du déroulé (outil simulé comme dans `test_deploy_runner_240.py`) :
chaîne Terraform, capi, clusters ; pile déjà prête (aucune action fille) ;
pile d'une autre version (bloqué avant toute action) ; trois clusters dont
le deuxième échoue (troisième fait, phase `partial`) ; dépendance sur un
cluster en échec (sauté) ; reprise : cluster fait revérifié et sauté,
cluster laissé après délai repris et attendu, cluster en échec en
`cluster-failed` ; arrêt pendant le premier cluster (deuxième `skipped`) ;
essai sans écriture ; `parent` posé sur chaque fille ; `undo` présent
seulement pour `created: true` ; aucun jeton ni chemin dans le résultat et
`actions.db`. Tests des routes : chaque constat nouveau ; plan à phase
clusters par un lecteur refusé, par un opérateur accepté. Mutations :
`M-recheck` (pas de `check --many` rejoué au début de la phase : le cas
« réseau créé par Terraform » doit échouer), `M-adopt` (cluster d'une autre
origine repris), `M-request-ctx` (`_capi_launch` qui relit
`current_cluster_identity()` : test sans contexte de requête).

Vérification réelle : tâche 8.

## Tâche 7 : ligne de commande

Fichiers : `bin/harvester-plans.py`, `bin/harvester-capi.py` (docstring et
`main`), `tests/api/test_harvester_plans_cli_240.py`,
`tests/api/test_capi_cli.py`.

- `harvester-plans` : `capi-template ...` (tâche 3) ; `check` et `apply`
  couvrent les nouvelles phases sans option nouvelle ; codes inchangés.
- `harvester-capi` : `create [--identity restricted|console] [--origin
  k=v,...]`, `check --many`, `state --name ns/nom [--json]`, `delete`
  (identité retirée). Analyse `allow_abbrev=False` comme les autres outils.

Tests : chaque sous-commande en tableau ; `--origin` à clé inconnue refusé ;
`state` d'un cluster absent (code 1, JSON) ; `--identity` inconnue refusée.
Mutation : `--origin` qui accepte une valeur non RFC 1123.

Vérification réelle : `harvester-plans apply <plan> --cluster <banc>
--dry-run`, puis `apply` par la CLI pour un cluster sur le banc.

## Tâche 8 : interface

Fichiers : `web/static/js/bm-plans.js`, `web/static/js/capi-create.js`,
`web/static/js/capi.js` (détails d'un cluster : ligne d'identité),
`web/static/js/i18n.js`, `tests/api/test_bm_plans_ui_240.py`,
`tests/api/test_capi_views_i18n.py`, nouveaux
`tests/e2e/test_bm_plans_capi_250.py` et `tests/e2e/test_capi_template_250.py`,
`tests/e2e/test_capi_create_view.py` (cas ajoutés).

- **Onglet Plans** : les cartes Cluster API et Clusters deviennent actives
  (badge « à venir » retiré ; Registre et Artefacts gardent « à venir
  (2.6+) »). Carte Cluster API : interrupteur, délai, versions du paquet
  actif (lecture `GET /api/capi/bundles`), bulle qui dit que la pile n'est
  jamais mise à niveau par un plan. Carte Clusters : liste d'entrées
  (modèle choisi dans une liste, nom à variables, dépendance), bouton
  « Plusieurs » qui ouvre le raccourci nombre et motif, ordre par les
  boutons monter/descendre existants de la phase Terraform.
- **Modèles de cluster** : sous-section de l'onglet Plans, à côté des
  modèles de déclaration (même patron). L'éditeur est le **formulaire de
  création existant** (`CapiCreate.formHtml`) en mode modèle : chaque champ
  de référence (image, réseau, réseaux supplémentaires, clé, pool, classe
  du disque supplémentaire) propose les valeurs d'un cluster choisi plus
  « Variable... », qui change le champ en saisie `{{nom}}` ; nom, namespace
  et namespace des VMs masqués (donnés par le plan). Point à trancher 173.
- **Fenêtre de création** (`capi-create.js`) : bouton « Enregistrer comme
  modèle » (administrateur ; grisé avec la raison sinon), qui ouvre la
  sous-section avec la demande courante. Le namespace des VMs est prérempli
  du namespace du cluster et grisé avec une bulle (identité restreinte,
  point à trancher 164).
- **Fenêtre Déployer** : constats Cluster API et Clusters regroupés par phase,
  bloquants d'abord ; Résumé avec les clusters (nom, modèle, VMs, déjà
  présent) et la durée estimée.
- **Suivi** : actions imbriquées (tâche 1) ; dans l'Historique de
  Bare-metal, un cluster enfant d'une action `deploy:` montre un lien vers
  Cluster API > Clusters.
- **Détails d'un cluster** (onglet Clusters) : « Identité : restreinte » ou
  « Identité : console (tous droits) » avec bulle d'aide.

Tests : i18n des nouvelles clés dans les cinq langues ; e2e sur routes
simulées : édition d'un plan à deux clusters et raccourci « Plusieurs »,
enregistrement (409 montré), modèle créé depuis la fenêtre de création puis
édité avec une variable, fenêtre Déployer avec constats de flotte (Lancer
grisé avec la raison), Activité dépliée, échappement d'un nom de modèle et
d'un nom de cluster hostiles. Mutations : `M-launch` (Lancer actif malgré un
constat de flotte), `M-escape` (`escapeHtml` retiré du nom de cluster),
`M-template-mode` (nom du cluster laissé visible dans l'éditeur de modèle).

Vérification réelle : parcours complet sur la console de dev
(`systemctl --user restart harvester-ops-dev` après changement du modèle de
page) avec le banc : modèle enregistré depuis la fenêtre de création, plan
créé dans l'onglet, appliqué depuis la page d'hôte, suivi imbriqué dans le
dock et l'Activité, reprise après un échec provoqué.

## Tâche 9 : essais réels, documentation, version

Fichiers : `VERSION` (2.5.0), `CHANGELOG.md`, `docs/en/bare-metal.md`,
`docs/fr/bare-metal.md` (plans : phases Cluster API et Clusters, modèles de
cluster), `docs/en/operating-procedure.md` et
`docs/fr/procedure-operationnelle.md` (Cluster API : identité restreinte,
suppression, modèle enregistré depuis la fenêtre de création),
`docs/en/troubleshooting.md` et `docs/fr/depannage.md` (suppression bloquée
par une identité incomplète), `tools/parity/data.py` puis `python3 tools/parity/gen.py
--docs`, mémoire du projet.

### Bancs (Cluster API exige Harvester 1.9)

| Banc | Version, taille | Ce qu'il permet | Ce qu'il ne permet pas |
|---|---|---|---|
| harvlab | v1.8.2, 3 nœuds imbriqués, 60 Gio | rien pour cette version (117) | |
| harvlab2 | v1.9.0, 1 nœud imbriqué, 8 vCPU, 24 Gio, disque 150 Go | pile depuis le paquet (pile absente : vraie installation, images poussées par `ctr import`), **un cluster 1+1** à identité restreinte, chaîne Terraform puis clusters, reprise, suppression, mutation réelle de la tâche 2 | plusieurs clusters ; répliques sur plusieurs nœuds |
| harv1 | v1.9.0, bare-metal, 7 CPU, 62,6 Gio, pile déjà prête | lectures : `check` (N = 1 et N = 6), `apply --dry-run`, phase capi vérifiée et sautée ; éventuellement un cluster 1+1 | porte le Rancher Manager et mlm du homelab |
| harv4 | v1.9.0, bare-metal, 20 threads, 128 Gio, 4 SSD, éteint | **trois clusters 1+1 en une exécution**, échec partiel, reprise, constats au bord (N qui dépasse), Activité à trois niveaux, suppression de tout | |

Capacité de harvlab2 pour un cluster 1+1 : environ 6,0 Gio et 0,25 CPU de
demandes, 8 Gio de mémoire réelle d'invité dans une VM de 24 Gio où
Harvester seul prend une part à mesurer (`kubectl describe node` avant
l'essai, consigné dans le CHANGELOG). Le relevé de capacité ne suffit pas sur
un banc imbriqué : la mémoire réelle compte. Proposition : porter harvlab2 à
**32 Gio** (VM arrêtée, `virsh setmaxmem --config` puis `setmem --config`),
disques des VMs du modèle d'essai à 20 Gio, image openSUSE Leap 15.6 cloud
servie par node1 et posée par la phase Terraform sur la classe `harv-rep1`
(une réplique : la règle `fleet-replicas-nodes` bloquerait trois répliques
sur un nœud). Budget de node2 : 32 Gio pour harvlab2, harvlab et le banc
sushy éteints ; `harvlab.sh tidy` après chaque essai.

Déroulé :

1. **harvlab2** (node2 allumé par son iLO, harvlab éteint) : plan `capitest`
   = modèle de déclaration image Leap sur `harv-rep1`, phase capi, un
   cluster 1+1 depuis `rke2-banc`. `check`, puis exécution ; `kubectl get
   capiproviders -A`, `clusters.cluster.x-k8s.io -A`, VMs, identité (tâche
   2). Échec provoqué : image arrêtée côté node1 pendant la phase Terraform,
   reprise. Arrêt pendant la création (la création va à son terme, la
   suite sautée). Suppression depuis l'onglet Clusters, mutation réelle de
   la tâche 2. Remise en état : déclarations détruites, pile laissée
   (règle 128 : la pile reste), `harvlab.sh tidy`, node2 éteint.
2. **harv1** (lecture) : `check` du plan avec N = 1 et N = 6 ; `apply
   --dry-run` (phase capi vérifiée et sautée, aucune écriture, `kubectl get`
   inchangé).
3. **harv4** (sur accord) : node4 allumé par son iLO, `harvester-startup.sh
   --cluster harv4` ; pool d'adresses sur une plage de 12 adresses réservée
   dans NetBox (`infra-dotfiles/netbox/seed.py`) et hors de la plage DHCP de
   Pi-hole ; plan à trois clusters 1+1, dont le deuxième désigne une image
   absente (échec partiel) ; `check` avec N = 8 pour voir le blocage de
   flotte ; reprise après correction ; suppression des trois clusters ;
   plage libérée dans NetBox ; arrêt gracieux (`harvester-shutdown.sh
   --cluster harv4`) et node4 éteint.

Non vérifiable sur nos bancs, à dire comme tel : plus de trois clusters
enfants, plans de contrôle à trois nœuds sur des hôtes distincts, version
stable de CAPHV qui contient #276.

Puis : suite API en parallèle (`python3 -m pytest tests/api/ -q -n 12`),
suite e2e (`python3 -m pytest tests/e2e/ -q -n 8`), bilan de conformité
règle par règle (CONTRIBUTING.md et style des développeurs Harvester),
relecture indépendante du diff, cas limites joués en réel, `git grep` des
tirets cadratins, flèches Unicode et mentions d'outillage dans tout ce qui est
publié.

## Points à trancher

162. **Bancs et accords.** harvlab est en v1.8.2 (pas de Cluster API).
     Recommandation : harvlab2 porté à 32 Gio pour la chaîne complète à un
     cluster et la mutation réelle de l'identité ; harv1 en lecture seule
     (`check`, `apply --dry-run`) ; harv4 pour trois clusters en une
     exécution. **Accords demandés** : (a) allumer node4 et employer harv4
     (décision 130, coût énergétique, node2 éteint pendant ce temps) ;
     (b) porter la VM harvlab2 de 24 à 32 Gio ; (c) facultatif, créer puis
     supprimer un cluster 1+1 sur harv1 (environ 6 Gio de demandes, à côté de
     mlm et du Rancher Manager) si harvlab2 ne suffit pas.
163. **Version de CAPHV embarquée.** #276 n'est que dans des préversions
     (`v1.10.0-dev.0` à `dev.4`). Recommandation : garder v0.10.1 et ses
     trois contournements en 2.5.0, ne pas embarquer une préversion ; le
     changement de version (retrait des contournements, suppression du
     service de remplacement, règles d'identité relues pour la nouvelle
     version) fait un lot à part dès la version stable.
164. **Namespace des VMs d'un cluster à identité restreinte.** Aujourd'hui
     `default` par défaut, ce qui donnerait à chaque identité les VMs de tous
     les clusters. Recommandation : imposé au namespace du cluster (un
     namespace par cluster porte ses objets Cluster API et ses VMs) ; le
     formulaire le préremplit et le grise.
165. **Identité restreinte aussi dans le formulaire de création.**
     Recommandation : oui, un seul chemin (`create --identity restricted`
     par défaut) pour tout nouveau cluster ; `console` reste une option de
     la CLI pour un diagnostic, absente de l'interface.
166. **Compte et liaison du fournisseur de cloud.** Recommandation : créés
     d'avance par la console avec l'identité de la personne, l'identité
     restreinte n'ayant que `get` ; jamais de droit `bind` sur
     `harvesterhci.io:cloudprovider` ; supprimés avec le cluster.
167. **Jeton de l'identité.** CAPHV relit le secret pendant toute la vie du
     cluster : un jeton court casserait le cluster à son expiration.
     Recommandation : jeton de compte de service porté par un secret
     (`kubernetes.io/service-account-token`), le mécanisme que CAPHV emploie
     pour le fournisseur de cloud ; supprimé avec le cluster ; rotation par
     un geste dédié plus tard.
168. **Clusters existants à identité de la console.** Recommandation : pas
     de migration en 2.5.0 ; « Identité : console » affiché dans les
     détails ; geste « Restreindre l'identité » avec la 2.7.0.
169. **Capacité insuffisante.** Recommandation : bloquant dans la chaîne
     (constats de flotte), avertissement inchangé dans le formulaire, où une
     personne décide.
170. **Clusters en parallèle.** Recommandation : séquentiel en 2.5.0 (moteur
     inchangé, ordre et dépendances déjà testés, allocations de pool sans
     concurrence) ; parallélisme borné plus tard d'après les mesures réelles.
171. **Pile présente dans une autre version.** Recommandation : constat
     bloquant `capi-version-differs`, la mise à niveau reste un geste
     explicite de Cluster API > Installation (d'autres clusters peuvent en
     dépendre).
172. **CAAPH dans la phase capi.** Recommandation : toujours déclaré avec la
     pile, comme le bouton Installer existant (fournisseur facultatif qui ne
     bloque rien) ; pas d'interrupteur dans le plan.
173. **Variables dans les modèles de cluster.** Recommandation : éditeur =
     formulaire de création en mode modèle, « Variable... » sur chaque champ
     de référence ; modèles rangés dans l'onglet Plans à côté des modèles de
     déclaration (précédent de la 2.4.0) ; bouton « Enregistrer comme
     modèle » dans la fenêtre de création.
174. **Présentation des actions imbriquées.** Recommandation : Activité au
     premier niveau, filles dépliables (chargées à la demande) ; dock : le
     parent et la fille en cours ; recherche filtrée à plat avec lien vers le
     parent ; rétention comptée au premier niveau.
175. **Arrêt pendant la création d'un cluster.** Recommandation : la création
     en cours va à son terme (règle des éléments, comme Terraform), les
     suivantes sont sautées ; annuler l'action fille elle-même reste possible
     et supprime ce cluster.
176. **Reprise d'un cluster laissé en place.** Recommandation : même origine
     et pas en échec, il est repris et attendu ; en échec, l'élément reste en
     erreur (`cluster-failed`) et la personne le supprime puis reprend : la
     chaîne ne supprime jamais un cluster d'elle-même.

## Décisions (05/11/2026)

Les recommandations des points 163 à 176 sont retenues.

**Revu en tâche 9 (essai réel sur harvlab2, 05/11/2026), point 164.** Dans
le namespace du cluster, CAPHV crée le jeton du fournisseur de cloud sous
le nom `<cluster>-token` (`util/cloud_provider.go:132`), celui que CAPRKE2
donne au jeton d'amorçage du cluster dans ce namespace : CAPHV relit le
secret de CAPRKE2, sans `token`, et le répartiteur de l'API n'est jamais
créé (nœud du plan de contrôle prêt, cluster jamais disponible). Les VMs
vont donc dans un namespace propre au cluster, `<namespace>-vms`, créé et
supprimé avec l'identité ; l'intention du point 164 (une identité
n'atteint que les VMs de son cluster) est gardée, et rien n'est plus
accordé dans le namespace du cluster, qui porte les secrets de CAPRKE2.
À confirmer par le propriétaire du produit. Le point 162 (bancs) attend l'accord du propriétaire du produit pour harv4, harvlab2 à 32 Gio et un cluster sur harv1.
