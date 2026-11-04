# Maintaining the job index

For the maintainer of `Newsplayer/lighthouse`. Users don't need any of this: they download the
app, start it, and it reads the index this repo publishes.

```
employer job boards ──► GitHub Action (every 6 h) ──► release "job-index" ──► every user's Lighthouse
                         the only thing that writes      manifest.json            downloads, never writes
                         job data                        base.db.xz  (weekly)
your commits ──────────► (board list, code)              delta.db.xz (each run)
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
- **Releases → Job index** (tag `job-index`) has `base.db.xz` and `manifest.json`.
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

## Day to day

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
- **You changed the field-extraction rules (`RULES_VERSION` in `workers.py`):** run the workflow with
  **full** ticked, so everyone gets a fresh full file.

### Force a fresh full file

**Actions → Job index → Run workflow →** tick **full**. Normally a new base is written automatically
once a week, or when the changes add up to half the base.

---

## How the build works (for reference)

1. **Restore** `data/builder.db` from the Actions cache (kept between runs; GitHub drops cache
   entries unused for 7 days). If it's gone, seed it from the published `base.db.xz`.
2. **Apply the board list** (CSV is the truth on the builder), then **fetch** all enabled boards.
   Unchanged boards and postings are skipped by hash, and the usual de-duplication, field extraction,
   years asked and ghost scoring run.
3. **Export.** Each posting has a fingerprint of everything readers get. Changed fingerprints are
   stamped with the build time, and:
   - **new base** (first build, weekly, `full`, or changes > half the base): every posting →
     `base.db.xz`
   - **otherwise:** postings changed since the base → `delta.db.xz` (cumulative, so a user who missed
     a few builds still needs only the latest one)

   The files are written by whitelist (jobs, boards, rule extractions, history), with descriptions as
   plain text and the whole file xz-compressed. That comes to about 1 KB per posting.
4. **Publish** data files first and `manifest.json` last, then save the builder database to the cache.
   If publishing fails, the cache isn't saved. The next run also compares the published manifest with
   its own state and writes a fresh base if they disagree, so readers can never get a delta that
   doesn't match their base.

## Limits to keep in mind

| Limit | Value | When it matters |
|---|---|---|
| Job run time | 6 h max (`timeout-minutes: 300`) | only if you add thousands of boards |
| Release file size | 2 GiB | far off (~1 KB/posting) |
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
python -m lighthouse index import dist\base.db.xz
```

The first command fetches everything into a separate database (it never touches your own
`data\lighthouse.db`). The second merges the result into your app the way a download would.
