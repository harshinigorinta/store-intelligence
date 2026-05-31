# DESIGN.md — Store Intelligence System
## Brigade Road Bangalore — STORE_BLR_002

---

## Plain-Language Overview

This system solves a specific business problem: Purplle's physical stores are a data blind spot. Online channels track every click and drop-off in real time, but in-store behaviour is invisible. My system closes that gap by turning raw CCTV footage into the same kind of session-level analytics that online teams take for granted.

The north star metric driving every decision is **offline store conversion rate** — the fraction of visitors who complete a purchase. Every stage of the pipeline either improves the accuracy of that number (detection layer) or makes it actionable (API layer).

The system is fully deployed:
- **Live Dashboard**: https://store-intelligence-ten.vercel.app
- **Backend API**: https://store-intelligence-production-325f.up.railway.app
- **Swagger UI**: https://store-intelligence-production-325f.up.railway.app/docs

---

## System Architecture

```
CCTV Clips (5 cameras — CAM 1 through CAM 5)
        │
        ▼
┌─────────────────────────────────────────────┐
│           Detection Layer (pipeline/)        │
│  YOLOv8s → ByteTrack → Zone Classifier      │
│  Staff Detector (HSV) → Re-ID → Emitter     │
└──────────────────────┬──────────────────────┘
                       │ JSONL events
                       ▼
┌─────────────────────────────────────────────┐
│           Intelligence API (app/)            │
│  FastAPI + SQLite (SQLAlchemy ORM)           │
│  POST /events/ingest  (idempotent)           │
│  GET  /stores/{id}/metrics                   │
│  GET  /stores/{id}/funnel                    │
│  GET  /stores/{id}/heatmap                   │
│  GET  /stores/{id}/anomalies                 │
│  GET  /health                                │
└──────────────────────┬──────────────────────┘
                       │ HTTP polling (3s)
                       ▼
┌─────────────────────────────────────────────┐
│           Live Dashboard                     │
│  Vercel (HTML/JS) — auto-refresh every 3s   │
│  Terminal (rich) — for local development     │
└─────────────────────────────────────────────┘
```

---

## Stage 1 — Detection Layer

### Person Detection
I used **YOLOv8s** via the Ultralytics library. The model runs on every other frame (FRAME_SKIP=2) to keep processing time reasonable on CPU without sacrificing tracking quality. I chose the small variant (s) over nano (n) because the billing zone clips have crowded, partially occluded scenes — nano struggled with overlapping people in early testing.

I deliberately chose not to use YOLOv8m or larger because the submission must run with `docker compose up` on any machine including CPU-only. The small model processes the 5 clips in under 30 minutes on CPU, which is acceptable for a batch pipeline.

### Multi-Object Tracking
**ByteTrack** handles tracking, built directly into Ultralytics. ByteTrack's key advantage for retail CCTV is that it maintains track continuity during brief occlusions using Kalman filter predictions. When a customer steps behind a display and reappears 2-3 frames later, ByteTrack keeps the same track_id rather than creating a new one, preventing spurious ENTRY/EXIT pairs.

### Re-ID and Visitor Token Assignment
Each track_id gets mapped to a `visitor_id` token (format: `VIS_xxxxxx`). For re-entry detection, I use **bounding box proximity matching**: when a new track appears within 150 pixels of an exited visitor's last known position, it's flagged as REENTRY rather than a new ENTRY. This directly solves the re-entry inflation problem.

I considered using OSNet embeddings (torchreid) for appearance-based Re-ID. Claude suggested this during design, noting it would give better accuracy especially when customers re-enter from different directions. I agreed with the technical argument but overrode it: OSNet requires GPU inference at acceptable speed, and the submission must run on CPU. Proximity Re-ID is sufficient for 20-minute clips and I documented this trade-off explicitly.

### Staff Detection
Staff wear uniforms — consistent single-colour clothing. I exploit this with an **HSV colour histogram**: crop the bounding box, convert to HSV, check if more than 60% of pixels fall in a narrow hue band (18-bin histogram). If yes, `is_staff=true`. This heuristic runs in microseconds per frame and works well for the Brigade Road footage where staff wear a consistent uniform colour.

