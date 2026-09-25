# Dépannage

## Pendant l'extinction

### « Pre-flight failed: API unreachable »

Le `KUBECONFIG` déclaré dans `config.yaml` est incorrect, ou l'API server est déjà down.

- Tester `kubectl --kubeconfig=<chemin> get nodes` manuellement.
- Vérifier la validité des certificats TLS (`openssl s_client -connect <api>:6443`).
- Si l'API est réellement down, le cluster est déjà dans un mauvais état — investiguer avant de poursuivre.

### Les VMs ne s'arrêtent pas dans `--vm-timeout`

Un guest est bloqué sur son propre handler ACPI. Options :

1. Allonger le timeout : `--vm-timeout 600`.
2. Forcer l'arrêt depuis l'UI web (tue directement le `VirtualMachineInstance` — l'état du guest peut être perdu).
3. Ignorer l'attente : `--skip-vm-stop` puis traiter manuellement les VMIs restants.

### Les volumes Longhorn ne se détachent pas

Généralement parce qu'un Pod référence encore un PVC lié au volume.

```bash
kubectl --kubeconfig=<chemin> -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=NAME:.metadata.name,STATE:.status.state,ATTACHED:.spec.nodeID
```

Pour chaque volume `attached`, trouver ce qui le retient :

```bash
kubectl get pod -A -o json | jq -r \
  '.items[] | select(.spec.volumes[]?.persistentVolumeClaim) |
   "\(.metadata.namespace)/\(.metadata.name)"'
```

Supprimer le pod fautif (ou son workload parent) avant de continuer.

### « SSH ... connection refused »

L'OS du node est déjà éteint ou injoignable. Le script log un warning mais continue — vérification manuelle recommandée.

## Pendant le démarrage

### L'API ne remonte pas

Vérifier le premier CP :

```bash
ssh rancher@<ip-cp> sudo systemctl status rke2-server
sudo journalctl -u rke2-server -n 100 --no-pager
```

Causes courantes :

- **Quorum etcd perdu** — survient quand plusieurs CPs ont été éteints trop rapprochés à l'extinction précédente.
  → Restaurer depuis le snapshot pris à l'étape 1 de l'extinction précédente :
  ```bash
  sudo systemctl stop rke2-server
  sudo rke2 server --cluster-reset \
    --cluster-reset-restore-path=/var/lib/rancher/rke2/server/db/snapshots/<snapshot>.db
  ```
- **Dérive horaire** — etcd y est très sensible. Vérifier chronyd/timesyncd.
- **Disque plein** sur `/var/lib/rancher` — les réplicas Longhorn peuvent le saturer.

### Des nodes restent NotReady

```bash
kubectl describe node <node-name>
ssh rancher@<node-ip> sudo journalctl -u rke2-agent -n 100 --no-pager
```

Causes courantes :

- Plugin CNI pas prêt (Calico/Multus). Vérifier `kubectl -n kube-system get pods | grep -E 'calico|multus'`.
- Container runtime (containerd) n'a pas démarré. Relancer `rke2-agent`.

### Les VMs restent `Halted` après démarrage

Le script ne redémarre automatiquement que les VMs qu'il a lui-même mises en `runStrategy: Halted` lors de l'extinction précédente. Pour les autres VMs à démarrer, basculer en `Always` manuellement ou utiliser le bouton « Redémarrer toutes les VMs » de l'onglet Namespaces de l'UI web.

## Problèmes UI web

### Le service ne démarre pas, ou ne lit pas une kubeconfig

Avant la 1.44.1, le service packagé ne pouvait pas démarrer : l'application
s'arrêtait sur `Read-only file system: '/var/lib/harvester-ops'`, et le
conteneur, qui tourne sous un compte non root, ne pouvait lire ni la clé
TLS, ni le fichier htpasswd, ni les kubeconfigs, qu'`install.sh` réservait
à root. Relancer `sudo ./install.sh` depuis le paquet 1.44.1 ou suivant : il
crée le compte `harvester-ops`, le volume persistant `/var/lib/harvester-ops/`,
et installe l'unité qui donne à ce compte la lecture de `/etc/harvester-ops/`
à chaque démarrage.

Si une kubeconfig ou une clé SSH ajoutée plus tard est signalée illisible,
redémarrer le service (`sudo systemctl restart harvester-ops`) : l'unité
rend tout fichier de `/etc/harvester-ops/` lisible par le groupe
`harvester-ops`, et par aucun autre compte, avant de lancer le conteneur.

### Boucle de login

Vérifier que `/etc/harvester-ops/htpasswd` existe et est au format bcrypt (`htpasswd -B -c`).

### « Cluster X non joignable » dans le dashboard

Le kubeconfig de ce cluster est invalide ou l'API est down. L'UI web ne plante pas — les autres clusters restent utilisables.

### Le stream SSE se coupe en cours d'opération

L'onglet du navigateur est resté inactif trop longtemps, ou le proxy devant Flask a un timeout court. L'opération continue côté serveur ; rafraîchir la page pour ré-attacher le stream.

### Une maintenance finit en erreur mais le nœud est en maintenance

Avant la 1.44.9, le dock des actions pouvait afficher « Harvester
refused the maintenance and withdrew the request » alors que le nœud
entrait bien en maintenance. Harvester retire sa marque `drain-requested`
avant la fin de la vidange, et celle-ci peut durer plusieurs minutes (elle
attend le budget de perturbation des gestionnaires Longhorn). Vérifier sur
le nœud :

```sh
kubectl get node <nom> -o jsonpath='{.metadata.annotations}'
```

`harvesterhci.io/maintain-status: completed` signifie que la maintenance
est faite, quoi qu'ait dit l'action. À partir de la 1.44.9, la console
attend avant de conclure : le cas ne se présente plus.

## Déplacer une VM entre clusters

### « The target cluster never fetched the disk »

La copie par la console exige que les nœuds de la cible joignent l'hôte de
la console en HTTP, sur le port 8094 par défaut. Ouvrir ce port sur l'hôte
(`firewall-cmd --add-port=8094/tcp`), ou régler `transfer: serve_address:
hôte:port` dans `config.yaml` sur une adresse et un port qu'ils joignent. Le
transfert s'arrête après cinq minutes sans requête et défait ce qu'il a
créé.

### La sauvegarde n'apparaît jamais sur la cible

Les deux clusters doivent avoir la même cible de sauvegarde, à l'état
`configured`. Harvester ne relit sa cible que sur demande (avec
`refreshIntervalInSeconds` à zéro, jamais de lui-même), et son premier
passage ne ramène que les images dont la VM est née : la sauvegarde apparaît
à un passage suivant. Le transfert le demande chaque minute, et demande
aussi à Longhorn de relire la cible tout de suite. Si les images sont
grosses, l'attente suit leur restauration.

### « duplicate mac address present for vm »

Harvester refuse deux VMs avec la même adresse MAC sur un réseau de
cluster, même si l'une est arrêtée (par exemple la VM d'origine, laissée
arrêtée, à côté de sa copie qui revient). Le contrôle préalable affiche les
adresses et la VM qui les porte : décocher « Garder les adresses MAC », ou
supprimer cette VM.

