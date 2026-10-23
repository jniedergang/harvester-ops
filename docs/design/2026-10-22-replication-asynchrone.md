# Réplication asynchrone de VMs entre clusters (reprise après sinistre)

Statut : conception, essais faits les 22 et 23/10/2026 sur harvlab et harvlab2.
Maquette cliquable : `2026-10-22-replication-maquette.html` (données fictives).

## Besoin

Répliquer une VM d'un cluster Harvester vers un autre, en asynchrone :

- RPO et RTO les plus courts possibles ;
- pouvoir tester la reprise ;
- journaux et supervision des synchros : débit entre clusters, statistiques de réplication, prévision des temps ;
- adapter la configuration du système invité au cluster de secours, et lancer des actions annexes à la reprise ;
- une mise en place guidée : choix du réseau, du stockage, des images.

Harvester sait sauvegarder et restaurer une VM. Il n'a ni plan de reprise, ni suivi du RPO, ni bascule orchestrée. C'est le rôle de la console multi-cluster.

## Mécanismes écartés

**Forklift (Harvester vers Harvester).** Son adaptateur pour une source KubeVirt (`pkg/controller/plan/adapter/ocp`) répond `WarmMigration() = false`. Le froid passe par `VirtualMachineExport`, que Harvester 1.8 et 1.9 désactivent. Le « live » exige une source vivante. Forklift déplace une VM une fois, il ne la réplique pas. Il reste pertinent pour une source VMware : une migration à chaud sans bascule refait une copie CBT à chaque intervalle. Ce cas est reporté.

**Restauration à froid (moteur de transfert actuel).** Elle recopie tout le disque au moment de la bascule : RTO de plusieurs heures pour de gros volumes. Elle reste le repli pour un volume non éligible.

**OpenVixDiskLib (cloudbase).** vSphere 8 seulement, pas de CBT, licence floue. En veille.

## Mécanisme retenu : volumes de secours Longhorn

1. Côté source, une tâche Longhorn récurrente (`RecurringJob`, `task: backup`) sauvegarde chaque volume de la VM vers une cible commune aux deux clusters.
2. Côté secours, un volume Longhorn en attente (`spec.Standby: true`, `spec.fromBackup`) se restaure de façon incrémentale à chaque nouvelle sauvegarde.
3. La console déclenche la relecture de la cible côté secours dès qu'une sauvegarde se termine (`BackupVolume.spec.syncRequestedAt`).
4. À la bascule : dernière relecture, activation (`spec.Standby: false`, `spec.frontend: blockdev`, comme le fait `VolumeManager.Activate` de longhorn-manager), PV statiques (`driver.longhorn.io`, `volumeHandle` = nom du volume), PVC, puis VM recréée par la console.

La console recrée la VM elle-même au lieu de passer par une restauration Harvester. Elle peut donc changer réseau, MAC, classe de stockage, CPU et mémoire : le webhook de restauration, qui exige les réseaux d'origine, n'intervient pas.

## RPO et RTO

Le RPO n'est pas l'intervalle de sauvegarde. Le RPO réel est l'âge du dernier point de reprise **appliqué** côté secours :

RPO ≈ intervalle + envoi des modifications + application côté secours

- Longhorn envoie des blocs de 2 Mio, compressés en lz4. Une petite écriture salit un bloc entier.
- Deux trajets : source -> cible, puis cible -> secours. Le plus lent décide.
- Si le volume modifié pendant un intervalle met plus d'un intervalle à passer, le RPO dérive. C'est le cas à détecter en premier.
- La latence ne ralentit pas les écritures de la VM. Elle réduit le débit utile vers la cible.

Dans l'interface, l'exploitant saisit un **objectif** de RPO. La console calcule l'intervalle et le RPO atteignable à partir de mesures, et dit s'il est tenable.

Le RTO, lui, ne dépend pas de la taille des disques : activation et création de la VM.

## Ce que l'essai a montré (22/10/2026)

Banc : harvlab (Harvester v1.8.2, Longhorn 1.11.2, 3 nœuds) vers harvlab2 (v1.9.0, Longhorn 1.12.1, 1 nœud), cible NFS du NAS. VM cirros de test, un disque système issu d'une image et un disque de données de 2 Gio ; un script y écrit un compteur horodaté et 8 Mio aléatoires par minute. Sauvegarde Longhorn toutes les 3 minutes.

