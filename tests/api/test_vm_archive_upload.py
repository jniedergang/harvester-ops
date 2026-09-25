"""v1.47.0 : ce qu'un export produit, et comment une archive revient.

Demandé par l'exploitant après un premier export depuis la fenêtre
« Migrer » : « comment est-ce que je récupère l'image ? comment la
réimporter ? ». Deux manques en sont sortis :

- la fin d'un export ne nommait pas l'archive : la console la nomme
  désormais elle-même et l'action la désigne dans son résultat ;
- une archive téléchargée ne pouvait revenir dans le magasin d'une autre
  console qu'en la recopiant à la main dans son répertoire. Elle se dépose
  maintenant par le navigateur, en flux (le corps de la requête est le
  fichier), et n'entre dans le magasin qu'une fois vérifiée.

Et un défaut trouvé en chemin : le bouton « annuler » du dock ne faisait
rien pour une action sans processus (téléchargement d'ISO, installation
bare-metal), faute de poser le drapeau que ces actions consultent.
"""

import io
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
os.environ.setdefault("HARVESTER_OPS_DISABLE_RATELIMIT", "1")
import app as wapp  # noqa: E402
import vm_transfer as vt  # noqa: E402


class _NoThread:
    started = []

    def __init__(self, target=None, args=(), **k):
        self.target, self.args = target, args

    def start(self):
        _NoThread.started.append(self.args)


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": [
        {"name": "harvlab", "kubeconfig": "/k/harvlab.yaml"}]})
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: {"harvlab": "/staged/a.yaml"}.get(c))
    monkeypatch.setattr(wapp, "EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setattr(wapp.threading, "Thread", _NoThread)
    _NoThread.started = []
    saved = dict(wapp.ACTIONS)
    wapp.ACTIONS.clear()
    wapp._UPLOADS.clear()
    yield wapp.app.test_client()
    wapp.ACTIONS.clear()
    wapp.ACTIONS.update(saved)
    wapp._UPLOADS.clear()


def _archive_bytes(tmp_path, name="web-01", disk=b"x" * 300_000):
    """Une vraie archive, écrite par l'écrivain du script."""
    path = tmp_path / f"src-{name}.hvx"
    path.unlink(missing_ok=True)
    w = vt.ArchiveWriter(path)
    w.add_json(vt.MANIFEST, {"format": 1, "created": "2026-09-25T00:00:00Z",
                             "source": {"cluster": "site-a", "version": "v1.8.2",
                                        "namespace": "default", "name": name},
                             "vm": {"metadata": {"name": name}},
                             "inventory": {"disks": [{"volume": "disk-0", "size": len(disk),
                                                      "member": "disks/disk-0.raw.gz"}],
                                           "networks": []},
                             "secrets": [{"name": "s", "data": {"userdata": "c2VjcmV0"}}]})
    w.add_stream("disks/disk-0.raw.gz", iter([disk]))
    w.close()
    return path.read_bytes()


def _put(client, name, data):
    return client.put(f"/api/exports/{name}", data=data,
                      content_type="application/octet-stream")


# -- la fin d'un export désigne son archive ---------------------------------

def test_an_export_names_its_archive_and_the_action_says_which(client):
    r = client.post("/api/vm/harvlab/default/web-01/transfer", json={"source": "running"})
    assert r.status_code == 201
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    archive = run.result["archive"]
    assert archive.startswith("web-01-") and archive.endswith(".hvx")
    assert wapp._export_safe_name(archive) == archive
    # le script écrit exactement ce fichier, dans le magasin
    cmd = _NoThread.started[0][1]
    assert cmd[cmd.index("--out") + 1] == str(wapp.EXPORT_DIR / archive)
    # l'Activité montre le nom, jamais le répertoire
    assert run.cmd[run.cmd.index("--out") + 1] == archive
    assert not any(str(wapp.EXPORT_DIR) in a for a in run.cmd)
    assert run.to_dict()["result"] == {"archive": archive}


def test_a_move_to_another_cluster_has_no_archive(client, monkeypatch):
    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: {"harvlab": "/staged/a.yaml", "other": "/staged/b.yaml"}.get(c))
    r = client.post("/api/vm/harvlab/default/web-01/transfer", json={"to": "other"})
    assert r.status_code == 201
    assert wapp.ACTIONS[r.get_json()["action_id"]].result == {}


# -- dépôt d'une archive ----------------------------------------------------

