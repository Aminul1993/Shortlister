"""Place bids on Freelancer.com with its official SDK, freelancersdk.

A bid is the one step that acts on your behalf, so nothing here runs on its own. The portal first looks the
project up live (still open? which currency and budget?) and shows the bid in a form you can edit. Only
submitting that form places it. Every placed bid is recorded in output/bids/<demo_id>.json.

Freelancer's bid API takes text only, so nothing can be attached to a bid.
"""

from __future__ import annotations

import functools
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests
from freelancersdk.resources.projects import get_project_by_id, place_project_bid
from freelancersdk.resources.projects.exceptions import BidNotPlacedException, ProjectsNotFoundException
from freelancersdk.resources.users import get_self
from freelancersdk.resources.users.exceptions import SelfNotRetrievedException
from freelancersdk.session import Session

from .store import write_json

log = logging.getLogger(__name__)

BID_DIR = "bids"
PLATFORM = "Freelancer.com"  # the Gig.platform this module can bid on; its gig_id is the project id
# The SDK sets no timeout, so a stalled request would hold one of the portal's threads indefinitely.
TIMEOUT_SECONDS = 30


class BidError(Exception):
    """Freelancer.com could not be reached or refused the request. The message names the step that failed."""


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


def _session(token: str) -> Session:
    session = Session(oauth_token=token)
    session.session.request = functools.partial(session.session.request, timeout=TIMEOUT_SECONDS)
    return session


def _call(step: str, fn, *args, **kwargs):
    """Run one SDK call, turning its failure modes into a BidError that says which step failed and why."""
    try:
        return fn(*args, **kwargs)
    except (SelfNotRetrievedException, ProjectsNotFoundException, BidNotPlacedException) as exc:
        log.warning("%s failed: %s (%s, request %s)", step, exc, exc.error_code, exc.request_id)
        raise BidError(f"{step} failed: {exc}") from exc
    except (ValueError, KeyError, TypeError) as exc:  # a reply that isn't the JSON the SDK expects
        log.warning("%s failed: unexpected reply (%r)", step, exc)
        raise BidError(f"{step} failed: Freelancer.com sent an unexpected reply.") from exc
    except requests.RequestException as exc:
        raise BidError(f"{step} failed: could not reach Freelancer.com ({exc}).") from exc


def _account(session: Session) -> Account:
    me = _call("Checking your Freelancer.com account", get_self, session)
    return Account(int(me["id"]), me.get("username") or str(me["id"]))


def _project(session: Session, project_id: int) -> Project:
    return parse_project(_call("Looking up the project", get_project_by_id, session, project_id))


def lookup(token: str, project_id: int) -> tuple[Account, Project]:
    """The account a bid would come from and the project's live state. Places nothing."""
    session = _session(token)
    return _account(session), _project(session, project_id)


def place_bid(token: str, project_id: int, amount: float, period: int, milestone_percentage: int,
              description: str) -> dict:
    """Place one bid exactly as reviewed. Returns the record to keep; raises BidError if nothing was placed."""
    session = _session(token)
    account, project = _account(session), _project(session, project_id)
    if not project.open:
        raise BidError(f"The project is {project.status} on Freelancer.com, so it no longer takes bids.")
    bid = _call("Placing the bid", place_project_bid, session, project_id=project_id, bidder_id=account.user_id,
                description=description, amount=amount, period=period, milestone_percentage=milestone_percentage)
    log.info("Placed bid %s on project %s: %s", getattr(bid, "id", "?"), project_id, project.money(amount))
    return {
        "bid_id": getattr(bid, "id", None),
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


def placed_bids(output_dir: Path, demo_id: str) -> list[dict]:
    """Every bid placed on this gig from the portal, oldest first. `placed_at` is a datetime (or None)."""
    try:
        rows = json.loads((output_dir / BID_DIR / f"{demo_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    bids = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    for row in bids:
        try:
            row["placed_at"] = datetime.fromisoformat(row["placed_at"])
        except (KeyError, TypeError, ValueError):
            row["placed_at"] = None
    return bids


def save_bid(output_dir: Path, demo_id: str, record: dict) -> None:
    write_json(output_dir / BID_DIR / f"{demo_id}.json", [*placed_bids(output_dir, demo_id), record])
