# OpenBao comme magasin de secrets facultatif (conception)

État : conception, 02/11/2026. Rien n'est codé. Les décisions à prendre sont
en fin de document, numérotées à partir de 133.

## La demande

Demande du propriétaire du produit (02/11/2026). Étudier l'intégration
d'OpenBao comme magasin de secrets de la console. La conception de la
post-installation bare-metal (`2026-11-02-bare-metal-post-installation.md`,
décision 129) renvoie ici : ses secrets sont saisis au lancement, redemandés
à la reprise, puis gardés seulement en Secrets Kubernetes. Ce document dit
ce qui change quand un OpenBao est disponible.

OpenBao est le fork libre de HashiCorp Vault, porté par la Linux Foundation,
sous licence MPL-2.0. Son API reprend celle de Vault. Dernière version au
02/11/2026 : 2.7.2 (26/10/2026), avec une branche 2.6.x maintenue (2.6.5).

## Objectifs

- **Facultatif.** Sans OpenBao, la console fonctionne exactement comme
  aujourd'hui. Aucun écran, aucune action ne l'exige.
- **Moins de secrets sur le disque de la console.** Le répertoire d'état
  porte aujourd'hui des kubeconfigs administrateurs, des clés SSH, des
  secrets de notification. Une sauvegarde de l'état ou une copie de l'hôte
  les emporte tous.
- **Un seul endroit pour les secrets du client**, avec ses propres règles
  d'accès, sa rotation et son journal d'audit.
- **Anti-enfermement.** Logiciel ouvert, hébergeable par le client, API
  standard. Un serveur Vault compatible du client convient aussi. La
  console garde la voie inverse : tout secret peut revenir en fichier local.
- **Airgap.** Rien n'est téléchargé au moment de l'installation ou de
  l'activation.

## Hors objectifs

- Remplacer les comptes de la console (`accounts.json`, hachés en bcrypt).
  Ils restent locaux : on doit pouvoir se connecter quand OpenBao est
  injoignable.
- Gérer le cycle de vie d'OpenBao au-delà du mode B (montées de version
  majeures, grappe multi-sites, réplication propre à OpenBao).
- Secrets dynamiques (comptes de base de données, jetons cloud). Rien dans
  la console n'en consomme aujourd'hui.
- PKI d'OpenBao pour le certificat de la console ou du registre
  d'amorçage. Piste notée, pas dans ce chantier.
- Agent OpenBao dans les VMs Harvester.

## Ce que la console range aujourd'hui

Relevé dans le code de la branche (2.0.4). L'état est sous
`~/.local/share/harvester-ops` en développement et sous
`/var/lib/harvester-ops` dans le paquet. La configuration
(`/etc/harvester-ops`) est montée en lecture seule dans le conteneur.

| Classe | Où | Sensibilité | Remarque |
|---|---|---|---|
| Kubeconfigs des clusters déclarés par la console | `<état>/clusters.d/<nom>.yaml` et `<état>/kubeconfigs/<nom>.yaml` (0600) | Très haute : souvent administrateur du cluster | Lus à chaque action, copiés en 0600 dans l'espace de travail (`_stage_kubeconfig`) |
| Kubeconfigs déclarés dans `config.yaml` | `/etc/harvester-ops/kubeconfigs/` | Très haute | Posés par l'installateur ou à la main |
| Clés SSH des hôtes KVM | `<état>/ssh/<nom>_id` (0600), empreinte épinglée `<nom>_known` | Haute : accès root à l'hyperviseur | `bin/lib/kvm_decl.py` |
| Clés SSH des clusters installés en bare-metal | `<état>/ssh/<nom>_id` (0600) | Haute : accès aux nœuds | Paire créée à l'installation (1.78.0) |
| Identifiants BMC, jeton et mot de passe du cluster | Jamais rangés : saisis au lancement d'une série (`bm_profiles.SECRET_FIELDS`) | Haute | Fichier 0600 de l'espace de travail, effacé à la fin |
| Secret du client OIDC de chaque Rancher | `<état>/rancher.d/<id>.oidc-secret` (0600) | Haute : permet de se faire passer pour la console auprès de Rancher | `web/rancher_servers.py` |
| Déclarations et états Terraform | `tf-declarations.db` (sans secret), `<état>/terraform/<espace>/terraform.tfstate` | Variable : l'état porte en clair ce que les ressources portent (cloud-init, clés) | En développement, les fichiers d'état sont en 0644 : à vérifier dans le paquet |
| Secrets des notifications de la réplication | `replication.db`, table `settings`, réglage `notify` (jeton Matrix, URL Slack, mot de passe SMTP) | Moyenne | Base privée 0600, masqués à la lecture |
| En-têtes secrets des actions annexes de la réplication | `replication.db`, `plans.options.hooks[].secret_headers` | Moyenne | Conservés d'une édition à l'autre (`keep_hook_secrets`) |
| Clés de confiance des mises à jour | `/opt/harvester-ops/update-signers` | Publiques | Intégrité, pas confidentialité |
| Comptes de la console | `accounts.json` (hachés bcrypt), jeton de premier démarrage | Haute | Restent locaux (hors objectifs) |
| Dossiers, réservations, usage, notes, actions | `folders.db`, `bookings.db`, `usage.db`, `notes.db`, `actions.db` | Aucun secret relevé | Restent locaux |

