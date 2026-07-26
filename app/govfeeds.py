"""Official government XML feed loaders for EU and UK sanctions lists.

Replaces the previous OpenSanctions-based loaders (which were CC BY-NC 4.0 and
therefore non-commercial-only) with direct downloads from the official
government publishers:

  * EU Financial Sanctions Files (FSF)
    - Portal: https://webgate.ec.europa.eu/fsd/fsf
    - XML download (auth-gated, free EU Login account required):
        https://webgate.ec.europa.eu/europeaid/fsd/fsf/public/files/
        xmlFullSanctionsList/content?token=<EU_FSF_TOKEN>
    - License: European Commission reuse policy. The consolidated list is
      public data; reuse -- including commercial reuse -- is permitted with
      acknowledgement of the source. See:
        https://ec.europa.eu/info/legal-notice_en
      and the dataset page:
        https://data.europa.eu/data/datasets/consolidated-list-of-persons-
        groups-and-entities-subject-to-eu-financial-sanctions
    - Required attribution: "(c) European Union, [year], reproduced under the
      European Commission reuse policy."

  * UK Sanctions List (UKSL) -- FCDO
    - Static XML URL (no auth, OGL v3.0):
        https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml
    - License: Open Government Licence v3.0 -- explicitly permits commercial
      reuse. See: https://www.nationalarchives.gov.uk/doc/open-government-
      licence/version/3/
    - Required attribution: "Contains UK public sector information licensed
      under the Open Government Licence v3.0."

The EU FSF feed is auth-gated: the operator must register a free EU Login
account at https://webgate.ec.europa.eu/europeaid/fsd/fsf#!/account, generate a
personal download token, and provide it via the `EU_FSF_TOKEN` environment
variable. When the token is absent the EU loader returns an empty list (and the
rest of the API keeps working with OFAC/UN/UK data); a clear warning is logged
via the /status endpoint's `eu` block. The EU FSF data MUST NOT be re-served
from OpenSanctions -- that is exactly the dependency this module removes.

The UK FCDO feed is a static public URL under OGL v3.0 and needs no token.

Both feeds are cached to disk (data/eu-fsf.xml, data/uk-sanctions.xml) with a
24h refresh TTL, matching the daily build cadence of the publishers. Parsed
entities are cached to JSON sidecars (data/eu-fsf.json, data/uk-sanctions.json)
so warm restarts skip the XML parse step.
"""
from __future__ import annotations

import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Iterable

import certifi
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Short tag -> metadata. The short tag is what the API exposes internally; the
# `source` label rendered to clients lives in app/main.py (SOURCE_LABELS) and is
# preserved verbatim ("EU Consolidated", "UK FCDO") for backward compatibility.
FEEDS: dict[str, dict[str, str]] = {
    "eu": {
        "display": "EU Consolidated",
        "publisher": "European Commission (EU FSF)",
        "license": "European Commission reuse policy (commercial OK w/ attribution)",
        "token_env": "EU_FSF_TOKEN",
        # Tokenized, auth-gated. The {token} placeholder is replaced at runtime.
        "url_template": (
            "https://webgate.ec.europa.eu/europeaid/fsd/fsf/public/files/"
            "xmlFullSanctionsList/content?token={token}"
        ),
        "fallback_doc_url": "https://webgate.ec.europa.eu/fsd/fsf#!/files",
    },
    "uk": {
        "display": "UK FCDO",
        "publisher": "UK FCDO (UK Sanctions List)",
        "license": "Open Government Licence v3.0 (commercial OK w/ attribution)",
        "token_env": "",
        "url": "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.xml",
    },
}

# How long a cached dump is considered fresh (seconds). 24h matches the daily
# build cadence of both publishers and the task's "daily refresh TTL" rule.
CACHE_TTL_SECONDS = 24 * 60 * 60

# HTTP settings.
REQUEST_TIMEOUT = 180.0
HEADERS = {
    "User-Agent": "SanctionsScreenerAPI/1.2 (contact: builder@api-portfolio.local)"
}

# Directory used for on-disk cache files. Placed next to the app package so it
# survives warm invocations on self-hosted deployments. On Vercel/serverless the
# filesystem is ephemeral, so the cache only helps within a single instance's
# lifetime -- see README for the trade-off.
CACHE_DIR = os.environ.get("SANCTIONS_CACHE_DIR", "data")


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _xml_cache_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short}-sanctions.xml")


def _json_cache_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short}-sanctions.json")


def _meta_path(short: str) -> str:
    return os.path.join(CACHE_DIR, f"{short}-sanctions.meta.json")


