"""CLI:
    python -m lighthouse start              # one step: fetch if empty, pick a free port, open browser, serve
    python -m lighthouse serve [--port N]   # website + scheduled fetches
    python -m lighthouse fetch              # pull all enabled boards
    python -m lighthouse add URL|FILE ...   # add boards (ATS links, careers pages, ats:slug, or a file of them)
    python -m lighthouse boards             # list boards and their status
    python -m lighthouse reprocess          # re-run clustering, extraction, ghost and eligibility on everything
    python -m lighthouse visa import FILE…  # import DOL LCA / USCIS H-1B hub files (or data/visa/ if none given)
    python -m lighthouse claude extract|fit [--limit N]   # optional Claude passes (uses your API key)
    python -m lighthouse claude poll        # collect finished batches
    python -m lighthouse key set|clear      # store / remove your Anthropic API key in the OS keychain
    python -m lighthouse mcp                # MCP server (stdio) for your own Claude app
    python -m lighthouse index status       # shared job index: when it was built, what it covers
    python -m lighthouse index import [FILE] # update now (or merge a local base-*.db.xz to test a build)
    python -m lighthouse index seed base.db.xz  # builder: start from the published index (cache expired)
    python -m lighthouse index build --out dist [--countries US,CA] [--published M] [--full]
    python -m lighthouse index csv [--out jobs.csv.gz] [--db FILE]  # every open role, one column per filter
                                            # what the GitHub Action runs (maintainers only)
    python -m lighthouse discover [--only NAME] [--out FILE]
                                            # find job boards from config/companies.csv careers pages
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import socket
import sys
import threading
import webbrowser
from pathlib import Path

from . import config, db, fetch


def _free_port(host: str, preferred: int, tries: int = 50) -> int:
    """First open port at or after `preferred` (5000 is often taken by macOS AirPlay)."""
    for port in range(preferred, preferred + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"No free port found between {preferred} and {preferred + tries - 1}.")


def _serve(host: str, port: int, open_browser: bool) -> None:
    from . import scheduler
    from .web import create_app
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Lighthouse only serves on this computer (127.0.0.1).")
    conn = db.connect(db.app_db())
    if not config.load().reader and fetch.needs_reprocess(conn):
        print("Upgrading your job database (one time, takes a moment)...")
        fetch.postprocess(conn)
    conn.close()
    url = f"http://{host}:{port}"
    print(f"\n  Lighthouse is running at {url}\n  Close this window or press Ctrl+C to stop.\n")
    scheduler.start()
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    create_app().run(host=host, port=port, debug=False, threaded=True)


def _index(args) -> None:
    import json
    from . import snapshot
    cfg = config.load()
    if args.action == "seed":   # builder: start from the published base when the Action's cache is gone
        if not args.file:
            raise SystemExit("usage: python -m lighthouse index seed base.db.xz [--db data/builder.db]")
        print(f"Seeded {snapshot.seed_builder(args.file, args.db or snapshot.BUILDER_DB)} postings.")
        return
    if args.action == "build":
        def num(v):   # the workflow passes repository variables, which may be empty
            try:
                return float(v) if v not in (None, "") else None
            except ValueError:
                raise SystemExit(f"not a number: {v!r}")
        m = snapshot.build(args.out, countries=args.countries.split(",") if args.countries else (),
                           db_path=args.db or snapshot.BUILDER_DB, detail_calls=args.detail_calls,
                           published=args.published, full=args.full, llm=not args.no_llm,
                           llm_minutes=num(args.llm_minutes), fetch_minutes=num(args.fetch_minutes))
        print(json.dumps(m, indent=2))
        return
    if args.action == "csv":
        src = args.db or args.file
        if not src:
            if cfg.reader:
                c = db.connect(db.app_db(cfg))
                src = db.attached_index(c)
                c.close()
            else:
                src = snapshot.BUILDER_DB if snapshot.BUILDER_DB.exists() else db.DEFAULT_DB
        if not src or not Path(src).exists():
            raise SystemExit("No job index here yet: start the app once (or pass --db FILE).")
        out = args.out if args.out != "dist" else snapshot.CSV_NAME
        info = snapshot.write_csv(src, out)
        print(f"Wrote {info['rows']} open roles to {out} ({info['bytes'] / 1e6:.1f} MB)")
        return
    conn = db.connect(db.app_db(cfg))
    if args.action == "status":
        for k, v in snapshot.status(conn, cfg).items():
            print(f"  {k:14} {v}")
        return
    if args.file:
        print(snapshot.import_file(conn, cfg, args.file))
        return
    if not cfg.reader:
        raise SystemExit('Set [index] repo = "owner/name" in config/sources.toml first.')
    from .net import default_client
    r = snapshot.sync(conn, cfg, default_client())
    if r["error"]:
        raise SystemExit(f"Job index not updated: {r['error']}")
    st = snapshot.status(conn, cfg)
    print(f"Job index from {st['built_at']}: {st['jobs']} open roles from {st['boards']} boards. {r['updated'] or 'Already up to date.'}")


def _discover(args) -> None:
    import json
    from . import discover, net
    cfg_dir = config.DEFAULT_CONFIG.parent
    companies = discover.load_companies(cfg_dir)
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
    sub.add_parser("fetch", help="pull all enabled job boards into the local DB")
    for name, help_text in (("serve", "run the local website"),
                            ("start", "fetch if empty, then serve and open the browser")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--port", type=int, default=5000)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    p = sub.add_parser("add", help="add boards from URLs, careers pages, ats:slug, or a text file of them")
    p.add_argument("items", nargs="+")
    p.add_argument("--no-check", action="store_true", help="don't test-fetch each board first")
    sub.add_parser("boards", help="list boards")
    sub.add_parser("reprocess", help="recompute clusters, extraction, ghost scores and eligibility")
    p = sub.add_parser("visa", help="sponsorship data")
    p.add_argument("action", choices=["import"])
    p.add_argument("files", nargs="*")
    p.add_argument("--force", action="store_true", help="re-import files already imported")
    p = sub.add_parser("claude", help="optional Claude passes")
    p.add_argument("action", choices=["extract", "fit", "poll"])
    p.add_argument("--limit", type=int, default=25)
    p = sub.add_parser("key", help="manage your Anthropic API key")
    p.add_argument("action", choices=["set", "clear"])
    sub.add_parser("mcp", help="MCP server over stdio for your own Claude app")
    p = sub.add_parser("index", help="the shared job index")
    p.add_argument("action", choices=["status", "import", "build", "seed", "csv"])
    p.add_argument("file", nargs="?", help="import: a local index file (.db.xz) instead of downloading")
    p.add_argument("--out", default="dist", help="build: output folder")
    p.add_argument("--db", default=None, help="build: builder database (default data/builder.db)")
    p.add_argument("--countries", default="", help="build: comma-separated country codes to keep (default: all)")
    p.add_argument("--detail-calls", type=int, default=None,
                   help="build: description calls per SmartRecruiters/Workday board per run")
    p.add_argument("--published", default=None, help="build: the manifest.json currently published")
    p.add_argument("--full", action="store_true", help="build: write a new base file even if a delta would do")
    p.add_argument("--no-llm", action="store_true",
                   help="build: rules only (no open-model gap fill, no category guesses)")
    p.add_argument("--llm-minutes", default=None,
                   help="build: minutes the open model reads unsettled postings (default from config, 20)")
    p.add_argument("--fetch-minutes", default=None,
                   help="build: start no new board or description call after this many minutes (default 40)")
    p = sub.add_parser("site", help="write the website (static files for GitHub Pages) from the builder database")
    p.add_argument("--db", default=None, help="builder database (default data/builder.db)")
    p.add_argument("--out", default="site_out", help="folder to write (replaced)")
    p.add_argument("--countries", default="US,CA,MX", help="comma-separated country codes to show (empty: all)")
    p.add_argument("--new-base", action="store_true", help="write a new weekly job list instead of a change file")
    p = sub.add_parser("discover", help="find job boards from the careers pages in config/companies.csv")
    p.add_argument("--only", default=None, help="only companies whose name contains this")
    p.add_argument("--out", default=None, help="write one JSON line per company here")
    p.add_argument("--workers", type=int, default=16)
    args = parser.parse_args()

    if args.cmd == "mcp":  # stdout is the protocol channel: log to stderr only
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
        from .mcp_server import serve
        serve()
        return
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "fetch":
        for r in fetch.run():
            print(r)
    elif args.cmd == "serve":
        _serve(args.host, args.port, open_browser=not args.no_browser)
    elif args.cmd == "start":
        cfg = config.load()
        conn = db.connect(db.app_db(cfg))
        if not cfg.reader:
            db.sync_config_boards(conn, cfg.boards)
        empty = not db.stats(conn)["total"]
        conn.close()
        if empty:
            print("First run: downloading the job index (a few hundred MB; a few minutes)..." if cfg.reader else
                  "First run: pulling jobs from your boards (this takes a few seconds)...")
            for r in fetch.run() or []:
                if r.get("error"):
                    print(f"  Couldn't download the job index: {r['error']}")
        _serve(args.host, _free_port(args.host, args.port), open_browser=not args.no_browser)
    elif args.cmd == "add":
        cfg = config.load()
        if cfg.reader:
            from .snapshot import suggest_url
            raise SystemExit("This copy reads the shared job index, so boards are added there. Suggest one at:\n  "
                             + suggest_url(cfg.index.repo))
        from .boards import add_many
        lines = []
        for item in args.items:
            p = Path(item)
            lines += p.read_text(encoding="utf-8").splitlines() if p.is_file() else [item]
        conn = db.connect(db.app_db(cfg))
        results = add_many(conn, lines, origin="cli", check=not args.no_check)
        for r in results:
            print(f"  ✕ {r['input']}: {r['error']}" if "error" in r else
                  f"  ✓ {r['ats']}:{r['slug']}" + (f" ({r['count']} postings)" if r["count"] is not None else ""))
        added = [(r["ats"], r["slug"]) for r in results if "error" not in r]
        conn.close()
        if added:
            print("Fetching the new boards...")
            fetch.run(only=added)
    elif args.cmd == "boards":
        conn = db.connect(db.app_db())
        if not config.load().reader:
            db.sync_config_boards(conn, config.load().boards)
        for b in db.list_boards(conn):
            state = "off" if not b["enabled"] else ("error: " + b["last_error"][:60]) if b["last_error"] else "ok"
            print(f"  {b['ats']:16} {b['slug']:30} {b['job_count'] or 0:>6}  {state}")
    elif args.cmd == "reprocess":
        if config.load().reader:
            raise SystemExit("This copy reads the shared job index, which is processed by its GitHub Action.")
        conn = db.connect()
        fetch.postprocess(conn)
        print("Done.")
    elif args.cmd == "visa":
        from . import sponsors
        conn = db.connect(db.app_db())
        results = ([sponsors.import_file(conn, f, force=args.force) for f in args.files]
                   if args.files else sponsors.import_folder(conn))
        for r in results:
            print(" ", r)
    elif args.cmd == "claude":
        from . import claude_api
        from . import profile as profile_mod
        cfg = config.load()
        conn = db.connect(db.app_db(cfg))
        if args.action == "poll":
            n = claude_api.poll_batches(conn, cfg)
            if n:
                fetch.refresh_derived(conn, cfg)
            print(f"{n} batch(es) collected.")
            return
        if args.action == "fit":
            prof = profile_mod.load(conn)
            rows = claude_api.fit_candidates(conn, profile_mod.fit_hash(prof), None, args.limit,
                                             cfg.settings.ghost_hide_signals, prof, cfg.settings.yoe_slack_years)
        else:
            rows = claude_api.extraction_candidates(conn, None, args.limit)
        print(claude_api.run(conn, cfg, args.action, rows))
        if args.action == "extract":
            fetch.refresh_derived(conn, cfg)
    elif args.cmd == "index":
        _index(args)
    elif args.cmd == "discover":
        _discover(args)
    elif args.cmd == "site":
        from . import site, snapshot
        stats = site.generate(args.db or snapshot.BUILDER_DB, args.out,
                              countries=[c for c in args.countries.split(",") if c.strip()], new_base=args.new_base)
        print(json.dumps(stats))
    elif args.cmd == "key":
        from . import claude_api
        if args.action == "set":
            claude_api.set_key(getpass.getpass("Anthropic API key (input hidden): "))
            print("Saved to your OS keychain.")
        else:
            claude_api.clear_key()
            print("Removed.")


if __name__ == "__main__":
    main()
