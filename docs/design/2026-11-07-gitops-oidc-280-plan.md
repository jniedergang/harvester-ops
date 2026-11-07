# 2.8.0 : GitOps, accès aux clusters enfants et registres achevés (plan de réalisation)

Conceptions : `2026-11-02-bare-metal-post-installation.md` (décisions 111 à
131 retenues, en particulier 124 et 125), plans de la 2.4.0
`2026-11-03-plans-deploiement-240-plan.md`, de la 2.5.0
`2026-11-05-plans-capi-250-plan.md`, de la 2.6.0
`2026-11-05-artefacts-registre-260-plan.md` (décisions 190, 191 et 196) et
de la 2.7.0 `2026-11-07-artefacts-complets-defaire-270-plan.md` (points 213
et 217). Point de départ : 2.7.0 (artefacts appliqués aux clusters enfants,
socle de politiques, comptes d'automatisation et jetons, « Défaire le
déploiement », désinstallation Turtles). Une 2.7.1 (m1 de la revue finale
2.7.0, `--remove-images` restreint, verrou tenu pendant la désinstallation)
peut sortir avant : ce plan n'en dépend pas et se rebase dessus.

## But

La 2.8.0 termine le découpage de la conception (dernière ligne du tableau
« Découpage en versions ») et solde ce que les plans 2.6.0 et 2.7.0 ont
reporté.

1. **Accès humain aux clusters enfants** (décision 125) :
   - **import dans un Rancher externe**, celui qui gère le cluster Harvester
     (connexion par Rancher, 1.50.0) : les droits se gèrent dans Rancher, la
     fenêtre Membres (1.73.0) sert aussi pour le cluster enfant ;
   - sinon **OIDC de l'API** du cluster enfant vers le fournisseur
     d'identité du client, posé à la création par le modèle de cluster ;
   - les comptes de service restent réservés à l'automatisation (2.7.0,
     inchangé).
2. **GitOps en option** (décision 124) : aucun par défaut ; un élément
   `gitops` choisit **Fleet si le cluster enfant est importé dans un Rancher
   externe, Flux sinon**. La console pose l'outil et sa source (dépôt Git du
   client, ou artefact OCI d'un paquet publié dans un registre), puis montre
   son état. Elle ne réécrit pas ce que l'outil sait faire.
3. **Renouvellement des machines** d'un cluster enfant existant : le geste
   « Changer les registres » promis par la décision 190, et le même
   mécanisme pour changer l'OIDC et pour faire confiance à une nouvelle CA
   de registre.
4. **Registres achevés** (points 213 et 217 de la 2.7.0, décision 196) :
   m-14 (mot de passe de lecture rangé deux fois), renouvellement du
   certificat du registre d'amorçage, branchement du réglage
   `containerd-registry` des nœuds Harvester sur un registre déclaré.
5. **Cluster enfant amorcé entièrement sans Internet** (décision 191) :
   RKE2 lui-même et ses images système depuis le registre.

Déjà fait, rien à refaire : la restriction du port d'écriture 5443 du
registre d'amorçage aux adresses de la console (2.6.1, second Service et
réglage Paramètres > Registres, `harvester-registry settings` et
`write-access`).

Hors 2.8.0 : Argo CD (décision 124, option C écartée), installation de
Rancher, montée de version de Kubernetes des clusters enfants, Harbor posé
par la console, les reliquats de la 2.7.0 marqués 2.7.1.

## Architecture

```
bin/lib/rancher_import.py   nouveau (pur) : nom du cluster dans Rancher,
                            corps du provisioning.cattle.io/v1 Cluster,
                            lecture du jeton d'enregistrement (forme 2.14 et
                            2.15), images du manifeste d'enregistrement,
                            restes connus après retrait, marques d'origine
bin/lib/child_oidc.py       nouveau (pur) : contrôle des réglages OIDC,
                            AuthenticationConfiguration, fichier, arguments et
                            montage de l'API RKE2, kubeconfig OIDC remis aux
                            personnes (greffon exec)
bin/lib/child_gitops.py     nouveau (pur) : choix de l'outil, objets Flux
                            (source, Kustomization, compte, liaison), objets
                            Fleet (GitRepo, HelmOp, cibles), état lu des
                            conditions, refus
bin/lib/flux_artifact.py    nouveau : artefact OCI au format Flux construit
                            depuis un élément gitops d'un paquet (archive
                            déterministe, types de média Flux), poussé par
                            oci_push.Registry
bin/lib/capi_renew.py       nouveau (pur) : rotation des gabarits d'un
                            cluster enfant (copie -r<N>, ClusterClass
                            repointée), différences, attente du renouvellement
bin/lib/rke2_offline.py     nouveau (pur) : élément `rke2` d'un paquet,
                            commandes de récupération par le registre,
                            fichier netrc, contrôles de version
bin/lib/capi_cluster.py     + options `rancher_external`, `oidc`, `offline` ;
                            postprocess pose fichiers, arguments, montage,
                            airGapped ; `rancher_import` (Turtles intégré)
                            exclusif de `rancher_external`
bin/lib/capi_manifests.py   + ressource Secret de type
                            addons.cluster.x-k8s.io/resource-set dans un CRS
                            (identifiants de lecture de Flux)
bin/lib/child_policies.py   + groupe administrateur OIDC (seule liaison vers
                            cluster-admin admise, rendue par la console)
bin/lib/registries.py       m-14 (marque par étiquette), CA en transition
                            (ancienne + nouvelle), réglage containerd-registry
bin/lib/registry_seed.py    clé de CA gardée (Secret), contraintes de nom,
                            renouvellement du certificat serveur
bin/lib/artefacts.py        gitops et rke2 rejoignent KINDS, LATER_KINDS vide
bin/lib/deploy_plans.py     + phase gitops (format, contrôle, rendu) ;
                            options de modèle rancher_external, oidc, offline
bin/lib/deploy_chain.py     ORDER + "gitops" (dernière phase)
bin/lib/deploy_undo.py      + sortes gitops et rancher-import
bin/harvester-capi.py       + rancher-import, rancher-unimport,
                            rancher-status, oidc-kubeconfig, gitops-check,
                            gitops-deploy, gitops-remove, gitops-status,
                            renew
bin/harvester-registry.py   + renew-cert, harvester-nodes
bin/harvester-artefacts.py  + pack --rke2 VERSION, publication de `gitops`
                            (artefact Flux) et de `rke2` (blobs)
web/app.py                  routes et actions des gestes, phase gitops,
                            défaire étendu, constats
web/static/js/              capi-create.js (Accès : Rancher externe, OIDC ;
                            sans Internet), capi.js (Renouveler, Accès),
                            nouveau capi-gitops.js, bm-plans.js (carte
                            GitOps), registries.js (Renouveler le
                            certificat, Nœuds Harvester), artefacts.js,
                            rancher-members.js (cluster enfant), i18n.js
```

Ce qui est réutilisé, sans copie :

| Besoin | Mécanisme existant | Où |
|---|---|---|
| Parler à Rancher au nom de la personne | kubeconfig de session (mandataire `https://<rancher>/k8s/clusters/<id>` + `tokenFile`), lu par `rancher_of` | `bin/lib/hv_projects.py` `rancher_of`, `web/app.py` `_session_rancher`, `_rancher_get` |
| Refus d'un compte local pour une écriture dans Rancher | `rancher-session-needed` (409) | `web/app.py` (Projets, Membres) |
| Client HTTP Rancher en ligne de commande | `web_loader.load("rancher_sso")`, `rancher_sso.Http` | `bin/lib/web_loader.py`, `web/rancher_sso.py` |
| Membres d'un cluster dans Rancher | CRTB par `/v3/clusterroletemplatebindings?clusterId=` | `web/static/js/rancher-members.js`, `web/app.py` `api_rancher_members` |
| Kubeconfig d'un cluster enfant | `child_access.session(kube, ns, cluster)` (Secret `<c>-kubeconfig`, identité de la personne, fichier 0600 effacé) | `bin/lib/child_access.py` |
| Droit d'administrer un cluster enfant | `SelfSubjectAccessReview` sur le Secret `<c>-kubeconfig` (`child-admin-required`) | `web/app.py` `_capi_child_base`, `CHILD_ADMIN_REQUIRED` |
| Livrer des objets dans un cluster enfant, retrait suivi | ConfigMap + `ClusterResourceSet` `art-<élément>` en `Reconcile` | `bin/lib/capi_manifests.py` (2.7.0) |
| Chart dans un cluster enfant | `harvester-capi service-deploy` source registre | `bin/harvester-capi.py`, `bin/lib/capi_services.py` |
| Rôles par groupe | `RoleBinding` vers `admin`, `edit`, `view` | `bin/lib/child_policies.py` |
| Fichiers et Secrets dans les gabarits RKE2 | `_apply_registries` (fichiers ajoutés aux deux gabarits, Secrets en documents) | `bin/lib/capi_cluster.py` |
| Configuration des registres RKE2 | `registries.rke2_config`, `NO_FALLBACK_FILE` | `bin/lib/registries.py` |
| Pousser un blob, un manifeste | `oci_push.Registry.push_blob`, `push_blob_file`, `put_manifest` | `bin/lib/oci_push.py` |
| Chart et images d'un paquet | `pack`, `oci_layout.chart_to_layout`, `copy_image` | `bin/harvester-artefacts.py`, `bin/lib/oci_layout.py` |
| CA et certificat du registre d'amorçage | `registry_seed.make_tls` (CA 3 650 jours, serveur 730 jours), `cert_not_after`, `registry-cert-expiring` | `bin/lib/registry_seed.py` |
| Réglage `containerd-registry` | masquage, démasquage, groupe network, danger `rke2` ; identifiants dans un Secret `rke.cattle.io/auth-config` de `fleet-local` (vu en 1.9) | `bin/lib/hv_settings.py`, `bin/lib/hv_forklift.py` `registry_auth_secret` |
| Phases, éléments, reprise, arrêt | `deploy_chain.run`, `Phase(items=..., before=..., verify_item=...)` | `bin/lib/deploy_chain.py` |
| Défaire | `deploy_undo.collect`, `plan`, `KINDS_ORDER`, `--expect-run` | `bin/lib/deploy_undo.py`, outils de `bin/` |
| Actions imbriquées | `_capi_launch(..., parent=run.id)`, `_deploy_wait_child` | `web/app.py` |
| Sonde depuis le réseau des VMs | `vm_probe` (Job, `udhcpc`, `wget`) | `bin/lib/vm_probe.py` (2.6.1) |
| Espace de travail | `work_dir.path` (0700, jamais `/tmp`) | `bin/lib/work_dir.py` |

## Contraintes communes (valeurs exactes)

### Import dans un Rancher externe

- **Quel Rancher** (point 222) : celui de la session de la personne, lu par
  `hv_projects.rancher_of` sur le kubeconfig de session du cluster Harvester.
  Compte local de la console : refus `rancher-session-needed` (409), comme
  Projets et Membres. Le Rancher intégré à Harvester n'est jamais visé par ce
  chemin.
- **Option du cluster enfant** `rancher_external: true` (liste blanche de
  `capi_cluster.KNOWN`, `CONSOLE_OPTIONS`) ; **exclusive** de
  `rancher_import` (Turtles du Rancher intégré, option existante) :
  `rancher-import-conflict`, bloquant. Deux agents Rancher dans un cluster se
  disputeraient `cattle-system`.
