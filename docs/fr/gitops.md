# GitOps des clusters enfants (2.8.0)

Un cluster enfant créé par Cluster API peut suivre tout seul une source de
manifestes. La console pose l'outil GitOps et sa source, montre son état,
et le retire. Elle ne réécrit jamais ce que l'outil sait faire.

- **Cluster API > GitOps** (nouvel onglet, entre Services et Registres)
  liste les entrées posées sur les clusters enfants du cluster Harvester
  affiché dans l'en-tête.
- Un plan de déploiement porte les mêmes entrées dans sa dernière phase,
  **GitOps** (voir [bare-metal.md](bare-metal.md)).
- Chaque geste lance `harvester-capi gitops-*` dans une action suivie.

## Flux ou Fleet

Une entrée nomme son outil : `auto`, `flux` ou `fleet`.

- `auto` (par défaut) choisit **Fleet** quand le cluster enfant est importé
  dans le Rancher de votre session (fenêtre Accès, voir
  [procedure-operationnelle.md](procedure-operationnelle.md)), et **Flux**
  sinon.
- `fleet` sur un cluster qui n'est pas importé est refusé
  (`gitops-fleet-needs-rancher`).
- Aucun outil n'est installé par défaut. Flux vient d'un paquet (plus bas).
  Fleet vient avec l'import dans Rancher.

| Source | Flux | Fleet |
|---|---|---|
| `oci` : l'artefact Flux d'un élément `gitops` d'un paquet publié | oui | non |
| `git` : un dépôt Git, `https://` ou `ssh://` | oui | oui |
| `chart` : un élément chart d'un paquet publié | non | oui (`HelmOp`) |

Toute autre paire est refusée (`gitops-source-unsupported`).

## Flux sur un cluster enfant

Flux est le chart communautaire `flux2` (Apache-2.0). Il vient dans un
paquet comme n'importe quel chart, et CAAPH l'applique comme service nommé
**`flux`** : appliquez l'élément chart `flux` du paquet depuis l'onglet
Artefacts, ou depuis la phase Artefacts d'un plan. Une entrée vérifie que
ce service est prêt (`gitops-flux-missing` sinon).

**Valeurs de verrouillage.** La console impose ces valeurs au service nommé
`flux`, par-dessus celles du paquet. Une valeur du paquet ne peut pas les
retirer.

```yaml
multitenancy:
  enabled: true
  defaultServiceAccount: hops-gitops
  privileged: false
imageAutomationController: {create: false}
imageReflectionController: {create: false}
sourceWatcher: {create: false}
```

- Flux usurpe alors le compte nommé par chaque `Kustomization`, et refuse
  les références d'un namespace à l'autre.
- L'automatisation des images n'est pas installée.
- Un service `flux` posé sans ces valeurs est refusé
  (`gitops-flux-not-locked`) : appliquez-le de nouveau depuis la console.

**Version du chart.** La console ne fixe pas la version du chart. Le paquet
porte celle qui convient.

- Le chart `flux2` 2.20.0 (Flux 2.10) refuse Kubernetes avant 1.35. Son job
  de vérification avant installation échoue, et aucune valeur ne le saute.
- Sur un cluster enfant en 1.34, prenez le chart `flux2` 2.19.1
  (Flux 2.9.5). Il s'est installé en 30 s sur le banc.

## Ce qu'écrit une entrée Flux

Les objets d'une entrée sont livrés par un `ClusterResourceSet`
`art-gitops-<entrée>`, comme un manifeste (voir
[artefacts.md](artefacts.md)). Dans le cluster enfant :

- le namespace **`hops-gitops`**, créé par la console, commun à toutes les
  entrées ;
- dans `hops-gitops` : un `OCIRepository` ou un `GitRepository`
  `<entrée>`, une `Kustomization` `<entrée>`, et le compte
  `hops-gitops-<entrée>` (`automountServiceAccountToken: false`) ;
- la `Kustomization` vise le **namespace d'équipe** de l'entrée
  (`targetNamespace`) et tourne avec le compte `hops-gitops-<entrée>` ;
- `privileges: namespace` (par défaut) : un `RoleBinding` de ce compte vers
  `edit` dans le namespace d'équipe ;
- `privileges: cluster` (administrateurs seulement) : un
  `ClusterRoleBinding` de ce compte vers `cluster-admin`, rendu par la
  console ;
- le compte de lecture du registre, sa CA et les identifiants Git vont dans
  des Secrets de `hops-gitops`. Ils voyagent dans le Secret du
  `ClusterResourceSet`, jamais dans son ConfigMap.

Le namespace d'équipe doit exister dans le cluster enfant
(`gitops-namespace-missing`). Un namespace système, `default` ou
`hops-gitops` lui-même est refusé (`gitops-namespace-not-team`).

