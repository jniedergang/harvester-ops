# harvester-ops 2.9.0, mode B1 : OpenBao à côté de la console, sur le même
# hôte. Rendu par l'agent des secrets (harvester-secrets-agent) dans
# /etc/harvester-ops-openbao/openbao.hcl : __SEAL__ devient la strophe
# `seal "static"` (clé remise par un secret podman), __INITIALIZE__ la
# strophe `initialize` de la première pose (vide ensuite), __ESO_LISTENER__
# le second écouteur d'ESO (2.10.0, vide sans cluster enfant). Aucune valeur
# secrète ici. Plan : docs/design/2026-11-08-openbao-290-plan.md.
ui            = false
log_level     = "info"
api_addr      = "https://127.0.0.1:8210"
cluster_addr  = "https://127.0.0.1:8211"

# Écoute locale seulement : la console tourne en --network host.
listener "tcp" {
  address         = "127.0.0.1:8210"
  cluster_address = "127.0.0.1:8211"
  tls_cert_file   = "/openbao/tls/server.pem"
  tls_key_file    = "/openbao/tls/server.key"
}

# 2.10.0 : second écouteur pour External Secrets Operator (point 298),
# posé par l'agent tant qu'un cluster enfant a ESO ; ligne vide sinon.
__ESO_LISTENER__

# Raft (nœud unique) : le moteur `file` a été retiré en 2.7.0 et
# l'API d'instantanés exige raft.
storage "raft" {
  path    = "/openbao/data"
  node_id = "hops-1"
}

# Journal d'audit déclaré en configuration (l'API ne peut plus en créer).
audit "file" "hops" {
  options {
    file_path = "/openbao/audit/audit.log"
  }
}

__SEAL__

__INITIALIZE__
