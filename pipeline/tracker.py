"""
pipeline/tracker.py
Visitor tracking + Re-ID logic.
Assigns visitor_id tokens, handles entry/exit/reentry/zone transitions.
"""

import uuid
from datetime import datetime, timezone


class VisitorSession:
    def __init__(self, visitor_id: str, track_id: int, first_seen: str):
        self.visitor_id = visitor_id
        self.track_id = track_id
        self.first_seen = first_seen
        self.last_seen = first_seen
        self.current_zone = None
        self.zone_enter_time = None
        self.zone_dwell_last_emit = None
        self.session_seq = 0
        self.has_entry = False
        self.has_exit = False
        self.is_staff = False
        self.bbox_history = []   # last N bboxes for Re-ID

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


class VisitorTracker:
    def __init__(self, store_id: str, camera_id: str, cam_type: str):
        self.store_id = store_id
        self.camera_id = camera_id
        self.cam_type = cam_type

        # track_id -> VisitorSession (active tracks)
        self.active: dict[int, VisitorSession] = {}

        # visitor_id -> VisitorSession (exited sessions, for Re-ID)
        self.exited: dict[str, VisitorSession] = {}

        # track_id -> visitor_id (for re-entry mapping)
        self.track_to_visitor: dict[int, str] = {}

        # Queue depth tracking (billing cameras)
        self.current_queue_depth = 0

    # ── Re-ID: match a new track_id to an exited visitor ──────────────────────
    def _find_reentry(self, bbox, confidence: float) -> str | None:
        """
        Simple bbox-overlap Re-ID: if a new track appears near where
        an exited visitor was last seen, treat it as re-entry.
        Only used when confidence > 0.6 to avoid false positives.
        """
        if confidence < 0.6:
            return None

        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2

        for vid, session in self.exited.items():
            if not session.bbox_history:
                continue
            last_bbox = session.bbox_history[-1]
            last_cx = (last_bbox[0] + last_bbox[2]) / 2
            last_cy = (last_bbox[1] + last_bbox[3]) / 2
            dist = ((cx - last_cx) ** 2 + (cy - last_cy) ** 2) ** 0.5
            # If within 150px of last known position — likely same person
            if dist < 150:
                return vid
        return None

    # ── Entry event ───────────────────────────────────────────────────────────
    def _make_entry_event(self, session: VisitorSession, timestamp: str, is_reentry: bool) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": "REENTRY" if is_reentry else "ENTRY",
            "timestamp": timestamp,
            "zone_id": None,
            "dwell_ms": 0,
            "is_staff": session.is_staff,
            "confidence": 0.9,
            "metadata": {
                "queue_depth": None,
                "sku_zone": None,
                "session_seq": session.next_seq(),
            },
        }

    # ── Zone enter event ──────────────────────────────────────────────────────
    def _make_zone_enter(self, session: VisitorSession, zone_id: str, timestamp: str, sku_zone: str = None) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": "ZONE_ENTER",
            "timestamp": timestamp,
            "zone_id": zone_id,
            "dwell_ms": 0,
            "is_staff": session.is_staff,
            "confidence": 0.85,
            "metadata": {
                "queue_depth": None,
                "sku_zone": sku_zone,
                "session_seq": session.next_seq(),
            },
        }

    # ── Zone exit event ───────────────────────────────────────────────────────
    def _make_zone_exit(self, session: VisitorSession, zone_id: str, timestamp: str, dwell_ms: int) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": "ZONE_EXIT",
            "timestamp": timestamp,
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": session.is_staff,
            "confidence": 0.85,
            "metadata": {
                "queue_depth": None,
                "sku_zone": None,
                "session_seq": session.next_seq(),
            },
        }

    # ── Zone dwell event ──────────────────────────────────────────────────────
    def _make_zone_dwell(self, session: VisitorSession, zone_id: str, timestamp: str, dwell_ms: int) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": "ZONE_DWELL",
            "timestamp": timestamp,
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": session.is_staff,
            "confidence": 0.85,
            "metadata": {
                "queue_depth": None,
                "sku_zone": None,
                "session_seq": session.next_seq(),
            },
        }

    # ── Billing queue events ──────────────────────────────────────────────────
    def _make_billing_join(self, session: VisitorSession, timestamp: str, queue_depth: int) -> dict:
        return {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": "BILLING_QUEUE_JOIN",
            "timestamp": timestamp,
            "zone_id": "BILLING",
            "dwell_ms": 0,
            "is_staff": session.is_staff,
            "confidence": 0.88,
            "metadata": {
                "queue_depth": queue_depth,
                "sku_zone": None,
                "session_seq": session.next_seq(),
            },
        }

    # ── Parse timestamp to ms ─────────────────────────────────────────────────
    def _ts_to_ms(self, ts: str) -> int:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    # ── Main update called per frame ──────────────────────────────────────────
    def update(
        self,
        detections: list,
        frame,
        frame_num: int,
        timestamp: str,
        frame_w: int,
        frame_h: int,
        zones: list,
        is_staff_fn,
        zone_fn,
    ) -> list:
        events = []
        current_track_ids = set()

        # Count active people in billing zone for queue depth
        billing_count = 0

        for det in detections:
            track_id = det["track_id"]
            bbox = det["bbox"]
            confidence = det["confidence"]
            current_track_ids.add(track_id)

            # Determine zone
            zone_id = zone_fn(bbox, frame_w, frame_h, zones, self.camera_id)

            # Count billing
            if zone_id == "BILLING":
                billing_count += 1

            # New track — create session
            if track_id not in self.active:
                # Check re-entry
                reentry_vid = self._find_reentry(bbox, confidence)
                is_reentry = reentry_vid is not None

                if is_reentry:
                    visitor_id = reentry_vid
                    # Remove from exited
                    session = self.exited.pop(reentry_vid)
                    session.track_id = track_id
                    session.has_exit = False
                else:
                    visitor_id = f"VIS_{uuid.uuid4().hex[:6]}"
                    session = VisitorSession(
                        visitor_id=visitor_id,
                        track_id=track_id,
                        first_seen=timestamp,
                    )

                # Detect staff
                session.is_staff = is_staff_fn(frame, bbox)

                self.active[track_id] = session
                self.track_to_visitor[track_id] = visitor_id

                # Emit ENTRY or REENTRY (entry cameras only, or any cam for floor)
                if self.cam_type in ("entry", "floor", "billing"):
                    if not session.has_entry or is_reentry:
                        session.has_entry = True
                        events.append(self._make_entry_event(session, timestamp, is_reentry))

            session = self.active[track_id]
            session.last_seen = timestamp
            session.bbox_history.append(bbox.tolist() if hasattr(bbox, 'tolist') else list(bbox))
            if len(session.bbox_history) > 10:
                session.bbox_history.pop(0)

            # Zone transition logic
            if zone_id and zone_id != session.current_zone:
                # Exit old zone
                if session.current_zone is not None:
                    dwell_ms = self._ts_to_ms(timestamp) - self._ts_to_ms(session.zone_enter_time or timestamp)
                    events.append(self._make_zone_exit(session, session.current_zone, timestamp, max(0, dwell_ms)))

                # Enter new zone
                sku_zone = next(
                    (z.get("sku_zone") for z in zones if z["zone_id"] == zone_id), None
                )
                session.current_zone = zone_id
                session.zone_enter_time = timestamp
                session.zone_dwell_last_emit = timestamp

                # Billing queue join
                if zone_id == "BILLING" and self.cam_type == "billing":
                    self.current_queue_depth = billing_count
                    if not session.is_staff:
                        events.append(self._make_billing_join(session, timestamp, billing_count))
                else:
                    events.append(self._make_zone_enter(session, zone_id, timestamp, sku_zone))

            # Zone dwell — emit every 30 seconds
            if session.current_zone and session.zone_dwell_last_emit:
                dwell_since_last = self._ts_to_ms(timestamp) - self._ts_to_ms(session.zone_dwell_last_emit)
                if dwell_since_last >= 30000:
                    total_dwell = self._ts_to_ms(timestamp) - self._ts_to_ms(session.zone_enter_time or timestamp)
                    events.append(self._make_zone_dwell(session, session.current_zone, timestamp, total_dwell))
                    session.zone_dwell_last_emit = timestamp

        # Handle disappeared tracks — emit EXIT for entry cameras
        disappeared = set(self.active.keys()) - current_track_ids
        for track_id in disappeared:
            session = self.active.pop(track_id)

            # Close open zone
            if session.current_zone:
                dwell_ms = self._ts_to_ms(timestamp) - self._ts_to_ms(session.zone_enter_time or timestamp)
                events.append(self._make_zone_exit(session, session.current_zone, timestamp, max(0, dwell_ms)))
                session.current_zone = None

            # Emit EXIT on entry cameras
            if self.cam_type == "entry" and not session.has_exit:
                session.has_exit = True
                events.append({
                    "event_id": str(uuid.uuid4()),
                    "store_id": self.store_id,
                    "camera_id": self.camera_id,
                    "visitor_id": session.visitor_id,
                    "event_type": "EXIT",
                    "timestamp": timestamp,
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": session.is_staff,
                    "confidence": 0.85,
                    "metadata": {
                        "queue_depth": None,
                        "sku_zone": None,
                        "session_seq": session.next_seq(),
                    },
                })

            # Move to exited pool for Re-ID
            self.exited[session.visitor_id] = session

            # Keep exited pool small
            if len(self.exited) > 50:
                oldest = next(iter(self.exited))
                del self.exited[oldest]

        return events

    def flush(self, timestamp: str) -> list:
        """Called at end of clip — close all open sessions."""
        events = []
        for track_id, session in list(self.active.items()):
            if session.current_zone:
                dwell_ms = self._ts_to_ms(timestamp) - self._ts_to_ms(session.zone_enter_time or timestamp)
                events.append(self._make_zone_exit(session, session.current_zone, timestamp, max(0, dwell_ms)))
            if self.cam_type == "entry" and not session.has_exit:
                events.append({
                    "event_id": str(uuid.uuid4()),
                    "store_id": self.store_id,
                    "camera_id": self.camera_id,
                    "visitor_id": session.visitor_id,
                    "event_type": "EXIT",
                    "timestamp": timestamp,
                    "zone_id": None,
                    "dwell_ms": 0,
                    "is_staff": session.is_staff,
                    "confidence": 0.85,
                    "metadata": {"queue_depth": None, "sku_zone": None, "session_seq": session.next_seq()},
                })
        self.active.clear()
        return events