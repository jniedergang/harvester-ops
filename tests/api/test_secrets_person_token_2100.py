"""2.10.0 tâches 6 et 7 : `harvester-secrets person-token` (assistant,
vérifications, activation, désactivation, groupes, clés de Rancher).

Faux OpenBao (méthode jwt du faux serveur, politique d'une personne = texte
exact de `openbao_policy.person_rules`), documents publics de Rancher simulés
(découverte, JWKS), agent joué dans le processus en B1 (même harnais que la
2.9.0). Aucune valeur : ni jeton d'identité, ni jeton OpenBao de personne
dans stdout, stderr ni les réglages.

Mutations jouées (rapport des tâches 6 et 7) : « politique `hops-person`
avec `kubeconfig/*` », « vérification 2 (connexion réelle) sautée »,
« activation malgré une vérification rouge », « clés de rotation remplacées
au lieu d'être ajoutées ».
"""

import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_harvester_secrets_cli_290 import CID, PREFIX, cli, creds, env, hsc, read_settings, settings  # noqa: E402,F401

import openbao_jwt as oj  # noqa: E402
import openbao_policy as pol  # noqa: E402
from fake_openbao import fake_jwt  # noqa: E402

ISS = "https://rancher.example/oidc"
CLIENT = "client-7zjw5jtpx6"
MOUNT = f"jwt-hops-{CID}-rmgr"
KV = "harvester-ops"


