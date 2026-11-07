# Paquets d'artefacts et registres (2.6.0), artefacts dans les clusters enfants (2.7.0)

Les clusters enfants créés par Cluster API tournent souvent là où Internet
n'est pas joignable. La 2.6.0 apporte de quoi leur donner quand même leurs
images et leurs charts :

- un **paquet d'artefacts** signé, construit sur une machine connectée et
  importé dans la console ;
- des **registres** déclarés dans la console : le registre de votre
  organisation, ou un **registre d'amorçage** que la console pose sur le
  cluster Harvester ;
- la **publication** d'un paquet dans un registre ;
- des **clusters enfants** qui tirent de ces registres, posés à la
  création ;
- des **services** (CAAPH) qui installent un chart publié dans un registre.

La 2.7.0 applique aux clusters enfants ce qu'apporte un paquet : les charts
par CAAPH, les manifestes et politiques par un `ClusterResourceSet` dont la
console retire elle-même les objets, un socle de politiques et des comptes
d'automatisation aux jetons courts. Voir « Artefacts dans les clusters
enfants (2.7.0) » plus bas. Les phases Registre et Artefacts des plans, et
le Défaire d'un déploiement, sont décrits dans [bare-metal.md](bare-metal.md).

## Le paquet

Un paquet est un fichier, `<nom>-<version>.tar.gz`, avec une signature
détachée facultative `<nom>-<version>.tar.gz.sig`.

- `nom` : lettres minuscules, chiffres et tirets, une lettre en premier, 40
  caractères au plus.
- `version` : `x.y.z` (avec un suffixe facultatif) ou une date
  `AAAA.MM[.JJ][-n]`.

Dedans, un seul répertoire racine :

```
<nom>-<version>/
  SHA256SUMS            premier membre de l'archive
  artefacts.yaml        l'index (écrit en JSON, YAML valide)
  charts/<chart>-<version>.tgz
  images/oci-layout     disposition OCI des images
  images/index.json     une entrée par image
  images/blobs/sha256/  les couches, chacune rangée une fois
  manifests/<élément>/<fichier>.yaml
```

`SHA256SUMS` vient en premier : l'import vérifie chaque fichier pendant
qu'il extrait l'archive. Un fichier absent de la liste, ou d'une autre
empreinte, fait refuser tout le paquet.

Sortes d'éléments : `chart`, `images`, `manifest`, `policy` (un manifeste
de politiques, livré comme un manifeste depuis la 2.7.0). Une sorte de
plus, `gitops`, est gardée dans le paquet et montrée « à venir (2.8) » ;
elle n'est jamais appliquée ni publiée. Manifestes et politiques ne sont
pas publiés dans un registre.

### Construire un paquet : `harvester-artefacts pack`

`pack` tourne sur toute machine connectée qui a Python 3. Il n'emploie que
la bibliothèque standard de Python : ni PyYAML, ni podman, ni skopeo, ni
helm. Il lit une description en JSON (en YAML seulement si PyYAML est
installé ; sinon il le dit).

```json
{"name": "socle", "version": "2026.11",
 "description": "podinfo et son image",
 "items": [
   {"name": "podinfo", "kind": "chart",
    "chart": {"repo": "oci://ghcr.io/stefanprodan/charts", "name": "podinfo",
              "version": "6.9.2"},
    "images": ["ghcr.io/stefanprodan/podinfo:6.9.2"]},
   {"name": "preuve", "kind": "images",
    "images": [{"ref": "ghcr.io/stefanprodan/podinfo:6.9.2",
                "as": "docker.io/harvops-proof/podinfo:2.6.0"}]},
   {"name": "quotas", "kind": "manifest", "files": ["quotas.yaml"]}]}
```

```bash
harvester-artefacts pack socle.json --out dist/ --sign-key ~/.ssh/artefacts_ed25519
```

- **Charts** : depuis un registre OCI (`oci://`) ou un dépôt Helm
  (`https://`, son `index.yaml`, l'empreinte du chart donnée par l'index
  contrôlée). Un élément chart peut porter des `values` : les services
  installés depuis le registre partent d'elles.
- **Images** : recopiées couche par couche, une plateforme, `linux/amd64`
  par défaut (`--platform os/arch` pour une autre). Une couche partagée par
  plusieurs images est rangée une fois. Les images dont un chart a besoin
  se listent à la main : `pack` ne rend pas le chart.
- **`as`** range une image sous un autre nom, avec un tag. Il sert à un nom
  qui n'existe pas sur Internet, par exemple pour prouver qu'un cluster
  tire bien de son registre.
- **Manifestes** : des fichiers YAML, lus à côté de la description.
- **`--creds FICHIER`** : identifiants des registres et dépôts Helm
  sources, un fichier JSON `{"hôte": {"username": "...", "password":
  "..."}}`. Le fichier doit être privé (`chmod 600`), sinon `pack` le
  refuse. Les identifiants ne passent jamais par un argument et ne sont
  jamais écrits dans le paquet. Ils ne partent que vers leur propre hôte :
  une redirection vers un autre hôte les retire.
