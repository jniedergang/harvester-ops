# Paquets d'artefacts et registres (2.6.0)

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

La phase Artefacts complète des plans de déploiement (charts et manifestes
appliqués, retrait propre) arrive en 2.7. La phase Registre des plans est
décrite dans [bare-metal.md](bare-metal.md), section « Plans de
déploiement : phase Registre (2.6.0) ».

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

Sortes d'éléments : `chart`, `images`, `manifest`. Deux sortes de plus,
`policy` et `gitops`, sont gardées dans le paquet et montrées « à venir
(2.7) » ; elles ne sont jamais appliquées ni publiées.

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
Constats : `registry-unknown`, `registry-not-attached`,
`registry-unreachable`, `registry-ca-invalid`, `registry-auth-refused`,
`registry-no-usage` (bloquants) ; `registry-pull-is-push`,
`registry-insecure`, `registry-vm-network-unchecked` (avertissements).
`registry-insecure` vient de `insecure_skip_verify: true` dans un fichier
de déclaration : les nœuds ne vérifient alors pas le certificat.

**La joignabilité du registre n'est vérifiée que depuis le cluster
Harvester, pas depuis le réseau des VMs.** La console et le cluster
Harvester sondent l'adresse ; aucun contrôle ne part du réseau des VMs des
clusters enfants. Chaque vérification le dit par un avertissement :
`registry-vm-network-unchecked` (Vérifier, fenêtre du registre
d'amorçage et constats des plans) et `cluster-registry-vm-network-unchecked`
(contrôle de création d'un cluster enfant, un par registre qui sert des
images). Un réseau de VMs qui ne route pas vers le registre (VLAN sans
route, pare-feu) n'est pas détecté : les nœuds se replient alors sur
Internet, ou échouent à tirer quand `no_fallback` est posé. Assurez-vous
que les VMs enfants atteignent l'adresse avant de compter dessus. Une
sonde par un pod attaché au réseau des VMs arrive en 2.6.1.

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

Les clusters enfants lisent sur 443. La console publie sur 5443. Le port
5443 est ouvert à toute machine qui joint l'adresse, réseaux des VMs
compris : une écriture exige toujours le compte `push`, et le compte
`pull` y est refusé.

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
harvester-registry check   NOM --cluster C
harvester-registry list | show NOM | forget NOM [--kubeconfig-for C=FICHIER ...]
harvester-registry deploy  NOM --cluster C --address IP --size-gib N --storage-class S
                           [--dns-name N] [--mirrors h1,h2] [--cert F --key F --ca F]
                           [--timeout S] [--dry-run]
harvester-registry status  NOM --cluster C
harvester-registry leftovers   --cluster C
harvester-registry delete  NOM --cluster C [--delete-data]
```

`attach` lit `{"push": {"username", "password"}, "pull": {...}}` sur
l'entrée standard. `deploy --dry-run` ne fait que les vérifications. Codes
de sortie : 0 succès, 1 échec, 2 refus (constat bloquant, registre
employé...).

## Hors de la 2.6.0

- La **phase Artefacts** des plans (charts, manifestes et politiques
  appliqués, retrait propre) : 2.7.
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
- Sonder le registre depuis le réseau des VMs : les vérifications
  avertissent que la joignabilité n'est vue que depuis le cluster
  Harvester (voir Vérifier plus haut). La sonde par un pod arrive en
  2.6.1 ; son image (`bci-busybox`) est déjà dans le paquet du registre de
  la 2.6.0, importée sur les nœuds avec l'image du registre, et ne sert
  encore à rien d'autre.
- Restreindre le port d'écriture 5443 à l'adresse de la console. Essayé
  sur le banc : un second service LoadBalancer à la même adresse, avec le
  seul port d'écriture et `loadBalancerSourceRanges` à l'adresse de la
  console, partage l'adresse par le kube-vip de Harvester, et la plage est
  appliquée (la console a reçu 401 puis 200 avec `push`, un autre hôte du
  LAN est resté sans réponse, le port 443 ouvert à tous). Pas livré : il
  faut l'adresse source de la console telle que le cluster la voit, un
  réglage à concevoir (NAT, réseau des conteneurs).

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

Non vérifié en réel : un Harbor du client, un cluster enfant amorcé
entièrement sans Internet (hors de la 2.6.0), un registre d'amorçage sur
un cluster de plusieurs nœuds, plus d'un cluster enfant à la fois sur un
registre, des paquets de plusieurs gigaoctets, et la reprise d'une
publication arrêtée en cours (tests seulement : la publication du banc a
pris 3 s).
