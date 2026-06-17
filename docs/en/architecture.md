# Architecture

```
Operator workstation
└─ harvester-ops container  (registry.suse.com/bci/python:3.11)
   │
   └─ Flask console  (sidebar tabs · action dock · SSE live feed · multi-cluster)
        │
        ├──▶ bin/harvester-{shutdown,startup,status}.sh  — power-sequencing engine (SSE)
        ├──▶ kubectl  — VM lifecycle, topology, status
        ├──▶ CAPHV / clusterctl  — downstream RKE2 clusters
        ├──▶ terraform  — declarations (IaC)
        └──▶ Redfish  — BMC discovery + power

   auth     ▶ Basic auth + TLS (browser) · kubectl + ssh (clusters) · redfish (BMCs)
   reaches  ▶ Harvester clusters (prod, staging, …): control-plane + workers,
              Harvester + Longhorn + KubeVirt
```

## Two kinds of operation

harvester-ops drives clusters through **two paths**, and the distinction
matters for trust and for airgap:

1. **Power sequencing** goes through the **bash scripts only**. The
   console spawns them; it never reimplements shutdown/startup logic. This
   is the auditable, UI-independent core.
2. **Everything else** (VM lifecycle, topology, Cluster API, Terraform,
   bare-metal) is the Flask console talking to **`kubectl` / providers /
   Redfish directly**. These surfaces exist only in the console.

So the strong guarantee — *"the UI never bypasses the scripts"* — applies
to **power sequencing**, the one place where getting the order wrong can
lose data. The higher-level surfaces are conventional API clients.

## Component responsibilities

### Bash scripts (`bin/`)

- The **sole execution path for power sequencing**. The console never
  bypasses them for shutdown/startup.
- Self-contained: depend only on `bash`, `kubectl`, `ssh`, `yq`,
  `python3` (a few jq-style transforms).
- Emit structured `STEP_EVENT|<id>|<status>|<msg>` lines on stderr for the
  console to parse into SSE events.
- Read configuration from `/etc/harvester-ops/config.yaml`.

### Common library (`bin/lib/common.sh`)

- Logging (colored TTY + plain log file).
- `confirm()` / `interactive_pause()` for human-in-the-loop mode.
- `run` wrapper honouring `--dry-run`.
- Cluster loader (parses YAML, sets `KUBECONFIG`, SSH options, node list).
- VM helpers: `get_ordered_vms`, `snapshot_vm`, `wait_for_vm_ready`,
  `set_vm_priority`.
- `emit_event()` for SSE-compatible step events.

### Flask console (`web/`)

- **One Flask app per container**, serving all configured clusters.
- **Power-sequencing surface**: spawns the bash scripts via
  `subprocess.Popen` and streams their stderr through SSE.
- **Direct-API surfaces**: VM lifecycle, cluster topology and status,
  Cluster API / CAPHV install + cluster CRUD, Terraform declarations,
  BMC / Redfish — implemented as `kubectl` / provider / Redfish calls.
- **Action model**: every mutating call becomes an `ActionRun` with a
  live event log, surfaced in the persistent dock and retained in a
  SQLite history (WAL mode).
- **Collaborative notes**: a WebSocket sync endpoint backs the Yjs notes,
  persisted in SQLite.
- **Observability**: Prometheus `/metrics`, `/healthz` (liveness),
  `/healthz/ready` (readiness).
- **State**: only the action history and notes are persisted; the app is
  restartable at any time. Auth is HTTP Basic (htpasswd) over TLS; OIDC
  is a candidate for a future version.

### Container image

- Base: `registry.suse.com/bci/python:3.11`.
- Includes `kubectl`, `yq`, `openssh-clients`, the Python wheels
  (offline), the bash scripts, and the console.
- Built via `container/Containerfile`, saved as an OCI tar
  (`images/harvester-ops-ui.tar`) for airgap delivery.
- CAPHV bundles and the Terraform provider binary are **supplied
  separately** (they enable the optional Cluster API / Terraform
  surfaces); the base image does not embed them.

## Multi-cluster model

A single `config.yaml` declares N clusters. Each CLI command
(`--cluster <name>`) loads exactly one cluster's context. The console
sidebar lists every cluster; switching only changes the active context —
there is no cross-cluster fan-out operation.

## SSE event stream

When the console triggers any action (a shutdown here):

```
client                          Flask                       bash script
  │ POST /api/action              │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ subprocess.Popen             │
  │                               │ ──────────────────────────▶  │
  │ GET /api/stream/<id> (SSE)    │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ read stderr line-by-line     │
  │                               │ ◀──── "STEP_EVENT|vm-stop|   │
  │                               │        running|..."          │
  │ event: step                   │                              │
  │ data: {"id":"vm-stop",..}     │                              │
  │ ◀───────────────────────────  │                              │
```

The frontend (vanilla JS + an auto-reconnecting `EventSource` wrapper)
updates step indicators in real time without polling. Direct-API actions
(VM ops, Terraform apply, CAPHV install) use the same `ActionRun` + SSE
mechanism, streaming step/log events as they progress.

## Trust model

- The container holds full `cluster-admin` access via mounted kubeconfigs
  and SSH keys, and can reach BMCs over Redfish.
- It should run on a **trusted operator workstation** or a dedicated jump
  host.
- Path params that map to Kubernetes names are validated (RFC 1123) at the
  HTTP boundary; mutating endpoints carry auth + rate-limiting; kubeconfigs
  are staged with `0600` permissions per Terraform workspace.
- Basic auth + TLS protect the console. For higher trust, place it behind
  a reverse proxy with mTLS or OIDC.
