# Déplacer une VM d'un cluster à l'autre : transfert, export, import

Conception validée le 24/09/2026, livrée en v1.45.0 le 25/09/2026 après essais
réels (section « Ce que le banc a appris », qui prime sur le reste en cas de
doute).

## Contexte

La console gère un ensemble de clusters Harvester, mais une VM reste
prisonnière du cluster où elle est née : rien ne permet de la déplacer vers un
autre cluster déclaré, ni de l'emporter vers un site isolé. Harvester fournit
une brique (la sauvegarde de VM vers une cible commune, restaurable sur un
autre cluster) sans l'outillage qui la rend sûre et sans voie de repli quand
les clusters ne partagent pas de cible.

## Décisions de l'exploitant (24/09/2026)

- **Deux usages** : le transfert direct entre deux clusters déclarés dans la
  console, et l'export vers un fichier (importable plus tard, ailleurs).
- **Interruption au choix** à chaque transfert : VM arrêtée pendant toute la
  copie, ou arrêt court (première copie VM en marche, puis seulement le delta).
- **États finaux au choix de l'opérateur**, pour la source (en marche,
  arrêtée, supprimée) comme pour la cible (démarrée ou laissée arrêtée).
- **Cible de sauvegarde commune utilisée si elle existe** ; sinon, repli sur
  une copie qui passe par la console, VM arrêtée pendant l'export seulement.
- **Console et ligne de commande** : même moteur, même format de fichier.

## Ce que Harvester fait, et ce qu'il ne fait pas

Fait (relevé sur harv1 v1.9.0 et dans la documentation) :

- `VirtualMachineBackup` de type `backup` vers la cible (NFS ou S3). Deux
  clusters réglés sur la même cible voient les sauvegardes l'un de l'autre :
  le contrôleur synchronise les métadonnées (`vmbackups/<ns>/<nom>.cfg`).
- `VirtualMachineRestore` sait créer une nouvelle VM (`newVM`), garder ou non
  les adresses MAC (`keepMacAddress`), la laisser arrêtée
  (`haltAfterRestore`).
- Depuis la 1.4, les images partent avec la sauvegarde (sauvegarde de
  « backing image » Longhorn) et sont recréées sur l'autre cluster, **sauf si
  une image du même nom ou du même nom affiché y existe déjà**.
- Les sauvegardes Longhorn sont incrémentales par volume.
- L'API télécharge une image : `.../virtualmachineimages/<ns>/<nom>/download`,
  flux gzip, joignable par le proxy de service de l'API Kubernetes. Une
  image « exportée d'un volume » sort en disque brut, une image téléversée
  dans son format d'origine (vérifié sur harv1).
- CDI (KubeVirt Containerized Data Importer) est installé : un `DataVolume`
  remplit un volume ordinaire depuis une URL HTTP (présent sur harv1).

Ne fait pas :

- aucune correspondance de réseau ni de classe de stockage à la restauration :
  la VM restaurée reprend les noms d'origine ;
- aucun contrôle préalable (cible commune, image homonyme, place allouable,
  version, namespace, nom libre) ;
- aucun enchaînement : sauvegarder, attendre la synchronisation, restaurer,
  décider du sort de la source restent des gestes séparés ;
- aucune voie sans cible commune, aucun fichier portable.

## Architecture

```
console (Flask)                        CLI (site isolé)
  assistant + magasin d'exports          harvester-vm-transfer.py ...
            |  Popen, STEP_EVENT                  |
            +-------> bin/harvester-vm-transfer.py <-+
                        check | migrate | export | import
                        |
                        +-- bin/lib/vm_transfer.py    (logique pure, testée)
                        +-- bin/lib/longhorn_room.py  (place allouable)
                        |
                        kubectl, et kubectl proxy local pour le HTTP de Harvester
```

- **Un seul moteur**, `bin/harvester-vm-transfer.py`, en Python stdlib qui
  pilote `kubectl` (précédent : `harvester-provider-install.py`). La console
  le lance dans un ActionRun et relaie ses lignes
  `STEP_EVENT|<étape>|<statut>|<message>` ; elle ne le contourne jamais.
