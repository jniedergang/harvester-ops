# Procédure opérationnelle — extinction / démarrage gracieux d'un cluster Harvester

> Public cible : opérateurs cluster disposant des droits `cluster-admin` et d'un accès physique (iLO/iDRAC/IPMI) aux nœuds.

## 1. Principe de fonctionnement

Un cluster Harvester combine :

- **RKE2 / K3s** — le control-plane Kubernetes (etcd, kube-apiserver, etc.)
- **KubeVirt** — les machines virtuelles sous forme de ressources `VirtualMachine` (VM) / `VirtualMachineInstance` (VMI)
- **Longhorn** — stockage bloc distribué avec réplicas synchrones

Une extinction gracieuse doit respecter trois invariants :

1. **Aucune perte de données** : chaque volume Longhorn doit être `detached` avant l'extinction physique de son nœud hôte, pour que tous les réplicas convergent.
2. **Aucune corruption d'etcd** : au moins un nœud control-plane doit conserver un état etcd récent au redémarrage ; le quorum ne doit pas se perdre en cours d'extinction.
3. **Aucune incohérence d'état des VMs** : les guests doivent recevoir un ACPI shutdown (gracieux) avant que le stockage ne soit détaché.

L'outillage garantit ces invariants en séquençant les opérations et en vérifiant l'état entre chaque étape.

## 2. Séquence d'extinction (8 étapes)

| # | Étape | Action | Pourquoi |
|---|---|---|---|
| 0 | Pré-vérifications | API joignable, nodes Ready, Longhorn présent | Détecter un cluster cassé avant d'aggraver |
| 1 | Snapshot etcd | `etcdctl snapshot save` sur un CP | Assurance disaster-recovery |
| 2 | Arrêt VMs | Patch chaque `VirtualMachine` avec `spec.runStrategy: Halted` | Déclenche un ACPI shutdown du guest |
| 3 | Attente VMIs disparues | Polling jusqu'à plus de `VirtualMachineInstance` | Confirme que les guests sont OFF |
| 4 | Mode maintenance Longhorn | `concurrent-replica-rebuild-per-node-limit=0`, attente `volumes.status.state == detached` | Évite des rebuilds inutiles |
| 5 | Cordon des nodes | `kubectl cordon` sur chaque node | Empêche le re-scheduling pendant l'extinction |
| 6 | Extinction des workers | `ssh <worker> 'sudo shutdown -h +0'` | Les workers n'ont pas de quorum |
| 7 | Extinction du control-plane | Idem, ordre inverse des hostnames, délai `--node-shutdown-delay` entre chaque | Le dernier CP éteint garde l'etcd le plus récent |

> **Vérifier que les machines sont bien éteintes.** Jusqu'à la 1.44.10, la
> séquence pouvait se déclarer réussie en n'ayant touché que le premier
> control-plane. Après une extinction, confirmer par les cartes
> d'administration, ou par un ping, que chaque nœud est éteint avant de
> couper l'alimentation.

## 3. Séquence de démarrage (5 étapes)

| # | Étape | Action |
|---|---|---|
| 1 | Démarrage du premier CP | L'opérateur allume **le dernier CP éteint** via iLO/iDRAC/IPMI. Le script attend `/readyz` sur l'API |
| 2 | Démarrage des autres nodes | Autres CPs puis workers, un par un |
| 3 | Attente Nodes Ready | Polling jusqu'à ce que tous les nodes reportent `Ready` |
| 4 | Restauration | Réactivation du rebuild Longhorn, `uncordon` de tous les nodes |
| 5 | Redémarrage des VMs | Tous les VMs en `runStrategy: Halted` repassent à `Always` |

## 4. Exécution

> **Une séquence à la fois.** Un cluster n'exécute qu'un arrêt ou un
> démarrage à la fois. La console refuse le second et dit quelle action
> tourne déjà ; les scripts font de même par un verrou posé à côté de leurs
> journaux, si bien que la ligne de commande et la console se voient. Les
> simulations ne sont jamais bloquées.

### Recommandé : mode interactif

Pour une première exécution sur un cluster, **toujours** utiliser `--interactive`. Le script fait une pause entre chaque étape pour permettre à l'opérateur de vérifier et d'interrompre si nécessaire.

```bash
harvester-shutdown --cluster prod --interactive
```

### Dry-run

Affiche ce qui serait fait sans toucher au cluster :

```bash
harvester-shutdown --cluster prod --dry-run --yes
```

### Mode batch (ex. UPS)

Pour une extinction non-interactive (batterie UPS faible, maintenance planifiée) :

```bash
harvester-shutdown --cluster prod --yes \
                   --skip-etcd-snapshot \
                   --vm-timeout 180
```

⚠️ **Ne jamais exécuter sans `--interactive`, `--yes`, ou `--dry-run`.** Le script refuse toute opération sans flag non-interactive explicite, par sécurité.

## 5. Observabilité

Chaque exécution produit :

- Un **log structuré** dans `/var/log/harvester-ops/<timestamp>-<cluster>-<action>.log`
- Des **événements stream** au format `STEP_EVENT|<step-id>|<status>|<msg>` (consommés par l'UI web pour le suivi temps réel)
- Des **codes de retour** : `0` succès, `1` annulé par l'opérateur ou erreur fatale, `2` erreur de configuration

L'UI web re-streame les mêmes événements via Server-Sent Events (SSE) pour un suivi en temps réel.

## 6. Multi-cluster

Le fichier de config `/etc/harvester-ops/config.yaml` déclare tous les clusters connus.
Chaque commande exige `--cluster <nom>`. Depuis la 2.0.0, l'UI web liste tous les hôtes (clusters Harvester et hyperviseurs KVM) dans son menu latéral ; voir la section 13.

Pour lister les clusters configurés :

```bash
yq '.clusters[].name' /etc/harvester-ops/config.yaml
```

## 7. Opérations par namespace

Quand on ne veut pas éteindre tout le cluster, mais seulement arrêter les VMs d'un seul tenant/namespace :

```bash
harvester-status   --cluster prod --namespace tenant-a
# (pas d'extinction par namespace en v1.0 — utiliser l'onglet Namespace de l'UI web)
```

Dans l'UI web, **Machines virtuelles** d'un hôte Harvester (`#harvester/<c>/vms`, l'ancien onglet Namespaces) liste chaque namespace avec ses VMs et permet de les démarrer/arrêter en groupe.

## 8. Suivi temps réel (dock Activity)

Toute action déclenchée depuis l'UI apparaît dans le **dock du bas** — toujours visible quel que soit l'onglet (toggleable depuis l'onglet Activity).

- Les actions en cours montrent un chronomètre live (`⏱ 12s elapsed`), une mini-barre de progression et l'étape courante.
- Clic sur `▸` d'une carte → déploie un terminal live avec les events (steps + logs script).
- Les actions terminées restent visibles **30 secondes** avec un badge ✓ done (ou ✗ error), leur durée totale et l'heure exacte de fin, puis basculent dans l'historique Activity.

Les opérations par VM (Start/Stop, changement de runStrategy, bulk depuis Namespaces) sont elles aussi trackées : tu vois le `kubectl patch` puis l'attente de la phase VMI en direct.

## 9. Archive de support anonymisée & dé-anonymisation

Quand tu construis une archive avec la case **Anonymize sensitive data** :

- Les identifiants sensibles sont remplacés par des placeholders sans ambiguïté : `<<NODE-1>>`, `<<IP-NODE-1>>`, `<<CLUSTER-1>>`, `<<VM-1>>`, `<<NS-1>>`. Impossible de les confondre avec une vraie valeur → dé-anonymisation 100% fidèle plus tard.
- La table de correspondance complète est incluse dans l'archive (`mapping.json`) ET téléchargeable séparément juste après la génération (bouton **🗝 Télécharger la table de correspondance**).
- Conserve précieusement le fichier mapping : c'est la **seule** clé pour ré-associer les placeholders aux noms réels.

Pour restaurer les noms d'origine dans un fichier log qui t'a été retourné par un tiers :

1. Paramètres → Support → **Dé-anonymiser un fichier de logs**
2. Upload le fichier modifié + le `mapping.json` d'origine
3. Clique sur *Dé-anonymiser et télécharger* → le fichier restauré se télécharge. L'en-tête HTTP `X-Replaced-Entries` indique le nombre de placeholders substitués.

## 10. Gestion multi-cluster depuis l'UI

Pour ajouter un cluster sans éditer `config.yaml` à la main :

- Réglages > onglet **Clusters** > *Add cluster*, OU **Ajouter un hôte** sous Hôtes dans le menu latéral, puis **Cluster Harvester**.
- Renseigner : un nom unique, une description, l'utilisateur SSH (défaut `rancher`), la liste des nœuds (hostname + IP + role : `control-plane` ou `worker`), upload kubeconfig + clé SSH privée.
- Le cluster est persisté dans le répertoire d'état de la console (`/var/lib/harvester-ops`, `HARVESTER_OPS_STATE_DIR`), jamais dans `config.yaml` : `clusters.d/<nom>.yaml` (mode 0600, écriture atomique), avec des chemins relatifs à ce répertoire pour que sa copie déplace la console (voir « Déplacer harvops vers un autre hôte » dans le guide d'installation).
- Le kubeconfig est stocké dans `<état>/kubeconfigs/<nom>.yaml` (mode 0600) ; la clé SSH dans `<état>/ssh/<nom>_id` (mode 0600).
- Chaque cluster dit qui l'a déclaré : `config.yaml` (l'opérateur) ou `console`. Quand la console ne peut pas écrire `config.yaml` (le service packagé monte `/etc/harvester-ops` en lecture seule), les clusters de `config.yaml` ne se modifient ni ne se suppriment depuis l'UI (l'API répond 409) : modifier le fichier. Quand elle le peut (environnement de développement), leurs changements y sont toujours écrits, avec un `.bak` de la version précédente.
- Depuis l'onglet Clusters tu peux aussi : remplacer le kubeconfig, remplacer la clé SSH, lancer **Test kubeconfig** (kubectl version) ou **Test SSH** (ping par nœud), éditer les métadonnées, ou supprimer le cluster (et ses fichiers associés).