Ce qui n'est rangé nulle part et ne change pas : les mots de passe des VMs
(propagés par fichier 0600 effacé), les identifiants vCenter de Forklift
(dans le Secret du fournisseur, sur le cluster), le kubeconfig des clusters
enfants Cluster API (dans le Secret `<cluster>-kubeconfig` du cluster
Harvester).

## Les modes

### Mode A : le serveur du client

Le client a déjà un OpenBao, ou un Vault compatible. Il crée pour la console
un point de montage KV v2, une politique et un rôle d'authentification. La
console ne fait que s'y connecter. Un assistant affiche les commandes `bao`
à exécuter côté client, avec le texte exact de la politique.

Compatibilité Vault : on vise l'API commune (KV v2, AppRole, transit,
`sys/capabilities-self`). La console vérifie ce qu'elle utilise au lieu de
lire un numéro de version.

### Mode B : un OpenBao posé par la console

Pour le client qui n'en a pas. Deux emplacements possibles.

**B1, conteneur à côté de la console.** Une seconde unité systemd,
`harvester-ops-openbao.service`, calquée sur celle de la console : compte
système dédié, système de fichiers racine en lecture seule, stockage
intégré (raft) dans `/var/lib/harvester-ops-openbao`, écoute locale
seulement. Simple, airgap, sans dépendance à un cluster. Défaut : il
partage le sort de l'hôte de la console.

**B2, sur un cluster Harvester par Helm.** Chart officiel `openbao/openbao`,
trois répliques raft sur un cluster à trois nœuds. Haute disponibilité. Mais
dépendance circulaire : la console a besoin du kubeconfig de ce cluster
pour joindre OpenBao, et ce kubeconfig ne peut donc pas vivre dans OpenBao.
Il faut aussi exposer le service (LoadBalancer ou entrée), gérer son
certificat, et le descellement après chaque redémarrage de pod.

**Recommandation.** Mode A en premier : c'est le cas des clients qui ont
déjà une politique de secrets, et c'est le moins de code. Puis B1, pour les
autres. B2 seulement sur demande d'un client, avec la règle écrite que le
kubeconfig du cluster hôte reste local.

## Ce qui part dans OpenBao, et dans quel ordre

Un secret garde sa place dans l'interface. Seul son support change. Dans les
fichiers de l'état, la valeur est remplacée par une référence :
`openbao:<montage>/<chemin>#<champ>`, chemins relatifs au préfixe de la
console (par défaut `harvester-ops/<identifiant de la console>/`).

| Priorité | Classes | Pourquoi dans cet ordre |
|---|---|---|
| P1 | Secrets des notifications et en-têtes secrets de la réplication ; secret du client OIDC de chaque Rancher | Peu d'accès, faible impact si OpenBao tombe (une notification ou une connexion SSO échoue, avec un message) |
| P1 | Chiffrement des états Terraform (voir plus bas) | Les états portent des secrets en clair aujourd'hui |
| P2 | Clés SSH des hôtes KVM et des clusters bare-metal | Haute valeur, accès fréquent : demande le cache décrit plus bas |
| P2 | Kubeconfigs des clusters déclarés par la console | La plus haute valeur, mais tout en dépend : arrive quand le cache et les messages de panne sont éprouvés |
| P3 | Secrets du plan de post-installation et identifiants BMC d'une série | Nouveau : ne plus les redemander à la reprise (prolonge la décision 129, voir plus bas) |

Reste local, toujours :

