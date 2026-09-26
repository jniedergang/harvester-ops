#!/usr/bin/env python3
"""harvester-resources : les objets d'un cluster Harvester que la console range
sous Cluster (Storage, Network, Add-ons, Security) et dans la fenêtre Backups.

Toute écriture de la console sur ces objets passe par ce script (parité CLI) ;
il s'utilise aussi seul.

  harvester-resources addon --cluster harv1 --namespace kube-system --name descheduler --enable
  harvester-resources addon --cluster harv1 --namespace kube-system --name descheduler --disable


Sorties : 0 fait, 1 échec, 2 refusé par le contrôle, 3 annulé. Les étapes
s'écrivent sur stderr en `STEP_EVENT|étape|statut|message`, que la console
relaie au dock.
"""

import argparse
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from kube import Kube, KubeError, cluster_config  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_BLOCKED, EXIT_CANCELLED = 0, 1, 2, 3
K_ADDON = "addons.harvesterhci.io"


class Cancelled(Exception):
    pass


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def _on_signal(signum, frame):
    raise Cancelled(f"signal {signum}")


def refusal(msg):
    """Un refus d'un webhook de Harvester, dit sans l'enveloppe de kubectl :
    « Error from server (BadRequest): admission webhook "validator.harvesterhci.io"
    denied the request: descheduler addon cannot be enabled as not enough
    nodes exist in the cluster » (vu sur harv1, un seul nœud)."""
    text = str(msg)
    if "denied the request:" in text:
        return "Harvester refused: " + text.split("denied the request:", 1)[1].strip()
    return text


def kube_from(args):
    entry = cluster_config(args.cluster) if args.cluster else None
    kc = args.kubeconfig or (entry or {}).get("kubeconfig")
    if not kc:
        raise SystemExit("give --cluster (with a kubeconfig in the configuration) or --kubeconfig")
    return Kube(kc)


# ---------------------------------------------------------------------------
# Add-ons (addons.harvesterhci.io)
#
# Activer un add-on installe son chart (Harvester le déploie par un HelmChart),
# le désactiver le retire. Vu sur harv1 (Harvester v1.9.0) : le statut passe
# par AddonEnabling / AddonDisabling puis AddonDeploySuccessful / AddonDisabled,
# et un échec se lit dans AddonDeployFailed ou la condition OperationFailed.
# ---------------------------------------------------------------------------

def addon_state(obj):
    """(statut, message d'échec éventuel) d'un Addon."""
    st = (obj or {}).get("status") or {}
    failed = ""
    for c in st.get("conditions") or []:
        if c.get("type") == "OperationFailed" and str(c.get("status")) == "True":
            failed = c.get("message") or c.get("reason") or "operation failed"
    return st.get("status") or "", failed


def addon_settled(status, failed, want):
    """True si l'add-on a atteint l'état voulu, False s'il a échoué, None
    s'il est encore en chemin."""
    if "Failed" in status or failed:
        return False
    if want and status in ("AddonDeploySuccessful", "AddonUpdateSuccessful", "AddonDeployed"):
        return True
    if not want and status == "AddonDisabled":
        return True
    return None


def set_addon(kube, ns, name, want, timeout=900, sleep=time.sleep, now=time.time):
    obj = kube.get(K_ADDON, ns, name)
    if obj is None:
        raise ValueError(f"no add-on {ns}/{name} on this cluster")
    label = "enable" if want else "disable"
    status, failed = addon_state(obj)
    if bool((obj.get("spec") or {}).get("enabled")) == want and addon_settled(status, "", want):
        step("patch", "done", f"{ns}/{name} is already {'enabled' if want else 'disabled'}")
        step("wait", "done", status)
        return EXIT_OK
    step("patch", "running", f"{label} {ns}/{name}")
    kube.patch(K_ADDON, ns, name, {"spec": {"enabled": want}})
    step("patch", "done", f"spec.enabled = {str(want).lower()}")
    step("wait", "running", "Harvester applies the change")
    deadline = now() + timeout
    last = None
    # Le contrôleur met quelques secondes à prendre la main : un statut encore
    # « succès » de l'état précédent ne doit pas être lu comme la fin.
    sleep(3)
    while now() < deadline:
        obj = kube.get(K_ADDON, ns, name) or {}
        status, failed = addon_state(obj)
        if status != last:
            step("wait", "running", status or "pending")
            last = status
        done = addon_settled(status, failed, want)
        if done is True and bool((obj.get("spec") or {}).get("enabled")) == want:
            step("wait", "done", status)
            return EXIT_OK
        if done is False:
            step("wait", "error", failed or status)
            return EXIT_FAIL
        sleep(5)
    step("wait", "error", f"still {last or 'pending'} after {timeout} s")
    return EXIT_FAIL


def cmd_addon(args):
    kube = kube_from(args)
    return set_addon(kube, args.namespace, args.name, args.enable, timeout=args.timeout)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-resources", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("addon", help="enable or disable a Harvester add-on")
    sp.set_defaults(fn=cmd_addon)
    sp.add_argument("--cluster", help="cluster name in the configuration")
    sp.add_argument("--kubeconfig", help="kubeconfig of the Harvester cluster")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--name", required=True)
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--enable", dest="enable", action="store_true")
    grp.add_argument("--disable", dest="enable", action="store_false")
    sp.add_argument("--timeout", type=int, default=900, help="seconds to wait for Harvester")

    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_signal)
    try:
        return args.fn(args)
    except ValueError as e:
        step("check", "error", str(e))
        return EXIT_BLOCKED
    except (KubeError, RuntimeError) as e:
        step(args.cmd, "error", refusal(e)[:300])
        return EXIT_FAIL
    except Cancelled:
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