def test_an_archive_is_uploaded_checked_and_kept(client, tmp_path):
    data = _archive_bytes(tmp_path)
    r = _put(client, "web-01-copy.hvx", data)
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["archive"] == "web-01-copy.hvx" and body["size"] == len(data)
    dest = wapp.EXPORT_DIR / "web-01-copy.hvx"
    assert dest.read_bytes() == data
    # elle porte les secrets cloud-init : lisible du seul service
    assert oct(dest.stat().st_mode & 0o777) == "0o600"
    assert not list(wapp.EXPORT_DIR.glob("*.part"))
    # suivie comme une action : envoi puis vérification, avec leurs bilans
    run = wapp.ACTIONS[body["action_id"]]
    assert run.action == "vm-archive-upload:web-01-copy.hvx" and run.cluster == "(local)"
    assert run.status == "done" and run.result == {"archive": "web-01-copy.hvx"}
    assert run.progress["upload"]["final"] and run.progress["upload"]["done"] == len(data)
    assert run.progress["verify"]["final"]
    steps = {(e["step_id"], e["status"]) for e in run.events if e.get("type") == "step"}
    assert ("upload", "done") in steps and ("verify", "done") in steps
    # et le magasin la montre, complète, importable
    listed = {e["name"]: e for e in client.get("/api/exports").get_json()["exports"]}
    assert listed["web-01-copy.hvx"]["complete"] is True
    assert listed["web-01-copy.hvx"]["cluster"] == "site-a"


@pytest.mark.parametrize("name", ["web.tar", "..hvx", ".hidden.hvx", "a b.hvx"])
def test_an_odd_name_is_refused_before_anything_is_written(client, name):
    r = _put(client, name, b"x" * 10)
    assert r.status_code in (400, 404)
    assert not wapp.EXPORT_DIR.exists() or not list(wapp.EXPORT_DIR.iterdir())


def test_an_archive_never_replaces_another(client, tmp_path):
    data = _archive_bytes(tmp_path)
    assert _put(client, "a.hvx", data).status_code == 201
    r = _put(client, "a.hvx", data + b"")
    assert r.status_code == 409 and "already in the store" in r.get_json()["error"]


def test_two_uploads_of_the_same_name_do_not_share_a_file(client, tmp_path):
    wapp._UPLOADS.add("a.hvx")
    r = _put(client, "a.hvx", _archive_bytes(tmp_path))
    assert r.status_code == 409


def test_no_room_is_said_before_receiving(client, tmp_path, monkeypatch):
    data = _archive_bytes(tmp_path)

    class St:
        f_bavail, f_frsize = 10, 4096
    monkeypatch.setattr(wapp.os, "statvfs", lambda p: St())
    r = _put(client, "a.hvx", data)
    assert r.status_code == 507
    body = r.get_json()
    assert body["need"] == len(data) and body["free"] == 40960
    assert not wapp.ACTIONS


def _flip_in_disk(data, tmp_path):
    """Change un octet au milieu du disque : même taille, contenu faux."""
    probe = tmp_path / "probe.hvx"
    probe.write_bytes(data)
    off, size = vt.ArchiveReader(probe).member("disks/disk-0.raw.gz")
    i = off + size // 2
    return data[:i] + bytes([data[i] ^ 0xFF]) + data[i + 1:]


@pytest.mark.parametrize("damage, expect", [
    (lambda d, t: b"not a tar at all" * 100, "incomplete archive"),
    # coupée avant la liste des sommes : un export interrompu
    (lambda d, t: d[:2048], "incomplete archive"),
    # un octet du disque changé, à la bonne taille : le cas vécu
    (_flip_in_disk, "checksum mismatch in disks/disk-0.raw.gz"),
])
def test_a_damaged_archive_is_refused_and_not_kept(client, tmp_path, damage, expect):
    good = _archive_bytes(tmp_path)
    data = damage(good, tmp_path)
    assert data != good
    r = _put(client, "bad.hvx", data)
    assert r.status_code == 422, r.get_json()
    assert expect in r.get_json()["error"]
    assert not list(wapp.EXPORT_DIR.iterdir())
    run = wapp.ACTIONS[r.get_json()["action_id"]]
    assert run.status == "error" and expect in run.error_summary


def test_a_cancelled_upload_keeps_nothing(client, tmp_path, monkeypatch):
    def cancelled(run, stream, length, part):
        part.write_bytes(b"partial")
        raise wapp._UploadCancelled()
    monkeypatch.setattr(wapp, "_receive_archive", cancelled)
    r = _put(client, "a.hvx", _archive_bytes(tmp_path))
    assert r.status_code == 409 and r.get_json()["error"] == "cancelled"
    assert not list(wapp.EXPORT_DIR.iterdir())
    assert wapp.ACTIONS[r.get_json()["action_id"]].status == "cancelled"
    assert "a.hvx" not in wapp._UPLOADS


def test_an_error_names_no_path(client, tmp_path, monkeypatch):
    def full(run, stream, length, part):
        raise OSError(28, "No space left on device", str(part))
    monkeypatch.setattr(wapp, "_receive_archive", full)
    r = _put(client, "a.hvx", _archive_bytes(tmp_path))
    assert r.status_code == 400
    assert r.get_json()["error"] == "No space left on device"
    assert str(wapp.EXPORT_DIR) not in r.get_data(as_text=True)


