"""Place bids on Freelancer.com through its API (see fdm.freelancer).

A bid is the one step that acts on your behalf, so nothing here runs on its own. The portal first looks the
project up live (still open? which currency and budget?) and shows the bid in a form you can edit. Only
submitting that form places it. Every placed bid is recorded in output/bids/<demo_id>.json.

Freelancer's bid API takes text only, so nothing can be attached to a bid.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .freelancer import APIError, Freelancer
from .store import write_json

log = logging.getLogger(__name__)

BID_DIR = "bids"
PLATFORM = "Freelancer.com"  # the Gig.platform this module can bid on; its gig_id is the project id


class BidError(Exception):
    """Freelancer.com could not be reached or refused the request. The message names the step that failed."""


class BidLogError(BidError):
    """A bid log exists but can't be read, so whether a bid was already placed is unknown."""


@dataclass(frozen=True)
class Account:
    user_id: int
    username: str


@dataclass(frozen=True)
class Project:
    """A gig's live state on Freelancer.com. Amounts are in the project's own currency."""

    project_id: int
    title: str
    status: str  # Freelancer's frontend status: "open" while it takes bids
    pay_type: str  # "fixed" | "hourly"
    currency: str  # e.g. "USD", "INR"
    sign: str  # e.g. "$", "₹"
    usd_rate: float  # US dollars per unit of `currency`
    budget_min: float | None
    budget_max: float | None
    bid_count: int
    bid_avg: float | None

    @property
    def open(self) -> bool:
        return self.status in {"open", "active"}

    def money(self, amount: float | None) -> str:
        return "" if amount is None else money(amount, self.sign, self.currency)

    def amount_from_usd(self, usd: float | None) -> int | None:
        """A suggested bid in US dollars, converted to the project's currency and kept inside its budget."""
        if usd is None:
            return None
        amount = max(usd / self.usd_rate, self.budget_min or 0)
        return round(min(amount, self.budget_max or amount))


def money(amount: float, sign: str, currency: str) -> str:
    """For example "$150 USD" or "₹12,500.50 INR"."""
    return f"{sign}{amount:,.2f}".removesuffix(".00") + f" {currency}"


def bid_amount(record: dict) -> str:
    """How a placed bid reads, for example "₹12,500 INR" or "$25 USD/hr"."""
    hourly = "/hr" if record.get("pay_type") == "hourly" else ""
    return money(float(record.get("amount") or 0), record.get("sign") or "", record.get("currency") or "") + hourly


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_project(data: dict) -> Project:
    currency = data.get("currency") or {}
    budget = data.get("budget") or {}
    stats = data.get("bid_stats") or {}
    return Project(
        project_id=int(data["id"]),
        title=data.get("title") or "",
        status=data.get("frontend_project_status") or data.get("status") or "unknown",
        pay_type=data.get("type") or "fixed",
        currency=currency.get("code") or "USD",
        sign=currency.get("sign") or "$",
        usd_rate=_number(currency.get("exchange_rate")) or 1.0,
        budget_min=_number(budget.get("minimum")),
        budget_max=_number(budget.get("maximum")),
        bid_count=int(stats.get("bid_count") or 0),
        bid_avg=_number(stats.get("bid_avg")),
    )


@contextmanager
def _step(name: str, if_unsure: str = "") -> Iterator[None]:
    """Turn a failed call, or a reply without the fields read here, into a BidError that names the step.

    `if_unsure` is added to the message when Freelancer.com may have acted on the call anyway.
    """
    try:
        yield
    except APIError as exc:
        note = f" {if_unsure}" if exc.unsure and if_unsure else ""
        raise BidError(f"{name} failed: {exc}{note}") from exc
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("%s failed: unexpected reply (%r)", name, exc)
        raise BidError(f"{name} failed: Freelancer.com sent an unexpected reply.") from exc


def _account(api: Freelancer) -> Account:
    with _step("Checking your Freelancer.com account"):
        me = api.get_self()
        return Account(int(me["id"]), me.get("username") or str(me["id"]))


def _project(api: Freelancer, project_id: int) -> Project:
    with _step("Looking up the project"):
        return parse_project(api.get_project(project_id))


def lookup(token: str, project_id: int, *, transport: httpx.BaseTransport | None = None) -> tuple[Account, Project]:
    """The account a bid would come from and the project's live state. Places nothing."""
    with Freelancer(token, transport) as api:
        return _account(api), _project(api, project_id)


def place_bid(token: str, project_id: int, amount: float, period: int, milestone_percentage: int,
              description: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """Place one bid exactly as reviewed. Returns the record to keep; raises BidError if nothing was placed."""
    with Freelancer(token, transport) as api:
        account, project = _account(api), _project(api, project_id)
        if not project.open:
            raise BidError(f"The project is {project.status} on Freelancer.com, so it no longer takes bids.")
        # Only the call itself: once it returns, the bid is placed.
        with _step("Placing the bid", if_unsure="It may have been placed anyway, so check the project on "
                                                "Freelancer.com before you try again."):
            bid = api.place_bid(project_id=project_id, bidder_id=account.user_id, description=description,
                                amount=amount, period=period, milestone_percentage=milestone_percentage)
    log.info("Placed bid %s on project %s: %s", bid.get("id", "?"), project_id, project.money(amount))
    return {
        "bid_id": bid.get("id"),
        "project_id": project_id,
        "bidder": account.username,
        "amount": amount,
        "currency": project.currency,
        "sign": project.sign,
        "pay_type": project.pay_type,
        "period": period,
        "milestone_percentage": milestone_percentage,
        "placed_at": datetime.now().isoformat(timespec="seconds"),
    }


def _read_log(output_dir: Path, demo_id: str, strict: bool) -> list[dict]:
    """The bid log's records as saved. One that exists but can't be read is [], or BidLogError if strict."""
    path = output_dir / BID_DIR / f"{demo_id}.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        # Strict readers rewrite the log, so a row they can't read must stop them rather than be dropped.
        if not isinstance(rows, list) or (strict and not all(isinstance(row, dict) for row in rows)):
            raise ValueError("expected a list of bid records")
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.error("Cannot read the bid log %s: %s", path, exc)
        if strict:  # never bid, or rewrite the log, on the guess that there was no bid
            raise BidLogError(f"{path} cannot be read, so the portal can't tell whether a bid was already placed. "
                              "Fix or move that file, then try again.") from exc
        return []
    return [row for row in rows if isinstance(row, dict)]


def placed_bids(output_dir: Path, demo_id: str, *, strict: bool = False) -> list[dict]:
    """Every bid placed on this gig from the portal, oldest first. `placed_at` is a datetime (or None).

    A log that can't be read counts as no bids, which is fine for display. Before bidding, pass strict=True to
    get BidLogError instead.
    """
    bids = _read_log(output_dir, demo_id, strict)
    for row in bids:
        try:
            row["placed_at"] = datetime.fromisoformat(row["placed_at"])
        except (KeyError, TypeError, ValueError):
            row["placed_at"] = None
    return bids


def save_bid(output_dir: Path, demo_id: str, record: dict) -> None:
    """Add a placed bid to the gig's log. Raises BidLogError, leaving the log as it is, if it can't be read."""
    write_json(output_dir / BID_DIR / f"{demo_id}.json", [*_read_log(output_dir, demo_id, strict=True), record])
