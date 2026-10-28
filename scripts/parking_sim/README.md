# Curb parking contention simulator

Estimates how hard it is to find a legal street space on each block face, by weekday and 15-minute slot. The output is meant to feed a parking-search path optimizer. Run every script from the repo root.

```bash
python3 scripts/curb/extract_citations.py curb    # curb-relevant tickets (driveway, hydrant, red, bus, loading, no-stop, double)
python3 scripts/parking_sim/supply.py              # block faces, capacity, rule calendars -> data/parking_sim/supply.npz
python3 scripts/parking_sim/sim.py --weekday 2     # one simulated day (Mon=0) -> contention_wd2_w2.npz
python3 scripts/parking_sim/validate.py            # 4 independent checks; the satellite anchor leaves the core out
python3 scripts/parking_sim/export.py --anchor-train-all   # a week, 4 seeds per day -> contention_week.json.gz
python3 scripts/parking_sim/build_viewer.py        # map viewer -> data/parking_sim/viewer.html
python3 scripts/parking_sim/traffic_profiles.py    # Google traffic history (R2, z15) onto blocks -> traffic_z15.npz
python3 scripts/parking_sim/traffic_signal.py      # does traffic show parking search? (2026-10-27: no, see below)
```

## Model
- **Supply** (`supply.py`)
  - **Block faces.** OSM segments (`data/osm_roads.db`) are chained into blocks between intersections, split to ≤ 200 m, with one face per side. This does not depend on `web/graph.json`, so any bbox in LA works.
  - **Capacity.**
    - Start from (length − 7 m setback at each intersection) / 6.1 m.
    - Multiply by (1 − the share of curb that tickets show cannot be parked on). Ticketed driveways count 4.5 m, hydrants 9.1 m, red curb 6 m, and bus zones 25 m. Tickets are clustered by position along the curb. Corners already inside the setback are not subtracted again. The share is pooled over same-type streets within 500 m.
    - Multiply by a residual usable factor.
    - Subtracting tickets per face is right on average but noisy, because tickets are written where parking pressure is high. That is why the share is pooled.
  - **Rules.** Weekly 15-minute calendars:
    - posted street sweeping, by week of the month;
    - permit-district hours, from preferential-parking tickets pooled within 150 m;
    - meter paid hours, max stay, and tow-away windows;
    - no-stopping / no-parking windows and loading-zone hours, from the hours those tickets are written.
- **Demand** (`demand.py`)
  - **Residents:** parcels give units and off-street spaces; LODES gives scale; plus commute and errand trips.
  - **Commuters:** LODES jobs by work block.
  - **Visitors:** busyness ÷ dwell time, ×3 from 5pm to 2am. Places beside a lot, and big-box categories, mostly park off-street.
    One global visitor scale can't fit both: more visitors improve evening spillover but blur middays, where the satellite shows busyness adds nothing.
- **Engine** (`sim.py`)
  - 1-minute steps.
  - A driver checks faces outward from the destination: residents within 300 m, then 800 m, and keep circling; visitors within 400 m; commuters within 500 m.
  - When a sweep window opens, 94% of the cars on that face move.
  - It logs search effort and failures, plus non-residents turned away by permit rules.
- **Anchor** (`anchor.py`, `--anchor night`, the default)
  - The satellite-trained estimator (`scripts/curb/occupancy_model.py`) sets where resident cars live overnight.
  - A single scale makes the area's midday total match.
  - The daytime pattern comes from the simulated dynamics. `--anchor midday` instead matches each cell's midday directly; it validated slightly worse.

## Calibration and validation (2026-10-27)
The core is K-town / Mid-Wilshire, 34.035–34.0725, −118.345 to −118.28: 5,976 faces with parking and 59,945 legal spaces.

Calibrated values:

| Parameter | Value |
|---|---|
| Share of street-parked resident cars that commute | 0.5 |
| Overnight demand scale | 1.3 |
| Visitor scale, daytime | 0.25 |
| Visitor multiplier, 5pm–2am | 3 |
| Commuter curb share | 0.05 |
| Sweep compliance | 0.94 |

These were chosen with a sweep over the commute share and night scale. Pushing the commute share to 0.7 broke middays and saturated nights to 100%. Override any of them with the `PARKSIM_*` environment variables.

| Check (held-out) | Simulator | Benchmark |
|---|---|---|
| Street-sweeping tickets per route area (38 areas) | ρ 0.88; 38 cars left vs 36 tickets per event | — |
| Evening (6pm–2am) permit-district tickets vs simulated outsiders turned away (539 faces) | ρ 0.54 (0.45 without the evening multiplier) | capacity alone 0.09 |
| All-hours permit-district tickets vs outsiders turned away | ρ 0.32 | capacity alone 0.09 |
| Night double-parking tickets vs search effort (1,014 cells) | ρ 0.34 | satellite estimator 0.34, capacity −0.38 |
| Midday cars, 2010 aerial (701 cells), Wed / Sat | ρ 0.30 / 0.39 | satellite estimator 0.40 |
| Capacity vs meter count (137 metered faces) | mean error 5.2 spaces | geometry alone 10.5 |

Two validation choices to keep:
- The night check uses double parking only. Red-zone, hydrant, and driveway tickets also set capacity, so using them made the check circular: capacity correlated −0.68 with them.
- The old satellite spot clusters in `web/parking_spots.json` sit a median 20 m from the curb (mostly lots and driveways), so they are not used for capacity.

## Google traffic history
1,057 z15 snapshots (Mar–May 2026, plus new runs) were mapped onto blocks. The test compared each block's evening (6pm–midnight) congestion with its own weekday-midday baseline.
- Result: no parking-search signal on residential streets. Per-cell ρ vs evening permit tickets, night double parking, and simulated contention: −0.04, −0.08, and −0.04.
- Evenings are less congested than middays on arterials and residential streets alike.
- The data stays useful for arterial drive times by hour in the search-path optimizer.
- A fairer retest needs overnight free-flow snapshots (the scrape schedule skips about midnight–6am).

## Known gaps
- North K-town (3rd St to Beverly) needs parcel data. `dtla_parcels.csv` ends at 34.073.
- Which side of the street is swept on which day is not in the data. The convention is that the north or east side takes the earlier day.
- Divided roads in OSM produce median-side faces that probably have no parking.
- Evenings rest on ticket proxies; there are no ground counts.
- Midday spatial accuracy is just below the satellite estimator.
