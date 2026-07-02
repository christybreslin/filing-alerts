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

- Organise into GROUPS by current status; only include groups that apply. Typical groups and their emoji:
  · Live & staking → :large_green_circle:
  · Registration / awaiting approval → :large_yellow_circle:
  · Approval milestone (19b-4) → :large_purple_circle:
  · Adding staking → :large_blue_circle:
  · Multi-asset (includes the asset) → :large_orange_circle:
  · Not a staking product / out of scope → :black_circle:
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
                    "emoji": {"type": "string", "description": "Slack emoji shortcode for the group."},
                    "label": {"type": "string"},
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
                "required": ["emoji", "label", "items"],
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


def build_context(rows):
    lines = []
    for r in rows:
        assets = ",".join(r.get("assets") or []) or "—"
        link = r.get("link") or r.get("url") or ""   # prefer the SEC filing link for citations
        extra = [f"{lab}={r[k]}" for lab, k in
                 [("fee", "sponsor_fee"), ("staking_fee", "staking_fee"),
                  ("staking_provider", "staking_provider"), ("custodian", "custodian"),
                  ("exchange", "listing_exchange"), ("staked", "pct_staked")] if r.get(k)]
        det = (" | " + "; ".join(extra)) if extra else ""
        lines.append(f"- {r['issuer']} | {r.get('signal')} | {assets} | {r.get('milestone')} "
                     f"| filed {r.get('filed')} | {r.get('summary')}{det} | {link}")
    return "\n".join(lines) if lines else "(no matching filings in the record)"


def build_answer_blocks(v):
    """Render the compose_answer structure as spacious Block Kit."""
    title = (v.get("title") or "Staking-ETF filings")[:150]
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}]
    for g in v.get("groups", []):
        blocks.append({"type": "divider"})
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"{g.get('emoji','')} *{g.get('label','')}*".strip()}})
        for it in g.get("items", []):
            head = f"*{it.get('name','')}*" + (f"   `{it['ticker']}`" if it.get("ticker") else "")
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


def answer(question):
    assets = detect_assets(question)
    rows = notion_store.query_filings(assets=assets or None, limit=100)
    ctx = build_context(rows)
    resp = _anthropic.messages.create(
        model=MODEL, max_tokens=2000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        tools=[ANSWER_TOOL],
        tool_choice={"type": "tool", "name": "compose_answer"},
        messages=[{"role": "user",
                   "content": f"QUESTION: {question}\n\nFILING RECORDS "
                              f"({len(rows)} rows{', asset filter: ' + ','.join(assets) if assets else ''}):\n{ctx}"}],
    )
    v = next(b.input for b in resp.content if b.type == "tool_use")
    return build_answer_blocks(v)


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
