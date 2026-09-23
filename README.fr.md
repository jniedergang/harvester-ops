# harvester-ops

**Une console moderne pour exploiter un ensemble de clusters SUSE
Harvester : tous les clusters dans une seule interface, les opérations
courantes automatisées, chaque événement conservé.**

[English](README.md) · Français

[![Licence : Apache 2.0](https://img.shields.io/badge/Licence-Apache_2.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/github/v/release/jniedergang/harvester-ops)](https://github.com/jniedergang/harvester-ops/releases/latest)
[![Tests](https://img.shields.io/badge/tests-1230%2B_au_vert-green.svg)](tests/)

> Projet libre et indépendant. Sans lien avec SUSE, ni approuvé ni pris en
> charge par SUSE.

[![Le tour de harvester-ops](docs/assets/video/tour-fr.webp)](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-fr.mp4)

*Le tour de la console, en descendant le menu de gauche. Cliquer sur
l'image pour la vidéo complète.*

## Pourquoi

L'interface de Harvester travaille un cluster à la fois. En exploiter
plusieurs, c'est passer d'une interface à l'autre, puis à `kubectl` et aux
scripts, sans que rien ne garde trace de qui a changé quoi, sur quel
cluster, et quand.

harvester-ops les réunit dans une seule console pensée pour l'exploitation
au quotidien : tous les clusters vus et pilotés depuis le même endroit, les
opérations courantes transformées en actions guidées et reproductibles, et
chaque changement enregistré, qu'il ait été fait depuis la console ou
ailleurs.

## À quoi il sert

### Tous les clusters dans une interface moderne

- **Tous vos clusters, une seule console.** On en déclare autant qu'il en
  faut et on passe de l'un à l'autre en un clic : toutes les vues suivent.
  Un cluster éteint ou injoignable le dit en deux secondes, et les autres
  continuent de répondre.
- **Des vues à raison d'un bloc par objet** : les hôtes et les VMs qu'ils
  font tourner, les réseaux et ce qui y est raccordé, les classes de
  stockage, les volumes et la place réellement disponible sur chaque
  disque, les cartes réseau, agrégats et commutateurs virtuels.
- **Une console qui reste vivante** : les vues se rafraîchissent seules,
  les opérations longues affichent leurs étapes au fil de l'eau, consoles
  et éditeurs s'ouvrent en fenêtres rangées dans une barre, et la console
  VNC d'une VM peut être suivie par plusieurs personnes à la fois.
- **Cinq langues** (anglais, français, allemand, espagnol, italien), thèmes
  clair et sombre, et une bulle d'aide sur chaque contrôle.
- **Rôles et identités** : lecteur, opérateur et administrateur, tout est
  refusé par défaut, et les actions sont portées sur le cluster sous
  l'identité propre de chaque opérateur.

### L'automatisation simplifiée

- **Machines virtuelles** : création guidée qui vérifie la place disponible
  au fil de la saisie, modèles, plusieurs machines d'un coup, assistant
  cloud-init, démarrage, arrêt et stratégie de démarrage en masse,
  instantanés et restauration, migration à chaud.
- **Maintenance des nœuds, guidée** : un pré-contrôle dit quelles VMs
  migreront, lesquelles s'arrêteraient et ce qui bloquerait la vidange,
  puis la vidange est suivie jusqu'au bout.
- **Tout un cluster éteint et redémarré dans l'ordre** : huit étapes à
  l'arrêt, cinq au démarrage, chacune vérifiée avant la suivante, et seules
  les VMs qui tournaient reviennent.
- **Infrastructure en code et provisionnement** : déclarations Terraform
  sauvegardées et fournisseur Harvester installé depuis la console ; la
  pile Cluster API et ses clusters RKE2 en aval ; Harvester installé sur
  une machine vierge par média virtuel Redfish.
- **Scriptable** : le séquençage électrique s'exécute aussi en ligne de
  commande, pour les chaînes d'automatisation et les sites hors ligne, et
  chaque opération a sa simulation.

### Chaque événement conservé

- **Tout changement est une action** : elle reçoit un identifiant dès son
  lancement, ses étapes s'affichent en direct dans le dock en bas de chaque
  page, et son journal complet est conservé. L'historique se fouille par
  cluster, état, type ou n'importe quel mot.
- **Les changements faits ailleurs apparaissent aussi.** Espaces de noms,
  images et leurs téléversements, réseaux, volumes et VMs sont surveillés
  sur chaque cluster ; ce qui a été changé depuis l'interface Harvester,
  `kubectl` ou Rancher est enregistré de la même façon, y compris pendant
  que la console elle-même était arrêtée.
- **Rien n'est anonyme** : chaque ligne de journal nomme son cluster, et
  une action en échec garde l'erreur d'origine plutôt qu'un simple code de
  sortie.
- **Un contexte partagé** : notes collaboratives sur les clusters, les
  nœuds et les VMs, métriques Prometheus détaillées par cluster, et paquet
  de support anonymisé en un clic.

## À voir

De courtes vidéos tournées sur un vrai cluster de trois nœuds, sous-titrées.
Les attentes sont accélérées, avec le facteur affiché à l'écran ; rien
n'est coupé.

| Vidéo | Ce qu'on y voit | Regarder |
|---|---|---|
| **Le tour** (2:23) | Chaque entrée du menu, de haut en bas, et les cinq langues de l'interface | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/tour-en.mp4) |
| **Plusieurs clusters** (1:16) | Changer de cluster, dont un éteint, les déclarations et les comptes du cluster | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/multi-cluster-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/multi-cluster-en.mp4) |
| **Activité** (1:12) | Toutes les actions conservées avec leur journal, et un paquet de support | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/activity-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/activity-en.mp4) |
| **Machines virtuelles** (1:17) | La liste des VMs, la création guidée, et la console intégrée | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/vms-console-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/vms-console-en.mp4) |
| **Instantanés** (1:50) | Une VM photographiée, modifiée, puis restaurée | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/snapshots-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/snapshots-en.mp4) |
| **Maintenance d'un nœud** (1:43) | Le pré-contrôle, un hôte vidé pendant que ses VMs migrent à chaud, puis une VM déplacée à la main | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/cluster-maintenance-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/cluster-maintenance-en.mp4) |
| **Stockage** (0:54) | La place réellement disponible, et un volume dégradé expliqué pendant sa reconstruction | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/storage-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/storage-en.mp4) |
| **Réseau** (1:12) | Les réseaux, la fabrique physique, et le chemin d'une VM | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/network-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/network-en.mp4) |
| **Arrêt et redémarrage** (2:16) | Tout un cluster éteint dans l'ordre, puis remonté | [Français](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/shutdown-startup-fr.mp4) · [English](https://github.com/jniedergang/harvester-ops/releases/download/v1.44.10/shutdown-startup-en.mp4) |

