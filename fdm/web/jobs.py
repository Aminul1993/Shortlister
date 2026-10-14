"""Background jobs for the portal: for each selected gig, make a new screenshot, a new proposal, or both.

Jobs run one at a time on a single daemon worker thread, so two jobs never claim the same version
number or compete for Ollama rate limits. Each gig inside a job is processed concurrently and its
status is updated as it moves through the pipeline:

    queued -> generating -> done
                        \\-> failed
"""

from __future__ import annotations

import asyncio
import copy
import logging
import queue
import threading
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime

from ..config import Settings
from ..generator import generate_demo, generate_proposal, generate_screenshot
from ..llm import OllamaCloud
from ..models import ScoredGig
from ..pages import make_demo_id
from ..screenshot import Renderer

log = logging.getLogger(__name__)

# What a job makes for each selected gig. Every run adds new versions; nothing is overwritten.
MODES = {"both": "Screenshots & proposals", "screenshot": "Screenshots only", "proposal": "Proposals only"}


@dataclass
class ItemStatus:
    rank: int
    title: str
    platform: str
    gig_url: str
    score: float
    demo_id: str
    state: str = "queued"
    # File name inside output/screenshots/. Proposal-only jobs show the existing screenshot the proposal describes,
    # or none if the proposal was written without one.
    screenshot: str | None = None
    path: str | None = None
    screenshot_version: int | None = None
    model: str | None = None
    used_fallback: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    proposal: str | None = None  # the message to send the client
    proposal_file: str | None = None  # file name inside output/proposals/
    proposal_path: str | None = None
    proposal_version: int | None = None
    bid: str | None = None  # e.g. "~$120" or "~$15/hr"
    delivery_days: int | None = None


def bid_label(bid_usd: float | None, pay_type: str | None) -> str | None:
    if bid_usd is None:
        return None
    return f"~${bid_usd:,.0f}{'/hr' if pay_type == 'hourly' else ''}"


@dataclass
class Job:
    id: str
    created_at: datetime
    items: list[ItemStatus]
    mode: str = "both"  # a key of MODES
    state: str = "queued"  # queued -> running -> done | failed
    error: str | None = None

    @property
    def mode_label(self) -> str:
        return MODES[self.mode]

    @property
    def finished(self) -> bool:
        return self.state in {"done", "failed"}

    @property
    def done_count(self) -> int:
        return sum(item.state in {"done", "failed"} for item in self.items)

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

    def submit(self, chosen: list[tuple[int, ScoredGig]], mode: str = "both") -> str:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        items = [ItemStatus(rank, s.gig.title, s.gig.platform, s.gig.url, s.score, make_demo_id(s.gig))
                 for rank, s in chosen]
        job = Job(uuid.uuid4().hex[:8], datetime.now(), items, mode)
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
        async with AsyncExitStack() as stack:
            # The browser starts first, so a missing Chromium fails the job before any LLM tokens are spent.
            # Proposal-only jobs describe screenshots that already exist and need no browser.
            renderer = None if job.mode == "proposal" else await stack.enter_async_context(Renderer())
            llm = await stack.enter_async_context(
                OllamaCloud(s.ollama_api_key, s.ollama_models, s.ollama_base_url, concurrency=s.llm_concurrency))
            await asyncio.gather(*(
                self._build(item, scored, job.mode, llm, renderer) for item, scored in zip(job.items, chosen)
            ))

    async def _build(self, item: ItemStatus, scored: ScoredGig, mode: str, llm: OllamaCloud,
                     renderer: Renderer | None) -> None:
        self._update(item, state="generating")
        try:
            if mode == "proposal":
                shot = await generate_proposal(llm, scored, self.settings)
            elif mode == "screenshot":
                shot = await generate_screenshot(llm, renderer, scored, self.settings)
            else:
                shot = await generate_demo(llm, renderer, scored, self.settings)
        except Exception as exc:  # one broken gig must not stop the others
            log.exception("Generating '%s' failed", item.title)
            self._update(item, state="failed", error=str(exc) or type(exc).__name__)
            return
        pitch, proposal = shot.pitch, shot.proposal_path
        self._update(item, state="done",
                     screenshot=shot.path.name if shot.path else None, path=str(shot.path) if shot.path else None,
                     screenshot_version=shot.version, model=shot.model,
                     used_fallback=shot.used_fallback, notes=list(shot.notes),
                     proposal=pitch.message if pitch else None,
                     proposal_file=proposal.name if proposal else None,
                     proposal_path=str(proposal) if proposal else None,
                     proposal_version=shot.proposal_version,
                     bid=bid_label(pitch.bid_usd, scored.gig.pay_type) if pitch else None,
                     delivery_days=pitch.delivery_days if pitch else None)
