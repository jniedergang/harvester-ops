"""v1.99.0 : une annulation ne se perd plus dans un `except Exception`.

Le rejeu réel de la réplication l'a montré : son `Cancelled` dérivait
d'`Exception`, et environ 15 % des annulations étaient avalées par une
lecture gardée (nettoyage d'un chemin, sonde d'un cluster). Le même motif
existait dans harvester-network, harvester-vm-transfer (et sa bibliothèque),
harvester-resources et harvester-capi. Leur `Cancelled` est maintenant une
`BaseException` : chaque test lève l'annulation de l'outil À L'INTÉRIEUR
d'un bloc gardé réel et vérifie qu'elle en ressort.

Mutation de contrôle : remettre `class Cancelled(Exception)` dans un outil
fait échouer ses lignes.
"""

import importlib.util
import signal
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))

TOOLS = {
    "network": "harvester-network.py",
    "vm-transfer": "harvester-vm-transfer.py",
    "resources": "harvester-resources.py",
    "capi": "harvester-capi.py",
}


def _load(tool):
    name = "cancel_" + tool.replace("-", "_")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "bin" / TOOLS[tool])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _raiser(exc):
    def boom(*a, **k):
        raise exc
    return boom


# Blocs gardés réels, appelés pendant une action annulable. Chacun rend une
# fonction qui, avec l'annulation `exc` levée au bon endroit, exécute le bloc.
def _site_read_yaml(monkeypatch, tmp_path, exc):
    """kube._read_yaml (lecture de la configuration, commune aux quatre
    outils) : `except Exception` autour de PyYAML."""
    import kube
    import yaml
    monkeypatch.setattr(kube.subprocess, "run", _raiser(OSError("no yq")))
    monkeypatch.setattr(yaml, "safe_load", _raiser(exc))
    f = tmp_path / "c.yaml"
    f.write_text("a: 1\n")
    return lambda: kube._read_yaml(f)


def _site_transfer_unlabel(monkeypatch, tmp_path, exc):
    """vm_transfer_run._unlabel : `except Exception: pass`."""
    import vm_transfer_run as run

    class K:
        patch = staticmethod(_raiser(exc))
    return lambda: run._unlabel(K(), "virtualmachines", "default", "vm")


def _site_transfer_delete_quiet(monkeypatch, tmp_path, exc):
    """vm_transfer_run.delete_quiet : `except Exception: return False`."""
    import vm_transfer_run as run

    class K:
        delete = staticmethod(_raiser(exc))
    return lambda: run.delete_quiet(K(), "pvc", "default", "x")


def _site_capi_values(monkeypatch, tmp_path, exc):
    """capi_services.check : valeurs lues par PyYAML sous `except Exception`."""
    import capi_services as cs
    import yaml
    monkeypatch.setattr(yaml, "safe_load", _raiser(exc))
    spec = {"service": next(iter(cs.CATALOG)), "name": "svc", "cluster": "ns/c",
            "chart": "chart", "namespace": "ns", "version": "1.0", "params": {},
            "repo": "https://charts.example", "values": ""}
    return lambda: cs.check(spec, {"caaph": True, "clusters": ["ns/c"]})


def _site_resources_upgrade(monkeypatch, tmp_path, exc):
    """hv_upgrade.upgrade_view : repoInfo lu sous `except Exception`."""
    import hv_upgrade
    import yaml
    monkeypatch.setattr(yaml, "safe_load", _raiser(exc))
    u = {"metadata": {"name": "u1"}, "spec": {"version": "v1.9.1"},
         "status": {"repoInfo": "release: {harvester: v1.9.1}"}}
    return lambda: hv_upgrade.upgrade_view(u)


def _site_resources_rancher_of(monkeypatch, tmp_path, exc):
    """hv_projects.rancher_of : kubeconfig lu sous `except Exception`."""
    import hv_projects
    import yaml
    monkeypatch.setattr(yaml, "safe_load", _raiser(exc))
    return lambda: hv_projects.rancher_of("clusters: []")


CASES = [
    ("network", _site_read_yaml),
    ("vm-transfer", _site_read_yaml),
    ("vm-transfer", _site_transfer_unlabel),
    ("vm-transfer", _site_transfer_delete_quiet),
    ("resources", _site_read_yaml),
    ("resources", _site_resources_upgrade),
    ("resources", _site_resources_rancher_of),
    ("capi", _site_read_yaml),
    ("capi", _site_capi_values),
]


@pytest.mark.parametrize("tool,site", CASES,
                         ids=[f"{t}-{s.__name__[6:]}" for t, s in CASES])
def test_a_cancel_inside_a_guarded_block_is_not_swallowed(monkeypatch, tmp_path, tool, site):
    mod = _load(tool)
    exc = mod.Cancelled("signal 15")
    call = site(monkeypatch, tmp_path, exc)
    with pytest.raises(mod.Cancelled):
        call()


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_cancelled_escapes_except_exception(tool):
    mod = _load(tool)
    assert issubclass(mod.Cancelled, BaseException)
    assert not issubclass(mod.Cancelled, Exception)
    # le gestionnaire de signal lève bien cette classe
    with pytest.raises(mod.Cancelled):
        mod._on_signal(signal.SIGTERM, None)


def test_the_network_undo_cannot_be_cut_by_a_second_signal():
    net = _load("network")
    before = signal.getsignal(signal.SIGTERM)

    class K:
        def __init__(self):
            self.deleted = []

        def get(self, kind, ns, name):
            # un second signal pendant le retour arrière serait ignoré
            assert signal.getsignal(signal.SIGTERM) == signal.SIG_IGN
            return {"metadata": {"name": name}}

        def delete(self, kind, ns, name):
            self.deleted.append((kind, name))
    try:
        k = K()
        net._undo(k, "subnet", {"name": "s1"}, "default/n1")
        assert k.deleted == [(net.K_SUBNET, "s1"), (net.K_NAD, "n1")]
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.parametrize("fail_at", [0, 1, 3])
def test_a_cancelled_download_leaves_no_partial_file(tmp_path, fail_at):
    """harvester-resources : image, journaux de mise à jour, kubeconfig. Un
    signal en cours d'écriture ne laisse ni fichier partiel sous le nom
    attendu ni fichier temporaire."""
    res = _load("resources")
    out = tmp_path / "img.qcow2"

    def chunks():
        for i in range(4):
            if i == fail_at:
                raise res.Cancelled("signal 15")
            yield b"x" * 10
    with pytest.raises(res.Cancelled):
        res._write_stream(out, chunks())
    assert list(tmp_path.iterdir()) == []


def test_a_completed_download_replaces_the_placeholder(tmp_path):
    """La console crée le fichier de sortie (mkstemp) : il est remplacé,
    mode 0600."""
    res = _load("resources")
    out = tmp_path / "kc.yaml"
    out.write_text("")
    assert res._write_stream(out, [b"ab", b"cd"]) == 4
    assert out.read_bytes() == b"abcd"
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kc.yaml"]
