"""2.9.0 tâche 4 : test de source, aucune lecture directe d'un secret hors des
lecteurs rangés derrière le magasin de secrets.

Dans `web/` et `bin/`, chaque lecture du champ `kubeconfig`, de la clé SSH
d'un cluster (`ssh.key`), d'un chemin `ssh/<nom>_id`, d'un fichier
`.oidc-secret`, du répertoire `.secrets` des espaces Terraform KVM ou du
réglage `notify` de la réplication doit se trouver dans une fonction de la
liste ci-dessous (lecteur qui passe par `secret_store`, écrivain ou gestion
de la configuration). Une lecture nouvelle ailleurs fait échouer le test :
elle contournerait OpenBao une fois la classe migrée.

Mutation jouée (rapport des tâches 2 à 4) : « une lecture directe ajoutée
dans web/app.py » (le test échoue).
"""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent

# Outils de bin/ : l'entrée vient de `kube.cluster_config` (ou de
# `declared_clusters`), qui la passe par `cluster_decl.materialize` : une
# référence y est déjà devenue un chemin de travail.
_BIN_TOOLS_MATERIALIZED = {
    ("bin/harvester-artefacts.py", "cmd_publish"), ("bin/harvester-capi.py", "kube_from"),
    ("bin/harvester-containers.py", "targets_from"), ("bin/harvester-folders.py", "make_kube"),
    ("bin/harvester-forklift.py", "kube_from"), ("bin/harvester-network.py", "kube_from"),
    ("bin/harvester-registry.py", "kube_from"), ("bin/harvester-registry.py", "users"),
    ("bin/harvester-registry.py", "_cluster_kube"), ("bin/harvester-replication.py", "cluster_client"),
    ("bin/harvester-resources.py", "kube_from"), ("bin/harvester-vm-transfer.py", "cluster_kubeconfig"),
}

ALLOWED = {
    "kubeconfig": _BIN_TOOLS_MATERIALIZED | {
        ("web/app.py", "_cluster_kubeconfig_path"),          # le lecteur
        ("web/app.py", "_cluster_of_kubeconfig"),            # comparaison, puis le lecteur
        ("web/app.py", "_registry_console_address"),         # présence, puis le lecteur
        ("web/app.py", "_baremetal_install_steps"),          # kubeconfig neuf d'une installation (écrivain)
        ("web/app.py", "api_clusters_create"), ("web/app.py", "api_clusters_update"),
        ("web/app.py", "api_clusters_delete"),               # gestion de la déclaration
        ("bin/lib/cluster_decl.py", "_map_paths"), ("bin/lib/cluster_decl.py", "materialize"),
        ("bin/lib/kube_rest.py", "run"),                     # option --kubeconfig de kubectl
    },
    "ssh-key": {
        ("web/app.py", "_cluster_ssh_key_path"),             # le lecteur
        ("web/app.py", "api_network_fabric_node_detail"), ("web/app.py", "_fabric_ssh_base"),  # présence
        ("web/app.py", "_bm_join_pubkey"),                   # drapeau `generated`, puis le lecteur
        ("web/app.py", "_validate_cluster_payload"), ("web/app.py", "api_clusters_create"),
        ("web/app.py", "api_clusters_update"), ("web/app.py", "api_clusters_delete"),
        ("bin/lib/cluster_decl.py", "_map_paths"), ("bin/lib/cluster_decl.py", "materialize"),
        ("bin/lib/node_images.py", "ssh_options"),           # par secret_store.path_of
    },
    "key-file": {
        ("bin/lib/kvm_decl.py", "key_rel"), ("bin/harvester-kvm.py", "cmd_host_add"),
        ("web/app.py", "_kvm_add_request"),                  # chemin dérivé du nom (écrivains)
        ("web/app.py", "_bm_cluster_files"), ("web/app.py", "api_clusters_create"),
        ("web/app.py", "api_clusters_update"), ("web/app.py", "api_clusters_upload_sshkey"),
        ("bin/lib/secret_classes.py", "local_location"), ("bin/lib/secret_store.py", "list_local"),
    },
    "oidc-secret": {
        ("web/rancher_servers.py", "delete"), ("web/rancher_servers.py", "set_sso"),
        ("web/rancher_servers.py", "clear_sso"),
        ("bin/lib/secret_classes.py", "<module>"), ("bin/lib/secret_classes.py", "local_location"),
        ("bin/lib/secret_store.py", "list_local"),
    },
    "tf-secrets": {
        ("web/tf_kvm.py", "<module>"), ("web/app.py", "_tf_kvm_secret"),
    },
    "notify": {
        ("bin/lib/replication_notify.py", "round"),          # par resolve_secrets
        ("bin/harvester-replication.py", "cmd_notify_test"),  # par resolve_secrets
        ("bin/harvester-replication.py", "cmd_notify_show"),  # masqué (rn.mask)
        ("bin/harvester-replication.py", "cmd_notify_set"),   # garde les valeurs rangées
        ("web/app.py", "_repl_notify_stored"),               # masqué / normalisé, jamais envoyé
    },
}


def _text(n):
    if isinstance(n, ast.Constant) and isinstance(n.value, str):
        return n.value
    if isinstance(n, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)
    return None


def _is_key(node, names):
    return isinstance(node, ast.Constant) and node.value in names