- Les comptes de la console et le jeton de premier démarrage.
- L'identifiant de connexion à OpenBao lui-même (voir Authentification).
- Les clés de confiance des mises à jour et les autorités de certification
  (publiques).
- Les empreintes d'hôtes SSH (publiques ; leur intégrité compte, le fichier
  reste 0600).
- Le kubeconfig du cluster qui héberge OpenBao en mode B2.
- Les kubeconfigs de `config.yaml` : ils appartiennent à l'installateur.
  Un réglage permet de les y référencer à la main.

### États Terraform

La console sait déjà si elle conduit OpenTofu ou Terraform (`TF_FLAVOR`).
OpenTofu chiffre l'état et le plan depuis la 1.7, avec un fournisseur de
clés `openbao` qui s'appuie sur le moteur transit. La console écrit le bloc
`encryption` dans `_providers.tf` quand OpenBao est actif, une clé transit
par déclaration. La migration d'un état existant passe par la méthode
`unencrypted` en repli, puis on la retire. Avec Terraform, pas de
chiffrement de l'état : la console le signale et propose OpenTofu, qu'elle
embarque déjà.

Le fournisseur `openbao` d'OpenTofu ne prend qu'un jeton. La console lui
passe un jeton enfant, à durée courte, limité aux chemins transit de cette
déclaration, par la variable `BAO_TOKEN` de l'environnement du processus.
Jamais en argument.

### Secrets du plan de post-installation

La décision 129 tient en mode local. Quand OpenBao est actif, le plan peut
garder ses secrets dans OpenBao, sous le chemin du déroulé. La reprise ne
les redemande plus. Ils sont effacés (versions comprises,
`metadata delete`) quand le déroulé se termine ou est abandonné. Une fois
posés, ils vivent toujours en Secrets Kubernetes sur le cluster qui en a
besoin.

## Authentification de la console

La console tourne dans un conteneur podman, pas dans Kubernetes. La méthode
Kubernetes d'OpenBao ne s'applique donc pas à elle.

**AppRole avec un `secret_id` emballé (recommandé).**

1. Côté OpenBao (le client en mode A, la console en mode B) : un rôle
   `harvester-ops` lié à une politique, `secret_id_bound_cidrs` et
   `token_bound_cidrs` sur l'adresse de la console, jeton périodique.
2. Le `role_id` va dans la configuration. Le `secret_id` est remis
   **emballé** (`-wrap-ttl=5m`) : un jeton à usage unique. La console le
   déballe une fois. Si le déballage échoue, c'est que quelqu'un l'a déjà
   lu : la console refuse et le dit.
3. Le `secret_id` déballé est écrit en 0600 dans
   `/etc/harvester-ops/openbao/secret-id`, hors du répertoire d'état. Une
   sauvegarde de l'état ne l'emporte donc pas.
4. Le jeton obtenu n'est gardé qu'en mémoire et renouvelé avant échéance.
   Au redémarrage, nouvelle connexion avec le `secret_id`.
5. Rotation : la politique autorise la console à se créer un nouveau
   `secret_id` et à détruire l'ancien. Action suivie, périodicité réglable.

Problème : `/etc/harvester-ops` est en lecture seule dans le conteneur. Le
déballage et la rotation passent donc par l'agent de l'hôte, comme les mises
à jour (dépôt d'une demande, traitement par l'agent root). Si l'agent n'est
pas installé, l'activation se fait par `install.sh` ou une commande sur
l'hôte, et la rotation automatique est désactivée.

