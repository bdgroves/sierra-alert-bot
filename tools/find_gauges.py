"""List NWPS river gauges in the Sierra box that have flood stages, for picking GAUGES in bot/main.py."""
import requests

url = "https://api.water.noaa.gov/nwps/v1/gauges"
params = {"bbox.xmin": -121.0, "bbox.ymin": 36.0, "bbox.xmax": -117.5, "bbox.ymax": 41.5, "srid": "EPSG_4326"}
r = requests.get(url, params=params, timeout=60, headers={"User-Agent": "SierraNevadaWX/2.0"})
r.raise_for_status()
gs = r.json().get("gauges", [])
with open("logs/gauge_probe.txt", "w") as f:
    f.write(f"{len(gs)} gauges\n")
    for g in gs:
        name = g.get("name", "")
        if any(w in name.lower() for w in ("tuolumne", "merced", "american", "truckee", "kings", "stanislaus",
                                             "carson", "walker", "yuba", "feather", "san joaquin", "kern", "kaweah", "mokelumne")):
            st = g.get("status", {}).get("observed", {})
            f.write(f"{g.get('lid')}\t{name}\t{st.get('floodCategory')}\t{st.get('primary')}\n")
print(open("logs/gauge_probe.txt").read())
