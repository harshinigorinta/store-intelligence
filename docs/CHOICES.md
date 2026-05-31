# CHOICES.md — Key Technical Decisions

## Decision 1: Detection Model — YOLOv8s + ByteTrack

### The Question
Which object detection model and tracker should be used as the foundation for the pipeline?

### Options Considered

| Model | Pros | Cons |
|-------|------|------|
| YOLOv8n (nano) | Fastest, lowest memory, runs on any CPU | Low accuracy on partial occlusion, misses individuals in groups at distance |
| YOLOv8s (small) | Good speed/accuracy balance, runs well on CPU | Slightly less accurate than medium on crowded scenes |
| YOLOv8m (medium) | Better accuracy on crowded billing zone | Slower on CPU, VRAM risk at 1080p without GPU |
| RT-DETR | Strong on occlusion (transformer-based) | Memory-hungry, harder to integrate with ByteTrack, slower |
| MediaPipe | Very fast, CPU-native | Designed for close-up pose detection, poor at 3-6m CCTV distance |

For tracker:
| Tracker | Pros | Cons |
|---------|------|------|
| ByteTrack | Built into Ultralytics, motion-only (robust when faces blurred), handles occlusion via Kalman prediction | No appearance features |
| DeepSORT | Appearance + motion features | Appearance extractor relies on face/body texture — degraded when faces are blurred per challenge spec |
| StrongSORT | Best accuracy | Most complex, slowest |

### What AI Suggested
I asked Claude to evaluate detection models for 1080p CCTV footage with blurred faces, running on CPU. Claude recommended YOLOv8s as the sweet spot — not nano (too low accuracy for group detection at retail distances) and not medium (too slow without GPU). For tracking, Claude correctly identified that DeepSORT's appearance extractor is degraded by face blurring and recommended ByteTrack for its motion-only approach.

I also asked about using a VLM (GPT-4V or Claude Vision) for zone classification. Claude's initial suggestion was to use VLM zero-shot classification per bounding box crop. I evaluated this and rejected it for the real-time pipeline — at 15fps with 10+ people per frame, that would require thousands of API calls per minute at significant cost and latency.

### What I Chose and Why
**YOLOv8s + ByteTrack.**

The critical constraint is that the submission must run with `docker compose up` on any machine — including CPU-only. YOLOv8s processes 1080p frames in ~100-150ms on CPU with FRAME_SKIP=2, making the 5 clips processable in under 30 minutes. YOLOv8m would take 2-3x longer.

ByteTrack is the right tracker because:
1. All faces are blurred per the challenge spec — appearance-based trackers like DeepSORT lose their primary feature
2. ByteTrack is integrated into Ultralytics with a single parameter (`tracker="bytetrack.yaml"`)
3. ByteTrack's Kalman filter handles the partial occlusion edge case — when customers step behind displays, track continuity is maintained

For VLM zone classification: I considered using it for staff detection specifically (prompt: "Is this person wearing a consistent single-colour uniform? Yes/No"). Claude Vision would be more accurate than my HSV heuristic on ambiguous cases. I rejected it for the batch pipeline but documented it as a production upgrade path in DESIGN.md.

---

## Decision 2: Event Schema Design

### The Question
How should the event schema be structured to support all analytics queries while remaining extensible?

### Options Considered

**Option A — Flat schema**: All fields at the top level. Simple to parse but creates sparse nullable columns — most events don't have queue_depth, most don't have sku_zone. Schema changes require ALTER TABLE.

**Option B — Nested metadata blob**: Core fields flat, optional event-specific fields in a JSON `metadata` object. Matches the problem statement's sample schema. Extensible without schema changes.

**Option C — Polymorphic schemas per event type**: Separate schema for EntryEvent, ZoneDwellEvent, BillingEvent etc. Strongly typed but breaks batch ingest — a mixed batch of 500 events can't be validated uniformly.

### What AI Suggested
Claude suggested Option B and specifically called out two additions I hadn't included in my first draft:
1. `session_seq` — ordinal position of the event within a visitor's session. "Without this, debugging a visitor's journey means sorting by timestamp and hoping no timestamp collisions."
2. Keep `confidence` as a top-level field, not in metadata. "Filtering by confidence is a core operation — it shouldn't require JSON parsing."

