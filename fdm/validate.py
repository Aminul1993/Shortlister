"""Guardrails for LLM-generated pages that can be checked without a browser.

Problems found while rendering (blocked requests, JavaScript errors, a blank page) are
reported by fdm.screenshot; both lists go back to the Developer for repair.
"""

from __future__ import annotations

import re

_TEMPLATE_SYNTAX = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)


def page_problems(html: str) -> list[str]:
    """Return problems that would make the screenshot wrong (empty means the page can be rendered)."""
    problems = []
    lowered = html.lower()
    if "<html" not in lowered or "<body" not in lowered:
        problems.append("the reply must be one complete HTML document with <html> and <body>")
    elif "</html>" not in lowered:
        problems.append("the page is cut off before </html>; reply with the complete page and keep it shorter")
    if match := _TEMPLATE_SYNTAX.search(html):
        snippet = " ".join(match.group(0).split())[:80]
        problems.append(f"the page contains template syntax ({snippet}); it is never rendered by a template "
                        "engine, so write the final sample content directly")
    return problems
