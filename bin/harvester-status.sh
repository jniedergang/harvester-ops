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

kubeconfig, ns_filter = sys.argv[1], sys.argv[2]
env = os.environ.copy()
env["KUBECONFIG"] = kubeconfig

def kc(*args):
    try:
        out = subprocess.check_output(["kubectl", "--kubeconfig", kubeconfig, *args],
                                      env=env, stderr=subprocess.DEVNULL)
        return json.loads(out)
    except Exception:
        return {"items": []}

result = {"nodes": [], "vms_by_namespace": {}, "longhorn": {}, "summary": {}}

# Nodes
nodes = kc("get", "nodes", "-o", "json")
for n in nodes.get("items", []):
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
vms = kc("get", "vm", "-A", "-o", "json")
vmis = kc("get", "vmi", "-A", "-o", "json")
vmi_phase = {(v["metadata"]["namespace"], v["metadata"]["name"]): v["status"].get("phase", "Unknown")
             for v in vmis.get("items", [])}

for vm in vms.get("items", []):
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
    vols = kc("get", "volumes.longhorn.io", "-n", "longhorn-system", "-o", "json")
    states = {}
    for v in vols.get("items", []):
        s = v.get("status", {}).get("state", "unknown")
        states[s] = states.get(s, 0) + 1
    result["longhorn"]["volumes_by_state"] = states
    settings = kc("get", "settings.longhorn.io", "concurrent-replica-rebuild-per-node-limit",
                  "-n", "longhorn-system", "-o", "json")
    result["longhorn"]["concurrent_rebuild_limit"] = settings.get("value", "?")
except Exception:
    result["longhorn"] = {"installed": False}

# Summary
result["summary"]["nodes_total"] = len(result["nodes"])
result["summary"]["nodes_ready"] = sum(1 for n in result["nodes"] if n["ready"] == "True")
result["summary"]["vms_running"] = sum(1 for ns in result["vms_by_namespace"].values()
                                       for v in ns if v["phase"] == "Running")
result["summary"]["vms_total"] = sum(len(v) for v in result["vms_by_namespace"].values())

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
