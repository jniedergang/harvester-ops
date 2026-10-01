# Sizing and performance

What the console costs, measured, and what it does to stay flat when many
people use it at once.

## How the console reads the clusters

- **Shared reads (1.81.0).** The screens that refresh on their own (overview,
  VM list, topology, usage, networks, storage, devices, upgrade, add-ons,
  migrations, activity...) are read once for everyone: while a read of a given
  view is running, the other requests for the same view wait for its result
  instead of starting their own `kubectl`, and that result serves for 3 more
  seconds (1 second for the activity dock). Two people only share a read when
  they present the same identity to the cluster (same kubeconfig, so the same
  RBAC) and have the same role in the console.
- **Writes are never hidden.** Any write request (POST, PUT, PATCH, DELETE)
  forgets the shared reads of the cluster it names, and so does the end of any
  action. A read that was running when a write arrived is not reused.
  `?fresh=1` on a view always reads again.
- **Configuration files are parsed once.** `config.yaml` and the cluster
  declarations are kept parsed until the file changes (date, size or inode),
  with libyaml when it is installed. They used to be parsed several times per
  request, which was most of the console's CPU.
- **Reads without kubectl (1.83.0).** Measured on 30 simulated clusters of
  200 VMs, 94 % of the console's CPU went into the `kubectl get -o json` it
  starts (each call decodes then re-encodes the whole list). The console and
  its scripts now make those reads directly against the Kubernetes API, with
  the same result as kubectl (a `List` whose objects carry their `kind`, the
  object alone for a name, and kubectl's own error text when the cluster
  refuses), connections kept open. Anything else still goes through kubectl:
  writes, other output formats, kubeconfigs that authenticate through an
  `exec` plugin or a proxy.
- **Read workers (1.83.0).** Once reads no longer go through kubectl, the
  heaviest screens (topology, VM list, storage map, network fabric) are built
  in a few separate worker processes, each on its own core, instead of queuing
  behind the single Python process. The main process keeps deciding who you
  are, your role and the identity presented to the cluster; a worker only
  computes the screen and returns it with any refusal from the cluster. A
  worker restarts after 200 screens to bound its memory, and stops with the
  console.
- **Cluster watchers are spread out.** Each cluster's watcher starts at its own
  offset within the first interval, and every cycle varies by up to 10 %, so
  ten clusters are not read in the same second every 15 seconds.

Settings (environment of the service):

| Variable | Default | Effect |
|---|---|---|
| `HARVESTER_OPS_READ_SHARE` | `1` | `0` turns shared reads off |
| `HARVESTER_OPS_READ_SHARE_TTL` | `3` | seconds a shared read serves |
| `HARVESTER_OPS_KUBE_REST` | `1` | `0` sends every read back through kubectl |
| `HARVESTER_OPS_READ_WORKERS` | 4 (fewer below 5 cores, 0 below 3) | read worker processes; `0` builds every screen in the main process |
| `HARVESTER_OPS_READ_WORKER_TASKS` | `200` | screens a worker builds before it restarts |
| `HARVESTER_OPS_WATCH_INTERVAL` | `15` | seconds between two watcher cycles |
| `HARVESTER_OPS_WATCH_IDLE_AFTER` / `_IDLE_INTERVAL` | `300` / `120` | slower watcher when nobody uses the console |

## Measured (1 October 2026)

One console process, one cluster (Harvester 1.9, 14 VMs), simulated people
each with the overview open: activity dock every second, VM list, overview,
topology and usage every 5 seconds. Latency at the median (p50) and 95th
percentile (p95):

| People | Before 1.81.0: overview p50 / p95 | 1.81.0: overview p50 / p95 | Console CPU before / after |
|---|---|---|---|
| 1 | 0.63 s / 0.65 s | 0.59 s / 0.60 s | 9 % / 5 % |
| 10 | 1.0 s / 1.8 s | 0.65 s / 0.92 s | 28 % / 5 % |
| 30 | 2.5 s / 4.1 s | 0.72 s / 0.96 s | 47 % / 9 % |
| 60 | 5.5 s / 6.3 s | 0.54 s / 0.87 s | 62 % / 16 % |

At 60 people the VM list went from 4.8 s to 0.17 s and the usage gauges from
7 s to 0.28 s. Memory stayed under 140 MB.

## Measured with many clusters (1.83.0)

Simulated clusters (`tests/bench/kwok`, real API servers with no machine
behind), 200 VMs each, one person per cluster so nothing is shared between
people. Median / 95th percentile of the topology and VM list screens, and the
CPU of the console with every process it starts:

| Clusters, people | Before 1.83.0 | 1.83.0 |
|---|---|---|
| 30 clusters, watchers only | 1.9 cores | 0.15 core |
| 30 clusters, 30 people | topology 5.4 / 8.1 s, VMs 4.4 / 7.2 s, 10.3 cores | topology 1.3 / 3.7 s, VMs 0.8 / 2.7 s, 3.4 cores |
| 60 clusters, watchers only | 3.5 cores | 0.3 core |
| 60 clusters, 30 people | topology 7.0 / 9.8 s, VMs 7.3 / 11.4 s, 9.6 cores | topology 1.8 / 2.9 s, VMs 1.6 / 3.2 s, 3.7 cores |
| 60 clusters, 60 people | topology 16.5 / 21.1 s, VMs 11.9 / 20.2 s, 9.7 cores | topology 6.3 / 8.9 s, VMs 5.6 / 7.6 s, 3.7 cores |

The console process itself stays under 450 MB; each read worker takes about
175 MB. The 60-people run was limited by the bench host: the 60 simulated API
servers ran on the same machine as the console (load above 40 on 28 cores),
which a real deployment does not do, and raising the workers from 4 to 8
changed nothing. Those figures are an upper bound, not the console's limit.

## Recommended resources

| Scale | Console host |
|---|---|
| Up to 10 clusters, a few hundred VMs, up to 60 people | 2 vCPU, 2 GiB (read workers off below 3 cores) |
| Up to 30 clusters, a few thousand VMs, 30 people | 4 vCPU, 4 GiB |
| Up to 60 clusters, about 12,000 VMs, 30 people | 6 vCPU, 6 GiB |

Each read worker adds about 175 MB. Beyond 60 clusters, or with many people
each on a different cluster, measure with the bench before committing to a
figure; raising `HARVESTER_OPS_WATCH_INTERVAL` lowers the constant load of the
watchers.
