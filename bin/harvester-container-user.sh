#!/usr/bin/env bash
# Docker has no --passwd-entry. Preserve image accounts and add the host user
# so ssh/ssh-keygen can resolve its numeric uid with a read-only container root.
#
# Runs as root (ExecStartPre) before each start. The files are built and kept
# in a root-owned directory that the console account cannot write:
# RuntimeDirectory=harvester-ops-nss, recreated by systemd at each start. No
# path under the console state directory is used, so the account cannot point
# what root writes, or what the Docker daemon mounts, somewhere else.
set -euo pipefail
runtime="${1:?container runtime executable required}"
dir="${HARVESTER_OPS_NSS_DIR:-/run/harvester-ops-nss}"
image="localhost/harvester-ops:latest"
uid=$(id -u harvester-ops)
gid=$(id -g harvester-ops)
# A real directory, owned by us (root in production) and writable by nobody else.
if [[ -L "$dir" || ! -d "$dir" || ! -O "$dir" ]] || [[ -n "$(find "$dir" -maxdepth 0 -perm /022)" ]]; then
    echo "harvester-container-user: $dir must be a directory owned by $(id -un) and writable only by it" >&2
    exit 1
fi
umask 022
work=$(mktemp -d "$dir/.build.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
# One container for both files; the marker line has no colon, so it cannot be
# an account or a group.
"$runtime" run --rm --network none --read-only --entrypoint /bin/sh "$image" \
    -c 'cat /etc/passwd; echo harvester-ops-group-file; cat /etc/group' > "$work/image"
grep -qx harvester-ops-group-file "$work/image" || { echo "harvester-container-user: unreadable image accounts" >&2; exit 1; }
awk -F: -v uid="$uid" '$0 == "harvester-ops-group-file" { exit } $1 != "harvester-ops" && $3 != uid' "$work/image" > "$work/passwd"
printf 'harvester-ops:x:%s:%s:harvester-ops web UI:/var/lib/harvester-ops:/bin/sh\n' "$uid" "$gid" >> "$work/passwd"
awk -F: -v gid="$gid" 'seen && $1 != "harvester-ops" && $3 != gid; $0 == "harvester-ops-group-file" { seen = 1 }' "$work/image" > "$work/group"
printf 'harvester-ops:x:%s:\n' "$gid" >> "$work/group"
chmod 0644 "$work/passwd" "$work/group"
# rename(2) in the same directory: atomic, and it replaces a link instead of following it.
mv -fT "$work/passwd" "$dir/passwd"
mv -fT "$work/group" "$dir/group"