### Zone Classification
I used **Shapely polygon overlap** with normalised coordinates (0-1 range). Zone polygons come from `store_layout.json`, which I built from the actual Brigade Road store layout Excel file provided in the challenge resources. I mapped all real zones — FACES, LAKME, DERMDOC, MINIMALIST, FOXTALE, NYBAE, ALPS, FOH, MAKEUP_UNIT, FRAGRANCE, and BILLING — to normalised polygons per camera.

When a person's bounding box centre falls inside a polygon associated with the current camera, ZONE_ENTER is emitted. ZONE_DWELL fires every 30 seconds of continued presence. ZONE_EXIT fires when the centre leaves the polygon.

### Entry/Exit Direction Detection
The entry camera (CAM 1) covers the store entrance. I detect direction using the vertical position of the bounding box centroid between consecutive frames: centroids moving downward (into store) emit ENTRY, centroids moving upward (toward exit) emit EXIT. This is more reliable than a fixed threshold line because it handles customers who pause at the entrance.

### Confidence Handling
I never suppress low-confidence detections. Every detection is stored with its actual confidence value. The problem statement explicitly requires this: "detection confidence must degrade gracefully, not fail silently." A detection with confidence=0.25 that happens to be the only record of a re-entry is more valuable than a clean log with a missing REENTRY event.

---

## Stage 2 — Event Stream

### Schema Design
The event schema has a flat core and a JSON metadata blob for event-specific optional fields:

```json
{
  "event_id": "uuid-v4",
  "store_id": "STORE_BLR_002",
  "camera_id": "CAM_ENTRY_01",
  "visitor_id": "VIS_c8a2f1",
  "event_type": "ZONE_DWELL",
  "timestamp": "2026-04-10T14:22:10Z",
  "zone_id": "FACES",
  "dwell_ms": 45000,
  "is_staff": false,
  "confidence": 0.87,
  "metadata": {
    "queue_depth": null,
    "sku_zone": "MAKEUP",
    "session_seq": 3
  }
}
```

The metadata blob handles event-specific fields (queue_depth for billing events, sku_zone from the store layout, session_seq for debugging visitor journeys) without creating sparse nullable columns in the main schema. This was suggested by Claude during design review — I agreed because the schema needs to be stable as new event types are added.

### Timestamp Derivation
Timestamps are derived from `clip_start_time + (frame_number / fps)`. I set clip_start_time to `2026-04-10T10:00:00Z` — matching the real Brigade Road sales date from the POS CSV provided in the challenge resources.

### POS Correlation
Conversion is attributed by time window: a visitor in the BILLING zone within 5 minutes before a transaction timestamp counts as converted. I used the real invoice timestamps from the Brigade Road CSV (ML0426KAP0001324 through ML0426KAP0001443, spanning 12:15 to 21:39 IST on 10 April 2026).

---

## Stage 3 — Intelligence API

### Storage
SQLite with SQLAlchemy. The database is created automatically at startup — no manual steps. Two indexes: `(store_id, timestamp)` for time-range queries and `(visitor_id)` for session lookups. I chose SQLite over PostgreSQL specifically because the acceptance gate requires `docker compose up` with no manual setup — PostgreSQL requires a separate container and credentials management.

### Idempotency
`INSERT OR IGNORE` on `event_id` as primary key. The same batch posted twice produces identical database state. Verified by `test_ingest_idempotent` in the test suite.

### Metrics Computation
All metrics are computed at query time from raw events — no pre-aggregation. This means numbers are always current. The trade-off is query latency at high event volumes. At 40 live stores, I would add a Redis cache with 30s TTL in front of metric queries.

### Funnel Logic
The funnel counts unique `visitor_id` values at each stage. A visitor who re-enters (REENTRY) still counts as one funnel entry because ENTRY and REENTRY share the same `visitor_id`. The session is the unit, not the event count.

### Anomaly Detection
Four anomaly types with explicit thresholds:
- **BILLING_QUEUE_SPIKE**: queue_depth ≥ 5 → WARN, ≥ 8 → CRITICAL
- **CONVERSION_DROP**: today's rate < 80% of 7-day rolling average → WARN
- **DEAD_ZONE**: no zone visits in 30 minutes → INFO
- **STALE_FEED**: no events in 10 minutes → WARN

