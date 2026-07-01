# PRD — Crypto Staking ETF Filing Alerts

| | |
|---|---|
| **Owner** | Christy Breslin (Bitwise Investments) |
| **Status** | Draft — Phase 0 (detection) validated; downstream phases pending decisions |
| **Last updated** | July 1, 2026 |
| **Related docs** | `crypto-staking-etf-filing-alerts-scope.md`, `phase0-detection-results.md` |

## 1. Summary

Build a system that detects new SEC filings related to crypto **staking** ETFs — across all issuers and the broad staking-asset universe (staked ETH, SOL, and other proof-of-stake assets) — and surfaces them in near real-time. The system watches two distinct filing streams (issuer registration filings and exchange rule filings), applies a relevance filter, deduplicates, and routes new filings to a delivery channel to be chosen later. A detection proof-of-concept has been built and validated against live data.

## 2. Problem & motivation

Staking ETF activity is moving fast and across many issuers. The filings that signal a new product, a material amendment, or an approval milestone are scattered across two different SEC systems with different formats and no unified feed. Monitoring this manually is slow and error-prone, and the highest-signal filings (the exchange rule filings that gate approval) are the easiest to miss. A reliable, low-latency alert removes that gap and gives the team a consistent, auditable view of the competitive and regulatory landscape.

## 3. Goals & non-goals

**Goals**

- Detect relevant filings across the full crypto staking universe, not just ETH/SOL.
- Cover both the issuer registration stream and the exchange 19b-4 approval stream.
- Deliver alerts in near real-time (practical floor ~10 minutes, set by SEC feed refresh).
- Keep false positives low enough that alerts stay trustworthy.
- Maintain an auditable record of every filing detected.

**Non-goals**

- Not a legal or investment analysis of filings — detection and routing only.
- Not a general SEC filing monitor — scope is crypto staking products.
- No paid data vendor at this stage; built on free SEC/Federal Register endpoints.
- Not building custom infrastructure to beat the ~10-minute SEC refresh latency.

## 4. Users & stakeholders

Primary user is the Bitwise product/research team tracking competitive and regulatory developments in staking ETFs. The system should be usable by non-engineers: alerts and the running record must be readable without querying an API.

## 5. Scope (confirmed decisions)

- **Asset universe:** broad crypto staking — staked ETH and SOL plus other staking/PoS assets (BNB, HYPE, NEAR, AAVE, TRX, CRO, INJ, Canton, multi-asset trusts, etc.) as they file.
- **Cadence:** near real-time.
- **Issuers:** all issuers (broad market/competitor monitoring), including Bitwise's own filings.
- **Delivery channel:** deferred — explicitly downstream. Slack is the likely eventual endpoint.

## 6. Background — the two-stream model

The filings of interest live in two separate SEC systems, and a system watching only one misses half the signal.

**Stream A — Issuer registration & lifecycle filings (EDGAR).** Filed by the trust/issuer: `S-1`, `S-1/A`, `424B*`, `POS AM`, `POS EX`, `EFFECT`, `8-A12B`, `CERT`, `8-K`, `10-Q`, `10-K`, `N-1A`. Searchable via EDGAR full-text search and RSS.

**Stream B — Exchange rule filings (SRO / 19b-4).** Filed by the listing exchange (NYSE Arca, Cboe BZX, Nasdaq), not the issuer. These are the milestones that start and move the SEC's statutory approval clock: Notice of Filing, Order Instituting Proceedings, Order Granting/Disapproving Approval, accelerated approvals, and delays. They publish at `sec.gov/rules/sro` and the Federal Register and do **not** appear in EDGAR's company full-text search.

## 7. Functional requirements

| # | Requirement |
|---|---|
| FR-1 | Query EDGAR full-text search for staking-related issuer filings on a recurring schedule. |
| FR-2 | Query the Federal Register API for SEC self-regulatory-organization (19b-4) rule filings. |
| FR-3 | Deduplicate results by unique filing ID (EDGAR accession number / Federal Register document number). |
| FR-4 | Apply the relevance filter (Section 9) to remove noise before alerting. |
| FR-5 | Persist which filings have already been seen, so each is alerted at most once. |
| FR-6 | Emit an alert for each new relevant filing, including issuer, asset, form type, date, and a direct link. |
| FR-7 | Maintain a browsable, filterable record of all detected filings. |
| FR-8 | Fail loudly — a silent outage (no runs, or a source returning errors) must be detectable. |

## 8. Data sources & technical constraints

| Source | Stream | Role | Notes |
|---|---|---|---|
| EDGAR Full-Text Search API (`efts.sec.gov/LATEST/search-index`) | A | Primary issuer-filing search | Free, no key. Returns JSON with issuer name, CIK, form, accession (`adsh`), file date, SIC. Full-text coverage 2001–present. |
| EDGAR RSS / submissions API (`data.sec.gov`) | A | Optional per-issuer watchlist monitoring | Free, no key. |
| Federal Register API (`federalregister.gov/api/v1`) | B | Primary 19b-4 rule-filing source | Free, no key. Filter by agency = SEC; keep titles beginning "Self-Regulatory Organizations". |
| `sec.gov/rules/sro` pages | B | Earliest raw source for 19b-4 PDFs | Scrape fallback; less stable than Federal Register. |

**Constraints**