I agreed with both. I rejected Claude's suggestion of a separate `session_id` field: the problem statement defines the session as the window between ENTRY and EXIT for a given `visitor_id`. Adding session_id would be redundant and would complicate re-entry handling (which session_id does a REENTRY belong to?).

### What I Chose and Why
**Option B — Nested metadata**, with:
- `confidence` at top level (not in metadata) — can filter in SQL WHERE clause without JSON parsing
- `is_staff` at top level — every metric query filters this, it must be a native column
- `session_seq` in metadata — for debugging, not for querying
- `queue_depth` in metadata — only meaningful for BILLING_QUEUE_JOIN events

The key design principle I held throughout: **never silently drop low-confidence detections**. The problem statement explicitly penalises this. A detection with confidence=0.22 that captures the only REENTRY event for a visitor is more valuable than a clean log with that event missing. Store every detection, flag the confidence, let operators tune thresholds downstream.

This principle directly influenced the `test_confidence_range` test — which verifies that all pipeline events have 0.0 ≤ confidence ≤ 1.0 and that none are set to a fake elevated value.

---

## Decision 3: API Storage — SQLite with documented PostgreSQL migration path

### The Question
What storage engine should back the Intelligence API?

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| SQLite | Zero-config, single file, Dockerizes trivially, no port conflicts, ACID compliant | Write concurrency limited (WAL mode helps), not horizontally scalable |
| PostgreSQL | Production-grade, full concurrency, partitioning, TimescaleDB extension | Requires separate container, startup delay, credentials management |
| Redis | Extremely fast for counters and real-time metrics | Not a relational store — funnel and heatmap queries need JOINs |
| DuckDB | Excellent analytical performance (columnar) | Less suitable for high-write OLTP ingest endpoint |

### What AI Suggested
Claude strongly recommended **PostgreSQL with TimescaleDB** for the time-series nature of the event data. It argued that:
1. Zone dwell aggregations and funnel queries over large event sets would benefit from TimescaleDB hypertable partitioning by timestamp
2. At 40 live stores, SQLite's serialised writes would become a bottleneck
3. TimescaleDB's continuous aggregates would make `/metrics` queries O(1) instead of O(n)

These are all correct arguments for production. I pushed back on two points:

**Point 1**: The challenge dataset is 5 clips, ~2800 events. SQLite handles this in microseconds. TimescaleDB's benefits only materialise at millions of events.

**Point 2**: The acceptance gate explicitly requires `docker compose up` with no manual steps beyond `git clone`. Adding PostgreSQL requires a second container, an init script, and environment variable coordination. Any failure in that sequence fails the acceptance gate.

Claude conceded both points and agreed that SQLite is the correct choice for the challenge, with a documented migration path for production.

### What I Chose and Why
**SQLite**, with an explicit production migration path documented:

```python
# Current (challenge) — zero config, runs anywhere
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/store.db")

# Production (40 stores, live feed) — one env var change
# DATABASE_URL = "postgresql://user:pass@db:5432/apex_retail"
```

SQLAlchemy as the ORM means this is a one-line environment variable change. The schema uses only standard SQL — no SQLite-specific syntax that would require changes.

**What breaks first at 40 live stores**: SQLite's single-writer lock. With 40 stores each sending 500-event batches every 30 seconds, writes would queue behind each other. The fix sequence is:
1. Enable WAL mode: `PRAGMA journal_mode=WAL` — allows concurrent reads during writes, costs nothing
2. Migrate to PostgreSQL with PgBouncer for connection pooling
3. Add Redis cache (TTL=30s) in front of `/metrics` endpoint — 40 concurrent metric queries shouldn't each hit the DB

This is the answer I'd give in follow-up question 3 from the problem statement: "At 40 live stores sending events in real time, what is the first thing that breaks?"

The AI recommendation was correct for production. I disagreed with it for the challenge submission. Both positions are defensible and I've documented both.