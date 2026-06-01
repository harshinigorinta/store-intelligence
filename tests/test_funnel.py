# PROMPT: "Write comprehensive pytest tests for a conversion funnel endpoint in a
# retail store analytics API. Test: 4 stages present always, session deduplication,
# re-entry not double counted, staff excluded from funnel, drop-off percentages
# calculated correctly, funnel with only entry events, funnel with complete journey,
# zero visitors funnel, multiple visitors different stages, billing visitors counted."
# CHANGES MADE: Added store isolation per test, fixed entry camera_id filter to
# use CAM_ENTRY_01, corrected drop-off percentage assertions for edge cases,
# added explicit session_seq in metadata.

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


def make_event(event_id=None, store_id="STORE_FUNNEL_TEST", visitor_id="VIS_f001",
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


# ── Funnel structure ──────────────────────────────────────────────────────────

def test_funnel_always_has_4_stages():
    resp = client.get("/stores/STORE_FUNNEL_EMPTY/funnel")
    assert resp.status_code == 200
    assert len(resp.json()["funnel"]) == 4


def test_funnel_stage_names():
    resp = client.get("/stores/STORE_FUNNEL_NAMES/funnel")
    stages = [s["stage"] for s in resp.json()["funnel"]]
    assert stages == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]


def test_funnel_has_drop_off_pct_field():
    resp = client.get("/stores/STORE_FUNNEL_FIELDS/funnel")
    for stage in resp.json()["funnel"]:
        assert "drop_off_pct" in stage
        assert "visitors" in stage
        assert "stage" in stage


def test_funnel_entry_drop_off_is_zero():
    resp = client.get("/stores/STORE_FUNNEL_DROP/funnel")
    entry_stage = resp.json()["funnel"][0]
    assert entry_stage["drop_off_pct"] == 0.0


# ── Zero visitors ─────────────────────────────────────────────────────────────

def test_funnel_zero_visitors_all_stages_zero():
    resp = client.get("/stores/STORE_FUNNEL_ZERO/funnel")
    for stage in resp.json()["funnel"]:
        assert stage["visitors"] == 0


def test_funnel_zero_visitors_no_crash():
    resp = client.get("/stores/STORE_FUNNEL_NOCRASH/funnel")
    assert resp.status_code == 200


# ── Single visitor complete journey ──────────────────────────────────────────

def test_funnel_complete_journey():
    clear_store("STORE_FUNNEL_FULL")
    events = [
        make_event("fj-1", "STORE_FUNNEL_FULL", "VIS_fj01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fj-2", "STORE_FUNNEL_FULL", "VIS_fj01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="FACES",
                   metadata={"queue_depth": None, "sku_zone": "MAKEUP", "session_seq": 2}),
        make_event("fj-3", "STORE_FUNNEL_FULL", "VIS_fj01", "BILLING_QUEUE_JOIN",
                   camera_id="CAM_BILLING_01", zone_id="BILLING",
                   metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3}),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_FULL/funnel")
    funnel = resp.json()["funnel"]
    assert funnel[0]["visitors"] == 1  # Entry
    assert funnel[1]["visitors"] == 1  # Zone Visit
    assert funnel[2]["visitors"] == 1  # Billing Queue
    assert funnel[3]["visitors"] == 1  # Purchase (no abandon)


# ── Session deduplication ─────────────────────────────────────────────────────

