"""v2.6.0 : publication d'un paquet dans un registre (tâche 8) contre un
registre simulé en TLS à CA propre : publication complète puis seconde
sans envoi, reprise après coupure, élément seul, tag d'une autre
empreinte (rien d'écrit), élément « à venir » sauté, aucun identifiant
dans le résultat ni le journal ; lancement hors contexte de requête."""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import artefacts as art  # noqa: E402
import oci_layout as ol  # noqa: E402
import oci_push as op  # noqa: E402
import registries as rg  # noqa: E402
import test_harvester_artefacts_cli_260 as tcli  # noqa: E402
import test_oci_layout_260 as t260  # noqa: E402
import test_oci_push_175 as t175  # noqa: E402

ha = tcli.ha
tls_pki = t175.tls_pki
PUSH_PW = "Push-Secret-Never-Echo-9"


@pytest.fixture
def world(tmp_path, tls_pki, monkeypatch):
    """Paquet importé (chart + deux images + manifeste + politique) et
    registre de destination en TLS, rattaché à harvlab2."""
    src = t175.FakeRegistry()
    shared = t175.payload(4000, b"base")
    t260.put_index(src, "stefanprodan/podinfo", "6.9.2", ["amd64"], shared=shared)
    t260.put_index(src, "x/tool", "1.0", ["amd64"], shared=shared)
    helm = tcli.HelmRepo({("podinfo", "6.9.2"): tcli.chart_tgz()})
    (tmp_path / "q.yaml").write_text("kind: ResourceQuota\n")
    (tmp_path / "p.yaml").write_text("kind: Policy\n")
    desc = {"name": "socle", "version": "2026.11", "items": [
        {"name": "podinfo", "kind": "chart", "chart": {"repo": helm.url, "name": "podinfo", "version": "6.9.2"},
         "images": [f"{src.host}/stefanprodan/podinfo:6.9.2"]},
        {"name": "outils", "kind": "images", "images": [f"{src.host}/x/tool:1.0"]},
        {"name": "quotas", "kind": "manifest", "files": ["q.yaml"]},
        {"name": "regles", "kind": "policy", "files": ["p.yaml"]}]}
    (tmp_path / "d.json").write_text(json.dumps(desc))
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARVESTER_OPS_STATE_DIR", str(state))
    monkeypatch.setenv("HARVESTER_OPS_CONFIG", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("HARVESTER_OPS_WORK_DIR", str(tmp_path))
    tcli.run("pack", tmp_path / "d.json", "--out", tmp_path / "out", "--plain-http", src.host, check=0)
    env = dict(os.environ)
    tcli.run("import", tmp_path / "out" / "socle-2026.11.tar.gz", "--accept-unsigned", env=env, check=0)
    src.close()
    helm.close()
    dst = t175.FakeRegistry(user="push", password=PUSH_PW, basic=True)
    import ssl
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_pki / "tls.crt", tls_pki / "tls.key")
    dst.srv.socket = ctx.wrap_socket(dst.srv.socket, server_side=True)
    rg.save(state, "amorcage", {"kind": "external", "endpoint": dst.host, "ca_pem": (tls_pki / "ca.crt").read_text(),
                                "usage": ["images", "charts"], "mirrors": ["docker.io"], "clusters": ["harvlab2"]})
    monkeypatch.setattr(rg, "read_creds", lambda kube, name, role: {"username": "push", "password": PUSH_PW}
                        if role == "push" else None)
    yield {"state": state, "dst": dst, "src_host": src.host, "tmp": tmp_path}
    dst.close()


def publish(capsys, *extra):
    code = ha.main(["publish", "socle", "2026.11", "--cluster", "harvlab2", "--registry", "amorcage",
                    "--kubeconfig", "/dev/null", "--person", "alice", *extra])
    o = capsys.readouterr()
    return code, (json.loads(o.out) if o.out.strip() else None), o.err


