import textwrap

from fdm.agents import default_blueprint, parse_app_code, parse_blueprint
from fdm.models import AppCode, Gig
from fdm.scaffold import fallback_app, make_project_id
from fdm.validate import normalize_app, static_problems, validate_app

GOOD_PY = textwrap.dedent('''
    from pathlib import Path

    from fastapi import FastAPI, Form, Request
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
    app = FastAPI()


    def render(request, total=None, error=""):
        return templates.TemplateResponse(request, "index.html", {"total": total, "error": error})


    @app.get("/")
    async def home(request: Request):
        return render(request)


    @app.post("/")
    async def add(request: Request, a: str = Form(""), b: str = Form("")):
        try:
            return render(request, total=float(a) + float(b))
        except ValueError:
            return render(request, error="Enter two numbers")


    if __name__ == "__main__":
        import uvicorn
        uvicorn.run(app)
''')
GOOD_HTML = (
    "<!DOCTYPE html><html><head><title>Adder</title></head><body>"
    "{% if error %}<p>{{ error }}</p>{% endif %}{% if total is not none %}<p>{{ total }}</p>{% endif %}"
    "</body></html>"
)
GIG = Gig(platform="Freelancer.com", gig_id="40767032", title="Company Information Website Build!",
          description="Need a 5 page website with a contact form.", url="https://example.com/g", skills=["PHP"])


def test_normalize_strips_main_block_and_injects_tailwind():
    code = normalize_app(AppCode(GOOD_PY, GOOD_HTML))
    assert "uvicorn" not in code.index_py
    assert "cdn.tailwindcss.com" in code.index_html
    assert code.index_html.index("<head>") < code.index_html.index("tailwindcss")


def test_good_app_passes_static_and_smoke_checks(tmp_path):
    assert validate_app(normalize_app(AppCode(GOOD_PY, GOOD_HTML)), tmp_path) == []


def test_disallowed_imports_and_calls_are_rejected():
    bad = GOOD_PY.replace("from pathlib import Path", "import os\nimport subprocess\nfrom pathlib import Path")
    bad += "\nresult = eval('1+1')\nleak = ().__class__\n"
    problems = "\n".join(static_problems(AppCode(bad, GOOD_HTML)))
    assert "'os'" in problems
    assert "'subprocess'" in problems
    assert "'eval'" in problems
    assert "__class__" in problems


def test_missing_app_and_template_rules_are_reported():
    problems = "\n".join(static_problems(AppCode("x = 1\n", '<html>{% extends "base.html" %}<img src="/static/a.png">')))
    assert "app = FastAPI" in problems
    assert "self-contained" in problems
    assert "static files" in problems


def test_jinja_syntax_error_is_reported():
    problems = static_problems(AppCode(GOOD_PY, "<html>{% if x %}</html>"))
    assert any("Jinja syntax error" in p for p in problems)


def test_smoke_test_catches_server_errors(tmp_path):
    crashing = GOOD_PY.replace("return render(request)\n", "return render(request, total=1 / 0)\n", 1)
    problems = validate_app(normalize_app(AppCode(crashing, GOOD_HTML)), tmp_path)
    assert any("GET /" in p and "ZeroDivisionError" in p for p in problems)


def test_smoke_test_catches_template_attribute_errors(tmp_path):
    html = GOOD_HTML.replace("<body>", "<body>{{ result.total }}")
    problems = validate_app(normalize_app(AppCode(GOOD_PY, html)), tmp_path)
    assert any("UndefinedError" in p for p in problems)


def test_fallback_app_passes_validation_for_default_and_custom_blueprints(tmp_path):
    custom = parse_blueprint(
        '{"app_title": "Quote \\"Builder\\"", "tagline": "t", "demo_summary": "s", '
        '"routes": [{"method": "POST", "path": "/quote", "purpose": "p", "response": "html", "template_vars": []}], '
        '"template_variables": [], "ui_sections": ["Hero", "Form"], "sample_data": ""}'
    )
    for i, blueprint in enumerate((default_blueprint(GIG), custom)):
        assert validate_app(fallback_app(GIG, blueprint), tmp_path / str(i)) == []


def test_parse_blueprint_adds_home_route_and_caps_routes():
    blueprint = parse_blueprint(
        '{"app_title": "Demo", "routes": ['
        + ",".join(f'{{"method": "POST", "path": "/r{i}"}}' for i in range(6))
        + '], "template_variables": [{"name": "not valid", "type": "x"}, {"name": "items", "type": "list"}]}'
    )
    assert blueprint.routes[0]["path"] == "/" and blueprint.routes[0]["method"] == "GET"
    assert len(blueprint.routes) == 4
    assert [v["name"] for v in blueprint.template_variables] == ["items"]


def test_parse_app_code_extracts_both_blocks():
    reply = f"Sure!\n```python\n{GOOD_PY}\n```\n\n```html\n{GOOD_HTML}\n```\n"
    code = parse_app_code(reply)
    assert "FastAPI()" in code.index_py
    assert code.index_html.startswith("<!DOCTYPE html>")


def test_project_id_is_a_valid_vercel_name():
    assert make_project_id(GIG) == "company-information-website-build-40767032"
