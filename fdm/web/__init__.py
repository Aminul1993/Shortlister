"""Flask web portal: browse the shortlist, select gigs, and watch their screenshots and proposals being made."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for

from ..config import Settings
from ..generator import PROPOSAL_DIR, SCREENSHOT_DIR, proposal_path, screenshot_path
from ..pages import make_demo_id
from ..ranking import rank_gigs
from ..sources import fetch_gigs
from ..store import gig_key, load_shortlist, save_shortlist, shortlist_updated_at
from .jobs import JobManager

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, top: int = 10) -> Flask:
    settings = settings or Settings.load()
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.getenv("FDM_PORTAL_SECRET") or secrets.token_hex(32)
    jobs = JobManager(settings)
    app.extensions["fdm_jobs"] = jobs

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def check_csrf():
        # Every POST spends LLM tokens or queries the gig feeds, so other sites must not be able to trigger one.
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Missing or expired form token. Reload the page and try again.")

    @app.get("/")
    def index():
        rows = []
        for rank, scored in enumerate(load_shortlist(settings.output_dir), 1):
            demo_id = make_demo_id(scored.gig)
            shot = screenshot_path(settings.output_dir, demo_id)
            proposal = proposal_path(settings.output_dir, demo_id)
            rows.append({"rank": rank, "key": gig_key(scored.gig), "item": scored,
                         "screenshot": shot.name if shot.is_file() else None,
                         "proposal": proposal.name if proposal.is_file() else None})
        return render_template(
            "index.html",
            rows=rows,
            updated=shortlist_updated_at(settings.output_dir),
            jobs=jobs.recent(),
            can_generate=bool(settings.ollama_api_key),
        )

    @app.post("/refresh")
    def refresh():
        try:
            gigs = asyncio.run(fetch_gigs(settings))
        except Exception as exc:  # network/Playwright failures become a message, not a 500 page
            log.exception("Refreshing the shortlist failed")
            flash(f"Could not fetch gigs: {exc}", "error")
            return redirect(url_for("index"))
        if not gigs:
            flash("No gigs were fetched. Check your network connection and FDM_* settings.", "error")
            return redirect(url_for("index"))
        ranked = rank_gigs(gigs)[:top]
        save_shortlist(ranked, settings.output_dir)
        flash(f"Scanned {len(gigs)} gigs and refreshed the top {len(ranked)}.", "ok")
        return redirect(url_for("index"))

    @app.post("/jobs")
    def create_job():
        if not settings.ollama_api_key:
            flash("Set OLLAMA_API_KEY in .env and restart the portal before creating screenshots.", "error")
            return redirect(url_for("index"))
        shortlist = {gig_key(s.gig): (rank, s) for rank, s in enumerate(load_shortlist(settings.output_dir), 1)}
        selected = request.form.getlist("gig")
        chosen = [shortlist[key] for key in selected if key in shortlist]
        if not chosen:
            flash("The shortlist changed since you loaded it. Select again." if selected
                  else "Select at least one gig.", "error")
            return redirect(url_for("index"))
        job_id = jobs.submit(chosen)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        return render_template("job.html", job=job)

    @app.get("/jobs/<job_id>/items")
    def job_items(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            abort(404)
        return render_template("_job_items.html", job=job)

    @app.get("/screenshots/<path:name>")
    def screenshot(name: str):
        return send_from_directory((settings.output_dir / SCREENSHOT_DIR).resolve(), name)

    @app.get("/proposals/<path:name>")
    def proposal(name: str):
        return send_from_directory((settings.output_dir / PROPOSAL_DIR).resolve(), name)

    return app
