"""2.9.0 tâche 7 : outil `harvester-secrets` (bin/harvester-secrets.py).

Chaque sous-commande contre le faux serveur (fake_openbao_b1.FakeB1), en
`--dry-run` puis pour de vrai quand c'est sans danger ; l'agent est joué
dans le processus (même code que l'agent de l'hôte, faux hôte). Aucune
valeur (jeton d'enveloppe, secret_id, mot de passe du compte de service)
dans stdout, stderr ni l'état.

Mutations jouées (rapport) : « original effacé avant la relecture
comparée », « disable accepté avec une classe migrée », « reprise qui
réécrit un secret déjà migré ».
"""

import importlib.util
import io
import json
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

spec = importlib.util.spec_from_file_location("hsc", ROOT / "bin" / "harvester-secrets.py")
hsc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hsc)
spec2 = importlib.util.spec_from_file_location("hsa", ROOT / "bin" / "harvester-secrets-agent.py")
hsa = sys.modules.get("hsa") or importlib.util.module_from_spec(spec2)
if "hsa" not in sys.modules:
    sys.modules["hsa"] = hsa
    spec2.loader.exec_module(hsa)

import openbao_client as oc  # noqa: E402
import openbao_policy as pol  # noqa: E402
import secret_classes as sc  # noqa: E402
import secret_store as ss  # noqa: E402
from fake_openbao_b1 import FakeB1, FakeHost, make_registry, cosign_test_key  # noqa: E402

CID = "abc123def456"
PREFIX = f"consoles/{CID}"
VALUE = "S3CRET-cli-290"


@pytest.fixture
def env(tmp_path, monkeypatch):
    state, etc, run = tmp_path / "state", tmp_path / "etc", tmp_path / "run"
    for d in (state, etc, run):
        d.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_OPENBAO_DIR", str(etc / "openbao"))
    monkeypatch.delenv(ss.SPAWNED_ENV, raising=False)
    monkeypatch.delenv(ss.FILES_ENV, raising=False)
    srv = FakeB1(CID).start()
    paths = hsa.Paths({"HARVESTER_OPS_STATE_DIR": str(state), "HARVESTER_OPS_ETC": str(etc),
                       "HARVESTER_OPS_LOG_DIR": str(tmp_path / "logs"),
                       "HARVESTER_SECRETS_AGENT_WORK": str(tmp_path / "work"),
                       "HARVESTER_OPS_OPENBAO_ETC": str(tmp_path / "bao-etc"),
                       "HARVESTER_OPS_OPENBAO_DATA": str(tmp_path / "bao-data")})
    host = FakeHost(paths)

    def agent():
        a = hsa.Agent(paths=paths, host=host, client=lambda addr, ca_pem=None: oc.Client(
            srv.addr, timeout=2, retry_delays=(0.01,)), sleep=lambda s: None, b1_addr=srv.addr)
        a._need_podman = lambda: None
        return a
    # l'outil joue l'agent dans son processus (branche root), avec ce faux hôte
    seen_paths = []

    def agent_factory(paths=None):                  # Paths reçus de l'outil (revue M11)
        seen_paths.append(paths)
        return agent()
    monkeypatch.setattr(hsc, "_agent_module", lambda: types.SimpleNamespace(Agent=agent_factory, Paths=hsa.Paths))
    monkeypatch.setattr(hsc, "_is_root", lambda: True)
    monkeypatch.setattr(hsc, "_pause", lambda s: None)
    yield {"state": state, "etc": etc / "openbao", "run": run, "srv": srv, "paths": paths, "host": host,
           "seen_paths": seen_paths}
    srv.stop()


def cli(argv, stdin="", capsys=None):
    sys.stdin = io.StringIO(stdin)
    try:
        rc = hsc.main(argv)
    finally:
        sys.stdin = sys.__stdin__
    o = capsys.readouterr()
    try:
        doc = json.loads(o.out)
    except ValueError:
        doc = o.out
    return rc, doc, o.err


