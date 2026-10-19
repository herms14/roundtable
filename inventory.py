#!/usr/bin/env python3
"""Live, read-only homelab inventory shared by every Roundtable agent (voice + Discord).

  compute     Proxmox cluster: nodes, every VM/LXC (status, node, CPU/RAM, IP, VLAN, tags), storage   (pvesh via node SSH)
  network     Omada: VLANs/subnets, gateway/switch/AP status, clients per VLAN, WAN                      (read-only API)
  containers  Docker containers on every VM/LXC in ~/.ssh/sre-hosts.txt                                 (docker inspect)

  python3 inventory.py [compute|network|containers|all] [filter]
Results are cached in kv (inventory:<section>) for a couple of minutes so voice calls stay snappy.
"""
import concurrent.futures as cf
import ipaddress
import json
import re
import sys
import time

import olympus_core as o

TTL = {"compute": 120, "network": 120, "containers": 300}
NODES = ["node01", "node02", "node03"]

PVE_SCRIPT = r"""
pvesh get /cluster/resources --output-format json
echo
echo '@@@'
for f in /etc/pve/nodes/*/lxc/*.conf /etc/pve/nodes/*/qemu-server/*.conf; do
  [ -f "$f" ] || continue
  awk -v id="$(basename "$f" .conf)" '/^\[/{exit} /^(net[0-9]+|ipconfig[0-9]+|hostname|description):/{print id"|"$0}' "$f"
done
"""

DOCKER_SCRIPT = r"""
D=""
if command -v docker >/dev/null 2>&1; then
  if docker ps -q >/dev/null 2>&1; then D="docker"; elif sudo -n docker ps -q >/dev/null 2>&1; then D="sudo -n docker"; fi
fi
[ -n "$D" ] && $D ps -a --format '{{.Names}}|{{.State}}|{{.Status}}|{{.Image}}'
echo "END"
"""


def _cached(section, fn, fresh=False):
    c = o.kv_get(f"inventory:{section}")
    if c and not fresh and time.time() - c.get("ts", 0) < TTL[section]:
        return c["data"]
    data = fn()
    if data and not data.get("error"):
        o.kv_set(f"inventory:{section}", {"ts": time.time(), "data": data})
    elif c:  # source down: serve the last good copy, flagged
        c["data"]["error"] = f"{data.get('error')} (showing data from " \
                             f"{time.strftime('%d %b %H:%M', time.localtime(c['ts']))})"
        return c["data"]
    return data


# --------------------------------------------------------------------------- compute (Proxmox)

def _gb(b):
    return round((b or 0) / 1024 ** 3, 1)


