# DESIGN.md — Store Intelligence System

## Overview

This system converts raw CCTV footage from retail stores into a live analytics API.
It is structured as a four-stage pipeline: Detection → Event Stream → Intelligence API → Dashboard.

The north star metric driving every design decision is **offline store conversion rate**:
unique visitors who completed a purchase divided by total unique visitors in a session window.

---

## Architecture

```
CCTV Clips
    │
    ▼
Detection Layer (pipeline/detect.py)
  - YOLOv8s for person detection (1080p @ 15fps)
  - ByteTrack for multi-object tracking
  - OSNet Re-ID for visitor_id assignment across frames
  - Rule-based zone classification using shapely polygon overlap
  - Staff detection via uniform colour heuristic + optional VLM prompt
    │
    ▼
Event Stream (pipeline/emit.py)
  - Structured JSONL events emitted per visitor action
  - Schema: event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence, metadata
  - Timestamps derived from clip start time + frame offset / fps
    │
    ▼
Intelligence API (app/)
  - FastAPI + SQLite (via SQLAlchemy)
  - POST /events/ingest   — idempotent batch ingest (INSERT OR IGNORE)
  - GET  /stores/{id}/metrics    — real-time visitor + conversion metrics
  - GET  /stores/{id}/funnel     — conversion funnel by session
  - GET  /stores/{id}/heatmap    — zone dwell heatmap normalised 0-100
  - GET  /stores/{id}/anomalies  — queue spike, conversion drop, dead zone
  - GET  /health                 — feed lag + stale feed detection
    │
    ▼
Live Dashboard (dashboard/live.py)
  - Terminal dashboard using rich library
  - Polls /metrics every 2 seconds
  - Shows visitor count, conversion rate, queue depth updating live
```

---

## Key Design Decisions

### 1. SQLite over PostgreSQL
SQLite was chosen because the acceptance gate requires `docker compose up` with no manual
steps. SQLite needs zero infrastructure — no separate database container, no init scripts,
no connection strings to configure. The data volume for 5 stores over 20-minute clips is
well within SQLite's capabilities. At production scale (40 live stores), I would migrate
to PostgreSQL with TimescaleDB for time-series queries.

### 2. Idempotency via INSERT OR IGNORE
The ingest endpoint uses SQLite's `INSERT OR IGNORE` with `event_id` as the primary key.
This means the same batch can be posted twice safely — the second call silently skips
already-stored events. This is the simplest possible correct implementation and is
verified by the `test_ingest_idempotent` test.

### 3. Session-based funnel, not event-based
The funnel counts unique `visitor_id` values at each stage, not raw event counts. This
means a visitor who triggers 10 `ZONE_ENTER` events still counts as 1 funnel entry.
Re-entries (REENTRY events) share the same `visitor_id` as the original session, so they
are also deduplicated automatically.

### 4. Confidence passed through, never suppressed
Low-confidence detections are stored with their actual confidence value rather than
being dropped. This allows downstream analysis to apply different thresholds without
reprocessing. The API's metric queries do not filter by confidence — operators can
adjust this threshold in the detection pipeline config.

### 5. Partial success on ingest
When a batch contains malformed events, the API accepts valid events and rejects only
the malformed ones, returning a structured error per rejected event. This is production
behaviour — a single bad event should not block an entire batch of 500.

---

## AI-Assisted Decisions

### 1. Event schema design
I asked Claude to review the initial schema draft. It suggested adding `session_seq`
(ordinal position of the event within a visitor session) to the metadata block. I agreed
and included it — it makes debugging visitor journeys significantly easier and costs
nothing at emit time.

### 2. Anomaly severity thresholds
I asked Claude what queue depth thresholds are realistic for a retail billing counter.
It suggested WARN at 5, CRITICAL at 8, citing typical service time of ~3 minutes per
customer. I used these thresholds and noted them in CHOICES.md.

### 3. Re-entry definition
I asked Claude whether a visitor who exits and returns within 2 minutes should be treated
as a re-entry or a new session. Claude suggested 5 minutes as a reasonable threshold
based on typical retail browsing patterns. I overrode this with a Re-ID similarity
approach instead — if the OSNet embedding distance is below threshold, it is a re-entry
regardless of time gap, because time alone is not reliable when the store is crowded.

---

## Trade-offs and Limitations

- **SQLite write concurrency**: SQLite serialises writes, which limits ingest throughput
  under high concurrency. For 40 live stores this would require either WAL mode or
  migration to PostgreSQL.

- **Detection pipeline is batch, not streaming**: The pipeline processes pre-recorded
  clips. For true real-time operation, the detection loop would need to consume a live
  RTSP stream and emit events with sub-second latency.

- **POS correlation is approximate**: Conversion is attributed by time window (5 minutes
  before a transaction), not by individual customer identity. This is intentional — the
  POS data contains no customer_id — but it means conversion rate is an estimate, not
  an exact count.