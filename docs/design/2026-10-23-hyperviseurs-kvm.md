# Gestion d'hyperviseurs KVM

Statut : conception, 23/10/2026.

## Besoin

Un client gère un parc de VMs portées par des hyperviseurs KVM (libvirt), et les hyperviseurs eux-mêmes. harvops doit le faire de bout en bout, à côté des clusters Harvester, dans la même console : inventaire, gestes de cycle de vie, console graphique, création et modification de VMs, stockage, réseaux, instantanés, migration à chaud entre hôtes, maintenance d'un hôte, supervision.

Ce module ne transforme pas un hôte KVM en cluster Harvester. Il gère libvirt tel qu'il est.

## Principes

- **Aucun agent sur les hôtes.** La console lance `virsh` sur l'hôte par SSH (`ssh <hôte> virsh -c qemu:///system ...`) et lit le XML et les statistiques renvoyés. Le livrable n'a besoin que du client SSH, déjà présent dans l'image (`openssh-clients`). Rien à embarquer, airgap simple.
- **libvirt reste la source de vérité.** La console ne garde pas de copie de la définition des VMs. Elle relit `virsh dumpxml` et applique les changements par `virsh define`, `attach-device`, `setvcpus`, etc.
- **Parité CLI et UI.** Chaque geste est une sous-commande de `bin/harvester-kvm.py`. La console la lance en action suivie (`_cli_action`), comme pour les conteneurs et les réservations.
- **Prérequis sur l'hôte** : libvirt (démons modulaires ou `libvirtd`), un compte membre du groupe `libvirt` (ou root), `qemu-img` pour les volumes. Rien d'autre.

## Déclaration des hôtes

Un hôte se déclare dans la console (Réglages, comme un cluster) ou par la CLI. Fichier `kvm.d/<nom>.yaml` dans le répertoire d'état, chemins relatifs, droits 0600 :

```yaml
name: kvm-paris-01
address: 10.0.0.21
port: 22
user: virtops
ssh_key: ssh/kvm-paris-01_id        # relatif au répertoire d'état
known_hosts: ssh/kvm-paris-01_known  # empreinte relevée à la déclaration
uri: qemu:///system
labels: {site: paris, groupe: prod}
```

**L'empreinte de la clé d'hôte est épinglée.** À la déclaration, la console relève l'empreinte, la montre à l'exploitant, qui la confirme. Ensuite : `StrictHostKeyChecking=yes` avec ce fichier. C'est plus strict que le `accept-new` utilisé pour les nœuds Harvester : un hôte KVM porte des VMs clientes, une usurpation de l'hôte ne doit pas passer en silence.

Un test de connexion vérifie : SSH, `virsh version`, droits sur `qemu:///system`, `qemu-img`, mode d'écoute VNC.

## Architecture

| Pièce | Rôle |
|---|---|
| `bin/lib/kvm_host.py` | Classe `VirshHost` : exécution par SSH (`BatchMode=yes`, `ConnectTimeout`, délai par commande), et analyseurs purs du XML et de `domstats`. Les analyseurs sont testés sur des sorties réelles relevées sur node2 (`tests/fixtures/kvm/`) |
| `bin/lib/kvm_decl.py` | Lecture et écriture des déclarations `kvm.d/`, même règles que `cluster_decl.py` (nom contrôlé, chemins relatifs) |
| `bin/harvester-kvm.py` | CLI : `host add|test|list`, `vm list|show|start|shutdown|reboot|destroy|suspend|resume|delete`, `snapshot`, `clone`, `create`, `edit`, `migrate`, `pool`, `vol`, `net`, `host maintenance` |
| `web/app.py` | Routes `/api/kvm/...` : lectures par `@shared_read()`, gestes par `_cli_action` avec `@_rate_limit` |
| `web/static/js/kvm-*.js` | Vues (hôtes, VMs, stockage, réseaux), fenêtres de création et de modification |
| Échantillonneur | Fil de la console, toutes les 60 s : `virsh domstats` par hôte -> `kvm_stats.db` (SQLite, comme `usage.db`) |

