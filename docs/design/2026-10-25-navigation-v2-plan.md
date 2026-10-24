# Navigation 2.0 : hôtes, VMs, dossiers, Disaster Recovery (plan de réalisation)

Conception approuvée : `2026-10-24-navigation-hotes-vms.md` (relue avec le propriétaire du produit le 25/10/2026), maquette `2026-10-24-navigation-maquette.html`. Base du plan : 1.99.0 (`master`, merge `7a494fa`).

## En-tête

**Objectif.** Remplacer la console « un cluster à la fois » par une console multi-hôtes : liste des hôtes (Harvester et KVM) à la place de `#cluster-select`, une page par hôte qui remonte en onglets toutes les vues déjà construites, une vue unique des VMs rangées dans des dossiers partagés (VMs, conteneurs, hôtes), un menu Disaster Recovery racine et par hôte, et une adresse `#...` exacte pour chaque écran. Rien ne se perd : chaque ligne du tableau de correspondance de la conception a sa tâche (index en fin de document).

**Architecture.**
- Un module pur `routes.js` (grammaire des adresses, lecture, écriture, traduction des anciens jetons) et un module `router.js` (écoute de `hashchange` et du chargement, appelle `App`) ; `App.setCluster()` devient interne au routeur ; `App.getCurrentCluster()` rend le cluster de la route.
- Les sections existantes (`<section id="tab-*">`, `Sections.DEF`, modules `KVM`, `Containers`, `AllVMs`, `ForkliftGlobal`...) ne sont pas réécrites : une page d'hôte est un bandeau (nom, famille, état, sous-menu, barre d'onglets `.sub-tabs-inline`) posé au-dessus de la section existante que la route désigne.
- Dossiers : arborescence dans `folders.db` (SQLite WAL, comme `bookings.db`), appartenance écrite sur l'objet (annotation, métadonnées libvirt, déclaration d'hôte), cache `memberships` mis à jour à chaque lecture de flotte. Toute écriture passe par `bin/harvester-folders.py`, lancé en action suivie par `_cli_action()`.
- DR : écrans neufs (`dr.js`) sur les routes de la 1.99.0 (`/api/replication/...`), aucune règle d'état dans le navigateur.

**Pile technique.** Flask 3 (`web/app.py`), JavaScript sans framework (modules IIFE exposés en `window.X`), SQLite WAL, SSE par `SSEReconnect.connect()`, scripts `bin/*.py` en bibliothèque standard (Python 3.11), `kubectl` pour Harvester, `virsh` par SSH (`bin/lib/kvm_host.py`, `VirshHost.batch`) pour KVM. Tests : pytest (`tests/api/`, logique JS pure par `node -e` comme `tests/api/test_tf_schema_i18n.py`), Playwright (`tests/e2e/`).