- **Nom dans Rancher** : `<harvester>-<c>` si le namespace du cluster enfant
  vaut son nom, sinon `<harvester>-<ns>-<c>` ; au-delà de 63 caractères,
  tronqué à 54 plus `-` et 8 hexadécimaux de sha256 du nom complet. RFC 1123.
- **Objet créé dans Rancher** : `provisioning.cattle.io/v1` `Cluster`,
  namespace `fleet-default`, `spec: {}`, étiquettes
  `harvester-ops.io/managed: "true"`, `harvester-ops.io/harvester: <h>`,
  `harvester-ops.io/child-namespace: <ns>`, `harvester-ops.io/child: <c>` ;
  annotations `harvester-ops.io/applied-by: <personne>` et, depuis un plan,
  `harvester-ops.io/plan-run: <id>`. Écrit par l'API Rancher avec le jeton de
  la session (`POST /v1/provisioning.cattle.io.clusters`).
- **Manifeste d'enregistrement** : lu par `/v3/clusterregistrationtokens?
  clusterId=<c-m-...>` (`manifestUrl`). L'API norman substitue le jeton ; en
  2.15 le statut du CRD ne le porte plus (`tokenSecretName`, `{token}` dans
  les adresses) : seule la lecture par `/v3` est employée (sources). Le
  manifeste est récupéré par la console (10 Mio au plus), écrit en 0600 dans
  `work_dir`, appliqué dans le cluster enfant par `kubectl apply` avec le
  kubeconfig de `child_access.session`, puis effacé dans un `finally`. Il
  n'est jamais rangé, jamais dans une étape ni un journal (il porte un jeton).
  Il ne passe pas par `capi_manifests` : il crée `cattle-system` et une
  `ClusterRoleBinding` vers `cluster-admin`, ce que la règle des manifestes
  refuse à juste titre pour un paquet ; exception nommée, réservée à ce
  manifeste lu chez le Rancher de la session.
- **Images de l'agent** : chaque image du manifeste (agent de Rancher) est
  sondée dans les registres du cluster enfant avant l'application
  (`HEAD` du manifeste avec le compte de lecture) ; absente et repli vers
  Internet coupé : `rancher-agent-image-missing`, bloquant ; absente avec
  repli : avertissement.
- **Marque sur le cluster de gestion** : annotation du `Cluster` Cluster API
  `harvester-ops.io/rancher-external: <id du Rancher>/<nom dans Rancher>/<c-m-...>`.
- **Prêt** : `status.ready` du cluster de provisionnement et cluster de
  gestion `Ready` dans Rancher, délai 900 s par défaut (1 à 60 min).
- **Retrait** (point 232) : suppression du cluster dans Rancher, attente de
  sa disparition (600 s), puis suppression par la console des restes connus
  dans le cluster enfant (liste relevée en tâche 1, constante citée avec sa
  source), chacune jugée d'abord par le serveur (`--dry-run=server`) ;
  jamais un namespace qui porte autre chose que des objets de Rancher.
  Marque relue juste avant d'écrire (`--expect-run`, leçon de la 2.7.0).
  Revue des tâches 1 à 4 : un reste n'est retiré que s'il porte une marque
  de Rancher (étiquette ou annotation d'un domaine `cattle.io`, propriétaire
  d'un groupe `cattle.io`, ou objet du manifeste d'enregistrement à son nom
  exact), sinon `kept-not-rancher` ; la suppression du cluster retire le
  cluster de Rancher et la marque sans rien lire dans le cluster enfant, et
  `delete --forget-rancher` (administrateur) oublie un Rancher disparu.

### OIDC de l'API

- **Option du cluster enfant** `oidc` (dictionnaire, liste blanche) :
  - `issuer_url` : `https://` seulement, 2 048 caractères au plus, sans
    identifiants ni requête ;
  - `audience` : 1 à 256 caractères (l'identifiant du client chez le
    fournisseur) ;
  - `username_claim` : défaut `email` ; `username_prefix` : défaut `oidc:`,
    **obligatoire et non vide**, jamais commençant par `system:` ;
  - `groups_claim` : défaut `groups` ; `groups_prefix` : défaut `oidc:`,
    mêmes règles ;
  - `ca_pem` : facultatif, CA du fournisseur (lisible par `ssl`, 64 Kio au
    plus), inline dans la configuration ;
  - `admin_group` : facultatif, nom de groupe (128 caractères, une ligne),
    lié à `cluster-admin` (point 229).
  Un seul fournisseur par cluster (point 228).
- **Forme** : `AuthenticationConfiguration`, `apiVersion:
  apiserver.config.k8s.io/v1` pour Kubernetes 1.34 et plus,
  `apiserver.config.k8s.io/v1beta1` de 1.30 à 1.33, refus
  `oidc-version-too-old` avant 1.30 ; version servie par le cluster enfant
  confirmée en tâche 1. `jwt[0].issuer.url`, `audiences: [<audience>]`,
  `certificateAuthority` si donnée, `claimMappings.username {claim, prefix}`,
  `claimMappings.groups {claim, prefix}`. Jamais avec les drapeaux
  `--oidc-*` (exclusifs de `--authentication-config`).
- **Pose** : dans le seul `RKE2ControlPlaneTemplate` :
  - fichier `/etc/rancher/rke2/hops-auth/authentication-config.yaml`,
    `owner: root:root`, `permissions: "0600"` ;
  - `serverConfig.kubeAPIServer.extraArgs` +
    `authentication-config=/etc/rancher/rke2/hops-auth/authentication-config.yaml` ;
  - `serverConfig.kubeAPIServer.extraMounts`
    `{"/etc/rancher/rke2/hops-auth": "/etc/rancher/rke2/hops-auth"}` (sens
    de la carte hôte vers conteneur et lecture seule confirmés en tâche 1).
  Champs de CAPRKE2 v1beta2 lus dans sa source (`ComponentConfig.ExtraArgs
  []string`, `ExtraMounts map[string]string`, `RKE2ConfigSpec.Files`).
- **Aucun secret** : l'API n'a besoin que de l'audience. Le secret du client,
  s'il existe, reste chez la personne (greffon kubelogin).
- **Kubeconfig remis aux personnes** (point 230) : utilisateur `oidc` en
  greffon `exec` (`client.authentication.k8s.io/v1`, commande `kubectl`,
  arguments `oidc-login get-token --oidc-issuer-url=<issuer>
  --oidc-client-id=<audience>`), serveur et CA du cluster enfant lus dans le
  Secret `<c>-kubeconfig`, **sans aucun identifiant**. Lisible par un
  opérateur qui a le droit d'administrer le cluster enfant (le fichier ne
  donne rien sans le fournisseur).
- **Groupe administrateur** : `ClusterRoleBinding` `hops-oidc-admins` vers
  `cluster-admin`, sujet `Group <groups_prefix><admin_group>`, rendue par
  `child_policies` dans l'élément intégré `hops-policies`. Seule liaison vers
  `cluster-admin` admise, jamais depuis un paquet (`manifest-crb-admin` reste
  vrai pour les manifestes).
- **Rancher et OIDC ensemble** (point 231) : permis, avertissement
  `oidc-with-rancher` (décision 125 : Rancher d'abord).

### GitOps

- **Sorte de paquet** `gitops` (fin de `LATER_KINDS`) : `files:
  manifests/<élément>/<fichier>.yaml` (forme déjà acceptée par
  `check_index`), objets soumis aux règles de `capi_manifests.check` à la
  construction de l'artefact (mêmes refus que les manifestes : un outil
  GitOps n'est pas une porte dérobée vers les CRD et les webhooks).
- **Outil** (point 223) : `tool: auto | flux | fleet`, défaut `auto` =
  `fleet` si le cluster enfant porte la marque `rancher-external`, `flux`
  sinon. `fleet` sans import : `gitops-fleet-needs-rancher`, bloquant.
- **Sources** (point 226) :
  - `{type: oci, package, version, item, registry}` : artefact OCI au format
    Flux publié dans un registre déclaré ; **Flux seulement**
    (`gitops-source-unsupported` avec Fleet) ;
  - `{type: git, url, branch, path, secret}` : `https://` ou `ssh://`,
    branche (défaut `main`), chemin (défaut `./`), `secret` = nom d'un secret
    du plan (saisi au lancement, décision 129) ; Flux et Fleet ;
  - `{type: chart, package, version, item, registry}` : Fleet seulement
    (`HelmOp`, chart tiré par les clusters enfants depuis le registre).
- **Flux** (point 224) : chart communautaire `flux2` 2.20.0 (Flux 2.10.0,
  Apache-2.0), livré par un paquet et installé par CAAPH dans le namespace
  `flux-system` du cluster enfant, nom de service `flux`. Valeurs imposées :
  registre des images = celui du cluster enfant (miroir), contrôleurs
  `source`, `kustomize`, `helm`, `notification` ; arguments
  `--no-cross-namespace-refs=true` et `--default-service-account=hops-gitops`
  (verrouillage multi-locataire, point 225), noms des valeurs relevés dans le
  chart en tâche 1. Les automatisations d'images ne sont pas installées.
- **Objets Flux d'un élément** (livrés par `capi_manifests`, CRS
  `art-gitops-<nom>`) : dans le namespace `hops-gitops` (créé par la
  console, hors motif `*-system`, donc admis par la règle des manifestes
  sans exception) : `OCIRepository` ou `GitRepository` `<nom>`,
  `Kustomization` `<nom>` (`prune: true` par défaut, `interval` 10 min par
  défaut, 1 min à 24 h, `targetNamespace` = le namespace d'équipe visé,
  `serviceAccountName: hops-gitops-<nom>`) ; `ServiceAccount`
  `hops-gitops-<nom>` et `RoleBinding` vers `edit` dans le namespace visé.
  `privileges: cluster` (administrateur seulement) lie ce compte à
  `cluster-admin` par une `ClusterRoleBinding` rendue par la console.
- **Identifiants de lecture pour Flux** (point 227) : Secret
  `kubernetes.io/dockerconfigjson` (compte de lecture du registre) et Secret
  `ca.crt`, livrés par une ressource **Secret** de type
  `addons.cluster.x-k8s.io/resource-set` du même CRS (jamais dans le
  ConfigMap) ; Git : Secret `identity` / `known_hosts` ou `username` /
  `password` depuis le secret du plan, même transport.
- **Artefact Flux** : archive tar.gz déterministe (ordre trié, `mtime` 0,
  uid et gid 0, mode 0644), couche
  `application/vnd.cncf.flux.content.v1.tar+gzip`, configuration
  `application/vnd.cncf.flux.config.v1+json`, dépôt
  `<registre>/harvester-ops/gitops/<paquet>/<élément>`, étiquette `<version>` ;
  types de média confirmés contre la source de Flux 2.10 en tâche 1.
