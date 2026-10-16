"""Background jobs for the portal: write a new proposal for each selected gig.

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
from dataclasses import dataclass
from datetime import datetime

from ..config import Settings
from ..generator import generate_proposal
from ..llm import OllamaCloud
from ..models import ScoredGig
from ..store import make_demo_id

log = logging.getLogger(__name__)


@dataclass
class ItemStatus:
    rank: int | None  # None once the gig has left the shortlist
    title: str
    platform: str
    gig_url: str
    score: float
    demo_id: str
    state: str = "queued"
    model: str | None = None
    error: str | None = None
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
    state: str = "queued"  # queued -> running -> done | failed
    error: str | None = None

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

    def submit(self, chosen: list[tuple[int | None, ScoredGig]]) -> str:
        items = [ItemStatus(rank, s.gig.title, s.gig.platform, s.gig.url, s.score, make_demo_id(s.gig))
                 for rank, s in chosen]
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
        async with OllamaCloud(s.ollama_api_key, s.ollama_models, s.ollama_base_url,
                               concurrency=s.llm_concurrency) as llm:
            await asyncio.gather(*(self._build(item, scored, llm) for item, scored in zip(job.items, chosen)))

    async def _build(self, item: ItemStatus, scored: ScoredGig, llm: OllamaCloud) -> None:
        self._update(item, state="generating")
        try:
            proposal = await generate_proposal(llm, scored, self.settings)
        except Exception as exc:  # one broken gig must not stop the others
            log.exception("Writing a proposal for '%s' failed", item.title)
            self._update(item, state="failed", error=str(exc) or type(exc).__name__)
            return
        pitch = proposal.pitch
        self._update(item, state="done", model=proposal.model, proposal=pitch.message,
                     proposal_file=proposal.path.name, proposal_path=str(proposal.path),
                     proposal_version=proposal.version,
                     bid=bid_label(pitch.bid_usd, scored.gig.pay_type), delivery_days=pitch.delivery_days)