- **`--plain-http HÔTE`** (répétable) : un registre ou un dépôt Helm parlé
  en clair (installations d'essai). Toute source `http://` l'exige, même
  sans identifiants.
- **`--sign-key FICHIER`** signe le paquet à la fin (voir plus bas).

Codes de sortie : 0 construit, 1 échec (source injoignable, chart
introuvable, empreinte fausse), 2 description refusée (clé inconnue, nom
invalide, deux images sous un même nom...).

### Signer et vérifier

| Commande | Ce qu'elle fait | Code de sortie |
|---|---|---|
| `sign PAQUET --key FICHIER` | `ssh-keygen -Y sign` avec l'espace de noms `harvester-artefacts` ; écrit `PAQUET.sig` | 0, 1 |
| `verify PAQUET [--signers FICHIER]` | état de confiance de la signature | 0 seulement pour `valid`, 1 sinon |
| `inspect PAQUET [--signers FICHIER]` | index, fichiers, taille et état de confiance, sans extraire | 0 lisible, 2 pas un paquet |
| `import PAQUET [--accept-unsigned \| --accept-unknown-key] [--person NOM]` | import dans la console (répertoire d'état) | 0 accepté, 2 refusé, 3 acquittement requis |
| `list` | paquets de la console | 0 |
| `remove NOM VERSION` | retire un paquet de la console | 0, 2 refusé |
| `publish NOM VERSION --cluster C --registry R [--items a,b] [--dry-run]` | recopie dans un registre (voir Publication) | 0, 1 échec, 2 refusé |

Signer et vérifier demandent `ssh-keygen` d'OpenSSH 8.2 ou plus. L'espace
de noms `harvester-artefacts` n'est pas celui des releases de la console :
une signature de release ne vaut pas pour un paquet, et l'inverse.

`pack`, `sign`, `verify` et `inspect` restent en bibliothèque standard.
`remove` lit les plans enregistrés, il a donc besoin de PyYAML, comme
`import` et `publish` : lancez ces trois-là là où la console est installée.

## Confiance

Chaque paquet reçoit un état de confiance :

| État | Sens |
|---|---|
| `valid` | signature juste, faite par une clé de confiance |
| `unknown-key` | signature juste, la clé n'est pas parmi les clés de confiance |
| `unsigned` | pas de `.sig` |
| `invalid` | signature fausse, signature d'un autre fichier, ou `.sig` illisible |
| `sums-mismatch` | un fichier diffère de `SHA256SUMS`, ou n'y est pas listé (vu à l'extraction) |

La console décide :

| État | Mode permissif (défaut) | Mode strict |
|---|---|---|
| `valid` | accepté | accepté |
| `unknown-key` | accepté après l'acquittement `unknown-key` | refusé |
| `unsigned` | accepté après l'acquittement `unsigned` | refusé |
| `invalid`, `sums-mismatch` | refusé | refusé |

L'acquittement doit nommer l'état constaté ; il n'est jamais deviné. Dans
la fenêtre **Importer**, un paquet non signé ou signé par une clé inconnue
montre ce que cela veut dire (« Ce paquet n'est pas signé. Son origine ne
peut pas être vérifiée. »), l'empreinte de la clé quand il y en a une, et
une case **J'accepte ce paquet en connaissance de cause**. **Accepter**
reste grisé tant que la case n'est pas cochée. L'acquittement est lié à
l'archive vérifiée : si un autre envoi l'a remplacée entre-temps, la
console refuse (`archive-changed`) et demande de vérifier à nouveau. Par
l'API, `POST /api/artefacts/<nom>/<version>/accept` avec un
`acknowledge` doit porter le `sha256` rendu par la pré-vérification
(`sha256-required`, 400, sinon).

**Journal.** L'étape `trust` de l'action d'import écrit, par exemple,
`accepted UNSIGNED package socle-2026.11 after acknowledgement by alice`
(ou `UNKNOWN-KEY`, ou `accepted package ... signed by <principal>
(<empreinte>)`). Les mêmes faits vont dans `trust.json`, à côté du paquet :
état, principal et empreinte de clé s'il y en a, personne, date, mode. Un
paquet accepté après un acquittement garde son badge (**Clé inconnue**,
**Non signé**) dans l'onglet Artefacts.

**Mode strict et clés de confiance.** Paramètres > Artefacts
(administrateurs) :

- interrupteur **Mode strict**, gardé dans `<état>/artefacts/settings.json` ;
- **Clés de confiance**, les lignes d'un fichier `allowed_signers`
  d'OpenSSH, gardées dans `<état>/artefacts/signers` (0600). Coller une
  ligne (principal, puis la clé publique) ajoute une clé ;
  `namespaces="harvester-artefacts"` est ajouté s'il manque. Clés admises :
  `ssh-ed25519`, `ecdsa-sha2-*`, `ssh-rsa` de 3072 bits ou plus. Une clé se
  retire par son empreinte.

`artefacts: {strict: true}` dans `config.yaml` verrouille le mode strict :
l'interrupteur est grisé avec la raison, et le désactiver est refusé
(`strict-locked`). Le verrou porte sur **le mode, pas sur les clés** : un
administrateur peut encore ajouter une clé de confiance, et rendre ainsi
`valid` sa propre signature. Restreignez qui est administrateur si cela
compte.

Un mode strict activé après un import vaut aussi pour la publication : un
paquet accepté par acquittement ne peut plus être publié
(`package-untrusted-strict`).

**Importer dans la console.** Cluster API > Artefacts > **Importer** :
envoyer l'archive et sa `.sig` (**Envoyer et vérifier**), lire l'état de
confiance, puis **Accepter**. La console vérifie avant la réception qu'il y
a la place pour deux fois l'archive plus 1 Gio (507 sinon). Le paquet est
extrait et vérifié dans `<état>/artefacts.d/<nom>-<version>/` (0700), avec
`trust.json` et sa `.sig` ; l'archive envoyée est ensuite supprimée. Un
paquet déjà importé doit être retiré avant d'être importé à nouveau.
**Retirer** est refusé tant qu'un plan enregistré publie le paquet
(`package-in-use`) ; les registres gardent ce qui a été publié.

## Registres

Un registre est déclaré dans la console sans aucun secret
(`<état>/registries.d/<nom>.yaml`, révision, 409 sur une révision
périmée). Ses comptes ne vivent que dans des Secrets des clusters Harvester
qui l'emploient. Cluster API > **Registres** les liste, rattachés ou non au
cluster de gestion affiché dans l'en-tête, avec leur adresse, leur usage,
leurs miroirs, les paquets publiés, les clusters et plans qui les emploient,
et leur état.

### Registre du client

**Déclarer un registre du client** (administrateurs) : un registre existant
de votre organisation.

- **Nom** : lettres minuscules, chiffres et tirets, 30 caractères au plus.
- **Adresse** : `hôte[:port]`, sans `https://` ni chemin.
- **CA** : le certificat public de la CA (PEM) ; vide, ce sont les CA du
  système.
- **Usage** : images (les nœuds en tirent leurs images), charts (CAAPH en
  tire les charts), ou les deux.
- **Miroirs** : les registres d'origine qu'il sert (`docker.io`,
  `ghcr.io`...). Un cluster enfant tire de lui les images de ces origines.

**Rattacher** (opérateurs) donne le compte d'écriture (`push`) et le compte
de lecture (`pull`) pour un cluster Harvester. Ils sont posés en Secrets
dans le namespace `harvester-ops-registry` de ce cluster (`<registre>-push`,
`<registre>-pull`, et `<registre>-ca` pour la CA), avec l'identité de la
personne. Ils sont envoyés une fois et jamais réaffichés. En ligne de
commande, ils arrivent sur l'entrée standard, jamais dans un argument.

- Sans compte de lecture distinct, le compte d'écriture sert aux deux, avec
  l'avertissement `registry-pull-is-push` : le compte de lecture finit dans
  un fichier de chaque nœud des clusters enfants.
- **Qui peut lire les Secrets de `harvester-ops-registry` sur le cluster
  Harvester détient le compte d'écriture de votre registre.** Le compte
  d'écriture ne quitte jamais ce namespace : les clusters enfants ne
  reçoivent que le compte de lecture.

**Vérifier** (opérateurs) lit les comptes sur le cluster et sonde le
registre depuis la console, avec chaque compte, sans rien écrire.
Choisissez un **réseau des VMs** à côté du bouton pour le sonder aussi
depuis ce réseau (voir plus bas). Constats : `registry-unknown`,
`registry-not-attached`, `registry-unreachable`, `registry-ca-invalid`,
`registry-auth-refused`, `registry-no-usage` (bloquants) ;
`registry-pull-is-push`, `registry-insecure`, `registry-write-port-open`
(registre d'amorçage dont le port d'écriture accepte toute adresse, voir
Paramètres > Registres), `registry-unreachable-from-vm-network`,
`registry-vm-network-unchecked` (avertissements) ; `registry-vm-network-ok`
(information).
`registry-insecure` vient de `insecure_skip_verify: true` dans un fichier
de déclaration : les nœuds ne vérifient alors pas le certificat.

**Joignabilité depuis le réseau des VMs (2.6.1).** Que la console et le
cluster Harvester joignent un registre ne dit pas que les VMs enfants le
joignent : un réseau de VMs peut ne pas y avoir de route (VLAN sans route,
pare-feu). Les contrôles lancent donc une **pod sonde** brève attachée au
réseau des VMs, comme le serait une VM :

- C'est un Job dans son propre namespace, `harvester-ops-probe`, attaché
  par Multus au réseau (NetworkAttachmentDefinition), sur les nœuds qui
  portent son réseau de cluster. L'image est `bci-busybox`, livrée dans le
  paquet du registre et épinglée par empreinte ; elle est importée sur les
  nœuds avec un registre d'amorçage (sur un cluster qui n'en a pas, le
  nœud la tire de `registry.suse.com`, ou la sonde dit que l'image manque).
- Elle prend une adresse comme une VM : celle que donne l'IPAM du réseau,
  sinon un bail DHCP, rendu à la fin de la sonde.
- Pour chaque adresse, elle essaie dans l'ordre : le nom avec le serveur
  DNS que donne le réseau (si le registre est un nom), une route par le
  réseau des VMs, une connexion TCP, puis une poignée de main TLS, le nom
  envoyé en SNI (une réponse HTTP, 401 compris). La CA elle-même est
  vérifiée par la console, pas par la sonde. Le client TLS de la sonde
  (BusyBox) ne lit que les certificats RSA et ECDSA.
- Une adresse donnée par l'IPAM du réseau sans passerelle (pour un
  registre hors de son sous-réseau) ou sans serveur DNS (pour un registre
  donné par son nom) n'est pas jugée : une VM peut les recevoir autrement.
  Le constat le dit alors (`ipam-incomplete`) au lieu de « injoignable ».