`prune: true` (par défaut) laisse Flux retirer ce qui sort de la source.
`interval_minutes` : 1 à 1 440, 10 par défaut. Le nom de l'entrée :
RFC 1123, 34 caractères au plus (`gitops-name-too-long`).

**Prête** quand la source, puis la `Kustomization`, sont `Ready`. Pour une
source `oci`, la révision appliquée doit être la version du paquet.

**Le verrouillage à l'œuvre.** Le compte d'une entrée `namespace` n'écrit
que dans son namespace d'équipe. Un `ClusterRole` dans une telle entrée est
refusé au compte usurpé, et la `Kustomization` échoue. C'est voulu.

## Ce qu'écrit une entrée Fleet

Fleet vit dans le cluster `local` de Rancher. La console y écrit avec le
jeton de votre session, dans `fleet-default` :

- un `GitRepo` (source `git`) ou un `HelmOp` (source `chart`), nommé
  `hops-<harvester>-<c>-<entrée>` (`hops-<harvester>-<ns>-<c>-<entrée>`
  quand le namespace diffère du nom ; au-delà de 63 caractères,
  54 caractères, `-` et 8 chiffres hexadécimaux) ;
- sa cible : le cluster Fleet du cluster enfant importé ;
- un Secret `<nom>-auth` (identifiants Git) ou `<nom>-pull` (compte de
  lecture du registre, et sa CA en `cacerts`).

La propriété de l'objet et de ses deux Secrets est contrôlée avant toute
écriture (`gitops-name-taken`). Une entrée posée par un autre plan, ou à la
main, n'est jamais reprise par un plan (`gitops-taken`). **Prête** quand
`status.summary.ready` égale `desiredReady`.

**Fleet est réservé aux administrateurs**, pour poser comme pour retirer
une entrée (`gitops-fleet-admin-only`). La raison : un `GitRepo` ou un
`HelmOp` sans `serviceAccount` déploie avec l'agent Fleet, qui est
`cluster-admin` du cluster enfant. `privileges: namespace` n'y est pas
tenu : un administrateur reçoit l'avertissement
`gitops-fleet-not-confined`.

## Éléments gitops d'un paquet

Un élément de paquet de sorte `gitops` porte des fichiers YAML
(`manifests/<élément>/<fichier>.yaml`). Il est publié en artefact Flux
(voir [artefacts.md](artefacts.md)) :

- dépôt `<registre>/harvester-ops/gitops/<paquet>/<élément>`, étiquette =
  la version du paquet ;
- une couche `application/vnd.cncf.flux.content.v1.tar+gzip`, un tar.gz
  déterministe : les mêmes fichiers donnent la même empreinte.

Le contenu est jugé par les règles des manifestes (`capi_manifests.check`)
à la publication : ni CRD, ni webhook, ni namespace système. Un outil
GitOps n'est pas une porte dérobée. Un élément refusé arrête la
publication (`publish-gitops-refused`, avec la raison).

**Chaque objet d'une sorte à namespace doit porter `metadata.namespace`.**
Cette règle de `capi_manifests.check` a été trouvée sur le banc : un
élément sans elle est refusé à la publication. Flux place quand même les
objets dans le namespace d'équipe de l'entrée (`targetNamespace`).

Un paquet publié avant la 2.8.0 n'a pas d'artefact Flux : une source `oci`
reçoit `gitops-package-not-published`. Publiez de nouveau le paquet.

## Identifiants Git

Une source `git` peut nommer un secret (`source.secret`) : une clé SSH
`{identity, known_hosts}` ou un compte `{username, password}`.

- Ils sont saisis au lancement, dans la fenêtre ou dans la fenêtre
  Déployer d'un plan. En ligne de commande, ils arrivent dans le fichier de
  demande privé.
- **Ils ne sont jamais rangés.** Ils partent vers l'outil dans un fichier
  de demande privé, puis vers le cluster enfant (Flux) ou vers Rancher
  (Fleet). Ils n'entrent jamais dans une action, une étape, un journal,
  `actions.db` ni l'état du plan.
- Un plan les garde en mémoire le temps de son exécution seulement. Une
  reprise les redemande.
- Une adresse `https://` qui porte des identifiants est refusée.

## Gestes

**Cluster API > GitOps** liste les entrées : cluster, entrée, outil, état,
origine (l'exécution d'un plan), **Retirer**.

**Ajouter une entrée** ouvre une fenêtre à trois onglets :

- **Paramètres** : cluster enfant, nom de l'entrée, outil (l'outil déduit
  est affiché), namespace d'équipe, source, privilèges, intervalle,
  élagage, identifiants Git ;
- **Vérifications** : `gitops-check` repart à chaque saisie (lecture
  seule) ;
- **Résumé**.

**Appliquer** reste grisé, avec la raison, tant que les vérifications des
valeurs affichées ne sont pas revenues sans constat bloquant. Les
identifiants Git partent une fois, puis sont effacés du formulaire.

