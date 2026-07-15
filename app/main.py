"""Sanctions Screener API - unified search across government sanctions lists.

Screens names against OFAC SDN (US Treasury) and UN Consolidated List.
Returns unified results with match score, source, and entity details.

Data sources:
- OFAC SDN List (US Treasury) - XML, refreshed daily
- UN Security Council Consolidated List - XML, refreshed daily
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
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Sanctions Screener API",
    description="Screen names against OFAC SDN and UN consolidated sanctions lists.",
    version="1.0.0",
)

OFAC_SDN_URL = "https://www.treasury.gov/ofac/downloads/sdn.xml"
UN_CONSOLIDATED_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
REQUEST_TIMEOUT = 120.0
HEADERS = {"User-Agent": "SanctionsScreenerAPI/1.0 (contact: builder@api-portfolio.local)"}

# In-memory cache
_cache: dict[str, Any] = {
    "ofac": {"data": [], "updated": None, "loading": False},
    "un": {"data": [], "updated": None, "loading": False},
}


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
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True, headers=HEADERS) as c:
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
                    aka_name = " ".join(filter(None, [aka.get("firstName",""), aka.get("lastName","")])).strip()
                    if aka_name:
                        akas.append(aka_name)
            entities.append({
                "entity_id": entry.get("sdnEntryNumber") or entry.get("uid"),
                "name": name,
                "type": entry.get("sdnType"),
                "program": entry.get("programList", {}).get("program", "") if isinstance(entry.get("programList"), dict) else "",
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


async def _refresh_cache(source: str):
    """Download and update cache for a source."""
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


async def _search_source(source: str, query: str, threshold: float) -> list[dict]:
    data = _cache[source]["data"]
    if not data:
        return []
    results = []
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
                "source": "OFAC SDN" if source == "ofac" else "UN Consolidated",
                "entity_id": entity["entity_id"],
                "name": entity["name"],
                "type": entity["type"],
                "program": entity["program"],
                "remarks": entity["remarks"],
                "matched_aka": best_aka if best_aka_score > score else None,
                "match_score": round(final_score, 2),
                "match_type": "exact" if final_score == 1.0 else ("aka" if best_aka_score > score else "name"),
            })
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results[:50]  # limit to top 50


@app.on_event("startup")
async def startup_load():
    """Load sanctions data on startup."""
    await asyncio.gather(_refresh_cache("ofac"), _refresh_cache("un"))


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
        "version": "1.0.0",
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
    return {
        "ofac": {
            "loaded": len(_cache["ofac"]["data"]),
            "updated": _cache["ofac"]["updated"],
            "loading": _cache["ofac"]["loading"],
        },
        "un": {
            "loaded": len(_cache["un"]["data"]),
            "updated": _cache["un"]["updated"],
            "loading": _cache["un"]["loading"],
        },
    }


@app.get("/screen")
async def screen(
    name: str = Query(..., description="Name to screen, e.g. 'John Doe' or 'company name'"),
    threshold: float = Query(0.7, ge=0.0, le=1.0, description="Minimum match score (0.0-1.0)"),
    refresh: bool = Query(False, description="Force refresh sanctions data before screening"),
):
    query = _normalize_name(name)
    if refresh or not _cache["ofac"]["data"]:
        await _refresh_cache("ofac")
    if refresh or not _cache["un"]["data"]:
        await _refresh_cache("un")

    ofac_results = await _search_source("ofac", query, threshold)
    un_results = await _search_source("un", query, threshold)
    all_results = ofac_results + un_results
    all_results.sort(key=lambda x: x["match_score"], reverse=True)

    return {
        "query": name,
        "threshold": threshold,
        "total_matches": len(all_results),
        "ofac_matches": len(ofac_results),
        "un_matches": len(un_results),
        "matches": all_results,
        "data_updated": {
            "ofac": _cache["ofac"]["updated"],
            "un": _cache["un"]["updated"],
        },
        "screened_at": datetime.now(timezone.utc).isoformat(),
    }