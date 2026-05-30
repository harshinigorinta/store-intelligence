# PROMPT: "Write additional pytest tests to cover missing lines in metrics.py (heatmap with data,
# zone dwell), anomalies.py (conversion drop, stale feed), and health.py (store with stale feed).
# Use the same TestClient pattern as existing tests. Include zone dwell events, heatmap with 20+
# sessions, and conversion drop scenario."
# CHANGES MADE: Added store_id isolation per test, fixed zone dwell event structure to match
# schema, added CAM_ENTRY camera_id for funnel tests, adjusted conversion drop threshold to
# match our 20% drop detection logic.

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine
from sqlalchemy import text

client = TestClient(app)


def clear_store(store_id: str):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM events WHERE store_id = :s"), {"s": store_id})
        conn.commit()


def make_event(event_id, store_id, visitor_id, event_type, camera_id="CAM_ENTRY_01",
               zone_id=None, dwell_ms=0, is_staff=False, confidence=0.9,
               timestamp="2026-03-03T10:00:00Z", metadata=None):
    return {
        "event_id": event_id,
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": metadata or {"queue_depth": None, "sku_zone": None, "session_seq": 1}
    }


# ── 1. Heatmap with actual data ───────────────────────────────────────────────
def test_heatmap_with_data():
    clear_store("STORE_HEATMAP")
    events = []
    # Add 25 visitors with zone dwell events
    for i in range(25):
        events.append(make_event(
            f"hm-entry-{i}", "STORE_HEATMAP", f"VIS_hm{i:03d}",
            "ENTRY", camera_id="CAM_ENTRY_01"
        ))
        events.append(make_event(
            f"hm-zone-{i}", "STORE_HEATMAP", f"VIS_hm{i:03d}",
            "ZONE_ENTER", camera_id="CAM_FLOOR_01",
            zone_id="SKINCARE"
        ))
        events.append(make_event(
            f"hm-dwell-{i}", "STORE_HEATMAP", f"VIS_hm{i:03d}",
            "ZONE_DWELL", camera_id="CAM_FLOOR_01",
            zone_id="SKINCARE", dwell_ms=45000
        ))

    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_HEATMAP/heatmap")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["zones"]) > 0
    assert data["data_confidence"] == "HIGH"  # >20 sessions
    zone = data["zones"][0]
    assert zone["zone_id"] == "SKINCARE"
    assert zone["normalised_score"] == 100.0
    assert zone["avg_dwell_seconds"] > 0


# ── 2. Heatmap low confidence (fewer than 20 sessions) ────────────────────────
def test_heatmap_low_confidence():
    clear_store("STORE_HEATMAP_LOW")
    events = []
    for i in range(5):
        events.append(make_event(
            f"hml-zone-{i}", "STORE_HEATMAP_LOW", f"VIS_hml{i}",
            "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="MAKEUP"
        ))
    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_HEATMAP_LOW/heatmap")
    assert resp.status_code == 200
    assert resp.json()["data_confidence"] == "LOW"


# ── 3. Metrics with zone dwell data ───────────────────────────────────────────
def test_metrics_with_zone_dwell():
    clear_store("STORE_DWELL")
    events = [
        make_event("dw-entry-1", "STORE_DWELL", "VIS_dw001", "ENTRY"),
        make_event("dw-zone-1", "STORE_DWELL", "VIS_dw001", "ZONE_DWELL",
                   camera_id="CAM_FLOOR_01", zone_id="SKINCARE", dwell_ms=60000),
        make_event("dw-zone-2", "STORE_DWELL", "VIS_dw001", "ZONE_DWELL",
                   camera_id="CAM_FLOOR_01", zone_id="MAKEUP", dwell_ms=30000),
    ]
    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_DWELL/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "SKINCARE" in data["avg_dwell_per_zone_seconds"]
    assert data["avg_dwell_per_zone_seconds"]["SKINCARE"] == 60.0


