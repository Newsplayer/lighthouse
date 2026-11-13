# Lighthouse: instructions for Claude Code

Lighthouse is a free job board. A GitHub Action in this repo (the **builder**) reads large
employers' own careers systems, de-duplicates and analyses every posting, and publishes the
result. There are two products, and nothing else:
- **A good public database:** `jobs.db.xz` (a plain SQLite file anyone can open) and `jobs.csv.gz`
  on the `job-index` release, new every build.
- **The website:** a static site on GitHub Pages for browsing it. Users download nothing. Their
  answers and saved/applied/hidden lists stay in their browser.

The local app was retired on 2026-11-12 (owner). Don't bring back accounts, a server, Claude fit, or
anything a user must install.

**Priority (owner, 2026-11-07):** a functional, effective aggregator first. AI fit is deferred.

**Goal:** the most effective free job board. Every real, open role at the employers we list in
the US, Canada and Mexico, once (no duplicates), updated as close to continuously as polite
crawling allows, with ghost jobs flagged. Each user should see which roles they are eligible
for and fit strongly, and apply on the employer's own site.

**Before starting work, read `docs/ROADMAP.md`.** It describes how the product differs from that
goal, the prioritized work, and the decisions waiting on the owner.

## Hard constraints

- **No paid services, and users never need a key or account.**
  - Every user feature must work with nothing set up.
  - **The builder fills filter fields with a free, open model on the build machine** (no key, no
    account): llama.cpp + Qwen3.5 2B via `llm_fill.py`, and job categories from similar titles via
    `categorize.py` (model2vec), in the workflow's *fill* job after publishing. Both are fetched by
    `scripts/get_llm.sh` (pinned versions; measure a new model with `scripts/eval_fill.py` first).
    - Only the sentences around an unsettled field are shown, and an answer is kept only when its
      quote is really in the text.
    - It's skipped when the model isn't there, and is time-boxed per build.
    - The build must always succeed on rules alone.
  - GitHub Models was shut down on 2026-07-30, and the owner moved off the Claude API key on
    2026-11-09. Other options are in `docs/ROADMAP.md` §5.
- **Personal data never leaves the user's machine.**
  - Published files (the public database and the website) are built by whitelist (`snapshot.py`
    `write_public_db`, `site.py`), and a test checks it. Never add a table or column to them without
    checking it's job data.
  - On the website, personal data lives only in the browser's `localStorage`.