## 11. Conception sécurisée

- **Double consentement** : les opérations destructives exigent à la fois un flag non-interactive (`--yes`) ET une confirmation finale, sauf si `--yes` est explicitement passé.
- **Idempotence** : ré-exécuter le script après un échec partiel reprend là où il en était (ex. VMs déjà arrêtées → on saute l'attente de l'étape 2).
- **Pas d'échec silencieux** : chaque étape sort en non-zéro sur erreur, sauf si l'opérateur choisit explicitement de l'ignorer.
- **Auditable** : chaque commande est tracée avec timestamp et code de retour.

## 12. Reprise après sinistre (réplication asynchrone)

Ce qu'elle fait, les objets qu'elle crée et ses limites :
[capabilites.md, section 11](capabilites.md#11-reprise-après-sinistre--réplication-asynchrone-des-vms-cli--api-de-la-console).
Chaque commande ci-dessous est `harvester-replication` (ajouter `--json`
pour les scripts).

**Avant tout.** Les deux clusters utilisent la même cible de sauvegarde
(réglage Harvester `backup-target`), placée sur aucun des deux sites. Faire
tourner la console qui suit les plans sur le site de secours ou hors des
deux sites, jamais seulement sur le site source : elle serait perdue avec
lui.

**Depuis la console.** Les mêmes étapes existent dans Disaster Recovery >
Contrats : **Nouveau contrat** (blocs 1 à 5, mesurer d'abord ou protéger
directement), puis la fenêtre du contrat (Vérifier, Mesurer, Protéger, les
onglets Graphiques et Journal) et **Bascule...** (administrateur, nom du
contrat tapé). Voir [capabilites.md, Écrans Disaster Recovery](capabilites.md#écrans-disaster-recovery).

**Première protection.**
1. `plan create --name web --from harv1 --to harv2 --vm default/web-01
   --rpo 15m --map-net default/prod=default/prod --map-sc
   harvester-longhorn=harvester-longhorn`. Mettre en correspondance chaque
   réseau et chaque classe de stockage des VMs. Un disque d'image sans
   `--map-image` prend la classe sur laquelle son image a été bâtie, à
   travers `--map-sc`.
2. `check web` jusqu'à ce qu'il ne montre aucun constat `block`.
3. `measure start web --interval 5m`, attendre trois passages, `measure
   stop web`, puis `measure report web` pour l'intervalle recommandé.
4. `protect web --dry-run`, puis `protect web` (il prend l'intervalle
   mesuré, sauf `--interval` ; `--retain N` fixe le nombre de sauvegardes
   gardées).
5. `status web` (sortie 4 : un objectif est dépassé) et `events --plan
   web`. Dans la console, les actions sont dans le dock et l'Activité.
6. `check web --phase failover` joue la bascule à blanc, sans rien écrire.

**Pendant la protection.** Le suiveur de la console applique chaque
sauvegarde sur la cible. `plan set web --paused true` le met en pause pour
le plan, `plan set web --rpo 30m` change l'objectif (les deux à chaud). Un
changement de processeur, de mémoire, de disque ou de réseau d'une VM
protégée apparaît comme une dérive : relancer `protect web` pour réécrire
son carnet. Pour changer la rétention : `protect web --retain 10`.

**Bascule planifiée** (les deux sites répondent, sans perte) : `failover
web --vm default/web-01` (ou `--all-vms --yes`). La VM source est arrêtée,
ses dernières écritures sont sauvegardées et appliquées, puis la VM
démarre sur la cible.

**Bascule non planifiée** (source perdue) : d'abord `check web --phase
failover-unplanned` pour voir les points, l'écart et les disques encore en
rattrapage, puis `failover web --all-vms --unplanned --yes`. Une VM dont
les disques sont trop écartés est refusée sauf avec `--accept-skew`. Lire
`lost_after` dans le résultat : les écritures qui suivent sont perdues.
Quand la source revient, s'assurer que ses VMs restent arrêtées.

**Interruption.** Une bascule coupée après l'activation (sortie 3
`interrupted`) se termine avec `resume web`, avec la cible seule si besoin.
Un redémarrage de la console, de l'hôte ou une mise à jour n'annule rien :
une protection (restauration initiale comprise), une mesure ou une
déprotection en cours reste `interrupted` avec l'étape atteinte, et
`resume web` la continue sans recopier ce qui est restauré (l'Activité affiche
« interrupted by the console shutdown »). Une bascule pas encore activée
est défaite à ce moment-là. Seule l'annulation d'une personne (bouton du
dock, Ctrl-C) défait une protection ; la relancer ensuite. Une commande
rendue `not-started` n'a jamais commencé : la relancer.

**Déprotection.** `unprotect web --yes`. Les VMs basculées gardent tout sur
la cible (listé dans `left`). Après une bascule terminée, les sauvegardes
se purgent à la main : voir « Purger les sauvegardes après une bascule
terminée » dans la page des capacités.

**Le magasin de secrets d'abord (2.9.0).** Quand la console garde ses
secrets dans OpenBao, la fiche de reprise commence par OpenBao. En B1,
restaurez avant toute chose le dernier instantané de
`/var/lib/harvester-ops-snapshots` avec sa clé de descellement (la copie
hors ligne) : Paramètres > Magasin de secrets > Instantanés, ou `sudo
/usr/local/bin/harvester-secrets restore <file>`. En mode A, votre serveur
et ses sauvegardes passent en premier. Sans OpenBao, la console ne peut pas
lire les kubeconfigs et les clés qu'elle y a migrés. Voir
[secrets.md](secrets.md).

## 13. Navigation par hôtes (2.0)

Un **hôte** est un cluster Harvester entier ou un hyperviseur KVM, jamais un
nœud Harvester. Le menu latéral liste tous les hôtes que la personne voit, en
deux familles, Harvester et KVM, sous **Hôtes**. Pas de plafond : une flotte
de 60 clusters et 40 hôtes KVM est listée en entier, dans une partie du menu
qui défile seule.

- **Tête de famille.** Chaque famille se replie (le choix est gardé par le
  navigateur) et dit son état, par exemple `58 ok, 2 en alerte`. Un hôte pas
  encore lu compte aussi (`0 ok, 0 en alerte, 1 sans état`) : les nombres
  font toujours le total de la famille. L'info-bulle du résumé explique les
  trois états avec les couleurs des pastilles. Une famille repliée ne montre
  aucune ligne d'hôte, ni hôte en alerte ni hôte ouvert : le résumé, en
  couleur d'alerte, suffit.
- **Ligne d'un hôte.** Une pastille donne l'état (verte joignable, rouge
  injoignable ou en erreur, orange accès refusé, grise pas encore lu), puis
  le nom et le nombre de VMs quand un écran les a déjà lues. La liste ne lit
  jamais un hôte elle-même.
- **Favoris.** L'étoile d'un hôte le garde en tête de sa famille. Les étoiles
  sont gardées par la console, par personne : on les retrouve sur un autre
  poste. Tout rôle peut poser une étoile.
- **Hôte ouvert.** L'hôte à l'écran se déplie sous son nom (un seul à la
  fois) : Vue d'ensemble, Machines virtuelles, Conteneurs, Imports et
  migrations, Paramètres, Disaster Recovery pour un cluster Harvester ; Vue
  d'ensemble, Machines virtuelles, Paramètres, Disaster Recovery pour un hôte
  KVM.
- **Recherche** (raccourci `/`). Elle filtre les hôtes par nom, étiquette
  (`clé`, `valeur` ou `clé=valeur`) et famille, et propose les VMs que les
  écrans ont déjà lues (« Aller à la VM » ouvre les VMs de son hôte, filtrées
  sur elle). Entrée ouvre le premier résultat, Échap vide la recherche.
- **Ajouter un hôte** (administrateur) ouvre une fenêtre à deux choix : un
  cluster Harvester (le formulaire de Réglages > Clusters) ou un hyperviseur
  KVM (la fenêtre de déclaration des écrans KVM).
- Au-dessus des hôtes : **Dossiers**, **Machines virtuelles** et
  **Conteneurs** de toute la flotte ; en dessous : **Disaster Recovery**
  (avec **Liens**), **Migrations**, **Ressources partagées** (GPU,
  Réservations, Usage), **Automatisation** (Cluster API, Terraform,
  Bare-metal) et **Activité**. Ces écrans de flotte n'ont pas d'hôte
  courant : `#containers` montre les charges de tous les clusters,
  `#migrations` toutes les vagues VMware.
- **Activité d'un hôte.** `#activity/host/harvester/<c>` et
  `#activity/host/kvm/<h>` ouvrent l'Activité filtrée sur cet hôte (les
  actions d'un hôte KVM portent l'étiquette `kvm:<h>`) ; le bouton
  **Activité de cet hôte** d'une page d'hôte y mène. Changer le filtre de
  cluster réécrit l'adresse.
- **Cluster de gestion.** Cluster API et Terraform (déclarations comprises)
  affichent **Cluster de gestion** dans leur en-tête : le cluster que
  visent leurs appels, le dernier hôte Harvester visité sauf si vous en
  choisissez un autre dans le menu à côté. Le choisir là ne change pas
  l'hôte ouvert. Bare-metal n'a pas cet en-tête : une jonction choisit son
  cluster elle-même. Bare-metal est en quatre onglets (2.1.0) : **Machines**
  (par défaut), **Profils**, **Images** et **Historique**, à
  `#automation/pxe/<onglet>` ; l'ancienne `#automation/pxe` ouvre Machines.
  Cluster API (2.2.0) : **Clusters** (par défaut), **Services**,
  **Historique** et **Installation**, à `#automation/capi/<onglet>` ;
  `#automation/capi` ouvre Clusters. Terraform (2.2.0) : **Déclarations**
  (par défaut), **Ressources**, **Historique** et **Installation**, à
  `#automation/terraform/<onglet>` ; `#automation/terraform` ouvre
  Déclarations. Chaque Historique est l'Activité filtrée sur les actions de
  son écran, avec un lien vers chaque journal. Clusters prévient quand la
  pile Cluster API n'est pas installée sur le cluster de gestion,
  Déclarations quand le provider Harvester ou OpenTofu manque ; les deux
  mènent à Installation.

Chaque entrée est un vrai lien : le clic milieu l'ouvre dans un nouvel
onglet, et l'adresse se copie. Un lien ouvert avant la connexion est rouvert
une fois connecté.

Le menu de la 1.x (sélecteur de cluster, groupes par cluster) est retiré.
Les anciens liens ouvrent toujours leur écran : `#storage` ouvre Paramètres
> Stockage du dernier hôte Harvester visité, `#allvms` ouvre `#vms`,
`#kvmhosts` l'index des hôtes KVM, `#forkliftglobal` les Migrations ; le
CHANGELOG de la 2.0.0 donne chaque ancien emplacement et sa nouvelle
adresse. Ouvrir la console sans adresse montre `#vms` ; la première fois
après la mise à jour, c'est le dernier écran ouvert qui s'affiche.

### Page d'un hôte (Harvester)

Un hôte Harvester s'ouvre sur une page avec un bandeau en tête, sur une
ligne : la famille, le nom, l'état, la version et le nombre de VMs, le
sous-menu de l'hôte (Vue d'ensemble, Machines virtuelles, Conteneurs,
Imports et migrations, Paramètres, Disaster Recovery) et le bouton icône
**Activité de cet hôte**. Dans une fenêtre étroite, le sous-menu défile de
côté au lieu de passer à la ligne, et un fondu montre le côté où il reste
des entrées. Chaque vue déjà construite garde sa place ; le bandeau ne fait
que l'encadrer.

