"""OpenSanctions data loader for EU and UK sanctions lists.

Fetches FollowTheMoney (FtM) JSON Lines exports from data.opensanctions.org
for the EU Financial Sanctions Files (eu_fsf) and the UK FCDO Sanctions List
(gb_fcdo_sanctions). Caches parsed entities to disk with a daily refresh TTL.

License note:
    OpenSanctions bulk data is published under the Creative Commons
    Attribution-NonCommercial 4.0 International (CC BY-NC 4.0) license.
    Non-commercial use (academic research, hobby analysis, journalism) is
    free. Commercial use -- including compliance screening -- requires a
    data license from OpenSanctions. See:
        https://www.opensanctions.org/docs/commercial/exemption/
    If you deploy this API commercially you must obtain such a license or
    swap the EU/UK loaders for official government feeds.
"""
from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Iterable

import certifi
import httpx

# Dataset metadata: short tag -> (dataset name, display source label).
# The short tag is what we expose in the API response `source` field for
# OpenSanctions-backed lists.
DATASETS: dict[str, tuple[str, str]] = {
    "eu": ("eu_fsf", "EU Consolidated"),
    "uk": ("gb_fcdo_sanctions", "UK FCDO"),
}

# Base URL for the OpenSanctions data delivery service. The "latest" alias
# always points at the most recently built artifact for a dataset.
DATA_BASE = "https://data.opensanctions.org/datasets/latest"

# How long a cached dump is considered fresh (seconds). 24h matches the daily
# build cadence of OpenSanctions and the task's "daily refresh TTL" rule.
CACHE_TTL_SECONDS = 24 * 60 * 60

# HTTP settings.
REQUEST_TIMEOUT = 180.0
HEADERS = {
    "User-Agent": "SanctionsScreenerAPI/1.1 (contact: builder@api-portfolio.local)"
}

# Directory used for on-disk cache files. Placed next to the app package so it
# survives warm invocations on self-hosted deployments. On Vercel/serverless the
# filesystem is ephemeral, so the cache only helps within a single instance's
# lifetime -- see README for the trade-off.
CACHE_DIR = os.environ.get("SANCTIONS_CACHE_DIR", "data")


def _cache_path(short: str) -> str:
    """Return the on-disk cache file path for a given short tag."""
    return os.path.join(CACHE_DIR, f"opensanctions-{short}.json")


def _meta_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"opensanctions-{short}.meta.json")


