# Architecture

```
Poste d'exploitation
└─ Conteneur harvester-ops  (registry.suse.com/bci/python:3.11)
   │
   └─ Console Flask  (menu des hôtes · dock d'actions · flux SSE · multi-hôtes)
        │
        ├──▶ bin/harvester-{shutdown,startup,status}.sh  — moteur de séquençage (SSE)
        ├──▶ kubectl  — cycle VM, topologie, status
        ├──▶ CAPHV / clusterctl  — clusters RKE2 downstream
        ├──▶ terraform  — déclarations (IaC)
        └──▶ Redfish  — découverte BMC + alimentation

   auth      ▶ Basic auth + TLS (navigateur) · kubectl + ssh (clusters) · redfish (BMC)
   atteint   ▶ clusters Harvester (prod, staging, …) : control-plane + workers,
               Harvester + Longhorn + KubeVirt
```

## Deux types d'opération

harvester-ops pilote les clusters par **deux voies**, et la distinction
compte pour la confiance et pour l'airgap :

1. Le **séquençage électrique** passe par les **scripts bash uniquement**.
   La console les spawn ; elle ne réimplémente jamais la logique
   shutdown/startup. C'est le cœur auditable, indépendant de l'UI.
2. **Tout le reste** (cycle VM, topologie, Cluster API, Terraform,
   bare-metal) est la console Flask qui parle à **`kubectl` / providers /
   Redfish directement**. Ces surfaces n'existent que dans la console.

La garantie forte — *« l'UI ne bypass jamais les scripts »* — s'applique
donc au **séquençage électrique**, le seul endroit où se tromper d'ordre
peut perdre des données. Les surfaces de plus haut niveau sont des clients
d'API classiques.

## Rôles des composants

### Scripts bash (`bin/`)

- **Seule voie d'exécution du séquençage électrique**. La console ne les
  bypass jamais pour le shutdown/startup.
- Autonomes : dépendances `bash`, `kubectl`, `ssh`, `yq`, `python3`
  (quelques transformations façon jq).
- Émettent des lignes structurées `STEP_EVENT|<id>|<status>|<msg>` sur
  stderr, parsées par la console en événements SSE.
- Lisent la config depuis `/etc/harvester-ops/config.yaml`.

### Outils annexes (`bin/`, Python)

- `harvester-provider-install.py` installe ou met à jour le provider
  Terraform depuis une version, une URL ou un fichier local. Bibliothèque
  standard uniquement, pour tourner sur un hôte airgap où n'existent que
  `python3` et le tarball décompressé.
- Même contrat `STEP_EVENT` que les scripts bash : la console les diffuse
  par le même chemin SSE. La console lance ce script au lieu de
  réimplémenter l'installation, ce qui maintient la parité CLI / UI.
- `harvester-vm-transfer.py` (1.45.0) contrôle, déplace, exporte et importe
  une VM entre clusters. Ses décisions vivent dans `bin/lib/vm_transfer.py`
  (pur, testé sans cluster), ses deux moteurs dans
  `bin/lib/vm_transfer_run.py` (qui pilote les clusters par un client
  injectable), le guichet HTTP qui sert les disques à CDI dans
  `bin/lib/vm_transfer_serve.py`, et la place allouable Longhorn qu'il
  partage avec la console dans `bin/lib/longhorn_room.py`.

### Bibliothèque commune (`bin/lib/common.sh`)

- Logging (TTY coloré + fichier de log plain).
- `confirm()` / `interactive_pause()` pour le mode interactif.
- Wrapper `run` respectant `--dry-run`.
- Chargeur de cluster (parse YAML, fixe `KUBECONFIG`, options SSH, liste
  des nodes).
- Helpers VM : `get_ordered_vms`, `snapshot_vm`, `wait_for_vm_ready`,
  `set_vm_priority`.
- `emit_event()` pour les événements d'étape compatibles SSE.

### Console Flask (`web/`)

- **Un conteneur Flask** servant tous les clusters configurés.
- **Surface de séquençage** : spawn les scripts bash via
  `subprocess.Popen` et stream leur stderr via SSE.
