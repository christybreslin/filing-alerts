#!/usr/bin/env python3
"""
Document-aware categorisation of a detected filing, via the Anthropic API.

Reads the filing's text (fetch_doc) and asks Claude the two ultimate questions —
"is this a NEW staking ETF?" and "is an existing ETF ADDING staking?" — returning
the same event `signal` the rules use, plus a one-line summary, an evidence quote,
and a confidence. Falls back to the rules (`classify.py`) when the API is
unavailable or errors, so `run.py` always gets a usable record.

Env: ANTHROPIC_API_KEY (required for the LLM path), CATEGORIZER_MODEL (default
claude-sonnet-4-6).
"""

import json
import os
import sys

import classify as clf
import fetch_doc

MODEL = os.environ.get("CATEGORIZER_MODEL", "claude-sonnet-4-6")

SIGNALS = ["New staking ETF", "ETF adding staking", "Approval milestone",
           "Staking ETF — routine update", "Not a staking product", "Review"]
STRUCTURES = ["Single-asset ETF", "Basket / Multi-asset", "Treasury company", "Other"]

SYSTEM = """You classify SEC filings for a crypto **staking** ETF monitoring system. \
You are given a filing's metadata and extracted text from the filing document itself. \
Answer the two questions that matter:

  1. Is this a NEW staking ETF? (an initial/registration filing for a product that will stake its assets)
  2. Is an EXISTING ETF ADDING staking? (a product that did not stake before now introducing it)

Assign exactly one `signal`:
- "New staking ETF": a registration-stage filing (S-1/S-1-A/424B/N-1A/POS/8-A/EFFECT/CERT) for a product that stakes, and this product is new to us (issuer not seen before / first registration).
- "ETF adding staking": an EXISTING product (issuer seen before, or clearly already trading) whose filing shows it is introducing staking — e.g. a name change to add "Staked/Staking", or prospectus language adding a staking feature to a product that previously did not stake.
- "Approval milestone": a Stream B 19b-4 exchange rule filing (Notice / Order Instituting Proceedings / Approval / Disapproval) concerning a staking ETF.
- "Staking ETF — routine update": a known staking product's ordinary ongoing filing (10-Q/10-K/prospectus) with no new-product or newly-added-staking event.
- "Not a staking product": a crypto treasury/operating company, or a spot ETF that does NOT stake and shows no sign of adding it.
- "Review": genuinely cannot tell from the provided text.

Rules:
- Ground the decision in the DOCUMENT text, not just the name. A product whose name omits "staking" can still be a staking product (read the text).
- `is_staking` = does this product stake (or intend to stake) its underlying crypto?
- `evidence_quote`: copy the single most decisive sentence/phrase FROM THE PROVIDED TEXT that justifies your call. If nothing in the text is decisive, quote the strongest available and lower confidence.
- `summary`: one sentence a analyst can act on (what the filing is + why it matters).
- Use the HISTORY hint to distinguish "New" (unseen) from "adding" (seen-before, previously non-staking) from "routine" (seen-before, already staking).
- `assets`: crypto tickers involved (e.g. ETH, SOL, BNB, HYPE); [] if none/basket-wide (use "Multi-asset" for broad baskets).
- Split the product identity into its parts:
  · `sponsor`: the issuer/brand behind the product (e.g. '21Shares', 'Grayscale', 'VanEck', 'Bitwise', 'Morgan Stanley'). For a 19b-4 exchange filing this is the fund's sponsor named in the rule change, NOT the exchange. For a crypto treasury/operating company, use the company name.
  · `product`: the product name WITHOUT the sponsor prefix (e.g. 'Polkadot ETF', 'Solana Staking ETF', 'Ethereum Trust'). Do not repeat the sponsor here.
  · `ticker`: the exchange ticker symbol if the document states one (e.g. 'TDOT', 'GSOL'); blank if none is stated yet.
- ALSO extract these details WHEN the document states them (leave blank, never guess): sponsor/management fee, staking fee or the sponsor's cut of staking rewards, staking provider(s)/validators, custodian(s), listing exchange, and the portion of assets staked. A routine report may not restate them — that's fine, leave blank."""

TOOL = {
    "name": "record_verdict",
    "description": "Record the classification verdict for this filing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_name": {"type": "string",
                             "description": "The ETF/fund/trust this filing concerns (e.g. 'Morgan Stanley Ethereum Trust', 'VanEck JitoSOL ETF'). For a 19b-4 exchange filing this is the fund named in the rule change, NOT the exchange."},
            "sponsor": {"type": "string", "description": "Issuer/brand behind the product (e.g. '21Shares', 'Grayscale'). For a treasury/operating company, the company name."},
            "product": {"type": "string", "description": "Product name without the sponsor prefix (e.g. 'Polkadot ETF', 'Solana Staking ETF')."},
            "ticker": {"type": "string", "description": "Exchange ticker if stated (e.g. 'TDOT'); empty otherwise."},
            "signal": {"type": "string", "enum": SIGNALS},
            "is_staking": {"type": "boolean"},
            "structure": {"type": "string", "enum": STRUCTURES},
            "assets": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "evidence_quote": {"type": "string"},
            "confidence": {"type": "string", "enum": ["High", "Medium", "Low"]},
            "sponsor_fee": {"type": "string", "description": "Annual sponsor/management fee exactly as stated (e.g. '0.19%'). Empty if the document doesn't state it."},
            "staking_fee": {"type": "string", "description": "Staking fee or the sponsor's cut of staking rewards, if stated (e.g. '7%'). Empty otherwise."},
            "staking_provider": {"type": "string", "description": "Staking provider(s)/validator(s) named, comma-separated (e.g. 'Coinbase, Figment'). Empty otherwise."},
            "custodian": {"type": "string", "description": "Crypto custodian(s) named. Empty otherwise."},
            "listing_exchange": {"type": "string", "description": "Listing exchange if stated (e.g. 'NYSE Arca', 'Nasdaq', 'Cboe BZX'). Empty otherwise."},
            "pct_staked": {"type": "string", "description": "Portion of assets to be staked if stated (e.g. 'up to 100%'). Empty otherwise."},
        },
        "required": ["product_name", "signal", "is_staking", "structure", "assets", "summary",
                     "evidence_quote", "confidence"],
    },
}

