# Curb parking analysis

Satellite and citation pipelines that feed the street-parking contention model. Run every script from the repo root.

| Script | What it does |
|---|---|
| `detect_tiles.py <release>` | Runs the fine-tuned detector (`models/curb_obb_ft512.pt`) over `data/tiles/z19/<release>/`. Writes `data/curb/dets_<release>_ft.npz` |
| `train_detector.py` | Re-trains the detector on the verified tiles in `dataset/` and prints test recall and precision |
| `calibrate_offsets.py <release>` | Prints car offsets from the OSM centerline per road class, used to set the curb bands in `common.py` |
| `occupancy_model.py` | Predicts curbside cars per km of curb, validated on a held-out area and on 2010 vs 2025 imagery |
| `extract_citations.py ppd\|sweep` | Pulls LA citations from `~/git/la-parking/db/parking.db` into `data/curb/` |
| `sweeping_from_citations.py` | Checks that the sweeping schedule can be inferred from citations |
| `fetch_wayback_slow.py` | Polite one-request-at-a-time Wayback tile fetcher that stops on the first 403/429 |

## Data
- `data/tiles/z19/10/`: Wayback release 10. Despite the 2014 release date, this is Microsoft aerial imagery from May 2010 at 30 cm, covering the sim area.
- `data/tiles/z19/63116/`: latest release (2026-09). Covers a K-town strip (802 tiles). The imagery is Vantor satellite at 0.34 m, captured Sep or Nov 2025. It is off-nadir and shadowed.
- `data/curb/`: detections, extracted citations, and the Wayback config and metadata.

## Baselines (2026-10-27)
- **Detector:** stock DOTA weights find 18–32% of labeled cars. The fine-tuned model gets 96% recall / 79% precision at conf 0.25, and roughly 65–75% recall on the 2010 aerial.
- **Occupancy, held-out K-town/Mid-Wilshire:**
  - Street type alone: ρ 0.29–0.33, R² ≈ 0.
  - Adding residents, jobs, and buildings: ρ 0.58, R² 0.35. The predicted "fullest 25%" is right 60% of the time.
  - Busyness adds nothing at midday.
- **Persistence, 2010 vs 2025 on 62 K-town cells:** ρ 0.40.
- **Sweeping rules from citations:** days right 98%, weeks right 99%, median 98% of citations inside the posted windows.

## Gotchas
- Homebrew python3.14 training crashes in pyexpat. Prefix the command with `DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`, and launch python directly: `nohup` and `env` strip `DYLD_*`.
- In LA's `street_sweeping.db`, `odd_even` means weeks of the month (Odd = 1st and 3rd, Even = 2nd and 4th), not the side of the street.
- Wayback metadata patches overlap, so the capture date for a given location can be ambiguous.
- Releases 301-redirect unchanged tiles to the release that last changed them.
