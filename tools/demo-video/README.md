# Vidéos de présentation

Chaîne de fabrication des courtes vidéos de démonstration : le navigateur
joue une scène dans la **vraie console** contre un **vrai cluster**, puis
le montage accélère les attentes, incruste les sous-titres et pose les
cartons et la musique.

Rien de tout ceci ne part chez le client : `package.sh` ne copie que
`bin web container config docs`.

## Fabriquer une vidéo

```bash
cd tools/demo-video
./seed.py up                                  # VMs de démonstration sur harvlab
./record.py cluster_maintenance --lang en     # tournage (console de dev, port 8095)
./build.py out/cluster-maintenance-en         # montage -> MP4 + aperçu WebP
./seed.py down                                # ménage
```

Le tournage exige un cluster joignable : `harvlab` tourne sur node2, qui
est **éteint par défaut** (voir `tests/bench/harvlab/README.md`).

## Comment c'est découpé

| Fichier | Rôle |
|---|---|
| `record.py` | pilote le navigateur, filme, et collecte les repères de la scène |
| `scenes/*.py` | une scène = un enchaînement de gestes, sans aucune mise en page |
| `captions/<scène>.<langue>.yaml` | les textes, une clé par repère `cam.say(...)` |
| `build.py` | ffmpeg : accélérations, sous-titres, cartons, musique, MP4 + WebP |
| `lib/timeline.py` | le calcul du montage (testé dans `tests/api/test_demo_video.py`) |
| `seed.py` | VMs de démonstration **par l'API de la console** (`up`, `down`, `wait`), `replicas` (2 répliques), `degrade` (supprime une réplique, pour filmer une reconstruction) |
| `screenshots.py` | refait les captures du README (`docs/assets/`), 3200x1800 réduites à 256 couleurs |
| `cards/card.html` | carton de début et de fin, aux couleurs du produit |

## Les scènes

| Scène | Durée | Ce qu'elle fait pour de vrai |
|---|---|---|
| `tour` | 2 min 23 | descend tout le menu, change la langue de l'interface |
| `shutdown_startup` | 2 min 16 | éteint les trois nœuds, les rallume (`harvlab.sh start` à la place d'un BMC), attend le démarrage complet |
| `cluster_maintenance` | 1 min 43 | met un nœud en maintenance, l'en sort, migre une VM |
| `vms_console` | 1 min 17 | formulaire de création, console, connexion à la VM |
| `storage` | 54 s | un volume dégradé (`seed.py degrade` juste avant) et sa reconstruction |
| `network` | 1 min 12 | réseaux, fabrique, chemin réseau d'une VM |
| `snapshots` | 1 min 50 | instantané, modification, restauration, preuve par `ls` |
| `activity` | 1 min 12 | historique, journal d'une action, paquet de support |
| `multi_cluster` | 1 min 16 | changement de cluster, cluster éteint (harv3), comptes du cluster |

L'arrêt et le redémarrage prennent 13 minutes réelles par langue, la
maintenance 5 : ce sont eux qu'on tourne en dernier.

## Écrire une scène

Une scène ne décide ni des coupes ni des textes : elle joue, et elle pose
des repères.

```python
NAME = "cluster-maintenance"     # nom des fichiers de sous-titres et de sortie
CLUSTER = "harvlab"

def scene(cam):
    cam.click('button[data-overview-tab="cluster"]')
    cam.say("intro")             # clé cherchée dans captions/<NAME>.<langue>.yaml
    cam.preview()                # début de l'extrait animé du README
    with cam.fast(8):            # attente réelle, montrée accélérée (repère « ×8 »)
        ...
```

Deux règles, vérifiées par les tests :

- toute clé `cam.say("…")` existe **dans les deux langues** ;
- une scène est rejouable : elle ne suppose ni un nom de nœud, ni un
  placement de VM (elle interroge l'API pour choisir sa cible).

## Pièges payés ici

- **Ce ffmpeg n'a pas `drawtext`** (sans freetype) : les textes passent par
  libass (sous-titres) ou par une capture de page HTML (cartons).
- **Le film commence un peu après la scène** : l'écart entre la durée réelle
  et la durée du fichier recale les repères (`build.py`, `offset`).
- **La musique est décorrélée du tournage** : elle est mixée au montage, donc
  la changer ne demande pas de refilmer.
- **Les sous-titres ne sont pas incrustés au tournage** : traduire ou
  reformuler ne coûte qu'un `build.py`.
- **`page.wait_for_function` n'attend pas une fonction asynchrone** : elle
  reçoit la promesse, la juge vraie et rend la main. Attendre avec
  `cam.until`, qui évalue pour de bon (un test le garde).
- **Agrandir la page dans Chromium ne sert à rien** (le zoom de la racine
  redimensionne aussi la fenêtre de rendu) : on filme en 1536x864 et le
  montage remonte en 1080p.
- **Pas de `>` ni de `|` dans la console d'une VM** : la console envoie des
  codes de touches physiques (c'est ce qui fait marcher l'AZERTY) et
  Playwright tape ces caractères sans appuyer sur Majuscule. `>` arrive
  `.` et la commande ne fait pas ce qu'on croit. Les scènes s'en passent
  (`touch`, `ls`).
- **Cliquer dans l'écran de la console avant de taper** : `cam.click()`
  retire le focus après chaque clic, pour fermer les infobulles.
- **YAML 1.1 : une clé `off`, `on`, `yes` ou `no` devient un booléen** et
  la scène ne retrouve plus son sous-titre (le test le signale).
- **L'arrêt rend la main quand l'ordre est parti, pas quand la machine est
  éteinte** : la scène attend que libvirt voie les trois machines arrêtées
  avant de les rallumer, sinon deux restent éteintes.
- **Une restauration d'instantané laisse la VM arrêtée** (comme Harvester)
  et l'ancien disque détaché : `seed.py` ne le supprime pas, faire le
  ménage des PVC `restore-*` orphelins après une série de prises.
