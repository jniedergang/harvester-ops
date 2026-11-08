"""2.9.0 tâche 11 : instantanés de B1 (agent de l'hôte et console).

Agent : restauration durcie (revue M2 de la seconde passe) : répertoire réglé
ouvert sans suivre de lien, tube nommé refusé sans blocage, fichier à un seul
lien, taille bornée, clé tirée du nom (plus de la date), instantané fabriqué
avec la clé exportée refusé ; instantané d'avant la mise à jour (décision 277
A) ; état lisible par la console (`health.json`).

Mutations jouées (rapport de la tâche 11) : « revue de l'instantané restauré
sautée », « O_NONBLOCK retiré » (le tube bloque : délai du test), « clé
choisie par la date malgré le nom », « nlink non contrôlé », « instantané
d'avant la mise à jour pris après le changement d'image ».
"""

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_secrets_agent_290 import CID, bundle, env, hsa, install, request, settings  # noqa: E402,F401

import openbao_policy as pol  # noqa: E402


def _snap(dirpath, name, data=b"SNAP" * 8):
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / name
    f.write_bytes(data)
    return f


def _console_dir(env, tmp_path):
    d = tmp_path / "nfs" / "openbao-snapshots"
    d.mkdir(parents=True)
    settings(env, snapshot_dir=str(d))
    return d


# ---------------------------------------------------------------------------
# Fichier de l'instantané (revue M2)
# ---------------------------------------------------------------------------
def test_restore_from_the_console_snapshot_dir(env, tmp_path):
    install(env)
    d = _console_dir(env, tmp_path)
    key = env["agent"]().agent_state()["current_key"]
    _snap(d, f"openbao-20261108T023000Z-{key}.snap", b"FROM-NFS")
    rc, st = request(env, op="restore-snapshot", file=f"openbao-20261108T023000Z-{key}.snap")
    assert rc == 0, st
    assert env["srv"].restored[-1] == b"FROM-NFS"


@pytest.mark.parametrize("make,code", [
    ("fifo", "snapshot-not-plain"),
    ("hardlink", "snapshot-not-plain"),
    ("symlink", "snapshot-missing"),
    ("dir-symlink", "snapshot-missing"),
    ("too-big", "snapshot-too-big"),
    ("empty", "snapshot-empty"),
])
def test_restore_refuses_what_is_not_a_plain_snapshot(env, tmp_path, monkeypatch, make, code):
    install(env)
    d = _console_dir(env, tmp_path)
    name = "openbao-20261108T023000Z.snap"
    if make == "fifo":
        os.mkfifo(d / name)
    elif make == "hardlink":
        _snap(tmp_path, "other.snap")
        os.link(tmp_path / "other.snap", d / name)
    elif make == "symlink":
        _snap(tmp_path, "secret")
        (d / name).symlink_to(tmp_path / "secret")
    elif make == "dir-symlink":
        real = tmp_path / "real"
        _snap(real, name)
        d.rmdir()
        d.symlink_to(real)
    elif make == "too-big":
        monkeypatch.setattr(hsa, "SNAP_MAX", 16)
        _snap(d, name, b"x" * 17)
    else:
        _snap(d, name, b"")
    done = {}
    t = threading.Thread(target=lambda: done.update(r=request(env, op="restore-snapshot", file=name)), daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "the agent blocked on the snapshot file"
    rc, st = done["r"]
    assert rc == 1 and st["code"] == code, st
    assert not env["srv"].restored
    assert not env["host"].ran("systemctl", "stop", hsa.UNIT)


def test_restore_stops_a_snapshot_that_grows(env, tmp_path, monkeypatch):
    """La taille relue sur le descripteur borne la copie : un fichier qui
    grandit pendant la copie est refusé (il remplirait /var)."""
    install(env)
    d = _console_dir(env, tmp_path)
    f = _snap(d, "openbao-20261108T023000Z.snap", b"A" * (3 << 20))
    real_fstat = os.fstat

    def small_fstat(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode) and st.st_size == f.stat().st_size:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                   1 << 20, st.st_atime, st.st_mtime, st.st_ctime))
        return st
    monkeypatch.setattr(hsa.os, "fstat", small_fstat)
    rc, st = request(env, op="restore-snapshot", file=f.name)
    assert rc == 1 and st["code"] == "snapshot-changing"


