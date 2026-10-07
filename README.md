# Israel Student Job Scraper

Pulls jobs from open APIs that need **no credentials and no approval**, filters for
Israel and student/entry-level roles, deduplicates, and tracks what's new.

## Setup

```bash
pip install pyyaml
```

That's the only dependency. Everything else is Python standard library.

## Run — web interface (recommended)

```bash
python webapp.py
```

Opens `http://localhost:8765` in your browser automatically. From there you can:

- **Refresh jobs** — runs the scrape with a live log and a per-source health strip
- Refresh **one source only**, using the dropdown next to the button
- Filter as you type: search text, **companies (pick any number)**, students,
  part-time, hide senior roles, max years, posted date, new today

### Companies first, jobs second

The landing view is a grid of company cards — logo, name, and the number of
jobs that company has **under the current filters** — and nothing else. There
is no flat list of every job. Click a card and it opens that company's jobs;
*← All companies* or `Esc` goes back.

Each card also carries small tags for what is inside it: how many student roles,
how many are new. That is usually enough to decide whether a card is worth
opening.

A company whose count falls to zero drops out of the grid, so the grid always
shows exactly the companies that have something for you. Filtering while a
company is open keeps you in that company's view, with a note if nothing is
left, rather than throwing you back to the grid mid-keystroke.

**Logos** come from each company's own domain, declared as `site:` in
`sources.yaml`. They are fetched through Google's favicon service, which means
the browser tells Google which company domains you are looking at — the
**Logos** switch in the header turns that off, and the cards fall back to
coloured monograms. The monogram is also the fallback whenever a favicon fails
to load, so a card never shows a broken image.

The domain is declared rather than guessed from job URLs, because those point
at the ATS: Aidoc's jobs live on `greenhouse.io`, so guessing would put
Greenhouse's logo on Aidoc's card.

### Multi-select filters

Four faceted pickers sit next to the search box, each multi-select, each
showing counts. They narrow which jobs count, which in turn changes every
company card's number:

| Filter | Built from |
|---|---|
| Location | `location`, bucketed into cities |
| Job type | `employment_type`, plus the student flag |
| Department | `department` as the source reports it |
| Level | `experience_level`, plus the student and senior flags |

Locations arrive as free text and differ per source — `Tel Aviv-Yafo, Israel`,
`Petach Tikva, Israel`, `Haifa, Israel, R&D`. They are bucketed into real
cities, with `Israel (other)` for rows that say only the country and `Remote`
for remote-only listings. Job type and level are bucketed the same way, because
otherwise thirty spellings of "Full-time" would each get their own checkbox.

Every facet's counts are computed with **its own** selection ignored and all
the other filters applied, so the numbers always tell you what you would get by
ticking one more box. Without that rule, ticking one value would zero out every
other value in the same picker and it would look broken.

### Refreshing hides the jobs

While a refresh runs, the company grid is replaced by a progress panel naming
the source being fetched. Nothing from the previous run stays on screen. The
cards come back, re-queried, when the run finishes.

**Search box** — matches against the **job title** only.

- **Each word is matched separately**, in any order. `student data` and
  `data student` both find *Data Science Student for AI Solutions Group*.
- **All words must match.** `student manager` finds nothing if no title has both.
- **Case is ignored** — `STUDENT`, `Student` and `student` are identical.
- **Typo-tolerant at 90% similarity.** `enginer` finds *Backend Engineer*.
- **Partial words work.** `engin` finds *Engineer* and *Engineering*.
- **Hebrew is handled properly:**
  - Final letters are normalised, so `תוכניתן` matches `תוכניתנ`
  - `סטודנטים` finds `סטודנט`
  - Inclusive forms split on the dot, so `סטודנט` matches `סטודנט.ית`

Change the tolerance by editing `FUZZY_THRESHOLD` in `scraper.py` (0.9 = 90%).
Lower it for more forgiving matching, raise it toward 1.0 for stricter.

Matching runs in Python rather than SQL, because SQLite cannot express
similarity. It executes over the already-filtered rows, so it stays instant.

**Company picker** — click *All companies* to open a checkbox list. Tick as many
as you like; the button then reads e.g. *3 companies*. Each row shows how many
jobs that company currently has under your other filters, and those counts
deliberately ignore the company selection itself, so ticking one company doesn't
blank out the others. *Select all* and *Clear* are at the top.
- Click any title to open the real posting