- **Fleet** : dans le cluster `local` du Rancher, namespace `fleet-default`,
  par l'API de Rancher avec le jeton de la session
  (`/k8s/clusters/local/apis/fleet.cattle.io/v1alpha1/...`) : `GitRepo`
  `hops-<harvester>-<nom>` (`repo`, `branch`, `paths`, `targets: [{clusterName:
  <nom du cluster Fleet>}]`, `clientSecretName` si secret) ou `HelmOp`
  (`helm.repo: oci://<registre>/<chemin du chart>`, `version`,
  `helmSecretName`). Nom du cluster Fleet d'un cluster importé et
  disponibilité de `HelmOp` sans drapeau dans Fleet 0.15 (Rancher 2.14)
  relevés en tâche 1. Mêmes étiquettes et annotations d'origine que
  l'import. Revue des tâches 1 à 4 : le nom porte le cluster enfant
  (`hops-<harvester>-<c>-<nom>`, `hops-<harvester>-<ns>-<c>-<nom>` si le
  namespace diffère, 54 caractères plus `-` et 8 hexadécimaux au-delà de 63),
  la propriété de l'objet et de ses Secrets est jugée avant toute écriture ;
  un `GitRepo` ou un `HelmOp` sans `serviceAccount` déploie avec l'agent
  Fleet, administrateur du cluster enfant : Fleet est réservé à
  l'administrateur (`gitops-fleet-admin-only`), averti par
  `gitops-fleet-not-confined` pour `privileges: namespace` (compte confiné
  de Fleet à relever sur le banc, tâche 10).
- **Phase `gitops` du plan** : dernière phase de `ORDER` ; 16 entrées au plus,
  64 éléments au plus une fois les clusters dépliés, `timeout_minutes` 1 à
  60, 15 par défaut ; nom d'entrée RFC 1123, 34 caractères au plus
  (`art-gitops-<nom>` tient dans `capi_services.NAME_RE`).
- **Prêt** : Flux, condition `Ready` de la source puis de la `Kustomization`
  à la révision attendue ; Fleet, `status.summary.ready` égal à
  `desiredReady` du `GitRepo` ou du `HelmOp`.

### Renouvellement des machines

- Action `capi-renew:<ns>/<c>`, `harvester-capi renew --name NS/C --what
  registries,oidc,ca [--spec F] [--timeout 3600] [--dry-run]`.
- **Rotation des gabarits** (point 233) : les gabarits
  `RKE2ControlPlaneTemplate` et `RKE2ConfigTemplate` du cluster sont copiés
  en `<nom>-r<N>` (N = révision suivante, annotation
  `harvester-ops.io/template-revision`), la copie reçoit le nouveau contenu,
  la `ClusterClass` du namespace est repointée ; la topologie remplace les
  machines (`RollingUpdate`, `maxSurge: 1`, déjà dans le gabarit). Les
  anciens gabarits sont supprimés quand plus aucune machine ne s'y rapporte.
  Un cluster par namespace et une `ClusterClass` par cluster (règle de
  `caphv-generate`) : aucun autre cluster n'est touché.
- **Adresses** : une de plus pendant le renouvellement (déjà comptée par les
  vérifications de la 2.5.0 : « une par machine plus une »).
- **Délai** : 3 600 s par défaut (60 à 10 800).

### Registres

- **m-14** (point 234) : `<c>-registry-<r>-auth` ne porte plus de mot de
  passe. La marque d'usage devient l'étiquette
  `harvester-ops.io/registry-<r>: "used"` sur le Secret `<c>-registries`.
  Lecture des deux formes (ajout puis retrait, décision « applicable à
  chaud ») ; l'ancienne forme est retirée au premier renouvellement des
  registres ou à la suppression du cluster.
- **Clé de CA gardée** (point 235), pour tout registre d'amorçage posé à
  partir de la 2.8.0 : Secret `harvester-ops-registry/<r>-ca-key` (Opaque,
  étiquette `harvester-ops.io/managed`), CA générée avec
  `nameConstraints` critique limité à l'adresse IP du registre et à son nom
  DNS s'il est donné ; durées inchangées (CA 3 650 jours, serveur 730 jours).
  `harvester-registry renew-cert NOM --cluster C [--days 730]` : nouveau
  certificat serveur signé par la même CA, Secret TLS remplacé, pod
  redémarré (coupure de lecture de quelques secondes, mesurée) ; aucun
  fichier des nœuds ne change.
- **Registre sans clé gardée** (d'avant 2.8.0) : `renew-cert --new-ca` en
  trois temps : nouvelle CA, CA en transition (ancienne et nouvelle) posée
  dans chaque cluster enfant par `renew --what ca`, puis bascule du
  certificat serveur ; refus `registry-ca-transition-pending` tant qu'un
  cluster enfant qui emploie le registre n'a pas la nouvelle CA.
- **Nœuds Harvester** (point 236) : `harvester-registry harvester-nodes NOM
  --cluster C [--remove] [--dry-run]` fusionne le registre (miroirs, CA,
  compte de lecture) dans `containerd-registry` sans toucher aux autres
  entrées ; identifiants dans le Secret `rke.cattle.io/auth-config` que
  Harvester 1.9 emploie ; valeur précédente gardée (masquée) dans le
  résultat de l'action pour le retour arrière. Avertissement
  `harvester-rke2-restart` (danger `rke2` du réglage). Hors des plans.

### Cluster enfant sans Internet

- **Sorte de paquet** `rke2` (point 237) : `version` RKE2 exacte
  (`v1.34.11+rke2r1`), `files: rke2/<version>/{rke2.linux-amd64.tar.gz,
  sha256sum-amd64.txt, install.sh}`, `images` = liste des images système de
  cette version (`rke2-images-all.linux-amd64.txt` filtrée par la CNI
  choisie). `harvester-artefacts pack --rke2 VERSION [--cni calico]` les
  récupère côté connecté.
- **Publication** : chaque fichier en blob du dépôt
  `<registre>/harvester-ops/rke2`, manifeste d'artefact étiqueté `<version>`
  (type `application/vnd.harvester-ops.rke2.v1+json`). Tâche 8 : la grammaire
  des étiquettes OCI interdit le `+` ; l'étiquette est la version au `+`
  remplacé par `_` (`v1.34.11_rke2r1`). `/opt/install.sh` n'est posé qu'après
  la vérification des trois empreintes (récupéré sous un autre nom) et
  CAPRKE2 le lance lui-même (`airGapped`).
- **Option du cluster enfant** `offline: true` : exige des registres dont le
  miroir de `docker.io` est sans repli (`no_fallback`) et l'élément `rke2`
  de la version du modèle publié dans l'un d'eux (`offline-rke2-missing`,
  `offline-rke2-version-mismatch`, bloquants). Dans les deux gabarits :
  fichier `/etc/rancher/rke2/hops-fetch.netrc` (0600, compte de lecture,
  depuis un Secret, `contentFrom`), `preRKE2Commands` qui récupèrent les
  trois blobs par empreinte avec `curl --netrc-file ... --cacert ...` dans
  `/opt/rke2-artifacts` et `/opt/install.sh`, vérifient le sha256, puis
  `agentConfig.airGapped: true`. Une image de VM qui porte déjà ces fichiers
  est acceptée (option `offline_artifacts: image`, aucune récupération).

### Communes

- **Rôles** : lecture viewer ; importer, retirer un import, poser ou retirer
  un élément GitOps, renouveler = opérateur ET droit d'administrer le
  cluster enfant (`child-admin-required`) ET, pour Rancher, session Rancher ;
  `privileges: cluster`, `renew-cert`, `harvester-nodes` = administrateur.
- **Toute route qui écrit** : `@requires_auth`, `@_rate_limit` (limite
  contrôlée par `limits.parse_many`), action suivie créée avant tout geste,
  outil de `bin/` (parité), identité de la personne.
- **Bibliothèque standard** pour `rancher_import`, `child_oidc`,
  `child_gitops`, `flux_artifact`, `capi_renew`, `rke2_offline` (preuve par
  `python3 -I -S`) ; PyYAML là où il l'est déjà.
- **Textes publics** : ni tiret cadratin ni flèche Unicode ; cinq langues ;
  info-bulle sur chaque contrôle.

## Constats préalables (lus dans le code le 07/11/2026)

- **L'option d'import existante ne vise pas un Rancher externe.**
  `capi_cluster.postprocess` pose l'étiquette
  `cluster-api.cattle.io/rancher-auto-import` sur le `Cluster` quand
  `rancher_import` est vrai ; Turtles l'importe alors dans le Rancher où il
  tourne, c'est-à-dire le Rancher intégré à Harvester 1.9 (la bulle
  `capi.new.t.rancher` le dit). La documentation de Turtles confirme que
  l'étiquette importe dans le Rancher qui porte les objets Cluster API.
  L'import dans un Rancher externe demande donc l'import générique de
  Rancher (tâche 2), pas l'étiquette.
- `caphv-generate` sait poser un `GitRepo` Fleet pour la CNI et le CSI
  (`--fleet-addon-repo`, mode Fleet par CAAPF) : il vise le Fleet du Rancher
  qui porte Cluster API, pas un Rancher externe. La console ne l'emploie pas
  et ne le propose pas pour le GitOps des applications.
- `artefacts.LATER_KINDS = ("gitops",)`, `check_index` accepte déjà
  `files: manifests/<élément>/...` pour `gitops` et marque l'élément `later` ;
  `deploy_plans.ARTEFACT_REFUSED_KINDS["gitops"] = "coming in 2.8"`.
- `capi_manifests.is_system_namespace` refuse tout `*-system` (motifs de
  `hv_containers.SYSTEM_NAMESPACES`) : `flux-system` est refusé à un
  manifeste. D'où `hops-gitops` pour les objets de la console, et le chart
  de Flux par CAAPH (aucune règle de namespace sur la release,
  `capi_services`). Le chart porte des CRD, que les manifestes refusent :
  Flux ne peut venir que par un chart (conception : « chart par CAAPH »).
- `capi_manifests.docs` ne produit qu'un ConfigMap par CRS ; un Secret de
  type `addons.cluster.x-k8s.io/resource-set` est l'extension nécessaire
  pour livrer un identifiant sans l'écrire en clair sur le cluster Harvester.
- `child_policies` lie des groupes à `admin`, `edit`, `view` seulement, sans
  préfixe de groupe : un groupe OIDC préfixé (`oidc:equipe-a`) doit être
  écrit tel quel par la personne aujourd'hui ; la 2.8.0 propose le préfixe
  du cluster.
- Le moteur de plans connaît `ORDER = ("install", "readiness", "terraform",
  "registry", "capi", "clusters", "artefacts")`, `COMING = ()` ;
  `deploy_undo.KINDS_ORDER = ("chart", "manifest", "policies",
  "capi-cluster", "registry", "tf-declaration")`.
- Les gabarits RKE2 viennent de `caphv-generate` (une `ClusterClass` par
  namespace). `RKE2ControlPlaneTemplate.spec.template.spec.serverConfig`
  n'a que `cni` et `cloudProviderName` ; `_apply_registries` ajoute des
  `files` aux deux gabarits à la création seulement.
- `registry_seed.make_tls` crée la CA dans un répertoire de travail effacé :
  la clé n'existe plus après le déploiement (m-9 de la revue 4-8 de la
  2.6.0). `registries.rke2_config` écrit le mot de passe de lecture dans
  `<c>-registries` et encore dans `<c>-registry-<r>-auth` (m-14 de la revue
  finale 2.6.0).
- `hv_settings` connaît `containerd-registry` (masquage des mots de passe) ;
  `hv_forklift.registry_auth_secret` a relevé en 1.9 que le réglage perd son
  `Auth` et que les identifiants vont dans un Secret de `fleet-local`.
- Les écritures dans Rancher passent aujourd'hui par le jeton de session de
  la personne (`_session_rancher`, `_rancher_get`, routes Projets et
  Membres) ; un compte local reçoit `rancher-session-needed`. Les
  identifiants d'un administrateur de Rancher ne sont demandés que pour un
  geste d'enregistrement (OIDCClient, chart RBAC) et jamais gardés
  (`rancher_admin.AdminSession`).