def scan_source(rel, src):
    """[(catégorie, fonction, ligne)] des lectures repérées dans `src`."""
    hits = []
    tree = ast.parse(src)

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, n):
            self.stack.append(n.name)
            self.generic_visit(n)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def add(self, cat, n):
            hits.append((cat, self.stack[-1] if self.stack else "<module>", n.lineno))

        def visit_Subscript(self, n):
            if isinstance(n.ctx, ast.Load):
                if _is_key(n.slice, {"kubeconfig"}):
                    self.add("kubeconfig", n)
                elif _is_key(n.slice, {"key"}) and "ssh" in ast.unparse(n.value).lower():
                    self.add("ssh-key", n)
            self.generic_visit(n)

        def visit_Call(self, n):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "get" and n.args:
                if _is_key(n.args[0], {"kubeconfig"}):
                    self.add("kubeconfig", n)
                elif _is_key(n.args[0], {"key"}) and "ssh" in ast.unparse(f.value).lower():
                    self.add("ssh-key", n)
            if isinstance(f, ast.Attribute) and f.attr == "get_setting" and len(n.args) > 1 \
                    and _is_key(n.args[1], {"notify"}):
                self.add("notify", n)
            if isinstance(f, ast.Attribute) and f.attr == "key_rel":
                self.add("key-file", n)
            self.generic_visit(n)

        def visit_JoinedStr(self, n):
            t = _text(n) or ""
            if t.endswith("}_id"):
                self.add("key-file", n)
            if ".oidc-secret" in t:
                self.add("oidc-secret", n)

        def visit_Constant(self, n):
            if isinstance(n.value, str) and n.value.endswith(".oidc-secret"):
                self.add("oidc-secret", n)

        def visit_Name(self, n):
            if n.id == "SECRETS_DIR":
                self.add("tf-secrets", n)

        def visit_Attribute(self, n):
            if n.attr == "SECRETS_DIR":
                self.add("tf-secrets", n)
            self.generic_visit(n)

    V().visit(tree)
    return hits


def _files():
    for d in ("web", "bin", "bin/lib"):
        yield from sorted((ROOT / d).glob("*.py"))


def violations():
    out = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        for cat, fn, line in scan_source(rel, p.read_text()):
            if (rel, fn) not in ALLOWED[cat]:
                out.append(f"{rel}:{line} {fn} reads {cat} directly")
    return out


def test_no_direct_secret_read():
    assert violations() == []


def test_allowed_functions_exist():
    """Une entrée de la liste qui ne lit plus rien est à retirer : la liste
    reste celle des vrais lecteurs."""
    seen = set()
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        seen |= {(cat, rel, fn) for cat, fn, _l in scan_source(rel, p.read_text())}
    stale = sorted(f"{cat}: {rel}:{fn}" for cat, pairs in ALLOWED.items() for rel, fn in pairs
                   if (cat, rel, fn) not in seen)
    assert stale == []


@pytest.mark.parametrize("snippet,cat", [
    ('def f(c):\n    return open(c["kubeconfig"]).read()\n', "kubeconfig"),
    ('def f(c):\n    return c.get("kubeconfig")\n', "kubeconfig"),
    ('def f(c):\n    return ["ssh", "-i", c["ssh"]["key"]]\n', "ssh-key"),
    ('def f(ssh):\n    return ssh.get("key")\n', "ssh-key"),
    ('def f(st, n):\n    return (st / f"ssh/{n}_id").read_text()\n', "key-file"),
    ('def f(d, i):\n    return (d / f"{i}.oidc-secret").read_text()\n', "oidc-secret"),
    ('def f(ws):\n    return ws / SECRETS_DIR\n', "tf-secrets"),
    ('def f(db, conn):\n    return db.get_setting(conn, "notify")\n', "notify"),
])
def test_scanner_catches_each_form(snippet, cat):
    assert [h[0] for h in scan_source("x.py", snippet)] == [cat]


@pytest.mark.parametrize("snippet", [
    'def f(c):\n    c["kubeconfig"] = "x"\n',                  # écriture
    'def f(c):\n    return c.get("name")\n',
    'def f(o):\n    return o["key"]\n',                         # clé sans rapport avec ssh
])
def test_scanner_ignores_others(snippet):
    assert scan_source("x.py", snippet) == []


def test_common_sh_resolves_references():
    """Les scripts bash lisent kubeconfig et clé par `_secret_field`, qui
    passe une référence à `harvester-secrets file` (copie tmpfs effacée à la
    sortie)."""
    sh = (ROOT / "bin" / "lib" / "common.sh").read_text()
    assert re.search(r'_secret_field KUBECONFIG_PATH "\$\(yq -r "\$q \| \.kubeconfig"', sh)
    assert re.search(r'_secret_field SSH_KEY "\$\(yq -r "\$q \| \.ssh\.key', sh)
    assert "harvester-secrets.py\" file" in sh
    assert 'for f in "${_HOPS_SECRET_FILES[@]}"; do rm -f -- "$f"; done' in sh
    assert "KUBECONFIG_PATH=$(_state_path" not in sh and "SSH_KEY=$(_state_path" not in sh


@pytest.mark.parametrize("rel,fn", [
    ("bin/lib/replication_notify.py", "round"),
    ("bin/harvester-replication.py", "cmd_notify_test"),
])
def test_notify_senders_resolve_references(rel, fn):
    """Les deux lecteurs du réglage qui ENVOIENT passent par
    `resolve_secrets` (référence -> valeur) avant d'utiliser un secret."""
    tree = ast.parse((ROOT / rel).read_text())
    body = next(ast.unparse(n) for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == fn)
    assert "resolve_secrets(" in body