- **Vue d'ensemble** (`#harvester/<c>`) : métriques, jauges d'usage, tableau
  des nœuds, et le bloc **Événements** en dessous (les sous-onglets Cluster
  et Événements sont partis). `#harvester/<c>/overview/events` fait défiler
  jusqu'aux événements. Le bouton **Upgrade** ouvre Paramètres > Mise à jour.
- **Paramètres** (`#harvester/<c>/params/<onglet>`), en onglets :
  - **Nœuds** : la vue en blocs des nœuds, avec la maintenance et les
    réglages d'hôte, et **Joindre un nœud** (administrateur), qui ouvre
    Automatisation > Bare-metal > Machines (`#automation/pxe/machines`) avec
    ce cluster déjà choisi : la fenêtre d'installation s'ouvre en mode
    jonction vers `https://<VIP>:443`, et l'onglet dit avant d'agir quand le
    magasin n'a aucun ISO de la version du cluster. La VIP
    est lue dans le cluster même (ConfigMap `harvester-system/vip` de
    Harvester) au clic. Par un kubeconfig Rancher, ou quand la VIP ne se lit
    pas, aucune adresse n'est devinée : le bandeau le dit et vous la
    saisissez. L'adresse reste modifiable dans la fenêtre, et sa
    confirmation est à cocher avant Installer, car l'installation efface les
    disques choisis de la machine.
  - **Stockage**, **Réseau**, **Add-ons**, **Monitoring et journaux**,
    **Avancé** : les mêmes sections et sous-onglets qu'avant.
  - **Sécurité** : Secrets, Clés SSH, et **Comptes du cluster** (venus des
    Réglages).
  - **Mise à jour** : le contenu de la fenêtre Upgrade, dans la page.
  - **Alimentation** : Démarrage et Arrêt (avec l'ordre des VMs). Les deux
    sont dans un sélecteur à côté du titre du panneau : la page n'a qu'une
    barre d'onglets.
  - **Connexion** : qui a déclaré le cluster (`config.yaml` ou la console),
    ses nœuds déclarés, **Tester le kubeconfig** et **Tester SSH**
    (administrateur), et un raccourci vers Réglages > Clusters.
- **Machines virtuelles** (`#harvester/<c>/vms`) : la vue par espace de noms
  et ses boutons (Backups, Modèles, Configurations cloud, Namespaces,
  Exports, Créer).
- **Imports et migrations** (`#harvester/<c>/imports/vmimport`,
  `.../imports/forklift`) : Import de VM et migrations VMware.

Un clic sur un autre hôte Harvester du menu garde l'écran : depuis
Paramètres > Stockage > Images d'un cluster, on arrive sur le même onglet de
l'autre (avec le voile de bascule). Un lecteur voit les mêmes pages, sans les
boutons d'écriture.

### Page d'un hôte (KVM)

Un hôte KVM s'ouvre sur le même genre de page (`#kvm/<hôte>`) : Vue
d'ensemble (le bloc de l'hôte), Machines virtuelles (cet hôte seulement),
Paramètres (Système, Pools, Réseaux, Étiquettes, Connexion) et Disaster
Recovery (pas encore disponible pour les domaines KVM). Connexion porte
**Tester** (opérateur) et **Retirer** (administrateur, confirmé dans la
page). Un administrateur peut modifier les étiquettes, après les contrôles
que montre la fenêtre. Voir Capacités, section 10.

