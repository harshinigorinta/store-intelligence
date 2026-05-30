# PROMPT: "Write pytest tests for a FastAPI store analytics API. Cover these edge cases:
# empty store (no events), all-staff clip (is_staff=true for all), zero purchases,
# re-entry in funnel, idempotent ingest, partial batch failure, health endpoint,
# anomaly detection. Use httpx TestClient. Include happy path for all endpoints."
# CHANGES MADE: Added store_id scoping to isolate tests, fixed re-entry funnel
# assertion to check visitor not double-counted, added queue_depth metadata to
# BILLING_QUEUE_JOIN events for anomaly tests.

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db, engine
from sqlalchemy import text

client = TestClient(app)


def clear_store(store_id: str):
    """Helper to wipe events for a specific store between tests."""
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM events WHERE store_id = :s"), {"s": store_id})
        conn.commit()


# ── 1. Health endpoint ─────────────────────────────────────────────────────────
def test_health_returns_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("OK", "DEGRADED")
    assert "checked_at" in data
    assert "total_events_ingested" in data


# ── 2. Empty store — no events ────────────────────────────────────────────────
def test_metrics_empty_store():
    """API must not crash or return null for a store with zero events."""
    resp = client.get("/stores/STORE_EMPTY_TEST/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unique_visitors"] == 0
    assert data["conversion_rate"] == 0.0
    assert data["current_queue_depth"] == 0


def test_funnel_empty_store():
    resp = client.get("/stores/STORE_EMPTY_TEST/funnel")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["funnel"]) == 4
    for stage in data["funnel"]:
        assert stage["visitors"] == 0


def test_heatmap_empty_store():
    resp = client.get("/stores/STORE_EMPTY_TEST/heatmap")
    assert resp.status_code == 200
    data = resp.json()
    assert data["zones"] == []
    assert data["data_confidence"] == "LOW"


def test_anomalies_empty_store():
    resp = client.get("/stores/STORE_EMPTY_TEST/anomalies")
    assert resp.status_code == 200
    data = resp.json()
    assert "anomalies" in data


# ── 3. Ingest — happy path ─────────────────────────────────────────────────────
def test_ingest_valid_events():
    clear_store("STORE_BLR_001")
    payload = {"events": [
        {
            "event_id": "test-001",
            "store_id": "STORE_BLR_001",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_aaa001",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T10:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.95,
            "metadata": None
        }
    ]}
    resp = client.post("/events/ingest", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 1
    assert data["rejected"] == 0


# ── 4. Idempotency — same event_id twice must not double count ─────────────────
def test_ingest_idempotent():
    clear_store("STORE_BLR_002")
    event = {
        "event_id": "idem-test-001",
        "store_id": "STORE_BLR_002",
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": "VIS_idem01",
        "event_type": "ENTRY",
        "timestamp": "2026-03-03T10:00:00Z",
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": None
    }
    payload = {"events": [event]}
    client.post("/events/ingest", json=payload)
    client.post("/events/ingest", json=payload)  # second time — same event_id

    resp = client.get("/stores/STORE_BLR_002/metrics")
    assert resp.json()["unique_visitors"] == 1  # must not be 2


# ── 5. All-staff clip — customer metrics must be zero ─────────────────────────
def test_all_staff_excluded_from_metrics():
    clear_store("STORE_STAFF_TEST")
    payload = {"events": [
        {
            "event_id": f"staff-{i}",
            "store_id": "STORE_STAFF_TEST",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": f"VIS_staff{i:02d}",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T09:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": True,   # all staff
            "confidence": 0.99,
            "metadata": None
        }
        for i in range(5)
    ]}
    client.post("/events/ingest", json=payload)
    resp = client.get("/stores/STORE_STAFF_TEST/metrics")
    assert resp.status_code == 200
    assert resp.json()["unique_visitors"] == 0  # staff excluded


# ── 6. Zero purchases — conversion rate must be 0, not crash ──────────────────
def test_zero_purchases_conversion():
    clear_store("STORE_NOPURCHASE")
    payload = {"events": [
        {
            "event_id": "nop-001",
            "store_id": "STORE_NOPURCHASE",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_nop001",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T11:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.88,
            "metadata": None
        }
    ]}
    client.post("/events/ingest", json=payload)
    resp = client.get("/stores/STORE_NOPURCHASE/metrics")
    assert resp.status_code == 200
    assert resp.json()["conversion_rate"] == 0.0


# ── 7. Re-entry — must not double count in funnel ─────────────────────────────
def test_reentry_not_double_counted_in_funnel():
    clear_store("STORE_REENTRY")
    payload = {"events": [
        {
            "event_id": "re-entry-001",
            "store_id": "STORE_REENTRY",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_re001",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T12:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": None
        },
        {
            "event_id": "re-exit-001",
            "store_id": "STORE_REENTRY",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_re001",
            "event_type": "EXIT",
            "timestamp": "2026-03-03T12:20:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": None
        },
        {
            "event_id": "re-reentry-001",
            "store_id": "STORE_REENTRY",
            "camera_id": "CAM_ENTRY_01",
            "visitor_id": "VIS_re001",
            "event_type": "REENTRY",
            "timestamp": "2026-03-03T12:25:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": None
        }
    ]}
    client.post("/events/ingest", json=payload)
    resp = client.get("/stores/STORE_REENTRY/funnel")
    assert resp.status_code == 200
    entry_stage = resp.json()["funnel"][0]
    assert entry_stage["visitors"] == 1  # same person, must not be 2


# ── 8. Partial batch — malformed event rejected, valid one accepted ───────────
def test_partial_batch_failure():
    payload = {"events": [
        {
            "event_id": "partial-good-001",
            "store_id": "STORE_PARTIAL",
            "camera_id": "CAM_01",
            "visitor_id": "VIS_p001",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T13:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.85,
            "metadata": None
        },
        {
            "event_id": "partial-bad-001",
            "store_id": "STORE_PARTIAL",
            "camera_id": "CAM_01",
            "visitor_id": "VIS_p002",
            "event_type": "INVALID_TYPE",   # bad event type
            "timestamp": "2026-03-03T13:01:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.85,
            "metadata": None
        }
    ]}
    resp = client.post("/events/ingest", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] == 1
    assert data["rejected"] == 1
    assert len(data["errors"]) == 1


# ── 9. Batch size limit ────────────────────────────────────────────────────────
def test_batch_size_limit():
    events = [
        {
            "event_id": f"bulk-{i:04d}",
            "store_id": "STORE_BULK",
            "camera_id": "CAM_01",
            "visitor_id": f"VIS_bulk{i:04d}",
            "event_type": "ENTRY",
            "timestamp": "2026-03-03T14:00:00Z",
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.9,
            "metadata": None
        }
        for i in range(501)  # one over limit
    ]
    resp = client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 400


# ── 10. Anomaly — queue spike detected ────────────────────────────────────────
def test_anomaly_queue_spike():
    clear_store("STORE_QUEUE_TEST")
    payload = {"events": [
        {
            "event_id": "q-entry-001",
            "store_id": "STORE_QUEUE_TEST",
            "camera_id": "CAM_BILLING_01",
            "visitor_id": "VIS_q001",
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": "2026-03-03T15:00:00Z",
            "zone_id": "BILLING",
            "dwell_ms": 0,
            "is_staff": False,
            "confidence": 0.92,
            "metadata": {"queue_depth": 9, "sku_zone": None, "session_seq": 1}
        }
    ]}
    client.post("/events/ingest", json=payload)
    resp = client.get("/stores/STORE_QUEUE_TEST/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "BILLING_QUEUE_SPIKE" in types