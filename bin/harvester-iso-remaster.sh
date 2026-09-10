#!/usr/bin/env bash
# harvester-iso-remaster.sh — prépare un ISO Harvester pour une installation
# automatique (v1.19.0)
#
# Le mécanisme zéro-touch de Harvester lit ses paramètres sur la LIGNE DE
# COMMANDE DU NOYAU : `harvester.install.automatic=true` fait chercher la
# configuration à l'URL de `harvester.install.config_url`. Or un média
# virtuel monte une image telle quelle, sans permettre d'ajouter des
# arguments — d'où cette remasterisation.
#
# CE QUE L'ISO RÉEL CONTIENT (constaté sur harvester-v1.8.2-amd64.iso,
# et ce n'est pas ce qu'on aurait deviné) :
#
#   * un SEUL grub réel, `/boot/grub2/grub.cfg` ; celui de l'EFI
#     (`/EFI/BOOT/grub.cfg`) ne fait que le charger par `configfile`. Il
#     n'y a donc rien à patcher « des deux côtés » ;
#   * ses entrées de menu se terminent toutes par `${extra_iso_cmdline}`,
#     une variable prévue pour exactement notre usage, et le grub source
#     `/boot/grub2/harvester.cfg` AVANT de définir les entrées ;
#   * l'amorce est **UEFI seulement**, et son image El Torito est cachée
#     (elle ne existe pas comme fichier dans l'arborescence) : la
#     reconstruire depuis un `mkisofs` donnerait un ISO sans amorce.
#
# D'où la méthode : ne pas extraire les 7,7 Go ni reconstruire l'image,
# mais la RECOPIER en remplaçant un fichier, avec `-boot_image any replay`
# qui rejoue le dispositif d'amorçage d'origine. Une minute, et les
# invariants (label `COS_LIVE` pour `root=live:CDLABEL=`, amorce EFI) sont
# conservés par construction plutôt que reconstitués à la main.
#
# Usage :
#   harvester-iso-remaster.sh --src <iso> --out <iso> --config-url <url>
#                             [--extra-args "..."] [--label COS_LIVE]
#                             [--work-dir <path>]
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

SRC="" ; OUT="" ; CONFIG_URL="" ; EXTRA_ARGS="" ; LABEL="COS_LIVE" ; WORK_DIR=""

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
      --work-dir <path>     répertoire de travail (défaut : celui de --out).
                            À NE PAS laisser tomber dans un /tmp en tmpfs.
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
        --work-dir) WORK_DIR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "Argument inconnu : $1"; usage; exit 1 ;;
    esac
done

[[ -z "$SRC" || -z "$OUT" || -z "$CONFIG_URL" ]] && { usage; exit 1; }
[[ ! -f "$SRC" ]] && { log_error "ISO source introuvable : $SRC"; exit 1; }
command -v xorriso >/dev/null || { log_error "xorriso absent"; exit 1; }

# Le travail se fait par défaut à côté de l'ISO produit, PAS dans /tmp :
# beaucoup d'hôtes montent /tmp en tmpfs.
[[ -z "$WORK_DIR" ]] && WORK_DIR="$(dirname "$OUT")"
mkdir -p "$WORK_DIR"
WORK="$(mktemp -d "$WORK_DIR/remaster-XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# La copie fait la taille de la source ; on ne l'extrait plus.
NEEDED_KB=$(( $(du -k "$SRC" | cut -f1) + 262144 ))
FREE_KB=$(df -Pk "$WORK" | awk 'NR==2 {print $4}')
if [[ "$FREE_KB" -lt "$NEEDED_KB" ]]; then
    emit_event "iso-patch" "error" "espace insuffisant sur $WORK_DIR"
    log_error "Il faut $((NEEDED_KB / 1048576)) Gio libres sur $WORK_DIR, il y en a $((FREE_KB / 1048576))"
    exit 1
fi

# `ip=dhcp rd.neednet=1` n'est PAS décoratif. L'installeur Harvester va
# chercher sa configuration à `config_url` AVANT de configurer le moindre
# réseau (harvester-installer, pkg/console/install_panels.go : le fetch
# précède applyNetworks). Sur une installation PXE, le réseau est déjà
# monté par le paramètre `ip=` du noyau ; en démarrant depuis un média
# virtuel, personne ne l'a fait, et l'installeur reste bloqué sans jamais
# émettre une requête. Ces deux paramètres font monter DHCP dans l'initrd.
KARGS="harvester.install.automatic=true harvester.install.config_url=${CONFIG_URL}"
KARGS="$KARGS ip=dhcp rd.neednet=1"
[[ -n "$EXTRA_ARGS" ]] && KARGS="$KARGS $EXTRA_ARGS"

# --- 1. localiser le grub d'installation et son point d'injection ---------
emit_event "iso-patch" "running" "lecture de la configuration d'amorçage"
mkdir -p "$WORK/cfg"
CFGS=()
while IFS= read -r line; do
    [[ -n "$line" ]] && CFGS+=("$line")
done < <(xorriso -indev "$SRC" -find / -name 'grub.cfg' -exec echo_path -- 2>/dev/null \
         | tr -d "'" | grep '^/' || true)
