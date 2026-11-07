# Lighthouse roadmap: from v0.6 to the most effective free job board

This was written 2026-11-07 for Claude Code. It compares Lighthouse v0.6 (branch `big-companies`,
verified by a full test build on 2026-11-06) with what the goal requires. The goal is:
- **The postings:** every real, open, de-duplicated role at the listed employers.
- **The user:** sees which roles they're eligible for and fit, and applies on the employer's site.
- **Cost:** nothing, and no API keys.

The industry practices it is checked against come from Job Boardly's *Job Board Aggregator Guide*.
Work top to bottom. Items marked **Owner decision** need the owner's answer before building them.

---

## 1. What "optimal" means: targets

| Measure | Target | Now (v0.6 test build) |
|---|---|---|
| Listed companies covered (`config/companies.csv`) | ≥ 90% | 376 / 700 (54%) |
| Fortune 500 group covered | ≥ 90% | 263 / 466 (56%) |
| Open postings with an analysed description | ≥ 95% | 30% overall: Workday 11%, Oracle 4%, SmartRecruiters 16% |
| Enabled boards checked | every 6 h | every 6 h ✓ |
| A posting that closes leaves the default list | ≤ 6 h | ≤ 6 h ✓ (but see P0-1) |
| Wrongly hidden as "not eligible" (the worst error) | ≤ 3% on the eval set | not measured |
| Weekly full download / on-disk size for readers | ≤ 150 MB / ≤ 500 MB | 267 MB at 30% fill; would grow to roughly 700 MB at full fill |
| Typical update download | ≤ 5 MB | chain of per-build deltas ✓ (size not yet measured in production) |
| Page loads (list, search, job) | < 200 ms | 2–50 ms; first load ~180 ms ✓ (real 124k index) |
| Money / keys needed | none | none ✓ (Claude optional) |

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
  - It's then published as a ready-to-use SQLite index: a weekly base plus a chain of deltas.
- **Test build, 2026-11-06:**
  - 461,682 open postings from 850 boards, in 2 h 06 min.
  - Base 267 MB (xz); builder DB 2.6 GB.
  - Workday errors fell from 117 to 5 after the shared rate budget.
- **Real 124k-posting index:**
  - Download 149 MB, 421 MB on disk.
  - Installs in 6 s.
  - Pages load in 2–50 ms.
