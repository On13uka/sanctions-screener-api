"""Sanctions Screener API - unified search across government sanctions lists.

Screens names against OFAC SDN (US Treasury), UN Consolidated List, EU
Consolidated Sanctions List, and UK FCDO Sanctions List. Returns unified
results with match score, source, and entity details.

Data sources:
- OFAC SDN List (US Treasury) - XML, refreshed daily
- UN Security Council Consolidated List - XML, refreshed daily
- EU Financial Sanctions Files (FSF) - via OpenSanctions, refreshed daily
- UK FCDO Sanctions List - via OpenSanctions, refreshed daily

License note (EU + UK data):
    The EU and UK datasets are sourced from OpenSanctions, which publishes
    bulk data under the Creative Commons Attribution-NonCommercial 4.0
    International (CC BY-NC 4.0) license. Non-commercial use is free;
    commercial use -- including compliance screening -- requires a data
    license from OpenSanctions. See:
        https://www.opensanctions.org/docs/commercial/exemption/
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

from . import opensanctions


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load sanctions data on startup.

    XML sources (OFAC, UN) are downloaded in parallel. OpenSanctions sources
    (EU, UK) are loaded from the on-disk cache if fresh; otherwise they are
    downloaded in a background thread so the server can start serving the
    already-cached OFAC/UN results without blocking on EU/UK downloads.
    """
    await asyncio.gather(_refresh_xml_cache("ofac"), _refresh_xml_cache("un"))
    # Kick off EU/UK loads in the background; they populate the cache when done.
    # If the cache is already fresh this returns quickly.
    asyncio.create_task(_refresh_opensanctions_source_async("eu"))
    asyncio.create_task(_refresh_opensanctions_source_async("uk"))
    yield


app = FastAPI(
    title="Sanctions Screener API",
    description=(
        "Screen names against OFAC SDN, UN Consolidated, EU Consolidated, "
        "and UK FCDO sanctions lists."
    ),
    version="1.1.0",
    lifespan=lifespan,
)

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
UN_CONSOLIDATED_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
REQUEST_TIMEOUT = 120.0
HEADERS = {"User-Agent": "SanctionsScreenerAPI/1.1 (contact: builder@api-portfolio.local)"}

# In-memory cache for the XML-backed sources (OFAC, UN).
_cache: dict[str, Any] = {
    "ofac": {"data": [], "updated": None, "loading": False},
    "un": {"data": [], "updated": None, "loading": False},
    # OpenSanctions-backed sources keep their own cache state here too, so
    # the /status endpoint can report them uniformly.
    "eu": {"data": [], "updated": None, "loading": False},
    "uk": {"data": [], "updated": None, "loading": False},
}

# Map our internal source keys to the `source` label returned in the API
# response. The OFAC and UN labels are preserved verbatim from v1.0 so
# existing clients keep working.
SOURCE_LABELS = {
    "ofac": "OFAC SDN",
    "un": "UN Consolidated",
    "eu": "EU Consolidated",
    "uk": "UK FCDO",
}

# Optional fixture paths for tests. Set SANCTIONS_FIXTURE_DIR to a folder
# containing opensanctions-eu.json / opensanctions-uk.json arrays to avoid
# network calls during tests.
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
    except Exception:
        pass
    finally:
        _cache[source]["loading"] = False


def _refresh_opensanctions_source(short: str) -> None:
    """Synchronously load an OpenSanctions-backed source into the cache.

    Uses the on-disk cache when fresh; otherwise downloads, parses, and writes
    the cache. In test mode (SANCTIONS_FIXTURE_DIR set) loads from a fixture
    file instead and never touches the network.
    """
    if _cache[short]["loading"]:
        return
    _cache[short]["loading"] = True
    try:
        if _FIXTURE_DIR:
            fixture_path = os.path.join(_FIXTURE_DIR, f"opensanctions-{short}.json")
            entities, updated = opensanctions.load_fixture(fixture_path)
        else:
            entities, updated = opensanctions.load_opensanctions(short)
        _cache[short]["data"] = entities
        _cache[short]["updated"] = updated
    except Exception:
        _cache[short]["data"] = _cache[short].get("data", []) or []
    finally:
        _cache[short]["loading"] = False


async def _refresh_opensanctions_source_async(short: str):
    """Async wrapper that runs the blocking OpenSanctions load in a thread."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _refresh_opensanctions_source, short)


async def _search_source(source: str, query: str, threshold: float) -> list[dict]:
    data = _cache[source]["data"]
    if not data:
        return []
    results = []
    label = SOURCE_LABELS.get(source, source.upper())
    for entity in data:
        score = _score_match(query, entity["name"])
        best_aka_score = 0.0
        best_aka = None
        for aka in entity.get("akas", []):
            aka_score = _score_match(query, aka)
            if aka_score > best_aka_score:
                best_aka_score = aka_score
                best_aka = aka
        final_score = max(score, best_aka_score)
        if final_score >= threshold:
            results.append({
                "source": label,
                "entity_id": entity["entity_id"],
                "name": entity["name"],
                "type": entity["type"],
                "program": entity["program"],
                "remarks": entity["remarks"],
                "matched_aka": best_aka if best_aka_score > score else None,
                "match_score": round(final_score, 2),
                "match_type": "exact"
                if final_score == 1.0
                else ("aka" if best_aka_score > score else "name"),
            })
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results[:50]  # limit to top 50


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
        "version": "1.1.0",
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
        }
    return out


@app.get("/screen")
async def screen(
    name: str = Query(..., description="Name to screen, e.g. 'John Doe' or 'company name'"),
    threshold: float = Query(0.7, ge=0.0, le=1.0, description="Minimum match score (0.0-1.0)"),
    refresh: bool = Query(False, description="Force refresh sanctions data before screening"),
):
    """Screen a name against all configured sanctions lists.

    The response is additive over v1.0: the existing `source`, `match_score`,
    `ofac_matches`, `un_matches`, and `matches` fields are preserved. New
    fields `eu_matches`, `uk_matches`, and per-source counts are added. Old
    clients reading just `source` and `match_score` keep working.
    """
    query = _normalize_name(name)

    # Refresh XML sources if asked or empty.
    if refresh or not _cache["ofac"]["data"]:
        await _refresh_xml_cache("ofac")
    if refresh or not _cache["un"]["data"]:
        await _refresh_xml_cache("un")
    # Refresh OpenSanctions sources if asked or empty (and not in fixture mode).
    if refresh or not _cache["eu"]["data"]:
        await _refresh_opensanctions_source_async("eu")
    if refresh or not _cache["uk"]["data"]:
        await _refresh_opensanctions_source_async("uk")

    ofac_results = await _search_source("ofac", query, threshold)
    un_results = await _search_source("un", query, threshold)
    eu_results = await _search_source("eu", query, threshold)
    uk_results = await _search_source("uk", query, threshold)
    all_results = ofac_results + un_results + eu_results + uk_results
    all_results.sort(key=lambda x: x["match_score"], reverse=True)

    return {
        "query": name,
        "threshold": threshold,
        "total_matches": len(all_results),
        # v1.0-compatible counts (preserved).
        "ofac_matches": len(ofac_results),
        "un_matches": len(un_results),
        # New counts (additive).
        "eu_matches": len(eu_results),
        "uk_matches": len(uk_results),
        "matches": all_results,
        "data_updated": {
            "ofac": _cache["ofac"]["updated"],
            "un": _cache["un"]["updated"],
            "eu": _cache["eu"]["updated"],
            "uk": _cache["uk"]["updated"],
        },
        "screened_at": datetime.now(timezone.utc).isoformat(),
    }