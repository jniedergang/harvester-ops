# Installation guide

## Prerequisites on the operator host

| Component | Version | Why |
|---|---|---|
| OS | SUSE / openSUSE | Supported targets (uses `zypper` for dependencies) |
| `bash` | ≥ 4.0 | Scripts |
| `kubectl` | matching cluster minor version | API client |
| `ssh` / `ssh-keygen` | OpenSSH 7+; `ssh-keygen -Y` (8.2+) for updates, otherwise the console image checks the signature (1.87.2) | Node shutdown + release signature verification |
| `yq` | ≥ 4.0 (mikefarah) | Config parsing |
| `python3` | ≥ 3.9 | JSON helpers + Flask UI |
| `podman` | recent | Required only if you install the web UI |

One-liner to install dependencies:

```bash
sudo zypper install -y bash openssh-clients kubectl yq python3 podman
```

The update agent runs on the host, outside the UI container. The installer
selects a Python >= 3.9 (`python3`, or a versioned `python3.9`–`python3.14`)
and pins its absolute path in `harvester-ops-update.service`. It refuses to
install the agent if none is available, without changing the system Python
alternative. A current Python inside the container does not satisfy this
host requirement.

When the host's `ssh-keygen` does not know `-Y` (OpenSSH before 8.2, as on
RHEL 8), the update agent has the signature checked by the `ssh-keygen` of
the installed console image (1.87.2): a throwaway container with no network
and a read-only file system, which sees only the signature and the trusted
keys and reads the archive on its standard input. Nothing is downloaded and
the check stays mandatory.

Existing installations with an older interpreter in the updater unit need a
one-time host repair before they can install this fix through the UI. Set
`ExecStart` in a systemd override to a supported host interpreter (for example
`/usr/bin/python3.12 /usr/local/bin/harvester-ops-update.py`, preceded by an
empty `ExecStart=`), run `systemctl daemon-reload`, then retry from the UI.

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
- The first account of the console, user name and password (only if UI):
  it administers the console (1.57.0; it used to land as a viewer)
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

The installer also places the embedded Cluster API bundle and Terraform
provider in `/var/lib/harvester-ops` (1.51.0): the Cluster API and
Terraform tabs work without anything to download. A bundle or provider you
choose later from the console is kept by later installations.

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

### 8b. Signing in (1.57.0)

The console always asks who you are: there is no open mode any more
(`HARVESTER_OPS_AUTH=none` restores it for tests only, and only while no
account exists). People sign in on the sign-in page with a user name and a
password; the session is an HttpOnly cookie, and "Sign out" in the account
menu (top right) ends it. Scripts and API clients keep using HTTP Basic.

- **Accounts**: the installer's account (htpasswd) and the accounts created
  in Settings > Console accounts, kept in
  `/var/lib/harvester-ops/accounts.json` (bcrypt hashes, mode 0600). An
  administrator creates them, sets their role (viewer, operator,
  administrator), resets a password or deletes them; everyone changes their
  own password from the account menu. Each change is recorded in Activity.
- **First start without any account** (no htpasswd, no Rancher): every page
  leads to `/setup`, which creates the first administrator. It asks for a
  token written to `/var/lib/harvester-ops/setup-token` (mode 0600, the
  service log gives the path): only who administers the server can read it,
  so nobody reaching the port first can take the console.

### 9. Uploading images from a browser (optional, 1.63.0)

An image file sent from a browser is kept by the console, then offered once
to the cluster, whose nodes download it over HTTP from this host on port
**8092** by default. Open it for them, or choose another port in
`/etc/harvester-ops/env`:

```bash
sudo firewall-cmd --permanent --add-port=8092/tcp && sudo firewall-cmd --reload
echo HARVESTER_OPS_IMAGE_UPLOAD_PORT=8192 | sudo tee -a /etc/harvester-ops/env   # another port
```

The file waits in `/var/lib/harvester-ops/image-uploads` and is deleted once
the image is imported (or the action cancelled).

### 10. Sign in through Rancher (optional, 1.50.0, set in the interface since 1.79.0)

People with a Rancher Manager account (2.12 or later) sign in to the
console with it, and get the rights Rancher gives them on each cluster.

