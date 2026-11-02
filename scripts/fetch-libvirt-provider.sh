#!/usr/bin/env bash
# 2.3.0 : le provider Terraform dmacvicar/libvirt (Apache-2.0), embarqué dans
# le livrable pour les déclarations qui visent un hôte KVM.
#
#   fetch-libvirt-provider.sh VERSION FINGERPRINT KEYFILE DEST [LABEL]
#
# Télécharge la release officielle et la VÉRIFIE avant de la poser :
#   1. KEYFILE ne porte que la clé épinglée (empreinte FINGERPRINT, 40 car.) ;
#   2. la signature GPG du fichier SHA256SUMS est bonne et faite par CETTE clé
#      (champ VALIDSIG, clé primaire) ;
#   3. la somme de l'archive linux_<arch> est celle du SHA256SUMS signé.
# Résultat : DEST/bin/terraform-provider-libvirt et DEST/provider.json
# (version, sha256 de l'archive, empreinte du signataire, provenance).
# LIBVIRT_PROVIDER_BASE_URL remplace l'adresse des releases (miroir, essais).
set -euo pipefail

die() { echo "fetch-libvirt-provider: $*" >&2; exit 1; }

[[ $# -ge 4 ]] || die "usage: $0 VERSION FINGERPRINT KEYFILE DEST [LABEL]"
version="$1"; fpr="$2"; key="$3"; dest="$4"; label="${5:-}"
arch="${ARCH:-amd64}"
base="${LIBVIRT_PROVIDER_BASE_URL:-https://github.com/dmacvicar/terraform-provider-libvirt/releases/download}"

[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid version: $version"
[[ "$fpr" =~ ^[0-9A-F]{40}$ ]] || die "invalid pinned fingerprint"
[[ "$arch" =~ ^(amd64|arm64)$ ]] || die "unsupported architecture: $arch"
[[ -f "$key" ]] || die "signing key file not found: $key"
command -v gpg >/dev/null || die "gpg is required to verify the release signature"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
name="terraform-provider-libvirt_${version}"
zip="${name}_linux_${arch}.zip"
for f in "${name}_SHA256SUMS" "${name}_SHA256SUMS.sig" "$zip"; do
    curl -fsSL --retry 3 -o "$work/$f" "$base/v${version}/$f" || die "download failed: $f"
done

gh="$work/gnupg"
mkdir -m 0700 "$gh"
gpg --homedir "$gh" --batch --quiet --import "$key" 2>/dev/null || die "cannot import $key"
primaries="$(gpg --homedir "$gh" --batch --with-colons --list-keys \
    | awk -F: '$1=="pub"{p=1; next} $1=="fpr" && p{print $10; p=0}')"
[[ "$primaries" == "$fpr" ]] || die "the key file must hold only the pinned key $fpr"

status="$(gpg --homedir "$gh" --batch --status-fd 1 \
    --verify "$work/${name}_SHA256SUMS.sig" "$work/${name}_SHA256SUMS" 2>/dev/null)" \
    || die "bad signature on ${name}_SHA256SUMS"
signer="$(awk '$2=="VALIDSIG"{print $NF}' <<<"$status")"
[[ "$signer" == "$fpr" ]] || die "SHA256SUMS signed by ${signer:-nobody}, not by the pinned key"

expected="$(awk -v f="$zip" '$2==f || $2=="*"f {print $1}' "$work/${name}_SHA256SUMS")"
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || die "$zip is not listed in the signed SHA256SUMS"
actual="$(sha256sum "$work/$zip" | awk '{print $1}')"
[[ "$actual" == "$expected" ]] || die "checksum mismatch for $zip"

member="terraform-provider-libvirt_v${version}"
unzip -p "$work/$zip" "$member" > "$work/bin" 2>/dev/null || die "$member missing from $zip"
[[ "$(head -c4 "$work/bin" | od -An -tx1 | tr -d ' \n')" == "7f454c46" ]] || die "$member is not an ELF binary"

mkdir -p "$dest/bin"
install -m 0755 "$work/bin" "$dest/bin/terraform-provider-libvirt"
python3 - "$dest/provider.json" "$version" "$actual" "$fpr" "${label:-$base/v${version}/$zip}" "$arch" <<'PY'
import json, sys, time
path, version, sha, fpr, source, arch = sys.argv[1:7]
json.dump({"name": "dmacvicar/libvirt", "version": version, "sha256": sha,
           "signer_fingerprint": fpr, "source": source, "arch": arch,
           "installed_at": time.time()}, open(path, "w"), indent=2)
PY
echo "terraform-provider-libvirt v${version}: signature by ${fpr} and checksum verified"
