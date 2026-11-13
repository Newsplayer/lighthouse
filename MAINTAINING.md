# Maintaining Lighthouse

For the maintainer of `Newsplayer/lighthouse` (or of a fork). Visitors need none of this: they open
the website, or download the public database.

```
config/companies.csv ─► careers pages ─┐
config/boards/*.csv ───► job boards ───┴► GitHub Action (every 3 h) ─► release "job-index": jobs.db.xz, jobs.csv.gz
                                           finds boards, fetches,     ─► the website (GitHub Pages)
your commits ──────────► (lists, code)     cleans, analyses
```

- **Who can change what:**
  - Only the Action writes job data.
  - Only people with write access to the repo (you) change the board lists and code.
  - Everyone else can read it, and suggest companies through issues.
- **Cost:** $0. Public repos get free, unlimited Actions minutes, and release downloads and Pages are free.

---

## One-time setup (about 15 minutes, then a first build of about an hour)

### 1–3. A public repo with the code

Fork or push this repo, and make it public: **Settings → General → Danger Zone → Change repository
visibility → Public.** The release files and GitHub Pages are only free and open on a public repo.
Before switching, check that the history holds no database: `git log --all --stat -- data` should
list no `.db` files (`data/` is git-ignored).

### 4. Check the Actions settings

**Settings → Actions → General:**
- **Actions permissions:** "Allow all actions and reusable workflows". The workflow uses only
  GitHub's own `checkout`, `setup-python` and `cache`.
- **Workflow permissions:** leave the default. The workflow asks for `contents: write` (and
  `issues: write`, for the one issue that lists broken boards) itself. If the
  *Publish* step later fails with `Resource not accessible by integration` (403), pick
  **Read and write permissions** here.
- **Fork pull request workflows:** "Require approval for all outside collaborators". This workflow
  doesn't run on pull requests anyway, so a pull request can never publish anything.

### 5. Countries covered

The build covers the **US, Canada and Mexico** (`US,CA,MX`). Postings located only elsewhere are
skipped before any description download, which makes builds faster and the files smaller. A
posting with no recognisable country ("Remote", "Multiple locations") is kept. A bare city name is
never placed abroad when a North American city has that name too.

To change the scope, add a repository variable under **Settings → Secrets and variables →
Actions → Variables**: `LIGHTHOUSE_COUNTRIES` = e.g. `US` or `US,CA,MX,GB`. (The website's place names
and metro areas cover the US, Canada and Mexico only.)

### 5b. The open model that reads what the rules can't (nothing to set up)

