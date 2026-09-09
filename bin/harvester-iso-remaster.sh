#!/usr/bin/env bash
# harvester-iso-remaster.sh — prépare un ISO Harvester pour une installation
# automatique (v1.18.0)
#
# Le mécanisme zéro-touch de Harvester lit ses paramètres sur la LIGNE DE
# COMMANDE DU NOYAU : `harvester.install.automatic=true` fait chercher la
# configuration à l'URL de `harvester.install.config_url`. Or un média
# virtuel monte une image telle quelle, sans permettre d'ajouter des
# arguments — d'où cette remasterisation.
#
# Deux invariants à ne pas casser, sous peine d'un ISO qui démarre mais
# ne trouve pas son système de fichiers :
#   * le label de volume DOIT rester `COS_LIVE` : le noyau monte son
#     rootfs via `root=live:CDLABEL=COS_LIVE` ;
#   * l'amorce EFI doit être conservée (les serveurs récents ne bootent
#     plus en BIOS hérité).
#
# Usage :
#   harvester-iso-remaster.sh --src <iso> --out <iso> --config-url <url>
#                             [--extra-args "..."] [--label COS_LIVE]
#
# Émet des STEP_EVENT sur stderr pour s'intégrer au suivi SSE de la console.

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
if [[ -f "$SCRIPT_DIR/lib/common.sh" ]]; then
    source "$SCRIPT_DIR/lib/common.sh"
else            # utilisable seul (tests)
    emit_event() { printf 'STEP_EVENT|%s|%s|%s\n' "$1" "$2" "$3" >&2; }
    log_info() { printf '[INFO ] %s\n' "$*" >&2; }
    log_ok()   { printf '[OK   ] %s\n' "$*" >&2; }
    log_error(){ printf '[ERROR] %s\n' "$*" >&2; }
fi

SRC="" ; OUT="" ; CONFIG_URL="" ; EXTRA_ARGS="" ; LABEL="COS_LIVE"

usage() {
cat <<'EOF'
Usage: harvester-iso-remaster.sh --src <iso> --out <iso> --config-url <url>

Options:
      --src <path>          ISO Harvester officiel (lecture seule)
      --out <path>          ISO à produire (écrasé s'il existe)
      --config-url <url>    URL de la configuration d'installation
      --extra-args "<...>"  arguments noyau supplémentaires
      --label <nom>         label de volume (défaut COS_LIVE — ne pas changer
                            sans savoir : le rootfs est monté par ce label)
  -h, --help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --src) SRC="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --config-url) CONFIG_URL="$2"; shift 2 ;;
        --extra-args) EXTRA_ARGS="$2"; shift 2 ;;
        --label) LABEL="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "Argument inconnu : $1"; usage; exit 1 ;;
    esac
done

[[ -z "$SRC" || -z "$OUT" || -z "$CONFIG_URL" ]] && { usage; exit 1; }
[[ ! -f "$SRC" ]] && { log_error "ISO source introuvable : $SRC"; exit 1; }
command -v xorriso >/dev/null || { log_error "xorriso absent"; exit 1; }

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

emit_event "iso-extract" "running" "Extraction de l'ISO source"
log_info "Extraction de $(basename "$SRC")"
# -osirrox on autorise l'extraction ; acl/xattr off évite les avertissements
# sur un système de fichiers qui ne les gère pas.
xorriso -osirrox on:auto_chmod_on -acl off -xattr off \
        -indev "$SRC" -extract / "$WORK/iso" >/dev/null 2>&1
emit_event "iso-extract" "done" "Contenu extrait"

KARGS="harvester.install.automatic=true harvester.install.config_url=${CONFIG_URL}"
[[ -n "$EXTRA_ARGS" ]] && KARGS="$KARGS $EXTRA_ARGS"

emit_event "iso-patch" "running" "Injection des paramètres d'installation"
patched=0
# On patche TOUS les grub.cfg trouvés : l'amorce BIOS et l'amorce EFI ont
# chacune le leur, et ne pas toucher les deux donne un ISO qui s'installe
# tout seul dans un mode et attend un opérateur dans l'autre.
while IFS= read -r -d '' cfg; do
    if grep -q "harvester.install" "$cfg" 2>/dev/null; then
        # Ajouter nos arguments à la fin de chaque ligne linux/linuxefi qui
        # porte déjà la ligne de commande d'installation.
        sed -i -E "s#^([[:space:]]*(linux|linuxefi)[[:space:]].*harvester\.install[^\n]*)#\1 ${KARGS}#" "$cfg"
        patched=$((patched+1))
        log_info "  patché : ${cfg#"$WORK/iso/"}"
    fi
done < <(find "$WORK/iso" -name "grub.cfg" -print0)

if [[ "$patched" -eq 0 ]]; then
    emit_event "iso-patch" "error" "Aucun grub.cfg d'installation trouvé"
    log_error "Aucun grub.cfg contenant harvester.install — ISO inattendu ?"
    exit 1
fi
emit_event "iso-patch" "done" "$patched fichier(s) grub patché(s)"

emit_event "iso-build" "running" "Reconstruction de l'image"
# Reprendre les options d'amorce de l'ISO source : el-torito BIOS + EFI.
BOOT_IMG="boot/x86_64/loader/eltorito.img"
EFI_IMG="boot/x86_64/efi"
[[ -f "$WORK/iso/$BOOT_IMG" ]] || BOOT_IMG=""
[[ -f "$WORK/iso/$EFI_IMG" ]]  || EFI_IMG=""

XOPTS=(-as mkisofs -V "$LABEL" -J -R -joliet-long)
if [[ -n "$BOOT_IMG" ]]; then
    XOPTS+=(-b "$BOOT_IMG" -no-emul-boot -boot-load-size 4 -boot-info-table)
fi
if [[ -n "$EFI_IMG" ]]; then
    XOPTS+=(-eltorito-alt-boot -e "$EFI_IMG" -no-emul-boot -isohybrid-gpt-basdat)
fi
rm -f "$OUT"
xorriso "${XOPTS[@]}" -o "$OUT" "$WORK/iso" >/dev/null 2>&1
emit_event "iso-build" "done" "$(basename "$OUT") ($(du -h "$OUT" | cut -f1))"

emit_event "iso-verify" "running" "Vérification du label et des paramètres"
GOT_LABEL="$(xorriso -indev "$OUT" -pvd_info 2>/dev/null \
             | sed -n 's/^Volume Id[[:space:]]*: *//p' | head -1)"
if [[ "$GOT_LABEL" != "$LABEL" ]]; then
    emit_event "iso-verify" "error" "Label $GOT_LABEL au lieu de $LABEL"
    log_error "Label de volume perdu : le noyau ne trouvera pas son rootfs"
    exit 1
fi
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")") > "$OUT.sha256"
emit_event "iso-verify" "done" "label $GOT_LABEL, $patched grub patché(s)"
log_ok "ISO prêt : $OUT"
