"""CLI (what the GitHub Actions run; see MAINTAINING.md):
    python -m lighthouse index build --out dist [--countries US,CA,MX] [--no-llm]
                                            # find boards, fetch, analyse, write the public files
    python -m lighthouse index seed jobs.db.xz   # start the builder from the published database (cache lost)
    python -m lighthouse index fill [--minutes N]    # the open-model gap fill alone
    python -m lighthouse index summary [dist/manifest.json] [--boards]  # how the build went (Markdown)
    python -m lighthouse site [--out site_out]       # write the website from the builder database
    python -m lighthouse discover [--only NAME] [--out FILE]
                                            # find job boards from config/companies.csv careers pages
    python -m lighthouse discover --report  # listed boards whose id doesn't look like their company (no network)
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import config, db, snapshot


def _num(v):   # the workflow passes repository variables, which may be empty
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        raise SystemExit(f"not a number: {v!r}")


def _index(args) -> None:
    path = args.db or snapshot.BUILDER_DB
    if args.action == "seed":
        if not args.file:
            raise SystemExit("usage: python -m lighthouse index seed jobs.db.xz [--db data/builder.db]")
        print(f"Seeded {snapshot.seed_builder(args.file, path)} postings.")
    elif args.action == "fill":
        conn = db.connect(path)
        try:
            print(json.dumps(snapshot.fill_gaps(conn, config.load(), _num(args.minutes)), indent=2))
        finally:
            conn.close()
    elif args.action == "summary":
        m = json.loads(Path(args.file or "dist/manifest.json").read_text(encoding="utf-8"))
        print(snapshot.boards_markdown(m) if args.boards else snapshot.summary_markdown(m))
    else:
        m = snapshot.build(args.out, countries=args.countries.split(",") if args.countries else (), db_path=path,
                           detail_calls=args.detail_calls, full=args.full, llm=not args.no_llm,
                           llm_minutes=_num(args.llm_minutes), fetch_minutes=_num(args.fetch_minutes))
        print(json.dumps(m, indent=2))


def _discover(args) -> None:
    from . import discover, net
    companies = discover.load_companies(config.DEFAULT_CONFIG.parent)
    if args.report:
        odd = discover.mismatched(config.load().boards, companies)
        for b in odd:
            print(f"  {b['ats'] + ':' + b['slug']:60} {b['company']}")
        print(f"{len(odd)} listed boards have an id that doesn't look like their company. Check each one: "
              "a board that belongs to someone else lists the wrong jobs under this name.")
        return
    client = discover.discovery_client()
    out = open(args.out, "w", encoding="utf-8") if args.out else None

    def show(r):
        line = discover.to_json(r)
        print("DISCOVER " + line, flush=True)
        if out:
            out.write(line + "\n")
    results = discover.run(companies, client, net.BUILDER_AGENT, workers=args.workers, only=args.only, on_result=show)
    if out:
        out.close()
    print("SUMMARY " + json.dumps(discover.summary(results)))


def main() -> None:
    parser = argparse.ArgumentParser(prog="lighthouse")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="build the public job database")
    p.add_argument("action", choices=["build", "seed", "fill", "summary"])
    p.add_argument("file", nargs="?", help="seed: the published jobs.db.xz; summary: a manifest.json")
    p.add_argument("--out", default="dist", help="build: output folder")
    p.add_argument("--db", default=None, help="builder database (default data/builder.db)")
    p.add_argument("--countries", default="", help="build: comma-separated country codes to keep (default: all)")
    p.add_argument("--detail-calls", type=int, default=None, help="build: description calls per board per run")
    p.add_argument("--full", action="store_true", help="build: look every listed company up again, and retry boards "
                                                       "that were switched off")
    p.add_argument("--no-llm", action="store_true", help="build: rules only (no open-model gap fill, no category guesses)")
    p.add_argument("--llm-minutes", default=None, help="build: minutes the open model reads unsettled postings")
    p.add_argument("--fetch-minutes", default=None,
                   help="build: start no new board or description call after this many minutes (default 40)")
    p.add_argument("--minutes", default=None, help="fill: minutes the open model reads (default from config)")
    p.add_argument("--boards", action="store_true", help="summary: only the boards that need a look")
    p = sub.add_parser("site", help="write the website (static files for GitHub Pages) from the builder database")
    p.add_argument("--db", default=None, help="builder database (default data/builder.db)")
    p.add_argument("--out", default="site_out", help="folder to write (replaced)")
    p.add_argument("--countries", default="US,CA,MX", help="comma-separated country codes to show (empty: all)")
    p.add_argument("--new-base", action="store_true", help="write a new weekly job list instead of a change file")
    p = sub.add_parser("discover", help="find job boards from the careers pages in config/companies.csv")
    p.add_argument("--only", default=None, help="only companies whose name contains this")
    p.add_argument("--out", default=None, help="write one JSON line per company here")
    p.add_argument("--workers", type=int, default=16)
    p.add_argument("--report", action="store_true",
                   help="list boards whose id doesn't look like their company, for a person to check (no network)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "index":
        _index(args)
    elif args.cmd == "discover":
        _discover(args)
    else:
        from . import site
        print(json.dumps(site.generate(args.db or snapshot.BUILDER_DB, args.out,
                                       countries=[c for c in args.countries.split(",") if c.strip()],
                                       new_base=args.new_base)))


if __name__ == "__main__":
    main()