def creds(env, rid="rid-console-1"):
    """Identifiants de la console déposés (comme l'agent) pour le faux serveur."""
    srv = env["srv"]
    srv.role_ids.setdefault(pol.console_role(CID), rid)
    sid = "sid-" + os.urandom(6).hex()
    srv.add_role(rid, sid)
    env["etc"].mkdir(parents=True, exist_ok=True)
    (env["etc"] / "role-id").write_text(rid)
    (env["etc"] / "secret-id").write_text(sid)
    return rid, sid


def settings(env, **kw):
    doc = {"mode": "a", "addr": env["srv"].addr, "console_id": CID, "secret_files_dir": str(env["run"]),
           "dev_disk_secret_files": True, **kw}
    (env["state"] / ss.SETTINGS_FILE).write_text(json.dumps(doc))
    return doc


def read_settings(env):
    return json.loads((env["state"] / ss.SETTINGS_FILE).read_text())


def wrapped(env):
    rid = env["srv"].role_ids.setdefault(pol.console_role(CID), "rid-console-1")
    sid = "sid-" + os.urandom(6).hex()
    env["srv"].add_role(rid, sid)
    return env["srv"].wrap({"secret_id": sid, "secret_id_accessor": "acc-w"}, pol.secret_id_path(CID)), sid


# ---------------------------------------------------------------------------
# status, assistant
# ---------------------------------------------------------------------------
def test_status_local(env, capsys):
    rc, doc, _ = cli(["status", "--json"], capsys=capsys)
    assert rc == 0 and doc["mode"] == "local" and doc["active"] is False and doc["credentials"] is False


def test_status_active_reports_health_without_values(env, capsys):
    creds(env)
    settings(env)
    rc, doc, err = cli(["status", "--json"], capsys=capsys)
    assert rc == 0 and doc["active"] and doc["health"] == {"sealed": False, "initialized": True}
    assert "sid-" not in json.dumps(doc) + err


def test_assistant_draws_and_keeps_the_console_id(env, capsys):
    rc, doc, _ = cli(["assistant", "--addr", "https://bao.example:8200", "--console-cidr", "10.0.0.5/32"], capsys=capsys)
    assert rc == 0 and pol.ID_RE.match(doc["console_id"])
    assert read_settings(env)["console_id"] == doc["console_id"] and read_settings(env).get("mode") is None
    assert doc["commands"][-1].startswith("bao write -wrap-ttl=15m")
    rc2, doc2, _ = cli(["assistant", "--addr", "https://bao.example:8200", "--console-cidr", "10.0.0.5/32"],
                       capsys=capsys)
    assert doc2["console_id"] == doc["console_id"]
    rc3, doc3, _ = cli(["assistant", "--addr", "x", "--console-cidr", "10.0.0.5/32"], capsys=capsys)
    assert rc3 == 2 and doc3["code"] == "bad-input"


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def ids(doc):
    return {c["id"]: c["ok"] for c in doc["checks"]}


def test_check_dry_run_with_wrap_before_deposit(env, capsys):
    settings(env, mode="local")
    wrap, _ = wrapped(env)
    rc, doc, err = cli(["check", "--mode", "a", "--addr", env["srv"].addr, "--wrapped-stdin", "--dry-run"],
                       stdin=wrap + "\n", capsys=capsys)
    assert rc == 1 and ids(doc) == {"address": True, "tls": True, "seal": True, "wrap": True, "login": False}
    assert wrap in env["srv"].wraps                      # lookup seulement, jamais déballée
    assert wrap not in json.dumps(doc) + err


def test_check_all_green_dry_run(env, capsys):
    creds(env)
    settings(env, mode="local")
    rc, doc, _ = cli(["check", "--mode", "a", "--addr", env["srv"].addr, "--dry-run"], capsys=capsys)
    assert rc == 0 and ids(doc)["capabilities"] and ids(doc)["roundtrip"] is None
    assert not [r for r in env["srv"].log if r["method"] == "POST" and "/data/" in r["path"]]


def test_check_writes_and_destroys_a_probe(env, capsys):
    creds(env)
    settings(env, mode="local")
    rc, doc, _ = cli(["check", "--mode", "a", "--addr", env["srv"].addr, "--transit"], capsys=capsys)
    assert rc == 0, doc
    assert ids(doc)["roundtrip"] and ids(doc)["transit"] and ids(doc)["tf-role"]
    assert not [k for k in env["srv"].kv if "/check/" in k[1]]


