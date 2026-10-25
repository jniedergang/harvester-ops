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
Chaque commande exige `--cluster <nom>`. L'UI web permet de basculer entre clusters via la sidebar gauche.

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

Dans l'UI web, l'onglet **Namespaces** liste chaque namespace avec ses VMs et permet de les démarrer/arrêter en groupe.

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

## 13. Navigation par hôtes (2.0)

Un **hôte** est un cluster Harvester entier ou un hyperviseur KVM, jamais un
nœud Harvester. Le menu latéral liste tous les hôtes que la personne voit, en
deux familles, Harvester et KVM, sous **Hôtes**. Pas de plafond : une flotte
de 60 clusters et 40 hôtes KVM est listée en entier, dans une partie du menu
qui défile seule.

- **Tête de famille.** Chaque famille se replie (le choix est gardé par le
  navigateur) et dit son état en une ligne, par exemple `58 ok, 2 en alerte`.
  Les hôtes pas encore lus sont comptés dans son info-bulle.
- **Ligne d'un hôte.** Une pastille donne l'état (verte joignable, rouge
  injoignable ou en erreur, orange accès refusé, grise pas encore lu), puis
  le nom et le nombre de VMs quand un écran les a déjà lues. Un hôte en alerte
  reste visible même dans une famille repliée. La liste ne lit jamais un hôte
  elle-même.
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
  **Conteneurs** de toute la flotte ; en dessous : **Disaster Recovery** et
  **Migrations**.

Chaque entrée est un vrai lien : le clic milieu l'ouvre dans un nouvel
onglet, et l'adresse se copie. Un lien ouvert avant la connexion est rouvert
une fois connecté.

### Page d'un hôte (Harvester)

Un hôte Harvester s'ouvre sur une page avec un bandeau en tête : la famille,
le nom, l'état, la version et le nombre de VMs, le sous-menu de l'hôte (Vue
d'ensemble, Machines virtuelles, Conteneurs, Imports et migrations,
Paramètres, Disaster Recovery) et **Activité de cet hôte**. Chaque vue déjà
construite garde sa place ; le bandeau ne fait que l'encadrer.

- **Vue d'ensemble** (`#harvester/<c>`) : métriques, jauges d'usage, tableau
  des nœuds, et le bloc **Événements** en dessous (les sous-onglets Cluster
  et Événements sont partis). `#harvester/<c>/overview/events` fait défiler
  jusqu'aux événements. Le bouton **Upgrade** ouvre Paramètres > Mise à jour.
- **Paramètres** (`#harvester/<c>/params/<onglet>`), en onglets :
  - **Nœuds** : la vue en blocs des nœuds, avec la maintenance et les
    réglages d'hôte, et **Joindre un nœud** (administrateur), qui ouvre
    Automatisation > Bare-metal avec ce cluster déjà choisi : la fenêtre
    d'installation s'ouvre en mode jonction vers `https://<VIP>:443`. La VIP
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
  - **Alimentation** : Démarrage et Arrêt (avec l'ordre des VMs).
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
