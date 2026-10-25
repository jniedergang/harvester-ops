"""Prochaine version KVM, tâche 4 : corrections après la revue des routes.

- Téléchargement : règles du repli alignées sur la garde partagée 1.99.0
  (f7bb19c) : identifiants refusés avant tout appel réseau et jamais
  recopiés, pas de https vers http, métadonnées / non spécifiée / multicast /
  diffusion toujours refusées même couvertes par iso_fetch.allow_private,
  SHA-256 exigée en http simple ; garde de livraison (le repli devient
  inatteignable, puis interdit, dès que le module partagé existe).
- Place sur la console : réservations des réceptions en cours sous verrou,
  iso_fetch.min_free_bytes, revérification pendant l'écriture.
- Mineures : nettoyage des restes au démarrage, limites et clé partagée des
  lectures coûteuses, verrous des pools et réseaux cités, modèles (secrets,
  propriétaire, étiquette propre), nom obligatoire, compteur de génération
  abîmé, journal sans URL complète."""

import logging
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_kvm_routes_196 import (  # noqa: E402,F401 : fixtures partagées
    ROOT, SRC, _NoThread, as_user, client, env, only_run, script_cmd, wapp)
from test_kvm_routes_create import (  # noqa: E402,F401
    CONFIG, DIGEST, SHA, SPEC, FakePopen, _Handler, base, holder, http_server, lib, staged, upload)

ko = wapp._ko


def fetch(client, url, sha=SHA, **extra):
    body = dict({"url": url, "pool": "images", "name": "c.img"}, **extra)
    if sha:
        body["sha256"] = sha
    return client.post("/api/kvm/hosts/node2/images/fetch", json=body)


def no_dns(monkeypatch):
    monkeypatch.setattr(wapp.socket, "getaddrinfo", lambda *a, **k: pytest.fail("network call"))


# --- 1. téléchargement : règles alignées sur la garde 1.99.0 -----------------------------------

@pytest.mark.parametrize("url", ["http://user:s3cr3tpw@example.org/x.img",
                                 "https://s3cr3tpw@mirror.example/x.img",
                                 "https://:s3cr3tpw@mirror.example/x.img",
                                 "https://@mirror.example/x.img"])
def test_fetch_refuses_credentials_before_any_network_call(client, upload, monkeypatch, url):
    no_dns(monkeypatch)
    r = fetch(client, url)
    assert r.status_code == 400 and r.get_json()["reason"] == "credentials", r.get_json()
    assert "s3cr3tpw" not in r.get_data(as_text=True) and "user" not in r.get_json()["error"]
    assert wapp.ACTIONS == {} and _NoThread.started == []


BROAD = {"iso_fetch": {"allow_private": ["0.0.0.0/0", "::/0"]}}


@pytest.mark.parametrize("url,why", [
    ("http://169.254.169.254/latest/meta-data", "metadata"),
    ("http://[fd00:ec2::254]/x", "metadata"),
    ("http://[::ffff:169.254.169.254]/x", "metadata"),
    ("http://0.0.0.0/x", "unspecified"),
    ("http://[::]/x", "unspecified"),
    ("http://224.0.0.1/x", "multicast"),
    ("http://[ff02::1]/x", "multicast"),
    ("http://255.255.255.255/x", "broadcast"),
])
def test_always_refused_addresses_even_with_a_broad_allowlist(client, upload, monkeypatch, url, why):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: BROAD)
    r = fetch(client, url)
    assert r.status_code == 400 and r.get_json()["reason"] == "address", r.get_json()
    assert why in r.get_json()["error"] and "never reachable" in r.get_json()["error"]
    assert "iso_fetch.allow_private" not in r.get_json()["error"]
    assert wapp.ACTIONS == {} and _NoThread.started == []


def test_the_allowlist_still_opens_loopback_and_private(client, upload, monkeypatch):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: BROAD)
    assert fetch(client, "http://127.0.0.1:9/x").status_code == 202


