"""
Shared plumbing for the alert bots: cache, posting, run log, NWS helpers.

The same file lives in bdgroves/nws-alert-bot and bdgroves/sierra-alert-bot.
Keep the two copies identical.

Design rules
------------
* Post only what matters. Each source decides what that means; this module
  only makes sure nothing posts twice and nothing fails silently.
* The cache stores keys with the time they were posted, and prunes by age.
  (The old cache kept a random 2,000 of a Python set, so it could forget
  recent posts and repeat them.)
* First run on a new cache: everything currently active is marked as seen
  without posting, so a deploy never floods the timeline.
* Every run writes logs/last_run.log, and each post attempt is appended to
  logs/posts.jsonl. Both are committed by the workflow, so the outcome of a
  run can be read from the repo without the Actions log.
* If X starts refusing posts, the run fails once (GitHub emails the owner)
  and then keeps logging quietly until posting works again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
MAX_TWEET_LEN = 280
CACHE_VERSION = 2
CACHE_KEEP_DAYS = 45
POSTS_LOG_KEEP = 500

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
CACHE_FILE = os.environ.get("CACHE_FILE", "posted_ids.json")
LOG_DIR = os.environ.get("LOG_DIR", "logs")
MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "6"))

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, "last_run.log"), mode="w", encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fmt_pacific(dt: datetime, with_day: bool = False) -> str:
    p = dt.astimezone(PACIFIC)
    tz = "PDT" if p.dst() else "PST"
    hour = p.strftime("%I").lstrip("0")
    minute = "" if p.minute == 0 else p.strftime(":%M")
    ampm = p.strftime("%p")
    day = p.strftime("%a ") if with_day else ""
    return f"{day}{hour}{minute} {ampm} {tz}"


def fit(text: str, limit: int = MAX_TWEET_LEN) -> str:
    """Trim to the X limit on a line boundary where possible."""
    if len(text) <= limit:
        return text
    lines = text.split("\n")
    # Drop middle lines (keep header, keep the last line if it is a link/hashtag)
    while len(lines) > 2 and len("\n".join(lines)) > limit:
        lines.pop(-2)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── Cache ─────────────────────────────────────────────────────────────────────
class Cache:
    def __init__(self, path: str = CACHE_FILE):
        self.path = path
        self.seen: dict[str, str] = {}
        self.state: dict = {}
        self.bootstrap = False
        raw = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, json.JSONDecodeError):
                log.warning("Cache unreadable; starting fresh.")
        if raw.get("version") == CACHE_VERSION:
            self.seen = dict(raw.get("seen", {}))
            self.state = dict(raw.get("state", {}))
        else:
            # Old list-format cache (or none): its keys use a different scheme,
            # so start a new one and mark today's active items as seen.
            self.bootstrap = True
            log.info("New cache format: this run marks current items as seen and posts nothing.")

    def __contains__(self, key: str) -> bool:
        return key in self.seen

    def add(self, *keys: str) -> None:
        stamp = now_utc().isoformat(timespec="seconds")
        for k in keys:
            self.seen[k] = stamp

    def save(self) -> None:
        cutoff = now_utc() - timedelta(days=CACHE_KEEP_DAYS)
        self.seen = {
            k: v for k, v in self.seen.items() if (parse_time(v) or now_utc()) >= cutoff
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "version": CACHE_VERSION,
                    "seen": dict(sorted(self.seen.items())),
                    "state": self.state,
                },
                f,
                indent=1,
            )
            f.write("\n")


# ── Posting ───────────────────────────────────────────────────────────────────
class Poster:
    """Posts to X, enforces the per-run cap, records every attempt."""

    def __init__(self, cache: Cache):
        self.cache = cache
        self.client = None
        self.posted = 0
        self.failed = 0
        self.skipped_cap = 0
        self.blocked = False  # X refused auth/plan; stop trying this run
        self.last_error = ""
        if not DRY_RUN:
            self.client = self._client()
            if cache.bootstrap or os.environ.get("CHECK_X"):
                self.check_x()

    def check_x(self) -> None:
        """Ask X who we are: shows whether the keys still work without posting."""
        if self.client is None:
            return
        try:
            me = self.client.get_me()
            who = getattr(getattr(me, "data", None), "username", "?")
            self.cache.state["x_check"] = f"ok @{who} {now_utc():%Y-%m-%d %H:%M}Z"
            log.info(f"X credentials work: @{who}")
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            self.cache.state["x_check"] = f"HTTP {status} {type(e).__name__}: {e}"[:300]
            log.error(f"X credential check failed: {self.cache.state['x_check']}")

    def _client(self):
        keys = ["TWITTER_API_KEY", "TWITTER_API_SECRET",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"]
        missing = [k for k in keys if not os.environ.get(k)]
        if missing:
            self.blocked = True
            self.last_error = f"missing secrets: {', '.join(missing)}"
            log.error(self.last_error)
            return None
        import tweepy  # imported here so dry runs and tests don't need it
        return tweepy.Client(
            consumer_key=os.environ["TWITTER_API_KEY"],
            consumer_secret=os.environ["TWITTER_API_SECRET"],
            access_token=os.environ["TWITTER_ACCESS_TOKEN"],
            access_token_secret=os.environ["TWITTER_ACCESS_SECRET"],
        )

    def _record(self, kind: str, key: str, text: str, outcome: str) -> None:
        path = os.path.join(LOG_DIR, "posts.jsonl")
        line = json.dumps({
            "t": now_utc().isoformat(timespec="seconds"),
            "kind": kind, "key": key, "outcome": outcome, "text": text,
        }, ensure_ascii=False)
        lines = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines = (lines + [line])[-POSTS_LOG_KEEP:]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def post(self, kind: str, text: str, key: str, also_mark: tuple[str, ...] = ()) -> bool:
        """Post once per key. Returns True if the key is now covered."""
        if key in self.cache:
            return True
        text = fit(text)
        if self.cache.bootstrap:
            self.cache.add(key, *also_mark)
            log.info(f"[bootstrap] seen {key}")
            return True
        if self.posted >= MAX_POSTS_PER_RUN:
            self.skipped_cap += 1
            log.info(f"[cap] holding {key} for next run")
            return False
        if DRY_RUN:
            log.info(f"[DRY RUN] {kind} {key}\n{text}\n")
            self._record(kind, key, text, "dry-run")
            self.posted += 1
            return False  # dry runs never mark keys as posted
        if self.blocked or self.client is None:
            self._record(kind, key, text, f"not sent: {self.last_error}")
            return False
        try:
            self.client.create_tweet(text=text)
        except Exception as e:  # tweepy raises several subclasses
            msg = f"{type(e).__name__}: {e}"
            status = getattr(getattr(e, "response", None), "status_code", None)
            log.error(f"X refused {key}: {msg}")
            if "duplicate" in msg.lower() or "187" in msg:
                self.cache.add(key, *also_mark)
                self._record(kind, key, text, "duplicate (marked)")
                return True
            self.failed += 1
            self.last_error = f"HTTP {status} {msg}"[:300]
            if status in (401, 402, 403, 429):
                self.blocked = True
            self._record(kind, key, text, f"error: {self.last_error}")
            return False
        self.cache.add(key, *also_mark)
        self.posted += 1
        self._record(kind, key, text, "posted")
        log.info(f"Posted {key}")
        time.sleep(2)
        return True

    def finish(self) -> int:
        """Save state; return the process exit code."""
        st = self.cache.state
        was_ok = st.get("x_ok", True)
        exit_code = 0
        if self.failed:
            st["x_ok"] = False
            st["x_error"] = self.last_error
            st["x_error_since"] = st.get("x_error_since") or now_utc().isoformat(timespec="seconds")
            if was_ok:
                log.error("X stopped accepting posts. Failing this run once so GitHub sends an email.")
                exit_code = 1
            else:
                log.error(f"X still refusing posts (since {st['x_error_since']}).")
        elif self.posted and not DRY_RUN:
            if not was_ok:
                log.info("X is accepting posts again.")
            st.update({"x_ok": True, "x_error": "", "x_error_since": ""})
        st["last_run"] = now_utc().isoformat(timespec="seconds")
        self.cache.save()
        log.info(
            f"Done. posted={self.posted} failed={self.failed} held={self.skipped_cap} "
            f"bootstrap={self.cache.bootstrap} dry_run={DRY_RUN} cache={len(self.cache.seen)}"
        )
        return exit_code


# ── NWS alert helpers ─────────────────────────────────────────────────────────
NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"
VTEC_RE = re.compile(
    r"/[OTEX]\.(?P<act>[A-Z]{3})\.(?P<office>[A-Z]{4})\.(?P<phen>[A-Z]{2})\."
    r"(?P<sig>[A-Z])\.(?P<etn>\d{4})\.(?P<begin>\d{6}T\d{4}Z)-(?P<end>\d{6}T\d{4}Z)/"
)
POST_ACTIONS = {"NEW", "EXA", "EXB"}  # new event, or the area grew


def vtec(props: dict) -> dict | None:
    for code in (props.get("parameters") or {}).get("VTEC", []) or []:
        m = VTEC_RE.search(code)
        if m:
            return m.groupdict()
    return None


def alert_key(props: dict, scope: str) -> str | None:
    """Stable key for an NWS hazard, or None if this message shouldn't post.

    VTEC alerts: one key per office + hazard + event number, so updates,
    extensions and continuations of the same hazard never repost.
    Non-VTEC alerts (e.g. Air Quality Alert): one key per event per day.
    """
    if props.get("status") != "Actual":
        return None
    v = vtec(props)
    sent = parse_time(props.get("sent")) or now_utc()
    if v:
        if v["act"] not in POST_ACTIONS:
            return None
        return f"{scope}:{v['office']}.{v['phen']}.{v['sig']}.{v['etn']}.{sent.year}"
    if props.get("messageType") != "Alert":
        return None
    day = sent.astimezone(PACIFIC).strftime("%Y%m%d")
    return f"{scope}:{props.get('event', '').replace(' ', '')}.{day}"


def what_line(props: dict) -> str:
    """The forecaster's one-line summary: the '* WHAT...' bullet if present."""
    desc = props.get("description") or ""
    m = re.search(r"\*\s*WHAT\.\.\.(.+?)(?:\n\s*\n|\n\s*\*|$)", desc, re.S)
    if m:
        return " ".join(m.group(1).split())
    head = ((props.get("parameters") or {}).get("NWSheadline") or [""])[0]
    if head:
        return " ".join(head.split()).capitalize()
    return ""


