# Lighthouse

A free job board. Postings come straight from employers' own careers sites: about 700 of the largest
US employers (the Fortune 500 plus major tech, finance, consulting and health employers) and ~500
more boards, in the US, Canada and Mexico. Duplicates and reposts are collapsed, likely ghost jobs
are flagged, and roles you can't apply to are filtered out.

## Use it: the website

**[newsplayer.github.io/lighthouse](https://newsplayer.github.io/lighthouse/)**: nothing to install,
no account.
- **Search:** what (title, skill or company) and where. Type a city, state, province, metro area
  ("Bay Area", "GTA") or country and pick it from the list; "NY-New York", "New York City" and
  "US, NY, New York" all count as New York.
- **Filters,** one button each: date posted, experience (level and/or a years-asked range),
  workplace, job type, pay, category and company. Each choice shows how many roles it would find.
  Skills, sponsorship, clearance, languages, travel, benefits and ghost jobs are under **More filters**.
- **"New since your last visit".**
- **"Can you apply?":** up to four questions (visa sponsorship, citizenship, degree, clearance) leave
  out roles you can't apply to, each with the reason. A question you skip never hides anything.
- **Saved / Applied / Hidden lists.**
- **Your data:** your answers and lists stay in your browser, and nothing is sent anywhere. "Save my
  data to a file" moves them to another browser.
- **Updates:** the job list is refreshed by a GitHub Action every few hours. The page checks for a
  newer one every 15 minutes and downloads only what changed.
- **Applying:** you apply on the employer's own site. The page shows an excerpt of each posting and
  every place it's listed.

Measured on the real list (226,771 roles, 2026-11-09), fast connection / phone-like connection:
- First visit: the newest roles show in 0.2 s / 0.6 s. The whole list (4.9 MB) is ready to search
  and filter in 1.4 s / 5.9 s.
- Searching: 0.1 s / 0.3 s. Filtering: under 10 ms. Opening a job: 0.1 s / 0.3 s.
- A visit after an update downloads only what changed since the weekly list (0.5 MB two days in).

## Use the data

Every build publishes the same data on this repo's
[`job-index` release](https://github.com/Newsplayer/lighthouse/releases/tag/job-index), free for anyone to use:

| File | What it is |
|---|---|
| [`jobs.db.xz`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/jobs.db.xz) | The whole database as one SQLite file. Unpack it (`xz -d jobs.db.xz`, or 7-Zip) and open it with any SQLite tool: DB Browser for SQLite, Datasette, `sqlite3`, pandas. |
| [`jobs.csv.gz`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/jobs.csv.gz) | Every open role as a spreadsheet: one row per role, one column per filter. |
| [`manifest.json`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/manifest.json) | When it was built, how many roles, and how the build went. |

What's in the database:

| Table | Holds |
|---|---|
| `postings` (a view) | One row per open role, duplicates collapsed, with the columns below plus `excerpt`. Start here. |
| `jobs` | Every listing, open and recently closed, with every field the build worked out and when it was first and last seen. |
| `excerpts` | About 600 characters describing each open role. The full posting is on the employer's page (`jobs.url`). |
| `events` | When each listing appeared, closed and reopened. |
| `extractions` | For each description, the sentence each field was read from. |
| `boards`, `companies` | The job boards read, and which listed companies are covered. |

Columns of `postings` and of the CSV:

`id, title, company, location, all_locations, countries, workplace, employment_type, seniority,
job_function, years_experience, years_experience_source, degree, visa_sponsorship, clearance,
us_citizens_only, pay_min_per_year, pay_max_per_year, pay_currency, pay_stated_per, skills, benefits,
travel, languages_required, departments, posted_on, first_seen_on, times_listed, reposts, ghost_score,
ghost_signals, has_description, filled_by_ai, url, ats, board`

Empty means the posting doesn't say. Pay is per year (hourly × 2080; `pay_stated_per` says how the
posting gave it). For example:

```sql
SELECT title, company, all_locations, pay_max_per_year, url
FROM postings
WHERE job_function = 'Data & Analytics' AND workplace = 'remote' AND ghost_score < 3
ORDER BY first_seen_on DESC LIMIT 50;
```

## Where the jobs come from

A GitHub Action in this repo (the **builder**) does all the work, every 3 hours:

1. Finds each listed company's job board from its careers page (re-checked every 3–14 days).
2. Reads every board, politely: only the public feeds the careers sites themselves load, within
   robots.txt and a rate limit per site.
3. Collapses duplicates and reposts, reads each description with free rules (pay, years asked, visa
   language, degree, clearance, skills...), and scores likely ghost jobs.
4. Closes postings that are no longer listed, and deletes them 45 days later.
5. Publishes the database and the website.

**Companies covered:**
- `config/companies.csv`: about 700 large employers by name and careers page. The builder works out
  which careers system each one uses and reads it.
- `config/boards/top-companies.csv`: 926 direct employer boards (about 510 switched on), mostly
  tech companies, startups and trading firms.

Supported careers systems: Workday, Oracle Recruiting, SAP SuccessFactors, Phenom, iCIMS (Jibe
sites), Eightfold, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and Amazon. Also
any careers site whose job pages carry standard schema.org JobPosting data (the format Google for
Jobs reads). On the website, **Sources** lists every company in the companies list: whether it's
covered, which system it uses, how many open roles it has, and, if it isn't covered, why. Clearly
hourly titles (warehouse associate, cashier, delivery driver...) are left out.

**Missing a company?** Open an issue with its careers page. Once it's added to the list, it shows up
for everyone after the next build.

**Running your own copy:** fork the repo and follow [MAINTAINING.md](MAINTAINING.md).

## What the builder works out

- **One row per role.** Exact duplicates (same company, title, location) and near-duplicates
  (same title, near-identical description, e.g. one role posted in six cities) collapse
  into one role that shows every location and every apply link.
- **Repost-aware dates.** A role's date is the earliest it was seen, by the careers system or by
  Lighthouse. A role taken down and reposted keeps its original date.
- **Ghost score with reasons.** Signals: open longer than 60 days, reposted, evergreen title,
  talent-pool wording, not updated in months. One signal never flags. Three or more are hidden on
  the website by default (one click shows them).
- **Postings come and go with the employer's site.** Each build adds new postings and closes the ones
  no longer listed. Most large employers' systems list jobs page by page, which can skip a posting
  when the order shifts, so there a posting closes once it's missing two builds in a row. If a large
  board suddenly lists less than half of its open roles, that's usually a glitch on their side, so
  those closures wait one build for confirmation. A careers-site page that gives its own closing date
  closes when that date passes.
- **Years asked** is read from the posting ("5+ years of…", "at least one year", "minimum 2–5
  years"), labelled *preferred* when it sits under "Preferred / Nice to have", and estimated from the
  title only when the posting gives no number (Intern/New Grad → 0, Senior → 5, Staff → 8,
  Principal/Director → 10, marked "title"). Only a stated requirement can rule anyone out.
- **Filter fields from the description.** Job category, seniority, employment type, skills, pay
  (hourly roles shown per hour), workplace, visa language, degree, clearance, citizenship, travel,
  required languages and benefits. Free rules read these. When a posting clearly talks about one of
  them but no rule can read the answer, a free open model on the build machine reads the sentences
  around it once and fills only that gap; `filled_by_ai` names those fields. A rule's answer is
  never replaced, and an answer is kept only when its quote is really in the text.
- **Excerpts, not full postings.** The public files carry about 600 characters per role and a link
  to the employer's own page, where you read the rest and apply.

## Privacy

There is no server and no account. The website talks only to its own GitHub Pages address: no
analytics, no fonts or scripts from anywhere else. Your answers to "Can you apply?" and your saved,
applied and hidden lists stay in your browser. The published files hold job data only.

## How the build stays cheap

| What | How |
|---|---|
| Unchanged boards | The whole response is hashed; byte-identical to last run → nothing is parsed or written |
| Unchanged postings | Each raw description is hashed; only new or edited ones are cleaned, stored and re-analysed. Rows whose fields didn't change aren't rewritten |
| Countries | Postings located only outside the US, Canada and Mexico are skipped before any description call. Locations are read without any API, using a built-in list of 77k city names (GeoNames, CC BY 4.0) |
| Many boards | 48 boards at a time with a rate limit per site; nothing new starts after 40 minutes, and boards not reached go first next build |
| Big boards | Descriptions are fetched only for new postings, a capped number per build, newest first |
| Sites that push back | A bot check, 403 or run of edge errors leaves that site (or Workday data center, or shared API host) alone for the rest of the build, never worked around; it goes first next build |
| Duplicates | One MinHash signature per description, compared within a company; identical groups are compared once |
| Analysis | Runs on every CPU core; only companies whose postings changed are re-clustered and re-read |
| The website | A weekly job list plus a small "what changed" file; the page downloads only the search words and job details it needs |
| A new careers system | One subclass of `sources.base.Source` and one line in `sources/__init__.py` |

## Code map

| Piece | File |
|---|---|
| Company list → job boards | `config/companies.csv`, `lighthouse/discover.py` |
| Readers for each careers system | `lighthouse/sources/` |
| HTTP rules and rate limits | `lighthouse/net.py` |
| The builder's database | `lighthouse/db.py` |
| Fetch and post-processing | `lighthouse/fetch.py` |
| Clustering, reposts | `lighthouse/dedupe.py` |
| Ghost score | `lighthouse/ghost.py` |
| Field rules; category, seniority, skills, benefits, travel, languages | `lighthouse/extract.py`, `lighthouse/classify.py` |
| Gap fill: a free open model reads what the rules couldn't settle; categories from similar titles | `lighthouse/llm_fill.py`, `lighthouse/local_llm.py`, `lighthouse/categorize.py`, `scripts/get_llm.sh` |
| The build and the public files | `lighthouse/snapshot.py`, `.github/workflows/job-index.yml` |
| Website: generator / page | `lighthouse/site.py` / `site/` (`logic.js` has no DOM: Node tests it) |
| The page's filters and eligibility, in Python (what the page is tested against) | `lighthouse/db.py` (`Filters`, `list_jobs`), `lighthouse/profile.py` |

## CLI (what the Actions run)

```
python -m lighthouse index build --out dist      # find boards, fetch, analyse, write the public files
python -m lighthouse index seed jobs.db.xz       # start the builder from the published database
python -m lighthouse index fill                  # the open-model gap fill alone
python -m lighthouse index summary [--boards]    # how the build went
python -m lighthouse site --out site_out         # the website's files, from the builder database
python -m lighthouse discover [--report]         # find boards from careers pages; check board names
```

## Not doing

Accounts, auto-apply, resume rewriting, LinkedIn or Indeed scraping, paid feeds.

## Tests

```bash
pip install -r requirements.txt -r requirements-builder.txt -r requirements-dev.txt
python -m playwright install chromium   # once, for the page tests
python -m pytest -q && ruff check .
```

GitHub runs the same on every pull request and push to `main` (`.github/workflows/tests.yml`).

The website's tests also run its JavaScript: in Node (filters, eligibility and search words must
match the Python versions exactly) and in Chromium via Playwright. Each is skipped when not installed.

## Credits

City names for location matching: [GeoNames](https://www.geonames.org/) (CC BY 4.0), compacted into
`lighthouse/data/cities.tsv.gz` by `scripts/make_cities.py` (city → country, worldwide) and
`lighthouse/data/places.tsv.gz` by `scripts/make_places.py` (US, Canadian and Mexican cities with
their state or province and metro area).