def test_funnel_same_visitor_multiple_entries_counted_once():
    clear_store("STORE_FUNNEL_DEDUP")
    events = [
        make_event("fd-1", "STORE_FUNNEL_DEDUP", "VIS_fd01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fd-2", "STORE_FUNNEL_DEDUP", "VIS_fd01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="FACES"),
        make_event("fd-3", "STORE_FUNNEL_DEDUP", "VIS_fd01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="LAKME"),
        make_event("fd-4", "STORE_FUNNEL_DEDUP", "VIS_fd01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="DERMDOC"),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_DEDUP/funnel")
    funnel = resp.json()["funnel"]
    assert funnel[0]["visitors"] == 1  # Entry — not 3
    assert funnel[1]["visitors"] == 1  # Zone Visit — not 3


def test_funnel_reentry_not_double_counted():
    clear_store("STORE_FUNNEL_REENTRY")
    events = [
        make_event("fr-1", "STORE_FUNNEL_REENTRY", "VIS_fr01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fr-2", "STORE_FUNNEL_REENTRY", "VIS_fr01", "EXIT",
                   camera_id="CAM_ENTRY_01"),
        make_event("fr-3", "STORE_FUNNEL_REENTRY", "VIS_fr01", "REENTRY",
                   camera_id="CAM_ENTRY_01"),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_REENTRY/funnel")
    assert resp.json()["funnel"][0]["visitors"] == 1


# ── Staff excluded ────────────────────────────────────────────────────────────

def test_funnel_excludes_staff():
    clear_store("STORE_FUNNEL_STAFF")
    events = [
        make_event("fs-1", "STORE_FUNNEL_STAFF", "VIS_fs01", "ENTRY",
                   camera_id="CAM_ENTRY_01", is_staff=True),
        make_event("fs-2", "STORE_FUNNEL_STAFF", "VIS_fs01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="FACES", is_staff=True),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_STAFF/funnel")
    funnel = resp.json()["funnel"]
    assert funnel[0]["visitors"] == 0
    assert funnel[1]["visitors"] == 0


# ── Multiple visitors ─────────────────────────────────────────────────────────

def test_funnel_multiple_visitors_different_stages():
    clear_store("STORE_FUNNEL_MULTI")
    events = [
        # Visitor 1: entry only
        make_event("fm-1", "STORE_FUNNEL_MULTI", "VIS_fm01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        # Visitor 2: entry + zone
        make_event("fm-2", "STORE_FUNNEL_MULTI", "VIS_fm02", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fm-3", "STORE_FUNNEL_MULTI", "VIS_fm02", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="FACES"),
        # Visitor 3: full journey
        make_event("fm-4", "STORE_FUNNEL_MULTI", "VIS_fm03", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fm-5", "STORE_FUNNEL_MULTI", "VIS_fm03", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="LAKME"),
        make_event("fm-6", "STORE_FUNNEL_MULTI", "VIS_fm03", "BILLING_QUEUE_JOIN",
                   camera_id="CAM_BILLING_01", zone_id="BILLING",
                   metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 3}),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_MULTI/funnel")
    funnel = resp.json()["funnel"]
    assert funnel[0]["visitors"] == 3  # all entered
    assert funnel[1]["visitors"] == 2  # 2 visited zones
    assert funnel[2]["visitors"] == 1  # 1 reached billing


def test_funnel_drop_off_calculated():
    clear_store("STORE_FUNNEL_DROPOFF")
    events = [
        make_event("do-1", "STORE_FUNNEL_DROPOFF", "VIS_do01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("do-2", "STORE_FUNNEL_DROPOFF", "VIS_do02", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("do-3", "STORE_FUNNEL_DROPOFF", "VIS_do01", "ZONE_ENTER",
                   camera_id="CAM_FLOOR_01", zone_id="FACES"),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_DROPOFF/funnel")
    funnel = resp.json()["funnel"]
    # 1 out of 2 visitors went to zone = 50% drop-off
    assert funnel[1]["drop_off_pct"] == 50.0


# ── Abandonment in funnel ─────────────────────────────────────────────────────

def test_funnel_abandoned_visitor_not_in_purchase():
    clear_store("STORE_FUNNEL_ABANDON")
    events = [
        make_event("fa-1", "STORE_FUNNEL_ABANDON", "VIS_fa01", "ENTRY",
                   camera_id="CAM_ENTRY_01"),
        make_event("fa-2", "STORE_FUNNEL_ABANDON", "VIS_fa01", "BILLING_QUEUE_JOIN",
                   zone_id="BILLING",
                   metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 2}),
        make_event("fa-3", "STORE_FUNNEL_ABANDON", "VIS_fa01", "BILLING_QUEUE_ABANDON",
                   zone_id="BILLING",
                   metadata={"queue_depth": 2, "sku_zone": None, "session_seq": 3}),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_FUNNEL_ABANDON/funnel")
    funnel = resp.json()["funnel"]
    assert funnel[2]["visitors"] == 1  # reached billing
    assert funnel[3]["visitors"] == 0  # abandoned — not purchased


def test_funnel_store_id_in_response():
    resp = client.get("/stores/STORE_FUNNEL_ID/funnel")
    assert resp.json()["store_id"] == "STORE_FUNNEL_ID"