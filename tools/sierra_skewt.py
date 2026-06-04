"""
Sierra Nevada Skew-T Log-P Diagram
===================================
Fetches the latest upper air sounding from stations near the Sierra Nevada
and plots a professional Skew-T with:
  - Temperature and dewpoint profiles
  - Wind barbs
  - Parcel path
  - LCL, LFC, EL markers
  - CAPE / CIN shading
  - Key indices: CAPE, CIN, LCL, LI, PW
  - Hodograph inset

Stations near Sierra Nevada:
  REV  - Reno, NV           (best for eastern Sierra)
  OAK  - Oakland, CA        (best for western Sierra / Bay influence)
  VBG  - Vandenberg, CA     (Southern California / coast)
  SLC  - Salt Lake City, UT (Great Basin context)

Data source: Iowa State Upper Air Archive via Siphon
Soundings launched twice daily: 00Z (5 PM PDT) and 12Z (5 AM PDT)

Usage:
    python sierra_skewt.py              # Latest REV sounding
    python sierra_skewt.py --station OAK
    python sierra_skewt.py --station REV --time 00Z
    python sierra_skewt.py --all        # All 4 stations, save PNGs
"""

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('TkAgg')  # Interactive window; change to 'Agg' to save only
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
import numpy as np

import metpy.calc as mpcalc
from metpy.plots import Hodograph, SkewT
from metpy.units import units, pandas_dataframe_to_unit_arrays

try:
    from siphon.simplewebservice.iastate import IAStateUpperAir
except ImportError:
    print("ERROR: siphon not installed. Run: pip install siphon")
    sys.exit(1)

