"""CLI:
    python -m lighthouse start    # one step: fetch if empty, pick a free port, open browser, serve
    python -m lighthouse fetch    # pull all configured boards
    python -m lighthouse serve [--port 5000]
"""

import argparse
import logging
import socket
import threading
import webbrowser

from . import db, fetch


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
    from .web import create_app
    url = f"http://{host}:{port}"
    print(f"\n  Lighthouse is running at {url}\n  Close this window or press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=[url]).start()
    create_app().run(host=host, port=port, debug=False)


def main() -> None:
    parser = argparse.ArgumentParser(prog="lighthouse")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch", help="pull all configured job boards into the local DB")
    for name, help_text in (("serve", "run the local website"),
                            ("start", "fetch if empty, then serve and open the browser")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--port", type=int, default=5000)
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.cmd == "fetch":
        for r in fetch.run():
            print(r)
    elif args.cmd == "serve":
        _serve(args.host, args.port, open_browser=not args.no_browser)
    elif args.cmd == "start":
        conn = db.connect()
        empty = not db.stats(conn)["total"]
        conn.close()
        if empty:
            print("First run: pulling jobs from your boards (this takes a few seconds)...")
            fetch.run()
        _serve(args.host, _free_port(args.host, args.port), open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
