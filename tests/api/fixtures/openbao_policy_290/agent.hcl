path "sys/rotate/root" { capabilities = ["update", "sudo"] }
path "identity/entity/name/tf-*" { capabilities = ["read", "delete"] }
path "harvester-ops/config" { capabilities = ["read", "update"] }
path "sys/storage/raft/snapshot" { capabilities = ["read"] }
path "auth/approle/role/harvester-ops-abc123def456/custom-secret-id" { capabilities = ["update"] }