Everything runs on your machine. Nothing is uploaded, no account, no server.
Press Ctrl+C in the terminal to stop it.

## Run — command line

```bash
python scraper.py                          # everything
python scraper.py --students-only          # student / intern / entry-level only
python scraper.py --part-time              # part-time roles only
python scraper.py --max-age-days 7         # posted in the last week
python scraper.py --no-senior              # drop Senior/Sr./Staff/Principal/Lead
python scraper.py --max-years 2            # asks for 2 years experience or less
python scraper.py --students-only --part-time --max-years 1   # combine freely
python scraper.py --source Intel           # one source, for debugging
python scraper.py --selftest               # offline tests, no network
```

### Why the date filter appeared to ignore real dates

Rows saved before `posted_date` existed kept it empty, while the table still
showed a date — it fell back to the raw `posted_at` string. So a job displaying
`2026-03-23` was stored as *undated*, and the "keep undated" rule let it through
a "last week" filter. The column said one thing, the filter saw another.

Two fixes:

1. **Automatic backfill.** Opening the database now converts any legacy
   `posted_at` into a proper `posted_date`. It runs once, needs no re-scrape,
   and leaves genuinely undated rows empty. Re-scraping alone would not have
   fixed this, because refreshing one source never touches the others.
2. **No more display fallback.** The Posted column shows a date only when the
   filter can actually see one. Otherwise it shows `—`. What you read is what
   gets filtered.

### Dates for sources that publish none

Several sources publish no date at all — Google, Mobileye, Rafael, Palo Alto
Networks, and most of the Comeet boards. Those rows used to be permanently
undated, which made "posted this week" much less useful than it sounds.

The scraper now remembers when it first saw each job and uses that as a
fallback date. The catch is the obvious one: **the first time a company is
scraped, everything on its board arrives at once**, and none of it was posted
that day. Dating a 200-job backlog "today" would be worse than leaving it
undated.

So a `company_meta` table records the timestamp of each company's first
successful run, and the effective date is:

1. the published date, when the source gives one;
2. otherwise `first_seen`, **but only for jobs that appeared after that first
   run** — those genuinely showed up while we were watching;
3. otherwise nothing. Still undated, still kept by default.

Every row inserted in a run shares one timestamp, so `first_seen > first_run`
separates the opening backlog from later arrivals exactly, with no fuzzy
window. Databases that predate this feature are backfilled from the earliest
`first_seen` per company, which errs the safe way: the old backlog stays
undated rather than being dated the day you installed the scraper.

The same rule fixes the **NEW** badge. "New" now means new to us, so adding a
company no longer paints its entire board as new today.

In the table, a fallback date is shown as `~2026-08-23` with a tooltip saying
it is when the scraper first saw the job. A real published date has no tilde.
The distinction matters: the app should never imply a company published a date
it never published. `jobs.csv` carries both `effective_date` and
`date_source` (`published` / `first_seen` / empty) for the same reason.

### About the posted-date filter

Sources report dates in four incompatible ways, so they are normalised to
ISO `yyyy-mm-dd` before storage:

| Source | Raw form | Stored |
|---|---|---|
| Elbit | `openDate` `2026-08-13T01:16:00` | exact publish date |
| Greenhouse | `updated_at` | ⚠️ last-updated, not first-published |
| Apple | `Aug 06, 2026` | exact |
| Eightfold (Microsoft) | `postedTs` epoch | exact |
| Workday (Intel, NVIDIA) | `Today`, `3 Days Ago` | computed from today |
| Workday | `30+ Days Ago` | approximate — treated as exactly 30 |
| **Rafael** | none — API has no date field | `—` |
| **Google, Mobileye, Palo Alto Networks** | none published | `—` |

Rafael, Google, Mobileye and Palo Alto Networks genuinely publish no date, so
they show `—` and are
kept by default. Tick **Exclude undated** to see only jobs with a date the
filter can verify.

**Undated jobs are kept by default**, same rule as `--max-years`. Google and
Eightfold publish no date at all, so excluding them would hide those sources
entirely without saying so. Tick *Exclude undated* (or pass `--strict-dates`)
when you want only jobs with a confirmed date.

Results are now sorted newest first.

### About "hide senior roles"

Matches the title **prefix only**, not the whole string. `Senior Software
Engineer` is dropped; `Intern, Senior Design Team` is kept — matching anywhere
would wrongly discard the second one.