def _b64(i):
    return base64.urlsafe_b64encode(i.to_bytes((i.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


N1, N2 = (1 << 2047) + 12345, (1 << 2047) + 67891
JWKS1 = {"keys": [{"kty": "RSA", "kid": "key", "use": "sig", "n": _b64(N1), "e": "AQAB"}]}
JWKS2 = {"keys": JWKS1["keys"] + [{"kty": "RSA", "kid": "key2", "use": "sig", "n": _b64(N2), "e": "AQAB"}]}
JWKS3 = {"keys": [JWKS2["keys"][1]]}


@pytest.fixture
def rancher(monkeypatch):
    """Documents publics de Rancher (découverte, JWKS), réglables."""
    r = {"discovery": {"issuer": ISS, "jwks_uri": ISS + "/.well-known/jwks.json",
                       "id_token_signing_alg_values_supported": ["RS256"]},
         "jwks": JWKS1, "down": False, "calls": []}

    def fetch(url, ca_pem=None, timeout=10, opener=None):
        r["calls"].append((url, ca_pem))
        if r["down"]:
            raise ValueError(f"{url}: URLError")
        return r["discovery"] if url.endswith("openid-configuration") else r["jwks"]
    monkeypatch.setattr(hsc.oj, "fetch_json", fetch)
    return r


def id_token(sub="user-kk67j", **kw):
    now = int(time.time())
    c = {"iss": ISS, "aud": [CLIENT], "sub": sub, "exp": now + 600, "iat": now, "nonce": "n0",
         "name": "Default Admin", "auth_provider": "local"}
    c.update(kw)
    return fake_jwt(c)


def person_mount(env, rules=None, cidr_ok=True):
    """Méthode jwt du faux serveur, la politique d'une personne étant le
    texte exact de person_rules (préfixes de ses chemins)."""
    rules = rules if rules is not None else pol.person_rules(KV, PREFIX, CID)
    return env["srv"].add_jwt_mount(MOUNT, issuer=ISS, audience=CLIENT, cidr_ok=cidr_ok,
                                    policies=("default", pol.person_policy_name(CID)),
                                    allow=[(p.rstrip("*"), set(c)) for p, c in rules])


def mode_a(env, **extra):
    creds(env)
    return settings(env, migrated={}, **extra)


def assistant(env, capsys, *more, keys="discovery"):
    return cli(["person-token", "assistant", "--rancher", "rmgr", "--issuer", ISS, "--client-id", CLIENT,
                "--keys", keys, "--console-cidr", "127.0.0.1/32", *more], capsys=capsys)


def check(capsys, what="check", token=None):
    return cli(["person-token", what, "--rancher", "rmgr", "--id-token-stdin"],
               stdin=(token or id_token()) + "\n", capsys=capsys)


def no_value(env, *texts):
    blob = "".join(texts) + json.dumps(read_settings(env))
    for t in list(env["srv"].tokens) + [j for _m, j in env["srv"].jwt_logins]:
        assert t not in blob


# ---------------------------------------------------------------------------
# Assistant
# ---------------------------------------------------------------------------
def test_assistant_mode_a_renders_commands_and_records_pending(env, capsys, rancher):
    mode_a(env)
    rc, doc, err = assistant(env, capsys)
    assert rc == 0, doc
    assert doc["applied"] is False and doc["mount"] == MOUNT
    text = "\n".join(doc["commands"])
    assert f"bao auth enable -path={MOUNT} jwt" in text
    assert pol.person_policy(KV, PREFIX, CID) in text
    assert json.dumps(oj.person_role_params(CLIENT, "127.0.0.1/32", CID), indent=2, sort_keys=True) in text
    pt = read_settings(env)["person_token"]
    assert pt["pending"]["rmgr"]["mount"] == MOUNT and pt["enabled"] is False and not pt["servers"]
    assert pt["pending"]["rmgr"]["keys_sha256"] == oj.keys_sha256(JWKS1)


def test_assistant_static_keys_carry_the_jwks_as_pem(env, capsys, rancher):
    mode_a(env)
    rc, doc, _ = assistant(env, capsys, keys="static")
    assert rc == 0, doc
    text = "\n".join(doc["commands"])
    assert "jwt_validation_pubkeys" in text and "oidc_discovery_url" not in text
    assert read_settings(env)["person_token"]["pending"]["rmgr"]["pubkeys"] == oj.pubkeys_of(JWKS1)


def test_assistant_static_keys_unreadable_fails(env, capsys, rancher):
    mode_a(env)
    rancher["down"] = True
    rc, doc, _ = assistant(env, capsys, keys="static")
    assert rc == 1 and doc["code"] == "openbao-jwt-keys-unreadable"
    assert "person_token" not in read_settings(env) or not read_settings(env)["person_token"]["pending"]


@pytest.mark.parametrize("args,code", [
    (["--rancher", "../x"], "bad-input"),
    (["--issuer", "https://rancher.example"], "bad-input"),
    (["--client-id", "a b"], "bad-input"),
    (["--console-cidr", "nope"], "bad-input"),
])
def test_assistant_bad_input(env, capsys, rancher, args, code):
    mode_a(env)
    base = {"--rancher": "rmgr", "--issuer": ISS, "--client-id": CLIENT, "--console-cidr": "127.0.0.1/32"}
    base.update(dict(zip(args[::2], args[1::2])))
    rc, doc, _ = cli(["person-token", "assistant", *[x for kv in base.items() for x in kv]], capsys=capsys)
    assert rc == 2 and doc["code"] == code


def test_assistant_needs_openbao_active(env, capsys, rancher):
    settings(env, mode=None)
    rc, doc, _ = assistant(env, capsys)
    assert rc == 2 and doc["code"] == "openbao-inactive"


def test_assistant_b1_asks_the_agent(env, capsys, rancher, monkeypatch):
    creds(env)
    settings(env, mode="b1")
    sent = []
    monkeypatch.setattr(hsc, "run_agent", lambda req, timeout=None: sent.append(req) or {"state": "done"})
    rc, doc, _ = assistant(env, capsys)
    assert rc == 0 and doc["applied"] is True and doc["commands"] is None
    assert sent == [{"op": "b1-jwt", "rancher": "rmgr", "client_id": CLIENT, "issuer": ISS, "groups_only": False}]
    assert read_settings(env)["person_token"]["pending"]["rmgr"]["console_cidr"] == pol.B1_CIDR


# ---------------------------------------------------------------------------
# Vérifications avant activation
# ---------------------------------------------------------------------------
def test_checks_all_green_then_enable(env, capsys, rancher):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    rc, doc, err = check(capsys)
    assert rc == 0, doc
    assert [c["id"] for c in doc["checks"]] == ["discovery", "login", "token", "capabilities", "revoke"]
    assert all(c["ok"] for c in doc["checks"])
    assert not read_settings(env)["person_token"]["enabled"]           # check n'active rien
    rc, doc, err2 = check(capsys, "enable")
    assert rc == 0 and doc["enabled"] is True
    assert env["dropped"]                           # lancé en root : droits quittés avant l'état
    pt = read_settings(env)["person_token"]
    assert pt["enabled"] and pt["servers"]["rmgr"]["checked_at"] and not pt["pending"]
    assert not [t for t, i in env["srv"].tokens.items() if i.get("kind") == "person"]     # jetons révoqués
    no_value(env, json.dumps(doc), err, err2)


def _fail(case, env, rancher):
    if case == "discovery-issuer":
        rancher["discovery"]["issuer"] = "https://other/oidc"
    elif case == "discovery-alg":
        rancher["discovery"]["id_token_signing_alg_values_supported"] = ["ES256"]
    elif case == "login-audience":
        env["srv"].jwt[MOUNT]["audience"] = "other-client"
    elif case == "login-cidr":
        env["srv"].jwt[MOUNT]["cidr_ok"] = False
    elif case == "token-policies":
        env["srv"].jwt[MOUNT]["policies"] = ["default", pol.person_policy_name(CID), "evil"]
    elif case == "token-ttl":
        env["srv"].jwt[MOUNT]["ttl"] = 3600
    elif case == "capabilities-kubeconfig":
        env["srv"].jwt[MOUNT]["allow"].append((f"{KV}/data/{PREFIX}/kubeconfig/", {"read"}))
    elif case == "capabilities-eso-read":
        env["srv"].jwt[MOUNT]["allow"].append((f"{KV}/data/{PREFIX}/eso/", {"read"}))
    elif case == "capabilities-other-console":
        env["srv"].jwt[MOUNT]["allow"].append((f"{KV}/data/consoles/000000000000/", {"read"}))
    elif case == "capabilities-missing":
        env["srv"].jwt[MOUNT]["allow"] = [a for a in env["srv"].jwt[MOUNT]["allow"] if "ssh-kvm" not in a[0]]


@pytest.mark.parametrize("case,red", [
    ("discovery-issuer", "discovery"), ("discovery-alg", "discovery"),
    ("login-audience", "login"), ("login-cidr", "login"),
    ("token-policies", "token"), ("token-ttl", "token"),
    ("capabilities-kubeconfig", "capabilities"), ("capabilities-eso-read", "capabilities"),
    ("capabilities-other-console", "capabilities"), ("capabilities-missing", "capabilities"),
])
def test_each_red_check_blocks_enable(env, capsys, rancher, case, red):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    _fail(case, env, rancher)
    rc, doc, _ = check(capsys, "enable")
    assert rc == 1 and doc["code"] == "person-token-checks-failed"
    assert next(c for c in doc["checks"] if c["id"] == red)["ok"] is False
    pt = read_settings(env)["person_token"]
    assert pt["enabled"] is False and not pt["servers"] and "rmgr" in pt["pending"]


def test_revoke_check_sees_a_token_still_accepted(env, capsys, rancher, monkeypatch):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    srv = env["srv"]
    real = srv._route

    def keep(h, rec, path, body, raw):
        if path == "auth/token/revoke-self":
            return h._send(204)                       # jeton gardé malgré la révocation
        return real(h, rec, path, body, raw)
    monkeypatch.setattr(srv, "_route", keep)
    rc, doc, _ = check(capsys)
    assert rc == 1 and next(c for c in doc["checks"] if c["id"] == "revoke")["ok"] is False


@pytest.mark.parametrize("how,code", [("none", "openbao-person-no-token"), ("access", "bad-input"),
                                      ("garbage", "bad-input")])
def test_checks_refuse_without_an_identity_token(env, capsys, rancher, how, code):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    stdin = {"none": "", "access": id_token(token="token-abc:rancher", scope=["openid"]), "garbage": "x"}[how]
    rc, doc, _ = cli(["person-token", "check", "--rancher", "rmgr", "--id-token-stdin"], stdin=stdin + "\n",
                     capsys=capsys)
    assert rc == 2 and doc["code"] == code
    assert not env["srv"].jwt_logins                     # rien n'est parti vers OpenBao


def test_identity_token_by_spec_file(env, capsys, rancher, tmp_path):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"id_token": id_token()}))
    spec.chmod(0o600)
    rc, doc, _ = cli(["person-token", "check", "--rancher", "rmgr", "--spec", str(spec)], capsys=capsys)
    assert rc == 0, doc