@pytest.mark.parametrize("breakage,failed", [
    (lambda s: setattr(s, "sealed", True), "seal"),
    (lambda s: s.mounts.update({"harvester-ops": 1}), "kv2"),
    (lambda s: s.denied.append(f"harvester-ops/data/{PREFIX}"), "capabilities"),
])
def test_check_one_red_per_cause(env, capsys, breakage, failed):
    creds(env)
    settings(env, mode="local")
    breakage(env["srv"])
    rc, doc, _ = cli(["check", "--mode", "a", "--addr", env["srv"].addr, "--dry-run"], capsys=capsys)
    assert rc == 1 and ids(doc)[failed] is False


def test_check_http_refused_outside_loopback(env, capsys):
    rc, doc, _ = cli(["check", "--mode", "a", "--addr", "http://bao.example:8200", "--dry-run"], capsys=capsys)
    assert rc == 1 and ids(doc) == {"address": False}


# ---------------------------------------------------------------------------
# enable, disable
# ---------------------------------------------------------------------------
def test_enable_a_dry_run_changes_nothing(env, capsys):
    settings(env, mode="local")
    wrap, _ = wrapped(env)
    rc, doc, _ = cli(["enable", "--mode", "a", "--addr", env["srv"].addr, "--role-id", "rid-console-1",
                      "--wrapped-stdin", "--dry-run"], stdin=wrap, capsys=capsys)
    assert rc == 0 and doc["dry_run"] and read_settings(env)["mode"] == "local"
    assert not (env["state"] / "secrets-agent" / "request.json").exists() and not env["etc"].exists()


def test_enable_a(env, capsys):
    settings(env, mode="local")
    wrap, sid = wrapped(env)
    rc, doc, err = cli(["enable", "--mode", "a", "--addr", env["srv"].addr, "--role-id", "rid-console-1",
                        "--wrapped-stdin"], stdin=wrap + "\n", capsys=capsys)
    assert rc == 0, doc
    s = read_settings(env)
    assert s["mode"] == "a" and s["addr"] == env["srv"].addr and s["prefix"] == PREFIX
    assert (env["etc"] / "secret-id").read_text().strip() == sid
    blob = json.dumps(doc) + err + (env["state"] / "secrets-agent" / "status.json").read_text()
    assert wrap not in blob and sid not in blob
    assert "STEP_EVENT|agent-unwrap|done|" in err


def test_enable_a_used_wrap_activates_nothing(env, capsys):
    settings(env, mode="local")
    wrap, _ = wrapped(env)
    env["srv"].wraps.pop(wrap)
    rc, doc, _ = cli(["enable", "--mode", "a", "--addr", env["srv"].addr, "--role-id", "rid-console-1",
                      "--wrapped-stdin"], stdin=wrap, capsys=capsys)
    assert rc == 1 and read_settings(env)["mode"] == "local"


def test_enable_refused_when_already_active(env, capsys):
    settings(env)
    rc, doc, _ = cli(["enable", "--mode", "b1", "--bundle", "x"], capsys=capsys)
    assert rc == 2 and doc["code"] == "openbao-already-active"


def test_enable_b1_needs_a_good_bundle(env, capsys, tmp_path):
    settings(env, mode="local")
    bad = tmp_path / "harvester-ops-openbao-1.0-x.tar.gz"
    bad.write_bytes(b"not a tarball")
    rc, doc, _ = cli(["enable", "--mode", "b1", "--bundle", str(bad), "--dry-run"], capsys=capsys)
    assert rc == 2 and doc["code"] == "bundle-unsafe"


def test_disable_refused_with_a_migrated_class(env, capsys):
    settings(env, migrated={"rancher-oidc": {"at": 1}})
    rc, doc, _ = cli(["disable"], capsys=capsys)
    assert rc == 2 and doc["code"] == "classes-still-migrated" and doc["classes"] == ["rancher-oidc"]
    assert read_settings(env)["mode"] == "a"


def test_disable(env, capsys):
    settings(env)
    rc, doc, _ = cli(["disable", "--dry-run"], capsys=capsys)
    assert rc == 0 and read_settings(env)["mode"] == "a"
    rc, doc, _ = cli(["disable"], capsys=capsys)
    s = read_settings(env)
    assert rc == 0 and s["mode"] == "local" and s["console_id"] == CID
    rc, doc, _ = cli(["disable", "--remove-b1"], capsys=capsys)
    assert rc == 2


