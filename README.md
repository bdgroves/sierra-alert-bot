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

The Sierra bounding box covers `-121.0°W to -117.5°W, 36.0°N to 41.5°N` — from Lassen in the north to the White Mountains in the south, from the Central Valley foothills to the Nevada Great Basin.

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
│   ├── nexrad_analysis.py   # NEXRAD Level 2 dual-pol analysis (Py-ART)
│   ├── sierra_firewx.py     # Sierra fire weather dashboard (FFWI/HDW/Haines)
│   └── sierra_skewt.py      # MetPy Skew-T upper air sounding plots
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

This repo isn't just a bot. It's a growing toolkit for understanding the Sierra Nevada atmosphere — and severe weather anywhere in the country.

---

### 🔥 Sierra Fire Weather Dashboard

```bash
pixi run firewx                    # All 5 Sierra stations, current conditions
pixi run firewx-save               # Save PNG to firewx_output/
pixi run -- python tools/sierra_firewx.py --station KBIH
```

Pulls live NWS surface observations from 5 Sierra stations and the latest REV upper air sounding from Reno. Calculates three fire weather indices and plots a Twitter-ready dashboard:

**The three indices:**

| Index | What it measures | Red Flag threshold |
|-------|-----------------|-------------------|
| **FFWI** Fosberg Fire Weather Index | Rate of fire spread based on fuel moisture, temp, wind | > 50 significant, > 75 critical |
| **HDW** Hot-Dry-Windy Index | Vapor pressure deficit × wind speed — the two key fire spread drivers | > 80 high, > 160 extreme |
| **Haines Index** | Atmospheric dryness and instability aloft — how much a fire can grow | 5-6 = high/very high |

**Sierra monitoring stations:**

| Station | Location | Elevation |
|---------|----------|-----------|
| KRNO | Reno-Tahoe International | 4,415 ft |
| KTVL | Lake Tahoe Airport | 6,264 ft |
| KMMH | Mammoth Yosemite Airport | 7,135 ft |
| KBLU | Blue Canyon | 5,284 ft |
| KBIH | Bishop (Owens Valley) | 4,124 ft |

**June 5, 2026 — Real data from a Fire Weather Watch day:**
```
KBIH Bishop:   97°F  RH:9%   Wind:70mph  Gusts:91mph  FFWI:205  HDW:3822  🔥 RED FLAG
KMMH Mammoth:  81°F  RH:16%  Wind:29mph  Gusts:62mph  FFWI:76   HDW:869
KRNO Reno:     90°F  RH:8%   Wind:33mph              FFWI:97   HDW:1450  🔥 RED FLAG
Haines: 5 (High)
```

---

### 📡 NEXRAD Level 2 Dual-Pol Analysis

```bash
pixi run nexrad-ok              # Latest KVNX (Vance AFB, Oklahoma)
pixi run nexrad-sierra          # Latest KRGX (Reno — Sierra radar)
pixi run -- python tools/nexrad_analysis.py --radar KDLH   # Duluth MN
pixi run -- python tools/nexrad_analysis.py --radar KLBB   # Lubbock TX
pixi run -- python tools/nexrad_analysis.py --radar KMAF --sweep 4
pixi run -- python tools/nexrad_analysis.py --all-sweeps --radar KMPX
```

