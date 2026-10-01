#!/usr/bin/env python3
"""harvester-baremetal : opérations bare-metal de la console en ligne de
commande (1.78.0).

  harvester-baremetal discover --bmc 10.0.0.21 --user admin --password-file pw \\
                               --iso /srv/iso/harvester-v1.9.0-amd64.iso \\
                               [--advertise 10.0.0.5] [--port 8091] \\
                               [--extra-args "console=ttyS1,115200"]
  echo "$PW" | harvester-baremetal discover --bmc 10.0.0.21 --user admin --password-stdin --iso ...

`discover` : démarrage de découverte. L'ISO Harvester, remasterisée pour
cette découverte avec un script à elle (effacée à la fin), démarre une fois par le média
virtuel du BMC, renvoie ce que Linux voit (disques, liens stables, cartes
réseau) puis éteint la machine. L'inventaire est enregistré dans le magasin
de la console (même format, un fichier par numéro de série), où la fenêtre
d'installation le retrouve. Même déroulé que la console (bin/lib/bm_discover.py).

Le mot de passe du BMC ne passe jamais par la ligne de commande : fichier
privé (`--password-file`) ou entrée standard (`--password-stdin`).

`profile` (1.80.0) : profils d'installation multi-nœuds de la console.

  harvester-baremetal profile list
  harvester-baremetal profile show rack-a
  harvester-baremetal profile apply rack-a --nodes nodes.csv --cluster-name rack-a \
                      --vip 10.0.0.100 --secrets-file secrets.yaml

`apply` déroule la même série que la console (même code, chargé depuis
web/app.py) : la première ligne crée le cluster, les suivantes le
rejoignent, deux à la fois. Le CSV nomme ses colonnes en première ligne
(bmc_host, bmc_user, puis les variables) et ne porte aucun mot de passe.
Les secrets viennent d'un fichier privé (0600) ou de l'entrée standard, en
YAML : `token`, `password` (OS, facultatif), `bmc_password` (commun) et
`bmc_passwords` (par hôte de BMC). Jamais de la ligne de commande.
Étapes en `STEP_EVENT|étape|état|message` sur la sortie d'erreur, résultat
JSON sur la sortie standard. Codes : 0 fait, 1 échec, 2 usage, 3 annulé.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

import bm_discover  # noqa: E402
import bm_profiles  # noqa: E402
import cluster_decl  # noqa: E402


def _step(sid, status, msg=""):
    print(f"STEP_EVENT|{sid}|{status}|{msg}", file=sys.stderr, flush=True)


def _load_web_module(name):
    """Le serveur d'artefacts et l'analyseur vivent avec la console (web/)."""
    for d in (os.environ.get("HARVESTER_OPS_WEB_DIR"), HERE.parent / "web",
              "/opt/harvester-ops/web"):
        if d and (Path(d) / f"{name}.py").is_file():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            return __import__(name)
    return None


def _remaster(cmd, step):
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for line in proc.stderr:
        line = line.strip()
        if line.startswith("STEP_EVENT|"):
            parts = line.split("|", 3)
            if len(parts) == 4:
                step(parts[1], parts[2], parts[3])
    return proc.wait()


def _read_password(args):
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    path = Path(args.password_file)
    if path.stat().st_mode & 0o077:
        raise SystemExit("--password-file must not be readable by others (chmod 600)")
    return path.read_text().strip()


def _local_ip_for(host):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 443))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def cmd_discover(args):
    pxe = _load_web_module("pxe_server")
    if pxe is None:
        print("pxe_server.py not found (set HARVESTER_OPS_WEB_DIR)", file=sys.stderr)
        return 1
    try:
        extra = bm_discover.clean_extra_args(args.extra_args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    src = Path(args.iso).resolve()
    if not src.is_file():
        print(f"ISO not found: {src}", file=sys.stderr)
        return 2
    password = _read_password(args)
    share = Path.home() / ".local/share/harvester-ops"
    # l'ISO de la découverte (7 Go) y est écrite puis effacée
    work_dir = Path(args.work_dir or (src.parent / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o700)
    store_dir = Path(args.store or os.environ.get("HARVESTER_OPS_INVENTORY_DIR")
                     or share / "inventory")

    cancelled = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: cancelled.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *a: cancelled.update(flag=True))

    started = False
    try:
        try:
            port = pxe.start(port=args.port)
            started = True
        except OSError as e:
            print(f"cannot listen on port {args.port} ({e.strerror or e}); "
                  "is the console running here? choose another --port", file=sys.stderr)
            return 1
        res = bm_discover.run(
            {"host": args.bmc, "src_iso": src,
             "work_dir": work_dir, "store_dir": store_dir,
             "remaster_script": HERE / "harvester-iso-remaster.sh",
             "advertise": args.advertise or _local_ip_for(args.bmc),
             "port": port, "extra_args": extra},
            bm_discover.RedfishBmc(args.bmc, args.user, password),
            pxe, _remaster, _step, cancelled=lambda: cancelled["flag"])
    except bm_discover.DiscoveryError as e:
        _step(e.step, "error", str(e)[:300])
        return 3 if isinstance(e, bm_discover.Cancelled) else 1
    finally:
        if started:
            pxe.stop()
    out = {"system_serial": res["serial"], "stored": str(res["path"]),
           "forced_off": res["forced_off"], "binding": res["binding"]}
    parser = _load_web_module("baremetal_disks")
    if parser is not None:
        inv = parser.parse_discovery(res["raw"])
        out.update(disks=inv["disks"], nics=inv["nics"])
    print(json.dumps(out, indent=2))
    return 0


# --- profils (1.80.0) --------------------------------------------------------

def _profiles_state(args):
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    return cluster_decl.state_dir()


def _read_secrets(args):
    """Secrets d'une série : fichier privé ou entrée standard, en YAML."""
    import yaml
    if args.secrets_stdin:
        text = sys.stdin.read()
    else:
        path = Path(args.secrets_file)
        if path.stat().st_mode & 0o077:
            raise SystemExit("--secrets-file must not be readable by others (chmod 600)")
        text = path.read_text()
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise SystemExit("secrets: invalid YAML") from None
    if not isinstance(doc, dict):
        raise SystemExit("secrets: expected a mapping (token, password, bmc_password, bmc_passwords)")
    unknown = set(doc) - {"token", "password", "bmc_password", "bmc_passwords"}
    if unknown:
        raise SystemExit(f"secrets: unknown keys {sorted(unknown)}")
    return doc


