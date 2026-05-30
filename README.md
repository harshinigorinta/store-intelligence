# Store Intelligence System
**Purplle Tech Challenge 2026 — Round 2**

End-to-end pipeline from raw CCTV footage to a live store analytics API.

---

## Quick Start (5 commands)

```bash
git clone https://github.com/harshinigorinta/store-intelligence.git
cd store-intelligence
# Add your MP4 clips to data/clips/ folder
docker compose up -d
curl http://localhost:8000/health
```

API is live at: http://localhost:8000
Interactive docs: http://localhost:8000/docs

---

## Running the Detection Pipeline

### 1. Install pipeline dependencies
```bash
pip install ultralytics opencv-python shapely
```

### 2. Add video clips
Place MP4 files in `data/clips/`. Expected files:
- `CAM 1.mp4` — Entry/Exit camera
- `CAM 2.mp4` — Main floor camera
- `CAM 3.mp4` — Main floor camera 2
- `CAM 4.mp4` — Billing area camera
- `CAM 5.mp4` — Billing area camera 2

### 3. Run detection on a single clip
```bash
python -m pipeline.detect --clip "CAM 4.mp4" --layout data/store_layout.json --output data/events.jsonl
```

### 4. Run detection on all clips
```bash
python -m pipeline.detect --layout data/store_layout.json --output data/events.jsonl
```

### 5. Feed generated events into the API
```bash
python feed_events.py
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/events/ingest` | Ingest up to 500 events (idempotent) |
| GET | `/stores/{id}/metrics` | Unique visitors, conversion rate, queue depth |
| GET | `/stores/{id}/funnel` | Entry → Zone → Billing → Purchase funnel |
| GET | `/stores/{id}/heatmap` | Zone dwell heatmap normalised 0-100 |
| GET | `/stores/{id}/anomalies` | Queue spike, conversion drop, dead zone alerts |
| GET | `/health` | Service status + stale feed detection |

### Example: Get store metrics
```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

### Example: Ingest events
```bash
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d '{"events": [...]}'
```

---

## Running Tests

```bash
pip install pytest pytest-cov httpx
pytest tests/ -v --cov=app --cov-report=term-missing
```

Expected: 13 tests passing, ~77% coverage.

---

## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py       # Main detection + tracking script (YOLOv8s + ByteTrack)
│   ├── tracker.py      # Re-ID + visitor session tracking
│   └── emit.py         # Event schema + JSONL emission
├── app/
│   ├── main.py         # FastAPI entrypoint + middleware
│   ├── models.py       # Pydantic event schema
│   ├── database.py     # SQLAlchemy + SQLite setup
│   ├── ingestion.py    # Ingest + deduplication
│   ├── metrics.py      # Real-time metrics + funnel + heatmap
│   ├── anomalies.py    # Anomaly detection
│   └── health.py       # Health check
├── tests/
│   └── test_metrics.py # 13 tests, 77% coverage
├── docs/
│   ├── DESIGN.md       # Architecture + AI-assisted decisions
│   └── CHOICES.md      # 3 key technical decisions
├── data/
│   ├── clips/          # MP4 files (not in repo)
│   ├── store_layout.json
│   ├── sample_events.jsonl
│   └── pos_transactions.csv
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── README.md
```

---

## Architecture

```
CCTV Clips → YOLOv8s Detection → ByteTrack Tracking → Event Stream (JSONL)
                                                              ↓
                                                    POST /events/ingest
                                                              ↓
                                                    SQLite (via SQLAlchemy)
                                                              ↓
                              ┌───────────────────────────────────────────┐
                              │  GET /metrics  GET /funnel  GET /heatmap  │
                              │  GET /anomalies             GET /health   │
                              └───────────────────────────────────────────┘
```

**North Star Metric:** Conversion Rate = Unique buyers ÷ Total unique visitors

---

## Detection Pipeline Details

- **Model:** YOLOv8s (person detection, class 0)
- **Tracker:** ByteTrack (built into Ultralytics)
- **Re-ID:** Bounding box proximity matching for re-entry detection
- **Staff detection:** HSV colour histogram (uniform = dominant single hue)
- **Zone classification:** Shapely polygon overlap with normalised coordinates
- **Frame skip:** Every 2nd frame processed for speed

## Edge Cases Handled

| Edge Case | Approach |
|-----------|----------|
| Group entry | Individual bounding boxes → individual ENTRY events |
| Staff movement | HSV uniform detection → `is_staff=true`, excluded from metrics |
| Re-entry | Bbox proximity Re-ID → REENTRY event, no double count |
| Partial occlusion | Confidence passed through, never suppressed |
| Billing queue | Person count in billing zone → queue_depth in metadata |
| Empty periods | API returns zero metrics, does not crash |

---
## Live Dashboard

Run the terminal dashboard (updates every 2 seconds):
```bash
python dashboard/live.py
```
Dashboard URL: http://localhost:8000 (API backend)
Terminal dashboard shows: unique visitors, conversion rate, funnel, anomalies live.
## Notes

- Video clips are not included in the repository (per challenge rules)
- YOLOv8s model (`yolov8s.pt`) is auto-downloaded on first run
- SQLite database is created automatically at `data/store.db`
- All timestamps are ISO-8601 UTC