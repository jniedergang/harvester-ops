"""Processus lecteurs : les écrans lourds calculés hors du processus principal
(v1.83.0).

Mesuré sur un banc de 30 clusters de 200 VMs : une fois les lectures faites
sans kubectl, le décodage des listes et la construction des écrans tournent
dans l'unique processus Python de la console, derrière son verrou global
(GIL). Avec 30 personnes, la topologie montait à 6,7 s pendant que la machine
restait aux trois quarts libre. Les écrans lourds (topologie, liste des VMs,
carte du stockage, fabrique réseau) sont donc calculés dans quelques processus
lecteurs, chacun sur son cœur.

Ce que le processus principal décide reste chez lui : la connexion, le rôle,
l'identité présentée au cluster (le kubeconfig de la personne est transmis tel
quel). Le lecteur exécute la vue, sans ses décorateurs, et rend la réponse
avec les refus de la RBAC rencontrés. Un lecteur ne surveille aucun cluster
et ne lance aucune action (HARVESTER_OPS_READ_WORKER=1 à l'import de app).

Réglage : HARVESTER_OPS_READ_WORKERS (nombre de processus ; 0 = tout dans le
processus principal, comme avant).
"""
import multiprocessing as mp
import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor

_pool = None
_lock = threading.Lock()
MAX_TASKS = int(os.environ.get("HARVESTER_OPS_READ_WORKER_TASKS", "200"))


def default_workers():
    n = os.cpu_count() or 1
    return 0 if n < 3 else min(4, n - 1)


def configured():
    try:
        return max(0, int(os.environ.get("HARVESTER_OPS_READ_WORKERS", default_workers())))
    except ValueError:
        return default_workers()


def _die_with_parent():
    """Un lecteur s'arrête avec la console : sans cela, une console tuée
    laissait ses lecteurs derrière elle (vu sur le banc). PR_SET_PDEATHSIG ne
    convient pas : il suit le FIL qui a créé le lecteur (celui d'une requête,
    qui se termine aussitôt) et tuait les lecteurs à peine nés (vu aussi). On
    surveille donc le processus parent."""
    ppid = os.getppid()

    def watch():
        import time
        while True:
            time.sleep(2)
            if os.getppid() != ppid:
                os._exit(0)
    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def _init():
    _die_with_parent()
    os.environ["HARVESTER_OPS_READ_WORKER"] = "1"
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    # app.py, lancé comme script, a déjà été réimporté par « spawn » sous le
    # nom __mp_main__ : le réemployer (un second import doublerait tout,
    # métriques Prometheus comprises, et le lecteur meurt)
    main = sys.modules.get("__mp_main__")
    if main is not None and os.path.basename(getattr(main, "__file__", "") or "") == "app.py":
        sys.modules["app"] = main
    import app  # noqa: F401


def pool():
    global _pool
    with _lock:
        if _pool is None:
            n = configured()
            if not n:
                return None
            # « spawn » : un fork d'un processus à fils d'exécution peut hériter
            # d'un verrou tenu et se figer
            # le lecteur se reconnaît dès l'import de app.py (voir IS_READ_WORKER)
            os.environ["HARVESTER_OPS_READ_WORKER_SPAWN"] = "1"
            # renouvelé toutes les MAX_TASKS tâches : borne la mémoire d'un
            # lecteur, qui décode de grosses listes
            _pool = ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"),
                                        initializer=_init, max_tasks_per_child=MAX_TASKS)
        return _pool


def render(endpoint, view_args, query_string, kubeconfig, role, user, timeout=180):
    """La réponse de la vue `endpoint`, calculée dans un lecteur :
    (statut, corps, type, en-têtes, refus, liste des refus)."""
    p = pool()
    if p is None:
        raise RuntimeError("no read workers")
    return p.submit(_render, endpoint, view_args, query_string, kubeconfig, role, user).result(timeout)


def _render(endpoint, view_args, query_string, kubeconfig, role, user):
    import inspect
    import app as A
    from flask import g
    A._READ_WORKER_CTX.update(kc=kubeconfig, role=role, user=user)
    try:
        with A.app.test_request_context(query_string=query_string):
            view = inspect.unwrap(A.app.view_functions[endpoint])
            resp = A.app.make_response(view(**view_args))
            headers = [(k, v) for k, v in resp.headers.items()
                       if k.lower() not in ("content-length", "set-cookie", "content-type")]
            return (resp.status_code, resp.get_data(), resp.mimetype, headers,
                    getattr(g, "cluster_denied", None), list(getattr(g, "cluster_denials", None) or []))
    finally:
        A._READ_WORKER_CTX.clear()


def shutdown():
    global _pool
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=False, cancel_futures=True)
            _pool = None