def test_a_redirect_to_the_metadata_address_is_refused_with_a_broad_allowlist(client, upload, monkeypatch,
                                                                              http_server):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: BROAD)
    monkeypatch.setattr(_Handler, "redirect", "http://169.254.169.254/latest/meta-data/")
    assert fetch(client, f"{http_server}/redir").status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "error" and "metadata" in run.error_summary
    assert FakePopen.calls == [] and staged(upload) == []


def test_a_redirect_from_https_to_http_is_refused_by_the_shared_guard(tmp_path, monkeypatch):
    """La route passe par la garde partagée : son suivi des redirections
    refuse le retour à http (connexion simulée, résolution publique)."""
    import functools

    class Resp:
        status = 302

        def getheader(self, name, default=None):
            return "http://mirror.example/x" if name == "Location" else default

    class Conn:
        def request(self, *a, **k):
            pass

        def getresponse(self):
            return Resp()

        def close(self):
            pass
    resolver = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]   # noqa: E731
    monkeypatch.setattr(wapp._ufg, "open_checked",
                        functools.partial(wapp._ufg.open_checked, resolver=resolver,
                                          connector=lambda *a: Conn()))
    with pytest.raises(wapp._KvmFetchRefused) as e:
        wapp._kvm_fetch_download("https://mirror.example/x", tmp_path, [], 1 << 20, 1,
                                 lambda d, t: None, lambda: False)
    assert e.value.reason == "downgrade" and "https to http" in e.value.message
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url,sha,code,reason", [
    ("http://mirror.example/x", None, 400, "sha256-required"),
    ("http://mirror.example/x", SHA, 202, None),
    ("https://mirror.example/x", None, 202, None),
])
def test_plain_http_needs_the_sha256(client, upload, monkeypatch, url, sha, code, reason):
    monkeypatch.setattr(wapp.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))])
    r = fetch(client, url, sha=sha)
    assert r.status_code == code, r.get_json()
    if reason:
        assert r.get_json()["reason"] == reason and wapp.ACTIONS == {}