**Contraintes globales** (valeurs exactes de la conception, à ne pas réinterpréter) :
- Hôte = cluster Harvester entier ou hyperviseur KVM, jamais un nœud Harvester. Deux familles, montrées à un seul endroit du menu.
- Plafond par famille : **8** hôtes avant « Voir les N hôtes ... », **réglable**. Au-delà : favoris, hôte ouvert, hôtes en alerte, puis le lien vers `#hosts/<famille>`. Un hôte en alerte n'est jamais caché. Tête de famille : `58 ok, 2 en alerte`.
- Favoris par personne, dans la base de la console (pas dans le navigateur).
- Annotation Harvester (VM et charge de conteneurs) : clé `harvester-ops.io/folder`, valeur = chemin du dossier (`/Production/ERP`).
- Métadonnées libvirt : espace de noms `https://harvester-ops.io/xmlns/folder/1`, préfixe `hops`, élément stocké `<hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="/Production/ERP"/>`, posé par `virsh metadata --uri ... --key hops --set ... --config` (et `--live` si le domaine tourne).
- `folders.db` (SQLite WAL) : `folders(id, parent_id, name, position, created_at, created_by)` et `memberships(vm_key, folder_path, source, seen_at)`, `source` vaut `vm` (lu sur l'objet) ou `console` (repli). `vm_key` porte la clé de tout objet rangé : `harvester/<cluster>/<ns>/<nom>`, `kvm/<uuid>`, `container/<cluster>/<ns>/<sorte>/<nom>`, `host/harvester/<nom>`, `host/kvm/<nom>`. Purge d'une entrée après 30 jours sans être vue.
- Un objet est dans **un seul** dossier, **sans héritage** (ranger un hôte ne range pas ses VMs ; un bouton propose de les ranger aussi).
- Supprimer un dossier non vide est **refusé** ; la fenêtre propose « déplacer le contenu vers... », en une action, puis supprime.
- Créer, renommer, ranger, déplacer, supprimer un dossier vide : rôle **operator** (viewer : lecture seule).
- Chaque geste sur les dossiers est une action suivie (dock, Activité), une ligne par objet touché ; l'action garde l'état d'avant et propose **« Annuler »** (déplacement, renommage, rangement en lot).
- **Vérifier avant d'agir** : chaque fenêtre de geste montre ses contrôles avant le bouton (droits par `SelfSubjectAccessReview` ou dry-run, capacité, réseau) ; un contrôle échoué dit pourquoi et quoi faire.
- Formes de routes API imposées : `GET /api/replication/plans`, `GET /api/replication/hosts/<hôte>/resources`, `GET /api/replication/resources/<uid>/events` (existent en 1.99.0) ; lignes rendues par `plan_row()` et `resource_row()` (`bin/lib/replication.py`).
- Adresses : jetons d'ancre `#...` seulement, segments passés par `encodeURIComponent`, famille dans l'adresse (`#harvester/<cluster>`, `#kvm/<hôte>`). F5 rend exactement l'écran affiché.
- **2.0.0 en bascule directe, sans interrupteur** ancienne/nouvelle navigation. Les **anciens jetons restent compris pour toujours** (`#storage`, `#allvms`, `#kvmhosts`...). Retour possible par l'agent de mise à jour (1.82.0).
- Règles du dépôt : `VERSION` et entrée `CHANGELOG.md` dans le commit qui apporte un changement visible ; cinq langues (EN, FR, DE, ES, IT) pour toute chaîne (`tests/api/test_i18n.py`) ; info-bulle sur chaque contrôle ; `escapeHtml(...)` avant tout `.innerHTML` ; `@requires_auth` partout et `@_rate_limit("N/minute")` sur toute mutation ; toute mutation = `ActionRun` ; l'interface ne contourne jamais `bin/*` pour une mutation ; doc EN et FR dans le commit du changement visible ; pas de framework JS ; contenu public sans tiret cadratin ni flèche Unicode.
- Essais réels : harv1 (`~/.kube/harvester.yaml`) pour les annotations ; hôte KVM de banc sur node2, **seuls les domaines `kvmtest-*` peuvent être touchés** (jamais `harvlab-*`, `hardpipe-*`, `netperf`, `vmw*`) ; transfert harvlab vers harvlab2 pour le dossier conservé.

## Grammaire des adresses (référence de toutes les tâches)

Segments séparés par `/`, chacun décodé par `decodeURIComponent`. `<c>` = nom de cluster (RFC 1123, `_K8S_NAME_RE`), `<h>` = nom d'hôte KVM (`kvm_decl.NAME_RE`, `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$`), `<id>` = identifiant de dossier ou de plan.

| Adresse | Écran | Origine |
|---|---|---|
| `#vms`, `#vms/folder/<id>`, `#vms/folder/unfiled` | Machines virtuelles (racine) | conception |
| `#vms/host/harvester/<c>`, `#vms/host/kvm/<h>` | panneau Hôtes, un hôte | conception |
| `#vms/host/harvester`, `#vms/host/kvm` | panneau Hôtes, une famille entière (cible de l'ancien `#kvmvms`) | ajout du plan |
| `#folders`, `#folders/<id>` | entrée « Dossiers » en haut du menu, trois groupes (VMs, conteneurs, hôtes) | ajout du plan (décision du 25/10) |
| `#containers`, `#containers/folder/<id>` | Conteneurs (racine) | conception, filtre par dossier ajouté |
| `#hosts`, `#hosts/harvester`, `#hosts/kvm`, `#hosts/folder/<id>` | index des hôtes | conception, filtre par dossier ajouté |
| `#harvester/<c>` (option `/overview/events`) | Vue d'ensemble, bloc Événements en option | conception, bloc ajouté |
| `#harvester/<c>/vms`, `#harvester/<c>/containers` | VMs, conteneurs d'un hôte | conception |
| `#harvester/<c>/imports/vmimport[/<pane>]`, `#harvester/<c>/imports/forklift[/<pane>]` | Imports et migrations ; panes de `Sections.DEF.vmimport` (`imports`, `vmware`, `openstack`, `ova`) et `Sections.DEF.forklift` (`prep`, `sources`, `inventory`, `waves`) | ajout du plan (la conception ne nomme pas les sous-adresses) |
| `#harvester/<c>/params/<tab>[/<sub>]` | Paramètres ; `<tab>` parmi `nodes`, `storage`, `network`, `addons`, `security`, `monlog`, `advanced`, `upgrade`, `power`, `conn` (identifiants de la maquette) ; `<sub>` = clé de pane de `Sections.DEF` (`storage/volumes|images|classes`, `network/vmnets|clusternets|lbs|pools|hostnets|overlay|underlay`, `security/secrets|sshkeys`, `monlog/metrics|alerts|flows|outputs`, `advanced/settings|pci|usb|sriov|support`) ou `power/startup|shutdown` | conception |
| `#harvester/<c>/dr`, `#kvm/<h>/dr` | DR d'un hôte | conception |
| `#kvm/<h>`, `#kvm/<h>/vms`, `#kvm/<h>/params/<system|pools|networks|labels|conn>` | hôte KVM | conception |
| `#dr`, `#dr/<id>` | tous les contrats, un contrat | conception |
| `#migrations`, `#gpus`, `#bookings`, `#usage`, `#automation/<capi|terraform|pxe>`, `#activity` | racines | conception |
| `#activity/host/harvester/<c>`, `#activity/host/kvm/<h>` | Activité filtrée sur un hôte (filtre `cluster` existant, `kvm:<h>` pour KVM) | ajout du plan |

Anciens jetons (`<L>` = dernier hôte Harvester visité, sinon le premier visible ; `<LK>` = dernier hôte KVM visité, sinon le premier ; sans hôte visible : `#hosts` avec message) :

| Ancien | Nouveau |
|---|---|
| `#overview`, `#cluster` | `#harvester/<L>` |
| `#storage`, `#network`, `#addons`, `#security`, `#monlog`, `#advanced` | `#harvester/<L>/params/<même nom>` |
| `#startup`, `#shutdown` | `#harvester/<L>/params/power/startup`, `.../power/shutdown` |
| `#namespaces` | `#harvester/<L>/vms` |
| `#allvms` | `#vms` |
| `#containers` (cluster courant aujourd'hui) | `#harvester/<L>/containers` (voir point à trancher 3) |
| `#allcontainers` | `#containers` |
| `#kvmhosts` | `#hosts` |
| `#kvmvms` | `#vms/host/kvm` |
| `#kvmstorage`, `#kvmnetworks` | `#kvm/<LK>/params/pools`, `#kvm/<LK>/params/networks` |
| `#vmimport`, `#forklift` | `#harvester/<L>/imports/vmimport`, `#harvester/<L>/imports/forklift` |
| `#forkliftglobal` | `#migrations` |
| `#automation` | `#automation/capi` |
| `#gpus`, `#bookings`, `#usage`, `#activity`, `#automation/<x>` | inchangés |

La conception cite `#kvm-hosts` : le jeton réel est `#kvmhosts` (`web/templates/index.html`, ligne 175). Les deux sont traduits.

## Étape 1 : routeur dans l'interface actuelle (peut sortir seule en 1.100.0, rien de visible ne change)

### Tâche 1 : grammaire pure `routes.js`

Fichiers : créer `web/static/js/routes.js` (aucun accès au DOM, export `window.Routes` et `module.exports` si défini, pour `node`), référencé dans `web/templates/index.html` avant `app.js`. Créer `tests/api/test_routes_js_1100.py`.

Interface produite :
```js
Routes.parse(hash)            // -> {name, kind, host, sub, tab, pane, id} ; {name: 'unknown', raw} si illisible
Routes.format(route)          // -> '#...' (encodeURIComponent par segment)
Routes.fromLegacy(token, ctx) // ctx = {lastHarvester, lastKvm, harvester: [...], kvm: [...]} -> route ou null
Routes.LEGACY                 // table des anciens jetons, figée par un test
Routes.HARVESTER_PARAMS       // ['nodes','storage','network','addons','security','monlog','advanced','upgrade','power','conn']
Routes.KVM_PARAMS             // ['system','pools','networks','labels','conn']
```
`name` parmi : `vms`, `folders`, `containers`, `hosts`, `host`, `dr`, `migrations`, `gpus`, `bookings`, `usage`, `automation`, `activity`, `unknown`. `kind` : `harvester` ou `kvm`. Les noms d'hôte sont contrôlés par les mêmes expressions que le serveur (RFC 1123 pour Harvester, `NAME_RE` de `kvm.js` pour KVM) ; un nom invalide rend `unknown`.

Tests (tableau, `node -e`, saut si `node` absent) :
- aller-retour `format(parse(x)) == x` pour **chaque** ligne du tableau des adresses, y compris un nom à point (`kvm-paris.01`) et un segment encodé (`%2F` refusé dans un nom) ;
- chaque ligne du tableau des anciens jetons, avec `ctx` à deux clusters et deux hôtes KVM, et les cas sans hôte (rend `{name:'hosts', notice:'no-host'}`) ;
- cas d'erreur, un par chemin : famille inconnue (`#xen/a`), onglet inconnu (`#harvester/a/params/foo`), pane inconnu (`.../params/storage/zzz`), nom invalide (`#harvester/-a`), segment vide (`#harvester//vms`), jeton vide (rend `null` : le routeur choisit le défaut) ;
- un cluster et un hôte KVM du même nom donnent deux routes distinctes ;
- mutation volontaire nommée `test_routes_mutation_legacy_table` : le test recharge le module en retirant `storage` de `LEGACY` et vérifie que la ligne `#storage` échoue (le test prouve qu'il lit la table, pas une copie).

Vérification réelle : aucune (logique pure) ; relecture du tableau avec la maquette (`perHost`, `HPARAMS`, `KPARAMS`, lignes 319 et 511-512).

### Tâche 2 : source unique du cluster courant

Aujourd'hui onze fichiers lisent ou pilotent `#cluster-select` directement au lieu de `App.getCurrentCluster()` : `bookings.js` (353), `containers.js` (254) lisent la liste des options ; `gpus.js` (291) et `forklift-global.js` (286) changent la valeur et envoient `change` ; `capi.js` (9 lectures), `terraform.js` (5), `tf-declarations.js` (127), `capi-services.js` (418), `harvester-users.js` (25), `settings.js` (255) lisent la valeur. Le retrait du sélecteur en étape 2 les casserait tous en silence.

Fichiers : modifier `web/static/js/app.js` et les dix fichiers ci-dessus. Créer `tests/api/test_cluster_source_1100.py`.

Interface produite par `App` (ajoutée au `return` de `app.js`) :
- `App.listClusters() -> string[]` (clusters visibles, lus une fois de `/api/clusters`, relus après ajout ou retrait) ;
- `App.openHost(kind, name, sub, tab, pane)` : navigation par le routeur (remplace « valeur du select + `dispatchEvent('change')` » de `gpus.js` et `forklift-global.js`) ;
- `App.managementCluster() -> string` et `App.setManagementCluster(name)` : cluster de gestion des pages Cluster API, Terraform, déclarations Terraform, services CAPI ; vaut le dernier hôte Harvester visité tant que la personne ne l'a pas changé ; gardé dans `localStorage` (`harvester_ops_mgmt_cluster`) ;
- `App.getCurrentCluster()` inchangé en signature.

Tests :
- test source : aucun fichier de `web/static/js/` hors `app.js` et `router.js` ne contient la chaîne `cluster-select` (tableau des fichiers fautifs dans le message) ; mutation volontaire `test_cluster_source_mutation` : réinjecter une lecture dans une copie temporaire de `capi.js` et vérifier que le contrôle échoue ;
- e2e ajouté à `tests/e2e/test_cluster_switch.py` : depuis GPU, « ouvrir les add-ons » d'un autre cluster mène à ce cluster (voile de bascule vu, `AccessNotice` remis à zéro) ; idem depuis Migrations (all clusters) vers les vagues.

Vérification réelle : console de dev (`systemctl --user restart harvester-ops-dev`), deux clusters déclarés (harv1 et le banc `gpudemo`) : GPU vers add-ons, Migrations vers vagues, Cluster API garde son cluster de gestion après un changement d'hôte.

### Tâche 3 : routeur branché sur l'interface actuelle

Fichiers : créer `web/static/js/router.js` (`window.Router`), modifier `web/static/js/app.js` (`setTab`, `init`, `setCluster`, `reloadForCluster`, `restoreSubTabsFromStorage`, liens d'en-tête), `web/static/js/sections.js` (`show`, `open` écrivent l'adresse), `web/templates/index.html` (ordre des scripts). Créer `tests/e2e/test_router_1100.py`, `tests/e2e/nav.py` (aide `goto(page, hash)` qui attend le voile retombé).

Interface :
```js
Router.go(hashOrRoute, {replace: false})  // pose location.hash, le reste suit hashchange
Router.current()                          // route courante (objet de Routes.parse)
Router.start()                            // appelé par App.init APRÈS bind(), remplace la restauration par localStorage
```
Comportement :
- un seul point d'entrée `apply(route)` : pose le cluster (ancien `setCluster`, devenu interne, garde `Veil.during` et `AccessNotice.reset()` sur changement d'hôte), PUIS l'onglet (`setTab`), PUIS le pane (`Sections.show`) ou le sous-onglet d'aperçu, dans cet ordre (défaut de 1.4.31 : onglet restauré avant le cluster, toile vide) ;
- `setTab` et `Sections.show` écrivent l'adresse par `history.replaceState` quand l'écran change par un clic, pour que F5 rende l'écran exact ;
- étape 1 seulement : une adresse de l'étape 2 (`#hosts`, `#dr`, `#vms/folder/...`, `#kvm/<h>`) ouvre l'écran actuel le plus proche (`kvmhosts`, `allvms`, `kvmstorage` filtré à la main) et ne casse rien ;
- adresse vide : en 1.100.0, l'ancien `harvester_ops_current_tab` reste lu (46 fichiers e2e l'utilisent pour ouvrir un onglet) ; en 2.0.0, voir point à trancher 2 ;
- hôte inconnu ou hors des droits (absent de `/api/clusters`, ou dans `hidden`) : retour à l'écran par défaut avec le message d'`AccessNotice` (`AccessNotice.report({...})` existant), jamais une page vide ;
- les choix d'interface restent dans `localStorage` (`harvester_ops_section_<sec>` sert de pane par défaut quand l'adresse n'en porte pas ; groupes repliés ; filtres).

Tests e2e (tableau paramétré) :
- chargement **à froid** de chaque adresse servie par l'étape 1 et de chaque ancien jeton : l'écran attendu est actif (`#tab-<x>.active`), le nom du cluster affiché est celui de l'adresse, aucun appel réseau vers un autre cluster (requêtes interceptées) ;
- F5 après un clic sur un pane (`network/overlay`) rend le même pane ; Précédent du navigateur revient à l'écran d'avant ;
- hôte inconnu, hôte caché par Rancher (`hidden` simulé par la route de test), nom invalide : message affiché, écran par défaut ;
- lien copié d'un onglet du cluster B ouvert depuis un onglet du cluster A : voile vu, `AccessNotice` vidé ;
- mutation volontaire `test_router_cold_load_detects_order` : une variante de test injecte `Router` avec l'ordre onglet puis cluster (paramètre de test `window.__routerOrder = 'tab-first'`, absent en production) et vérifie que la toile de `#harvester/<c>/params/nodes` reste vide, donc que le test attrape la régression.

Version et doc : `VERSION` 1.100.0, entrée `CHANGELOG.md` (Added : liens directs et F5 exacts ; Internal : `routes.js`, `router.js`, source unique du cluster ; Tests), `docs/en/architecture.md` et `docs/fr/architecture.md` (section navigation : grammaire des adresses). Suites : fichiers ciblés, puis `pytest tests/api/ -q -n 12` et `pytest tests/e2e/ -q -n 8`.

Vérification réelle : console de dev sur harv1 et gpudemo ; copier l'adresse de `Network > Overlay` de harv1, l'ouvrir dans une fenêtre privée après connexion, constater l'écran exact ; même essai avec un compte viewer, puis avec un cluster retiré de la configuration (message, pas de page blanche).

## Étape 2 : nouveau menu (2.0.0)

### Tâche 4 : API des hôtes et favoris

Fichiers : modifier `web/app.py` ; créer `bin/lib/folders_db.py` (partagé avec l'étape 3 ; ici seulement la table des favoris et `connect()`), `tests/api/test_hosts_routes_200.py`, `tests/api/test_favorites_200.py`.

Interfaces :
- `GET /api/hosts` (`@requires_auth`, `@shared_read(per_user=True, scope="_hosts")`) :
```json
{"cap": 8,
 "families": {"harvester": {"ok": 58, "alert": 2}, "kvm": {"ok": 2, "alert": 0}},
 "hosts": [{"kind": "harvester", "name": "harv1", "state": "ok|unreachable|denied|error|unknown",
            "error": null, "vm_count": 12, "labels": {}, "version": "v1.9.0",
            "favorite": true, "folder": "/Production", "origin": "config|console"}]}
```
  Harvester : clusters de `load_config()` filtrés comme `api_clusters()` (SSO : `_sso_cluster_state`, les cachés ne figurent pas), état par `_cluster_reachable()` (sonde TCP en cache, `_REACH_TTL`), `vm_count` et `version` pris dans le cache des lectures partagées de `/api/vms-all` et `/api/status/<c>` s'il est chaud, sinon `null` (le menu ne lance aucune lecture par hôte). KVM : `kvm_decl.load_all()`, état et `vm_count` pris dans le cache `KVM_SCOPE` s'il est chaud, sinon `unknown`.
- Plafond : clé `ui.host_family_cap` de la configuration (défaut 8, entier 1..100), relue à chaque requête (`load_config_ro()`), donc modifiable à chaud ; voir point à trancher 6.
- `PUT /api/me/favorites/<kind>/<name>` et `DELETE /api/me/favorites/<kind>/<name>` (`@requires_auth`, `@_rate_limit("60/minute")`, rôle viewer comme `/api/me/password`) ; table `favorites(user, host_key, created_at, PRIMARY KEY(user, host_key))` dans `folders.db`. Voir point à trancher 5 (préférence personnelle écrite sans `ActionRun`).

Tests (tableau) : clusters config et console, cluster caché par SSO absent, cluster injoignable (`state: unreachable`), hôte KVM sans cache (`unknown`), compte de VMs `null` puis renseigné après un `/api/vms-all` ; plafond par défaut, plafond 3, valeur invalide (0, 101, texte) ramenée au défaut avec avertissement au journal ; favoris : ajout, doublon idempotent, retrait, `kind` inconnu (400), nom invalide (400), hôte non déclaré (404), deux personnes ne voient pas les favoris l'une de l'autre ; garde « aucune lecture par hôte » : `_kubectl_run` et `VirshHost.batch` remplacés par des leurres qui échouent le test s'ils sont appelés ; mutation volontaire `test_hosts_mutation_alert_hidden` : forcer le tri qui place les hôtes en alerte après le plafond et vérifier que le test « alerte jamais cachée » de la tâche 5 échoue.

Vérification réelle : `curl` authentifié sur la console de dev avec harv1, gpudemo et node2 déclarés ; arrêter le banc gpudemo, l'état passe à `unreachable` sans attendre de délai kubectl.

### Tâche 5 : menu latéral des hôtes, recherche, favoris, plafond

Fichiers : modifier `web/templates/index.html` (remplacer `.cluster-selector` et les groupes `tab-group-cluster`, `tab-group-vms`, `tab-group-containers`, `tab-group-kvm`, `tab-group-vmio` par l'arborescence de la conception ; garder `tab-group-shared`, `tab-group-automation`, `activity`), `web/static/css/style.css` (réutiliser `.tab`, `.tab-group`, `.tab-group-head`, `.tab-child`, badges existants), `web/static/js/i18n.js`. Créer `web/static/js/host-nav.js` (`window.HostNav`), `tests/e2e/test_host_nav_200.py`, `tests/api/test_host_nav_model_200.py` (logique de plafond, pure, par `node -e`).

Interface :
```js
HostNav.render(data)          // data = réponse de /api/hosts
HostNav.visible(hosts, cap, openKey) // pure : [{key, reason: 'favorite'|'open'|'alert'|'rest'}], "Voir les N" si coupé
HostNav.focusSearch()         // raccourci « / » (hors champ de saisie)
```
Contenu : « Dossiers » en tête (`#folders`), recherche (filtre hôtes par nom, étiquette, famille ; propose « Aller à la VM » à partir du cache de `/api/vms-all` déjà chargé ; Entrée ouvre le premier résultat), Machines virtuelles, Conteneurs, Hôtes (familles repliables, état gardé dans `localStorage`, pastille d'état, nombre de VMs, étoile de favori avec info-bulle, accordéon : un seul hôte déplié, sous-menu de la conception), « + Ajouter un hôte » (fenêtre `.modal-overlay` à deux choix : Harvester ouvre `Clusters.openAddForm()`, KVM ouvre la fenêtre de déclaration existante, exportée par `kvm.js` en `KVM.openDeclare()`), Disaster Recovery, Migrations, Ressources partagées, Automatisation, Activité. Les entrées sont des `<a href="#...">` réels (clic milieu, copie de lien). Aucun interrupteur de navigation dans Réglages > Apparence (la maquette en parle encore dans une info-bulle, ligne 213 : ne pas la reprendre).

Tests :
- modèle pur (tableau) : 5 hôtes (tout visible), 9 hôtes (8 + « Voir les 9 »), 60 hôtes dont 3 favoris, 2 en alerte au rang 59 et 60 et l'hôte ouvert au rang 40 (tous visibles), plafond 3 ; égalités de tri par nom ;
- e2e avec 100 hôtes simulés (route `/api/hosts` interceptée) : hauteur du menu bornée, tête de famille `58 ok, 2 en alerte` (format i18n), recherche par nom et par étiquette, `/` donne le focus, Entrée ouvre l'hôte, étoile persistée après rechargement (appel `PUT` vu), « Voir les 60 hôtes Harvester » mène à `#hosts/harvester` ;
- accessibilité clavier du menu (Tab, Entrée), info-bulle présente sur chaque contrôle (`title` ou `data-tip`) ;
- parité i18n (`tests/api/test_i18n.py` inchangé, doit rester vert).

Vérification réelle : console de dev avec harv1, gpudemo, harvlab2 (éteint : alerte visible), node2 ; recherche d'une VM `kvmtest-1`, favori retrouvé depuis un autre navigateur.

### Tâche 6 : page d'hôte Harvester, sous-menu et onglets de Paramètres

Fichiers : créer `web/static/js/host-page.js` (`window.HostPage`) ; modifier `web/templates/index.html` (bandeau `#host-page-head` avec sous-menu et barre `.sub-tabs-inline`, posé au-dessus des sections existantes ; la section `tab-overview` perd ses sous-onglets Cluster et Events, déplacés), `web/static/js/app.js` (`setTab` reçoit la route), `web/static/js/sections.js` (aucune réécriture : `Sections.show(sec, pane)` est appelé par le routeur), `web/static/js/upgrade.js` (`Upgrade.open(cluster)` monté dans l'onglet au lieu de la fenêtre, ou fenêtre ouverte depuis l'onglet : voir ci-dessous), `web/static/js/bmc.js` (export `BMC.openJoin(cluster)`), `web/static/js/i18n.js`. Créer `tests/e2e/test_host_page_200.py`.

Correspondance montée par `HostPage` (chaque ligne = une ligne du tableau de la conception) :

| Route | Contenu monté (existant) |
|---|---|
| `#harvester/<c>` | `tab-overview` : métriques, jauges (`#overview-usage`), table des nœuds ; bloc Événements = `EventsBoard` monté sous les métriques (`mountTopology('events')` dans un second hôte de bloc) ; bouton « Activité de cet hôte » vers `#activity/host/harvester/<c>` |
| `.../params/nodes` | vue de blocs `ClusterMap` (`mountTopology('cluster')`), avec maintenance des nœuds et `HostSettings` ; bouton « Joindre un nœud » qui ouvre `BMC.openJoin(<c>)` (Automatisation > Bare-metal, cluster déjà choisi) |
| `.../params/storage[/pane]`, `network`, `addons`, `security`, `monlog`, `advanced` | sections `tab-storage`, `tab-network`, `tab-addons`, `tab-security`, `tab-monlog`, `tab-advanced` par `Sections.show` |
| `.../params/upgrade` | contenu de la fenêtre `Upgrade` (versions, progression, journaux) monté dans l'onglet ; le bouton Upgrade de l'aperçu y mène |
| `.../params/power/startup`, `/shutdown` | `tab-startup` et `tab-shutdown` (ordre des VMs, `loadVMOrder`) sous une barre à deux onglets |
| `.../params/conn` | nouveau pane léger : origine (`config` ou `console`), nœuds déclarés, boutons « Tester le kubeconfig » et « Tester SSH » (routes existantes `POST /api/clusters/<name>/test-kubeconfig`, `/test-ssh`, admin), raccourci vers Réglages > Clusters (`Settings.openTab('clusters')`) |
| `#harvester/<c>/vms` | `tab-namespaces` avec ses boutons (Backups, modèles, configurations cloud, Namespaces, archives de transfert, créer) |
| `#harvester/<c>/containers` | `Containers.current.start(...)` |
| `#harvester/<c>/imports/vmimport[/pane]`, `/imports/forklift[/pane]` | sections `tab-vmimport` et `tab-forklift` |
| `#harvester/<c>/dr` | tâche 16 |

Tests e2e (tableau, une ligne par route ci-dessus, chargement à froid) : l'onglet attendu est actif dans la barre, la section existante est visible, le cluster de la route est celui des appels réseau ; changement d'hôte depuis une page de Paramètres garde l'onglet (`params/storage` de A vers `params/storage` de B) avec voile ; bouton Upgrade de l'aperçu ouvre `params/upgrade` ; « Joindre un nœud » ouvre Bare-metal avec le cluster prérempli ; rôle viewer : boutons d'écriture grisés avec raison (comportement existant conservé) ; mutation volontaire `test_host_page_mutation_lost_view` : retirer `advanced` de `Routes.HARVESTER_PARAMS` doit faire échouer la ligne `params/advanced/pci` (et le test d'inventaire de la tâche 10).

Vérification réelle sur harv1 : parcourir chaque onglet de Paramètres, ouvrir une fenêtre d'édition par onglet (sans appliquer), passer un nœud en maintenance puis l'en sortir sur harvlab si le banc tourne (harv1 est mononœud : le webhook refuse le dernier nœud, refus attendu affiché), lancer un « Tester SSH » ; comparer avec la 1.99.0 qu'aucune fonction ne manque (liste écrite dans le compte rendu de la tâche).

### Tâche 7 : page d'hôte KVM

Fichiers : modifier `web/static/js/kvm.js` (option de filtre d'hôte, exports), `web/static/js/host-page.js`, `web/static/js/i18n.js`. Créer `tests/e2e/test_kvm_host_page_200.py`.

Interface : `KVM.start(view, el, {host})` (le filtre d'hôte existant `cur.prefs.host` devient imposé et son menu masqué quand `host` est donné) ; `KVM.openDeclare()`, `KVM.testHost(name)`, `KVM.removeHost(name)` exportés (code existant de `onClick`, lignes 316 et 346).

| Route | Contenu |
|---|---|
| `#kvm/<h>` | tuile de l'hôte (vue `hosts` filtrée) : CPU, mémoire, versions, VMs par état |
| `#kvm/<h>/vms` | `KVM.start('vms', el, {host: h})` |
| `.../params/system` | versions de libvirt et de QEMU, CPU, mémoire, URI, de `GET /api/kvm/hosts/<h>` |
| `.../params/pools` | `KVM.start('storage', el, {host: h})` |
| `.../params/networks` | `KVM.start('networks', el, {host: h})` |
| `.../params/labels` | étiquettes de la déclaration, en lecture seule (voir point à trancher 8) |
| `.../params/conn` | Tester (operator) et Retirer (admin), adresse, port, utilisateur, empreinte épinglée (sans chemin de clé) |
| `#kvm/<h>/dr` | état vide : « la réplication des domaines KVM n'est pas encore disponible » (sans appel à `/api/replication/hosts/<h>/resources`, voir point à trancher 9) |

Tests : chaque route à froid (KVM simulé par les jeux d'essai `tests/fixtures/kvm/`), un hôte KVM et un cluster du même nom ne se mélangent pas, Retirer demande admin (operator : bouton grisé avec raison), hôte injoignable : tuile en erreur classée et Paramètres lisibles (déclaration seule).

Vérification réelle sur node2 : `#kvm/node2/vms`, geste « démarrer » puis « arrêter » sur `kvmtest-1` uniquement, `Tester` depuis Connexion ; aucun geste sur un autre domaine.

### Tâche 8 : index des hôtes

Fichiers : créer `web/static/js/hosts-index.js` (`window.HostsIndex`), section `tab-hosts` dans `web/templates/index.html`, `web/static/js/i18n.js`, `tests/e2e/test_hosts_index_200.py`.

Contenu : tableau des deux familles (famille, nom, état, version, étiquettes, VMs, dossier, favori), tri par colonne, filtres famille, état, étiquette, version, dossier ; mêmes gestes que le menu (ouvrir, favori, ranger dans un dossier en étape 3) ; `#hosts/kvm` reprend ce qu'offrait KVM > Hosts (Déclarer via « + Ajouter un hôte », Tester, Retirer par ligne). Source : `GET /api/hosts` seulement.

Tests : tri et filtres en tableau, 100 hôtes, adresse `#hosts/harvester` pré-filtrée, message d'un lien vers un hôte disparu (`#harvester/absent` arrive ici avec la notice).

Vérification réelle : console de dev, filtre par étiquette KVM de node2, tri par état avec harvlab2 éteint.

### Tâche 9 : entrées racines de flotte et pages de gestion

Fichiers : modifier `web/templates/index.html`, `web/static/js/app.js`, `web/static/js/capi.js`, `web/static/js/terraform.js`, `web/static/js/tf-declarations.js`, `web/static/js/capi-services.js`, `web/static/js/harvester-users.js`, `web/static/js/i18n.js`. Créer `tests/e2e/test_fleet_roots_200.py`.

- `#containers` monte `Containers.all` ; `#migrations` monte `ForkliftGlobal` ; `#gpus`, `#bookings`, `#usage` inchangés ; `#activity/host/<kind>/<name>` pose le filtre `cluster` de l'Activité (`<name>` ou `kvm:<name>`).
- Automatisation : en-tête « Cluster de gestion : <nom> [changer] » (menu des clusters visibles) sur Cluster API, Terraform et les déclarations ; valeur par `App.managementCluster()` (tâche 2). Bare-metal inchangé.
- Réglages > Comptes du cluster (`stab-husers`) lit `App.getCurrentCluster()` (dernier hôte Harvester) et l'affiche dans son en-tête avec un menu pour en changer (voir point à trancher 7).

Tests : chaque racine à froid ; le cluster de gestion affiché est celui utilisé par les appels de `capi.js` (requêtes interceptées) ; changer de cluster de gestion ne change pas l'hôte ouvert ; filtre d'Activité par hôte KVM.

Vérification réelle : Cluster API sur harv1 (lecture du diagnostic), plan Terraform d'une déclaration existante sans appliquer, Activité filtrée sur node2.

### Tâche 10 : retrait de l'ancien menu, migration des tests, inventaire des écrans

Fichiers : modifier `web/templates/index.html` (plus de `#cluster-select`, plus de `data-tab` des anciens groupes), `web/static/js/app.js` (le `change` de `#cluster-select`, `KVM_TABS`, restauration `TAB_STORAGE` retirés ; `Routes.LEGACY` reste), les tests e2e qui cliquent `.tab[data-tab=...]` (20 clics, 17 sélecteurs, 4 `#tab-group-kvm`, 2 `#tab-group-vmio`) ou posent `harvester_ops_current_tab` (46 fichiers) : passage à `nav.goto(page, '#...')` ; `tests/e2e/test_ui_flows.py` (liste ligne 116), `tests/e2e/test_nav_vmio_177.py`, `tests/api/test_usage_ui_195.py` (ligne 22, liste `["gpus","bookings","usage"]` du groupe partagé, conservée), `tests/api/test_bookings_ui_194.py`. Créer `tests/api/test_nav_inventory_200.py` et `tests/e2e/test_nav_inventory_200.py`.

Inventaire : une table Python unique `SCREENS` (une entrée par ligne du tableau de correspondance de la conception : ancien emplacement, adresse nouvelle, sélecteur DOM attendu). Le test API vérifie que chaque `<section id="tab-*">` de `index.html` est atteint par au moins une adresse de `SCREENS` (un écran orphelin fait échouer) et que chaque ancien jeton de `Routes.LEGACY` a sa ligne ; le test e2e charge chaque adresse à froid et attend son sélecteur. Mutation volontaire `test_nav_inventory_mutation_orphan` : ajouter une section factice `tab-zzz` à une copie du modèle et vérifier l'échec.

Vérification réelle : console de dev en 2.0.0 sur harv1 + node2 : chaque ancien lien noté dans la doc (`#storage`, `#allvms`, `#kvmhosts`, `#forkliftglobal`...) ouvre son écran.

## Étape 3 : vue unique des VMs et dossiers partagés (2.0.0)

### Tâche 11 : ligne commune des VMs (Harvester et KVM)

Le schéma de ligne commun « prévu en 1.97.0 » **n'existe pas** dans `master` : aucune occurrence de `common_row` ni de `memory_bytes`, `/api/kvm/vms` ne rend pas d'`uuid` (l'inventaire lance `list --all`, sans `--uuid`), et `/api/vms-all` rend `cluster`, `memory` en texte Kubernetes. La 1.97.0 a porté les profils de stockage, la 1.98.0 la console KVM ; la mémoire du projet note « Reste : vue unique (common_row/uuid/key) ». Cette tâche le livre, tel que décrit dans `2026-10-24-kvm-197-plan.md` (section « Vers une vue unique »).

Fichiers : modifier `bin/lib/kvm_host.py` (`INVENTORY_COMMANDS` ajoute `"uuids": ("list", "--all", "--uuid", "--name")`, `host_inventory` rend `uuid` par VM), `bin/lib/kvm_ops.py` (`common_row(vm)`), `web/app.py` (`_vms_collect` : `folder` lu dans l'annotation ; `harvester_common_row(v, cluster)` ; `GET /api/vms-all?kinds=harvester,kvm`), `bin/lib/kvm_host.py` (lecture des dossiers, voir ci-dessous). Créer `tests/fixtures/kvm/list-uuid-name.txt` (relevé sur node2, libvirt 12.7.0), `tests/fixtures/kvm/metadata-folder.xml`, `tests/api/test_vm_common_row_200.py`.

Interface (champs ajoutés, aucun renommé) : `kind`, `source`, `namespace`, `name`, `phase`, `cpu`, `memory_bytes`, `node`, `ips`, `labels`, `uuid`, `key`, plus `folder` (chemin lu sur l'objet ou `null`) et `folder_source` (`vm`, `console`, `null`). États libvirt ramenés : `running`, `idle`, `blocked` -> `Running` ; `shut off` -> `Stopped` ; `paused`, `pmsuspended` -> `Paused` ; `crashed` -> `Failed` ; `in shutdown` -> `Stopping` ; autre -> `Unknown`. Réponse : `{"sources": [{"kind","source","state","count","error"}], "vms": [...]}` ; `clusters` reste rendu tel quel pour `all-vms.js` tant qu'il le lit.

Lecture des dossiers KVM : `virsh metadata <domaine> https://harvester-ops.io/xmlns/folder/1` par domaine **persistant ou actif**, dans le même `batch` SSH que l'inventaire (une commande par domaine, une seule connexion) ; absence de l'élément = pas de dossier (code de sortie de virsh à relever sur node2 : erreur « metadata not found », classée comme absence, pas comme échec). Voir point à trancher 10 (coût sur 40 hôtes).

Tests (tableau) : chaque état libvirt vers sa phase ; mémoire `4Gi`, `512Mi`, `1G`, vide ; KVM `memory_kib` absent ; clé Harvester et clé KVM ; domaine renommé garde sa clé ; liste figée des champs communs (un test qui casse si un champ est renommé) ; métadonnées présentes, absentes, XML illisible (dossier `null`, erreur listée), chemin invalide dans l'annotation (ignoré, signalé) ; mutation volontaire `test_common_row_mutation_phase` : inverser `paused` et `running` dans la table et vérifier l'échec.

Vérification réelle : node2, `virsh list --all --uuid --name` comparé à la réponse ; `/api/vms-all?kinds=harvester,kvm` sur harv1 + node2.

### Tâche 12 : registre `folders.db` et logique pure des dossiers

Fichiers : créer `bin/lib/folders.py` (pur), compléter `bin/lib/folders_db.py`, créer `tests/api/test_folders_model_200.py`, `tests/api/test_folders_db_200.py`.

`folders_db` (calqué sur `bin/lib/bookings_db.py`) : `default_path()` (`HARVESTER_OPS_FOLDERS_DB`, sinon `folders.db` à côté de `notes.db`), `connect(path)` (WAL, `BEGIN IMMEDIATE` pour écrire), schéma exact de la conception plus les ajouts listés au point à trancher 4 : colonne `folders.origin` (`console` ou `objects`, « recréé depuis les VMs »), table `moves(action_id, seq, object_key, from_path, to_path, status, message, at)` (journal des écritures, sert à la reprise et à « Annuler »), table `favorites` (tâche 4). Fonctions : `list_tree`, `create(parent_id, name, by)`, `rename(id, name)`, `move_folder(id, new_parent_id)`, `delete_empty(id)`, `path_of(id)`, `id_of(path)`, `ensure_path(path, origin)`, `upsert_membership(key, path, source, seen_at)`, `purge(now, days=30)`, `record_move(...)`, `pending_moves(action_id)`.

`folders` (pur) : `FOLDER_NAME_RE` (1 à 63 caractères, sans `/`, sans caractère de contrôle, ni `.` ni `..`), `check_path(path) -> str|None` (absolu, profondeur et longueur bornées : voir point à trancher 4), `object_key(kind, ...)`, `reconcile(tree, rows, now) -> (memberships, created_paths)` (règle « l'objet fait foi » : une valeur sur l'objet gagne ; un chemin inconnu crée le dossier avec `origin=objects`), `rename_plan(tree, members, folder_id, new_name) -> [(key, from, to)]`, `undo_plan(moves, current) -> [(key, to, from)] + conflits` (un objet déplacé ailleurs depuis n'est pas remis : conflit dit), `delete_check(id, members) -> {"empty": bool, "count_visible": n}` (ne compte que ce que la personne voit).

Tests (tableau) : noms valides et invalides (un cas par règle) ; reconstruction complète depuis des objets seuls (base perdue) ; objet qui dit `/A/B`, cache qui dit `/C` (l'objet gagne) ; objet sans valeur et cache `console` (le cache reste) ; purge à 29 et 31 jours ; hôte injoignable : ses entrées ne sont pas purgées tant qu'il n'est pas relu ; plan de renommage sur 3 niveaux ; annulation avec un objet déplacé entre-temps (conflit) ; suppression d'un dossier vide, non vide, vide pour la personne mais pas pour une autre (refus, sans révéler le nombre caché) ; concurrence : deux `create` du même nom sous le même parent dans deux connexions, un seul passe ; mutation volontaire `test_folders_mutation_inheritance` : une variante de `reconcile` qui range les VMs d'un hôte rangé doit faire échouer le cas « pas d'héritage ».

### Tâche 13 : ligne de commande `bin/harvester-folders.py`

Fichiers : créer `bin/harvester-folders.py`, modifier `bin/lib/cluster_decl.py` et `bin/lib/kvm_decl.py` (`check_decl` accepte un champ facultatif `folder`, validé par `folders.check_path`), `bin/lib/kvm_host.py` ou `bin/lib/kvm_ops.py` (`set_folder(host, domain, path, live)`), créer `tests/api/test_folders_cli_200.py`.

Protocole commun des scripts : `--json`, `--db`, `--as <personne>`, `--id <action>` (journal lié à l'Activité), progression `STEP_EVENT|<id>|<status>|<message>` sur stderr, codes 0, 1, 2. La demande arrive par le fichier privé de `_cli_action(spec=...)`.

Sous-commandes :
- `tree [--json]` ; `create --parent <id|/> --name N` ; `rename --id I --name N` ; `move-folder --id I --parent P` ; `delete --id I` (refus si non vide, code 1, message « move content to another folder first ») ;
- `assign --to <chemin|unfiled>` avec la liste des objets dans la demande : `[{"key", "kind", "cluster", "namespace", "name", "workload_kind", "host", "domain", "write": true|false}]` ; pour chaque objet, une ligne `moves` puis l'écriture :
  - VM Harvester : `Kube.patch("virtualmachines.kubevirt.io", ns, name, {"metadata": {"annotations": {"harvester-ops.io/folder": path}}})` en merge patch, `null` pour « Non rangées » (sémantique déjà apprise en réel : une clé se retire par `null`) ;
  - charge de conteneurs : même patch sur `deployments.apps`, `statefulsets.apps`, `daemonsets.apps` (voir point à trancher 11 pour les autres sortes) ;
  - domaine KVM : `virsh metadata --domain D --uri https://harvester-ops.io/xmlns/folder/1 --key hops --set '<folder path="..."/>' --config`, plus `--live` si actif ; `--remove` pour « Non rangées » ; domaine transitoire : `--live` seul, ligne marquée `console` dans le cache (perdu à l'arrêt) ;
  - hôte : champ `folder` de `clusters.d/<nom>.yaml` ou `kvm.d/<nom>.yaml` (écriture atomique existante) ; cluster déclaré dans `config.yaml` : base seulement (point à trancher 12) ;
  - `write: false` (pas le droit d'écrire l'objet) : base seulement, `source=console` ;
- `apply-rename --id I` : réécrit tous les objets du dossier renommé ou déplacé, reprend les lignes `pending` d'une action interrompue (`resume --action A`), débit borné (`--rate` écritures par seconde, défaut 5) ;
- `undo --action A` : rejoue `undo_plan`, une ligne par objet, conflits dits ;
- `write-back --folder I` : écrit sur les objets les rangements `console` que la personne a désormais le droit d'écrire.

Tests (tableau, `Kube` et `VirshHost` remplacés par des leurres qui enregistrent les appels) : chaque sous-commande en réussite ; patch exact envoyé (clé, valeur, `null`) ; commande virsh exacte, avec `shlex.quote` d'un chemin contenant une apostrophe et un espace ; domaine transitoire ; refus du webhook (dry-run serveur rejeté) : ligne `failed`, les autres continuent ; hôte injoignable en cours de lot : reprise par `resume` sans réécrire les lignes `done` ; `delete` d'un dossier non vide (code 1) ; nom invalide (code 2) ; `undo` avec conflit ; deux `assign` simultanés sur le même objet : la dernière écriture gagne, journal cohérent ; mutation volontaire `test_folders_cli_mutation_patch_key` : remplacer la clé d'annotation par `harvester-ops.io/folders` dans le module et vérifier l'échec des cas Harvester.

Vérification réelle :
- harv1 : `harvester-folders.py assign` sur une VM de test créée pour l'occasion, `kubectl --kubeconfig ~/.kube/harvester.yaml get vm <vm> -o jsonpath='{.metadata.annotations.harvester-ops\.io/folder}'` relu ; contrôler l'absence de condition `RestartRequired` et de redémarrage (`kubectl get vm <vm> -o jsonpath='{.status.conditions}'`, `vmi` inchangée, `metadata.generation` inchangée) ; retrait par « Non rangées » puis VM de test supprimée ;
- node2 : `virsh metadata kvmtest-1 https://harvester-ops.io/xmlns/folder/1` après pose, relu après un redémarrage du domaine (`--config` tient), clone de `kvmtest-1` (vers `kvmtest-clone`) qui naît dans le même dossier, puis nettoyage ; aucun autre domaine touché ;
- transfert harvlab vers harvlab2 (1.45.0) d'une VM rangée : dossier conservé sous sa nouvelle clé (`sanitize_vm` ne retire pas l'annotation : `_DROP_ANNOTATIONS` et `_DROP_ANNOTATION_PREFIXES` de `bin/lib/vm_transfer.py` ne la visent pas) ; restauration depuis une sauvegarde Harvester : à constater, pas supposé (la conception l'affirme).

### Tâche 14 : routes console des dossiers et contrôles avant geste

Fichiers : modifier `web/app.py` (section « dossiers », chemins sous `/api/folders`, **pas** sous `/api/clusters` qui est réservé aux admins par `ADMIN_ONLY_PREFIXES`), `web/static/js/dock.js` (bouton « Annuler » d'une action qui le propose). Créer `tests/api/test_folders_routes_200.py`.

Interfaces :
- `GET /api/folders` -> `{"tree": [{"id","parent_id","name","position","path","origin","counts": {"vms","containers","hosts"}}]}` (comptes limités à ce que la personne voit) ;
- `POST /api/folders` `{parent_id, name}` ; `PATCH /api/folders/<id>` `{name?, parent_id?, position?}` ; `DELETE /api/folders/<id>` ; `POST /api/folders/<id>/empty-into` `{target_id}` (déplacer le contenu puis supprimer, une action) ;
- `POST /api/folders/check` `{objects: [...], to}` -> `{"objects": [{"key","write": true|false,"reason","check": "ssar|virsh|decl"}]}` : `kubectl auth can-i patch virtualmachines.kubevirt.io/<nom> -n <ns>` (ou la sorte de la charge) avec le kubeconfig de la personne (`_kubectl_for_cluster`, usurpation et Rancher compris) ; KVM : hôte joignable et domaine persistant ; hôte : rôle operator et déclaration `console` ;
- `POST /api/folders/assign` `{objects, to}` ; `POST /api/folders/actions/<action_id>/undo` ; `POST /api/folders/<id>/write-back` ;
- toutes les mutations : `@requires_auth`, `@_rate_limit("30/minute")`, rôle operator par la règle par défaut de `required_role_for` (à figer par un test), `_cli_action(..., "harvester-folders", spec=..., check_busy=False, id_arg=True)` ; réponse `202 {"action_id": ...}` ; `run.undo = {"path": "/api/folders/actions/<id>/undo"}` sérialisé par `ActionRun.to_dict()` (nouvel attribut, absent = pas de bouton).
- cache : `_vms_collect`, `_containers_collect`, la lecture KVM et `/api/hosts` appellent `folders.reconcile` et écrivent `memberships` par `folders_db.upsert_membership` (écriture de cache par la console, sans `ActionRun` : ce n'est pas une mutation d'objet, voir point à trancher 4).

Tests (tableau) : chaque route en réussite et en erreur (corps non objet, nom invalide, dossier inconnu 404, viewer 403, opérateur 202, admin 202) ; `check` avec droit, sans droit (403 de `can-i` simulé), cluster injoignable (`write: false`, raison) ; chemin de kubeconfig absent de toute réponse et de `cmd` affiché ; limite de débit déclarée valide (`limits.parse_many`, piège flask-limiter connu) ; `to_dict()` porte `undo` seulement pour les actions de dossiers ; mutation volontaire `test_folders_routes_mutation_role` : déclarer la route `assign` en viewer dans une copie de la table et vérifier l'échec du cas viewer.

Vérification réelle : sur la console de dev avec un compte operator et un compte viewer ; à travers Rancher (session SSO d'un membre de cluster sans droit d'écriture) : `check` rend `write: false`, le rangement passe en `console`, la pastille le dit.

### Tâche 15 : vue Machines virtuelles, panneau Dossiers et Hôtes, gestes

Fichiers : modifier `web/static/js/all-vms.js` (devient la vue racine `#vms` : lit `/api/vms-all?kinds=harvester,kvm`, compose le menu d'une ligne selon `kind` : `VMActions` pour Harvester, menu de `kvm-vm.js` pour KVM ; console par `VMConsole.open` ou `VMConsole.openKvm`), `web/static/js/containers.js` et `web/static/js/hosts-index.js` (filtre par dossier, gestes de rangement), `web/static/js/host-nav.js` (« Dossiers »), `web/static/js/i18n.js`, `web/static/css/style.css`. Créer `web/static/js/folders.js` (`window.Folders` : panneau, saisie sur place, fenêtre « Déplacer vers un dossier »), section `tab-folders` dans `index.html`, `tests/e2e/test_folders_200.py`.

Comportement : panneau à deux modes (Dossiers, Hôtes), dossiers virtuels « Toutes les VMs » et « Non rangées » non modifiables ; pastilles famille, DR (si la VM est dans un contrat : `resource_uid` présent dans `/api/replication/plans`, lu une fois), « rangement connu de la console seulement » ; glisser une ou plusieurs lignes ; menu « Déplacer vers un dossier » avec recherche ; créer, renommer, supprimer en saisie sur place ; la fenêtre de déplacement affiche le résultat de `POST /api/folders/check` objet par objet **avant** le bouton ; ranger un hôte propose « ranger aussi ses N VMs » (case décochée par défaut) ; suppression d'un dossier non vide : fenêtre « déplacer le contenu vers... ». Une VM cachée par les droits n'est jamais comptée.

Tests e2e (données simulées) : glisser 3 lignes de deux familles, fenêtre de contrôle avec une ligne `write: false`, action suivie dans le dock puis « Annuler » qui remet les dossiers d'origine ; renommer, créer, supprimer vide, supprimer non vide (refus et proposition) ; `#vms/folder/unfiled` ; `#vms/host/kvm/node2` ; viewer : aucun geste de rangement ; XSS : un nom de dossier `<img onerror>` reste du texte ; les 5 langues sur les nouveaux libellés.

Vérification réelle : harv1 + node2 dans la console de dev, ranger une VM de test de harv1 et `kvmtest-1` dans `/Essais/Nav`, relire l'annotation et la métadonnée, renommer `/Essais` en `/Essais2` (deux objets réécrits, suivi dans le dock), annuler, puis retirer le rangement et supprimer les dossiers.

## Étape 4 : Disaster Recovery (2.0.0)

### Tâche 16 : DR racine, contrat et sous-menu d'hôte

Aucun écran de réplication n'existe dans `master` (la 1.99.0 livre moteur, CLI et routes ; `web/static/js/` ne contient aucun module de réplication). « Reprendre les écrans de réplication » veut donc dire les construire depuis `2026-10-22-replication-maquette.html` sur les routes existantes.

Fichiers : créer `web/static/js/dr.js` (`window.DR`), sections `tab-dr` dans `index.html`, `web/static/js/host-page.js` (sous-menu DR), `web/static/js/i18n.js`, `tests/e2e/test_dr_200.py`, `tests/api/test_dr_ui_200.py` (contrôle source : aucune règle d'état dans `dr.js`, pas de liste d'états autorisés recopiée).

Lectures : `GET /api/replication/plans` (racine, une ligne par contrat dépliable sur ses ressources), `GET /api/replication/plans/<id>` (`#dr/<id>`), `GET /api/replication/hosts/<c>/resources` (`#harvester/<c>/dr`, colonnes « côté » et hôte d'en face), `GET /api/replication/resources/<uid>/events` (journal, lien vers l'action d'Activité quand `action_id` est présent). Actions : boutons rendus depuis `actions: [{name, role, allowed}]`, grisés avec leur raison quand `allowed` est faux, appel des routes existantes (`/check`, `/measure`, `/protect`, `/failover`, `/resume`, `/unprotect`, `/settings`, `DELETE`), chaque appel rend un `action_id` suivi dans le dock. Vocabulaire : « contrat » à l'écran, `plan` dans l'API. Création de contrat : fenêtre de la maquette sur `POST /api/replication/plans` puis `/check` affiché avant « Protéger » (principe « vérifier avant d'agir »).

Tests : racine avec 3 contrats d'états différents (réponses de `tests/api/test_replication_routes_199.py` réutilisées comme jeux d'essai), dépliage, `#dr/<id>` à froid, sous-menu d'hôte cible et source, action non permise grisée avec raison (viewer, operator face à `failover` admin), hôte KVM : état vide, journal paginé (`before`) ; mutation volontaire `test_dr_mutation_role_copy` : une copie de `dr.js` qui recalcule `allowed` côté navigateur doit faire échouer le contrôle source.

Vérification réelle : harvlab vers harvlab2 (bancs allumés sur node2) : créer un contrat sur une VM cirros de test, `check`, `protect`, constater RPO tenu et événements à la racine et dans `#harvester/harvlab/dr` et `#harvester/harvlab2/dr` (mêmes lignes), `unprotect`, supprimer le contrat. Ranger la VM protégée dans un dossier pendant la protection : aucun événement `plan-drift` (vérifie la règle de la tâche 17).

### Tâche 17 : dérive de réplication et annotation de dossier

Constat dans le code : la conception demande de comparer la `generation` et non le `resourceVersion`. `bin/lib/replication.py` fait déjà mieux : `vm_spec_generation(vm)` (ligne 680) ne sert que de **préfiltre** (`metadata.generation`, jamais `resourceVersion`), et `spec_drifted(known, seen)` (ligne 756) décide sur `vm_spec_digest(vm, disks)` (ligne 715), empreinte des volumes, disques, tailles, réseaux, interfaces, `firmware.uuid`, CPU et mémoire, qui **ignore annotations et étiquettes**. `bin/lib/replication_follow.py` (lignes 1251-1262) émet `plan-drift` sur cette seule empreinte. Une annotation ne change ni `generation` ni l'empreinte.

Fichiers : `tests/api/test_replication_model_199.py` (ajout), aucun changement de code attendu.

Tests : cas ajoutés au tableau de `spec_drifted` : VM avant et après ajout de `harvester-ops.io/folder` (même `generation`, même empreinte, pas de dérive) ; `generation` différente et annotation seule changée (empreinte égale, pas de dérive) ; mutation volontaire `test_drift_mutation_annotations_in_digest` : une variante de `vm_spec_digest` qui inclut `metadata.annotations` doit faire échouer le premier cas.

Vérification réelle : intégrée à la tâche 16.

## Fin de chantier

### Tâche 18 : documentation, version 2.0.0, bilan de conformité

Fichiers : `VERSION` (2.0.0), `CHANGELOG.md` (entrée `## [2.0.0] - AAAA-MM-JJ - Host-centred navigation, unified VMs with folders, Disaster Recovery screens`, sections Added, Changed, Fixed, Internal, Tests, et le **tableau de correspondance complet** ancien emplacement -> nouvelle adresse), `docs/en/architecture.md`, `docs/fr/architecture.md` (navigation, routeur, dossiers, modèle de données), `docs/en/capabilities.md`, `docs/fr/capabilites.md` (dossiers, DR, hôtes), `docs/en/operating-procedure.md`, `docs/fr/procedure-operationnelle.md` (retrouver un ancien écran, ranger, annuler, rôles), `docs/en/harvester-parity.md`, `docs/fr/parite-harvester.md` régénérées par `python3 tools/parity/gen.py --docs` après mise à jour de `tools/parity/data.py`, index des documents de conception du dépôt (pointeur vers ce plan et la conception).

Bilan : tableau règle par règle (règles du dépôt et règles TERRAHARV) -> conforme, écart, corrigé, chaque affirmation du CHANGELOG vérifiée dans le code ; relecture par un relecteur indépendant ; cas limites joués en réel (hôte injoignable pendant un renommage, droit retiré entre le contrôle et l'écriture, console arrêtée pendant `apply-rename` puis `resume`). Suites complètes : `pytest tests/api/ -q -n 12`, `pytest tests/e2e/ -q -n 8`. Release vérifiée **par l'unité systemd packagée** sur le banc client `hops-upd`, y compris le retour arrière 2.0.0 -> 1.100.0 par l'agent de mise à jour (anciennes adresses rouvertes après retour).

## Index : correspondance de la conception -> tâche

| Ligne de la conception | Tâche |
|---|---|
| Sélecteur de cluster -> liste des hôtes, recherche rapide | 2, 5, 10 |
| Bouton `+` -> « + Ajouter un hôte » | 5 |
| Overview > Metrics -> Vue d'ensemble | 6 |
| Overview > Cluster (blocs des nœuds) -> Paramètres > Nœuds | 6 |
| Overview > Events -> Vue d'ensemble, bloc Événements | 6 |
| Overview, bouton Upgrade -> Paramètres > Mise à jour | 6 |
| Storage -> Paramètres > Stockage | 6 |
| Network (7 onglets) -> Paramètres > Réseau | 6 |
| Add-ons -> Paramètres > Add-ons | 6 |
| Security -> Paramètres > Sécurité | 6 |
| Monitoring & Logging -> Paramètres > Monitoring et journaux | 6 |
| Advanced (réglages, PCI, USB, SR-IOV, support) -> Paramètres > Avancé | 6 |
| Startup, Shutdown -> Paramètres > Alimentation | 6 |
| Current cluster's VMs (et ses boutons) -> Hôte > Machines virtuelles | 6 |
| All clusters VMs -> Machines virtuelles (racine), dossiers, KVM compris | 11, 15 |
| Current cluster's containers -> Hôte > Conteneurs | 6 |
| All clusters containers -> Conteneurs (racine) | 9, 15 |
| Shared resources -> inchangé | 9 |
| KVM > Hosts -> Hôtes > KVM, Vue d'ensemble KVM, Connexion, « + Ajouter » | 5, 7, 8 |
| KVM > Virtual machines -> racine (filtre KVM) et Hôte KVM > VMs | 7, 11, 15 |
| KVM > Storage -> Hôte KVM > Pools | 7 |
| KVM > Networks -> Hôte KVM > Réseaux | 7 |
| VM Import -> Hôte > Imports et migrations > Imports | 6 |
| VMware migrations -> Hôte > Imports et migrations > VMware | 6 |
| Migrations (all clusters) -> Migrations (racine) | 9 |
| Automation > Cluster API, Terraform -> inchangé (cluster de gestion explicite) | 2, 9 |
| Automation > Bare-metal ; « Joindre un nœud » depuis Nœuds | 6, 9 |
| Activity ; filtre par hôte | 9 |
| Settings > Clusters ; raccourci Paramètres > Connexion | 6 |
| (nouveau) Disaster Recovery racine et par hôte | 16, 17 |
| (nouveau) Hôtes : page index | 4, 8 |
| Routeur, liens directs, anciens jetons | 1, 3, 10 |
| Dossiers partagés (VMs, conteneurs, hôtes), annuler | 12, 13, 14, 15 |

## Points à trancher

1. **Schéma de ligne commun « prévu en 1.97.0 » absent.** La conception le cite comme existant ; `master` n'a ni `common_row`, ni `memory_bytes`, ni `uuid` KVM (inventaire en `list --all`). Le plan le livre en tâche 11, avant les dossiers. À confirmer : livré dans 2.0.0 (recommandé) ou en 1.x séparé avant.
2. **Adresse vide en 2.0.0.** Ouvrir `#vms` (défaut de la conception, la VM est l'objet le plus cherché) ou le dernier écran (`harvester_ops_current_tab`, habitude actuelle) ? Recommandation : `#vms`, et l'ancien réglage lu une seule fois à la première ouverture de la 2.0.0 pour ne pas surprendre.
3. **Sens de l'ancien `#containers`.** Aujourd'hui il désigne les conteneurs du cluster courant ; en 2.0.0 `#containers` est la racine de flotte. Le même jeton ne peut pas tenir les deux sens. Recommandation : `#containers` = racine (conception), un ancien lien perd seulement le filtre de cluster.
4. **Ajouts au schéma de `folders.db`.** La conception fixe `folders(...)` et `memberships(...)` ; il faut en plus `folders.origin` (« recréé depuis les VMs »), une table `moves` (reprise d'un renommage, « Annuler ») et `favorites`. Bornes non fixées : profondeur (proposé 8 niveaux), longueur de chemin (proposé 253 caractères, sous la limite d'une valeur d'annotation), nom de dossier (1 à 63 caractères). Et la colonne `vm_key` porte aussi des clés de conteneurs et d'hôtes (nom gardé). Écriture du cache par la console sans `ActionRun` (lecture qui réconcilie) : à valider comme exception à « seule la CLI écrit ».
5. **Favoris hors `ActionRun`.** Préférence personnelle, écrite par la console sous `/api/me/` comme le mot de passe, sans action suivie ni CLI. Sinon : un `harvester-folders.py favorite`, mais une étoile dans le dock serait du bruit. Recommandation : exception documentée. Aucun stockage de « préférences de compte » n'existe aujourd'hui (seul `/api/me/password`), contrairement à ce que dit la conception.
6. **Où régler le plafond de 8.** `config.yaml` (`ui.host_family_cap`, relu à chaud, lecture seule pour le service packagé, donc édition par l'exploitant) ou réglage de la console modifiable par un admin dans Réglages > Apparence (stocké dans l'état). Recommandation : réglage de la console (règle « applicable à chaud »), `config.yaml` comme valeur de départ.
7. **Vues de réglages qui dépendent d'un cluster.** Réglages > Comptes du cluster (`stab-husers`, `harvester-users.js`) lit aujourd'hui le cluster du sélecteur et n'a pas de ligne dans le tableau de correspondance. Proposé : garder dans Réglages avec le dernier hôte Harvester affiché et un menu pour en changer, ou le déplacer en Paramètres > Sécurité de chaque hôte.
8. **Onglet Étiquettes d'un hôte KVM.** Aucune route ne modifie les étiquettes d'un hôte déclaré (`kvm_decl` les écrit seulement à la déclaration). Lecture seule en 2.0.0, ou ajout d'une modification (admin, action suivie, `harvester-kvm.py host label`) ?
9. **Route DR d'un hôte KVM.** `GET /api/replication/hosts/<cluster>/resources` prend un nom de cluster et rend `host_kind: "harvester"` : un hôte KVM du même nom qu'un cluster y verrait les ressources du cluster. Le plan n'appelle pas la route pour un hôte KVM (état vide côté navigateur). À trancher pour plus tard : `?kind=kvm` ou chemin `/hosts/<kind>/<nom>/resources`.
10. **Coût de lecture des dossiers KVM.** Une commande `virsh metadata` par domaine, dans le batch d'inventaire existant : 40 hôtes de 50 domaines = 2 000 commandes par lecture de flotte (un SSH par hôte). Alternative : lire les dossiers seulement depuis `virsh dumpxml` à la demande et s'appuyer sur le cache. À mesurer sur node2 avant de figer.
11. **Sortes de charges rangeables.** La conception cite Deployment, StatefulSet, DaemonSet ; la vue Conteneurs montre aussi Job, CronJob, ReplicaSet et Pod (`hv_containers.WORKLOAD_RESOURCES`). Proposé : CronJob en plus (objet durable), Job, ReplicaSet et Pod non rangeables (éphémères ou possédés).
12. **Dossier d'un cluster déclaré dans `config.yaml`.** La conception écrit le dossier dans la déclaration de l'hôte ; un cluster de `config.yaml` (`origin: config`) n'a pas de déclaration modifiable par le service packagé. Proposé : base seulement (`source=console`), avec la pastille.
13. **Rôle requis pour ranger un hôte.** Déclarer ou modifier un cluster est admin (`/api/clusters` dans `ADMIN_ONLY_PREFIXES`), ranger un hôte est operator (conception). Le plan écrit seulement le champ `folder` de la déclaration, sous `/api/folders`. À confirmer.
14. **Entrée « Dossiers » et adresses ajoutées.** La décision du 25/10 place « Dossiers » en haut du menu, mais l'arborescence et le tableau des adresses de la conception ne l'ont pas : le plan ajoute `#folders`, `#folders/<id>`, `#containers/folder/<id>`, `#hosts/folder/<id>`, `#vms/host/<famille>`, `#harvester/<c>/overview/events`, `#harvester/<c>/imports/<section>/<pane>`, `#activity/host/<kind>/<nom>`.
15. **Numéros de version pendant le chantier.** Étape 1 publiée seule en 1.100.0 ; les étapes 2 à 4 vivent sur une branche qui doit, selon la règle du dépôt, changer `VERSION` à chaque commit visible. Proposé : `VERSION` à 2.0.0 dès le premier commit de l'étape 2 et une seule entrée de CHANGELOG complétée au fil des commits, la branche n'étant fusionnée qu'en fin de chantier.
16. **Info-bulle de la maquette contraire à la décision.** La maquette (ligne 213) parle encore d'un « interrupteur de navigation dans Apparence » ; la décision du 25/10 l'exclut. Le plan ne le reprend pas.
