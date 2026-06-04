# 🌪️ Sierra Nevada Alert Bot

> *"It's already here."*

---

The mountains don't warn you. The sky doesn't ask permission. A wildfire crowns at 2 AM. The Tuolumne blows past flood stage on a Tuesday. A supercell drops a tornado over a mobile home park while everyone's at work.

**This bot watches so you don't have to.**

`@SierraNevadaWX` is an autonomous environmental monitoring system built for the Sierra Nevada — one of the most geologically violent, meteorologically complex, and ecologically irreplaceable mountain ranges on Earth. It pulls real data from seven live federal data streams, runs every few minutes on GitHub Actions, and posts to Twitter the moment something worth knowing is happening.

No ads. No clickbait. No weather personality telling you to stay tuned. Just the data, raw and real, the moment it matters.

---

## 📡 What It Watches

| Source | Trigger | Why It Matters |
|--------|---------|----------------|
| 🌩️ **NWS Weather Alerts** | Any Sierra zone warning | The official word from forecasters watching the same sky |
| 🔥 **NIFC Wildfire Perimeters** | New fire or 50%+ growth, <85% contained, ≥10 acres | Fires that are still fighting back |
| 🔥 **CAL FIRE Incidents** | Same thresholds, faster CA updates | Aerial intel — sometimes ahead of NIFC by hours |
| ⛈️ **IEM Storm Reports** | Any LSR in the Sierra bbox | Trained spotters on the ground calling it in |
| 😷 **AirNow Air Quality** | AQI >100 at 8 Sierra stations | When smoke makes the air a hazard |
| 🌊 **NWPS Stream Gauges** | Minor flood stage on 7 Sierra rivers | The snowmelt reckoning arriving downstream |
| 🌋 **USGS Earthquakes** | M2.5+ in the Sierra bbox | The Sierra Nevada sits on active fault systems |

The Sierra bounding box covers `-121.0°W to -117.5°W, 35.5°N to 41.5°N` — from Lassen in the north to the White Mountains in the south, from the Central Valley foothills to the Nevada Great Basin.

---

## 🏔️ The Rivers We Watch

When the snowpack releases, it all flows somewhere. These are the gauges that tell us what's coming:

| River | Station | Minor Flood | Moderate Flood |
|-------|---------|------------|----------------|
| Tuolumne | Modesto | 55 ft | 62 ft |
| Tuolumne | Hetch Hetchy | 9 ft | 11 ft |
| Merced | Happy Isles / Yosemite Valley | 7.5 ft | 9 ft |
| Merced | Merced | 71 ft | 74 ft |
| American | Fair Oaks | 25 ft | 33 ft |
| Truckee | Reno | 11 ft | 13.5 ft |
| Kings | Pine Flat | 18 ft | 22 ft |

Flood stages from NWS/CNRFC. The bot tweets at **minor flood stage** — when property damage begins — not at action stage. We're not here to cry wolf.

---

## 🛠️ How It's Built

