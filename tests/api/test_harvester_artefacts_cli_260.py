"""v2.6.0 : `harvester-artefacts` (pack, sign, verify, inspect, import, list,
remove) contre des registres et un dépôt Helm simulés ; `pack` lancé par
`python3 -I -S` (bibliothèque standard seulement) ; vraies clés ssh-keygen
pour la confiance."""

import base64
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(ROOT / "bin"))
import artefacts as art  # noqa: E402
import oci_layout as ol  # noqa: E402
import oci_push as op  # noqa: E402
import test_oci_layout_260 as t260  # noqa: E402  (put_index)
import test_oci_push_175 as t175  # noqa: E402  (FakeRegistry, payload)

CLI = ROOT / "bin" / "harvester-artefacts.py"
SECRET = "s3cr3t-pw-Zq9"
needs_keygen = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen needed")

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("harvester_artefacts", CLI)
ha = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ha)


def chart_tgz(name="podinfo", version="6.9.2"):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        data = f"apiVersion: v2\nname: {name}\nversion: {version}\nappVersion: \"{version}\"\n".encode()
        ti = tarfile.TarInfo(f"{name}/Chart.yaml")
        ti.size = len(data)
        t.addfile(ti, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), mtime=0)


class HelmRepo:
    """Dépôt Helm HTTP simulé : index.yaml (forme de `helm repo index`) et
    les .tgz ; Basic optionnel ; journal des Authorization reçues."""

    def __init__(self, charts, user=None, password=None, bad_digest=False):
        import hashlib
        self.files, self.seen = {}, []
        lines = ["apiVersion: v1", "entries:"]
        for (name, version), data in charts.items():
            fn = f"{name}-{version}.tgz"
            self.files["/" + fn] = data
            dig = hashlib.sha256(data + (b"x" if bad_digest else b"")).hexdigest()
            lines += [f"  {name}:", "  - apiVersion: v2", f"    appVersion: \"{version}\"",
                      "    created: \"2026-11-01T00:00:00Z\"", f"    digest: {dig}", f"    name: {name}",
                      "    urls:", f"    - {fn}", f"    version: {version}"]
        lines.append("generated: \"2026-11-01T00:00:00Z\"")
        self.files["/index.yaml"] = ("\n".join(lines) + "\n").encode()
        repo = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                repo.seen.append(self.headers.get("Authorization"))
                want = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
                if user and self.headers.get("Authorization") != want:
                    self.send_response(401)
                    self.end_headers()
                    return
                data = repo.files.get(self.path)
                self.send_response(200 if data is not None else 404)
                self.send_header("Content-Length", str(len(data or b"")))
                self.end_headers()
                self.wfile.write(data or b"")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.host = f"127.0.0.1:{self.srv.server_address[1]}"
        self.url = f"http://{self.host}"
        threading.Thread(target=self.srv.serve_forever, args=(0.05,), daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def run(*args, env=None, check=None, stdlib_only=True):
    cmd = [sys.executable] + (["-I", "-S"] if stdlib_only else []) + [str(CLI), *map(str, args)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    if check is not None:
        assert p.returncode == check, (p.returncode, p.stdout, p.stderr)
    return p


def steps(stderr):
    return [ln.split("|", 3)[1:] for ln in stderr.splitlines() if ln.startswith("STEP_EVENT|")]


@pytest.fixture
def world(tmp_path):
    """Deux registres (l'un à Basic), un dépôt Helm à Basic, un manifeste."""
    a = t175.FakeRegistry(user="ju", password=SECRET, basic=True)
    b = t175.FakeRegistry()
    helm = HelmRepo({("podinfo", "6.9.2"): chart_tgz()}, user="ju", password=SECRET)
    shared = t175.payload(5000, b"shared-base")
    t260.put_index(a, "stefanprodan/podinfo", "6.9.2", ["arm64", "amd64"], shared=shared)
    t260.put_index(b, "x/tool", "1.0", ["amd64"], shared=shared)
    (tmp_path / "quotas.yaml").write_text("kind: ResourceQuota\n")
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({a.host: {"username": "ju", "password": SECRET},
                                 helm.host: {"username": "ju", "password": SECRET}}))
    creds.chmod(0o600)
    desc = {"name": "socle", "version": "2026.11", "items": [
        {"name": "podinfo", "kind": "chart",
         "chart": {"repo": helm.url, "name": "podinfo", "version": "6.9.2"},
         "images": [f"{a.host}/stefanprodan/podinfo:6.9.2"]},
        {"name": "preuve", "kind": "images",
         "images": [{"ref": f"{a.host}/stefanprodan/podinfo:6.9.2", "as": "docker.io/harvops-proof/podinfo:2.6.0"},
                    f"{b.host}/x/tool:1.0"]},
        {"name": "quotas", "kind": "manifest", "files": ["quotas.yaml"]}]}
    (tmp_path / "socle.json").write_text(json.dumps(desc))
    yield {"a": a, "b": b, "helm": helm, "dir": tmp_path, "creds": creds, "desc": desc, "shared": shared}
    a.close()
    b.close()
    helm.close()


def pack_world(w, extra=(), stdlib_only=True):
    plain = [x for h in (w["a"].host, w["b"].host, w["helm"].host) for x in ("--plain-http", h)]
    return run("pack", w["dir"] / "socle.json", "--out", w["dir"] / "out", "--creds", w["creds"], *plain, *extra,
               stdlib_only=stdlib_only)


def members(pkg):
    with tarfile.open(pkg, "r:gz") as t:
        return [(m.name, m) for m in t.getmembers()]


def test_pack_builds_a_full_package_with_the_standard_library_only(world):
    p = pack_world(world)
    assert p.returncode == 0, p.stderr
    res = json.loads(p.stdout)
    assert res["ok"] and res["images"] == 3 and res["charts"] == 1 and res["items"] == 3
    pkg = world["dir"] / "out" / "socle-2026.11.tar.gz"
    names = [n for n, m in members(pkg) if m.isfile()]
    assert names[0] == "socle-2026.11/SHA256SUMS"
    info = art.inspect_package(pkg)
    assert info["name"] == "socle"
    dest = world["dir"] / "x"
    art.extract_verified(pkg, dest)
    idx = json.loads((dest / "artefacts.yaml").read_text())
    a = world["a"].host
    assert idx["items"][0] == {"name": "podinfo", "kind": "chart", "version": "6.9.2",
                               "chart": "charts/podinfo-6.9.2.tgz", "images": [f"{a}/stefanprodan/podinfo:6.9.2"]}
    assert idx["items"][1]["images"] == ["docker.io/harvops-proof/podinfo:2.6.0", f"{world['b'].host}/x/tool:1.0"]
    assert idx["items"][2]["files"] == ["manifests/quotas/quotas.yaml"]
    assert (dest / "charts/podinfo-6.9.2.tgz").read_bytes() == chart_tgz()
    lay = ol.Layout(dest / "images")
    entries = {e["annotations"][ol.A_NAME]: e for e in lay.entries()}
    assert set(entries) == {f"{a}/stefanprodan/podinfo:6.9.2", "docker.io/harvops-proof/podinfo:2.6.0",
                            f"{world['b'].host}/x/tool:1.0"}
    assert entries["docker.io/harvops-proof/podinfo:2.6.0"]["annotations"][ol.A_REF] == "2.6.0"
    assert entries["docker.io/harvops-proof/podinfo:2.6.0"]["digest"] == \
        entries[f"{a}/stefanprodan/podinfo:6.9.2"]["digest"]                 # « as » : même image, autre nom
    # couche partagée entre deux images de deux registres : une fois sur disque, tirée une fois
    shared = t175.sha(world["shared"])
    assert lay.has_blob(shared)
    pulls = [x for r in (world["a"], world["b"]) for x in r.log if x[0] == "GET" and x[1].endswith(shared[7:])]
    assert len(pulls) == 1
    assert [s[0] for s in steps(p.stderr)].count("package") == 2


def test_credentials_never_leave_their_file(world):
    p = pack_world(world)
    assert p.returncode == 0, p.stderr
    pkg = world["dir"] / "out" / "socle-2026.11.tar.gz"
    raw = gzip.decompress(pkg.read_bytes())
    b64 = base64.b64encode(f"ju:{SECRET}".encode())
    for blob in (p.stdout.encode(), p.stderr.encode(), raw):
        assert SECRET.encode() not in blob and b64 not in blob
    assert world["helm"].seen and all(s and s.startswith("Basic ") for s in world["helm"].seen)


def test_a_credentials_file_readable_by_others_is_refused(world):
    world["creds"].chmod(0o644)
    p = pack_world(world)
    assert p.returncode == 1 and "chmod 600" in p.stderr


def test_a_yaml_description_needs_pyyaml(world):
    (world["dir"] / "socle.json").write_text("name: socle\nversion: '2026.11'\nitems: []\n")
    p = pack_world(world)                       # -S : pas de PyYAML
    assert p.returncode == 2 and "PyYAML is not installed" in p.stderr
    p = pack_world(world, stdlib_only=False)
    pytest.importorskip("yaml")
    assert p.returncode == 2 and "items: a list, one item at least" in p.stderr


@pytest.mark.parametrize("change,message", [
    (lambda d: d.update(colour="red"), "unknown key: colour"),
    (lambda d: d["items"][0].update(helm=1), "items[0]: unknown key: helm"),
    (lambda d: d["items"][0]["chart"].pop("version"), "items[0].chart: {repo, name, version}"),
    (lambda d: d["items"][0]["chart"].update(repo="ftp://x"), "oci://, https:// or http://"),
    (lambda d: d["items"][1]["images"].append({"ref": "a/b:1", "as": "c/d@sha256:" + "a" * 64}), "as: a name"),
    (lambda d: d["items"][1]["images"].append({"ref": "q.io/other:1", "as": "docker.io/harvops-proof/podinfo:2.6.0"}),
     "names two different images"),
    (lambda d: d["items"][2].update(files=["a.txt"]), "items[2].files"),
    (lambda d: d.update(version="latest"), "version: x.y.z"),
])
def test_a_bad_description_is_refused_before_anything_is_fetched(world, change, message):
    change(world["desc"])
    (world["dir"] / "socle.json").write_text(json.dumps(world["desc"]))
    p = pack_world(world)
    assert p.returncode == 2 and message in p.stderr, p.stderr
    assert not world["a"].log and not world["helm"].seen


@pytest.mark.parametrize("what,message", [
    ("chart-version", "not in"),
    ("chart-digest", "does not match the repository index"),
    ("image", "image "),
])
def test_a_missing_or_altered_source_fails_with_a_named_error(world, what, message):
    if what == "chart-version":
        world["desc"]["items"][0]["chart"]["version"] = "9.9.9"
    elif what == "chart-digest":
        world["helm"].close()
        world["helm"] = HelmRepo({("podinfo", "6.9.2"): chart_tgz()}, bad_digest=True)
        world["desc"]["items"][0]["chart"]["repo"] = world["helm"].url
    else:
        world["desc"]["items"][1]["images"][1] = f"{world['b'].host}/x/absent:1"
    (world["dir"] / "socle.json").write_text(json.dumps(world["desc"]))
    p = pack_world(world)
    assert p.returncode == 1 and message in p.stderr, p.stderr
    assert not list((world["dir"] / "out").glob("*.tar.gz*")) and not list((world["dir"] / "out").glob(".socle*"))


def test_a_chart_is_read_from_an_oci_repository(world, tmp_path):
    lay = ol.Layout(tmp_path / "c")
    (tmp_path / "p.tgz").write_bytes(chart_tgz("podinfo", "6.9.2"))
    ol.chart_to_layout(tmp_path / "p.tgz", lay)
    entry = lay.find("podinfo:6.9.2")
    ol.push_image(lay, entry, op.Registry(world["b"].host, plain_http=True), "charts/podinfo", "6.9.2")
    world["desc"]["items"][0]["chart"]["repo"] = f"oci://{world['b'].host}/charts"
    (world["dir"] / "socle.json").write_text(json.dumps(world["desc"]))
    p = pack_world(world)
    assert p.returncode == 0, p.stderr
    with tarfile.open(world["dir"] / "out" / "socle-2026.11.tar.gz") as t:
        assert t.extractfile("socle-2026.11/charts/podinfo-6.9.2.tgz").read() == chart_tgz()


@pytest.mark.parametrize("text,expected", [
    ("apiVersion: v1\nentries:\n  podinfo:\n  - digest: " + "a" * 64 + "\n    urls:\n    - p-1.tgz\n    version: 1.0.0\n"
     "  - digest: " + "b" * 64 + "\n    urls:\n    - p-2.tgz\n    version: \"2.0.0\"\n  other:\n  - version: 9\n",
     [("1.0.0", "a" * 64, ["p-1.tgz"]), ("2.0.0", "b" * 64, ["p-2.tgz"])]),
    ("entries:\n    podinfo:\n        - name: podinfo\n          urls:\n              - 'https://x/p.tgz'\n"
     "          digest: " + "c" * 64 + "\n          version: 3.1.0\n",
     [("3.1.0", "c" * 64, ["https://x/p.tgz"])]),
    ("entries:\n  podinfo:\n  - urls: [a.tgz, b.tgz]\n    version: 1.0\n    digest: d\n", [("1.0", "d", ["a.tgz", "b.tgz"])]),
    (json.dumps({"entries": {"podinfo": [{"version": "1", "digest": "e", "urls": ["u"]}]}}), [("1", "e", ["u"])]),
    ("apiVersion: v1\ngenerated: x\n", []),
])
def test_helm_repository_indexes_are_read_without_pyyaml(text, expected):
    got = [(str(e.get("version")), e.get("digest"), e.get("urls")) for e in ha.helm_index_entries(text, "podinfo")]
    assert got == expected


def test_pack_signs_when_asked(world):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen needed")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(world["dir"] / "k")], check=True)
    p = pack_world(world, extra=("--sign-key", world["dir"] / "k"))
    assert p.returncode == 0, p.stderr
    pkg = world["dir"] / "out" / "socle-2026.11.tar.gz"
    assert json.loads(p.stdout)["signed"] is True
    assert art.verify_trust(pkg, str(pkg) + ".sig", None)["state"] == "unknown-key"


# ---------------------------------------------------------------------------
# sign, verify, inspect, import, list, remove
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen needed")
    d = tmp_path_factory.mktemp("keys")
    for k in ("client", "stranger"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", k, "-f", str(d / k)], check=True)
    pub = (d / "client.pub").read_text().split()
    (d / "signers").write_text(f"ops@client {pub[0]} {pub[1]}\n")
    return d


def small_package(tmp_path, name="socle", version="2026.11"):
    (tmp_path / "ns.yaml").write_text("kind: Namespace\n")
    (tmp_path / "d.json").write_text(json.dumps({"name": name, "version": version, "items": [
        {"name": "ns", "kind": "manifest", "files": ["ns.yaml"]}]}))
    run("pack", tmp_path / "d.json", "--out", tmp_path / "out", check=0)
    return tmp_path / "out" / f"{name}-{version}.tar.gz"


def console_env(tmp_path, strict=None, locked=False):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(json.dumps({"artefacts": {"strict": True}} if locked else {"clusters": []}))
    if strict is not None:
        (state / "artefacts").mkdir(exist_ok=True)
        (state / "artefacts" / "settings.json").write_text(json.dumps({"strict": strict}))
    return dict(os.environ, HARVESTER_OPS_STATE_DIR=str(state), HARVESTER_OPS_CONFIG=str(cfg)), state


@needs_keygen
def test_sign_then_verify_and_inspect(keys, tmp_path):
    pkg = small_package(tmp_path)
    run("sign", pkg, "--key", keys / "client", check=0)
    p = run("verify", pkg, "--signers", keys / "signers", check=0)
    assert json.loads(p.stdout)["principal"] == "ops@client"
    p = run("verify", pkg, check=1)                 # sans clés de confiance : clé inconnue, code non nul
    assert json.loads(p.stdout)["state"] == "unknown-key"
    p = run("inspect", pkg, "--signers", keys / "signers", check=0)
    info = json.loads(p.stdout)
    assert info["trust"]["state"] == "valid" and info["index"]["items"][0]["name"] == "ns"


@needs_keygen
@pytest.mark.parametrize("state,strict,locked,flag,code,expect", [
    ("valid", False, False, None, 0, "valid"),
    ("valid", True, False, None, 0, "valid"),
    ("unknown-key", False, False, None, 3, "acknowledge-unknown-key"),
    ("unknown-key", False, False, "--accept-unknown-key", 0, "accepted-unknown-key"),
    ("unknown-key", False, False, "--accept-unsigned", 3, "acknowledge-unknown-key"),
    ("unknown-key", True, False, "--accept-unknown-key", 2, "strict-refuses-unknown-key"),
    ("unsigned", False, False, None, 3, "acknowledge-unsigned"),
    ("unsigned", False, False, "--accept-unsigned", 0, "accepted-unsigned"),
    ("unsigned", None, True, "--accept-unsigned", 2, "strict-refuses-unsigned"),     # verrou de config.yaml
    ("invalid", False, False, "--accept-unsigned", 2, "invalid"),
    ("invalid", True, False, None, 2, "invalid"),
])
def test_import_follows_the_decision_table(keys, tmp_path, state, strict, locked, flag, code, expect):
    pkg = small_package(tmp_path)
    env, sdir = console_env(tmp_path, strict, locked)
    art.save_signers(sdir, (keys / "signers").read_text())
    if state in ("valid", "unknown-key"):
        run("sign", pkg, "--key", keys / ("client" if state == "valid" else "stranger"), check=0)
    elif state == "invalid":
        other = tmp_path / "other"
        other.write_bytes(b"x")
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / "client"), "-n", art.SIG_NAMESPACE,
                        str(other)], check=True)
        shutil.move(str(other) + ".sig", str(pkg) + ".sig")
    p = run("import", pkg, *([flag] if flag else []), "--person", "alice", env=env, check=code)
    res = json.loads(p.stdout)
    assert res["code"] == expect and res["accepted"] is (code == 0)
    pdir = sdir / "artefacts.d" / "socle-2026.11"
    assert pdir.is_dir() is (code == 0)
    if code == 0:
        trust = json.loads((pdir / "trust.json").read_text())
        assert trust["state"] == state and trust["person"] == "alice"
        assert (pdir / "manifests/ns/ns.yaml").read_text() == "kind: Namespace\n"
        line = [s for s in steps(p.stderr) if s[0] == "trust"][0][2]
        assert line == art.journal_line(trust, "socle", "2026.11", "alice")
        assert oct(pdir.stat().st_mode & 0o777) == "0o700"
        assert (pdir / "socle-2026.11.tar.gz.sig").is_file() is (state != "unsigned")


