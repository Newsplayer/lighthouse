# Lighthouse roadmap: from v0.6 to the most effective free job board

This was written 2026-11-07 for Claude Code, and updated the same day with the owner's
decisions (§8). It compares Lighthouse v0.6 (PR #1, branch `big-companies`, verified by a full
test build on 2026-11-06) with what the goal requires.

**Direction (owner, 2026-11-07):** the top priority is a functional, effective job aggregator.
- **Hosting:** Lighthouse becomes a **website hosted on GitHub Pages**, so users install and
  download nothing (P1).
- **AI fit:** deferred.
- **The local app** keeps working from the published index but gets no new features.

The goal is:
- **The postings:** every real, open, de-duplicated role at the listed employers in the US,
  Canada and Mexico, updated as close to continuously as polite crawling allows.
- **The user:** sees which roles they're eligible for and fit, and applies on the employer's site.
- **Cost:** nothing for users, nothing to install, and no user ever needs a key. The maintainer may
  use a free-tier LLM API key in the builder (§5).

The industry practices it is checked against come from Job Boardly's *Job Board Aggregator Guide*.
Work top to bottom.

---

## 1. What "optimal" means: targets

| Measure | Target | Now |
|---|---|---|
| Listed companies covered (`config/companies.csv`) | ≥ 90% | 376 / 700 (54%) |
| Fortune 500 group covered | ≥ 90% | 263 / 466 (56%) |
| Open postings with analysed descriptions | ≥ 95% | 30% overall: Workday 11%, Oracle 4%, SmartRecruiters 16% |
| A new posting reaches users | ≤ 2 h | ≤ 6 h + up to 1 h (builds every 6 h, app checks hourly) |
| A posting that closes leaves the default list | ≤ 6 h; instantly when the user opens it | ≤ 7 h (but see P0-1) |
| Wrongly hidden as "not eligible" (the worst error) | ≤ 3% on the eval set | not measured |
| Install / download needed | nothing (website) | website built (P1-1 ✓); goes live with the first build after merge |
| First visit to the website | ≤ 6 MB | 1.7 MB for 73k roles, including a search and opening 3 jobs (≈ 6 MB at 255k) |
| Each later visit / update | ≤ 0.5 MB | 0.07 MB after an update of 1,361 changed + 334 closed roles |
| Filtering and sorting in the page | < 50 ms | 2–7 ms on 73k roles |
| Opening a job | < 300 ms | 59 ms (local server) |
| Money / keys users need | none | none ✓ |

## 2. Where v0.6 is

- **Pipeline:**
  - Discovery reads 700 companies' careers pages and finds the system behind each one.
  - Readers cover Workday, Oracle, SuccessFactors (RSS sitemap), Phenom, iCIMS/Jibe, Eightfold,
    Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Amazon, and any site with
    schema.org JobPosting pages.
  - There are also 926 direct boards in `config/boards/top-companies.csv`.
  - Each posting is de-duplicated (exact fingerprint plus MinHash near-duplicates), analysed by
    free rules (pay, YOE, visa, clearance, degree, function, seniority, employment type, skills),
    and ghost-scored.
  - It's then published as a ready-to-use SQLite index: a weekly base plus a chain of per-build
    deltas.
- **Country scope:** US, Canada and Mexico by default (`job-index.yml`; repo variable
  `LIGHTHOUSE_COUNTRIES` overrides).
  - Location rules now include a 77k-name GeoNames city list. Unknown locations on real data fell
    from 17% to 10%.
  - A bare name a North American city also has is never placed abroad.
  - On the real 124k index, 67% of postings stay in scope.
- **Test build, 2026-11-06** (worldwide, before the scope):
  - 461,682 open postings from 850 boards, in 2 h 06 min.
  - Base 267 MB (xz); builder DB 2.6 GB.
  - Workday errors fell from 117 to 5 after the shared rate budget.
