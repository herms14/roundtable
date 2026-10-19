#!/usr/bin/env python3
"""Torrent - keep Pi-hole (secondary DNS) answering the same local names as Technitium (primary).

  dnssync.py status      # compare Technitium A records with Pi-hole dns.hosts (read-only)
  dnssync.py sync        # write Pi-hole dns.hosts only when they differ (daily timer dns-sync)

Technitium (homelab-dns01, 192.168.90.60) is the source of truth: A records in hrmsmrflrii.xyz keep their full
name, A records in the "lan" zone become bare names (pihole, fs01, ...) as Pi-hole served them. Reads use the
read-only `monitor` token (TECHNITIUM_MONITOR_TOKEN); the Pi-hole write goes through `ssh pihole`.
"""
import json
import os
import sys

import olympus_core as o

AGENT = "netops"
API = os.environ.get("TECHNITIUM_API", "http://192.168.90.60:5380/api/")
ZONES = {"hrmsmrflrii.xyz": False, "lan": True}   # zone -> strip the zone suffix


def technitium():
    """{name_lower: ip} from Technitium, or raise."""
    want = {}
    for zone, bare in ZONES.items():
        s, d = o.http("GET", f"{API}zones/records/get?domain={zone}&zone={zone}&listZone=true",
                      headers={"Authorization": "Bearer " + os.environ.get("TECHNITIUM_MONITOR_TOKEN", "")})
        if s != 200 or not isinstance(d, dict) or d.get("status") != "ok":
            raise RuntimeError(f"Technitium {zone}: {d.get('errorMessage') if isinstance(d, dict) else d}")
        for r in d["response"]["records"]:
            if r["type"] != "A" or r.get("disabled"):
                continue
            name = r["name"].lower()
            if bare:
                name = name[:-len(zone) - 1] if name.endswith("." + zone) else name
            if name and name != zone:
                want[name] = r["rData"]["ipAddress"]
    return want


def pihole():
    """Current dns.hosts list ("ip name" strings)."""
    rc, out, err = o.ssh("pihole", "pihole-FTL --config dns.hosts", timeout=30)
    if rc != 0:
        raise RuntimeError(f"pihole: {err or out}")
    out = out.strip().strip("[]")
    return [e.strip() for e in out.split(",") if e.strip()]


def diff():
    want, have = technitium(), pihole()
    cur = {}
    for e in have:
        ip, _, name = e.partition(" ")
        cur[name.strip().lower()] = ip
    add = {n: ip for n, ip in want.items() if cur.get(n) != ip}
    drop = sorted(n for n in cur if n not in want)
    return want, have, add, drop


def status():
    want, have, add, drop = diff()
    print(f"Technitium {len(want)} A records | Pi-hole {len(have)} hosts")
    for n, ip in sorted(add.items()):
        print(f"  + {ip} {n}")
    for n in drop:
        print(f"  - {n}")
    print("in sync" if not add and not drop else f"{len(add)} to add/change, {len(drop)} to remove")
    return add, drop


def sync():
    run = o.record_run
    try:
        want, have, add, drop = diff()
    except Exception as e:  # noqa: BLE001 - reported, never half-written
        o.log(AGENT, f"dns-sync read failed: {e}")
        run(AGENT, "dns-sync", status="error", summary=str(e)[:300])
        raise SystemExit(1)
    if not add and not drop:
        run(AGENT, "dns-sync", summary=f"in sync ({len(want)} names)")
        print("in sync")
        return
    # Keep Pi-hole's own order and spelling for unchanged names, append the rest
    keep = []
    for e in have:
        ip, _, name = e.partition(" ")
        n = name.strip().lower()
        if n in want and n not in add:
            keep.append(e)
    new = keep + [f"{ip} {n}" for n, ip in sorted(add.items())]
    payload = json.dumps(new)
    assert "'" not in payload
    rc, out, err = o.ssh("pihole", f"pihole-FTL --config dns.hosts '{payload}' >/dev/null && "
                                   "pihole-FTL --config dns.hosts | tr ',' '\\n' | wc -l", timeout=60)
    if rc != 0:
        o.log(AGENT, f"dns-sync write failed: {err or out}")
        run(AGENT, "dns-sync", status="error", summary=(err or out)[:300])
        raise SystemExit(1)
    lines = [f"+ `{ip} {n}`" for n, ip in sorted(add.items())] + [f"- `{n}`" for n in drop]
    summary = f"Pi-hole local DNS synced from Technitium: {len(add)} added/changed, {len(drop)} removed"
    o.log(AGENT, summary)
    run(AGENT, "dns-sync", summary=summary)
    o.post("homelab-infrastructure", embeds=[o.embed(AGENT, "Secondary DNS synced", summary + "\n" +
                                                     "\n".join(lines)[:3500])])
    print(summary)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"status": status, "sync": sync}.get(cmd, lambda: sys.exit(__doc__))()