def test_import_refuses_an_altered_package_and_leaves_nothing(tmp_path):
    pkg = small_package(tmp_path)
    raw = gzip.decompress(pkg.read_bytes()).replace(b"kind: Namespace\n", b"kind: Namespacf\n")
    pkg.write_bytes(gzip.compress(raw))
    env, sdir = console_env(tmp_path)
    p = run("import", pkg, "--accept-unsigned", env=env, check=2)
    assert json.loads(p.stdout)["code"] == "sums-mismatch"
    assert not (sdir / "artefacts.d" / "socle-2026.11").exists()


def test_import_twice_is_refused_then_list_and_remove(tmp_path):
    pkg = small_package(tmp_path)
    env, sdir = console_env(tmp_path)
    run("import", pkg, "--accept-unsigned", env=env, check=0)
    p = run("import", pkg, "--accept-unsigned", env=env, check=2)
    assert json.loads(p.stdout)["code"] == "exists"
    listed = json.loads(run("list", env=env, check=0).stdout)
    assert [(x["name"], x["version"], x["trust"]) for x in listed] == [("socle", "2026.11", "unsigned")]
    # 2.6.0 : `remove` lit les plans (usage du paquet), donc PyYAML
    run("remove", "socle", "2026.11", env=env, check=0, stdlib_only=False)
    assert json.loads(run("list", env=env, check=0).stdout) == []
    run("remove", "socle", "2026.11", env=env, check=2, stdlib_only=False)