[[ ${#CFGS[@]} -eq 0 ]] && CFGS=(/boot/grub2/grub.cfg /EFI/BOOT/grub.cfg)

GRUB_ISO="" ; GRUB_LOCAL=""
for c in "${CFGS[@]}"; do
    local_c="$WORK/cfg/$(echo "$c" | tr '/' '_')"
    xorriso -osirrox on -indev "$SRC" -extract "$c" "$local_c" >/dev/null 2>&1 || continue
    # Le grub intéressant est celui qui définit les entrées de menu, pas
    # celui qui se contente de le charger.
    if grep -q 'menuentry' "$local_c" 2>/dev/null; then
        GRUB_ISO="$c" ; GRUB_LOCAL="$local_c" ; break
    fi
done
if [[ -z "$GRUB_LOCAL" ]]; then
    emit_event "iso-patch" "error" "aucune configuration grub avec des entrées de menu"
    log_error "Ce n'est pas un ISO d'installation Harvester"
    exit 1
fi
log_info "grub d'installation : $GRUB_ISO"

# --- 2. injecter les arguments -------------------------------------------
# Voie normale : la variable ${extra_iso_cmdline} que les entrées de menu
# concatènent déjà. On la pose dans le fichier que le grub source AVANT de
# définir ses entrées ; à défaut, juste avant la première entrée.
MAPS=()
PATCH_MODE=""
if grep -q 'extra_iso_cmdline' "$GRUB_LOCAL"; then
    # `source (${root})/boot/grub2/harvester.cfg` -> /boot/grub2/harvester.cfg
    SOURCED="$(grep -E '^[[:space:]]*source[[:space:]]' "$GRUB_LOCAL" \
               | grep -oE '/[^[:space:])]*\.cfg' | head -1)"
    if [[ -n "$SOURCED" ]]; then
        local_s="$WORK/cfg/sourced.cfg"
        if xorriso -osirrox on -indev "$SRC" -extract "$SOURCED" "$local_s" >/dev/null 2>&1; then
            printf 'set extra_iso_cmdline="%s"\n' "$KARGS" >> "$local_s"
            MAPS+=(-map "$local_s" "$SOURCED")
            PATCH_MODE="extra_iso_cmdline via $SOURCED"
        fi
    fi
    if [[ -z "$PATCH_MODE" ]]; then
        awk -v args="$KARGS" 'BEGIN{done=0}
             /^[[:space:]]*menuentry/ && !done {printf "set extra_iso_cmdline=\"%s\"\n", args; done=1}
             {print}' "$GRUB_LOCAL" > "$GRUB_LOCAL.new"
        mv "$GRUB_LOCAL.new" "$GRUB_LOCAL"
        MAPS+=(-map "$GRUB_LOCAL" "$GRUB_ISO")
        PATCH_MODE="extra_iso_cmdline dans $GRUB_ISO"
    fi
else
    # ISO plus ancien : les arguments vont directement sur les lignes linux.
    # Le chargeur peut être écrit en dur (`linux`, `linuxefi`) ou passer par
    # une variable (`$linux`, `${linux}`), comme le fait Harvester pour
    # choisir entre BIOS et EFI dans le même fichier.
    KERNEL_RE='^[[:space:]]*(linux|linuxefi|\$\{?linux\}?)[[:space:]]'
    if ! grep -qE "$KERNEL_RE" "$GRUB_LOCAL"; then
        emit_event "iso-patch" "error" "aucune ligne de noyau à patcher"
        exit 1
    fi
    sed -i -E "s#(${KERNEL_RE}.*)\$#\1 ${KARGS}#" "$GRUB_LOCAL"
    MAPS+=(-map "$GRUB_LOCAL" "$GRUB_ISO")
    PATCH_MODE="ligne noyau de $GRUB_ISO"
fi
emit_event "iso-patch" "done" "$PATCH_MODE"

# --- 3. recopier l'image en rejouant son amorçage -------------------------
emit_event "iso-build" "running" "écriture de l'image (copie de $(du -h "$SRC" | cut -f1))"
rm -f "$OUT"
# `-boot_image any replay` reprend le dispositif d'amorçage de la source.
# C'est le point clé : l'image El Torito de cet ISO est cachée (pas un
# fichier de l'arborescence), donc irreproductible par un mkisofs.
xorriso -indev "$SRC" -outdev "$OUT" -boot_image any replay \
        -volid "$LABEL" "${MAPS[@]}" -commit >/dev/null 2>&1
emit_event "iso-build" "done" "$(basename "$OUT") ($(du -h "$OUT" | cut -f1))"

# --- 4. vérifier ce qui casse silencieusement ----------------------------
emit_event "iso-verify" "running" "vérification du label, de l'amorce et des arguments"
GOT_LABEL="$(xorriso -indev "$OUT" -pvd_info 2>/dev/null \
             | sed -n 's/^Volume Id[[:space:]]*: *//p' | head -1)"
if [[ "$GOT_LABEL" != "$LABEL" ]]; then
    emit_event "iso-verify" "error" "label $GOT_LABEL au lieu de $LABEL"
    log_error "Label de volume perdu : le noyau ne trouverait pas son rootfs"
    exit 1
fi
if ! xorriso -indev "$OUT" -report_el_torito plain 2>&1 | grep -q 'El Torito boot img'; then
    emit_event "iso-verify" "error" "amorce El Torito absente de l'image produite"
    log_error "L'ISO produit ne démarrerait pas"
    exit 1
fi
CHECK_ISO="${MAPS[2]}"          # chemin ISO du dernier fichier remplacé
xorriso -osirrox on -indev "$OUT" -extract "$CHECK_ISO" "$WORK/verify.cfg" >/dev/null 2>&1
if ! grep -qF "$CONFIG_URL" "$WORK/verify.cfg" 2>/dev/null; then
    emit_event "iso-verify" "error" "les arguments d'installation ne sont pas dans l'image"
    exit 1
fi
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")") > "$OUT.sha256"
emit_event "iso-verify" "done" "label $GOT_LABEL, amorce EFI et arguments présents"
log_ok "ISO prêt : $OUT"
