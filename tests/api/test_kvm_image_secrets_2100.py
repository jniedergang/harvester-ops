"""2.10.0 (tâche 13, essai R4 en réel) : un envoi d'image vers un hôte KVM
(`images/upload`, `images/fetch`) lance `harvester-kvm image upload` par son
propre relais (`_kvm_run_cli`), pas par `run_action_thread`. Avec la classe
`ssh-kvm` dans OpenBao, l'outil ne recevait aucune copie de la clé de l'hôte
(`HARVESTER_OPS_SECRET_FILES` absent) et finissait en `SecretError`
(« unexpected internal error »), vu sur node2. Désormais :

* l'action d'envoi porte les hôtes visés (`secret_clusters`) et fige les
  secrets personnels au déclenchement, avec le jeton de la personne ;
* chaque passage de l'outil reçoit le fichier de correspondance des copies,
  préparé par le service dans le fil (copies figées reprises), effacé après.
"""

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import app as wapp  # noqa: E402
import secret_store as ss  # noqa: E402


def test_image_run_carries_the_hosts_and_freezes_personal_secrets(monkeypatch):
    seen = []
    monkeypatch.setattr(wapp, "_freeze_person_secrets", lambda run: seen.append(tuple(run.secret_clusters)))
    with wapp.app.test_request_context("/api/kvm/hosts/n1/images/upload", method="POST"):
        run, err = wapp._kvm_image_run("n1", "kvm:image-upload:n1/default/a.iso", ["n1", "n2"], "default",
                                       "a.iso", ["harvester-kvm"])
    try:
        assert err is None
        assert tuple(run.secret_clusters) == ("kvm:n1", "kvm:n2")
        assert seen == [("kvm:n1", "kvm:n2")]
    finally:
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS.pop(run.id, None)


def test_image_run_refused_by_openbao_is_not_registered(monkeypatch):
    def refuse(run):
        e = ss.SecretError("openbao-denied", "ssh-kvm", "n1", "denied")
        e.person = True                        # refus fait au jeton de la personne
        raise e
    monkeypatch.setattr(wapp, "_freeze_person_secrets", refuse)
    before = set(wapp.ACTIONS)
    with wapp.app.test_request_context("/api/kvm/hosts/n1/images/upload", method="POST"):
        run, err = wapp._kvm_image_run("n1", "kvm:image-upload:n1/default/b.iso", ["n1"], "default", "b.iso",
                                       ["harvester-kvm"])
        body, status = err
        assert run is None and status == 403 and body.get_json()["code"] == "openbao-denied"
    assert set(wapp.ACTIONS) == before

def test_each_tool_pass_gets_the_secret_files(monkeypatch, tmp_path):
    mp = tmp_path / "secret-files.json"
    mp.write_text("{}")
    calls, envs = [], []

    def spawn(clusters, refs=(), files=None, identity=None, skip=()):
        calls.append((tuple(clusters), dict(files or {}), identity, tuple(skip)))
        return {ss.FILES_ENV: str(mp)}, mp

    class P:
        def __init__(self, cmd, **kw):
            envs.append(kw.get("env"))
            self.stderr = iter(())

        def wait(self):
            return 0
    monkeypatch.setattr(wapp, "_spawn_secret_env", spawn)
    monkeypatch.setattr(wapp, "_KVM_POPEN", P)
    run = types.SimpleNamespace(cluster="kvm:n1", secret_clusters=("kvm:n1", "kvm:n2"),
                                secret_files={"openbao:ssh-kvm/n1#private_key": "/run/x"}, secret_skip=(),
                                secret_identity="person", emit=lambda e: None, emit_progress=lambda s: None,
                                proc=None, events=[])
    rc, _data, _last = wapp._kvm_run_cli(run, ["harvester-kvm", "image", "upload"])
    assert rc == 0
    assert calls == [(("kvm:n1", "kvm:n1", "kvm:n2"), {"openbao:ssh-kvm/n1#private_key": "/run/x"}, ss.SERVICE, ())]
    assert envs[0] is not None and envs[0][ss.FILES_ENV] == str(mp)
    assert not mp.exists()                     # correspondance effacée après le passage


