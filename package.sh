#!/usr/bin/env bash
# harvester-ops — packager
# Builds an airgap-ready tarball: harvester-ops-<version>.tar.gz
#
# Steps:
#   1. Build the container image (FROM registry.suse.com/bci/python:3.11)
#   2. Save the image as OCI tar (images/harvester-ops-ui.tar)
#   3. Pre-download Python wheels into web/vendor/ for offline install
#   4. Build the tarball
#   5. Generate SHA-256 checksum

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION="$(cat VERSION)"
NAME="harvester-ops-${VERSION}"
DIST_DIR="${SCRIPT_DIR}/dist"
WORK_DIR="${DIST_DIR}/${NAME}"
RUNTIME="${CONTAINER_RUNTIME:-$(command -v podman || command -v docker)}"
SKIP_IMAGE_BUILD="${SKIP_IMAGE_BUILD:-0}"
SKIP_WHEELS="${SKIP_WHEELS:-0}"

C_GREEN=$'\e[32m'; C_BLUE=$'\e[34m'; C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
info() { printf '%s==>%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s ✓ %s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s !!%s  %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()  { printf '%s ✗ %s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

[[ -n "$RUNTIME" ]] || { err "podman or docker required"; exit 1; }

info "Packaging harvester-ops version ${VERSION}"
info "Container runtime: $RUNTIME"

# -----------------------------------------------------------------------------
# 0. Clean & prepare
# -----------------------------------------------------------------------------
rm -rf "$WORK_DIR"
mkdir -p "$WORK_DIR"

# -----------------------------------------------------------------------------
# 1. Wheels for offline pip install
# -----------------------------------------------------------------------------
if [[ "$SKIP_WHEELS" == "1" ]]; then
    warn "SKIP_WHEELS=1 — not bundling Python wheels"
else
    info "Downloading Python wheels for offline install..."
    rm -rf web/vendor
    mkdir -p web/vendor
    pip download -r web/requirements.txt -d web/vendor --no-deps >/dev/null
    pip download -r web/requirements.txt -d web/vendor >/dev/null
    ok "Wheels in web/vendor/ ($(ls web/vendor | wc -l) files)"
fi

# -----------------------------------------------------------------------------
# 2. Build container image
# -----------------------------------------------------------------------------
if [[ "$SKIP_IMAGE_BUILD" == "1" ]]; then
    warn "SKIP_IMAGE_BUILD=1 — skipping image build"
else
    info "Building container image (FROM registry.suse.com/bci/python:3.11)..."
    "$RUNTIME" build -t "harvester-ops:${VERSION}" -t "harvester-ops:latest" \
        -f container/Containerfile .
    ok "Image built: harvester-ops:${VERSION}"

    info "Saving OCI image to images/harvester-ops-ui.tar..."
    mkdir -p images
    "$RUNTIME" save -o "images/harvester-ops-ui.tar" "localhost/harvester-ops:latest"
    ok "Image saved ($(du -h images/harvester-ops-ui.tar | awk '{print $1}'))"
fi

# -----------------------------------------------------------------------------
# 3. Assemble tarball content
# -----------------------------------------------------------------------------
info "Assembling deliverable..."
cp -r bin web container config docs "$WORK_DIR/"
cp README.md VERSION install.sh uninstall.sh package.sh "$WORK_DIR/"
[[ -f LICENSE ]] && cp LICENSE "$WORK_DIR/"
[[ -f CHANGELOG.md ]] && cp CHANGELOG.md "$WORK_DIR/"

if [[ -d images ]]; then
    mkdir -p "$WORK_DIR/images"
    cp -v images/*.tar "$WORK_DIR/images/" 2>/dev/null || true
fi

# Strip wheels from source tree but include in package
[[ -d web/vendor ]] && cp -r web/vendor "$WORK_DIR/web/vendor"

ok "Working tree assembled at $WORK_DIR"

# -----------------------------------------------------------------------------
# 4. Tarball + checksum
# -----------------------------------------------------------------------------
info "Creating tarball..."
tar -C "$DIST_DIR" -czf "${DIST_DIR}/${NAME}.tar.gz" "${NAME}"
SHA="$(sha256sum "${DIST_DIR}/${NAME}.tar.gz" | awk '{print $1}')"
echo "$SHA  ${NAME}.tar.gz" > "${DIST_DIR}/${NAME}.tar.gz.sha256"
SIZE=$(du -h "${DIST_DIR}/${NAME}.tar.gz" | awk '{print $1}')

# -----------------------------------------------------------------------------
# 5. Summary
# -----------------------------------------------------------------------------
cat <<EOF

${C_GREEN}╔════════════════════════════════════════════════════════════╗
║                ✓ Packaging complete                        ║
╚════════════════════════════════════════════════════════════╝${C_RESET}

  Tarball:  ${DIST_DIR}/${NAME}.tar.gz   (${SIZE})
  SHA-256:  ${SHA}
  Manifest: ${DIST_DIR}/${NAME}.tar.gz.sha256

Transfer to the client host, then:

  sha256sum -c ${NAME}.tar.gz.sha256
  tar xzf ${NAME}.tar.gz
  cd ${NAME}
  sudo ./install.sh

EOF
