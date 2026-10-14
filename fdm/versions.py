"""Saved versions of each gig's screenshot and proposal.

Regenerating never overwrites: every run saves the next version beside the earlier ones. Version 1 keeps
the plain name (screenshots/<demo_id>.png), so files saved before versioning count as version 1, and
later versions add a suffix (screenshots/<demo_id>-v2.png). A JSON sidecar with the same stem records
how each file was made: the design brief behind a screenshot, and which screenshot a proposal describes
along with its suggested bid. Files saved before versioning have no sidecar.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SCREENSHOT_DIR = "screenshots"
PROPOSAL_DIR = "proposals"
DEMO_ID = re.compile(r"[a-z0-9-]+")  # what pages.make_demo_id produces; also keeps globs and paths safe


@dataclass
class Version:
    number: int
    path: Path
    saved_at: datetime
    meta: dict = field(default_factory=dict)  # the sidecar; {} for files saved before versioning


def _read_meta(path: Path) -> dict:
    try:
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _versions(folder: Path, demo_id: str, suffix: str) -> list[Version]:
    pattern = re.compile(rf"{re.escape(demo_id)}(?:-v(\d+))?{re.escape(suffix)}")
    found: list[Version] = []
    for path in folder.glob(f"{demo_id}*{suffix}"):
        match = pattern.fullmatch(path.name)
        if not match:
            continue  # another gig whose id starts with this one
        try:
            saved_at = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        found.append(Version(int(match.group(1) or 1), path, saved_at, _read_meta(path)))
    return sorted(found, key=lambda version: version.number)


def _save(folder: Path, demo_id: str, suffix: str, data: bytes, meta: dict) -> Version:
    folder.mkdir(parents=True, exist_ok=True)
    existing = _versions(folder, demo_id, suffix)
    number = existing[-1].number + 1 if existing else 1
    path = folder / f"{demo_id if number == 1 else f'{demo_id}-v{number}'}{suffix}"
    # The sidecar goes first, so anyone who finds the file can also read how it was made.
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    path.write_bytes(data)
    return Version(number, path, datetime.now(), meta)


def screenshots(output_dir: Path, demo_id: str) -> list[Version]:
    """Every saved screenshot of this gig, oldest first."""
    return _versions(output_dir / SCREENSHOT_DIR, demo_id, ".png")


def proposals(output_dir: Path, demo_id: str) -> list[Version]:
    """Every saved proposal for this gig, oldest first."""
    return _versions(output_dir / PROPOSAL_DIR, demo_id, ".txt")


def save_screenshot(output_dir: Path, demo_id: str, png: bytes, meta: dict) -> Version:
    return _save(output_dir / SCREENSHOT_DIR, demo_id, ".png", png, meta)


def save_proposal(output_dir: Path, demo_id: str, text: str, meta: dict) -> Version:
    return _save(output_dir / PROPOSAL_DIR, demo_id, ".txt", text.encode("utf-8"), meta)


def described_screenshot(proposal: Version, shots: list[Version]) -> int | None:
    """The screenshot version a proposal was written for, or None if that is unknown."""
    if "screenshot_version" in proposal.meta:
        return proposal.meta["screenshot_version"]
    # Saved before versioning, when a proposal was always written with the screenshot of the same name.
    if not proposal.meta and shots and shots[0].number == 1 and not shots[0].meta:
        return 1
    return None
