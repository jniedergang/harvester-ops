# Architecture

```
Poste d'exploitation
└─ Conteneur harvester-ops  (registry.suse.com/bci/python:3.11)
   │
   └─ Console Flask  (onglets sidebar · dock d'actions · flux SSE · multi-cluster)
        │
        ├──▶ bin/harvester-{shutdown,startup,status}.sh  — moteur de séquençage (SSE)
        ├──▶ kubectl  — cycle VM, topologie, status
        ├──▶ CAPHV / clusterctl  — clusters RKE2 downstream
        ├──▶ terraform  — déclarations (IaC)
        └──▶ Redfish  — découverte BMC + alimentation

   auth      ▶ Basic auth + TLS (navigateur) · kubectl + ssh (clusters) · redfish (BMC)
   atteint   ▶ clusters Harvester (prod, staging, …) : control-plane + workers,
               Harvester + Longhorn + KubeVirt
```

## Deux types d'opération

harvester-ops pilote les clusters par **deux voies**, et la distinction
compte pour la confiance et pour l'airgap :

1. Le **séquençage électrique** passe par les **scripts bash uniquement**.
   La console les spawn ; elle ne réimplémente jamais la logique
   shutdown/startup. C'est le cœur auditable, indépendant de l'UI.
2. **Tout le reste** (cycle VM, topologie, Cluster API, Terraform,
   bare-metal) est la console Flask qui parle à **`kubectl` / providers /
   Redfish directement**. Ces surfaces n'existent que dans la console.

La garantie forte — *« l'UI ne bypass jamais les scripts »* — s'applique
donc au **séquençage électrique**, le seul endroit où se tromper d'ordre
peut perdre des données. Les surfaces de plus haut niveau sont des clients
d'API classiques.

## Rôles des composants

### Scripts bash (`bin/`)

- **Seule voie d'exécution du séquençage électrique**. La console ne les
  bypass jamais pour le shutdown/startup.
- Autonomes : dépendances `bash`, `kubectl`, `ssh`, `yq`, `python3`
  (quelques transformations façon jq).
- Émettent des lignes structurées `STEP_EVENT|<id>|<status>|<msg>` sur
  stderr, parsées par la console en événements SSE.
- Lisent la config depuis `/etc/harvester-ops/config.yaml`.

### Bibliothèque commune (`bin/lib/common.sh`)

- Logging (TTY coloré + fichier de log plain).
- `confirm()` / `interactive_pause()` pour le mode interactif.
- Wrapper `run` respectant `--dry-run`.
- Chargeur de cluster (parse YAML, fixe `KUBECONFIG`, options SSH, liste
  des nodes).
- Helpers VM : `get_ordered_vms`, `snapshot_vm`, `wait_for_vm_ready`,
  `set_vm_priority`.
- `emit_event()` pour les événements d'étape compatibles SSE.

### Console Flask (`web/`)

- **Un conteneur Flask** servant tous les clusters configurés.
- **Surface de séquençage** : spawn les scripts bash via
  `subprocess.Popen` et stream leur stderr via SSE.
- **Surfaces API directes** : cycle de vie VM, topologie et status
  cluster, install Cluster API / CAPHV + CRUD de clusters, déclarations
  Terraform, BMC / Redfish — implémentées en appels `kubectl` / provider /
  Redfish.
- **Modèle d'action** : chaque appel mutatif devient un `ActionRun` avec
  un log d'événements live, affiché dans le dock persistant et conservé
  dans un historique SQLite (mode WAL).
- **Notes collaboratives** : un endpoint de sync WebSocket sous-tend les
  notes Yjs, persistées en SQLite.
- **Observabilité** : `/metrics` Prometheus, `/healthz` (liveness),
  `/healthz/ready` (readiness).
- **État** : seuls l'historique d'actions et les notes sont persistés ;
  l'app est redémarrable à tout moment. Auth HTTP Basic (htpasswd) sur
  TLS ; OIDC est un candidat pour une version future.

### Image conteneur

- Base : `registry.suse.com/bci/python:3.11`.
- Contient `kubectl`, `yq`, `openssh-clients`, les wheels Python
  (offline), les scripts bash et la console.
- Construit via `container/Containerfile`, sauvegardé en tar OCI
  (`images/harvester-ops-ui.tar`) pour la livraison airgap.
- Les bundles CAPHV et le binaire du provider Terraform sont **fournis
  séparément** (ils activent les surfaces optionnelles Cluster API /
  Terraform) ; l'image de base ne les embarque pas.

## Modèle multi-cluster

Un seul `config.yaml` déclare N clusters. Chaque commande CLI
(`--cluster <nom>`) charge exactement le contexte d'un cluster. La sidebar
de la console liste tous les clusters ; basculer ne fait que changer le
contexte actif — pas d'opération cross-cluster en fan-out.

## Flux d'événements SSE

Quand la console déclenche une action (un shutdown ici) :

```
client                          Flask                       script bash
  │ POST /api/action              │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ subprocess.Popen             │
  │                               │ ──────────────────────────▶  │
  │ GET /api/stream/<id> (SSE)    │                              │
  │ ───────────────────────────▶  │                              │
  │                               │ read stderr ligne par ligne  │
  │                               │ ◀──── "STEP_EVENT|vm-stop|   │
  │                               │        running|..."          │
  │ event: step                   │                              │
  │ data: {"id":"vm-stop",..}     │                              │
  │ ◀───────────────────────────  │                              │
```

Le frontend (vanilla JS + un wrapper `EventSource` à reconnexion
automatique) met à jour les indicateurs d'étape en temps réel sans
polling. Les actions API directes (ops VM, apply Terraform, install
CAPHV) utilisent le même mécanisme `ActionRun` + SSE, streamant les
événements step/log au fil de leur progression.

## Modèle de confiance

- Le conteneur dispose d'un accès `cluster-admin` complet via les
  kubeconfigs et clés SSH montés, et peut atteindre les BMC en Redfish.
- Il doit tourner sur un **poste opérateur de confiance** ou un jump host
  dédié.
- Les path params correspondant à des noms Kubernetes sont validés
  (RFC 1123) à la frontière HTTP ; les endpoints mutatifs portent auth +
  rate-limiting ; les kubeconfigs sont stagés en permissions `0600` par
  workspace Terraform.
- Basic auth + TLS protègent la console. Pour plus de sécurité, placer
  derrière un reverse proxy avec mTLS ou OIDC.