def _is_cache_fresh(short: str) -> bool:
    """True if a cached JSON dump exists and is younger than CACHE_TTL_SECONDS."""
    meta = _meta_path(short)
    cache = _json_cache_path(short)
    if not (os.path.exists(meta) and os.path.exists(cache)):
        return False
    try:
        with open(meta, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    ts = data.get("fetched_at_epoch", 0)
    return ts > 0 and (time.time() - ts) < CACHE_TTL_SECONDS


def _read_json_cache(short: str) -> list[dict]:
    path = _json_cache_path(short)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return []


def _write_json_cache(short: str, entities: list[dict]) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_json_cache_path(short), "w", encoding="utf-8") as f:
        json.dump(entities, f)
    with open(_meta_path(short), "w", encoding="utf-8") as f:
        json.dump(
            {
                "fetched_at_epoch": time.time(),
                "fetched_at_iso": datetime.now(timezone.utc).isoformat(),
                "count": len(entities),
                "source": FEEDS[short]["publisher"],
                "license": FEEDS[short]["license"],
            },
            f,
        )


def _write_xml_cache(short: str, xml_bytes: bytes) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_xml_cache_path(short), "wb") as f:
        f.write(xml_bytes)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _download(url: str) -> bytes | None:
    """Download a URL and return the raw bytes, or None on failure."""
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT, verify=certifi.where(), follow_redirects=True,
            headers=HEADERS,
        ) as c:
            r = c.get(url)
        if r.status_code != 200:
            return None
        return r.content
    except httpx.HTTPError:
        return None


# ---------------------------------------------------------------------------
# EU FSF XML parser (Fsdexport 1.0/1.1 schema)
# ---------------------------------------------------------------------------
#
# Structure (namespace http://eu.europa.ec/fpi/fsd/export, but we parse
# namespace-agnostically for robustness):
#
#   <export>
#     <sanctionEntity logicalId="..." euReferenceNumber="..." ...>
#       <subjectType code="person" classificationCode="P"/>   # P=person, E=enterprise
#       <nameAlias firstName="..." lastName="..." wholeName="..." middleName="..."
#                  nameLanguage="en" strong="true"/>
#       ... more <nameAlias> (each is an alias / AKA)
#       <remark>free text</remark>
#       <regulation programme="IRQ" numberTitle="1210/2003 (OJ L169)" .../>
#     </sanctionEntity>
#     ...
#   </export>

_EU_NS = "{http://eu.europa.ec/fpi/fsd/export}"


def _strip_ns(tag: str) -> str:
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _parse_eu_fsf(xml_bytes: bytes) -> list[dict]:
    """Parse EU FSF XML bytes into a list of entity dicts."""
    entities: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return entities

    # Find <export> root (some builds wrap it; some don't).
    if _strip_ns(root.tag) != "export":
        export = root.find(f"{_EU_NS}export")
        if export is None:
            # Fall back to searching for any descendant named export.
            export = None
            for elem in root.iter():
                if _strip_ns(elem.tag) == "export":
                    export = elem
                    break
        if export is None:
            return entities
    else:
        export = root

    for entity_elem in export:
        if _strip_ns(entity_elem.tag) != "sanctionEntity":
            continue
        eu_ref = entity_elem.get("euReferenceNumber") or ""
        logical_id = entity_elem.get("logicalId") or ""
        un_id = entity_elem.get("unitedNationId") or ""

        # subjectType -> person vs enterprise
        subject_type_code = ""
        classification = ""
        for child in entity_elem:
            if _strip_ns(child.tag) == "subjectType":
                subject_type_code = child.get("code", "")
                classification = child.get("classificationCode", "")
                break

        # Collect all nameAlias elements. The first one (or the one with
        # strong="true") is the primary name; the rest are AKAs.
        primary_name = ""
        akas: list[str] = []
        name_aliases: list[ET.Element] = []
        remarks: list[str] = []
        programme = ""
        regulation_title = ""
        for child in entity_elem:
            tag = _strip_ns(child.tag)
            if tag == "nameAlias":
                name_aliases.append(child)
            elif tag == "remark":
                txt = (child.text or "").strip()
                if txt:
                    remarks.append(txt)
            elif tag == "regulation":
                if not programme:
                    programme = child.get("programme", "") or ""
                if not regulation_title:
                    regulation_title = child.get("numberTitle", "") or ""

        def _alias_name(el: ET.Element) -> str:
            whole = (el.get("wholeName") or "").strip()
            if whole:
                return whole
            first = (el.get("firstName") or "").strip()
            last = (el.get("lastName") or "").strip()
            middle = (el.get("middleName") or "").strip()
            parts = [p for p in (first, middle, last) if p]
            return " ".join(parts)

        # Primary name: prefer the alias marked strong="true", else the first.
        strong_alias = None
        for el in name_aliases:
            if (el.get("strong") or "").lower() == "true":
                strong_alias = el
                break
        primary_el = strong_alias or (name_aliases[0] if name_aliases else None)
        if primary_el is not None:
            primary_name = _alias_name(primary_el)

        for el in name_aliases:
            nm = _alias_name(el)
            if nm and nm != primary_name and nm not in akas:
                akas.append(nm)

        if not primary_name:
            continue

        entity_type = "Person" if (classification == "P" or subject_type_code == "person") else "Entity"
        # Compose a human-readable program/remarks combo for the verdict engine.
        program_str = programme or eu_ref or "EU FSF"
        remarks_str = "; ".join(remarks) if remarks else regulation_title

        entities.append({
            "entity_id": eu_ref or logical_id or un_id,
            "name": primary_name,
            "type": entity_type,
            "program": program_str,
            "remarks": remarks_str,
            "akas": akas,
        })
    return entities


