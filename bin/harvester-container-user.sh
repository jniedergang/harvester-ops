#!/usr/bin/env bash
# Docker has no --passwd-entry. Preserve image accounts and add the host user
# so ssh/ssh-keygen can resolve its numeric uid with a read-only container root.
set -euo pipefail
runtime="${1:?container runtime executable required}"
state="${HARVESTER_OPS_STATE_DIR:-/var/lib/harvester-ops}"
image="localhost/harvester-ops:latest"
uid=$(id -u harvester-ops)
gid=$(id -g harvester-ops)
install -d -m 0750 -o harvester-ops -g harvester-ops "$state/.nss"
work=$(mktemp -d "$state/.nss-build.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
"$runtime" run --rm --network none --read-only --entrypoint /bin/sh "$image" -c 'cat /etc/passwd' > "$work/passwd.base"
"$runtime" run --rm --network none --read-only --entrypoint /bin/sh "$image" -c 'cat /etc/group' > "$work/group.base"
awk -F: -v uid="$uid" '$1 != "harvester-ops" && $3 != uid' "$work/passwd.base" > "$work/passwd"
printf 'harvester-ops:x:%s:%s:harvester-ops web UI:/var/lib/harvester-ops:/bin/sh\n' "$uid" "$gid" >> "$work/passwd"
awk -F: -v gid="$gid" '$1 != "harvester-ops" && $3 != gid' "$work/group.base" > "$work/group"
printf 'harvester-ops:x:%s:\n' "$gid" >> "$work/group"
install -m 0644 -o harvester-ops -g harvester-ops "$work/passwd" "$state/.nss/passwd"
install -m 0644 -o harvester-ops -g harvester-ops "$work/group" "$state/.nss/group"
