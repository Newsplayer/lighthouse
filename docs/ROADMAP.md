# Lighthouse roadmap: from v0.6 to the most effective free job board

This was written 2026-11-07 for Claude Code, and updated the same day with the owner's
decisions (§8). It compares Lighthouse v0.6 (PR #1, branch `big-companies`, verified by a full
test build on 2026-11-06) with what the goal requires. The goal is:
- **The postings:** every real, open, de-duplicated role at the listed employers in the US,
  Canada and Mexico, updated as close to continuously as polite crawling allows.
- **The user:** sees which roles they're eligible for and fit, and applies on the employer's site.
- **Cost:** nothing for users, and no user ever needs a key. The maintainer may use a free-tier
  LLM API key in the builder (§5).

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
| Weekly full download / on-disk size | ≤ 150 MB / ≤ 500 MB | worldwide with full text: 267 MB at 30% fill. US/CA/MX with excerpts (projected): ~130 MB / ~700 MB |
| Each update while the app is open | ≤ 1 MB | 42 KB fixed + ~1.3 KB per changed posting with full text (measured) |
| Page loads (list, search, job) | < 200 ms | 2–50 ms; first load ~180 ms ✓ (real 124k index) |
| Money / keys users need | none | none ✓ (Claude optional) |

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
- **Reader:**
  - Eligibility is computed at query time (`profile.eligibility_sql`).
  - Fit comes from Claude (API key, or the user's Claude app via MCP).
  - The job page links every open copy of the role, with tracking parameters removed.

## 3. Freshness: how updates reach users

Job boards change all day, so freshness is a core feature. Each step below says how it works
today and what changes.

1. **The builder re-reads every board.**
   - Today: every 6 h. Unchanged boards cost one request (response hash). Unchanged postings
     aren't re-analysed.
   - Plan (P1-2): back-to-back hourly runs with a cheap check first, so new postings appear within
     ~1–2 h. A full listing runs only when a board changed, or every 6 h to catch closures.
2. **It publishes only what changed.**
   - Each build writes one delta: rows whose published fields changed since the previous build.
     The manifest lists the chain since the weekly base.
   - Today a delta is 42 KB fixed plus ~1.3 KB per changed posting. With excerpts (P1-1) it gets
     smaller, because changed descriptions no longer ship.
3. **The app checks for new files.**
   - Today: hourly while open, and at start.
   - Plan: every 15 min. Each check is one ~1 KB manifest request, done in the background; pages
     never wait.
4. **It downloads only the deltas it's missing,** applies them in order with SQL, and shows them
   as "new since your last visit".
   - After days away, if the missing deltas outweigh the base, or the chain has a gap, it
     downloads the base instead.
5. **A new base arrives weekly.**
   - It is ready to use, swapped in within seconds, and leaves the user's data untouched.
   - Plan: raise `MAX_DELTAS` from 40 to ~200, so hourly builds don't force a new base every
     40 hours.
6. **Opening a job is a live check.**
   - Plan (P1-1): the app fetches that one posting's full description from the employer's public
     endpoint.
   - If the employer no longer lists it, the job is shown as closed right away, ahead of the next
     build.

**Rough budget** for US/CA/MX at ~310k postings, assuming ~3% change per day (measure it, P0-5):
- An hourly update of ~400 changed postings is ~0.3 MB.
- An app open all day downloads ~5–8 MB a day, plus a ~130 MB base once a week.
- An app opened weekly downloads only what it missed, or the base if that's cheaper.

## 4. Lighthouse vs the industry guide

| Guide practice | Lighthouse now | Gap / action |
|---|---|---|
| Collect via APIs / "job wrapping" (an employer's own ATS feed), not scraping | ✓ Only the public feeds careers sites load, and robots.txt respected | Coverage: see P3-1 |
| Backfill from bought feeds | Not used (paid) | Non-goal |
| Normalize titles, locations, salary, job type | Pay per hour/year, function, seniority, employment type, country (GeoNames cities) | No standard occupation (P2-3), no coordinates or radius (P3-3), no currency conversion for MXN/CAD (P3-5) |
| De-duplicate across postings | ✓ Fingerprint + MinHash per company; all apply links kept | None |
| Re-crawl; expire filled or removed jobs | ✓ Every 6 h; a posting missing from its feed closes | Faster cycle (P1-2); `validThrough` / end dates ignored (P0-2) |
| **"Kill and fill": keep the last-known-good set if a feed comes back empty or partial** | ✗ A successful but shrunken feed closes every missing posting; they "reopen" next run, which pollutes the repost and ghost signals | **P0-1** |
| Ghost / spam controls, audits | ✓ Multi-signal ghost score with reasons | No measurement (P0-3). User reports: not for now (§8) |
| Keyword search + filters (date, type, industry, location, salary, experience) | ✓ FTS5 + all of those except industry and radius | Richer filters (P2-0), industry (P3-4), radius (P3-3) |
| Ranking by relevance, recency, preferences | Sort by date / fit / YOE | Blended ranking (P2-2) |
| AI matching | Claude only (optional) | Free matching (P2-1, P2-2, P2-4) |
| Clean cards that link to the original | ✓ | None |
| Check application links | Postings come from live feeds | Live check when a job is opened (P1-1); sitemap pages (P0-2) |
| Copyright: avoid reproducing descriptions verbatim | ✗ Full descriptions are republished in a public release | **P1-1** (decided: excerpts) |
| Privacy (GDPR/CCPA) | ✓ No server, no accounts, personal data local | Keep it that way |
| Source changes break crawlers | Per-board failure counts, auto switch-off, 14-day re-discovery | Alerts and health metrics (P1-4) |
| Metrics / KPIs | Coverage table on the Sources page | Build health in the manifest, Actions summary and Sources page (P1-4) |
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
| **Gemini API free tier, with the maintainer's key** (repo secret) | Filter fields and a short summary per posting (P2-0) | **Adopt.** About 500 requests a day per model for Flash-Lite as of Nov 2026, with separate quotas per model. At 20–25 postings per request that's ~10–12k postings a day per model. Free-tier content may be used by Google to improve its products; send only public posting text. Use the OpenAI-compatible endpoint, so the provider can be swapped. |
| Groq free tier (maintainer's key) | Small second provider | 1,000 requests/day but 200k tokens/day per model, so only ~250 postings a day. Fallback only. |
| **model2vec static embeddings** (`model2vec` 0.10, numpy only, MIT; e.g. `potion-base-8M`, `potion-retrieval-32M`) | Semantic search, "more like this", resume similarity, title → occupation | **Adopt (P2-1).** Up to ~500× faster than the original transformer on CPU: minutes for every posting, incremental after that. Ship 256-bit binary vectors (~32 B per posting). |
| ONNX sentence-transformers (bge-small / e5-small via `fastembed`) | Higher-quality embeddings | Only if the eval set shows model2vec isn't enough; ~100× slower |
| Generative LLM on the runner's CPU (llama.cpp / Ollama) | Field extraction | **Reject at scale:** ~7–10 tokens/s, so it can't keep up with thousands of new postings a day |
| GitHub Models via `GITHUB_TOKEN` | — | **Gone:** retired 2026-07-30 |
| Cerebras, OpenRouter free, Cloudflare Workers AI, SambaNova | — | Trials or tiny daily limits (50 requests, 10k "neurons", 20 requests); not worth the code |

### On the user's machine (optional, found automatically, never required, no sign-in)

| Option | Use | Notes |
|---|---|---|
| **Rules + embeddings fit score** (P2-2) | Default "Strong / Possible / Weak" for every eligible job, with reasons | No model download. Probably most of the value. |
| **Local AI app on this computer:** Ollama (`127.0.0.1:11434`), LM Studio (`:1234`), llama.cpp server (`:8080`) | Fit verdicts for a shortlist (≤ 50), in the background | Lighthouse looks for these on localhost (OpenAI-compatible API), so there's nothing to sign in to and no key. Loopback only. Suggested models: Qwen3.5 4B (JSON extraction), Phi-4-mini (structured output), SmolLM3 3B. CPU-only laptops take ~30–90 s per job. |
| **Chrome's built-in model** (Prompt API, Chrome 148+) | Fit check of the job being viewed; plain words → filters | Runs in the page, nothing to install or sign in to. Needs ≥ 22 GB free disk and > 4 GB VRAM, or 16 GB RAM + 4 cores. JSON schema via `responseConstraint`. Handle `downloading`/`unavailable`. |
| In-browser WebGPU (WebLLM, Transformers.js) | Same, other browsers | Big one-time download; needs a WebGPU GPU. Low priority. |
| **User's Claude app via MCP** (exists) | Best-quality fit, on the user's own Claude plan, no key | Keep |
| Claude API key (exists) | Same, batch | Optional, paid by the user. Never required. |

**Rejected:**
- For users: any service that needs the user to make a key or an account.
- Anywhere: anonymous "keyless" hosted endpoints (no uptime or privacy guarantees).

## 6. Work, in order

Each item lists where the work is and what "done" means. Keep every change incremental (it only
touches new or changed postings) and measured (§2 baselines).

### P0: trust and correctness

1. **Shrink guard ("kill and fill").** In `db.upsert_board` (the closing logic near its end), when a
   board that answered successfully lists < 50% of its open postings (and ≥ 20 are missing), or
   zero:
   - Don't close them. Record `boards.shrink_seen_at`.
   - Close them only if the next run confirms the drop.
   - Show "held: feed shrank" on the Sources page and in the manifest.
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
     in `tests/data/eval/`, with descriptions trimmed to what's needed.
   - Add `python -m lighthouse eval` to report precision and recall per field, for rules and for
     the builder LLM (P2-0).
   - Make the tests fail when "not eligible" precision is < 97%; hiding a real fit is the worst
     error.
4. **Run the tests on every push and PR.** There is no test workflow yet. Add
   `.github/workflows/tests.yml` running `python -m pytest -q` on Python 3.11 and 3.13.
5. **Merge v0.6 (PR #1)** and watch the first production build: new base, timing, sizes.
   - Then measure a week of real delta sizes and the daily change rate, and fill in §1 and §3.

### P1: freshness, size and health

1. **Excerpts, plus the full description when a job is opened** (decided).
   - **Base:** ship structured fields, rule/LLM extractions, the search index (built from the full
     text, so search still covers it), embeddings, and a short text.
     - The short text is the LLM summary when there is one (P2-0).
     - Otherwise it's a ~600-character excerpt that skips boilerplate ("About us", benefits, EEO),
       preferring the responsibilities/requirements part.
   - **Reader, when a job is opened:**
     - Fetch that posting's full description from the employer's public endpoint, as one request
       through the reader's HTTP client. Each source gets a `detail(slug, external_id)`; the
       allowlist adds that board's host for that request only.
     - Sanitize it like today. Cache it in `user.db`, and keep it for saved and applied jobs.
     - If the employer reports the posting gone, show it as closed at once.
   - **Fallback:** an "Open on employer site" link.
   - Bump `FORMAT`/`INDEX_SCHEMA`.
   - Done:
     - base ≤ 150 MB for US/CA/MX
     - the full text shows in < 1.5 s
     - saved jobs work offline
     - tests for each source's `detail`
2. **Near-constant updates.**
   - **Builder:** run hourly (`cron: "17 * * * *"`); the workflow's `concurrency` keeps runs from
     overlapping.
   - **Cheap check first, every run:**
     - Single-request boards (Greenhouse, Lever, Ashby...) already skip unchanged responses by
       hash.
     - For Workday/Oracle, read page 1 sorted newest-first plus the total count. Verify the sort
       options with the network probe first.
   - **Full listing** (needed to catch closures) only when the cheap check changed, or every 6 h.
   - **Workday:** ask only for US/CA/MX via its country facet (`appliedFacets`; take the facet ids
     from the first response). Probe first.
   - **Reader:** `check_every_hours = 0.25`.
   - **Chain length:** `MAX_DELTAS` ≈ 200; the weekly base stays.
   - Done:
     - median time from a posting going live to it being in the index ≤ 2 h, on a probe sample
     - total requests per day to any one careers system no higher than today's 6-hourly full
       listings
3. **Description fill rate** (the builder needs full text to analyse, even though it won't ship
   it).
   - Spend the 120-minute description budget (`FETCH_DETAIL_MINUTES`) on new postings first, then
     professional functions (by title rules).
   - Report fill rate per system in the manifest.
   - Done: ≥ 95% within a week of a new base; Workday stays within the shared 8/s budget.
4. **Build health.** Add to `manifest.json` and the Actions job summary:
   - coverage %, fill rate, postings new, closed and held, ghost rate
   - boards failing or switched off, build minutes, base and delta sizes

   Open (or update) one GitHub issue listing boards that broke, and show the same on the Sources
   page.
5. **Fewer bytes per posting** (disk target ≤ 500 MB). On the real index a jobs row is ~950 B.
   - Don't ship `fingerprint` and `detail_marker` (`seed_builder` recomputes them).
   - Store timestamps as integers.
   - Ship only the last few events per posting.
   - Drop extraction evidence snippets the job page doesn't show.
   - Done: measured before/after on the real index.

### P2: filters and matching

0. **Filter fields from a free-tier LLM in the builder** (maintainer's key only).
   - New builder step `lighthouse/enrich.py`. It's skipped when the `LLM_API_KEY` secret is unset,
     and stops when the day's quota or a 60-minute cap is used.
   - Provider settings: `LLM_BASE_URL`, `LLM_MODEL`; Gemini's OpenAI-compatible endpoint by
     default.
   - **Input:** batches of 20–25 postings, each as title + the requirements part of the text
     (≤ 1,500 characters). New postings first, then the backlog.
   - **Output:** strict JSON schema per posting:
     - summary (≤ 2 sentences)
     - must-have and nice-to-have requirements (short phrases)
     - years required vs preferred; degree level and field; certifications/licenses
     - clearance; citizenship / sponsorship stance
     - work arrangement and days in office; travel %; schedule/shift
     - seniority; function; languages
   - **Storage:** validate enums, and store in `extractions` as `llm:<model>` keyed by description
     hash and schema version. Rules fill whatever the LLM leaves empty.
   - Add the new fields as filters in `db.Filters`, the UI and MCP.
   - Log rule/LLM disagreements for the eval set.
   - Done: eval-set precision ≥ rules on every field it replaces; the build never fails because of
     the LLM step.
1. **Embeddings in the builder.**
   - Use model2vec (pin the model in `requirements.txt`; cache it in Actions).
   - Embed title + function + skills + summary for each new or changed posting, and ship binary
     256-bit vectors.
   - **Reader:** embeds the profile/resume once (numpy), and ranks with Hamming distance.
   - Done: "More like this" and "Closest to my profile", both < 200 ms on 310k postings.
2. **Free fit score** (new `fit_rules.py`).
   - Only eligible jobs get a score, made of: skills overlap, embedding similarity, seniority and
     YOE gap, preferred functions, and pay vs floor.
   - Turn the score into Strong / Possible / Weak, with 2–4 plain reasons. Claude's or a local
     AI's verdict wins when present.
   - Done: on the eval set, hand-labeled good fits rank in the top 20% for ≥ 80% of cases.
3. **Standard occupation.**
   - Map titles to O*NET-SOC occupations via embeddings against O*NET's free title lists. Check the
     license and attribution first.
   - Use it for an "Occupation" filter and "similar titles".
4. **Local AI verdicts** (decided: link locally, no sign-in).
   - On the AI page, Lighthouse looks for Ollama / LM Studio / llama.cpp on `127.0.0.1` and lists
     the models it finds. The user picks one.
   - Requests go to loopback only, with no key. A local AI never sees anything but the profile
     and the posting.
   - Chrome's built-in model is used in the page when available.
   - Use the same schema and cache as Claude fit, on a shortlist (top N from P2-2), in the
     background.
   - Done: verdicts appear with no key and no account; nothing breaks when no local AI exists;
     injection tests pass for each provider.
5. **Plain words → filters.**
   - First a rule parser (e.g. "remote senior python over 150k no clearance" → filters).
   - Then, optionally, a local AI or Chrome's model with a strict JSON schema of `db.Filters`.
   - Also expose it in MCP `search_jobs`.

### P3: coverage and UX

1. **More careers systems.** These are the unsupported counts from the test build. For each one,
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
2. **Discovery sanity check.** Add `python -m lighthouse discover --report` to list companies whose
   board name doesn't match the company, for a person to pin or reject.
3. **Radius filter.** Add coordinates from the GeoNames city list (already in
   `lighthouse/data/`, extend it with lat/lon) at build time, and a "within N miles of …" filter.
4. **Industry.** Add an `industry` column to `companies.csv` (by hand, or SEC SIC codes for public
   companies) and use it as a filter.
5. **Smaller items:**
   - MXN/CAD pay shown with a USD equivalent, from a static rate table updated with each base
   - Spanish/French titles and descriptions handled in the rules and classifiers (Mexico, Quebec)
   - saved searches with a "new matches since last visit" view

## 7. Non-goals

- Paid backfill feeds.
- LinkedIn, Indeed or other aggregator scraping.
- SEO landing pages, employer postings, accounts, payments.
- Auto-apply or resume rewriting.
- Any feature a user needs a key or a paid plan for.
- Countries outside the US, Canada and Mexico (for now).
- Community ghost/closed reports (not for now).

## 8. Owner decisions (2026-11-07)

| # | Question | Decision |
|---|---|---|
| D1 | Republish full descriptions, or ship excerpts and fetch on open? | **Excerpts** (LLM summary when available) **+ full text fetched when a job is opened** (P1-1) |
| D2 | Countries in the public index? | **US, Canada, Mexico** (done in PR #1) |
| D3 | Community ghost/closed reports? | **Not for now** |
| D4 | AI on the user's computer? | **Yes, linked locally** (loopback, no sign-in or key) (P2-4) |
| D5 | Paid/keyed APIs? | **Users: none. Builder: free-tier LLM API with the maintainer's key** for filter fields (P2-0) |
| — | Update frequency | **As close to constant as polite crawling allows** (P1-2) |

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
- Lighthouse test builds: Actions runs 42108386998 and 42119014542 (2026-11-05/06)
