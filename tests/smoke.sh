#!/usr/bin/env bash
# Minimal smoke tests — no real cluster required.
# Verifies that scripts at least parse, expose --help, and fail safely.

set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

FAIL=0
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; FAIL=1; }

echo "[1/4] Bash syntax"
for f in bin/lib/common.sh bin/harvester-shutdown.sh bin/harvester-startup.sh bin/harvester-status.sh install.sh uninstall.sh package.sh; do
    if bash -n "$ROOT/$f"; then ok "syntax $f"; else fail "syntax $f"; fi
done

echo "[2/4] CLI --help"
for s in harvester-shutdown harvester-startup harvester-status; do
    out=$(bash "$ROOT/bin/${s}.sh" --help 2>&1 || true)
    if echo "$out" | grep -q -i 'usage'; then ok "$s --help"; else fail "$s --help"; fi
done

echo "[3/4] CLI refuses destructive without flags"
out=$(bash "$ROOT/bin/harvester-shutdown.sh" --cluster fake 2>&1 || true)
if echo "$out" | grep -q -i 'config'; then ok "shutdown fails on missing config"; else fail "shutdown should fail without config"; fi

echo "[4/4] Python web app parses"
if python3 -c "import ast; ast.parse(open('$ROOT/web/app.py').read())"; then ok "app.py parses"; else fail "app.py parse"; fi

echo
if [[ "$FAIL" == "1" ]]; then echo "FAILED"; exit 1; else echo "OK"; fi
