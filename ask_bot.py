#!/usr/bin/env python3
"""
Ask-the-bot: @-mention the SEC Filing Alerts bot with a question and it answers
from the tracked Notion filing record.

  @SEC Filing Alerts tell me about current ETH staking ETFs

Runs as a Slack Bolt app in Socket Mode (no public URL needed — dials out to Slack).
Env: SLACK_BOT_TOKEN (xoxb), SLACK_APP_TOKEN (xapp), ANTHROPIC_API_KEY, NOTION_TOKEN,
NOTION_DB_ID; optional ANSWER_MODEL (default claude-sonnet-4-6).

Local run:  .venv/bin/python ask_bot.py
"""
import datetime as dt
import os
import re

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import classify as clf
import notion_store

MODEL = os.environ.get("ANSWER_MODEL", "claude-haiku-4-5")
_anthropic = anthropic.Anthropic()

SYSTEM = """You are an analyst assistant inside a Bitwise Slack channel that tracks SEC \
filings for crypto staking ETFs. Answer the user's question using ONLY the filing records \
provided — our tracked database — by calling the `compose_answer` tool.

- Each record carries a STAGE field computed from its full filing history. TRUST it — do not
  re-derive live-vs-registration yourself. Map STAGE to the group:
  · STAGE=Effective / trading   → "Live & staking"
  · STAGE=In registration       → "In registration"
  · STAGE=Not staking (yet)     → "Not staking (yet)"
- Exceptions that override STAGE for grouping: a Stream B 19b-4 approval filing → "Approval milestone (19b-4)";
  a broad multi-asset basket that merely includes the asset → "Multi-asset (includes the asset)".
- Only include groups that apply. Use these labels verbatim. Each product appears in exactly ONE group.
- Do NOT put emoji on group labels — the coloured-circle legend belongs to the alert stream, not the Q&A. Plain text labels only.
- Under each group, list the relevant products. Per product provide: name; ticker (if any);
  a one-line status; an optional short detail (a notable fact — a fee change, staking mechanics,
  who stakes it); and the filing link + a short link label (e.g. "S-1/A", "10-K", "OIP", "424B3").
- Base everything on the provided records — do NOT invent products, tickers, dates, or facts.
  If a product's status is unclear from the records, say so in its status line.
- Keep status and detail to one short line each; the layout provides the structure."""


ANSWER_TOOL = {
    "name": "compose_answer",
    "description": "Compose the Slack answer as structured groups of products.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short headline, e.g. 'SOL ETF activity'."},
            "groups": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Plain-text group label, no emoji."},
                    "items": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "ticker": {"type": "string"},
                            "status": {"type": "string"},
                            "detail": {"type": "string"},
                            "link": {"type": "string"},
                            "link_label": {"type": "string"},
                        },
                        "required": ["name", "status"],
                    }},
                },
                "required": ["label", "items"],
            }},
            "footer": {"type": "string"},
        },
        "required": ["title", "groups"],
    },
}


def detect_assets(text):
    low = text.lower()
    found = []
    for kw, tok in clf.ASSET_KW:
        if kw in low and tok not in found:
            found.append(tok)
    return found


_SPONSORS = None  # lazy cache of the DB's distinct sponsor names


def detect_sponsor(text):
    """Return a sponsor named in the question, matched against the record's actual
    sponsors (longest first, word-boundary). None if the question isn't sponsor-scoped."""
    global _SPONSORS
    if _SPONSORS is None:
        try:
            _SPONSORS = sorted(notion_store.known_sponsors(), key=len, reverse=True)
        except Exception:  # noqa: BLE001 — degrade to no sponsor filter
            _SPONSORS = []
    low = text.lower()
    for s in _SPONSORS:
        if re.search(rf"\b{re.escape(s.lower())}\b", low):
            return s
    return None


_DETAIL_KEYS = ("sponsor_fee", "staking_fee", "staking_provider",
                "custodian", "listing_exchange", "pct_staked")


# Milestones that mean the registration is effective / the product is trading or
# reporting. A product that ONLY ever filed S-1 / S-1-A is still in registration.
_LIVE_HINTS = ("effect", "cert", "listing", "8-a", "424b", "post-effective",
               "10-q", "10-k", "8-k")


def _product_key(r):
    """Stable identity for a product across renames AND ticker changes. CIK is the
    same EDGAR entity through both, so it's the primary key; fall back to ticker,
    then sponsor+product, then the raw issuer string (e.g. Stream B rows w/o a CIK)."""
    cik = (r.get("cik") or "").strip().lstrip("0")
    if cik:
        return f"cik:{cik}"
    t = (r.get("ticker") or "").strip().lower()
    if t:
        return f"t:{t}"
    sp = " ".join((r.get("sponsor") or "").split()).lower()
    pr = " ".join((r.get("product") or "").split()).lower()
    return f"{sp}|{pr}" if (sp or pr) else " ".join((r.get("issuer") or "").split()).lower()