# ── 4. Funnel with entry camera events ────────────────────────────────────────
def test_funnel_with_entry_camera():
    clear_store("STORE_FUNNEL2")
    events = [
        make_event("fn2-entry-1", "STORE_FUNNEL2", "VIS_fn001",
                   "ENTRY", camera_id="CAM_ENTRY_01"),
        make_event("fn2-zone-1", "STORE_FUNNEL2", "VIS_fn001",
                   "ZONE_ENTER", camera_id="CAM_FLOOR_01", zone_id="SKINCARE"),
        make_event("fn2-billing-1", "STORE_FUNNEL2", "VIS_fn001",
                   "BILLING_QUEUE_JOIN", camera_id="CAM_BILLING_01",
                   zone_id="BILLING",
                   metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3}),
    ]
    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_FUNNEL2/funnel")
    assert resp.status_code == 200
    funnel = resp.json()["funnel"]
    assert funnel[0]["visitors"] == 1   # Entry
    assert funnel[1]["visitors"] == 1   # Zone Visit
    assert funnel[2]["visitors"] == 1   # Billing Queue


# ── 5. Anomaly — conversion drop ──────────────────────────────────────────────
def test_anomaly_conversion_drop():
    clear_store("STORE_CONV_DROP")
    # Historical week: high conversion (8 buyers out of 10)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    events = []
    for i in range(10):
        day_offset = i % 7 + 1
        ts = (now - timedelta(days=day_offset)).strftime("%Y-%m-%dT10:00:00Z")
        events.append(make_event(
            f"cd-hist-entry-{i}", "STORE_CONV_DROP", f"VIS_hist{i:02d}",
            "ENTRY", timestamp=ts
        ))
        if i < 8:  # 8 out of 10 bought historically
            events.append(make_event(
                f"cd-hist-bill-{i}", "STORE_CONV_DROP", f"VIS_hist{i:02d}",
                "BILLING_QUEUE_JOIN", timestamp=ts,
                zone_id="BILLING",
                metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2}
            ))

    # Today: low conversion (1 buyer out of 10)
    today = now.strftime("%Y-%m-%dT10:00:00Z")
    for i in range(10):
        events.append(make_event(
            f"cd-today-entry-{i}", "STORE_CONV_DROP", f"VIS_today{i:02d}",
            "ENTRY", timestamp=today
        ))
    # Only 1 buyer today
    events.append(make_event(
        "cd-today-bill-1", "STORE_CONV_DROP", "VIS_today00",
        "BILLING_QUEUE_JOIN", timestamp=today,
        zone_id="BILLING",
        metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2}
    ))

    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_CONV_DROP/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "CONVERSION_DROP" in types


# ── 6. Health with multiple stores ────────────────────────────────────────────
def test_health_multiple_stores():
    # Ingest events for a second store
    events = [make_event(
        "hs-001", "STORE_SECOND", "VIS_hs001", "ENTRY"
    )]
    client.post("/events/ingest", json={"events": events})
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    store_ids = [s["store_id"] for s in data["stores"]]
    assert "STORE_SECOND" in store_ids


# ── 7. Abandonment rate calculation ───────────────────────────────────────────
def test_abandonment_rate():
    clear_store("STORE_ABANDON")
    events = [
        make_event("ab-entry-1", "STORE_ABANDON", "VIS_ab001", "ENTRY"),
        make_event("ab-bill-1", "STORE_ABANDON", "VIS_ab001",
                   "BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 2}),
        make_event("ab-abandon-1", "STORE_ABANDON", "VIS_ab001",
                   "BILLING_QUEUE_ABANDON", zone_id="BILLING",
                   metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 3}),
    ]
    client.post("/events/ingest", json={"events": events})
    resp = client.get("/stores/STORE_ABANDON/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["abandonment_rate"] > 0


# ── 8. Ingest with missing required field ─────────────────────────────────────
def test_ingest_missing_required_field():
    payload = {"events": [{
        "event_id": "missing-field-001",
        "store_id": "STORE_TEST",
        # missing camera_id, visitor_id, event_type etc
        "timestamp": "2026-03-03T10:00:00Z",
        "confidence": 0.9
    }]}
    resp = client.post("/events/ingest", json=payload)
    # Should return 422 validation error from Pydantic
    assert resp.status_code == 422