"""Phase 1b: heuristic 1-10 score for "can this be demoed in an afternoon?".

Three weighted signals:
  velocity      quick-win deliverables (landing pages, calculators, small APIs)
  independence  absence of heavy dependencies (payments, databases, auth, mobile)
  clarity       a description that is specific and itemised but not sprawling
Gigs that are not web builds at all (SEO, logo design, data entry) are damped.
"""

from __future__ import annotations

import re

from .models import Gig, ScoredGig

WEIGHTS = {"velocity": 0.40, "independence": 0.35, "clarity": 0.25}

# Regex alternatives -> weight. Each group counts at most once per gig.
VELOCITY_TERMS = {
    r"landing ?pages?": 3.0,
    r"one[- ]page|single[- ]page": 3.0,
    r"calculators?": 3.0,
    r"converters?": 2.5,
    r"portfolio": 2.5,
    r"static (?:site|website|page)s?": 2.5,
    r"fastapi": 2.5,
    r"tailwind": 2.0,
    r"flask": 2.0,
    r"quiz|countdown|to-?do": 2.0,
    r"contact forms?": 2.0,
    r"api endpoints?": 2.0,
    r"forms?": 1.0,
    r"html5?": 1.0,
    r"rest(?:ful)? api": 1.5,
    r"mock-?ups?|prototypes?": 1.5,
    r"generators?|surveys?": 1.5,
    r"simple|basic": 1.5,
    r"small|quick|easy": 1.0,
    r"css3?|bootstrap": 0.5,
    r"dashboard": 1.0,
}
DEPENDENCY_TERMS = {
    r"payments?|payment gateway|stripe|paypal|razorpay|afterpay|klarna|checkout": 3.0,
    r"android|ios|flutter|react native|mobile app": 3.0,
    r"blockchain|smart contracts?|web3|nft|solidity": 3.0,
    r"erp|salesforce|sap|magento": 3.0,
    r"oauth|authentication|user accounts?|jwt|log ?in|sign ?up|registration": 2.5,
    r"woocommerce|e-?commerce|marketplace|saas|multi-?vendor": 2.5,
    r"machine learning|deep learning|computer vision|train(?:ing)? (?:a |the )?models?": 2.5,
    r"database|mysql|postgres(?:ql)?|mongo(?:db)?|firebase|supabase|sql": 2.0,
    r"scrap(?:e|er|ers|ing)|crawlers?|bots?": 2.0,
    r"real-?time|websockets?|live chat|video calls?|streaming": 2.0,
    r"wordpress|shopify|wix|webflow|squarespace|crm": 2.0,
    r"migrat(?:e|ion)|integrat(?:e|ion)s?|third-party": 1.5,
    r"full-?time|long-?term|ongoing|retainer": 1.5,
    r"admin panel|multi-?tenant|roles and permissions": 1.5,
}
WEB_TERMS = (
    r"websites?|web ?apps?|web|landing|pages?|html|css|javascript|api|python|fastapi|flask|django"
    r"|front-?end|dashboard|calculators?|forms?|tools?|php|tailwind|react|vue|ui"
)
NON_DEMO_TERMS = (
    r"seo|logo|articles?|copywriting|data entry|video editing|translat(?:e|ion)|social media"
    r"|voice-?over|illustrations?|photoshop|thumbnails?|lead generation|virtual assistant|reviews"
    r"|advertising|ads|ad sets?|ads manager|campaigns?|pixels?|funnels?"
)

_LIST_ITEM = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)])\s+\S", re.M)
_REQUIREMENT = re.compile(r"\b(?:must|should|need(?:s|ed)?|requires?|deliverables?)\b", re.I)


def _compile(terms: dict[str, float]) -> list[tuple[re.Pattern[str], float]]:
    return [(re.compile(rf"\b(?:{pattern})\b", re.I), weight) for pattern, weight in terms.items()]


_VELOCITY = _compile(VELOCITY_TERMS)
_DEPENDENCIES = _compile(DEPENDENCY_TERMS)
_WEB = re.compile(rf"\b(?:{WEB_TERMS})\b", re.I)
_NON_DEMO = re.compile(rf"\b(?:{NON_DEMO_TERMS})\b", re.I)


def _hits(patterns: list[tuple[re.Pattern[str], float]], text: str) -> list[tuple[str, float]]:
    found = []
    for pattern, weight in patterns:
        match = pattern.search(text)
        if match:
            found.append((match.group(0).lower(), weight))
    return found


def _budget_adjustment(gig: Gig) -> float:
    if gig.pay_type == "hourly":
        return -0.1
    top = gig.budget_max_usd or gig.budget_min_usd
    if top is None:
        return 0.0
    if top <= 150:
        return 0.15
    if top <= 400:
        return 0.05
    if top >= 1500:
        return -0.2
    return 0.0


def _clarity(description: str) -> float:
    length = len(description.strip())
    if length < 80:
        base = 0.2
    elif length < 200:
        base = 0.55
    elif length <= 1500:
        base = 0.85
    elif length <= 3000:
        base = 0.6
    else:
        base = 0.35
    if _LIST_ITEM.search(description):
        base += 0.1
    if len(_REQUIREMENT.findall(description)) >= 2:
        base += 0.05
    return min(1.0, base)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_gig(gig: Gig) -> ScoredGig:
    text = f"{gig.title}\n{gig.description}\n{' '.join(gig.skills)}"
    speed = _hits(_VELOCITY, text)
    deps = _hits(_DEPENDENCIES, text)

    breakdown = {
        "velocity": _clamp(min(1.0, sum(w for _, w in speed) / 7) + _budget_adjustment(gig)),
        "independence": _clamp(1 - sum(w for _, w in deps) / 6),
        "clarity": _clarity(gig.description),
    }
    total = sum(WEIGHTS[key] * value for key, value in breakdown.items())

    web = {m.lower() for m in _WEB.findall(text)}
    non_demo = {m.lower() for m in _NON_DEMO.findall(text)}
    signals = [f"+{term}" for term, _ in speed] + [f"-{term}" for term, _ in deps]
    if len(web) <= len(non_demo):
        total *= 0.35
        signals.append("not a web build")

    return ScoredGig(
        gig=gig,
        score=round(1 + 9 * total, 1),
        breakdown={key: round(value, 2) for key, value in breakdown.items()},
        signals=signals,
    )


def rank_gigs(gigs: list[Gig]) -> list[ScoredGig]:
    scored = [score_gig(gig) for gig in gigs if gig.title.strip()]
    return sorted(scored, key=lambda s: (s.score, s.breakdown["independence"]), reverse=True)


def _cell(text: str, limit: int) -> str:
    flat = " ".join(text.split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def render_table(ranked: list[ScoredGig]) -> str:
    lines = [
        "| Rank | Title | Platform | Brief | Suitability Score |",
        "|---:|---|---|---|---:|",
    ]
    for rank, item in enumerate(ranked, 1):
        gig = item.gig
        lines.append(
            f"| {rank} | {_cell(gig.title, 60)} | {gig.platform} | {_cell(gig.description, 110)} | {item.score:.1f}/10 |"
        )
    return "\n".join(lines)
