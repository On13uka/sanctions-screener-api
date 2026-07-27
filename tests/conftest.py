"""Shared pytest fixtures for the Sanctions Screener API tests.

We avoid network access entirely by pre-populating the in-memory cache of
`app.main` with small fixture data. This makes tests fast, deterministic,
and independent of the live OFAC/UN/EU/UK/BIS feeds.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name: str) -> list[dict]:
    path = os.path.join(FIXTURE_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def populate_cache(monkeypatch, tmp_path):
    """Inject fixture data into the in-memory cache before each test.

    This also stubs out the refresh functions so no test ever hits the
    network, regardless of query parameters. The BIS cache and the OFAC
    crypto address index are also populated from fixtures.
    """
    from app import main
    from app import crypto_index

    now = datetime.now(timezone.utc).isoformat()
    main._cache["ofac"]["data"] = _load_fixture("ofac-fixture.json")
    main._cache["ofac"]["updated"] = now
    main._cache["ofac"]["loading"] = False
    main._cache["ofac"]["status"] = None

    main._cache["un"]["data"] = _load_fixture("un-fixture.json")
    main._cache["un"]["updated"] = now
    main._cache["un"]["loading"] = False
    main._cache["un"]["status"] = None

    main._cache["eu"]["data"] = _load_fixture("eu-fixture.json")
    main._cache["eu"]["updated"] = now
    main._cache["eu"]["loading"] = False
    main._cache["eu"]["status"] = None

    main._cache["uk"]["data"] = _load_fixture("uk-fixture.json")
    main._cache["uk"]["updated"] = now
    main._cache["uk"]["loading"] = False
    main._cache["uk"]["status"] = None

    # BIS fixture (trade.gov CSL). Tests that exercise the degrade path
    # (missing TRADE_GOV_API_KEY) monkeypatch this to [].
    bis_path = os.path.join(FIXTURE_DIR, "bis-fixture.json")
    if os.path.exists(bis_path):
        main._cache["bis"]["data"] = _load_fixture("bis-fixture.json")
    else:
        main._cache["bis"]["data"] = []
    main._cache["bis"]["updated"] = now
    main._cache["bis"]["loading"] = False
    main._cache["bis"]["status"] = None

    # Rebuild the OFAC digital currency address index from the OFAC fixture.
    crypto_index.reset()
    crypto_index.build_index(main._cache["ofac"]["data"])

    # Make refresh functions no-ops so `refresh=True` queries don't download.
    async def _noop_xml(source):
        return None

    async def _noop_govfeed(short):
        return None

    async def _noop_bis():
        return None

    monkeypatch.setattr(main, "_refresh_xml_cache", _noop_xml, raising=False)
    monkeypatch.setattr(main, "_refresh_govfeed_async", _noop_govfeed, raising=False)
    monkeypatch.setattr(main, "_refresh_bis_async", _noop_bis, raising=False)

    # Isolate monitor storage in a per-test tmp dir so monitor tests don't
    # pollute the real data/monitors.json on disk.
    monkeypatch.setenv("SANCTIONS_CACHE_DIR", str(tmp_path))
    # Reload monitors module so it picks up the new cache dir.
    import importlib
    from app import monitors as monitors_mod
    importlib.reload(monitors_mod)
    monkeypatch.setattr(main, "monitors", monitors_mod, raising=False)


@pytest.fixture()
def client():
    """FastAPI TestClient bound to the app with the populated cache."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c