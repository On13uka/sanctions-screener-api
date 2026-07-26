[![RapidAPI](https://img.shields.io/badge/RapidAPI-Live-brightgreen)](https://rapidapi.com/On13uka/api/sanctions-screener)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.13-blue)](https://python.org)
[![Tests](https://img.shields.io/badge/tests-10%20pass-brightgreen)](#tests)

# Sanctions Screener API

Screen names against **four** government sanctions lists with a single,
unified JSON response: OFAC SDN (US Treasury), UN Consolidated, EU
Consolidated, and UK FCDO. Returns match score, source, entity details,
and AKAs (aliases).

## What's new in v1.1

- **EU Consolidated Sanctions List** (EU Financial Sanctions Files / FSF)
  - ~7,800 entities, via OpenSanctions `eu_fsf`.
- **UK FCDO Sanctions List** (replaces the deprecated UK HMT/OFSI list)
  - ~12,000 entities, via OpenSanctions `gb_fcdo_sanctions`.
- **Additive response fields**: `eu_matches`, `uk_matches`, and per-source
  `data_updated` entries are added alongside the v1.0 fields. Old clients
  reading `source` and `match_score` keep working unchanged.
- **On-disk caching** of the OpenSanctions EU/UK dumps with a 24h TTL, so
  the API does not re-download on every request.

## Data sources

| Source | Provider | Refresh | License |
|---|---|---|---|
| OFAC SDN List (US Treasury) | direct XML feed | on startup / `refresh=true` | public domain (US federal work) |
| UN Security Council Consolidated List | direct XML feed | on startup / `refresh=true` | UN public data |
| EU Financial Sanctions Files (FSF) | OpenSanctions `eu_fsf` | daily (24h TTL cache) | CC BY-NC 4.0 (see below) |
| UK FCDO Sanctions List | OpenSanctions `gb_fcdo_sanctions` | daily (24h TTL cache) | CC BY-NC 4.0 (see below) |

Total: 20,000+ sanctioned individuals, entities, and vessels across four
jurisdictions.

> **Note on the UK list.** The UK HMT/OFSI Consolidated List was deprecated
> by the UK government on 28 January 2026 and replaced by the UK Sanctions
> List (UKSL), which OpenSanctions exposes as `gb_fcdo_sanctions`. This API
> therefore screens against the current UK list, not the frozen HMT feed.

## Endpoints

### GET /screen?name=John+Doe&threshold=0.7

Screen a name against all four sanctions lists. Returns matches above
threshold with match score, source, entity details, and matched AKA.

**Parameters:**
- `name` (required): Name to screen
- `threshold` (optional, default 0.7): Minimum match score (0.0-1.0)
- `refresh` (optional, default false): Force refresh sanctions data

**Example:**

```bash
curl "https://your-app.onrender.com/screen?name=Vladimir+Putin&threshold=0.7"
```

**Response (single-list match):**

```json
{
  "query": "Kim Jong",
  "threshold": 0.7,
  "total_matches": 1,
  "ofac_matches": 0,
  "un_matches": 1,
  "eu_matches": 0,
  "uk_matches": 0,
  "matches": [
    {
      "source": "UN Consolidated",
      "entity_id": "6908643",
      "name": "KIM JONG UN",
      "type": "Individual",
      "program": "2017-12-22",
      "remarks": "Gender: male",
      "matched_aka": null,
      "match_score": 0.85,
      "match_type": "name"
    }
  ],
  "data_updated": {
    "ofac": "2026-07-26T12:33:32+00:00",
    "un": "2026-07-26T12:33:10+00:00",
    "eu": "2026-07-26T12:34:00+00:00",
    "uk": "2026-07-26T12:34:00+00:00"
  },
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

**Response (multi-list match)** - the same name appears on several lists;
each list contributes its own entry to `matches`:

```json
{
  "query": "Vladimir Putin",
  "threshold": 0.7,
  "total_matches": 3,
  "ofac_matches": 1,
  "un_matches": 0,
  "eu_matches": 1,
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
      "match_type": "exact"
    },
    {
      "source": "EU Consolidated",
      "entity_id": "NK-eu-...",
      "name": "VLADIMIR PUTIN",
      "type": "Person",
      "program": "eu_fsf",
      "remarks": "President of Russia",
      "matched_aka": null,
      "match_score": 1.0,
      "match_type": "exact"
    },
    {
      "source": "UK FCDO",
      "entity_id": "NK-uk-...",
      "name": "VLADIMIR PUTIN",
      "type": "Person",
      "program": "gb_fcdo_sanctions",
      "remarks": "President of Russia",
      "matched_aka": "PUTIN, Vladimir Vladimirovich",
      "match_score": 1.0,
      "match_type": "aka"
    }
  ],
  "data_updated": {
    "ofac": "2026-07-26T12:33:32+00:00",
    "un": "2026-07-26T12:33:10+00:00",
    "eu": "2026-07-26T12:34:00+00:00",
    "uk": "2026-07-26T12:34:00+00:00"
  },
  "screened_at": "2026-07-26T12:35:12+00:00"
}
```

### GET /status

Returns data loading status and entity counts per source (`ofac`, `un`,
`eu`, `uk`).

### GET /health

Returns `{"status": "ok"}`.

## Match scoring

| Score | Match type | Example |
|---|---|---|
| 1.0 | exact | "Vladimir Putin" = "VLADIMIR PUTIN" (normalized) |
| 0.85 | starts_with / partial | "Kim Jong" matches "KIM JONG UN" |
| 0.7 | contains | "Al" in "AL FURQAN MEDIA" |
| 0.5-0.6 | token overlap | shared words between query and target |

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# or: source .venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8127
```

Note: First startup downloads ~28MB OFAC XML + ~2MB UN XML, plus ~15MB EU
and ~21MB UK OpenSanctions JSON Lines dumps. The OpenSanctions dumps are
cached to `data/opensanctions-eu.json` and `data/opensanctions-uk.json` and
reused for 24 hours. Set `SANCTIONS_CACHE_DIR` to relocate the cache.

### Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```

Tests use small fixture files in `tests/fixtures/` and never touch the
network, so they run in under a second.

## Deployment notes

### Self-hosted (Render / Railway / Fly.io / a VPS) - recommended

Works as-is. The on-disk cache in `data/` survives across warm invocations
and is refreshed once per day. Total disk usage for the four lists is around
~70MB.

### Vercel (serverless) - trade-off

Vercel serverless functions have an **ephemeral filesystem** and a default
1024MB memory / 10s execution budget (or 60s on Pro). The OpenSanctions
EU/UK dumps are ~36MB combined and take several seconds to download and
parse. This means:

- Every cold start re-downloads EU + UK dumps (no persistent disk cache).
- Cold starts may exceed the execution budget if all four lists are empty.
- The in-memory cache only helps within a single warm invocation.

For Vercel we recommend one of:
1. **Run a small subset.** Set `SANCTIONS_FIXTURE_DIR` and ship a trimmed
   JSON fixture (e.g. only EU + UK entities matching your customer base) so
   the function loads from the bundle instead of downloading.
2. **Use an external cache.** Put the parsed dumps in Upstash Redis or
   S3 and have the function read from there on cold start.
3. **Self-host** the API on Render/Railway/Fly.io where the filesystem
   persists between invocations. This is the simplest path.

The OFAC + UN XML feeds are smaller (~30MB combined) and load quickly, so
they work fine on Vercel even on cold start. The trade-off only applies to
the EU + UK OpenSanctions dumps.

## Attribution (required by the OpenSanctions license)

The EU and UK sanctions data is sourced from
[OpenSanctions](https://www.opensanctions.org/) and is licensed under the
[Creative Commons Attribution-NonCommercial 4.0 International
(CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/) license.

> **Data attribution:** EU and UK sanctions records in this API are derived
> from OpenSanctions (https://www.opensanctions.org/), a database of
> sanctions targets and persons of interest published under the
> Creative Commons Attribution-NonCommercial 4.0 International license.

### License terms you must respect

- **Non-commercial use** (academic research, hobby analysis, journalism,
  anti-corruption advocacy) is free under CC BY-NC 4.0.
- **Commercial use - including compliance screening of clients, suppliers,
  or counterparties - requires a data license from OpenSanctions.** See
  https://www.opensanctions.org/docs/commercial/exemption/ for details.
- Attribution must be retained in the API response, documentation, and any
  downstream product that exposes this data.

If you deploy this API commercially, you must obtain an OpenSanctions data
license, or replace the EU/UK loaders with official government feeds (the EU
FSF XML is published at `webgate.ec.europa.eu/fsd/fsf`; the UK Sanctions List
XML/CSV is published at `gov.uk/government/publications/financial-sanctions-consolidated-list-of-targets`).

## Pricing for RapidAPI

- Free: 100 requests/month
- Pro: $29/month - unlimited requests, all four sources
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