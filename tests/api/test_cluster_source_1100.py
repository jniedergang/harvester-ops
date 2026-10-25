"""Navigation 2.0, étape 1 : une seule source pour le cluster courant.

Le sélecteur `#cluster-select` disparaît à l'étape 2 : un module qui le lit
ou le pilote encore casserait en silence. Seuls `app.js` (qui le remplit en
attendant) et `router.js` y ont droit ; les autres passent par
`App.getCurrentCluster()`, `App.listClusters()`, `App.managementCluster()`
ou `App.openHost(...)`.
"""

import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
JS = ROOT / "web/static/js"
ALLOWED = {"app.js", "router.js"}
# `conn-cluster-select` (diagnostic de connexion des réglages) est un autre
# sélecteur : seul l'identifiant exact compte.
NEEDLE = re.compile(r"(?<![\w-])cluster-select(?![\w-])")


def offenders(root):
    out = []
    for p in sorted(Path(root).glob("*.js")):
        if p.name in ALLOWED:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if NEEDLE.search(line):
                out.append(f"{p.name}:{n}: {line.strip()}")
    return out


def test_no_module_reads_the_cluster_select():
    bad = offenders(JS)
    assert bad == [], "lectures directes du sélecteur de cluster :\n" + "\n".join(bad)


def test_the_needle_ignores_the_connection_select():
    assert not NEEDLE.search("$('#conn-cluster-select')")
    assert NEEDLE.search("document.querySelector('#cluster-select')")
    assert NEEDLE.search("document.getElementById('cluster-select')")


def test_cluster_source_mutation(tmp_path):
    """Mutation volontaire : une lecture réinjectée dans une copie de capi.js
    doit faire échouer le contrôle (il lit vraiment les fichiers)."""
    for p in JS.glob("*.js"):
        shutil.copy(p, tmp_path / p.name)
    capi = tmp_path / "capi.js"
    capi.write_text(capi.read_text(encoding="utf-8")
                    + "\nconst _x = document.querySelector('#cluster-select')?.value;\n", encoding="utf-8")
    bad = offenders(tmp_path)
    assert len(bad) == 1 and bad[0].startswith("capi.js:"), bad


def test_app_publishes_the_single_source():
    src = (JS / "app.js").read_text(encoding="utf-8")
    ret = src[src.rindex("return { init"):]
    for name in ("getCurrentCluster", "listClusters", "refreshClusters", "managementCluster",
                 "setManagementCluster", "openHost"):
        assert name in ret, name
    assert "harvester_ops_mgmt_cluster" in src


def test_gpu_and_migrations_navigate_through_the_router():
    """GPU et Migrations ne changent plus la valeur du sélecteur : ils
    demandent l'hôte au routeur."""
    gpus = (JS / "gpus.js").read_text(encoding="utf-8")
    fk = (JS / "forklift-global.js").read_text(encoding="utf-8")
    assert "App.openHost('harvester', b.dataset.cluster, 'params', 'addons', 'list')" in gpus
    assert "App.openHost('harvester', cluster, 'imports', 'forklift', 'waves')" in fk
    for src in (gpus, fk):
        assert "dispatchEvent(new Event('change'" not in src


MGMT_USERS = ("capi.js", "terraform.js", "tf-declarations.js", "capi-services.js")


def test_management_pages_use_the_management_cluster():
    for f in MGMT_USERS:
        assert "managementCluster()" in (JS / f).read_text(encoding="utf-8"), f