**From the interface (1.79.0)**: Settings > Sign-in through Rancher, as a
console administrator. Add a Rancher (a label, its `https://` address, and
its certificate authority in PEM when Rancher's certificate is not signed
by a public authority; "skip TLS verification" exists for a lab), then
**Test**: the console shows Rancher's version and its authentication
providers. Several Rancher can be set; changes apply at once, without a
restart.

- **Direct sign-in** (on by default): the sign-in page asks for the Rancher
  user name and password, for the providers that take a password (local
  users, LDAP, OpenLDAP, Active Directory, FreeIPA). Nothing has to be
  declared in Rancher. The console gets a Rancher token for that person,
  valid for the session length (1 to 24 hours), uses it exactly like the
  single sign-on token, and deletes it in Rancher at sign-out. The session
  is not renewed: it ends with the token. Providers without a password
  (OIDC such as Keycloak, SAML, GitHub) go through single sign-on.
- **Single sign-on** (optional): **Register** asks once for the user name
  and password of a Rancher administrator. The console signs in with them,
  creates its OIDC client in Rancher (with its return address, taken from
  the browser's address), keeps the generated client secret (0600) and
  signs out; the administrator's credentials are not kept. Someone already
  signed in to Rancher then enters without typing anything. **Unregister**
  removes the client from Rancher.
- **Harvester RBAC chart**: the console shows whether the `harvester-rbac`
  chart of the `rancher-charts` catalog is installed in Rancher's `local`
  cluster and installs it (a tracked action, with a Rancher administrator's
  credentials, not kept). It brings the "View/Manage Virtualization
  Resources" roles, for clusters and projects. The console refuses with the
  reason when the chart's Rancher or Kubernetes requirement is not met
  (109.0.0+up0.1.1: Rancher 2.14.x, Kubernetes below 1.36).
- The sign-in page lists the Rancher (the last one chosen comes first),
  never a free address, and the console's own accounts below. A Rancher
  that does not answer within three seconds is shown as unavailable and
  does not hold the page.

These settings live in the console's state directory
(`/var/lib/harvester-ops/rancher.d/<id>.yaml`, the certificate authority and
the client secret beside it, 0600, paths relative to the state directory):
they move with the console (see below).

**From `config.yaml` (1.50.0, still read)**: the section below declares one
Rancher, shown read-only in the interface ("config.yaml" badge). It wins
over a Rancher set in the interface with the same address. Add
`direct_login: true` to offer direct sign-in for it too. For single
sign-on, declare the console in Rancher, on its local cluster:

```bash
cat <<EOF | kubectl apply -f -
apiVersion: management.cattle.io/v3
kind: OIDCClient
metadata:
  name: harvester-ops
spec:
  description: harvester-ops console
  redirectURIs: ["https://console.example.com/auth/rancher/callback"]
  tokenExpirationSeconds: 600
  refreshTokenExpirationSeconds: 43200     # the session length
EOF
CID=$(kubectl get oidcclient harvester-ops -o jsonpath='{.status.clientID}')
kubectl get secret -n cattle-oidc-client-secrets "$CID" \
  -o jsonpath='{.data.client-secret-1}' | base64 -d \
  | sudo install -m 0600 /dev/stdin /etc/harvester-ops/rancher-oidc-secret
```

Then in `config.yaml`:

```yaml
rancher:
  url: https://rancher.example.com
  client_id: client-xxxxxxxx
  client_secret_file: /etc/harvester-ops/rancher-oidc-secret
  redirect_uri: https://console.example.com/auth/rancher/callback
  default_role: operator        # console role of non-administrators
  admin_groups: []              # Rancher group principals made console admins
  session_hours: 12             # keep it at refreshTokenExpirationSeconds
```

The console must reach Rancher, and each cluster must be imported in
Rancher for a Rancher user to see it. Local accounts keep working; starting
and stopping a cluster stays with them.

## Updating harvops (1.82.0)

From 1.82.0 on, the console updates itself from the interface: click the
version number, then the **Update** tab (administrators).

- **Online**: the console reads `release.json` at its update source (the
  project's GitHub releases by default; an internal mirror is any HTTP
  directory serving the same files: `release.json`, the archive and its
  `.sig`). **Check now**, then **Download**: the archive and its signature are
  fetched, the SHA-256 compared with `release.json`, the signature checked.
  The console honours `HTTPS_PROXY`.
- **Offline**: give the archive `harvester-ops-<version>.tar.gz` and its
  `.sig` through the browser (streamed to disk, never held in memory).
- **Install**: the console hands the release to the **update agent** of the
  host (`harvester-ops-update.path` and `.service`, installed by
  `install.sh`). The agent, as root, checks the signature again with the
  host's trusted keys, keeps the current files and image, runs the release's
  own `install.sh --upgrade` (scripts, image, embedded bundles, units; never
  the configuration, accounts or certificates), restarts the console and
  waits for the new version to answer. If it does not, it **puts the previous
  version back by itself**. The page follows the restart and reloads; the
  outcome is kept in Activity (`console-update`) and the agent's log in
  `/var/log/harvester-ops/update-*.log`.

The restart leaves the interface unavailable for a few seconds; the clusters
are not touched. An install is refused while actions are running (they would
stop with the restart) unless you confirm.

**Trust.** The agent installs only an archive signed by a key of
`/opt/harvester-ops/update-signers` (shipped with the installed version), or
of `/etc/harvester-ops/update-signers` when you write one (it then wins: put
your own key there to sign your own builds). An unsigned archive is accepted
only if root wrote `allow_unsigned=true` in `/etc/harvester-ops/update.conf`.
The signature is what protects the host: the agent runs the release's
installer as root.

**First time.** A console older than 1.82.0 has no agent: install 1.82.0 with
`sudo ./install.sh` as before, or `sudo ./install.sh --upgrade` (non
interactive, keeps the configuration). The next versions install from the
interface. A console run from the sources has no agent; the tab says so.

**By hand.** `sudo ./install.sh --upgrade` from an extracted release does the
same install without the interface; `sudo harvester-ops-update.py --status`
shows the agent's last outcome.

## Moving harvops to another host

The console keeps two directories apart, and copying both is all a move
takes:

- **The operator's configuration**, `/etc/harvester-ops/`, read-only for
  the service: `config.yaml`, `env`, `htpasswd`, and the kubeconfigs and
  SSH keys the operator put there for the clusters of `config.yaml`.
- **The console's state**, `/var/lib/harvester-ops/`
  (`HARVESTER_OPS_STATE_DIR`): everything the console writes by itself.
  Console accounts (`accounts.json`), notes, the action history
  (`actions.db`), the clusters declared from the console (Settings >
  Clusters, bare-metal installs) with their keys and kubeconfigs
  (`clusters.d/<name>.yaml`, `ssh/`, `kubeconfigs/`, 0600 files in 0700
  directories), the Rancher set in the interface (`rancher.d/`, 1.79.0),
  and the stores (exports, VDDK archives, Cluster API
  bundles, discovery inventories).

The paths inside `clusters.d/` and `rancher.d/` are relative to the state directory, so
the copy works on another host or under another path without rewriting
anything. Absolute paths written by the operator in `config.yaml` stay as
they are: keep the files they name at the same place.

```bash
# old host
sudo systemctl stop harvester-ops
sudo tar -C / -cpzf harvops-move.tgz etc/harvester-ops var/lib/harvester-ops
# new host, after install.sh with the same release
sudo systemctl stop harvester-ops
sudo tar -C / -xpzf harvops-move.tgz
sudo chown -R harvester-ops:harvester-ops /var/lib/harvester-ops
sudo systemctl start harvester-ops
```

A cluster declared in `config.yaml` wins over a console declaration of the
same name (the latter is ignored, with a warning in the log). In the
packaged service the console cannot write `config.yaml`: its clusters show
a `config.yaml` badge in Settings > Clusters and are changed in that file;
the clusters declared from the console stay editable there.

## Uninstall

```bash
sudo /opt/harvester-ops/uninstall.sh
```

Removes binaries, systemd unit, container image. Preserves `/etc/harvester-ops/`, `/var/log/harvester-ops/`, `/var/lib/harvester-ops/` and the `harvester-ops` account unless `--purge` is passed.
