# Dimensionnement et performances

Ce que coûte la console, mesuré, et ce qu'elle fait pour ne pas ralentir
quand beaucoup de personnes l'utilisent en même temps.

## Comment la console lit les clusters

- **Lectures partagées (1.81.0).** Les écrans qui se rafraîchissent seuls
  (aperçu, liste des VMs, topologie, jauges d'usage, réseaux, stockage,
  périphériques, mise à jour, add-ons, migrations, activité...) sont lus une
  fois pour tout le monde : pendant qu'une lecture d'un écran donné tourne,
  les autres demandes du même écran attendent son résultat au lieu de lancer
  leur propre `kubectl`, et ce résultat sert encore 3 secondes (1 seconde pour
  le dock d'activité). Deux personnes ne partagent une lecture que si elles
  présentent la même identité au cluster (même kubeconfig, donc même RBAC) et
  ont le même rôle dans la console.
- **Une écriture n'est jamais masquée.** Toute requête d'écriture (POST, PUT,
  PATCH, DELETE) oublie les lectures partagées du cluster qu'elle nomme, comme
  la fin de toute action. Une lecture en cours au moment d'une écriture n'est
  pas réutilisée. `?fresh=1` sur un écran relit toujours.
- **Les fichiers de configuration sont analysés une fois.** `config.yaml` et
  les déclarations de clusters restent analysés tant que le fichier ne change
  pas (date, taille ou inode), avec libyaml quand elle est installée. Ils
  étaient réanalysés plusieurs fois par requête, ce qui faisait l'essentiel
  du CPU de la console.
- **Les surveillances de clusters sont étalées.** Chacune démarre à son propre
  décalage dans le premier intervalle, et chaque tour varie de 10 % au plus :
  dix clusters ne sont plus relus dans la même seconde toutes les 15 secondes.

Réglages (environnement du service) :

| Variable | Défaut | Effet |
|---|---|---|
| `HARVESTER_OPS_READ_SHARE` | `1` | `0` coupe les lectures partagées |
| `HARVESTER_OPS_READ_SHARE_TTL` | `3` | secondes pendant lesquelles une lecture sert |
| `HARVESTER_OPS_WATCH_INTERVAL` | `15` | secondes entre deux tours de surveillance |
| `HARVESTER_OPS_WATCH_IDLE_AFTER` / `_IDLE_INTERVAL` | `300` / `120` | surveillance ralentie quand personne n'utilise la console |

## Mesuré (1er octobre 2026)

Un processus de console, un cluster (Harvester 1.9, 14 VMs), des personnes
simulées ayant chacune l'aperçu ouvert : dock d'activité chaque seconde, liste
des VMs, aperçu, topologie et usage toutes les 5 secondes. Latence médiane
(p50) et au 95e centile (p95) :

| Personnes | Avant 1.81.0 : aperçu p50 / p95 | 1.81.0 : aperçu p50 / p95 | CPU de la console avant / après |
|---|---|---|---|
| 1 | 0,63 s / 0,65 s | 0,59 s / 0,60 s | 9 % / 5 % |
| 10 | 1,0 s / 1,8 s | 0,65 s / 0,92 s | 28 % / 5 % |
| 30 | 2,5 s / 4,1 s | 0,72 s / 0,96 s | 47 % / 9 % |
| 60 | 5,5 s / 6,3 s | 0,54 s / 0,87 s | 62 % / 16 % |

À 60 personnes, la liste des VMs est passée de 4,8 s à 0,17 s et les jauges
d'usage de 7 s à 0,28 s. La mémoire est restée sous 140 Mo.

## Ressources recommandées

| Échelle | Hôte de la console |
|---|---|
| Jusqu'à 10 clusters, quelques centaines de VMs, jusqu'à 60 personnes | 2 vCPU, 2 Gio |
| 10 à 30 clusters | 4 vCPU, 4 Gio |

Le nombre de personnes ne fait plus le coût ; le nombre de clusters et
d'objets par cluster, si (chaque surveillance relit son cluster toutes les 15
secondes quand quelqu'un utilise la console). Au-delà de 30 clusters ou de
plusieurs milliers de VMs, relever `HARVESTER_OPS_WATCH_INTERVAL` et mesurer :
ces échelles n'ont pas encore été essayées en charge.
