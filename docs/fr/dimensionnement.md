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
- **Lectures sans kubectl (1.83.0).** Mesuré sur 30 clusters simulés de 200
  VMs : 94 % du CPU de la console partait dans les `kubectl get -o json`
  qu'elle lance (chaque appel décode puis réencode toute la liste). La
  console et ses scripts font maintenant ces lectures directement contre l'API
  Kubernetes, avec le même résultat que kubectl (une `List` dont les objets
  portent leur `kind`, l'objet seul pour un nom, et le texte d'erreur de
  kubectl quand le cluster refuse), connexions gardées ouvertes. Le reste
  passe toujours par kubectl : écritures, autres formats de sortie,
  kubeconfigs qui s'authentifient par un plugin `exec` ou un proxy.
- **Processus lecteurs (1.83.0).** Une fois les lectures sorties de kubectl,
  les écrans les plus lourds (topologie, liste des VMs, carte du stockage,
  fabrique réseau) sont construits dans quelques processus lecteurs séparés,
  chacun sur son cœur, au lieu d'attendre derrière l'unique processus Python.
  Le processus principal continue de décider qui vous êtes, votre rôle et
  l'identité présentée au cluster ; un lecteur ne fait que calculer l'écran et
  le rendre, avec les refus éventuels du cluster. Un lecteur repart à neuf
  toutes les 200 vues pour borner sa mémoire, et s'arrête avec la console.
- **Les surveillances de clusters sont étalées.** Chacune démarre à son propre
  décalage dans le premier intervalle, et chaque tour varie de 10 % au plus :
  dix clusters ne sont plus relus dans la même seconde toutes les 15 secondes.

Réglages (environnement du service). Pour le service packagé, les écrire en
lignes `VAR=valeur` dans `/etc/harvester-ops/env`, puis
`systemctl restart harvester-ops` :

| Variable | Défaut | Effet |
|---|---|---|
| `HARVESTER_OPS_READ_SHARE` | `1` | `0` coupe les lectures partagées |
| `HARVESTER_OPS_READ_SHARE_TTL` | `3` | secondes pendant lesquelles une lecture sert |
| `HARVESTER_OPS_KUBE_REST` | `1` | `0` renvoie toutes les lectures à kubectl |
| `HARVESTER_OPS_READ_WORKERS` | 4 (moins sous 5 cœurs, 0 sous 3) | processus lecteurs ; `0` construit tous les écrans dans le processus principal |
| `HARVESTER_OPS_READ_WORKER_TASKS` | `200` | écrans construits par un lecteur avant qu'il reparte à neuf |
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

## Mesuré avec beaucoup de clusters (1.83.0)

Clusters simulés (`tests/bench/kwok`, vrais serveurs d'API sans machine
derrière), 200 VMs chacun, une personne par cluster : rien ne se partage entre
personnes. Médiane / 95e centile de la topologie et de la liste des VMs, et CPU
de la console avec tous les processus qu'elle lance :

| Clusters, personnes | Avant 1.83.0 | 1.83.0 |
|---|---|---|
| 30 clusters, surveillances seules | 1,9 cœur | 0,15 cœur |
| 30 clusters, 30 personnes | topologie 5,4 / 8,1 s, VMs 4,4 / 7,2 s, 10,3 cœurs | topologie 1,3 / 3,7 s, VMs 0,8 / 2,7 s, 3,4 cœurs |
| 60 clusters, surveillances seules | 3,5 cœurs | 0,3 cœur |
| 60 clusters, 30 personnes | topologie 7,0 / 9,8 s, VMs 7,3 / 11,4 s, 9,6 cœurs | topologie 1,8 / 2,9 s, VMs 1,6 / 3,2 s, 3,7 cœurs |
| 60 clusters, 60 personnes | topologie 16,5 / 21,1 s, VMs 11,9 / 20,2 s, 9,7 cœurs | topologie 6,3 / 8,9 s, VMs 5,6 / 7,6 s, 3,7 cœurs |

Le processus de la console reste sous 450 Mo ; chaque processus lecteur prend
environ 175 Mo. L'essai à 60 personnes était limité par la machine du banc :
les 60 serveurs d'API simulés tournaient sur la même machine que la console
(charge au-dessus de 40 sur 28 cœurs), ce qu'un déploiement réel ne fait pas,
et passer de 4 à 8 lecteurs n'a rien changé. Ces chiffres sont un majorant,
pas la limite de la console.

## Ressources recommandées

| Échelle | Hôte de la console |
|---|---|
| Jusqu'à 10 clusters, quelques centaines de VMs, jusqu'à 60 personnes | 2 vCPU, 2 Gio (lecteurs coupés sous 3 cœurs) |
| Jusqu'à 30 clusters, quelques milliers de VMs, 30 personnes | 4 vCPU, 4 Gio |
| Jusqu'à 60 clusters, environ 12 000 VMs, 30 personnes | 6 vCPU, 6 Gio |

Chaque processus lecteur ajoute environ 175 Mo. Au-delà de 60 clusters, ou avec
beaucoup de personnes chacune sur un cluster différent, mesurer avec le banc
avant de s'engager sur un chiffre ; relever `HARVESTER_OPS_WATCH_INTERVAL`
baisse la charge constante des surveillances.
