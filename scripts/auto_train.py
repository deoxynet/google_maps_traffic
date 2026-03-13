#!/usr/bin/env python3
"""Auto-training subprocess: export verified annotations + fine-tune YOLO OBB.

Called by parking_server.py when verified tile count crosses threshold.
Writes progress to training_status.json for the UI to poll.

Usage: python scripts/auto_train.py <verified_count> <epochs>
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATUS_FILE = BASE_DIR / "training_status.json"


def write_status(state, **kwargs):
    """Atomic JSON write to training_status.json."""
    data = {"state": state, **kwargs}
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(STATUS_FILE)


def find_latest_model():
    """Scan runs/parking/auto-v*/weights/best.pt, return highest version.
    Falls back to yolov8n-obb.pt."""
    runs_dir = BASE_DIR / "runs" / "parking"
    if not runs_dir.exists():
        return str(BASE_DIR / "yolov8n-obb.pt"), 1

    versions = []
    for d in runs_dir.iterdir():
        m = re.match(r"auto-v(\d+)", d.name)
        if m:
            best = d / "weights" / "best.pt"
            if best.exists():
                versions.append((int(m.group(1)), str(best)))

    if versions:
        versions.sort()
        last_v, last_path = versions[-1]
        return last_path, last_v + 1

    return str(BASE_DIR / "yolov8n-obb.pt"), 1


def main():
    verified_count = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    epochs = int(sys.argv[2]) if len(sys.argv) > 2 else 15

    try:
        # Step 1: Export dataset
        write_status("exporting", verified_count=verified_count)

        # Import export function from sibling script
        sys.path.insert(0, str(BASE_DIR / "scripts"))
        from export_yolo_dataset import export_dataset

        dataset_dir = BASE_DIR / "dataset"
        export_dataset(str(dataset_dir), min_annotations=1, seed=42)

        data_yaml = dataset_dir / "data.yaml"
        if not data_yaml.exists():
            write_status("error", error="No data.yaml — export produced nothing")
            return

        # Step 2: Train
        # Prefer the currently active model (passed as argv[3]) over scanning auto-v* runs
        active_model = sys.argv[3] if len(sys.argv) > 3 else None
        if active_model and Path(active_model).exists():
            _, next_version = find_latest_model()
            base_weights = active_model
        else:
            base_weights, next_version = find_latest_model()
        run_name = f"auto-v{next_version}"

        write_status("training", epoch=0, total_epochs=epochs,
                     run_name=run_name, base_weights=base_weights)

        from ultralytics import YOLO

        model = YOLO(base_weights)

        # Register epoch callback for progress updates
        def on_epoch_end(trainer):
            write_status("training",
                         epoch=trainer.epoch + 1,
                         total_epochs=epochs,
                         run_name=run_name,
                         base_weights=base_weights)

        model.add_callback("on_train_epoch_end", on_epoch_end)

        model.train(
            data=str(data_yaml),
            epochs=epochs,
            imgsz=256,
            batch=16,
            device="mps",
            lr0=0.001,
            degrees=180,
            flipud=0.5,
            mosaic=0.0,
            project=str(BASE_DIR / "runs" / "parking"),
            name=run_name,
        )

        # Step 3: Done
        best_model = str(BASE_DIR / "runs" / "parking" / run_name / "weights" / "best.pt")
        if Path(best_model).exists():
            write_status("done", best_model=best_model, run_name=run_name,
                         verified_count=verified_count)
        else:
            write_status("error", error=f"Training finished but {best_model} not found")

    except Exception as e:
        import traceback
        write_status("error", error=str(e), traceback=traceback.format_exc())


if __name__ == "__main__":
    main()