- Every SEC request must send a descriptive `User-Agent` (e.g., `Bitwise Investments christy@bitwiseinvestments.com`) or it is rejected.
- Requests are capped at **10/second across all SEC domains**.
- EDGAR date filtering is **day-granularity** — so alerting on each new filing intraday (multiple runs per day) requires persistent state, not a rolling time window.
- Practical latency floor is ~10 minutes, set by SEC's own feed refresh.
- Requests must run through a network-capable fetch path (the isolated shell has no outbound network; the web-fetch tooling does).

## 9. Matching / relevance logic

Derived from live data during the Phase 0 build:

1. **Deduplicate by filing ID.** A single filing lists many exhibit documents (one iShares S-1/A returned ~16 rows). Collapse to one alert per accession/document number.
2. **Whitelist issuer forms:** S-1, S-1/A, 424B*, POS AM, POS EX, EFFECT, 8-A12B, CERT, 8-K, 10-Q, 10-K, N-1A.
3. **Exclude holder/ownership filings:** 13F-HR and SCHEDULE 13G/13G-A match only because large funds *hold* these ETFs (Goldman, Citadel, Jane Street, BlackRock, UBS, Morgan Stanley all appeared). Drop them.
4. **Filter by SIC code:** keep 6221 (commodity brokers/dealers) and 6199 (finance services), where the ETF trusts sit. Removes off-topic keyword hits (a silver miner and an AI company matched raw "staking").
5. **Confirm relevance:** require a staking term plus a crypto-asset name in the issuer/product name; route borderline cases through a lightweight classification check before alerting.

## 10. System architecture

```
[Scheduled run, every N min]
  → query EDGAR FTS (Stream A) + Federal Register (Stream B)
  → apply relevance filter (Section 9)
  → diff against persistent "seen" store
  → classify borderline hits
  → for each NEW filing: format alert + append to record
  → deliver  ← (channel pluggable: Slack / email / Notion)
```

The delivery step is deliberately the last, swappable link so the detection layer can ship and be trusted before a channel is chosen. The "seen" store and the browsable record can be the same artifact (e.g., a Notion database where each filing is a row).

## 11. Delivery (deferred)

Channel is an open decision. Candidate options, non-exclusive: a Slack channel/DM for team visibility and fast reaction; email digest or per-filing alerts; a Notion database that doubles as the record. The architecture supports any of these as the final routing step without changing detection.

## 12. Phasing & milestones

| Phase | Scope | Status |
|---|---|---|
| **Phase 0 — Detection MVP** | On-demand query of EDGAR + Federal Register, relevance filtering, deduped digest. Proves matching quality. No storage/cadence/delivery. | **Done & validated** (see `phase0-detection-results.md`) |
| **Phase 1 — Full coverage + quality** | Add persistent "seen" store, full Stream B lifecycle coverage, relevance classifier, and a browsable record (e.g., Notion). Enables true per-filing near-real-time alerts. | Pending decisions |
| **Phase 2 — Productionize** | Lock delivery channel (e.g., Slack routing by issuer/asset), tune thresholds, add heartbeat/health checks, backfill recent history. | Pending Phase 1 |

## 13. Open decisions

- **State/record store:** where the "seen" log and browsable record live (Notion database recommended; alternatives: cloud-drive log file, or a stateless daily digest with no store).
- **Run cadence:** concrete frequency and hours (e.g., every 15/30/60 min, weekdays, business hours).
- **Delivery channel(s):** Slack, email, Notion, or a combination.
- **Asset-list confirmation:** confirm any assets to include/exclude beyond those already caught.
- **Issuer watchlist:** whether to maintain a curated CIK list to complement the keyword net.

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Missing the 19b-4 (Stream B) approval milestones | Treat Stream B as a first-class source, not an add-on; Federal Register API is the stable primary. |
| SRO page-scraping fragility | Prefer the Federal Register API; use `sec.gov/rules/sro` only as a secondary/earliest-signal source. |
| False positives from broad "staking" scope | Apply the five-rule relevance filter; add a classification pass for borderline hits. |
| Duplicate alerts | Persistent dedup keyed on accession/document number (required because EDGAR dates are day-granularity). |
| Silent failure (no alerts because a run broke) | Heartbeat/health check so absence of runs is itself detectable. |
| SEC rate limiting / blocking | Respect 10 req/s and always send a descriptive User-Agent. |
| Latency expectations | Communicate the ~10-minute floor; "near real-time," not sub-second. |

## 15. Success metrics

- **Recall:** no relevant staking-ETF filing is missed across either stream (spot-checked against known products).
- **Precision:** low false-positive rate; alerts remain trustworthy enough to act on.
- **Latency:** relevant filings surfaced within ~10–30 minutes of hitting SEC systems.
- **Coverage:** ETH, SOL, and the broader staking universe all represented in the record.
- **Reliability:** scheduled runs complete on cadence; outages are detected, not silent.

## 16. Appendix — reproducible queries

```
# Stream A — EDGAR full-text search (issuer filings)
GET https://efts.sec.gov/LATEST/search-index
  ?q="staking"
  &forms=S-1,S-1/A,424B3,POS AM,POS EX,EFFECT,8-A12B,10-Q
  &startdt=<from>&enddt=<to>
  Header: User-Agent: Bitwise Investments christy@bitwiseinvestments.com
  → dedup by _source.adsh; apply SIC + form + holder filters (Section 9)

# Stream B — Federal Register (exchange 19b-4 rule filings)
GET https://www.federalregister.gov/api/v1/documents.json
  ?conditions[agencies][]=securities-and-exchange-commission
  &conditions[term]=staking
  &order=newest
  → keep titles starting "Self-Regulatory Organizations"; dedup by document_number
```