Prefixes live in `senior_title_prefixes` in `sources.yaml`: senior, sr, sr.,
staff, principal, lead, head, director, vp, chief, בכיר. Edit the list freely —
if you want to keep team-lead roles, delete `lead`.

### About `--max-years`

Years of experience are read out of the posting text with patterns like
"5 years of experience", "5+ years", and "3 שנות ניסיון". When a posting lists
several bars ("2 years of X, 8 years of Y") the **lowest** is used, because
minimum-qualification lists state the real entry point first.

**Not every source publishes requirements text.** Greenhouse and Google do.
Workday and Eightfold return listing data only, so their years are unknown.
Unknown is stored as `NULL`, never as `0`, and `--max-years` **keeps unknown
rows by default** — otherwise the filter would silently discard most of the
board. Add `--strict-years` to drop them instead.

In the HTML report the Yrs column shows `—` for unknown. That means "not
published", not "none required".

## Output

| File | What it is |
|---|---|
| `report.html` | Open in a browser. Student roles highlighted green, jobs found today tagged NEW. Source health table at the top. |
| `jobs.csv` | Same data as a spreadsheet. UTF-8 BOM so Hebrew opens correctly in Excel. |
| `jobs.db` | SQLite history. Keeps `first_seen` / `last_seen` so "new" means genuinely new. |

## Sources included

| Company | Type | Verified |
|---|---|---|
| Wiz, Taboola, JFrog, MyHeritage | Greenhouse | ✅ 255 jobs fetched |
| Intel | Workday, student facet | ✅ 26 jobs |
| NVIDIA | Workday, new-grad + intern facets | ✅ 74 jobs |
| Microsoft | Eightfold (`pcsx`) | ✅ 12 jobs |
| Amdocs | Eightfold (`pcsx`) | ⚠️ low yield — see below |
| Google | server-rendered HTML | ✅ fixed, see below |
| Apple | server-rendered HTML | ✅ Herzliya roles confirmed |
| Mobileye | server-rendered HTML | ✅ 141 jobs on one page |
| **רפאל** | public JSON API | ✅ 315 jobs, **59 student** |
| **אלביט מערכות** | static JSON feed | ✅ 635 jobs, **~41 student** |
| **Palo Alto Networks** | Radancy, server-rendered HTML | ✅ 147 in Israel, 15/page |
| **UVeye, Percepto, WSC Sports** | Comeet | ✅ token read from careers page |
| **Nexar** | Teamtailor, server-rendered HTML | ✅ small board |
| **Arbe, Imagry** | Comeet plugin, no token needed | ✅ 10 jobs each |
| **Trigo** | bespoke WordPress theme | ✅ 4 jobs |
| **Hailo** | bespoke career site | ⚠️ 1 catch-all posting — see below |
| Innoviz | — | ❌ Imperva bot protection |
| **Aidoc** | Greenhouse | ✅ 30 jobs, ~23 Tel Aviv |
| **Lightricks** | Greenhouse | ✅ 6 jobs, 4 Jerusalem |
| **General Motors** | Workday (whole board) | ⚠️ slow — see below |
| **Medtronic** | Workday, Israel country facet | ✅ 34 in Israel, 2 requests |
| Philips | Workday | ⛔ added but disabled — see below |
| **Kela Technologies** | Comeet, token from the hosted board | ✅ 37 jobs, 29 Tel Aviv |
| **Forsight Robotics** | Comeet, token from the hosted board | ✅ 3 jobs, Caesarea |
| **Magnus Metal** | bespoke Elementor page | ✅ 6 jobs |
| **Innoviz, Nanox, Samsung R&D Israel** | Comeet, token from the hosted board | ✅ 2 / 6 / 18 jobs |
| **Foretellix** | Comeet, token from the hosted board | ⚠️ board empty on 2026-10-07 |
| **Autobrains, Lumus, Mentee Robotics, Healthy.io** | Comeet, token from the hosted board | ✅ 5 / 3 / 8 / 2 jobs |
| **Rail Vision, Airobotics** | Comeet plugin, no token needed | ✅ 5 / 19 jobs |
| **Viz.ai** | Ashby | ✅ 18 jobs, 7 Tel Aviv |
| **KLA** | Workday, Israel country facet | ✅ 75 in Israel |

