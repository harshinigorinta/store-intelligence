"""
pipeline/emit.py
Event schema + emission to JSONL file.
"""

import json
from pathlib import Path


VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT",
    "ZONE_DWELL", "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON", "REENTRY",
}


class EventEmitter:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self.count = 0
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    def validate(self, event: dict) -> bool:
        required = ["event_id", "store_id", "camera_id", "visitor_id",
                    "event_type", "timestamp", "confidence"]
        for field in required:
            if field not in event:
                return False
        if event["event_type"] not in VALID_EVENT_TYPES:
            return False
        if not (0.0 <= event.get("confidence", 0) <= 1.0):
            return False
        return True

    def emit(self, event: dict):
        if not self.validate(event):
            return
        with open(self.output_path, "a") as f:
            f.write(json.dumps(event) + "\n")
        self.count += 1