def test_check_unconfigured_rancher(env, capsys, rancher):
    mode_a(env)
    rc, doc, _ = check(capsys)
    assert rc == 2 and doc["code"] == "person-token-unconfigured"


# ---------------------------------------------------------------------------
# Désactivation, groupes
# ---------------------------------------------------------------------------
def _enabled(env, capsys, rancher):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys)[0] == 0
    assert check(capsys, "enable")[0] == 0


def test_disable_global_then_per_rancher_with_remove(env, capsys, rancher):
    _enabled(env, capsys, rancher)
    rc, doc, _ = cli(["person-token", "disable"], capsys=capsys)
    assert rc == 0 and read_settings(env)["person_token"]["enabled"] is False
    assert "rmgr" in read_settings(env)["person_token"]["servers"]
    rc, doc, _ = cli(["person-token", "disable", "--rancher", "rmgr", "--remove"], capsys=capsys)
    assert rc == 0 and "rmgr" not in read_settings(env)["person_token"]["servers"]
    assert doc["commands"][-2:] == [f"bao auth disable {MOUNT}", f"bao policy delete {pol.person_policy_name(CID)}"]
    rc, doc, _ = cli(["person-token", "disable", "--remove"], capsys=capsys)
    assert rc == 2


def test_groups_mode_a(env, capsys, rancher):
    mode_a(env)
    uid = "11111111-2222-3333-4444-555555555555"
    rc, doc, _ = cli(["person-token", "groups", "set", "ops", "--class", "ssh-kvm", "--cluster", "node2",
                      "--member", uid], capsys=capsys)
    assert rc == 0, doc
    text = "\n".join(doc["commands"])
    assert f"identity/group/name/hops-{CID}-ops" in text and uid in text
    assert pol.group_policy(KV, PREFIX, CID, "ops", ["ssh-kvm"], ["node2"]) in text
    assert read_settings(env)["person_token"]["groups"]["ops"]["members"] == [uid]
    rc, doc, _ = cli(["person-token", "groups", "only", "on"], capsys=capsys)
    assert rc == 0 and pol.person_policy(KV, PREFIX, CID, True) in "\n".join(doc["commands"])
    rc, doc, _ = cli(["person-token", "groups", "remove", "ops"], capsys=capsys)
    assert rc == 0 and "ops" not in read_settings(env)["person_token"]["groups"]


