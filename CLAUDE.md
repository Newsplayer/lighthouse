# Lighthouse: instructions for Claude Code

Lighthouse is a free, locally run job board. A GitHub Action in this repo (the **builder**) reads
large employers' own careers systems and publishes a ready-to-use job index on the `job-index`
release. Each user's local app (a **reader**) downloads that index and adds personal data:
profile, eligibility, fit, saved/applied/hidden.

**Goal:** the most effective free job board. Every real, open role at the employers we list,
once (no duplicates), with ghost jobs flagged. Each user should see which roles they are eligible
for and fit strongly, and apply on the employer's own site.

**Before starting work, read `docs/ROADMAP.md`.** It describes how the product differs from that
goal, the prioritized work, and the decisions waiting on the owner.

## Hard constraints

- **No paid services and no required API keys.** Every feature must work without an account or
  key. Claude is the one optional, opt-in extra: the user's own API key, or their Claude app via
  MCP. Free tiers that need a key (Gemini, Groq, OpenRouter...) are out. GitHub Models was shut down
  on 2026-07-30. Free LLM options that work are listed in `docs/ROADMAP.md` §4.
- **Personal data never leaves the user's machine.** Published index files are built by whitelist
  (`snapshot.py` export) and a test checks it. Never add a table or column to them without
  checking it's job data.
- **The builder is the only writer of job data.** Readers don't fetch boards. The only reader-side
  job-data writes are re-deriving fields from the user's own Claude extractions
  (`extract.derive_claude`).
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
| Ghost score | `lighthouse/ghost.py` |
| Eligibility (`check` and `eligibility_sql` must agree; a test enforces it) | `lighthouse/profile.py` |
| Claude fit / MCP server | `lighthouse/claude_api.py`, `lighthouse/mcp_server.py` |
| Web UI | `lighthouse/web.py`, `lighthouse/templates/` |
| Builder workflow / network probe | `.github/workflows/job-index.yml`, `.github/workflows/network-probe.yml` |
| Maintainer guide | `MAINTAINING.md` |

## Versions to bump

- **`RULES_VERSION`** (`workers.py`): bump when extraction rules change. The next build writes a
  new base by itself.
- **`DERIVE_VERSION`** (`classify.py`): bump when derived columns change.
- **`INDEX_SCHEMA`** (`db.py`) / **`FORMAT`** (`snapshot.py`): bump when the published file layout
  changes. Older apps then keep their jobs and say they need an update.
- **`SCHEMA_VERSION`** (`db.py`): follows the `MIGRATIONS` list. Add a migration whenever local
  tables change.

## How to work here

- **Tests:** run `python -m pytest -q` (92 pass at v0.6) before every commit. No CI runs them yet
  (ROADMAP P0-4). Add a test for each
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
  `docs/ROADMAP.md` §2.
- **Branches:** work on a branch, never commit to `main`, and open a PR with a short summary.
- **Decisions:** keep them brief and informative. Note each one in the PR. If the claude.ai
  Projects tool is available, also add an entry to `claude/lighthouse-decisions.md` (read it
  first, then write the whole file back).
- **Writing style:** use plain words in UI text and docs. Say what a user sees, not internal names.