### Index des hôtes

**Hôtes** dans le menu latéral (`#hosts`) ouvre un tableau de tous les hôtes
des deux familles : famille, nom, état, version, étiquettes, VMs, dossier et
favori. Un clic sur le titre d'une colonne trie (un second inverse) ; les
filtres portent sur la famille, l'état (ou tous les hôtes en alerte),
l'étiquette, la version ou le dossier. `#hosts/harvester` et `#hosts/kvm`
s'ouvrent avec le filtre de famille posé, et changer ce filtre réécrit
l'adresse. Une ligne ouvre la page de son hôte ; son étoile fonctionne comme
dans le menu. Sur `#hosts/kvm`, **Déclarer un hôte** et, par ligne KVM,
**Tester** et **Retirer** (confirmé dans la ligne) reprennent ce qu'offrait
KVM > Hôtes, et les onglets **Capacité**, **Pools** et **Réseaux** montrent
ceux de tous les hôtes KVM sur un écran (`#hosts/kvm/capacity`, `/pools`,
`/networks`), le nom de chaque hôte menant à sa page. Un lien vers un hôte
que vous ne voyez pas (retiré, ou hors de
vos droits) ouvre cet index avec un message. L'index ne lit que la liste
des hôtes, jamais un hôte.

### Retrouver un écran de la 1.x

Les anciens liens et favoris du navigateur marchent toujours : la console
traduit le jeton 1.x vers sa nouvelle adresse et réécrit la barre
d'adresse. Un jeton propre à un cluster ouvre cet écran sur le dernier hôte
Harvester visité (sinon le premier visible) ; un jeton KVM ouvre la famille
KVM. Les plus courants :

| Vous cherchiez | Désormais |
|---|---|
| Le sélecteur de cluster | **Hôtes** dans le menu latéral, ou l'index des hôtes `#hosts` |
| Cluster > Vue d'ensemble, Événements | la **Vue d'ensemble** de l'hôte (Événements sous les métriques) |
| Cluster > Stockage, Réseau, Modules, Sécurité, Monitoring et journaux, Avancé | les **Paramètres** de l'hôte, mêmes noms d'onglets |
| Cluster > Démarrage, Arrêt | **Paramètres > Alimentation** |
| Réglages > Comptes du cluster | **Paramètres > Sécurité > Comptes du cluster** |
| VMs du cluster courant | les **Machines virtuelles** de l'hôte |
| VMs de tous les clusters, KVM > Machines virtuelles | **Machines virtuelles** (`#vms`), panneau Hôtes pour un hôte ou une famille |
| `#containers` | les conteneurs de toute la flotte ; ceux d'un cluster : **Conteneurs** de l'hôte |
| KVM > Hôtes, Stockage, Réseaux | la famille KVM (`#hosts/kvm`) et ses onglets Capacité, Pools, Réseaux, ou les **Paramètres** de l'hôte |
| Import / export de VM | **Imports et migrations** de l'hôte ; tous les clusters : **Migrations** |

Le CHANGELOG de la 2.0.0 donne le tableau complet, chaque ancien jeton
compris.

### Ranger des VMs, des conteneurs et des hôtes dans des dossiers

Un dossier est partagé par tous ceux qui utilisent la console. Un objet
(VM, charge de conteneurs, hôte) est dans un seul dossier, sans héritage :
ranger un hôte ne range pas ses VMs (la fenêtre propose **Ranger aussi ses
N VMs**, décoché).

1. Ouvrir **Machines virtuelles** (`#vms`), **Conteneurs** ou l'index des
   hôtes. Le panneau Dossiers, à gauche, montre l'arborescence, **Toutes les VMs**
   et **Non rangées**, avec les compteurs de ce que vous voyez.
2. Sélectionner des lignes et les glisser sur un dossier, ou **Déplacer
   vers un dossier** sur une ligne ou une sélection, puis choisir le
   dossier (recherche par nom).
3. Lire les contrôles que la fenêtre montre, objet par objet, avant son
   bouton : **écrit sur l'objet** (annotation d'une VM ou d'une charge
   Harvester, métadonnées libvirt d'un domaine KVM, champ `folder` d'une
   déclaration d'hôte), **console seulement** (vous ne pouvez pas écrire
   l'objet, ou le cluster est déclaré dans `config.yaml` : le rangement est
   gardé par la console, avec une pastille), ou **refusé** avec la raison et
   quoi faire.
4. Confirmer. L'action apparaît au dock, une ligne par objet, et reste dans
   l'Activité.

Créer, renommer et supprimer des dossiers depuis le panneau. Un dossier non
vide ne se supprime pas : la fenêtre propose **Déplacer le contenu de...**
vers un autre dossier, puis le supprime, en une action. Un nom déjà pris
par un frère est refusé avant que rien ne parte.

Depuis la 2.0.2, l'entrée d'un objet qui n'existe plus (hôte KVM retiré, VM
supprimée, cluster retiré) ne bloque plus la suppression. **Supprimer**
vérifie chaque entrée et la retire quand l'objet est prouvé disparu : une
étape et une ligne de journal chacune, dans la même action. Une entrée
qu'on ne peut pas vérifier (hôte injoignable, objet que vous ne voyez pas,
lecture en échec) bloque toujours, et l'action dit pourquoi. Rien n'est
alors retiré.

### Annuler et reprendre un geste sur les dossiers

- **Annuler** : un rangement, renommage, déplacement, vidage ou une
  annulation terminés proposent **Annuler** sur leur carte du dock, puis
  dans l'Activité (aussi après un redémarrage de la console). L'annulation
  remet exactement le dossier que chaque objet avait juste avant l'action.
  Tout opérateur peut annuler le geste d'un autre ; l'annulation nomme les
  deux personnes.
