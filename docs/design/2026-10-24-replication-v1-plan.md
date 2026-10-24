# Réplication asynchrone 1.97.0 : moteur et ligne de commande (plan de réalisation)

Conception et résultats des bancs : `2026-10-22-replication-asynchrone.md`. Maquette (versions suivantes) : `2026-10-22-replication-maquette.html`.

Cette version livre le moteur de réplication et sa ligne de commande `bin/harvester-replication.py`, le suiveur (boucle en ligne de commande et fil de la console), et les routes de la console qui lancent les commandes en actions suivies. Elle ne livre aucun écran : la vue de suivi est la version suivante, la fenêtre de création celle d'après, puis les exercices et le retour au site principal.

## Périmètre retenu, et pourquoi

| Dans la 1.97.0 | Hors 1.97.0 | Raison |
|---|---|---|
| Plan gardé par la console, mesure, protection, suiveur, état, bascule planifiée et non planifiée, déprotection | Exercices, retour au site principal (reprotection), vagues, appels HTTP annexes, cohérence par gel de l'invité | Le moteur doit d'abord tenir son RPO en continu et basculer sans perte sur le banc. Les exercices et le retour réutilisent les mêmes briques (volume de secours, activation, PV/PVC, carnet de reprise) et s'ajoutent sans changer le modèle |
| Routes de la console, sans écran | Vue de suivi, fenêtre de création | Les routes donnent dès maintenant l'audit dans Activité et les rôles ; la vue se branche dessus ensuite |
| Volumes Longhorn v1, disques de VM Harvester (bloc, `rwx` + `migratable`) et `rwo` | Longhorn v2, volumes chiffrés, `rwx` non migratable (share-manager) | Non vérifiés sur le banc : refusés par le contrôle, pas livrés en silence |
| Une cible de sauvegarde commune déjà configurée des deux côtés | Création ou changement de cible par la console | La cible est réglée par Harvester ; la console ne fait que vérifier qu'elle est la même et disponible |

## Contraintes communes

