import re
import time
from dataclasses import replace

import pytest

import fdm.web.jobs as jobs_module
from fdm.config import Settings
from fdm.models import GeneratedProject, Gig, ScoredGig
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
    return replace(base, output_dir=tmp_path, ollama_api_key="test-key", vercel_token="")


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
    assert 'name="deploy" disabled' in html  # no VERCEL_TOKEN configured


def test_posts_require_csrf_token(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    assert client.post("/jobs", data={"gig": gig_key(SHORTLIST[0].gig)}).status_code == 400
    assert client.post("/refresh", data={"csrf": "forged"}).status_code == 400


def test_empty_selection_is_rejected(client, settings):
    save_shortlist(SHORTLIST, settings.output_dir)
    response = client.post("/jobs", data={"csrf": csrf(client)}, follow_redirects=True)
    assert "Select at least one gig." in response.get_data(as_text=True)


def test_create_job_generates_selected_gigs(settings, monkeypatch):
    built: list[str] = []

    async def fake_generate(llm, scored_gig, settings_, *, smoke):
        built.append(scored_gig.gig.gig_id)
        path = settings_.output_dir / f"demo-{scored_gig.gig.gig_id}"
        return GeneratedProject(path.name, path, scored_gig, "fake-model", False, ["validated first try"])

    monkeypatch.setattr(jobs_module, "generate_project", fake_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()

    response = client.post("/jobs", data={"csrf": csrf(client), "gig": [gig_key(SHORTLIST[1].gig)],
                                          "smoke": "on", "deploy": "on"})
    assert response.status_code == 302
    job_id = response.headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done" and not job.deploy  # deploy ignored without a token
    assert built == ["202"]
    assert job.items[0].state == "generated" and job.items[0].model == "fake-model"

    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "BMI calculator" in page and "fake-model" in page
    fragment = client.get(f"/jobs/{job_id}/items").get_data(as_text=True)
    assert 'data-finished="true"' in fragment and "1 of 1 finished" in fragment
    assert client.get("/jobs/nope").status_code == 404


def test_failing_gig_does_not_stop_the_others(settings, monkeypatch):
    async def flaky_generate(llm, scored_gig, settings_, *, smoke):
        if scored_gig.gig.gig_id == "101":
            raise RuntimeError("disk full")
        path = settings_.output_dir / "ok"
        return GeneratedProject("ok", path, scored_gig, "fake-model", True, [])

    monkeypatch.setattr(jobs_module, "generate_project", flaky_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    keys = [gig_key(s.gig) for s in SHORTLIST]
    job_id = client.post("/jobs", data={"csrf": csrf(client), "gig": keys}).headers["Location"].rsplit("/", 1)[-1]

    job = wait_for_job(app, job_id)
    assert job.state == "done"
    states = {item.title: (item.state, item.error) for item in job.items}
    assert states["Bakery landing page"] == ("failed", "disk full")
    assert states["BMI calculator"][0] == "generated"
    page = client.get(f"/jobs/{job_id}").get_data(as_text=True)
    assert "Fallback app" in page and "1 failed" in page


def test_job_is_failed_when_every_gig_fails(settings, monkeypatch):
    async def broken_generate(llm, scored_gig, settings_, *, smoke):
        raise RuntimeError("Ollama Cloud rejected the API key")

    monkeypatch.setattr(jobs_module, "generate_project", broken_generate)
    save_shortlist(SHORTLIST, settings.output_dir)
    app = create_app(settings)
    client = app.test_client()
    data = {"csrf": csrf(client), "gig": [gig_key(SHORTLIST[0].gig)]}
    job_id = client.post("/jobs", data=data).headers["Location"].rsplit("/", 1)[-1]
    assert wait_for_job(app, job_id).state == "failed"
