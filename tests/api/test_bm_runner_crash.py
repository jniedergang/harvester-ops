"""Un déroulé de longue durée ne meurt jamais en laissant son action
« running ».

Vu en réel (banc bare-metal, PR 7) : le port du serveur d'artefacts était
déjà pris sur node1, `pxe_server.start()` levait avant les blocs `try` du
déroulé d'installation, le fil mourait, l'action restait « running » pour
toujours et la configuration servie (qui porte le jeton du cluster) restait
sur le disque. Ces tests jouent une exception à chaque endroit non protégé
et exigent : action finie en échec, message sans secret ni chemin, fichiers
à secrets effacés, jetons révoqués, serveur d'artefacts arrêté.
"""

import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "bin" / "lib"))

import app as wapp          # noqa: E402
import pxe_server as px     # noqa: E402

SECRET = "SECRET-FIXTURE-TOKEN"


def fake_run():
    # cancel_requested / kill_grace : champs de tout ActionRun depuis la 1.99.0
    return SimpleNamespace(id="crash0000001", status="starting", _cancel=False,
                           cancel_requested=False, kill_grace=None,
                           error_summary=None, emit=Mock(), close=Mock())


def steps(run, status="error"):
    return [c.args[0] for c in run.emit.call_args_list
            if c.args[0].get("type") == "step" and c.args[0].get("status") == status]


@pytest.fixture()
def real_pxe(monkeypatch):
    """Vrai serveur d'artefacts sur un port éphémère, arrêté avant et après."""
    px.stop()
    px.revoke(*list(px._TOKENS))
    monkeypatch.setenv("HARVESTER_OPS_PXE_PORT", "0")
    monkeypatch.setattr(wapp, "_BM_PXE_USERS", [0], raising=False)
    yield px
    px.stop()
    px.revoke(*list(px._TOKENS))


@pytest.fixture()
def taken_port(monkeypatch):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    port = s.getsockname()[1]
    monkeypatch.setenv("HARVESTER_OPS_PXE_PORT", str(port))
    yield port
    s.close()


def install_env(monkeypatch, tmp_path):
    (tmp_path / "src.iso").write_bytes(b"iso")
    profile = {"ok": True, "power_state": "Off", "virtualmedia_path": "/media",
               "boot_targets": ["Cd"], "uefi_targets": ["HD.fixture"], "system_path": "/system"}
    replacements = {
        "_bmc_discover_one": lambda *a: profile,
        "_bm_nic_warning": lambda *a: None,
        "_iso_dir": lambda: tmp_path,
        "_iso_work_dir": lambda: tmp_path,
        "_bm_artifact_base_urls": lambda *a: ("http://fixture", "http://fixture"),
        "_bm_join_pubkey": lambda *a: None,
        # la configuration rendue porte le jeton : elle ne doit pas survivre
        "_harvester_install_config": lambda *a, **kw: f"token: {SECRET}\n",
        "_bm_boot_network_args": lambda *a: "",
        "_bm_remaster_stream": lambda *a: 0,
        "_bm_power_off": lambda *a: (True, ""),
        "_redfish_get": lambda *a, **kw: {"PowerState": "Off"},
    }
    for name, fn in replacements.items():
        monkeypatch.setattr(wapp, name, fn)
    monkeypatch.setattr(wapp._bmd, "load_inventory", lambda *a: None)
    return {"bmc_host": "192.0.2.10", "bmc_user": "u", "bmc_password": SECRET,
            "iso": "src.iso", "mode": "join", "cluster": "fixture",
            "hostname": "n1", "token": SECRET}


def boom(exc):
    def f(*a, **kw):
        raise exc
    return f


# (nom, fonction remplacée, exception) : chaque point où le déroulé levait
# hors de ses blocs try
INSTALL_CRASHES = [
    ("config-render", "_harvester_install_config", KeyError(SECRET)),
    ("boot-network", "_bm_boot_network_args", RuntimeError(SECRET)),
    ("remaster", "_bm_remaster_stream", RuntimeError(SECRET)),
    ("remaster-oserror", "_bm_remaster_stream", OSError(2, "No such file", f"/x/{SECRET}")),
    ("power-read", "_redfish_get", ValueError(SECRET)),
    ("preflight", "_bmc_discover_one", TypeError(SECRET)),
]


@pytest.mark.parametrize("name,target,exc", INSTALL_CRASHES, ids=[c[0] for c in INSTALL_CRASHES])
def test_install_runner_crash_ends_failed_and_cleans(monkeypatch, tmp_path, real_pxe, name, target, exc):
    opts = install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wapp, target, boom(exc))
    run = fake_run()
    wapp._baremetal_install_runner(run, opts)          # ne lève pas
    assert run.status == "error" and run.exit_code == 1
    run.close.assert_called_once()
    errs = steps(run)
    assert errs, "une étape en erreur dit l'échec"
    assert type(exc).__name__ in errs[-1]["message"]
    for text in [e["message"] for e in errs] + [run.error_summary]:
        assert SECRET not in text and "/x/" not in text
    assert not list(tmp_path.glob("config-*.yaml")), "configuration servie effacée"
    assert not list(tmp_path.glob("install-*")), "ISO remasterisée effacée"
    assert not px._TOKENS, "jetons révoqués"
    assert not px.is_running(), "serveur d'artefacts arrêté"


