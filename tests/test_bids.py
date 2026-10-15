import re
from types import SimpleNamespace

import pytest
import requests
from freelancersdk.resources.projects.exceptions import BidNotPlacedException
from freelancersdk.resources.users.exceptions import SelfNotRetrievedException

import fdm.bids as bids
from fdm.bids import BidError, bid_amount, parse_project, placed_bids, save_bid

# A trimmed GET /api/projects/0.1/projects/<id>/ result.
PROJECT = {
    "id": 101, "title": "Bakery landing page", "status": "active", "frontend_project_status": "open",
    "type": "fixed", "currency": {"code": "INR", "sign": "₹", "exchange_rate": 0.012},
    "budget": {"minimum": 1500, "maximum": 12500}, "bid_stats": {"bid_count": 7, "bid_avg": 6400.5},
}


@pytest.fixture
def sdk(monkeypatch):
    """Fakes the three SDK calls the module makes and records what was sent."""
    calls = {"placed": []}

    def place(session, **bid):
        calls["placed"].append(bid)
        return SimpleNamespace(id=555, **bid)

    monkeypatch.setattr(bids, "get_self", lambda session: {"id": 42, "username": "aminul"})
    monkeypatch.setattr(bids, "get_project_by_id", lambda session, project_id: {**PROJECT, "id": project_id})
    monkeypatch.setattr(bids, "place_project_bid", place)
    return calls


def test_parse_project_converts_a_usd_suggestion_into_the_budget():
    project = parse_project(PROJECT)
    assert project.open and project.currency == "INR" and project.bid_count == 7
    assert project.amount_from_usd(60) == 5000  # $60 at 0.012 USD per rupee
    assert project.amount_from_usd(1000) == 12500 and project.amount_from_usd(1) == 1500
    assert project.amount_from_usd(None) is None
    assert project.money(6400.5) == "₹6,400.50 INR" and project.money(5000) == "₹5,000 INR"

    bare = parse_project({"id": 7, "status": "closed"})  # older or trimmed replies still parse
    assert not bare.open and bare.currency == "USD" and bare.usd_rate == 1.0 and bare.budget_max is None
    assert bare.amount_from_usd(80.4) == 80


def test_place_bid_sends_the_reviewed_values(sdk):
    record = bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=50, description="A pitch.")
    assert sdk["placed"] == [{"project_id": 101, "bidder_id": 42, "description": "A pitch.", "amount": 5000,
                              "period": 4, "milestone_percentage": 50}]
    assert record["bid_id"] == 555 and record["bidder"] == "aminul" and record["currency"] == "INR"
    assert bid_amount(record) == "₹5,000 INR"
    assert bid_amount({**record, "pay_type": "hourly", "amount": 12.5}) == "₹12.50 INR/hr"


def test_place_bid_refuses_a_closed_project(sdk, monkeypatch):
    monkeypatch.setattr(bids, "get_project_by_id",
                        lambda session, project_id: {**PROJECT, "frontend_project_status": "closed"})
    with pytest.raises(BidError, match="The project is closed on Freelancer.com"):
        bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=100, description="A pitch.")
    assert sdk["placed"] == []


@pytest.mark.parametrize(("failure", "message"), [
    (SelfNotRetrievedException("You must be logged in", "AUTH_REQUIRED", "r1"),
     "Checking your Freelancer.com account failed: You must be logged in"),
    (requests.ConnectionError("timed out"), "could not reach Freelancer.com (timed out)"),
    (requests.JSONDecodeError("Expecting value", "<html>", 0), "Freelancer.com sent an unexpected reply"),
    (KeyError("message"), "Freelancer.com sent an unexpected reply"),
])
def test_sdk_failures_become_bid_errors(sdk, monkeypatch, failure, message):
    def fail(session):
        raise failure

    monkeypatch.setattr(bids, "get_self", fail)
    with pytest.raises(BidError, match=re.escape(message)):
        bids.lookup("token", 101)


def test_a_refused_bid_reports_freelancers_reason(sdk, monkeypatch):
    def refuse(session, **bid):
        raise BidNotPlacedException("You have already bid on this project.", "BID_EXISTS", "r2")

    monkeypatch.setattr(bids, "place_project_bid", refuse)
    with pytest.raises(BidError, match="Placing the bid failed: You have already bid on this project."):
        bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=100, description="A pitch.")


def test_sessions_time_out():
    session = bids._session("token")
    assert session.session.headers["Freelancer-OAuth-V1"] == "token"
    assert session.session.request.keywords == {"timeout": bids.TIMEOUT_SECONDS}


def test_placed_bids_round_trip(tmp_path):
    assert placed_bids(tmp_path, "bakery-101") == []
    save_bid(tmp_path, "bakery-101", {"bid_id": 1, "amount": 90, "placed_at": "2026-10-15T09:30:00"})
    save_bid(tmp_path, "bakery-101", {"bid_id": 2, "amount": 95, "placed_at": "not a date"})
    first, second = placed_bids(tmp_path, "bakery-101")
    assert first["bid_id"] == 1 and first["placed_at"].hour == 9
    assert second["bid_id"] == 2 and second["placed_at"] is None

    (tmp_path / "bids" / "broken-1.json").write_text("{not json", encoding="utf-8")
    assert placed_bids(tmp_path, "broken-1") == []
