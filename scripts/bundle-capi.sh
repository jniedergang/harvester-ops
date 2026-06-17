#!/usr/bin/env bash
# Build an airgap bundle of the CAPI / CAPHV stack.
#
# Usage:
#   scripts/bundle-capi.sh [--output dist/capi-bundle.tar.gz]
#
# Pulls every container image listed in scripts/capi-components.yaml, downloads
# the matching kubectl manifests, and packages them into a single tar.gz.
#
# On the target Harvester cluster, the harvester-ops UI (or the
# `harvester-capi-install.sh` script) consumes this bundle to:
#   1. `ctr images import` every .tar into containerd of each node
#   2. `kubectl apply -f manifests/*.yaml` in the documented order

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONF="$SCRIPT_DIR/capi-components.yaml"
OUT="${1:-$ROOT/dist/capi-bundle.tar.gz}"
RUNTIME="${CONTAINER_RUNTIME:-$(command -v podman || command -v docker)}"

[[ -n "$RUNTIME" ]] || { echo "podman or docker required" >&2; exit 1; }
[[ -f "$CONF" ]] || { echo "missing $CONF" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 required" >&2; exit 1; }
python3 -c 'import yaml' 2>/dev/null || { echo "python3 + PyYAML required (sudo zypper install python3-PyYAML)" >&2; exit 1; }

C_GREEN=$'\e[32m'; C_BLUE=$'\e[34m'; C_RED=$'\e[31m'; C_RESET=$'\e[0m'
info() { printf '%s==>%s %s\n' "$C_BLUE"  "$C_RESET" "$*"; }
ok()   { printf '%s ✓ %s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
err()  { printf '%s ✗ %s %s\n' "$C_RED"   "$C_RESET" "$*" >&2; }

WORK="$(mktemp -d)"
trap "rm -rf '$WORK'" EXIT
BUNDLE="$WORK/capi-bundle"
mkdir -p "$BUNDLE/images" "$BUNDLE/manifests" "$BUNDLE/clusterclass"

info "Container runtime: $RUNTIME"
info "Output bundle: $OUT"

# Inventory: emit one record per (component, kind, value) so the shell can
# iterate. Replaces yq usage — Python + PyYAML is portable across SLES/RHEL
# and already required by harvester-ops for app.py.
#
# Output format (tab-separated):
#   <component>\t<kind:image|manifest>\t<value>\t[<extra>]
#       image:    value = image ref
#       manifest: value = URL,  extra = manifest name
inventory_file="$WORK/inventory.tsv"
python3 - "$CONF" > "$inventory_file" <<'PY'
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f) or {}
for comp, body in data.items():
    if not isinstance(body, dict):
        continue
    for m in body.get("manifests") or []:
        name = m.get("name", "")
        url = m.get("url", "")
        if url:
            print(f"{comp}\tmanifest\t{url}\t{name}")
    for img in body.get("images") or []:
        print(f"{comp}\timage\t{img}\t")
PY

# ----------------------------------------------------------------------------
# 1. Download manifests
# ----------------------------------------------------------------------------
info "Downloading manifests..."
while IFS=$'\t' read -r comp kind value extra; do
    [[ "$kind" == "manifest" ]] || continue
    mkdir -p "$BUNDLE/manifests/$comp"
    echo "  • $comp/$extra"
    curl -fsSL "$value" -o "$BUNDLE/manifests/$comp/$extra.yaml" \
        || { err "failed to download $value"; exit 1; }
done < "$inventory_file"
ok "Manifests downloaded"

# ----------------------------------------------------------------------------
# 2. Pull + save images
# ----------------------------------------------------------------------------
info "Pulling and saving container images (this can take a few minutes)..."
manifest_index="$BUNDLE/manifest.json"
# Seed manifest.json with bundle metadata + per-component versions extracted
# from capi-components.yaml. The UI inspect overlay reads this directly.
python3 - "$CONF" "$manifest_index" <<'PY'
import json, os, sys, time, yaml
conf, out = sys.argv[1], sys.argv[2]
with open(conf) as f:
    data = yaml.safe_load(f) or {}
meta = data.pop("bundle_metadata", {}) or {}
components = []
for comp, body in data.items():
    if not isinstance(body, dict):
        continue
    components.append({
        "name": comp,
        "version": body.get("version", ""),
        "image_count": len(body.get("images") or []),
        "manifest_count": len(body.get("manifests") or []),
    })
manifest = {
    "version": "1.2.0",
    "bundle": {
        "created_at": int(time.time()),
        "created_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": os.uname().nodename,
        "compatible_harvester_versions":
            meta.get("compatible_harvester_versions") or [],
        "notes": meta.get("notes", ""),
    },
    "components": components,
    "images": [],
}
with open(out, "w") as f:
    json.dump(manifest, f, indent=2)
PY

while IFS=$'\t' read -r comp kind value extra; do
    [[ "$kind" == "image" ]] || continue
    img="$value"
    safe=$(echo "$img" | tr '/:' '__')
    tar="$BUNDLE/images/${safe}.tar"
    echo "  • $img"
    "$RUNTIME" pull --quiet "$img" >/dev/null
    "$RUNTIME" save -o "$tar" "$img"
    gzip "$tar"
    python3 - "$manifest_index" "$comp" "$img" "images/${safe}.tar.gz" <<'PY'
import json, sys
path, comp, img, file = sys.argv[1:5]
with open(path) as f:
    data = json.load(f)
data.setdefault("images", []).append({"component": comp, "name": img, "file": file})
with open(path, "w") as f:
    json.dump(data, f, indent=2)
PY
done < "$inventory_file"
ok "Images bundled ($(ls "$BUNDLE/images" | wc -l) files)"

# ----------------------------------------------------------------------------
# 3. Local ClusterClass + templates (from CAPHV repo if present)
# ----------------------------------------------------------------------------
# Search several plausible locations for the plain-YAML ClusterClass
# bundle. Override with CAPHV_REPO env var to point at any local clone.
caphv_candidates=(
    "${CAPHV_REPO:-}"
    "$ROOT/../CAPHV"
    "$HOME/workspace/CAPHV"
    "$HOME/workspace/RESEARCH/caphv"
)
clusterclass_src=""
for cand in "${caphv_candidates[@]}"; do
    [[ -z "$cand" ]] && continue
    # Preferred: the plain-YAML templates directory (no Helm placeholders).
    if [[ -d "$cand/templates/clusterclass/rke2" ]]; then
        clusterclass_src="$cand/templates/clusterclass/rke2"
        break
    fi
done
if [[ -n "$clusterclass_src" ]]; then
    info "Including ClusterClass templates from $clusterclass_src"
    cp "$clusterclass_src"/*.yaml "$BUNDLE/clusterclass/" 2>/dev/null || true
    cc_count=$(ls "$BUNDLE/clusterclass" 2>/dev/null | wc -l)
    ok "ClusterClass templates copied ($cc_count files)"
    # Patch the manifest_count on the caphv-clusterclass entry so the inspect
    # overlay shows it, since these are real files (not container images).
    python3 - "$manifest_index" "$cc_count" <<'PY'
import json, sys
path = sys.argv[1]; count = int(sys.argv[2])
with open(path) as f: data = json.load(f)
for c in data.get("components", []):
    if c.get("name") == "caphv-clusterclass":
        c["manifest_count"] = count
        break
with open(path, "w") as f: json.dump(data, f, indent=2)
PY
else
    info "No ClusterClass source found — bundle ships without it (set CAPHV_REPO to fix)"
fi

# ----------------------------------------------------------------------------
# 4. README inside the bundle
# ----------------------------------------------------------------------------
cat > "$BUNDLE/README.md" <<'EOF'
# CAPI / CAPHV airgap bundle

Generated by harvester-ops `scripts/bundle-capi.sh`.

## Contents

```
manifest.json              Index of all images + their files
manifests/<comp>/*.yaml    Kubernetes manifests per component
images/*.tar.gz            OCI images, one per file (gzipped)
clusterclass/              ClusterClass + templates (local to this build)
```

## Installation order

1. **cert-manager**       (if not already present on the target cluster)
2. **cluster-api**        (CAPI core CRDs + controller)
3. **cabp-rke2**          (RKE2 bootstrap provider)
4. **cacp-rke2**          (RKE2 control-plane provider)
5. **caphv**              (Harvester infrastructure provider)
6. **clusterclass**       (kubectl apply -f clusterclass/)

Use the harvester-ops UI (Automation → Cluster API → Install stack) or the
`harvester-capi-install.sh` helper to run the installation. Both:
- load every `.tar.gz` into containerd on every node of the target Harvester cluster
- apply every manifest in the order above
- wait for each controller deployment to become Available

## Image push to a private registry (optional)

If you have a local registry and prefer to push instead of `ctr import`:

```bash
for f in images/*.tar.gz; do
    img=$(gunzip -c "$f" | tar tf - | grep manifest.json | head -1)
    podman load -i "$f"
done
# Then podman tag + push as needed
```
EOF

# ----------------------------------------------------------------------------
# 5. Tarball
# ----------------------------------------------------------------------------
info "Creating tarball..."
mkdir -p "$(dirname "$OUT")"
tar -C "$WORK" -czf "$OUT" "$(basename "$BUNDLE")"
SIZE=$(du -h "$OUT" | awk '{print $1}')
SHA=$(sha256sum "$OUT" | awk '{print $1}')
echo "$SHA  $(basename "$OUT")" > "$OUT.sha256"

cat <<EOF

${C_GREEN}╔════════════════════════════════════════════════════════════╗
║                ✓ CAPI bundle ready                         ║
╚════════════════════════════════════════════════════════════╝${C_RESET}

  Tarball: $OUT  ($SIZE)
  SHA-256: $SHA
  Contents:
$(tar -tzf "$OUT" | sed 's/^/    /' | head -20)
$(test "$(tar -tzf "$OUT" | wc -l)" -gt 20 && echo "    ... $(tar -tzf "$OUT" | wc -l) total entries")

To install on the target cluster:
  cd \$(dirname $(basename "$OUT")) && tar xzf $(basename "$OUT")
  scripts/harvester-capi-install.sh capi-bundle/

EOF
