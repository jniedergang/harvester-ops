# Volumes dégradés : plan d'implémentation

Mise en œuvre de `docs/design/2026-09-21-volumes-degrades.md`, tâche par
tâche, chaque tâche avec ses tests. Cases à cocher pour le suivi.

**Objectif :** dans la vue Stockage, dire pourquoi un volume Longhorn est
dégradé, quoi faire, et le corriger d'un clic quand c'est sûr.

**Architecture :** un module pur `web/volume_health.py` diagnostique chaque
volume à partir des objets Longhorn déjà lus par la carte du stockage (plus
`engines.longhorn.io`). `_build_storage_map` joint le diagnostic à chaque
volume. Un point d'accès de correction refait le diagnostic au moment d'agir
et lance une action tracée (vérifier, appliquer, observer). La vue Stockage
affiche bandeau, pastilles et encart « Santé ».

**Technique :** Python 3 stdlib, Flask existant, JS vanilla (IIFE, `Board`),
pytest, Playwright.

## Contraintes globales

- Version livrée : `1.42.0` (VERSION + CHANGELOG dans le commit qui l'apporte).
- Aucun appel kubectl de plus pour afficher la vue : un seul type ajouté à
  `STORAGE_KINDS`.
- Le serveur ne renvoie que des codes et des faits ; tout texte est traduit
  dans les cinq langues (en, fr, de, es, it) via `tr('clé', 'repli')` ou
  `i18n.t('clé')` littéral (le contrôle de parité ne voit que ces formes).
- Pas de tiret cadratin ni de flèche Unicode dans le contenu public ; aucune
  mention d'outil d'IA.
- `escapeHtml`/`esc` avant toute interpolation dans `innerHTML` ; bulle
  d'aide sur chaque bouton.
- Aucune correction sur un volume `faulted` ; `set-replicas` entre 1 et la
  valeur actuelle ; `rebuild-now` seulement avec une réplique saine ;
  `enable-rebuild` jamais pendant un arrêt ou un démarrage en cours.
- Tests API avant chaque commit : `python3 -m pytest tests/api/ -q`.

---

### Tâche 1 : le diagnostic (`web/volume_health.py`)

**Fichiers :**
- Créer : `web/volume_health.py`
- Tests : `tests/api/test_volume_health.py`

**Interfaces produites :**
- `setting(settings: dict, name: str, default=None, engine="v1") -> str|None`
- `node_state(lh_nodes: list) -> {node: {"ready": bool, "schedulable": bool, "disks": {uuid: {"name", "ready", "schedulable"}}}}`
- `diagnose(volume: dict, replicas: list, engine: dict|None, nodes: dict, settings: dict, disks: list) -> list[finding]`
- `health_of(volume: dict, findings: list) -> "healthy"|"degraded"|"faulted"|"at-risk"|"unknown"`
- `diagnose_all(volumes, replicas, engines, lh_nodes, settings, disks) -> {nom_volume: {"health": str, "findings": list}}`
- `finding = {"cause": str, "severity": "critical"|"action"|"watch"|"info", "facts": dict, "fix": None|{"kind": str, "params": dict}}`
- `REBUILD_LIMIT_SETTING = "concurrent-replica-rebuild-per-node-limit"`, `REBUILD_LIMIT_RESTORED = "5"`

- [ ] **Étape 1 : tests d'abord.** Un test par cause et par garde-fou, sur
  des objets au format relevé sur harv1 (volume, réplique, moteur, nœud
  Longhorn) : `faulted` sans correction et correction retirée des autres
  constats ; `rebuild-disabled` seulement si dégradé ; `rebuilding` avec la
  progression lue par adresse `ip:port` ou `tcp://ip:port` ;
  `replica-failed` avec `rebuild-now` seulement s'il reste une réplique `RW` ;
  `not-enough-nodes` avec cible = nœuds planifiables, jamais proposée si
  aucun nœud ; ignorée en anti-affinité souple (globale ou du volume) ;
  volume détaché à risque (`watch`) ; `no-room` quand les nœuds existent
  mais qu'aucun disque n'a la place ; `node-unavailable` pour un nœud ou un
  disque non prêt ; `unexplained` en dernier recours ; un volume sain n'a
  aucun constat ; réglages à valeur par moteur (`{"v1":"3"}`) lus.
- [ ] **Étape 2 : lancer, constater l'échec** (`ModuleNotFoundError`).
- [ ] **Étape 3 : écrire le module** (code livré dans le dépôt, commenté).
- [ ] **Étape 4 : tests verts**, puis sabotage de chaque garde-fou pour
  vérifier que son test échoue.
- [ ] **Étape 5 : commit.**

### Tâche 2 : le diagnostic dans la carte du stockage

**Fichiers :**
- Modifier : `web/app.py` (`STORAGE_KINDS`, `_build_storage_map`)
- Tests : `tests/api/test_storage_map.py`

**Interfaces :**
- Consomme : `volume_health.diagnose_all`, `volume_health.node_state`.
- Produit : chaque entrée de `volumes` porte `"health"` et `"findings"` ; la
  carte porte `"health_summary": {"degraded": int, "faulted": int, "at_risk": int, "top_cause": str|None}`.

- [ ] **Étape 1 : tests.** `engines.longhorn.io` fait partie du même appel
  groupé ; un volume dégradé de la carte porte ses constats ; le résumé
  compte dégradés, faulted et à risque, et donne la cause la plus fréquente
  parmi les gravités `critical` et `action` ; un volume sans PVC est aussi
  diagnostiqué.
- [ ] **Étape 2 : échec constaté.**
- [ ] **Étape 3 : implémentation** : ajouter `"engines.longhorn.io"` à
  `STORAGE_KINDS` ; dans `_build_storage_map`, appeler
  `volume_health.diagnose_all(by_kind["Volume"], by_kind["Replica"], by_kind["Engine"], lh_nodes, settings, room["disks"])`
  et joindre le résultat par nom Longhorn.
- [ ] **Étape 4 : tests verts, commit.**

### Tâche 3 : le point d'accès de correction

**Fichiers :**
- Modifier : `web/app.py` (après `api_storage_map`)
- Tests : `tests/api/test_volume_fix.py`

**Interfaces :**
- `POST /api/volume-health/<cluster>/<volume>/fix`, corps `{"kind": "set-replicas"|"enable-rebuild"|"rebuild-now"}`.
- `_volume_fix_plan(entry: dict, kind: str, cluster: str) -> (plan|None, raison|None)`, `plan = {"kind", "args": [...kubectl...], "summary": str, "params": dict}`.
- `_power_action_running(cluster) -> bool`.
- `_volume_fix_runner(run, kc, volume, plan)` : étapes `verify`, `apply`, `observe`.

- [ ] **Étape 1 : tests.** Genre inconnu : 400 ; volume inconnu : 404 ;
  correction qui ne s'applique plus : 409 avec la raison ; `faulted` : 409 ;
  `set-replicas` : cible calculée par le serveur (un `replicas` envoyé par
  la page est ignoré), bornée entre 1 et la valeur actuelle ;
  `rebuild-now` : 409 sans réplique saine ; `enable-rebuild` : 409 pendant un
  arrêt ou un démarrage en cours ; cas nominal : 201 et action tracée avec
  la commande kubectl attendue ; le cache de la carte est ignoré (relecture).
- [ ] **Étape 2 : échec constaté.**
- [ ] **Étape 3 : implémentation.** L'observation relit le volume toutes les
  5 s pendant 60 s : sain, ou reconstruction démarrée, conclut en succès ;
  sinon étape `observe` en `warn` et action en erreur « appliqué, aucun
  effet observé ».
- [ ] **Étape 4 : tests verts, sabotage des garde-fous, commit.**

### Tâche 4 : l'écran

**Fichiers :**
- Modifier : `web/static/js/storage-map.js`, `web/static/css/style.css`, `web/static/js/i18n.js`
- Tests : `tests/e2e/test_volume_health_view.py`

- [ ] **Étape 1 : tests navigateur** (réseau intercepté) : bandeau présent
  seulement s'il y a un problème, avec les comptes et la cause dominante ;
  clic sur le bandeau : détail du premier volume concerné ; pastille et
  étiquette sur la ligne ; encart Santé avec faits, marche à suivre, bouton,
  commande équivalente copiable ; confirmation refusée : aucun appel ;
  acceptée : POST du seul `kind` ; refus du serveur affiché ; progression
  de reconstruction ; aucune correction proposée sur `faulted` ; bulles
  d'aide partout ; rendu en français sans erreur.
- [ ] **Étape 2 : échec constaté.**
- [ ] **Étape 3 : implémentation**, textes dans les cinq langues.
- [ ] **Étape 4 : tests verts, sabotage, commit.**

### Tâche 5 : en réel sur harv1, doc, version

- [ ] `not-enough-nodes` : PVC jetable en `harvester-longhorn` (3 répliques)
  monté par un pod ; la vue le montre dégradé ; correction ; retour à sain ;
  suppression des objets.
- [ ] `rebuilding` : PVC jetable, volume passé à 2 répliques avec
  `replicaSoftAntiAffinity: enabled`, monté par un pod ; suppression d'une
  réplique ; la vue montre la reconstruction et sa progression ; suppression
  des objets.
- [ ] `rebuild-disabled` : réglage à 0 pendant la reconstruction précédente,
  correction depuis la vue, réglage relu à 5.
- [ ] Doc EN et FR (`docs/en/capabilities.md`, `docs/fr/capabilites.md`),
  CHANGELOG `1.42.0`, VERSION, mémoire du projet.
- [ ] Suites complètes, commit, push Gitea.

### Tâche 6 : release

- [ ] Paquet `1.42.0` construit et vérifié (empreinte, contenu, application
  extraite démarrée contre harv1).
- [ ] Notes couvrant 1.41.0 et 1.42.0 présentées pour validation ; après
  accord : push GitHub, tag, release avec le tarball et son `.sha256`.
