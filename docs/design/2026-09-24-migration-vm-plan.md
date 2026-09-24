# Déplacer une VM d'un cluster à l'autre : plan d'implémentation

Mise en œuvre de `docs/design/2026-09-24-migration-vm.md`, tâche par tâche,
chaque tâche avec ses tests. Cases à cocher pour le suivi.

**Objectif :** transférer une VM entre deux clusters déclarés (par la cible
de sauvegarde commune, ou par la console), l'exporter dans un fichier et
l'importer, depuis la console comme en ligne de commande.

**Architecture :** un moteur CLI `bin/harvester-vm-transfer.py` (stdlib +
`kubectl`), dont toute la logique testable vit dans `bin/lib/` : décisions
pures (`vm_transfer.py`), guichet HTTP des disques (`vm_transfer_serve.py`),
déroulé des moteurs sur une interface `Kube` injectable
(`vm_transfer_run.py`). La console lance le script dans un ActionRun, relaie
ses STEP_EVENT, et affiche assistant et magasin d'exports.

**Technique :** Python 3 stdlib, Flask existant, JS vanilla (IIFE), pytest,
Playwright ; côté cluster : `VirtualMachineBackup`/`Restore` de Harvester,
`VirtualMachineImage` `export-from-volume`, `DataVolume` CDI de source HTTP.

## Contraintes globales