### The computer-vision sweep (2026-08-23)

Companies whose product *is* vision — automotive perception, medical imaging,
video analytics — were checked one by one against Greenhouse, Lever, Ashby and
SmartRecruiters. The result is worth stating plainly, because it explains why so
few were added:

**Almost the entire Israeli computer-vision mid-market runs on Comeet.** Hailo,
Innoviz, Arbe, Nexar, Foretellix, Imagry, UVeye, Trigo, Percepto, WSC Sports,
Nanox, Samsung R&D Israel and Sony Semiconductor Israel are all there. Their
hosted boards at `comeet.com/jobs/{slug}/{uid}` are Angular shells — the markup
contains `{{ position.name }}`, not job titles — and three token-less endpoint
shapes all returned HTTP 400.

Vision work also happens **inside companies large enough to run a real ATS**:
Aidoc and Lightricks on Greenhouse, GM's Herzliya perception centre on Workday,
and — already present — Mobileye, Intel, NVIDIA, Apple and Google.

## The Comeet token

The token turned out not to be a barrier at all, and definitely not a secret.
Comeet's own [widget documentation](https://developers.comeet.com/reference/embedding-widgets)
tells companies to put it in the `<head>` of their careers site:

```html
<script>
   window.comeetInit = function() {
      COMEET.init({
         "token":       "-- your token --",
         "company-uid": "-- your company uid ---",
         ...
```

So it is sitting in the page source of every company that embeds the widget.
`--probe` now reads it out:

```bash
python scraper.py --probe https://hailo.ai/careers/
```

```
ATS signatures found:
  comeet           via comeet.co

Comeet credentials in the page source:
  company uid : D5.007
  token       : ...

Paste into sources.yaml:

  - name: COMPANY
    type: comeet
    uid: "D5.007"
    token: "..."
    enabled: true
```

Both forms are recognised — the `COMEET.init({...})` call and a direct
`careers-api/2.0/company/{uid}/positions?token=...` URL.

**Probe the company's own domain first.** If it only links out to
`comeet.com/jobs/{slug}/{uid}` instead of embedding the widget, the token is
still public: the hosted board's page source carries it in
`COMPANY_DATA = {... "token": "..."}`. Kela Technologies and Forsight Robotics
were added that way on 2026-10-07. `--probe` does not read that form yet, and
it misreports the hosted board as Imperva-blocked — open the saved
`probe_www.comeet.com.html` and search for `"token"`.

The connector requests `details=true`, so Comeet returns the Description and
Requirements text. That makes it one of the few sources where `--max-years`
actually works — better than Workday, Eightfold or SmartRecruiters, which
return listing data only. Closed and internal-only positions are dropped.

Every UID found during the sweep is already in the `blocked:` section of
`sources.yaml`. Each row needs one probe and a move up into `sources:`.

### First probe run — 9 companies, 3 tokens

| Company | Result |
|---|---|
| UVeye | ✅ Comeet, uid `92.00F`, token read |
| Percepto | ✅ Comeet, uid `44.000`, token read |
| WSC Sports | ✅ Comeet, uid `93.007`, token read |
| Nexar | ⚠️ not Comeet at all — **Teamtailor**, on `careers.getnexar.com` |
| Hailo, Arbe, Trigo, Imagry | ❓ no ATS signature, jobs still render |
| Innoviz | ❌ 212 bytes returned — a stub, not the page |

All three UIDs match what public search had already turned up, which is a
useful cross-check that the extractor reads the right `"token"` field and not
some analytics key sitting nearby in the same page.

The four "no signature" companies are the interesting ones: their careers pages
show jobs but contain none of the strings any known ATS leaves behind. Either
they render from their own backend, or they load an ATS this list has not met
yet. `--probe` now writes `probe_{host}.html` instead of a single `probe.html`,
so a loop over nine companies leaves nine files to compare rather than one.
It also warns when a page comes back implausibly small, which is what Innoviz
did.

### Second probe run — the four "no signature" companies

All four turned out to be findable; the signature list was simply looking for
the wrong strings.

| Company | What it actually is |
|---|---|
| Arbe, Imagry | Comeet **rendered into the page** by its WordPress plugin |
| Hailo | custom theme backed by Comeet data, no plugin markup at all |
| Trigo | bespoke WordPress theme; links carry Comeet position UIDs |
| Innoviz | Imperva/Incapsula — an 840-byte challenge stub, not a page |

