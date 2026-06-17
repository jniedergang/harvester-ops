#!/usr/bin/env bash
# bundle.sh — build the harvester-ops airgap bundles.
#
# Usage:
#   ./bundle.sh                # build all bundles (default)
#   ./bundle.sh capi           # only the CAPI / CAPHV stack bundle
#   ./bundle.sh harvester-ops  # only the harvester-ops install tarball
#   ./bundle.sh --help
#
# Prerequisites (checked at start):
#   - bash, curl, tar, sha256sum, python3
#   - podman or docker (for image pulls)
#   - yq (mikefarah, v4+)
#
# Outputs in dist/:
#   harvester-ops-<version>.tar.gz   the harvester-ops tarball itself
#   capi-bundle.tar.gz               the CAPI / CAPHV airgap stack

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST="$SCRIPT_DIR/dist"
VERSION="$(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo dev)"

# ---------------------------------------------------------------------------
# Colors / messaging
# ---------------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    C_BOLD=$'\e[1m'; C_GREEN=$'\e[32m'; C_BLUE=$'\e[34m'
    C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
else
    C_BOLD="" C_GREEN="" C_BLUE="" C_YELLOW="" C_RED="" C_RESET=""
fi

info()  { printf '%s==>%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s ✓ %s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s !!%s  %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()   { printf '%s ✗ %s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

usage() {
    cat <<EOF
${C_BOLD}harvester-ops bundle builder${C_RESET}  (v$VERSION)

Build airgap-ready tarballs for offline installation.

${C_BOLD}Usage:${C_RESET}
  ./bundle.sh [target]

${C_BOLD}Targets:${C_RESET}
  all              build every target (default)
  capi             build the CAPI / CAPHV stack bundle  → dist/capi-bundle.tar.gz
  harvester-ops    build the main harvester-ops tarball → dist/harvester-ops-<version>.tar.gz

${C_BOLD}Prerequisites:${C_RESET}
  bash, curl, tar, sha256sum  (POSIX userland)
  podman or docker             (for container image pulls)
  yq v4+ (mikefarah), jq       (YAML/JSON parsing)

${C_BOLD}Environment overrides:${C_RESET}
  CONTAINER_RUNTIME=podman|docker  (default: auto-detect)
  SKIP_IMAGE_BUILD=1               (skip container image build for harvester-ops)
  SKIP_WHEELS=1                    (skip Python wheels download for harvester-ops)

EOF
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
check_prereqs() {
    info "Checking prerequisites..."
    local missing=()
    for cmd in bash curl tar sha256sum yq jq; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing commands: ${missing[*]}"
        err "Install them (on SLES/openSUSE):"
        err "  sudo zypper install -y curl tar coreutils jq"
        err "  curl -L https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -o /usr/local/bin/yq && chmod +x /usr/local/bin/yq"
        exit 2
    fi
    if [[ -z "${CONTAINER_RUNTIME:-}" ]]; then
        if command -v podman >/dev/null 2>&1; then
            export CONTAINER_RUNTIME=podman
        elif command -v docker >/dev/null 2>&1; then
            export CONTAINER_RUNTIME=docker
        else
            err "podman or docker required (for container image pulls)"
            exit 2
        fi
    fi
    ok "All prerequisites found. Runtime: $CONTAINER_RUNTIME"
}

build_capi() {
    info "Building CAPI / CAPHV airgap bundle..."
    mkdir -p "$DIST"
    bash "$SCRIPT_DIR/scripts/bundle-capi.sh" "$DIST/capi-bundle.tar.gz"
}

build_harvester_ops() {
    info "Building harvester-ops main tarball..."
    bash "$SCRIPT_DIR/package.sh"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TARGET="${1:-all}"
case "$TARGET" in
    -h|--help|help) usage; exit 0 ;;
esac

check_prereqs
mkdir -p "$DIST"

case "$TARGET" in
    all)
        build_capi
        build_harvester_ops
        ;;
    capi)
        build_capi
        ;;
    harvester-ops|main)
        build_harvester_ops
        ;;
    *)
        err "Unknown target: $TARGET"
        usage
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
cat <<EOF

${C_GREEN}${C_BOLD}╔════════════════════════════════════════════════════════════╗
║                ✓ Bundles ready                             ║
╚════════════════════════════════════════════════════════════╝${C_RESET}

Output directory: ${C_BOLD}$DIST${C_RESET}

EOF
ls -lh "$DIST"/*.tar.gz 2>/dev/null | awk '{printf "  %-12s  %s\n", $5, $NF}'

cat <<EOF

To consume on a client host:
  1. Copy *.tar.gz to the client
  2. Verify checksums:    sha256sum -c *.tar.gz.sha256
  3. Install harvester-ops: tar xzf harvester-ops-*.tar.gz && cd harvester-ops-*/ && sudo ./install.sh
  4. CAPI bundle: keep alongside, or the UI auto-detects it at dist/capi-bundle.tar.gz

EOF
