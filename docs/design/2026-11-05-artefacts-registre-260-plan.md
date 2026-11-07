# 2.6.0 : paquet d'artefacts signé et registres des clusters enfants (plan de réalisation)

Conceptions : `2026-11-02-bare-metal-post-installation.md` (décisions 111 à
131 retenues, avec les précisions du propriétaire du produit sur 119 à 122),
plans de la 2.4.0 `2026-11-03-plans-deploiement-240-plan.md` et de la 2.5.0
`2026-11-05-plans-capi-250-plan.md`. Point de départ : 2.5.0 (phases Cluster
API et Clusters, modèles de cluster, identité CAPHV restreinte).

## But

La 2.6.0 apporte de quoi livrer du contenu aux clusters enfants sans accès
à Internet. La phase Artefacts complète (charts, manifestes, politiques,
retrait propre) reste pour la 2.7.0.

1. **Un paquet d'artefacts** : un `tar.gz` au format du paquet de mise à jour
   (décision 121), signé par une clé du client. Il contient un index, des
   charts, des images en disposition OCI, des manifestes et des empreintes.
   Il est construit sur une machine connectée par `harvester-artefacts
   pack`, en bibliothèque standard seulement (décision 122). Il est importé
   dans la console par téléversement ou par `harvester-artefacts import`.
2. **Une confiance permissive par défaut** (précision de 121). Un paquet non
   signé, ou signé par une clé inconnue, est accepté après un avertissement
   clair et un acquittement explicite, noté au journal. Un réglage
   d'administration impose le mode strict. Un paquet dont la signature ou
   les empreintes sont fausses est toujours refusé (point à trancher 184).
3. **Plusieurs registres déclarés** (précision de 119) : par cluster, par
   usage (images, charts), en miroir les uns des autres. Deux sortes :
   - le **registre du client**, existant, déclaré avec son adresse, sa CA et
     ses identifiants ;
   - le **registre d'amorçage**, posé par la console sur le cluster
     Harvester : CNCF Distribution (décision 120), dans l'image maintenue par
     SUSE (point 181). Le registre privé de SUSE, basé sur Harbor, sera
     proposé plus tard : le format le prévoit (`software`), rien d'autre.
4. **La publication d'un paquet dans un registre** : images et charts
   recopiés couche par couche par l'API de registre v2, sans podman ni
   skopeo, par le code de `oci_push.py`.
5. **Les clusters enfants branchés sur leurs registres** : configuration de
   registres privés de RKE2 (miroirs, CA, compte de lecture) posée à la
   création, depuis le formulaire et depuis les modèles de cluster.
6. **Les charts tirés du registre par CAAPH** : un service de Cluster API >
   Services peut viser un chart publié (`oci://`), avec CA et identifiants.
7. **La phase Registre des plans** : poser ou relire les registres, publier
   les paquets choisis, avant la création des clusters.

La phase Artefacts reste désactivée, avec la mention « à venir (2.7) ».

## Architecture

```
bin/lib/oci_push.py        + flux (blobs vers et depuis un fichier, sans tout
                           garder en mémoire), CA propre au registre, HEAD de
                           manifeste, montage entre dépôts
bin/lib/oci_layout.py      nouveau : disposition OCI sur disque (lire, écrire,
                           index), copie d'une image d'un registre vers la
                           disposition et de la disposition vers un registre,
                           charts Helm en artefacts OCI, archive pour ctr
bin/lib/artefacts.py       nouveau : index du paquet, inspection sûre,
                           SHA256SUMS, confiance (signature à trois états),
                           décision permissive ou stricte, extraction vérifiée
bin/lib/self_update.py     verify_signature paramétrée (espace de noms,
                           identité) ; inchangée pour la mise à jour
bin/lib/registries.py      nouveau : déclarations de registres, rendu de la
                           configuration RKE2, constats (pur)
bin/lib/registry_seed.py   nouveau : manifestes du registre d'amorçage,
                           configuration de Distribution, état (pur)
bin/lib/node_images.py     nouveau : import d'images sur les nœuds par ctr,
                           extrait de harvester-capi push_images
bin/lib/capi_cluster.py    + option `registries`, postprocess (miroirs RKE2)
bin/lib/capi_services.py   + identifiants et CA d'un dépôt OCI
bin/lib/deploy_plans.py    + phase registry, champ registries des clusters
bin/lib/deploy_chain.py    ORDER : registry avant capi ; COMING = artefacts
bin/harvester-artefacts.py nouveau : pack, sign, verify, inspect, import,
                           list, remove, publish
bin/harvester-registry.py  nouveau : declare, deploy, status, check, delete
bin/harvester-capi.py      push_images par node_images ; create avec
                           registres
web/app.py                 routes des paquets, des registres, réglages de
                           confiance, phase registry, constats
web/static/js/             artefacts.js (nouveau), registries.js (nouveau),
                           settings.js, capi-create.js, capi.js (services),
                           bm-plans.js, i18n.js
container/Containerfile    + openssl, + htpasswd (point 183)
package.sh                 + bundles/registry (images du registre d'amorçage)
scripts/registry-bundle.json nouveau : description de ce paquet interne
```

Ce qui est réutilisé, sans copie :

| Besoin | Mécanisme existant | Où |
|---|---|---|
| Recopier des couches d'image | `oci_push.Registry` (jeton Bearer ou Basic, redirections sans identifiant, contrôle d'empreinte) | `bin/lib/oci_push.py` (1.75.0) |
| Format et signature du paquet | `ssh-keygen -Y sign/verify`, fichier `allowed_signers`, `.sig` détachée, inspection sûre de l'archive | `package.sh` l. 164-178, `bin/lib/self_update.py` (`inspect_archive`, `verify_signature`) |
| Réception d'un gros fichier | flux vers `.part`, progression, annulation, place vérifiée avant | `web/app.py` `api_update_upload`, `_receive_archive` |
| Images sur les nœuds Harvester sans registre | `ctr images import` par SSH | `bin/harvester-capi.py` `push_images` |
| Adresse d'un service sur le cluster Harvester | service LoadBalancer annoté `kube-vip.io/loadbalancerIPs`, servi par le kube-vip de Harvester | `bin/lib/capi_stack.py` `shim_manifest` |
| Place Longhorn | `longhorn_room.fits` (deux contraintes, répliques sur des nœuds distincts) | `bin/lib/longhorn_room.py` |
| Adresses de pool en plus des machines | `capi_cluster.check_many(..., extra_addresses=...)` | `bin/lib/capi_cluster.py` |
| Secrets du déroulé | `secret_store` (`plan-run/<id>/...`, effacés à la fin) | `bin/lib/secret_store.py` |
| Chaîne, phases à éléments, reprise | `deploy_chain.run`, `Phase(items=...)`, `before`, `verify_item` | `bin/lib/deploy_chain.py` |
| Charts par CAAPH | `capi_services.manifest` (`URL_RE` accepte déjà `oci://`) | `bin/lib/capi_services.py` |

## Contraintes communes (valeurs exactes)

- **Bibliothèque standard** pour `harvester-artefacts pack`, `sign`,
  `verify` et `inspect`, et pour les modules qu'ils chargent (`oci_push`,
  `oci_layout`, `artefacts`). Preuve par un test qui les lance avec
  `python3 -I -S` (pas de site-packages). `ssh-keygen` (OpenSSH 8.2 ou
  plus) est le seul outil externe, et seulement pour signer ou vérifier.
- **Paquet** : fichier `<nom>-<version>.tar.gz`, signature détachée
  `<nom>-<version>.tar.gz.sig`. `nom` : RFC 1123, 40 caractères au plus,
  commence par une lettre (`artefacts.NAME_RE`). `version` : celle de
  `self_update.VERSION_RE`, ou une date `AAAA.MM[.JJ][-n]`
  (`artefacts.VERSION_RE`). Un seul répertoire racine `<nom>-<version>/`.
  Contenu :

  ```
  <nom>-<version>/
    SHA256SUMS            premier membre de l'archive (extraction en flux)
    artefacts.yaml        l'index (écrit en JSON, YAML valide)
    charts/<chart>-<version>.tgz
    images/oci-layout
    images/index.json     une entrée par image, annotations ci-dessous
    images/blobs/sha256/<empreinte>   couches partagées entre images
    manifests/<élément>/<fichier>.yaml
  ```

- **Signature** : espace de noms `harvester-artefacts` (jamais celui de la
  console, `harvester-ops-release` : une signature de release ne vaut pas
  pour un paquet, et l'inverse). Clés de confiance du client au format
  `allowed_signers` d'OpenSSH, dans `<état>/artefacts/signers` (0600).
  Principal libre (celui du client), retrouvé par `ssh-keygen -Y
  find-principals`.
- **États de confiance** (`artefacts.verify_trust`) : `valid` (clé connue,
  signature juste), `unknown-key` (signature juste, clé absente des clés de
  confiance : `ssh-keygen -Y check-novalidate` réussit, `find-principals`
  ne trouve rien), `unsigned` (pas de `.sig`), `invalid` (signature fausse,
  ou `.sig` illisible). Plus, à l'extraction : `sums-mismatch` (un membre ne
  correspond pas à `SHA256SUMS`, ou un membre non listé).
