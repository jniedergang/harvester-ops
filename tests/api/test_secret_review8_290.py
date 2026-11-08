"""2.9.0, corrections de la revue de la tâche 8 (migration par classe).

Chaque cas a été vu rouge avant sa correction. Mutations jouées (rapport) :
« marque posée seulement avec une référence » (I3), « _set_decl ssh-kvm
sans verrou » (I1), « confirmation qui efface sans comparer » (I1),
« write_ref hors verrou » (I2), « marque posée en fin d'action » (I2),
« pas de seconde comparaison avant le remplacement » (I2).
"""

import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_secret_migrate_290 import (  # noqa: E402,F401
    CLASSES, KC, KEY, OIDC, PREFIX, appenv, bao_value, cli, env, fs, gate, hsc, kv, kvm_decl, migrate, sc, ss,
    store, wapp)


def settings(env):
    return json.loads((env["state"] / ss.SETTINGS_FILE).read_text())


def _held(path):
    """Vrai si un autre détenteur tient le verrou `path` (flock)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# I3 : une classe vide est marquée migrée, ses secrets nouveaux vont dans OpenBao
# ---------------------------------------------------------------------------
EMPTY = {
    "ssh-kvm": (lambda st: [p.unlink() for p in (st / "kvm.d").glob("*.yaml")], "k7", "private_key", KEY),
    "rancher-oidc": (lambda st: (st / "rancher.d" / "r1.oidc-secret").unlink(), "r7", "client_secret", OIDC),
    "tf-kvm": (lambda st: (st / "terraform" / ".kvm" / "k1" / "decls" / "d1" / ".secrets" / "tok").unlink(),
               "k1/d7/tok", "value", "t"),
}


@pytest.mark.parametrize("cls", list(EMPTY))
def test_empty_class_is_marked_and_new_secrets_go_to_openbao(env, capsys, cls):
    empty, path, field, value = EMPTY[cls]
    empty(env["state"])
    rc, doc, _ = migrate(cls, capsys)
    assert rc == 0 and doc["classes"][cls] == {"done": [], "errors": []}
    mark = settings(env)["migrated"][cls]
    assert mark["complete"] is True and mark["by"] == "alice"
    ref = store(env).put(cls, path, field, value)
    assert ref == ss.make_ref(cls, path, field, "openbao") and bao_value(env, cls, path, field) == value
    # le retour en local retire la marque d'une classe sans référence restante
    store(env).delete(ref)
    assert migrate(cls, capsys, "local")[0] == 0
    assert cls not in settings(env).get("migrated", {})


def test_class_with_an_outage_is_not_marked(env, capsys):
    """Aucun secret n'a pu migrer (panne) : aucune référence, la marque posée
    au début de l'action est retirée."""
    srv = env["srv"]
    real = srv._kv

    def cut(h, m, mount, rest, body, rec):
        if "/ssh-kvm/" in rest:
            return h._send(503, {"errors": ["Vault is sealed"]})
        return real(h, m, mount, rest, body, rec)
    srv._kv = cut
    rc, doc, _ = migrate("ssh-kvm", capsys)
    assert rc == 1 and doc["classes"]["ssh-kvm"]["errors"]
    assert "ssh-kvm" not in settings(env).get("migrated", {})


# ---------------------------------------------------------------------------
# I1 et I2 : la référence est posée sous le verrou de la déclaration
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cls,lock", [
    ("kubeconfig", "clusters.d/.lock"),
    ("ssh-cluster", "clusters.d/.lock"),
    ("ssh-kvm", "kvm.d/.lock"),
])
def test_reference_is_set_under_the_declaration_lock(env, capsys, monkeypatch, cls, lock):
    """Lecture, comparaison, pose et retrait sous le verrou partagé avec la
    console (CONFIG_LOCK) et harvester-kvm (host_lock) : une écriture
    intercalée attend au lieu d'être perdue."""
    seen = []
    real_norm, real_set = ss.FileStore._norm, ss.FileStore._set_decl_locked

    def norm(self, c, data):
        seen.append(("read", _held(env["state"] / lock)))
        return real_norm(self, c, data)

    def set_decl(self, c, path, decl, key, value):
        seen.append(("set", _held(env["state"] / lock)))
        return real_set(self, c, path, decl, key, value)
    monkeypatch.setattr(ss.FileStore, "_norm", norm)
    monkeypatch.setattr(ss.FileStore, "_set_decl_locked", set_decl)
    assert migrate(cls, capsys)[0] == 0
    assert {k for k, _h in seen} == {"read", "set"} and all(h for _k, h in seen), seen


