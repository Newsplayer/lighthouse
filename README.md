# Lighthouse

A local job aggregator. Pulls postings straight from employers' applicant-tracking systems,
collapses duplicates and reposts, scores likely ghost jobs, filters out roles you can't apply
to, and (optionally) asks Claude how well each remaining role fits you, with reasons.

Runs entirely on your machine. Uses only free, keyless public ATS APIs, plus the Claude API
if you choose to add a key (or your own Claude app over MCP, with no key at all).

## Quick start

Needs Python 3.11+ ([python.org](https://www.python.org/downloads/)). Then:

| System | How to start |
|---|---|
| Windows | Double-click `start-windows.bat` |
| macOS | Double-click `start-mac.command` (first time: right-click → Open) |
| Linux | `./start.sh` |

The first run sets everything up and pulls jobs. After that, startup takes a couple of
seconds. Your browser opens to the site automatically; close the terminal window to stop it.
While it's open, boards refresh every 6 hours.

Upgrading from v0.1: your existing database is migrated automatically on first start.

### Manual setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m lighthouse start
```

## Sources

| ATS | Board link looks like | Notes |
|---|---|---|
| Greenhouse | `job-boards.greenhouse.io/<slug>` | |
| Lever | `jobs.lever.co/<slug>` | salary when posted |
| Ashby | `jobs.ashbyhq.com/<slug>` | salary included |
| Workable | `apply.workable.com/<slug>` | |
| SmartRecruiters | `jobs.smartrecruiters.com/<CompanyId>` | case-sensitive id; descriptions fetched for new/changed postings only |
| Recruitee | `<slug>.recruitee.com` | |

Add boards on the **Sources** page (paste many links at once, or a company's own careers page
and Lighthouse finds the embedded board), with `python -m lighthouse add <url or file>`, or in
`config/sources.toml`.

## What you get

- **One row per role.** Exact duplicates (same company, title, location) and near-duplicates
  (same title, near-identical description, e.g. one role posted in six cities) collapse
  into one listing that shows every location.
- **Repost-aware dates.** "Since" is the earliest date the role was seen, by the ATS or by
  Lighthouse. A role taken down and reposted keeps its original date.
- **Ghost score with reasons.** Signals: open longer than 60 days, reposted, evergreen title,
  talent-pool wording, not updated in months. One signal never flags. Two = "possible ghost".
  Three or more are hidden by default (one click shows them).
- **Filters.** Salary, workplace, years asked, visa language, sponsorship history, fit tier,
  company, location, full-text search.
- **Save / Applied / Hide**, plus a "New since last visit" tab.
- **Eligibility (Profile page).** Deterministic rules remove roles that clearly rule you
  out (no sponsorship, citizenship or clearance, location, years of experience, salary floor,
  degree). Unknown never disqualifies. Each exclusion says why.
- **Fit tiers (optional, Claude).** Strong / Possible / Weak with each requirement marked
  met, missing or unclear. No 0–100 scores.
- **Sponsorship history (optional).** Import DOL LCA and USCIS H-1B Employer Data Hub files
  on the **Sponsorship** page; each job shows the employer's filings and approvals, kept separate.

## Claude, two ways

**Your own API key.** Claude page → paste key (stored in your OS keychain). Extraction uses
Haiku, fit uses Sonnet; 20+ requests go through the Batch API at half price; the candidate
profile is a cached prompt prefix. A monthly cap (default $5) blocks runs that would exceed it.
The Profile page shows the exact request before anything is sent. Your resume's name, email,
phone, links and street address are stripped first.

**Your own Claude app (no key).** Lighthouse is an MCP server. In Claude Desktop's or Claude
Code's MCP config:

```json
{"mcpServers": {"lighthouse": {
  "command": "C:/Users/you/Coding/lighthouse/.venv/Scripts/python.exe",
  "args": ["-m", "lighthouse", "mcp"],
  "cwd": "C:/Users/you/Coding/lighthouse"}}}
```

Then ask your Claude app something like "use Lighthouse to find eligible roles I haven't
judged yet and save a verdict for each". Tools: `search_jobs`, `get_posting`, `get_profile`,
`save_verdict`, `set_mark`. On consumer Claude plans, check the "Help improve Claude" setting.

## Privacy

No Lighthouse server, no telemetry. Outbound requests are limited in code to the ATS APIs and
`api.anthropic.com` (see the **Network** page). The single exception is a careers page you paste,
which is opened once to find its board. The site only answers on 127.0.0.1, rejects foreign
Host headers, and requires a per-session token on every form.

## How it scales

| Concern | Approach |
|---|---|
| Many boards | Thread pool (`fetch_workers`), shared per-host rate limit, retries with backoff; one failing board never stops a run |
| Big boards | SmartRecruiters descriptions fetched only for new/changed postings, capped per run |
| Duplicates | One-pass MinHash per description + LSH banding per company, so cost grows with postings, not pairs |
| Re-runs | Only companies whose postings changed are re-clustered and re-extracted; signatures and extractions are cached by description hash |
| The list | Cluster/filter fields are precomputed and indexed; pages are `LIMIT/OFFSET` queries; FTS5 search |
| Claude spend | Eligibility and ghost filters run first; Batch API; prompt caching; cache by (description, profile); monthly cap |
| Storage | Descriptions of roles closed 180+ days are dropped (history kept); sponsorship data aggregated per employer-year |
| Schema | Versioned migrations (`PRAGMA user_version`) |
| New ATS | One subclass of `sources.base.Source` + one line in `sources/__init__.py` |

## Code map

| Piece | File |
|---|---|
| ATS adapters | `lighthouse/sources/` |
| HTTP allowlist + throttle | `lighthouse/net.py` |
| Storage, migrations, list query | `lighthouse/db.py` |
| Fetch + post-processing | `lighthouse/fetch.py` |
| Clustering, reposts | `lighthouse/dedupe.py` |
| Ghost score | `lighthouse/ghost.py` |
| Field extraction | `lighthouse/extract.py` |
| Profile, eligibility, redaction | `lighthouse/profile.py` |
| Claude API (extraction, fit, batches, spend) | `lighthouse/claude_api.py` |
| Sponsorship data | `lighthouse/sponsors.py` |
| MCP server | `lighthouse/mcp_server.py` |
| Scheduler | `lighthouse/scheduler.py` |
| Website | `lighthouse/web.py`, `templates/` |

## CLI

```
python -m lighthouse start | serve | fetch
python -m lighthouse add https://jobs.lever.co/palantir urls.txt ...
python -m lighthouse boards
python -m lighthouse reprocess
python -m lighthouse visa import [files]       # default: everything in data/visa/
python -m lighthouse claude extract|fit|poll [--limit N]
python -m lighthouse key set|clear
python -m lighthouse mcp
```

## Not doing

Auto-apply, resume rewriting, LinkedIn scraping, contact harvesting, chat coach. See the market scan.

## Tests

```bash
pip install pytest && pytest
```