- Faits amont (sources en fin de plan) : Flux 2.10.0 (26/10/2026) est la
  dernière version, le chart communautaire `flux2` 2.20.0 la porte
  (Apache-2.0) ; Flux Operator est sous AGPL-3.0 ; l'installation airgap de
  Flux recopie les images des contrôleurs dans un registre privé et pointe
  les manifestes dessus ; Rancher 2.14 embarque Fleet 0.15.x ; `HelmOp` n'est
  plus expérimental depuis Fleet 0.13 et tire les charts depuis les clusters
  enfants ; RKE2 passe des arguments à l'API par `kube-apiserver-arg` et
  monte des fichiers par `kube-apiserver-extra-mount` ; la configuration
  structurée (`--authentication-config`) exclut les drapeaux `--oidc-*`.

## Tâche 1 : essais préalables sur les bancs (avant tout code)

Fichiers : aucun dans le dépôt ; compte rendu ajouté en fin de ce plan
(section « Essais préalables »), au même commit que la tâche 2.

Sur harvlab2 (pile Cluster API de la 2.7.0, registre d'amorçage
172.16.2.76, un cluster enfant 1+1 `rke2-banc` créé pour l'occasion) et le
Rancher externe (VM `rancher-manager` sur harv1, démarrée pour l'essai,
arrêtée après) ; chaque essai remis en état :

1. **Import générique dans Rancher** (écrit dans Rancher, retiré à la fin) :
   version de Rancher relevée (`/rancherversion`) ; `POST
   /v1/provisioning.cattle.io.clusters` avec le jeton d'une session de
   console ; délai avant `status.clusterName` ; forme de
   `/v3/clusterregistrationtokens?clusterId=` (jeton substitué ou non) ;
   images du manifeste ; application dans `rke2-banc` par le kubeconfig de
   `child_access` ; délai jusqu'à `Active` ; fenêtre Membres de la console
   sur ce cluster.
2. **Retrait de l'import** : suppression du cluster de provisionnement ;
   ce que Rancher nettoie lui-même dans le cluster enfant ; liste des restes
   (namespaces, CRB, CRD, webhooks) ; durée.
3. **Fleet** : nom et étiquettes du cluster Fleet d'un cluster importé ;
   `GitRepo` depuis un dépôt Gitea temporaire (SSH 172.16.1.11:2222, clé de
   déploiement en lecture) vers ce cluster ; `HelmOp` du chart podinfo publié
   dans 172.16.2.76 (tiré par le cluster enfant, `helmSecretName`) ; HelmOp
   disponible sans drapeau en Fleet 0.15 ?
4. **Flux** : chart `flux2` 2.20.0 et ses images dans un paquet de banc,
   publiés, installés par `service-deploy` ; noms des valeurs pour les
   arguments des contrôleurs ; artefact Flux construit et poussé par un
   script de banc en bibliothèque standard, accepté par `OCIRepository`
   (types de média) ; CRS des objets Flux appliqué après le chart ; CRS
   appliqué AVANT le chart (CRD absentes) : réessai ou échec définitif ?
   Secret `addons.cluster.x-k8s.io/resource-set` dans un CRS : appliqué tel
   quel ? `--no-cross-namespace-refs` et compte par défaut : objet hors du
   namespace visé refusé.
5. **OIDC** : Dex temporaire sur node1 (voir Préparatifs) ; cluster enfant
   créé avec le fichier, l'argument et le montage dans le gabarit du plan de
   contrôle (rendus à la main) ; `apiVersion` servie (`v1` sur 1.34) ; sens
   de `extraMounts` ; jeton obtenu par l'octroi par mot de passe de Dex,
   `kubectl --token` : utilisateur et groupe préfixés vus par
   `SelfSubjectReview`, refus d'une autre audience, refus après expiration.
6. **Rotation des gabarits** : copie `-r1` d'un `RKE2ConfigTemplate` et du
   `RKE2ControlPlaneTemplate` (fichier ajouté), `ClusterClass` repointée :
   renouvellement déclenché ? ordre, durée, disponibilité de l'API pendant
   (sonde toutes les 5 s), adresse supplémentaire prise et rendue ; le
   webhook refuse-t-il une modification EN PLACE d'un gabarit (preuve que la
   rotation est nécessaire) ?
7. **Sans Internet** : règle nftables temporaire de node2 (celle de la
   2.6.0) qui coupe Internet au réseau des VMs du banc ; RKE2 et ses images
   système publiés dans 172.16.2.76 ; cluster créé avec la récupération par
   `curl` puis `airGapped: true` (rendus à la main) ; présence de `curl` dans
   l'image Leap 15.6 ; tailles relevées (archive RKE2, images système de la
   CNI, chart et images de Flux, images de l'agent Rancher).
8. **`containerd-registry`** sur harvlab2 : fusion d'un miroir vers
   172.16.2.76 ; effet (redémarrage de RKE2 de Harvester, durée
   d'indisponibilité de l'API) ; image tirée par `crictl` sur le nœud depuis
   le miroir ; retour à la valeur d'origine.

Critère de passage : 1, 4, 5 et 6 réussis. Un échec de 1 (manifeste
inapplicable par la console) est soumis avant de coder : l'import passerait
par un CRS à ressource Secret, autre conception. Un échec de 6 (aucun
renouvellement par rotation) renvoie le renouvellement à une conception à
part. Un échec de 7 détache la tâche 8 en 2.8.1 (point 238).

## Tâche 2 : import dans un Rancher externe (`rancher_import`)

