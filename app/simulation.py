"""
app/simulation.py
Simulation controller — replays events from events.jsonl at configurable speed.
Allows reviewers to see metrics update live without running the detection pipeline.
"""

import json
import time
import threading
import os
from app.simulation import start_simulation, stop_simulation, set_speed, get_simulation_status
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import engine
from app.ingestion import ingest_events
from app.models import IngestRequest, Event

# Global simulation state
_sim_state = {
    "running": False,
    "speed": 1.0,
    "thread": None,
    "events_sent": 0,
    "total_events": 0,
    "camera_id": None,
}


def get_simulation_status() -> dict:
    return {
        "running": _sim_state["running"],
        "speed": _sim_state["speed"],
        "events_sent": _sim_state["events_sent"],
        "total_events": _sim_state["total_events"],
        "camera_id": _sim_state["camera_id"],
    }


def _run_simulation(events: list, speed: float):
    """Background thread that replays events at the given speed multiplier."""
    _sim_state["running"] = True
    _sim_state["events_sent"] = 0
    _sim_state["total_events"] = len(events)

    # Sort events by timestamp
    events_sorted = sorted(events, key=lambda e: e.get("timestamp", ""))

    # Replay in batches of 10 events
    BATCH_SIZE = 10
    BASE_INTERVAL = 0.5  # seconds between batches at 1x speed

    for i in range(0, len(events_sorted), BATCH_SIZE):
        if not _sim_state["running"]:
            break

        batch = events_sorted[i:i + BATCH_SIZE]

        try:
            request = IngestRequest(events=[Event(**e) for e in batch])
            with engine.connect() as conn:
                ingest_events(request, conn)
            _sim_state["events_sent"] += len(batch)
        except Exception as ex:
            print(f"Simulation batch error: {ex}")

        # Sleep between batches (adjusted for speed)
        interval = BASE_INTERVAL / max(_sim_state["speed"], 0.1)
        time.sleep(interval)

    _sim_state["running"] = False


def start_simulation(speed: float = 1.0, camera_id: str = None, events_path: str = "data/events.jsonl") -> dict:
    """Start replaying events from events.jsonl."""
    if _sim_state["running"]:
        return {"error": "Simulation already running. Stop it first."}

    if not os.path.exists(events_path):
        return {"error": f"Events file not found: {events_path}. Run detection pipeline first."}

    # Load events
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                    if camera_id is None or event.get("camera_id") == camera_id:
                        events.append(event)
                except Exception:
                    pass

    if not events:
        return {"error": "No events found matching the filter."}

    _sim_state["speed"] = speed
    _sim_state["camera_id"] = camera_id
    _sim_state["events_sent"] = 0

    # Clear existing events for clean replay
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM events WHERE store_id = 'STORE_BLR_002'"))
        conn.commit()

    # Start background thread
    thread = threading.Thread(
        target=_run_simulation,
        args=(events, speed),
        daemon=True
    )
    _sim_state["thread"] = thread
    thread.start()

    return {
        "status": "started",
        "speed": speed,
        "total_events": len(events),
        "camera_id": camera_id or "all",
        "message": f"Replaying {len(events)} events at {speed}x speed"
    }


def stop_simulation() -> dict:
    _sim_state["running"] = False
    return {
        "status": "stopped",
        "events_sent": _sim_state["events_sent"],
        "total_events": _sim_state["total_events"],
    }


def set_speed(speed: float) -> dict:
    _sim_state["speed"] = max(0.1, min(speed, 20.0))
    return {
        "status": "updated",
        "speed": _sim_state["speed"]
    }