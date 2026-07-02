#!/usr/bin/env python3
"""
Notion record + dedup store for the staking-ETF filing alerts.

Talks to the Notion REST API with an internal-integration token read from
NOTION_TOKEN. The target database is the Bitwise-workspace "Crypto Staking
ETF Filings" DB; its id defaults to DB_ID below but can be overridden with
NOTION_DB_ID.

  seen_filing_ids()  -> set of Filing IDs already recorded (for dedup)
  add_filing(record) -> create one row from a detect/enrich record

Usage:
  export NOTION_TOKEN=ntn_...
  python3 notion_store.py --selftest      # verify access + show row count
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# Personal-workspace "Crypto Staking ETF Filings" DB (interim record store).
# Override with NOTION_DB_ID to repoint at a Bitwise-owned DB once an admin
# provisions an integration there (Bitwise copy id: ef28a476fbc4836b9341818a838c66b4).
DB_ID = os.environ.get("NOTION_DB_ID", "f274f6ba6afb40219b7907baec1ba53a")


def _req(method, path, body=None):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise SystemExit("NOTION_TOKEN not set (put it in .env or export it).")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(f"Notion API {e.code}: {detail}")


def get_db():
    return _req("GET", f"/databases/{DB_ID}")


def seen_filing_ids():
    """Return the set of 'Filing ID' titles already in the DB (paginated)."""
    ids, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _req("POST", f"/databases/{DB_ID}/query", body)
        for row in res.get("results", []):
            title = row["properties"].get("Filing ID", {}).get("title", [])
            if title:
                ids.add(title[0]["plain_text"])
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return ids


STAKING_SIGNALS = {"New staking ETF", "ETF adding staking", "Approval milestone",
                   "Staking ETF — routine update"}


def issuer_history():
    """Map CIK -> {'ever_staking': bool} from existing rows, so classify() can
    detect an existing product newly adding staking. Reads Signal (new) or
    Relevance (legacy) or a staking-y issuer name."""
    hist, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = _req("POST", f"/databases/{DB_ID}/query", body)
        for row in res.get("results", []):
            p = row["properties"]
            cik = "".join(t.get("plain_text", "") for t in p.get("CIK", {}).get("rich_text", []))
            if not cik:
                continue
            name = "".join(t.get("plain_text", "") for t in p.get("Issuer", {}).get("rich_text", []))
            sig = (p.get("Signal", {}).get("select") or {}).get("name")
            rel = (p.get("Relevance", {}).get("select") or {}).get("name")
            staking = (sig in STAKING_SIGNALS) or (rel == "Staking") or ("stak" in name.lower())
            h = hist.setdefault(cik, {"ever_staking": False})
            h["ever_staking"] = h["ever_staking"] or staking
        if not res.get("has_more"):
            break
        cursor = res["next_cursor"]
    return hist


def query_filings(assets=None, since=None, limit=100):
    """Return recorded filings (newest first) for the ask-the-bot Q&A, optionally
    filtered by asset ticker(s) and/or a 'filed on-or-after' date. Each row is a
    compact dict: issuer, signal, assets, structure, milestone, filed, summary, link, url."""
    conds = []
    if assets:
        conds.append({"or": [{"property": "Assets", "multi_select": {"contains": a}} for a in assets]})
    if since:
        conds.append({"property": "Filed", "date": {"on_or_after": since}})
    body = {"page_size": min(limit, 100),
            "sorts": [{"property": "Filed", "direction": "descending"}]}
    if conds:
        body["filter"] = {"and": conds} if len(conds) > 1 else conds[0]
    res = _req("POST", f"/databases/{DB_ID}/query", body)

    def rt(p, k):
        return "".join(t.get("plain_text", "") for t in p.get(k, {}).get("rich_text", []))

    def sel(p, k):
        return (p.get(k, {}).get("select") or {}).get("name")

    rows = []
    for row in res.get("results", []):
        p = row["properties"]
        title = p.get("Filing ID", {}).get("title", [])
        rows.append({
            "filing_id": title[0]["plain_text"] if title else "",
            "issuer": rt(p, "Issuer"),
            "signal": sel(p, "Signal"),
            "assets": [o["name"] for o in p.get("Assets", {}).get("multi_select", [])],
            "structure": sel(p, "Structure"),
            "milestone": rt(p, "Form / Milestone"),
            "filed": (p.get("Filed", {}).get("date") or {}).get("start"),
            "summary": rt(p, "Summary"),
            "sponsor_fee": rt(p, "Sponsor fee"),
            "staking_fee": rt(p, "Staking fee"),
            "staking_provider": rt(p, "Staking provider"),
            "custodian": rt(p, "Custodian"),
            "listing_exchange": rt(p, "Listing exchange"),
            "pct_staked": rt(p, "% staked"),
            "link": p.get("Link", {}).get("url"),
            "url": row.get("url"),
        })
    return rows


def add_filing(r):
    """Create one DB row from an enriched record dict."""
    assets = r.get("assets") or []
    if isinstance(assets, str):
        assets = json.loads(assets)
    props = {
        "Filing ID": {"title": [{"text": {"content": r["filing_id"]}}]},
        "Issuer": {"rich_text": [{"text": {"content": r.get("issuer", "")}}]},
        "Assets": {"multi_select": [{"name": a} for a in assets]},
        "Signal": {"select": {"name": r["signal"]}},
        "Structure": {"select": {"name": r["structure"]}},
        "Stream": {"select": {"name": r["stream"]}},
        "Form / Milestone": {"rich_text": [{"text": {"content": r.get("milestone", "")}}]},
        "Filed": {"date": {"start": r["filed"]}},
        "Summary": {"rich_text": [{"text": {"content": r.get("summary", "")}}]},
        "Evidence": {"rich_text": [{"text": {"content": (r.get("evidence") or "")[:1900]}}]},
        "Link": {"url": r.get("link") or None},
        "CIK": {"rich_text": [{"text": {"content": r.get("cik", "")}}]},
    }
    for col, key in [("Sponsor fee", "sponsor_fee"), ("Staking fee", "staking_fee"),
                     ("Staking provider", "staking_provider"), ("Custodian", "custodian"),
                     ("Listing exchange", "listing_exchange"), ("% staked", "pct_staked")]:
        val = r.get(key) or ""
        if val:
            props[col] = {"rich_text": [{"text": {"content": val[:1900]}}]}
    if r.get("confidence"):
        props["Confidence"] = {"select": {"name": r["confidence"]}}
    return _req("POST", "/pages", {"parent": {"database_id": DB_ID}, "properties": props})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.parse_args()

    db = get_db()
    title = "".join(t.get("plain_text", "") for t in db.get("title", []))
    print(f"OK connected to DB: {title!r} (id {DB_ID})")
    print("Properties:", ", ".join(db.get("properties", {}).keys()))
    ids = seen_filing_ids()
    print(f"Existing rows (Filing IDs): {len(ids)}")
    for fid in list(ids)[:5]:
        print("  -", fid)


if __name__ == "__main__":
    main()