- Moindre privilège : capacités `NET_ADMIN` (adresse et routes) et
  `NET_RAW` (socket DHCP) seulement, toutes les autres retirées, aucun
  jeton de compte de service, système de fichiers en lecture seule sauf un
  `/tmp` de 1 Mio en mémoire, 32 Mio de mémoire. Ce sont les capacités que
  l'essai préalable de la 2.6.0 a prouvées suffisantes.
- Elle est retirée à la fin de chaque contrôle, réussi ou non ; le Job a
  aussi un délai et `ttlSecondsAfterFinished`, il disparaît donc même si
  la console s'arrête en route.
- Il faut le droit de créer un Job dans `harvester-ops-probe` (et le
  namespace la première fois). Sans lui, le contrôle le dit.

Où elle part : **Vérifier** un registre avec un réseau des VMs choisi
(une sonde neuve à chaque fois), et le contrôle de création d'un cluster
enfant et les contrôles des plans, depuis le réseau principal du cluster
(`network`), une fois pour tous ses registres qui servent des images. Ces
contrôles se relancent à la saisie dans la fenêtre de création : un
résultat est gardé 5 minutes par cluster Harvester, réseau et adresses,
pour que la saisie ne lance pas une pod à chaque fois. Une sonde qui n'a
pas pu juger n'est pas gardée (cela dépend souvent des droits de la
personne). Un registre
d'amorçage posé par un plan est sondé au début de la phase Clusters (il ne
répond pas avant).

Constats : `cluster-registry-vm-network-ok` (information, avec l'adresse
de la sonde), `cluster-registry-unreachable-from-vm-network` (avec
l'étape qui a échoué : nom, route, TCP ou TLS ; un avertissement,
**bloquant quand le cluster coupe le repli vers Internet**, ses nœuds ne
tirant alors plus rien), et `cluster-registry-vm-network-unchecked` quand
la sonde ne peut pas juger, avec la raison : aucun réseau des VMs, réseau
introuvable, droits manquants, image de la sonde absente, aucune adresse
(pas de bail DHCP en 20 s et pas d'IPAM), adresse de l'IPAM sans
passerelle ni DNS, pod non démarrée (message de Multus ou de
l'ordonnanceur), pas de résultat à temps. Mêmes codes sans le
préfixe `cluster-` pour Vérifier. La fenêtre du registre d'amorçage dit
`registry-vm-network-unchecked` : l'adresse n'est pas encore servie, elle
est sondée quand un cluster enfant ou un plan l'emploie.

Limites : la sonde est une pod sur un nœud Harvester, pas une VM ; elle ne
part que du réseau principal du cluster ; un pare-feu qui traite pods et
VMs différemment (par adresse MAC, par exemple) n'est pas vu.

**Oublier** retire les Secrets du registre (les deux comptes et la CA) de
chaque cluster Harvester rattaché, avec votre identité, puis la
déclaration. C'est refusé tant qu'un plan, un modèle de cluster ou un
cluster enfant emploie le registre ; la fenêtre dit alors ce qui
l'emploie. L'oubli est une action de `harvester-registry forget`, suivie
dans l'Activité (`registry-forget:<nom>`) dès le départ : chaque Secret
retiré est une étape. Quand un cluster refuse le retrait, l'action finit
en erreur avec les étapes déjà faites, et la déclaration reste pour
réessayer. Un cluster rattaché qui n'est plus dans la console est nommé
dans une étape : ses Secrets y restent, à retirer à la main.

### Registre d'amorçage

**Registre d'amorçage** (administrateurs) pose un registre sur le cluster
Harvester : CNCF Distribution 3.1 dans l'image maintenue par SUSE
(`registry.suse.com/suse/registry:3.1`), épinglée par empreinte et livrée
avec la release (voir [installation.md](installation.md)). Rien n'est tiré
d'Internet.

La fenêtre a trois onglets, **Paramètres**, **Vérifications**, **Résumé** ;
**Lancer** reste grisé tant qu'une vérification bloque, avec la raison.

- **Adresse** : une adresse IPv4 libre du réseau de gestion, hors des pools
  d'adresses, servie par le kube-vip de Harvester. Les nœuds des clusters
  enfants doivent la joindre.
- **Volume (Gio)** : 20 par défaut, de 5 à 2048, contrôlé contre la place
  de Longhorn (sur-provisionnement et place réelle, répliques de la classe
  sur des nœuds distincts).
- **Classe de stockage** : une classe Longhorn.
- **Nom DNS (facultatif)** : ajouté au certificat à côté de l'adresse.
- **Miroirs** : les registres d'origine qu'il sert. Ils sont fixés au
  déploiement.

Vérifications : `registry-address-invalid`, `registry-address-in-pool`,
`registry-address-vip`, `registry-address-answers`, `registry-name-taken`,
`registry-images-missing`, `registry-tools-missing`,
`registry-storage-class-unknown`, `registry-replicas-nodes`,
`registry-room-short`.

**Ce qui est posé.** Dans le namespace `harvester-ops-registry` : un
Deployment (une réplique, `Recreate`), un PVC Longhorn `<nom>-data`, un
Service `<nom>` de type LoadBalancer annoté `kube-vip.io/loadbalancerIPs`
(sans NodePort), les Secrets du certificat, des entrées htpasswd et des
comptes, et une ConfigMap. Le pod a **deux conteneurs sur le même volume**,
car l'authentification htpasswd de Distribution n'a pas de droits (tout
compte pourrait écrire) :

| Port | Instance | Comptes |
|---|---|---|
| 443 | lecture seule (toute écriture reçoit 405) | `push` et `pull` |
| 5443 | lecture et écriture | `push` seulement |

Les clusters enfants lisent sur 443. La console publie sur 5443. Depuis
la 2.6.1, les deux ports sont **deux Services LoadBalancer à la même
adresse** (`<nom>` pour 443, `<nom>-write` pour 5443), servis tous deux
par le kube-vip de Harvester, pour que le port d'écriture puisse être
réservé à la console.

**Port d'écriture réservé à la console (2.6.1).** Paramètres > Registres
(administrateurs) garde les réseaux de la console admis sur 5443
(adresses ou réseaux IPv4, séparés par des virgules). La fenêtre suggère
l'adresse par laquelle la console sort vers chaque cluster qui porte un
registre d'amorçage. Le réglage est lu à chaque geste, sans redémarrage.

- Il devient `loadBalancerSourceRanges` du Service `<nom>-write` : un
  autre hôte n'a pas de réponse sur 5443 (la connexion expire), le port
  443 reste ouvert à toute adresse. Un seul Service à plages limitées
  couperait aussi 443 aux VMs, d'où le second Service.
- Enregistrer le réglage y porte chaque registre d'amorçage que la console
  déclare : une action suivie par cluster Harvester
  (`registry-write-access:<cluster>`, `harvester-registry write-access`),
  avec votre identité. C'est aussi la reprise d'un registre posé par la
  2.6.0 (5443 sur son Service principal) : le port quitte d'abord le
  Service principal, puis le Service d'écriture est posé ; les écritures
  s'arrêtent quelques secondes, les lectures jamais. Un nouveau registre
  d'amorçage est posé avec le réglage.
- Vide, le port d'écriture accepte toute adresse, comme en 2.6.0, et
  Vérifier dit `registry-write-port-open`. Un fichier de réglages abîmé
  n'est jamais lu comme « ouvert » : le déploiement et `write-access` sont
  refusés tant que le réglage n'est pas réenregistré.
- Le nom d'un registre d'amorçage ne peut pas finir par `-write` (le nom
  du Service d'écriture d'un autre registre), et Supprimer ne retire que
  les objets étiquetés au nom du registre.
- Ce que cela protège : le port d'écriture contre les autres hôtes des
  réseaux qui joignent l'adresse (VMs enfants comprises). Ce que cela ne
  fait pas : ce n'est pas une authentification (une écriture exige
  toujours le compte `push`, et `pull` y est refusé) ; cela ne voit pas à
  travers une traduction d'adresses ou un proxy (le cluster doit voir la
  vraie adresse de la console : derrière un NAT, donnez l'adresse
  traduite) ; et le filtre porte sur l'adresse qui arrive au nœud
  Harvester, donc un hôte qui peut emprunter une adresse admise passe.

