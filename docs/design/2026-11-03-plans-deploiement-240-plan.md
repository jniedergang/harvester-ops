# 2.4.0 : plans de déploiement, chaîne suivie, contenu Terraform (plan de réalisation)

Conceptions : `2026-11-02-bare-metal-post-installation.md` (décisions 111 à
131 retenues) et `2026-11-02-openbao-secrets.md` (décisions 133 à 145
retenues ; 2.4.0 ne porte que l'interface interne du magasin de secrets).
Point de départ : 2.3.0 (déclarations Terraform pour les hôtes KVM).

## But

Un plan de déploiement nommé, réutilisable, que l'on applique à un cluster
Harvester déclaré ou que l'on enchaîne après une installation bare-metal. Il
déroule, en une seule action suivie et reprenable, les phases disponibles en
2.4.0 : installation (si lancée depuis Bare-metal), disponibilité, contenu
Terraform. Les phases suivantes (gestion Cluster API, clusters enfants,
registre, artefacts) existent déjà dans le modèle, désactivées, avec la
mention « à venir (2.5+) » : le format des plans ne changera pas.

## Architecture

```
bin/lib/secret_store.py   magasin de secrets interne, support local (mémoire)
bin/lib/deploy_plans.py   plans.d et tf-templates.d : contrôle, rendu, variables
bin/lib/deploy_chain.py   moteur pur : phases, ordre, arrêt, reprise, partiel
bin/lib/deploy_ready.py   constats de disponibilité (purs, sur objets lus)
web/app.py                routes, action deploy:<cluster>, branchement des phases
                          sur les mécanismes existants (série, installation,
                          déclarations Terraform)
bin/harvester-plans.py    ligne de commande ; charge web/app.py (comme
                          harvester-baremetal profile apply)
web/static/js/bm-plans.js onglet Plans, fenêtre Déployer
```

Les phases appellent ce qui existe :

| Phase | Mécanisme réutilisé | Action fille |
|---|---|---|
| install | `_bm_batch_plan`, `_bm_batch_presence_refusal` (garde 132), `_baremetal_batch_runner` ; une machine : `_bm_prepare_install`, `_bm_presence_refusal`, `_baremetal_install_runner` | `baremetal-batch:<cluster>` ou `baremetal-install:<hôte>` |
| readiness | `hv_upgrade.prechecks` (nœuds Ready, `ManagedChart`), `volumes.longhorn.io`/`nodes.longhorn.io`, classes de stockage | aucune (lectures) |
| terraform | `TF_DECLS` (`web/tf_store.py`), `_tf_render_resources`, `_tf_decl_runner`, `tfplan.meta.summary` (`web/tf_state.plan_summary`) | `tf-plan-decl:<nom>:<n>`, `tf-apply-decl:<nom>:<n>` |

Le moteur tourne **dans le processus de la console** (fil lancé par
`track_action`, comme la série de profils) et dans le processus de la ligne
de commande quand elle est employée : même code (`deploy_chain.run`), deux
appelants. Raison et alternative : point à trancher 1.

## Contraintes communes (valeurs exactes)

- **Action** : libellé `deploy:<cluster>`, une seule à la fois par cluster
  (409 `deploy-running` sinon), bloquée par un arrêt de cluster en cours
  (`_transfer_busy`). Dry-run : même libellé, `ActionRun.dry_run = True`.
  Reprise : nouvelle action `deploy:<cluster>`, `result.resumes = <id>`.
- **Étapes** : `phase-<id>` pour chaque phase (`install`, `readiness`,
  `terraform`, puis `capi`, `clusters`, `registry`, `artefacts` émises
  `skipped` avec le message `coming in 2.5+` quand elles figurent au plan),
  sous-étapes `ready-<code>` et `tf-<modèle>`. États d'étape : `running`,
  `done`, `error`, `skipped`, `partial`.
- **Résultat** (`run.result`, persisté dans `actions.db` par
  `_actions_persist`, c'est ce que lit la reprise) :

  ```json
  {"plan": "site-standard", "plan_revision": 4, "cluster": "harvlab",
   "resumes": null, "dry_run": false,
   "phases": [{"id": "install", "status": "done", "actions": ["a1b2c3d4e5f6"]},
              {"id": "readiness", "status": "done", "unmet": []},
              {"id": "terraform", "status": "partial",
               "items": [{"template": "reseaux-site", "declaration": "site-standard-reseaux-site",
                          "declaration_id": "9f0e...", "revision_applied": 1, "status": "done",
                          "actions": ["...", "..."]}]},
              {"id": "capi", "status": "disabled"}]}
  ```

  Noms et identifiants seulement : jamais une valeur de variable marquée
  secrète, jamais un secret.
- **Délais** : disponibilité 20 minutes par défaut, réglable par plan
  (`phases.readiness.timeout_minutes`, entier de 1 à 180), relecture toutes
  les 15 s. Attente d'une action fille : celle de la série
  (`BM_BATCH_POLL`).
- **Échec** (116) : la chaîne s'arrête à la phase en échec. Dans la phase
  Terraform, les modèles indépendants continuent et la phase est `partial`.
  Ordre des modèles = ordre du plan ; un modèle n'est « indépendant » que
  s'il ne vient pas après un modèle en échec qu'il désigne
  (`depends_on: [modèle]` facultatif dans l'entrée du plan).
- **Arrêt** : `run._cancel` (route existante `DELETE /api/action/<id>`),
  transmis à l'action fille d'installation en cours (`_bm_wait_action`) ; une
  exécution Terraform en cours va à son terme, les modèles non commencés sont
  `skipped`. Statut final `cancelled`, code 3.
- **Reprise** (115) : relit le résultat de l'action reprise, revérifie chaque
  phase terminée par une lecture bon marché (installation : cluster déclaré
  et joignable, nœuds attendus présents ; disponibilité : rejouée, elle est
  peu coûteuse ; Terraform : déclaration présente, `last_applied_status`
  à `done`, `revision` égale à `revision_applied` consignée dans le
  résultat), puis repart de la première phase non
  terminée. Une phase revérifiée en défaut est rejouée.
- **Secrets** (129) : saisis au lancement, gardés par `secret_store` dans la
  mémoire du déroulé, effacés à la fin quelle qu'en soit l'issue, redemandés
  à la reprise. En 2.4.0 seule la phase d'installation en consomme (jeton du
  cluster, mot de passe de l'OS, mots de passe des BMC, ceux de
  `bm_profiles.SECRET_FIELDS`). Un plan appliqué à un cluster existant ne
  demande donc aucun secret. Ni le libellé, ni les étapes, ni le résultat,
  ni les fichiers `plans.d` et `tf-templates.d` n'en portent.
- **Garde 132** : une installation enchaînée passe par la même lecture des
  BMC que la série (2.2.1) et la fenêtre d'une machine (2.2.0), AVANT la
  création de l'action parente ; refus 409 `fields: ["system_confirm"]`
  inchangé, noms tapés hors des options d'installation.
- **Rangement** (comme `profiles.d`) : `<état>/plans.d/<nom>.yaml` et
  `<état>/tf-templates.d/<nom>.yaml`, fichiers 0600 écrits par remplacement
  atomique, répertoires 0700 (`bm_profiles._private_dir`), nom RFC 1123
  (`bm_profiles.NAME_RE`), chemins relatifs à l'état.
- **Variables** : moteur des profils (`bm_profiles.VAR_RE`, `check_value` :
  une ligne, 512 caractères, aucun caractère de contrôle ;
  `render_advanced` et ses jetons neutres pour les chaînes YAML ; type tiré
  du schéma de formulaire pour une chaîne qui n'est qu'une variable).
  Variables intégrées : `cluster`, `vip` (refusées comme noms déclarés).
  Variable absente : refus avant toute action, avec son chemin
  (`templates[reseaux-site].resources[0].spec.vlan_id`).
- **Révision** : plans et modèles comme les déclarations ; écriture avec la
  révision lue, 409 sinon.
- **Déclarations produites** (114) : déclarations ordinaires de `TF_DECLS`,
  nommées `<plan>-<modèle>` sur le cluster, avec une origine
  `{"plan", "template", "template_revision", "plan_revision"}`. Elles
  restent modifiables dans Terraform > Déclarations.
- **Jamais de destruction par la chaîne** : un plan Terraform dont le résumé
  (`tfplan.meta.summary.counts`) compte un `delete` ou un `replace` n'est
  pas appliqué ; le modèle passe en `error`, code `plan-destroys`, avec un
  lien vers la déclaration à revoir.
- **Workspaces** : tout passe par `_tf_decl_ws`, donc par la réparation
  0700/0600 de 2.3.0 (`_tf_secure_ws`, `_tf_secure_all`) ; rien de neuf.
- **Identité** : figée au déclenchement (`run.cluster_user`, kubeconfig
  résolu par `_kubectl_for_cluster` dans la requête). Cluster créé par
  l'installation : identité de la console, comme la série
  (`plan[0]["_cluster_kc"]`).
- **Rôles** : lecture des plans et modèles = lecteur ; écrire un plan ou un
  modèle = administrateur ; appliquer un plan à un cluster existant =
  opérateur ; chaîne avec installation = administrateur (préfixe
  `/api/baremetal/` de `ADMIN_ONLY_PREFIXES`). Voir point à trancher 3.
- **Interface** : `FloatingPanels` pour les fenêtres, `.sub-tabs-inline`,
  `.btn`, `.card` ; `escapeHtml` sur tout nom ; bulle d'aide sur chaque
  contrôle ; cinq langues, écart de `test_i18n` à zéro ; SSE par
  `SSEReconnect.connect()`.
- **Contenu public** : ni tiret cadratin ni flèche Unicode ; aucune mention
  d'outillage dans les commits, le CHANGELOG et la doc.

## Constats préalables (faits le 03/11/2026)

### Provider Harvester 1.9.0 embarqué : ressources présentes

Méthode : répertoire jetable, miroir local du binaire embarqué
(`terraform-provider-harvester_v1.9.0`, sha256 `0c46ea3e...b9eca66`),
`terraformrc` en `filesystem_mirror` avec `direct` exclu, `terraform init`
puis `terraform providers schema -json`. `tofu` n'est pas installé sur node1
(seulement dans des images) : la commande `terraform` 1.14.5 lit le même
schéma, le protocole des providers est commun. À rejouer avec `tofu` dans le
conteneur de la console avant la release (une ligne dans la tâche 10).

Ressources : `harvester_bootstrap`, `cloudinit_secret`, `clusternetwork`,
`image`, `ippool`, `loadbalancer`, `network`, `pci_device`,
`schedule_backup`, `setting`, `ssh_key`, `storageclass`, `virtualmachine`,
`vlanconfig`, `volume`.

| Objet | Ressource | Champs (R requis, O facultatif) | Conséquence |
|---|---|---|---|
| Réseau de VM | `harvester_network` | `name` R, `cluster_network_name` R, `vlan_id` R (0 à 4094, `IntBetween` dans la source), `namespace`, `description`, `route_mode`, `route_cidr`, `route_gateway`, `route_dhcp_server_ip`, `config`, `labels` O | Couvert pour les réseaux VLAN (L2VlanNetwork, `vlan_id` 0 pour non étiqueté). **Aucun attribut de type** : les réseaux overlay kube-ovn ne se déclarent pas par cette ressource |
| Réseau de cluster | `harvester_clusternetwork` | `name` R, `description`, `labels` O | Couvert |
| Configuration VLAN | `harvester_vlanconfig` | `name` R, `cluster_network_name` R, `node_selector` O, bloc `uplink` exactement 1 (`nics` R, `bond_mode`, `bond_miimon`, `mtu` O) | Couvert ; touche les cartes physiques (point à trancher 8) |
| Classe de stockage | `harvester_storageclass` | `name` R, `parameters` R (map), `volume_provisioner`, `reclaim_policy`, `volume_binding_mode`, `allow_volume_expansion`, `is_default`, bloc `allowed_topologies` (au plus 1) O | Couvert ; `"migratable": "true"` exigé pour la migration à chaud (description du schéma) |
| Pool d'adresses | `harvester_ippool` | `name` R, bloc `range` au moins 1 (`start`, `end`, `subnet`, `gateway` tous R), bloc `selector` au plus 1 (`network`, `priority`, sous-bloc `scope` : `guest_cluster`, `namespace`, `project`) | Couvert ; objet de cluster (pas de `namespace`) ; `gateway` requis par plage |

Conclusion : les formulaires réseau, classe de stockage et pool d'adresses
prévus en 2.4.0 sont possibles (tâche 8). Réseau de cluster et
configuration VLAN : ressources présentes, formulaires renvoyés au point 8.

## Tâche 1 : magasin de secrets interne (`secret_store`)

Fichiers : nouveau `bin/lib/secret_store.py`, nouveau
`tests/api/test_secret_store_240.py`.

Interface (aucun changement visible, aucune route, aucun réglage) :
- `REF_RE = ^local:(?P<cls>[a-z0-9-]+)/(?P<path>[a-z0-9._/-]{1,200})#(?P<field>[a-z0-9_]{1,64})$`
  (calqué sur `openbao:<montage>/<chemin>#<champ>` de la conception
  OpenBao, pour que 2.9.0 ne change que le préfixe).
- `CLASSES = {"plan-run": {"persist": False}}` : seule classe en 2.4.0.
- `class Store` : `put(cls, path, field, value) -> ref`, `get(ref) -> str`
  (lève `SecretMissing(ref)`, jamais la valeur dans le message),
  `delete(ref)`, `delete_tree(cls, path) -> int`, `refs(cls, path)`.
  `is_ref(s) -> bool`. `store(state_dir=None) -> Store` rend le support
  configuré : en 2.4.0 toujours `LocalStore`.
- `LocalStore` : dictionnaire en mémoire sous verrou ; une classe
  `persist: True` lève `NotImplementedError` (aucune n'existe en 2.4.0,
  point à trancher 6). `__repr__` et `__getstate__` ne montrent ni ne
  sérialisent aucune valeur.
- Règle de chemin des secrets d'un déroulé : `plan-run/<action_id>/<nom>`,
  `delete_tree("plan-run", <action_id>)` dans le `finally` du moteur.

Tests (tableaux) : référence bien formée et refus (classe inconnue, `..`,
chemin absolu, champ en majuscules, 201 caractères) ; `put`/`get`/`delete`
; `delete_tree` n'efface que son préfixe (`plan-run/a1` ne touche pas
`plan-run/a10`) ; `SecretMissing` sans valeur ni chemin d'état ; `repr`,
`pickle` et `json.dumps` d'un magasin rempli ne contiennent pas la valeur ;
fils concurrents. Mutations : préfixe comparé sans `/` final (le cas
`a1`/`a10` doit échouer), `__repr__` qui dumpe le dictionnaire.

Vérification réelle : aucune propre (interne) ; exercée par la tâche 6.

## Tâche 2 : plans, modèles de déclaration, champ `plan` du profil

Fichiers : nouveau `bin/lib/deploy_plans.py`, `bin/lib/bm_profiles.py`,
nouveau `tests/api/test_deploy_plans_240.py`, `tests/api/test_bm_profiles_180.py`
(un cas de plus).

`deploy_plans` (bibliothèque standard + PyYAML, comme `bm_profiles`) :
- Constantes : `PLAN_DIR = "plans.d"`, `TEMPLATE_DIR = "tf-templates.d"`,
  `PHASES = ("readiness", "terraform", "capi", "clusters", "registry", "artefacts")`,
  `AVAILABLE = ("readiness", "terraform")`, `BUILTIN_VARS = ("cluster", "vip")`,
  `MAX_TEMPLATES = 32`, `TIMEOUT_MIN, TIMEOUT_MAX, TIMEOUT_DEFAULT = 1, 180, 20`.
- Plan normalisé :

  ```yaml
  name: site-standard
  revision: 4
  description: ""
  variables: [reseau_vms, vlan, pool_debut, pool_fin]
  phases:
    readiness: {enabled: true, timeout_minutes: 20}
    terraform: {enabled: true, templates: [{template: reseaux-site}, {template: images-base, depends_on: [reseaux-site]}]}
    capi: {enabled: false}
    clusters: {enabled: false}
    registry: {enabled: false}
    artefacts: {enabled: false}
  ```

  Une phase non disponible acceptée seulement désactivée ; `enabled: true`
  donne l'erreur `(phases.capi, "coming in 2.5+")`. Ses autres clés sont
  gardées telles quelles (le format de 2.5+ n'est pas figé ici, il n'est
  pas lu). Clé de phase inconnue : refus.
- Modèle de déclaration : `{name, revision, description, variables,
  resources: [{kind, spec}]}` au format de `tf_store.clean_resources`
  (famille `harvester` seulement en 2.4.0 ; `raw` accepté). Les secrets
  sont retirés à l'enregistrement par la règle de
  `kvm_templates.scrub` (clés `password|passwd|secret|token|key|credential|private`),
  sauf `public_key` et `ssh_keys` (publiques, désignées par nom) ; la liste
  de ce qui a été retiré est rendue (point à trancher 7).
- Fonctions : `check_plan(doc)`, `check_template(doc)` (lèvent
  `bm_profiles.ProfileError` avec `[(où, raison)]`) ; `save_plan(state,
  name, doc, revision)`, `load_plan`, `list_plans`, `delete_plan` ;
  idem `*_template` ; `PlanConflict(current_revision)` pour le 409 ;
  `used_variables(plan, templates)` ; `render_templates(plan, templates,
  values, field_type) -> [(template, resources rendues)]` qui passe chaque
  `spec` par `render_advanced` (jetons neutres, type tiré du schéma
  Terraform) ; `declaration_name(plan, template)` = `f"{plan}-{template}"`,
  refusé s'il dépasse 63 caractères (erreur nommée, pas de troncature).
- Profil : `check_profile` accepte une clé de premier niveau `plan`
  (facultative, nom RFC 1123 ou vide) et la rend ; `PROFILE_FIELDS`
  inchangé. Le plan n'est pas chargé ici (le profil reste valide si le plan
  est supprimé ; la fenêtre le signale).

