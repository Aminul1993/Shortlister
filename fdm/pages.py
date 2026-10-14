"""Demo page helpers: stable demo ids, Tailwind injection and the deterministic fallback page."""

from __future__ import annotations

import hashlib
import re

from jinja2 import Environment

from .models import Blueprint, Gig

TAILWIND_CDN = "https://cdn.tailwindcss.com"


def make_demo_id(gig: Gig) -> str:
    """Lowercase, hyphenated and unique per gig; used as the screenshot's file name."""
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


FALLBACK_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ demo.title }}</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-50 text-slate-800 antialiased">
  <header class="bg-gradient-to-br from-indigo-600 to-sky-500 text-white">
    <div class="mx-auto max-w-5xl px-6 py-16">
      <p class="text-sm font-semibold uppercase tracking-widest text-indigo-100">Proof-of-concept demo</p>
      <h1 class="mt-3 text-4xl font-bold sm:text-5xl">{{ demo.title }}</h1>
      {% if demo.tagline %}<p class="mt-4 max-w-2xl text-lg text-indigo-50">{{ demo.tagline }}</p>{% endif %}
    </div>
  </header>

  <main class="mx-auto max-w-5xl space-y-12 px-6 py-12">
    {% if demo.summary %}
    <section>
      <h2 class="text-2xl font-semibold">What this demo covers</h2>
      <p class="mt-3 leading-relaxed text-slate-600">{{ demo.summary }}</p>
    </section>
    {% endif %}

    {% if demo.sections %}
    <section>
      <h2 class="text-2xl font-semibold">Planned scope</h2>
      <ol class="mt-4 grid gap-4 sm:grid-cols-2">
        {% for section in demo.sections %}
        <li class="flex gap-3 rounded-xl bg-white p-5 shadow-sm ring-1 ring-slate-200">
          <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-indigo-100 font-semibold text-indigo-700">{{ loop.index }}</span>
          <span class="pt-1">{{ section }}</span>
        </li>
        {% endfor %}
      </ol>
    </section>
    {% endif %}

    {% if demo.sample_data %}
    <section class="rounded-2xl bg-white p-8 shadow-sm ring-1 ring-slate-200">
      <h2 class="text-2xl font-semibold">Sample content</h2>
      <p class="mt-3 leading-relaxed text-slate-600">{{ demo.sample_data }}</p>
    </section>
    {% endif %}
  </main>

  <footer class="border-t border-slate-200 py-8 text-center text-sm text-slate-500">
    Proof-of-concept demo{% if demo.skills %} &middot; {{ demo.skills | join(', ') }}{% endif %}
  </footer>
</body>
</html>
"""

_FALLBACK = Environment(autoescape=True).from_string(FALLBACK_TEMPLATE)


def fallback_page(gig: Gig, blueprint: Blueprint) -> str:
    """A safe, deterministic page used when the agents cannot produce one that passes the checks."""
    return _FALLBACK.render(demo={
        "title": blueprint.app_title,
        "tagline": blueprint.tagline,
        "summary": blueprint.demo_summary,
        "sections": blueprint.ui_sections,
        "sample_data": blueprint.sample_data,
        "skills": gig.skills[:6],
    })
