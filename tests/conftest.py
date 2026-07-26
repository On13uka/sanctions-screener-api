"""Shared pytest fixtures for the Sanctions Screener API tests.

We avoid network access entirely by pre-populating the in-memory cache of
`app.main` with small fixture data. This makes tests fast, deterministic,
and independent of the live OFAC/UN/EU/UK feeds.
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
def populate_cache(monkeypatch):
    """Inject fixture data into the in-memory cache before each test.

    This also stubs out the refresh functions so no test ever hits the
    network, regardless of query parameters.
    """
    from app import main

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

    # Make refresh functions no-ops so `refresh=True` queries don't download.
    async def _noop_xml(source):
        return None

    async def _noop_govfeed(short):
        return None

    monkeypatch.setattr(main, "_refresh_xml_cache", _noop_xml, raising=False)
    monkeypatch.setattr(main, "_refresh_govfeed_async", _noop_govfeed, raising=False)


@pytest.fixture()
def client():
    """FastAPI TestClient bound to the app with the populated cache."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c