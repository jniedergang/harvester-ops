#!/usr/bin/env bash
# harvester-startup.sh — Bring a Harvester cluster back online after graceful shutdown
#
# Reverse order:
#   1. Power on first control-plane (via WoL or out-of-band — TBD)
#   2. Wait for API readiness
#   3. Power on remaining CPs, then workers
#   4. Wait for all nodes Ready
#   5. Re-enable Longhorn rebuild
#   6. Uncordon all nodes
#   7. Optionally restart VMs that had runStrategy=Always before shutdown

set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

API_TIMEOUT=600
NODE_READY_TIMEOUT=900
SKIP_VM_RESTART=0
WAIT_BETWEEN_NODES=20

usage() {
cat <<'EOF'
Usage: harvester-startup.sh --cluster <name> [options]

The script assumes nodes will be powered on out-of-band (iLO/iDRAC/IPMI/WoL).
Operators are prompted to power on each node when needed.

Options:
  -c, --cluster <name>      Cluster name from config
  -i, --interactive         Step-by-step prompts (default for startup)
  -n, --dry-run             Dry-run
  -y, --yes                 Skip confirmations
      --skip-vm-restart     Don't restart VMs at the end
      --api-timeout <s>     API readiness timeout (default: 600)
      --node-timeout <s>    All-nodes-ready timeout (default: 900)
  -h, --help                Show this help
EOF
}

parse_common_args "$@"
set -- "${POSITIONAL[@]}"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --skip-vm-restart) SKIP_VM_RESTART=1; shift ;;
        --api-timeout) API_TIMEOUT="$2"; shift 2 ;;
        --node-timeout) NODE_READY_TIMEOUT="$2"; shift 2 ;;
        *) log_error "Argument inconnu : $1"; usage; exit 1 ;;
    esac
done

[[ -z "$CLUSTER_NAME" ]] && { log_error "--cluster requis"; usage; exit 1; }
# Startup is inherently interactive (needs human to power on nodes)
[[ "$ASSUME_YES" != "1" ]] && INTERACTIVE=1

load_cluster "$CLUSTER_NAME"
init_logging "startup"

cat >&2 <<EOF

${C_BOLD}${C_MAGENTA}╔══════════════════════════════════════════════════════════════╗
║      HARVESTER STARTUP — cluster: $(printf '%-25s' "$CLUSTER_NAME") ║
╚══════════════════════════════════════════════════════════════╝${C_RESET}

EOF

# =============================================================================
# Step 1: power on first CP and wait for API
# =============================================================================
step_power_first_cp() {
    emit_event "power-cp1" "running" "Power on first CP"
    log_step "[1/5] Démarrage du premier control-plane"

    local first_cp_host first_cp_ip
    IFS='|' read -r first_cp_host first_cp_ip <<< "$(nodes_by_role control-plane | head -n1)"
    [[ -z "$first_cp_host" ]] && { log_error "Aucun control-plane configuré"; exit 1; }

    log_info "À allumer en premier (via iLO/iDRAC/IPMI) : $first_cp_host ($first_cp_ip)"
    log_info "First node to power on (iLO/iDRAC/IPMI): $first_cp_host ($first_cp_ip)"
    confirm "Le node $first_cp_host a-t-il été allumé ? / Has it been powered on?" || exit 1

    if [[ "$DRY_RUN" == "1" ]]; then
        emit_event "power-cp1" "done" "[DRY-RUN] API wait skipped"
        log_ok "[DRY-RUN] Attente API ignorée"
        return 0
    fi

    log_info "Attente de l'API Kubernetes (timeout ${API_TIMEOUT}s)..."
    local deadline=$(( SECONDS + API_TIMEOUT ))
    while (( SECONDS < deadline )); do
        if kc_quiet get --raw=/readyz >/dev/null 2>&1; then
            emit_event "power-cp1" "done" "API ready on $first_cp_host"
            log_ok "API Kubernetes prête sur $first_cp_host"
            return 0
        fi
        emit_event "power-cp1" "progress" "Waiting API on $first_cp_host..."
        log_info "  → API pas encore prête..."
        sleep 15
    done

    emit_event "power-cp1" "error" "API timeout"
    log_error "L'API n'est pas prête après ${API_TIMEOUT}s"
    exit 1
}

