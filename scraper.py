#!/usr/bin/env python3
"""
Student job scraper for Israel.

Pulls from open job APIs that need no credentials or approval:
  Greenhouse, Workday, Eightfold (two dialects), Google Careers.

Usage:
    python scraper.py                 # full run
    python scraper.py --students-only # only student/intern roles
    python scraper.py --source Intel  # one source
    python scraper.py --selftest      # run offline tests, no network

Outputs:
    jobs.db          SQLite store with first_seen / last_seen
    jobs.csv         all currently-open matching jobs
    report.html      browsable report, new jobs highlighted
"""

from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import http.cookiejar
import json
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "sources.yaml"
DB_PATH = HERE / "jobs.db"
CSV_PATH = HERE / "jobs.csv"
HTML_PATH = HERE / "report.html"

REQUEST_TIMEOUT = 60          # Comeet/Workday can be slow on large boards
POLITE_DELAY = 1.0            # seconds between requests to the same host

# JSON APIs are happy with an honest identifying agent.
API_USER_AGENT = "student-job-scraper/1.0 (personal job search tool)"

# HTML career pages are not. Google and Apple both serve a stripped page to
# unrecognised agents — the markup parses fine when fetched by a browser and
# comes back empty otherwise, which is exactly why those two connectors
# returned 0 while the parsers were correct. A normal desktop UA plus the
# language/encoding headers a browser always sends fixes it.
BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Accept-Encoding": "gzip, deflate",
}


# --------------------------------------------------------------------------
# Config loading (minimal YAML subset, so PyYAML stays optional)
# --------------------------------------------------------------------------

def load_config(path: Path = CONFIG_PATH) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text)
    except ImportError:
        raise SystemExit(
            "PyYAML is required.\n"
            "Install it with:  pip install pyyaml\n"
            "(or:  pip install pyyaml --break-system-packages)"
        )


# --------------------------------------------------------------------------
# The normalized record every connector must produce
# --------------------------------------------------------------------------

@dataclass
class Job:
    source: str                 # connector type, e.g. "greenhouse"
    company: str
    title: str
    url: str
    location: str = ""
    employment_type: str = ""
    experience_level: str = ""
    department: str = ""
    description: str = ""
    posted_at: str = ""         # ISO date if the source gives one
    qualifications: str = ""    # minimum-requirements text, when the source gives one
    is_student: bool = False
    in_israel: bool = False
    is_senior: bool = False
    is_part_time: bool = False
    min_years: int | None = None   # None = source gave no usable signal
    posted_date: str = ""          # normalized ISO date; "" = source gave none
    hash: str = field(default="", init=False)

    def __post_init__(self) -> None:
        # Identity = company + title + location. Deliberately excludes the URL,
        # because the same role reposted gets a new ID but is not a new job.
        key = "|".join([
            _norm(self.company), _norm(self.title), _norm(self.location)
        ])
        self.hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _norm(s: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation noise."""
    s = (s or "").lower().strip()
    s = re.sub(r"[\s ]+", " ", s)
    s = re.sub(r"[^\w\s֐-׿]", "", s)
    return s


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def normalize_date(raw: str, today: date | None = None) -> str:
    """
    Turn whatever a source calls a date into ISO yyyy-mm-dd.

    Sources disagree wildly:
      Greenhouse / Rafael / Elbit  '2026-08-13T01:16:00'  -> exact
      Apple                        'Aug 06, 2026'         -> exact
      Workday                      '3 Days Ago', 'Today'  -> relative
      Workday                      '30+ Days Ago'         -> approximate

    Returns "" when nothing usable is present. An unknown date must stay
    distinguishable from an old one, otherwise a date filter would quietly
    hide every job from sources that publish no date at all.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    today = today or datetime.now(timezone.utc).date()

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)

    m = re.match(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"

    low = s.lower()
    if "today" in low or "just posted" in low or "היום" in s:
        return today.isoformat()
    if "yesterday" in low or "אתמול" in s:
        return (today - timedelta(days=1)).isoformat()

    m = re.search(r"(\d+)\s*\+?\s*(day|days|יום|ימים)", low)
    if m:
        return (today - timedelta(days=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*\+?\s*(week|weeks|שבוע|שבועות)", low)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*\+?\s*(month|months|חודש|חודשים)", low)
    if m:
        return (today - timedelta(days=30 * int(m.group(1)))).isoformat()

    # Radancy career sites print US-style mm/dd/yyyy. Checked last, so it can
    # never shadow one of the unambiguous forms above.
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            return f"{yyyy}-{mm:02d}-{dd:02d}"
    return ""


def decode_body(data: bytes, encoding: str | None, charset: str = "utf-8") -> str:
    """
    Decompress a response body.

    Rafael's endpoint returns gzip unconditionally — `Vary: Accept-Encoding` is
    set but the nginx proxy in front of it serves the compressed variant even
    when the client asks for identity. http_text already handled this; http_json
    did not, so the raw gzip bytes hit json.loads and produced
    "Expecting value: line 1 column 1 (char 0)". Both paths use this now.
    """
    enc = (encoding or "").lower()
    if enc == "gzip" or data[:2] == b"\x1f\x8b":
        import gzip
        try:
            data = gzip.decompress(data)
        except OSError:
            pass
    elif enc == "deflate":
        import zlib
        try:
            data = zlib.decompress(data)
        except zlib.error:
            data = zlib.decompress(data, -zlib.MAX_WBITS)
    elif enc == "br":
        try:
            import brotli  # type: ignore
            data = brotli.decompress(data)
        except ImportError as e:
            raise FetchError(
                "server sent brotli; install it with:  pip install brotli"
            ) from e
    return data.decode(charset, errors="replace")


# --------------------------------------------------------------------------
# Fuzzy title search
# --------------------------------------------------------------------------

FUZZY_THRESHOLD = 0.9        # 90% similarity, as requested

# Hebrew final forms. "סטודנטים" and "סטודנט" should match each other, and a
# word typed with a final letter should match the same word mid-string.
_HEB_FINALS = str.maketrans("ךםןףץ", "כמנפצ")
_NIQQUD = re.compile(r"[֑-ׇ]")


def normalize_search_text(s: str) -> str:
    """Lowercase, drop Hebrew vowel marks, unify Hebrew final letters."""
    s = (s or "").lower()
    s = _NIQQUD.sub("", s)
    return s.translate(_HEB_FINALS)


def tokenize(s: str) -> list[str]:
    """
    Split into words. Hebrew inclusive forms like "סטודנט.ית" split on the dot,
    which is helpful — searching "סטודנט" then matches the first token exactly.
    """
    return [t for t in re.split(r"[^\w֐-׿]+", normalize_search_text(s)) if t]


def word_matches(needle: str, tokens: list[str],
                 threshold: float = FUZZY_THRESHOLD) -> bool:
    """
    One search word against one title's tokens.

    Two ways to match:
      1. substring — so partial typing works ("engin" finds "engineer")
      2. similarity >= threshold — so typos work ("enginer" finds "engineer")
    """
    if not needle:
        return True
    for t in tokens:
        if needle in t or t in needle:
            return True
        if difflib.SequenceMatcher(None, needle, t).ratio() >= threshold:
            return True
    return False


def title_matches(query: str, title: str,
                  threshold: float = FUZZY_THRESHOLD) -> bool:
    """
    Every search word must match somewhere in the title, independently and in
    any order. "student data" finds "Data Science Student"; word order and
    case are irrelevant.
    """
    needles = tokenize(query)
    if not needles:
        return True
    tokens = tokenize(title)
    return all(word_matches(n, tokens, threshold) for n in needles)


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class FetchError(Exception):
    pass


_last_request_at: dict[str, float] = {}

# A shared cookie jar. Several Israeli career sites sit behind a WAF that hands
# out a session cookie on the first HTML hit and returns an empty body to any
# API call that arrives without it — Rafael behaves exactly this way, which is
# why its endpoint works in a browser and returned nothing from Python.
_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar)
)
_warmed: set[str] = set()


def warm_up(url: str) -> None:
    """Visit a site's HTML once so the WAF issues its cookies."""
    host = urllib.parse.urlparse(url).netloc
    if host in _warmed:
        return
    _warmed.add(host)
    try:
        http_text(f"https://{host}/")
    except FetchError:
        pass          # not fatal; the API may not need it


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).netloc
    last = _last_request_at.get(host, 0.0)
    wait = POLITE_DELAY - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_request_at[host] = time.time()


# Microsoft's Eightfold tenant answers HTTP 429 "Please try again later" to
# roughly one request in three, even at one request per second, and sends no
# Retry-After. Without a retry a single unlucky page failed the whole source.
RETRY_STATUSES = (429, 503)
RETRY_WAITS = (5, 15, 30)       # seconds; used when the server names no wait


def _open(req: urllib.request.Request):
    """_opener.open, retried with backoff when the server asks us to slow down."""
    for wait in RETRY_WAITS:
        try:
            return _opener.open(req, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES:
                raise
            asked = e.headers.get("Retry-After", "")
            time.sleep(min(int(asked), 60) if asked.isdigit() else wait)
    return _opener.open(req, timeout=REQUEST_TIMEOUT)


def http_json(url: str, method: str = "GET", payload: dict | None = None,
              referer: str | None = None) -> Any:
    _throttle(url)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    # Greenhouse/Workday/Eightfold don't care about the agent, but sites that
    # merely *host* a JSON endpoint next to a normal website often do — Rafael
    # is one. Sending browser-ish headers costs nothing and avoids a class of
    # silent empty responses.
    headers = {
        "User-Agent": BROWSER_HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": BROWSER_HEADERS["Accept-Language"],
        "X-Requested-With": "XMLHttpRequest",
        "Accept-Encoding": "gzip, deflate",
        "X-Client": API_USER_AGENT,
    }
    ref = urllib.parse.urlparse(url)
    headers["Referer"] = referer or f"{ref.scheme}://{ref.netloc}/"
    headers["Origin"] = f"{ref.scheme}://{ref.netloc}"
    headers["Sec-Fetch-Dest"] = "empty"
    headers["Sec-Fetch-Mode"] = "cors"
    headers["Sec-Fetch-Site"] = "same-origin"
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open(req) as resp:
            body = decode_body(resp.read(),
                               resp.headers.get("Content-Encoding"),
                               resp.headers.get_content_charset() or "utf-8")
        if not body.strip():
            raise FetchError(
                f"empty body from {url} (HTTP {resp.status}). The site likely "
                "requires a session cookie or blocks non-browser clients."
            )
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            snippet = body.strip()[:120].replace("\n", " ")
            raise FetchError(
                f"non-JSON response from {url}: {snippet!r}"
            ) from e
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} for {url}") from e
    except Exception as e:
        raise FetchError(f"{type(e).__name__} for {url}: {e}") from e


def http_text(url: str) -> str:
    _throttle(url)
    req = urllib.request.Request(url, headers=dict(BROWSER_HEADERS))
    try:
        with _open(req) as resp:
            return decode_body(resp.read(),
                               resp.headers.get("Content-Encoding"),
                               resp.headers.get_content_charset() or "utf-8")
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} for {url}") from e
    except Exception as e:
        raise FetchError(f"{type(e).__name__} for {url}: {e}") from e


# --------------------------------------------------------------------------
# Connectors
# --------------------------------------------------------------------------

def fetch_greenhouse(cfg: dict) -> list[Job]:
    """Public documented API: https://developers.greenhouse.io/job-board.html"""
    token = cfg["token"]
    url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{token}/jobs?content=true"
    )
    data = http_json(url)
    return _parse_greenhouse_payload(data, cfg["name"])