def cmd_profile(args):
    state = _profiles_state(args)
    if args.pcmd == "list":
        print(json.dumps(bm_profiles.list_profiles(state), indent=2))
        return 0
    prof = bm_profiles.load_profile(state, args.name)
    if prof is None:
        print(f"no such profile: {args.name} (in {state / bm_profiles.PROFILE_DIR})", file=sys.stderr)
        return 2
    if args.pcmd == "show":
        import yaml
        print(yaml.safe_dump(prof, sort_keys=False, allow_unicode=True), end="")
        return 0
    try:
        rows = bm_profiles.parse_nodes_csv(Path(args.nodes).read_text(), prof["variables"])
    except bm_profiles.ProfileError as e:
        for where, why in e.errors:
            print(f"{where}: {why}", file=sys.stderr)
        return 2
    if any("bmc_password" in r for r in rows):
        print("the nodes CSV must not hold passwords: use bmc_passwords in the secrets", file=sys.stderr)
        return 2
    sec = _read_secrets(args)
    per_host = sec.get("bmc_passwords") or {}
    for r in rows:
        if per_host.get(r.get("bmc_host")):
            r["bmc_password"] = str(per_host[r["bmc_host"]])
    if args.port:
        os.environ["HARVESTER_OPS_PXE_PORT"] = str(args.port)
    # le cluster créé est déclaré dans le même répertoire d'état
    os.environ.setdefault("HARVESTER_OPS_STATE_DIR", str(state))
    app = _load_web_module("app")
    if app is None:
        print("web/app.py not found (set HARVESTER_OPS_WEB_DIR)", file=sys.stderr)
        return 1
    batch, secrets_ = app._bm_batch_input({
        "cluster_name": args.cluster_name, "vip": args.vip, "iso": args.iso, "rows": rows,
        "token": sec.get("token"), "password": sec.get("password"),
        "bmc_password": sec.get("bmc_password")})
    action_id, errors = app._bm_batch_start(prof, batch, secrets_, args.concurrency)
    if errors:
        for where, why in errors:
            print(f"{where}: {why}", file=sys.stderr)
        return 2
    with app.ACTIONS_LOCK:
        run = app.ACTIONS[action_id]
    stop = lambda *a: setattr(run, "_cancel", True)      # noqa: E731
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    seq = 0
    while True:
        events, seq = run.events_since(seq)
        for ev in events:
            if ev.get("type") == "step":
                _step(ev.get("step_id"), ev.get("status"), ev.get("message", ""))
        if run.status in ("done", "error", "cancelled") and not run.events_since(seq)[0]:
            break
        time.sleep(1)
    print(json.dumps({"action_id": action_id, "status": run.status,
                      "nodes": run.result.get("nodes", [])}, indent=2))
    return {"done": 0, "cancelled": 3}.get(run.status, 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-baremetal", description=__doc__, allow_abbrev=False,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", allow_abbrev=False, help="discovery boot: read the disks and NICs Linux sees")
    d.add_argument("--bmc", required=True, help="BMC address (Redfish)")
    d.add_argument("--user", required=True, help="BMC user")
    pw = d.add_mutually_exclusive_group(required=True)
    pw.add_argument("--password-file", help="file holding the BMC password (mode 0600)")
    pw.add_argument("--password-stdin", action="store_true",
                    help="read the BMC password from the first line of stdin")
    d.add_argument("--iso", required=True, help="official Harvester ISO")
    d.add_argument("--advertise", help="address the BMC and the machine reach this host at")
    d.add_argument("--port", type=int, default=int(os.environ.get("HARVESTER_OPS_PXE_PORT", 8091)),
                   help="artifact server port (default 8091)")
    d.add_argument("--extra-args", default="", help="extra kernel arguments (console=...)")
    d.add_argument("--work-dir", help="scratch directory for the discovery ISO "
                   "(default: <ISO dir>/work, needs the ISO size free)")
    d.add_argument("--store", help="inventory store (default: the console's)")
    p = sub.add_parser("profile", allow_abbrev=False, help="multi-node install profiles")
    p.add_argument("--state-dir", help="console state directory (default: HARVESTER_OPS_STATE_DIR "
                   "or /var/lib/harvester-ops)")
    psub = p.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("list", help="list the profiles")
    ps = psub.add_parser("show", help="print a profile")
    ps.add_argument("name")
    pa = psub.add_parser("apply", allow_abbrev=False, help="install a batch of nodes from a profile")
    pa.add_argument("name")
    pa.add_argument("--nodes", required=True, help="nodes CSV (header: bmc_host, bmc_user, variables)")
    pa.add_argument("--cluster-name", required=True, help="name of the cluster the first node creates")
    pa.add_argument("--vip", required=True, help="cluster VIP; the other nodes join https://<vip>:443")
    pa.add_argument("--iso", help="ISO of the console store (default: the profile's)")
    pa.add_argument("--concurrency", type=int, default=bm_profiles.DEFAULT_CONCURRENCY,
                    help="joins running at once (default 2, at most 4)")
    pa.add_argument("--port", type=int, help="artifact server port (default 8091)")
    sg = pa.add_mutually_exclusive_group(required=True)
    sg.add_argument("--secrets-file", help="YAML file (mode 0600): token, password, bmc_password, bmc_passwords")
    sg.add_argument("--secrets-stdin", action="store_true", help="read the same YAML from stdin")
    args = ap.parse_args(argv)
    if args.cmd == "discover":
        return cmd_discover(args)
    if args.cmd == "profile":
        return cmd_profile(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