def _compute():
    out, err = None, []
    for n in NODES:
        rc, out, e = o.ssh(n, PVE_SCRIPT, timeout=40)
        if rc == 0 and "@@@" in out:
            break
        err.append(f"{n}: {e[:120] or rc}")
        out = None
    if not out:
        return {"error": "Proxmox unreachable (" + "; ".join(err) + ")"}
    res_txt, conf_txt = out.split("@@@", 1)
    res = json.loads(res_txt.strip())
    net = {}
    for line in conf_txt.splitlines():
        if "|" not in line:
            continue
        vid, kv = line.split("|", 1)
        k, _, v = kv.partition(": ")
        g = net.setdefault(vid, {"ips": [], "vlans": [], "interfaces": {}})
        if k == "hostname":
            g["hostname"] = v
        elif k == "description":
            g["desc"] = re.sub(r"%[0-9A-Fa-f]{2}", " ", v)[:120]
        m = re.search(r"\bip=([\d.]+)", v)
        if m:
            g["ips"].append(m.group(1))
        elif re.search(r"\bip=dhcp", v):
            g["ips"].append("dhcp")
        m = re.search(r"\btag=(\d+)", v)
        if m and m.group(1) not in g["vlans"]:
            g["vlans"].append(m.group(1))
        if re.fullmatch(r"(?:net|ipconfig)\d+", k):
            index = re.search(r"\d+$", k).group()
            interface = g["interfaces"].setdefault(index, {})
            mac = re.search(r"\b(?:hwaddr|virtio|e1000e?|rtl8139|vmxnet3)=([0-9A-Fa-f:]{17})", v)
            address = re.search(r"\bip=([\d.]+)", v)
            if mac:
                interface["mac"] = mac.group(1).upper().replace(":", "-")
            if address:
                interface["ip"] = address.group(1)
            if m:
                interface["vlan"] = int(m.group(1))
    hosts = {h["name"]: h["ip"] for h in o.load_hosts().values()}
    nodes, guests, storage = [], [], []
    for r in res:
        t = r.get("type")
        if t == "node":
            nodes.append({"node": r["node"], "status": r.get("status"), "cpu_pct": round((r.get("cpu") or 0) * 100),
                          "cores": r.get("maxcpu"), "ram_gb": _gb(r.get("mem")), "ram_max_gb": _gb(r.get("maxmem")),
                          "uptime_days": round((r.get("uptime") or 0) / 86400, 1)})
        elif t in ("qemu", "lxc"):
            vid = str(r.get("vmid"))
            g = net.get(vid, {})
            ips = [i for i in g.get("ips", []) if i != "dhcp"] or ([hosts[r.get("name")]] if r.get("name") in hosts
                                                                     else g.get("ips", []))
            guests.append({"id": r.get("vmid"), "name": r.get("name"), "kind": "VM" if t == "qemu" else "LXC",
                           "node": r.get("node"), "status": r.get("status"), "template": bool(r.get("template")),
                           "cores": r.get("maxcpu"), "cpu_pct": round((r.get("cpu") or 0) * 100),
                           "ram_gb": _gb(r.get("mem")), "ram_max_gb": _gb(r.get("maxmem")),
                           "disk_max_gb": _gb(r.get("maxdisk")), "uptime_days": round((r.get("uptime") or 0) / 86400, 1),
                           "ip": ", ".join(ips), "vlan": ", ".join(g.get("vlans", [])),
                           "interfaces": list(g.get("interfaces", {}).values()),
                           "tags": (r.get("tags") or "").strip(), "note": g.get("desc", "")})
        elif t == "storage" and r.get("status") == "available":
            storage.append({"storage": r.get("storage"), "node": r.get("node"), "shared": bool(r.get("shared")),
                            "used_gb": _gb(r.get("disk")), "size_gb": _gb(r.get("maxdisk"))})
    seen, shared = set(), []
    for s in storage:  # shared storage appears once per node
        if s["shared"]:
            if s["storage"] in seen:
                continue
            seen.add(s["storage"])
            s["node"] = "shared"
        shared.append(s)
    guests.sort(key=lambda g: g["id"])
    # Every NIC MAC in any cluster guest config (/etc/pve is cluster-wide), so IPAM can tell
    # a destroyed guest's leftover Omada client from a live one.
    macs = sorted({i["mac"] for g in net.values() for i in g["interfaces"].values() if i.get("mac")})
    return {"t": o.iso(), "nodes": sorted(nodes, key=lambda n: n["node"]), "guests": guests, "storage": shared,
            "macs": macs}


# --------------------------------------------------------------------------- network (Omada, read-only)

def _omada_rows(om, path, query="", key="id"):
    rows, total, seen = [], None, set()
    for page in range(1, 101):
        result = om.get(f"{path}?currentPage={page}&currentPageSize=1000" + (f"&{query}" if query else ""))
        count, batch = result.get("totalRows"), result.get("data")
        if (not isinstance(count, int) or count < 0 or not isinstance(batch, list)
                or result.get("currentPage") != page or (total is not None and count != total)):
            raise RuntimeError(f"Incomplete or changing Omada inventory: {path}")
        total = count
        for row in batch:
            identity = row.get(key)
            if not identity or identity in seen:
                raise RuntimeError(f"Missing or duplicate Omada {key}: {path}")
            seen.add(identity)
        rows.extend(batch)
        if len(rows) == total:
            return rows
        if not batch or len(rows) > total:
            break
    raise RuntimeError(f"Incomplete Omada inventory: {path} ({len(rows)}/{total} rows)")


