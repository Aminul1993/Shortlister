"""Phase 1a: pull open gigs from public freelance feeds with Playwright's HTTP client.

Only public, unauthenticated endpoints are used: the Freelancer.com projects API
and any RSS/Atom feeds listed in FDM_RSS_FEEDS. No browser is launched, so
`playwright install` is not needed for this step.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlencode, urlparse

from playwright.async_api import APIRequestContext, async_playwright

from .config import Settings
from .models import Gig

log = logging.getLogger(__name__)

USER_AGENT = "FreelanceDemoMachine/0.1 (+python playwright)"
FREELANCER_API = "https://www.freelancer.com/api/projects/0.1/projects/active/"
# Freelancer.com skill ids: Python, HTML, CSS, JavaScript, FastAPI, Flask,
# Landing Pages, Web Development, Website Design.
FREELANCER_SKILLS = (13, 335, 77, 9, 2688, 1094, 482, 1031, 17)
ATOM = "{http://www.w3.org/2005/Atom}"

_BREAKS = re.compile(r"<(?:br|/p|/div|/h\d)[^>]*>", re.I)
_LIST_ITEMS = re.compile(r"<li[^>]*>", re.I)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")


def _html_to_text(markup: str) -> str:
    text = _LIST_ITEMS.sub("\n• ", _BREAKS.sub("\n", markup or ""))
    text = unescape(_TAGS.sub(" ", text))
    lines = (_SPACES.sub(" ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_freelancer_projects(payload: dict) -> list[Gig]:
    projects = (payload.get("result") or {}).get("projects") or []
    gigs: list[Gig] = []
    for project in projects:
        if not project.get("id") or not project.get("title"):
            continue
        budget = project.get("budget") or {}
        rate = (project.get("currency") or {}).get("exchange_rate") or 1.0

        def usd(value: object) -> float | None:
            return round(float(value) * rate, 2) if isinstance(value, (int, float)) else None

        submitted = project.get("time_submitted")
        gigs.append(
            Gig(
                platform="Freelancer.com",
                gig_id=str(project["id"]),
                title=project["title"].strip(),
                description=(project.get("description") or project.get("preview_description") or "").strip(),
                url=f"https://www.freelancer.com/projects/{project.get('seo_url') or project['id']}",
                skills=[job["name"] for job in project.get("jobs") or [] if job.get("name")],
                budget_min_usd=usd(budget.get("minimum")),
                budget_max_usd=usd(budget.get("maximum")),
                pay_type=project.get("type") or "unknown",
                posted_at=datetime.fromtimestamp(submitted, tz=timezone.utc) if submitted else None,
            )
        )
    return gigs


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _feed_gig(platform: str, guid: str, title: str | None, body: str | None, link: str, posted: str | None) -> Gig:
    return Gig(
        platform=platform,
        gig_id=hashlib.sha1((guid or link).encode()).hexdigest()[:12],
        title=_html_to_text(title or ""),
        description=_html_to_text(body or ""),
        url=link,
        posted_at=_parse_date(posted),
    )


def parse_feed(xml_text: str, feed_url: str) -> list[Gig]:
    """Parse an RSS 2.0 or Atom feed into gigs. The feed host becomes the platform name."""
    root = ET.fromstring(xml_text)
    platform = (urlparse(feed_url).hostname or feed_url).removeprefix("www.")
    gigs: list[Gig] = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        gigs.append(
            _feed_gig(platform, item.findtext("guid") or link, item.findtext("title"),
                      item.findtext("description"), link, item.findtext("pubDate"))
        )
    for entry in root.iter(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        body = entry.findtext(f"{ATOM}content") or entry.findtext(f"{ATOM}summary")
        gigs.append(
            _feed_gig(platform, entry.findtext(f"{ATOM}id") or link, entry.findtext(f"{ATOM}title"),
                      body, link, entry.findtext(f"{ATOM}updated"))
        )
    return [gig for gig in gigs if gig.title and gig.url]


async def fetch_freelancer(request: APIRequestContext, query: str, limit: int) -> list[Gig]:
    params: list[tuple[str, str | int]] = [
        ("limit", limit),
        ("full_description", "true"),
        ("job_details", "true"),
        ("languages[]", "en"),
        *(("jobs[]", skill) for skill in FREELANCER_SKILLS),
    ]
    if query:
        params.append(("query", query))
    response = await request.get(f"{FREELANCER_API}?{urlencode(params)}", timeout=30_000)
    if not response.ok:
        raise RuntimeError(f"Freelancer.com returned HTTP {response.status} for query {query or '<latest>'!r}")
    return parse_freelancer_projects(await response.json())


async def fetch_feed(request: APIRequestContext, url: str) -> list[Gig]:
    response = await request.get(url, timeout=30_000)
    if not response.ok:
        raise RuntimeError(f"feed {url} returned HTTP {response.status}")
    return parse_feed(await response.text(), url)


async def fetch_gigs(settings: Settings) -> list[Gig]:
    """Query every configured source concurrently and de-duplicate the results."""
    async with async_playwright() as pw:
        request = await pw.request.new_context(user_agent=USER_AGENT)
        try:
            queries = ("", *settings.search_queries)
            jobs = [fetch_freelancer(request, q, settings.results_per_query) for q in queries]
            jobs += [fetch_feed(request, url) for url in settings.rss_feeds]
            batches = await asyncio.gather(*jobs, return_exceptions=True)
        finally:
            await request.dispose()

    unique: dict[tuple[str, str], Gig] = {}
    for batch in batches:
        if isinstance(batch, BaseException):
            log.warning("Source failed: %s", batch)
            continue
        for gig in batch:
            unique.setdefault((gig.platform, gig.gig_id), gig)
    log.info("Fetched %d unique gigs from %d request(s)", len(unique), len(batches))
    return list(unique.values())
