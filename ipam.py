"""Diallos: evidence-based IP recommendations and atomic service reservations."""
import contextlib
import datetime as dt
import ipaddress
import subprocess
import re

import olympus_core as o

SCHEMA = """
CREATE TABLE IF NOT EXISTS ip_reservations(
 ip TEXT PRIMARY KEY, service TEXT NOT NULL UNIQUE, vlan INTEGER NOT NULL,
 state TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL);
"""
POOL = range(36, 50)
VLANS = {20, 40, 90}
PROXMOX_OUI = "BC-24-11-"  # MAC prefix Proxmox assigns to guest NICs


def _db():
    con = o.db()
    con.executescript(SCHEMA)
    return con


def picture(fresh=True):
    import inventory
    compute, network = inventory.get("compute", fresh=fresh), inventory.get("network", fresh=fresh)
    if compute.get("error") or network.get("error") or network.get("ipam_error"):
        raise RuntimeError("IPAM cannot confirm availability: " +
                           "; ".join(str(x.get("error") or x.get("ipam_error"))
                                     for x in (compute, network) if x.get("error") or x.get("ipam_error")))
    used, blockers = set(), {}
    def block(vlan, reason):
        key = str(vlan) if vlan in VLANS else "*"
        blockers.setdefault(key, []).append(reason)

    interfaces = {}
    for g in compute.get("guests", []):
        addresses = [part.split("/")[0] for part in re.split(r"[,\s]+", g.get("ip") or "")
                     if part and part != "dhcp"]
        used.update(addresses)
        if not addresses and not g.get("template"):
            vlans = [int(v) for v in re.findall(r"\d+", g.get("vlan") or "")]
            for vlan in vlans or [None]:
                if vlan in VLANS or vlan is None:
                    block(vlan, f"Guest {g.get('id')} has no authoritative static IPv4 address")
        for interface in g.get("interfaces", []):
            address = interface.get("ip")
            if not address and len(g["interfaces"]) == 1 and len(addresses) == 1:
                address = addresses[0]
            if not address and not g.get("template"):
                vlan = interface.get("vlan")
                if vlan in VLANS or vlan is None:
                    block(vlan, f"Guest {g.get('id')} interface has no authoritative static IPv4 address")
            if interface.get("mac") and address and not g.get("template"):
                mac = interface["mac"].upper().replace(":", "-")
                interfaces.setdefault(mac, set()).add(address)
    for group in ("devices", "clients", "dhcp_reservations", "address_bindings"):
        used.update(c["ip"] for c in network.get(group, []) if c.get("ip"))
    # Proxmox MACs exist only in guest configs; one absent from a complete cluster read belongs to a
    # destroyed guest and cannot rejoin the network, so its remembered Omada client is not a blocker.
    cluster_macs = set(compute["macs"]) | set(interfaces) if "macs" in compute else None
    retired = []
    for client in network.get("unresolved_clients", []):
        mac = client["mac"].upper().replace(":", "-")
        if cluster_macs is not None and mac.startswith(PROXMOX_OUI) and mac not in cluster_macs:
            retired.append({"mac": mac, "vlan": client.get("vlan"), "name": client.get("name")})
            continue
        addresses = interfaces.get(mac, set())
        address = next(iter(addresses)) if len(addresses) == 1 else None
        vlan = client.get("vlan")
        if address and vlan in VLANS and ipaddress.ip_address(address) in ipaddress.ip_network(f"192.168.{vlan}.0/24"):
            used.add(address)
        elif vlan in VLANS or vlan in (None, 0):
            block(vlan, f"{client.get('name') or client['mac']}: {client['reason']}")
    used.update(h["ip"] for h in o.load_hosts().values() if h.get("ip"))
    with contextlib.closing(_db()) as con:
        reservations = [dict(r) for r in con.execute("SELECT * FROM ip_reservations")]
    return {"as_of": o.iso(), "vlans": network.get("vlans", []), "used": sorted(used - {""}),
            "reservations": reservations,
            "layer2_transports": network.get("layer2_transports", []),
            "allocation_blockers": blockers, "retired_guest_clients": retired,
            "policy": "New-service candidates .36-.49 on VLAN 20/40/90 only; existing IPs/DHCP are unchanged. "
                      "DHCP, known/remembered clients, infrastructure and all controller reservations (including "
                      "disabled entries) are excluded. Confirmed internal mesh transport links are recorded "
                      "separately, with both managed AP IPs excluded. Unresolved clients/guests block their VLAN, except "
                      "Proxmox-MAC clients whose guest no longer exists in the cluster; unknown VLANs "
                      "block all allocation. No reply to ping alone is not proof."}