# =============================================================================
# Step 2: power on remaining CPs and workers
# =============================================================================
step_power_rest() {
    emit_event "power-rest" "running" "Power on remaining nodes"
    log_step "[2/5] Démarrage des autres nodes"

    # remaining CPs
    local i=0
    while IFS='|' read -r host ip; do
        i=$((i+1))
        [[ "$i" -eq 1 ]] && continue   # skip first (already on)
        log_info "À allumer : $host ($ip)"
        emit_event "power-rest" "progress" "$host"
        confirm "$host allumé ? / Powered on?" || log_warn "$host non démarré sur demande"
        sleep "$WAIT_BETWEEN_NODES"
    done < <(nodes_by_role "control-plane")

    while IFS='|' read -r host ip; do
        [[ -z "$host" ]] && continue
        log_info "À allumer : $host ($ip)"
        emit_event "power-rest" "progress" "$host"
        confirm "$host allumé ? / Powered on?" || log_warn "$host non démarré sur demande"
        sleep "$WAIT_BETWEEN_NODES"
    done < <(nodes_by_role "worker")

    emit_event "power-rest" "done" "All nodes powered on"
}

# =============================================================================
# Step 3: wait for all nodes Ready
# =============================================================================
step_wait_nodes_ready() {
    emit_event "wait-ready" "running" "Waiting for nodes Ready"
    log_step "[3/5] Attente que tous les nodes soient Ready"

    local expected="${#CLUSTER_NODES[@]}"
    if [[ "$DRY_RUN" == "1" ]]; then
        emit_event "wait-ready" "done" "[DRY-RUN] node wait skipped"
        log_ok "[DRY-RUN] Attente Nodes Ready ignorée"
        return 0
    fi
    local deadline=$(( SECONDS + NODE_READY_TIMEOUT ))
    while (( SECONDS < deadline )); do
        local ready
        ready=$(kc_quiet get nodes --no-headers 2>/dev/null | awk '$2 == "Ready" {c++} END {print c+0}')
        if [[ "$ready" -ge "$expected" ]]; then
            emit_event "wait-ready" "done" "$ready/$expected Ready"
            log_ok "$ready/$expected nodes Ready"
            return 0
        fi
        emit_event "wait-ready" "progress" "$ready/$expected Ready"
        log_info "  → $ready/$expected Ready..."
        sleep 15
    done

    emit_event "wait-ready" "warn" "Timeout — some nodes not Ready"
    log_warn "Timeout : certains nodes ne sont pas Ready"
    kc_quiet get nodes
    confirm "Continuer ? / Continue?" || exit 1
}

# =============================================================================
# Step 4: re-enable Longhorn + uncordon
# =============================================================================
step_restore_cluster_state() {
    emit_event "restore" "running" "Restore cluster state"
    log_step "[4/5] Restauration : Longhorn + uncordon"

    if kc_quiet get crd settings.longhorn.io >/dev/null 2>&1; then
        log_info "Réactivation du rebuild Longhorn"
        run kubectl --kubeconfig="$KUBECONFIG_PATH" -n longhorn-system patch settings.longhorn.io \
            concurrent-replica-rebuild-per-node-limit \
            --type=merge -p '{"value":"5"}' || log_warn "patch concurrent-rebuild échoué"
    fi

    while IFS= read -r node; do
        log_info "Uncordon: $node"
        run kubectl --kubeconfig="$KUBECONFIG_PATH" uncordon "$node" || true
    done < <(kc_quiet get nodes -o name 2>/dev/null | sed 's|node/||')

    emit_event "restore" "done" "Cluster state restored"
    log_ok "Cluster restauré : rebuild ON, nodes uncordoned"
}

