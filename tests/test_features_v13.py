"""Tests for the three v1.3 features: BIS CSL, crypto wallet screening, and
webhook monitoring.

All tests are mocked: no real network calls. BIS data comes from
bis-fixture.json; OFAC crypto addresses come from the remarks of the OFAC
fixture (LAZARUS GROUP carries XBT/ETH/LTC addresses); monitor storage is
isolated in a per-test tmp dir via the SANCTIONS_CACHE_DIR env var set in
conftest.py.
"""
from __future__ import annotations

from unittest.mock import patch

# ---------------------------------------------------------------------------
# Feature 1: BIS Denied Persons List + Entity List (trade.gov CSL)
# ---------------------------------------------------------------------------

def test_bis_match(client):
    """A name present in the BIS fixture returns a BIS CSL match and a
    bis_matches count >= 1.
    """
    r = client.get("/screen", params={"name": "ZHANG SAN TRADE CO", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    sources = [m["source"] for m in body["matches"]]
    assert "BIS CSL" in sources
    assert body["bis_matches"] >= 1
    bis = next(m for m in body["matches"] if m["source"] == "BIS CSL")
    assert "ZHANG SAN" in bis["name"].upper()


def test_bis_no_match(client):
    """A clean name produces zero BIS matches and bis_matches == 0."""
    r = client.get("/screen", params={"name": "John Q Random Innocent", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["bis_matches"] == 0
    assert not any(m["source"] == "BIS CSL" for m in body["matches"])


def test_bis_degrade_when_api_key_missing(client, monkeypatch):
    """When the trade.gov API key is missing AND the on-disk BIS cache is
    empty, the BIS source returns zero matches and a clear status warning
    surfaced via /status.
    """
    from app import main
    from app import bis as bis_mod

    # Simulate the degrade: clear the in-memory BIS cache and make the loader
    # report the missing-key status.
    main._cache["bis"]["data"] = []
    main._cache["bis"]["status"] = (
        "BIS CSL feed unavailable: missing TRADE_GOV_API_KEY env var. ..."
    )
    # Patch the refresh helper so /screen's refresh path also stays empty.
    async def _empty_bis():
        main._cache["bis"]["data"] = []
        main._cache["bis"]["status"] = "missing TRADE_GOV_API_KEY"
    monkeypatch.setattr(main, "_refresh_bis_async", _empty_bis)

    r = client.get("/screen", params={"name": "ZHANG SAN TRADE CO", "threshold": 0.7})
    assert r.status_code == 200
    body = r.json()
    assert body["bis_matches"] == 0
    # The other four sources still ran (OFAC/UN/EU/UK are populated by the
    # conftest fixture) -- total is 0 because the name is not on those lists.
    assert body["ofac_matches"] == 0

    # /status surfaces the degrade warning.
    s = client.get("/status").json()
    assert s["bis"]["status"] is not None
    assert "TRADE_GOV_API_KEY" in s["bis"]["status"]


# ---------------------------------------------------------------------------
# Feature 2: Crypto wallet screening (OFAC SDN digital currency addresses)
# ---------------------------------------------------------------------------

def test_crypto_btc_address_match(client):
    """A BTC/XBT address present in the OFAC LAZARUS GROUP remarks is flagged
    as sanctioned. BTC and XBT are aliases for Bitcoin.
    """
    r = client.get("/screen_crypto", params={
        "address": "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",
        "currency": "BTC",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sanctioned"] is True
    assert body["currency"] == "BTC"
    assert len(body["matches"]) >= 1
    m = body["matches"][0]
    assert m["source"] == "OFAC SDN"
    assert m["entity_name"].upper() == "LAZARUS GROUP"
    assert m["program"] == "DPRK2"
    assert body["risk_verdict"].startswith("HIGH RISK")
    assert "LAZARUS GROUP" in body["risk_verdict"].upper() or "Lazarus" in body["risk_verdict"]


def test_crypto_eth_address_match(client):
    """An ETH address in the OFAC LAZARUS GROUP remarks is flagged."""
    r = client.get("/screen_crypto", params={
        "address": "0x901bb9583b24d97e99513c6778dc6888ab6870e",
        "currency": "ETH",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sanctioned"] is True
    assert body["matches"][0]["currency"] == "ETH"
    assert body["matches"][0]["entity_name"].upper() == "LAZARUS GROUP"


def test_crypto_no_match_clean(client):
    """An address not in the OFAC index returns sanctioned=false, CLEAN
    verdict, and an empty matches array.
    """
    r = client.get("/screen_crypto", params={
        "address": "0xdeadbeef00000000000000000000000000000000",
        "currency": "ETH",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sanctioned"] is False
    assert body["matches"] == []
    assert body["risk_verdict"].startswith("CLEAN")


def test_crypto_invalid_params(client):
    """Missing address or currency is rejected with 422."""
    r = client.get("/screen_crypto", params={"address": "0xabc", "currency": ""})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Feature 3: Webhook monitoring for new designations
# ---------------------------------------------------------------------------

def test_monitor_registration(client):
    """POST /monitor returns a monitor_id and registered status."""
    r = client.post("/monitor", json={
        "name": "Vladimir Putin",
        "webhook_url": "https://example.com/hook",
        "lists": ["OFAC", "UN"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "registered"
    assert body["monitor_id"].startswith("mon_")
    assert body["name"] == "Vladimir Putin"
    assert body["lists"] == ["OFAC", "UN"]

    # GET /monitor/{id} returns the same monitor.
    mid = body["monitor_id"]
    g = client.get(f"/monitor/{mid}").json()
    assert g["monitor_id"] == mid
    assert g["name"] == "Vladimir Putin"

    # DELETE /monitor/{id} removes it.
    d = client.delete(f"/monitor/{mid}").json()
    assert d["status"] == "deleted"
    assert client.get(f"/monitor/{mid}").status_code == 404


def test_monitor_invalid_webhook_rejected(client):
    """A non-http(s) webhook_url is rejected with 422."""
    r = client.post("/monitor", json={
        "name": "Foo",
        "webhook_url": "ftp://nope",
    })
    assert r.status_code == 422


def test_monitor_trigger_new_match_fires_webhook(client):
    """When /monitor/run is triggered and a NEW match appears for a
    registered name, the webhook URL is POSTed the new_match payload.

    We register a name that matches the OFAC fixture, then run the monitor
    cycle. On the first run the monitor has no previous signature, so the
    whole match set is treated as new and the webhook fires.
    """
    # Register a monitor for a name that will match the OFAC fixture.
    reg = client.post("/monitor", json={
        "name": "LAZARUS GROUP",
        "webhook_url": "https://example.com/hook",
        "lists": ["OFAC"],
    }).json()
    mid = reg["monitor_id"]

    # Mock the webhook delivery so no real network call happens. We capture
    # the posted payloads.
    posted = []
    def fake_deliver(url, payload):
        posted.append((url, payload))
        return True, "delivered (mock)"
    from app import monitors as monitors_mod
    with patch.object(monitors_mod, "deliver_webhook", side_effect=fake_deliver):
        # Run the check synchronously (bypass the background task) so we can
        # assert on the result immediately.
        from app.main import _screen_all_sync
        summary = monitors_mod.run_checks(_screen_all_sync, deliver=True)

    assert summary["checked"] >= 1
    assert summary["fired"] >= 1
    assert len(posted) >= 1
    url, payload = posted[0]
    assert url == "https://example.com/hook"
    assert payload["event"] == "new_match"
    assert payload["monitor_id"] == mid
    assert payload["name"] == "LAZARUS GROUP"
    assert payload["new_match"]["source"] == "OFAC SDN"

    # The monitor's history should record the delivery.
    m = client.get(f"/monitor/{mid}").json()
    events = [h["event"] for h in m["history"]]
    assert "new_match" in events
    assert "delivery_ok" in events


def test_monitor_trigger_no_change_does_not_fire(client):
    """When a second /monitor/run produces the same match set as the first,
    no webhook is fired (no_change event).
    """
    reg = client.post("/monitor", json={
        "name": "LAZARUS GROUP",
        "webhook_url": "https://example.com/hook",
        "lists": ["OFAC"],
    }).json()
    mid = reg["monitor_id"]

    posted = []
    def fake_deliver(url, payload):
        posted.append((url, payload))
        return True, "delivered (mock)"
    from app import monitors as monitors_mod
    from app.main import _screen_all_sync
    with patch.object(monitors_mod, "deliver_webhook", side_effect=fake_deliver):
        monitors_mod.run_checks(_screen_all_sync, deliver=True)
        first_count = len(posted)
        # Second run: same match set -> no new webhooks.
        monitors_mod.run_checks(_screen_all_sync, deliver=True)

    assert len(posted) == first_count  # no new deliveries on the second run

    # The history should now contain a no_change event.
    m = client.get(f"/monitor/{mid}").json()
    events = [h["event"] for h in m["history"]]
    assert "no_change" in events


def test_monitor_get_unknown_returns_404(client):
    """GET /monitor/{unknown_id} returns 404."""
    r = client.get("/monitor/mon_doesnotexist")
    assert r.status_code == 404


def test_monitor_delete_unknown_returns_404(client):
    """DELETE /monitor/{unknown_id} returns 404."""
    r = client.delete("/monitor/mon_doesnotexist")
    assert r.status_code == 404


def test_monitor_delivery_logged_to_jsonl(client, tmp_path):
    """When a webhook delivery is attempted, an audit line is appended to
    data/monitor-log.jsonl (best-effort append-only log).
    """
    import os
    from app import monitors as monitors_mod
    from app.main import _screen_all_sync

    # Point the monitor log at a temp file we can inspect.
    log_path = tmp_path / "monitor-log.jsonl"
    monkeypatch_target = monitors_mod
    # Reload the module so MONITOR_LOG_PATH picks up the env var? Simpler:
    # patch the module-level path directly.
    original = monitors_mod.MONITOR_LOG_PATH
    monitors_mod.MONITOR_LOG_PATH = str(log_path)
    try:
        reg = client.post("/monitor", json={
            "name": "LAZARUS GROUP",
            "webhook_url": "https://example.com/hook",
            "lists": ["OFAC"],
        }).json()
        posted = []
        def fake_deliver(url, payload):
            posted.append((url, payload))
            # Call the real logger to exercise the JSONL append.
            monitors_mod._log_delivery(payload, True, "delivered (mock)")
            return True, "delivered (mock)"
        with patch.object(monitors_mod, "deliver_webhook", side_effect=fake_deliver):
            monitors_mod.run_checks(_screen_all_sync, deliver=True)
        assert log_path.exists(), "monitor-log.jsonl was not created"
        import json as _json
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert lines, "monitor-log.jsonl is empty"
        rec = _json.loads(lines[-1])
        assert rec["ok"] is True
        assert rec["monitor_id"] == reg["monitor_id"]
        assert rec["event"] == "new_match"
    finally:
        monitors_mod.MONITOR_LOG_PATH = original