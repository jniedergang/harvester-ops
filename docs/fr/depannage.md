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

### « Le cluster cible ne répond pas » derrière un proxy Rancher

Un proxy Rancher peut retourner 404 pour `/readyz` alors que les lectures
authentifiées des ressources Kubernetes fonctionnent. Les anciens contrôles
utilisaient cette adresse et pouvaient déclarer la cible à tort injoignable.

Depuis la 1.87.3, le contrôle lit d'abord une seule définition de ressource
personnalisée (`?limit=1`, 15 secondes au plus), qui passe par le proxy comme
toute ressource. Il lit ensuite la liste entière dont il a besoin avec les
60 secondes habituelles : elle pèse environ 37 Mo sur un cluster Harvester 1.9.
Une cible qui met plus longtemps à la rendre est annoncée comme ne répondant pas.

- Une cible qui ne répond pas, ou dont le certificat est refusé, reste
  annoncée « Le cluster cible ne répond pas ».
- Une cible qui répond mais refuse le lecteur (403, ou 401 pour des
  identifiants expirés) est annoncée comme telle : vérifier les droits ou les
  identifiants de son kubeconfig. Il doit autoriser la lecture des définitions
  de ressources personnalisées.

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

### Une vague VMware qui reste « en suppression »

Avant 1.84.0, supprimer une vague laissait son plan et ses migrations en
suppression pour toujours (finaliseur `orphan`) : le ramasse-miettes de
Kubernetes doit modifier chaque migration, et le webhook de Forklift refuse
toute modification d'une migration à un compte qui ne peut pas créer de VMs
dans le namespace cible. Supprimer de nouveau la vague à partir de 1.84.0 :
la console retire elle-même le finaliseur, et les VMs restent.

### Une VM Windows échoue à « conversion de l'invité »

Le pod `<vague>-<vm>-…` du namespace cible affiche `inspect_os: unhandled
exception thrown: Hivex.Error` : la conversion de l'invité du Forklift livré
pour Harvester 1.9 (virt-v2v 2.7.7) ne sait pas lire le registre d'un Windows
récent (vu sur Windows Server 2025). La bascule a arrêté la source : revenir
à la source (elle se rallume), clore et supprimer la vague, puis composer une
nouvelle vague en **copie brute** avec le **mode compatibilité** (le choix par
défaut pour Windows depuis 1.87.0).

### Une vague VMware affiche « Copie en attente de stockage »

La classe de stockage cible ne peut pas placer un disque de la vague : Longhorn
laisse le volume détaché (« insufficient storage », « un-schedulable
replicas ») et la copie ne peut pas commencer. Libérer de la place sur le
cluster, ajouter un disque, associer le datastore à une autre classe, ou
relever `storage-over-provisioning-percentage` de Longhorn (les volumes sont
alloués à la demande : un disque Windows de 40 Gio en occupe souvent bien
moins). La copie démarre d'elle-même dès que le volume s'attache. Depuis
1.87.0, une telle vague est refusée à la composition quand les chiffres
montrent qu'elle ne tient pas.

### Un retour arrière dit « VMware waits for an answer »

VMware a arrêté la source sur une question à la mise sous tension (souvent
un fichier de port série déjà présent). Le message donne la question et ses
choix ; y répondre dans le vCenter (« Répondre à la question » de la VM),
puis relancer le retour arrière : cette VM n'est pas marquée revenue tant
qu'elle ne tourne pas.

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

## Connexion par Rancher

### La page de connexion dit « La réponse de Rancher n'a pas pu être vérifiée »

Le code sous le message dit quel contrôle a échoué. `token-issuer` : l'`url`
de `config.yaml` diffère de l'adresse sous laquelle Rancher répond.
`token-audience` : `client_id` n'est pas le client OIDC de cette console.
Sur l'hôte de la console, la ligne « Rancher sign-in refused » du journal
porte le message de Rancher (un statut et une phrase, jamais un jeton).

### Une personne de Rancher ne voit aucun cluster, ou des vues vides