def _ipv4(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    return str(address) if address.version == 4 else None


def _mesh_transport(om, client, history, devices):
    if client.get("wireless") is not True or client.get("vid") != 0 or not history:
        return None
    if any(h.get("mac") != client["mac"] or h.get("vid") != 0 or h.get("ip") for h in history):
        return None
    latest = max(history, key=lambda h: h.get("lastSeen", 0))
    if latest.get("lastSeen", 0) < client.get("lastSeen", 0):
        return None
    ssid = latest.get("ssid", "")
    parents = [d for d in devices if d.get("type") == "ap" and d.get("statusCategory") == 1
               and ssid == "mesh_" + d["mac"].replace("-", "").lower()]
    if len(parents) != 1:
        return None
    parent = parents[0]
    if any(h.get("ssid") != ssid or h.get("deviceName") != parent["name"] for h in history):
        return None
    groups = om.get("/wlanSsid")
    if not isinstance(groups, list) or not groups:
        raise RuntimeError("Configured WLAN inventory is unverified")
    user_ssids = {s["name"] for group in groups for s in group["ssids"]}
    if ssid in user_ssids:
        return None
    root = om.get("/eaps/" + parent["mac"])
    mesh = om.get("/eaps/" + parent["mac"] + "/mesh/stastic")
    if (root.get("mac") != parent["mac"] or root.get("isMeshStatus") is not True
            or root.get("statusCategory") != 1 or mesh.get("statusCategory") != 1
            or mesh.get("wirelessLinked") is not False):
        return None
    peers = mesh.get("childAps")
    if not isinstance(peers, list) or len(peers) != 1:
        return None
    peer = peers[0]
    matches = [d for d in devices if d.get("mac") == peer.get("mac") and d.get("type") == "ap"
               and d.get("statusCategory") == 1 and d.get("wirelessLinked") is True
               and d.get("uplink") == parent["mac"] and d.get("ip") == peer.get("ip")]
    if (len(matches) != 1 or peer.get("type") != "ap" or peer.get("statusCategory") != 1
            or not _ipv4(parent.get("ip")) or not _ipv4(peer.get("ip"))):
        return None
    return {"mac": client["mac"], "kind": "omada_mesh_backhaul", "ssid": ssid,
            "parent_mac": parent["mac"], "parent_ip": parent["ip"],
            "peer_mac": peer["mac"], "peer_ip": peer["ip"], "last_seen": latest["lastSeen"],
            "evidence": "Complete internal-mesh association history, no configured user SSID, "
                        "online mesh-enabled root and unique reciprocal managed-AP wireless link"}


def _network():
    import netops
    try:
        om = netops.Omada()
        ov = om.get("/dashboard/overviewDiagram")
        devs = om.get("/devices")
        nets = _omada_rows(om, "/setting/lan/networks")
    except Exception as e:  # noqa: BLE001
        return {"error": f"Omada API: {e}"}
    per_vlan, clients, reservations, bindings, unresolved, ipam_error = {}, [], [], [], [], None
    transports, active = [], []   # active: connected right now (the IP map's per-VLAN usage counts DHCP leases from it)
    try:
        cl = _omada_rows(om, "/clients", "filters.active=true", key="mac")
        known = _omada_rows(om, "/insight/clients", key="mac")
        reservations = _omada_rows(om, "/setting/service/dhcp")
        bindings = _omada_rows(om, "/setting/firewall/imbs")
        if any(not _ipv4(c.get("ip")) for c in reservations + bindings + devs):
            raise RuntimeError("Device, DHCP reservation or IP/MAC binding addresses are unverified")
        current = {c["mac"]: c for c in cl}
        managed = {d["mac"]: d for d in devs if d.get("mac") and _ipv4(d.get("ip"))}
        for c in cl:
            k = str(c.get("vid") or c.get("networkName") or "?")
            per_vlan[k] = per_vlan.get(k, 0) + 1
            if _ipv4(c.get("ip")):
                active.append({"ip": _ipv4(c.get("ip")), "vlan": c.get("vid")})
        for c in cl + known:
            address = _ipv4(c.get("ip"))
            match = current.get(c["mac"]) or managed.get(c["mac"])
            if not address and match:
                address = _ipv4(match.get("ip"))
            if address:
                clients.append({"ip": address, "vlan": c.get("vid"), "name": c.get("name")})
                continue
            history = _omada_rows(om, "/clientHistory/" + c["mac"])
            clients.extend({"ip": _ipv4(h["ip"]), "vlan": h.get("vid"), "name": c.get("name")}
                           for h in history if _ipv4(h.get("ip")))
            latest = max(history, key=lambda h: h.get("lastSeen", 0), default={})
            if (_ipv4(latest.get("ip")) and latest.get("lastSeen", 0) >= c.get("lastSeen", 0)
                    and latest.get("vid") == c.get("vid")):
                continue
            transport = _mesh_transport(om, c, history, devs)
            if transport:
                transports.append(transport)
                continue
            unresolved.append({"mac": c["mac"], "vlan": c.get("vid"), "name": c.get("name"),
                               "reason": "Last-seen client lacks authoritative IPv4 evidence"})
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as e:
        ipam_error = f"Known client inventory unavailable: {e}"
        o.log("inventory", ipam_error)
    vlans = []
    for n in nets:
        vid = n.get("vlan")
        dhcp = n.get("dhcpSettings") or {}
        ranges = dhcp.get("ipRangePool") or []
        if len(ranges) > 1:
            ipam_error = "Multiple DHCP pools require manual IPAM review; allocation is blocked"
        vlans.append({"name": n["name"], "vlan": vid, "subnet": n.get("gatewaySubnet"),
                      "dhcp": bool(dhcp["enable"]) if "enable" in dhcp else None,
                      "dhcp_range": {"start": dhcp.get("ipaddrStart") or dhcp.get("ipStart") or dhcp.get("start"),
                                     "end": dhcp.get("ipaddrEnd") or dhcp.get("ipEnd") or dhcp.get("end")},
                      "clients": per_vlan.get(str(vid), per_vlan.get(n["name"]))})
    return {"t": o.iso(), "vlans": sorted(vlans, key=lambda v: v["vlan"] or 0),
            "clients": clients, "active_clients": active, "dhcp_reservations": reservations, "address_bindings": bindings,
            "unresolved_clients": unresolved, "layer2_transports": transports,
            "ipam_error": ipam_error,
            "devices": [{"name": d["name"], "type": d["type"], "model": d["model"], "ip": d.get("ip"),
                         "online": d.get("statusCategory") == 1, "firmware": d.get("version"),
                         "upgrade_available": bool(d.get("needUpgrade")), "cpu_pct": d.get("cpuUtil"),
                         "mem_pct": d.get("memUtil"), "clients": d.get("clientNum"),
                         "uptime_days": round((d.get("uptimeLong") or 0) / 86400, 1)} for d in devs],
            "overview": {k: ov.get(k) for k in ("totalClientNum", "wiredClientNum", "wirelessClientNum", "guestNum",
                                                "connectedSwitchNum", "totalSwitchNum", "connectedApNum", "totalApNum",
                                                "connectedGatewayNum", "totalGatewayNum", "wanHealth")},
            "dns": "Technitium 192.168.90.60 (primary, homelab-dns01) + Pi-hole 192.168.90.53 (secondary)", "reverse_proxy": "Traefik 192.168.40.20 (*.hrmsmrflrii.xyz)"}


# --------------------------------------------------------------------------- containers (Docker)

def _containers():
    hosts = [h for h in o.load_hosts().values() if h["type"] in ("vm", "lxc")]

    def one(h):
        rc, out, e = o.ssh(h["name"], DOCKER_SCRIPT, timeout=25)
        if "END" not in out:
            return h["name"], None, (e or str(rc))[:100]
        rows = []
        for line in out.splitlines():
            p = line.split("|")
            if len(p) == 4:
                rows.append({"name": p[0], "state": p[1], "status": p[2], "image": p[3]})
        return h["name"], rows, None

    with cf.ThreadPoolExecutor(12) as ex:
        res = list(ex.map(one, hosts))
    return {"t": o.iso(), "hosts": {n: r for n, r, _ in res if r},
            "unreachable": {n: e for n, r, e in res if r is None}}


SOURCES = {"compute": _compute, "network": _network, "containers": _containers}


def get(section, fresh=False):
    return _cached(section, SOURCES[section], fresh)


# --------------------------------------------------------------------------- text for LLMs / voice

def _match(f, *vals):
    return not f or any(f in str(v).lower() for v in vals if v is not None)


def text(section="all", flt=None, limit=None):
    f = (flt or "").lower().strip()
    parts = []
    if section in ("compute", "all"):
        c = get("compute")
        if c.get("error") and not c.get("guests"):
            parts.append(f"COMPUTE: unavailable - {c['error']}")
        else:
            g = c["guests"]
            real = [x for x in g if not x["template"]]
            vms, lxcs = [x for x in real if x["kind"] == "VM"], [x for x in real if x["kind"] == "LXC"]
            run = lambda xs: sum(x["status"] == "running" for x in xs)  # noqa: E731
            parts.append(
                f"COMPUTE (Proxmox cluster MorpheusCluster, as of {c['t'][11:16]} UTC"
                + (f", STALE: {c['error']}" if c.get("error") else "") + "):\n"
                f"Totals: {len(vms)} VMs ({run(vms)} running), {len(lxcs)} LXCs ({run(lxcs)} running), "
                f"{len(g) - len(real)} templates.\n"
                "Nodes: " + "; ".join(f"{n['node']} {n['status']} CPU {n['cpu_pct']}% of {n['cores']} cores, "
                                      f"RAM {n['ram_gb']}/{n['ram_max_gb']} GB, up {n['uptime_days']} d"
                                      for n in c["nodes"]) + "\n"
                "Guests (id kind name @node status cores ram ip vlan tags):\n" + "\n".join(
                    f"- {x['id']} {x['kind']} {x['name']} @{x['node']} {x['status']} {x['cores']}c "
                    f"{x['ram_gb']}/{x['ram_max_gb']}GB ip {x['ip'] or '?'} vlan {x['vlan'] or '-'}"
                    + (f" tags {x['tags']}" if x["tags"] else "") + (" [template]" if x["template"] else "")
                    for x in g if _match(f, x["id"], x["name"], x["node"], x["status"], x["ip"], x["vlan"], x["tags"],
                                         x["kind"]))[: limit or 20000])
    if section in ("storage", "all"):
        c = get("compute")
        if c.get("storage"):
            parts.append("STORAGE (Proxmox storage pools, used/size): " + "; ".join(
                f"{s['storage']} ({s['node']}{', shared' if s.get('shared') else ''}) {s['used_gb']}/{s['size_gb']} GB"
                for s in c["storage"] if _match(f, s["storage"], s["node"])))
        else:
            parts.append(f"STORAGE: unavailable - {c.get('error', 'no data')}")
    if section in ("network", "all"):
        n = get("network")
        if n.get("error") and not n.get("vlans"):
            parts.append(f"NETWORK: unavailable - {n['error']}")
        else:
            ov = n["overview"]
            parts.append(
                f"NETWORK (TP-Link Omada, as of {n['t'][11:16]} UTC):\n"
                f"{len(n['vlans'])} networks/VLANs: " + "; ".join(
                    f"{v['name']} VLAN {v['vlan']} {v['subnet']}" + (" DHCP" if v["dhcp"] else "")
                    + (f" {v['clients']} clients" if v["clients"] is not None else "") for v in n["vlans"]) + "\n"
                f"Clients: {ov.get('totalClientNum')} total ({ov.get('wiredClientNum')} wired, "
                f"{ov.get('wirelessClientNum')} wireless, {ov.get('guestNum')} guest). "
                f"Gateways {ov.get('connectedGatewayNum')}/{ov.get('totalGatewayNum')}, switches "
                f"{ov.get('connectedSwitchNum')}/{ov.get('totalSwitchNum')}, APs {ov.get('connectedApNum')}/"
                f"{ov.get('totalApNum')} online.\nDevices:\n" + "\n".join(
                    f"- {d['name']} ({d['type']} {d['model']}) {d['ip']} {'online' if d['online'] else 'OFFLINE'}, "
                    f"fw {d['firmware']}{' (upgrade available)' if d['upgrade_available'] else ''}, "
                    f"{d['clients']} clients, CPU {d['cpu_pct']}% RAM {d['mem_pct']}%, up {d['uptime_days']} d"
                    for d in n["devices"] if _match(f, d["name"], d["type"], d["model"], d["ip"])) +
                f"\nDNS: {n['dns']}. Reverse proxy: {n['reverse_proxy']}.")
    if section in ("containers", "all"):
        d = get("containers")
        hosts = d.get("hosts", {})
        tot = sum(len(v) for v in hosts.values())
        up = sum(r["state"] == "running" for v in hosts.values() for r in v)
        lines = []
        for h, rows in sorted(hosts.items()):
            sel = [r for r in rows if _match(f, h, r["name"], r["image"], r["state"])]
            if rows and sel:
                lines.append(f"- {h}: " + ", ".join(
                    r["name"] + ("" if r["state"] == "running" else f" ({r['status']})") for r in sel))
        parts.append(f"CONTAINERS (Docker, as of {d.get('t', '')[11:16]} UTC): {up}/{tot} running on "
                     f"{sum(1 for v in hosts.values() if v)} hosts\n" + "\n".join(lines)
                     + (f"\nUnreachable: {', '.join(d['unreachable'])}" if d.get("unreachable") else ""))
    return "\n\n".join(parts)


# Layer boundaries: agents.json "scope" lists layers; each layer maps to inventory sections.
LAYERS = {"application": ["containers"], "compute": ["compute"], "network": ["network"], "storage": ["storage"]}
ALL_LAYERS = list(LAYERS)


def agent_scope(aid):
    a = next((x for x in o.registry()["agents"] if x["id"] == aid), {})
    if a.get("homelab_visibility") is False:
        return []
    return a.get("scope", ALL_LAYERS if aid == "boss" else [])


def allowed_sections(aid):
    return [s for layer in agent_scope(aid) for s in LAYERS.get(layer, [])]


def scoped_text(aid, section="all", flt=None, limit=None):
    """Inventory restricted to the agent's layers. Returns (text, refused_sections)."""
    allowed = allowed_sections(aid)
    want = ["compute", "storage", "network", "containers"] if section == "all" else [section]
    ok = [s for s in want if s in allowed]
    refused = [s for s in want if s not in allowed]
    return "\n\n".join(text(s, flt, limit) for s in ok), refused


def owners(section):
    """Agents whose scope covers a section (for 'ask X instead')."""
    layer = next((k for k, v in LAYERS.items() if section in v), None)
    return [a for a in o.registry()["agents"] if layer in (a.get("scope") or []) and a["id"] not in ("boss",)]


def boundary_text(aid):
    """Prompt paragraph describing the agent's layer boundaries and chain of command."""
    sc = agent_scope(aid)
    if set(sc) >= set(ALL_LAYERS):
        vis = "You have visibility across ALL layers: application, compute, network and storage."
    elif sc:
        vis = (f"Your boundary: you only see and act on the {', '.join(sc)} layer(s). For anything outside it "
               "(" + ", ".join(l for l in ALL_LAYERS if l not in sc) + ") say it is outside your boundary and hand "
               "it to the agent who owns that layer, or to Gideon.")
    else:
        vis = ("You have no direct visibility into the homelab infrastructure layers; for questions about VMs, "
               "containers, network or storage, hand over to the owning agent or to Gideon.")
    import hierarchy
    return vis + " " + hierarchy.policy_text(aid)


def summary(cached_only=False, aid=None):
    """Compact always-on context (counts only) for system prompts. cached_only: never block on a live fetch.
    aid: only include the layers that agent may see."""
    allowed = allowed_sections(aid) if aid else ["compute", "network", "containers", "storage"]
    def load(sec):
        if cached_only:
            c = o.kv_get(f"inventory:{sec}")
            return c["data"] if c else {}
        return get(sec)
    bits = []
    try:
        c = load("compute") if "compute" in allowed else {}
        real = [x for x in c.get("guests", []) if not x["template"]]
        if real:
            bits.append(f"{sum(x['kind'] == 'VM' for x in real)} VMs and {sum(x['kind'] == 'LXC' for x in real)} LXCs "
                        f"({sum(x['status'] == 'running' for x in real)} running) on {len(c.get('nodes', []))} "
                        f"Proxmox nodes")
    except Exception as e:  # noqa: BLE001
        bits.append(f"compute unknown ({e})")
    try:
        n = load("network") if "network" in allowed else {}
        if n.get("vlans"):
            bits.append(f"{len(n['vlans'])} VLANs: " + ", ".join(f"{v['name']} ({v['vlan']})" for v in n["vlans"]))
    except Exception as e:  # noqa: BLE001
        bits.append(f"network unknown ({e})")
    try:
        d = load("containers") if "containers" in allowed else {}
        if d.get("hosts"):
            rows = [r for v in d["hosts"].values() for r in v]
            bits.append(f"{sum(r['state'] == 'running' for r in rows)}/{len(rows)} Docker containers running on "
                        f"{sum(1 for v in d['hosts'].values() if v)} hosts")
    except Exception as e:  # noqa: BLE001
        bits.append(f"containers unknown ({e})")
    if not allowed:
        return "no homelab layers in your scope"
    return "; ".join(bits) or "not loaded yet - call homelab_inventory"


def warm():
    for s in SOURCES:
        try:
            get(s)
        except Exception as e:  # noqa: BLE001
            o.log("inventory", f"warm {s}: {e}")


if __name__ == "__main__":
    sec = sys.argv[1] if len(sys.argv) > 1 else "all"
    print(text(sec, sys.argv[2] if len(sys.argv) > 2 else None))
