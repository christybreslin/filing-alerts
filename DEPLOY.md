# Deploying the staking-ETF filing alerts on a server (cron)

The pipeline is stateless between runs — dedup + the record live in Notion — so a cron
job is all that's needed. Only genuinely new filings get the (metered) LLM call, so run
frequency doesn't drive cost.

## 1. Get the code onto the server

Copy the project directory (everything **except** `.env` and `.venv/`, which are recreated
on the server). The code files are:

```
detect.py  classify.py  categorize_llm.py  fetch_doc.py
notion_store.py  slack_post.py  run.py  run.sh  requirements.txt
```

e.g. `scp -r sec-filings/ user@server:/opt/` (or clone from a git remote if you put it in one).

## 2. Python env

```
cd /opt/sec-filings
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt      # installs anthropic
```

Needs Python 3.9+ and outbound network to: sec.gov, efts.sec.gov, federalregister.gov,
slack.com, api.notion.com, api.anthropic.com.

## 3. Secrets — create `.env` (chmod 600, never commit)

```
SLACK_BOT_TOKEN=xoxb-...
NOTION_TOKEN=ntn_...
NOTION_DB_ID=f274f6ba6afb40219b7907baec1ba53a      # personal DB for now; change when it moves to Bitwise
ANTHROPIC_API_KEY=sk-ant-...
CATEGORIZER_MODEL=claude-sonnet-4-6                # optional (default)
RUN_DAYS=3                                         # trailing detection window
# optional dead-man's-switch (see step 6):
# HEALTHCHECK_URL=https://hc-ping.com/<uuid>
# HEALTH_CHANNEL=C0BEJ3QNP6G                        # where failures post (default: alerts channel)
```

```
chmod 600 .env
chmod +x run.sh
```

## 4. Smoke test before scheduling

```
set -a; . ./.env; set +a
.venv/bin/python run.py --days 3 --dry-run     # detect + categorize, no writes/posts
./run.sh                                        # one real run; check run.log
```

## 5. Cron

`crontab -e`, then (every 30 min, weekdays — tune to taste; cron uses server local time):

```
*/30 * * * 1-5  /opt/sec-filings/run.sh
```

The wrapper self-locks, so overlapping runs are skipped. Logs append to `run.log`
(rotate with logrotate if desired).

## 6. Heartbeat (FR-8 — detect a silent outage)

Failures already post to Slack. To also catch the pipeline *not running at all* (server
down, cron disabled), use a free dead-man's-switch such as healthchecks.io:
create a check, put its ping URL in `.env` as `HEALTHCHECK_URL`, and it alerts you if the
expected ping doesn't arrive on schedule. `run.sh` pings on success and `/fail` on failure.

## Notes
- `requirements.txt` pins `anthropic`; everything else is Python stdlib.
- To change the alert model or window without redeploying, edit `.env` (`CATEGORIZER_MODEL`, `RUN_DAYS`).
- `--rules-only` on `run.py` bypasses the LLM (offline/debug or if the API key lapses).
