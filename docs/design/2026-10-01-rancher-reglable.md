# Connexion par Rancher réglable dans l'interface, et chart Harvester RBAC (1.79.0)

## Le besoin

La 1.50.0 a livré la connexion par Rancher avec droits hérités : un seul
Rancher, réglé dans `config.yaml` (`rancher:` : url, client_id,
client_secret_file, default_role, session_hours, redirect_uri), par son
fournisseur OIDC, le client OIDC étant déclaré à la main dans Rancher. Le
parcours voulu (validé sur maquette le 29/09/2026) :

- les Rancher se règlent dans l'interface (Réglages > Connexion par
  Rancher), à chaud, **plusieurs** possibles, avec un bouton **Tester** ;
- **connexion directe** par identifiant et mot de passe Rancher, sans rien
  déclarer dans Rancher (`/v3-public/<fournisseur>Providers/<fournisseur>?action=login`) ;
- **authentification unique en option** : la console s'enregistre elle-même
  dans Rancher (OIDCClient) avec un administrateur de ce Rancher, une seule
  fois, sans garder ses identifiants ;
- page de connexion : la liste des Rancher s'il y en a plusieurs (dernier
  choix retenu), le bouton SSO s'il est activé, jamais d'adresse libre ; le
  compte local de la console en bas ;
- la console propose d'installer le **chart Harvester RBAC** (catalogue
  `rancher-charts`, `harvester-rbac`, vérifié sur Rancher v2.14.1 : version
  `109.0.0+up0.1.1`, `catalog.cattle.io/rancher-version >= 2.14.0 < 2.15.0`,
  Kubernetes `< 1.36`), qui apporte les rôles « View/Manage Virtualization
  Resources » au niveau cluster et projet, dans le cluster `local` de Rancher.

## Décisions

| Sujet | Retenu |
|---|---|
| Où vivent les réglages | Dans l'état de la console (`<état>/rancher.d/<id>.yaml`, secret du client OIDC à côté en 0600, chemins relatifs) : portable comme les clusters déclarés par la console (1.78.0). La section `rancher:` de `config.yaml` reste lue, en lecture seule, origine « config », prioritaire sur un réglage de même adresse |
| Prise en compte | À chaud : aucun redémarrage |
| Connexion directe | Fournisseurs à mot de passe seulement (local, LDAP, OpenLDAP, Active Directory, FreeIPA) ; les autres (OIDC, SAML, GitHub) passent par le SSO |
| Jeton obtenu | Un jeton Rancher de la personne, utilisé exactement comme celui du SSO (mandataire `/k8s/clusters/<id>`, droits hérités) ; déconnexion = suppression du jeton dans Rancher |
| Enregistrement SSO | Identifiants d'un administrateur demandés une fois, jamais gardés ; création de l'OIDCClient, lecture du secret généré, gardé 0600 dans l'état ; « Désenregistrer » le supprime de Rancher |
| Chart RBAC | Un bouton par Rancher : état (absent, installé, version), installer, suivi comme action ; refus clair si la version de Rancher ou de Kubernetes ne convient pas |
| Rôle dans la console | Rôle par défaut par Rancher (viewer, operator, admin), comme aujourd'hui |

## Vérification

- Tests : réglages (lecture, priorité de `config.yaml`, écriture, chemins
  relatifs, secret jamais renvoyé), connexion directe simulée, rafraîchissement
  à chaud, page de connexion multi-Rancher (e2e).
- En réel sur le Rancher de harv1 (`rancher.home.zypp.fr`, v2.14.1) : test de
  connexion, connexion directe avec un compte local Rancher puis avec un
  compte Keycloak si le fournisseur LDAP n'est pas configuré (sinon SSO),
  enregistrement SSO par la console puis connexion SSO, désenregistrement,
  installation du chart RBAC et présence des quatre rôles, un utilisateur à
  qui on donne « View Virtualization Resources » sur harv1 voit les VMs dans
  la console sans pouvoir les modifier.