def test_sums_written_last_are_refused_by_the_streaming_import(tmp_path, monkeypatch):
    """Garde de la mutation M-sums-order : un paquet dont SHA256SUMS n'est
    pas le premier membre est refusé à l'import."""
    pkg = small_package(tmp_path)
    with tarfile.open(pkg) as t:
        ms = [(m, t.extractfile(m).read() if m.isfile() else None) for m in t.getmembers()]
    ms.sort(key=lambda x: x[0].name.endswith("SHA256SUMS"))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as t:
        for m, data in ms:
            t.addfile(m, io.BytesIO(data) if data is not None else None)
    pkg.write_bytes(raw.getvalue())
    env, _ = console_env(tmp_path)
    p = run("import", pkg, "--accept-unsigned", env=env, check=2)
    assert json.loads(p.stdout)["code"] == "sums-not-first"


def test_the_written_package_puts_its_sums_first(tmp_path):
    pkg = small_package(tmp_path)
    files = [n for n, m in members(pkg) if m.isfile()]
    assert files[0].endswith("/SHA256SUMS") and len(files) == 3


def test_remove_without_pyyaml_says_where_to_run_it(tmp_path):
    """Revue finale m-8 : sans PyYAML (`-I -S`), `remove` finit par une
    étape d'erreur claire et le code 1, jamais une trace ; rien n'est
    retiré."""
    pkg = small_package(tmp_path)
    env, sdir = console_env(tmp_path)
    run("import", pkg, "--accept-unsigned", env=env, check=0)
    p = run("remove", "socle", "2026.11", env=env, check=1)
    assert "Traceback" not in p.stderr
    assert ["remove", "error", "remove reads the saved plans; run it where the console is installed (PyYAML)"] \
        in steps(p.stderr)
    listed = json.loads(run("list", env=env, check=0).stdout)
    assert [(x["name"], x["version"]) for x in listed] == [("socle", "2026.11")]