Each anomaly includes a `suggested_action` string — directly actionable for store managers without requiring them to interpret raw metrics.

---

## Stage 4 — Live Dashboard

Two frontends:

**Web Dashboard (Vercel)**: Single-file HTML/JS dashboard deployed at https://store-intelligence-ten.vercel.app. Polls the Railway API every 3 seconds. Shows visitor count, conversion rate, zone heatmap with colour-coded intensity, conversion funnel with animated bars, anomaly alerts with severity badges, and system health. No framework dependencies — pure HTML, CSS, and vanilla JavaScript.

**Terminal Dashboard**: Built with the `rich` library for local development. Polls the API every 2 seconds. Useful for demonstrating the system without a browser.

---

## AI-Assisted Decisions

### 1. Re-ID approach — where I overrode Claude

Claude strongly recommended **OSNet via torchreid** for appearance-based Re-ID, arguing it would give better accuracy on re-entry detection, especially when customers re-enter from different directions. I agreed with the technical reasoning but overrode the recommendation.

My reasoning: OSNet requires GPU inference to run at acceptable speed for video processing. The submission must pass `docker compose up` on any machine including CPU-only reviewers. Proximity-based Re-ID (150px threshold, confidence > 0.6) is sufficient for 20-minute clips and adds zero infrastructure requirements. I documented this limitation — proximity Re-ID breaks when two customers enter from the same direction within 3 seconds. This is a known trade-off I made consciously.

**Result**: I overrode the AI recommendation. The correct production choice is OSNet, but the correct challenge choice is proximity matching.

### 2. Anomaly thresholds — where I agreed with Claude

I asked Claude what queue depth thresholds are realistic for a specialty retail billing counter like Purplle's Brigade Road store. Claude suggested WARN at 5 customers and CRITICAL at 8, reasoning that with 2-3 minutes per transaction, a queue of 8 means a 16-24 minute wait — past the point where customers abandon.

I thought this was well-calibrated for a beauty retail environment (higher dwell tolerance than a supermarket) and adopted it. I also adopted Claude's suggestion to include a `suggested_action` string per anomaly — one sentence that tells a store manager what to do, not just what's wrong.

**Result**: I agreed with the AI recommendation and adopted both thresholds and the suggested_action pattern.

### 3. Schema session_seq field — where I partially agreed

Claude's initial schema review didn't include `session_seq`. When I asked it to check the schema against the problem statement, it suggested adding session_seq as the ordinal position of each event in a visitor's session — makes debugging visitor journeys significantly easier.

I agreed and added it. However, Claude also suggested a separate `session_id` field distinct from `visitor_id`. I rejected this: the problem statement defines the session as the period between ENTRY and EXIT for a given visitor_id. A separate session_id would add complexity without adding precision for the metrics being computed.

**Result**: Partial agreement — added session_seq, rejected separate session_id.

---

## Trade-offs and Known Limitations

**SQLite write concurrency**: SQLite serialises writes. At 40 live stores sending events in real time, the write queue would build up. Fix: enable WAL mode (one-line change). Long-term: migrate to PostgreSQL with PgBouncer connection pooling.

**Proximity Re-ID false positives**: Two customers entering from the same door within 3 seconds may cause the second to be incorrectly flagged as REENTRY. This artificially deflates unique visitor counts. OSNet embeddings would solve this at the cost of GPU requirements.

**Zone polygon accuracy**: Polygons in store_layout.json are approximations from the floor plan. Cameras have perspective distortion — a person at the back of a zone appears smaller. Production deployment would require per-camera homography calibration.

**POS correlation window**: The 5-minute window is an approximation. A customer could be at the billing counter for 10+ minutes before their transaction processes. A direct POS integration feed would improve accuracy.

**Batch vs streaming**: The pipeline processes pre-recorded clips. True real-time operation requires consuming a live RTSP stream. The architecture supports this — events are emitted frame-by-frame — but has not been tested on live streams.