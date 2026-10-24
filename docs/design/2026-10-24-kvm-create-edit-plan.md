# KVM, prochaine version : créer et modifier des VMs, images, pools et réseaux (plan de réalisation)

Statut : plan, 24/10/2026. Numéro de version posé à la livraison (« prochaine version KVM » dans ce document).

Conception : `2026-10-23-hyperviseurs-kvm.md` (ligne « Création et modification de VMs (CPU, mémoire, disques, cartes, à chaud quand libvirt le permet), pools et volumes, réseaux »). Plans précédents : `2026-10-23-kvm-196-plan.md`, `2026-10-24-kvm-197-plan.md` (publié en 1.98.0).

Demande de ju (24/10/2026) : « il faudra aussi la possibilité d'éditer et de créer des VMs sur les noeuds KVM. Reprendre l'expérience utilisateur Harvester ». Cette version ajoute donc, en CLI et dans la console :

- la création d'une VM sur un hôte KVM, dans une fenêtre qui reprend celle des VMs Harvester (mêmes onglets, mêmes champs quand ils ont un sens, même assistant cloud-init) ;
- sa modification, avec la distinction « appliqué maintenant » et « appliqué au prochain démarrage » ;
- les gestes à chaud du menu d'une VM (CPU et mémoire, disque, carte, CD-ROM), comme ceux des VMs Harvester ;
- un catalogue d'images par hôte (téléverser, télécharger depuis une URL, somme de contrôle) ;
- la gestion des pools de stockage, des volumes et des réseaux libvirt, avec des garde-fous.

## Contraintes communes

Celles de la 1.96.0 et de la 1.98.0 restent valables : bibliothèque standard seulement dans `bin/`, `virsh` lancé sur l'hôte par SSH, `shlex.quote` sur chaque argument distant (`kvm_host.ssh_argv`), `env LC_ALL=C` devant chaque `virsh`, protocole `STEP_EVENT|<id>|<status>|<message>`, `--json`, codes de sortie 0, 1, 2 et 3 (annulé), objet JSON à chaque sortie (y compris une erreur d'usage : `{ok:false, error, kind}`). S'y ajoutent ou se précisent :

