"""Sanctions Screener API - unified search across government sanctions lists.

Screens names against OFAC SDN (US Treasury), UN Consolidated List, EU
Financial Sanctions Files (FSF), and the UK FCDO Sanctions List (UKSL).
Returns unified results with match score, source, entity details, an
explainable match explanation, and a plain-English risk verdict.

Data sources (all commercially usable):
- OFAC SDN List (US Treasury) - direct XML feed, US public domain (17 USC 105)
- UN Security Council Consolidated List - direct XML feed, UN public data
- EU Financial Sanctions Files (FSF) - official EU FSF XML (auth-gated free EU
  Login token via EU_FSF_TOKEN). European Commission reuse policy permits
  commercial reuse with attribution.
- UK FCDO Sanctions List (UKSL) - official static XML at
  sanctionslist.fcdo.gov.uk, Open Government Licence v3.0 (commercial OK).

OpenSanctions is intentionally NOT used: its bulk data is CC BY-NC 4.0 and
cannot be used for commercial compliance screening without a paid data
license. The EU/UK loaders pull directly from the official government feeds.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

import certifi
import httpx
import xmltodict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import govfeeds


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load sanctions data on startup.

    XML sources (OFAC, UN) are downloaded in parallel. Official government
    feed sources (EU FSF, UK FCDO) are loaded from the on-disk cache if fresh;
    otherwise they are downloaded in a background thread so the server can
    start serving the already-cached OFAC/UN results without blocking on
    EU/UK downloads.
    """
    await asyncio.gather(_refresh_xml_cache("ofac"), _refresh_xml_cache("un"))
    # Kick off EU/UK loads in the background; they populate the cache when done.
    # If the cache is already fresh this returns quickly.
    asyncio.create_task(_refresh_govfeed_async("eu"))
    asyncio.create_task(_refresh_govfeed_async("uk"))
    yield


app = FastAPI(
    title="Sanctions Screener API",
    description=(
        "Screen names against OFAC SDN, UN Consolidated, EU FSF, "
        "and UK FCDO sanctions lists."
    ),
    version="1.2.0",
    lifespan=lifespan,
)

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
UN_CONSOLIDATED_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
REQUEST_TIMEOUT = 120.0
HEADERS = {"User-Agent": "SanctionsScreenerAPI/1.2 (contact: builder@api-portfolio.local)"}

# In-memory cache for the XML-backed sources (OFAC, UN).
_cache: dict[str, Any] = {
    "ofac": {"data": [], "updated": None, "loading": False, "status": None},
    "un": {"data": [], "updated": None, "loading": False, "status": None},
    # Official government feed sources keep their own cache state here too, so
    # the /status endpoint can report them uniformly.
    "eu": {"data": [], "updated": None, "loading": False, "status": None},
    "uk": {"data": [], "updated": None, "loading": False, "status": None},
}

# Map our internal source keys to the `source` label returned in the API
# response. The OFAC and UN labels are preserved verbatim from v1.0 so
# existing clients keep working. The EU/UK labels are preserved from v1.1
# ("EU Consolidated", "UK FCDO") so v1.1 clients keep working too -- only the
# underlying data source changes (OpenSanctions -> official government feeds).
SOURCE_LABELS = {
    "ofac": "OFAC SDN",
    "un": "UN Consolidated",
    "eu": "EU Consolidated",
    "uk": "UK FCDO",
}

# Optional fixture paths for tests. Set SANCTIONS_FIXTURE_DIR to a folder
# containing eu-fixture.json / uk-fixture.json arrays to avoid network calls
# during tests.
_FIXTURE_DIR = os.environ.get("SANCTIONS_FIXTURE_DIR", "")


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message


def _normalize_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 500:
        raise APIError(422, "invalid_name", "Provide a non-empty name (max 500 chars)")
    return name.lower()


# ---------------------------------------------------------------------------
# Match scoring + explanation
# ---------------------------------------------------------------------------
#
# Scoring (unchanged from v1.1 for backward compat):
#   exact=1.0, starts_with=0.85, contains=0.7, token overlap=0.5-0.6
#
# New in v1.2: every match carries a `match_explanation` object describing
# which field matched (name / aka), which value, the match_type (exact /
# fuzzy / partial / token), and the tokens that overlapped. The top-level
# `risk_verdict` is derived from the highest-severity match.

def _score_match(query: str, target: str) -> float:
    """Simple match scoring: exact=1.0, starts_with=0.85, contains=0.7, fuzzy=0.5."""
    query = query.lower().strip()
    target = target.lower().strip()
    if not target:
        return 0.0
    if query == target:
        return 1.0
    if target.startswith(query) or query.startswith(target):
        return 0.85
    if query in target or target in query:
        return 0.7
    # Token overlap
    q_tokens = set(query.split())
    t_tokens = set(target.split())
    overlap = len(q_tokens & t_tokens)
    if overlap > 0:
        return min(0.6, overlap / max(len(q_tokens), len(t_tokens)))
    return 0.0