- Python 3.11, **bibliothèque standard seulement** dans `bin/` ; les clusters sont lus et écrits par `bin/lib/kube.py` (kubectl), avec l'interface déjà utilisée par le transfert (`get`, `list`, `create`, `replace`, `patch`, `delete`). Ajout à `Kube` : un paramètre `dry_run=False` sur `create` et `patch` (`--dry-run=server`), sans changer les appelants existants.
- Clusters désignés par leur nom (`cluster_config()` : configuration puis déclarations de la console, `bin/lib/cluster_decl.py`), ou par `--from-kubeconfig` / `--to-kubeconfig` pour la console qui passe le kubeconfig de la personne (`_kubectl_for_cluster`). Le plan ne garde que des noms de cluster, jamais de chemin.
- Réutiliser le transfert au lieu de recopier : `vm_transfer.sanitize_vm`, `retarget_vm`, `vm_macs`, `vm_inventory`, `normalize_backup_target`, `parse_quantity`, `version_tuple`, `step` ; `vm_transfer_run.wait_for`, `Fail`, `transient`, `stop_vm`, `start_vm`, `ensure_namespace`, `vmi_phase`, `delete_quiet` ; `longhorn_room.storage_room` pour la place. Toute constante Longhorn ou Harvester recopiée cite sa source (fichier et version de longhorn-manager ou de Harvester).
- Ne jamais fabriquer un nom d'objet dérivé : moteurs Longhorn trouvés par l'étiquette `longhornvolume=<volume>` (pas `<volume>-e-0`), `BackupVolume` par `spec.volumeName` et `spec.backupTargetName`, classe d'une image par son `status.storageClassName`.
- Protocole des scripts : progression sur stderr en `STEP_EVENT|<id>|<status>|<message>`, débit en `PROGRESS_EVENT|<id>|<json>` (format de `vm_transfer_progress`), sortie `--json` sur stdout, demande complète possible par `--spec fichier.json` (comme `_cli_action`). Codes de sortie, alignés sur le transfert : 0 réussite, 1 échec, 2 refus du contrôle préalable ou usage, 3 annulation, 4 objectif de RPO dépassé (`status` seulement, pour une sonde).
- **Toute attente est bornée et réglable** : table `TIMEOUTS` du module de déroulé, surchargée par `--timeout NOM=SECONDES` (répétable) et, pour le suiveur, par les réglages du plan en base (modifiables à chaud). Valeurs de départ : `probe` 15, `stop` 600, `snapshot` 300, `final_backup` 6 h, `resync_apply` 600, `initial_restore` 12 h, `backing_image` 2 h, `activate` 300, `bind` 120, `vm_start` 900, `unplanned_sync` 120, `barrier` la moitié de l'intervalle.
- **Toute commande est rejouable** : chaque étape constate d'abord l'état réel (objet présent avec nos étiquettes et la bonne spec, volume déjà activé, PVC déjà lié) et ne refait que ce qui manque. Une commande relancée après un arrêt brutal converge, sans doublon. SIGTERM ou SIGINT : retour arrière de ce que la commande en cours a créé, sortie 3 (modèle de `harvester-vm-transfer.py`), sauf après une activation, qui est définitive (voir la bascule).
- Une seule commande mutative à la fois par plan : verrou en base (ligne `locks`, prise en `BEGIN IMMEDIATE`, libérée à la sortie, reprise si le processus détenteur n'existe plus sur cet hôte).
- Tout ce que la réplication crée porte `harvester-ops.io/replication-plan=<id>` et `harvester-ops.io/replication-generation=<n>`, plus `harvester-ops.io/replication-vm=<uid de la VM source>` quand l'objet appartient à une VM. Nettoyage et sélection passent par ces étiquettes, jamais par un motif de nom.
- Identifiants en anglais, commentaires en français. Contenu public (docs, CHANGELOG, commits) : ni tiret cadratin ni flèche Unicode, pas de ligne `Co-Authored-By`.
- Sécurité web : `@requires_auth` sur tout, `@_rate_limit("N/minute")` sur toute mutation, identifiant de plan contrôlé par `PLAN_ID_RE`, noms k8s par `_K8S_NAME_RE`, aucune URL de cible avec identifiants, aucun chemin ni secret dans une réponse ou un journal.
- Tests en tableau, un cas par chemin d'erreur, cas à plusieurs VMs et plusieurs disques, au moins une mutation volontaire par fichier pour prouver qu'un test détecte. Puis vérification réelle sur harvlab vers harvlab2 (tâche 7).

## Tâche 1 : registre `replication_db`

Fichiers : `bin/lib/replication_db.py`, `tests/api/test_replication_db_197.py`.

Calqué sur `bin/lib/bookings_db.py` : SQLite en WAL, `replication.db` dans le répertoire d'état (ou `HARVESTER_OPS_REPLICATION_DB`), fichier 0600, `busy_timeout`, schéma en `CREATE TABLE IF NOT EXISTS` (migrations additives seulement). Seule la ligne de commande y écrit ; la console la lit (et son fil de suivi lance la ligne de commande).

Tables :
- `plans` : `id` (`rp-<12 hex>`), `name` (RFC 1123, unique), `source_host_kind` et `target_host_kind` (`harvester` aujourd'hui, `kvm` plus tard), `source_cluster`, `target_cluster` (nom de l'hôte), `state`, `generation` (1 au départ), `active_side` (`source` ou `target`), `rpo_objective_s`, `interval_s`, `cron`, `retain`, `mappings` (JSON : réseaux, classes, namespaces, images), `options` (JSON : `cloud_init` `rerun|keep`, `mac` `keep|new`, `create_namespace`, `start` `started|stopped`, `follow` `console|cli|off`, `paused`, surcharges de délais), `selector` (étiquette facultative), `created_by`, `created_at`, `updated_at`.
- `plan_resources` (une ligne par ressource protégée) : `plan_id`, `generation`, `resource_kind` (`vm` aujourd'hui, `kvm-domain` plus tard), `resource_uid` (uid de la VM source), `source_host`, `target_host`, `namespace`, `name`, `target_namespace`, `target_name`, `vm_generation` (dernière version de la spec vue), `firmware_uuid`, `state` (état propre de la ressource, voir tâche 2), `state_since`. Index sur `source_host` et sur `target_host` : la liste d'un hôte se lit des deux côtés sans parcourir les plans.
- `plan_volumes` : `plan_id`, `generation`, `vm_uid`, `disk` (nom du volume dans la VM), `claim`, `source_volume`, `size`, `access_mode`, `migratable`, `backing_image`, `target_volume`, `target_class`, `target_replicas`, `backup_volume_source`, `backup_volume_target`, `state` (`pending`, `restoring`, `standby`, `activated`, `removed`).
- `ticks` : une ligne par sauvegarde suivie : `plan_id`, `generation`, `source_volume`, `backup`, `snapshot_at`, `backup_started_at`, `backup_done_at`, `newly_uploaded`, `size`, `resync_at`, `applied_at`, `state` (`completed`, `error`, `applied`, `skipped`), `skew_s` (écart avec les autres disques de la VM sur le même passage).
- `measures` : passages de la phase de mesure (même forme que `ticks`, sans cible).
- `events` : `ts`, `plan_id`, `resource_uid` (vide pour un événement du plan entier), `host`, `side` (`source` ou `target`), `operation` (`protect`, `measure`, `follow`, `failover`, `unprotect`, `plan`), `step`, `level`, `code`, `message`, `facts` (JSON), `by`, `action_id` (l'action de la console qui l'a lancé, passée par `--id` : lien vers Activité). **Journal persistant par ressource** : chaque étape de chaque opération du moteur y écrit une ligne pour chaque ressource touchée (début, fin, échec, refus, durée), ainsi que chaque constat du suiveur. C'est l'historique du futur sous-menu, qui va au-delà d'Activité (bornée et non indexée par ressource). Index sur `(resource_uid, ts)` et `(host, ts)`.
- `runs` : bascules et déprotections, avec les points de reprise choisis par volume et la durée de chaque étape (la base des futurs exercices).
- `leases` : `plan_id`, `owner` (`console:<hôte>:<pid>` ou `cli:<hôte>:<pid>`), `heartbeat`.
- `locks` : `plan_id`, `command`, `owner`, `started_at`.

Fonctions : `connect`, `new_plan_id`, `insert_plan`, `get_plan`, `get_plan_by_name`, `list_plans`, `list_resources_by_host(conn, host, side="any")`, `resource_events(conn, resource_uid, limit, before)` (pagination par date), `set_state(conn, plan_id, expected, new)` (refuse une transition inattendue), `upsert_resource`, `set_resource_state`, `upsert_volume`, `record_tick`, `mark_applied`, `record_event`, `take_lease(conn, plan_id, owner, ttl, take_over=False)`, `heartbeat`, `acquire_lock` / `release_lock`, `prune(conn, before)` (ticks et événements au-delà de 400 jours, comme `usage_db`).

États d'un plan : `draft` -> `measuring` -> `protecting` (restauration initiale) -> `protected` -> `failing_over` -> `failed_over` ; `unprotecting` -> `unprotected` ; `error` garde l'état d'origine dans `events`. Chaque commande déclare les états d'où elle part, et reprend depuis son propre état intermédiaire.

Tests : transitions valides et refusées en tableau, unicité du nom, liste par hôte côté source, côté cible et des deux côtés (un hôte source d'un plan et cible d'un autre), événements d'une ressource paginés, bail pris, refusé, repris après expiration et par `take_over`, verrou repris quand le PID n'existe plus, migration additive sur une base créée par le schéma précédent (ajout d'une colonne simulé), `prune`.

## Tâche 2 : logique pure `replication`

Fichiers : `bin/lib/replication.py`, `tests/api/test_replication_model_197.py`.

**Noms.**
- `target_volume_name(plan_id, vm_uid, disk, generation, vm_name)` : `hr-<vm tronqué>-<disque tronqué>-<6 hex>` (empreinte de `plan_id|vm_uid|disk|generation`), minuscules, RFC 1123, déterministe (une relance retrouve le même nom). Longueur bornée à 40 pour laisser la place aux noms dérivés de Longhorn (moteur, répliques) ; la limite exacte est lue dans la validation du nom de volume de longhorn-manager 1.11 et 1.12, ligne citée en commentaire.
- `recurring_job_name(plan_id, vm_uid)` : une tâche par VM (voir cohérence), `hr-<8 hex du plan>-<8 hex de la VM>`.
- PV = nom du volume de secours (comme sur le banc). PVC cible = nom du PVC source, sauf correspondance donnée. VM cible = nom source dans le namespace mappé, sauf correspondance donnée.
- Règles de collision (`collisions(target_facts, plan)`) : un volume, une tâche, une backing image ou un secret qui existe déjà **sans** nos étiquettes de plan, ou avec un autre plan, bloque (`target-volume-taken`, `recurring-job-taken`, `carnet-taken`). Un PVC ou une VM cible déjà présents avertissent à la protection (`target-pvc-taken`, `target-vm-taken`) et bloquent à la bascule.

**Éligibilité** (`volume_findings(lh_volume, pvc, sc)`), par disque :
- `dataEngine: v2` : bloque (`data-engine-v2`).
- volume hors Longhorn (autre CSI, LVM) : bloque (`not-longhorn`).
- `encrypted: true` : bloque (`encrypted-volume`, non vérifié).
- `accessMode: rwx` sans `migratable` (share-manager) : bloque (`rwx-share-manager`, non vérifié). `rwx` + `migratable` (disque de VM Harvester) et `rwo` : acceptés, recopiés tels quels sur le volume de secours.
- disque `containerDisk`, `hostDisk` ou `emptyDisk` : avertit (`disk-not-replicated`, non répliqué, la VM cible le reçoit vide ou sans lui selon le type).
- volume déjà porté par une tâche récurrente du groupe `default` en sauvegarde : avertit (`default-backup-job`, deux tâches écrivent dans le même `BackupVolume`, sans danger mais à savoir).

**Contrôle d'un plan** (`check(src, dst, plan)`, même forme de constats que `vm_transfer.check` : `{"code", "level", "facts"}`, blocages d'abord) :
- cibles de sauvegarde : réglage `backup-target` de Harvester égal des deux côtés (`normalize_backup_target`) et `backuptargets.longhorn.io/default` disponible des deux côtés (`backup-target-differs`, `backup-target-unavailable`) ;
- versions de Longhorn lues des deux côtés (`settings.longhorn.io/current-longhorn-version`) : paires vérifiées sur le banc (1.11.2 vers 1.12.1 et retour) en `ok`, toute autre paire en `warn` (`longhorn-pair-unverified`), une cible plus ancienne que la source en `warn` ;
- correspondances : chaque réseau multus mappé vers un réseau existant de la cible, chaque classe vers une classe existante, namespace présent ou `create_namespace` ;
- **place sur la cible** : les volumes de secours occupent leur taille entière dès leur création. Somme des tailles par classe cible, multipliée par le nombre de répliques de la classe, comparée à l'allouable de `longhorn_room.storage_room` (mode dégradé sur un site mononœud comme dans le transfert) ; bloque (`capacity-short`) ; la backing image à restaurer compte aussi ;
- backing images : même nom présent sur la cible avec la même somme : réutilisée ; même nom, autre somme : bloque (`backing-image-conflict`) ; absente : restaurée (`backing-image-restore`, `ok`) ;
- MAC (`mac: keep`) : collision avec une VM de la cible, par `vm_macs` : `warn` à la protection, bloque à la bascule (`mac-in-use`) ;
- `cloud_init: keep` sans `firmware.uuid` lisible sur la source : bloque (`firmware-uuid-unknown`) ;
- `cloud_init: rerun` : avertit que cloud-init rejoue sa configuration et régénère les clés d'hôte SSH (`cloud-init-rerun`) ;
- périphériques et épinglage retirés par `retarget_vm` : avertit, comme le transfert ;
- connexion à la cible par Rancher : avertit que la bascule dépend de ce Rancher (`target-through-rancher`).

**Intervalle et prévision.**
- `CRON_STEPS` : minutes 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, puis heures 1, 2, 3, 4, 6, 8, 12, 24 (diviseurs de l'heure et du jour : un `*/7` repartirait à zéro chaque heure). `cron_for(interval_s)` rend `*/N * * * *` ou `0 */H * * *`.
- `derive_interval(objective_s, send_p95_s, apply_p95_s, resync_s=10, margin=0.2)` : le plus grand pas tel que `pas + envoi + application + relecture <= objectif` et `envoi + application <= pas x (1 - marge)` (sinon le RPO dérive). Rend `(pas, rpo_atteignable, tenable)` ; intenable : le plus petit pas et son RPO atteignable, avec la raison.
- `blocks_forecast(size, writes, block=2 MiB)` : `N x (1 - (1 - 1/N)^écritures)` avec `N = taille / bloc`, borne haute pour des écritures dispersées ; `bytes_written / bloc` arrondi au-dessus, borne basse pour des écritures séquentielles. Les deux bornes sont rendues. La taille de bloc reste un paramètre : 2 Mio relevés sur les deux versions du banc ; si la version du cluster source expose une taille de bloc de sauvegarde par volume, elle est lue dans sa source et utilisée.
- `aggregate_upper(measures, factor)` : estimation pour un pas multiple du pas mesuré, par somme des envois des passages consécutifs (l'union des blocs salis est au plus la somme : la mesure au petit pas majore les grands pas).
- `first_copy_eta(used_bytes, throughput)` : durée de la première copie, inconnue tant qu'aucune sauvegarde n'a donné de débit.

**État** (`plan_status(plan, resources, volumes, ticks, now, vm_running)`) :
- RPO tenu d'une VM = âge du point appliqué **le plus ancien** de ses disques (`now - snapshot_at` du dernier tick appliqué). Une VM arrêtée depuis avant ce point : `à jour` (rien n'a changé), pas de dépassement.
- arriéré : sauvegardes terminées côté source, pas encore appliquées, plus celle en cours ; dérive (`rpo-drift`) quand un passage a mis plus d'un pas entre la prise de l'instantané et l'application ;
- débits des deux trajets, séparés : source vers cible (`newly_uploaded / durée de sauvegarde`) et cible vers secours (durée d'application) ; écart entre disques d'une VM (`skew_s`) ; prochain passage (prochaine échéance du cron).

**État et actions d'une ressource** (`resource_state(plan, resource, volumes, ticks, now, vm_running)` et `allowed_actions(plan, resource, side)`), source unique pour la ligne de commande, la console et le futur sous-menu :
- états : `draft`, `measuring`, `protecting` (première copie), `protected`, `lagging` (RPO tenu au-delà de l'objectif), `drifting` (un passage plus long qu'un pas, ou spec changée sans `protect`), `degraded` (volume de secours dégradé, sauvegarde en erreur), `source-unreachable`, `failing_over`, `failed_over`, `unprotecting`, `unprotected`, `error` ;
- actions rendues selon l'état et le côté d'où l'on regarde, chacune avec le rôle requis : `measure-start`, `measure-stop`, `protect`, `failover` et `failover-unplanned` (côté cible, ou source injoignable), `unprotect`, `unprotect-delete-backups`, `pause`, `resume`. Une action pas encore livrée (`drill`, `failback`) n'est pas rendue : elle s'ajoutera dans cette seule fonction ;
- `plan_actions(plan, side)` : les actions d'un plan entier (protéger, basculer toutes ses VMs, déprotéger, pause), dérivées des états de ses ressources ;
- `plan_row()` et `resource_row()` : la forme unique rendue par la liste des plans, la liste par hôte et la ligne de commande ;
- tests en tableau : chaque état, vu de la source et de la cible, avec ses actions et leurs rôles ; un plan aux ressources dans des états mêlés.

**Manifestes** (purs, testés à l'octet près contre les objets du banc, `/tmp/drspike2/*.yaml`) :
- `recurring_job(name, cron, retain, concurrency, plan_id, vm_uid)` : `task: backup`, `groups: []`, `labels` portant le plan et la VM (pour retrouver nos sauvegardes), `retain` du plan (6 par défaut, entre 3 et 50 : point appliqué, point en route, une marge) ;
- étiquette de volume `recurring-job.longhorn.io/<tâche>: enabled` (gardée par Harvester, vérifié sur le banc) ;
- `backing_image_restore(name, checksum, backup_url, target_name)` : `sourceType: restore`, paramètres `backup-target-name`, `backup-url`, `concurrent-limit` ;
- `standby_volume(name, from_backup, size, access_mode, migratable, replicas, backing_image, class_params)` : `Standby: true`, `frontend: ""`, `dataEngine: v1`, paramètres de placement recopiés de la classe cible (`numberOfReplicas`, `dataLocality`, `diskSelector`, `nodeSelector`, `staleReplicaTimeout`) ;
- `snapshot(volume, name)` et `backup(volume, snapshot, backup_volume, target)` pour la sauvegarde finale ;
- `activation_patch()` = `{"spec": {"Standby": false, "frontend": "blockdev"}}` (comme `VolumeManager.Activate`, source citée) ;
- `static_pv(volume, size, mode, access_modes, storage_class)` (`driver.longhorn.io`, `volumeHandle` = volume, `Retain`) et `static_pvc(...)` (`volumeName`), l'annotation `harvesterhci.io/imageId` posée seulement si l'image est mappée sur une image de la cible ;
- `carnet(plan, vm, volumes, secrets)` : voir tâche 3.

**VM cible** (`target_vm(source_vm, resource, mappings, options, claims)`) : `sanitize_vm` puis `retarget_vm` (réseaux, PVC, secrets cloud-init, MAC selon `mac`), puis `firmware.uuid` recopié (`keep`) ou retiré (`rerun`, KubeVirt prend alors un nouvel `instance-id` : `cloudInitUUIDFromVMI`), `runStrategy` selon `start`.

Tests (en tableau) : noms (déterministes, longueurs limites, caractères interdits, collision d'empreinte simulée) ; chaque constat d'éligibilité et de contrôle, un cas par code, plan à deux VMs et trois disques ; `derive_interval` sur une grille (objectif 5 min, 15 min, 1 h, intenable) ; `blocks_forecast` avec les nombres du banc (600 écritures sur 2 Gio : 454 blocs, 952,6 Mo ; 3 x 8 Mio séquentiels : 12 blocs) ; `aggregate_upper` ; `plan_status` (VM arrêtée, disque en retard, dérive, aucun tick) ; manifestes comparés aux objets du banc ; `target_vm` en `keep` et `rerun`.

## Tâche 3 : déroulé `replication_run`

Fichiers : `bin/lib/replication_run.py`, `tests/api/test_replication_run_197.py`.

`Ctx` compatible avec `vm_transfer_run.wait_for` (`emit`, `sleep`, `now`, `timeouts`, `created`, `record`, `forget`), plus `db` et `plan`. Faux cluster : celui de `tests/api/test_vm_transfer_run.py` (`Clock`, `Store`, `FakeCluster`), étendu d'un **Longhorn simulé** : une `Backup` passe `Completed` quand l'horloge avance, une relecture (`syncRequestedAt`) fait apparaître la sauvegarde côté secours, le moteur du volume de secours avance son `lastRestoredBackup`, l'activation passe le volume `detached` non `Standby`. Le faux sait aussi échouer à une étape choisie (erreur, délai, coupure transitoire).

**`measure_start(ctx, interval)`** (`draft` -> `measuring`) : tâches récurrentes de la phase de mesure posées sur les volumes source, **sans rien créer sur la cible** (aucune place consommée). Pas de mesure par défaut 5 min (1 min permis). La première sauvegarde est complète et donne le débit vers la cible ; elle n'est pas perdue : le `BackupVolume` est le même que celui de la protection, qui part donc en incrémental. `measure_stop` retire les tâches. `measure_report` : par pas candidat (multiples du pas mesuré), données envoyées (moyenne, p95, majorant), durée d'envoi, RPO atteignable, tenable ou non, intervalle conseillé pour l'objectif du plan. Le temps d'application côté secours n'existe pas sans volume de secours : il est pris égal au temps d'envoi au même débit, hypothèse écrite dans le rapport, remplacée par la mesure réelle dès la protection.

**`measure formula`** (sans cluster) : taille, nombre d'écritures, pas, débit facultatif ; rend les deux bornes et le RPO atteignable. Pour le cas où l'on ne connaît que des compteurs d'écritures.

**`protect(ctx)`** (`draft`, `measuring` ou `protecting` -> `protecting` -> `protected`) :
1. Relevés et `check` ; un blocage arrête avant toute écriture (sortie 2). `--dry-run` : contrôle, puis dry-run serveur de chaque objet cible (volumes de secours, backing images, carnet) ; le webhook de Longhorn y signale notamment une backing image absente.
2. Intervalle dérivé de l'objectif et des mesures s'il y en a, sinon pas donné par `--interval` (obligatoire sans mesure, avec avertissement).
3. Source : une tâche récurrente par VM, `concurrency` = nombre de disques de la VM ; étiquette de tâche sur chaque volume. Si une tâche de mesure existe, elle devient la tâche du plan (cron changé, pas de seconde tâche).
4. Attente de la première sauvegarde terminée de chaque volume (déclenchée tout de suite par un `Backup` manuel portant les étiquettes du plan, pour ne pas attendre l'échéance du cron).
5. Cible : relecture forcée des `BackupVolume` concernés ; backing images restaurées si absentes (`BackupBackingImage` créé par Longhorn avec la première sauvegarde ; attente `backing_image`) ; volumes de secours créés depuis la dernière sauvegarde (`fromBackup` = URL de cette sauvegarde, lue sur l'objet `Backup` synchronisé côté secours) ; attente de la restauration initiale, avec progression (`PROGRESS_EVENT`, octets restaurés du moteur).
6. Carnet de reprise écrit sur la cible : namespace `harvester-ops-replication`, un `Secret` par VM (`hr-<plan>-<vm>`, étiqueté) avec `vm.json` (VM cible déjà réécrite), `secrets.json` (les secrets cloud-init de la source, recopiés), `volumes.json` (volumes de secours, PVC, classes, tailles, modes, image). La bascule n'a besoin que de la cible et de ce carnet : elle reste possible si la source est perdue, et même si la console a perdu sa base (relecture du carnet). Les secrets ne sont jamais stockés dans `replication.db`.
7. État `protected`, événement d'audit.

Rejouable : chaque sous-étape constate l'existant (tâche et cron, étiquette, backing image, volume de secours avec le même `fromBackup` de `BackupVolume`, carnet à jour par empreinte). Annulation : retour arrière de ce que cette exécution a créé ; ce qui existait avant reste.

**`failover(ctx, vms, planned)`** (`protected` ou `failing_over` -> `failing_over` -> `failed_over`) :

Bascule planifiée (les deux sites joignables ; c'est elle qui donne zéro perte, 67 s sur le banc) :
1. Sondes (`probe`) ; la source doit répondre, sinon refus avec l'indication `--unplanned`.
2. Suiveur mis en pause pour ce plan (option `paused` en base, lue à chaud par le suiveur).
3. VM source arrêtée (`stop_vm`, attente `stop`), étiquette de tâche retirée de ses volumes.
4. Instantané et sauvegarde finale de chaque disque (`Snapshot` puis `Backup` étiquetés), attente `final_backup`. Tous les disques sont figés VM arrêtée : point cohérent pour la VM entière.
5. Relecture forcée côté secours, attente que chaque moteur ait appliqué **cette** sauvegarde (`resync_apply`).
6. Avant activation, tout est réversible : une erreur ou une annulation redémarre la VM source et remet l'étiquette de tâche.
7. Activation (`activation_patch`), attente `isStandby == false` et `detached` (`activate`). À partir d'ici, **pas de retour arrière** : une erreur ou une annulation s'arrête sur une étape propre, sortie 1 ou 3, avec le message « relancer failover pour terminer » ; la relance reprend à l'étape suivante.
8. Namespace (si `create_namespace`), secrets cloud-init depuis le carnet, PV puis PVC, attente `Bound` (`bind`).
9. VM créée depuis le carnet (MAC contrôlée juste avant), démarrée selon `start`, attente `Running` (`vm_start`).
10. État `failed_over`, ligne `runs` avec les points de reprise et la durée de chaque étape.

Une relance après une activation ne demande que la cible : l'état de la source ne bloque plus, et le mode, les VMs et `--force-unplanned` de l'exécution d'origine sont repris.

Bascule non planifiée (`--unplanned`, exige `--yes`) :
- la source ne répond pas à la sonde ; si elle répond, refus sauf `--force-unplanned` (cas d'une source joignable mais inutilisable) ;
- avec `--force-unplanned` et une source vivante, la source est d'abord clôturée (VM arrêtée, étiquette de tâche retirée, tâche de la VM supprimée), avant toute lecture des points ;
- relecture finale (`unplanned_sync`), puis attente que chaque moteur ait appliqué la dernière sauvegarde connue de son volume (`lastRestoredBackup == status.lastBackup`, délai `unplanned_catchup`), puis activation ; les points rendus (écart, `lost_after`) sont relus après l'activation ;
- par VM, l'écart entre les points appliqués de ses disques est calculé et rendu ; au-delà de `skew_tolerance` (10 s par défaut), la bascule de cette VM demande `--accept-skew` ;
- si la source répond pendant la bascule, arrêt de la VM source tenté au mieux, résultat écrit (`source-fenced` ou `source-not-fenced`) ;
- le résultat dit ce qui est perdu : les écritures postérieures au dernier point appliqué.

`--vm` limite la bascule à une partie du plan (base des vagues). `--dry-run` : contrôles et dry-run serveur des PV, PVC et VM cibles.

**`unprotect(ctx, delete_backups)`** (tout état sauf `failing_over` -> `unprotecting` -> `unprotected`) : étiquettes de tâche retirées des volumes source, tâches supprimées, volumes de secours non activés supprimés, carnet supprimé, backing images restaurées par le plan supprimées si aucun volume ne les utilise. `--delete-backups` (exige `--yes`) supprime les objets `Backup` **portant les étiquettes du plan** et eux seuls ; jamais un `BackupVolume`, qui porte aussi les sauvegardes Harvester de la VM. Source injoignable : la partie cible se fait, la partie source reste notée « à faire » et se termine à la relance. Une VM dont la bascule a déjà activé un volume (même un seul disque) garde tout sur la cible : volumes, secours restants compris, PV, PVC, VM, secrets, backing images, et son carnet tant que la VM cible n'existe pas ; tout est listé dans `left`, avec un sélecteur d'étiquette par genre. Tant qu'une telle VM existe, `--delete-backups` est refusé. La suppression de ces objets est un geste séparé et explicite (décision 47).

Tests : protection complète sur un plan à deux VMs (une à trois disques, une avec backing image absente côté secours) ; **relance après un arrêt brutal à chaque étape** (paramétré sur l'indice de l'étape interrompue : aucun doublon, état final identique) ; annulation avant et après activation ; chaque délai dépassé (horloge simulée) ; coupure transitoire de l'API pendant une attente ; bascule planifiée (ordre exact des appels, sauvegarde finale appliquée avant activation) ; bascule non planifiée (source injoignable, écart entre disques, `--accept-skew`, source qui revient pendant la bascule) ; MAC prise ; PVC déjà présent ; déprotection qui laisse une sauvegarde Harvester intacte ; déprotection avec source injoignable puis relance. Journal : chaque étape de chaque opération écrit un événement par ressource touchée (vérifié sur la protection, la bascule et la déprotection, chemins d'échec compris). Mutation volontaire : activer avant la relecture finale doit faire échouer le test « zéro perte ».

## Tâche 4 : suiveur `replication_follow`

Fichiers : `bin/lib/replication_follow.py`, `tests/api/test_replication_follow_197.py`.

Le suiveur raccourcit le RPO (relecture forcée à la fin de chaque sauvegarde : 9 à 10 s au lieu de 1 à 3 min) et tient les statistiques. Il ne fait qu'une écriture sur les clusters : le `syncRequestedAt` des `BackupVolume`. S'il s'arrête, la réplication continue au rythme de la relecture naturelle de Longhorn : le RPO s'allonge de 1 à 3 min, rien ne casse.

`follow_once(db, clusters, now)` traite tous les plans `measuring`, `protecting` et `protected` dont il tient le bail, groupés **par cluster** pour économiser kubectl :
- une liste des `Backup` par cluster source, sélecteur serveur `harvester-ops.io/replication-plan` (étiquettes reçues de la tâche ; à défaut, si la version ne les propage pas, `backup-volume in (...)`) ;
- une liste des moteurs par cluster cible, sélecteur `longhornvolume in (...)` ;
- nouveau `Backup` terminé : tick enregistré (taille envoyée, durées) ;
- **barrière par VM** : la relecture n'est demandée que lorsque tous les disques de la VM ont terminé leur sauvegarde du même passage (instantanés pris dans la même échéance du cron), ou à l'expiration de `barrier` ; un disque en erreur est noté et n'empêche pas les autres ;
- relecture : patch de `syncRequestedAt` sur chaque `BackupVolume` cible concerné (nom mis en cache, retrouvé par `spec.volumeName`) ;
- application constatée quand `lastRestoredBackup` du moteur vaut la sauvegarde attendue ; délai `resync_apply` dépassé : événement `apply-late` ;
- rythme : sommeil jusqu'à la prochaine échéance du cron, puis lecture toutes les 3 s jusqu'à la fin du passage (bornée), 60 s le reste du temps pour la santé (volumes de secours dégradés ou en défaut, moteur figé, cluster injoignable) ;
- spec de la VM source relue toutes les 10 min (par `metadata.generation`) : carnet réécrit si elle a changé ; disque ajouté ou retiré, taille changée : événement `plan-drift` (« relancer protect »), jamais d'ajout automatique, qui consommerait de la place sur la cible sans accord.
- événements : `rpo-exceeded`, `rpo-drift`, `backup-error`, `apply-late`, `source-unreachable`, `target-unreachable`, `standby-degraded`, `plan-drift`, `source-vm-running-after-failover` (plan basculé et VM source revenue en marche : alerte seulement, l'arrêt reste un geste de l'exploitant).

Bail : `take_lease` au début de chaque tour, durée trois tours ; deux suiveurs ne travaillent jamais sur le même plan (la console et un `follow` lancé à la main) ; `follow --take-over` force la reprise.

Tests : barrière (trois disques, le troisième en retard, puis en erreur) ; relecture jamais demandée avant la fin de la barrière ; stats exactes ; dépassement et dérive ; VM arrêtée ; bail ; plan en pause ; cluster injoignable ; **nombre d'appels kubectl par tour constant** quel que soit le nombre de plans d'une même paire de clusters (deux listes) ; mutation volontaire : retirer la barrière doit faire échouer le test des trois disques.

## Tâche 5 : ligne de commande `bin/harvester-replication.py`

Fichiers : `bin/harvester-replication.py`, `tests/api/test_replication_cli_197.py`.

Sous-commandes (argparse, `--json`, `--db`, `--spec`, `--timeout NOM=S`) :
- `plan create --name N --from A --to B (--vm ns/nom ... | --selector k=v --namespace ns) --rpo 15m [--map-net S=D] [--map-sc S=D] [--map-ns S=D] [--target-name ns/vm=nom] [--cloud-init rerun|keep] [--mac keep|new] [--create-namespace] [--start started|stopped] [--retain N] [--interval 5m]` : contrôle des noms et de la forme, aucune écriture sur les clusters ; le sélecteur est résolu à `protect` (et à chaque relance de `protect`).
- `plan list`, `plan show P`, `plan delete P` (seulement `draft` ou `unprotected`), `plan set P --rpo|--paused|--follow|--timeout ...` (réglages à chaud, lus par le suiveur au tour suivant).
- `check P` : contrôle complet, rien n'est écrit (sortie 2 si un blocage).
- `measure start P [--interval]`, `measure stop P`, `measure report P`, `measure formula --size --writes --interval [--block] [--throughput]`.
- `protect P [--dry-run] [--interval]`.
- `follow (--plan P | --all) [--once] [--take-over] [--console]` : boucle du suiveur ; `--console` écrit le journal au format de la console.
- `resources --host H [--side source|target|any]` : toutes les ressources de cet hôte incluses dans un plan, vu comme source ou comme cible, avec leur état, leurs derniers événements et les actions permises (`allowed_actions`) ; `events --resource UID [--limit N] [--before T]` : le journal d'une ressource.
- `status [P] [--host H]` : RPO tenu face à l'objectif par VM, arriéré, derniers débits des deux trajets, écart entre disques, prochain passage, derniers événements ; sortie 4 si un objectif est dépassé.
- `failover P [--vm ns/nom ...] [--unplanned] [--force-unplanned] [--accept-skew] [--yes] [--dry-run]`.
- `unprotect P [--delete-backups] [--yes]`.

Chaque commande mutative écrit ses événements par ressource (`by` = utilisateur de la console passé par `--by`, sinon utilisateur système ; `action_id` passé par `--id`). Tests : argv et codes de sortie de chaque sous-commande, sortie `--json` stable, `--spec` équivalent aux options, refus d'usage (RPO illisible, correspondance mal formée, plan inconnu), aucune URL de cible ni chemin dans les sorties d'erreur.

## Tâche 6 : console (fil de suivi, routes, rôles), sans écran

Fichiers : `web/app.py`, `tests/api/test_replication_routes_197.py`.

- **Fil de suivi** calqué sur l'échantillonneur d'usage (`_start_usage_sampler`) : un fil unique par processus principal (pas dans les lecteurs, `IS_READ_WORKER`) qui lance et surveille `harvester-replication.py follow --all --console` avec le kubeconfig de la console, le relance avec un délai croissant s'il s'arrête, et écrit son journal sous `logs/replication-follow.log`. Interrupteur à chaud : `HARVESTER_OPS_REPLICATION_FOLLOW=0` au démarrage, et l'option `follow` de chaque plan en base. La console doit tourner du côté du secours ou à l'extérieur des deux sites : écrit dans la doc et dans le résultat de `check` quand on peut le savoir.
- Routes : `GET /api/replication/plans`, `GET /api/replication/plans/<id>` (état calculé par `plan_status`, lecture de la base seulement, `@shared_read`), `POST /api/replication/plans` (création), `POST .../check`, `POST .../measure` (`start|stop`), `POST .../protect`, `POST .../failover`, `POST .../unprotect`, `PATCH .../settings`, `DELETE /api/replication/plans/<id>`, et les lectures par hôte de la section « Interface future », livrées dès cette version : `GET /api/replication/hosts/<host>/resources` et `GET /api/replication/resources/<uid>/events`. Toute mutation passe par `_cli_action` (action suivie, Activité, dock, annulable), avec les kubeconfigs de la personne pour les deux clusters et un contrôle d'occupation qui couvre **les deux clusters** et le plan (`_cli_action` reçoit un paramètre `busy_clusters` ; libellé `replication:<id>`), pour qu'une bascule ne parte pas sous un arrêt de cluster en cours.
- Rôles : lecture = `viewer` ; création, mesure, protection, réglages, déprotection sans suppression de sauvegardes, suppression d'un plan = `operator` ; **bascule et `--delete-backups` = `admin`** (contrôle dans la route, comme les autres actions réservées dont le droit dépend du corps de la demande). La personne doit en plus avoir sur les deux clusters les droits Kubernetes voulus (`longhorn-system`, PV) : un refus est dit comme tel (`target-denied`), pas en vue vide.
- Tests : rôles en tableau (trois rôles, chaque route), liste par hôte vue des deux côtés avec actions filtrées par le rôle de la personne, mêmes lignes de ressource dans `GET /api/replication/plans` et dans la liste par hôte (comparaison directe), événements paginés, limite de débit valide (`limits.parse_many`), action créée avec une commande publique sans chemin ni kubeconfig, occupation sur l'un ou l'autre cluster, fil de suivi : démarrage unique, relance après arrêt, interrupteur.

## Tâche 7 : vérification réelle sur harvlab vers harvlab2

Préparation :
- node2 allumé par son iLO (Redfish, identifiants Vault `secret/infra/ilo`), puis `tests/bench/harvlab/harvlab.sh start` et `HARVLAB_NAME=harvlab2 HARVLAB_NODES=1 tests/bench/harvlab/harvlab.sh start`. Clusters déclarés dans la console de dev.
- **Disque Longhorn de harvlab-n3** : indisponible le 23/10. Le constater (`nodes.longhorn.io`, conditions du disque), le réparer avant les mesures ; s'il ne peut pas l'être, noter que les volumes sont à 2 répliques sur 3 et que les mesures n'en dépendent pas.
- Objets des essais précédents (`dr-*`, `dr2-*`, tâches et backing images d'essai) listés, puis supprimés un par un après vérification (jamais par motif).
- VMs d'essai sur harvlab : `rep-a` (cirros, disque d'image + disque de 2 Gio, écrivain du banc : compteur horodaté et 8 Mio par minute), `rep-b` (trois disques, écritures dispersées de 4 Kio), `rep-c` (arrêtée), et une image téléversée seulement sur harvlab (backing image absente de harvlab2).

Essais, chacun vérifié côté cluster (`kubectl get`) puis remis dans son état d'origine :
1. `plan create` puis `check` : refus vus pour la place (disque creux de 200 Gio sur harvlab2), un nom de volume déjà pris (volume posé à la main), une MAC prise ; les refus v2, chiffrement et cible différente restent vérifiés en unitaire seulement (banc sans volume v2 ni seconde cible), et sont dits comme tels dans la doc.
2. `measure` sur `rep-a` et `rep-b` : envoi par passage comparé aux relevés du 23/10 (25 Mo par 3 min ; environ 950 Mo pour les écritures dispersées), formule comparée à la mesure.
3. `protect` : backing image restaurée sur harvlab2, volumes de secours, première copie mesurée, carnet présent.
4. `follow` par la console pendant 10 passages à 3 min : application au plus 15 s après la fin de la sauvegarde, RPO tenu inférieur à pas + 30 s, statistiques en base ; écart entre disques de `rep-b` relevé ; arrêt du processus de suivi : relancé par la console ; `follow --take-over` à la main puis retour à la console.
5. Relance après arrêt brutal : `kill -9` de `protect` à mi-course, relance : aucun doublon.
6. Migration à chaud de `rep-a` entre nœuds de harvlab pendant la protection : les sauvegardes continuent.
7. Bascule planifiée de `rep-a` : compteur continu (zéro perte), durée relevée, réseau `default/lab-net`, une VM en `cloud_init: keep` et une en `rerun` (marque `REPRISE`, clés SSH changées).
8. Bascule non planifiée de `rep-b` : nœuds de harvlab suspendus (`virsh suspend`, banc seulement) ; écart entre disques rendu ; harvlab repris ensuite : alerte `source-vm-running-after-failover` si la VM source revient en marche.
9. Bascule planifiée de `rep-c` (VM arrêtée) : sauvegarde finale d'un volume détaché.
10. Sens 1.12 vers 1.11 : un plan harvlab2 vers harvlab (volume de secours, application, activation).
11. `resources --host harvlab` et `--host harvlab2`, puis la route de la console : chaque VM d'essai vue des deux côtés avec son état, son journal et ses actions (pendant la protection, en retard, après bascule).
12. `unprotect --delete-backups` : tout ce qui porte le plan a disparu des deux côtés ; une sauvegarde Harvester de `rep-a` prise avant l'essai est toujours là.
13. Arrêt gracieux puis démarrage de harvlab2 par la console (`harvester-shutdown.sh`, `harvester-startup.sh`) pendant une protection : les volumes de secours reprennent leur application.

Points à trancher par le réel (et à écrire dans la conception) : les étiquettes d'une tâche récurrente arrivent-elles sur ses `Backup` dans les deux versions ; que fait un volume de secours quand la rétention supprime la sauvegarde qu'il a appliquée (rétention à 3, suiveur en pause, puis reprise) ; un volume de secours suit-il l'agrandissement du volume source ; le webhook de Harvester accepte-t-il une VM dont le PVC d'un disque d'image n'a pas d'image sur la cible ; le dry-run serveur d'une VM dont les PVC n'existent pas encore. Chaque réponse modifie le code ou la doc avant la livraison ; un point resté sans réponse est écrit comme non vérifié.

Après les essais : `harvlab.sh tidy`, arrêt des deux bancs, extinction de node2 (`PowerState: Off` vérifié par l'iLO).

## Tâche 8 : documentation, version

- `VERSION` 1.97.0, entrée en tête du `CHANGELOG.md` (Added, Changed, Tests).
- Doc EN et FR : `docs/en/capabilities.md`, `docs/fr/capabilites.md` (réplication en ligne de commande, limites : v1 seulement, cohérence comme après une coupure de courant, place occupée sur la cible dès la protection, activation définitive) ; `docs/en/operating-procedure.md` et `docs/fr/procedure-operationnelle.md` (mesurer, protéger, suivre, basculer, déprotéger, où placer la console).
- Conception : section « Ce que la 1.97.0 a appris » avec les réponses de la tâche 7 ; tableau de parité inchangé (fonction sans équivalent dans Harvester).

## Cohérence entre disques d'une VM : la limite, dite telle quelle

Une tâche récurrente Longhorn traite chaque volume pour lui-même : il n'existe pas d'instantané atomique de plusieurs volumes. La 1.97.0 réduit l'écart sans le supprimer :
- une tâche par VM, avec autant de volumes en parallèle que de disques : les instantanés d'une VM partent dans la même seconde ou presque ;
- la barrière du suiveur n'applique côté secours un passage que lorsque tous les disques de la VM l'ont terminé ;
- l'écart est mesuré à chaque passage (`skew_s`), montré par `status`, et contrôlé à la bascule non planifiée (`--accept-skew` au-delà de la tolérance) ;
- la bascule planifiée, VM arrêtée, donne un point exact pour toute la VM.

Une application qui répartit une transaction sur deux disques (données et journal séparés) peut donc voir, après une bascule non planifiée, des disques décalés d'une seconde ou deux. La version des exercices pourra proposer un mode cohérent piloté par la console : gel du système invité par l'agent (sous-ressource `freeze` de la VMI), instantané de tous les disques, dégel, puis sauvegarde. L'option `--consistency` de la maquette est réservée à ce mode.

## Interface future : menu « Disaster Recovery » et sous-menu de chaque hôte

Un seul modèle, deux vues :
- un menu racine **« Disaster Recovery »** (la section « Protection » de la maquette) présente tous les plans, avec l'état et les actions de chaque plan et de chacune de ses ressources ;
- chaque hôte (un cluster Harvester aujourd'hui, un hyperviseur KVM plus tard) a un sous-menu « Disaster Recovery » : les mêmes données, filtrées sur cet hôte, comme source ou comme cible.

La 1.97.0 livre ce que ces deux vues liront :

- `GET /api/replication/plans` : `[{id, name, source_host, target_host, state, rpo_objective_s, rpo_held_s (pire ressource), interval_s, next_tick_at, resources_by_state: {...}, last_events: [...3], actions: [{name, role, allowed}], resources: [<ligne de ressource ci-dessous>]}]`, et `GET /api/replication/plans/<id>` pour un plan avec ses statistiques détaillées ; les actions d'un plan (`plan_actions(plan, side)`, à côté de `allowed_actions`) agissent sur toutes ses ressources ;
- `GET /api/replication/hosts/<host>/resources?side=source|target|any` : les lignes de ressource, produites par la **même fonction** que la liste des plans, filtrées par hôte : `[{plan, plan_name, resource_kind, resource_uid, namespace, name, side, peer_host, state, state_since, rpo_held_s, rpo_objective_s, last_applied_at, last_events: [...3], actions: [{name, role, allowed}]}]` ; `allowed` tient compte du rôle de la personne, l'action reste listée (bouton grisé avec sa raison) ;
- `GET /api/replication/resources/<uid>/events?limit=&before=` : le journal persistant de la ressource, toutes opérations, avec le lien vers l'action d'Activité quand il y en a une ;
- les actions passent par les routes du plan (`POST /api/replication/plans/<id>/failover` avec `vms`, etc.) : pas de seconde porte d'écriture.

Les deux routes partagent `resource_row()` et `plan_row()` : ce que montre un hôte est exactement ce que montre le menu racine pour ses ressources. En ligne de commande, `plan list --json` et `resources --host H --json` rendent les mêmes objets. `<host>` est le nom d'un cluster déclaré ; un hôte KVM utilisera le même chemin avec `host_kind` dans la réponse, sans changer la forme.

## Ce que le modèle garde possible pour la suite

- **Exercices** : l'exercice courant restaure une copie séparée depuis la sauvegarde appliquée, sans toucher aux volumes de secours ; il réutilise PV/PVC, carnet et VM cible avec le réseau d'exercice des correspondances. Les durées d'étape de `runs` servent de RTO mesuré.
- **Retour au site principal** : `generation` et `active_side` du plan. Reprotéger = une nouvelle génération, sens inverse, nouveaux volumes de secours nommés avec la nouvelle génération, **copie complète dans chaque sens** (le volume activé ouvre un nouveau `BackupVolume`, l'incrémental ne le traverse pas) ; les lignes de l'ancienne génération restent pour l'audit et le nettoyage. La perte côté ancien site (écritures après sa dernière sauvegarde) se calcule depuis `ticks` et sera dite avant le retour.
- **Vagues** : `failover --vm` existe déjà ; l'ordre et les appels annexes viendront dans les options du plan.
- **Vue de suivi** : tout ce qu'elle affiche sort de `plan_status`, `ticks` et `events`, déjà rendus par `GET /api/replication/plans/<id>`.
