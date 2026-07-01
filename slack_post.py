#!/usr/bin/env python3
"""
Slack delivery for the staking-ETF filing alerts (Tier B — bot token).

Posts via chat.postMessage using a bot user OAuth token read from the
SLACK_BOT_TOKEN environment variable. Message text uses Slack mrkdwn
(*bold*, `code`, <url|label>, > quote) — the same format already approved.

Usage:
  export SLACK_BOT_TOKEN=xoxb-...
  python3 slack_post.py --test                 # post a connectivity check
  python3 slack_post.py --channel C0BEJ3QNP6G --text "hello"
"""

import argparse
import json
import os
import sys
import urllib.request

API = "https://slack.com/api/chat.postMessage"
DEFAULT_CHANNEL = "C0BEJ3QNP6G"  # #sec-filing-alerts


def post(channel, text, token=None, blocks=None):
    """Post a message; returns Slack's parsed JSON response.

    `text` is the notification fallback (always sent). If `blocks` is given, the
    message renders as Block Kit and `text` is only used for the notification/preview.
    """
    token = token or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SystemExit("SLACK_BOT_TOKEN not set (put it in .env or export it).")
    body = {
        "channel": channel,
        "text": text,
        "unfurl_links": False,   # don't expand the SEC/Notion links into previews
        "unfurl_media": False,
    }
    if blocks:
        body["blocks"] = blocks
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API, data=payload,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _explain(err):
    hints = {
        "not_in_channel": "The bot isn't in the channel. In Slack: /invite @SEC Filing Alerts",
        "channel_not_found": "Channel ID wrong or bot can't see it.",
        "invalid_auth": "Token is invalid/revoked.",
        "missing_scope": "App is missing the chat:write scope — reinstall with it.",
        "not_authed": "No token sent.",
    }
    return hints.get(err, "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--text", default=None)
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()

    text = args.text or (
        "🧪 SEC Filing Alerts — connectivity test, please ignore." if args.test else None)
    if not text:
        raise SystemExit("Provide --text or --test.")

    r = post(args.channel, text)
    if r.get("ok"):
        print(f"OK posted ts={r.get('ts')} channel={r.get('channel')}")
    else:
        err = r.get("error", "unknown")
        print(f"FAILED error={err}  {_explain(err)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