def test_the_key_comes_from_the_name_not_from_the_date(env, tmp_path):
    """La date d'un fichier copié (NFS, rsync sans -t) ne choisit plus la
    clé : le nom la porte. Fichier daté d'avant la rotation, nommé avec la
    nouvelle clé : restauré sans previous_key ni rotate/root."""
    install(env)
    env["clock"]["t"] += 86400
    request(env, op="rotate-unseal-key")
    st0 = env["agent"]().agent_state()
    old, new = st0["keys"][0], st0["current_key"]
    d = _console_dir(env, tmp_path)
    f = _snap(d, f"openbao-20261109T023000Z-{new}.snap")
    os.utime(f, (old["created"] + 10,) * 2)
    rotations = len([e for e in env["srv"].events if e[0] == "rotate-root"])
    rc, st = request(env, op="restore-snapshot", file=f.name)
    assert rc == 0, st
    assert len([e for e in env["srv"].events if e[0] == "rotate-root"]) == rotations
    assert not any(s["id"] == "rotate-root" for s in st["steps"])


def test_a_named_key_no_longer_kept_is_refused_before_anything_stops(env, tmp_path):
    install(env)
    d = _console_dir(env, tmp_path)
    _snap(d, "openbao-20261108T023000Z-hops-20200101-9.snap")
    rc, st = request(env, op="restore-snapshot", file="openbao-20261108T023000Z-hops-20200101-9.snap")
    assert rc == 1 and st["code"] == "snapshot-key-unknown"
    assert not env["host"].ran("systemctl", "stop", hsa.CONSOLE_UNIT)


# ---------------------------------------------------------------------------
# Instantané fabriqué avec la clé exportée (revue M2) : refusé, volume remis
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("craft", ["foreign-token", "root-token", "console-admin"])
def test_a_crafted_snapshot_is_refused_and_the_volume_put_back(env, craft):
    install(env)
    p, srv = env["p"], env["srv"]
    marker = p.b1_data / "data" / "raft.db"
    marker.write_text("live")
    snap = _snap(p.b1_data / "snapshots", "openbao-20261108T020000Z.snap")

    class Crafted(list):
        def append(self, raw):
            super().append(raw)
            if craft == "foreign-token":
                srv.add_token(policies=("default", "evil"))
            elif craft == "root-token":
                srv.add_token(policies=("root",))
            else:
                srv.crafted_admin = True
    srv.restored = Crafted()
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 1 and st["code"] == "snapshot-untrusted", st
    assert marker.read_text() == "live"
    assert not list(p.b1_data.parent.glob(p.b1_data.name + ".old-*"))
    assert env["host"].ran("systemctl", "start", hsa.CONSOLE_UNIT)
    blob = json.dumps(st)
    assert not any(t in blob for t in srv.tokens)


def test_a_genuine_snapshot_passes_the_review(env):
    install(env)
    p, srv = env["p"], env["srv"]
    snap = _snap(p.b1_data / "snapshots", "openbao-20261108T020000Z.snap")
    rc, st = request(env, op="restore-snapshot", file=snap.name)
    assert rc == 0, st
    review = [s for s in st["steps"] if s["id"] == "review"]
    assert review and review[-1]["status"] == "done"
    # politiques de l'agent remises AVANT la revue (sinon un instantané qui
    # lui retire ses droits de lecture passerait la revue sans rien voir)
    names = [e for e in srv.events if e[0] == "policy-post"]
    assert (("policy-post", pol.agent_role(CID)) in names)


def test_the_review_reads_policies_never_token_values(env):
    install(env)
    srv = env["srv"]
    snap = _snap(env["p"].b1_data / "snapshots", "openbao-20261108T020000Z.snap")
    request(env, op="restore-snapshot", file=snap.name)
    assert srv.requests("auth/token/lookup-accessor")
    assert not [r for r in srv.log if r["path"] == "auth/token/lookup"]       # lookup par jeton : jamais


# ---------------------------------------------------------------------------
# b1-upgrade : instantané d'avant la mise à jour (décision 277 A)
# ---------------------------------------------------------------------------
def test_b1_upgrade_takes_a_pre_upgrade_snapshot_before_the_image(env):
    install(env)
    p, host = env["p"], env["host"]
    seen = {}
    real_run = host.run

    def run(argv, *a, **kw):
        if argv[:2] == ["podman", "tag"] and "snaps" not in seen:
            seen["snaps"] = sorted(x.name for x in (p.b1_data / "snapshots").glob("*pre-upgrade.snap"))
        return real_run(argv, *a, **kw)
    host.run = run
    rc, st = request(env, op="b1-upgrade", bundle=env["bundle"].name)
    assert rc == 0, st
    key = env["agent"]().agent_state()["current_key"]
    assert seen["snaps"] and seen["snaps"][0].endswith(f"-{key}-pre-upgrade.snap")
    assert hsa.SNAP_RE.match(seen["snaps"][0])


