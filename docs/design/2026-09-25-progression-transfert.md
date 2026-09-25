# Progression et vitesse d'un transfert de VM

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

## Vitesse et ressources (demande de l'exploitant, 25/09/2026)

« Tout faire pour optimiser la vitesse, et donner des options si cela
impacte les ressources. » Mesurer d'abord : la progression par phase donne
le débit de chaque maillon. Leviers relevés :

- **Le gzip de Longhorn est mono-cœur par flux** : Harvester relaie le
  téléchargement de Longhorn (`backingimages/<nom>/download`), toujours
  compressé, sans réglage (code lu le 25/09/2026). Mesuré sur harv1 :
  93 Mo/s de disque brut pour un flux. D'où :
  - **disques en parallèle** (moteur fichier) : chaque disque est
    téléchargé et importé en même temps que les autres, un cœur de
    compression par disque. Option `parallel` : tous (défaut) ou N ;
    1 ménage la source.
- **Plafond de débit** (moteur fichier) : `bandwidth` en Mo/s, appliqué par
  le guichet au total du transfert (seau à jetons). Défaut : aucun.
- **Concurrence de Longhorn** (moteur sauvegarde) : `backup-concurrent-limit`
  sur la source et `restore-concurrent-limit` sur la cible valent 2
  (relevé sur harv1) ; relevés à 8 le temps du transfert, puis rétablis à
  leur valeur d'origine, y compris sur échec ou annulation. Coût : CPU et
  réseau de tous les nœuds des deux clusters, et toute autre sauvegarde ou
  restauration en cours pendant ce temps. Défaut : non.
- **Temps morts** : relance de synchronisation toutes les 20 s au lieu de 60.
- **À évaluer sur le banc** : l'export KubeVirt (`VirtualMachineExport`, CRD
  présente) en disque brut, sans le gzip mono-cœur. Retenu seulement si la
  mesure le justifie : le brut transporte aussi les zéros d'un disque creux.

**Interface et CLI** : un choix « Vitesse », dont la bulle d'aide dit le coût :

| Profil | Disques en parallèle | Concurrence Longhorn | Pour quoi |
|---|---|---|---|
| Économe | 1 | inchangée | cluster de production chargé |
| Normale (défaut) | tous | inchangée | le cas courant |
| Maximale | tous | relevée à 8 | fenêtre de maintenance |

plus un plafond de débit facultatif (Mo/s). CLI : `--speed eco|normal|max`,
`--bandwidth <Mo/s>`, `--parallel N`.

## Tests

- Calcul : débit sur fenêtre, temps restant, étranglement à 2 s, bilan.
- Comptage brut et transmis d'un flux gzip ; la sonde de CDI ignorée.
- Moteur : chaque phase publie une progression (clusters simulés).
- Console : un seul point gardé par phase, `to_dict` le rend ; le flux
  continue au-delà de 500 événements.
- Navigateur : ligne du dock et bloc de suivi en français, quantité annoncée
  au contrôle.
- Vitesse : parallélisme (deux disques servis en même temps), plafond
  respecté (débit mesuré sous le plafond), concurrence Longhorn relevée puis
  rétablie, même sur échec.
- Réel : transferts sur le banc (node2 rallumé puis éteint), chiffres
  cohérents avec la taille des disques ; mesures comparées (un disque puis
  deux en parallèle, concurrence 2 puis 8, export brut KubeVirt), consignées
  ici.
