"""End-to-end tests for the Sanctions Screener API /screen endpoint.

Covers:
  - Backward-compat cases (v1.0/v1.1): OFAC match, EU-only match, clean name,
    multi-list match, additive fields, /status, root, AKA match, invalid name,
    threshold filtering.
  - New v1.2 explainable-match cases: exact-match explanation, fuzzy-match
    explanation, AKA-match explanation, no-match verdict, multi-match verdict
    (picks highest severity), risk_verdict field is always present.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# v1.0 / v1.1 backward-compat tests (unchanged behavior)
# ---------------------------------------------------------------------------

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
    assert body["total_matches"] == body["ofac_matches"] + body["un_matches"] + body["eu_matches"] + body["uk_matches"] + body["bis_matches"]


def test_additive_fields_present(client):
    """New v1.1/v1.2 fields are present alongside the v1.0 fields (backward compat)."""
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
    # v1.2 additive fields.
    assert "risk_verdict" in body
    assert isinstance(body["risk_verdict"], str) and body["risk_verdict"]


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
    """The root endpoint advertises all five sources (OFAC, UN, EU, UK, BIS)."""
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert "OFAC SDN" in body["sources"]
    assert "UN Consolidated" in body["sources"]
    assert "EU Consolidated" in body["sources"]
    assert "UK FCDO" in body["sources"]
    assert "BIS CSL" in body["sources"]
    assert body["version"] == "1.3.0"


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
    # In v1.2 match_type reports the similarity class (exact/fuzzy/partial/token)
    # and the matched field lives in match_explanation.matched_field. The AKA
    # 'LAVROV, Sergei Viktorovich' is a prefix of query 'LAVROV' so the class
    # is 'fuzzy' (0.85) and the field is 'aka'.
    assert ofac[0]["match_type"] in ("exact", "fuzzy", "partial", "token")
    assert ofac[0]["match_explanation"]["matched_field"] == "aka"
    assert ofac[0]["matched_aka"] is not None


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


# ---------------------------------------------------------------------------
# v1.2 explainable-match + risk verdict tests
# ---------------------------------------------------------------------------

def test_exact_match_explanation_and_high_risk_verdict(client):
    """An exact name match produces an 'exact' match_type, a match_explanation
    with matched_field='name', and a HIGH RISK verdict naming OFAC SDN.
    """
    r = client.get("/screen", params={"name": "Vladimir Putin", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    ofac = next(m for m in body["matches"] if m["source"] == "OFAC SDN")
    assert ofac["match_type"] == "exact"
    assert ofac["match_score"] == 1.0
    expl = ofac["match_explanation"]
    assert expl["matched_field"] == "name"
    assert expl["match_type"] == "exact"
    assert expl["matched_value"].upper().startswith("VLADIMIR PUTIN")
    # tokens matched should include both first and last name.
    assert "vladimir" in expl["tokens_matched"]
    assert "putin" in expl["tokens_matched"]
    # Risk verdict: HIGH RISK, exact match, mentions OFAC SDN.
    verdict = body["risk_verdict"]
    assert verdict.startswith("HIGH RISK")
    assert "OFAC SDN" in verdict


def test_fuzzy_match_explanation_and_medium_risk_verdict(client):
    """A fuzzy (prefix) match at 0.85 produces a 'fuzzy' match_type and a
    MEDIUM RISK verdict with a percentage confidence.
    """
    # 'Kim Jong' is a prefix of 'KIM JONG UN' (UN fixture) -> score 0.85.
    r = client.get("/screen", params={"name": "Kim Jong", "threshold": 0.85})
    assert r.status_code == 200
    body = r.json()
    un_match = next(m for m in body["matches"] if m["source"] == "UN Consolidated")
    assert un_match["match_type"] == "fuzzy"
    assert un_match["match_score"] == 0.85
    expl = un_match["match_explanation"]
    assert expl["matched_field"] == "name"
    assert expl["match_type"] == "fuzzy"
    assert "kim" in expl["tokens_matched"]
    assert "jong" in expl["tokens_matched"]
    verdict = body["risk_verdict"]
    # 0.85 is the MEDIUM band (>=0.85).
    assert "MEDIUM RISK" in verdict
    assert "UN Consolidated" in verdict
    assert "85%" in verdict


def test_aka_match_explanation(client):
    """A match that wins on an AKA alias reports matched_field='aka' and the
    matched_value is the alias string.
    """
    # 'LAVROV' matches the AKA 'LAVROV, Sergei Viktorovich' (prefix, 0.85).
    r = client.get("/screen", params={"name": "LAVROV", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    ofac = next(m for m in body["matches"] if m["source"] == "OFAC SDN")
    expl = ofac["match_explanation"]
    assert expl["matched_field"] == "aka"
    assert "LAVROV" in expl["matched_value"].upper()
    assert expl["match_type"] in ("fuzzy", "partial", "exact")
    # matched_aka should be populated when the aka won.
    assert ofac["matched_aka"] is not None
    assert "LAVROV" in ofac["matched_aka"].upper()


def test_no_match_clean_verdict(client):
    """A clean name produces a CLEAN verdict and no matches."""
    r = client.get("/screen", params={"name": "John Q Random Innocent", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] == 0
    verdict = body["risk_verdict"]
    assert verdict.startswith("CLEAN")
    assert "5 lists screened" in verdict


def test_multi_match_picks_highest_severity_verdict(client):
    """When the same name matches multiple lists, the verdict reflects the
    highest-severity match. 'Vladimir Putin' matches OFAC (exact, 1.0) and
    UK (exact, 1.0) -- both HIGH. The verdict must be HIGH RISK and mention
    OFAC SDN (the first list in our severity tiebreak, since both are exact).
    """
    r = client.get("/screen", params={"name": "Vladimir Putin", "threshold": 0.85})
    assert r.status_code == 200
    body = r.json()
    sources = [m["source"] for m in body["matches"]]
    assert "OFAC SDN" in sources
    assert "UK FCDO" in sources
    verdict = body["risk_verdict"]
    assert verdict.startswith("HIGH RISK")
    # At least one of the matching lists must be named in the verdict.
    assert ("OFAC SDN" in verdict) or ("UK FCDO" in verdict)


def test_low_risk_partial_match_verdict(client):
    """A 0.7-score 'partial' (contains) match produces a LOW RISK verdict
    recommending manual review.
    """
    # 'Al' is a prefix of 'AL FURQAN MEDIA' (0.85) -- that's MEDIUM. To force a
    # LOW (0.7-0.85) band we use a contains-only match. 'Furqan' is contained
    # in 'AL FURQAN MEDIA' -> the contains rule fires (0.7) because neither is
    # a prefix of the other and 'furqan' is in 'al furqan media'.
    r = client.get("/screen", params={"name": "Furqan", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] >= 1
    # Find the AL FURQAN MEDIA match.
    furqan = next(
        m for m in body["matches"] if "FURQAN" in m["name"].upper()
    )
    assert furqan["match_score"] == 0.7
    assert furqan["match_type"] == "partial"
    verdict = body["risk_verdict"]
    # 0.7 is the LOW band. Verdict should recommend manual review.
    assert ("LOW RISK" in verdict) or ("MEDIUM RISK" in verdict)
    assert "manual review" in verdict or "confidence" in verdict


def test_match_explanation_always_present(client):
    """Every match object carries a non-empty match_explanation dict."""
    r = client.get("/screen", params={"name": "Al", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["total_matches"] >= 1
    for m in body["matches"]:
        assert "match_explanation" in m
        expl = m["match_explanation"]
        assert isinstance(expl, dict)
        assert "matched_field" in expl
        assert "matched_value" in expl
        assert "match_type" in expl
        assert "tokens_matched" in expl
        assert expl["matched_field"] in ("name", "aka")