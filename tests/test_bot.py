"""Offline tests: fake feeds and a fake X client. Run: pixi run -e dev test"""

import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "bot"))
NOW = datetime.now(timezone.utc)


def iso(h):
    return (NOW + timedelta(hours=h)).isoformat()


def alert(event, area, act="NEW", phen="FW", sig="W", etn=1, office="KSTO"):
    return {"properties": {
        "id": f"{event}-{area}-{act}-{etn}", "status": "Actual", "messageType": "Alert",
        "event": event, "areaDesc": area, "sent": iso(-1), "ends": iso(10),
        "geocode": {"SAME": [], "UGC": []},
        "parameters": {"VTEC": [f"/O.{act}.{office}.{phen}.{sig}.{etn:04d}.260101T0000Z-260102T0000Z/"]},
        "description": "* WIND...Gusts to 45 mph.\n\n* WHAT...Gusty winds and low humidity.\n\n",
    }}


def calfire(name, acres, county="Tuolumne", lon=-120.2, lat=37.9, contained=0, uid=None):
    return {"geometry": {"coordinates": [lon, lat]}, "properties": {
        "Name": name, "AcresBurned": acres, "PercentContained": contained, "County": county,
        "UniqueId": uid or name, "Url": f"/incidents/2026/10/4/{name.lower()}-fire/",
        "Type": "Wildfire", "Final": False, "Started": "2026-10-04T13:00:00Z"}}


class R:
    def __init__(self, d):
        self.d, self.ok, self.status_code = d, True, 200

    def json(self):
        return self.d

    def raise_for_status(self):
        pass


class FakeX:
    def __init__(self):
        self.sent = []

    def create_tweet(self, text):
        self.sent.append(text)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("LOG_DIR", "logs")
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    for m in ("core", "main"):
        sys.modules.pop(m, None)
    core = importlib.import_module("core")
    main = importlib.import_module("main")
    feeds = {"CA": [], "NV": [], "calfire": [], "nifc": [], "lsr": [], "eq": [], "gauge": {}}

    def get(url, params=None, **kw):
        if "weather.gov" in url:
            return R({"features": feeds["CA" if url.endswith("CA") else "NV"]})
        if "fire.ca.gov" in url:
            return R({"features": feeds["calfire"]})
        if "arcgis" in url:
            return R({"features": feeds["nifc"]})
        if "lsr" in url:
            return R({"features": feeds["lsr"]})
        if "water.noaa" in url:
            return R(feeds["gauge"].get(url.rsplit("/", 1)[-1], {}))
        return R({"features": feeds["eq"]})

    import requests
    monkeypatch.setattr(requests, "get", get)
    x = FakeX()
    monkeypatch.setattr(core.Poster, "_client", lambda self: x)
    (tmp_path / "cache.json").write_text(json.dumps({"version": 2, "seen": {}, "state": {}}))
    return main, core, feeds, x, tmp_path


def test_sierra_zones_only(bot):
    main, core, feeds, x, tmp = bot
    feeds["CA"] = [
        alert("Red Flag Warning", "West Slope Northern Sierra Nevada; Northern Sacramento Valley"),
        alert("Red Flag Warning", "Mono County", etn=1, office="KREV"),
        alert("Red Flag Warning", "San Joaquin Valley"),           # Valley only
        alert("Heat Advisory", "Tuolumne County Foothills", phen="HT", sig="Y"),  # advisory
        alert("Red Flag Warning", "Mariposa Foothills", act="EXT", etn=3),        # extension
    ]
    feeds["NV"] = [alert("High Wind Warning", "Las Vegas Valley", phen="HW", office="KVEF")]
    main.main()
    assert len(x.sent) == 1, x.sent
    t = x.sent[0]
    assert t.startswith("🔥 Red Flag Warning\nWest Slope Northern Sierra Nevada; Mono County")
    assert "Sacramento Valley" not in t and "Gusty winds" in t


def test_fire_new_then_milestones(bot):
    main, core, feeds, x, tmp = bot
    feeds["calfire"] = [calfire("Ferretti", 40), calfire("Small", 50, county="Placer", lon=-120.8, lat=39.0),
                        calfire("Valley", 900, county="Fresno", lon=-119.9, lat=35.5)]
    main.main()
    assert [t.split("\n")[0] for t in x.sent] == ["🔥 New fire: Ferretti Fire"]  # Tuolumne at 10 ac
    assert "Tuolumne County, CA" in x.sent[0]
    assert "Ferretti Fire" in (tmp / "logs" / "tuolumne_drafts.md").read_text()
    feeds["calfire"] = [calfire("Ferretti", 800)]
    main.main()
    assert len(x.sent) == 1                                  # growth below 1,000: quiet
    feeds["calfire"] = [calfire("Ferretti", 6200, contained=10)]
    main.main()
    assert x.sent[-1].startswith("🔥 Ferretti Fire passes 5,000 acres")  # skips 1,000
    main.main()
    assert len(x.sent) == 2


def test_big_new_fire_covers_milestones(bot):
    main, core, feeds, x, tmp = bot
    feeds["calfire"] = [calfire("Mosquito", 30000, county="Placer", lon=-120.7, lat=39.0)]
    main.main()
    main.main()
    assert len(x.sent) == 1 and "30,000 acres" in x.sent[0]


def test_gauges_and_lsr(bot):
    main, core, feeds, x, tmp = bot
    feeds["gauge"] = {"TRRN2": {"status": {"observed": {"primary": 14.2, "floodCategory": "minor"}}},
                      "MDSC1": {"status": {"observed": {"primary": 50.0, "floodCategory": "action"}}}}
    pt = {"type": "Feature", "geometry": {"coordinates": [-119.0, 37.6]}}
    feeds["lsr"] = [dict(pt, properties={"typetext": "HAIL", "magnitude": "0.5", "city": "X", "valid": "2026-10-04T20:00:00Z"}),
                    dict(pt, properties={"typetext": "SNOW", "magnitude": "30", "city": "Mammoth Lakes",
                                         "county": "Mono", "state": "CA", "wfo": "REV",
                                         "valid": "2026-10-04T20:00:00Z", "remark": "Storm total."})]
    main.main()
    heads = [t.split("\n")[0] for t in x.sent]
    assert '❄️ Spotter report: 30" of snow' in heads
    assert "🌊 Minor flooding: Truckee River at Reno at 14.2 ft" in heads
    assert len(x.sent) == 2
