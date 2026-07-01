#!/usr/bin/env python3
"""
Orchestrator for the crypto staking ETF filing alerts.

  detect (both streams) -> §9 filter -> drop already-seen (Notion)
  -> classify (rules) -> write row (Notion) -> post alert (Slack)

Dedup is keyed on Filing ID against the Notion record, so this can run on a
short trailing window every N minutes without re-posting. Fails loudly:
non-zero exit + summary if any source or per-filing step errors.

Usage:
  export SLACK_BOT_TOKEN=...  NOTION_TOKEN=...   (see .env)
  python3 run.py --days 3 --dry-run     # show what WOULD post, no writes
  python3 run.py --days 3               # live: write to Notion + post to Slack
"""

import argparse
import datetime as dt
import sys

import detect
import classify as clf
import categorize_llm as cat
import notion_store
import slack_post

CHANNEL = slack_post.DEFAULT_CHANNEL


def _title(rec):
    """The headline is always the fund/product name."""
    return (rec.get("product_name") or rec.get("issuer") or "Filing")[:150]


def format_alert(rec, notion_url=None):
    """Plain-text fallback (used as the Slack notification preview)."""
    emoji = clf.SIGNAL_EMOJI[rec["signal"]]
    return f"{emoji} {_title(rec)} — {rec['signal']}"


def format_blocks(rec, notion_url=None):
    """Block Kit 'Option C' layout: the FUND NAME is the header; status (colour +
    signal) is denoted on the line beneath it, then What & why, Evidence, footer."""
    emoji = clf.SIGNAL_EMOJI[rec["signal"]]
    stream_short = "Stream B (19b-4)" if rec["stream"].startswith("B") else "Stream A"
    chips = "  ".join(f"`{a}`" for a in (rec.get("assets") or []))
    conf = rec.get("confidence")
    status = "  ·  ".join(x for x in [f"{emoji} *{rec['signal']}*", chips,
                                      rec.get("structure"),
                                      (f"_{conf} confidence_" if conf else "")] if x)
    links = f"<{rec['link']}|View filing →>"
    if notion_url:
        links += f"  ·  <{notion_url}|Notion record →>"

    blocks = [
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": _title(rec), "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": status}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What & why*\n{rec.get('summary','')}"}},
    ]
    ev = rec.get("evidence") or ""
    if ev and not ev.startswith("("):   # skip the rules-fallback "(...)" notes
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*Evidence from the filing*\n> {ev}"}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"{stream_short}  ·  {rec['milestone']}  ·  📅 {rec['filed']}  ·  {links}"}]})
    return blocks


def detect_all(start, end):
    """Both streams through the §9 filter. Raises on source failure (fail loudly)."""
    stream_a = detect.filter_edgar(detect.fetch_edgar(start, end))
    stream_b = detect.fetch_fedreg(start, end)
    return stream_a + stream_b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="trailing window size")
    ap.add_argument("--start", help="YYYY-MM-DD (overrides --days)")
    ap.add_argument("--end", help="YYYY-MM-DD (defaults to today)")
    ap.add_argument("--dry-run", action="store_true", help="no Notion writes / no Slack posts")
    ap.add_argument("--limit", type=int, default=0, help="cap number of new filings processed (0 = all)")
    ap.add_argument("--alerts-only", action="store_true", help="process only signals that alert (skip record-only)")
    ap.add_argument("--rules-only", action="store_true", help="skip the LLM; classify from metadata only")
    args = ap.parse_args()

    end = args.end or dt.date.today().isoformat()
    start = args.start or (dt.date.fromisoformat(end) - dt.timedelta(days=args.days)).isoformat()

    categorize = clf.classify if args.rules_only else cat.categorize

    filings = detect_all(start, end)
    seen = notion_store.seen_filing_ids()
    history = notion_store.issuer_history()
    # genuinely new = detected minus already-recorded; oldest first for chronological posting
    candidates = sorted((f for f in filings if f["filing_id"] not in seen),
                        key=lambda f: (f.get("filed") or "", f["filing_id"]))

    engine = "rules" if args.rules_only else f"LLM ({cat.MODEL})"
    print(f"window {start}..{end} | detected {len(filings)} | already-seen {len(filings) - len(candidates)} "
          f"| NEW {len(candidates)} | engine {engine}")
    if not candidates:
        print("nothing new — done.")
        return

    # Categorize each filing exactly once (LLM calls are metered). Respect --limit and
    # --alerts-only while categorizing so a capped/alerts-only run doesn't pay for the rest.
    work = []
    for f in candidates:
        if args.limit and len(work) >= args.limit:
            break
        rec = categorize(f, history)
        if args.alerts_only and not clf.alertable(rec["signal"]):
            continue
        work.append(rec)

    if not work:
        print("nothing to process after filters — done.")
        return

    errors, posted, recorded = [], 0, 0
    for rec in work:
        alert = clf.alertable(rec["signal"])
        conf = rec.get("confidence", "")
        if args.dry_run:
            tag = "ALERT " if alert else "record"
            print(f"  {tag} · {clf.SIGNAL_EMOJI[rec['signal']]} {rec['signal']:<28} · "
                  f"{rec['issuer']} · {rec['milestone']}"
                  + (f"  [{conf}]" if conf else "")
                  + (f"\n           ↳ {rec.get('evidence','')}" if rec.get("evidence") else ""))
            continue
        try:
            page = notion_store.add_filing(rec)   # everything is recorded
            recorded += 1
            if alert:                              # only high-signal events post
                resp = slack_post.post(CHANNEL, format_alert(rec, page.get("url")),
                                       blocks=format_blocks(rec, page.get("url")))
                posted += 1
                print(f"  POSTED  {rec['signal']} · {rec['issuer']} · slack_ts={resp.get('ts')}")
            else:
                print(f"  record  {rec['signal']} · {rec['issuer']}")
        except Exception as e:  # noqa: BLE001 - collect and report, don't abort the batch
            errors.append((rec["filing_id"], str(e)))
            print(f"  ERROR {rec['filing_id']}: {e}", file=sys.stderr)

    if args.dry_run:
        alerts = sum(clf.alertable(r["signal"]) for r in work)
        print(f"done (dry-run) — would alert {alerts} / record {len(work) - alerts} (processed {len(work)})")
    else:
        print(f"done — recorded {recorded} | posted {posted} | errors {len(errors)}")
    if errors:
        sys.exit(1)  # fail loudly so a scheduler surfaces it


if __name__ == "__main__":
    main()
