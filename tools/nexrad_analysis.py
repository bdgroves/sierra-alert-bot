"""
NEXRAD Level 2 Dual-Pol Analysis
==================================
Fetches the latest NEXRAD Level 2 scan from AWS S3 (free, no key needed)
and plots all 6 dual-pol fields with automatic TDS and TVS detection.

What each field shows:
  Z    Reflectivity       — precipitation intensity, hook echo
  V    Velocity           — wind speed/direction, rotation couplet = TVS
  SW   Spectrum Width     — turbulence, high values near rotation
  ZDR  Diff Reflectivity  — drop size/shape (hail = near 0 dB)
  CC   Correlation Coeff  — TDS = low CC inside high Z = lofted debris
  PhiDP Diff Phase        — total rain accumulation path

Tornado signatures:
  TVS (Tornado Vortex Signature) — tight inbound/outbound couplet on V
  TDS (Tornado Debris Signature) — Z>40 AND CC<0.80 = confirmed tornado
  Hail signature                 — Z>55 AND ZDR near 0 = large hail

Usage:
  python tools/nexrad_analysis.py                    # Latest KVNX (OK)
  python tools/nexrad_analysis.py --radar KTLX       # OKC radar
  python tools/nexrad_analysis.py --radar KRGX       # Reno (Sierra!)
  python tools/nexrad_analysis.py --radar KBBX       # Beale AFB (N CA Sierra)
  python tools/nexrad_analysis.py --sweep 1          # 2nd elevation tilt
  python tools/nexrad_analysis.py --save             # Save PNG only
  python tools/nexrad_analysis.py --all-sweeps       # Loop all tilts

Sierra Nevada radars:
  KRGX  Reno NV          — eastern Sierra, Great Basin
  KBBX  Beale AFB CA     — northern Sierra, Sacramento Valley
  KHNX  Hanford CA       -- southern/central Sierra, San Joaquin Valley
  KFSX  Flagstaff AZ     — southeastern Sierra periphery
"""

import argparse, sys, os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import Circle

try:
    import pyart
except ImportError:
    print("ERROR: run  pixi install  to get arm-pyart")
    sys.exit(1)

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    HAS_BOTO = True
except ImportError:
    HAS_BOTO = False

# ── Style ─────────────────────────────────────────────────────────────────────
BG  = '#0d1117'
BG2 = '#161b22'

# ── Dual-pol panel config ─────────────────────────────────────────────────────
PANELS = [
    ('reflectivity',             'Reflectivity (Z)',          'NWSRef',    -10, 75,   'dBZ'),
    ('velocity',                 'Velocity (V)',               'NWSVel',    -30, 30,   'kt'),
    ('spectrum_width',           'Spectrum Width (SW)',        'NWS_SPW',    0,  10,   'm/s'),
    ('differential_reflectivity','Diff Reflectivity (ZDR)',   'Carbone42',  -2,  6,   'dB'),
    ('cross_correlation_ratio',  'Correlation Coeff (CC)',    'Wild25',     0.2, 1.05, ''),
    ('differential_phase',       'Diff Phase (PhiDP)',        'Theodore16', 0,  180,  '°'),
]

# ── Fetch latest scan from AWS S3 ─────────────────────────────────────────────
def fetch_latest_scan(radar_id: str) -> str:
    """Download the latest Level 2 scan from NOAA's AWS bucket.
    
    The noaa-nexrad-level2 bucket is public and free — no AWS account needed.
    Uses unsigned (anonymous) requests with explicit us-east-1 region.
    """
    if not HAS_BOTO:
        raise RuntimeError("boto3 not installed — run: pixi install")

    # Must specify region explicitly for anonymous access on Windows
    # Force anonymous access — bypass any AWS credentials in environment
    # (Zillow work credentials would cause AccessDenied on public buckets)
    import os
    env_backup = {}
    for k in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN',
              'AWS_PROFILE','AWS_DEFAULT_PROFILE']:
        if k in os.environ:
            env_backup[k] = os.environ.pop(k)

    s3 = boto3.client(
        's3',
        region_name='us-east-1',
        aws_access_key_id='',
        aws_secret_access_key='',
        config=Config(
            signature_version=UNSIGNED,
            retries={'max_attempts': 3, 'mode': 'standard'}
        )
    )
    BUCKET = 'unidata-nexrad-level2'
    now = datetime.now(timezone.utc)

    for delta in range(3):
        dt     = now - timedelta(days=delta)
        prefix = f"{dt.strftime('%Y/%m/%d')}/{radar_id.upper()}/"
        try:
            print(f"  Searching s3://{BUCKET}/{prefix}")
            paginator = s3.get_paginator('list_objects_v2')
            files = []
            for page in paginator.paginate(
                    Bucket=BUCKET, Prefix=prefix):
                for obj in page.get('Contents', []):
                    key = obj['Key']
                    if '_MDM' not in key and not key.endswith('/'):
                        files.append(key)
            files.sort()
            if files:
                key      = files[-1]
                filename = key.split('/')[-1]
                out_dir  = Path(os.environ.get('TEMP', '/tmp'))
                out_path = str(out_dir / filename)
                if not os.path.exists(out_path):
                    print(f"  Downloading {filename} ({len(files)} scans today)...")
                    s3.download_file(BUCKET, key, out_path)
                else:
                    print(f"  Using cached {filename}")
                return out_path
            else:
                print(f"  No scans found for {dt.strftime('%Y-%m-%d')}, trying previous day...")
        except Exception as e:
            print(f"  ⚠  S3 error: {e}")
            print(f"     If you see AccessDenied, check your internet connection.")

    raise RuntimeError(
        f"Could not fetch scan for {radar_id}\n\n"
        f"Download manually from either:\n"
        f"  https://unidata-nexrad-level2.s3.amazonaws.com/index.html#{'{'}datetime.now(timezone.utc).strftime('%Y/%m/%d'){'}'}/{radar_id}/\n"
        f"  https://www.ncei.noaa.gov/data/nexrad-level-2/access/{radar_id}/\n\n"
        f"Then run:\n"
        f"  python tools/nexrad_analysis.py --file C:\\path\\to\\{radar_id}YYYYMMDD_HHMMSS_V06"
    )


