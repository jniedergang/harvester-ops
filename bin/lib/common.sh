#!/usr/bin/env bash
# Common library for harvester-ops scripts
# Provides: logging, colors, confirmation, dry-run, interactive mode, config loading

set -o pipefail

# -----------------------------------------------------------------------------
# Colors (disabled if NO_COLOR set or non-tty)
# -----------------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_RESET=$'\e[0m'
    C_RED=$'\e[31m'
    C_GREEN=$'\e[32m'
    C_YELLOW=$'\e[33m'
    C_BLUE=$'\e[34m'
    C_MAGENTA=$'\e[35m'
    C_CYAN=$'\e[36m'
    C_BOLD=$'\e[1m'
    C_DIM=$'\e[2m'
else
    C_RESET="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE=""
    C_MAGENTA="" C_CYAN="" C_BOLD="" C_DIM=""
fi

# -----------------------------------------------------------------------------
# Globals (overridable from caller)
# -----------------------------------------------------------------------------
: "${HARVESTER_OPS_LOG_DIR:=/var/log/harvester-ops}"
: "${HARVESTER_OPS_CONFIG:=/etc/harvester-ops/config.yaml}"
: "${DRY_RUN:=0}"
: "${INTERACTIVE:=0}"
: "${ASSUME_YES:=0}"
: "${VERBOSE:=0}"
: "${CLUSTER_NAME:=}"

# Will be set by load_cluster()
KUBECONFIG_PATH=""
SSH_USER="rancher"
SSH_KEY=""
SSH_PORT="22"
SSH_OPTS=""
declare -a CLUSTER_NODES=()    # "hostname|ip|role"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
_log_file=""

init_logging() {
    local action="${1:-action}"
    mkdir -p "$HARVESTER_OPS_LOG_DIR" 2>/dev/null || HARVESTER_OPS_LOG_DIR="${TMPDIR:-/tmp}/harvester-ops"
    mkdir -p "$HARVESTER_OPS_LOG_DIR"
    _log_file="$HARVESTER_OPS_LOG_DIR/$(date +%Y%m%d-%H%M%S)-${CLUSTER_NAME:-default}-${action}.log"
    : > "$_log_file"
    log_info "Log file: $_log_file"
}

_log() {
    local level="$1"; shift
    local color="$1"; shift
    local msg="$*"
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s%s [%s]%s %s\n' "$color" "$ts" "$level" "$C_RESET" "$msg" >&2
    if [[ -n "$_log_file" ]]; then
        printf '%s [%s] %s\n' "$ts" "$level" "$msg" >> "$_log_file"
    fi
    return 0
}

log_info()    { _log "INFO " "$C_BLUE"    "$@"; }
log_ok()      { _log "OK   " "$C_GREEN"   "$@"; }
log_warn()    { _log "WARN " "$C_YELLOW"  "$@"; }
log_error()   { _log "ERROR" "$C_RED"     "$@"; }
log_step()    { _log "STEP " "$C_MAGENTA$C_BOLD" "$@"; }
log_debug()   { if [[ "$VERBOSE" == "1" ]]; then _log "DEBUG" "$C_DIM" "$@"; fi; return 0; }

# Structured event emitter (for the web UI to parse over SSE)
# Format: STEP_EVENT|<step_id>|<status>|<message>
emit_event() {
    local step_id="$1" status="$2" msg="${3:-}"
    printf 'STEP_EVENT|%s|%s|%s\n' "$step_id" "$status" "$msg" >&2
    if [[ -n "$_log_file" ]]; then
        printf 'STEP_EVENT|%s|%s|%s\n' "$step_id" "$status" "$msg" >> "$_log_file"
    fi
    return 0
}

# -----------------------------------------------------------------------------
# Confirmation / interactive prompts
# -----------------------------------------------------------------------------
confirm() {
    local prompt="${1:-Continuer ?}"
    if [[ "$ASSUME_YES" == "1" ]]; then
        log_debug "Auto-confirm: $prompt"
        return 0
    fi
    if [[ "$INTERACTIVE" != "1" && "$ASSUME_YES" != "1" ]]; then
        log_error "Action critique sans --yes ni --interactive : abandon."
        log_error "Critical action without --yes or --interactive: aborting."
        return 1
    fi
    local reply
    while true; do
        printf '%s%s [y/N]:%s ' "$C_YELLOW$C_BOLD" "$prompt" "$C_RESET" >&2
        read -r reply </dev/tty || return 1
        case "$reply" in
            [yY]|[yY][eE][sS]) return 0 ;;
            ""|[nN]|[nN][oO]) return 1 ;;
            *) echo "Réponse invalide / Invalid answer" >&2 ;;
        esac
    done
}

