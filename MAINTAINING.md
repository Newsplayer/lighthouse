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

### 5. Countries in the index

The index covers the **US, Canada and Mexico** (`US,CA,MX`). Postings located only elsewhere are
skipped before any description download, which makes builds faster and downloads smaller. A
posting with no recognisable country ("Remote", "Multiple locations") is kept. A bare city name is
never placed abroad when a North American city has that name too.

To change the scope, add a repository variable under **Settings → Secrets and variables →
Actions → Variables**: `LIGHTHOUSE_COUNTRIES` = e.g. `US` or `US,CA,MX,GB`. Users can narrow
further with their Profile countries, but they can't get countries the index left out.

### 5b. The open model that reads what the rules can't (nothing to set up)

The free rules read every filter field. When a posting clearly talks about one of them but no rule can
read the answer (sponsorship in unusual wording, years written oddly, a pay band we can't parse), a free,
open model reads that posting on the build machine itself. There's no key, account or cost.

- **What runs:** llama.cpp's server with Qwen3.5 2B (Apache-2.0), on the runner's CPU, in the *fill*
  job after the index and the site are published, so it never delays new postings. What it fills goes
  out with the next build. It's asked only about the unsettled fields, and shown only the sentences
  around them (about a tenth of the posting).
  An answer is kept only when the model quotes the words it read and the quote is really in the text;
  numbers must appear in their quote. It fills empty fields only, never overrides a rule answer, and
  reads each description at most once. Filled fields are marked `"by": "model"`.
- **Job categories:** a title no rule recognises takes the category of the most similar titles the
  rules did place, from a small embedding model (model2vec). If those titles don't clearly agree, the
  category stays unknown. These are marked `"by": "titles"`.
- **How long:** 60 minutes per build, newest postings first. That's 700–1,500 postings an hour,
  depending on the machine GitHub assigns, so about 2,800–6,000 a day, against the 2,500 or so new
  ones a day that need it. To change it, add a repository variable `LIGHTHOUSE_LLM_MINUTES`.
- **Setup:** none. The first *fill* job runs `scripts/get_llm.sh`, which builds llama.cpp and downloads
  the two models (1.3 GB; about 10 minutes). They're kept in the Actions cache, so later runs start at
  once. If the *fill* job fails, nothing is lost: the next build starts from the build job's database,
  and the rule answers stand.
- **Changing the model:** edit the pins in `scripts/get_llm.sh` (the cache key follows the file).
  First compare models with `scripts/eval_fill.py` through the network probe (see "Trying something
  against real careers sites" below).
- **Progress:** the manifest's `llm` entry shows how many descriptions the model has read, and how
  many are still waiting.

### 6. Turn on Issues (for board suggestions)

**Settings → General → Features → Issues** ✓. The app's **Suggest a board** button opens a
pre-filled issue here.

### 7. Run the first build

**Actions → Job index → Run workflow → Run workflow.** The first run fetches for 40 minutes and
publishes what it has (about an hour in all). Boards it didn't reach go first in the next run, and big
Workday sites fill in their descriptions over the next few days. When it's green:
- **Releases → Job index** (tag `job-index`) has `manifest.json` and a `base-<time>.db.xz`. Each build's
  files get their own names; the manifest says which are current, and older ones are removed after it's uploaded.
- The *Publish* step's summary reads like `307717 open roles (485693 postings) from 852 boards; new
  base-….db.xz: 514519 rows, 537.6 MB`,
  then how full each filter column is (`Filled for this % of open roles`) and the `jobs.csv.gz` row count.

After that it runs every 6 hours by itself, plus whenever a push to `main` changes `config/`,
`lighthouse/` or `requirements.txt`. It runs on `main` only: started on another branch it does
nothing, because it would publish that branch's data to everyone (test builds: the network probe below).

### 8. Check it from the app

Restart your local Lighthouse (it's a reader too now), or run `python -m lighthouse index import`.
The **Sources** page shows "Jobs as of … · N open roles from M boards".
`python -m lighthouse index status` prints the same.

The first import replaces your locally fetched jobs with the index. Your profile, saved/applied/hidden
marks and Claude verdicts carry over: they're keyed by posting, and the postings are the same ones.

### 8b. The website (GitHub Pages)

**Settings → Pages → Build and deployment → Source: GitHub Actions** (once). After each build the
workflow writes the website from the builder database (`python -m lighthouse site`, step *Write the
website*), and the *deploy-site* job publishes it at `https://<owner>.github.io/<repo>/`. Each deploy
replaces the whole site at once, so the site is never down during a deploy.

- **Pages of visitors who already have the site open** still name the previous build's search and
  job files, which the deploy removed. When one is missing, the page loads the new build and draws
  the list again. After the weekly list (when jobs are renumbered), a job opened at that moment asks
  to be opened again.
- **If the website step or the deploy fails,** the job index is still published and the cache still
  saved. The site keeps its previous version, and the *Build* log shows why. The next build fixes it.
- **What visitors download per build** is only the change file. What's regenerated each build (search
  and job files) is redone from cached excerpts in under a minute.

**Changing only the page (`site/`) doesn't need a build.** Each build keeps the website it wrote in
the Actions cache. A push to `main` that changes `site/` runs the *Website* workflow
(`website.yml`), which puts the new page files on the last build's job data and deploys in a minute
or two: no crawling, and the builder database isn't touched. Its *Run workflow* button does the same
for whatever `main` has.
- If the push also changes `lighthouse/` (how the job files are written), the *Website* run deploys
  nothing and says so in its summary. That push starts a *Job index* build, which writes everything
  again and puts the change live in about an hour.
- A build that was already running when you pushed uses the newest page files from `main` too, so it
  doesn't put the old page back.

### 9. Optional: protect `main`

**Settings → Rules → Rulesets → New branch ruleset:** target the default branch, enable
**Restrict deletions** and **Block force pushes**. Only you can push (you're the only collaborator:
**Settings → Collaborators**), so this just guards against accidents.

**Branches are tidied by themselves** (`tidy-branches.yml`): after each push to `main`, and daily,
branches whose work is all on `main` are deleted, and so are `probe/…` branches with no new commit
for a day. A branch with work that isn't on `main` is never touched. Each run lists what it deleted.

---

## Trying something against real careers sites

Most development sandboxes (including Claude Code's) can't reach careers sites. Push a script as
`probe/run.py` on a branch named `probe/<topic>`. The **Network probe** workflow runs it from
GitHub's network; read its output in the run's log. Add an empty `probe/run-build` file to run a
full test build instead, or an empty `probe/run-site` file to get the last build's database first
(read only) so `probe/run.py` can, for example, write and time the website on real data. Nothing is
published either way. Delete the branch when done.

## Day to day

### Look at the data

- **As a spreadsheet:** download `jobs.csv.gz` from **Releases → Job index**. One row per open role,
  one column per filter (the column list is in the README).
- **As a database:** your app's `data/index/index-*.db` is the published index. Open it in DB
  Browser for SQLite and browse the `postings` view (same columns), or query `jobs` directly.
- **How complete it is:** the manifest's `fill` entry says, per filter, the share of open roles it's
  known for, and how many have a description. Many postings simply don't mention a field.

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
- **You changed the field-extraction rules (`RULES_VERSION` in `workers.py`) or the filter labels
  (`DERIVE_VERSION` in `classify.py`):** the next build re-reads every description once (not just the
  boards that changed) and writes a fresh full file by itself (the manifest's rules version no longer
  matches). Earlier gap fills (the open model's, or Claude's from before 2026-11-09) are kept, not read again.
- **0.8.0** (schema v7, builder only): two misses to close a posting on paged listings, a role's
  places on its canonical row only, one description per role in the published index, a 40-minute
  fetch limit, and the `postings` view plus `jobs.csv.gz`. The first build after the upgrade
  re-clusters everything once and writes a fresh base. The manifest's `jobs` now counts open roles
  (what every app labels it and what its lists add up to); `postings` counts every open listing.
  Older apps keep reading the index; on them, a duplicate listing's own page shows no description
  (its role's main listing does).
- **0.7.0** adds filter columns and deletion records (schema v6). Older apps keep reading new indexes
  (they ignore the new columns and deletions), and 0.7 reads older indexes (it adds the columns, empty).
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
   postings are skipped by hash.
   - **Within the hour.** 48 boards run in parallel, stalest first (whatever wasn't reached last
     time), and among those the biggest first. After 40 minutes (`--fetch-minutes`, or the
     `LIGHTHOUSE_FETCH_MINUTES` variable) no new board or description call is started; boards not
     reached keep their postings and lead the next run. Workday boards are fetched every other run
     (11 hours apart), and each Workday data center gets 3 requests a second.
   - **Pushing back is respected.** A bot-check page, 403, 429 after backoff or a run of CDN edge
     errors counts against the board's group: one Workday data center (wd1, wd5...), one shared API
     host (Greenhouse, Lever...), or the board's own site. After three, the group is left alone for the
     rest of the run: its other boards wait, their postings stay open, and they go first next run.
     Nothing is retried around a block, and the user agent always says what the builder is. Then the usual steps run: de-duplication, field extraction (pay,
   years asked, visa, clearance, citizenship, degree, job function, seniority, employment type, skills,
   benefits, travel, languages) and ghost scoring.
   - **Closing and deleting.** A posting its board no longer lists is closed: at once on boards that
     answer in one response (Greenhouse, Lever, Ashby, Workable, Recruitee, sitemaps), and on paged
     listings (Workday, Oracle, SmartRecruiters...) once it's missing two runs in a row, since a shifting
     order can skip a posting between pages. If a board with 20+ open
     postings suddenly lists fewer than half of them, the closures are held for one build: if the next
     fetch agrees, they're closed then. A posting closed more than `closed_retention_days` (45) ago is
     deleted once no other listing in its cluster is open, or after a year regardless. Each deletion
     leaves a tombstone that the next delta carries, so readers delete it too. Postings you marked
     yourself (local mode) are never deleted.
   - **Open-model gap fill** (step 5b; the *fill* job, after publishing). First, titles the rules
     couldn't place get a category from the most similar placed titles. Then the rules' list of gaps
     (per description, the fields it talks about that they couldn't settle) goes to the open model,
     newest open, de-duplicated, non-ghost postings first, for 60 minutes. Grounded answers are merged
     into the extraction for the empty fields only, marked `"by": "model"`, and the builder database is
     saved again; the next build publishes them. If the job fails, the rule answers stand. Descriptions
     already read are recovered from the published base when seeding.
3. **Export.** Each posting has a fingerprint of everything readers get (and separate ones for its
   search text and its description). Changed fingerprints are stamped with the build time, and:
   - **new base** (first build, weekly, `full`, a new rules version, changes > half the base, or 40 deltas):
     a ready-to-use database → `base-<time>.db.xz`. Every posting, a search index (index only, no second
     copy of the text), the list indexes, the `postings` view, and one description per role (its
     canonical listing's; duplicate listings show the same text), each compressed against a dictionary
     of its board's common text: the company's "About us", benefits and EEO text (~5x smaller than
     plain text, still readable one row at a time). A role's places are stored once, on its canonical
     row, and its search entry covers all of them. Compressed with `xz` on every core.
   - **otherwise:** postings changed since the previous build → `delta-<time>.db.xz`, added to the
     chain of deltas the manifest lists. A user downloads only the deltas they haven't applied, in
     order. If they missed so many that the deltas would outweigh the base, they get the base
     instead. Descriptions and search text are included only where they changed. If nothing
     changed, no file is written.

   - **`jobs.csv.gz`:** every open role with a column per filter (the `postings` view), replaced each build.

   The files are written by whitelist (jobs, boards, rule extractions, history, coverage), so nothing
   personal can be in them. Ghost reasons are stored as dates ("open since 2026-08-26"), not day
   counts, so an unchanged posting doesn't change every day.
4. **Publish** data files first and `manifest.json` last.
5. **Write the website** (`lighthouse/site.py`), then save the builder database to the cache. The
   website is static files, so there's no server:
   - **Job list:** one entry per role, as columns, in a weekly file. A small "changed since then" file
     comes with every build: added or changed roles, and ids gone.
   - **Keyword index:** split into files by the first two letters.
   - **Job details:** in files of 128. Each holds an excerpt that skips company boilerplate, every
     apply link, places and ghost reasons.
   - **Browser:** it keeps the weekly file in its cache storage, so a return visit downloads only the
     changes.
   - **Builder database:** it remembers each role's id for the week (`site_ids`) and keeps the weekly
     file (`site_files`), so it travels in the Actions cache.

   A new weekly file is written after 7 days, or when the changes exceed a quarter of it.
   If publishing fails, the cache isn't saved. The next run also compares the published manifest with
   its own state and writes a fresh base if they disagree, so readers can never get a delta that
   doesn't match their base.

## Limits to keep in mind

| Limit | Value | When it matters |
|---|---|---|
| Job run time | about an hour (40-minute fetch limit); `timeout-minutes: 100` only stops a stuck run | a run after a rules change re-reads every description: ~15 minutes more |
| Release file size | 2 GiB | far off |
| GitHub Pages site | 1 GB; a deploy must finish in 10 minutes; ~100 GB/month traffic (soft) | 185 MB at 227k roles; a first visit is ~5 MB, so ~20k first visits a month |
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
| Build is slow or times out | lower `LIGHTHOUSE_FETCH_MINUTES` (default 40), set `LIGHTHOUSE_COUNTRIES`, or switch big boards off |
| Many Workday boards fail with "Pushed back" | Workday's bot protection answered with block pages; that data center was left alone for the run and goes first next time. If it lasts for days, lower `SHARED_LIMITS` in `net.py` or raise `WORKDAY_REFRESH_HOURS` in `snapshot.py`. Don't work around it |
| The log says "time limit (40 min) reached: N boards not started" | normal on a first build or after a block; they go first next run |
| Many boards fail with HTTP 429 | lower `per_host_requests_per_second` in `config/sources.toml` |
| Manifest's `llm` entry stays at 0 descriptions | the *fill* job failed: its *Get the open model* step says why (a download or the llama.cpp build). It's retried next build |
| `llm.waiting` keeps growing | more new postings than the reading time covers; raise `LIGHTHOUSE_LLM_MINUTES` (the *fill* job gets longer; publishing doesn't) |
| *deploy-site* fails with "Timeout reached" | GitHub was slow copying the files; it waits up to 30 minutes now, and the next build deploys anyway |
| A board's postings all stayed open although its site emptied | expected for one build (mass-closure hold); the next build closes them |
| *deploy-site* is skipped | the *Write the website* step failed (its log says why); the index was published anyway |
| *deploy-site* fails with "Not Found" / Pages not enabled | Settings → Pages → Source: GitHub Actions (step 8b) |
| A page change pushed to `main` isn't live; the *Website* run deployed nothing | its summary says why: no build has kept a website yet (the first build after this workflow was added does), or the push also changed `lighthouse/` (wait for the next build, or *Run workflow* on *Job index*) |

## Testing a build locally

```bat
python -m lighthouse index build --out dist --db data\test-builder.db
python -m lighthouse index import dist\base-<time>.db.xz
```

The first command finds boards and fetches everything into a separate database (it never touches your own
data). The second installs the result in your app the way a download would.
