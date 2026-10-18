# Load bench: many simulated clusters (kwok)

Measures what the console costs with many clusters, many VMs and many people,
without real machines. [kwok](https://github.com/kubernetes-sigs/kwok) runs a
real `kube-apiserver` and `etcd` per simulated cluster, with no node and no VM
behind: that is exactly what the console reads. Gestures that act on machines
(power, migration) are not covered.

## Setup

```bash
B=~/.local/share/kwok-bench; mkdir -p $B/bin
V=v0.8.0   # or the latest release
for b in kwok kwokctl; do
  curl -sL -o $B/bin/$b https://github.com/kubernetes-sigs/kwok/releases/download/$V/${b}-linux-amd64
  chmod +x $B/bin/$b
done
python3 tests/bench/kwok/kwok_bench.py templates --from ~/.kube/harvester.yaml
python3 tests/bench/kwok/kwok_bench.py up 30 --vms 200     # 30 clusters x 200 VMs
```

`templates` copies the definitions (CRDs) of KubeVirt, Harvester and Longhorn
and one object of each kind from a real cluster (cloud-init user data is
dropped). `up` creates the clusters one at a time (kwokctl in parallel mixes up
certificate authorities), fills them in parallel (10 namespaces, VMs with their
VMI, volume claim and Longhorn volume, images, networks, a node), and writes a
console configuration in `$B/config.yaml`. About 500 MB of memory per cluster.

## Run

Start a console on that configuration, open (no sign-in) and without the rate
limiter, then drive it:

```bash
cd ~/workspace/HARVESTER-OPS && HARVESTER_OPS_AUTH=none HARVESTER_OPS_DISABLE_RATELIMIT=1 \
  HARVESTER_OPS_CONFIG=$B/config.yaml HARVESTER_OPS_STATE_DIR=$B/state \
  HARVESTER_OPS_ACTIONS_DB=$B/state/actions.db HARVESTER_OPS_NOTES_DB=$B/state/notes.db \
  HARVESTER_OPS_LOG_DIR=$B/logs python3 web/app.py &
python3 tests/bench/kwok/load.py --base http://127.0.0.1:8135 --users 30 --duration 60 --pid <console pid>
```

Each simulated person has the overview of one cluster open (person `i` on
cluster `i mod N`, so nothing is shared between them): activity dock every
second, VM list, overview, topology and usage every 5 seconds. `load.py`
reports the latency per screen and the CPU of the console and of the
processes it starts (kubectl, scripts), in cores. `--users 0` measures the
cluster watchers alone.

`python3 tests/bench/kwok/kwok_bench.py down` removes the clusters.

## Caveat

All simulated API servers run on the bench host: past a few dozen people they
compete with the console for CPU, which a real deployment does not do (each
cluster has its own API server). Watch the host load (`/proc/loadavg`) and
read the results with that in mind.

## Simulated GPU cluster (inventory and bookings without hardware)

`gpu_demo.py` creates one kwok cluster, `gpudemo`, that carries the CRDs of
Harvester's pcidevices controller (copied from a real Harvester) and sample
GPU objects shaped like the real ones: a whole H100 passed through to a VM,
an H100 split into vGPUs (one held by a VM, two disabled), an A100 partitioned
in MIG with a MIG-backed vGPU held by a VM, and a node serving four GPUs to
containers, two of them taken by a Deployment. It is declared in the dev
console's state (`clusters.d/`), never in an operator configuration.

```bash
python3 tests/bench/kwok/gpu_demo.py prepare --from ~/.kube/harvester.yaml
python3 tests/bench/kwok/gpu_demo.py up      # then restart the dev console
python3 tests/bench/kwok/gpu_demo.py down
```

Nothing runs behind it: assigning a GPU to a VM or applying a booking for real
is not exercised by this cluster.