# Pause in interactive mode between steps
interactive_pause() {
    [[ "$INTERACTIVE" != "1" ]] && return 0
    local msg="${1:-Appuyez sur Entrée pour continuer / Press Enter to continue}"
    printf '%s>>> %s%s\n' "$C_CYAN" "$msg" "$C_RESET" >&2
    read -r _ </dev/tty || true
}

# -----------------------------------------------------------------------------
# Dry-run wrapper
# -----------------------------------------------------------------------------
run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '%s[DRY-RUN]%s %s\n' "$C_DIM" "$C_RESET" "$*" >&2
        return 0
    fi
    log_debug "exec: $*"
    "$@"
}

run_pipe() {
    # For commands with pipes/redirections that need to be eval'd
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '%s[DRY-RUN]%s %s\n' "$C_DIM" "$C_RESET" "$*" >&2
        return 0
    fi
    log_debug "exec: $*"
    eval "$@"
}

# -----------------------------------------------------------------------------
# Config loading (minimal YAML parser for the subset we need)
# -----------------------------------------------------------------------------
require_yq() {
    if ! command -v yq >/dev/null 2>&1; then
        log_error "yq non trouvé / not found. Installer yq (mikefarah/yq v4+)."
        exit 2
    fi
}

load_cluster() {
    local name="$1"
    [[ -z "$name" ]] && { log_error "Nom de cluster requis / cluster name required"; return 1; }
    [[ ! -f "$HARVESTER_OPS_CONFIG" ]] && { log_error "Config non trouvée : $HARVESTER_OPS_CONFIG"; return 1; }
    require_yq

    local found
    found=$(yq ".clusters[] | select(.name == \"$name\") | .name" "$HARVESTER_OPS_CONFIG")
    [[ -z "$found" || "$found" == "null" ]] && { log_error "Cluster '$name' introuvable dans la config"; return 1; }

    CLUSTER_NAME="$name"
    KUBECONFIG_PATH=$(yq -r ".clusters[] | select(.name == \"$name\") | .kubeconfig" "$HARVESTER_OPS_CONFIG")
    SSH_USER=$(yq -r ".clusters[] | select(.name == \"$name\") | .ssh.user // \"rancher\"" "$HARVESTER_OPS_CONFIG")
    SSH_KEY=$(yq -r ".clusters[] | select(.name == \"$name\") | .ssh.key // \"\"" "$HARVESTER_OPS_CONFIG")
    SSH_PORT=$(yq -r ".clusters[] | select(.name == \"$name\") | .ssh.port // 22" "$HARVESTER_OPS_CONFIG")

    SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p $SSH_PORT"
    [[ -n "$SSH_KEY" && "$SSH_KEY" != "null" ]] && SSH_OPTS="$SSH_OPTS -i $SSH_KEY"

    export KUBECONFIG="$KUBECONFIG_PATH"

    # Load nodes
    CLUSTER_NODES=()
    local count
    count=$(yq ".clusters[] | select(.name == \"$name\") | .nodes | length" "$HARVESTER_OPS_CONFIG")
    for ((i=0; i<count; i++)); do
        local h ip role
        h=$(yq -r ".clusters[] | select(.name == \"$name\") | .nodes[$i].hostname" "$HARVESTER_OPS_CONFIG")
        ip=$(yq -r ".clusters[] | select(.name == \"$name\") | .nodes[$i].ip" "$HARVESTER_OPS_CONFIG")
        role=$(yq -r ".clusters[] | select(.name == \"$name\") | .nodes[$i].role" "$HARVESTER_OPS_CONFIG")
        CLUSTER_NODES+=("$h|$ip|$role")
    done

    log_ok "Cluster chargé / loaded: $CLUSTER_NAME (${#CLUSTER_NODES[@]} nodes)"
}

list_clusters() {
    require_yq
    [[ ! -f "$HARVESTER_OPS_CONFIG" ]] && { log_error "Config non trouvée : $HARVESTER_OPS_CONFIG"; return 1; }
    yq -r '.clusters[].name' "$HARVESTER_OPS_CONFIG"
}

nodes_by_role() {
    local role="$1"
    for entry in "${CLUSTER_NODES[@]}"; do
        IFS='|' read -r h ip r <<< "$entry"
        [[ "$r" == "$role" ]] && echo "$h|$ip"
    done
}

# -----------------------------------------------------------------------------
# kubectl + SSH helpers
# -----------------------------------------------------------------------------
kc() {
    run kubectl --kubeconfig="$KUBECONFIG_PATH" "$@"
}

kc_quiet() {
    # No dry-run wrap, for read-only queries
    kubectl --kubeconfig="$KUBECONFIG_PATH" "$@"
}

