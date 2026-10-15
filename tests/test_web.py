import re
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import fdm.web as web_module
import fdm.web.jobs as jobs_module
from fdm.bids import Account, BidError, Project, placed_bids
from fdm.config import Settings
from fdm.models import Gig, Pitch, Proposal, ScoredGig
from fdm.store import gig_key, load_shortlist, make_demo_id, save_shortlist
from fdm.versions import save_proposal
from fdm.web import create_app


def scored(gig_id: str, title: str, score: float) -> ScoredGig:
    gig = Gig(platform="Freelancer.com", gig_id=gig_id, title=title, description=f"{title} brief",
              url=f"https://example.com/{gig_id}", budget_max_usd=120.0, pay_type="fixed")
    return ScoredGig(gig=gig, score=score, breakdown={"velocity": 1.0}, signals=["+landing page", "-login"])


SHORTLIST = [scored("101", "Bakery landing page", 9.4), scored("202", "BMI calculator", 8.8)]


@pytest.fixture
def settings(tmp_path):
    base = Settings.load(env_file=tmp_path / "missing.env")
    return replace(base, output_dir=tmp_path, ollama_api_key="test-key")


@pytest.fixture
def client(settings):
    return create_app(settings).test_client()


def csrf(client) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', client.get("/").get_data(as_text=True)).group(1)


def wait_for_job(app, job_id: str):
    manager = app.extensions["fdm_jobs"]
    for _ in range(200):
        job = manager.get(job_id)
        if job.finished:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_shortlist_round_trips_through_store(tmp_path):
    save_shortlist(SHORTLIST, tmp_path)
    loaded = load_shortlist(tmp_path)
    assert [s.gig for s in loaded] == [s.gig for s in SHORTLIST]
    assert (tmp_path / "top10.md").read_text(encoding="utf-8").startswith("| Rank |")


def test_index_shows_empty_state(client):
    html = client.get("/").get_data(as_text=True)
    assert "Nothing shortlisted yet" in html


