#!/usr/bin/env python3
"""Boc - OpenAI's actual month-to-date bill, so the portal, briefs and budget stop use real cost, not only the ledger.

  openai_costs.py sync        # read the Costs API (needs OPENAI_ADMIN_KEY, an OpenAI *admin* key); timer every 3 h
  openai_costs.py set 56.43   # type in the figure from platform.openai.com/usage when there is no admin key
  openai_costs.py show        # bill, ledger and what the portal counts

The run ledger misses Codex CLI use and anything before it started (2026-10-10), so it reads low. The bill is
stored in kv `openai_bill` {month, usd, at, source}; olympus_core.month_spend() adds what the ledger records after
`at`. Read-only towards OpenAI; nothing is changed there.
"""
import datetime as dt
import os
import sys
import urllib.parse

import olympus_core as o

AGENT = "cost"
API = "https://api.openai.com/v1/organization/costs"


def month_start():
    t = o.utcnow()
    return dt.datetime(t.year, t.month, 1, tzinfo=dt.timezone.utc)


def costs_api(key):
    """Month-to-date USD from the Costs API (daily buckets, all projects and line items)."""
    total, page = 0.0, None
    for _ in range(10):
        q = {"start_time": int(month_start().timestamp()), "bucket_width": "1d", "limit": 31}
        if page:
            q["page"] = page
        code, d = o.http("GET", f"{API}?{urllib.parse.urlencode(q)}", headers={"Authorization": f"Bearer {key}"},
                         timeout=30, retries=2)
        if code != 200 or not isinstance(d, dict):
            raise RuntimeError(f"Costs API HTTP {code}: {str(d)[:200]}")
        for b in d.get("data", []):
            for r in b.get("results", []):
                total += float((r.get("amount") or {}).get("value") or 0)
        if not d.get("has_more"):
            return total
        page = d.get("next_page")
    return total


def store(usd, source):
    o.kv_set("openai_bill", {"month": o.local().strftime("%Y-%m"), "usd": round(float(usd), 4), "at": o.iso(),
                             "source": source})


def show():
    b = o.openai_bill()
    print(f"OpenAI bill: {o.money(b['usd'])} ({b['source']}, read {b['at']})" if b else "OpenAI bill: unknown")
    print(f"Ledger this month: {o.money(o.ledger_spend())} · counted: {o.money(o.month_spend())} of "
          f"${o.budget_limit():.0f}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "set":
        store(float(sys.argv[2]), "typed in from the OpenAI usage page")
        show()
    elif cmd == "sync":
        o.load_env()
        key = os.environ.get("OPENAI_ADMIN_KEY")
        if not key:
            o.heartbeat(AGENT, "OpenAI bill: no OPENAI_ADMIN_KEY, keeping the last typed-in figure")
            return
        usd = costs_api(key)
        store(usd, "OpenAI Costs API")
        o.heartbeat(AGENT, f"OpenAI bill {o.money(usd)} month to date")
        show()
    else:
        show()


if __name__ == "__main__":
    main()
