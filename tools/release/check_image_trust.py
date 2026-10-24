#!/usr/bin/env python3
"""Release check: trust store and Go toolchains of a built harvester-ops image.

Run at every release, after ./package.sh:

    python3 tools/release/check_image_trust.py localhost/harvester-ops:1.98.1 \
        [--bundles dist/harvester-ops-1.98.1/bundles]

Checks:
  1. the image trust store no longer holds a distrusted CA (emSign ECC Root
     CA - G3, emSign ECC Root CA - C3, emSign Root CA - C1, compromised in
     October 2026); emSign Root CA - G1, kept by Mozilla, is reported;
  2. every Go binary of the image (and of the embedded bundles) is built
     with a Go release that carries the fix for CVE-2026-111943 (Go 1.26.10,
     1.27.3 or later), unless it is listed in RESIDUAL with its reason;
  3. Python verifies TLS against the system store (no private bundle).

Exit code 0 when everything passes, 1 otherwise. Needs podman (or
CONTAINER_RUNTIME) and, for exact Go versions, a `go` binary on the host
(`go version` reads the build info of any Go binary); without it the
version is read from the binary's strings.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DISTRUSTED = (
    "emSign ECC Root CA - G3",
    "emSign ECC Root CA - C3",
    "emSign Root CA - C1",
)
KEPT = ("emSign Root CA - G1",)

# Smallest fixed patch per Go minor (go.dev/doc/devel/release, Go issue
# 82345: go1.26.10 and go1.27.3, 19/10/2026). Any later minor is fixed.
GO_FIXED = {(1, 26): 10, (1, 27): 3}
GO_FIRST_FIXED_MINOR_AFTER = (1, 28)

# Binaries knowingly shipped with an unfixed Go: the upstream has no release
# built with a fixed Go yet. Each entry pins the Go version it was accepted
# with, so an older build (or a forgotten version bump) still fails.
# Revisit at every release; drop an entry once its upstream is fixed.
RESIDUAL = {
    "kubectl": ("go1.26.8", "no Kubernetes release built with Go >= 1.26.10 "
                "yet (v1.35.9); reads the system trust store"),
    "yq": ("go1.22.5", "no yq release built with a fixed Go (latest v4.54.1: "
           "go1.27.1); yq makes no TLS connection"),
    "terraform-provider-harvester": ("go1.26.0", "no stable release built "
                                     "with a fixed Go (1.9.0); reads the "
                                     "system trust store"),
}


def classify(name, version):
    """'fixed', 'residual' or 'unfixed' for a Go binary of the release."""
    if go_is_fixed(version):
        return "fixed"
    accepted = RESIDUAL.get(name)
    if accepted and accepted[0] == version:
        return "residual"
    return "unfixed"


STORES = ("/etc/ssl/ca-bundle.pem", "/var/lib/ca-certificates/ca-bundle.pem")


def go_is_fixed(version):
    """True when a `goX.Y[.Z]` string carries the CVE-2026-111943 fix."""
    m = re.fullmatch(r"go(\d+)\.(\d+)(?:\.(\d+))?\S*", version or "")
    if not m:
        return False
    major, minor, patch = int(m[1]), int(m[2]), int(m[3] or 0)
    if (major, minor) >= GO_FIRST_FIXED_MINOR_AFTER:
        return True
    need = GO_FIXED.get((major, minor))
    return need is not None and patch >= need


def subjects_check(subjects_text):
    """(present distrusted CAs, present kept CAs) from `openssl pkcs7` output."""
    cns = set(re.findall(r"^subject=.*?CN\s*=\s*(.+?)\s*$", subjects_text, re.M))
    return sorted(c for c in DISTRUSTED if c in cns), sorted(c for c in KEPT if c in cns)


def go_version_of(path):
    """Go release a binary was built with, None for anything else."""
    with open(path, "rb") as f:
        if f.read(4) != b"\x7fELF":
            return None  # archives, scripts, manifests
    go = shutil.which("go")
    if go:
        r = subprocess.run([go, "version", str(path)], capture_output=True, text=True)
        v = r.stdout.rsplit(":", 1)[-1].strip() if r.returncode == 0 else ""
        return v if v.startswith("go") else None
    data = Path(path).read_bytes()
    if b"go:buildid" not in data and b"\xff Go buildinf:" not in data:
        return None
    m = re.search(rb"go1\.\d+(?:\.\d+)?", data)
    return m[0].decode() if m else "go?"


def run(runtime, image, script):
    return subprocess.run([runtime, "run", "--rm", "--entrypoint", "sh", image, "-c", script],
                          capture_output=True, text=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("image")
    ap.add_argument("--bundles", help="extracted bundles/ directory of the deliverable")
    args = ap.parse_args(argv)
    runtime = os.environ.get("CONTAINER_RUNTIME") or shutil.which("podman") or shutil.which("docker")
    failed = False

    print("== trust store")
    for store in STORES:
        r = run(runtime, args.image,
                f"openssl crl2pkcs7 -nocrl -certfile {store} | openssl pkcs7 -print_certs -noout")
        if r.returncode != 0:
            print(f"  {store}: unreadable ({r.stderr.strip()[:200]})")
            failed = True
            continue
        bad, kept = subjects_check(r.stdout)
        n = r.stdout.count("subject=")
        print(f"  {store}: {n} CAs; distrusted present: {bad or 'none'}; kept: {kept or 'none'}")
        failed |= bool(bad)
    r = run(runtime, args.image, "rpm -q ca-certificates-mozilla")
    print(f"  {r.stdout.strip()}")

    print("== Go binaries")
    with tempfile.TemporaryDirectory() as tmp:
        cid = subprocess.run([runtime, "create", args.image], capture_output=True, text=True,
                             check=True).stdout.strip()
        try:
            for d in ("/usr/local/bin",):
                subprocess.run([runtime, "cp", f"{cid}:{d}", tmp], check=True)
        finally:
            subprocess.run([runtime, "rm", cid], capture_output=True)
        files = [p for p in Path(tmp).rglob("*") if p.is_file() and not p.is_symlink()]
        if args.bundles:
            files += [p for p in Path(args.bundles).rglob("*") if p.is_file()]
        for p in sorted(files, key=lambda p: p.name):
            if p.stat().st_size < 1 << 20:
                continue
            v = go_version_of(p)
            if v is None:
                continue
            name = re.sub(r"_v?\d.*$", "", p.name)
            verdict = classify(name, v)
            if verdict == "fixed":
                print(f"  {p.name}: {v} fixed")
            elif verdict == "residual":
                print(f"  {p.name}: {v} RESIDUAL ({RESIDUAL[name][1]})")
            else:
                print(f"  {p.name}: {v} NOT FIXED")
                failed = True

    print("== Python")
    r = run(runtime, args.image,
            "python3 -c 'import ssl; p=ssl.get_default_verify_paths(); print(p.cafile, p.capath)'")
    paths = r.stdout.strip()
    print(f"  verify paths: {paths}")
    if not paths.startswith("/var/lib/ca-certificates/") and not paths.startswith("/etc/ssl/"):
        failed = True

    print("RESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
