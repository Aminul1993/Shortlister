import re
import time
from dataclasses import replace

import pytest

import fdm.web.jobs as jobs_module
from fdm.config import Settings
from fdm.models import DemoScreenshot, Gig, ScoredGig
from fdm.generator import screenshot_path
from fdm.pages import make_demo_id
from fdm.screenshot import BrowserError
from fdm.store import gig_key, load_shortlist, save_shortlist
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
        path = screenshot_path(settings_.output_dir, make_demo_id(scored_gig.gig))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake")
        return DemoScreenshot(path.stem, path, scored_gig, "fake-model", False, ["passed first try"])

    monkeypatch.setattr(jobs_module, "generate_screenshot", fake_generate)
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
    assert item.state == "captured" and item.model == "fake-model" and item.screenshot == "bmi-calculator-202.png"

    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "BMI calculator" in page and "fake-model" in page
    assert f'src="/screenshots/bmi-calculator-202.png?v={job_id}"' in page
    image = client.get("/screenshots/bmi-calculator-202.png")
    assert image.status_code == 200 and image.mimetype == "image/png" and image.data == b"\x89PNG fake"
    image.close()
    assert client.get("/screenshots/..%2Ftop10.json").status_code == 404
    assert "screenshot saved" in client.get("/").get_data(as_text=True)
    fragment = client.get(f"/jobs/{job_id}/items").get_data(as_text=True)
    assert 'data-finished="true"' in fragment and "1 of 1 finished" in fragment
    assert client.get("/jobs/nope").status_code == 404


def test_failing_gig_does_not_stop_the_others(settings, monkeypatch):
    async def flaky_generate(llm, renderer, scored_gig, settings_):
        if scored_gig.gig.gig_id == "101":
            raise RuntimeError("disk full")
        path = settings_.output_dir / "screenshots" / "ok.png"
        return DemoScreenshot("ok", path, scored_gig, "fake-model", True, [])

    monkeypatch.setattr(jobs_module, "generate_screenshot", flaky_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    keys = [gig_key(s.gig) for s in SHORTLIST]
    job_id = client.post("/jobs", data={"csrf": csrf(client), "gig": keys}).headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done"
    states = {item.title: (item.state, item.error) for item in job.items}
    assert states["Bakery landing page"] == ("failed", "disk full")
    assert states["BMI calculator"][0] == "captured"
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Fallback page" in page and "1 failed" in page


def test_job_is_failed_when_every_gig_fails(settings, monkeypatch):
    async def broken_generate(llm, renderer, scored_gig, settings_):
        raise RuntimeError("Ollama Cloud rejected the API key")

    monkeypatch.setattr(jobs_module, "generate_screenshot", broken_generate)
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
