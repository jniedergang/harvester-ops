#!/bin/bash
# harvester-ops : publie le site de présentation et sa démo sur GitHub Pages
# (v1.86.0). Construit dist/site depuis le commit courant, puis le pose seul
# sur la branche gh-pages (un commit par publication) et la pousse sur le
# remote donné. GitHub Pages sert cette branche à la racine, une fois Pages
# activé sur elle dans les réglages du dépôt (le site public a été retiré le
# 08/10/2026 : ne relancer qu'à la demande de l'auteur).
#
#   tools/demo-site/publish-pages.sh [remote]        # défaut : github
set -euo pipefail
REMOTE="${1:-github}"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
[ -z "$(git status --porcelain -- site tools/demo-site web VERSION)" ] || {
    echo "modifications non commitées dans site/, tools/demo-site/, web/ ou VERSION : commiter d'abord" >&2; exit 2; }
VERSION="$(cat VERSION)"
COMMIT="$(git rev-parse --short HEAD)"
python3 tools/demo-site/build.py --out dist/site
WT="$(mktemp -d)"
trap 'git worktree remove --force "$WT" >/dev/null 2>&1 || true; rm -rf "$WT"' EXIT
if git ls-remote --exit-code "$REMOTE" gh-pages >/dev/null 2>&1; then
    git fetch -q "$REMOTE" gh-pages
    git worktree add -q --detach "$WT" FETCH_HEAD
else
    git worktree add -q --detach "$WT"
    git -C "$WT" checkout -q --orphan gh-pages-new
fi
find "$WT" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a dist/site/. "$WT/"
git -C "$WT" add -A
if git -C "$WT" diff --cached --quiet; then
    echo "site inchangé, rien à publier"; exit 0
fi
git -C "$WT" commit -q -m "site: v$VERSION ($COMMIT)"
git -C "$WT" push -q "$REMOTE" HEAD:refs/heads/gh-pages
echo "publié : v$VERSION ($COMMIT) -> $REMOTE gh-pages"
