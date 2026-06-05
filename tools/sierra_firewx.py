"""
Sierra Nevada Fire Weather Dashboard
======================================
Pulls live surface observations from NWS API for Sierra Nevada stations
and the latest REV upper air sounding, then calculates:

  1. Fosberg Fire Weather Index (FFWI)
     The primary index used by CAL FIRE and USFS for Red Flag criteria.
     > 50 = significant danger, > 75 = critical

  2. Haines Index
     Measures atmospheric stability and dryness aloft — how much a fire
     already burning can grow and become erratic.
     2-3 = very low, 4 = moderate, 5 = high, 6 = very high

  3. Hot-Dry-Windy Index (HDW)
     Combines vapor pressure deficit and wind speed — the two most
     critical physical drivers of fire spread.
     > 40 = moderate, > 80 = high, > 160 = extreme

  4. Red Flag criteria check
     NWS Red Flag Warning requires: RH < 15%, winds > 25 mph (sustained
     or gusts), and receptive fuels. This tool auto-flags when criteria met.

Sierra monitoring stations (NWS ASOS/AWOS):
  KRNO  Reno-Tahoe International   (eastern Sierra base)
  KTVL  Lake Tahoe Airport         (high Sierra, 6,264 ft)
  KMMH  Mammoth Yosemite Airport   (central Sierra, 7,135 ft)
  KBLU  Blue Canyon               (western Sierra, 5,284 ft)
  KBIH  Bishop                    (Owens Valley, eastern Sierra)
  KSNS  Salinas                   (marine influence reference)

Usage:
  pixi run firewx                  # All stations, current conditions
  python tools/sierra_firewx.py --station KRNO
  python tools/sierra_firewx.py --all --save
  python tools/sierra_firewx.py --post   # Post to @SierraNevadaWX if Red Flag
"""

import argparse, sys, os, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch
import matplotlib.patheffects as pe

try:
    import requests
except ImportError:
    import urllib.request as _ur
    requests = None

try:
    from siphon.simplewebservice.iastate import IAStateUpperAir
    HAS_SIPHON = True
except ImportError:
    HAS_SIPHON = False

from metpy.units import units

# ── Style ─────────────────────────────────────────────────────────────────────
BG    = '#0d1117'
BG2   = '#161b22'
WHITE = '#e6edf3'
MUTED = '#8b949e'
RED   = '#ff4444'
ORANGE= '#ff8c00'
GOLD  = '#ffd43b'
GREEN = '#51cf66'
CYAN  = '#66d9e8'

# ── Sierra monitoring stations ────────────────────────────────────────────────
STATIONS = {
    'KRNO': {'name': 'Reno',          'elev_ft': 4415, 'desc': 'Eastern Sierra base'},
    'KTVL': {'name': 'Lake Tahoe',    'elev_ft': 6264, 'desc': 'High Sierra / Tahoe'},
    'KMMH': {'name': 'Mammoth Lakes', 'elev_ft': 7135, 'desc': 'Central Sierra'},
    'KBLU': {'name': 'Blue Canyon',   'elev_ft': 5284, 'desc': 'W Sierra / I-80'},
    'KBIH': {'name': 'Bishop',        'elev_ft': 4124, 'desc': 'Owens Valley'},
}

# ── Fire weather calculations ─────────────────────────────────────────────────
def fosberg_ffwi(temp_f: float, rh: float, wind_mph: float) -> float:
    """
    Fosberg Fire Weather Index (0-100+)
    Based on equilibrium moisture content of fine fuels.
    Primary index for NWS Red Flag Warning issuance.
    """
    if rh < 10:
        emc = 0.03229 + 0.281073*rh - 0.000578*temp_f*rh
    elif rh < 50:
        emc = 2.22749 + 0.160107*rh - 0.014784*temp_f
    else:
        emc = 21.0606 + 0.005565*rh**2 - 0.00035*rh*temp_f - 0.483199*rh

    emc  = emc / 30.0
    eta  = 1 - 2*emc + 1.5*emc**2 - 0.5*emc**3
    ffwi = eta * np.sqrt(1 + wind_mph**2) / 0.3002
    return round(max(0, ffwi), 1)


def hdw_index(temp_f: float, rh: float, wind_mph: float) -> float:
    """
    Hot-Dry-Windy Index = VPD × wind speed
    Developed by USFS to capture the two key fire spread drivers.
    """
    temp_c = (temp_f - 32) * 5/9
    es     = 6.112 * np.exp(17.67 * temp_c / (temp_c + 243.5))
    e      = es * rh / 100
    vpd    = es - e
    return round(vpd * wind_mph, 1)


