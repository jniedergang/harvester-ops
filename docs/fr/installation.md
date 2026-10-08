# Guide d'installation

## Pré-requis sur le poste opérateur

| Composant | Version | Pourquoi |
|---|---|---|
| OS | SUSE / openSUSE | Cibles supportées (dépendances installées via `zypper`) |
| `bash` | ≥ 4.0 | Scripts |
| `kubectl` | version mineure du cluster | Client API |
| `ssh` / `ssh-keygen` | OpenSSH 7+ ; `ssh-keygen -Y` (8.2+) pour les mises à jour, sinon l'image de la console vérifie la signature (1.87.2) | Extinction des nodes + vérification des signatures |
| `yq` | ≥ 4.0 (mikefarah) | Parsing config |
| `python3` | ≥ 3.9 | Helpers JSON + UI Flask |
| `podman` | récent | Requis uniquement pour l'UI web |

One-liner pour installer les dépendances :

```bash
sudo zypper install -y bash openssh-clients kubectl yq python3 podman
```

L'agent de mise à jour tourne sur l'hôte, hors du conteneur de la console.
L'installeur choisit un Python >= 3.9 (`python3`, ou `python3.9`–`python3.14`)
et inscrit son chemin absolu dans `harvester-ops-update.service`. Sans
interpréteur compatible, il refuse d'installer l'agent ; il ne change pas
l'alternative Python du système. Le Python du conteneur ne suffit pas.

Quand le `ssh-keygen` de l'hôte ne connaît pas `-Y` (OpenSSH avant 8.2, comme
sur RHEL 8), l'agent de mise à jour fait vérifier la signature par le
`ssh-keygen` de l'image de la console installée (1.87.2) : un conteneur jetable,
sans réseau, au système de fichiers en lecture seule, qui ne voit que la
signature et les clés de confiance et lit l'archive sur son entrée standard.
Rien n'est téléchargé et le contrôle reste obligatoire.