def test_a_leftover_from_a_crash_is_removed_but_not_a_live_upload(client, tmp_path):
    wapp._export_dir()
    (wapp.EXPORT_DIR / "old.hvx.part").write_bytes(b"left by a crash")
    (wapp.EXPORT_DIR / "live.hvx.part").write_bytes(b"being written")
    wapp._UPLOADS.add("live.hvx")
    assert _put(client, "a.hvx", _archive_bytes(tmp_path)).status_code == 201
    assert not (wapp.EXPORT_DIR / "old.hvx.part").exists()
    assert (wapp.EXPORT_DIR / "live.hvx.part").exists()


# -- la réception elle-même -------------------------------------------------

def _run():
    run = wapp.ActionRun("up000000test", "vm-archive-upload:a.hvx", "(local)", [])
    run.close = lambda: None
    return run


def test_reception_stops_when_the_browser_does(tmp_path):
    run = _run()
    part = tmp_path / "a.hvx.part"
    with pytest.raises(ValueError, match="stopped at 10 of 100 bytes"):
        wapp._receive_archive(run, io.BytesIO(b"x" * 10), 100, part)


def test_reception_obeys_the_dock(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_UPLOAD_CHUNK", 4)
    run = _run()

    class Stream:
        def __init__(self):
            self.n = 0

        def read(self, size):
            self.n += 1
            if self.n == 2:
                run._cancel = True           # l'exploitant clique pendant l'envoi
            return b"y" * size
    with pytest.raises(wapp._UploadCancelled):
        wapp._receive_archive(run, Stream(), 100, tmp_path / "a.hvx.part")


def test_reception_writes_the_file_private_and_reports_progress(tmp_path):
    run = _run()
    part = tmp_path / "a.hvx.part"
    res = wapp._receive_archive(run, io.BytesIO(b"z" * 5000), 5000, part)
    assert part.read_bytes() == b"z" * 5000
    assert oct(part.stat().st_mode & 0o777) == "0o600"
    assert res["done"] == 5000 and run.progress["upload"]["final"]


# -- le dock annule une action sans processus -------------------------------

def test_the_dock_cancels_an_action_without_a_process(client):
    run = wapp.ActionRun("iso000000001", "iso-fetch:x.iso", "(local)", [])
    run.status = "running"
    wapp.ACTIONS[run.id] = run
    r = client.delete(f"/api/action/{run.id}")
    assert r.status_code == 200
    assert run._cancel is True
    # l'action s'arrête d'elle-même : son statut n'est pas forcé ici
    assert run.status == "running"


def test_the_dock_leaves_a_finished_action_alone(client):
    run = wapp.ActionRun("iso000000002", "iso-fetch:x.iso", "(local)", [])
    run.status = "done"
    wapp.ACTIONS[run.id] = run
    client.delete(f"/api/action/{run.id}")
    assert not getattr(run, "_cancel", False)


# -- la vérification suit sa progression ------------------------------------

def test_verify_reports_every_byte_it_reads(tmp_path):
    data = _archive_bytes(tmp_path, disk=b"q" * 3_000_000)
    path = tmp_path / "v.hvx"
    path.write_bytes(data)
    reader = vt.ArchiveReader(path)
    seen = []
    assert reader.verify(on_chunk=seen.append) == []
    expected = sum(size for name, (_, size) in reader.members.items() if name in reader.sums())
    assert sum(seen) == expected and len(seen) > 2


def test_a_cancelled_iso_download_reads_cancelled(tmp_path, monkeypatch):
    """Vécu en réel : le téléchargement s'arrêtait bien, mais se lisait
    « erreur » dans le dock et l'Activité."""
    run = _run()

    class Resp:
        headers = {"Content-Length": "100"}

        def read(self, n):
            run._cancel = True
            return b"i" * 10

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    dest = tmp_path / "x.iso"
    wapp._iso_fetch_runner(run, "http://example.invalid/x.iso", dest)
    assert run.status == "cancelled" and run.exit_code == 3
    assert not dest.exists() and not list(tmp_path.iterdir())


def test_a_failed_iso_download_still_reads_error(tmp_path, monkeypatch):
    run = _run()
    import urllib.request

    def boom(*a, **k):
        raise OSError("unreachable")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    wapp._iso_fetch_runner(run, "http://example.invalid/x.iso", tmp_path / "x.iso")
    assert run.status == "error" and run.exit_code == 1


def test_a_cancelled_bare_metal_install_reads_cancelled(monkeypatch):
    run = _run()
    run._cancel = True
    monkeypatch.setattr(wapp, "_bmc_discover_one",
                        lambda *a, **k: {"ok": False, "error": "cancelled by operator"})
    wapp._baremetal_install_runner(run, {"bmc_user": "u", "bmc_password": "p",
                                         "bmc_host": "192.0.2.1"})
    assert run.status == "cancelled" and run.exit_code == 3
    ends = [e for e in run.events if e.get("type") == "status"]
    assert ends[-1]["status"] == "cancelled"
