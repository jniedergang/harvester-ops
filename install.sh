#!/usr/bin/env bash
# harvester-ops — interactive installer
# Supports:
#   - CLI scripts (always)
#   - Web UI (optional, via podman/docker)
#   - systemd unit (optional, requires Web UI)
#   - Self-signed TLS cert generation
#   - HTTP Basic auth setup (htpasswd)

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="/usr/local/bin"
CONF_DIR="/etc/harvester-ops"
LOG_DIR="/var/log/harvester-ops"
INSTALL_DIR="/opt/harvester-ops"

C_GREEN=$'\e[32m'; C_YELLOW=$'\e[33m'; C_RED=$'\e[31m'; C_BLUE=$'\e[34m'; C_BOLD=$'\e[1m'; C_RESET=$'\e[0m'

info()  { printf '%s[INFO]%s %s\n' "$C_BLUE"   "$C_RESET" "$*"; }
ok()    { printf '%s[ OK ]%s %s\n' "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf '%s[WARN]%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
err()   { printf '%s[ERR ]%s %s\n' "$C_RED"    "$C_RESET" "$*" >&2; }

require_root() {
    [[ $EUID -eq 0 ]] || { err "Run as root (sudo $0)"; exit 1; }
}

prompt() {
    local question="$1" default="$2" reply
    if [[ -t 0 ]]; then
        read -r -p "$question [$default]: " reply </dev/tty
    else
        reply=""
    fi
    echo "${reply:-$default}"
}

prompt_yesno() {
    local question="$1" default="${2:-Y}" reply
    while true; do
        if [[ -t 0 ]]; then
            read -r -p "$question [$default]: " reply </dev/tty
        else
            reply=""
        fi
        reply="${reply:-$default}"
        case "$reply" in
            [yY]|[yY][eE][sS]) return 0 ;;
            [nN]|[nN][oO]) return 1 ;;
            *) echo "Please answer y or n" ;;
        esac
    done
}

detect_container_runtime() {
    if command -v podman >/dev/null 2>&1; then
        echo "podman"
    elif command -v docker >/dev/null 2>&1; then
        echo "docker"
    else
        echo ""
    fi
}

check_deps() {
    local missing=()
    for cmd in bash ssh yq python3; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing dependencies: ${missing[*]}"
        info "On SLES: zypper install -y bash openssh-clients yq python3"
        info "On RHEL/Ubuntu: see docs/en/install.md"
        exit 2
    fi

    if ! command -v kubectl >/dev/null 2>&1; then
        warn "kubectl not found — required to operate clusters"
        info "Install: https://kubernetes.io/docs/tasks/tools/"
    fi
}

# -----------------------------------------------------------------------------
# Step 1: install bash scripts
# -----------------------------------------------------------------------------
install_scripts() {
    info "Installing CLI scripts to $PREFIX/"
    install -d -m 0755 "$PREFIX"
    install -d -m 0755 "$PREFIX/lib"
    install -m 0755 "$SCRIPT_DIR/bin/harvester-shutdown.sh" "$PREFIX/"
    install -m 0755 "$SCRIPT_DIR/bin/harvester-startup.sh"  "$PREFIX/"
    install -m 0755 "$SCRIPT_DIR/bin/harvester-status.sh"   "$PREFIX/"
    install -m 0644 "$SCRIPT_DIR/bin/lib/common.sh"         "$PREFIX/lib/"
    ln -sf "$PREFIX/harvester-shutdown.sh" "$PREFIX/harvester-shutdown"
    ln -sf "$PREFIX/harvester-startup.sh"  "$PREFIX/harvester-startup"
    ln -sf "$PREFIX/harvester-status.sh"   "$PREFIX/harvester-status"
    ok "Scripts installed: harvester-{shutdown,startup,status}"
}

