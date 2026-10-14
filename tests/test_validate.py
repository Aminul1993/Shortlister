import pytest

import json
from dataclasses import replace

from fdm.agents import default_blueprint, parse_blueprint, fit_bid, parse_page, parse_pitch
from fdm.models import Gig
from fdm.pages import ensure_tailwind, fallback_page, make_demo_id
from fdm.validate import page_problems

GOOD_HTML = (
    "<!DOCTYPE html><html><head><title>Adder</title></head><body>"
    "<h1>Bakery Adder</h1><p>2 + 3 = <strong>5</strong></p>"
    "</body></html>"
)
GIG = Gig(platform="Freelancer.com", gig_id="40767032", title="Company Information Website Build!",
          description="Need a 5 page website with a contact form.", url="https://example.com/g", skills=["PHP"])


def test_good_page_passes():
    assert page_problems(GOOD_HTML) == []


def test_ensure_tailwind_injects_into_head():
    html = ensure_tailwind(GOOD_HTML)
    assert html.index("<head>") < html.index("cdn.tailwindcss.com")
    assert ensure_tailwind(html) == html


@pytest.mark.parametrize(("html", "expected"), [
    ("<p>just a fragment</p>", "complete HTML document"),
    (GOOD_HTML.removesuffix("</body></html>"), "cut off before </html>"),
    (GOOD_HTML.replace("<strong>5</strong>", "<strong>{{ total }}</strong>"), "template syntax ({{ total }})"),
    (GOOD_HTML.replace("<h1>", "{% for x in items %}<h1>"), "template syntax"),
])
def test_page_problems(html, expected):
    problems = page_problems(html)
    assert len(problems) == 1 and expected in problems[0]


def test_css_and_js_braces_are_not_template_syntax():
    html = GOOD_HTML.replace("</head>", "<style>@media (min-width: 1px) { a { color: red }}</style>"
                                        "<script>const o = {a: {b: 1}};</script></head>")
    assert page_problems(html) == []


def test_fallback_page_passes_for_default_and_custom_blueprints():
    custom = parse_blueprint('{"app_title": "Quote <Builder>", "tagline": "t", "demo_summary": "s", '
                             '"ui_sections": ["Hero", "Form"], "sample_data": "3 quotes"}')
    for blueprint in (default_blueprint(GIG), custom):
        html = fallback_page(GIG, blueprint)
        assert page_problems(html) == []
        assert "Proof-of-concept demo" in html
    assert "Quote &lt;Builder&gt;" in fallback_page(GIG, custom)  # untrusted text is escaped


def test_parse_blueprint_normalises_fields():
    blueprint = parse_blueprint('{"app_title": "  Demo  ", "ui_sections": ["Hero", "", "Calculator"], "extra": 1}')
    assert blueprint.app_title == "Demo"
    assert blueprint.ui_sections == ["Hero", "Calculator"]
    with pytest.raises(ValueError):
        parse_blueprint('{"tagline": "no title"}')


def test_parse_page_extracts_the_html_block():
    reply = f"Sure!\n```html\n{GOOD_HTML}\n```\n"
    assert parse_page(reply) == GOOD_HTML + "\n"


def test_parse_page_accepts_an_unclosed_fence():
    page = parse_page(f"```html\n{GOOD_HTML}")
    assert page.startswith("<!DOCTYPE html>") and page_problems(page) == []
    with pytest.raises(ValueError):
        parse_page("I can't help with that.")


def test_demo_id_is_a_safe_file_name():
    assert make_demo_id(GIG) == "company-information-website-build-40767032"


PITCH_TEXT = ("You need a 5-page company site with a contact form that works on phones.\n\n"
              "The attached screenshot shows your home page with the services grid and the contact form filled in.")


def test_parse_pitch_cleans_the_message():
    reply = {"message": f"**Hello**  \n\n\n\n{PITCH_TEXT}\n\nA quick mock‑up.", "bid_usd": 140.5,
             "delivery_days": 3.6}
    pitch = parse_pitch(json.dumps(reply))
    assert pitch.message == f"Hello\n\n{PITCH_TEXT}\n\nA quick mock-up."
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
    assert fit_bid(120, GIG) is None  # no budget to anchor a price to
    assert fit_bid(None, gig) is None
