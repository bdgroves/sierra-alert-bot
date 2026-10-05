"""
Sierra Nevada Alert Bot (@SierraNevadaWX)
=========================================
Posts only what matters in the Sierra Nevada:

  * NWS warnings for Sierra zones (not watches, advisories or statements).
    One post per hazard: updates and extensions of the same event never
    repost, and zones sharing a hazard in one run are combined.
  * Wildfires: a new fire at 100+ acres (10+ in Tuolumne County), then again
    at 1k, 5k, 10k, 25k, 50k, 100k and 250k acres. CAL FIRE for California,
    NIFC for Nevada, so one fire is never posted twice from two feeds.
  * Spotter reports that matter: tornado, flash flood, avalanche, 1"+ hail,
    75+ mph gusts, 2 ft+ of snow.
  * Smoke: PM2.5 AQI 151+ (Unhealthy) at mountain towns, once per level per day.
  * River gauges at minor flood or worse, once per level per day.
  * Earthquakes M3.5+.

New Tuolumne County fires also get a Wikipedia draft row, which goes to a
GitHub issue in this repo (not to X).
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
from core import (  # noqa: E402
    DRY_RUN, PACIFIC, Cache, Poster, alert_end, alert_key, fetch_nws, fmt_pacific,
    format_alert, log, now_utc,
)

UA = "SierraNevadaWX/2.0 (github.com/bdgroves/sierra-alert-bot)"
SIERRA_BBOX = (-121.0, 36.0, -117.5, 41.5)  # W, S, E, N
TAGS = "#SierraNevada"

# ── NWS ───────────────────────────────────────────────────────────────────────
# Zone names (from areaDesc) that are Sierra Nevada. Matching on zone names,
# not county names, keeps out the Valley halves of Fresno, Tulare and Kern
# counties, and the old "nevada" match that caught all of the state of Nevada.
SIERRA_WORDS = (
    "sierra", "tahoe", "yosemite", "sequoia", "kings canyon", "mono", "mammoth",
    "owens valley", "white mountains", "tuolumne", "mariposa", "calaveras",
    "amador", "el dorado", "placer", "plumas", "lassen", "alpine", "truckee",
    "greater reno", "carson", "minden",
)
SIERRA_EVENTS = {
    "Red Flag Warning", "Fire Warning", "Evacuation Immediate",
    "Civil Emergency Message", "Winter Storm Warning", "Blizzard Warning",
    "Ice Storm Warning", "High Wind Warning", "Extreme Wind Warning",
    "Flash Flood Warning", "Flood Warning", "Excessive Heat Warning",
    "Extreme Heat Warning", "Extreme Cold Warning", "Avalanche Warning",
    "Severe Thunderstorm Warning", "Tornado Warning", "Dust Storm Warning",
}
EMOJI = {
    "Red Flag": "🔥", "Fire": "🔥", "Evacuation": "🚨", "Civil": "🚨",
    "Winter Storm": "❄️", "Blizzard": "🌨️", "Ice Storm": "🧊", "Wind": "💨",
    "Flash Flood": "🌊", "Flood": "💧", "Heat": "🌡️", "Cold": "🥶",
    "Avalanche": "🏔️", "Thunderstorm": "⛈️", "Tornado": "🌪️", "Dust": "🌫️",
}


def sierra_zones(p: dict) -> list[str]:
    names = [a.strip() for a in (p.get("areaDesc") or "").split(";") if a.strip()]
    return [n for n in names if any(w in n.lower() for w in SIERRA_WORDS)]


def run_alerts(poster: Poster) -> None:
    alerts = []
    for area in ("CA", "NV"):
        try:
            alerts += fetch_nws(area, UA)
        except Exception as e:
            log.error(f"NWS {area} fetch failed: {e}")
    groups: dict[str, list[dict]] = defaultdict(list)
    seen_ids = set()
    for p in alerts:
        if p.get("id") in seen_ids:
            continue
        seen_ids.add(p.get("id"))
        ev = p.get("event", "")
        if ev not in SIERRA_EVENTS:
            continue
        end = alert_end(p)
        if end and end < now_utc():
            continue
        zones = sierra_zones(p)
        if not zones:
            continue
        key = alert_key(p, "sierra")
        if not key:
            continue
        p["_key"], p["_zones"] = key, zones
        groups[ev].append(p)
    log.info(f"NWS: {sum(len(v) for v in groups.values())} Sierra warning products "
             f"in {len(groups)} hazards")
    for ev, plist in groups.items():
        fresh = [p for p in plist if p["_key"] not in poster.cache]
        if not fresh:
            continue
        emoji = next((e for k, e in EMOJI.items() if k in ev), "⚠️")
        areas = [z for p in fresh for z in p["_zones"]]
        text = format_alert(ev, emoji, "", areas, fresh, TAGS)
        keys = [p["_key"] for p in fresh]
        poster.post("nws", text, keys[0], also_mark=tuple(keys[1:]))


# ── Wildfires ─────────────────────────────────────────────────────────────────
CALFIRE_URL = "https://www.fire.ca.gov/umbraco/api/IncidentApi/GeoJsonList?inactive=false"
NIFC_URL = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
            "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
FIRE_NEW_ACRES = 100
TUOLUMNE_NEW_ACRES = 10
MILESTONES = (1_000, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000)


def in_bbox(lon, lat) -> bool:
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return False
    return SIERRA_BBOX[0] <= lon <= SIERRA_BBOX[2] and SIERRA_BBOX[1] <= lat <= SIERRA_BBOX[3]


def fetch_calfire() -> list[dict]:
    import requests
    r = requests.get(CALFIRE_URL, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    data = r.json()
    feats = data.get("features", []) if isinstance(data, dict) else data
    fires = []
    for f in feats:
        p = f.get("properties", {}) or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [p.get("Longitude"), p.get("Latitude")]
        name = (p.get("Name") or "").strip()
        kind = f"{p.get('Type') or ''} {name}".lower()
        if p.get("Final") or "prescribed" in kind or " rx" in f" {kind}":
            continue
        if not in_bbox(coords[0], coords[1]):
            continue
        fires.append({
            "id": str(p.get("UniqueId") or f"{name}-{p.get('County')}").lower(),
            "name": name.title() or "Unknown",
            "acres": float(p.get("AcresBurned") or 0),
            "contained": p.get("PercentContained"),
            "county": p.get("County") or p.get("Counties") or "",
            "state": "CA",
            "url": (f"https://www.fire.ca.gov{p['Url']}" if str(p.get("Url", "")).startswith("/")
                    else p.get("Url") or "https://www.fire.ca.gov/incidents"),
            "started": p.get("Started") or p.get("StartedDateOnly"),
            "agency": p.get("AdminUnit") or "CAL FIRE",
            "cause": p.get("Cause") or "",
        })
    log.info(f"CAL FIRE: {len(fires)} active Sierra incidents")
    return fires


def fetch_nifc_nevada() -> list[dict]:
    import requests
    w, s, e, n = SIERRA_BBOX
    params = {
        "where": "attr_IncidentTypeCategory = 'WF' AND attr_POOState = 'US-NV'",
        "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "false",
        "outFields": "poly_IncidentName,poly_GISAcres,attr_PercentContained,"
                     "attr_POOCounty,attr_UniqueFireIdentifier,attr_IrwinID",
        "f": "json",
    }
    r = requests.get(NIFC_URL, params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    fires = []
    for f in r.json().get("features", []):
        a = f.get("attributes", {})
        fid = a.get("attr_UniqueFireIdentifier") or a.get("poly_IncidentName")
        fires.append({
            "id": str(fid).lower(),
            "name": (a.get("poly_IncidentName") or "Unknown").title(),
            "acres": float(a.get("poly_GISAcres") or 0),
            "contained": a.get("attr_PercentContained"),
            "county": a.get("attr_POOCounty") or "",
            "state": "NV",
            "url": "https://inciweb.wildfire.gov/",
        })
    log.info(f"NIFC: {len(fires)} Nevada fires in the Sierra box")
    return fires


def fire_text(f: dict, milestone: int | None) -> str:
    acres = f"{f['acres']:,.0f} acres"
    cont = f"{int(f['contained'])}% contained" if f.get("contained") is not None else "containment unknown"
    county = f"{f['county']} County, {f['state']}" if f["county"] else f["state"]
    if milestone:
        head = f"🔥 {f['name']} Fire passes {milestone:,} acres"
    else:
        head = f"🔥 New fire: {f['name']} Fire"
    return f"{head}\n{acres} · {cont}\n{county}\n{f['url']}\n{TAGS} #wildfire"


def run_fires(poster: Poster) -> None:
    fires = []
    for fetch in (fetch_calfire, fetch_nifc_nevada):
        try:
            fires += fetch()
        except Exception as e:
            log.error(f"{fetch.__name__} failed: {e}")
    for f in sorted(fires, key=lambda f: -f["acres"]):
        if f.get("contained") is not None and float(f["contained"]) >= 100:
            continue
        tuolumne = "tuolumne" in f["county"].lower()
        start = TUOLUMNE_NEW_ACRES if tuolumne else FIRE_NEW_ACRES
        if f["acres"] < start:
            continue
        base = f"fire:{f['id']}"
        passed = [m for m in MILESTONES if f["acres"] >= m]
        mkeys = tuple(f"{base}:{m}" for m in passed)
        if base not in poster.cache:
            # First sighting: one post, and every milestone already passed is covered.
            if poster.post("fire", fire_text(f, None), base, also_mark=mkeys) and tuolumne:
                wiki_draft(f, poster)
        elif passed and mkeys[-1] not in poster.cache:
            poster.post("fire", fire_text(f, passed[-1]), mkeys[-1], also_mark=mkeys[:-1])


def wiki_draft(f: dict, poster: Poster) -> None:
    """New Tuolumne fire → a GitHub issue with a draft row for the Wikipedia list."""
    if poster.cache.bootstrap or DRY_RUN:
        return
    started = None
    try:
        started = datetime.fromisoformat(str(f.get("started")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        pass
    dts = (f"{{{{dts|{started.year}|{started.month}|{started.day}|format=md}}}}"
           if started else "{{dts|?|?|?|format=md}}")
    access = now_utc().astimezone(PACIFIC).strftime("%B %-d, %Y")
    row = "\n".join([
        "|-", f"|{f['name']}", "|Near [location], Tuolumne County",
        f"|{{{{nts|{round(f['acres'])}}}}}", f"|{dts}", "|",
        f"|{f.get('cause') or 'Under investigation'}. Managed by {f.get('agency')}. [Add notes.]",
        f"|<ref>{{{{cite web |title={f['name']} Fire |url={f['url']} "
        f"|publisher=CAL FIRE |access-date={access}}}}}</ref>",
    ])
    body = (f"New Tuolumne County fire: **{f['name']} Fire**, {f['acres']:,.0f} acres.\n\n"
            f"Draft row for the Tuolumne County wildfires list on Wikipedia:\n\n```\n{row}\n```\n\n"
            f"Source: {f['url']}")
    with open(os.path.join("logs", "tuolumne_drafts.md"), "a", encoding="utf-8") as fh:
        fh.write(f"\n## {f['name']} Fire ({access})\n\n```\n{row}\n```\n")
    if os.environ.get("GH_TOKEN"):
        try:
            subprocess.run(["gh", "issue", "create", "--title",
                            f"Wikipedia draft: {f['name']} Fire (Tuolumne County)",
                            "--body", body], check=True, timeout=60)
            log.info("Opened Wikipedia draft issue")
        except Exception as e:
            log.error(f"Could not open draft issue: {e}")


# ── Spotter reports ───────────────────────────────────────────────────────────
LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.php"


def lsr_matters(p: dict) -> tuple[str, str] | None:
    t = (p.get("typetext") or "").upper()
    try:
        mag = float(p.get("magnitude") or 0)
    except ValueError:
        mag = 0
    if "TORNADO" in t:
        return "🌪️", "Tornado"
    if "FLASH FLOOD" in t or "DEBRIS FLOW" in t:
        return "🌊", t.title()
    if "AVALANCHE" in t:
        return "🏔️", "Avalanche"
    if "HAIL" in t and mag >= 1.0:
        return "🧊", f'{mag:g}" hail'
    if ("GUST" in t or "WND" in t or "WIND" in t) and mag >= 75:
        return "💨", f"{mag:.0f} mph gust"
    if "SNOW" in t and mag >= 24:
        return "❄️", f'{mag:g}" of snow'
    return None


def run_lsr(poster: Poster) -> None:
    import requests
    end = now_utc()
    params = {"sts": (end - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "ets": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        r = requests.get(LSR_URL, params=params, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception as e:
        log.error(f"LSR fetch failed: {e}")
        return
    n = 0
    for f in feats:
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        if not in_bbox(lon, lat):
            continue
        p = f.get("properties", {})
        hit = lsr_matters(p)
        if not hit:
            continue
        n += 1
        emoji, label = hit
        key = f"lsr:{p.get('wfo')}:{p.get('valid')}:{p.get('typetext')}:{p.get('city')}"
        when = ""
        try:
            when = " · " + fmt_pacific(datetime.fromisoformat(str(p["valid"]).replace("Z", "+00:00"))
                                       .replace(tzinfo=timezone.utc), with_day=True)
        except Exception:
            pass
        remark = " ".join((p.get("remark") or "").split())[:140]
        text = (f"{emoji} Spotter report: {label}\n{p.get('city', '')}, {p.get('county', '')} Co. "
                f"{p.get('state') or p.get('st') or ''}{when}\n{remark}\n{TAGS}")
        poster.post("lsr", text, key)
    log.info(f"LSR: {n} significant Sierra reports in 12 h")


# ── Smoke ─────────────────────────────────────────────────────────────────────
AIRNOW_URL = "https://www.airnowapi.org/aq/observation/latLong/current/"
AQI_MIN = 151
SMOKE_POINTS = [
    (38.9577, -119.9229, "South Lake Tahoe"), (39.3280, -120.1833, "Truckee"),
    (37.6487, -118.9720, "Mammoth Lakes"), (37.7456, -119.5936, "Yosemite"),
    (37.9841, -120.3822, "Sonora"), (36.4864, -118.8259, "Sequoia NP"),
    (39.5296, -119.8138, "Reno"), (37.3636, -118.3951, "Bishop"),
]


def aqi_level(aqi: int) -> tuple[str, str]:
    if aqi >= 301:
        return "☠️", "Hazardous"
    if aqi >= 201:
        return "🚨", "Very Unhealthy"
    return "😷", "Unhealthy"


def run_smoke(poster: Poster) -> None:
    import requests
    key = os.environ.get("AIRNOW_API_KEY")
    if not key:
        log.info("AIRNOW_API_KEY not set; skipping smoke check")
        return
    today = now_utc().astimezone(PACIFIC).strftime("%Y%m%d")
    for lat, lon, town in SMOKE_POINTS:
        try:
            r = requests.get(AIRNOW_URL, timeout=15, params={
                "format": "application/json", "latitude": lat, "longitude": lon,
                "distance": 25, "API_KEY": key})
            obs = r.json() if r.ok else []
        except Exception as e:
            log.warning(f"AirNow {town}: {e}")
            continue
        for o in obs if isinstance(obs, list) else []:
            if o.get("ParameterName") != "PM2.5" or (o.get("AQI") or 0) < AQI_MIN:
                continue
            aqi = int(o["AQI"])
            emoji, label = aqi_level(aqi)
            area = o.get("ReportingArea") or town
            text = (f"{emoji} Smoke: {label} air near {town}\n"
                    f"PM2.5 AQI {aqi} ({area}) as of {o.get('HourObserved', '?')}:00\n"
                    f"https://fire.airnow.gov/\n{TAGS} #smoke")
            poster.post("smoke", text, f"aq:{area}:{label}:{today}")
            break


# ── River gauges ──────────────────────────────────────────────────────────────
NWPS_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{}"
GAUGES = [
    ("mdsc1", "Tuolumne River at Modesto"), ("hchy1", "Tuolumne River at Hetch Hetchy"),
    ("hisc1", "Merced River at Happy Isles"), ("merc1", "Merced River near Merced"),
    ("foac1", "American River at Fair Oaks"), ("rnkn2", "Truckee River at Reno"),
    ("pnfc1", "Kings River at Pine Flat"),
]
FLOOD_LEVELS = ("minor", "moderate", "major")


def gauge_status(data: dict) -> tuple[str, float | None, str]:
    """(flood category, stage ft, time) from an NWPS gauge record, any shape."""
    obs = (data.get("status") or {}).get("observed") or data.get("observed") or {}
    cat = str(obs.get("floodCategory") or "").lower().replace("_", " ")
    stage = obs.get("primary")
    if isinstance(stage, dict):
        stage = stage.get("value")
    try:
        stage = float(stage) if stage is not None else None
    except (TypeError, ValueError):
        stage = None
    if stage is not None and stage < -900:  # NWPS uses -999 for missing
        stage = None
    return cat, stage, str(obs.get("validTime") or obs.get("timestamp") or "")


def run_gauges(poster: Poster) -> None:
    import requests
    today = now_utc().astimezone(PACIFIC).strftime("%Y%m%d")
    for gid, name in GAUGES:
        try:
            r = requests.get(NWPS_URL.format(gid), headers={"User-Agent": UA}, timeout=15)
            r.raise_for_status()
            cat, stage, _ = gauge_status(r.json())
        except Exception as e:
            log.warning(f"Gauge {gid}: {e}")
            continue
        log.info(f"Gauge {gid}: {cat or 'n/a'} {stage if stage is not None else ''}")
        if cat not in FLOOD_LEVELS:
            continue
        emoji = "🌊🌊" if cat != "minor" else "🌊"
        st = f" at {stage:.1f} ft" if stage is not None else ""
        text = (f"{emoji} {cat.title()} flooding: {name}{st}\n"
                f"https://water.noaa.gov/gauges/{gid}\n{TAGS} #flooding")
        poster.post("gauge", text, f"gauge:{gid}:{cat}:{today}")


# ── Earthquakes ───────────────────────────────────────────────────────────────
EQ_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
EQ_MIN_MAG = 3.5


def run_quakes(poster: Poster) -> None:
    import requests
    w, s, e, n = SIERRA_BBOX
    params = dict(format="geojson", minmagnitude=EQ_MIN_MAG, orderby="time",
                  starttime=(now_utc() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S"),
                  minlatitude=s, maxlatitude=n, minlongitude=w, maxlongitude=e)
    try:
        r = requests.get(EQ_URL, params=params, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        feats = r.json().get("features", [])
    except Exception as e:
        log.error(f"USGS fetch failed: {e}")
        return
    log.info(f"USGS: {len(feats)} quakes M{EQ_MIN_MAG}+ in 24 h")
    for f in sorted(feats, key=lambda f: f["properties"].get("time", 0)):
        p = f["properties"]
        if p.get("type") != "earthquake":
            continue
        mag = p.get("mag") or 0
        depth = (f.get("geometry", {}).get("coordinates") or [0, 0, 0])[2]
        when = fmt_pacific(datetime.fromtimestamp(p["time"] / 1000, timezone.utc), with_day=True)
        felt = f" · {p['felt']} felt reports" if p.get("felt") else ""
        icon = "🚨" if mag >= 5 else "📳"
        text = (f"{icon} M{mag:.1f} earthquake — {p.get('place', 'Sierra Nevada')}\n"
                f"{when} · {depth:.0f} km deep{felt}\n{p.get('url', '')}\n{TAGS} #earthquake")
        poster.post("quake", text, f"eq:{f.get('id')}")


def main() -> int:
    log.info("=== Sierra Nevada Alert Bot ===")
    cache = Cache()
    poster = Poster(cache)
    for step in (run_alerts, run_fires, run_lsr, run_smoke, run_gauges, run_quakes):
        try:
            step(poster)
        except Exception as e:  # one broken feed never stops the others
            log.exception(f"{step.__name__} crashed: {e}")
    return poster.finish()


if __name__ == "__main__":
    sys.exit(main())
