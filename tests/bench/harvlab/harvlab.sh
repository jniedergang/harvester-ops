#!/usr/bin/env bash
# harvlab : banc d'essai Harvester à trois nœuds, imbriqué sur node2.
#
# Sert à vérifier en réel ce qu'un cluster mononœud ne permet pas : isoler et
# mettre en maintenance un nœud, migrer une VM, une réplique Longhorn sur un
# nœud en panne, la jonction d'un nœud. Voir README.md à côté.
#
# Lancé depuis node1. Les secrets (jeton du cluster, mot de passe du compte
# rancher) vivent dans Vault `secret/infra/harvlab` et ne sont jamais
# affichés ; les configurations d'installation sont produites par la
# fonction même de harvester-ops (`_harvester_install_config`).
#
#   harvlab.sh secrets        crée les secrets dans Vault s'ils manquent
#   harvlab.sh serve          publie noyau, initrd, ISO et configurations
#   harvlab.sh install N...   installe les nœuds N (1 crée le cluster)
#   harvlab.sh kubeconfig     écrit ~/.kube/harvlab.yaml (serveur sur la VIP)
#   harvlab.sh unserve        retire la publication et referme le port
#   harvlab.sh status         état des VMs et des nœuds
#   harvlab.sh stop|start     arrête proprement ou démarre les trois VMs
#   harvlab.sh destroy        supprime VMs et disques (confirmation)
#   harvlab.sh pci            donne au nœud 3 un IOMMU virtuel, une e1000e et
#                             une igb (SR-IOV) pour le passthrough PCI
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"

NODE2="${HARVLAB_HOST:-ju@172.16.1.12}"
NODE2_IP="${NODE2#*@}"
DIR="/var/lib/libvirt/images/harvlab"
ISO="harvester-v1.8.2-amd64.iso"
ISO_LOCAL="$HOME/.local/share/harvester-ops/iso/$ISO"
PORT=8099
VIP=172.16.2.60
VCPUS=8
MEMORY_MB=20480
DISK_GB=250
VAULT_PATH=secret/infra/harvlab
KUBECONFIG_OUT="$HOME/.kube/harvlab.yaml"

ip_of()   { echo "172.16.2.6$1"; }
mac_of()  { echo "52:54:00:4c:ab:6$1"; }
name_of() { echo "harvlab-n$1"; }

on_node2() { ssh -o BatchMode=yes -o LogLevel=ERROR "$NODE2" "$@"; }
say()      { printf '[harvlab] %s\n' "$*"; }

vault_env() {
    export VAULT_ADDR=http://127.0.0.1:8200
    VAULT_TOKEN="$(sudo cat /data/vault/root-token)"
    export VAULT_TOKEN
}

cmd_secrets() {
    vault_env
    if vault kv get -field=token "$VAULT_PATH" >/dev/null 2>&1; then
        say "secrets déjà présents dans Vault ($VAULT_PATH)"
        return
    fi
    # Générés ici et passés par stdin : jamais en argument de commande.
    python3 -c 'import json, secrets; print(json.dumps({
        "token": secrets.token_urlsafe(24), "os_password": secrets.token_urlsafe(18)}))' \
        | vault kv put "$VAULT_PATH" - >/dev/null
    say "secrets créés dans Vault ($VAULT_PATH)"
}

# Configuration d'installation d'un nœud, rendue par harvester-ops.
#   $1 numéro du nœud, $2 « hash » ou « plain », $3 adresse de l'ISO
render_config() {
    local n="$1" form="$2" iso_url="$3" mode=join
    [[ "$n" == 1 ]] && mode=create
    vault_env
    HL_MODE="$mode" HL_FORM="$form" HL_ISO_URL="$iso_url" HL_IP="$(ip_of "$n")" \
    HL_MAC="$(mac_of "$n")" HL_NAME="$(name_of "$n")" HL_VIP="$VIP" \
    python3 - "$REPO" "$VAULT_PATH" <<'PY'
import logging, os, subprocess, sys, tempfile
repo, vault_path = sys.argv[1], sys.argv[2]
work = tempfile.mkdtemp()
os.environ.update(HARVESTER_OPS_WATCH="0",
                  HARVESTER_OPS_ACTIONS_DB=os.path.join(work, "actions.db"),
                  HARVESTER_OPS_NOTES_DB=os.path.join(work, "notes.db"))
sys.path.insert(0, os.path.join(repo, "web"))
logging.disable(logging.CRITICAL)
import app  # noqa: E402

def vault(field):
    return subprocess.run(["vault", "kv", "get", f"-field={field}", vault_path],
                          capture_output=True, text=True, check=True).stdout

password = vault("os_password")
if os.environ["HL_FORM"] == "hash":
    password = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=password,
                              capture_output=True, text=True, check=True).stdout.strip()