Pour une installation existante dont l'unité utilise un ancien Python, une
correction sur l'hôte est nécessaire avant de recevoir ce correctif depuis
l'interface : définir `ExecStart` dans une surcharge systemd avec un Python
compatible (par exemple `/usr/bin/python3.12 /usr/local/bin/harvester-ops-update.py`,
précédé d'un `ExecStart=` vide), lancer `systemctl daemon-reload`, puis
réessayer depuis l'interface.

## Étapes d'installation

### 1. Récupérer et vérifier le tarball

```bash
sha256sum -c harvester-ops-1.0.0.tar.gz.sha256
tar xzf harvester-ops-1.0.0.tar.gz
cd harvester-ops-1.0.0
```

### 2. Lancer l'installeur

```bash
sudo ./install.sh
```

L'installeur est **interactif**. Il demande :

- Installer l'UI web ? (défaut : oui)
- Le premier compte de la console, nom et mot de passe (uniquement si UI) :
  il administre la console (1.57.0 ; il arrivait lecteur auparavant)
- TLS : générer un certificat self-signed ? (défaut : oui)
- Port d'écoute de l'UI (défaut : 8090)
- Service systemd pour l'UI ? (défaut : oui)

L'installeur va :

- Copier `bin/*` dans `/usr/local/bin/`
- Créer le compte système `harvester-ops` : le conteneur de l'UI web tourne
  sous ce compte, jamais en root
- Créer `/etc/harvester-ops/` (config, htpasswd, TLS, dossier clés ssh),
  lisible par le groupe `harvester-ops` et par aucun autre compte
- Créer `/var/lib/harvester-ops/` (historique des actions, notes, photo de
  la surveillance des clusters) et `/var/log/harvester-ops/`, qui
  appartiennent à ce compte
- Copier `config/config.yaml.example` → `/etc/harvester-ops/config.yaml` (si absent)
- Charger `images/harvester-ops-ui.tar` dans podman/docker
- Installer `config/systemd/harvester-ops.service` (si UI sélectionnée)

L'installeur pose aussi le paquet Cluster API et le provider Terraform
embarqués dans `/var/lib/harvester-ops` (1.51.0) : les onglets Cluster API
et Terraform marchent sans rien télécharger. Un paquet ou un provider choisi
ensuite depuis la console est conservé par les installations suivantes.

### 3. Fournir les kubeconfigs et clés SSH

Pour chaque cluster à gérer :

```bash
sudo install -m 0640 -g harvester-ops /chemin/vers/prod-kubeconfig.yaml /etc/harvester-ops/kubeconfigs/prod.yaml
sudo install -m 0640 -g harvester-ops /chemin/vers/id_ed25519 /etc/harvester-ops/ssh/id_ed25519
```

L'UI web lit ces fichiers sous le compte `harvester-ops`, par son groupe.
Ne pas les réserver à root (`chmod 600`) : le service rétablit la lecture
par le groupe à chaque démarrage, et un fichier copié autrement est donc
corrigé par `sudo systemctl restart harvester-ops`. SSH accepte une clé
qui appartient à root et que le groupe peut lire.

### 4. Éditer la config

```bash
sudo $EDITOR /etc/harvester-ops/config.yaml
```

Déclarer chaque cluster avec son nom, le chemin du kubeconfig, les credentials SSH, et la liste complète des nodes (hostname, IP, rôle).

### 5. Tester la connectivité (read-only)

```bash
harvester-status --cluster prod
```

Vous devez voir les nodes, VMs et volumes Longhorn.

### 6. Valider avec un dry-run

```bash
harvester-shutdown --cluster prod --dry-run --yes
```

Affiche toutes les commandes qui seraient exécutées, sans rien faire.

### 7. Démarrer l'UI web (si installée)

```bash
sudo systemctl enable --now harvester-ops
sudo systemctl status harvester-ops
```

Ouvrir `https://<host>:8090` dans un navigateur. Accepter le certificat self-signed, se logger avec les credentials définis à l'installation.

### 8. Déplacer des VMs entre clusters (facultatif)

Quand deux clusters ne partagent pas de cible de sauvegarde, une VM passe
par cet hôte : les nœuds du cluster cible viennent y chercher chaque disque
en HTTP, sur le port 8094 par défaut. L'ouvrir pour eux, ou choisir une
autre adresse et un autre port dans `config.yaml` :

```bash
sudo firewall-cmd --permanent --add-port=8094/tcp && sudo firewall-cmd --reload
```

```yaml
transfer:
  serve_address: 10.0.0.5:8094    # une adresse que les nœuds des clusters joignent
```

Les archives exportées sont gardées dans `/var/lib/harvester-ops/exports`
(le volume persistant du service), comme celles déposées depuis un
navigateur (1.47.0) : dimensionner ce volume pour la plus grosse VM qu'on
pense déplacer par fichier.


### 8b. Se connecter (1.57.0)

La console demande toujours qui vous êtes : il n'y a plus de mode ouvert
(`HARVESTER_OPS_AUTH=none` le rétablit pour les tests seulement, et tant
qu'aucun compte n'existe). On se connecte sur la page de connexion avec un
nom et un mot de passe ; la session est un cookie HttpOnly, et « Se
déconnecter » dans le menu du compte (en haut à droite) la ferme. Les
scripts et clients d'API gardent HTTP Basic.

- **Comptes** : celui de l'installeur (htpasswd) et ceux créés dans
  Réglages > Comptes de la console, gardés dans
  `/var/lib/harvester-ops/accounts.json` (empreintes bcrypt, mode 0600). Un
  administrateur les crée, choisit leur rôle (lecteur, opérateur,
  administrateur), réinitialise un mot de passe ou les supprime ; chacun
  change son propre mot de passe depuis le menu du compte. Chaque changement
  est inscrit dans l'Activité.
- **Premier démarrage sans aucun compte** (ni htpasswd, ni Rancher) : toute
  page mène à `/setup`, qui crée le premier administrateur. Il demande un
  jeton écrit dans `/var/lib/harvester-ops/setup-token` (mode 0600, le
  journal du service en donne le chemin) : seul qui administre le serveur
  peut le lire, si bien que personne ne prend la console en atteignant son
  port le premier.

### 9. Envoyer des images depuis le navigateur (facultatif, 1.63.0)

Un fichier d'image envoyé par le navigateur est gardé par la console, puis
offert une fois au cluster, dont les nœuds le téléchargent en HTTP depuis
cet hôte, sur le port **8092** par défaut. L'ouvrir pour eux, ou choisir un
autre port dans `/etc/harvester-ops/env` :

```bash
sudo firewall-cmd --permanent --add-port=8092/tcp && sudo firewall-cmd --reload
echo HARVESTER_OPS_IMAGE_UPLOAD_PORT=8192 | sudo tee -a /etc/harvester-ops/env   # un autre port
```

Le fichier attend dans `/var/lib/harvester-ops/image-uploads` et il est
effacé dès que l'image est importée (ou l'action annulée).
### 10. Connexion par Rancher (facultatif, 1.50.0, réglée dans l'interface depuis la 1.79.0)

