#!/usr/bin/env python3
"""harvester-bookings : les réservations de GPU de la console (v1.94.0).

Même registre que l'onglet Shared resources > Bookings (bookings.db de la
console, bin/lib/bookings_db.py) et mêmes règles (bin/lib/hv_bookings.py).
Une réservation retient un créneau dans la console ; elle n'est pas encore
appliquée sur le cluster : rien n'y est écrit.

  harvester-bookings.py list   [--db FILE] [--cluster NAME] [--mine USER] [--state S] [--json]
  harvester-bookings.py check  (--spec FILE | --cluster NAME --type T (--unit ID | --model M [--count N])
        --start ISO --end ISO [--holder USER | --group NAME] [--target-vm NS/NAME | --target-workload NS/NAME]
        [--note TEXT]) [--inventory FILE | --kubeconfig FILE] [--db FILE]
  harvester-bookings.py create ... (comme check) [--as USER] [--id bk-...]
  harvester-bookings.py cancel --id bk-... [--as USER] [--role operator|admin] [--db FILE]
  harvester-bookings.py usage  [--from ISO] [--to ISO] [--tz MINUTES] [--db FILE] [--usage-db FILE] [--json | --csv]
  harvester-bookings.py quota  list [--db FILE] [--json]
  harvester-bookings.py quota  set (--holder USER | --group NAME) [--monthly-hours H] [--max-units N] [--as USER] [--db FILE]
  harvester-bookings.py quota  delete (--holder USER | --group NAME) [--as USER] [--db FILE]

La capacité vient de l'inventaire GPU du cluster : la console le lit avec
l'identité de la personne et le passe dans la demande (`inventory` du
fichier --spec) ; sinon --inventory (la sortie JSON de harvester-gpus.py
list), sinon le cluster est lu ici (--kubeconfig, ou le cluster déclaré du
même nom). Le contrôle des conflits est refait dans la transaction
d'écriture.

Un opérateur annule les réservations qu'il a créées ou dont il est le
titulaire ; un admin (--role admin) celles de tout le monde.

v1.95.0 : `usage` donne, par titulaire, les GPU-heures réservées et
consommées sur une période (le mois en cours par défaut), le gaspillage
(réservé et pas utilisé) et le hors règle (utilisé sans réservation),
d'après bookings.db et les relevés de l'échantillonneur de la console
(usage.db). `quota` règle, par personne ou groupe, un plafond de GPU-heures
réservées par mois et un nombre maximal d'unités réservées en même temps ;
`create` refuse une réservation qui dépasserait le quota de son titulaire.

Codes de sortie : 0 fait (ou pas de conflit), 1 conflit ou refus,
2 erreur d'usage ou demande invalide.
"""

import argparse
import getpass
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import bookings_db as bdb  # noqa: E402
import hv_bookings as hb  # noqa: E402
import hv_usage as hu  # noqa: E402
import usage_db as udb  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def now():
    return datetime.now(timezone.utc)


