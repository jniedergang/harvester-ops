"""Réglages communs aux tests d'API.

v1.81.0 : les lectures partagées (read_share.py) servent la même réponse
quelques secondes ; un test qui change sa simulation entre deux appels du même
écran recevrait la précédente. Coupées par défaut ici, activées par les tests
qui les vérifient (test_read_share_181.py).

Même chose pour le relevé gardé des hôtes KVM (kvm_ops.SURVEYS) : coupé
ici, vérifié par ses propres instances (test_kvm_next_review.py)."""
import os

os.environ.setdefault("HARVESTER_OPS_READ_SHARE", "0")
os.environ.setdefault("HARVESTER_OPS_KVM_SURVEY_CACHE", "0")


# 2.2.0 : l'installation bare-metal lit le BMC (système déjà présent ?)
# avant de partir. Les tests d'API n'ont pas de BMC : la lecture rend
# « inconnu » (aucun refus), sauf pour les tests marqués `real_presence`
# (test_bm_presence_220.py), qui simulent eux-mêmes les réponses Redfish.
import sys  # noqa: E402

import pytest  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "real_presence: keep the real bare-metal system presence check")
    config.addinivalue_line("markers", "live: needs a real backend and the --live flag")


@pytest.fixture(autouse=True)
def _no_bmc_presence_read(request, monkeypatch):
    wapp = sys.modules.get("app")
    if wapp is None or not hasattr(wapp, "_bm_system_presence") or request.node.get_closest_marker("real_presence"):
        yield
        return
    monkeypatch.setattr(wapp, "_bm_system_presence",
                        lambda host, user, pwd, system_path=None: {"state": "unknown", "evidence": [],
                                                                   "confirm_name": host, "source": None})
    yield