def test_a_full_publication_then_a_second_one_that_sends_nothing(world, capsys):
    code, res, err = publish(capsys)
    assert code == 0, err
    assert res["images"] == 2 and res["charts"] == 1 and res["pushed_blobs"] > 0
    host = world["src_host"].replace(":", "_")
    m = world["dst"].manifests
    assert (f"{host}/stefanprodan/podinfo", "6.9.2") in m and (f"{host}/x/tool", "1.0") in m
    assert ("charts/podinfo", "6.9.2") in m
    assert "regles: skipped (coming in 2.7)" in err and "quotas: skipped (manifests are not published)" in err
    pub = rg.load(world["state"], "amorcage")["published"]
    assert [(p["package"], p["version"], p["by"]) for p in pub] == [("socle", "2026.11", "alice")]
    uploads = len(world["dst"].uploads)
    code, res2, _ = publish(capsys)
    assert code == 0 and res2["pushed_blobs"] == 0 and len(world["dst"].uploads) == uploads
    assert len(rg.load(world["state"], "amorcage")["published"]) == 1          # pas de doublon
    for text in (json.dumps(res), err, json.dumps(rg.load(world["state"], "amorcage"))):
        assert PUSH_PW not in text


def test_an_interrupted_publication_resumes_without_sending_twice(world, capsys, monkeypatch):
    real = op.Registry.push_blob_file
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise op.RegistryError("connection lost")
        return real(self, *a, **kw)
    monkeypatch.setattr(op.Registry, "push_blob_file", flaky)
    code, _, err = publish(capsys)
    assert code == 1 and "connection lost" in err
    sent_before = len(world["dst"].uploads)
    assert sent_before == 2
    monkeypatch.setattr(op.Registry, "push_blob_file", real)
    code, res, err = publish(capsys)
    assert code == 0, err
    assert res["skipped_blobs"] + res["mounted_blobs"] >= 2                  # les deux déjà envoyés
    assert len(world["dst"].uploads) == sent_before + res["pushed_blobs"]


def test_one_item_alone(world, capsys):
    code, res, _ = publish(capsys, "--items", "outils")
    assert code == 0 and res["images"] == 1 and res["charts"] == 0
    assert not [k for k in world["dst"].manifests if k[0].startswith("charts/")]


def test_a_tag_with_another_digest_blocks_before_any_write(world, capsys):
    host = world["src_host"].replace(":", "_")
    world["dst"].manifests[(f"{host}/x/tool", "1.0")] = (b'{"schemaVersion":2}', op.M_OCI)
    before = dict(world["dst"].manifests)
    code, res, err = publish(capsys)
    assert code == 2 and [f["code"] for f in res["findings"]] == ["publish-tag-differs"]
    assert world["dst"].uploads == [] and world["dst"].manifests == before
    assert not [x for x in world["dst"].log if x[0] in ("POST", "PUT", "PATCH")]


def test_dry_run_lists_what_would_go(world, capsys):
    code, res, _ = publish(capsys, "--dry-run")
    assert code == 0 and res["blocked"] is False
    assert sorted((t["kind"], t["state"]) for t in res["todo"]) == [("chart", "new"), ("image", "new"), ("image", "new")]
    assert world["dst"].uploads == []


@pytest.mark.parametrize("change,code", [
    (lambda w: rg.update(w["state"], "amorcage", lambda d: d.update(clusters=[])), "registry-not-attached"),
    (lambda w: rg.update(w["state"], "amorcage", lambda d: d.update(usage=["charts"])), "registry-no-usage"),
    (lambda w: art.save_settings(w["state"], True), "package-untrusted-strict"),
    (lambda w: rg.delete(w["state"], "amorcage"), "registry-unknown"),
])
def test_each_refusal_writes_nothing(world, capsys, change, code):
    change(world)
    rc, res, _ = publish(capsys)
    assert rc == 2 and code in [f["code"] for f in res["findings"]]
    assert world["dst"].uploads == []


def test_an_unknown_item_is_refused(world, capsys):
    rc, res, _ = publish(capsys, "--items", "nope")
    assert rc == 2 and res["findings"][0]["code"] == "publish-item-unknown"


# ---------------------------------------------------------------------------
# Console : route et lancement hors requête
# ---------------------------------------------------------------------------
@pytest.fixture
def console(world, monkeypatch, tmp_path):
    import app as wapp
    import test_deploy_routes_240 as tr
    monkeypatch.setattr(wapp, "_bm_profiles_state", lambda: world["state"])
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    monkeypatch.setattr(wapp, "open_mode", lambda: True)
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    kc = tmp_path / "kc"
    kc.write_text(tr.KC)
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: str(kc) if c == "harvlab2" else None)
    return wapp


