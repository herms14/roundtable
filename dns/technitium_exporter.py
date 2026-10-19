#!/usr/bin/env python3
"""Prometheus exporter for Technitium DNS (stdlib only). Runs on homelab-dns01, serves :9274/metrics.

Reads the dashboard stats with the read-only `monitor` token (TECHNITIUM_MONITOR_TOKEN from the systemd
EnvironmentFile) and sends one real DNS query to 127.0.0.1:53 per scrape, so "up" means "answers DNS", not just
"web API alive".
"""
import json
import os
import socket
import struct
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = os.environ.get("TECHNITIUM_API", "http://127.0.0.1:5380/api/")
TOKEN = os.environ.get("TECHNITIUM_MONITOR_TOKEN", "")
PORT = int(os.environ.get("EXPORTER_PORT", "9274"))
PROBE = os.environ.get("PROBE_NAME", "google.com")
STATS = {"totalQueries": "queries", "totalNoError": "noerror", "totalServerFailure": "servfail",
         "totalNxDomain": "nxdomain", "totalRefused": "refused", "totalAuthoritative": "authoritative",
         "totalRecursive": "recursive", "totalCached": "cached", "totalBlocked": "blocked", "totalDropped": "dropped"}
GAUGES = {"totalClients": "clients_last_hour", "zones": "zones", "cachedEntries": "cache_entries",
          "allowedZones": "allowed_zones", "blockedZones": "blocked_zones", "allowListZones": "allow_list_zones",
          "blockListZones": "block_list_zones"}


def probe(name=PROBE):
    """(ok, seconds, rcode) for one A query over UDP to the local server."""
    qid = int(time.time() * 1000) & 0xFFFF
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    t = time.monotonic()
    try:
        s.sendto(q, ("127.0.0.1", 53))
        data = s.recv(4096)
        rcode = data[3] & 0x0F
        ancount = struct.unpack(">H", data[6:8])[0]
        return rcode == 0 and ancount > 0, time.monotonic() - t, rcode
    except OSError:
        return False, time.monotonic() - t, -1
    finally:
        s.close()


def stats():
    req = urllib.request.Request(API + "dashboard/stats/get?type=LastHour",
                                 headers={"Authorization": "Bearer " + TOKEN})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    if d.get("status") != "ok":
        raise RuntimeError(d.get("errorMessage") or d.get("status"))
    return d["response"]["stats"]


def metrics():
    out = []
    ok, secs, rcode = probe()
    out += ["# HELP technitium_probe_success 1 if a local A query for the probe name got an answer.",
            "# TYPE technitium_probe_success gauge", f"technitium_probe_success {int(ok)}",
            "# TYPE technitium_probe_duration_seconds gauge", f"technitium_probe_duration_seconds {secs:.4f}",
            "# TYPE technitium_probe_rcode gauge", f"technitium_probe_rcode {rcode}"]
    try:
        s = stats()
        api_up = 1
    except Exception:  # noqa: BLE001 - reported as technitium_api_up 0
        s, api_up = {}, 0
    out += ["# HELP technitium_api_up 1 if the web API answered.", "# TYPE technitium_api_up gauge",
            f"technitium_api_up {api_up}",
            "# HELP technitium_queries_last_hour Queries in the last hour by result (dashboard stats).",
            "# TYPE technitium_queries_last_hour gauge"]
    out += [f'technitium_queries_last_hour{{result="{v}"}} {s[k]}' for k, v in STATS.items() if k in s]
    for k, v in GAUGES.items():
        if k in s:
            out += [f"# TYPE technitium_{v} gauge", f"technitium_{v} {s[k]}"]
    return "\n".join(out) + "\n"


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
