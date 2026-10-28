#!/usr/bin/env python3
"""Polite Wayback z19 fetch: one request at a time, stop on the first 403/429, record each tile's real release.

Usage: python3 scripts/curb/fetch_wayback_slow.py <release> <lat0,lat1,lon0,lon1> [seconds_between=1.0]
Esri blocked Python clients after ~800 tiles at 6-way concurrency (2026-10-27) and re-blocked after 2 tiles at 1/s
once lifted. Do not switch clients to dodge a block; slow down or use the Wayback tile-package export.
"""
import sys, math, time, csv, urllib.request, urllib.error
from common import TILES_DIR
rel=sys.argv[1]; la0,la1,lo0,lo1=map(float,sys.argv[2].split(',')); gap=float(sys.argv[3]) if len(sys.argv)>3 else 1.0
Z=19; N=2**Z
tx=lambda lon: int((lon+180)/360*N)
ty=lambda lat: int((1-math.log(math.tan(math.radians(lat))+1/math.cos(math.radians(lat)))/math.pi)/2*N)
out=TILES_DIR/rel; out.mkdir(parents=True,exist_ok=True)
log=out/'_source_release.csv'; new=not log.exists(); lf=open(log,'a',newline=''); w=csv.writer(lf)
if new: w.writerow(['ty','tx','source_release'])
URL=f'https://wayback.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{rel}/{Z}/{{y}}/{{x}}'
jobs=[(x,y) for x in range(tx(lo0),tx(lo1)+1) for y in range(ty(la1),ty(la0)+1) if not (out/f'{y}_{x}.jpg').exists()]
print(f"{len(jobs)} tiles to fetch, ~{len(jobs)*gap/60:.0f} min at {gap}s spacing", flush=True)
for i,(x,y) in enumerate(jobs):
    t=time.time()
    try:
        with urllib.request.urlopen(URL.format(x=x,y=y),timeout=30) as r:
            (out/f'{y}_{x}.jpg').write_bytes(r.read()); w.writerow([y,x,r.url.split('/tile/')[1].split('/')[0]])
    except urllib.error.HTTPError as e:
        if e.code in (403,429): print(f"stopping on HTTP {e.code} after {i} tiles; resume later", flush=True); break
        print(f"HTTP {e.code} on {y}_{x}, skipping", flush=True)
    except Exception as e: print(f"{type(e).__name__} on {y}_{x}, skipping", flush=True)
    if (i+1)%200==0: lf.flush(); print(f"{i+1}/{len(jobs)}", flush=True)
    time.sleep(max(0,gap-(time.time()-t)))
lf.close(); print("finished", flush=True)
