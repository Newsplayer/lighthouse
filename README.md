# Lighthouse

A local job aggregator. Pulls postings from public ATS job boards, removes duplicates,
flags likely ghost jobs, and (soon) scores how well each role fits you.

Runs entirely on your machine. Depends only on free, keyless public APIs — plus the
Claude API later, for fit scoring.

## Quick start

Needs Python 3.11+ ([python.org](https://www.python.org/downloads/)). Then:

| System | How to start |
|---|---|
| Windows | Double-click `start-windows.bat` |
| macOS | Double-click `start-mac.command` (first time: right-click → Open) |
| Linux | `./start.sh` |

The first run sets everything up and pulls jobs. After that, startup takes a couple
of seconds. Your browser opens to the site automatically; close the terminal window to stop it.

### Manual setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python -m lighthouse fetch       # pull every board in config/sources.toml
python -m lighthouse serve       # open http://127.0.0.1:5000
```

Each job shows a link to the original posting **and** a sanitized local copy of the
description. The **Refresh boards** button re-runs the fetch.

## Choosing boards

Edit `config/sources.toml`. For Greenhouse, the token is the slug in the company's
board URL (`job-boards.greenhouse.io/<token>`).

## How it works

| Piece | File | Notes |
|---|---|---|
| Sources | `lighthouse/sources/` | One class per ATS. Greenhouse done. |
| Storage | `lighthouse/db.py` | SQLite at `data/lighthouse.db` (git-ignored). |
| De-duplication | `lighthouse/models.py` | `uid` = same posting re-fetched; `fingerprint` = company + title + location, collapses the same role across boards. |
| Ghost signals | `lighthouse/ghost.py` | Flags, never hides: posting age, evergreen titles. Postings that disappear are marked closed. |
| Sanitizer | `lighthouse/sanitize.py` | Allow-list HTML cleaning before a description is stored. |
| Website | `lighthouse/web.py`, `templates/` | Flask, bound to localhost only. |

## Roadmap

- [x] Greenhouse source, local site, link + copy of posting
- [ ] Lever, Ashby, Workable, SmartRecruiters sources (all free public JSON)
- [ ] Better ghost detection: repost tracking, time-to-close, Claude review of description
- [ ] Profile (resume, location, work authorization) → eligibility filter
- [ ] Claude API fit score + one-line reason per job
- [ ] Saved / applied / hidden tracking

## Tests

```bash
pip install pytest && pytest
```
