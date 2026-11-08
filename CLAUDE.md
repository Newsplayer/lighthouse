# Lighthouse: instructions for Claude Code

Lighthouse is a free job board. A GitHub Action in this repo (the **builder**) reads large
employers' own careers systems, de-duplicates and analyses every posting, and publishes the
result.
- **Website (the main product, ROADMAP P1):** a static site on GitHub Pages. Users download
  nothing. Their profile, eligibility and saved/applied/hidden marks stay in their browser.
- **Local app (frozen, bug fixes only):** downloads the `job-index` release and adds personal
  data locally.

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
  - Optional extras need no sign-in: a local AI app reached on `127.0.0.1` only (Ollama, LM
    Studio, llama.cpp), Chrome's built-in model, or the user's own Claude (API key or Claude app
    via MCP).
  - **The builder may use an LLM API with the maintainer's key**, stored as a repo secret, to fill
    filter fields. Today that's Claude Haiku via `llm_fill.py`, capped monthly; a free tier such as
    Gemini would also fit.
    - It sends only public posting text.
    - It's skipped when no key is set, and stops when the quota runs out.
    - The build must always succeed on rules alone.
  - GitHub Models was shut down on 2026-07-30. Options are in `docs/ROADMAP.md` §5.
- **Personal data never leaves the user's machine.**
  - Published files (index and website) are built by whitelist (`snapshot.py` export, `site.py`),
    and a test checks it. Never add a table or column to them without checking it's
    job data.
  - On the website, personal data lives only in the browser's `localStorage`.
- **The builder is the only writer of job data.** The website and the local app never contact
  careers sites; "Apply" and "Read the full posting" link to the employer.
  - The website and the published index ship excerpts/summaries, not full descriptions.
  - The website makes no requests to anyone but its own GitHub Pages origin: no analytics, no
    CDNs, no fonts.
- **Be a polite crawler.** Respect robots.txt, identify as `BUILDER_AGENT`, use only public
  JSON/structured-data endpoints the careers sites themselves load, and stay within the rate
  limits in `net.py`. No LinkedIn or Indeed scraping, no logins, no CAPTCHA solving, no paid
  feeds.
- **Untrusted input:** posting text and links are untrusted. Descriptions are sanitized again
  when shown (`ensure_safe`), links go through `safe_url`, and LLM calls get no tools, a strict
  JSON schema, and escaped output.

## Map

| What | Where |
|---|---|
| Companies by careers page → job boards | `config/companies.csv`, `lighthouse/discover.py` |
| Direct board lists | `config/boards/*.csv` |
| Readers for each careers system (14) | `lighthouse/sources/` |
| Fetch runner, per-board retries, description time cap | `lighthouse/fetch.py` |
| HTTP: reader allowlist, builder open network, shared Workday budget, response limits | `lighthouse/net.py` |
| Schema, migrations, filters, list queries, attaching the index | `lighthouse/db.py` |
| Builder build/export (base + delta chain); reader sync/install/apply | `lighthouse/snapshot.py` |
| Rule extraction (pay, YOE, visa, clearance...) / classification (function, seniority, skills) | `lighthouse/extract.py`, `lighthouse/classify.py` |
| Location → country (rules + GeoNames city list; rebuild with `scripts/make_cities.py`) | `lighthouse/geo.py`, `lighthouse/data/cities.tsv.gz` |
| Location → city, state/province, metro area for the website (bump `places.VERSION` when parsing changes; rebuild the table with `scripts/make_places.py`) | `lighthouse/places.py`, `lighthouse/data/places.tsv.gz` |
| Ghost score | `lighthouse/ghost.py` |
| Eligibility (`check` and `eligibility_sql` must agree; a test enforces it) | `lighthouse/profile.py` |
| Claude fit / MCP server | `lighthouse/claude_api.py`, `lighthouse/mcp_server.py` |
| Website: generator / page (`logic.js` has no DOM; Node tests it against the Python logic) | `lighthouse/site.py` / `site/` |
| Local app UI (frozen) | `lighthouse/web.py`, `lighthouse/templates/` |
| Builder workflow / network probe | `.github/workflows/job-index.yml`, `.github/workflows/network-probe.yml` |
| Maintainer guide | `MAINTAINING.md` |

## Versions to bump

- **`RULES_VERSION`** (`workers.py`): bump when extraction rules change. The next build writes a
  new base by itself.
- **`DERIVE_VERSION`** (`classify.py`): bump when derived columns change.
- **`INDEX_SCHEMA`** (`db.py`) / **`FORMAT`** (`snapshot.py`): bump when the published file layout
  changes. Older apps then keep their jobs and say they need an update.
- **`FORMAT`** (`site.py`) / **`VERSION`** (`places.py`): bump when the website's files or the place
  rules change. The next build writes a new weekly base by itself.
- **`SCHEMA_VERSION`** (`db.py`): follows the `MIGRATIONS` list. Add a migration whenever local
  tables change.

## How to work here

- **Tests:** run `python -m pytest -q` (132 pass) before every commit. No CI runs them yet
  (ROADMAP P0-4).
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
- **Measure before and after** any change to the builder, index format or queries: build time,
  base/delta size, pages in ms. Record the numbers in the PR. Real-data baselines are in
  `docs/ROADMAP.md` §2, and how updates reach users is in §3. For the website, also record
  first-visit bytes, bytes per later visit, and filter/sort time in the page.
- **Freshness matters:** job boards change all day. Never make a change that makes users download
  more per update or wait longer for new postings without saying so in the PR.
- **Branches:** work on a branch, never commit to `main`, and open a PR with a short summary.
- **Decisions:** keep them brief and informative. Note each one in the PR. If the claude.ai
  Projects tool is available, also add an entry to `claude/lighthouse-decisions.md` (read it
  first, then write the whole file back).
- **Writing style:** use plain words in UI text and docs. Say what a user sees, not internal names.