# -----------------------------------------------------------------------------
# Step 2: config directories
# -----------------------------------------------------------------------------
prepare_config() {
    info "Preparing config directories under $CONF_DIR/"
    install -d -m 0755 "$CONF_DIR"
    install -d -m 0700 "$CONF_DIR/kubeconfigs"
    install -d -m 0700 "$CONF_DIR/ssh"
    install -d -m 0700 "$CONF_DIR/tls"
    install -d -m 0755 "$LOG_DIR"

    if [[ ! -f "$CONF_DIR/config.yaml" ]]; then
        install -m 0644 "$SCRIPT_DIR/config/config.yaml.example" "$CONF_DIR/config.yaml"
        ok "Default config copied to $CONF_DIR/config.yaml — edit it before first use"
    else
        info "Existing config at $CONF_DIR/config.yaml — left untouched"
    fi
}

# -----------------------------------------------------------------------------
# Step 3: web UI (container image)
# -----------------------------------------------------------------------------
install_web_ui() {
    local runtime
    runtime="$(detect_container_runtime)"
    if [[ -z "$runtime" ]]; then
        err "Neither podman nor docker found — cannot install Web UI"
        return 1
    fi
    info "Container runtime: $runtime"

    local image_tar="$SCRIPT_DIR/images/harvester-ops-ui.tar"
    if [[ -f "$image_tar" ]]; then
        info "Loading container image from $image_tar"
        "$runtime" load -i "$image_tar"
        ok "Image loaded"
    else
        warn "No bundled image found at $image_tar"
        info "You can build it later with: cd $SCRIPT_DIR && $runtime build -t harvester-ops:latest -f container/Containerfile ."
    fi
}

setup_basic_auth() {
    info "Setting up HTTP Basic auth"
    if [[ -f "$CONF_DIR/htpasswd" ]]; then
        if ! prompt_yesno "$CONF_DIR/htpasswd already exists. Overwrite?" "N"; then
            info "Keeping existing htpasswd"
            return
        fi
    fi

    local user
    user="$(prompt "Web UI username" "admin")"

    if ! command -v htpasswd >/dev/null 2>&1; then
        info "htpasswd not found — using Python bcrypt instead"
        local pw1 pw2
        while true; do
            read -r -s -p "Password: " pw1 </dev/tty; echo
            read -r -s -p "Confirm:  " pw2 </dev/tty; echo
            [[ "$pw1" == "$pw2" ]] && break
            warn "Passwords do not match — try again"
        done
        python3 - "$user" "$pw1" "$CONF_DIR/htpasswd" <<'PY'
import sys
try:
    from passlib.apache import HtpasswdFile
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "passlib", "bcrypt"])
    from passlib.apache import HtpasswdFile
user, pw, path = sys.argv[1], sys.argv[2], sys.argv[3]
ht = HtpasswdFile(path, new=True, default_scheme="bcrypt")
ht.set_password(user, pw)
ht.save()
PY
    else
        htpasswd -B -c "$CONF_DIR/htpasswd" "$user"
    fi
    chmod 0640 "$CONF_DIR/htpasswd"
    ok "Basic auth configured for user '$user'"
}

setup_tls() {
    if [[ -f "$CONF_DIR/tls/cert.pem" && -f "$CONF_DIR/tls/key.pem" ]]; then
        if ! prompt_yesno "TLS certs already present. Replace them?" "N"; then
            info "Keeping existing TLS certs"
            return
        fi
    fi

    cat <<EOF

${C_BOLD}TLS certificate options:${C_RESET}
  1) Provide your own certificate (cert.pem + key.pem)
  2) Generate a self-signed certificate (default)

