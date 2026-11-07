# Maintaining the job index

For the maintainer of `Newsplayer/lighthouse`. Users don't need any of this: they download the
app, start it, and it reads the index this repo publishes.

```
config/companies.csv ─► careers pages ─┐
config/boards/*.csv ───► job boards ───┴► GitHub Action (every 6 h) ─► release "job-index" ─► every user's Lighthouse
                                           finds boards, fetches,        manifest.json          uses base.db.xz as is,
                                           cleans, analyses, indexes     base.db.xz  (weekly)   applies deltas, never writes
your commits ──────────► (lists, code)                                   delta.db.xz (each run)
```

- **Who can change what:**
  - Only the Action writes the index.
  - Only people with write access to the repo (you) change the board list and code.
  - Everyone else can read it, and suggest boards through issues.
- **Cost:** $0. Public repos get free, unlimited Actions minutes, and release downloads are free.

---

## One-time setup (about 15 minutes, then a 30–60 minute first build)

### 1. Put the workflow file in place

GitHub only runs workflows stored in `.github/workflows/`. The file was delivered to the repo root
as `job-index.yml`. In a terminal in `C:\Users\luisa\Coding\lighthouse`:

```bat
mkdir .github\workflows
move job-index.yml .github\workflows\job-index.yml
del snapshot.yml
del tests\test_snapshot.py
```

(`snapshot.yml` and `tests\test_snapshot.py` belong to the earlier per-user version, which this replaces.)

### 2. Commit and push

```bat
git add -A
git commit -m "Shared job index: GitHub Action builds it, the app reads it"
git push
```

`config/sources.toml` now contains `[index] repo = "Newsplayer/lighthouse"`, which is what makes
every downloaded copy read your index. Keep it in every release.

### 3. Make the repo public

