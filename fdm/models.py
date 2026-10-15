"""Plain data objects passed between the pipeline phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class Gig:
    platform: str
    gig_id: str
    title: str
    description: str
    url: str
    skills: list[str] = field(default_factory=list)
    budget_min_usd: float | None = None
    budget_max_usd: float | None = None
    pay_type: str = "unknown"  # "fixed" | "hourly" | "unknown"
    posted_at: datetime | None = None


@dataclass
class ScoredGig:
    gig: Gig
    score: float  # 1.0 – 10.0
    breakdown: dict[str, float]
    signals: list[str]


@dataclass
class Pitch:
    """The Pitch Writer's proposal: the message to send the client, plus a suggested bid for the bid form."""

    message: str
    bid_usd: float | None = None  # fixed price, or hourly rate for hourly gigs; always inside the client's budget
    delivery_days: int | None = None


@dataclass
class Proposal:
    """A proposal saved as the gig's next version."""

    demo_id: str
    scored: ScoredGig
    pitch: Pitch
    model: str
    path: Path
    version: int
