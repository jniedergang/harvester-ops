#!/usr/bin/env bash
# harvester-ops — uninstaller
set -eo pipefail

PREFIX="/usr/local/bin"
CONF_DIR="/etc/harvester-ops"
LOG_DIR="/var/log/harvester-ops"
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
  --purge   Also remove $CONF_DIR and $LOG_DIR (default: keep them)
EOF
            exit 0 ;;
        *) shift ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

info "Stopping systemd service..."
systemctl disable --now harvester-ops 2>/dev/null || true
rm -f /etc/systemd/system/harvester-ops.service
systemctl daemon-reload 2>/dev/null || true

info "Removing container image..."
if command -v podman >/dev/null 2>&1; then
    podman rm -f harvester-ops 2>/dev/null || true
    podman rmi localhost/harvester-ops:latest 2>/dev/null || true
elif command -v docker >/dev/null 2>&1; then
    docker rm -f harvester-ops 2>/dev/null || true
    docker rmi harvester-ops:latest 2>/dev/null || true
fi

info "Removing CLI scripts..."
rm -f "$PREFIX/harvester-shutdown" "$PREFIX/harvester-startup" "$PREFIX/harvester-status"
rm -f "$PREFIX/harvester-shutdown.sh" "$PREFIX/harvester-startup.sh" "$PREFIX/harvester-status.sh"
rm -f "$PREFIX/lib/common.sh"
[[ -d "$PREFIX/lib" ]] && rmdir --ignore-fail-on-non-empty "$PREFIX/lib" 2>/dev/null || true

info "Removing install dir..."
rm -rf "$INSTALL_DIR"

if [[ "$PURGE" == "1" ]]; then
    warn "Purging configuration and logs..."
    rm -rf "$CONF_DIR" "$LOG_DIR"
    ok "Configuration and logs removed"
else
    info "Configuration preserved at $CONF_DIR/ (use --purge to delete)"
    info "Logs preserved at $LOG_DIR/"
fi

ok "harvester-ops uninstalled"
