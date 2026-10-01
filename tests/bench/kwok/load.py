#!/usr/bin/env python3
"""Charge simulée sur une console : des personnes réparties sur les clusters
déclarés, chacune avec l'aperçu de « son » cluster ouvert (dock d'activité
chaque seconde ; VMs, aperçu, topologie, usage toutes les 5 s, comme
l'interface). Mesure la latence par écran et le CPU consommé par la console ET
par les kubectl qu'elle lance (temps des enfants), en cœurs.

  load.py --base http://127.0.0.1:8135 --users 30 --duration 60 --pid <pid console>
  load.py ... --users 0     # surveillances seules (une requête légère toutes les 10 s
                            # garde la console « utilisée », donc au rythme de 15 s)

La console de banc doit être ouverte (HARVESTER_OPS_AUTH=none) ; voir README.md.
"""
import argparse
import json
import os
import statistics
import threading
import time
import urllib.request

SCREENS = ("vms", "status", "topology", "usage")


def cpu_split(pid):
    """(console, enfants) : temps CPU du processus et de ses enfants terminés
    (les kubectl, scripts et interpréteurs qu'il lance)."""
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    hz = os.sysconf("SC_CLK_TCK")
    return (int(f[11]) + int(f[12])) / hz, (int(f[13]) + int(f[14])) / hz


def rss_mb(pid):
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:8135")
    p.add_argument("--users", type=int, default=10)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    clusters = [c["name"] for c in json.load(urllib.request.urlopen(a.base + "/api/clusters"))["clusters"]]
    lat, errors, lock = {}, [0], threading.Lock()
    err_by = {}

    def get(path, key):
        t = time.time()
        try:
            with urllib.request.urlopen(a.base + path, timeout=120) as r:
                body = r.read()
                if r.status != 200 or body.lstrip()[:1] not in (b"{", b"["):
                    errors[0] += 1
                    err_by[key] = err_by.get(key, 0) + 1
        except Exception as e:
            errors[0] += 1
            err_by[key] = err_by.get(key, 0) + 1
            err_by[key + ":last"] = str(e)[:120]
        with lock:
            lat.setdefault(key, []).append(time.time() - t)

    stop = time.time() + a.duration

    def user(i):
        c = clusters[i % len(clusters)]
        time.sleep(i * 0.1)
        n = 0
        while time.time() < stop:
            get("/api/activity", "activity")
            if n % 5 == 0:
                for s in SCREENS:
                    get(f"/api/{s}/{c}", s)
            n += 1
            time.sleep(1)

    def keepalive():
        while time.time() < stop:
            get("/api/whoami", "whoami")
            time.sleep(10)

    c0, t0 = cpu_split(a.pid), time.time()
    threads = [threading.Thread(target=user, args=(i,)) for i in range(a.users)]
    if not a.users:
        threads = [threading.Thread(target=keepalive)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    c1, dt = cpu_split(a.pid), time.time() - t0
    own, kids = (c1[0] - c0[0]) / dt, (c1[1] - c0[1]) / dt
    res = {"clusters": len(clusters), "users": a.users, "errors": errors[0], "errors_by": err_by,
           "cpu_cores": round(own + kids, 2), "cpu_console": round(own, 2), "cpu_children": round(kids, 2),
           "rss_mb": round(rss_mb(a.pid)), "screens": {}}
    for k, v in sorted(lat.items()):
        v.sort()
        res["screens"][k] = {"n": len(v), "p50": round(statistics.median(v), 3),
                             "p95": round(v[max(0, int(len(v) * .95) - 1)], 3), "max": round(v[-1], 3)}
    if a.json:
        print(json.dumps(res))
        return
    print(f"clusters={res['clusters']} users={a.users} errors={errors[0]} "
          f"cpu={res['cpu_cores']} cores (console {res['cpu_console']}, kubectl & scripts "
          f"{res['cpu_children']}) rss={res['rss_mb']} MB" + (f" errors_by={err_by}" if err_by else ""))
    for k, s in res["screens"].items():
        print(f"  {k:9s} n={s['n']:5d} p50={s['p50']*1000:7.0f}ms p95={s['p95']*1000:7.0f}ms max={s['max']*1000:7.0f}ms")


if __name__ == "__main__":
    main()