- **TLS toujours.** La console génère une CA (10 ans) et un certificat
  serveur (2 ans), ou prend votre certificat (`--cert`, `--key`, `--ca`).
  **La clé de la CA générée est effacée après le déploiement : le
  certificat ne peut pas être renouvelé.** Un nouveau déploiement donne une
  nouvelle CA, et les clusters enfants gardent l'ancienne sur leurs nœuds
  (les registres sont posés à la création seulement, voir plus bas).
  Prévoyez-le : avec la CA générée, le registre a deux ans devant lui.
- **Deux comptes**, `push` et `pull`, aux mots de passe aléatoires (32
  octets). Ils n'existent que dans des Secrets du cluster Harvester. Il n'y
  a jamais d'accès anonyme.
- Le pod tourne en uid 499, système de fichiers racine en lecture seule,
  sans capacité, limite mémoire de 512 Mio, sondes toutes les 60 s.
- **L'image est importée sur les nœuds** listés pour ce cluster dans la
  configuration de la console (`ctr images import` par SSH), comme les
  images de Cluster API l'étaient. **Un nœud ajouté plus tard au cluster ne
  la reçoit pas** : si le pod y est déplacé, il reste en `ErrImageNeverPull`
  (voir [depannage.md](depannage.md)).
- Le déploiement attend que `/v2/` réponde 401 sans compte, puis 200 avec
  le compte de lecture. La déclaration est ensuite enregistrée.

**Supprimer** retire le registre du cluster. Son volume est gardé, un
nouveau déploiement retrouve donc ses images, sauf si vous cochez
**Supprimer aussi ses données** et tapez son nom. La suppression est
refusée tant qu'un cluster enfant, un service, un plan ou un modèle de
cluster l'emploie (`registry-in-use`, avec les noms).

**Un déploiement en échec laisse ses objets.** Si le registre n'est pas
prêt à temps, les objets et le volume restent sur le cluster et aucune
déclaration n'est enregistrée ; un nouvel essai depuis la fenêtre est
refusé (`registry-name-taken`, souvent avec `registry-address-answers`).
L'onglet Registres montre ces objets sur une ligne **Restes** (trouvés par
les étiquettes de la console dans `harvester-ops-registry`), avec les
objets restés et le plan qui les a posés. **Supprimer** sur cette ligne
les retire ; le volume est gardé sauf si vous retapez le nom, comme pour
un registre déclaré. Un plan de déploiement qui les a posés n'est pas
bloqué : sa reprise les reprend (les constats sont rejoués avec l'origine
du plan) et redéploie. En ligne de commande : `harvester-registry
leftovers --cluster C`, puis `harvester-registry delete NOM --cluster C`.

**Pas de nettoyage en 2.6.0.** Le ramasse-miettes de Distribution exige un
registre arrêté ou en lecture seule, et les suppressions sont désactivées.
Ce qui est publié reste. Surveillez l'occupation du volume dans le détail
du registre.

### Rangement dans un registre

Une image `<hôte>/<chemin>:<tag>` est publiée en
`<registre>/<hôte>/<chemin>:<tag>` :

- `docker.io/library/nginx:1.27` devient `<registre>/docker.io/library/nginx:1.27` ;
- un port dans l'hôte s'écrit `_<port>` (`localhost:5000/x` devient
  `<registre>/localhost_5000/x`), car `:` est interdit dans un nom de
  dépôt.

Deux images de même chemin sur deux hôtes ne se mélangent jamais. Les charts
vont sous `<registre>/charts/<chart>:<version>` (un `+` de la version
devient `_`).

## Publication

**Publier** sur un paquet de l'onglet Artefacts (opérateurs) : choisir un
registre rattaché au cluster Harvester, les éléments, lire les
vérifications, puis publier.

- **Les vérifications passent avant toute écriture** : `registry-unknown`,
  `registry-not-attached`, `registry-no-usage` (une image vers un registre
  de charts seuls, ou l'inverse), `package-untrusted-strict`,
  `publish-item-unknown`, et `publish-tag-differs`.
- **`publish-tag-differs`** : un tag déjà présent avec un autre contenu.
  Rien n'est écrasé, et rien n'est écrit du tout.
- **Idempotente** : un tag présent à la même empreinte est sauté. Une
  couche déjà dans le registre n'est pas renvoyée (ou elle est montée
  depuis un autre dépôt du même registre). Une publication interrompue se
  reprend en publiant à nouveau.
- Les images d'abord, puis les charts ; les manifestes et les sortes « à
  venir » sont sautés. La progression montre `images 3/14` et les Mio
  envoyés.
- La déclaration du registre garde la liste des paquets publiés (noms,
  versions, date, personne).

Le compte d'écriture est lu dans le Secret `<registre>-push` du cluster
Harvester et jamais affiché. En ligne de commande : `harvester-artefacts
publish NOM VERSION --cluster C --registry R [--items a,b] [--dry-run]` ;
`--dry-run` liste ce qui partirait et les constats.

## Clusters enfants branchés sur leurs registres

Dans la fenêtre de création de Cluster API > Clusters, le menu
**Registres** porte une liste ordonnée de registres, déclarés et rattachés
à ce cluster Harvester (un registre non rattaché est montré mais ne peut
pas être choisi). Les modèles de cluster et les entrées de plan prennent la
même liste, avec des `{{variables}}`.

- **Posés à la création seulement.** RKE2 lit sa configuration de
  registres au démarrage. Changer les registres d'un cluster existant n'est
  pas proposé en 2.6.0.
- **Ordre** : pour chaque origine miroitée, les registres qui la servent
  sont essayés dans l'ordre de la liste ; le premier est préféré, les
  suivants sont ses secours.
- **Miroirs** : par registre, les origines qu'il sert pour ce cluster,
  choisies parmi celles de sa déclaration.
- La console écrit elle-même `/etc/rancher/rke2/registries.yaml`, par
  des fichiers des gabarits du plan de contrôle et des workers : miroirs
  avec un `rewrite` `^(.*)$` vers `<origine>/$1` (le rangement
  ci-dessus), et par registre son compte et sa CA, dans
  `/etc/rancher/rke2/tls/harvester-ops/<registre>/ca.crt`. Seuls les
  registres qui servent des images sont employés. Elle n'emploie pas le
  champ `privateRegistriesConfig` de CAPRKE2 : en v0.25.2, ce champ ne
  garde le compte et la CA que d'un seul registre, et écrit toutes les CA
  dans le même fichier (vu sur le banc, voir « Vu en réel »).