# ---------------------------------------------------------------------------
# UK FCDO UKSL XML parser
# ---------------------------------------------------------------------------
#
# The UK Sanctions List XML schema (static URL, OGL v3.0) uses these fields per
# <Designation> (field names are the GOV.UK "Format guide" labels, rendered in
# the XML as elements/attributes):
#   UniqueID, OFSIGroupID, UNReferenceNumber
#   Name1..Name6  (Name6 = surname for individuals, full name for entities/ships)
#   NameType, AliasStrength
#   GroupType (Individual / Entity / Ship)
#   RegimeName, DesignationSource, DateDesignated
#   SanctionsImposed, OtherInformation, UKStatementOfReasons
#
# The XML wraps everything in <SanctionsList><Designation>...</Designation>...
# Each Designation may carry multiple <Name> children (primary + aliases).

def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_uk_fcdo(xml_bytes: bytes) -> list[dict]:
    """Parse UK FCDO UKSL XML bytes into a list of entity dicts."""
    entities: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return entities

    # Find all <Designation> elements regardless of exact wrapper name.
    designations: list[ET.Element] = []
    for elem in root.iter():
        if _strip_ns(elem.tag).lower() == "designation":
            designations.append(elem)

    for desig in designations:
        unique_id = ""
        group_type = ""
        regime = ""
        date_designated = ""
        sanctions_imposed = ""
        statement = ""
        other_info = ""

        # Direct child scalar fields.
        for child in desig:
            tag = _strip_ns(child.tag).lower()
            if tag in ("uniqueid", "unique_id", "id"):
                unique_id = _text(child)
            elif tag in ("grouptype", "group_type", "type"):
                group_type = _text(child)
            elif tag in ("regimename", "regime_name", "regime"):
                regime = _text(child)
            elif tag in ("datedesignated", "date_designated"):
                date_designated = _text(child)
            elif tag in ("sanctionsimposed", "sanctions_imposed"):
                sanctions_imposed = _text(child)
            elif tag in ("ukstatementofreasons", "statement_of_reasons"):
                statement = _text(child)
            elif tag in ("otherinformation", "other_information"):
                other_info = _text(child)

        if not unique_id:
            unique_id = desig.get("UniqueID") or desig.get("uniqueId") or ""

        # Collect all <Name> children. The primary name is the one whose
        # NameType is empty / "Primary" / "Name"; aliases have NameType set to
        # alias-type values. We assemble the display name from Name1..Name6.
        names: list[ET.Element] = []
        for child in desig:
            if _strip_ns(child.tag).lower() == "name":
                names.append(child)

        def _assemble_name(name_el: ET.Element) -> str:
            """Assemble a display name from Name1..Name6 children of <Name>."""
            parts: dict[str, str] = {}
            name_type = ""
            for nc in name_el:
                tag = _strip_ns(nc.tag).lower()
                if tag.startswith("name") and len(tag) == 5 and tag[4].isdigit():
                    parts[tag[4]] = _text(nc)
                elif tag in ("nametype", "name_type"):
                    name_type = _text(nc)
            # Name6 is the surname (individual) or full name (entity/ship).
            # For individuals, render "Name1 Name2 ... Name6" (first names then
            # surname). For entities, Name6 is usually the whole name.
            name6 = parts.get("6", "")
            first_parts = [parts.get(str(i), "") for i in range(1, 6)]
            first_parts = [p for p in first_parts if p]
            if first_parts and name6:
                assembled = " ".join(first_parts) + " " + name6
            elif name6:
                assembled = name6
            else:
                assembled = " ".join(first_parts)
            return assembled.strip()

        primary_name = ""
        akas: list[str] = []
        primary_set = False
        for name_el in names:
            assembled = _assemble_name(name_el)
            if not assembled:
                continue
            # Heuristic: first <Name> is the primary; later ones are aliases.
            if not primary_set:
                primary_name = assembled
                primary_set = True
            else:
                if assembled not in akas and assembled != primary_name:
                    akas.append(assembled)

        if not primary_name:
            continue

        # Normalize group type -> our entity type vocabulary.
        gt = group_type.lower()
        if "entity" in gt:
            entity_type = "Entity"
        elif "ship" in gt or "vessel" in gt:
            entity_type = "Vessel"
        elif "individual" in gt or "person" in gt:
            entity_type = "Individual"
        else:
            entity_type = group_type or "Entity"

        program_str = regime or "UK Sanctions List"
        remarks_bits = [b for b in (sanctions_imposed, statement, other_info) if b]
        remarks_str = "; ".join(remarks_bits)

        entities.append({
            "entity_id": unique_id,
            "name": primary_name,
            "type": entity_type,
            "program": program_str,
            "remarks": remarks_str,
            "akas": akas,
        })
    return entities


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _resolve_url(short: str) -> str | None:
    """Resolve the download URL for a feed. Returns None if unavailable."""
    meta = FEEDS[short]
    if "url" in meta:
        return meta["url"]
    token_env = meta.get("token_env", "")
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if not token:
        return None
    return meta["url_template"].format(token=token)