Tests (tableaux) : un cas par refus de `check_plan` (nom, phase inconnue,
phase 2.5+ activée, délai 0 et 181, modèle absent de la liste, doublon,
`depends_on` vers un modèle absent ou placé après, variable intégrée
déclarée, plus de 32 modèles) ; rendu : variable absente (avec son
chemin), valeur à syntaxe YAML (`a: b # c`) arrivée telle quelle, `{{vlan}}`
seul sur `vlan_id` devenu entier, valeur à saut de ligne refusée, nom de
déclaration de 64 caractères refusé ; révision (écriture à révision
périmée = `PlanConflict`) ; fichiers 0600, répertoire 0700, aucun `.tmp`
laissé ; modèle : mot de passe dans `user_data` retiré et nommé,
`public_key` gardé ; profil avec `plan` aller-retour, `plan` invalide
refusé. Mutations : phase 2.5+ acceptée activée, `scrub` sans exemption de
`public_key` (le test de clé publique doit échouer), rendu par substitution
puis relecture (la valeur à `#` doit casser le test).

Vérification réelle : enregistrer un plan et deux modèles dans l'état de
dev, les relire par la CLI (tâche 7).

## Tâche 3 : déclarations, origine et lancement hors requête

Fichiers : `web/tf_store.py`, `web/app.py` (`_tf_decl_start`,
`_tf_decl_view`, route `PUT /api/tf-declarations/<id>`),
`tests/api/test_tf_store.py`, nouveau `tests/api/test_tf_decl_origin_240.py`.

