import json
import re
from types import SimpleNamespace

import httpx
import pytest

import fdm.bids as bids
from fdm.bids import BidError, BidLogError, bid_amount, parse_project, placed_bids, save_bid
from fdm.freelancer import TIMEOUT_SECONDS

# A trimmed GET /api/projects/0.1/projects/<id>/ result.
PROJECT = {
    "id": 101, "title": "Bakery landing page", "status": "active", "frontend_project_status": "open",
    "type": "fixed", "currency": {"code": "INR", "sign": "₹", "exchange_rate": 0.012},
    "budget": {"minimum": 1500, "maximum": 12500}, "bid_stats": {"bid_count": 7, "bid_avg": 6400.5},
}


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "result": result})


def refused(status: int, message: str, code: str) -> httpx.Response:
    return httpx.Response(status, json={"status": "error", "message": message, "error_code": code,
                                        "request_id": "r1"})


@pytest.fixture
def api():
    """A fake Freelancer.com API. Replace `replies[path]` to change one endpoint; `requests` records the calls."""
    calls: list[httpx.Request] = []
    replies = {
        "/api/users/0.1/self/": lambda request: ok({"id": 42, "username": "aminul"}),
        "/api/projects/0.1/projects/101/": lambda request: ok(PROJECT),
        "/api/projects/0.1/bids/": lambda request: ok({"id": 555, **json.loads(request.content)}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return replies[request.url.path](request)

    return SimpleNamespace(transport=httpx.MockTransport(handler), requests=calls, replies=replies)


def bids_sent(api) -> list[dict]:
    return [json.loads(r.content) for r in api.requests if r.method == "POST"]


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


def test_lookup_calls_the_api_as_the_token_owner(api):
    account, project = bids.lookup("token", 101, transport=api.transport)
    assert (account.user_id, account.username, project.project_id, project.currency) == (42, "aminul", 101, "INR")
    assert [(r.method, str(r.url)) for r in api.requests] == [
        ("GET", "https://www.freelancer.com/api/users/0.1/self/"),
        ("GET", "https://www.freelancer.com/api/projects/0.1/projects/101/"),
    ]
    for request in api.requests:
        assert request.headers["Freelancer-OAuth-V1"] == "token"
        # Every call times out, so a stalled one can't hold a portal thread.
        assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), TIMEOUT_SECONDS)


def test_place_bid_sends_the_reviewed_values(api):
    record = bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=50, description="A pitch.",
                            transport=api.transport)
    assert bids_sent(api) == [{"project_id": 101, "bidder_id": 42, "description": "A pitch.", "amount": 5000,
                               "period": 4, "milestone_percentage": 50}]
    assert str(api.requests[-1].url) == "https://www.freelancer.com/api/projects/0.1/bids/"
    assert record["bid_id"] == 555 and record["bidder"] == "aminul" and record["currency"] == "INR"
    assert bid_amount(record) == "₹5,000 INR"
    assert bid_amount({**record, "pay_type": "hourly", "amount": 12.5}) == "₹12.50 INR/hr"


def test_place_bid_refuses_a_closed_project(api):
    api.replies["/api/projects/0.1/projects/101/"] = lambda request: ok({**PROJECT, "frontend_project_status": "closed"})
    with pytest.raises(BidError, match="The project is closed on Freelancer.com"):
        bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=100, description="A pitch.",
                       transport=api.transport)
    assert bids_sent(api) == []


def _raise(exc: Exception):
    def reply(request):
        raise exc
    return reply


@pytest.mark.parametrize(("reply", "message"), [
    (lambda request: refused(401, "You must be logged in", "AUTH_REQUIRED"),
     "Checking your Freelancer.com account failed: You must be logged in"),
    (_raise(httpx.ConnectTimeout("timed out")),
     "Checking your Freelancer.com account failed: could not reach Freelancer.com (timed out)."),
    (lambda request: httpx.Response(200, text="<html>"), "Freelancer.com sent an unexpected reply."),
    (lambda request: httpx.Response(502, text="<html>Bad gateway</html>"),
     "Freelancer.com sent an unexpected reply (HTTP 502)."),
    (lambda request: refused(500, "", "INTERNAL_ERROR"), "Freelancer.com sent an unexpected reply (HTTP 500)."),
    (lambda request: ok({"username": "no id"}), "Freelancer.com sent an unexpected reply."),
    (lambda request: httpx.Response(200, json={"status": "success"}), "Freelancer.com sent an unexpected reply."),
])
def test_api_failures_become_bid_errors(api, reply, message):
    api.replies["/api/users/0.1/self/"] = reply
    with pytest.raises(BidError, match=re.escape(message)):
        bids.lookup("token", 101, transport=api.transport)