**The Comeet plugin case needs no token at all.** When a company embeds the
plugin rather than linking out, the jobs are already in the HTML:

```html
<div class="comeet-g-r Engineering">
  <div class="comeet-list comeet-group-name">Engineering</div>
  <ul class="comeet-positions-list"><li>
    <a class="comeet-position" href="//site/career/co/.../33.D67/title/all">
      <div class="comeet-position-name">DSP Software Team Leader</div>
      <div class="comeet-position-meta">Tel Aviv &middot; Full-time</div>
```

`type: comeet_embed` takes just the careers page URL. `--probe` now recognises
this markup and prints that YAML instead of hunting for a token.

Two details the markup forces:

- The plugin leaves a **commented-out copy of the title** inside the name div.
  Stripping tags without removing comments first prints every title twice.
- Companies group **either by department or by location**, and nothing says
  which. Arbe groups by department with the city in the meta line; Imagry
  groups by location with the department there instead. So everything that is
  not an employment type or a seniority level goes into `location`. The Israel
  filter only needs to see the place, and a slightly noisy location beats a
  confidently wrong one. Both layouts are pinned in `--selftest`.

### Hailo — the connector was never the problem

`/careers/` and `/company-overview/careers/` return the same 148 KB page, and
it contains no Comeet markup whatsoever. What it does contain is Comeet *data*
under a custom theme: the job URL ends in a Comeet position UID (`07.432`) and
the location filter buttons carry Comeet location UIDs (`7E.109`).

The page is server-rendered with client-side pagination, so one fetch is the
whole board. Its one trap is that the location div wraps an icon div, so a lazy
match to the first `</div>` returns the icon and no location — the fixture in
`--selftest` pins that.

**And the board holds exactly one posting: "Hailo has amazing openings!"** —
a catch-all, department *Other*. Their Comeet-hosted board agrees: no open
positions. So the two empty probe runs were not a parsing failure at all.
Nothing is being missed; Hailo is simply not listing individual roles right
now. The connector is on so they appear the moment they do.

**Innoviz is a genuine dead end.** `--probe` now names the WAF instead of
shrugging — "no ATS signature found" on an 840-byte body is a different problem
from a page that has no ATS, and the two should not read the same.

### Teamtailor

Nexar was mapped as Comeet from search results and turned out to be on
Teamtailor — a reminder that a public job URL says where a job is *listed*, not
which ATS is *live*. Teamtailor has no key-free API, but the career site is
rendered server-side, so `{base}/jobs` is a plain fetch.

The Tailwind class names on those pages are generated and could change, so the
parser hangs off the stable part — the `/jobs/{id}-{slug}` URL shape — and
identifies the location by the country code in brackets (`Tel Aviv-Yafo (IL)`)
rather than by position, so a missing department cannot shift the columns.

### Palo Alto Networks — Radancy, not SmartRecruiters

Their SmartRecruiters board (`careers.smartrecruiters.com/PaloAltoNetworks2`)
still appears in search results but now 302s to the SmartRecruiters homepage.
It is dead. The live site is `jobs.paloaltonetworks.com`, which runs Radancy
(TalentBrew) and renders results server-side.

Pagination is in the URL *path*, not a query parameter — `?p=2` is silently
ignored, which is an easy way to scrape page 1 ten times and think you have 147
jobs. The working form is:

```
/en/location/israel-jobs/47263/294640/2/{page}
```

15 jobs per page, 10 pages, filtered to Israel at source. The `radancy`
connector is generic: `base` + a `path` containing `{page}` is all another
Radancy company needs.

Dates come back as US-style `mm/dd/yyyy`, so `normalize_date` learned that
format. It is checked **last**, after every unambiguous form, so it can never
reinterpret an ISO date.

**It is not bot-protected.** The first live run answered in 0.8 seconds and
returned all 15 cards on page 1. What looked like a hang was the 60-second
default timeout with nothing printed in between.

**The first parse was wrong, though.** Locations came back as
`IT Internal Audit Manager Petach` — the title bleeding into the location
column. Two causes, both fixed:

1. The card location is `class="section29__result-location"`, not
   `job-location`, so the location pattern never matched and a loose
   "City, Country" fallback scraped whatever text came first — the title.
2. Each card's text window ran a flat 1500 characters, which reached into the
   next card. Windows now end at the next job link.