# ── TDS / TVS detection ───────────────────────────────────────────────────────
def detect_signatures(radar, sweep_idx):
    """
    Returns dict with TDS and TVS candidate pixel masks.
    TDS = Tornado Debris Signature: Z>40 AND CC<0.80
    TVS proxy = High Z AND High Spectrum Width (turbulent rotation)
    Hail = Z>55 AND ZDR close to 0
    """
    sigs = {}
    fields = radar.fields.keys()

    z  = np.ma.filled(radar.get_field(sweep_idx, 'reflectivity'), np.nan)

    if 'cross_correlation_ratio' in fields:
        cc = np.ma.filled(radar.get_field(sweep_idx, 'cross_correlation_ratio'), np.nan)
        sigs['tds'] = (z > 40) & (cc < 0.80) & np.isfinite(cc)

    if 'spectrum_width' in fields:
        sw = np.ma.filled(radar.get_field(sweep_idx, 'spectrum_width'), np.nan)
        sigs['tvs_proxy'] = (z > 45) & (sw > 7) & np.isfinite(sw)

    if 'differential_reflectivity' in fields:
        zdr = np.ma.filled(radar.get_field(sweep_idx, 'differential_reflectivity'), np.nan)
        sigs['hail'] = (z > 55) & (np.abs(zdr) < 0.5) & np.isfinite(zdr)

    return sigs