La console ne propose que les clusters que Rancher montre à cette personne,
retrouvés par l'UID de `kube-system` ; un cluster éteint ne peut pas être
reconnu (nouvel essai au bout d'une minute). Des vues vides veulent dire que
Rancher n'y donne aucun droit de lecture : vérifier ses rôles de cluster et
de projet dans Rancher. Sur harv1, l'espace de noms `default` était rattaché
à un projet d'un autre Rancher (`field.cattle.io/projectId`) : les rôles de
projet ne couvraient pas ses VMs.

### La connexion directe dit que l'identifiant ou le mot de passe est faux (1.79.0)

La console dit la même chose pour un mauvais mot de passe et un compte
inconnu. Vérifier le type de compte : une personne de LDAP ou d'Active
Directory doit choisir ce fournisseur, pas « Local ». Une personne d'un
fournisseur OIDC (Keycloak, par exemple) n'a pas de mot de passe que
Rancher sache vérifier : passer par l'authentification unique. Sur l'hôte
de la console, la ligne « Rancher direct sign-in refused » du journal donne
le code (jamais le mot de passe).

### Un Rancher est indisponible sur la page de connexion (1.79.0)

La console n'a pas obtenu ses fournisseurs en trois secondes (nouvel essai
au bout de trente secondes). Paramètres > Connexion par Rancher > Tester :
l'erreur dit si l'adresse ne répond pas ou si le certificat est refusé
(donner l'autorité de certification en PEM).

### Le chart Harvester RBAC est refusé (1.79.0)

Le chart déclare les versions de Rancher et de Kubernetes qu'il accepte
(`catalog.cattle.io/rancher-version`, `catalog.cattle.io/kube-version`) ;
la console montre les deux exigences et les versions qu'elle a lues. Mettre
Rancher à jour, ou attendre la version du chart qui lui correspond dans
`rancher-charts`.

### « starting or stopping a cluster needs a local account »

Voulu : l'alimentation se fait avec un compte local.

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

### La suppression d'un cluster se termine par « identity of ns/nom: N object(s) kept » (2.5.0)

Le `Cluster` est parti, mais des objets de son identité restreinte n'ont pu
être ni lus ni supprimés, le plus souvent parce que l'identité Harvester de
la personne qui l'a supprimé n'a pas `get` ou `delete` sur les rôles,
liaisons, comptes de service ou secrets d'un des namespaces cités. Le
namespace et le compte de l'identité sont gardés exprès : le compte porte
la liste des namespaces annexes. Donnez le droit qui manque (ou demandez à
quelqu'un qui l'a), puis supprimez à nouveau le cluster depuis Cluster API
> Clusters ou par `harvester-capi delete --name ns/nom` : la suppression
reprend sur l'identité et se termine.

### Un cluster reste « Deleting » et le journal de CAPHV dit « forbidden » (2.5.0)

CAPHV supprime le pool `<namespace>-<nom>-ippool` sans condition quand un
cluster part, et un refus n'est pas une absence : sans ce droit, il
recommence sans fin et le cluster ne disparaît jamais. L'identité
restreinte détient ce droit ; il ne manque que si quelqu'un a modifié le
ClusterRole `harvester-ops-caphv-<namespace>`. Comparez-le avec `kubectl
auth can-i --list --as=system:serviceaccount:<ns>:caphv-identity` et le
tableau de Capacités, section 4, et remettez la règle. La suppression
reprend d'elle-même à la tentative suivante de CAPHV, qui espace ses
tentatives après des erreurs répétées : sur le banc, elle est venue 8
minutes après la correction (jusqu'à 17 minutes environ). Vu en réel :
droit retiré, le `Cluster` est resté `Deleting` 10 minutes, avec `cannot
delete resource "ippools"` dans le journal de CAPHV.

### « the Harvester address is unknown » à la création d'un cluster (2.5.0)

L'identité restreinte a besoin de l'adresse de Harvester. Elle est lue sur
`kube-system/rke2-traefik` (installation neuve de 1.9), puis sur
`kube-system/ingress-expose` (cluster mis à jour depuis 1.8). Si aucun des
deux n'a d'adresse, vérifiez ces services sur le cluster (`kubectl -n
kube-system get svc rke2-traefik ingress-expose`).

## Paquets d'artefacts et registres (2.6.0)

### Un cluster enfant ne tire pas de son registre

Regardez sur un nœud du cluster enfant (plan de contrôle et worker) :

- `/etc/rancher/rke2/registries.yaml` : les miroirs et le compte du
  registre écrits à la création (compte de lecture seulement).
- `/var/lib/rancher/rke2/agent/etc/containerd/certs.d/<origine>/hosts.toml` :
  un bloc par miroir, dans l'ordre. Une ligne `server` veut dire que le
  repli vers le registre d'origine est actif (c'est le défaut).
- `/var/lib/rancher/rke2/agent/containerd/containerd.log` : chaque tirage
  nomme le point d'accès essayé et la raison de l'échec (certificat, 401,
  introuvable).

Causes habituelles :

- Les registres ont été choisis après la création du cluster. Ils sont lus
  au démarrage de RKE2 et posés à la création seulement : un cluster
  existant ne change pas.
- Le registre ne sert pas d'images (usage charts seulement), ou l'origine
  n'est pas parmi les miroirs choisis pour ce cluster.
- Les nœuds ne joignent pas l'adresse du registre (routage depuis le réseau
  des VMs, pare-feu). La console vérifie le registre depuis elle-même
  seulement.
