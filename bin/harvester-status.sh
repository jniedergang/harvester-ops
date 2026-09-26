#!/usr/bin/env bash
# harvester-status.sh — Read-only status snapshot of a Harvester cluster
# Used both as a CLI tool and by the web UI (JSON output mode)

set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

OUTPUT="text"  # text | json
NAMESPACE=""   # filter by namespace

usage() {
cat <<'EOF'
Usage: harvester-status.sh --cluster <name> [options]

Options:
  -c, --cluster <name>     Cluster name
  -o, --output <fmt>       text | json (default: text)
  -N, --namespace <ns>     Filter VM listing by namespace
      --config <path>      Config file
  -h, --help               Show this help
EOF
}

parse_common_args "$@"
set -- "${POSITIONAL[@]}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -o|--output) OUTPUT="$2"; shift 2 ;;
        -N|--namespace) NAMESPACE="$2"; shift 2 ;;
        *) log_error "Argument inconnu : $1"; usage; exit 1 ;;
    esac
done

[[ -z "$CLUSTER_NAME" ]] && { log_error "--cluster requis"; usage; exit 1; }
# Suppress stdout (load_cluster prints a confirmation that would corrupt the
# JSON output expected by the UI) but KEEP stderr so genuine errors
# (missing yq, missing kubeconfig, unknown cluster) surface to the Flask
# subprocess's stderr — without this, the UI would just see an empty
# "status failed" with no clue what went wrong (the "Overview shows
# nothing" recurring bug class).
load_cluster "$CLUSTER_NAME" >/dev/null

if [[ "$OUTPUT" == "json" ]]; then
    # Emit a compact JSON the web UI can consume
    python3 - "$KUBECONFIG_PATH" "$NAMESPACE" <<'PY'
import json, subprocess, sys, os
from concurrent.futures import ThreadPoolExecutor

kubeconfig, ns_filter = sys.argv[1], sys.argv[2]
env = os.environ.copy()
env["KUBECONFIG"] = kubeconfig

denied = []


def kc(*args):
    try:
        r = subprocess.run(["kubectl", "--kubeconfig", kubeconfig, *args],
                           env=env, capture_output=True, text=True)
    except OSError:
        return {"items": []}
    if r.returncode != 0:
        return {"items": [], "_err": r.stderr}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"items": []}


def kc_kinds(scope, kinds):
    """Un `get` groupé. Un seul type refusé par la RBAC fait échouer le lot :
    un membre du cluster, qui lit les nœuds mais pas les VMs, voyait alors
    « 0 nœud » (vu en réel, v1.56.0). On relit donc type par type ce qui est
    permis, et l'on retient les refus pour que l'écran les dise."""
    out = kc("get", *scope, ",".join(kinds), "-o", "json")
    if "_err" not in out:
        return out
    items = []
    for kind in kinds:
        one = kc("get", *scope, kind, "-o", "json")
        if "_err" not in one:
            items += one.get("items", [])
            continue
        line = next((x for x in one["_err"].splitlines() if "forbidden" in x.lower()), "")
        if line:
            denied.append(line.strip()[:300])
    return {"items": items}


def by_kind(payload):
    """Objets d'un `get a,b,c` rangés par Kind. Chaque objet porte le sien."""
    out = {}
    for item in payload.get("items", []):
        out.setdefault(item.get("kind"), []).append(item)
    return out


# Cinq `kubectl get` séparés coûtaient 6,4 s mesurés sur harv1, dont ~0,7 s de
# démarrage de processus CHACUN avant même de toucher au réseau. Regroupés par
# portée (cluster, puis longhorn-system) et lancés de front : ~2,7 s. La page
# d'aperçu se rafraîchit toutes les 8 s, donc l'ancien coût ne rentrait pas
# dans son propre intervalle.
with ThreadPoolExecutor(max_workers=2) as ex:
    f_core = ex.submit(kc_kinds, ["-A"], ["nodes", "vm", "vmi"])
    f_lh = ex.submit(kc_kinds, ["-n", "longhorn-system"],
                     ["volumes.longhorn.io", "settings.longhorn.io"])
    core = by_kind(f_core.result())
    lh = by_kind(f_lh.result())

result = {"nodes": [], "vms_by_namespace": {}, "longhorn": {}, "summary": {}}

# Nodes
for n in core.get("Node", []):
    name = n["metadata"]["name"]
    conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
    ready = conds.get("Ready", "Unknown")
    schedulable = not n.get("spec", {}).get("unschedulable", False)
    result["nodes"].append({
        "name": name,
        "ready": ready,
        "schedulable": schedulable,
        "roles": [k.split("/")[-1] for k in n["metadata"].get("labels", {}) if k.startswith("node-role.kubernetes.io/")],
    })

# VMs grouped by namespace
vmi_phase = {(v["metadata"]["namespace"], v["metadata"]["name"]):
             v.get("status", {}).get("phase", "Unknown")
             for v in core.get("VirtualMachineInstance", [])}

for vm in core.get("VirtualMachine", []):
    ns = vm["metadata"]["namespace"]
    name = vm["metadata"]["name"]
    if ns_filter and ns != ns_filter:
        continue
    rs = vm["spec"].get("runStrategy", "?")
    phase = vmi_phase.get((ns, name), "Stopped")
    result["vms_by_namespace"].setdefault(ns, []).append({
        "name": name,
        "runStrategy": rs,
        "phase": phase,
    })

# Longhorn
try:
    states = {}
    for v in lh.get("Volume", []):
        st = v.get("status", {}).get("state", "unknown")
        states[st] = states.get(st, 0) + 1
    result["longhorn"]["volumes_by_state"] = states
    # Le réglage est pris dans le lot plutôt que demandé par son nom : un
    # appel de moins, et la valeur est la même.
    limit = next((x.get("value") for x in lh.get("Setting", [])
                  if x.get("metadata", {}).get("name")
                  == "concurrent-replica-rebuild-per-node-limit"), "?")
    result["longhorn"]["concurrent_rebuild_limit"] = limit
except Exception:
    result["longhorn"] = {"installed": False}

# Summary
result["summary"]["nodes_total"] = len(result["nodes"])
result["summary"]["nodes_ready"] = sum(1 for n in result["nodes"] if n["ready"] == "True")
result["summary"]["vms_running"] = sum(1 for ns in result["vms_by_namespace"].values()
                                       for v in ns if v["phase"] == "Running")
result["summary"]["vms_total"] = sum(len(v) for v in result["vms_by_namespace"].values())
if denied:
    result["denied"] = denied

print(json.dumps(result, indent=2))
PY
    exit 0
fi

# Text output
cat <<EOF

${C_BOLD}${C_CYAN}═══ Cluster: $CLUSTER_NAME ═══${C_RESET}

${C_BOLD}Nodes:${C_RESET}
EOF
kc_quiet get nodes -o wide || true

cat <<EOF

${C_BOLD}VMs (top 20):${C_RESET}
EOF
if [[ -n "$NAMESPACE" ]]; then
    kc_quiet get vm -n "$NAMESPACE" 2>/dev/null | head -n 21 || echo "  (aucune)"
else
    kc_quiet get vm -A 2>/dev/null | head -n 21 || echo "  (aucune)"
fi

cat <<EOF

${C_BOLD}Longhorn volumes:${C_RESET}
EOF
if kc_quiet get crd volumes.longhorn.io >/dev/null 2>&1; then
    kc_quiet -n longhorn-system get volumes.longhorn.io --no-headers 2>/dev/null | \
        awk '{print $2}' | sort | uniq -c || echo "  (aucun)"
else
    echo "  (Longhorn non installé)"
fi
echo
