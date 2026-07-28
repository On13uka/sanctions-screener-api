"""Experimental Russian-context sanctions loaders.

STATUS: EXPERIMENTAL. The Russian regulatory landscape is a grey zone for
data reuse and the publishers are less stable than the EU/UK/US feeds. This
module is therefore opt-in and degrades gracefully: if RU_FEEDS_ENABLED is
unset or "false", load_feed("RU") returns an empty list with a clear note.

Sources targeted:
  * Rosfinmonitoring 401-FZ list (terrorists / extremists)
    - Public reference page: https://www.fedsfm.ru/documents/618
    - The list is published as an XML/HTML download. The exact URL changes
      over time; we keep a configurable default and allow RU_ROSFIN_URL to
      override it. License status: the list is published under Russian law
      (401-FZ "On counteracting the financing of terrorism"); reuse is not
      explicitly licensed but the data is a public regulatory act. We mark
      the source as experimental and document the caveat in /status.

  * EGRUL (Russian corporate registry) is NOT handled here -- it lives in
    company-info-api next to companies_house.py. See ru_companies.py.

  * Russian crypto monitoring reuses the existing crypto_index.py OFAC
    digital-currency parser; we do not add a separate RF crypto list yet.

This module mirrors the govfeeds.py structure: 24h disk cache, JSON sidecar,
graceful degradation when the feed is unreachable or disabled.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import certifi
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT = 180.0
HEADERS = {
    "User-Agent": "SanctionsScreenerAPI/1.4-ru-experimental",
    "Accept": "application/xml, text/xml, application/xhtml+xml, */*",
}

CACHE_DIR = os.environ.get("SANCTIONS_CACHE_DIR", "data")

# Opt-in toggle. Default OFF because the source is experimental.
RU_FEEDS_ENABLED = os.environ.get("RU_FEEDS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")

# Default Rosfinmonitoring list URL. The publisher rotates the file; override
# with RU_ROSFIN_URL if the default 404s.
DEFAULT_ROSFIN_URL = os.environ.get(
    "RU_ROSFIN_URL",
    "https://www.fedsfm.ru/documents/618/dop",
)


# ---------------------------------------------------------------------------
# Disk cache (mirrors govfeeds.py)
# ---------------------------------------------------------------------------
def _xml_cache_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short.lower()}.xml")


def _json_cache_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short.lower()}.json")


def _meta_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short.lower()}.meta.json")


def _is_cache_fresh(short: str) -> bool:
    p = _meta_path(short)
    if not os.path.exists(p):
        return False
    try:
        meta = json.load(open(p, "r", encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    ts = meta.get("downloaded_at")
    if not ts:
        return False
    try:
        ts_dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    age = (datetime.now(timezone.utc) - ts_dt).total_seconds()
    return age < CACHE_TTL_SECONDS


def _read_json_cache(short: str) -> list[dict]:
    p = _json_cache_path(short)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("entities"), list):
            return data["entities"]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _write_json_cache(short: str, entities: list[dict]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = _json_cache_path(short)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"entities": entities, "count": len(entities)}, f, ensure_ascii=False, indent=2)


def _write_xml_cache(short: str, xml_bytes: bytes) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_xml_cache_path(short), "wb") as f:
        f.write(xml_bytes)
    with open(_meta_path(short), "w", encoding="utf-8") as f:
        json.dump({"downloaded_at": datetime.now(timezone.utc).isoformat()}, f)


def _download(url: str) -> bytes | None:
    """Download URL into bytes. Returns None on any failure."""
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True, headers=HEADERS) as c:
            r = c.get(url)
        if r.status_code >= 400:
            return None
        return r.content
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Rosfinmonitoring 401-FZ parser
# ---------------------------------------------------------------------------
# The published XML shape (historical): a flat list of <Person> / <Organization>
# nodes with <Name>, <Address>, <INN>, <OGRN>, etc. The schema has shifted over
# time, so we are deliberately permissive: any node with a Name-like child is
# captured. We tag each entity with source = "RU Rosfinmonitoring".


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


_NAME_TAGS = ("Name", "Fio", "FIO", "FullName", "ShortName", "Наименование")


def _extract_name(node: ET.Element) -> str | None:
    for tag in _NAME_TAGS:
        el = node.find(f".//{tag}")
        if el is not None and _text(el):
            return _text(el)
    # Fallback: any direct child whose tag contains "name" (case-insensitive).
    for child in node:
        tag_l = (child.tag or "").lower()
        if "name" in tag_l and _text(child):
            return _text(child)
    return None


def _extract_id(node: ET.Element) -> str:
    """Best-effort stable id: INN > OGRN > id attr > generated."""
    for tag in ("INN", "Inn", "OGRN", "Ogrn", "Id", "ID"):
        el = node.find(f".//{tag}")
        if el is not None and _text(el):
            return f"ru-{tag.lower()}-{_text(el)}"
    iid = node.get("id") or node.get("Id") or node.get("ID")
    if iid:
        return f"ru-attr-{iid}"
    # Last resort: hash of the node's serialised content.
    return "ru-hash-" + str(abs(hash(ET.tostring(node, encoding="unicode"))))


_ENTITY_TAGS = ("Person", "Organization", "LegalEntity", "Individual", "Физлицо", "Юрлицо")


def _parse_rosfin(xml_bytes: bytes) -> list[dict]:
    """Parse Rosfinmonitoring XML into the canonical entity shape used by the
    rest of the API: {entity_id, name, source, aliases, type, raw}.

    Permissive: we accept several historical schemas and fall back to "any
    node with a name-like child" if the expected wrapper tags are absent.
    """
    entities: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return entities

    # Collect candidate nodes: prefer known tags, otherwise scan everything.
    candidates: list[ET.Element] = []
    for tag in _ENTITY_TAGS:
        candidates.extend(root.iter(tag))
    if not candidates:
        # Fallback: any element that has a name-like child.
        for node in root.iter():
            if _extract_name(node) and node is not root:
                candidates.append(node)

    seen_ids: set[str] = set()
    for node in candidates:
        name = _extract_name(node)
        if not name:
            continue
        eid = _extract_id(node)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        # Type inference: tag-based.
        tag_l = (node.tag or "").lower()
        etype = "organization" if any(k in tag_l for k in ("org", "юрл", "legal")) else "person"
        entities.append({
            "entity_id": eid,
            "name": name,
            "source": "RU Rosfinmonitoring",
            "aliases": [],
            "type": etype,
            "raw": {"xml_tag": node.tag},
        })
    return entities


# ---------------------------------------------------------------------------
# Public API (mirrors govfeeds.load_feed)
# ---------------------------------------------------------------------------
def load_feed(short: str = "RU") -> tuple[list[dict], str | None, str | None]:
    """Load the Russian sanctions feed.

    Returns (entities, source_label, error).
    - When RU_FEEDS_ENABLED is unset/false: (empty, None, "disabled")
    - When the download fails: returns the cached list (even if stale) with
      an error string; when no cache exists at all, returns (empty, None, err).
    """
    global _LAST_FETCH_STATUS
    if not RU_FEEDS_ENABLED:
        _LAST_FETCH_STATUS = "disabled"
        return [], None, "disabled"

    short = short.upper()
    if short != "RU":
        return [], None, f"unknown ru feed: {short}"

    if _is_cache_fresh(short):
        cached = _read_json_cache(short)
        if cached:
            return cached, "RU Rosfinmonitoring", None

    xml = _download(DEFAULT_ROSFIN_URL)
    if xml is None:
        _LAST_FETCH_STATUS = "download_failed"
        # Fall back to stale cache if we have one.
        cached = _read_json_cache(short)
        if cached:
            _LAST_FETCH_STATUS = "download_failed_used_cache"
            return cached, "RU Rosfinmonitoring", "download_failed_used_cache"
        _LAST_FETCH_STATUS = "download_failed"
        return [], None, "download_failed"

    entities = _parse_rosfin(xml)
    if not entities:
        # Possibly an HTML page (publisher sometimes wraps XML in HTML).
        # Save the raw bytes for debugging but report an empty list.
        _write_xml_cache(short, xml)
        _LAST_FETCH_STATUS = "parse_returned_empty"
        return [], None, "parse_returned_empty"

    _write_xml_cache(short, xml)
    _write_json_cache(short, entities)
    _LAST_FETCH_STATUS = "ok"
    return entities, "RU Rosfinmonitoring", None


def load_fixture(path: str) -> tuple[list[dict], str | None]:
    """Load a fixture file for tests. Returns (entities, source_label)."""
    try:
        with open(path, "rb") as f:
            xml_bytes = f.read()
    except OSError:
        return [], None
    return _parse_rosfin(xml_bytes), "RU Rosfinmonitoring"


# ---------------------------------------------------------------------------
# Status helper for the /status endpoint.
# ---------------------------------------------------------------------------
_LAST_FETCH_STATUS = "unknown"  # updated by load_feed


def status() -> dict[str, Any]:
    return {
        "enabled": RU_FEEDS_ENABLED,
        "source": "RU Rosfinmonitoring (401-FZ)",
        "url": DEFAULT_ROSFIN_URL,
        "experimental": True,
        "last_fetch_status": _LAST_FETCH_STATUS,
        "note": (
            "Russian-context compliance is experimental. The publisher is "
            "less stable than EU/UK/US feeds and the reuse license is not "
            "explicit. The default URL has not been verified to return valid "
            "XML at publish time; override with RU_ROSFIN_URL if it 404s. "
            "Enable with RU_FEEDS_ENABLED=true."
        ),
    }