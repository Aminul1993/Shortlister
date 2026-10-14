"""Background jobs for the portal: generate (and optionally deploy) the gigs a user selected.

Jobs run one at a time on a single daemon worker thread, so two jobs never write the same
output folder or compete for Ollama rate limits. Each gig inside a job is processed
concurrently and its status is updated as it moves through the pipeline:

    queued -> generating -> generated -> deploying -> live
                                    \\-> failed (at any step)
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
from ..deploy import deploy_project, preflight, vercel_api_client
from ..generator import generate_project
from ..llm import OllamaCloud
from ..models import ScoredGig

log = logging.getLogger(__name__)


@dataclass
class ItemStatus:
    rank: int
    title: str
    platform: str
    gig_url: str
    score: float
    state: str = "queued"
    project_id: str | None = None
    path: str | None = None
    model: str | None = None
    used_fallback: bool = False
    live_url: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Job:
    id: str
    created_at: datetime
    deploy: bool
    smoke: bool
    items: list[ItemStatus]
    state: str = "queued"  # queued -> running -> done | failed
    error: str | None = None

    @property
    def finished(self) -> bool:
        return self.state in {"done", "failed"}

    @property
    def done_count(self) -> int:
        terminal = {"live", "failed"} if self.deploy else {"generated", "failed"}
        return sum(item.state in terminal for item in self.items)

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

    def submit(self, chosen: list[tuple[int, ScoredGig]], *, deploy: bool, smoke: bool) -> str:
        items = [ItemStatus(rank, s.gig.title, s.gig.platform, s.gig.url, s.score) for rank, s in chosen]
        job = Job(uuid.uuid4().hex[:8], datetime.now(), deploy, smoke, items)
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
                    if item.state in {"queued", "generating", "deploying"}:
                        self._update(item, state="failed", error=f"Job stopped: {message}")
            else:
                self._update(job, state="failed" if job.failed_count == len(job.items) else "done")

    async def _run(self, job: Job, chosen: list[ScoredGig]) -> None:
        s = self.settings
        cli = await preflight(s) if job.deploy else None
        deploy_slots = asyncio.Semaphore(s.deploy_concurrency)
        async with (
            OllamaCloud(s.ollama_api_key, s.ollama_models, s.ollama_base_url, concurrency=s.llm_concurrency) as llm,
            vercel_api_client(s.vercel_token) as api,
        ):
            await asyncio.gather(*(
                self._build(job, item, scored, llm, api, cli, deploy_slots)
                for item, scored in zip(job.items, chosen)
            ))

    async def _build(self, job, item, scored, llm, api, cli, deploy_slots) -> None:
        s = self.settings
        self._update(item, state="generating")
        try:
            project = await generate_project(llm, scored, s, smoke=job.smoke)
        except Exception as exc:  # one broken gig must not stop the others
            log.exception("Generating '%s' failed", item.title)
            self._update(item, state="failed", error=str(exc) or type(exc).__name__)
            return
        self._update(item, state="generated", project_id=project.project_id, path=str(project.path),
                     model=project.model, used_fallback=project.used_fallback, notes=list(project.notes))
        if cli is None:
            return

        self._update(item, state="deploying")
        async with deploy_slots:
            try:
                result = await deploy_project(project.path, token=s.vercel_token, scope=s.vercel_scope,
                                              cli=cli, api=api)
            except OSError as exc:
                self._update(item, state="failed", error=f"could not run the Vercel CLI: {exc}")
                return
        self._update(item, state="live" if result.ok else "failed", live_url=result.live_url, error=result.error)
