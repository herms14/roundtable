#!/usr/bin/env python3
"""Configure Technitium DNS on homelab-dns01 (192.168.90.60) to match, then replace, Pi-hole as primary DNS.

Run on homelab-dns01 as root:  sudo python3 technitium_setup.py [--check]
Reads the API token from /root/technitium-admin.secret (never printed). Idempotent: re-running converges.

- Recursive resolver (no forwarders), DNSSEC validation, QNAME minimisation, serve-stale, 100k cache.
- Blocking through the Advanced Blocking app: Pi-hole's 4 lists, exact denies, regex denies and allowlist.
- hrmsmrflrii.xyz is a Conditional Forwarder zone (this-server): the local A records below answer with the
  Traefik address; every other name in the zone is resolved publicly, exactly like Pi-hole's local hosts did.
- "lan" primary zone for the short names Pi-hole served.
- Query Logs (Sqlite) app for the query log.
"""
import json
import sys
import urllib.parse
import urllib.request

API = "http://127.0.0.1:5380/api/"
SECRET = "/root/technitium-admin.secret"
TRAEFIK = "192.168.40.20"
ZONE = "hrmsmrflrii.xyz"
# Pi-hole dns.hosts on 2026-10-18 (all via Traefik unless noted) + node03 (route existed, record was missing)
# + pihole (Pi-hole answered it from its own hostname, not dns.hosts; now via Traefik like the others)
# + bao (OpenBao UI on openbao-lxc CT213, 2026-10-18)
# + nas (Synology DSM, 2026-10-19)
TRAEFIK_NAMES = """proxmox node01 node02 node03 pbs traefik auth photos gitlab jellyfin radarr sonarr lidarr prowlarr bazarr
overseerr jellyseerr tdarr autobrr deluge sabnzbd glance grafana olympus roundtable uptime prometheus n8n speedtest
bentopdf edgeshark resume jaeger demo notebook paperless lagident karakeep wizarr tracearr feeds ha metube omada
chronicle power homepage vlanflow study dns pihole bao nas""".split()
OTHER_A = {"analytics": "192.168.80.10"}
LAN = {"pihole": "192.168.90.53", "homelab-dns01": "192.168.90.60", "FS01": "192.168.80.4", "FS02": "192.168.80.5"}
STORE = {"Advanced Blocking": "https://download.technitium.com/dns/apps/AdvancedBlockingApp-v11.2.1.zip",
         "Query Logs (Sqlite)": "https://download.technitium.com/dns/apps/QueryLogsSqliteApp-v9.1.3.zip"}
HOSTS_LISTS = ["https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"]
ADBLOCK_LISTS = ["https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/fake.txt",
                 "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adguard/dns-rebind-protection.txt",
                 "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/pro.plus.txt"]
BLOCKED = ["pornhub.com", "www.temu.com"]
BLOCKED_REGEX = [
    r"(\.|^)pornhub\.com$",
    r"(\.|^)(porn|xxx|sex|adult|nsfw|hentai|xnxx|xvideos|xhamster|redtube|youporn|tube8|spankbang|xnxx|beeg|brazzers|bangbros|realitykings|naughtyamerica|mofos|fakehub|teamskeet|vixen|blacked|tushy|deeper|slayed|milf|threesome|creampie|cumshot|gangbang|anal|bdsm|fetish|escort|camgirl|livejasmin|chaturbate|stripchat|bongacams|camsoda|myfreecams|onlyfans|fansly|manyvids)",
    r"(\.|^)(pornhub|xvideos|xnxx|xhamster|redtube|youporn|spankbang|eporner|drtuber|sunporno|txxx|hclips|hdzog|upornia|vjav|javhd|jav|r18|dmm\.co\.jp|fanza|daftsex|sxyprn|heavy-r|efukt|motherless)",
    r"(\.|^)(18|nude|naked|erotic|sensual|lustful|horny|slutty|whore|hooker|pussy|cock|dick|tits|boobs|ass|butt|f[u]ck|fap|masturbat|orgasm|dildo|vibrator|fleshlight)",
    r"(\.|^)(temu|temuapp|temulive)",
    r"(\.|^)(shein|sheinsz|sheinside|romwe|zaful|banggood|gearbest|tomtop|geekbuying|lightinthebox|miniinthebox|sammydress|rosegal|dresslily|newchic|tidebuy|ericdress|tbdress|milanoo|jollychic)",
    r"(\.|^)(wish|wishapp|wishpost|patpat|cider|halara|urbanic|cupshe|emmiol|chicme|floryday|fairyseason|fashionmia|rotita|stylewe|metisu|popreal)",
    r"(\.|^)(aliexpress|alibaba|alipay|alicdn|aliapp|1688|taobao|tmall|alimama|aliyun|aliyuncs)",
    r"(\.|^)(dhgate|made-in-china|globalsources|hktdc|tradekey|tradeindia|indiamart|alibaba-inc)",
    r"(\.|^)(pinduoduo|pdd|jd|jingdong|vip|vipshop|kaola|yanxuan|mogujie|meilishuo|jumei|beibei|xiaohongshu|dewu|poizon)",
]
ALLOWED = ["msit.powerbi.com", "analytics.hrmsmrflrii.xyz", "app.fabric.microsoft.com", "app.powerbi.com",
           "functional.events.data.microsoft.com", "teams.events.data.microsoft.com",
           "us-v20.events.data.microsoft.com", "dc.services.visualstudio.com", "http-intake.logs.us5.datadoghq.com",
           "mobile.events.data.microsoft.com", "api.powerbi.com",
           "wabi-west-us3-a-primary-redirect.analysis.windows.net", "assets-proxy.anthropic.com",
           "us-v20.events.endpoint.security.microsoft.com"]


