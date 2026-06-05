"""
Sierra Nevada Alert Bot
Monitors NWS weather alerts and USGS earthquakes for the Sierra Nevada
and posts to @SierraNevadaWX on X/Twitter.
"""

import os
import sys
import json
import time
import logging
import hashlib
import requests
import tweepy
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
NWS_ALERTS_URL   = "https://api.weather.gov/alerts/active"
USGS_EQ_URL      = "https://earthquake.usgs.gov/fdsnws/event/1/query"
PACIFIC          = ZoneInfo("America/Los_Angeles")
MAX_TWEET_LEN    = 280
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "120"))
CACHE_FILE       = os.environ.get("CACHE_FILE", "posted_ids.json")
DRY_RUN          = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")


# Minimum fire size to report (acres)
FIRE_MIN_ACRES     = 10.0

# Growth threshold to re-tweet an existing fire (fraction — 0.5 = 50% bigger)
FIRE_GROWTH_FACTOR = 0.5

# NIFC WFIGS live fire perimeters — Sierra bbox filtered
# CAL FIRE GeoJSON API — California incidents, updates every ~15 min
CALFIRE_URL = "https://www.fire.ca.gov/umbraco/api/IncidentApi/GeoJsonList?inactive=false"

NIFC_FIRE_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)

# Sierra bbox as separate coords for ESRI API
# Sierra Nevada bounding box
# Southern boundary raised to 36.0 to exclude southern Kern County / Mojave
# Eastern boundary kept at -117.5 to include White Mountains / Inyo
SIERRA_BBOX = (-121.0, 36.0, -117.5, 41.5)


# IEM Local Storm Reports API
IEM_LSR_URL = "https://mesonet.agron.iastate.edu/geojson/lsr.php"

# LSR type → (emoji, label, season)
# season: 'summer', 'winter', 'any'
LSR_TYPES = {
    # Summer / convective
    "H":  ("🧊", "Hail",              "summer"),
    "G":  ("💨", "Wind Gust",         "summer"),
    "D":  ("💨", "Wind Damage",       "summer"),
    "T":  ("🌪️", "Tornado",           "summer"),
    "F":  ("🌪️", "Funnel Cloud",      "summer"),
    "W":  ("🌊", "Water Spout",       "summer"),
    "x":  ("⛈️", "Heavy Rain",        "summer"),
    "E":  ("🌊", "Flash Flood",       "summer"),
    "f":  ("🌊", "Flood",             "summer"),
    "M":  ("🌊", "Marine Hail",       "summer"),
    "L":  ("⚡", "Lightning",         "summer"),
    "A":  ("🌊", "Avalanche",         "any"),
    "U":  ("⚠️", "High Surf",         "summer"),
    # Winter
    "S":  ("❄️", "Heavy Snow",        "winter"),
    "s":  ("🌨️", "Snow Squall",       "winter"),
    "Z":  ("🧊", "Freezing Rain",     "winter"),
    "I":  ("🧊", "Ice Storm",         "winter"),
    "i":  ("🧊", "Black Ice",         "winter"),
    "B":  ("🌨️", "Blowing Snow",      "winter"),
    "R":  ("🥶", "Sleet",             "winter"),
    # Any season
    "q":  ("💨", "High Wind",         "any"),
    "N":  ("🌫️", "Dense Fog",         "any"),
    "P":  ("🌡️", "Extreme Heat",      "any"),
    "C":  ("🥶", "Extreme Cold",      "any"),
    "2":  ("🌫️", "Dense Smoke",       "any"),
    "O":  ("⚠️", "Other",             "any"),
}


# AirNow API — air quality monitoring
# Use latLong endpoint with multiple Sierra monitoring points instead of bbox
# (bbox endpoint is unreliable; latLong with distance is more robust)
AIRNOW_MIN_AQI = 101  # Unhealthy for Sensitive Groups threshold

# Key Sierra Nevada monitoring locations [lat, lon, name]
SIERRA_MONITOR_POINTS = [
    (38.9577, -119.9229, "South Lake Tahoe"),
    (37.6487, -118.9720, "Mammoth Lakes"),
    (37.9780, -119.8825, "Yosemite Valley"),
    (36.5785, -118.2923, "Sequoia NP"),
    (39.7596, -121.8374, "Chico/Foothills"),
    (38.5816, -121.4944, "Sacramento"),
    (39.1638, -120.1422, "Truckee"),
    (36.7783, -119.4179, "Fresno"),
]

# AQI category → emoji
def aqi_emoji(aqi: int) -> str:
    if aqi >= 301: return "☠️"
    if aqi >= 201: return "🚨"
    if aqi >= 151: return "😷"
    return "⚠️"

def aqi_label(aqi: int) -> str:
    if aqi >= 301: return "Hazardous"
    if aqi >= 201: return "Very Unhealthy"
    if aqi >= 151: return "Unhealthy"
    return "Unhealthy for Sensitive Groups"




# NWS National Water Prediction Service API — faster and more reliable than USGS direct
NWPS_GAUGE_URL = "https://api.water.noaa.gov/nwps/v1/gauges/{gauge_id}"

