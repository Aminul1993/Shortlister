import asyncio
import json
from dataclasses import replace

import httpx

from fdm.config import Settings
from fdm.generator import generate_demo, generate_proposal, generate_screenshot
from fdm.llm import OllamaCloud
from fdm.models import Gig, ScoredGig
from fdm.screenshot import BLANK, Shot
from tests.test_validate import GOOD_HTML

BLUEPRINT = {
    "app_title": "Bakery Adder", "tagline": "Adds numbers", "demo_summary": "Shows a calculator.",
    "ui_sections": ["Hero", "Calculator"], "sample_data": "2 + 3 = 5",
}
PITCH = {
    "message": "You want customers to add up a bakery order without a phone call.\n\n"
               "The attached screenshot shows Bakery Adder totalling 2 + 3 = 5, built with sample data.\n\n"
               "Which items should the live calculator list first?",
    "bid_usd": 80, "delivery_days": 2,
}
GOOD_REPLY = f"```html\n{GOOD_HTML}\n```"
TEMPLATED_REPLY = GOOD_REPLY.replace("<strong>5</strong>", "<strong>{{ total }}</strong>")
SCRIPTED_REPLY = GOOD_REPLY.replace("</body>", "<script>boom()</script></body>")
PNG = b"\x89PNG fake"
SCORED = ScoredGig(
    gig=Gig(platform="Freelancer.com", gig_id="123", title="Bakery calculator", description="Add two numbers.",
            url="https://example.com/123"),
    score=9.0, breakdown={}, signals=[],
)


class FakeRenderer:
    """Stands in for headless Chromium: pages with a <script> 'throw', everything else renders cleanly."""

    def __init__(self):
        self.pages: list[str] = []

    async def render(self, html: str) -> Shot:
        self.pages.append(html)
        problems = ["inline JavaScript threw an error: boom is not defined"] if "<script>boom" in html else []
        return Shot(PNG, problems, warnings=["Could not load the CDN"])


def _settings(tmp_path) -> Settings:
    return replace(Settings.load(env_file=tmp_path / "missing.env"), output_dir=tmp_path, max_repairs=1)


