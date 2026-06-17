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

## 3. Séquence de démarrage (5 étapes)

| # | Étape | Action |
|---|---|---|
| 1 | Démarrage du premier CP | L'opérateur allume **le dernier CP éteint** via iLO/iDRAC/IPMI. Le script attend `/readyz` sur l'API |
| 2 | Démarrage des autres nodes | Autres CPs puis workers, un par un |
| 3 | Attente Nodes Ready | Polling jusqu'à ce que tous les nodes reportent `Ready` |
| 4 | Restauration | Réactivation du rebuild Longhorn, `uncordon` de tous les nodes |
| 5 | Redémarrage des VMs | Tous les VMs en `runStrategy: Halted` repassent à `Always` |

## 4. Exécution

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

- Paramètres → onglet **Clusters** → *Add cluster*, OU bouton **+** à côté du sélecteur de cluster dans la sidebar.
- Renseigner : un nom unique, une description, l'utilisateur SSH (défaut `rancher`), la liste des nœuds (hostname + IP + role : `control-plane` ou `worker`), upload kubeconfig + clé SSH privée.
- Le cluster est persisté dans `/etc/harvester-ops/config.yaml` (écriture atomique, `.bak` de la version précédente conservée).
- Le kubeconfig est stocké dans `/etc/harvester-ops/kubeconfigs/<nom>.yaml` (mode 0600) ; la clé SSH dans `/etc/harvester-ops/ssh/<nom>_id` (mode 0600).
- Depuis l'onglet Clusters tu peux aussi : remplacer le kubeconfig, remplacer la clé SSH, lancer **Test kubeconfig** (kubectl version) ou **Test SSH** (ping par nœud), éditer les métadonnées, ou supprimer le cluster (et ses fichiers associés).

## 11. Conception sécurisée

- **Double consentement** : les opérations destructives exigent à la fois un flag non-interactive (`--yes`) ET une confirmation finale, sauf si `--yes` est explicitement passé.
- **Idempotence** : ré-exécuter le script après un échec partiel reprend là où il en était (ex. VMs déjà arrêtées → on saute l'attente de l'étape 2).
- **Pas d'échec silencieux** : chaque étape sort en non-zéro sur erreur, sauf si l'opérateur choisit explicitement de l'ignorer.
- **Auditable** : chaque commande est tracée avec timestamp et code de retour.
