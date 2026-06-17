#!/usr/bin/env bash
# harvester-shutdown.sh — Graceful shutdown of a Harvester HCI cluster
#
# Performs:
#   1. Pre-flight checks
#   2. Optional etcd snapshot
#   3. Stop all VMs (graceful ACPI)
#   4. Wait for VMIs to terminate
#   5. Longhorn maintenance mode (disable rebuild)
#   6. Wait for all Longhorn volumes to detach
#   7. Cordon nodes
#   8. Shutdown workers (parallel)
#   9. Shutdown control-plane (sequential, preserve quorum)
#
# Usage:
#   harvester-shutdown.sh --cluster <name> [--interactive] [--dry-run] [--yes]
#                          [--skip-etcd-snapshot] [--skip-vm-stop]
#                          [--vm-timeout 300] [--volume-timeout 120]

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

# Defaults
SKIP_ETCD_SNAPSHOT=0
SKIP_VM_STOP=0
VM_TIMEOUT=300       # seconds to wait for VMIs to disappear
VOLUME_TIMEOUT=180   # seconds to wait for Longhorn volumes to detach
NODE_SHUTDOWN_DELAY=15
DO_VM_SNAPSHOT=0     # --snapshot: take VirtualMachineBackup before stop
PER_VM_TIMEOUT=120   # per-VM ACPI shutdown timeout in ordered mode

usage() {
cat <<'EOF'
Usage: harvester-shutdown.sh --cluster <name> [options]

Required:
  -c, --cluster <name>      Cluster name from config

Options:
  -i, --interactive         Step-by-step prompts (recommended for production)
  -n, --dry-run             Print commands without executing
  -y, --yes                 Skip all confirmations (non-interactive batch mode)
  -v, --verbose             Verbose logging
      --config <path>       Config file (default: /etc/harvester-ops/config.yaml)
      --skip-etcd-snapshot  Skip the etcd snapshot step
      --skip-vm-stop        Skip the VM stop step (assume already stopped)
      --snapshot            Take a VirtualMachineBackup (type=snapshot) of every
                            running VM before stopping it. VMs annotated
                            harvester-ops.io/snapshot=false are excluded.
      --vm-timeout <sec>    Total VM shutdown timeout (default: 300)
      --per-vm-timeout <s>  Per-VM timeout when stopping in ordered mode (default: 120)
      --volume-timeout <s>  Longhorn volume detach timeout (default: 180)
      --no-color            Disable colored output
  -h, --help                Show this help

Examples:
  # Interactive mode (recommended first time)
  harvester-shutdown.sh -c harv-prod -i

  # Audit dry-run
  harvester-shutdown.sh -c harv-prod --dry-run -y

  # Automated batch (e.g. UPS-triggered)
  harvester-shutdown.sh -c harv-prod -y --skip-etcd-snapshot
EOF
}

# Parse args
parse_common_args "$@"
set -- "${POSITIONAL[@]}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --skip-etcd-snapshot) SKIP_ETCD_SNAPSHOT=1; shift ;;
        --skip-vm-stop) SKIP_VM_STOP=1; shift ;;
        --snapshot) DO_VM_SNAPSHOT=1; shift ;;
        --vm-timeout) VM_TIMEOUT="$2"; shift 2 ;;
        --per-vm-timeout) PER_VM_TIMEOUT="$2"; shift 2 ;;
        --volume-timeout) VOLUME_TIMEOUT="$2"; shift 2 ;;
        *) log_error "Argument inconnu : $1"; usage; exit 1 ;;
    esac
done

[[ -z "$CLUSTER_NAME" ]] && { log_error "--cluster requis"; usage; exit 1; }

load_cluster "$CLUSTER_NAME"
init_logging "shutdown"

cat >&2 <<EOF

${C_BOLD}${C_MAGENTA}╔══════════════════════════════════════════════════════════════╗
║      HARVESTER GRACEFUL SHUTDOWN — cluster: $(printf '%-15s' "$CLUSTER_NAME") ║
╚══════════════════════════════════════════════════════════════╝${C_RESET}