class _Refused(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason, self.message = reason, message


def _fake_guard(seen):
    def check_url(url):
        seen.append(url)
        p = wapp.urllib.parse.urlsplit(url)
        if p.username is not None:
            raise _Refused("credentials", "credentials in the URL are not supported")
        return p.scheme, p.hostname, p.port or 80, "/"
    return types.SimpleNamespace(FetchRefused=_Refused, parse_allowlist=lambda cfg: [],
                                 limits=lambda cfg: (5000, 10), check_url=check_url,
                                 resolve_checked=lambda host, port, allow: "1.2.3.4")


def test_the_shared_guard_also_needs_the_sha256_for_plain_http(client, upload, monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_ufg", _fake_guard(seen))
    r = fetch(client, "http://mirror.example/x", sha=None)
    assert r.status_code == 400 and r.get_json()["reason"] == "sha256-required"
    r = fetch(client, "http://u:pw@mirror.example/x")
    assert r.status_code == 400 and r.get_json()["reason"] == "credentials"
    assert "pw" not in r.get_data(as_text=True)


# --- garde de livraison : le repli disparaît avec la garde partagée ----------------------------

def test_release_gate_the_kvm_fetch_uses_the_shared_guard_only():
    """Fusion de la 1.99.0 : le repli REPLI-AVANT-1.99 a été supprimé ; la
    route et la CLI passent par bin/lib/url_fetch_guard.py seul."""
    import url_fetch_guard
    assert wapp._ufg is url_fetch_guard and wapp._kvm_cli.ufg is url_fetch_guard
    assert (ROOT / "bin" / "lib" / "url_fetch_guard.py").exists()
    assert not (ROOT / "web" / "url_fetch_guard.py").exists()
    cli_src = (ROOT / "bin" / "harvester-kvm.py").read_text()
    for src in (SRC, cli_src):
        assert "REPLI-AVANT-1.99" not in src
    assert "_kvm_load_fetch_guard" not in SRC


def test_a_stray_web_copy_of_the_guard_never_shadows_bin_lib(tmp_path):
    """Une copie de url_fetch_guard placée avant bin/lib dans le chemin de
    recherche (web/ l'est toujours) n'est pas celle que charge la console."""
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / "url_fetch_guard.py").write_text("STRAY = True\n")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2]); "
            "import app; import url_fetch_guard as u; "
            "print(app._ufg.__file__); print(u is app._ufg); print(hasattr(app._ufg, 'STRAY'))")
    env = dict(os.environ, HARVESTER_OPS_CONFIG=str(tmp_path / "none.yaml"),
               HARVESTER_OPS_BIN=str(ROOT / "bin"), HARVESTER_OPS_LOG_DIR=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", code, str(stray), str(ROOT / "web")],
                       capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr[-2000:]
    path, same, is_stray = r.stdout.strip().splitlines()[-3:]
    assert Path(path).resolve() == (ROOT / "bin" / "lib" / "url_fetch_guard.py").resolve()
    assert same == "True" and is_stray == "False"


def _package_gate(tmp_path, guard, fallback, stray=False):
    d = tmp_path / "pkg"
    (d / "web").mkdir(parents=True)
    (d / "bin" / "lib").mkdir(parents=True)
    shutil.copy(ROOT / "package.sh", d / "package.sh")
    (d / "VERSION").write_text("0.0.0\n")
    (d / "web" / "app.py").write_text("# REPLI-AVANT-1.99\n" if fallback == "app" else "# rien\n")
    (d / "bin" / "harvester-kvm.py").write_text("# REPLI-AVANT-1.99\n" if fallback == "cli"
                                                else "# rien\n")
    if guard:
        (d / "bin" / "lib" / "url_fetch_guard.py").write_text("")
    if stray:                                  # copie égarée à l'ancien emplacement
        (d / "web" / "url_fetch_guard.py").write_text("")
    bindir = tmp_path / "bin"                  # ni podman ni docker : rien ne se construit
    bindir.mkdir(exist_ok=True)
    for tool in ("cat", "dirname", "grep"):
        target = shutil.which(tool)
        if not (bindir / tool).exists():
            os.symlink(target, bindir / tool)
    return subprocess.run(["/bin/bash", str(d / "package.sh")], capture_output=True, text=True,
                          env={"PATH": str(bindir)}, timeout=30)


@pytest.mark.parametrize("guard,fallback,stray,refused", [
    (True, "app", False, True), (True, "cli", False, True), (True, None, False, False),
    (False, None, False, True), (True, None, True, True)])
def test_package_refuses_a_release_without_the_shared_guard_or_with_the_fallback(
        tmp_path, guard, fallback, stray, refused):
    # sans podman ni docker, le script s'arrête de toute façon plus loin : rien ne se construit
    r = _package_gate(tmp_path, guard, fallback, stray)
    assert r.returncode == 1
    assert ("KVM fetch must use" in r.stderr) is refused, r.stderr


# --- 2. place sur la console : réservations ---------------------------------------------------

@pytest.fixture
def space(monkeypatch):
    """Système de fichiers simulé : `free` octets libres (fixes) et
    iso_fetch.min_free_bytes = 1000 ; aucune réservation d'un autre test."""
    box = {"free": 4000}
    monkeypatch.setattr(wapp.shutil, "disk_usage",
                        lambda p: types.SimpleNamespace(total=1 << 40, used=0, free=box["free"]))
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"min_free_bytes": 1000}})
    with wapp._KVM_STAGING_LOCK:
        saved = set(wapp._KVM_STAGING_HOLDS)
        wapp._KVM_STAGING_HOLDS.clear()
    yield box
    with wapp._KVM_STAGING_LOCK:
        wapp._KVM_STAGING_HOLDS.clear()
        wapp._KVM_STAGING_HOLDS.update(saved)


