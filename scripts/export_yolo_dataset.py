#!/usr/bin/env python3
"""Export verified annotations to YOLO OBB format for training."""
import argparse
import math
import random
import sqlite3
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ANNO_DB = BASE_DIR / "annotations.db"
ZOOM = 19
TILE_SIZE = 256


def obb_corners(cx, cy, w, h, angle):
    """Compute 4 OBB corner points, normalized to 0-1."""
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    dx, dy = w / 2, h / 2
    corners = []
    for sx, sy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        x = cx + sx * dx * cos_a - sy * dy * sin_a
        y = cy + sx * dx * sin_a + sy * dy * cos_a
        corners.append((x / TILE_SIZE, y / TILE_SIZE))
    return corners


TILE_CACHE_DIR = BASE_DIR / "tile_cache"


def download_tile(tx, ty, release_id, output_path):
    """Download tile image from Wayback, using tile_cache if available."""
    cache_key = f"{tx}_{ty}_{release_id}.jpg"
    cache_path = TILE_CACHE_DIR / cache_key

    if cache_path.exists():
        # Copy from cache
        import shutil
        shutil.copy2(cache_path, output_path)
        return

    url = (
        f"https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
        f"World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/"
        f"{release_id}/{ZOOM}/{ty}/{tx}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=15).read()
    output_path.write_bytes(data)

    # Save to cache
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)


def export_dataset(output_dir, min_annotations=1, seed=42):
    output_dir = Path(output_dir)

    conn = sqlite3.connect(str(ANNO_DB))
    conn.row_factory = sqlite3.Row

    # Get all verified tiles
    tiles = conn.execute(
        "SELECT tile_x, tile_y, release_id FROM annotation_meta WHERE status='verified'"
    ).fetchall()

    if not tiles:
        print("No verified tiles found. Annotate some tiles first!")
        return

    # Filter to tiles with enough annotations
    valid_tiles = []
    for t in tiles:
        count = conn.execute(
            "SELECT COUNT(*) FROM annotations WHERE tile_x=? AND tile_y=? AND release_id=?",
            (t["tile_x"], t["tile_y"], t["release_id"])
        ).fetchone()[0]
        if count >= min_annotations:
            valid_tiles.append(t)

    if not valid_tiles:
        print(f"No tiles with >= {min_annotations} annotations.")
        return

    print(f"Found {len(valid_tiles)} verified tiles with >= {min_annotations} annotations")

    # Split: 80/10/10
    random.seed(seed)
    random.shuffle(valid_tiles)
    n = len(valid_tiles)
    n_train = max(1, int(n * 0.8))
    n_val = max(1, int(n * 0.1)) if n > 1 else 0
    splits = {
        "train": valid_tiles[:n_train],
        "val": valid_tiles[n_train:n_train + n_val],
        "test": valid_tiles[n_train + n_val:],
    }
    # If only 1 tile, put it in train
    if n == 1:
        splits = {"train": valid_tiles, "val": [], "test": []}

    total_images = 0
    total_labels = 0

    for split, split_tiles in splits.items():
        if not split_tiles:
            continue

        img_dir = output_dir / "images" / split
        lbl_dir = output_dir / "labels" / split
        img_dir.mkdir(parents=True, exist_ok=True)
        lbl_dir.mkdir(parents=True, exist_ok=True)

        for t in split_tiles:
            tx, ty, rid = t["tile_x"], t["tile_y"], t["release_id"]
            name = f"{tx}_{ty}_{rid}"

            # Download image
            img_path = img_dir / f"{name}.jpg"
            if not img_path.exists():
                try:
                    download_tile(tx, ty, rid, img_path)
                    print(f"  Downloaded {name}")
                except Exception as e:
                    print(f"  FAILED {name}: {e}")
                    continue
            total_images += 1

            # Get annotations
            annos = conn.execute(
                "SELECT cls, cx, cy, w, h, angle FROM annotations "
                "WHERE tile_x=? AND tile_y=? AND release_id=?",
                (tx, ty, rid)
            ).fetchall()

            # Write label file
            lbl_path = lbl_dir / f"{name}.txt"
            lines = []
            for a in annos:
                corners = obb_corners(a["cx"], a["cy"], a["w"], a["h"], a["angle"])
                coords = " ".join(f"{x:.6f} {y:.6f}" for x, y in corners)
                # Use DOTA class IDs: 9=large vehicle, 10=small vehicle
                # Map class 0 (legacy) by size: >27px long = large vehicle
                cls = a["cls"]
                if cls == 0:
                    long_side = max(a["w"], a["h"])
                    cls = 9 if long_side > 27 else 10
                lines.append(f"{cls} {coords}")
                total_labels += 1
            lbl_path.write_text("\n".join(lines) + "\n" if lines else "")

        print(f"  {split}: {len(split_tiles)} tiles")

    conn.close()

    # Write data.yaml
    yaml_path = output_dir / "data.yaml"
    yaml_path.write_text(
        f"path: {output_dir.resolve()}\n"
        f"train: images/train\n"
        f"val: images/val\n"
        f"test: images/test\n"
        f"\n"
        f"names:\n"
        f"  0: plane\n"
        f"  1: ship\n"
        f"  2: storage tank\n"
        f"  3: baseball diamond\n"
        f"  4: tennis court\n"
        f"  5: basketball court\n"
        f"  6: ground track field\n"
        f"  7: harbor\n"
        f"  8: bridge\n"
        f"  9: large vehicle\n"
        f"  10: small vehicle\n"
        f"  11: helicopter\n"
        f"  12: roundabout\n"
        f"  13: soccer ball field\n"
        f"  14: swimming pool\n"
    )

    print(f"\nExported {total_images} images, {total_labels} labels")
    print(f"Dataset config: {yaml_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export annotations to YOLO OBB format")
    parser.add_argument("--output", default="dataset/", help="Output directory")
    parser.add_argument("--min-annotations", type=int, default=1, help="Min annotations per tile")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for split")
    args = parser.parse_args()
    export_dataset(args.output, args.min_annotations, args.seed)
