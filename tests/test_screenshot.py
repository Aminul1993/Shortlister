"""Renders real pages in headless Chromium; skipped when `python -m playwright install chromium` hasn't been run."""

import asyncio
import tempfile

import pytest

from fdm.screenshot import BrowserError, Renderer
from tests.test_validate import GOOD_HTML


def render_all(*pages: str):
    async def go():
        async with Renderer() as renderer:
            return [await renderer.render(page) for page in pages]

    try:
        return asyncio.run(go())
    except BrowserError as exc:
        pytest.skip(str(exc))


def test_renders_a_png_and_deletes_the_temporary_page(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    [shot] = render_all(GOOD_HTML)
    assert shot.png.startswith(b"\x89PNG") and shot.problems == [] and shot.usable
    assert list(tmp_path.iterdir()) == []


def test_reports_blocked_requests_missing_files_and_script_errors():
    page = GOOD_HTML.replace("</body>", '<img src="https://example.com/hero.jpg"><img src="img/logo.png">'
                                        "<script>notDefined()</script></body>")
    [shot] = render_all(page)
    problems = "\n".join(shot.problems)
    assert "tried to load https://example.com/hero.jpg" in problems
    assert "local files that do not exist: img/logo.png" in problems
    assert "notDefined is not defined" in problems
    assert shot.usable  # still worth keeping if repairs run out


def test_blank_page_is_not_usable():
    [shot] = render_all("<!DOCTYPE html><html><body><div id=app></div></body></html>")
    assert shot.png is not None and not shot.usable
