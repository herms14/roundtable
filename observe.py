#!/usr/bin/env python3
"""Read-only Grafana + Glance access for Roundtable agents, filtered by layer boundary.

Grafana: list dashboards, open one and evaluate every panel's PromQL/LogQL live (instant values), so an agent can
explain "what the dashboard shows" without a browser. Glance: fetch a page's rendered widgets as text.
Each dashboard/page belongs to a layer (application/compute/network/storage or general); an agent only sees the
ones inside its scope (inventory.agent_scope). Auth failures are logged as access gaps (access.py).

CLI: python3 observe.py grafana [dashboard] [--panel x] [--agent id] | glance [page] [--agent id]
"""
import argparse
import base64
import concurrent.futures as cf
import html
import json
import os
import re
import time
import urllib.parse

import olympus_core as o

GRAFANA = os.environ.get("GRAFANA_URL", "http://192.168.40.13:3030")
GRAFANA_PUBLIC = "https://grafana.hrmsmrflrii.xyz"
GLANCE = os.environ.get("GLANCE_URL", "http://192.168.40.12:8080")
GLANCE_PUBLIC = "https://glance.hrmsmrflrii.xyz"
PROM = os.environ.get("PROM_URL", "http://192.168.40.13:9090")
LOKI = os.environ.get("LOKI_URL", "http://192.168.40.13:3100")

# dashboard uid / glance page keyword -> layer. Unknown dashboards default to "application".
DASH_LAYER = {"containers-modern": "application", "immich-host-health": "application",
              "network-utilization": "network", "omada-network": "network",
              "pbs-backup-status": "storage", "synology-nas-modern": "storage",
              "proxmox-cluster-health": "compute", "proxmox-compute": "compute"}
GLANCE_LAYER = {"home": "general", "overview": "general", "roundtable": "general", "news": "general",
                "network": "network", "compute": "compute",
                "storage": "storage", "backup": "storage", "media": "application", "services": "application"}
CACHE_S = 60
_cache = {}


def _scope(aid):
    import inventory
    return set(inventory.agent_scope(aid))


def _allowed(aid, layer):
    if not aid:
        return True
    sc = _scope(aid)
    return layer == "general" and bool(sc) or layer in sc


def _gap(aid, resource, err, why):
    try:
        import access
        access.from_error(aid or "watcher", resource, err, why=why, source="observe")
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- Grafana

def _gget(path):
    user, pw = os.environ.get("GRAFANA_USER"), os.environ.get("GRAFANA_PASS")
    h = {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()} if user else {}
    if os.environ.get("GRAFANA_TOKEN"):
        h = {"Authorization": f"Bearer {os.environ['GRAFANA_TOKEN']}"}
    code, d = o.http("GET", GRAFANA + path, h, timeout=20, retries=1)
    if code != 200:
        raise RuntimeError(f"Grafana HTTP {code} on {path}: {str(d)[:200]}")
    return d


def dashboards(aid=None):
    out = []
    for x in _gget("/api/search?type=dash-db&limit=200"):
        layer = DASH_LAYER.get(x["uid"], "application")
        if _allowed(aid, layer):
            out.append({"uid": x["uid"], "title": x["title"], "layer": layer, "url": GRAFANA_PUBLIC + x.get("url", "")})
    return out


def _subst(expr):
    expr = re.sub(r"\$__(rate_interval|interval|range|auto)\b|\$\{__(rate_interval|interval|range)\}", "5m", expr)
    expr = re.sub(r'(\w+)\s*=\s*"\$\{?\w+(:\w+)?\}?"', r'\1=~".+"', expr)    # label="$var" -> regex any
    expr = re.sub(r'(\w+)\s*=~\s*"\$\{?\w+(:\w+)?\}?"', r'\1=~".+"', expr)
    return re.sub(r"\$\{?\w+(:\w+)?\}?", ".+", expr)