def token():
    for line in open(SECRET):
        if line.startswith("TECHNITIUM_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no API token in " + SECRET)


TOKEN = token()


def call(path, **params):
    data = urllib.parse.urlencode(dict(params, token=TOKEN)).encode()
    with urllib.request.urlopen(urllib.request.Request(API + path, data=data), timeout=900) as r:
        d = json.load(r)
    if d.get("status") != "ok":
        raise RuntimeError(f"{path}: {d.get('errorMessage') or d.get('status')}")
    return d.get("response") or {}


def apps():
    have = {a["name"] for a in call("apps/list").get("apps", [])}
    for name, url in STORE.items():
        if name not in have:
            call("apps/downloadAndInstall", name=name, url=url)
            print("installed app", name)


def settings():
    call("settings/set", dnsServerDomain="homelab-dns01.hrmsmrflrii.xyz", recursion="AllowOnlyForPrivateNetworks",
         dnssecValidation="true", qnameMinimization="true", serveStale="true", preferIPv6="false",
         cacheMaximumEntries="100000", enableBlocking="false", logQueries="true", udpPayloadSize="1232",
         forwarders="")
    print("settings ok (built-in blocking off: Advanced Blocking does it)")


def blocking():
    cfg = {
        "enableBlocking": True, "blockListUrlUpdateIntervalHours": 24,
        "networkGroupMap": {"0.0.0.0/0": "default", "[::]/0": "default"},
        "groups": [{
            "name": "default", "enableBlocking": True, "allowTxtBlockingReport": True, "blockAsNxDomain": False,
            "blockingAddresses": ["0.0.0.0", "::"],
            "allowed": ALLOWED, "blocked": BLOCKED, "allowListUrls": [], "blockListUrls": HOSTS_LISTS,
            "allowedRegex": [], "blockedRegex": BLOCKED_REGEX, "regexAllowListUrls": [], "regexBlockListUrls": [],
            "adblockListUrls": ADBLOCK_LISTS}]}
    call("apps/config/set", name="Advanced Blocking", config=json.dumps(cfg, indent=2))
    print("advanced blocking ok:", len(HOSTS_LISTS) + len(ADBLOCK_LISTS), "lists,", len(BLOCKED_REGEX), "regex,",
          len(ALLOWED), "allowed")


def zones():
    have = {z["name"]: z["type"] for z in call("zones/list").get("zones", [])}
    if ZONE not in have:
        call("zones/create", zone=ZONE, type="Forwarder", forwarder="this-server", dnssecValidation="true")
        print("zone", ZONE, "created (Forwarder -> this-server)")
    if "lan" not in have:
        call("zones/create", zone="lan", type="Primary")
        print("zone lan created")
    for zone in (ZONE, "lan"):  # read-only view for the monitoring user (exporter, dnssync.py on CT211)
        call("zones/permissions/set", zone=zone, userPermissions="admin|true|true|true|monitor|true|false|false",
             groupPermissions="Administrators|true|true|true")
    want = {f"{n}.{ZONE}": TRAEFIK for n in TRAEFIK_NAMES} | {f"{n}.{ZONE}": ip for n, ip in OTHER_A.items()}
    for zone, records in ((ZONE, want), ("lan", {f"{n.lower()}.lan": ip for n, ip in LAN.items()})):
        cur = {}
        for r in call("zones/records/get", domain=zone, zone=zone, listZone="true").get("records", []):
            if r["type"] == "A":
                cur.setdefault(r["name"].lower(), set()).add(r["rData"]["ipAddress"])
        added = 0
        for name, ip in records.items():
            if cur.get(name) == {ip}:
                continue
            call("zones/records/add", zone=zone, domain=name, type="A", ipAddress=ip, ttl="300", overwrite="true")
            added += 1
        print(f"zone {zone}: {len(records)} A records ({added} written)")


def check():
    s = call("settings/get")
    print("version", s["version"], "| recursion", s["recursion"], "| dnssec", s["dnssecValidation"],
          "| built-in blocking", s["enableBlocking"], "| query log", s["logQueries"])
    print("apps:", [a["name"] for a in call("apps/list").get("apps", [])])
    print("zones:", [(z["name"], z["type"]) for z in call("zones/list").get("zones", []) if not z.get("internal")])


if __name__ == "__main__":
    if "--check" not in sys.argv:
        apps()
        settings()
        blocking()
        zones()
    check()