Users' apps download the index without logging in, which only works on a public repo.
**Settings → General → Danger Zone → Change repository visibility → Public.**
(Skip this if it's already public.) Before switching, check that the repo's history doesn't contain
a database or anything personal: `git log --all --stat -- data` should list no `.db` files.
`data/*.db` is git-ignored.

### 4. Check the Actions settings

**Settings → Actions → General:**
- **Actions permissions:** "Allow all actions and reusable workflows". The workflow uses only
  GitHub's own `checkout`, `setup-python` and `cache`.
- **Workflow permissions:** leave the default. The workflow asks for `contents: write` itself. If the
  *Publish* step later fails with `Resource not accessible by integration` (403), pick
  **Read and write permissions** here.
- **Fork pull request workflows:** "Require approval for all outside collaborators". This workflow
  doesn't run on pull requests anyway, so a pull request can never publish an index.

### 5. Optional: limit the index to some countries

**Settings → Secrets and variables → Actions → Variables → New repository variable:**
`LIGHTHOUSE_COUNTRIES` = `US` (or `US,CA,GB`). Postings located only elsewhere are skipped, which
makes builds faster and downloads smaller. Leave it unset for everything. Users can always narrow
further with their Profile countries, but they can't get countries the index left out.

### 6. Turn on Issues (for board suggestions)

**Settings → General → Features → Issues** ✓. The app's **Suggest a board** button opens a
pre-filled issue here.

### 7. Run the first build

**Actions → Job index → Run workflow → Run workflow.** The first run fetches everything (30–60 min;
big Workday sites fill in their descriptions over the next few runs). When it's green:
- **Releases → Job index** (tag `job-index`) has `manifest.json` and a `base-<time>.db.xz`. Each build's
  files get their own names; the manifest says which are current, and older ones are removed after it's uploaded.
- The *Publish* step's last line reads like `48213 open roles from 509 boards; new base 41.2 MB`.

After that it runs every 6 hours by itself, plus whenever you push a change under `config/`.

### 8. Check it from the app

Restart your local Lighthouse (it's a reader too now), or run `python -m lighthouse index import`.
The **Sources** page shows "Jobs as of … · N open roles from M boards".
`python -m lighthouse index status` prints the same.

The first import replaces your locally fetched jobs with the index. Your profile, saved/applied/hidden
marks and Claude verdicts carry over: they're keyed by posting, and the postings are the same ones.

### 9. Optional: protect `main`

**Settings → Rules → Rulesets → New branch ruleset:** target the default branch, enable
**Restrict deletions** and **Block force pushes**. Only you can push (you're the only collaborator:
**Settings → Collaborators**), so this just guards against accidents.

---

## Trying something against real careers sites

Most development sandboxes (including Claude Code's) can't reach careers sites. Push a script as
`probe/run.py` on a branch named `probe/<topic>`. The **Network probe** workflow runs it from
GitHub's network; read its output in the run's log. Add an empty `probe/run-build` file to run a
full test build instead. Nothing is published either way. Delete the branch when done.

## Day to day

### Add a large employer by its careers page

Add a row to `config/companies.csv`: `company,group,domain,careers_url,ats,slug,enabled`, e.g.
`Acme Corp,fortune500,acme.com,https://careers.acme.com,,,yes`. Leave `ats` and `slug` empty and
push. The next build opens the careers page and finds the job system behind it: Workday, Oracle,
SuccessFactors, Phenom, iCIMS/Jibe, Eightfold, Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
Recruitee, or a sitemap of job pages with JobPosting data. It checks the board answers, then
fetches it. The result is in the app's **Sources → Large employers** table and in
`manifest.json` → `coverage`.

- **Re-checks:** a company whose board works is re-checked every 14 days, sooner if its board
  starts failing. One that couldn't be resolved is retried every 3 days. A full build re-checks all.
- **Boards under other names:** a careers page often links to subsidiaries' or partners' boards. A board
  is taken as the company's own only if its name matches the company (`jpmc` for JPMorgan Chase), it
  was read off the company's own site, or the company's careers address redirects to it. A lone
  board under another name is used with a note in its detail; several of them are listed and left
  for you to pin.
- **Wrong board, or a site discovery can't read:** fill in `ats` and `slug` to pin the board by hand
  (slug formats are in `config/sources.toml`). Oracle: `<api host>/<site>`. SuccessFactors / Jibe:
  the careers host. Phenom: `<host>/<country>/<lang>`. Eightfold: `<host>|<domain>`. Structured-data
  sites: the sitemap URL.
- **Remove a company:** set `enabled` to `no`.
- **Statuses:**
  - `unsupported`: the site uses a system Lighthouse can't read yet (Taleo, Avature, BrassRing...;
    the row says which).
  - `blocked`: robots.txt disallows the page.
  - `error`: the page didn't open from GitHub's network.
  - `not_found`: no job system was recognised.

Careers pages are only read for links and structured data. Every page fetch respects robots.txt
and identifies itself as `Lighthouse-index/<version> (+https://github.com/Newsplayer/lighthouse)`.

### Add a board (or act on a "Board suggestion" issue)

1. Find the board's link on the company's careers page and check it answers:

   | ATS | Open this in a browser; it should show JSON with jobs |
   |---|---|
   | Greenhouse | `https://boards-api.greenhouse.io/v1/boards/<slug>/jobs` |
   | Lever | `https://api.lever.co/v0/postings/<slug>?mode=json` |
   | Ashby | `https://api.ashbyhq.com/posting-api/job-board/<slug>` |
   | Workable | `https://apply.workable.com/api/v1/widget/accounts/<slug>` |
   | SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/<CompanyId>/postings` (case-sensitive) |
   | Recruitee | `https://<slug>.recruitee.com/api/offers/` |
   | Workday | can't be opened directly; `<tenant>.wd<N>.myworkdayjobs.com/<Site>` should list jobs. Slug = `<tenant>.wd<N>/<Site>` |

2. Add a row to `config/boards/top-companies.csv` (or a new CSV in `config/boards/`):
   `greenhouse,acme,Acme Corp,yes,,curated`
3. Commit and push. The push starts a build; users get the board within an hour of it finishing
   (their app checks hourly).

### Remove or pause a board

Set its `enabled` column to `no` (or delete the row) and push. Its postings are closed in the next
build, and they disappear for users with the next update.

### Boards that stopped existing

A board that answers "not found" two runs in a row is switched off automatically. The build's
`manifest.json` lists them under `switched_off`. Fix the slug in the CSV (that counts as a new board)
or set `enabled` to `no`. To retry them all, run the workflow with **full** ticked.

### Ship an app update

Commit and push code as usual; bump `version` in `pyproject.toml` and `lighthouse/__init__.py`.

- **No database change:** users' apps keep reading the index whatever version they run.
- **You added a migration in `db.py` (schema change):** the next index is built with the new schema,
  and older apps can't read it. They keep the jobs they have, and their Sources page says
  "the job index needs a newer Lighthouse". Tell users to download the new version.
- **You changed the field-extraction rules (`RULES_VERSION` in `workers.py`):** the next build writes a
  fresh full file by itself (the manifest's rules version no longer matches).
- **You changed the published index layout (`INDEX_SCHEMA` in `db.py`):** older apps keep the jobs
  they have and say "needs a newer Lighthouse".

### Force a fresh full file

**Actions → Job index → Run workflow →** tick **full**. Normally a new base is written automatically
once a week, or when the changes add up to half the base.

---

## How the build works (for reference)

1. **Restore** `data/builder.db` from the Actions cache (kept between runs; GitHub drops cache
   entries unused for 7 days). If it's gone, seed it from the published base
   (`python -m lighthouse index seed`).
2. **Discover** boards for the companies list (only those due a re-check), **apply the board lists**
   (the CSVs are the truth on the builder), then **fetch** all enabled boards. Unchanged boards and
   postings are skipped by hash. Then the usual steps run: de-duplication, field extraction (pay,
   years asked, visa, clearance, degree, job function, seniority, employment type, skills) and
   ghost scoring.
3. **Export.** Each posting has a fingerprint of everything readers get (and separate ones for its
   search text and its description). Changed fingerprints are stamped with the build time, and:
   - **new base** (first build, weekly, `full`, a new rules version, changes > half the base, or 40 deltas):
     a ready-to-use database → `base-<time>.db.xz`. Every posting, a search index (index only, no second
     copy of the text), the list indexes, and each description compressed against a dictionary of its
     board's common text: the company's "About us", benefits and EEO text (~5x smaller than plain
     text, still readable one row at a time).
   - **otherwise:** postings changed since the previous build → `delta-<time>.db.xz`, added to the
     chain of deltas the manifest lists. A user downloads only the deltas they haven't applied, in
     order. If they missed so many that the deltas would outweigh the base, they get the base
     instead. Descriptions and search text are included only where they changed. If nothing
     changed, no file is written.

   The files are written by whitelist (jobs, boards, rule extractions, history, coverage), so nothing
   personal can be in them. Ghost reasons are stored as dates ("open since 2026-08-26"), not day
   counts, so an unchanged posting doesn't change every day.
4. **Publish** data files first and `manifest.json` last, then save the builder database to the cache.
   If publishing fails, the cache isn't saved. The next run also compares the published manifest with
   its own state and writes a fresh base if they disagree, so readers can never get a delta that
   doesn't match their base.

## Limits to keep in mind

| Limit | Value | When it matters |
|---|---|---|
| Job run time | 6 h max (`timeout-minutes: 340`) | first build of a large list; later runs only fetch what changed |
| Release file size | 2 GiB | far off |
| Actions cache | 10 GB per repo | builder DB is ~1–2 GB; old entries are evicted automatically |
| Inactivity | scheduled workflows pause after 60 days without repo activity (commits) | GitHub emails you; click **Enable workflow** on the Actions tab, or push any commit |
| Failure emails | sent to whoever last edited the `cron:` line | that's you |

## Troubleshooting

| Symptom | Fix |
|---|---|
| *Publish* fails with 403 | Step 4: Workflow permissions → Read and write |
| Users see "no job index published there yet" | the first build hasn't finished, or the repo is private (step 3) |
| Users see "needs a newer Lighthouse" | expected after a schema change; they need the new version |
| Users see "checksum mismatch" | a build was publishing while they downloaded; it fixes itself on the next check |
| Build is slow or times out | lower `--detail-calls` in the workflow, set `LIGHTHOUSE_COUNTRIES`, or switch big Workday boards off |
| Many boards fail with HTTP 429 | lower `per_host_requests_per_second` in `config/sources.toml` |

## Testing a build locally

```bat
python -m lighthouse index build --out dist --db data\test-builder.db
python -m lighthouse index import dist\base-<time>.db.xz
```

The first command finds boards and fetches everything into a separate database (it never touches your own
data). The second installs the result in your app the way a download would.