def wait(wapp, aid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        run = wapp.ACTIONS.get(aid)
        if run is not None and run.status in ("done", "error", "cancelled"):
            return run
        time.sleep(0.05)
    raise AssertionError("action did not finish")


def test_the_launch_needs_no_request_context(console, world, monkeypatch):
    """Garde de M-request-ctx : la chaîne d'un plan lance la publication hors
    de toute requête."""
    import flask
    assert not flask.has_request_context()

    def no_session():
        raise RuntimeError("the launch read the request or the session")
    for fn in ("current_user", "current_cluster_identity", "current_role", "_sso_session", "_local_session"):
        monkeypatch.setattr(console, fn, no_session)
    # le registre simulé n'est pas joignable depuis l'outil lancé (les
    # identifiants sont simulés dans ce processus seulement) : on vérifie le
    # lancement, la commande et l'action, pas la publication
    run, err = console._artefacts_publish_launch("harvlab2", "/dev/null", ("socle", "2026.11"), "amorcage",
                                                 ["outils"], cluster_user="ops", person="alice")
    assert err is None and run.action == "artefacts-publish:socle-2026.11@amorcage"
    assert run.cluster_user == "ops"
    assert "--items" in run.cmd and "outils" in run.cmd and not [a for a in run.cmd if a.startswith("/")]
    wait(console, run.id)


def test_the_route_refuses_a_blocked_publication_and_leaks_no_secret(console, world, monkeypatch):
    host = world["src_host"].replace(":", "_")
    world["dst"].manifests[(f"{host}/x/tool", "1.0")] = (b'{"schemaVersion":2}', op.M_OCI)
    seen = []
    monkeypatch.setattr(console, "_capi_run_json", lambda cmd, spec=None, timeout=180: seen.append(cmd) or (
        ({"blocked": True, "findings": [{"code": "publish-tag-differs", "level": "block", "facts": {}}]}, 2), None))
    c = console.app.test_client()
    r = c.post("/api/artefacts/socle/2026.11/publish", json={"cluster": "harvlab2", "registry": "amorcage"})
    assert r.status_code == 409 and r.get_json()["findings"][0]["code"] == "publish-tag-differs"
    assert seen[0][-1] == "--dry-run" and PUSH_PW not in json.dumps(r.get_json())
    assert c.post("/api/artefacts/socle/2026.11/publish", json={"cluster": "nope", "registry": "amorcage"}).status_code == 404
    assert c.post("/api/artefacts/socle/2026.12/publish", json={"cluster": "harvlab2", "registry": "amorcage"}).status_code == 404
    assert c.post("/api/artefacts/socle/2026.11/publish", json={"cluster": "harvlab2", "registry": "amorcage",
                                                                "items": ["Bad Item"]}).status_code == 400


def test_a_published_action_keeps_no_credential(console, world, monkeypatch):
    monkeypatch.setattr(console, "_capi_run_json", lambda cmd, spec=None, timeout=180: (
        ({"blocked": False, "findings": [], "todo": []}, 0), None))
    r = console.app.test_client().post("/api/artefacts/socle/2026.11/publish",
                                       json={"cluster": "harvlab2", "registry": "amorcage"})
    assert r.status_code == 202
    run = wait(console, r.get_json()["action_id"])
    dump = json.dumps([list(run.events), run.result, run.cmd])
    assert PUSH_PW not in dump
    db = console.ACTIONS_DB
    if Path(db).is_file():
        with sqlite3.connect(db) as con:
            rows = con.execute("select * from actions where id = ?", (run.id,)).fetchall()
        assert PUSH_PW not in json.dumps(rows, default=str)


def test_the_detail_marks_later_kinds_and_lists_the_registries(console, world, capsys):
    assert publish(capsys, "--items", "outils")[0] == 0
    d = console.app.test_client().get("/api/artefacts/socle/2026.11").get_json()
    kinds = {it["name"]: it.get("later", False) for it in d["index"]["items"]}
    assert kinds == {"podinfo": False, "outils": False, "quotas": False, "regles": True}
    assert [p["registry"] for p in d["published"]] == ["amorcage"]
    assert {im["name"].split("/", 1)[1] for im in d["images"]} == {"stefanprodan/podinfo:6.9.2", "x/tool:1.0"}