# ── Station info ──────────────────────────────────────────────────────────────
STATIONS = {
    "REV": {"name": "Reno, NV",           "lat": 39.57, "lon": -119.80,
            "desc": "Eastern Sierra / Great Basin"},
    "OAK": {"name": "Oakland, CA",         "lat": 37.73, "lon": -122.22,
            "desc": "Western Sierra / Marine influence"},
    "VBG": {"name": "Vandenberg, CA",      "lat": 34.74, "lon": -120.57,
            "desc": "Southern California"},
    "SLC": {"name": "Salt Lake City, UT",  "lat": 40.77, "lon": -111.95,
            "desc": "Great Basin context"},
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def latest_sounding_time(prefer_hour=None):
    """Return the most recent 00Z or 12Z datetime."""
    now = datetime.now(timezone.utc)
    if prefer_hour == 0:
        candidate = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif prefer_hour == 12:
        candidate = now.replace(hour=12, minute=0, second=0, microsecond=0)
    else:
        # Pick whichever 00Z/12Z is most recent and at least 2h old
        for offset in [0, 12, 24]:
            for h in [12, 0]:
                t = now.replace(hour=h, minute=0, second=0, microsecond=0)
                t -= timedelta(hours=offset if h > now.hour else 0)
                if (now - t).total_seconds() > 7200:
                    return t
        candidate = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if candidate > now - timedelta(hours=2):
        candidate -= timedelta(hours=12)
    return candidate


def fetch_sounding(station, valid_time, retries=2):
    """Fetch sounding data via Siphon/IEM."""
    for attempt in range(retries):
        try:
            print(f"  Fetching {station} at {valid_time.strftime('%HZ %Y-%m-%d')}...")
            df = IAStateUpperAir.request_data(valid_time, station)
            if df is not None and len(df) > 5:
                print(f"  ✅ Got {len(df)} levels")
                return df, valid_time
        except Exception as e:
            print(f"  ⚠️  Attempt {attempt+1} failed: {e}")
            valid_time -= timedelta(hours=12)
    return None, None


# ── Main plot function ────────────────────────────────────────────────────────
def plot_skewt(station_id, valid_time=None, save_path=None, show=True):
    """Fetch data and plot a full Skew-T for the given station."""

    if valid_time is None:
        valid_time = latest_sounding_time()

    info = STATIONS.get(station_id.upper(), {
        "name": station_id,
        "desc": "Custom station",
        "lat": None, "lon": None
    })

    df, actual_time = fetch_sounding(station_id.upper(), valid_time)
    if df is None:
        print(f"❌ Could not retrieve sounding for {station_id}")
        return None

    # ── Extract variables with units ──────────────────────────────────────────
    # Drop rows with missing mandatory fields
    df = df.dropna(subset=['pressure', 'temperature', 'dewpoint'])
    df = df[df['pressure'] > 10]  # Remove stratospheric junk

    p   = df['pressure'].values * units.hPa
    T   = df['temperature'].values * units.degC
    Td  = df['dewpoint'].values * units.degC

    # Wind (may have NaNs at some levels)
    wind_mask = (~np.isnan(df['speed'].values)) & (~np.isnan(df['direction'].values))
    p_wind    = df['pressure'].values[wind_mask] * units.hPa
    wspd      = df['speed'].values[wind_mask] * units.knots
    wdir      = df['direction'].values[wind_mask] * units.degrees
    u, v      = mpcalc.wind_components(wspd, wdir)

    # ── Calculate thermodynamic parameters ────────────────────────────────────
    try:
        lcl_p, lcl_t         = mpcalc.lcl(p[0], T[0], Td[0])
        lfc_p, lfc_t         = mpcalc.lfc(p, T, Td)
        el_p, el_t           = mpcalc.el(p, T, Td)
        parcel_prof          = mpcalc.parcel_profile(p, T[0], Td[0]).to('degC')
        cape, cin            = mpcalc.cape_cin(p, T, Td, parcel_prof)
        pw                   = mpcalc.precipitable_water(p, Td)
        li                   = mpcalc.lifted_index(p, T, parcel_prof)
    except Exception as e:
        print(f"  ⚠️  Some indices could not be calculated: {e}")
        lcl_p = lcl_t = lfc_p = el_p = parcel_prof = None
        cape = cin = pw = li = None

    # ── Build figure ──────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(10, 12))
    fig.patch.set_facecolor('#0d1117')  # Dark background

    skew = SkewT(fig, rotation=45, subplot=(1, 1, 1))
    ax   = skew.ax
    ax.set_facecolor('#0d1117')
    ax.tick_params(colors='white')
    ax.spines[:].set_color('#444')
    ax.set_ylim(1020, 100)
    ax.set_xlim(-40, 60)

    # Reference lines
    skew.plot_dry_adiabats(colors='#2a4a2a', linewidth=0.5, alpha=0.6)
    skew.plot_moist_adiabats(colors='#1a3a4a', linewidth=0.5, alpha=0.6)
    skew.plot_mixing_lines(colors='#3a3a1a', linewidth=0.5, alpha=0.6)

    # Freezing line
    skew.ax.axvline(0, color='cyan', linestyle='--', linewidth=0.8, alpha=0.5)

    # Temperature and dewpoint
    skew.plot(p, T,  '#FF4444', linewidth=2.5, label='Temperature')
    skew.plot(p, Td, '#44AA44', linewidth=2.5, label='Dewpoint')

    # Wind barbs
    skew.plot_barbs(p_wind[::2], u[::2], v[::2],
                    color='white', length=6, linewidth=0.8)

    # Parcel path + CAPE/CIN shading
    if parcel_prof is not None:
        skew.plot(p, parcel_prof, 'white', linewidth=1.5,
                  linestyle='--', label='Parcel Path', alpha=0.8)
        skew.shade_cape(p, T, parcel_prof)
        skew.shade_cin(p, T, parcel_prof)

    # Key level markers
    if lcl_p is not None:
        skew.plot(lcl_p, lcl_t, 'o', color='gold',
                  markersize=8, label=f'LCL {lcl_p:.0f}')
    if lfc_p is not None:
        try:
            skew.plot(lfc_p, lfc_t, '^', color='orange',
                      markersize=10, label=f'LFC {lfc_p:.0f}')
        except Exception:
            pass
    if el_p is not None:
        try:
            skew.plot(el_p, el_t, 'v', color='red',
                      markersize=10, label=f'EL {el_p:.0f}')
        except Exception:
            pass

    # ── Hodograph inset ───────────────────────────────────────────────────────
    ax_hodo = inset_axes(ax, '30%', '30%', loc=1)
    ax_hodo.set_facecolor('#0d1117')
    ax_hodo.tick_params(colors='#888', labelsize=7)
    ax_hodo.spines[:].set_color('#444')
    h = Hodograph(ax_hodo, component_range=80)
    h.add_grid(increment=20, color='#333')
    if len(u) > 3:
        # Color by altitude
        h.plot_colormapped(u, v, wspd.magnitude)

    # ── Indices text box ──────────────────────────────────────────────────────
    indices_lines = []
    if cape is not None:
        indices_lines += [
            f"CAPE: {cape.magnitude:.0f} J/kg",
            f"CIN:  {cin.magnitude:.0f} J/kg",
        ]
    if pw is not None:
        indices_lines.append(f"PW:   {pw.to('inch').magnitude:.2f} in")
    if li is not None:
        try:
            indices_lines.append(f"LI:   {li.magnitude[0]:.1f} °C")
        except Exception:
            pass
    if lcl_p is not None:
        indices_lines.append(f"LCL:  {lcl_p.magnitude:.0f} hPa")

    if indices_lines:
        txt = "\n".join(indices_lines)
        ax.text(0.02, 0.02, txt,
                transform=ax.transAxes,
                fontsize=9, fontfamily='monospace',
                color='white', verticalalignment='bottom',
                bbox=dict(facecolor='#1a1a2e', alpha=0.8,
                          edgecolor='#444', boxstyle='round,pad=0.4'))

    # ── Title ─────────────────────────────────────────────────────────────────
    pdt_offset = -7  # PDT
    local_time = actual_time + timedelta(hours=pdt_offset)
    title1 = (f"{station_id.upper()} — {info['name']}")
    title2 = (f"{actual_time.strftime('%HZ')} "
              f"({local_time.strftime('%I:%M %p')} PDT)  "
              f"{actual_time.strftime('%B %d, %Y')}  |  {info['desc']}")

    ax.set_title(title1, color='white', fontsize=14, fontweight='bold', pad=12)
    ax.set_xlabel("Temperature (°C)", color='white')
    ax.set_ylabel("Pressure (hPa)", color='white')
    ax.text(0.5, 1.04, title2, transform=ax.transAxes,
            fontsize=9, color='#aaa', ha='center')

    # Legend
    ax.legend(loc='upper right', fontsize=8,
              facecolor='#1a1a2e', edgecolor='#444', labelcolor='white')

    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight',
                    facecolor='#0d1117')
        print(f"  💾 Saved: {save_path}")

    if show:
        plt.show()

    return fig


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sierra Nevada Skew-T Sounding Plots"
    )
    parser.add_argument("--station", default="REV",
                        help="Station ID (REV, OAK, VBG, SLC)")
    parser.add_argument("--time", choices=["00Z", "12Z"],
                        help="Sounding time (default: most recent)")
    parser.add_argument("--all", action="store_true",
                        help="Plot all 4 Sierra stations and save PNGs")
    parser.add_argument("--save", metavar="FILE",
                        help="Save plot to file instead of showing")
    args = parser.parse_args()

    prefer_hour = None
    if args.time == "00Z":
        prefer_hour = 0
    elif args.time == "12Z":
        prefer_hour = 12

    valid = latest_sounding_time(prefer_hour)

    if args.all:
        out_dir = Path("skewt_output")
        out_dir.mkdir(exist_ok=True)
        for stn in STATIONS:
            fname = out_dir / f"skewt_{stn}_{valid.strftime('%Y%m%d_%HZ')}.png"
            print(f"\n{'='*50}")
            print(f"Plotting {stn}...")
            plot_skewt(stn, valid, save_path=str(fname), show=False)
        print(f"\n✅ All soundings saved to {out_dir}/")
    else:
        save = args.save or None
        show = save is None
        print(f"\nPlotting {args.station.upper()} sounding...")
        plot_skewt(args.station, valid, save_path=save, show=show)