## Captures d'écran

| | |
|---|---|
| [![Vue Cluster](docs/assets/cluster.png)](docs/assets/cluster.png) | [![Activité](docs/assets/activity.png)](docs/assets/activity.png) |
| Vue Cluster, avec un pré-contrôle de maintenance | Activité : chaque action et son journal |
| [![Machines virtuelles](docs/assets/vms.png)](docs/assets/vms.png) | [![Console VNC](docs/assets/console.png)](docs/assets/console.png) |
| Les machines virtuelles d'un espace de noms | La console VNC intégrée |
| [![Stockage](docs/assets/storage.png)](docs/assets/storage.png) | [![Fabrique réseau](docs/assets/fabric.png)](docs/assets/fabric.png) |
| Le stockage, avec un volume en reconstruction | La fabrique physique du cluster |
| [![Arrêt gracieux](docs/assets/shutdown.png)](docs/assets/shutdown.png) | [![Bare-metal](docs/assets/baremetal.png)](docs/assets/baremetal.png) |
| Les huit étapes de l'arrêt | Bare-metal : découverte Redfish et installation sans intervention |

Les captures montrent l'interface en anglais ; elle existe dans les cinq
langues.

## Démarrage rapide

```bash
# Sur le poste d'exploitation
tar xzf harvester-ops-<version>.tar.gz
cd harvester-ops-<version>
sudo ./install.sh                      # installeur interactif
xdg-open https://localhost:8090        # la console
```