Fichiers : nouveau `bin/lib/rancher_import.py` ; `bin/lib/capi_cluster.py`
(option `rancher_external`, conflit avec `rancher_import`, annotation) ;
`bin/harvester-capi.py` (`rancher-import`, `rancher-unimport`,
`rancher-status` ; `create` enchaîne l'import quand l'option est posée ;
`delete` retire l'import avant le cluster) ; `web/app.py` (routes `POST
/api/capi/<cluster>/rancher-import`, `DELETE
/api/capi/<cluster>/rancher-import/<namespace>/<name>`, `GET
/api/capi/<cluster>/rancher-status`) ; nouveaux
`tests/api/test_rancher_import_280.py`,
`tests/api/test_capi_rancher_import_cli_280.py`,
`tests/api/test_capi_rancher_routes_280.py`.

Interfaces :

```
rancher_import.rancher_name(harvester, ns, cluster) -> str        # 63 car., empreinte au-delà
rancher_import.cluster_body(name, harvester, ns, cluster, by, run=None) -> dict
rancher_import.registration_manifest_url(tokens_list) -> str      # forme /v3, jeton substitué ; RancherImportError sinon
rancher_import.manifest_images(text) -> [str]                     # images des Deployment/DaemonSet/Job
rancher_import.leftovers(objs) -> [(kind, ns, name)]              # restes connus, constante relevée en tâche 1
rancher_import.mark(server_id, name, cid) -> str ; parse_mark(text) -> dict | None
rancher_import.ours(prov_cluster, harvester, ns, cluster, expected_run=None) -> bool
```

`harvester-capi rancher-import --name NS/C [--timeout 900] [--dry-run]` :
lit la session Rancher (`HARVESTER_OPS_KUBECONFIG`, `rancher_of`), refuse
sans session (code 2, `rancher-session-needed`), contrôle les droits dans
Rancher (schéma de la collection : `POST` permis), crée le cluster de
provisionnement (ou le reprend s'il porte les étiquettes de ce cluster
enfant ; s'il porte autre chose : `rancher-name-taken`), attend
`status.clusterName`, lit le manifeste, sonde les images, l'applique par
`child_access.session`, pose l'annotation, attend `Active`. Étapes :
`session`, `create`, `token`, `images`, `apply`, `wait`. Le jeton et le
manifeste n'apparaissent dans aucune étape.

Tests (tableaux paramétrés, un cas par chemin d'erreur) :

| Cas | Attendu |
|---|---|
| nom court, namespace égal au nom | `<h>-<c>` |
| nom long (namespace différent, 70 car.) | 63 car., suffixe d'empreinte stable |
| `rancher_import` et `rancher_external` ensemble | `rancher-import-conflict` bloquant |
| compte local | 2, `rancher-session-needed`, aucun appel à Rancher |
| droit de création absent | `rancher-forbidden`, rien créé |
| cluster de même nom sans nos étiquettes | `rancher-name-taken` |
| cluster de même nom à nous | repris, pas recréé |
| jeton 2.15 (`{token}` littéral) lu par `/v3` | adresse substituée |
| adresse encore à `{token}` | refus `rancher-token-unreadable`, rien appliqué |
| manifeste de plus de 10 Mio | refus `rancher-manifest-too-big` |
| image de l'agent absente, repli coupé | `rancher-agent-image-missing` bloquant |
| jeton cherché dans `actions.db`, le journal, les étapes, stderr | absent |
| fichier du manifeste après succès, après échec | effacé |
| retrait : namespace étranger dans les restes | gardé, nommé `kept-in-use` |
| retrait : marque d'une autre exécution | `undo-not-ours` |

Mutations : « jeton du manifeste consigné dans l'étape », « fichier 0600
non effacé dans le finally », « conflit avec Turtles ignoré », « reprise
d'un cluster Rancher étranger », « restes supprimés sans dry-run serveur ».

## Tâche 3 : OIDC de l'API (`child_oidc`)

Fichiers : nouveau `bin/lib/child_oidc.py` ; `bin/lib/capi_cluster.py`
(option `oidc`, `postprocess` : fichier, arguments, montage dans le seul
gabarit du plan de contrôle) ; `bin/lib/child_policies.py` (groupe
administrateur, préfixe de groupe proposé) ; `bin/harvester-capi.py`
(`oidc-kubeconfig --name NS/C`) ; `web/app.py` (`GET
/api/capi/<cluster>/oidc-kubeconfig/<namespace>/<name>`) ; nouveaux
`tests/api/test_child_oidc_280.py`, `tests/api/test_capi_oidc_render_280.py`,
`tests/api/test_capi_oidc_routes_280.py`.

Interfaces :

```
child_oidc.check(cfg, k8s_version) -> (cfg normalisé, [finding])
    # codes : oidc-issuer-invalid, oidc-audience-invalid, oidc-prefix-required,
    # oidc-prefix-system, oidc-ca-unreadable, oidc-version-too-old,
    # oidc-admin-group-invalid, oidc-with-rancher (avertissement)
child_oidc.auth_config(cfg, k8s_version) -> dict                 # AuthenticationConfiguration
child_oidc.template_patch(cfg, k8s_version) -> {"files": [...], "extraArgs": [...], "extraMounts": {...}}
child_oidc.kubeconfig(cfg, server, ca_b64, cluster) -> str       # greffon exec, aucun identifiant
child_policies.admin_binding(groups_prefix, admin_group) -> dict # seule CRB vers cluster-admin
```

`postprocess` ajoute le patch au `RKE2ControlPlaneTemplate` seulement
(jamais aux workers : l'API n'y tourne pas), sans écraser un argument ou un
montage déjà présent (refus `oidc-arg-conflict`). Le contrôle du modèle de
cluster (`check`) rejoue `child_oidc.check` ; un plan rejoue la même chose
dans la phase Clusters.

Tests :

| Cas | Attendu |
|---|---|
| `http://` | `oidc-issuer-invalid` |
| adresse avec identifiants ou requête | `oidc-issuer-invalid` |
| préfixe vide | `oidc-prefix-required` |
| préfixe `system:x` | `oidc-prefix-system` |
| CA illisible | `oidc-ca-unreadable` |
| version 1.29 / 1.31 / 1.34 | refus / `v1beta1` / `v1` |
| rendu : gabarit worker | inchangé |
| argument `authentication-config` déjà présent | `oidc-arg-conflict` |
| fichier rendu | 0600, root, YAML relu égal à `auth_config` |
| kubeconfig rendu | aucune clé `token`, `password`, `client-key-data` |
| `admin_group` absent | aucune CRB |
| CRB vers `cluster-admin` dans un paquet | toujours `manifest-crb-admin` |

Mutations : « préfixe vide accepté », « fichier posé sur les workers »,
« drapeaux --oidc-* ajoutés en plus », « CRB admin admise depuis un
paquet ».

## Tâche 4 : GitOps (`child_gitops`, `flux_artifact`)

Fichiers : nouveaux `bin/lib/child_gitops.py`, `bin/lib/flux_artifact.py` ;
`bin/lib/artefacts.py` (`gitops` dans `KINDS`) ; `bin/lib/capi_manifests.py`
(ressource Secret `resource-set` : `docs(..., secrets=[...])`, état du
binding sur les deux ressources, retrait) ; `bin/harvester-artefacts.py`
(publication d'un élément `gitops` = artefact Flux) ;
`bin/harvester-capi.py` (`gitops-check`, `gitops-deploy`, `gitops-remove`,
`gitops-status`, `gitops`) ; `web/app.py` (routes `POST
/api/capi/<cluster>/gitops`, `DELETE
/api/capi/<cluster>/gitops/<namespace>/<name>/<entry>`, `GET
/api/capi/<cluster>/gitops`) ; nouveaux `tests/api/test_child_gitops_280.py`,
`tests/api/test_flux_artifact_280.py`,
`tests/api/test_capi_gitops_cli_280.py`,
`tests/api/test_capi_gitops_routes_280.py`,
`tests/api/test_capi_manifests_secret_280.py`.

Interfaces :

```
child_gitops.choose(tool, imported) -> "flux" | "fleet"            # gitops-fleet-needs-rancher
child_gitops.check(entry, facts) -> [finding]
    # gitops-source-unsupported, gitops-flux-missing (chart flux absent ou non prêt),
    # gitops-namespace-not-team, gitops-privileges-admin-only, gitops-name-too-long,
    # gitops-secret-missing, gitops-package-not-published
child_gitops.flux_objects(entry, source_url, secrets) -> ([objet], [secret])
child_gitops.fleet_object(entry, fleet_cluster, harvester) -> dict  # GitRepo ou HelmOp
child_gitops.state_flux(source, kustomization, revision) -> {"ready", "revision", "message"}
child_gitops.state_fleet(obj) -> {"ready", "message"}
flux_artifact.build(files, item, version) -> (layer_bytes, config_bytes, manifest_bytes)
flux_artifact.push(registry, repo, tag, built) -> digest
```

`gitops-deploy --spec F` (spec : `cluster: <ns>/<c>`, `entry` du plan ou du
geste, `origin` facultatif, secrets par l'entrée standard) : choisit
l'outil ; Flux : vérifie le service `flux` prêt (sinon refus, l'installer
d'abord par le chart du paquet), pose `hops-gitops`, livre objets et
Secrets par `capi_manifests` (`art-gitops-<nom>`), attend `Ready` ; Fleet :
écrit l'objet dans Rancher par la session, attend le résumé. Étapes :
`check`, `apply`, `wait`.

Tests :

| Cas | Attendu |
|---|---|
| `auto` importé / non importé | `fleet` / `flux` |
| `fleet` sans import | `gitops-fleet-needs-rancher` |
| Fleet + source `oci` | `gitops-source-unsupported` |
| Flux + source `chart` | `gitops-source-unsupported` |
| Flux non installé | `gitops-flux-missing` |
| `targetNamespace` système (`kube-system`) | `gitops-namespace-not-team` |
| `privileges: cluster` par un opérateur | `gitops-privileges-admin-only` |
| artefact construit deux fois | octets identiques (déterministe) |
| objet refusé par `capi_manifests.check` dans l'élément gitops | construction refusée |
| identifiants : ConfigMap rendu | ne contient ni mot de passe ni clé |
| identifiants : Secret `resource-set` | contient le dockerconfigjson |
| retrait Flux | CRS, ConfigMap, Secret puis objets dans le cluster enfant |
| retrait Fleet | objet supprimé dans Rancher, marque relue avant |

Mutations : « mot de passe écrit dans le ConfigMap », « fleet choisi sans
import », « verrouillage multi-locataire retiré des valeurs », « archive
non déterministe (mtime réel) », « privileges cluster admis pour un
opérateur ».

## Tâche 5 : phase GitOps des plans, options d'accès des modèles, Défaire

Fichiers : `bin/lib/deploy_plans.py` (phase `gitops` : format, contrôle,
rendu des clusters et des sources, refus `gitops` retiré de
`ARTEFACT_REFUSED_KINDS` ; modèles de cluster : `rancher_external`, `oidc`,
`offline` admis) ; `bin/lib/deploy_chain.py` (`ORDER` + `gitops`) ;
`bin/lib/deploy_undo.py` (sortes `gitops` et `rancher-import`, ordre) ;
`web/app.py` (`_deploy_gitops_phase`, `_deploy_gitops_prepare`, import dans
`_deploy_clusters_phase` après la création, marques `undo`) ;
`bin/harvester-plans.py` (rien de nouveau : `undo` et `resume` couvrent les
sortes) ; nouveaux `tests/api/test_deploy_plans_280.py`,
`tests/api/test_deploy_gitops_280.py`, `tests/api/test_deploy_undo_280.py`.

Format de la phase :

```yaml
gitops:
  enabled: true
  timeout_minutes: 15
  entries:
    - name: apps
      tool: auto
      clusters: ["{{cluster}}-a"]
      target_namespace: equipe-a
      source: {type: oci, package: banc-280, version: 1.0.0, item: apps, registry: amorcage}
    - name: infra
      tool: auto
      clusters: ["{{cluster}}-b"]
      target_namespace: equipe-b
      source: {type: git, url: "ssh://git@git.client/infra.git", branch: main, path: ./site, secret: git-infra}
```

Ordre (point 223) : l'import et l'OIDC sont des options du cluster (phase
Clusters : création, puis import) ; la phase GitOps vient après Artefacts
(le chart de Flux est un artefact). Dépendances : un élément GitOps dépend
de `clusters:<c>` et, pour Flux, de l'élément chart `flux` du même cluster
s'il est dans le plan. **Défaire** : ordre `gitops`, artefacts,
`rancher-import`, clusters, registre, Terraform (`KINDS_ORDER` complété) ;
l'import est retiré AVANT la suppression du cluster (sinon Rancher garde un
cluster mort). Secrets Git d'une entrée : secrets du plan, redemandés à la
reprise (décision 129).

Tests : format (clé inconnue, source de chaque type, nom trop long, 17
entrées, 65 éléments dépliés), rendu des variables, dépendances
(Flux du plan, Flux déjà présent, Flux absent : constat), reprise (élément
vérifié et sauté par `gitops-status`), défaire (ordre, import avant
cluster, élément d'une autre exécution gardé), plan de 2.7 portant une
phase `gitops` inexistante (avertissement, clé ignorée). Mutations :
« import retiré après le cluster », « dépendance au chart flux ignorée »,
« secret Git rangé dans le résultat ».

## Tâche 6 : renouvellement des machines (`capi_renew`)

Fichiers : nouveau `bin/lib/capi_renew.py` ; `bin/harvester-capi.py`
(`renew`) ; `bin/lib/capi_cluster.py` (rendu des seuls gabarits RKE2 d'une
demande, réutilisé) ; `web/app.py` (route `POST
/api/capi/<cluster>/renew/<namespace>/<name>`, action
`capi-renew:<ns>/<c>`) ; nouveaux `tests/api/test_capi_renew_280.py`,
`tests/api/test_capi_renew_cli_280.py`.

Interfaces :

```
capi_renew.current(kube, ns, cluster) -> {"class", "templates": {...}, "revision"}
capi_renew.diff(current_docs, wanted_docs) -> [{"template", "paths": [...]}]   # vide = rien à faire
capi_renew.rotated(docs, revision) -> [doc]                                    # -r<N>, annotation
capi_renew.class_patch(cls, names) -> dict                                     # merge patch de la ClusterClass
capi_renew.progress(machines, wanted_templates) -> {"done", "old", "new", "message"}
```

`renew --what registries` recalcule `registries.rke2_config` d'après la
nouvelle liste du cluster (Secrets de registre mis à jour d'abord, anciens
retirés après), `--what oidc` d'après `child_oidc`, `--what ca` pose la CA
en transition. `--dry-run` rend la différence sans écrire. Refus :
`renew-nothing` (différence vide), `renew-plan-running`, `renew-busy`
(renouvellement déjà en cours : machines de deux révisions), capacité et
adresse pour la machine en plus (constats de la 2.5.0 rejoués).

Tests (tableaux) : différence vide, fichier ajouté, argument ajouté,
ClusterClass repointée sur les deux gabarits, révision suivante (r1 puis
r2), anciens gabarits gardés tant qu'une machine s'y rapporte, retirés
après, délai dépassé (erreur, rien retiré), Secrets de registre : nouveaux
avant, anciens après. Mutations : « gabarit modifié en place », « anciens
gabarits supprimés avant la fin », « Secrets anciens retirés avant la
bascule ».

## Tâche 7 : registres achevés (m-14, certificat, nœuds Harvester)

Fichiers : `bin/lib/registries.py` (m-14 : marque par étiquette, lecture des
deux formes ; CA en transition ; fusion `containerd-registry`) ;
`bin/lib/registry_seed.py` (clé de CA gardée, `nameConstraints`,
`renew_cert`) ; `bin/harvester-registry.py` (`renew-cert`,
`harvester-nodes`) ; `web/app.py` (routes `POST
/api/registries/<name>/renew-cert`, `POST
/api/registries/<name>/harvester-nodes`, constats
`registry-ca-key-missing` (information) et `registry-ca-transition-pending`)
; nouveaux `tests/api/test_registries_280.py`,
`tests/api/test_registry_seed_280.py`,
`tests/api/test_registry_nodes_280.py`.

Tests :

| Cas | Attendu |
|---|---|
| cluster créé en 2.7 (`-auth` avec données) | lu ; retiré au renouvellement |
| cluster créé en 2.8 | aucun mot de passe hors de `<c>-registries` |
| `child_users` | même résultat sur les deux formes |
| registre posé en 2.8 | Secret `<r>-ca-key` présent, CA avec `nameConstraints` critique |
| `renew-cert` avec clé | nouveau `notAfter`, même CA, aucun gabarit touché |
| `renew-cert` sans clé, sans `--new-ca` | refus `registry-ca-key-missing` |
| `--new-ca` puis bascule avant les renouvellements | `registry-ca-transition-pending` |
| `harvester-nodes` : autres miroirs présents | gardés |
| `harvester-nodes --remove` | seule l'entrée du registre retirée |
| mot de passe dans le résultat de l'action | masqué |

Mutations : « mot de passe encore copié dans -auth », « CA sans
contraintes de nom », « réglage écrasé au lieu d'être fusionné », « bascule
sans transition ».

## Tâche 8 : cluster enfant sans Internet (`rke2_offline`)

Fichiers : nouveau `bin/lib/rke2_offline.py` ; `bin/lib/artefacts.py`
(sorte `rke2`) ; `bin/harvester-artefacts.py` (`pack --rke2 VERSION [--cni
C]`, publication des blobs) ; `bin/lib/capi_cluster.py` (option `offline`,
`offline_artifacts`) ; `bin/harvester-capi.py` (`check` : constats) ;
nouveaux `tests/api/test_rke2_offline_280.py`,
`tests/api/test_artefacts_rke2_280.py`.

Interfaces :

```
rke2_offline.check_item(entry) -> [erreur]                    # version exacte, trois fichiers, images
rke2_offline.pre_commands(registry_host, repo, digests, ca_path) -> [str]
rke2_offline.netrc(host, user, password) -> str               # rendu dans un Secret, jamais en argv
rke2_offline.findings(spec, published, entries) -> [finding]  # offline-rke2-missing, -version-mismatch, offline-needs-no-fallback
```

Les commandes ne portent aucun identifiant (fichier netrc 0600 venu d'un
Secret par `contentFrom`) et vérifient chaque empreinte avant
`install.sh`. Tests : commandes rendues (aucun mot de passe, `--fail`,
vérification sha256 avant exécution), version différente du modèle,
miroir avec repli, élément absent, mode `image` (aucune commande). Mutations :
« mot de passe dans preRKE2Commands », « install.sh exécuté avant la
vérification », « repli vers Internet admis en mode sans Internet ».

## Tâche 9 : interface

Fichiers : `web/static/js/capi-create.js` (section **Accès** : aucun,
Rancher intégré (Turtles, déconseillé), Rancher externe (grisé sans session
Rancher, bulle qui le dit) ; OIDC : émetteur, audience, revendications,
préfixes, CA, groupe administrateur ; case **Sans Internet**),
`web/static/js/capi.js` (cluster enfant : **Renouveler** (fenêtre
Paramètres, Différence, Confirmation), **Accès** (état de l'import, bouton
Importer ou Retirer, Télécharger le kubeconfig OIDC), lien vers Membres),
nouveau `web/static/js/capi-gitops.js` (onglet **GitOps** de Cluster API :
éléments par cluster, outil, source, état, Poser, Retirer),
`web/static/js/bm-plans.js` (carte **GitOps** ; options d'accès dans les
modèles de cluster ; constats et résumé), `web/static/js/registries.js`
(**Renouveler le certificat**, **Nœuds Harvester**, échéance du
certificat), `web/static/js/artefacts.js` (sortes `gitops` et `rke2`),
`web/static/js/rancher-members.js` (cluster enfant importé),
`web/static/js/i18n.js` (cinq langues), `web/templates/index.html` (script
ajouté) ; e2e nouveaux `tests/e2e/test_capi_access_280.py`,
`tests/e2e/test_capi_gitops_280.py`, `tests/e2e/test_capi_renew_280.py`,
`tests/e2e/test_registries_280.py`, `tests/e2e/test_bm_plans_gitops_280.py` ;
mises à jour de `tests/api/test_nav_inventory_200.py` et des listes
d'onglets (onglet GitOps ajouté).

Règles : fenêtres overlay en onglets comme Déployer ; boutons qui écrivent
grisés tant que la vérification n'est pas revenue ; tout passe par
`escapeHtml` ; SSE par `SSEReconnect.connect()` ; info-bulle sur chaque
contrôle. Tests e2e sur routes simulées : Rancher externe grisé sans
session, conflit Turtles signalé, préfixe vide refusé à la saisie, kubeconfig
OIDC téléchargé sans identifiant, carte GitOps (outil déduit affiché),
Renouveler grisé sans différence, noms hostiles échappés. Mutations :
« Rancher externe actif sans session », « échappement retiré », « bouton
Renouveler actif sans différence ».

## Tâche 10 : essais réels, documentation, version

Fichiers : `VERSION` (2.8.0), `CHANGELOG.md`, nouveaux `docs/en/gitops.md`
et `docs/fr/gitops.md`, `docs/en/artefacts.md` et `docs/fr/artefacts.md`
(sortes `gitops` et `rke2`, « Not in 2.8.0 »), `docs/en/bare-metal.md` et
`docs/fr/bare-metal.md` (phase GitOps, options d'accès), les sections
Cluster API de `docs/en/operating-procedure.md` et
`docs/fr/procedure-operationnelle.md` (import, OIDC, renouvellement ; il
n'existe pas de page Cluster API à part), `docs/en/architecture.md` et son
pendant français (modules nouveaux), `docs/en/troubleshooting.md` et
`docs/fr/depannage.md` (agent Rancher sans image, Kustomization en échec,
renouvellement bloqué, CA en transition), `docs/en/capabilities.md` et
`docs/fr/capabilites.md` (rôles), `tools/parity/data.py` puis `python3
tools/parity/gen.py --docs`, mémoire du projet. Suites complètes avant le
commit de version : `python3 -m pytest tests/api/ -q -n 12` et `python3 -m
pytest tests/e2e/ -q -n 8`, sans échec ni test instable.

### Bancs

| Banc | État | Sert à | Ne sert pas à |
|---|---|---|---|
| harvlab2 | v1.9.0, 1 nœud imbriqué, 32 Gio, pile Cluster API de la 2.7.0, registre d'amorçage 172.16.2.76, éteint | toute la preuve avec un cluster enfant 1+1 | deux clusters enfants à la fois |
| Rancher externe | VM `rancher-manager` sur harv1 (4 vCPU, 16 Gio), ARRÊTÉE, harv1 allumé | import, Membres, Fleet ; démarrée à la demande, arrêtée après | rien d'autre |
| harv4 | v1.9.0, bare-metal, 128 Gio, éteint | R12 : deux clusters dans une exécution, un importé (Fleet), un non (Flux) | rien de nécessaire à la preuve centrale |
| node1 | console de dev, Gitea | Dex temporaire (OIDC), dépôt Gitea temporaire (Fleet et Flux Git) | |

### Préparatifs (règles du homelab)

- node2 allumé par son iLO (172.16.1.32) ; harvlab2 démarré
  (`HARVLAB_NAME=harvlab2 HARVLAB_NODES=1 harvlab.sh start`) ;
  `harvester-capi status` prêt aux versions du paquet.
- Adresses : registre 172.16.2.76 (réservée en 2.6.0) ; clusters enfants
  dans le pool 172.16.2.200-205 du banc ; aucune adresse nouvelle (toute
  adresse ajoutée est enregistrée dans NetBox et Pi-hole avant usage).
- `storage-minimal-available-percentage` de 25 à 15 pendant les créations,
  remis à 25 (comme en 2.6.0 et 2.7.0).
- **Rancher externe** : VM `default/rancher-manager` de harv1 démarrée par la
  sous-ressource `start` ; **l'essai écrit dans Rancher** (cluster de
  provisionnement, jeton d'enregistrement, `GitRepo` et `HelmOp` dans
  `fleet-default`, agents dans le cluster enfant) ; tout est retiré à la fin
  (R5, R6), vérifié par `kubectl get clusters.provisioning.cattle.io -n
  fleet-default` et `gitrepos,helmops -n fleet-default` sur le cluster
  `local` ; VM arrêtée après, état `Stopped` relu.
- **Dex** sur node1 : conteneur podman sans privilèges, image épinglée,
  `--memory 128m`, CA et certificat de banc générés dans l'espace de travail,
  deux utilisateurs et deux groupes, mots de passe générés dans Vault
  `secret/infra/harvops-oidc-test` (jamais affichés) ; port 5556 ouvert à
  chaud seulement (`firewall-cmd --zone=public --add-port=5556/tcp`, jamais
  `--reload`), refermé et conteneur supprimé à la fin.
- **Gitea** : dépôt temporaire `jniedergang/hops-gitops-banc`, clé de
  déploiement en lecture (clé générée dans l'espace de travail) ; dépôt
  supprimé à la fin. C'est une écriture dans Gitea, dite ici.
- Paquet `banc-280` 1.0.0 construit sur node1 par `harvester-artefacts
  pack` (chart `flux2` 2.20.0 et ses images, élément `gitops` de deux objets,
  élément `rke2` de la version du modèle, chart podinfo), signé par la clé de
  test de Vault `secret/infra/harvops-artefacts-test` ; une version 1.0.1 dont
  l'élément `gitops` a un objet de moins.
- Règle nftables temporaire de node2 (celle de la 2.6.0) pour R11 seulement,
  retirée après.
- Après chaque essai : rien ne reste qui n'y était pas ; `harvlab.sh tidy` ;
  à la fin `harvlab.sh stop`, `sudo systemctl poweroff` de node2,
  `PowerState: Off` lu par l'iLO.

### Déroulé

| # | Vérification | Banc et ressources | Vérifié côté cluster par |
|---|---|---|---|
| R1 | Cluster 1+1 créé avec OIDC vers Dex et groupe administrateur ; jeton d'un membre du groupe : `cluster-admin` ; jeton d'un autre : seulement ce que le socle donne à son groupe ; autre audience refusée ; kubeconfig OIDC remis : aucun identifiant | harvlab2, Dex | `kubectl auth whoami` (`SelfSubjectReview`), `kubectl auth can-i --list` avec chaque jeton |
| R2 | Renouveler `--what oidc` (groupe administrateur changé) : machines remplacées, API joignable pendant (sonde 5 s), anciens gabarits retirés | même cluster | `kubectl get machines`, gabarits `-r1`, nouvelle CRB |
| R3 | Flux par plan : phase Artefacts (chart `flux`), phase GitOps (source OCI du paquet publiée dans 172.16.2.76) ; passage à 1.0.1 : objet retiré élagué ; objet hors du namespace visé refusé | même cluster | `kubectl get ocirepository,kustomization -n hops-gitops`, objets dans `equipe-a` |
| R4 | Flux Git : dépôt Gitea temporaire par SSH, secret du plan ; reprise après arrêt pendant la phase : secret redemandé, élément vérifié et sauté | même cluster, Gitea | `kubectl get gitrepository`, journal de la reprise |
| R5 | Import dans le Rancher externe (session Rancher de la console de dev) : `Active`, Membres affichés, puis retrait : cluster absent de Rancher, restes nettoyés dans le cluster enfant | harvlab2 + VM `rancher-manager` démarrée | Rancher `kubectl get clusters.provisioning.cattle.io -n fleet-default`, enfant `kubectl get ns` |
| R6 | Fleet : `GitRepo` depuis Gitea et `HelmOp` du chart podinfo publié ; outil `auto` choisi `fleet` ; puis Défaire de l'exécution : GitOps, artefacts, import, cluster, dans cet ordre | même | `gitrepos,helmops -n fleet-default` ; release podinfo dans l'enfant ; plus rien dans Rancher |
| R7 | Changer les registres (miroir ajouté) par Renouveler `--what registries` : images tirées du nouveau registre après | harvlab2 | `registries.yaml` des nouveaux nœuds par `kubectl debug node`, événements de tirage |
| R8 | m-14 : cluster créé par la 2.7.0 (`-auth` avec données), lu par la 2.8.0, renouvelé : ancienne forme retirée | harvlab2 | `kubectl get secret -l harvester-ops.io/registry` |
| R9 | Certificat : registre posé en 2.8 renouvelé sans toucher aux nœuds (images toujours tirées) ; registre posé en 2.7 : `--new-ca`, transition par Renouveler `--what ca`, bascule | harvlab2 | `openssl s_client` (nouveau `notAfter`), tirage d'une image après bascule |
| R10 | Nœuds Harvester : miroir fusionné dans `containerd-registry`, image tirée par `crictl` depuis 172.16.2.76, retrait, valeur d'origine relue | harvlab2 | réglage relu, `crictl pull` sur le nœud |
| R11 | Sans Internet : Internet coupé au réseau des VMs, cluster créé avec `offline: true` disponible ; compteurs de la règle nftables relevés | harvlab2, règle nftables | `kubectl get nodes`, compteurs `nft list ruleset` |
| R12 | Deux clusters dans une exécution : un importé (Fleet), un non (Flux), `auto` des deux, puis Défaire | **harv4** (iLO 172.16.1.34, `harvester-startup.sh --cluster harv4`, pool relu dans NetBox) + VM `rancher-manager`, à la demande | idem R3, R6 par cluster |

Non vérifiable sur nos bancs, à écrire comme tel : un fournisseur
d'identité de client (Keycloak, Entra ID) autre que Dex, un Rancher en
airgap avec `system-default-registry`, Rancher 2.15 (forme du jeton couverte
par les tests seulement), plus de deux clusters enfants, Harbor.

## Points à trancher

219. **Bancs et accords.** Recommandation : harvlab2 pour R1 à R11 (node2
     allumé quelques heures, 32 Gio, 172.16.2.76 déjà réservée, réglage
     Longhorn temporaire, règle nftables pour R11) ; VM `rancher-manager`
     démarrée pour R5, R6 et R12 puis arrêtée, **avec écriture dans Rancher**
     (import d'un cluster de test, `GitRepo`, `HelmOp`), tout retiré après ;
     Dex temporaire sur node1 (port 5556 à chaud, refermé) ; dépôt Gitea
     temporaire supprimé après ; harv4 pour R12 à la demande.
220. **Mécanisme d'import dans un Rancher externe.** Options : A import
     générique (cluster de provisionnement créé par l'API de Rancher,
     manifeste d'enregistrement appliqué par la console dans le cluster
     enfant) ; B Turtles du Rancher externe (il faudrait les objets Cluster
     API chez lui) ; C étiquette d'auto-import existante (vise le Rancher
     intégré, écarté par la conception). Recommandation : A.
221. **Identité pour écrire dans Rancher.** Options : A session Rancher de la
     personne seulement (comme Projets et Membres ; un plan qui importe est
     refusé au lancement sans session) ; B jeton d'API Rancher saisi comme
     secret du plan ; C identifiants d'un administrateur de Rancher.
     Recommandation : A, B plus tard si un client le demande.
222. **Quel Rancher.** Options : A celui de la session, qui gère le cluster
     Harvester ; B n'importe quel Rancher réglé, choisi par cluster.
     Recommandation : A.
223. **Place dans le plan.** Recommandation : import et OIDC sont des options
     du modèle de cluster (phase Clusters, import juste après la création) ;
     nouvelle phase `gitops` après Artefacts ; outil `auto` par défaut.
224. **Distribution de Flux.** Options : A chart communautaire `flux2`
     2.20.0 (Flux 2.10.0, Apache-2.0) livré par un paquet et CAAPH ; B Flux
     Operator (AGPL-3.0) ; C manifestes de `flux install --export` (CRD
     refusées aux manifestes). Recommandation : A, avec une recette de paquet
     fournie dans la documentation ; la console n'embarque pas Flux dans son
     archive.
225. **Droits de Flux.** Options : A par namespace par défaut (compte par
     élément lié à `edit` dans le namespace visé, références entre
     namespaces interdites), `cluster-admin` sur demande d'un administrateur ;
     B `cluster-admin` par défaut (réglage d'origine de Flux).
     Recommandation : A.
226. **Sources admises.** Recommandation : Flux : artefact OCI d'un paquet
     publié, ou Git du client ; Fleet : Git joignable depuis Rancher, ou
     chart d'un registre par `HelmOp` ; Fleet avec les manifestes d'un paquet
     refusé (`gitops-source-unsupported`, conseil : Flux ou Git).
227. **Transport des identifiants de lecture de Flux.** Options : A Secret de
     type `resource-set` dans le CRS de l'élément ; B lecture anonyme du
     registre ; C application directe par la console hors CRS.
     Recommandation : A.
228. **Forme de l'OIDC.** Options : A configuration structurée
     (`--authentication-config`, un fournisseur, CA intégrée, préfixes
     obligatoires, `v1` dès 1.34, `v1beta1` de 1.30 à 1.33) ; B drapeaux
     `--oidc-*`. Recommandation : A.
229. **Groupe administrateur OIDC.** Options : A `admin_group` facultatif,
     seule liaison vers `cluster-admin`, rendue par la console et montrée au
     résumé, jamais depuis un paquet ; B aucun, l'administration reste au
     kubeconfig de Cluster API. Recommandation : A.
230. **Kubeconfig des personnes en OIDC.** Options : A rendu par la console
     avec le greffon kubelogin (`kubectl oidc-login`), kubelogin installé par
     la personne ; B kubelogin embarqué dans l'archive de la console.
     Recommandation : A.
231. **Rancher et OIDC sur le même cluster.** Recommandation : permis avec
     l'avertissement `oidc-with-rancher` ; OIDC décoché par défaut quand
     l'import est coché.
232. **Retrait d'un import.** Options : A suppression dans Rancher, attente,
     puis restes connus supprimés par la console après dry-run serveur,
     namespace étranger gardé ; B restes laissés et nommés. Recommandation :
     A ; dans Défaire, l'import part avant le cluster.
233. **Renouvellement des machines (décision 190, OIDC, CA).** Options : A
     rotation des gabarits (`-r<N>`, ClusterClass repointée, remplacement
     progressif) en un geste « Renouveler » ; B modification en place ; C
     recréer le cluster. Recommandation : A, si la tâche 1 le prouve.
234. **m-14.** Recommandation : marque d'usage par étiquette sur
     `<c>-registries`, lecture des deux formes, ancienne forme retirée au
     premier renouvellement ou à la suppression.
235. **Certificat du registre d'amorçage.** Options : A clé de CA gardée en
     Secret sur Harvester pour les registres posés dès la 2.8.0, CA limitée
     par `nameConstraints` au registre, renouvellement sans toucher aux
     nœuds ; registres antérieurs par nouvelle CA en transition et
     Renouveler ; B toujours une nouvelle CA et un renouvellement des
     machines. Recommandation : A.
236. **Réglage `containerd-registry` des nœuds Harvester (196).** Options : A
     geste « Nœuds Harvester » dans Registres, fusion, valeur précédente
     gardée, redémarrage de RKE2 annoncé, hors plans ; B rester manuel.
     Recommandation : A.
237. **Cluster enfant sans Internet (191).** Options : A sorte `rke2` du
     paquet, blobs publiés, récupération par `curl` et `airGapped: true` ; B
     image de VM préparée par le client seulement ; C les deux.
     Recommandation : C (A par défaut, B accepté par `offline_artifacts:
     image`).
238. **Découpage.** Options : A tout en 2.8.0, la tâche 8 (sans Internet)
     détachable en 2.8.1 si R11 échoue ; B 2.8.0 accès et GitOps, 2.9.0
     registres et sans Internet. Recommandation : A.
239. **Option existante `rancher_import` (Turtles du Rancher intégré).**
     Recommandation : gardée, libellée « Rancher intégré (Turtles),
     déconseillé », exclusive de l'import externe.
240. **Reliquats 2.7.1.** Recommandation : m1 de la revue finale 2.7.0,
     `--remove-images` restreint et verrou tenu pendant la désinstallation
     sortent en 2.7.1 avant la 2.8.0 ; la 2.8.0 se rebase dessus et ne les
     reprend pas.

## Sources

- Flux, installation airgap : https://fluxcd.io/flux/installation/configuration/air-gapped/
- Flux, `flux install` (`--export`, `--registry`, `--image-pull-secret`) : https://fluxcd.io/flux/cmd/flux_install/
- Versions de Flux (v2.10.0 du 26/10/2026, v2.9.x) : https://github.com/fluxcd/flux2/releases
- Chart communautaire `flux2` 2.20.0, appVersion 2.10.0 : https://github.com/fluxcd-community/helm-charts/blob/main/charts/flux2/Chart.yaml ; licence Apache-2.0 : https://github.com/fluxcd-community/helm-charts/blob/main/LICENSE
- Flux Operator, licence AGPL-3.0 : https://github.com/controlplaneio-fluxcd/flux-operator
- Turtles, enregistrement des clusters (étiquette `cluster-api.cattle.io/rancher-auto-import`) : https://turtles.docs.rancher.com/turtles/stable/en/user/rancher-cluster-registration.html
- Rancher, enregistrer un cluster existant : https://ranchermanager.docs.rancher.com/how-to-guides/new-user-guides/kubernetes-clusters-in-rancher-setup/register-existing-clusters
- Harvester 1.9, cluster de provisionnement puis `ClusterRegistrationToken` (`status.clusterName`, `manifestUrl`) : https://docs.harvesterhci.io/v1.9/rancher/virtualization-management/
- Rancher 2.15, jeton d'enregistrement hors du statut (`tokenSecretName`, `{token}`) : https://github.com/loft-sh/vcluster-rancher-operator/issues/45
- Rancher 2.14 et Fleet 0.15.x : https://documentation.suse.com/cloudnative/rancher-manager/v2.14/en/release-notes.html ; https://github.com/rancher/rancher/pull/58284
- Fleet, HelmOps (OCI, `helmSecretName`, charts tirés par les clusters enfants) : https://fleet.rancher.io/how-tos-for-users/helm-ops ; fin du statut expérimental en 0.13 : https://github.com/rancher/fleet/releases/tag/v0.13.0
- RKE2, référence de configuration du serveur (`kube-apiserver-arg`, `kube-apiserver-extra-mount`) : https://docs.rke2.io/reference/server_config ; montages des composants : https://docs.rke2.io/advanced
- Discussion RKE2 sur les arguments de l'API (OIDC) : https://github.com/rancher/rke2/discussions/3861
- CAPRKE2, types v1beta2 (`serverConfig.kubeAPIServer`, `ComponentConfig`, `files`, `airGapped`) : https://github.com/rancher/cluster-api-provider-rke2/blob/main/controlplane/api/v1beta2/rke2controlplane_types.go ; https://github.com/rancher/cluster-api-provider-rke2/blob/main/bootstrap/api/v1beta2/rke2config_types.go

## Décisions (07/11/2026)

Les recommandations des points 219 à 240 sont retenues, bancs compris (VM rancher-manager démarrée puis arrêtée, import de test retiré de Rancher après, Dex et dépôt Gitea temporaires, règle nftables temporaire sur node2, harv4 à la demande).

## Essais préalables (tâche 1)

Faits le 07/11/2026 sur harvlab2 (node2 allumé par son iLO, éteint après,
`PowerState: Off` relu), le Rancher externe (VM `rancher-manager` de harv1
démarrée puis arrêtée, `runStrategy: Halted` remis), un Dex de banc sur
node1 (podman sans privilèges, `ghcr.io/dexidp/dex:v2.45.0`, port haut tiré
au hasard ouvert à chaud puis refermé, conteneur supprimé) et un dépôt Gitea
temporaire (supprimé). Cluster enfant 1+1 `rke2-banc` (RKE2
v1.34.11+rke2r1, Leap 15.6, disques de 10 Gio) créé avec le gabarit du plan
de contrôle retouché à la main, supprimé à la fin par `harvester-capi
delete` ; objets du banc (image, paire de clés, pool) retirés,
`storage-minimal-available-percentage` remis à 25. Les autres clusters de
Rancher n'ont pas bougé (liste relue avant et après). Critère de passage :
1, 4, 5 et 6 réussis (4 avec la réserve de version ci-dessous).

1. **Import générique** : Rancher v2.14.1 Prime, Fleet 0.15.1. Le schéma de
   `provisioning.cattle.io.cluster` permet `POST` ; `POST
   /v1/provisioning.cattle.io.clusters` avec `spec: {}` et nos étiquettes
   est accepté, `status.clusterName` (`c-m-nwsjn9p6`) arrive en 3,6 s.
   `/v3/clusterregistrationtokens?clusterId=` rend `default-token` quelques
   secondes plus tard, avec `token` et `manifestUrl` déjà substitué
   (`https://<rancher>/v3/import/<jeton>_<c-m-...>.yaml`, aucun `{token}`).
   Manifeste de 5 251 octets, neuf objets : ClusterRole
   `proxy-clusterrole-kubeapiserver` et `cattle-admin`, ClusterRoleBinding
   `proxy-role-binding-kubernetes-master` et `cattle-admin-binding`,
   Namespace `cattle-system`, ServiceAccount `cattle`, Secret
   `cattle-credentials-*`, Deployment et Service `cattle-cluster-agent`. Une
   seule image : `registry.rancher.com/rancher/rancher-agent:v2.14.1` (570 Mo
   compressés). Appliqué par `kubectl apply` avec le kubeconfig du cluster
   enfant : `Active` en 350 s (nœud encore `NotReady` à l'application : CNI,
   tirage de l'image compris), `status.ready` vrai. Le cluster Fleet porte le
   nom du cluster de provisionnement (`harvlab2-rke2-banc`) et en recopie les
   étiquettes de la console. Membres : une seule liaison, `cluster-owner`
   pour la personne qui importe (CRTB lue par `/v3`, ce que la fenêtre
   Membres affiche). Depuis node1, `rancher.home.zypp.fr` se résout sur
   l'adresse publique : la console relit le manifeste par l'adresse de la
   session Rancher (chemin `/v3/import/...` seul), jamais par l'hôte écrit
   dans `manifestUrl`.
2. **Retrait** : `DELETE` du cluster de provisionnement, disparu de
   `/v1` et de `/v3/clusters` en 6 s. Rancher lance alors dans le cluster
   enfant un Job `cattle-cleanup-*` (namespace `default`, image de l'agent,
   environ une minute) qui retire `cattle-system` et `cattle-fleet-system`.
   Restent ensuite : le namespace `local` (vide) ; les ClusterRole
   `cattle-*`, `cluster-owner`, `create-ns`, `project-owner`,
   `project-owner-promoted`, `proxy-clusterrole-kubeapiserver`,
   `p-<id>-namespaces-edit|readonly` ; les ClusterRoleBinding `cattle-*`,
   `crb-*`, `globaladmin-*`, `proxy-role-binding-kubernetes-master` ; les CRD
   des groupes `management.cattle.io`, `catalog.cattle.io`, `ui.cattle.io`,
   `upgrade.cattle.io`, `auditlog.cattle.io`, `telemetry.cattle.io` et leurs
   APIService. Aucun webhook. **Jamais** les CRD `k3s.cattle.io` et
   `helm.cattle.io` : elles sont à RKE2. Les namespaces posés par Fleet
   (cibles des bundles) restent aussi. Constante `rancher_import.LEFTOVERS`.
3. **Fleet** : `HelmOp` disponible sans drapeau en Fleet 0.15.1 (chart
   `oci://ghcr.io/stefanprodan/charts/podinfo` 6.11.0, tiré par le cluster
   enfant, prêt en moins d'une minute). `GitRepo` par SSH depuis la VM
   Rancher vers Gitea (172.16.1.11:2222), `clientSecretName` de type
   `kubernetes.io/ssh-auth` avec `known_hosts` : prêt 1/1. Piège :
   `known_hosts` doit porter toutes les clés de l'hôte (ed25519 seule :
   `knownhosts: key mismatch`), et un `GitRepo` en échec ne se relit qu'avec
   `spec.forceSyncGeneration` incrémenté. `targetNamespace` du `GitRepo`
   marche. Prêt = `status.summary.ready` égal à `desiredReady`. Supprimer le
   `GitRepo` ou le `HelmOp` retire les objets du cluster enfant et garde les
   namespaces. `helmSecretName` non exercé (chart public).
4. **Flux** : le chart `flux2` 2.20.0 (Flux 2.10.0) **refuse Kubernetes
   avant 1.35** : son Job `flux-check` (crochet pre-install, aucune valeur
   pour le retirer) échoue sur `v1.34.11+rke2r1 does not match >=1.35.0-0`.
   `flux2` 2.19.1 (Flux 2.9.5) s'installe en 30 s sur 1.34. La console ne
   fixe donc pas la version du chart : le paquet porte celle qui convient au
   cluster enfant, la recette de la documentation le dit, et
   `gitops-flux-missing` lit l'état du service `flux`. Valeurs du chart :
   `multitenancy.enabled: true`, `multitenancy.defaultServiceAccount:
   hops-gitops`, `multitenancy.privileged: false` (donnent
   `--no-cross-namespace-refs=true` et `--default-service-account` aux
   contrôleurs), `imageAutomationController.create`,
   `imageReflectionController.create` et `sourceWatcher.create` à `false`.
   Aucune valeur de registre : les noms d'image sont gardés et le miroir
   `ghcr.io` du `registries.yaml` les sert. Images de 2.20.0 : flux-cli
   v2.10.0 (48,5 Mo), helm-controller v1.7.1 (55,3), kustomize-controller
   v1.10.1 (74,9), notification-controller v1.10.0 (54,9), source-controller
   v1.10.1 (72,9), 306,6 Mo ; chart de 68 Kio, onze CRD. Artefact construit
   par `flux_artifact` (bibliothèque standard) et poussé par
   `oci_push.Registry` dans un registre de banc : accepté par
   l'`OCIRepository` (révision `<étiquette>@sha256:<manifeste>`), deux
   constructions donnent les mêmes octets. `Kustomization` appliquée dans
   `equipe-a` par `targetNamespace` avec le compte `hops-gitops-<nom>` :
   **le compte vit dans le namespace de la `Kustomization` (`hops-gitops`)**,
   Flux usurpe `system:serviceaccount:hops-gitops:hops-gitops-<nom>`, la
   `RoleBinding` vers `edit` est dans `equipe-a`. Passage à un contenu d'un
   objet de moins : objet élagué. Contenu avec une ClusterRole : refus
   `Forbidden` (verrouillage). Référence vers une source d'un autre
   namespace : `AccessDenied ... cross-namespace references have been
   blocked`. **CRS appliqué avant le chart** : le ConfigMap reste
   `applied: false`, application partielle (namespaces, compte, Secret
   créés), réessais espacés (19:19:17, 19:22:31, 19:28:00), appliqué
   2 min 30 après Flux prêt : la console installe Flux d'abord et refuse
   sans lui. **Secret `addons.cluster.x-k8s.io/resource-set` dans un CRS** :
   appliqué tel quel (Secret `kubernetes.io/dockerconfigjson` dans
   `hops-gitops`) ; l'empreinte du binding pour un Secret est le sha256 des
   valeurs DÉCODÉES, clés triées. Supprimer le CRS a emporté le ConfigMap ET
   le Secret (propriétaire) : `--cascade=orphan` pour les deux.
5. **OIDC** : Dex (mots de passe statiques avec groupes, octroi par mot de
   passe), `apiserver.config.k8s.io/v1` accepté par 1.34.11. `kubectl auth
   whoami` : `oidc:alice@bench.lo`, groupes `oidc:hops-admins`,
   `oidc:equipe-a`, `system:authenticated` ; autre audience :
   `Unauthorized` ; jeton accepté 6 s avant `exp`, refusé 74 s après.
   `ClusterRoleBinding` vers `cluster-admin` pour `oidc:hops-admins` : alice
   oui, bob non. **RKE2 monte lui-même le fichier passé à
   `authentication-config`** (volume `file1`, `hostPath` de type `File`,
   lecture seule) ; `extraMounts` ajoutait un second montage du répertoire en
   lecture-écriture : redondant, abandonné (zéro redondance). Fichier écrit
   par CAPRKE2 avec `root:root` et `0600`. Schéma CAPRKE2 v1beta2 relu
   (`extraArgs []string`, `extraMounts map[string]string`).
6. **Rotation des gabarits** : la modification EN PLACE d'un gabarit est
   acceptée par les webhooks (`--dry-run=server`) : la rotation n'est pas
   imposée, elle reste retenue (ancien gabarit gardé pour le retour arrière,
   rien de ce que `caphv-generate` a produit n'est réécrit). Copies `-r1`
   (fichier ajouté) puis `ClusterClass` repointée : remplacement du plan de
   contrôle et du worker EN MÊME TEMPS (`maxSurge: 1`), 9 min pour un 1+1 ;
   API injoignable environ 65 s (7 sondes sur 107, toutes les 5 s) au
   passage de l'unique plan de contrôle ; adresses .200 et .201 rendues,
   .202 et .203 prises. Cluster API ne supprime pas les anciens gabarits.
   OIDC toujours servi par le nouveau plan de contrôle.
7. **Sans Internet, bases** : `curl` 8.22 et `sha256sum` présents dans
   l'image Leap 15.6 ; `agentConfig.airGapped` existe dans CAPRKE2 v1beta2.
   Tailles : `rke2.linux-amd64.tar.gz` 41 Mo, images `core` 814 Mo et
   `calico` 610 Mo (tar.gz) ; agent Rancher 570 Mo ; Flux 307 Mo. Disque de
   10 Gio du worker à 78 % juste après sa création, et l'ancien worker en
   échec de ramasse-miettes d'images avec l'agent et Flux : 20 Gio
   recommandés dès qu'un import ou Flux est demandé. Cluster entièrement
   sans Internet non créé ici : tâche 8 et R11.
8. **`containerd-registry`** de harvlab2, en lecture seule : valeur JSON
   `{Mirrors: {<hôte>: {Endpoints, Rewrites}}, Configs: {<hôte>: {Auth:
   null, TLS}}, Auths: null}`, déjà un miroir `172.16.1.11:5005` (essai
   d'une version précédente) ; identifiants dans le Secret
   `fleet-local/harvester-containerd-registry-<empreinte>` (type
   `rke.cattle.io/auth-config`, clés `auth`, `host`, `identityToken`,
   `password`, `username`, étiquette `harvesterhci.io/setting:
   containerd-registry`), cité par `fleet-local/local`
   `spec.rkeConfig.registries.configs.<hôte>.authConfigSecretName`. Rien
   n'a été modifié.

Conséquences pour les tâches 2 à 4 : restes connus de l'import écrits en
constante (point 2) ; manifeste relu par l'adresse de la session ; OIDC
sans `extraMounts` ; compte de Flux dans `hops-gitops` ; Flux d'abord ;
empreinte des Secrets du CRS sur les valeurs décodées ; `--cascade=orphan`
pour ConfigMap et Secret ; version du chart Flux laissée au paquet.