def _parse_feed(short: str, xml_bytes: bytes) -> list[dict]:
    if short == "eu":
        return _parse_eu_fsf(xml_bytes)
    if short == "uk":
        return _parse_uk_fcdo(xml_bytes)
    return []


def load_feed(short: str) -> tuple[list[dict], str | None, str | None]:
    """Load entities for an official-government-feed-backed list.

    Returns (entities, updated_iso_or_none, status_message_or_none).
    `status_message` is set when the feed could not be downloaded (e.g. the EU
    FSF token is missing) so the caller can surface it via /status.
    """
    if short not in FEEDS:
        return [], None, f"unknown feed: {short}"
    if _is_cache_fresh(short):
        cached = _read_json_cache(short)
        if cached:
            try:
                with open(_meta_path(short), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return cached, updated, None
    url = _resolve_url(short)
    if not url:
        # EU FSF token missing (or other config gap). Surface a clear message
        # rather than silently returning empty. Do NOT fall back to OpenSanctions.
        token_env = FEEDS[short].get("token_env", "")
        msg = (
            f"{FEEDS[short]['display']} feed unavailable: missing {token_env} "
            f"env var. See {FEEDS[short].get('fallback_doc_url', '')} to obtain "
            "a free download token. OpenSanctions is intentionally NOT used."
        )
        # Serve stale cache if we have one.
        stale = _read_json_cache(short)
        if stale:
            try:
                with open(_meta_path(short), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return stale, updated, msg
        return [], None, msg
    xml = _download(url)
    if not xml:
        stale = _read_json_cache(short)
        if stale:
            try:
                with open(_meta_path(short), "r", encoding="utf-8") as f:
                    meta = json.load(f)
                updated = meta.get("fetched_at_iso")
            except (OSError, json.JSONDecodeError):
                updated = None
            return stale, updated, "download failed; serving stale cache"
        return [], None, f"{FEEDS[short]['display']} download failed"
    # Persist raw XML for debugging/audit, then parse.
    try:
        _write_xml_cache(short, xml)
    except OSError:
        pass
    entities = _parse_feed(short, xml)
    if not entities:
        # Parse produced nothing -- keep any prior cache rather than wiping it.
        stale = _read_json_cache(short)
        if stale:
            return stale, None, "parse produced 0 entities; serving stale cache"
        return [], None, f"{FEEDS[short]['display']} parse produced 0 entities"
    _write_json_cache(short, entities)
    updated = datetime.now(timezone.utc).isoformat()
    return entities, updated, None


def load_fixture(path: str) -> tuple[list[dict], str | None]:
    """Load a small JSON fixture for tests instead of downloading.

    The fixture is a JSON array of pre-normalized entity dicts (same shape as
    the `entities` returned by load_feed).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, datetime.now(timezone.utc).isoformat()
    except (OSError, json.JSONDecodeError):
        pass
    return [], None


# Backward-compat alias: the previous module was called `opensanctions` and
# exposed `load_opensanctions`. Some callers/tests may still reference it.
load_opensanctions = load_feed