import re
import time
from dataclasses import replace

import pytest

import fdm.web.jobs as jobs_module
from fdm.config import Settings
from fdm.models import DemoScreenshot, Gig, Pitch, ScoredGig
from fdm.pages import make_demo_id
from fdm.screenshot import BrowserError
from fdm.store import gig_key, load_shortlist, save_shortlist
from fdm.versions import save_proposal, save_screenshot
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
    assert "Create screenshots" in html and "screenshot saved" not in html


def test_posts_require_csrf_token(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    assert client.post("/jobs", data={"gig": gig_key(SHORTLIST[0].gig)}).status_code == 400
    assert client.post("/refresh", data={"csrf": "forged"}).status_code == 400


def test_empty_selection_is_rejected(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    response = client.post("/jobs", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Select at least one gig." in response.get_data(as_text=True)


class NoBrowser:
    """Replaces the headless-Chromium renderer; the fake generators below never render anything."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    monkeypatch.setattr(jobs_module, "Renderer", NoBrowser)


def test_create_job_generates_selected_gigs(settings, monkeypatch):
    built: list[str] = []

    async def fake_generate(llm, renderer, scored_gig, settings_):
        built.append(scored_gig.gig.gig_id)
        demo_id = make_demo_id(scored_gig.gig)
        shot = save_screenshot(settings_.output_dir, demo_id, b"\x89PNG fake", {"model": "fake-model"})
        pitch = Pitch("You want a BMI calculator <that> works.\n\nThe attached screenshot shows it.", 95, 2)
        proposal = save_proposal(settings_.output_dir, demo_id, pitch.message + "\n", {"screenshot_version": 1})
        return DemoScreenshot(demo_id, shot.path, scored_gig, "fake-model", False, ["passed first try"], pitch,
                              proposal.path, version=shot.number, proposal_version=proposal.number)

    monkeypatch.setattr(jobs_module, "generate_demo", fake_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()

    response = client.post("/jobs", data={"csrf": csrf(client), "gig": [gig_key(SHORTLIST[1].gig)]})
    assert response.status_code == 302
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done"
    assert built == ["202"]
    item = job.items[0]
    assert item.state == "done" and item.model == "fake-model" and item.screenshot == "bmi-calculator-202.png"

    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "BMI calculator" in page and "fake-model" in page
    assert f'src="/screenshots/bmi-calculator-202.png?v={job_id}"' in page
    image = client.get("/screenshots/bmi-calculator-202.png")
    assert image.status_code == 200 and image.mimetype == "image/png" and image.data == b"\x89PNG fake"
    image.close()
    assert client.get("/screenshots/..%2Ftop10.json").status_code == 404
    assert "screenshot v1" in client.get("/").get_data(as_text=True)

    # The proposal is shown escaped with its line breaks, ready to copy, beside the suggested bid.
    assert item.proposal.startswith("You want a BMI calculator") and item.bid == "~$95" and item.delivery_days == 2
    assert ('id="proposal-1">You want a BMI calculator &lt;that&gt; works.\n\nThe attached screenshot shows it.</p>'
            in page)
    assert 'data-copy="proposal-1"' in page and "suggested bid ~$95" in page and "2 days" in page
    text = client.get("/proposals/bmi-calculator-202.txt")
    assert text.mimetype == "text/plain" and text.get_data(as_text=True).startswith("You want a BMI calculator")
    text.close()
    assert client.get("/proposals/..%2Ftop10.json").status_code == 404
    assert "proposal v1" in client.get("/").get_data(as_text=True)
    fragment = client.get(f"/jobs/{job_id}/items").get_data(as_text=True)
    assert 'data-finished="true"' in fragment and "1 of 1 finished" in fragment
    assert client.get("/jobs/nope").status_code == 404


def test_failing_gig_does_not_stop_the_others(settings, monkeypatch):
    async def flaky_generate(llm, renderer, scored_gig, settings_):
        if scored_gig.gig.gig_id == "101":
            raise RuntimeError("disk full")
        path = settings_.output_dir / "screenshots" / "ok.png"
        return DemoScreenshot("ok", path, scored_gig, "fake-model", True, [])

    monkeypatch.setattr(jobs_module, "generate_demo", flaky_generate)
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
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Fallback page" in page and "1 failed" in page


def test_job_is_failed_when_every_gig_fails(settings, monkeypatch):
    async def broken_generate(llm, renderer, scored_gig, settings_):
        raise RuntimeError("Ollama Cloud rejected the API key")

    monkeypatch.setattr(jobs_module, "generate_demo", broken_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    data = {"csrf": csrf(client), "gig": [gig_key(SHORTLIST[0].gig)]}
    job_id = client.post("/jobs", data=data).headers["Location"].rsplit("/", 1)[-1]
    assert wait_for_job(app, job_id).state == "failed"


def test_job_fails_clearly_without_a_browser(settings, monkeypatch):
    class MissingBrowser(NoBrowser):
        async def __aenter__(self):
            raise BrowserError("could not start headless Chromium. Install it with: python -m playwright install chromium")

    monkeypatch.setattr(jobs_module, "Renderer", MissingBrowser)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    data = {"csrf": csrf(client), "gig": [gig_key(SHORTLIST[0].gig)]}
    job_id = client.post("/jobs", data=data).headers["Location"].rsplit("/", 1)[-1]
    job = wait_for_job(app, job_id)
    assert job.state == "failed" and "playwright install chromium" in job.error
    assert job.items[0].state == "failed"


def test_each_mode_runs_its_own_step_and_proposals_need_no_browser(settings, monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake(step):
        async def run(*args):
            scored_gig, settings_ = args[-2], args[-1]
            calls.append((step, scored_gig.gig.gig_id))
            demo_id = make_demo_id(scored_gig.gig)
            shot = save_screenshot(settings_.output_dir, demo_id, b"png", {})
            return DemoScreenshot(demo_id, shot.path, scored_gig, None, False, version=shot.number)
        return run

    class MissingBrowser(NoBrowser):
        async def __aenter__(self):
            raise BrowserError("no Chromium")

    for step in ("generate_demo", "generate_screenshot", "generate_proposal"):
        monkeypatch.setattr(jobs_module, step, fake(step))
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    key = gig_key(SHORTLIST[0].gig)

    def run_job(mode: str):
        response = client.post("/jobs", data={"csrf": csrf(client), "gig": [key, key], "mode": mode})
        return wait_for_job(app, response.headers["Location"].rsplit("/", 1)[-1])

    shot_job = run_job("screenshot")
    assert calls == [("generate_screenshot", "101")]  # the duplicate key is dropped
    assert shot_job.mode_label == "Screenshots only" and shot_job.items[0].screenshot_version == 1

    monkeypatch.setattr(jobs_module, "Renderer", MissingBrowser)
    proposal_job = run_job("proposal")
    assert proposal_job.state == "done" and calls[-1] == ("generate_proposal", "101")
    assert run_job("both").state == "failed" and len(calls) == 2  # needs the browser, so nothing ran
    assert "proposals only" in client.get("/").get_data(as_text=True)
    assert client.post("/jobs", data={"csrf": csrf(client), "gig": key, "mode": "everything"}).status_code == 400


def test_demo_page_lists_every_version_newest_first(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    out, demo_id = settings.output_dir, make_demo_id(SHORTLIST[0].gig)
    save_screenshot(out, demo_id, b"png1", {"blueprint": {"app_title": "Bakery"}, "model": "big-model",
                                            "used_fallback": False, "notes": ["Check 1 found 1 problem(s)"]})
    save_proposal(out, demo_id, "First pitch for the bakery.\n",
                  {"screenshot_version": 1, "bid_usd": 90, "pay_type": "fixed", "delivery_days": 3})
    save_proposal(out, demo_id, "Second pitch for the bakery.\n", {"screenshot_version": 1})
    save_screenshot(out, demo_id, b"png2", {"blueprint": {"app_title": "Bakery"}, "used_fallback": True})

    index = client.get("/").get_data(as_text=True)
    assert f'href="/demos/{demo_id}#screenshots">screenshot v2</a>' in index and "proposal v2</a>" in index

    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert "Bakery landing page" in page and "2 screenshots, 2 proposals" in page
    assert page.index("Proposal v2") < page.index("Proposal v1") < page.index("Screenshot v2") < page.index("Screenshot v1")
    assert f'src="/screenshots/{demo_id}-v2.png?v=' in page and f'src="/screenshots/{demo_id}.png?v=' in page
    assert "suggested bid ~$90" in page and "3 days" in page and "built by big-model" in page
    assert "fallback page" in page and "Build notes (1)" in page
    # The latest proposal describes screenshot v1, but v2 is what would be attached now.
    assert "Written for screenshot v1" in page and "not the latest one (v2)" in page
    assert f'name="gig" value="{gig_key(SHORTLIST[0].gig)}"' in page
    assert re.search(r'value="proposal"\s+>New proposal', page)  # enabled: v2 has a saved brief

    assert client.get("/demos/no-such-gig").status_code == 404
    assert client.get("/demos/..%2Ftop10").status_code == 404


def test_demo_page_without_a_brief_or_shortlist_entry(client, settings):
    out = settings.output_dir
    (out / "screenshots").mkdir()
    (out / "screenshots" / "old-gig-9.png").write_bytes(b"png")  # saved before versioning
    (out / "proposals").mkdir()
    (out / "proposals" / "old-gig-9.txt").write_text("An old pitch.", encoding="utf-8")
    page = client.get("/demos/old-gig-9").get_data(as_text=True)
    assert "No longer on the shortlist" in page and 'name="mode"' not in page
    assert "for screenshot v1" in page and "Written for" not in page

    save_shortlist(SHORTLIST, out)
    demo_id = make_demo_id(SHORTLIST[1].gig)
    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert re.search(r'value="proposal"\s+>New proposal', page)  # a proposal can be written without a screenshot
    assert "There is no screenshot yet, so a new proposal won't mention one" in page

    (out / "screenshots" / f"{demo_id}.png").write_bytes(b"png")
    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert re.search(r'value="proposal"\s+>New proposal', page)
    assert "Screenshot v1 was saved before design briefs were kept, so a\n  new proposal won" in page


def test_demo_page_flags_a_proposal_written_without_a_screenshot(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    out, demo_id = settings.output_dir, make_demo_id(SHORTLIST[0].gig)
    save_proposal(out, demo_id, "A pitch with nothing attached.\n", {"screenshot_version": None})
    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert " · no screenshot" in page and "Written without a screenshot" not in page  # nothing better to offer yet

    save_screenshot(out, demo_id, b"png", {"blueprint": {"app_title": "Bakery"}})
    page = client.get(f"/demos/{demo_id}").get_data(as_text=True)
    assert "Written without a screenshot, so it doesn't mention one" in page and "pitch with screenshot v1" in page
    assert "Written for" not in page


def test_proposal_only_job_without_a_screenshot(settings, monkeypatch):
    async def text_only(llm, scored_gig, settings_):
        demo_id = make_demo_id(scored_gig.gig)
        saved = save_proposal(settings_.output_dir, demo_id, "A pitch.\n", {"screenshot_version": None})
        return DemoScreenshot(demo_id, None, scored_gig, None, False, ["no screenshot"], Pitch("A pitch.", None, 2),
                              saved.path, version=None, proposal_version=saved.number)

    monkeypatch.setattr(jobs_module, "generate_proposal", text_only)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    data = {"csrf": csrf(client), "gig": gig_key(SHORTLIST[0].gig), "mode": "proposal"}
    job_id = client.post("/jobs", data=data).headers["Location"].rsplit("/", 1)[-1]
    item = wait_for_job(app, job_id).items[0]
    assert item.state == "done" and item.screenshot is None and item.proposal_version == 1
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "<img" not in page and "None. The proposal doesn't mention one" in page and "Proposal v1" in page
