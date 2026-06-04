"""
Sierra Nevada Skew-T Log-P Diagram
====================================
Fetches the latest upper air sounding and plots a Twitter-ready Skew-T with:
  - Temperature / dewpoint profiles
  - Wind barbs
  - Parcel path + CAPE/CIN shading
  - LCL, LFC, EL markers
  - Hodograph inset
  - Key indices: CAPE, CIN, LI, PW, LCL
  - Clean dark theme suitable for social media

Stations:
  REV  Reno, NV           (Eastern Sierra / Great Basin)
  OAK  Oakland, CA        (Western Sierra / Marine influence)
  VBG  Vandenberg, CA     (Southern California)
  SLC  Salt Lake City, UT (Great Basin context)

Soundings launch twice daily: 00Z (5 PM PDT) and 12Z (5 AM PDT)

Usage:
  pixi run skewt                           # Latest REV sounding
  python tools/sierra_skewt.py --station OAK
  python tools/sierra_skewt.py --all       # All stations -> skewt_output/
  python tools/sierra_skewt.py --time 00Z
"""

import argparse, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.patheffects as pe
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
import numpy as np

import metpy.calc as mpcalc
from metpy.plots import Hodograph, SkewT
from metpy.units import units

try:
    from siphon.simplewebservice.iastate import IAStateUpperAir
except ImportError:
    print("ERROR: run  pixi install  to get siphon")
    sys.exit(1)

# ── Colour palette ────────────────────────────────────────────────────────────
BG      = '#0d1117'
BG2     = '#161b22'
BORDER  = '#30363d'
RED     = '#ff6b6b'
GREEN   = '#51cf66'
WHITE   = '#e6edf3'
MUTED   = '#8b949e'
GOLD    = '#ffd43b'
ORANGE  = '#fd7e14'
CYAN    = '#66d9e8'

# ── Station registry ──────────────────────────────────────────────────────────
STATIONS = {
    'REV': {'name': 'Reno, NV',          'desc': 'Eastern Sierra / Great Basin'},
    'OAK': {'name': 'Oakland, CA',        'desc': 'Western Sierra / Marine Influence'},
    'VBG': {'name': 'Vandenberg, CA',     'desc': 'Southern California'},
    'SLC': {'name': 'Salt Lake City, UT', 'desc': 'Great Basin Context'},
}

# ── Helpers ───────────────────────────────────────────────────────────────────
def latest_time(prefer=None):
    now = datetime.now(timezone.utc)
    for delta_h in range(0, 48, 12):
        for h in [12, 0]:
            t = now.replace(hour=h, minute=0, second=0, microsecond=0)
            t -= timedelta(hours=delta_h)
            if (now - t).total_seconds() > 7200:
                if prefer is None or t.hour == prefer:
                    return t
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def fetch(station, valid, retries=3):
    for _ in range(retries):
        try:
            print(f"  Fetching {station} @ {valid.strftime('%HZ %Y-%m-%d')} ...")
            df = IAStateUpperAir.request_data(valid, station)
            if df is not None and len(df) > 10:
                print(f"  ✅ {len(df)} levels")
                return df, valid
        except Exception as e:
            print(f"  ⚠  {e}")
            valid -= timedelta(hours=12)
    return None, None


