"""v2.6.0 : la configuration du registre d'amorçage contre la vraie image
(Distribution 3.1 de SUSE), en conteneurs podman sans privilège sur des
ports locaux tirés au hasard, retirés après l'essai.

Essai d'intégration, lancé seulement avec HARVESTER_OPS_PODMAN_IT=1 (il
tire l'image de registry.suse.com) : deux instances sur le même volume,
comme le pod (point 197) ; `pull` lit sur l'instance de lecture et n'écrit
nulle part ; `push` écrit sur l'instance d'écriture ; ce qui y est poussé
se lit aussitôt sur l'autre ; un accès anonyme est refusé."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bin" / "lib"))
import oci_push as op  # noqa: E402
import registry_seed as rs  # noqa: E402

IMAGE = "registry.suse.com/suse/registry@sha256:de84c976e7934762d3c2cee87d56567889d19bbddccf5dbdaeb1f910cff755a1"
# le vrai podman de l'hôte (tests/conftest.py en masque le nom pour tous les autres tests)
PODMAN = next((p for p in ("/usr/bin/podman", "/usr/local/bin/podman") if os.path.exists(p)), None)
pytestmark = pytest.mark.skipif(os.environ.get("HARVESTER_OPS_PODMAN_IT") != "1" or not PODMAN,
                                reason="integration test: HARVESTER_OPS_PODMAN_IT=1 and podman")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bcrypt_line(user, password):
    bcrypt = pytest.importorskip("bcrypt")
    return f"{user}:{bcrypt.hashpw(password.encode(), bcrypt.gensalt(5)).decode()}\n"


@pytest.fixture(scope="module")
def registry(tmp_path_factory):
    d = tmp_path_factory.mktemp("reg")
    tls = rs.make_tls(d, "127.0.0.1", "localhost", "it")
    pw = {"push": uuid.uuid4().hex, "pull": uuid.uuid4().hex}
    push = bcrypt_line("push", pw["push"])
    files = {"read.yml": json.dumps(rs.distribution_config(True)), "write.yml": json.dumps(rs.distribution_config(False)),
             "tls.crt": tls["crt"], "tls.key": tls["key"], "read": push + bcrypt_line("pull", pw["pull"]),
             "write": push}
    for n, text in files.items():
        (d / n).write_text(text)
        (d / n).chmod(0o644)
    tag = uuid.uuid4().hex[:8]
    vol, names = f"hops-it-{tag}", []
    ports = {"read": free_port(), "write": free_port()}
    p = subprocess.run([PODMAN, "pull", "-q", IMAGE], capture_output=True, text=True, timeout=300)
    assert p.returncode == 0, p.stderr[-500:]
    subprocess.run([PODMAN, "volume", "create", vol], check=True, capture_output=True)
    try:
        # écriture d'abord (elle prépare le volume), lecture ensuite, volume en
        # lecture seule comme dans le pod (revue m-7)
        for role, target in (("write", rs.WRITE_TARGET), ("read", rs.READ_TARGET)):
            name = f"hops-it-{role}-{tag}"
            names.append(name)
            subprocess.run([PODMAN, "run", "-d", "--name", name, "--user", f"{rs.UID}:{rs.UID}",
                            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "--memory", "512m", "-p", f"127.0.0.1:{ports[role]}:{target}",
                            "-v", f"{vol}:{rs.DATA}:" + ("U" if role == "write" else "ro"),
                            "-v", f"{d / (role + '.yml')}:/etc/registry/conf/{role}.yml:ro,Z",
                            "-v", f"{d / 'tls.crt'}:/etc/registry/tls/tls.crt:ro,Z",
                            "-v", f"{d / 'tls.key'}:/etc/registry/tls/tls.key:ro,Z",
                            "-v", f"{d / role}:/etc/registry/auth/htpasswd:ro,Z",
                            IMAGE, "serve", f"/etc/registry/conf/{role}.yml"], check=True, capture_output=True)
        for p in ports.values():
            for _ in range(60):
                try:
                    socket.create_connection(("127.0.0.1", p), timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.5)
        yield {"ca": tls["ca"], "pw": pw, "ports": ports, "names": names}
    finally:
        for n in names:
            subprocess.run([PODMAN, "rm", "-f", n], capture_output=True)
        subprocess.run([PODMAN, "volume", "rm", "-f", vol], capture_output=True)


def client(r, role, user):
    creds = {"username": user, "password": r["pw"][user]} if user else None
    return op.Registry(f"127.0.0.1:{r['ports'][role]}", creds, ca_pem=r["ca"], timeout=20)


def push_blob(c, data=b"hello harvester-ops"):
    import hashlib
    d = "sha256:" + hashlib.sha256(data).hexdigest()
    return c.push_blob("it/x", d, data), d


def test_anonymous_access_is_refused_on_both_instances(registry):
    for role in ("read", "write"):
        with pytest.raises(op.RegistryError, match="authentication required"):
            client(registry, role, None).request("GET", "/v2/", "")


def test_the_read_account_reads_but_never_writes(registry):
    code, _, _ = client(registry, "read", "pull").request("GET", "/v2/", "")
    assert code == 200
    with pytest.raises(op.RegistryError, match="HTTP 405"):
        push_blob(client(registry, "read", "pull"))
    with pytest.raises(op.RegistryError, match="authentication refused"):
        client(registry, "write", "pull").request("GET", "/v2/", "")


def test_what_push_writes_is_read_at_once_on_the_read_instance(registry):
    pushed, digest = push_blob(client(registry, "write", "push"))
    assert pushed is True
    assert client(registry, "read", "pull").blob("it/x", digest) == b"hello harvester-ops"
    with pytest.raises(op.RegistryError, match="HTTP 405"):
        push_blob(client(registry, "read", "push"), b"other")
