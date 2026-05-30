import json
from sqlalchemy import text
from app.models import Event, IngestRequest, IngestResponse


def ingest_events(request: IngestRequest, conn) -> IngestResponse:
    accepted = 0
    rejected = 0
    errors = []

    for event in request.events:
        try:
            # Validate event_type
            valid_types = {
                "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
                "ZONE_DWELL", "BILLING_QUEUE_JOIN",
                "BILLING_QUEUE_ABANDON", "REENTRY"
            }
            if event.event_type not in valid_types:
                raise ValueError(f"Invalid event_type: {event.event_type}")

            # Validate confidence range
            if not (0.0 <= event.confidence <= 1.0):
                raise ValueError(f"Confidence must be 0-1, got {event.confidence}")

            metadata_str = event.metadata.model_dump_json() if event.metadata else None

            # INSERT OR IGNORE = idempotency (same event_id twice is safe)
            conn.execute(text("""
                INSERT OR IGNORE INTO events
                (event_id, store_id, camera_id, visitor_id, event_type,
                 timestamp, zone_id, dwell_ms, is_staff, confidence, metadata)
                VALUES
                (:event_id, :store_id, :camera_id, :visitor_id, :event_type,
                 :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence, :metadata)
            """), {
                "event_id":   event.event_id,
                "store_id":   event.store_id,
                "camera_id":  event.camera_id,
                "visitor_id": event.visitor_id,
                "event_type": event.event_type,
                "timestamp":  event.timestamp,
                "zone_id":    event.zone_id,
                "dwell_ms":   event.dwell_ms,
                "is_staff":   1 if event.is_staff else 0,
                "confidence": event.confidence,
                "metadata":   metadata_str,
            })
            accepted += 1

        except Exception as e:
            rejected += 1
            errors.append({
                "event_id": getattr(event, "event_id", "unknown"),
                "error": str(e)
            })

    conn.commit()
    return IngestResponse(accepted=accepted, rejected=rejected, errors=errors)