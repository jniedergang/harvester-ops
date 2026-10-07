"""v1.87.0 : une copie arrêtée faute de stockage est dite dans la vue de toutes
les migrations, avec la raison en bulle, au lieu d'un « copie des disques »
muet. Données routées, même banc que test_forklift_global_176."""

import copy
import json
import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import expect  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_forklift_global_176 as G  # noqa: E402
from test_forklift_global_176 import two_clusters  # noqa: E402,F401  (fixture)

REASON = ("the target storage cannot place this disk (Longhorn: insufficient storage), the copy waits for it: "
          "free space, add a disk, or raise storage-over-provisioning-percentage")


def blocked_data():
    d = copy.deepcopy(G.GLOBAL_DATA)
    vm = d["clusters"][0]["waves"][0]["vms"][0]
    vm["progress"] = {"done": 0, "total": 40960}
    vm["blocked"] = REASON
    d["clusters"][0]["waves"][0]["message"] = f"vmwlab-src-1: {REASON}"
    return d


def test_a_blocked_copy_is_said_with_its_reason(context, flask_server, two_clusters):
    page = G.open_global(context, flask_server, data=blocked_data, initial_cluster="harv-fake")
    row = page.locator('#tab-forkliftglobal tr[data-fkg-wave="vague-1"]')
    expect(row).to_be_visible(timeout=10000)
    mark = row.locator(".fk-blocked")
    expect(mark).to_have_text(re.compile("Disk copy waiting for storage"))
    expect(mark).to_have_attribute("data-tip", REASON)
    # les autres lignes gardent leur étape
    expect(page.locator('#tab-forkliftglobal tr[data-fkg-wave="vague-2"] .fk-blocked')).to_have_count(0)
