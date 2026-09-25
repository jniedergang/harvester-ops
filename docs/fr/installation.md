# Guide d'installation

## Pré-requis sur le poste opérateur

| Composant | Version | Pourquoi |
|---|---|---|
| OS | SUSE / openSUSE | Cibles supportées (dépendances installées via `zypper`) |
| `bash` | ≥ 4.0 | Scripts |
| `kubectl` | version mineure du cluster | Client API |
| `ssh` | OpenSSH 7+ | Extinction des nodes |
| `yq` | ≥ 4.0 (mikefarah) | Parsing config |
| `python3` | ≥ 3.9 | Helpers JSON + UI Flask |
| `podman` | récent | Requis uniquement pour l'UI web |

One-liner pour installer les dépendances :

```bash
sudo zypper install -y bash openssh-clients kubectl yq python3 podman
```

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
- Nom d'utilisateur et mot de passe HTTP Basic (uniquement si UI)
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

### 9. Connexion par Rancher (facultatif, 1.50.0)

Les personnes déjà connectées à Rancher Manager (2.12 et suivants) entrent
dans la console sans rien saisir, avec les droits que Rancher leur donne sur
chaque cluster. Déclarer la console dans Rancher, sur son cluster local :

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

## Désinstallation

```bash
sudo /opt/harvester-ops/uninstall.sh
```

Supprime les binaires, l'unité systemd, l'image conteneur. Préserve `/etc/harvester-ops/`, `/var/log/harvester-ops/`, `/var/lib/harvester-ops/` et le compte `harvester-ops` sauf si `--purge` est passé.