def haines_index(t850: float, t700: float, td850: float,
                 elevation_ft: float = 4000) -> int:
    """
    Haines Index (2-6) using low/mid elevation version.
    Low elev (<2000 ft) uses 950/850.
    Mid elev (2000-6000 ft) uses 850/700.
    High elev (>6000 ft) uses 700/500.
    Sierra stations mostly mid/high elevation.
    """
    # Mid-elevation version (most Sierra stations)
    a = t850 - t700    # stability term
    b = t850 - td850   # moisture term

    A = 1 if a <= 3 else (2 if a <= 7 else 3)
    B = 1 if b <= 5 else (2 if b <= 10 else 3)
    return A + B


def red_flag_check(temp_f: float, rh: float,
                   wind_mph: float, wind_gust_mph: float) -> dict:
    """
    Check NWS Red Flag Warning criteria for California/Nevada.
    Criteria: RH < 15% AND (sustained wind > 25 mph OR gusts > 35 mph)
    """
    rh_crit    = rh < 15
    wind_crit  = wind_mph > 25 or wind_gust_mph > 35
    temp_crit  = temp_f > 75
    return {
        'red_flag':  rh_crit and wind_crit,
        'watch':     rh_crit and (wind_mph > 20 or wind_gust_mph > 30),
        'rh_crit':   rh_crit,
        'wind_crit': wind_crit,
        'temp_crit': temp_crit,
    }


# ── Data fetching ─────────────────────────────────────────────────────────────
def fetch_obs(station_id: str) -> dict | None:
    """Fetch latest surface observation from NWS API."""
    url = f"https://api.weather.gov/stations/{station_id}/observations/latest"
    headers = {"User-Agent": "SierraNevadaWX/1.0 (github.com/bdgroves/sierra-alert-bot)"}
    try:
        if requests:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
        else:
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

        props = data['properties']
        temp_c     = props.get('temperature', {}).get('value')
        dewp_c     = props.get('dewpoint', {}).get('value')
        wind_ms    = props.get('windSpeed', {}).get('value')
        gust_ms    = props.get('windGust', {}).get('value')
        rh         = props.get('relativeHumidity', {}).get('value')
        timestamp  = props.get('timestamp', '')

        if temp_c is None or dewp_c is None:
            return None

        temp_f     = temp_c * 9/5 + 32
        dewp_f     = dewp_c * 9/5 + 32
        wind_mph   = (wind_ms or 0) * 2.237
        gust_mph   = (gust_ms or 0) * 2.237
        if rh is None:
            rh = max(0, min(100, 100 * (
                np.exp(17.625*dewp_c/(243.04+dewp_c)) /
                np.exp(17.625*temp_c/(243.04+temp_c))
            )))

        return {
            'temp_f':    round(temp_f, 1),
            'dewp_f':    round(dewp_f, 1),
            'rh':        round(rh, 1),
            'wind_mph':  round(wind_mph, 1),
            'gust_mph':  round(gust_mph, 1),
            'timestamp': timestamp,
        }
    except Exception as e:
        print(f"  ⚠  {station_id} obs error: {e}")
        return None


def fetch_upper_air_haines() -> int | None:
    """Fetch REV sounding and calculate Haines Index."""
    if not HAS_SIPHON:
        return None
    now = datetime.now(timezone.utc)
    for delta in range(4):
        for hour in [12, 0]:
            t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            t -= timedelta(hours=delta * 12)
            if (now - t).total_seconds() < 7200:
                continue
            try:
                df = IAStateUpperAir.request_data(t, 'REV')
                if df is None or len(df) < 10:
                    continue
                # Get 850 and 700 hPa values
                p  = df['pressure'].values
                T  = df['temperature'].values
                Td = df['dewpoint'].values

                i850 = np.argmin(np.abs(p - 850))
                i700 = np.argmin(np.abs(p - 700))

                t850  = float(T[i850])
                t700  = float(T[i700])
                td850 = float(Td[i850])

                h = haines_index(t850, t700, td850)
                print(f"  Haines from REV {t.strftime('%HZ %Y-%m-%d')}: "
                      f"T850={t850:.1f} T700={t700:.1f} Td850={td850:.1f} → {h}")
                return h
            except Exception:
                continue
    return None


# ── Plotting ──────────────────────────────────────────────────────────────────
def ffwi_color(v):
    if v >= 75: return RED
    if v >= 50: return ORANGE
    if v >= 25: return GOLD
    return GREEN

def hdw_color(v):
    if v >= 160: return RED
    if v >= 80:  return ORANGE
    if v >= 40:  return GOLD
    return GREEN

def haines_color(v):
    if v >= 6: return RED
    if v >= 5: return ORANGE
    if v >= 4: return GOLD
    return GREEN

def rh_color(v):
    if v <= 10: return RED
    if v <= 15: return ORANGE
    if v <= 25: return GOLD
    return GREEN