vip = os.environ["HL_VIP"]
sys.stdout.write(app._harvester_install_config({
    "token": vault("token"), "hostname": os.environ["HL_NAME"], "password": password,
    "ssh_keys": open(os.path.expanduser("~/.ssh/id_ed25519.pub")).read(),
    "ntp": "0.suse.pool.ntp.org", "dns": "172.16.3.6",
    "mode": os.environ["HL_MODE"], "device": "/dev/vda",
    "iso_url": os.environ["HL_ISO_URL"], "mgmt_interface": os.environ["HL_MAC"],
    "method": "static", "ip": os.environ["HL_IP"], "subnet_mask": "255.255.0.0",
    "gateway": "172.16.0.1", "vip": vip, "server_url": f"https://{vip}:443",
}))
PY
}

# Le chemin publié est noté HORS du répertoire servi, qui ne se liste pas.
serve_path() { on_node2 "sudo cat $DIR/.serve-path 2>/dev/null" || true; }

cmd_serve() {
    on_node2 "sudo install -d -m 0755 $DIR"
    if ! on_node2 "sudo test -s $DIR/$ISO"; then
        say "copie de l'ISO vers node2 (8 Go)"
        rsync -a -e "ssh -o LogLevel=ERROR" --rsync-path="sudo rsync" "$ISO_LOCAL" "$NODE2:$DIR/"
    fi
    # Noyau et initrd de l'installeur, pour un démarrage direct : la ligne de
    # commande porte l'adresse de la configuration propre à chaque nœud.
    local tmp rand url n form
    tmp="$(mktemp -d)"
    xorriso -osirrox on -indev "$ISO_LOCAL" \
        -extract /boot/x86_64/loader/linux "$tmp/linux" \
        -extract /boot/x86_64/loader/initrd "$tmp/initrd" >/dev/null 2>&1
    rsync -a -e "ssh -o LogLevel=ERROR" --rsync-path="sudo rsync" "$tmp/linux" "$tmp/initrd" "$NODE2:$DIR/"
    rm -rf "$tmp"
    # Un chemin non devinable : les configurations portent le jeton du cluster.
    rand="$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
    # http.server liste un répertoire sans index : une page vide à chaque
    # niveau, sinon la racine révélerait le chemin.
    on_node2 "sudo rm -rf $DIR/serve && sudo install -d -m 0755 $DIR/serve/$rand \
        && echo $rand | sudo tee $DIR/.serve-path >/dev/null \
        && sudo touch $DIR/serve/index.html $DIR/serve/$rand/index.html \
        && sudo ln -s $DIR/$ISO $DIR/serve/$rand/$ISO"
    url="http://$NODE2_IP:$PORT/$rand/$ISO"
    for n in 1 2 3; do
        # Le nœud 3 reçoit le mot de passe EN CLAIR, comme le fait l'onglet
        # Bare-metal : on vérifie ainsi ce que l'installeur en fait.
        form=hash; [[ "$n" == 3 ]] && form=plain
        render_config "$n" "$form" "$url" \
            | on_node2 "sudo install -m 0644 /dev/stdin $DIR/serve/$rand/$(name_of "$n").yaml"
    done
    on_node2 "sudo firewall-cmd --add-port=$PORT/tcp --timeout=3h >/dev/null \
        && (sudo systemctl stop harvlab-serve 2>/dev/null || true) \
        && sudo systemd-run --unit=harvlab-serve --collect \
           python3 -m http.server $PORT --bind $NODE2_IP --directory $DIR/serve >/dev/null"
    say "publication active sur $NODE2_IP:$PORT (chemin non devinable, port ouvert 3 h)"
}

cmd_unserve() {
    on_node2 "sudo systemctl stop harvlab-serve 2>/dev/null || true; \
        sudo firewall-cmd --remove-port=$PORT/tcp >/dev/null 2>&1 || true; \
        sudo rm -rf $DIR/serve $DIR/.serve-path"
    say "publication retirée"
}

cmd_install() {
    local rand n name args
    rand="$(serve_path)"
    [[ -n "$rand" ]] || { say "rien de publié : lancer « serve » d'abord"; exit 1; }
    for n in "$@"; do
        name="$(name_of "$n")"
        # Paramètres du menu de l'ISO (boot/grub2/grub.cfg), plus :
        #   ip=dhcp rd.neednet=1 : l'installeur lit config_url AVANT de
        #   configurer le réseau (piège payé sur harv3) ;
        #   skipchecks : 20 Gio de mémoire, sous le minimum de production.
        args="cdroot root=live:CDLABEL=COS_LIVE rd.live.dir=/ rd.live.squashimg=rootfs.squashfs"
        args+=" console=ttyS0 console=tty1 rd.cos.disable net.ifnames=1 ip=dhcp rd.neednet=1"
        args+=" harvester.install.automatic=true harvester.install.skipchecks=true"
        args+=" harvester.install.config_url=http://$NODE2_IP:$PORT/$rand/$name.yaml"
        say "installation de $name ($(ip_of "$n"))"
        # --wait -1 : virt-install enchaîne lui-même la fin de l'installation
        # (arrêt) et le premier démarrage sur le disque.
        # cache=unsafe : sur le RAID1 SATA de node2, etcd attendait ses fsync
        # jusqu'à 800 ms et kube-vip perdait la VIP en boucle (constaté le
        # 22/09/2026). Un banc jetable accepte le risque (perte seulement si
        # node2 lui-même tombe).
        on_node2 "sudo systemd-run --unit=harvlab-install-$name --collect \
            virt-install --name $name --memory $MEMORY_MB --vcpus $VCPUS \
            --cpu host-passthrough --machine q35 --osinfo detect=on,require=off \
            --boot uefi,firmware.feature0.name=secure-boot,firmware.feature0.enabled=no \
            --disk path=$DIR/$name.qcow2,size=$DISK_GB,format=qcow2,bus=virtio,cache=unsafe \
            --disk path=$DIR/$ISO,device=cdrom,bus=sata,readonly=on \
            --network bridge=br0,model=virtio,mac=$(mac_of "$n") \
            --install kernel=$DIR/linux,initrd=$DIR/initrd,kernel_args=\"$args\",kernel_args_overwrite=yes \
            --graphics vnc,listen=127.0.0.1 --serial pty \
            --noautoconsole --wait -1 >/dev/null"
    done
}

