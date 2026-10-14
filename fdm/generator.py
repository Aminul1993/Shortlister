"""Phase 2: run Architect -> Developer -> validate/repair for every shortlisted gig."""

from __future__ import annotations

import asyncio
import logging

from .agents import default_blueprint, run_architect, run_developer, run_repair
from .config import Settings
from .llm import LLMAuthError, LLMError, OllamaCloud
from .models import AppCode, GeneratedProject, ScoredGig
from .scaffold import fallback_app, make_project_id, project_metadata, write_project
from .validate import normalize_app, validate_app

log = logging.getLogger(__name__)


async def generate_project(llm: OllamaCloud, scored: ScoredGig, settings: Settings, *,
                           smoke: bool = True) -> GeneratedProject:
    gig = scored.gig
    project_id = make_project_id(gig)
    path = settings.output_dir / project_id
    notes: list[str] = []
    model: str | None = None

    log.info("[%s] Architect is designing the demo", project_id)
    try:
        blueprint, model = await run_architect(llm, gig)
    except LLMAuthError:
        raise
    except LLMError as exc:
        notes.append(f"Architect failed, using the default blueprint: {exc}")
        blueprint = default_blueprint(gig)

    accepted: AppCode | None = None
    try:
        log.info("[%s] Developer is writing index.py and templates/index.html", project_id)
        draft, model = await run_developer(llm, gig, blueprint)
        for attempt in range(settings.max_repairs + 1):
            draft = normalize_app(draft)
            problems = await asyncio.to_thread(validate_app, draft, path, smoke=smoke)
            if not problems:
                accepted = draft
                break
            notes.append(f"Validation attempt {attempt + 1} found {len(problems)} problem(s): {problems[0][:300]}")
            log.warning("[%s] %s", project_id, notes[-1])
            if attempt < settings.max_repairs:
                draft, model = await run_repair(llm, gig, blueprint, draft, problems)
    except LLMAuthError:
        raise
    except LLMError as exc:
        notes.append(f"Developer failed: {exc}")

    used_fallback = accepted is None
    if used_fallback:
        log.warning("[%s] Shipping the fallback demo app", project_id)
        accepted = fallback_app(gig, blueprint)

    write_project(path, accepted, project_metadata(gig, scored.score, blueprint, model, used_fallback, notes))
    log.info("[%s] Wrote %s", project_id, path)
    return GeneratedProject(project_id, path, scored, model, used_fallback, notes)


async def generate_all(chosen: list[ScoredGig], settings: Settings, *, smoke: bool = True) -> list[GeneratedProject]:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    async with OllamaCloud(
        settings.ollama_api_key,
        settings.ollama_models,
        settings.ollama_base_url,
        concurrency=settings.llm_concurrency,
    ) as llm:
        return list(await asyncio.gather(*(generate_project(llm, s, settings, smoke=smoke) for s in chosen)))
