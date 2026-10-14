"""Write demo projects to disk in the Vercel-ready layout, plus the deterministic fallback app.

output/<project_id>/
  index.py               FastAPI entrypoint (Vercel Python runtime picks up `app`)
  vercel.json            @vercel/python build + catch-all route
  requirements.txt       fastapi, jinja2, python-multipart
  templates/index.html   single self-contained Jinja2 template (Tailwind CDN)
  .vercelignore          keeps .fdm/ metadata and caches out of the upload
  .fdm/gig.json          source gig, score, blueprint and build notes
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from pathlib import Path
from pprint import pformat

from .models import AppCode, Blueprint, Gig

TAILWIND_CDN = "https://cdn.tailwindcss.com"
APP_REQUIREMENTS = "fastapi>=0.115\njinja2>=3.1\npython-multipart>=0.0.9\n"
VERCELIGNORE = ".fdm/\n__pycache__/\n*.pyc\n.venv/\n"
VERCEL_CONFIG = {
    "version": 2,
    "builds": [{"src": "index.py", "use": "@vercel/python", "config": {"includeFiles": "templates/**"}}],
    "routes": [{"src": "/(.*)", "dest": "index.py"}],
}


def make_project_id(gig: Gig) -> str:
    """Lowercase, hyphenated and unique per gig, which also makes it a valid Vercel project name."""
    slug = re.sub(r"[^a-z0-9]+", "-", gig.title.lower()).strip("-")[:40].strip("-") or "demo"
    suffix = re.sub(r"[^a-z0-9]", "", gig.gig_id.lower())[-8:] or hashlib.sha1(gig.url.encode()).hexdigest()[:8]
    return f"{slug}-{suffix}"


def ensure_tailwind(html: str) -> str:
    if "tailwindcss" in html.lower():
        return html
    tag = f'<script src="{TAILWIND_CDN}"></script>'
    head = re.search(r"<head[^>]*>", html, re.I)
    if head:
        return f"{html[: head.end()]}\n  {tag}{html[head.end():]}"
    return f"{tag}\n{html}"


def write_app_files(path: Path, code: AppCode) -> None:
    (path / "templates").mkdir(parents=True, exist_ok=True)
    (path / "index.py").write_text(code.index_py, encoding="utf-8")
    (path / "templates" / "index.html").write_text(code.index_html, encoding="utf-8")


def write_vercel_config(path: Path) -> None:
    (path / "vercel.json").write_text(json.dumps(VERCEL_CONFIG, indent=2) + "\n", encoding="utf-8")


def write_project(path: Path, code: AppCode, metadata: dict) -> None:
    write_app_files(path, code)
    write_vercel_config(path)
    (path / "requirements.txt").write_text(APP_REQUIREMENTS, encoding="utf-8")
    (path / ".vercelignore").write_text(VERCELIGNORE, encoding="utf-8")
    (path / ".fdm").mkdir(exist_ok=True)
    (path / ".fdm" / "gig.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")


FALLBACK_INDEX_PY = '''\
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
app = FastAPI(title=__TITLE__)

DEMO = __DEMO__


def render(request: Request, message: str = "", submitted: str = ""):
    context = {"demo": DEMO, "message": message, "submitted": submitted}
    return templates.TemplateResponse(request, "index.html", context)


@app.get("/")
async def home(request: Request):
    return render(request)


@app.post("/")
async def preview(request: Request, requirement: str = Form("")):
    requirement = " ".join(requirement.split())[:280]
    if not requirement:
        return render(request, message="Type a requirement to see it added to the scope preview.")
    return render(request, message="Added to the scope preview below.", submitted=requirement)


@app.get("/health")
async def health():
    return {"status": "ok"}
'''

FALLBACK_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ demo.title if demo and demo.title else 'Demo' }}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-50 text-slate-800 antialiased">
  <header class="bg-gradient-to-br from-indigo-600 to-sky-500 text-white">
    <div class="mx-auto max-w-5xl px-6 py-16">
      <p class="text-sm font-semibold uppercase tracking-widest text-indigo-100">Proof-of-concept demo</p>
      <h1 class="mt-3 text-4xl font-bold sm:text-5xl">{{ demo.title if demo and demo.title else 'Demo' }}</h1>
      {% if demo and demo.tagline %}<p class="mt-4 max-w-2xl text-lg text-indigo-50">{{ demo.tagline }}</p>{% endif %}
      <a href="#try" class="mt-8 inline-block rounded-lg bg-white px-5 py-3 font-semibold text-indigo-700 shadow hover:bg-indigo-50">Try it</a>
    </div>
  </header>

  <main class="mx-auto max-w-5xl space-y-12 px-6 py-12">
    {% if demo and demo.summary %}
    <section>
      <h2 class="text-2xl font-semibold">What this demo covers</h2>
      <p class="mt-3 leading-relaxed text-slate-600">{{ demo.summary }}</p>
    </section>
    {% endif %}

    {% if demo and demo.sections %}
    <section>
      <h2 class="text-2xl font-semibold">Planned scope</h2>
      <ol class="mt-4 grid gap-4 sm:grid-cols-2">
        {% for section in demo.sections or [] %}
        <li class="flex gap-3 rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-indigo-100 font-semibold text-indigo-700">{{ loop.index }}</span>
          <span class="pt-1">{{ section }}</span>
        </li>
        {% endfor %}
        {% if submitted %}
        <li class="flex gap-3 rounded-xl bg-emerald-50 p-5 shadow-sm ring-1 ring-emerald-200">
          <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-200 font-semibold text-emerald-800">+</span>
          <span class="pt-1">{{ submitted }}</span>
        </li>
        {% endif %}
      </ol>
    </section>
    {% endif %}

    {% if demo and demo.routes %}
    <section>
      <h2 class="text-2xl font-semibold">Planned endpoints</h2>
      <div class="mt-4 overflow-x-auto rounded-xl bg-white shadow-sm ring-1 ring-slate-200">
        <table class="w-full text-left text-sm">
          <thead class="bg-slate-100 text-slate-600">
            <tr><th class="px-4 py-3">Method</th><th class="px-4 py-3">Path</th><th class="px-4 py-3">Purpose</th></tr>
          </thead>
          <tbody>
            {% for route in demo.routes or [] %}
            <tr class="border-t border-slate-100">
              <td class="px-4 py-3 font-mono text-indigo-700">{{ route.method if route.method else '' }}</td>
              <td class="px-4 py-3 font-mono">{{ route.path if route.path else '' }}</td>
              <td class="px-4 py-3 text-slate-600">{{ route.purpose if route.purpose else '' }}</td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
    </section>
    {% endif %}

    <section id="try" class="rounded-2xl bg-white p-8 shadow-sm ring-1 ring-slate-200">
      <h2 class="text-2xl font-semibold">Shape the scope</h2>
      <p class="mt-2 text-slate-600">Add a requirement and see how it slots into the plan.</p>
      {% if message %}
      <p class="mt-4 rounded-lg bg-indigo-50 px-4 py-3 text-indigo-800">{{ message }}</p>
      {% endif %}
      <form method="post" action="/" class="mt-6 flex flex-col gap-3 sm:flex-row">
        <input name="requirement" maxlength="280" placeholder="e.g. Add a pricing table"
               class="flex-1 rounded-lg border border-slate-300 px-4 py-3 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-200">
        <button class="rounded-lg bg-indigo-600 px-6 py-3 font-semibold text-white hover:bg-indigo-700">Add</button>
      </form>
    </section>
  </main>

  <footer class="border-t border-slate-200 py-8 text-center text-sm text-slate-500">
    Proof-of-concept demo{% if demo and demo.skills %} &middot; {{ demo.skills | join(', ') }}{% endif %}
  </footer>
</body>
</html>
"""


def fallback_app(gig: Gig, blueprint: Blueprint) -> AppCode:
    """A safe, deterministic demo used when the agents cannot produce code that passes validation."""
    demo = {
        "title": blueprint.app_title,
        "tagline": blueprint.tagline,
        "summary": blueprint.demo_summary,
        "sections": blueprint.ui_sections,
        "routes": [{k: r[k] for k in ("method", "path", "purpose")} for r in blueprint.routes],
        "skills": gig.skills[:6],
    }
    index_py = (
        FALLBACK_INDEX_PY
        .replace("__TITLE__", repr(blueprint.app_title))
        .replace("__DEMO__", pformat(demo, width=100, sort_dicts=False))
    )
    return AppCode(index_py=index_py, index_html=FALLBACK_INDEX_HTML)


def project_metadata(gig: Gig, score: float, blueprint: Blueprint, model: str | None,
                     used_fallback: bool, notes: list[str]) -> dict:
    return {
        "gig": asdict(gig),
        "score": score,
        "blueprint": asdict(blueprint),
        "model": model,
        "used_fallback": used_fallback,
        "notes": notes,
    }