- **The builder is the only writer of job data.** The website never contacts careers sites; "Apply"
  and "Read the full posting" link to the employer.
  - The website and the public database ship excerpts, not full descriptions (those stay in the
    builder's own database, which is never published).
  - The website makes no requests to anyone but its own GitHub Pages origin: no analytics, no
    CDNs, no fonts.
- **Be a polite crawler.** Respect robots.txt, identify as `BUILDER_AGENT`, use only public
  JSON/structured-data endpoints the careers sites themselves load, and stay within the rate
  limits in `net.py`. No LinkedIn or Indeed scraping, no logins, no CAPTCHA solving, no paid
  feeds.
- **Untrusted input:** posting text and links are untrusted. Descriptions are sanitized when stored
  (`sanitize_html`), the page inserts text only as text, links go through `safeUrl` / `clean_url`,
  and LLM calls get no tools, a strict JSON schema, and escaped output.

## Map

| What | Where |
|---|---|
| Companies by careers page → job boards | `config/companies.csv`, `lighthouse/discover.py` |
| Direct board lists | `config/boards/*.csv` |
| Readers for each careers system (14) | `lighthouse/sources/` |
| Fetch runner, per-board retries, description time cap | `lighthouse/fetch.py` |
| HTTP: rate limits, shared Workday budget, response limits | `lighthouse/net.py` |
| The builder's tables (`SCHEMA`, `migrate`), writes, and the list queries the page is tested against | `lighthouse/db.py` |
| The build; the public files (`jobs.db.xz`, `jobs.csv.gz`, `manifest.json`); seeding a new builder from them | `lighthouse/snapshot.py` |
| Rule extraction (pay, YOE, visa, clearance...) / classification (function, seniority, skills) | `lighthouse/extract.py`, `lighthouse/classify.py` |
| Location → country (rules + GeoNames city list; rebuild with `scripts/make_cities.py`) | `lighthouse/geo.py`, `lighthouse/data/cities.tsv.gz` |
| Location → city, state/province, metro area for the website (bump `places.VERSION` when parsing changes; rebuild the table with `scripts/make_places.py`) | `lighthouse/places.py`, `lighthouse/data/places.tsv.gz` |
| Ghost score | `lighthouse/ghost.py` |
| Build health (manifest `health`, Actions summary, the site's Sources view, the broken-boards issue) | `snapshot.health`, `summary_markdown`, `boards_markdown` |
| Eligibility in Python: the reference `site/logic.js` is tested against (`check` and `eligibility_sql` must agree; a test enforces it) | `lighthouse/profile.py` |
| Builder gap fill: open model on the build machine / its server / job categories from similar titles | `lighthouse/llm_fill.py` / `lighthouse/local_llm.py` / `lighthouse/categorize.py` |
| Getting the models (pinned) / measuring them on real data | `scripts/get_llm.sh` / `scripts/eval_fill.py` |
| Website: generator / page (`logic.js` has no DOM; Node tests it against the Python logic) | `lighthouse/site.py` / `site/` |
| Workflows: builder, page-only deploy, tests and lint, network probe, branch tidying | `.github/workflows/job-index.yml`, `website.yml`, `tests.yml`, `network-probe.yml`, `tidy-branches.yml` |
| Page-only deploy (a push to `site/` goes live on the last build's data, no build) | `.github/workflows/website.yml` |
| Maintainer guide | `MAINTAINING.md` |

## Versions to bump

- **`RULES_VERSION`** (`workers.py`): bump when extraction rules change. The next build re-reads
  every description once.
- **`DERIVE_VERSION`** (`classify.py`): bump when derived columns change.
- **`FORMAT`** (`site.py`) / **`VERSION`** (`places.py`): bump when the website's files or the place
  rules change. The next build writes a new weekly base by itself.
- **`SCHEMA_VERSION`** (`db.py`): bump when the builder's tables (`SCHEMA`) change, and add the step
  from the previous version to `migrate()`. The production database lives in the Actions cache and
  must be carried forward.
- **The public database** (tables and the `postings` view in `snapshot.py`) is read by other
  people's queries. Say so in the PR when a table or column changes, and update the README.

## How to work here

- **Tests:** run `python -m pytest -q` (123 pass) and `ruff check .` before every commit
  (`pip install -r requirements-dev.txt`). `tests.yml` runs both on every pull request and push to
  `main`.
  - The website's tests run `site/logic.js` in Node and the page in Chromium (Playwright).
  - Filters, eligibility and search words must give exactly the same results as the Python code.
    Change both sides together. Add a test for each
  behaviour you change. Fakes for each careers system live in `tests/conftest.py` and
  `tests/test_enterprise.py`.
- **Network:** your sandbox usually can't reach careers sites or GitHub release downloads. To try
  something against real sites:
  1. Push a script as `probe/run.py` on a branch named `probe/<topic>`. It runs in Actions via
     `network-probe.yml`.
  2. Print one JSON line per result.
  3. Read the output with `gh run view <id> --log`. Use REST `gh api`, because GraphQL calls may
     be blocked.
  4. Add an empty `probe/run-build` file to run a full test build instead. It publishes nothing.
  5. Or add an empty `probe/run-site` file: the run first gets the last build's database from the
     Actions cache (read only), so `probe/run.py` can write and time the website on real data.
- **Measure before and after** any change to the builder, the public files or queries: build time,
  file sizes, pages in ms. Record the numbers in the PR. Real-data baselines are in
  `docs/ROADMAP.md` §2, and how updates reach users is in §3. For the website, also record
  first-visit bytes, bytes per later visit, and filter/sort time in the page.
- **Freshness matters:** job boards change all day. Never make a change that makes users download
  more per update or wait longer for new postings without saying so in the PR.
- **Branches:** work on a branch, never commit to `main`, and open a PR with a short summary.
  - Check open branches and PRs first (`git ls-remote --heads origin`). Build on unmerged work
    instead of starting a parallel branch that touches the same files.
  - The *Job index* workflow runs on `main` only; test builds go through the network probe.
  - Merged branches and idle `probe/…` branches are deleted by `tidy-branches.yml`.
- **Decisions:** keep them brief and informative. Note each one in the PR. If the claude.ai
  Projects tool is available, also add an entry to `claude/lighthouse-decisions.md` (read it
  first, then write the whole file back).
- **Writing style:** use plain words in UI text and docs. Say what a user sees, not internal names.
