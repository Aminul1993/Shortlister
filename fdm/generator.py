"""Phase 2: Architect -> Developer -> check/render/repair -> Pitch Writer for every shortlisted gig.

The page HTML only ever exists in memory and in a temporary file while it is rendered. Each gig
ends up as a PNG in output/screenshots/ and the proposal to send with it in output/proposals/.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .agents import default_blueprint, run_architect, run_developer, run_pitch_writer, run_repair
from .config import Settings
from .llm import LLMAuthError, LLMError, OllamaCloud
from .models import DemoScreenshot, Pitch, ScoredGig
from .pages import ensure_tailwind, fallback_page, make_demo_id
from .screenshot import BrowserError, Renderer, Shot
from .validate import page_problems

log = logging.getLogger(__name__)

SCREENSHOT_DIR = "screenshots"
PROPOSAL_DIR = "proposals"


def screenshot_path(output_dir: Path, demo_id: str) -> Path:
    return output_dir / SCREENSHOT_DIR / f"{demo_id}.png"


def proposal_path(output_dir: Path, demo_id: str) -> Path:
    return output_dir / PROPOSAL_DIR / f"{demo_id}.txt"


def _attempt_note(attempt: int, problems: list[str]) -> str:
    headlines = "; ".join(problem.splitlines()[0] for problem in problems)
    if len(headlines) > 400:
        headlines = headlines[:399] + "…"
    return f"Check {attempt} found {len(problems)} problem(s): {headlines}"


async def generate_screenshot(llm: OllamaCloud, renderer: Renderer, scored: ScoredGig,
                              settings: Settings) -> DemoScreenshot:
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

    path = screenshot_path(settings.output_dir, demo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(accepted.png)
    log.info("[%s] Saved %s", demo_id, path)

    # Written last so it describes the screenshot that was actually kept, outline page included.
    log.info("[%s] Pitch Writer is drafting the proposal", demo_id)
    pitch: Pitch | None = None
    try:
        pitch, _ = await run_pitch_writer(llm, gig, blueprint, working_mockup=not used_fallback,
                                          freelancer_name=settings.freelancer_name,
                                          freelancer_profile=settings.freelancer_profile)
    except LLMAuthError:
        raise
    except LLMError as exc:
        notes.append(f"Pitch Writer failed, so no proposal was saved: {exc}")

    proposal = proposal_path(settings.output_dir, demo_id)
    if pitch is None:
        proposal.unlink(missing_ok=True)  # an older proposal would describe a different screenshot
        return DemoScreenshot(demo_id, path, scored, model, used_fallback, notes)
    proposal.parent.mkdir(parents=True, exist_ok=True)
    proposal.write_text(pitch.message + "\n", encoding="utf-8")
    log.info("[%s] Saved %s", demo_id, proposal)
    return DemoScreenshot(demo_id, path, scored, model, used_fallback, notes, pitch, proposal)