def _classify_match(score: float) -> str:
    if score >= 1.0:
        return "exact"
    if score >= 0.85:
        return "fuzzy"
    if score >= 0.7:
        return "partial"
    if score > 0.0:
        return "token"
    return "none"


def _tokens_matched(query: str, target: str) -> list[str]:
    """Return the sorted list of tokens shared between query and target."""
    q_tokens = set(query.lower().split())
    t_tokens = set(target.lower().split())
    shared = q_tokens & t_tokens
    return sorted(shared)


def _build_explanation(
    matched_field: str,
    matched_value: str,
    score: float,
    query: str,
) -> dict:
    return {
        "matched_field": matched_field,  # "name" | "aka"
        "matched_value": matched_value,
        "match_type": _classify_match(score),  # exact|fuzzy|partial|token|none
        "tokens_matched": _tokens_matched(query, matched_value),
    }


# ---------------------------------------------------------------------------
# XML download + parse (OFAC, UN) -- unchanged
# ---------------------------------------------------------------------------

async def _download_and_parse(url: str, source: str) -> list[dict]:
    """Download XML, parse, return list of entity dicts."""
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True,
        headers=HEADERS,
    ) as c:
        r = await c.get(url)
    if r.status_code != 200:
        return []
    # Write to temp file for xmltodict streaming
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False, mode="wb") as f:
        f.write(r.content)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as f:
            data = xmltodict.parse(f)
    finally:
        os.unlink(tmp_path)

    entities = []
    if source == "ofac":
        root = data.get("sdnList", {})
        entries = root.get("sdnEntry", [])
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries:
            name_parts = []
            first = entry.get("firstName")
            last = entry.get("lastName")
            if first:
                name_parts.append(first)
            if last:
                name_parts.append(last)
            name = " ".join(name_parts).strip()
            if not name:
                name = entry.get("sdnType", "")
            aka_list = entry.get("akaList", {})
            akas = []
            if aka_list and aka_list.get("aka"):
                aka_items = aka_list["aka"]
                if isinstance(aka_items, dict):
                    aka_items = [aka_items]
                for aka in aka_items:
                    aka_name = " ".join(
                        filter(None, [aka.get("firstName", ""), aka.get("lastName", "")])
                    ).strip()
                    if aka_name:
                        akas.append(aka_name)
            entities.append({
                "entity_id": entry.get("sdnEntryNumber") or entry.get("uid"),
                "name": name,
                "type": entry.get("sdnType"),
                "program": entry.get("programList", {}).get("program", "")
                if isinstance(entry.get("programList"), dict)
                else "",
                "remarks": entry.get("remarks", ""),
                "akas": akas,
            })
    elif source == "un":
        root = data.get("CONSOLIDATED_LIST", {})
        individuals = root.get("INDIVIDUALS", {})
        entities_list = individuals.get("INDIVIDUAL", [])
        if isinstance(entities_list, dict):
            entities_list = [entities_list]
        for entry in entities_list:
            first = entry.get("FIRST_NAME", "")
            second = entry.get("SECOND_NAME", "")
            third = entry.get("THIRD_NAME", "")
            name = " ".join(filter(None, [first, second, third])).strip()
            aka_list = entry.get("INDIVIDUAL_ALIAS", [])
            akas = []
            if isinstance(aka_list, dict):
                aka_list = [aka_list]
            for aka in aka_list:
                alias = aka.get("ALIAS_NAME", "")
                if alias:
                    akas.append(alias)
            entities.append({
                "entity_id": entry.get("DATAID"),
                "name": name,
                "type": "Individual",
                "program": entry.get("LISTED_ON", ""),
                "remarks": entry.get("COMMENTS1", ""),
                "akas": akas,
            })
        entities_corp = root.get("ENTITIES", {})
        corp_list = entities_corp.get("ENTITY", [])
        if isinstance(corp_list, dict):
            corp_list = [corp_list]
        for entry in corp_list:
            name = entry.get("FIRST_NAME", "").strip()
            aka_list = entry.get("ENTITY_ALIAS", [])
            akas = []
            if isinstance(aka_list, dict):
                aka_list = [aka_list]
            for aka in aka_list:
                alias = aka.get("ALIAS_NAME", "")
                if alias:
                    akas.append(alias)
            entities.append({
                "entity_id": entry.get("DATAID"),
                "name": name,
                "type": "Entity",
                "program": entry.get("LISTED_ON", ""),
                "remarks": entry.get("COMMENTS1", ""),
                "akas": akas,
            })
    return entities


