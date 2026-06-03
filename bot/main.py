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
NIFC_FIRE_URL = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)

# Sierra bbox as separate coords for ESRI API
SIERRA_BBOX = (-121.0, 35.5, -117.5, 41.5)

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
        "where":         "attr_IncidentTypeCategory = 'WF'",
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
    try:
        resp = requests.get(NIFC_FIRE_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        features = resp.json().get("features", [])
        # Filter to minimum size
        filtered = [f for f in features
                    if (f.get("attributes", {}).get("poly_GISAcres") or 0) >= FIRE_MIN_ACRES]
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
        # Post new fire discovery
        post_tweet(client, text, new_uid, posted, new_count, error_count)
        # Post growth update (only if fire already known but grew)
        if new_uid in posted and growth_uid not in posted:
            growth_text = "📈 " + text[2:] if text.startswith("🔥") else "📈 " + text
            post_tweet(client, growth_text, growth_uid, posted, new_count, error_count)

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
        f"Done. NWS: {len(nws_alerts)} alerts | Fires: {len(fires)} | "
        f"EQ: {len(earthquakes)} quakes | "
        f"Posted: {new_count[0]} | Errors: {error_count[0]} | "
        f"Cache: {len(posted)}"
    )

if __name__ == "__main__":
    run()