Les personnes qui ont un compte Rancher Manager (2.12 et suivants) se
connectent à la console avec lui, et reçoivent les droits que Rancher leur
donne sur chaque cluster.

**Depuis l'interface (1.79.0)** : Paramètres > Connexion par Rancher, en
administrateur de la console. Ajouter un Rancher (un libellé, son adresse
`https://`, et son autorité de certification en PEM quand le certificat de
Rancher n'est pas signé par une autorité publique ; « ne pas vérifier TLS »
existe pour un banc), puis **Tester** : la console montre la version de
Rancher et ses fournisseurs d'authentification. Plusieurs Rancher peuvent
être réglés ; un changement vaut tout de suite, sans redémarrage.

- **Connexion directe** (active par défaut) : la page de connexion demande
  l'identifiant et le mot de passe Rancher, pour les fournisseurs à mot de
  passe (utilisateurs locaux, LDAP, OpenLDAP, Active Directory, FreeIPA).
  Rien à déclarer dans Rancher. La console obtient un jeton Rancher pour la
  personne, valable la durée de session (1 à 24 heures), s'en sert
  exactement comme du jeton de l'authentification unique, et le supprime
  dans Rancher à la déconnexion. La session n'est pas renouvelée : elle
  finit avec le jeton. Les fournisseurs sans mot de passe (OIDC comme
  Keycloak, SAML, GitHub) passent par l'authentification unique.
- **Authentification unique** (facultative) : **Enregistrer** demande une
  fois l'identifiant et le mot de passe d'un administrateur de Rancher. La
  console se connecte avec, crée son client OIDC dans Rancher (avec son
  adresse de retour, prise sur l'adresse du navigateur), garde le secret
  généré (0600) et se déconnecte ; les identifiants de l'administrateur ne
  sont pas gardés. Une personne déjà connectée à Rancher entre alors sans
  rien saisir. **Désenregistrer** retire le client de Rancher.
- **Chart Harvester RBAC** : la console dit si le chart `harvester-rbac` du
  catalogue `rancher-charts` est installé dans le cluster `local` de
  Rancher, et l'installe (action suivie, avec les identifiants d'un
  administrateur de Rancher, non gardés). Il apporte les rôles « View/Manage
  Virtualization Resources », pour les clusters et les projets. La console
  refuse en disant pourquoi quand l'exigence du chart sur Rancher ou
  Kubernetes n'est pas remplie (109.0.0+up0.1.1 : Rancher 2.14.x,
  Kubernetes avant 1.36).
- La page de connexion liste les Rancher (le dernier choisi en premier),
  jamais une adresse libre, et les comptes de la console en dessous. Un
  Rancher qui ne répond pas en trois secondes est montré indisponible et ne
  bloque pas la page.

