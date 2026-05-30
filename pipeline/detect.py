"""
pipeline/detect.py
Main detection + tracking script.
Processes CCTV clips using YOLOv8s + ByteTrack, emits structured events.
"""

import cv2
import json
import uuid
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from ultralytics import YOLO
from pipeline.tracker import VisitorTracker
from pipeline.emit import EventEmitter

# ── Constants ──────────────────────────────────────────────────────────────────
PERSON_CLASS_ID = 0          # YOLO class 0 = person
CONFIDENCE_THRESHOLD = 0.35  # minimum detection confidence
FRAME_SKIP = 2               # process every Nth frame (speeds up processing)
DWELL_EMIT_INTERVAL_MS = 30000  # emit ZONE_DWELL every 30 seconds


def load_layout(layout_path: str) -> dict:
    with open(layout_path) as f:
        return json.load(f)


def get_camera_config(layout: dict, camera_file: str) -> tuple:
    """Return (store_id, camera_id, camera_type, clip_start_time, zones) for a given file."""
    for store in layout["stores"]:
        for cam in store["cameras"]:
            if cam["file"] == camera_file:
                return (
                    store["store_id"],
                    cam["camera_id"],
                    cam["type"],
                    cam["clip_start_time"],
                    store["zones"],
                )
    # fallback if layout not matched
    return ("STORE_BLR_002", "CAM_UNKNOWN", "floor", "2026-03-03T10:00:00Z", [])


def frame_to_timestamp(clip_start_time: str, frame_num: int, fps: float) -> str:
    start = datetime.fromisoformat(clip_start_time.replace("Z", "+00:00"))
    offset = timedelta(seconds=frame_num / fps)
    return (start + offset).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_staff_by_color(frame, bbox) -> bool:
    """
    Heuristic: staff wear uniforms with a dominant single hue.
    Crops the bounding box, converts to HSV, checks if >60% pixels
    share a narrow hue band — indicating a uniform.
    """
    x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return False

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue_channel = hsv[:, :, 0].flatten()

    if len(hue_channel) == 0:
        return False

    # Check if majority of pixels fall in a narrow hue range (uniform colour)
    import numpy as np
    hist, _ = np.histogram(hue_channel, bins=18, range=(0, 180))
    dominant_bin_ratio = hist.max() / (len(hue_channel) + 1e-5)
    return bool(dominant_bin_ratio > 0.60)


def get_zone_for_bbox(bbox, frame_w: int, frame_h: int, zones: list, camera_id: str) -> str | None:
    """
    Returns zone_id if the centre of bbox falls inside a zone polygon
    that covers this camera. Polygons are normalised 0-1.
    """
    from shapely.geometry import Point, Polygon

    cx = ((bbox[0] + bbox[2]) / 2) / frame_w
    cy = ((bbox[1] + bbox[3]) / 2) / frame_h
    point = Point(cx, cy)

    for zone in zones:
        if camera_id not in zone.get("camera_ids", []):
            continue
        poly = Polygon(zone["polygon"])
        if poly.contains(point):
            return zone["zone_id"]
    return None


def process_clip(
    video_path: str,
    layout_path: str,
    output_path: str,
    model_path: str = "yolov8s.pt",
):
    print(f"\n{'='*60}")
    print(f"Processing: {video_path}")
    print(f"{'='*60}")

    # Load model
    model = YOLO(model_path)

    # Load layout
    layout = load_layout(layout_path)
    camera_file = Path(video_path).name
    store_id, camera_id, cam_type, clip_start_time, zones = get_camera_config(layout, camera_file)

    print(f"Store: {store_id} | Camera: {camera_id} | Type: {cam_type}")

    # Open video
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    print(f"FPS: {fps:.1f} | Frames: {total_frames} | Resolution: {frame_w}x{frame_h}")

    # Initialise tracker and emitter
    tracker = VisitorTracker(store_id=store_id, camera_id=camera_id, cam_type=cam_type)
    emitter = EventEmitter(output_path=output_path)

    frame_num = 0
    processed = 0

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        frame_num += 1

        # Skip frames for speed
        if frame_num % FRAME_SKIP != 0:
            continue

        timestamp = frame_to_timestamp(clip_start_time, frame_num, fps)

        # Run YOLO detection — only person class
        results = model.track(
            frame,
            persist=True,
            classes=[PERSON_CLASS_ID],
            conf=CONFIDENCE_THRESHOLD,
            tracker="bytetrack.yaml",
            verbose=False,
        )

        # Parse detections
        detections = []
        if results and results[0].boxes is not None:
            boxes = results[0].boxes
            for i in range(len(boxes)):
                bbox = boxes.xyxy[i].cpu().numpy()
                conf = float(boxes.conf[i].cpu().numpy())
                track_id = int(boxes.id[i].cpu().numpy()) if boxes.id is not None else None
                if track_id is None:
                    continue
                detections.append({
                    "track_id": track_id,
                    "bbox": bbox,
                    "confidence": conf,
                })

        # Update tracker + emit events
        events = tracker.update(
            detections=detections,
            frame=frame,
            frame_num=frame_num,
            timestamp=timestamp,
            frame_w=frame_w,
            frame_h=frame_h,
            zones=zones,
            is_staff_fn=is_staff_by_color,
            zone_fn=get_zone_for_bbox,
        )

        for event in events:
            emitter.emit(event)

        processed += 1
        if processed % 100 == 0:
            pct = (frame_num / total_frames * 100) if total_frames > 0 else 0
            print(f"  Frame {frame_num}/{total_frames} ({pct:.1f}%) — {emitter.count} events emitted")

    # Flush final events (open sessions)
    final_events = tracker.flush(
        timestamp=frame_to_timestamp(clip_start_time, frame_num, fps)
    )
    for event in final_events:
        emitter.emit(event)

    cap.release()
    print(f"\nDone. Total events emitted: {emitter.count}")
    print(f"Output: {output_path}\n")


def main():
    parser = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    parser.add_argument("--clips-dir", default="data/clips", help="Directory with MP4 clips")
    parser.add_argument("--layout", default="data/store_layout.json", help="Store layout JSON")
    parser.add_argument("--output", default="data/events.jsonl", help="Output events JSONL file")
    parser.add_argument("--model", default="yolov8s.pt", help="YOLO model path")
    parser.add_argument("--clip", default=None, help="Process single clip (filename only)")
    args = parser.parse_args()

    clips_dir = Path(args.clips_dir)
    output_path = args.output

    # Clear output file
    open(output_path, "w").close()

    if args.clip:
        clips = [clips_dir / args.clip]
    else:
        clips = sorted(clips_dir.glob("*.mp4"))

    print(f"Found {len(clips)} clips to process")

    for clip_path in clips:
        process_clip(
            video_path=str(clip_path),
            layout_path=args.layout,
            output_path=output_path,
            model_path=args.model,
        )

    print(f"\n{'='*60}")
    print(f"Pipeline complete. Events saved to: {output_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()