- L'image n'a jamais été publiée, ou sous un autre nom : les images vivent
  à `<registre>/<hôte d'origine>/<chemin>:<tag>` ; listez-les avec le
  compte de lecture (`GET /v2/_catalog`).
- `containerd.log` dit `x509: certificate signed by unknown authority`
  pour un registre de la liste : son fichier de CA manque sur le nœud.
  La console écrit un fichier de CA par registre sous
  `/etc/rancher/rke2/tls/harvester-ops/<registre>/ca.crt`. Vérifiez que la
  CA de chaque registre a été déclarée.

### Une VM de cluster enfant reste en « Scheduling » (2.6.0)

La PVC de son disque racine reste `Pending` et l'événement de Longhorn dit
`volume size should be larger than the backing image size` : le disque
racine est plus petit que la taille virtuelle de l'image. Le contrôle de
création le bloque désormais (`disk-smaller-than-image`) et donne la
taille à demander. L'image cloud openSUSE Leap 15.6 publiée annonce
10 Gio.

### La publication dit « publish-tag-differs »

Un tag du paquet existe déjà dans le registre avec un autre contenu. Rien
n'a été écrit, pour aucune image. Publiez le contenu sous un nouveau tag
(une nouvelle version du paquet, ou `as` dans la description). Un registre
d'amorçage a les suppressions désactivées : ses tags ne peuvent pas être
retirés. Dans un registre du client, retirez le tag avec les outils de ce
registre, sciemment.

### Le registre d'amorçage est plein

La 2.6.0 n'a ni ramasse-miettes ni geste d'agrandissement, et ce qui est
publié reste. Le détail du registre montre l'occupation du volume. Déployez
un second registre avec un plus gros volume, ou, quand plus rien n'emploie
celui-ci, supprimez-le avec ses données (nom tapé) et déployez-le à
nouveau. Dans un plan, `registry-package-too-big` bloque des paquets qui
dépassent 90 % du volume.

### Un déploiement de registre d'amorçage a échoué et un nouvel essai dit « registry-name-taken »

Un déploiement qui n'est pas prêt à temps laisse ses objets et son volume
sur le cluster, et n'enregistre aucune déclaration. L'onglet Registres le
montre sur une ligne **Restes**. Lancez **Supprimer** sur cette ligne (ou
`harvester-registry delete NOM --cluster C`, avec `--delete-data` pour
retirer aussi le volume), corrigez la cause donnée par l'étape `ready`,
puis déployez à nouveau. Un plan de déploiement qui l'a posé n'a rien à
faire : sa reprise reprend ces restes et redéploie.

### Le pod du registre reste en « ErrImageNeverPull »

L'image du registre est importée au déploiement sur les nœuds listés dans
la configuration de la console. Un nœud ajouté plus tard au cluster ne l'a
pas, et le pod y a été placé. Importez l'image sur ce nœud à la main,
depuis l'hôte de la console :

```bash
ssh rancher@<nœud> 'sudo /var/lib/rancher/rke2/bin/ctr --address /run/k3s/containerd/containerd.sock \
  --namespace k8s.io images import -' < /var/lib/harvester-ops/registry-bundle/registry-images.tar
```

Ajoutez aussi le nœud à la configuration de la console.

### L'import demande un acquittement, ou le mode strict refuse