Ces réglages vivent dans le répertoire d'état de la console
(`/var/lib/harvester-ops/rancher.d/<id>.yaml`, l'autorité et le secret du
client à côté, en 0600, chemins relatifs au répertoire d'état) : ils
suivent la console quand on la déplace (voir plus bas).

**Depuis `config.yaml` (1.50.0, toujours lu)** : la section ci-dessous
déclare un Rancher, montré en lecture seule dans l'interface (pastille
« config.yaml »). Il l'emporte sur un Rancher réglé dans l'interface à la
même adresse. Ajouter `direct_login: true` pour lui proposer aussi la
connexion directe. Pour l'authentification unique, déclarer la console
dans Rancher, sur son cluster local :

```bash
cat <<EOF | kubectl apply -f -
apiVersion: management.cattle.io/v3
kind: OIDCClient
metadata:
  name: harvester-ops
spec:
  description: console harvester-ops
  redirectURIs: ["https://console.example.com/auth/rancher/callback"]
  tokenExpirationSeconds: 600
  refreshTokenExpirationSeconds: 43200     # la durée d'une session
EOF
CID=$(kubectl get oidcclient harvester-ops -o jsonpath='{.status.clientID}')
kubectl get secret -n cattle-oidc-client-secrets "$CID" \
  -o jsonpath='{.data.client-secret-1}' | base64 -d \
  | sudo install -m 0600 /dev/stdin /etc/harvester-ops/rancher-oidc-secret
```

Puis dans `config.yaml` :

```yaml
rancher:
  url: https://rancher.example.com
  client_id: client-xxxxxxxx
  client_secret_file: /etc/harvester-ops/rancher-oidc-secret
  redirect_uri: https://console.example.com/auth/rancher/callback
  default_role: operator        # rôle console des non-administrateurs
  admin_groups: []              # groupes Rancher faits administrateurs de la console
  session_hours: 12             # à garder égal à refreshTokenExpirationSeconds
```

La console doit joindre Rancher, et chaque cluster doit être importé dans
Rancher pour qu'une personne de Rancher le voie. Les comptes locaux restent
utilisables ; l'arrêt et le démarrage d'un cluster leur restent réservés.

## Mettre harvops à jour (1.82.0)

Depuis la 1.82.0, la console se met à jour depuis l'interface : cliquer sur
le numéro de version, puis l'onglet **Mise à jour** (administrateurs).

- **En ligne** : la console lit `release.json` à sa source de mise à jour (les
  publications GitHub du projet par défaut ; un miroir interne est n'importe
  quel répertoire HTTP qui sert les mêmes fichiers : `release.json`,
  l'archive et sa `.sig`). **Vérifier**, puis **Télécharger** : l'archive et
  sa signature sont récupérées, la SHA-256 comparée à `release.json`, la
  signature vérifiée. La console suit `HTTPS_PROXY`.
- **Hors ligne** : fournir l'archive `harvester-ops-<version>.tar.gz` et sa
  `.sig` par le navigateur (écrites en flux sur le disque, jamais en mémoire).
- **Installer** : la console confie la version à l'**agent de mise à jour**
  de l'hôte (`harvester-ops-update.path` et `.service`, posés par
  `install.sh`). L'agent, root, revérifie la signature avec les clés de
  confiance de l'hôte, garde les fichiers et l'image en place, lance
  l'`install.sh --upgrade` de la version (scripts, image, fournisseurs
  embarqués, unités ; jamais la configuration, les comptes ni les
  certificats), redémarre la console et attend que la nouvelle version
  réponde. Sinon, il **remet la version précédente tout seul**. La page suit
  le redémarrage et se recharge ; l'issue reste dans l'Activité
  (`console-update`) et le journal de l'agent dans
  `/var/log/harvester-ops/update-*.log`.