@pytest.mark.parametrize("args", [["set", "ops", "--class", "rancher-oidc", "--cluster", "a"],
                                  ["set", "ops", "--class", "ssh-kvm", "--cluster", "*"],
                                  ["set", "Bad/Name", "--class", "ssh-kvm", "--cluster", "a"],
                                  ["set", "ops", "--class", "ssh-kvm", "--cluster", "a", "--member", "x"],
                                  ["only", "maybe"]])
def test_groups_bad_input(env, capsys, rancher, args):
    mode_a(env)
    rc, doc, _ = cli(["person-token", "groups", *args], capsys=capsys)
    assert rc == 2 and doc["code"] == "bad-input"


# ---------------------------------------------------------------------------
# Tâche 7 : clés statiques de Rancher
# ---------------------------------------------------------------------------
def _static(env, capsys, rancher):
    mode_a(env)
    person_mount(env)
    assert assistant(env, capsys, keys="static")[0] == 0
    assert check(capsys, "enable")[0] == 0


def test_keys_discovery_has_nothing_to_do(env, capsys, rancher):
    _enabled(env, capsys, rancher)
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr"], capsys=capsys)
    assert rc == 0 and doc["keys"] == "discovery" and doc["changed"] is False


def test_keys_rotation_old_and_new_together_then_confirm(env, capsys, rancher):
    _static(env, capsys, rancher)
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr"], capsys=capsys)
    assert rc == 0 and doc["changed"] is False
    rancher["jwks"] = JWKS2                               # Rancher ajoute la nouvelle clé publique
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr"], capsys=capsys)
    assert doc["changed"] is True and doc["code"] == "openbao-jwt-keys-changed"
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr", "--refresh"], capsys=capsys)
    assert rc == 0 and doc["count"] == 2
    e = read_settings(env)["person_token"]["servers"]["rmgr"]
    assert e["keys_sha256"] == oj.keys_sha256(JWKS2) and len(e["pubkeys"]) == 2
    rancher["jwks"] = JWKS3                               # l'ancienne clé retirée chez Rancher
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr", "--refresh"], capsys=capsys)
    assert doc["count"] == 2 and doc["pending_confirm"] is True      # ancienne gardée jusqu'à confirmation
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr", "--confirm"], capsys=capsys)
    assert rc == 0 and doc["count"] == 1 and doc["pending_confirm"] is False
    assert read_settings(env)["person_token"]["servers"]["rmgr"]["pubkeys"] == oj.pubkeys_of(JWKS3)
    assert "jwt_validation_pubkeys" in "\n".join(doc["commands"])