EOF

    if prompt_yesno "Do you want to provide your own TLS certificate?" "N"; then
        # Custom cert path
        local cert_src key_src
        while true; do
            cert_src="$(prompt "Path to your TLS certificate (PEM)" "")"
            if [[ -n "$cert_src" && -f "$cert_src" ]]; then
                if openssl x509 -in "$cert_src" -noout -text >/dev/null 2>&1; then
                    break
                else
                    err "File is not a valid X.509 PEM certificate, try again"
                fi
            else
                err "File not found: $cert_src"
            fi
        done
        while true; do
            key_src="$(prompt "Path to your TLS private key (PEM)" "")"
            if [[ -n "$key_src" && -f "$key_src" ]]; then
                if openssl rsa  -in "$key_src" -noout -check 2>/dev/null \
                || openssl ec   -in "$key_src" -noout -check 2>/dev/null \
                || openssl pkey -in "$key_src" -noout 2>/dev/null; then
                    break
                else
                    err "File is not a valid PEM private key, try again"
                fi
            else
                err "File not found: $key_src"
            fi
        done
        # Optional chain (CA bundle)
        local chain_src
        chain_src="$(prompt "Path to CA chain bundle (optional, blank to skip)" "")"

        install -m 0644 "$cert_src" "$CONF_DIR/tls/cert.pem"
        install -m 0600 "$key_src"  "$CONF_DIR/tls/key.pem"
        if [[ -n "$chain_src" && -f "$chain_src" ]]; then
            install -m 0644 "$chain_src" "$CONF_DIR/tls/chain.pem"
            # Append chain to cert.pem for servers that need fullchain
            cat "$chain_src" >> "$CONF_DIR/tls/cert.pem"
            ok "User TLS certificate installed (with CA chain appended)"
        else
            ok "User TLS certificate installed"
        fi

        # Show cert info
        local cn_info exp_info
        cn_info=$(openssl x509 -in "$CONF_DIR/tls/cert.pem" -noout -subject 2>/dev/null | sed 's/subject= *//')
        exp_info=$(openssl x509 -in "$CONF_DIR/tls/cert.pem" -noout -enddate 2>/dev/null | sed 's/notAfter=//')
        info "Certificate subject: $cn_info"
        info "Certificate expires: $exp_info"
        return
    fi

    info "Generating self-signed TLS certificate"
    local cn
    cn="$(prompt "Common Name (CN) for the certificate" "$(hostname -f 2>/dev/null || hostname)")"
    openssl req -x509 -nodes -newkey rsa:4096 -days 3650 \
        -keyout "$CONF_DIR/tls/key.pem" -out "$CONF_DIR/tls/cert.pem" \
        -subj "/CN=$cn" \
        -addext "subjectAltName=DNS:$cn,DNS:localhost,IP:127.0.0.1" 2>/dev/null
    chmod 0600 "$CONF_DIR/tls/key.pem"
    ok "Self-signed TLS cert generated for CN=$cn (10-year validity)"
}

# -----------------------------------------------------------------------------
# Step 4: systemd unit
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# Open the UI port in the host firewall (permanent, survives reboot).
#
# Two reasons this is its own step rather than buried in install_systemd():
#   1. UI can be installed without systemd (manual `python3 app.py`).
#      The firewall still needs the port either way.
#   2. We learned the hard way (v1.4.19) that opening with
#      `firewall-cmd --add-port=X` WITHOUT --permanent is a footgun:
#      the runtime rule vanishes at the next `--reload` / reboot.
#      This helper always commits permanently AND reloads.
#
# Detects firewalld (RHEL/Fedora/SUSE family) and ufw (Debian/Ubuntu).
# If neither is found, prints a clear hint and continues — admins
# running iptables-direct or nftables-direct don't want us touching
# their carefully crafted rulesets.
# -----------------------------------------------------------------------------
setup_firewall() {
    local port
    port="$(yq -r '.web.bind_port // 8090' "$CONF_DIR/config.yaml" 2>/dev/null)"
    if [[ -z "$port" || "$port" == "null" ]]; then
        warn "Could not read web.bind_port from $CONF_DIR/config.yaml — skipping firewall"
        return 0
    fi

    if command -v firewall-cmd >/dev/null 2>&1 && \
       systemctl is-active --quiet firewalld 2>/dev/null; then
        info "firewalld detected — opening port ${port}/tcp permanently"
        if firewall-cmd --list-ports 2>/dev/null | tr ' ' '\n' | grep -qx "${port}/tcp"; then
            ok "Port ${port}/tcp already open in firewalld runtime"
        fi
        # --permanent writes to /etc/firewalld/zones/<active>.xml so the
        # rule survives reboot AND any later --reload (the runtime-only
        # gotcha that bit us in v1.4.19). --reload then reapplies the
        # whole permanent set into the live ruleset.
        firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null
        firewall-cmd --reload >/dev/null
        ok "firewalld: ${port}/tcp open (permanent)"
        return 0
    fi

    if command -v ufw >/dev/null 2>&1 && \
       ufw status 2>/dev/null | grep -qi "Status: active"; then
        info "ufw detected — opening ${port}/tcp"
        ufw allow "${port}/tcp" >/dev/null
        ok "ufw: ${port}/tcp open"
        return 0
    fi

    warn "No managed firewall detected (firewalld/ufw)."
    info "If you run iptables / nftables directly, open ${port}/tcp manually."
    info "  iptables: iptables -A INPUT -p tcp --dport ${port} -j ACCEPT"
    info "  nftables: nft add rule inet filter input tcp dport ${port} accept"
    return 0
}