def test_an_unreadable_project_is_reported_not_raised(api):
    api.replies["/api/projects/0.1/projects/101/"] = lambda request: ok({**PROJECT, "currency": "INR"})
    with pytest.raises(BidError, match="Looking up the project failed: Freelancer.com sent an unexpected reply."):
        bids.lookup("token", 101, transport=api.transport)


def test_a_refused_bid_reports_freelancers_reason(api):
    api.replies["/api/projects/0.1/bids/"] = lambda request: refused(409, "You have already bid on this project.",
                                                                     "BID_EXISTS")
    with pytest.raises(BidError, match="Placing the bid failed: You have already bid on this project."):
        bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=100, description="A pitch.",
                       transport=api.transport)


def test_placed_bids_round_trip(tmp_path):
    assert placed_bids(tmp_path, "bakery-101") == []
    save_bid(tmp_path, "bakery-101", {"bid_id": 1, "amount": 90, "placed_at": "2026-10-15T09:30:00"})
    save_bid(tmp_path, "bakery-101", {"bid_id": 2, "amount": 95, "placed_at": "not a date"})
    first, second = placed_bids(tmp_path, "bakery-101")
    assert first["bid_id"] == 1 and first["placed_at"].hour == 9
    assert second["bid_id"] == 2 and second["placed_at"] is None
    # The log keeps each record exactly as it was saved.
    saved = json.loads((tmp_path / "bids" / "bakery-101.json").read_text(encoding="utf-8"))
    assert [row["placed_at"] for row in saved] == ["2026-10-15T09:30:00", "not a date"]


@pytest.mark.parametrize("content", ["{not json", '{"bid_id": 1}', b"\xff\xfe"])
def test_an_unreadable_bid_log_is_never_guessed_empty_when_it_matters(tmp_path, content):
    log = tmp_path / "bids" / "broken-1.json"
    log.parent.mkdir()
    log.write_bytes(content if isinstance(content, bytes) else content.encode())
    before = log.read_bytes()

    assert placed_bids(tmp_path, "broken-1") == []  # fine for display
    with pytest.raises(BidLogError, match="can't tell whether a bid was already placed"):
        placed_bids(tmp_path, "broken-1", strict=True)
    # Recording a bid must not replace the log with only the new record.
    with pytest.raises(BidLogError):
        save_bid(tmp_path, "broken-1", {"bid_id": 2})
    assert log.read_bytes() == before
    assert issubclass(BidLogError, BidError)


@pytest.mark.parametrize(("reply", "unsure"), [
    (_raise(httpx.ReadTimeout("timed out")), True),  # sent, but the answer was lost
    (_raise(httpx.RemoteProtocolError("Server disconnected")), True),
    (lambda request: httpx.Response(200, text="<html>"), True),  # a garbled success
    (lambda request: httpx.Response(504, text="Gateway Timeout"), True),
    (_raise(httpx.ConnectError("Name or service not known")), False),  # never sent
    (lambda request: refused(409, "You have already bid on this project.", "BID_EXISTS"), False),
])
def test_an_unclear_bid_outcome_says_the_bid_may_have_been_placed(api, reply, unsure):
    api.replies["/api/projects/0.1/bids/"] = reply
    with pytest.raises(BidError, match="Placing the bid failed: ") as caught:
        bids.place_bid("token", 101, amount=5000, period=4, milestone_percentage=100, description="A pitch.",
                       transport=api.transport)
    assert ("It may have been placed anyway, so check the project on Freelancer.com" in str(caught.value)) == unsure


def test_a_strict_read_refuses_rows_it_would_drop(tmp_path):
    log = tmp_path / "bids" / "shop-1.json"
    log.parent.mkdir()
    log.write_text('[{"bid_id": 1}, "a stray line"]', encoding="utf-8")
    assert [row["bid_id"] for row in placed_bids(tmp_path, "shop-1")] == [1]  # display keeps what it can
    with pytest.raises(BidLogError):
        save_bid(tmp_path, "shop-1", {"bid_id": 2})
    assert log.read_text(encoding="utf-8") == '[{"bid_id": 1}, "a stray line"]'
