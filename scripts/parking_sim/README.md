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
BOUNDS=34.029,-118.351,34.0785,-118.274 cargo run --release --example export_street_ways --features osm -- socal.osm.pbf
python3 scripts/parking_sim/network.py             # directed driving graph (one-way, names, curb faces per direction)
python3 scripts/parking_sim/lots.py                # public lots (city, LAZ, SP+, ACE, ABM, OSM) -> lots.json
python3 scripts/parking_sim/optimize.py --dest 34.0560,-118.3090 --day Sat --time 13:00   # search loop + Maps links
python3 scripts/parking_sim/evaluate_routes.py     # probe drivers follow optimized vs circling routes inside the sim
python3 scripts/parking_sim/build_app.py           # Curb Finder app page (optimizer in the browser + trip log)
```

## Search-path optimizer (`optimize.py`)
- **What it finds:** the driving loop with the lowest expected time from arrival to the door.
- **Per block:**
  - A directed block offers the curb a driver can legally take: the right side on two-way streets, both sides on one-way streets.
  - Its chance of a space is the simulator's **capture rate**: the share of simulated searchers who checked that face in that 15-minute slot and got a space.
  - Share-of-time-free is the wrong measure. In saturated K-town evenings it is 0 everywhere, even though freed spaces do get taken.
  - A second pass over a block on the same loop counts at 35%.
- **Cost of the search:**
  - Parking costs the drive so far, plus 30 s to park, plus the walk at 1.3 m/s.
  - Ending the loop without a space costs the best public lot from where the loop ends: driving time over the graph, plus 2 min to get in and pay, plus the walk. If no lot is within reach, it's 15 min of searching farther.
  - Lots come from `lots.json`: 47 in the area. Only the 6 city lots have posted rates and hours.
  - 17 untagged OSM multi-storey garages count as "likely public", with a 5-minute penalty because they may be tenant-only.
- **Search method:** beam search over loops of up to 10 minutes of driving.
- **Baseline:** a driver who keeps taking the next block closest to the destination.
- **Output:** Google Maps links with up to 9 waypoints (desktop) or 3 (mobile), placed at the middle of each block after a turn. The origin is omitted, so the route starts at the user's location.
- **Examples (2026-10-27):**
  - Western & 8th, Sat 1pm: a 56% loop, 12.2 min expected vs 19.7 for typical circling; walking up to 800 m brings it to 7.8 min.
  - Country Club Park, Wed 7pm: 3.8 min vs 18.4. Circling the nearest blocks hits permit-only streets.
  - Western & 6th, Fri 8pm: about 5% within 400 m, so the advice is a lot. That's the simulator's view; evening absolute levels are uncalibrated.
- **Answer format:** each query reports "straight to a lot" (name, walk, price for `--stay` hours where posted) next to the best street loop, with a lot to use if the loop fails. It also shows whether walking up to 800 m is worth it.
- **Calibration (`PARKSIM_P_SCALE`, default 0.5; 0.25 before 2026-10-28):** the capture rate counts every face a simulated searcher scans in one instant, so it overstates one driver's chance, by about 2× with the current model. `evaluate_routes.py` checked this with probe drivers following the planned loops minute by minute in a fresh simulated day:
  - 30 destinations, 5 time slots, 6 start times.
  - Optimized: 7.9 min to the door. Typical circling: 14.7 min. The optimized loop was faster or equal in 89% of 900 paired trips.
  - With 0.25, predicted loop times matched the probes within about 0.8 min in every slot.
  - Rechecked 2026-10-28 with the current model: route quality is the same at 0.25, 0.35 and 0.5 (K-town 7.5–7.6 vs 14.5 min; region 4.7–4.8 vs 8.6). The planner's mean error on optimized loop times:

    | Area | 0.25 | 0.5 |
    |---|---|---|
    | K-town | 0.72 min (always high) | 0.34 min |
    | Region | 1.14 min | 0.16 min |

    Its estimate for typical circling is optimistic at either value (K-town evenings: 13.8–14.7 predicted vs 17 realized), so the app understates the savings.
- **By hour** (`evaluate_routes.py --scenarios ...`, 20 destinations × 4 start times, 800 paired trips): minutes to the door, optimized vs typical circling. Overall 6.5 vs 12.7; optimized is faster or equal in 87% of trips.

  | Time | Optimized | Typical circling |
  |---|---|---|
  | Wed 8am | 3.4 | 6.1 |
  | Wed noon | 4.2 | 8.0 |
  | Wed 5pm | 6.1 | 12.9 |
  | Wed 7pm | 7.6 | 15.5 |
  | Wed 9pm | 7.9 | 16.7 |
  | Wed 11pm | 7.9 | 15.3 |
  | Sat 11am | 5.2 | 7.3 |
  | Sat 2pm | 6.8 | 12.7 |
  | Sat 7pm | 7.8 | 15.6 |
  | Sat 10pm | 7.9 | 16.5 |

  Predictions run slightly cautious at midday (e.g. 5.4 predicted vs 4.2 actual at Wed noon).
- **App (`build_app.py` → `data/parking_sim/app.html`):** the same optimizer, ported to JavaScript.
  - The app uses hourly chances, so its plans can differ slightly from the 15-minute Python ones.
  - Tap a destination, then choose day, arrival time, walk limit and stay.
  - It shows the plan with the route drawn and Google Maps links.
  - "How did it go?" saves trips (outcome, minutes searching, where you parked) to the page's `db` collection `trips`, for calibration.
- **Known gaps:**
  - Most lots have no posted price or hours, and lot occupancy is unknown.
  - A waypoint fixes the block but not the travel direction, which matters for right-side parking.
  - The loop starts at the intersection nearest the destination, not along the driver's approach.

## Model
- **Supply** (`supply.py`)
  - **Block faces.** OSM segments (`data/osm_roads.db`) are chained into blocks between intersections, split to ≤ 200 m, with one face per side. This does not depend on `web/graph.json`, so any bbox in LA works.
  - **Capacity.**
    - Start from (length − 7 m setback at each intersection) / 6.1 m.
    - Multiply by (1 − the share of curb that tickets show cannot be parked on). Ticketed driveways count 4.5 m, hydrants 9.1 m, red curb 6 m, and bus zones 25 m. Tickets are clustered by position along the curb. Corners already inside the setback are not subtracted again. The share is pooled over same-type streets within 500 m.
    - Multiply by a residual usable factor.
    - Inner faces of divided roads get zero capacity. These are parallel one-way carriageways of the same street within 25 m.
    - Subtracting tickets per face is right on average but noisy, because tickets are written where parking pressure is high. That is why the share is pooled.
  - **Rules.** Weekly 15-minute calendars:
    - posted street sweeping, by week of the month. Which side is swept on which day is learned per (route, street, house-number parity) from sweep tickets. LA puts odd numbers on the north side of E-W streets and the west side of N-S streets. The two parities fall on different days 97% of the time, and odd vs even days of the month agree 99%. The north/east-first convention is only the fallback. (Until 2026-10-28 the side came from where tickets snapped. Ticket coordinates sit near the centerline, so that put both sides of 41% of ticket-learned blocks on the same day.);
    - permit-district hours, from preferential-parking tickets pooled within 150 m;
    - meter paid hours, max stay, and tow-away windows;
    - posted time limits, from the hours over-time-limit tickets are written. The limit itself isn't in the tickets, so it is assumed to be 2 h. The optimizer and app skip faces whose legal stay at arrival is shorter than the planned stay (`--stay`);
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
  - Residents still unparked after the 800 m search plus one 15-minute retry park "elsewhere" until their next trip: a garage, farther away, or illegally. Without this, about 6.5k phantom circlers at 3am swamped every freed space.
  - It logs search effort and failures, plus non-residents turned away by permit rules.
- **Anchor** (`anchor.py`, `--anchor night`, the default)
  - The satellite-trained estimator (`scripts/curb/occupancy_model.py`) sets where resident cars live overnight.
  - A single scale makes the area's midday total match.
  - The daytime pattern comes from the simulated dynamics. `--anchor midday` instead matches each cell's midday directly; it validated slightly worse.

## Regions
`PARKSIM_REGION=<name>` writes every output to `data/parking_sim/<name>/` instead of the K-town default. Pass the region's `--bbox` to `supply.py` and run the street export with matching `BOUNDS`. `PARKSIM_PAR` limits how many simulated days `export.py` runs at once; lower it for big regions, since memory is the limit. `sim_area` (33.965–34.0725, −118.395 to −118.255) has 34,871 faces with parking and about 404k spaces.

Pages for a region: `PARKSIM_AREA_LABEL` sets the app's area line; `PARKSIM_TITLE` (page title and heading) and `PARKSIM_AREA_TEXT` set the viewer's. `PARKSIM_VAL_TABLE=region` swaps the viewer's K-town validation table for the held-out region table. For example:
```bash
PARKSIM_REGION=sim_area PARKSIM_AREA_LABEL="Central LA: Palms to Downtown, Manchester to 3rd St" python3 scripts/parking_sim/build_app.py
PARKSIM_REGION=sim_area PARKSIM_VAL_TABLE=region PARKSIM_TITLE="Central LA Curb Contention" PARKSIM_AREA_TEXT="Manchester Ave to 3rd St · Palms to Downtown" python3 scripts/parking_sim/build_viewer.py
```

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
| Street-sweeping tickets per route area (38 areas) | ρ 0.88; 32.5 cars left vs 36 tickets per event | — |
| Evening (6pm–2am) permit-district tickets vs simulated outsiders turned away (539 faces) | ρ 0.54 (0.45 without the evening multiplier) | capacity alone 0.09 |
| All-hours permit-district tickets vs outsiders turned away | ρ 0.32 | capacity alone 0.09 |
| Night double-parking tickets vs search effort (1,014 cells) | ρ 0.35 | satellite estimator 0.34, capacity −0.38 |
| Midday cars, 2010 aerial (701 cells), Wed / Sat | ρ 0.26 / 0.35 | satellite estimator 0.40 |
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

## Validation notes (2026-10-28, after sweeping sides, medians, give-up rule)
Ranges are across 3 seeds:

| Check | Result |
|---|---|
| Sweeping | ρ 0.84–0.86 |
| Night double parking | ρ 0.33–0.36 |
| Evening permit spillover | ρ 0.49–0.53 |
| Saturday midday | ρ 0.29–0.34 |
| Wednesday midday | ρ 0.10–0.13 |

Wednesday midday was 0.21 with the old north/east convention for sweeping sides (`PARKSIM_SWEEP_SIDE=convention`); nothing else changed. (Those ticket-derived sides were later found to put both sides of 41% of blocks on the same day, and were replaced by sides from address parity; see the region section.) The reason Wednesday looks worse: the 2010 aerial is a Monday around 12–2pm, not a Wednesday (see the region section below), so Wednesday's sweeps empty different sides than the photo shows.

Overnight stock for commercial cells was tested with `PARKSIM_NIGHT_EXTRA=0.25`, i.e. 25% of the satellite-predicted stock in cells without residential parcels counts as overnight residents. Saturday midday fell to 0.18, night to 0.29 and the sweeping level to 26 vs 36, with permit spillover about flat. Midday cars on those streets really do stand in for overnight demand (likely nearby apartment residents), so the default stays at 1.0.

## Region validation (2026-10-28)
`sim_area` was scored in three held-out boxes. Each box's satellite anchor was trained without it (`PARKSIM_HOLDOUT`). The boxes:
- Mid-City–Fairfax: 34.02–34.0725, −118.395 to −118.325
- Koreatown–Westlake: 34.02–34.0725, −118.325 to −118.255
- South LA: 33.965–34.02, full width

The K-town core column reruns the K-town-only build with the same metrics. Ranges are across 3 seeds, with sweep sides from address parity (below).

| Check | K-town core | Mid-City–Fairfax | Koreatown–Westlake | South LA |
|---|---|---|---|---|
| Sweeping tickets per route area, ρ | 0.84–0.86 (38 areas) | 0.85–0.91 (48) | 0.54–0.57 (57) | 0.79–0.83 (63) |
| Cars left per sweep vs tickets per sweep | 30–37 vs 36 | 34–35 vs 32 | 29–33 vs 38 | 45–52 vs 38 |
| Evening permit tickets vs outsiders turned away, ρ | 0.50–0.52 (539 faces) | 0.43–0.44 (1,363) | 0.72 (465) | 0.07–0.08 (196) |
| Night double parking vs occupancy, ρ | 0.29–0.33 | 0.25–0.26 | 0.24–0.26 | 0.50–0.53 |
| Same, beyond capacity per curb-km (partial ρ) | 0.07–0.11 | 0.18–0.19 | 0.21–0.22 | 0.30–0.34 |
| Midday cars, 2010 aerial, Monday 12–2pm, ρ | 0.25–0.30 | 0.22–0.30 | 0.14–0.18 | 0.36–0.37 |
| Satellite estimator alone, same cells | 0.39 | 0.33 | 0.41 | 0.47 |

What it shows:
- **Weekday of the aerial.** On blocks swept on one side only, the side swept Monday 12–2pm holds 0.55× the cars of the other side. Tuesday's sides, the mirror image, hold 2.1×; other days and hours are near 1. So the photo is a Monday around 12–2pm. Only the southeast corner (east of −118.30, south of 34.02) shows no dip, so it was probably flown on another day. Monday 12–2pm now leads the midday check.
- **Night.** Capacity per curb-km alone correlates −0.16 to −0.55 with double parking, likely because enforcement-heavy streets collect both exclusion tickets and double-parking tickets. The partial ρ is what the simulator adds beyond it. In K-town it is small: nights are saturated (98% median occupancy) and variation is mostly capacity. Partials use average ranks, since most cells tie at zero tickets.
- **Permit spillover outside K-town.** Mid-City–Fairfax and Koreatown–Westlake hold up. South LA does not. Its 196 faces fall in three clusters: Leimert Park is under-predicted, Crenshaw & Exposition over-predicted, and near Manchester nothing is predicted at all. The Crenshaw over-prediction comes from strip-mall businesses that have their own lots; neither the places data nor the parcels say so, and OSM maps few lots there. Satellite off-street car counts near a place track density, not lots: K-town scores higher than South LA. So that fix was not used.
- **Weekday middays** trail the satellite estimator they are anchored to, most in Koreatown–Westlake. The daytime dynamics (who leaves, where workers and visitors park, overflow) blur the resident pattern. Saturday's higher ρ is partly structural: with residents home, the pattern is mostly the anchor's.
- **What blurs weekday middays** (Koreatown–Westlake, Monday, components run alone or in pairs). Midday ρ vs the aerial: residents alone 0.32, residents + visitors 0.27, residents + commuters 0.20, everything 0.17. Commuters alone sit on a few job-dense blocks, such as the Wilshire Center towers. Visitors alone run against the photo (−0.20). Two one-seed tests across all four boxes changed nothing beyond seed noise:
  - Halving daytime visitors, with evenings unchanged (`PARKSIM_VISITOR_SCALE=0.125`, `PARKSIM_VISITOR_EVENING=6`).
  - A 2% commuter curb share (`PARKSIM_COMMUTER_CURB=0.02`), which also pushed sweeping levels further from the tickets (South LA 52 vs 38).
  The night anchor rescales residents so the midday total still matches, so cutting other demand mostly adds residents back. The blur comes from overall saturation and overflow, not one component. Defaults stay.
- **Search-path optimizer, region-wide** (`evaluate_routes.py --dests 40`, probes in a fresh seed; final data with parity sweep sides and P_SCALE 0.5): 4.7 vs 8.7 min to the door over 1,200 paired trips; optimized faster or equal in 83%. K-town (`--dests 30`): 7.6 vs 14.8 min over 900, 92%. Planned loop times are within about 0.5 min of the probes, except K-town Sat 1pm (1 min low).

**Sweep sides from address parity (2026-10-28).** Snapped tickets agree with the parity convention only 59% of the time, so the old snapped-face sides were mostly noise.
- Keyed by (route, street, parity): blocks with both sides swept the same day fell from 35% to 10% in K-town, and from 41% to 4% among ticket-learned blocks. Ticket-learned blocks rose from 1,444 to 3,145.
- On the 2010 aerial, the side swept Monday holds 0.68× the other side's cars, vs 0.77× with the old sides.
- One seed per box, old → new:

  | Box | Monday midday | Wednesday midday | Sweeping |
  |---|---|---|---|
  | K-town core | 0.26 → 0.30 | 0.16 → 0.20 | 0.86 → 0.86 |
  | Mid-City–Fairfax | 0.29 → 0.30 | — | 0.89 → 0.91 |
  | Koreatown–Westlake | 0.18 → 0.18 | — | 0.55 → 0.55 |
  | South LA | 0.40 → 0.37 | — | 0.78 → 0.81 |

- Night and permit checks barely move, since they pool both sides. The table above is rerun with this fix.

To reproduce one box: `PARKSIM_REGION=sim_area PARKSIM_HOLDOUT=33.965,34.02,-118.395,-118.255 PARKSIM_OUT=/tmp/v PARKSIM_PAR=3 python3 scripts/parking_sim/validate.py` (about 3 min, 4 GB). Add `PARKSIM_REUSE=1` to rescore saved days.

## Known gaps
- North K-town (3rd St to Beverly) needs parcel data. `dtla_parcels.csv` ends at 34.073.
- Swept sides come from sweep tickets' house-number parity (odd = north / west). Streets with too few tickets, or no OSM name, fall back to the convention (north or east side takes the earlier day). That is 8,001 of 31,187 swept faces in `sim_area`.
- The 2010 aerial reads as a Monday ~12–2pm (by the parity sides, and by the old ones), but Esri's metadata says May 25, a Tuesday. If the metadata is right, the parity convention would have to be reversed. Landmarks (LACMA 5905 and the Petersen 6060 on Wilshire; Disney Hall at 111 S Grand) and the tickets' own slight lean (59% vs 41%) both say it isn't.
- Inner faces of divided roads get zero capacity. That is right for medians, but a few wide boulevards with parking on the inner side lose it.
- The region uses K-town's calibration (visitor scale, night scale); P_SCALE was checked region-wide. Held-out checks above say it transfers, except South LA permit spillover.
- The anchor's reference day is still Wednesday 11–13h. The aerial is a Monday 12–2pm; the levels differ little (about 35 vs 33 cars/km).
- Evenings rest on ticket proxies; there are no ground counts.
- Weekday midday spatial accuracy trails the satellite estimator (0.14–0.40 vs 0.33–0.47 held out).