- `tf_store` : colonne additive `origin TEXT` (JSON, NULL pour les
  anciennes lignes), migration comme `target_kind` (`ALTER TABLE ... ADD
  COLUMN` si absente). `create(..., origin=None)` ; `update` ne touche
  jamais `origin` ; `_row` la rend décodée ; `find_by_name(cluster, name,
  target_kind="harvester")`.
- `_tf_decl_view` expose `origin` (lecture seule).
- Découpe de `_tf_decl_start` en `_tf_decl_launch(cluster, kc, decl_id,
  mode, plan_hash=None, user="", cluster_user=None) -> (run_id, hash)` qui
  lève `TfLaunchError(status, payload)` au lieu de rendre un `jsonify`, et
  ne lit ni `request`, ni `current_user()`, ni
  `current_cluster_identity()`. `_tf_decl_start` devient l'enveloppe de
  route (même comportement, mêmes réponses : les tests existants
  `test_tf_declarations.py`, `test_tf_apply_declaration.py`,
  `test_tf_kvm_decl_230.py` passent sans changement).
- `_tf_plan_meta(decl) -> dict | None` : lit `tfplan.meta` de l'espace
  (hash, révision, `summary`) pour le moteur.

Tests : migration d'une base 2.3.0 (lignes conservées, `origin` NULL) ;
`origin` à la création, non modifiable par `PUT` (champ ignoré, révision
inchangée si seul `origin` est envoyé) ; `_tf_decl_launch` appelé depuis un
fil sans contexte de requête (plan simulé) ; chaque erreur de
`_tf_decl_start` rendue à l'identique (tableau : 404, 400 cible, 400
cluster, 400 incomplet, 409 plan périmé, 503 provider KVM). Mutation :
`_tf_decl_launch` qui relit `current_user()` (le test sans contexte doit
échouer).