- **Surfaces API directes** : cycle de vie VM, topologie et status
  cluster, install Cluster API / CAPHV + CRUD de clusters, déclarations
  Terraform, BMC / Redfish — implémentées en appels `kubectl` / provider /
  Redfish.
- **Modèle d'action** : chaque appel mutatif devient un `ActionRun` avec
  un log d'événements live, affiché dans le dock persistant et conservé
  dans un historique SQLite (mode WAL).
- **Notes collaboratives** : un endpoint de sync WebSocket sous-tend les
  notes Yjs, persistées en SQLite.
- **Observabilité** : `/metrics` Prometheus, `/healthz` (liveness),
  `/healthz/ready` (readiness).
- **État** : seuls l'historique d'actions et les notes sont persistés ;
  l'app est redémarrable à tout moment. Auth HTTP Basic (htpasswd) sur
  TLS ; OIDC est un candidat pour une version future.

### Image conteneur

- Base : `registry.suse.com/bci/python:3.11`.
- Contient `kubectl`, `yq`, `openssh-clients`, `xorriso`, OpenTofu
  (l'équivalent ouvert et compatible de Terraform, MPL-2.0), les wheels
  Python (offline), les scripts bash et la console.
- Construit via `container/Containerfile`, sauvegardé en tar OCI
  (`images/harvester-ops-ui.tar`) pour la livraison airgap.
- **Prêt à l'emploi (1.51.0)** : le livrable porte aussi le paquet
  Cluster API du moment (fournisseurs et leurs images, airgap) et le
  provider Terraform pour Harvester, épinglés dans
  `scripts/embedded-providers.env`. `install.sh` les pose dans l'état
  persistant du service (`/var/lib/harvester-ops`), actifs dès le premier
  démarrage ; un paquet ou un provider choisi ensuite par l'exploitant n'est
  jamais écrasé par une nouvelle installation.

## Modèle multi-hôtes

Un hôte est un cluster Harvester entier ou un hyperviseur KVM, jamais un
nœud Harvester. Les clusters sont déclarés dans `config.yaml` et dans
`clusters.d/` (déclarations de la console), les hôtes KVM dans `kvm.d/`.
Chaque commande CLI (`--cluster <nom>`, `--host <nom>`) charge exactement le
contexte d'un hôte, et toute mutation agit sur des objets nommés d'hôtes
nommés.

La console lit la flotte d'un coup là où un écran en a besoin : le menu
latéral (`GET /api/hosts`), Machines virtuelles
(`/api/vms-all?kinds=harvester,kvm`), Conteneurs (`/api/containers-all`),
GPU (`/api/gpus-all`) et les compteurs des dossiers. Ces lectures sont
partagées entre les personnes qui obtiendraient la même réponse
(`shared_read`) : la clé porte le kubeconfig rendu pour la personne quand la
route nomme un cluster et, depuis la 2.0.0, l'identité que la personne
présente aux clusters (session Rancher, ou utilisateur et groupes délégués,
`_read_share_identity`) quand elle n'en nomme pas. Les comptes locaux sans
délégation d'identité emploient tous le kubeconfig de la console et
partagent encore une lecture par rôle.

## Navigation et adresses

Chaque écran de la console a une adresse après `#` : un lien ouvre l'écran
exact et un rechargement (F5) le rend. Deux modules du navigateur la portent :

- `web/static/js/routes.js` est pur (ni DOM ni réseau, testé dans node). Il
  lit une adresse en route `{name, kind, host, sub, tab, pane, id}`, l'écrit
  à l'inverse (`encodeURIComponent` sur chaque segment) et traduit les
  anciens jetons (`#storage`, `#allvms`, `#kvmhosts`...), qui restent compris.
  Les noms d'hôte sont contrôlés par la règle de déclaration du serveur
  (lettres, chiffres, `.`, `_`, `-`), la même pour un cluster Harvester et
  un hôte KVM.
- `web/static/js/router.js` applique une adresse : le cluster d'abord, puis
  l'onglet, puis le pane. C'est le seul chemin d'ouverture d'un écran : le
  menu des hôtes est fait de vrais liens, et le menu de la 1.x (sélecteur
  de cluster et groupes par cluster) est retiré depuis la 2.0.0. Un clic dans
  un écran (onglet de section) réécrit l'adresse sans ajouter d'entrée
  d'historique ; un lien du menu ou d'une vue vers une autre
  (`App.openHost`) en ajoute une, et Précédent revient à l'écran d'avant. Un hôte inconnu ou caché par
  Rancher ouvre l'index des hôtes (`#hosts`) avec un message, comme un
  ancien jeton qui demande un hôte alors qu'aucun n'est visible ; une
  adresse qui ne mène à aucun écran ouvre l'écran par défaut (`#vms`) avec
  un message. Jamais une page vide. Une adresse vide ouvre `#vms` ; la
  première fois que la 2.0.0 s'ouvre dans un navigateur, elle rouvre à la
  place le dernier écran du menu de la 1.x (l'onglet retenu par la 1.x),
  une seule fois, puis l'oublie.

Grammaire des adresses (`<c>` = cluster Harvester, `<h>` = hôte KVM) :

| Adresse | Écran |
|---|---|
| `#vms`, `#vms/folder/<id>`, `#vms/host/<harvester ou kvm>[/<nom>]` | machines virtuelles |
| `#folders[/<id>]`, `#containers[/folder/<id>]`, `#hosts[/harvester, /kvm ou /folder/<id>]` | dossiers, conteneurs, hôtes |
| `#harvester/<c>`, `#harvester/<c>/overview/events` | aperçu d'un cluster |
| `#harvester/<c>/vms`, `#harvester/<c>/containers`, `#harvester/<c>/dr` | VMs, conteneurs, reprise après sinistre d'un cluster |
| `#harvester/<c>/imports/<vmimport ou forklift>[/<pane>]` | imports et migrations VMware |
| `#harvester/<c>/params/<onglet>[/<pane>]` | paramètres : `nodes`, `storage`, `network`, `addons`, `security`, `monlog`, `advanced`, `upgrade`, `power`, `conn` |
| `#kvm/<h>`, `#kvm/<h>/vms`, `#kvm/<h>/dr`, `#kvm/<h>/params/<system, pools, networks, labels ou conn>` | hôte KVM |
| `#dr`, `#dr/<contrat>`, `#dr/links` | contrats de réplication, liens |
| `#migrations`, `#gpus`, `#bookings`, `#usage`, `#automation/<capi, terraform ou pxe>`, `#activity[/host/<famille>/<nom>]`, `#activity/action/<id>` | écrans de flotte (une action : l'Activité filtrée sur elle, son détail ouvert) |

Deux autres modules portent le menu de la 2.0 : `web/static/js/host-nav.js`
(le menu latéral des hôtes, alimenté par `GET /api/hosts`, ordre et recherche
purs testés dans node) et `web/static/js/host-page.js` (le bandeau de la page
d'un hôte, Harvester ou KVM, posé sur la section que l'adresse désigne). Le
routeur appelle `HostNav.sync(route)` et `HostPage.show(route)` après chaque
écran. Sur la page d'un hôte KVM, les vues KVM reçoivent l'hôte de la route
(`KVM.start(vue, el, {host})`) : leur filtre d'hôte est imposé et masqué. Ses
Paramètres > Système lisent `GET /api/kvm/hosts/<h>` ; Étiquettes et Connexion
lisent `GET /api/kvm/hosts/<h>/declaration` (aucun SSH, empreintes épinglées,
jamais un chemin de clé) ; les étiquettes s'écrivent par `PUT
/api/kvm/hosts/<h>/labels` (administrateur), action suivie de
`harvester-kvm.py host label`.

Les modules ne lisent jamais un sélecteur de cluster (il n'y en a plus depuis la 2.0.0) : ils passent par
`App.getCurrentCluster()`, `App.listClusters()`, `App.managementCluster()`
(cluster d'où Cluster API et Terraform gèrent, le dernier cluster visité
sauf choix contraire) et `App.openHost(famille, nom, sub, onglet, pane)`.

### Pages d'un hôte

Une page d'hôte est un bandeau (famille, nom, état, version, nombre de VMs,
sous-menu, Activité de cet hôte) posé au-dessus de la section existante que
l'adresse désigne ; les sections elles-mêmes ne sont pas réécrites. Sur un
hôte Harvester, les Paramètres sont des onglets sur les anciennes sections
Cluster (`Sections.DEF` donne leurs panneaux) ; la Vue d'ensemble montre les
métriques et, dessous, le bloc Événements, qui relit `/api/events/<c>`
toutes les 20 s tant que la page est visible (un lecteur périodique de plus
par personne qu'en 1.x, où Événements était un onglet).

### Liste des hôtes et favoris

`GET /api/hosts` donne au menu latéral ses hôtes : chaque cluster Harvester
que la personne voit (une session Rancher ne reçoit pas les clusters que
Rancher lui cache) et chaque hôte KVM déclaré, avec un état (`ok`,
`unreachable`, `denied`, `error`, `unknown`), le nombre de VMs, la version,
les étiquettes, l'origine (`config` ou `console`) et l'étoile de la personne.
Chaque famille a son résumé (`ok`, `alert`, `unknown`). Pas de plafond : tous
les hôtes sont listés.

La liste ne lit jamais un hôte. L'état d'un cluster vient de la sonde TCP en
cache de son serveur d'API ; son nombre de VMs et sa version viennent des
lectures partagées que les écrans ont déjà faites (`/api/vms-all`,
`/api/status/<c>`, `/api/upgrade/<c>`, de 10 minutes au plus,
`HARVESTER_OPS_HOSTS_PEEK_AGE`) ; l'état et le nombre de VMs d'un hôte KVM
viennent de la dernière lecture de `/api/kvm/hosts`. Ce qui n'a pas encore été
lu vaut `null` ou `unknown`.

Les favoris sont une préférence personnelle : `PUT` et `DELETE
/api/me/favorites/<famille>/<hôte>` (tout rôle, comme changer son propre mot
de passe), rangés par personne dans la base de la console `folders.db` (à côté
de `notes.db`, ou `HARVESTER_OPS_FOLDERS_DB`), retrouvés depuis un autre poste.
Ce ne sont pas des actions suivies et il n'y a pas de ligne de commande : rien
n'est écrit sur un cluster ou un hyperviseur.

## Dossiers (2.0.0)

Un dossier range des VMs (Harvester et KVM), des charges de conteneurs
(Deployment, StatefulSet, DaemonSet, CronJob) et des hôtes. Un objet est dans
un seul dossier, sans héritage : ranger un hôte ne range pas ses VMs.

Le dossier d'un objet est écrit **sur l'objet**, qui fait foi :

| Objet | Où | Clé dans le cache |
|---|---|---|
| VM Harvester, charge de conteneurs | annotation `harvester-ops.io/folder` (merge patch, `null` pour retirer) | `harvester/<cluster>/<ns>/<nom>`, `container/<cluster>/<ns>/<sorte>/<nom>` |
| Domaine KVM | métadonnées libvirt `<hops:folder xmlns:hops="https://harvester-ops.io/xmlns/folder/1" path="..."/>` (`--config`, et `--live` s'il tourne) | `kvm/<uuid>` (un domaine renommé la garde) |
| Hôte | champ `folder` de `clusters.d/<nom>.yaml` ou `kvm.d/<nom>.yaml` | `host/harvester/<nom>`, `host/kvm/<nom>` |

La base de la console `folders.db` garde l'arborescence (`folders` :
identifiant, parent, nom, position, auteur, `origin` = `console`, ou
`objects` pour un dossier recréé depuis les objets qui le nomment), un cache
des rangements (`memberships` : clé, chemin, `source` = `vm` lu sur l'objet
ou `console` quand la personne n'a pas le droit d'écrire l'objet, `seen_at`,
`scope` = la lecture qui l'a vu) et le journal de chaque action sur les
dossiers (`moves`, une ligne par objet : reprise d'une action interrompue,
Annuler). Un chemin est absolu, de 8 niveaux et 253 caractères au plus ; un
nom a de 1 à 63 caractères, sans `/` ni caractère de contrôle, et n'est ni
`.` ni `..`. Deux frères ne portent jamais le même nom.

Chaque lecture de flotte réaligne le cache : une valeur sur l'objet
l'emporte et crée les dossiers qu'elle nomme ; un objet sans valeur garde un
rangement `console` et perd un rangement `vm` (quelqu'un l'a retiré sur
l'objet). Cette écriture se fait pendant une lecture, sans action suivie :
c'est l'exception documentée, puisqu'elle ne fait que recopier ce que disent
les objets. Une entrée non vue depuis 30 jours est retirée, mais seulement
après une lecture réussie de son cluster ou de son hôte : un hôte injoignable
garde ses entrées.

Les dossiers KVM sont lus par une seule invocation de `virsh` par hôte, qui
enchaîne les lectures `metadata` de tous les domaines, dans un second appel
SSH sur la même connexion. Mesuré sur un hôte de 15 domaines (libvirt
12.8.0) : l'inventaire seul prend 1,7 s, 0,3 s de plus avec cette lecture,
contre 2,7 s de plus pour une commande `virsh metadata` par domaine et 2,9 s
de plus pour un `dumpxml` par domaine. Une erreur de virsh autre que « pas de
métadonnées » fait relire l'hôte domaine par domaine : un échec n'est jamais
pris pour « pas de dossier ».

Une lecture de flotte crée chaque dossier nommé par des objets, sans
plafond. Une lecture
partie avant la fin d'un renommage ou d'un déplacement (kubectl et SSH
prennent quelques secondes) rend encore l'ancien chemin : pendant deux
minutes après le changement, ou tant que des objets restent à réécrire, ce
chemin est ramené sur le nouveau au lieu de recréer l'ancien dossier.

Toute modification passe par `bin/harvester-folders.py` (voir Capacités) ;
la console le lance depuis les routes sous `/api/folders`. Dans le
navigateur, `web/static/js/folders.js` (`window.Folders`) dessine le panneau
de la vue Machines virtuelles et de la vue Dossiers, et les fenêtres
« Déplacer vers un dossier » et « Déplacer le contenu » ; `all-vms.js` est la
vue Machines virtuelles (`#vms`, `#vms/folder/<id>`, `#vms/folder/unfiled`,
`#vms/host/<famille>/<hôte>`) ; `FoldersView` (dans `folders.js`) est
`#folders`.

### Gestes sur les dossiers : contrôles, journal, annulation, reprise

Avant un geste, `POST /api/folders/check` dit objet par objet ce qui va se
passer : écrit sur l'objet (`kubectl auth can-i` avec l'identité de la
personne, une revue par cluster, namespace et ressource ; hôte KVM joint et
domaine persistant ; déclaration d'hôte modifiable), rangé dans la console
seulement, ou refusé et pourquoi. La fenêtre montre ces lignes avant son
bouton.

Chaque geste est un `ActionRun` (dock, Activité) qui lance
`harvester-folders.py` avec le rôle et le kubeconfig de la personne. L'outil
écrit une ligne `moves` par objet (`pending`, puis `done`, `failed` ou
`skipped`) avec la valeur lue sur l'objet juste avant de l'écrire, d'où :

- **Annuler** (`POST /api/folders/actions/<id>/undo`) remet exactement
  cette valeur ; ce dont le bouton a besoin est gardé dans la colonne
  additive `undo` d'`actions.db`, donc après un redémarrage de la console,
  et l'Activité le propose une fois la carte du dock partie.
- **Reprendre** (`POST /api/folders/actions/<id>/resume`) finit seulement
  les lignes `pending` et `failed` d'une action finie en erreur ou arrêtée
  par l'arrêt de la console ; une ligne `done` n'est jamais réécrite, et un
  objet rangé ailleurs depuis est laissé. Le serveur ajoute un chemin
  `resume` à une telle action tant qu'il reste des lignes
  (`_folder_resume`), dont le dock et l'Activité font un bouton. Une action
  de dossiers est rangée dans `actions.db` dès son départ : coupée par un
  arrêt de la console, elle revient `interrupted` au redémarrage, avec
  Reprendre et Annuler.
- Un objet que la personne ne voit pas (cluster masqué, namespace illisible)
  n'est jamais nommé dans les étapes, la sortie ou `--json` : il est compté
  (« 2 other objects not shown to you »), et sa vraie clé reste
  dans le journal seulement.

## Écrans de Disaster Recovery (2.0.0)

Les écrans de réplication de la 1.101.0 (`web/static/js/replication.js`,
`replication-plan.js`, `replication-create.js`...) sont placés dans la
navigation, pas reconstruits : `#dr` liste les contrats, `#dr/<id>` ouvre la
fenêtre d'un contrat au-dessus de la liste (la fermer rend `#dr`), et
`#dr/links` liste les liens entre sites. Le Disaster Recovery d'un hôte
Harvester (`#harvester/<c>/dr`) lit `GET
/api/replication/hosts/harvester/<c>/resources` et montre les contrats où
l'hôte est la source ou le site de secours ; `#kvm/<h>/dr` dit que la
réplication KVM n'est pas encore disponible et ne lit rien. La pastille DR
de la ligne d'un hôte dans le menu latéral compte ses alertes comme source
et comme site de secours.

Le navigateur ne porte aucune règle d'état : chaque bouton suit les
`actions` de la ligne du contrat (`plan_row()` et `resource_row()` dans
`bin/lib/replication.py`), chacune avec son `role`, `allowed` et `reason`.
**Reprendre la commande**, dans la fenêtre d'un contrat, suit une action
`resume-command` que la console ajoute à la ligne quand
`replication_run.resumable` dit que la commande `resume` a quelque chose à
finir, avec le rôle du geste qu'elle finit (admin pour une bascule).

## Données de la console

| Stockage | Contenu | Écrit par |
|---|---|---|
| `actions.db` (SQLite WAL) | chaque `ActionRun`, ses événements et son résultat, `undo` (2.0.0, colonne additive) | la console |
| `folders.db` (SQLite WAL, 0600 avec ses `-wal` et `-shm`) | `favorites`, `folders`, `memberships` (cache), `moves` (journal) | la console (favoris, réalignement du cache pendant une lecture), `harvester-folders.py` (tout le reste) |
| `replication.db` (SQLite WAL) | contrats, leurs VMs et volumes, passes, mesures, exécutions, événements | `harvester-replication.py`, son suiveur |
| objets | annotation `harvester-ops.io/folder`, métadonnées libvirt, champ `folder` d'une déclaration d'hôte | `harvester-folders.py` |

## Flux d'événements SSE

Quand la console déclenche une action (un shutdown ici) :

```
client                          Flask                       script bash
  │ POST /api/action              │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ subprocess.Popen             │
  │                               │ ──────────────────────────▶  │
  │ GET /api/stream/<id> (SSE)    │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ read stderr ligne par ligne  │
  │                               │ ◀──── "STEP_EVENT|vm-stop|   │
  │                               │        running|..."          │
  │ event: step                   │                              │
  │ data: {"id":"vm-stop",..}     │                              │
  │ ◀───────────────────────────  │                              │
```

Le frontend (vanilla JS + un wrapper `EventSource` à reconnexion
automatique) met à jour les indicateurs d'étape en temps réel sans
polling. Les actions API directes (ops VM, apply Terraform, install
CAPHV) utilisent le même mécanisme `ActionRun` + SSE, streamant les
événements step/log au fil de leur progression.

## Modèle de confiance

- Le conteneur dispose d'un accès `cluster-admin` complet via les
  kubeconfigs et clés SSH montés, et peut atteindre les BMC en Redfish.
- Il doit tourner sur un **poste opérateur de confiance** ou un jump host
  dédié.
- Les path params correspondant à des noms Kubernetes sont validés
  (RFC 1123) à la frontière HTTP ; les endpoints mutatifs portent auth +
  rate-limiting ; les kubeconfigs sont stagés en permissions `0600` par
  workspace Terraform.
- Basic auth + TLS protègent la console. Pour plus de sécurité, placer
  derrière un reverse proxy avec mTLS ou OIDC.
