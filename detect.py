#!/usr/bin/env python3
"""
Crypto Staking ETF Filing Alerts — detection core.

Fetches the two filing streams described in the PRD, applies the Section 9
relevance filter, and emits a deduped JSON list of candidate filings on stdout.

  Stream A — EDGAR full-text search (issuer registration/lifecycle filings)
  Stream B — Federal Register API (exchange 19b-4 rule filings)

This is detection only. Categorization + summary (the lightweight LLM pass),
dedup against the Notion "seen" store, and Slack delivery happen downstream.

Usage:
  python3 detect.py --start 2026-05-15 --end 2026-07-01
  python3 detect.py --days 45          # rolling window ending today (needs --today)
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

UA = "Bitwise Investments christy@bitwiseinvestments.com"

# Section 9.2 — issuer form whitelist (matched against EDGAR root_forms / form).
FORM_WHITELIST_PREFIXES = (
    "S-1", "424B", "POS AM", "POS EX", "EFFECT",
    "8-A12B", "CERT", "8-K", "10-Q", "10-K", "N-1A",
)
# Section 9.3 — holder/ownership forms to drop even if they match "staking".
FORM_BLACKLIST = ("13F", "SCHEDULE 13G", "SC 13G", "13G", "SCHEDULE 13D", "SC 13D")
# Section 9.4 — SIC codes where the ETF trusts sit.
SIC_WHITELIST = {"6221", "6199"}
# Section 9.5 — a crypto asset name must co-occur with the staking term.
ASSET_TERMS = (
    "eth", "ether", "ethereum", "sol", "solana", "bnb", "hype", "hyperliquid",
    "near", "aave", "trx", "tron", "cro", "cronos", "inj", "injective",
    "canton", "avax", "avalanche", "dot", "polkadot", "ada", "cardano",
    "sui", "apt", "aptos", "atom", "cosmos", "tia", "celestia", "crypto",
    "digital asset", "staking", "staked",
)
STAKING_RE = re.compile(r"stak(e|ing|ed)", re.I)


def _get(url, retries=4):
    # urllib doesn't auto-decompress, so ask for identity encoding.
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # EDGAR intermittently 500s / 503s; retry transient server errors with backoff.
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise


# ---- Stream A: EDGAR full-text search --------------------------------------

def fetch_edgar(start, end, query="staking"):
    """Paginate EDGAR FTS, return raw exhibit-level hits."""
    base = "https://efts.sec.gov/LATEST/search-index"
    hits, frm, PAGE = [], 0, 100  # EDGAR FTS returns up to 100 hits/page; `from` must step by PAGE.
    while True:
        params = {"q": f'"{query}"', "startdt": start, "enddt": end}
        if frm:
            params["from"] = frm
        url = f"{base}?{urllib.parse.urlencode(params)}"
        data = _get(url)
        page = data.get("hits", {}).get("hits", [])
        hits.extend(page)
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        frm += PAGE
        if frm >= total or not page:
            break
        time.sleep(0.2)  # well under the 10 req/s SEC cap
    return hits


def filter_edgar(hits):
    """Apply Section 9 rules 1-5. Returns deduped list keyed by accession."""
    by_adsh = {}
    for h in hits:
        s = h.get("_source", {})
        adsh = s.get("adsh")
        if not adsh:
            continue
        form = (s.get("form") or "").upper()
        names = " ".join(s.get("display_names") or [])
        sics = set(s.get("sics") or [])

        # 9.3 — drop holder/ownership filings.
        if any(form.startswith(b) for b in FORM_BLACKLIST):
            continue
        # 9.2 — keep only whitelisted issuer forms.
        if not any(form.startswith(p) for p in FORM_WHITELIST_PREFIXES):
            continue
        # 9.4 — SIC gate (commodity brokers / finance services).
        if sics and not (sics & SIC_WHITELIST):
            continue
        # 9.5 — require a crypto-asset term in the issuer/product name.
        if not any(t in names.lower() for t in ASSET_TERMS):
            continue

        # 9.1 — collapse many exhibit rows to one filing per accession.
        if adsh not in by_adsh:
            cik = (s.get("ciks") or ["0"])[0]
            cikn, adshn = cik.lstrip("0"), adsh.replace("-", "")
            # EDGAR FTS `_id` is "<accession>:<primary-document-filename>".
            _id = h.get("_id", "")
            fname = _id.split(":", 1)[1] if ":" in _id else ""
            by_adsh[adsh] = {
                "filing_id": adsh,
                "stream": "A",
                "issuer": (s.get("display_names") or ["?"])[0],
                "cik": cik,
                "form": s.get("form"),
                "filed": s.get("file_date"),
                "sic": next(iter(sics), None),
                "link": f"https://www.sec.gov/Archives/edgar/data/{cikn}/{adshn}/{adsh}-index.htm",
                "doc": f"https://www.sec.gov/Archives/edgar/data/{cikn}/{adshn}/{fname}" if fname else None,
            }
    return list(by_adsh.values())


# ---- Stream B: Federal Register (19b-4 SRO rule filings) -------------------

def fetch_fedreg(start, end, term="staking"):
    base = "https://www.federalregister.gov/api/v1/documents.json"
    params = [
        ("conditions[agencies][]", "securities-and-exchange-commission"),
        ("conditions[term]", term),
        ("conditions[publication_date][gte]", start),
        ("conditions[publication_date][lte]", end),
        ("order", "newest"),
        ("per_page", "100"),
        ("fields[]", "document_number"),
        ("fields[]", "title"),
        ("fields[]", "publication_date"),
        ("fields[]", "html_url"),
        ("fields[]", "abstract"),
        ("fields[]", "raw_text_url"),
    ]
    url = f"{base}?{urllib.parse.urlencode(params)}"
    data = _get(url)
    out = []
    for d in data.get("results", []):
        title = d.get("title") or ""
        # Keep only Self-Regulatory Organization rule filings (Stream B).
        if not title.startswith("Self-Regulatory Organizations"):
            continue
        # Crypto relevance: staking term OR a known asset term in title/abstract.
        blob = f"{title} {d.get('abstract') or ''}".lower()
        if not (STAKING_RE.search(blob) or any(t in blob for t in ASSET_TERMS)):
            continue
        out.append({
            "filing_id": d.get("document_number"),
            "stream": "B",
            "issuer": title.split(";")[1].strip() if ";" in title else "SRO",
            "form": "19b-4",
            "filed": d.get("publication_date"),
            "title": title,
            "abstract": d.get("abstract"),
            "link": d.get("html_url"),
            "doc": d.get("raw_text_url"),
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--query", default="staking")
    args = ap.parse_args()

    stream_a = filter_edgar(fetch_edgar(args.start, args.end, args.query))
    stream_b = fetch_fedreg(args.start, args.end, args.query)

    result = {
        "window": {"start": args.start, "end": args.end},
        "counts": {"stream_a": len(stream_a), "stream_b": len(stream_b)},
        "filings": sorted(stream_a + stream_b, key=lambda f: f.get("filed") or "", reverse=True),
    }
    json.dump(result, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