_client = None


def _anthropic():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    return _client


def _stream_notion(stream_letter):
    return "B - 19b-4 (Fed Register)" if stream_letter == "B" else "A - Issuer (EDGAR)"


def _rules_record(f, history, note, confidence="Low"):
    """Adapt classify.classify() output into the enriched record shape run.py expects."""
    rec = clf.classify(f, history)
    rec["evidence"] = note
    rec["confidence"] = confidence
    return rec


def categorize(f, history=None):
    """Return an enriched record: filing_id, issuer, assets, signal, structure, stream,
    milestone, filed, summary, link, cik, evidence, confidence, known."""
    history = history or {}

    # Cheap pre-filter: skip the document read + LLM for obvious treasury/operating
    # companies (rules are high-confidence here) to save tokens.
    pre = clf.classify(f, history)
    if pre["signal"] == "Not a staking product" and pre["structure"] == "Treasury company":
        pre["evidence"] = "Crypto treasury/operating company (rules pre-filter — no document read)."
        pre["confidence"] = "High"
        return pre

    # No key → rules fallback for everything.
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _rules_record(f, history, "(rules fallback — ANTHROPIC_API_KEY not set)")

    issuer = clf.clean_issuer(f.get("issuer", ""))
    cik = f.get("cik") or ""
    milestone = _milestone(f)
    seen_before = cik in history
    prior_staking = seen_before and history[cik].get("ever_staking")

    try:
        text, mentions = fetch_doc.doc_text(f)
        user = (
            f"FILING METADATA\n"
            f"- issuer/registrant: {issuer or f.get('issuer')}\n"
            f"- form: {f.get('form')}  | stream: {'B (19b-4 exchange rule filing)' if f.get('stream')=='B' else 'A (issuer registration/report)'}\n"
            f"- filed: {f.get('filed')}\n"
            f"- HISTORY: issuer seen before = {seen_before}; previously classified as staking = {bool(prior_staking)}\n"
            f"- staking-word mentions in document = {mentions}\n\n"
            f"FILING TEXT (extracted)\n{text}"
        )
        client = _anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "record_verdict"},
            messages=[{"role": "user", "content": user}],
        )
        v = next(b.input for b in resp.content if b.type == "tool_use")
        _log_usage(f.get("filing_id"), resp.usage)
    except Exception as e:  # noqa: BLE001 — fail soft to the rules
        print(f"  LLM categorize failed for {f.get('filing_id')}: {e}", file=sys.stderr)
        return _rules_record(f, history, f"(rules fallback — LLM error: {e})")

    return {
        "filing_id": f["filing_id"],
        "issuer": issuer or f.get("issuer", ""),
        "product_name": v.get("product_name") or issuer or f.get("issuer", ""),
        "sponsor": v.get("sponsor", ""),
        "product": v.get("product", ""),
        "ticker": v.get("ticker", ""),
        "assets": v.get("assets") or [],
        "signal": v["signal"],
        "structure": v.get("structure", "Other"),
        "stream": _stream_notion(f.get("stream")),
        "milestone": milestone,
        "filed": f["filed"],
        "summary": v.get("summary", ""),
        "link": f.get("link"),
        "cik": cik,
        "evidence": v.get("evidence_quote", ""),
        "confidence": v.get("confidence", "Medium"),
        "sponsor_fee": v.get("sponsor_fee", ""),
        "staking_fee": v.get("staking_fee", ""),
        "staking_provider": v.get("staking_provider", ""),
        "custodian": v.get("custodian", ""),
        "listing_exchange": v.get("listing_exchange", ""),
        "pct_staked": v.get("pct_staked", ""),
        "known": cik in clf.CIK_MAP,
    }


def _milestone(f):
    if f.get("stream") == "B":
        title = (f.get("title") or "").lower()
        for kw, label in [("order instituting", "Order Instituting Proceedings"),
                          ("granting", "Order Granting Approval"),
                          ("disapprov", "Order Disapproving"),
                          ("notice of filing", "Notice of Filing"),
                          ("immediate effectiveness", "Notice (Immediately Effective)")]:
            if kw in title:
                return label
        return "19b-4 rule filing"
    return clf.FORM_LABEL.get((f.get("form") or "").upper(), f.get("form") or "filing")


def _log_usage(fid, u):
    try:
        print(f"  usage {fid}: in={u.input_tokens} out={u.output_tokens} "
              f"cache_read={getattr(u,'cache_read_input_tokens',0)} "
              f"cache_write={getattr(u,'cache_creation_input_tokens',0)}", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# expose these so run.py can keep using them
SIGNAL_EMOJI = clf.SIGNAL_EMOJI
alertable = clf.alertable


if __name__ == "__main__":
    # Debug: categorize a single filing by re-detecting a narrow window.
    import detect
    fid = sys.argv[1] if len(sys.argv) > 1 else None
    fs = detect.filter_edgar(detect.fetch_edgar("2026-05-15", "2026-07-01")) + detect.fetch_fedreg("2026-05-15", "2026-07-01")
    target = next((x for x in fs if x["filing_id"] == fid), fs[0]) if fid else fs[0]
    print(json.dumps(categorize(target, {}), indent=2, ensure_ascii=False))