- **Reprendre** : une action finie en erreur (un hôte injoignable en
  chemin, un refus de webhook) ou arrêtée par l'arrêt de la console propose
  **Reprendre**. Elle n'écrit que les objets que l'action n'a pas atteints ;
  un objet fait n'est jamais réécrit, et un objet rangé ailleurs depuis est
  laissé. En ligne de commande : `harvester-folders.py resume --action <id>`
  et `undo --action <id>`.
- Les objets que vous ne voyez pas (un cluster que Rancher vous masque, un
  namespace que vous ne pouvez pas lire) ne sont jamais nommés dans les
  étapes : ils sont comptés.

### Rôles (2.0.0)

| Geste | lecteur | opérateur | admin |
|---|---|---|---|
| Lire chaque écran, étoiler un hôte | oui | oui | oui |
| Créer, renommer, supprimer un dossier vide, vider un dossier, ranger et déplacer des objets | non (grisé, avec la raison) | oui | oui |
| Annuler ou reprendre un geste sur les dossiers, de n'importe qui | non | oui | oui |
| Tester un hôte KVM | non | oui | oui |
| Disaster Recovery : mesurer, protéger, suspendre, reprendre, déprotéger | non | oui | oui |
| Disaster Recovery : bascule, déprotection qui supprime les sauvegardes, reprise de l'une ou l'autre | non | non | oui |
| Ajouter un hôte, retirer un hôte KVM, modifier ses étiquettes, Joindre un nœud, Tester kubeconfig et SSH | non | non | oui |

Un bouton que vous ne pouvez pas utiliser reste visible, grisé, avec le
rôle qu'il faut dans son info-bulle ; le serveur applique la même règle.

## 14. Appliquer un plan de déploiement (2.4.0)

Un plan pose le même contenu Terraform (réseaux, classes de stockage, pools
d'adresses, images, clés, VMs) sur un cluster, après une installation
bare-metal ou sur un cluster déjà déclaré. Référence complète :
[bare-metal.md](bare-metal.md), section « Plans de déploiement ».

1. **Admin, une fois :** dans Automatisation > Bare-metal > Plans, écrire les
   modèles de déclaration (formulaires Terraform, `{{variables}}` là où une
   valeur change selon le site), puis le plan : délai de disponibilité,
   modèles dans l'ordre, dépendance quand l'un a besoin d'un autre. Les
   modèles ne portent aucun secret ; une VM qui en a besoin désigne par son
   nom un Secret Kubernetes existant.
2. **Vérifier avant d'agir :** ouvrir la page d'hôte du cluster, **Appliquer
   un plan**, choisir le plan, remplir les variables, lire
   **Vérifications**. Lancer reste grisé tant qu'un constat bloque (variable
   absente, nom de déclaration pris par autre chose, cluster injoignable,
   objet réservé à l'admin pour un opérateur, construction Terraform
   refusée).
3. **Essai à blanc** d'abord sur un cluster de production : il planifie
   chaque modèle et retire ses déclarations d'essai ; rien n'est écrit sur
   le cluster.
4. **Lancer.** Suivre l'action `deploy:<cluster>` dans le dock ou l'onglet
   Historique : une étape par phase et par modèle, chacune avec un lien vers
   son plan et son apply Terraform.
5. **Quand un modèle échoue**, les modèles indépendants continuent et le
   déroulé finit `partial`. Corriger la cause (souvent la déclaration
   elle-même, dans Terraform > Déclarations, ou le cluster), puis
   **Reprendre** depuis l'onglet Historique : les modèles faits sont
   revérifiés et sautés.
6. **Arrêter** laisse finir un apply Terraform en cours et saute le reste ;
   **Reprendre** repart de là.
7. Un plan ne détruit jamais : un plan qui supprimerait ou remplacerait une
   ressource s'arrête sur ce modèle (`plan-destroys`). Retirer des
   ressources de la déclaration dans Terraform > Déclarations, sciemment.

Avec une installation (admin) : renseigner le champ **Plan** de la fenêtre
d'installation d'une machine ou de la fenêtre de série d'un profil.
L'installation devient la première phase ; ses secrets sont demandés au
lancement et à la reprise, jamais gardés.

## 15. Clusters enfants depuis un plan de déploiement (2.5.0)

Un plan sait aussi déclarer la pile Cluster API et créer des clusters RKE2
enfants, chacun avec sa propre identité CAPHV restreinte, sur Harvester 1.9
ou plus. Référence complète : [bare-metal.md](bare-metal.md), section
« Plans de déploiement : Cluster API et clusters enfants » ; l'identité :
Capacités, section 4.

1. **Admin, une fois :** remplir la fenêtre de création de Cluster API >
   Clusters sur un cluster qui a les images, réseaux, clés et pools, puis
   **Enregistrer comme modèle** ; ou écrire le modèle dans Plans > Modèles
   de cluster, avec **Variable...** pour une référence qui change d'un site
   à l'autre. Donner à chaque cluster enfant une classe de stockage d'image
   que ses nœuds savent placer (une réplique sur un cluster à un nœud).
2. Dans le plan, activer **Cluster API** et **Clusters**, ajouter les
   clusters (ou **Plusieurs** avec un motif de nom qui contient `{{n}}`),
   leur ordre et « dépend de ».
3. **Contrôler avant d'agir :** **Appliquer un plan** sur la page de
   l'hôte. Les contrôles comptent tous les clusters du plan ensemble : CPU,
   mémoire, place Longhorn, adresses des pools, vos droits sur Harvester,
   versions. Lancer reste désactivé tant qu'un constat bloque.
4. **Lancer** et suivre `deploy:<cluster>` : déplier la ligne dans
   l'Activité pour voir `capi-install` et un `capi-cluster-create` par
   cluster.
5. **En cas d'échec**, les clusters indépendants suivants continuent. Un
   cluster resté en échec n'est jamais supprimé par le plan : le supprimer
   depuis Cluster API > Clusters, puis **Reprendre**. Un arrêt laisse la
   création en cours aller à son terme.
6. **Supprimer un cluster enfant** depuis Cluster API > Clusters. Si la
   suppression se termine par « object(s) kept », rétablir le droit qui
   manque et supprimer à nouveau : elle reprend là où elle s'est arrêtée.

## 16. Livrer du contenu aux clusters enfants sans Internet (2.6.0)

Donner aux clusters enfants leurs images et leurs charts depuis un registre
plutôt que depuis Internet. Référence complète : [artefacts.md](artefacts.md) ;
la phase des plans : [bare-metal.md](bare-metal.md), section « Plans de
déploiement : phase Registre ».

1. **Sur une machine connectée :** écrire la description (charts, images,
   manifestes) et lancer `harvester-artefacts pack DESCRIPTION --sign-key
   CLÉ`. Les identifiants des sources vont dans un fichier privé
   (`--creds`), jamais dans un argument. Apporter le `.tar.gz` et sa `.sig`
   à la console.
2. **Admin, une fois :** dans Paramètres > Artefacts, ajouter la clé
   publique de votre organisation aux clés de confiance ; activer le mode
   strict si seuls des paquets signés peuvent entrer.
3. **Admin : importer.** Cluster API > Artefacts > **Importer** : envoyer
   l'archive et la signature, lire l'état de confiance, **Accepter**. Un
   paquet non signé, ou d'une clé inconnue, demande la case d'acquittement
   (refusé en mode strict).