The tenant prefix (`section29__`) is per-site, so every pattern keys off the
stable suffix and both spellings of the location class are accepted. Category
is now read too, and lands in the department column. `--selftest` carries the
real markup as a fixture, including a second card whose only job is to fail if
the bleed ever returns.

**No dates.** These cards publish no posted date anywhere, so Palo Alto Networks
joins Google, Mobileye and Rafael in the `—` column. Ticking *Exclude undated*
hides it entirely.

**If it does stall.** Radancy sits behind bot protection on some tenants. A stalled
request looks identical to a slow one until the 60-second timeout fires, so use
`--timeout` to force the answer out quickly:

```bash
python scraper.py --timeout 15 --debug "Palo Alto Networks"
```

`--debug` now warms the host up first (it previously did not, so it could fail
where a full run succeeded) and prints how long each step took. A timeout with
no HTTP status means the server took the connection and never answered — that
is protection stalling a non-browser client. A 403 means it refused outright.
Either way, open the same URL in a browser: if it loads there and not here, the
site needs a real browser and this connector cannot reach it.

`robots.txt` on that host disallows `/search-jobs/` but not `/location/`, which
is the path this connector uses.

### General Motors, Medtronic and Philips — the size problem

GM's Herzliya site is an autonomous-driving and perception centre and posts
roles like *Computer Vision Algorithms Engineer*. No student facet ID is known
for that tenant, so the whole board is pulled at 20 rows per request with a
one-second delay — roughly 100 requests, making it the slowest source here. It
is enabled because the Israeli roles are worth it.

Medtronic (Mazor surgical navigation, Caesarea) and Philips (medical imaging
R&D, Haifa) have the same cost with a much thinner Israeli payoff, so both are
in `sources.yaml` **disabled**. Flip `enabled: true` if you want them, or set
`search_text` to narrow the board server-side first — Workday searches posting
text rather than location, so verify with `--debug` that it does not return zero.

### Israeli defence companies

Both turned out to have clean, open JSON — better than most of the tech companies.

**Rafael** — `GET https://career.rafael.co.il/api/jobs` returns
`{status, code, data:[...]}`. Each row is `{id, tl (title), ar [area ids],
jt [job type ids], pr [profession ids]}`.

The valuable part: **job type `2` means "משרת סטודנט"** — an explicit,
company-maintained student flag. That beats keyword guessing, which is what
every other source relies on. 59 of 315 jobs carry it. Area IDs map to site
names via the `areas:` table in `sources.yaml`; all 13 sites are in Israel.

**Elbit** — `GET https://elbitsystemscareer.com/cron/jobs.json` is a static file
their own cron refreshes. 635 rows with `jobTitle`, `area`, `employerName`
(which subsidiary), `openDate`, plus full `description` and `requirements`.
Because the requirements text is included, **years-of-experience parsing works
for Elbit** — unlike Workday and Eightfold, which return listing data only.
Rows with `status != 1` are filtered out as closed.

One limitation: no public deep-link pattern was found for individual Elbit
jobs, so links point at the listing page and the job code is stored in the
department column so you can find the role.

### IAI — mapped but not connected

512 jobs at `jobs.iai.co.il`. Same WordPress theme vendor as Rafael, but the
listings load through `admin-ajax` rather than a clean endpoint, and the site
runs Akamai bot protection that will very likely refuse a plain Python request.
Recorded in the `blocked:` section of `sources.yaml`. Getting it working needs
the `admin-ajax` action name and probably a session cookie.

### Apple and Mobileye

Neither exposes a job API, but both render their results server-side, so a plain
fetch works — no browser automation needed.

**Apple** — cards are `li.rc-accordion-item`, title in `<h3>`, location after the
word "Location". Filtered at source with `location=israel-ISR`.

**Mobileye** — every job sits on one page in `div.jobItem`, so there is no
pagination. Card text runs *department / title / location / type / description*.
There is no location filter, so the Israel classifier separates Ramat Gan from
their Beijing and other overseas roles.

### Fixes applied after the first live run

**Google returned 0.** Its hrefs are *relative with no leading slash*
(`href="jobs/results/123-title"`), so the original pattern matched nothing. The
parser now splits on the `lLd3Je` card class, reads the title from `<h3>`, and
pulls location and minimum-qualifications text from each card. A fixture in
`--selftest` reproduces the original bug so it can't come back silently.

