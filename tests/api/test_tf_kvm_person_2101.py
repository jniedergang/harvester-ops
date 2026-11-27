"""2.10.1 tâche 2 (R4) : déclaration Terraform KVM lancée par une personne
connectée par Rancher, plus longue que son jeton OpenBao.

OpenBao simulé, jeton de personne allumé. Au déclenchement, la clé de l'hôte
(`ssh-kvm`) et le jeton des variables (`tf-kvm`) sont lus avec le jeton de la
personne. Ensuite ce jeton expire (révoqué, et la politique de la personne
vidée : toute lecture par lui rendrait 403). Le fil de l'action relit clé et
variables avec l'identité du service, comme le fait `_tf_decl_runner_body` :
il doit être servi par la copie et le cache issus du jugement de la personne,
sans aucune requête à OpenBao et sans marque « service tardif ».

Tableau : un cas par chemin (copie servie ; copie absente -> relecture par
le service marquée `late-service` ; fil qui tenterait le jeton de la
personne -> refus). Mutation rapportée (rapport 2.10.1, tâche 2) : « le fil
relit avec le jeton de la personne » (identity=idt au lieu de SERVICE) ->
`test_worker_after_expiry_reads_nothing` échoue (SecretError
openbao-person-no-token).

Hypothèse à confirmer au banc (tâche 1, essai 1) : le fil démarre avant
l'échéance du cache commun ; une action plus longue que le cache ne relit rien
après ses étapes de départ (`_tf_kvm_stage` et `_tf_kvm_env` sont appelées au
début du fil seulement).
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
sys.path.insert(0, str(Path(__file__).parent))
import app as wapp  # noqa: E402
import secret_store as ss  # noqa: E402
from test_sso_person_token_2100 import JWT_MOUNT, KV, PREFIX, console, open_session  # noqa: E402,F401
from test_secret_identity_routes_2100 import fake_run, in_request, in_thread, kv  # noqa: E402

KEY_REF = "openbao:ssh-kvm/k1#private_key"


@pytest.fixture
def r4(console, tmp_path, monkeypatch):
    bao = console["bao"]
    bao.jwt[JWT_MOUNT]["allow"] += [(f"{KV}/data/{PREFIX}/tf-kvm/", {"read", "create", "update"}),
                                    (f"{KV}/metadata/{PREFIX}/tf-kvm/", {"read"})]
    bao.put_raw(KV, f"{PREFIX}/ssh-kvm/k1", {"private_key": "KVM-KEY-R4"})
    p = Path(ss._STORE.state_dir) / ss.SETTINGS_FILE
    doc = json.loads(p.read_text())
    doc["migrated"] = {"tf-kvm": {"at": 1}}
    p.write_text(json.dumps(doc))
    ss._STORE._sig, ss._STORE._checked = None, 0.0
    ws = tmp_path / "tf" / ".kvm" / "k1" / "decls" / "d1"
    ws.mkdir(parents=True)
    monkeypatch.setattr(wapp, "_tf_kvm_host", lambda h: ({"name": "k1", "ssh_key": KEY_REF}, None))
    monkeypatch.setattr(wapp, "_tf_decl_ws", lambda decl, create=True: ws)
    monkeypatch.setattr(wapp._tfk, "token_vars", lambda rendered: ["rke2_token"])
    sess = open_session()
    idts = in_request(sess, lambda: {c: wapp._secret_identity(c) for c in ("ssh-kvm", "tf-kvm")})
    mark = in_request(sess, lambda: wapp._tf_kvm_prefetch({"cluster": "k1"}, [], idts))
    assert mark == "person"
    tok = console["pt"].get(sess.sid)._tok.value
    # jeton de la personne échu : révoqué côté OpenBao, politique vidée
    bao.jwt[JWT_MOUNT]["allow"] = []
    bao.tokens.pop(tok, None)
    console["pt"].drop(sess.sid)
    return dict(console, ws=ws, sess=sess, idts=idts, tok=tok)


def _worker_reads(run, ws, identity):
    """Ce que fait le fil (`_tf_decl_runner_body`) : clé de l'hôte puis
    variables, dans `_late_reads(run)`."""
    def go():
        with wapp._late_reads(run):
            key = wapp._kd.key_path(Path(ss._STORE.state_dir), KEY_REF, "tf-kvm-ssh", identity=identity)
        with wapp._late_reads(run):
            val = wapp._tf_kvm_secret(ws, "rke2_token", identity=identity)
        return Path(key).read_text(), val
    return in_thread(go)


def test_worker_after_expiry_reads_nothing(r4):
    bao = r4["bao"]
    run = fake_run(secret_identity="person")
    n = len(bao.log)
    key, val = _worker_reads(run, r4["ws"], ss.SERVICE)
    assert key == "KVM-KEY-R4" and len(val) == 64
    assert len(bao.log) == n, "le fil n'interroge pas OpenBao après le déclenchement"
    assert run.secret_identity == "person" and not run.events


def test_worker_without_copy_is_marked_late_service(r4):
    bao = r4["bao"]
    run = fake_run(secret_identity="person")
    ss._STORE.clear_cache()
    key, _val = _worker_reads(run, r4["ws"], ss.SERVICE)
    assert key == "KVM-KEY-R4"
    assert run.secret_identity == "late-service"
    steps = [e for e in run.events if e.get("step_id") == "secrets"]
    assert steps and all(e["status"] == "warn" for e in steps)
    assert "KVM-KEY-R4" not in json.dumps(run.events)
    used = {r["headers"]["x-vault-token"] for r in kv(bao, "ssh-kvm/k1")[-1:]}
    assert r4["tok"] not in used


def test_worker_never_uses_the_person_token(r4):
    run = fake_run(secret_identity="person")
    ss._STORE.clear_cache()
    with pytest.raises(ss.SecretError) as e:
        _worker_reads(run, r4["ws"], r4["idts"]["ssh-kvm"])
    assert e.value.code == "openbao-person-no-token"


@pytest.mark.parametrize("mode", ["plan", "apply", "destroy", "destroy-plan", "decrypt-state"])
def test_every_mode_reads_through_the_runner_as_service(mode):
    """Toutes les exécutions (destroy et decrypt-state compris) passent par
    le même fil : ses deux lectures de secrets sont faites par le service
    dans `_late_reads(run)`."""
    import inspect
    src = inspect.getsource(wapp._tf_decl_runner_body)
    assert src.count("with _late_reads(run):") >= 2
    assert "_tf_kvm_stage(ws, host_decl, identity=_sst.SERVICE)" in src
    assert "_tf_kvm_env(ws, rendered, identity=_sst.SERVICE)" in src
    launch = inspect.getsource(wapp._tf_decl_launch)
    assert f'"{mode}"' in launch
