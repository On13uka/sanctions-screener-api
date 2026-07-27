[![RapidAPI](https://img.shields.io/badge/RapidAPI-Live-brightgreen)](https://rapidapi.com/On13uka/api/sanctions-screener)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-31%20pass-brightgreen)](#tests)

# Sanctions Screener API

Screen names against **five** government sanctions lists with a single,
unified JSON response: OFAC SDN (US Treasury), UN Consolidated, EU Financial
Sanctions Files (FSF), UK FCDO Sanctions List (UKSL), and the US BIS
Consolidated Screening List (DPL + Entity List + UVL + MEU). Returns match
score, source, entity details, AKAs (aliases), an explainable match
explanation, and a plain-English risk verdict.

Also includes two table-stakes features competitors charge extra for:

- **Crypto wallet screening** (`/screen_crypto`) -- check a BTC/ETH/XMR/LTC/...
  wallet address against OFAC SDN digital currency addresses (US public
  domain, per OFAC FAQ 563). Free because the data is already inside the
  OFAC SDN feed.
- **Webhook monitoring** (`/monitor` family) -- register a name + webhook URL
  and get a `new_match` POST the moment a new designation appears. Best-effort
  delivery, simple JSON-file storage (documented as not production-grade).

## What's new

### v1.3 -- BIS CSL + Crypto wallet screening + Webhook monitoring

- **US BIS Consolidated Screening List** (`BIS CSL` source): trade.gov CSL
  API integration covering the BIS Denied Persons List (DPL), Entity List,
  Unverified List (UVL), and Military End-User (MEU) List. US public domain
  (17 USC 105). Free API key from `https://developer.trade.gov/` set via the
  `TRADE_GOV_API_KEY` env var. Degrades gracefully (empty + status warning)
  when the key is missing. 24h on-disk cache.
- **`/screen_crypto` endpoint**: screen a crypto wallet address against OFAC
  SDN digital currency addresses (BTC/XBT, ETH, LTC, XMR, XRP, DASH, NEO,
  MIOTA, PTR, and any other symbol OFAC publishes). In-memory index rebuilt
  on every OFAC refresh; bounded at ~100k addresses (<20MB). Returns
  `sanctioned: true/false`, `matches[]`, and a plain-English `risk_verdict`.
- **`/monitor` family**: `POST /monitor` (register), `GET /monitor/{id}`,
  `DELETE /monitor/{id}`, `POST /monitor/run` (cron-callable). Persists to
  `data/monitors.json` (simple JSON file -- NOT production-grade; use Redis +
  a real queue for scale). Webhook delivery: 10s timeout, one retry,
  best-effort, never blocks the registering request.
- 14 new pytest tests (31 total, all mocked, all pass).

### v1.2 -- Explainable Match + Plain-English Risk Verdict (killer differentiator)

- **`match_explanation`** on every match: *why* the match was made -- which
  field (primary name / AKA), which value, the match type (exact / fuzzy /
  partial / token), and the tokens that overlapped.
- **`risk_verdict`** top-level string: a one-line plain-English verdict
  derived from the highest-severity match, e.g.
  `"HIGH RISK: exact match on OFAC SDN, listed under RUSSIA-EO14024"`.
- Risk bands: HIGH (exact), MEDIUM (fuzzy >=0.85), LOW (0.7-0.85, manual
  review), CLEAN (no match).
- Pure logic on data we already parse -- no new dependencies, no new data
  sources. Old fields (`source`, `match_score`) preserved.

### v1.2 -- Licensing fix: OpenSanctions removed, official government feeds in

- **EU Financial Sanctions Files (FSF)**: now loaded directly from the
  official EU FSF XML feed (`webgate.ec.europa.eu/europeaid/fsd/fsf`).
  European Commission reuse policy permits commercial reuse with
  attribution. OpenSanctions is no longer used.
- **UK FCDO Sanctions List (UKSL)**: now loaded directly from the official
  UK FCDO static XML at `sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml`,
  published under the **Open Government Licence v3.0** (commercial use
  explicitly allowed). OpenSanctions is no longer used.
- OFAC SDN (US public domain, 17 USC 105) and UN Consolidated (UN public
  data) feeds are unchanged -- they were already clean.
- **Result:** the API is now safe to sell on RapidAPI for commercial
  compliance screening. The previous v1.1 used OpenSanctions (CC BY-NC 4.0,
  commercial use prohibited) for EU/UK data and was NOT legally sellable.

## Data sources

| Source | Provider | Refresh | License |
|---|---|---|---|
| OFAC SDN List (US Treasury) | direct XML feed | on startup / `refresh=true` | US public domain (17 USC 105) |
| UN Security Council Consolidated List | direct XML feed | on startup / `refresh=true` | UN public data |
| EU Financial Sanctions Files (FSF) | official EU FSF XML (auth-gated, free EU Login token) | daily (24h TTL cache) | European Commission reuse policy (commercial OK w/ attribution) |
| UK FCDO Sanctions List (UKSL) | official UK FCDO static XML | daily (24h TTL cache) | Open Government Licence v3.0 (commercial OK w/ attribution) |
| US BIS Consolidated Screening List (CSL) | trade.gov CSL API (free `TRADE_GOV_API_KEY`) | daily (24h TTL cache) | US public domain (17 USC 105) |
| OFAC SDN digital currency addresses | parsed from OFAC SDN `<remarks>` (already loaded) | on every OFAC refresh | US public domain (17 USC 105) |

Total: 20,000+ sanctioned individuals, entities, and vessels across five
jurisdictions, plus OFAC-sanctioned crypto wallet addresses.

> **Note on the EU FSF token.** The EU FSF XML download is auth-gated: the
> operator must register a free EU Login account at
> `https://webgate.ec.europa.eu/europeaid/fsd/fsf#!/account`, generate a
> personal download token, and set it as the `EU_FSF_TOKEN` environment
> variable. When the token is missing, the EU loader returns an empty list
> (and surfaces a clear status message on `/status`); the rest of the API
> keeps working with OFAC/UN/UK/BIS data. **OpenSanctions is intentionally NOT
> used as a fallback** -- that is exactly the dependency this version
> removes.

> **Note on the BIS CSL API key.** The trade.gov CSL endpoint requires a free
> API key from the ITA Developer Portal at `https://developer.trade.gov/`.
> Register, subscribe to "Data Services Platform APIs", and copy the primary
> key from your Profile page. Set it as the `TRADE_GOV_API_KEY` environment
> variable. When the key is missing, the BIS loader returns an empty list and
> surfaces a clear status message on `/status`; the rest of the API keeps
> working with OFAC/UN/EU/UK data.

> **Note on the UK list.** The UK HMT/OFSI Consolidated List was deprecated
> by the UK government on 28 January 2026 and replaced by the UK Sanctions
> List (UKSL). This API screens against UKSL via the official FCDO XML feed.

## Endpoints

### GET /screen?name=John+Doe&threshold=0.7

Screen a name against all four sanctions lists. Returns matches above
threshold with match score, source, entity details, matched AKA,
explainable match explanation, and a top-level plain-English risk verdict.

**Parameters:**
- `name` (required): Name to screen
- `threshold` (optional, default 0.7): Minimum match score (0.0-1.0)
- `refresh` (optional, default false): Force refresh sanctions data

**Example:**

```bash
curl "https://your-app.onrender.com/screen?name=Vladimir+Putin&threshold=0.7"
```

**Response (multi-list match with explainable fields):**

```json
{
  "query": "Vladimir Putin",
  "threshold": 0.7,
  "total_matches": 2,
  "ofac_matches": 1,
  "un_matches": 0,
  "eu_matches": 0,
  "uk_matches": 1,
  "matches": [
    {
      "source": "OFAC SDN",
      "entity_id": "ofac-fx-001",
      "name": "VLADIMIR PUTIN",
      "type": "Individual",
      "program": "RUSSIA-EO14024",
      "remarks": "President of the Russian Federation",
      "matched_aka": null,
      "match_score": 1.0,
      "match_type": "exact",
      "match_explanation": {
        "matched_field": "name",
        "matched_value": "VLADIMIR PUTIN",
        "match_type": "exact",
        "tokens_matched": ["putin", "vladimir"]
      }
    },
    {
      "source": "UK FCDO",
      "entity_id": "UK-002",
      "name": "VLADIMIR PUTIN",
      "type": "Individual",
      "program": "Russia",
      "remarks": "President of Russia",
      "matched_aka": "PUTIN, Vladimir Vladimirovich",
      "match_score": 1.0,
      "match_type": "exact",
      "match_explanation": {
        "matched_field": "aka",
        "matched_value": "PUTIN, Vladimir Vladimirovich",
        "match_type": "exact",
        "tokens_matched": ["putin", "vladimir"]
      }
    }
  ],
  "risk_verdict": "HIGH RISK: exact match on OFAC SDN, listed under RUSSIA-EO14024",
  "data_updated": {
    "ofac": "2026-07-26T12:33:32+00:00",
    "un": "2026-07-26T12:33:10+00:00",
    "eu": "2026-07-26T12:34:00+00:00",
    "uk": "2026-07-26T12:34:00+00:00"
  },
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

**Response (clean name):**

```json
{
  "query": "John Q Random Innocent",
  "threshold": 0.7,
  "total_matches": 0,
  "ofac_matches": 0,
  "un_matches": 0,
  "eu_matches": 0,
  "uk_matches": 0,
  "matches": [],
  "risk_verdict": "CLEAN: no matches found across 4 lists screened",
  "data_updated": { "ofac": "...", "un": "...", "eu": "...", "uk": "..." },
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

### GET /screen_crypto?address=0x901b...&currency=ETH

Screen a crypto wallet address against OFAC SDN digital currency addresses
(US public domain, per OFAC FAQ 563). The index is built from the OFAC SDN
`<remarks>` text and refreshed on every OFAC feed refresh. Supports BTC/XBT
(Bitcoin aliases), ETH, LTC, XMR, XRP, DASH, NEO, MIOTA, PTR, and any other
symbol OFAC publishes.

**Parameters:**
- `address` (required): wallet address (max 256 chars per OFAC FAQ 563)
- `currency` (required): digital currency symbol (e.g. `ETH`, `BTC`, `XBT`)

**Example (sanctioned address):**

```bash
curl "https://your-app.onrender.com/screen_crypto?address=0x901bb9583b24d97e99513c6778dc6888ab6870e&currency=ETH"
```

```json
{
  "address": "0x901bb9583b24d97e99513c6778dc6888ab6870e",
  "currency": "ETH",
  "sanctioned": true,
  "matches": [
    {
      "source": "OFAC SDN",
      "entity_id": "ofac-fx-004",
      "entity_name": "LAZARUS GROUP",
      "program": "DPRK2",
      "currency": "ETH",
      "address": "0x901bb9583b24d97e99513c6778dc6888ab6870e",
      "listed_on": "2022-04-22",
      "entity_type": "Entity"
    }
  ],
  "risk_verdict": "HIGH RISK: address belongs to OFAC-sanctioned entity LAZARUS GROUP (DPRK2 program)",
  "index_built_at": "2026-07-26T12:33:32+00:00",
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

**Example (clean address):**

```json
{
  "address": "0xdeadbeef00000000000000000000000000000000",
  "currency": "ETH",
  "sanctioned": false,
  "matches": [],
  "risk_verdict": "CLEAN: address not found in OFAC SDN digital currency addresses",
  "index_built_at": "2026-07-26T12:33:32+00:00",
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

### POST /monitor -- register a webhook monitor

Register an entity name for ongoing monitoring. When `POST /monitor/run` is
called (cron-callable) and a NEW match appears for the name (a list that
wasn't matched before, or a new entity_id), the checker POSTs a webhook
payload to the registered URL.

**Request body:**

```json
{
  "name": "Vladimir Putin",
  "webhook_url": "https://your-app.com/sanctions-webhook",
  "lists": ["OFAC", "UN", "EU", "UK"]
}
```

`lists` is optional and defaults to all five (`OFAC`, `UN`, `EU`, `UK`,
`BIS`). Allowed values: `OFAC`, `UN`, `EU`, `UK`, `BIS`.

**Response:**

```json
{
  "monitor_id": "mon_a1b2c3d4",
  "status": "registered",
  "name": "Vladimir Putin",
  "webhook_url": "https://your-app.com/sanctions-webhook",
  "lists": ["OFAC", "UN", "EU", "UK"],
  "created_at": "2026-07-26T12:35:12+00:00",
  "next_check": "call POST /monitor/run to trigger a check"
}
```

### GET /monitor/{monitor_id} -- check monitor status

Returns the monitor record with `last_check`, `last_match_signature`, and a
rolling `history` array of check/delivery events.

### DELETE /monitor/{monitor_id} -- unregister

Returns `{"monitor_id": "...", "status": "deleted"}`.

### POST /monitor/run -- trigger a check cycle (cron-callable)

Triggers a background check across all registered monitors. Returns
immediately with `202 Accepted`:

```json
{
  "status": "accepted",
  "message": "monitor check scheduled in background",
  "monitors_count": 12,
  "triggered_at": "2026-07-26T12:35:12+00:00"
}
```

For each monitor with a NEW match, the checker POSTs this payload to the
registered webhook URL (10s timeout, one retry, best-effort):

```json
{
  "event": "new_match",
  "monitor_id": "mon_a1b2c3d4",
  "name": "Vladimir Putin",
  "new_match": {
    "source": "OFAC SDN",
    "entity_id": "...",
    "name": "VLADIMIR PUTIN",
    "match_score": 1.0,
    "match_type": "exact",
    "match_explanation": { ... }
  },
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

> **Storage caveat.** Monitor registrations are persisted to a single JSON
> file at `data/monitors.json`. This is intentionally simple and is NOT
> production-grade: it does not scale beyond a few thousand monitors, offers
> no concurrency guarantees under heavy write load, and is not encrypted at
> rest. For production scale, swap the storage layer for Redis or Postgres
> and use a real task queue (Celery, RQ, arq) instead of the in-process
> background task. The webhook delivery itself is best-effort: a 10s timeout
> per POST, one retry on any non-2xx response or transport error, and every
> delivery attempt is appended to `data/monitor-log.jsonl` (one JSON object
> per line) for audit. The registering HTTP request is never blocked by
> webhook delivery.

### GET /status

Returns data loading status and entity counts per source (`ofac`, `un`,
`eu`, `uk`, `bis`), plus a `crypto_index` block with the OFAC digital
currency address index stats. Each source block carries a `status` message
(e.g. an EU FSF token-missing warning or a BIS `TRADE_GOV_API_KEY`-missing
warning) so operators can see at a glance which lists are degraded.

### GET /health

Returns `{"status": "ok"}`.

## Match scoring

| Score | match_type | Example |
|---|---|---|
| 1.0 | exact | "Vladimir Putin" = "VLADIMIR PUTIN" (normalized) |
| 0.85 | fuzzy | "Kim Jong" matches "KIM JONG UN" (prefix) |
| 0.7 | partial | "Furqan" contained in "AL FURQAN MEDIA" |
| 0.5-0.6 | token | shared words between query and target |

## Risk verdict logic

| Condition | Verdict | Example |
|---|---|---|
| exact match (1.0) on any list | `HIGH RISK: exact match on {source}...` | "HIGH RISK: exact match on OFAC SDN, listed under RUSSIA-EO14024" |
| fuzzy match (>=0.85) | `MEDIUM RISK: fuzzy match ({N}% confidence) on {source} via {field}` | "MEDIUM RISK: fuzzy match (85% confidence) on UN Consolidated via name" |
| partial match (0.7-0.85) | `LOW RISK: possible match ({N}% confidence) on {source}, recommend manual review` | "LOW RISK: possible match (70% confidence) on OFAC SDN via name, recommend manual review" |
| no match | `CLEAN: no matches found across {N} lists screened` | "CLEAN: no matches found across 4 lists screened" |

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
# Optional: set EU FSF token for EU data (UK + OFAC + UN need no token)
set EU_FSF_TOKEN=your_eu_login_token   # Windows
# export EU_FSF_TOKEN=your_eu_login_token   # macOS/Linux
# Optional: set trade.gov API key for BIS CSL data (degrades gracefully if missing)
set TRADE_GOV_API_KEY=your_trade_gov_key   # Windows
# export TRADE_GOV_API_KEY=your_trade_gov_key   # macOS/Linux
uvicorn app.main:app --reload --port 8127
```

Note: First startup downloads ~28MB OFAC XML + ~2MB UN XML, plus the EU FSF
XML (when `EU_FSF_TOKEN` is set) and the UK FCDO XML. The EU/UK parsed dumps
are cached to `data/eu-sanctions.json` and `data/uk-sanctions.json` and
reused for 24 hours. Set `SANCTIONS_CACHE_DIR` to relocate the cache.

### Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```

Tests use small fixture files in `tests/fixtures/` and never touch the
network, so they run in under a second. 31 tests (17 backward-compat +
explainable-match + 14 v1.3 feature tests) all pass.

## Deployment notes

### Self-hosted (Render / Railway / Fly.io / a VPS) - recommended

Works as-is. The on-disk cache in `data/` survives across warm invocations
and is refreshed once per day. Set `EU_FSF_TOKEN` in the environment for EU
data; UK + OFAC + UN need no token.

### Vercel (serverless) - trade-off

Vercel serverless functions have an **ephemeral filesystem** and a default
1024MB memory / 10s execution budget (or 60s on Pro). The EU/UK XML dumps
take several seconds to download and parse. This means:

- Every cold start re-downloads EU + UK dumps (no persistent disk cache).
- The in-memory cache only helps within a single warm invocation.

For Vercel we recommend one of:
1. **Run a small subset.** Set `SANCTIONS_FIXTURE_DIR` and ship a trimmed
   JSON fixture so the function loads from the bundle instead of downloading.
2. **Use an external cache.** Put the parsed dumps in Upstash Redis or
   S3 and have the function read from there on cold start.
3. **Self-host** the API on Render/Railway/Fly.io where the filesystem
   persists between invocations. This is the simplest path.

The OFAC + UN XML feeds are smaller (~30MB combined) and load quickly, so
they work fine on Vercel even on cold start.

## Attribution (required by the data licenses)

### EU Financial Sanctions Files (FSF)

> (c) European Union, 2026, reproduced under the European Commission reuse
> policy. Source: EU Financial Sanctions Database (FSF),
> https://webgate.ec.europa.eu/fsd/fsf

The European Commission publishes the consolidated list of persons, groups
and entities subject to EU financial sanctions as public data. Reuse --
including commercial reuse -- is permitted with acknowledgement of the
source. See the dataset page on data.europa.eu:
https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions

### UK FCDO Sanctions List (UKSL)

> Contains UK public sector information licensed under the Open Government
> Licence v3.0. Source: UK Sanctions List, Foreign, Commonwealth &
> Development Office, https://sanctionslist.fcdo.gov.uk

The Open Government Licence v3.0 explicitly permits "exploit the Information
commercially and non-commercially". See:
https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/

### OFAC SDN List

US federal government work -- public domain under 17 USC 105. Source:
US Department of the Treasury, Office of Foreign Assets Control,
https://www.treasury.gov/ofac/downloads/sdn.xml

### UN Security Council Consolidated List

UN public data. Source: UN Security Council Consolidated List,
https://scsanctions.un.org/resources/xml/en/consolidated.xml

### US BIS Consolidated Screening List (CSL)

US federal government work -- public domain under 17 USC 105. Source:
US International Trade Administration, Consolidated Screening List,
https://www.trade.gov/consolidated-screening-list (API endpoint:
https://api.trade.gov/gateway/v1/consolidated_screening_list/search).

The CSL consolidates export-control and sanctions screening lists from
multiple US agencies, including the BIS Denied Persons List (DPL), BIS
Entity List, BIS Unverified List (UVL), BIS Military End-User (MEU) List,
State Department ITAR Debarred Parties, and OFAC non-SDN entries. A free
API key from https://developer.trade.gov/ is required (`TRADE_GOV_API_KEY`
env var).

### OpenSanctions -- NOT used

This API does **not** use OpenSanctions data. A previous version (v1.1)
pulled EU/UK data from OpenSanctions, which publishes under CC BY-NC 4.0
(commercial use prohibited). That dependency has been removed and replaced
with the official government feeds above. Do not re-introduce OpenSanctions
as a data source without obtaining a commercial data license.

## Pricing for RapidAPI

- Free: 100 requests/month
- Pro: $29/month - unlimited requests, all four sources, explainable match
- Enterprise: $99/month - API access, webhook alerts, bulk screening

## Available on RapidAPI

**Live API:** https://rapidapi.com/On13uka/api/sanctions-screener

Subscribe and get an instant API key. Free tier: 100 requests/month.

## Other APIs in the Portfolio

- [Domain WHOIS](https://github.com/On13uka/domain-whois-api)
- [Company Info](https://github.com/On13uka/company-info-api)
- [Email Validator](https://github.com/On13uka/email-validator-api)
- [IP Geolocation](https://github.com/On13uka/ip-geolocation-api)
- [Sanctions Screener](https://github.com/On13uka/sanctions-screener-api)

All APIs available on RapidAPI: https://rapidapi.com/user/On13uka