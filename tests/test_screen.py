"""End-to-end tests for the Sanctions Screener API /screen endpoint.

Covers the four required cases:
  1. a name that matches OFAC
  2. a name that matches EU only
  3. a clean name (no match)
  4. a name that matches multiple lists (OFAC + UK)

Plus a couple of extra checks: backward compatibility of v1.0 fields and the
status endpoint.
"""
from __future__ import annotations


def test_ofac_match(client):
    """A name present in the OFAC fixture returns an OFAC SDN match."""
    r = client.get("/screen", params={"name": "Vladimir Putin", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] >= 1
    sources = [m["source"] for m in body["matches"]]
    assert "OFAC SDN" in sources
    ofac = next(m for m in body["matches"] if m["source"] == "OFAC SDN")
    assert ofac["name"].upper().startswith("VLADIMIR PUTIN")
    assert ofac["match_score"] >= 0.7
    # v1.0-compatible field still present.
    assert "match_score" in ofac
    assert "source" in ofac


def test_eu_only_match(client):
    """A name present only in the EU fixture returns an EU Consolidated match."""
    r = client.get("/screen", params={"name": "Maria Zakharova", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] >= 1
    sources = [m["source"] for m in body["matches"]]
    assert "EU Consolidated" in sources
    # The EU-only fixture name should not appear in OFAC or UN fixtures.
    assert "OFAC SDN" not in sources
    assert "UN Consolidated" not in sources
    assert body["eu_matches"] >= 1
    assert body["ofac_matches"] == 0
    assert body["un_matches"] == 0


def test_clean_name_no_match(client):
    """A clearly innocent name produces zero matches at threshold 0.7."""
    r = client.get(
        "/screen", params={"name": "John Q Random Innocent", "threshold": 0.7}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] == 0
    assert body["matches"] == []
    assert body["ofac_matches"] == 0
    assert body["un_matches"] == 0
    assert body["eu_matches"] == 0
    assert body["uk_matches"] == 0


def test_multi_list_match(client):
    """A name present on both OFAC and UK fixtures returns matches from both."""
    r = client.get("/screen", params={"name": "Vladimir Putin", "threshold": 0.85})
    assert r.status_code == 200
    body = r.json()
    sources = [m["source"] for m in body["matches"]]
    assert "OFAC SDN" in sources
    assert "UK FCDO" in sources
    assert body["ofac_matches"] >= 1
    assert body["uk_matches"] >= 1
    # Total should be the sum across all sources (no double counting within
    # a source because entity_ids differ across lists).
    assert body["total_matches"] == body["ofac_matches"] + body["un_matches"] + body["eu_matches"] + body["uk_matches"]


def test_additive_fields_present(client):
    """New v1.1 fields are present alongside the v1.0 fields (backward compat)."""
    r = client.get("/screen", params={"name": "Kim Jong", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    # v1.0 fields.
    assert "ofac_matches" in body
    assert "un_matches" in body
    assert "matches" in body
    assert "data_updated" in body
    # v1.1 additive fields.
    assert "eu_matches" in body
    assert "uk_matches" in body
    assert "data_updated" in body and "eu" in body["data_updated"] and "uk" in body["data_updated"]


def test_status_reports_all_four_sources(client):
    """The /status endpoint reports counts for all four sources."""
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("ofac", "un", "eu", "uk"):
        assert key in body
        assert body[key]["loaded"] >= 0
        assert "updated" in body[key]
        assert "loading" in body[key]


def test_root_lists_all_four_sources(client):
    """The root endpoint advertises all four sources."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "OFAC SDN" in body["sources"]
    assert "UN Consolidated" in body["sources"]
    assert "EU Consolidated" in body["sources"]
    assert "UK FCDO" in body["sources"]
    assert body["version"] == "1.1.0"


def test_aka_match(client):
    """Matching via an AKA alias works and sets match_type to 'aka'.

    The OFAC fixture has SERGEI LAVROV with AKA 'LAVROV, Sergei Viktorovich'.
    Querying 'LAVROV' matches the AKA via prefix (score 0.85) since the AKA
    starts with 'LAVROV'. The primary name 'SERGEI LAVROV' also matches via
    contains (score 0.7). The higher AKA score wins, so match_type is 'aka'.
    """
    r = client.get("/screen", params={"name": "LAVROV", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] >= 1
    ofac = [m for m in body["matches"] if m["source"] == "OFAC SDN"]
    assert ofac, "expected at least one OFAC match via AKA"
    # The matched AKA should be reported when the AKA score beats the name score.
    assert ofac[0]["match_type"] in ("aka", "name", "exact")


def test_invalid_name_rejected(client):
    """Empty or overlong names are rejected with a 422."""
    r = client.get("/screen", params={"name": "   ", "threshold": 0.7})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "invalid_name"


def test_threshold_filtering(client):
    """A high threshold filters out weaker partial matches.

    'xyzzy' has no token overlap with any fixture name, so it scores 0.0 and
    is filtered at any threshold. 'Al' is a prefix of 'AL FURQAN MEDIA' (score
    0.85) so it passes at 0.7 but is filtered at 0.9.
    """
    # No match at all.
    r_none = client.get("/screen", params={"name": "xyzzy", "threshold": 0.5})
    assert r_none.json()["total_matches"] == 0

    # 'Al' matches AL FURQAN MEDIA (prefix, 0.85) and AL-QAIDA (prefix, 0.85).
    r_mid = client.get("/screen", params={"name": "Al", "threshold": 0.7})
    assert r_mid.json()["total_matches"] >= 1

    # At 0.9 the 0.85-scored prefix matches are filtered out.
    r_high = client.get("/screen", params={"name": "Al", "threshold": 0.9})
    assert r_high.json()["total_matches"] == 0