**JWT signé par la console (écarté pour l'instant).** La console publierait
ses clés, OpenBao les croirait. Mais la clé privée de la console devient le
secret à protéger : même problème, plus de code.

**Jeton statique (refusé).** Pas de rotation, pas de liaison à une adresse.

**Client HTTP.** Bibliothèque standard (`urllib`, `ssl`), comme
`kube_rest`. Pas de roue `hvac` : l'API utilisée est petite, et chaque
dépendance pèse sur le livrable airgap.

**Variables d'environnement.** La console ignore `VAULT_ADDR`,
`VAULT_TOKEN`, `BAO_ADDR` et `BAO_TOKEN` de son propre environnement. Seule
sa configuration compte. Un shell d'administrateur qui exporte ces variables
ne doit pas détourner la console vers un autre serveur.

### Descellement en mode B

- **Clé statique (recommandé).** Le mécanisme `static` d'OpenBao : une clé
  AES-256 de 32 octets, lue dans un fichier au démarrage, avec rotation
  `previous_key` / `current_key`. Le fichier vit dans
  `/etc/harvester-ops-openbao/`, root seulement, hors de toute sauvegarde
  d'état. La console propose une copie hors ligne à l'activation et exige un
  acquittement. Sans cette clé, un instantané raft est illisible.
- **Shamir manuel.** Un opérateur saisit des parts à chaque redémarrage.
  Plus sûr, mais un redémarrage de l'hôte rend la console aveugle jusqu'à
  une intervention. La documentation d'OpenBao déconseille d'automatiser
  Shamir.
- **Transit d'un autre OpenBao.** Possible si le client en a un, mais alors
  le mode A suffit.

Point à vérifier en réel : OpenBao 2.7 sort plusieurs mécanismes de
descellement du binaire vers des greffons. Il faut confirmer que `static`
reste intégré dans la version embarquée.

HA : B1 est un nœud unique, assumé. La console redémarre OpenBao avec elle.
B2 apporte la HA raft à trois répliques.

## Accès par identité

Aujourd'hui, la console agit sur un cluster avec l'identité de la personne
(usurpation dans le kubeconfig, ou jeton Rancher hérité). Pour OpenBao :

- **Étape 1 : identité de service seule.** OpenBao voit la console. La
  console applique ses propres règles avant de lire un secret : rôle dans la
  console, et droit Kubernetes de la personne sur la ressource concernée
  (même contrôle qu'aujourd'hui). Le journal d'actions dit qui. Pour relier
  les deux journaux, la console envoie l'identifiant de l'action et le nom
  de la personne dans des en-têtes que l'audit d'OpenBao enregistre
  (`sys/config/auditing/request-headers`). Point à vérifier en réel.
- **Étape 2, sur décision : jeton par personne.** Les personnes connectées
  par Rancher ont un jeton OIDC de Rancher. Une méthode JWT d'OpenBao qui
  fait confiance à Rancher donnerait un jeton OpenBao par personne, avec des
  politiques par groupe. Alors OpenBao refuse lui-même. Coût : une
  configuration de plus chez le client, et un comportement différent pour
  les comptes locaux de la console, qui restent sur l'identité de service.

## Rotation et audit

- **KV v2** garde les versions. La console n'en montre que les
  métadonnées (version, date, auteur de l'action), jamais la valeur. Un
  retour à la version précédente est une action suivie.
- **Rotation par la console**, là où elle possède le secret :
  - clé SSH d'un hôte KVM : nouvelle paire, ajout à `authorized_keys`,
    connexion d'essai avec la nouvelle, retrait de l'ancienne, nouvelle
    version dans OpenBao. Toute étape en échec rend l'état d'avant ;
  - `secret_id` de la console (ci-dessus) ;
  - clé transit d'une déclaration Terraform (`transit/keys/<clé>/rotate`,
    les anciens états restent lisibles).
- **Rotation que la console ne possède pas** : secret OIDC (côté Rancher),
  jeton Matrix, kubeconfig. La console montre l'âge de la version et
  propose de saisir la nouvelle valeur.
- **Audit.** En mode B, la console active un périphérique d'audit fichier
  dans le volume d'OpenBao, avec rotation. OpenBao refuse une requête qu'il
  ne peut pas auditer : un disque plein arrête donc OpenBao. La console
  surveille la place et prévient avant. En mode A, l'audit appartient au
  client. Dans les deux modes, chaque lecture, écriture, migration et
  rotation crée aussi une entrée dans Activité, sans valeur.

## Clusters enfants et VMs Harvester

Deux façons de faire arriver un secret dans un cluster enfant Cluster API.

**La console écrit des Secrets Kubernetes (défaut).** C'est la décision
129 : la console lit la valeur (saisie, ou dans OpenBao) et pose un Secret
sur le cluster qui en a besoin. Aucun composant de plus dans le cluster,
rien à rendre disponible en airgap. Défaut : la valeur est recopiée, et une
rotation dans OpenBao ne la suit pas sans nouvelle action.

**External Secrets Operator dans le cluster enfant (proposé).** ESO n'a pas
de fournisseur OpenBao propre. Il passe par son fournisseur Vault, que la
documentation d'ESO dit essayé avec OpenBao. La console ajoute ESO au
catalogue d'artefacts de la post-installation, crée pour chaque cluster
enfant un montage d'authentification Kubernetes dans OpenBao (adresse et CA
de l'API du cluster enfant, compte de service d'ESO) et une politique
limitée à son chemin. Le `SecretStore` et les `ExternalSecret` font partie
du plan. La rotation suit seule. Il faut que les nœuds du cluster enfant
joignent OpenBao : à vérifier avant d'agir.

