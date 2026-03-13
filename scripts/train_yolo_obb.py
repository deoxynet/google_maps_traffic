#!/usr/bin/env python3
"""Fine-tune YOLOv8n-OBB on annotated satellite parking tiles."""
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def train(data_yaml, epochs, device, batch, lr0, name):
    from ultralytics import YOLO

    weights = str(BASE_DIR / "yolov8n-obb.pt")
    model = YOLO(weights)

    model.train(
        data=data_yaml,
        epochs=epochs,
        imgsz=256,
        batch=batch,
        device=device,
        lr0=lr0,
        degrees=180,    # full rotation augmentation (aerial = no canonical "up")
        flipud=0.5,     # vertical flip (nadir imagery is rotation-invariant)
        mosaic=0.0,     # disable mosaic (tiles are already small, 256px)
        project=str(BASE_DIR / "runs" / "parking"),
        name=name,
    )

    print(f"\nTraining complete. Results in: runs/parking/{name}/")
    print(f"Best weights: runs/parking/{name}/weights/best.pt")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fine-tune YOLOv8n-OBB on parking tiles")
    parser.add_argument("--data", default="dataset/data.yaml", help="Path to data.yaml")
    parser.add_argument("--epochs", type=int, default=100, help="Training epochs")
    parser.add_argument("--device", default="mps", help="Device (mps, cpu, 0)")
    parser.add_argument("--batch", type=int, default=32, help="Batch size")
    parser.add_argument("--lr0", type=float, default=0.001, help="Initial learning rate")
    parser.add_argument("--name", default="finetune-v1", help="Run name")
    args = parser.parse_args()
    train(args.data, args.epochs, args.device, args.batch, args.lr0, args.name)