def test_index_lists_shortlist_with_selection_controls(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    html = client.get("/").get_data(as_text=True)
    assert "Bakery landing page" in html and "BMI calculator" in html
    assert f'value="{gig_key(SHORTLIST[0].gig)}"' in html
    assert "9.4" in html and "up to $120" in html
    assert "Write proposals</button>" in html and "creenshot" not in html


def test_posts_require_csrf_token(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    assert client.post("/jobs", data={"gig": gig_key(SHORTLIST[0].gig)}).status_code == 400
    assert client.post("/refresh", data={"csrf": "forged"}).status_code == 400


def test_empty_selection_is_rejected(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    response = client.post("/jobs", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Select at least one gig." in response.get_data(as_text=True)


def test_create_job_writes_proposals_for_selected_gigs(settings, monkeypatch):
    written: list[str] = []

    async def fake_generate(llm, scored_gig, settings_):
        written.append(scored_gig.gig.gig_id)
        demo_id = make_demo_id(scored_gig.gig)
        pitch = Pitch("You want a BMI calculator <that> works.\n\nVisitors enter height and weight.", 95, 2)
        saved = save_proposal(settings_.output_dir, demo_id, pitch.message + "\n", {"bid_usd": 95})
        return Proposal(demo_id, scored_gig, pitch, "fake-model", saved.path, saved.number)

    monkeypatch.setattr(jobs_module, "generate_proposal", fake_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()

    key = gig_key(SHORTLIST[1].gig)
    response = client.post("/jobs", data={"csrf": csrf(client), "gig": [key, key]})
    assert response.status_code == 302
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done"
    assert written == ["202"]  # the duplicate key is dropped
    item = job.items[0]
    assert item.state == "done" and item.model == "fake-model" and item.proposal_version == 1

    # The proposal is shown escaped with its line breaks, ready to copy, beside the suggested bid.
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "BMI calculator" in page and "Written by</dt><dd>fake-model" in page and "<img" not in page
    assert item.bid == "~$95" and item.delivery_days == 2
    assert 'id="proposal-1">You want a BMI calculator &lt;that&gt; works.\n\nVisitors enter height and weight.</p>' in page
    assert 'data-copy="proposal-1"' in page and "suggested bid ~$95" in page and "2 days" in page
    text = client.get("/proposals/bmi-calculator-202.txt")
    assert text.mimetype == "text/plain" and text.get_data(as_text=True).startswith("You want a BMI calculator")
    text.close()
    assert client.get("/proposals/..%2Ftop10.json").status_code == 404
    assert client.get("/screenshots/bmi-calculator-202.png").status_code == 404
    index = client.get("/").get_data(as_text=True)
    assert "proposal v1</a>" in index and "1 proposal</a>" in index
    fragment = client.get(f"/jobs/{job_id}/items").get_data(as_text=True)
    assert 'data-finished="true"' in fragment and "1 of 1 finished" in fragment
    assert client.get("/jobs/nope").status_code == 404


def test_failing_gig_does_not_stop_the_others(settings, monkeypatch):
    async def flaky_generate(llm, scored_gig, settings_):
        if scored_gig.gig.gig_id == "101":
            raise RuntimeError("disk full")
        demo_id = make_demo_id(scored_gig.gig)
        saved = save_proposal(settings_.output_dir, demo_id, "A pitch.\n", {})
        return Proposal(demo_id, scored_gig, Pitch("A pitch."), "fake-model", saved.path, saved.number)

    monkeypatch.setattr(jobs_module, "generate_proposal", flaky_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    keys = [gig_key(s.gig) for s in SHORTLIST]
    job_id = client.post("/jobs", data={"csrf": csrf(client), "gig": keys}).headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done"
    states = {item.title: (item.state, item.error) for item in job.items}
    assert states["Bakery landing page"] == ("failed", "disk full")
    assert states["BMI calculator"][0] == "done"
    assert "1 failed" in client.get(f"/jobs/{job_id}").get_data(as_text=True)


def test_job_is_failed_when_every_gig_fails(settings, monkeypatch):
    async def broken_generate(llm, scored_gig, settings_):
        raise RuntimeError("Ollama Cloud rejected the API key")

    monkeypatch.setattr(jobs_module, "generate_proposal", broken_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    data = {"csrf": csrf(client), "gig": [gig_key(SHORTLIST[0].gig)]}
    job_id = client.post("/jobs", data=data).headers["Location"].rsplit("/", 1)[-1]
    assert wait_for_job(app, job_id).state == "failed"


def test_demo_page_lists_every_proposal_newest_first(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    out, demo_id = settings.output_dir, make_demo_id(SHORTLIST[0].gig)
    save_proposal(out, demo_id, "First pitch for the bakery.\n",
                  {"bid_usd": 90, "pay_type": "fixed", "delivery_days": 3})
    save_proposal(out, demo_id, "Second pitch for the bakery.\n", {})

    index = client.get("/").get_data(as_text=True)
    assert f'href="/demos/{demo_id}#proposals">proposal v2</a>' in index
    assert "tag-bid" not in index  # only the latest proposal's bid is shown, and v2 was saved without one

    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert "Bakery landing page" in page and "2 proposals" in page and "creenshot" not in page
    assert page.index("Proposal v2") < page.index("Second pitch") < page.index("Proposal v1") < page.index("First pitch")
    assert "suggested bid ~$90" in page and "3 days" in page
    assert f'name="gig" value="{gig_key(SHORTLIST[0].gig)}"' in page
    assert re.search(r'type="submit" >New proposal</button>', page)  # enabled

    assert client.get("/demos/no-such-gig").status_code == 404
    assert client.get("/demos/..%2Ftop10").status_code == 404


def test_demo_page_for_a_gig_no_longer_shortlisted(client, settings):
    (settings.output_dir / "proposals").mkdir()
    (settings.output_dir / "proposals" / "old-gig-9.txt").write_text("An old pitch.", encoding="utf-8")
    page = client.get("/demos/old-gig-9").get_data(as_text=True)
    assert "No longer on the shortlist" in page and "An old pitch." in page and "New proposal</button>" not in page


def test_index_shows_the_bid_suggested_with_the_latest_proposal(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    out = settings.output_dir
    save_proposal(out, make_demo_id(SHORTLIST[0].gig), "A pitch.\n",
                  {"bid_usd": 90, "pay_type": "fixed", "delivery_days": 3})
    save_proposal(out, make_demo_id(SHORTLIST[1].gig), "A pitch.\n",
                  {"bid_usd": 15, "pay_type": "hourly", "delivery_days": 1})
    index = client.get("/").get_data(as_text=True)
    assert "bid ~$90 · 3 days</a>" in index and "bid ~$15/hr · 1 day</a>" in index
    assert 'title="Suggested with proposal v1"' in index


INR_PROJECT = Project(101, "Bakery landing page", "open", "fixed", "INR", "₹", 0.012, 1500.0, 12500.0, 7, 6400.0)


@pytest.fixture
def bidding(settings, monkeypatch):
    """A portal with a Freelancer.com token, a shortlisted gig with a proposal, and a fake Freelancer.com API."""
    settings = replace(settings, freelancer_oauth_token="fl-token")
    save_shortlist(SHORTLIST, settings.output_dir)
    demo_id = make_demo_id(SHORTLIST[0].gig)
    save_proposal(settings.output_dir, demo_id, "Customers pick a cake and see the total before they order.\n",
                  {"bid_usd": 60, "pay_type": "fixed", "delivery_days": 4})
    sent = []

    def fake_place(token, project_id, **values):
        sent.append((token, project_id, values))
        return {"bid_id": 555, "project_id": project_id, "bidder": "aminul", "amount": values["amount"],
                "currency": "INR", "sign": "₹", "pay_type": "fixed", "period": values["period"],
                "milestone_percentage": values["milestone_percentage"], "placed_at": "2026-10-15T09:30:00"}

    monkeypatch.setattr(web_module, "lookup", lambda token, project_id: (Account(42, "aminul"), INR_PROJECT))
    monkeypatch.setattr(web_module, "place_bid", fake_place)
    client = create_app(settings).test_client()
    return SimpleNamespace(client=client, settings=settings, demo_id=demo_id, url=f"/demos/{demo_id}/bid", sent=sent)


def test_bid_review_page_shows_the_live_project_and_an_editable_bid(bidding, monkeypatch):
    demo = bidding.client.get(f"/demos/{bidding.demo_id}").get_data(as_text=True)
    assert f'href="{bidding.url}">Review &amp; place bid</a>' in demo

    page = bidding.client.get(bidding.url).get_data(as_text=True)
    assert "Bidding as</dt><dd>aminul" in page and "7 bids so far, averaging ₹6,400 INR" in page
    assert "₹1,500 INR to ₹12,500 INR" in page and "Bid amount (INR)" in page
    assert 'name="amount" value="5000"' in page and 'name="period" value="4"' in page  # $60 in rupees
    assert 'required>Customers pick a cake and see the total before they order.</textarea>' in page
    assert "creenshot" not in page and 'type="submit" >Place bid on Freelancer.com' in page

    # An older proposal written to go with a screenshot is flagged for rewording, since nothing goes with a bid.
    save_proposal(bidding.settings.output_dir, bidding.demo_id, "The attached screenshot shows your page.\n",
                  {"screenshot_version": 1})
    page = bidding.client.get(bidding.url).get_data(as_text=True)
    assert "Proposal v2 was written to go with a screenshot" in page and 'name="amount" value=""' in page

    closed = replace(INR_PROJECT, status="closed", pay_type="hourly")
    monkeypatch.setattr(web_module, "lookup", lambda token, project_id: (Account(42, "aminul"), closed))
    page = bidding.client.get(bidding.url).get_data(as_text=True)
    assert "This project is closed on Freelancer.com" in page and 'type="submit" disabled>Place bid' in page
    assert "Hourly rate (INR)" in page and "₹12,500 INR per hour" in page


def test_placing_a_bid_sends_the_edited_form_and_records_it(bidding):
    client, url = bidding.client, bidding.url
    form = {"csrf": csrf(client), "amount": "5200", "period": "3", "milestone_percentage": "50",
            "description": "Edited pitch.\r\n\r\nSecond paragraph."}
    assert client.post(url, data={**form, "csrf": "forged"}).status_code == 400

    response = client.post(url, data=form)
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/demos/{bidding.demo_id}#bids")
    assert bidding.sent == [("fl-token", 101, {"amount": 5200.0, "period": 3, "milestone_percentage": 50,
                                                "description": "Edited pitch.\n\nSecond paragraph."})]
    (record,) = placed_bids(bidding.settings.output_dir, bidding.demo_id)
    assert record["bid_id"] == 555 and record["proposal_version"] == 1
    assert record["description"] == "Edited pitch.\n\nSecond paragraph."

    demo = client.get(f"/demos/{bidding.demo_id}").get_data(as_text=True)
    assert "Bid placed on Freelancer.com: ₹5,200 INR, 3 days." in demo
    assert "Bid placed</h3>" in demo and "50% upfront" in demo and "from proposal v1" in demo
    assert "Review &amp; place bid" not in demo
    assert "bid placed · ₹5,200 INR</a>" in client.get("/").get_data(as_text=True)

    # Freelancer.com takes one bid per project, so a second one is never sent.
    again = client.post(url, data=form, follow_redirects=True).get_data(as_text=True)
    assert "already been placed" in again and len(bidding.sent) == 1
    assert 'type="submit" disabled>Place bid' in client.get(url).get_data(as_text=True)


def test_a_refused_or_invalid_bid_shows_why_and_keeps_the_edits(bidding, monkeypatch):
    def refuse(token, project_id, **values):
        raise BidError("Placing the bid failed: Your bid is below the minimum.")

    monkeypatch.setattr(web_module, "place_bid", refuse)
    client, url = bidding.client, bidding.url
    form = {"csrf": csrf(client), "amount": "100", "period": "3", "milestone_percentage": "100",
            "description": "My <edited> pitch."}
    page = client.post(url, data=form).get_data(as_text=True)
    assert "Placing the bid failed: Your bid is below the minimum." in page
    assert 'name="amount" value="100"' in page and "required>My &lt;edited&gt; pitch.</textarea>" in page
    assert placed_bids(bidding.settings.output_dir, bidding.demo_id) == []

    for change, problem in [({"amount": "lots"}, "as numbers"), ({"amount": "nan"}, "more than zero"),
                            ({"period": "0"}, "at least one day"), ({"milestone_percentage": "150"}, "0 to 100"),
                            ({"description": "  "}, "The proposal is empty.")]:
        assert problem in client.post(url, data={**form, **change}).get_data(as_text=True)


def test_review_page_reports_when_freelancer_cannot_be_reached(bidding, monkeypatch):
    def down(token, project_id):
        raise BidError("Looking up the project failed: could not reach Freelancer.com (timed out).")

    monkeypatch.setattr(web_module, "lookup", down)
    page = bidding.client.get(bidding.url, follow_redirects=True).get_data(as_text=True)
    assert "could not reach Freelancer.com (timed out)" in page and 'id="proposals"' in page


def test_bidding_needs_a_token_a_proposal_and_a_freelancer_gig(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    demo_id = make_demo_id(SHORTLIST[0].gig)
    save_proposal(settings.output_dir, demo_id, "A pitch.\n", {})
    assert "Set <code>FREELANCER_OAUTH_TOKEN</code>" in client.get(f"/demos/{demo_id}").get_data(as_text=True)
    page = client.get(f"/demos/{demo_id}/bid", follow_redirects=True).get_data(as_text=True)
    assert "Set FREELANCER_OAUTH_TOKEN in .env" in page

    with_token = create_app(replace(settings, freelancer_oauth_token="fl-token")).test_client()
    no_proposal = make_demo_id(SHORTLIST[1].gig)
    page = with_token.get(f"/demos/{no_proposal}/bid", follow_redirects=True).get_data(as_text=True)
    assert "Write a proposal first" in page
    assert with_token.get("/demos/no-such-gig/bid").status_code == 404

    feed = replace(SHORTLIST[0], gig=replace(SHORTLIST[0].gig, platform="example.org", gig_id="3f9a1c"))
    save_shortlist([feed], settings.output_dir)
    assert with_token.get(f"/demos/{make_demo_id(feed.gig)}/bid").status_code == 404
    assert "Bid on Freelancer.com" not in with_token.get(f"/demos/{make_demo_id(feed.gig)}").get_data(as_text=True)
