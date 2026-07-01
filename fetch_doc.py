#!/usr/bin/env python3
"""
Fetch a filing's primary document and extract the text worth showing the LLM.

We never send the whole prospectus (an S-1 can be ~200K tokens). Instead we pull:
  - the product-description opening (first ~4K chars of body text), and
  - up to ~15 windows of +/-220 chars around each "stak" mention,
then cap the total. This keeps the LLM call at a few thousand tokens per filing.

Stream A docs live on sec.gov (need the descriptive User-Agent); Stream B docs are
the Federal Register raw-text URL captured in detect.py.

Usage (debug):
  python3 fetch_doc.py https://www.sec.gov/Archives/edgar/data/2103976/000110465926075838/tm2534146d4_s1a.htm
"""

import re
import sys
import urllib.request
import urllib.error

UA = "Bitwise Investments christy@bitwiseinvestments.com"
STAKING_RE = re.compile(r"stak(e|ing|ed)", re.I)
TAG_RE = re.compile(r"<[^>]+>")
ENT_RE = re.compile(r"&#?\w+;")
WS_RE = re.compile(r"\s+")

LEAD_CHARS = 4000        # product-description opening
WINDOW = 220             # chars each side of a "stak" hit
MAX_WINDOWS = 15
MAX_TOTAL = 24000        # hard cap on returned characters (~6K tokens)


def _fetch(url, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    import time
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1)); continue
            raise


def _to_text(raw):
    t = TAG_RE.sub(" ", raw)
    t = ENT_RE.sub(" ", t)
    return WS_RE.sub(" ", t).strip()


def extract(url):
    """Return (text_for_llm, staking_mentions) for a document URL, or ('', 0) if no doc."""
    if not url:
        return "", 0
    text = _to_text(_fetch(url))
    if not text:
        return "", 0

    mentions = len(STAKING_RE.findall(text))
    parts = [f"[DOCUMENT OPENING]\n{text[:LEAD_CHARS]}"]

    windows, used_spans = [], []
    for m in STAKING_RE.finditer(text):
        s, e = max(0, m.start() - WINDOW), min(len(text), m.start() + WINDOW)
        if any(s < us_e and e > us_s for us_s, us_e in used_spans):
            continue  # overlaps a window we already captured
        used_spans.append((s, e))
        windows.append(text[s:e].strip())
        if len(windows) >= MAX_WINDOWS:
            break

    if windows:
        parts.append("[STAKING MENTIONS IN CONTEXT]\n" + "\n---\n".join(windows))
    blob = "\n\n".join(parts)
    return blob[:MAX_TOTAL], mentions


def doc_text(record):
    """Convenience wrapper for a detect record: uses record['doc'], falls back to
    Stream B abstract/title if no document URL is available."""
    url = record.get("doc")
    try:
        text, mentions = extract(url) if url else ("", 0)
    except Exception as e:  # noqa: BLE001 — fetch is best-effort; caller decides fallback
        return f"[document fetch failed: {e}]", 0
    if not text:
        # Fall back to whatever metadata we have (esp. Stream B).
        text = (record.get("title") or "") + "\n" + (record.get("abstract") or "")
    return text, mentions


if __name__ == "__main__":
    t, n = extract(sys.argv[1])
    print(f"staking mentions: {n}")
    print(f"extracted chars: {len(t)}")
    print(t[:2000])
