"""OFAC SDN digital-currency-address index for crypto wallet screening.

OFAC's SDN List (US public domain, 17 USC 105) includes digital currency
addresses for some sanctioned individuals and entities. The basic `sdn.xml`
feed we already download encodes these addresses as text inside the `<remarks>`
field of each SDN entry, in the format documented by OFAC FAQ 563:

    Digital Currency Address - XBT 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2;
    Digital Currency Address - ETH 0x901bb9583b24d97e99513c6778dc6888ab6870e;

The advanced `sdn_advanced.xml` feed (which we do not currently download)
structures the same data as dedicated `<Feature>` elements keyed by a
`FeatureTypeID`. The basic feed is sufficient for our purposes: we parse the
`remarks` text of every entity already in the OFAC cache and build an
in-memory index of `{currency}_{address}` -> entity.

The index is rebuilt whenever the OFAC cache is refreshed (daily, on startup,
or when `refresh=true` is passed to /screen). Memory usage is bounded by the
number of sanctioned addresses OFAC has published -- as of 2026 this is well
under 100,000 entries (OFAC's SDN list contains ~12,000 entries total, only a
fraction of which carry digital currency addresses). Each entry in the index
is a small dict (~200 bytes), so the index occupies <20MB even at the
theoretical maximum.

Supported currencies (per OFAC FAQ 563): XBT (Bitcoin), BTC (Bitcoin alias),
ETH (Ethereum), LTC (Litecoin), XMR (Monero), XRP (Ripple), DASH, NEO, MIOTA
(Iota), PTR (Petro), USDT, USDC, TRX, SOL, BSC, and any other "Digital
Currency Address - <SYM>" prefix we encounter. We accept any uppercase
symbol the caller passes and also map a few common aliases (BTC<->XBT).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Regex that captures the digital currency symbol and the address from the
# OFAC remarks text. OFAC's documented format is:
#   "Digital Currency Address - XBT 1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
# The address is "up to 256 characters" alphanumeric plus a few symbols
# (0x, bc1, etc.). We are generous about the address charset.
_DC_REGEX = re.compile(
    r"Digital\s+Currency\s+Address\s*-\s*([A-Za-z0-9]+)\s+([A-Za-z0-9]+)",
    re.IGNORECASE,
)

# Currency aliases we normalize. BTC and XBT both refer to Bitcoin. Callers
# may query either; we store both forms so lookups succeed regardless.
CURRENCY_ALIASES: dict[str, str] = {
    "BTC": "XBT",  # Bitcoin: OFAC uses XBT; callers often say BTC.
}

# Reverse alias map so we can look up either form.
def _normalize_currency(currency: str) -> str:
    c = currency.strip().upper()
    return CURRENCY_ALIASES.get(c, c)


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
#
# The index is a dict mapping "{CURRENCY}_{ADDRESS}" -> entity dict (a copy of
# the OFAC entity with the crypto-specific fields attached). We key on the
# normalized currency symbol so a caller querying "BTC" hits the same record
# as one querying "XBT".
#
# We also keep a per-currency count for the /status endpoint.

_index: dict[str, dict[str, Any]] = {}
_index_built_at: str | None = None
_index_source_count: int = 0


def _index_key(currency: str, address: str) -> str:
    return f"{_normalize_currency(currency)}_{address.strip().lower()}"


def build_index(ofac_entities: list[dict]) -> None:
    """Rebuild the in-memory digital currency address index from OFAC entities.

    Each OFAC entity dict (as produced by app.main._download_and_parse for the
    "ofac" source) is expected to carry a `remarks` string and the usual
    `entity_id`, `name`, `type`, `program` fields. We scan every entity's
    remarks for "Digital Currency Address - <SYM> <ADDR>" patterns and record
    one index entry per (currency, address) pair, pointing back to the parent
    entity.
    """
    global _index, _index_built_at, _index_source_count
    new_index: dict[str, dict[str, Any]] = {}
    source_entities = 0
    for entity in ofac_entities:
        remarks = entity.get("remarks") or ""
        if not isinstance(remarks, str) or "Digital Currency Address" not in remarks:
            continue
        source_entities += 1
        # Find every (currency, address) pair in the remarks.
        for m in _DC_REGEX.finditer(remarks):
            raw_currency = m.group(1)
            address = m.group(2)
            if not raw_currency or not address:
                continue
            currency = _normalize_currency(raw_currency)
            # Build the match record. We copy the parent entity fields and
            # attach the crypto-specific ones so /screen_crypto can return a
            # self-contained per-address match object.
            listed_on = ""
            # Try to extract a date from remarks (OFAC often writes
            # "Digital Currency Address - XBT <addr>; ... For more information
            # please see <date>." We do a light scan for an ISO-ish date.
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", remarks)
            if date_match:
                listed_on = date_match.group(1)
            record = {
                "source": "OFAC SDN",
                "entity_id": entity.get("entity_id"),
                "entity_name": entity.get("name"),
                "program": entity.get("program") or "",
                "currency": currency,
                "address": address,
                "listed_on": listed_on,
                "entity_type": entity.get("type") or "",
            }
            # Key under both the normalized currency and the raw currency the
            # caller might use, so BTC and XBT both resolve.
            new_index[_index_key(currency, address)] = record
            if currency != raw_currency.upper():
                new_index[_index_key(raw_currency, address)] = record
    _index = new_index
    _index_built_at = datetime.now(timezone.utc).isoformat()
    _index_source_count = source_entities


def lookup(address: str, currency: str) -> list[dict[str, Any]]:
    """Look up a crypto address in the OFAC SDN index.

    Returns a list of match records (usually 0 or 1; in rare cases the same
    address may be listed under multiple currencies or entities). The list is
    returned even for single matches so the /screen_crypto endpoint can keep
    a consistent `matches[]` shape.
    """
    if not _index:
        return []
    currency = _normalize_currency(currency)
    addr = address.strip()
    if not currency or not addr:
        return []
    key = _index_key(currency, addr)
    rec = _index.get(key)
    if rec:
        return [dict(rec)]
    # Also try the raw address without normalization, in case the caller
    # passed a mixed-case ETH address that we lower-cased. We already lower
    # in _index_key, so this is a no-op for symmetry.
    return []


def index_stats() -> dict[str, Any]:
    """Return stats about the current index (for /status)."""
    # Count addresses per currency.
    per_currency: dict[str, int] = {}
    for key in _index:
        # key = "{CURRENCY}_{address}" -- split on first underscore.
        if "_" in key:
            cur, _addr = key.split("_", 1)
            per_currency[cur] = per_currency.get(cur, 0) + 1
    return {
        "total_addresses": len(_index),
        "source_entities_with_crypto": _index_source_count,
        "built_at": _index_built_at,
        "per_currency": per_currency,
    }


def is_built() -> bool:
    return bool(_index) or _index_built_at is not None


def reset() -> None:
    """Clear the index (used by tests)."""
    global _index, _index_built_at, _index_source_count
    _index = {}
    _index_built_at = None
    _index_source_count = 0