- **Logique pure** dans `bin/lib/vm_transfer.py` : nettoyage du manifeste,
  correspondances, verdict de contrôle, écriture et lecture de l'archive,
  choix du moteur. Importée par le script et par `web/app.py` (`bin/` est
  copié tel quel dans l'image, `lib/` compris).
- **Place allouable Longhorn** : `_storage_room` quitte `web/app.py` pour
  `bin/lib/longhorn_room.py`, importé des deux côtés (même calcul que la
  création de VM : sur-provisionnement et place réelle, la plus serrée
  décide).
- **HTTP de Harvester** (téléchargement d'image) par un `kubectl proxy`
  lancé sur un port local éphémère, arrêté en fin de commande : marche quel
  que soit le mode d'authentification du kubeconfig, et n'exige que l'accès à
  l'API Kubernetes.
- La console passe au script les kubeconfigs déjà préparés pour chaque
  cluster (`_kubectl_for_cluster`), donc avec l'identité déléguée de
  l'opérateur. En CLI : `--from`/`--to` nomment des clusters de
  `config.yaml`, ou `--from-kubeconfig`/`--to-kubeconfig` des fichiers.

## Le contrôle préalable (`check`)

Lecture seule, sortie JSON : une liste de constats
`{code, niveau: ok|warn|block, faits}`. Le texte est composé côté interface
(cinq langues) et côté CLI (anglais). Un seul `block` interdit le lancement.

| Contrôle | Niveau si échec |
|---|---|
| Cluster cible joignable, CRD KubeVirt présentes | block |
| Version Harvester cible >= source | warn (non garanti par Harvester) |
| Namespace cible existant | block, ou création acceptée par l'opérateur |
| Nom de VM libre sur la cible | block |
| Chaque réseau de la VM a une correspondance existante sur la cible | block |
| Chaque classe de stockage a une correspondance existante sur la cible | block |
| Place allouable Longhorn cible >= somme des disques (répliques comprises) | block |
| Périphériques PCI, vGPU, affinité ou sélecteur de nœuds de la source | warn (retirés, listés) |
| Moteur sauvegarde : même cible (type, adresse, compartiment normalisés), condition `configured` des deux côtés, chaque réseau garde son nom sur la cible, chaque classe (hors image) y existe | décide du moteur, sinon copie par la console |
| Adresses MAC gardées déjà portées par une VM de la cible (même arrêtée) | block |
| Plus de répliques demandées que de nœuds sur la cible | warn (volumes dégradés ; place jugée sur les nœuds réels) |
| Moteur sauvegarde : image de même nom ou nom affiché sur la cible, contenu différent (somme de contrôle) | block (Harvester sauterait la synchro) |
| Moteur sauvegarde : Harvester cible < 1.4 et image absente | block, proposer le moteur fichier |
| Moteur fichier : CDI prêt sur la cible | block |
| Moteur fichier : place allouable sur la source pour les images temporaires (1 réplique) | block |
| Export : place libre dans le magasin >= taille réellement occupée des volumes | block |
| Mode arrêt court demandé sans cible commune | block (le mode exige des sauvegardes incrémentales) |

Correspondances par défaut : même namespace et même nom s'ils existent sur la
cible, sinon la classe de stockage par défaut de la cible et aucun réseau
(l'opérateur choisit). Toute correspondance est modifiable.

## Moteur « sauvegarde » (cible commune)

Étapes, chacune une ligne STEP_EVENT :

1. `check` refait au moment d'agir.
2. Mode **arrêt court** : sauvegarde n°1 VM en marche (gel du système de
   fichiers par l'agent invité s'il est présent), attente `readyToUse`.
3. Arrêt de la source (sauf état final « en marche » en copie).
4. Sauvegarde n°2 (en arrêt court, seul le delta part), attente `readyToUse`.
5. Attente de la sauvegarde sur la cible (objet synchronisé), avec une
   relance chaque minute : retrait de l'annotation `harvesterhci.io/hash` du
   réglage `backup-target` (seul déclencheur d'une relecture quand
   `refreshIntervalInSeconds` vaut 0) et demande de relecture à Longhorn
   (`BackupTarget.spec.syncRequestedAt`). Le premier passage ne ramène que
   les images ; la sauvegarde apparaît à un passage suivant.
6. `VirtualMachineRestore` sur la cible : `newVM: true`, nom et namespace
   choisis, `keepMacAddress` selon la section MAC, `deletionPolicy: retain`,
   `haltAfterRestore: true` seulement si la CRD de la cible le connaît (1.9 ;
   la 1.8 le refuse). Sinon la VM restaurée démarre d'elle-même et le moteur
   l'arrête avant l'étape suivante.
7. Retrait des périphériques et épinglages signalés sur la VM restaurée.
   Les réseaux gardent leur nom : le webhook de Harvester refuse une
   restauration dont un réseau d'origine manque sur la cible, c'est pourquoi
   un réseau renommé impose la copie par la console.
8. États finaux (section suivante).
9. Nettoyage : les sauvegardes du transfert sont supprimées, sauf si
   l'opérateur demande à les garder. L'objet de restauration reste (Harvester
   refuse de le supprimer tant que la VM restaurée existe), sans l'étiquette
   du transfert.

Si la source doit rester en marche (copie), une seule sauvegarde est prise
VM en marche, sans interruption ; le mode d'interruption est alors sans objet.

Interruption réelle : en arrêt simple, sauvegarde complète + restauration ;
en arrêt court, delta + restauration. La restauration relit tout le volume
depuis la cible, elle reste proportionnelle à la taille.

Vérifié en réel : l'image recréée sur la cible garde le nom de classe de
l'image source (`lh-<uuid>` identique) ; la sauvegarde n'est synchronisée
qu'une fois cette classe créée.

## Moteur « fichier » (sans cible commune, export, import)

**Export** :

1. `check`.
2. Arrêt de la source.
3. Pour chaque disque : image temporaire « exportée du volume »
   (`export-from-volume`, une réplique, étiquetée
   `harvester-ops.io/transfer=<id>`), attente de son état prêt.
4. **Source rallumée tout de suite** si son état final est « en marche » :
   les données sont figées dans l'image. L'interruption dure l'export interne
   au cluster, pas le transfert.
5. Téléchargement en flux de chaque image dans l'archive (sans
   recompression).
6. Suppression des images temporaires, écriture des sommes de contrôle.

**Import** :

1. Lecture du manifeste (premier membre de l'archive) et `check` contre la
   cible.
2. Création de ce que l'opérateur a accepté de créer (namespace).
3. Pour chaque disque, un `DataVolume` CDI de source HTTP, dans la classe de
   stockage choisie, avec les modes d'accès et de volume du disque d'origine
   (bloc, RWX pour la migration à chaud). CDI va chercher le disque sur une
   URL à jeton éphémère servie par le poste de la console (sur le modèle de
   `web/pxe_server.py`), qui renvoie le membre `.raw.gz` de l'archive.
4. Création de la VM (manifeste nettoyé, disques pointant ces volumes,
   correspondances appliquées), arrêtée.
5. États finaux.

**Transfert direct sans cible commune** : export puis import enchaînés,
**sans fichier intermédiaire** : l'URL servie à CDI relaie en direct le
téléchargement depuis la source.

Exigence : les nœuds de la cible doivent joindre le poste de la console en
HTTP sur un port (comme pour une installation Bare-metal). Un refus est
détecté au premier disque (condition du `DataVolume`, aucun accès au jeton
dans le délai) et arrête le transfert avec cette explication.

Résultat sur la cible : des volumes ordinaires, sans image Harvester
derrière eux. Passer par une image Harvester en aurait laissé une par disque,
impossible à supprimer tant que la VM existe.

**Vérifié sur harv1 le 24/09/2026** (disque système de 10 Gio d'une VM
arrêtée, tout nettoyé ensuite) :

- export du volume en image : 90 s ; téléchargement par le proxy de l'API :
  110 s, 500 Mo de gzip pour 10 Gio de disque brut ;
- `DataVolume` de source HTTP vers `harv-rep1` (RWX, bloc) : importé en
  2 min 30. L'importeur fait un `HEAD` puis un seul `GET`, sans requête
  `Range` : le guichet doit répondre aux deux, en flux ;
- VM créée sur ce volume (sans annotation `volumeClaimTemplates`) : démarrée,
  agent invité connecté en 40 s. Le webhook de Harvester pose lui-même
  `mac-address` et `vmRunStrategy` ;
- **CDI ne ramasse pas le `DataVolume` terminé**, et le volume lui appartient
  (`ownerReference` contrôleur) : supprimer le `DataVolume` supprimerait le
  disque de la VM. Le moteur le supprime donc avec `--cascade=orphan` une fois
  l'import réussi (vérifié : le volume reste lié, sans propriétaire, la VM
  continue de tourner) ;
- l'affinité `network.harvesterhci.io/<réseau de cluster>` d'une VM vient de
  ses réseaux, pas de ses nœuds : elle suit la correspondance des réseaux au
  lieu d'être signalée comme une affinité de nœud.

## Format d'archive `<vm>-<AAAAMMJJ-HHMMSS>.hvx`

Tar non compressé, lisible en flux, membres dans cet ordre :

1. `manifest.json` : `format` (1), version de harvester-ops, cluster et
   version Harvester d'origine, date, la VM nettoyée (sans `status`, `uid`,
   `resourceVersion`, `managedFields`, annotations de contrôleur), les
   secrets cloud-init qu'elle référence, et pour chaque disque : nom du
   volume, taille, classe de stockage, modes d'accès et de volume, image
   d'origine, nom du membre.
2. `disks/<volume>.raw.gz` : le flux gzip de Harvester, tel quel.
3. `SHA256SUMS` : somme de chaque membre précédent, écrite en dernier.

Le fichier est créé en 0600 : il contient les secrets cloud-init (sans eux la
VM ne redémarre pas comme avant). L'assistant le signale.

## États finaux, adresses MAC, retour arrière

- **Source** : en marche, arrêtée (annotée
  `harvester-ops.io/transferred-to=<cluster>/<ns>/<nom>`) ou supprimée avec ses
  volumes. La suppression n'a lieu qu'une fois la cible vérifiée.
- **Cible** : démarrée, ou laissée arrêtée.
- **Vérification de la cible** : démarrée, la VM atteint `Running` (et
  l'agent invité s'il existait sur la source) ; arrêtée, tous ses volumes sont
  liés et prêts.
- **MAC** : si la source ne reste pas en marche, les adresses sont gardées
  par défaut (les réservations DHCP suivent), désactivable. Si la source
  reste en marche, de nouvelles adresses sont imposées, et l'assistant
  prévient que le nom d'hôte cloud-init sera en double.
- **Retour arrière** : tout ce que le transfert crée porte l'étiquette
  `harvester-ops.io/transfer=<id>`. En cas d'échec ou d'annulation (depuis le
  dock : signal au script) avant la vérification de la cible, le script
  supprime ce qu'il a créé des deux côtés et remet la source dans son état de
  départ (relancée si elle tournait). La source n'est jamais supprimée sur un
  transfert en échec.
- **Concurrence** : un seul transfert par VM ; refus si un arrêt ou un
  démarrage de cluster tourne sur la source ou la cible (même mécanisme que
  `ActionBusy`).

## Console

- **Une seule fenêtre « Migrer »** (décision de l'exploitant, 25/09/2026) :
  la migration à chaud existante et le transfert entre clusters sont deux
  destinations du même geste, ouvert par le bouton de migration de la ligne
  de la VM. En tête, le choix de la destination :
  - **un autre nœud** de ce cluster : la migration à chaud d'aujourd'hui,
    inchangée (nœuds disponibles, historique) ;
  - **un autre cluster** déclaré ;
  - **un fichier** (export).
  Pour un autre cluster ou un fichier : namespace et nom, moteur retenu et
  pourquoi, mode d'interruption (arrêt court proposé seulement avec une
  cible commune), états finaux, MAC, table des correspondances pré-remplie,
  puis le rapport de contrôle (chaque constat expliqué, les blocages en
  tête). « Lancer » crée l'ActionRun ; la suite se lit dans le dock et
  l'Activité, sur les deux clusters.
- **Magasin d'exports** : fenêtre flottante ouverte par un bouton « Exports »
  de la barre de la vue Machines virtuelles (la vue n'a pas de sous-onglets ;
  même patron que les instantanés), sur le modèle du magasin d'ISO : liste (VM, cluster et date d'origine, taille,
  intégrité), suppression, téléchargement, « Importer vers... » qui rouvre
  l'assistant en partant du fichier.
