# 2.10.0 : jeton OpenBao par personne et External Secrets Operator (plan de réalisation)

Conception : `2026-11-02-openbao-secrets.md` (décisions 133 à 145 retenues le
03/11/2026, en particulier 138 « hybride » et 139). Plan précédent :
`2026-11-08-openbao-290-plan.md` (2.9.0, décisions 249 à 278, en particulier
265 « place du jeton par personne » et 264 « External Secrets Operator en
2.10.0 »). Connexion par Rancher : `2026-09-26-connexion-par-rancher.md`
(1.50.0, complétée en 1.56.0 et 1.79.0). Accès et GitOps des clusters
enfants : `2026-11-07-gitops-oidc-280-plan.md` (2.8.0). Point de départ :
2.9.0 (`0eb3384`).

## But

La 2.10.0 achève la décision 138 et livre la décision 139, reportée par le
point 264.

1. **Jeton OpenBao par personne** (seconde moitié de la décision 138 :
   « identité de service pour les tâches de fond et les comptes locaux ; jeton
   personnel pour qui se connecte par Rancher ou OIDC, politiques OpenBao par
   groupe ») :
   - une méthode d'authentification `jwt` d'OpenBao fait confiance au
     fournisseur OIDC de chaque Rancher dont la connexion unique est réglée ;
   - une personne connectée par Rancher lit et écrit les secrets des classes
     dites personnelles avec SON jeton OpenBao : OpenBao refuse lui-même, et
     son journal d'audit nomme la personne sans dépendre d'un en-tête posé par
     la console ;
   - politiques par classe puis par cluster (chemins de la 2.9.0, point 265),
     une politique commune aux personnes et des politiques de groupe ;
   - identité de service (AppRole de la 2.9.0) inchangée pour les comptes
     locaux, les tâches de fond, la ligne de commande et tout ce qui tourne
     après le déclenchement d'une action.
2. **External Secrets Operator (ESO)** en option dans les clusters enfants
   créés par la console (décision 139 : « Secrets Kubernetes écrits par la
   console, External Secrets Operator proposé en option ») : la console pose
   ESO, un magasin par cluster relié à OpenBao, l'authentification de ce
   cluster dans OpenBao, et montre l'état de synchronisation. Une rotation
   dans OpenBao suit seule jusqu'aux Secrets du cluster enfant.

Hors 2.10.0 : connexion OIDC à la console par un autre fournisseur que
Rancher (point 302), ESO dans les clusters Harvester eux-mêmes (point 293),
`PushSecret` (écriture du cluster vers OpenBao), mode B2, secrets dynamiques,
synchronisation par la console des groupes de Rancher vers OpenBao (point
285, option B).

## Architecture

```
bin/lib/openbao_client.py   + login_jwt(mount, role, jwt), jeton porté par
                            l'objet (personne ou service), lookup_self
                            (entité, alias, politiques), revoke_self déjà là
bin/lib/openbao_jwt.py      nouveau (pur) : paramètres de la méthode jwt
                            d'une personne (montage, configuration, rôle) et
                            d'un cluster enfant (ESO), lecture des
                            revendications utiles, empreinte des clés JWKS,
                            décision « personne ou service » par classe
bin/lib/secret_store.py     + Identity (service | personne), registre des
                            jetons de personne d'un processus, cache indexé
                            par (entité, référence), lecture gardée avant
                            remise d'une copie partagée
bin/lib/secret_classes.py   + colonne `person` (classe lue avec le jeton de la
                            personne quand elle en a un), classe `eso`
bin/lib/openbao_policy.py   + politique commune des personnes, modèles de
                            politique de groupe, politique ESO d'un cluster
                            (gabarit d'identité par namespace), règles de
                            l'agent élargies, commandes `bao` de l'assistant
                            (méthode jwt, groupes, ESO), liste blanche de la
                            revue d'un instantané restauré
bin/lib/child_eso.py        nouveau (pur) : valeurs du chart, objets du
                            magasin (SecretStore et compte `hops-eso` par
                            namespace activé), refus des namespaces
                            système, état des
                            ExternalSecret, retrait sans perte des Secrets
bin/lib/capi_manifests.py   + sortes ESO connues (namespacées et de portée
                            cluster), ordre après le chart
bin/harvester-secrets.py    + person-token (assistant, check, enable,
                            disable, groups), eso (assistant, add, keys,
                            remove, status)
bin/harvester-secrets-agent.py  + opérations b1-jwt, b1-jwt-remove,
                            b1-eso-cluster, b1-eso-cluster-remove,
                            b1-eso-listener ; revue de restauration élargie
web/rancher_sso.py          + jeton d'identité gardé et renouvelé dans la
                            session, crochets « renouvelé » et « fermé »
web/app.py                  identité des lectures de secrets figée au
                            déclenchement, routes Paramètres > Secrets >
                            Jetons personnels et > External Secrets, option
                            ESO des clusters enfants, état ESO, garde de
                            retour arrière
web/static/js/secrets.js    + carte « Jetons personnels », assistant, groupes,
                            carte « External Secrets »
web/static/js/              capi-*.js (option et onglet « Secrets externes »
                            du cluster enfant), activity.js (marque
                            « jeton personnel » ou « service »), i18n.js
```

Ce qui est réutilisé, sans copie :

| Besoin | Mécanisme existant | Où |
|---|---|---|
| Client HTTP OpenBao, filtres de jetons, codes d'erreur, en-têtes `X-Hops-*` | `Client`, `scrub`, `OpenBaoError`, `with_headers` | `bin/lib/openbao_client.py` (2.9.0) |
| Magasin, références, copies tmpfs, baux d'action | `MultiStore`, `OpenBaoStore`, `Lease`, `shared_file`, `hold_for` | `bin/lib/secret_store.py` (2.9.0) |
| Identité figée au déclenchement, pas relue dans un fil | `ActionRun.identity_env`, `cluster_user` (v1.32.0) | `web/app.py` |
| Session Rancher, rafraîchissement, fil de renouvellement, fermeture | `Sessions.renew`, `_sso_start_renewer`, `Sessions.close` | `web/rancher_sso.py`, `web/app.py` |
| Vérification d'un jeton d'identité (émetteur, audience, nonce, `sub`) | `check_id_token`, `claims_of` | `web/rancher_sso.py` |
| Plusieurs Rancher, leurs réglages et leur CA | `_RANCHER_STORE`, `_server_settings`, `ca_file` | `web/rancher_servers.py`, `web/app.py` (1.79.0) |
| Agent root, liste fermée, demande par fichier, mise à niveau des politiques | `OPS`, `b1-policies`, `review_restored` | `bin/harvester-secrets-agent.py` (2.9.0) |
| Politique fixe à gabarit d'identité au lieu d'une politique par objet | `tf_child_policy` (essai 8 de la 2.9.0) | `bin/lib/openbao_policy.py` |
| Assistant du mode A (commandes `bao` sans valeur) | `mode_a_commands` | `bin/lib/openbao_policy.py` |
| Chart installé par CAAPH depuis le registre du cluster enfant | élément `chart` d'un paquet, `HelmChartProxy` | `bin/lib/artefacts.py`, `bin/lib/capi_services.py` (2.6.0, 2.7.0) |
| Objets livrés par ClusterResourceSet, retrait par étiquettes | `capi_manifests.render`, `removed`, `delete_order` | `bin/lib/capi_manifests.py` (2.7.0) |
| Kubeconfig d'un cluster enfant lu avec l'identité de la personne | `child_access.session` | `bin/lib/child_access.py` (2.7.0) |
| Sonde depuis le réseau des VMs | `vm_probe.run`, `vm_network_findings` | `bin/lib/vm_probe.py`, `bin/lib/registries.py` (2.6.1) |
| Garde de retour arrière sous une version | refus « rollback below 2.9.0 » | `bin/harvester-ops-update.py`, `bin/lib/self_update.py` (2.9.0) |
| Phase d'un plan de déploiement, reprise, Défaire | `deploy_chain`, `deploy_undo` | `bin/lib/deploy_chain.py`, `bin/lib/deploy_undo.py` (2.4.0, 2.7.0) |

## Contraintes communes (valeurs exactes)

### Qui agit, avec quelle identité

Règle de la décision 138 précisée par les constats ci-dessous (point 281) :

| Situation | Identité présentée à OpenBao | `X-Hops-Actor` |
|---|---|---|
| Personne connectée par Rancher (connexion unique, `kind = "sso"`), requête qui lit ou écrit un secret d'une classe personnelle | jeton de la personne | la personne |
| Même personne, classe non personnelle (`kubeconfig`, `rancher-oidc`, `replication-*`, `plan-run`, jetons enfants d'OpenTofu) | service | la personne |
| Personne connectée par Rancher en connexion directe (`kind = "direct"`, identifiant et mot de passe, aucun jeton OIDC) | service | la personne |
| Compte local de la console | service | le compte |
| Fil de travail d'une action, après le déclenchement | aucune lecture nouvelle avec un jeton de personne : copies remises au déclenchement, tenues par le bail de l'action ; lecture tardive inévitable par le service | la personne qui a lancé l'action |
| Tâches de fond (balayages, instantanés, suiveur de réplication, minuteries, sonde) | service | `service` (`bg-<tâche>` en action) |
| Outil de `bin/` lancé seul, `sudo harvester-secrets` | service (AppRole du processus) | utilisateur de sudo ou `service` |
| Flux de connexion (secret OIDC de Rancher) | service | `service` |

Classes personnelles (colonne `person` de `secret_classes.CLASS_TABLE`) :
`ssh-cluster`, `ssh-kvm`, `tf-kvm`, et la classe nouvelle `eso` (écriture des
valeurs lues par ESO, jamais lue par la console). Les trois premières sont
les seules qu'une personne connectée par Rancher fait lire au moment où elle
agit (constats). Une
politique de groupe peut ouvrir `kubeconfig/<cluster>` pour plus tard, sans
effet en 2.10.0.

**Pas de repli silencieux** (point 282) : une personne qui a un jeton
OpenBao et à qui OpenBao refuse une lecture reçoit le refus
(`openbao-denied`, avec la classe et le nom). La console ne relit jamais avec
l'identité de service ce que la personne n'a pas pu lire. Le repli sur le
service n'existe que pour les sessions qui n'ont pas de jeton OIDC du tout
(compte local, connexion directe) ou quand le réglage est éteint.

### Jeton présenté à OpenBao

- **Le jeton d'identité (`id_token`) de Rancher, jamais le jeton d'accès**
  (point 280). Le jeton d'accès du fournisseur OIDC de Rancher porte un jeton
  Rancher (`token: token-...`) qui marche en Bearer sur l'API de Rancher et sur
  `/k8s/clusters/<id>` (vu en 1.50.0) : le remettre à un OpenBao, souvent
  celui du client en mode A, lui donnerait les droits de la personne sur tous
  ses clusters. Le jeton d'identité n'ouvre rien chez Rancher.
- Contenu (source de Rancher, `pkg/oidc/provider/token.go`) : signé RS256
  avec un `kid` ; `iss` = `<url de Rancher>/oidc`, `sub` = identifiant Rancher
  de la personne (`user-xxxxx` ou `u-xxxxx`), `aud` = [identifiant du client
  OIDC de la console], `exp`, `iat`, `name` (portée `profile`), `nonce`,
  `config_name` (`auth_provider`, déprécié, encore émis), `groups` selon la
  portée (plus bas). `check_id_token` vérifie déjà émetteur, audience, `exp`,
  nonce et `sub`.
- **Le jeton d'accès porte la même audience** (`aud` = [client], plus
  `scope`, `token`, `token_kind`) : `bound_audiences` ne distingue donc pas
  les deux jetons, OpenBao accepterait l'un comme l'autre. La garantie vient
  de la console, seule détentrice des deux (aucun ne quitte le serveur), et
  d'un test qui refuse tout envoi du jeton d'accès vers OpenBao. Un rôle CEL
  d'OpenBao (`auth/<montage>/cel/roles`, absent de Vault) pourrait refuser un
  jeton qui porte `token` : essayé en tâche 1 (essai 2), retenu seulement s'il
  marche aussi simplement qu'un rôle ordinaire, jamais en mode A où le client
  peut avoir un Vault.
