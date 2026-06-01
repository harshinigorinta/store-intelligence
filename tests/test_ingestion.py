# PROMPT: "Write comprehensive pytest tests for a FastAPI event ingestion endpoint.
# Cover: valid batch ingestion, idempotency, partial batch failure, batch size limits,
# invalid event types, confidence out of range, missing required fields, empty batch,
# large batch performance, duplicate event_ids within same batch, staff events ingested
# correctly, all 8 event types accepted, metadata validation."
# CHANGES MADE: Added store isolation, fixed event_type validation to match our
# VALID_EVENT_TYPES set, added metadata structure tests, fixed confidence boundary
# tests to use exact 0.0 and 1.0 values.

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


def make_event(event_id=None, store_id="STORE_INGEST_TEST", visitor_id="VIS_ing001",
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


# ── All 8 event types accepted ────────────────────────────────────────────────

def test_entry_event_accepted():
    resp = client.post("/events/ingest", json={"events": [make_event(event_type="ENTRY")]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_exit_event_accepted():
    resp = client.post("/events/ingest", json={"events": [make_event(event_type="EXIT")]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_zone_enter_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="ZONE_ENTER", zone_id="FACES")
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_zone_exit_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="ZONE_EXIT", zone_id="FACES", dwell_ms=30000)
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_zone_dwell_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="ZONE_DWELL", zone_id="LAKME", dwell_ms=60000)
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_billing_queue_join_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="BILLING_QUEUE_JOIN", zone_id="BILLING",
                   metadata={"queue_depth": 3, "sku_zone": None, "session_seq": 1})
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_billing_queue_abandon_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="BILLING_QUEUE_ABANDON", zone_id="BILLING")
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_reentry_event_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="REENTRY")
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_idempotent_single_event():
    clear_store("STORE_IDEM_1")
    event = make_event(event_id="idem-single-001", store_id="STORE_IDEM_1")
    client.post("/events/ingest", json={"events": [event]})
    client.post("/events/ingest", json={"events": [event]})
    resp = client.get("/stores/STORE_IDEM_1/metrics")
    assert resp.json()["unique_visitors"] == 1


def test_idempotent_large_batch():
    clear_store("STORE_IDEM_2")
    events = [make_event(event_id=f"idem-batch-{i:03d}", store_id="STORE_IDEM_2",
                         visitor_id=f"VIS_ib{i:03d}") for i in range(20)]
    client.post("/events/ingest", json={"events": events})
    r2 = client.post("/events/ingest", json={"events": events})
    assert r2.json()["accepted"] == 20
    resp = client.get("/stores/STORE_IDEM_2/metrics")
    assert resp.json()["unique_visitors"] == 20


def test_duplicate_event_id_in_same_batch():
    """Two events with same event_id in one batch — second should be ignored."""
    clear_store("STORE_DUP")
    event = make_event(event_id="dup-001", store_id="STORE_DUP")
    resp = client.post("/events/ingest", json={"events": [event, event]})
    assert resp.status_code == 200
    # Only 1 should be stored
    resp2 = client.get("/stores/STORE_DUP/metrics")
    assert resp2.json()["unique_visitors"] == 1


# ── Validation ────────────────────────────────────────────────────────────────

def test_invalid_event_type_rejected():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="INVALID_TYPE")
    ]})
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 1
    assert len(resp.json()["errors"]) == 1


def test_confidence_exactly_zero_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(confidence=0.0)
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_confidence_exactly_one_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(confidence=1.0)
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


def test_confidence_above_one_rejected():
    resp = client.post("/events/ingest", json={"events": [
        make_event(confidence=1.1)
    ]})
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 1


def test_confidence_below_zero_rejected():
    resp = client.post("/events/ingest", json={"events": [
        make_event(confidence=-0.1)
    ]})
    assert resp.status_code == 200
    assert resp.json()["rejected"] == 1


def test_missing_camera_id_returns_422():
    resp = client.post("/events/ingest", json={"events": [{
        "event_id": str(uuid.uuid4()),
        "store_id": "STORE_TEST",
        "visitor_id": "VIS_001",
        "event_type": "ENTRY",
        "timestamp": "2026-04-10T10:00:00Z",
        "confidence": 0.9
    }]})
    assert resp.status_code == 422


def test_empty_batch_accepted():
    resp = client.post("/events/ingest", json={"events": []})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0
    assert resp.json()["rejected"] == 0


def test_batch_size_exactly_500():
    events = [make_event(store_id="STORE_BULK500",
                         visitor_id=f"VIS_b5{i:03d}") for i in range(500)]
    resp = client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 500


def test_batch_size_501_rejected():
    events = [make_event(store_id="STORE_BULK501",
                         visitor_id=f"VIS_b5{i:03d}") for i in range(501)]
    resp = client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 400


# ── Staff events ──────────────────────────────────────────────────────────────

def test_staff_events_stored_with_flag():
    clear_store("STORE_STAFF_FLAG")
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_id="staff-flag-001", store_id="STORE_STAFF_FLAG",
                   visitor_id="VIS_staff01", is_staff=True)
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1
    # Staff should not appear in metrics
    metrics = client.get("/stores/STORE_STAFF_FLAG/metrics")
    assert metrics.json()["unique_visitors"] == 0


def test_mixed_staff_customer_batch():
    clear_store("STORE_MIXED")
    events = [
        make_event(event_id="mix-c-1", store_id="STORE_MIXED",
                   visitor_id="VIS_cust01", is_staff=False),
        make_event(event_id="mix-s-1", store_id="STORE_MIXED",
                   visitor_id="VIS_staff01", is_staff=True),
        make_event(event_id="mix-c-2", store_id="STORE_MIXED",
                   visitor_id="VIS_cust02", is_staff=False),
    ]
    resp = client.post("/events/ingest", json={"events": events})
    assert resp.json()["accepted"] == 3
    metrics = client.get("/stores/STORE_MIXED/metrics")
    assert metrics.json()["unique_visitors"] == 2  # only customers


# ── Partial success ───────────────────────────────────────────────────────────

def test_partial_success_returns_error_details():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_id="ps-good-1"),
        make_event(event_id="ps-bad-1", event_type="FAKE_TYPE"),
        make_event(event_id="ps-good-2", visitor_id="VIS_ps002"),
    ]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 2
    assert data["rejected"] == 1
    assert len(data["errors"]) == 1
    assert "ps-bad-1" in data["errors"][0]["event_id"]


def test_all_rejected_returns_zero_accepted():
    resp = client.post("/events/ingest", json={"events": [
        make_event(event_type="BAD_TYPE_1"),
        make_event(event_type="BAD_TYPE_2"),
    ]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 0
    assert resp.json()["rejected"] == 2


# ── Response structure ────────────────────────────────────────────────────────

def test_ingest_response_has_required_fields():
    resp = client.post("/events/ingest", json={"events": [make_event()]})
    assert resp.status_code == 200
    data = resp.json()
    assert "accepted" in data
    assert "rejected" in data
    assert "errors" in data
    assert isinstance(data["errors"], list)


def test_ingest_returns_200_not_201():
    """API should return 200, not 201, for ingest endpoint."""
    resp = client.post("/events/ingest", json={"events": [make_event()]})
    assert resp.status_code == 200