def test_the_pre_upgrade_snapshot_restores(env):
    install(env)
    request(env, op="b1-upgrade", bundle=env["bundle"].name)
    name = next(x.name for x in (env["p"].b1_data / "snapshots").glob("*pre-upgrade.snap"))
    rc, st = request(env, op="restore-snapshot", file=name)
    assert rc == 0, st


@pytest.mark.parametrize("snaps,now_days,gone", [
    # le dernier instantané de chaque motif reste, même vieux
    ([("openbao-20260101T000000Z-hops-20260101-1-pre-upgrade.snap", 0)], 100, []),
    ([("openbao-20260101T000000Z-hops-20260101-1-pre-upgrade.snap", 0),
      ("openbao-20260201T000000Z-hops-20260101-1-pre-upgrade.snap", 31)], 100,
     ["openbao-20260101T000000Z-hops-20260101-1-pre-upgrade.snap"]),
    # moins de 14 jours : gardés
    ([("openbao-a-hops-20260101-1-rotation.snap", 0), ("openbao-b-hops-20260101-2-rotation.snap", 5)], 10, []),
    ([("openbao-a-hops-20260101-1-rotation.snap", 0), ("openbao-b-hops-20260101-2-rotation.snap", 5),
      ("openbao-c-hops-20260101-1-pre-upgrade.snap", 1)], 30, ["openbao-a-hops-20260101-1-rotation.snap"]),
])
def test_agent_snapshot_retention(snaps, now_days, gone):
    day = 86400
    assert sorted(hsa._agent_snapshot_prune([(n, d * day) for n, d in snaps], now_days * day)) == sorted(gone)


# ---------------------------------------------------------------------------
# health.json : lu par la console, sans valeur
# ---------------------------------------------------------------------------
def test_health_written_after_each_pass_without_values(env):
    install(env)
    p, srv = env["p"], env["srv"]
    h = json.loads((p.state / hsa.HEALTH).read_text())
    st = env["agent"]().agent_state()
    assert h["b1_installed"] and h["current_key"] == st["current_key"]
    assert h["audit"]["total"] > 0 and h["audit"]["free"] >= 0
    assert oct(os.stat(p.state / hsa.HEALTH).st_mode & 0o777) == "0o644"
    blob = json.dumps(h)
    for v in [s for r in srv.roles.values() for s in r["secret_ids"]] + list(srv.tokens):
        assert v not in blob
    for k in (p.b1_etc / "keys").glob("*.key"):
        assert k.read_bytes().hex() not in blob
    # passage de la minuterie (sans demande) : réécrit
    (p.state / hsa.HEALTH).unlink()
    env["agent"]().process()
    assert (p.state / hsa.HEALTH).exists()


def test_health_lists_agent_snapshots(env):
    install(env)
    env["clock"]["t"] += 86400
    request(env, op="rotate-unseal-key")
    h = json.loads((env["p"].state / hsa.HEALTH).read_text())
    assert any(s["file"].endswith("-rotation.snap") for s in h["snapshots"])


# ---------------------------------------------------------------------------
# harvester-secrets snapshot / snapshots / settings (console, sans root)
# ---------------------------------------------------------------------------
import test_harvester_secrets_cli_290 as tcli  # noqa: E402
from test_harvester_secrets_cli_290 import cli, creds, read_settings  # noqa: E402

cli_env = tcli.env


@pytest.fixture
def b1(cli_env, tmp_path, monkeypatch):
    # la console lance l'outil sous son compte (snapshot refusé en root)
    monkeypatch.setattr(tcli.hsc, "_is_root", lambda: False)
    creds(cli_env)
    d = tmp_path / "nfs-snaps"
    d.mkdir()
    tcli.settings(cli_env, mode="b1", snapshot_dir=str(d))
    agent_dir = cli_env["state"] / "secrets-agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "health.json").write_text(json.dumps({"current_key": "hops-20261108-1"}))
    return {**cli_env, "dir": d}