ssh_exec() {
    local target="$1"; shift
    run ssh $SSH_OPTS "${SSH_USER}@${target}" "$@"
}

ssh_exec_quiet() {
    local target="$1"; shift
    ssh $SSH_OPTS -o LogLevel=ERROR "${SSH_USER}@${target}" "$@" 2>&1
}

# -----------------------------------------------------------------------------
# Arg parsing helper (sets DRY_RUN, INTERACTIVE, etc.)
# -----------------------------------------------------------------------------
parse_common_args() {
    POSITIONAL=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run|-n) DRY_RUN=1; shift ;;
            --interactive|-i) INTERACTIVE=1; shift ;;
            --yes|-y) ASSUME_YES=1; shift ;;
            --verbose|-v) VERBOSE=1; shift ;;
            --cluster|-c) CLUSTER_NAME="$2"; shift 2 ;;
            --cluster=*) CLUSTER_NAME="${1#*=}"; shift ;;
            --config) HARVESTER_OPS_CONFIG="$2"; shift 2 ;;
            --config=*) HARVESTER_OPS_CONFIG="${1#*=}"; shift ;;
            --no-color) NO_COLOR=1; shift ;;
            --) shift; POSITIONAL+=("$@"); break ;;
            *) POSITIONAL+=("$1"); shift ;;
        esac
    done
}

# -----------------------------------------------------------------------------
# Annotation constants
# -----------------------------------------------------------------------------
ANNOT_PRIORITY="harvester-ops.io/shutdown-priority"           # intra-group order
ANNOT_GROUP="harvester-ops.io/shutdown-group"
ANNOT_GROUP_PRIORITY="harvester-ops.io/shutdown-group-priority"  # group-level priority
ANNOT_SNAPSHOT="harvester-ops.io/snapshot"
ANNOT_READY_TIMEOUT="harvester-ops.io/ready-timeout"
ANNOT_PREV_RUNSTRATEGY="harvester-ops.io/previous-runStrategy"
DEFAULT_PRIORITY=10
DEFAULT_GROUP_PRIORITY=100
DEFAULT_GROUP="default"
DEFAULT_READY_TIMEOUT=300

# -----------------------------------------------------------------------------
# Ordered VM list — emits sorted lines
#   "ns|name|intra_order|snapshot|timeout|group|group_priority"
#
# Two priority levels (since v1.4.14):
#   - group_priority (per-group): lower = runs earlier. Groups sharing the
#     same group_priority are processed IN PARALLEL.
#   - intra_order (per-vm): only applies to NON-default groups. Within a
#     normal group, VMs stop sequentially (lowest first). The "default"
#     group is special: ALL its VMs stop in parallel, intra_order ignored.
#
# Default annotation values when missing:
#   - group = "default", group_priority = 100, intra_order = 10
#
# Backward compatibility: v1.4.9-12 used `shutdown-priority` to mean the
# group's priority. We now read it as intra_order. If a VM has only the
# old annotation, it lands in "default" with group_priority=100 — the
# user re-adjusts in the UI and saves. No silent migration.
# -----------------------------------------------------------------------------
get_ordered_vms() {
    local reverse="${1:-}"   # any non-empty value = reverse (for startup)
    kc_quiet get vm -A -o json 2>/dev/null | REVERSE="$reverse" python3 -c '
import json, os, sys

reverse = bool(os.environ.get("REVERSE", ""))
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

vms = []
for item in data.get("items", []):
    name = item["metadata"]["name"]
    ns = item["metadata"]["namespace"]
    annot = item["metadata"].get("annotations", {}) or {}
    try:
        intra = int(annot.get("harvester-ops.io/shutdown-priority", "10"))
    except (TypeError, ValueError):
        intra = 10
    try:
        gprio = int(annot.get("harvester-ops.io/shutdown-group-priority", "100"))
    except (TypeError, ValueError):
        gprio = 100
    snapshot_flag = annot.get("harvester-ops.io/snapshot", "true").lower() != "false"
    timeout = annot.get("harvester-ops.io/ready-timeout", "300")
    group = annot.get("harvester-ops.io/shutdown-group", "") or "default"
    vms.append((gprio, group, intra, name, ns, snapshot_flag, timeout))

# Outer sort: (group_priority, group, intra_order, name). Groups stay
# contiguous so the bash batcher can iterate level-by-level.
vms.sort(key=lambda v: (v[0], v[1], v[2], v[3]))
if reverse:
    vms.reverse()

for gprio, group, intra, name, ns, snap, timeout in vms:
    print(f"{ns}|{name}|{intra}|{int(snap)}|{timeout}|{group}|{gprio}")
'
}