def test_install_runner_base_exception_is_reraised_after_cleanup(monkeypatch, tmp_path, real_pxe):
    opts = install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wapp, "_bm_remaster_stream", boom(KeyboardInterrupt()))
    run = fake_run()
    with pytest.raises(KeyboardInterrupt):
        wapp._baremetal_install_runner(run, opts)
    assert run.status == "error"
    run.close.assert_called_once()
    assert not list(tmp_path.glob("config-*.yaml"))
    assert not px._TOKENS and not px.is_running()


def test_install_runner_port_taken_names_the_port(monkeypatch, tmp_path, real_pxe, taken_port):
    opts = install_env(monkeypatch, tmp_path)
    run = fake_run()
    wapp._baremetal_install_runner(run, opts)
    assert run.status == "error"
    assert str(taken_port) in run.error_summary and "in use" in run.error_summary
    assert not list(tmp_path.glob("config-*.yaml"))
    assert not px.is_running()
    run.close.assert_called_once()


def test_install_runner_failure_keeps_server_for_another_user(monkeypatch, tmp_path, real_pxe):
    """Le serveur est partagé (séries de 4 installations) : un échec n'arrête
    pas le serveur tant qu'un autre déroulé s'en sert."""
    port = wapp._bm_pxe_acquire({})
    other = px.issue(tmp_path / "other.iso", "iso")
    opts = install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wapp, "_bm_remaster_stream", boom(RuntimeError("x")))
    run = fake_run()
    wapp._baremetal_install_runner(run, opts)
    assert run.status == "error"
    assert px.is_running() and px._SERVER.server_address[1] == port
    assert list(px._TOKENS) == [other]


def test_install_runner_success_releases_the_server(monkeypatch, tmp_path, real_pxe):
    opts = install_env(monkeypatch, tmp_path)
    monkeypatch.setattr(wapp, "_bm_remaster_stream", lambda *a: 1)   # échec ordinaire
    run = fake_run()
    wapp._baremetal_install_runner(run, opts)
    assert run.status == "error" and "remastering" in run.error_summary
    assert not px.is_running() and not px._TOKENS
    assert not list(tmp_path.glob("config-*.yaml"))


def test_pxe_start_port_taken_is_a_clear_error(real_pxe, taken_port):
    with pytest.raises(px.PortUnavailable) as e:
        px.start()
    assert str(taken_port) in str(e.value)
    assert isinstance(e.value, OSError)
    assert not px.is_running()


# --- autres déroulés : découverte, série, transfert -------------------------

def disc_opts():
    return {"bmc_host": "192.0.2.21", "bmc_user": "u", "bmc_password": SECRET,
            "iso": "h.iso", "advertise_host": "127.0.0.1"}


def _disc_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "ISO_DIR", tmp_path / "iso")
    (tmp_path / "iso").mkdir()
    (tmp_path / "iso" / "h.iso").write_bytes(b"source")


def _crash_iso_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(wapp, "_iso_dir", boom(RuntimeError(SECRET)))


def _crash_bmd(tmp_path, monkeypatch):
    _disc_iso(tmp_path, monkeypatch)
    monkeypatch.setattr(wapp._bmd, "run", boom(LookupError(SECRET)))


DISCOVER_CRASHES = [("iso-dir", _crash_iso_dir, "RuntimeError"),
                    ("discovery", _crash_bmd, "LookupError")]


@pytest.mark.parametrize("name,setup,expect", DISCOVER_CRASHES, ids=[c[0] for c in DISCOVER_CRASHES])
def test_discover_runner_crash_ends_failed(monkeypatch, tmp_path, real_pxe, name, setup, expect):
    setup(tmp_path, monkeypatch)
    run = fake_run()
    wapp._baremetal_discover_runner(run, disc_opts())
    assert run.status == "error"
    run.close.assert_called_once()
    assert expect in run.error_summary and SECRET not in run.error_summary
    assert not px.is_running()


def test_discover_runner_port_taken_names_the_port(monkeypatch, tmp_path, real_pxe, taken_port):
    _disc_iso(tmp_path, monkeypatch)
    run = fake_run()
    wapp._baremetal_discover_runner(run, disc_opts())
    assert run.status == "error"
    assert str(taken_port) in run.error_summary
    assert not px.is_running()


def test_batch_runner_crash_outside_its_try_ends_failed():
    plan = [{"bmc_host": "h", "mode": "create"}]          # pas de hostname : KeyError
    run = fake_run()
    run.result = None
    wapp._baremetal_batch_runner(run, plan)
    assert run.status == "error" and "KeyError" in run.error_summary
    run.close.assert_called_once()
    assert plan == [], "secrets de la série effacés"


def test_transfer_runner_crash_before_its_try_ends_failed(monkeypatch):
    monkeypatch.setattr(wapp.tempfile, "TemporaryFile", boom(OSError(28, "No space", "/x")))
    run = fake_run()
    run.result = None
    wapp._vm_transfer_runner(run, ["true"], capture=True)
    assert run.status == "error"
    assert "/x" not in run.error_summary
    run.close.assert_called_once()
