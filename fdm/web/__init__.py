"""Flask web portal: browse the shortlist, select gigs, watch their proposals being written, look back at
every saved version, and place a reviewed bid on Freelancer.com."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import secrets
import threading

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for

from ..bids import PLATFORM as BID_PLATFORM
from ..bids import BidError, bid_amount, lookup, place_bid, placed_bids, save_bid
from ..config import Settings
from ..models import ScoredGig
from ..ranking import rank_gigs
from ..sources import fetch_gigs
from ..store import gig_key, load_shortlist, make_demo_id, save_shortlist, shortlist_updated_at
from ..versions import DEMO_ID, PROPOSAL_DIR, Version, mentions_screenshot, proposals
from .jobs import JobManager, bid_label

log = logging.getLogger(__name__)


def can_bid_on(scored: ScoredGig) -> bool:
    return scored.gig.platform == BID_PLATFORM and scored.gig.gig_id.isdigit()


def read_bid_form(form) -> tuple[dict, dict | None, str | None]:
    """The form as typed (to show again), the values to send, and what is wrong with them, if anything."""
    typed = {name: form.get(name, "").strip() for name in ("amount", "period", "milestone_percentage")}
    typed["description"] = form.get("description", "").replace("\r\n", "\n").strip()
    try:
        amount, period, milestone = float(typed["amount"]), int(typed["period"]), int(typed["milestone_percentage"])
    except ValueError:
        return typed, None, "Enter the bid amount, delivery days and upfront milestone as numbers."
    if not (math.isfinite(amount) and amount > 0):
        return typed, None, "The bid amount must be more than zero."
    if period < 1:
        return typed, None, "Delivery must take at least one day."
    if not 0 <= milestone <= 100:
        return typed, None, "The upfront milestone is a percentage from 0 to 100."
    if not typed["description"]:
        return typed, None, "The proposal is empty."
    values = {"amount": round(amount, 2), "period": period, "milestone_percentage": milestone,
              "description": typed["description"]}
    return typed, values, None


def create_app(settings: Settings | None = None) -> Flask:
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
    app.jinja_env.globals["bid_amount"] = bid_amount
    # Held from the duplicate check until the bid is recorded, so a double submit can't send two bids.
    bid_lock = threading.Lock()

    def shortlisted(demo_id: str) -> tuple[int, ScoredGig] | None:
        return next(((rank, s) for rank, s in enumerate(load_shortlist(settings.output_dir), 1)
                     if make_demo_id(s.gig) == demo_id), None)

    @app.before_request
    def check_csrf():
        # Every POST spends LLM tokens, queries the gig feeds or places a bid, so other sites must not trigger one.
        if request.method == "POST":
            sent = request.form.get("csrf", "")
            if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Missing or expired form token. Reload the page and try again.")

    @app.get("/")
    def index():
        rows = []
        for rank, scored in enumerate(load_shortlist(settings.output_dir), 1):
            demo_id = make_demo_id(scored.gig)
            texts = proposals(settings.output_dir, demo_id)
            # The bid shown is the one suggested with the latest proposal, since that is the one that gets sent.
            meta = texts[-1].meta if texts else {}
            placed = placed_bids(settings.output_dir, demo_id)
            rows.append({"rank": rank, "key": gig_key(scored.gig), "item": scored, "demo_id": demo_id,
                         "proposal": texts[-1].number if texts else None,
                         "bid": bid_label(meta.get("bid_usd"), meta.get("pay_type")),
                         "delivery_days": meta.get("delivery_days"),
                         "placed": placed[-1] if placed else None})
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
        ranked = rank_gigs(gigs)[:settings.shortlist_size]
        save_shortlist(ranked, settings.output_dir)
        flash(f"Scanned {len(gigs)} gigs and refreshed the top {len(ranked)}.", "ok")
        return redirect(url_for("index"))

    @app.post("/jobs")
    def create_job():
        if not settings.ollama_api_key:
            flash("Set OLLAMA_API_KEY in .env and restart the portal before writing proposals.", "error")
            return redirect(url_for("index"))
        shortlist = {gig_key(s.gig): (rank, s) for rank, s in enumerate(load_shortlist(settings.output_dir), 1)}
        selected = list(dict.fromkeys(request.form.getlist("gig")))  # a gig twice would race for one version number
        chosen = [shortlist[key] for key in selected if key in shortlist]
        if not chosen:
            flash("The shortlist changed since you loaded it. Select again." if selected
                  else "Select at least one gig.", "error")
            return redirect(url_for("index"))
        job_id = jobs.submit(chosen)
        return redirect(url_for("job_detail", job_id=job_id))

    @app.get("/demos/<demo_id>")
    def demo(demo_id: str):
        """Every saved proposal of one gig, newest first, the bids placed on it, and a button to write a new one."""
        if not DEMO_ID.fullmatch(demo_id):
            abort(404)
        entry = shortlisted(demo_id)
        texts = proposals(settings.output_dir, demo_id)
        placed = placed_bids(settings.output_dir, demo_id)
        if entry is None and not texts and not placed:
            abort(404)
        written = []
        for version in texts:
            try:
                text = version.path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            meta = version.meta
            written.append({"version": version, "text": text,
                            "bid": bid_label(meta.get("bid_usd"), meta.get("pay_type")),
                            "delivery_days": meta.get("delivery_days")})
        return render_template(
            "demo.html",
            demo_id=demo_id,
            rank=entry[0] if entry else None,
            scored=entry[1] if entry else None,
            key=gig_key(entry[1].gig) if entry else None,
            proposals=written[::-1],
            can_generate=bool(settings.ollama_api_key),
            biddable=bool(entry and can_bid_on(entry[1])),
            can_bid=bool(settings.freelancer_oauth_token),
            placed=placed[::-1],
        )

    def bid_target(demo_id: str) -> tuple[ScoredGig, Version]:
        """The gig and the proposal a bid would send, or a redirect saying why there is nothing to bid with."""
        entry = shortlisted(demo_id) if DEMO_ID.fullmatch(demo_id) else None
        if entry is None or not can_bid_on(entry[1]):
            abort(404)
        texts = proposals(settings.output_dir, demo_id)
        if not settings.freelancer_oauth_token:
            flash("Set FREELANCER_OAUTH_TOKEN in .env and restart the portal to place bids.", "error")
        elif not texts:
            flash("Write a proposal first. The bid is placed with it.", "error")
        else:
            return entry[1], texts[-1]
        abort(redirect(url_for("demo", demo_id=demo_id)))

    def bid_page(demo_id: str, scored: ScoredGig, proposal: Version, typed: dict | None = None,
                 error: str | None = None):
        """The review form: the live project, the account the bid comes from, and the bid itself, editable."""
        try:
            account, project = lookup(settings.freelancer_oauth_token, int(scored.gig.gig_id))
        except BidError as exc:
            if error:
                flash(error, "error")
            flash(str(exc), "error")
            return redirect(url_for("demo", demo_id=demo_id))
        meta = proposal.meta
        if typed is None:
            try:
                text = proposal.path.read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            suggested = project.amount_from_usd(meta.get("bid_usd"))
            typed = {"amount": "" if suggested is None else str(suggested),
                     "period": str(meta.get("delivery_days") or ""), "milestone_percentage": "100",
                     "description": text}
        return render_template(
            "bid.html",
            demo_id=demo_id,
            scored=scored,
            proposal=proposal,
            suggested=bid_label(meta.get("bid_usd"), meta.get("pay_type")),
            # Nothing can be attached to a bid, so an older proposal that points to a screenshot needs rewording.
            mentions_screenshot=mentions_screenshot(proposal),
            account=account,
            project=project,
            placed=placed_bids(settings.output_dir, demo_id),
            form=typed,
            error=error,
        )

    @app.get("/demos/<demo_id>/bid")
    def bid_form(demo_id: str):
        scored, proposal = bid_target(demo_id)
        return bid_page(demo_id, scored, proposal)

    @app.post("/demos/<demo_id>/bid")
    def bid(demo_id: str):
        scored, proposal = bid_target(demo_id)
        typed, values, error = read_bid_form(request.form)
        with bid_lock:
            if placed_bids(settings.output_dir, demo_id):
                flash("A bid on this gig has already been placed. Freelancer.com takes one bid per project, so "
                      "change it on the gig page.", "error")
                return redirect(url_for("demo", demo_id=demo_id))
            if not error:
                try:
                    record = place_bid(settings.freelancer_oauth_token, int(scored.gig.gig_id), **values)
                except BidError as exc:
                    error = str(exc)
                else:
                    record.update(proposal_version=proposal.number, description=values["description"])
                    save_bid(settings.output_dir, demo_id, record)
        if error:  # shown above the form, with the edits kept
            return bid_page(demo_id, scored, proposal, typed, error)
        days = record["period"]
        flash(f"Bid placed on Freelancer.com: {bid_amount(record)}, {days} day{'s' if days != 1 else ''}.", "ok")
        return redirect(url_for("demo", demo_id=demo_id) + "#bids")

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

    @app.get("/proposals/<path:name>")
    def proposal(name: str):
        return send_from_directory((settings.output_dir / PROPOSAL_DIR).resolve(), name)

    return app
