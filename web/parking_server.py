#!/usr/bin/env python3
"""Dev server for parking map with live YOLO detection + annotation API."""
import base64
import io
import json
import math
import sqlite3
import subprocess
import sys
import urllib.request
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "scripts"))
DB_PATH = Path("/Volumes/PRO-G40/satellite_parking.db")
ANNO_DB_PATH = BASE_DIR / "annotations.db"
YOLO_MODEL = str(BASE_DIR / "yolov8m-obb.pt")
ZOOM = 19
YOLO_CONFIDENCE = 0.15
VEHICLE_CLASSES = {9, 10}

_model = None
_model_path = None
_sam_model = None

# --- Auto-training state ---
_training_process = None
_last_trained_verified = 0
TRAINING_INTERVAL = 10
STATUS_FILE = BASE_DIR / "training_status.json"
TILE_CACHE_DIR = BASE_DIR / "tile_cache"


def get_model():
    global _model, _model_path
    if _model is None or _model_path != YOLO_MODEL:
        from ultralytics import YOLO
        print(f"Loading YOLO model: {YOLO_MODEL}")
        _model = YOLO(YOLO_MODEL)
        _model_path = YOLO_MODEL
        print("YOLO ready.")
    return _model


def get_sam():
    global _sam_model
    if _sam_model is None:
        from ultralytics import SAM
        from ultralytics.models.sam.predict import Predictor as SAMPredictor
        # Try local paths, fall back to auto-download
        candidates = [
            BASE_DIR / "mobile_sam.pt",
            Path("/Users/k/git/la-parking/FastSAM-s.pt"),
        ]
        sam_path = "mobile_sam.pt"  # ultralytics auto-downloads
        for p in candidates:
            if p.exists():
                sam_path = str(p)
                break
        print(f"Loading SAM model: {sam_path}")
        _sam_model = SAM(sam_path)

        # Monkey-patch to force multimask_output=True (returns 3 masks at
        # different granularity levels instead of 1, letting us pick the
        # vehicle-sized one rather than getting the whole parking lot).
        _orig_pi = SAMPredictor.prompt_inference

        def _multimask_pi(self, im, bboxes=None, points=None, labels=None,
                          masks=None, multimask_output=False):
            return _orig_pi(self, im, bboxes=bboxes, points=points,
                            labels=labels, masks=masks, multimask_output=True)

        SAMPredictor.prompt_inference = _multimask_pi

        print("SAM ready (multimask enabled).")
    return _sam_model


