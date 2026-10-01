"""v1.80.0 : profils d'installation multi-nœuds et séries.

* substitution des variables (absente = refus avec la ligne et le nom, YAML
  avancé relu AVANT de remplacer, valeurs exactes dans `content: |`) ;
* magasin des profils (0600, relatif au répertoire d'état, déplaçable) ;
* déroulé d'une série (création seule d'abord, jonctions deux à deux, une
  création ratée arrête tout) ;
* CSV des nœuds ; routes (authentification, rôle admin, limites) ;
* secrets jamais rangés ni renvoyés.
"""

import json
import os
import re
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
WEB = ROOT / "web"
BIN = ROOT / "bin"
sys.path.insert(0, str(WEB))
sys.path.insert(0, str(BIN / "lib"))

import app as wapp            # noqa: E402
import bm_profiles as bmp     # noqa: E402
import harvester_install_schema as his  # noqa: E402

TOKEN = "TOKSECRET-180a"
OSPW = "OSPWSECRET-180b"
BMCPW = "BMCSECRET-180c"

ADVANCED = """\
os:
  write_files:
  - path: /etc/NetworkManager/system-connections/storage.nmconnection
    permissions: '0600'
    content: |
      [connection]
      id=storage
      [ipv4]
      method=manual
      address1={{storage_ip}}/24
      # node {{hostname}}: storage # not a comment
  - path: /etc/ssh/sshd_config.d/10-listen.conf
    content: |
      ListenAddress {{ip}}
      ListenAddress {{admin_ip}}
"""

FIELDS = {
    "iso": "harvester.iso", "hostname": "{{hostname}}", "device": "/dev/sda",
    "mgmt_interfaces": ["{{mgmt_mac}}"], "bond_mode": "active-backup",
    "method": "static", "ip": "{{ip}}", "subnet_mask": "255.255.255.0",
    "gateway": "192.0.2.1", "dns": "192.0.2.53",
}


def profile(**over):
    doc = {"description": "rack A", "variables": ["storage_ip", "admin_ip"],
           "fields": dict(FIELDS), "advanced_yaml": ADVANCED}
    doc.update(over)
    return bmp.check_profile(doc)


def row(n, **values):
    v = {"hostname": f"node{n}", "ip": f"192.0.2.{10 + n}", "mgmt_mac": f"52:54:00:00:00:0{n}",
         "storage_ip": f"172.18.122.{100 + n}", "admin_ip": f"10.0.0.{n}"}
    v.update(values)
    return {"bmc_host": f"198.51.100.{n}", "bmc_user": "admin", "values": v}


def batch(n=3, **over):
    b = {"cluster_name": "rack-a", "vip": "192.0.2.100", "rows": [row(i + 1) for i in range(n)]}
    b.update(over)
    return b


# --- substitution ------------------------------------------------------------

def test_variables_land_inside_write_files_content_verbatim():
    opts = bmp.node_opts(profile(), batch(), 1, wapp._bm_field_type, his.dump_install_config)
    cfg = yaml.safe_load(opts["advanced_yaml"])
    files = cfg["os"]["write_files"]
    assert "address1=172.18.122.102/24\n" in files[0]["content"]
    # `: ` et `#` d'une ligne de bloc littéral restent du texte
    assert "# node node2: storage # not a comment\n" in files[0]["content"]
    assert files[1]["content"] == "ListenAddress 192.0.2.12\nListenAddress 10.0.0.2\n"
    assert opts["hostname"] == "node2" and opts["mgmt_interfaces"] == ["52:54:00:00:00:02"]
    assert opts["mode"] == "join" and opts["server_url"] == "https://192.0.2.100:443"
    assert "vip" not in opts and "cluster_name" not in opts


def test_first_row_creates_the_cluster():
    opts = bmp.node_opts(profile(), batch(), 0, wapp._bm_field_type, his.dump_install_config)
    assert opts["mode"] == "create" and opts["vip"] == "192.0.2.100"
    assert opts["cluster_name"] == "rack-a" and "server_url" not in opts