**Runtime:** GitHub Actions, scheduled every ~5 minutes  
**Language:** Python 3.12  
**Package manager:** [pixi](https://prefix.dev/docs/pixi/)  
**Twitter API:** Tweepy v4 (Basic tier)  
**Deduplication:** SHA-MD5 cache committed to `posted_ids.json`

```
sierra-alert-bot/
├── bot/
│   └── main.py              # The engine — all 7 data sources
├── tools/
│   ├── sierra_skewt.py      # MetPy Skew-T upper air sounding plots
│   └── nexrad_analysis.py   # Py-ART NEXRAD Level 2 dual-pol analysis
├── .github/
│   └── workflows/
│       └── sierra-bot.yml   # GitHub Actions schedule
└── pyproject.toml           # pixi dependencies
```

### Deploy your own

```bash
git clone https://github.com/bdgroves/sierra-alert-bot
cd sierra-alert-bot
pixi install
```

Set GitHub Actions secrets:
```
TWITTER_API_KEY
TWITTER_API_SECRET
TWITTER_ACCESS_TOKEN
TWITTER_ACCESS_SECRET
AIRNOW_API_KEY        # free at airnow.gov
```

---

## 🌡️ The Science Tools

This repo isn't just a bot. It's a growing toolkit for understanding the Sierra Nevada atmosphere.

### Skew-T Log-P Soundings

```bash
pixi run skewt                    # Latest Reno (REV) sounding
pixi run skewt-all                # All 4 stations → skewt_output/
pixi run -- python tools/sierra_skewt.py --station OAK --time 12Z
```

Twice a day, weather balloons launch from Reno, Oakland, Salt Lake City, and Vandenberg. They rise to 100,000 feet, measuring temperature, humidity, and wind every few meters. `sierra_skewt.py` pulls that data via the Iowa State RAOB archive and plots it as a professional Skew-T Log-P diagram with:

- Temperature and dewpoint profiles
- Wind barbs showing shear with altitude
- Parcel path with CAPE/CIN shading
- LCL, LFC, and EL markers
- Hodograph inset (colored by pressure level)
- Indices panel: CAPE, CIN, LI, PW, LCL

**Reading the Sierra morning sounding:**  
When Reno (REV) shows CAPE near zero and a massive dewpoint depression between 500–700 hPa, that's the Great Basin drying out — a classic setup for dry lightning and high fire danger on the eastern slope by afternoon. When Salt Lake City shows negative LI with no CIN cap, that instability is heading your way.

**Stations:**
| ID | Location | What it tells you |
|----|----------|------------------|
| REV | Reno, NV | Eastern Sierra, Great Basin moisture |
| OAK | Oakland, CA | Marine layer depth, western slope stability |
| VBG | Vandenberg, CA | Southern California offshore flow |
| SLC | Salt Lake City, UT | Great Basin instability moving west |

---

### NEXRAD Level 2 Dual-Pol Analysis

```bash
pixi run nexrad-ok              # Latest KVNX (Vance AFB, Oklahoma)
pixi run nexrad-sierra          # Latest KRGX (Reno — Sierra radar)
pixi run -- python tools/nexrad_analysis.py --radar KBBX  # Beale AFB
pixi run -- python tools/nexrad_analysis.py --radar KHNX  # Hanford
```

NOAA puts every NEXRAD scan on AWS S3 within seconds of it completing — free, public, real-time. `nexrad_analysis.py` pulls the latest volume from `s3://unidata-nexrad-level2`, reads all six dual-pol fields with [Py-ART](https://arm-doe.github.io/pyart/), and produces a dark-themed 6-panel analysis plot with automatic signature detection:

**The six fields:**

| Field | What it measures | What to look for |
|-------|-----------------|-----------------|
| **Z** Reflectivity | Precipitation intensity | Hook echo, bow echo, hail core |
| **V** Velocity | Radial wind speed | Tight green/red couplet = rotation = TVS |
| **SW** Spectrum Width | Turbulence | High values near rotation core |
| **ZDR** Diff Reflectivity | Drop shape/size | Near 0 dB = tumbling hail |
| **CC** Correlation Coeff | Scatterer uniformity | <0.80 inside high-Z = **tornado debris** |
| **PhiDP** Diff Phase | Total rain accumulation | Rapid increase = flash flood rainfall |

**Automatic detection:**

- 🟣 **TDS** (Tornado Debris Signature): Z>40 AND CC<0.80 — lofted non-meteorological debris. When you see magenta dots on the CC panel, there is almost certainly a tornado on the ground.
- 🟡 **TVS proxy**: Z>45 AND SW>7 — turbulent high-reflectivity. Check the velocity panel for the rotation couplet.
- 🩵 **Hail signature**: Z>55 AND ZDR~0 — large hail tumbling randomly in the beam.

**Sierra Nevada radars:**
```
KRGX  Reno, NV        — eastern Sierra crest, Tahoe basin
KBBX  Beale AFB, CA   — northern Sierra, Sacramento Valley
KHNX  Hanford, CA     — southern Sierra, San Joaquin Valley
```

---

## 🌩️ A Day in the Life

On June 4, 2026, while building this system, we watched a severe thunderstorm outbreak unfold in real time over north-central Oklahoma. The bot was watching the Sierra while we pivoted the NEXRAD tool east:

- `KVNX` scan at 21Z showed a massive bow echo centered over Alfalfa and Garfield County
- **TDS: 54 pixels detected** — CC below 0.80 inside a high-Z core near Enid
- **Hail signature: 11 pixels** — Z>55 with ZDR near zero
- The tornado warning polygon was visible on the QGIS radar overlay
- `#okwx` confirmed severe thunderstorm warnings for Alfalfa, Garfield, and Grant County

The "TDS" turned out to be a hail/rain mixture rather than tornado debris — a good reminder that signatures must be read in context across all six fields. The velocity panel was range-folded. The CC was 0.75–0.79, not the 0.60 you'd expect from a real debris field.

Science in real time. That's the point.

---

## 📺 The Sierra Nevada

> *"You've never seen it from the inside."*

The Sierra Nevada runs 400 miles along California's eastern spine. It generates its own weather. It feeds rivers that water 40 million people. It burns — sometimes catastrophically. It shakes. It floods. It hosts the deepest snowpack in North America and some of the most complex terrain-driven convection on the continent.

**What this bot has already caught in its first days:**
- 🔥 Sage Fire — Inyo County, 30 acres, 50% contained (before NIFC listed it)
- 🌊 Tuolumne River at Modesto — running 36.4 ft (19 feet below flood stage — the thresholds matter)
- 🌊 Merced River at Merced — 49.4 ft
- ☀️ AQI monitoring across 8 Sierra stations — ready for fire season

---

## 🛰️ Data Sources

| Data | Provider | Endpoint |
|------|----------|----------|
| Weather Alerts | NWS / api.weather.gov | `api.weather.gov/alerts/active/area/{state}` |
| Wildfire Perimeters | NIFC / WFIGS | ArcGIS FeatureServer |
| CAL FIRE Incidents | CAL FIRE | GeoJSON API |
| Storm Reports | IEM | `mesonet.agron.iastate.edu/geojson/lsr.php` |
| Air Quality | AirNow | `airnowapi.org/aq/observation/latLong/current/` |
| Stream Gauges | NWS NWPS | `api.water.noaa.gov/nwps/v1/gauges/{id}` |
| Earthquakes | USGS | `earthquake.usgs.gov/fdsnws/event/1/query` |
| Upper Air Soundings | Iowa State RAOB | Siphon / IAStateUpperAir |
| NEXRAD Level 2 | NOAA / Unidata | `s3://unidata-nexrad-level2` |

---

## 📜 License

MIT. Use it, fork it, build on it. If you extend it to another mountain range, open a PR.

---

## 🙏 Built with

[MetPy](https://unidata.github.io/MetPy/) · [Py-ART](https://arm-doe.github.io/pyart/) · [Tweepy](https://www.tweepy.org/) · [Siphon](https://unidata.github.io/siphon/) · [pixi](https://prefix.dev/) · [GitHub Actions](https://github.com/features/actions) · [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) · [NOAA Open Data Dissemination](https://www.noaa.gov/information-technology/open-data-dissemination)

---

*Built in the Tri-Cities, watching the Sierra Nevada.*  
*[@bdgroves](https://twitter.com/bdgroves) · [@SierraNevadaWX](https://twitter.com/SierraNevadaWX)*