Autres voies, non retenues pour ce chantier : le fournisseur CSI d'OpenBao
(pilote Secrets Store CSI, DaemonSet privilégié par nœud) et
l'`openbao-secrets-operator`, dont la maturité n'est pas établie.

**Recommandation.** Secrets écrits par la console par défaut, ESO comme
artefact proposé quand OpenBao est actif.

**VMs Harvester.** Leur cloud-init est déjà un Secret du cluster Harvester.
La console y place la valeur lue dans OpenBao au moment de la création. Rien
ne change dans la VM.

## Migration depuis les fichiers

Une commande, pour la parité avec l'interface :
`harvester-secrets migrate --to openbao [--class ...] [--dry-run]`, et
`--to local` pour le chemin inverse. L'interface la lance et suit ses
étapes.

Pour chaque secret :

1. Lire la valeur locale. Écrire dans OpenBao (`cas=0` : refus si le chemin
   existe déjà avec une autre valeur).
2. Relire et comparer les empreintes SHA-256. Jamais la valeur au journal.
3. Remplacer la valeur locale par la référence, par écriture atomique.
4. Garder l'original en `<fichier>.migrated` (0600) jusqu'à la confirmation
   de l'administrateur ou 7 jours. Puis effacement.

Retour arrière : `--to local` relit OpenBao et réécrit les fichiers. Si
OpenBao est perdu avant la confirmation, les `.migrated` suffisent. Après,
seule une sauvegarde d'OpenBao permet de revenir. L'interface le dit avant
la confirmation.

Pour les bases (`replication.db`), même principe sur les champs :
transaction, référence à la place de la valeur, original dans une table de
retour vidée à la confirmation.

## Sauvegarde, reprise après sinistre et réplication

- **Mode A** : la sauvegarde appartient au client. La console vérifie à
  l'activation qu'elle sait lire ce qu'elle écrit, et rappelle dans la
  fiche de reprise que ses secrets sont chez lui.
- **Mode B** : instantanés raft (`sys/storage/raft/snapshot`) planifiés par
  la console, déposés sur la cible de sauvegarde choisie, avec rétention.
  Ils apparaissent dans la section Protection comme un objet protégé. Un
  instantané est chiffré par la barrière : sans la clé de descellement, il
  est inutile. La clé suit donc un autre chemin que l'instantané.
- **Avec la réplication asynchrone.** Le carnet de reprise vit sur le
  cluster cible et ne porte aucun secret. Mais si le site de la console est
  perdu, une console de secours a besoin des kubeconfigs et des secrets de
  notification. Avec OpenBao, ils ne sont plus dans l'état de la console :
  la fiche de reprise liste donc OpenBao comme prérequis (mode A : serveur
  du client joignable depuis le site de secours ; mode B : instantané et clé
  restaurés). L'exercice de reprise inclut la restauration du magasin.
- **Dépendance à éviter** : OpenBao en B2 sur le cluster même que l'on
  protège. La console refuse ce placement pour un cluster source d'un plan
  de réplication.

## Livrable airgap

- **Mode A** : rien de plus dans le paquet. Le client HTTP est en
  bibliothèque standard.
- **Mode B** : l'image officielle d'OpenBao (et le chart Helm pour B2) dans
  un paquet séparé facultatif, sur le modèle du paquet Cluster API
  (`HARVESTER_OPS_OPENBAO_BUNDLE`). Taille à mesurer à la construction et à
  publier dans le CHANGELOG. Le paquet principal ne grossit pas.
- **Vérification.** À la construction, l'éditeur vérifie la signature
  cosign publiée par OpenBao (fichier de sommes et image) et note l'empreinte
  de l'image. Le paquet est ensuite signé comme une mise à jour de la
  console. Le client vérifie avec les outils qu'il a déjà (signature de la
  release, empreintes), sans cosign ni réseau.
- **Image de base.** La règle « BCI uniquement » vaut pour l'image de la
  console. OpenBao est un logiciel tiers : on embarque son image officielle,
  épinglée par empreinte, plutôt que de reconstruire le binaire.
- Stockage : le moteur `file` a été retiré des paquets récents d'OpenBao.
  B1 utilise le stockage intégré raft, même en nœud unique.

## Pannes