class _Stream:
    """Corps lu par blocs ; `during` est appelé au premier bloc (une seconde
    demande arrive PENDANT la réception de la première)."""

    def __init__(self, data, during=None):
        self.data, self.during = data, during

    def read(self, n):
        if self.during:
            during, self.during = self.during, None
            during()
        out, self.data = self.data[:min(n, 500)], self.data[min(n, 500):]
        return out


def _upload(client, name, length, during=None):
    env_ = {"CONTENT_LENGTH": str(length), "wsgi.input": _Stream(b"x" * length, during)}
    return client.post(f"/api/kvm/hosts/node2/images/upload?pool=images&name={name}",
                       environ_overrides=env_, content_type="application/octet-stream")


def test_two_concurrent_uploads_cannot_overcommit_the_free_space(client, upload, space):
    # 4000 libres, 1000 gardés : 2000 + 2000 ne tiennent pas ensemble
    second = {}

    def during():
        second["r"] = _upload(client, "b.img", 2000)
    first = _upload(client, "a.img", 2000, during)
    assert first.status_code == 202, first.get_json()
    r = second["r"]
    assert r.status_code == 507 and r.get_json()["reason"] == "no-space", r.get_json()
    assert r.get_json()["reserved"] == 2000 and r.get_json()["need"] == 2000
    assert len(wapp.ACTIONS) == 1                                  # rien lancé pour la seconde
    # la première reçue : sa place est écrite (comptée par le disque), la seconde passe
    with wapp._KVM_STAGING_LOCK:
        [hold] = wapp._KVM_STAGING_HOLDS
    assert hold.remaining() == 0 and hold.path
    assert _upload(client, "b.img", 2000).status_code == 202
    for t in list(_NoThread.started):
        t.target()                                                 # envoi aux hôtes, fichiers retirés
    assert wapp._KVM_STAGING_HOLDS == set() and staged(upload) == []


def test_two_small_uploads_fit_together(client, upload, space):
    second = {}

    def during():
        second["r"] = _upload(client, "b.img", 1000)
    assert _upload(client, "a.img", 1000, during).status_code == 202
    assert second["r"].status_code == 202


def test_free_space_is_checked_again_while_receiving(client, upload, space, monkeypatch):
    monkeypatch.setattr(wapp, "KVM_SPACE_CHECK_EVERY", 1000)

    class Filling(_Stream):
        def read(self, n):
            out = super().read(n)
            if len(self.data) < 2000:
                space["free"] = 1500                           # le disque se remplit ailleurs
            return out
    env_ = {"CONTENT_LENGTH": "3000", "wsgi.input": Filling(b"x" * 3000)}
    space["free"] = 5000
    r = client.post("/api/kvm/hosts/node2/images/upload?pool=images&name=a.img",
                    environ_overrides=env_, content_type="application/octet-stream")
    assert r.status_code == 507 and r.get_json()["reason"] == "no-space", r.get_json()
    assert "ran out of free space during the upload" in r.get_json()["error"]
    run = only_run()
    assert run.status == "error" and "iso_fetch.min_free_bytes" in run.error_summary
    assert staged(upload) == [] and wapp._KVM_STAGING_HOLDS == set() and _NoThread.started == []


def test_a_running_upload_counts_against_a_download(client, upload, space, monkeypatch):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"min_free_bytes": 1000,
                                                                        "allow_private": ["127.0.0.0/8"]}})
    other = wapp._KvmStagingHold(wapp._kvm_cli.staging_dir(wapp._state_dir()), 1000)
    other.reserve(2500)
    space["free"] = 3400                                          # 3400 - 2500 < 1000
    r = fetch(client, "http://127.0.0.1:9/x")
    assert r.status_code == 507 and r.get_json()["reserved"] == 2500, r.get_json()
    other.release()
    assert fetch(client, "http://127.0.0.1:9/x").status_code == 202