- Le jeton d'identité est gardé dans la `Session` (mémoire seulement, jamais
  écrit, absent de `public()`, de `repr` et des journaux), remplacé à chaque
  rafraîchissement si Rancher en rend un nouveau (essai 1 de la tâche 1 : non
  établi par la lecture de la source).
- **Groupes dans le jeton** : sur la branche principale de Rancher (PR
  rancher#55473, fusionnée le 18/06/2026, probablement en 2.15.0), `groups`
  n'est émis que si le client demande la portée `groups`, et cette portée doit
  figurer dans `spec.scopes` de l'`OIDCClient` (absente des portées par
  défaut). Avant ce changement (cas probable de notre Rancher 2.14.1),
  `groups` est toujours émis. Les valeurs sont les noms des principaux de
  groupe de la connexion qui a émis le jeton (par exemple
  `keycloakoidc_group://rancher-admins`). La console demande `groups` quand
  le document de découverte l'annonce (`scopes_supported`) et l'ajoute à
  `spec.scopes` quand elle enregistre elle-même le client (1.79.0).

### Méthode `jwt` d'une personne (une par Rancher)

- Un montage par Rancher dont la connexion unique est réglée (1.79.0 permet
  plusieurs Rancher, chacun son émetteur et son client) :
  `jwt-hops-<identifiant de la console>-<identifiant du Rancher>`
  (identifiant du Rancher : celui de `rancher.d/<id>`, 12 caractères au plus
  dans le nom de montage, tronqué avec 6 hexadécimaux d'empreinte au-delà).
- Configuration du montage :
  - `oidc_discovery_url` = émetteur de Rancher (`<url>/oidc`, sans
    `.well-known/openid-configuration`), `oidc_discovery_ca_pem` = CA de
    Rancher connue de la console (`ca_file` du Rancher), `bound_issuer` =
    émetteur ; **ou**, quand OpenBao ne joint pas Rancher (point 286),
    `jwt_validation_pubkeys` = clés publiques PEM tirées par la console du
    JWKS de Rancher (`<url>/oidc/.well-known/jwks.json`), avec leur empreinte
    gardée dans les réglages. OpenBao n'accepte qu'UNE source de clés par
    montage (`oidc_discovery_url`, `jwks_url` ou `jwt_validation_pubkeys`) et
    n'a pas `jwks_pairs` de Vault ; la liste `jwt_validation_pubkeys` peut en
    revanche porter l'ancienne et la nouvelle clé pendant une rotation ;
  - `jwt_supported_algs = ["RS256"]` (algorithme de Rancher ; un rôle `jwt`
    accepte sinon tous les algorithmes) ;
  - `default_role = "person"`.
- Rôle `person` :
  - `role_type = "jwt"`, `user_claim = "sub"`,
    `bound_audiences = ["<client_id>"]` ;
  - `claim_mappings = {"name": "name", "config_name": "provider"}`
    (correspondances simples, donc facultatives : un claim absent ne fait pas
    échouer la connexion, contrairement à une correspondance en pointeur
    JSON) ; métadonnées du jeton et de l'alias, visibles dans l'audit ;
    `groups_claim = "groups"` seulement si l'essai 3 montre ce claim ;
  - `token_policies = ["hops-person-<id>"]`, `token_type = "service"`,
    `token_ttl = "15m"`, `token_max_ttl = "15m"`, `token_num_uses = 0`,
    `token_no_default_policy = false` (la politique `default` donne
    `revoke-self` et `lookup-self`) ;
  - `token_bound_cidrs` = adresse de la console (127.0.0.1/32 en B1, la plage
    du rôle AppRole en A) : un jeton de personne n'est utilisable que depuis
    la console ;
  - `verbose_oidc_logging = false`.
- Un jeton obtenu par la méthode `jwt` est renouvelable jusqu'à
  `token_max_ttl` ; l'appartenance aux groupes externes est relue à la
  connexion et au renouvellement (documentation de l'identité d'OpenBao),
  jamais le JWT, qui n'est plus présenté. D'où le choix de reconnecter plutôt
  que de renouveler, quand Rancher le permet.
- Le jeton OpenBao d'une personne vit en mémoire de la console, dans un
  registre indexé par identifiant de session ; jamais remis à un outil, jamais
  écrit. Durée (point 283) : la console se reconnecte avec le nouveau jeton
  d'identité à chaque rafraîchissement Rancher (toutes les 7 à 8 minutes avec
  le client de 10 minutes), révoque l'ancien (`revoke-self`) ; à la
  déconnexion, à la fermeture de session ou au refus du rafraîchissement :
  `revoke-self` et purge des entrées de cache de cette entité.
- Pas de connexion tant que la personne ne lit pas de secret personnel : le
  jeton est obtenu à la première lecture, puis suivi par les rafraîchissements
  (un montage sollicité seulement par qui en a besoin).

### Politiques

- **La console n'écrit jamais une politique, un rôle ni un montage
  d'authentification.** Un droit d'écriture sur `sys/policies/acl/...`, sur un
  rôle d'une méthode d'authentification ou sur `sys/auth/...` permet de se
  fabriquer un jeton de n'importe quelle politique (escalade démontrée par
  l'essai 8 de la 2.9.0 pour les politiques ; même mécanisme pour un rôle,
  dont `token_policies` est libre). Tout passe par l'agent en B1 et par les
  commandes de l'assistant en mode A (point 287).
- `hops-person-<id>` (commune, texte de référence
  `openbao_policy.person_rules`) : `read` sur
  `<montage>/data/<préfixe>/ssh-cluster/*`, `.../ssh-kvm/*`,
  `.../tf-kvm/*` ; `create, update` sur `.../tf-kvm/*` et `.../ssh-cluster/*`
  (déclaration d'une variable Terraform, clé téléversée par la personne) ;
  `create, update` sur `.../eso/*`, sans `read` (une valeur ESO s'écrit, ne
  se relit pas depuis la console) ;
  `read` sur `<montage>/metadata/<préfixe>/{ssh-cluster,ssh-kvm,tf-kvm}/*`
  (versions) ; `update` sur `sys/capabilities-self`. Rien sur `kubeconfig`,
  `rancher-oidc`, `replication-*`, `plan-run`, transit, AppRole.
- Groupes (point 284, 285) : politiques `hops-group-<id>-<nom>` rendues par
  `openbao_policy.group_rules(nom, classes, clusters)` (lecture limitée à
  `<classe>/<cluster>` pour une liste de clusters) et rattachées à des groupes
  d'identité OpenBao :
  - **externes** (`type = "external"`, alias de groupe = nom du groupe dans le
    claim `groups` sur le montage de ce Rancher) si l'essai 3 montre ce
    claim ;
  - sinon **internes**, dont l'administrateur d'OpenBao tient les membres
    (entités) ; la console liste les entités qu'elle a vues (alias, `name`,
    dernière connexion) et rend les commandes `bao` d'ajout et de retrait.
  Avec des groupes, `hops-person-<id>` peut être réduite à `capabilities-self`
  par un réglage (« lecture seulement par groupe »), pour un client qui veut
  tout décider par groupe.
- La console garde ses propres contrôles en plus (rôle dans la console, droit
  Rancher sur le cluster) : OpenBao est une seconde barrière, pas la seule.

### Cache, copies et pannes (prolonge la décision 137)

- Lecture par une personne (point 288) : toujours une requête OpenBao avec son
  jeton (`kv_get`), qui juge et qui journalise. La valeur obtenue alimente le
  cache commun et la copie partagée de la 2.9.0 (même référence, même
  valeur), dont le chemin n'est jamais remis à la personne.
- Cache des personnes : entrée (entité, référence) gardée la durée du cache
  (15 min, réglable) pour servir la même personne quand OpenBao ne répond plus
  ; une personne différente n'est jamais servie depuis l'entrée d'une autre.
  Purge à la déconnexion.
- Jeton refusé à la connexion (`openbao-login-failed` sur la méthode jwt :
  audience, émetteur, clé inconnue) : refus de la lecture et bandeau
  « connexion personnelle à OpenBao refusée », sans repli.
- Mêmes codes qu'en 2.9.0, plus : `openbao-person-login-failed`,
  `openbao-person-no-token` (session Rancher sans jeton d'identité, par
  exemple ouverte avant l'activation), `openbao-jwt-keys-changed` (clé de
  signature de Rancher absente des clés statiques).

### Audit et attribution

- Chaque requête d'une personne est journalisée par OpenBao avec son entité
  (`auth.entity_id`), son alias (`sub`), son `display_name`
  (`<montage>-<sub>` chez Vault, forme d'OpenBao relevée à l'essai 2), ses
  métadonnées (`name`, `provider`, `role`) :
  l'attribution ne dépend plus de l'en-tête. `X-Hops-Action` et
  `X-Hops-Actor` restent posés (corrélation avec Activité).