- **Décision** (`artefacts.decide`) :

  | État | Mode permissif (défaut) | Mode strict |
  |---|---|---|
  | `valid` | accepté | accepté |
  | `unknown-key` | accepté après acquittement `unknown-key` | refusé |
  | `unsigned` | accepté après acquittement `unsigned` | refusé |
  | `invalid`, `sums-mismatch` | refusé | refusé |

  L'acquittement est un champ explicite de la demande (`acknowledge:
  "unsigned"` ou `"unknown-key"`), égal à l'état constaté, jamais deviné.
  Il est écrit au journal de l'action (« accepted UNSIGNED package
  socle-2026.11 after acknowledgement by <personne> ») et dans
  `<état>/artefacts.d/<nom>-<version>/trust.json` : état, empreinte de clé
  et principal s'il y en a, personne, date, mode.
- **Mode strict** : `<état>/artefacts/settings.json` `{"strict": bool}`,
  écrit par un administrateur depuis Paramètres > Artefacts. Si
  `config.yaml` porte `artefacts: {strict: true}`, le mode strict est
  verrouillé : l'interface le montre grisé, avec la raison (point 184).
- **Rangement des paquets** : `<état>/artefacts.d/<nom>-<version>/` (0700,
  fichiers 0600), contenu extrait et vérifié, plus `trust.json` et la
  `.sig` d'origine. L'archive téléversée est supprimée après extraction.
  Place vérifiée avant réception : deux fois la taille annoncée plus 1 Gio
  (archive puis extraction ; les couches étant déjà compressées, l'extrait
  pèse à peu près l'archive).
- **Images dans l'index** (`images/index.json`) : chaque entrée porte
  `org.opencontainers.image.ref.name` (le tag) et
  `io.containerd.image.name` (la référence complète d'origine), pour que
  `ctr images import` nomme l'image sans `--base-name`. Une seule plateforme
  par défaut, `linux/amd64` (point 185).
- **Rangement dans un registre** (point 186) : une image
  `<hôte>/<chemin>:<tag>` devient `<registre>/<hôte>/<chemin>:<tag>`
  (`docker.io/library/nginx:1.27` devient `<registre>/docker.io/library/nginx:1.27`).
  Les charts vont sous `<registre>/charts/<chart>:<version>`. Un tag déjà
  présent avec une autre empreinte est refusé (`publish-tag-differs`) ;
  même empreinte : rien à faire (idempotent).
- **Registre d'amorçage** : namespace `harvester-ops-registry` sur le
  cluster Harvester. Un `Deployment` à une réplique, stratégie `Recreate`
  (volume RWO), un PVC Longhorn, un service LoadBalancer à l'adresse
  donnée, annoté `kube-vip.io/loadbalancerIPs`. Port 443 sur le service,
  5000 dans le conteneur. Ressources : demandes 50m et 128 Mi, limite
  mémoire 512 Mi (règle de limite mémoire de tout conteneur). Sondes TCP
  toutes les 60 s (un `/v2/` répond 401 avec authentification). Taille par
  défaut 20 Gio (entier de 5 à 2048), classe de stockage au choix.
- **Comptes du registre d'amorçage** : `push` (écriture, gardé seulement
  dans un Secret du cluster Harvester) et `pull` (lecture, recopié dans les
  clusters enfants). Mots de passe de 32 octets aléatoires
  (`secrets.token_urlsafe`), htpasswd bcrypt produit par l'outil en lisant
  le mot de passe sur l'entrée standard. Jamais d'accès anonyme :
  l'authentification htpasswd de Distribution s'applique à toute requête
  (point 183).
- **Identifiants des registres** : jamais dans l'état de la console
  (décision 129). Secrets `kubernetes.io/basic-auth`
  `harvester-ops-registry/<registre>-push` et `<registre>-pull` sur chaque
  cluster Harvester qui emploie le registre ; CA (publique) dans la
  déclaration et dans un Secret `<registre>-ca` (`ca.crt`). Saisie : à la
  déclaration ou au rattachement à un cluster, par le corps de la requête
  ou l'entrée standard, jamais dans argv. Un registre du client sans compte
  de lecture distinct est accepté avec un avertissement
  (`registry-pull-is-push`) : ce compte finira dans un fichier des nœuds.
- **Côté clusters enfants** : Secrets `<cluster>-registry-<registre>-auth`
  (`username`, `password`, compte de lecture) et
  `<cluster>-registry-<registre>-ca` (`Opaque`, `ca.crt` seul : un Secret
  `kubernetes.io/tls` exige `tls.crt` et `tls.key`, vides ils cassent
  containerd, essais préalables) dans le
  namespace du cluster, étiquetés `GENERATED` pour partir avec lui.
- **Délais** : publication, 60 minutes par défaut
  (`phases.registry.publish_timeout_minutes`, entier de 5 à 600) ; registre
  d'amorçage prêt, 10 minutes (`phases.registry.timeout_minutes`, entier de
  1 à 60).
- **Étapes** : `phase-registry` ; sous-étapes `reg-<registre>` puis
  `pub-<paquet>-<registre>` (phase à éléments, `item_key = "id"`). Résultat
  (noms et identifiants seulement) :

  ```json
  {"id": "registry", "status": "done",
   "items": [{"id": "reg-amorcage", "kind": "bootstrap", "created": true,
              "endpoint": "172.16.2.76", "status": "done", "actions": ["..."],
              "undo": {"kind": "registry", "ref": "harvlab2/amorcage"}},
             {"id": "pub-socle-2026.11-amorcage", "images": 14, "charts": 2,
              "skipped_blobs": 31, "status": "done", "actions": ["..."]}]}
  ```

- **Actions** : `artefacts-import:<nom>-<version>`,
  `artefacts-publish:<nom>-<version>@<registre>`,
  `registry-deploy:<cluster>/<registre>`,
  `registry-delete:<cluster>/<registre>`,
  `registry-attach:<cluster>/<registre>`. Filles de `deploy:<cluster>` quand
  la chaîne les lance (champ `parent` de la 2.5.0).
- **Rôles** (point 195) : importer, supprimer un paquet, clés et mode
  strict, poser ou supprimer un registre d'amorçage, déclarer un registre
  du client = administrateur ; publier, rattacher un registre à un cluster
  enfant, choisir des registres dans le formulaire de création = opérateur ;
  lire = lecteur. Écritures sur le cluster Harvester avec l'identité de la
  personne (usurpation portée par le kubeconfig, comme en 2.5.0).
- **Routes** : `@requires_auth` et `@_rate_limit` sur chaque écriture,
  limites validées par `limits.parse_many()` (piège de flask-limiter connu
  du projet). Version d'un paquet passée en paramètre séparé et contrôlée
  par `artefacts.VERSION_RE` (le crochet `before_request` ne connaît que
  RFC 1123).
- **Interface** : `FloatingPanels`, `.sub-tabs-inline`, `.btn`, `.card`,
  `.modal-overlay` ; `escapeHtml` sur tout nom, principal, référence
  d'image ; bulle d'aide sur chaque contrôle ; cinq langues, écart de
  `test_i18n` à zéro ; SSE par `SSEReconnect.connect()`.
- **Contenu public** : ni tiret cadratin ni flèche Unicode ; aucune mention
  d'outillage dans les commits, le CHANGELOG et la doc.

## Constats préalables (faits le 05/11/2026)

### RKE2 et CAPRKE2 : registres privés

- **CAPRKE2 v0.25.2 porte `privateRegistriesConfig`** dans `RKE2ConfigSpec`
  (donc dans `RKE2ConfigTemplate` et `RKE2ControlPlaneTemplate`). Relu dans
  les composants embarqués :
  `~/.local/share/harvester-ops/capi-providers/rke2-v0.25.2/bootstrap-components.yaml`
  l. 436 à 565 (et `control-plane-components.yaml` l. 628, 2225). Champs :
  `mirrors.<registre>.endpoint[]`, `mirrors.<registre>.rewrite{}`,
  `configs.<hôte>.authSecret` (Secret à `username` et `password`),
  `configs.<hôte>.tls.tlsConfigSecret` (Secret `kubernetes.io/tls`, jusqu'à
  `tls.crt`, `tls.key`, `ca.crt`), `configs.<hôte>.tls.insecureSkipVerify`.
  CAPRKE2 en tire `/etc/rancher/rke2/registries.yaml` sur chaque nœud.
- **RKE2** ([Private Registry Configuration](https://docs.rke2.io/install/private_registry)) :
  le fichier doit exister avant le démarrage de RKE2 (sinon redémarrage de
  chaque nœud) ; containerd essaie les miroirs dans l'ordre, **puis le
  point d'accès par défaut** ; `disable-default-registry-endpoint: true`
  supprime ce repli, mais seulement pour les registres qui ont une entrée
  de miroir ; `rewrite` s'applique au nom de l'image vers un miroir, jamais
  vers le point d'accès par défaut ; `"*"` donne des valeurs par défaut.
  Conséquence : le repli vers Internet est coupé par registre miroité, ce
  que la console pose quand le plan ou le formulaire le demande (option
  `no_fallback`, vraie par défaut pour un registre d'amorçage).
- **Ce que CAPRKE2 ne porte pas** : `disable-default-registry-endpoint`
  n'est pas un champ de `serverConfig` ni d'`agentConfig`. Il s'écrit par
  un fichier `files` vers `/etc/rancher/rke2/config.yaml.d/50-harvester-ops-registry.yaml`,
  lu par RKE2 au démarrage. La version de RKE2 qui introduit l'option et la
  lecture de `config.yaml.d` sur les nœuds CAPRKE2 sont à confirmer en
  réel (tâche 1) sur v1.34.11 (version éprouvée du paquet).
- **Le générateur `caphv-generate` n'a aucune option de registre** (relu,
  `bin/caphv-generate`, commit `5373a21`). La configuration s'ajoute par
  `capi_cluster.postprocess`, directement dans les gabarits
  `RKE2ControlPlaneTemplate` et `RKE2ConfigTemplate`, qui sont propres au
  cluster (un cluster enfant par namespace, règle de la 2.5.0).
- **Amorçage complet sans Internet** : CAPRKE2 ne sait installer RKE2 que
  par `install.sh`. En mode `airGapped`, l'image des VMs doit contenir
  `rke2-images.linux-amd64.tar.zst`, `rke2.linux-amd64.tar.gz` et
  `sha256sum-amd64.txt` dans `/opt/rke2-artifacts`
  ([CAPRKE2, Air-gapped installation](https://caprke2.docs.rancher.com/02_topics/01_air-gapped-installation.html)).
  Défaut ouvert : `airGappedChecksum` produit un cloud-init invalide
  ([rancher/cluster-api-provider-rke2#1128](https://github.com/rancher/cluster-api-provider-rke2/issues/1128)).
  Une image de VM préparée est hors du périmètre de la 2.6.0 (point 191).

### CAAPH v0.6.4 et les dépôts OCI

Source à l'étiquette `v0.6.4`,
[`api/v1alpha1/helmchartproxy_types.go`](https://github.com/kubernetes-sigs/cluster-api-addon-provider-helm/blob/v0.6.4/api/v1alpha1/helmchartproxy_types.go) :
`repoURL` accepte `oci://<dépôt>` (le chart est `oci://<dépôt>/<chartName>`) ;
`credentials: {secret: {name, namespace}, key}` désigne un Secret au format
`config.json` de Docker (clé par défaut `config.json`) ; `tlsConfig:
{caSecret: {name, namespace}, insecureSkipTLSVerify}`, le Secret portant
`ca.crt`. Ces Secrets vivent sur le cluster de gestion, donc sur le
cluster Harvester. Le comportement réel avec une CA privée et des
identifiants reste à éprouver (la conception le signalait) : tâche 1, avant
tout code de la tâche 10. Repli si l'essai échoue : dépôt Helm HTTP
(`index.yaml`) servi à côté du registre, décidé à ce moment.

### Le registre : logiciel et image