def test_a_download_reserves_its_announced_size(client, upload, space, monkeypatch, http_server):
    # le contrôle du script (marge de DOWNLOAD_MARGIN) passe, la réservation
    # (iso_fetch.min_free_bytes) refuse : un octet de trop
    margin = wapp._kvm_cli.DOWNLOAD_MARGIN
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"min_free_bytes": margin + 1000,
                                                                        "allow_private": ["127.0.0.0/8"]}})
    space["free"] = len(_Handler.payload) + margin + 999
    assert fetch(client, f"{http_server}/x").status_code == 202
    _NoThread.started[-1].target()
    run = only_run()
    assert run.status == "error" and "not enough free space" in run.error_summary
    assert FakePopen.calls == [] and staged(upload) == [] and wapp._KVM_STAGING_HOLDS == set()


# --- 5. restes du répertoire de réception -------------------------------------------------------

def test_startup_sweep_removes_old_leftovers_only(env, space):
    staging = wapp._kvm_cli.staging_dir(env["state"])
    now = time.time()
    old = now - wapp.KVM_STAGING_TTL - 60
    files = {"up-old": old, "dl-old": old, "up-recent": now - 60, "up-active": old,
             "notes.json": old, "other-old": old}
    for name, mtime in files.items():
        (staging / name).write_bytes(b"x")
        os.utime(staging / name, (mtime, mtime))
    (staging / "dl-dir").mkdir()
    os.utime(staging / "dl-dir", (old, old))
    hold = wapp._KvmStagingHold(staging, 1)
    hold.reserve(0)
    hold.attach(staging / "up-active")
    try:
        assert sorted(wapp._kvm_staging_sweep(env["state"], now=now)) == ["dl-old", "up-old"]
    finally:
        hold.release()
    assert sorted(p.name for p in staging.iterdir()) == [
        "dl-dir", "notes.json", "other-old", "up-active", "up-recent"]


def test_the_sweep_runs_at_startup_outside_read_workers():
    i = SRC.index("def _kvm_staging_sweep(")
    assert "if not IS_READ_WORKER:\n    with contextlib.suppress(Exception):\n        _kvm_staging_sweep()" \
        in SRC[i:i + 3000]


SPEC_SWEEP_CASES = [
    # (nom, âge en secondes, fichier ordinaire, retiré)
    ("kvm-spec-old.json", wapp.KVM_SPEC_TTL + 60, True, True),
    ("kvm-spec-fresh.json", 30, True, False),
    ("kvm-spec-dir.json", wapp.KVM_SPEC_TTL + 60, False, False),
    ("other-old.json", wapp.KVM_SPEC_TTL + 60, True, False),
    ("kvm-spec-old.txt", wapp.KVM_SPEC_TTL + 60, True, False),
]