def test_snapshot_written_0600_with_the_key_in_its_name(b1, capsys):
    rc, doc, err = cli(["--actor", "service", "snapshot"], capsys=capsys)
    assert rc == 0, (doc, err)
    f = b1["dir"] / doc["file"]
    assert doc["file"].endswith("-hops-20261108-1.snap") and hsa.SNAP_KEY_RE.search(doc["file"])
    assert f.read_bytes() == b1["srv"].snapshot and oct(f.stat().st_mode & 0o777) == "0o600"
    last = read_settings(b1)["snapshots"]
    assert last["last_file"] == doc["file"] and last["last_error"] is None


def test_snapshot_retention_keeps_14(b1, capsys):
    for i in range(14):
        (b1["dir"] / f"openbao-202610{i + 10:02d}T023000Z-hops-20261001-1.snap").write_bytes(b"x")
    (b1["dir"] / "notes.txt").write_text("kept")
    rc, doc, _ = cli(["snapshot"], capsys=capsys)
    assert rc == 0
    files = sorted(p.name for p in b1["dir"].glob("openbao-*.snap"))
    assert len(files) == 14 and "openbao-20261010T023000Z-hops-20261001-1.snap" not in files
    assert doc["pruned"] == ["openbao-20261010T023000Z-hops-20261001-1.snap"]
    assert (b1["dir"] / "notes.txt").exists()


def test_snapshot_failure_is_recorded(b1, capsys):
    b1["srv"].sealed = True
    rc, doc, err = cli(["snapshot"], capsys=capsys)
    assert rc == 1 and doc["code"] == "snapshot-failed"
    last = read_settings(b1)["snapshots"]
    assert last["last_error"] == "openbao-sealed" and last["last_error_at"]


@pytest.mark.parametrize("where,code", [("state", "snapshot-in-state-dir"), ("relative", "bad-input"),
                                        ("missing", "bad-input")])
def test_snapshot_never_in_the_state_directory(b1, capsys, where, code):
    dest = {"state": str(b1["state"] / "snaps"), "relative": "snaps", "missing": "/nonexistent/x"}[where]
    if where == "state":
        (b1["state"] / "snaps").mkdir()
    rc, doc, _ = cli(["snapshot", "--dest", dest], capsys=capsys)
    assert rc == 2 and doc["code"] == code


def test_snapshots_lists_both_places_without_values(b1, capsys):
    (b1["dir"] / "openbao-20261101T023000Z-hops-20261101-1.snap").write_bytes(b"x")
    (b1["state"] / "secrets-agent" / "health.json").write_text(json.dumps({
        "current_key": "hops-20261108-1", "snapshots": [{"file": "openbao-a-hops-20261108-1-pre-upgrade.snap",
                                                          "bytes": 3, "at": 1}]}))
    rc, doc, _ = cli(["snapshots"], capsys=capsys)
    assert rc == 0 and [f["file"] for f in doc["files"]] == ["openbao-20261101T023000Z-hops-20261101-1.snap"]
    assert doc["agent_files"][0]["file"].endswith("pre-upgrade.snap") and doc["keep"] == 14


@pytest.mark.parametrize("item,code", [
    ("cache_ttl_min=0", "bad-input"), ("cache_ttl_min=121", "bad-input"), ("snapshot_keep=x", "bad-input"),
    ("snapshot_time=25:00", "bad-input"), ("nope=1", "bad-input"), ("snapshot_dir=relative", "bad-input"),
])
def test_settings_refusals(b1, capsys, item, code):
    rc, doc, _ = cli(["settings", "--set", item], capsys=capsys)
    assert rc == 2 and doc["code"] == code


def test_settings_set_and_state_dir_refused(b1, capsys, tmp_path):
    rc, doc, _ = cli(["settings", "--set", "snapshot_time=03:15", "--set", "snapshot_keep=30",
                      "--set", "cache_ttl_min=30"], capsys=capsys)
    assert rc == 0 and doc["settings"]["snapshot_time"] == "03:15" and doc["settings"]["snapshot_keep"] == 30
    (b1["state"] / "inside").mkdir()
    rc, doc, _ = cli(["settings", "--set", f"snapshot_dir={b1['state'] / 'inside'}"], capsys=capsys)
    assert rc == 2 and doc["code"] == "snapshot-in-state-dir"
    rc, doc, _ = cli(["settings", "--set", "snapshot_dir="], capsys=capsys)
    assert rc == 0 and doc["settings"]["snapshot_dir"] is None


