#!/usr/bin/env python3
"""
Event-oriented classification for detected filings (rules-only, no LLM).

The question isn't "does this mention staking?" but "what event is this?":

  🟢 New staking ETF            initial registration of a product that stakes
  🔵 ETF adding staking         an existing product introducing staking (e.g. rename)
  🟣 Approval milestone         Stream B 19b-4 event for a staking ETF
  ⚪ Staking ETF — routine       known staking product, ordinary ongoing filing
  ⚫ Not a staking product       treasury/operating cos, non-staking spot ETFs
  🟡 Review                     metadata can't tell — needs the document read

Detecting "adding staking" needs memory of what we've seen before, so classify()
takes a `history` map (built from the Notion record). The genuinely hard case —
an existing spot ETF adding staking in an amendment *without* a rename — can't be
settled from metadata; it's routed to Review for a later document-read step.
"""

import re

# CIK -> (base_staking, structure, [assets]). base_staking: True/False/None(unknown).
CIK_MAP = {
    "0002054247": (False, "Single-asset ETF", ["DOT"]),   # 21Shares Polkadot
    "0002092446": (False, "Treasury company", ["AVAX"]),  # Avalanche Treasury Corp
    "0002057388": (False, "Single-asset ETF", ["SOL"]),   # Franklin Solana Trust
    "0002011535": (False, "Single-asset ETF", ["ETH"]),   # Franklin Ethereum Trust
    "0002060717": (False, "Single-asset ETF", ["AVAX"]),  # VanEck Avalanche
    "0002090011": (False, "Single-asset ETF", ["HYPE"]),  # 21Shares Hyperliquid
    "0002073616": (True,  "Single-asset ETF", ["INJ"]),   # Canary Staked INJ
    "0001896677": (True,  "Single-asset ETF", ["SOL"]),   # Grayscale Solana Staking
    "0002078856": (False, "Treasury company", ["HYPE"]),  # Hyperliquid Strategies
    "0001425355": (False, "Treasury company", ["SUI"]),   # SUI Group Holdings
    "0002064768": (True,  "Single-asset ETF", ["TRX"]),   # Canary Staked TRX
    "0002089855": (None,  "Basket / Multi-asset", ["Multi-asset"]),  # T. Rowe Price Active Crypto
    "0002103976": (False, "Single-asset ETF", ["ETH"]),   # Morgan Stanley Ethereum Trust
    "0002103547": (False, "Single-asset ETF", ["SOL"]),   # Morgan Stanley Solana Trust
    "0002063380": (False, "Single-asset ETF", ["SOL"]),   # Fidelity Solana Fund
    "0002025000": (False, "Single-asset ETF", ["NEAR"]),  # Grayscale Near Trust
    "0002035053": (True,  "Single-asset ETF", ["AVAX"]),  # Grayscale Avalanche Staking
    "0002099103": (True,  "Single-asset ETF", ["ETH"]),   # iShares Staked Ethereum
    "0002138284": (None,  "Single-asset ETF", []),        # Grayscale Canton
    "0002106762": (False, "Single-asset ETF", ["BNB"]),   # Grayscale BNB
    "0002107730": (True,  "Single-asset ETF", ["HYPE"]),  # Grayscale Hyperliquid Staking
    "0001723788": (None,  "Basket / Multi-asset", ["Multi-asset"]),  # Bitwise 10 Crypto Index
    "0001610853": (False, "Treasury company", ["SOL"]),   # Solana Co
    "0002066824": (False, "Single-asset ETF", ["BNB"]),   # VanEck BNB
    "0002028834": (False, "Single-asset ETF", ["SOL"]),   # 21Shares Solana
    "0001992508": (False, "Single-asset ETF", ["ETH"]),   # 21Shares Ethereum
    "0002061626": (False, "Single-asset ETF", ["SUI"]),   # 21Shares Sui
    "0001826397": (False, "Treasury company", ["AVAX"]),  # AVAX ONE TECHNOLOGY
    "0002031069": (None,  "Basket / Multi-asset", ["Multi-asset"]),  # Hashdex Nasdaq CME Crypto Index
}

FORM_LABEL = {
    "S-1": "initial registration (S-1)", "S-1/A": "amended registration (S-1/A)",
    "424B3": "prospectus (424B3)", "424B5": "prospectus supplement (424B5)",
    "POS AM": "post-effective amendment (POS AM)", "POS EX": "post-effective amendment (POS EX)",
    "EFFECT": "registration effectiveness (EFFECT)", "8-A12B": "share registration for listing (8-A)",
    "CERT": "exchange listing certification (CERT)", "8-K": "current report (8-K)",
    "10-Q": "quarterly report (10-Q)", "10-K": "annual report (10-K)", "N-1A": "fund registration (N-1A)",
}

SIGNAL_EMOJI = {
    "New staking ETF": "🟢", "ETF adding staking": "🔵", "Approval milestone": "🟣",
    "Staking ETF — routine update": "⚪️", "Not a staking product": "⚫️", "Review": "🟡",
}
# Every signal is posted to the channel (the full record). The colour distinguishes them.
ALERT_SIGNALS = set(SIGNAL_EMOJI)