def test_a_value_with_yaml_syntax_does_not_break_the_document():
    """Substituer le texte puis le relire casserait ici : la valeur porte
    `: `, `#` et des guillemets."""
    prof = profile(variables=["storage_ip", "admin_ip", "note"],
                   advanced_yaml='os:\n  environment:\n    NOTE: "{{note}}"\n    RAW: {{note}}\n')
    b = batch(1)
    b["rows"][0]["values"]["note"] = 'a: b # "c"'
    adv = bmp.render_advanced(prof["advanced_yaml"], b["rows"][0]["values"])
    assert adv["os"]["environment"] == {"NOTE": 'a: b # "c"', "RAW": 'a: b # "c"'}


def test_a_lone_variable_takes_the_schema_type():
    adv = bmp.render_advanced("install:\n  management_interface:\n    vlan_id: {{vlan}}\n"
                              "os:\n  hostname: {{n}}\n",
                              {"vlan": "122", "n": "123"}, wapp._bm_field_type)
    assert adv["install"]["management_interface"]["vlan_id"] == 122
    # un nom d'hôte tout en chiffres reste du texte
    assert adv["os"]["hostname"] == "123"
    assert not his.check_install_config(adv)


def test_missing_variable_names_the_row_and_the_variable():
    b = batch()
    del b["rows"][2]["values"]["storage_ip"]
    with pytest.raises(bmp.ProfileError) as e:
        bmp.node_opts(profile(), b, 2, wapp._bm_field_type, his.dump_install_config)
    assert e.value.errors == [("row 3 {{storage_ip}}", "missing variable")]
    errs = bmp.check_batch(profile(), b)
    assert ("row 3 {{storage_ip}}", "missing variable") in errs


def test_values_are_single_line():
    b = batch(1)
    b["rows"][0]["values"]["admin_ip"] = "10.0.0.1\nPermitRootLogin yes"
    assert ("row 1 admin_ip", "control character or line break") in bmp.check_batch(profile(), b)


def test_batch_refuses_duplicates_and_unknown_variables():
    b = batch(2)
    b["rows"][1]["values"]["ip"] = b["rows"][0]["values"]["ip"]
    b["rows"][1]["bmc_host"] = b["rows"][0]["bmc_host"]
    b["rows"][1]["values"]["bogus"] = "x"
    errs = dict(bmp.check_batch(profile(), b))
    assert errs["row 2 ip"] == "same as row 1"
    assert errs["row 2 bmc_host"] == "same as row 1"
    assert errs["row 2 bogus"] == "unknown variable"
    assert dict(bmp.check_batch(profile(), batch(1, vip="", cluster_name="Bad_Name"))).keys() >= {"vip", "cluster_name"}


# --- profils -----------------------------------------------------------------

def test_profile_refuses_secrets_undeclared_variables_and_foreign_fields():
    with pytest.raises(bmp.ProfileError) as e:
        bmp.check_profile({"variables": ["ip", "ok_var", "Bad"],
                           "fields": {"token": "t", "password": "p", "vip": "1", "hostname": "{{nope}}"},
                           "advanced_yaml": "os:\n  hostname: {{other}}\n"})
    errs = dict(e.value.errors)
    assert errs["fields.token"].startswith("secret") and errs["fields.password"].startswith("secret")
    assert errs["fields.vip"] == "not a profile field"
    assert errs["variables[ip]"] == "reserved name" and errs["variables[Bad]"] == "invalid name"
    assert errs["{{nope}}"] == errs["{{other}}"] == "undeclared variable"


def test_store_is_private_and_moves_with_the_state_dir(tmp_path):
    state = tmp_path / "state"
    bmp.save_profile(state, "rack-a", profile())
    path = state / "profiles.d" / "rack-a.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    text = path.read_text()
    assert str(tmp_path) not in text                 # rien d'absolu
    moved = tmp_path / "elsewhere"
    shutil.move(str(state), str(moved))
    assert bmp.load_profile(moved, "rack-a")["fields"]["hostname"] == "{{hostname}}"
    [item] = bmp.list_profiles(moved)
    assert item["name"] == "rack-a" and set(item["uses"]) >= {"storage_ip", "hostname"}
    assert bmp.profile_path(moved, "../x") is None and bmp.profile_path(moved, "Rack") is None
    assert bmp.delete_profile(moved, "rack-a") and bmp.list_profiles(moved) == []


# --- CSV ---------------------------------------------------------------------

