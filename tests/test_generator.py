import asyncio
import json
from dataclasses import replace

import httpx

from fdm.config import Settings
from fdm.generator import generate_project
from fdm.llm import OllamaCloud
from fdm.models import Gig, ScoredGig
from tests.test_validate import GOOD_HTML, GOOD_PY

BLUEPRINT = {
    "app_title": "Bakery Adder", "tagline": "Adds numbers", "demo_summary": "Shows a calculator.",
    "routes": [{"method": "GET", "path": "/", "purpose": "page", "response": "html", "template_vars": ["total"]}],
    "template_variables": [{"name": "total", "type": "float", "description": "sum"}],
    "ui_sections": ["Hero", "Calculator"], "sample_data": "none",
}
GOOD_REPLY = f"```python\n{GOOD_PY}\n```\n```html\n{GOOD_HTML}\n```"
BAD_REPLY = GOOD_REPLY.replace("from pathlib import Path", "import os\nfrom pathlib import Path")
SCORED = ScoredGig(
    gig=Gig(platform="Freelancer.com", gig_id="123", title="Bakery calculator", description="Add two numbers.",
            url="https://example.com/123"),
    score=9.0, breakdown={}, signals=[],
)


def _settings(tmp_path) -> Settings:
    return replace(Settings.load(env_file=tmp_path / "missing.env"), output_dir=tmp_path, max_repairs=1)


def _generate(tmp_path, developer_replies: list[str]):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if "Agent A" in messages[0]["content"]:
            content = json.dumps(BLUEPRINT)
            seen.append("architect")
        else:
            content = developer_replies[min(len(seen) - 1, len(developer_replies) - 1)]
            seen.append("repair" if len(messages) > 2 else "developer")
        return httpx.Response(200, json={"message": {"role": "assistant", "content": content}})

    async def go():
        async with OllamaCloud("key", ["m1"], transport=httpx.MockTransport(handler), backoff=0) as llm:
            return await generate_project(llm, SCORED, _settings(tmp_path))

    return asyncio.run(go()), seen


def test_repair_loop_fixes_rejected_code(tmp_path):
    project, seen = _generate(tmp_path, [BAD_REPLY, GOOD_REPLY])
    assert seen == ["architect", "developer", "repair"]
    assert not project.used_fallback
    files = {p.relative_to(project.path).as_posix() for p in project.path.rglob("*") if p.is_file()}
    assert {"index.py", "templates/index.html", "requirements.txt", "vercel.json", ".vercelignore",
            ".fdm/gig.json"} <= files
    assert "import os" not in (project.path / "index.py").read_text(encoding="utf-8")
    assert "tailwindcss" in (project.path / "templates" / "index.html").read_text(encoding="utf-8")


def test_unfixable_code_ships_fallback_app(tmp_path):
    project, seen = _generate(tmp_path, [BAD_REPLY])
    assert seen == ["architect", "developer", "repair"]
    assert project.used_fallback
    assert "Bakery Adder" in (project.path / "index.py").read_text(encoding="utf-8")
    meta = json.loads((project.path / ".fdm" / "gig.json").read_text(encoding="utf-8"))
    assert meta["used_fallback"] is True and len(meta["notes"]) == 2
