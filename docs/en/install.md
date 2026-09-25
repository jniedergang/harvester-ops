# Installation guide

## Prerequisites on the operator host

| Component | Version | Why |
|---|---|---|
| OS | SUSE / openSUSE | Supported targets (uses `zypper` for dependencies) |
| `bash` | ≥ 4.0 | Scripts |
| `kubectl` | matching cluster minor version | API client |
| `ssh` | OpenSSH 7+ | Node shutdown |
| `yq` | ≥ 4.0 (mikefarah) | Config parsing |
| `python3` | ≥ 3.9 | JSON helpers + Flask UI |
| `podman` | recent | Required only if you install the web UI |

One-liner to install dependencies:

```bash
sudo zypper install -y bash openssh-clients kubectl yq python3 podman
```

## Install steps

### 1. Receive and verify the tarball

```bash
sha256sum -c harvester-ops-1.0.0.tar.gz.sha256
tar xzf harvester-ops-1.0.0.tar.gz
cd harvester-ops-1.0.0
```

### 2. Run the installer

```bash
sudo ./install.sh
```

The installer is **interactive**. It will ask:

- Install the web UI? (default: yes)
- HTTP Basic auth username and password (only if UI)
- TLS: generate a self-signed certificate? (default: yes)
- Web UI bind port (default: 8090)
- systemd service for the UI? (default: yes)

The installer will:

- Copy `bin/*` to `/usr/local/bin/`
- Create the `harvester-ops` system account: the web UI container runs as
  this account, never as root
- Create `/etc/harvester-ops/` (config, htpasswd, TLS, ssh keys directory),
  readable by the `harvester-ops` group and by no other account
- Create `/var/lib/harvester-ops/` (action history, notes, cluster watcher
  snapshot) and `/var/log/harvester-ops/`, owned by that account
- Copy `config/config.yaml.example` → `/etc/harvester-ops/config.yaml` (if not present)
- Load `images/harvester-ops-ui.tar` into podman/docker
- Install `config/systemd/harvester-ops.service` (if UI was selected)

### 3. Provide kubeconfigs and SSH keys

For each cluster you intend to manage:

```bash
sudo install -m 0640 -g harvester-ops /path/to/prod-kubeconfig.yaml /etc/harvester-ops/kubeconfigs/prod.yaml
sudo install -m 0640 -g harvester-ops /path/to/id_ed25519 /etc/harvester-ops/ssh/id_ed25519
```

The web UI reads these files as the `harvester-ops` account, through its
group. Do not make them root-only (`chmod 600`): the service reapplies
group read access at each start, so a file copied another way is fixed by
`sudo systemctl restart harvester-ops`. SSH accepts a key owned by root and
readable by the group.

### 4. Edit the config

```bash
sudo $EDITOR /etc/harvester-ops/config.yaml
```

Declare each cluster with its name, kubeconfig path, ssh credentials, and the full list of nodes (hostname, IP, role).

### 5. Test connectivity (read-only)

```bash
harvester-status --cluster prod
```

You should see nodes, VMs, and Longhorn volumes.

### 6. Validate with a dry-run

```bash
harvester-shutdown --cluster prod --dry-run --yes
```

This prints every command that would run, without executing.

### 7. Start the web UI (if installed)

```bash
sudo systemctl enable --now harvester-ops
sudo systemctl status harvester-ops
```

Open `https://<host>:8090` in a browser. Accept the self-signed certificate, log in with the credentials set during install.

### 8. Moving VMs between clusters (optional)

When two clusters do not share a backup target, a VM moves through this
host: the target cluster's nodes fetch each disk over HTTP, on port 8094 by
default. Open it for them, or choose another address and port in
`config.yaml`:

```bash
sudo firewall-cmd --permanent --add-port=8094/tcp && sudo firewall-cmd --reload
```

```yaml
transfer:
  serve_address: 10.0.0.5:8094    # an address the clusters' nodes reach
```

Exported archives are kept in `/var/lib/harvester-ops/exports` (the
service's persistent volume), as are the archives added from a browser
(1.47.0): size that volume for the largest VM you expect to move by file.

## Uninstall

```bash
sudo /opt/harvester-ops/uninstall.sh
```

Removes binaries, systemd unit, container image. Preserves `/etc/harvester-ops/`, `/var/log/harvester-ops/`, `/var/lib/harvester-ops/` and the `harvester-ops` account unless `--purge` is passed.
