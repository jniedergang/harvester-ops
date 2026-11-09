# Magasin de secrets (OpenBao, 2.9.0 et 2.10.0)

English version: [../en/secrets.md](../en/secrets.md).

Par défaut, la console garde ses secrets dans des fichiers locaux de son
répertoire d'état : kubeconfigs des clusters, clés SSH, secret client OIDC
de Rancher, secrets des notifications et des actions annexes de
réplication, variables Terraform des déclarations KVM, secrets d'un plan de
déploiement en cours. Depuis la 2.9.0, elle peut les garder dans OpenBao à
la place, classe par classe, et revenir aux fichiers locaux à tout moment.
Depuis la 2.10.0, une personne connectée par Rancher lit et écrit les
classes personnelles avec son propre jeton OpenBao
([Jetons personnels](#jetons-personnels-2100)), et les clusters enfants
créés par la console peuvent synchroniser des valeurs gardées dans OpenBao
vers leurs Secrets avec External Secrets Operator
([External Secrets Operator](#external-secrets-operator-dans-les-clusters-enfants-2100)).

## Deux modes

| Mode | Où tourne OpenBao | À qui il appartient | Quand le choisir |
|---|---|---|---|
| **A** | votre propre OpenBao (ou un Vault compatible) | à vous : politiques, sauvegardes, descellement | vous faites déjà tourner un serveur de secrets |
| **B1** | un conteneur à côté de la console, sur le même hôte, `127.0.0.1:8210` | à l'agent de l'hôte de la console | vous voulez sortir les secrets du répertoire d'état sans faire tourner de serveur |

Le mode B1 emploie l'image OpenBao de la SUSE Application Collection
(`dp.apps.rancher.io/containers/openbao:2.7.2-5.4`, abonnement Rancher
Prime). harvops ne la redistribue pas : vous construisez un **paquet
OpenBao** à part, avec votre propre compte de service de l'Application
Collection (voir [Construire le paquet B1](#construire-le-paquet-b1)).

Rien ne change tant que le magasin reste en mode local : la 2.9.0 sans
OpenBao se comporte comme la 2.8.3. Seule différence : `install.sh` installe
désormais les unités de l'agent du magasin de secrets (une unité path et une
minuterie horaire, qui ne font que rafraîchir un fichier de santé).

## Ce qui peut passer dans OpenBao

| Classe | Emplacement local | Migrée |
|---|---|---|
| `kubeconfig` | `clusters.d/<n>.yaml`, `kubeconfigs/<n>.yaml` | oui, proposée seulement après la migration d'une autre classe et une panne simulée réussie |
| `ssh-cluster` | `ssh/<n>_id` (clé privée) | oui (`.pub` et `known_hosts` restent locaux) |
| `ssh-kvm` | `ssh/<host>_id` | oui, avec rotation |
| `rancher-oidc` | `rancher.d/<id>.oidc-secret` | oui |
| `replication-notify` | base de réplication, réglages des notifications | oui, champ par champ |
| `replication-hook` | en-têtes secrets des actions annexes de réplication | oui |
| `tf-kvm` | `<workspace>/.secrets/<variable>` | oui |
| `plan-run` | mémoire (2.4.0) | gardés dans OpenBao pendant le déroulé d'un plan, effacés à la fin (7 jours au plus pour un déroulé arrêté) |

Toujours locaux : les comptes de la console, `setup-token`, `htpasswd`, la
clé TLS de la console, toutes les clés publiques, les identifiants de
connexion à OpenBao eux-mêmes, et les kubeconfigs écrits dans `config.yaml`
(ils appartiennent à l'installateur).

Dans OpenBao, un secret vit à `<mount>/data/<prefix>/<class>/<path>`, avec
par défaut le montage `harvester-ops` et le préfixe `consoles/<console id>`.
Chaque écriture porte des `custom_metadata` (`hops_action`, `hops_actor`,
`hops_class`), jamais une empreinte de la valeur.

## Activer le mode A

Paramètres > Magasin de secrets > Activer OpenBao ouvre l'assistant.

1. **Serveur** : adresse (`https://`, `http` seulement sur la boucle
   locale), la CA si elle est privée, l'adresse de la console vue par
   OpenBao (CIDR), le montage et le préfixe. L'assistant montre les
   commandes `bao` à lancer sur votre serveur et le texte exact de la
   politique. La console ne crée rien hors de son préfixe.
2. **Vérifications** : adresse et certificat, état de scellement, et le
   `secret_id` emballé (son chemin de création doit être le rôle de la
   console). Avant la première activation, la connexion est marquée
   « après » : `enable` dépose les identifiants AppRole par l'agent de
   l'hôte, puis vérifie la connexion, KV v2, les capacités de chaque
   chemin, une écriture d'essai sous `<prefix>/check/`, transit et le rôle
   de jeton d'OpenTofu. Il n'active que si tout est vert. Les identifiants
   laissés sur l'hôte par un serveur précédent ne servent jamais à la
   vérification avant activation.
3. **Confirmation** : activer.

Le `secret_id` est toujours remis **emballé**. L'agent examine le jeton
d'enveloppe, vérifie son chemin de création, le déballe et écrit
`/etc/harvester-ops/openbao/secret-id` (`root:harvester-ops 0640`). Un jeton
déjà déballé ou expiré donne `openbao-unwrap-used` : quelqu'un d'autre a
peut-être lu le secret.

Vault : l'API est la même (KV v2, AppRole, enveloppe, transit). Vault 1.14.2
a été vérifié en mode A : une partie des vérifications, l'activation, la
migration et le retour. OpenTofu ne documente son fournisseur de clés
`openbao` qu'avec Vault 1.14 : avec un Vault plus récent, l'onglet État vous
demande de l'acquitter avant que les états soient chiffrés (Vault 1.17.6 a
chiffré des états sans erreur dans nos essais).

## Activer le mode B1

### Construire le paquet B1

Sur une machine qui joint l'Application Collection (ou votre miroir) :

```bash
harvester-secrets bundle --registry dp.apps.rancher.io \
    --user <service account> --password-stdin -o ./bundles/openbao/ < password-file
```

L'outil tire l'image lui-même (bibliothèque standard, pas de `podman
login`). Il vérifie sa signature cosign avec la clé publique de
l'Application Collection et épingle l'empreinte de l'index. Il écrit
`harvester-ops-openbao-<version>-<tag>.tar.gz` (environ 59 Mo, image de
202 Mo une fois chargée) avec son `.sha256`, sa provenance et la signature
telle que servie par le registre. Une étiquette autre que `2.7.2-5.4`
(épinglée) n'est acceptée qu'avec une signature embarquée valide. Un
OpenBao plus ancien que 2.7.2 est refusé.

Placez le paquet sous `bundles/openbao/` de la version de harvops avant
`install.sh`, ou gardez-le sur l'hôte.

### Poser B1

- Depuis la console : Paramètres > Magasin de secrets > Activer OpenBao,
  mode B1, choisir le paquet qu'`install.sh` a placé.
- Sur l'hôte : `sudo /usr/local/bin/harvester-secrets enable --mode b1
  --bundle <file>` (sur openSUSE, le `secure_path` de `sudo` ne contient pas
  `/usr/local/bin`).

L'agent crée le compte `harvester-ops-openbao` et charge l'image. Il écrit
l'unité `harvester-ops-openbao.service` (`--read-only`, `--network host`,
`--memory 512m`, toutes les capacités retirées), une CA et un certificat de
serveur. Il crée une clé de descellement statique
(`/etc/harvester-ops-openbao/unseal.key`, root 0400, remise au conteneur
comme secret podman). Puis il démarre OpenBao avec une initialisation
déclarative : aucun jeton root n'est jamais rendu, aucune clé de
récupération n'existe.

### Copie hors ligne de la clé de descellement

Après l'installation et après chaque rotation de la clé de descellement, la
clé en service attend sa copie hors ligne. Passer une classe dans OpenBao
est refusé (`offline-copy-ack-required`) tant qu'elle n'est pas acquittée :

1. Exporter (taper l'identifiant de la console) : l'agent remet une copie.
2. La télécharger une fois (`no-store` ; un second téléchargement répond
   410).
3. Acquitter avec l'identifiant de la clé. Une mauvaise clé est refusée.

Gardez le fichier loin des instantanés : un instantané sans sa clé ne peut
pas être restauré, et la clé sans l'instantané ne sert à rien.

## Migrer les classes et revenir

Paramètres > Magasin de secrets > Classes, ou `harvester-secrets migrate
--to openbao|local --class <c>`. Chaque secret est écrit avec `cas=0`,
relu, comparé par SHA-256 en mémoire. Puis la valeur locale est remplacée
par une référence (`openbao:<class>/<path>#<field>`). L'original est gardé
sous `<file>.migrated` (0600) pendant 7 jours (de 1 à 30,
`originals_keep_days`), ou jusqu'à **Confirmer**. **Revenir en local**
réécrit la valeur là où elle était et retire la copie d'OpenBao.

`kubeconfig` est proposée en dernier : une autre classe doit avoir migré et
**Tester la panne** doit avoir réussi.

## Cache et pannes

Les valeurs lues dans OpenBao sont gardées en mémoire 15 minutes (de 1 à
120). Jamais sur disque : les copies de travail remises à `kubectl`, `ssh`
ou `tofu` sont des fichiers 0600 sur tmpfs (`/tmp/hops-secrets` dans le
conteneur), effacés après l'action.

| Ce qui arrive | Ce que vous voyez |
|---|---|
| OpenBao arrêté, valeur en cache | l'action passe ; bandeau « OpenBao ne répond pas, depuis HH:MM ; servi depuis la mémoire jusqu'à HH:MM » |
| OpenBao arrêté, cache expiré ou vidé | `503 openbao-unreachable: <class>/<name> (<path>: ...)` |
| scellé | `openbao-sealed` |
| jeton révoqué sur le serveur | la console se reconnecte par AppRole, l'action passe |
| politique restreinte | `openbao-denied ... (capabilities: deny)` pour les classes hors de la politique |
| journal d'audit plein | `openbao-internal` après deux réponses 500, bandeau de panne ; `sys/health` dit encore descellé |

Il n'y a pas de copie locale de repli. Les connexions à un cluster déjà
ouvertes par la console continuent de fonctionner jusqu'à leur fermeture,
même quand le kubeconfig ne peut plus être relu. **Vider le cache** agit
sur le processus de la console.

## Rotation

| Secret | Comment |
|---|---|
| `secret_id` de la console | tous les 30 jours par l'agent de l'hôte (minuterie horaire), ou **Faire tourner** |
| clé de descellement de B1 | sur demande (taper l'identifiant) : nouvelle clé, redémarrage avec les deux, `sys/rotate/root`, ancienne clé retirée et gardée 14 jours pour les instantanés qui en ont besoin |
| clé SSH d'un hôte KVM | sur demande ; âge affiché, signalée après 365 jours ; nouvelle clé ajoutée, essayée, rangée, ancienne clé retirée et vérifiée refusée ; un échec au milieu remet l'état précédent |
| clé transit d'une déclaration | sur demande ; les états plus anciens restent lisibles |
| secrets que la console ne possède pas (OIDC, jeton Matrix, kubeconfig) | âge affiché ; les remplacer là où ils se règlent |

Après une rotation de clé KVM, un retour à une version ou une restauration
lancés depuis la console, son cache est vidé.

Versions (Paramètres > Magasin de secrets > Versions) liste qui a écrit
quelle version (auteur, action), jamais une valeur. L'onglet peut ramener
un secret à une version plus ancienne. Ramener la clé SSH d'un hôte KVM à
une version antérieure à sa dernière rotation donne une clé que l'hôte
refuse déjà.

## Instantanés (B1)

- Répertoire : `/var/lib/harvester-ops-snapshots` sur l'hôte, monté dans le
  conteneur de la console (créé s'il manque). Vous pouvez y monter un
  partage (NFS sous SELinux :
  `context="system_u:object_r:container_file_t:s0"`). Un partage en
  root-squash fonctionne : l'agent le lit sous le compte de la console. Le
  répertoire d'état est refusé (il est sauvegardé avec la console).
- Planification : chaque jour à `snapshot_time` (02:30 par défaut, **UTC**,
  l'horloge du conteneur), 14 gardés. Un instantané en échec est retenté
  après une heure et affiché dans État ; il n'est jamais relancé à chaque
  passage.
- Fichiers `openbao-<UTC stamp>-<key id>.snap` (0600). Un instantané est
  chiffré par la barrière : les valeurs et les textes des politiques ne
  sont pas lisibles, les noms des clés de stockage le sont.
- `sudo harvester-secrets snapshot` est refusé (il agirait en root sur
  l'état de la console) : `sudo -u harvester-ops harvester-secrets snapshot
  --dest DIR`.

### Restaurer

Paramètres > Magasin de secrets > Instantanés > Restaurer (taper
l'identifiant). Avant tout arrêt, la console liste ce que la restauration
casserait, et vous l'acquittez :

- les actions en cours (elles s'arrêtent avec la console) ;
- les déclarations chiffrées dont l'état a été écrit après l'instantané :
  la clé transit naît au premier chiffrement (souvent bien après la
  déclaration) et une rotation ajoute une version, donc un tel état peut
  demander une clé que l'instantané n'a pas et devenir illisible (un état
  réécrit avec une clé qui existait déjà est listé aussi, sans danger) ;
- les hôtes KVM dont la clé SSH a été tournée depuis (OpenBao rendrait une
  clé que l'hôte refuse) ;
- les classes passées dans OpenBao après l'instantané (leurs secrets n'y
  sont pas).

L'agent arrête alors la console et OpenBao. Il met le volume actuel de côté
pour 7 jours (`/var/lib/harvester-ops-openbao.old-<UTC stamp>`), démarre
une instance neuve avec la clé de l'instantané et le restaure. Puis il
relit la configuration restaurée face à une **liste permise** de ce que
cette console attend : noms et textes exacts des politiques (la politique
`default` comparée à l'empreinte notée à l'installation ou à la première
remise à niveau des politiques ; si cette lecture a échoué, le journal de
l'agent le dit et `default` n'est pas comparée avant la remise à niveau
suivante), méthodes
d'authentification `token` et `approle` seulement, les deux rôles AppRole
avec leurs politiques, plages d'adresses et `secret_id` unique, le rôle de
jeton d'OpenTofu, aucun groupe d'identité, des entités d'identité sans
politique dont les alias sont les deux rôles AppRole ou `tf-<déclaration>`
sur `token/`, aucun espace de noms, les montages et le périphérique d'audit
attendus. Toute différence refuse l'instantané et remet le volume
précédent. Ensuite, chaque jeton de l'instantané est révoqué ; la console
et l'agent se reconnectent. Les politiques de l'agent et du jeton
d'OpenTofu sont réécrites avant la relecture : un instantané qui ne fait
que les élargir est réparé, pas refusé.

Un alias d'entité `tf-<déclaration>` est accepté pour tout nom bien formé,
pas seulement pour les déclarations vivantes : un instantané pris avant le
retrait d'une déclaration garde son entité, et le rôle de jeton accepte
`tf-*` parce que la console crée l'alias d'une nouvelle déclaration à sa
première action. Une telle entité ne porte aucune politique : elle ne donne
rien de plus que la politique des jetons d'OpenTofu.

Une restauration lancée depuis la console arrête la console, et avec elle
l'outil qui rangerait les réglages d'après l'instantané : la console
redémarrée lance `harvester-secrets restore-settle` une fois pour cette
restauration. Si une autre opération de l'agent passe avant le retour de la
console (par exemple `install.sh` qui met `b1-policies` en file), ce
rangement ne fait rien : lancez `sudo /usr/local/bin/harvester-secrets
restore-settle` à la main, ou restaurez depuis l'hôte. Un secret migré après
l'instantané est perdu (la vérification préalable cite la classe) ; un test
SSH du cluster répond alors 500.

Depuis la 2.10.0, la liste blanche accepte aussi ce que la console rend pour
les jetons personnels et ESO, et rien d'autre : les méthodes
`jwt-hops-<id>-p-*` et `jwt-hops-<id>-eso-*` avec leur configuration et
leurs rôles exacts (le rôle `eso` lié aux namespaces et aux UID de comptes
notés), les politiques `hops-person-<id>`, `hops-group-<id>-*` et
`hops-eso-<id>-*` au texte rendu par la console d'après ses réglages, les
groupes `hops-<id>-*` qui ne portent que leur politique `hops-group`, les
entités sans politique dont les alias sont sur ces méthodes, et le rôle de
jeton d'OpenTofu lié ou non à `127.0.0.1/32`. Après une restauration,
`harvester-secrets restore` range les réglages d'après ce que porte
l'instantané (méthodes, groupes, clusters ESO, écouteur ; jetons personnels
éteints s'il ne reste aucune méthode), et l'écouteur ESO est éteint s'il ne
reste aucun cluster ESO. Un instantané pris avant l'activation d'un
namespace pour ESO est refusé (sa liste de namespaces et d'UID diffère).

Une restauration à la main (`sudo harvester-secrets restore <file>`) ne
vérifie pas les actions en cours de la console. La vérification préalable
ne liste que les clés KVM tournées depuis la console : une rotation lancée
à la main par `harvester-secrets rotate ssh-kvm` n'y figure pas.

## États OpenTofu chiffrés

Avec OpenBao actif et OpenTofu (pas Terraform), l'état de chaque
déclaration est chiffré par le moteur transit : une clé par déclaration, un
jeton enfant limité à cette clé et à l'action. Les états en clair existants
sont migrés à l'action suivante.

Limites connues :

- **La migration demande le provider et le cluster** : elle passe par
  `apply -refresh-only`. La première action d'une déclaration après
  l'activation (même un plan) rafraîchit donc et écrit l'état, dérive
  comprise. Un cluster injoignable fait échouer cette action à
  `encrypt-state` tant que la migration ne peut pas passer.
- **Les copies d'avant un déplacement restent en clair dans l'espace
  partagé** : quand des ressources sont reprises par une déclaration
  chiffrée, la copie `.pre-move-*` de l'espace partagé du cluster reste en
  clair (l'espace partagé n'est pas une déclaration). Retirez-la avec le
  nettoyage de l'espace.
- **Une lecture peut créer un jeton** : la vue d'une déclaration (rôle
  lecteur) dont le cache d'adresses est périmé demande à OpenBao un jeton
  enfant, et peut créer la clé transit. Ce n'est pas une action suivie.
- **Remettre en clair, puis toute action rechiffre** : le bouton sert
  « avant de désactiver OpenBao » ; il n'y a pas de refus par déclaration
  tant qu'OpenBao est actif. `disable` refuse tant qu'un état est chiffré.
- Les déplacements d'état passent par `state pull` / `state push -` ;
  `-state-out` est refusé (il écrit la destination en clair).
- Les clés transit d'une déclaration supprimée sont gardées 30 jours, puis
  retirées par le balayage quotidien, sauf si la déclaration existe de
  nouveau avec le même identifiant. La console peut retirer les clés
  `hops-<console id>-*` de son propre préfixe.

## Secrets d'un plan de déploiement

Pendant le déroulé d'un plan, les secrets tapés pour lui restent dans
OpenBao (`plan-run/<action id>/...`) : un plan arrêté reprend sans les
retaper. Ils sont effacés à la fin du déroulé, après 7 jours pour un
déroulé arrêté, ou par **Oublier les secrets gardés**.

## Audit et corrélation

Chaque requête porte `X-Hops-Action` (l'identifiant de l'action de la
console, ou `bg-<task>`) et `X-Hops-Actor` (la personne, ou `service`),
notés en clair dans le journal d'audit d'OpenBao. En B1, le journal d'audit
est déclaré dans la configuration. OpenBao refuse les requêtes qu'il ne
peut pas journaliser : la console avertit donc sous 1 Gio libre ou 10 % du
volume.

## Rôles

- Écritures sur `/api/secrets/*` et toute action du magasin de secrets :
  administrateur.
- `GET /api/secrets/status` : opérateur (la carte Disaster Recovery).
- Le bandeau : tout le monde.
- Jetons personnels et ESO (mise en place, clés, écouteur, retrait) :
  administrateur. Écrire une valeur ESO : opérateur qui a le droit
  d'administrer le cluster enfant (lecture de son Secret
  `<cluster>-kubeconfig`), avec son propre jeton.
- Un refus d'une personne par OpenBao rend HTTP 403 ; une panne reste en
  503.
- Les actions du magasin de secrets (`secrets:*`) nomment des secrets et
  des chemins : elles ne sont montrées dans le dock, le détail des actions,
  le flux et l'Activité qu'aux administrateurs. De même pour les journaux de
  l'agent (`secrets-agent-*.log`) dans l'Activité et `/api/logs/`.

## Jetons personnels (2.10.0)

Éteint par défaut après la mise à jour : rien ne change tant qu'un
administrateur ne l'allume pas. Allumé, une personne connectée à la console
par Rancher (connexion unique) lit et écrit les **classes personnelles**
(`ssh-cluster`, `ssh-kvm`, `tf-kvm`, et la classe `eso`, en écriture
seulement) avec un jeton OpenBao qui lui est propre. OpenBao juge lui-même
chaque requête et son journal d'audit nomme la personne, sans dépendre d'un
en-tête posé par la console.

### Qui agit, avec quelle identité

| Situation | Identité présentée à OpenBao |
|---|---|
| Personne connectée par la connexion unique de Rancher, requête qui lit ou écrit une classe personnelle | le jeton de la personne |
| Même personne, autres classes (`kubeconfig`, `rancher-oidc`, `replication-*`, `plan-run`, jetons enfants d'OpenTofu) | service (AppRole) |
| Vues KVM partagées (tableau des hôtes, relevés, vue de toutes les VMs) | service ; les gestes sur un hôte gardent le jeton de la personne |
| Connexion directe à Rancher (identifiant et mot de passe), compte local de la console | service |
| Fil de travail d'une action après son déclenchement | aucune lecture nouvelle avec un jeton de personne : les secrets sont lus au déclenchement et tenus par le bail de l'action ; une lecture tardive passe par le service et est marquée |
| Tâches de fond, ligne de commande, `sudo harvester-secrets` | service |

**Pas de repli silencieux** : quand OpenBao refuse une personne qui a un
jeton, elle reçoit le refus (HTTP 403, `openbao-denied` avec la classe et le
nom, ou `openbao-person-login-failed`) ; la console ne relit jamais le même
secret avec l'identité de service. Le service ne sert qu'aux sessions sans
jeton OIDC et quand le réglage est éteint.

Activité marque chaque action qui a lu un secret : **jeton personnel**,
**service** ou **service tardif** (lu par le service après le
déclenchement). Les écritures portent le `custom_metadata` `hops_identity`
(`person` ou `service`) à côté de `hops_actor`.

### Fonctionnement

- Une méthode d'authentification `jwt` d'OpenBao par Rancher dont la
  connexion unique est réglée : `jwt-hops-<id de console>-p-<id du
  Rancher>`. Elle fait confiance au fournisseur OIDC de ce Rancher
  (`oidc_discovery_url` = `<url de Rancher>/oidc`, RS256 seulement,
  `default_role = person`).
- La console présente le **jeton d'identité** de Rancher (`id_token`),
  jamais le jeton d'accès : celui-ci porte un jeton Rancher utilisable sur
  tous les clusters de la personne. Les deux ont la même audience, OpenBao
  ne les distingue donc pas ; la console refuse d'envoyer un JWT qui porte
  `token` ou `scope`.
- Rôle `person` : `user_claim = sub`, `bound_audiences` = client OIDC de la
  console, `claim_mappings` `name` et `auth_provider` (métadonnées `name`,
  `provider`, visibles dans l'audit), politique `hops-person-<id de
  console>`, jeton de 15 minutes non renouvelable au-delà, lié à l'adresse
  de la console (`127.0.0.1/32` en B1, la plage de la console en mode A :
  une connexion d'ailleurs est refusée en 403). Jamais de `groups_claim` (un
  rôle qui en porte un refuse toute personne dont le jeton n'a pas de
  groupes).
- Le jeton OpenBao d'une personne vit en mémoire de la console seulement,
  par session. La console se connecte à la première lecture personnelle, se
  reconnecte avec le nouveau jeton d'identité à chaque rafraîchissement
  Rancher (toutes les 7 à 8 minutes avec un client de 10 minutes) et révoque
  le précédent ; à la déconnexion, à la fin de session ou au refus du
  rafraîchissement, elle révoque le jeton et vide les entrées de cache de
  cette personne. Rancher 2.14.1 rend un nouvel `id_token` à chaque
  rafraîchissement : aucun jeton de longue durée n'est nécessaire.
- Politique `hops-person-<id de console>` : lecture, création et mise à jour
  sur les chemins `ssh-cluster`, `ssh-kvm` et `tf-kvm` et leurs métadonnées ;
  création et mise à jour (sans lecture) sur `eso` et ses métadonnées ;
  `sys/capabilities-self`. Rien sur `kubeconfig`, `rancher-oidc`,
  `replication-*`, `plan-run`, transit ni AppRole.
- Cache : une lecture d'une personne passe toujours par OpenBao avec son
  jeton ; la valeur alimente ensuite le cache commun. L'entrée (entité,
  référence) ne sert que la même personne, pendant une panne, la durée du
  cache.
- Une entité d'identité par personne et par méthode reste dans OpenBao
  après la dernière connexion. Elle ne porte aucune politique. La retirer
  revient à l'administrateur d'OpenBao ; la console liste les entités
  qu'elle a vues.

### Allumer

Paramètres > Magasin de secrets > **Jetons personnels** (administrateurs).
Pour chaque Rancher de Paramètres > Connexion par Rancher :

1. **Mettre en place** : en B1, l'agent crée la méthode, sa configuration,
   le rôle et la politique, sans redémarrer OpenBao (découverte seulement :
   les clés statiques sont refusées en B1, `person-token-static-mode-a`). En
   mode A, l'assistant rend les commandes `bao` et le texte exact de la
   politique pour l'administrateur de votre OpenBao ; OpenBao doit joindre
   l'adresse de découverte de Rancher, sinon des clés statiques tirées du
   JWKS de Rancher (les commandes portent l'empreinte du JWKS et la façon de
   la recalculer : faire confiance à ces clés est la décision de
   l'administrateur d'OpenBao). La console n'écrit elle-même aucune
   politique, aucun rôle ni aucune méthode.
2. **Vérifications**, par une connexion réelle de l'administrateur qui ouvre
   la fenêtre (session à connexion unique de ce même Rancher, avec un jeton
   d'identité) : document de découverte, connexion, jeton (politiques
   `default` et `hops-person-<id>`, lié à la console, 900 s au plus),
   capacités (classes personnelles lisibles, `eso` en écriture seule, autres
   classes et autres consoles refusées), révocation. **Activer** reste grisé
   tant que les cinq ne sont pas vertes dans la fenêtre. Un compte local, une
   connexion directe ou une session d'un autre Rancher voient pourquoi les
   vérifications sont fermées (`openbao-person-no-token`,
   `person-token-other-rancher`).
3. **Activer** : à chaud, sans redémarrer la console ni OpenBao. Toute
   session à connexion unique de la 2.10.0 garde déjà son jeton d'identité :
   les sessions ouvertes avant l'activation passent au jeton personnel dès
   leur prochaine lecture personnelle. `openbao-person-no-token` concerne une
   session sans jeton d'identité (compte local, connexion directe) qui tente
   une vérification.

**Désactiver** remet tout sur le service à chaud et révoque les jetons en
service. **Retirer** (identifiant retapé) retire aussi d'OpenBao la méthode
d'un Rancher ; **Tout retirer** retire toutes les méthodes et tous les
groupes.

### Groupes (groupes internes d'OpenBao)

Rancher 2.14.1 n'émet pas de claim `groups` (et refuse la portée `groups`,
ce qui fait échouer toute la connexion) : la console ne la demande jamais en
2.10.0. Les groupes sont des groupes d'identité **internes** d'OpenBao,
`hops-<id>-<nom>`, avec une politique `hops-group-<id>-<nom>` (lecture
limitée à `<classe>/<cluster>` pour les classes et clusters choisis), dont
les membres sont des entités choisies parmi celles que la console a vues.
Ajouter ou retirer un membre vaut dès la requête suivante, sur le jeton déjà
émis. En B1 l'agent écrit les groupes ; en mode A l'assistant rend les
commandes. Le réglage **Lecture seulement par groupe** réduit
`hops-person-<id>` à `sys/capabilities-self`.

En B1, l'agent peut poser n'importe quelle politique sur un groupe
`hops-<id>-*` : `allowed_parameters` ne l'en empêche pas (une chaîne à
virgules contourne le motif). L'agent est root sur l'hôte et détient la clé
de descellement ; la garde est la liste fermée des opérations de l'agent et
la revue de restauration.

### Clés de signature de Rancher

La console relit l'empreinte du JWKS de Rancher chaque heure. Avec des clés
statiques (mode A), une clé changée lève `openbao-jwt-keys-changed` et un
bandeau pour les administrateurs : **Mettre à jour les clés** écrit
l'ancienne et la nouvelle (`jwt_validation_pubkeys` en porte plusieurs),
**Confirmer** retire l'ancienne une fois les sessions passées sur la
nouvelle. Un JWKS illisible trois heures de suite lève
`openbao-jwt-keys-unreadable`. Avec la découverte, OpenBao suit les clés
lui-même.

### Limites des jetons personnels

- En B1, l'émetteur, le client et la CA d'un Rancher sont épinglés dans
  l'état de root à la première pose (confiance à la première utilisation)
  et ne changent que par `sudo harvester-secrets person-token ...` ;
  l'épinglage survit au retrait de la méthode.
- Une personne qui écrit une classe personnelle peut aussi réécrire le
  `custom_metadata` (`hops_actor`, `hops_identity`) et `max_versions` des
  secrets de cette classe : l'attribution montrée par la console est
  déclarative, le journal d'audit d'OpenBao (entité, `sub`, `name`) fait
  foi.
- Un jeton d'identité trouvé invalide à un rafraîchissement Rancher ferme la
  session de la console (la personne se reconnecte), jetons personnels
  allumés ou non.
- Révocation : une déconnexion de la console révoque le jeton personnel
  aussitôt ; une déconnexion côté Rancher se voit au rafraîchissement
  suivant (refusé), donc en 10 minutes environ ; **Désactiver** révoque en
  30 secondes environ. Les jetons d'un processus de console arrêté
  (redémarrage, plantage) ne sont pas révoqués : ils expirent avec leur
  durée (15 minutes au plus), et ne marchent que depuis l'adresse de la
  console.
- Avec **Lecture seulement par groupe**, les administrateurs ont aussi
  besoin d'un groupe pour les classes personnelles.
- En mode A avec clés statiques, l'alerte « clés changées » disparaît dès
  que les commandes de mise à jour sont rendues, avant que l'administrateur
  d'OpenBao les passe.
- Les lectures personnelles faites au déclenchement d'une action portent
  `X-Hops-Action: bg-console` et non l'identifiant de l'action, et la ligne
  de connexion d'une personne (première connexion et reconnexion à chaque
  rafraîchissement) porte `X-Hops-Actor: service` ; les lignes de données et
  la réponse de connexion nomment quand même la personne (entité, alias,
  `name`).
- La garde de retour lit les réglages de la console, pas l'état de root :
  après un `person-token disable --remove` qui a échoué à mi-chemin,
  vérifiez l'onglet Jetons personnels (et `bao auth list` en mode A) avant
  de revenir en arrière.

## External Secrets Operator dans les clusters enfants (2.10.0)

Option des clusters enfants RKE2 créés par la console (Cluster API sur
Harvester), administrateurs seulement, OpenBao actif seulement
(`eso-needs-openbao`). Jamais sur un cluster Harvester. La console pose
l'opérateur, son accès à OpenBao et un magasin par namespace d'équipe
activé, et montre l'état de synchronisation. Les objets `ExternalSecret`
appartiennent aux équipes (GitOps ou leurs propres outils) ; une rotation
dans OpenBao atteint seule leurs Secrets.

### Ce que la console pose

- **Chart** : le chart officiel `external-secrets` dans un paquet
  d'artefacts (élément `chart` nommé `eso`, comme `flux2`), installé par
  CAAPH dans le namespace `external-secrets` depuis le registre du cluster
  enfant. Le paquet porte la version (vérifié avec le chart 2.14.0,
  appVersion v2.14.0, API `external-secrets.io/v1`). Valeurs imposées par la
  console : CRD par le chart, webhook et contrôleur de certificats allumés,
  magasins de portée cluster, `ClusterExternalSecret` et `PushSecret`
  éteints, bitwarden et service monitor éteints, pas de cache de jetons.
- **Par namespace activé** (étiquette `harvester-ops.io/eso: "on"`) : un
  compte de service `hops-eso` sans aucun droit Kubernetes et un
  `SecretStore` `hops-openbao` (fournisseur `vault`, KV v2, CA d'OpenBao en
  ligne, `auth.jwt` avec le rôle `eso`, audience du jeton
  `hops-openbao-<id de console>`, 600 s), livrés par le ClusterResourceSet
  `art-eso` une fois le chart prêt. Les namespaces système sont refusés
  (`eso-system-namespace`).
- **Dans OpenBao** : une méthode `jwt` par cluster enfant,
  `jwt-hops-<id>-eso-<clé>` (clé = 8 hexadécimaux du SHA-256 de
  `<harvester>/<namespace>/<cluster>`), avec les clés publiques du JWKS des
  comptes de service du cluster enfant, lues par la console avec son
  kubeconfig (RKE2 refuse la lecture anonyme du JWKS : OpenBao ne joint
  jamais l'API du cluster enfant et ne garde aucun jeton de relecteur). Rôle
  `eso` : audience `hops-openbao-<id>`, `bound_claims` sur le nom de compte
  `hops-eso`, la liste des namespaces activés et l'**UID** de chaque compte
  `hops-eso` (un namespace ou un compte recréé sous le même nom est refusé
  jusqu'à une nouvelle activation : `eso-account-changed`), jetons de 10
  minutes liés au réseau des nœuds du cluster enfant quand il est connu.
  Une politique à gabarit par cluster : un namespace ne lit que
  `eso/<clé>/<son namespace>/*`.
- **Valeurs** sous le préfixe de la console, `eso/<clé>/<namespace>/<nom>`.
  La console ne les lit jamais. Une personne en écrit une depuis l'onglet
  External Secrets du cluster enfant avec son propre jeton (champs masqués,
  jamais réaffichés), ou l'équipe les écrit directement dans OpenBao.

Avant de poser le magasin, un pod du réseau des VMs sonde OpenBao
(`GET sys/health` en TLS avec la CA) : `eso-openbao-unreachable` bloque.

### Mode B1 : l'écouteur ESO

En B1, OpenBao n'écoute que sur `127.0.0.1`. ESO a besoin d'un **second
écouteur TLS** sur une adresse de l'hôte. Réglez son adresse par
`harvester-secrets eso listener --addr <ip>:8212` (ou la carte External
Secrets de Paramètres > Magasin de secrets) : l'adresse est gardée, et
l'écouteur s'allume avec le premier cluster enfant qui reçoit ESO et
s'éteint avec le dernier. Les rôles de la console restent liés à
`127.0.0.1`.

- L'adresse doit être **statique** sur l'hôte. À chaque démarrage, l'unité
  B1 vérifie que l'adresse est présente (`ip -o -4 addr`) et, sinon, démarre
  OpenBao sur la boucle locale seule et le dit (la console continue de
  fonctionner, ESO cesse de se synchroniser) ; le passage horaire de santé
  de l'agent lève `eso-listener-address-absent` et un bandeau : une adresse
  perdue se voit en moins d'une heure.
- L'écouteur a **sa propre CA** (la CA de B1 est contrainte à `127.0.0.1` et
  sa clé n'est pas gardée), contrainte à l'adresse de l'écouteur et refaite
  à chaque allumage ; les magasins posés alors la portent.
- Ouvrez le port dans le pare-feu de l'hôte aux **seuls réseaux des nœuds
  des clusters enfants**, par exemple
  `firewall-cmd --zone=public --add-rich-rule='rule family=ipv4 source address=<réseau des nœuds enfants> port port=8212 protocol=tcp accept'`
  (et la même avec `--permanent`). La console ne touche pas au pare-feu.
- Les jetons enfants d'OpenTofu sont liés à `127.0.0.1/32` et les points
  d'entrée de re-chiffrement non authentifiés sont coupés : l'écouteur
  n'expose que ce dont ESO a besoin.
- Un paquet B1 construit avant la 2.10.0 n'a pas la ligne de l'écouteur
  dans son modèle : `upgrade-b1` avec un tel paquet est refusé tant que
  l'écouteur est allumé (`eso-listener-template`).
- À l'inverse, un paquet B1 construit par la 2.10.0 est pour une console
  2.10.0 : `enable --mode b1` avec lui sur une console 2.9.x échoue (« a
  storage backend must be specified », puis `b1-start-timeout`, défait
  proprement). Après un retour arrière, gardez la B1 déjà posée (la 2.10
  laisse `openbao.hcl.tpl` lisible par la 2.9 et garde son propre modèle
  dans `openbao-eso.hcl.tpl`) ou construisez le paquet avec la version 2.9.x.
- Le certificat de l'écouteur vaut 730 jours et n'est pas renouvelé seul :
  éteignez puis rallumez l'écouteur (sans cluster ESO restant) ou
  déplacez-le avant son expiration ; la date est dans
  `/etc/harvester-ops-openbao/tls/eso.pem`.

Le port de cluster de l'écouteur reste sur la boucle locale
(`127.0.0.1:<port + 1>`) : sans cela, OpenBao ouvrirait aussi
`<adresse>:<port + 1>` sur le réseau des enfants. Les ports 8209 à 8211 et
65535 sont refusés.

En mode A, l'adresse d'OpenBao des réglages de la console est celle que
prennent les magasins : elle doit donc être joignable depuis les nœuds
enfants (la plage AppRole de la console est alors l'adresse de l'hôte, pas
`127.0.0.1`). Les actions de mise en place, de namespace et de retrait se
terminent par les commandes pour l'administrateur de votre OpenBao (méthode,
rôle, politique du cluster), montrées dans une fenêtre avec copie ; les
magasins restent `InvalidProviderConfig` tant qu'elles ne sont pas passées.
Pour les rejouer, sautez la première ligne (`bao auth enable`) quand la
méthode existe déjà.

### Retirer ESO (décision 300)

**Retirer les secrets externes** (onglet du cluster enfant, ou `harvester-capi
eso-remove`), dans cet ordre : les `ownerReferences` qui lient les Secrets
synchronisés à **nos** objets `ExternalSecret` sont retirées (les Secrets
restent, figés à leur dernière valeur), ces objets sont supprimés tant
qu'ESO tourne (leur finalizer n'est retiré que par le contrôleur ;
`eso-remove-stuck` si c'est trop long), puis le ClusterResourceSet, les
étiquettes des namespaces, le chart, et la méthode, le rôle et la politique
dans OpenBao. « Nos » objets : dans un namespace activé, pointant vers le
magasin `hops-openbao`, non livrés par Flux. Les `ExternalSecret` des
équipes ne sont pas touchés ; tant qu'il en reste, retirer le chart est
refusé (`eso-foreign-objects`, avec leurs noms), car le chart emporte les
CRD.

Le **Défaire** d'un plan de déploiement qui a posé ESO retire nos objets
avec la phase ESO et garde le chart tant que des `ExternalSecret` d'équipes
restent dans un cluster enfant qui reste : l'élément du chart finit en
`eso-foreign-objects: chart kept, ...`. Retirez ces objets (ou les entrées
GitOps de leurs propriétaires), puis défaites de nouveau. La même règle
vaut quand le cluster enfant part dans le même Défaire (le chart bloquerait
aussi son retrait) : laissez décoché l'élément du chart `eso` et le retrait
du cluster emporte tout. Retirer le chart `eso` par un autre chemin
(Services ou Artefacts, après **Retirer les secrets externes** en gardant le
chart) est refusé de la même façon.

### Limites

- Un jeton de compte de service révoqué dans Kubernetes reste accepté par
  OpenBao jusqu'à son expiration (10 minutes).
- Les clés d'un cluster enfant sont lues par la console et crues à la
  première pose ; le passage horaire compare leur empreinte
  (`eso-keys-changed`, **Mettre à jour les clés**). Les clés de comptes de
  service de RKE2 ne tournent pas seules ; un renouvellement des machines
  les garde.
- Une console compromise pourrait poser sa propre clé sur la méthode d'un
  cluster enfant et lire `eso/<clé>/<ns>/*`, qu'elle peut déjà lire et
  écrire : l'attribution d'une lecture ESO dans l'audit n'est pas une
  garantie face à une console compromise.
- Mode A : l'onglet reste sur « en attente de l'administrateur d'OpenBao »
  même quand les magasins sont prêts (pas d'acquittement).
- Un compte `hops-eso` supprimé à la main est signalé `eso-account-changed`
  (et non manquant), et le ClusterResourceSet ne le recrée pas : réactivez le
  namespace.
- Retirer le chart retire aussi son namespace de release `external-secrets`.
  Les entités OpenBao des connexions d'ESO restent (l'administrateur
  d'OpenBao les retire).
- ESO se connecte à chaque réconciliation et révoque son jeton ensuite :
  gardez `refreshInterval` à 1 h ou plus (l'exemple que montre la console)
  pour un journal d'audit raisonnable.

## Désactiver, changer de mode, revenir à une console plus ancienne

- `disable` est refusé tant qu'une classe est dans OpenBao ou qu'un état
  est chiffré. Il efface d'abord les secrets des déroulés de plan arrêtés.
  `disable --remove-b1` arrête et retire aussi l'unité B1 et ses secrets
  podman, et met les données de B1 de côté ; le compte, les images et le
  paquet restent (voir plus bas).
- De B1 vers A : ramener toutes les classes (`migrate --to local`),
  `disable --remove-b1`, puis activer le mode A.
- **Revenir à une console antérieure à la 2.9.0** (Mise à jour > Revenir
  en arrière, ou Installer en autorisant une version plus ancienne) est
  refusé tant que le magasin n'est pas local, qu'une classe est dans
  OpenBao ou qu'un état est chiffré (`secrets-in-openbao` ; aussi quand
  `secrets-settings.json` existe mais ne se lit pas). Le refus donne les étapes
  à faire d'abord : ramener toutes les classes, remettre en clair les états
  chiffrés, désactiver le magasin. Une console 2.8.x lirait les références
  `openbao:` comme des chemins ou des valeurs : les clusters au kubeconfig
  migré échouent (une action `status` se dit même terminée alors que
  kubectl a échoué), les clés SSH sont introuvables, les notifications
  envoient la référence comme jeton. `install.sh` lancé à la main avec une
  version plus ancienne n'est pas gardé : suivez d'abord les mêmes étapes.
  Le conteneur B1 et les unités de l'agent de la 2.9.0 continuent de
  tourner après une telle installation ; les originaux `.migrated` restent
  sur disque jusqu'à leur expiration.
- **Revenir sous la 2.10.0** en B1 est refusé tant que les réglages portent
  une méthode `jwt` d'un Rancher (réglée ou en attente), un groupe,
  l'écouteur ESO ou un cluster ESO (`secrets-person-token-b1`) : une console
  2.9.x refuserait ses propres instantanés (la revue de la 2.9.0 ne connaît
  pas ces objets). Étapes d'abord : `harvester-secrets person-token disable
  --remove` et `harvester-secrets eso remove --all` (après avoir retiré ESO
  de chaque cluster enfant). En mode A, le retour est permis avec un
  avertissement dans l'action et la réponse : retirez vous-même de votre
  OpenBao les méthodes, politiques et groupes dont vous ne voulez plus.

### Ce qui reste après `disable --remove-b1` et après un retour arrière

`disable --remove-b1` arrête et retire l'unité B1 et son secret podman, et
met les données de côté (jamais supprimées sur le moment) :

- gardés exprès : `/var/lib/harvester-ops-openbao.removed-<stamp>`,
  `/etc/harvester-ops-openbao.removed-<stamp>` (clés de descellement) et les
  volumes `.old-<stamp>` des restaurations précédentes, les instantanés de
  `/var/lib/harvester-ops-snapshots` ;
- laissés en place : le compte `harvester-ops-openbao`, les images
  `localhost/harvester-ops-openbao:*` et l'image de l'Application
  Collection, le paquet dans `/var/lib/harvester-ops/secrets-agent/staged/`,
  le répertoire vide `/etc/harvester-ops/openbao`.

#### Retirer les restes de B1, console gardée en 2.9.x

Gardez `/opt/harvester-ops/openbao`, `harvester-secrets-agent.path`,
`harvester-secrets-agent.service` et `harvester-secrets-rotate.timer` : la
2.9.0 en a besoin pour activer B1 de nouveau et pour garder le `secret_id`
de la console tourné.

```bash
# toutes les images B1 existantes : current, le tag de version, et previous
# seulement si une mise à jour a eu lieu (xargs -r n'appelle podman rmi
# zéro fois sur une liste vide : une console jamais mise à jour n'y échoue pas)
sudo podman images --format '{{.Repository}}:{{.Tag}}' \
    | grep '^localhost/harvester-ops-openbao:' | xargs -r sudo podman rmi -i
# l'image de l'Application Collection chargée par le paquet : même registre
# et même tag que ceux utilisés par `harvester-secrets bundle`
# (dp.apps.rancher.io/containers/openbao par défaut ; vérifier `podman
# images` si le paquet venait d'un miroir)
sudo podman rmi -i dp.apps.rancher.io/containers/openbao:<tag-de-version>
sudo rm -f /var/lib/harvester-ops/secrets-agent/staged/harvester-ops-openbao-*
sudo rmdir /etc/harvester-ops/openbao 2>/dev/null || true
sudo userdel harvester-ops-openbao   # recréé automatiquement si B1 est réactivé
```

Si ces secrets peuvent encore servir, gardez d'abord ailleurs une copie du
dernier instantané et de sa clé de descellement.

#### Retirer l'agent de magasin de secrets de la 2.9.0, après un retour en 2.8.x

Une console ramenée en 2.8.x ne connaît pas les unités du magasin de
secrets de la 2.9.0 : `harvester-secrets-agent.path` et
`harvester-secrets-rotate.timer` restent actives (elles ne font que
rafraîchir un fichier de santé une fois le magasin local). Pour les retirer
à la main :

```bash
sudo systemctl disable --now harvester-secrets-agent.path harvester-secrets-rotate.timer \
    harvester-ops-openbao
sudo rm -f /etc/systemd/system/harvester-secrets-agent.{path,service} \
           /etc/systemd/system/harvester-secrets-rotate.timer \
           /etc/systemd/system/harvester-ops-openbao.service /usr/local/bin/harvester-secrets*
sudo systemctl daemon-reload
sudo rm -rf /opt/harvester-ops/openbao /etc/harvester-ops/openbao \
            /var/lib/harvester-ops-openbao.* /etc/harvester-ops-openbao.* \
            /var/lib/harvester-secrets-agent /var/lib/harvester-secrets-agent.lock
```

Retirez ensuite les restes de B1 avec les commandes ci-dessus si ce n'est pas
déjà fait. `uninstall.sh` retire lui-même les unités ; `uninstall.sh --purge`
retire aussi les données et les clés de B1.

## Ligne de commande

`harvester-secrets` (lancé sous le compte de la console, ou avec `sudo` ;
en root, il ne fait lui-même que l'appel à l'agent et lance le reste dans
un processus enfant ramené à `harvester-ops`) : `status`, `assistant`,
`check`, `enable`, `disable`, `migrate`, `confirm`, `outage-test`,
`versions`, `rollback`, `rotate`, `offline-copy`, `snapshot`, `snapshots`,
`settings`, `sweep-orphans`, `restore`, `bundle`, `upgrade-b1`, `file`,
`tf-encryption`, `tf-entity-remove`, `tf-keys`, et depuis la 2.10.0 :

- `person-token status | assistant --rancher ID --issuer URL --client-id ID
  [--ca FILE] [--keys discovery|static] [--console-cidr CIDR] | check|enable
  --rancher ID --id-token-stdin | disable [--rancher ID] [--remove] | groups
  list|set NOM --class C --cluster X [--member ENTITÉ]|remove NOM|only on|off
  | keys --rancher ID [--refresh|--confirm]`. Le jeton d'identité se lit sur
  l'entrée standard seulement, jamais en argument.
- `eso status | listener --addr IP:PORT | --off | assistant|add --cluster
  H/NS/C --namespace NS ... [--kubeconfig K] [--node-cidr CIDR] [--sa-uid
  NS=UID] | keys --cluster H/NS/C [--refresh] | remove --cluster H/NS/C |
  --all`.
- Côté cluster enfant : `harvester-capi eso-deploy`, `eso-status`,
  `eso-remove [--keep-chart]`.
- `restore-settle` : range les réglages d'après la dernière restauration
  (lancé par la console après une restauration qu'elle a lancée).
- Avec `sudo`, un `--spec FICHIER` doit appartenir à root, en mode 0600.

`upgrade-b1 --bundle <file>` : nouvelle image B1 depuis un paquet, avec
d'abord un instantané d'avant mise à jour, et retour à l'image précédente si
OpenBao ne se descelle pas ; un OpenBao plus ancien est refusé.

Un outil lancé à la main lit la racine des espaces Terraform que la console
a notée au démarrage (`<state>/console-paths.json`), sauf si
`HARVESTER_OPS_TF_WORKSPACES` est posée.

## Non vérifié

- OpenBao en cluster chez un client, Vault Enterprise avec namespaces.
- La rotation des clés SSH d'un parc de plusieurs hôtes KVM (un hôte
  vérifié).
- La phase GitOps d'un plan de déploiement arrêté puis repris (un plan avec
  des secrets Git a été arrêté et repris sans les retaper, sans la phase
  GitOps).
- La rotation du `secret_id` à 30 jours déclenchée par la minuterie
  (rotation sur demande vérifiée ; la minuterie n'a rafraîchi que la santé).
- L'avertissement d'espace d'audit de B1 avant le refus sur un volume plein
  (le refus et son code `openbao-internal` vérifiés en mode A).
- Une console qui a le même identifiant qu'une autre sur un serveur de mode
  A partagé (clone, restauration) : chacune balaie les clés et les
  orphelins de l'autre.
- 2.10.0, jetons personnels : des groupes nourris par un claim `groups` de
  Rancher (aucun Rancher avec fournisseur de groupes au banc : seuls les
  groupes internes ont été vérifiés) ; un Rancher derrière Active Directory
  ou SAML ; la rotation réelle de la clé de signature de Rancher (simulée
  avec des clés statiques) ; une déclaration Terraform KVM plus longue que
  le jeton de la personne (le mécanisme a été vérifié par un téléchargement
  d'image KVM de 17 minutes vers node2 : secrets lus au déclenchement avec
  le jeton de la personne, jeton révoqué entre-temps, action terminée) ;
  les jetons personnels sur un Vault autre que 1.14.2.
- 2.10.0, External Secrets : ESO en B1 de bout en bout avec un vrai cluster
  enfant (ESO vérifié en mode A sur un cluster enfant de harvlab2 ;
  l'écouteur de B1, sa CA, le repli de l'unité et la garde de retour sur une
  VM client sans cluster enfant) ; un cluster enfant d'un Harvester à
  plusieurs nœuds ; la fenêtre qui montre les commandes du mode A (les mêmes
  commandes ont été prises dans l'assistant pendant l'essai).
