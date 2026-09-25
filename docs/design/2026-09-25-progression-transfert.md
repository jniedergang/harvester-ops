# Progression d'un transfert de VM : débit, temps restant, quantités

Conception validée le 25/09/2026. Livraison prévue : v1.46.0. Complète
`2026-09-24-migration-vm.md`.

## Besoin (exploitant, 25/09/2026)

Pendant un transfert, voir le débit, une estimation du temps restant, la
quantité de données à transférer et celle déjà transférée.

## Constat qui s'y ajoute

Une action garde ses 500 derniers événements (`deque(maxlen=500)`) et le flux
SSE les parcourt par position. Passé 500, les plus anciens sortent, la file
ne grandit plus, et le flux n'envoie plus rien : le dock se fige. Un
transfert de plusieurs heures qui publierait sa progression l'atteindrait.
Le flux numérote désormais les événements (numéro absolu), et saute ce qui
est sorti de la file.

## Principe : un canal de progression à part

Le script calcule, la console relaie, l'interface traduit.

- **`bin/lib/vm_transfer_progress.py`** (pur, horloge injectée) :
  `Progress(emit, now, phase, total, items)` et `update(done, wire=None,
  item=None)`. Débit sur une fenêtre glissante de 20 s, temps restant =
  reste / débit (absent tant que le débit est inconnu), émission au plus
  toutes les 2 s et à `finish()`. `finish()` rend aussi un bilan (quantité,
  octets transmis, durée, débit moyen).
- **Ligne émise** : `PROGRESS_EVENT|<phase>|<json>` sur stderr, avec
  `{"phase", "item", "done", "total", "wire", "rate", "eta", "elapsed",
  "items_done", "items_total", "final"}` (octets et secondes). En terminal
  interactif, une ligne lisible à la place.
- **Phases** et source des chiffres :

  | Phase | Moteur | Mesure | Total |
  |---|---|---|---|
  | `freeze` | fichier | progression de l'image exportée (%) | taille des disques |
  | `download` | fichier (archive) | octets comptés au passage, bruts (décompressés à la volée) et transmis (gzip) | taille des disques |
  | `import` | fichier (direct, import) | idem, par le guichet ; la première connexion de CDI (sonde) ne compte pas | taille des disques |
  | `backup` | sauvegarde | progression de la `VirtualMachineBackup` (%) | taille des volumes |
  | `images` | sauvegarde | progression des images ramenées sur la cible (%) | leur taille virtuelle |
  | `restore` | sauvegarde | progression de la `VirtualMachineRestore` (%) | taille des volumes |

  Une sauvegarde Longhorn est incrémentale : la quantité affichée est celle
  des volumes, ce qui transite réellement peut être bien moindre. Le libellé
  le dit.
- **Fin de phase** : un événement d'étape garde le bilan dans l'Activité
  (« 10.0 GiB (512 MiB sent) in 2 min 5 s, 85 MiB/s average »).

## Console

- `_vm_transfer_runner` relaie `PROGRESS_EVENT` : le dernier point de chaque
  phase est gardé dans `run.progress` (jamais dans la file d'événements),
  diffusé en SSE (`event: progress`) et rendu par `to_dict()` (donc par
  `/api/activity`).
- Le flux SSE envoie le dernier point quand il change (numéro de version).

## Interface

- **Dock** : sous l'étape, « Import disk-0 : 3,2 / 10 Gio · 85 Mio/s · reste
  ≈ 1 min 30 » ; la barre suit la phase en cours.
- **Fenêtre « Migrer »** : après « Lancer », un bloc de suivi en direct
  (`SSEReconnect.connect`) : phase, barre, fait / total, transmis, débit,
  temps restant, écoulé ; il se fige sur le bilan à la fin.
- **Contrôle préalable** : « À transférer : 2 disques, 20 Gio (1,2 Gio
  occupés) » (tailles et occupation réelle Longhorn de l'inventaire).
- Textes et unités dans les cinq langues.

## Tests

- Calcul : débit sur fenêtre, temps restant, étranglement à 2 s, bilan.
- Comptage brut et transmis d'un flux gzip ; la sonde de CDI ignorée.
- Moteur : chaque phase publie une progression (clusters simulés).
- Console : un seul point gardé par phase, `to_dict` le rend ; le flux
  continue au-delà de 500 événements.
- Navigateur : ligne du dock et bloc de suivi en français, quantité annoncée
  au contrôle.
- Réel : un transfert sur le banc (node2 rallumé puis éteint), chiffres
  cohérents avec la taille des disques.