EOF

if [[ "$DRY_RUN" == "1" ]]; then
    log_warn "Mode DRY-RUN — aucune commande ne sera exécutée"
fi
if [[ "$INTERACTIVE" == "1" ]]; then
    log_info "Mode INTERACTIF — confirmation à chaque étape"
fi

# =============================================================================
# Step 0: pre-flight
# =============================================================================
preflight || exit 1
interactive_pause

# =============================================================================
# Step 1: etcd snapshot
# =============================================================================
step_etcd_snapshot() {
    emit_event "etcd-snapshot" "running" "Snapshot etcd"
    log_step "[1/8] Snapshot etcd"

    if [[ "$SKIP_ETCD_SNAPSHOT" == "1" ]]; then
        emit_event "etcd-snapshot" "skipped" "Skipped by user"
        log_warn "Snapshot etcd ignoré (--skip-etcd-snapshot)"
        return 0
    fi

    local cp_node
    cp_node=$(nodes_by_role "control-plane" | head -n1 | cut -d'|' -f2)
    [[ -z "$cp_node" ]] && { log_warn "Aucun control-plane configuré, snapshot ignoré"; emit_event "etcd-snapshot" "skipped" "no CP configured"; return 0; }

    log_info "Création du snapshot sur $cp_node"
    local snap_name="pre-shutdown-$(date +%Y%m%d-%H%M%S)"
    local cmd="sudo /var/lib/rancher/rke2/bin/etcdctl \
--endpoints=https://127.0.0.1:2379 \
--cacert=/var/lib/rancher/rke2/server/tls/etcd/server-ca.crt \
--cert=/var/lib/rancher/rke2/server/tls/etcd/server-client.crt \
--key=/var/lib/rancher/rke2/server/tls/etcd/server-client.key \
snapshot save /var/lib/rancher/rke2/server/db/snapshots/${snap_name}.db"

    if ssh_exec "$cp_node" "$cmd"; then
        emit_event "etcd-snapshot" "done" "Snapshot: ${snap_name}.db"
        log_ok "Snapshot etcd : ${snap_name}.db"
    else
        emit_event "etcd-snapshot" "error" "Snapshot failed"
        log_error "Échec du snapshot etcd"
        confirm "Continuer sans snapshot ? / Continue without snapshot?" || exit 1
    fi
}

