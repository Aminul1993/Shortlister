import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from fdm.config import Settings
from fdm.generator import generate_proposal
from fdm.llm import LLMError, OllamaCloud
from fdm.models import Gig, ScoredGig

PITCH = {
    "message": "You want customers to add up a bakery order without a phone call.\n\n"
               "They tick the cakes they want and see the total, delivery included, before they order.\n\n"
               "Which items should the calculator list first?",
    "bid_usd": 80, "delivery_days": 2,
}
SCORED = ScoredGig(
    gig=Gig(platform="Freelancer.com", gig_id="123", title="Bakery calculator", description="Add two numbers.",
            url="https://example.com/123"),
    score=9.0, breakdown={}, signals=[],
)


def _generate(tmp_path, pitch_reply: str = json.dumps(PITCH), prompts: dict[str, str] | None = None,
              **settings_changes):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        calls.append(messages[0]["content"])
        if prompts is not None:
            prompts["system"], prompts["user"] = messages[0]["content"], messages[1]["content"]
        return httpx.Response(200, json={"message": {"role": "assistant", "content": pitch_reply}})

    async def go():
        async with OllamaCloud("key", ["m1"], transport=httpx.MockTransport(handler), backoff=0) as llm:
            settings = replace(Settings.load(env_file=tmp_path / "missing.env"), output_dir=tmp_path,
                               **settings_changes)
            return await generate_proposal(llm, SCORED, settings)

    return asyncio.run(go()), calls


def _saved_files(tmp_path) -> set[str]:
    return {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}


def test_proposal_is_saved_with_its_suggested_bid(tmp_path):
    prompts: dict[str, str] = {}
    proposal, calls = _generate(tmp_path, prompts=prompts)
    assert len(calls) == 1 and proposal.model == "m1" and proposal.version == 1
    assert _saved_files(tmp_path) == {"proposals/bakery-calculator-123.txt", "proposals/bakery-calculator-123.json"}
    # The file is exactly what gets sent.
    assert proposal.path.read_text(encoding="utf-8") == PITCH["message"] + "\n"
    assert proposal.pitch.delivery_days == 2 and proposal.pitch.bid_usd == 80  # no budget, so the estimate stands
    meta = json.loads(proposal.path.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta == {"model": "m1", "bid_usd": 80, "pay_type": "unknown", "delivery_days": 2}
    # Nothing goes with a bid, so the Pitch Writer is told never to mention an attachment.
    assert "Nothing is attached" in prompts["system"] and "Never mention a screenshot" in prompts["system"]
    assert "No profile given" in prompts["user"] and "Client budget: not given" in prompts["user"]


def test_freelancer_profile_reaches_the_pitch_writer(tmp_path):
    prompts: dict[str, str] = {}
    _generate(tmp_path, prompts=prompts, freelancer_name="Aminul",
              freelancer_profile="Python and Flask developer; built 3 booking sites.")
    assert "Name: Aminul\nProfile: Python and Flask developer; built 3 booking sites." in prompts["user"]
    assert "No profile given" not in prompts["user"]


def test_new_proposals_add_a_version_and_keep_the_old_ones(tmp_path):
    _generate(tmp_path)
    proposal, _ = _generate(tmp_path, pitch_reply=json.dumps({**PITCH, "delivery_days": 3}))
    assert proposal.version == 2 and proposal.path.name == "bakery-calculator-123-v2.txt"
    assert (tmp_path / "proposals" / "bakery-calculator-123.txt").read_text(encoding="utf-8") == PITCH["message"] + "\n"
    assert proposal.pitch.delivery_days == 3


def test_an_unsendable_proposal_saves_nothing(tmp_path):
    with pytest.raises(LLMError):
        _generate(tmp_path, pitch_reply=json.dumps({**PITCH, "message": "Hi [Your Name]"}))
    assert _saved_files(tmp_path) == set()