- **Secrets** dans le namespace du cluster, sur Harvester :
  `<cluster>-registries` (`registries.yaml` et un `ca-<registre>.crt` par
  registre, lus par les nœuds à leur premier démarrage),
  `<cluster>-registry-<registre>-auth` (le compte de lecture) et
  `<cluster>-registry-<registre>-ca` (`Opaque`, `ca.crt` seul), ces deux
  derniers étiquetés au nom du registre (ils disent quels clusters
  l'emploient). Tous sont étiquetés pour partir avec le cluster. **Seul le
  compte de lecture va sur les nœuds** ; le compte d'écriture jamais.
- **Le mot de passe de lecture finit dans le cloud-init des VMs.** CAPRKE2
  recopie `registries.yaml`, mot de passe de lecture compris, dans le
  Secret d'amorçage de chaque Machine, et CAPHV le recopie dans le Secret
  cloud-init de la VM, dans son namespace sur Harvester (qui peut être
  `default`). Quiconque lit les Secrets de ce namespace, ou voit les
  données utilisateur d'une VM dans Harvester, peut lire le compte de
  lecture. `privateRegistriesConfig` fait de même ; seul le compte de
  lecture voyage ainsi.

**Repli vers Internet.** containerd essaie les miroirs, puis le registre
d'origine. La 2.6.0 **ne coupe jamais ce repli par défaut**, registre
d'amorçage compris.

- **Pas de repli vers Internet** (`no_fallback: true` sur une entrée) le
  coupe. RKE2 le coupe pour **tous** les registres miroités du cluster à la
  fois : une seule décision par cluster. Une image absente des registres
  échoue alors au lieu de venir d'Internet.
- C'est refusé (`cluster-registry-no-fallback-system`, bloquant) tant que
  `docker.io` ou `registry.rancher.com` est miroité et qu'aucun registre de
  la liste ne porte les images système de RKE2 (`rancher/rke2-runtime` dans
  un paquet qui y est publié) : les nœuds ne pourraient pas démarrer. Le
  formulaire de création grise la case et dit pourquoi.
- Cela remplace la conception précédente, où un registre d'amorçage
  coupait le repli par défaut.

Constats de la création : `cluster-registry-unknown`,
`cluster-registry-not-attached`, `cluster-registry-mirror-unknown`,
`cluster-registry-no-fallback-system` (bloquants),
`cluster-registry-no-images` (avertissement).

## Charts depuis un registre (Services)

Dans Cluster API > Services, **Depuis un registre** propose les charts
publiés dans les registres rattachés à ce cluster (grisé avec la raison
quand il n'y en a pas).

- CAAPH lit le chart à `oci://<registre>/charts`, avec le nom et la version
  du chart du paquet.
- Valeurs : celles de l'élément du paquet d'abord, les vôtres posées
  par-dessus.
- Identifiants : le compte de lecture, dans un Secret `config.json` de
  Docker `<cluster>-chart-<registre>` ; CA : le Secret
  `<cluster>-registry-<registre>-ca` du cluster (posé s'il manque).
  `insecureSkipTLSVerify` n'est jamais posé.
- Les images du chart sont tirées par le cluster enfant sous leur nom
  d'origine, par ses miroirs. Si le cluster ne tire pas de ce registre,
  l'avertissement `service-registry-not-mirrored` dit que les images
  viendront de leur origine.
- Retirer le service retire son Secret d'identifiants quand aucun autre
  service ne l'emploie ; jamais la CA du cluster.

Constats : `service-registry-unknown`, `service-registry-no-charts`,
`service-registry-not-attached`, `service-package-missing`,
`service-item-not-chart` (bloquants) ; `service-chart-not-published`,
`service-registry-not-mirrored` (avertissements).

## Artefacts dans les clusters enfants (2.7.0)

Un élément de paquet arrive dans un cluster enfant `<ns>/<c>` créé par
Cluster API. Tout ce que la console écrit pour lui vit sur le cluster
Harvester, dans `<ns>` :

| Sorte d'élément | Écrit sur Harvester | Appliqué par |
|---|---|---|
| `chart` | un `HelmChartProxy` du nom de l'élément, étiquette `harvester-ops.io/svc-<élément>: "on"` sur le `Cluster` | CAAPH, depuis un registre où le paquet est publié (comme « Depuis un registre » dans Services) |
| `manifest`, `policy` | un ConfigMap et un `ClusterResourceSet` nommés `art-<élément>` (`strategy: Reconcile`), étiquette `harvester-ops.io/art-<élément>: "on"` sur le `Cluster` | Cluster API, dans le cluster enfant |

Chacun porte les étiquettes `harvester-ops.io/managed`,
`harvester-ops.io/artefact-item` et `harvester-ops.io/artefact-package`, les
annotations `harvester-ops.io/artefact-version` et
`harvester-ops.io/applied-by` et, depuis un plan, l'origine du plan
(étiquette `harvester-ops.io/plan`, annotation `harvester-ops.io/plan-run`).

### Manifestes et politiques : un retrait suivi par la console

Cluster API ne retire jamais ce qu'un `ClusterResourceSet` a appliqué, et un
jeu en `Reconcile` laisse en place un objet sorti de son ConfigMap (vu en
réel, voir plus bas). La console suit donc le retrait elle-même :

- Chaque objet du contenu reçoit les étiquettes `harvester-ops.io/managed`,
  `harvester-ops.io/artefact-item` et `harvester-ops.io/artefact-cluster`.
- **Mise à jour** (nouvelle version de l'élément) : les objets qui quittent
  le contenu sont écrits dans une annotation du ConfigMap avant tout le
  reste, le nouveau contenu est appliqué, la console attend que le binding
  porte la nouvelle empreinte, puis supprime ces objets dans le cluster
  enfant. Un déroulé arrêté en route termine le travail à la mise à jour ou
  au retrait suivant.
- **Retrait** : le `ClusterResourceSet` d'abord (plus de réapplication),
  l'étiquette du `Cluster`, puis les objets un par un, namespacés d'abord,
  `Namespace` et portée cluster en dernier, puis le ConfigMap. Chaque
  suppression est jugée par le serveur (`--dry-run=server`) avant la
  première.
- **Seuls partent les objets qui portent encore les étiquettes.** Un objet
  modifié à la main (étiquettes retirées) est gardé et nommé
  (`kept-relabelled`). Un `Namespace` livré n'est supprimé que s'il ne porte
  rien d'étranger (une charge ou un volume qui n'est pas de l'élément) :
  `kept-in-use`. Une sorte qui n'est plus servie compte comme partie. Un
  ConfigMap supprimé à la main perd la liste des objets : le retrait le dit
  et les objets étiquetés restent.
- Cluster enfant déjà supprimé : seuls le `ClusterResourceSet` et son
  ConfigMap sont retirés (« objets partis avec lui »).

Objets acceptés dans un manifeste : toute sorte namespacée hors des
namespaces système du cluster enfant (`kube-system`, `*-system`,
`cattle-*`, `tigera-operator`...) ; de portée cluster, seulement
`Namespace`, `ClusterRole`, `ClusterRoleBinding` (jamais vers
`cluster-admin`, jamais vers un sujet `system:*`), `StorageClass`,
`PriorityClass` et `IngressClass`. Refusés : CRD, webhooks, politiques
d'admission, `APIService`, `Node`, `PersistentVolume`, `CSIDriver`,
`RuntimeClass` (un opérateur s'installe par un chart). Un Secret de type
`kubernetes.io/service-account-token` est refusé. 500 objets et 900 Kio au
plus par élément. Une `List` est dépliée, de même que les `<Sorte>List` des
groupes de Kubernetes ; une sorte d'un autre groupe finie en `List` reste un
objet. Une `ClusterRole` à règles jokers liée par une `ClusterRoleBinding`
est acceptée : qui l'applique peut de toute façon lire le kubeconfig
administrateur du cluster enfant (règle plus bas).

### Socle de politiques, rôles et comptes d'automatisation

La console fournit un socle de politiques, **décoché par défaut**. Coché, il
pose dans chaque namespace d'équipe (1 à 16 par cluster, jamais un
namespace système, étiquette `harvester-ops.io/team: "true"`) :

- `ResourceQuota` `hops-quota` : `requests.cpu: "4"`, `requests.memory:
  8Gi`, `limits.cpu: "8"`, `limits.memory: 16Gi`, `pods: "50"`,
  `persistentvolumeclaims: "10"`, `requests.storage: 200Gi`,
  `services.loadbalancers: "2"` ;
- `LimitRange` `hops-limits` : défaut 500m et 512Mi, demande par défaut 100m
  et 128Mi, maximum 2 CPU et 4Gi (chaque valeur se règle ; une demande
  au-dessus de son défaut, ou un défaut au-dessus du maximum, est refusé) ;
- `NetworkPolicy` : `hops-default-deny` (tous les pods, entrée et sortie),
  `hops-allow-same-namespace`, `hops-allow-dns` (vers `kube-dns` de
  `kube-system`, UDP et TCP 53), `hops-allow-ingress` (depuis le contrôleur
  d'ingress de RKE2, `kube-system`, pods
  `app.kubernetes.io/name: rke2-ingress-nginx`).

Un namespace d'équipe déjà présent sans les étiquettes de la console bloque
(`team-namespace-taken`) ; le socle n'écrit jamais dans `default` ni dans un
namespace qu'il n'a pas créé.

- **Rôles par groupe** : une `RoleBinding` dans un namespace d'équipe vers
  les rôles intégrés `admin`, `edit` ou `view`, sujet `Group`. Aucune
  `ClusterRoleBinding`.
- **Comptes d'automatisation** : 16 par cluster au plus, un
  `ServiceAccount` dans un namespace d'équipe
  (`automountServiceAccountToken: false`) et une `RoleBinding`
  `hops-sa-<nom>` vers `edit` ou `view`. Aucun Secret de jeton n'est jamais
  créé.

Socle décoché, rôles et comptes vont dans des namespaces d'équipe qui
doivent déjà exister (`team-namespace-missing` sinon). Le tout est rendu
comme un élément nommé `hops-policies` (paquet `harvester-ops`, version
`builtin`), livré et retiré comme un manifeste. Les éléments `policy` d'un
paquet sont des manifestes ordinaires, contrôlés de la même façon.

### Jetons

**Émettre un jeton** (Cluster API > Clusters, carte du cluster enfant, ou
`harvester-capi token`) demande au cluster enfant un `TokenRequest` pour un
compte d'automatisation : de 600 à 86 400 secondes, 3 600 par défaut. Le
jeton est montré une fois, avec un bouton Copier, puis effacé de la page à
la fermeture de la fenêtre (un clic à côté ne la ferme plus une fois le
jeton montré). Il n'est jamais rangé : ni dans une action, une étape, un
journal, le flux d'événements ou `actions.db` ; la réponse part avec
`Cache-Control: no-store`. L'API du cluster enfant l'accepte encore environ
une minute après son expiration (tolérance d'horloge) : « valable N s, plus
une minute au plus ». Un compte sans les étiquettes de la console est refusé
(`account-not-managed`).

### Gestes hors plan

- **Onglet Artefacts, Appliquer** sur un élément chart, manifeste ou
  politique : une fenêtre Paramètres (cluster enfant, registre pour un
  chart, valeurs), Vérifications et Résumé. Appliquer reste grisé tant que
  les constats des valeurs courantes ne sont pas revenus, et reste grisé
  une fois lancé.
- **Cluster API > Services** : charts, manifestes et politiques dans un
  seul tableau, colonne **Sorte**, état du binding pour un manifeste,
  **Retirer**.
- **Cluster API > Clusters** : comptes d'automatisation d'un cluster enfant
  et **Émettre un jeton**.

### Rôles

| Geste | Rôle |
|---|---|
| Lire les éléments appliqués | viewer |
| Appliquer ou retirer un élément, socle et comptes, émettre un jeton | operator **et** le droit de lire le Secret `<c>-kubeconfig` du cluster enfant |
| Défaire un déploiement, désinstaller la pile Cluster API | admin |

Qui lit `<c>-kubeconfig` est administrateur du cluster enfant : le serveur
contrôle ce droit par une `SelfSubjectAccessReview` faite avec l'identité de
la personne avant tout lancement (`child-admin-required`, 403).

### Ligne de commande

```
harvester-capi manifest-check  --spec F
harvester-capi manifest-deploy --spec F [--timeout 900]
harvester-capi manifest-remove --name NS/C/ELEMENT [--dry-run]
harvester-capi manifest-verify --name NS/C/ELEMENT
harvester-capi manifests [--json]
harvester-capi policy-render   --spec F
harvester-capi token --name NS/C --account NS/SA [--seconds N]
```

Une spec : `{cluster: "ns/c", package, version, item}` ou `{cluster,
policies: {base, teams, roles, accounts}}`. `token` écrit le jeton sur la
sortie standard seulement. Les fichiers de travail des outils (et le
kubeconfig du cluster enfant, 0600, effacé à la fin) vont dans
`HARVESTER_OPS_WORK_DIR`, ou `<état>/work` (0700) ; jamais `/tmp` nu (un
sous-répertoire donné par la variable est accepté, propriétaire et mode
contrôlés).

## Rôles

| Geste | Rôle |
|---|---|
| Lire les paquets, les registres et les réglages de confiance | lecteur |
| Importer ou retirer un paquet ; mode strict et clés de confiance | admin |
| Déclarer, modifier ou oublier un registre du client | admin |
| Déployer ou supprimer un registre d'amorçage | admin |
| Rattacher un registre du client à un cluster Harvester, vérifier un registre | opérateur |
| Publier un paquet | opérateur |
| Choisir des registres à la création d'un cluster enfant, déployer un service depuis un registre | opérateur |

Les écritures sur le cluster Harvester se font avec l'identité de la
personne, comme en 2.5.0.

## Ligne de commande : `harvester-registry`

```
harvester-registry declare NOM --file r.yaml|- [--revision N]
harvester-registry attach  NOM --cluster C          (comptes en JSON sur stdin)
harvester-registry check   NOM --cluster C [--vm-network NS/NOM]
harvester-registry list | show NOM | forget NOM [--kubeconfig-for C=FICHIER ...]
harvester-registry deploy  NOM --cluster C --address IP --size-gib N --storage-class S
                           [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                           [--timeout S] [--dry-run]
harvester-registry status  NOM --cluster C
harvester-registry leftovers   --cluster C
harvester-registry delete  NOM --cluster C [--delete-data]
harvester-registry write-access [NOM] --cluster C
harvester-registry settings [--write-sources A,B | --clear]
```

`attach` lit `{"push": {"username", "password"}, "pull": {...}}` sur
l'entrée standard. `deploy --dry-run` ne fait que les vérifications.
`check --vm-network` ajoute la sonde depuis ce réseau de VMs. `settings`
montre ou pose les réseaux de la console admis sur le port d'écriture ;
`write-access` y porte les registres d'amorçage d'un cluster (ou celui
nommé). Codes
de sortie : 0 succès, 1 échec, 2 refus (constat bloquant, registre
employé...).

## Hors de la 2.7.0

- Les éléments **GitOps** (Fleet, Flux) et l'OIDC de l'API des clusters
  enfants : 2.8.0. La sorte `gitops` reste « à venir (2.8) ».
- Le renouvellement du certificat d'un registre d'amorçage (sa CA n'est pas
  gardée), la restriction du port 5443 à l'adresse de la console au-delà de
  la 2.6.1, le format des Secrets de registre (m-14) : plus tard.
- Un cluster enfant amorcé **entièrement sans Internet** (RKE2 lui-même et
  ses images système depuis le registre, une image de VM préparée) : plus
  tard.
- **Changer les registres d'un cluster existant** : plus tard, par
  renouvellement des machines.
- Un registre Harbor posé par la console. Un Harbor du client s'emploie
  comme registre du client ; ce n'a pas été essayé en réel.
- Brancher le réglage `containerd-registry` des nœuds Harvester sur les
  registres déclarés : le réglage reste un geste manuel (Réglages).
- Le ramasse-miettes et l'agrandissement d'un registre d'amorçage.
- La sonde depuis le réseau des VMs et le port d'écriture réservé à la
  console sont arrivés en 2.6.1 (voir plus haut).

**Vu en réel (2.6.0).** Banc harvlab2 (Harvester v1.9.0, un nœud
imbriqué, 32 Gio), console de cette version, 7 novembre 2026.

- **Paquet** `banc-260` 1.0.0 : le chart podinfo 6.9.2, l'image
  `ghcr.io/stefanprodan/podinfo:6.9.2`, la même rangée sous
  `docker.io/harvops-proof/podinfo:2.6.0` (un nom qui n'existe pas sur
  Internet), un manifeste. `pack` a pris 6,5 s pour 31,3 Mio ; les
  couches partagées par les deux noms ne sont rangées qu'une fois.
- **Import.** Un octet changé, avec sa signature : refusé
  (`invalid-signature`). Le même sans signature : accepté à
  l'acquittement, puis refusé à l'extraction (`sums-mismatch`). Non signé
  et clé inconnue : 409 jusqu'à l'acquittement, puis importés, avec la
  ligne du journal et `trust.json`. Le mode strict a refusé les deux. La
  clé ajoutée aux clés de confiance, le paquet signé était `valid` et
  importé en 1 s environ. `harvester-artefacts import` a rendu 3 sans
  l'acquittement.
- **Registres.** Registre d'amorçage de 8 Gio sur une classe à une
  réplique, posé et prêt en 37 s (anonyme 401, compte de lecture 200) ;
  publication en 3 s. Registre du client simulé par Distribution 3.1 sur
  un autre hôte, avec sa propre CA et deux comptes : rattachement 2 s,
  publication 2 s.
- **Cluster enfant** 1+1 créé avec les deux registres (client en premier)
  en 10 minutes environ. Chaque nœud porte les deux registres, avec le
  seul compte de lecture, et un fichier de CA par registre.
- **Sans Internet.** Une règle nftables temporaire sur l'hôte du banc
  jetait tout paquet des VMs du cluster enfant vers l'extérieur du LAN
  (Internet joignable avant, sans réponse après). Une image absente des
  registres a échoué (`not found` une fois le repli expiré, comme le
  montre `containerd.log`). L'image renommée et l'image podinfo ont été
  tirées des registres en 3 s et moins, les journaux du registre montrant
  le compte `pull` ; des pods sur l'image renommée ont tourné sur les deux
  nœuds. Le registre du client a servi en premier ; une fois arrêté, le
  registre d'amorçage a servi le même tirage (`connection refused`, puis
  l'hôte suivant).
- **Chart par CAAPH** depuis le registre d'amorçage (Services, « Depuis
  un registre »), Internet toujours coupé pour le cluster enfant :
  HelmReleaseProxy prêt, release déployée en 15 s, pod en marche. CAAPH
  tourne sur le cluster Harvester ; le chart a été lu dans le registre
  (son journal montre le client Helm).
- **Robustesse.** Pod du registre supprimé : revenu en quelques secondes,
  contenu intact. Supprimé en gardant ses données, puis redéployé sur le
  volume en 26 s : toutes les images étaient là, avec une nouvelle CA,
  comme dit plus haut. Supprimé avec ses données après le nom retapé (un
  nom faux a été refusé). Suppression, oubli et retrait refusés tant que
  le cluster et le plan les employaient.
- **Trouvé et corrigé sur le banc :** avec deux registres, CAPRKE2 v0.25.2
  ne gardait qu'un registre sur les nœuds (le registre du client n'avait
  ni sa CA ni son compte) ; un disque racine plus petit que l'image
  (l'image openSUSE Leap 15.6 publiée annonce désormais 10 Gio) laissait
  la VM en Scheduling ; oublier un registre du client laissait ses comptes
  sur le cluster Harvester.
- **Réglage du banc.** Le contrôle de place de la console refusait deux
  disques de 10 Gio sur le disque Longhorn de 48 Gio de harvlab2. Le
  réglage Longhorn `storage-minimal-available-percentage` a été abaissé
  de 25 à 15 pendant chaque création, puis remis.

**Vu en réel (2.6.1).** Banc harvlab2, console de cette version,
7 novembre 2026. Registre d'amorçage de 8 Gio en 172.16.2.76.

- **Sonde depuis `default/lab-net`** (sans VLAN, DHCP de Pi-hole) : bail
  obtenu puis rendu (absent des baux DHCP ensuite), poignée de main TLS
  faite, `registry-vm-network-ok` en 11 à 14 s ; le contrôle de création
  d'un cluster enfant sur ce réseau a rendu
  `cluster-registry-vm-network-ok`. Aucun Job ni pod resté dans
  `harvester-ops-probe` après chaque contrôle.
- **Réseaux qui ne joignent pas le registre**, faits pour l'essai sur les
  VLAN 998 et 999 : sans DHCP ni IPAM, `registry-vm-network-unchecked`
  avec la raison `no-address` (28 s, l'attente du DHCP) ; un IPAM statique
  sans passerelle, le même avec `ipam-incomplete` (7 s) ; un IPAM statique
  avec une route par une passerelle qui n'existe pas,
  `registry-unreachable-from-vm-network` (`tcp`), et dans le contrôle de
  création d'un cluster enfant qui coupe le repli,
  `cluster-registry-unreachable-from-vm-network` bloquant.
- **Port d'écriture.** Un registre façonné comme en 2.6.0 (5443 sur le
  Service principal, port ajouté par un autre gestionnaire de champs) a été
  repris en enregistrant `172.16.1.11/32` ; un nouveau registre a ensuite
  été posé avec le réglage. Les deux fois : deux Services à la même
  adresse, 443 répond depuis node1 et depuis node2, 5443 répond depuis
  node1 (la console) et expire depuis node2. Vérifier a sondé les deux
  comptes à travers lui sans constat.
- L'étape TLS de la sonde a aussi été jouée contre un serveur TLS derrière
  un mandataire inverse, sur un réseau local : réponse avec le nom en SNI,
  refus sans lui.

**Vu en réel (2.7.0).** Banc harvlab2 (Harvester v1.9.0, Turtles avec
Cluster API v1.13.3, CAAPH v0.6.4, CAPHV v0.10.1, fournisseur RKE2
v0.25.2), console de cette version, 7 novembre 2026. Paquet `banc-270`
1.0.0 (chart et image podinfo, un manifeste d'un ConfigMap et d'un
Deployment, un élément `policy` d'une NetworkPolicy, un manifeste `extra`)
et 1.0.1 (le manifeste sans le Deployment), signé par une clé inconnue de la
console.

- **Plan** `banc270` sur harvlab2 : Terraform (un réseau de VM et une clé
  SSH), registre d'amorçage en 172.16.2.76 avec la publication, la pile
  Cluster API (déjà là, vérifiée), un cluster enfant 1+1, puis la phase
  Artefacts (socle de deux namespaces d'équipe, dont un avec un quota de
  4 pods, un rôle `view` pour un groupe, un compte d'automatisation `ci`,
  le manifeste, la politique, le chart podinfo). Terraform 8 s, registre
  45 s, publication 2 s, cluster 623 s ; arrêté au début de la phase
  Artefacts, repris et arrêté après le socle, repris encore : chaque
  élément précédent `already done (checked)`, les deux derniers appliqués
  en 1 min 17 s. Dans le cluster enfant : les deux namespaces d'équipe et
  leurs étiquettes, `hops-quota`, `hops-limits`, les quatre
  NetworkPolicies, le compte et sa `RoleBinding`, les objets du manifeste
  aux trois étiquettes, la release podinfo en marche ; sur harvlab2 les
  trois `ClusterResourceSet` appliqués, leur binding à l'empreinte, le
  `HelmChartProxy` prêt, chacun portant l'exécution qui l'a créé (trois
  exécutions différentes de la même chaîne).
- **Politiques.** Un quatrième pod du namespace d'équipe refusé
  (`exceeded quota: hops-quota`) ; un conteneur sans ressources a reçu
  100m/128Mi demandés et 500m/512Mi de limites ; depuis le namespace en
  refus par défaut, `wget` vers un autre namespace a expiré alors que le
  DNS répondait, le même depuis le namespace sans refus est passé ; une
  Ingress du namespace d'équipe a répondu depuis node1 sur les deux nœuds
  par le contrôleur de RKE2.
- **Jeton** de 600 s pour `ci` : liste les comptes de son namespace,
  `Forbidden` dans `kube-system`, `Cache-Control: no-store`, et sa valeur
  introuvable dans `actions.db`, les journaux et l'état (recherche de la
  valeur). Un jeton de 600 s émis sur harv4 expirait à 18:27:11 : encore accepté 35 s après, `Unauthorized` 81 s après.
- **Mise à jour et retrait.** Le plan passé en 1.0.1 : le Deployment sorti
  du contenu a été supprimé du cluster enfant (déroulé de 1 min 28 s). Les
  étiquettes du ConfigMap retirées à la main, puis l'élément retiré
  (1,7 s) : le ConfigMap gardé et nommé `kept-relabelled`, le
  `ClusterResourceSet` et son ConfigMap partis.
- **Défaire, et ce qu'il ne doit pas toucher.** Avant le Défaire, trois
  leurres sans la marque d'exécution : le même élément `app` appliqué de
  nouveau hors plan (même nom de `ClusterResourceSet` `art-app`), un
  élément `extra` appliqué hors plan, et un ConfigMap `art-app` étiqueté
  comme ceux de la console dans `default` de harvlab2. Le plan de
  destruction (15 s) a nommé `app` comme laissé tel quel. Premier Défaire
  (20 s) : chart, politique et socle retirés (namespaces d'équipe gardés :
  `kept-in-use`, ils portaient des pods faits à la main) ; `app`
  `undo-not-ours`, donc le cluster et le contenu Terraform sont restés
  (sautés). Les trois leurres ont survécu, avec leurs objets dans le
  cluster enfant. Les deux éléments leurres retirés hors plan, le Défaire
  relancé : ce qui était parti vérifié et sauté, cluster supprimé (265 s),
  déclaration détruite et retirée (2 s) ; l'exécution disait « Défait en
  partie » (registre laissé). Le Défaire du registre était refusé sur le
  banc (`registry-in-use` : le plan défait désigne son propre registre),
  corrigé (`--ignore-plan`), puis retiré en 2 s, volume gardé ;
  l'exécution est passée « Défait » et un nouveau Défaire a été refusé
  (`undo-already-done`). Le leurre `default/art-app` était toujours là à
  la fin. Harvester et la pile Cluster API intacts.
- **Défaire interrompu.** Un autre déroulé (un cluster, un manifeste,
  608 s) : la suppression du cluster annulée depuis le dock au bout de
  30 s, le Défaire fini en partie avec le cluster en Deleting ; relancé,
  il a vérifié le manifeste et fini la suppression en 2 min 11 s, Défait.
- **Désinstallation Turtles.** Refusée tant que le cluster enfant et le
  chart existaient (`capi-clusters-present`, `caaph-services-present`),
  dans l'action suivie, rien touché. Après le Défaire : essai à blanc, puis
  retrait en 26 s des quatre `CAPIProvider` de la console, de leurs
  déploiements et des ConfigMaps du paquet ; `core/cluster-api` et Turtles
  en marche, CRD et namespaces gardés, le service `ingress-expose` (pas
  posé par la console sur ce banc) gardé. **Installer** a reposé la pile
  en 52 s, prête aux mêmes versions.
- **HarvesterCluster restant.** Avec un propriétaire disparu et un
  finaliseur : `check` bloquant (`harvester-cluster-leftover`, finaliseur
  nommé), `delete` a attendu 120 s et nommé le finaliseur, que la console a
  laissé. Sans propriétaire : un avertissement, puis retiré par `delete`
  en 6 s.
- **Deux clusters enfants sur harv4** (bare-metal, Harvester v1.9.0) : un
  plan avec un registre d'amorçage, deux clusters 1+1, le manifeste
  `extra` et le chart podinfo sur les deux : registre 41 s, publication
  3 s, clusters 477 s et 499 s, manifestes 7 s, charts 14 s, 19 min 20 s
  en tout. Puis un seul Défaire de tout le déroulé, registre et données compris (deux noms tapés) : les deux charts et manifestes retirés en 1 à 2 s chacun, clusters supprimés en 169 s et 177 s, registre et volume retirés en 2 s, 6 min 15 s en tout, exécution « Défait » ; un ConfigMap leurre `art-extra` étiqueté comme ceux de la console dans `default` a survécu. Objets du banc retirés, cluster arrêté proprement et node4 éteint.
- **Trouvé et corrigé sur le banc :** l'étape registre d'un Défaire
  toujours refusée par le plan défait ; un Défaire relancé aussitôt refusé
  par le verrou qu'il rendait ; un Défaire en échec après un Défaire en
  partie faisait passer l'exécution de « en partie » à « en échec » ; la
  fenêtre du Défaire nommait la dernière reprise comme créatrice.
- **Réglage du banc.** Longhorn `storage-minimal-available-percentage` 25
  -> 15 pendant les créations, remis ; image, paire de clés et pools
  d'adresses du banc créés pour l'essai puis supprimés.

Non vérifié en réel (2.7.0) : plus de deux clusters enfants, un Rancher
extérieur au banc qui importe les clusters enfants, une personne Rancher
restreinte qui émet un jeton (essai par usurpation seulement), l'option
`--remove-images` de la désinstallation.

Non vérifié en réel (2.6.0) : un Harbor du client, un cluster enfant amorcé
entièrement sans Internet (hors de la 2.6.0), un registre d'amorçage sur
un cluster de plusieurs nœuds, plus d'un cluster enfant à la fois sur un
registre, des paquets de plusieurs gigaoctets, et la reprise d'une
publication arrêtée en cours (tests seulement : la publication du banc a
pris 3 s).
