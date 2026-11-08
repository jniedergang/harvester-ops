"""2.9.0, corrections de la revue des tâches 5 à 7 (première passe).

Chaque cas a été vu rouge avant sa correction. Mutations jouées (rapport) :
« bail par fil seulement » (I1), « hold_secrets ignoré » (I1),
« bail jamais rendu » (I1).
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_secret_migrate_290 import KC, appenv, env, ss, wapp  # noqa: E402,F401


def _in_request_thread(fn):
    """Joue `fn` dans un fil qui meurt aussitôt (fil werkzeug d'une requête)."""
    box = {}
    t = threading.Thread(target=lambda: box.update(v=fn()))
    t.start()
    t.join()
    return box["v"]


# ---------------------------------------------------------------------------
# I1 : bail porté par l'action, pas par le fil
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("how", ["created-in-request", "held-explicitly", "in-command"])
def test_copy_resolved_by_the_request_is_kept_for_the_action(appenv, how):
    """Le fil de la requête reçoit la copie partagée, crée l'action et meurt ;
    la copie expire : elle reste tant que l'action n'est pas finie, puis
    part avec elle."""
    w, st = appenv["wapp"], appenv["store"]
    ref = st.put("kubeconfig", "h1", "kubeconfig", KC)

    def request():
        if how == "created-in-request":
            p = st.shared_file(ref, "kubectl")
            run = w.ActionRun("lease-1", "capi-install:h1", "h1", [])
        else:                                            # copie résolue APRÈS la création de l'action
            run = w.ActionRun("lease-1", "capi-install:h1", "h1", [])
            p = st.shared_file(ref, "kubectl")
            run.hold_secrets(str(p)) if how == "held-explicitly" else run.hold_secrets("tool", f"--kubeconfig={p}")
        return p, run
    p, run = _in_request_thread(request)
    st.clear_cache()                                     # copie retirée (expirée, cache vidé)
    st.openbao.sweep()
    assert p.exists() and p.read_text() == KC            # l'action l'utilise encore
    run.close()
    assert not p.exists()


def test_a_finished_request_without_action_does_not_keep_the_copy(appenv):
    st = appenv["store"]
    ref = st.put("kubeconfig", "h1", "kubeconfig", KC)
    p = _in_request_thread(lambda: st.shared_file(ref, "kubectl"))
    st.clear_cache()
    st.openbao.sweep()
    assert not p.exists()


def test_capi_install_holds_its_kubeconfig_and_key(appenv):
    """api_capi_install résout kubeconfig et clé dans la requête : l'action
    les tient (lu dans la source, la route complète demande un cluster)."""
    import inspect
    src = inspect.getsource(appenv["wapp"].api_capi_install)
    assert "run.hold_secrets(kc, key_path)" in src
    assert src.index("run.hold_secrets(kc, key_path)") < src.index("threading.Thread(")
