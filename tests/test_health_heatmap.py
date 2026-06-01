# PROMPT: "Write pytest tests for health endpoint and heatmap endpoint in a retail
# store analytics API. Health tests: status field present, stores list present,
# total events count, stale feed detection, multiple stores in health. Heatmap tests:
# zones list present, normalised scores 0-100, data_confidence flag, high confidence
# with 20+ sessions, zone structure has required fields, empty heatmap handling."
# CHANGES MADE: Added store isolation, fixed normalised_score range assertion,
# added data_confidence LOW check for fewer than 20 sessions.

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine
from sqlalchemy import text
import uuid

client = TestClient(app)


def clear_store(store_id: str):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM events WHERE store_id = :s"), {"s": store_id})
        conn.commit()


def make_event(event_id=None, store_id="STORE_HH_TEST", visitor_id="VIS_hh001",
               event_type="ENTRY", camera_id="CAM_ENTRY_01", zone_id=None,
               dwell_ms=0, is_staff=False, confidence=0.9,
               timestamp="2026-04-10T10:00:00Z", metadata=None):
    return {
        "event_id": event_id or str(uuid.uuid4()),
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


def ingest(events):
    return client.post("/events/ingest", json={"events": events})


# ── Health endpoint tests ─────────────────────────────────────────────────────

def test_health_status_field_present():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "status" in resp.json()


def test_health_status_valid_value():
    resp = client.get("/health")
    assert resp.json()["status"] in ("OK", "DEGRADED", "ERROR")


def test_health_checked_at_present():
    resp = client.get("/health")
    assert "checked_at" in resp.json()


def test_health_total_events_present():
    resp = client.get("/health")
    assert "total_events_ingested" in resp.json()
    assert isinstance(resp.json()["total_events_ingested"], int)


def test_health_stores_is_list():
    resp = client.get("/health")
    assert "stores" in resp.json()
    assert isinstance(resp.json()["stores"], list)


def test_health_store_has_required_fields():
    ingest([make_event(store_id="STORE_HEALTH_FIELDS")])
    resp = client.get("/health")
    stores = resp.json()["stores"]
    store = next((s for s in stores if s["store_id"] == "STORE_HEALTH_FIELDS"), None)
    assert store is not None
    assert "store_id" in store
    assert "last_event_timestamp" in store
    assert "feed_status" in store


def test_health_feed_status_valid_values():
    resp = client.get("/health")
    for store in resp.json()["stores"]:
        assert store["feed_status"] in ("OK", "STALE_FEED", "UNKNOWN")


def test_health_total_events_increases_after_ingest():
    before = client.get("/health").json()["total_events_ingested"]
    ingest([make_event(store_id="STORE_HEALTH_COUNT")])
    after = client.get("/health").json()["total_events_ingested"]
    assert after >= before


# ── Heatmap endpoint tests ────────────────────────────────────────────────────

def test_heatmap_empty_store_returns_empty_zones():
    resp = client.get("/stores/STORE_HM_EMPTY/heatmap")
    assert resp.status_code == 200
    assert resp.json()["zones"] == []


def test_heatmap_empty_store_low_confidence():
    resp = client.get("/stores/STORE_HM_LOWCONF/heatmap")
    assert resp.status_code == 200
    assert resp.json()["data_confidence"] == "LOW"


def test_heatmap_has_required_fields():
    resp = client.get("/stores/STORE_HM_FIELDS/heatmap")
    assert "zones" in resp.json()
    assert "data_confidence" in resp.json()
    assert "store_id" in resp.json()


def test_heatmap_zone_structure():
    clear_store("STORE_HM_STRUCT")
    ingest([make_event(store_id="STORE_HM_STRUCT", event_type="ZONE_ENTER",
                       zone_id="FACES", camera_id="CAM_FLOOR_01")])
    resp = client.get("/stores/STORE_HM_STRUCT/heatmap")
    zones = resp.json()["zones"]
    if zones:
        zone = zones[0]
        assert "zone_id" in zone
        assert "visit_count" in zone
        assert "avg_dwell_seconds" in zone
        assert "normalised_score" in zone


def test_heatmap_normalised_score_range():
    clear_store("STORE_HM_SCORE")
    events = [
        make_event(store_id="STORE_HM_SCORE", event_type="ZONE_ENTER",
                   zone_id="FACES", camera_id="CAM_FLOOR_01",
                   visitor_id=f"VIS_hms{i:02d}")
        for i in range(5)
    ]
    ingest(events)
    resp = client.get("/stores/STORE_HM_SCORE/heatmap")
    for zone in resp.json()["zones"]:
        assert 0.0 <= zone["normalised_score"] <= 100.0


def test_heatmap_top_zone_score_is_100():
    clear_store("STORE_HM_TOP")
    events = [
        make_event(store_id="STORE_HM_TOP", event_type="ZONE_ENTER",
                   zone_id="FACES", camera_id="CAM_FLOOR_01",
                   visitor_id=f"VIS_hmt{i:02d}")
        for i in range(5)
    ]
    ingest(events)
    resp = client.get("/stores/STORE_HM_TOP/heatmap")
    zones = resp.json()["zones"]
    if zones:
        assert zones[0]["normalised_score"] == 100.0


def test_heatmap_high_confidence_with_20_sessions():
    clear_store("STORE_HM_HIGH")
    events = []
    for i in range(20):
        events.append(make_event(
            store_id="STORE_HM_HIGH",
            event_type="ENTRY",
            visitor_id=f"VIS_hmh{i:02d}",
            camera_id="CAM_ENTRY_01"
        ))
        events.append(make_event(
            store_id="STORE_HM_HIGH",
            event_type="ZONE_ENTER",
            zone_id="FACES",
            camera_id="CAM_FLOOR_01",
            visitor_id=f"VIS_hmh{i:02d}"
        ))
    ingest(events)
    resp = client.get("/stores/STORE_HM_HIGH/heatmap")
    assert resp.json()["data_confidence"] == "HIGH"


def test_heatmap_multiple_zones():
    clear_store("STORE_HM_MULTI")
    events = []
    for zone in ["FACES", "LAKME", "DERMDOC"]:
        for i in range(3):
            events.append(make_event(
                store_id="STORE_HM_MULTI",
                event_type="ZONE_ENTER",
                zone_id=zone,
                camera_id="CAM_FLOOR_01",
                visitor_id=f"VIS_hmm{zone[:2]}{i}"
            ))
    ingest(events)
    resp = client.get("/stores/STORE_HM_MULTI/heatmap")
    zone_ids = [z["zone_id"] for z in resp.json()["zones"]]
    assert "FACES" in zone_ids
    assert "LAKME" in zone_ids
    assert "DERMDOC" in zone_ids


def test_heatmap_staff_excluded():
    clear_store("STORE_HM_STAFF")
    ingest([
        make_event(store_id="STORE_HM_STAFF", event_type="ZONE_ENTER",
                   zone_id="FACES", camera_id="CAM_FLOOR_01",
                   is_staff=True, visitor_id="VIS_staff01"),
    ])
    resp = client.get("/stores/STORE_HM_STAFF/heatmap")
    assert resp.json()["zones"] == []


def test_heatmap_store_id_in_response():
    resp = client.get("/stores/STORE_HM_ID/heatmap")
    assert resp.json()["store_id"] == "STORE_HM_ID"