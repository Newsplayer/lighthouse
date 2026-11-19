# Lighthouse

A free job board built from employers' own careers sites: about 700 of the largest US employers plus
about 500 boards picked by hand, for roles in the US, Canada and Mexico. Each role appears once, likely
ghost jobs are flagged, and you apply on the employer's site.

## The website

[newsplayer.github.io/lighthouse](https://newsplayer.github.io/lighthouse/). No install and no account.

A few things that aren't obvious from the page:

- Place search understands cities, states, provinces, metro areas ("Bay Area", "GTA") and the odd ways
  careers sites write them: "NY-New York", "New York City" and "US, NY, New York" all match New York.
- "Can you apply?" asks up to four questions (sponsorship, citizenship, degree, clearance) and hides
  roles that rule you out, or shows them marked with the reason if you prefer. A question you skip
  hides nothing.
- Likely ghost jobs (three or more warning signs) are hidden by default. One click shows them, with
  the signs listed.
- Your answers and your saved, applied and hidden lists live only in this browser. Clearing the site's
  data deletes them; "Save my data to a file" moves them to another browser.
- The list is rebuilt about every 50 minutes. An open page checks every 15 minutes and adds new
  roles without a reload.
- The page shows an excerpt of each posting. The full text, and the apply button, are on the
  employer's site.

## The data

Every build publishes the same data on the
[`job-index` release](https://github.com/Newsplayer/lighthouse/releases/tag/job-index), free to use.
The links below always point to the newest build.

| File | What it is |
|---|---|
| [`jobs.db.xz`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/jobs.db.xz) | Everything, as one SQLite file. Unpack it (`xz -d jobs.db.xz`, or 7-Zip) and open it with any SQLite tool: DB Browser for SQLite, Datasette, `sqlite3`, pandas. |
| [`jobs.csv.gz`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/jobs.csv.gz) | Open roles only, one row per role, newest first: the `postings` table below, without the excerpts. |
| [`manifest.json`](https://github.com/Newsplayer/lighthouse/releases/download/job-index/manifest.json) | When it was built, how many roles, and how the build went. |

Start with the `postings` table: one row per open role, duplicates collapsed, newest first. What
each column means is written into the table's definition, so any SQLite tool shows it. The other tables:

| Table | Holds |
|---|---|
| `jobs` | Every listing, including closed ones (kept about 45 days) and the duplicate listings `postings` collapses. To get the same rows as `postings`, filter `active = 1 AND is_canonical = 1`. |
| `events` | When each listing appeared, closed and reopened. |
| `extractions` | For each description, the sentence each field was read from. |
| `boards`, `companies` | The boards read, and which listed companies are covered (and why not). |

Columns of `postings` and of the CSV:

```
id, title, company, location, all_locations, countries, workplace, employment_type, seniority,
job_function, years_experience, years_experience_source, degree, visa_sponsorship, clearance,
us_citizens_only, pay_min_per_year, pay_max_per_year, pay_currency, pay_stated_per, skills, benefits,
travel, languages_required, departments, posted_on, first_seen_on, times_listed, reposts, ghost_score,
ghost_signals, has_description, filled_by_ai, url, ats, board, excerpt
```

What you'd otherwise have to guess:

- An empty field (NULL) means the posting doesn't say. For `countries` it means no country could be
  read from the location (often "Remote" with nothing else).
- Pay is per year in `pay_currency`, and isn't converted: CAD and MXN amounts are in those currencies.
  Hourly pay is multiplied by 2,080; `pay_stated_per` says whether the posting gave it per hour or per
  year. Pay no posting really means (a misread "$130,000 – $150,000,000") is left empty.
- `years_experience_source` is `stated` (a requirement), `preferred` (under "nice to have") or
  `title`: estimated from the title because the posting gives no number (Senior → 5, Staff → 8...).
  Treat `title` as a guess.
- `first_seen_on` is the earliest date the role was seen anywhere, so a role taken down and reposted
  keeps its old date. Use it rather than `posted_on` to find new roles.
- `times_listed` is how many listings were collapsed into the row (one role posted in six cities is
  one row with six); `all_locations` lists them, separated by `|`. `reposts` counts reposts.
- `ghost_score` runs from 0 to 4; `ghost_signals` gives the reasons. The website hides 3 and up.
- `clearance = 'unspecified'` means a clearance is required but the level isn't given.
  `us_citizens_only` is 1 when the posting requires US citizenship.
- `filled_by_ai` names the fields that no rule could read, which a small open model read from the
  posting's own sentences (or, for `job_function`, guessed from similar titles). Everything else came
  from fixed rules.

For example, recent remote data roles that don't look like ghost jobs:

```sql
SELECT title, company, all_locations, pay_max_per_year, url
FROM postings
WHERE job_function = 'Data & Analytics' AND workplace = 'remote' AND ghost_score < 3
ORDER BY first_seen_on DESC LIMIT 50;
```

## Where the jobs come from

A GitHub Action in this repo (the builder) runs one build after another, each under an hour. It reads
the companies in `config/companies.csv` (about 700, by careers page) and the boards in
`config/boards/top-companies.csv` (931, 516 of them switched on), cleans and de-duplicates the
postings, and publishes the files above and the website.

It reads Workday, Oracle Recruiting, SAP SuccessFactors, Phenom, iCIMS, Eightfold, Greenhouse, Lever,
Ashby, Workable, SmartRecruiters, Recruitee and Amazon, plus any careers site whose job pages carry
schema.org JobPosting data (what Google for Jobs reads). It uses only the public feeds those sites load
themselves, follows robots.txt, and backs off when a site pushes back. It never logs in, scrapes
LinkedIn or Indeed, or buys data.

How quickly a new posting shows up depends on the system: within about an hour or two on Greenhouse,
Lever, Ashby, Workable and Recruitee, which answer in one request; within about 5 hours on most
others; and within about 12 on Workday, which is read twice a day because its servers push back on
anything more (sometimes longer, when they push back anyway). Clearly hourly
roles (cashier, warehouse associate, delivery driver...) and roles outside the US, Canada and Mexico
are left out on purpose.

The website's Sources view lists every company in the list: whether it's covered, which system it
uses, how many open roles it has, and if it isn't covered, why.

Missing a company? Open an issue with its careers page.

## How the fields are worked out

- Duplicates: listings with the same company, title and location are one role, and so are listings
  with the same title and a near-identical description (one role posted in six cities).
- Closing: a posting closes when its board stops listing it. Most big systems list jobs page by page
  and can skip one when the order shifts, so there it must be missing two builds in a row. If a board
  with 20+ open roles suddenly lists fewer than half, the closures wait one build in case it's a glitch
  on their side. A posting that gives its own closing date closes on that date.
- Ghost signs: open more than 60 days, reposted, an evergreen or "talent pipeline" title or wording,
  not updated in months. One sign alone never flags a role.
- Years asked are read from the text ("5+ years of…", "at least one year", "2–5 years") and marked
  preferred when they sit under "Preferred" or "Nice to have".
- Pay, workplace, visa language, degree, clearance, citizenship, travel, languages, benefits, skills,
  seniority and job category come from fixed rules. When a posting clearly talks about one of them
  but no rule can read it, a free open model on the build machine reads only the sentences around it.
  Its answer is kept only if the quote it gives is really in the posting, and it never overrides a
  rule.

## Privacy

There's no server and no account. The website only talks to its own GitHub Pages address: no
analytics, no outside fonts or scripts. The published files contain job data only.

## Running your own copy

Fork the repo and follow [MAINTAINING.md](MAINTAINING.md). It runs on GitHub's free runners with no
keys or paid services.

| Piece | File |
|---|---|
| Company list → job boards | `config/companies.csv`, `lighthouse/discover.py` |
| One reader per careers system | `lighthouse/sources/` |
| Rate limits, robots.txt, time limits | `lighthouse/net.py`, `lighthouse/fetch.py` |
| The builder's database | `lighthouse/db.py` |
| Duplicates, reposts and the ghost score | `lighthouse/dedupe.py` |
| Field rules | `lighthouse/extract.py`, `lighthouse/classify.py` |
| The open model's gap fill | `lighthouse/gap_fill.py`, `scripts/get_llm.sh`, `.github/workflows/fill.yml` |
| The build and the public files | `lighthouse/snapshot.py`, `.github/workflows/job-index.yml` |
| Website: generator / page | `lighthouse/site.py` / `site/` |
| The page's filters and eligibility in Python, which the page is tested against | `lighthouse/db.py` (`Filters`, `list_jobs`, `check_eligibility`) |

```
python -m lighthouse index build --out dist      # find boards, fetch, analyse, write the public files
python -m lighthouse index seed jobs.db.xz       # start a builder database from the published one
python -m lighthouse index fill answers.json.gz  # the open model's gap fill; its answers to a file
python -m lighthouse index merge answers.json.gz # merge those answers into the builder database
python -m lighthouse index summary [--boards]    # how the build went
python -m lighthouse site --out site_out         # the website's files
python -m lighthouse discover [--report]         # find boards from careers pages; check board names
```

Tests:

```bash
pip install -r requirements.txt -r requirements-builder.txt -r requirements-dev.txt
python -m playwright install chromium   # once, for the page tests
python -m pytest -q && ruff check .
```

The website's filters, eligibility and search are written twice, in JavaScript for the page and in
Python for the tests, and the tests check that both give the same results. A change to one needs the
same change to the other.

## Credits

City names for location matching: [GeoNames](https://www.geonames.org/) (CC BY 4.0), compacted into
`lighthouse/data/cities.tsv.gz` and `lighthouse/data/places.tsv.gz` by `scripts/make_places.py`.