- Activité marque chaque action qui a lu un secret : « jeton personnel » ou
  « service » (champ `secret_identity` de l'action, sans valeur).
- `custom_metadata` des écritures : inchangées (`hops_actor`), plus
  `hops_identity` = `person` ou `service`.
- Une entité par personne reste dans OpenBao après sa dernière connexion :
  elle ne porte aucune politique (seuls le rôle et les groupes en donnent) ;
  la retirer appartient à l'administrateur d'OpenBao (point 289).

### Mode A et mode B1

- **Mode A** : l'assistant rend les commandes `bao auth enable -path=...
  jwt`, `bao write auth/<montage>/config ...`, `bao write
  auth/<montage>/role/person ...`, `bao policy write hops-person-<id> -`
  (texte exact), et pour les groupes `bao write identity/group ...` et
  `identity/group-alias ...`. Vérifications avant activation : voir tâche 6.
  OpenBao doit joindre l'adresse de découverte de Rancher, sinon clés
  statiques (point 286).
- **B1** : l'agent pose tout par l'opération `b1-jwt`, sans redémarrer
  OpenBao. Sa politique (`openbao_policy.agent_rules`) gagne, par
  `b1-policies` lancée par `install.sh` à la mise à jour : `create, update,
  read, delete, sudo` sur `sys/auth/jwt-hops-<id>-*` (montages des
  personnes et des clusters enfants) ; `create, update, read, delete` sur
  `auth/jwt-hops-<id>-*` ; `create, update, read, delete` sur
  `sys/policies/acl/hops-person-<id>`, `sys/policies/acl/hops-group-<id>-*`,
  `sys/policies/acl/hops-eso-<id>-*` ; `create, update, read, delete, list`
  sur `identity/group/name/hops-<id>-*`, `identity/group/id/*` et
  `identity/group-alias/id/*`. Les chemins par identifiant ne se restreignent
  pas par nom : l'agent n'y vise que les identifiants des groupes
  `hops-<id>-*` qu'il vient de lire par nom, et en B1 seule la console crée
  des groupes (la revue de restauration refuse tout autre groupe). L'agent
  est déjà root sur l'hôte et détient la clé de descellement (même raison que
  la décision 271).
- **Revue d'un instantané restauré** (B1) : la liste blanche de la 2.9.0
  (« méthodes `token` et `approle` seulement, aucun groupe d'identité »)
  s'élargit aux montages `jwt-hops-<id>-*` et `jwt-hops-<id>-eso-*`, à leurs
  configurations et rôles exacts (rendus par `openbao_jwt`), aux politiques
  `hops-person-<id>`, `hops-group-<id>-*` (texte rendu par la console d'après
  ses réglages), `hops-eso-<id>-*`, aux groupes `hops-<id>-*` sans politique
  autre que `hops-group-<id>-*`, aux entités dont les alias sont sur ces
  montages et ne portent aucune politique. Toute autre différence refuse
  l'instantané, comme en 2.9.0.

### External Secrets Operator

- **Portée** (point 293) : clusters enfants RKE2 créés par la console
  (Cluster API sur Harvester), option `external_secrets` du cluster enfant
  (liste blanche de `capi_cluster.KNOWN`), administrateur, OpenBao actif
  seulement (`eso-needs-openbao`, bloquant). Jamais sur un cluster Harvester.
- **Livraison** (point 294) : chart officiel `external-secrets`
  (`https://charts.external-secrets.io`, Apache-2.0) dans un paquet
  d'artefacts (élément `chart`, comme `flux2` en 2.8.0), installé par CAAPH
  dans le namespace `external-secrets` du cluster enfant, nom de service
  `eso`, images tirées du registre du cluster enfant. Version courante au
  08/11/2026 : v2.14.0 (chart `helm-chart-2.14.0`, 23/10/2026), seule la
  dernière mineure est maintenue ; API `external-secrets.io/v1` seule servie
  (`v1beta1` à `served: false` depuis 0.17), `PushSecret` encore en
  `v1alpha1`. Une seule image pour le contrôleur, le webhook et le contrôleur
  de certificats (`ghcr.io/external-secrets/external-secrets`), sous-chart
  bitwarden éteint par défaut. La console ne fixe pas la version : le paquet
  porte celle qui convient, relevée et publiée dans le CHANGELOG. Les CRD
  viennent du chart (`installCRDs: true`, défaut) : jamais par un
  ClusterResourceSet (la règle des manifestes les refuse à juste titre).
  Elles dépassent 256 Kio : `kubectl apply` côté client échoue (annotation
  `last-applied-configuration`), Helm les crée sans cette annotation ;
  l'installation par CAAPH est vérifiée en tâche 1 (essai 7). Valeurs
  imposées par `child_eso.VALUES` (noms relevés dans le chart en tâche 1) :
  webhook et contrôleur de certificats actifs, `processClusterStore: false`
  (aucun magasin de portée cluster, point 296), `processPushSecret: false`,
  `bitwarden-sdk-server.enabled: false`, sans `ServiceMonitor` par défaut.
- **Fournisseur** : `vault` (stable, documenté par ESO pour OpenBao, essayé
  avec OpenBao 2.2.0). Le fournisseur `openBao` propre d'ESO est alpha,
  lecture seule et sans authentification `jwt` (projet de PR seulement) :
  pas employé en 2.10.0, à revoir quand il sera stable.
- **Magasin** (point 296) : un `SecretStore` `hops-openbao` dans chaque
  namespace activé, `provider.vault` : `server` = adresse d'OpenBao vue du
  cluster enfant, `path` = montage KV, `version: v2`, `caBundle` = CA
  d'OpenBao en ligne (rien à monter, rien de secret), `auth.jwt` avec
  `path` = montage `jwt` du cluster, `role: eso`,
  `kubernetesServiceAccountToken: {serviceAccountRef: {name: hops-eso},
  audiences: ["hops-openbao-<id>"], expirationSeconds: 600}` (défaut d'ESO :
  audience `vault`, 10 min). Un `ClusterSecretStore` exige un namespace dans
  sa référence de compte (documentation du fournisseur `vault`) : il ne
  donnerait qu'un compte pour tout le cluster, d'où un magasin par
  namespace. Un namespace système (`hv_containers.SYSTEM_NAMESPACES`,
  `*-system`) est refusé (`eso-system-namespace`).
- **Compte `hops-eso`** : posé par la console dans chaque namespace activé
  (bouton « Activer les secrets externes » d'un namespace d'équipe, étiquette
  `harvester-ops.io/eso: "on"` sur le namespace), sans aucun droit
  Kubernetes : ESO demande lui-même un jeton pour ce compte
  (`TokenRequest`, droit du contrôleur d'ESO).
- **Authentification dans OpenBao** (point 295) : un montage `jwt` par
  cluster enfant, `jwt-hops-<id>-eso-<clé>` (clé = 8 hexadécimaux de sha256
  de `<harvester>/<ns>/<c>`), `jwt_validation_pubkeys` = clés publiques du
  JWKS du cluster enfant (`/openid/v1/jwks`, lu par la console avec le
  kubeconfig du cluster enfant, `kubectl get --raw`, converti en PEM),
  `bound_issuer` = émetteur des comptes de service du cluster enfant (RKE2 :
  `https://kubernetes.default.svc.cluster.local` par défaut, le même pour
  tous les clusters, d'où un montage par cluster qui isole ses clés).
  RKE2 coupe l'accès anonyme (`--anonymous-auth=false`) et la liaison
  `system:service-account-issuer-discovery` ne vise que
  `system:serviceaccounts` : OpenBao ne pourrait pas lire ce JWKS sans
  identifiant, et la découverte par l'émetteur est exclue. OpenBao n'a pas à
  joindre l'API du cluster enfant, aucun jeton de relecteur n'est gardé.
  Limite connue : un jeton de compte de service révoqué dans Kubernetes reste
  accepté par OpenBao jusqu'à son expiration (10 min). Rôle `eso` : `role_type =
  "jwt"`, `user_claim = "sub"`, `bound_audiences = ["hops-openbao-<id>"]`,
  `bound_claims = {"/kubernetes.io/serviceaccount/name": "hops-eso"}`,
  `claim_mappings = {"/kubernetes.io/namespace": "namespace"}` (pointeur
  JSON, donc obligatoire : un jeton sans ce claim est refusé),
  `token_policies = ["hops-eso-<id>-<clé>"]`, `token_ttl = "10m"`,
  `token_max_ttl = "10m"`, `token_bound_cidrs` = plage du réseau des nœuds du
  cluster enfant quand elle est connue.
- **Politique ESO d'un cluster** (une seule, à gabarit) :
  `path "<montage>/data/<préfixe>/eso/<clé>/{{identity.entity.aliases.<accesseur>.metadata.namespace}}/*"
  { capabilities = ["read"] }` et la même ligne en `metadata` (`read, list`).
  Un namespace ne lit que son chemin ; aucun accès aux classes de la console.
- **Espace des secrets ESO** (point 297) : classe `eso`,
  `eso/<clé>/<namespace>/<nom>`, sous le préfixe de la console. Jamais lue par
  une action de la console ; écrite par une personne (jeton personnel,
  administrateur ou opérateur ayant le droit d'administrer le cluster enfant)
  depuis l'onglet « Secrets externes » (champs masqués, jamais réaffichés), ou
  par les outils du client directement dans OpenBao.
- **Joignabilité d'OpenBao depuis le cluster enfant** : sonde `vm_probe`
  (pod du réseau des VMs, `GET sys/health` en TLS avec la CA) avant de poser
  le magasin ; échec : `eso-openbao-unreachable`, bloquant. En B1, OpenBao
  n'écoute que sur 127.0.0.1 (point 254 de la 2.9.0) : un second écouteur est
  nécessaire (point 298).
- **Objets livrés** : par namespace activé, `SecretStore` et compte
  `hops-eso`, par le ClusterResourceSet `art-eso` (étiquettes de la console,
  retrait par `capi_manifests.removed`), appliqué après que le chart a posé
  les CRD (`HelmReleaseProxy` prêt). `capi_manifests` connaît les sortes
  `external-secrets.io` : `ExternalSecret`, `SecretStore` (namespacées),
  `PushSecret` (refusée) et `ClusterSecretStore`, `ClusterExternalSecret`,
  `ClusterPushSecret` (portée cluster, refusées depuis un paquet : un paquet
  ne crée pas de magasin qui traverse les namespaces).
- **Objets `ExternalSecret`** (point 299) : ceux des équipes (GitOps de la
  2.8.0, ou leurs outils). La console montre leur état (`Ready`,
  `SecretSynced`, dernière synchronisation, sans valeur) et rend un exemple.
  Le rôle `edit` agrégé par le chart permet à une équipe d'en créer dans son
  namespace (relevé en tâche 1).
- **Retrait** (point 300) : la console retire d'abord les `ownerReferences`
  qui lient les Secrets synchronisés à leurs `ExternalSecret`
  (`creationPolicy: Owner` les ferait supprimer par le ramasse-miettes avec
  les CRD), puis le ClusterResourceSet, puis le chart, puis le montage, le
  rôle et la politique dans OpenBao (agent en B1, commandes en A). Les
  Secrets restent, figés à leur dernière valeur, et l'interface le dit.
- **Clés du cluster enfant** : empreinte du JWKS gardée dans les réglages ;
  relue à chaque passage de la tâche de fond des clusters enfants (1 h) ;
  changement : `eso-keys-changed` et action « Mettre à jour les clés » (agent
  ou commande). Une clé de compte de service RKE2 ne tourne pas seule
  (`/var/lib/rancher/rke2/server/tls/service.key`), mais un renouvellement des
  machines peut la changer (essai 7).

### Réglages (`secrets-settings.json`, à chaud)

- `person_token` : `{"enabled": false, "servers": {"<rancher id>":
  {"mount", "client_id", "issuer", "keys": "discovery" | "static",
  "keys_sha256", "configured_at", "configured_by"}}, "groups_only": false}`.
- `eso` : `{"listener": null | "<adresse>:<port>", "clusters": {"<clé>":
  {"harvester", "namespace", "cluster", "mount", "issuer", "keys_sha256",
  "at", "by"}}}`.
- Défaut après la mise à jour : `enabled: false` ; aucun comportement ne change
  tant que l'administrateur n'a pas activé (point 290).

### Communes

- Chaque opération mutative crée un `ActionRun` (configurer la méthode d'un
  Rancher, activer, désactiver, groupes, poser ESO sur un cluster, mettre à
  jour ses clés, retirer, écrire un secret ESO). Routes `@requires_auth`,
  `@_rate_limit(...)` avec des noms de limite valides, administrateur pour
  la configuration ; écriture d'un secret ESO : opérateur avec le droit
  d'administrer le cluster enfant (lecture du Secret `<c>-kubeconfig`, règle
  de la 2.7.0).
- Parité CLI et interface : configuration par `bin/harvester-secrets.py`
  (`_cli_action`) ; les lectures de secrets restent dans le processus.
- Cinq langues, info-bulles, `escapeHtml`, `SSEReconnect.connect()`.
- Jamais de jeton d'identité, de jeton OpenBao de personne ni de valeur ESO
  dans `actions.db`, les étapes, les journaux, les réponses d'API ; test de
  recherche de chaînes connues (jeton d'identité de test, jeton OpenBao de
  test, valeur témoin) dans tous ces endroits.

## Constats préalables (lus dans le code le 08/11/2026)

- **Les lectures ne portent pas la personne.** `_secret_actor()` n'est passé
  qu'aux écritures (`_secret_put`, `_secret_forget`, déplacement d'un
  kubeconfig, 13462) ; les lectures (`_cluster_kubeconfig_path` 9328,
  `_cluster_ssh_key_path`, `kvm_decl` 88, `cluster_decl.materialize` 116,
  `node_images` 30, `_tf_kvm_*`) appellent `path_of` sans `actor` : leurs
  requêtes partent en `X-Hops-Actor: service`. Le paramètre `actor` de la
  2.9.0 existe jusqu'au client (`with_headers`) mais n'est pas encore tiré
  jusqu'aux lecteurs.
- **Une seule identité par processus.** `default_client_factory` ouvre une
  connexion AppRole par processus et rend une copie par action pour les
  en-têtes ; `OpenBaoStore._client(actor, action)` ne choisit pas de jeton.
- **Cache et copies indifférents à la personne.** `OpenBaoStore._cache` est
  indexé par référence, `_shared` donne une copie tmpfs par référence à tout
  lecteur ; seules les copies d'identité (`_identity_kubeconfig`) sont « une
  par cluster et par personne ». Avec des jetons par personne, servir le cache
  avant le jugement d'OpenBao annulerait la décision 138.
- **Le jeton d'identité est vérifié puis jeté.** `sso_callback` (3319) exige
  `id_token`, le vérifie (`check_id_token` : émetteur, audience = client,
  `exp`, nonce, `sub`) puis ne garde dans la `Session` que le jeton d'accès
  et le jeton de rafraîchissement ; `refresh` ne lit pas `id_token`. Le
  `sub` est aussi vérifié égal à l'identifiant Rancher (`identity-mismatch`).
- **Le jeton d'accès est un jeton Rancher.** Vu en 1.50.0 : il porte
  `token: token-...` et marche en Bearer sur l'API de Rancher et sur
  `/k8s/clusters/<id>`. Il ne doit jamais sortir de la console (point 280).
- **Sous une session Rancher, le kubeconfig de la console ne sert qu'à la
  découverte.** `_kubectl_for_cluster` (9308) rend le kubeconfig de session
  (mandataire Rancher, jeton de la personne) ; le kubeconfig rangé n'est lu
  que pour l'UID de `kube-system` et des nœuds (`_kube_system_uid`). Un
  cluster que Rancher ne montre pas est refusé (`cluster-not-in-rancher`).
  L'alimentation est refusée aux sessions Rancher (`power-needs-local-account`,
  13963). D'où `kubeconfig` en classe de service.
- **Hôtes KVM et nœuds bare-metal hors de Rancher.** `_sso_cluster_guard` ne
  regarde que `view_args["cluster"]` : un geste sur un hôte KVM (`host`) ou
  sur un nœud par SSH lit la clé de la console après le contrôle du rôle dans
  la console. Ce sont les lectures que le jeton personnel couvre
  (`ssh-kvm`, `ssh-cluster`, `tf-kvm`).
- **Plusieurs Rancher** (1.79.0) : `rancher.d/<id>`, chacun son émetteur, son
  client OIDC et sa CA ; une session porte `server_id`. Une méthode `jwt` a
  un seul émetteur : un montage par Rancher.
- **Connexion directe sans jeton OIDC** (1.79.0) : `kind = "direct"`, jeton
  Rancher ordinaire, aucun `id_token`.
- **Pas de groupes dans le jeton d'identité, à revérifier.** Vu en 1.50.0 avec
  l'administrateur local (`sub`, `auth_provider`, `name`, pas de `groups`) ;
  les groupes viennent de `/v3/principals` (`rancher_identity`). Un compte
  local n'a pas de principal de groupe, et la source de Rancher émet
  `groups` (toujours avant la PR rancher#55473, sur la portée `groups`
  après) : l'absence vient très probablement du compte. La console ne
  demande aujourd'hui que `openid profile offline_access` (`authorize_url`).
  L'essai 3 le tranche avec un compte Keycloak membre d'un groupe.
- **La revue d'un instantané B1 refuse tout autre montage d'authentification
  et tout groupe d'identité** (`review_restored`, 1464 : `auths != {"token/",
  "approle/"}`) : la 2.10.0 doit l'élargir, et une console 2.9.x refuserait un
  instantané pris après l'activation (point 291).
- **L'agent peut déjà réécrire sa propre politique** (`b1-policies`,
  décision 276) : l'élargissement passe par la mise à jour, sans geste à la
  main.
- **Les manifestes refusent les CRD et les webhooks**
  (`capi_manifests.REFUSED_KINDS`) et une sorte inconnue bloque
  (`NAMESPACED_KINDS`, `CLUSTER_SCOPED_KINDS`) : ESO passe par un chart, ses
  objets par une table de sortes étendue.
- **Le chart `flux2` est le modèle** (`child_gitops.VALUES`, CAAPH, service
  `flux`, version portée par le paquet). La sonde depuis le réseau des VMs
  existe (`vm_probe`, 2.6.1).
- **B1 n'écoute que sur 127.0.0.1:8210** (`openbao_policy.B1_LISTEN`), la
  console est en `--network host` ; le point 254 de la 2.9.0 réservait
  l'écoute réseau à ESO.

## Tâche 1 : essais préalables sur les bancs (avant tout code)

Fichiers : aucun dans le dépôt ; compte rendu ajouté en fin de ce plan
(section « Essais préalables (tâche 1) »), dans un commit à part avant la
tâche 2.

Bancs : OpenBao de la collection en rootless sur node1 (recette de la 2.9.0,
`127.0.0.1:8210`, mémoire du projet `openbao-290.md`) ; Rancher Manager
(`rancher-manager`, v2.14.1, client OIDC `harvester-ops-dev` existant) ;
fournisseur Keycloak de Rancher (VM `idp`) si ju l'accorde (point 279) ;
harvlab2 pour un cluster enfant Cluster API ; Vault 1.14.2 jetable.
Garde-fous de la 2.9.0 (port 8200 refusé, `VAULT_*` et `BAO_*` vidés). Chaque
essai détruit ce qu'il crée.

1. **Jetons du fournisseur OIDC de Rancher** : contenu exact de l'`id_token`
   et du jeton d'accès (`iss`, `aud`, `sub`, `exp`, `kid`, autres claims) ;
   durée de l'`id_token` face à `tokenExpirationSeconds` ; le
   rafraîchissement rend-il un nouvel `id_token` ? `scopes_supported` du
   document de découverte (portée `groups` annoncée ?) ; document de découverte et
   `jwks_uri`, accès anonyme au JWKS, nombre de clés, rotation (où vit la clé
   de signature, ce qui la change).
2. **Méthode `jwt` d'OpenBao 2.7.2 contre Rancher** : `oidc_discovery_url` +
   CA ; connexion avec l'`id_token` ; refus avec le jeton d'accès si
   l'audience diffère (sinon noter qu'OpenBao l'accepterait) ; jeton expiré,
   mauvaise audience, autre émetteur ; `jwt_validation_pubkeys` puis clé
   retirée (message exact) ; `token_bound_cidrs` ; ce que l'audit écrit
   (`display_name`, `entity_id`, métadonnées, alias) ; une entité par `sub`,
   réutilisée à la connexion suivante.
3. **Groupes** : compte Keycloak membre d'un groupe : claim `groups` présent
   ou non, forme (nom, `keycloakoidc_group://...`) ; groupe externe et alias
   ; groupe interne et membre ajouté à la main ; politique de groupe
   effective (`capabilities-self`).
4. **Durée et révocation** : `revoke-self` d'un jeton de personne ; jeton
   utilisé après `token_ttl` ; reconnexion à chaque rafraîchissement
   (charge : nombre d'entités et d'alias après dix reconnexions).
5. **Gabarits** : politique `hops-eso-<id>-<clé>` à gabarit
   `{{identity.entity.aliases.<accesseur>.metadata.namespace}}` alimenté par
   `claim_mappings` en pointeur JSON (`/kubernetes.io/namespace`) ;
   `bound_claims` en pointeur JSON.
6. **B1 en service** : agent de banc, `b1-policies` qui élargit la politique de
   l'agent, puis montage `jwt`, rôle, politiques et groupe posés sans
   redémarrer OpenBao ; instantané puis revue de restauration élargie ;
   instantané pris après l'activation relu par la revue de la 2.9.0 (refus
   attendu, cas du point 291).
7. **ESO sur un cluster enfant de harvlab2** : chart v2.14.0 (ou la dernière
   mineure du jour) : noms exacts des valeurs imposées, images (liste pour le
   registre), installation par CAAPH (CRD de plus de 256 Kio, webhook,
   contrôleur de certificats) ; émetteur et JWKS des comptes de service du
   cluster enfant lus par `kubectl get --raw` avec le kubeconfig de Cluster
   API (accès anonyme refusé, à constater) ; `SecretStore` en fournisseur
   `vault` vers l'OpenBao de banc (écouteur de banc sur l'adresse de node1,
   port 8212, pare-feu ouvert le temps de l'essai) ; authentification `jwt`
   avec le jeton du compte `hops-eso` et l'audience `hops-openbao-<id>` ;
   isolation par la politique à gabarit : le namespace A ne lit pas le chemin
   du namespace B, même avec un `SecretStore` écrit à la main dans A qui vise
   le rôle ; un `ClusterSecretStore` sans namespace dans sa référence de
   compte (refus attendu, confirme le point 296) ; rotation suivie
   (`refreshInterval`) ; rôle `edit` agrégé par le chart (une équipe crée un
   `ExternalSecret`, et un `SecretStore` ?) ; retrait des CRD avec
   `creationPolicy: Owner` (Secrets supprimés par le ramasse-miettes ?) puis
   avec les `ownerReferences` retirées d'abord ; renouvellement des machines
   du cluster enfant (2.8.0) : JWKS inchangé ?
8. **Vault 1.14.2** (mode A) : essais 2, 4 et 5 ; écarts.

Critère de passage : 1, 2, 4 et 6 réussis. Un `id_token` non renouvelé par le
rafraîchissement change la durée du jeton (point 283, option B). Un échec de 3
fait retenir les groupes internes (point 285). Un échec de 5 (gabarit
alimenté par un pointeur JSON) fait retenir un rôle et une politique par
namespace (point 296, option B). Un échec de l'installation d'ESO (7) détache
les tâches 8 à 10 en 2.10.1 (point 301).

## Tâche 2 : client OpenBao, connexion jwt

Fichiers : `bin/lib/openbao_client.py` ;
`tests/api/test_openbao_client_290.py` (cas ajoutés) ; nouveau
`tests/api/test_openbao_client_jwt_2100.py` (faux serveur) ;
`tests/api/test_openbao_client_live_290.py` (cas `--live` jwt).

Interfaces :

```
Client.login_jwt(mount, role, jwt) -> dict          # {entity_id, ttl, policies, accessor}
Client.lookup_self() -> dict                        # entité, alias, politiques, expiration
Client.for_token(token, ttl, kind="person") -> Client   # copie qui porte un autre jeton
OpenBaoError("openbao-person-login-failed", ...)    # 400/403 de auth/<montage>/login
```

Tests (tableau, un cas par chemin d'erreur) :

| Cas | Attendu |
|---|---|
| connexion réussie | jeton gardé dans l'objet, jamais dans `repr`, `pickle`, une exception |
| audience refusée, émetteur refusé, jeton expiré, clé inconnue | `openbao-person-login-failed`, message d'OpenBao filtré (le JWT n'y figure jamais) |
| JWT présent dans le corps d'une erreur | absent du message (filtre étendu aux motifs `eyJ...` de plus de 20 caractères) |
| `for_token` | n'altère pas le jeton du client de service |
| jeton de personne expiré pendant une lecture | aucune reconnexion AppRole (le client de personne ne sait pas se reconnecter), code rendu à l'appelant |
| `revoke_self` d'un jeton de personne | requête faite, jeton oublié même si OpenBao répond 403 |

Mutations : « reconnexion AppRole sur un client de personne », « JWT consigné
dans l'exception », « jeton de personne copié dans le client de service ».

## Tâche 3 : identité des lectures (`secret_store`, `secret_classes`, `openbao_jwt`)

Fichiers : `bin/lib/secret_store.py`, `bin/lib/secret_classes.py` ; nouveau
`bin/lib/openbao_jwt.py` ; nouveaux `tests/api/test_secret_identity_2100.py`,
`tests/api/test_openbao_jwt_2100.py` ; `tests/api/test_secret_classes_290.py`
(colonne `person`).

Interfaces :

```
secret_store.Identity(kind, actor, session_id=None, entity=None)   # kind: service | person
secret_store.SERVICE                                  # identité de service
secret_store.PersonTokens()                           # registre d'un processus
  .get(session_id) -> Client | None ; .set(session_id, client, entity)
  .drop(session_id)                                   # revoke-self + purge du cache de l'entité
Store.get(ref, identity=None) ; Store.file(ref, purpose, identity=None)
Store.put(cls, path, field, value, identity=None, action=None)
secret_classes.CLASS_TABLE[cls].person -> bool        # ssh-cluster, ssh-kvm, tf-kvm, eso
openbao_jwt.identity_for(cls, session, settings) -> Identity     # table « Qui agit »
openbao_jwt.person_mount(console_id, rancher_id) -> str
openbao_jwt.person_role_params(client_id, console_cidr, groups_claim=None) -> dict
openbao_jwt.mount_config(issuer, ca_pem=None, pubkeys=None) -> dict
openbao_jwt.keys_sha256(jwks) -> str
```

- `actor` (2.9.0) reste accepté : une chaîne vaut `Identity("service",
  actor)`, pour ne rien casser des appelants de la 2.9.0.
- Lecture d'une identité de personne : `kv_get` avec le jeton de la personne,
  toujours ; entrée de cache (entité, référence) ; puis copie partagée comme
  en 2.9.0. Refus : remonté, aucune lecture de service.
- Identité de personne sans jeton OpenBao : la console se connecte avec
  l'`id_token` de la session (fourni par un rappel posé par `web/app.py`, le
  magasin ne connaît pas Rancher), puis garde le client dans `PersonTokens`.

Tests :

| Cas | Attendu |
|---|---|
| classe personnelle, session SSO avec jeton d'identité | requête avec le jeton de la personne, en-tête = personne |
| classe de service, même session | requête AppRole, en-tête = personne |
| compte local, connexion directe, réglage éteint | AppRole |
| personne refusée par OpenBao | `openbao-denied`, AUCUNE requête AppRole ensuite |
| personne A a lu, personne B lit pendant une panne | B refusée (`openbao-unreachable`), A servie depuis le cache |
| deux personnes, même référence, OpenBao en service | deux requêtes, une seule copie partagée |
| `drop` | `revoke-self` envoyé, entrées de l'entité retirées |
| `identity_for` sur chaque classe et chaque sorte de session | la table « Qui agit » ligne pour ligne |
| jeton de personne absent de `repr`, `pickle`, des exceptions et des copies | vrai |

Mutations : « cache servi avant le jugement d'OpenBao », « repli AppRole
après un refus », « entrée de cache partagée entre entités ».

## Tâche 4 : sessions Rancher et rappels

Fichiers : `web/rancher_sso.py` ; `web/app.py` (`sso_callback`,
`_sso_start_renewer`, déconnexion, fermeture) ;
`tests/api/test_rancher_sso_*.py` (cas ajoutés) ; nouveau
`tests/api/test_sso_person_token_2100.py`.

- Portée `groups` ajoutée à la demande d'autorisation quand le document de
  découverte la cite (`scopes_supported`) ; ajoutée à `spec.scopes` de
  l'`OIDCClient` quand la console l'enregistre (`api_rancher_sso_register`),
  et signalée à l'administrateur pour un client enregistré à la main.
- `Session.id_token` (mémoire), posé par `sso_callback` après
  `check_id_token` ; `Sessions.renew` garde le nouvel `id_token` du
  rafraîchissement après la même vérification (émetteur, audience, `sub`
  inchangé, sans nonce) ; jeton d'identité refusé : session close.
- Rappels : `on_renewed(session)` reconnecte le jeton OpenBao de la personne
  s'il existe (nouvel `id_token`) et révoque l'ancien ; `on_closed(session)`
  appelle `PersonTokens.drop`. Le fil `rancher-token-renewer` les appelle
  aussi.
- Sessions ouvertes avant l'activation, ou sans `id_token` :
  `openbao-person-no-token` pour une classe personnelle, avec « reconnectez-
  vous par Rancher » (pas de repli).
- Tests : `id_token` jamais dans `public()`, les journaux, `repr` ;
  rafraîchissement sans `id_token` (selon l'essai 1) ; `sub` différent au
  rafraîchissement = session close ; déconnexion = révocation.

Mutations : « `id_token` dans `public()` », « `sub` non revérifié au
rafraîchissement ».

## Tâche 5 : identité figée au déclenchement dans les actions

Fichiers : `web/app.py` (`_cluster_ssh_key_path`, lecteurs `ssh-kvm` des
routes KVM, `_tf_kvm_secret`, `_tf_kvm_env`, `_tf_kvm_stage`,
`_spawn_secret_env`, `ActionRun` : `secret_identity`) ; `bin/lib/kvm_decl.py`,
`bin/lib/cluster_decl.py`, `bin/lib/node_images.py` (paramètre
d'identité transmis, service par défaut) ; nouveaux
`tests/api/test_secret_identity_routes_2100.py`,
`tests/api/test_no_late_person_read_2100.py` (test de source).

- Les secrets d'une action sont lus avec l'identité de la requête au
  déclenchement (comme `identity_env`, v1.32.0), remis en copies tenues par
  le bail de l'action ; le fil de travail ne crée jamais de client de
  personne (`PersonTokens` refuse hors contexte de requête).
- Lecture tardive inévitable (secret dont l'action découvre le besoin en
  route) : service, marquée `late-service` dans l'étape et dans
  `secret_identity`.
- Test de source : aucun appel au magasin d'une classe personnelle dans les
  fonctions de fil (`_run_*`, `_worker_*`, liste nommée) sans identité
  passée.

Mutations : « lecture de personne dans le fil », « identité relue dans le
fil (None) puis service sans marque ».

## Tâche 6 : politiques, assistant, agent (`openbao_policy`, agent, CLI)

Fichiers : `bin/lib/openbao_policy.py` ; `bin/harvester-secrets-agent.py`
(opérations `b1-jwt`, `b1-jwt-remove`, `b1-groups`, `review_restored`) ;
`bin/harvester-secrets.py` (`person-token assistant|check|enable|disable|
groups`) ; `install.sh` (`b1-policies` inchangé, nouvelle politique) ;
`tests/api/test_openbao_policy_290.py` (cas ajoutés) ; nouveaux
`tests/api/test_secrets_person_token_2100.py`,
`tests/api/test_secrets_agent_jwt_2100.py` ; fixtures de texte exact.

Interfaces :

```
person_rules(mount, prefix, cid) / person_policy(...)
group_rules(mount, prefix, cid, name, classes, clusters) / group_policy(...)
eso_rules(mount, prefix, cid, key, accessor) / eso_policy(...)
agent_rules(cid, mount)                     # + sys/auth, auth/jwt-hops-*, politiques, groupes
person_token_commands(addr, cid, rancher, client_id, issuer, ca_pem, console_cidr, keys) -> [str]
group_commands(cid, name, mount_accessor, external, members) -> [str]
```

Vérifications avant activation (`person-token check`), chacune avec résultat
et raison :

La politique de la console n'est pas élargie pour ces vérifications (en B1,
l'agent ne réécrit jamais la politique de la console, décision 276) : tout
se vérifie par une connexion réelle.

1. document de découverte de Rancher relu par la console : émetteur égal au
   réglage, `jwks_uri` présent, `RS256` annoncé ;
2. **connexion réelle avec le jeton d'identité de l'administrateur qui lance
   la vérification** (il doit être connecté par Rancher) : prouve le montage,
   l'émetteur, l'audience et la source de clés ; un échec rend le message
   d'OpenBao filtré ;
3. `lookup-self` du jeton obtenu : politiques exactement `default` et
   `hops-person-<id>` (plus les politiques de groupe de l'administrateur,
   listées), `bound_cidrs` égal à l'adresse de la console, durée de 15 min ;
4. `capabilities-self` : lecture permise sur les trois classes
   personnelles, écriture sans lecture sur `eso/`, refus sur `kubeconfig/`,
   `rancher-oidc/`, `plan-run/` et sur le préfixe d'une autre console ;
5. `revoke-self`, puis le même jeton refusé.

Rien n'est activé tant qu'une vérification est rouge. Activation et
désactivation : réglage `person_token.enabled`, relu à chaud (1 s).

Tests : textes exacts ; aucune capacité hors du préfixe ; commandes sans
valeur ; agent : montage posé sans redémarrage (requêtes du faux serveur),
refus d'un nom de montage hors motif, revue de restauration (montage `jwt`
attendu accepté, montage `userpass` refusé, politique de groupe élargie
refusée, entité avec politique refusée, groupe hors préfixe refusé).
Mutations : « politique `hops-person` avec `kubeconfig/*` », « revue qui
accepte toute méthode `jwt` », « vérification 2 sautée ».

## Tâche 7 : rotation des clés de Rancher et clés statiques

Fichiers : `bin/lib/openbao_jwt.py` (`keys_sha256`) ; `web/app.py` (tâche de
fond horaire : JWKS de chaque Rancher configuré en `static`) ;
`bin/harvester-secrets.py` (`person-token keys --refresh`) ; agent
(`b1-jwt` en mise à jour des clés) ; tests ajoutés aux fichiers de la
tâche 6.

- Côté Rancher, les clés vivent dans le Secret `cattle-system/oidc-signing-key`
  : une seule clé privée (`.pem`) signe, mais le JWKS publie toutes les clés
  publiques (`.pub`) du Secret ; supprimer le Secret régénère la clé au
  redémarrage et invalide tous les jetons émis (documentation du fournisseur
  OIDC de Rancher). Une rotation propre ajoute donc la nouvelle clé publique
  avant de changer la clé privée : la console voit l'empreinte changer dès
  l'ajout.
- Mode `discovery` : rien à faire (OpenBao relit le JWKS).
- Mode `static` : empreinte comparée chaque heure ; changement :
  `openbao-jwt-keys-changed` au bandeau administrateur et action
  « Mettre à jour les clés » (agent en B1, commande rendue en A). Les clés
  ancienne et nouvelle sont posées ensemble jusqu'à la confirmation, pour
  ne couper aucune session.

## Tâche 8 : ESO, objets et chart (`child_eso`, `capi_manifests`)

Fichiers : nouveau `bin/lib/child_eso.py` ; `bin/lib/capi_manifests.py`
(sortes ESO) ; `bin/lib/capi_cluster.py` (option `external_secrets`) ;
nouveaux `tests/api/test_child_eso_2100.py`,
`tests/api/test_capi_manifests_eso_2100.py`.

Interfaces :

```
child_eso.VALUES ; chart_values(user_values) -> dict      # imposées par-dessus
child_eso.cluster_key(harvester, ns, cluster) -> str      # 8 hex
child_eso.namespace_objects(namespace, addr, kv_mount, jwt_mount, audience, ca_pem) -> [obj]
                                                          # SecretStore hops-openbao + ServiceAccount hops-eso
child_eso.status(externalsecrets) -> [{ns, name, ready, synced_at, reason}]   # sans valeur
child_eso.orphan_patches(secrets, externalsecrets) -> [patch]    # retrait des ownerReferences
```

Tests (tableaux) : magasin rendu exact (fixture) ; namespace système refusé
(`eso-system-namespace`) ; `PushSecret` refusé ; `ClusterExternalSecret`
refusé depuis un paquet ; valeurs imposées non retirables par le paquet ;
`orphan_patches` ne touche que les Secrets dont le propriétaire est un
`ExternalSecret` ; état sans aucune valeur (fixture qui en contient une).
Mutations : « namespace système admis », « `ownerReferences` d'un autre
propriétaire retirées ».

## Tâche 9 : ESO, authentification dans OpenBao et écouteur de B1

Fichiers : `bin/harvester-secrets.py` (`eso assistant|add|keys|remove|
status`) ; agent (`b1-eso-cluster`, `b1-eso-cluster-remove`,
`b1-eso-listener`) ; `bin/lib/openbao_policy.py` (`b1_config` : second
écouteur facultatif) ; `config/openbao/openbao.hcl.tpl` ; nouveaux
`tests/api/test_secrets_eso_2100.py`, `tests/api/test_secrets_agent_eso_2100.py`.

- `eso add --cluster H/NS/C` : lit le JWKS et l'émetteur du cluster enfant
  (kubeconfig par `child_access.session`, identité de la personne en
  interface, administrateur), demande à l'agent (B1) ou rend les commandes
  (A) : montage, configuration, rôle, politique ; relit le montage.
- Écouteur de B1 pour ESO (point 298) : `listener "tcp"` supplémentaire sur
  `<adresse>:8212` (réglage `eso.listener`), même certificat signé par la CA
  de B1 avec l'adresse en `subjectAltName` (certificat refait par l'agent),
  redémarrage d'OpenBao (quelques secondes, le cache de 15 min couvre la
  console) ; le rôle AppRole de la console reste lié à 127.0.0.1/32 et le
  rôle de personne à l'adresse de la console : l'écouteur n'ouvre que les
  rôles ESO. Le pare-feu de l'hôte n'est pas touché par la console : le port
  à ouvrir est affiché, la sonde dit s'il l'est.
- Tests : commandes et requêtes exactes ; refus d'une adresse non locale à
  l'hôte ; écouteur retiré au dernier cluster retiré ; montage d'un cluster
  supprimé retiré.

## Tâche 10 : ESO dans les clusters enfants, plans et interface

Fichiers : `web/app.py` (routes `/api/capi/<ns>/<c>/eso`,
`/api/capi/<ns>/<c>/eso/namespaces/<n>`, `/api/capi/<ns>/<c>/eso/secrets`
(écriture d'une valeur), état) ; `bin/lib/deploy_plans.py` (option
`external_secrets` dans un modèle de cluster d'un plan, après la phase des
artefacts) ; `bin/lib/deploy_undo.py` (Défaire retire ESO selon le point
300) ; `web/static/js/capi-*.js` (option dans la création et l'édition,
onglet « Secrets externes » : état du magasin, namespaces activés,
ExternalSecret et leur état, écriture d'une valeur, exemple à copier) ;
`web/static/js/secrets.js` (carte « External Secrets » : écouteur, clusters,
clés) ; `web/static/js/i18n.js` ; e2e nouveaux
`tests/e2e/test_child_eso_2100.py`, `tests/e2e/test_settings_person_token_2100.py`.

- Ordre de pose : sonde (`eso-openbao-unreachable`), chart par CAAPH
  (attente du `HelmReleaseProxy`), authentification dans OpenBao, objets du
  magasin, condition `Ready` de chaque `SecretStore`.
- Écriture d'une valeur : jeton de la personne (classe `eso`, politique de
  groupe ou commune selon le réglage), champ masqué, jamais réaffiché,
  action suivie.

## Tâche 11 : interface du jeton personnel et Activité

Fichiers : `web/static/js/secrets.js` (carte « Jetons personnels » : par
Rancher, état, mode des clés, assistant à onglets Serveur, Vérifications,
Confirmation ; groupes : entités vues, commandes) ; `web/static/js/activity.js`
(marque « jeton personnel », « service », « service tardif ») ; bandeau
(`openbao-person-login-failed`, `openbao-jwt-keys-changed`) ;
`web/templates/index.html` ; `web/static/js/i18n.js` ; mises à jour de
`tests/api/test_nav_inventory_200.py` et des listes d'onglets si une
sous-section s'ajoute.

Règles : boutons qui écrivent grisés tant que les vérifications ne sont pas
vertes ; la vérification 3 dit clairement « connectez-vous par Rancher pour la
lancer » à un compte local ; info-bulles ; `escapeHtml`. Tests e2e sur
routes simulées : activation bloquée, commandes sans valeur, marque dans
Activité, noms hostiles échappés. Mutations : « Activer actif avec une
vérification rouge », « échappement retiré ».

## Tâche 12 : retour arrière et revue de restauration

Fichiers : `bin/harvester-ops-update.py`, `bin/lib/self_update.py`
(garde) ; `bin/harvester-secrets.py` (`person-token disable --remove`,
`eso remove --all`) ; `docs/en/secrets.md` et `docs/fr/secrets.md` (section
retour) ; tests ajoutés à `tests/api/test_self_update_*.py`.

- Désactiver le jeton personnel : réglage, à chaud ; les jetons de personne
  en mémoire sont révoqués ; tout repasse en service.
- Retour sous 2.10.0 (point 291) : refusé tant que B1 porte un montage
  `jwt-hops-*` ou un écouteur ESO (la revue de restauration de la 2.9.x
  refuserait tout instantané), avec les étapes : `person-token disable
  --remove`, `eso remove --all`. En mode A, permis avec un avertissement (la
  configuration est chez le client et ne gêne pas une 2.9.x).

## Tâche 13 : essais réels, documentation, version

Fichiers : `VERSION` (2.10.0), `CHANGELOG.md` (version du chart ESO et de son
API, taille de l'élément de paquet), `docs/en/secrets.md` et
`docs/fr/secrets.md` (jeton personnel, groupes, ESO, retour arrière),
`docs/en/rancher-sso.md` et pendant français (jeton d'identité gardé, si la
page existe ; sinon section de la page de connexion), `docs/en/capi.md` et
pendant (option ESO, onglet), `docs/en/architecture.md`,
`docs/en/troubleshooting.md` et pendants, `docs/en/capabilities.md` et
`docs/fr/capabilites.md`, `tools/parity/data.py` puis `python3
tools/parity/gen.py --docs`, mémoire du projet. Suites complètes avant le
commit de version : `python3 -m pytest tests/api/ -q -n 12` et `python3 -m
pytest tests/e2e/ -q -n 8`.

### Bancs

| Banc | État | Sert à | Ne sert pas à |
|---|---|---|---|
| node1 | console de dev (8095, unité `harvester-ops-dev`), podman rootless | OpenBao de banc (A et B1 simulé), Vault 1.14.2 jetable | jamais le Vault du homelab (8200) |
| rancher-manager | VM sur harv1, Rancher v2.14.1, client OIDC `harvester-ops-dev` | jetons d'identité réels, compte d'essai `hops-membre` recréé puis supprimé | ses autres clients et réglages |
| idp | VM sur harv1, Keycloak et OpenLDAP, état à relever (dit hors service le 29/09) | compte membre d'un groupe pour le claim `groups` (si accordé) | |
| hops-upd | VM client sur harv1, ARRÊTÉE | B1 réel : mise à jour 2.9.0 -> 2.10.0 avec `b1-policies`, `b1-jwt`, écouteur ESO, restauration, retour arrière refusé puis permis | |
| harvlab2 | v1.9.0, 1 nœud imbriqué sur node2, éteint, pile Cluster API | cluster enfant avec ESO, rotation suivie, retrait sans perte des Secrets | |
| harv1 | v1.9.0, allumé | hôte KVM ou cluster bare-metal non disponibles : lecture `ssh-kvm` faite sur node2 (ci-dessous) | |
| node2 (KVM) | hyperviseur, éteint par défaut | geste KVM d'une personne Rancher avec son jeton (`ssh-kvm`, `tf-kvm`) | ses domaines existants, jamais touchés |

### Préparatifs (règles du homelab)

- OpenBao de banc : rootless, `--memory 512m --memory-swap 512m`, 127.0.0.1:8210
  ; écouteur d'essai ESO sur 172.16.1.11:8212 seulement pendant R8 à R10, port
  ouvert à chaud (`firewall-cmd --add-port`, jamais `--reload`, jamais
  `--permanent`) puis refermé.
- Rancher : compte `hops-membre` recréé pour R2 et R3, supprimé à la fin avec
  son secret Vault ; le client OIDC `harvester-ops-dev` n'est pas modifié
  (sa durée de jeton est celle des essais 1 et 4).
- idp : démarrée par la sous-ressource `start` de harv1 si arrêtée, remise
  dans son état d'origine ensuite ; Keycloak relevé avant (noté hors service le
  29/09).
- harvlab2 : node2 allumé par son iLO (172.16.1.32), `HARVLAB_NAME=harvlab2
  HARVLAB_NODES=1 harvlab.sh start` ; à la fin `harvlab.sh tidy`, `harvlab.sh
  stop`, `sudo systemctl poweroff` de node2, `PowerState: Off` lu par l'iLO.
  Avant d'éteindre node2, demander aux autres sessions si elles l'utilisent.
- hops-upd : démarrée par `start`, arrêtée après (`runStrategy: Halted`
  relu) ; paquet 2.9.0 puis 2.10.0.
- Aucune adresse nouvelle n'est réservée (écouteur sur l'adresse de node1) ;
  rien à enregistrer dans NetBox ni Pi-hole.
- Après chaque essai : montages `jwt` retirés, politiques et groupes de banc
  retirés, entités de banc retirées, aucun jeton de banc ni valeur ESO sur
  disque.

### Déroulé

| # | Vérification | Banc et ressources | Vérifié par |
|---|---|---|---|
| R1 | Mode A : `person-token check` avec chaque vérification mise en défaut une fois (montage absent, audience fausse, compte local, politique trop large, CIDR), puis activation | node1, rancher-manager | réponses, audit du banc |
| R2 | Personne Rancher : geste KVM sur node2, lecture `ssh-kvm` avec son jeton ; audit nomme l'entité et `name` ; Activité marque « jeton personnel » | node2, rancher-manager | audit, Activité |
| R3 | Refus : `hops-membre` sans politique sur `ssh-kvm` (groupe ou réglage « par groupe ») : geste refusé, aucune requête AppRole après | node1, node2 | audit (aucune lecture du service) |
| R4 | Action longue (déclaration Terraform KVM de plus de 15 min) : secrets lus au déclenchement, action finie après l'expiration du jeton de personne | node2 | journal de l'action, audit |
| R5 | Déconnexion Rancher : jeton OpenBao révoqué ; session ouverte avant l'activation : `openbao-person-no-token` | rancher-manager | `lookup` du jeton, message |
| R6 | Clés statiques : JWKS changé (clé de banc ajoutée par simulation), alerte et mise à jour sans couper la session | node1 | bandeau, action |
| R7 | B1 sur hops-upd : mise à jour 2.9.0 -> 2.10.0, `b1-policies`, `b1-jwt`, activation, redémarrage de la VM, instantané et restauration avec la revue élargie, retour sous 2.10.0 refusé puis permis après `--remove` | hops-upd | `systemctl`, revue, garde |
| R8 | ESO posé sur un cluster enfant de harvlab2 par l'option du cluster : sonde, chart, montage, magasin `Ready` | harvlab2 | `kubectl get clustersecretstore`, montage relu |
| R9 | Deux namespaces : chacun lit sa valeur, A ne lit pas celle de B ; rotation dans OpenBao suivie dans le Secret | harvlab2 | `kubectl get secret -o yaml` (empreinte seule), audit |
| R10 | Retrait d'ESO : Secrets gardés, montage et politique retirés, Défaire d'un plan qui l'avait posé | harvlab2 | `kubectl get secret`, `bao auth list` |
| R11 | Vault 1.14.2 jetable : R1 et R2 en mode A | node1 | idem |

Non vérifiable sur nos bancs, à écrire comme tel : un Rancher derrière un
autre fournisseur que Keycloak (Active Directory, SAML), un OpenBao en grappe,
un Vault Enterprise avec espaces de noms, ESO sur un cluster enfant d'un
Harvester à plusieurs nœuds, la rotation de la clé de signature de Rancher en
conditions réelles (simulée en R6).

## Points à trancher

279. **Bancs et accords.** Recommandation : OpenBao rootless sur node1 et
     Vault 1.14.2 jetable (comme la 2.9.0) ; rancher-manager avec le client
     `harvester-ops-dev` et un compte d'essai recréé puis supprimé ; VM `idp`
     démarrée si ju l'accorde (claim `groups`), sinon l'essai 3 est marqué
     « non vérifié » et les groupes internes retenus ; hops-upd pour B1 ;
     harvlab2 (node2 allumé quelques heures) pour ESO ; node2 KVM pour le
     geste d'une personne, sans toucher ses domaines ; port 8212 de node1
     ouvert à chaud le temps des essais ESO.
280. **Jeton présenté à OpenBao.** Options : A `id_token` de Rancher ; B
     jeton d'accès ; C JWT signé par la console. Recommandation : A (le jeton
     d'accès porte un jeton Rancher utilisable sur tous les clusters de la
     personne ; C a été écarté par la décision 135 et ne prouverait rien de
     plus que la console elle-même).
281. **Qui agit sous quelle identité.** Options : A jeton de la personne pour
     les classes personnelles (`ssh-cluster`, `ssh-kvm`, `tf-kvm`, plus `eso`
     en écriture) au déclenchement d'une requête d'une session Rancher,
     service pour tout le reste (table « Qui agit ») ; B jeton de la personne
     pour toute lecture faite dans une requête, `kubeconfig` et
     `replication-*` compris ; C service partout, jeton de personne seulement
     pour un contrôle `capabilities-self`. Recommandation : A (B mettrait la
     découverte des clusters et le flux de connexion à la merci d'une
     politique de personne ; C ne prouve rien à l'audit sur la lecture même).
282. **Repli quand la personne n'a pas de jeton.** Options : A service pour
     les sessions sans jeton OIDC (comptes locaux, connexion directe) et
     quand le réglage est éteint, refus sans repli quand OpenBao refuse une
     personne qui a un jeton ; B repli sur le service à tout échec ; C refus
     de toute lecture personnelle sans jeton, comptes locaux compris.
     Recommandation : A (décision 138 ; B viderait le jeton personnel de son
     sens ; C couperait les comptes locaux gardés pour l'alimentation).
283. **Durée du jeton et actions longues.** Options : A secrets lus au
     déclenchement avec le jeton de la personne et tenus par le bail de
     l'action, jeton OpenBao de 15 min non renouvelé, reconnexion à chaque
     rafraîchissement Rancher avec le nouvel `id_token` ; B si Rancher ne
     renouvelle pas l'`id_token` (essai 1) : jeton renouvelable jusqu'à la
     fin de la session Rancher (`token_max_ttl` = durée de la session, 12 h),
     révoqué à la fermeture ; C jeton aussi long que l'action la plus longue.
     Recommandation : A, B si l'essai 1 l'impose.
284. **Ce qu'une personne peut lire.** Options : A politique commune
     `hops-person-<id>` sur les classes personnelles pour toute personne du
     Rancher de la console, politiques de groupe par cluster en plus, et un
     réglage « lecture seulement par groupe » ; B rien par défaut, tout par
     groupe ; C un rôle OpenBao par rôle de la console, choisi par la
     console. Recommandation : A (C laisse la console choisir le rôle, donc
     OpenBao ne refuse rien de plus qu'elle ; B est offert par le réglage).
285. **Groupes.** Options : A groupes externes nourris par le claim `groups`
     si Rancher l'émet (essai 3), sinon groupes internes tenus par
     l'administrateur d'OpenBao avec l'aide de la console (entités vues,
     commandes) ; B la console recopie les groupes de Rancher dans OpenBao ;
     C pas de groupes en 2.10.0. Recommandation : A (B donnerait à la console
     un droit d'écriture sur l'identité, équivalent administrateur).
286. **Clés de validation des jetons de Rancher.** Options : A découverte
     (`oidc_discovery_url` + CA) par défaut, clés statiques quand OpenBao ne
     joint pas Rancher, empreinte surveillée chaque heure et mise à jour
     sans coupure ; B clés statiques toujours ; C découverte seulement.
     Recommandation : A.
287. **Qui configure OpenBao.** Options : A commandes de l'assistant en mode
     A, agent en B1 (politique élargie par `b1-policies` à la mise à jour),
     la console n'écrit jamais politique, rôle ni montage ; B droit donné à la
     console sur ses propres montages et politiques. Recommandation : A (B
     est l'escalade de l'essai 8 de la 2.9.0).
288. **Cache et copies avec des jetons de personne.** Options : A chaque
     lecture d'une personne passe par OpenBao avec son jeton, cache (entité,
     référence) pour les pannes seulement, copie partagée réutilisée après le
     jugement ; B jugement par `capabilities-self` puis lecture de service ;
     C aucun cache pour les personnes. Recommandation : A.
289. **Audit et entités.** Recommandation : métadonnées `name` et
     `auth_provider` par `claim_mappings`, en-têtes `X-Hops-*` gardés,
     marque « jeton personnel / service / service tardif » dans Activité,
     `hops_identity` dans les métadonnées des versions ; entités laissées
     dans OpenBao (sans politique), leur retrait appartient à
     l'administrateur d'OpenBao, la console les liste.
290. **Activation et migration depuis la 2.9.0.** Recommandation : réglage
     éteint après la mise à jour ; configuration par Rancher (assistant ou
     agent), vérifications dont une connexion réelle de l'administrateur,
     puis activation à chaud ; sessions ouvertes avant : reconnexion demandée
     pour les classes personnelles seulement ; aucun redémarrage de la
     console ni d'OpenBao.
291. **Retour arrière.** Options : A désactivation à chaud ; retour sous
     2.10.0 refusé tant que B1 porte un montage `jwt-hops-*` ou l'écouteur
     ESO, avec les étapes `person-token disable --remove` et `eso remove
     --all` ; permis avec avertissement en mode A ; B 2.9.1 qui tolère ces
     montages dans sa revue de restauration ; C aucun garde.
     Recommandation : A (même règle que le retour sous 2.9.0) ; B en plus si
     une 2.9.1 sort de toute façon (liste de nettoyage déjà prévue).
292. **Revue d'un instantané restauré.** Recommandation : liste blanche
     élargie aux seuls objets rendus par la console (montages, rôles exacts,
     politiques `hops-person`, `hops-group`, `hops-eso`, groupes
     `hops-<id>-*`, entités sans politique), toute autre différence refuse.
293. **Portée d'ESO.** Options : A clusters enfants créés par la console,
     option par cluster, OpenBao actif ; B aussi les clusters Harvester ; C
     tout cluster déclaré. Recommandation : A (Harvester n'est pas une cible
     de charges applicatives ; C sort du périmètre « clusters créés par la
     console »).
294. **Livraison d'ESO.** Options : A chart dans un paquet d'artefacts,
     installé par CAAPH, version portée par le paquet, objets du magasin par
     ClusterResourceSet ; B par le GitOps de la 2.8.0 ; C `kubectl apply` par
     la console. Recommandation : A (modèle `flux2`, airgap, retrait par
     étiquettes).
295. **Authentification d'ESO dans OpenBao.** Options : A méthode `jwt` par
     cluster enfant avec les clés publiques de ses comptes de service lues
     par la console ; B méthode `kubernetes` (TokenReview : OpenBao doit
     joindre l'API du cluster enfant et garder un jeton de relecteur) ; C
     AppRole avec un `secret_id` dans le cluster enfant. Recommandation : A
     (aucun flux d'OpenBao vers le cluster enfant, aucun secret de longue
     durée, isolement des clés par montage).
296. **Isolement par namespace.** Options : A un `SecretStore` et un compte
     `hops-eso` par namespace activé, un seul rôle et une seule politique à
     gabarit (`metadata.namespace` de l'alias) par cluster ; B un rôle et une
     politique par namespace, écrits par l'agent ou l'administrateur à chaque
     activation ; C un `ClusterSecretStore` avec un compte unique pour tout
     le cluster. Recommandation : A si l'essai 5 confirme le gabarit, sinon B
     ; jamais C (le fournisseur `vault` exige un namespace dans la référence
     de compte d'un `ClusterSecretStore` : un seul compte lirait les secrets
     de tous les namespaces).
297. **Où vivent les secrets ESO.** Options : A classe `eso` sous le préfixe
     de la console (`eso/<clé>/<namespace>/<nom>`) ; B montage KV choisi par
     le client. Recommandation : A (une seule configuration à vérifier ; B
     reste possible plus tard par un réglage de chemin).
298. **Écoute de B1 pour ESO** (complète le point 254). Options : A second
     écouteur TLS sur une adresse de l'hôte (port 8212 par défaut), éteint
     tant qu'aucun cluster n'a ESO, rôles de la console toujours liés à
     127.0.0.1 ; B ESO en mode A seulement ; C mandataire inverse.
     Recommandation : A.
299. **Qui écrit les `ExternalSecret` et les valeurs.** Options : A les
     `ExternalSecret` appartiennent aux équipes (GitOps ou leurs outils), la
     console pose le magasin, montre l'état et permet d'écrire une valeur
     avec le jeton de la personne ; B la console rend aussi des
     `ExternalSecret` depuis les plans. Recommandation : A.
300. **Retrait d'ESO.** Options : A `ownerReferences` des Secrets
     synchronisés retirées d'abord, Secrets gardés à leur dernière valeur ;
     B tout supprimer. Recommandation : A (B couperait les charges des
     équipes).
301. **Découpage.** Options : A tout en 2.10.0, tâches 8 à 10 (ESO)
     détachables en 2.10.1 si l'essai 7 échoue ; B jeton personnel en
     2.10.0, ESO en 2.11.0. Recommandation : A.
302. **Connexion OIDC autre que Rancher** (la décision 138 dit « Rancher ou
     OIDC »). Options : A Rancher seulement en 2.10.0 (seule connexion OIDC
     de la console) ; B ajouter une connexion OIDC générique.
     Recommandation : A, B à concevoir à part si un client le demande.

## Sources

- Rancher, fournisseur OIDC (drapeau `oidc-provider`, `OIDCClient`,
  `tokenExpirationSeconds`, `refreshTokenExpirationSeconds`, portées par
  défaut, secret du client, clé de signature `cattle-system/oidc-signing-key`
  et rotation) :
  https://ranchermanager.docs.rancher.com/how-to-guides/advanced-user-guides/configure-oidc-provider
- Rancher, points d'entrée du fournisseur (`/oidc/.well-known/openid-configuration`,
  `/oidc/.well-known/jwks.json`, `/oidc/token`) :
  https://raw.githubusercontent.com/rancher/rancher/main/pkg/oidc/provider/provider.go ;
  contenu des jetons (RS256, `kid`, claims de l'`id_token` et du jeton
  d'accès, `aud` commun) :
  https://raw.githubusercontent.com/rancher/rancher/main/pkg/oidc/provider/token.go ;
  valeurs de `groups` :
  https://raw.githubusercontent.com/rancher/rancher/main/pkg/oidc/provider/groups.go ;
  portée `groups` et `expires_in` en secondes : https://github.com/rancher/rancher/pull/55473
- OpenBao, versions (2.7.2 et 2.6.5 du 26/10/2026) : https://github.com/openbao/openbao/releases
- OpenBao, méthode JWT/OIDC (configuration, une seule source de clés, rôles,
  `claim_mappings`, pointeurs JSON, `bound_claims`, renouvellement, rôles CEL) :
  https://openbao.org/docs/api/auth/jwt/ et https://openbao.org/docs/auth/jwt/ ;
  Kubernetes comme fournisseur (clés statiques, révocation) :
  https://openbao.org/docs/auth/jwt/oidc-providers/kubernetes/
- Vault, `jwks_pairs` (absent d'OpenBao) : https://developer.hashicorp.com/vault/api-docs/auth/jwt ;
  audience exigée depuis Vault 1.17 : https://github.com/hashicorp/vault/issues/27343
- OpenBao, identité (entités, alias, groupes externes relus à la connexion et
  au renouvellement) : https://openbao.org/docs/concepts/identity/ et
  https://openbao.org/docs/secrets/identity/
- OpenBao, gabarits des politiques : https://openbao.org/docs/concepts/policies/
- OpenBao, audit (champs `auth`, HMAC) : https://openbao.org/docs/audit/ et
  https://pkg.go.dev/github.com/OpenBao/openbao/audit
- OpenBao, méthode Kubernetes (jeton du client comme relecteur) :
  https://openbao.org/docs/api/auth/kubernetes/ et https://openbao.org/docs/auth/kubernetes/
- External Secrets Operator, versions (v2.14.0 du 23/10/2026) :
  https://github.com/external-secrets/external-secrets/releases ; maintenance :
  https://external-secrets.io/latest/introduction/stability-support/ ;
  `v1beta1` non servie : https://github.com/external-secrets/external-secrets/issues/4785
- ESO, installation (dépôt du chart, CRD au-delà de 256 Kio) :
  https://external-secrets.io/latest/introduction/getting-started/ ; valeurs :
  https://raw.githubusercontent.com/external-secrets/external-secrets/main/deploy/charts/external-secrets/values.yaml
- ESO, fournisseur Vault (méthodes, `kubernetesServiceAccountToken`, namespace
  exigé pour un `ClusterSecretStore`) : https://external-secrets.io/latest/provider/hashicorp-vault/ ;
  OpenBao par le fournisseur Vault : https://external-secrets.io/latest/provider/openbao/ ;
  fournisseur OpenBao propre (alpha) :
  https://github.com/external-secrets/external-secrets/issues/6446 et
  https://github.com/external-secrets/external-secrets/pull/6591
- ESO, `ClusterSecretStore` (`conditions`) :
  https://external-secrets.io/latest/api/clustersecretstore/ ; `ExternalSecret`
  (`creationPolicy`, `deletionPolicy`, `refreshPolicy`) :
  https://external-secrets.io/latest/api/externalsecret/ ; `PushSecret` :
  https://external-secrets.io/latest/api/pushsecret/
- RKE2, accès anonyme coupé et émetteur par défaut :
  https://docs.rke2.io/security/cis_self_assessment110 ; découverte de
  l'émetteur des comptes de service :
  https://kubernetes.io/docs/tasks/configure-pod-container/configure-service-account/

## Décisions (08/11/2026)

Points 280 à 302 : recommandations retenues (consigne de ju : « on fait tes
recommandations »). Point 279 retenu sans la VM `idp` : brancher Keycloak ou
OpenLDAP comme fournisseur d'authentification de Rancher modifierait la
configuration de connexion du Rancher de ju, ce qui reste sa décision. En
attendant, l'essai du claim `groups` se limite à ce que donne un compte local
de Rancher, et le repli par groupes internes (point 285) est la voie
vérifiée.