- **Reader:**
  - Eligibility is computed at query time (`profile.eligibility_sql`).
  - Fit comes from Claude (API key, or the user's Claude app via MCP).
  - The job page links every open copy of the role, with tracking parameters removed.
- **Not yet merged:** v0.6 is on `big-companies`; `main` is still v0.5. The first production build
  after merge writes a new format-3 base.

## 3. Lighthouse vs the industry guide

| Guide practice | Lighthouse now | Gap / action |
|---|---|---|
| Collect via APIs / "job wrapping" (an employer's own ATS feed), not scraping | ✓ Only the public feeds careers sites load, and robots.txt respected | Coverage: see P3-1 |
| Backfill from bought feeds | Not used (paid) | Non-goal |
| Normalize titles, locations, salary, job type | Pay per hour/year, function, seniority, employment type, country | No standard occupation (P2-3), no coordinates or radius (P3-3), no currency conversion (P3-6) |
| De-duplicate across postings | ✓ Fingerprint + MinHash per company; all apply links kept | None |
| Re-crawl; expire filled or removed jobs | ✓ Every 6 h; a posting missing from its feed closes | `validThrough` / end dates ignored (P0-2) |
| **"Kill and fill": keep the last-known-good set if a feed comes back empty or partial** | ✗ A successful but shrunken feed closes every missing posting; they "reopen" next run, which pollutes the repost and ghost signals | **P0-1** |
| Ghost / spam controls, audits | ✓ Multi-signal ghost score with reasons | No measurement (P0-3), no user reports (P3-4) |
| Keyword search + filters (date, type, industry, location, salary, experience) | ✓ FTS5 + all of those except industry and radius | Industry (P3-5), radius (P3-3) |
| Ranking by relevance, recency, preferences | Sort by date / fit / YOE | Blended ranking (P2-2) |
| AI matching | Claude only (optional) | Free matching (P2-1, P2-2, P2-4) |
| Clean cards that link to the original | ✓ | None |
| Check application links | Postings come from live feeds | Sitemap/JSON-LD pages can outlive the job (P0-2) |
| Copyright: avoid reproducing descriptions verbatim | ✗ Full descriptions are republished in a public release | **Owner decision D1** |
| Privacy (GDPR/CCPA) | ✓ No server, no accounts, personal data local | Keep it that way |
| Source changes break crawlers | Per-board failure counts, auto switch-off, 14-day re-discovery | Alerts and health metrics (P1-3) |
| Metrics / KPIs | Coverage table on the Sources page | Build health in the manifest, Actions summary and Sources page (P1-3) |
| SEO pages, accounts, monetization, Stripe | n/a (private local app) | Non-goal |

## 4. Free LLM and ML options (no key, no payment; verified 2026-11-07)

**Rule:** anything an LLM does must also work without it. LLMs refine a ranking or explain a
verdict; they never gate what the user can see. One verdict schema serves every model: tier plus
per-requirement met / missing / unclear, the same as `claude_api.py`. Verdicts are cached by
(cluster, description hash, profile hash, model, prompt version). The injection rules apply to
every model.

### In the builder (GitHub Action, CPU only, runs once for everyone)

| Option | Use | Verdict |
|---|---|---|
| **model2vec static embeddings** (`model2vec` 0.10, numpy only, MIT; e.g. `potion-base-8M`, `potion-retrieval-32M`, distilled from bge-base) | Embeddings for title + summary of every posting: semantic search, "more like this", resume similarity, weakly supervised classifiers, title → occupation | **Adopt (P2-1).** Up to ~500× faster than the original transformer on CPU, so 460k postings is minutes, and incremental after that. Ship 256-bit binary vectors (~32 B per posting, ~15 MB). |
| ONNX sentence-transformers (bge-small / e5-small via `fastembed`) | Higher-quality embeddings if model2vec isn't good enough | Measure with the eval set first; ~100× slower |
| Generative LLM on the runner (llama.cpp / Ollama, 1–4B models) | Field extraction | **Reject at scale.** CPU speed is ~7–10 tokens/s generation, tens to hundreds for prompts. 15k new postings a day would need tens of hours a day. At most an experiment on ≤ 200 postings per run where the rules found nothing and the field matters. |
| GitHub Models via `GITHUB_TOKEN` | — | **Gone:** retired 2026-07-30 (inference API, playground, BYOK) |

### On the user's machine (optional, detected at runtime, never required)

| Option | Use | Notes |
|---|---|---|
| **Rules + embeddings fit score** (P2-2) | Default "Strong / Possible / Weak" for every eligible job, with reasons | No model download. Probably most of the value. |
| **Chrome built-in Prompt API** (Gemini Nano; web pages from Chrome 148) | On-demand fit check of the job being viewed; plain words → filters | Desktop Win/macOS/Linux/ChromeOS Plus. Needs ≥ 22 GB free disk and > 4 GB VRAM, or 16 GB RAM + 4 cores. JSON schema via `responseConstraint`. Stays on device. Check `LanguageModel.availability()` and handle `downloading`/`unavailable`. |
| **Ollama / llama.cpp / LM Studio on localhost** | Background fit verdicts for a shortlist (≤ 50) | User installs it. Suggested models: Qwen3.5 4B (JSON extraction), Phi-4-mini (structured output), SmolLM3 3B (classification). Use JSON-schema structured output. Older CPU-only laptops take ~30–90 s per job, so shortlist only. Allowlist `127.0.0.1` only. |
| In-browser WebGPU (WebLLM, Transformers.js) | Same as Prompt API, other browsers | One-time model download (hundreds of MB to GBs); needs a WebGPU GPU. Lower priority. |
| **User's Claude app via MCP** (exists) | Best-quality fit / extraction on the user's own Claude plan, no key | Keep; document as the top-quality free path |
| Claude API key (exists) | Same, batch | Optional, paid by the user. Keep, never required. |

**Rejected:**
- Free tiers that need a key: Gemini, Groq, OpenRouter, Cloudflare Workers AI, Mistral.
- Anonymous "keyless" hosted endpoints: no uptime or privacy guarantees, and the profile and resume
  would leave the machine.

## 5. Work, in order

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
   - For sitemap sites, a page that drops out of the sitemap closes, as it does today.
   - Done: tests per reader.
3. **Eval set and accuracy in CI.**
   - Hand-label ~300 postings sampled across systems: YOE required, visa sponsorship,
     citizenship, clearance, degree, workplace, pay, function, seniority. Store them as fixtures
     in `tests/data/eval/`, with descriptions trimmed to what's needed.
   - Add `python -m lighthouse eval` to report precision and recall per field.
   - Make the tests fail when "not eligible" precision is < 97%; hiding a real fit is the worst
     error.
   - Use the set to judge every later rules or ML change.
4. **Run the tests on every push and PR.** There is no test workflow yet; only the builder and the
   network probe run in Actions. Add `.github/workflows/tests.yml` running `python -m pytest -q` on
   Python 3.11 and 3.13.
5. **Merge v0.6.**
   - Open the PR from `big-companies`, merge it, and watch the first production build: new base,
     timing, sizes.
   - Then measure real delta sizes across a week and fill in §1.

### P1: size, freshness and health

1. **Owner decision D1 (descriptions).** If it's "excerpt + on demand", build it:
   - **Base:** ship structured fields, rule extractions, the search index (built from full text, so
     search still covers it), embeddings, and an excerpt of about the first 600 characters.
   - **Reader:** fetches the full text from the employer's public endpoint when the user opens a
     job, as one user-initiated request. Each source gets a `detail(slug, external_id)`. The text
     is cached in `user.db`, and stored when a job is saved or applied to.
   - **Fallback:** an "Open on employer site" link.
   - Done: base ≤ 150 MB for ~460k postings, job page shows the full text in < 1.5 s, works offline
     for saved jobs.
2. **Description fill rate (all decisions need it, since analysis happens in the builder).**
   - Spend the 120-minute description budget (`FETCH_DETAIL_MINUTES`) where users will look: new
     postings first, then the user's countries scope, then professional functions (by title rules).
   - Report fill rate per system in the manifest.
   - Done: ≥ 95% within a week of a new base; Workday stays within the shared 8/s budget.
3. **Build health.** Add to `manifest.json` and the Actions job summary:
   - coverage %, fill rate, postings new, closed and held, ghost rate
   - boards failing or switched off, build minutes, base and delta sizes

   Also open (or update) one GitHub issue listing boards that broke this run. Show the same on the
   Sources page.
4. **Owner decision D2 (country scope).** Set the `LIGHTHOUSE_COUNTRIES` repo variable if the
   public index should be US-only. On real data 52% of postings are US and 17% unknown (kept),
   so the size drops by about 30%.

### P2: matching without paid LLMs

1. **Embeddings in the builder.**
   - Use model2vec (pin the model in `requirements.txt`; cache it in Actions).
   - Embed title + function + skills + excerpt for each new or changed posting.
   - Ship binary 256-bit vectors in the index.
   - **Reader:** embeds the profile/resume once (model2vec runs on numpy), and ranks with Hamming
     distance in SQL or numpy.
   - Done: "More like this" on job pages, and a "Closest to my profile" sort, both < 200 ms on
     460k.
2. **Free fit score** (`profile.py` / new `fit_rules.py`).
   - Only eligible jobs get a score, made of: skills overlap (extracted skills ∩ profile skills),
     embedding similarity, seniority and YOE gap, preferred functions, and pay vs floor.
   - Turn the score into Strong / Possible / Weak, with 2–4 plain reasons. Show it where the
     Claude verdict shows; Claude's verdict wins when present.
   - Done: on the eval set, a hand-labeled "good fit" sample ranks in the top 20% for ≥ 80% of
     cases.
3. **Standard occupation.**
   - Map each title to an O*NET-SOC occupation via embeddings against O*NET's free
     title/alternate-title lists. Check the license and attribution terms before shipping data.
   - Use it for an "Occupation" filter, "similar titles", and tighter duplicate checks.
4. **Optional local LLM verdicts.**
   - One module: provider detection (Chrome Prompt API in the page; Ollama/llama.cpp on
     `127.0.0.1`; existing MCP / API key), the same schema and cache as Claude fit, and a
     shortlist of the top N from P2-2.
   - Done: verdicts appear with no key; nothing breaks when no provider exists; injection tests
     pass for every provider.
5. **Plain words → filters.**
   - First a rule parser (e.g. "remote senior python over 150k no clearance" → filters).
   - Then, optionally, the Prompt API / local LLM with a strict JSON schema of `db.Filters`.
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
2. **Discovery sanity check.**
   - A lone board under another name is accepted only on enterprise systems; review the notes
     (`detail`) weekly.
   - Add `python -m lighthouse discover --report` to list companies whose board name doesn't
     match the company, for a person to pin or reject.
3. **Radius filter.**
   - Geocode location strings offline with GeoNames cities (free; check attribution), at build
     time, into lat/lon columns.
   - Add "within N miles of …" to the filters.
4. **Community reports (Owner decision D3).**
   - A "Report ghost / closed" button opens a pre-filled GitHub issue, like "Suggest a board".
   - The builder reads issues with that label and uses ≥ 2 distinct reporters as a soft ghost
     signal, shown as such. A label alone never hides a job.
5. **Industry.** Add an `industry` column to `companies.csv`, filled by hand or from SEC SIC codes
   for public companies, and use it as a filter.
6. **Smaller items:**
   - currency conversion for non-USD pay, from a static table updated with each base
   - saved searches with a "new matches since last visit" view
   - a weekly "still open?" re-check for saved and applied jobs

## 6. Owner decisions waiting

| # | Question | Recommendation |
|---|---|---|
| D1 | Keep republishing full descriptions in the public index, or ship excerpts + fetch on open? | **Excerpt + on open.** Smaller download (target ≤ 150 MB), follows the guide's copyright advice, and search still covers the full text. Cost: readers make one request to the employer's site per job opened. |
| D2 | Should the public index be US-only (`LIGHTHOUSE_COUNTRIES=US`)? | Yes, if the users are US-based: about 30% smaller, faster builds |
| D3 | Accept community ghost/closed reports through GitHub issues? | Yes, as a soft, labelled signal (≥ 2 reporters) |
| D4 | Allow readers to call a local LLM (Ollama on 127.0.0.1) and use Chrome's built-in model? | Yes, opt-in, off by default |

## 7. Non-goals

- Paid backfill feeds.
- LinkedIn, Indeed or other aggregator scraping.
- SEO landing pages, employer postings, accounts, payments.
- Auto-apply or resume rewriting.
- Any feature that needs a key or a paid plan to work.

## Sources

- [Job Boardly: Job Board Aggregator Complete Guide](https://www.jobboardly.com/blog/job-board-aggregator-complete-guide)
- [Chrome for Developers: The Prompt API](https://developer.chrome.com/docs/ai/prompt-api)
- [GitHub Changelog: GitHub Models retirement (July 2026)](https://github.blog/changelog/month/07-2026/)
  and [Developers Digest: GitHub Models is retired](https://www.developersdigest.tech/blog/github-models-retired-2026)
- [model2vec on PyPI](https://pypi.org/project/model2vec)
- [Popular AI: Best CPU-only local LLMs in 2026](https://www.popularai.org/p/best-cpu-only-local-llm-2026)
- [It's FOSS: CPU-only LLM speeds measured with Ollama](https://itsfoss.com/testing-local-llms-without-gpu)
- Lighthouse test builds: Actions runs 42108386998 and 42119014542 (2026-11-05/06)