def test_csv_header_names_the_variables():
    rows = bmp.parse_nodes_csv("bmc_host;bmc_user;hostname;ip;storage_ip\n"
                               "198.51.100.1;admin;node1;192.0.2.11;172.18.122.101\n\n"
                               "198.51.100.2;admin;node2;192.0.2.12\n", ["storage_ip"])
    assert rows[0] == {"bmc_host": "198.51.100.1", "bmc_user": "admin",
                       "values": {"hostname": "node1", "ip": "192.0.2.11", "storage_ip": "172.18.122.101"}}
    assert rows[1]["values"]["storage_ip"] == ""


def test_csv_refusals():
    with pytest.raises(bmp.ProfileError) as e:
        bmp.parse_nodes_csv("bmc_host,colour\n1,2\n", [])
    assert ("column colour", "unknown column") in e.value.errors
    with pytest.raises(bmp.ProfileError):
        bmp.parse_nodes_csv("hostname\nx\n", [])               # pas de bmc_host
    with pytest.raises(bmp.ProfileError):
        bmp.parse_nodes_csv("bmc_host\n1,2\n", [])             # cellule de trop
    with pytest.raises(bmp.ProfileError):
        bmp.parse_nodes_csv("", [])


# --- déroulé d'une série -----------------------------------------------------

def test_create_first_then_joins_two_at_a_time():
    events, running, peak = [], [0], [0]
    lock = threading.Lock()

    def launch(i):
        with lock:
            events.append(("launch", i))
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        return f"a{i}"

    def wait(i, h):
        time.sleep(0.05)
        with lock:
            running[0] -= 1
            events.append(("end", i))
        return "done"

    states = bmp.run_batch(6, launch, wait, lambda *a: None, concurrency=2)
    assert states == ["done"] * 6
    assert events[:2] == [("launch", 0), ("end", 0)]            # la création seule, d'abord
    assert peak[0] == 2
    assert [e[1] for e in events if e[0] == "launch"] == [0, 1, 2, 3, 4, 5]


def test_a_failed_create_stops_the_joins():
    launched, reports = [], []
    states = bmp.run_batch(3, lambda i: launched.append(i) or i, lambda i, h: "error",
                           lambda i, s, m: reports.append((i, s)))
    assert launched == [0] and states == ["error", "skipped", "skipped"]
    assert (1, "skipped") in reports and (2, "skipped") in reports


def test_cancel_skips_rows_not_started():
    flag = {"c": False}

    def wait(i, h):
        flag["c"] = True
        return "done"
    states = bmp.run_batch(3, lambda i: i, wait, lambda *a: None, cancelled=lambda: flag["c"])
    assert states == ["done", "skipped", "skipped"]


def test_a_launch_refusal_is_reported_not_raised():
    states = bmp.run_batch(2, lambda i: (_ for _ in ()).throw(RuntimeError("busy")) if i else i,
                           lambda i, h: "done", lambda *a: None)
    assert states == ["done", "error"]


# --- routes ------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    wapp.app.config["TESTING"] = True
    state = tmp_path / "state"
    monkeypatch.setattr(wapp, "_bm_profiles_state", lambda: state)
    monkeypatch.setattr(wapp, "_BM_IMPORT_CACHE", {})
    monkeypatch.setattr(wapp, "current_user", lambda: "alice")
    monkeypatch.setattr(wapp, "current_cluster_identity", lambda: None)
    monkeypatch.setattr(wapp, "_safe_artifact_name", lambda n: n)
    monkeypatch.setattr(wapp, "load_config", lambda: {"clusters": []})
    started = []
    monkeypatch.setattr(wapp, "track_action",
                        lambda label, cluster, worker, *a: started.append((label, worker, a)) or "b1")
    return {"state": state, "started": started, "client": wapp.app.test_client()}


def body_of(p):
    return {"description": p["description"], "variables": p["variables"],
            "fields": p["fields"], "advanced_yaml": p["advanced_yaml"]}


def create(c, name="rack-a", **over):
    return c.post("/api/baremetal/profiles", json=dict(body_of(profile()), name=name, **over))


