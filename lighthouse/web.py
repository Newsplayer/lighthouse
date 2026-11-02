"""Local website. Binds to 127.0.0.1 by default — not exposed to your network."""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, abort, redirect, render_template, request, url_for
from markupsafe import Markup

from . import db, fetch


def create_app(db_path: Path | str = db.DEFAULT_DB) -> Flask:
    app = Flask(__name__)

    def conn():
        return db.connect(db_path)

    @app.template_filter("fromjson")
    def fromjson(value):
        return json.loads(value) if value else []

    @app.get("/")
    def index():
        q = request.args.get("q", "").strip()
        company = request.args.get("company", "")
        hide_ghosts = request.args.get("hide_ghosts") == "1"
        c = conn()
        jobs = db.list_jobs(c, q=q, company=company, hide_ghosts=hide_ghosts)
        ctx = dict(jobs=jobs, q=q, company=company, hide_ghosts=hide_ghosts,
                   companies=db.companies(c), stats=db.stats(c))
        c.close()
        return render_template("index.html", **ctx)

    @app.get("/job/<path:uid>")
    def job(uid):
        c = conn()
        row = db.get_job(c, uid)
        if row is None:
            c.close()
            abort(404)
        dups = db.duplicates_of(c, row["fingerprint"], uid)
        c.close()
        # content_html was sanitized with an allow-list at ingest (lighthouse/sanitize.py).
        return render_template("job.html", job=row, dups=dups, body=Markup(row["content_html"] or ""))

    @app.post("/refresh")
    def refresh():
        fetch.run(db_path=db_path)
        return redirect(url_for("index"))

    return app