install_systemd() {
    install -d -m 0755 /etc/systemd/system
    install -m 0644 "$SCRIPT_DIR/config/systemd/harvester-ops.service" \
        /etc/systemd/system/harvester-ops.service
    systemctl daemon-reload
    ok "systemd unit installed (not yet enabled)"
    if prompt_yesno "Enable and start harvester-ops now?" "Y"; then
        systemctl enable --now harvester-ops
        sleep 2
        if systemctl is-active --quiet harvester-ops; then
            ok "Service started"
            local port
            port=$(yq -r '.web.bind_port // 8090' "$CONF_DIR/config.yaml")
            echo
            printf '%s═══════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
            printf '   ✓ harvester-ops UI is live\n'
            printf '   → https://%s:%s\n' "$(hostname -f 2>/dev/null || hostname)" "$port"
            printf '%s═══════════════════════════════════════════════%s\n' "$C_GREEN" "$C_RESET"
            echo
        else
            warn "Service did not start cleanly — check: journalctl -u harvester-ops"
        fi
    fi
}

# -----------------------------------------------------------------------------
# Step 5: uninstall helper
# -----------------------------------------------------------------------------
install_uninstaller() {
    install -d -m 0755 "$INSTALL_DIR"
    install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR/docs" "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR/README.md" "$SCRIPT_DIR/VERSION" "$INSTALL_DIR/" 2>/dev/null || true
}

# -----------------------------------------------------------------------------
# Main flow
# -----------------------------------------------------------------------------
main() {
    require_root

    cat <<EOF

${C_BOLD}╔════════════════════════════════════════════════════════════╗
║              harvester-ops installer — v$(cat "$SCRIPT_DIR/VERSION")              ║
╚════════════════════════════════════════════════════════════╝${C_RESET}

EOF

    info "Checking dependencies..."
    check_deps

    install_scripts
    prepare_config

    if prompt_yesno "Install the Web UI (recommended)?" "Y"; then
        install_web_ui
        setup_basic_auth
        setup_tls
        # Open the UI port in the host firewall BEFORE starting the
        # service — otherwise admins testing the URL right after install
        # see "connection refused" and think the install failed.
        if prompt_yesno "Open the UI port in the host firewall?" "Y"; then
            setup_firewall
        fi
        if prompt_yesno "Install the systemd service?" "Y"; then
            install_systemd
        fi
    fi

    install_uninstaller

    cat <<EOF

${C_GREEN}${C_BOLD}╔════════════════════════════════════════════════════════════╗
║                 ✓ Installation complete                    ║
╚════════════════════════════════════════════════════════════╝${C_RESET}

Next steps:

  1. Edit ${C_BOLD}$CONF_DIR/config.yaml${C_RESET} to declare your clusters.
  2. Copy kubeconfigs to ${C_BOLD}$CONF_DIR/kubeconfigs/${C_RESET}.
  3. Copy the SSH key used to reach nodes to ${C_BOLD}$CONF_DIR/ssh/${C_RESET}.
  4. Test: ${C_BOLD}harvester-status --cluster <name>${C_RESET}
  5. Dry-run: ${C_BOLD}harvester-shutdown --cluster <name> --dry-run --yes${C_RESET}

Documentation: $INSTALL_DIR/docs/en/ (English) and $INSTALL_DIR/docs/fr/ (Français)

EOF
}

main "$@"