def test_no_secret_env_without_openbao(monkeypatch):
    envs = []

    class P:
        def __init__(self, cmd, **kw):
            envs.append(kw.get("env"))
            self.stderr = iter(())

        def wait(self):
            return 0
    monkeypatch.setattr(wapp, "_spawn_secret_env", lambda *a, **k: ({}, None))
    monkeypatch.setattr(wapp, "_KVM_POPEN", P)
    run = types.SimpleNamespace(cluster="kvm:n1", emit=lambda e: None, emit_progress=lambda s: None, proc=None)
    rc, _d, _l = wapp._kvm_run_cli(run, ["harvester-kvm"])
    assert rc == 0 and ss.FILES_ENV not in (envs[0] or {})


# ---------------------------------------------------------------------------
# Revue finale m-A : l'action d'image est créée AVANT la lecture des secrets
# personnels et le contrôle d'occupation ; sur un refus d'OpenBao, un 409 ou
# toute autre erreur, la jauge `actions_in_flight` revient à sa valeur et le
# bail des copies personnelles (tmpfs) est rendu.
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


class _FakeStore:
    _bao = None                                # pas de bail créé par le constructeur

    def __init__(self):
        self.released = []

    def release(self, lease):
        self.released.append(lease)


def _in_flight():
    return wapp.metric_actions_in_flight._value.get()


@pytest.mark.parametrize("case", ["openbao-refused", "busy", "other-error"])
def test_image_run_refusals_give_back_gauge_and_lease(monkeypatch, case):
    store = _FakeStore()
    monkeypatch.setattr(ss, "_STORE", store)
    lease = object()

    def freeze(run):
        run.secret_lease = lease               # copies personnelles déjà tenues
        if case == "openbao-refused":
            raise ss.SecretError("openbao-denied", "ssh-kvm", "n1", "denied")
        if case == "other-error":
            raise RuntimeError("unexpected")
    monkeypatch.setattr(wapp, "_freeze_person_secrets", freeze)
    monkeypatch.setattr(wapp, "_kvm_holds_busy", lambda holds: [("n1", "volume", "default/c.iso")]
                        if case == "busy" else None)
    monkeypatch.setattr(wapp, "_busy_response", lambda busy: ("busy", 409))
    before_ids, before = set(wapp.ACTIONS), _in_flight()
    with wapp.app.test_request_context("/api/kvm/hosts/n1/images/upload", method="POST"):
        if case == "other-error":
            with pytest.raises(RuntimeError):
                wapp._kvm_image_run("n1", "kvm:image-upload:n1/default/c.iso", ["n1"], "default", "c.iso",
                                    ["harvester-kvm"])
        else:
            run, err = wapp._kvm_image_run("n1", "kvm:image-upload:n1/default/c.iso", ["n1"], "default",
                                           "c.iso", ["harvester-kvm"])
            assert run is None and err[1] == (409 if case == "busy" else 503)
    assert _in_flight() == before
    assert store.released == [lease]
    assert set(wapp.ACTIONS) == before_ids


def test_cli_action_freeze_error_gives_back_the_lease(monkeypatch):
    """Même chemin pour `_cli_action` : les copies personnelles déjà tenues
    au moment du refus sont rendues avec la jauge."""
    store = _FakeStore()
    monkeypatch.setattr(ss, "_STORE", store)
    lease = object()

    def freeze(run):
        run.secret_lease = lease
        raise ss.SecretError("openbao-denied", "ssh-kvm", "n1", "denied")
    monkeypatch.setattr(wapp, "_freeze_person_secrets", freeze)
    before_ids, before = set(wapp.ACTIONS), _in_flight()
    with wapp.app.test_request_context("/api/x", method="POST"):
        with pytest.raises(ss.SecretError):
            wapp._cli_action("kvm:n1", "test-2100:lease", ["/usr/bin/env", "true"], "true", check_busy=False)
    assert _in_flight() == before and store.released == [lease]
    assert set(wapp.ACTIONS) == before_ids