**Amdocs returned 403.** The `/api/apply/v2/jobs` endpoint is now blocked on that
tenant, exactly like Microsoft's. Switched to the `pcsx` dialect, which responds.
Its location filter returns 0 for every value tested, so location filtering is
disabled for this source and the Israel classifier handles it instead. Expect
only a handful of jobs — this tenant exposes very little publicly.

## Adding a company

Edit `sources.yaml`. No code changes needed.

**Greenhouse** — open the company's careers page, find `job-boards.greenhouse.io/<token>`:

```yaml
- name: SomeCompany
  type: greenhouse
  token: somecompany
  enabled: true
```

**Workday** — the careers URL is `https://{tenant}.{host}.myworkdayjobs.com/{site}`:

```yaml
- name: SomeCompany
  type: workday
  host: wd3
  tenant: somecompany
  site: External
  student_facets: {}     # omit to pull everything and filter by keyword
  enabled: true
```

To find a company's student facet ID, POST to its `/jobs` endpoint with an empty
body and read the `facets` array — look under `workerSubType`.

Optional: `search_text: Israel` narrows a very large tenant server-side.

**Comeet** — run `--probe` on the company's careers page and paste what it
prints:

```yaml
- name: SomeCompany
  type: comeet
  uid: "D5.007"
  token: "..."
  enabled: true
```

**Comeet, embedded** — when `--probe` reports the plugin markup, only the
careers page URL is needed:

```yaml
- name: SomeCompany
  type: comeet_embed
  url: https://somecompany.com/careers/
  enabled: true
```

**Teamtailor** — `base` is the career site root:

```yaml
- name: SomeCompany
  type: teamtailor
  base: https://careers.somecompany.com
  max_pages: 10
  enabled: true
```

**Lever** — token is the path segment on `jobs.lever.co/<token>`:

```yaml
- name: SomeCompany
  type: lever
  token: somecompany
  enabled: true
```

Lever is the most generous of the four: the listing includes the full
description *and* the requirements lists, so `--max-years` works on it.

**Ashby** — token is the path segment on `jobs.ashbyhq.com/<token>`:

```yaml
- name: SomeCompany
  type: ashby
  token: somecompany
  enabled: true
```

One request returns the whole board. Postings flagged `isListed: false` are
reachable by direct link only and are dropped.

**SmartRecruiters** — token is the path segment on
`jobs.smartrecruiters.com/<token>`:

```yaml
- name: SomeCompany
  type: smartrecruiters
  token: SomeCompany
  country: il          # optional, defaults to il
  enabled: true
```

The API reports countries as ISO-2 codes (`il`), which no `israel_term` would
ever match, so the connector spells `il` out as `Israel` before the classifier
sees it. If the country filter returns nothing the connector retries unfiltered
rather than reporting an empty board — a filter that matches nothing and a
company with no Israeli roles look identical otherwise.

**Radancy / TalentBrew** — career sites like `jobs.paloaltonetworks.com`:

```yaml
- name: SomeCompany
  type: radancy
  base: https://jobs.somecompany.com
  path: /en/location/israel-jobs/{orgid}/{facet}/2/{page}
  max_pages: 12
  enabled: true
```

Use the site's own location filter to build `path`, then replace the trailing
page number with `{page}`.

## Design notes worth knowing

**Failure is not emptiness.** If a source errors, its existing jobs stay open in
the database. Only sources that actually returned data get their vanished jobs
closed. Without this, one network blip would silently wipe a company's listings.

**Zero jobs is flagged, not celebrated.** A source returning HTTP 200 with an
empty list shows as `!!` in the health table. That is almost always a broken
connector, not an empty job board.

**Deduplication ignores the URL.** Identity is company + title + location,
normalized. A reposted role gets a new ID from the ATS but is not a new job to you.

**Senior roles can't be student roles.** `student_exclude` in the config catches
things like "Senior Intern Program Manager" that would otherwise match on the
word "intern".

**Rate limited to 1 request/second per host** with an identifying User-Agent.
Please don't lower this.

## Troubleshooting

### Why Rafael failed with JSONDecodeError

`career.rafael.co.il/api/jobs` returns **gzip unconditionally**. It sets
`Vary: Accept-Encoding`, but the nginx proxy in front of it serves the
compressed variant even to a client that asked for identity.

