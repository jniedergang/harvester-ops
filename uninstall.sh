#!/usr/bin/env bash
# harvester-ops — uninstaller
set -eo pipefail

PREFIX="/usr/local/bin"
CONF_DIR="/etc/harvester-ops"
LOG_DIR="/var/log/harvester-ops"
STATE_DIR="/var/lib/harvester-ops"
INSTALL_DIR="/opt/harvester-ops"
PURGE=0

C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
info()  { printf '%s[INFO]%s %s\n' '\e[34m'  "$C_RESET" "$*"; }
ok()    { printf '%s[ OK ]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%s[WARN]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        -h|--help)
            cat <<EOF
Usage: uninstall.sh [--purge]
  --purge   Also remove $CONF_DIR, $LOG_DIR, $STATE_DIR, the
            harvester-ops system account, and the OpenBao of mode B1 (its
            data, unseal keys and account) (default: keep them)
EOF
            exit 0 ;;
        *) shift ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

info "Stopping systemd service..."
systemctl disable --now harvester-ops 2>/dev/null || true
systemctl disable --now harvester-ops-update.path 2>/dev/null || true
rm -f /etc/systemd/system/harvester-ops.service /etc/systemd/system/harvester-ops-update.service /etc/systemd/system/harvester-ops-update.path
rm -rf /var/lib/harvester-ops-update
# 2.9.0 : agent des secrets et OpenBao du mode B1. Ses données (raft,
# journaux d'audit) et ses clés (/etc/harvester-ops-openbao) restent sauf
# --purge : elles portent les secrets migrés.
systemctl disable --now harvester-secrets-agent.path harvester-secrets-rotate.timer harvester-ops-openbao 2>/dev/null || true
rm -f /etc/systemd/system/harvester-secrets-agent.path /etc/systemd/system/harvester-secrets-agent.service \
      /etc/systemd/system/harvester-secrets-rotate.timer /etc/systemd/system/harvester-ops-openbao.service \
      /etc/logrotate.d/harvester-ops-openbao
rm -rf /var/lib/harvester-secrets-agent /run/hops-openbao-boot
systemctl daemon-reload 2>/dev/null || true

info "Removing container image..."
# Both runtimes: a host may carry Docker and Podman, and the console runs in
# the one its unit names. The previous tag is the update agent's rollback copy.
for runtime in podman docker; do
    command -v "$runtime" >/dev/null 2>&1 || continue
    "$runtime" rm -f harvester-ops >/dev/null 2>&1 || true
    for image in localhost/harvester-ops:latest localhost/harvester-ops:previous harvester-ops:latest; do
        "$runtime" rmi "$image" >/dev/null 2>&1 || true
    done
    "$runtime" rm -f harvester-ops-openbao >/dev/null 2>&1 || true
done
rm -rf /run/harvester-ops-nss

info "Removing CLI scripts..."
rm -f "$PREFIX/harvester-shutdown" "$PREFIX/harvester-startup" "$PREFIX/harvester-status"
rm -f "$PREFIX/harvester-shutdown.sh" "$PREFIX/harvester-startup.sh" "$PREFIX/harvester-status.sh"
rm -f "$PREFIX/harvester-container-user.sh"
rm -f "$PREFIX/lib/common.sh"
[[ -d "$PREFIX/lib" ]] && rmdir --ignore-fail-on-non-empty "$PREFIX/lib" 2>/dev/null || true

info "Removing install dir..."
rm -rf "$INSTALL_DIR"

if [[ "$PURGE" == "1" ]]; then
    warn "Purging configuration, logs and state..."
    rm -rf "$CONF_DIR" "$LOG_DIR" "$STATE_DIR"
    # 2.9.0 : OpenBao du mode B1 (données, clés, secrets podman, image, compte)
    rm -rf /etc/harvester-ops-openbao /var/lib/harvester-ops-openbao /var/lib/harvester-ops-openbao.old-* \
           /var/lib/harvester-ops-openbao.removed-* /etc/harvester-ops-openbao.removed-*
    if command -v podman >/dev/null 2>&1; then
        podman secret rm hops-openbao-unseal hops-openbao-unseal-prev >/dev/null 2>&1 || true
        podman images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep '^localhost/harvester-ops-openbao:' \
            | xargs -r podman rmi >/dev/null 2>&1 || true
    fi
    if id -u harvester-ops-openbao >/dev/null 2>&1; then
        userdel harvester-ops-openbao 2>/dev/null || true
    fi
    if id -u harvester-ops >/dev/null 2>&1; then
        userdel harvester-ops 2>/dev/null || true
    fi
    if getent group harvester-ops >/dev/null; then
        groupdel harvester-ops 2>/dev/null || true
    fi
    ok "Configuration, logs, state and the harvester-ops account removed"
else
    info "Configuration preserved at $CONF_DIR/ (use --purge to delete)"
    info "Logs preserved at $LOG_DIR/, state at $STATE_DIR/"
fi

ok "harvester-ops uninstalled"