cmd_kubeconfig() {
    local tmp
    tmp="$(mktemp)"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "rancher@$(ip_of 1)" \
        "sudo cat /etc/rancher/rke2/rke2.yaml" > "$tmp"
    sed -i "s#server: https://127.0.0.1:6443#server: https://$VIP:6443#" "$tmp"
    install -m 0600 "$tmp" "$KUBECONFIG_OUT"
    rm -f "$tmp"
    say "kubeconfig écrit : $KUBECONFIG_OUT"
}

cmd_status() {
    on_node2 "sudo virsh list --all | grep -E 'harvlab|Name' || true"
    if [[ -s "$KUBECONFIG_OUT" ]]; then
        kubectl --kubeconfig "$KUBECONFIG_OUT" get nodes -o wide 2>&1 | head -5 || true
    fi
}

cmd_stop() {
    local n
    for n in 3 2 1; do on_node2 "sudo virsh shutdown $(name_of "$n") >/dev/null 2>&1 || true"; done
    say "arrêt demandé (virsh shutdown)"
}

cmd_start() {
    local n
    for n in 1 2 3; do on_node2 "sudo virsh start $(name_of "$n") >/dev/null 2>&1 || true"; done
    say "démarrage demandé"
}

cmd_destroy() {
    read -r -p "Supprimer les VMs harvlab et leurs disques ? (tapez harvlab) " answer
    [[ "$answer" == "harvlab" ]] || { say "abandon"; exit 1; }
    local n
    for n in 1 2 3; do
        on_node2 "sudo virsh destroy $(name_of "$n") >/dev/null 2>&1 || true; \
            sudo virsh undefine $(name_of "$n") --nvram --remove-all-storage >/dev/null 2>&1 || true"
    done
    cmd_unserve
    rm -f "$KUBECONFIG_OUT"
    say "VMs et disques supprimés (l'ISO reste dans $DIR)"
}

# Passthrough PCI et SR-IOV sur le nœud 3, avec du matériel émulé. Le nœud
# doit être vidé avant (mise en maintenance depuis la console) : il est
# arrêté, modifié puis redémarré. Idempotent.
cmd_pci() {
    local name; name="$(name_of 3)"
    on_node2 "sudo virsh dumpxml $name | grep -q \"<iommu model='intel'\"" \
        && { say "$name a déjà son IOMMU virtuel"; return; }
    on_node2 "sudo virsh shutdown $name >/dev/null; \
        for i in \$(seq 1 60); do [ \"\$(sudo virsh domstate $name)\" = 'shut off' ] && break; sleep 5; done; \
        sudo virt-xml $name --edit --features ioapic.driver=qemu >/dev/null && \
        sudo virt-xml $name --add-device --iommu model=intel,driver.intremap=on,driver.caching_mode=on >/dev/null && \
        sudo virt-xml $name --add-device --network bridge=br0,model=e1000e,mac=52:54:00:4c:ab:73 >/dev/null && \
        sudo virt-xml $name --add-device --network bridge=br0,model=igb,mac=52:54:00:4c:ab:74 >/dev/null && \
        sudo virsh start $name >/dev/null"
    say "$name : IOMMU virtuel, e1000e (04:00.0) et igb SR-IOV (05:00.0) ajoutés"
}

case "${1:-}" in
    secrets)    cmd_secrets ;;
    serve)      cmd_serve ;;
    unserve)    cmd_unserve ;;
    install)    shift; cmd_install "$@" ;;
    # Débogage : configuration d'un nœud (contient des secrets, à masquer).
    render)     shift; render_config "$@" ;;
    kubeconfig) cmd_kubeconfig ;;
    status)     cmd_status ;;
    stop)       cmd_stop ;;
    start)      cmd_start ;;
    destroy)    cmd_destroy ;;
    pci)        cmd_pci ;;
    *) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 1 ;;
esac