# ---------------------------------------------------------------------------
# migrate (moteur, adaptateur simulé)
# ---------------------------------------------------------------------------
class FakeAdapter:
    def __init__(self, values):
        self.values = dict(values)
        self.events = []

    def adapter(self):
        return sc.Adapter(lambda fs: [(p, "client_secret") for p in self.values],
                          lambda fs, p, f: self.values.get(p), self.write_ref, self.restore)

    def write_ref(self, fs, p, f, ref):
        self.events.append(("write_ref", p))
        self.values[p] = ref

    def restore(self, fs, p, f, value):
        self.events.append(("restore", p))
        self.values[p] = value


@pytest.fixture
def mig(env, monkeypatch):
    creds(env)
    settings(env)
    fa = FakeAdapter({"r1": VALUE + "-1", "r2": VALUE + "-2"})
    monkeypatch.setattr(sc, "adapter", lambda cls: fa.adapter())
    return fa


def version(env, name):
    return env["srv"].kv[("harvester-ops", f"{PREFIX}/rancher-oidc/{name}")]["current"]


def writes(env, name):
    return [r for r in env["srv"].log if r["method"] == "POST" and r["path"].endswith(f"/data/{PREFIX}/rancher-oidc/{name}")]


def test_migrate_dry_run(env, mig, capsys):
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc", "--dry-run"], capsys=capsys)
    assert rc == 0 and [d["would"] for d in doc["classes"]["rancher-oidc"]["done"]] == ["write", "write"]
    assert not writes(env, "r1") and mig.events == [] and "migrated" not in read_settings(env)


def test_migrate_interrupted_then_resumed_without_double_write(env, mig, capsys):
    env["srv"].fail_paths[f"harvester-ops/data/{PREFIX}/rancher-oidc/r2"] = (403, "permission denied")
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 1
    assert mig.values["r1"] == "openbao:rancher-oidc/r1#client_secret" and mig.values["r2"] == VALUE + "-2"
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 0, doc
    # r1 jamais réécrit (déjà une référence), r2 écrit une seule fois
    assert len(writes(env, "r1")) == 1 and version(env, "r1") == 1 and version(env, "r2") == 1
    assert read_settings(env)["migrated"]["rancher-oidc"]["at"]
    assert all(json.loads(w["raw"])["options"] == {"cas": 0} for w in writes(env, "r2"))


def test_migrate_resumes_a_value_already_written(env, mig, capsys):
    """Coupure entre l'écriture et la référence : même valeur relue, rien
    réécrit, la référence est posée."""
    env["srv"].put_raw("harvester-ops", f"{PREFIX}/rancher-oidc/r1", {"client_secret": VALUE + "-1"})
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    # cas=0 refusé (même 400 pour la même valeur, essai 5) : relu, comparé, rien réécrit
    assert rc == 0 and version(env, "r1") == 1 and ("write_ref", "r1") in mig.events
    assert doc["classes"]["rancher-oidc"]["done"][0]["resumed"] is True


def test_migrate_conflict_keeps_local(env, mig, capsys):
    env["srv"].put_raw("harvester-ops", f"{PREFIX}/rancher-oidc/r1", {"client_secret": "someone else"})
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "openbao-cas-conflict"
    assert mig.values["r1"] == VALUE + "-1" and ("write_ref", "r1") not in mig.events


def test_migrate_readback_mismatch_never_touches_the_original(env, mig, capsys):
    srv = env["srv"]
    real = srv._kv
    state = {"posted": False}

    def kv(h, m, mount, rest, body, rec):
        if m == "POST" and rest.startswith("data/"):
            state["posted"] = True
        if m == "GET" and rest.startswith("data/") and state["posted"]:
            return h._send(200, {"data": {"data": {"client_secret": "altered"}, "metadata": {"version": 1}}})
        return real(h, m, mount, rest, body, rec)
    srv._kv = kv
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "migrate-readback-mismatch"
    assert mig.events == [] and mig.values["r1"] == VALUE + "-1"