Vérification réelle : plan puis apply d'une déclaration existante de harv1
par l'interface, inchangés (non-régression), puis destroy de la
déclaration d'essai.

## Tâche 4 : constats de disponibilité (`deploy_ready`)

Fichiers : nouveau `bin/lib/deploy_ready.py`, nouveau
`tests/api/test_deploy_ready_240.py`, jeux d'essai sous
`tests/fixtures/deploy/` relevés sur harvlab (nœuds, `managedcharts`,
`nodes.longhorn.io`, `storageclasses`).

- `evaluate(reads, expected_nodes=None, phases=()) -> {"ready": bool,
  "unmet": [{"code", "detail"}]}`, pur. `reads` : `{"version": ...,
  "nodes": [...], "charts": [...], "lh_nodes": [...], "storageclasses":
  [...]}`, `None` pour une lecture en échec (constat `read-failed`, jamais
  « prêt »).
- Codes : `api-unreachable` (lecture de version en échec), `nodes-not-ready`
  et `nodes-missing` (attendus = lignes de la série, sinon tous les nœuds
  lus), `charts-not-ready` (règle de `hv_upgrade.prechecks`, réutilisée
  telle quelle), `longhorn-not-schedulable` (condition `Schedulable` de
  chaque `nodes.longhorn.io`), `no-default-storageclass` (annotation
  `storageclass.kubernetes.io/is-default-class: "true"`, même lecture que
  `replication_run`). `rke2-traefik` et Turtles ne sont évalués que si
  `capi` est dans `phases` : jamais en 2.4.0 (point à trancher 10).
- La console fournit les lectures (`_kubectl_json` en parallèle, comme
  `hv_upgrade`), le moteur relit toutes les 15 s jusqu'au délai.

Tests (tableau, un cas par code, plus « tout prêt » et cas à trois nœuds
dont un seul en défaut) ; lecture `None` jamais prise pour prête ;
`expected_nodes` plus grand que les nœuds lus = `nodes-missing`. Mutations :
`charts-not-ready` sans relire `hv_upgrade` (copie divergente : test
d'identité de règle), lecture `None` comptée comme vide.

Vérification réelle : `harvester-plans check` (tâche 7) sur harv1 et sur
harvlab : constats vides ; puis un nœud Longhorn de harvlab passé en non
ordonnançable 1 minute (`allowScheduling: false`, remis aussitôt) : constat
`longhorn-not-schedulable` nommé.

## Tâche 5 : moteur de chaîne (`deploy_chain`)

Fichiers : nouveau `bin/lib/deploy_chain.py`, nouveau
`tests/api/test_deploy_chain_240.py`.

Moteur pur, toutes les entrées-sorties injectées :

```python
run(ctx, phases, previous=None, sleep=time.sleep, now=time.time) -> dict
# ctx.emit_step(step_id, status, message)   ctx.set_result(dict)
# ctx.cancelled() -> bool
# phases : liste ordonnée de Phase(id, enabled, execute, verify)
#   execute(ctx, state) -> PhaseOutcome(status, items, message)
#   verify(ctx, state)  -> bool    lecture bon marché pour la reprise
# previous : result de l'action reprise (ou None)
```

- Ordre fixe `install, readiness, terraform, capi, clusters, registry,
  artefacts` ; une phase absente du plan n'émet rien, une phase 2.5+ émet
  `skipped` « coming in 2.5+ ».
- Arrêt à la première phase `error` ; `partial` arrête aussi la chaîne (les
  phases suivantes dépendent du contenu) mais garde les éléments faits.
- Reprise : phases `done` de `previous` passées à `verify` ; `True` =
  `skipped` « already done (checked) », `False` = rejouée ; la première
  phase non `done` repart. Les éléments `done` d'une phase `partial`
  (modèles Terraform) sont revérifiés un à un et sautés.
- `ctx.cancelled()` consulté avant chaque phase et chaque élément.
- Exception d'une phase : statut `error`, `error_summary` = nom de
  l'exception seulement (règle de la série, aucun détail qui porterait un
  secret).