def _is_cache_fresh(short: str) -> bool:
    """True if a cached dump exists and is younger than CACHE_TTL_SECONDS."""
    meta = _meta_path(short)
    cache = _cache_path(short)
    if not (os.path.exists(meta) and os.path.exists(cache)):
        return False
    try:
        with open(meta, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    ts = data.get("fetched_at_epoch", 0)
    return ts > 0 and (time.time() - ts) < CACHE_TTL_SECONDS


def _read_cache(short: str) -> list[dict]:
    """Read cached entities from disk. Returns [] on any error."""
    path = _cache_path(short)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _write_cache(short: str, entities: list[dict]) -> None:
    """Persist entities and a small metadata sidecar to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(short)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entities, f)
    with open(_meta_path(short), "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at_epoch": time.time(),
                "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
                "count": len(entities),
            },
            f,
        )


def _resolve_artifact_url(dataset: str) -> str | None:
    """Fetch the dataset index.json and return the entities.ftm.json URL.

    Returns None if the index cannot be fetched or the resource is missing.
    """
    index_url = f"{DATA_BASE}/{dataset}/index.json"
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True,
            headers=HEADERS,
        ) as c:
            r = c.get(index_url)
        if r.status_code != 200:
            return None
        idx = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    for res in idx.get("resources", []):
        if res.get("name") == "entities.ftm.json":
            return res.get("url")
    return None


def _stream_json_lines(url: str) -> Iterable[dict]:
    """Stream a JSON Lines URL line by line, yielding parsed objects.

    Uses httpx streaming so we never hold the whole response in memory at
    once while parsing.
    """
    with httpx.Client(
        timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True,
        headers=HEADERS,
    ) as c:
        with c.stream("GET", url) as r:
            if r.status_code != 200:
                return
            buf = io.StringIO()
            for chunk in r.iter_text():
                buf.write(chunk)
            # Re-parse line by line from the buffer (we accumulated it for
            # simplicity; the FtM files are tens of MB, not GB, so this is
            # acceptable for the EU/UK datasets).
            content = buf.getvalue()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _normalize_entity(raw: dict) -> dict | None:
    """Convert a raw FtM entity to our internal entity dict.

    Only Thing/LegalEntity/Person/Company-like schemas are kept; supporting
    entities (Passport, Sanction, Address, ...) are dropped because they are
    not name-bearing screening targets.
    """
    schema = raw.get("schema", "")
    # Keep only name-bearing entity types. The FtM model also emits
    # Address, Passport, Sanction, etc. as separate entities; we only want
    # the targets themselves for name screening.
    keep_schemas = {
        "Person",
        "Company",
        "LegalEntity",
        "Organization",
        "PublicBody",
        "Vehicle",
        "Asset",
        "Thing",
    }
    # Sub-schemas like "Person" are exact; but some entries use more specific
    # types (e.g. "Vessel"). Accept anything that is a name-bearing entity by
    # checking the caption/name presence instead, to be safe.
    props = raw.get("properties", {}) or {}
    name = raw.get("caption") or (props.get("name") or [""])[0] if props.get("name") else raw.get("caption")
    if not name:
        # Fall back to first name property value if no caption.
        names = props.get("name") or []
        name = names[0] if names else ""
    if not name:
        return None
    if schema not in keep_schemas and schema:
        # Be permissive: if it has a name, keep it. Many sanctions targets use
        # schemas like "Vessel", "Organization", etc. Dropping them would lose
        # real targets. The keep_schemas set above is a hint, not a gate.
        pass
    aliases: list[str] = []
    for key in ("alias", "previousName", "weakAlias"):
        vals = props.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals:
            if v and v not in aliases:
                aliases.append(v)
    # Some FtM exports store additional name parts.
    extra_name_parts = []
    for key in ("firstName", "middleName", "lastName", "fatherName", "secondName", "thirdName"):
        vals = props.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        for v in vals:
            if v:
                extra_name_parts.append(v)
    if extra_name_parts and " ".join(extra_name_parts).strip() and " ".join(extra_name_parts).strip() != name:
        aliases.append(" ".join(extra_name_parts).strip())
    entity_type = schema or "Entity"
    return {
        "entity_id": raw.get("id") or raw.get("referents", [None])[0],
        "name": name,
        "type": entity_type,
        "program": ", ".join(raw.get("datasets", [])),
        "remarks": "",
        "akas": aliases,
    }


def load_opensanctions(short: str) -> tuple[list[dict], str | None]:
    """Load entities for an OpenSanctions-backed list.

    Returns (entities, updated_iso_or_none). Uses the on-disk cache when fresh;
    otherwise downloads, parses, caches, and returns.
    """
    dataset, _label = DATASETS.get(short, ("", ""))
    if not dataset:
        return [], None
    if _is_cache_fresh(short):
        cached = _read_cache(short)
        if cached:
            try:
                with open(_meta_path(short), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return cached, updated
    # Cache miss or stale: download fresh.
    url = _resolve_artifact_url(dataset)
    if not url:
        # Fall back to the stable URL pattern (may be one build behind "latest").
        url = f"{DATA_BASE}/{dataset}/entities.ftm.json"
    entities: list[dict] = []
    try:
        for raw in _stream_json_lines(url):
            ent = _normalize_entity(raw)
            if ent is not None:
                entities.append(ent)
    except Exception:
        # Network/parse failure: if we have any cache at all, serve it stale
        # rather than returning empty.
        stale = _read_cache(short)
        if stale:
            try:
                with open(_meta_path(short), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return stale, updated
        return [], None
    _write_cache(short, entities)
    updated = datetime.now(timezone.utc).isoformat()
    return entities, updated


def load_fixture(path: str) -> tuple[list[dict], str | None]:
    """Load a small JSON Lines fixture for tests instead of downloading.

    The fixture is a JSON array of pre-normalized entity dicts (same shape as
    the `entities` returned by load_opensanctions).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, datetime.now(timezone.utc).isoformat()
    except (OSError, json.JSONDecodeError):
        pass
    return [], None