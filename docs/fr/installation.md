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
- Créer `/etc/harvester-ops/` (config, htpasswd, TLS, dossier clés ssh)
- Copier `config/config.yaml.example` → `/etc/harvester-ops/config.yaml` (si absent)
- Charger `images/harvester-ops-ui.tar` dans podman/docker
- Installer `config/systemd/harvester-ops.service` (si UI sélectionnée)

### 3. Fournir les kubeconfigs et clés SSH

Pour chaque cluster à gérer :

```bash
sudo cp /chemin/vers/prod-kubeconfig.yaml /etc/harvester-ops/kubeconfigs/prod.yaml
sudo chmod 600 /etc/harvester-ops/kubeconfigs/*.yaml
sudo cp /chemin/vers/id_ed25519 /etc/harvester-ops/ssh/id_ed25519
sudo chmod 600 /etc/harvester-ops/ssh/id_ed25519
```

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

## Désinstallation

```bash
sudo /opt/harvester-ops/uninstall.sh
```

Supprime les binaires, l'unité systemd, l'image conteneur. Préserve `/etc/harvester-ops/` et `/var/log/harvester-ops/` sauf si `--purge` est passé.