def test_startup_sweep_removes_leftover_spec_files(tmp_path):
    """Revue finale m2 : un spec laissé par une console tuée pendant une
    action (user-data, mots de passe cloud-init) ne reste pas sur le disque."""
    now = time.time()
    for name, age, regular, _ in SPEC_SWEEP_CASES:
        p = tmp_path / name
        p.write_text("{}") if regular else p.mkdir()
        os.utime(p, (now - age, now - age))
    assert wapp._kvm_spec_sweep(tmp_path, now=now) == ["kvm-spec-old.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        c[0] for c in SPEC_SWEEP_CASES if not c[3])
    assert wapp._kvm_spec_sweep(tmp_path / "absent", now=now) == []
    assert not (tmp_path / "absent").exists()


def test_the_spec_sweep_runs_at_startup_outside_read_workers():
    i = SRC.index("# au démarrage de la console (un processus lecteur n'écrit rien)")
    block = SRC[i:i + 400]
    assert block.index("if not IS_READ_WORKER:") < block.index("_kvm_spec_sweep()")


# --- 6. lectures coûteuses : clé partagée sans paramètre inconnu --------------------------------

@pytest.mark.parametrize("path,call", [
    ("/api/kvm/hosts/node2/pools", "list_pools"),
    ("/api/kvm/hosts/node2/networks", "list_networks"),
    ("/api/kvm/hosts/node2/links", "host_links"),
    ("/api/kvm/vms/node2/web/form", "vm_form"),
])
def test_an_unknown_query_argument_does_not_bypass_the_shared_read(client, lib, monkeypatch, path, call):
    seen = []
    mod = {"list_pools": wapp._ks, "list_networks": wapp._kn, "host_links": wapp._kn,
           "vm_form": wapp._ke}[call]
    real = getattr(mod, call)
    monkeypatch.setattr(mod, call, lambda *a: seen.append(1) or real(*a))
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    wapp.READ_SHARE.invalidate(wapp.KVM_SCOPE)
    for q in ("", "?x=random", "?x=other&y=1", "?fresh=0&z=2"):
        assert client.get(path + q).status_code == 200
    assert len(seen) == 1


def test_caps_keep_the_machine_argument_in_the_key(client, monkeypatch):
    monkeypatch.setattr(wapp._kh.VirshHost, "virsh", lambda self, *a, **k: "v")
    seen = []
    monkeypatch.setattr(wapp._kc, "host_options", lambda h: seen.append(1) or {
        "by_machine": {"q35": {}, "pc": {}}, "disabled": {"q35": {}, "pc": {}}})
    monkeypatch.setattr(wapp, "READ_SHARE_ENABLED", True)
    wapp.READ_SHARE.invalidate(wapp.KVM_SCOPE)
    wapp._KVM_CAPS.clear()
    a = client.get("/api/kvm/hosts/node2/caps?machine=pc").get_json()
    b = client.get("/api/kvm/hosts/node2/caps?machine=q35&x=1").get_json()
    c = client.get("/api/kvm/hosts/node2/caps?machine=pc&x=2").get_json()
    assert a["machine"] == "pc" and b["machine"] == "q35" and c == a
    wapp._KVM_CAPS.clear()


def test_the_read_rate_is_a_valid_limit():
    wapp._validate_rate_spec(wapp.KVM_READ_RATE)


# --- 7. verrous des pools et réseaux cités : 409 tôt -----------------------------------------

NIC = {"kind": "network", "source": "kvmtest-net"}
DISK = {"source": {"kind": "blank", "pool": "images", "size_bytes": 1 << 20}}


@pytest.mark.parametrize("held,method,path,body", [
    (("node2", "pool", "images", "x"), "DELETE", "/api/kvm/hosts/node2/volumes/images/d.qcow2",
     {"expect_delete": DIGEST}),
    (("node2", "pool", "images", "x"), "DELETE", "/api/kvm/hosts/node2/images/images/c.qcow2",
     {"expect_delete": DIGEST}),
    (("node2", "pool", "images", "x"), "POST", "/api/kvm/hosts/node2/images/images/c.qcow2/verify", {}),
    (("node2", "volume", "images/c.qcow2", "x"), "POST",
     "/api/kvm/hosts/node2/images/images/c.qcow2/verify", {}),
    (("node2", "pool", "images", "x"), "PATCH", "/api/kvm/vms/node2/web",
     {"spec": {"disks": [DISK]}, "expect_config": CONFIG}),
    (("node2", "network", "kvmtest-net", "x"), "PATCH", "/api/kvm/vms/node2/web",
     {"spec": {"nics": [NIC]}, "expect_config": CONFIG}),
    (("node2", "network", "kvmtest-net", "x"), "POST", "/api/kvm/vms/node2/web/hot/add-nic", {"nic": NIC}),
])
def test_a_pool_or_network_being_stopped_gives_409_early(client, lib, env, held, method, path, body):
    holder("kvm:pool-stop:node2/other", holds=[held])
    r = client.open(path, method=method, json=body)
    assert r.status_code == 409, r.get_json()
    assert r.get_json()["running_action"] == "kvm:pool-stop:node2/other"
    assert _NoThread.started == [] and list(env["work"].iterdir()) == []


@pytest.mark.parametrize("method,path,body,holds", [
    ("DELETE", "/api/kvm/hosts/node2/volumes/images/d.qcow2", {"expect_delete": DIGEST},
     {("node2", "volume", "images/d.qcow2", "x"), ("node2", "pool", "images", "s")}),
    ("POST", "/api/kvm/hosts/node2/images/images/c.qcow2/verify", {},
     {("node2", "volume", "images/c.qcow2", "s"), ("node2", "pool", "images", "s")}),
    ("PATCH", "/api/kvm/vms/node2/web", {"spec": {"disks": [DISK], "nics": [NIC]}, "expect_config": CONFIG},
     {("node2", "pool", "images", "s"), ("node2", "network", "kvmtest-net", "s")}),
    ("POST", "/api/kvm/vms/node2/web/hot/add-nic", {"nic": NIC}, {("node2", "network", "kvmtest-net", "s")}),
])
def test_holds_mirror_the_cli_locks(client, lib, method, path, body, holds):
    r = client.open(path, method=method, json=body)
    assert r.status_code == 202, r.get_json()
    assert set(only_run().kvm_holds) == holds


# --- 8. modèles -----------------------------------------------------------------------------

@pytest.mark.parametrize("spec,removed", [
    ({"cloudinit": {"user_data": "#cloud-config"}}, ["cloudinit.user_data"]),
    ({"display": {"vnc_password": "x"}}, ["display.vnc_password"]),
    ({"a": {"ApiToken": "t"}}, ["a.ApiToken"]),
    ({"ssh_authorized_keys": ["ssh-ed25519 AAAA"]}, ["ssh_authorized_keys"]),
    ({"x": [{"Private_Key": {"pem": "-----"}}]}, ["x[0].Private_Key"]),
    ({"cloud": {"credentials": {"u": "v"}}}, ["cloud.credentials"]),
    ({"db": {"SECRET": "s", "PassWD": "p"}}, ["db.SECRET", "db.PassWD"]),
    ({"vcpus": 2, "key": "", "token": None}, []),                 # vide : rien à retirer
    ({"vcpus": 2, "name": "web"}, []),
])
def test_template_scrub_removes_every_secret_looking_key(spec, removed):
    clean, got = wapp._ktpl.scrub(spec)
    assert got == removed
    for path in removed:                              # la clé retirée n'est plus dans le spec
        assert repr(path.split(".")[-1]) not in repr(clean)


def test_template_of_another_person_needs_an_administrator(as_user, env):
    wapp._ktpl.save(env["state"], "small", {"vcpus": 1}, owner="patronne")
    r = as_user("alice", "POST", "/api/kvm/templates", json={"name": "small", "spec": {"vcpus": 2}})
    assert r.status_code == 403 and r.get_json()["owner"] == "patronne"
    r = as_user("alice", "DELETE", "/api/kvm/templates/small")
    assert r.status_code == 403 and _NoThread.started == []
    wapp._ktpl.save(env["state"], "mine", {"vcpus": 1}, owner="alice")
    r = as_user("patronne", "POST", "/api/kvm/templates", json={"name": "mine", "spec": {"vcpus": 2}})
    assert r.status_code == 202
    assert script_cmd()[-2:] == ["--owner=patronne", "--replace-other"]
    r = as_user("alice", "POST", "/api/kvm/templates", json={"name": "new", "spec": {"vcpus": 2}})
    assert r.status_code == 202 and script_cmd()[-1] == "--owner=alice"
    d = as_user("curieux", "GET", "/api/kvm/templates").get_json()
    assert {t["name"]: t["owner"] for t in d["templates"]} == {"mine": "alice", "small": "patronne"}


def test_template_actions_have_their_own_tag_and_no_survey_generation(client, env, lib, monkeypatch):
    monkeypatch.setattr(wapp, "_vm_transfer_runner", lambda run, cmd, capture=False: None)
    r = client.post("/api/kvm/templates", json={"name": "t1", "spec": {"vcpus": 1}})
    assert r.status_code == 202
    run = only_run()
    assert run.cluster == "kvm-templates" and run.kvm_holds == (("@templates", "template", "t1", "x"),)
    _NoThread.started[-1].target()
    assert not (env["state"] / "kvm.d" / "survey-gen").exists()
    # un hôte nommé « local » ne se heurte pas aux modèles
    holder("kvm:pool-stop:local/t1", holds=[("local", "template", "t1", "x")])
    wapp.ACTIONS.pop(run.id)
    assert client.post("/api/kvm/templates", json={"name": "t1", "spec": {}}).status_code == 202


# --- 9. nom obligatoire ---------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/api/kvm/vms/node2", "/api/kvm/hosts/node2/vm-plan"])
@pytest.mark.parametrize("name", [None, "", "   ", 3])
def test_create_refuses_a_missing_or_empty_name(client, lib, env, path, name):
    spec = {k: v for k, v in SPEC.items() if k != "name"}
    if name is not None:
        spec["name"] = name
    r = client.post(path, json={"spec": dict(spec, instances=2)})
    assert r.status_code == 400 and "name" in r.get_json()["error"]
    assert wapp.ACTIONS == {} and not [c for c in lib if c[0] == "create-plan"]


# --- 10. compteur de génération abîmé -------------------------------------------------------

@pytest.mark.parametrize("content", [b"garbage", b"", b"\x00\x01", None])
def test_a_corrupt_generation_restarts_above_every_cached_one(tmp_path, content):
    host = types.SimpleNamespace(state=tmp_path, name="node2", decl={})
    made = []
    c = ko.SurveyCache(ttl=100, max_stale=1000, enabled=True, spawn=lambda fn: None,
                       factory=lambda h: made.append(1) or f"s{len(made)}")
    for _ in range(3):
        ko.bump_generation(host)
    gen = tmp_path / "kvm.d" / "survey-gen" / "node2"
    c.get(host)                                       # gardé sous la génération courante
    cached = int(gen.read_text())
    if content is None:
        gen.unlink()                                  # compteur perdu
    else:
        gen.write_bytes(content)
    ko.bump_generation(host)
    assert int(gen.read_text()) > cached and int(gen.read_text()) > 10 ** 18
    assert c.get(host)[0] == "s2"                     # jamais l'ancien relevé


# --- 11. journal sans URL complète ----------------------------------------------------------

def test_a_failed_fetch_never_logs_the_full_url(client, upload, monkeypatch, caplog):
    monkeypatch.setattr(wapp, "load_config_ro", lambda: {"iso_fetch": {"allow_private": ["127.0.0.0/8"]}})
    url = "http://127.0.0.1:9/img.qcow2?X-Amz-Signature=SIGNED-SECRET"

    def boom(*a, **k):
        raise RuntimeError(f"failure for {url}")
    monkeypatch.setattr(wapp, "_kvm_fetch_download", boom)
    # réservations laissées par un autre test du même processus (action jamais
    # exécutée) : seules celles de ce test doivent être rendues
    before = set(wapp._KVM_STAGING_HOLDS)
    assert fetch(client, url).status_code == 202
    with caplog.at_level(logging.DEBUG):
        _NoThread.started[-1].target()
    assert "SIGNED-SECRET" not in caplog.text and "http://127.0.0.1:9/img.qcow2" in caplog.text
    run = only_run()
    assert run.status == "error" and "SIGNED-SECRET" not in str(run.to_dict())
    assert wapp._KVM_STAGING_HOLDS - before == set()