### L'import montre tous les octets envoyés, puis attend

« La cible écrit encore » : CDI a reçu le disque et l'écrit sur le volume.
Sur un stockage lent, cela peut durer des minutes (mesuré sur un cluster de
test imbriqué : cinq minutes pour 4 Gio). Le transfert continue de lui-même.

### « could not undo, remove by hand »

Un retour arrière réessaie ses suppressions pendant cinq minutes quand l'API
d'un cluster est injoignable. Ce qui reste est listé, et porte l'étiquette
`harvester-ops.io/transfer=<id>` : `kubectl get vm,pvc,virtualmachineimages
-A -l harvester-ops.io/transfer=<id>`. Harvester refuse de supprimer un
volume tant qu'une image en est exportée : supprimer les images d'abord.

### « Dépôt refusé » en déposant une archive dans le magasin

Le message dit pourquoi :

- `checksum mismatch in disks/<volume>.raw.gz` : le fichier a été abîmé en
  route (copie, clé USB, outil de transfert), bien que sa taille soit la
  bonne. Le retélécharger depuis le magasin d'origine et comparer
  `sha256sum` des deux côtés ;
- `incomplete archive` : l'export a été interrompu, ou la copie coupée ;
- `already in the store` : renommer le fichier (le nom doit finir par
  `.hvx`) ou supprimer d'abord l'ancienne archive ;
- `not enough room in the store` : la taille nécessaire et la place libre
  sont données ; faire de la place dans `/var/lib/harvester-ops/exports`.

Derrière un proxy inverse, relever sa limite de taille de requête (nginx :
`client_max_body_size 0;`) et ses délais pour l'adresse de la console : une
archive est une seule requête aussi grosse que le fichier.

### Une VirtualMachineRestore reste sur la cible

Normal : Harvester la lie à la VM restaurée et refuse de la supprimer tant
que cette VM existe. Des objets `BackupVolume` vides restent aussi dans
Longhorn après la suppression des sauvegardes du transfert ; c'est le
fonctionnement de Longhorn.

## Réseaux kube-ovn (onglet VPC)

### Une VM branchée sur un réseau overlay ne reçoit aucune adresse