- `acknowledge-required` (code de sortie 3 en ligne de commande) : le
  paquet n'est pas signé, ou il est signé par une clé inconnue. Cochez
  **J'accepte ce paquet en connaissance de cause**, ou ajoutez la clé dans
  Paramètres > Artefacts, ou passez `--accept-unsigned` /
  `--accept-unknown-key`.
- `strict-mode` : le mode strict refuse tout ce qui n'est pas signé par une
  clé de confiance. S'il est verrouillé par `config.yaml` (`strict-locked`
  quand on essaie de le désactiver), seul un administrateur de l'hôte peut
  le changer.
- `invalid-signature`, `sums-mismatch` : toujours refusés. Reconstruisez ou
  retéléchargez le paquet.
- `archive-changed` : un autre envoi a remplacé l'archive après sa
  vérification. Vérifiez-la à nouveau.
- `package-untrusted-strict` à la publication : le mode strict a été activé
  après que le paquet a été accepté par acquittement.

### `harvester-artefacts remove` échoue sur une machine sans PyYAML

`remove` lit les plans enregistrés (un paquet qu'un plan publie ne peut pas
être retiré), et les plans ont besoin de PyYAML. Sans lui, l'étape dit
« remove reads the saved plans; run it where the console is installed
(PyYAML) » et la commande sort avec le code 1, sans rien retirer.
Lancez-la là où la console est installée. `pack`, `sign`, `verify` et `inspect` restent en
bibliothèque standard de Python.

## Artefacts dans les clusters enfants et Défaire (2.7.0)

### Un élément retiré a laissé un objet dans le cluster enfant (« kept-relabelled »)

La console ne supprime que les objets qui portent encore ses étiquettes
(`harvester-ops.io/managed`, `artefact-item`, `artefact-cluster`). Un objet
dont les étiquettes ont été retirées à la main est gardé et nommé dans
l'étape. Supprimez-le vous-même s'il n'est plus voulu. Un namespace livré
qui porte une charge ou un volume de quelqu'un d'autre est gardé aussi
(`kept-in-use`).

### Le binding d'un manifeste reste « pas encore appliqué »

Cluster API n'applique le ConfigMap que quand le `Cluster` porte
l'étiquette `harvester-ops.io/art-<élément>: "on"` et que le cluster enfant
est joignable. Lisez `kubectl get clusterresourcesetbinding -n <ns>
<cluster> -o yaml` sur le cluster Harvester : un objet que l'API du cluster
enfant refuse (un quota, un namespace absent, un LimitRange désordonné) y
est nommé. L'action de déploiement échoue après son délai (900 s par
défaut) avec l'état du binding.

### Un retrait ou un Défaire attend un finaliseur

La console ne retire jamais un finaliseur. Un `HarvesterCluster` qui reste
après Supprimer est nommé avec son finaliseur dans l'étape (attente de
120 s) ; un `Cluster` bloqué en Deleting veut en général dire qu'il manque
un droit à CAPHV (voir l'entrée 2.5.0 plus haut). Corrigez la cause, puis
relancez Supprimer ou le Défaire.

### La création d'un cluster dit « harvester-cluster-leftover »

Un `HarvesterCluster` de ce nom reste d'un ancien `Cluster`. Lancez
**Supprimer** sur le cluster (ou `harvester-capi delete --name ns/nom`) : il
est retiré quand aucun `Cluster` ne le porte. Un orphelin sans aucun
propriétaire n'est qu'un avertissement ; la création l'adopte.

### Le Défaire dit « undo-not-ours » pour un élément

Quelque chose porte le nom de l'élément sans la marque
`harvester-ops.io/plan-run` d'une exécution de la chaîne défaite : recréé
depuis par un autre déroulé, ou à la main. Le Défaire le laisse, et garde
ce qui en dépend (son cluster, le contenu Terraform). Soit retirez-le là où
il a été fait, puis relancez le Défaire ; soit gardez-le, et décochez
l'élément **et** les déclarations Terraform. Une déclaration Terraform
n'est jamais détruite tant qu'un cluster de la chaîne est encore là (voir
« undo-tf-carries-cluster » ci-dessous).

Une déclaration Terraform est « à nous » par son seul identifiant, jamais
réattribué. Une déclaration reprise par un déroulé suivant du même plan est
donc détruite par le Défaire de l'exécution qui l'a créée, dès qu'aucun
cluster de la chaîne ne reste.

### Le Défaire dit « undo-tf-carries-cluster »

Un cluster que la phase Clusters de la chaîne nomme (créé ou repris) est
encore là : décoché, recréé par un autre déroulé, ou repris. Il peut
employer le réseau, la classe de stockage ou l'image de la déclaration :
elle est gardée. Le plan de destruction nomme ces clusters sur la
déclaration. Retirez d'abord le cluster (cochez-le, ou supprimez-le là où il
a été fait), puis relancez le Défaire ; ou décochez la déclaration pour la
garder.