def _prom(q):
    code, d = o.http("GET", f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": q}), timeout=20, retries=1)
    if code != 200 or not isinstance(d, dict) or d.get("status") != "success":
        raise RuntimeError(f"PromQL HTTP {code}")
    return d["data"]


def _loki(q):
    code, d = o.http("GET", f"{LOKI}/loki/api/v1/query?" + urllib.parse.urlencode({"query": q, "limit": 20}),
                     timeout=20, retries=1)
    if code != 200 or not isinstance(d, dict):
        raise RuntimeError(f"LogQL HTTP {code}")
    return d["data"]


def _fmt_val(v, unit):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if unit in ("bytes", "decbytes"):
        for u in ("B", "KB", "MB", "GB", "TB", "PB"):
            if abs(f) < 1024:
                return f"{f:.1f} {u}"
            f /= 1024
    if unit in ("percentunit",):
        return f"{f * 100:.1f}%"
    if unit in ("percent",):
        return f"{f:.1f}%"
    if unit in ("s", "dtdurations"):
        return f"{f / 86400:.1f} d" if f > 172800 else f"{f / 3600:.1f} h" if f > 7200 else f"{f:.0f} s"
    if unit in ("Bps", "binBps"):
        return _fmt_val(f, "bytes") + "/s"
    if unit in ("bps",):
        return f"{f / 1e6:.2f} Mbps"
    return f"{f:.4g}"


def _label(metric, legend):
    if legend and legend != "__auto":
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(metric.get(m.group(1), "")), legend)
    m = {k: v for k, v in metric.items() if k not in ("__name__", "job", "instance")} or metric
    return ", ".join(f"{k}={v}" for k, v in list(m.items())[:4]) or "value"


def _panels(dash):
    for p in dash.get("panels", []):
        yield p
        yield from p.get("panels", []) or []  # collapsed rows


def _eval_panel(p):
    unit = ((p.get("fieldConfig") or {}).get("defaults") or {}).get("unit", "")
    ds = json.dumps(p.get("datasource") or "").lower()
    out = {"title": p.get("title") or "(untitled)", "type": p.get("type"), "values": [], "queries": []}
    if p.get("type") in ("row", "text", "news", "dashlist"):
        if p.get("type") == "text":
            out["values"] = [re.sub(r"<[^>]+>", " ", ((p.get("options") or {}).get("content") or ""))[:300]]
        return out
    for t in (p.get("targets") or [])[:4]:
        q = t.get("expr")
        if not q:
            continue
        out["queries"].append(q)
        try:
            is_loki = "loki" in ds or "loki" in json.dumps(t.get("datasource") or "").lower()
            if is_loki:
                d = _loki(_subst(q))
                if d.get("resultType") == "streams":
                    out["values"] += [line[:200] for s in d["result"] for _, line in s["values"]][:8]
                    continue
            else:
                d = _prom(_subst(q))
            res = d.get("result") or []
            if d.get("resultType") == "scalar":
                res = [{"metric": {}, "value": res}]
            for r in res[:12]:
                out["values"].append(f"{_label(r.get('metric') or {}, t.get('legendFormat'))}: "
                                     f"{_fmt_val((r.get('value') or [0, '?'])[1], unit)}")
            if len(res) > 12:
                out["values"].append(f"... {len(res) - 12} more series")
            if not res:
                out["values"].append("(no data)")
        except Exception as e:  # noqa: BLE001
            out["values"].append(f"(query failed: {str(e)[:80]})")
    return out


def dashboard(name, aid=None, panel=None, max_panels=40):
    """Open a dashboard by uid or title fragment and evaluate its panels (current values)."""
    dl = dashboards(None)
    n = (name or "").lower()
    hit = next((d for d in dl if d["uid"] == n), None) or next((d for d in dl if n and n in d["title"].lower()), None)
    if not hit:
        return {"error": f"no dashboard matching '{name}'", "dashboards": [d["title"] for d in dashboards(aid)]}
    if not _allowed(aid, hit["layer"]):
        return {"error": f"'{hit['title']}' is a {hit['layer']}-layer dashboard, outside your boundary"}
    key = (hit["uid"], panel)
    if key in _cache and time.time() - _cache[key][0] < CACHE_S:
        return _cache[key][1]
    dash = _gget(f"/api/dashboards/uid/{hit['uid']}")["dashboard"]
    panels = [p for p in _panels(dash) if p.get("type") != "row"
              and (not panel or panel.lower() in (p.get("title") or "").lower())][:max_panels]
    with cf.ThreadPoolExecutor(8) as ex:
        evald = list(ex.map(_eval_panel, panels))
    res = {"dashboard": hit["title"], "layer": hit["layer"], "url": hit["url"],
           "time": o.local().strftime("%Y-%m-%d %H:%M"), "panels": evald,
           "note": "Values are current (instant) readings of each panel's queries; template variables = all."}
    _cache[key] = (time.time(), res)
    return res


