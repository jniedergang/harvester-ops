"""v1.4.36 — the 6 cluster-scoped list endpoints powering the Terraform
form's dropdowns.

The test server's harv-fake cluster points to a fake kubeconfig, so every
real `kubectl get` call returns a non-zero exit and our endpoints surface
a 502 with `error`. That's the contract we lock in: kubectl-failure → 502,
not 500 / no opaque traceback. The dropdown UI shows an empty list and
the user can still proceed (e.g. typing a name manually or via inline
create in Phase B).
"""


ENDPOINTS = [
    "/api/namespaces",
    "/api/images",
    "/api/networks",
    "/api/sshkeys",
    "/api/storageclasses",
    "/api/cloudinits",
]


def test_each_list_endpoint_exists(api):
    """Every endpoint must respond — 200 with a list or 502 with error.
    Either is fine; the form swallows errors and renders an empty <select>."""
    for ep in ENDPOINTS:
        status, payload = api(
            "GET", f"{ep}/harv-fake", expect_status=None
        )
        assert status in (200, 502), (
            f"{ep}/harv-fake: unexpected status {status}, payload={payload!r}"
        )
        if status == 200:
            assert isinstance(payload, list), (
                f"{ep}/harv-fake returned non-list: {type(payload).__name__}"
            )
        else:
            assert isinstance(payload, dict) and "error" in payload, (
                f"{ep}/harv-fake error response must carry 'error', got {payload!r}"
            )


def test_unknown_cluster_returns_502_with_error(api):
    """A non-existent cluster name must surface a 502 with a clear error
    (not a 500 or a stack trace) so the dropdown can render an explanatory
    state instead of crashing the form."""
    for ep in ENDPOINTS:
        status, payload = api(
            "GET", f"{ep}/does-not-exist", expect_status=None
        )
        assert status == 502, (
            f"{ep}/does-not-exist: expected 502, got {status} ({payload!r})"
        )
        assert "error" in payload
        assert "does-not-exist" in (payload.get("error") or "") or \
               "unknown cluster" in (payload.get("error") or ""), (
            f"{ep}/does-not-exist error message must be self-explanatory, "
            f"got {payload!r}"
        )


def test_reducer_image_preserves_display_name_and_size(api):
    """Each reducer narrows the kubectl JSON to a UI-friendly dict.
    For images, the dropdown shows display_name; the form may use
    name (`image-xxxxx`) and namespace to build the value. Lock those
    fields in so a reducer rewrite doesn't drop them."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    out = wapp._reduce_image({
        "metadata": {"name": "image-9dlfh", "namespace": "default"},
        "spec": {"displayName": "openSUSE Tumbleweed",
                 "sourceType": "upload", "url": "..."},
        "status": {"size": 4578082816, "progress": 100},
    })
    assert out == {
        "name": "image-9dlfh", "namespace": "default",
        "display_name": "openSUSE Tumbleweed",
        "source_type": "upload",
        "size": 4578082816, "progress": 100,
    }


def test_reducer_network_extracts_vlan_and_clusternetwork(api):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    out = wapp._reduce_network({
        "metadata": {
            "name": "prod-vlan100", "namespace": "default",
            "labels": {
                "network.harvesterhci.io/vlan-id": "100",
                "network.harvesterhci.io/clusternetwork": "mgmt",
            },
        },
        "spec": {"config": '{"cniVersion":"0.3.1"}'},
    })
    assert out["name"] == "prod-vlan100"
    assert out["vlan"] == "100"
    assert out["cluster_network"] == "mgmt"


def test_reducer_sshkey_preserves_fingerprint_and_pubkey(api):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    out = wapp._reduce_sshkey({
        "metadata": {"name": "capi-ssh-key", "namespace": "default"},
        "spec": {"publicKey": "ssh-ed25519 AAA..."},
        "status": {"fingerPrint": "ab:cd:ef"},
    })
    assert out["name"] == "capi-ssh-key"
    assert out["fingerprint"] == "ab:cd:ef"
    assert out["public_key"] == "ssh-ed25519 AAA..."


def test_reducer_sc_marks_default_class_via_annotation(api):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    out_def = wapp._reduce_sc({
        "metadata": {"name": "harv-rep1",
                     "annotations": {
                         "storageclass.kubernetes.io/is-default-class": "true",
                     }},
        "provisioner": "driver.longhorn.io",
        "reclaimPolicy": "Delete",
    })
    assert out_def["is_default"] is True
    assert out_def["provisioner"] == "driver.longhorn.io"
    out_nondef = wapp._reduce_sc({
        "metadata": {"name": "alt"},
        "provisioner": "driver.longhorn.io",
    })
    assert out_nondef["is_default"] is False


def test_reducer_cloudinit_detects_user_and_network_data(api):
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    out = wapp._reduce_cloudinit({
        "metadata": {"name": "ci-1", "namespace": "default"},
        "data": {"userdata": "...", "networkdata": "..."},
    })
    assert out["has_user_data"] is True
    assert out["has_network_data"] is True


def test_list_cache_hit_returns_within_ttl_without_recall(tmp_path,
                                                            monkeypatch):
    """Once a list endpoint has fetched, the next call inside
    LIST_CACHE_TTL must return the memoised data without invoking
    `_kubectl_json` again. This is the difference between 1 kubectl
    call per form render vs N (one per dropdown change)."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp

    monkeypatch.setattr(wapp, "_kubectl_for_cluster",
                        lambda c: "/dev/null")
    # Bust any inherited cache
    with wapp._list_lock:
        wapp._list_cache.clear()
    calls = []
    def fake_kubectl_json(kc, *args, timeout=15):
        calls.append(args)
        return {"items": [{"metadata": {"name": "a"}}]}
    monkeypatch.setattr(wapp, "_kubectl_json", fake_kubectl_json)

    # First call: should run kubectl once
    data1, err1 = wapp._list_k8s_resources("c1", "ns")
    assert err1 is None
    assert data1 == [{"name": "a", "namespace": None}]
    assert len(calls) == 1
    # Second call inside TTL: must be served from cache (calls unchanged)
    data2, err2 = wapp._list_k8s_resources("c1", "ns")
    assert data2 == data1
    assert len(calls) == 1, "cache hit should not invoke kubectl"


def test_namespaces_filters_system_namespaces(api):
    """A 200 from /api/namespaces (when kubectl ever succeeds) MUST NOT
    include kube-system / cattle-system / longhorn-system — those would
    pollute the VM-creation dropdown."""
    # This test is a guard against regression of the HIDDEN filter even
    # when the fake cluster yields a 502. We exercise the filter logic
    # by importing the endpoint helper directly.
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                          .resolve().parent.parent.parent / "web"))
    import app as wapp
    # Patch _list_k8s_resources to return a synthetic list
    orig = wapp._list_k8s_resources
    SYNTH = [
        {"name": "default", "namespace": None},
        {"name": "kube-system", "namespace": None},
        {"name": "longhorn-system", "namespace": None},
        {"name": "my-app", "namespace": None},
    ]
    try:
        wapp._list_k8s_resources = lambda *a, **kw: (SYNTH, None)
        with wapp.app.test_client() as c:
            r = c.get("/api/namespaces/whatever")
            assert r.status_code == 200
            names = [n["name"] for n in r.get_json()]
            assert "default" in names
            assert "my-app" in names
            assert "kube-system" not in names
            assert "longhorn-system" not in names
    finally:
        wapp._list_k8s_resources = orig