### Une suppression de registre s'est arrêtée en route

`harvester-registry delete` retire en dernier le Deployment, qui porte la
marque de l'exécution, après la fiche de la console. Une suppression
arrêtée en route (droit refusé, erreur d'API, annulation) laisse le
Deployment : le Défaire reconnaît toujours le registre et finit le retrait
quand on le relance.

### Le Défaire dit « undo-already-done »

Tout ce que l'exécution a créé est parti. Relancez le plan pour le recréer ;
un défaire ne se défait jamais.

## Accès aux clusters enfants, GitOps, renouvellement et registres (2.8.0)

### L'import dans Rancher dit « rancher-agent-image-missing »

Le manifeste d'enregistrement tire l'agent de Rancher (par exemple
`registry.rancher.com/rancher/rancher-agent:<version>` avec Rancher
Prime). La console le
sonde d'abord dans les registres du cluster enfant. Quand le cluster coupe
le repli vers Internet et qu'aucun registre ne porte l'image, l'import est
refusé avant toute application. Miroitez `registry.rancher.com` dans un
registre du cluster, publiez un paquet qui porte l'image de l'agent, puis
relancez l'import. Repli ouvert, le même cas n'est qu'un avertissement
(`rancher-agent-image-fallback`).

### Une entrée GitOps reste pas prête : la Kustomization échoue

Lisez l'état dans le cluster enfant :

```
kubectl -n hops-gitops get kustomization,ocirepository,gitrepository
kubectl -n hops-gitops describe kustomization <entrée>
```

- Une source pas prête : le registre, sa CA ou les identifiants Git. Une
  source `oci` dont l'artefact manque échoue au bout du délai (l'étiquette
  n'est pas contrôlée avant) ; publiez de nouveau le paquet.
- **Un `ClusterRole` (ou tout objet de portée cluster) dans une entrée
  `namespace` est refusé au compte usurpé.** C'est le verrouillage à
  l'œuvre, voulu : le compte `hops-gitops-<entrée>` n'écrit que dans son
  namespace d'équipe. Passez l'objet dans un élément manifeste, ou demandez
  `privileges: cluster` à un administrateur.
- `gitops-flux-not-locked` : le service `flux` a été installé sans les
  valeurs de verrouillage. Appliquez de nouveau l'élément chart `flux`
  depuis la console.

### Tous les objets Flux d'un cluster enfant ont disparu d'un coup

Les CRD de Flux viennent avec le chart `flux2`. Quand CAAPH réinstalle la
release (vu sur le banc lors d'un changement de son namespace), les CRD
sont retirées puis recréées, et chaque `OCIRepository`, `GitRepository`
et `Kustomization` part avec elles. Le `ClusterResourceSet` d'une entrée
ne réapplique pas son contenu tant que sa liaison porte la même
empreinte : l'entrée finit alors en délai dépassé.

- Depuis la 2.8.0, une release `flux` déjà posée garde son namespace : la
  console ne demande jamais à CAAPH de la déplacer.
- Pour réparer, faites réappliquer les entrées par Cluster API : supprimez
  le `ClusterResourceSetBinding` du cluster enfant sur le cluster Harvester
  (`kubectl -n <ns> delete clusterresourcesetbinding <c>`), puis touchez
  l'un de ses `ClusterResourceSet` (une annotation suffit). Les objets
  reviennent en quelques secondes.

### Un renouvellement est refusé : « renew-busy » ou « renew-plan-running »

- `renew-busy` : un renouvellement précédent de ce cluster enfant n'a pas
  fini (délai, console arrêtée), ou un autre renouvellement tient le
  cluster Harvester. Sa marque reste sur le `Cluster`. **Finir le
  renouvellement** (`harvester-capi renew --name NS/C --finish`) attend de
  nouveau et fait le ménage. **Abandonner** (`--abandon`, administrateurs)
  retire seulement la marque : les anciens gabarits et Secrets sont gardés
  et nommés.
- `renew-plan-running` : un plan (console ou `harvester-plans`) tient le
  cluster Harvester. Attendez sa fin.
- À l'inverse, `renew-running` : un renouvellement tient le cluster
  Harvester, donc les plans, le Défaire et la désinstallation de Turtles
  sont refusés jusqu'à sa fin (au plus son délai, 3 h au maximum).

### Renouveler un certificat dit « registry-ca-transition-pending »

Une nouvelle CA est en transition et des copies ne lui font pas encore
confiance. Le refus liste chaque copie avec sa raison. Lancez
`harvester-capi renew --name NS/C --what ca` sur chaque cluster enfant
listé (`nodes`, `rke2-fetch`, `service-ca`), `renew --finish` pour
`renewing`, redéployez les entrées Flux listées `flux:<entrée>`, et
relancez `harvester-nodes` pour `harvester-nodes`. Puis relancez
`renew-cert`. Les entrées Fleet ne sont pas dans la liste (avertissement
`registry-ca-fleet-unchecked`) : redéployez-les avant la bascule.

### La construction d'un paquet échoue en HTTP 403

Certains sites de téléchargement refusent l'agent de Python par défaut
(`get.rke2.io` pour `pack --rke2`). Depuis la 2.8.0, les outils envoient
un `User-Agent`. Avec un `harvester-artefacts` plus ancien, prenez celui
de la 2.8.0 sur la machine connectée.

### Nœuds Harvester est refusé : « harvester-nodes-other-auth »

Un autre registre du réglage Harvester `containerd-registry` garde un mot
de passe dans Harvester (un Secret `rke.cattle.io/auth-config` de
`fleet-local`). Quand les miroirs changent, Harvester 1.9 réécrit ces
Secrets depuis le réglage avec l'hôte seul, et effacerait ce mot de passe.
Rien n'a été écrit. Retirez d'abord ces identifiants dans Harvester
(Réglages > containerd-registry), lancez Nœuds Harvester, puis saisissez-les
de nouveau dans Harvester ensuite.


### Un cluster enfant sans Internet reste à 10 %

CAPHV (v0.10.1) écrit `package_update: true` et `packages:
[qemu-guest-agent, iptables]` dans le cloud-init de chaque machine. Sans
Internet, zypper attend l'expiration de chaque dépôt de l'image (environ
5 minutes chacun, vu sur harvlab2 avec Leap 15.6) avant que les commandes
RKE2 ne passent.

Depuis 2.8.1, un cluster créé avec `offline: true` coupe la mise à jour
des paquets et met de côté les dépôts zypper pendant le premier démarrage
(voir la procédure opérationnelle, cluster enfant sans Internet). Si les
lignes `Timeout exceeded when accessing ...` apparaissent encore dans la
console de la machine (`kubectl -n <ns>-vms logs <pod virt-launcher> -c
guest-console-log`) :

- le cluster a été créé avant 2.8.1 : ses gabarits ne portent pas
  `agentConfig.additionalUserData`. Supprimez-le et créez-le de nouveau ;
- lisez le Secret cloud-init de la machine sur le cluster Harvester
  (`<machine>-cloud-init` dans l'espace de noms des VMs, clé `userdata`).
  Il doit porter `package_update: false` et la ligne `bootcmd`
  `harvester-ops-repos-aside`. Si `package_update` vaut encore true, la
  version de CAPHV fusionne cloud-init autrement : employez une image dont
  les dépôts visent un miroir que les machines joignent, ou qui n'en a
  aucun.

La création sans Internet de bout en bout n'est pas encore vérifiée en
réel (2.8.1 reste à passer au banc).

### Nœuds Harvester posé, mais les images viennent encore de l'origine

Les nœuds du cluster Harvester qui porte un registre d'amorçage peuvent ne
pas joindre eux-mêmes l'adresse du registre. Vu sur harvlab2 (un nœud) :
l'adresse est portée par le nœud, et une connexion du nœud vers elle
expire. containerd se replie alors sur le registre d'origine. Branchez les
nœuds Harvester sur un registre qu'ils joignent : un registre du client, ou
le registre d'amorçage d'un autre cluster Harvester. Contrôle depuis un
nœud : `curl -k https://<registre>/v2/` (401 attendu).

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
