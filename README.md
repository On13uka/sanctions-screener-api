# Sanctions Screener API

Screen names against OFAC SDN (US Treasury) and UN Consolidated sanctions lists. Unified JSON response with match score, source, entity details, and AKAs.

## Data sources

- **OFAC SDN List** (US Treasury) - 19,000+ entities, refreshed on startup
- **UN Security Council Consolidated List** - 1,000+ entities, refreshed on startup

Total: 20,000+ sanctioned individuals, entities, and vessels in memory.

## Endpoints

### GET /screen?name=John+Doe&threshold=0.7

Screen a name against both sanctions lists. Returns matches above threshold with match score.

**Parameters:**
- `name` (required): Name to screen
- `threshold` (optional, default 0.7): Minimum match score (0.0-1.0)
- `refresh` (optional, default false): Force refresh sanctions data

**Example:**

```bash
curl "https://your-app.onrender.com/screen?name=Kim+Jong&threshold=0.7"
```

**Response:**

```json
{
  "query": "Kim Jong",
  "threshold": 0.7,
  "total_matches": 1,
  "ofac_matches": 0,
  "un_matches": 1,
  "matches": [
    {
      "source": "UN Consolidated",
      "entity_id": "6908643",
      "name": "KIM JONG SIK",
      "type": "Individual",
      "program": "2017-12-22",
      "remarks": "Gender: male",
      "matched_aka": null,
      "match_score": 0.85,
      "match_type": "name"
    }
  ],
  "data_updated": {
    "ofac": "2026-07-15T12:33:32.973784+00:00",
    "un": "2026-07-15T12:33:10.981273+00:00"
  },
  "screened_at": "2026-07-15T12:35:12.051077+00:00"
}
```

### GET /status

Returns data loading status and entity counts per source.

### GET /health

Returns `{"status": "ok"}`.

## Match scoring

| Score | Match type | Example |
|---|---|---|
| 1.0 | exact | "Al Qaeda" = "AL QAIDA" (normalized) |
| 0.85 | starts_with / partial | "Kim Jong" matches "KIM JONG SIK" |
| 0.7 | contains | "Al" in "AL FURQAN" |
| 0.5-0.6 | token overlap | shared words between query and target |

## Local development

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8127
```

Note: First startup downloads 28MB OFAC XML + 2MB UN XML (~30s).

## Pricing for RapidAPI

- Free: 100 requests/month
- Pro: $29/month - unlimited requests, all sources
- Enterprise: $99/month - API access, webhook alerts, bulk screening
