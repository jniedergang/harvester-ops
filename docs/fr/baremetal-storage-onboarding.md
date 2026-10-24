# Profils de stockage facultatifs pour un nœud bare-metal

Dans **Bare-metal > Installer**, sélectionner le mode de jonction à un cluster
existant puis activer la configuration de stockage facultative. L'option reste
désactivée par défaut. Elle refuse un nom de nœud déjà enregistré.

Choisir un profil et fournir les noms Linux, MAC physiques et adresses IPv4/CIDR
attribuées à cet hôte. Les interfaces de gestion, VM et stockage doivent être
distinctes. Aucune adresse n'est déduite de l'IP de gestion. Les connexions de
stockage n'ont ni passerelle ni DNS, avec `never-default=true` et IPv6 désactivé.
Les interfaces VM utilisent les contrôles existants. Les pools et interfaces VM
sont configurés avant les vérifications finales du stockage.

- **Storage network only** configure deux interfaces NetworkManager dédiées,
  sans réglages multipath, iSCSI, NVMe ni vérification de fournisseur CSI.
- **Pure iSCSI and NVMe/TCP (example)** configure deux interfaces iSCSI et deux
  NVMe/TCP, les modules/règles udev/multipath et les liaisons `eth-iscsi1/2`.
  Il exclut le WWID SCSI exact du disque local et les volumes logiques HPE.
  Les arguments de démarrage conservent `multipath=off`; le profil OEM active
  multipathd pour le stockage externe. Adapter ces réglages à son environnement.

L'exemple Pure attend le DaemonSet **déjà installé**
`portworx/px-pure-csi-node`. Son nom et son namespace sont des données du profil,
pas une convention universelle. Aucun profil n'installe de chart CSI, ne crée de
secrets ni ne configure l'accès aux baies. Vérifier le YAML généré avant de lancer.
Le disque local Longhorn doit devenir Ready. Le partitionnement reste inchangé.

Une taint initiale `harvester-ops.io/onboarding=pending:NoSchedule` protège le
nouveau nœud. Après vérification de l'identité propre à l'installation, un patch
conditionnel UID/resourceVersion le rend non planifiable et enlève uniquement
cette taint. Les autres labels/taints sont préservés. Les opérations utilisent le
kubeconfig de la personne, figé lors de la requête de jonction.

Les étapes OEM vérifient les NIC, adresses, modules, services et liaisons avant
d'annoncer la configuration terminée. Les contrôles finaux exigent Node Ready,
absence de pression, disque local Longhorn prêt et, si déclaré par le profil,
pod du DaemonSet CSI exact prêt. Un statut initial nul reste en attente.
Le nœud reste non planifiable après succès ou échec; aucune remise en production,
migration de VM ou suppression de nœud n'est automatique. Une écriture incertaine
n'est jamais renvoyée automatiquement.

Les profils sont du code de livraison revu, sous
`web/storage_profiles/<identifiant>/profile.yaml`. Le manifeste décrit les ports,
fichiers OEM explicites, politique du disque local, arguments de démarrage,
modules, services, liaisons iSCSI et référence CSI facultative. Copier le profil
neutre pour ajouter un fournisseur et ajouter ses tests. Les fichiers OEM peuvent
exécuter des commandes privilégiées : ne jamais accepter de scripts arbitraires
depuis l'API, ni inclure d'adresses réelles ou de secrets. Voir le
[guide anglais](../en/baremetal-storage-onboarding.md) pour le schéma détaillé.

La validation finale des chemins, du basculement, des migrations et des E/S du
guest reste nécessaire. L'option concerne le dialogue d'installation unitaire;
l'éditeur YAML par lots ne sérialise pas encore cet objet.