# -----------------------------------------------------------------------------
# Snapshot a VM — creates VirtualMachineBackup type=snapshot (Longhorn-backed)
# -----------------------------------------------------------------------------
snapshot_vm() {
    local ns="$1" name="$2"
    local snap_name="${name}-preshutdown-$(date +%Y%m%d-%H%M%S)"
    local manifest
    manifest=$(cat <<EOF
apiVersion: harvesterhci.io/v1beta1
kind: VirtualMachineBackup
metadata:
  name: ${snap_name}
  namespace: ${ns}
  labels:
    harvester-ops.io/created-by: harvester-ops
  annotations:
    harvester-ops.io/source-vm: ${name}
spec:
  type: snapshot
  source:
    apiGroup: kubevirt.io
    kind: VirtualMachine
    name: ${name}
EOF
)
    if [[ "$DRY_RUN" == "1" ]]; then
        printf '%s[DRY-RUN]%s create VMBackup snapshot %s/%s\n' "$C_DIM" "$C_RESET" "$ns" "$snap_name" >&2
        echo "$snap_name"
        return 0
    fi
    if echo "$manifest" | kubectl --kubeconfig="$KUBECONFIG_PATH" apply -f - >/dev/null 2>&1; then
        echo "$snap_name"
        return 0
    else
        log_error "Échec snapshot $ns/$name"
        return 1
    fi
}

# -----------------------------------------------------------------------------
# Wait for VM to be ready (running + qemu-guest-agent connected)
# -----------------------------------------------------------------------------
wait_for_vm_ready() {
    local ns="$1" name="$2" timeout="${3:-$DEFAULT_READY_TIMEOUT}"
    if [[ "$DRY_RUN" == "1" ]]; then
        log_info "[DRY-RUN] would wait for $ns/$name ready (timeout ${timeout}s)"
        return 0
    fi
    local deadline=$(( SECONDS + timeout ))
    local last_phase="" last_agent=""
    while (( SECONDS < deadline )); do
        local phase agent
        phase=$(kc_quiet -n "$ns" get vmi "$name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
        agent=$(kc_quiet -n "$ns" get vmi "$name" -o jsonpath='{.status.conditions[?(@.type=="AgentConnected")].status}' 2>/dev/null || echo "")
        if [[ "$phase" == "Running" && "$agent" == "True" ]]; then
            log_ok "  → $ns/$name Ready (agent connected)"
            return 0
        fi
        if [[ "$phase" != "$last_phase" || "$agent" != "$last_agent" ]]; then
            log_info "  → $ns/$name phase=$phase agent=$agent..."
            last_phase="$phase"; last_agent="$agent"
        fi
        sleep 5
    done
    # Fallback: accept "Running" alone if agent never connects
    local phase
    phase=$(kc_quiet -n "$ns" get vmi "$name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    if [[ "$phase" == "Running" ]]; then
        log_warn "  → $ns/$name Running mais agent jamais connecté (timeout) — on continue"
        return 0
    fi
    log_error "  → $ns/$name pas Ready après ${timeout}s (phase=$phase)"
    return 1
}

# -----------------------------------------------------------------------------
# Patch shutdown-priority annotation on a VM
# -----------------------------------------------------------------------------
set_vm_priority() {
    local ns="$1" name="$2" priority="$3"
    run kubectl --kubeconfig="$KUBECONFIG_PATH" annotate vm "$name" -n "$ns" \
        "${ANNOT_PRIORITY}=${priority}" --overwrite
}

# -----------------------------------------------------------------------------
# Pre-flight checks
# -----------------------------------------------------------------------------
preflight() {
    emit_event "preflight" "running" "Vérifications préalables / Pre-flight checks"
    log_step "Pré-vérifications du cluster '$CLUSTER_NAME'"

    if ! kc_quiet get nodes >/dev/null 2>&1; then
        emit_event "preflight" "error" "API Kubernetes injoignable"
        log_error "API Kubernetes injoignable / unreachable. Vérifier KUBECONFIG."
        return 1
    fi

    local notready
    notready=$(kc_quiet get nodes --no-headers | awk '$2 != "Ready" {print $1}' | wc -l)
    if [[ "$notready" -gt 0 ]]; then
        log_warn "$notready node(s) non Ready avant shutdown — état dégradé"
        kc_quiet get nodes
        confirm "Continuer malgré tout ? / Continue anyway?" || return 1
    fi

    if ! kc_quiet get crd volumes.longhorn.io >/dev/null 2>&1; then
        log_warn "Longhorn non détecté / not detected — étapes Longhorn ignorées"
    fi

    emit_event "preflight" "done" "Cluster accessible"
    log_ok "Cluster accessible — $(kc_quiet get nodes --no-headers | wc -l) nodes"
}
