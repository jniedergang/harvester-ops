# Volumes dégradés : diagnostiquer, guider, corriger

Conception validée le 21/09/2026. Livraison prévue : v1.42.0.

## Contexte

Un volume Longhorn « dégradé » a moins de répliques saines que demandé. Il
reste lisible, mais une panne de plus peut le rendre inaccessible, et rien
dans la console ne le signalait : la santé n'apparaissait que comme un champ
brut dans le détail de la vue Stockage.

Les causes sont connues et peu nombreuses, et plusieurs se corrigent d'un
geste sûr. L'objectif est que l'exploitant sache, sans lire la documentation
de Longhorn, **pourquoi** un volume est dégradé et **quoi faire**, et puisse le
faire depuis la console quand c'est sans risque.

Relevé sur harv1 au moment de la conception : aucun volume dégradé (4
attachés sains, 20 détachés). Mais la classe `harvester-longhorn` demande 3
répliques et `replica-soft-anti-affinity` vaut `false` : sur un cluster à un
nœud, tout volume de cette classe démarre dégradé. Le cas se reproduit donc à
volonté.

## Périmètre

- **Dans** : la vue Stockage (bandeau, lignes de volume, détail du volume).
- **Hors** (décision de l'exploitant) : alerte globale hors de la vue, entrée
  d'Activité à chaque changement d'état, contrôle des volumes dégradés par
  l'arrêt et le démarrage gracieux du cluster.

## Données

Tout vient de l'appel kubectl groupé de la carte du stockage
(`STORAGE_KINDS`, v1.40.0), auquel s'ajoute un seul type :
`engines.longhorn.io`, pour l'état des répliques vu par le moteur et la
progression des reconstructions. Aucun appel de plus.

Champs lus (vérifiés sur harv1, Longhorn de Harvester 1.8) :

| Objet | Champs |
|---|---|
| `volumes.longhorn.io` | `status.robustness` (`healthy`, `degraded`, `faulted`, `unknown`), `status.state`, `spec.numberOfReplicas`, `spec.replicaSoftAntiAffinity`, `status.conditions[type=Scheduled]` (statut, raison, message), `spec.size` |
| `replicas.longhorn.io` | `spec.volumeName`, `spec.nodeID`, `spec.diskID`, `spec.failedAt`, `spec.lastHealthyAt`, `status.currentState` |
| `engines.longhorn.io` | `spec.volumeName`, `status.replicaModeMap` (`RW`, `WO` = en reconstruction, `ERR`), `status.rebuildStatus` (progression par réplique) |
| `nodes.longhorn.io` | `spec.allowScheduling`, `status.conditions` (`Ready`, `Schedulable`), `status.diskStatus[*]` (conditions, `diskUUID`, capacités) |
| `settings.longhorn.io` | `concurrent-replica-rebuild-per-node-limit`, `replica-soft-anti-affinity`, `replica-replenishment-wait-interval`, sur-provisionnement et minimum libre (déjà lus) |

## Le diagnostic

Une fonction **pure** (`web/volume_health.py`), sans Flask ni cluster, reçoit
ces objets et rend, pour chaque volume qui n'est pas sain, une liste de
constats :

```
{"cause": <code>, "severity": "critical" | "action" | "watch" | "info",
 "facts": {...}, "fix": null | {"kind": <code>, "params": {...}}}
```

`action` : à corriger ; `watch` : à surveiller ; `info` : normal, rien à faire.
Le texte affiché est construit côté navigateur à partir du code et des faits
(traduction dans les cinq langues). Le serveur ne rend que des données.

### Causes, dans l'ordre d'examen

Plusieurs peuvent se cumuler sur un même volume.

| Code | Reconnaissance | Gravité | Correction |
|---|---|---|---|
| `faulted` | `robustness = faulted` | critical | aucune ; consigne : ne rien supprimer, rétablir le nœud ou le disque qui portait les données |
| `rebuild-disabled` | volume dégradé et `concurrent-replica-rebuild-per-node-limit = 0` | action | `enable-rebuild` : remettre 5 (valeur du démarrage gracieux) |
| `rebuilding` | une réplique en mode `WO` dans le moteur | info | aucune ; progression affichée |
| `replica-failed` | une réplique avec `spec.failedAt` renseigné | watch | `rebuild-now` : supprimer la réplique en échec pour que Longhorn en reconstruise une tout de suite, au lieu d'attendre jusqu'à `replica-replenishment-wait-interval` |
| `not-enough-nodes` | répliques demandées > nœuds planifiables distincts, anti-affinité stricte (réglage global ou du volume) | action | `set-replicas` : ramener au nombre de nœuds planifiables (au moins 1) |
| `no-room` | assez de nœuds, mais aucun disque avec la place du volume (`room` de `_storage_room`) | action | aucune ; conseil : libérer de la place (volumes orphelins de la même vue), ajouter un disque, revoir le sur-provisionnement |
| `node-unavailable` | une réplique sur un nœud ou un disque qui n'est plus `Ready` | action | aucune ; conseil : rétablir le nœud |
| `unexplained` | dégradé sans aucune cause ci-dessus | watch | aucune ; la condition `Scheduled` brute est montrée |

**Volume détaché à risque** : un volume détaché dont les répliques demandées
dépassent les nœuds planifiables démarrera dégradé. Il reçoit le constat
`not-enough-nodes` avec la gravité `watch` et la même correction.

Un nœud est **planifiable** s'il est `Ready`, `Schedulable`, `allowScheduling`
et porte au moins un disque `Ready` et `Schedulable` avec `allowScheduling`.

## L'écran

- **Bandeau** en tête de la vue, seulement s'il y a quelque chose :
  « 2 volumes dégradés, 1 à risque », et la cause la plus fréquente. Un clic
  ouvre le détail du premier volume concerné.
- **Lignes de volume** : pastille orange (dégradé, à risque) ou rouge
  (faulted), et une étiquette.
- **Détail du volume** : un encart « Santé » en tête. Pour chaque constat :
  titre, faits (« 3 répliques demandées, 1 nœud planifiable »), marche à
  suivre, bouton de correction s'il y en a un, et la commande `kubectl`
  équivalente, repliée et copiable. Pendant une reconstruction, une barre de
  progression par réplique.
- Chaque bouton porte une bulle d'aide ; tout texte existe dans les cinq
  langues.

## Les corrections

`POST /api/volume-health/<cluster>/<volume>/fix` avec `{"kind": ...}`.

1. **Le serveur ne croit pas la page.** Il relit l'état (même appel groupé,
   sans cache), refait le diagnostic, et n'applique la correction que si le
   constat correspondant existe toujours. Les paramètres (nombre de
   répliques, réplique à supprimer) sont calculés par lui, jamais repris de
   la requête. Sinon : 409 avec la raison (« le volume est redevenu sain »,
   « la cause a changé »).