4. **Un registre.** Soit (admin) **Registre d'amorçage** dans Cluster API >
   Registres : une adresse libre hors des pools d'adresses, un volume, une
   classe de stockage, les registres d'origine qu'il miroite ; lire
   **Vérifications**, puis **Lancer**. Soit (admin) **Déclarer un registre
   du client**, puis (opérateur) le **Rattacher** au cluster Harvester avec
   ses comptes d'écriture et de lecture, et **Vérifier**.
5. **Opérateur : publier.** Sur le paquet, **Publier** dans le registre.
   Lire d'abord les vérifications : un tag qui existe avec un autre contenu
   bloque tout (`publish-tag-differs`). Publier à nouveau reprend et saute
   ce qui est là.
6. **Opérateur : créer le cluster enfant** avec les registres, dans le menu
   **Registres** de la fenêtre de création (ou dans un modèle de cluster).
   Ils sont posés à la création seulement. Laisser **Pas de repli vers
   Internet** désactivé, sauf si le registre porte les images système de
   RKE2.
7. **Opérateur : services.** Cluster API > Services, **Depuis un
   registre** : le chart publié dans le registre, tiré par CAAPH avec le
   compte de lecture et la CA.

Un plan fait les étapes 4 et 5 avant ses clusters (phase Registre) ; poser
un registre d'amorçage depuis un plan demande le rôle admin. Rôles : la
lecture est ouverte aux lecteurs ; importer, les clés, le mode strict,
déclarer et poser des registres relèvent de l'admin ; rattacher, vérifier,
publier et créer des clusters à registres relèvent de l'opérateur.

## 17. Appliquer des artefacts aux clusters enfants, émettre des jetons, défaire un déploiement (2.7.0)

Référence complète : [artefacts.md](artefacts.md), « Artefacts dans les
clusters enfants » ; la phase du plan et le Défaire :
[bare-metal.md](bare-metal.md).

1. **Opérateur avec le droit de lire `<c>-kubeconfig` : appliquer.**
   Cluster API > Artefacts, ouvrir le paquet, **Appliquer** sur un élément
   chart, manifeste ou politique : choisir le cluster enfant (et le registre
   pour un chart), lire **Vérifications**, puis **Appliquer**. Suivre
   l'action dans le dock. Un refus `child-admin-required` veut dire que vous
   ne pouvez pas lire le kubeconfig administrateur du cluster enfant.
2. **Voir ce qui est appliqué.** Cluster API > Services : colonne
   **Sorte**, état du binding (« appliqué », « pas encore appliqué »).
   **Retirer** enlève un élément ; les objets modifiés à la main sont gardés
   et nommés.
3. **Socle de politiques et comptes.** Dans un plan (carte Artefacts) ou par
   `harvester-capi manifest-deploy` et une spec `policies`. Le socle est
   décoché par défaut : ne le cocher que pour des namespaces d'équipe que la
   console crée.
4. **Jeton pour une chaîne d'intégration.** Cluster API > Clusters, carte du
   cluster enfant, **Émettre un jeton** sur un compte d'automatisation :
   durée de 600 à 86 400 s ; le copier tout de suite, il n'est jamais
   remontré. Il fonctionne environ une minute après son expiration.
5. **Admin : défaire un déploiement.** Bare-metal > Historique, **Défaire**
   sur l'exécution : lire **Contenu** (registre et déclarations non
   prouvées ou modifiées décochés), lire le **Plan de destruction** (un
   élément dit recréé depuis reste ; une déclaration gardée par un cluster
   qui reste est signalée), taper le nom du cluster dans
   **Confirmation** (et le nom du registre pour supprimer ses données),
   **Défaire**. Un résultat partiel dit ce qui est resté et pourquoi :
   corriger, puis relancer **Défaire** sur la même exécution ; ce qui est
   parti est vérifié et sauté. Pour recréer, relancer le plan.
6. **Admin : désinstaller la pile Cluster API.** Supprimer d'abord chaque
   cluster enfant. Cluster API > Installation, **Désinstaller** : lire ce
   qui part et ce qui reste, taper le nom du cluster. **Installer** la
   repose depuis le paquet. Ne lancez pas de déroulé pendant le retrait :
   le verrou des déroulés est vérifié au départ mais pas tenu pendant le
   retrait.
7. **Bancs d'essai : des adresses à eux.** Chaque banc d'essai a sa propre
   adresse de registre et son propre pool d'adresses, réservés dans la
   gestion des adresses et le DNS avant usage. N'empruntez jamais l'adresse
   d'un autre banc : les deux bancs allumés, deux répartiteurs kube-vip
   répondraient en ARP pour la même adresse.

## 18. Accès aux clusters enfants, GitOps, renouvellement et amorçage sans Internet (2.8.0)

Références complètes : le GitOps dans [gitops.md](gitops.md) ; les sortes
de paquet et les registres dans [artefacts.md](artefacts.md) ; la phase du
plan et le Défaire dans [bare-metal.md](bare-metal.md). Chaque cluster
enfant de Cluster API > Clusters reçoit deux boutons de plus : **Accès** et
**Renouveler**.

### Import dans le Rancher externe de votre session

Le cluster enfant est importé dans le Rancher qui gère votre session, par
l'import générique de Rancher. Le Rancher intégré à Harvester n'est jamais
visé par ce chemin.

1. **Connectez-vous par Rancher.** Un compte local de la console est refusé
   (`rancher-session-needed`, 409), comme pour Projets et Membres.
2. **Fenêtre Accès, onglet Rancher.** Elle montre l'état de l'import, le
   Rancher de votre session et l'exécution de plan qui l'a importé.
   **Importer dans Rancher** (opérateur **et** droit d'administrer le cluster enfant)
   est grisé avec la raison quand il ne peut pas partir. À la création, le
   menu **Accès** propose la même chose sous « Rancher externe de la
   session » ; un modèle de cluster prend `rancher_external: true`.
3. Ce que fait la console, dans une action suivie
   `capi-rancher-import:<ns>/<c>` :
   - elle crée un cluster `provisioning.cattle.io` vide dans
     `fleet-default` de Rancher avec le jeton de votre session, nommé
     `<harvester>-<c>` (`<harvester>-<ns>-<c>` quand le namespace diffère
     du nom) ;
   - elle lit le manifeste d'enregistrement chez ce Rancher (10 Mio au
     plus), l'applique dans le cluster enfant, puis l'efface : il porte un
     jeton et n'est jamais rangé, ni dans une étape ni dans un journal ;
   - elle sonde d'abord l'image de l'agent Rancher dans les registres du
     cluster enfant. Absente alors que le repli vers Internet est coupé :
     `rancher-agent-image-missing`, bloquant. Miroitez
     `registry.rancher.com` dans un registre du cluster ;
   - elle attend que le cluster soit actif (900 s par défaut, 60 à 3 600),
     puis marque le `Cluster` (`harvester-ops.io/rancher-external`).
4. **Exclusif de l'import par le Turtles intégré** (`rancher_import`) : les
   deux ensemble sont refusés (`rancher-import-conflict`). Deux agents se
   disputeraient `cattle-system`.
5. **Onglet Membres.** Les membres du cluster enfant importé dans Rancher,
   portée cluster seulement, comme dans la fenêtre Membres d'un cluster
   Harvester (`child-not-imported` s'il n'est pas importé dans votre
   Rancher).