- Répertoire du magasin : `HARVESTER_OPS_EXPORT_DIR`, par défaut
  `~/.local/share/harvester-ops/exports`, et `/var/lib/harvester-ops/exports`
  dans le service installé (volume persistant déjà monté). En 1.45.0, pas de
  téléversement par le navigateur (un formulaire multipart de plusieurs
  gigaoctets passerait par un tmpfs) : on déposait le fichier dans le
  répertoire, ou on importait en CLI. Levé en 1.47.0, voir plus bas.
- Points d'accès : `POST /api/vm/<cluster>/<ns>/<nom>/transfer/check`,
  `POST /api/vm/<cluster>/<ns>/<nom>/transfer`, `GET /api/exports`,
  `DELETE /api/exports/<fichier>`, `GET /api/exports/<fichier>/download`,
  `POST /api/exports/<fichier>/check`, `POST /api/exports/<fichier>/import`,
  et depuis 1.47.0 `PUT /api/exports/<fichier>` (dépôt).
  Tous `@requires_auth` ; les mutations avec `@_rate_limit`, rôle operator.

### 1.47.0 : l'archive désignée, le dépôt en flux

Question de l'exploitant après son premier export réel : « comment est-ce
que je récupère l'image ? comment la réimporter ? ». La fenêtre disait
« Transfert terminé » sans nommer le fichier, et une archive téléchargée ne
revenait dans le magasin d'une autre console qu'à la main.