Tests (tableaux) : chaîne complète ; phase désactivée ; phase 2.5+ ;
échec en phase 2 (phase 3 jamais appelée) ; terraform partiel (modèle
indépendant poursuivi, modèle à `depends_on` en échec sauté) ; arrêt avant
une phase, entre deux modèles ; reprise : phase faite revérifiée et sautée,
phase faite dont la vérification échoue rejouée, partiel repris au premier
élément non fait ; exception sans fuite de texte. Mutations : reprise sans
`verify` (le cas « vérification échoue » doit casser), `partial` qui
continue la chaîne, arrêt non consulté entre éléments.

Vérification réelle : par la tâche 6.

## Tâche 6 : la chaîne dans la console

Fichiers : `web/app.py`, nouveau `tests/api/test_deploy_routes_240.py`,
nouveau `tests/api/test_deploy_runner_240.py`.

Routes (`@requires_auth` + `@_rate_limit` sur chaque écriture ; limites
validées par `limits.parse_many` dans le test, piège flask-limiter) :

| Méthode et chemin | Rôle | Limite | Corps et réponse |
|---|---|---|---|
| `GET /api/baremetal/plans` | lecteur | | `{"plans": [{name, revision, description, variables, phases_enabled, invalid?}]}` |
| `GET /api/baremetal/plans/<name>` | lecteur | | plan normalisé |
| `PUT /api/baremetal/plans/<name>` | admin | `30/minute` | `{revision, ...plan}` ; 201 création, 200, 400 `errors: [[où, raison]]`, 409 `{current_revision}` |
| `DELETE /api/baremetal/plans/<name>` | admin | `30/minute` | 204 ; 409 si un déroulé du plan tourne |
| `GET/PUT/DELETE /api/baremetal/tf-templates[/<name>]` | lecteur / admin | `30/minute` | même forme ; `PUT` rend aussi `removed: [chemins]` |
| `POST /api/baremetal/tf-templates/from-declaration` | admin | `30/minute` | `{declaration_id, name}` : modèle tiré d'une déclaration, mêmes retraits |
| `POST /api/deploy/<cluster>/check` | opérateur | `30/minute` | `{plan, variables}` -> `{"findings": [{code, level, phase, detail}], "summary": {declarations: [{name, exists, origin_match}], estimate_minutes}}`, lecture seule |
| `POST /api/deploy/<cluster>` | opérateur | `6/minute` | `{plan, variables, dry_run}` -> 202 `{action_id}` ; 400 constats bloquants ; 409 `deploy-running` |
| `POST /api/deploy/<cluster>/resume` | opérateur | `6/minute` | `{action_id, secrets?}` -> 202 `{action_id, resumes}` ; 409 si l'action reprise tourne ; 400 `secrets-required` si la phase d'installation reste à faire |
| `POST /api/baremetal/profiles/<name>/batch` | admin | `6/minute` (inchangé) | champs ajoutés `plan`, `plan_variables` : avec un plan, la réponse porte `{action_id: <deploy>, cluster, nodes}` et la série devient l'action fille |
| `POST /api/baremetal/install` | admin | inchangée | mêmes champs ajoutés |

Constats de `check` (codes, niveau) : `var-missing` et `var-invalid`
(bloquant, avec chemin), `template-missing` (bloquant),
`decl-name-taken` (bloquant : une déclaration `<plan>-<modèle>` existe sans
cette origine), `decl-exists` (information : elle existe avec cette
origine, elle sera seulement planifiée et appliquée),
`template-newer` (avertissement : le modèle a changé depuis
l'instanciation, la déclaration n'est pas réécrite, point à trancher 4),
`tf-missing` (bloquant : OpenTofu ou Terraform, ou le provider Harvester,
absents ; mêmes lectures que la bannière de 2.2.0), `cluster-unreachable`
(bloquant), `phase-not-available` (bloquant, défensif : plan écrit à la
main), `readiness-later` (information : les constats de disponibilité sont
rejoués au début de la phase).

Branchement des phases (`_deploy_phases(run, cluster, plan, ...)`) :
- `install` : découpe de `_bm_batch_start` en `_bm_batch_prepare(prof,
  batch, secrets_, confirms) -> (plan, errors, machines)` (contrôles,
  occupation des BMC, garde 132) et `_bm_batch_launch(plan, concurrency) ->
  action_id` ; la route `/batch` et la CLI `profile apply` gardent leur
  comportement. La route appelle `_bm_batch_prepare` dans la requête, crée
  l'action `deploy:<cluster>`, dont la phase `install` appelle
  `_bm_batch_launch` et attend par `_bm_wait_action`. Même découpe pour une
  machine (`_bm_presence_refusal` dans la requête, `_bm_track` dans la
  phase). Secrets : `secret_store.put("plan-run", f"{run.id}/token", ...)`,
  relus par la phase au lancement de l'action fille, `delete_tree` dans le
  `finally`. `verify` : cluster dans `clusters.d`, kubeconfig joignable,
  nœuds des lignes présents. Reprise d'une installation en échec : point
  à trancher 12.
- `readiness` : lectures parallèles, `deploy_ready.evaluate`, étape
  `ready-<code>` par constat, délai du plan. `verify` : rejouée (une
  lecture).
- `terraform` : pour chaque modèle, dans l'ordre : déclaration créée si
  absente (`TF_DECLS.create(..., origin=...)`), sinon reprise telle quelle
  si son origine est ce plan et ce modèle, sinon `decl-name-taken` ; puis
  `_tf_decl_launch(mode="plan")`, attente de l'action fille, lecture de
  `_tf_plan_meta` : `delete` ou `replace` comptés = `plan-destroys` ;
  plan vide = `done` sans apply ; sinon `_tf_decl_launch(mode="apply",
  plan_hash=h)` et attente. En dry-run : plan seulement ; les déclarations
  créées pour l'essai sont supprimées à la fin, avec leur espace (rien
  n'est appliqué, il n'y a pas d'état à détruire). `verify` : déclaration
  présente, `last_applied_status == "done"` (valeur écrite par
  `TF_DECLS.mark_applied`), `revision` égale à `items[].revision_applied`
  du résultat.
- Libellé d'Historique : `deploy` ajouté aux `HISTORY_KINDS` de Bare-metal
  (tâche 9).

