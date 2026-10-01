# Parité avec l'interface de Harvester

État au 2026-09-30, console **v1.79.0**, comparée à l'interface de **Harvester v1.9** (menus relevés dans le code de harvester-ui-extension v1.9.0 et la documentation v1.9).

Sur 136 fonctions de l'interface de Harvester : **133 faites**, **2 partielles**, **1 manquantes** ; 1 hors périmètre. Une fonction manquante porte la version où elle est prévue.

Statuts : Fait, Partiel (ce qui manque est dit), Manquant (version prévue), Console seulement (ce que Harvester n'a pas), Hors périmètre.

## Tableau de bord

Menu Harvester : *Dashboard*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Compteurs nœuds, VMs, volumes | Fait | 1.2 | Vue d'ensemble |
| Capacité CPU, mémoire, stockage | Fait | 1.62 | usage réel (metrics.k8s.io), réservé, stockage écrit et promis |
| Événements du cluster (hôtes, VMs, volumes, images) | Fait | 1.62 | onglet Événements de l'aperçu, filtre avertissements |
| Métriques du cluster et des VMs (rancher-monitoring) | Fait | 1.70 | onglet Métriques : Prometheus quand rancher-monitoring est actif, metrics-server sinon ; CPU de VM juste (Harvester le divise par 1000) |
| Bouton Mettre à jour Harvester | Fait | 1.69 | dans l'en-tête de l'aperçu et sur server-version |

## Hôtes

Menu Harvester : *Hosts*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Liste des hôtes, état, rôles | Fait | 1.0 |  |
| Mode maintenance (avec forçage) | Fait | 1.43 | pré-contrôle : ce qui migre, ce qui s'arrête |
| Cordon / uncordon | Fait | 1.27 |  |
| Modifier : nom affiché, URL de console, labels | Fait | 1.62 | labels système protégés |
| Disques : ajouter, retirer, étiquettes (host/disk tags) | Fait | 1.62 | Longhorn V1, V2 ou LVM ; planification par disque |
| Hugepages | Fait | 1.62 |  |
| Ksmtuned (stratégie, mode, seuils) | Fait | 1.62 |  |
| Activer / désactiver le CPU manager | Fait | 1.62 |  |
| Alimentation (éteindre, allumer, redémarrer) | Fait | 1.62 | par harvester-seeder en maintenance, comme Harvester ; aussi par Redfish dans Bare-metal |
| Accès hors bande (seeder) | Fait | 1.62 | vérifié par IPMI (virtualbmc) ; Redfish sur 443 seulement |
| Supprimer un hôte (cluster à plusieurs nœuds) | Fait | 1.62 | nom tapé pour confirmer |
| Détail : réseau, stockage, VMs de l'hôte | Fait | 1.68 | onglets Essentiel, Instances, Réseau, Événements de la fenêtre d'hôte |

## Machines virtuelles : liste et actions

Menu Harvester : *Virtual Machines*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Liste par namespace, état, stratégie | Fait | 1.2 |  |
| Colonnes CPU, mémoire, IP, nœud ; filtre par labels | Fait | 1.61 |  |
| Démarrer / arrêter | Fait | 1.2 |  |
| Redémarrer | Fait | 1.60 | dans le délai de grâce (menu) ou brutal (console) |
| Redémarrage doux (agent invité) | Fait | 1.60 |  |
| Pause / reprise | Fait | 1.60 |  |
| Arrêt forcé | Fait | 1.60 |  |
| Migrer | Fait | 1.60 | vers un nœud choisi ou n'importe lequel |
| Abandonner une migration | Fait | 1.60 |  |
| Migration du stockage (volume vers un autre) | Fait | 1.61 | refusée après la bascule de KubeVirt (reviendrait à l'ancienne copie) |
| Prendre une sauvegarde | Fait | 1.58 |  |
| Prendre un instantané | Fait | 1.2 |  |
| Restaurer (nouvelle VM ou remplacement, MAC gardée) | Fait | 1.11 |  |
| Créer une planification depuis la VM | Fait | 1.61 |  |
| Quota d'instantanés de la VM | Fait | 1.61 |  |
| Modifier CPU et mémoire à chaud | Fait | 1.61 | la mémoire exige virtio-mem dans l'invité, et 1 Gio au moins |
| Ajouter un volume à chaud / le détacher | Fait | 1.60 |  |
| Brancher / débrancher une carte réseau à chaud | Fait | 1.61 |  |
| Éjecter un CD-ROM | Fait | 1.60 | à froid, avec la suppression de son volume, comme l'action historique de Harvester |
| Insérer une image dans un CD-ROM | Fait | 1.61 | lecteur SATA vide ; éjection à chaud aussi |
| Générer un template | Fait | 1.60 | depuis une VM, avec ou sans les données |
| Cloner (avec ou sans les données) | Fait | 1.60 |  |
| Supprimer, en choisissant les volumes | Fait | 1.60 | le bouton de la vue Cluster échouait jusqu'à 1.59 (route absente) |
| Modifier / télécharger le YAML | Fait | 1.60 |  |
| Modifier la configuration | Fait | 1.8 | essai à blanc côté serveur avant d'appliquer |
| Console VNC | Fait | 1.7 | partagée entre plusieurs personnes |
| Console série | Fait | 1.61 |  |
| Journaux de la VM | Fait | 1.61 | pod virt-launcher, opérateurs |
| Actions groupées | Fait | 1.60 | démarrer, arrêter, redémarrer, arrêt forcé, migrer |

## Machines virtuelles : création et réglages

Menu Harvester : *Create / Edit VM*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Création, une ou plusieurs instances | Fait | 1.28 | jusqu'à 50, essai à blanc |
| Depuis un template et sa version | Fait | 1.29 |  |
| CPU, mémoire, modèle de CPU, épinglage, NUMA | Fait | 1.12 |  |
| Plafonds du branchement à chaud CPU / mémoire | Fait | 1.61 | case de Harvester : un cœur par socket, limites = maximums |
| Volumes : image, vide, existant, conteneur ; ordre de boot | Fait | 1.8 |  |
| Cartes réseau (modèle, type, MAC) | Fait | 1.8 |  |
| IP statique d'une carte (v1.9) | Fait | 1.62 | appliquée par kube-ovn sur un réseau overlay (DHCP vers l'invité) ; seulement montrée sur un VLAN |
| Placement sur les nœuds (sélecteur, règles) | Fait | 1.13 |  |
| Affinité / anti-affinité entre VMs | Fait | 1.13 |  |
| Périphériques PCI | Fait | 1.15 |  |
| Périphériques USB | Fait | 1.68 | tablette QEMU passée à une VM sur le banc |
| Access credentials (mot de passe, clés par l'agent) | Fait | 1.61 | appliqués au prochain redémarrage |
| Volume de système de fichiers (virtiofs, v1.9) | Fait | 1.62 | à la création ; le noyau invité doit connaître virtiofs |
| Labels, labels d'instance, annotations | Fait | 1.62 |  |
| Stratégie d'exécution | Fait | 1.2 |  |
| Type d'OS, mémoire réservée, stratégie de maintenance | Fait | 1.61 | et nom affiché, description à la clé de Harvester |
| Nom d'hôte, délai d'arrêt | Fait | 1.12 |  |
| Cloud-init (user-data, network-data) | Fait | 1.60 | perdu à la création jusqu'à 1.59 ; en Secret, créé ou converti à l'enregistrement |
| Clés SSH à la création | Fait | 1.60 |  |
| Installer l'agent invité | Fait | 1.60 |  |
| Windows : unattend et sysprep | Fait | 1.62 | fichier de réponses à la création, lecteur sysprep |
| TPM, EFI, Secure Boot, tablette USB | Fait | 1.12 |  |

## Volumes

Menu Harvester : *Volumes*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Liste : répliques, santé, VM, place écrite | Fait | 1.40 |  |
| Créer (vide ou depuis une image) | Fait | 1.59 |  |
| Agrandir | Fait | 1.59 |  |
| Supprimer | Fait | 1.63 | tout volume qu'aucune VM n'utilise |
| Cloner (avec ou sans les données) | Fait | 1.63 |  |
| Exporter en image | Fait | 1.63 |  |
| Prendre un instantané | Fait | 1.63 |  |
| Annuler un agrandissement | Fait | 1.63 | vérifié sur un agrandissement figé (banc) |
| Migration de données (vers une autre classe) | Fait | 1.63 | une copie par CDI, l'original reste, comme Harvester |
| Modifier / télécharger le YAML | Fait | 1.60 |  |
| Diagnostic des volumes dégradés et corrections | Console seulement | 1.42 | propre à la console |

## Images

Menu Harvester : *Images*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Liste : état, taille, classe, qui s'en sert | Fait | 1.57 |  |
| Créer depuis une URL | Fait | 1.59 |  |
| Envoyer un fichier depuis le navigateur | Fait | 1.63 | la console le sert au cluster (port 8092) |
| Somme de contrôle SHA512 | Fait | 1.63 |  |
| Chiffrer / déchiffrer | Fait | 1.63 |  |
| Télécharger l'image | Fait | 1.74 | images Longhorn v1 (gzip) ; images CDI en qcow2 par le downloader de Harvester, vérifié sur une classe LVM |
| Cloner, modifier (description, labels) | Fait | 1.63 | le nom est figé par Harvester |
| Créer une VM depuis l'image | Fait | 1.63 |  |
| Supprimer (si rien ne s'en sert) | Fait | 1.59 |  |
| Modifier / télécharger le YAML | Fait | 1.60 |  |

## Namespaces

Menu Harvester : *Namespaces*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Liste | Fait | 1.62 | fenêtre Namespaces : VMs, volumes, quota ; système caché |
| Créer / modifier / supprimer | Fait | 1.62 | suppression par nom tapé |
| Quota d'instantanés du namespace | Fait | 1.62 |  |

## Réseaux

Menu Harvester : *Networks*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Réseaux de cluster et configurations (cartes, bond, MTU, hôtes visés, état par hôte) | Fait | 1.65 |  |
| Déplacer une configuration vers un autre réseau de cluster | Fait | 1.65 |  |
| Réseaux de VMs (VLAN, sans étiquette, trunk : créer, modifier, supprimer) | Fait | 1.65 |  |
| Réseau overlay (kube-ovn) | Fait | 1.49 |  |
| Mode trunk (plages de VLAN), route et serveur DHCP | Fait | 1.65 |  |
| Load balancers | Fait | 1.65 |  |
| IP pools | Fait | 1.65 |  |
| Réseaux d'hôte (HostNetworkConfig) | Fait | 1.65 |  |
| Réseau de stockage, de migration, RWX | Fait | 1.65 |  |
| Chemin réseau d'une VM jusqu'au switch (LLDP) | Console seulement | 1.36 | propre à la console |

## Réseaux overlay et underlay (kube-ovn)

Menu Harvester : *Overlay / Underlay Networks*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| VPC : créer, routes statiques, peerings | Fait | 1.49 |  |
| Sous-réseaux (CIDR, passerelle, NAT sortant, DHCP, ACL) | Fait | 1.49 |  |
| Politiques réseau (isolation des VMs) | Fait | 1.66 |  |
| Passerelles NAT, IP externes, règles SNAT / DNAT | Fait | 1.66 |  |
| Underlay : réseaux fournisseurs, VLANs, réseaux externes | Fait | 1.66 |  |
| Santé de kube-ovn dite avant tout geste ; réparation d'une passerelle NAT (kube-ovn avant 1.16.1) | Console seulement | 1.66 | propre à la console |

## Sauvegardes et instantanés

Menu Harvester : *Backup & Snapshots*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Planifications (créer, modifier, suspendre, reprendre, supprimer) | Fait | 1.68 |  |
| Sauvegardes : restaurer en nouvelle VM ou remplacer | Fait | 1.58 |  |
| Remplacer en supprimant les anciens volumes ; délai de gel du système de fichiers | Fait | 1.68 | gel proposé sans « 0s » (gel sans limite : Harvester n'appelle jamais le dégel) ; ignoré avant Harvester 1.9 |
| Instantanés de VM : restaurer, supprimer | Fait | 1.58 |  |
| Instantanés de volume : restaurer, supprimer | Fait | 1.58 |  |
| Cible de sauvegarde (NFS, S3) | Fait | 1.67 | formulaire NFS ou S3, test de connexion, retrait |
| YAML des sauvegardes et planifications | Fait | 1.60 |  |

## Monitoring et logging

Menu Harvester : *Monitoring & Logging*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Activer rancher-monitoring / rancher-logging et leur configuration | Fait | 1.57 | par les add-ons |
| Configurations Alertmanager (récepteurs) | Fait | 1.70 | webhook, Slack, e-mail, PagerDuty, Opsgenie, Teams ; route et filtres ; secrets saisis devenus Secrets ; livraison vérifiée en réel |
| Flows, cluster flows, outputs, cluster outputs | Fait | 1.70 | journaux, audit, événements ; 11 cibles ; sortie utilisée protégée |
| Configuration de fluentd refusée dite à l'enregistrement | Console seulement | 1.70 | Harvester enregistre et fluentd garde l'ancienne configuration sans rien dire |

## Avancé

Menu Harvester : *Advanced*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Templates : versions, par défaut, lancer une version, supprimer | Fait | 1.64 |  |
| Clés SSH (créer, lire depuis un fichier, modifier, supprimer) | Fait | 1.64 |  |
| Modèles de configuration cloud (user / network data) | Fait | 1.64 |  |
| Classes de stockage (Longhorn v1, chiffrement, topologies, LVM, par défaut, supprimer) | Partiel | 1.74 | LVM vérifié en réel en 1.74 (add-on expérimental, disque de réserve) ; Longhorn v2 proposé mais pas vérifié (absent des bancs) |
| Périphériques PCI | Fait | 1.68 | activer ou désactiver le passthrough, par sélection ; groupe IOMMU et VMs qui s'en servent dits |
| SR-IOV réseau (nombre de VF) | Fait | 1.68 | carte igb émulée sur le banc |
| GPU SR-IOV, vGPU, configurations MIG | Manquant |  | non livré : aucun GPU compatible sous Harvester sur les bancs, rien ne peut y être essayé en réel |
| Périphériques USB (passthrough) | Fait | 1.68 |  |
| Add-ons : activer, désactiver, configurer | Fait | 1.57 |  |
| Secrets (Opaque, TLS, Basic, Registry, SSH, chiffrement : créer, nouvelles valeurs, supprimer) | Fait | 1.64 |  |
| Réglages de Harvester (les 40 : NTP, proxy, CA, TLS, overcommit…) | Fait | 1.67 | formulaires typés, contrôlés d'avance comme le webhook, état appliqué, secrets masqués, avertissement des réglages qui coupent un accès |

## Mise à jour de Harvester

Menu Harvester : *Upgrade*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Mettre à jour (version, notes, suivi par nœud, journaux) | Fait | 1.69 | éligibilité dite avant le téléchargement de l'ISO, suivi qui tient à la coupure de l'API, abandon tant que Harvester l'accepte |
| Mise à jour airgap (image envoyée) | Fait | 1.69 | ISO du magasin de la console, SHA-512 vérifié, servi par le guichet de la console |

## Support

Menu Harvester : *Support*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Bundle de support | Fait | 1.67 | celui de Harvester (créer, suivre, télécharger, supprimer) et celui de la console (anonymisé) |
| Télécharger le kubeconfig du cluster | Fait | 1.67 | plus sûr que celui de Harvester : rôle et namespace choisis, jeton qui expire, révocation, fichier remis une seule fois |
| Accès aux interfaces Rancher et Longhorn embarquées | Hors périmètre |  | hors périmètre : la console couvre ces vues |

## Utilisateurs et accès

Menu Harvester : *Authentication / Rancher*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Connexion par Rancher (ses fournisseurs d'identité), droits de la personne | Fait | 1.50 | comme Harvester importé dans Rancher (Virtualization Management) : les appels partent avec le jeton de la personne, les droits Rancher du cluster et du projet s'appliquent |
| Rôles de virtualisation (chart Harvester RBAC, Rancher 2.14.1, expérimental) | Partiel | 1.50 | appliqués d'office par le jeton Rancher ; vérifié avec un membre du cluster, pas encore avec les rôles de ce chart |
| Projets Rancher : namespaces rangés par projet, quotas de ressources | Fait | 1.72 | créer, modifier, supprimer un projet, déplacer un namespace, quota du namespace, limite par défaut des VMs ; par Rancher avec le jeton de la personne |
| Annotations de projet d'un autre cluster signalées | Console seulement | 1.72 | Harvester montre ces namespaces hors projet sans dire pourquoi |
| Membres du cluster et des projets | Fait | 1.73 | comptes du cluster (1.31) et membres Rancher : chercher un utilisateur ou un groupe, donner ou retirer un rôle, par Rancher avec le jeton de la personne |
| Connexion obligatoire, comptes et rôles propres à la console | Console seulement | 1.57 | sans Rancher ; Harvester seul n'a qu'un compte admin |

## Menus apportés par des add-ons

Menu Harvester : *VM Imports / VM Migration*

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Imports de VM (VMware, OpenStack, OVA) | Fait | 1.71 | OVA importée de bout en bout en réel ; source VMware vérifiée contre vcsim, puis contre le vrai vCenter du banc vmwlab (1.75) ; l'import réel d'une VM depuis VMware n'est pas encore fait, ni celui depuis OpenStack (pas d'OpenStack sur les bancs) |
| Raison d'un import ou d'une source bloqués, tirée du journal du contrôleur ; noms refusés avant d'écrire | Console seulement | 1.71 | Harvester boucle sans rien dire (image au nom trop long, identifiants refusés) |
| Migration par forklift-operator | Fait | 1.76 | onglet Migrations VMware : Forklift installé, importeur CDI amont (harvester#11773), image VDDK, sources vCenter, inventaire ; vagues à chaud (bascule immédiate ou planifiée, retour à la source, clôture) ; l'assistant de Harvester force warm: false |
| Toutes les migrations VMware de tous les clusters dans une vue | Console seulement | 1.76 | une VM déjà prise dans une vague d'un autre cluster est refusée |

## Ce que la console ajoute

| Fonction | Statut | Version | Précision |
|---|---|---|---|
| Connexion par plusieurs Rancher réglés dans l'interface, directe ou SSO, droits hérités | Console seulement | 1.79 | la console s'enregistre elle-même dans Rancher pour le SSO et installe le chart Harvester RBAC |
| Plusieurs clusters dans une seule interface, sans Rancher | Console seulement | 1.1 | Rancher (Virtualization Management) réunit aussi plusieurs clusters Harvester ; la console le fait seule, et ajoute les gestes d'un cluster à l'autre |
| Arrêt et démarrage gracieux d'un cluster entier | Console seulement | 1.0 | Harvester le décrit comme une procédure manuelle |
| Transfert de VM entre clusters, export / import | Console seulement | 1.45 |  |
| Terraform : déclarations gardées par la console | Console seulement | 1.54 |  |
| Clusters RKE2 par Cluster API, services par CAAPH | Console seulement | 1.48 | Rancher les crée aussi, par son pilote de nœud Harvester ; la console le fait sans Rancher |
| Installation bare-metal de Harvester par Redfish | Console seulement | 1.19 | configuration complète de l'installeur, import d'un fichier existant et aperçu (1.77) ; découverte des disques, rôles, pools, cluster déclaré tout seul (1.78) |
| Activité : chaque geste suivi, dock des actions | Console seulement | 1.6 |  |
| Notes collaboratives sur les VMs et nœuds | Console seulement | 1.4 |  |
| Allouable Longhorn réel par classe | Console seulement | 1.29 |  |
| Cinq langues | Console seulement | 1.10 | l'extension Harvester est en anglais |