def available(vlan, data, service=""):
    if vlan not in VLANS:
        raise ValueError(f"VLAN {vlan} has no reviewed service allocation pool; ask Diallos for a design review")
    blockers = data.get("allocation_blockers", {})
    reasons = blockers.get("*", []) + blockers.get(str(vlan), [])
    if reasons:
        raise RuntimeError("IPAM cannot confirm availability: " + "; ".join(reasons))
    net = ipaddress.ip_network(f"192.168.{vlan}.0/24")
    config = next((n for n in data["vlans"] if n.get("vlan") == vlan), None)
    if not config:
        raise RuntimeError(f"No authoritative Omada network data for VLAN {vlan}")
    if config.get("dhcp") is None or not config.get("subnet"):
        raise RuntimeError("VLAN subnet/DHCP state is unverified; IPAM refuses allocation")
    if config.get("subnet") and ipaddress.ip_network(config["subnet"], strict=False) != net:
        raise RuntimeError("VLAN subnet differs from the reviewed design; manual IPAM review required")
    dhcp = config.get("dhcp_range")
    if config.get("dhcp") and (not dhcp or not dhcp.get("start") or not dhcp.get("end")):
        raise RuntimeError("DHCP is enabled but its range is unverified; IPAM refuses allocation")
    if dhcp and dhcp.get("start") and dhcp.get("end"):
        start, end = ipaddress.ip_address(dhcp["start"]), ipaddress.ip_address(dhcp["end"])
        if start not in net or end not in net or start > end:
            raise RuntimeError("DHCP range is inconsistent with the VLAN; manual IPAM review required")
    used = set(data["used"])
    used.update(r["ip"] for r in data["reservations"]
                if r["service"] != service and (r["state"] == "active" or r["expires"] >= o.iso()))
    options = []
    for host in POOL:
        candidate = str(net.network_address + host)
        if candidate in used:
            continue
        if dhcp and dhcp.get("start") and dhcp.get("end"):
            if ipaddress.ip_address(dhcp["start"]) <= ipaddress.ip_address(candidate) <= ipaddress.ip_address(dhcp["end"]):
                continue
        options.append(candidate)
    return options


def _responds(ip):
    result = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, timeout=5)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"IPAM reachability probe failed for {ip}")
    return result.returncode == 0


def reserve(service, vlan, requested="auto"):
    if not service or len(service) > 64:
        raise ValueError("A service name is required for IPAM")
    data = picture()
    options = available(vlan, data, service)
    old = next((r for r in data["reservations"] if r["service"] == service), None)
    if requested in ("", "auto", None) and old:
        requested = old["ip"]
    if requested not in ("", "auto", None):
        if requested not in options:
            raise ValueError(f"IP {requested} is used, outside the service pool, reserved or inside DHCP")
        options = [requested]
    for ip in options:
        if _responds(ip):
            continue
        with contextlib.closing(_db()) as con, con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM ip_reservations WHERE state='pending' AND expires<?", (o.iso(),))
            taken = con.execute("SELECT service FROM ip_reservations WHERE ip=?", (ip,)).fetchone()
            if taken and taken["service"] != service:
                continue
            con.execute("DELETE FROM ip_reservations WHERE service=? AND state='pending'", (service,))
            con.execute("INSERT OR IGNORE INTO ip_reservations VALUES(?,?,?,'pending',?,?)",
                        (ip, service, vlan, o.iso(), o.iso(o.utcnow() + dt.timedelta(hours=24))))
            stored = con.execute("SELECT * FROM ip_reservations WHERE service=?", (service,)).fetchone()
            if not stored or stored["ip"] != ip:
                raise RuntimeError("An active reservation prevents changing the service address")
            return dict(stored)
    raise RuntimeError(f"No available IP in the reviewed service pool for VLAN {vlan}")


def verify(service, ip, vlan, existing=False):
    with contextlib.closing(_db()) as con:
        row = con.execute("SELECT * FROM ip_reservations WHERE service=? AND ip=? AND vlan=?",
                          (service, ip, vlan)).fetchone()
    if not row or (row["state"] != "active" and row["expires"] < o.iso()):
        raise PermissionError("New services require a current Diallos/IPAM reservation; re-plan before approval")
    if not existing:
        if ip not in available(vlan, picture(), service) or _responds(ip):
            raise PermissionError("Reserved IP is no longer free; re-plan and re-approve")
    else:
        data = picture()
        data["used"] = [used for used in data["used"] if used != ip]
        if ip not in available(vlan, data, service):
            raise PermissionError("Existing service no longer meets the reviewed VLAN/DHCP design")
    return dict(row)


def release(service):
    """Free a decommissioned service's address once its guest is confirmed gone."""
    with contextlib.closing(_db()) as con, con:
        return con.execute("DELETE FROM ip_reservations WHERE service=?", (service,)).rowcount


def activate(service):
    with contextlib.closing(_db()) as con, con:
        updated = con.execute("UPDATE ip_reservations SET state='active' WHERE service=?", (service,))
        if not updated.rowcount:
            raise PermissionError("Cannot activate a missing IPAM reservation")