def sam_click(tx, ty, release_id, points):
    """Run SAM with point prompt(s), fit OBB to the mask.

    points: list of [x, y] coordinates (one or more).
    """
    import numpy as np
    import cv2

    url = (
        f"https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
        f"World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/"
        f"{release_id}/{ZOOM}/{ty}/{tx}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    tile_data = urllib.request.urlopen(req, timeout=15).read()

    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
        f.write(tile_data)
        tmp_path = f.name

    sam = get_sam()
    labels = [1] * len(points)
    results = sam(tmp_path, points=points, labels=labels,
                  device="mps", verbose=False)
    os.unlink(tmp_path)

    result = results[0]
    if result.masks is None or len(result.masks) == 0:
        return {"ok": False, "error": "No mask found at click point"}

    # SAM returns multiple masks at different granularity levels (3 with
    # multimask_output=True). Pick the one closest to typical vehicle size.
    # At z19 (~0.3m/px), a car is roughly 15x6 px = ~90px area,
    # a truck ~25x8 = ~200px. Target range: 30-2000px area.
    VEHICLE_AREA_TARGET = 150  # ideal vehicle area in pixels
    MIN_VEHICLE_AREA = 20
    MAX_VEHICLE_AREA = 3000

    best_contour = None
    best_area = None
    best_score = float('inf')
    n_masks = result.masks.data.shape[0]

    for mi in range(n_masks):
        mask = result.masks.data[mi].cpu().numpy().astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(contour)
        # Score: log-distance from target area (lower is better)
        if area >= MIN_VEHICLE_AREA:
            score = abs(math.log(area / VEHICLE_AREA_TARGET))
            if score < best_score:
                best_contour = contour
                best_area = area
                best_score = score

    if best_contour is None:
        return {"ok": False, "error": "No suitable mask found"}

    contour = best_contour
    area = best_area

    if area > MAX_VEHICLE_AREA:
        return {"ok": False, "error": f"Mask too large ({area:.0f}px) — try clicking more precisely on the vehicle"}

    rect = cv2.minAreaRect(contour)
    (cx, cy), (w, h), angle_deg = rect

    # cv2.minAreaRect returns angle in degrees [-90, 0) — convert to radians
    angle = math.radians(angle_deg)

    # Ensure w >= h (swap if needed so w is the longer side)
    if h > w:
        w, h = h, w
        angle += math.pi / 2

    # At z19 (~0.3m/px), large vehicle (truck/bus) > ~27px long
    long_side = max(w, h)
    vehicle_cls = 9 if long_side > 27 else 10  # 9=large, 10=small

    return {
        "ok": True,
        "annotation": {
            "cls": vehicle_cls,
            "cx": round(cx, 1),
            "cy": round(cy, 1),
            "w": round(w, 1),
            "h": round(h, 1),
            "angle": round(angle, 4),
            "source": "sam",
        }
    }


def swap_model(new_path):
    """Hot-swap the YOLO model (for active learning iterations)."""
    global YOLO_MODEL, _model, _model_path
    p = Path(new_path)
    if not p.exists():
        return {"error": f"Model not found: {new_path}"}
    YOLO_MODEL = str(p)
    _model = None
    _model_path = None
    get_model()  # eager load to verify it works
    return {"ok": True, "model": YOLO_MODEL}


def init_anno_db():
    """Create annotation tables if they don't exist."""
    conn = sqlite3.connect(str(ANNO_DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tile_x INTEGER NOT NULL,
            tile_y INTEGER NOT NULL,
            release_id INTEGER NOT NULL,
            cls INTEGER NOT NULL,
            cx REAL NOT NULL,
            cy REAL NOT NULL,
            w REAL NOT NULL,
            h REAL NOT NULL,
            angle REAL NOT NULL,
            source TEXT DEFAULT 'manual',
            UNIQUE(tile_x, tile_y, release_id, cx, cy)
        );
        CREATE TABLE IF NOT EXISTS annotation_meta (
            tile_x INTEGER NOT NULL,
            tile_y INTEGER NOT NULL,
            release_id INTEGER NOT NULL,
            status TEXT DEFAULT 'unlabeled',
            PRIMARY KEY (tile_x, tile_y, release_id)
        );
    """)
    conn.close()


def get_releases():
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT release_id, capture_date FROM releases ORDER BY capture_date DESC"
    ).fetchall()
    conn.close()
    return [{"release_id": r[0], "capture_date": r[1]} for r in rows]


def _dedup_detections(candidates, iou_thresh=0.3):
    """Remove overlapping OBBs, preferring angle-consistent ones.

    For each pair that overlaps (center distance < half the sum of long sides),
    keep the one whose angle better matches the median angle of nearby detections.
    """
    if len(candidates) <= 1:
        return candidates

    import math as _math

    # Compute pairwise overlap using center distance as a fast proxy
    n = len(candidates)
    suppress = [False] * n

    for i in range(n):
        if suppress[i]:
            continue
        ci = candidates[i]
        ri = max(ci["w"], ci["h"]) / 2
        for j in range(i + 1, n):
            if suppress[j]:
                continue
            cj = candidates[j]
            rj = max(cj["w"], cj["h"]) / 2
            dx = ci["cx"] - cj["cx"]
            dy = ci["cy"] - cj["cy"]
            dist = _math.sqrt(dx * dx + dy * dy)
            # Overlap if centers are closer than average of half-lengths
            if dist < (ri + rj) * 0.5:
                # Find nearby non-overlapping detections for angle consensus
                median_angle = _neighbor_median_angle(candidates, i, j, suppress)
                # Compare angles (mod pi — cars are symmetric)
                diff_i = _angle_diff(ci["angle"], median_angle)
                diff_j = _angle_diff(cj["angle"], median_angle)
                if diff_i <= diff_j:
                    suppress[j] = True
                else:
                    suppress[i] = True
                    break

    return [c for c, s in zip(candidates, suppress) if not s]


def _neighbor_median_angle(candidates, idx_a, idx_b, suppress):
    """Median angle of nearby detections (excluding the two overlapping ones)."""
    import math as _math
    cx = (candidates[idx_a]["cx"] + candidates[idx_b]["cx"]) / 2
    cy = (candidates[idx_a]["cy"] + candidates[idx_b]["cy"]) / 2
    angles = []
    for k, c in enumerate(candidates):
        if k == idx_a or k == idx_b or suppress[k]:
            continue
        dx = c["cx"] - cx
        dy = c["cy"] - cy
        if dx * dx + dy * dy < 80 * 80:  # within ~80px
            angles.append(c["angle"] % _math.pi)
    if not angles:
        # No neighbors — fall back to higher confidence
        return candidates[idx_a]["angle"] if candidates[idx_a]["conf"] >= candidates[idx_b]["conf"] else candidates[idx_b]["angle"]
    angles.sort()
    return angles[len(angles) // 2]


def _angle_diff(a, b):
    """Smallest angle difference mod pi (vehicles are symmetric)."""
    import math as _math
    d = abs((a % _math.pi) - (b % _math.pi))
    return min(d, _math.pi - d)


def detect_tile(tx, ty, release_id):
    """Download tile, run YOLO, return annotated image + detections."""
    url = (
        f"https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
        f"World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/"
        f"{release_id}/{ZOOM}/{ty}/{tx}"
    )

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    tile_data = urllib.request.urlopen(req, timeout=15).read()

    # Cache tile for training
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = TILE_CACHE_DIR / f"{tx}_{ty}_{release_id}.jpg"
    if not cache_path.exists():
        cache_path.write_bytes(tile_data)

    # Save to temp file for YOLO
    import tempfile, os
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
        f.write(tile_data)
        tmp_path = f.name

    model = get_model()
    results = model(tmp_path, conf=YOLO_CONFIDENCE, device="mps", verbose=False)
    result = results[0]
    os.unlink(tmp_path)

    # Load building mask for rooftop false-positive filtering
    building_mask = None
    try:
        from building_mask import get_building_mask
        building_mask = get_building_mask(tx, ty)
    except Exception:
        pass

    # Load road zone mask for parked vs driving classification
    road_mask = None
    try:
        from road_mask import get_road_mask
        road_mask = get_road_mask(tx, ty)
    except Exception:
        pass

    # Collect candidates, then filter
    candidates = []
    if result.obb is not None and len(result.obb) > 0:
        for i in range(len(result.obb)):
            cls = int(result.obb.cls[i].item())
            if cls not in VEHICLE_CLASSES:
                continue

            xywhr = result.obb.xywhr[i].tolist()
            cx, cy, w, h, angle = xywhr

            # Filter rooftop false positives
            if building_mask is not None:
                ix, iy = int(cx), int(cy)
                if 0 <= ix < 256 and 0 <= iy < 256 and building_mask[iy, ix]:
                    continue

            # Filter by aspect ratio — cars are elongated, not square
            long_side = max(w, h)
            short_side = min(w, h)
            aspect = long_side / short_side if short_side > 0 else 1.0
            if aspect < 1.5:
                continue

            conf = float(result.obb.conf[i].item())
            corners = result.obb.xyxyxyxy[i].cpu().numpy()
            pts = [(float(corners[j][0]), float(corners[j][1])) for j in range(4)]

            candidates.append({
                "cls": cls,
                "conf": conf,
                "cx": cx, "cy": cy, "w": w, "h": h, "angle": angle,
                "corners": pts,
            })

    # Deduplicate overlapping OBBs — when two boxes overlap significantly,
    # keep the one whose angle is more consistent with neighbors
    detections = _dedup_detections(candidates)

    # Draw vehicle OBBs on the tile
    img = Image.open(io.BytesIO(tile_data)).convert("RGB")
    draw = ImageDraw.Draw(img)

    for det in detections:
        conf = det["conf"]
        # Color by confidence
        if conf >= 0.5:
            color = (0, 255, 70)
        elif conf >= 0.3:
            color = (255, 255, 0)
        else:
            color = (255, 100, 50)

        # Draw OBB as 4 line segments
        pts = det["corners"]
        for j in range(4):
            draw.line([pts[j], pts[(j + 1) % 4]], fill=color, width=2)

        # Classify road zone: 0=off-road, 1=parking-edge, 2=driving
        rz = 0
        if road_mask is not None:
            rix, riy = int(det["cx"]), int(det["cy"])
            if 0 <= rix < 256 and 0 <= riy < 256:
                rz = int(road_mask[riy, rix])
        det["road_zone"] = rz

        # Round for JSON output
        det["conf"] = round(conf, 3)
        det["cx"] = round(det["cx"], 1)
        det["cy"] = round(det["cy"], 1)
        det["w"] = round(det["w"], 1)
        det["h"] = round(det["h"], 1)
        det["angle"] = round(det["angle"], 4)

    # Encode as JPEG base64
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=92)

    return {
        "image": base64.b64encode(buf.getvalue()).decode(),
        "detections": detections,
        "vehicle_count": len(detections),
        "tile_x": tx,
        "tile_y": ty,
        "release_id": release_id,
    }


def get_annotations(tx, ty, release_id):
    """Load annotations for a tile."""
    conn = sqlite3.connect(str(ANNO_DB_PATH))
    conn.row_factory = sqlite3.Row
    annos = conn.execute(
        "SELECT cls, cx, cy, w, h, angle, source FROM annotations "
        "WHERE tile_x=? AND tile_y=? AND release_id=?",
        (tx, ty, release_id)
    ).fetchall()
    meta = conn.execute(
        "SELECT status FROM annotation_meta WHERE tile_x=? AND tile_y=? AND release_id=?",
        (tx, ty, release_id)
    ).fetchone()
    conn.close()
    return {
        "tile_x": tx, "tile_y": ty, "release_id": release_id,
        "status": meta["status"] if meta else "unlabeled",
        "annotations": [dict(a) for a in annos],
    }


def save_annotations(data):
    """Replace all annotations for a tile (atomic)."""
    tx = data["tile_x"]
    ty = data["tile_y"]
    rid = data["release_id"]
    status = data.get("status", "verified")
    annos = data.get("annotations", [])

    conn = sqlite3.connect(str(ANNO_DB_PATH))
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM annotations WHERE tile_x=? AND tile_y=? AND release_id=?",
            (tx, ty, rid)
        )
        for a in annos:
            conn.execute(
                "INSERT INTO annotations (tile_x, tile_y, release_id, cls, cx, cy, w, h, angle, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (tx, ty, rid, a.get("cls", 0), a["cx"], a["cy"],
                 a["w"], a["h"], a["angle"], a.get("source", "manual"))
            )
        conn.execute(
            "INSERT OR REPLACE INTO annotation_meta (tile_x, tile_y, release_id, status) "
            "VALUES (?, ?, ?, ?)",
            (tx, ty, rid, status)
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "count": len(annos)}


def bootstrap_tile(tx, ty, release_id):
    """Run YOLO on tile and save detections as bootstrap annotations."""
    det = detect_tile(tx, ty, release_id)
    annos = []
    for d in det["detections"]:
        annos.append({
            "cls": d["cls"],  # preserve DOTA class (9=large, 10=small vehicle)
            "cx": d["cx"], "cy": d["cy"],
            "w": d["w"], "h": d["h"],
            "angle": d["angle"],
            "source": "bootstrap",
        })
    save_annotations({
        "tile_x": tx, "tile_y": ty, "release_id": release_id,
        "status": "bootstrapped",
        "annotations": annos,
    })
    return {
        "ok": True,
        "count": len(annos),
        "annotations": annos,
        "image": det["image"],
    }


def get_sample_tiles(n=50):
    """Return N random tile coords, prioritizing unlabeled/bootstrapped over verified."""
    # Get all verified tile coords to exclude
    anno_conn = sqlite3.connect(str(ANNO_DB_PATH))
    verified_rows = anno_conn.execute(
        "SELECT tile_x, tile_y FROM annotation_meta WHERE status='verified'"
    ).fetchall()
    verified_set = {(r[0], r[1]) for r in verified_rows}

    # Also get bootstrapped tiles to show their status
    meta_map = {}
    for row in anno_conn.execute("SELECT tile_x, tile_y, status FROM annotation_meta").fetchall():
        meta_map[(row[0], row[1])] = row[2]
    anno_conn.close()

    conn = sqlite3.connect(str(DB_PATH))
    # Pull more candidates than needed so we can filter out verified
    rows = conn.execute(
        "SELECT DISTINCT tile_x, tile_y FROM detections ORDER BY RANDOM() LIMIT ?",
        (n * 3,)
    ).fetchall()
    conn.close()

    result = []
    for tx, ty in rows:
        if (tx, ty) in verified_set:
            continue
        status = meta_map.get((tx, ty), "unlabeled")
        result.append({"tile_x": tx, "tile_y": ty, "status": status})
        if len(result) >= n:
            break

    return result


def get_annotate_stats():
    """Return counts by annotation status."""
    conn = sqlite3.connect(str(ANNO_DB_PATH))
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM annotation_meta GROUP BY status"
    ).fetchall()
    total_annos = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    conn.close()
    stats = {r[0]: r[1] for r in rows}
    stats["total_annotations"] = total_annos
    return stats


def _read_training_status():
    """Read training_status.json, return dict or None."""
    try:
        if STATUS_FILE.exists():
            return json.loads(STATUS_FILE.read_text())
    except Exception:
        pass
    return None


def _check_training_result():
    """Check if training subprocess finished; auto-swap model if done."""
    global _training_process
    if _training_process is None:
        return
    ret = _training_process.poll()
    if ret is None:
        return  # still running
    _training_process = None
    status = _read_training_status()
    if status and status.get("state") == "done":
        best = status.get("best_model")
        if best and Path(best).exists():
            result = swap_model(best)
            print(f"[auto-train] Model swapped: {result}")


def _get_verified_count():
    """Count verified tiles in annotation DB."""
    conn = sqlite3.connect(str(ANNO_DB_PATH))
    count = conn.execute(
        "SELECT COUNT(*) FROM annotation_meta WHERE status='verified'"
    ).fetchone()[0]
    conn.close()
    return count


def maybe_trigger_training():
    """Check threshold and spawn auto_train.py if needed."""
    global _training_process, _last_trained_verified

    # If a training process exists, check if it finished
    _check_training_result()

    # Don't start if already running
    if _training_process is not None:
        return

    verified = _get_verified_count()
    # Trigger when we've gained TRAINING_INTERVAL more verified tiles
    if verified - _last_trained_verified >= TRAINING_INTERVAL:
        _start_training(verified)


def _start_training(verified_count, epochs=30):
    """Spawn auto_train.py subprocess."""
    global _training_process, _last_trained_verified
    script = str(BASE_DIR / "scripts" / "auto_train.py")
    cmd = [sys.executable, script, str(verified_count), str(epochs), YOLO_MODEL]
    print(f"[auto-train] Starting: {' '.join(cmd)}")
    _training_process = subprocess.Popen(cmd)
    _last_trained_verified = verified_count


def get_training_status():
    """Return training status for the API, enriched with process liveness."""
    _check_training_result()
    status = _read_training_status()
    if status is None:
        return {"state": "idle"}
    # If status says training/exporting but process is dead, mark as error
    if status.get("state") in ("training", "exporting") and _training_process is None:
        status["state"] = "error"
        status["error"] = status.get("error", "Training process exited unexpectedly")
    status["process_alive"] = _training_process is not None
    return status


class ParkingHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == '/api/releases':
            self._json_response(get_releases())
        elif parsed.path == '/api/detect':
            params = parse_qs(parsed.query)
            try:
                tx = int(params['tx'][0])
                ty = int(params['ty'][0])
                rid = int(params['rid'][0])
                data = detect_tile(tx, ty, rid)
                self._json_response(data)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=500)
        elif parsed.path == '/api/annotations':
            params = parse_qs(parsed.query)
            try:
                tx = int(params['tx'][0])
                ty = int(params['ty'][0])
                rid = int(params['rid'][0])
                self._json_response(get_annotations(tx, ty, rid))
            except Exception as e:
                self._json_response({"error": str(e)}, status=400)
        elif parsed.path == '/api/sample':
            params = parse_qs(parsed.query)
            n = int(params.get('n', ['50'])[0])
            self._json_response(get_sample_tiles(n))
        elif parsed.path == '/api/annotate_stats':
            self._json_response(get_annotate_stats())
        elif parsed.path == '/api/model':
            self._json_response({"model": YOLO_MODEL})
        elif parsed.path == '/api/training_status':
            self._json_response(get_training_status())
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        content_len = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_len)

        if parsed.path == '/api/annotations':
            try:
                data = json.loads(body)
                result = save_annotations(data)
                self._json_response(result)
                # Auto-trigger training after verified save
                if data.get("status") == "verified":
                    maybe_trigger_training()
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=400)
        elif parsed.path == '/api/training_start':
            try:
                _check_training_result()
                if _training_process is not None:
                    self._json_response({"error": "Training already in progress"}, status=409)
                else:
                    verified = _get_verified_count()
                    data = json.loads(body) if body else {}
                    epochs = data.get("epochs", 15)
                    _start_training(verified, epochs=epochs)
                    self._json_response({"ok": True, "verified_count": verified})
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=500)
        elif parsed.path == '/api/bootstrap':
            params = parse_qs(parsed.query)
            try:
                tx = int(params['tx'][0])
                ty = int(params['ty'][0])
                rid = int(params['rid'][0])
                result = bootstrap_tile(tx, ty, rid)
                self._json_response(result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=500)
        elif parsed.path == '/api/sam_click':
            try:
                data = json.loads(body)
                # Accept either multi-point "points" or legacy single "click_x"/"click_y"
                if "points" in data:
                    points = data["points"]
                else:
                    points = [[data["click_x"], data["click_y"]]]
                result = sam_click(
                    data["tile_x"], data["tile_y"], data["release_id"],
                    points,
                )
                self._json_response(result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=500)
        elif parsed.path == '/api/model':
            try:
                data = json.loads(body)
                result = swap_model(data["path"])
                if "error" in result:
                    self._json_response(result, status=400)
                else:
                    self._json_response(result)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._json_response({"error": str(e)}, status=400)
        else:
            self._json_response({"error": "Not found"}, status=404)

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if '/api/' in str(args[0]):
            super().log_message(format, *args)


if __name__ == '__main__':
    import os
    os.chdir(Path(__file__).parent)
    init_anno_db()

    # Restore _last_trained_verified from status file
    status = _read_training_status()
    if status and status.get("state") == "done":
        _last_trained_verified = status.get("verified_count", 0)
        print(f"[auto-train] Restored last trained count: {_last_trained_verified}")

    port = 8081
    print(f"Parking map: http://localhost:{port}/parking_map.html")
    print(f"Annotation UI: http://localhost:{port}/annotate.html")
    server = HTTPServer(('', port), ParkingHandler)
    server.serve_forever()