- **La console nomme l'archive** (`<vm>-<AAAAMMJJ-HHMMSS>.hvx`) et la passe
  au script en `--out <fichier>` au lieu du répertoire ; l'`ActionRun` la
  porte dans `result.archive`, que l'événement de fin transmet. La fenêtre
  affiche alors le nom, la taille et trois gestes : télécharger, importer,
  ouvrir le magasin. `result` n'est pas persisté en base : après un
  redémarrage ou le ménage d'une heure, le magasin reste le chemin.
- **Dépôt : `PUT /api/exports/<fichier>`, le corps EST le fichier.** Rien
  ne passe par le parseur de formulaires de Werkzeug, qui aurait recopié le
  fichier entier dans `/tmp` (en mémoire dans le service installé) : la
  requête est lue par tranches de 1 Mio dans `<fichier>.part` (0600), puis
  liée sous son nom final (`os.link`, qui refuse d'écraser). Content-Length
  obligatoire ; la place est contrôlée avant de lire le premier octet
  (taille + 256 Mio de marge).
- **Vérifiée avant d'entrer** : complète (liste des sommes présente),
  manifeste lisible avec une VM, chaque membre conforme à sa somme. La leçon
  des téléchargements corrompus « à la bonne taille » vaut ici : une archive
  abîmée est refusée au dépôt (422, membre en cause), pas au milieu d'un
  import de vingt minutes.
- **Suivi comme une action**, bien que le travail se fasse dans le thread
  de la requête (les données arrivent par elle) : phases `upload` puis
  `verify` avec débit et temps restant, dans le dock et l'Activité. La
  fenêtre du magasin mesure l'envoi côté navigateur (XHR), puis suit la
  vérification par le flux de l'action.
- **Annulation** : depuis la fenêtre (abandon de la requête) ou le dock.
  Le dock ne savait tuer qu'un processus ; les actions sans processus
  (téléchargement d'ISO, installation bare-metal) consultaient un drapeau
  `_cancel` que rien ne posait. Il est posé désormais, ce qui répare aussi
  ces deux-là.
- Un `.part` laissé par un arrêt de la console est retiré au dépôt suivant
  (tout `.part` qu'aucun dépôt en cours n'écrit est un reste).
- Parité CLI : une archive est un fichier, `harvester-vm-transfer import
  --in` le prend directement ; le magasin n'est qu'un répertoire.

## Ligne de commande

```
harvester-vm-transfer check   --from A --vm ns/nom --to B [--map-net a=b] [--map-sc a=b] [--json]
harvester-vm-transfer migrate --from A --vm ns/nom --to B [--mode stop|short] [--source running|stopped|deleted]
                              [--target started|stopped] [--new-mac] [--name x] [--namespace y] [--keep-backups]
harvester-vm-transfer export  --from A --vm ns/nom --out fichier.hvx [--source running|stopped]
harvester-vm-transfer import  --to B --in fichier.hvx [--serve-address ip:port] [...mêmes options de cible]
```

`--dry-run` sur les quatre : affiche le plan sans rien modifier.

## Sécurité

- Les secrets cloud-init ne transitent jamais dans une réponse ni dans un
  libellé d'action ; l'archive est en 0600.
- URL de disque à jeton aléatoire, à durée de vie limitée, révoquée en fin
  d'import ; aucun listing, aucun autre chemin.
- Noms de fichier du magasin validés (pas de chemin, suffixe `.hvx`).

## Tests et essais réels

- **Unitaires** (`tests/api/`) : nettoyage du manifeste, correspondances,
  chaque constat du contrôle sur des objets simulés, choix du moteur,
  normalisation des cibles de sauvegarde, écriture et relecture d'une archive
  avec un faux disque, sommes de contrôle, analyse des STEP_EVENT, points
  d'accès (contrôle, lancement, refus de concurrence, magasin).
- **Navigateur** (`tests/e2e/`) : l'assistant affiche un blocage et empêche le
  lancement, le magasin liste un export.
- **Réel**, sur un banc à deux clusters de même version tous deux sur node2
  (harvlab et un second cluster imbriqué, voir le CLAUDE.md global) :
  1. d'abord les deux points à vérifier (CDI importe un `.raw.gz` dans une
     classe Longhorn ; Harvester accepte une VM sur un volume ainsi créé) ;
  2. transfert par cible commune (NFS dédié au banc), en arrêt simple puis en
     arrêt court, d'une VM née d'une image ;
  3. transfert direct sans cible commune ;
  4. export puis import d'une archive ;
  5. entre versions différentes : archive d'un cluster 1.8.2 importée sur
     harv1 (1.9.0), puis VM de test supprimée ;
  6. chaque fois : VM vérifiée côté cible (`kubectl`), source dans l'état
     choisi, rien d'étiqueté `harvester-ops.io/transfer` laissé derrière.

## Ce que le banc a appris (25/09/2026)

Essais réels sur harvlab (3 nœuds) et harvlab2 (1 nœud), deux clusters
Harvester 1.8.2 imbriqués sur node2, et sur harv1 (1.9.0). Chaque constat a
d'abord été reproduit par un test, puis corrigé :

- **CDI coupe sa première connexion** après avoir reconnu le format, puis
  retélécharge tout : le guichet distingue le client qui raccroche (normal)
  de la source qui échoue.
- **Le proxy de l'API a refusé une fois un téléchargement** (« tls:
  unrecognized name ») puis l'a servi : un téléchargement refusé avant son
  premier octet est repris, jamais une coupure en cours de route.
