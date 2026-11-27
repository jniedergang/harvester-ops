"""2.10.1 tâche 5 : réglages TLS de Harvester selon son contrôleur d'ingress.

`ssl-parameters` était dit « sans effet » sans condition (Harvester 1.9 neuf,
Traefik). C'est faux sur un 1.9.0 venu de 1.8 qui garde ingress-nginx
(harvlab2). Le contrôleur est lu sur le cluster (HelmChart de RKE2 et
services de kube-system) : `ssl-parameters` est sans effet sous Traefik seul,
`traefik-default-tls-options` sous ingress-nginx seul ; aucun badge quand la
lecture ne dit rien.

Hypothèse à confirmer au banc (tâche 1, essais 5 et 6) : après la mise à jour
en 1.9.1 d'un cluster venu de 1.8 (harvlab), le HelmChart `rke2-ingress-nginx`
et le service `rke2-ingress-nginx-controller` ont disparu de kube-system, et
`rke2-traefik` est présent (chart et service).

Mutation rapportée (rapport 2.10.1, tâche 5) : « inerte sans condition »
(META `inert: True` remis) -> les cas harvlab2 et « unknown » échouent.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import hv_settings as hs  # noqa: E402


def obj(name):
    return {"metadata": {"name": name, "namespace": "kube-system"}}


HARV1 = ([obj("rke2-traefik"), obj("rke2-traefik-crd"), obj("rke2-canal")], [obj("rke2-traefik"), obj("kube-dns")])
HARVLAB2 = ([obj("rke2-ingress-nginx"), obj("rke2-canal")], [obj("ingress-expose"), obj("kube-dns")])
HARVLAB = ([obj("rke2-traefik"), obj("rke2-traefik-crd")], [obj("rke2-traefik")])
BOTH = ([obj("rke2-traefik"), obj("rke2-ingress-nginx")], [obj("rke2-traefik"), obj("rke2-ingress-nginx-controller")])


@pytest.mark.parametrize("label,charts,services,want", [
    ("harv1, 1.9.0 fresh", *HARV1, "traefik"),
    ("harvlab2, 1.9.0 from 1.8", *HARVLAB2, "nginx"),
    ("harvlab, 1.9.1 from 1.8", *HARVLAB, "traefik"),
    ("both controllers", *BOTH, "both"),
    ("charts unreadable, services say traefik", None, [obj("rke2-traefik")], "traefik"),
    ("services unreadable, charts say nginx", [obj("rke2-ingress-nginx")], None, "nginx"),
    ("nothing readable", None, None, "unknown"),
    ("no controller found", [obj("rke2-canal")], [obj("kube-dns")], "unknown"),
    ("traefik-crd alone is not Traefik", [obj("rke2-traefik-crd")], [], "unknown"),
])
def test_ingress_kind(label, charts, services, want):
    assert hs.ingress_kind(charts, services) == want, label


def settings():
    return [{"metadata": {"name": n}, "value": "", "default": ""}
            for n in ("ssl-parameters", "traefik-default-tls-options", "ssl-certificates", "log-level")]


@pytest.mark.parametrize("kind,inert", [
    ("traefik", {"ssl-parameters"}),
    ("nginx", {"traefik-default-tls-options"}),
    ("both", set()),
    ("unknown", set()),
    (None, set()),
])
def test_rows_inert_follows_the_controller(kind, inert):
    rows = {r["name"]: r for r in hs.rows(settings(), ingress=kind)}
    assert {n for n, r in rows.items() if r["inert"]} == inert
    assert rows["ssl-certificates"]["inert"] is False and rows["log-level"]["inert"] is False


def test_rows_default_has_no_inert_badge():
    """Appelant sans lecture du contrôleur : aucune règle fixe par version."""
    assert not any(r["inert"] for r in hs.rows(settings()))


@pytest.mark.parametrize("label,charts,services,ingress,inert", [
    ("harvlab2", *HARVLAB2, "nginx", ["traefik-default-tls-options"]),
    ("harv1", *HARV1, "traefik", ["ssl-parameters"]),
    ("reads failed", None, None, "unknown", []),
])
def test_route_reads_the_controller(monkeypatch, label, charts, services, ingress, inert):
    sys.path.insert(0, str(ROOT / "web"))
    import app as wapp
    wapp.app.config["TESTING"] = True
    monkeypatch.setattr(wapp, "_kubectl_for_cluster", lambda c: "/kc")
    monkeypatch.setattr(wapp, "_cluster_reachable", lambda kc: True)

    def kj(kc, *args, **kw):
        if args[1] == hs.K_SETTING:
            return {"items": settings()}
        got = charts if args[1].startswith("helmcharts") else services
        return None if got is None else {"items": got}
    monkeypatch.setattr(wapp, "_kubectl_json", kj)
    d = wapp.app.test_client().get("/api/hv-settings/h1").get_json()
    assert d["ingress"] == ingress
    assert [r["name"] for r in d["items"] if r["inert"]] == inert
