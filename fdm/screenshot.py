"""Render a generated page in headless Chromium and capture a full-page PNG.

Each page is written to a temporary .html file, opened from disk and screenshotted. The temporary
file is deleted straight afterwards, so the PNG is the only thing that is kept. The page may only
reach the Tailwind and Google Fonts CDNs. Anything else it tries to load is blocked and reported,
together with JavaScript errors and blank pages, so the Developer can be asked to fix it.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import Browser, BrowserContext, Playwright, Request, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

ALLOWED_HOSTS = frozenset({"cdn.tailwindcss.com", "fonts.googleapis.com", "fonts.gstatic.com"})
VIEWPORT = {"width": 1280, "height": 800}
LOAD_TIMEOUT_MS = 30_000
IDLE_TIMEOUT_MS = 10_000
SETTLE_MS = 500  # lets the Tailwind CDN finish generating styles for the final DOM
RENDER_TIMEOUT = 90  # seconds; also stops pages whose JavaScript never yields
MAX_LISTED = 5
BLANK = "the page renders no visible text"


class BrowserError(RuntimeError):
    pass


@dataclass
class Shot:
    png: bytes | None
    problems: list[str] = field(default_factory=list)  # for the Developer to fix
    warnings: list[str] = field(default_factory=list)  # environment issues, e.g. the CDN was unreachable

    @property
    def usable(self) -> bool:
        """A screenshot exists and shows a page with content, even if some problems remain."""
        return self.png is not None and not any(p.startswith(BLANK) for p in self.problems)


def _listed(items: list[str]) -> str:
    unique = list(dict.fromkeys(items))
    more = f" and {len(unique) - MAX_LISTED} more" if len(unique) > MAX_LISTED else ""
    return ", ".join(unique[:MAX_LISTED]) + more


class Renderer:
    """One headless Chromium shared by every render. Use as `async with Renderer() as renderer`."""

    def __init__(self, *, concurrency: int = 4):
        self._slots = asyncio.Semaphore(concurrency)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def __aenter__(self) -> Renderer:
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch()
        except PlaywrightError as exc:
            await self._playwright.stop()
            reason = str(exc).strip().splitlines()[0]
            raise BrowserError(f"could not start headless Chromium ({reason}). "
                               "Install it with: python -m playwright install chromium") from exc
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def render(self, html: str) -> Shot:
        assert self._browser is not None, "use Renderer as an async context manager"
        async with self._slots:
            with tempfile.TemporaryDirectory(prefix="fdm-page-", ignore_cleanup_errors=True) as tmp:
                page_file = Path(tmp) / "index.html"
                page_file.write_text(html, encoding="utf-8")
                context = await self._browser.new_context(viewport=VIEWPORT, service_workers="block")
                try:
                    return await asyncio.wait_for(self._capture(context, page_file), RENDER_TIMEOUT)
                except TimeoutError:
                    return Shot(None, [f"the page did not finish rendering within {RENDER_TIMEOUT}s "
                                       "(an endless loop in inline JavaScript?)"])
                except PlaywrightError as exc:  # e.g. the page crashed the renderer process
                    return Shot(None, [f"the browser failed while rendering the page: {str(exc).splitlines()[0]}"])
                finally:
                    await context.close()

    async def _capture(self, context: BrowserContext, page_file: Path) -> Shot:
        folder = page_file.parent.as_uri() + "/"
        blocked: list[str] = []
        missing: list[str] = []
        unreachable: list[str] = []
        errors: list[str] = []

        async def gate(route: Route) -> None:
            url = urlsplit(route.request.url)
            if url.scheme in {"http", "https", "ws", "wss"} and url.hostname not in ALLOWED_HOSTS:
                blocked.append(route.request.url[:120])
                await route.abort("blockedbyclient")
            else:
                await route.continue_()

        def failed(request: Request) -> None:
            if request.url.startswith(folder):
                missing.append(unquote(request.url.removeprefix(folder)))
            elif urlsplit(request.url).hostname in ALLOWED_HOSTS:
                unreachable.append(f"{request.url[:120]} ({request.failure})")

        await context.route("**/*", gate)
        page = await context.new_page()
        page.set_default_timeout(LOAD_TIMEOUT_MS)
        page.on("pageerror", lambda error: errors.append(" ".join(str(error).split())[:300]))
        page.on("requestfailed", failed)

        problems: list[str] = []
        try:
            await page.goto(page_file.as_uri(), wait_until="load", timeout=LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            problems.append(f"the page did not finish loading within {LOAD_TIMEOUT_MS // 1000}s")
        try:
            await page.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass  # e.g. a polling script; the page is screenshotted as it is
        await page.evaluate("document.fonts.ready.then(() => true)")
        await page.wait_for_timeout(SETTLE_MS)
        text_length = await page.evaluate("document.body ? document.body.innerText.trim().length : 0")
        png = await page.screenshot(full_page=True, type="png")

        if blocked:
            problems.append(f"the page tried to load {_listed(blocked)}; only cdn.tailwindcss.com and Google Fonts "
                            "are reachable, so draw images with inline SVG, CSS or data: URIs")
        if missing:
            problems.append(f"the page references local files that do not exist: {_listed(missing)}; "
                            "inline everything in the one HTML file")
        if errors:
            problems.append(f"inline JavaScript threw an error: {_listed(errors)}")
        if not text_length:
            problems.append(f"{BLANK}; put the content in the HTML instead of building it with JavaScript")
        warnings = [f"Could not load {_listed(unreachable)}; the screenshot may be missing styles or fonts"] \
            if unreachable else []
        return Shot(png, problems, warnings)
