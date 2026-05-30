from sqlalchemy import text
from datetime import datetime, timezone, timedelta


def get_anomalies(store_id: str, conn) -> dict:
    anomalies = []
    now = datetime.now(timezone.utc)

    # --- 1. BILLING_QUEUE_SPIKE ---
    # Get latest queue depth from metadata
    queue_row = conn.execute(text("""
        SELECT metadata FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"store_id": store_id}).fetchone()

    if queue_row and queue_row[0]:
        import json
        try:
            meta = json.loads(queue_row[0])
            queue_depth = meta.get("queue_depth") or 0
            if queue_depth >= 5:
                anomalies.append({
                    "type": "BILLING_QUEUE_SPIKE",
                    "severity": "CRITICAL" if queue_depth >= 8 else "WARN",
                    "detail": f"Queue depth is {queue_depth}",
                    "suggested_action": "Open an additional billing counter immediately."
                })
        except Exception:
            pass

    # --- 2. CONVERSION_DROP ---
    # Compare today's conversion rate vs last 7 days average
    today_str = now.date().isoformat()

    today_entries = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'ENTRY'
          AND DATE(timestamp) = :today
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    today_purchases = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND DATE(timestamp) = :today
          AND visitor_id NOT IN (
              SELECT visitor_id FROM events
              WHERE store_id = :store_id
                AND event_type = 'BILLING_QUEUE_ABANDON'
                AND DATE(timestamp) = :today
          )
    """), {"store_id": store_id, "today": today_str}).scalar() or 0

    today_rate = today_purchases / today_entries if today_entries > 0 else None

    week_ago = (now - timedelta(days=7)).date().isoformat()
    hist_entries = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'ENTRY'
          AND DATE(timestamp) BETWEEN :week_ago AND :today
    """), {"store_id": store_id, "week_ago": week_ago, "today": today_str}).scalar() or 0

    hist_purchases = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id) FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND DATE(timestamp) BETWEEN :week_ago AND :today
          AND visitor_id NOT IN (
              SELECT visitor_id FROM events
              WHERE store_id = :store_id
                AND event_type = 'BILLING_QUEUE_ABANDON'
                AND DATE(timestamp) BETWEEN :week_ago AND :today
          )
    """), {"store_id": store_id, "week_ago": week_ago, "today": today_str}).scalar() or 0

    hist_rate = hist_purchases / hist_entries if hist_entries > 0 else None

    if today_rate is not None and hist_rate is not None and hist_rate > 0:
        drop_pct = (hist_rate - today_rate) / hist_rate * 100
        if drop_pct >= 20:
            anomalies.append({
                "type": "CONVERSION_DROP",
                "severity": "CRITICAL" if drop_pct >= 40 else "WARN",
                "detail": f"Conversion rate dropped {round(drop_pct, 1)}% vs 7-day average",
                "suggested_action": "Check for staff shortages, product stockouts, or pricing issues."
            })

    # --- 3. DEAD_ZONE ---
    # Any zone with zero visits in the last 30 minutes
    thirty_min_ago = (now - timedelta(minutes=30)).isoformat()

    active_zones = conn.execute(text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND zone_id IS NOT NULL
          AND timestamp >= :cutoff
    """), {"store_id": store_id, "cutoff": thirty_min_ago}).fetchall()

    all_zones = conn.execute(text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id = :store_id
          AND zone_id IS NOT NULL
    """), {"store_id": store_id}).fetchall()

    active_set = {r[0] for r in active_zones}
    all_set = {r[0] for r in all_zones}
    dead_zones = all_set - active_set

    for zone in dead_zones:
        anomalies.append({
            "type": "DEAD_ZONE",
            "severity": "INFO",
            "detail": f"Zone '{zone}' has had no visitor activity in the last 30 minutes.",
            "suggested_action": f"Check if zone '{zone}' display or signage needs attention."
        })

    # --- 4. STALE_FEED (also reported in /health but useful here too) ---
    last_event = conn.execute(text("""
        SELECT timestamp FROM events
        WHERE store_id = :store_id
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"store_id": store_id}).scalar()

    if last_event:
        try:
            last_ts = datetime.fromisoformat(last_event.replace("Z", "+00:00"))
            lag_minutes = (now - last_ts).total_seconds() / 60
            if lag_minutes > 10:
                anomalies.append({
                    "type": "STALE_FEED",
                    "severity": "WARN",
                    "detail": f"Last event received {round(lag_minutes, 1)} minutes ago.",
                    "suggested_action": "Check camera connectivity and pipeline health."
                })
        except Exception:
            pass

    return {
        "store_id": store_id,
        "anomalies": anomalies,
        "checked_at": now.isoformat(),
    }