Le redémarrage rend l'interface indisponible quelques secondes ; les clusters
ne sont pas touchés. L'installation est refusée tant que des actions tournent
(elles s'arrêteraient avec le redémarrage), sauf confirmation.

**Confiance.** L'agent n'installe qu'une archive signée par une clé de
`/opt/harvester-ops/update-signers` (livré avec la version installée), ou de
`/etc/harvester-ops/update-signers` si vous en écrivez un (il prévaut alors :
y mettre votre propre clé pour signer vos propres constructions). Une archive
non signée n'est acceptée que si root a écrit `allow_unsigned=true` dans
`/etc/harvester-ops/update.conf`. C'est la signature qui protège l'hôte :
l'agent exécute en root l'installeur de la version.

**Première fois.** Une console d'avant la 1.82.0 n'a pas d'agent : installer
la 1.82.0 avec `sudo ./install.sh` comme avant, ou `sudo ./install.sh
--upgrade` (non interactif, garde la configuration). Les versions suivantes
s'installent depuis l'interface. Une console lancée depuis les sources n'a pas
d'agent ; l'onglet le dit.

**À la main.** `sudo ./install.sh --upgrade` depuis une version extraite fait
la même installation sans l'interface ; `sudo harvester-ops-update.py
--status` montre la dernière issue de l'agent.

## Déplacer harvops vers un autre hôte

La console sépare deux répertoires, et les copier tous les deux suffit à
la déplacer :

- **La configuration de l'opérateur**, `/etc/harvester-ops/`, en lecture
  seule pour le service : `config.yaml`, `env`, `htpasswd`, et les
  kubeconfigs et clés SSH que l'opérateur y a posés pour les clusters de
  `config.yaml`.
- **L'état de la console**, `/var/lib/harvester-ops/`
  (`HARVESTER_OPS_STATE_DIR`) : tout ce que la console écrit d'elle-même.
  Les comptes de la console (`accounts.json`), les notes, l'historique des
  actions (`actions.db`), les clusters déclarés depuis la console
  (Paramètres > Clusters, installations bare-metal) avec leurs clés et
  kubeconfigs (`clusters.d/<nom>.yaml`, `ssh/`, `kubeconfigs/`, fichiers
  en 0600 dans des répertoires en 0700), les Rancher réglés dans
  l'interface (`rancher.d/`, 1.79.0), et les magasins (exports,
  archives VDDK, paquets Cluster API, inventaires de découverte).

Les chemins écrits dans `clusters.d/` et `rancher.d/` sont relatifs au répertoire d'état :
la copie fonctionne sur un autre hôte ou sous un autre chemin sans rien
réécrire. Un chemin absolu écrit par l'opérateur dans `config.yaml` reste
tel quel : garder les fichiers qu'il désigne au même endroit.

```bash
# ancien hôte
sudo systemctl stop harvester-ops
sudo tar -C / -cpzf harvops-move.tgz etc/harvester-ops var/lib/harvester-ops
# nouvel hôte, après install.sh de la même version
sudo systemctl stop harvester-ops
sudo tar -C / -xpzf harvops-move.tgz
sudo chown -R harvester-ops:harvester-ops /var/lib/harvester-ops
sudo systemctl start harvester-ops
```

Un cluster déclaré dans `config.yaml` l'emporte sur une déclaration de la
console du même nom (celle-ci est ignorée, avec un avertissement dans le
journal). Dans le service packagé, la console ne peut pas écrire
`config.yaml` : ses clusters portent une pastille `config.yaml` dans
Paramètres > Clusters et se modifient dans ce fichier ; les clusters
déclarés depuis la console y restent modifiables.

## Désinstallation

```bash
sudo /opt/harvester-ops/uninstall.sh
```

Supprime les binaires, l'unité systemd, l'image conteneur. Préserve `/etc/harvester-ops/`, `/var/log/harvester-ops/`, `/var/lib/harvester-ops/` et le compte `harvester-ops` sauf si `--purge` est passé.