- **Real 124k-posting index, measured 2026-11-07:**

  | Measure | Full descriptions | 600-character excerpts |
  |---|---|---|
  | Download | 149 MB | 51 MB |
  | On disk | 421 MB | 286 MB |

  - A delta with 2,000 changed postings (half with new descriptions) is 2.5 MB. An update with one
    change is 42 KB. Exporting a delta takes 8 s.
  - Installing the index takes 6 s; pages load in 2–50 ms.
- **Website (P1-1, built 2026-11-07; branch `website`):**
  - Measured on the real 124k-posting builder database: 72,812 roles in scope.
    - Generated in 37 s; the site is 50 MB.
    - The job list is 8.6 MB, 1.66 MB gzip.
  - In Chromium on a local server:

    | Measure | Result |
    |---|---|
    | First results | 0.38 s |
    | Filter or sort | 2–7 ms |
    | Opening a job | 59 ms |
    | First visit (search + 3 jobs) | 1.71 MB gzip |
    | Return visit after a 2% update | 0.07 MB |
  - Tests: Node runs the page logic against the Python logic (19 filter combinations; eligibility
    reasons for 120 jobs × 40 profiles; search words), and a Chromium test drives the page.
  - Full-scale test build in Actions (fresh database, all 700 companies; run 42406810708):
    - Build: 73 min, 234,917 postings from 367 companies.
    - Site: 132,951 roles in the US, Canada and Mexico from 407 companies, generated in 43 s.
    - Size: 70.8 MB in all; the job list is 2.35 MB gzip; the largest search-word file is 57 KB
      gzip.
    - Only 27% of descriptions were read in that first run, so the site grows as later runs read
      the rest.
- **Owner's changes on `main` (2026-11-07):**
  - **Builder gap fill:** Claude Haiku with the maintainer's key, capped at $10/month, reads only
    the postings whose filter fields the rules couldn't settle (`llm_fill.py`).
  - **Mass-closure hold:** this is P0-1.
  - **Old postings deleted:** after 45 days closed.
  - **Builds of about an hour:** a 40-minute fetch limit, stalest boards first.
- **Static-site prototype** (measured 2026-11-07 on the real 124k index): 68,365 jobs after
  de-duplication and the US/CA/MX scope.
  - Everything needed to list and filter them (title, company, location, dates, function,
    seniority, type, workplace, pay, years asked, visa/clearance/citizenship/degree, ghost score):
    10.6 MB, 1.4 MB gzip.
  - Keyword index over title, company, skills and location: 18k words in 831 files, 0.9 MB in
    total, the largest 26 KB.
  - Job details (excerpt, all apply links, skills, ghost reasons) in 128-job files: 41 MB on the
    site, ~25 KB gzip per file.
  - Generated in 30 s.
