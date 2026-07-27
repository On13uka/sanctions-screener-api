"""Webhook monitoring for new sanctions designations.

Customers register an entity name (plus optional webhook URL and list filter)
for ongoing monitoring. The background checker re-screens the name against the
configured lists; when a NEW match appears (a list that was not matched on the
previous run, or a new entity_id that was not seen before), the checker POSTs
a webhook payload to the registered URL.

Storage: monitor registrations are persisted to a single JSON file on disk
(`data/monitors.json`). This is intentionally simple -- no database, no
queue, no encryption of the on-disk file. It is documented as NOT
production-grade: for scale, use Redis/Postgres + a real task queue
(Celery, RQ, arq) instead. The JSON file is fine for low-volume self-hosted
deployments and for proving the feature on RapidAPI.

Delivery: best-effort. The checker POSTs the webhook payload with a 10s
timeout and retries once on any non-2xx response or transport error. Delivery
status (success / failed / retried) is recorded in each monitor's `history`
array so /monitor/{id} can report it. The webhook POST itself runs in a
background task (FastAPI BackgroundTasks) so it never blocks the registering
HTTP request.

Data model (one row per monitor, persisted to data/monitors.json):
    {
      "monitor_id": "mon_<8 hex>",
      "name": "Vladimir Putin",
      "webhook_url": "https://your-app.com/sanctions-webhook",
      "lists": ["OFAC", "UN", "EU", "UK"],     # which lists to watch
      "created_at": "2026-...",
      "last_check": "2026-..." | null,
      "last_match_signature": "<sig>" | null,  # fingerprint of last match set
      "history": [                              # rolling log of check events
        {"at": "...", "event": "new_match"|"no_change"|"delivery_ok"|
                                "delivery_failed", "detail": "..."}
      ]
    }

The "match signature" is a stable string derived from the set of
(source, entity_id) pairs matched on the last run. When a new run produces a
different signature, we know a new designation has appeared and we fire the
webhook. This avoids needing to persist the full match payloads.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import certifi
import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CACHE_DIR = os.environ.get("SANCTIONS_CACHE_DIR", "data")
MONITORS_PATH = os.path.join(CACHE_DIR, "monitors.json")
# Append-only JSONL log of every webhook delivery attempt (success or
# failure). One JSON object per line, newest at the bottom. Best-effort:
# a write failure is swallowed so it never breaks a check cycle.
MONITOR_LOG_PATH = os.path.join(CACHE_DIR, "monitor-log.jsonl")

# Webhook delivery settings.
WEBHOOK_TIMEOUT = 10.0
WEBHOOK_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "SanctionsScreenerAPI/1.3-monitor",
}

# Allowed list codes a monitor can subscribe to. These map to the internal
# source keys used by app.main (plus "BIS" for the new BIS source).
ALLOWED_LISTS = {"OFAC", "UN", "EU", "UK", "BIS"}

# Cap history length to avoid unbounded growth of the on-disk file.
HISTORY_CAP = 50

# Optional fixture path for tests. Set SANCTIONS_FIXTURE_DIR to a folder
# containing monitors-fixture.json to avoid touching the real file.
_FIXTURE_DIR = os.environ.get("SANCTIONS_FIXTURE_DIR", "")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _monitors_path() -> str:
    if _FIXTURE_DIR:
        return os.path.join(_FIXTURE_DIR, "monitors-fixture.json")
    return MONITORS_PATH


def _monitor_log_path() -> str:
    if _FIXTURE_DIR:
        return os.path.join(_FIXTURE_DIR, "monitor-log-fixture.jsonl")
    return MONITOR_LOG_PATH


def _append_monitor_log(record: dict[str, Any]) -> None:
    """Append one delivery record to data/monitor-log.jsonl (best-effort).

    Each line is a self-contained JSON object so the file can be tailed,
    grep'd, or streamed. Never raises -- a failed log write must not break
    a check cycle.
    """
    try:
        os.makedirs(os.path.dirname(_monitor_log_path()), exist_ok=True)
        with open(_monitor_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _load_all() -> dict[str, dict[str, Any]]:
    """Load all monitors from disk. Returns {monitor_id: monitor_dict}."""
    path = _monitors_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "monitors" in data:
            # File wraps monitors under a top-level key.
            monitors = data["monitors"]
            if isinstance(monitors, list):
                return {m["monitor_id"]: m for m in monitors if "monitor_id" in m}
            if isinstance(monitors, dict):
                return dict(monitors)
        if isinstance(data, dict):
            # Bare dict of id -> monitor.
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        if isinstance(data, list):
            return {m["monitor_id"]: m for m in data if isinstance(m, dict) and "monitor_id" in m}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_all(monitors: dict[str, dict[str, Any]]) -> None:
    if _FIXTURE_DIR:
        # In test mode, still write to the fixture path so the test can
        # observe state changes.
        path = _monitors_path()
    else:
        path = _monitors_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Wrap under a top-level "monitors" key for forward-compat (we may add
    # metadata later).
    payload = {
        "monitors": monitors,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def _new_monitor_id() -> str:
    return "mon_" + secrets.token_hex(4)


def _validate_lists(lists: list[str]) -> list[str]:
    """Filter the requested lists to the allowed set; default to all."""
    if not lists:
        return sorted(ALLOWED_LISTS)
    cleaned = []
    for l in lists:
        u = (l or "").strip().upper()
        if u in ALLOWED_LISTS and u not in cleaned:
            cleaned.append(u)
    return cleaned or sorted(ALLOWED_LISTS)


def register(
    name: str,
    webhook_url: str,
    lists: list[str] | None = None,
) -> dict[str, Any]:
    """Register a new monitor. Returns the monitor dict (with monitor_id)."""
    monitors = _load_all()
    monitor_id = _new_monitor_id()
    while monitor_id in monitors:  # collision guard
        monitor_id = _new_monitor_id()
    now = datetime.now(timezone.utc).isoformat()
    monitor = {
        "monitor_id": monitor_id,
        "name": name.strip(),
        "webhook_url": webhook_url.strip(),
        "lists": _validate_lists(lists or []),
        "created_at": now,
        "last_check": None,
        "last_match_signature": None,
        "history": [],
    }
    monitors[monitor_id] = monitor
    _save_all(monitors)
    return monitor


def get(monitor_id: str) -> dict[str, Any] | None:
    return _load_all().get(monitor_id)


def delete(monitor_id: str) -> bool:
    monitors = _load_all()
    if monitor_id not in monitors:
        return False
    del monitors[monitor_id]
    _save_all(monitors)
    return True


def list_all() -> list[dict[str, Any]]:
    return list(_load_all().values())


# ---------------------------------------------------------------------------
# Match signature + webhook delivery
# ---------------------------------------------------------------------------

def _match_signature(matches: list[dict[str, Any]]) -> str:
    """Build a stable signature from a set of matches.

    We sort (source, entity_id) pairs so the signature is order-independent.
    Adding a new entity or a new list changes the signature; reordering or
    re-screening the same set does not.
    """
    pairs = []
    for m in matches:
        src = (m.get("source") or "").strip()
        eid = str(m.get("entity_id") or "").strip()
        if src and eid:
            pairs.append((src, eid))
    pairs.sort()
    return "|".join(f"{s}:{e}" for s, e in pairs)


def _filter_matches_by_lists(
    matches: list[dict[str, Any]], lists: list[str]
) -> list[dict[str, Any]]:
    """Keep only matches whose source label corresponds to a subscribed list."""
    if not lists:
        return matches
    # Map our list codes to the source labels used in match objects.
    label_for = {
        "OFAC": "OFAC SDN",
        "UN": "UN Consolidated",
        "EU": "EU Consolidated",
        "UK": "UK FCDO",
        "BIS": "BIS CSL",
    }
    allowed_labels = {label_for.get(l.upper(), l.upper()) for l in lists}
    return [m for m in matches if (m.get("source") or "") in allowed_labels]


def _append_history(monitor: dict[str, Any], event: str, detail: str = "") -> None:
    hist = monitor.setdefault("history", [])
    hist.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "detail": detail,
    })
    # Trim to the cap.
    if len(hist) > HISTORY_CAP:
        del hist[: len(hist) - HISTORY_CAP]


def deliver_webhook(webhook_url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """POST the payload to the webhook URL with a 10s timeout and one retry.

    Returns (success, detail_message). Best-effort: never raises. Each
    delivery attempt (success or failure) is appended to
    `data/monitor-log.jsonl` as one JSON object per line for audit.
    """
    if not webhook_url:
        _log_delivery(payload, False, "no webhook_url configured")
        return False, "no webhook_url configured"
    last_err = ""
    ok = False
    for attempt in (1, 2):
        try:
            with httpx.Client(
                timeout=WEBHOOK_TIMEOUT, verify=certifi.where(), follow_redirects=True,
                headers=WEBHOOK_HEADERS,
            ) as c:
                r = c.post(webhook_url, json=payload)
            if 200 <= r.status_code < 300:
                detail = f"delivered (HTTP {r.status_code}) on attempt {attempt}"
                _log_delivery(payload, True, detail, attempt=attempt, http_status=r.status_code)
                return True, detail
            last_err = f"HTTP {r.status_code} on attempt {attempt}"
            _log_delivery(payload, False, last_err, attempt=attempt, http_status=r.status_code)
        except httpx.HTTPError as exc:
            last_err = f"{type(exc).__name__}: {exc} on attempt {attempt}"
            _log_delivery(payload, False, last_err, attempt=attempt)
    return False, last_err


def _log_delivery(
    payload: dict[str, Any],
    ok: bool,
    detail: str,
    attempt: int = 1,
    http_status: int | None = None,
) -> None:
    """Append one JSON line per delivery attempt to data/monitor-log.jsonl.

    Best-effort: any write error is swallowed so it never breaks a check
    cycle. The log is append-only; rotate it externally if it grows large.
    """
    try:
        os.makedirs(os.path.dirname(MONITOR_LOG_PATH), exist_ok=True)
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "monitor_id": payload.get("monitor_id"),
            "event": payload.get("event"),
            "ok": ok,
            "attempt": attempt,
            "detail": detail,
        }
        if http_status is not None:
            record["http_status"] = http_status
        with open(MONITOR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Check runner
# ---------------------------------------------------------------------------

def run_checks(
    screen_fn,
    now_iso: str | None = None,
    deliver: bool = True,
) -> dict[str, Any]:
    """Run a check cycle across all registered monitors.

    `screen_fn` is a callable `(name: str) -> list[dict]` that returns the
    matches for a name (typically a thin wrapper around app.main._screen_all).
    We re-screen each monitor's name, compute the new match signature, and
    when it differs from the last run we fire the webhook and record the
    event.

    Returns a summary dict: {checked, fired, failed, deliveries}.
    """
    monitors = _load_all()
    now = now_iso or datetime.now(timezone.utc).isoformat()
    summary = {"checked": 0, "fired": 0, "failed": 0, "deliveries": []}

    for monitor_id, monitor in monitors.items():
        summary["checked"] += 1
        name = monitor.get("name") or ""
        if not name:
            continue
        try:
            all_matches = screen_fn(name)
        except Exception as exc:  # noqa: BLE001 - best-effort runner
            _append_history(monitor, "screen_error", str(exc))
            monitor["last_check"] = now
            summary["failed"] += 1
            continue
        matches = _filter_matches_by_lists(all_matches, monitor.get("lists") or [])
        sig = _match_signature(matches)
        prev_sig = monitor.get("last_match_signature")
        monitor["last_check"] = now

        if sig == prev_sig:
            _append_history(monitor, "no_change", "no new matches")
            continue

        # Signature changed -> we have a new designation (or a removal).
        # Identify which matches are NEW relative to the previous run. We
        # approximate "new" as: every match in this run whose (source,
        # entity_id) pair was not in the previous signature. On the very
        # first run (prev_sig is None) we treat the entire match set as new
        # so the customer gets an initial baseline webhook.
        prev_pairs: set[tuple[str, str]] = set()
        if prev_sig:
            for tok in prev_sig.split("|"):
                if ":" in tok:
                    s, e = tok.split(":", 1)
                    prev_pairs.add((s, e))
        new_matches = [
            m for m in matches
            if ((m.get("source") or ""), str(m.get("entity_id") or "")) not in prev_pairs
        ]

        _append_history(
            monitor,
            "new_match",
            f"{len(new_matches)} new match(es), signature changed",
        )
        monitor["last_match_signature"] = sig

        if not new_matches and prev_sig is not None:
            # The set shrank (a designation was removed). We still record the
            # change but do not fire a "new_match" webhook.
            continue

        if not deliver:
            continue

        # Fire one webhook per new match (so the customer can act on each
        # designation independently) -- but cap at 5 per cycle to avoid
        # hammering a webhook if a whole list was re-published.
        for nm in new_matches[:5]:
            payload = {
                "event": "new_match",
                "monitor_id": monitor_id,
                "name": name,
                "new_match": nm,
                "screened_at": now,
            }
            ok, detail = deliver_webhook(monitor.get("webhook_url") or "", payload)
            summary["deliveries"].append({
                "monitor_id": monitor_id,
                "ok": ok,
                "detail": detail,
            })
            if ok:
                _append_history(monitor, "delivery_ok", detail)
            else:
                _append_history(monitor, "delivery_failed", detail)
        summary["fired"] += 1

    _save_all(monitors)
    return summary