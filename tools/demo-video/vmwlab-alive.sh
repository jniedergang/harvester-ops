#!/bin/bash
# Prépare les invités Debian du banc vmwlab pour la vidéo de migration à chaud :
# un service écrit l'état du système sur l'écran (tty1) toutes les 2 s : nom,
# heure, durée de fonctionnement, compteur du service d'écritures `churn`.
# Visible dans la console VMware avant la bascule, puis dans la console
# Harvester après : même système, compteur qui reprend où il en était.
# Usage : vmwlab-alive.sh 172.16.2.82 [172.16.2.83 ...]   (clé ssh de node1)
set -euo pipefail
for ip in "$@"; do
  ssh -o BatchMode=yes "debian@$ip" 'sudo tee /usr/local/bin/alive.sh >/dev/null <<"S"
#!/bin/bash
while true; do
  {
    printf "\033[H\033[2J\n"
    printf "   ==============================================\n"
    printf "     %s  is alive\n" "$(hostname)"
    printf "     %s\n" "$(date "+%Y-%m-%d %H:%M:%S %Z")"
    printf "     up %s\n" "$(uptime -p | sed "s/^up //")"
    printf "     writes (churn): %s\n" "$(cat /var/lib/churn/counter 2>/dev/null || echo -)"
    printf "     kernel %s, disk %s\n" "$(uname -r)" "$(lsblk -dno NAME,MODEL | head -1 | tr -s " ")"
    printf "   ==============================================\n"
  } > /dev/tty1
  sleep 2
done
S
sudo chmod 755 /usr/local/bin/alive.sh
sudo tee /etc/systemd/system/alive.service >/dev/null <<"S"
[Unit]
Description=Show the system state on tty1 (harvester-ops demo)
After=getty@tty1.service
[Service]
ExecStart=/usr/local/bin/alive.sh
Restart=always
[Install]
WantedBy=multi-user.target
S
sudo systemctl daemon-reload && sudo systemctl enable --now alive.service && systemctl is-active alive.service'
done