def dashboard_text(res, limit=9000):
    if "error" in res:
        return json.dumps(res)
    lines = [f"Grafana dashboard '{res['dashboard']}' ({res['layer']} layer) at {res['time']} - {res['url']}"]
    for p in res["panels"]:
        lines.append(f"- {p['title']} [{p['type']}]: " + ("; ".join(p["values"][:12]) or "(no queries)"))
    t = "\n".join(lines)
    return t if len(t) <= limit else t[:limit] + "\n…(truncated; ask for a specific panel)"


# --------------------------------------------------------------------------- Glance

def glance_pages(aid=None):
    code, d = o.http("GET", GLANCE + "/", timeout=15, retries=1)
    if code != 200:
        raise RuntimeError(f"Glance HTTP {code}")
    page = d if isinstance(d, str) else ""
    out = []
    for slug in dict.fromkeys(re.findall(r'href="/([^"/.]+)"', page)):
        title = urllib.parse.unquote(slug)
        kw = re.sub(r"[^a-z]", "", title.lower())
        layer = next((v for k, v in GLANCE_LAYER.items() if k in kw), "general")
        if _allowed(aid, layer):
            out.append({"slug": slug, "title": title, "layer": layer})
    return out


def glance_page(name, aid=None, limit=6000):
    pages = glance_pages(None)
    n = re.sub(r"[^a-z]", "", (name or "home").lower())
    hit = next((p for p in pages if n and n in re.sub(r"[^a-z]", "", p["title"].lower())), None)
    if not hit:
        return {"error": f"no Glance page matching '{name}'", "pages": [p["title"] for p in glance_pages(aid)]}
    if not _allowed(aid, hit["layer"]):
        return {"error": f"Glance page '{hit['title']}' is {hit['layer']}-layer, outside your boundary"}
    code, d = o.http("GET", f"{GLANCE}/api/pages/{hit['slug']}/content/", timeout=30, retries=1)
    if code != 200:
        raise RuntimeError(f"Glance page HTTP {code}")
    t = d if isinstance(d, str) else json.dumps(d)
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ", t, flags=re.S)
    t = re.sub(r"</(div|li|tr|h\d|p)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = "\n".join(re.sub(r"[ \t]+", " ", html.unescape(x)).strip() for x in t.splitlines())
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return {"page": hit["title"], "layer": hit["layer"], "url": f"{GLANCE_PUBLIC}/{hit['slug']}",
            "content": t[:limit] + ("\n…(truncated)" if len(t) > limit else "")}


# --------------------------------------------------------------------------- tool entry points (voice / chat)

def grafana_tool(aid, name=None, panel=None):
    try:
        if not name:
            return {"dashboards": dashboards(aid), "note": "call again with a dashboard name to read its panels"}
        res = dashboard(name, aid, panel)
        return {"summary": dashboard_text(res)} if "error" not in res else res
    except Exception as e:  # noqa: BLE001
        _gap(aid, "Grafana API", e, "read dashboards and panel queries to explain them to Master Hermes")
        return {"error": f"Grafana unavailable: {str(e)[:200]}"}


def glance_tool(aid, name=None):
    try:
        if not name:
            return {"pages": glance_pages(aid), "note": "call again with a page name to read it"}
        return glance_page(name, aid)
    except Exception as e:  # noqa: BLE001
        _gap(aid, "Glance dashboard", e, "read the Glance pages to describe them to Master Hermes")
        return {"error": f"Glance unavailable: {str(e)[:200]}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["grafana", "glance"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--panel")
    ap.add_argument("--agent")
    a = ap.parse_args()
    if a.what == "grafana":
        r = grafana_tool(a.agent, a.name, a.panel)
        print(r.get("summary") or json.dumps(r, indent=1, ensure_ascii=False))
    else:
        r = glance_tool(a.agent, a.name)
        print(r.get("content") and f"{r['page']} ({r['url']})\n{r['content']}" or json.dumps(r, indent=1,
                                                                                          ensure_ascii=False))


if __name__ == "__main__":
    main()