| Situation | Ce qui marche encore | Ce que la console dit |
|---|---|---|
| OpenBao injoignable, secrets déjà en cache | Tout, jusqu'à expiration du cache | Bandeau « OpenBao injoignable depuis HH:MM, secrets servis depuis la mémoire jusqu'à HH:MM » |
| OpenBao injoignable, cache vide (console redémarrée) | Connexion, historique, notes, dossiers, réservations, vues des clusters dont le kubeconfig est local | Refus de l'action avec la classe du secret et l'adresse visée : « Clé SSH de l'hôte node2 dans OpenBao : serveur injoignable (https://…) » |
| OpenBao scellé | Comme ci-dessus | « OpenBao scellé » et, en mode B, le geste de descellement |
| Jeton refusé ou `secret_id` expiré | Comme ci-dessus | « Connexion à OpenBao refusée : renouveler l'identifiant de la console » avec le lien vers l'assistant |
| Politique trop étroite | Les classes dont le chemin reste lisible | Le chemin refusé et la capacité manquante (lue par `sys/capabilities-self`) |
| Notification de réplication sans secret | La réplication elle-même | L'échec de la notification au journal du plan, comme aujourd'hui |

**Cache.** En mémoire du processus seulement, durée réglable (15 minutes
par défaut), vidé à la déconnexion d'OpenBao par l'administrateur. Jamais
sur disque. Les copies de travail (kubeconfig, clé SSH remis à `kubectl` ou
`ssh`) restent des fichiers 0600 effacés en fin d'action. Elles vont sur le
tmpfs `/tmp` du conteneur, déjà monté par l'unité, pour ne jamais toucher le
disque.

## Expérience

Selon les principes du propriétaire du produit : vérifier avant d'agir,
actions suivies, historique, retour arrière, info-bulles, cinq langues.

**Réglages, section Secrets** (administrateur). Une carte d'état : local ou
OpenBao, adresse, mode, dernière vérification. Puis un tableau par classe :
où elle vit, combien de secrets, date de migration, bouton « Migrer » ou
« Revenir en local ».

**Vérifications avant activation**, chacune avec son résultat et sa raison :

1. Adresse joignable, certificat reconnu (CA fournie ou système).
2. État de scellement (`sys/seal-status`, sans authentification).
3. Connexion AppRole réussie.
4. Montage KV en version 2.
5. Capacités sur chaque chemin nécessaire (`sys/capabilities-self`),
   lecture, écriture, suppression des métadonnées.
6. Écriture, relecture et suppression d'une valeur d'essai.
7. Si OpenTofu est utilisé : moteur transit et droit de créer une clé.

Rien n'est activé tant qu'une vérification est rouge.

**Actions suivies** : activer, migrer une classe, revenir en local,
renouveler l'identifiant, faire tourner une clé, instantané, restauration.
Chacune crée un `ActionRun`, avec ses étapes dans le dock et l'onglet
Activité. **Historique** : versions KV par secret, métadonnées seules.

## Essais réels

Sur le homelab, sans jamais toucher le Vault HashiCorp du homelab
(`127.0.0.1:8200`), qui ne doit pas être modifié.

- **Banc** : un conteneur OpenBao **rootless** sur node1, réseau podman
  propre, écoute sur `127.0.0.1:8210`, stockage raft dans un volume nommé,
  clé statique générée dans un fichier 0600 du banc. Version épinglée
  (2.7.2). Une commande de banc le crée, le configure (KV v2, transit,
  AppRole, politique, audit) et le détruit.
- **Garde-fou** : le banc et les tests refusent de tourner si l'adresse
  visée est le port 8200, et lancent la console avec `VAULT_*` et `BAO_*`
  vidés de l'environnement. Un test vérifie que la console ignore ces
  variables.
- **Mode A** : le banc joue le serveur du client. Console de développement
  (8095) branchée dessus.
- **Cas joués** : activation avec chaque vérification mise en défaut une
  fois ; `secret_id` emballé déjà déballé ; migration puis retour de chaque
  classe ; OpenBao arrêté avec et sans cache ; OpenBao scellé ; jeton
  révoqué ; politique réduite à un chemin ; rotation de clé SSH sur l'hôte
  KVM node2 (sans toucher ses domaines) ; état OpenTofu chiffré sur harvlab,
  plan puis application ; instantané puis restauration dans un second
  conteneur.
- **Mode B1** : unité systemd sur le banc client `hops-upd`, paquet
  airgap, redémarrage de l'hôte, descellement automatique vérifié.
