"""Shortlist persistence for the web portal."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from .models import Gig, ScoredGig
from .ranking import render_table

SHORTLIST_FILE = "top10.json"
_GIG_FIELDS = {f.name for f in fields(Gig)}


def gig_key(gig: Gig) -> str:
    """Stable identity for a gig across shortlist refreshes (ranks shift, ids don't)."""
    return f"{gig.platform}:{gig.gig_id}"


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def save_shortlist(ranked: list[ScoredGig], output_dir: Path) -> str:
    """Write top10.json and top10.md (table plus gig links). Returns the Markdown table."""
    table = render_table(ranked)
    write_json(output_dir / SHORTLIST_FILE, [{"rank": i, **asdict(s)} for i, s in enumerate(ranked, 1)])
    links = "\n".join(f"{i}. [{s.gig.title}]({s.gig.url})" for i, s in enumerate(ranked, 1))
    (output_dir / "top10.md").write_text(f"{table}\n\n{links}\n", encoding="utf-8")
    return table


def _gig_from_dict(data: dict) -> Gig:
    values = {key: value for key, value in data.items() if key in _GIG_FIELDS}
    posted = values.get("posted_at")
    values["posted_at"] = datetime.fromisoformat(posted) if posted else None
    return Gig(**values)


def load_shortlist(output_dir: Path) -> list[ScoredGig]:
    """Read the saved shortlist in rank order. A missing or unreadable file yields []."""
    try:
        rows = json.loads((output_dir / SHORTLIST_FILE).read_text(encoding="utf-8"))
        return [
            ScoredGig(
                gig=_gig_from_dict(row["gig"]),
                score=float(row["score"]),
                breakdown=row.get("breakdown") or {},
                signals=row.get("signals") or [],
            )
            for row in sorted(rows, key=lambda row: row.get("rank", 0))
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def shortlist_updated_at(output_dir: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp((output_dir / SHORTLIST_FILE).stat().st_mtime)
    except OSError:
        return None