def test_crud(env):
    c = env["client"]
    assert create(c).status_code == 201
    assert create(c).status_code == 409
    d = c.get("/api/baremetal/profiles").get_json()
    assert [p["name"] for p in d["profiles"]] == ["rack-a"] and "vip" in d["builtins"]
    assert c.get("/api/baremetal/profiles/rack-a").get_json()["advanced_yaml"] == ADVANCED
    upd = dict(body_of(profile()), description="rack B")
    assert c.put("/api/baremetal/profiles/rack-a", json=upd).get_json()["description"] == "rack B"
    assert c.put("/api/baremetal/profiles/nope", json=upd).status_code == 404
    assert c.delete("/api/baremetal/profiles/rack-a").status_code == 200
    assert c.get("/api/baremetal/profiles/rack-a").status_code == 404


def test_save_refuses_what_the_installer_would_refuse(env):
    r = create(env["client"], advanced_yaml="install:\n  iso_url: http://x/{{hostname}}.iso\n")
    assert r.status_code == 400
    assert "install.iso_url" in r.get_json()["fields"]
    r = create(env["client"], name="b", fields=dict(FIELDS, token=TOKEN))
    assert r.status_code == 400 and TOKEN not in r.get_data(as_text=True)


def test_fields_as_yaml_and_a_start_from_a_config_file(env):
    c = env["client"]
    fy = yaml.safe_dump(FIELDS, sort_keys=False)
    r = c.post("/api/baremetal/profiles", json={"name": "y", "variables": "storage_ip, admin_ip",
                                                "fields_yaml": fy, "advanced_yaml": ADVANCED})
    assert r.status_code == 201, r.get_json()
    got = c.get("/api/baremetal/profiles/y").get_json()
    assert yaml.safe_load(got["fields_yaml"]) == FIELDS
    r = c.post("/api/baremetal/profiles", json={"name": "z", "fields_yaml": "a: [b"})
    assert r.status_code == 400 and r.get_json()["errors"][0][1] == "invalid YAML"
    text = (ROOT / "tests/api/fixtures/bm_config_177/operator.yaml").read_text()
    text = text.replace("token: example-token", f"token: {TOKEN}")
    r = c.post("/api/baremetal/profiles/from-config", json={"text": text})
    d = r.get_json()
    assert r.status_code == 200 and d["secrets_dropped"] is True
    assert TOKEN not in r.get_data(as_text=True)
    fields = yaml.safe_load(d["fields_yaml"])
    assert "mode" not in fields and "server_url" not in fields and "vip" not in fields
    assert fields["hostname"] and d["advanced_yaml"]
    assert wapp._BM_IMPORT_CACHE == {}                          # rien gardé


def test_csv_route_drops_bmc_passwords(env):
    c = env["client"]
    create(c)
    r = c.post("/api/baremetal/profiles/rack-a/csv",
               json={"text": f"bmc_host,bmc_user,bmc_password,hostname\n198.51.100.1,admin,{BMCPW},n1\n"})
    assert r.status_code == 200 and BMCPW not in r.get_data(as_text=True)
    assert r.get_json()["passwords_dropped"] is True
    assert c.post("/api/baremetal/profiles/rack-a/csv", json={"text": "bmc_host,x\n1,2\n"}).status_code == 400


def test_render_previews_every_row_masked(env):
    c = env["client"]
    create(c)
    r = c.post("/api/baremetal/profiles/rack-a/render", json=batch(2))
    d = r.get_json()
    assert r.status_code == 200 and d["ok"], d
    assert [x["mode"] for x in d["rows"]] == ["create", "join"]
    cfg = yaml.safe_load(d["rows"][1]["yaml"])
    assert cfg["server_url"] == "https://192.0.2.100:443" and cfg["token"] == wapp._BM_MASK
    assert "address1=172.18.122.102/24" in cfg["os"]["write_files"][0]["content"]
    assert cfg["install"]["power_off"] is True
    bad = batch(2)
    del bad["rows"][1]["values"]["admin_ip"]
    r = c.post("/api/baremetal/profiles/rack-a/render", json=bad)
    assert r.status_code == 400 and ["row 2 {{admin_ip}}", "missing variable"] in r.get_json()["errors"]