The free rules read every filter field. When a posting clearly talks about one of them but no rule can
read the answer (sponsorship in unusual wording, years written oddly, a pay band we can't parse), a free,
open model reads that posting on the build machine itself. There's no key, account or cost.

- **What runs:** llama.cpp's server with Qwen3.5 2B (Apache-2.0), on the runner's CPU, in the *fill*
  job after the database and the site are published, so it never delays new postings. What it fills goes
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

### 6. Turn on Issues

**Settings → General → Features → Issues** ✓. People suggest companies there, and the build keeps one
issue listing the boards that stopped answering.

### 7. Run the first build

**Actions → Job index → Run workflow → Run workflow.** The first run fetches for 40 minutes and
publishes what it has (about an hour in all). Boards it didn't reach go first in the next run, and big
Workday sites fill in their descriptions over the next few days. When it's green:
- **Releases → Job index** (tag `job-index`) has `jobs.db.xz`, `jobs.csv.gz` and `manifest.json`. Every
  build replaces them, so their download links never change.
- The run's **summary page** shows how many roles and postings, what was published and how big, the
  boards that failed, and the share of descriptions read for each careers system.

After that it runs every 3 hours by itself, plus whenever a push to `main` changes `config/`,
`lighthouse/` or `requirements.txt`. It runs on `main` only: started on another branch it does
nothing, because it would publish that branch's data to everyone (test builds: the network probe below).

### 8. The website (GitHub Pages)

**Settings → Pages → Build and deployment → Source: GitHub Actions** (once). After each build the
workflow writes the website from the builder database (`python -m lighthouse site`, step *Write the
website*), and the *deploy-site* job publishes it at `https://<owner>.github.io/<repo>/`. Each deploy
replaces the whole site at once, so the site is never down during a deploy.

- **Pages of visitors who already have the site open** still name the previous build's search and
  job files, which the deploy removed. When one is missing, the page loads the new build and draws
  the list again. After the weekly list (when jobs are renumbered), a job opened at that moment asks
  to be opened again.
- **If the website step or the deploy fails,** the database is still published and the cache still
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
- **As a database:** download `jobs.db.xz` from the same release, unpack it (`xz -d jobs.db.xz`) and
  open it in DB Browser for SQLite. Browse the `postings` view (same columns, plus an excerpt), or
  query `jobs` directly. The README lists the tables.
- **How complete it is:** the manifest's `fill` entry says, per filter, the share of open roles it's
  known for, and how many have a description. Many postings simply don't mention a field.
- **How the last build went:** open the run on the **Actions** tab. Its summary page shows the build
  time, postings new and closed, boards that failed, and the share of descriptions read for each
  careers system. The same numbers are in the manifest's `health` entry, and the website shows a short
  version under **Sources**. To print it yourself: `python -m lighthouse index summary dist/manifest.json`.

### Add a large employer by its careers page

Add a row to `config/companies.csv`: `company,group,domain,careers_url,ats,slug,enabled`, e.g.
`Acme Corp,fortune500,acme.com,https://careers.acme.com,,,yes`. Leave `ats` and `slug` empty and
push. The next build opens the careers page and finds the job system behind it: Workday, Oracle,
SuccessFactors, Phenom, iCIMS/Jibe, Eightfold, Greenhouse, Lever, Ashby, SmartRecruiters, Workable,
Recruitee, or a sitemap of job pages with JobPosting data. It checks the board answers, then
fetches it. The result is on the website under **Sources** and in `manifest.json` → `coverage`.

- **Already in a board list:** a company that `config/boards/*.csv` has a board for (same name) is
  covered by that board without any looking up, and the board is switched on. Careers pages often
  hide their job system behind scripts or robots.txt; the hand-checked board is the better answer.
- **Re-checks:** a company whose board works is re-checked every 14 days, sooner if its board
  starts failing, and after 3 days if the board lists nothing for the countries covered. One that
  couldn't be resolved is retried every 3 days. A full build re-checks all.
- **Sitemaps:** when a site publishes one sitemap per country, the US English one is taken.
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
3. Commit and push. The push starts a build; the board's roles are on the website when it finishes
   (about an hour).

### Remove or pause a board

Set its `enabled` column to `no` (or delete the row) and push. Its postings are closed in the next
build and leave the website with it.

### Boards that stopped existing

A board that answers "not found" two runs in a row is switched off automatically. The build's
`manifest.json` lists them under `switched_off`. Fix the slug in the CSV (that counts as a new board)
or set `enabled` to `no`. To retry them all, run the workflow with **full** ticked.

The build also keeps one issue, **Job boards that need a look**, listing boards that were switched off
or haven't answered for two days. It is updated by every build and closed when all boards answer again.

### Check that boards belong to their companies

`python -m lighthouse discover --report` lists the boards in `config/` whose id doesn't look like the
company's name or website. It needs no network. Most are renamed companies or parent brands; a board
that belongs to someone else lists the wrong jobs under this name, so fix or remove it.

### After a code change

Commit and push as usual. A push to `main` that changes `lighthouse/` or `config/` starts a build.

- **You changed the field rules (`RULES_VERSION` in `workers.py`) or the filter labels
  (`DERIVE_VERSION` in `classify.py`):** the next build re-reads every description once, not just the
  boards that changed. Earlier gap fills are kept, not read again.
- **You changed the builder's tables (`SCHEMA` in `db.py`):** bump `SCHEMA_VERSION` and add the step
  from the previous version to `migrate()`. The next build re-processes every company once.
- **You changed the website's files (`FORMAT` in `site.py`) or the place rules (`VERSION` in
  `places.py`):** the next build writes a new weekly job list by itself.
- **You changed the public database's tables or the `postings` view:** say so in the release notes
  and the README; other people's queries read them.

### Look every company up again

**Actions → Job index → Run workflow →** tick **full**. Every listed company's careers page is read
again (normally only those due), and boards that were switched off are retried.

---

## How the build works (for reference)

1. **Restore** `data/builder.db` from the Actions cache (kept between runs; GitHub drops cache
   entries unused for 7 days). If it's gone, seed it from the published database
   (`python -m lighthouse index seed jobs.db.xz`): every posting keeps its history, and its description
   is read again when its board is next fetched.
