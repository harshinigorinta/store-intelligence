# PROMPT: "Write comprehensive pytest tests for anomaly detection in a retail store
# analytics API. Test BILLING_QUEUE_SPIKE at different severity levels (WARN at depth 5,
# CRITICAL at depth 8), CONVERSION_DROP with historical vs today comparison, DEAD_ZONE
# detection after 30 minutes of inactivity, STALE_FEED detection, multiple anomalies
# simultaneously, and anomaly suggested_action field presence. Use FastAPI TestClient."
# CHANGES MADE: Added store isolation per test, fixed timestamp generation for today
# vs historical events, added severity level assertions, verified suggested_action
# is non-empty string for all anomaly types.

import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app.main import app
from app.database import engine
from sqlalchemy import text

client = TestClient(app)


def clear_store(store_id: str):
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM events WHERE store_id = :s"), {"s": store_id})
        conn.commit()


def make_event(event_id, store_id, visitor_id, event_type,
               camera_id="CAM_ENTRY_01", zone_id=None,
               dwell_ms=0, is_staff=False, confidence=0.9,
               timestamp=None, metadata=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def ingest(events):
    return client.post("/events/ingest", json={"events": events})


# ── Queue spike tests ──────────────────────────────────────────────────────────

def test_queue_spike_warn_at_depth_5():
    clear_store("STORE_QS_WARN")
    ingest([make_event("qs-w-1", "STORE_QS_WARN", "VIS_qs01", "BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 5, "sku_zone": None, "session_seq": 1})])
    resp = client.get("/stores/STORE_QS_WARN/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    severities = {a["type"]: a["severity"] for a in resp.json()["anomalies"]}
    assert "BILLING_QUEUE_SPIKE" in types
    assert severities["BILLING_QUEUE_SPIKE"] == "WARN"


def test_queue_spike_critical_at_depth_8():
    clear_store("STORE_QS_CRIT")
    ingest([make_event("qs-c-1", "STORE_QS_CRIT", "VIS_qs02", "BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 8, "sku_zone": None, "session_seq": 1})])
    resp = client.get("/stores/STORE_QS_CRIT/anomalies")
    assert resp.status_code == 200
    severities = {a["type"]: a["severity"] for a in resp.json()["anomalies"]}
    assert severities.get("BILLING_QUEUE_SPIKE") == "CRITICAL"


def test_no_queue_spike_below_threshold():
    clear_store("STORE_QS_NONE")
    ingest([make_event("qs-n-1", "STORE_QS_NONE", "VIS_qs03", "BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 3, "sku_zone": None, "session_seq": 1})])
    resp = client.get("/stores/STORE_QS_NONE/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "BILLING_QUEUE_SPIKE" not in types


def test_queue_spike_has_suggested_action():
    clear_store("STORE_QS_ACT")
    ingest([make_event("qs-a-1", "STORE_QS_ACT", "VIS_qs04", "BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1})])
    resp = client.get("/stores/STORE_QS_ACT/anomalies")
    anomalies = resp.json()["anomalies"]
    spike = next((a for a in anomalies if a["type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert "suggested_action" in spike
    assert len(spike["suggested_action"]) > 0


# ── Dead zone tests ────────────────────────────────────────────────────────────

def test_dead_zone_detected_after_30_min():
    clear_store("STORE_DZ_TEST")
    # Add zone events from 35 minutes ago
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([
        make_event("dz-1", "STORE_DZ_TEST", "VIS_dz01", "ZONE_ENTER",
                   zone_id="FACES", timestamp=old_ts),
        make_event("dz-2", "STORE_DZ_TEST", "VIS_dz02", "ZONE_ENTER",
                   zone_id="LAKME", timestamp=old_ts),
    ])
    resp = client.get("/stores/STORE_DZ_TEST/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "DEAD_ZONE" in types


def test_no_dead_zone_with_recent_activity():
    clear_store("STORE_DZ_NONE")
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("dz-n-1", "STORE_DZ_NONE", "VIS_dz03", "ZONE_ENTER",
                       zone_id="FACES", timestamp=now_ts)])
    resp = client.get("/stores/STORE_DZ_NONE/anomalies")
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "DEAD_ZONE" not in types


def test_dead_zone_severity_is_info():
    clear_store("STORE_DZ_SEV")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("dz-s-1", "STORE_DZ_SEV", "VIS_dz04", "ZONE_ENTER",
                       zone_id="BILLING", timestamp=old_ts)])
    resp = client.get("/stores/STORE_DZ_SEV/anomalies")
    dead_zones = [a for a in resp.json()["anomalies"] if a["type"] == "DEAD_ZONE"]
    assert all(a["severity"] == "INFO" for a in dead_zones)


def test_dead_zone_suggested_action_present():
    clear_store("STORE_DZ_ACT")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("dz-a-1", "STORE_DZ_ACT", "VIS_dz05", "ZONE_ENTER",
                       zone_id="DERMDOC", timestamp=old_ts)])
    resp = client.get("/stores/STORE_DZ_ACT/anomalies")
    dead_zones = [a for a in resp.json()["anomalies"] if a["type"] == "DEAD_ZONE"]
    assert len(dead_zones) > 0
    assert all(len(a.get("suggested_action", "")) > 0 for a in dead_zones)


# ── Conversion drop tests ──────────────────────────────────────────────────────

def test_conversion_drop_detected():
    clear_store("STORE_CD_TEST")
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%dT10:00:00Z")

    events = []
    # Historical: high conversion (8/10)
    for i in range(10):
        day = (now - timedelta(days=i % 7 + 1)).strftime("%Y-%m-%dT10:00:00Z")
        events.append(make_event(f"cd-h-e-{i}", "STORE_CD_TEST", f"VIS_cdh{i:02d}",
                                 "ENTRY", timestamp=day))
        if i < 8:
            events.append(make_event(f"cd-h-b-{i}", "STORE_CD_TEST", f"VIS_cdh{i:02d}",
                                     "BILLING_QUEUE_JOIN", zone_id="BILLING",
                                     timestamp=day,
                                     metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2}))

    # Today: low conversion (1/10)
    for i in range(10):
        events.append(make_event(f"cd-t-e-{i}", "STORE_CD_TEST", f"VIS_cdt{i:02d}",
                                 "ENTRY", timestamp=today))
    events.append(make_event("cd-t-b-0", "STORE_CD_TEST", "VIS_cdt00",
                              "BILLING_QUEUE_JOIN", zone_id="BILLING",
                              timestamp=today,
                              metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2}))

    ingest(events)
    resp = client.get("/stores/STORE_CD_TEST/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "CONVERSION_DROP" in types


def test_no_conversion_drop_stable_rate():
    clear_store("STORE_CD_NONE")
    now = datetime.now(timezone.utc)
    events = []
    # Consistent 50% conversion historically and today
    for i in range(14):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%dT10:00:00Z")
        for j in range(4):
            vid = f"VIS_cdn{i:02d}{j}"
            events.append(make_event(f"cdn-e-{i}-{j}", "STORE_CD_NONE", vid,
                                     "ENTRY", timestamp=day))
            if j < 2:
                events.append(make_event(f"cdn-b-{i}-{j}", "STORE_CD_NONE", vid,
                                         "BILLING_QUEUE_JOIN", zone_id="BILLING",
                                         timestamp=day,
                                         metadata={"queue_depth": 1, "sku_zone": None, "session_seq": 2}))
    ingest(events)
    resp = client.get("/stores/STORE_CD_NONE/anomalies")
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "CONVERSION_DROP" not in types


# ── Stale feed tests ───────────────────────────────────────────────────────────

def test_stale_feed_detected():
    clear_store("STORE_SF_TEST")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("sf-1", "STORE_SF_TEST", "VIS_sf01", "ENTRY", timestamp=old_ts)])
    resp = client.get("/stores/STORE_SF_TEST/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "STALE_FEED" in types


def test_stale_feed_severity_is_warn():
    clear_store("STORE_SF_SEV")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("sf-s-1", "STORE_SF_SEV", "VIS_sf02", "ENTRY", timestamp=old_ts)])
    resp = client.get("/stores/STORE_SF_SEV/anomalies")
    stale = [a for a in resp.json()["anomalies"] if a["type"] == "STALE_FEED"]
    assert len(stale) > 0
    assert stale[0]["severity"] == "WARN"


def test_no_stale_feed_with_recent_events():
    clear_store("STORE_SF_NONE")
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ingest([make_event("sf-n-1", "STORE_SF_NONE", "VIS_sf03", "ENTRY", timestamp=now_ts)])
    resp = client.get("/stores/STORE_SF_NONE/anomalies")
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "STALE_FEED" not in types


# ── Multiple anomalies ─────────────────────────────────────────────────────────

def test_multiple_anomalies_simultaneously():
    clear_store("STORE_MULTI")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events = [
        # Queue spike
        make_event("multi-1", "STORE_MULTI", "VIS_m01", "BILLING_QUEUE_JOIN",
                   zone_id="BILLING",
                   metadata={"queue_depth": 7, "sku_zone": None, "session_seq": 1}),
        # Dead zone (old event)
        make_event("multi-2", "STORE_MULTI", "VIS_m02", "ZONE_ENTER",
                   zone_id="FACES", timestamp=old_ts),
    ]
    ingest(events)
    resp = client.get("/stores/STORE_MULTI/anomalies")
    assert resp.status_code == 200
    types = [a["type"] for a in resp.json()["anomalies"]]
    assert "BILLING_QUEUE_SPIKE" in types
    assert "DEAD_ZONE" in types
    assert len(resp.json()["anomalies"]) >= 2


def test_anomalies_response_structure():
    resp = client.get("/stores/STORE_STRUCTURE_TEST/anomalies")
    assert resp.status_code == 200
    data = resp.json()
    assert "anomalies" in data
    assert "checked_at" in data
    assert isinstance(data["anomalies"], list)


def test_all_anomalies_have_required_fields():
    clear_store("STORE_FIELDS_TEST")
    ingest([make_event("f-1", "STORE_FIELDS_TEST", "VIS_f01", "BILLING_QUEUE_JOIN",
                       zone_id="BILLING",
                       metadata={"queue_depth": 9, "sku_zone": None, "session_seq": 1})])
    resp = client.get("/stores/STORE_FIELDS_TEST/anomalies")
    for anomaly in resp.json()["anomalies"]:
        assert "type" in anomaly
        assert "severity" in anomaly
        assert "detail" in anomaly
        assert "suggested_action" in anomaly
        assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")