- Version livrée : `1.45.0` (VERSION + CHANGELOG dans le commit qui l'apporte).
- Aucune dépendance nouvelle ; le script tourne sur un hôte airgap avec
  python3, kubectl et yq (comme les scripts bash).
- Étiquette de tout ce qu'un transfert crée : `harvester-ops.io/transfer=<id>` ;
  annotation de la source transférée : `harvester-ops.io/transferred-to`.
- Le serveur ne renvoie que des codes et des faits ; les textes sont traduits
  dans les cinq langues (en, fr, de, es, it) via `i18n.t('clé')` littéral.
- Aucun secret (cloud-init) dans une réponse, un libellé d'action ou un
  journal ; archive en 0600.
- Pas de tiret cadratin ni de flèche Unicode dans le contenu public ; aucune
  mention d'outil d'IA.
- `escapeHtml` avant toute interpolation dans `innerHTML` ; bulle d'aide sur
  chaque contrôle ; `@requires_auth` partout, `@_rate_limit` sur les mutations.
- Kubeconfigs du transfert calculés dans le thread de la requête
  (`_kubectl_for_cluster`), jamais dans le thread de travail (pas de contexte
  d'identité).
- Tests API avant chaque commit : `python3 -m pytest tests/api/ -q`.

---

### Tâche 0 : les deux inconnues du moteur fichier (FAIT le 24/09/2026)

Vérifié sur harv1, consigné dans la spec : export de volume, téléchargement
gzip par `kubectl get --raw`, import CDI HTTP (HEAD puis GET, sans Range),
VM démarrée sur le volume importé, `DataVolume` à supprimer en
`--cascade=orphan`. Conséquence pour le plan : `kubectl get --raw` suffit à
lire le flux (pas de `kubectl proxy`).

### Tâche 1 : la place allouable partagée (`bin/lib/longhorn_room.py`)

**Fichiers :**
- Créer : `bin/lib/longhorn_room.py`
- Modifier : `web/app.py` (`_storage_room` et ses aides deviennent un import)
- Tests : `tests/api/test_longhorn_room.py`

**Interfaces produites :**
- `storage_room(lh_nodes: list, storage_classes: list, over: int, minimal: int) -> {"disks": list, "classes": {nom: {"replicas": int, "allocatable": int, "reason": str|None}}, "schedulable_nodes": int}`
  (signature et résultat identiques à l'actuel `_storage_room`).
- `web/app.py` ajoute `BIN_DIR / "lib"` à `sys.path` une seule fois et garde
  le nom `_storage_room` (alias), sans toucher aux appelants.

- [ ] Tests : les cas de `_storage_room` couverts aujourd'hui, rejoués sur
  `longhorn_room.storage_room` (sur-provisionnement, place réelle, nœud non
  planifiable, trop peu de nœuds pour les répliques) ; `app._storage_room is
  longhorn_room.storage_room`.
- [ ] Échec constaté, déplacement, tests verts, suite API verte.
- [ ] Commit.

### Tâche 2 : les décisions pures (`bin/lib/vm_transfer.py`)

**Fichiers :**
- Créer : `bin/lib/vm_transfer.py`
- Tests : `tests/api/test_vm_transfer_logic.py`

**Interfaces produites :**
- Constantes : `TRANSFER_LABEL`, `TRANSFERRED_TO`, `FORMAT = 1`,
  `ARCHIVE_SUFFIX = ".hvx"`.
- `step(sid: str, status: str, msg: str = "") -> None` : écrit
  `STEP_EVENT|sid|status|msg` sur stderr (retours à la ligne neutralisés).
- `version_tuple(v: str) -> tuple` : `"v1.9.0"` donne `(1, 9, 0)`, inconnu `(0, 0, 0)`.
- `normalize_backup_target(raw: str|dict|None) -> tuple|None` : NFS
  `("nfs", "hôte:/chemin")` (hôte en minuscules, `/` final retiré, préfixe
  `nfs://` retiré) ; S3 `("s3", endpoint, bucketName, bucketRegion)` ; vide ou
  illisible `None`.
- `vm_inventory(vm: dict, pvcs: dict, secrets: list) -> dict` avec
  `disks: [{"volume", "claim", "size", "storage_class", "access_modes", "volume_mode", "image"}]`,
  `networks: [ns/nom]`, `secrets: [nom]`, `devices: [str]`,
  `node_affinity: bool`, `run_strategy: str`, `running: bool`. `image` vient
  de l'annotation `harvesterhci.io/imageId` du PVC.
- `default_mappings(inv: dict, target: dict, engine: str) -> {"networks": {src: dst|None}, "storage_classes": {src: dst|None}}` :
  même nom s'il existe sur la cible ; sinon réseau `None`, classe = classe
  par défaut de la cible. En moteur fichier, une classe d'image (`lh-*`,
  `longhorn-image-*`) va vers la classe par défaut de la cible.
- `sanitize_vm(vm: dict) -> dict` : sans `status`, `uid`, `resourceVersion`,
  `creationTimestamp`, `generation`, `managedFields`, `ownerReferences`,
  `finalizers` ; annotations retirées : `kubevirt.io/*`,
  `kubectl.kubernetes.io/*`, `harvesterhci.io/volumeClaimTemplates`,
  `harvesterhci.io/mac-address`, `harvesterhci.io/vmRunStrategy`,
  `network.harvesterhci.io/ips`, `TRANSFERRED_TO` ; étiquette
  `TRANSFER_LABEL` retirée.
- `retarget_vm(vm: dict, *, name, namespace, networks: dict, claims: dict, secrets: dict, keep_mac: bool, transfer_id: str) -> (dict, list[str])` :
  renomme (y compris `spec.template.metadata.labels["harvesterhci.io/vmName"]`),
  réécrit `multus.networkName`, `persistentVolumeClaim.claimName`, les
  `secretRef`/`networkDataSecretRef`/`userDataSecretRef` cloud-init, retire
  les `macAddress` si `keep_mac` est faux, retire `hostDevices`, `gpus`,
  `nodeSelector` et les termes d'affinité sur `kubernetes.io/hostname`
  (renvoyés dans la liste des retraits), retire les termes
  `network.harvesterhci.io/*` (le webhook les recalcule), pose
  `runStrategy: Halted` et `TRANSFER_LABEL`.
- Manifestes : `backup_manifest(ns, vm, name, tid)`,
  `restore_manifest(backup_ns, backup_name, name, namespace, keep_mac, tid)`
  (toujours `newVM: true`, `haltAfterRestore: true`),
  `export_image_manifest(ns, pvc, image_name, tid, target_sc)` (une réplique),
  `datavolume_manifest(ns, name, url, disk, storage_class, tid)` (modes
  d'accès et de volume du disque, `storage.bind.immediate.requested`),
  `secret_manifest(secret: dict, name, namespace, tid)`.
- `choose_engine(src: dict, dst: dict, req: dict) -> (str, str)` :
  `("backup", "shared-target")` si les deux cibles normalisées sont égales et
  `configured` des deux côtés, sinon `("file", raison)` ; `export`/`import`
  toujours `file`.
- `check(src: dict, dst: dict|None, req: dict) -> list[dict]`, constats
  `{"code", "level": "ok"|"warn"|"block", "facts": dict}` ; codes :
  `target-unreachable`, `kubevirt-missing`, `version-older`,
  `namespace-missing`, `vm-name-taken`, `network-unmapped`,
  `storage-class-unmapped`, `storage-class-missing-backup`,
  `capacity-short`, `devices-removed`, `node-affinity-removed`, `engine`,
  `image-conflict`, `image-sync-unsupported`, `cdi-missing`,
  `source-room-short`, `store-room-short`, `short-mode-needs-backup`,
  `secrets-in-archive`, `hostname-duplicate`.
- `blocking(findings) -> bool`.
- `ArchiveWriter(path)` : `add_json(name, obj)`, `add_stream(name, chunks) -> {"size", "sha256"}`
  (en-tête tar réécrit après coup, fichier créé en 0600), `close()` (écrit
  `SHA256SUMS` puis la fin d'archive).
- `ArchiveReader(path)` : `manifest() -> dict`, `member(name) -> (offset, size)`,
  `open_member(name) -> fichier borné`, `verify() -> list[str]` (membres
  corrompus ou absents).

- [ ] Tests (objets au format relevé sur harv1, dont la VM leap156) :
  nettoyage (champs et annotations retirés, étiquettes utiles gardées) ;
  inventaire (disques, image par `imageId`, secrets cloud-init, réseaux,
  périphériques) ; correspondances par défaut (même nom, classe par défaut,
  classe d'image en moteur fichier) ; `retarget_vm` sur chaque réécriture et
  chaque retrait, MAC gardées ou non ; normalisation NFS et S3 ; choix du
  moteur (cible commune, cible différente, non configurée, export) ; chaque
  code de `check` au bon niveau, et aucun `block` sur un cas nominal ;
  archive écrite puis relue avec un faux disque de 3 Mo non multiple de 512,
  sommes justes, `verify` qui détecte un octet modifié, droits 0600 ;
  `step` neutralise un `|` final et un retour à la ligne.
- [ ] Échec constaté, module écrit, tests verts, sabotage de chaque garde-fou
  (le test correspondant doit échouer).
- [ ] Commit.

### Tâche 3 : le guichet des disques (`bin/lib/vm_transfer_serve.py`)

**Fichiers :**
- Créer : `bin/lib/vm_transfer_serve.py`
- Tests : `tests/api/test_vm_transfer_serve.py`

**Interfaces produites :**
- `DiskServer(bind: str = "0.0.0.0", port: int = 0)` : `start() -> int`
  (port effectif), `publish(opener: Callable[[], Iterable[bytes]], size: int|None) -> str`
  (chemin `/<jeton>.raw.gz`, jeton `secrets.token_urlsafe(24)`),
  `hits(path) -> int`, `revoke(path)`, `stop()`.
- `HEAD` : 200 avec `Content-Length` si connu ; `GET` : flux par morceaux ;
  tout autre chemin, jeton révoqué ou méthode : 404/405 sans corps ; aucun
  listing ; journalisation sans le jeton.

- [ ] Tests : HEAD et GET d'un contenu publié (corps identique), longueur
  annoncée, jeton inconnu 404, jeton révoqué 404, racine 404, `hits`
  incrémenté par GET seulement, deux publications simultanées.
- [ ] Échec, module, verts, commit.

### Tâche 4 : le déroulé des moteurs (`bin/lib/vm_transfer_run.py`)

**Fichiers :**
- Créer : `bin/lib/vm_transfer_run.py`
- Tests : `tests/api/test_vm_transfer_run.py` (avec un `FakeKube` en mémoire)

**Interfaces produites :**
- Interface attendue d'un client : `get(kind, ns, name) -> dict|None`,
  `list(kind, ns=None, selector=None) -> list`, `create(obj)`, `apply(obj)`,
  `patch(kind, ns, name, patch: dict)`, `delete(kind, ns, name, cascade=None)`,
  `raw_stream(path) -> Iterable[bytes]`.
- `Ctx(src, dst, req, facts, tid, emit=step, sleep=time.sleep, now=time.time, server=None, writer=None, reader=None)`
  et son registre `ctx.created: list[(côté, kind, ns, nom)]`.
- `wait_for(ctx, what: str, fn: Callable[[], bool|str], timeout: float, every: float)`
  (une chaîne renvoyée par `fn` est une erreur définitive).
- `stop_vm(kube, ns, name, ctx)`, `start_vm(kube, ns, name, ctx)` (par
  `runStrategy`, attente de la disparition ou de `Running` de la VMI).
- `run_backup(ctx)` : étapes de la spec (arrêt court, arrêt, sauvegarde,
  attente sur la cible avec relance de synchro, restauration, remappage des
  réseaux, retraits) ; renvoie `(ns, nom)` de la VM cible.
- `run_export(ctx)` : images temporaires, rallumage anticipé, flux vers
  `ctx.writer` (ou publication sur `ctx.server` en transfert direct),
  suppression des images ; renvoie la liste des disques publiés.
- `run_import(ctx, sources: dict[volume -> opener])` : namespace, secrets,
  `DataVolume`s, attente, suppression en `--cascade=orphan`, VM ; renvoie
  `(ns, nom)`.
- `finalize(ctx, target: tuple)` : vérification de la cible, états finaux
  (source en marche, arrêtée annotée ou supprimée avec ses volumes après
  vérification), nettoyage des sauvegardes du transfert sauf `keep_backups`.
- `rollback(ctx)` : supprime `ctx.created` dans l'ordre inverse, remet la
  source dans son `runStrategy` de départ.

- [ ] Tests (FakeKube à états scriptés, horloge et sommeil injectés) :
  moteur sauvegarde en arrêt simple et en arrêt court (deux sauvegardes, la
  seconde après l'arrêt) ; copie sans arrêt (une seule sauvegarde) ;
  sauvegarde absente de la cible puis relance de synchro par réécriture de
  `backup-target` ; restauration avec `keepMacAddress` selon la demande ;
  réseaux remappés sur la VM restaurée ; export : rallumage de la source
  AVANT le téléchargement quand elle doit rester en marche ; images
  temporaires supprimées même en échec ; import : `DataVolume` par disque
  dans la classe choisie, `delete --cascade=orphan`, VM créée arrêtée ;
  refus d'accès (aucun hit et condition d'erreur du DataVolume) : message
  explicite ; finalize : suppression de la source seulement après
  vérification ; rollback : ressources créées supprimées, source relancée ;
  aucune ressource étiquetée restante après un échec simulé à chaque étape.
- [ ] Échec, module, verts, sabotage, commit.

### Tâche 5 : le script (`bin/harvester-vm-transfer.py`)

**Fichiers :**
- Créer : `bin/harvester-vm-transfer.py`
- Modifier : `install.sh` (lien `harvester-vm-transfer`),
  `container/Containerfile` (lien identique)
- Tests : `tests/api/test_vm_transfer_cli.py` (faux `kubectl` sur le PATH qui
  répond depuis des fichiers JSON)

**Interfaces produites :**
- `Kube(kubeconfig)` : implémentation réelle de l'interface de la tâche 4
  par `kubectl` (`-o json`, `get --raw` en flux, `create -f -`).
- Résolution d'un cluster : `--from/--to <nom>` lus dans
  `HARVESTER_OPS_CONFIG` par `yq -o=json` (repli PyYAML si importable), ou
  `--from-kubeconfig/--to-kubeconfig <fichier>`.
- `collect_source(kube, ns, name) -> dict`, `collect_target(kube, ns, name) -> dict`
  (faits attendus par `check`).
- Sous-commandes et options de la spec ; `check --json` imprime
  `{"engine", "reason", "findings", "mappings", "inventory"}` sur stdout ;
  `--dry-run` imprime le plan ; code de sortie 0 succès, 1 échec, 2 blocage
  au contrôle, 3 annulation.
- `SIGTERM`/`SIGINT` : `rollback` puis sortie 3.
- `--serve-address hôte[:port]` : adresse à laquelle la cible joint le
  guichet (défaut : l'adresse de l'hôte vers l'API du cluster cible).

- [ ] Tests : `check --json` sur fixtures (nominal, blocages) ; `--help` de
  chaque sous-commande ; résolution d'un cluster par nom via un faux `yq` ;
  code 2 quand le contrôle bloque ; `--dry-run` ne lance aucune commande
  mutative (le faux kubectl journalise ses appels).
- [ ] Échec, script, verts, commit.

### Tâche 6 : la console (points d'accès et magasin)

**Fichiers :**
- Modifier : `web/app.py`, `config/systemd/harvester-ops.service`
  (`HARVESTER_OPS_EXPORT_DIR=/var/lib/harvester-ops/exports`, volume déjà monté)
- Tests : `tests/api/test_vm_transfer_api.py`

**Interfaces produites :**
- `EXPORT_DIR` (`HARVESTER_OPS_EXPORT_DIR`, défaut
  `~/.local/share/harvester-ops/exports`), `_export_dir()`,
  `_export_safe_name(name) -> str|None` (suffixe `.hvx`, sans chemin).
- `POST /api/vm/<c>/<ns>/<n>/transfer/check` corps
  `{"to": str|null, "name", "namespace", "mode", "source", "target", "keep_mac", "networks", "storage_classes", "create_namespace"}`
  : lance `check --json` (délai 90 s) et renvoie sa sortie.
- `POST /api/vm/<c>/<ns>/<n>/transfer` même corps (+ `keep_backups`) :
  ActionRun `vm-transfer:<ns>/<n>`, 409 si un transfert de cette VM tourne
  ou si un arrêt/démarrage tourne sur la source ou la cible ; renvoie
  `{"action_id"}`.
- `GET /api/exports`, `DELETE /api/exports/<f>`,
  `GET /api/exports/<f>/download`, `POST /api/exports/<f>/check`,
  `POST /api/exports/<f>/import`.
- Adresse du guichet passée au script : réglage `transfer.serve_address` de
  `config.yaml` si présent, sinon celle de l'onglet Bare-metal si réglée,
  sinon le défaut du script.

- [ ] Tests : contrôle relayé (Popen simulé), cluster inconnu 404, cible
  inconnue 404 ; lancement crée l'ActionRun et passe les bons kubeconfigs ;
  409 pour un second transfert de la même VM et pendant un arrêt de la
  cible ; magasin : liste lit le manifeste sans charger les disques,
  suppression, nom invalide 400, téléchargement ; aucun secret dans les
  réponses ; limite de débit déclarée valide (`limits.parse_many`).
- [ ] Échec, implémentation, verts, commit.

### Tâche 7 : l'écran

**Fichiers :**
- Créer : `web/static/js/vm-transfer.js`
- Modifier : `web/static/js/vm-migrate.js` (la fenêtre « Migrer » propose
  trois destinations : un autre nœud, un autre cluster, un fichier ; décision
  du 25/09/2026), `web/static/js/app.js`,
  `web/templates/index.html` (script, bouton « Exports »),
  `web/static/js/icons.js` (icône `transfer`), `web/static/js/i18n.js`
  (clés `transfer.*`, cinq langues), `web/static/css/style.css` si besoin
- Tests : `tests/e2e/test_vm_transfer_view.py`, parité i18n existante

**Interfaces produites :**
- `VMTransfer.open(cluster, ns, name)`, `VMTransfer.openStore(cluster)`,
  `VMTransfer.openImport(file)`.

- [ ] Tests e2e (API interceptée) : l'assistant affiche un blocage en tête et
  désactive « Lancer » ; l'arrêt court n'est proposé qu'avec une cible
  commune ; changer une correspondance relance le contrôle ; « Lancer » part
  avec le bon corps ; le magasin liste un export et ouvre l'import ; textes
  en français avec la langue réglée sur fr.
- [ ] Implémentation, verts, suite e2e complète verte, commit.

### Tâche 8 : en réel, doc, version

- [ ] Banc : second cluster imbriqué sur node2 (`harvlab.sh` paramétré par
  variables d'environnement, IP réservées et enregistrées dans NetBox et
  Pi-hole), cible de sauvegarde NFS dédiée au banc sur le NAS.
- [ ] Scénarios de la spec (cible commune en arrêt simple et court, direct
  sans cible commune, export puis import, archive 1.8.2 vers harv1 1.9.0) :
  chaque fois VM vérifiée côté cible, source dans l'état choisi, aucune
  ressource étiquetée restante. Tout défaut trouvé : test qui le reproduit,
  correction, nouvel essai.
- [ ] Doc EN + FR : `capabilities.md` (nouvelle section), 
  `operating-procedure.md` (CLI), `architecture.md` (moteur),
  `troubleshooting.md` (accès du cluster cible au guichet) ; README EN + FR
  (une ligne dans les automatisations).
- [ ] `VERSION` 1.45.0, entrée CHANGELOG, suite API et e2e vertes, commit,
  poussée Gitea puis GitHub.
- [ ] Banc éteint, node2 éteint ; mémoire du projet mise à jour.
