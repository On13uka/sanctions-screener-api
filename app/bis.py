"""US Bureau of Industry and Security (BIS) screening via the trade.gov
Consolidated Screening List (CSL).

The CSL is a single API endpoint hosted by the International Trade
Administration (ITA) at `https://api.trade.gov/gateway/v1/
consolidated_screening_list/search`. It consolidates multiple export-control
and sanctions screening lists from the Departments of Commerce, State, and
Treasury, including:

  * BIS Denied Persons List (DPL)
  * BIS Entity List
  * BIS Unverified List (UVL)
  * BIS Military End-User (MEU) List
  * State Department ITAR Debarred List
  * Treasury OFAC SDN (also covered separately by our OFAC loader)

All CSL data is published by US federal government agencies and is therefore
US public domain (17 USC 105) -- commercial use is permitted with attribution.

Authentication: a free API key from the ITA Developer Portal
(https://developer.trade.gov/) is required. Register, subscribe to "Data
Services Platform APIs", and copy the primary key from your Profile page.
Provide it via the `TRADE_GOV_API_KEY` environment variable. When the key is
missing the loader returns an empty result and surfaces a clear status warning
via the /status endpoint -- the rest of the API keeps working with OFAC/UN/EU/
UK data.

Caching: parsed results are cached to `data/bis-csl.json` with a 24h TTL,
matching the daily refresh cadence used for the EU/UK government feeds. The
trade.gov CSL itself is updated hourly, so a 24h cache is conservative but
keeps the request budget predictable.

This module is intentionally self-contained: it has its own download, parse,
and cache helpers (mirroring the pattern in `govfeeds.py`) so the BIS data path
is independent of the other four sources.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import certifi
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Short tag used by app/main.py to reference BIS data. The `source` label
# exposed to API clients lives in app/main.py SOURCE_LABELS as "BIS CSL".
SHORT = "bis"

# Free API key from the ITA Developer Portal. Required for the CSL endpoint.
API_KEY_ENV = "TRADE_GOV_API_KEY"

# CSL search endpoint. The `q` parameter performs a fuzzy name search; we page
# through the full list by using a wildcard-friendly query. The endpoint caps
# results at 1000 per page; we follow `total` pagination.
CSL_SEARCH_URL = (
    "https://api.trade.gov/gateway/v1/consolidated_screening_list/search"
)

# How long a cached dump is considered fresh (seconds). 24h matches the daily
# refresh cadence used for EU/UK feeds.
CACHE_TTL_SECONDS = 24 * 60 * 60

# HTTP settings. The CSL endpoint is fast; keep a tight timeout so a slow
# trade.gov response never blocks startup for long.
REQUEST_TIMEOUT = 60.0
HEADERS = {
    "User-Agent": "SanctionsScreenerAPI/1.3 (contact: builder@api-portfolio.local)"
}

# Directory used for on-disk cache files. Mirrors govfeeds.CACHE_DIR.
CACHE_DIR = os.environ.get("SANCTIONS_CACHE_DIR", "data")

# Page size cap for the CSL endpoint. The API returns up to 1000 results per
# call; we use a smaller batch to keep responses light.
PAGE_SIZE = 500

# Optional fixture path for tests. Set SANCTIONS_FIXTURE_DIR to a folder
# containing bis-fixture.json to avoid network calls during tests.
_FIXTURE_DIR = os.environ.get("SANCTIONS_FIXTURE_DIR", "")


# ---------------------------------------------------------------------------
# Cache helpers (mirrors govfeeds.py)
# ---------------------------------------------------------------------------

def _json_cache_path() -> str:
    return os.path.join(CACHE_DIR, f"{SHORT}-csl.json")


def _meta_path() -> str:
    return os.path.join(CACHE_DIR, f"{SHORT}-csl.meta.json")


def _is_cache_fresh() -> bool:
    meta = _meta_path()
    cache = _json_cache_path()
    if not (os.path.exists(meta) and os.path.exists(cache)):
        return False
    try:
        with open(meta, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    ts = data.get("fetched_at_epoch", 0)
    return ts > 0 and (time.time() - ts) < CACHE_TTL_SECONDS


def _read_json_cache() -> list[dict]:
    try:
        with open(_json_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_json_cache(entities: list[dict]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_json_cache_path(), "w", encoding="utf-8") as f:
        json.dump(entities, f)
    with open(_meta_path(), "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at_epoch": time.time(),
                "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
                "count": len(entities),
                "source": "US trade.gov Consolidated Screening List (BIS + others)",
                "license": "US public domain (17 USC 105)",
            },
            f,
        )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _api_key() -> str:
    """Return the trade.gov API key from env (BOM-stripped)."""
    key = os.environ.get(API_KEY_ENV, "")
    if key:
        return key.replace("\ufeff", "").strip()
    # Fall back to .env file if present (BOM-stripped).
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{API_KEY_ENV}="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def _csl_search(api_key: str, query: str, offset: int) -> dict[str, Any] | None:
    """Run a single CSL search page. Returns the parsed JSON or None on error."""
    params = {
        "api_key": api_key,
        "q": query,
        "limit": PAGE_SIZE,
        "offset": offset,
    }
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True,
            headers=HEADERS,
        ) as c:
            r = c.get(CSL_SEARCH_URL, params=params)
        if r.status_code != 200:
            return None
        return r.json()
    except (httpx.HTTPError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Parse CSL JSON -> normalized entity dicts
# ---------------------------------------------------------------------------
#
# The CSL search response shape (from the trade.gov docs + apis.io OpenAPI):
#   {
#     "total": <int>,
#     "sources": [ ... ],          # metadata about contributing lists
#     "results": [
#       {
#         "id": "...",
#         "name": "...",
#         "alt_names": "...",        # newline or comma separated
#         "entity_type": "..."|null, # individual / entity / etc.
#         "addresses": [ {"address":..., "city":..., "state":...,
#                         "postal_code":..., "country":...} ],
#         "source_info_url": "...",
#         "source_list_url": "...",
#         "federal_register_notice": "...",
#         "start_date": "...",
#         "exclusion_programs": "...",   # BIS-specific
#         "license_requirement": "...",  # BIS-specific
#         "license_policy": "...",       # BIS-specific
#         "lists": ["DPL", "EL", "UVL", "MEU", "ISN", "SDN", ...]
#       },
#       ...
#     ]
#   }
#
# The `lists` array tells us which sub-lists the party appears on. We keep
# every CSL record (one row per (party, source-list) match) but only treat the
# BIS sub-lists (DPL, EL, UVL, MEU) as our "BIS" source for the /screen
# endpoint. Other CSL lists (SDN, ISN, etc.) are reported in the
# `csl_lists` field of the cached entity for completeness but do not
# contribute to bis_matches -- OFAC SDN is already covered by our primary
# OFAC loader.

# CSL list codes we treat as BIS-managed (export control).
BIS_LIST_CODES = {"DPL", "EL", "UVL", "MEU"}

# Display names for the BIS sub-lists.
BIS_LIST_NAMES = {
    "DPL": "BIS Denied Persons List",
    "EL": "BIS Entity List",
    "UVL": "BIS Unverified List",
    "MEU": "BIS Military End-User List",
}


def _split_alt_names(raw: str) -> list[str]:
    if not raw:
        return []
    # alt_names is typically newline-separated; some lists use commas.
    parts = raw.replace("\r", "\n").replace(",", "\n").split("\n")
    return [p.strip() for p in parts if p.strip()]


def _parse_csl_results(results: list[dict]) -> list[dict]:
    """Convert raw CSL `results` rows into normalized entity dicts.

    One CSL row may appear on multiple lists; we emit one entity dict per row
    (preserving the `lists` array) so the /screen endpoint can match against
    the name and AKAs once, and the response can report which BIS sub-lists
    the hit falls under.
    """
    entities: list[dict] = []
    for row in results:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        lists = row.get("lists") or []
        if isinstance(lists, str):
            lists = [s.strip() for s in lists.split(",") if s.strip()]
        # Normalize lists to upper-case codes.
        lists = [str(l).upper() for l in lists if l]
        # Build a human-readable "program" string naming the BIS sub-lists
        # this row appears on (so the risk_verdict engine can mention it).
        bis_lists_on = [BIS_LIST_NAMES.get(l, l) for l in lists if l in BIS_LIST_CODES]
        program = "; ".join(bis_lists_on) if bis_lists_on else "; ".join(lists)
        # Compose remarks from BIS-specific fields when present.
        remarks_bits = []
        for k in ("license_requirement", "license_policy", "exclusion_programs"):
            v = (row.get(k) or "").strip()
            if v:
                remarks_bits.append(f"{k.replace('_', ' ').title()}: {v}")
        addr = row.get("addresses") or []
        if isinstance(addr, list) and addr:
            a = addr[0]
            country = (a.get("country") or "").strip()
            city = (a.get("city") or "").strip()
            if country or city:
                remarks_bits.append(f"Location: {city}, {country}".strip(", "))
        remarks = "; ".join(remarks_bits)
        entity_type = (row.get("entity_type") or "").strip().title() or "Entity"
        entities.append({
            "entity_id": str(row.get("id") or row.get("name") or ""),
            "name": name,
            "type": entity_type,
            "program": program or "BIS CSL",
            "remarks": remarks,
            "akas": _split_alt_names(row.get("alt_names") or ""),
            "lists": lists,  # raw CSL list codes for downstream reporting
        })
    return entities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_fixture(path: str) -> tuple[list[dict], str | None]:
    """Load a small JSON fixture for tests instead of downloading."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, datetime.now(timezone.utc).isoformat()
    except (OSError, json.JSONDecodeError):
        pass
    return [], None