def test_migrate_too_big_stays_local(env, mig, capsys):
    mig.values["r1"] = "x" * (ss.VALUE_MAX + 1)
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 1 and doc["classes"]["rancher-oidc"]["errors"][0]["code"] == "secret-too-big"
    assert ("write_ref", "r2") in mig.events


def test_migrate_back_to_local(env, mig, capsys):
    assert cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)[0] == 0
    rc, doc, _ = cli(["migrate", "--to", "local", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 0, doc
    assert mig.values == {"r1": VALUE + "-1", "r2": VALUE + "-2"}
    assert not [k for k in env["srv"].kv if "rancher-oidc" in k[1]]
    assert "rancher-oidc" not in read_settings(env).get("migrated", {})


def test_migrate_real_adapter(env, capsys):
    creds(env)
    settings(env)
    (env["state"] / "rancher.d").mkdir()
    (env["state"] / "rancher.d" / "r1.oidc-secret").write_text(VALUE)
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 0, doc
    assert (env["state"] / "rancher.d" / "r1.oidc-secret").read_text() == "openbao:rancher-oidc/r1#client_secret\n"
    assert (env["state"] / "rancher.d" / "r1.oidc-secret.migrated").read_text() == VALUE


@pytest.mark.parametrize("argv", [["migrate", "--to", "openbao"],
                                  ["migrate", "--to", "openbao", "--class", "rancher-oidc", "--class", "tf-kvm"]])
def test_migrate_one_class_per_action(env, capsys, argv):
    settings(env)
    rc, doc, _ = cli(argv, capsys=capsys)
    assert rc == 2 and doc["code"] == "one-class-per-action"


@pytest.mark.parametrize("cls", ["accounts", "plan-run-x", "web-auth"])
def test_migrate_refuses_a_class(env, capsys, cls):
    settings(env)
    rc, doc, _ = cli(["migrate", "--to", "openbao", "--class", cls], capsys=capsys)
    assert rc == 2 and doc["code"] == "class-not-migratable"


def test_confirm(env, capsys):
    settings(env)
    assert cli(["confirm", "--class", "rancher-oidc"], capsys=capsys)[0] == 2
    settings(env, migrated={"rancher-oidc": {"at": 1}})
    rc, doc, _ = cli(["confirm", "--class", "rancher-oidc"], capsys=capsys)
    assert rc == 0 and doc["originals_erased"] == 0


# ---------------------------------------------------------------------------
# versions, rollback, rotate, snapshot, restore
# ---------------------------------------------------------------------------
def test_versions_and_rollback(env, capsys):
    creds(env)
    settings(env)
    ref = "openbao:kubeconfig/h1#kubeconfig"
    for v in ("v1", "v2"):
        env["srv"].put_raw("harvester-ops", f"{PREFIX}/kubeconfig/h1", {"kubeconfig": v + VALUE})
    rc, doc, err = cli(["versions", ref], capsys=capsys)
    assert rc == 0 and [v["version"] for v in doc["versions"]] == [1, 2] and VALUE not in json.dumps(doc) + err
    rc, doc, _ = cli(["rollback", ref, "--version", "1", "--dry-run"], capsys=capsys)
    assert rc == 0 and env["srv"].kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]["current"] == 2
    rc, doc, err = cli(["--actor", "alice", "--id", "act-7", "rollback", ref, "--version", "1"], capsys=capsys)
    e = env["srv"].kv[("harvester-ops", f"{PREFIX}/kubeconfig/h1")]
    assert rc == 0 and e["current"] == 3 and e["versions"][3] == {"kubeconfig": "v1" + VALUE}
    assert e["custom"]["hops_v3_actor"] == "alice" and VALUE not in json.dumps(doc) + err
    assert cli(["versions", "local:kubeconfig/h1#kubeconfig"], capsys=capsys)[0] == 2


def test_rotate(env, capsys):
    creds(env)
    settings(env)
    assert cli(["rotate", "secret-id", "--dry-run"], capsys=capsys)[0] == 0
    assert cli(["rotate", "unseal-key"], capsys=capsys)[1]["code"] == "b1-only"
    assert cli(["rotate", "ssh-kvm", "k1"], capsys=capsys)[0] == 2
    rc, doc, _ = cli(["rotate", "transit", "Bad Decl"], capsys=capsys)
    assert rc == 2
    env["srv"].transit[f"hops-{CID}-tf-d1"] = 1
    rc, doc, _ = cli(["rotate", "transit", "d1"], capsys=capsys)
    assert rc == 0 and env["srv"].transit[f"hops-{CID}-tf-d1"] == 2


def test_rotate_secret_id_through_the_agent(env, capsys):
    rid, old = creds(env)
    (env["etc"] / "secret-id-accessor").write_text("")
    settings(env)
    rc, doc, err = cli(["rotate", "secret-id"], capsys=capsys)
    assert rc == 0, doc
    new = (env["etc"] / "secret-id").read_text().strip()
    assert new != old and old not in err + json.dumps(doc) and new not in err + json.dumps(doc)


def test_in_process_agent_uses_the_directories_of_the_tool(env, capsys, monkeypatch, tmp_path):
    """Revue M11 : `sudo harvester-secrets` joue l'agent avec les répertoires
    de l'outil, même hors des chemins par défaut."""
    creds(env)
    (env["etc"] / "secret-id-accessor").write_text("")
    settings(env)
    monkeypatch.delenv("HARVESTER_OPS_ETC", raising=False)
    assert cli(["rotate", "secret-id"], capsys=capsys)[0] == 0
    got = env["seen_paths"][-1]
    assert got.state == env["state"] / "secrets-agent" and got.etc == env["etc"]


def test_snapshot(env, capsys, tmp_path):
    creds(env)
    settings(env)
    assert cli(["snapshot", "--dest", str(tmp_path)], capsys=capsys)[1]["code"] == "b1-only"
    settings(env, mode="b1")
    rc, doc, _ = cli(["snapshot", "--dest", str(env["state"] / "snaps")], capsys=capsys)
    assert rc == 2
    (env["state"] / "snaps").mkdir()
    assert cli(["snapshot", "--dest", str(env["state"] / "snaps")], capsys=capsys)[1]["code"] == "snapshot-in-state-dir"
    dest = tmp_path / "snaps"
    dest.mkdir()
    assert cli(["snapshot", "--dest", str(dest), "--dry-run"], capsys=capsys)[0] == 0 and not list(dest.iterdir())
    rc, doc, _ = cli(["snapshot", "--dest", str(dest)], capsys=capsys)
    f = dest / doc["file"]
    assert rc == 0 and f.read_bytes() == env["srv"].snapshot and (f.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("name", ["../openbao-x.snap", "/tmp/openbao-x.snap", "x.snap"])
def test_restore_bad_name(env, capsys, name):
    settings(env, mode="b1")
    rc, doc, _ = cli(["restore", name], capsys=capsys)
    assert rc == 2 and doc["code"] == "snapshot-bad-name"


def test_restore_dry_run(env, capsys):
    settings(env, mode="b1")
    rc, doc, _ = cli(["restore", "openbao-20261108T020000Z.snap", "--dry-run"], capsys=capsys)
    assert rc == 0 and not (env["state"] / "secrets-agent" / "request.json").exists()


# ---------------------------------------------------------------------------
# Canal de l'agent
# ---------------------------------------------------------------------------
def test_second_request_refused_while_one_waits(env, capsys, monkeypatch):
    creds(env)
    settings(env)
    d = env["state"] / "secrets-agent"
    d.mkdir(parents=True)
    (d / "request.json").write_text("{}")
    rc, doc, _ = cli(["rotate", "secret-id"], capsys=capsys)
    assert rc == 2 and doc["code"] == "secrets-agent-busy" and (d / "request.json").read_text() == "{}"


def test_without_agent_and_root_refused(env, capsys, monkeypatch):
    creds(env)
    settings(env)
    monkeypatch.setattr(hsc, "_is_root", lambda: False)
    rc, doc, _ = cli(["rotate", "secret-id"], capsys=capsys)
    assert rc == 2 and doc["code"] == "secrets-agent-missing" and "sudo harvester-secrets" in doc["error"]


def test_agent_request_written_private_and_followed(env, capsys, monkeypatch):
    """Non root, agent présent : la demande est écrite 0600 puis suivie."""
    creds(env)
    settings(env)
    monkeypatch.setattr(hsc, "_is_root", lambda: False)
    d = env["state"] / "secrets-agent"
    d.mkdir(parents=True)
    (d / "agent.json").write_text("{}")
    seen = {}

    def sleep(_s):
        rp = d / "request.json"
        if rp.exists():
            seen["mode"] = rp.stat().st_mode & 0o777
            hsc._agent_module().Agent().process()
    monkeypatch.setattr(hsc, "_pause", sleep)
    rc, doc, _ = cli(["rotate", "secret-id"], capsys=capsys)
    assert rc == 0 and seen["mode"] == 0o600


# ---------------------------------------------------------------------------
# bundle, file
# ---------------------------------------------------------------------------
def test_bundle_password_only_on_stdin(env, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(hsc, "COSIGN_KEY", cosign_test_key()[1])
    monkeypatch.setattr(hsc, "_cosign_runner", lambda *a: None)          # pas d'outil cosign
    reg = make_registry()
    try:
        argv = ["bundle", "--registry", reg.host, "--user", "svc", "--password-stdin", "--tag", "9.9.9-1",
                "-o", str(tmp_path / "out"), "--plain-http"]
        rc, doc, err = cli(argv, stdin="Pw-AppCo-777\n", capsys=capsys)
        assert rc == 0, doc
        assert doc["name"] == "harvester-ops-openbao-9.9.9-9.9.9-1.tar.gz" and doc["cosign"] == "not-run"
        assert "Pw-AppCo-777" not in json.dumps(doc) + err + " ".join(argv)
        assert (tmp_path / "out" / doc["name"]).exists()
        rc, doc, err = cli(argv[:5] + argv[6:], stdin="Pw-AppCo-777\n", capsys=capsys)
        assert rc == 2 and doc["code"] == "bad-input"
        rc, doc, err = cli(argv, stdin="wrong-password\n", capsys=capsys)
        assert rc == 1 and "wrong-password" not in json.dumps(doc) + err
    finally:
        reg.close()


def test_bundle_bad_output_name(env, capsys, tmp_path):
    rc, doc, _ = cli(["bundle", "--user", "svc", "--password-stdin", "-o", str(tmp_path / "x.tar.gz")],
                     stdin="pw\n", capsys=capsys)
    assert rc == 2 and doc["code"] == "bundle-bad-name"


def test_file_prints_a_private_copy(env, capsys):
    creds(env)
    settings(env)
    env["srv"].put_raw("harvester-ops", f"{PREFIX}/ssh-cluster/h1", {"private_key": VALUE})
    ss._STORE = None
    try:
        rc, out, err = cli(["file", "openbao:ssh-cluster/h1#private_key"], capsys=capsys)
    finally:
        ss._STORE = None
    p = Path(str(out).strip())
    assert rc == 0, err
    assert p.read_text() == VALUE and (p.stat().st_mode & 0o777) == 0o600 and VALUE not in err
    p.unlink()


def test_file_launched_by_the_console_without_a_copy(env, capsys, monkeypatch):
    creds(env)
    settings(env)
    monkeypatch.setenv(ss.SPAWNED_ENV, "1")
    ss._STORE = None
    try:
        rc, out, err = cli(["file", "openbao:ssh-cluster/h1#private_key"], capsys=capsys)
    finally:
        ss._STORE = None
    assert rc == 1 and "openbao-spawned-no-file" in err and not str(out).strip()
    assert not env["srv"].requests("auth/approle/login")


def test_in_process_agent_writes_nothing_through_a_planted_link(env, capsys, tmp_path):
    """2.9.0 second pass: `sudo harvester-secrets` hands the request to the
    agent in memory; a link planted as secrets-agent/ receives nothing."""
    creds(env)
    settings(env)
    outside = tmp_path / "outside"
    outside.mkdir()
    d = env["state"] / "secrets-agent"
    if d.exists():
        os.rename(d, tmp_path / "old")
    d.symlink_to(outside)
    rc, doc, _ = cli(["rotate", "secret-id"], capsys=capsys)
    assert rc != 0 and list(outside.iterdir()) == []
    assert not d.is_symlink()
