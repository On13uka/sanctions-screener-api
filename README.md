[![RapidAPI](https://img.shields.io/badge/RapidAPI-Live-brightgreen)](https://rapidapi.com/On13uka/api/sanctions-screener)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-17%20pass-brightgreen)](#tests)

# Sanctions Screener API

Screen names against **four** government sanctions lists with a single,
unified JSON response: OFAC SDN (US Treasury), UN Consolidated, EU
Financial Sanctions Files (FSF), and UK FCDO Sanctions List (UKSL). Returns
match score, source, entity details, AKAs (aliases), an explainable match
explanation, and a plain-English risk verdict.

## What's new

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

Total: 20,000+ sanctioned individuals, entities, and vessels across four
jurisdictions.

> **Note on the EU FSF token.** The EU FSF XML download is auth-gated: the
> operator must register a free EU Login account at
> `https://webgate.ec.europa.eu/europeaid/fsd/fsf#!/account`, generate a
> personal download token, and set it as the `EU_FSF_TOKEN` environment
> variable. When the token is missing, the EU loader returns an empty list
> (and surfaces a clear status message on `/status`); the rest of the API
> keeps working with OFAC/UN/UK data. **OpenSanctions is intentionally NOT
> used as a fallback** -- that is exactly the dependency this version
> removes.

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

### GET /status

Returns data loading status and entity counts per source (`ofac`, `un`,
`eu`, `uk`), plus a `status` message per source (e.g. an EU FSF token-missing
warning).

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
network, so they run in under a second. 17 tests (10 backward-compat + 7
explainable-match) all pass.

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