"""Guardrails for LLM-generated apps: normalisation, static policy checks, then a live smoke test.

The static checks run first and reject anything outside a small import allowlist or that
calls eval/exec/open & co. Only code that passes them is imported by the smoke test,
which runs in a child process and hits every parameter-free route with FastAPI's TestClient.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from jinja2 import Environment, TemplateSyntaxError

from .models import AppCode
from .scaffold import ensure_tailwind, write_app_files

ALLOWED_MODULES = frozenset({
    "fastapi", "starlette", "pydantic", "jinja2", "typing", "datetime", "math", "random", "json", "re",
    "statistics", "decimal", "fractions", "pathlib", "uuid", "collections", "dataclasses", "enum",
    "itertools", "functools", "string", "time", "html", "textwrap", "calendar", "__future__",
})
ALLOWED_SUBMODULES = frozenset({"urllib.parse"})
BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "breakpoint", "getattr", "setattr", "delattr",
    "globals", "locals",
})
ALLOWED_DUNDERS = frozenset({"__name__", "__file__", "__doc__", "__annotations__"})
SMOKE_TIMEOUT = 60

_SMOKE_RUNNER = r"""
import json, sys, traceback, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, sys.argv[1])
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import index
client = TestClient(index.app)
results = []
for route in index.app.routes:
    if not isinstance(route, APIRoute) or "{" in route.path:
        continue
    for method in sorted(route.methods & {"GET", "POST"}):
        item = {"method": method, "path": route.path}
        try:
            response = client.request(method, route.path, data={} if method == "POST" else None)
            item.update(status=response.status_code, html="<html" in response.text.lower())
        except Exception:
            item.update(status=500, error=traceback.format_exc(limit=-3))
        results.append(item)
print("FDM_SMOKE" + json.dumps(results))
"""


def _module_allowed(name: str) -> bool:
    return name in ALLOWED_SUBMODULES or name.split(".")[0] in ALLOWED_MODULES


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _strip_main_block(source: str) -> str:
    """Drop `if __name__ == "__main__":` blocks; they usually import uvicorn, which Vercel doesn't need."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines(keepends=True)
    for node in reversed(tree.body):
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            del lines[node.lineno - 1 : node.end_lineno]
    return "".join(lines).rstrip() + "\n"


def normalize_app(code: AppCode) -> AppCode:
    return AppCode(index_py=_strip_main_block(code.index_py), index_html=ensure_tailwind(code.index_html))


def _forbidden_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name not in ALLOWED_DUNDERS


def _defines_app(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "app" for t in targets) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name == "FastAPI":
            return True
    return False


def template_problems(html: str) -> list[str]:
    problems = []
    try:
        Environment(autoescape=True).parse(html)
    except TemplateSyntaxError as exc:
        problems.append(f"templates/index.html line {exc.lineno}: Jinja syntax error: {exc.message}")
    if "<html" not in html.lower():
        problems.append("templates/index.html must be a complete HTML document")
    if "url_for(" in html or re.search(r"""(?:src|href)\s*=\s*["']/?static/""", html, re.I):
        problems.append("templates/index.html must not reference static files or url_for(); inline everything")
    if re.search(r"{%-?\s*(?:extends|include|import|from)\b", html):
        problems.append("templates/index.html must be self-contained (no extends/include/import)")
    return problems


def static_problems(code: AppCode) -> list[str]:
    problems = template_problems(code.index_html)
    try:
        tree = ast.parse(code.index_py, filename="index.py")
    except SyntaxError as exc:
        return [f"index.py line {exc.lineno}: syntax error: {exc.msg}", *problems]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            problems += [
                f"index.py line {node.lineno}: import of '{alias.name}' is not allowed"
                for alias in node.names
                if not _module_allowed(alias.name)
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module or not _module_allowed(node.module):
                problems.append(f"index.py line {node.lineno}: import from '{node.module or '.'}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_NAMES or _forbidden_dunder(node.id)):
            problems.append(f"index.py line {node.lineno}: use of '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute) and _forbidden_dunder(node.attr):
            problems.append(f"index.py line {node.lineno}: access to '.{node.attr}' is not allowed")

    if not _defines_app(tree):
        problems.append("index.py must define a module-level `app = FastAPI(...)`")
    if not re.search(r"""["']index\.html["']""", code.index_py):
        problems.append("index.py never renders 'index.html'")
    return problems


def smoke_test(path: Path) -> list[str]:
    """Import the app in a child process and request every parameter-free GET/POST route."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _SMOKE_RUNNER, str(path.resolve())],
            cwd=path, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return [f"the app did not finish its smoke test within {SMOKE_TIMEOUT}s (blocking code at import time?)"]

    marker = next((line for line in proc.stdout.splitlines() if line.startswith("FDM_SMOKE")), None)
    if proc.returncode != 0 or marker is None:
        return [f"index.py failed to import or start:\n{proc.stderr.strip()[-1500:]}"]

    problems = []
    results = json.loads(marker.removeprefix("FDM_SMOKE"))
    home = next((r for r in results if r["method"] == "GET" and r["path"] == "/"), None)
    if home is None:
        problems.append("there is no GET / route")
    elif home["status"] != 200 or not home.get("html"):
        problems.append(f"GET / must return the rendered HTML page with status 200, got {home['status']}")
    for result in results:
        if result["status"] >= 500:
            detail = result.get("error", "").strip()
            problems.append(f"{result['method']} {result['path']} raised a server error (500):\n{detail[-1200:]}")
    return problems


def validate_app(code: AppCode, workdir: Path, *, smoke: bool = True) -> list[str]:
    """Return a list of problems (empty means the app is safe to ship). Writes the files to workdir."""
    problems = static_problems(code)
    if problems:
        return problems
    write_app_files(workdir, code)
    return smoke_test(workdir) if smoke else []