Tests routes : authentification, rôles (lecteur, opérateur, admin) et
limites de chaque route ; corps invalides (un cas par champ) ; 409 de
révision ; `check` sur cluster inconnu, plan inconnu, chaque code de
constat ; `deploy` refusé avec constat bloquant (rien de lancé, `ACTIONS`
inchangé) ; second `deploy` sur le même cluster = 409 ; batch avec plan :
machine présente sans nom = 409 inchangé et aucune action `deploy:` créée ;
noms tapés jamais dans le résultat ; aucun secret dans réponses, étapes,
`result`, `actions.db` (recherche de la valeur dans la base après coup).
Tests du déroulé (Terraform simulé comme dans `test_tf_kvm_decl_230.py`,
installation simulée comme dans `test_bm_profiles_180.py`) : chaîne
installation, disponibilité, Terraform ; plan qui détruit refusé ; plan vide
sans apply ; dry-run sans apply ni déclaration restante ; arrêt transmis à
l'installation ; reprise après un modèle en échec ; secrets effacés du
magasin après succès, échec, arrêt et exception. Mutations : garde 132
déplacée après la création de l'action, `delete_tree` hors du `finally`,
`plan-destroys` non contrôlé, dry-run qui applique.

Vérification réelle (harvlab, cluster existant, objets `plantest-*`) :
plan `plantest` à trois modèles : réseau VLAN sur `mgmt` (`vlan_id` libre
du banc), classe de stockage à trois répliques (`migratable: "true"`), pool
d'adresses sur une plage réservée dans NetBox (`infra-dotfiles/netbox/seed.py`)
et Pi-hole avant l'essai, image cirros servie par node1, clé SSH. Constats
de `check`, puis exécution ; `kubectl get network-attachment-definitions,
storageclass,ippool,virtualmachineimage,keypair` pour chaque objet. Échec
provoqué : serveur d'image de node1 arrêté, modèle image en `error`,
autres modèles faits, phase `partial` ; serveur relancé, « Reprendre » :
disponibilité revérifiée, modèles faits sautés, image appliquée. Arrêt
pendant la phase Terraform : modèle en cours mené à terme, suivants
`skipped`. Remise en état : destroy de chaque déclaration `plantest-*`
depuis Terraform > Déclarations, objets absents côté cluster, plage
libérée dans NetBox, `harvlab.sh tidy`.

## Tâche 7 : ligne de commande `bin/harvester-plans.py`

Fichiers : nouveau `bin/harvester-plans.py`, `install.sh` (lien
`harvester-plans`, liste du message `Scripts installed`), nouveau
`tests/api/test_harvester_plans_cli_240.py`.

```
harvester-plans [--state-dir D] plan list | show <nom> | save <nom> --file plan.yaml [--revision N] | delete <nom>
harvester-plans [--state-dir D] template list | show <nom> | save <nom> --file m.yaml [--revision N] | delete <nom>
harvester-plans [--state-dir D] check  <plan> --cluster C [--vars vars.yaml]
harvester-plans [--state-dir D] apply  <plan> --cluster C [--vars vars.yaml] [--dry-run]
harvester-plans [--state-dir D] resume <action_id> [--secrets-file s.yaml | --secrets-stdin]
harvester-plans [--state-dir D] install <plan> --profile P --nodes nodes.csv --cluster-name C --vip V
                [--vars vars.yaml] [--confirm-system HOST=NAME]... (--secrets-file s.yaml | --secrets-stdin)
```

- Chargement de la console comme `harvester-baremetal profile apply` :
  `HARVESTER_OPS_STATE_DIR`, `HARVESTER_OPS_WATCH=0`,
  `HARVESTER_OPS_READ_WORKERS=0`, `HARVESTER_OPS_REPLICATION_FOLLOW=0`,
  `HARVESTER_OPS_ACTIONS_DB` et `HARVESTER_OPS_NOTES_DB` dans l'état, puis
  `_load_web_module("app")` (fonction reprise de `harvester-baremetal.py`,
  déplacée dans `bin/lib/` si les deux la partagent : `bin/lib/web_loader.py`).
- Étapes en `STEP_EVENT|<étape>|<état>|<message>` sur la sortie d'erreur,
  résultat JSON sur la sortie standard, codes 0 fait, 1 échec, 2 usage ou
  refus, 3 annulé ; SIGINT et SIGTERM posent `run._cancel`.
- Secrets : fichier 0600 (refus sinon) ou entrée standard, en YAML, clés de
  `harvester-baremetal` (`token`, `password`, `bmc_password`,
  `bmc_passwords`) ; jamais en argument. `--vars` : YAML de valeurs, refus
  d'une clé qui n'est pas une variable du plan.
- `install` passe par la même garde 132 que `profile apply` (sortie 2 et
  ligne `--confirm-system` à ajouter).

Tests : chaque sous-commande en tableau sur un état temporaire ; `--vars`
inconnue ; fichier de secrets 0644 refusé ; secret absent de stdout et
stderr ; `check` sans console joignable = constat, code 2 ; `install` avec
machine présente = code 2, rien lancé ; analyse des arguments
(`allow_abbrev=False`). Mutation : lecture du fichier de secrets sans
contrôle de mode.

Vérification réelle : `harvester-plans check plantest --cluster harvlab`
puis `apply --dry-run` sur harv1 (plan à un modèle image et une clé :
plans lus, rien appliqué, `kubectl get` inchangé, aucune déclaration
restante).

## Tâche 8 : formulaires Terraform réseau, classe de stockage, pool d'adresses

Fichiers : `web/tf_store.py` (`HARVESTER_KINDS` gagne `network`,
`storageclass`, `ippool`), `web/app.py` (`_render_tf_for_kind`,
`_tf_render_resources`, la liste `supported` des erreurs),
`web/static/js/tf-schema.js` (entrées et `TF_SCHEMA_I18N` en cinq
langues), `web/static/js/tf-form.js` si un bloc répété manque
(`range` du pool), `tests/api/test_tf_render.py`,
`tests/api/test_tf_schema.py`, `tests/api/test_tf_schema_i18n.py`,
nouveau `tests/e2e/test_tf_forms_240.py`.

- Champs strictement ceux du schéma relevé (constats préalables), noms
  Terraform identiques ; valeurs contrôlées ou échappées par `_hcl_str`
  (règle de 2.3.0) ; `vlan_id` entier 0 à 4094 ; pool : au moins une plage,
  chaque adresse contrôlée par `ipaddress`, `start <= end` dans `subnet`,
  `gateway` dans `subnet` ; classe de stockage : `parameters` en paires
  clé-valeur, `migratable` proposé coché, `is_default` avec bulle d'aide
  (une seule classe par défaut : Harvester refuse la seconde, le plan le
  montrera).
- Les trois genres entrent dans les modèles de déclaration (tâche 2) sans
  autre code.