- **ESO** : sur un cluster enfant Cluster API de harvlab, un
  `ExternalSecret` suit une rotation dans OpenBao.
- **B2** (si décidé) : chart Helm sur harvlab2, un nœud.
- **Tests automatisés** : client OpenBao contre un faux serveur en
  bibliothèque standard (tableaux de cas, un cas par chemin d'erreur),
  mutations volontaires sur les refus (déballage, `cas`, capacités). Tests
  `--live` contre le banc, non lancés par défaut.
- Après chaque essai : conteneur détruit, volumes supprimés, fichiers
  `.migrated` et références remis à l'état local.

## Découpage en versions

La série de la post-installation (2.4.0 à 2.8.0) n'a pas besoin d'OpenBao.
Une seule exception utile : elle ajoute des classes de secrets. On glisse
donc dans 2.4.0 une interface interne de magasin de secrets (lire, écrire,
supprimer, références), avec le seul support local, sans changement
visible. Les nouvelles classes l'utilisent dès le départ.

| Version | Apporte |
|---|---|
| 2.4.0 (avec la post-installation) | Interface interne de magasin, support local seul, aucun changement visible |
| 2.9.0 | Mode A : client HTTP, AppRole emballé, vérifications, section Secrets ; classes P1 (notifications, en-têtes de la réplication, secrets OIDC) ; `harvester-secrets migrate` et retour ; pannes et messages ; corrélation d'audit |
| 2.10.0 | Classes P2 (clés SSH, kubeconfigs) avec cache ; chiffrement des états OpenTofu par transit ; rotation des clés SSH KVM et du `secret_id` par l'agent de l'hôte |
| 2.11.0 | Mode B1 : paquet OpenBao séparé, unité systemd, clé statique, instantanés dans Protection, restauration |
| 2.12.0 | Classe P3 (secrets du plan gardés pour la reprise) ; ESO comme artefact des clusters enfants ; jeton par personne si décidé |
| Sur demande | Mode B2 sur Harvester par Helm |

Chaque lot est utile seul. 2.9.0 suffit à un client qui a déjà OpenBao.

## Risques

- **Point unique de panne.** Les kubeconfigs dans OpenBao rendent la
  console dépendante de lui. Parade : P2 seulement après P1 éprouvé, cache,
  messages précis, retour en local en une commande.
- **Dépendance circulaire** en B2 et en reprise après sinistre. Parade :
  règles écrites et refus explicites (kubeconfig du cluster hôte local,
  pas d'OpenBao sur un cluster source de réplication).
- **Clé de descellement perdue** en mode B : magasin et instantanés perdus.
  Parade : copie hors ligne exigée à l'activation, rappel à chaque
  instantané.
- **Dérive entre OpenBao et Vault.** Les deux API divergent avec le temps
  (greffons de descellement, espaces de noms). ESO passe par son
  fournisseur Vault. Parade : n'utiliser que le socle commun, le vérifier
  par capacités, essayer chaque version embarquée.
- **Audit qui bloque.** OpenBao refuse tout si l'audit ne peut plus écrire.
  Parade : surveillance de la place en mode B.
- **Jeton passé à OpenTofu** par l'environnement : visible du même compte
  dans `/proc`. Parade : jeton enfant court, limité à une clé transit.
- **Fenêtre de double présence** pendant la migration (`.migrated`).
  Parade : durée bornée, effacement suivi.
- **Charge de configuration chez le client** en mode A. Parade : commandes
  et politique générées par l'assistant, vérifiées une à une.
- **Périmètre qui gonfle** (PKI, secrets dynamiques, agent dans les VMs).
  Parade : hors objectifs écrits ci-dessus.

## Décisions à prendre

| # | Sujet | Options | Ma recommandation |
|---|---|---|---|
| 133 | Mode à livrer d'abord | A serveur du client d'abord, puis mode B ; B mode B d'abord ; C les deux ensemble | A |
| 134 | Emplacement du mode B | A conteneur à côté de la console (B1), Helm sur Harvester sur demande (B2) ; B Helm sur Harvester seulement ; C pas de mode B | A |
| 135 | Authentification de la console | A AppRole, `secret_id` emballé, gardé dans `/etc`, rotation par l'agent de l'hôte ; B JWT signé par la console ; C jeton statique | A |
| 136 | Classes migrées et ordre | A P1 notifications, OIDC, états Terraform ; P2 clés SSH et kubeconfigs ; P3 secrets du plan ; comptes et clés publiques toujours locaux ; B tout d'un coup ; C P1 seulement | A |
| 137 | Cache quand OpenBao tombe | A mémoire seule, 15 minutes réglables ; B aucun cache ; C cache chiffré sur disque | A |
| 138 | Accès par identité | A identité de service, contrôles de la console, corrélation des audits par en-têtes ; jeton par personne (JWT Rancher) plus tard sur décision ; B jeton par personne dès le départ | A |
| 139 | Secrets des clusters enfants | A Secrets Kubernetes écrits par la console par défaut, ESO proposé comme artefact quand OpenBao est actif ; B ESO obligatoire ; C fournisseur CSI | A |
| 140 | États Terraform | A chiffrement OpenTofu par transit quand OpenBao est actif, avertissement avec Terraform ; B pas de chiffrement | A |
| 141 | Secrets du plan avec OpenBao (prolonge 129) | A gardés dans OpenBao pendant le déroulé, plus redemandés à la reprise, effacés à la fin ; B toujours redemandés | A |
| 142 | Descellement du mode B | A clé statique root sur l'hôte, copie hors ligne exigée ; B Shamir manuel ; C transit d'un autre OpenBao | A |
| 143 | Image d'OpenBao | A image officielle épinglée, paquet séparé facultatif, signée avec la release ; B binaire reconstruit sur BCI ; C dans le paquet principal | A |
| 144 | Sauvegarde du magasin en mode B | A instantanés raft planifiés, objet de la section Protection, restauration dans l'exercice de reprise ; B laissée à l'administrateur | A |
| 145 | Découpage | A interface interne dans 2.4.0, puis 2.9.0 à 2.12.0 après la post-installation ; B tout après 2.8.0 ; C entrelacé avec la post-installation | A |

## Sources

- OpenBao, versions : https://github.com/openbao/openbao/releases
- OpenBao, installation et vérification (GPG, cosign) : https://openbao.org/docs/install/
- OpenBao, descellement : https://openbao.org/docs/concepts/seal/
- OpenBao, mécanisme `static` : https://openbao.org/docs/configuration/seal/static/
  et sa RFC https://openbao.org/community/rfcs/static-auto-unseal/
- OpenBao, intégrations Kubernetes : https://openbao.org/docs/platform/k8s/
- OpenBao, fournisseur CSI : https://openbao.org/docs/platform/k8s/csi/ et
  https://github.com/openbao/openbao-csi-provider
- OpenBao Secrets Operator : https://github.com/openbao/openbao-secrets-operator
- External Secrets Operator, OpenBao : https://external-secrets.io/latest/provider/openbao/
- OpenTofu, chiffrement de l'état et du plan : https://opentofu.org/docs/language/state/encryption/
- Image officielle : https://hub.docker.com/r/openbao/openbao

## Décisions du propriétaire du produit (03/11/2026)

- 133 : les deux modes livrés ensemble (serveur du client et OpenBao déployé par la console).
- 134 : OpenBao déployé en conteneur à côté de la console ; Helm sur un Harvester seulement sur demande.
- 135 : AppRole, secret emballé, gardé dans `/etc`, rotation par l'agent de l'hôte.
- 136 : toutes les classes de secrets migrées dans la même version ; les comptes et les clés publiques restent locaux.
- 137 : cache en mémoire seulement, 15 minutes réglables.
- 138 : hybride. Identité de service pour les tâches de fond et les comptes locaux ; jeton personnel pour qui se connecte par Rancher ou OIDC, politiques OpenBao par groupe. Identité de service d'abord, jeton personnel dans la version suivante.
- 139 : Secrets Kubernetes écrits par la console, External Secrets Operator proposé en option.
- 140 : états chiffrés par OpenTofu avec une clé gérée par OpenBao.
- 141 : secrets d'un plan gardés dans OpenBao pendant le déroulé, effacés à la fin.
- 142 : clé de descellement statique lisible par root, copie hors ligne exigée.
- 143 : image OpenBao de la SUSE Application Collection, dans un paquet séparé facultatif. Elle demande les identifiants Application Collection du client, ou un registre miroir.
- 144 : instantanés raft planifiés, visibles dans Protection, restauration testée dans l'exercice de reprise.
- 145 : interface interne dans 2.4.0, puis une version 2.9.0 complète (deux modes, toutes les classes, états chiffrés), jeton personnel en 2.10.0.