# NWS gauge IDs for Sierra Nevada rivers
# Format: (nwps_id, name, river)
# Flood stages pulled dynamically from the API itself — no hardcoding needed!
SIERRA_NWPS_GAUGES = [
    ("mdsc1",  "Modesto",              "Tuolumne River"),
    ("hchy1",  "Hetch Hetchy",         "Tuolumne River"),
    ("hisc1",  "Happy Isles/Yosemite", "Merced River"),
    ("merc1",  "Merced",               "Merced River"),
    ("foac1",  "Fair Oaks",            "American River"),
    ("rnkn2",  "Reno",                 "Truckee River"),
    ("pnfc1",  "Pine Flat",            "Kings River"),
]

# ── Sierra Nevada NWS forecast zones ─────────────────────────────────────────
# Covers the full Sierra Nevada mountain range and surrounding foothills
# Source: api.weather.gov/zones
SIERRA_NWS_ZONES = [
    # California Sierra zones
    "CAZ061",  # Northern Sierra Nevada above 7000 feet
    "CAZ062",  # Northern Sierra Nevada 5000-7000 feet
    "CAZ063",  # Northern Sierra Nevada Foothills
    "CAZ064",  # Lake Tahoe Area
    "CAZ065",  # Tahoe Basin
    "CAZ066",  # Central Sierra Nevada above 7000 feet
    "CAZ067",  # Central Sierra Nevada 5000-7000 feet
    "CAZ068",  # Central Sierra Nevada Foothills
    "CAZ069",  # Southern Sierra Nevada above 7000 feet
    "CAZ070",  # Southern Sierra Nevada 5000-7000 feet
    "CAZ071",  # Southern Sierra Nevada Foothills
    "CAZ072",  # Kern County Mountains
    # Nevada Eastern Sierra zones
    "NVZ001",  # Washoe County (Reno/Tahoe)
    "NVZ002",  # Storey/Lyon/Douglas Counties
    "NVZ003",  # Carson City area
    "NVZ018",  # Eastern Sierra slopes NV
    # County-level zones for Sierra counties
    "CAC017",  # El Dorado County
    "CAC019",  # Fresno County (Sierra portion)
    "CAC031",  # Kings Canyon/Sequoia area
    "CAC039",  # Madera County
    "CAC051",  # Mono County
    "CAC057",  # Nevada County
    "CAC061",  # Placer County
    "CAC067",  # Shasta County
    "CAC091",  # Sierra County
    "CAC101",  # Sutter/Yuba
    "CAC105",  # Tulare County (Sierra)
    "CAC109",  # Tuolumne County
    "CAC113",  # Yolo County
]



# Minimum earthquake magnitude to post
EQ_MIN_MAGNITUDE = 2.5

# ── Tweet emoji map ───────────────────────────────────────────────────────────
EVENT_EMOJI = {
    "Tornado Warning":              "🌪️",
    "Tornado Watch":                "🌪️",
    "Severe Thunderstorm Warning":  "⛈️",
    "Severe Thunderstorm Watch":    "⛈️",
    "Flash Flood Warning":          "🌊",
    "Flash Flood Watch":            "🌊",
    "Flash Flood Advisory":         "💧",
    "Flood Warning":                "💧",
    "Flood Watch":                  "💧",
    "Winter Storm Warning":         "❄️",
    "Winter Storm Watch":           "❄️",
    "Blizzard Warning":             "🌨️",
    "Ice Storm Warning":            "🧊",
    "High Wind Warning":            "💨",
    "High Wind Watch":              "💨",
    "Wind Advisory":                "💨",
    "Red Flag Warning":             "🔥",
    "Fire Weather Watch":           "🔥",
    "Extreme Fire Danger":          "🔥",
    "Dust Storm Warning":           "🌫️",
    "Dense Fog Advisory":           "🌫️",
    "Excessive Heat Warning":       "🌡️",
    "Heat Advisory":                "🌡️",
    "Freeze Warning":               "🥶",
    "Frost Advisory":               "🥶",
    "Avalanche Warning":            "🏔️",
    "Avalanche Watch":              "🏔️",
    "Air Quality Alert":            "😷",
    "Special Weather Statement":    "📢",
    "Hazardous Weather Outlook":    "📋",
    "Dense Smoke Advisory":         "🚨",
}
DEFAULT_EMOJI = "⚠️"

# Earthquake magnitude emoji
def eq_emoji(mag: float) -> str:
    if mag >= 6.0: return "🚨"
    if mag >= 5.0: return "😬"
    if mag >= 4.0: return "📳"
    return "🔹"


