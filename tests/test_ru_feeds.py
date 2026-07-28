"""Tests for app.ru_feeds: experimental Russian sanctions loader."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import ru_feeds


def test_disabled_by_default_returns_empty(monkeypatch):
    monkeypatch.setattr(ru_feeds, "RU_FEEDS_ENABLED", False)
    entities, source, err = ru_feeds.load_feed("RU")
    assert entities == []
    assert err == "disabled"


def test_parse_rosfin_extracts_person_from_known_schema():
    xml = b"""<?xml version="1.0"?>
    <List>
      <Person>
        <Name>IVANOV IVAN IVANOVICH</Name>
        <INN>1234567890</INN>
      </Person>
      <Organization>
        <Name>EVIL CORP LLC</Name>
        <OGRN>9876543210</OGRN>
      </Organization>
    </List>"""
    entities, source = ru_feeds.load_fixture.__wrapped__ if hasattr(ru_feeds.load_fixture, "__wrapped__") else (None, None)
    # Call the internal parser directly.
    entities = ru_feeds._parse_rosfin(xml)
    assert len(entities) == 2
    names = [e["name"] for e in entities]
    assert "IVANOV IVAN IVANOVICH" in names
    assert "EVIL CORP LLC" in names
    assert all(e["source"] == "RU Rosfinmonitoring" for e in entities)
    # Type inference: Person -> person, Organization -> organization
    types = {e["name"]: e["type"] for e in entities}
    assert types["IVANOV IVAN IVANOVICH"] == "person"
    assert types["EVIL CORP LLC"] == "organization"


def test_parse_rosfin_empty_xml_returns_empty():
    entities = ru_feeds._parse_rosfin(b"<List></List>")
    assert entities == []


def test_parse_rosfin_invalid_xml_returns_empty():
    entities = ru_feeds._parse_rosfin(b"not xml at all <<<")
    assert entities == []


def test_parse_rosfin_falls_back_to_name_like_child():
    xml = b"""<?xml version="1.0"?>
    <Root>
      <Row>
        <FullName>FALLBACK CORP</FullName>
      </Row>
    </Root>"""
    entities = ru_feeds._parse_rosfin(xml)
    assert len(entities) == 1
    assert entities[0]["name"] == "FALLBACK CORP"


def test_status_reports_experimental_flag():
    s = ru_feeds.status()
    assert s["experimental"] is True
    assert "Rosfinmonitoring" in s["source"]