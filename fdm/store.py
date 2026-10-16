"""Shortlist persistence for the web portal."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path

from .models import Gig, ScoredGig
from .ranking import render_table

SHORTLIST_FILE = "top10.json"
REPLACE_ATTEMPTS = 10  # about a second
_GIG_FIELDS = {f.name for f in fields(Gig)}


def gig_key(gig: Gig) -> str:
    """Stable identity for a gig across shortlist refreshes (ranks shift, ids don't)."""
    return f"{gig.platform}:{gig.gig_id}"


def make_demo_id(gig: Gig) -> str:
    """Lowercase, hyphenated and unique per gig; names the gig's saved files (proposals, bids)."""
    slug = re.sub(r"[^a-z0-9]+", "-", gig.title.lower()).strip("-")[:40].strip("-") or "demo"
    suffix = re.sub(r"[^a-z0-9]", "", gig.gig_id.lower())[-8:] or hashlib.sha1(gig.url.encode()).hexdigest()[:8]
    return f"{slug}-{suffix}"


def _write(path: Path, data: str | bytes) -> None:
    """Replace the file in one step: after a crash, readers find the old file or the new one, never half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with open(fd, "wb") if isinstance(data, bytes) else open(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(REPLACE_ATTEMPTS):  # Windows won't replace a file a reader or scanner has open
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(0.1)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_bytes(path: Path, data: bytes) -> None:
    _write(path, data)


def write_text(path: Path, text: str) -> None:
    """Writes what Path.write_text would (UTF-8, the platform's line endings), atomically."""
    _write(path, text)


def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, default=str))


def save_shortlist(ranked: list[ScoredGig], output_dir: Path) -> str:
    """Write top10.json and top10.md (table plus gig links). Returns the Markdown table."""
    table = render_table(ranked)
    write_json(output_dir / SHORTLIST_FILE, [{"rank": i, **asdict(s)} for i, s in enumerate(ranked, 1)])
    links = "\n".join(f"{i}. [{s.gig.title}]({s.gig.url})" for i, s in enumerate(ranked, 1))
    write_text(output_dir / "top10.md", f"{table}\n\n{links}\n")
    return table


def gig_from_dict(data: dict) -> Gig:
    """A Gig from its asdict() JSON, as saved in the shortlist and in proposal sidecars."""
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
                gig=gig_from_dict(row["gig"]),
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