Soit aucun subnet ne sert ce réseau (l'onglet VPC le dit au-dessus des
blocs, avec un bouton qui ouvre le formulaire de subnet sur lui), soit le
subnet a le DHCP coupé et la VM attend le DHCP. Un subnet sert un réseau
quand son `provider` vaut `<réseau>.<espace de noms>.ovn`.

### Une VM d'un subnet d'un autre VPC ne sort pas sur Internet

kube-ovn ne fait le NAT sortant que dans son VPC par défaut. Un VPC à part
reste isolé tant qu'il n'a pas de route vers l'extérieur (un appairage avec
le VPC par défaut et une route statique, ou une passerelle NAT de VPC, que
la console proposera dans une version suivante).

## Clusters Cluster API

### Des machines restent « Provisioning » alors que leurs VMs tournent

CAPHV v0.10.1 déclare une HarvesterMachine prête dans `status.ready`
seulement, alors que le cœur Cluster API de Harvester v1.9 (v1.13) lit
`status.initialization.provisioned`, à cause d'une étiquette de contrat sur
la CRD HarvesterMachine. L'installation retire cette étiquette par un
correctif porté par le `CAPIProvider` Harvester (une retouche à la main est
défaite par Turtles). Vérifier qu'elle a disparu :

```bash
kubectl get crd harvestermachines.infrastructure.cluster.x-k8s.io \
  -o jsonpath='{.metadata.labels}'     # pas de « cluster.x-k8s.io/v1beta2 »
```

L'onglet Installation le signale (`compatibility/contract-label`) et
l'installation la retire : Turtles réapplique le fournisseur sans ôter une
étiquette déjà posée.

### « unable to compute the Harvester Endpoint: problem in getting the ingress-expose service »

Harvester v1.9 n'a plus le Service `kube-system/ingress-expose` où CAPHV
v0.10.1 lit la VIP. L'installation crée un remplaçant étiqueté
`harvester-ops.io/shim=caphv-ingress-expose` ; l'onglet Installation dit
s'il est présent. Relancer l'installation pour le recréer.

### « spec.template.metadata in body should have at least 1 properties »

Les gabarits Harvester ont été créés en `v1alpha1` (par le générateur
amont lancé à la main, par exemple) : la conversion enregistre un
`metadata` vide que le schéma refuse ensuite quand la topologie recopie le
gabarit. Les clusters créés par la console sont en `v1beta1`. Supprimer le
cluster et ses gabarits, puis le recréer depuis la console.

### Le contrôle préalable dit que l'espace de noms contient déjà un cluster

Les objets générés (ClusterClass, secret d'identité, compléments) portent
des noms fixes : un second cluster dans le même espace de noms écraserait
ceux du premier. Laisser l'espace de noms vide dans les options avancées
(il prend alors le nom du cluster) ou en choisir un autre.

### Le pool d'adresses annonce plus d'adresses libres qu'il n'en a

Le compteur `available` d'un pool d'adresses Harvester dérive vers le haut
après des suppressions de clusters (24 libres sur 16 vu sur un cluster
d'essai). La console compte plutôt d'après la table des adresses
allouées.

### ingress-nginx reste en ContainerCreating : « iptables: executable file not found »

L'image des nœuds n'a pas `iptables` : cloud-init l'installe au premier
démarrage, et sur une image SLES non enregistrée (aucun dépôt) l'installation
échoue (`cloud-init status --long` sur le nœud le montre). Enregistrer
l'image, lui donner un dépôt local, préparer une image qui contient déjà
`iptables` et `qemu-guest-agent`, ou prendre l'image cloud openSUSE Leap
15.6.

### L'adresse de l'API répond avec le certificat de Harvester

Tant que le premier nœud de plan de contrôle n'est pas monté, l'adresse
d'équilibrage du nouveau cluster n'a pas de destination, et la connexion
aboutit sur le serveur d'API de Harvester. C'est normal jusqu'à ce que le
plan de contrôle compte 1.

## Reprendre une extinction interrompue

Si le script a été tué (Ctrl-C, perte SSH, ...) avec certains nodes éteints et d'autres encore up :

1. Lancer `harvester-status --cluster <nom>` depuis le poste opérateur.
2. Si l'API répond encore : relancer `harvester-shutdown` — il est idempotent et reprendra là où il s'est arrêté.
3. Si l'API ne répond plus mais des nodes sont up : SSH sur un CP encore vivant, éteindre manuellement les workers/CPs restants dans le bon ordre.

## Obtenir plus de diagnostic

```bash
# Trace bash complète
bash -x /usr/local/bin/harvester-shutdown.sh --cluster prod --dry-run -y 2>&1 | tee debug.log

# Snapshot cluster (read-only)
harvester-status --cluster prod --output json > status.json
```

Joindre le log du script (`/var/log/harvester-ops/*.log`) et le statut JSON lors d'une demande de support.