`http_text` already decompressed. `http_json` did not — so raw gzip bytes went
straight into `json.loads`, which reported
*"Expecting value: line 1 column 1 (char 0)"*. Both paths now share one
`decode_body()` helper handling gzip, deflate and brotli, and both advertise
`Accept-Encoding` honestly. Verified against a local server that reproduces
Rafael's exact behaviour.

### Why Microsoft failed with HTTP 429

Microsoft's Eightfold tenant answers `429 Please try again later` to roughly
one request in three, even at one request per second, and sends no
`Retry-After`. One unlucky page used to fail the whole source. Requests that
get a 429 or 503 are now retried after 5, 15 and 30 seconds.

### Why Lightricks returned 0 jobs

The company rebranded to LTX and opened a new Greenhouse board, `ltx`. The old
`lightricks` board still answers HTTP 200, just with an empty list — which is
exactly the "zero jobs is flagged" case. The new token is in the page source
of `ltx.io/careers`.

### A filter is missing from the page

This was a **browser cache** problem, not a server problem — the page was being
served correctly and the browser kept showing an older copy.

Fixed properly: every response now sends `Cache-Control: no-store`, and the
fetch calls pass `cache: 'no-store'`. The browser can no longer hold a stale UI.

The header also shows a **build id** derived from a hash of `webapp.py` itself.
It changes automatically whenever the file changes, so if you ever suspect a
stale page, compare the id in the header against the one printed in the terminal
at startup.

(The earlier version-number check could never have worked: both the page and the
server read the same constant from the same process, so they always agreed.)

### Adding a company you found yourself

```bash
python scraper.py --probe https://careers.somecompany.com/jobs
```

Fetches the page, saves it as `probe.html`, and tells you which ATS it runs on
(Greenhouse, Comeet, Workday, Lever, Eightfold, SuccessFactors, Radancy,
Niloosoft and others), plus whether the job list is server-rendered or built by
JavaScript. Then add the matching entry to `sources.yaml`.

### A source returns 0 jobs

```bash
python scraper.py --debug Google
python scraper.py --debug Apple
python scraper.py --debug רפאל
```

`--debug` now works for **every** source, JSON APIs included. For a JSON source
it calls the connector and reports how many jobs came back, how many passed the
Israel filter, how many are student roles, and the first few rows — so you can
see immediately whether the problem is the fetch, the parse, or the filter.

For HTML sources it fetches one page exactly the way the scraper does, saves it
as `debug_<source>.html`, and reports which markup markers the parser found,
distinguishing the two real causes:

- **Markers missing** → the server sent a different page than a browser gets
  (bot filtering, consent wall, redirect). Open the saved file and look.
- **Markers present but 0 parsed** → the markup changed shape. The saved file
  shows the new structure.

### Why Google and Apple were returning 0

Their parsers were correct — verified against real page HTML, 20 of 20 cards.
The problem was the request headers. Both sites serve a stripped-down page to
unrecognised user agents, so Python received markup a browser never sees.

HTML pages are now fetched with normal desktop browser headers (User-Agent,
Accept, Accept-Language) plus transparent gzip handling. JSON APIs still use the
honest `student-job-scraper/1.0` agent, because none of them care.

## Known fragility

**Google is the weak connector.** It parses page markup rather than an API
contract, so a redesign will break it. If Google reports 0 jobs while everything
else is healthy, look there first.

**Endpoints rot.** Microsoft's documented endpoint was already dead when this was
built — they had migrated to Eightfold. Check the health table on every run.

## Not included, and why

**Comeet (13 companies: Riskified, Armis, BioCatch, Pentera, Skai, Natural
Intelligence, Mend.io, Nova, FundGuard, CodeValue, Lasso Security, Payoneer,
HiBob).** Their API needs a per-company token that cannot be derived from the
public UID — verified: `token=UID` returns HTTP 400. The UIDs are recorded in the
`blocked:` section of `sources.yaml` so they're ready if you ever obtain access
through their Job Board partner program.

**Iron Community.** Its job board has not been updated since October 2025.

**Fiverr, CrowdStrike, Varonis, Elementor.** ATS not identified — not on
Greenhouse, Lever, Ashby, or SmartRecruiters.

## Scheduling

To run this automatically each morning, ask Claude: *"run the job scraper every
weekday at 8am and tell me what's new."*
