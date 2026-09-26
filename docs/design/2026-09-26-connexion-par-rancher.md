# Se connecter à la console par Rancher, avec les droits de Rancher

État : livré en 1.50.0, 25/09/2026. Conception validée par l'exploitant (« parfait »),
après une maquette sur le vrai Rancher. Demande : « les utilisateurs de
Rancher peuvent se connecter sur harvops une fois connectés sur Rancher, et
les droits sont hérités ».

## Ce que la maquette a établi (Rancher Prime v2.14.1, harv1 importé `c-sg2q6`)

- Rancher a un **fournisseur OIDC intégré** (fonction `oidc-provider`,
  active par défaut depuis 2.12). On y déclare la console une fois
  (`OIDCClient`, adresses de retour) ; Rancher donne un identifiant et un
  secret.
- **Authentification unique** : un utilisateur déjà connecté à Rancher
  reçoit le code sans rien ressaisir ; sinon Rancher montre sa page de
  connexion, compte local ou Keycloak (Keycloak n'est pas nécessaire).
- Le jeton d'accès rendu **fonctionne sur l'API de Rancher et sur son
  mandataire vers le cluster** (`/k8s/clusters/c-sg2q6`). En passant par là,
  c'est Rancher qui applique ses droits : l'utilisateur, ses groupes
  Keycloak, ses rôles de cluster et de projet.
- Rancher écrit déjà ses droits dans harv1 (liaisons `crb-*`,
  `globaladmin-*`, groupe `keycloakoidc_group://rancher-admins` ->
  `cluster-admin`). La console n'a rien à recopier.
- Supprimer le client OIDC ne révoque pas les jetons émis.
- Le jeton OIDC **est** la session Rancher de la personne (même jeton,
  étiqueté par le client) : se déconnecter de Rancher ferme aussi la session
  de la console, à son renouvellement suivant.
- `expires_in` y est donné en **nanosecondes** (600000000000 pour 600 s) :
  seule la date `exp` du jeton fait foi.

## Parcours

1. La console affiche une page de connexion : **« Se connecter avec
   Rancher »**, et **« Compte local »** quand un htpasswd existe.
2. « Rancher » : code d'autorisation avec PKCE (S256), `state` et `nonce`
   à usage unique liés au navigateur par un cookie ; retour sur
   `/auth/rancher/callback`.
3. La console échange le code **de serveur à serveur, en TLS, avec son
   secret**. OIDC Core 3.1.3.7 autorise alors à se fier à TLS plutôt qu'à la
   signature du jeton : pas de bibliothèque cryptographique à embarquer
   (livrable airgap). Émetteur, audience, expiration et `nonce` sont
   vérifiés, puis l'identité est **relue auprès de Rancher** avec le jeton
   (`/v3/users?me=true`) : Rancher reste l'autorité.
4. La console garde le **jeton d'accès et le jeton de rafraîchissement
   côté serveur** seulement, renouvelle le premier avant son expiration (10
   minutes) et réécrit les kubeconfigs de la session : une action longue
   continue. Le navigateur ne reçoit qu'un identifiant de session aléatoire
   (cookie HttpOnly, SameSite=Lax, Secure dès que l'adresse de retour est en
   HTTPS, même derrière Traefik).

   Prévu d'abord : un jeton Rancher dérivé, valable toute la session.
   **Rancher le refuse** : un jeton OIDC ne peut ni créer ni supprimer de
   jeton Rancher (401 « failed to retrieve auth token », vu en réel). D'où
   le rafraîchissement, et une déconnexion qui **oublie** la session plutôt
   que de la révoquer.

## Droits

- **Sur un cluster géré par ce Rancher** : chaque appel part vers
  `https://<rancher>/k8s/clusters/<id>` avec le jeton de l'utilisateur. Le
  point de substitution unique du kubeconfig (v1.32.0) rend ce kubeconfig ;
  les scripts et les ~50 sites d'appel n'ont pas à changer.
- **Correspondance cluster de la console -> cluster Rancher** : lue dans la
  configuration (`rancher_cluster`), sinon trouvée seule en comparant l'UID
  de l'espace de noms `kube-system` (le même objet vu des deux côtés).
- **Cluster que Rancher ne gère pas**, ou auquel l'utilisateur n'a pas
  accès : invisible pour lui, et dit.
- **Rôle dans la console** (réglages, déclaration des clusters) :
  administrateur pour les administrateurs de Rancher (rôle global `admin`)
  et les groupes listés en configuration ; opérateur sinon (réglable).
  Les gestes sur les clusters restent jugés par Rancher.
- **Arrêt et démarrage d'un cluster** : réservés aux comptes locaux. Un
  cluster éteint ne passe plus par Rancher, et harv1 porte la VM de Rancher :
  l'éteindre à travers Rancher couperait la branche en cours de route.

## Sécurité

- Requêtes qui modifient, authentifiées par cookie : l'en-tête `Origin`
  (ou `Referer`) doit désigner la console, en plus de SameSite.
- Aucun jeton dans une réponse, un journal, une ligne de commande ou une
  étiquette d'action. Les kubeconfigs de session sont dans le répertoire
  privé des identités (0700, fichiers 0600), effacés à la déconnexion.
- Sessions en mémoire : un redémarrage de la console les perd, et
  l'authentification unique reconnecte sans saisie.
- **Garde centrale** : pour une session Rancher, toute requête qui nomme un
  cluster que Rancher ne lui montre pas est refusée, et ces clusters sont
  retirés des listes. Le statut, les actions de script, la console VNC et le
  paquet de diagnostic ont été relus : ils passaient encore par le compte
  de la console, corrigés.
- Les comptes locaux (HTTP Basic, htpasswd) restent inchangés.

## Configuration

```yaml
rancher:
  url: https://rancher.example.com
  client_id: client-xxxxxxxx
  client_secret_file: /etc/harvester-ops/rancher-oidc-secret   # 0600
  redirect_uri: https://console.example.com/auth/rancher/callback  # derrière un mandataire TLS
  default_role: operator          # rôle console des non-administrateurs
  admin_groups: []                # principaux de groupe -> admin console
  session_hours: 12
```

Et, par cluster si l'on ne veut pas de la découverte :
`rancher_cluster: c-sg2q6`.

## Essai réel sur Rancher Prime 2.14.1 et harv1 (25/09/2026)

Console de dev déclarée comme client OIDC (`harvester-ops-dev`), essais à
travers `https://harvops.home.lo` (Traefik) comme l'exploitant l'utilise.

1. **Administrateur de Rancher** (compte local) : connecté à Rancher, un clic
   sur « Se connecter avec Rancher » et retour dans la console en 6 s, sans
   saisie ; `admin@rancher`, rôle admin ; seul harv1 proposé (les autres
   bancs sont éteints ou inconnus de Rancher) ; 14 VMs listées et un arrêt
   sans effet appliqué, à travers Rancher ; kube-ovn lu à travers Rancher ;
   l'arrêt du cluster refusé ; déconnexion.
2. **Compte d'essai « membre du cluster »** : `hops-membre@rancher`, rôle
   opérateur ; harv1 proposé mais **harv1 lui-même refuse** :
   `User "u-t286c" cannot get resource "virtualmachines"` (u-t286c est son
   id Rancher). Les droits viennent bien de Rancher.
3. Trois écarts trouvés en route et corrigés : le jeton dérivé refusé par
   Rancher (d'où le rafraîchissement), `expires_in` en nanosecondes, la
   découverte qui interrogeait aussi les clusters indisponibles (6 s ->
   limitée aux clusters actifs).
4. Nettoyage : compte d'essai, ses droits sur harv1 et les sessions des
   essais supprimés. Le client OIDC de la console de dev est gardé (secret
   en Vault `secret/services/harvester-ops-rancher-oidc`).

Reste à améliorer : sur harv1, l'espace de noms `default` pointe vers un
projet d'un autre Rancher (`c-qt5jz`), si bien que les rôles de projet de ce
Rancher ne couvrent pas les VMs.

## v1.56.0 : ce que le réel a encore appris

Nouvel essai avec un compte « membre du cluster » (`hops-membre`, créé pour
l'occasion puis supprimé), console de dev fraîchement redémarrée :

1. **Le membre ne voyait AUCUN cluster.** La découverte lisait `kube-system`
   avec son jeton : refusé (`cannot get resource "namespaces" in the
   namespace "kube-system"`). En 1.50.0 il voyait harv1 parce que
   l'administrateur s'était connecté avant lui et que l'id découvert était
   gardé pour tout le processus. Deux corrections :
   - les nœuds, que le membre lit, désignent le cluster aussi sûrement
     (leurs UID relus par la console) ;
   - l'id d'un cluster appris par une session sert aux suivantes, mais
     Rancher est interrogé compte par compte (`GET /v3/clusters/<id>`,
     réponse gardée cinq minutes) : avant, un compte sans aucun droit
     voyait le cluster dès qu'un autre l'avait découvert.
2. **Les vues vides sans un mot** : l'aperçu montrait « 0 nœud » parce que
   le `get nodes,vm,vmi` groupé échouait en bloc sur les VMs refusées ; la
   topologie, la carte du stockage, les modèles rendaient des listes vides.
   Les refus sont maintenant rangés (verbe, ressource, groupe, espace de
   noms) depuis tous les appels kubectl de la console et le script d'état,
   joints à la réponse (en-tête `X-Cluster-Denied`) et affichés dans un
   bandeau ; le script d'état relit type par type ce qui est permis.
3. **Audit D18, jeton périmé pendant un apply long** : Terraform recopie le
   kubeconfig dans son espace ; le jeton qui y était écrit mourait au bout
   de dix minutes. Le kubeconfig désigne désormais un fichier de jeton
   (`tokenFile`) que le renouvellement réécrit. Prouvé avec client-go
   v0.33.7 (celui du provider) contre un serveur TLS de test : un client
   lancé passe au nouveau jeton dans la minute. Point payé : client-go ne
   présente AUCUN identifiant à un serveur en HTTP simple, le premier essai
   en `httptest.NewServer` ne voyait donc aucun en-tête. Plan réel sur harv1
   par une session Rancher : kubeconfig à `tokenFile`, « 1 à créer ».
4. La garde centrale des clusters avait perdu son décorateur
   `@app.before_request` pendant la réécriture : les tests existants l'ont
   vu tout de suite (500 au lieu de 403).