def test_batch_starts_one_parent_action_and_keeps_secrets_out(env):
    c = env["client"]
    create(c)
    body = dict(batch(3), token=TOKEN, password=OSPW, bmc_password=BMCPW)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=body)
    assert r.status_code == 202, r.get_json()
    assert r.get_json() == {"action_id": "b1", "cluster": "rack-a", "nodes": 3}
    [(label, worker, args)] = env["started"]
    assert label == "baremetal-batch:rack-a" and worker is wapp._baremetal_batch_runner
    plan, concurrency = args
    assert concurrency == 2 and [p["mode"] for p in plan] == ["create", "join", "join"]
    assert all(p["token"] == TOKEN and p["password"] == OSPW and p["bmc_password"] == BMCPW for p in plan)
    for f in env["state"].rglob("*"):
        if f.is_file():
            text = f.read_text()
            assert TOKEN not in text and OSPW not in text and BMCPW not in text


def test_batch_refusals_happen_before_anything_starts(env):
    c = env["client"]
    create(c)
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=dict(batch(2), bmc_password=BMCPW))
    assert r.status_code == 400 and ["token", "missing"] in r.get_json()["errors"]
    r = c.post("/api/baremetal/profiles/rack-a/batch", json=dict(batch(2), token=TOKEN))
    assert ["row 1 bmc_password", "missing"] in r.get_json()["errors"]
    assert TOKEN not in r.get_data(as_text=True)
    assert env["started"] == []


def test_batch_takes_secrets_from_an_import(env):
    c = env["client"]
    create(c)
    import_id = wapp._bm_import_put({"token": TOKEN, "password": OSPW})
    r = c.post("/api/baremetal/profiles/rack-a/batch",
               json=dict(batch(1), import_id=import_id, bmc_password=BMCPW))
    assert r.status_code == 202
    assert env["started"][0][2][0][0]["token"] == TOKEN


def test_writes_are_admin_only(env, monkeypatch):
    c = env["client"]
    create(c)
    monkeypatch.setattr(wapp, "current_role", lambda: "operator")
    for m, path in (("post", "/api/baremetal/profiles"), ("put", "/api/baremetal/profiles/rack-a"),
                    ("delete", "/api/baremetal/profiles/rack-a"),
                    ("post", "/api/baremetal/profiles/rack-a/csv"),
                    ("post", "/api/baremetal/profiles/rack-a/render"),
                    ("post", "/api/baremetal/profiles/rack-a/batch")):
        assert getattr(c, m)(path, json={}).status_code == 403, path


def test_routes_are_authenticated_and_rate_limited():
    from limits import parse_many
    src = (WEB / "app.py").read_text()
    heads = src.split('@app.route("/api/baremetal/profiles')[1:]
    assert len(heads) == 9
    for head in heads:
        head = head.split("\ndef ", 1)[0]
        assert "@requires_auth" in head
        assert parse_many(re.search(r'@_rate_limit\("([^"]+)"\)', head).group(1))


# --- déroulé côté console ----------------------------------------------------

def test_batch_runner_waits_for_the_create_then_joins(monkeypatch):
    """Le runner lance chaque nœud par _bm_track (le même runner que la route
    d'installation) et ne lance les jonctions qu'une fois la création finie."""
    launched = []

    class Child:
        def __init__(self):
            self.status = "done"

    def fake_track(label, host, worker, opts):
        assert worker is wapp._baremetal_install_runner
        launched.append((label, opts["mode"]))
        aid = f"c{len(launched)}"
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS[aid] = Child()
        return aid, None

    prepared = []
    monkeypatch.setattr(wapp, "_bm_track", fake_track)
    monkeypatch.setattr(wapp, "_bm_prepare_install",
                        lambda d, defer_join=False: (prepared.append(d["hostname"]) or dict(d, cluster="rack-a"), None))
    run = wapp.ActionRun("p1", "baremetal-batch:rack-a", "(local)", [])
    plan = [{"hostname": f"n{i}", "bmc_host": f"h{i}", "mode": "create" if i == 0 else "join",
             "token": TOKEN} for i in range(3)]
    wapp._baremetal_batch_runner(run, plan, 2)
    assert run.status == "done" and launched[0] == ("baremetal-install:n0", "create")
    assert sorted(launched[1:]) == [("baremetal-install:n1", "join"), ("baremetal-install:n2", "join")]
    assert prepared and set(prepared) == {"n1", "n2"}          # jonctions recontrôlées au lancement
    assert [n["status"] for n in run.result["nodes"]] == ["done"] * 3
    assert plan == []                                          # secrets lâchés en fin de série
    assert TOKEN not in json.dumps(list(run.events)) + json.dumps(run.result)


