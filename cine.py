#!/usr/bin/env python3
"""Build a local HTML page of the coming showings at two Toulouse cinemas.

Sources:
  - American Cosmograph: https://www.american-cosmograph.fr/les-horaires.html
  - ABC:                 https://abc-toulouse.fr/horaires.html

Each showing on the page carries a link that opens Google Calendar with the
event prefilled. The user still has to press Save in Google Calendar.

The script keeps a state file next to the page (cine-state.json). Each run
merges what the sites publish into that state:
  - a day a site returned replaces the stored showings of that site and day;
  - a day a site did not return keeps its stored showings;
  - showings that already started are dropped.
So the script can run as often as wanted. It never adds a showing twice and
the latest answer of a site wins.

Usage:

    python3 cine.py                 # writes ./seances.html and ./cine-state.json
    python3 cine.py -o out.html     # writes elsewhere; the state file follows
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as dt
import hashlib
import html
import io
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo

try:
    from PIL import Image, ImageOps
except ImportError:  # without Pillow the page links to the posters on the cinema sites
    Image = None
    ImageOps = None

TZ = ZoneInfo("Europe/Paris")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) cine-agenda/1.0"
TIMEOUT = 30
STATE_VERSION = 1

COSMO_URL = "https://www.american-cosmograph.fr/les-horaires.html"
COSMO_NAME = "American Cosmograph"
COSMO_SHORT = "Cosmo"
COSMO_ADDRESS = "American Cosmograph, 24 rue Montardy, 31000 Toulouse"

ABC_URL = "https://abc-toulouse.fr/horaires.html"
ABC_DAY_URL = "https://abc-toulouse.fr/horaires.html?d={date}"
ABC_NAME = "ABC"
ABC_SHORT = "ABC"
ABC_ADDRESS = "Cinéma ABC, 13 rue Saint-Bernard, 31000 Toulouse"
ABC_PROBE_DAYS = 13      # how far after today the script looks for next week's ABC pages
ABC_PROBE_STOP = 2       # stop probing after this many consecutive unpublished days

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
FRENCH_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
FRENCH_MONTH_NAMES = [
    "", "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
]


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #

@dataclass
class Showing:
    cinema: str          # short name used in the event title
    cinema_full: str
    address: str
    film: str
    start: dt.datetime   # tz-aware, Europe/Paris
    end: dt.datetime
    duration_min: int
    room: str
    version: str = ""    # VO, VF, VOST... empty when the site does not say
    tags: list[str] = field(default_factory=list)
    film_url: str = ""
    booking_url: str = ""
    director: str = ""
    poster_url: str = ""

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.cinema, film_key(self.film), self.start.isoformat(), self.room)


SHOWING_FIELDS = {f.name for f in dataclasses.fields(Showing)}


@dataclass
class SourceResult:
    """What one run got from one cinema site."""
    cinema: str
    showings: list[Showing] = field(default_factory=list)
    days: set[dt.date] = field(default_factory=set)   # days this answer is authoritative for
    requests: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # An answer with no showing at all is treated as a failure: the stored
        # showings stay. This guards against a markup change that breaks a parser.
        return bool(self.showings)


def film_key(title: str) -> str:
    """Same film at both cinemas, whatever the case and accents."""
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", t).strip().casefold()


def showing_to_dict(s: Showing) -> dict:
    d = asdict(s)
    d["start"] = s.start.isoformat()
    d["end"] = s.end.isoformat()
    return d


def showing_from_dict(d: dict) -> Showing:
    d = {k: v for k, v in d.items() if k in SHOWING_FIELDS}
    d["start"] = dt.datetime.fromisoformat(d["start"]).astimezone(TZ)
    d["end"] = dt.datetime.fromisoformat(d["end"]).astimezone(TZ)
    return Showing(**d)


# --------------------------------------------------------------------------- #
# Fetching and text helpers
# --------------------------------------------------------------------------- #

def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", errors="replace")


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html.unescape(text)
    return re.sub(r"[\s\xa0]+", " ", text).strip()


def parse_hm(s: str) -> tuple[int, int]:
    """'13h30' or '13:30' -> (13, 30)."""
    m = re.fullmatch(r"(\d{1,2})[h:](\d{2})", s.strip())
    if not m:
        raise ValueError(f"bad time {s!r}")
    return int(m.group(1)), int(m.group(2))


def sentence_case(title: str) -> str:
    """Cosmo prints titles in capitals. Keep the first letter only, like ABC does."""
    if not title or title != title.upper():
        return title
    return title[0] + title[1:].lower()


def parse_duration(s: str) -> int:
    """'1h58' -> 118 minutes."""
    h, m = parse_hm(s)
    return h * 60 + m


# --------------------------------------------------------------------------- #
# American Cosmograph
# --------------------------------------------------------------------------- #

COSMO_PANE_RE = re.compile(
    r'<div class="progseances-daypane[^"]*" id="day-(\d{4}-\d{2}-\d{2})"[^>]*>(.*?)(?=<div class="progseances-daypane|</div>\s*</div>\s*<nav|$)',
    re.S,
)
COSMO_THUMB_RE = re.compile(r'<li class="progseances-thumb[^"]*".*?</li>', re.S)
COSMO_START_RE = re.compile(r"Début du film\s*:\s*<strong>\s*([\d]{1,2}h\d{2})\s*</strong>", re.S)
COSMO_TITLE_RE = re.compile(r'<h3 class="filmTitle">\s*<a href="([^"]*)">(.*?)</a>', re.S)
COSMO_END_RE = re.compile(r"Fin de séance\s*:\s*([\d]{1,2}h\d{2})")
COSMO_DUR_RE = re.compile(r"Durée\s*:\s*([\d]{1,2}h\d{2})")
COSMO_ROOM_RE = re.compile(r"(salle\s*\d+)", re.I)
COSMO_TAG_RE = re.compile(r'<span class="filmTag">.*?title="([^"]*)"', re.S)
COSMO_IMG_RE = re.compile(r"background-image:\s*url\('([^']*)'\)")
COSMO_NEXT_RE = re.compile(r'<a class="seance-next" href="([^"]+)"')


def parse_cosmo(page: str) -> tuple[list[Showing], set[dt.date]]:
    """Return the showings and the set of days the page has a pane for."""
    out: list[Showing] = []
    days: set[dt.date] = set()
    for day_str, pane in COSMO_PANE_RE.findall(page):
        day = dt.date.fromisoformat(day_str)
        days.add(day)
        for thumb in COSMO_THUMB_RE.findall(pane):
            m_start = COSMO_START_RE.search(thumb)
            m_title = COSMO_TITLE_RE.search(thumb)
            m_dur = COSMO_DUR_RE.search(thumb)
            if not (m_start and m_title and m_dur):
                continue
            h, mi = parse_hm(m_start.group(1))
            start = dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=TZ)
            duration = parse_duration(m_dur.group(1))
            m_end = COSMO_END_RE.search(thumb)
            if m_end:
                eh, em = parse_hm(m_end.group(1))
                end = dt.datetime(day.year, day.month, day.day, eh, em, tzinfo=TZ)
                if end < start:
                    end += dt.timedelta(days=1)
            else:
                end = start + dt.timedelta(minutes=duration)
            m_room = COSMO_ROOM_RE.search(strip_tags(thumb))
            room = m_room.group(1).lower() if m_room else ""
            tags = [html.unescape(t) for t in COSMO_TAG_RE.findall(thumb)]
            version = "ST-SME" if "ST-SME" in tags else ""
            film_url = urllib.parse.urljoin(COSMO_URL, html.unescape(m_title.group(1)))
            m_img = COSMO_IMG_RE.search(thumb)
            poster = urllib.parse.urljoin(COSMO_URL, html.unescape(m_img.group(1))) if m_img else ""
            out.append(Showing(
                cinema=COSMO_SHORT, cinema_full=COSMO_NAME, address=COSMO_ADDRESS,
                film=sentence_case(strip_tags(m_title.group(2))),
                start=start, end=end, duration_min=duration, room=room,
                version=version, tags=tags, film_url=film_url, poster_url=poster,
            ))
    return out, days


def fetch_cosmo(log) -> SourceResult:
    """The showtimes page carries 7 days. Follow the 'next week' link once."""
    res = SourceResult(COSMO_SHORT)
    try:
        page = fetch(COSMO_URL)
        res.requests += 1
    except (OSError, ValueError) as e:
        res.errors.append(f"{COSMO_URL}: {e}")
        return res
    shows, days = parse_cosmo(page)
    if shows:
        res.showings += shows
        res.days |= days
    else:
        res.errors.append(f"{COSMO_URL}: no showing parsed")
    m = COSMO_NEXT_RE.search(page)
    if m:
        nxt = urllib.parse.urljoin(COSMO_URL, html.unescape(m.group(1)))
        try:
            shows2, days2 = parse_cosmo(fetch(nxt))
            res.requests += 1
            if shows2:
                res.showings += shows2
                res.days |= days2
            else:
                log(f"cosmo: next week not published yet")
        except (OSError, ValueError) as e:
            res.errors.append(f"{nxt}: {e}")
    return res


# --------------------------------------------------------------------------- #
# ABC
# --------------------------------------------------------------------------- #

ABC_FILM_RE = re.compile(r'<div class="mediumfilm[^"]*"\s*>(.*?)(?=<div class="mediumfilm|<div class="separator"\s*>\s*</div>\s*</div>|$)', re.S)
ABC_TITLE_RE = re.compile(r'<a href="([^"]*)" class="title"\s*>\s*<h2>(.*?)</h2>', re.S)
ABC_DIRECTOR_RE = re.compile(r'<div class="director"\s*>(.*?)</div>', re.S)
ABC_GROUP1_RE = re.compile(r'<div class="field group1"\s*>\s*<div class="val"\s*>(.*?)</div>', re.S)
ABC_SDATE_RE = re.compile(r'<div class="sdate"\s*>(.*?)(?=<div class="sdate"\s*>|</div>\s*</div>\s*</div>\s*<!--|$)', re.S)
ABC_DAY_RE = re.compile(r'<div class="day"\s*>(.*?)</div>', re.S)
ABC_MONTH_RE = re.compile(r'<div class="month"\s*>(.*?)</div>', re.S)
ABC_SESSION_RE = re.compile(r'<a href="([^"]*)"[^>]*class="session[^"]*"[^>]*>(.*?)</a>', re.S)
ABC_SESSION_DIV_RE = re.compile(r'<div class="session[^"]*"[^>]*>(.*?)</div>\s*</div>', re.S)
ABC_STIME_RE = re.compile(r'<div class="stime"\s*>(.*?)</div>', re.S)
ABC_VERSION_RE = re.compile(r'<div class="version"\s*>(.*?)</div>', re.S)
ABC_ROOM_RE = re.compile(r'<div class="room"\s*>(.*?)</div>', re.S)
ABC_DAY_LINK_RE = re.compile(r'href="/horaires\.html\?d=(\d{4}-\d{2}-\d{2})"')
ABC_POSTER_RE = re.compile(r'class="poster"\s*>\s*<img src="([^"]*)"', re.S)


def _abc_date(day_txt: str, month_txt: str) -> dt.date:
    # day_txt: "Jeudi 12", month_txt: "novembre 2026"
    m_day = re.search(r"(\d{1,2})", day_txt)
    m_month = re.match(r"\s*(\S+)\s+(\d{4})", month_txt)
    if not (m_day and m_month):
        raise ValueError(f"bad abc date {day_txt!r} {month_txt!r}")
    month = FRENCH_MONTHS[m_month.group(1).lower()]
    return dt.date(int(m_month.group(2)), month, int(m_day.group(1)))


def parse_abc(page: str) -> tuple[list[Showing], int]:
    """Return the showings and the number of film blocks on the page.

    A day the cinema has not published yet comes back with zero film blocks.
    """
    out: list[Showing] = []
    nfilms = 0
    for block in ABC_FILM_RE.findall(page):
        m_title = ABC_TITLE_RE.search(block)
        if not m_title:
            continue
        nfilms += 1
        film = strip_tags(m_title.group(2))
        film_url = urllib.parse.urljoin(ABC_URL, html.unescape(m_title.group(1)))
        m_dir = ABC_DIRECTOR_RE.search(block)
        director = strip_tags(m_dir.group(1)) if m_dir else ""
        m_img = ABC_POSTER_RE.search(block)
        poster = urllib.parse.urljoin(ABC_URL, html.unescape(m_img.group(1))) if m_img else ""
        m_g1 = ABC_GROUP1_RE.search(block)
        info = strip_tags(m_g1.group(1)) if m_g1 else ""
        m_dur = re.search(r"(\d{1,2}h\d{2})", info)
        duration = parse_duration(m_dur.group(1)) if m_dur else 0
        default_version = ""
        m_ver = re.search(r"/\s*([A-Z][A-Z\- ]+)$", info)
        if m_ver:
            default_version = m_ver.group(1).strip()

        for sdate in ABC_SDATE_RE.findall(block):
            m_d = ABC_DAY_RE.search(sdate)
            m_m = ABC_MONTH_RE.search(sdate)
            if not (m_d and m_m):
                continue
            day = _abc_date(strip_tags(m_d.group(1)), strip_tags(m_m.group(1)))
            sessions = [(href, body) for href, body in ABC_SESSION_RE.findall(sdate)]
            if not sessions:
                sessions = [("", body) for body in ABC_SESSION_DIV_RE.findall(sdate)]
            for href, body in sessions:
                m_st = ABC_STIME_RE.search(body)
                if not m_st:
                    continue
                h, mi = parse_hm(strip_tags(m_st.group(1)))
                start = dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=TZ)
                end = start + dt.timedelta(minutes=duration) if duration else start + dt.timedelta(hours=2)
                m_v = ABC_VERSION_RE.search(body)
                version = strip_tags(m_v.group(1)) if m_v else default_version
                m_r = ABC_ROOM_RE.search(body)
                room = strip_tags(m_r.group(1)) if m_r else ""
                if room and len(room) <= 2:
                    room = f"salle {room.upper()}"
                out.append(Showing(
                    cinema=ABC_SHORT, cinema_full=ABC_NAME, address=ABC_ADDRESS,
                    film=film, start=start, end=end, duration_min=duration,
                    room=room, version=version, tags=[], film_url=film_url,
                    booking_url=html.unescape(href), director=director,
                    poster_url=poster,
                ))
    return out, nfilms


def fetch_abc(today: dt.date, log) -> SourceResult:
    """The main page shows today and links to the other days of the week.

    Next week's pages are probed after the last linked day, until two
    consecutive days come back unpublished.
    """
    res = SourceResult(ABC_SHORT)

    def fetch_day(day: dt.date | None) -> int | None:
        url = ABC_URL if day is None else ABC_DAY_URL.format(date=day.isoformat())
        try:
            shows, nfilms = parse_abc(fetch(url))
        except (OSError, ValueError) as e:
            res.errors.append(f"{url}: {e}")
            return None
        res.requests += 1
        if nfilms:
            res.days.add(today if day is None else day)
        res.showings += shows
        res.days |= {s.start.date() for s in shows}
        return nfilms

    try:
        page = fetch(ABC_URL)
    except (OSError, ValueError) as e:
        res.errors.append(f"{ABC_URL}: {e}")
        return res
    res.requests += 1
    shows, nfilms = parse_abc(page)
    if nfilms:
        res.days.add(today)
    res.showings += shows
    res.days |= {s.start.date() for s in shows}

    linked = sorted({dt.date.fromisoformat(d) for d in ABC_DAY_LINK_RE.findall(page)})
    for day in linked:
        if day >= today:
            fetch_day(day)

    last = max(linked + [today])
    day = last + dt.timedelta(days=1)
    empty = 0
    while day <= today + dt.timedelta(days=ABC_PROBE_DAYS) and empty < ABC_PROBE_STOP:
        n = fetch_day(day)
        if n is None:
            break
        empty = 0 if n else empty + 1
        day += dt.timedelta(days=1)
    if empty >= ABC_PROBE_STOP:
        log(f"abc: nothing published after {last.isoformat()}")
    return res


# --------------------------------------------------------------------------- #
# State and merge
# --------------------------------------------------------------------------- #

def load_state(path: str, log) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            raise ValueError("unknown state format")
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log(f"state: {path}: {e}; starting from an empty state")
        return {}


def write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def merge(stored: list[Showing], results: list[SourceResult], now: dt.datetime) -> list[Showing]:
    """Fetched days replace stored days. Other stored days stay. Past showings go."""
    replaced = {(r.cinema, d) for r in results if r.ok for d in r.days}
    kept = [s for s in stored if (s.cinema, s.start.date()) not in replaced]
    fresh = [s for r in results if r.ok for s in r.showings]
    by_key: dict[tuple, Showing] = {}
    for s in kept + fresh:          # a fetched showing wins over a stored one
        by_key[s.key] = s
    out = [s for s in by_key.values() if s.start >= now]
    out.sort(key=lambda s: (s.start, s.cinema, s.film))
    return out


# --------------------------------------------------------------------------- #
# Thumbnails
# --------------------------------------------------------------------------- #

THUMB_W, THUMB_H = 96, 144   # twice the 48x72 box on the page, for sharp screens
THUMB_QUALITY = 70


def build_thumbs(showings: list[Showing], cache: dict[str, str], log) -> tuple[dict[str, str], dict[str, str]]:
    """Map each poster URL to a value for CSS url(), and return the new cache.

    With Pillow, a poster is downloaded once, cropped to THUMB_W x THUMB_H and
    kept in the cache as a JPEG data URI. Without Pillow, or when a download
    fails, the value is the remote URL and nothing is cached.
    """
    urls = sorted({s.poster_url for s in showings if s.poster_url})
    out: dict[str, str] = {}
    new_cache: dict[str, str] = {}
    downloaded = 0
    for u in urls:
        if u in cache:
            out[u] = new_cache[u] = cache[u]
            continue
        if Image is None:
            out[u] = u
            continue
        try:
            im = Image.open(io.BytesIO(fetch_bytes(u))).convert("RGB")
            im = ImageOps.fit(im, (THUMB_W, THUMB_H), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=THUMB_QUALITY, optimize=True)
            out[u] = new_cache[u] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            downloaded += 1
        except (OSError, ValueError) as e:
            log(f"thumb: {u}: {e}")
            out[u] = u
    log(f"thumbs: {len(urls)} posters, {downloaded} downloaded, {len(urls) - downloaded} from cache or linked")
    return out, new_cache


# --------------------------------------------------------------------------- #
# Google Calendar link
# --------------------------------------------------------------------------- #

def gcal_stamp(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def gcal_link(s: Showing) -> str:
    title = f"{s.film} · {s.cinema}"
    if s.room:
        title += f" {s.room}"
    details_lines = [f"{s.cinema_full}"]
    if s.version:
        details_lines.append(f"Version : {s.version}")
    if s.director:
        details_lines.append(f"Réalisation : {s.director}")
    if s.duration_min:
        details_lines.append(f"Durée : {s.duration_min // 60}h{s.duration_min % 60:02d}")
    if s.tags:
        details_lines.append("Tags : " + ", ".join(s.tags))
    if s.film_url:
        details_lines.append(f"Film : {s.film_url}")
    if s.booking_url:
        details_lines.append(f"Réserver : {s.booking_url}")
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{gcal_stamp(s.start)}/{gcal_stamp(s.end)}",
        "details": "\n".join(details_lines),
        "location": s.address,
        "ctz": "Europe/Paris",
    }
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

CSS = """
:root{
  --bg:#f6f4ef; --fg:#1d1b17; --muted:#6c675c; --line:#dcd7cc; --card:#fffdf8;
  --cosmo:#b4432a; --abc:#2a5fb4; --chip:#ebe7de; --btn:#1d1b17; --btn-fg:#fffdf8; --shade:rgba(0,0,0,.35);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#15140f; --fg:#efeadf; --muted:#a39d90; --line:#2d2b24; --card:#1d1b16;
    --cosmo:#ff8a6b; --abc:#7aa6ff; --chip:#2a281f; --btn:#efeadf; --btn-fg:#15140f; --shade:rgba(0,0,0,.55);
    color-scheme:dark;
  }
}
:root[data-theme="dark"]{
  --bg:#15140f; --fg:#efeadf; --muted:#a39d90; --line:#2d2b24; --card:#1d1b16;
  --cosmo:#ff8a6b; --abc:#7aa6ff; --chip:#2a281f; --btn:#efeadf; --btn-fg:#15140f; --shade:rgba(0,0,0,.55);
  color-scheme:dark;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:16px 48px}