def test_kvm_restore_is_under_the_kvm_lock(env, capsys, monkeypatch):
    assert migrate("ssh-kvm", capsys)[0] == 0
    seen = []
    real = ss.FileStore._set_decl_locked

    def set_decl(self, *a):
        seen.append(_held(env["state"] / "kvm.d" / ".lock"))
        return real(self, *a)
    monkeypatch.setattr(ss.FileStore, "_set_decl_locked", set_decl)
    assert migrate("ssh-kvm", capsys, "local")[0] == 0
    assert seen == [True] and kvm_decl.load(env["state"], "k1")["ssh_key"] == "ssh/k1_id"


@pytest.mark.parametrize("cls", ["rancher-oidc", "tf-kvm"])
def test_file_class_is_marked_before_the_first_secret(env, capsys, monkeypatch, cls):
    """Classes sans verrou partagé avec leurs écrivains : la marque est posée
    avant la première référence, les écrivains vont alors dans OpenBao."""
    doc = settings(env)
    doc["migrated"] = {}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    marks = []
    real = ss.FileStore.write_ref

    def probe(self, *a, **k):
        marks.append(cls in settings(env).get("migrated", {}))
        return real(self, *a, **k)
    monkeypatch.setattr(ss.FileStore, "write_ref", probe)
    assert migrate(cls, capsys)[0] == 0
    assert marks == [True]


def test_file_class_value_written_after_the_first_comparison_is_kept(env, capsys, monkeypatch):
    """Écriture intercalée après la première comparaison (copie `.migrated`
    déjà faite) : comparée encore avant le remplacement, rien n'est perdu
    et aucun original fantôme n'est laissé."""
    target = env["state"] / "rancher.d" / "r1.oidc-secret"
    real = kvm_decl.write_private

    def racing(path, data):
        out = real(path, data)
        if str(path).endswith(ss.MIGRATED_SUFFIX):
            real(target, "newer\n")
        return out
    monkeypatch.setattr(kvm_decl, "write_private", racing)
    rc, doc, _ = migrate("rancher-oidc", capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "local-reference-failed"
    assert target.read_text() == "newer\n"
    assert not target.with_name(target.name + ss.MIGRATED_SUFFIX).exists()
    assert sc.adapter("rancher-oidc").originals(fs(env)) == []


# ---------------------------------------------------------------------------
# I1 : la confirmation n'efface que l'original de la référence attendue
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("case,local,erased", [
    ("decl-rewritten-file-absent", None, False),          # édition KVM concurrente : clé effacée
    ("decl-rewritten-other-value", "other", False),
    ("decl-rewritten-same-value", "original", True),      # la valeur est toujours là : copie inutile
    ("reference", "ref", True),
])
def test_confirm_checks_the_local_value(env, capsys, case, local, erased):
    st = env["state"]
    assert migrate("ssh-kvm", capsys)[0] == 0
    mig = st / "ssh" / "k1_id.migrated"
    original = mig.read_text()
    if local != "ref":
        doc = kvm_decl.load(st, "k1")
        doc["ssh_key"] = "ssh/k1_id"
        kvm_decl.write(st, doc)
        if local is not None:
            kvm_decl.write_private(st / "ssh" / "k1_id", original if local == "original" else "other")
    rc, doc, _ = cli(["confirm", "--class", "ssh-kvm"], capsys)
    assert mig.exists() is not erased
    if erased:
        assert rc == 0 and doc["originals_erased"] == 1
    else:
        assert rc == 1 and doc["errors"] == [{"secret": "ssh-kvm/k1#private_key",
                                              "code": "original-kept-local-changed"}]


def test_confirm_db_class_reference_replaced(env, capsys):
    """replication-notify : la valeur rangée n'est plus la référence (réglage
    réécrit) -> original gardé."""
    import replication_db as rdb
    assert migrate("replication-notify", capsys)[0] == 0
    conn = rdb.connect(env["state"] / "replication.db")
    try:
        cfg = rdb.get_setting(conn, "notify")
        cfg["channels"][0]["token"] = "typed-again"
        rdb.set_setting(conn, "notify", cfg)
    finally:
        conn.close()
    rc, doc, _ = cli(["confirm", "--class", "replication-notify"], capsys)
    assert rc == 1 and {"secret": "replication-notify/global#mx_1__token",
                        "code": "original-kept-local-changed"} in doc["errors"]
    assert len(sc.adapter("replication-notify").originals(fs(env))) == 1


# ---------------------------------------------------------------------------
# M8 : chemins du registre des originaux
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside", "ssh/../../outside"])
def test_originals_registry_paths_stay_in_the_state(env, capsys, tmp_path, bad):
    assert migrate("ssh-kvm", capsys)[0] == 0
    victim = tmp_path / "outside"
    victim.write_text("keep")
    p = env["state"] / ss.ORIGINALS_FILE
    doc = json.loads(p.read_text())
    doc["ssh-kvm/k1#private_key"]["file"] = bad
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        fs(env).erase_original("ssh-kvm", "k1", "private_key")
    assert victim.read_text() == "keep"
