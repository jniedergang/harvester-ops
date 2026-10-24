# Profils de stockage facultatifs pour un nœud bare-metal

Dans **Bare-metal > Installer**, sélectionner le mode de jonction à un cluster
existant puis activer la configuration de stockage facultative. L'option reste
désactivée par défaut et masquée à la création d'un cluster. Elle refuse un nom
de nœud déjà enregistré, ainsi qu'un nœud témoin (witness).

Choisir un profil. Pour chaque port de stockage, désigner la carte physique par
sa **MAC** et saisir l'adresse **IPv4/CIDR** attribuée. Les interfaces de
gestion, VM et stockage doivent être distinctes. Aucune adresse n'est déduite de
l'IP de gestion. Les connexions de stockage n'ont ni passerelle ni DNS, avec
`never-default=true` et IPv6 désactivé. Les interfaces VM utilisent les
contrôles existants. Les pools et interfaces VM sont configurés avant les
vérifications finales du stockage.

## Identité des cartes : la MAC, pas le nom

Une carte de stockage est désignée par sa seule MAC, comme les cartes de gestion
et les cartes VM. Aucun nom Linux n'est saisi et aucun préfixe n'est supposé
(`ens`, `enp`, `eth`, `eno` et les autres conviennent).

- À chaque démarrage, le script généré cherche la MAC dans
  `/sys/class/net/*/address`. Il exige une seule carte physique (lien `device`),
  non asservie à un bond ou un pont. Sinon l'étape de stockage s'arrête avant
  toute activation.
- Le nom trouvé est rangé dans `/run/harvester-ops/storage-nics/<rôle>` pour ce
  démarrage. Les fichiers de profil le désignent par `{{rôle}}`.
- La connexion NetworkManager est liée par `[ethernet] mac-address=`, sans
  `interface-name`. Une carte renommée garde sa connexion.
- Après la jonction, la console relit l'inventaire LinkMonitor de l'hôte. Chaque
  MAC doit être une carte physique hors bond. Une configuration réseau
  automatique (VlanConfig) qui vise l'hôte ne doit lister aucune carte de
  stockage. Ce contrôle n'a lieu qu'après la jonction : les noms ne sont pas
  connus avant.

Avant l'installation, la console refuse une MAC de stockage que LinkMonitor
signale déjà sur un hôte enregistré.

## Options de l'installeur

`harvester.install.skipchecks=true` (ou le champ de levée des contrôles) est
accepté avec un profil de stockage. Le profil ne dépend pas des contrôles
matériels de l'installeur.

## Profils livrés

- **Storage network only** configure deux interfaces NetworkManager dédiées,
  sans réglages multipath, iSCSI, NVMe ni vérification de fournisseur CSI.
- **Pure iSCSI and NVMe/TCP (unverified example)** configure deux interfaces
  iSCSI et deux NVMe/TCP, les règles udev, les réglages multipath des baies et
  les liaisons `eth-iscsi1/2`. **Cet exemple n'a pas été vérifié sur une baie
  par les mainteneurs de la console.** Adapter chaque réglage à son
  environnement.

L'exemple Pure passe par l'option multipath de l'installeur Harvester. Son bloc
`multipath` devient `os.external_storage_config` (`enabled: true` et un
`multi_path_config` avec `blacklist`, `blacklistExceptions` et
`blacklistWwids`). Harvester rend alors `/etc/multipath.conf` et active
`multipathd`, et l'installeur n'ajoute plus `multipath=off`. Le WWID du disque
de démarrage local saisi est ajouté à `blacklistWwids`. Saisir le WWID SCSI du
vrai disque local, ni `/dev/sda` ni un WWN préfixé par `0x`. Les réglages que
`external_storage_config` ne sait pas exprimer (`defaults`, `devices`,
`devnode`) vont dans `/etc/multipath/conf.d/90-pure.conf`. Les modules noyau
sont chargés par l'installeur (`os.modules`).

L'exemple Pure attend le DaemonSet **déjà installé**
`portworx/px-pure-csi-node`. Son nom et son namespace sont des données du profil,
pas une convention universelle. Aucun profil n'installe de chart CSI, ne crée de
secrets ni ne configure l'accès aux baies. Vérifier le YAML généré avant de lancer.
Le disque local Longhorn doit devenir Ready. Le partitionnement reste inchangé.

## Protection et vérifications

Le nœud qui rejoint reste cordonné. La façon d'y arriver dépend de la version
du cluster rejoint. La console lit le réglage `server-version` avec le
kubeconfig de la personne, figé lors de la requête, avant l'installation.

### Harvester v1.9.0 et plus : cordon natif de l'installeur

La console pose l'option d'installation `install.cordoned: true` (ajoutée par
harvester/harvester#11301, présente dans le tag v1.9.0). Harvester fait le
reste :

- l'installeur ajoute le label `harvesterhci.io/install-cordoned=true` à la
  configuration de RKE2 : le nœud le porte dès son enregistrement ;
- le contrôleur de promotion cordonne le nœud une seule fois et le note par
  l'annotation `harvesterhci.io/install-cordoned-processed=true` ;
- rien dans Harvester ne lève ce cordon, et le contrôleur ne choisit jamais un
  nœud cordonné pour une promotion.

La console n'écrit rien sur le nœud. Elle attend l'annotation, puis vérifie que
le nœud est toujours non planifiable. Aucune taint d'onboarding n'est écrite et
aucun droit de patch sur les nœuds n'est demandé. La tâche s'arrête si le nœud
n'a pas le label (un installeur plus ancien que le cluster ignore l'option) ou
s'il est planifiable. Harvester laisse le nœud planifiable quand aucun autre
nœud n'est disponible.