# =============================================================================
# Step 2a: snapshot VMs (optional, --snapshot)
# =============================================================================
step_snapshot_vms() {
    if [[ "$DO_VM_SNAPSHOT" != "1" ]]; then
        emit_event "vm-snapshot" "skipped" "Snapshot non demandé"
        return 0
    fi
    emit_event "vm-snapshot" "running" "Snapshot des VMs running"
    log_step "[2a/8] Snapshot des VMs en cours d'exécution"

    local count=0 failed=0 skipped=0
    while IFS='|' read -r ns name prio snap_flag timeout; do
        [[ -z "$name" ]] && continue
        # Skip non-running VMs
        local phase
        phase=$(kc_quiet -n "$ns" get vmi "$name" -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
        if [[ "$phase" != "Running" ]]; then
            log_debug "Skip $ns/$name (phase=$phase, non running)"
            continue
        fi
        # Skip if annotation snapshot=false
        if [[ "$snap_flag" == "0" ]]; then
            log_info "Skip $ns/$name (annotation snapshot=false)"
            emit_event "vm-snapshot" "progress" "skip $ns/$name (excluded)"
            skipped=$((skipped+1))
            continue
        fi
        log_info "Snapshot: $ns/$name"
        emit_event "vm-snapshot" "progress" "snapshot $ns/$name"
        if snap_name=$(snapshot_vm "$ns" "$name"); then
            log_ok "  → $ns/$name → $snap_name"
            count=$((count+1))
        else
            failed=$((failed+1))
        fi
    done < <(get_ordered_vms)

    if (( failed > 0 )); then
        emit_event "vm-snapshot" "warn" "$count créés, $failed échoués, $skipped exclus"
        log_warn "$failed snapshot(s) échoué(s)"
        confirm "Continuer malgré les échecs de snapshot ? / Continue despite snapshot failures?" || exit 1
    else
        emit_event "vm-snapshot" "done" "$count snapshots créés, $skipped exclus"
        log_ok "$count snapshot(s) créé(s), $skipped exclus"
    fi
}

# =============================================================================
# Step 2: stop VMs (ordered, one by one)
# =============================================================================
step_stop_vms() {
    emit_event "vm-stop" "running" "Arrêt ordonné des VMs"
    log_step "[2/8] Arrêt gracieux (groupes parallèles entre eux, séquentiel intra-groupe, default = parallèle)"

    if [[ "$SKIP_VM_STOP" == "1" ]]; then
        emit_event "vm-stop" "skipped" "Skipped by user"
        log_warn "Arrêt VM ignoré (--skip-vm-stop)"
        return 0
    fi

    local ordered_vms
    ordered_vms=$(get_ordered_vms)
    local vm_count
    vm_count=$(echo "$ordered_vms" | grep -c . || true)
    if [[ "$vm_count" -eq 0 ]]; then
        emit_event "vm-stop" "done" "No VM to stop"
        log_ok "Aucune VM à arrêter"
        return 0
    fi
    log_info "$vm_count VM(s) à arrêter — voir le plan détaillé ci-dessous"

    # ---------- Plan preview (always printed, helps the operator review) ----------
    {
        echo "==== Plan d'arrêt ===="
        local last_gp=""
        echo "$ordered_vms" | sort -t'|' -k7,7n -k6,6 -k3,3n | \
        while IFS='|' read -r ns name intra snap timeout group gprio; do
            if [[ "$gprio" != "$last_gp" ]]; then
                echo "─── Niveau de priorité $gprio (groupes de ce niveau exécutés en parallèle) ───"
                last_gp="$gprio"
            fi
            if [[ "$group" == "default" ]]; then
                echo "    [default ⚡parallèle] $ns/$name"
            else
                echo "    [$group ▶intra=$intra] $ns/$name"
            fi
        done
        echo "======================"
    } >&2

    if [[ "$INTERACTIVE" == "1" ]]; then
        confirm "Arrêter ces $vm_count VMs selon ce plan ?" \
            || { log_warn "Étape ignorée par l'opérateur"; emit_event "vm-stop" "skipped" "operator skip"; return 0; }
    fi

    # ---------------------------------------------------------------------------
    # Algorithm:
    #   Outer loop (SEQUENTIAL): each distinct group_priority level
    #     Inner loop (PARALLEL): each group at that level
    #       If group == "default": all VMs in parallel
    #       Else: VMs sequentially in intra_order
    # ---------------------------------------------------------------------------

    _stop_one_sync() {
        # Fire patch then BLOCK until the VMI is gone (or timeout).
        local ns="$1" name="$2"
        local current_rs
        current_rs=$(kc_quiet -n "$ns" get vm "$name" -o jsonpath='{.spec.runStrategy}' 2>/dev/null || echo "Always")
        [[ -z "$current_rs" || "$current_rs" == "Halted" ]] && current_rs="Always"
        run kubectl --kubeconfig="$KUBECONFIG_PATH" annotate vm "$name" -n "$ns" \
            "${ANNOT_PREV_RUNSTRATEGY}=${current_rs}" --overwrite >/dev/null 2>&1 || true
        run kubectl --kubeconfig="$KUBECONFIG_PATH" patch vm "$name" -n "$ns" --type merge \
            -p '{"spec":{"runStrategy":"Halted"}}' >/dev/null 2>&1 || { log_warn "Patch failed $ns/$name"; return 1; }
        [[ "$DRY_RUN" == "1" ]] && return 0
        local deadline=$(( SECONDS + PER_VM_TIMEOUT ))
        while (( SECONDS < deadline )); do
            kc_quiet -n "$ns" get vmi "$name" >/dev/null 2>&1 || return 0
            sleep 3
        done
        log_warn "  → $ns/$name pas arrêté après ${PER_VM_TIMEOUT}s, on continue"
        return 0
    }

    _process_group() {
        # Args: group_name; then VM specs as remaining args ("intra|ns|name")
        local gname="$1"; shift
        local specs=("$@")
        local size=${#specs[@]}
        if [[ "$gname" == "default" ]]; then
            log_info "  [$gname ⚡parallèle] $size VM(s) en parallèle"
            local pids=()
            for spec in "${specs[@]}"; do
                IFS='|' read -r _intra ns name <<< "$spec"
                log_info "    → fire stop $ns/$name"
                _stop_one_sync "$ns" "$name" &
                pids+=($!)
            done
            for p in "${pids[@]}"; do wait "$p" || true; done
        else
            log_info "  [$gname ▶séquentiel] $size VM(s) une par une"
            # specs already pre-sorted by intra by the outer loop
            for spec in "${specs[@]}"; do
                IFS='|' read -r intra ns name <<< "$spec"
                log_info "    → stop $ns/$name (intra=$intra), wait…"
                _stop_one_sync "$ns" "$name"
            done
        fi
        log_ok "  [$gname] terminé ($size VM(s))"
    }

    # Build the priority-level → groups → VMs structure in-memory using
    # bash arrays. Streaming-style: read all lines, accumulate, then drain.
    declare -A level_groups       # level_groups["$gprio"] = "group1\tgroup2\t..."
    declare -A group_specs        # group_specs["$gprio|$group"] = "intra|ns|name\nintra|ns|name\n..."
    local ordered_levels=()       # distinct group priorities in ascending order

    while IFS='|' read -r ns name intra _snap _timeout group gprio; do
        [[ -z "$name" ]] && continue
        local key="$gprio|$group"
        if [[ -z "${group_specs[$key]:-}" ]]; then
            # First time seeing this (gprio, group) pair
            group_specs[$key]="${intra}|${ns}|${name}"
            if [[ -z "${level_groups[$gprio]:-}" ]]; then
                level_groups[$gprio]="$group"
                ordered_levels+=("$gprio")
            else
                # Append if not already present (defensive)
                case $'\t'"${level_groups[$gprio]}"$'\t' in
                    *$'\t'"$group"$'\t'*) ;;
                    *) level_groups[$gprio]="${level_groups[$gprio]}"$'\t'"$group" ;;
                esac
            fi
        else
            group_specs[$key]="${group_specs[$key]}"$'\n'"${intra}|${ns}|${name}"
        fi
    done <<< "$ordered_vms"

    # Sort ordered_levels ascending (numeric)
    IFS=$'\n' read -d '' -r -a ordered_levels < <(printf '%s\n' "${ordered_levels[@]}" | sort -n | uniq && printf '\0') || true

    local level_idx=0 done_count=0
    for level in "${ordered_levels[@]}"; do
        level_idx=$((level_idx+1))
        local groups_in_level
        IFS=$'\t' read -ra groups_in_level <<< "${level_groups[$level]}"
        log_info "─── Niveau $level_idx (group_priority=$level) — ${#groups_in_level[@]} groupe(s) en parallèle ───"
        emit_event "vm-stop" "progress" "niveau $level_idx (group_priority=$level): ${#groups_in_level[@]} groupes"

        local group_pids=()
        for gname in "${groups_in_level[@]}"; do
            # Collect specs for this (level, group), sorted by intra order
            local key="$level|$gname"
            local specs_str="${group_specs[$key]}"
            mapfile -t specs < <(printf '%s\n' "$specs_str" | sort -t'|' -k1,1n)
            # Run each group in background (parallel between groups)
            _process_group "$gname" "${specs[@]}" &
            group_pids+=($!)
            done_count=$((done_count + ${#specs[@]}))
        done
        # Wait for ALL groups in this level to finish before moving on
        for p in "${group_pids[@]}"; do wait "$p" || true; done
        log_ok "─── Niveau $level_idx terminé ───"
    done

    if [[ "$DRY_RUN" == "1" ]]; then
        emit_event "vm-stop" "done" "$vm_count VMs (dry-run, no wait)"
        log_ok "[DRY-RUN] Attente d'arrêt VMs ignorée"
        return 0
    fi

    # Final safety check
    local remaining
    remaining=$(kc_quiet get vmi -A --no-headers 2>/dev/null | wc -l)
    if [[ "$remaining" -gt 0 ]]; then
        log_warn "$remaining VMI encore actives après séquence ordonnée"
        kc_quiet get vmi -A
        confirm "Continuer malgré tout ? / Continue anyway?" || exit 1
    fi
    emit_event "vm-stop" "done" "$vm_count VMs stopped (ordered)"
    log_ok "Toutes les VMs sont arrêtées"
}

# =============================================================================
# Step 3: Longhorn maintenance
# =============================================================================
step_longhorn_maintenance() {
    emit_event "longhorn-maint" "running" "Longhorn maintenance mode"
    log_step "[3/8] Longhorn — mode maintenance"

    if ! kc_quiet get crd settings.longhorn.io >/dev/null 2>&1; then
        emit_event "longhorn-maint" "skipped" "Longhorn not installed"
        log_info "Longhorn non installé, étape ignorée"
        return 0
    fi

    run kubectl --kubeconfig="$KUBECONFIG_PATH" -n longhorn-system patch settings.longhorn.io \
        concurrent-replica-rebuild-per-node-limit \
        --type=merge -p '{"value":"0"}' || log_warn "patch concurrent-rebuild échoué"

    if [[ "$DRY_RUN" == "1" ]]; then
        emit_event "longhorn-maint" "done" "Dry-run, detach wait skipped"
        log_ok "[DRY-RUN] Attente détachement Longhorn ignorée"
        return 0
    fi

    log_info "Attente du détachement des volumes (timeout ${VOLUME_TIMEOUT}s)..."
    local deadline=$(( SECONDS + VOLUME_TIMEOUT ))
    while (( SECONDS < deadline )); do
        local attached
        attached=$(kc_quiet -n longhorn-system get volumes.longhorn.io \
            -o jsonpath='{range .items[?(@.status.state=="attached")]}{.metadata.name}{"\n"}{end}' 2>/dev/null | wc -l)
        if [[ "$attached" -eq 0 ]]; then
            emit_event "longhorn-maint" "done" "All volumes detached"
            log_ok "Tous les volumes Longhorn sont détachés"
            return 0
        fi
        log_info "  → $attached volume(s) encore attaché(s)..."
        emit_event "longhorn-maint" "progress" "$attached volumes still attached"
        sleep 5
    done

    emit_event "longhorn-maint" "warn" "Some volumes still attached"
    log_warn "Certains volumes ne se détachent pas — risque pour la cohérence"
    kc_quiet -n longhorn-system get volumes.longhorn.io
    confirm "Continuer malgré tout ? / Continue anyway?" || exit 1
}

# =============================================================================
# Step 4: cordon nodes
# =============================================================================
step_cordon() {
    emit_event "cordon" "running" "Cordon all nodes"
    log_step "[4/8] Cordon des nodes"

    while IFS= read -r node; do
        log_info "Cordon: $node"
        run kubectl --kubeconfig="$KUBECONFIG_PATH" cordon "$node" || true
    done < <(kc_quiet get nodes -o name 2>/dev/null | sed 's|node/||')

    emit_event "cordon" "done" "Nodes cordoned"
    log_ok "Tous les nodes sont cordonnés"
}

# =============================================================================
# Step 5: shutdown workers
# =============================================================================
step_shutdown_workers() {
    emit_event "shutdown-workers" "running" "Shutdown workers"
    log_step "[5/8] Extinction des workers"

    local count=0
    while IFS='|' read -r host ip; do
        [[ -z "$host" ]] && continue
        count=$((count + 1))
        log_info "Worker shutdown: $host ($ip)"
        emit_event "shutdown-workers" "progress" "$host"
        ssh_exec "$ip" "sudo shutdown -h +0 'Harvester graceful shutdown'" || log_warn "SSH $host : code retour non-zéro (normal si node éteint)"
    done < <(nodes_by_role "worker")

    if [[ "$count" -eq 0 ]]; then
        emit_event "shutdown-workers" "skipped" "no workers"
        log_info "Pas de worker dédié — étape ignorée"
        return 0
    fi

    if [[ "$DRY_RUN" != "1" ]]; then
        log_info "Attente $NODE_SHUTDOWN_DELAY s pour que les workers s'éteignent..."
        sleep "$NODE_SHUTDOWN_DELAY"
    fi
    emit_event "shutdown-workers" "done" "$count workers shutdown"
    log_ok "$count worker(s) éteint(s)"
}

# =============================================================================
# Step 6: shutdown control-plane (reverse order, preserve quorum)
# =============================================================================
step_shutdown_cp() {
    emit_event "shutdown-cp" "running" "Shutdown control-plane"
    log_step "[6/8] Extinction control-plane (ordre inverse)"

    # Reverse order: shut down highest hostname first, keep cp1 alive longest
    local cps
    cps=$(nodes_by_role "control-plane" | tac)
    local total
    total=$(echo "$cps" | grep -c . || true)

    [[ "$total" -eq 0 ]] && { log_warn "Aucun control-plane à éteindre"; emit_event "shutdown-cp" "skipped" "none"; return 0; }

    local i=0
    while IFS='|' read -r host ip; do
        [[ -z "$host" ]] && continue
        i=$((i + 1))
        log_info "CP shutdown ($i/$total): $host ($ip)"
        emit_event "shutdown-cp" "progress" "$host ($i/$total)"
        if [[ "$INTERACTIVE" == "1" ]]; then
            confirm "Éteindre $host maintenant ? / Power off $host now?" || { log_warn "$host non éteint sur demande opérateur"; continue; }
        fi
        ssh_exec "$ip" "sudo shutdown -h +0 'Harvester graceful shutdown'" || log_warn "SSH $host : code retour non-zéro"
        if (( i < total )) && [[ "$DRY_RUN" != "1" ]]; then
            log_info "Pause $NODE_SHUTDOWN_DELAY s avant le CP suivant..."
            sleep "$NODE_SHUTDOWN_DELAY"
        fi
    done <<< "$cps"

    emit_event "shutdown-cp" "done" "$total CP nodes shutdown"
    log_ok "$total control-plane node(s) éteints"
}

# =============================================================================
# Main
# =============================================================================
log_info "Étapes : etcd-snapshot → vm-snapshot(?) → vm-stop → longhorn-maint → cordon → workers → CP"
[[ "$DO_VM_SNAPSHOT" == "1" ]] && log_info "Snapshot VMs ACTIVÉ (--snapshot)"

if [[ "$INTERACTIVE" != "1" && "$ASSUME_YES" != "1" && "$DRY_RUN" != "1" ]]; then
    log_error "Action destructive : passez --interactive, --yes ou --dry-run"
    exit 1
fi

confirm "Démarrer l'arrêt du cluster '$CLUSTER_NAME' ?" || { log_warn "Annulé par l'opérateur"; exit 0; }

step_etcd_snapshot;       interactive_pause
step_snapshot_vms;        interactive_pause
step_stop_vms;            interactive_pause
step_longhorn_maintenance; interactive_pause
step_cordon;              interactive_pause
step_shutdown_workers;    interactive_pause
step_shutdown_cp

emit_event "complete" "done" "Cluster shutdown sequence complete"

cat >&2 <<EOF

${C_BOLD}${C_GREEN}╔══════════════════════════════════════════════════════════════╗
║  ✓ SHUTDOWN COMPLETE — cluster: $(printf '%-30s' "$CLUSTER_NAME") ║
╚══════════════════════════════════════════════════════════════╝${C_RESET}

Log: $_log_file

Pour le redémarrage : harvester-startup.sh --cluster $CLUSTER_NAME
For startup       : harvester-startup.sh --cluster $CLUSTER_NAME

EOF