- **Local app (reader):**
  - Eligibility is computed at query time (`profile.eligibility_sql`).
  - Fit comes from Claude (API key, or the user's Claude app via MCP).
  - The job page links every open copy of the role, with tracking parameters removed.

## 3. Freshness: how updates reach users

Job boards change all day, so freshness is a core feature.

1. **The builder re-reads every board.**
   - Today: every 6 h. Unchanged boards cost one request (response hash). Unchanged postings
     aren't re-analysed.
   - Plan (P2-1): back-to-back hourly runs with a cheap check first, so new postings appear within
     ~1–2 h. A full listing runs only when a board changed, or every 6 h to catch closures.
2. **Each build redeploys the website** (P1-1).
   - The job list ships as a weekly base file plus a small "changed since the base" file. Both
     have per-build names.
   - Browsers keep the base cached and fetch only the small file when it changes.
3. **The page checks for a newer build.**
   - It fetches a ~1 KB `latest.json` on each visit and every 15 minutes while open.
   - When there's a newer build, it applies the change file in memory and shows the new jobs as
     "new since your last visit". No reload, no waiting.
4. **The local app** (frozen) keeps working as today: a chain of per-build deltas on the
   `job-index` release.
   - Measured: 42 KB fixed + ~1.3 KB per changed posting with full text; less with excerpts.

**Rough budget** at ~255k jobs (US/CA/MX, one row per role), assuming ~3% change per day
(measure it, P0-5):
- A first visit is ~5 MB.
- A visit a day later is ~0.2–0.5 MB.
- A tab left open all day downloads about the same: only what changed.
- GitHub Pages' soft limit of 100 GB/month covers ~20k first visits a month plus many more return
  visits.

## 4. Lighthouse vs the industry guide

| Guide practice | Lighthouse now | Gap / action |
|---|---|---|
| Collect via APIs / "job wrapping" (an employer's own ATS feed), not scraping | ✓ Only the public feeds careers sites load, and robots.txt respected | Coverage: see P2-4 |
| Backfill from bought feeds | Not used (paid) | Non-goal |
| Normalize titles, locations, salary, job type | Pay per hour/year, function, seniority, employment type, country (GeoNames cities) | No standard occupation (P3-2), no coordinates or radius (P3-4), no currency conversion for MXN/CAD (P3-6) |
| De-duplicate across postings | ✓ Fingerprint + MinHash per company; all apply links kept | None |
| Re-crawl; expire filled or removed jobs | ✓ Every 6 h; a posting missing from its feed closes | Faster cycle (P2-1); `validThrough` / end dates ignored (P0-2) |
| **"Kill and fill": keep the last-known-good set if a feed comes back empty or partial** | ✗ A successful but shrunken feed closes every missing posting; they "reopen" next run, which pollutes the repost and ghost signals | **P0-1** |
| Ghost / spam controls, audits | ✓ Multi-signal ghost score with reasons | No measurement (P0-3). User reports: not for now (§8) |
| Keyword search + filters (date, type, industry, location, salary, experience) | ✓ FTS5 + all of those except industry and radius | On the website (P1-1); richer filters (P3-1); radius, industry (P3-4, P3-5) |
| Ranking by relevance, recency, preferences | Sort by date / pay / YOE | Relevance for keyword search on the website (P1-1) |
| AI matching | Claude only (optional, local app) | Deferred (§6 "Later") |
| Clean cards that link to the original | ✓ | None |
| Search without registering | Local app only (install needed) | **Website (P1-1)** |
| Check application links | Postings come from live feeds | Sitemap pages (P0-2) |
| Copyright: avoid reproducing descriptions verbatim | ✗ Full descriptions are republished in a public release | Excerpts only (P1-2, decided) |
| Privacy (GDPR/CCPA) | ✓ No server, no accounts, personal data local | Keep it that way |
| Source changes break crawlers | Per-board failure counts, auto switch-off, 14-day re-discovery | Alerts and health metrics (P2-3) |
| Metrics / KPIs | Coverage table on the Sources page | Build health in the manifest, Actions summary and the website (P2-3) |
| SEO pages, accounts, monetization, Stripe | n/a (private local app) | Non-goal |

## 5. LLM and ML options (verified 2026-11-07)

**Rules:**
- Anything an LLM does must also work without it. Rules are always the base layer.
- Builder LLM output adds filter fields. Reader LLMs refine a ranking or explain a verdict. Neither
  ever gates what the user can see.
- One verdict schema serves every model: tier plus per-requirement met / missing / unclear, the same
  as `claude_api.py`. Verdicts are cached by (cluster, description hash, profile hash, model,
  prompt version).
- The injection rules (no tools, strict JSON schema, escaped output) apply to every model.

### In the builder (GitHub Action, runs once for everyone)

| Option | Use | Verdict |
|---|---|---|
| **Gemini API free tier, with the maintainer's key** (repo secret) | Filter fields and a short summary per posting (P3-1) | **Adopt.** About 500 requests a day per model for Flash-Lite as of Nov 2026, with separate quotas per model. At 20–25 postings per request that's ~10–12k postings a day per model. Free-tier content may be used by Google to improve its products; send only public posting text. Use the OpenAI-compatible endpoint, so the provider can be swapped. |
| Groq free tier (maintainer's key) | Small second provider | 1,000 requests/day but 200k tokens/day per model, so only ~250 postings a day. Fallback only. |
| **model2vec static embeddings** (`model2vec` 0.10, numpy only, MIT; e.g. `potion-base-8M`, `potion-retrieval-32M`) | Semantic search, "more like this", resume similarity, title → occupation | **Adopt (P3-3).** Up to ~500× faster than the original transformer on CPU: minutes for every posting, incremental after that. Ship 256-bit binary vectors (~32 B per posting). |
| ONNX sentence-transformers (bge-small / e5-small via `fastembed`) | Higher-quality embeddings | Only if the eval set shows model2vec isn't enough; ~100× slower |
| Generative LLM on the runner's CPU (llama.cpp / Ollama) | Field extraction | **Reject at scale:** ~7–10 tokens/s, so it can't keep up with thousands of new postings a day |
| GitHub Models via `GITHUB_TOKEN` | — | **Gone:** retired 2026-07-30 |
| Cerebras, OpenRouter free, Cloudflare Workers AI, SambaNova | — | Trials or tiny daily limits (50 requests, 10k "neurons", 20 requests); not worth the code |

### On the user's machine: deferred with AI fit (optional, found automatically, never required, no sign-in)

| Option | Use | Notes |
|---|---|---|
| **Rules + embeddings fit score** (deferred) | Default "Strong / Possible / Weak" for every eligible job, with reasons | No model download. Probably most of the value. |
| **Local AI app on this computer:** Ollama (`127.0.0.1:11434`), LM Studio (`:1234`), llama.cpp server (`:8080`) | Fit verdicts for a shortlist (≤ 50), in the background | Lighthouse looks for these on localhost (OpenAI-compatible API), so there's nothing to sign in to and no key. Loopback only. Suggested models: Qwen3.5 4B (JSON extraction), Phi-4-mini (structured output), SmolLM3 3B. CPU-only laptops take ~30–90 s per job. |
| **Chrome's built-in model** (Prompt API, Chrome 148+) | Fit check of the job being viewed; plain words → filters | Runs in the page, nothing to install or sign in to. Needs ≥ 22 GB free disk and > 4 GB VRAM, or 16 GB RAM + 4 cores. JSON schema via `responseConstraint`. Handle `downloading`/`unavailable`. |
| In-browser WebGPU (WebLLM, Transformers.js) | Same, other browsers | Big one-time download; needs a WebGPU GPU. Low priority. |
| **User's Claude app via MCP** (exists) | Best-quality fit, on the user's own Claude plan, no key | Keep |
| Claude API key (exists) | Same, batch | Optional, paid by the user. Never required. |

**Rejected:**
- For users: any service that needs the user to make a key or an account.
- Anywhere: anonymous "keyless" hosted endpoints (no uptime or privacy guarantees).

## 6. Work, in order

The priority is a functional, effective aggregator: correct data first, then the website, then
freshness and coverage, then richer filters. Each item lists where the work is and what "done"
means. Keep every change incremental and measured (§2 baselines).

### P0: working and correct

1. **✓ Done on `main` (owner, `hold_mass_closures`).** Shrink guard ("kill and fill"). In `db.upsert_board` (the closing logic near its end), when a
   board that answered successfully lists < 50% of its open postings (and ≥ 20 are missing), or
   zero:
   - Don't close them. Record `boards.shrink_seen_at`.
   - Close them only if the next run confirms the drop.
   - Show "held: feed shrank" in the manifest.
   - Done: a test shows one bad fetch produces no closed or reopened events, and a confirmed drop
     closes on the second run.
2. **Expiry from the posting itself.**
   - Honour `validThrough` (JSON-LD, `sources/jsonld.py`) and end/expiry dates other readers
     return, where present.
   - Past dates are closed at build time.
   - Done: tests per reader.
3. **Eval set and accuracy tests.**
   - Hand-label ~300 postings sampled across systems: YOE required, visa sponsorship,
     citizenship, clearance, degree, workplace, pay, function, seniority. Store them as fixtures
     in `tests/data/eval/`.
   - Add `python -m lighthouse eval` to report precision and recall per field.
   - Make the tests fail when "not eligible" precision is < 97%; hiding a real fit is the worst
     error.
4. **Run the tests on every push and PR.** Add `.github/workflows/tests.yml` running
   `python -m pytest -q` on Python 3.11 and 3.13, plus the website's JS tests once they exist
   (`node --test`).
5. **Merge v0.6 (PR #1)** and watch the first production build: new base, timing, sizes.
   - Then measure a week of the daily change rate, and fill in §1 and §3.

### P1: the website (GitHub Pages; nothing to install)

1. **✓ Built (branch `website`): static site built by the Action.** The design below is what was
   built. Measurements are in §2.
   - **Generator:** new `lighthouse/site.py`, run after each build: `python -m lighthouse site
     --out site`. It reads `data/builder.db` and writes plain static files, so there is no server
     and no build tool. Start from `scripts/site_prototype.py`, which measured §2.
     - `latest.json`: the current build's file names, build time and counts. Fetched with a
       cache-busting query, ~1 KB.
     - `jobs-base-<id>.json`: one entry per role (de-duplicated, in scope), as columns for
       compression. Fields: title, company (index into a company list), short location,
       countries, first seen, function, seniority, type, workplace, pay range and interval,
       years asked, visa/clearance/citizenship/degree, ghost score, number of copies, skill ids,
       detail file number. Rebuilt with each new base.
     - `jobs-delta-<id>.json`: entries added or changed since the base, and ids removed. Every
       build.
     - `words/<xx>.bin`: keyword index over title, company, skills, location and summary/excerpt,
       sharded by the first two letters, with delta-encoded ids. The page fetches only the 1–3
       shards a query needs.
     - `detail/<n>.json`: 128 jobs per file, holding the excerpt or summary, every open apply link
       (tracking parameters removed), skills, locations, ghost reasons and departments.
     - `index.html`, `app.js`, `app.css`: hand-written, no framework, no external requests.
   - **Page:**
     - The same filters as the local app: keyword, company, function, seniority, type, workplace,
       country/state, pay floor, years asked, visa/citizenship/clearance/degree, posted within N
       days, hide likely ghosts.
     - Sorts: newest, pay, fewest years, keyword relevance.
     - Infinite list; a job panel with the excerpt, all "Apply on …" links and ghost reasons; a
       "new since your last visit" count; and links to the Sources view (coverage per company).
   - **Your data stays in your browser:**
     - The profile, eligibility and saved/applied/hidden marks live in `localStorage`, with
       export/import to a JSON file. Wrap every read and write in try/catch.
     - "Eligible only" uses a JS port of `profile.check`. A test runs the Python and JS versions
       on the same 400 rows × 60 profiles and requires identical results.
   - **Safety:**
     - All text is inserted as text, never as HTML.
     - Links pass the same `safe_url` rules (http/https only).
     - Content-Security-Policy meta: `default-src 'self'`.
     - `noindex`, so search engines send people to employers' own pages.
   - **Workflow:**
     - `job-index.yml` gets a deploy job: `actions/upload-pages-artifact` +
       `actions/deploy-pages`, with `pages: write` and `id-token: write`.
     - The owner sets **Settings → Pages → Source: GitHub Actions** once.
     - Limits to stay inside: site ≤ 1 GB, deploy ≤ 10 min, ~100 GB/month soft bandwidth.
   - Done:
     - first visit ≤ 6 MB and ≤ 3 s on a typical connection
     - filter/sort < 50 ms and opening a job < 300 ms on ~255k jobs
     - works on a phone (no horizontal scroll)
     - a Playwright test drives the page against a generated test site
2. **Excerpts instead of full descriptions** (decided) in the website and the published index.
   - Use the LLM summary when there is one (P3-1). Otherwise a ~600-character excerpt that skips
     boilerplate ("About us", benefits, EEO), preferring the responsibilities/requirements part.
   - The full text stays in the builder for analysis and search.
   - The job panel says "Read the full posting on <employer>".
3. **The local app is frozen:** bug fixes only. Its README section points to the website.
   - Decide at the first major change whether to retire it.
   - The `job-index` release keeps being published while it exists.

### P2: freshness, coverage and health

1. **Near-constant updates.**
   - **Builder:** run hourly (`cron: "17 * * * *"`); `concurrency` already keeps runs from
     overlapping. Each run redeploys the site.
   - **Cheap check first, every run:**
     - Single-request boards (Greenhouse, Lever, Ashby...) already skip unchanged responses by
       hash.
     - For Workday/Oracle, read page 1 sorted newest-first plus the total count. Verify the sort
       options with the network probe first.
   - **Full listing** (needed to catch closures) only when the cheap check changed, or every 6 h.
   - **Workday:** ask only for US/CA/MX via its country facet (`appliedFacets`; take the facet ids
     from the first response). Probe first.
   - Done:
     - median time from a posting going live to it being on the site ≤ 2 h, on a probe sample
     - requests per day to any one careers system no higher than today's 6-hourly full listings
2. **Description fill rate** (the builder needs full text to analyse and index).
   - Spend the 120-minute description budget (`FETCH_DETAIL_MINUTES`) on new postings first, then
     professional functions.
   - Report fill rate per system in the manifest.
   - Done: ≥ 95% within a week of a new base.
3. **Build health.** Add to `latest.json` / `manifest.json` and the Actions job summary:
   - coverage %, fill rate, postings new, closed and held, ghost rate
   - boards failing or switched off, build minutes, file sizes

   Show it on the site's Sources view, and open (or update) one GitHub issue listing boards that
   broke.
4. **More careers systems.** These are the unsupported counts from the test build. For each one,
   probe with `network-probe.yml` first, and add a reader only if a public JSON/XML/JSON-LD feed
   exists.

   | System | Companies |
   |---|---|
   | SuccessFactors without the RSS sitemap (`career?company=` sites) | 26 |
   | Paradox | 12 |
   | Eightfold PCSX tenants (403 today: Microsoft, Starbucks, HP...) | 12 |
   | Classic iCIMS portals (try their JSON-LD job pages + sitemap) | 11 |
   | Avature | 10 |
   | Taleo | 9 |
   | ADP | 5 |
   | BrassRing | 5 |

   - Also: 147 companies show "no careers system recognised", 42 are blocked by robots.txt, and
     26 errored. Spot-check 20 of each and pin boards by hand where discovery missed them.
   - Notable uncovered: Apple, Microsoft, Walmart, UnitedHealth Group, Goldman Sachs, Tesla,
     Netflix, Deloitte, McKinsey, Bloomberg.
5. **Discovery sanity check.** Add `python -m lighthouse discover --report` to list companies whose
   board name doesn't match the company, for a person to pin or reject.

### P3: richer filters

1. **Filter fields from an LLM in the builder** (maintainer's key only).
   - **Partly done on `main`:** the owner chose Claude Haiku via the Batch API, capped monthly
     (`llm_fill.py`). It fills only the fields the rules couldn't settle.
   - What's left: summaries and the extra fields below. Gemini's free tier remains an alternative.
   - New builder step `lighthouse/enrich.py`. It's skipped when the `LLM_API_KEY` secret is unset,
     and stops when the day's quota or a 60-minute cap is used.
   - Provider settings: `LLM_BASE_URL`, `LLM_MODEL`; Gemini's OpenAI-compatible endpoint by
     default.
   - **Input:** batches of 20–25 postings, each as title + the requirements part of the text
     (≤ 1,500 characters). New postings first.
   - **Output:** strict JSON schema per posting:
     - summary (≤ 2 sentences)
     - must-have and nice-to-have requirements
     - years required vs preferred; degree level and field; certifications/licenses
     - clearance; citizenship / sponsorship stance
     - work arrangement and days in office; travel %; schedule/shift
     - seniority; function; languages
   - **Storage:** validate enums, and store in `extractions` as `llm:<model>` keyed by description
     hash and schema version. Rules fill whatever the LLM leaves empty.
   - The new fields become website filters.
   - Done: eval-set precision ≥ rules on every field it replaces; the build never fails because of
     the LLM step.
2. **Standard occupation.** Map titles to O*NET-SOC occupations (free title lists; check the license
   and attribution) for an "Occupation" filter and "similar titles".
3. **"More like this"** with model2vec embeddings of title + skills + summary. Ship 1-bit vectors in
   the detail files, and compare in JS.
4. **Radius filter.** Add coordinates from the GeoNames city list at build time, and a "within N
   miles of …" filter.
5. **Industry.** Add an `industry` column to `companies.csv` and use it as a filter.
6. **Smaller items:**
   - MXN/CAD pay with a USD equivalent
   - Spanish/French titles and descriptions in the rules (Mexico, Quebec)
   - saved searches in the browser

### Later (deferred by the owner)

- **AI fit:** Claude verdicts, a local AI app over loopback, Chrome's built-in model, and a rules
  + embeddings fit score (§5). The website would run them in the browser, with nothing sent
  anywhere but the user's own local AI.
- Community ghost/closed reports.

## 7. Non-goals

- Paid backfill feeds.
- LinkedIn, Indeed or other aggregator scraping.
- SEO landing pages, employer postings, accounts, payments.
- Auto-apply or resume rewriting.
- Any feature a user needs a key or a paid plan for.
- Countries outside the US, Canada and Mexico (for now).
- A server or database service: the website is static files on GitHub Pages.
- Community ghost/closed reports (not for now).

## 8. Owner decisions (2026-11-07)

| # | Question | Decision |
|---|---|---|
| D1 | Republish full descriptions, or ship excerpts? | **Excerpts** (LLM summary when available); "Read the full posting" links to the employer (P1-2). (Fetching the full text on open isn't possible from a website: employers' APIs don't allow other sites' pages to call them.) |
| D2 | Countries in the public index? | **US, Canada, Mexico** (done in PR #1) |
| D3 | Community ghost/closed reports? | **Not for now** |
| D4 | AI on the user's computer? | **Yes, linked locally** (loopback, no sign-in or key), **but deferred** with AI fit |
| D5 | Paid/keyed APIs? | **Users: none. Builder: free-tier LLM API with the maintainer's key** for filter fields (P3-1) |
| D6 | Hosting? | **A website on GitHub Pages; users download nothing** (P1, built). The local app is frozen. |
| D8 | LLM in the builder? | **Claude Haiku with the maintainer's key, capped at $10/month** (owner, on `main`), for fields the rules couldn't settle |
| D7 | Priority? | **A functional, effective aggregator first; AI fit later** |
| — | Update frequency | **As close to constant as polite crawling allows** (P2-1) |

## Sources

- [Job Boardly: Job Board Aggregator Complete Guide](https://www.jobboardly.com/blog/job-board-aggregator-complete-guide)
- [Chrome for Developers: The Prompt API](https://developer.chrome.com/docs/ai/prompt-api)
- [GitHub Changelog: GitHub Models retirement (July 2026)](https://github.blog/changelog/month/07-2026/)
  and [Developers Digest: GitHub Models is retired](https://www.developersdigest.tech/blog/github-models-retired-2026)
- [Free LLM API tiers, verified 2026-11-07 (gravity.fast)](https://gravity.fast/data/free-llm-api-tiers/)
- [Google AI Developers Forum: Flash-Lite free-tier limit of 500 requests/day (2026-11-03)](https://discuss.ai.google.dev/t/free-tier-daily-quota-not-reset-at-midnight-pacific-on-2-nov-2026-429-limit-500-after-one-request-gemini-3-5-flash-lite/197849)
- [model2vec on PyPI](https://pypi.org/project/model2vec)
- [Popular AI: Best CPU-only local LLMs in 2026](https://www.popularai.org/p/best-cpu-only-local-llm-2026)
- [It's FOSS: CPU-only LLM speeds measured with Ollama](https://itsfoss.com/testing-local-llms-without-gpu)
- [GeoNames](https://www.geonames.org/) (city names, CC BY 4.0)
- [GitHub Docs: GitHub Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)
- [Querying SQLite on GitHub Pages with sql.js-httpvfs (2026)](https://recca0120.github.io/en/2026/03/07/sql-js-httpvfs-static-hosting/):
  considered and not chosen, because multi-filter searches make it read large parts of the file
- Lighthouse test builds: Actions runs 42108386998 and 42119014542 (2026-11-05/06)