def _stage(members):
    """Deterministic lifecycle status from the product's whole filing history —
    so 'live vs in registration' is decided consistently, not guessed per answer."""
    sigs = {m.get("signal") for m in members}
    mis = " ".join((m.get("milestone") or "").lower() for m in members)
    if sigs <= {"Not a staking product"}:
        return "Not staking (yet)"
    if any(h in mis for h in _LIVE_HINTS):
        return "Effective / trading"
    return "In registration"


def collapse_to_products(rows):
    """Collapse many filing rows to one per product, so the model sees the current
    product set — not every historical filing. Group by stable product key; keep the
    newest filing as the base (current name/ticker/status), backfill any detail field
    it lacks from that product's older filings (a fee stated in an S-1 but absent from
    a later 10-Q is preserved), and attach a deterministic lifecycle `stage`."""
    groups = {}
    for r in rows:
        groups.setdefault(_product_key(r), []).append(r)
    out = []
    for members in groups.values():
        members.sort(key=lambda r: (r.get("filed") or ""), reverse=True)  # newest first
        base = dict(members[0])
        for r in members[1:]:
            for f in _DETAIL_KEYS:
                if not base.get(f) and r.get(f):
                    base[f] = r[f]
        base["stage"] = _stage(members)
        out.append(base)
    return out


def build_context(rows):
    lines = []
    for r in rows:
        assets = ",".join(r.get("assets") or []) or "—"
        link = r.get("link") or r.get("url") or ""   # prefer the SEC filing link for citations
        name = " ".join(x for x in [r.get("sponsor"), r.get("product")] if x) or r.get("issuer", "")
        tick = f" [{r['ticker']}]" if r.get("ticker") else ""
        extra = [f"{lab}={r[k]}" for lab, k in
                 [("fee", "sponsor_fee"), ("staking_fee", "staking_fee"),
                  ("staking_provider", "staking_provider"), ("custodian", "custodian"),
                  ("exchange", "listing_exchange"), ("staked", "pct_staked")] if r.get(k)]
        det = (" | " + "; ".join(extra)) if extra else ""
        stage = f" | STAGE={r['stage']}" if r.get("stage") else ""
        lines.append(f"- {name}{tick} | sponsor={r.get('sponsor') or '—'} | {r.get('signal')}{stage} "
                     f"| {assets} | latest: {r.get('milestone')} | filed {r.get('filed')} "
                     f"| {r.get('summary')}{det} | {link}")
    return "\n".join(lines) if lines else "(no matching filings in the record)"


def build_answer_blocks(v, known_tickers=None):
    """Render the compose_answer structure as spacious Block Kit. Tickers are clamped
    to those actually in the record (known_tickers) — the model must not surface a
    ticker we don't hold, even if it's confident (kills invented symbols)."""
    known = {t.upper() for t in (known_tickers or set())}
    title = (v.get("title") or "Staking-ETF filings")[:150]
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}]
    for g in v.get("groups", []):
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*{g.get('label','')}*"}})
        for it in g.get("items", []):
            tk = (it.get("ticker") or "").strip()
            show_tk = tk and tk.upper() in known
            head = f"*{it.get('name','')}*" + (f"   `{tk}`" if show_tk else "")
            lines = [head]
            if it.get("status"):
                lines.append(it["status"])
            if it.get("detail"):
                lines.append(f"_{it['detail']}_")
            if it.get("link"):
                lines.append(f"<{it['link']}|{it.get('link_label') or 'View filing'} →>")
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
            if len(blocks) >= 46:
                break
        if len(blocks) >= 46:
            break
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": v.get("footer") or "Reflects filings tracked in our system, as of the latest run."}]})
    return title, blocks


def parse_window(question):
    """If the question is time-scoped, return (since_iso, until_iso, label) computed
    from today() — never let the model guess dates. Returns None otherwise."""
    low = question.lower()
    today = dt.date.today()
    m = re.search(r"(?:last|past|previous)\s+(\d+)\s+day", low)
    if m:
        n = int(m.group(1))
        return (today - dt.timedelta(days=n)).isoformat(), today.isoformat(), f"last {n} days"
    if re.search(r"\b(last|past|this|previous)\s+month\b", low):
        return (today - dt.timedelta(days=30)).isoformat(), today.isoformat(), "last 30 days"
    if re.search(r"\b(last|past|this|previous)\s+week\b", low) or re.search(r"\brecent(ly)?\b", low) or "lately" in low:
        return (today - dt.timedelta(days=7)).isoformat(), today.isoformat(), "last 7 days"
    if re.search(r"\byesterday\b", low):
        return (today - dt.timedelta(days=1)).isoformat(), today.isoformat(), "since yesterday"
    if re.search(r"\btoday\b", low):
        return today.isoformat(), today.isoformat(), "today"
    return None