def plot_firewx(results: list, haines: int | None,
                save_path: str | None = None, show: bool = True):
    """Plot the Sierra fire weather dashboard."""

    n = len(results)
    if n == 0:
        print("No data to plot.")
        return

    now_str = datetime.now(timezone.utc).strftime('%HZ %B %d, %Y')

    fig = plt.figure(figsize=(12, 8), dpi=130, facecolor=BG)
    gs  = gridspec.GridSpec(3, n, figure=fig,
                            left=0.05, right=0.97,
                            top=0.88, bottom=0.06,
                            wspace=0.3, hspace=0.55)

    # Row labels
    row_labels = ['FFWI', 'HDW', 'RH / Wind']

    for col, r in enumerate(results):
        stn   = r['station']
        info  = STATIONS.get(stn, {'name': stn, 'elev_ft': 0, 'desc': ''})
        obs   = r['obs']
        ffwi  = r['ffwi']
        hdw   = r['hdw']
        rf    = r['red_flag']

        # Column header
        fig.text((col + 0.5) / n * 0.92 + 0.05, 0.915,
                 f"{stn}  {info['name']}",
                 ha='center', color=WHITE, fontsize=10, fontweight='bold')
        fig.text((col + 0.5) / n * 0.92 + 0.05, 0.897,
                 f"{info['elev_ft']:,} ft  ·  {info['desc']}",
                 ha='center', color=MUTED, fontsize=8)

        # Red Flag banner
        if rf['red_flag']:
            fig.text((col + 0.5) / n * 0.92 + 0.05, 0.877,
                     '🔥 RED FLAG',
                     ha='center', color=RED, fontsize=9, fontweight='bold')
        elif rf['watch']:
            fig.text((col + 0.5) / n * 0.92 + 0.05, 0.877,
                     '⚠️  WATCH',
                     ha='center', color=ORANGE, fontsize=9)

        # ── Row 0: FFWI gauge ─────────────────────────────────────────────
        ax0 = fig.add_subplot(gs[0, col])
        ax0.set_facecolor(BG2)
        ax0.set_xlim(0, 100); ax0.set_ylim(0, 1)
        ax0.set_xticks([]); ax0.set_yticks([])
        for sp in ax0.spines.values(): sp.set_color('#333')

        # Background danger zones
        for (x0, x1, c, lbl) in [(0,25,'#1a3a1a','Low'),
                                   (25,50,'#3a3a00','Mod'),
                                   (50,75,'#3a2000','High'),
                                   (75,100,'#3a0000','Crit')]:
            ax0.axvspan(x0, x1, alpha=0.4, color=c)
            ax0.text((x0+x1)/2, 0.08, lbl, ha='center',
                     fontsize=6, color='#555')

        # Value bar
        ax0.barh(0.5, min(ffwi, 100), height=0.35,
                 color=ffwi_color(ffwi), alpha=0.9)
        ax0.axvline(min(ffwi, 100), color=ffwi_color(ffwi),
                    linewidth=2, alpha=0.8)
        ax0.text(50, 0.82, f'FFWI  {ffwi}',
                 ha='center', color=ffwi_color(ffwi),
                 fontsize=11, fontweight='bold')

        # ── Row 1: HDW gauge ──────────────────────────────────────────────
        ax1 = fig.add_subplot(gs[1, col])
        ax1.set_facecolor(BG2)
        ax1.set_xlim(0, 300); ax1.set_ylim(0, 1)
        ax1.set_xticks([]); ax1.set_yticks([])
        for sp in ax1.spines.values(): sp.set_color('#333')

        for (x0, x1, c) in [(0,40,'#1a3a1a'),(40,80,'#3a3a00'),
                              (80,160,'#3a2000'),(160,300,'#3a0000')]:
            ax1.axvspan(x0, x1, alpha=0.4, color=c)

        ax1.barh(0.5, min(hdw, 300), height=0.35,
                 color=hdw_color(hdw), alpha=0.9)
        ax1.text(150, 0.82, f'HDW  {hdw}',
                 ha='center', color=hdw_color(hdw),
                 fontsize=11, fontweight='bold')

        # ── Row 2: RH + Wind text ─────────────────────────────────────────
        ax2 = fig.add_subplot(gs[2, col])
        ax2.set_facecolor(BG2)
        ax2.set_xlim(0, 1); ax2.set_ylim(0, 1)
        ax2.set_xticks([]); ax2.set_yticks([])
        for sp in ax2.spines.values(): sp.set_color('#333')

        rh_c = rh_color(obs['rh'])
        ax2.text(0.5, 0.78, f"{obs['temp_f']:.0f}°F",
                 ha='center', color=ORANGE, fontsize=13, fontweight='bold')
        ax2.text(0.5, 0.55, f"RH  {obs['rh']:.0f}%",
                 ha='center', color=rh_c, fontsize=11, fontweight='bold')
        ax2.text(0.5, 0.33,
                 f"Wind  {obs['wind_mph']:.0f} mph",
                 ha='center', color=CYAN, fontsize=10)
        if obs['gust_mph'] > 0:
            ax2.text(0.5, 0.14,
                     f"Gusts  {obs['gust_mph']:.0f} mph",
                     ha='center', color=CYAN, fontsize=9, alpha=0.8)

    # ── Haines panel (spans full width at bottom) ─────────────────────────
    fig.text(0.5, 0.03,
             f"Haines Index (REV sounding):  "
             f"{haines if haines else 'N/A'}  "
             f"{'— ' + ['','','Very Low','Low','Moderate','High','Very High'][haines] if haines else ''}",
             ha='center',
             color=haines_color(haines) if haines else MUTED,
             fontsize=10, fontweight='bold')

    # ── Title ─────────────────────────────────────────────────────────────
    fig.text(0.5, 0.965,
             'Sierra Nevada Fire Weather Indices',
             ha='center', color=WHITE, fontsize=14, fontweight='bold')
    fig.text(0.5, 0.945,
             f'{now_str}  ·  NWS Surface Obs + REV Upper Air Sounding',
             ha='center', color=MUTED, fontsize=9)
    fig.text(0.5, 0.927,
             'FFWI > 50 = significant  ·  FFWI > 75 = critical  ·  '
             'Red Flag: RH < 15% AND winds > 25 mph',
             ha='center', color='#555', fontsize=8)

    # ── Footer ────────────────────────────────────────────────────────────
    fig.text(0.5, 0.005,
             '@SierraNevadaWX  ·  Data: NWS API + Iowa State RAOB  ·  '
             'Indices: Fosberg 1978, Haines 1988, Srock et al. 2018',
             ha='center', color='#444', fontsize=7)

    if save_path:
        plt.savefig(save_path, dpi=130, bbox_inches='tight', facecolor=BG)
        print(f"  💾 {save_path}")
    if show:
        plt.show()
    plt.close(fig)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='Sierra Nevada Fire Weather Dashboard'
    )
    ap.add_argument('--station', default=None,
                    help='Single station ID (e.g. KRNO)')
    ap.add_argument('--all', action='store_true',
                    help='All Sierra stations (default)')
    ap.add_argument('--save', action='store_true',
                    help='Save PNG to firewx_output/')
    ap.add_argument('--post', action='store_true',
                    help='Post to Twitter if Red Flag criteria met')
    args = ap.parse_args()

    stations = ([args.station] if args.station
                else list(STATIONS.keys()))

    print(f"\n{'='*55}")
    print(f"Sierra Nevada Fire Weather  —  "
          f"{datetime.now(timezone.utc).strftime('%HZ %b %d, %Y')}")
    print(f"{'='*55}")

    # Fetch upper air for Haines
    print("\nFetching REV upper air sounding for Haines Index...")
    haines = fetch_upper_air_haines()

    # Fetch surface obs and calculate indices
    results = []
    for stn in stations:
        print(f"\nFetching {stn}...")
        obs = fetch_obs(stn)
        if obs is None:
            print(f"  ⚠  No data for {stn}")
            continue

        ffwi = fosberg_ffwi(obs['temp_f'], obs['rh'], obs['wind_mph'])
        hdw  = hdw_index(obs['temp_f'], obs['rh'], obs['wind_mph'])
        rf   = red_flag_check(obs['temp_f'], obs['rh'],
                               obs['wind_mph'], obs['gust_mph'])

        print(f"  {obs['temp_f']:.0f}°F  RH:{obs['rh']:.0f}%  "
              f"Wind:{obs['wind_mph']:.0f}mph  Gusts:{obs['gust_mph']:.0f}mph")
        print(f"  FFWI:{ffwi}  HDW:{hdw}  "
              f"{'🔥 RED FLAG!' if rf['red_flag'] else '⚠️  WATCH' if rf['watch'] else 'OK'}")

        results.append({
            'station': stn,
            'obs':     obs,
            'ffwi':    ffwi,
            'hdw':     hdw,
            'red_flag': rf,
        })

    if not results:
        print("\n❌ No data retrieved — check network connection")
        sys.exit(1)

    # Any Red Flag stations?
    rf_stations = [r for r in results if r['red_flag']['red_flag']]
    if rf_stations:
        print(f"\n🔥 RED FLAG CONDITIONS at: "
              f"{', '.join(r['station'] for r in rf_stations)}")
    else:
        print("\n✅ No Red Flag conditions currently")

    # Plot
    save = None
    if args.save:
        out = Path('firewx_output'); out.mkdir(exist_ok=True)
        save = str(out / f"firewx_{datetime.now(timezone.utc).strftime('%Y%m%d_%HZ')}.png")

    plot_firewx(results, haines,
                save_path=save,
                show=(save is None or not args.save))
