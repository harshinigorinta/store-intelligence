from sqlalchemy import text


def get_metrics(store_id: str, conn) -> dict:
    # Unique customer visitors today (exclude staff)
    unique_visitors = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'ENTRY'
    """), {"store_id": store_id}).scalar() or 0

    # Visitors who reached billing zone
    billing_visitors = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND zone_id IN ('BILLING', 'BILLING_COUNTER', 'BILLING_AREA')
    """), {"store_id": store_id}).scalar() or 0

    # Conversion rate
    conversion_rate = round(billing_visitors / unique_visitors, 4) if unique_visitors > 0 else 0.0

    # Average dwell per zone (exclude staff)
    zone_dwell_rows = conn.execute(text("""
        SELECT zone_id, AVG(dwell_ms) as avg_dwell
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'ZONE_DWELL'
          AND zone_id IS NOT NULL
        GROUP BY zone_id
    """), {"store_id": store_id}).fetchall()

    avg_dwell_per_zone = {
        row[0]: round(row[1] / 1000, 2)  # convert ms to seconds
        for row in zone_dwell_rows
    }

    # Current queue depth (latest BILLING_QUEUE_JOIN metadata)
    queue_row = conn.execute(text("""
        SELECT metadata
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
        ORDER BY timestamp DESC
        LIMIT 1
    """), {"store_id": store_id}).fetchone()

    queue_depth = 0
    if queue_row and queue_row[0]:
        import json
        try:
            meta = json.loads(queue_row[0])
            queue_depth = meta.get("queue_depth") or 0
        except Exception:
            pass

    # Abandonment rate
    abandoned = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'BILLING_QUEUE_ABANDON'
    """), {"store_id": store_id}).scalar() or 0

    abandonment_rate = round(abandoned / billing_visitors, 4) if billing_visitors > 0 else 0.0

    return {
        "store_id": store_id,
        "unique_visitors": unique_visitors,
        "conversion_rate": conversion_rate,
        "avg_dwell_per_zone_seconds": avg_dwell_per_zone,
        "current_queue_depth": queue_depth,
        "abandonment_rate": abandonment_rate,
        "billing_zone_visitors": billing_visitors,
    }


def get_funnel(store_id: str, conn) -> dict:
    # All unique customer sessions (by visitor_id, deduplicated — re-entries don't add)
    total_entries = conn.execute(text("""
    SELECT COUNT(DISTINCT visitor_id)
    FROM events
    WHERE store_id = :store_id
      AND is_staff = 0
      AND event_type = 'ENTRY'
      AND camera_id LIKE '%ENTRY%'
"""), {"store_id": store_id}).scalar() or 0

    # Visitors who entered at least one zone
    zone_visitors = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'ZONE_ENTER'
    """), {"store_id": store_id}).scalar() or 0

    # Visitors who reached billing queue
    billing_queue = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'BILLING_QUEUE_JOIN'
    """), {"store_id": store_id}).scalar() or 0

    # Visitors who completed purchase (reached billing but did NOT abandon)
    purchased = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND visitor_id NOT IN (
              SELECT visitor_id FROM events
              WHERE store_id = :store_id
                AND event_type = 'BILLING_QUEUE_ABANDON'
          )
    """), {"store_id": store_id}).scalar() or 0

    def drop_pct(a, b):
        if b == 0:
            return 0.0
        return round((b - a) / b * 100, 2)

    return {
        "store_id": store_id,
        "funnel": [
            {
                "stage": "Entry",
                "visitors": total_entries,
                "drop_off_pct": 0.0
            },
            {
                "stage": "Zone Visit",
                "visitors": zone_visitors,
                "drop_off_pct": drop_pct(zone_visitors, total_entries)
            },
            {
                "stage": "Billing Queue",
                "visitors": billing_queue,
                "drop_off_pct": drop_pct(billing_queue, zone_visitors)
            },
            {
                "stage": "Purchase",
                "visitors": purchased,
                "drop_off_pct": drop_pct(purchased, billing_queue)
            },
        ]
    }


def get_heatmap(store_id: str, conn) -> dict:
    rows = conn.execute(text("""
        SELECT zone_id,
               COUNT(DISTINCT visitor_id) as visit_count,
               AVG(dwell_ms)              as avg_dwell_ms
        FROM events
        WHERE store_id = :store_id
          AND is_staff = 0
          AND zone_id IS NOT NULL
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
        GROUP BY zone_id
    """), {"store_id": store_id}).fetchall()

    if not rows:
        return {"store_id": store_id, "zones": [], "data_confidence": "LOW"}

    max_visits = max(r[1] for r in rows) or 1
    total_sessions = conn.execute(text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id AND is_staff = 0 AND event_type = 'ENTRY'
    """), {"store_id": store_id}).scalar() or 0

    zones = []
    for row in rows:
        zone_id, visit_count, avg_dwell = row
        zones.append({
            "zone_id": zone_id,
            "visit_count": visit_count,
            "avg_dwell_seconds": round((avg_dwell or 0) / 1000, 2),
            "normalised_score": round(visit_count / max_visits * 100, 1),
        })

    return {
        "store_id": store_id,
        "zones": sorted(zones, key=lambda z: z["normalised_score"], reverse=True),
        "data_confidence": "LOW" if total_sessions < 20 else "HIGH",
    }