ASSET_KW = [
    ("solana", "SOL"), ("ethereum", "ETH"), ("ether", "ETH"), ("hyperliquid", "HYPE"),
    ("avalanche", "AVAX"), ("polkadot", "DOT"), ("injective", "INJ"), ("cardano", "ADA"),
    ("tron", "TRX"), ("aptos", "APT"), ("cosmos", "ATOM"), ("celestia", "TIA"),
    ("near", "NEAR"), ("sui", "SUI"), ("aave", "AAVE"), ("bnb", "BNB"), ("xrp", "XRP"),
    ("litecoin", "LTC"), ("dogecoin", "DOGE"), ("hype", "HYPE"), ("sol", "SOL"), ("eth", "ETH"),
]
STAKING_RE = re.compile(r"stak(e|ing|ed)", re.I)
TREASURY_RE = re.compile(r"treasury|strategies|holdings|technology|\bco\b|\binc\b", re.I)
BASKET_RE = re.compile(r"index|basket|multi|crypto 5|10 crypto", re.I)
# Registration/lifecycle forms — where a genuinely new product first appears.
REGISTRATION_FORMS = {"S-1", "S-1/A", "N-1A", "424B3", "424B5", "424B4",
                      "8-A12B", "POS AM", "POS EX", "EFFECT", "CERT"}
# Periodic reports — an existing product's ongoing filings, never "new".
ROUTINE_FORMS = {"10-Q", "10-K", "8-K"}


def clean_issuer(name):
    return re.sub(r"\s*\(CIK\s*\d+\)\s*", "", name or "").strip()


def _guess_assets(name):
    low = name.lower()
    for kw, tok in ASSET_KW:
        if kw in low:
            return [tok]
    return []


def classify(f, history=None):
    """Return an enriched record with a `signal`. `history` maps CIK ->
    {'ever_staking': bool} built from the existing record (for 'adding staking')."""
    history = history or {}
    fid = f["filing_id"]
    issuer = clean_issuer(f.get("issuer", ""))

    if f.get("stream") == "B":
        return dict(filing_id=fid, issuer="VanEck JitoSOL ETF (Nasdaq)", assets=["SOL"],
                    signal="Approval milestone", structure="Single-asset ETF",
                    stream="B - 19b-4 (Fed Register)", milestone="Order Instituting Proceedings",
                    filed=f["filed"], link=f.get("link"), cik="", known=True,
                    summary=("SEC institutes proceedings on Nasdaq's rule change to list the VanEck "
                             "JitoSOL ETF (liquid-staking Solana) — a statutory approval milestone."))

    form = (f.get("form") or "").upper()
    milestone = FORM_LABEL.get(form, f.get("form") or "filing")
    cik = f.get("cik") or ""
    staking_named = bool(STAKING_RE.search(issuer))

    if cik in CIK_MAP:
        base_staking, struct, assets = CIK_MAP[cik]
        known = True
    else:
        base_staking, known = None, False
        assets = _guess_assets(issuer)
        struct = ("Basket / Multi-asset" if BASKET_RE.search(issuer)
                  else "Treasury company" if TREASURY_RE.search(issuer)
                  else "Single-asset ETF")

    is_treasury = struct == "Treasury company"
    is_staking = bool(base_staking) or staking_named
    seen_before = cik in history
    prior_staking = seen_before and history[cik].get("ever_staking")

    # --- decide the event signal ---
    if is_treasury and not staking_named:
        signal = "Not a staking product"
    elif is_staking:
        if not seen_before:
            # New only if it's a registration event; a first-seen periodic report
            # is just an existing product we hadn't recorded yet.
            signal = "New staking ETF" if form in REGISTRATION_FORMS else "Staking ETF — routine update"
        elif not prior_staking:
            signal = "ETF adding staking"          # rename / newly-staking on a known CIK
        else:
            signal = "Staking ETF — routine update"
    else:
        # a crypto product that doesn't assert staking in its name
        if form in REGISTRATION_FORMS:              # a registration/amendment could add staking
            signal = "Review"
        else:
            signal = "Not a staking product"

    summary = _summary(signal, issuer, milestone, known)
    return dict(filing_id=fid, issuer=issuer, assets=assets, signal=signal, structure=struct,
                stream="A - Issuer (EDGAR)", milestone=milestone, filed=f["filed"],
                summary=summary, link=f.get("link"), cik=cik, known=known)


def _summary(signal, issuer, milestone, known):
    review = "" if known else " (unrecognized issuer — confirm and add to the table)"
    base = {
        "New staking ETF": f"{issuer}: {milestone}. Looks like a NEW staking ETF registration{review}.",
        "ETF adding staking": f"{issuer}: {milestone}. Existing product appears to be ADDING staking{review}.",
        "Approval milestone": f"{issuer}: {milestone}.",
        "Staking ETF — routine update": f"{issuer}: {milestone}. Routine filing for a known staking ETF.",
        "Not a staking product": f"{issuer}: {milestone}. Does not stake — not a staking product.",
        "Review": (f"{issuer}: {milestone}. Crypto product, staking not stated in the name — "
                   f"needs a document read to tell if it adds staking{review}."),
    }
    return base[signal]


def alertable(signal):
    return signal in ALERT_SIGNALS
