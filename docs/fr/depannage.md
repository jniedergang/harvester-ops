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
avant de poser `maintain-status`, et un relevé tombé entre les deux le
prenait pour un abandon. Vérifier sur le nœud :

```sh
kubectl get node <nom> -o jsonpath='{.metadata.annotations}'
```

`harvesterhci.io/maintain-status: completed` signifie que la maintenance
est faite, quoi qu'ait dit l'action. À partir de la 1.44.9, la console
attend avant de conclure : le cas ne se présente plus.

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