- **CNCF Distribution v3** : chemin de configuration par défaut
  `/etc/distribution/config.yml` dans l'image amont, pilotes `oss` et
  `swift` retirés, première version stable v3.0.0 après v2.8.3
  ([releases](https://github.com/distribution/distribution/releases),
  [configuration](https://distribution.github.io/distribution/about/configuration/)).
  Le ramasse-miettes exige un registre en lecture seule ou arrêté
  ([garbage collection](https://distribution.github.io/distribution/about/garbage-collection/)) :
  pas de nettoyage en 2.6.0.
- **SUSE publie Distribution 3.1 en image BCI** :
  `registry.suse.com/suse/registry:3.1`, Apache-2.0, configuration
  `/etc/registry/config.yml`, données `/var/lib/docker-registry`, port 5000
  ([page de l'image](https://registry.suse.com/repositories/suse-registry)).
  C'est l'image retenue (point 181) : base BCI maintenue et suivie côté CVE,
  même éditeur que Harvester, logiciel ouvert et remplaçable. L'utilisateur
  de l'image et les droits du volume sont à relever en tâche 1.
- **Harvester n'embarque aucun registre** ; la charge conteneur sur le
  cluster Harvester est déjà employée par la console (gestes Conteneurs
  1.93.0) et par le service de compatibilité de CAPHV.

### Copier des images en bibliothèque standard

- `oci_push.Registry` sait lire un manifeste, un index multi-architecture,
  un blob (empreinte contrôlée), pousser un blob et un manifeste. **Limites
  actuelles** : `blob()` et `push_blob()` gardent le blob entier en mémoire
  (`r.read()`, `data=data`) ; contexte TLS par défaut seulement (pas de CA
  propre au registre) ; pas de lecture d'un manifeste par `HEAD`. Les
  couches d'une image courante font des centaines de mégaoctets : la
  tâche 2 passe en flux.
- Disposition OCI : `oci-layout`, `index.json`, `blobs/<alg>/<empreinte>`
  ([OCI image layout](https://github.com/opencontainers/image-spec/blob/main/image-layout.md)).
  Envoi monolithique `POST` puis `PUT ?digest=`, montage entre dépôts
  `POST ?mount=<empreinte>&from=<dépôt>`, `HEAD` de manifeste
  ([OCI distribution spec](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)).
- Charts Helm en OCI : configuration
  `application/vnd.cncf.helm.config.v1+json`, couche
  `application/vnd.cncf.helm.chart.content.v1.tar+gzip`, tag = version du
  chart ([Helm, Use OCI-based registries](https://helm.sh/docs/topics/registries/)).

### Outils absents de l'image de la console

`container/Containerfile` installe `openssh-clients` (donc `ssh-keygen`),
mais ni `openssl` (certificat du registre d'amorçage) ni `htpasswd`
(bcrypt, seul format de Distribution). La bibliothèque standard de Python
ne fait ni X.509 ni bcrypt. Ajout des deux paquets (point 183) ; la
présence de `apache2-utils` dans le dépôt BCI de `registry.suse.com/bci/python:3.11`
est à vérifier en tâche 1, repli prévu.

### Les bancs (relevé du CHANGELOG 2.5.0)

harvlab2 : Harvester v1.9.0, un nœud imbriqué, VM portée à 32 Gio, disque
Longhorn de **48 Gio**, pile Cluster API laissée installée (RKE2 v0.25.2,
CAPHV v0.10.1, CAAPH v0.6.4), classe d'image à une réplique, pool
172.16.2.200-205 réservé dans NetBox. Un cluster 1+1 du modèle `rke2-banc`
(disques de 8 Gio) s'y crée en 8 min 50 s. harv4 : bare-metal, 128 Gio,
pile laissée installée, éteint.

## Tâche 1 : essais préalables sur le banc (avant tout code)

Fichiers : aucun dans le dépôt ; compte rendu dans `docs/design/` en fin de
ce plan (section « Essais préalables », ajoutée au même commit que la
tâche 2).

Sur harvlab2 (point 180), à la main, chaque essai remis en état :

1. **CAPRKE2 et miroirs** : sur un cluster 1+1 de `rke2-banc`, gabarits
   corrigés à la main avant application (`harvester-capi render` puis
   édition) : `privateRegistriesConfig` vers un registre de test, fichier
   `config.yaml.d` portant `disable-default-registry-endpoint`. Relever
   `/etc/rancher/rke2/registries.yaml`, `hosts.toml` de containerd,
   `containerd.log` sur un nœud ; un tag absent du miroir échoue sans repli.
2. **CAAPH et OCI** : un chart podinfo poussé à la main (`oci_push` en
   console Python) dans un registre à CA privée et htpasswd ; un
   `HelmChartProxy` `oci://` avec `credentials` et `tlsConfig.caSecret` ;
   `HelmReleaseProxy` prêt, release installée dans le cluster enfant.
3. **Registre dans un pod** : image `registry.suse.com/suse/registry:3.1`
   importée par `ctr` depuis une archive OCI écrite par un prototype de
   `oci_layout` (annotations nommant l'image), PVC Longhorn sur la classe à
   une réplique, service annoté kube-vip ; `/v2/` joint depuis node1 et
   depuis une VM du réseau `default/lab-net`. Relever l'utilisateur de
   l'image et les droits nécessaires sur le volume.
4. **Sonde depuis le réseau des VMs** (point 192) : pod bref attaché par
   Multus au réseau de VMs, adresse par DHCP (`udhcpc` de bci-busybox,
   capacité `NET_ADMIN`), `wget` de `/v2/`. Si l'adresse ne s'obtient pas,
   noter la cause et appliquer le repli du point 192.
5. **Outils** : `openssl` et `apache2-utils` installables dans
   `registry.suse.com/bci/python:3.11` (`zypper search`).

Critère de passage : 1, 2 et 3 réussis. Un échec de 2 bascule la tâche 10
sur le repli HTTP (point à ajouter alors, soumis avant de coder).

## Tâche 2 : copie d'images en flux et disposition OCI

Fichiers : `bin/lib/oci_push.py`, nouveau `bin/lib/oci_layout.py`,
`tests/api/test_oci_push_175.py` (cas ajoutés), nouveau
`tests/api/test_oci_layout_260.py`.

`oci_push.Registry` (compatibles avec l'appelant existant,
`push_vddk_image`, dont le test passe sans changement) :
- `__init__(host, creds=None, plain_http=False, timeout=120, ca_pem=None)` :
  `ca_pem` chargé par `ssl.create_default_context(cadata=...)`.
- `request(..., stream_to=None, body_file=None, body_size=None)` : réponse
  écrite par blocs de 1 Mio dans un fichier ouvert, ou corps lu d'un
  fichier avec `Content-Length`.
- `blob_to_file(repo, digest, path, on_bytes=None)` : écrit
  `path + ".part"`, empreinte calculée pendant l'écriture, renommage si
  juste, sinon suppression et `RegistryError`.
- `push_blob_file(repo, digest, path, size, mount_from=None, on_bytes=None)` :
  `False` si déjà présent ; montage tenté si `mount_from` (201 = monté,
  202 = repli sur l'envoi) ; envoi monolithique en flux.
- `manifest_digest(repo, ref) -> str | None` (`HEAD`, `Docker-Content-Digest`).

`oci_layout` (nouveau, bibliothèque standard) :
- `Layout(root)` : `init()`, `blob_path(d)`, `has_blob(d)`,
  `add_blob_file(src, digest)`, `entries()`, `add_entry(desc, ref_name,
  image_name)`, `write_index()` (écriture atomique).
- `copy_image(src, ref, layout, platform="linux/amd64", progress=None) ->
  dict` : index multi-architecture résolu vers la plateforme ; manifeste,
  configuration et couches écrits tels quels (empreintes inchangées : la
  signature amont éventuelle reste valable) ; `{"name", "digest",
  "size", "platform", "layers"}`.
- `push_image(layout, entry, dst, repo, tag, progress=None) -> dict` :
  couches puis configuration puis manifeste ; `manifest_digest` d'abord :
  même empreinte = `{"pushed": False}` ; autre empreinte =
  `RegistryError("publish-tag-differs")`.
- `chart_to_layout(tgz, layout) -> dict` : lit `Chart.yaml` dans le `.tgz`
  (nom, version ; lecture YAML minimale `clé: valeur` de premier niveau, la
  seule nécessaire), écrit configuration et couche aux types Helm.
- `export_ctr_tar(layout, names, out)` : archive OCI que `ctr images
  import` accepte (pour le paquet interne du registre, tâche 4).
- `target_repo(image_ref) -> (repo, tag)` : la règle de rangement des
  contraintes communes (`<hôte>/<chemin>`), `docker.io` implicite rendu
  explicite, `library/` ajouté aux images officielles.

Tests (tableaux, deux registres simulés comme dans
`test_oci_push_175.py`) : blob de 5 Mio en flux (mémoire du processus
mesurée sous un plafond), empreinte fausse en cours de route (fichier
`.part` supprimé), montage accepté et refusé, blob déjà présent, manifeste
déjà présent même empreinte et autre empreinte, index multi-architecture
sans `linux/amd64` (erreur nommée), CA propre au registre (serveur TLS de
test à CA générée par le test), redirection d'un blob vers un autre hôte
sans `Authorization`, chart dont le `Chart.yaml` manque, `target_repo` sur
`nginx`, `nginx:1.27`, `docker.io/nginx`, `ghcr.io/a/b@sha256:...`,
`localhost:5000/x`. Mutations : `M-buffer` (`blob_to_file` qui lit tout
puis écrit : le test de plafond doit échouer), `M-digest-late` (empreinte
contrôlée après le renommage), `M-auth-redirect` (en-tête gardé sur la
redirection), `M-tag-overwrite` (tag d'une autre empreinte écrasé).

Vérification réelle : copie de `ghcr.io/stefanprodan/podinfo` et de son
chart vers une disposition sur node1, puis poussée vers le registre de la
tâche 1 ; `crictl pull` depuis un nœud du cluster enfant.

## Tâche 3 : format du paquet et confiance (`artefacts`)

Fichiers : nouveau `bin/lib/artefacts.py`, `bin/lib/self_update.py`
(`verify_signature` paramétrée), nouveau `tests/api/test_artefacts_260.py`,
`tests/api/test_self_update*.py` (inchangés, doivent passer).

- `self_update.verify_signature(archive, signature, signers, runner=...,
  in_image=None, namespace=SIG_NAMESPACE, identity=SIG_IDENTITY)` :
  défauts inchangés pour la mise à jour.
- `artefacts` :
  - `NAME_RE`, `VERSION_RE`, `ARCHIVE_RE`, `SIG_NAMESPACE =
    "harvester-artefacts"`, `KINDS = ("chart", "manifest", "images")`,
    `LATER_KINDS = ("policy", "gitops")` (lus et gardés, montrés « à venir »,
    jamais appliqués) ;
  - `check_index(doc) -> (index, errors)` : `name`, `version`,
    `description`, `items[]` (`name` RFC 1123 unique, `kind`, `version`,
    `chart` pour un chart, `files` pour un manifeste, `images[]` de
    références présentes dans `images/index.json`, `values` pour un chart),
    clés inconnues refusées ;
  - `inspect_package(path) -> dict` : règles de `self_update.inspect_archive`
    (membre absolu ou en `..`, lien hors de l'arborescence, fichier spécial,
    une seule racine) plus `SHA256SUMS` en premier membre, racine égale à
    `<nom>-<version>` de l'index et du nom de fichier ;
  - `verify_trust(path, sig, signers, runner=subprocess.run) -> dict` :
    `find-principals`, puis `verify` avec le principal, sinon
    `check-novalidate` ; `{"state", "principal", "key_fingerprint"}`,
    jamais la sortie brute de `ssh-keygen` ;
  - `decide(trust, strict, acknowledge) -> (accepted, code)` : le tableau
    des contraintes communes ;
  - `extract_verified(path, dest, on_bytes=None)` : lecture en flux
    (`tarfile` mode `r|gz`), `SHA256SUMS` lu d'abord, chaque fichier haché
    pendant l'écriture, membre absent de la liste ou empreinte fausse =
    `sums-mismatch` et répertoire partiel supprimé ; mêmes protections
    d'extraction que `self_update.extract_archive` ;
  - `settings(state_dir, config) -> {"strict", "locked", "signers"}`,
    `save_settings(state_dir, strict)`, `save_signers(state_dir, text)`
    (chaque ligne vérifiée : principal, `namespaces="harvester-artefacts"`
    ajouté s'il manque, type de clé `ssh-ed25519`, `ecdsa-sha2-*` ou
    `ssh-rsa` d'au moins 3072 bits) ;
  - `list_packages(state_dir)`, `load_package(state_dir, name, version)`,
    `remove_package(state_dir, name, version)` (refusé si un plan
    enregistré le désigne : `package-in-use`).

Tests (tableaux) : quatre états de confiance avec de vraies clés générées
par `ssh-keygen` dans le test (clé connue, inconnue, signature d'un autre
fichier, `.sig` vide) ; signature faite avec l'espace de noms de la console
refusée ; douze cases de `decide` (trois modes d'acquittement, deux modes,
états) ; archive malveillante (membre `../x`, lien absolu, périphérique,
deux racines, racine qui ne correspond pas, `SHA256SUMS` pas en premier,
membre non listé, empreinte fausse au dernier membre : répertoire supprimé) ;
index (clé inconnue, élément en double, image référencée absente, `policy`
gardé et marqué) ; clés de confiance (ligne RSA 2048 refusée, espace de noms
ajouté). Mutations : `M-ack-any` (un acquittement `unsigned` accepte une clé
inconnue), `M-invalid-permissive` (signature fausse acceptée en permissif),
`M-sums-skip` (membre non listé accepté), `M-namespace` (espace de noms de
la console accepté).

Vérification réelle : tâche 13 (quatre imports, un par état).

## Tâche 4 : `harvester-artefacts` (construction, signature, import, publication)

Fichiers : nouveau `bin/harvester-artefacts.py`, nouveau
`scripts/registry-bundle.json`, `package.sh`, `install.sh` (copie de
`bundles/registry`), `bin/harvester-capi.py` (`push_images` par
`node_images`), nouveau `bin/lib/node_images.py`, nouveaux
`tests/api/test_harvester_artefacts_cli_260.py` et
`tests/api/test_node_images_260.py`, `tests/api/test_capi_cli.py` (inchangé,
doit passer).

Sous-commandes (analyse `allow_abbrev=False` comme les autres outils) :

- `pack <description> [--out DIR] [--sign-key FICHIER] [--creds FICHIER]
  [--platform linux/amd64]` : bibliothèque standard seulement. Description
  en JSON ; en YAML si PyYAML est présent (sinon message qui le dit,
  point 185). Forme :

  ```json
  {"name": "socle", "version": "2026.11",
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

  Charts lus dans un dépôt HTTP (`index.yaml` puis `.tgz`, empreinte de
  l'index contrôlée) ou OCI (`oci_layout.chart_to_layout`). Images par
  `oci_layout.copy_image`, couches partagées une seule fois. `as` donne un
  autre nom à une image (sert à la preuve sans Internet). Identifiants des
  registres sources par un fichier 0600 (`--creds`, JSON `{hôte: {username,
  password}}`), jamais en argument. `SHA256SUMS` écrit et placé en premier.
  `--sign-key` appelle `sign` à la fin.
- `sign <paquet> --key FICHIER` : `ssh-keygen -Y sign -n
  harvester-artefacts`. `verify <paquet> [--signers FICHIER]` : état de
  confiance, code 0 seulement pour `valid`. `inspect <paquet>` : index,
  tailles, état de confiance, sans extraire.
- `import <paquet> [--accept-unsigned | --accept-unknown-key]` (dans la
  console) : `inspect`, confiance, décision, extraction vérifiée,
  `trust.json` ; codes 0 accepté, 2 refusé, 3 acquittement requis.
- `list`, `remove <nom> <version>`.
- `publish <nom> <version> --cluster C --registry R [--items a,b]
  [--dry-run]` : identifiants d'écriture lus dans le Secret
  `<R>-push` du cluster Harvester avec le kubeconfig donné, jamais
  affichés ; images puis charts, progression par étapes
  (`images 3/14`), blobs déjà présents comptés ; `--dry-run` liste ce qui
  serait poussé.

Paquet interne du registre d'amorçage : `scripts/registry-bundle.json`
décrit `registry.suse.com/suse/registry:3.1` et
`registry.suse.com/bci/bci-busybox:16.0` (sonde), épinglés par empreinte.
`package.sh` lance `harvester-artefacts pack` puis `export_ctr_tar` vers
`bundles/registry/registry-images.tar` et sa somme, comme le paquet Cluster
API (règle des releases prêtes à l'emploi). `node_images.import_on_nodes(entry,
files, step)` reprend le code de `push_images`, qui l'appelle désormais
(zéro redondance).

Tests : `pack` avec deux registres simulés et un dépôt HTTP simulé (chart),
lancé par `python3 -I -S` (preuve de la bibliothèque standard) ; couche
partagée écrite une fois ; `as` ; identifiant absent du stdout, du stderr et
du paquet ; description à clé inconnue ; chart introuvable ; `import` dans
les quatre états et deux modes (codes) ; `publish --dry-run` ; `publish`
contre un registre simulé, deux fois (seconde sans envoi) ;
`node_images` simulé (même commande distante qu'avant pour `push_images`).
Mutations : `M-pyyaml` (import de `yaml` en tête de module : le test `-S`
doit échouer), `M-creds-argv` (identifiants passés en argument),
`M-sums-order` (`SHA256SUMS` écrit en dernier : l'extraction en flux doit
refuser).

Vérification réelle : `pack` sur node1 du paquet `banc-260` de la tâche 13,
`inspect`, `verify` ; `package.sh` produit `bundles/registry` et l'archive
s'importe par `ctr` sur harvlab2.

## Tâche 5 : paquets dans la console

Fichiers : `web/app.py` (routes, `_receive_archive` réemployé),
nouveau `web/static/js/artefacts.js`, `web/static/js/settings.js`,
`web/templates/index.html`, `web/static/js/i18n.js`, nouveaux
`tests/api/test_artefacts_routes_260.py` et
`tests/e2e/test_artefacts_260.py`.

Routes :
- `GET /api/artefacts` (lecteur) : paquets, index résumé, confiance.
- `GET /api/artefacts/<nom>/<version>` (lecteur) : index complet, images
  (références, tailles), `trust.json` sans rien de secret.
- `POST /api/artefacts/upload?name=` (administrateur, `5/minute`) : archive
  ou `.sig`, en flux vers `<état>/artefacts/incoming/`, place vérifiée (507),
  action `artefacts-import:<nom>-<version>` créée dès la réception ;
  pré-vérification (inspection et confiance) rendue avec `acknowledge_needed`
  (`unsigned`, `unknown-key` ou rien).
- `POST /api/artefacts/<nom>/<version>/accept` (administrateur, `10/minute`)
  : `{"acknowledge": "unsigned" | "unknown-key" | null}` ; décision, puis
  extraction dans le fil de l'action, étapes `check`, `extract`, `trust`.
  409 `acknowledge-required` avec l'état constaté, 409 `strict-mode` en
  mode strict, 422 `invalid-signature` ou `sums-mismatch`.
- `DELETE /api/artefacts/<nom>/<version>` (administrateur), 409
  `package-in-use`.
- `GET/PUT /api/artefacts/settings` (lecture : lecteur ; écriture :
  administrateur, `10/minute`) : `strict`, `locked`, clés de confiance
  (empreinte et principal affichés, ligne entière en écriture seulement).

Interface :
- **Où** : point 194. Onglet **Artefacts** (liste des paquets, détail en
  panneau à droite : éléments, images, confiance, plans qui le désignent) et
  bouton **Importer** (fenêtre overlay, téléversement de l'archive puis de la
  `.sig`, progression dans le dock).
- **Avertissement** : un paquet `unsigned` ou `unknown-key` ouvre une fenêtre
  qui dit ce que cela veut dire, en une phrase par idée (« Ce paquet n'est
  pas signé. Son origine ne peut pas être vérifiée. »), avec l'empreinte de
  clé si elle existe et une case à cocher « J'accepte ce paquet en
  connaissance de cause ». Le bouton Accepter reste grisé tant que la case
  n'est pas cochée. En mode strict, la fenêtre dit le refus et le réglage
  qui l'impose.
- **Paramètres > Artefacts** (administrateur) : interrupteur Mode strict
  (grisé avec la raison si `config.yaml` le verrouille), clés de confiance
  (liste, ajout par collage d'une ligne `allowed_signers`, retrait), bulle
  d'aide sur chaque contrôle.
- Badge de confiance sur chaque paquet : Signé (principal), Clé inconnue,
  Non signé ; un paquet accepté par acquittement garde son badge.

Tests des routes : rôles (lecteur refusé à l'import, opérateur refusé),
limites valides (`limits.parse_many`), nom d'archive invalide, version
invalide, place insuffisante (507), quatre états, acquittement absent,
acquittement faux (`unsigned` pour une clé inconnue), mode strict, mode
verrouillé par `config.yaml` (PUT refusé 409 `strict-locked`), journal de
l'action portant la personne et l'état, aucun chemin absolu dans les
réponses. e2e sur routes simulées : import non signé avec case à cocher,
Accepter grisé puis actif, mode strict, badge, échappement d'un nom de
paquet et d'un principal hostiles. Mutations : `M-ack-ui` (Accepter actif
sans la case), `M-journal` (acquittement non écrit au journal : le test
d'Activité doit échouer), `M-escape` (`escapeHtml` retiré du principal).

Vérification réelle : tâche 13.

## Tâche 6 : registres déclarés (`registries`)

Fichiers : nouveau `bin/lib/registries.py`, nouveau
`bin/harvester-registry.py`, `web/app.py` (routes), nouveaux
`tests/api/test_registries_260.py` et `tests/api/test_registry_routes_260.py`.

- Déclaration (console, sans secret) : `<état>/registries.d/<nom>.yaml`,
  0600, révision, 409 sur révision périmée, comme les modèles :

  ```yaml
  name: harbor-client
  kind: external            # ou bootstrap
  software: distribution    # bootstrap seulement ; harbor plus tard
  endpoint: harbor.client.example:443
  ca_pem: |                 # publique ; absente = CA du système
    -----BEGIN CERTIFICATE-----
  usage: [images, charts]
  mirrors: [docker.io, registry.suse.com, ghcr.io]
  clusters: [harv4]         # clusters Harvester où ses Secrets sont posés
  revision: 2
  ```

  Un registre d'amorçage porte en plus `cluster`, `address`, `size_gib`,
  `storage_class` ; il est créé par le geste de la tâche 7, jamais à la
  main.
- **Plusieurs registres** (précision de 119) : un cluster enfant en
  désigne une liste ordonnée ; pour chaque registre d'origine miroité
  (`docker.io`...), les registres qui le servent sont essayés dans l'ordre de
  la liste (le premier est préféré, les suivants sont ses miroirs de
  secours). Un registre sert les images, les charts, ou les deux.
- `rke2_config(entries, decls, cluster) -> (private_registries_config,
  files, secrets)` (pur) : miroirs avec `rewrite` `"^(.*)$":
  "<hôte d'origine>/$1"` (rangement du point 186), `configs` par hôte de
  registre avec `authSecret` et `tlsConfigSecret`, fichier `config.yaml.d`
  si `no_fallback`, Secrets du compte de lecture et de la CA (contraintes
  communes). Jamais le compte d'écriture.
- `attach(kube, decl, creds)` : Secrets `<registre>-push`, `-pull`, `-ca`
  dans `harvester-ops-registry` du cluster Harvester, étiquetés
  `harvester-ops.io/registry=<nom>` ; namespace créé s'il manque, étiqueté
  `harvester-ops.io/managed`.
- Constats (codes, niveau) : `registry-unknown` (bloquant),
  `registry-not-attached` (bloquant : pas de Secrets sur ce cluster),
  `registry-unreachable` (bloquant, depuis la console), `registry-auth-refused`
  (bloquant, compte de lecture puis d'écriture), `registry-ca-invalid`
  (bloquant), `registry-pull-is-push` (avertissement),
  `registry-no-usage` (bloquant : ni images ni charts),
  `registry-mirror-duplicate` (avertissement : même registre deux fois),
  `registry-insecure` (avertissement : `insecureSkipVerify` demandé).
- CLI `harvester-registry declare <nom> --file r.yaml [--revision N]`,
  `attach <nom> --cluster C` (identifiants sur l'entrée standard, JSON),
  `check <nom> --cluster C [--json]`, `list`, `show <nom>`,
  `forget <nom>` (refusé si un plan ou un cluster enfant le désigne).
- Routes : `GET /api/registries`, `GET/PUT/DELETE /api/registries/<nom>`,
  `POST /api/registries/<nom>/attach/<cluster>` (identifiants dans le corps,
  jamais renvoyés), `POST /api/registries/<nom>/check/<cluster>`.

Tests (tableaux) : `rke2_config` avec un registre, deux registres pour le
même `docker.io` (ordre), un registre pour images seulement (absent des
miroirs de charts), `no_fallback` vrai et faux, registre sans CA ; aucun
mot de passe dans le résultat hors des Secrets, aucun compte d'écriture ;
déclaration à clé inconnue, `endpoint` avec schéma ou chemin (refusé),
CA illisible ; `attach` simulé (identifiants absents de argv, des étapes et
de la réponse) ; chaque constat ; `forget` d'un registre désigné. Mutations :
`M-push-to-nodes` (compte d'écriture rendu dans les Secrets du cluster
enfant), `M-order` (ordre des miroirs inversé), `M-rewrite` (rangement sans
préfixe d'hôte : deux images de même chemin sur deux hôtes se mélangent).

Vérification réelle : registre du client simulé sur node1 (tâche 13),
déclaré, rattaché à harvlab2, `check` vert ; compte de lecture faux,
`registry-auth-refused`.

## Tâche 7 : registre d'amorçage (`registry_seed`)

Fichiers : nouveau `bin/lib/registry_seed.py`, `bin/harvester-registry.py`
(`deploy`, `status`, `delete`), `container/Containerfile`, `web/app.py`
(routes), nouveaux `tests/api/test_registry_seed_260.py` et
`tests/api/test_registry_seed_cli_260.py`.

- `registry_seed.manifests(spec, tls, htpasswd) -> [dict]` (pur) : Secret
  `<nom>-tls` (`tls.crt`, `tls.key`, `ca.crt`), Secret `<nom>-htpasswd`,
  ConfigMap `<nom>-config` (`config.yml` : `storage.filesystem`
  `/var/lib/docker-registry`, `storage.delete.enabled: false`,
  `auth.htpasswd`, `http.tls`, `http.addr :5000`), PVC `<nom>-data`,
  Deployment `<nom>` (image du paquet interne, `imagePullPolicy: Never` :
  l'image vient de `ctr import`), Service `<nom>` LoadBalancer annoté
  `kube-vip.io/loadbalancerIPs: <adresse>` (même annotation que
  `capi_stack.shim_manifest`). Tous étiquetés
  `harvester-ops.io/registry=<nom>`.
- `registry_seed.check(spec, facts) -> [finding]` : `registry-address-invalid`,
  `registry-address-in-pool` (adresse dans un pool Harvester),
  `registry-address-answers` (l'adresse répond déjà), `registry-address-vip`
  (VIP du cluster), `registry-room-short` (`longhorn_room.fits` avec les
  répliques de la classe), `registry-replicas-nodes` (répliques demandées
  supérieures au nombre de nœuds), `registry-images-missing` (paquet interne
  absent de l'installation), `registry-tools-missing` (`openssl` ou
  `htpasswd` absents), `registry-name-taken`.
- `harvester-registry deploy <nom> --cluster C --address IP --size-gib N
  --storage-class S [--dns-name N] [--cert FICHIER --key FICHIER]` :
  constats ; certificat généré par `openssl` (CA de 10 ans et certificat
  serveur de 2 ans, SAN IP et nom DNS facultatif, clés dans des fichiers
  0600 de `HARVESTER_OPS_WORK_DIR` effacés dans un `finally`), ou
  certificat du client ; htpasswd par `htpasswd -niB` (mot de passe sur
  l'entrée standard) ; images par `node_images.import_on_nodes` sur chaque
  nœud ; application avec l'identité de la personne ; attente de la
  disponibilité (`/v2/` répond 401 sur l'adresse, puis 200 avec le compte
  de lecture) ; déclaration écrite (`kind: bootstrap`, CA publique) et
  Secrets `-push`, `-pull`, `-ca` posés (même forme que `attach`).
- `status <nom> --cluster C [--json]` : Deployment, PVC (taille, usage
  relevé par `/v2/_catalog` et les volumes Longhorn), adresse, version de
  l'image, comptes présents.
- `delete <nom> --cluster C [--delete-data]` : refusé si un cluster enfant
  ou un plan le désigne (`registry-in-use`, avec les noms) ; sans
  `--delete-data`, le PVC est gardé (point 193) ; déclaration retirée.
- Routes : `POST /api/registries/bootstrap/<cluster>` (administrateur,
  `5/minute`, action `registry-deploy:<cluster>/<nom>`),
  `GET /api/registries/bootstrap/<cluster>/<nom>`,
  `DELETE /api/registries/bootstrap/<cluster>/<nom>` (confirmation tapée du
  nom quand `delete_data`).
- `Containerfile` : `openssl` et `apache2-utils` ajoutés (repli du
  point 183 si le second manque au dépôt BCI).

Tests (tableaux) : manifestes (étiquettes, annotation kube-vip, limite
mémoire présente, `imagePullPolicy: Never`, aucun mot de passe en clair hors
du Secret htpasswd, qui ne porte que des empreintes bcrypt) ; configuration
de Distribution relue et comparée champ par champ ; chaque constat ; deploy
simulé : clés et mots de passe absents de argv (commandes enregistrées par
le faux `runner`), des étapes et du stdout, fichiers de travail effacés
après succès et après échec ; delete gardant le PVC ; delete refusé quand
utilisé. Mutations : `M-no-limit` (limite mémoire retirée),
`M-argv-secret` (mot de passe passé à `htpasswd -b`), `M-pvc-delete`
(PVC supprimé sans `--delete-data`), `M-anon` (`auth` retiré de la
configuration).

Vérification réelle : tâche 13 (déploiement sur harvlab2, redémarrage du
pod, données gardées).

## Tâche 8 : publication d'un paquet dans un registre

Fichiers : `bin/harvester-artefacts.py` (`publish`), `web/app.py` (route,
lancement hors requête `_artefacts_publish_launch`), nouveau
`tests/api/test_artefacts_publish_260.py`.

- `POST /api/artefacts/<nom>/<version>/publish` (opérateur, `10/minute`) :
  `{"cluster", "registry", "items": [...] | null}` ; constats d'abord
  (`registry-not-attached`, `registry-unreachable`, `registry-no-usage` pour
  une image vers un registre de charts seuls, `publish-tag-differs` relevé
  par `manifest_digest` sans rien écrire, `package-untrusted-strict` si le
  mode strict a été activé depuis l'import d'un paquet accepté par
  acquittement) ; puis action `artefacts-publish:...`, fille de la chaîne
  quand elle la lance.
- `_artefacts_publish_launch(cluster, kc, package, registry, items,
  parent=None, cluster_user=None)` : ne lit ni `request` ni la session
  (découpe de `_capi_launch` en 2.5.0).
- Résultat : `{"images", "charts", "pushed_blobs", "skipped_blobs",
  "bytes"}` ; la déclaration du registre garde la liste des paquets publiés
  (`published: [{package, version, at}]`, noms seulement).

Tests : publication complète, reprise après coupure au milieu (blobs déjà
envoyés sautés), élément choisi seul, tag d'une autre empreinte (rien
écrit), paquet `policy` (élément marqué « à venir », sauté), aucun
identifiant dans le résultat ni `actions.db`. Mutation : `M-request-ctx`
(lancement qui relit la session : test sans contexte de requête).

Vérification réelle : tâche 13.

## Tâche 9 : clusters enfants branchés sur leurs registres

Fichiers : `bin/lib/capi_cluster.py` (`KNOWN`, `normalize`, `validate`,
`postprocess`, constats), `bin/harvester-capi.py` (`cmd_create`, `_check`),
`bin/lib/deploy_plans.py` (`check_capi_template`, `render_capi_template`),
`web/static/js/capi-create.js`, `web/static/js/i18n.js`,
`tests/api/test_capi_cluster_logic.py`, `tests/api/test_capi_generator.py`,
nouveau `tests/api/test_capi_registries_260.py`,
`tests/e2e/test_capi_create_view.py` (cas ajoutés).

- Option `registries` de la demande : liste ordonnée de `{"registry": nom,
  "mirrors": [...] | null, "no_fallback": bool | null}` (`mirrors` absent =
  ceux de la déclaration ; `no_fallback` absent = vrai pour un registre
  d'amorçage, faux pour un registre du client). Variables admises dans les
  modèles (`{{registre}}`).
- `postprocess(docs, spec, origin=None, registries=None)` : `registries`
  rendu par `registries.rke2_config`, passé par l'appelant (jamais lu dans
  la demande brute) ; `privateRegistriesConfig` et `files` posés dans
  `RKE2ControlPlaneTemplate.spec.template.spec` et
  `RKE2ConfigTemplate.spec.template.spec` ; Secrets ajoutés aux documents,
  étiquetés `GENERATED`.
- `harvester-capi create` lit les Secrets `-pull` et `-ca` du registre sur le
  cluster Harvester (identité de la personne), les recopie sous les noms du
  cluster enfant ; jamais dans argv, une étape ou le rendu affiché
  (`render` les montre masqués).
- Constats de `check` : `cluster-registry-unknown`,
  `cluster-registry-not-attached`, `cluster-registry-later` (information :
  registre créé par la phase Registre du même plan, rejoué au début de la
  phase Clusters), `cluster-registry-no-images` (registre sans usage
  images).
- Clusters existants : rien ne change (point 190).
- Formulaire de création : section **Registres** repliée, liste ordonnée
  (registres déclarés et rattachés au cluster Harvester, boutons
  monter/descendre existants), miroirs cochés depuis la déclaration, case
  « Pas de repli vers Internet » avec bulle d'aide. Mode modèle : « Variable... »
  sur le nom du registre.

Tests (tableaux) : rendu réel par le générateur embarqué
(`test_capi_generator.py`) avec un et deux registres : champs posés dans les
deux gabarits, `rewrite`, `files` présent seulement avec `no_fallback`,
Secrets étiquetés `GENERATED` ; suppression d'un cluster : Secrets du
registre partis avec lui (sélecteur existant) ; constats ; modèle à
variable de registre ; mot de passe masqué dans `render`. Mutations :
`M-cp-only` (configuration posée dans le plan de contrôle seulement : un
worker tirerait d'Internet), `M-files-always` (repli coupé pour un registre
du client sans le demander), `M-not-generated` (Secrets sans étiquette :
le test de suppression doit échouer).

Vérification réelle : tâche 13 (fichier `registries.yaml` relu sur le plan
de contrôle et sur le worker).

## Tâche 10 : charts tirés du registre par CAAPH

Fichiers : `bin/lib/capi_services.py` (`normalize`, `check`, `manifest`),
`bin/harvester-capi.py` (`cmd_service_deploy`, `cmd_service_remove`),
`web/static/js/capi.js`, `web/static/js/i18n.js`,
`tests/api/test_capi_services*.py` (cas ajoutés), nouveau
`tests/e2e/test_capi_service_registry_260.py`.

Seulement si la tâche 1 (essai 2) a réussi.

- Demande de service : `source: {"registry": nom, "package": nom,
  "version": v, "item": chart}` à la place de `repo` et `chart` ; la console
  en déduit `repoURL = oci://<endpoint>/charts`, `chartName`, `version`.
- `manifest(spec, registry=None)` : `credentials: {secret: {name:
  "<cluster>-chart-<registre>", namespace: <ns>}, key: "config.json"}` et
  `tlsConfig: {caSecret: {name: "<cluster>-registry-<registre>-ca",
  namespace: <ns>}}` ; Secret `config.json` (compte de lecture) posé dans
  le namespace du cluster, étiqueté `GENERATED` et
  `harvester-ops.io/svc-<nom>`, supprimé avec le service.
- Valeurs du chart : celles de l'élément du paquet, puis celles du
  formulaire. Les images du chart sont tirées par le cluster enfant à leur
  nom d'origine, servies par ses miroirs (tâche 9) : rien à réécrire dans
  les valeurs.
- Fenêtre Services : choix « Depuis un registre » à côté du catalogue ;
  liste des charts publiés dans les registres rattachés ; constat
  `service-registry-not-mirrored` (avertissement) si le cluster enfant n'a
  pas ce registre pour ses images.

Tests : manifeste avec et sans registre (catalogue inchangé), Secret
`config.json` au bon format (`auths.<endpoint>.auth` en base64), supprimé
avec le service ; constat. e2e : choix d'un chart publié, échappement d'un
nom de chart hostile. Mutations : `M-push-creds` (compte d'écriture dans le
Secret de CAAPH), `M-ca-missing` (`tlsConfig` omis).

Vérification réelle : tâche 13 (podinfo depuis le registre d'amorçage,
Internet coupé pour le cluster enfant).

## Tâche 11 : phase Registre des plans

Fichiers : `bin/lib/deploy_plans.py`, `bin/lib/deploy_chain.py`,
`web/app.py` (`_deploy_phases`, nouveau `_deploy_registry_phase`,
`_deploy_prepare` et ses constats), `bin/harvester-plans.py`,
`tests/api/test_deploy_chain_240.py`, `tests/api/test_deploy_plans_250.py`,
nouveaux `tests/api/test_deploy_plans_260.py` et
`tests/api/test_deploy_registry_260.py`.

- `deploy_chain.ORDER = ("install", "readiness", "terraform", "registry",
  "capi", "clusters", "artefacts")` (point 189), `COMING = ("artefacts",)`,
  `COMING_MESSAGE = "coming in 2.7"`. La reprise d'une action de 2.5.0 n'est
  pas touchée (entrées repérées par `id`, pas par position).
- `deploy_plans.AVAILABLE` gagne `registry`. Format :

  ```yaml
  phases:
    registry:
      enabled: true
      timeout_minutes: 10
      publish_timeout_minutes: 60
      registries:
        - name: amorcage
          mode: bootstrap
          address: "{{registre_ip}}"
          size_gib: 20
          storage_class: harv-rep1
        - name: harbor-client
          mode: external        # déclaré et rattaché d'avance
      publish:
        - {package: socle, version: "2026.11", registry: amorcage}
  ```

  Règles : `mode: external` exige une déclaration existante (constat
  `registry-unknown`) ; un registre `bootstrap` du plan ne porte pas
  d'identifiant (générés) ; `publish` désigne un paquet importé
  (`artefact-package-missing`) et un registre du plan ou déclaré.
  Plan d'une version antérieure, phase `registry` désactivée à clés libres :
  acceptée, clés ignorées, avertissement (précédent de la 2.5.0).
- Phase à éléments : `reg-<nom>` (bootstrap : `harvester-registry deploy`,
  ou relecture si présent avec la même origine ; external : `check`), puis
  `pub-<paquet>-<registre>` dépendant de son registre. `before` rejoue les
  constats (adresse libre, place Longhorn, paquet présent, confiance en mode
  strict). `verify_item` : registre prêt et à la même adresse ; paquet
  publié (manifeste de chaque image présent à la bonne empreinte, un `HEAD`
  par image).
- Origine posée sur le registre d'amorçage (étiquettes `harvester-ops.io/plan`,
  annotation `harvester-ops.io/plan-run`) : un registre de même nom sans
  cette origine = `registry-name-taken` ; la chaîne ne supprime jamais un
  registre.
- `undo: {"kind": "registry", "ref": "<cluster>/<nom>"}` seulement si
  `created` (lu par « Défaire le déploiement » en 2.7.0).
- Phase Clusters : un cluster désigne un registre du plan par son nom ;
  `cluster-registry-later` tant que la phase Registre n'a pas tourné.
- Résumé de `check` : registres (nom, sorte, adresse, taille), paquets à
  publier (taille), durée estimée (mesurée en tâche 13).

Tests du déroulé (outil simulé, comme `test_deploy_runner_240.py`) : chaîne
Terraform, Registre, capi, Clusters ; registre déjà présent (repris) ;
registre de même nom sans origine (bloqué) ; publication en échec (cluster
non lancé, phase `partial`) ; reprise : publication refaite sans envoi ;
arrêt entre deux éléments ; essai sans écriture ; `parent` posé ; `undo`
seulement si `created` ; aucun identifiant dans le résultat. Mutations :
`M-order` (registry laissé après clusters : le cas « cluster qui tire du
registre du plan » doit échouer), `M-coming` (`registry` laissé dans
`COMING`), `M-adopt` (registre d'une autre origine repris).

Vérification réelle : tâche 13.

## Tâche 12 : interface

Fichiers : `web/static/js/artefacts.js`, nouveau `web/static/js/registries.js`,
`web/static/js/bm-plans.js`, `web/static/js/settings.js`,
`web/templates/index.html`, `web/static/js/i18n.js`,
`tests/api/test_bm_plans_ui_240.py`, `tests/api/test_capi_views_i18n.py`,
`tests/e2e/test_sidebar*.py` et tests de navigation qui listent les
onglets (`tests/api/test_nav_inventory_200.py`), nouveaux
`tests/e2e/test_registries_260.py` et `tests/e2e/test_bm_plans_registry_260.py`.

- **Registres** (point 194) : onglet de Cluster API, à côté de Clusters et
  Services. Liste des registres déclarés, rattachés au cluster Harvester
  choisi ou non ; détail : adresse, usage, miroirs, paquets publiés, clusters
  enfants qui l'emploient, état (prêt, joignable, comptes). Boutons
  **Registre d'amorçage** (fenêtre overlay à trois onglets Paramètres,
  Vérifications, Résumé, comme la fenêtre Déployer), **Déclarer un registre
  du client**, **Rattacher**, **Vérifier**, **Supprimer** (confirmation tapée
  pour les données).
- **Artefacts** : onglet de Cluster API (tâche 5), bouton **Publier** sur un
  paquet (registre, éléments, constats avant).
- **Onglet Plans** : carte Registre active (badge « à venir » retiré ;
  Artefacts garde « à venir (2.7) ») ; liste des registres du plan, liste
  des publications (paquet et registre choisis dans des listes) ; carte
  Clusters : registres par entrée.
- **Fenêtre Déployer** : constats Registre groupés, Résumé avec les
  registres et les paquets.
- Bulles d'aide sur chaque contrôle, cinq langues.

Tests : i18n ; inventaire de navigation mis à jour (les tests refusent un
onglet non placé : voulu) ; e2e sur routes simulées : registre d'amorçage
avec constat bloquant (Lancer grisé avec la raison), déclaration d'un
registre du client (identifiants jamais réaffichés), plan à registre et
publication, échappement d'un nom de registre et d'une référence d'image
hostiles. Mutations : `M-launch` (Lancer actif malgré un constat bloquant),
`M-creds-echo` (mot de passe réaffiché dans le formulaire après
enregistrement).

Vérification réelle : parcours complet sur la console de dev
(`systemctl --user restart harvester-ops-dev` après changement du modèle de
page) avec harvlab2 : import, registre d'amorçage, publication, cluster
enfant, service depuis le registre.

## Tâche 13 : essais réels, documentation, version

Fichiers : `VERSION` (2.6.0), `CHANGELOG.md`, `docs/en/bare-metal.md` et
`docs/fr/bare-metal.md` (phase Registre), nouveaux `docs/en/artefacts.md` et
`docs/fr/artefacts.md` (paquet, `harvester-artefacts pack`, confiance,
registres, publication, clusters enfants, charts), `docs/en/operating-procedure.md`
et `docs/fr/procedure-operationnelle.md`, `docs/en/troubleshooting.md` et
`docs/fr/depannage.md` (miroir non pris : `containerd.log` ; tag d'une autre
empreinte ; registre plein), `docs/en/install.md` et
`docs/fr/installation.md` (bundle du registre, outils de l'image),
`tools/parity/data.py` puis `python3 tools/parity/gen.py --docs`, mémoire
du projet.

### Bancs

| Banc | État | Ce qu'il permet | Ce qu'il ne permet pas |
|---|---|---|---|
| harvlab2 | v1.9.0, 1 nœud imbriqué, 32 Gio, disque Longhorn de 48 Gio, pile Cluster API installée, éteint | **la preuve centrale** : registre d'amorçage de 8 Gio sur la classe à une réplique, un cluster 1+1 (disques de 8 Gio) branché dessus, Internet coupé pour ses VMs, image et chart tirés du registre ; registre du client simulé en miroir | paquets de plusieurs gigaoctets (place) ; deux clusters enfants à la fois (mémoire) |
| harv4 | v1.9.0, bare-metal, 128 Gio, SSD de 1 To en pools, pile installée, éteint | paquet de plusieurs gigaoctets (images système de RKE2), registre de 40 Gio, durée d'import et de publication à l'échelle, deux clusters enfants à registres différents | rien de nécessaire à la preuve centrale |
| harv1 | v1.9.0, porte Rancher Manager et mlm | rien en écriture | |

Place sur harvlab2 : registre 8 Gio, deux disques de 8 Gio, image de VM
d'environ 1 Gio, soit environ 25 Gio sur 48 Gio de disque Longhorn ; à
relever avant (volumes existants, `longhorn_room.fits` de la console
elle-même). Mémoire : cluster 1+1 de 8 Gio d'invité et registre de 512 Mio
au plus dans 32 Gio. Disque des images de node2 : `harvlab.sh tidy` après
chaque essai.

### Préparatifs (règles du homelab)

- Adresse du registre d'amorçage de harvlab2 : **172.16.2.76**
  (`harvlab2-registry.home.lo`), enregistrée dans NetBox
  (`infra-dotfiles/netbox/seed.py`) et Pi-hole (`dns.hosts` de
  `pihole.toml`, `pihole reloaddns`) avant le déploiement, hors du pool
  172.16.2.200-205 et de la plage DHCP.
- Registre du client simulé : conteneur podman sans privilège sur node1,
  `registry.suse.com/suse/registry:3.1`, TLS par une CA de test et
  htpasswd, `--memory 256m`, port 5443 ouvert à chaud
  (`firewall-cmd --zone=public --add-port=5443/tcp`, jamais
  `--reload`), retiré après l'essai (port refermé).
- Clé de signature de test du client : générée pour l'essai, clé privée
  rangée dans Vault (`secret/infra/harvops-artefacts-test`), jamais dans le
  dépôt.
- Coupure d'Internet des VMs du cluster enfant : table nftables temporaire
  sur node2, famille `bridge`, qui rejette tout paquet transféré dont la
  source est dans 172.16.2.200-205 et la destination hors de
  172.16.0.0/16 (compteurs relevés). Retirée après l'essai
  (`nft delete table bridge harvops-proof`).

### Déroulé

1. **Paquet** (node1) : `harvester-artefacts pack` de `banc-260` 1.0.0 :
   chart podinfo (OCI), image `ghcr.io/stefanprodan/podinfo`, la même
   renommée `docker.io/harvops-proof/podinfo:2.6.0` (nom qui n'existe pas
   sur Internet), un manifeste. Quatre fichiers : signé par la clé de test,
   non signé, signé par une clé inconnue, signé puis altéré (un octet).
2. **Import** (console de dev, harvlab2 non requis) : altéré refusé ; non
   signé et clé inconnue acceptés après acquittement, journal relu dans
   l'Activité ; mode strict : les deux refusés ; clé ajoutée aux clés de
   confiance : signé accepté. Mode strict remis à l'état d'origine.
3. **Registre d'amorçage** (node2 allumé par son iLO, harvlab éteint,
   `HARVLAB_NAME=harvlab2 HARVLAB_NODES=1 harvlab.sh start`) : plan
   `registrytest` = phase Registre (amorçage 8 Gio, 172.16.2.76, publication
   de `banc-260`) et phase Clusters (un 1+1 de `rke2-banc` avec registres
   `amorcage` puis `client-sim`, miroirs `docker.io` et `ghcr.io`, sans
   repli). `check`, puis exécution, durées relevées. Côté cluster :
   `kubectl -n harvester-ops-registry get deploy,pvc,svc,secret`,
   `/v2/_catalog` avec le compte de lecture depuis node1.
4. **Nœuds du cluster enfant** : `/etc/rancher/rke2/registries.yaml` et
   `config.yaml.d` relus sur le plan de contrôle et sur le worker (compte de
   lecture seulement, aucun compte d'écriture).
5. **Preuve sans Internet** : table nftables posée ; témoin négatif
   `crictl pull docker.io/library/busybox:1.37` (absent du paquet) qui
   échoue ; `crictl pull docker.io/harvops-proof/podinfo:2.6.0` et
   `ghcr.io/stefanprodan/podinfo:<version>` qui réussissent ;
   `containerd.log` qui nomme le point d'accès du registre ; compteurs de la
   table non nuls. Puis un pod qui emploie l'image renommée : `Running`.
6. **Chart par CAAPH**, table toujours posée : service podinfo depuis le
   registre d'amorçage (Cluster API > Services, « Depuis un registre ») ;
   `HelmReleaseProxy` prêt, pod `Running` dans le cluster enfant.
7. **Miroir** : registre du client en premier, publication de `banc-260`
   dessus ; conteneur arrêté sur node1, pull d'une image du paquet qui
   réussit encore (repli sur le registre d'amorçage) ; relancé.
8. **Robustesse** : pod du registre supprimé, recréé, images toujours là
   (PVC) ; reprise d'une exécution arrêtée pendant la publication (blobs
   déjà envoyés sautés).
9. **Remise en état** : table nftables retirée, service et cluster enfant
   supprimés (Secrets des registres partis avec lui, relevé), registre
   d'amorçage supprimé avec ses données (confirmation tapée), déclarations
   retirées, registre du client arrêté et port refermé, entrées NetBox et
   Pi-hole gardées pour les essais suivants (ou libérées sur demande),
   `harvlab.sh tidy`, harvlab2 arrêté, node2 éteint.
10. **harv4** (si accordé) : node4 allumé par son iLO,
    `harvester-startup.sh --cluster harv4` ; paquet des images système de
    RKE2 v1.34.11 (taille et durées de `pack`, import, publication), registre
    de 40 Gio sur le pool `fast` ; deux clusters 1+1 à registres différents
    (l'un sur le registre d'amorçage, l'autre sur le registre du client
    simulé) ; suppression de tout ; adresse du registre de harv4 réservée
    puis libérée dans NetBox et Pi-hole ; `harvester-shutdown.sh --cluster
    harv4`, node4 éteint.

Non vérifiable sur nos bancs, à dire comme tel : un registre Harbor de
client réel, un cluster enfant amorcé entièrement sans Internet (point 191),
un registre d'amorçage sur un cluster de plusieurs nœuds (répliques
Longhorn distribuées), plus de deux clusters enfants sur un registre.

Puis : suite API en parallèle (`python3 -m pytest tests/api/ -q -n 12`),
suite e2e (`python3 -m pytest tests/e2e/ -q -n 8`), bilan de conformité
règle par règle (CONTRIBUTING.md et style des développeurs Harvester),
relecture indépendante du diff, cas limites joués en réel, `git grep` des
tirets cadratins, flèches Unicode et mentions d'outillage dans tout ce qui
est publié.

## Points à trancher

180. **Bancs et accords.** Recommandation : harvlab2 seul suffit à la
     preuve centrale (cluster enfant qui tire une image et un chart du
     registre d'amorçage, Internet coupé). **Accords demandés** :
     (a) reprendre harvlab2 à 32 Gio (accord donné pour la 2.5.0, à étendre)
     et allumer node2 ; (b) poser puis retirer une table nftables
     temporaire sur node2 pour couper Internet aux VMs du cluster enfant ;
     (c) un conteneur temporaire sur node1 (registre du client simulé,
     port 5443 ouvert puis refermé) ; (d) réserver 172.16.2.76 dans NetBox et
     Pi-hole ; (e) facultatif, harv4 pour un paquet de plusieurs gigaoctets
     et deux clusters à registres différents (coût énergétique de node4).
181. **Image du registre d'amorçage.** Options : A
     `registry.suse.com/suse/registry:3.1` (Distribution 3.1 en image BCI) ;
     B image amont `registry:3` ; C Zot. Recommandation : A, épinglée par
     empreinte dans le paquet interne de la release.
182. **Où tourne le registre d'amorçage.** Options : A pod sur le RKE2 de
     Harvester, PVC Longhorn, service LoadBalancer servi par le kube-vip de
     Harvester ; B VM dédiée ; C NodePort sur l'adresse d'un nœud.
     Recommandation : A (mécanisme déjà employé par le service de
     compatibilité de CAPHV, pas de VM à maintenir, adresse qui survit à un
     nœud).
183. **TLS, comptes et outils.** Recommandation : TLS toujours, CA générée
     par la console (ou certificat du client) ; deux comptes, écriture et
     lecture, jamais d'accès anonyme (l'authentification htpasswd de
     Distribution couvre toute requête) ; `openssl` et `apache2-utils`
     ajoutés à l'image de la console ; si `apache2-utils` manque au dépôt
     BCI, bcrypt écrit en Python pur dans `bin/lib`, éprouvé par les
     vecteurs de test publiés.
184. **Confiance du paquet.** Recommandation : signature fausse ou empreinte
     fausse toujours refusées, même en mode permissif ; non signé et clé
     inconnue acceptés après acquittement explicite, noté au journal et dans
     `trust.json` ; mode strict par Paramètres > Artefacts (administrateur),
     verrouillable par `config.yaml` (`artefacts.strict: true`).
185. **Entrées de `pack`.** Recommandation : description en JSON (YAML si
     PyYAML est présent) ; images d'un chart listées explicitement (pas de
     rendu Helm sans helm) ; une seule plateforme, `linux/amd64`, option
     `--platform` pour une autre.
186. **Rangement dans le registre.** Options : A `<registre>/<hôte
     d'origine>/<chemin>` avec `rewrite` dans les miroirs RKE2 ; B même
     chemin qu'à l'origine. Recommandation : A (deux images de même chemin
     sur deux hôtes ne se mélangent pas) ; tag déjà présent avec une autre
     empreinte refusé.
187. **Plusieurs registres.** Recommandation : déclarations dans la console
     (sans secret), identifiants en Secrets sur chaque cluster Harvester qui
     emploie le registre, saisis au rattachement ; un cluster enfant désigne
     une liste ordonnée (préféré puis secours), chaque registre déclare son
     usage (images, charts) et les registres d'origine qu'il miroite.
188. **Identifiants sur les nœuds.** Recommandation : seul le compte de
     lecture va dans les clusters enfants (il finit dans un fichier des
     nœuds) ; le compte d'écriture ne quitte jamais le Secret du cluster
     Harvester ; avertissement si le client ne donne qu'un compte.
189. **Ordre des phases.** Recommandation : Registre après Terraform et avant
     Cluster API (les clusters ont besoin du registre à leur création ; la
     phase peut employer une classe de stockage créée par Terraform).
190. **Clusters enfants existants.** Recommandation : registres posés à la
     création seulement en 2.6.0 (RKE2 lit sa configuration au démarrage,
     un changement demande un renouvellement des machines) ; geste
     « Changer les registres » par renouvellement avec la 2.7.0.
191. **Amorçage complet sans Internet.** Recommandation : hors 2.6.0. La
     preuve de la 2.6.0 coupe Internet après l'installation de RKE2. Une
     image de VM préparée (`/opt/rke2-artifacts`, mode `airGapped` de
     CAPRKE2) et les images système de RKE2 dans le paquet font l'objet d'un
     lot à part (2.7.0 ou plus tard).
192. **Registre joignable depuis le réseau des VMs.** Recommandation : pod
     sonde bref attaché au réseau des VMs, adresse par DHCP, si la tâche 1 le
     prouve ; sinon, avertissement « vérifié depuis le cluster Harvester
     seulement » et contrôle rejoué depuis le premier nœud du cluster
     enfant, avant les workers.
     *Écart livré (décision du 07/11/2026, revue finale I-4)* : la 2.6.0
     livre l'avertissement seul (`registry-vm-network-unchecked`,
     `cluster-registry-vm-network-unchecked`) dans chaque vérification et
     dans la doc ; la sonde par un pod du réseau des VMs, prouvée par la
     tâche 1, arrive en 2.6.1. `bci-busybox` reste dans le paquet du
     registre : la recette de la sonde s'en sert (`udhcpc`, `wget`).
193. **Taille et suppression du registre d'amorçage.** Recommandation : 20
     Gio par défaut, place vérifiée sous les deux contraintes de Longhorn ;
     pas de nettoyage automatique en 2.6.0 ; suppression refusée tant qu'un
     cluster enfant ou un plan l'emploie ; données gardées sauf confirmation
     tapée.
194. **Emplacement dans l'interface.** Recommandation : onglets **Registres**
     et **Artefacts** dans Cluster API (les clusters enfants en sont les
     consommateurs), carte Registre dans l'onglet Plans, réglages de
     confiance dans Paramètres > Artefacts.
195. **Rôles.** Recommandation : administrateur pour importer, supprimer un
     paquet, clés et mode strict, poser ou supprimer un registre d'amorçage,
     déclarer un registre du client ; opérateur pour publier, rattacher et
     choisir des registres pour un cluster enfant.
196. **Nœuds Harvester (`containerd-registry`).** Recommandation : hors
     2.6.0 ; le réglage existant reste le geste manuel ; le brancher sur les
     registres déclarés vient avec la 2.7.0 si un contenu Terraform en a
     besoin.

## Décisions (05/11/2026)

Les recommandations des points 180 à 196 sont retenues, bancs compris : harvlab2, règle nftables temporaire sur node2, faux registre client temporaire sur node1 (port 5443, ouvert à chaud puis refermé), 172.16.2.76 réservée dans NetBox et Pi-hole ; harv4 seulement si un essai l'exige.

## Essais préalables (tâche 1, 05/11/2026, harvlab2)

node2 allumé par son iLO, harvlab2 démarré (Harvester v1.9.0, pile Cluster
API de la 2.5.0). 172.16.2.76 réservée avant usage : NetBox
(`harvlab2-registry`, `infra-dotfiles` f591eea) et Pi-hole
(`harvlab2-registry.home.lo`). Tout a été retiré du banc après les essais,
sauf ces deux réservations.

**Registre dans un pod (critère 3 : réussi).**
- `registry.suse.com/suse/registry:3.1` est Distribution v3.1.1.
  Utilisateur `registry`, uid et gid 499. Configuration
  `/etc/registry/config.yml`, données `/var/lib/docker-registry` (VOLUME),
  port 5000. Index multi-architecture
  `sha256:40748574613462a186825ce49652be7ddc6fd2dbdc9e4d6b5ca4e62c5235536d`,
  image linux/amd64 `sha256:de84c976e7934762d3c2cee87d56567889d19bbddccf5dbdaeb1f910cff755a1`.
- `runAsNonRoot` refuse un utilisateur non numérique : il faut
  `runAsUser: 499`, `runAsGroup: 499`, et `fsGroup: 499` pour le volume
  Longhorn (PVC `harv-rep1` de 2 Gio, lié aussitôt).
- Image importée par `ctr` depuis une archive écrite par `oci_layout` :
  nommée seulement par son tag, elle est introuvable pour un pod qui
  l'épingle par empreinte (`ErrImageNeverPull`). L'archive porte donc aussi
  le nom `<dépôt>@<empreinte>` (fait dans `export_ctr_tar`) ; le pod a
  ensuite démarré sans rien tirer.
- Service LoadBalancer annoté `kube-vip.io/loadbalancerIPs: 172.16.2.76` :
  adresse servie par le kube-vip de Harvester ; `/v2/` répond 401 depuis
  node1 sans identifiant.
- **L'authentification htpasswd de Distribution n'a pas de droits** : le
  compte `pull` pousse aussi (session d'envoi acceptée, vu sur un
  conteneur local). Séparation prouvée par deux conteneurs dans le même pod
  sur le même volume : une instance en lecture seule
  (`storage.maintenance.readonly.enabled: true`, comptes `push` et `pull`)
  sur le port 443 du service, une instance en écriture (compte `push`
  seulement) sur le port 5443. Mesuré : `pull` et `push` lisent sur 443,
  toute écriture y reçoit 405 ; `pull` est refusé sur 5443. Une image
  poussée sur 5443 se lit aussitôt sur 443 (pas de cache de descripteurs).
  Point 197 ci-dessous.

**Miroirs RKE2 par CAPRKE2 (critère 1 : réussi, avec deux corrections).**
- Cluster 1+1 (`harvester-capi render`, gabarits retouchés à la main) :
  `privateRegistriesConfig` (miroir `ghcr.io` vers `https://172.16.2.76`,
  `rewrite` `^(.*)$` vers `ghcr.io/$1`, `authSecret` au compte de lecture)
  et un fichier `files` `/etc/rancher/rke2/config.yaml.d/50-harvester-ops-registry.yaml`
  portant `disable-default-registry-endpoint: true`. RKE2 v1.34.11 lit bien
  ce répertoire : le `hosts.toml` de `ghcr.io` n'a plus de ligne `server`
  (aucun repli). Même résultat sur le plan de contrôle et sur le worker.
- Image présente : tirée du registre (journal du registre :
  `GET /v2/ghcr.io/stefanprodan/podinfo/...` avec le compte `pull`). Tag
  absent : `not found` (404 du miroir), pas de repli vers Internet.
- Les registres sans miroir gardent leur point d'accès : le cluster a monté
  avec les images système de RKE2 tirées de `docker.io`. **Conséquence** :
  miroiter `docker.io` sans repli exige les images système de RKE2 dans le
  registre (point 198).
- **Correction 1** : un Secret `kubernetes.io/tls` exige `tls.crt` et
  `tls.key` ; vides, CAPRKE2 écrit `cert_file` et `key_file` vides et
  containerd refuse tout tirage de ce registre (« failed to load X509 key
  pair »). Un Secret `Opaque` portant seulement `ca.crt` marche (CAPRKE2 ne
  regarde pas le type) : seul `ca_file` est écrit. Les Secrets
  `<cluster>-registry-<registre>-ca` seront donc `Opaque`.
- **Correction 2** : `insecureSkipVerify` laissé absent : vérification
  active (la description du CRD, « default is true », est trompeuse).

**CAAPH et OCI (critère 2 : réussi, pas de repli HTTP).**
- `HelmChartProxy` `repoURL: oci://172.16.2.76/charts`, `chartName:
  podinfo`, `credentials` (Secret `config.json` de Docker) et
  `tlsConfig.caSecret` (`ca.crt`), Secrets sur le cluster Harvester :
  `HelmReleaseProxy` prêt, release `podinfo` déployée, pod `Running` dans
  le cluster enfant, image tirée par le miroir.
- Un chart absent au chemin attendu donne « FetchReference ... not found » :
  la CA et le compte étaient acceptés. Le chart doit être sous
  `<repoURL>/<chartName>:<version>`, soit `charts/podinfo:6.9.2`
  (`oci_layout.chart_repo`).
- Un chart rangé par `chart_to_layout` (configuration écrite par la
  console) est tiré par `helm pull oci://` à la même empreinte.

**Sonde depuis le réseau des VMs (point 192 : réussie).** Pod bref
`bci-busybox:16.0` attaché par Multus à `default/lab-net`, capacités
`NET_ADMIN` et `NET_RAW` : bail DHCP obtenu de Pi-hole, route vers
172.16.2.76 par `net1`, `wget` de `/v2/` : 401. `bci-busybox` n'a pas de
script `udhcpc` : la sonde fournit le sien (`ip addr add`) et doit passer
`-R` pour rendre le bail (deux baux de l'essai, 172.16.11.86 et .204,
expirent en 24 h).

**Outils (point 183).** `openssl` (openssl-3 3.5.0) est déjà dans
`registry.suse.com/bci/python:3.11` ; `apache2-utils` 2.4.66 est dans le
dépôt SLE_BCI et ajoute `libapr1`, `libapr-util1`, `libgdbm4` (paquets
marqués sans support de l'éditeur). `htpasswd -niB <compte>` lit le mot de
passe sur l'entrée standard (coût bcrypt 5 par défaut). Pas de bcrypt en
Python pur.

**CA générée par la console.** Python 3.13 vérifie les certificats en mode
strict : une CA sans `keyUsage` est refusée (« CA cert does not include
key usage extension »). La CA porte `basicConstraints=critical,CA:TRUE`,
`keyUsage=critical,keyCertSign,cRLSign`, `subjectKeyIdentifier` ; le
certificat du registre `subjectAltName` (nom et adresse),
`keyUsage=critical,digitalSignature`, `extendedKeyUsage=serverAuth`,
`authorityKeyIdentifier`.

**Copie en flux (tâche 2, contre de vrais registres).**
`registry.suse.com/suse/registry:3.1` (49 Mo) copié en 1,4 s,
`ghcr.io/stefanprodan/podinfo:6.9.2` (33 Mo) en 2,7 à 3,9 s, jetons
anonymes de registry.suse.com et ghcr.io ; 30 Mio de mémoire au plus pour
82 Mo. Distribution 3.1 accepte les morceaux `PATCH` (4 Mio), le montage
entre dépôts (201, huit couches sur huit) et la reprise par `Range` (206,
deux tiers du blob seulement relus).

Points nouveaux, soumis avant les tâches concernées :

197. **Séparer lecture et écriture du registre d'amorçage** (tâche 7).
     Options : A deux conteneurs Distribution dans le pod, lecture seule sur
     443 et écriture sur 5443, même volume (essayé) ; B un seul conteneur,
     compte `pull` capable d'écrire (risque : il est dans un fichier de
     chaque nœud) ; C serveur de jetons. Recommandation : A.
198. **Miroir de `docker.io` sans repli** (tâches 9 et 13). Couper le repli
     pour `docker.io` coupe aussi les images système de RKE2 au démarrage
     des nœuds. Options : A pas de `no_fallback` par défaut pour
     `docker.io`, avertissement s'il est demandé sans les images système
     dans le registre ; B images système de RKE2 ajoutées au paquet
     (lot du point 191). Recommandation : A pour la 2.6.0, et la preuve de
     la tâche 13 miroite `docker.io` avec repli puis coupe Internet par la
     table nftables, qui prouve seule l'absence de repli.