2. **Discover** boards for the companies list (only those due a re-check), **apply the board lists**
   (the CSVs are the truth on the builder), then **fetch** all enabled boards. Unchanged boards and
   postings are skipped by hash.
   - **Within the hour.** 48 boards run in parallel, stalest first (whatever wasn't reached last
     time), and among those the biggest first. After 40 minutes (`--fetch-minutes`, or the
     `LIGHTHOUSE_FETCH_MINUTES` variable) no new board or description call is started; boards not
     reached keep their postings and lead the next run.
   - **How often.** Builds run every 3 hours. A board that answers in one request (Greenhouse, Lever,
     Ashby, Workable, Recruitee) is read every build. A listing that takes many requests is read
     four times a day, and Workday twice a day (11 hours apart); each Workday data center gets
     3 requests a second.
   - **Workday listings are read whole.** A Workday site serves at most 2,000 results per listing
     (past that it repeats earlier pages). A bigger listing is read in parts, using the site's own
     filters (state, job category, full or part time...), and a part that is still over 2,000 is split
     again. Where the site has a country filter, only the countries the build covers are asked for.
     Pages are read four at a time. A board listed twice in different letter case is read once.
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
     deleted once no other listing in its cluster is open, or after a year regardless. A careers-site
     page that gives its own closing date closes when that date passes.
   - **Open-model gap fill** (step 5b; the *fill* job, after publishing). First, titles the rules
     couldn't place get a category from the most similar placed titles. Then the rules' list of gaps
     (per description, the fields it talks about that they couldn't settle) goes to the open model,
     newest open, de-duplicated, non-ghost postings first, for 60 minutes. Grounded answers are merged
     into the extraction for the empty fields only, marked `"by": "model"`, and the builder database is
     saved again; the next build publishes them. If the job fails, the rule answers stand. Descriptions
     already read are recovered from the published database when seeding.
3. **Write the public files** (`snapshot.export`), new every build:
   - **`jobs.db.xz`:** a plain SQLite database: every listing with its fields and history (`jobs`,
     `events`), an excerpt of each open role (`excerpts`), where each field was read from
     (`extractions`), the boards and the companies list, and the `postings` view (one row per open
     role, a column per filter). No full descriptions: those stay in the builder. Compressed with `xz`
     on every core.
   - **`jobs.csv.gz`:** the `postings` view without the excerpts.
   - **`manifest.json`:** counts, how full each filter is (`fill`), and how the build went (`health`).

   The files are written by whitelist (jobs, boards, rule extractions, history, coverage), so nothing
   else on the build machine can be in them. Ghost reasons are stored as dates ("open since
   2026-08-26"), not day counts, so an unchanged posting doesn't change every day.
4. **Publish** the three files on the `job-index` release, under the same names every build.
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
   If publishing fails, the cache isn't saved, and the next run starts from the previous one's.

## Limits to keep in mind

| Limit | Value | When it matters |
|---|---|---|
| Job run time | about an hour (40-minute fetch limit); `timeout-minutes: 100` only stops a stuck run | a run after a rules change re-reads every description: ~15 minutes more |
| Release file size | 2 GiB | far off |
| GitHub Pages site | 1 GB; a deploy must finish in 10 minutes; ~100 GB/month traffic (soft) | 185 MB at 227k roles; a first visit is ~5 MB, so ~20k first visits a month |
| Actions cache | 10 GB per repo | builder DB is ~3 GB on disk, ~1 GB in the cache, saved twice a build; old entries are evicted automatically. The build compacts the file when 15% of it is free space |
| Inactivity | scheduled workflows pause after 60 days without repo activity (commits) | GitHub emails you; click **Enable workflow** on the Actions tab, or push any commit |
| Failure emails | sent to whoever last edited the `cron:` line | that's you |

## Troubleshooting

| Symptom | Fix |
|---|---|
| *Publish* fails with 403 | Step 4: Workflow permissions → Read and write |
| The build fails at once with "this database is from an older Lighthouse" | the cached builder database predates the current tables: delete the `lighthouse-builder-*` entries under **Actions → Caches** and run the workflow; it seeds from the published database |
| Build is slow or times out | lower `LIGHTHOUSE_FETCH_MINUTES` (default 40), set `LIGHTHOUSE_COUNTRIES`, or switch big boards off |
| Many Workday boards fail with "Pushed back" | Workday's bot protection answered with block pages; that data center was left alone for the run and goes first next time. If it lasts for days, lower `SHARED_LIMITS` in `net.py` or raise `WORKDAY_REFRESH_HOURS` in `snapshot.py`. Don't work around it |
| The log says "time limit (40 min) reached: N boards not started" | normal on a first build or after a block; they go first next run |
| Many boards fail with HTTP 429 | lower `per_host_requests_per_second` in `config/sources.toml` |
| Manifest's `llm` entry stays at 0 descriptions | the *fill* job failed: its *Get the open model* step says why (a download or the llama.cpp build). It's retried next build |
| `llm.waiting` keeps growing | more new postings than the reading time covers; raise `LIGHTHOUSE_LLM_MINUTES` (the *fill* job gets longer; publishing doesn't) |
| *deploy-site* fails with "Timeout reached" | GitHub was slow copying the files; it waits up to 30 minutes now, and the next build deploys anyway |
| A board's postings all stayed open although its site emptied | expected for one build (mass-closure hold); the next build closes them |
| *deploy-site* is skipped | the *Write the website* or *Upload the website* step failed (its log says why); the database was published anyway |
| The *Tests* check on a pull request is red | its log names the failing test or the lint finding; fix it before merging (the builder publishes from `main`) |
| The website's Sources list says a company isn't covered | the reason is in its row. "Uses … which Lighthouse can't read yet" needs a new reader; "No readable job list found" may only need its board pinned in `config/companies.csv` (`ats` and `slug` columns) |
| *deploy-site* fails with "Not Found" / Pages not enabled | Settings → Pages → Source: GitHub Actions (step 8) |
| A page change pushed to `main` isn't live; the *Website* run deployed nothing | its summary says why: no build has kept a website yet (the first build after this workflow was added does), or the push also changed `lighthouse/` (wait for the next build, or *Run workflow* on *Job index*) |

## Testing a build locally

```bash
python -m lighthouse index build --out dist --db data/test-builder.db --fetch-minutes 5
python -m lighthouse site --db data/test-builder.db --out site_out
python -m http.server -d site_out 8000        # then open http://localhost:8000
```

The first command finds boards and fetches for five minutes into a separate database, then writes
`dist/jobs.db.xz`, `dist/jobs.csv.gz` and `dist/manifest.json`. The second writes the website from it.