main{max-width:860px;margin:0 auto}
a,a:visited{color:inherit;text-decoration:none}
.f .name a{border-bottom:1px solid var(--line)}
.f .name a:hover,.f .name a:focus-visible{border-bottom-color:var(--fg)}
.foot a,.foot a:visited{text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:2px}
.foot a:hover,.foot a:focus-visible{text-decoration-color:var(--muted)}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 16px;font-size:.9rem}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;position:sticky;top:0;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line);z-index:5}
.filters label,.chip{display:inline-flex;align-items:center;gap:6px;background:var(--chip);color:var(--fg);padding:6px 10px;border:0;border-radius:999px;font:inherit;font-size:.9rem;cursor:pointer}
#seen-toggle{border:1px solid var(--muted);font-weight:600}
.filters select,.filters input[type=search]{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg);min-width:0}
.filters input[type=search]{flex:1 1 160px}
.day h2{font-size:1.05rem;margin:22px 0 8px;padding-bottom:4px;border-bottom:1px solid var(--line);position:sticky;top:52px;background:var(--bg);z-index:4}
.row{display:grid;grid-template-columns:3.6em 48px 1fr auto;gap:8px 12px;align-items:center;padding:8px 0;border-bottom:1px dashed var(--line)}
.row[hidden]{display:none}
.t{font-variant-numeric:tabular-nums;font-weight:600}
.th{width:48px;height:72px;border-radius:4px;background:var(--chip) center/cover no-repeat}
.f{min-width:0}
.f .name{font-weight:600}
.f .meta{color:var(--muted);font-size:.85rem;display:flex;flex-wrap:wrap;gap:4px 10px}
.c{font-size:.78rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase}
.c.cosmo{color:var(--cosmo)}.c.abc{color:var(--abc)}
.tag{background:var(--chip);border-radius:4px;padding:0 5px}
.act{display:flex;gap:8px;align-items:center}
.add,.add:visited{display:inline-block;background:var(--btn);color:var(--btn-fg);text-decoration:none;padding:7px 12px;border-radius:8px;font-size:.85rem;white-space:nowrap}
.seen-btn{font:inherit;font-size:.85rem;background:var(--chip);color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer;white-space:nowrap}
.add:focus-visible,.seen-btn:focus-visible,.chip:focus-visible,.drawer button:focus-visible{outline:2px solid var(--cosmo);outline-offset:2px}
.empty{color:var(--muted);padding:24px 0}
.foot{color:var(--muted);font-size:.8rem;margin-top:32px}
.backdrop{position:fixed;inset:0;background:var(--shade);z-index:19}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(340px,92vw);background:var(--card);color:var(--fg);border-left:1px solid var(--line);
  box-shadow:-8px 0 24px var(--shade);transform:translateX(100%);transition:transform .2s ease;z-index:20;overflow:auto;
  padding:16px;padding-right:max(16px,env(safe-area-inset-right,0px));padding-top:max(16px,env(safe-area-inset-top,0px));padding-bottom:max(16px,env(safe-area-inset-bottom,0px))}