def step(sid, status, msg=""):
    clean = str(msg).replace("\n", " ").replace("|", "/")[:500]
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def _gpus_module():
    spec = importlib.util.spec_from_file_location("harvester_gpus", HERE / "harvester-gpus.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def request_from(args):
    """(demande, inventaire ou None) depuis --spec ou les options."""
    if args.spec:
        data = json.loads(Path(args.spec).read_text())
        if not isinstance(data, dict):
            raise ValueError("the spec must be a JSON object")
        inv = data.pop("inventory", None)
        return data, inv
    holder = {"kind": "group", "name": args.group} if args.group else \
        {"kind": "user", "name": args.holder or getattr(args, "as_user", None) or getpass.getuser()}
    target = None
    if args.target_vm or args.target_workload:
        ns, _, name = (args.target_vm or args.target_workload).partition("/")
        target = {"kind": "vm" if args.target_vm else "workload", "namespace": ns, "name": name}
    req = {"cluster": args.cluster, "type": args.type, "unit": args.unit or "", "model": args.model or "",
           "count": args.count, "holder": holder, "start": args.start, "end": args.end,
           "target": target, "note": args.note or ""}
    return req, None


def inventory_for(args, cluster, given):
    """Les unités GPU du cluster : celles passées, sinon --inventory, sinon
    lues sur le cluster."""
    if given is not None:
        if not isinstance(given, list):
            raise ValueError("inventory must be a list of GPU units")
        return given
    if args.inventory:
        data = json.loads(Path(args.inventory).read_text())
        units = data.get("units") if isinstance(data, dict) else data
        return [u for u in units or [] if isinstance(u, dict) and u.get("cluster", cluster) == cluster]
    gpus = _gpus_module()
    ns = argparse.Namespace(cluster=cluster, kubeconfig=args.kubeconfig)
    [(name, kube)] = gpus.targets_from(ns)
    if gpus.reachable(kube) is False:
        raise RuntimeError(f"cluster {cluster} is unreachable: its GPU inventory is needed to check capacity")
    inv, _denials = gpus.read_cluster(kube, cluster)
    return inv["units"]


def prepare(args):
    """(réservation normalisée et résolue, capacité de son modèle)."""
    req, given = request_from(args)
    booking = hb.normalize(req, now())
    units = inventory_for(args, booking["cluster"], given)
    booking = hb.resolve(booking, units)
    return booking, hb.capacity(units, booking["type"], booking["model"])


def cmd_check(args):
    booking, cap = prepare(args)
    conn = bdb.connect(args.db)
    existing = bdb.list_bookings(conn, cluster=booking["cluster"], since=booking["start"])
    reasons = hb.conflicts(booking, existing, cap) + bdb.quota_reasons(conn, booking)
    if args.json:
        print(json.dumps({"ok": not reasons, "capacity": cap, "booking": booking, "conflicts": reasons}, indent=2))
    elif reasons:
        for r in reasons:
            print(f"conflict: {hb.describe(r)}")
    else:
        print(f"ok: {booking['count']} x {booking['type']} {booking['model']} free on {booking['cluster']} "
              f"from {booking['start']} to {booking['end']} (capacity {cap})")
    return EXIT_FAIL if reasons else EXIT_OK


def cmd_create(args):
    step("check", "running", "checking the slot against the bookings and the GPU inventory")
    try:
        booking, cap = prepare(args)
    except (ValueError, RuntimeError) as e:
        step("check", "error", str(e))
        raise
    conn = bdb.connect(args.db)
    try:
        saved = bdb.insert_checked(conn, booking, cap, args.as_user or getpass.getuser(), now(), bid=args.id)
    except bdb.Conflict as e:
        step("check", "error", f"refused: {e}")
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_FAIL
    step("check", "done", f"capacity {cap}, no conflict")
    what = saved["unit"] or f"{saved['count']} x {saved['model']}"
    step("book", "done", f"booked {saved['type']} {what} on {saved['cluster']} for "
                         f"{saved['holder']['kind']} {saved['holder']['name']}, {saved['start']} to {saved['end']} "
                         f"({saved['id']}); kept in the console, not enforced on the cluster")
    if args.json:
        print(json.dumps(saved, indent=2))
    else:
        print(saved["id"])
    return EXIT_OK


def cmd_cancel(args):
    if not hb.ID_RE.match(args.id or ""):
        raise ValueError("invalid booking id")
    conn = bdb.connect(args.db)
    b = bdb.get(conn, args.id)
    if not b:
        step("cancel", "error", f"no booking {args.id}")
        return EXIT_FAIL
    user = args.as_user or getpass.getuser()
    if not hb.can_cancel(b, user, args.role):
        step("cancel", "error", f"{user} ({args.role}) may cancel only their own bookings")
        return EXIT_FAIL
    st = hb.state(b, now())
    if st in ("cancelled", "ended"):
        step("cancel", "error", f"booking {args.id} is already {st}")
        return EXIT_FAIL
    bdb.cancel(conn, args.id, user, now())
    step("cancel", "done", f"booking {args.id} cancelled")
    return EXIT_OK


def cmd_list(args):
    conn = bdb.connect(args.db)
    t = now()
    rows = []
    for b in bdb.list_bookings(conn, cluster=args.cluster):
        b["state"] = hb.state(b, t)
        if args.mine and not hb.is_mine(b, args.mine):
            continue
        if args.state and b["state"] != args.state:
            continue
        rows.append(b)
    if args.json:
        print(json.dumps({"bookings": rows}, indent=2))
        return EXIT_OK
    head = ("ID", "CLUSTER", "TYPE", "WHAT", "HOLDER", "START", "END", "STATE", "TARGET")
    lines = [head] + [(b["id"], b["cluster"], b["type"], b["unit"] or f"{b['count']} x {b['model']}",
                       f"{b['holder']['kind']}:{b['holder']['name']}", b["start"], b["end"], b["state"],
                       f"{b['target']['kind']}:{b['target']['namespace']}/{b['target']['name']}" if b["target"] else "-")
                      for b in rows]
    if not rows:
        print("(no booking)")
        return EXIT_OK
    widths = [max(len(r[i]) for r in lines) for i in range(len(head))]
    print("\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in lines))
    return EXIT_OK


def _fmt_h(v):
    return f"{v:.2f}"


def cmd_usage(args):
    t = now()
    start, end = hu.period(args.from_, args.to, t)
    bconn, uconn = bdb.connect(args.db), udb.connect(args.usage_db)
    rep = udb.build_report(bconn, uconn, start, end, hu.tz_from(args.tz), t)
    if args.json:
        print(json.dumps(rep, indent=2))
        return EXIT_OK
    if args.csv:
        sys.stdout.write(hu.to_csv(rep))
        return EXIT_OK
    print(f"GPU-hours from {rep['from']} to {rep['to']} (quotas: {rep['quota_month']})")
    if not rep["holders"]:
        print("(no booking and no GPU use in this period)")
        return EXIT_OK
    head = ("HOLDER", "BOOKED", "CONSUMED", "WASTE", "OUT-OF-RULE", "QUOTA", "REMAINING")
    lines = [head]
    for r in rep["holders"]:
        q = r["quota"] or {}
        who = f"{r['holder']['kind']}:{r['holder']['name']}"
        quota = "-" if q.get("monthly_hours") is None else _fmt_h(q["monthly_hours"])
        if q.get("max_units"):
            quota += f" ({q['max_units']} units)"
        lines.append((who, _fmt_h(r["booked"]), _fmt_h(r["consumed"]), _fmt_h(r["waste"]),
                      _fmt_h(r["out_of_rule"]), quota,
                      "-" if q.get("remaining") is None else _fmt_h(q["remaining"])))
    tot = rep["totals"]
    lines.append(("TOTAL", _fmt_h(tot["booked"]), _fmt_h(tot["consumed"]), _fmt_h(tot["waste"]),
                  _fmt_h(tot["out_of_rule"]), "", ""))
    widths = [max(len(r[i]) for r in lines) for i in range(len(head))]
    print("\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip() for r in lines))
    return EXIT_OK


def _holder_of(args):
    if bool(args.holder) == bool(args.group):
        raise ValueError("give --holder USER or --group NAME")
    return {"kind": "group", "name": args.group} if args.group else {"kind": "user", "name": args.holder}


def cmd_quota(args):
    conn = bdb.connect(args.db)
    if args.qcmd == "list":
        rows = bdb.list_quotas(conn)
        if args.json:
            print(json.dumps({"quotas": rows}, indent=2))
        elif not rows:
            print("(no quota)")
        else:
            for q in rows:
                h = q["holder"]
                hours = "-" if q["monthly_hours"] is None else _fmt_h(q["monthly_hours"])
                print(f"{h['kind']}:{h['name']}  monthly GPU-hours {hours}  max units {q['max_units'] or '-'}"
                      f"  (set by {q['updated_by'] or '?'} on {q['updated_at']})")
        return EXIT_OK
    holder = _holder_of(args)
    who = f"{holder['kind']} {holder['name']}"
    if args.qcmd == "set":
        quota = hu.normalize_quota({"holder": holder, "monthly_hours": args.monthly_hours,
                                    "max_units": args.max_units})
        saved = bdb.set_quota(conn, quota, args.as_user or getpass.getuser(), now())
        hours = "none" if saved["monthly_hours"] is None else _fmt_h(saved["monthly_hours"])
        step("quota", "done", f"quota of {who}: monthly GPU-hours {hours}, max concurrent units "
                              f"{saved['max_units'] or 'none'}; applied to new bookings")
        if args.json:
            print(json.dumps(saved, indent=2))
        return EXIT_OK
    if not bdb.delete_quota(conn, holder):
        step("quota", "error", f"no quota for {who}")
        return EXIT_FAIL
    step("quota", "done", f"quota of {who} removed")
    return EXIT_OK


def _slot_args(sp):
    sp.add_argument("--spec", help="the booking as JSON (with an optional `inventory` list of GPU units)")
    sp.add_argument("--cluster", help="the cluster of the GPUs")
    sp.add_argument("--type", choices=hb.TYPES, help="unit type")
    sp.add_argument("--unit", help="a specific unit id from the GPU inventory (gpu, vgpu)")
    sp.add_argument("--model", help="vGPU type, MIG profile, GPU resource or GPU model, booked by count")
    sp.add_argument("--count", type=int, default=1, help="how many units of the model (default 1)")
    sp.add_argument("--start", help="start, ISO 8601 (2026-10-20T09:00:00Z)")
    sp.add_argument("--end", help="end, ISO 8601, after the start, 90 days at most")
    sp.add_argument("--holder", help="the person who holds the booking (default: --as, or you)")
    sp.add_argument("--group", help="a group that holds the booking (console role or Rancher group)")
    sp.add_argument("--target-vm", help="optional target VM, namespace/name")
    sp.add_argument("--target-workload", help="optional target container workload, namespace/name")
    sp.add_argument("--note", help="free note")
    sp.add_argument("--inventory", help="GPU inventory JSON (output of harvester-gpus.py list --json)")
    sp.add_argument("--kubeconfig", help="read the GPU inventory with this kubeconfig")
    sp.add_argument("--db", help="bookings database (default: HARVESTER_OPS_BOOKINGS_DB or the state directory)")
    sp.add_argument("--json", action="store_true", help="JSON output")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="harvester-bookings",
                                 description="GPU bookings kept by the console (not enforced on the clusters).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("list", help="bookings, nearest first")
    sp.add_argument("--db")
    sp.add_argument("--cluster")
    sp.add_argument("--mine", metavar="USER", help="only those created by or held by USER")
    sp.add_argument("--state", choices=hb.STATES)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_list)
    sp = sub.add_parser("check", help="would this booking pass? (nothing written)")
    _slot_args(sp)
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("create", help="book a slot")
    _slot_args(sp)
    sp.add_argument("--as", dest="as_user", help="who creates it (default: you)")
    sp.add_argument("--id", help="booking id to use (bk-...)")
    sp.set_defaults(fn=cmd_create)
    sp = sub.add_parser("cancel", help="cancel a booking (kept in the history)")
    sp.add_argument("--id", required=True)
    sp.add_argument("--as", dest="as_user", help="who cancels (default: you)")
    sp.add_argument("--role", choices=("operator", "admin"), default="operator",
                    help="admin cancels anyone's booking, operator only their own")
    sp.add_argument("--db")
    sp.set_defaults(fn=cmd_cancel)
    sp = sub.add_parser("usage", help="GPU-hours booked and consumed per holder over a period")
    sp.add_argument("--from", dest="from_", help="start of the period, ISO 8601 (default: start of this month, UTC)")
    sp.add_argument("--to", help="end of the period, ISO 8601 (default: start of next month, UTC)")
    sp.add_argument("--tz", default="0", help="minutes east of UTC for the days of the series (default 0)")
    sp.add_argument("--db", help="bookings database")
    sp.add_argument("--usage-db", help="GPU samples database (default: HARVESTER_OPS_USAGE_DB or the state directory)")
    out = sp.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true")
    out.add_argument("--csv", action="store_true", help="CSV, one row per holder and per cluster and type")
    sp.set_defaults(fn=cmd_usage)
    sp = sub.add_parser("quota", help="quotas per person or group, applied when a booking is created")
    qsub = sp.add_subparsers(dest="qcmd", required=True)
    q = qsub.add_parser("list", help="the quotas")
    q.add_argument("--db")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_quota)
    for name, helptext in (("set", "set or replace the quota of a person or group"),
                           ("delete", "remove the quota of a person or group")):
        q = qsub.add_parser(name, help=helptext)
        q.add_argument("--holder", help="a person (console account)")
        q.add_argument("--group", help="a group (console role or Rancher group)")
        if name == "set":
            q.add_argument("--monthly-hours", help="cap of booked GPU-hours per calendar month (UTC)")
            q.add_argument("--max-units", help="maximum units booked at the same time, all clusters")
            q.add_argument("--json", action="store_true")
        q.add_argument("--as", dest="as_user", help="who changes it (default: you)")
        q.add_argument("--db")
        q.set_defaults(fn=cmd_quota)
    args = ap.parse_args(argv)
    if args.cmd in ("check", "create") and not args.spec and not (args.cluster and args.type and args.start and args.end):
        ap.error("give --spec, or --cluster, --type, --start and --end")
    try:
        return args.fn(args)
    except (ValueError, json.JSONDecodeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL
    except Exception as e:  # noqa: BLE001 - un cluster illisible (KubeError) est dit
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