Les préversions de v1.9.0 et une version illisible suivent le mécanisme de la
console ci-dessous.

### Harvester v1.8 : taint et cordon de la console

Une taint initiale `harvester-ops.io/onboarding=pending:NoSchedule` protège le
nouveau nœud. Elle est écrite dans `/etc/rancher/rke2/config.yaml.d/` par
l'étape initramfs du système installé, avant le premier démarrage de RKE2. Cette
étape réécrit le fichier à **chaque démarrage**. Il n'a plus d'effet après
l'enregistrement : kubelet n'applique ces taints qu'à la création de l'objet
Node. Un redémarrage ultérieur ne remet pas la taint, sauf si l'objet Node a été
supprimé. Le fichier vit dans la configuration OEM de l'installeur, que la
console ne peut pas modifier après la jonction : il n'est donc pas retiré.

Après vérification de l'identité propre à l'installation, un patch conditionnel
UID/resourceVersion rend le nœud non planifiable et enlève uniquement cette
taint. Si le nœud a changé entre-temps, le patch est refusé en bloc : la console
relit le nœud et recommence, au plus `HARVESTER_OPS_ONBOARDING_PATCH_ATTEMPTS`
fois (5 par défaut). Un refus de la RBAC arrête tout de suite. Une écriture dont
l'issue est inconnue (délai dépassé) n'est jamais renvoyée. Les autres
labels/taints sont préservés. Les opérations utilisent le kubeconfig de la
personne, figé lors de la requête de jonction.

Harvester promeut un nœud qui rejoint le cluster tant que celui-ci compte moins
de trois nœuds de gestion, sauf s'il a le rôle `worker`. Son contrôleur de
promotion rend le nœud non planifiable au début et planifiable à la fin
(`promote_controller.go`). L'état se lit dans l'annotation
`harvesterhci.io/promote-status` (`running`, `complete`, `failed`, `unknown`).
En v1.8, le contrôleur peut promouvoir un nœud déjà cordonné par la console.

- Tant que l'état vaut `running`, rien n'est déclaré prêt et la tâche attend.
  Si la promotion a commencé avant le cordon de la console, la taint
  d'onboarding reste en place jusque-là. À l'état `complete`, la console
  cordonne le nœud et retire la taint.
- Si le contrôleur lève le cordon en fin de promotion, la console le repose.
  Toute autre levée arrête la tâche.
- Une promotion `failed` ou `unknown` arrête la tâche ; le nœud garde sa taint
  ou son cordon.

Deux trous restent en v1.8 :

- entre la levée du cordon par le contrôleur et le nouveau cordon de la
  console, le nœud peut rester planifiable le temps d'un intervalle de lecture
  (5 secondes) ;
- une promotion qui commence après la fin de la tâche lève le cordon. Vérifier
  le nœud avant de le remettre en service.

### Vérifications finales

Les étapes OEM vérifient les cartes, adresses, modules, services et liaisons
avant d'annoncer la configuration terminée. Les contrôles finaux exigent Node
Ready, absence de pression, disque local Longhorn prêt et, si déclaré par le
profil, pod du DaemonSet CSI exact prêt sur ce nœud (lu avec un sélecteur de
champ côté serveur, un pod en cours de suppression est ignoré). Un statut
initial nul reste en attente. Les contrôles abandonnent après
`HARVESTER_OPS_ONBOARDING_TIMEOUT` secondes (1800 par défaut), dans le budget
d'après installation. Le nœud reste non planifiable après succès ou échec ;
aucune remise en production, migration de VM ou suppression de nœud n'est
automatique.

## Ajouter un fournisseur

Les profils sont du code de livraison revu, sous
`web/storage_profiles/<identifiant>/profile.yaml`. Le manifeste décrit les ports,
fichiers OEM explicites, politique du disque local, bloc `multipath` (clés de
`multi_path_config`), modules chargés par l'installeur, services, liaisons iSCSI
et référence CSI facultative. Les fichiers OEM n'utilisent que les étapes `fs`,
`boot` et `network` (leurs étapes `rootfs` et `initramfs` ne tourneraient pas au
premier démarrage) ; `{{rôle}}` n'est admis que dans l'étape `network`. Copier le
profil neutre pour ajouter un fournisseur et ajouter ses tests. Les fichiers OEM
peuvent exécuter des commandes privilégiées : ne jamais accepter de scripts
arbitraires depuis l'API, ni inclure d'adresses réelles ou de secrets. Voir le
[guide anglais](../en/baremetal-storage-onboarding.md) pour le schéma détaillé.

La validation finale des chemins, du basculement, des migrations et des E/S du
guest reste nécessaire. L'option concerne le dialogue d'installation unitaire ;
l'éditeur YAML par lots ne sérialise pas encore cet objet.
