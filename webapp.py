#!/usr/bin/env python3
"""
Local web interface for the job scraper.

    python webapp.py

Then open http://localhost:8765 in your browser.

Runs entirely on your machine. No dependency beyond what scraper.py needs.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
import threading
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import scraper

PORT = 8765
# Build id derived from the file's own contents. Changing the UI changes this
# automatically, so it can never drift out of sync the way a hand-bumped
# number does.
UI_VERSION = hashlib.sha256(
    pathlib.Path(__file__).read_bytes()).hexdigest()[:8]

# Shared state for the background refresh. Guarded by _lock because the HTTP
# server is threaded and the status endpoint is polled while a run is going.
_lock = threading.Lock()
_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "log": [],
    "health": [],
    "error": None,
    "summary": {},
}


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _log(msg: str) -> None:
    with _lock:
        _state["log"].append(msg)


def _snapshot() -> dict:
    with _lock:
        return json.loads(json.dumps(_state))


# --------------------------------------------------------------------------
# Refresh worker
# --------------------------------------------------------------------------

def _refresh(only_source: str | None = None) -> None:
    """Run the scrape. Never raises — failures land in the status payload."""
    try:
        cfg = scraper.load_config()
        classifier = scraper.Classifier(cfg.get("filters", {}))
        sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
        if only_source:
            sources = [s for s in sources
                       if s["name"].lower() == only_source.lower()]

        all_jobs, health, healthy = [], [], set()
        for s in sources:
            fn = scraper.CONNECTORS.get(s["type"])
            if not fn:
                health.append([s["name"], f"unknown type {s['type']}", 0])
                continue
            _log(f"fetching {s['name']} ...")
            try:
                jobs = fn(s)
            except Exception as e:  # noqa: BLE001
                _log(f"  {s['name']} FAILED: {e}")
                health.append([s["name"], f"failed: {e}", 0])
                _set(health=health)
                continue
            status = "ok" if jobs else "returned 0 jobs - check connector"
            health.append([s["name"], status, len(jobs)])
            if jobs:
                healthy.add(s["name"])
            all_jobs.extend(jobs)
            _log(f"  {s['name']}: {len(jobs)} jobs")
            _set(health=health)

        all_jobs = classifier.apply(all_jobs)
        unique = {}
        for j in all_jobs:
            unique.setdefault(j.hash, j)
        deduped = list(unique.values())

        conn = scraper.open_db()
        new_count, closed = scraper.save(conn, deduped, healthy)
        conn.close()

        israel = [j for j in deduped if j.in_israel]
        summary = {
            "fetched": len(all_jobs),
            "deduped": len(deduped),
            "israel": len(israel),
            "student": sum(1 for j in israel if j.is_student),
            "senior": sum(1 for j in israel if j.is_senior),
            "part_time": sum(1 for j in israel if j.is_part_time),
            "new": new_count,
            "closed": closed,
        }
        _log(f"done — {summary['israel']} in Israel, {new_count} new")
        _set(summary=summary, health=health, error=None)
    except Exception:
        _set(error=traceback.format_exc(limit=3))
        _log("run failed, see error")
    finally:
        _set(running=False,
             finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def start_refresh(only_source: str | None = None) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, log=[], health=[], error=None, summary={},
                      started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      finished_at=None)
    threading.Thread(target=_refresh, args=(only_source,), daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Facets
# --------------------------------------------------------------------------

# Locations arrive as free text and differ per source: "Tel Aviv-Yafo, Israel",
# "Petach Tikva, Israel", "Haifa, Israel, R&D". Bucketing them makes the filter
# usable. Order matters — the first match wins, so specific cities are listed
# before the country-wide fallback.
CITY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Tel Aviv",      ("tel aviv", "tel-aviv", "telaviv", "tlv", "תל אביב")),
    ("Ramat Gan",     ("ramat gan", "רמת גן")),
    ("Givatayim",     ("givatayim", "גבעתיים")),
    ("Herzliya",      ("herzliya", "herzeliya", "הרצליה")),
    ("Petah Tikva",   ("petah tikva", "petah-tikva", "petach tikva",
                       "petak tikva", "פתח תקווה")),
    ("Haifa",         ("haifa", "חיפה")),
    ("Jerusalem",     ("jerusalem", "ירושלים")),
    ("Beer Sheva",    ("beer sheva", "be'er sheva", "beersheva", "באר שבע")),
    ("Netanya",       ("netanya", "נתניה")),
    ("Ra'anana",      ("raanana", "ra'anana", "רעננה")),
    ("Yokneam",       ("yokneam", "jokneam", "יקנעם")),
    ("Rehovot",       ("rehovot", "רחובות")),
    ("Kiryat Gat",    ("kiryat gat", "kiryat-gat", "קריית גת", "קרית גת")),
    ("Hod Hasharon",  ("hod hasharon", "הוד השרון")),
    ("Caesarea",      ("caesarea", "קיסריה")),
    ("Modiin",        ("modiin", "modi'in", "מודיעין")),
    ("Airport City",  ("airport city",)),
    ("Yakum",         ("yakum",)),
    ("Kiryot",        ("kiryot", "קריות")),
    ("Israel (other)", ("israel", "ישראל")),
]

# employment_type is whatever each ATS calls it. Group the synonyms so the
# filter has five buttons instead of thirty.
ETYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Student / Intern", ("student", "intern", "מתמחה", "סטודנט", "scholarship",
                          "apprentice", "trainee", "co-op")),
    ("Part-time",        ("part-time", "part time", "parttime", "hourly",
                          "משרה חלקית", "חצי משרה")),
    ("Contract",         ("contract", "freelance", "consultant", "temporary",
                          "temp")),
    ("Full-time",        ("full-time", "full time", "fulltime", "permanent",
                          "משרה מלאה")),
]

LEVEL_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("Student / Intern", ("student", "intern", "סטודנט", "מתמחה")),
    ("Entry level",      ("entry", "junior", "graduate", "new grad", "ג'וניור")),
    ("Mid level",        ("mid", "intermediate", "experienced", "associate")),
    ("Senior",           ("senior", "principal", "staff", "lead", "בכיר")),
    ("Management",       ("manager", "director", "head", "vp", "executive",
                          "c-level", "chief")),
]


def _bucket(value: str, rules: list[tuple[str, tuple[str, ...]]],
            fallback: str) -> str:
    low = (value or "").lower()
    if not low.strip():
        return fallback
    for label, needles in rules:
        if any(n in low for n in needles):
            return label
    return fallback


def city_of(row) -> str:
    loc = row["location"] or ""
    label = _bucket(loc, CITY_RULES, "")
    if label:
        return label
    return "Remote" if "remote" in loc.lower() else "Unspecified"


def etype_of(row) -> str:
    # A student flag on the row is a stronger signal than a blank type field.
    label = _bucket(row["employment_type"] or "", ETYPE_RULES, "")
    if label:
        return label
    return "Student / Intern" if row["is_student"] else "Unspecified"


def level_of(row) -> str:
    label = _bucket(row["experience_level"] or "", LEVEL_RULES, "")
    if label:
        return label
    if row["is_student"]:
        return "Student / Intern"
    return "Senior" if row["is_senior"] else "Unspecified"


def dept_of(row) -> str:
    return (row["department"] or "").strip() or "Unspecified"


FACETS = {
    "company": lambda r: r["company"],
    "city": city_of,
    "etype": etype_of,
    "dept": dept_of,
    "level": level_of,
}


def query_jobs(p: dict) -> dict:
    def flag(name: str) -> bool:
        return p.get(name, ["0"])[0] in ("1", "true", "on", "yes")

    def num(name: str):
        try:
            return int(p[name][0]) if p.get(name, [""])[0] else None
        except ValueError:
            return None

    students = flag("students")
    part_time = flag("part_time")
    no_senior = flag("no_senior")
    strict_dates = flag("strict_dates")
    strict = flag("strict_years")
    new_only = flag("new_only")
    max_age = num("max_age")
    max_years = num("max_years")

    # Repeated ?company=/?city=/... params. Empty values are ignored so
    # "select none" behaves like "select all" rather than returning nothing.
    chosen = {name: {v.strip() for v in p.get(name, []) if v and v.strip()}
              for name in FACETS}
    text = (p.get("q", [""])[0] or "").strip()

    where = scraper.build_where(students, part_time, max_years, strict,
                                no_senior, max_age, strict_dates)
    if new_only:
        # New TO US: a company's opening backlog is not news, however recently
        # it landed in the database.
        where += (f" AND DATE(first_seen) = DATE('now')"
                  f" AND {scraper.IS_TRULY_NEW}")

    conn = scraper.open_db()
    conn.row_factory = sqlite3.Row
    # SQL applies everything it can express. Fuzzy title matching and the facet
    # buckets cannot be expressed in SQLite, so they run in Python over the
    # narrowed result — a few thousand rows at most, which is instant.
    rows = conn.execute(
        "SELECT company,title,location,posted_at,is_student,is_part_time,"
        "min_years,is_senior,posted_date,source,url,first_seen,"
        "employment_type,experience_level,department,"
        f"{scraper.EFFECTIVE_DATE} AS effective_date,"
        f"{scraper.DATE_SOURCE} AS date_source,"
        f"{scraper.IS_TRULY_NEW} AS tracked FROM jobs WHERE " + where +
        f" ORDER BY {scraper.EFFECTIVE_DATE} IS NULL,"
        f" {scraper.EFFECTIVE_DATE} DESC,"
        " is_student DESC, min_years IS NULL, min_years, company"
    ).fetchall()
    total_israel = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE is_open=1 AND in_israel=1").fetchone()[0]
    tracking = dict(conn.execute(
        "SELECT company, first_run FROM company_meta").fetchall())
    conn.close()

    if text:
        rows = [r for r in rows if scraper.title_matches(text, r["title"])]

    tagged = [(r, {k: f(r) for k, f in FACETS.items()}) for r in rows]

    def passes(vals: dict, skip: str | None = None) -> bool:
        for name, picked in chosen.items():
            if name == skip or not picked:
                continue
            if vals[name] not in picked:
                return False
        return True

    # Each facet is counted with its OWN selection ignored. Otherwise ticking
    # one company would zero every other company's count, and the picker would
    # look broken.
    # The company's own domain, for the tile logo. Taken from sources.yaml, not
    # from job URLs: those point at the ATS, so Aidoc would end up wearing
    # Greenhouse's logo.
    try:
        conf = scraper.load_config()
        sites = {s0["name"]: (s0.get("site") or "")
                 for s0 in conf.get("sources", [])}
    except Exception:                       # a broken YAML must not blank the UI
        sites = {}

    facets: dict[str, list[dict]] = {}
    for name in FACETS:
        counts: dict[str, int] = {}
        for _, vals in tagged:
            if passes(vals, skip=name):
                counts[vals[name]] = counts.get(vals[name], 0) + 1
        facets[name] = [{"name": k, "count": v}
                        for k, v in sorted(counts.items(),
                                           key=lambda kv: (-kv[1], kv[0]))]
        if name == "company":
            for entry in facets[name]:
                entry["site"] = sites.get(entry["name"], "")

    jobs = []
    for r, vals in tagged:
        if not passes(vals):
            continue
        d = dict(r)
        d.pop("first_seen", None)
        d["is_new"] = bool(r["tracked"]) and (r["first_seen"] or "")[:10] == \
            datetime.now(timezone.utc).date().isoformat()
        d["city"] = vals["city"]
        d["etype"] = vals["etype"]
        d["level"] = vals["level"]
        jobs.append(d)

    return {"jobs": jobs, "facets": facets,
            "companies": facets["company"],   # kept for older clients
            "shown": len(jobs), "total_israel": total_israel,
            "tracking_since": tracking}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Israel Job Finder</title>
<style>
 *{box-sizing:border-box}
 :root{
   --bg:#f5f6f8; --card:#fff; --line:#e6e6ea; --ink:#18181b; --dim:#71717a;
   --faint:#a1a1aa; --accent:#2563eb; --accent-soft:#eff6ff;
 }
 body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
      background:var(--bg);color:var(--ink)}
 header{background:var(--card);border-bottom:1px solid var(--line);
        padding:.9rem 1.5rem;position:sticky;top:0;z-index:30}
 h1{margin:0;font-size:1.1rem;letter-spacing:-.01em}
 .wrap{padding:1.25rem 1.5rem 3rem;max-width:1280px;margin:0 auto}
 .bar{display:flex;flex-wrap:wrap;gap:.55rem;align-items:center;margin-top:.7rem}
 .bar label{display:flex;align-items:center;gap:.3rem;font-size:13px;
            background:#f4f4f5;padding:.35rem .6rem;border-radius:6px;cursor:pointer}
 input[type=text],input[type=number],select{padding:.38rem .55rem;
   border:1px solid #d4d4d8;border-radius:7px;font-size:13px;font-family:inherit}
 input[type=text]{min-width:200px}
 button{padding:.42rem .9rem;border:0;border-radius:7px;background:var(--accent);
        color:#fff;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit}
 button:disabled{background:var(--faint);cursor:not-allowed}
 button.ghost{background:#fff;color:var(--ink);border:1px solid #d4d4d8;font-weight:500}
 .muted{color:var(--dim);font-size:13px}

 /* ---- company tiles ---- */
 .tiles{display:grid;gap:.85rem;margin-top:1rem;
        grid-template-columns:repeat(auto-fill,minmax(215px,1fr))}
 .tile{position:relative;background:var(--card);border:1px solid var(--line);
   border-radius:14px;padding:1rem 1.05rem 1.05rem;cursor:pointer;text-align:start;
   display:block;width:100%;font-family:inherit;color:inherit;
   box-shadow:0 1px 2px rgba(16,24,40,.04);
   transition:transform .13s ease, box-shadow .13s ease, border-color .13s ease}
 .tile:hover{transform:translateY(-2px);border-color:#c7d2fe;
   box-shadow:0 10px 24px rgba(16,24,40,.10)}
 .tile:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
 .tile .top{display:flex;align-items:center;gap:.6rem;margin-bottom:.85rem}
 .logo{width:34px;height:34px;border-radius:9px;flex:0 0 34px;object-fit:contain;
   background:#fff;border:1px solid var(--line);padding:3px}
 .mono{width:34px;height:34px;border-radius:9px;flex:0 0 34px;display:grid;
   place-items:center;font-weight:700;font-size:13px;color:#fff;letter-spacing:.02em}
 .tile .nm{font-size:13.5px;font-weight:650;line-height:1.25;overflow-wrap:anywhere}
 .tile .big{font-size:2rem;font-weight:700;line-height:1;
   font-variant-numeric:tabular-nums;letter-spacing:-.02em}
 .tile .unit{font-size:12px;color:var(--dim);margin-inline-start:.3rem;font-weight:500}
 .tags{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.6rem;min-height:20px}
 .tag{font-size:11px;padding:.13rem .45rem;border-radius:999px;
   background:#f4f4f5;color:var(--dim);white-space:nowrap}
 .tag.stu{background:#dcfce7;color:#166534}
 .tag.new{background:var(--accent);color:#fff}
 .arrow{position:absolute;inset-inline-end:.9rem;top:1.05rem;color:#d4d4d8;
   font-size:15px;transition:color .13s, transform .13s}
 .tile:hover .arrow{color:var(--accent);transform:translateX(2px)}

 /* ---- company detail ---- */
 .back{background:none;border:0;color:var(--accent);font-weight:600;font-size:13px;
   padding:.2rem 0;margin-top:1rem;cursor:pointer;display:inline-flex;
   align-items:center;gap:.35rem}
 .back:hover{text-decoration:underline}
 .chead{display:flex;align-items:center;gap:.85rem;background:var(--card);
   border:1px solid var(--line);border-radius:14px;padding:1rem 1.15rem;
   margin:.7rem 0 1rem;box-shadow:0 1px 2px rgba(16,24,40,.04)}
 .chead .logo,.chead .mono{width:44px;height:44px;flex-basis:44px;border-radius:11px;
   font-size:16px}
 .chead h2{margin:0;font-size:1.15rem;letter-spacing:-.01em}

 table{border-collapse:collapse;width:100%;background:var(--card);border-radius:12px;
       overflow:hidden;font-size:13.5px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
 th,td{padding:.6rem .8rem;text-align:left;border-bottom:1px solid #f2f2f4;
       vertical-align:top}
 th{background:#fafafa;font-size:11.5px;text-transform:uppercase;
    letter-spacing:.04em;color:var(--dim)}
 tr:last-child td{border-bottom:0}
 tr.student td{background:#f6fdf9}
 a{color:#1d4ed8;text-decoration:none} a:hover{text-decoration:underline}
 .badge{background:var(--accent);color:#fff;font-size:10px;padding:1px 5px;
        border-radius:3px;margin-left:.35rem;vertical-align:middle}
 .undated{color:var(--faint)}
 .seen{color:var(--dim);border-bottom:1px dotted var(--faint);cursor:help}
 .yrs{text-align:right;font-variant-numeric:tabular-nums;color:var(--dim)}

 /* ---- dropdowns ---- */
 .dd{position:relative;display:inline-block}
 .dd>summary{list-style:none;cursor:pointer;font-size:13px;background:#f4f4f5;
   border:1px solid #d4d4d8;border-radius:7px;padding:.38rem .7rem;user-select:none}
 .dd>summary::-webkit-details-marker{display:none}
 .dd>summary::after{content:' \\25BE';color:var(--dim)}
 .dd[open]>summary{background:#e4e4e7}
 .dd.active>summary{background:var(--accent-soft);border-color:var(--accent);
   color:#1d4ed8}
 .ddpanel{position:absolute;z-index:20;top:calc(100% + 4px);inset-inline-start:0;
   background:#fff;border:1px solid #d4d4d8;border-radius:10px;padding:.5rem;
   min-width:250px;max-height:340px;overflow:auto;
   box-shadow:0 12px 28px rgba(16,24,40,.14)}
 .ddactions{display:flex;gap:.4rem;margin-bottom:.4rem;padding-bottom:.4rem;
   border-bottom:1px solid #f0f0f1}
 .ddactions button{background:#fff;color:var(--ink);border:1px solid #d4d4d8;
   font-weight:500;padding:.25rem .55rem;font-size:12px}
 .ddrow{display:flex;align-items:center;gap:.45rem;padding:.3rem;border-radius:6px;
   cursor:pointer;font-size:13px;white-space:nowrap}
 .ddrow:hover{background:#f4f4f5}
 .ddrow .n{margin-inline-start:auto;color:var(--faint);font-size:12px;
   font-variant-numeric:tabular-nums}

 #log{background:#18181b;color:#d4d4d8;font-family:ui-monospace,monospace;
      font-size:12px;padding:.75rem 1rem;border-radius:10px;margin-top:.75rem;
      max-height:190px;overflow:auto;white-space:pre-wrap;display:none}
 .health{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.6rem}
 .chip{font-size:12px;padding:.2rem .55rem;border-radius:999px;background:#e4e4e7}
 .chip.ok{background:#dcfce7;color:#166534}
 .chip.bad{background:#fee2e2;color:#991b1b}
 .empty{padding:3rem 1.5rem;text-align:center;color:var(--dim);background:var(--card);
   border:1px solid var(--line);border-radius:14px}
 #busy{display:none;background:var(--card);border:1px solid var(--line);
   border-radius:14px;padding:2.4rem 1.5rem;text-align:center;
   box-shadow:0 1px 2px rgba(16,24,40,.05);margin-top:1rem}
 #busy.on{display:block}
 .spin{width:26px;height:26px;border:3px solid var(--line);
   border-top-color:var(--accent);border-radius:50%;margin:0 auto .8rem;
   animation:sp .8s linear infinite}
 @keyframes sp{to{transform:rotate(360deg)}}
 .hide{display:none !important}
</style></head><body>
<header>
  <h1>Israel Job Finder <span style="color:var(--faint);font-size:11px;font-weight:400">&nbsp;build __UIVER__</span></h1>
  <div class="bar">
    <button id="refresh">Refresh jobs</button>
    <select id="src"><option value="">All sources</option></select>
    <span id="status" class="muted"></span>
    <button class="ghost" id="toggleLog" type="button">Log</button>
    <label style="margin-inline-start:auto"><input type="checkbox" id="logos" checked> Logos</label>
  </div>
</header>
<div class="wrap">
  <div class="bar">
    <input type="text" id="q" placeholder="Search job title — any order, typo-tolerant">
    <details id="dd_city" class="dd"><summary>All locations</summary>
      <div class="ddpanel"><div class="ddactions">
        <button type="button" data-clear="city">Clear</button></div>
        <div class="list"></div></div></details>
    <details id="dd_etype" class="dd"><summary>All job types</summary>
      <div class="ddpanel"><div class="ddactions">
        <button type="button" data-clear="etype">Clear</button></div>
        <div class="list"></div></div></details>
    <details id="dd_dept" class="dd"><summary>All departments</summary>
      <div class="ddpanel"><div class="ddactions">
        <button type="button" data-clear="dept">Clear</button></div>
        <div class="list"></div></div></details>
    <details id="dd_level" class="dd"><summary>All levels</summary>
      <div class="ddpanel"><div class="ddactions">
        <button type="button" data-clear="level">Clear</button></div>
        <div class="list"></div></div></details>
    <label><input type="checkbox" id="students"> Students / entry-level</label>
    <label><input type="checkbox" id="part_time"> Part-time</label>
    <label><input type="checkbox" id="no_senior"> Hide senior roles</label>
    <label>Max years <input type="number" id="max_years" min="0" max="20"
           style="width:60px"></label>
    <label><input type="checkbox" id="strict_years"> Exclude unknown years</label>
    <label>Posted
      <select id="max_age">
        <option value="">any time</option>
        <option value="1">last 24h</option>
        <option value="7">last week</option>
        <option value="30">last month</option>
        <option value="90">last 3 months</option>
      </select></label>
    <label><input type="checkbox" id="strict_dates"> Exclude undated</label>
    <label><input type="checkbox" id="new_only"> New today</label>
    <button class="ghost" id="reset" type="button">Reset</button>
  </div>
  <div class="health" id="health"></div>
  <div id="log"></div>

  <div id="busy">
    <div class="spin"></div>
    <div id="busyText" style="font-weight:600">Refreshing…</div>
    <div class="muted" id="busySub">Jobs will reappear when the run finishes.</div>
  </div>

  <div id="live">
    <p class="muted" id="count" style="margin:.9rem 0 0"></p>
    <div id="grid"></div>
    <div id="detail" class="hide"></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const ctl = ['q','students','part_time','no_senior','max_years',
             'strict_years','max_age','strict_dates','new_only'];
const FACETS = ['city','etype','dept','level'];
const LABEL = {city:'locations', etype:'job types', dept:'departments',
               level:'levels'};
const sel = {};
FACETS.forEach(f => sel[f] = new Set());
let pollTimer = null, busy = false, open = null, data = null;

function params(){
  const p = new URLSearchParams();
  if ($('q').value.trim()) p.set('q', $('q').value.trim());
  FACETS.forEach(f => sel[f].forEach(v => p.append(f, v)));
  if ($('students').checked) p.set('students','1');
  if ($('part_time').checked) p.set('part_time','1');
  if ($('no_senior').checked) p.set('no_senior','1');
  if ($('max_age').value) p.set('max_age', $('max_age').value);
  if ($('strict_dates').checked) p.set('strict_dates','1');
  if ($('max_years').value !== '') p.set('max_years', $('max_years').value);
  if ($('strict_years').checked) p.set('strict_years','1');
  if ($('new_only').checked) p.set('new_only','1');
  return p;
}
function esc(s){ return String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// Deterministic colour per company, so a tile keeps its identity across runs.
function hue(name){
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.codePointAt(0)) % 360;
  return h;
}
function initials(name){
  const parts = name.replace(/[^\\p{L}\\p{N} ]/gu,' ').split(/\\s+/).filter(Boolean);
  return ((parts[0]||'?')[0] + (parts[1] ? parts[1][0] : '')).toUpperCase();
}
function logoHtml(name, site){
  const mono = `<span class="mono" style="background:hsl(${hue(name)} 55% 45%)">${
    esc(initials(name))}</span>`;
  if (!site || !$('logos').checked) return mono;
  // The favicon is a nice-to-have: if the lookup fails, swap in the monogram
  // rather than leaving a broken image on the tile.
  return `<img class="logo" alt="" loading="lazy"
    src="https://www.google.com/s2/favicons?domain=${encodeURIComponent(site)}&sz=64"
    onerror="this.outerHTML=this.dataset.fb" data-fb="${esc(mono)}">`;
}

function dateCell(j){
  if (!j.effective_date)
    return '<span class="undated" title="no published date, and this job was ' +
           'already listed when tracking started">&mdash;</span>';
  if (j.date_source === 'first_seen')
    return '<span class="seen" title="this source publishes no date — ' +
           'this is when the scraper first saw the job">~' +
           esc(j.effective_date) + '</span>';
  return esc(j.effective_date);
}

function renderGrid(){
  $('detail').classList.add('hide');
  $('grid').classList.remove('hide');
  const list = data.facets.company;
  if (!list.length){
    $('grid').innerHTML = '<div class="empty">No jobs match these filters.<br>' +
      'Try clearing filters, or press <b>Refresh jobs</b> if the database is empty.</div>';
    return;
  }
  const by = {};
  data.jobs.forEach(j => (by[j.company] = by[j.company] || []).push(j));
  $('grid').innerHTML = '<div class="tiles">' + list.map(c => {
    const jobs = by[c.name] || [];
    const stu = jobs.filter(j => j.is_student).length;
    const nw  = jobs.filter(j => j.is_new).length;
    return `<button class="tile" data-c="${esc(c.name)}">
      <span class="arrow">&rsaquo;</span>
      <span class="top">${logoHtml(c.name, c.site)}
        <span class="nm">${esc(c.name)}</span></span>
      <span class="big">${c.count}</span><span class="unit">${
        c.count === 1 ? 'job' : 'jobs'}</span>
      <span class="tags">
        ${stu ? `<span class="tag stu">${stu} student</span>` : ''}
        ${nw ? `<span class="tag new">${nw} new</span>` : ''}
      </span></button>`;
  }).join('') + '</div>';
  $('grid').querySelectorAll('.tile').forEach(t =>
    t.onclick = () => { open = t.dataset.c; render(); });
}

function renderCompany(){
  $('grid').classList.add('hide');
  $('detail').classList.remove('hide');
  const jobs = data.jobs.filter(j => j.company === open);
  const meta = data.facets.company.find(c => c.name === open) || {};
  if (!jobs.length){
    // Every job was filtered away while the company view was open.
    $('detail').innerHTML =
      `<button class="back" id="back">&larr; All companies</button>
       <div class="empty">${esc(open)} has no jobs matching these filters.</div>`;
    $('back').onclick = () => { open = null; render(); };
    return;
  }
  $('detail').innerHTML =
    `<button class="back" id="back">&larr; All companies</button>
     <div class="chead">${logoHtml(open, meta.site)}
       <div><h2>${esc(open)}</h2>
         <div class="muted">${jobs.length} ${jobs.length===1?'job':'jobs'}
           matching your filters</div></div></div>
     <table><thead><tr><th>Title</th><th>Location</th><th>Posted</th>
       <th>Type</th><th>Student</th><th>PT</th><th>Yrs</th></tr></thead><tbody>` +
    jobs.map(j => `<tr class="${j.is_student?'student':''}">
      <td><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>
        ${j.is_new?'<span class="badge">NEW</span>':''}</td>
      <td>${esc(j.location)}</td><td>${dateCell(j)}</td>
      <td>${esc(j.etype === 'Unspecified' ? '' : j.etype)}</td>
      <td>${j.is_student?'★':''}</td><td>${j.is_part_time?'½':''}</td>
      <td class="yrs">${j.min_years==null?'—':j.min_years+'y'}</td></tr>`).join('') +
    '</tbody></table>';
  $('back').onclick = () => { open = null; render(); };
}

function renderFacet(name, list){
  const box = $('dd_' + name);
  const panel = box.querySelector('.list');
  panel.innerHTML = list.map(v => `
    <label class="ddrow"><input type="checkbox" value="${esc(v.name)}"
      ${sel[name].has(v.name)?'checked':''}>
      <span>${esc(v.name)}</span><span class="n">${v.count}</span></label>`).join('')
    || '<div class="muted" style="padding:.3rem">nothing to filter</div>';
  panel.querySelectorAll('input').forEach(cb => {
    cb.onchange = () => {
      cb.checked ? sel[name].add(cb.value) : sel[name].delete(cb.value);
      load();
    };
  });
  const n = sel[name].size;
  box.querySelector('summary').textContent =
    n === 0 ? 'All ' + LABEL[name]
    : n === 1 ? [...sel[name]][0]
    : n + ' ' + LABEL[name];
  box.classList.toggle('active', n > 0);
}

function render(){
  FACETS.forEach(f => renderFacet(f, data.facets[f]));
  // In the company view the header already states the count; repeating the
  // global one above it just reads as a contradiction.
  $('count').classList.toggle('hide', open !== null);
  $('count').textContent =
    `${data.facets.company.length} companies · ${data.shown} jobs shown of ` +
    `${data.total_israel} open in Israel`;
  // A company that filtered down to nothing keeps its view, with an explanation,
  // rather than bouncing you back to the grid mid-typing.
  (open === null) ? renderGrid() : renderCompany();
}

async function load(){
  if (busy) return;
  const r = await fetch('/api/jobs?' + params().toString(), {cache:'no-store'});
  data = await r.json();
  render();
}

function setBusy(on, note){
  busy = on;
  $('live').classList.toggle('hide', on);
  $('busy').classList.toggle('on', on);
  if (on) $('busySub').textContent = note || 'Jobs will reappear when the run finishes.';
}

async function poll(){
  const s = await (await fetch('/api/status', {cache:'no-store'})).json();
  $('refresh').disabled = s.running;
  $('status').textContent = s.running ? 'refreshing...'
    : (s.finished_at ? 'last run ' + s.finished_at.replace('T',' ') + 'Z' : '');
  $('log').textContent = (s.log||[]).join('\\n') + (s.error ? '\\n\\n' + s.error : '');
  $('health').innerHTML = (s.health||[]).map(([n,st,c]) =>
    `<span class="chip ${st==='ok'?'ok':'bad'}">${esc(n)}: ${c}${
      st==='ok'?'':' — '+esc(st)}</span>`).join('');
  if (s.running){
    setBusy(true, (s.log||[]).slice(-1)[0] || 'Starting…');
  } else if (busy){
    setBusy(false);
    load();
  }
  if (!s.running && pollTimer){ clearInterval(pollTimer); pollTimer = null; }
}

$('refresh').onclick = async () => {
  $('refresh').disabled = true;
  setBusy(true, 'Starting…');
  await fetch('/api/refresh?source=' + encodeURIComponent($('src').value),
              {method:'POST'});
  if (!pollTimer) pollTimer = setInterval(poll, 1000);
  poll();
};
$('toggleLog').onclick = () => {
  $('log').style.display = $('log').style.display === 'none' ? 'block' : 'none';
};
$('logos').onchange = () => {
  try { localStorage.setItem('logos', $('logos').checked ? '1' : '0'); } catch (e) {}
  render();
};
try { if (localStorage.getItem('logos') === '0') $('logos').checked = false; } catch (e) {}

document.querySelectorAll('[data-clear]').forEach(b => {
  b.onclick = () => { sel[b.dataset.clear].clear(); load(); };
});
document.addEventListener('click', e => {
  document.querySelectorAll('details.dd[open]').forEach(d => {
    if (!d.contains(e.target)) d.open = false;
  });
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && open !== null){ open = null; render(); }
});

$('reset').onclick = () => {
  ctl.forEach(id => { const e=$(id);
    if (e.type === 'checkbox') e.checked = false; else e.value = ''; });
  FACETS.forEach(f => sel[f].clear());
  load();
};
ctl.forEach(id => {
  const e = $(id);
  e.addEventListener(e.tagName === 'INPUT' && e.type === 'text' ? 'input' : 'change',
                     () => { clearTimeout(window._t);
                             window._t = setTimeout(load, 200); });
});

fetch('/api/sources').then(r=>r.json()).then(d=>{
  $('src').innerHTML = '<option value="">All sources</option>' +
    d.map(n => `<option>${esc(n)}</option>`).join('');
});
poll(); load();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The UI changes often. Caching it produced a page missing filters that
        # the server was actually serving, which is very confusing to debug.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: object, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            self._send(200, PAGE.replace('__UIVER__', UI_VERSION).encode("utf-8"), "text/html; charset=utf-8")
        elif u.path == "/api/jobs":
            try:
                self._json(query_jobs(p))
            except Exception as e:  # noqa: BLE001
                self._json({"error": str(e), "jobs": [], "companies": [],
                            "shown": 0, "total_israel": 0}, 500)
        elif u.path == "/api/status":
            self._json({**_snapshot(), "ui_version": UI_VERSION})
        elif u.path == "/api/sources":
            cfg = scraper.load_config()
            self._json([s["name"] for s in cfg.get("sources", [])
                        if s.get("enabled", True)])
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/refresh":
            src = urllib.parse.parse_qs(u.query).get("source", [""])[0] or None
            self._json({"started": start_refresh(src)})
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a) -> None:
        pass  # keep the console clean; the UI shows progress


def main() -> None:
    scraper.open_db().close()   # create/migrate before first request
    url = f"http://localhost:{PORT}"
    print(f"Job finder running at {url}")
    print(f"UI build {UI_VERSION}")
    print("filters: search, company, students, part-time, hide-senior, "
          "max-years, posted-date, new-today")
    print("Press Ctrl+C to stop.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