# ── Twitter client ────────────────────────────────────────────────────────────
def get_twitter_client() -> tweepy.Client:
    required = [
        "TWITTER_API_KEY", "TWITTER_API_SECRET",
        "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(f"Missing Twitter credentials: {', '.join(missing)}")
    return tweepy.Client(
        consumer_key=os.environ["TWITTER_API_KEY"],
        consumer_secret=os.environ["TWITTER_API_SECRET"],
        access_token=os.environ["TWITTER_ACCESS_TOKEN"],
        access_token_secret=os.environ["TWITTER_ACCESS_SECRET"],
    )


# ── Cache helpers ─────────────────────────────────────────────────────────────
def load_cache() -> set:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return set(json.load(f).get("posted", []))
        except (json.JSONDecodeError, KeyError):
            log.warning("Cache corrupt, starting fresh.")
    return set()

def save_cache(posted: set) -> None:
    recent = list(posted)[-2000:]
    with open(CACHE_FILE, "w") as f:
        json.dump({
            "posted": recent,
            "updated": datetime.now(timezone.utc).isoformat()
        }, f)


# ── NWS alert fetch ───────────────────────────────────────────────────────────
def fetch_nws_alerts(lookback_minutes: int) -> list[dict]:
    """Fetch NWS alerts for Sierra Nevada zones."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    headers = {
        "User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)",
        "Accept": "application/geo+json",
    }

    # Fetch by zone for precision instead of broad state query
    all_features = []
    seen_ids = set()

    # Also fetch by state to catch county-level alerts
    for state in ["CA", "NV"]:
        try:
            resp = requests.get(
                f"{NWS_ALERTS_URL}/area/{state}",
                headers=headers, timeout=30
            )
            resp.raise_for_status()
            for f in resp.json().get("features", []):
                fid = f.get("id", "")
                if fid not in seen_ids:
                    seen_ids.add(fid)
                    all_features.append(f)
        except requests.RequestException as e:
            log.error(f"NWS fetch error for {state}: {e}")

    log.info(f"Fetched {len(all_features)} raw NWS alerts for CA/NV.")

    # Filter to Sierra zones and recent alerts
    sierra_alerts = []
    for f in all_features:
        props = f.get("properties", {})

        # Check if alert affects any Sierra zone
        ugc_zones = props.get("geocode", {}).get("UGC", [])
        area_desc = props.get("areaDesc", "").lower()
        sierra_counties = [
            "sierra", "tuolumne", "mariposa", "calaveras", "amador",
            "el dorado", "placer", "nevada", "plumas", "lassen",
            "mono", "inyo", "fresno", "tulare", "kern", "shasta",
            "tahoe", "mammoth", "yosemite", "sequoia", "kings canyon"
        ]
        affects_sierra = (
            any(z in SIERRA_NWS_ZONES for z in ugc_zones) or
            any(c in area_desc for c in sierra_counties)
        )
        if not affects_sierra:
            continue

        # Check recency
        sent_str = props.get("sent") or props.get("effective", "")
        if not sent_str:
            continue
        try:
            sent_dt = datetime.fromisoformat(sent_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if sent_dt < cutoff:
            continue

        sierra_alerts.append(f)

    log.info(f"{len(sierra_alerts)} Sierra Nevada NWS alerts within lookback window.")
    return sierra_alerts


# ── USGS earthquake fetch ─────────────────────────────────────────────────────
def fetch_earthquakes(lookback_minutes: int) -> list[dict]:
    """Fetch USGS earthquakes in the Sierra Nevada bounding box."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    # Sierra Nevada bounding box
    params = {
        "format":       "geojson",
        "starttime":    cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": EQ_MIN_MAGNITUDE,
        "minlatitude":  str(SIERRA_BBOX[1]),
        "maxlatitude":  str(SIERRA_BBOX[3]),
        "minlongitude": str(SIERRA_BBOX[0]),
        "maxlongitude": str(SIERRA_BBOX[2]),
        "orderby":      "time",
    }
    try:
        resp = requests.get(USGS_EQ_URL, params=params, timeout=30)
        resp.raise_for_status()
        features = resp.json().get("features", [])
        log.info(f"Fetched {len(features)} earthquakes M{EQ_MIN_MAGNITUDE}+ in Sierra bbox.")
        return features
    except requests.RequestException as e:
        log.error(f"USGS fetch error: {e}")
        return []


# ── Tweet formatters ──────────────────────────────────────────────────────────
def fmt_pacific(dt: datetime) -> str:
    pac = dt.astimezone(PACIFIC)
    tz_abbr = "PDT" if pac.dst() else "PST"
    return f"{pac.strftime('%I:%M %p').lstrip('0')} {tz_abbr}"

def alert_uid(props: dict) -> str:
    raw = (props.get("id") or props.get("@id") or
           f"{props.get('event','')}-{props.get('sent','')}-{props.get('areaDesc','')}")
    return "nws-" + hashlib.md5(raw.encode()).hexdigest()

def eq_uid(eq_props: dict) -> str:
    return "eq-" + hashlib.md5(
        f"{eq_props.get('ids','')}-{eq_props.get('time','')}".encode()
    ).hexdigest()

def format_nws_tweet(props: dict) -> str:
    event    = props.get("event", "Weather Alert")
    emoji    = EVENT_EMOJI.get(event, DEFAULT_EMOJI)
    area     = props.get("areaDesc", "Sierra Nevada")
    headline = (props.get("parameters", {}).get("NWSheadline", [""])[0]
                or props.get("headline", "")
                or "")
    headline = headline.strip()

    expires_label = ""
    expires_str = props.get("expires") or props.get("ends") or ""
    if expires_str:
        try:
            exp_dt = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            expires_label = f"Until {fmt_pacific(exp_dt)}"
        except ValueError:
            pass

    url = props.get("@id", "")
    url_part      = f"\n{url}" if url else ""
    expires_part  = f"\n{expires_label}" if expires_label else ""
    header        = f"{emoji} {event} — {area}"
    if len(header) > 100:
        header = f"{emoji} {event} — {area[:80]}…"

    skeleton  = f"{header}{expires_part}\n\n{url_part}"
    remaining = MAX_TWEET_LEN - len(skeleton) - 2

    if headline and remaining > 20:
        if len(headline) > remaining:
            headline = headline[:remaining - 1] + "…"
        body = f"{header}{expires_part}\n{headline}{url_part}"
    else:
        body = f"{header}{expires_part}{url_part}"

    return body[:MAX_TWEET_LEN]

def format_eq_tweet(props: dict, geometry: dict) -> str:
    mag    = props.get("mag", 0.0)
    place  = props.get("place", "Sierra Nevada region")
    depth  = props.get("depth", 0.0)
    emoji  = eq_emoji(mag)

    # Time
    eq_time_ms = props.get("time", 0)
    eq_dt = datetime.fromtimestamp(eq_time_ms / 1000, tz=timezone.utc)
    time_str = fmt_pacific(eq_dt)

    # USGS event page
    eq_id  = props.get("ids", "").strip(",").split(",")[0]
    url    = f"https://earthquake.usgs.gov/earthquakes/eventpage/{eq_id}" if eq_id else ""

    # Coordinates
    coords = geometry.get("coordinates", [None, None, None])
    lat    = f"{coords[1]:.3f}N" if coords[1] is not None else ""
    lon    = f"{abs(coords[0]):.3f}W" if coords[0] is not None else ""

    body = (
        f"{emoji} M{mag:.1f} Earthquake — {place}\n"
        f"Depth: {depth:.1f} km · {time_str}\n"
        f"{lat}, {lon}"
    )
    if url:
        body += f"\n{url}"

    # Sierra Nevada hashtags
    body += "\n#SierraNevada #earthquake #CAwx #NVwx"

    return body[:MAX_TWEET_LEN]



# ── NIFC wildfire fetch ────────────────────────────────────────────────────────
def fetch_fires() -> list[dict]:
    """Fetch active fire perimeters intersecting the Sierra Nevada bbox."""
    params = {
        "where":         "attr_IncidentTypeCategory = 'WF' AND (attr_PercentContained < 85 OR attr_PercentContained IS NULL)",
        "geometry":      f"{SIERRA_BBOX[0]},{SIERRA_BBOX[1]},{SIERRA_BBOX[2]},{SIERRA_BBOX[3]}",
        "geometryType":  "esriGeometryEnvelope",
        "spatialRel":    "esriSpatialRelIntersects",
        "outFields":     "poly_IncidentName,poly_GISAcres,poly_DateCurrent,"
                         "attr_PercentContained,attr_POOCounty,attr_POOState,"
                         "attr_FireDiscoveryDateTime,attr_UniqueFireIdentifier,"
                         "attr_InitialLatitude,attr_InitialLongitude",
        "f":             "json",
    }
    headers = {
        "User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)",
    }
    # Retry up to 3 times with backoff — ESRI servers occasionally drop connections
    import time as _time
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.get(NIFC_FIRE_URL, params=params,
                               headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except Exception as _e:
            last_err = _e
            if attempt < 2:
                _time.sleep(5 * (attempt + 1))
                log.warning(f"NIFC attempt {attempt+1} failed, retrying...")
    else:
        raise last_err
    try:
        resp = resp  # already set
        features = resp.json().get("features", [])
        # Filter to minimum size AND strictly within Sierra bbox by fire origin point
        def in_sierra_bbox(attrs):
            lat = attrs.get("attr_InitialLatitude")
            lon = attrs.get("attr_InitialLongitude")
            if lat is None or lon is None:
                return True  # keep if no coords — let NWS zone filter handle it
            return (SIERRA_BBOX[1] <= lat <= SIERRA_BBOX[3] and
                    SIERRA_BBOX[0] <= lon <= SIERRA_BBOX[2])

        filtered = [f for f in features
                    if (f.get("attributes", {}).get("poly_GISAcres") or 0) >= FIRE_MIN_ACRES
                    and in_sierra_bbox(f.get("attributes", {}))]
        log.info(f"Fetched {len(filtered)} Sierra fires ≥{FIRE_MIN_ACRES} acres from NIFC.")
        return filtered
    except requests.RequestException as e:
        log.error(f"NIFC fire fetch error: {e}")
        return []


def fire_uid(attrs: dict) -> str:
    """Stable ID for a fire — use unique fire identifier."""
    fid = attrs.get("attr_UniqueFireIdentifier") or attrs.get("poly_IncidentName", "unknown")
    return "fire-" + hashlib.md5(fid.encode()).hexdigest()


def fire_growth_uid(attrs: dict) -> str:
    """UID that changes when fire grows significantly — triggers re-tweet."""
    fid  = attrs.get("attr_UniqueFireIdentifier") or attrs.get("poly_IncidentName", "unknown")
    acres = attrs.get("poly_GISAcres") or 0
    # Bucket acres into growth milestones: 10, 15, 22, 33, 50, 75, 112...
    # Each 50% growth creates a new bucket → new UID → new tweet
    import math
    if acres > 0:
        bucket = int(math.log(acres / FIRE_MIN_ACRES, 1 + FIRE_GROWTH_FACTOR))
    else:
        bucket = 0
    return "fire-" + hashlib.md5(f"{fid}-growth{bucket}".encode()).hexdigest()


def format_fire_tweet(attrs: dict) -> str:
    name      = (attrs.get("poly_IncidentName") or "Unknown Fire").title()
    acres     = attrs.get("poly_GISAcres") or 0
    contained = attrs.get("attr_PercentContained")
    county    = attrs.get("attr_POOCounty") or ""
    state     = (attrs.get("attr_POOState") or "").replace("US-", "")
    lat       = attrs.get("attr_InitialLatitude")
    lon       = attrs.get("attr_InitialLongitude")
    fid       = attrs.get("attr_UniqueFireIdentifier") or ""

    # Containment string
    if contained is not None:
        contained_str = f"{int(contained)}% contained"
    else:
        contained_str = "containment unknown"

    # Location
    location = f"{county}, {state}" if county else state

    # Size label
    if acres >= 1000:
        size_str = f"{acres:,.0f} acres"
    else:
        size_str = f"{acres:.0f} acres"

    # InciWeb link if we have the fire ID
    url = ""
    if fid:
        suffix = fid.split("-")[-1]
        url = "\nhttps://inciweb.wildfire.gov/incident-information/" + suffix

    # Emoji based on size
    if acres >= 10000:
        emoji = "🔥🔥"
    elif acres >= 1000:
        emoji = "🔥"
    else:
        emoji = "🌿🔥"

    body = (
        f"{emoji} Wildfire — {name}\n"
        f"{size_str} · {contained_str}\n"
        f"{location}"
    )
    if lat and lon:
        body += f" ({lat:.3f}N, {abs(lon):.3f}W)"
    if url:
        body += url
    body += "\n#SierraNevada #wildfire #CAwx #NVwx"

    return body[:MAX_TWEET_LEN]



# ── IEM Local Storm Reports fetch ─────────────────────────────────────────────
def fetch_lsr(lookback_minutes: int) -> list[dict]:
    """Fetch Local Storm Reports from IEM for the Sierra Nevada bbox."""
    now     = datetime.now(timezone.utc)
    start   = now - timedelta(minutes=lookback_minutes)

    params = {
        "sts":  start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ets":  now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wfo":  "ALL",
    }
    headers = {
        "User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)",
    }
    try:
        resp = requests.get(IEM_LSR_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        features = resp.json().get("features", [])

        # Filter to Sierra bbox
        sierra = []
        for f in features:
            coords = f.get("geometry", {}).get("coordinates", [None, None])
            lon, lat = coords[0], coords[1]
            if (lon is not None and lat is not None and
                    SIERRA_BBOX[0] <= lon <= SIERRA_BBOX[2] and
                    SIERRA_BBOX[1] <= lat <= SIERRA_BBOX[3]):
                sierra.append(f)

        log.info(f"Fetched {len(sierra)} LSRs in Sierra bbox.")
        return sierra
    except requests.RequestException as e:
        log.error(f"IEM LSR fetch error: {e}")
        return []


def lsr_uid(props: dict) -> str:
    raw = f"{props.get('wfo','')}-{props.get('valid','')}-{props.get('typetext','')}-{props.get('city','')}"
    return "lsr-" + hashlib.md5(raw.encode()).hexdigest()


def format_lsr_tweet(props: dict, coords: list) -> str:
    lsr_type  = props.get("type", "O")
    info      = LSR_TYPES.get(lsr_type, ("⚠️", "Storm Report", "any"))
    emoji, label, season = info

    # Winter vs summer label prefix
    if season == "winter":
        prefix = "❄️ Winter Report"
    elif season == "summer":
        prefix = "⛈️ Storm Report"
    else:
        prefix = "⚠️ Report"

    city      = props.get("city", "")
    county    = props.get("county", "")
    state     = props.get("st", "")
    magnitude = props.get("magnitude", "")
    mag_unit  = props.get("magunit", "")
    remark    = (props.get("remark") or "")[:120]
    valid_str = props.get("valid", "")
    source    = props.get("source", "")
    wfo       = props.get("wfo", "")

    # Time in Pacific
    time_str = ""
    if valid_str:
        try:
            dt = datetime.fromisoformat(valid_str.replace("Z", "+00:00"))
            time_str = fmt_pacific(dt)
        except ValueError:
            pass

    # Location
    location = city
    if county:
        location += f" [{county} Co, {state}]"

    # Magnitude string
    mag_str = ""
    if magnitude and str(magnitude) not in ("0", "0.0", "None"):
        mag_str = f" - {magnitude} {mag_unit}".strip()

    body = f"{emoji} {label}{mag_str}\n{location}"
    if time_str:
        body += f" - {time_str}"
    if remark:
        body += f"\n{remark}"
    body += "\n#" + state.lower() + "wx #SierraNevada"

    return body[:MAX_TWEET_LEN]


# ── AirNow air quality fetch ──────────────────────────────────────────────────
def fetch_airnow() -> list[dict]:
    """Fetch current AQI from AirNow latLong endpoint for Sierra monitoring points."""
    api_key = os.environ.get("AIRNOW_API_KEY", "")
    if not api_key:
        log.warning("AIRNOW_API_KEY not set — skipping AQ check.")
        return []

    url = "https://www.airnowapi.org/aq/observation/latLong/current/"
    bad_air = []
    seen = set()  # deduplicate by reporting area + parameter

    for lat, lon, name in SIERRA_MONITOR_POINTS:
        params = {
            "format":   "application/json",
            "latitude":  lat,
            "longitude": lon,
            "distance":  50,   # km radius
            "API_KEY":   api_key,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code != 200 or not resp.text.strip().startswith("["):
                continue
            observations = resp.json()
            for obs in observations:
                if not isinstance(obs, dict):
                    continue
                aqi = obs.get("AQI") or 0
                key = f"{obs.get('ReportingArea','')}-{obs.get('ParameterName','')}"
                if aqi >= AIRNOW_MIN_AQI and key not in seen:
                    seen.add(key)
                    bad_air.append(obs)
        except Exception:
            continue

    log.info(f"Fetched {len(bad_air)} AirNow observations above AQI {AIRNOW_MIN_AQI} in Sierra.")
    return bad_air


def airnow_uid(obs: dict) -> str:
    """UID that changes when AQI crosses category boundaries — re-tweets on worsening."""
    site   = obs.get("ReportingArea", "") + obs.get("StateCode", "")
    param  = obs.get("ParameterName", "")
    aqi    = obs.get("AQI", 0)
    # Bucket: 101-150, 151-200, 201-300, 300+
    if aqi >= 301:   bucket = "hazardous"
    elif aqi >= 201: bucket = "very_unhealthy"
    elif aqi >= 151: bucket = "unhealthy"
    else:            bucket = "sensitive"
    return "aq-" + hashlib.md5(f"{site}-{param}-{bucket}".encode()).hexdigest()


def format_airnow_tweet(obs: dict) -> str:
    aqi       = obs.get("AQI", 0)
    area      = obs.get("ReportingArea", "Sierra Nevada")
    state     = obs.get("StateCode", "CA")
    param     = obs.get("ParameterName", "PM2.5")
    category  = obs.get("Category", {}).get("Name", aqi_label(aqi))
    hour_str  = obs.get("HourObserved", "")
    date_str  = obs.get("DateObserved", "")

    emoji = aqi_emoji(aqi)
    label = aqi_label(aqi)

    # Friendly parameter name
    param_name = {
        "PM2.5": "Fine Particles (PM2.5)",
        "PM10":  "Coarse Particles (PM10)",
        "OZONE": "Ozone",
    }.get(param, param)

    # Time
    time_label = ""
    if date_str and hour_str != "":
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.strptime(f"{date_str.strip()} {int(hour_str):02d}:00",
                                   "%Y-%m-%d %H:%M")
            dt = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
            tz_abbr = "PDT" if dt.dst() else "PST"
            time_label = f" · {dt.strftime('%I:%M %p').lstrip('0')} {tz_abbr}"
        except Exception:
            pass

    body = (
        f"{emoji} Air Quality Alert - {area}, {state}\n"
        f"AQI {aqi} - {label}\n"
        f"{param_name}{time_label}\n"
        "#SierraNevada #AirQuality #" + state.lower() + "wx"
    )
    return body[:MAX_TWEET_LEN]




# ── CAL FIRE incident fetch ───────────────────────────────────────────────────
def fetch_calfire() -> list[dict]:
    """Fetch active CAL FIRE incidents filtered to Sierra bbox."""
    headers = {
        "User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)",
    }
    try:
        resp = requests.get(CALFIRE_URL, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features", []) if isinstance(data, dict) else data

        sierra = []
        for f in features:
            props = f.get("properties", {}) if isinstance(f, dict) else {}
            geo   = f.get("geometry", {}) if isinstance(f, dict) else {}

            # Skip if contained >= 85% or acres < minimum
            contained = props.get("PercentContained") or 0
            acres     = props.get("AcresBurned") or 0
            if contained >= 85 or acres < FIRE_MIN_ACRES:
                continue

            # Filter by coordinates to Sierra bbox
            # Must have valid non-null coordinates — null coords slip through otherwise
            coords = geo.get("coordinates", []) if geo else []
            if (coords and len(coords) >= 2 and
                    coords[0] is not None and coords[1] is not None):
                lon, lat = float(coords[0]), float(coords[1])
                if (SIERRA_BBOX[0] <= lon <= SIERRA_BBOX[2] and
                        SIERRA_BBOX[1] <= lat <= SIERRA_BBOX[3]):
                    sierra.append(f)
            else:
                log.debug(f"CAL FIRE: skipping '{props.get('Name','?')}' — no valid coordinates")

        log.info(f"Fetched {len(sierra)} CAL FIRE incidents in Sierra bbox.")
        return sierra
    except requests.RequestException as e:
        log.error(f"CAL FIRE fetch error: {e}")
        return []


def calfire_uid(props: dict) -> str:
    """Stable UID for a CAL FIRE incident — must never change across updates.

    Returns TWO keys: one based on UniqueId GUID, one based on Name+County.
    Both get added to the cache so either form of the same fire is deduplicated.
    This handles the case where CAL FIRE changes the UniqueId between updates.
    """
    name   = (props.get("Name") or "unknown").strip().upper()
    county = (props.get("Counties") or props.get("County") or "").strip().upper()
    name_key = "calfire-" + hashlib.md5(f"{name}-{county}".encode()).hexdigest()[:8]

    uid = (props.get("UniqueId") or
           props.get("IncidentID") or
           props.get("incident_id") or
           None)
    if uid:
        guid_key = "calfire-" + hashlib.md5(str(uid).encode()).hexdigest()[:8]
        return guid_key, name_key   # return both
    return name_key, name_key       # same key twice if no GUID


def calfire_already_posted(props: dict, cache: set) -> bool:
    """Returns True if either the GUID-based or name-based UID is in cache."""
    k1, k2 = calfire_uid(props)
    return k1 in cache or k2 in cache


def calfire_cache_keys(props: dict) -> tuple:
    """Returns both cache keys to add after posting."""
    return calfire_uid(props)


def calfire_growth_uid(props: dict) -> str:
    """Growth-bucketed UID — new tweet when fire grows 50%+."""
    import math
    raw   = props.get("UniqueId") or props.get("Name", "unknown")
    acres = props.get("AcresBurned") or 0
    if acres > 0:
        bucket = int(math.log(max(acres, FIRE_MIN_ACRES) / FIRE_MIN_ACRES,
                               1 + FIRE_GROWTH_FACTOR))
    else:
        bucket = 0
    return "calfire-" + hashlib.md5(f"{raw}-growth{bucket}".encode()).hexdigest()


def format_calfire_tweet(props: dict) -> str:
    name      = (props.get("Name") or "Unknown Fire").title()
    acres     = props.get("AcresBurned") or 0
    contained = props.get("PercentContained")
    county    = props.get("Counties") or props.get("County") or ""
    status    = props.get("Status") or ""
    updated   = props.get("Updated") or ""
    url       = props.get("CanonicalUrl") or ""

    contained_str = f"{int(contained)}% contained" if contained is not None else "containment unknown"
    size_str      = f"{acres:,.0f} acres" if acres >= 1000 else f"{acres:.0f} acres"

    if acres >= 10000: emoji = "🔥🔥"
    elif acres >= 1000: emoji = "🔥"
    else:               emoji = "🌿🔥"

    location = f"{county}, CA" if county else "Sierra Nevada, CA"

    body = (
        f"{emoji} CAL FIRE - {name}\n"
        f"{size_str} - {contained_str}\n"
        f"{location}"
    )
    if url:
        full_url = f"https://www.fire.ca.gov{url}" if url.startswith("/") else url
        body += "\n" + full_url
    body += "\n#SierraNevada #wildfire #CAwx #CalFire"

    return body[:MAX_TWEET_LEN]


# ── NWPS Stream Gauge fetch ──────────────────────────────────────────────────
def fetch_gauges() -> list[dict]:
    """Fetch Sierra gauge status from NWS NWPS API — gets flood stages dynamically."""
    headers = {
        "User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)",
        "Accept":     "application/json",
    }
    alerts = []
    for gauge_id, label, river in SIERRA_NWPS_GAUGES:
        try:
            url  = NWPS_GAUGE_URL.format(gauge_id=gauge_id)
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()

            # Get current observed stage
            obs   = data.get("observed", {})
            stage = obs.get("primary", {}).get("value")
            if stage is None:
                continue
            stage = float(stage)

            # Get flood thresholds from API
            thresholds = data.get("flood", {}).get("categories", {})
            minor_ft    = thresholds.get("minor")
            moderate_ft = thresholds.get("moderate")
            if minor_ft is None:
                continue

            minor_ft    = float(minor_ft)
            moderate_ft = float(moderate_ft) if moderate_ft else minor_ft + 5

            if stage >= minor_ft:
                alerts.append({
                    "gauge_id":    gauge_id,
                    "label":       label,
                    "river":       river,
                    "stage":       stage,
                    "minor_ft":    minor_ft,
                    "moderate_ft": moderate_ft,
                    "datetime":    obs.get("timestamp", ""),
                    "severity":    "moderate" if stage >= moderate_ft else "minor",
                })
        except Exception as e:
            log.warning(f"NWPS gauge {gauge_id} error (non-fatal): {e}")
            continue

    log.info(f"Checked {len(SIERRA_NWPS_GAUGES)} Sierra gauges, {len(alerts)} at flood stage.")
    return alerts


def gauge_uid(alert: dict) -> str:
    """UID that changes when gauge crosses flood stage threshold."""
    level = alert["severity"]
    feet_above = int(alert["stage"] - alert["minor_ft"])
    level = f"{level}+{feet_above}ft"
    return "gauge-" + hashlib.md5(
        f"{alert['gauge_id']}-{level}".encode()
    ).hexdigest()


def format_gauge_tweet(alert: dict) -> str:
    river    = alert["river"]
    label    = alert["label"]
    stage    = alert["stage"]
    minor_ft    = alert["minor_ft"]
    moderate_ft = alert["moderate_ft"]
    severity    = alert["severity"]

    if severity == "moderate":
        emoji  = "🌊🌊"
        status = "MODERATE FLOODING"
        above  = stage - moderate_ft
        detail = f"{stage:.1f} ft — {above:.1f} ft above moderate flood stage ({moderate_ft} ft)"
    else:
        emoji  = "🌊"
        status = "MINOR FLOODING"
        above  = stage - minor_ft
        detail = f"{stage:.1f} ft — {above:.1f} ft above minor flood stage ({minor_ft} ft)"

    # Time in Pacific
    time_str = ""
    try:
        dt = datetime.fromisoformat(alert["datetime"].replace("Z", "+00:00"))
        time_str = fmt_pacific(dt)
    except Exception:
        pass

    url = f"https://waterdata.usgs.gov/monitoring-location/{alert['site_id']}/"

    body = (
        f"{emoji} {status} - {river} at {label}\n"
        f"{detail}\n"
    )
    if time_str:
        body += "As of " + time_str + "\n"
    body += url + "\n"
    body += "#SierraNevada #flooding #CAwx #NVwx"

    return body[:MAX_TWEET_LEN]

# ── Post tweet ────────────────────────────────────────────────────────────────
def post_tweet(client, text: str, uid: str, posted: set,
               new_count: list, error_count: list) -> None:
    if uid in posted:
        log.debug(f"Skipping already-posted: {uid}")
        return

    log.info(f"Posting [{uid[:12]}] len={len(text)}")

    if DRY_RUN:
        log.info(f"[DRY RUN]\n{text}\n")
        posted.add(uid)
        new_count[0] += 1
        return

    try:
        client.create_tweet(text=text)
        posted.add(uid)
        new_count[0] += 1
        time.sleep(2)
    except tweepy.TweepyException as e:
        log.error(f"Twitter error: {e}")
        error_count[0] += 1
        if "187" in str(e):
            posted.add(uid)
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        error_count[0] += 1


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    log.info("=== Sierra Nevada Alert Bot starting ===")

    client  = None if DRY_RUN else get_twitter_client()
    posted  = load_cache()
    log.info(f"Cache contains {len(posted)} previously posted IDs.")

    new_count   = [0]
    error_count = [0]

    # ── NWS weather alerts ────────────────────────────────────────────────────
    nws_alerts = fetch_nws_alerts(LOOKBACK_MINUTES)
    for feature in nws_alerts:
        props = feature.get("properties", {})
        uid   = alert_uid(props)
        text  = format_nws_tweet(props)
        post_tweet(client, text, uid, posted, new_count, error_count)

    # ── NIFC wildfire monitoring ──────────────────────────────────────────────
    fires = fetch_fires()
    for feature in fires:
        attrs = feature.get("attributes", {})
        # Tweet on new fires (first-seen UID)
        new_uid    = fire_uid(attrs)
        # Tweet again on significant growth (growth-bucketed UID)
        growth_uid = fire_growth_uid(attrs)

        text = format_fire_tweet(attrs)
        is_new_fire = new_uid not in posted
        # Post new fire discovery
        post_tweet(client, text, new_uid, posted, new_count, error_count)
        # Post growth update ONLY if fire was already known (not brand new this run)
        if not is_new_fire and growth_uid not in posted:
            growth_text = "📈 " + text[2:] if text.startswith("🔥") else "📈 " + text
            post_tweet(client, growth_text, growth_uid, posted, new_count, error_count)

    # ── CAL FIRE incidents ────────────────────────────────────────────────────
    calfire_incidents = fetch_calfire()
    for feature in calfire_incidents:
        props      = feature.get("properties", {})
        k1, k2     = calfire_uid(props)
        growth_uid = calfire_growth_uid(props)
        text       = format_calfire_tweet(props)
        is_new     = not calfire_already_posted(props, posted)
        # Use guid-based key as primary, add both to cache after posting
        new_uid    = k1
        post_tweet(client, text, new_uid, posted, new_count, error_count)
        posted.add(k2)  # cache name-based key to prevent re-tweet on record update
        if not is_new and growth_uid not in posted:
            growth_text = "📈 " + text[2:] if text.startswith("🔥") else "📈 " + text
            post_tweet(client, growth_text, growth_uid, posted, new_count, error_count)

    # ── IEM Local Storm Reports ───────────────────────────────────────────────
    lsrs = fetch_lsr(LOOKBACK_MINUTES)
    for feature in lsrs:
        props  = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [None, None])
        uid    = lsr_uid(props)
        text   = format_lsr_tweet(props, coords)
        post_tweet(client, text, uid, posted, new_count, error_count)

    # ── AirNow air quality ────────────────────────────────────────────────────
    aq_obs = fetch_airnow()
    for obs in aq_obs:
        uid  = airnow_uid(obs)
        text = format_airnow_tweet(obs)
        post_tweet(client, text, uid, posted, new_count, error_count)

    # ── USGS Stream Gauges ────────────────────────────────────────────────────
    gauge_alerts = fetch_gauges()
    for alert in gauge_alerts:
        uid  = gauge_uid(alert)
        text = format_gauge_tweet(alert)
        post_tweet(client, text, uid, posted, new_count, error_count)

    # ── USGS earthquakes ──────────────────────────────────────────────────────
    earthquakes = fetch_earthquakes(LOOKBACK_MINUTES)
    for feature in earthquakes:
        props    = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        uid      = eq_uid(props)
        text     = format_eq_tweet(props, geometry)
        post_tweet(client, text, uid, posted, new_count, error_count)

    save_cache(posted)
    log.info(
        f"Done. NWS: {len(nws_alerts)} | NIFC: {len(fires)} | CAL FIRE: {len(calfire_incidents)} | LSR: {len(lsrs)} | AQ: {len(aq_obs)} | Gauges: {len(gauge_alerts)} | "
        f"EQ: {len(earthquakes)} quakes | "
        f"Posted: {new_count[0]} | Errors: {error_count[0]} | "
        f"Cache: {len(posted)}"
    )

if __name__ == "__main__":
    run()