# =============================================================================
# Step 5: restart VMs (reverse order of shutdown, wait-for-ready between each)
# =============================================================================
step_restart_vms() {
    emit_event "vm-restart" "running" "Restart VMs (groupes parallèles entre eux, séquentiel inverse intra-groupe)"
    log_step "[5/5] Redémarrage VMs (groupes parallèles entre eux, ordre inverse intra-groupe)"

    if [[ "$SKIP_VM_RESTART" == "1" ]]; then
        emit_event "vm-restart" "skipped" "Skipped by user"
        log_warn "Redémarrage VM ignoré (--skip-vm-restart)"
        return 0
    fi

    # Get VMs in REVERSE order — groups with HIGHEST group_priority start
    # FIRST (mirror of shutdown). Within a normal group, REVERSE intra
    # order so the VM that was stopped last comes back up first.
    local halted_vms
    halted_vms=$(get_ordered_vms reverse | while IFS='|' read -r ns name intra snap timeout group gprio; do
        [[ -z "$name" ]] && continue
        local rs
        rs=$(kc_quiet -n "$ns" get vm "$name" -o jsonpath='{.spec.runStrategy}' 2>/dev/null || echo "")
        if [[ "$rs" == "Halted" ]]; then
            echo "$ns|$name|$intra|$snap|$timeout|$group|$gprio"
        fi
    done)
    local total
    total=$(echo "$halted_vms" | grep -c . || true)

    if [[ "$total" -eq 0 ]]; then
        emit_event "vm-restart" "done" "No halted VMs"
        log_ok "Aucune VM en état Halted"
        return 0
    fi

    log_info "$total VM(s) à démarrer — voir le plan ci-dessous"

    # ---------- Plan preview ----------
    {
        echo "==== Plan de démarrage (ordre inverse) ===="
        local last_gp=""
        echo "$halted_vms" | sort -t'|' -k7,7nr -k6,6 -k3,3nr | \
        while IFS='|' read -r ns name intra snap timeout group gprio; do
            if [[ "$gprio" != "$last_gp" ]]; then
                echo "─── Niveau group_priority=$gprio (groupes en parallèle) ───"
                last_gp="$gprio"
            fi
            if [[ "$group" == "default" ]]; then
                echo "    [default ⚡parallèle] $ns/$name"
            else
                echo "    [$group ▶intra=$intra] $ns/$name"
            fi
        done
        echo "==========================================="
    } >&2

    if [[ "$INTERACTIVE" == "1" ]]; then
        confirm "Démarrer ces $total VMs ?" \
            || { emit_event "vm-restart" "skipped" "operator skip"; return 0; }
    fi

    # ---------------------------------------------------------------------------
    # Algorithm (mirror of shutdown):
    #   Outer (SEQUENTIAL): each distinct group_priority level, DESCENDING
    #     Inner (PARALLEL): each group at that level
    #       If group == "default": all VMs in parallel
    #       Else: VMs sequentially in REVERSE intra order, wait-for-ready each
    # ---------------------------------------------------------------------------

    _start_one_sync() {
        local ns="$1" name="$2" timeout="$3"
        local target_rs
        target_rs=$(kc_quiet -n "$ns" get vm "$name" -o jsonpath="{.metadata.annotations['${ANNOT_PREV_RUNSTRATEGY//\//\\/}']}" 2>/dev/null || echo "")
        [[ -z "$target_rs" ]] && target_rs="Always"
        run kubectl --kubeconfig="$KUBECONFIG_PATH" patch vm "$name" -n "$ns" --type merge \
            -p "{\"spec\":{\"runStrategy\":\"${target_rs}\"}}" >/dev/null 2>&1 \
            || { log_warn "Échec start $ns/$name"; return 1; }
        [[ "$DRY_RUN" == "1" ]] && return 0
        wait_for_vm_ready "$ns" "$name" "${timeout:-$DEFAULT_READY_TIMEOUT}" \
            || log_warn "VM $ns/$name non Ready dans le délai — on continue"
        return 0
    }

    _process_group() {
        local gname="$1"; shift
        local specs=("$@")    # each: "intra|ns|name|timeout"
        local size=${#specs[@]}
        if [[ "$gname" == "default" ]]; then
            log_info "  [$gname ⚡parallèle] démarrage de $size VM(s) en parallèle"
            # Phase 1: fire all patches in parallel
            local pids=()
            for spec in "${specs[@]}"; do
                IFS='|' read -r _i ns name timeout <<< "$spec"
                log_info "    → fire start $ns/$name"
                _start_one_sync "$ns" "$name" "$timeout" &
                pids+=($!)
            done
            for p in "${pids[@]}"; do wait "$p" || true; done
        else
            log_info "  [$gname ▶séquentiel] $size VM(s) une par une (ordre inverse)"
            # specs already pre-sorted by intra DESC by the outer loop
            for spec in "${specs[@]}"; do
                IFS='|' read -r intra ns name timeout <<< "$spec"
                log_info "    → start $ns/$name (intra=$intra), wait ready…"
                _start_one_sync "$ns" "$name" "$timeout"
            done
        fi
        log_ok "  [$gname] terminé ($size VM(s))"
    }

    # Same accumulation pattern as shutdown — group VMs by (gprio, group)
    declare -A level_groups
    declare -A group_specs
    local ordered_levels=()

    while IFS='|' read -r ns name intra _snap timeout group gprio; do
        [[ -z "$name" ]] && continue
        local key="$gprio|$group"
        if [[ -z "${group_specs[$key]:-}" ]]; then
            group_specs[$key]="${intra}|${ns}|${name}|${timeout}"
            if [[ -z "${level_groups[$gprio]:-}" ]]; then
                level_groups[$gprio]="$group"
                ordered_levels+=("$gprio")
            else
                case $'\t'"${level_groups[$gprio]}"$'\t' in
                    *$'\t'"$group"$'\t'*) ;;
                    *) level_groups[$gprio]="${level_groups[$gprio]}"$'\t'"$group" ;;
                esac
            fi
        else
            group_specs[$key]="${group_specs[$key]}"$'\n'"${intra}|${ns}|${name}|${timeout}"
        fi
    done <<< "$halted_vms"

    # DESCENDING for startup (highest group_priority first — mirror of shutdown)
    IFS=$'\n' read -d '' -r -a ordered_levels < <(printf '%s\n' "${ordered_levels[@]}" | sort -nr | uniq && printf '\0') || true

    local level_idx=0 done_count=0
    for level in "${ordered_levels[@]}"; do
        level_idx=$((level_idx+1))
        local groups_in_level
        IFS=$'\t' read -ra groups_in_level <<< "${level_groups[$level]}"
        log_info "─── Niveau $level_idx (group_priority=$level) — ${#groups_in_level[@]} groupe(s) en parallèle ───"
        emit_event "vm-restart" "progress" "niveau $level_idx (gprio=$level): ${#groups_in_level[@]} groupes"

        local group_pids=()
        for gname in "${groups_in_level[@]}"; do
            local key="$level|$gname"
            local specs_str="${group_specs[$key]}"
            # REVERSE intra order for startup
            mapfile -t specs < <(printf '%s\n' "$specs_str" | sort -t'|' -k1,1nr)
            _process_group "$gname" "${specs[@]}" &
            group_pids+=($!)
            done_count=$((done_count + ${#specs[@]}))
        done
        for p in "${group_pids[@]}"; do wait "$p" || true; done
        log_ok "─── Niveau $level_idx terminé ───"
    done

    emit_event "vm-restart" "done" "$total VMs redémarrées"
    log_ok "$total VM(s) redémarrée(s)"
}

# =============================================================================
# Main
# =============================================================================
log_info "Procédure : power-CP1 → power-rest → wait-Ready → restore → restart-VMs"

step_power_first_cp;          interactive_pause
step_power_rest;              interactive_pause
step_wait_nodes_ready;        interactive_pause
step_restore_cluster_state;   interactive_pause
step_restart_vms

emit_event "complete" "done" "Cluster startup sequence complete"

cat >&2 <<EOF

${C_BOLD}${C_GREEN}╔══════════════════════════════════════════════════════════════╗
║  ✓ STARTUP COMPLETE — cluster: $(printf '%-31s' "$CLUSTER_NAME") ║
╚══════════════════════════════════════════════════════════════╝${C_RESET}

Log: $_log_file

EOF