Un élément `gitops` de l'onglet Artefacts porte un bouton **GitOps** qui
ouvre cette fenêtre avec sa source remplie.

## Retrait et Défaire

**Retirer** une entrée :

- **Flux** : la `Kustomization` d'abord, attendue jusqu'à sa disparition
  (son finaliseur élague ce qu'elle a posé, le compte encore là ; 300 s au
  plus). Puis la source, puis le compte, ses liaisons et ses Secrets, puis
  le `ClusterResourceSet` et son ConfigMap. Au-delà de l'attente, le reste
  est gardé et le retrait le dit : relancez-le.
- **Fleet** (administrateurs) : le `GitRepo` ou le `HelmOp`, relu juste
  avant la suppression, puis ses Secrets.

**Défaire** un plan retire d'abord les entrées GitOps créées par
l'exécution (sorte `gitops`), avant les artefacts, l'import dans Rancher,
les clusters, le registre et le contenu Terraform. Le chart Flux ne part
qu'après les entrées de son cluster. Sans session Rancher, une entrée
Fleet n'est jamais jugée « déjà partie » : l'élément du Défaire échoue avec
`rancher-session-needed`.

## Rôles

| Geste | Rôle |
|---|---|
| Lire les entrées | lecteur |
| Poser ou retirer une entrée Flux | opérateur **et** le droit d'administrer le cluster enfant (`child-admin-required`) |
| Poser ou retirer une entrée Fleet | admin, avec une session Rancher |
| `privileges: cluster` | admin |

## Ligne de commande

```
harvester-capi gitops-check  --cluster C --spec F
harvester-capi gitops-deploy --cluster C --spec F [--timeout 900]
harvester-capi gitops-remove --cluster C --name NS/C/ENTREE [--role admin|operator|viewer]
                             [--expect-run ID] [--dry-run]
harvester-capi gitops-status --cluster C [--name NS/C] [--json]
```

Une spec : `{cluster: "ns/c", entry: {...}, secrets: {nom: {...}}, role,
origin, by}`. L'entrée prend les clés `name`, `tool`, `namespace` (le
namespace d'équipe ; `target_namespace` dans un plan), `source`,
`privileges`, `interval_minutes`, `prune`. `gitops-remove --role` vaut
`admin` par défaut en ligne de commande ; la console passe le rôle de la
personne. Les gestes Fleet demandent `--harvester-name` et un kubeconfig
de session Rancher.

## Limites connues

- L'étiquette de l'artefact Flux n'est pas lue dans le registre au moment
  de la vérification (pas de HEAD). Une entrée dont l'artefact manque
  échoue au bout du délai avec un message clair.
- Les identifiants de lignes d'un plan peuvent se heurter : `a-b/c` et
  `a/b-c` donnent le même. À corriger en 2.9.
- Les copies Fleet de la CA d'un registre ne sont pas contrôlées avant une
  bascule de CA : la bascule émet l'étape d'avertissement
  `registry-ca-fleet-unchecked` (voir [artefacts.md](artefacts.md)).
  Redéployez les entrées Fleet avant.
- Fleet avec un `serviceAccount` confiné n'est pas proposé.

## Vu en réel (2.8.0)

Sur harvlab2 (Harvester 1.9.0, cluster enfant 1+1, Kubernetes 1.34.11),
avec le Rancher externe 2.14.1 (Fleet 0.15.1) et un dépôt Gitea
temporaire :

- Flux 2.9.5 (chart `flux2` 2.19.1) depuis le registre d'amorçage,
  valeurs de verrouillage sur la release ; une entrée `oci` et une entrée
  `git` par SSH (secret du plan) prêtes dans `equipe-a`. Le passage du plan
  au paquet 1.0.1 a élagué l'objet qu'il ne porte plus. Un `ClusterRole`
  dans une entrée de namespace est refusé par le compte usurpé, comme
  attendu.
- Fleet : un `GitRepo` depuis Gitea et un `HelmOp` du chart podinfo publié
  dans le registre, `auto` choisissant Fleet sur le cluster importé ; un
  second plan qui porte le même nom d'entrée est refusé (`gitops-taken`).
- Reprise : secret Git redemandé, entrées faites vérifiées et sautées. Le
  Défaire sans session Rancher laisse les entrées Fleet
  (`rancher-session-needed`) ; avec elle, GitOps puis artefacts sont
  retirés dans cet ordre, et les objets déployés partent avec eux.
- Quatre défauts trouvés là sont corrigés en 2.8.0 : la version du
  `HelmOp`, les bundles Fleet laissés après un retrait, le contrôle du plan
  d'une entrée sur un cluster enfant existant, le namespace de la release
  `flux`.

Non vérifié en réel : Fleet avec un serveur Git du client en HTTPS
(authentification basique), plusieurs clusters enfants à la fois, une
entrée Flux posée pendant une transition de CA.