6. **Retirer l'import** (`capi-rancher-unimport:`) : le cluster est
   supprimé dans Rancher et attendu (600 s). Puis les restes que Rancher
   laisse dans le cluster enfant (rôles et liaisons de portée cluster, CRD
   de `management.cattle.io` et d'autres, le namespace `local`) sont
   retirés, chacun jugé d'abord par le serveur. Seul part un objet qui
   porte une marque de Rancher (étiquette, annotation ou propriétaire
   `cattle.io`). Tout autre est gardé et nommé (`kept-not-rancher`).
   Depuis 2.8.2, le Bundle d'agent `fleet-agent-<nom dans Rancher>` que
   Rancher 2.14 laisse dans `fleet-default` est supprimé aussi, ici et à
   la suppression ou au Défaire d'un cluster enfant importé. Seulement si
   son nom est exact, que chacune de ses cibles ne nomme que ce cluster,
   qu'il porte une marque de Rancher ou de Fleet et aucune étiquette de la
   console, et une fois parti le cluster Fleet de ce nom (attendu jusqu'à
   60 s : Rancher le retire après son propre cluster ; tant qu'il est là,
   Fleet recréerait le Bundle, ou c'est l'agent d'un cluster Fleet
   vivant). Sinon il est gardé et nommé dans les étapes
   (`kept-not-rancher` ; `kept-fleet-cluster` ; `kept-not-matched`
   au-delà de 63 caractères, que Fleet raccourcit). Un refus de Rancher sur le Bundle est dit et ne
   défait pas le retrait. Vérifié par les tests seulement pour l'instant.
7. **Supprimer un cluster enfant importé.** Le cluster est d'abord retiré
   de Rancher, sans rien lire dans le cluster enfant (un enfant mort reste
   supprimable). Si Rancher est injoignable ou que vous n'avez pas de
   session, la suppression est refusée. Quand ce Rancher a disparu pour de
   bon, un administrateur oublie la marque d'import par `harvester-capi
   delete --forget-rancher` (API : `?forget_rancher=1`,
   `rancher-forget-admin` sinon). Le cluster peut alors rester listé dans
   ce Rancher.

### OIDC de l'API d'un cluster enfant

Sans Rancher, les personnes se connectent à l'API du cluster enfant par le
fournisseur d'identité de votre organisation. L'option `oidc` se pose à la
création (menu Accès, bloc OIDC) ou par un renouvellement :

| Clé | Règle |
|---|---|
| `issuer_url` | `https://` seulement, sans identifiants ni requête |
| `audience` | l'identifiant du client chez le fournisseur, 1 à 256 caractères |
| `username_claim`, `groups_claim` | `email` et `groups` par défaut |
| `username_prefix`, `groups_prefix` | `oidc:` par défaut ; jamais vide, jamais commençant par `system:` |
| `ca_pem` | CA facultative du fournisseur |
| `admin_group` | groupe facultatif lié à `cluster-admin` |

- La console écrit une `AuthenticationConfiguration` dans le seul gabarit
  du plan de contrôle : le fichier
  `/etc/rancher/rke2/hops-auth/authentication-config.yaml` (root, 0600) et
  l'argument `authentication-config` du serveur d'API. Version
  `apiserver.config.k8s.io/v1` dès Kubernetes 1.34, `v1beta1` de 1.30 à
  1.33, refusée avant (`oidc-version-too-old`). Les drapeaux `--oidc-*` ne
  sont jamais employés.
- Aucun secret : l'API n'a besoin que de l'audience.
- **`admin_group`** est lié à `cluster-admin` par le `ClusterRoleBinding`
  `hops-oidc-admins`, sujet `<groups_prefix><admin_group>`, dans le socle
  de politiques du cluster (`hops-policies`).
- **Télécharger le kubeconfig OIDC** (fenêtre Accès, onglet OIDC ;
  opérateur et droit d'administrer le cluster enfant) : serveur et CA du
  cluster enfant, utilisateur `oidc` par le greffon exec kubelogin
  (`kubectl oidc-login get-token`). Il ne porte aucun identifiant.
- Import Rancher et OIDC ensemble sont permis, avec l'avertissement
  `oidc-with-rancher`.

### Renouveler les machines d'un cluster enfant

Les registres, les réglages OIDC et la CA des registres vivent dans les
gabarits RKE2 et sont lus au premier démarrage d'un nœud. **Renouveler**
remplace les machines qui en ont besoin :

```
harvester-capi renew --name NS/C --what registries,oidc,ca [--spec F]
                     [--timeout 60-10800] [--dry-run]
harvester-capi renew --name NS/C --finish
harvester-capi renew --name NS/C --abandon
```

1. **Fenêtre Renouveler, Paramètres** : quoi renouveler (registres : relire
   les déclarations ou remplacer la liste ; OIDC : les réglages actuels sont
   remplis, décochez pour retirer l'OIDC ; nouvelle CA de registre), le
   délai.
2. **Différence** : l'essai à blanc, lu sur le cluster sans rien écrire.
   Renouveler reste grisé tant qu'il n'y a pas une différence fraîche avec
   des changements, sans constat bloquant, et la confirmation cochée.
3. Ce qui se passe :
   - seuls les gabarits qui changent sont copiés en `<nom>-r<N>` avec le
     nouveau contenu (annotation `harvester-ops.io/template-revision`), et
     la ClusterClass est repointée ;
   - seules les machines de ces gabarits sont remplacées, et seules elles
     sont attendues. Une adresse de plus du pool d'IP est prise pendant le
     remplacement (`renew-pool-short` bloque s'il n'y en a pas). Avec un
     seul plan de contrôle, l'API est coupée une minute environ ;
   - une marque `harvester-ops.io/renewal` sur le `Cluster` nomme la
     révision, les gabarits remplacés, les anciennes machines et les
     Secrets à retirer ;
   - une fois toutes les machines de ces rôles neuves : anciens gabarits et
     Secrets retirés, marque retirée. Au-delà du délai, rien d'ancien n'est
     retiré.
4. **Un renouvellement pas fini** (délai, console arrêtée) laisse sa
   marque. Un nouveau renouvellement est refusé (`renew-busy`). **Finir le
   renouvellement** (`--finish`, opérateur) attend de nouveau et fait le
   ménage. **Abandonner** (`--abandon`, administrateurs) retire seulement
   la marque et liste ce qui est gardé.
5. **Groupe administrateur.** Un changement de `admin_group` seul ne
   remplace aucune machine : il change l'annotation et relivre la liaison.
   Après chaque renouvellement OIDC, la liaison `hops-oidc-admins` suit les
   nouveaux réglages (changée, ajoutée ou retirée).
6. **Verrou.** Le renouvellement tient le verrou du cluster Harvester, le
   même que les plans. Pendant ce temps, les plans, le Défaire, la
   désinstallation de Turtles et les autres renouvellements sur le même
   cluster Harvester sont refusés (`renew-running`, `renew-busy`), jusqu'au
   délai : 1 h par défaut, 3 h au plus. Un renouvellement est refusé
   pendant un plan (`renew-plan-running`).

`--what ca` porte la CA suivante d'un registre dans le cluster pendant un
changement de CA (voir [artefacts.md](artefacts.md), « Registres achevés
(2.8.0) »). Il rafraîchit aussi la CA de `<c>-rke2-fetch` d'un cluster sans
Internet.

### Cluster enfant sans Internet

Un cluster enfant peut s'amorcer avec RKE2 lui-même tiré d'un registre :
option `offline: true` (fenêtre de création, menu Registres, **Sans
Internet** ; ou un modèle de cluster).

1. **Sur une machine connectée :** `harvester-artefacts pack --rke2
   v1.34.11+rke2r1 --cni calico`, importez le paquet, publiez-le dans un
   registre qui sert les images. Depuis la 2.8.1, le paquet porte aussi
   les images du fournisseur de cloud, du pilote CSI et de kube-vip que la
   console pose dans chaque cluster enfant : reconstruisez un paquet fait
   par la 2.8.0.
2. **Créez le cluster** avec ce registre. Il faut :
   - un registre qui miroite `docker.io` avec **Pas de repli vers
     Internet** (`offline-needs-no-fallback`) ;
   - l'élément `rke2` de la version de Kubernetes du modèle publié dans un
     registre du cluster (`offline-rke2-missing`,
     `offline-rke2-version-mismatch`). La révision `rke2rN` n'est pas
     comparée.
3. Ce que font les nœuds, dans les deux gabarits :
   - un fichier `/etc/rancher/rke2/hops-fetch.netrc` (0600) avec le compte
     de lecture, depuis le Secret `<c>-rke2-fetch`, jamais dans une
     commande ;
   - `curl` avec ce netrc récupère les trois fichiers par empreinte dans
     `/opt/rke2-artifacts`. Chaque sha256 est contrôlé, puis
     `sha256sum-amd64.txt`. Au moindre écart, tout est retiré et le
     démarrage échoue ;
   - `/opt/install.sh` n'est posé qu'après ces contrôles, et CAPRKE2 le
     lance (`agentConfig.airGapped: true`).
4. `offline_artifacts: image` : l'image de VM porte déjà ces fichiers. Rien
   n'est récupéré.
5. cloud-init sans réseau (2.8.1, les deux modes). CAPHV demande à
   cloud-init une mise à jour des paquets, ainsi que `qemu-guest-agent` et
   `iptables`, sur chaque machine. Sans Internet, zypper attendait environ
   5 minutes par dépôt avant RKE2. La console pose désormais
   `agentConfig.additionalUserData` dans les deux gabarits :
   - `package_update`, `package_upgrade` et `package_reboot_if_required`
     à false ;
   - au premier démarrage, un `bootcmd` met de côté `/etc/zypp/repos.d` et
     `/etc/zypp/services.d` (une fois par instance). La liste de paquets de
     CAPHV ne peut pas être retirée, et son installation rafraîchirait les
     dépôts. Sans dépôt, elle échoue tout de suite ;
   - la première commande sans Internet les remet en place, avant la
     récupération.

   L'image doit donc déjà porter `iptables` et `qemu-guest-agent` : ils ne
   sont pas installés. Sans `iptables`, les pods qui publient un port
   d'hôte (ingress-nginx) ne démarrent pas. Sans `qemu-guest-agent`, les
   machines ne remontent aucune adresse. Chaque création et chaque
   contrôle de plan sans Internet affiche l'avertissement
   `offline-node-packages` (non bloquant) ; `image-sles-repos` n'est pas
   affiché dans ce mode.

   `cloud-init status` signale une erreur sur ces nœuds, c'est voulu : le
   module des paquets échoue tout de suite faute de dépôt, et le démarrage
   continue. `repos_back` écrit une ligne par répertoire dans
   `/var/log/cloud-init-output.log` (`harvester-ops: zypper repositories
   restored in ...`).
6. `offline_packages: keep` (fenêtre de création, menu Registres, **Dépôts
   de l'image** ; ou un modèle de cluster ; `skip` par défaut) : pour une
   image dont les dépôts visent un miroir du réseau local (RMT, MLM). Les
   dépôts restent en place et cloud-init installe `iptables` et
   `qemu-guest-agent` depuis le miroir. `package_update` et
   `package_upgrade` restent à false dans les deux modes. Avec `keep`, un
   dépôt que les machines ne joignent pas ramène l'attente de plusieurs
   minutes par dépôt.

Limites : la récupération emploie l'authentification Basic. Un registre
qui n'accepte que l'authentification par jeton sur ses blobs (Harbor,
façon ghcr) n'est pas contrôlé. Un disque de moins de 20 Gio est un
avertissement (`offline-disk-small`) : à 10 Gio, le banc a atteint 78 %
avec l'agent et Flux, et le ramasse-miettes des images échouait.

### Rôles (2.8.0)

| Geste | Rôle |
|---|---|
| Lire les imports, les réglages OIDC, les entrées GitOps | lecteur |
| Importer, retirer l'import, poser ou retirer une entrée Flux, renouveler, finir un renouvellement, télécharger le kubeconfig OIDC | opérateur **et** droit d'administrer le cluster enfant (et une session Rancher pour Rancher) |
| Entrées Fleet, `privileges: cluster`, abandonner un renouvellement, `delete --forget-rancher`, `renew-cert`, `harvester-nodes` | admin |

### Vu en réel (2.8.0)

Sur harvlab2, avec un Dex de banc (OIDC) et le Rancher externe 2.14.1 :

- OIDC : un membre du groupe administrateur est `cluster-admin`
  (`kubectl auth whoami`, `auth can-i '*' '*'`) ; une autre personne n'a
  que ce que donne le rôle d'équipe (`view` dans `equipe-a`) ; un jeton
  d'une autre audience est refusé (401). Le kubeconfig OIDC ne porte aucun
  identifiant.
- Import à la création : `Active` en 142 s, Membres montre qui a importé
  (`cluster-owner`) ; Retirer l'import : le cluster quitte Rancher, 36
  restes supprimés dans le cluster enfant, 11 gardés sans marque de Rancher
  (`kept-not-rancher`), la liste des clusters de Rancher identique à avant.
- Renouveler `--what oidc` (préfixe d'utilisateur et groupe administrateur
  changés) : seule la machine du plan de contrôle remplacée, workers
  gardés ; la liaison `hops-oidc-admins` passée au nouveau groupe. Arrêté
  par `--timeout 60`, puis `--finish` a fait le ménage. `--abandon`, puis le
  renouvellement suivant a retiré les gabarits et le Secret des nœuds
  laissés.
- Le verrou : `harvester-plans apply` refusé pendant un renouvellement
  (`renew-running`), et un renouvellement refusé pendant un plan
  (`renew-plan-running`).

Non vérifié en réel : un fournisseur d'identité de client autre que Dex
(Keycloak, Entra ID), Rancher 2.15, un Rancher en airgap avec
`system-default-registry`, le cluster enfant sans Internet de bout en bout
(arrêté en 2.8.0 par la mise à jour de paquets que CAPHV demande à
cloud-init ; 2.8.1 la saute, l'essai au banc reste à faire), un plan enchaîné à une vraie installation de Harvester avec
un secret Git, plus de deux clusters enfants, harv4 (deux clusters dans une
exécution).

## 19. Magasin de secrets (2.9.0)

1. Choisir le mode : A (votre OpenBao ou Vault) ou B1 (OpenBao à côté de
   la console). B1 demande le paquet OpenBao construit avec votre compte de
   l'Application Collection.
2. Paramètres > Magasin de secrets > Activer OpenBao : lancer les
   vérifications, activer.
3. B1 : exporter, télécharger et acquitter la copie hors ligne de la clé de
   descellement ; régler le répertoire et l'heure (UTC) des instantanés.
4. Migrer les classes une par une (onglet Classes), kubeconfig en dernier
   après **Tester la panne** ; confirmer les originaux une fois que la
   console fonctionne.
5. Avant de ramener la console sous la 2.9.0, ou avant de désactiver :
   ramener toutes les classes, remettre en clair les états chiffrés, puis
   désactiver.

Vu en réel (2.9.0) : le mode A sur OpenBao 2.7.2 et Vault 1.14.2 (chaque
vérification mise en échec une fois, puis activation, classes migrées,
console redémarrée, valeurs identiques, retour en local), un kubeconfig de
harv1 migré et employé pour démarrer et arrêter une VM, des pannes (cache
chaud et froid, scellé, jeton révoqué, politique restreinte, audit plein),
la corrélation d'audit par `X-Hops-Action`, B1 depuis l'Application
Collection sur une VM de client (redémarrage descellé tout seul, rotations,
instantané et restauration, mise à jour), des états OpenTofu chiffrés sur
harvlab2, la rotation de clé KVM sur node2.