def office_link(props: dict) -> str:
    v = vtec(props)
    if v:
        return f"https://www.weather.gov/{v['office'][1:].lower()}/"
    return "https://alerts.weather.gov/"


def alert_end(props: dict) -> datetime | None:
    return parse_time(props.get("ends")) or parse_time(props.get("expires"))


def fetch_nws(area: str, user_agent: str) -> list[dict]:
    import requests
    r = requests.get(
        f"{NWS_ALERTS_URL}?area={area}",
        headers={"User-Agent": user_agent, "Accept": "application/geo+json"},
        timeout=30,
    )
    r.raise_for_status()
    feats = r.json().get("features", [])
    log.info(f"NWS {area}: {len(feats)} active alerts")
    return [f.get("properties", {}) for f in feats]


def format_alert(event: str, emoji: str, flag: str, areas: list[str],
                 props_list: list[dict], tags: str, max_area: int = 110) -> str:
    """One post for one hazard type, however many zones/products carry it."""
    area = "; ".join(dict.fromkeys(areas))
    if len(area) > max_area:
        area = area[: max_area - 1].rsplit(";", 1)[0] + " +more"
    ends = [e for e in (alert_end(p) for p in props_list) if e]
    until = f"Until {fmt_pacific(max(ends), with_day=True)}" if ends else ""
    what = next((w for w in (what_line(p) for p in props_list) if w), "")
    head = [f"{emoji} {event}{flag}", area] + ([until] if until else [])
    tail = [office_link(props_list[0])] + ([tags] if tags else [])
    if what:
        # X counts any link as 23 characters; budget the summary around that.
        used = sum(len(x) + 1 for x in head + tail) - len(tail[0]) + 23
        room = MAX_TWEET_LEN - used - 1
        if room >= 40:
            if len(what) > room:
                what = what[: room - 1].rsplit(" ", 1)[0] + "…"
            head.append(what)
    return fit("\n".join(head + tail))
