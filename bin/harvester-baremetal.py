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
Étapes en `STEP_EVENT|étape|état|message` sur la sortie d'erreur, résultat
JSON sur la sortie standard. Codes : 0 fait, 1 échec, 2 usage, 3 annulé.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

import bm_discover  # noqa: E402


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
    args = ap.parse_args(argv)
    if args.cmd == "discover":
        return cmd_discover(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