.drawer.open{transform:none}
@media (prefers-reduced-motion:reduce){.drawer{transition:none}}
.drawer-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.drawer h2{font-size:1.05rem;margin:0}
.drawer .close{font:inherit;font-size:1.4rem;line-height:1;background:none;border:0;color:var(--muted);cursor:pointer;padding:4px 8px}
.hint{color:var(--muted);font-size:.85rem}
#seen-list{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
#seen-list li{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line)}
#seen-list li .th{flex:none;width:32px;height:48px}
#seen-list li span{flex:1;min-width:0;overflow-wrap:anywhere}
#seen-list button{font:inherit;font-size:.8rem;background:none;border:1px solid var(--line);border-radius:6px;color:var(--fg);padding:3px 8px;cursor:pointer;white-space:nowrap}
@media (max-width:480px){
  .row{grid-template-columns:3.2em 48px 1fr;}
  .row .act{grid-column:1/-1}
}
"""

JS = """
(function(){
  var SEEN='cine-seen', FILT='cine-filters';
  var rows=[].slice.call(document.querySelectorAll('.row'));
  var days=[].slice.call(document.querySelectorAll('section.day'));
  var cCosmo=document.getElementById('f-cosmo'), cAbc=document.getElementById('f-abc');
  var film=document.getElementById('f-film'), q=document.getElementById('f-q');
  var opts=[].slice.call(film.options);
  var drawer=document.getElementById('seen'), backdrop=document.getElementById('backdrop');
  var toggle=document.getElementById('seen-toggle'), closeBtn=document.getElementById('seen-close');
  var list=document.getElementById('seen-list'), count=document.getElementById('seen-count'), seenEmpty=document.getElementById('seen-empty');
  var seen={};
  function loadSeen(){
    try{var v=JSON.parse(localStorage.getItem(SEEN)||'{}');seen=(v&&typeof v==='object')?v:{};}catch(e){seen={};}
    // Older entries have no poster. Fill it from the rows while the film is still on the page.
    var changed=false;
    rows.forEach(function(r){
      var k=r.dataset.key, b=r.querySelector('[data-seen]');
      if(seen[k]&&!seen[k].poster&&b&&b.dataset.poster){seen[k].poster=b.dataset.poster;changed=true;}
    });
    if(changed) saveSeen();
  }
  function saveSeen(){try{localStorage.setItem(SEEN,JSON.stringify(seen));}catch(e){}}
  function apply(){
    var wantCosmo=cCosmo.checked, wantAbc=cAbc.checked, s=(q.value||'').trim().toLowerCase();
    if(film.value&&seen[film.value]) film.value='';
    var f=film.value;
    opts.forEach(function(o){o.hidden=!!(o.value&&seen[o.value]);});
    rows.forEach(function(r){
      var ok=true;
      if(seen[r.dataset.key]) ok=false;
      if(r.dataset.cinema==='cosmo'&&!wantCosmo) ok=false;
      if(r.dataset.cinema==='abc'&&!wantAbc) ok=false;
      if(f&&r.dataset.key!==f) ok=false;
      if(s&&r.dataset.film.toLowerCase().indexOf(s)<0) ok=false;
      r.hidden=!ok;
    });
    days.forEach(function(d){
      d.hidden=![].some.call(d.querySelectorAll('.row'),function(r){return !r.hidden;});
    });
    document.getElementById('empty').hidden=rows.some(function(r){return !r.hidden;});
    try{localStorage.setItem(FILT,JSON.stringify({c:wantCosmo,a:wantAbc,f:f,s:s}));}catch(e){}
  }
  function renderSeen(){
    var keys=Object.keys(seen).sort(function(a,b){return String(seen[a].title||a).localeCompare(String(seen[b].title||b),'fr');});
    list.textContent='';
    keys.forEach(function(k){
      var title=String(seen[k].title||k), poster=String(seen[k].poster||'');
      var li=document.createElement('li'), th=document.createElement('div'), span=document.createElement('span'), b=document.createElement('button');
      th.className=/^p[0-9a-f]{10}$/.test(poster)?'th '+poster:'th';
      span.textContent=title;
      b.type='button'; b.textContent='Retirer'; b.dataset.unseen=k; b.setAttribute('aria-label','Retirer '+title+' des films vus');
      li.appendChild(th); li.appendChild(span); li.appendChild(b); list.appendChild(li);
    });
    count.textContent=String(keys.length);
    seenEmpty.hidden=keys.length>0;
  }
  function setDrawer(open){
    drawer.classList.toggle('open',open);
    drawer.setAttribute('aria-hidden',String(!open));
    backdrop.hidden=!open;
    toggle.setAttribute('aria-expanded',String(open));
  }
  document.addEventListener('click',function(ev){
    var t=ev.target&&ev.target.closest?ev.target.closest('[data-seen],[data-unseen]'):null;
    if(!t) return;
    if(t.dataset.seen!==undefined){seen[t.dataset.seen]={title:t.dataset.title,poster:t.dataset.poster||'',at:new Date().toISOString()};}
    else{delete seen[t.dataset.unseen];}
    saveSeen(); renderSeen(); apply();
  });
  toggle.addEventListener('click',function(){setDrawer(!drawer.classList.contains('open'));});
  closeBtn.addEventListener('click',function(){setDrawer(false);});
  backdrop.addEventListener('click',function(){setDrawer(false);});
  document.addEventListener('keydown',function(ev){if(ev.key==='Escape')setDrawer(false);});
  try{
    var st=JSON.parse(localStorage.getItem(FILT)||'null');
    if(st){cCosmo.checked=st.c!==false;cAbc.checked=st.a!==false;film.value=st.f||'';q.value=st.s||'';}
  }catch(e){}
  [cCosmo,cAbc,film].forEach(function(el){el.addEventListener('change',apply);});
  q.addEventListener('input',apply);
  loadSeen(); renderSeen(); apply();
})();
"""


def fmt_day_heading(d: dt.date) -> str:
    return f"{FRENCH_DAYS[d.weekday()]} {d.day} {FRENCH_MONTH_NAMES[d.month]}"


def fmt_hm(t: dt.datetime) -> str:
    return t.strftime("%Hh%M")


def fmt_when(t: dt.datetime) -> str:
    return f"{fmt_day_heading(t.date())} à {fmt_hm(t)}"


def poster_class_name(url: str) -> str:
    return "p" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def css_url(value: str) -> str:
    # Inside <style>, HTML entities are not decoded, so escape for CSS only.
    return value.replace("\\", "%5C").replace('"', "%22").replace("<", "%3C")


def source_note(info: dict | None) -> str:
    if not info or not info.get("last_success"):
        return "aucune donnée"
    when = fmt_when(dt.datetime.fromisoformat(info["last_success"]).astimezone(TZ))
    note = f"relevé le {when}"
    if not info.get("last_run_ok", True):
        note += ", dernier essai en échec"
    return note


def render_html(showings: list[Showing], generated: dt.datetime,
                thumbs: dict[str, str], sources: dict[str, dict]) -> str:
    e = html.escape
    by_day: dict[dt.date, list[Showing]] = {}
    for s in showings:
        by_day.setdefault(s.start.date(), []).append(s)
    films: dict[str, str] = {}
    for s in showings:
        films.setdefault(film_key(s.film), s.film)

    # One CSS class per poster, so each image is embedded once, not per row.
    # The class name is a hash of the poster address, so it is stable across
    # rebuilds and the browser's seen list can keep it.
    poster_class: dict[str, str] = {}
    poster_css: list[str] = []
    for url, value in sorted(thumbs.items()):
        cls = poster_class_name(url)
        poster_class[url] = cls
        poster_css.append(f".{cls}{{background-image:url(\"{css_url(value)}\")}}")

    parts: list[str] = []
    parts.append("<!doctype html><html lang=\"fr\"><head><meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">")
    parts.append("<title>Deux salles, de cinéma</title>")
    parts.append(f"<style>{CSS}</style>")
    if poster_css:
        parts.append(f"<style>{''.join(poster_css)}</style>")
    parts.append("</head><body><main>")
    parts.append("<h1>Deux salles, de cinéma</h1>")
    if by_day:
        first, last = min(by_day), max(by_day)
        parts.append(f"<p class=\"sub\">Le Cosmo et l'ABC, à Toulouse · du {e(fmt_day_heading(first))} au {e(fmt_day_heading(last))} · "
                     f"{len(showings)} séances · mis à jour le {e(fmt_when(generated))}</p>")
    else:
        parts.append(f"<p class=\"sub\">Le Cosmo et l'ABC, à Toulouse · aucune séance à venir · mis à jour le {e(fmt_when(generated))}</p>")

    parts.append("<div class=\"filters\">")
    parts.append("<label><input type=\"checkbox\" id=\"f-cosmo\" checked> Cosmo</label>")
    parts.append("<label><input type=\"checkbox\" id=\"f-abc\" checked> ABC</label>")
    parts.append("<select id=\"f-film\" aria-label=\"Film\"><option value=\"\">Tous les films</option>")
    for key, title in sorted(films.items(), key=lambda kv: kv[1].casefold()):
        parts.append(f"<option value=\"{e(key)}\">{e(title)}</option>")
    parts.append("</select>")
    parts.append("<input type=\"search\" id=\"f-q\" placeholder=\"Chercher un film\" aria-label=\"Chercher un film\">")
    parts.append("<button type=\"button\" class=\"chip\" id=\"seen-toggle\" aria-controls=\"seen\" aria-expanded=\"false\">"
                 "Films vus · <span id=\"seen-count\">0</span></button>")
    parts.append("</div>")

    for d in sorted(by_day):
        parts.append(f"<section class=\"day\" id=\"d-{d.isoformat()}\"><h2>{e(fmt_day_heading(d))}</h2>")
        for s in by_day[d]:
            cls = "cosmo" if s.cinema == COSMO_SHORT else "abc"
            key = film_key(s.film)
            meta: list[str] = [f"<span class=\"c {cls}\">{e(s.cinema)}</span>"]
            if s.room:
                meta.append(f"<span>{e(s.room)}</span>")
            meta.append(f"<span>fin {e(fmt_hm(s.end))}</span>")
            if s.version:
                meta.append(f"<span>{e(s.version)}</span>")
            for t in s.tags:
                if t == "ST-SME":
                    continue
                meta.append(f"<span class=\"tag\">{e(t)}</span>")
            if s.director:
                meta.append(f"<span>{e(s.director)}</span>")
            name = f"<a href=\"{e(s.film_url)}\" target=\"_blank\" rel=\"noopener\">{e(s.film)}</a>" if s.film_url else e(s.film)
            pcls = poster_class.get(s.poster_url, "")
            thumb = f"<div class=\"th {pcls}\" role=\"img\" aria-label=\"Affiche\"></div>" if pcls else "<div class=\"th\"></div>"
            parts.append(
                f"<div class=\"row\" data-cinema=\"{cls}\" data-key=\"{e(key)}\" data-film=\"{e(s.film)}\">"
                f"<div class=\"t\">{e(fmt_hm(s.start))}</div>"
                f"{thumb}"
                f"<div class=\"f\"><div class=\"name\">{name}</div><div class=\"meta\">{''.join(meta)}</div></div>"
                f"<div class=\"act\">"
                f"<a class=\"add\" href=\"{e(gcal_link(s))}\" target=\"_blank\" rel=\"noopener\">+ Agenda</a>"
                f"<button type=\"button\" class=\"seen-btn\" data-seen=\"{e(key)}\" data-title=\"{e(s.film)}\" "
                f"data-poster=\"{pcls}\" title=\"Marquer ce film comme vu\">Vu</button>"
                f"</div></div>"
            )
        parts.append("</section>")

    parts.append("<p class=\"empty\" id=\"empty\" hidden>Aucune séance ne correspond aux filtres.</p>")
    parts.append(
        f"<p class=\"foot\">Sources : <a href=\"{e(COSMO_URL)}\">{e(COSMO_NAME)}</a> ({e(source_note(sources.get(COSMO_SHORT)))}) "
        f"et <a href=\"{e(ABC_URL)}\">{e(ABC_NAME)}</a> ({e(source_note(sources.get(ABC_SHORT)))}). "
        f"Le bouton « + Agenda » ouvre Google Agenda avec l'évènement prérempli ; il reste à appuyer sur Enregistrer. "
        f"« Vu » masque toutes les séances d'un film ; la liste des films vus reste dans ce navigateur.</p>"
    )
    parts.append("</main>")
    parts.append("<div class=\"backdrop\" id=\"backdrop\" hidden></div>")
    parts.append(
        "<aside class=\"drawer\" id=\"seen\" aria-label=\"Films vus\" aria-hidden=\"true\">"
        "<div class=\"drawer-head\"><h2>Films vus</h2>"
        "<button type=\"button\" class=\"close\" id=\"seen-close\" aria-label=\"Fermer\">×</button></div>"
        "<p class=\"hint\">Les séances de ces films sont masquées. La liste reste dans ce navigateur.</p>"
        "<ul id=\"seen-list\"></ul>"
        "<p class=\"empty\" id=\"seen-empty\">Aucun film marqué comme vu.</p>"
        "</aside>"
    )
    parts.append(f"<script>{JS}</script>")
    parts.append("</body></html>")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", default="seances.html", help="HTML file to write (default: seances.html)")
    ap.add_argument("--state", help="state file (default: cine-state.json next to the output)")
    ap.add_argument("--no-state", action="store_true", help="do not read or write the state file")
    ap.add_argument("--no-thumbs", action="store_true", help="do not download posters; no thumbnails on the page")
    ap.add_argument("--from-files", nargs=2, metavar=("COSMO_HTML", "ABC_HTML"),
                    help="parse these two saved pages instead of fetching (for tests)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        if not args.quiet:
            print(msg, file=sys.stderr)

    now = dt.datetime.now(TZ)
    today = now.date()
    state_path = args.state or os.path.join(os.path.dirname(os.path.abspath(args.output)), "cine-state.json")
    state = {} if args.no_state else load_state(state_path, log)
    stored: list[Showing] = []
    for d in state.get("showings", []):
        try:
            stored.append(showing_from_dict(d))
        except (KeyError, TypeError, ValueError) as e:
            log(f"state: dropped a bad row: {e}")
    sources: dict[str, dict] = state.get("sources", {})

    if args.from_files:
        with open(args.from_files[0], encoding="utf-8", errors="replace") as fh:
            c_shows, c_days = parse_cosmo(fh.read())
        with open(args.from_files[1], encoding="utf-8", errors="replace") as fh:
            a_shows, _ = parse_abc(fh.read())
        results = [
            SourceResult(COSMO_SHORT, c_shows, c_days),
            SourceResult(ABC_SHORT, a_shows, {s.start.date() for s in a_shows}),
        ]
    else:
        results = [fetch_cosmo(log), fetch_abc(today, log)]

    failed = False
    for r in results:
        for err in r.errors:
            log(f"{r.cinema}: {err}")
        days = f"{min(r.days).isoformat()}..{max(r.days).isoformat()}" if r.days else "no day"
        log(f"{r.cinema}: {len(r.showings)} showings, {days}, {r.requests} requests, {'ok' if r.ok else 'FAILED'}")
        info = dict(sources.get(r.cinema, {}))
        info["last_run"] = now.isoformat()
        info["last_run_ok"] = r.ok
        if r.ok:
            info["last_success"] = now.isoformat()
        else:
            failed = True
        if r.errors:
            info["last_error"] = r.errors[-1]
        sources[r.cinema] = info

    showings = merge(stored, results, now)
    dropped = len(stored) - sum(1 for s in stored if s.start >= now)
    log(f"merge: {len(stored)} stored, {dropped} past, {len(showings)} on the page")

    cache = state.get("posters", {})
    if args.no_thumbs:
        thumbs: dict[str, str] = {}
    else:
        thumbs, cache = build_thumbs(showings, cache, log)

    page = render_html(showings, now, thumbs, sources)
    write_atomic(args.output, page)
    log(f"wrote {args.output}: {len(showings)} showings")

    if not args.no_state:
        state = {
            "version": STATE_VERSION,
            "updated": now.isoformat(),
            "sources": sources,
            "showings": [showing_to_dict(s) for s in showings],
            "posters": cache,
        }
        write_atomic(state_path, json.dumps(state, ensure_ascii=False, indent=0))
        log(f"wrote {state_path}")

    if not showings:
        return 1
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