# ── Main plot ─────────────────────────────────────────────────────────────────
def plot_nexrad(radar_file: str, sweep_idx: int = 0,
                save_path: str = None, show: bool = True):

    print(f"\nLoading {radar_file} ...")
    radar    = pyart.io.read_nexrad_archive(radar_file)
    station  = radar.metadata.get('instrument_name', 'UNKN').strip()
    time_str = radar.time['units'].replace('seconds since ', '')
    dt       = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
    pdt_h    = dt.hour - 7; ampm = 'AM' if pdt_h < 12 else 'PM'
    pdt_h    = pdt_h % 12 or 12
    pdt      = f"{dt.strftime('%HZ')} ({pdt_h}:00 {ampm} PDT)  {dt.strftime('%B %d, %Y')}"
    elev     = float(radar.fixed_angle['data'][sweep_idx])
    avail    = list(radar.fields.keys())

    print(f"  Station: {station}  Elevation: {elev:.1f}°  Time: {pdt}")
    print(f"  Fields:  {avail}")
    print(f"  Sweeps:  {radar.nsweeps}")

    # ── Figure ────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 9), dpi=130, facecolor=BG)
    gs  = gridspec.GridSpec(2, 3, figure=fig,
                            left=0.04, right=0.97, top=0.91, bottom=0.04,
                            wspace=0.28, hspace=0.38)
    display = pyart.graph.RadarDisplay(radar)
    axes    = []

    for i, (field, title, cmap, vmin, vmax, unit) in enumerate(PANELS):
        ax = fig.add_subplot(gs[i//3, i%3])
        ax.set_facecolor(BG); axes.append(ax)
        ax.tick_params(colors='#555', labelsize=7)
        for sp in ax.spines.values():
            sp.set_color('#333')

        if field in avail:
            display.plot_ppi(field, sweep=sweep_idx, vmin=vmin, vmax=vmax,
                             cmap=cmap, colorbar_label=unit, title='', ax=ax)
        else:
            ax.text(0.5, 0.5, 'Not available', transform=ax.transAxes,
                    ha='center', va='center', color='#555', fontsize=10)

        ax.set_title(title, color='#ccc', fontsize=9, pad=4)
        ax.set_xlabel('Range (km)', color='#666', fontsize=7)
        ax.set_ylabel('Range (km)', color='#666', fontsize=7)
        ax.set_xlim(-250, 250); ax.set_ylim(-250, 250)

        for r in [50, 100, 150, 200]:
            ax.add_patch(Circle((0,0), r, fill=False,
                         color='#2a2a2a', linewidth=0.5, linestyle='--'))
        ax.axhline(0, color='#333', lw=0.3)
        ax.axvline(0, color='#333', lw=0.3)

    # ── Signature overlays ────────────────────────────────────────────────────
    sigs     = detect_signatures(radar, sweep_idx)
    x, y, _  = radar.get_gate_x_y_z(sweep_idx)
    xkm, ykm = x/1000, y/1000
    alerts   = []

    if 'tds' in sigs:
        n = int(np.sum(sigs['tds']))
        if n > 10:
            axes[4].scatter(xkm[sigs['tds']], ykm[sigs['tds']],
                            c='magenta', s=3, alpha=0.85,
                            label=f'TDS: {n} px', zorder=5)
            axes[4].legend(fontsize=7, facecolor=BG2, edgecolor='#555',
                           labelcolor='magenta', loc='upper right')
            alerts.append(f'⚠️  TDS detected — {n} pixels (possible tornado debris!)')

    if 'tvs_proxy' in sigs:
        n = int(np.sum(sigs['tvs_proxy']))
        if n > 5:
            axes[1].scatter(xkm[sigs['tvs_proxy']], ykm[sigs['tvs_proxy']],
                            c='yellow', s=3, alpha=0.8,
                            label=f'TVS candidate: {n} px', zorder=5)
            axes[1].legend(fontsize=7, facecolor=BG2, edgecolor='#555',
                           labelcolor='yellow', loc='upper right')
            alerts.append(f'⚡ TVS candidate — {n} pixels (check velocity for rotation)')

    if 'hail' in sigs:
        n = int(np.sum(sigs['hail']))
        if n > 5:
            axes[0].scatter(xkm[sigs['hail']], ykm[sigs['hail']],
                            c='cyan', s=3, alpha=0.8,
                            label=f'Hail: {n} px', zorder=5)
            axes[0].legend(fontsize=7, facecolor=BG2, edgecolor='#555',
                           labelcolor='cyan', loc='upper right')
            alerts.append(f'🧊 Hail signature — {n} pixels (Z>55, ZDR~0)')

    if alerts:
        print('\n'.join(alerts))
    else:
        print('  No significant signatures detected.')

    # ── Title / footer ────────────────────────────────────────────────────────
    fig.text(0.5, 0.955,
             f'{station}  ·  NEXRAD Level 2 Dual-Pol  ·  {elev:.1f}° Elev  ·  {pdt}',
             ha='center', color='#e6edf3', fontsize=12, fontweight='bold')
    fig.text(0.5, 0.935,
             'TDS (magenta): Z>40 & CC<0.80 = lofted tornado debris  ·  '
             'TVS (yellow): Z>45 & SW>7 = rotation candidate  ·  '
             'Hail (cyan): Z>55 & ZDR~0',
             ha='center', color='#555', fontsize=8)
    fig.text(0.5, 0.013,
             'Data: NOAA NEXRAD Level 2 / AWS S3  ·  Py-ART  ·  @SierraNevadaWX',
             ha='center', color='#444', fontsize=7)

    if save_path:
        plt.savefig(save_path, dpi=130, bbox_inches='tight', facecolor=BG)
        print(f"\n  💾 Saved: {save_path}")
    if show:
        plt.show()
    plt.close(fig)
    return alerts


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    ap = argparse.ArgumentParser(
        description='NEXRAD Level 2 Dual-Pol Analysis + TDS/TVS Detection'
    )
    ap.add_argument('--radar',  default='KVNX',
                    help='NEXRAD station ID (default: KVNX = Vance AFB OK)\n'
                         'Sierra: KRGX=Reno  KBBX=Beale  KHNX=Hanford')
    ap.add_argument('--sweep',  type=int, default=0,
                    help='Elevation sweep index (0 = lowest 0.5°)')
    ap.add_argument('--file',   help='Use a local Level 2 file instead of S3')
    ap.add_argument('--save',   action='store_true',
                    help='Save PNG to nexrad_output/ instead of displaying')
    ap.add_argument('--all-sweeps', action='store_true',
                    help='Loop through all elevation sweeps')
    args = ap.parse_args()

    if args.file:
        radar_file = args.file
    else:
        print(f"Fetching latest {args.radar.upper()} scan from AWS S3...")
        radar_file = fetch_latest_scan(args.radar)

    if args.all_sweeps:
        import pyart as _pyart
        r = _pyart.io.read_nexrad_archive(radar_file)
        out = Path('nexrad_output'); out.mkdir(exist_ok=True)
        for s in range(r.nsweeps):
            elev = float(r.fixed_angle['data'][s])
            fname = out / f"{args.radar}_{s:02d}_{elev:.1f}deg.png"
            print(f"\n--- Sweep {s} ({elev:.1f}°) ---")
            plot_nexrad(radar_file, sweep_idx=s,
                        save_path=str(fname), show=False)
        print(f"\n✅ All sweeps saved to {out}/")
    else:
        save = None
        if args.save:
            out = Path('nexrad_output'); out.mkdir(exist_ok=True)
            save = str(out / f"{args.radar}_{args.sweep:02d}.png")
        plot_nexrad(radar_file, sweep_idx=args.sweep,
                    save_path=save, show=(save is None))
