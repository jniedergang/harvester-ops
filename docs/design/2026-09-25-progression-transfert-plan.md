# Progression d'un transfert : plan d'implémentation

Mise en œuvre de `docs/design/2026-09-25-progression-transfert.md`.

**Objectif :** pendant un transfert, voir débit, temps restant, quantité
totale et déjà transférée, dans le dock, la fenêtre « Migrer » et le CLI ;
aller aussi vite que possible, avec des options quand cela coûte des
ressources.

**Architecture :** le script calcule (module pur `vm_transfer_progress.py`)
et publie `PROGRESS_EVENT` ; la console garde le dernier point par phase
hors de la file d'événements et le diffuse ; l'interface traduit.

**Technique :** Python stdlib (zlib pour compter les octets bruts d'un flux
gzip), Flask existant, JS vanilla, pytest, Playwright.

## Contraintes globales

- Version livrée : `1.46.0` (VERSION + CHANGELOG dans le commit qui l'apporte).
- Octets et secondes dans les données ; tout texte affiché traduit dans les
  cinq langues, via `tr('clé')` littéral.
- Aucune progression dans `run.events` (plafond de 500) ; au plus une
  émission toutes les 2 s par phase.
- Pas de tiret cadratin ni de flèche Unicode dans le contenu public.
- `python3 -m pytest tests/api/ -q` vert avant chaque commit.

---

### Tâche 1 : le calcul (`bin/lib/vm_transfer_progress.py`)

**Interfaces produites :**
- `Progress(emit, phase, total, now=time.time, items_total=None, window=20.0, every=2.0)`
- `.update(done, wire=None, item=None, items_done=None)` : émet si 2 s se
  sont écoulées depuis la dernière émission.
- `.add(n_raw, n_wire=0)` : incrémente (pour les flux).
- `.finish()` : émet le point final (`final: true`) et rend
  `{"done", "wire", "elapsed", "rate"}` (débit moyen).
- `.snapshot()` : dict émis `{"phase", "item", "done", "total", "wire", "rate", "eta", "elapsed", "items_done", "items_total", "final"}`.
- `emit_line(snap)` : écrit `PROGRESS_EVENT|<phase>|<json>` sur stderr, ou une
  ligne lisible si stderr est un terminal.
- `human(snap) -> str` et `summary(phase, result) -> str` (anglais, pour le
  CLI et le bilan d'étape).
- `GzipCounter()` : `.feed(chunk) -> int` (octets bruts décompressés du
  morceau), `.wire` (octets compressés vus).

- [x] Tests : débit sur fenêtre glissante (horloge factice), `eta` absent
  sans débit puis juste, étranglement à 2 s, `finish` émet toujours, bilan
  moyen ; `GzipCounter` sur un gzip réel découpé en morceaux (brut = taille
  d'origine, transmis = taille du gzip) ; ligne `PROGRESS_EVENT` bien formée.

### Tâche 2 : le moteur publie ses phases (`bin/lib/vm_transfer_run.py`)

**Interfaces :** `Ctx(..., progress=vm_transfer_progress.emit_line)` ; chaque
phase de la spec crée un `Progress` et appelle `finish()` ; bilan dans un
`emit(sid, "done", summary)`.
- `freeze` : dans `export_images`, somme `progress% × taille` des images.
- `download` : `write_archive` compte chaque flux par `GzipCounter`.
- `import` : les ouvreurs servis au guichet sont enveloppés ; un nouveau GET
  sur un disque repart de zéro pour ce disque (la sonde de CDI ne compte pas).
- `backup`, `restore` : `progress%` × somme des tailles des volumes.
- `images` : pendant `wait_synced`, progression des images en restauration.

- [x] Tests (clusters simulés) : chaque moteur publie ses phases avec un
  `total` égal à la somme des disques et un point final ; le bilan est dans
  les étapes ; la sonde de CDI ne double pas le compte.

### Tâche 3 : la console relaie (`web/app.py`)

- `ActionRun.progress` (phase -> dernier point), `progress_ver`,
  `emit_progress(snap)` ; `to_dict()` rend `progress`.
- `emit()` numérote (`_seq`) ; le flux SSE indexe par numéro absolu et saute
  ce qui est sorti de la file ; il envoie `event: progress` quand
  `progress_ver` change.
- `_vm_transfer_runner` lit `PROGRESS_EVENT|`.
- Contrôle : le rapport rend `amount` = `{disks, size, used}`.

- [x] Tests : un seul point gardé par phase ; `to_dict` ; le flux continue
  au-delà de 500 événements ; le relais du runner ; `amount` dans le rapport.

### Tâche 4 : l'interface

- `vm-transfer.js` : ligne « À transférer » dans le rapport ; bloc de suivi
  après « Lancer » (SSE via `SSEReconnect.connect`).
- `dock.js` : ligne de progression et barre de la phase en cours.
- `i18n.js` : phases, « reste », « transmis », unités, cinq langues.

- [x] Tests navigateur : quantité annoncée, bloc de suivi alimenté par un flux
  simulé (français), ligne du dock.

### Tâche 4 bis : la vitesse

- `vm_transfer_run.import_disks` : tous les `DataVolume` créés d'emblée
  (ou par lots de `req["parallel"]`), attente commune ; `write_archive`
  garde l'ordre (une archive s'écrit en série) mais télécharge à l'avance.
- `vm_transfer_serve.DiskServer(..., rate=None)` : seau à jetons partagé.
- `vm_transfer_run.boost_longhorn(ctx)` / `restore_longhorn(ctx)` : relève
  puis rétablit `backup-concurrent-limit` (source) et
  `restore-concurrent-limit` (cible), inscrit pour le retour arrière.
- `TIMEOUTS["sync_nudge"] = 20`.
- Script et console : `--speed`, `--bandwidth`, `--parallel` ; profil dans
  l'assistant avec bulle d'aide sur le coût.

- [x] Tests : deux disques demandés en même temps (guichet simulé) ; plafond
  respecté ; concurrence relevée puis rétablie, y compris sur échec ; options
  validées par la console ; profil dans l'assistant.

### Tâche 5 : en réel, doc, version

- [x] Banc : node2 rallumé, un transfert par la console et un par sauvegarde,
  chiffres cohérents ; node2 éteint.
- [x] Doc EN/FR (capacités), CHANGELOG 1.46.0, VERSION, tests verts, push.