# --- ligne de commande -------------------------------------------------------

def _cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hbm_cli", BIN / "harvester-baremetal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli_env(tmp_path):
    state = tmp_path / "state"
    bmp.save_profile(state, "rack-a", profile())
    nodes = tmp_path / "nodes.csv"
    nodes.write_text("bmc_host,bmc_user,hostname,ip,mgmt_mac,storage_ip,admin_ip\n"
                     "198.51.100.1,admin,node1,192.0.2.11,52:54:00:00:00:01,172.18.122.101,10.0.0.1\n"
                     "198.51.100.2,admin,node2,192.0.2.12,52:54:00:00:00:02,172.18.122.102,10.0.0.2\n")
    sec = tmp_path / "secrets.yaml"
    sec.write_text(f"token: {TOKEN}\nbmc_password: {BMCPW}\nbmc_passwords:\n  198.51.100.2: other\n")
    sec.chmod(0o600)
    return state, nodes, sec


def test_cli_apply_runs_the_console_batch(tmp_path, monkeypatch, capsys):
    state, nodes, sec = _cli_env(tmp_path)
    seen = {}

    def fake_start(prof, batch, secrets_, concurrency):
        seen.update(prof=prof["name"], rows=batch["rows"], secrets=secrets_, conc=concurrency)
        run = wapp.ActionRun("cli1", "baremetal-batch:rack-a", "(local)", [])
        run.emit({"type": "step", "step_id": "node-1", "status": "done", "message": "node1"})
        run.status = "done"
        run.result = {"nodes": [{"row": 1, "status": "done"}]}
        with wapp.ACTIONS_LOCK:
            wapp.ACTIONS["cli1"] = run
        return "cli1", None
    monkeypatch.setattr(wapp, "_bm_batch_start", fake_start)
    monkeypatch.setitem(sys.modules, "app", wapp)
    code = _cli().main(["profile", "--state-dir", str(state), "apply", "rack-a", "--nodes", str(nodes),
                        "--cluster-name", "rack-a", "--vip", "192.0.2.100", "--secrets-file", str(sec)])
    out = capsys.readouterr()
    assert code == 0 and json.loads(out.out)["status"] == "done"
    assert "STEP_EVENT|node-1|done|node1" in out.err
    assert seen["secrets"]["token"] == TOKEN and seen["conc"] == 2
    assert [r.get("bmc_password", "") for r in seen["rows"]] == ["", "other"]
    assert TOKEN not in out.out + out.err


def test_cli_refuses_open_secrets_and_passwords_in_csv(tmp_path):
    state, nodes, sec = _cli_env(tmp_path)
    cli = _cli()
    base = ["profile", "--state-dir", str(state), "apply", "rack-a", "--nodes", str(nodes),
            "--cluster-name", "rack-a", "--vip", "192.0.2.100"]
    sec.chmod(0o644)
    with pytest.raises(SystemExit) as e:
        cli.main(base + ["--secrets-file", str(sec)])
    assert "chmod 600" in str(e.value)
    sec.chmod(0o600)
    nodes.write_text("bmc_host,bmc_user,bmc_password,hostname\n1.2.3.4,admin,pw,n1\n")
    assert cli.main(base + ["--secrets-file", str(sec)]) == 2
    with pytest.raises(SystemExit):
        cli.main(base + ["--token", TOKEN])                     # pas d'option de secret en argv
    src = (BIN / "harvester-baremetal.py").read_text()
    assert 'add_argument("--token"' not in src and 'add_argument("--password"' not in src


def test_cli_list_and_show(tmp_path, capsys):
    state, _, _ = _cli_env(tmp_path)
    cli = _cli()
    assert cli.main(["profile", "--state-dir", str(state), "list"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "rack-a"
    assert cli.main(["profile", "--state-dir", str(state), "show", "rack-a"]) == 0
    assert "{{storage_ip}}" in capsys.readouterr().out
    assert cli.main(["profile", "--state-dir", str(state), "show", "nope"]) == 2
