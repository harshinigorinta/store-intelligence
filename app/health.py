from sqlalchemy import text
from datetime import datetime, timezone, timedelta


def get_health(conn) -> dict:
    now = datetime.now(timezone.utc)

    # Get last event timestamp per store
    rows = conn.execute(text("""
        SELECT store_id, MAX(timestamp) as last_ts
        FROM events
        GROUP BY store_id
    """)).fetchall()

    stores_status = []
    overall_status = "OK"

    for row in rows:
        store_id, last_ts_str = row
        status = "OK"
        lag_minutes = None

        if last_ts_str:
            try:
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                lag_minutes = round((now - last_ts).total_seconds() / 60, 1)
                if lag_minutes > 10:
                    status = "STALE_FEED"
                    overall_status = "DEGRADED"
            except Exception:
                status = "UNKNOWN"

        stores_status.append({
            "store_id": store_id,
            "last_event_timestamp": last_ts_str,
            "lag_minutes": lag_minutes,
            "feed_status": status,
        })

    # Total event count
    total_events = conn.execute(text("SELECT COUNT(*) FROM events")).scalar() or 0

    return {
        "status": overall_status,
        "checked_at": now.isoformat(),
        "total_events_ingested": total_events,
        "stores": stores_status,
    }