def test_keys_unreadable_jwks(env, capsys, rancher):
    _static(env, capsys, rancher)
    rancher["down"] = True
    rc, doc, _ = cli(["person-token", "keys", "--rancher", "rmgr"], capsys=capsys)
    assert rc == 1 and doc["code"] == "openbao-jwt-keys-unreadable"


# ---------------------------------------------------------------------------
# Tâche 7 : surveillance horaire par la console (lecture seule)
# ---------------------------------------------------------------------------
@pytest.fixture
def watch(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "web"))
    import app as wapp
    doc = {"mode": "a", "addr": "https://bao.example", "prefix": PREFIX, "person_token": {
        "enabled": True, "servers": {
            "rmgr": {"mount": MOUNT, "issuer": ISS, "keys": "static", "keys_sha256": oj.keys_sha256(JWKS1)},
            "lab": {"mount": f"jwt-hops-{CID}-lab", "issuer": "https://lab.example/oidc", "keys": "discovery"}}}}
    monkeypatch.setattr(wapp, "_secrets_settings", lambda: doc)
    monkeypatch.setattr(wapp, "_PERSON_KEYS", {"at": 0.0, "seen": {}})
    return wapp, doc


def test_console_watch_flags_a_changed_static_key_hourly(watch):
    wapp, doc = watch
    calls = []
    jwks = {"v": JWKS1}

    def fetch(url, ca_pem=None, **kw):
        calls.append(url)
        return jwks["v"]
    assert wapp._person_keys_tick(now=10_000, fetch=fetch) == {"rmgr": oj.keys_sha256(JWKS1)}
    assert calls == [ISS + "/.well-known/jwks.json"]                  # discovery : jamais relu
    assert wapp._person_keys_changed(doc) == [] and not wapp._secrets_banner()["jwt_keys_changed"]
    jwks["v"] = JWKS2
    wapp._person_keys_tick(now=10_000 + 60, fetch=fetch)              # moins d'une heure : rien
    assert len(calls) == 1
    wapp._person_keys_tick(now=10_000 + 3600, fetch=fetch)
    assert wapp._person_keys_changed(doc) == ["rmgr"]
    assert wapp._secrets_banner()["jwt_keys_changed"] is True
    w = [x for x in wapp._secrets_warnings(doc, {}, {}) if x["code"] == "openbao-jwt-keys-changed"]
    assert w and w[0]["servers"] == ["rmgr"]


def test_console_watch_survives_an_unreachable_rancher(watch):
    wapp, doc = watch

    def down(url, ca_pem=None, **kw):
        raise ValueError(f"{url}: URLError")
    assert wapp._person_keys_tick(now=10_000, fetch=down) == {}
    assert wapp._person_keys_changed(doc) == []