def _fmt(iso):
    try:
        return dt.date.fromisoformat(iso).strftime("%b %-d")
    except (ValueError, TypeError):
        return iso or "?"


def _milestone_label(m):
    """Short label for a filing, e.g. 'registration effectiveness (EFFECT)' -> 'EFFECT'."""
    m = m or "filing"
    p = re.search(r"\(([^)]+)\)", m)
    return p.group(1) if p else m


def answer_recency(question, window, assets, sponsor=None):
    """Deterministic time-scoped answer: the filings in [since, until], grouped by
    product (CIK identity). No LLM — a timeline is pure data, so no invented dates or
    tickers. The window is stated explicitly and data-currency is called out."""
    since, until, label = window
    rows = notion_store.query_filings(assets=assets or None, since=since, sponsor=sponsor, limit=100)
    newest_overall = (notion_store.query_filings(limit=1) or [{}])
    newest_iso = newest_overall[0].get("filed") if newest_overall else None

    scope = "".join(f" · {x}" for x in [sponsor, "/".join(assets) if assets else None] if x)
    title = f"Filings — {label}{scope}"
    win_txt = f"Window {_fmt(since)}–{_fmt(until)}, {until[:4]}."
    if newest_iso and newest_iso < until:
        win_txt += f" Latest filing on record: {_fmt(newest_iso)}, {newest_iso[:4]}."

    if not rows:
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
                  {"type": "section", "text": {"type": "mrkdwn",
                   "text": f"No tracked staking-ETF filings in this window. {win_txt}"}}]
        return title, blocks

    # Group the window's filings by product (CIK-first identity); newest product first.
    groups = {}
    for r in rows:
        groups.setdefault(_product_key(r), []).append(r)
    ordered = sorted(groups.values(), key=lambda ms: max(m.get("filed") or "" for m in ms), reverse=True)

    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}]
    for members in ordered:
        members.sort(key=lambda r: (r.get("filed") or ""), reverse=True)
        b = members[0]
        name = " ".join(x for x in [b.get("sponsor"), b.get("product")] if x) or b.get("issuer", "")
        head = f"*{name}*" + (f"   `{b['ticker']}`" if b.get("ticker") else "")
        filings = []
        for m in members:
            lab, link = _milestone_label(m.get("milestone")), (m.get("link") or m.get("url"))
            entry = f"<{link}|{lab}>" if link else lab
            filings.append(f"{entry} ({_fmt(m.get('filed'))})")
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"{head}\n• " + "  ·  ".join(filings)}})
        if len(blocks) >= 46:
            break
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": win_txt}]})
    return title, blocks


def answer(question):
    assets = detect_assets(question)
    sponsor = detect_sponsor(question)
    window = parse_window(question)
    if window:
        return answer_recency(question, window, assets, sponsor)
    rows = collapse_to_products(
        notion_store.query_filings(assets=assets or None, sponsor=sponsor, limit=100))
    ctx = build_context(rows)
    resp = _anthropic.messages.create(
        model=MODEL, max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[ANSWER_TOOL],
        tool_choice={"type": "tool", "name": "compose_answer"},
        messages=[{"role": "user",
                   "content": f"QUESTION: {question}\n\nFILING RECORDS "
                              f"({len(rows)} rows{', asset filter: ' + ','.join(assets) if assets else ''}):\n{ctx}"}],
    )
    v = next(b.input for b in resp.content if b.type == "tool_use")
    known_tickers = {r["ticker"] for r in rows if r.get("ticker")}
    return build_answer_blocks(v, known_tickers)


app = App(token=os.environ["SLACK_BOT_TOKEN"])


@app.middleware
def _log_all(body, next):
    ev = (body or {}).get("event", {}) or {}
    print(f"[recv] outer={body.get('type')} event={ev.get('type')}", flush=True)
    next()


@app.event("app_mention")
def on_mention(event, say, logger):
    q = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
    thread = event.get("thread_ts") or event["ts"]
    print(f"[mention] channel={event.get('channel')} user={event.get('user')} q={q!r}", flush=True)
    if not q:
        say(text="Ask me about tracked staking-ETF filings, e.g. “current ETH staking ETFs”.",
            thread_ts=thread)
        return
    try:
        text, blocks = answer(q)
        say(text=text, blocks=blocks, thread_ts=thread, unfurl_links=False)
    except Exception as e:  # noqa: BLE001
        logger.exception("answer failed")
        say(text=f":warning: Sorry, I hit an error answering that ({e}).", thread_ts=thread)


if __name__ == "__main__":
    print(f"ask_bot starting (model {MODEL}) — Socket Mode…")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