Tests : rendu en tableau par genre (minimal, complet, chaque refus),
rendu relu par `terraform validate` contre le provider du miroir si
présent (sinon `skip`), comme les tests de 2.3.0 ; i18n des nouvelles
clés ; e2e : chaque formulaire rempli, code généré affiché. Mutations :
`gateway` hors sous-réseau accepté, `vlan_id` 4095 accepté.

Vérification réelle : sur harvlab, une déclaration de chaque genre créée
depuis Terraform > Déclarations, plan revu, apply, `kubectl get`, destroy.

## Tâche 9 : interface (onglet Plans, fenêtre Déployer, page d'hôte)

Fichiers : nouveau `web/static/js/bm-plans.js`, `web/static/js/bmc.js`
(`TABS`, squelette `data-bm-pane="plans"`, `HISTORY_KINDS`, `kindLabel`),
`web/static/js/routes.js` (`AUTOMATION_PANES.pxe`),
`web/templates/index.html` (bouton d'onglet `data-bm-tab="plans"`, bouton
`#btn-apply-plan` à côté de `#btn-join-node`, balise du script),
`web/static/js/host-page.js` (gestionnaire du bouton),
`web/static/js/bm-profiles.js` (choix du plan dans l'éditeur de profil et
dans la fenêtre de série), la fenêtre d'installation d'une machine dans
`bmc.js`, `web/static/js/tf-declarations.js` (« Enregistrer comme
modèle »), `web/static/js/i18n.js`, `tests/nav_inventory.py`,
`tests/api/test_routes_js_1100.py`, `tests/api/test_bm_tabs_210.py`,
nouveaux `tests/api/test_bm_plans_ui_240.py` (source et node) et
`tests/e2e/test_bm_plans_240.py`.

- **Onglet Plans** entre Profils et Images : `AUTOMATION_PANES.pxe =
  ['machines', 'profiles', 'plans', 'images', 'history']`, adresse
  `#automation/pxe/plans`. Liste à gauche, plan à droite, une carte par
  phase avec son interrupteur ; Cluster API, Clusters, Registre et
  Artefacts grisés avec le badge « à venir (2.5+) » et une bulle qui le
  dit. Modèles choisis dans une liste (jamais retapés), ordre par
  glisser ou boutons monter/descendre (patron existant à réutiliser s'il
  existe dans `bm-profiles.js`, sinon boutons). Sous-section « Modèles de
  déclaration » : liste, édition par les formulaires Terraform existants
  (`tf-form.js`), retraits de secrets annoncés. Aperçu du rendu pour un
  cluster et des valeurs (route `check`).
- **Fenêtre Déployer** (`FloatingPanels`) en trois onglets
  `.sub-tabs-inline` : Paramètres (cluster ou série, plan, variables,
  secrets seulement si installation, avec la phrase « redemandés à la
  reprise »), Vérifications (constats de `check` regroupés par phase,
  bloquants d'abord), Résumé (déclarations créées ou reprises, durée
  estimée : installation 20 min, disponibilité jusqu'au délai, Terraform 1
  à 2 min par modèle d'après les mesures de la tâche 10). Lancer reste
  grisé tant qu'un constat bloque, avec la raison.
- **Page d'hôte Harvester** : bouton « Appliquer un plan » (opérateur ;
  grisé pour un lecteur avec la raison, comme `hosts-index.js`) qui ouvre
  la fenêtre, cluster déjà choisi.
- **Fenêtres d'installation et de série** : champ Plan facultatif
  (prérempli par le `plan` du profil ; plan absent signalé) et ses
  variables ; avec un plan, l'action suivie ouverte est `deploy:`.
- **Suivi** : l'action parente montre ses étapes ; chaque étape porte
  l'identifiant de son action fille, lien `#activity/action/<id>` (patron
  de la série). Bouton « Reprendre » sur une action `deploy:` en `error`,
  `partial` ou `cancelled` dans l'Historique de Bare-metal.
- **Historique** : `HISTORY_KINDS = ['baremetal-', 'bmc-power', 'deploy']`,
  `kindLabel` gagne `deploy` (`bmh.kind.deploy`).

Tests : inventaire de navigation (une ligne `Bare-metal > deployment
plans`, `#automation/pxe/plans`, sélecteur `#bmc-body
[data-bm-pane="plans"]:not([hidden])`), table des adresses
(`#automation/pxe/plans` valide, `#automation/pxe/Plans` refusé), ordre des
onglets ; i18n des nouvelles clés dans les cinq langues ; e2e sur routes
simulées : édition et enregistrement d'un plan (409 montré), phases 2.5+
non activables, fenêtre Déployer (constats, Lancer grisé, secrets absents
pour un cluster existant), bouton de la page d'hôte (lecteur grisé),
Historique avec une action `deploy:` et Reprendre, échappement d'un nom de
modèle hostile. Mutations : onglet retiré de `AUTOMATION_PANES`, Lancer
actif malgré un constat bloquant, `escapeHtml` retiré du nom de modèle.

Vérification réelle : parcours complet sur la console de dev
(`systemctl --user restart harvester-ops-dev` après changement du modèle de
page) avec harvlab : créer `plantest` dans l'onglet, appliquer depuis la
page d'hôte, suivre dans le dock et l'Historique, reprendre.

## Tâche 10 : chaîne complète réelle, documentation, version