async def _refresh_xml_cache(source: str):
    """Download and update the in-memory cache for an XML-backed source."""
    if _cache[source]["loading"]:
        return
    _cache[source]["loading"] = True
    try:
        url = OFAC_SDN_URL if source == "ofac" else UN_CONSOLIDATED_URL
        entities = await _download_and_parse(url, source)
        _cache[source]["data"] = entities
        _cache[source]["updated"] = datetime.now(timezone.utc).isoformat()
        _cache[source]["status"] = None
    except Exception as exc:
        _cache[source]["status"] = f"load error: {exc}"
    finally:
        _cache[source]["loading"] = False


# ---------------------------------------------------------------------------
# Official government feed loaders (EU FSF, UK FCDO)
# ---------------------------------------------------------------------------

def _refresh_govfeed(short: str) -> None:
    """Synchronously load an official-government-feed source into the cache.

    Uses the on-disk cache when fresh; otherwise downloads, parses, and writes
    the cache. In test mode (SANCTIONS_FIXTURE_DIR set) loads from a fixture
    file instead and never touches the network.
    """
    if _cache[short]["loading"]:
        return
    _cache[short]["loading"] = True
    try:
        if _FIXTURE_DIR:
            fixture_path = os.path.join(_FIXTURE_DIR, f"{short}-fixture.json")
            entities, updated = govfeeds.load_fixture(fixture_path)
            status = None
            if not entities:
                status = f"fixture {fixture_path} empty or missing"
        else:
            entities, updated, status = govfeeds.load_feed(short)
        _cache[short]["data"] = entities
        _cache[short]["updated"] = updated
        _cache[short]["status"] = status
    except Exception as exc:
        _cache[short]["data"] = _cache[short].get("data", []) or []
        _cache[short]["status"] = f"load error: {exc}"
    finally:
        _cache[short]["loading"] = False