- **La console lance le script en `--opt=valeur`** (`--name=-x` passe, `--name -x` est pris pour une option). Vaut pour tous les nouveaux paramètres (`--pool=`, `--volume=`, `--network=`, `--target=`...).
- **Rien n'est installé sur l'hôte.** Le XML du domaine, le XML des volumes, des pools et des réseaux, et l'ISO cloud-init sont fabriqués par la console en Python pur. Ni `virt-install`, ni `virt-clone`, ni `genisoimage`, `mkisofs`, `xorriso` ou `cloud-localds` sur l'hôte. Prérequis inchangés : libvirt, `qemu-img`, un compte membre du groupe `libvirt`.
- **Les définitions passent par l'entrée standard.** `virsh define --validate /dev/stdin`, `attach-device --file /dev/stdin`, `vol-create --file /dev/stdin`, `net-define --file /dev/stdin`, `pool-define --file /dev/stdin` par `VirshHost.run_input` : rien dans un fichier temporaire de l'hôte, rien sur la ligne de commande (le XML peut porter le mot de passe VNC). `--validate` toujours : libvirt contrôle le XML contre son schéma avant de l'accepter.
- **Le formulaire n'est jamais une source de vérité complète.** La modification relit `dumpxml --inactive --security-info`, applique des changements ciblés sur l'arbre `ElementTree` et redéfinit. Tout ce que le formulaire ne gère pas (épinglage CPU, ordonnanceur temps réel, `<numatune>`, `<hostdev>`, `<qemu:commandline>`, métadonnées d'autres outils, `<seclabel>`, contrôleurs ajoutés à la main) reste octet pour octet dans le XML (comparaison canonique C14N, testée sur `dumpxml-netperf.xml`). Une modification vide ne lance aucun `define`.
- **Lectures rejouées, écritures jamais.** Les lectures (`dumpxml`, `domcapabilities`, `vol-list`, `pool-dumpxml`...) passent avec `idempotent=True` (reprise sur une connexion neuve après une coupure du transport, gotcha OpenSSH 10.2p1). Une écriture n'est jamais rejouée : après une coupure, la commande relit l'état et dit ce qui a été fait ou ce qui reste incertain. **Les écritures longues** (téléversement d'un volume, copie d'une image en copie complète, `vol-create-from`) passent sur une connexion propre (`fresh=True`, `ControlMaster=no`, `ControlPath=none`) : un plantage de `sshd-session` sous sessions multiplexées simultanées ne doit pas les couper, et elles ne bloquent pas les lectures des autres.
- **Une opération à la fois par VM.** Création, modification et gestes à chaud prennent le verrou par VM de la 1.98.0 (`_kvm_vm_op`, `run.kvm_vms`) ; une création tient les noms qu'elle va créer (`kvm_vms = (nom-01, nom-02, ...)`) pour qu'une seconde création ou un clonage vers le même nom reçoive 409. Les opérations de pool et de réseau prennent un verrou par objet (`kvm:pool-<verbe>:<hôte>/<pool>`, `kvm:net-<verbe>:<hôte>/<réseau>`) ; supprimer un pool refuse pendant une création qui y écrit.
- **Plan recalculé à l'exécution.** Comme la suppression de la 1.98.0 : l'aperçu montré dans la page n'est jamais cru. La modification envoie l'empreinte (`sha256` du XML inactif canonique) lue à l'ouverture de la fenêtre ; si le XML a changé entre-temps, refus `kind: "changed"` (« the VM was changed since you opened it: reload »). L'empreinte couvre la définition ENTIÈRE, pas seulement les champs gérés : tout changement venu d'ailleurs depuis l'ouverture est refusé. Reste la fenêtre entre ce contrôle (plan recalculé juste avant d'écrire) et le `define` (les gestes à chaud passent avant, jusqu'à 30 s pour un retrait de disque) : le `define` part du XML relu à ce moment et n'y change que les champs modifiés, donc un changement extérieur sur un autre champ est gardé, mais un changement extérieur sur le même champ géré est écrasé par la valeur du formulaire (dit dans la docstring de `kvm_edit`).
- **Pools : la console ne crée un pool que sur demande explicite d'un administrateur** (nouvelle fonction de cette version, tâche 2), et jamais comme effet de bord. Aucune création de VM ou de volume ne crée de pool. La règle « après chaque `virt-install`, `pool-list --all` » du banc reste : le banc n'utilise toujours pas `virt-install`.
- **Aucun secret ne sort.** Mot de passe VNC (généré par la console, jamais affiché ni rendu), mots de passe des utilisateurs cloud-init et contenu de user-data : jamais dans une réponse d'API autre que la lecture du cloud-init par un opérateur (comme `GET /api/vm/.../cloudinit` côté Harvester), jamais dans un journal, une étape d'action, une ligne de commande ou l'Activité. Le JSON du script dit `user_data: true` et la taille, pas le contenu. Le spec de création arrive au script par un fichier 0600 du répertoire de travail de l'action (`--spec-file=`), supprimé par le `cleanup` de `_kvm_action`, jamais par argv (visible dans `/proc`).
- **Interface** : `escapeHtml` sur tout nom (VM, volume, pool, réseau, pont, chemin, image, libellé d'action), confirmations dans la page (jamais `confirm()` ni `prompt()`), bulle d'aide sur chaque contrôle, cinq langues (`test_i18n` à zéro écart), suivi dans le dock par `SSEReconnect`.
- **Rôles** : lectures = lecteur ; créer et modifier une VM, gestes à chaud, créer, téléverser ou redimensionner un volume, rafraîchir un pool = opérateur ; supprimer un volume, créer, arrêter, démarrer, supprimer un pool ou un réseau, régler l'autostart d'un pool ou d'un réseau = administrateur (un pool ou un réseau sert à toutes les VMs de l'hôte).
- **Domaines de test** : uniquement des `kvmtest-*` (VMs, volumes, pools `kvmtest-pool*`, réseaux `kvmtest-net*`) sur node2, jamais un objet existant ; relevé de référence (`list --all --name`, `pool-list --all`, `net-list --all`, `vol-list` de chaque pool) avant, comparé après. node2 porte un autre banc en ce moment : **les vérifications réelles se feront plus tard**, la réalisation avance sur jeux d'essai et faux hôte, et la livraison attend la vérification réelle.
- Contenu public : ni tiret cadratin ni flèche Unicode, pas de ligne `Co-Authored-By`, aucune mention d'outil d'écriture.

## Décisions de conception

### Créer sans rien installer : XML fabriqué par la console

La console lit ce que l'hôte sait faire, puis fabrique un XML que libvirt valide :

- `virsh capabilities` (jeu d'essai `capabilities.xml` déjà présent) : architectures, types de machine (`<machine canonical=...>`), domaines `kvm` disponibles.
- `virsh domcapabilities --virttype kvm --arch x86_64 --machine <type>` : micrologiciel (`<os><enum name='firmware'>`, `<loader>` et ses valeurs, `secure`), modes CPU (`host-passthrough`, `host-model` et son modèle, modèles nommés `usable='yes'`), bus de disque (`<disk><enum name='bus'>`), modèles vidéo, types d'affichage, TPM (`<tpm supported>`, `backendModel`), `memoryBacking`, `features` (SMM, SEV...).
- Lecture mise en cache par hôte et par version de libvirt et de QEMU (clé : `version` déjà relu par l'inventaire), relue au test de l'hôte et sur `?fresh=1`.

Choix concrets du XML produit (`kvm_xml.build_domain`) :

| Élément | Choix | Raison |
|---|---|---|
| Type de machine | `q35` par défaut (libvirt le fixe à la version au `define`), `pc` (i440fx) au choix ; liste lue dans `capabilities` | q35 : PCIe, SATA, Secure Boot ; i440fx pour les vieux invités |
| Micrologiciel | BIOS par défaut ; UEFI par **sélection automatique** `<os firmware='efi'>`, avec `<firmware><feature name='secure-boot'/><feature name='enrolled-keys'/></firmware>` pour Secure Boot ; chemins OVMF explicites (`<loader readonly='yes' type='pflash'>` + `<nvram template=...>`) seulement si `domcapabilities` n'annonce pas `firmware=efi` | aucun chemin OVMF codé en dur : chaque distribution les range ailleurs, libvirt les connaît par ses descripteurs |
| Secure Boot | exige q35, ajoute `<features><smm state='on'/>` | règle de QEMU, refus nommé sinon |
| CPU | `host-model` par défaut (migration à chaud de la version suivante), `host-passthrough`, ou un modèle nommé utilisable ; topologie `<topology sockets cores threads>` dont le produit égale le maximum de vCPU | `host-passthrough` bloque la migration vers un autre CPU, comme l'explique déjà la bulle Harvester |
| vCPU | `<vcpu placement='static' current='N'>MAX</vcpu>` ; quand « CPU et mémoire à chaud » est coché, une seule unité par socket (comme Harvester : sockets = MAX, cores = threads = 1) | `setvcpus --live` monte jusqu'à MAX |
| Mémoire | `<memory>` = plafond, `<currentMemory>` = valeur courante, `<memballoon model='virtio'>` | `setmem --live` jusqu'au plafond ; la mémoire enfichable (DIMM, virtio-mem) est reportée |
| Disques | `type='volume'` (`<source pool= volume=>`), `<driver name='qemu' type='qcow2' cache= discard='unmap'>` (ou `raw`), bus `virtio`, `sata` ou `scsi` (ce dernier ajoute un contrôleur `virtio-scsi`), ordre de démarrage par périphérique (`<boot order>`) | `volume` garde le lien au pool (utile pour la migration et pour la preuve de propriété de la suppression) |
| CD-ROM | `device='cdrom'`, `readonly`, SATA sur q35, IDE sur i440fx ; un lecteur est toujours créé (vide si aucune ISO) quand « garder un lecteur de CD-ROM » est coché (par défaut) | sur q35, un lecteur SATA ne s'ajoute pas à chaud, mais son média se change à chaud |
| Cartes réseau | `type='network'` (réseau libvirt), `type='bridge'` (pont de l'hôte) ou `type='direct'` (macvtap, modes `bridge`, `vepa`, `private`, `passthrough`) ; modèles `virtio`, `e1000e`, `e1000`, `rtl8139` ; MAC libre ou fournie | macvtap : l'hôte ne joint pas l'invité par cette carte, la bulle le dit |
| Emplacements à chaud | sur q35, `N` ports `pcie-root-port` libres en plus (4 par défaut, réglable 0 à 16, champ « emplacements pour l'ajout à chaud ») | sans port libre, `attach-device --live` échoue « no more available PCI slots » : piège classique de q35 |
| Affichage | `<graphics type='vnc' autoport='yes'><listen type='address' address='127.0.0.1'/></graphics>`, mot de passe facultatif **généré par la console** (8 caractères, la limite du protocole VNC), jamais affiché, résolu par `vnc_mux` (1.98.0) ; vidéo `virtio` par défaut, `vga` et `bochs` au choix d'après `domcapabilities` | même chemin de console que la 1.98.0, rien d'exposé sur le réseau |
| Agent invité | canal `org.qemu.guest_agent.0` quand « installer l'agent invité » est coché (comme Harvester, qui ajoute aussi `qemu-guest-agent` au cloud-init) | IP, arrêt doux par l'agent plus tard |
| Divers | console série `pty`, `<rng model='virtio'>` (`/dev/urandom`), tablette USB (case, cochée par défaut comme le « USB tablet pointer » de Harvester), horloge `utc` (Linux) ou `localtime` + éclairages Hyper-V (Windows) | préréglages par type d'OS, comme le champ « Operating system » de Harvester |
| TPM | `<tpm model='tpm-crb'><backend type='emulator' version='2.0'/>` seulement si `domcapabilities` l'annonce (paquet `swtpm` sur l'hôte) ; sinon la case est grisée avec la raison | aucun prérequis ajouté en silence |
| Métadonnées | `<metadata><hops:vm xmlns:hops="https://harvester-ops.io/xmlns/vm/1"><hops:os>linux</hops:os><hops:seed pool=... volume=.../></hops:vm></metadata>` | type d'OS relu par la modification ; volume cloud-init reconnu comme possédé par la VM. Espace de noms distinct de celui réservé aux dossiers (`.../xmlns/folder/1`) |
| Démarrage automatique | `virsh autostart` après le `define` si coché | réglage de l'hôte, hors XML |

L'uuid du domaine est tiré par la console (`uuid4`) et posé dans le XML (écart assumé à la tâche 2, voir « Création ») : le seed porte ainsi dès sa fabrication l'`instance-id` = uuid du domaine, sans `define` préalable avec un lecteur vide ; libvirt refuse un uuid déjà pris et le retour arrière ne retire qu'un domaine qui porte cet uuid. Pas de `<mac>` quand l'exploitant n'en donne pas (libvirt génère `52:54:00:...`). Les MAC fournies sont contrôlées contre les domaines de l'hôte (relevé de la 1.98.0) : un doublon sur le même hôte est refusé ; entre hôtes, rien n'est contrôlé et c'est dit dans la bulle.

### Disques d'une nouvelle VM

Trois sources, comme les sources `blank`, `image` et `pvc` de Harvester :

- **Vierge** : `vol-create-as --pool P --name N --capacity <octets> --format qcow2` (`--prealloc-metadata` au choix). Place disponible contrôlée avant (`pool-dumpxml` : `available`).
- **Depuis une image** : deux modes.
  - **Lié** (par défaut quand l'image est dans le même pool et en `qcow2` ou `raw`) : `vol-create-as --pool P --name N --capacity C --format qcow2 --backing-vol <image> --backing-vol-format <qcow2|raw>`. Rapide et économe, comme les images de base de Harvester. L'image devient alors utilisée : la suppression d'une image citée en image de base est refusée (`backing_users`, déjà écrit en 1.98.0).
  - **Copie complète** (au choix, et imposée quand le pool de destination diffère) : `vol-create-from` avec le XML de destination produit par `volume_xml()` (1.98.0, chaîne aplatie), puis `vol-resize` si la taille demandée dépasse la taille virtuelle de l'image. Écriture longue : connexion propre, étape avec octets copiés quand libvirt les donne (relecture de `vol-info` en parallèle).
  - Une ISO choisie comme « image » devient un CD-ROM de démarrage (ordre 1), et le premier disque vierge passe en ordre 2, comme le fait `withImageDisk()` de `vm-create.js`.
- **Volume existant** : un volume libre du pool (aucun domaine ne le cite, relevé de la 1.98.0), en lecture-écriture ; un volume déjà utilisé n'est proposé qu'en lecture seule avec la case `shareable`, et c'est dit.

Nom des volumes créés : `<vm>-<cible>.qcow2` (par exemple `kvmtest-1-vda.qcow2`), même convention que le clonage. Refus si un volume du même nom existe.

### Cloud-init : ISO NoCloud fabriquée par la console

**Décision : l'ISO NoCloud est fabriquée par la console en Python pur, puis téléversée comme volume du pool par `vol-upload`.** Raisons :

1. Aucun outil de plus sur l'hôte : `genisoimage`, `xorriso` ou `cloud-localds` n'y sont pas garantis (souvent absents des hyperviseurs de production, et la conception n'exige que libvirt et `qemu-img`).
2. Même résultat sur tous les hôtes, testable sur node1 sans hôte.
3. Le contenu (qui peut porter des mots de passe) ne passe jamais par un fichier temporaire de l'hôte ni par une ligne de commande : il part sur l'entrée standard de `vol-upload` et finit dans un volume `0600` géré par libvirt.

Ce qui est écrit (`bin/lib/iso9660.py`, nouveau) :

- Image ISO 9660 niveau 1, étiquette de volume `cidata` (exigée par la source NoCloud de cloud-init), avec une **extension Joliet** (descripteur supplémentaire, noms UCS-2) pour que les noms `user-data`, `meta-data`, `network-config` (et `vendor-data` s'il est fourni) soient lus tels quels. Linux monte Joliet par défaut quand Rock Ridge est absent, et cloudbase-init (Windows) lit Joliet. Noms ISO 9660 de repli `USER_DAT.;1`, `META_DAT.;1`, `NETWORK_.;1`.
- Pas de Rock Ridge ni d'El Torito (pas d'amorçage) : une centaine de lignes, aucune dépendance.
- `meta-data` : `instance-id` = uuid du domaine (tiré par la console et posé dans le XML avant le `define`, puis relu par la vérification finale), `local-hostname` = nom d'hôte invité. Garder le même `instance-id` lors d'une modification du cloud-init veut dire que cloud-init ne rejoue pas ses modules de premier démarrage, comme chez Harvester (« la VM le lit à son prochain démarrage », selon ce que cloud-init décide) ; une case « run cloud-init again at next boot » change l'`instance-id`.
- user-data et network-data viennent du même assistant que Harvester (`genUserData`, `genNetworkData`), clés SSH comprises ; network-data en format v1, que NoCloud lit sous le nom `network-config`.

Le volume s'appelle `<vm>-cidata.iso`, dans le pool du premier disque (ou celui choisi), branché sur un second lecteur de CD-ROM en lecture seule, et cité dans `<hops:seed>`. La suppression de la VM (1.98.0) apprend à le reconnaître comme possédé : un CD-ROM listé dans `<hops:seed>` et cité par aucun autre domaine est supprimable, avec la raison `cloud-init seed of this VM`. Le clonage ne recopie pas le seed : il le régénère avec le nouveau nom d'hôte si la source en a un (sinon le clone démarrerait avec l'identité de la source).

### Modifier : à chaud ou au prochain démarrage

Chaque champ du formulaire a une règle fixe, servie par l'API avec le formulaire (table `HOTNESS` de `kvm_edit.py`) pour que la page affiche avant l'envoi ce qui sera appliqué tout de suite et ce qui attendra :

| Changement | VM en marche | Commande |
|---|---|---|
| Nombre de vCPU, en hausse jusqu'au maximum | maintenant | `setvcpus --live --config --count N` |
| Nombre de vCPU, en baisse | maintenant si QEMU accepte le retrait (vCPU enfichables), sinon au prochain démarrage, et c'est dit | `setvcpus --live --config`, repli `--config` |
| Mémoire courante, jusqu'au plafond | maintenant (ballon) ; après l'appel, `dommemstat` relit `actual` : si l'invité n'a pas suivi (pas de pilote de ballon), le résultat le dit | `setmem --live --config` |
| Maximum de vCPU, plafond de mémoire, topologie, modèle et mode CPU, type de machine, micrologiciel, Secure Boot, TPM, modèle vidéo, préréglage d'OS, emplacements pour l'ajout à chaud, cache et bus d'un disque existant | prochain démarrage | `define` ciblé |
| Ajouter un disque `virtio` ou `scsi` (avec contrôleur présent) | maintenant | `attach-device --live --config --file /dev/stdin` |
| Ajouter un disque `sata`, ou `scsi` sans contrôleur | prochain démarrage (pas d'enfichage à chaud SATA sur QEMU) | `attach-device --config` |
| Retirer un disque | maintenant pour `virtio`/`scsi` : la demande part, puis la console relit le XML vivant jusqu'à 30 s ; si l'invité n'a pas libéré le périphérique, « the guest has not released the disk yet: it is removed from the configuration and will be gone at next boot » | `detach-device --live --config` |
| Changer ou éjecter le média d'un CD-ROM | maintenant | `update-device --live --config --file /dev/stdin` (source `volume`), `change-media --eject` |
| Ajouter une carte | maintenant si un port PCIe est libre (q35) ou sur i440fx ; sinon prochain démarrage, avec la raison | `attach-device --live --config` |
| Retirer une carte | maintenant (désignée par sa MAC) | `detach-interface --type T --mac M --live --config` |
| Titre, description | maintenant | `desc --live --config --title` / `--new-desc` |
| Démarrage automatique | maintenant | `autostart [--disable]` |
| Ajouter un affichage VNC (VM qui n'a que SPICE, promesse de la 1.98.0), mot de passe VNC posé ou retiré | prochain démarrage | `define` ciblé |
| Renommer | VM arrêtée seulement, sans instantané (libvirt refuse sinon) | `domrename` |

Règles :

- **VM arrêtée** : tout passe par un seul `define` ciblé (`--config` partout), sauf le renommage (`domrename`) et l'autostart.
- **VM en marche** : les changements « maintenant » sont appliqués un par un, en `--live --config` pour que la configuration persistante suive ; les changements « prochain démarrage » sont regroupés dans un seul `define` ciblé du XML inactif. Le résultat rend une ligne par changement : `{field, applied: "now" | "next-boot" | "refused", reason}`. La fenêtre affiche les mêmes trois états, comme le bandeau « Changes apply at the next VM restart » de Harvester, en précisant pour KVM qu'**un redémarrage dans l'invité ne suffit pas** : il faut un arrêt puis un démarrage (le processus QEMU doit être recréé).
- **Changements en attente** : la lecture d'une VM compare le XML vivant et le XML inactif sur les champs gérés et rend `pending: [champs]`. La liste des VMs affiche un badge « restart needed », comme la condition `RestartRequired` de Harvester (`restart_required` du menu).
- **Ordre de démarrage** : si le XML existant utilise `<os><boot dev=...>`, un ordre par périphérique demandé par le formulaire retire les `<boot dev>` (libvirt refuse les deux à la fois) ; sinon on n'y touche pas.
- **Domaines transitoires** : modification refusée avec la raison (« transient VM: its definition disappears at shutdown, so there is nothing to edit; make it persistent first ») ; les gestes à chaud `--live` seuls restent possibles sur eux (sans `--config`). Le geste « rendre persistante » (`define` du XML vivant) est une question ouverte.
- **Champs non gérés** : la fenêtre les montre en lecture seule (épinglage, temps réel, `hostdev`, `numatune`) avec « kept as is: not managed by the console ».
- **Une erreur au milieu** : les changements déjà appliqués restent (libvirt n'a pas de transaction) ; le résultat les nomme et dit ce qui n'a pas été fait. Jamais de retour arrière automatique d'un `setmem` ou d'un `attach-device` réussi.

### Catalogue d'images

- **Ce qu'est une image** : un volume d'un **pool d'images** de l'hôte. La déclaration de l'hôte gagne un champ facultatif `image_pools: [images, iso]` (contrôlé par `check_decl`, réglable dans la fenêtre de l'hôte, administrateur). Sans pool d'images déclaré, le formulaire propose les volumes libres de tous les pools, regroupés par pool. Pas de convention de nommage imposée aux volumes existants, rien n'est écrit dans libvirt pour marquer une image.
- **Type** : `vol-dumpxml` (format `qcow2`, `raw`, `iso` quand libvirt le détecte) ; une ISO que libvirt annonce `raw` est reconnue par la signature `CD001` à l'octet 32769 (`vol-download --offset 32768 --length 2048`, 2 Kio lus par libvirt avec ses droits) ou, à défaut, par l'extension `.iso`.
- **Notes de la console** : `kvm-images/<hôte>.json` dans le répertoire d'état (somme SHA-256, URL d'origine, date, auteur du téléversement, par `pool/volume`). C'est une information, pas une copie de la définition : un volume disparu de libvirt disparaît du catalogue ; une note sans volume est ignorée.
- **Téléverser depuis le navigateur** : reçu en flux sur le disque de la console comme l'archive de mise à jour (`_receive_archive`, `request.stream`, jamais en multipart qui passerait par un `tmpfs`), contrôle de place (507), SHA-256 calculé pendant la réception et comparé à celui que l'exploitant a pu saisir. Puis la CLI `vol upload` envoie le fichier : `vol-create-as --format raw --capacity <taille>` (libvirt sonde ensuite le format réel au `pool-refresh` d'un pool `dir`) et `vol-upload --pool P --vol V /dev/stdin` sur une **connexion propre**, par blocs de 4 Mio écrits dans le tuyau de `ssh`, progression (octets, débit, temps restant) une fois par seconde, débit plafonnable (`--limit-mbps`). Annulation : `ssh` arrêté, volume partiel supprimé par `vol-delete`, et le résultat le dit. Plusieurs hôtes d'un coup : le même fichier reçu une fois, envoyé hôte après hôte, puis supprimé de la console. Un qcow2 dont l'en-tête annonce une taille virtuelle au-delà de 16 Tio (`MAX_VIRTUAL_SIZE`, réglable par l'appelant) est refusé avant tout envoi : il remplirait le pool à la première écriture de l'invité.
- **Télécharger depuis une URL** : **par la console**, en flux, avec le magasin d'ISO existant (`/api/iso/fetch` : `http`/`https` seulement, redirections bornées, taille annoncée contrôlée), puis le même envoi. Raisons : les hyperviseurs de production n'ont souvent pas d'accès sortant, `curl` n'y est pas un prérequis, la somme est contrôlée une seule fois avant d'écrire sur un hôte, et le même fichier sert à plusieurs hôtes. Contrepartie : le fichier traverse la console. Le téléchargement par l'hôte (`curl | virsh vol-upload /dev/stdin`) est une question ouverte.
- **Contrôle après envoi** : par défaut, `virsh vol-download --pool P --vol V /dev/stdout | sha256sum` sur l'hôte (libvirt lit avec ses droits ; `sha256sum` de coreutils), comparé à la somme reçue. Si `sha256sum` manque sur l'hôte, le contrôle est sauté et c'est dit (pas de rapatriement de l'image entière). Bouton « vérifier » sur une image du catalogue pour refaire ce contrôle.
- **Sécurité d'une image qcow2 téléversée** : l'en-tête est lu par la console avant l'envoi (`bin/lib/qcow2.py`, Python pur : signature `QFI\xfb`, version, `backing_file_offset`, bits `incompatible_features`). Refus d'une image qui déclare un fichier de base (elle ferait lire à QEMU un fichier choisi par l'auteur de l'image) ou un fichier de données externe (bit `external data file`). Même contrôle sur une image téléchargée.

### Pools, volumes, réseaux

Pools (administrateur, sauf rafraîchir) :

- **Créer** : types `dir` (`pool-define` d'un XML produit par la console, `pool-build`, `pool-start`, `pool-autostart` au choix) et `netfs` NFS (`<source><host name=.../><dir path=.../><format type='nfs'/></source>`). Chemin cible absolu, normalisé, refusé s'il vaut ou contient `/`, `/boot`, `/dev`, `/etc`, `/proc`, `/run`, `/sys`, `/usr`, `/var/lib/libvirt/qemu`, `/var/run`, ou s'il est la cible d'un autre pool (un même fichier vu par deux pools fausserait la preuve de propriété). Imbrication (vu sur node2 : 7 pools sur 9 sous `/var/lib/libvirt/images`, lui-même un pool) : acceptée seulement entre deux pools `dir` (libvirt ne descend pas dans les sous-répertoires ; le pool parent voit le répertoire de l'enfant comme un volume de format `dir`, que la console ne liste jamais dans le catalogue et refuse de supprimer) ; tout autre type (`netfs`, `fs`, `logical`...) qui contient la cible d'un pool ou s'y trouve est refusé, un montage cacherait les fichiers de l'autre. Les chemins se comparent par texte ET par identité (`stat -L -c %d:%i` du chemin et de chacun de ses parents) : un lien vers le répertoire d'un pool, ou vers `/etc`, est le même répertoire. Les autres types (`logical`, `iscsi`, `rbd`, `zfs`...) restent en lecture seule.
- **Démarrer, arrêter** (`pool-start`, `pool-destroy`, dit « stop » à l'écran : `pool-destroy` ne détruit rien, la bulle le précise) : arrêt refusé si un domaine en marche utilise un volume du pool.
- **Rafraîchir** (`pool-refresh`, opérateur) : après une copie faite hors de la console.
- **Supprimer** : refusé si un domaine (définition active ou inactive) cite un volume ou un chemin du pool (relevé de la 1.98.0, par texte et par identité), si la chaîne de sauvegarde d'un domaine ou d'un volume d'un autre pool passe par un fichier du pool, ou si une opération de la console écrit dedans ; une lecture manquée refuse (un fichier illisible pour refus de droits peut être accepté, décision 46). Par défaut `pool-destroy` puis `pool-undefine` : les fichiers restent. Case « also remove the empty directory » : `pool-delete` seulement si `vol-list` est vide (libvirt refuse un répertoire non vide). Jamais de suppression de volumes en masse par ce geste.

Volumes : créer (vierge, opérateur), téléverser (opérateur), redimensionner en hausse (`vol-resize`, refusé sur un volume utilisé par un domaine en marche : utiliser `blockresize` est une question ouverte), supprimer (administrateur). Redimensionner et supprimer un volume, et retirer l'ancien seed après sa régénération, reprennent EXACTEMENT la preuve de `_owned` (1.98.0) : chemin du volume lu d'abord, relevé neuf avec ce chemin en `extra`, `sharers` et chaînes (`backers`) comparés par périphérique:inode (un lien ou un second nom compte), refus sur stockage partagé (`shared-storage: cannot prove` : pool `netfs`, système de fichiers NFS...), refus d'un répertoire, lecture manquée refusée (seul un `Permission denied` peut être accepté par l'exploitant). La suppression se fait en plan puis exécution avec une empreinte (pool, nom, clé, chemin, identité) : un volume recréé sous le même nom entre l'aperçu et l'exécution donne `plan-changed`. Le catalogue d'images, lui, est une vue : il lit un relevé gardé par hôte (rafraîchi en arrière-plan au-delà de 20 s, refait sur place au-delà de 120 s, oublié à chaque écriture de la console), jamais utilisé pour décider d'une écriture.

Réseaux (administrateur) :

- **Créer** : réseau NAT, routé ou isolé (nom, pont laissé à libvirt ou nommé, sous-réseau IPv4, plage DHCP proposée de `.2` à `.254`, nom de domaine facultatif, autostart), ou réseau `bridge` vers un pont existant de l'hôte. Refus si le sous-réseau chevauche une adresse ou une route de l'hôte (`ip -j addr`, `ip -j route`, lus par le compte SSH) ou un autre réseau libvirt : un NAT `172.16.0.0/16` sur node2 couperait le LAN de la maison.
- **Démarrer, arrêter, autostart** : arrêt refusé si un domaine en marche y a une carte.
- **Supprimer** : `net-destroy` puis `net-undefine`, refusé si une définition de domaine (active ou inactive) le cite.
- « Y a une carte » / « le cite » : `type='network'` sur son nom, ou `type='bridge'` sur le pont que le réseau crée (NAT, routé, isolé : une carte branchée directement sur `virbrN` tombe avec lui). En mode `bridge`, le pont est celui de l'hôte et vit sans le réseau ; une carte `type='direct'` (macvtap) vise une carte de l'hôte : ni l'une ni l'autre ne dépend du réseau.
- **Ponts et cartes de l'hôte** (lecture) : `ip -j -d link show` (ponts, cartes physiques candidates à macvtap), sans `netcf` (souvent absent des libvirt récentes).
- Baux DHCP fixes (`net-update ... ip-dhcp-host`) : plus tard.

### Formulaire commun aux deux familles

Vision de ju : toutes les VMs et tous les hôtes dans une vue, deux familles (Harvester, KVM), des dossiers. La création ne doit donc pas être une copie de `vm-create.js`, mais un composant commun avec un adaptateur par famille. Le faire d'un coup risquerait la création Harvester, très couverte par les tests e2e ; on le fait en deux temps.

Dans cette version :

- **`web/static/js/vm-form.js` (nouveau)** : la coquille commune, extraite de `vm-create.js` et `vm-edit.js` sans changer leur comportement. En-tête (nom, nombre d'instances et suffixes, démarrer après création, agent invité, clés SSH, point de départ « modèle »), barre d'onglets à icônes (`SECTIONS`), pied (Créer, essai à blanc, enregistrer comme modèle, résultat), rendu du résultat ligne par ligne (« appliqué maintenant », « au prochain démarrage », « refusé »). Contrat d'un adaptateur :
  - `id` (`harvester` ou `kvm`) ;
  - `sections(ctx)` (onglets proposés) ;
  - `loadRefs(ctx)` (espaces de noms ou pools, réseaux, images, clés) ;
  - `renderSection(id, model, ctx)` et `readSection(id, el, model)` ;
  - `validate(model)` ;
  - `hotness(field, state)` ;
  - `submit(model, {dryRun})` et `apply(changes, {dryRun})` ;
  - `templates` (lister, charger, enregistrer).
- **`web/static/js/cloudinit-form.js` (nouveau)** : `CI_USER_SCHEMA`, `CI_NET_SCHEMA`, `genUserData`, `genNetworkData` et `renderCloudInit` déplacés de `vm-edit.js`, qui les réexporte pour ne rien casser. Pur, sans dépendance à KubeVirt : les deux familles l'utilisent. Le champ « SSH key (Harvester) » d'un utilisateur devient « SSH key » et lit sa liste par l'adaptateur.
- **Les éditeurs à cartes** (disques, cartes réseau) gardent le moteur `TFForm` ; KVM a ses propres schémas (`KVM_DISK_SCHEMA`, `KVM_NIC_SCHEMA` dans `kvm-form.js`) aux mêmes noms de champs quand le sens est le même (`device`, `bus`, `boot_order`, `source` avec `image`, `blank` et `volume` au lieu de `pvc`, `size`, `cache`, `serial`, `readonly`, `shareable` ; `model`, `mac`, `boot_order`).
- **`web/static/js/kvm-form.js` (nouveau)** : l'adaptateur KVM, et les fenêtres « Créer une VM » et « Modifier » d'une VM KVM, construites sur `vm-form.js`.
- **`vm-create.js`** passe sur `vm-form.js` avec un adaptateur Harvester mince qui appelle le code actuel (`skeleton`, `merge`, `withImageDisk`, `VMEdit.wireSection`). Critère : tous les tests e2e existants de création et de modification passent sans être modifiés. Si ce critère ne tient pas dans la version, `vm-create.js` reste tel quel, l'extraction de `cloudinit-form.js` suffit, et c'est noté dans le CHANGELOG (Internal).
- **`vm-edit.js`** reste propre à Harvester dans cette version (2 600 lignes, huit sections liées au manifeste KubeVirt), à part le déplacement du cloud-init. Sa bascule sur l'adaptateur viendra avec la vue unique.

Plus tard (vue unique) : un seul bouton « Créer une VM » qui demande d'abord la cible (cluster et espace de noms, ou hôte KVM) et choisit l'adaptateur ; les modèles enregistrés en spec neutre pourront servir aux deux familles quand les champs le permettent.

**Clés SSH côté KVM** : il n'y a pas d'objet KeyPair sur un hôte libvirt. Le sélecteur propose les clés publiques des KeyPairs des clusters Harvester déclarés que la personne peut lire (API existante `/api/sshkeys/<cluster>`, regroupées par cluster) et un champ pour coller des clés. Un magasin de clés propre à la console est une question ouverte.

**Modèles côté KVM** : des specs du formulaire (pas du XML), gardées par la console dans `kvm-templates/<nom>.json` (répertoire d'état, 0600, sans mot de passe : les champs secrets sont retirés à l'enregistrement et le dit), utilisables sur tous les hôtes. « Enregistrer cette VM comme modèle » depuis une VM existante est reporté.

## Tableau de parité des champs

`ok` = même champ côté KVM, `adapté` = même intention avec la forme libvirt, `s.o.` = sans objet, `plus tard` = prévu après cette version.

| Harvester (création ou modification) | KVM | Note |
|---|---|---|
| Nom | ok | nom libvirt, `check_new_vm_name` (pas de `/`, pas de point en tête) |
| Namespace | adapté : hôte, puis pool par défaut des disques | |
| Nombre d'instances, suffixes `-01` | ok | un spec par instance, disques et seed propres |
| Démarrer après création | ok | `start` après le `define` |
| Installer l'agent invité | ok | canal virtio + paquet dans le cloud-init |
| Clés SSH | adapté | KeyPairs des clusters + clés collées |
| Partir d'un modèle | adapté | modèles de la console |
| Enregistrer comme modèle | adapté | idem |
| Nom affiché | adapté : `<title>` | `desc --title` |
| Description | ok : `<description>` | |
| Système d'exploitation | adapté : préréglage (horloge, Hyper-V, bus et modèles par défaut), gardé dans `<hops:os>` | |
| Stratégie de maintenance | plus tard | avec la maintenance d'un hôte |
| Stratégie de marche (`runStrategy`) | adapté : démarrage automatique de l'hôte, `on_poweroff`, `on_reboot`, `on_crash` | |
| Nom d'hôte invité | ok | `local-hostname` du seed |
| Tags, labels, annotations | plus tard | avec les dossiers et étiquettes de la vue unique (métadonnées `hops`) |
| Sockets, cœurs, threads | ok | |
| Mémoire | ok | courante et plafond |
| Mémoire réservée | s.o. | propre à KubeVirt |
| CPU et mémoire à chaud, maximums | ok | une unité par socket, `current` < max |
| Modèle de CPU | ok | `host-model`, `host-passthrough`, modèle nommé |
| Épinglage, thread émulateur isolé, NUMA | plus tard | montrés en lecture seule et gardés tels quels |
| Réservations d'ordonnancement | s.o. | |
| Mode de démarrage BIOS, UEFI, Secure Boot | ok | sélection automatique du micrologiciel |
| TPM 2.0, état persistant | ok si l'hôte a `swtpm` | l'état d'un TPM émulé est toujours persistant |
| Type de machine | ok | liste de `capabilities` |
| Numéro de série du micrologiciel | adapté : `<sysinfo>` `system/serial` | à la création seulement (non modifiable dans cette version) |
| Console série, affichage VNC | ok | VNC en boucle locale, mot de passe facultatif ; console série : à la création seulement (non modifiable dans cette version) |
| Ballon mémoire, tablette USB | ok | ballon : à la création seulement (non modifiable dans cette version) ; tablette modifiable |
| Chien de garde | ok : `<watchdog model='i6300esb' action=...>` | à la création seulement (non modifiable dans cette version) |
| Passage PCI, GPU | plus tard | `hostdev` gardés intacts |
| Disque : nom | adapté : cible (`vda`, `sdb`) | |
| Disque : périphérique disque ou CD-ROM | ok | |
| Disque : bus | ok | virtio, sata, scsi (d'après `domcapabilities`) |
| Disque : ordre de démarrage | ok | |
| Disque : source pvc, image, vierge | adapté : volume existant, image (lié ou copie), vierge | non modifiable sur un disque existant (refusé avec la raison : retirer le disque et en ajouter un) ; seul le média d'un CD-ROM se change |
| Disque : taille | ok | octets ; au moins la taille virtuelle de l'image |
| Disque : storage class | adapté : pool | |
| Disque : numéro de série, cache, partageable, lecture seule | ok | |
| Disque : thread d'E/S dédié | ok : `<iothreads>` + `iothread=` | à la création seulement (non modifiable dans cette version) |
| Carte : nom | s.o. | désignée par sa MAC |
| Carte : attachement bridge, masquerade | adapté : réseau libvirt, pont de l'hôte, macvtap | |
| Carte : réseau (multus) | adapté : réseau libvirt ou pont | |
| Carte : modèle, ordre de démarrage, MAC | ok | |
| Carte : IP statique | adapté | network-config du seed (pas d'IPAM côté libvirt) |
| Cloud-init : user-data, network-data, assistant | ok | même composant |
| Fichier de réponses Windows | adapté : ISO `autounattend.xml` fabriquée par le même écrivain ISO 9660, sur un CD-ROM SATA | plus tard si le temps manque (question ouverte) |
| Volumes virtiofs (ConfigMap, Secret) | s.o. | |
| Placement : sélecteur de nœud, affinité, tolérances | s.o. | l'hôte est choisi à la création ; migration dans la version suivante |
| Délai d'arrêt ACPI | adapté : délai de l'action d'arrêt de la console (réglage de la 1.96.0) | |
| Stratégie d'éviction | s.o. | |
| Geste : CPU et mémoire à chaud | ok | |
| Geste : ajouter, retirer un volume | ok | |
| Geste : insérer, éjecter une image | ok | |
| Geste : ajouter, retirer une carte | ok | |
| Geste : migration de stockage | plus tard | `blockcopy`, avec la migration |
| Geste : quota d'instantanés | s.o. | |
| Geste : accès (mot de passe, clés par l'agent) | plus tard | `set-user-password` et `set-user-sshkeys` de l'agent invité |
| Geste : YAML | adapté : XML en lecture (déjà dans le détail), modification de l'XML brut | plus tard (risque d'écraser les réglages non gérés) |

## Tâche 1 : bibliothèque pure (capacités, XML, ISO, qcow2)

Fichiers : nouveaux `bin/lib/kvm_caps.py`, `bin/lib/kvm_xml.py`, `bin/lib/iso9660.py`, `bin/lib/qcow2.py` ; tests `tests/api/test_kvm_caps.py`, `test_kvm_xml.py`, `test_iso9660.py`, `test_qcow2.py` ; jeux d'essai relevés sur node2 dans `tests/fixtures/kvm/` (`domcapabilities-q35.xml`, `domcapabilities-pc.xml`, `ip-link.json`, `ip-addr.json`, `ip-route.json`) et dérivés écrits à la main, nommés comme tels (`domcapabilities-q35-no-efi-autoselect.xml`, `domcapabilities-q35-no-tpm.xml`), et golden files `tests/fixtures/kvm/golden/*.xml`.

`kvm_caps` :
- `parse_capabilities(text) -> {arch, machines: [{name, canonical, max_cpus}], domain_types}`.
- `parse_domcapabilities(text) -> {machine, firmware_autoselect: bool, loaders: [chemins], secure_boot: bool, cpu_modes: {host_passthrough, host_model: modèle|None, custom: [modèles utilisables]}, disk_buses, video_models, graphics_types, tpm: {models, backends}|None, max_vcpus}`.
- `form_options(caps, domcaps_by_machine) -> dict` : ce que le formulaire propose, avec la raison de chaque option grisée (`"tpm": "the host has no TPM emulator (swtpm)"`).

`kvm_xml` :
- `VmSpec` (dict validé par `check_spec(spec, options) -> list[raisons]`) : `name`, `title`, `description`, `os` (`linux`, `windows`, `other`), `machine`, `firmware` (`bios`, `efi`), `secure_boot`, `tpm`, `cpu` (`mode`, `model`, `vcpus`, `max_vcpus`, `sockets`, `cores`, `threads`, `hotplug`), `memory` (`current_mib`, `max_mib`, `balloon`), `disks[]` (`target`, `device`, `bus`, `boot_order`, `source` : `{kind: blank|image|volume|iso|empty, pool, volume, image, linked, size_bytes, format}`, `cache`, `serial`, `readonly`, `shareable`, `io_thread`), `nics[]` (`kind: network|bridge|direct`, `source`, `mode`, `model`, `mac`, `boot_order`), `display` (`vnc`, `password: bool`, `video`), `guest_agent`, `serial_console`, `tablet`, `rng`, `watchdog`, `hotplug_slots`, `autostart`, `on_poweroff`, `on_reboot`, `on_crash`, `sysinfo_serial`, `cloudinit` (`user_data`, `network_data`, `seed_pool`).
- `build_domain(spec, options, volumes, vnc_password=None) -> str` : le XML de création (tableau des choix ci-dessus). Cibles attribuées par bus (`vda`, `vdb`... ; `sda`... pour SATA et SCSI), contrôleurs ajoutés seulement quand un bus l'exige (`virtio-scsi`), `pcie-root-port` en nombre `hotplug_slots` sur q35.
- `device_xml(kind, item) -> str` : un `<disk>` ou un `<interface>` seul, pour `attach-device` et `update-device`.
- Modifications ciblées (`kvm_xml.Edit`, sur l'arbre inactif) : `set_vcpus(current, max, topology)`, `set_memory(current, max)`, `set_cpu(mode, model)`, `set_machine`, `set_firmware(kind, secure_boot)` (passage BIOS vers UEFI : retire `<loader>`/`<nvram>` explicites seulement s'ils ont été posés par une sélection automatique ; sinon refus nommé « firmware set by hand: change it outside the console »), `set_tpm`, `add_disk`, `remove_disk(target)`, `set_disk(target, bus, cache, readonly...)`, `add_nic`, `remove_nic(mac)`, `set_boot_order(map)`, `set_display(vnc, password)`, `set_title`, `set_description`, `set_lifecycle`, `set_metadata_os`, `set_seed`. Chaque fonction ne touche que ses éléments et leurs attributs ; espaces de noms des `<metadata>` gardés (`_register_namespaces` de la 1.98.0).
- `managed_view(xml) -> dict` : la VM vue par le formulaire (ce qu'il sait relire), plus `unmanaged: [réglages gardés tels quels]`.
- `diff(view_inactive, view_live) -> pending` et `changes(view_before, spec_after) -> [changement]` (champ, ancienne, nouvelle valeur).
- `canonical_digest(xml) -> sha256` du XML inactif canonique (C14N 2.0 de `ElementTree.canonicalize`, `passwd` retiré avant le calcul pour qu'il n'entre jamais dans une empreinte transmise).

`iso9660` :
- `build_iso(files: dict[nom, bytes], label="cidata", joliet=True) -> bytes` : niveau 1, Joliet, tailles et dates fixes (`SOURCE_DATE_EPOCH` ou date passée en paramètre) pour une sortie reproductible.
- `read_iso(data) -> {label, files: dict[nom Joliet, bytes]}` : lecteur minimal pour les tests.
- `seed_files(user_data, network_data, instance_id, hostname, vendor_data=None) -> dict`.

`qcow2` :
- `header(first_bytes) -> {version, virtual_size, backing_file: bool, external_data_file: bool, encrypted: bool}` ; `check_upload(first_bytes) -> list[raisons]`.

Tests :
- **Golden files** : un spec par cas (`linux-bios-q35`, `linux-uefi-secure-tpm`, `windows-uefi`, `pc-i440fx-ide-cdrom`, `three-disks-mixed-buses`, `nics-network-bridge-direct`, `vnc-password`, `hotplug-cpu-mem`, `cloudinit-seed`) produit avec les capacités réelles de node2 et comparé au fichier attendu ; régénération par `UPDATE_GOLDEN=1` seulement.
- **Schéma libvirt** : chaque golden file passé à `virt-xml-validate <fichier> domain` quand l'outil est présent sur la machine de test (paquet `libvirt-client` ; sinon le test est sauté et le dit). La validation réelle par `define --validate` est faite en tâche 7.
- `form_options` en tableau : sans sélection automatique (chemins `loader` explicites), sans TPM, sans Secure Boot, hôte sans `virtio-scsi`, i440fx sans Secure Boot.
- `check_spec` : un cas par refus (topologie qui ne fait pas le maximum, mémoire courante au-dessus du plafond, Secure Boot sur `pc`, deux disques sur la même cible, MAC invalide, MAC en double, ordre de démarrage en double, taille sous la taille virtuelle de l'image, nom avec `/`).
- Modifications ciblées : chaque `Edit` sur `dumpxml-netperf.xml` laisse épinglage, temps réel, cartes `direct` et `<seclabel>` identiques (C14N) ; modification vide = XML identique et aucun `define` ; `<boot dev>` retiré seulement quand l'ordre par périphérique est demandé ; métadonnées libosinfo gardées ; `passwd` hors de l'empreinte.
- `iso9660` : aller-retour `build_iso`/`read_iso` ; étiquette `cidata` lue dans le descripteur primaire ; noms Joliet `user-data`, `meta-data`, `network-config` ; sortie identique deux fois de suite ; lecture par `xorriso -indev <f> -find` et `isoinfo -J -l` quand ils existent (sautés sinon) ; contenu UTF-8 et fichier vide ; fichier de 2 Mio (plusieurs blocs).
- `qcow2` : en-têtes fabriqués (v2, v3, avec image de base, avec fichier de données externe, chiffré, pas un qcow2).
- Mutations volontaires : `build_domain` qui oublie les `pcie-root-port` ; `Edit.set_vcpus` qui régénère `<cputune>` ; `build_iso` sans Joliet (le test des noms doit échouer) ; `check_upload` qui ignore `backing_file_offset`.

## Tâche 2 : opérations (création, modification, gestes à chaud, images, pools, réseaux)

Fichiers : `bin/lib/kvm_host.py` (transport), nouveaux `bin/lib/kvm_create.py`, `bin/lib/kvm_edit.py`, `bin/lib/kvm_storage.py`, `bin/lib/kvm_net.py`, `bin/lib/kvm_ops.py` (seed possédé reconnu par `owned_disks` et `delete_plan`, seed régénéré au clonage), `bin/lib/kvm_decl.py` (`image_pools`) ; tests `tests/api/test_kvm_create.py`, `test_kvm_edit.py`, `test_kvm_storage.py`, `test_kvm_net.py`, cas ajoutés dans `test_kvm_ops_197.py` et `test_kvm_decl_196.py` (aucun cas existant modifié).

Transport (`kvm_host`) :
- `VirshHost.run_stream(remote_argv, source, size, progress=None, cancel=None, limit_bps=None) -> str` : `subprocess.Popen` de `ssh_argv(..., fresh=True)`, `stdin=PIPE` binaire, écriture par blocs de 4 Mio, `progress(octets)` au plus une fois par seconde, `cancel()` consulté entre deux blocs (fermeture de l'entrée, SIGTERM, 2 s, SIGKILL), plafond de débit par seau à jetons. Erreurs classées comme `run()`, chemins et secrets expurgés.
- `VirshHost.run_capture_bytes` n'est pas ajouté : les lectures d'octets (`CD001`) se font par `sh -c 'virsh ... vol-download ... /dev/stdout | od -An -c -N 6'` côté hôte et rendent du texte.

Création (`kvm_create`) :
- `create_plan(host, spec) -> dict` (lecture seule, aussi `--dry-run`) : capacités relues, `check_spec`, nom libre (`dominfo` = `notfound`) pour chaque instance, pools actifs et place disponible (somme des volumes à créer par pool, liées comptées à leur taille initiale), images présentes et de format connu, réseaux actifs (un réseau inactif est accepté avec l'avertissement « the network is inactive: the VM will not start until it is started »), MAC libres, volumes de destination absents. Rend `{possible, refusals, warnings, instances: [{name, volumes: [...], xml_preview}]}` ; le `xml_preview` porte `passwd='***'`.
- `create_vm(host, spec, progress, cancel)` : pour chaque instance, dans l'ordre : absence des volumes à créer revérifiée (une instance précédente a pu prendre des minutes), volumes (`vol-create-as` ou `vol-create-from` sur connexion propre, octets copiés relus par `vol-info` toutes les 2 s pendant une copie complète), uuid tiré par la console, seed (`iso9660.build_iso` avec `instance-id` = cet uuid, puis `vol-create-as --format raw` + `run_stream(vol-upload)`), `define --validate /dev/stdin` du XML qui porte l'uuid, `autostart`, `start` si demandé, relecture (uuid comparé). Étapes : `plan`, `volume-<cible>`, `seed`, `define`, `start`, `verify`. (Livré ainsi : la première version du plan prévoyait un `define` avec lecteur vide puis `update-device`, une écriture de plus et une VM un moment sans seed.)
- Retour arrière sur échec ou annulation, au mieux et dans l'ordre inverse : `destroy` si démarrée, `undefine --nvram` si définie, `vol-delete` des volumes créés par cette action (jamais d'un volume existant choisi comme source, ni d'un volume que libvirt refuse de créer parce qu'il existe déjà : créé ailleurs entre le plan et l'écriture). Ce qui reste est nommé (`leftovers`). Pour plusieurs instances : une instance réussie reste ; le résultat liste les réussites et l'échec.

Modification (`kvm_edit`) :
- `HOTNESS` : la table de la section « Modifier » (champ, règle, commande), servie à la console.
- `edit_plan(host, name, spec, expect_digest) -> dict` : relit `dumpxml --inactive --security-info` et `dumpxml` vivant, contrôle l'empreinte, calcule `changes()`, classe chaque changement (`now`, `next-boot`, `refused` avec raison : domaine transitoire, VM en pause ou en arrêt en cours, emplacement PCIe absent, bus non enfichable, plafond dépassé, renommage d'une VM en marche ou avec instantanés).
- `apply_edit(host, name, plan, progress, cancel)` : gestes `now` un par un, puis un seul `define --validate` du XML inactif modifié pour les `next-boot` ; relecture finale, résultat par changement ; `detach-device` suivi jusqu'à 30 s.
- Gestes à chaud (mêmes fonctions, un changement chacun) : `set_cpu_memory`, `attach_disk` (crée le volume vierge ou depuis une image si demandé, puis l'attache ; retour arrière du volume créé si l'attache échoue), `detach_disk` (ne supprime jamais le volume ; la suppression d'un volume est un geste de stockage, administrateur), `insert_media`, `eject_media`, `attach_nic`, `detach_nic`.
- `regenerate_seed(host, name, user_data, network_data, rerun)` : nouvelle ISO téléversée sous un nom neuf (`<vm>-cidata-<n>.iso`), média changé (à chaud si la VM tourne), ancien volume supprimé après le changement réussi, `<hops:seed>` mis à jour.

Images et stockage (`kvm_storage`) :
- `list_images(host, decl, notes)` : volumes des pools d'images (ou libres de tous les pools), format, taille virtuelle, ISO reconnue, somme connue, utilisé comme image de base par (`backing_users`).
- `upload_volume(host, pool, name, path, fmt_hint, sha256, verify, progress, cancel, limit_bps)` : contrôle de nom (`VOLUME_RE = ^[A-Za-z0-9][A-Za-z0-9._+-]{0,250}$`, ni `/` ni `..`), refus si le volume existe, contrôle de place, `qcow2.check_upload` sur les 64 premiers Kio, `vol-create-as --format raw --capacity <taille>`, `run_stream(vol-upload --pool P --vol V /dev/stdin)`, `pool-refresh` (format sondé), `vol-dumpxml` relu (format attendu, capacité), contrôle SHA-256 sur l'hôte si demandé, note écrite. Annulation et échec : `vol-delete` du volume partiel.
- `create_volume`, `resize_volume`, `delete_volume` (refus nommés de la 1.98.0 : cité par `<vm>`, image de base de `<volume>`, illisible).
- Pools : `define_pool(host, spec)` (XML produit par la console, chemins contrôlés, chevauchements refusés), `start_pool`, `stop_pool` (refus si un domaine en marche l'utilise), `autostart_pool`, `refresh_pool`, `delete_pool(host, name, remove_dir)` (refus si utilisé ; `pool-delete` seulement si vide).

Réseaux (`kvm_net`) :
- `host_links(host)` : `ip -j -d link show` (ponts, cartes physiques), `ip -j addr`, `ip -j route` dans un seul lot shell.
- `network_xml(spec) -> str` (NAT, routé, isolé, bridge), `check_network(spec, links, networks) -> raisons` (chevauchement, nom de pont pris, plage DHCP hors du sous-réseau).
- `define_network`, `start_network`, `stop_network` (refus si un domaine en marche y a une carte), `autostart_network`, `delete_network` (refus si une définition le cite).

Déclaration (`kvm_decl`) : `image_pools` facultatif, liste de noms de pools (chaînes sans caractère de contrôle, 32 au plus), contrôlé par `check_decl` ; une ancienne déclaration sans le champ reste valide (test).

Tests (faux hôte `SimNet` de la 1.98.0, étendu à `vol-create-as`, `vol-upload`, `define --validate`, `attach-device`, `detach-device`, `update-device`, `setvcpus`, `setmem`, `pool-*`, `net-*`) :
- argv exact de chaque commande virsh, en particulier `--backing-vol` et `--backing-vol-format` d'un disque lié, `/dev/stdin` pour chaque XML, `--live --config` des gestes à chaud, `--config` seul pour une VM arrêtée ;
- table `HOTNESS` jouée ligne par ligne sur une VM arrêtée, en marche, en pause, transitoire (résultat attendu pour chaque champ) ;
- création de trois instances dont la deuxième échoue au `define` : première gardée, volumes de la deuxième supprimés, troisième pas commencée, `leftovers` vide ;
- annulation par SIGTERM pendant le téléversement (vrai sous-processus contre un `ssh` factice sur `PATH`, comme le test `host add` de la 1.96.0) : sortie 3, `vol-delete` du partiel, aucun fichier temporaire de la console restant ;
- `run_stream` contre un vrai processus local (`sh -c 'cat > fichier'`) : octets identiques, progression monotone, plafond de débit respecté à 20 % près, coupure du processus au milieu classée ;
- pools et réseaux : chaque refus (chemin interdit, chevauchement de pool, pool utilisé par une VM inactive, réseau utilisé, sous-réseau qui chevauche `172.16.0.0/16` du relevé `ip-addr.json`) ;
- seed : reconnu possédé par `owned_disks` (supprimé avec la VM), gardé s'il est aussi cité par une autre VM, régénéré au clonage avec le nouveau nom d'hôte ;
- noms piégés (`a;rm -rf /`, `$(id)`, espaces, `-x`, retour à la ligne) pour la VM, le volume, le pool, le réseau, le pont : un seul argument distant, jamais interprété ;
- aucun secret (mot de passe VNC factice, mot de passe d'utilisateur cloud-init factice, contenu de user-data) dans un `repr`, une erreur, une étape ou le JSON (test qui cherche les valeurs factices dans toutes les sorties) ;
- mutations volontaires : `vol-upload` sur la connexion partagée (le test vérifie `ControlMaster=no`) ; `delete_pool` sans le relevé des définitions inactives ; `apply_edit` qui rejoue une écriture après une coupure ; `create_vm` qui supprime un volume source existant au retour arrière.

## Tâche 3 : CLI `bin/harvester-kvm.py`

Fichiers : `bin/harvester-kvm.py`, `tests/api/test_kvm_cli_create.py`.

Sous-commandes (`--json`, `--state-dir`, valeurs en `--opt=valeur` côté console) :
- `host caps --host N [--machine=q35]` : options du formulaire, raisons des options grisées.
- `vm create --host N --spec-file=F [--dry-run] [--timeout S]` (3600 s par défaut) ; `F` est un JSON `VmSpec` (ou `-` pour l'entrée standard en usage manuel).
- `vm edit --host N --name D --spec-file=F --expect-config=DIGEST [--dry-run] [--timeout S]` (600 s).
- Gestes à chaud, noms de Harvester : `vm cpumem --host N --name D [--vcpus=N] [--memory-mib=M]`, `vm add-volume --host N --name D --spec-file=F` (un disque), `vm remove-volume --host N --name D --target=vdb`, `vm insert-cdrom --host N --name D --target=sda --pool=P --volume=V`, `vm eject-image --host N --name D --target=sda`, `vm add-nic --host N --name D --spec-file=F`, `vm remove-nic --host N --name D --mac=M`.
- `vm cloudinit --host N --name D --spec-file=F [--rerun]` (régénère le seed).
- `image list --host N`, `image verify --host N --pool=P --volume=V`.
- `vol create --host N --pool=P --name=V --capacity=BYTES [--format=qcow2] [--backing=IMG]`, `vol upload --host N --pool=P --name=V --file=PATH [--sha256=HEX] [--no-verify] [--limit-mbps=M] [--timeout S]` (sans délai par défaut : la progression fait foi), `vol resize --host N --pool=P --name=V --capacity=BYTES`, `vol delete --host N --pool=P --name=V [--dry-run]`.
- `pool define --host N --spec-file=F [--dry-run]`, `pool start|stop|refresh --host N --pool=P`, `pool autostart --host N --pool=P [--disable]`, `pool delete --host N --pool=P [--remove-dir] [--dry-run]`.
- `net define --host N --spec-file=F [--dry-run]`, `net start|stop --host N --network=R`, `net autostart --host N --network=R [--disable]`, `net delete --host N --network=R [--dry-run]`, `net links --host N`.

Chaque commande émet `plan`, ses étapes, relit l'état réel à la fin (`dominfo`, `vol-dumpxml`, `pool-info`, `net-info`) et rend un objet JSON (`created`, `changes` avec `applied`, `deleted`, `kept` avec raison, `leftovers`). Messages d'annulation ajoutés à `cancel_message()` : création « creation cancelled: rolled back » ou la liste de ce qui reste ; modification « some changes may already be applied: reload the VM » ; téléversement « upload cancelled: the partial volume was deleted » ou son nom s'il n'a pas pu l'être ; pool et réseau « may already have been done: check the list ».

Tests : argv exact de chaque sous-commande ; `--spec-file` lu puis jamais recopié dans la sortie (user-data absente du JSON et de stderr) ; usage invalide = JSON `{ok:false, kind:"usage"}` et sortie 2 ; noms piégés ; annulation réelle de `vol upload` et de `vm create` par SIGTERM ; `--dry-run` qui ne lance aucune écriture (faux hôte qui refuse toute écriture).

## Tâche 4 : routes de la console

Fichiers : `web/app.py`, tests `tests/api/test_kvm_routes_create.py`.

Lectures (lecteur, `@shared_read(scope=KVM_SCOPE)`) :
- `GET /api/kvm/hosts/<host>/caps?machine=q35` : `host caps`, mis en cache par version.
- `GET /api/kvm/hosts/<host>/images` : catalogue.
- `GET /api/kvm/hosts/<host>/links` : ponts et cartes.
- `GET /api/kvm/vms/<host>/<domain>/form` : `managed_view` + `unmanaged` + `pending` + `HOTNESS` + empreinte `config_digest` ; le mot de passe VNC n'y est que sous la forme `password: true`. Le cloud-init (user-data, network-data) est rendu à un opérateur seulement, comme `GET /api/vm/.../cloudinit`.
- `GET /api/kvm/templates`, `GET /api/kvm/templates/<name>`.

Mutations (actions suivies par `_kvm_action` ou `_kvm_vm_op`) :
- `POST /api/kvm/vms/<host>` `{spec, dry_run}` (opérateur, `10/minute`) : création ; le spec est écrit dans un fichier 0600 du répertoire de travail, passé en `--spec-file=`, supprimé par `cleanup`. Taille du corps bornée (1 Mio, 413 avant lecture).
- `PATCH /api/kvm/vms/<host>/<domain>` `{spec, expect_config, dry_run}` (opérateur, `10/minute`) : modification ; 409 `changed` si l'empreinte ne correspond plus.
- `POST /api/kvm/vms/<host>/<domain>/hot/<op>` (opérateur, `20/minute`), `op` dans `cpumem`, `add-volume`, `remove-volume`, `insert-cdrom`, `eject-image`, `add-nic`, `remove-nic`, `cloudinit`. Segments statiques avant la route `<gesture>` de la 1.96.0 (test qui le fixe, comme pour `snapshots` et `clone`).
- `POST /api/kvm/hosts/<host>/images/upload?pool=P&name=V&sha256=H&hosts=a,b` (opérateur, `10/minute`) : corps reçu en flux (`_receive_archive`), contrôle qcow2, puis `vol upload` par hôte dans la même action ; `POST /api/kvm/hosts/<host>/images/fetch` `{url, pool, name, sha256, hosts}` : magasin de téléchargement existant, puis même envoi. Le fichier reçu est rangé dans `<état>/kvm-images/staging/` (0600) et supprimé à la fin, réussite ou échec.
- `POST /api/kvm/hosts/<host>/images/<pool>/<volume>/verify` (opérateur, `5/minute`).
- `POST /api/kvm/hosts/<host>/volumes` (opérateur), `POST .../volumes/<pool>/<volume>/resize` (opérateur), `DELETE .../volumes/<pool>/<volume>` (administrateur).
- `POST /api/kvm/hosts/<host>/pools` (administrateur), `POST .../pools/<pool>/<verbe>` avec `start`, `stop`, `autostart`, `refresh` (`refresh` opérateur, le reste administrateur), `DELETE .../pools/<pool>` (administrateur).
- `POST /api/kvm/hosts/<host>/networks` (administrateur), `POST .../networks/<net>/<verbe>` (`start`, `stop`, `autostart`), `DELETE .../networks/<net>` (administrateur).
- `PUT /api/kvm/hosts/<host>/image-pools` `{pools}` (administrateur) : écrit `image_pools` dans la déclaration.
- `POST /api/kvm/templates` (opérateur), `DELETE /api/kvm/templates/<name>` (opérateur).

`_kvm_required_role` : table complétée (volumes supprimés, pools, réseaux et pools d'images = administrateur, sauf `refresh`) ; test de la table entière, comme en 1.98.0. Les paramètres de chemin s'appellent `host`, `domain`, `pool`, `volume`, `net` : hors de `_VALIDATED_PATH_PARAMS` (un nom libvirt n'est pas un nom Kubernetes), contrôlés par `_kvm_check_domain` ou `VOLUME_RE` dans chaque route. Les lectures rendent les hôtes en échec comme `/api/kvm/vms` (`{nom, état, erreur}`).

`/api/kvm/vms` ajoute `pending` (nombre de changements en attente) et, si la 1.98.0 ne l'a pas encore fait, les champs communs `kind`, `source`, `phase`, `cpu`, `memory_bytes`, `node`, `ips`, `labels`, `uuid`, `key` du plan 1.97 (« Vers une vue unique ») : resté ouvert à la livraison de la 1.98.0, il est repris ici puisque la création unifiée s'en sert.

Tests : rôles (lecteur refusé partout en écriture, opérateur refusé sur pools, réseaux et suppression de volume), limites valides (`limits.parse_many`), refus avant toute action (nom invalide, spec trop gros, empreinte absente), argv exact en `--opt=valeur`, fichier de spec 0600 supprimé après l'action (réussite, échec, annulation), occupation par VM (création vers un nom tenu par un clonage = 409), réception en flux d'une image (place insuffisante 507, corps interrompu, annulation), aucun secret dans les réponses ni dans `dev-server.log`.

Réalisé (tâche 4), écarts et précisions :

- Aperçus en routes à part, limités par personne (`KVM_PREVIEW_RATE`) et sans action : `POST /api/kvm/hosts/<hôte>/vm-plan` {spec}, `POST /api/kvm/vms/<hôte>/<domaine>/edit-plan` {spec, expect_config}, `GET .../volumes/<pool>/<volume>/delete-plan`, `GET .../pools/<pool>/delete-plan?remove_dir=1`, `GET .../networks/<réseau>/delete-plan`. `dry_run` reste accepté sur la création, la modification, la création d'un pool ou d'un réseau, et rend le même aperçu. Lectures ajoutées : `GET .../hosts/<hôte>/pools`, `GET .../hosts/<hôte>/networks`.
- Le cloud-init d'une VM se lit par `GET /api/kvm/vms/<hôte>/<domaine>/cloudinit` (opérateur, hors lecture partagée), pas dans `/form`.
- Toute suppression (volume, image, pool, réseau) exige `expect_delete` (400 sinon, rien lancé) ; un test énumère toutes les routes DELETE de `/api/kvm/` et refuse une route nouvelle non classée.
- Occupation par objet (`kvm_holds` : pool, volume, réseau, en partage ou en exclusif, comme les verrous du script) en plus de l'occupation par VM : 409 avant de lancer.
- Pools d'images et modèles passent par le script (`host image-pools`, `template save|delete`, nouvelles sous-commandes) : la console n'écrit rien elle-même, chaque écriture est une action suivie. Modèles dans `bin/lib/kvm_templates.py`.
- Téléchargement d'une URL : par la console, une seule fois pour tous les hôtes, puis `image upload --origin-url=` vers chacun. Garde d'adresses par un adaptateur au contrat de `url_fetch_guard` (1.99.0) : après la fusion de master il passe par elle, d'ici là par les règles du script (`REPLI-AVANT-1.99`). Réglages partagés avec le magasin d'ISO (`iso_fetch.allow_private`, `max_bytes`, `min_free_bytes`, relus à chaque demande). Aucun mandataire d'environnement, comme le script.
- Relevé gardé du catalogue : génération partagée par hôte dans `<état>/kvm.d/survey-gen/<hôte>`, avancée par toute écriture de la bibliothèque, par la CLI à la fin de toute commande qui écrit et par la console à la fin de chaque action KVM ; relue à chaque lecture du cache (voir la docstring de `kvm_ops.SurveyCache`).
- Pas encore fait : `pending` dans `/api/kvm/vms` (il faudrait deux `dumpxml` par VM en marche à chaque relevé ; disponible par VM dans `/form`).

Après la revue de la tâche 4 :

- Repli du téléchargement aligné sur la garde 1.99.0 (commit f7bb19c, `address_refusal` recopiée telle quelle dans le script) : URL avec identifiants refusée avant tout appel réseau, jamais recopiée ; redirection de https vers http refusée ; métadonnées de cloud, adresse non spécifiée, multicast et diffusion (255.255.255.255, ajoutée) toujours refusées, même couvertes par `iso_fetch.allow_private` ; la liste n'ouvre que bouclage, lien local hors métadonnées, privées, CGNAT, réservées. Une URL en http simple exige la SHA-256 attendue (console, garde partagée ou repli, et `image download-url` du script).
- Garde de livraison : dès que `url_fetch_guard` s'importe, le repli est inatteignable (`_kvm_load_fetch_guard`) ; un test et `package.sh` refusent une version où `web/url_fetch_guard.py` existe alors que `web/app.py` porte encore `REPLI-AVANT-1.99`. À la fusion de master : supprimer le repli.
- Place sur la console : chaque réception (téléversement, téléchargement) réserve sa taille annoncée sous un verrou ; la place libre moins ce que les autres réceptions du même système de fichiers doivent encore écrire doit garder `iso_fetch.min_free_bytes` (507 sinon, avec la place réservée par les autres). Revérifiée tous les 64 Mio pendant l'écriture : arrêt propre, fichier retiré. Réservations propres au processus de la console.
- Au démarrage de la console : les restes `up-*` / `dl-*` du répertoire de réception de plus de 24 h qu'aucune réception ne suit sont retirés.
- Lectures coûteuses (caps, links, pools, networks, form, cloudinit) limitées (`KVM_READ_RATE`, 60/minute) ; la clé de lecture partagée ne garde que les paramètres connus (`query_keys`) : `?x=hasard` ne force pas de relecture.
- La console tient les mêmes objets que les verrous du script, qui ont été complétés : suppression de volume ou d'image et vérification d'image tiennent le pool en partage (et le volume, en partage pour la vérification) ; une modification tient en partage les pools et réseaux de ses disques et cartes ; l'ajout de carte tient son réseau. 409 tôt au lieu d'un « busy » tardif du script.
- Modèles : un seul espace de noms partagé, chaque modèle note son propriétaire ; le remplacer ou le supprimer est refusé (403) à une autre personne, sauf un administrateur (`--replace-other`). Secrets retirés : user-data du cloud-init et toute valeur non vide d'une clé contenant password, passwd, secret, token, key, credential ou private (casse ignorée). Actions sous l'étiquette `kvm-templates` (clé d'objet `@templates`), sans compteur de génération de relevés.
- La création refuse un `name` absent ou vide (400) avant de fabriquer les noms d'instances.
- Compteur de génération neuf ou abîmé : il repart d'une valeur prise sur l'horloge (ns), plus grande que toute génération gardée par un cache.
- Un échec de téléchargement est journalisé avec l'URL montrable seulement (ni identifiants, ni jeton, ni texte de l'exception).

## Tâche 5 : fenêtres de création et de modification, gestes à chaud

Fichiers : nouveaux `web/static/js/vm-form.js`, `web/static/js/cloudinit-form.js`, `web/static/js/kvm-form.js` ; `web/static/js/vm-create.js`, `web/static/js/vm-edit.js` (déplacement du cloud-init, réexport), `web/static/js/kvm.js` (bouton « Créer une VM » de la vue des VMs et du bloc d'un hôte, entrées du menu), `web/static/js/kvm-vm.js` (gestes à chaud), `web/templates/index.html`, `web/static/js/i18n.js`, `web/static/css/style.css` ; tests `tests/e2e/test_kvm_create.py`, `tests/e2e/test_kvm_edit.py`, `tests/api/test_vm_form_source.py` (contrôles de source : `escapeHtml` à chaque interpolation, pas de `confirm(`, pas de `new EventSource(`).

Fenêtre « Créer une VM » (KVM) :
- En-tête : hôte (liste des hôtes joignables, préchoisi depuis le bloc d'un hôte), nom, nombre d'instances, démarrer après création, agent invité, clés SSH, modèle.
- Onglets, mêmes icônes et même ordre que Harvester quand ils existent : Général (titre, description, OS, démarrage automatique, actions à l'arrêt, au redémarrage, au plantage), Calcul (vCPU, maximum, topologie, mémoire et plafond, CPU et mémoire à chaud, mode et modèle CPU), Micrologiciel (type de machine, BIOS ou UEFI, Secure Boot, TPM, numéro de série, console série, affichage VNC et mot de passe, vidéo, tablette, ballon, chien de garde, emplacements pour l'ajout à chaud), Disques (cartes `KVM_DISK_SCHEMA` ; pool, source vierge, image liée ou copiée, volume, ISO ; capacité restante du pool affichée sous la carte comme `renderDiskCapacity` le fait pour Longhorn), Réseau (cartes `KVM_NIC_SCHEMA` : réseau libvirt, pont, macvtap avec son mode), Cloud-init (composant commun, seed dans le pool du premier disque ou un autre).
- Options grisées avec la raison venue de `/caps` (jamais une règle propre au navigateur).
- Essai à blanc : `create_plan` affiché (refus, avertissements, volumes, XML en lecture avec `passwd='***'`).
- Créer : action suivie dans le dock, étapes dans la fenêtre, résultat qui nomme les VMs créées et les restes éventuels.
- Depuis une image du catalogue (menu de l'image) : « Créer une VM » préremplit le premier disque, comme le menu d'une image Harvester (`withImageDisk`).

Fenêtre « Modifier » (KVM) :
- Mêmes onglets, remplis par `/form`. Chaque champ porte un repère « now » ou « next boot » (d'après `HOTNESS` et l'état de la VM), mis à jour quand la VM change d'état.
- Réglages non gérés listés en lecture seule dans l'onglet concerné.
- Bandeau « restart needed » quand `pending` n'est pas vide, avec la liste des champs et le bouton « shut down and start » (gestes de la 1.96.0, `VMActions.follow`).
- Appliquer : résultat par changement (appliqué maintenant, au prochain démarrage, refusé avec la raison) ; 409 `changed` affiche « reload » sans perdre la saisie.
- VM transitoire : la fenêtre s'ouvre en lecture seule avec la raison.

Menu d'une VM KVM (groupe « Disques et réseau » sous les gestes de la 1.98.0, conditions servies par l'API comme les autres) : Modifier, CPU et mémoire, Ajouter un disque, Retirer `<cible>`, Insérer une image dans `<cible>`, Éjecter `<cible>`, Ajouter une carte, Retirer `<mac>`, Cloud-init. Les fenêtres reprennent celles de `vm-actions.js` (`cpumemDialog`, `insertDialog`, `nicDialog`), avec leurs champs et leurs bulles.

Tests e2e (faux hôte derrière la console, comme en 1.98.0) : création d'une VM (onglets, option grisée avec sa raison, essai à blanc, création suivie) ; trois instances ; modification d'une VM en marche (repères now et next boot, résultat mixte, bandeau restart needed) ; 409 `changed` ; gestes à chaud du menu ; noms hostiles (VM, volume, pool, réseau) échappés dans les fenêtres, le dock et l'Activité ; création Harvester inchangée (tous les e2e existants de `vm-create` et `vm-edit` relancés sans modification).

Réalisé (tâche 5), écarts et précisions :

- **Décision 65 = A tenue** : `vm-create.js` passe sur la coquille commune (`VMForm.shellHtml` + `VMForm.mount`, adaptateur Harvester mince qui rejoue `VMEdit.renderSectionHtml` / `wireSection`) ; tous les e2e Harvester passent sans modification.
- `cloudinit-form.js` porte les schémas, les générateurs et l'assistant (`renderWizard`, `wireWizard`) ; `renderCloudInit` reste dans `vm-edit.js` (fichier de réponses Windows, virtiofs, boutons d'enregistrement propres à Harvester), qui réexporte `genUserData` et `genNetworkData`. Harvester garde sa confirmation du navigateur avant d'écraser l'éditeur (comportement inchangé) ; KVM confirme dans la page. Seul test existant touché : `tests/api/test_vm_edit.py` charge `cloudinit-form.js` avant `vm-edit.js` (aucune assertion changée).
- Côté KVM, le champ « SSH key (Harvester) » d'un utilisateur est retiré : les clés viennent de l'en-tête (KeyPairs des clusters lisibles + clés collées), ajoutées au user-data par la page avec l'agent invité (`withKeysAndAgent`, ce que le serveur fait pour Harvester). Un user-data qui n'est pas un `#cloud-config` reçoit un refus nommé s'il faudrait y ajouter des clés ou l'agent.
- Les gestes à chaud vivent dans `kvm-form.js` (pas `kvm-vm.js`). Conditions du groupe « Disques et réseau » servies par une table de plus, `edit_operations` dans `/api/kvm/vms` (`operations` de la 1.98.0 inchangé) : opérateur, VM en marche ou arrêtée (états de `kvm_edit`), VM transitoire acceptée (fenêtre en lecture seule, gestes `--live`).
- Modifier : disques et cartes existants en tableau (bus, cache, ordre de démarrage, média d'un CD-ROM, modèle, retrait), ajouts en cartes TFForm ; seuls les champs changés partent (spec partiel de `kvm_xml.changes`). Les repères par champ sont une prévision (table HOTNESS + état ; une règle du serveur recopiée : un lecteur SATA ou de CD-ROM se retire au prochain démarrage) ; la revue montre la classification exacte du serveur.
- Badge « restart needed » dans la liste : servi par `/api/kvm/vms` (`pending` par VM) sans aucun `dumpxml`. Repère `<état>/kvm.d/pending.json` (`bin/lib/kvm_pending.py`) écrit quand la console compare les deux XML (relecture après une modification ou un geste, fenêtre « Modifier ») avec l'Id libvirt de la VM ; il tombe dès que la VM est vue arrêtée, redémarrée (autre Id) ou absente d'un hôte lu. Une modification faite hors de la console (virsh) n'est vue qu'à l'ouverture de sa fenêtre « Modifier ». La page garde en plus ce qu'elle vient d'apprendre, effacé par les mêmes règles.
- Vu en réel sur node2 et corrigé avec un test : rechoisir le même hôte reconstruisait les onglets (saisie perdue) ; l'aperçu de la revue remplaçait le résultat d'une modification ; deux montages de la coquille cumulaient les écouteurs des onglets.
- Reprises après revue :
  - `withKeysAndAgent` ne lit que les clés de premier niveau (colonne 0) : une clé imbriquée (`users[].ssh_authorized_keys`) ou un texte de `write_files` ne comptent pas. Les éléments s'ajoutent à l'indentation de la liste existante (y compris la liste à indentation nulle) ; une liste entre crochets sur une ligne aux éléments simples est complétée dans ses crochets ; tout le reste (crochets sur plusieurs lignes, valeur qui n'est pas une liste, clé en double, plusieurs documents, script shell) est refusé avec la raison, texte inchangé. Fins de ligne CRLF gardées ligne par ligne. Tests en tableau relus par PyYAML (outil de test seulement).
  - Agent invité décoché par défaut pour un démarrage sur ISO (disque de plus petit ordre de démarrage) et pour un système Windows, tant que la personne n'a pas touché la case. Sans user-data, sans agent et sans clé : pas de seed. Un script shell passe case décochée ; case cochée, la fenêtre explique que l'agent ne s'ajoute qu'à un cloud-config.
  - Cloud-init : un user-data vide qui remplacerait un user-data non vide est refusé par la bibliothèque (`kvm_edit.empty_user_data_refusal`, plan et écriture, geste à chaud et modification) sauf `clear: true` ; la fenêtre du geste grise « Appliquer » tant que le seed n'est pas lu (et le reste si la lecture échoue), « Vider le user-data » envoie `clear`.
  - Modifier après un 409 `changed` ou un changement vu par la relecture périodique (20 s) : bandeau qui nomme les champs changés ailleurs (ceux aussi modifiés ici marqués), sans accepter la nouvelle empreinte ; « Relire » fait la fusion à trois voies (base, saisie, nouvelle vue) : champs non touchés relus, saisie gardée, tableaux de disques et cartes refaits s'ils n'ont pas été touchés, cloud-init relu s'il n'a pas été modifié.
  - Fenêtre de création déjà ouverte : nouvel hôte et image appliqués, après accord dans la page si une saisie serait perdue ; une réponse d'un hôte abandonné est ignorée (jeton de requête) ; `aria-invalid` sur le contrôle ; bulles des onglets Harvester (création et modification) ; analyseur de gabarits étendu à toute interpolation `${...}` d'un gabarit HTML (portée écrite dans le test).
- Lenteur connue : le catalogue d'images de node2 (relevé de la 1.98.0) met environ 20 s, la fenêtre de création attend ce relevé avant de proposer ses choix.

## Tâche 6 : Images, Stockage et Réseaux dans la console

Fichiers : `web/static/js/kvm.js` (vues Stockage et Réseaux, aujourd'hui en lecture), nouveau `web/static/js/kvm-storage.js` (fenêtres pool, volume, téléversement, réseau), `web/templates/index.html`, `web/static/js/i18n.js`, `web/static/css/style.css`, `web/static/js/xfer-progress.js` (réutilisé pour débit et temps restant) ; tests `tests/e2e/test_kvm_storage.py`.

- **Images** (sous-onglet de Stockage) : tableau par hôte (nom, pool, format, taille virtuelle, ISO, somme connue, « base de N disques »), boutons Téléverser (fichier du navigateur, pool, nom proposé d'après le fichier, SHA-256 facultatif, hôtes cibles, plafond de débit), Télécharger depuis une URL, Vérifier, Créer une VM, Supprimer (administrateur, refus nommé si l'image sert de base). Réglage « pools d'images » de l'hôte (administrateur).
- **Pools** : bouton Créer un pool (type `dir` ou NFS, chemin, autostart), par pool Démarrer, Arrêter, Rafraîchir, Autostart, Supprimer (confirmation dans la page avec l'aperçu : VMs qui l'utilisent, volumes, répertoire gardé ou retiré).
- **Volumes** : Créer, Redimensionner, Supprimer (aperçu de la 1.98.0 : cité par, base de).
- **Réseaux** : Créer un réseau (NAT, routé, isolé, pont ; sous-réseau proposé libre, plage DHCP), Démarrer, Arrêter, Autostart, Supprimer ; ponts et cartes de l'hôte en lecture.
- Progression du téléversement : barre en deux temps (réception par la console, puis envoi à chaque hôte), débit, temps restant, annulation.

Tests e2e : téléversement d'un petit fichier vers un faux hôte avec progression et annulation ; somme fausse refusée ; qcow2 à image de base refusé avant l'envoi ; pool refusé (chemin `/etc`) avec la raison ; suppression d'un pool utilisé grisée ; réseau qui chevauche le LAN refusé ; noms hostiles.

## Tâche 7 : vérification réelle, documentation, version

Banc (node2, 172.16.1.12, allumé par son iLO, éteint après ; **attendre que node2 soit libéré par le banc en cours**) :
- Relevé de référence : domaines, pools, réseaux, volumes de chaque pool (fichier gardé avec les journaux de la vérification).
- Capture des jeux d'essai (`domcapabilities` q35 et pc, `ip -j`) avant l'écriture des golden files définitifs ; comparaison avec les jeux de la tâche 1 et correction si la forme diffère.
- Pool de banc `kvmtest-pool` **créé par la console** (c'est une fonction de la version) sur `/var/lib/libvirt/images/kvmtest-pool`, réseau `kvmtest-net` NAT sur un sous-réseau réservé et enregistré dans NetBox avant usage.
- Images : cirros 0.6.2 (somme publiée vérifiée) téléversée par la console, et une image cloud openSUSE Tumbleweed ou SLES 16 (somme publiée) pour un vrai cloud-init ; une ISO (petite, par exemple l'ISO netinst d'une distribution) pour le chemin CD-ROM.

Vérifications (chacune par la console de dev, puis côté hôte par `virsh` et dans l'invité par la console VNC ou SSH) :
- `virsh vol-upload ... /dev/stdin` accepte un tuyau sur libvirt 12.7.0 (point à prouver en premier ; sinon revoir le chemin de téléversement avant toute autre tâche) ; format `qcow2` sondé après `pool-refresh` ; somme contrôlée sur l'hôte.
- Création `kvmtest-c1` : BIOS, q35, disque lié à cirros, réseau `kvmtest-net`, VNC avec mot de passe ; `define --validate` accepte le XML ; démarrage, console.
- Création `kvmtest-c2` : UEFI avec Secure Boot et TPM (si `swtpm` est présent sur node2, sinon option grisée vérifiée), image Tumbleweed en copie complète, cloud-init avec utilisateur, clé SSH et `network-config` statique ; connexion SSH dans l'invité, `cloud-init status` = done, nom d'hôte, IP ; le seed est lu par cloud-init (preuve que l'ISO Joliet de la console convient).
- Trois instances `kvmtest-m-01..03` ; annulation pendant une copie complète (aucun reste).
- Modifications sur `kvmtest-c2` en marche : vCPU 2 vers 4 (vu dans l'invité, `nproc`), mémoire montée par le ballon (vu dans `free`), disque `virtio` ajouté puis retiré à chaud (vu dans `lsblk`), carte ajoutée puis retirée, média changé ; type de machine et plafond changés = « next boot », bandeau, arrêt puis démarrage, changement pris ; emplacements PCIe épuisés = ajout reporté avec la raison.
- Réglages non gérés gardés : un `kvmtest-pin` défini à la main avec épinglage et `<cputune>`, modifié par la console (mémoire, titre), XML comparé (C14N) hors champs modifiés.
- Suppression de `kvmtest-c2` avec stockage : seed supprimé avec lui ; image Tumbleweed gardée ; suppression de l'image cirros refusée tant que `kvmtest-c1` l'utilise en base.
- Pools et réseaux : arrêt de `kvmtest-net` refusé VM en marche, suppression de `kvmtest-pool` refusée tant qu'un volume y est cité, puis acceptée vide (répertoire retiré) ; refus d'un réseau sur `172.16.0.0/16`.
- Fin : tous les `kvmtest-*` (VMs, volumes, pool, réseau) supprimés par la console, relevé comparé à la référence (diff vide), mot de passe VNC et mot de passe cloud-init factice absents des réponses, de `dev-server.log`, de `actions.db` et de son WAL.

Documentation et version : `VERSION`, entrée en tête du `CHANGELOG.md` (Added, Changed, Security, Internal avec ce qui n'est pas dans cette version, Tests), `docs/en/capabilities.md` et `docs/fr/capabilites.md` (section 10 : création, modification avec la table à chaud, images, pools, réseaux, API, ligne de commande, ce qui a été vérifié en réel et ce qui ne l'a pas été), `README.md` et `README.fr.md` (puce KVM), ce plan mis à jour avec ce que le réel a appris. Suites : fichiers ciblés pendant le travail, puis `pytest tests/api/ -q -n 12` et `pytest tests/e2e/ -q -n 8` avant la livraison.

## Ordre et dépendances

Tâche 1 d'abord (tout en dépend). Tâches 2 et 5 (partie `vm-form.js` et `cloudinit-form.js`, sans KVM) en parallèle. Tâche 3 après 2, tâche 4 après 3, la suite de 5 et la tâche 6 après 4. La preuve `vol-upload /dev/stdin` sur node2 est la seule vérification réelle à faire tôt : si node2 reste pris, elle se fait sur un libvirt de même version dans une VM jetable, ou la tâche 2 garde le repli « copie par `qemu-img convert` dans un fichier temporaire du pool » prêt (droits d'écriture du compte SSH exigés, refus nommé sinon).

## Questions ouvertes pour ju

| # | Sujet | Options | Recommandation |
|---|---|---|---|
| 1 | Télécharger une image depuis une URL | A : par la console seulement (le fichier traverse la console) ; B : aussi par l'hôte quand `curl` y est (`curl \| virsh vol-upload`) | A dans cette version, B plus tard si un client a des hôtes connectés et une console lente |
| 2 | Clés SSH pour les VMs KVM | A : KeyPairs des clusters Harvester + clés collées ; B : magasin de clés propre à la console | A maintenant ; B avec la vue unique |
| 3 | Disque depuis une image : mode par défaut | A : lié (rapide, l'image devient non supprimable) ; B : copie complète (indépendant, plus lent) | A, comme Harvester |
| 4 | Fichier de réponses Windows (`autounattend.xml`) | A : dans cette version (même écrivain ISO) ; B : plus tard | B si la version déborde, sinon A |
| 5 | Geste « rendre persistante » une VM transitoire | A : l'ajouter ; B : seulement expliquer le refus | B maintenant, A sur demande d'un client |
| 6 | Bascule de `vm-create.js` sur le formulaire commun | A : dans cette version, sous condition que les e2e existants passent tels quels ; B : seulement à la vue unique | A |
| 7 | Redimensionner un volume utilisé par une VM en marche | A : `blockresize` à chaud ; B : refus, VM arrêtée seulement | B maintenant |
| 8 | Types de pool créables | A : `dir` et NFS ; B : aussi LVM (`logical`) et iSCSI | A |