Fichiers : `VERSION` (2.4.0), `CHANGELOG.md`, `docs/en/bare-metal.md`,
`docs/fr/bare-metal.md` (section « Plans de déploiement »),
`docs/en/operating-procedure.md` et `docs/fr/procedure-operationnelle.md`
(Terraform : nouveaux formulaires, origine d'une déclaration),
`tools/parity/data.py` puis `python3 tools/parity/gen.py --docs`, mémoire
du projet.

Vérifications réelles, ce qui est réaliste :

| Essai | Banc | Ce qui est fait | Ce qui ne l'est pas |
|---|---|---|---|
| Chaîne avec installation | Banc Redfish sushy de node2, `bmcfg-n1` à 8 vCPU et 32 Gio, harvlab2 éteint | Une fois avant la release : installation d'un nœud (le système déjà présent déclenche la garde 132, nom tapé), disponibilité, plan à deux modèles (image, clé). Environ 30 à 40 minutes. Durées relevées pour la fenêtre | Série de plusieurs nœuds avec plan : la RAM de node2 ne le permet pas avec harvlab ; couverte par les tests du déroulé |
| Plan sur cluster existant | harvlab | Tâche 6 (trois modèles, échec, reprise, arrêt) | |
| Lecture seule | harv1 | `check` et `apply --dry-run` (tâche 7) | Aucun apply sur harv1 |
| Schéma du provider | conteneur de la console | `tofu providers schema -json` rejoué dans le conteneur, mêmes ressources que les constats préalables | |

Budget de node2 : jamais harvlab (60 Gio) et le banc sushy (32 Gio) avec
harvlab2 ; `harvlab.sh tidy` après chaque essai.

Puis : suite API en parallèle (`python3 -m pytest tests/api/ -q -n 12`),
suite e2e (`python3 -m pytest tests/e2e/ -q -n 8`), bilan de conformité
règle par règle (CONTRIBUTING.md et style des développeurs Harvester), relecture
indépendante du diff, `git grep` des tirets cadratins, flèches Unicode et
mentions d'outillage dans tout ce qui est publié.

## Points à trancher

1. **Moteur dans le processus de la console ou par `_cli_action` ?** La
   consigne demandait une action par `_cli_action` lançant
   `bin/harvester-plans.py`. Le code s'y oppose : l'installation s'appuie
   sur le serveur d'artefacts du processus (`pxe_server.start`, port 8091,
   `PortUnavailable` si deux processus le veulent), sur le verrou
   d'occupation des BMC (`_BM_LAUNCH_LOCK`, `_bm_busy`, tenus dans
   `ACTIONS` du processus) et sur les actions filles visibles en direct
   dans le dock (`ACTIONS` en mémoire ; `actions.db` n'est écrit qu'à la
   fin d'une action) ; Terraform tourne aussi dans le processus web. Un
   sous-processus aurait des installations invisibles en direct et pourrait
   piloter un BMC déjà pris. Recommandation : moteur partagé
   (`deploy_chain`) lancé par `track_action` dans la console et chargé par
   la CLI, précédent de `harvester-baremetal profile apply` et de la
   série. Ce plan est écrit ainsi.
2. **Nom de la commande** : la conception dit `harvester-deploy`, la
   consigne `harvester-plans`. Recommandation : `harvester-plans` (elle
   gère aussi plans et modèles), et le dire dans la doc.
3. **Rôle pour lancer** : la conception veut opérateur ; tout
   `/api/baremetal/` est administrateur. Recommandation : plans et modèles
   sous `/api/baremetal/` (admin pour écrire), application à un cluster
   existant sous `/api/deploy/<cluster>` (opérateur), chaîne avec
   installation administrateur (elle passe par les routes bare-metal).
4. **Déclaration déjà instanciée dont le modèle a changé** : réécrire,
   refuser ou laisser ? Recommandation : laisser (114 : une déclaration
   produite devient ordinaire et se modifie dans Terraform), avertissement
   `template-newer` ; réinstancier = supprimer la déclaration puis relancer.
5. **Secrets dans les modèles Terraform** : toute valeur passée à Terraform
   finit dans l'état. Recommandation 2.4.0 : aucune variable secrète dans
   un modèle (retirée à l'enregistrement, tâche 2) ; un secret de VM passe
   par `user_data_secret_name` vers un Secret existant. Les secrets du plan
   en 2.4.0 sont ceux de l'installation.
6. **Support local du magasin** : mémoire seule (seule classe
   `plan-run`, non persistée) ou aussi un fichier 0600 pour les classes
   persistées ? Recommandation : mémoire seule en 2.4.0, fichier ajouté en
   2.9.0 avec la première classe migrée (zéro code sans appelant).
7. **Retrait des secrets d'un modèle** : réutiliser `kvm_templates.scrub`
   retire aussi `user_data` entier dans les modèles KVM. Recommandation :
   même règle de clés, `public_key` et `ssh_keys` exemptées, `user_data`
   gardé mais retiré s'il contient `password`, `passwd` ou `chpasswd`, et
   le dire.
8. **Formulaires réseau de cluster et configuration VLAN** : présents dans
   le provider, mais ils touchent les cartes physiques (`uplink.nics`) et
   peuvent couper un nœud. Recommandation : HCL brut seulement en 2.4.0,
   formulaires plus tard après un essai sur une seconde carte du banc.
9. **Réseaux overlay kube-ovn** : absents de `harvester_network` (aucun
   attribut de type). Hors du contenu Terraform ; ils restent dans l'onglet
   VPC (1.49, 1.66).
10. **Constats `rke2-traefik` et Turtles** : la conception les place dans la
    disponibilité. Recommandation : rattachés à la phase Cluster API,
    évalués à partir de 2.5.0 seulement.
11. **Variable intégrée `vip` d'un cluster existant** : `cluster_decl` ne
    garde pas de VIP. Recommandation : l'hôte de `server` du kubeconfig
    (pour un cluster installé par la console, c'est la VIP), sans
    résolution DNS ; vide s'il ne s'agit pas d'une adresse, et un plan qui
    l'emploie est alors refusé par `var-missing`.
12. **Reprise d'une installation en échec** : relancer toute la série
    n'est pas idempotent (la garde 132 verra un système sur les machines
    déjà faites). Recommandation : la reprise ne relance que les lignes
    dont le nom d'hôte n'est pas un nœud du cluster (jonctions si le
    cluster existe, série complète sinon), avec la garde 132 et les secrets
    redemandés.
13. **Actions filles dépliées dans l'Activité** : la conception les veut
    imbriquées. Recommandation 2.4.0 : identifiant et lien dans chaque
    étape, comme la série ; une vraie imbrication (champ `parent` dans
    `actions.db`) quand 2.5.0 ajoutera ses phases.
14. **Dry-run** : il crée puis supprime les déclarations qu'il a créées
    (rien n'est écrit sur le cluster). À confirmer, sinon le dry-run se
    limite aux constats de `check` et à la disponibilité.

## Décisions (05/11/2026)

Toutes les recommandations des points 1 à 14 sont retenues (décisions 146 à 159).

Décisions ajoutées après la relecture du bloc 3 :

- **160** : les déclarations de réseau, de classe de stockage et de pool
  d'adresses sont réservées à l'admin, comme `_OBJ_ADMIN_KINDS` ailleurs
  dans la console ; contrôle serveur par genre, chaîne d'un plan comprise
  (et types créés par un code libre), l'écran grise avec la raison.
- **161** : un opérateur peut lancer un plan dont les modèles en code libre
  ont été écrits par un admin, valeurs neutralisées ; l'auteur du modèle est
  consigné dans l'origine de la déclaration (`template_by`).
