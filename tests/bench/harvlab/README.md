# harvlab : banc d'essai Harvester à trois nœuds

Cluster Harvester **imbriqué** dans trois VMs KVM de node2, pour vérifier en
réel ce qu'un cluster mononœud (harv1, harv3) ne permet pas : isoler un nœud
(Harvester refuse d'isoler le dernier nœud disponible), le mettre en
maintenance avec migration des VMs, une réplique Longhorn sur un nœud en
panne, la jonction d'un nœud à un cluster existant.

| | Adresse | Nom |
|---|---|---|
| VIP du cluster | 172.16.2.60 | `harvlab.home.lo` |
| Nœud 1 (crée le cluster) | 172.16.2.61 | `harvlab-n1.home.lo` |
| Nœud 2 (rejoint) | 172.16.2.62 | `harvlab-n2.home.lo` |
| Nœud 3 (rejoint) | 172.16.2.63 | `harvlab-n3.home.lo` |

Enregistrés dans NetBox (`infra-dotfiles/netbox/seed.py`, `seed_vms.py`) et
Pi-hole (`dns.hosts`). Harvester v1.8.2, 8 vCPU, 20 Gio et 250 Go (qcow2
creux) par nœud, sur le LAN par le bridge `br0` de node2 : la console
(node1) l'atteint comme harv1, sans route ni tunnel.

## Utilisation

Depuis node1, dans l'ordre :

```bash
tests/bench/harvlab/harvlab.sh secrets     # jeton et mot de passe dans Vault
tests/bench/harvlab/harvlab.sh serve       # publie noyau, ISO, configurations
tests/bench/harvlab/harvlab.sh install 1   # crée le cluster (~20 min)
tests/bench/harvlab/harvlab.sh install 2 3 # rejoignent le cluster
tests/bench/harvlab/harvlab.sh kubeconfig  # ~/.kube/harvlab.yaml
tests/bench/harvlab/harvlab.sh unserve     # retire la publication
```

Puis `stop` / `start` pour l'éteindre et le rallumer, `status` pour son état,
`destroy` pour tout supprimer (l'ISO reste sur node2).

## Ce qu'il faut savoir

- **node2 est éteint par défaut** (consommation) : l'allumer par son iLO
  avant, l'éteindre après. **Jamais plus de deux lames allumées** sur le
  châssis (une seule alimentation) : node1 + node2 seulement.
- **Secrets** : Vault `secret/infra/harvlab` (`token`, `os_password`),
  jamais affichés. Le mot de passe du compte `rancher` est servi sous forme
  de condensat SHA-512, sauf pour le nœud 3 (voir plus bas).
- **Configurations** produites par `_harvester_install_config` de
  harvester-ops, la même fonction que l'onglet Bare-metal : le banc vérifie
  donc aussi ce qu'elle écrit, en création comme en jonction.
- **Publication** : serveur HTTP transitoire sur node2 (`harvlab-serve`),
  chemin aléatoire non listé, port ouvert pour 3 h au plus dans firewalld.
  Les configurations portent le jeton du cluster : `unserve` dès que les
  installations sont faites.
- **Démarrage direct du noyau** de l'installeur (`virt-install --install
  kernel=...`) : chaque nœud reçoit sur sa ligne de commande l'adresse de sa
  propre configuration. `ip=dhcp rd.neednet=1` sont obligatoires (l'installeur
  lit la configuration avant de configurer le réseau) ; `skipchecks` parce que
  20 Gio est sous le minimum de production.
- **Nœud 3 : mot de passe en clair**, comme l'envoie l'onglet Bare-metal.
  Vérifié le 22/09/2026 : l'installeur le hache lui-même (SHA-512, le
  condensat de `/etc/shadow` recalculé avec son sel correspond) ; un
  condensat `$6$` fourni est gardé tel quel. Les deux formes sont sûres.
- Disques en **`cache=unsafe`** : en `cache=none` sur le RAID1 SATA de node2,
  etcd attendait ses écritures jusqu'à 800 ms, kube-vip ne renouvelait plus
  son bail et relâchait la VIP en boucle. Le risque (données perdues si node2
  lui-même tombe) est acceptable pour un banc jetable.
- UEFI **sans Secure Boot** (le démarrage direct du noyau ne passe pas par
  shim) ; CPU `host-passthrough` pour que KubeVirt puisse lancer des VMs dans
  les nœuds.

## Passthrough PCI et SR-IOV (nœud 3)

`harvlab.sh pci` donne au nœud 3 un IOMMU virtuel (Intel, remappage des
interruptions), une carte e1000e (`04:00.0`) à passer telle quelle et une
carte igb (`05:00.0`) qui sait faire du SR-IOV (7 fonctions virtuelles). Le
vider avant (mise en maintenance depuis la console). Harvester démarre déjà
avec `intel_iommu=on iommu=pt`.

Ensuite, côté Harvester : activer l'addon `pcidevices-controller`, créer le
`PCIDeviceClaim` du périphérique **avec une ownerReference vers son
PCIDevice** (sans elle, le contrôleur boucle sur « Cannot find PCIDevice that
owns », et le claim ne se supprime plus sans retirer son finalizer). Pour le
SR-IOV, régler `spec.numVFs` du `SriovNetworkDevice` du nœud : chaque
fonction virtuelle devient un PCIDevice à réserver de la même façon.
Vérifié le 22/09/2026 : la carte et une fonction virtuelle, choisies dans
l'éditeur de VM de la console, sont arrivées sur le bus PCI de l'invité
(`virsh qemu-monitor-command ... "info pci"` dans le virt-launcher).