# ---------------------------------------------------------------------------
# Secrets orphelins (rapport de la tâche 8, point 2)
# ---------------------------------------------------------------------------
def _tf_kvm(b1, used=("h1/d1/token",), orphans=("h1/d2/token",), created="2026-01-01T00:00:00Z"):
    tf = b1["state"].parent / "tfws"
    for u in used:
        host, decl, var = u.split("/")
        f = tf / ".kvm" / host / "decls" / decl / ".secrets" / var
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"openbao:tf-kvm/{u}#value")
    for u in list(used) + list(orphans):
        b1["srv"].put_raw("harvester-ops", f"{tcli.PREFIX}/tf-kvm/{u}", {"value": "v-" + u})
    return tf


def test_orphan_sweep_removes_only_unreferenced_old_secrets(b1, capsys, monkeypatch):
    tcli.settings(b1, mode="a")
    tf = _tf_kvm(b1)
    monkeypatch.setattr(tcli.hsc.time, "time", lambda: 1794150000.0)    # fake : écrits le 2026-11-08
    rc, doc, _ = cli(["sweep-orphans", "--class", "tf-kvm", "--tf-dir", str(tf), "--dry-run"], capsys=capsys)
    assert rc == 0 and [o["path"] for o in doc["orphans"]] == ["h1/d2/token"]
    assert doc["orphans"][0]["sweepable"] is False                      # écrit il y a moins de 7 jours
    monkeypatch.setattr(tcli.hsc.time, "time", lambda: 1794150000.0 + 30 * 86400)
    rc, doc, err = cli(["sweep-orphans", "--class", "tf-kvm", "--tf-dir", str(tf)], capsys=capsys)
    assert rc == 0 and doc["removed"] == ["tf-kvm/h1/d2/token"]
    kv = b1["srv"].kv
    assert ("harvester-ops", f"{tcli.PREFIX}/tf-kvm/h1/d2/token") not in kv
    assert ("harvester-ops", f"{tcli.PREFIX}/tf-kvm/h1/d1/token") in kv
    assert "v-h1" not in err + json.dumps(doc)


@pytest.mark.parametrize("case,why", [("no-source", "local source not found"),
                                      ("no-local-ref", "no local reference seen for this class")])
def test_orphan_sweep_never_trusts_a_missing_or_empty_source(b1, capsys, monkeypatch, case, why):
    tcli.settings(b1, mode="a")
    tf = _tf_kvm(b1, used=() if case == "no-local-ref" else ("h1/d1/token",))
    if case == "no-local-ref":
        (tf / ".kvm").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tcli.hsc.time, "time", lambda: 1794150000.0 + 30 * 86400)
    arg = str(tf) if case == "no-local-ref" else str(b1["state"].parent / "nowhere")
    rc, doc, _ = cli(["sweep-orphans", "--class", "tf-kvm", "--tf-dir", arg], capsys=capsys)
    assert rc == 0 and doc["removed"] == []
    assert all(o["kept_because"] == why for o in doc["orphans"]) and doc["orphans"]


# Relecture finale I8 : un refus avant de commencer (répertoire disparu,
# NFS démonté) est rangé comme un échec ; sans cela le fil de fond relançait
# une action toutes les 5 minutes et l'avertissement ne paraissait jamais.
@pytest.mark.parametrize("how,code", [("dir-gone", "bad-input"), ("unexpected", "RuntimeError")])
def test_a_snapshot_refused_before_starting_is_recorded(b1, capsys, monkeypatch, how, code):
    if how == "dir-gone":
        b1["dir"].rmdir()
    else:
        def boom(*a, **k):
            raise RuntimeError("x")
        monkeypatch.setattr(tcli.hsc, "admin_client", boom)
    if how == "unexpected":
        with pytest.raises(RuntimeError):
            cli(["snapshot"], capsys=capsys)
    else:
        rc, doc, _ = cli(["snapshot"], capsys=capsys)
        assert rc != 0
    last = read_settings(b1)["snapshots"]
    assert last["last_error"] == code and last["last_error_at"]


@pytest.mark.parametrize("keep", ["0", "-100", "366"])
def test_snapshot_keep_is_bounded(b1, capsys, keep):
    (b1["dir"] / "openbao-20261010T023000Z-hops-20261001-1.snap").write_bytes(b"x")
    rc, doc, _ = cli(["snapshot", "--keep", keep], capsys=capsys)
    assert rc == 2 and doc["code"] == "bad-input"
    assert (b1["dir"] / "openbao-20261010T023000Z-hops-20261001-1.snap").exists()