NOAA puts every NEXRAD scan on AWS S3 within seconds of it completing — free, public, real-time. `nexrad_analysis.py` pulls the latest volume from `s3://unidata-nexrad-level2`, reads all six dual-pol fields with [Py-ART](https://arm-doe.github.io/pyart/), and produces a dark-themed 6-panel analysis plot with automatic signature detection.

**The six fields:**

| Field | What it measures | What to look for |
|-------|-----------------|-----------------|
| **Z** Reflectivity | Precipitation intensity | Hook echo, bow echo, hail core |
| **V** Velocity | Radial wind — dealiased | Tight green/red couplet = rotation = TVS |
| **SW** Spectrum Width | Turbulence | High values near rotation core |
| **ZDR** Diff Reflectivity | Drop shape/size | Near 0 dB = tumbling hail |
| **CC** Correlation Coeff | Scatterer uniformity | <0.80 inside high-Z = tornado debris |
| **PhiDP** Diff Phase | Total rain accumulation | Rapid increase = flash flood rainfall |

**Automatic signature detection:**

- 🟣 **TDS** (Tornado Debris Signature): Z>40 AND CC<0.80 — lofted non-meteorological debris
- 🟡 **TVS proxy**: Z>45 AND SW>7 — turbulent high-reflectivity, check velocity for rotation
- 🩵 **Hail signature**: Z>55 AND ZDR~0 — large hail tumbling randomly in the beam

**Technical details:**
- Automatic split-cut VCP detection — velocity plotted from the correct sweep (not sweep 0 which is reflectivity-only on most NEXRAD VCPs)
- `dealias_region_based` velocity dealiasing with CC gate filter
- Falls back to raw velocity if dealiasing produces empty output
- Strips Zillow/corporate AWS credentials from environment for true anonymous S3 access

**Sierra Nevada radars:**
```
KRGX  Reno, NV        — eastern Sierra crest, Tahoe basin  (7,807 ft elevation!)
KBBX  Beale AFB, CA   — northern Sierra, Sacramento Valley
KHNX  Hanford, CA     — southern Sierra, San Joaquin Valley
```

---

### 🌡️ Skew-T Log-P Soundings

```bash
pixi run skewt                    # Latest Reno (REV) sounding
pixi run skewt-all                # All 4 stations → skewt_output/
```

Twice a day, weather balloons launch from Reno, Oakland, Salt Lake City, and Vandenberg. They rise to 100,000 feet, measuring temperature, humidity, and wind every few meters. `sierra_skewt.py` pulls that data via the Iowa State RAOB archive and plots it as a professional Skew-T Log-P diagram with hodograph, CAPE/CIN shading, and indices panel.

**Stations:** REV (Reno), OAK (Oakland), VBG (Vandenberg), SLC (Salt Lake City)

---

## 🌩️ Bow Echo — What It Looks Like

A bow echo is what happens when a squall line gets punched from behind by a rear inflow jet. The middle of the line accelerates faster than the ends, bending into a bow shape. The apex is where the most violent straight-line winds occur — often 60-80 mph.

**The four stages we watch for:**

| Stage | FFWI | Winds | Threat | Warning type |
|-------|------|-------|--------|-------------|
| 1 — Gentle bow | — | 40-50 mph | SVR | Severe Thunderstorm |
| 2 — Classic bow | — | 60-75 mph | SVR | Severe Thunderstorm |
| 3 — Intense bow | — | 75-90 mph | SVR + possible TOR | Tornado possible |
| 4 — Derecho | — | 90-130+ mph | Widespread destruction | Multiple TOR + SVR |

A **derecho** is a bow echo that has traveled more than 400 miles maintaining 58+ mph winds. They are effectively inland hurricanes.

**What to watch for on radar:**
- Bow sharpening — apex getting pointier = rear inflow intensifying
- Comma head — northern book-end vortex wrapping into a curl = rotation
- Debris notch — gap in precip on south flank = strong inflow

---

## 🌩️ A Day in the Life — June 5, 2026

While building this system, we watched a severe weather outbreak unfold in real time across four states simultaneously:

**Minnesota MCS (morning):**
- Classic bow echo tracking from west MN toward Duluth
- KDLH dual-pol confirmed hail signatures and book-end vortex development
- Bow evolved Stage 2→3, comma head developed, then system collapsed
- Severe Thunderstorm Warning issued by WFO Duluth — no tornado

**Oklahoma supercell (midday):**
- KVNX scan showed TDS 55px, Hail 52px near Cleo Springs/Helena
- Ground truth (#okwx): Severe Thunderstorm Warning, 60 mph winds, 1" hail
- Lesson: slow-moving storm (10-15 mph) + large hail = TDS false alarm
- TDS pixel count decreasing scan-to-scan confirmed storm weakening

**Fort Stockton TX supercell:**
- Warned cell near I-10/US-67 interchange
- KMAF contaminated by Permian Basin oil infrastructure ground clutter
- KDFX (Del Rio) at long range — beam overshooting, sampling anvil
- Davis Mountains create significant radar gap — spotters essential here

**New Mexico/Texas Panhandle cluster (afternoon):**
- KFDX showed rotation couplet with TDS 56px and Hail 73px
- KLBB Lubbock: Hail 112px — highest count of the day — baseball-sized hail
- Multiple discrete supercells, Severe Thunderstorm Warnings confirmed

**Sierra Nevada fire weather (afternoon):**
- Bishop FFWI 205, RH 9%, 91 mph gusts — off the scale
- Reno FFWI 97, RH 8% — Red Flag conditions
- Haines Index 5 (High) from 12Z REV sounding
- Fire Weather Watch upgraded to Red Flag Warning

---

## 📺 The Sierra Nevada

> *"You've never seen it from the inside."*

The Sierra Nevada runs 400 miles along California's eastern spine. It generates its own weather. It feeds rivers that water 40 million people. It burns — sometimes catastrophically. It shakes. It floods. It hosts the deepest snowpack in North America and some of the most complex terrain-driven convection on the continent.

KRGX, the Reno NEXRAD, sits at **7,807 feet elevation** — one of the highest radar sites in the country — because the terrain blocks ground-level coverage. Even then there are significant beam blockage issues in the deep valleys. The forecasters at WFO Reno are Sierra terrain specialists working with the same data this toolkit uses.

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
| Surface Observations | NWS API | `api.weather.gov/stations/{id}/observations/latest` |
| NEXRAD Level 2 | NOAA / Unidata | `s3://unidata-nexrad-level2` |

---

## 📜 License

MIT. Use it, fork it, build on it. If you extend it to another mountain range, open a PR.

---

## 🙏 Built with

[MetPy](https://unidata.github.io/MetPy/) · [Py-ART](https://arm-doe.github.io/pyart/) · [Tweepy](https://www.tweepy.org/) · [Siphon](https://unidata.github.io/siphon/) · [pixi](https://prefix.dev/) · [GitHub Actions](https://github.com/features/actions) · [Iowa Environmental Mesonet](https://mesonet.agron.iastate.edu/) · [NOAA Open Data Dissemination](https://www.noaa.gov/information-technology/open-data-dissemination)

---

*Built in Lakewood, WA, watching the Sierra Nevada.*
*[@bdgroves](https://twitter.com/bdgroves) · [@SierraNevadaWX](https://twitter.com/SierraNevadaWX)*