def _generate(tmp_path, developer_replies: list[str], renderer=None, pitch_reply: str = json.dumps(PITCH),
              prompts: dict[str, str] | None = None, step=generate_demo, **settings_changes):
    seen: list[str] = []
    renderer = renderer or FakeRenderer()

    def handler(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if "Agent A" in messages[0]["content"]:
            content = json.dumps(BLUEPRINT)
            seen.append("architect")
        elif "Agent C" in messages[0]["content"]:
            content = pitch_reply
            seen.append("pitch")
            if prompts is not None:
                prompts["pitch_system"] = messages[0]["content"]
                prompts["pitch"] = messages[1]["content"]
        else:
            content = developer_replies[min(len(seen) - 1, len(developer_replies) - 1)]
            seen.append("repair" if len(messages) > 2 else "developer")
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    async def go():
        async with OllamaCloud("key", ["m1"], transport=httpx.MockTransport(handler), backoff=0) as llm:
            settings = replace(_settings(tmp_path), **settings_changes)
            if step is generate_proposal:
                return await step(llm, SCORED, settings)
            return await step(llm, renderer, SCORED, settings)

    return asyncio.run(go()), seen, renderer


def _saved_files(tmp_path) -> set[str]:
    return {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}


SAVED = {"screenshots/bakery-calculator-123.png", "screenshots/bakery-calculator-123.json",
         "proposals/bakery-calculator-123.txt", "proposals/bakery-calculator-123.json"}


def test_repair_loop_fixes_rejected_page_and_saves_screenshot_and_proposal(tmp_path):
    prompts: dict[str, str] = {}
    shot, seen, renderer = _generate(tmp_path, [TEMPLATED_REPLY, GOOD_REPLY], prompts=prompts)
    assert seen == ["architect", "developer", "repair", "pitch"]
    assert not shot.used_fallback and shot.model == "m1"
    # The templated draft fails the static checks, so only the repaired page reaches the browser.
    assert len(renderer.pages) == 1 and "tailwindcss" in renderer.pages[0]
    assert _saved_files(tmp_path) == SAVED
    assert shot.path.read_bytes() == PNG
    assert shot.notes[0].startswith("Check 1 found 1 problem(s): the page contains template syntax")
    assert shot.notes[-1] == "Could not load the CDN"
    # The proposal is written for the screenshot that was kept, and the file is exactly what gets sent.
    assert shot.proposal_path.read_text(encoding="utf-8") == PITCH["message"] + "\n"
    assert shot.pitch.delivery_days == 2 and shot.pitch.bid_usd is None  # the gig states no budget
    assert "high-fidelity mockup" in prompts["pitch"] and '"app_title": "Bakery Adder"' in prompts["pitch"]
    assert "No profile given" in prompts["pitch"] and "Client budget: not given" in prompts["pitch"]


def test_render_problems_are_sent_back_for_repair(tmp_path):
    shot, seen, renderer = _generate(tmp_path, [SCRIPTED_REPLY, GOOD_REPLY])
    assert seen == ["architect", "developer", "repair", "pitch"]
    assert len(renderer.pages) == 2 and not shot.used_fallback
    assert "boom is not defined" in shot.notes[0]


def test_last_usable_draft_is_kept_when_repairs_run_out(tmp_path):
    shot, seen, renderer = _generate(tmp_path, [SCRIPTED_REPLY])
    assert seen == ["architect", "developer", "repair", "pitch"]
    assert not shot.used_fallback and len(renderer.pages) == 2
    assert "Kept the screenshot of the last draft" in shot.notes[-2]


def test_unfixable_page_renders_the_fallback(tmp_path):
    prompts: dict[str, str] = {}
    shot, seen, renderer = _generate(tmp_path, [TEMPLATED_REPLY], prompts=prompts)
    assert seen == ["architect", "developer", "repair", "pitch"]
    assert shot.used_fallback
    assert len(renderer.pages) == 1 and "Bakery Adder" in renderer.pages[0]
    assert _saved_files(tmp_path) == SAVED
    assert len([n for n in shot.notes if n.startswith("Check")]) == 2
    # The pitch must not claim the outline page shows a working feature.
    assert "only an outline of the planned scope" in prompts["pitch"]


def test_freelancer_profile_reaches_the_pitch_writer(tmp_path):
    prompts: dict[str, str] = {}
    _generate(tmp_path, [GOOD_REPLY], prompts=prompts, freelancer_name="Aminul",
              freelancer_profile="Python and Flask developer; built 3 booking sites.")
    assert "Name: Aminul\nProfile: Python and Flask developer; built 3 booking sites." in prompts["pitch"]
    assert "No profile given" not in prompts["pitch"]


def test_failed_pitch_keeps_the_screenshot_and_the_older_proposal(tmp_path):
    older = tmp_path / "proposals" / "bakery-calculator-123.txt"
    older.parent.mkdir()
    older.write_text("A proposal for last week's screenshot", encoding="utf-8")
    shot, seen, _ = _generate(tmp_path, [GOOD_REPLY], pitch_reply=json.dumps({**PITCH, "message": "Hi [Your Name]"}))
    assert seen == ["architect", "developer", "pitch", "pitch"]  # one retry, then give up
    assert shot.pitch is None and shot.proposal_path is None and shot.proposal_version is None
    assert _saved_files(tmp_path) == {"screenshots/bakery-calculator-123.png", "screenshots/bakery-calculator-123.json",
                                      "proposals/bakery-calculator-123.txt"}
    assert older.read_text(encoding="utf-8") == "A proposal for last week's screenshot"
    assert shot.notes[-1].startswith("Pitch Writer failed, so no proposal was saved")


def test_screenshot_only_adds_a_version_and_keeps_the_old_files(tmp_path):
    _generate(tmp_path, [GOOD_REPLY])
    first = (tmp_path / "screenshots" / "bakery-calculator-123.png").stat().st_mtime_ns
    shot, seen, _ = _generate(tmp_path, [GOOD_REPLY], step=generate_screenshot)
    assert seen == ["architect", "developer"]  # no proposal
    assert shot.version == 2 and shot.path.name == "bakery-calculator-123-v2.png" and shot.pitch is None
    assert _saved_files(tmp_path) == SAVED | {"screenshots/bakery-calculator-123-v2.png",
                                              "screenshots/bakery-calculator-123-v2.json"}
    assert (tmp_path / "screenshots" / "bakery-calculator-123.png").stat().st_mtime_ns == first
    meta = json.loads((tmp_path / "screenshots" / "bakery-calculator-123-v2.json").read_text(encoding="utf-8"))
    assert meta["blueprint"] == BLUEPRINT and meta["model"] == "m1" and meta["used_fallback"] is False


def test_proposal_only_describes_the_latest_screenshot_and_keeps_older_proposals(tmp_path):
    _generate(tmp_path, [GOOD_REPLY])
    _generate(tmp_path, [TEMPLATED_REPLY], step=generate_screenshot)  # v2 is the fallback outline page
    prompts: dict[str, str] = {}
    shot, seen, renderer = _generate(tmp_path, [], step=generate_proposal, prompts=prompts,
                                     pitch_reply=json.dumps({**PITCH, "bid_usd": 0, "delivery_days": 3}))
    assert seen == ["pitch"] and renderer.pages == []
    assert shot.version == 2 and shot.used_fallback and shot.path.name == "bakery-calculator-123-v2.png"
    assert shot.proposal_version == 2 and shot.proposal_path.name == "bakery-calculator-123-v2.txt"
    assert shot.pitch.delivery_days == 3
    # The brief saved with the screenshot reaches the Pitch Writer, so it describes that screenshot.
    assert '"app_title": "Bakery Adder"' in prompts["pitch"] and "only an outline" in prompts["pitch"]
    meta = json.loads(shot.proposal_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["screenshot_version"] == 2 and meta["delivery_days"] == 3
    assert (tmp_path / "proposals" / "bakery-calculator-123.txt").read_text(encoding="utf-8") == PITCH["message"] + "\n"


def test_proposal_only_without_a_screenshot_mentions_none(tmp_path):
    prompts: dict[str, str] = {}
    shot, seen, renderer = _generate(tmp_path, [], step=generate_proposal, prompts=prompts)
    assert seen == ["pitch"] and renderer.pages == []
    assert shot.path is None and shot.version is None and shot.proposal_version == 1
    assert shot.notes == ["This gig has no screenshot, so this proposal doesn't mention one."]
    assert "Nothing is attached" in prompts["pitch_system"] and "point to the attached screenshot" not in prompts["pitch_system"]
    assert "<demo>" not in prompts["pitch"] and "Client budget: not given" in prompts["pitch"]
    assert _saved_files(tmp_path) == {"proposals/bakery-calculator-123.txt", "proposals/bakery-calculator-123.json"}
    meta = json.loads(shot.proposal_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["screenshot_version"] is None

    # A screenshot from before versioning has no brief to describe it by, so it is not mentioned either.
    (tmp_path / "screenshots").mkdir()
    (tmp_path / "screenshots" / "bakery-calculator-123.png").write_bytes(PNG)
    shot, _, _ = _generate(tmp_path, [], step=generate_proposal, prompts=prompts)
    assert shot.path is None and shot.proposal_version == 2 and "Nothing is attached" in prompts["pitch_system"]
    assert shot.notes == ["Screenshot v1 was saved before design briefs were kept, so this proposal doesn't mention it."]


def test_proposal_with_a_screenshot_points_to_it(tmp_path):
    prompts: dict[str, str] = {}
    _generate(tmp_path, [GOOD_REPLY], prompts=prompts)
    assert "point to the attached screenshot" in prompts["pitch_system"] and "Nothing is attached" not in prompts["pitch_system"]


def test_blank_render_is_not_kept(tmp_path):
    class BlankRenderer(FakeRenderer):
        async def render(self, html: str) -> Shot:
            self.pages.append(html)
            return Shot(PNG, [] if "Bakery Adder" in html else [f"{BLANK}; put the content in the HTML"])

    blank_page = GOOD_REPLY.replace("<h1>Bakery Adder</h1><p>2 + 3 = <strong>5</strong></p>", "<div id=app></div>")
    shot, _, renderer = _generate(tmp_path, [blank_page], BlankRenderer())
    assert shot.used_fallback and len(renderer.pages) == 3