Déclarer ses clusters depuis la console (Réglages, Clusters : un kubeconfig
et, pour les opérations électriques, une clé SSH), ou dans
`/etc/harvester-ops/config.yaml`.

Le séquençage électrique s'exécute aussi en ligne de commande, ce
qu'utilisent les scripts et les sites hors ligne :

```bash
harvester-status   --cluster prod
harvester-shutdown --cluster prod --dry-run
harvester-startup  --cluster prod
```

Voir [l'installation](docs/fr/installation.md) et la
[procédure opérationnelle](docs/fr/procedure-operationnelle.md).

## Comment c'est construit

- **Un moteur, deux entrées.** Les opérations qui doivent fonctionner sans
  la console (séquençage électrique, état) vivent dans des scripts bash
  lisibles (`bin/`), qui n'ont besoin que de `kubectl` et de `ssh`. La
  console (Flask, JavaScript simple, sans framework) lance ces mêmes
  scripts et ne les contourne jamais.
- **Hors ligne par construction** : un seul tarball avec sa somme SHA-256,
  les paquets Python et l'image du conteneur à l'intérieur ; rien n'est
  téléchargé à l'exécution.
- **Installé comme un service systemd** qui fait tourner un conteneur en
  lecture seule, construit sur SUSE BCI, sous un compte sans privilège.

## Testé sur de vrais clusters

Les tests unitaires et navigateur tournent à chaque modification (plus de
1 000 tests côté serveur et 200 dans le navigateur). Chaque fonctionnalité
est aussi exercée sur un vrai cluster avant d'être livrée : un cluster de
production à un nœud, et un cluster d'essai à trois nœuds pour ce qui en
demande plusieurs (maintenance, migration, arrêt et redémarrage complets).
Les vidéos ci-dessus ont été tournées sur ce cluster d'essai, et leur
tournage a trouvé et corrigé des défauts qu'aucun cluster à un nœud ne
pouvait montrer ; le [journal des versions](CHANGELOG.md) raconte chacun
d'eux.

## Documentation

[Capacités](docs/fr/capabilites.md) ·
[Procédure opérationnelle](docs/fr/procedure-operationnelle.md) ·
[Architecture](docs/fr/architecture.md) ·
[Installation](docs/fr/installation.md) ·
[Bare-metal](docs/fr/bare-metal.md) ·
[Dépannage](docs/fr/depannage.md) ·
[Journal des versions](CHANGELOG.md) (en anglais)

<details>
<summary>Contenu du tarball</summary>

```
harvester-ops/
├── README.md, README.fr.md, VERSION, CHANGELOG.md, LICENSE
├── install.sh, uninstall.sh        installeur interactif
├── web/                            la console
│   ├── app.py
│   ├── templates/, static/         interface en JavaScript simple
│   └── vendor/                     paquets Python, pour les sites hors ligne
├── bin/
│   ├── harvester-shutdown.sh       moteur d'arrêt (8 étapes)
│   ├── harvester-startup.sh        moteur de démarrage (5 étapes)
│   ├── harvester-status.sh         état du cluster (texte ou JSON)
│   └── lib/common.sh               journaux, simulation, étapes en direct
├── container/Containerfile         FROM registry.suse.com/bci/python:3.11
├── images/harvester-ops-ui.tar     image OCI (podman load)
├── config/                         configuration d'exemple, unité systemd
└── docs/en/, docs/fr/
```

</details>

## Contribuer

Voir [CONTRIBUTING.md](CONTRIBUTING.md) pour l'organisation du dépôt, la
mise en place et le circuit de publication. Chaque modification arrive
avec un test, une nouvelle `VERSION` et une entrée dans `CHANGELOG.md`.

## Licence

Licence Apache 2.0, voir [LICENSE](LICENSE).
