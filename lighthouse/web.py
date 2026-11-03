"""Local website. Binds to 127.0.0.1 — not exposed to your network.

Hardening: requests must carry a localhost Host header (blocks DNS rebinding), every
POST needs the per-process CSRF token, and posting HTML is allow-list sanitized at ingest.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup
from werkzeug.utils import secure_filename

from . import boards as boards_mod
from . import claude_api, config, db, extract, fetch, net, sponsors
from . import profile as profile_mod
from .sources import SOURCES

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
PER_PAGE = 50


def create_app(db_path: Path | str = db.DEFAULT_DB, config_path: Path | str = config.DEFAULT_CONFIG,
               visa_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["CSRF"] = secrets.token_urlsafe(32)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # LCA spreadsheets are big
    visa_dir = Path(visa_dir or sponsors.DATA_DIR)

    def conn():
        return db.connect(db_path)

    def cfg():
        return config.load(config_path)

    @app.before_request
    def guard():
        host = (request.host or "").rsplit(":", 1)[0] if not request.host.startswith("[") else request.host.split("]")[0] + "]"
        if host not in LOCAL_HOSTS:
            abort(403)
        if request.method == "POST" and request.form.get("csrf") != app.config["CSRF"]:
            abort(400, "Missing or invalid form token; reload the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                                "form-action 'self'; frame-ancestors 'none'")
        return resp

    @app.context_processor
    def globals_():
        return {"csrf": app.config["CSRF"], "fetch_status": fetch.STATUS, "claude_status": claude_api.STATUS}

    @app.template_filter("fromjson")
    def fromjson(value):
        return json.loads(value) if value else []

    @app.template_filter("money")
    def money(v, cur=None):
        if v is None:
            return ""
        sym = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$"}.get(cur or "", "")
        return f"{sym}{v / 1000:,.0f}K" if v >= 1000 else f"{sym}{v:,.0f}"

    @app.template_filter("day")
    def day(v):
        return (v or "")[:10]

    def back(default="index"):
        nxt = request.form.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for(default))

    # ------------------------------------------------------------ list

    def filters_from(args, c, settings, prof):
        def num(name, cast=float):
            try:
                v = args.get(name, "").replace(",", "").strip()
                return cast(v) if v else None
            except ValueError:
                return None
        prof_set = profile_mod.is_set(prof)
        return db.Filters(
            q=args.get("q", "").strip(), company=args.get("company", ""), source=args.get("source", ""),
            location=args.get("location", "").strip(), workplace=args.get("workplace", ""),
            min_salary=num("min_salary"), max_yoe=num("max_yoe", int), visa=args.get("visa", ""),
            eligible_only=args.get("elig", "1" if prof_set else "0") == "1",
            show_ghosts=args.get("ghosts") == "1", view=args.get("view", "all"), tier=args.get("tier", ""),
            sort=args.get("sort", "newest"), ghost_hide_at=settings.ghost_hide_signals,
            profile_hash=profile_mod.fit_hash(prof))

    def touch_visit(c) -> str | None:
        """'New since last visit' = first seen after the previous session (30+ min gap)."""
        now = datetime.now(timezone.utc)
        v = db.get_meta(c, "visit", {}) or {}
        last = v.get("last")
        if not last or now - datetime.fromisoformat(last) > timedelta(minutes=30):
            v = {"prev": last, "last": now.isoformat(timespec="seconds")}
        else:
            v["last"] = now.isoformat(timespec="seconds")
        db.set_meta(c, "visit", v)
        return v.get("prev")

    @app.get("/")
    def index():
        c = conn()
        s = cfg().settings
        prof = profile_mod.load(c)
        f = filters_from(request.args, c, s, prof)
        f.since = touch_visit(c)
        page = max(1, request.args.get("page", 1, type=int))
        jobs, total = db.list_jobs(c, f, page=page, per_page=PER_PAGE)
        args = {k: v for k, v in request.args.items() if k != "page"}
        ctx = dict(jobs=jobs, total=total, f=f, page=page, pages=max(1, -(-total // PER_PAGE)),
                   args=args, qs=lambda **kw: "?" + urlencode({**args, **kw}),
                   companies=db.companies(c), sources=db.sources_in_use(c),
                   stats=db.stats(c, s.ghost_hide_signals), marks=db.mark_counts(c),
                   profile_set=profile_mod.is_set(prof), settings=s, has_key=bool(claude_api.get_key()),
                   new_count=db.list_jobs(c, db.Filters(view="new", since=f.since, ghost_hide_at=s.ghost_hide_signals,
                                                        eligible_only=f.eligible_only), 1, 1)[1] if f.since else 0)
        c.close()
        return render_template("index.html", **ctx)

    @app.get("/job/<path:uid>")
    def job(uid):
        c = conn()
        row = db.get_job(c, uid)
        if row is None:
            c.close()
            abort(404)
        prof = profile_mod.load(c)
        members = db.cluster_members(c, row["cluster_id"] or uid)
        data, method = extract.get(c, row["content_hash"])
        fit = c.execute("SELECT * FROM fit WHERE content_hash=? AND profile_hash=?",
                        (row["content_hash"], profile_mod.fit_hash(prof))).fetchone()
        ctx = dict(job=row, members=members, events=db.events_for(c, [m["uid"] for m in members]),
                   extracted=data, method=method, fit=fit, sponsor=sponsors.history(c, row["company_key"]),
                   mark=db.mark_for_cluster(c, row["cluster_id"] or uid), settings=cfg().settings,
                   body=Markup(row["content_html"] or ""))  # sanitized with an allow-list at ingest
        c.close()
        return render_template("job.html", **ctx)

    @app.post("/mark")
    def mark():
        c = conn()
        status = request.form.get("status") or None
        db.set_mark(c, request.form["uid"], status if status in ("saved", "applied", "hidden") else None)
        c.close()
        return back()

    @app.post("/refresh")
    def refresh():
        threading.Thread(target=fetch.run, kwargs={"config_path": config_path, "db_path": db_path, "wait": False},
                         daemon=True).start()
        return back()

    @app.get("/status")
    def status():
        return jsonify(fetch=fetch.STATUS, claude=claude_api.STATUS)

    # ------------------------------------------------------------ sources

    @app.get("/sources")
    def sources_page():
        c = conn()
        db.sync_config_boards(c, cfg().boards)
        rows = db.list_boards(c)
        c.close()
        return render_template("sources.html", boards=rows, ats=SOURCES,
                               results=json.loads(request.args.get("r", "[]") or "[]"))

    @app.post("/sources/add")
    def sources_add():
        lines = (request.form.get("urls") or "").splitlines()[:200]
        c = conn()
        results = boards_mod.add_many(c, lines, origin="ui")
        c.close()
        added = [(r["ats"], r["slug"]) for r in results if "error" not in r]
        if added:
            threading.Thread(target=fetch.run, kwargs={"config_path": config_path, "db_path": db_path,
                                                       "only": added, "wait": True}, daemon=True).start()
        return redirect(url_for("sources_page", r=json.dumps(results)))

    @app.post("/sources/toggle")
    def sources_toggle():
        c = conn()
        db.set_board_enabled(c, request.form["ats"], request.form["slug"], request.form.get("enabled") == "1")
        c.close()
        return redirect(url_for("sources_page"))

    # ------------------------------------------------------------ profile

    @app.get("/profile")
    def profile_page():
        c = conn()
        prof = profile_mod.load(c)
        sample = c.execute("SELECT j.uid, j.title, j.company, j.location, c.html AS content_html, j.content_hash "
                           "FROM jobs j JOIN job_content c ON c.uid = j.uid "
                           "WHERE j.active=1 AND j.is_canonical=1 AND c.html <> '' "
                           "ORDER BY j.effective_first_seen DESC LIMIT 1").fetchone()
        preview = json.dumps(claude_api.fit_params(cfg(), prof, sample), indent=2, ensure_ascii=False) if sample else ""
        c.close()
        return render_template("profile.html", p=prof, preview=preview, saved=request.args.get("saved"))

    @app.post("/profile")
    def profile_save():
        form = request.form
        def int_or_none(v):
            try:
                return int(float(v.replace(",", ""))) if v and v.strip() else None
            except ValueError:
                return None
        p = {
            "about": form.get("about", "").strip(),
            "resume": form.get("resume", "").strip(),
            "years_experience": int_or_none(form.get("years_experience")),
            "degree": form.get("degree", "none"),
            "needs_sponsorship": form.get("needs_sponsorship") == "1",
            "us_citizen": form.get("us_citizen") == "1",
            "clearance": form.get("clearance", "none"),
            "locations": [l.strip() for l in form.get("locations", "").replace(";", ",").split(",") if l.strip()],
            "workplaces": form.getlist("workplaces") or ["remote", "hybrid", "onsite"],
            "min_salary": int_or_none(form.get("min_salary")),
        }
        c = conn()
        profile_mod.save(c, p)
        profile_mod.recompute(c, None, cfg().settings.yoe_slack_years, force=True)
        c.close()
        return redirect(url_for("profile_page", saved=1))

    # ------------------------------------------------------------ claude

    @app.get("/claude")
    def claude_page():
        c = conn()
        k = cfg()
        prof = profile_mod.load(c)
        ph = profile_mod.fit_hash(prof)
        ctx = dict(
            key_source=claude_api.key_source(), cap=k.claude.monthly_cap_usd, spent=claude_api.month_spend(c),
            reserved=claude_api.reserved(c), claude=k.claude, profile_set=profile_mod.is_set(prof),
            batches=c.execute("SELECT * FROM claude_batches ORDER BY submitted_at DESC LIMIT 10").fetchall(),
            usage=c.execute("SELECT purpose, model, COUNT(*) n, SUM(cost_usd) usd FROM api_usage "
                            "WHERE at >= ? GROUP BY purpose, model",
                            (datetime.now(timezone.utc).strftime("%Y-%m-01"),)).fetchall(),
            pending_extract=len(claude_api.extraction_candidates(c, None, 100000)),
            pending_fit=len(claude_api.fit_candidates(c, ph, None, 100000, k.settings.ghost_hide_signals)),
            keyring_ok=_keyring_ok())
        c.close()
        return render_template("claude.html", **ctx)

    @app.post("/claude/key")
    def claude_key():
        if request.form.get("clear"):
            claude_api.clear_key()
        elif request.form.get("key", "").strip().startswith("sk-"):
            claude_api.set_key(request.form["key"])
        return redirect(url_for("claude_page"))

    @app.post("/claude/run")
    def claude_run():
        purpose = request.form.get("purpose")
        if purpose not in ("fit", "extract"):
            abort(400)
        uids = request.form.getlist("uid") or None
        limit = max(1, min(request.form.get("limit", 25, type=int), 2000))
        started = claude_api.run_in_background(db_path, cfg(), purpose, uids, limit)
        if not started:
            claude_api.STATUS["message"] = "Another Claude run is still going."
        return back("claude_page")

    @app.post("/claude/poll")
    def claude_poll():
        c = conn()
        try:
            if claude_api.poll_batches(c, cfg()):
                fetch.refresh_derived(c, cfg())
        except Exception as exc:
            claude_api.STATUS["message"] = f"poll: {exc}"
        c.close()
        return redirect(url_for("claude_page"))

    # ------------------------------------------------------------ sponsorship

    @app.get("/sponsorship")
    def sponsorship_page():
        c = conn()
        ctx = dict(files=sponsors.files(c), folder=visa_dir,
                   matched=c.execute("SELECT COUNT(*) FROM sponsor_match").fetchone()[0],
                   results=json.loads(request.args.get("r", "[]") or "[]"))
        c.close()
        return render_template("sponsorship.html", **ctx)

    @app.post("/sponsorship/import")
    def sponsorship_import():
        visa_dir.mkdir(parents=True, exist_ok=True)
        for up in request.files.getlist("files"):
            if up and up.filename:
                up.save(visa_dir / secure_filename(up.filename))
        c = conn()
        results = sponsors.import_folder(c, visa_dir)
        c.close()
        return redirect(url_for("sponsorship_page", r=json.dumps(results)))

    # ------------------------------------------------------------ network

    @app.get("/network")
    def network_page():
        client = net.default_client()
        return render_template("network.html", allowed=net.ALLOWED_HOSTS, counts=sorted(client.counts.items()),
                               blocked=sorted(client.blocked.items()))

    return app


def _keyring_ok() -> bool:
    try:
        import keyring
        return not type(keyring.get_keyring()).__name__.lower().startswith(("fail", "null"))
    except Exception:
        return False
