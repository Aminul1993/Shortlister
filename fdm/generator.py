"""Phase 2: Architect -> Developer -> check/render/repair -> Pitch Writer for every shortlisted gig.

The page HTML only ever exists in memory and in a temporary file while it is rendered. Each gig
ends up as a PNG in output/screenshots/ and the proposal to send with it in output/proposals/.
Either one can be regenerated on its own, and every run saves a new version beside the old ones
(see fdm.versions).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, fields, replace
from pathlib import Path

from .agents import default_blueprint, run_architect, run_developer, run_pitch_writer, run_repair
from .config import Settings
from .llm import LLMAuthError, LLMError, OllamaCloud
from .models import Blueprint, DemoScreenshot, ScoredGig
from .pages import ensure_tailwind, fallback_page, make_demo_id
from .screenshot import BrowserError, Renderer, Shot
from .validate import page_problems
from .versions import save_proposal, save_screenshot, screenshots

log = logging.getLogger(__name__)

_BLUEPRINT_FIELDS = {f.name for f in fields(Blueprint)}


def _attempt_note(attempt: int, problems: list[str]) -> str:
    headlines = "; ".join(problem.splitlines()[0] for problem in problems)
    if len(headlines) > 400:
        headlines = headlines[:399] + "…"
    return f"Check {attempt} found {len(problems)} problem(s): {headlines}"


async def generate_screenshot(llm: OllamaCloud, renderer: Renderer, scored: ScoredGig,
                              settings: Settings) -> DemoScreenshot:
    """Design, write and render a demo page, and save its screenshot as the gig's next version."""
    gig = scored.gig
    demo_id = make_demo_id(gig)
    notes: list[str] = []
    model: str | None = None

    log.info("[%s] Architect is designing the demo", demo_id)
    try:
        blueprint, model = await run_architect(llm, gig)
    except LLMAuthError:
        raise
    except LLMError as exc:
        notes.append(f"Architect failed, using the default blueprint: {exc}")
        blueprint = default_blueprint(gig)

    accepted: Shot | None = None
    usable: Shot | None = None  # latest draft that rendered real content, kept if repairs run out
    try:
        log.info("[%s] Developer is writing the page", demo_id)
        html, model = await run_developer(llm, gig, blueprint)
        for attempt in range(settings.max_repairs + 1):
            html = ensure_tailwind(html)
            problems = page_problems(html)
            if not problems:
                shot = await renderer.render(html)
                problems = shot.problems
                if not problems:
                    accepted = shot
                    break
                if shot.usable:
                    usable = shot
            notes.append(_attempt_note(attempt + 1, problems))
            log.warning("[%s] %s", demo_id, notes[-1])
            if attempt < settings.max_repairs:
                html, model = await run_repair(llm, gig, blueprint, html, problems)
    except LLMAuthError:
        raise
    except LLMError as exc:
        notes.append(f"Developer failed: {exc}")

    used_fallback = False
    if accepted is None and usable is not None:
        accepted = usable
        notes.append("Kept the screenshot of the last draft that rendered content, despite the problems above.")
    if accepted is None:
        log.warning("[%s] Rendering the fallback page", demo_id)
        used_fallback = True
        accepted = await renderer.render(fallback_page(gig, blueprint))
        if accepted.png is None:
            raise BrowserError(f"could not render even the fallback page: {'; '.join(accepted.problems)}")
    notes += accepted.warnings

    # The brief is kept so a proposal can be written for this exact screenshot later on.
    saved = save_screenshot(settings.output_dir, demo_id, accepted.png, {
        "model": model, "used_fallback": used_fallback, "blueprint": asdict(blueprint), "notes": notes,
    })
    log.info("[%s] Saved %s", demo_id, saved.path)
    return DemoScreenshot(demo_id, saved.path, scored, model, used_fallback, list(notes),
                          blueprint=blueprint, version=saved.number)


def latest_screenshot(scored: ScoredGig, output_dir: Path) -> DemoScreenshot | None:
    """The gig's newest saved screenshot, with the brief it was built from if one was kept."""
    demo_id = make_demo_id(scored.gig)
    saved = screenshots(output_dir, demo_id)
    if not saved:
        return None
    last = saved[-1]
    brief = last.meta.get("blueprint")
    try:
        blueprint = Blueprint(**{k: v for k, v in brief.items() if k in _BLUEPRINT_FIELDS})
    except (AttributeError, TypeError):  # no brief, or one that was edited by hand
        blueprint = None
    return DemoScreenshot(demo_id, last.path, scored, last.meta.get("model"), bool(last.meta.get("used_fallback")),
                          blueprint=blueprint, version=last.number)


async def write_proposal(llm: OllamaCloud, shot: DemoScreenshot, settings: Settings) -> DemoScreenshot:
    """Have the Pitch Writer describe `shot`, and save the proposal as the gig's next version.

    Without `shot.blueprint` there is no screenshot it can describe, so the proposal mentions none.
    Raises LLMError if no usable proposal was written; nothing is saved then.
    """
    gig = shot.scored.gig
    log.info("[%s] Pitch Writer is drafting the proposal", shot.demo_id)
    pitch, model = await run_pitch_writer(llm, gig, shot.blueprint, working_mockup=not shot.used_fallback,
                                          freelancer_name=settings.freelancer_name,
                                          freelancer_profile=settings.freelancer_profile)
    saved = save_proposal(settings.output_dir, shot.demo_id, pitch.message + "\n", {
        "model": model, "screenshot_version": shot.version if shot.blueprint else None, "bid_usd": pitch.bid_usd,
        "pay_type": gig.pay_type, "delivery_days": pitch.delivery_days,
    })
    log.info("[%s] Saved %s", shot.demo_id, saved.path)
    return replace(shot, pitch=pitch, proposal_path=saved.path, proposal_version=saved.number)


async def generate_proposal(llm: OllamaCloud, scored: ScoredGig, settings: Settings) -> DemoScreenshot:
    """Write a new proposal for the gig's latest screenshot, keeping the screenshot and older proposals.

    If there is no screenshot whose brief was saved, the proposal is written without one and mentions none.
    """
    shot = latest_screenshot(scored, settings.output_dir)
    if shot is None or shot.blueprint is None:
        note = (f"Screenshot v{shot.version} was saved before design briefs were kept, so this proposal "
                "doesn't mention it." if shot else "This gig has no screenshot, so this proposal doesn't mention one.")
        shot = DemoScreenshot(make_demo_id(scored.gig), None, scored, None, False, [note], version=None)
    return await write_proposal(llm, shot, settings)


async def generate_demo(llm: OllamaCloud, renderer: Renderer, scored: ScoredGig,
                        settings: Settings) -> DemoScreenshot:
    """A new screenshot, then a proposal written for it. If the Pitch Writer fails, the screenshot is still kept."""
    shot = await generate_screenshot(llm, renderer, scored, settings)
    # Written last so it describes the screenshot that was actually kept, outline page included.
    try:
        return await write_proposal(llm, shot, settings)
    except LLMAuthError:
        raise
    except LLMError as exc:
        shot.notes.append(f"Pitch Writer failed, so no proposal was saved: {exc}")
        return shot
