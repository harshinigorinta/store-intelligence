# CHOICES.md — Technical Decisions

## Decision 1: Detection Model — YOLOv8s + ByteTrack

### Options Considered
- **YOLOv8n** (nano): Fastest, lowest memory, but lower accuracy on partial occlusion
- **YOLOv8s** (small): Good balance of speed and accuracy at 1080p/15fps
- **RT-DETR**: Better accuracy on crowded scenes, but slower and harder to integrate with ByteTrack
- **MediaPipe**: Easy to use but not designed for multi-person retail tracking

### What AI Suggested
I asked Claude to compare YOLOv8 variants for retail CCTV at 1080p/15fps. It recommended
YOLOv8s over nano because the billing queue edge case (crowded, partial occlusion) would
cause significant missed detections with the nano model. It also suggested RT-DETR as a
stretch option if accuracy was the top priority over speed.

### What I Chose and Why
**YOLOv8s + ByteTrack.** The small model gives sufficient accuracy for the 15fps clips
without requiring a GPU in the Docker container. ByteTrack is built into Ultralytics,
so integration is a single parameter change. RT-DETR would score higher on the billing
queue clip but would make the pipeline significantly slower and harder to run locally.

For staff detection I used a two-step approach: bounding box crop → HSV colour histogram
to detect uniform colours (typically a single dominant hue). I considered using a VLM
(Claude Vision / GPT-4V) for staff detection with the prompt:
*"Is the person in this image wearing a retail staff uniform with a consistent single colour?
Answer only YES or NO."*
The VLM approach was more accurate on ambiguous cases but added ~200ms per detection.
I used the colour heuristic for speed and noted the VLM as a production upgrade path.

---

## Decision 2: Event Schema Design

### Options Considered
- **Flat schema**: All fields at the top level, simple to query
- **Nested metadata**: Core fields flat, optional/event-specific fields in a JSON metadata blob
- **Separate tables per event type**: Normalised, but complex joins for funnel queries

### What AI Suggested
Claude suggested the nested metadata approach — keeping the core schema stable while
allowing event-specific fields (queue_depth, session_seq, sku_zone) to live in a JSON
blob. It argued this avoids sparse columns (most events have null queue_depth) and makes
schema evolution easier. It also suggested adding `session_seq` — the ordinal position
of each event within a visitor's session — which I had not included in my first draft.

### What I Chose and Why
**Nested metadata blob, as suggested.** I agreed with the reasoning. SQLite stores JSON
as TEXT and I parse it at query time only when needed (e.g., reading queue_depth for
anomaly detection). The `session_seq` addition was good — I kept it.

One place I overrode the AI: Claude initially suggested storing `session_id` as a
separate field distinct from `visitor_id`. I rejected this because the problem statement
defines the session as the unit between ENTRY and EXIT for a given visitor_id. Adding a
separate session_id would complicate re-entry handling without adding precision.

---

## Decision 3: API Storage Engine — SQLite over PostgreSQL

### Options Considered
- **SQLite**: Zero infrastructure, file-based, built into Python
- **PostgreSQL**: Production-grade, better concurrency, supports TimescaleDB for time-series
- **Redis**: Fast in-memory, good for real-time counters, but no persistence by default
- **DuckDB**: Excellent for analytical queries, but less mature for transactional ingest

### What AI Suggested
Claude strongly recommended PostgreSQL with a TimescaleDB extension for the time-series
nature of the event data. It argued that zone dwell queries and funnel aggregations over
large event sets would benefit from hypertable partitioning by timestamp. It also noted
that SQLite's serialised writes would become a bottleneck at 40 live stores.

### What I Chose and Why
**SQLite**, overriding the AI recommendation. The acceptance gate requirement —
`docker compose up` with no manual steps beyond `git clone` — is a hard constraint.
PostgreSQL requires a separate container, initialisation scripts, and connection string
configuration. For the 5-store, 20-minute clip dataset, SQLite is entirely sufficient.

I documented the PostgreSQL migration path explicitly: enable WAL mode on SQLite for
better read concurrency now, migrate to PostgreSQL + TimescaleDB when the store count
exceeds 10 and event volume exceeds 1M events/day. The AI's recommendation is correct
for production — it was wrong for this specific submission constraint.