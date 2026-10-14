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
    """Agent A's data contract: what the demo shows and what the template receives."""

    app_title: str
    tagline: str
    demo_summary: str
    routes: list[dict]
    template_variables: list[dict]
    ui_sections: list[str]
    sample_data: str


@dataclass
class AppCode:
    """Agent B's output: the two files that make up a demo app."""

    index_py: str
    index_html: str


@dataclass
class GeneratedProject:
    project_id: str
    path: Path
    scored: ScoredGig
    model: str | None
    used_fallback: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class DeployResult:
    project_id: str
    ok: bool
    live_url: str | None = None
    deployment_url: str | None = None
    error: str | None = None
