#!/bin/bash
# harvester-ops : démarrage de découverte (1.78.0), déposé à la racine de
# l'ISO et lancé par `systemd.run=/run/initramfs/live/discover.sh`.
#
# Il ne porte AUCUN secret : seulement l'adresse de dépôt de l'inventaire,
# dont le jeton n'accepte qu'un seul envoi, pendant une découverte en cours.

# `systemd.run=` s'exécute aussi DANS l'initrd (vu en réel) : sans cette
# garde, la commande « réussit » à vide et éteint la machine avant le vrai
# système. Dans l'initrd, ne rien faire.
test -e /etc/initrd-release && exit 0

UPLOAD_URL="__UPLOAD_URL__"
OUT=/run/harvester-ops-discovery.txt

# les disques et les liens stables arrivent par udev : attendre qu'il ait fini
udevadm settle --timeout=120 >/dev/null 2>&1

# `echo` après chaque sortie JSON : un en-tête de section doit commencer sa
# propre ligne, même si une commande n'a pas fini la sienne.
{
    echo "== lsblk"
    lsblk -J -b -O
    echo
    echo "== links"
    for l in /dev/disk/by-id/* /dev/disk/by-path/*; do
        [ -L "$l" ] && echo "$l $(readlink -f "$l")"
    done
    echo "== nics"
    ip -j link
    echo
    echo "== speeds"
    for n in /sys/class/net/*; do
        echo "${n##*/} $(cat "$n/speed" 2>/dev/null)"
    done
} > "$OUT" 2>/dev/null

# Le réseau n'est pas encore monté quand le script démarre (vu en réel) :
# curl réessaie, y compris sur refus de connexion.
curl -sS --retry 60 --retry-delay 5 --retry-all-errors --max-time 30 \
     -X POST -H 'Content-Type: text/plain' --data-binary "@$OUT" "$UPLOAD_URL"

# La machine s'éteint d'elle-même, inventaire déposé ou non : la console
# attend cette extinction avant d'éjecter le média.
systemctl poweroff