2. **Garde-fous durs**, vérifiés côté serveur :
   - aucune correction sur un volume `faulted` ;
   - `set-replicas` : cible entre 1 et la valeur actuelle, jamais au-dessus ;
   - `rebuild-now` : seulement s'il reste au moins une réplique saine (`RW`)
     et que la réplique visée est bien en échec ;
   - `enable-rebuild` : seulement si le réglage vaut 0 et qu'aucune action
     d'arrêt ou de démarrage du cluster n'est en cours dans la console.
3. **Rôle opérateur** (porte centrale) et limite de débit.
4. **Action tracée** : créée au déclenchement, étapes `verify`, `apply`,
   `observe`. L'observation relit le volume pendant 60 s au plus et conclut
   selon ce qu'elle voit : cause disparue, reconstruction démarrée, ou rien
   de changé (l'action le dit au lieu d'annoncer un succès).
5. Chaque bouton demande **confirmation** en nommant la conséquence (réduire
   les répliques diminue la redondance).

Commandes équivalentes affichées :

- `set-replicas` : `kubectl -n longhorn-system patch volumes.longhorn.io <v> --type merge -p '{"spec":{"numberOfReplicas":N}}'`
- `enable-rebuild` : `kubectl -n longhorn-system patch settings.longhorn.io concurrent-replica-rebuild-per-node-limit --type merge -p '{"value":"5"}'`
- `rebuild-now` : `kubectl -n longhorn-system delete replicas.longhorn.io <r>`

## Tests

- **Unitaires, diagnostic** : une batterie par cause, sur des structures
  Longhorn relevées sur harv1 ; cumul de causes ; volume détaché à risque ;
  nœud non planifiable pour chacune des raisons. Chaque test est vérifié par
  sabotage.
- **API, corrections** : refus quand la cause a disparu ou changé, refus sur
  `faulted`, bornes de `set-replicas`, réplique saine requise pour
  `rebuild-now`, `enable-rebuild` refusé pendant un arrêt, paramètres jamais
  repris de la requête, action tracée.
- **Navigateur** : bandeau, pastilles, encart Santé, confirmation, commande
  équivalente copiable, bulles d'aide.
- **En réel sur harv1**, avec des volumes jetables supprimés ensuite :
  `not-enough-nodes` (volume de 3 répliques attaché, correction, retour à
  sain) ; `rebuilding` (volume de 2 répliques à anti-affinité souple, une
  réplique supprimée) ; `rebuild-disabled` (réglage mis à 0 un court instant,
  puis remis).
- **Non vérifiables sur harv1** (un seul nœud, pas de panne à provoquer sans
  risque) : `node-unavailable`, `replica-failed`, `faulted`. Couverts par les
  tests automatisés et documentés comme non vérifiés en réel.