def fetch_workday(cfg: dict) -> list[Job]:
    """
    POST {tenant}.{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

    If student_facets is set, only those are pulled — far fewer requests than
    downloading the whole board. Otherwise everything is pulled and filtered
    by keyword downstream.

    search_text narrows a very large tenant server-side. Workday searches the
    posting text, not the location field, so it is a blunt instrument — use it
    only where downloading 2000 rows at one request per second is too slow, and
    check with --debug that it does not return zero.
    """
    host, tenant, site = cfg["host"], cfg["tenant"], cfg["site"]
    base = f"https://{tenant}.{host}.myworkdayjobs.com"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    facets = cfg.get("student_facets") or {}
    search_text = cfg.get("search_text", "") or ""

    out: list[Job] = []
    offset, page_size, seen = 0, 20, set()
    while offset < 2000:                      # hard stop; Workday caps around 2000
        body = {
            "appliedFacets": facets,
            "limit": page_size,
            "offset": offset,
            "searchText": search_text,
        }
        data = http_json(url, method="POST", payload=body)
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for p in postings:
            path = p.get("externalPath") or ""
            job_url = f"{base}/{site}{path}" if path else base
            key = (p.get("title", ""), p.get("locationsText", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(Job(
                source="workday",
                company=cfg["name"],
                title=p.get("title", ""),
                url=job_url,
                location=p.get("locationsText", "") or "",
                posted_at=_workday_posted(p.get("postedOn", "")),
            ))
        if len(postings) < page_size:
            break
        offset += page_size
    return out


def _workday_posted(s: str) -> str:
    """Workday returns things like 'Posted 3 Days Ago'. Not a date — keep raw."""
    return (s or "").replace("Posted ", "").strip()


def fetch_eightfold_pcsx(cfg: dict) -> list[Job]:
    """Microsoft's Eightfold dialect. /api/apply/v2/jobs returns 403 here."""
    base, domain = cfg["base"].rstrip("/"), cfg["domain"]
    # Some tenants (Amdocs) return 0 for every location value, so allow the
    # filter to be turned off and let the Israel classifier do the work.
    loc = urllib.parse.quote(cfg.get("location", "")) if cfg.get("use_location_param", True) else ""
    out: list[Job] = []
    seen = set()
    for start in range(0, 200, 10):
        url = (
            f"{base}/api/pcsx/search?domain={domain}"
            f"&start={start}&num=10&sort_by=relevance"
        )
        if loc:
            url += f"&location={loc}"
        data = http_json(url)
        positions = ((data.get("data") or {}).get("positions")) or []
        if not positions:
            break
        new = 0
        for p in positions:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            new += 1
            out.append(Job(
                source="eightfold",
                company=cfg["name"],
                title=p.get("name", ""),
                url=base + (p.get("positionUrl") or ""),
                location="; ".join(p.get("locations") or []),
                department=p.get("department", "") or "",
                posted_at=_ts_to_date(p.get("postedTs")),
            ))
        if new == 0:
            break
    return out


def fetch_eightfold_v2(cfg: dict) -> list[Job]:
    """Standard public Eightfold endpoint (Amdocs and most other tenants)."""
    base, domain = cfg["base"].rstrip("/"), cfg["domain"]
    loc = urllib.parse.quote(cfg.get("location", ""))
    out: list[Job] = []
    for start in range(0, 300, 100):
        url = (
            f"{base}/api/apply/v2/jobs?domain={domain}"
            f"&start={start}&num=100&sort_by=relevance"
        )
        if loc:
            url += f"&location={loc}"
        data = http_json(url)
        positions = data.get("positions") or []
        if not positions:
            break
        for p in positions:
            out.append(Job(
                source="eightfold",
                company=cfg["name"],
                title=p.get("name", ""),
                url=p.get("canonicalPositionUrl") or p.get("positionUrl") or "",
                location=p.get("location", "") or "",
                department=p.get("department", "") or "",
                description=strip_html(p.get("job_description", ""))[:4000],
                posted_at=_ts_to_date(p.get("t_create")),
            ))
        if len(positions) < 100:
            break
    return out


def _ts_to_date(ts: Any) -> str:
    """Epoch seconds or milliseconds -> ISO date. Lever reports milliseconds."""
    try:
        n = int(ts)
    except Exception:
        return ""
    if n > 10_000_000_000:        # anything this large is milliseconds
        n //= 1000
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


GOOGLE_BASE = "https://www.google.com/about/careers/applications/"
GOOGLE_CARD_SPLIT = re.compile(r'<li[^>]*class="[^"]*lLd3Je[^"]*"', re.I)
GOOGLE_H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.I | re.S)
GOOGLE_ID = re.compile(r"\b\d{15,20}\b")


def _slugify(s: str) -> str:
    s = s.lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _parse_google_page(body: str) -> list[Job]:
    """
    Read the results page card by card.

    Google's server HTML contains the cards, titles and job IDs but *no*
    anchor tags — the links are built client-side by JavaScript. So the URL
    is reconstructed here from the job ID plus the slugified title, which is
    exactly the shape the page produces: /jobs/results/{id}-{slug}
    """
    out: list[Job] = []
    for chunk in GOOGLE_CARD_SPLIT.split(body)[1:]:
        h3 = GOOGLE_H3.search(chunk)
        if not h3:
            continue
        title = strip_html(h3.group(1))
        if not title:
            continue
        jid = GOOGLE_ID.search(chunk)
        if not jid:
            continue
        url = f"{GOOGLE_BASE}jobs/results/{jid.group(0)}-{_slugify(title)}"

        text = strip_html(chunk)
        loc = ""
        lm = re.search(r"place\s+(.*?)(?:\s+bar_chart|\s+Minimum qualifications|$)",
                       text, re.I)
        if lm:
            loc = re.sub(r"\s*;\s*\+\d+ more.*$", "", lm.group(1).strip())[:200]
        quals = ""
        qm = re.search(r"Minimum qualifications(.*?)(?:Learn more|share|$)",
                       text, re.I | re.S)
        if qm:
            quals = qm.group(1).strip()[:2000]
        level = ""
        vm = re.search(r"bar_chart\s+(\w+)", text, re.I)
        if vm:
            level = vm.group(1)

        out.append(Job(
            source="google_careers", company="Google", title=title, url=url,
            location=loc, experience_level=level, qualifications=quals,
        ))
    return out


def fetch_google(cfg: dict) -> list[Job]:
    """
    Google's results page is server-side rendered, so no API is needed.

    NOTE: the most fragile connector here — it reads page markup, not a
    contract. If Google reports 0 jobs while other sources are healthy,
    the markup changed. Check `lLd3Je` (card class) and the href shape first.
    """
    loc = urllib.parse.quote(cfg.get("location", "Israel"))
    out: list[Job] = []
    seen: set[str] = set()
    for page in range(1, int(cfg.get("max_pages", 5)) + 1):
        url = (
            "https://www.google.com/about/careers/applications/jobs/results"
            f"?location={loc}&page={page}"
        )
        try:
            body = http_text(url)
        except FetchError:
            break
        jobs = _parse_google_page(body)
        fresh = [j for j in jobs if j.url not in seen]
        if not fresh:
            break
        for j in fresh:
            seen.add(j.url)
        out.extend(fresh)
    return out


# ---- Apple -------------------------------------------------------------
# jobs.apple.com search results are server-rendered. Cards are li.rc-accordion-item,
# title in <h3>, and the card text runs "Title / Team / Date / Location / City".
APPLE_BASE = "https://jobs.apple.com"
# NOTE: do NOT split on rc-accordion-item. In the server HTML that class belongs
# to the filter sidebar; only after JavaScript runs do job cards inherit it.
# Anchors are matched directly instead. The href may carry a query string, so
# the character class must not exclude "?" — that bug made this connector
# return 0 while the page was perfectly readable.
APPLE_ANCHOR = re.compile(
    r'<a[^>]*href="(/[a-z-]{2,7}/details/[^"]+)"[^>]*>([\s\S]{0,200}?)</a>', re.I
)
APPLE_TEAM = re.compile(r'class="team-name[^"]*"[^>]*>(.*?)<', re.I | re.S)
APPLE_DATE = re.compile(r'class="job-posted-date"[^>]*>(.*?)<', re.I | re.S)


def _parse_apple_page(body: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    for m in APPLE_ANCHOR.finditer(body):
        path = html.unescape(m.group(1)).split("?")[0].split("#")[0]
        title = strip_html(m.group(2))
        if not title or path in seen:
            continue
        seen.add(path)
        tail = body[m.end():m.end() + 1200]
        team = strip_html(t.group(1)) if (t := APPLE_TEAM.search(tail)) else ""
        posted = strip_html(d.group(1)) if (d := APPLE_DATE.search(tail)) else ""
        text = strip_html(tail)
        loc = ""
        lm = re.search(r"Location\s+(.*?)(?:\s+(?:Actions|Add to|Save)|$)", text, re.I)
        if lm:
            loc = lm.group(1).strip()[:120]
        out.append(Job(source="apple", company="Apple", title=title,
                       url=APPLE_BASE + path, location=loc, department=team,
                       posted_at=posted, qualifications=text[:1500]))
    return out


def fetch_apple(cfg: dict) -> list[Job]:
    loc = cfg.get("location_code", "israel-ISR")
    out: list[Job] = []
    seen: set[str] = set()
    for page in range(1, int(cfg.get("max_pages", 6)) + 1):
        url = f"{APPLE_BASE}/en-us/search?location={loc}&page={page}"
        try:
            body = http_text(url)
        except FetchError:
            break
        fresh = [j for j in _parse_apple_page(body) if j.url not in seen]
        if not fresh:
            break
        for j in fresh:
            seen.add(j.url)
        out.extend(fresh)
    return out


# ---- Mobileye ----------------------------------------------------------
# careers.mobileye.com/jobs renders every job on one page inside div.jobItem.
# Card text is "Department / Title / Location / Employment type / Description".
MOBILEYE_BASE = "https://careers.mobileye.com"
MOBILEYE_CARD_SPLIT = re.compile(r'<div[^>]*class="[^"]*jobItem[^"]*"', re.I)
MOBILEYE_LINK = re.compile(r'href="(/jobs/[^"#?]+/[0-9a-f-]{16,})"', re.I)


def _parse_mobileye_page(body: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    for chunk in MOBILEYE_CARD_SPLIT.split(body)[1:]:
        m = MOBILEYE_LINK.search(chunk)
        if not m:
            continue
        path = html.unescape(m.group(1))
        if path in seen:
            continue
        seen.add(path)
        # Splitting on the opening <div ...> leaves its trailing ">" attached to
        # the chunk, which would shift every field by one. Drop it first.
        chunk = re.sub(r"^[^>]*>", "", chunk, count=1)
        # Text nodes in document order: department, title, location, type, desc
        parts = [html.unescape(p).strip()
                 for p in re.split(r"<[^>]+>", chunk) if p.strip()]
        dept = parts[0] if len(parts) > 0 else ""
        title = parts[1] if len(parts) > 1 else ""
        loc = parts[2] if len(parts) > 2 else ""
        etype = parts[3] if len(parts) > 3 else ""
        desc = " ".join(parts[4:])[:3000]
        if not title:
            slug = path.split("/")[2] if len(path.split("/")) > 2 else ""
            title = slug.replace("-", " ").title()
        out.append(Job(source="mobileye", company="Mobileye", title=title,
                       url=MOBILEYE_BASE + path, location=loc, department=dept,
                       employment_type=etype, description=desc))
    return out


def fetch_mobileye(cfg: dict) -> list[Job]:
    """Everything is on one page — no pagination."""
    body = http_text(f"{MOBILEYE_BASE}/jobs")
    return _parse_mobileye_page(body)


# ---- Rafael ------------------------------------------------------------
# Custom WordPress + Vue site with a clean public JSON endpoint.
#   GET https://career.rafael.co.il/api/jobs -> {status, code, data: [...]}
# Each row: id, tl (title), ar [area ids], jt [job type ids], pr [profession ids]
# Job type 2 = "משרת סטודנט" — an explicit student flag, which is rare and
# far more reliable than keyword matching.
RAFAEL_BASE = "https://career.rafael.co.il"
RAFAEL_STUDENT_JOB_TYPE = 2


def fetch_rafael(cfg: dict) -> list[Job]:
    # Load the careers page first so the WAF cookie is in the jar, then call
    # the API with the search page as Referer — the same order a browser uses.
    warm_up(RAFAEL_BASE)
    try:
        http_text(f"{RAFAEL_BASE}/search/")
    except FetchError:
        pass
    return _parse_rafael_rows(
        http_json(f"{RAFAEL_BASE}/api/jobs", referer=f"{RAFAEL_BASE}/search/"), cfg)


def _parse_rafael_rows(data: dict, cfg: dict) -> list[Job]:
    areas = {int(k): v for k, v in (cfg.get("areas") or {}).items()}
    rows = data.get("data") or []
    out: list[Job] = []
    for r in rows:
        loc = ", ".join(areas.get(a, "") for a in (r.get("ar") or []) if areas.get(a))
        is_student = RAFAEL_STUDENT_JOB_TYPE in (r.get("jt") or [])
        j = Job(
            source="rafael",
            company=cfg["name"],
            title=r.get("tl", ""),
            url=f"{RAFAEL_BASE}/job/{r.get('id')}",
            # Every Rafael site is in Israel; keep the site name and add the
            # country so the Israel classifier matches even for sites whose
            # Hebrew name isn't in israel_terms.
            location=(loc + " (ישראל)") if loc else "ישראל",
            employment_type="משרת סטודנט" if is_student else "",
        )
        out.append(j)
    return out


# ---- Elbit Systems -----------------------------------------------------
# Next.js site that reads a static JSON file refreshed by a cron job:
#   GET https://elbitsystemscareer.com/cron/jobs.json -> [ {...}, ... ]
# Rich rows: jobId, jobTitle, jobCode, openDate, updateDate, area,
# employerName (subsidiary), description, requirements, status.
ELBIT_BASE = "https://elbitsystemscareer.com"
ELBIT_AREA_HE = {
    "Center": "מרכז", "North": "צפון", "South": "דרום",
    "Jerusalem": "ירושלים", "Sharon": "שרון", "Shfela": "השפלה",
    "Haifa": "חיפה", "Dan": "גוש דן",
}


def fetch_elbit(cfg: dict) -> list[Job]:
    return _parse_elbit_rows(http_json(f"{ELBIT_BASE}/cron/jobs.json"), cfg)


def _parse_elbit_rows(rows: Any, cfg: dict) -> list[Job]:
    if not isinstance(rows, list):
        rows = rows.get("jobs") or rows.get("data") or []
    out: list[Job] = []
    for r in rows:
        if r.get("status") not in (None, 1, "1"):
            continue                       # 1 = open
        area = r.get("area") or r.get("locationAddress") or ""
        area_he = ELBIT_AREA_HE.get(area, area)
        out.append(Job(
            source="elbit",
            company=cfg["name"],
            title=r.get("jobTitle", ""),
            # No public deep-link pattern was found; the listing is the honest
            # destination. jobCode goes in department so the role is findable.
            url=f"{ELBIT_BASE}/jobs/",
            location=f"{area_he} (ישראל)" if area_he else "ישראל",
            department=f"{r.get('employerName','')} · קוד {r.get('jobCode','')}".strip(" ·"),
            description=strip_html(r.get("description") or "")[:4000],
            qualifications=strip_html(r.get("requirements") or "")[:2000],
            posted_at=(r.get("openDate") or "")[:10],
        ))
    return out


# ---- Lever -------------------------------------------------------------
# Public documented API, no key: https://github.com/lever/postings-api
#   GET https://api.lever.co/v0/postings/{site}?mode=json&limit=100&skip=N
# `token` is the site name — the path segment on jobs.lever.co/{site}.
LEVER_PAGE = 100
LEVER_MAX = 2000


def _parse_lever_rows(rows: Any, company: str) -> list[Job]:
    out: list[Job] = []
    for j in rows or []:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ""
        all_loc = [x for x in (cats.get("allLocations") or []) if x]
        if all_loc:
            loc = ", ".join(dict.fromkeys([loc] + all_loc if loc else all_loc))
        quals = " ".join(
            strip_html(l.get("content", "")) for l in (j.get("lists") or [])
        )
        out.append(Job(
            source="lever", company=company, title=j.get("text", ""),
            url=j.get("hostedUrl") or j.get("applyUrl") or "",
            location=loc,
            department=cats.get("department") or cats.get("team") or "",
            employment_type=cats.get("commitment") or "",
            experience_level=cats.get("level") or "",
            description=strip_html(
                j.get("descriptionPlain") or j.get("description") or ""
            )[:4000],
            qualifications=quals[:3000],
            posted_at=_ts_to_date(j.get("createdAt")),
        ))
    return out


def fetch_lever(cfg: dict) -> list[Job]:
    site = cfg["token"]
    base = cfg.get("base", "https://api.lever.co").rstrip("/")
    out: list[Job] = []
    skip = 0
    while skip < LEVER_MAX:
        url = f"{base}/v0/postings/{site}?mode=json&limit={LEVER_PAGE}&skip={skip}"
        rows = http_json(url)
        if not isinstance(rows, list) or not rows:
            break
        out.extend(_parse_lever_rows(rows, cfg["name"]))
        if len(rows) < LEVER_PAGE:
            break
        skip += LEVER_PAGE
    return out


# ---- Ashby -------------------------------------------------------------
# Public documented API, no key:
#   GET https://api.ashbyhq.com/posting-api/job-board/{token}
# One request returns the whole board, so there is no pagination.
def _parse_ashby_payload(data: Any, company: str) -> list[Job]:
    out: list[Job] = []
    for j in (data or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue          # reachable by direct link only, not a real opening
        locs = [j.get("location") or ""]
        for sec in (j.get("secondaryLocations") or []):
            if isinstance(sec, dict):
                locs.append(sec.get("location")
                            or (sec.get("address") or {}).get("postalAddress", {})
                            .get("addressLocality", ""))
            elif isinstance(sec, str):
                locs.append(sec)
        loc = ", ".join(dict.fromkeys([x for x in locs if x]))
        out.append(Job(
            source="ashby", company=company, title=j.get("title", ""),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            location=loc,
            department=j.get("department") or j.get("team") or "",
            employment_type=j.get("employmentType") or "",
            description=(j.get("descriptionPlain")
                         or strip_html(j.get("descriptionHtml") or ""))[:4000],
            posted_at=(j.get("publishedAt") or "")[:10],
        ))
    return out


def fetch_ashby(cfg: dict) -> list[Job]:
    token = cfg["token"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    return _parse_ashby_payload(http_json(url), cfg["name"])


# ---- SmartRecruiters ---------------------------------------------------
# Public Posting API, no key: https://developers.smartrecruiters.com
#   GET https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N
# `token` is the company identifier — the path segment on
# jobs.smartrecruiters.com/{id}. The listing carries no description text, so
# --max-years cannot work here (same limitation as Workday and Eightfold).
SR_PAGE = 100
SR_MAX = 3000


def _parse_smartrecruiters_rows(rows: Any, company: str,
                                company_id: str) -> list[Job]:
    out: list[Job] = []
    for j in rows or []:
        loc = j.get("location") or {}
        country = (loc.get("country") or "").strip()
        # The API reports ISO-2 country codes ("il"), which no israel_term
        # would ever match. Spell the country out so the classifier can see it.
        if len(country) == 2:
            country = {"il": "Israel"}.get(country.lower(), country.upper())
        parts = [loc.get("city"), loc.get("region"), country]
        location = ", ".join(p for p in parts if p)
        if loc.get("remote"):
            location = (location + ", Remote").lstrip(", ")
        jid = j.get("id") or j.get("uuid") or ""
        out.append(Job(
            source="smartrecruiters", company=company, title=j.get("name", ""),
            url=f"https://jobs.smartrecruiters.com/{company_id}/{jid}",
            location=location,
            department=(j.get("department") or {}).get("label", ""),
            employment_type=(j.get("typeOfEmployment") or {}).get("label", ""),
            experience_level=(j.get("experienceLevel") or {}).get("label", ""),
            posted_at=(j.get("releasedDate") or "")[:10],
        ))
    return out


def fetch_smartrecruiters(cfg: dict) -> list[Job]:
    company_id = cfg["token"]

    def pull(country: str) -> list[Any]:
        rows: list[Any] = []
        offset = 0
        while offset < SR_MAX:
            q = {"limit": SR_PAGE, "offset": offset}
            if country:
                q["country"] = country
            url = ("https://api.smartrecruiters.com/v1/companies/"
                   f"{company_id}/postings?{urllib.parse.urlencode(q)}")
            data = http_json(url)
            content = data.get("content") or []
            rows.extend(content)
            total = int(data.get("totalFound") or 0)
            offset += SR_PAGE
            if not content or offset >= total:
                break
        return rows

    country = cfg.get("country", "il")
    rows = pull(country)
    # A country filter that silently matches nothing looks identical to a board
    # with no Israeli roles. Retry unfiltered and let the classifier decide.
    if country and not rows:
        rows = pull("")
    return _parse_smartrecruiters_rows(rows, cfg["name"], company_id)


# ---- Radancy / TalentBrew ---------------------------------------------
# No API, but results are rendered server-side and paginate through the URL
# path, so a plain fetch works. Palo Alto Networks runs on this.
#   {base}/en/location/israel-jobs/{orgid}/{facet}/2/{page}
# `path` must contain {page}. Everything else about the URL is copied straight
# from the site's own location filter, so a different company only needs a new
# base + path — no code change.
# Card markup, confirmed against a saved page on 2026-08-23:
#   <a class="...search-results-link" href="/en/job/petah-tikva/.../47263/991...">
#     <h2 class="...search-results-job-title">IT Internal Audit Manager</h2>
#     <span class="...result-location newLoc">Petach Tikva, Israel</span>
#     <span class="...result-category"><span>Internal Audit</span></span>
# The class prefix ("section29__") is per-tenant, so every pattern matches on
# the stable suffix instead. There is no posted date anywhere on the card.
RADANCY_JOB_LINK = re.compile(
    r'href="((?:https?://[^"/]+)?(?:/[a-z]{2}(?:-[a-z]{2})?)?/job/[^"#?]+)"', re.I)
RADANCY_TITLE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.I | re.S)
RADANCY_LOC = re.compile(
    r'class="[^"]*(?:result-location|job-location)[^"]*"[^>]*>(.*?)</\s*span',
    re.I | re.S)
RADANCY_CAT = re.compile(
    r'class="[^"]*result-category[^"]*"[^>]*>(.*?)</\s*span', re.I | re.S)
RADANCY_DATE = re.compile(
    r'class="[^"]*(?:date-posted|job-date)[^"]*"[^>]*>(.*?)</\s*span', re.I | re.S)


def _parse_radancy_page(body: str, company: str, base: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    matches = list(RADANCY_JOB_LINK.finditer(body))
    for i, m in enumerate(matches):
        href = html.unescape(m.group(1)).split("?")[0].rstrip("/")
        if href in seen:
            continue
        seen.add(href)
        # Stop at the next job link. Without this bound the fields of one card
        # bleed into the next, which is how the location column ended up
        # holding a job title.
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[m.end():min(end, m.end() + 4000)]

        t = RADANCY_TITLE.search(chunk)
        title = strip_html(t.group(1)) if t else ""
        if not title:
            segs = [x for x in href.split("/") if x]
            slug = segs[-2] if len(segs) >= 2 else (segs[-1] if segs else "")
            title = slug.replace("-", " ").title()

        l = RADANCY_LOC.search(chunk)
        loc = strip_html(l.group(1)) if l else ""
        if not loc:
            # No location class on this tenant: read the text after the title
            # only, and require a comma so a stray heading cannot pass for one.
            tail = strip_html(chunk[t.end():] if t else chunk)
            lm = re.search(r"([A-Za-z][\w .'\-]{1,40},\s*[A-Za-z][\w .'\-]{1,40})",
                           tail)
            loc = lm.group(1) if lm else ""

        c = RADANCY_CAT.search(chunk)
        d = RADANCY_DATE.search(chunk)
        out.append(Job(
            source="radancy", company=company, title=title,
            url=href if href.startswith("http") else base + href,
            location=loc,
            department=strip_html(c.group(1)) if c else "",
            posted_at=strip_html(d.group(1)) if d else "",
        ))
    return out


def fetch_radancy(cfg: dict) -> list[Job]:
    base = cfg["base"].rstrip("/")
    path = cfg["path"]
    warm_up(base)
    out: list[Job] = []
    seen: set[str] = set()
    verbose = bool(cfg.get("verbose"))
    for page in range(1, int(cfg.get("max_pages", 12)) + 1):
        try:
            body = http_text(base + path.format(page=page))
        except FetchError as e:
            # Page 1 failing is a broken source, not the end of the results.
            # Saying so beats returning an empty list that reads as "0 jobs".
            if page == 1:
                raise FetchError(f"{e} (page 1 of {cfg['name']})") from e
            break
        fresh = [j for j in _parse_radancy_page(body, cfg["name"], base)
                 if j.url not in seen]
        if verbose:
            print(f"\n    page {page}: {len(fresh)} new", end="", flush=True)
        if not fresh:
            break              # header links repeat on every page; no new rows = done
        for j in fresh:
            seen.add(j.url)
        out.extend(fresh)
    return out


# ---- Comeet / Spark Hire Recruit ---------------------------------------
# Public documented API. Needs a company UID *and* a token:
#   GET https://www.comeet.co/careers-api/2.0/company/{uid}/positions
#       ?token={token}&details=true
# The token is not derivable from the UID — token=UID returns HTTP 400. But it
# is not a secret either: Comeet's own widget documentation puts it in the
# page source of the company's careers site, inside COMEET.init({...}).
# `--probe <careers page URL>` pulls both values out and prints the YAML.
#
# details=true returns the Description and Requirements text, which makes
# Comeet one of the few sources where --max-years actually works.
COMEET_BASE = "https://www.comeet.co/careers-api/2.0"


def _comeet_location(loc: Any) -> str:
    if isinstance(loc, str):
        return loc
    if not isinstance(loc, dict):
        return ""
    name = (loc.get("name") or "").strip()
    parts = [loc.get("city"), loc.get("state"), loc.get("country")]
    composed = ", ".join(str(p).strip() for p in parts if p)
    # name is often already "Tel Aviv, Israel"; prefer whichever says more.
    return composed if len(composed) > len(name) else name


def _parse_comeet_rows(rows: Any, company: str) -> list[Job]:
    if isinstance(rows, dict):
        rows = rows.get("positions") or rows.get("data") or []
    out: list[Job] = []
    for j in rows or []:
        if not isinstance(j, dict):
            continue
        status = (j.get("status") or "open").lower()
        if status != "open" or j.get("is_internal"):
            continue
        details = {}
        for d in (j.get("details") or []):
            if isinstance(d, dict) and d.get("name"):
                details[str(d["name"]).strip().lower()] = strip_html(
                    str(d.get("value") or ""))
        url = (j.get("url_active_page") or j.get("url_comeet_hosted_page")
               or j.get("url_recruit_hosted_page") or j.get("URL")
               or j.get("url") or "")
        out.append(Job(
            source="comeet", company=company, title=j.get("name", ""),
            url=url, location=_comeet_location(j.get("location")),
            department=j.get("department") or "",
            employment_type=j.get("employment_type") or "",
            experience_level=j.get("experience_level") or "",
            description=details.get("description", "")[:4000],
            qualifications=details.get("requirements", "")[:3000],
            posted_at=str(j.get("time_updated") or "")[:10],
        ))
    return out


def fetch_comeet(cfg: dict) -> list[Job]:
    uid, token = cfg["uid"], cfg.get("token")
    if not token:
        raise FetchError(
            f"{cfg['name']}: no Comeet token. Run "
            f"--probe <their careers page URL> to read it out of the page."
        )
    q = urllib.parse.urlencode({"token": token, "details": "true"})
    url = f"{COMEET_BASE}/company/{uid}/positions?{q}"
    return _parse_comeet_rows(http_json(url), cfg["name"])


# ---- Teamtailor -------------------------------------------------------
# No public API without a key, but the career site is server-rendered, so
# {base}/jobs is a plain fetch. Markup confirmed against careers.getnexar.com
# on 2026-08-23:
#   <a href="https://careers.getnexar.com/jobs/8247234-senior-ai-researcher">
#     <span class="absolute inset-0"></span> Senior AI Researcher </a>
#   <div class="mt-1 text-md">
#     <span>R&D</span> <span>&middot;</span> <span>Tel Aviv-Yafo (IL)</span>
#     <span>&middot;</span> <span>Hybrid</span>
#   </div>
# The utility class names are Tailwind and could change; the /jobs/{id}-{slug}
# URL shape is the stable part, so parsing hangs off that.
TEAMTAILOR_ANCHOR = re.compile(
    r'<a[^>]*href="((?:https?://[^"]+)?/jobs/\d+[^"#?]*)"[^>]*>(.*?)</a>',
    re.I | re.S)
TEAMTAILOR_META = re.compile(
    r'<div[^>]*class="[^"]*text-md[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
TEAMTAILOR_SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.I | re.S)
# "Tel Aviv-Yafo (IL)" — the country code in brackets marks the location field
# wherever it sits, so the department is not mistaken for it.
TEAMTAILOR_COUNTRY = re.compile(r"\(([A-Z]{2})\)\s*$")


def _parse_teamtailor_page(body: str, company: str, base: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    matches = list(TEAMTAILOR_ANCHOR.finditer(body))
    for i, m in enumerate(matches):
        href = html.unescape(m.group(1)).split("?")[0].rstrip("/")
        url = href if href.startswith("http") else base + href
        if url in seen:
            continue
        seen.add(url)
        title = strip_html(m.group(2))
        if not title:
            slug = href.rsplit("/", 1)[-1]
            title = re.sub(r"^\d+-", "", slug).replace("-", " ").title()

        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[m.end():min(end, m.end() + 1200)]
        parts: list[str] = []
        meta = TEAMTAILOR_META.search(chunk)
        if meta:
            for sp in TEAMTAILOR_SPAN.finditer(meta.group(1)):
                txt = strip_html(sp.group(1))
                if txt and txt not in {"·", "•"}:
                    parts.append(txt)

        loc = dept = etype = ""
        idx = next((k for k, v in enumerate(parts)
                    if TEAMTAILOR_COUNTRY.search(v)), None)
        if idx is None:
            # No country code: assume department, location, workplace type.
            dept = parts[0] if len(parts) > 1 else ""
            loc = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
            etype = parts[2] if len(parts) > 2 else ""
        else:
            loc = parts[idx]
            dept = ", ".join(parts[:idx])
            etype = ", ".join(parts[idx + 1:])
        out.append(Job(source="teamtailor", company=company, title=title,
                       url=url, location=loc, department=dept,
                       employment_type=etype))
    return out


def fetch_teamtailor(cfg: dict) -> list[Job]:
    base = cfg["base"].rstrip("/")
    warm_up(base)
    out: list[Job] = []
    seen: set[str] = set()
    for page in range(1, int(cfg.get("max_pages", 10)) + 1):
        url = f"{base}/jobs" if page == 1 else f"{base}/jobs?page={page}"
        try:
            body = http_text(url)
        except FetchError as e:
            if page == 1:
                raise FetchError(f"{e} (page 1 of {cfg['name']})") from e
            break
        fresh = [j for j in _parse_teamtailor_page(body, cfg["name"], base)
                 if j.url not in seen]
        if not fresh:
            break
        for j in fresh:
            seen.add(j.url)
        out.extend(fresh)
    return out


# ---- Comeet, embedded and server-rendered ------------------------------
# The Comeet WordPress plugin renders the board into the company's own page,
# so the jobs are in the HTML and no token is needed at all. Confirmed against
# arberobotics.com and imagry.co on 2026-08-23:
#   <div class="comeet-g-r Engineering">
#     <div class="comeet-list comeet-group-name">Engineering</div>
#     <ul class="comeet-positions-list"><li>
#       <a class="comeet-position" href="//site/career/co/.../33.D67/title/all">
#         <div class="comeet-position-name">Customer Support Manager</div>
#         <div class="comeet-position-meta">Tel Aviv &middot; Full-time</div>
#
# Companies group either by department or by location, and nothing in the
# markup says which. Arbe groups by department and puts the city in the meta
# line; Imagry groups by location and puts the department there instead. So
# rather than guess and risk filing a department as a location, every field
# that is not an employment type or a seniority level goes into location. The
# Israel filter only needs to see the place, and a slightly noisy location
# beats a confidently wrong one.
COMEET_EMBED_ANCHOR = re.compile(r"<a\b([^>]*comeet-position[^>]*)>(.*?)</a>",
                                 re.I | re.S)
COMEET_EMBED_HREF = re.compile(r'href="([^"]+)"', re.I)
COMEET_EMBED_NAME = re.compile(
    r'class="[^"]*comeet-position-name[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
COMEET_EMBED_META = re.compile(
    r'class="[^"]*comeet-position-meta[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
COMEET_GROUP_SPLIT = re.compile(r'<div[^>]*class="[^"]*comeet-g-r[^"]*"', re.I)
COMEET_GROUP_NAME = re.compile(
    r'class="[^"]*comeet-group-name[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
COMEET_EMP_RE = re.compile(
    r"^(full[- ]?time|part[- ]?time|contract(or)?|temporary|temp|"
    r"intern(ship)?|student|freelance|hourly|permanent|scholarship)$", re.I)
COMEET_LEVEL_RE = re.compile(
    r"^(senior|junior|entry[- ]level|mid[- ]level|experienced|associate|"
    r"principal|lead|management|director|executive|c[- ]level)$", re.I)
COMEET_SEP = re.compile(r"\s*(?:·|&middot;|\|)\s*")


def _comeet_embed_positions(chunk: str, group: str, company: str,
                            base: str) -> list[Job]:
    out: list[Job] = []
    for m in COMEET_EMBED_ANCHOR.finditer(chunk):
        attrs, inner = m.group(1), m.group(2)
        h = COMEET_EMBED_HREF.search(attrs)
        if not h:
            continue
        href = html.unescape(h.group(1)).strip()
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = base + href
        # The plugin leaves a commented-out copy of the title inside the name
        # div. Stripping tags without removing comments first duplicates it.
        inner = re.sub(r"<!--.*?-->", "", inner, flags=re.S)
        n = COMEET_EMBED_NAME.search(inner)
        title = strip_html(n.group(1)) if n else strip_html(inner)
        if not title:
            continue
        parts = [group] if group else []
        mm = COMEET_EMBED_META.search(inner)
        if mm:
            parts += [p for p in COMEET_SEP.split(strip_html(mm.group(1))) if p]

        etype = level = ""
        rest: list[str] = []
        for p in parts:
            if not etype and COMEET_EMP_RE.match(p):
                etype = p
            elif not level and COMEET_LEVEL_RE.match(p):
                level = p
            else:
                rest.append(p)
        out.append(Job(source="comeet_embed", company=company, title=title,
                       url=href, location=", ".join(rest),
                       employment_type=etype, experience_level=level))
    return out


def _parse_comeet_embed_page(body: str, company: str, base: str) -> list[Job]:
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    groups = COMEET_GROUP_SPLIT.split(body)
    out: list[Job] = []
    seen: set[str] = set()
    # groups[0] is everything before the first group and holds no positions.
    for chunk in (groups[1:] or [body]):
        g = COMEET_GROUP_NAME.search(chunk)
        group = strip_html(g.group(1)) if g else ""
        for j in _comeet_embed_positions(chunk, group, company, base):
            if j.url in seen:
                continue
            seen.add(j.url)
            out.append(j)
    return out


def fetch_comeet_embed(cfg: dict) -> list[Job]:
    url = cfg["url"]
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    warm_up(base)
    return _parse_comeet_embed_page(http_text(url), cfg["name"], base)


# ---- Trigo -------------------------------------------------------------
# Bespoke WordPress theme, not the Comeet plugin, though the links carry
# Comeet position UIDs (?uid=92.A5A). Everything is server-rendered on one
# page, same as Mobileye.
#   <div class="item ..."><div class="title"><a href="...?uid=">Data Engineer</a>
#     <div class="emp-type">Full-time</div></div>
#   <div class="job-info"><div class="department">Operations IL</div>
#     <div class="location">Israel</div></div>
TRIGO_ITEM_SPLIT = re.compile(r'<div[^>]*class="[^"]*\bitem\b[^"]*"', re.I)
TRIGO_LINK = re.compile(r'<a[^>]*href="([^"]*\?uid=[^"]+)"[^>]*>(.*?)</a>',
                        re.I | re.S)
TRIGO_FIELD = {
    "employment_type": re.compile(
        r'class="[^"]*emp-type[^"]*"[^>]*>(.*?)</div>', re.I | re.S),
    "department": re.compile(
        r'class="[^"]*department[^"]*"[^>]*>(.*?)</div>', re.I | re.S),
    "location": re.compile(
        r'class="[^"]*location[^"]*"[^>]*>(.*?)</div>', re.I | re.S),
}


def _parse_trigo_page(body: str, company: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    for chunk in TRIGO_ITEM_SPLIT.split(body)[1:]:
        m = TRIGO_LINK.search(chunk)
        if not m:
            continue
        url = html.unescape(m.group(1))
        title = strip_html(m.group(2))
        if not title or url in seen:
            continue
        seen.add(url)
        vals = {}
        for key, pat in TRIGO_FIELD.items():
            f = pat.search(chunk)
            vals[key] = strip_html(f.group(1)) if f else ""
        out.append(Job(source="trigo", company=company, title=title, url=url,
                       **vals))
    return out


def fetch_trigo(cfg: dict) -> list[Job]:
    url = cfg.get("url", "https://www.trigoretail.com/careers/")
    warm_up(url)
    return _parse_trigo_page(http_text(url), cfg["name"])


# ---- Hailo -------------------------------------------------------------
# Custom theme backed by Comeet data — the position UID is in the job URL and
# the filter buttons carry Comeet location UIDs — but none of the plugin's
# markup, which is why the signature scan came up empty on it twice.
# Everything is server-rendered on one page; the pagination is client-side,
# so a single fetch returns the whole board.
#   <a class="b-careers-lobby__job" href="..." data-location="7E.109">
#     <h3 class="b-careers-lobby__job-name">Title</h3>
#     <div class="b-careers-lobby__job-department">Other</div>
#     <div class="b-careers-lobby__job-location"><div class="...-icon"></div>
#       Tel Aviv-Yafo, IL</div>
HAILO_URL = "https://hailo.ai/company-overview/careers/"
HAILO_ANCHOR = re.compile(
    r'<a[^>]*class="[^"]*b-careers-lobby__job"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.I | re.S)
HAILO_NAME = re.compile(r"<h3[^>]*job-name[^>]*>(.*?)</h3>", re.I | re.S)
HAILO_DEPT = re.compile(
    r'class="[^"]*job-department[^"]*"[^>]*>(.*?)</div>', re.I | re.S)
# The location div wraps an icon div, so a lazy match to the first </div>
# would return the icon and nothing else. Read to the arrow div instead.
HAILO_LOC = re.compile(
    r'class="[^"]*job-location"[^>]*>(.*?)(?=<div[^>]*job-arrow|\Z)',
    re.I | re.S)


def _parse_hailo_page(body: str, company: str) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    for m in HAILO_ANCHOR.finditer(body):
        url = html.unescape(m.group(1)).strip()
        inner = m.group(2)
        if url in seen:
            continue
        seen.add(url)
        n = HAILO_NAME.search(inner)
        d = HAILO_DEPT.search(inner)
        l = HAILO_LOC.search(inner)
        title = strip_html(n.group(1)) if n else ""
        if not title:
            continue
        out.append(Job(source="hailo", company=company, title=title, url=url,
                       location=strip_html(l.group(1)) if l else "",
                       department=strip_html(d.group(1)) if d else ""))
    return out


def fetch_hailo(cfg: dict) -> list[Job]:
    url = cfg.get("url", HAILO_URL)
    warm_up(url)
    return _parse_hailo_page(http_text(url), cfg["name"])


# ---- Magnus Metal ------------------------------------------------------
# Elementor page, no ATS behind it. Each job is a run of heading widgets —
# "Job#2418", the title, a one-paragraph description — closed by an Apply
# button linking to /careers/{slug}-job{id}/. Everything is on one page.
# The cards carry no location at all; the company has a single site, so the
# location comes from `location:` in sources.yaml.
MAGNUS_URL = "https://www.magnusmetal.com/careers/"
MAGNUS_LOCATION = "Kibbutz Revadim, Israel"
MAGNUS_JOB_SPLIT = re.compile(r"<h\d[^>]*>\s*Job\s*#\s*(\d+)\s*</h\d>", re.I)
MAGNUS_HEADING = re.compile(r"<h\d[^>]*>(.*?)</h\d>", re.I | re.S)
MAGNUS_LINK = re.compile(r'href="([^"]*/careers/[^"]*job\d+[^"]*)"', re.I)


def _parse_magnus_page(body: str, company: str,
                       location: str = MAGNUS_LOCATION) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    # split() with one capture group yields [before, id, chunk, id, chunk, ...]
    parts = MAGNUS_JOB_SPLIT.split(body)
    for job_id, chunk in zip(parts[1::2], parts[2::2]):
        link = MAGNUS_LINK.search(chunk)
        if not link:
            continue
        url = html.unescape(link.group(1))
        # Headings after the button belong to the page footer, not this job.
        heads = [strip_html(h) for h in
                 MAGNUS_HEADING.findall(chunk[:link.start()])]
        heads = [h for h in heads if h]
        if not heads or url in seen:
            continue
        seen.add(url)
        out.append(Job(source="magnus", company=company, title=heads[0],
                       url=url, location=location,
                       department=f"Job#{job_id}",
                       description=" ".join(heads[1:])[:4000]))
    return out


def fetch_magnus(cfg: dict) -> list[Job]:
    url = cfg.get("url", MAGNUS_URL)
    warm_up(url)
    return _parse_magnus_page(http_text(url), cfg["name"],
                              cfg.get("location", MAGNUS_LOCATION))


CONNECTORS = {
    "rafael": fetch_rafael,
    "magnus": fetch_magnus,
    "elbit": fetch_elbit,
    "greenhouse": fetch_greenhouse,
    "comeet": fetch_comeet,
    "comeet_embed": fetch_comeet_embed,
    "trigo": fetch_trigo,
    "hailo": fetch_hailo,
    "teamtailor": fetch_teamtailor,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "radancy": fetch_radancy,
    "workday": fetch_workday,
    "eightfold_pcsx": fetch_eightfold_pcsx,
    "eightfold_v2": fetch_eightfold_v2,
    "google_careers": fetch_google,
    "apple": fetch_apple,
    "mobileye": fetch_mobileye,
}


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

# "5 years of experience", "5+ years experience", "at least 3 years",
# "שנתיים ניסיון", "3 שנות ניסיון"
YEARS_PATTERNS = [
    re.compile(r"(\d{1,2})\s*\+?\s*years?\b[^.]{0,40}?\bexperience", re.I),
    re.compile(r"experience[^.]{0,30}?(\d{1,2})\s*\+?\s*years?\b", re.I),
    re.compile(r"(\d{1,2})\s*\+?\s*שנ(?:ות|ים|ה)\s*(?:של\s*)?(?:ניסיון|נסיון)"),
]
HEB_WORD_YEARS = {"שנה": 1, "שנתיים": 2, "שלוש שנים": 3, "ארבע שנים": 4, "חמש שנים": 5}


def extract_min_years(*texts: str) -> int | None:
    """
    Lowest number of years mentioned near the word 'experience'.

    Minimum-qualification lists usually state several bars ("2 years of X,
    5 years of Y"); the lowest is the real entry point. Returns None when the
    source gave us nothing to read — that is different from 0 and must stay
    distinguishable, otherwise every Workday row would look entry-level.
    """
    found: list[int] = []
    for t in texts:
        if not t:
            continue
        chunk = t[:3000]
        for pat in YEARS_PATTERNS:
            for m in pat.finditer(chunk):
                try:
                    n = int(m.group(1))
                except (ValueError, IndexError):
                    continue
                if 0 <= n <= 30:
                    found.append(n)
        low = chunk.lower()
        for phrase, n in HEB_WORD_YEARS.items():
            if phrase in low and ("ניסיון" in low or "נסיון" in low):
                found.append(n)
    return min(found) if found else None


class Classifier:
    def __init__(self, filters: dict):
        self.israel = [t.lower() for t in filters.get("israel_terms", [])]
        self.student = [t.lower() for t in filters.get("student_terms", [])]
        self.exclude = [t.lower() for t in filters.get("student_exclude", [])]
        self.part_time = [t.lower() for t in filters.get("part_time_terms", [])]
        self.senior_prefixes = [t.lower() for t in
                                filters.get("senior_title_prefixes", [])]

    def in_israel(self, job: Job) -> bool:
        hay = f"{job.location} {job.title}".lower()
        return any(t in hay for t in self.israel)

    def is_student(self, job: Job) -> bool:
        title = job.title.lower()
        # A senior role is never a student role, whatever else it says.
        if any(x in title for x in self.exclude):
            return False
        if any(t in title for t in self.student):
            return True
        meta = f"{job.employment_type} {job.experience_level}".lower()
        if any(t in meta for t in self.student):
            return True
        # Description is noisy — require a strong signal only.
        desc = job.description.lower()[:1500]
        return any(t in desc for t in ("student position", "internship program",
                                       "משרת סטודנט", "למשרת סטודנט"))

    def is_part_time(self, job: Job) -> bool:
        hay = f"{job.title} {job.employment_type}".lower()
        if any(t in hay for t in self.part_time):
            return True
        # Student roles in Israel are part-time by default even when unlabelled.
        return job.is_student and "intern" not in job.title.lower()

    def is_senior(self, job: Job) -> bool:
        """
        True when the title *starts with* a seniority word.

        Prefix-only on purpose. "Senior Engineer" is senior; "Intern, Senior
        Design Team" is not — matching anywhere in the string would wrongly
        drop the second one.
        """
        t = re.sub(r"^[\s\-–—•]+", "", job.title.lower())
        return any(
            t == p or t.startswith(p + " ") or t.startswith(p + ",")
            or t.startswith(p + "-") or t.startswith(p + ".")
            for p in self.senior_prefixes
        )

    def apply(self, jobs: Iterable[Job]) -> list[Job]:
        out = []
        for j in jobs:
            j.in_israel = self.in_israel(j)
            j.is_student = self.is_student(j)
            j.is_senior = self.is_senior(j)
            j.is_part_time = self.is_part_time(j)
            j.min_years = extract_min_years(j.qualifications, j.description)
            j.posted_date = normalize_date(j.posted_at)
            out.append(j)
        return out


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    hash            TEXT PRIMARY KEY,
    source          TEXT, company TEXT, title TEXT, url TEXT,
    location        TEXT, employment_type TEXT, experience_level TEXT,
    department      TEXT, description TEXT, posted_at TEXT,
    qualifications  TEXT,
    is_student      INTEGER, in_israel INTEGER, is_senior INTEGER,
    is_part_time    INTEGER, min_years INTEGER, posted_date TEXT,
    first_seen      TEXT, last_seen TEXT, is_open INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_open    ON jobs(is_open);
CREATE INDEX IF NOT EXISTS idx_student ON jobs(is_student, in_israel);

-- When each company was first scraped. Everything that arrived in that first
-- run is the pre-existing backlog: those jobs were posted at some unknown time
-- before we ever looked, so their first_seen says when WE found them, not when
-- the company posted them. Treating that as a publish date would make an
-- entire board look like it went up today. Anything first seen after that run
-- genuinely appeared while we were watching, and its first_seen is a real
-- lower bound on the publish date.
CREATE TABLE IF NOT EXISTS company_meta (
    company   TEXT PRIMARY KEY,
    first_run TEXT NOT NULL
);
"""

# The date the filters and the UI should use. Published date wins. Failing
# that, first_seen counts only for jobs that appeared after we started
# tracking that company. Otherwise NULL — genuinely unknown, and kept by
# default like any other undated row.
EFFECTIVE_DATE = """(CASE
    WHEN jobs.posted_date IS NOT NULL AND jobs.posted_date <> ''
        THEN jobs.posted_date
    WHEN jobs.first_seen >
         (SELECT first_run FROM company_meta WHERE company = jobs.company)
        THEN DATE(jobs.first_seen)
END)"""

# Which of the two produced it, so the UI can say so instead of implying the
# company published a date it never published.
DATE_SOURCE = """(CASE
    WHEN jobs.posted_date IS NOT NULL AND jobs.posted_date <> '' THEN 'published'
    WHEN jobs.first_seen >
         (SELECT first_run FROM company_meta WHERE company = jobs.company)
        THEN 'first_seen'
    ELSE ''
END)"""

# "New" must mean new to us, not "arrived in the first dump of a new company".
IS_TRULY_NEW = """(jobs.first_seen >
    (SELECT first_run FROM company_meta WHERE company = jobs.company))"""

# Columns added after the first release. Existing jobs.db files get migrated
# in place rather than forcing users to delete their history.
MIGRATIONS = [
    ("qualifications", "TEXT"),
    ("is_part_time", "INTEGER"),
    ("min_years", "INTEGER"),
    ("is_senior", "INTEGER"),
    ("posted_date", "TEXT"),
]


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    added = []
    for col, coltype in MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}")
            added.append(col)
    conn.commit()
    if "posted_date" in added or _needs_date_backfill(conn):
        backfill_posted_dates(conn)
    backfill_company_meta(conn)
    return conn


def backfill_company_meta(conn: sqlite3.Connection) -> int:
    """
    Give existing databases a first_run per company, taken from the earliest
    first_seen we have for it. That is the best available answer, and it errs
    the safe way: the opening backlog stays undated rather than being dated the
    day the scraper happened to be installed.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO company_meta (company, first_run) "
        "SELECT company, MIN(first_seen) FROM jobs "
        "WHERE company IS NOT NULL AND first_seen IS NOT NULL AND first_seen<>'' "
        "GROUP BY company")
    conn.commit()
    return cur.rowcount or 0


def _needs_date_backfill(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE (posted_date IS NULL OR posted_date='') "
        "AND posted_at IS NOT NULL AND posted_at<>''"
    ).fetchone()
    return bool(row and row[0])


def backfill_posted_dates(conn: sqlite3.Connection) -> int:
    """
    Populate posted_date for rows stored before the column existed.

    Without this, an old row keeps posted_date NULL while still showing a date
    in the UI (which fell back to the raw posted_at). The date filter then
    treats it as undated and lets it through — so the table displays March
    dates under a "last week" filter. Re-scraping only fixes the sources you
    happen to refresh, so the repair belongs here.
    """
    rows = conn.execute(
        "SELECT hash, posted_at FROM jobs "
        "WHERE (posted_date IS NULL OR posted_date='') "
        "AND posted_at IS NOT NULL AND posted_at<>''"
    ).fetchall()
    fixed = 0
    for h, raw in rows:
        iso = normalize_date(raw)
        if iso:
            conn.execute("UPDATE jobs SET posted_date=? WHERE hash=?", (iso, h))
            fixed += 1
    conn.commit()
    return fixed


def save(conn: sqlite3.Connection, jobs: list[Job],
         healthy_companies: set[str]) -> tuple[int, int]:
    """Upsert jobs; close anything from a healthy source that vanished."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Stamp the first run per company BEFORE inserting, and only for companies
    # that actually returned rows. INSERT OR IGNORE keeps the original stamp on
    # every later run. Every row inserted in this run shares `now` exactly, so
    # "first_seen > first_run" cleanly excludes the opening backlog.
    for company in sorted({j.company for j in jobs}):
        conn.execute(
            "INSERT OR IGNORE INTO company_meta (company, first_run) VALUES (?,?)",
            (company, now))
    new_count = 0
    for j in jobs:
        row = conn.execute(
            "SELECT hash FROM jobs WHERE hash=?", (j.hash,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE jobs SET last_seen=?, is_open=1, url=?, posted_at=?,"
                " is_part_time=?, min_years=?, is_senior=?, posted_date=? WHERE hash=?",
                (now, j.url, j.posted_at, int(j.is_part_time), j.min_years,
                 int(j.is_senior), j.posted_date, j.hash)
            )
        else:
            new_count += 1
            d = asdict(j)
            conn.execute(
                "INSERT INTO jobs (hash, source, company, title, url, location,"
                " employment_type, experience_level, department, description,"
                " posted_at, qualifications, is_student, in_israel, is_senior,"
                " is_part_time, min_years, posted_date, first_seen, last_seen, is_open)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (j.hash, d["source"], d["company"], d["title"], d["url"],
                 d["location"], d["employment_type"], d["experience_level"],
                 d["department"], d["description"], d["posted_at"],
                 d["qualifications"], int(j.is_student), int(j.in_israel),
                 int(j.is_senior), int(j.is_part_time), j.min_years,
                 j.posted_date, now, now),
            )
    # Only expire companies we actually reached this run. A failed fetch must
    # never look like "all jobs closed" — that is how stale data creeps in.
    closed = 0
    if healthy_companies:
        placeholders = ",".join("?" * len(healthy_companies))
        cur = conn.execute(
            f"UPDATE jobs SET is_open=0 WHERE is_open=1 AND last_seen<? "
            f"AND company IN ({placeholders})",
            (now, *sorted(healthy_companies)),
        )
        closed = cur.rowcount
    conn.commit()
    return new_count, closed


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

FIELDS = ["company", "title", "location", "posted_at", "is_student",
          "is_part_time", "min_years", "is_senior", "posted_date",
          "source", "url", "first_seen"]


def build_where(students_only: bool = False, part_time_only: bool = False,
                max_years: int | None = None, strict_years: bool = False,
                hide_senior: bool = False,
                max_age_days: int | None = None,
                strict_dates: bool = False) -> str:
    """Shared filter clause so CSV, HTML and counts never drift apart."""
    parts = ["is_open=1", "in_israel=1"]
    if students_only:
        parts.append("is_student=1")
    if part_time_only:
        parts.append("is_part_time=1")
    if hide_senior:
        parts.append("COALESCE(is_senior,0)=0")
    if max_age_days is not None:
        # Same reasoning as max_years: a job with no published date must not be
        # silently discarded, because several sources publish none at all.
        cutoff = f"DATE('now','-{int(max_age_days)} day')"
        eff = EFFECTIVE_DATE
        parts.append(
            f"{eff} >= {cutoff}" if strict_dates
            else f"({eff} IS NULL OR {eff} >= {cutoff})"
        )
    if max_years is not None:
        # Unknown (NULL) is kept by default. Most sources give no years signal
        # at all, so excluding NULL would throw away nearly everything.
        parts.append(
            f"min_years<={max_years}" if strict_years
            else f"(min_years IS NULL OR min_years<={max_years})"
        )
    return " AND ".join(parts)


def write_csv(conn: sqlite3.Connection, where: str) -> int:
    cols = FIELDS + ["effective_date", "date_source"]
    rows = conn.execute(
        f"SELECT {','.join(FIELDS)}, {EFFECTIVE_DATE}, {DATE_SOURCE} "
        f"FROM jobs WHERE {where} "
        f"ORDER BY {EFFECTIVE_DATE} IS NULL, {EFFECTIVE_DATE} DESC, "
        "is_student DESC, min_years IS NULL, min_years, company"
    ).fetchall()
    with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    return len(rows)


def write_html(conn: sqlite3.Connection, health: list[tuple[str, str, int]],
               where: str) -> None:
    rows = conn.execute(
        "SELECT company, title, location, posted_at, is_student, url, first_seen,"
        f" is_part_time, min_years, {EFFECTIVE_DATE}, {DATE_SOURCE}, {IS_TRULY_NEW}"
        " FROM jobs WHERE " + where +
        " ORDER BY is_student DESC, min_years IS NULL, min_years, company, title"
    ).fetchall()
    today = datetime.now(timezone.utc).date().isoformat()
    students = [r for r in rows if r[4]]

    def esc(x: Any) -> str:
        return html.escape(str(x or ""))

    body = []
    for (c, t, loc, posted, stu, url, first, pt, yrs, pdate, dsrc,
         tracked) in rows:
        is_new = bool(tracked) and (first or "")[:10] == today
        cls = " ".join(filter(None, ["student" if stu else "", "new" if is_new else ""]))
        yrs_txt = "—" if yrs is None else f"{yrs}y"
        # A first_seen date is when we noticed it, not when they posted it.
        # Marking it keeps the column honest.
        date_txt = (f'<span title="first seen by the scraper on this date; '
                    f'this source publishes no date">~{esc(pdate)}</span>'
                    if dsrc == "first_seen" else esc(pdate)) if pdate else "&mdash;"
        body.append(
            f'<tr class="{cls}"><td>{esc(c)}</td>'
            f'<td><a href="{esc(url)}" target="_blank">{esc(t)}</a>'
            f'{" <span class=badge>NEW</span>" if is_new else ""}</td>'
            f"<td>{esc(loc)}</td><td>{date_txt}</td>"
            f'<td>{"★" if stu else ""}</td>'
            f'<td>{"½" if pt else ""}</td>'
            f'<td class="yrs">{yrs_txt}</td></tr>'
        )

    health_rows = "".join(
        f'<tr><td>{esc(n)}</td><td class="{"ok" if s=="ok" else "bad"}">{esc(s)}</td>'
        f"<td>{c}</td></tr>"
        for n, s, c in health
    )

    HTML_PATH.write_text(f"""<!doctype html><meta charset="utf-8">
<title>Israel student jobs</title>
<style>
 body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 th,td{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}}
 th{{background:#f4f4f5;position:sticky;top:0}}
 tr.student{{background:#f0fdf4}}
 .badge{{background:#2563eb;color:#fff;font-size:10px;padding:1px 5px;border-radius:3px}}
 .ok{{color:#15803d}} .bad{{color:#b91c1c;font-weight:600}}
 h2{{margin-top:2rem}} .meta{{color:#666;font-size:13px}}
 .yrs{{text-align:right;font-variant-numeric:tabular-nums;color:#555}}
 .legend{{color:#666;font-size:12px;margin:.5rem 0}}
</style>
<h1>Israel jobs</h1>
<p class="meta">{len(rows)} open jobs in Israel &middot; {len(students)} student roles
&middot; generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>
<p class="legend">★ student/entry-level &middot; ½ part-time &middot;
&ldquo;~date&rdquo; = first seen by the scraper on that date, because the source
publishes none &middot;
Yrs = minimum years of experience found in the posting; &ldquo;—&rdquo; means the
source published no requirements text, <em>not</em> that none are required.</p>
<h2>Source health</h2>
<table><tr><th>Source</th><th>Status</th><th>Jobs fetched</th></tr>{health_rows}</table>
<h2>Jobs</h2>
<table><tr><th>Company</th><th>Title</th><th>Location</th><th>Posted</th>
<th>Student</th><th>PT</th><th>Yrs</th></tr>
{''.join(body)}</table>
""", encoding="utf-8")


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def run(students_only: bool = False, only_source: str | None = None,
        part_time_only: bool = False, max_years: int | None = None,
        strict_years: bool = False, hide_senior: bool = False,
        max_age_days: int | None = None, strict_dates: bool = False) -> int:
    cfg = load_config()
    classifier = Classifier(cfg.get("filters", {}))
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if only_source:
        sources = [s for s in sources if s["name"].lower() == only_source.lower()]
        if not sources:
            print(f"No enabled source named {only_source!r}", file=sys.stderr)
            return 2

    all_jobs: list[Job] = []
    health: list[tuple[str, str, int]] = []
    healthy: set[str] = set()

    for s in sources:
        fn = CONNECTORS.get(s["type"])
        if not fn:
            health.append((s["name"], f"unknown type {s['type']}", 0))
            continue
        print(f"  fetching {s['name']} ({s['type']}) ...", end="", flush=True)
        try:
            jobs = fn(s)
        except FetchError as e:
            print(f" FAILED: {e}")
            health.append((s["name"], f"failed: {e}", 0))
            continue
        except Exception as e:  # noqa: BLE001 - a broken connector must not kill the run
            print(f" ERROR: {type(e).__name__}: {e}")
            health.append((s["name"], f"error: {type(e).__name__}", 0))
            continue

        # Zero jobs from a source that returned 200 is suspicious, not success.
        status = "ok" if jobs else "returned 0 jobs - check connector"
        health.append((s["name"], status, len(jobs)))
        if jobs:
            healthy.add(s["name"])
        all_jobs.extend(jobs)
        print(f" {len(jobs)} jobs")

    all_jobs = classifier.apply(all_jobs)

    # Deduplicate within this run before touching the database.
    unique: dict[str, Job] = {}
    for j in all_jobs:
        unique.setdefault(j.hash, j)
    deduped = list(unique.values())

    conn = open_db()
    new_count, closed = save(conn, deduped, healthy)
    where = build_where(students_only, part_time_only, max_years,
                        strict_years, hide_senior, max_age_days, strict_dates)
    n_csv = write_csv(conn, where)
    write_html(conn, health, where)
    conn.close()

    israel = [j for j in deduped if j.in_israel]
    student = [j for j in israel if j.is_student]
    part_time = [j for j in israel if j.is_part_time]
    with_years = [j for j in israel if j.min_years is not None]
    juniorish = [j for j in with_years if j.min_years <= 2]

    print()
    print(f"  fetched         {len(all_jobs)}  ({len(deduped)} after dedup)")
    print(f"  in Israel       {len(israel)}")
    print(f"  student         {len(student)}")
    print(f"  part-time       {len(part_time)}")
    print(f"  senior (hidden by --no-senior) {sum(1 for j in israel if j.is_senior)}")
    dated = [j for j in israel if j.posted_date]
    print(f"  dates known     {len(dated)}  (newest: {max((j.posted_date for j in dated), default='-')})")
    print(f"  years known     {len(with_years)}  (of which <=2 yrs: {len(juniorish)})")
    print(f"  new this run    {new_count}")
    print(f"  closed          {closed}")
    print()
    for name, status, count in health:
        mark = "OK " if status == "ok" else "!! "
        print(f"  {mark}{name:<14} {count:>4}  {'' if status=='ok' else status}")
    print()
    print(f"  wrote {CSV_PATH.name} ({n_csv} rows), {HTML_PATH.name}, {DB_PATH.name}")

    failures = [h for h in health if h[1] != "ok"]
    return 1 if failures else 0


# --------------------------------------------------------------------------
# Offline self-test — exercises parsing/filtering with no network
# --------------------------------------------------------------------------

def selftest() -> int:
    ok = True

    def check(label: str, cond: bool) -> None:
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        ok = ok and bool(cond)

    filters = {
        "israel_terms": ["israel", "tel aviv", "haifa", "kiryat gat"],
        "student_terms": ["student", "intern", "junior", "משרת סטודנט"],
        "student_exclude": ["senior", "manager", "director"],
    }
    c = Classifier(filters)

    j1 = Job("workday", "Intel", "Engineering student for Intel Kiryat Gat",
             "http://x", location="Israel, Kiryat-Gat")
    j2 = Job("workday", "NVIDIA", "Senior Intern Program Manager",
             "http://x", location="Israel, Tel Aviv")
    j3 = Job("greenhouse", "Wiz", "Software Engineer", "http://x",
             location="New York, USA")
    j4 = Job("greenhouse", "Taboola", "Backend Engineer, Intern", "http://x",
             location="Tel Aviv")

    check("Israel detected from location", c.in_israel(j1))
    check("non-Israel rejected", not c.in_israel(j3))
    check("student title detected", c.is_student(j1))
    check("'Senior ... Intern Manager' excluded", not c.is_student(j2))
    check("intern in title detected", c.is_student(j4))

    # dedup: same role, different URL, must collapse
    a = Job("greenhouse", "Wiz", "Backend Engineer", "http://a", location="Tel Aviv")
    b = Job("greenhouse", "Wiz", "Backend  engineer", "http://b", location="tel aviv")
    check("dedup collapses case/spacing variants", a.hash == b.hash)

    d = Job("greenhouse", "Wiz", "Frontend Engineer", "http://a", location="Tel Aviv")
    check("different titles do not collide", a.hash != d.hash)

    check("html stripped", strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world")

    # --- years of experience ---
    check("years: '8 years of experience'",
          extract_min_years("8 years of experience in design") == 8)
    check("years: takes the lowest bar",
          extract_min_years("2 years of experience with X. "
                            "5 years of experience with Y.") == 2)
    check("years: '5+ years'",
          extract_min_years("5+ years experience required") == 5)
    check("years: Hebrew",
          extract_min_years("דרוש 3 שנות ניסיון בפיתוח") == 3)
    check("years: unknown stays None", extract_min_years("") is None)
    check("years: no false positive on '2026'",
          extract_min_years("Starting in 2026") is None)

    # --- part time ---
    cpt = Classifier({**filters, "part_time_terms": ["part-time", "part time",
                                                     "משרה חלקית"]})
    p1 = Job("x", "Y", "Data Analyst (Part-Time)", "u", location="Tel Aviv")
    p1.is_student = cpt.is_student(p1)
    check("part-time detected in title", cpt.is_part_time(p1))
    p2 = Job("x", "Y", "Backend Engineer", "u", location="Tel Aviv")
    p2.is_student = cpt.is_student(p2)
    check("full-time not flagged", not cpt.is_part_time(p2))

    # --- google parser ---
    # Google ships NO anchors in server HTML; the URL is rebuilt from the
    # numeric job id + slugified title. Fixture mirrors the real markup.
    google_html = (
        '<ul><li class="lLd3Je xyz"><h3 class="mUzaXb">Student Researcher</h3>'
        '<div jsdata="x;113531416431469254;y"></div>'
        '<span>place Tel Aviv, Israel ; +2 more</span><span>bar_chart Early</span>'
        'Minimum qualifications Currently pursuing a degree. '
        '1 year of experience with Python. Learn more</li>'
        '<li class="lLd3Je"><h3>Staff Engineer, Cloud &amp; AI</h3>'
        '<div jsdata="x;113531416431469255;y"></div>'
        '<span>place Haifa, Israel</span>Minimum qualifications '
        '10 years of experience. Learn more</li></ul>'
    )
    g = _parse_google_page(google_html)
    check("google: 2 cards parsed", len(g) == 2)
    check("google: title from h3", g[0].title == "Student Researcher")
    check("google: url rebuilt from id + slug",
          g[0].url == GOOGLE_BASE + "jobs/results/113531416431469254-student-researcher")
    check("google: ampersand slugified",
          g[1].url.endswith("-staff-engineer-cloud-and-ai"))
    check("google: location parsed", g[0].location == "Tel Aviv, Israel")
    check("google: '+N more' stripped", "more" not in g[0].location)
    check("google: years from qualifications",
          extract_min_years(g[0].qualifications) == 1)
    check("google: senior years read", extract_min_years(g[1].qualifications) == 10)

    # --- apple parser ---
    # href carries a query string; the old [^"#?]+ class made this return 0.
    apple_html = (
        '<div class="job-title-link"><h3><a aria-label="Design Engineer 200572626" '
        'href="/en-us/details/200572626-0865/design-engineer?team=HRDWR" '
        'data-discover="true">Design Engineer</a></h3>'
        '<span class="team-name mt-0">Hardware</span>'
        '<span class="job-posted-date">Aug 06, 2026</span></div>'
        '<div><span>Location</span><span>Herzliya</span><span>Actions</span></div>'
    )
    ap = _parse_apple_page(apple_html)
    check("apple: 1 card", len(ap) == 1)
    check("apple: title", ap[0].title == "Design Engineer")
    check("apple: query string stripped from url",
          ap[0].url ==
          "https://jobs.apple.com/en-us/details/200572626-0865/design-engineer")
    check("apple: team parsed", ap[0].department == "Hardware")
    check("apple: posted date parsed", ap[0].posted_at == "Aug 06, 2026")
    check("apple: location", "Herzliya" in ap[0].location)

    # --- mobileye parser ---
    mob_html = (
        '<div class="jobItem toggleClose"><span>Algorithms</span>'
        '<span>3D Algorithm Developer</span><span>Ramat Gan</span>'
        '<span>Full time</span><p>Mobileye is looking for someone.</p>'
        '<a href="/jobs/3d-algorithm-developer/bb661a53-79b8-459d-a8df-5dd419d62596">go</a>'
        '</div>'
        '<div class="jobItem"><span>Algorithms</span><span>AI Algorithm Engineer</span>'
        '<span>Beijing</span><span>Full time</span><p>desc</p>'
        '<a href="/jobs/ai-algorithm-engineer/b042958e-6d74-42d6-afb3-0d9e2d046f66">go</a>'
        '</div>'
    )
    mb = _parse_mobileye_page(mob_html)
    check("mobileye: 2 cards", len(mb) == 2)
    check("mobileye: title", mb[0].title == "3D Algorithm Developer")
    check("mobileye: location", mb[0].location == "Ramat Gan")
    check("mobileye: department", mb[0].department == "Algorithms")
    check("mobileye: employment type", mb[0].employment_type == "Full time")
    mb_c = Classifier(filters)
    check("mobileye: Ramat Gan not matched by default terms",
          not mb_c.in_israel(mb[1]))   # Beijing must not pass

    # --- rafael / elbit parsers ---
    raf_cfg = {"name": "רפאל", "areas": {7: "מכון דוד - קריות", 4: "תרדיון"}}
    raf_rows = {"status": "ok", "data": [
        {"id": 13305, "tl": "מהנדס.ת מערכת בייצור", "ar": [7], "jt": [1], "pr": [12]},
        {"id": 14001, "tl": "סטודנט.ית להנדסת איכות", "ar": [7], "jt": [2], "pr": [3]},
        {"id": 14002, "tl": "סטודנט.ית למכונות", "ar": [4], "jt": [2], "pr": [3]},
    ]}
    raf = _parse_rafael_rows(raf_rows, raf_cfg)
    check("rafael: 3 jobs", len(raf) == 3)
    check("rafael: area id mapped to name", "מכון דוד" in raf[0].location)
    check("rafael: country appended for Israel filter", "ישראל" in raf[0].location)
    check("rafael: job type 2 flagged student",
          raf[1].employment_type == "משרת סטודנט")
    check("rafael: job type 1 not flagged", raf[0].employment_type == "")
    check("rafael: url built from id",
          raf[0].url == "https://career.rafael.co.il/job/13305")

    elb_rows = [
        {"jobId": 20939, "jobTitle": "סטודנט.ית אלגוריתמיקה", "jobCode": "7111",
         "openDate": "2026-08-13T01:16:00", "area": "Center", "status": 1,
         "employerName": "סיגמאביט", "description": "<p>תיאור</p>",
         "requirements": "2 years of experience"},
        {"jobId": 20845, "jobTitle": "משרה סגורה", "jobCode": "7006",
         "openDate": "2026-07-20T04:16:00", "area": "North", "status": 0,
         "employerName": "x", "description": "", "requirements": ""},
    ]
    elb = _parse_elbit_rows(elb_rows, {"name": "אלביט מערכות"})
    check("elbit: closed job dropped", len(elb) == 1)
    check("elbit: area translated", "מרכז" in elb[0].location)
    check("elbit: job code kept for lookup", "7111" in elb[0].department)
    check("elbit: posted date trimmed to ISO", elb[0].posted_at == "2026-08-13")
    check("elbit: html stripped from description", elb[0].description == "תיאור")
    check("elbit: years read from requirements",
          extract_min_years(elb[0].qualifications) == 2)

    ilc = Classifier(load_config()["filters"])
    check("rafael job classified as in Israel", ilc.in_israel(raf[1]))
    check("elbit job classified as in Israel", ilc.in_israel(elb[0]))
    check("rafael student title detected", ilc.is_student(raf[1]))
    check("elbit student title detected", ilc.is_student(elb[0]))

    # --- compressed responses (the Rafael failure) ---
    import gzip as _gz
    import zlib as _zl
    payload = '{"status":"ok","data":[{"id":1,"tl":"סטודנט.ית"}]}'
    raw = payload.encode("utf-8")
    check("decode: plain passthrough", decode_body(raw, None) == payload)
    check("decode: gzip via header",
          decode_body(_gz.compress(raw), "gzip") == payload)
    check("decode: gzip detected without header",
          decode_body(_gz.compress(raw), None) == payload)
    check("decode: deflate", decode_body(_zl.compress(raw), "deflate") == payload)
    check("decode: gzip json round-trips",
          json.loads(decode_body(_gz.compress(raw), "gzip"))["data"][0]["tl"]
          == "סטודנט.ית")

    # --- fuzzy title search ---
    def tm(q, t):
        return title_matches(q, t)

    check("search: case insensitive", tm("STUDENT", "Student Researcher"))
    check("search: words in any order",
          tm("student data", "Data Science Student, AI Group"))
    check("search: all words required",
          not tm("student manager", "Data Science Student"))
    check("search: partial word", tm("engin", "Backend Engineer"))
    check("search: typo within 90%", tm("enginer", "Backend Engineer"))
    check("search: unrelated word rejected", not tm("chef", "Backend Engineer"))
    check("search: empty query matches all", tm("", "anything"))
    check("search: extra spaces ignored", tm("  data   student ",
                                             "Student, Data Platform"))

    check("search: Hebrew basic", tm("סטודנט", "סטודנט.ית להנדסת איכות"))
    check("search: Hebrew inclusive form splits on dot",
          tm("ית", "סטודנט.ית להנדסת איכות"))
    check("search: Hebrew plural matches singular",
          tm("סטודנטים", "סטודנט להנדסה"))
    check("search: Hebrew final letter normalized",
          tm("תוכניתן", "תוכניתנ מערכות"))
    check("search: Hebrew two words any order",
          tm("איכות סטודנט", "סטודנט.ית להנדסת איכות"))
    check("search: Hebrew unrelated rejected",
          not tm("נהג", "סטודנט.ית להנדסת איכות"))

    check("search: threshold respected — 50% similar rejected at 0.9",
          not word_matches("abcd", ["abzz"], 0.9))
    check("search: 90% similar accepted",
          word_matches("engineerx", ["engineer"], 0.9))

    # --- legacy backfill (the "filter ignores real dates" bug) ---
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        legacy = Path(td) / "legacy.db"
        c = sqlite3.connect(legacy)
        c.executescript(
            "CREATE TABLE jobs (hash TEXT PRIMARY KEY, source TEXT, company TEXT,"
            " title TEXT, url TEXT, location TEXT, employment_type TEXT,"
            " experience_level TEXT, department TEXT, description TEXT,"
            " posted_at TEXT, is_student INTEGER, in_israel INTEGER,"
            " first_seen TEXT, last_seen TEXT, is_open INTEGER DEFAULT 1);")
        c.execute("INSERT INTO jobs (hash,posted_at,in_israel,is_open) "
                  "VALUES ('x','2026-03-23',1,1)")
        c.execute("INSERT INTO jobs (hash,posted_at,in_israel,is_open) "
                  "VALUES ('y','',1,1)")
        c.commit()
        c.close()
        c2 = open_db(legacy)
        got = dict(c2.execute("SELECT hash, posted_date FROM jobs"))
        check("backfill: legacy row gets posted_date", got["x"] == "2026-03-23")
        check("backfill: genuinely undated row stays empty", not got["y"])
        c2.close()

    # --- date normalization ---
    T = date(2026, 8, 17)
    check("date: ISO datetime", normalize_date("2026-08-13T01:16:00", T) == "2026-08-13")
    check("date: plain ISO", normalize_date("2026-08-13", T) == "2026-08-13")
    check("date: Apple 'Aug 06, 2026'",
          normalize_date("Aug 06, 2026", T) == "2026-08-06")
    check("date: Workday 'Today'", normalize_date("Today", T) == "2026-08-17")
    check("date: Workday '3 Days Ago'",
          normalize_date("3 Days Ago", T) == "2026-08-14")
    check("date: Workday '30+ Days Ago'",
          normalize_date("30+ Days Ago", T) == "2026-07-18")
    check("date: '2 Weeks Ago'", normalize_date("2 Weeks Ago", T) == "2026-08-03")
    check("date: Hebrew 'לפני 5 ימים'",
          normalize_date("לפני 5 ימים", T) == "2026-08-12")
    check("date: empty stays empty", normalize_date("", T) == "")
    check("date: junk stays empty", normalize_date("recently", T) == "")

    w = build_where(max_age_days=7)
    check("where: undated jobs kept by default", " IS NULL OR " in w)
    check("where: strict dates drops undated",
          " IS NULL OR " not in build_where(max_age_days=7, strict_dates=True))

    # --- effective date: first_seen counts, but not for the opening backlog ---
    with tempfile.TemporaryDirectory() as td:
        db = open_db(Path(td) / "t.db")
        run1 = "2026-08-01T09:00:00+00:00"
        run2 = "2026-08-20T09:00:00+00:00"
        db.execute("INSERT INTO company_meta (company, first_run) VALUES (?,?)",
                   ("Acme", run1))

        def add(h, title, first_seen, posted_date=""):
            db.execute(
                "INSERT INTO jobs (hash,company,title,url,location,posted_date,"
                " first_seen,last_seen,is_open,in_israel,is_student) "
                " VALUES (?,?,?,?,?,?,?,?,1,1,0)",
                (h, "Acme", title, "u", "Tel Aviv", posted_date, first_seen,
                 run2))

        add("h1", "Backlog undated", run1)              # first run, no date
        add("h2", "Backlog dated", run1, "2026-07-15")  # first run, real date
        add("h3", "Appeared later", run2)               # after tracking began
        db.commit()

        eff = dict(db.execute(
            f"SELECT title, {EFFECTIVE_DATE} FROM jobs").fetchall())
        check("effective date: opening backlog stays undated",
              eff["Backlog undated"] is None)
        check("effective date: published date always wins",
              eff["Backlog dated"] == "2026-07-15")
        check("effective date: job seen after tracking began gets its date",
              eff["Appeared later"] == "2026-08-20")

        src = dict(db.execute(
            f"SELECT title, {DATE_SOURCE} FROM jobs").fetchall())
        check("date source: labelled published", src["Backlog dated"] == "published")
        check("date source: labelled first_seen",
              src["Appeared later"] == "first_seen")
        check("date source: blank when unknown", src["Backlog undated"] == "")

        new_rows = {r[0] for r in db.execute(
            f"SELECT title FROM jobs WHERE {IS_TRULY_NEW}")}
        check("truly new: backlog is not new", "Backlog undated" not in new_rows)
        check("truly new: later arrival is new", "Appeared later" in new_rows)

        # A 30-day window from 2026-08-23 keeps the later arrival, keeps the
        # undated backlog by default, and drops the July posting.
        titles = {r[0] for r in db.execute(
            "SELECT title FROM jobs WHERE "
            + build_where(max_age_days=30).replace(
                "DATE('now','-30 day')", "'2026-07-24'"))}
        check("date filter: recent arrival kept", "Appeared later" in titles)
        check("date filter: undated kept by default", "Backlog undated" in titles)
        check("date filter: old published date dropped",
              "Backlog dated" not in titles)
        strict = {r[0] for r in db.execute(
            "SELECT title FROM jobs WHERE "
            + build_where(max_age_days=30, strict_dates=True).replace(
                "DATE('now','-30 day')", "'2026-07-24'"))}
        check("date filter: strict drops the undated backlog",
              strict == {"Appeared later"})

        # save() must stamp a company the first time it is seen, and never again
        db.execute("DELETE FROM company_meta")
        db.commit()
        j = Job("greenhouse", "NewCo", "Student Role", "http://u",
                location="Tel Aviv")
        save(db, [j], set())
        stamped = db.execute(
            "SELECT first_run FROM company_meta WHERE company='NewCo'").fetchone()
        check("save: first run stamped for a new company", stamped is not None)
        save(db, [j], set())
        again = db.execute(
            "SELECT first_run FROM company_meta WHERE company='NewCo'").fetchone()
        check("save: first run never moves", again[0] == stamped[0])
        check("save: a company's own first batch is not new",
              not db.execute(
                  f"SELECT COUNT(*) FROM jobs WHERE company='NewCo' "
                  f"AND {IS_TRULY_NEW}").fetchone()[0])
        db.close()

    # --- senior prefix ---
    csen = Classifier({**filters, "senior_title_prefixes":
                       ["senior", "sr", "sr.", "staff", "principal", "lead"]})
    def sen(t): return csen.is_senior(Job("x", "Y", t, "u"))
    check("senior: 'Senior Software Engineer'", sen("Senior Software Engineer"))
    check("senior: 'Sr. Data Analyst'", sen("Sr. Data Analyst"))
    check("senior: 'Staff Engineer'", sen("Staff Engineer"))
    check("senior: leading whitespace", sen("  Senior Analyst"))
    check("senior: plain engineer not senior", not sen("Software Engineer"))
    check("senior: 'Seniority Coordinator' not senior",
          not sen("Seniority Program Coordinator"))
    check("senior: prefix-only, mid-title ignored",
          not sen("Intern, Senior Design Team"))

    # --- where clause ---
    w = build_where(max_years=2)
    check("where: unknown years kept by default", "min_years IS NULL" in w)
    check("where: strict drops unknown",
          "IS NULL" not in build_where(max_years=2, strict_years=True))

    gh = fetch_greenhouse.__doc__ is not None
    check("connectors documented", gh)

    # Greenhouse payload shape
    sample = {"jobs": [{"title": "Student Analyst", "absolute_url": "http://u",
                        "location": {"name": "Tel Aviv, Israel"},
                        "updated_at": "2026-08-01T10:00:00Z",
                        "content": "<p>Great &amp; fun</p>",
                        "departments": [{"name": "Data"}], "offices": []}]}
    parsed = _parse_greenhouse_payload(sample, "Wiz")
    check("greenhouse parse: 1 job", len(parsed) == 1)
    check("greenhouse parse: location", parsed[0].location == "Tel Aviv, Israel")
    check("greenhouse parse: date", parsed[0].posted_at == "2026-08-01")
    check("greenhouse parse: html stripped", parsed[0].description == "Great & fun")

    # Lever payload shape
    lever_rows = [{
        "id": "abc", "text": "Computer Vision Intern",
        "hostedUrl": "https://jobs.lever.co/acme/abc",
        "createdAt": 1785542400000,          # milliseconds, not seconds
        "categories": {"location": "Tel Aviv", "commitment": "Intern",
                       "team": "Algorithms", "allLocations": ["Tel Aviv"]},
        "descriptionPlain": "Work on vision models",
        "lists": [{"text": "Requirements",
                   "content": "<li>2 years of experience</li>"}],
    }]
    lv = _parse_lever_rows(lever_rows, "Acme")
    check("lever parse: 1 job", len(lv) == 1)
    check("lever parse: title", lv[0].title == "Computer Vision Intern")
    check("lever parse: ms epoch date", lv[0].posted_at == "2026-08-01")
    check("lever parse: lists become qualifications",
          "2 years of experience" in lv[0].qualifications)
    check("lever parse: years readable",
          extract_min_years(lv[0].qualifications) == 2)

    # Ashby payload shape
    ashby = {"jobs": [
        {"title": "Vision Engineer", "location": "Tel Aviv",
         "secondaryLocations": [{"location": "Haifa"}],
         "employmentType": "Intern", "department": "R&D",
         "jobUrl": "https://jobs.ashbyhq.com/acme/1",
         "publishedAt": "2026-08-01T09:00:00.000Z",
         "descriptionPlain": "text", "isListed": True},
        {"title": "Hidden", "location": "X", "isListed": False},
    ]}
    ab = _parse_ashby_payload(ashby, "Acme")
    check("ashby parse: unlisted dropped", len(ab) == 1)
    check("ashby parse: secondary locations merged",
          ab[0].location == "Tel Aviv, Haifa")
    check("ashby parse: date", ab[0].posted_at == "2026-08-01")

    # SmartRecruiters payload shape
    newc = Classifier(filters)
    sr_rows = [{"id": "744000", "name": "Software Engineer",
                "location": {"city": "Tel Aviv-Yafo", "region": "Tel Aviv",
                             "country": "il", "remote": False},
                "department": {"label": "R&D"},
                "typeOfEmployment": {"label": "Full-time"},
                "experienceLevel": {"label": "Student"},
                "releasedDate": "2026-08-01T00:00:00.000Z"}]
    sr = _parse_smartrecruiters_rows(sr_rows, "Palo Alto Networks", "pan2")
    check("smartrecruiters parse: 1 job", len(sr) == 1)
    check("smartrecruiters parse: country code spelled out",
          "Israel" in sr[0].location)
    check("smartrecruiters parse: url built",
          sr[0].url == "https://jobs.smartrecruiters.com/pan2/744000")
    check("smartrecruiters parse: israel classifier sees it",
          newc.in_israel(sr[0]))

    # Comeet payload shape
    comeet_rows = [
        {"uid": "AA.001", "name": "Computer Vision Student", "status": "open",
         "location": {"name": "Tel Aviv", "city": "Tel Aviv",
                      "country": "Israel"},
         "department": "Algorithms", "employment_type": "Part-time",
         "experience_level": "Student",
         "url_active_page": "https://acme.com/careers/cv-student",
         "time_updated": "2026-08-01T08:00:00Z",
         "details": [{"name": "Description", "value": "<p>Vision work</p>"},
                     {"name": "Requirements", "value": "2 years of experience"}]},
        {"uid": "AA.002", "name": "Closed Role", "status": "closed",
         "location": {"name": "Tel Aviv"}},
        {"uid": "AA.003", "name": "Internal Only", "status": "open",
         "is_internal": True, "location": {"name": "Tel Aviv"}},
    ]
    cm = _parse_comeet_rows(comeet_rows, "Acme")
    check("comeet parse: closed and internal dropped", len(cm) == 1)
    check("comeet parse: location composed", cm[0].location == "Tel Aviv, Israel")
    check("comeet parse: description from details",
          cm[0].description == "Vision work")
    check("comeet parse: requirements feed --max-years",
          extract_min_years(cm[0].qualifications) == 2)
    check("comeet parse: dict payload also accepted",
          len(_parse_comeet_rows({"positions": comeet_rows}, "Acme")) == 1)
    check("comeet parse: student + israel classified",
          newc.is_student(cm[0]) and newc.in_israel(cm[0]))

    # Comeet credential extraction from a careers page
    init_page = (
        '<script>window.comeetInit = function() { COMEET.init({'
        '"token": "1A.2B3C", "company-uid": "D5.007",'
        '"company-name":"Acme"}); };</script>'
    )
    uid, tok = extract_comeet_credentials(init_page)
    check("comeet probe: uid from COMEET.init", uid == "D5.007")
    check("comeet probe: token from COMEET.init", tok == "1A.2B3C")
    url_page = ('<script src="https://www.comeet.co/careers-api/2.0/company/'
                'A6.005/positions?token=9Z.8Y7X&details=true"></script>')
    uid2, tok2 = extract_comeet_credentials(url_page)
    check("comeet probe: uid from API url", uid2 == "A6.005")
    check("comeet probe: token from API url", tok2 == "9Z.8Y7X")
    check("comeet probe: nothing found stays empty",
          extract_comeet_credentials("<html>no comeet here</html>") == ("", ""))

    # Comeet plugin markup, rendered into the company's own page.
    # Arbe groups by department and puts the city in the meta line; Imagry
    # groups by location and puts the department there. Both are here, because
    # a parser that only ever saw one of them would look correct and be wrong.
    arbe_html = (
        '<div class="comeet-g-r Engineering">'
        '<div class="comeet-list comeet-group-name">'
        '<a href="#">Engineering</a></div>'
        '<ul class="comeet-positions-list"><li>'
        '<a class="comeet-position" href="//arbe.com/career/co/eng/33.D67/'
        'dsp-software-team-leader/all">'
        '<div class="comeet-position-name">'
        '<!--<a href="#">DSP Software Team Leader</a>-->'
        'DSP Software Team Leader</div>'
        '<div class="comeet-position-meta">Tel Aviv &middot; Full-time</div>'
        '</a></li></ul></div>'
    )
    ce = _parse_comeet_embed_page(arbe_html, "Arbe", "https://www.arbe.com")
    check("comeet_embed: 1 job", len(ce) == 1)
    check("comeet_embed: commented-out title not duplicated",
          ce[0].title == "DSP Software Team Leader")
    check("comeet_embed: protocol-relative href fixed",
          ce[0].url.startswith("https://arbe.com/career/"))
    check("comeet_embed: employment type split out",
          ce[0].employment_type == "Full-time")
    check("comeet_embed: group + city both land in location",
          ce[0].location == "Engineering, Tel Aviv")
    check("comeet_embed: israel classifier sees it", newc.in_israel(ce[0]))

    imagry_html = (
        '<div class="comeet-g-r"><div class="comeet-list comeet-group-name">'
        'Haifa, Israel</div><ul class="comeet-positions-list"><li>'
        '<a class="comeet-position" href="//imagry.co/x/co/haifa/06.D63/jr/all">'
        '<div class="comeet-position-name">Jr. DevOps / MLOps Engineer</div>'
        '<div class="comeet-position-meta">R&amp;D &middot; Full-time '
        '&middot; Entry-level</div></a></li></ul></div>'
    )
    ci = _parse_comeet_embed_page(imagry_html, "Imagry", "https://imagry.co")
    check("comeet_embed: location-grouped site also works",
          ci[0].location == "Haifa, Israel, R&D")
    check("comeet_embed: seniority split out",
          ci[0].experience_level == "Entry-level")
    check("comeet_embed: israel found when it is the group",
          newc.in_israel(ci[0]))

    # Trigo's bespoke theme
    trigo_html = (
        '<div class="item read-more-click show operations-il israel">'
        '<div class="title">'
        '<a href="https://www.trigoretail.com/careers/?uid=92.A5A">'
        'Data Engineer</a><div class="emp-type">Full-time</div></div>'
        '<div class="job-info"><div class="department">Operations IL</div>'
        '<div class="divider">/</div>'
        '<div class="location">Israel</div></div></div>'
    )
    tg = _parse_trigo_page(trigo_html, "Trigo")
    check("trigo parse: 1 job", len(tg) == 1)
    check("trigo parse: title", tg[0].title == "Data Engineer")
    check("trigo parse: location separate from department",
          tg[0].location == "Israel" and tg[0].department == "Operations IL")

    # Hailo's custom theme. The location div wraps an icon div, which is the
    # trap: a lazy match to the first </div> returns the icon and no location.
    hailo_html = (
        '<a class="b-careers-lobby__job" '
        'href="https://hailo.ai/company-overview/careers/ml-engineer/07.432/" '
        'data-location="7E.109" data-department="Other">'
        '<div class="b-careers-lobby__job-name-dep">'
        '<h3 class="b-careers-lobby__job-name">ML Engineer</h3>'
        '<div class="b-careers-lobby__job-department">Algorithms</div></div>'
        '<div class="b-careers-lobby__job-location">'
        '<div class="b-careers-lobby__job-location-icon"></div>'
        'Tel Aviv-Yafo, IL</div>'
        '<div class="b-careers-lobby__job-arrow"></div></a>'
    )
    hj = _parse_hailo_page(hailo_html, "Hailo")
    check("hailo parse: 1 job", len(hj) == 1)
    check("hailo parse: title", hj[0].title == "ML Engineer")
    check("hailo parse: location past the nested icon div",
          hj[0].location == "Tel Aviv-Yafo, IL")
    check("hailo parse: department", hj[0].department == "Algorithms")
    check("hailo parse: israel classifier sees it", newc.in_israel(hj[0]))

    # Magnus Metal's Elementor page. The trap is the footer: headings after
    # the last Apply button must not be read as part of the last job.
    magnus_html = (
        '<h2 class="elementor-heading-title">Job#2418</h2>'
        '<h2 class="elementor-heading-title">Experimental Physicist\n</h2>'
        '<h2 class="elementor-heading-title">Hands-on development.</h2>'
        '<a class="elementor-button" href="https://magnusmetal.com/careers/'
        'experimental-physicist-job2418/">Apply</a>'
        '<h2 class="elementor-heading-title">Contact us</h2>'
    )
    mg = _parse_magnus_page(magnus_html, "Magnus Metal")
    check("magnus parse: 1 job", len(mg) == 1)
    check("magnus parse: title", mg[0].title == "Experimental Physicist")
    check("magnus parse: footer heading not in description",
          mg[0].description == "Hands-on development.")
    check("magnus parse: israel classifier sees it", newc.in_israel(mg[0]))

    # Teamtailor markup shape
    tt_html = (
        '<ul id="jobs_list_container"><li>'
        '<a data-turbo="false" href="https://careers.x.com/jobs/8247234-senior-ai-researcher">'
        '<span class="absolute inset-0"></span>Senior AI Researcher</a>'
        '<div class="mt-1 text-md"><span>R&amp;D</span>'
        '<span class="mx-[2px]">&middot;</span>'
        '<span>Tel Aviv-Yafo (IL)</span>'
        '<span class="mx-[2px]">&middot;</span><span>Hybrid</span></div></li>'
        '<li><a href="/jobs/999-support-lead">Support Lead</a>'
        '<div class="mt-1 text-md"><span>Ops</span>'
        '<span>New York (US)</span></div></li></ul>'
    )
    tt = _parse_teamtailor_page(tt_html, "Nexar", "https://careers.x.com")
    check("teamtailor parse: 2 jobs", len(tt) == 2)
    check("teamtailor parse: title", tt[0].title == "Senior AI Researcher")
    check("teamtailor parse: location by country code",
          tt[0].location == "Tel Aviv-Yafo (IL)")
    check("teamtailor parse: department", tt[0].department == "R&D")
    check("teamtailor parse: workplace type", tt[0].employment_type == "Hybrid")
    check("teamtailor parse: relative href made absolute",
          tt[1].url == "https://careers.x.com/jobs/999-support-lead")
    check("teamtailor parse: israel classifier sees it", newc.in_israel(tt[0]))
    check("teamtailor parse: second card keeps its own location",
          tt[1].location == "New York (US)")

    # Radancy markup shape
    # Real Palo Alto Networks markup. The tenant prefix ("section29__") is
    # per-site, the suffixes are not, so the patterns key off the suffixes.
    # The second card exists to pin the bug this fixture was written for:
    # unbounded chunks let one card's title land in the next card's location.
    radancy_html = (
        '<ul class="section29__search-results-ul">'
        '<li class="section29__search-results-li">'
        '<a class="section29__search-results-link"'
        ' href="/en/job/tel-aviv/vision-algorithms-student/47263/999">'
        '<h2 class="section29__search-results-job-title">'
        'Vision Algorithms Student</h2>'
        '<div class="section29__result-info-container">'
        '<span class="section29__result-location newLoc">'
        'Tel Aviv-Yafo, Israel</span>'
        '<span class="section29__result-category"><span>Product Engineering'
        '</span></span></div></a></li>'
        '<li class="section29__search-results-li">'
        '<a class="section29__search-results-link"'
        ' href="/en/job/sofia/backend-engineer/47263/998">'
        '<h2 class="section29__search-results-job-title">Backend Engineer</h2>'
        '<div class="section29__result-info-container">'
        '<span class="section29__result-location newLoc">Sofia, Bulgaria</span>'
        '</div></a></li></ul>'
    )
    rd = _parse_radancy_page(radancy_html, "Palo Alto Networks",
                             "https://jobs.paloaltonetworks.com")
    check("radancy parse: 2 jobs", len(rd) == 2)
    check("radancy parse: title", rd[0].title == "Vision Algorithms Student")
    check("radancy parse: location", rd[0].location == "Tel Aviv-Yafo, Israel")
    check("radancy parse: category becomes department",
          rd[0].department == "Product Engineering")
    check("radancy parse: absolute url",
          rd[0].url.startswith("https://jobs.paloaltonetworks.com/en/job/"))
    check("radancy parse: no date published, stays empty",
          rd[0].posted_at == "")
    check("radancy parse: second card keeps its own location",
          rd[1].location == "Sofia, Bulgaria")
    check("radancy parse: title does not leak into next location",
          "Vision" not in rd[1].location)
    check("radancy parse: non-israel row not matched",
          not newc.in_israel(rd[1]))
    check("radancy parse: legacy job-location class still works",
          _parse_radancy_page(
              '<a href="/en/job/x/y/1/2"><h2>T</h2>'
              '<span class="job-location">Haifa, Israel</span></a>',
              "X", "https://x")[0].location == "Haifa, Israel")

    print()
    print("  ALL PASS" if ok else "  FAILURES PRESENT")
    return 0 if ok else 1


def _parse_greenhouse_payload(data: dict, company: str) -> list[Job]:
    """Extracted so the parser can be tested without network access."""
    out = []
    for j in data.get("jobs", []):
        offices = ", ".join(
            o.get("name", "") for o in (j.get("offices") or []) if o.get("name")
        )
        loc = (j.get("location") or {}).get("name", "") or offices
        depts = ", ".join(
            d.get("name", "") for d in (j.get("departments") or []) if d.get("name")
        )
        out.append(Job(
            source="greenhouse", company=company, title=j.get("title", ""),
            url=j.get("absolute_url", ""), location=loc, department=depts,
            description=strip_html(j.get("content", ""))[:4000],
            posted_at=(j.get("updated_at") or "")[:10],
        ))
    return out


DEBUG_MARKERS = {
    "google_careers": ["lLd3Je", "<h3", "Minimum qualifications", "place"],
    "apple": ["/details/", "team-name", "job-posted-date", "<h3"],
    "mobileye": ["jobItem", "/jobs/"],
    "radancy": ["/job/", "<h2", "job-location"],
    "comeet_embed": ["comeet-position", "comeet-position-name",
                     "comeet-position-meta"],
    "trigo": ["?uid=", "emp-type", "job-info"],
    "hailo": ["b-careers-lobby__job", "job-name", "job-location"],
    "magnus": ["Job#", "/careers/", "elementor-heading-title"],
}

DEBUG_URLS = {
    "google_careers": lambda c: (
        "https://www.google.com/about/careers/applications/jobs/results"
        f"?location={urllib.parse.quote(c.get('location','Israel'))}&page=1"),
    "apple": lambda c: (
        f"{APPLE_BASE}/en-us/search"
        f"?location={c.get('location_code','israel-ISR')}&page=1"),
    "mobileye": lambda c: f"{MOBILEYE_BASE}/jobs",
    "radancy": lambda c: c["base"].rstrip("/") + c["path"].format(page=1),
    "comeet_embed": lambda c: c["url"],
    "trigo": lambda c: c.get("url", "https://www.trigoretail.com/careers/"),
    "hailo": lambda c: c.get("url", HAILO_URL),
    "magnus": lambda c: c.get("url", MAGNUS_URL),
}


def debug_source(name: str) -> int:
    """
    Fetch one page exactly as the scraper does, save it, and report what the
    parser can see. Turns "returns 0" into evidence instead of guesswork.
    """
    cfg = load_config()
    src = next((s for s in cfg.get("sources", [])
                if s["name"].lower() == name.lower()), None)
    if not src:
        print(f"No source named {name!r}", file=sys.stderr)
        return 2
    stype = src["type"]

    # JSON sources: call the connector directly and report what came back.
    if stype not in DEBUG_URLS:
        fn = CONNECTORS.get(stype)
        if not fn:
            print(f"Unknown source type {stype!r}")
            return 2
        print(f"Source : {name}  ({stype})\n")
        try:
            jobs = fn(src)
        except FetchError as e:
            print(f"FETCH FAILED: {e}\n")
            print("Diagnosis: the endpoint refused or could not be reached.")
            print("A 403 usually means the site rejects non-browser requests.")
            return 1
        print(f"Connector returned {len(jobs)} jobs")
        if jobs:
            cls = Classifier(cfg.get("filters", {}))
            tagged = cls.apply(jobs)
            il = [j for j in tagged if j.in_israel]
            st = [j for j in il if j.is_student]
            print(f"  in Israel : {len(il)}")
            print(f"  student   : {len(st)}")
            print(f"  dated     : {sum(1 for j in tagged if j.posted_date)}")
            print("\nfirst rows:")
            for j in tagged[:5]:
                print(f"  - {j.title[:48]:<48} | {j.location[:28]:<28} "
                      f"| {j.posted_date or '-'}")
            if not il:
                print("\nDiagnosis: jobs arrived but none matched israel_terms.")
                print("Check the location values above against sources.yaml.")
        else:
            print("\nDiagnosis: the request succeeded but returned no rows.")
            print("Either the feed is genuinely empty or the response shape changed.")
        return 0 if jobs else 1

    url = DEBUG_URLS[stype](src)
    print(f"Source : {name}  ({stype})")
    print(f"URL    : {url}")
    print(f"Timeout: {REQUEST_TIMEOUT}s per request\n")

    # The real connectors warm up the host first so a WAF can hand over its
    # cookie. Skipping that here made --debug fail on sites that a full run
    # handles fine, which is the opposite of what a debug command is for.
    if stype in ("radancy", "mobileye", "comeet_embed", "trigo", "hailo"):
        print("  warming up host ...", end="", flush=True)
        t0 = time.time()
        warm_up(url)
        print(f" {time.time() - t0:.1f}s")

    print("  fetching page 1 ...", end="", flush=True)
    t0 = time.time()
    try:
        body = http_text(url)
    except FetchError as e:
        print(f" FAILED after {time.time() - t0:.1f}s")
        print(f"\n{e}\n")
        msg = str(e).lower()
        if "timed out" in msg or "timeout" in msg:
            print("A timeout with no HTTP status means the server accepted the")
            print("connection and then never answered. That is bot protection")
            print("stalling a non-browser client, not a slow network.")
        elif "403" in msg or "forbidden" in msg:
            print("403 means the site recognised this as a non-browser request")
            print("and refused it outright.")
        elif "404" in msg:
            print("404 means the URL itself is wrong — check base and path in")
            print("sources.yaml against what the site's own filter produces.")
        print("Open the same URL in a browser. If it loads there but not here,")
        print("the site needs a real browser and cannot be scraped this way.")
        return 1
    print(f" {time.time() - t0:.1f}s")

    out = HERE / f"debug_{name.lower()}.html"
    out.write_text(body, encoding="utf-8")
    print(f"Fetched {len(body):,} chars -> saved to {out.name}\n")
    print("Markers the parser needs:")
    missing = 0
    for marker in DEBUG_MARKERS.get(stype, []):
        n = body.count(marker)
        if n == 0:
            missing += 1
        print(f"  {'ok ' if n else 'MISSING'}  {marker!r:32} x{n}")

    parser = {
        "google_careers": _parse_google_page,
        "apple": _parse_apple_page,
        "mobileye": _parse_mobileye_page,
        "radancy": lambda b: _parse_radancy_page(
            b, src["name"], src["base"].rstrip("/")),
        "comeet_embed": lambda b: _parse_comeet_embed_page(
            b, src["name"],
            "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(src["url"]))),
        "trigo": lambda b: _parse_trigo_page(b, src["name"]),
        "hailo": lambda b: _parse_hailo_page(b, src["name"]),
        "magnus": lambda b: _parse_magnus_page(
            b, src["name"], src.get("location", MAGNUS_LOCATION)),
    }[stype]
    jobs = parser(body)
    print(f"\nParser produced {len(jobs)} jobs")
    for j in jobs[:5]:
        print(f"  - {j.title[:55]:<55} | {j.location[:32]}")

    if not jobs:
        print("\nDiagnosis:")
        if missing:
            print("  The markup markers are absent. The server returned a different")
            print("  page than a browser gets — usually bot filtering, a consent")
            print("  wall, or a redirect. Open the saved HTML file and look at it.")
        else:
            print("  Markers are present but nothing parsed — the markup changed")
            print("  shape. The saved HTML file shows the new structure.")
    return 0 if jobs else 1


# Fingerprints for identifying which ATS a careers site runs on.
ATS_SIGNATURES = {
    "greenhouse":      ["boards.greenhouse.io", "job-boards.greenhouse.io",
                        "boards-api.greenhouse.io"],
    # comeet-position / comeet-positions-list mean the board is rendered INTO
    # the page by the Comeet plugin — jobs are already in the HTML and no
    # token is needed. That is a different, better case than an embed that
    # only calls comeet.co from JavaScript, so probe reports it separately.
    "comeet":          ["comeet.co", "comeet.com", "careers-api/2.0",
                        "comeet-position", "comeet-positions-list",
                        "comeet-groups-list"],
    "lever":           ["jobs.lever.co", "api.lever.co"],
    "ashby":           ["ashbyhq.com"],
    "workday":         ["myworkdayjobs.com", "wday/cxs"],
    "eightfold":       ["eightfold.ai", "/api/apply/v2/", "/api/pcsx/"],
    "smartrecruiters": ["smartrecruiters.com"],
    "workable":        ["apply.workable.com"],
    "recruitee":       ["recruitee.com"],
    "teamtailor":      ["teamtailor.com"],
    "successfactors":  ["successfactors.com", "sfcareer", "career2.sap"],
    "taleo":           ["taleo.net"],
    "icims":           ["icims.com"],
    "phenom":          ["phenompeople.com", "phenom.com"],
    "radancy":         ["radancy", "talentbrew", "/search-jobs/results"],
    "niloosoft":       ["niloosoft", "hunterhrms", "hunter.co.il"],
    "jobinfo":         ["jobinfo.co.il"],
    "sniffer":         ["/api/jobs", "/api/positions", "graphql"],
}

# Bot protection that answers with a stub instead of the page. Worth naming,
# because "no ATS signature found" on an 840-byte body is a different problem
# from a page that genuinely has no ATS.
WAF_SIGNATURES = {
    "Imperva / Incapsula": ["_incapsula_resource", "incapsula incident"],
    "Cloudflare":          ["cf-browser-verification", "cdn-cgi/challenge"],
    "Akamai":              ["akamai bot manager", "_abck"],
    "PerimeterX":          ["_px", "perimeterx"],
}


# Comeet's own widget documentation puts the company UID and token straight
# into the careers page, either in a COMEET.init({...}) call or in the API URL
# the page fetches. Both forms are matched here. The token is a careers-website
# credential, not a private API key — it is meant to be readable by the page.
COMEET_TOKEN_PATTERNS = [
    re.compile(r'["\']?token["\']?\s*:\s*["\']([\w.\-]{4,64})["\']', re.I),
    re.compile(r'comeet[_\-]?token["\']?\s*[:=]\s*["\']([\w.\-]{4,64})["\']', re.I),
    re.compile(r'careers-api/2\.0/company/[\w.\-]+/positions\?[^"\'<>]*token='
               r'([\w.\-]{4,64})', re.I),
    re.compile(r'data-comeet-token=["\']([\w.\-]{4,64})["\']', re.I),
]
COMEET_UID_PATTERNS = [
    re.compile(r'["\']?company[_\-]uid["\']?\s*:\s*["\']([\w.\-]{3,32})["\']', re.I),
    re.compile(r'comeet[_\-]?uid["\']?\s*[:=]\s*["\']([\w.\-]{3,32})["\']', re.I),
    re.compile(r'careers-api/2\.0/company/([\w.\-]{3,32})/', re.I),
    re.compile(r'comeet\.(?:co|com)/jobs/[\w.\-]+/([\w.\-]{3,32})', re.I),
    re.compile(r'data-comeet-uid=["\']([\w.\-]{3,32})["\']', re.I),
]


def extract_comeet_credentials(body: str) -> tuple[str, str]:
    """Return (uid, token) found in a careers page, or empty strings."""
    def first(patterns: list[re.Pattern]) -> str:
        for pat in patterns:
            m = pat.search(body)
            if m:
                return m.group(1)
        return ""
    return first(COMEET_UID_PATTERNS), first(COMEET_TOKEN_PATTERNS)


def probe(url: str) -> int:
    """
    Fetch any careers URL and report which ATS it appears to run on.

    Use this to add a company: point it at the careers page, see which system
    comes back, then add the matching entry to sources.yaml.
    """
    print(f"Probing {url}\n")
    try:
        body = http_text(url)
    except FetchError as e:
        print(f"FETCH FAILED: {e}")
        print("\nIf this is a 403 or timeout the site is refusing non-browser")
        print("requests. Open it in a real browser and check the Network tab.")
        return 1

    # One file per host. A single probe.html meant that probing nine companies
    # in a loop left you with eight overwritten pages and only the last one.
    host = urllib.parse.urlparse(url).netloc.replace(":", "_")
    out = HERE / f"probe_{host or 'page'}.html"
    out.write_text(body, encoding="utf-8")
    print(f"Fetched {len(body):,} chars -> saved to {out.name}\n")

    if len(body) < 2000:
        print("That is far too small for a careers page. The site probably")
        print("answered with a redirect stub or a bot-protection page rather")
        print("than the real HTML.\n")

    low = body.lower()
    hits = {name: [m for m in marks if m.lower() in low]
            for name, marks in ATS_SIGNATURES.items()}
    hits = {k: v for k, v in hits.items() if v}

    if hits:
        print("ATS signatures found:")
        for name, marks in hits.items():
            print(f"  {name:<16} via {', '.join(marks)}")
    else:
        print("No known ATS signature found.")

    # Is the page even rendered server-side?
    shell = any(s in low for s in
                ("loading jobs", "loading...", "enable javascript",
                 "please enable js", "app-root", "__nuxt__", "__next_data__"))
    print(f"\nLooks client-rendered : {'YES' if shell else 'no'}")
    print(f"Page has <h3> headings: {low.count('<h3')}")
    print(f"Page has <li> items   : {low.count('<li')}")

    waf = [n for n, marks in WAF_SIGNATURES.items()
           if any(m in low for m in marks)]
    if waf:
        print(f"\nBot protection detected: {', '.join(waf)}")
        print("The server answered with a challenge stub, not the page. A")
        print("plain Python request will not get past this one.")
        return 1

    if any(m in low for m in ("comeet-position", "comeet-positions-list",
                              "comeet-groups-list")):
        print("\nComeet is rendered INTO this page by the plugin, so the jobs")
        print("are already in the HTML and no token is needed.")
        print("\nPaste into sources.yaml:\n")
        print("  - name: COMPANY")
        print("    type: comeet_embed")
        print(f"    url: {url}")
        print("    enabled: true")
        return 0

    if "comeet" in hits:
        uid, token = extract_comeet_credentials(body)
        print("\nComeet credentials in the page source:")
        print(f"  company uid : {uid or 'NOT FOUND'}")
        print(f"  token       : {token or 'NOT FOUND'}")
        if uid and token:
            print("\nPaste into sources.yaml:\n")
            print("  - name: COMPANY")
            print("    type: comeet")
            print(f'    uid: "{uid}"')
            print(f'    token: "{token}"')
            print("    enabled: true")
            return 0
        print("\n  This company links to a Comeet-hosted board instead of")
        print("  embedding the widget, so no token is in the page. Try their")
        print("  careers page on their own domain rather than comeet.com.")

    print("\nWhat to do next:")
    if hits and not shell:
        first = next(iter(hits))
        print(f"  Looks like {first}. Add it to sources.yaml with type: {first}")
        print("  (see the README for the identifier each type needs)")
    elif shell:
        print("  The job list is built by JavaScript, so a plain fetch will not")
        print("  see it. Open the page in a browser, watch the Network tab, and")
        print("  find the request that returns the jobs. That URL is what to use.")
    else:
        print(f"  Unclear. Open {out.name} and search it for job titles you can")
        print("  see on the live page — if they are absent, the page is dynamic.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--students-only", action="store_true",
                    help="only student/intern/entry-level roles")
    ap.add_argument("--part-time", action="store_true",
                    help="only part-time roles")
    ap.add_argument("--max-years", type=int, metavar="N",
                    help="only roles asking for N years of experience or fewer")
    ap.add_argument("--strict-years", action="store_true",
                    help="with --max-years, also drop roles whose requirements "
                         "could not be read (default keeps them)")
    ap.add_argument("--max-age-days", type=int, metavar="N",
                    help="only jobs posted within the last N days")
    ap.add_argument("--strict-dates", action="store_true",
                    help="with --max-age-days, also drop jobs with no published date")
    ap.add_argument("--no-senior", action="store_true",
                    help="drop roles whose title starts with Senior/Staff/Principal")
    ap.add_argument("--source", help="run a single source by name")
    ap.add_argument("--selftest", action="store_true",
                    help="run offline tests (no network) and exit")
    ap.add_argument("--debug", metavar="SOURCE",
                    help="fetch one page from an HTML source, save it, and "
                         "report what the parser can see")
    ap.add_argument("--probe", metavar="URL",
                    help="identify which ATS a careers page runs on, so you "
                         "know what to add to sources.yaml")
    ap.add_argument("--timeout", type=int, metavar="SECONDS",
                    help="per-request timeout (default 60). Lower it to make "
                         "a stalled site fail fast instead of looking hung.")
    args = ap.parse_args()

    if args.timeout:
        global REQUEST_TIMEOUT
        REQUEST_TIMEOUT = args.timeout

    if args.selftest:
        print("Running offline self-test\n")
        return selftest()

    if args.debug:
        return debug_source(args.debug)

    if args.probe:
        return probe(args.probe)

    print("Fetching sources\n")
    return run(students_only=args.students_only, only_source=args.source,
               part_time_only=args.part_time, max_years=args.max_years,
               strict_years=args.strict_years, hide_senior=args.no_senior,
               max_age_days=args.max_age_days, strict_dates=args.strict_dates)


if __name__ == "__main__":
    raise SystemExit(main())
