import json
from dataclasses import replace

import pytest

from fdm.agents import fit_bid, parse_pitch
from fdm.models import Gig
from fdm.store import make_demo_id

GIG = Gig(platform="Freelancer.com", gig_id="40767032", title="Company Information Website Build!",
          description="Need a 5 page website with a contact form.", url="https://example.com/g", skills=["PHP"])


def test_demo_id_is_a_safe_file_name():
    assert make_demo_id(GIG) == "company-information-website-build-40767032"


PITCH_TEXT = ("You need a 5-page company site with a contact form that works on phones.\n\n"
              "Visitors land on a home page with your services, then reach you through a short form on any device.")


def test_parse_pitch_cleans_the_message():
    reply = {"message": f"**Hello**  \n\n\n\n{PITCH_TEXT}\n\nA quick non‑breaking hyphen.", "bid_usd": 140.5,
             "delivery_days": 3.6}
    pitch = parse_pitch(json.dumps(reply))
    assert pitch.message == f"Hello\n\n{PITCH_TEXT}\n\nA quick non-breaking hyphen."
    assert pitch.bid_usd == 140.5 and pitch.delivery_days == 4
    assert parse_pitch(json.dumps({"message": PITCH_TEXT, "bid_usd": 0, "delivery_days": "soon"})).bid_usd is None
    odd = parse_pitch(json.dumps({"message": PITCH_TEXT, "bid_usd": float("nan"), "delivery_days": float("inf")}))
    assert odd.bid_usd is None and odd.delivery_days is None


@pytest.mark.parametrize(("message", "expected"), [
    ("Thanks!", "too short"),
    (f"{PITCH_TEXT}\n\nBest,\n[Your Name]", "placeholder [Your Name]"),
    (f"Hi [client name], {PITCH_TEXT}", "placeholder [client name]"),
])
def test_parse_pitch_rejects_unsendable_messages(message, expected):
    with pytest.raises(ValueError) as excinfo:
        parse_pitch(json.dumps({"message": message, "bid_usd": 100, "delivery_days": 2}))
    assert expected in str(excinfo.value)


def test_fit_bid_stays_inside_the_budget():
    gig = replace(GIG, budget_min_usd=30.0, budget_max_usd=250.0)
    assert fit_bid(120.4, gig) == 120
    assert fit_bid(900, gig) == 250 and fit_bid(5, gig) == 30
    assert fit_bid(119.6, GIG) == 120  # no budget, so the Pitch Writer's estimate stands
    assert fit_bid(10, replace(GIG, budget_min_usd=30.0)) == 30
    assert fit_bid(None, gig) is None
