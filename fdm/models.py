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
class Blueprint:
    """Agent A's design brief: what the demo page shows."""

    app_title: str
    tagline: str
    demo_summary: str
    ui_sections: list[str]
    sample_data: str


@dataclass
class Pitch:
    """Agent C's proposal: the message to send the client, plus a suggested bid for the bid form."""

    message: str
    bid_usd: float | None = None  # fixed price, or hourly rate for hourly gigs; always inside the client's budget
    delivery_days: int | None = None


@dataclass
class DemoScreenshot:
    """A saved screenshot of a gig's demo page and, when one was written, the proposal that goes with it.

    A proposal written without a screenshot has no `path`, `version` or `blueprint`.
    """

    demo_id: str
    path: Path | None
    scored: ScoredGig
    model: str | None
    used_fallback: bool
    notes: list[str] = field(default_factory=list)
    pitch: Pitch | None = None  # None if no proposal was written in this run; the screenshot is still kept
    proposal_path: Path | None = None
    blueprint: Blueprint | None = None  # what the screenshot shows; None for screenshots saved before versioning
    version: int | None = 1
    proposal_version: int | None = None
