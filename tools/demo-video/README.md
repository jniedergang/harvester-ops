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
| `seed.py` | crée et efface les VMs de démonstration, **par l'API de la console** |
| `cards/card.html` | carton de début et de fin, aux couleurs du produit |

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