async def _refresh_govfeed_async(short: str):
    """Async wrapper that runs the blocking govfeed load in a thread."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _refresh_govfeed, short)


# ---------------------------------------------------------------------------
# Search + risk verdict
# ---------------------------------------------------------------------------

async def _search_source(source: str, query: str, threshold: float) -> list[dict]:
    data = _cache[source]["data"]
    if not data:
        return []
    results = []
    label = SOURCE_LABELS.get(source, source.upper())
    for entity in data:
        name_score = _score_match(query, entity["name"])
        best_aka_score = 0.0
        best_aka = None
        for aka in entity.get("akas", []):
            aka_score = _score_match(query, aka)
            if aka_score > best_aka_score:
                best_aka_score = aka_score
                best_aka = aka
        # Decide which field "won" the match.
        if best_aka_score > name_score:
            final_score = best_aka_score
            matched_field = "aka"
            matched_value = best_aka or ""
        else:
            final_score = name_score
            matched_field = "name"
            matched_value = entity["name"]
        if final_score >= threshold:
            explanation = _build_explanation(
                matched_field, matched_value, final_score, query
            )
            results.append({
                "source": label,
                "entity_id": entity["entity_id"],
                "name": entity["name"],
                "type": entity["type"],
                "program": entity["program"],
                "remarks": entity["remarks"],
                "matched_aka": best_aka if matched_field == "aka" else None,
                "match_score": round(final_score, 2),
                "match_type": _classify_match(final_score),
                # New in v1.2 -- explainable match.
                "match_explanation": explanation,
            })
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results[:50]  # limit to top 50


def _risk_verdict(all_results: list[dict], screened_lists: int) -> str:
    """Generate a one-line plain-English risk verdict from the matches.

    Severity ordering:
      exact on any list  -> HIGH RISK
      fuzzy >=0.85       -> MEDIUM RISK
      fuzzy 0.7-0.85     -> LOW RISK (manual review)
      no match           -> CLEAN
    """
    if not all_results:
        return f"CLEAN: no matches found across {screened_lists} lists screened"

    # Pick the highest-severity match. exact > fuzzy(>=0.85) > fuzzy(0.7-0.85).
    def severity(m: dict) -> int:
        s = m["match_score"]
        if s >= 1.0:
            return 3
        if s >= 0.85:
            return 2
        if s >= 0.7:
            return 1
        return 0

    top = max(all_results, key=severity)
    score = top["match_score"]
    source = top["source"]
    expl = top.get("match_explanation") or {}
    matched_field = expl.get("matched_field", "name")
    matched_value = expl.get("matched_value", top.get("name", ""))
    match_type = top.get("match_type", "")
    program = top.get("program", "") or ""
    remarks = top.get("remarks", "") or ""

    if score >= 1.0:
        # HIGH RISK: exact match. Include program/remarks if available.
        detail_bits = []
        if program:
            detail_bits.append(f"listed under {program}")
        if remarks:
            # Trim remarks to a reasonable one-liner.
            short_remarks = remarks.split(";")[0].strip()
            if len(short_remarks) > 120:
                short_remarks = short_remarks[:117] + "..."
            if short_remarks:
                detail_bits.append(f"({short_remarks})")
        via = ""
        if matched_field == "aka":
            via = f" via alias '{matched_value}'"
        detail = ", ".join(detail_bits)
        if detail:
            return f"HIGH RISK: exact match on {source}{via}, {detail}"
        return f"HIGH RISK: exact match on {source}{via}"

    if score >= 0.85:
        pct = int(round(score * 100))
        via = f" via {matched_field}"
        if matched_field == "aka" and matched_value:
            via = f" via alias '{matched_value}'"
        return (
            f"MEDIUM RISK: fuzzy match ({pct}% confidence) on {source}{via}"
        )

    if score >= 0.7:
        pct = int(round(score * 100))
        via = f" via {matched_field}"
        if matched_field == "aka" and matched_value:
            via = f" via alias '{matched_value}'"
        return (
            f"LOW RISK: possible match ({pct}% confidence) on {source}{via}, "
            "recommend manual review"
        )

    # Below threshold but somehow in results (shouldn't happen) -- treat as low.
    pct = int(round(score * 100))
    return f"LOW RISK: weak match ({pct}% confidence) on {source}, recommend manual review"


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.get("/")
async def root():
    return {
        "name": "Sanctions Screener API",
        "version": "1.2.0",
        "sources": ["OFAC SDN", "UN Consolidated", "EU Consolidated", "UK FCDO"],
        "endpoints": {
            "screen": "/screen?name=John+Doe&threshold=0.7",
            "status": "/status",
            "health": "/health",
        },
        "docs": "/docs",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    """Return data loading status and entity counts per source."""
    out: dict[str, Any] = {}
    for key in ("ofac", "un", "eu", "uk"):
        out[key] = {
            "loaded": len(_cache[key]["data"]),
            "updated": _cache[key]["updated"],
            "loading": _cache[key]["loading"],
            "status": _cache[key].get("status"),
        }
    return out


@app.get("/screen")
async def screen(
    name: str = Query(..., description="Name to screen, e.g. 'John Doe' or 'company name'"),
    threshold: float = Query(0.7, ge=0.0, le=1.0, description="Minimum match score (0.0-1.0)"),
    refresh: bool = Query(False, description="Force refresh sanctions data before screening"),
):
    """Screen a name against all configured sanctions lists.

    The response is additive over v1.0/v1.1: the existing `source`,
    `match_score`, `ofac_matches`, `un_matches`, `eu_matches`, `uk_matches`,
    and `matches` fields are preserved. New in v1.2: each match carries a
    `match_explanation` object (which field matched, which value, match_type,
    tokens matched) and the top-level `risk_verdict` gives a one-line
    plain-English verdict. Old clients reading just `source` and
    `match_score` keep working.
    """
    query = _normalize_name(name)

    # Refresh XML sources if asked or empty.
    if refresh or not _cache["ofac"]["data"]:
        await _refresh_xml_cache("ofac")
    if refresh or not _cache["un"]["data"]:
        await _refresh_xml_cache("un")
    # Refresh official government feed sources if asked or empty.
    if refresh or not _cache["eu"]["data"]:
        await _refresh_govfeed_async("eu")
    if refresh or not _cache["uk"]["data"]:
        await _refresh_govfeed_async("uk")

    ofac_results = await _search_source("ofac", query, threshold)
    un_results = await _search_source("un", query, threshold)
    eu_results = await _search_source("eu", query, threshold)
    uk_results = await _search_source("uk", query, threshold)
    all_results = ofac_results + un_results + eu_results + uk_results
    all_results.sort(key=lambda x: x["match_score"], reverse=True)

    screened_lists = 4
    verdict = _risk_verdict(all_results, screened_lists)

    return {
        "query": name,
        "threshold": threshold,
        "total_matches": len(all_results),
        # v1.0-compatible counts (preserved).
        "ofac_matches": len(ofac_results),
        "un_matches": len(un_results),
        # v1.1 additive counts (preserved).
        "eu_matches": len(eu_results),
        "uk_matches": len(uk_results),
        "matches": all_results,
        # New in v1.2: plain-English risk verdict.
        "risk_verdict": verdict,
        "data_updated": {
            "ofac": _cache["ofac"]["updated"],
            "un": _cache["un"]["updated"],
            "eu": _cache["eu"]["updated"],
            "uk": _cache["uk"]["updated"],
        },
        "screened_at": datetime.now(timezone.utc).isoformat(),
    }