| Point | Résultat |
|---|---|
| Tâche Longhorn sur un volume de Harvester | Gardée par Harvester (étiquette `recurring-job.longhorn.io/<tâche>` intacte) |
| Volume modifié envoyé | 25 174 351 octets par sauvegarde, soit exactement 3 x 8 Mio (données aléatoires, non compressibles). Disque système : 0 |
| Durée d'une sauvegarde de 25 Mo | Environ 5 s |
| Volume de secours avec backing image | Accepté. La backing image existait déjà sur harvlab2 (restaurée lors d'un transfert précédent) |
| Restauration initiale | Quelques secondes pour 60 Mo |
| Application côté secours, sans intervention | 1 à 3 min après la fin de la sauvegarde (relecture de la cible) |
| Application avec relecture déclenchée par la console | **9 à 10 s** |
| Activation des deux volumes | 13 s, relecture finale comprise |
| VM démarrée sur le cluster de secours | **43 s** après le début de la bascule, sur `default/lab-net` au lieu du réseau de pods, nouvelle MAC |
| Données reprises | Compteur écrit à 13:53:53, dernière sauvegarde à 13:54:06, bascule à 13:54:44 : moins d'une minute perdue |
| cloud-init rejoué | Oui, marque `REPRISE` écrite au démarrage |

**cloud-init.** Pour une source NoCloud, KubeVirt prend comme `instance-id` le `firmware.uuid` de la VM (`cloudInitUUIDFromVMI`). Recopier l'UUID de la source : cloud-init ne se rejoue pas. Laisser un nouvel UUID : cloud-init se rejoue, applique la nouvelle configuration, et régénère aussi les clés d'hôte SSH. L'interface doit le dire.

## Essais complémentaires (23/10/2026)

**Écritures dispersées.** Disque de 2 Gio rempli de données aléatoires, puis 200 écritures de 4 Kio par minute à des adresses aléatoires.

| Mesure, par intervalle de 3 min | Valeur |
|---|---|
| Données écrites | 2 457 600 octets (600 x 4 Kio) |
| Taille de l'instantané Longhorn | 2,47 Mo (à 1 % près ce qui est écrit) |
| Données envoyées par la sauvegarde | 944 à 960 Mo (450 à 458 blocs de 2 Mio), 14 s |
| Prévision par blocs distincts : 1024 x (1 - (1023/1024)^600) | 454 blocs, 952,6 Mo |

La taille d'un instantané sous-estime l'envoi d'un facteur pouvant aller jusqu'à 512. La phase de mesure compte donc des **blocs de 2 Mio distincts** : par la valeur `newlyUploadDataSize` de vraies sauvegardes (la mesure de référence), ou par la formule N x (1 - (1 - 1/N)^écritures) quand on ne connaît que le nombre d'écritures. Première sauvegarde complète du disque : 30 s, environ 72 Mo/s vers le NAS.

**Retour au site principal.** Bascule vers harvlab2 en 23 s (source arrêtée, volumes activés, VM démarrée), environ 1,5 min d'écritures perdues côté source (après sa dernière sauvegarde). Réplication inverse, puis retour sur harvlab en **67 s, sans perte** : arrêt de la VM, instantané et sauvegarde finale, application, activation, VM démarrée. Le compteur lu sur le disque continue sans trou d'un site à l'autre.

- **Reprotéger coûte une copie complète, dans les deux sens.** Le volume activé porte un nouveau nom : ses sauvegardes ouvrent un nouveau `BackupVolume`, et l'incrémental ne traverse jamais deux `BackupVolume`. Les sauvegardes suivantes redeviennent incrémentales.
- **Ce que l'ancien site a écrit après sa dernière sauvegarde est perdu**, et le retour l'écrase. L'interface doit le dire avant un retour.
- **Bascule planifiée** (les deux sites vivants) : arrêter la VM, puis instantané et sauvegarde finale avant d'activer. C'est ce qui donne zéro perte.

**Backing image absente côté secours.** Le webhook de Longhorn refuse le volume de secours (`failed to get backing image ...`). Mais Longhorn sauvegarde déjà la backing image avec la première sauvegarde du volume (`BackupBackingImage` créé tout seul). La console crée côté secours une `BackingImage` en `sourceType: restore` (paramètres `backup-target-name`, `backup-url`), prête en 11 s pour 21,7 Mo, puis le volume de secours. Harvester ne se plaint pas d'une backing image sans `VirtualMachineImage`. Pour démarrer une VM sur ce disque, il faudra en plus faire correspondre la classe de stockage de l'image (bloc 3 de la maquette).

## Pièges et points encore ouverts

- **L'activation est définitive.** Un volume activé redevient un volume normal. Pour réarmer, il faut recréer le volume de secours, donc tout recopier. D'où deux exercices : l'exercice courant restaure une copie séparée depuis la même sauvegarde sur un réseau isolé ; l'exercice complet (activation puis réarmement) mesure le vrai RTO, rarement.
- **Un volume de secours occupe sa taille dès sa création** sur le cluster de secours, pas seulement le jour de la bascule.
- **La relecture naturelle de la cible coûte 1 à 3 min de RPO.** La console doit déclencher la relecture à la fin de chaque sauvegarde.
- **Une tâche récurrente supprime les instantanés précédents.** Pendant la réplication, la mesure exacte est `newlyUploadDataSize` de chaque `Backup`.
- **Volumes Longhorn v2 non couverts.**
- **Cohérence** : la sauvegarde Longhorn récurrente est cohérente comme après une coupure de courant. Le gel par l'agent invité de Harvester ne s'applique qu'à ses propres sauvegardes.
- **La cible ne doit être sur aucun des deux sites**, et la console doit tourner du côté du secours ou à l'extérieur.
- **Santé du banc** : pendant les essais, le disque Longhorn de harvlab-n3 était indisponible (volumes à 2 répliques sur 3). Sans effet sur les mesures, à réparer sur le banc.

## Interface (voir la maquette)

- Section de menu **Protection**, au niveau de Shared resources : Plans de réplication, Exercices et bascules, Liens entre clusters. Entrée « Protéger... » dans le menu d'actions d'une VM.
- Un **plan** = un groupe de VMs (sélection ou étiquette), car vagues, exercices et bascules se raisonnent par application.
- Fenêtre de création à six blocs visibles :
  1. quoi et vers où ;
  2. objectif (RPO objectif, phase de mesure, RPO atteignable, durée de la première copie) ;
  3. correspondances réseau, stockage, image, namespace, avec « Créer sur la cible » et un réseau d'exercice ;
  4. reprise par vagues, cloud-init rejoué ou non, appels HTTP annexes ;
  5. exercices ;
  6. récapitulatif avec la commande CLI équivalente.
- Tout ce qui touche le cluster de secours est validé par un dry-run serveur.
- Vue du plan : âge du dernier point appliqué comparé à l'objectif, débit, prochaine synchro, arriéré ; boutons Exercice, Basculer, Retour.

## Étapes suivantes

1. Découper la réalisation : moteur CLI (`bin/harvester-replication.py`), puis vue de suivi, puis fenêtre de création, puis exercices et bascule.
