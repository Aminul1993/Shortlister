import pytest

from fdm.cli import parse_selection
from fdm.models import Gig
from fdm.ranking import rank_gigs, render_table, score_gig


def make_gig(title: str, description: str, **fields) -> Gig:
    return Gig(platform="Freelancer.com", gig_id=fields.pop("gig_id", title[:10]), title=title,
               description=description, url="https://example.com/gig", **fields)


EASY = make_gig(
    "Simple landing page for my bakery",
    "I need a one-page landing page with:\n- hero section\n- menu with prices\n- contact form\n"
    "It should be responsive. HTML/CSS/Tailwind is fine.",
    budget_max_usd=120, pay_type="fixed",
)
HARD = make_gig(
    "Marketplace with Stripe payments",
    "Build a multi-vendor marketplace with user accounts, login, Stripe checkout, a PostgreSQL database "
    "and an Android app. Long-term ongoing work.",
    budget_max_usd=5000, pay_type="fixed",
)
SEO = make_gig(
    "Off-page SEO for two sites",
    "Link building, social media marketing and SEO articles for my websites.",
    skills=["SEO", "Link Building"],
)


def test_easy_web_gig_ranks_first():
    ranked = rank_gigs([HARD, SEO, EASY])
    assert ranked[0].gig is EASY


def test_score_bounds_and_spread():
    easy, hard, seo = score_gig(EASY), score_gig(HARD), score_gig(SEO)
    for item in (easy, hard, seo):
        assert 1.0 <= item.score <= 10.0
    assert easy.score >= 8
    assert hard.score <= 4
    assert "not a web build" in seo.signals


def test_dependency_signals_are_reported():
    signals = score_gig(HARD).signals
    assert "-stripe" in signals
    assert "-android" in signals
    assert "+landing page" in score_gig(EASY).signals


def test_render_table_has_required_columns_and_escapes_pipes():
    gig = make_gig("Calc | tool", "A tiny calculator | nothing else")
    table = render_table(rank_gigs([gig]))
    assert table.splitlines()[0] == "| Rank | Title | Platform | Brief | Suitability Score |"
    assert "Calc \\| tool" in table
    assert "/10 |" in table


def test_parse_selection():
    assert parse_selection("1, 4, 7", 10) == [1, 4, 7]
    assert parse_selection("2,2,3", 10) == [2, 3]
    assert parse_selection("all", 3) == [1, 2, 3]
    for bad in ("0", "11", "a", " , "):
        with pytest.raises(ValueError):
            parse_selection(bad, 10)