# ── Core plot ─────────────────────────────────────────────────────────────────
def plot_skewt(station_id, valid_time=None, save_path=None, show=True):
    if valid_time is None:
        valid_time = latest_time()

    info = STATIONS.get(station_id.upper(),
                        {'name': station_id, 'desc': 'Custom station'})

    df, actual_time = fetch(station_id.upper(), valid_time)
    if df is None:
        print(f"❌ No data for {station_id}")
        return None

    # Clean data
    df = df.dropna(subset=['pressure', 'temperature', 'dewpoint'])
    df = df[df['pressure'] > 10].reset_index(drop=True)

    p   = df['pressure'].values   * units.hPa
    T   = df['temperature'].values * units.degC
    Td  = df['dewpoint'].values    * units.degC

    wind_ok = (~np.isnan(df['speed'].values)) & (~np.isnan(df['direction'].values))
    p_w = df['pressure'].values[wind_ok]   * units.hPa
    spd = df['speed'].values[wind_ok]      * units.knots
    drn = df['direction'].values[wind_ok]  * units.degrees
    u, v = mpcalc.wind_components(spd, drn)

    # Thermodynamic indices
    lcl_p = lcl_t = lfc_p = lfc_t = el_p = el_t = None
    parcel = cape = cin = pw = li = None
    try:
        lcl_p, lcl_t = mpcalc.lcl(p[0], T[0], Td[0])
        parcel       = mpcalc.parcel_profile(p, T[0], Td[0]).to('degC')
        cape, cin    = mpcalc.cape_cin(p, T, Td, parcel)
        pw           = mpcalc.precipitable_water(p, Td)
        li           = mpcalc.lifted_index(p, T, parcel)
    except Exception as e:
        print(f"  ⚠  Indices: {e}")
    try:
        lfc_p, lfc_t = mpcalc.lfc(p, T, Td)
    except Exception:
        pass
    try:
        el_p, el_t = mpcalc.el(p, T, Td)
    except Exception:
        pass

    # ── Figure layout ─────────────────────────────────────────────────────────
    # 1200 × 1350 px @ 150 dpi = 8 × 9 in  — good for Twitter
    fig = plt.figure(figsize=(8, 9), dpi=150, facecolor=BG)

    # Skew-T occupies left 75%, hodograph top-right, indices bottom-right
    gs = gridspec.GridSpec(2, 2, figure=fig,
                           left=0.08, right=0.96,
                           top=0.91, bottom=0.07,
                           wspace=0.35, hspace=0.0,
                           height_ratios=[1, 1],
                           width_ratios=[3, 1])

    # Skew-T spans full height on left
    ax_skew = fig.add_subplot(gs[:, 0])
    skew    = SkewT(fig, rotation=45, subplot=ax_skew)
    ax      = skew.ax

    # Hodograph top-right
    ax_hodo = fig.add_subplot(gs[0, 1])

    # Indices bottom-right
    ax_idx  = fig.add_subplot(gs[1, 1])

    # ── Skew-T styling ────────────────────────────────────────────────────────
    ax.set_facecolor(BG)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.set_xlabel('Temperature (°C)', color=MUTED, fontsize=9, labelpad=6)
    ax.set_ylabel('Pressure (hPa)',   color=MUTED, fontsize=9, labelpad=6)
    ax.set_ylim(1020, 100)
    ax.set_xlim(-40, 60)

    # Reference lines
    skew.plot_dry_adiabats(   colors='#1e3a1e', linewidth=0.6, alpha=0.7)
    skew.plot_moist_adiabats( colors='#1a2e3a', linewidth=0.6, alpha=0.7)
    skew.plot_mixing_lines(   colors='#2e2e1a', linewidth=0.5, alpha=0.6)

    # Freezing line
    ax.axvline(0, color=CYAN, linestyle='--', linewidth=0.7, alpha=0.4)

    # Data lines
    skew.plot(p, T,  RED,   linewidth=2.2)
    skew.plot(p, Td, GREEN, linewidth=2.2)

    # Parcel + shading
    if parcel is not None:
        skew.plot(p, parcel, WHITE, linewidth=1.4, linestyle='--', alpha=0.7)
        skew.shade_cape(p, T, parcel)
        skew.shade_cin(p, T, parcel)

    # Wind barbs — every other level, cleaner
    thin = slice(None, None, 2)
    skew.plot_barbs(p_w[thin], u[thin], v[thin],
                    color=WHITE, length=5.5, linewidth=0.7, alpha=0.85)

    # Level markers
    marker_kw = dict(zorder=5, markersize=8)
    if lcl_p is not None:
        skew.plot(lcl_p, lcl_t, 'o', color=GOLD,   label='LCL', **marker_kw)
    if lfc_p is not None:
        skew.plot(lfc_p, lfc_t, '^', color=ORANGE, label='LFC', **marker_kw)
    if el_p is not None:
        skew.plot(el_p,  el_t,  'v', color=RED,    label='EL',  **marker_kw)

    # Marker legend (small, bottom-left of skew-T)
    handles = []
    from matplotlib.lines import Line2D
    if lcl_p is not None:
        handles.append(Line2D([0],[0], marker='o', color='none',
                               markerfacecolor=GOLD,   markersize=7, label='LCL'))
    if lfc_p is not None:
        handles.append(Line2D([0],[0], marker='^', color='none',
                               markerfacecolor=ORANGE, markersize=7, label='LFC'))
    if el_p is not None:
        handles.append(Line2D([0],[0], marker='v', color='none',
                               markerfacecolor=RED,    markersize=7, label='EL'))
    handles += [
        Line2D([0],[0], color=RED,   linewidth=2, label='Temp'),
        Line2D([0],[0], color=GREEN, linewidth=2, label='Dewpt'),
        Line2D([0],[0], color=WHITE, linewidth=1.4, linestyle='--', label='Parcel'),
    ]
    if handles:
        leg = ax.legend(handles=handles, loc='lower left', fontsize=7,
                        facecolor=BG2, edgecolor=BORDER, labelcolor=WHITE,
                        framealpha=0.9, ncol=2,
                        handlelength=1.4, handletextpad=0.4,
                        columnspacing=0.8, borderpad=0.6)

    # ── Hodograph ─────────────────────────────────────────────────────────────
    ax_hodo.set_facecolor(BG)
    for sp in ax_hodo.spines.values():
        sp.set_color(BORDER)
    ax_hodo.tick_params(colors=MUTED, labelsize=6)
    ax_hodo.set_title('Hodograph', color=MUTED, fontsize=8, pad=4)

    h = Hodograph(ax_hodo, component_range=60)
    h.add_grid(increment=20, color=BORDER, linewidth=0.5)
    if len(u) > 3:
        # Colour by pressure level: surface=warm, upper=cool
        norm_p = (p_w.magnitude - p_w.magnitude.min()) / \
                 (p_w.magnitude.max() - p_w.magnitude.min() + 1e-9)
        h.plot_colormapped(u, v, norm_p, cmap='plasma')

    # Label 0 km ring
    ax_hodo.text(0, 20, '20', color=MUTED, fontsize=5,
                 ha='center', va='bottom')
    ax_hodo.text(0, 40, '40 kt', color=MUTED, fontsize=5,
                 ha='center', va='bottom')

    # ── Indices panel ─────────────────────────────────────────────────────────
    ax_idx.set_facecolor(BG2)
    ax_idx.set_xticks([]); ax_idx.set_yticks([])
    for sp in ax_idx.spines.values():
        sp.set_color(BORDER)

    rows = []
    if cape is not None:
        cape_mag = cape.magnitude
        cin_mag  = cin.magnitude
        cape_col = ('#ff4444' if cape_mag > 1000 else
                    '#ffa500' if cape_mag > 500  else
                    '#51cf66' if cape_mag > 100  else WHITE)
        rows.append(('CAPE', f'{cape_mag:.0f} J/kg', cape_col))
        rows.append(('CIN',  f'{cin_mag:.0f} J/kg',  CYAN))
    if pw is not None:
        rows.append(('PW', f'{pw.to("inch").magnitude:.2f} in', '#74c0fc'))
    if li is not None:
        try:
            li_val = float(li.magnitude.flat[0])
            li_col = '#ff4444' if li_val < -3 else '#ffa500' if li_val < 0 else WHITE
            rows.append(('LI', f'{li_val:.1f} °C', li_col))
        except Exception:
            pass
    if lcl_p is not None:
        rows.append(('LCL', f'{lcl_p.magnitude:.0f} hPa', GOLD))

    n = len(rows)
    for i, (label, value, color) in enumerate(rows):
        y = 1 - (i + 0.5) / max(n, 1)
        ax_idx.text(0.08, y, label, transform=ax_idx.transAxes,
                    fontsize=8, color=MUTED, fontfamily='monospace',
                    va='center', ha='left')
        ax_idx.text(0.92, y, value, transform=ax_idx.transAxes,
                    fontsize=8, color=color, fontfamily='monospace',
                    va='center', ha='right', fontweight='bold')

    ax_idx.set_title('Indices', color=MUTED, fontsize=8, pad=4)

    # ── Title & footer ────────────────────────────────────────────────────────
    pdt_h = actual_time.hour - 7
    ampm  = 'AM' if pdt_h < 12 else 'PM'
    pdt_h = pdt_h % 12 or 12
    time_str = f'{actual_time.strftime("%HZ")} ({pdt_h}:00 {ampm} PDT)'
    date_str = actual_time.strftime('%B %d, %Y')

    fig.text(0.5, 0.955,
             f'{station_id.upper()} — {info["name"]}',
             ha='center', va='bottom',
             color=WHITE, fontsize=13, fontweight='bold')

    fig.text(0.5, 0.933,
             f'{time_str}  ·  {date_str}  ·  {info["desc"]}',
             ha='center', va='bottom',
             color=MUTED, fontsize=8)

    # Branding footer
    fig.text(0.5, 0.015,
             '@SierraNevadaWX  ·  Data: Iowa State RAOB Archive',
             ha='center', color=MUTED, fontsize=7, alpha=0.7)

    # ── Save / show ───────────────────────────────────────────────────────────
    plt.savefig(save_path or '/tmp/skewt_preview.png',
                dpi=150, bbox_inches='tight', facecolor=BG)
    if save_path:
        print(f"  💾 {save_path}")
    if show:
        plt.show()
    plt.close(fig)
    return fig


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Sierra Nevada Skew-T Soundings')
    ap.add_argument('--station', default='REV',
                    help='Station ID: REV OAK VBG SLC (default: REV)')
    ap.add_argument('--time', choices=['00Z', '12Z'],
                    help='Sounding time (default: most recent)')
    ap.add_argument('--all',  action='store_true',
                    help='Plot all stations → skewt_output/')
    ap.add_argument('--save', metavar='FILE',
                    help='Save to file (PNG/PDF)')
    args = ap.parse_args()

    prefer = 0 if args.time == '00Z' else 12 if args.time == '12Z' else None
    valid  = latest_time(prefer)

    if args.all:
        out = Path('skewt_output'); out.mkdir(exist_ok=True)
        for stn in STATIONS:
            fname = out / f'skewt_{stn}_{valid.strftime("%Y%m%d_%HZ")}.png'
            print(f'\n{"="*50}\nPlotting {stn}...')
            plot_skewt(stn, valid, save_path=str(fname), show=False)
        print(f'\n✅ Saved to {out}/')
    else:
        save = args.save
        plot_skewt(args.station, valid,
                   save_path=save,
                   show=(save is None))
