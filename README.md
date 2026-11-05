# Lighthouse

A local job board. Postings come straight from employers' own careers sites: about 700 of the
largest US employers (the Fortune 500 plus major tech, finance, consulting and health employers)
and ~500 more boards, with duplicates and reposts collapsed, likely ghost jobs scored, and roles you
can't apply to filtered out. Optionally, Claude tells you how well each remaining role fits you.

The app runs on your computer and your data stays there. The job data comes from a shared,
read-only **job index** that a scheduled GitHub Action rebuilds every 6 hours; your copy just
downloads it. No account, no server, no API key needed (Claude is optional).

## Quick start

Needs Python 3.11+ ([python.org](https://www.python.org/downloads/)). Download this repo
(**Code → Download ZIP**, then unzip) and start it:

| System | How to start |
|---|---|
| Windows | Double-click `start-windows.bat` |
| macOS | Double-click `start-mac.command` (first time: right-click → Open) |
| Linux | `./start.sh` |

The first start sets everything up and downloads the job index (a minute or two). After that,
startup takes a couple of seconds and only the changes are downloaded (usually a few MB).
Your browser opens to the site; close the terminal window to stop it.

Then:
1. **Profile:** add your experience, location, countries and visa needs. Roles you can't apply to
   are filtered out, each with the reason.
2. **Claude (optional):** paste an API key on the **Claude** page, or connect your own Claude app
   (see [Claude, two ways](#claude-two-ways)) to get fit tiers.

**Updating Lighthouse:** download the latest version and replace the folder (keep `data/`, which
holds your profile, marks and verdicts in `data/user.db`). Coming from 0.5 or earlier, your data is
moved into `user.db` automatically on first start. The old `data/lighthouse.db` is deleted once
the new index has downloaded. If the job index ever needs a newer version, the
**Sources** page says so.

### Manual setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m lighthouse start
```

## Where the jobs come from

| Step | Where it runs | Who can change it |
|---|---|---|
| Find each listed company's job board from its careers page | GitHub Action (re-checked every 3–14 days) | the maintainer only |
| Fetch every board, de-duplicate, analyse descriptions, score ghosts | GitHub Action, every 6 hours | the maintainer only |
| Publish a ready-to-use database on the repo's `job-index` release | GitHub | the Action only |
| Use that database as downloaded; add eligibility, fit, saved/applied/hidden | your computer | you (personal data only) |

Your copy never contacts job boards and does no processing of its own. About once a week it
downloads a full index, already de-duplicated, analysed and search-indexed, and opens it as is
(a few seconds). After that it applies a small "what changed" file every few hours. Your own data
(profile, marks, Claude verdicts) lives in a separate file, `data/user.db`, which a new index
never touches. Every download is checked against its published SHA-256. A description and its
links are sanitized again when a page shows them, so a damaged or tampered file can't put scripts
into your site. If an update fails, the jobs you already have stay and the **Sources** page says why.

**Companies covered:**
- `config/companies.csv`: about 700 large employers by name and careers page. The Action works out
  which careers system each one uses and reads it.
- `config/boards/top-companies.csv`: 926 direct employer boards (about 510 switched on), mostly
  tech companies, startups and trading firms.

Supported careers systems: Workday, Oracle Recruiting, SAP SuccessFactors, Phenom, iCIMS (Jibe
sites), Eightfold, Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and Amazon. Also
any careers site whose job pages carry standard schema.org JobPosting data (the format Google for
Jobs reads). The **Sources** page lists every company in the companies list. For each it shows
whether it's covered, which system it uses, how many open roles it has, and, if it isn't covered,
why (for example, a careers system Lighthouse can't read yet). Clearly hourly titles (warehouse
associate, cashier, delivery driver...) are left out.

**Missing a company?** On the **Sources** page, click **Suggest a board**. It opens a pre-filled
GitHub issue; once the maintainer adds it, it shows up for everyone after the next build.

**Running your own index** (maintainers, or anyone who wants a private board list): see
[MAINTAINING.md](MAINTAINING.md). Without an `[index] repo` set, Lighthouse fetches boards itself,
as it did before v0.5.

## What you get

- **One row per role.** Exact duplicates (same company, title, location) and near-duplicates
  (same title, near-identical description, e.g. one role posted in six cities) collapse
  into one listing that shows every location.
- **Repost-aware dates.** "Since" is the earliest date the role was seen, by the ATS or by
  Lighthouse. A role taken down and reposted keeps its original date.
- **Ghost score with reasons.** Signals: open longer than 60 days, reposted, evergreen title,
  talent-pool wording, not updated in months. One signal never flags. Two = "possible ghost".
  Three or more are hidden by default (one click shows them).
- **Filters.** Salary, workplace, **years of experience asked** (min–max, with or without roles
  that don't say), countries, visa language, sponsorship history, fit tier, company, location,
  full-text search. Sort by newest, best fit, highest pay or fewest years asked.
- **Years asked** is read from the posting ("5+ years of…", "at least one year", "minimum 2–5
  years"), labelled *preferred* when it sits under "Preferred / Nice to have", and estimated from the
  title only when the posting gives no number (Intern/New Grad → 0, Senior → 5, Staff → 8,
  Principal/Director → 10, shown as "from title"). Only a stated requirement can rule you out.
- **Save / Applied / Hide**, plus a "New since last visit" tab.
- **Eligibility (Profile page).** Deterministic rules remove roles that clearly rule you
  out (no sponsorship, citizenship or clearance, location, years of experience, salary floor,
  degree). Unknown never disqualifies. Each exclusion says why.
- **Fit tiers (optional, Claude).** Strong / Possible / Weak with each requirement marked
  met, missing or unclear. No 0–100 scores.
- **Sponsorship history (optional).** Import DOL LCA and USCIS H-1B Employer Data Hub files
  on the **Sponsorship** page; each job shows the employer's filings and approvals, kept separate.
- **Filters from the description.** Job function (Software Engineering, Data & Analytics, Finance...),
  seniority (intern to executive), employment type, skills the posting mentions (python, sql, aws,
  excel...), years asked, pay (hourly roles shown per hour), workplace, visa language. All of it is
  worked out by free rules in the GitHub Action, so filtering needs no API and no work on your
  computer.
- **Every apply link.** When the same role is listed on several sites, the job page links each
  open copy, with tracking parameters removed.

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

No Lighthouse server, no telemetry. Outbound requests are limited in code to the job index's release
files on `github.com` (served from `*.githubusercontent.com`) and, if you use Claude with a key,
`api.anthropic.com`; see the **Network** page. Your copy never contacts job boards. Nothing is
uploaded anywhere: your profile, marks and verdicts live only in `data/user.db`. The site only
answers on 127.0.0.1, rejects foreign Host headers, and requires a per-session token on every form.

## Speed: doing less work, not just faster work

On your computer the work is a download and fast local queries: the index arrives ready to
search, and eligibility is worked out inside each query, only for the rows you're looking at. The rest of this table
is what keeps the index build itself cheap (it runs on GitHub, in [MAINTAINING.md](MAINTAINING.md)).

| What | How |
|---|---|
| Unchanged boards | The whole response is hashed; byte-identical to last run → nothing is parsed or written (16 ms instead of ~3 s for a 1,700-posting board) |
| Unchanged postings | Each raw description is hashed; only new/edited ones are cleaned, stored and re-analysed. Rows whose fields didn't change aren't rewritten |
| Countries | The index can be limited to some countries (`LIGHTHOUSE_COUNTRIES`); postings located only elsewhere are skipped before any detail download. Your Profile countries filter the list on top |
| Hourly roles | Skipped before any per-posting call |
| Shared index | The fetching, analysis and search indexing run once, on GitHub, for everyone. Your computer downloads a ready-to-use file weekly and opens it as is (a few seconds), then applies a small change file every few hours |
| Descriptions on disk | Each is compressed against one shared dictionary of common posting text: about 3× smaller than plain text, and a page still reads only the one it shows |
| Analysis | Runs on every CPU core; field extraction only scans short windows around keywords ("year", "sponsor"…): 5× faster with identical results |
| Search | The job list reads one table through partial indexes (marks and filter fields are stored on each row), counts stop at 5,000, dropdown lists are cached until data changes; page loads at 52k postings 17–145 ms (were 240–330 ms) |

## How it scales

| Concern | Approach |
|---|---|
| Many boards | Thread pool (`fetch_workers`), shared per-host rate limit, retries with backoff; one failing board never stops a run |
| Big boards | SmartRecruiters/Workday descriptions fetched only for new postings, capped per run; hourly titles dropped before any detail call |
| ~900 boards | Board lists in `config/boards/*.csv`; 16 parallel workers with a per-host limit; dead boards switched off automatically |
| Duplicates | One-pass MinHash per description + LSH banding per company, so cost grows with postings, not pairs |
| Re-runs | Only companies whose postings changed are re-clustered and re-extracted; signatures and extractions are cached by description hash |
| The list | Cluster/filter fields (function, seniority, skills, pay...) are precomputed by the builder and indexed; pages are `LIMIT/OFFSET` queries; FTS5 search |
| Claude spend | Eligibility and ghost filters run first; Batch API; prompt caching; cache by (description, profile); monthly cap |
| Storage | Descriptions stored zlib-compressed in their own table; search indexes the first 4,000 characters; descriptions of roles closed 180+ days are dropped (history kept); sponsorship data aggregated per employer-year |
| Schema | Versioned migrations (`PRAGMA user_version`) |
| New ATS | One subclass of `sources.base.Source` + one line in `sources/__init__.py` |

## Code map

| Piece | File |
|---|---|
| Company list → job boards | `config/companies.csv`, `lighthouse/discover.py` |
| ATS adapters | `lighthouse/sources/` |
| HTTP allowlist + throttle | `lighthouse/net.py` |
| Storage, migrations, list query | `lighthouse/db.py` |
| Fetch + post-processing | `lighthouse/fetch.py` |
| Clustering, reposts | `lighthouse/dedupe.py` |
| Ghost score | `lighthouse/ghost.py` |
| Field extraction; function, seniority, skills | `lighthouse/extract.py`, `lighthouse/classify.py` |
| Profile, eligibility, redaction | `lighthouse/profile.py` |
| Claude API (extraction, fit, batches, spend) | `lighthouse/claude_api.py` |
| Sponsorship data | `lighthouse/sponsors.py` |
| MCP server | `lighthouse/mcp_server.py` |
| Job index: build, download, merge | `lighthouse/snapshot.py`, `.github/workflows/job-index.yml` |
| Scheduler | `lighthouse/scheduler.py` |
| Website | `lighthouse/web.py`, `templates/` |

## CLI

```
python -m lighthouse start | serve | fetch       # fetch = update the job index (or fetch boards, without [index] repo)
python -m lighthouse add https://jobs.lever.co/palantir urls.txt ...   # without [index] repo only
python -m lighthouse boards
python -m lighthouse reprocess
python -m lighthouse visa import [files]       # default: everything in data/visa/
python -m lighthouse claude extract|fit|poll [--limit N]
python -m lighthouse key set|clear
python -m lighthouse mcp
python -m lighthouse index status                 # when the job index was built, what it covers
python -m lighthouse index import [FILE.db.xz]   # update now, or merge a local index file
python -m lighthouse index build --out dist       # what the GitHub Action runs (MAINTAINING.md)
```

## Not doing

Auto-apply, resume rewriting, LinkedIn scraping, contact harvesting, chat coach. See the market scan.

## Tests

```bash
pip install pytest && pytest
```
