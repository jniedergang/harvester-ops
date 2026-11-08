"""2.9.0 tâche 3 : table des classes de secrets (bin/lib/secret_classes.py).

Table pure : chemins OpenBao par classe puis par cluster, classes non
migrables, emplacements locaux (descripteurs, rien n'est lu), champs
d'OpenBao dérivés des noms de canaux et d'en-têtes (jamais d'une valeur).
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import secret_classes as sc   # noqa: E402
import secret_store as ss     # noqa: E402


def test_table_matches_store_classes():
    assert set(sc.MIGRATABLE) == set(ss.CLASSES)
    assert set(sc.MIGRATION_ORDER) == set(sc.MIGRATABLE) - {"plan-run"}
    assert sc.MIGRATION_ORDER[0] == "replication-notify" and sc.MIGRATION_ORDER[-1] == "kubeconfig"


@pytest.mark.parametrize("cls,bao", [
    ("kubeconfig", "kubeconfig/{name}"), ("ssh-cluster", "ssh-cluster/{name}"), ("ssh-kvm", "ssh-kvm/{name}"),
    ("rancher-oidc", "rancher-oidc/{name}"), ("replication-notify", "replication-notify/global"),
    ("replication-hook", "replication-hook/{plan}/{action}"), ("tf-kvm", "tf-kvm/{host}/{decl}/{var}"),
    ("plan-run", "plan-run/{action}/{name}"),
])
def test_bao_paths_by_class_then_name(cls, bao):
    s = sc.spec(cls)
    assert s.bao_path == bao and s.migratable is True
    assert s.bao_path.startswith(cls + "/")


@pytest.mark.parametrize("cls", ["accounts", "web-auth", "public-keys", "openbao-credentials", "config-kubeconfig"])
def test_not_migratable(cls):
    s = sc.spec(cls)
    assert s.migratable is False and s.bao_path is None
    with pytest.raises(sc.NotMigratable) as e:
        sc.require_migratable(cls)
    assert e.value.code == "class-not-migratable" and e.value.cls == cls


def test_unknown_class():
    with pytest.raises(ValueError):
        sc.spec("nope")


@pytest.mark.parametrize("cls,path,field,kind,tail", [
    ("kubeconfig", "h1", "kubeconfig", "decl-file", "kubeconfigs/h1.yaml"),
    ("ssh-cluster", "h1", "private_key", "decl-file", "ssh/h1_id"),
    ("ssh-kvm", "k1", "private_key", "file", "ssh/k1_id"),
    ("rancher-oidc", "r1", "client_secret", "file", "rancher.d/r1.oidc-secret"),
    ("tf-kvm", "k1/d1/tok", "value", "file", "terraform/.kvm/k1/decls/d1/.secrets/tok"),
    ("replication-notify", "global", "mx_1__token", "notify", "replication.db"),
    ("replication-hook", "rp-0123456789ab/pre", "hdr_0123456789", "hook", "replication.db"),
    ("plan-run", "a1/install", "token", "memory", None),
])
def test_local_location(tmp_path, cls, path, field, kind, tail):
    k, p, extra = sc.local_location(cls, path, field, tmp_path)
    assert k == kind
    if tail:
        assert p == tmp_path / tail
    if kind == "notify":
        assert extra == ("mx-1", "token")
    assert not list(tmp_path.iterdir())               # rien n'est créé ni lu


@pytest.mark.parametrize("cls,path,field", [
    ("kubeconfig", "h1/x", "kubeconfig"),
    ("kubeconfig", "h1", "token"),
    ("tf-kvm", "k1/d1", "value"),
    ("replication-notify", "other", "a__token"),
    ("replication-notify", "global", "token"),
    ("replication-hook", "rp-x", "hdr_0"),
    ("replication-hook", "rp-x/a", "token"),
    ("accounts", "x", "value"),
])
def test_local_location_refused(tmp_path, cls, path, field):
    with pytest.raises(ValueError):
        sc.local_location(cls, path, field, tmp_path)


@pytest.mark.parametrize("cid,secret,field", [
    ("mx-1", "token", "mx_1__token"),
    ("a", "url", "a__url"),
    ("ml", "password", "ml__password"),
])
def test_notify_field(cid, secret, field):
    assert sc.notify_field(cid, secret) == field
    assert sc.FIELD_RE.match(field)


def test_header_field_from_name_only():
    f1 = sc.header_field("X-Api-Key")
    assert f1 == sc.header_field("x-api-key")          # nom d'en-tête insensible à la casse
    assert f1 != sc.header_field("Authorization")
    assert re.match(r"^hdr_[0-9a-f]{10}$", f1)
    long_id = "c" + "-x" * 19
    assert sc.FIELD_RE.match(sc.notify_field(long_id, ("header", "X-Api-Key")))


def test_adapter_lists_and_reads(tmp_path):
    (tmp_path / "rancher.d").mkdir()
    (tmp_path / "rancher.d" / "r1.oidc-secret").write_text("v\n")
    fs = ss.FileStore(tmp_path, tf_dir=tmp_path / "tf", repl_db=tmp_path / "r.db")
    a = sc.adapter("rancher-oidc")
    assert a.list_local(fs) == [("r1", "client_secret")]
    assert a.read_local(fs, "r1", "client_secret") == "v"
    with pytest.raises(NotImplementedError):
        a.write_ref(fs, "r1", "client_secret", "openbao:rancher-oidc/r1#client_secret")
    assert sc.adapter("plan-run").list_local(fs) == []


def test_readers_are_named():
    for cls in sc.MIGRATABLE:
        for r in sc.spec(cls).readers:
            f, _, fn = r.partition(":")
            assert (ROOT / f).is_file() and fn, r


@pytest.mark.parametrize("cid", ["a--b", "a-", "mx-1", "a--", "x"])
@pytest.mark.parametrize("secret", ["token", "url", "password", ("header", "X-Api-Key")])
def test_notify_field_round_trip(tmp_path, cid, secret):
    """M10 : un identifiant de canal avec `--` (permis) se relit à l'identique."""
    field = sc.notify_field(cid, secret)
    kind, _db, (got_cid, got_secret) = sc.local_location("replication-notify", "global", field, tmp_path)
    assert kind == "notify" and got_cid == cid
    assert got_secret == (secret if isinstance(secret, str) else sc.header_field(secret[1]))