Les lectures d'un parc de dizaines d'hôtes se font en parallèle (`ThreadPoolExecutor`), une connexion SSH par hôte réutilisée par `ControlMaster` (le socket reste dans le répertoire d'état, droits 0700) pour ne pas payer une poignée de main SSH par commande.

## Console graphique

Les VMs écoutent en VNC sur `127.0.0.1:59xx` de leur hôte (réglage par défaut de libvirt). La console ouvre un tunnel SSH vers ce port et le branche sur `vnc_mux`. Son contrat amont n'a que quatre méthodes (`send`, `receive`, `connected`, `close`) : un adaptateur autour d'une socket TCP suffit, et le partage entre plusieurs personnes vient avec. SPICE n'est pas pris en charge en v1 : la console le dit et propose d'ajouter un affichage VNC.

## Interface

Section de menu **KVM**, au même niveau que les clusters :

- **Hôtes** : un bloc par hôte (CPU, mémoire allouée et libre, nombre de VMs, état, étiquettes), filtre par étiquette, bouton « Déclarer un hôte ».
- **Machines virtuelles** : toutes les VMs de tous les hôtes, filtres (hôte, état, étiquette), tri, menu d'actions par VM comme pour les VMs Harvester, sélection multiple.
- **Stockage** : pools et volumes par hôte (capacité, allocation), créer un volume, téléverser une image.
- **Réseaux** : réseaux libvirt et ponts de l'hôte.

Même vocabulaire visuel que le reste de la console : blocs, badges, fenêtres, bulles d'aide, cinq langues.

## Versions

| Version | Contenu |
|---|---|
| 1.96.0 | Déclaration des hôtes (empreinte épinglée, test), inventaire en lecture (hôtes, VMs, pools, réseaux), gestes de cycle de vie (démarrer, arrêter, redémarrer, forcer l'arrêt, suspendre, reprendre), CLI |
| 1.97.0 | Console VNC par tunnel SSH, instantanés, clonage, suppression |
| 1.98.0 | Création et modification de VMs (CPU, mémoire, disques, cartes, à chaud quand libvirt le permet), pools et volumes, réseaux |
| 1.99.0 | Migration à chaud entre hôtes (stockage partagé, ou copie du stockage), maintenance d'un hôte (vidange), supervision (échantillonneur, graphiques, événements) |

Plus tard : migration d'une VM KVM vers un cluster Harvester, vue « toutes les VMs » qui mêle Harvester et KVM.

## Bancs d'essai

- **node2 en lecture seule** pour l'inventaire : 15 domaines, 9 pools, 3 réseaux. Ses domaines existants (bancs Harvester, VMware, Xen, MLM, hardpipe, netperf) ne sont jamais modifiés par les essais.
- **Gestes sur des domaines de test dédiés** `kvmtest-*` créés sur node2 pour l'occasion, puis supprimés.
- **Migration à chaud (1.99.0)** : deux hyperviseurs de test imbriqués sur node2 (la virtualisation imbriquée y est active), IP réservées et enregistrées dans NetBox et Pi-hole avant leur création.

## Points d'attention

- **Les définitions transitoires** (`virsh create` sans `define`) disparaissent à l'arrêt : la console les montre comme telles.
- **Les VMs à placement CPU particulier** (épinglage, ordonnanceur temps réel, comme `netperf` sur node2) : la modification par formulaire ne doit jamais écraser ces réglages. On modifie le XML de façon ciblée, jamais en le régénérant.
- **Délais.** Un `virsh shutdown` rend la main tout de suite : l'action suit l'état jusqu'à l'arrêt réel, ou jusqu'au délai réglable, puis propose l'arrêt forcé.
- **Droits.** Les rôles internes de la console s'appliquent : lecture, opérateur (gestes), administrateur (déclaration des hôtes, suppression).