def load_feed() -> tuple[list[dict], str | None, str | None]:
    """Load BIS CSL entities.

    Returns (entities, updated_iso_or_none, status_message_or_none).
    `status_message` is set when the feed could not be downloaded (e.g. the
    trade.gov API key is missing) so the caller can surface it via /status.
    """
    # Test fixture mode: never touch the network.
    if _FIXTURE_DIR:
        fixture_path = os.path.join(_FIXTURE_DIR, f"{SHORT}-fixture.json")
        entities, updated = load_fixture(fixture_path)
        status = None if entities else f"fixture {fixture_path} empty or missing"
        return entities, updated, status

    if _is_cache_fresh():
        cached = _read_json_cache()
        if cached:
            try:
                with open(_meta_path(), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return cached, updated, None

    api_key = _api_key()
    if not api_key:
        msg = (
            "BIS CSL feed unavailable: missing TRADE_GOV_API_KEY env var. "
            "Register a free key at https://developer.trade.gov/ and set it "
            "in the environment. BIS screening returns empty until then."
        )
        stale = _read_json_cache()
        if stale:
            try:
                with open(_meta_path(), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return stale, updated, msg
        return [], None, msg

    # Page through the full CSL. The endpoint supports a wildcard-friendly
    # `q` parameter; an empty-ish query ("a") with a high limit returns the
    # broadest set. We use `q="*"` which the CSL search treats as "match all".
    all_rows: list[dict] = []
    offset = 0
    total = None
    seen = 0
    while True:
        page = _csl_search(api_key, "*", offset)
        if page is None:
            break
        results = page.get("results") or []
        all_rows.extend(results)
        if total is None:
            total = page.get("total") or len(results)
        seen += len(results)
        if not results or seen >= total:
            break
        offset += PAGE_SIZE
        # Safety cap to avoid infinite loops on malformed pagination.
        if offset > 100000:
            break

    if not all_rows:
        stale = _read_json_cache()
        if stale:
            return stale, None, "CSL returned 0 rows; serving stale cache"
        return [], None, "BIS CSL returned 0 rows"

    entities = _parse_csl_results(all_rows)
    if not entities:
        return [], None, "BIS CSL parse produced 0 entities"
    _write_json_cache(entities)
    updated = datetime.now(timezone.utc).isoformat()
    return entities, updated, None


def attribution() -> str:
    """Return the attribution string required by the data license."""
    return (
        "Source: US trade.gov Consolidated Screening List (CSL), "
        "International Trade Administration. US public domain (17 USC 105). "
        "https://www.trade.gov/consolidated-screening-list"
    )