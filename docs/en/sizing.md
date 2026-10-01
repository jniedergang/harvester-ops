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
- **Cluster watchers are spread out.** Each cluster's watcher starts at its own
  offset within the first interval, and every cycle varies by up to 10 %, so
  ten clusters are not read in the same second every 15 seconds.

Settings (environment of the service):

| Variable | Default | Effect |
|---|---|---|
| `HARVESTER_OPS_READ_SHARE` | `1` | `0` turns shared reads off |
| `HARVESTER_OPS_READ_SHARE_TTL` | `3` | seconds a shared read serves |
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

## Recommended resources

| Scale | Console host |
|---|---|
| Up to 10 clusters, a few hundred VMs, up to 60 people | 2 vCPU, 2 GiB |
| 10 to 30 clusters | 4 vCPU, 4 GiB |

The number of people no longer drives the cost; the number of clusters and
the number of objects per cluster do (each watcher reads its cluster every
15 seconds while someone uses the console). Beyond 30 clusters or several
thousand VMs, raise `HARVESTER_OPS_WATCH_INTERVAL` and measure: those scales
have not been load-tested yet.