- **Adresses MAC** : une VM créée hors de l'interface de Harvester ne les a
  que dans l'annotation `harvesterhci.io/mac-address` (recopiées au
  nettoyage) ; Harvester refuse deux VMs de même MAC, même arrêtée (contrôle
  `mac-in-use`).
- **Synchronisation de la cible de sauvegarde** : voir l'étape 5 du moteur.
- **`haltAfterRestore` inconnu en 1.8**, **réseau d'origine exigé à la
  restauration**, **restauration indélébile tant que la VM vit** : voir les
  étapes 6, 7 et 9.
- **Site mononœud** : la classe par défaut demande 3 répliques, Longhorn
  crée le volume dégradé ; c'est un avertissement, pas un manque de place.
- **Interface** : les correspondances suivent le cluster de la réponse du
  contrôle (une réponse écartée laissait les listes d'un autre cluster).

Scénarios passés : copie directe sans cible commune (disques identiques bit
à bit), export puis import, sauvegarde en arrêt simple avec image recréée,
arrêt court avec suppression de la source, archive 1.8.2 importée sur harv1
1.9.0 (identique bit à bit), transfert lancé depuis l'interface, annulation
depuis le dock et deux échecs réels, chacun défait sans rien laisser.

## Hors périmètre

- Transfert de plusieurs VMs d'un coup, planification, reprise d'un
  transfert interrompu.
- Restauration incrémentale continue côté cible (volumes de reprise Longhorn).
- Chiffrement de l'archive.
