"""Background jobs for the portal: screenshot a generated demo page for each gig a user selected.

Jobs run one at a time on a single daemon worker thread, so two jobs never write the same
screenshot or compete for Ollama rate limits. Each gig inside a job is processed concurrently
and its status is updated as it moves through the pipeline:

    queued -> generating -> captured
                        \\-> failed
"""

from __future__ import annotations

import asyncio
import copy
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings
from ..generator import generate_screenshot
from ..llm import OllamaCloud
from ..models import ScoredGig
from ..screenshot import Renderer

log = logging.getLogger(__name__)


@dataclass
class ItemStatus:
    rank: int
    title: str
    platform: str
    gig_url: str
    score: float
    state: str = "queued"
    screenshot: str | None = None  # file name inside output/screenshots/
    path: str | None = None
    model: str | None = None
    used_fallback: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Job:
    id: str
    created_at: datetime
    items: list[ItemStatus]
    state: str = "queued"  # queued -> running -> done | failed
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.state in {"done", "failed"}

    @property
    def done_count(self) -> int:
        return sum(item.state in {"captured", "failed"} for item in self.items)

    @property
    def failed_count(self) -> int:
        return sum(item.state == "failed" for item in self.items)


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[Job, list[ScoredGig]]] = queue.Queue()
        threading.Thread(target=self._work, name="fdm-jobs", daemon=True).start()

    def submit(self, chosen: list[tuple[int, ScoredGig]]) -> str:
        items = [ItemStatus(rank, s.gig.title, s.gig.platform, s.gig.url, s.score) for rank, s in chosen]
        job = Job(uuid.uuid4().hex[:8], datetime.now(), items)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put((job, [scored for _, scored in chosen]))
        return job.id

    def get(self, job_id: str) -> Job | None:
        """A consistent snapshot, safe to render while the worker keeps updating the job."""
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def recent(self, limit: int = 6) -> list[Job]:
        with self._lock:
            return copy.deepcopy(sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit])

    def _update(self, target: Job | ItemStatus, **changes: object) -> None:
        with self._lock:
            for name, value in changes.items():
                setattr(target, name, value)

    def _work(self) -> None:
        while True:
            job, chosen = self._queue.get()
            self._update(job, state="running")
            try:
                asyncio.run(self._run(job, chosen))
            except Exception as exc:  # report on the job page instead of dying silently in this thread
                log.exception("Job %s failed", job.id)
                message = str(exc) or type(exc).__name__
                self._update(job, state="failed", error=message)
                for item in job.items:
                    if item.state in {"queued", "generating"}:
                        self._update(item, state="failed", error=f"Job stopped: {message}")
            else:
                self._update(job, state="failed" if job.failed_count == len(job.items) else "done")

    async def _run(self, job: Job, chosen: list[ScoredGig]) -> None:
        s = self.settings
        # The browser starts first, so a missing Chromium fails the job before any LLM tokens are spent.
        async with (
            Renderer() as renderer,
            OllamaCloud(s.ollama_api_key, s.ollama_models, s.ollama_base_url, concurrency=s.llm_concurrency) as llm,
        ):
            await asyncio.gather(*(
                self._build(item, scored, llm, renderer) for item, scored in zip(job.items, chosen)
            ))

    async def _build(self, item: ItemStatus, scored: ScoredGig, llm: OllamaCloud, renderer: Renderer) -> None:
        self._update(item, state="generating")
        try:
            shot = await generate_screenshot(llm, renderer, scored, self.settings)
        except Exception as exc:  # one broken gig must not stop the others
            log.exception("Generating '%s' failed", item.title)
            self._update(item, state="failed", error=str(exc) or type(exc).__name__)
            return
        self._update(item, state="captured", screenshot=shot.path.name, path=str(shot.path), model=shot.model,
                     used_fallback=shot.used_fallback, notes=list(shot.notes))
