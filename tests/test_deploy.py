import asyncio
import json
import sys
import textwrap

import httpx
import pytest

from fdm.deploy import DeployError, deploy_project, discover_projects, parse_vercel_output, verify_vercel_token

DEPLOY_URL = "https://shop-demo-abc123-me.vercel.app"
ALIAS_URL = "https://shop-demo.vercel.app"
STDERR = (
    "Vercel CLI 48.0.0\n"
    "🔍  Inspect: https://vercel.com/me/shop-demo/xyz [2s]\n"
    f"✅  Production: {DEPLOY_URL} [2s]\n"
    f"🔗  Aliased: {ALIAS_URL} [3s]\n"
)


def test_parse_vercel_output_prefers_alias():
    assert parse_vercel_output(f"{DEPLOY_URL}\n", STDERR) == (ALIAS_URL, DEPLOY_URL)


def test_parse_vercel_output_without_alias():
    assert parse_vercel_output(f"{DEPLOY_URL}\n", "Vercel CLI 48.0.0\n") == (None, DEPLOY_URL)
    assert parse_vercel_output("", f"Production: {DEPLOY_URL}\n") == (None, DEPLOY_URL)


def test_verify_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer good":
            return httpx.Response(200, json={"user": {"username": "aminul"}})
        return httpx.Response(403, json={"error": {"code": "forbidden"}})

    transport = httpx.MockTransport(handler)
    assert asyncio.run(verify_vercel_token("good", transport=transport)) == "aminul"
    with pytest.raises(DeployError):
        asyncio.run(verify_vercel_token("bad", transport=transport))
    with pytest.raises(DeployError):
        asyncio.run(verify_vercel_token(""))


def _fake_cli(tmp_path, body: str) -> list[str]:
    script = tmp_path / "fake_vercel.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]


def _project(tmp_path):
    project = tmp_path / "shop-demo-1"
    (project / "templates").mkdir(parents=True)
    (project / "index.py").write_text("app = None\n", encoding="utf-8")
    return project


def test_deploy_project_injects_config_and_reads_urls(tmp_path):
    cli = _fake_cli(tmp_path, f'''
        import sys
        assert sys.argv[1:4] == ["deploy", "--yes", "--prod"], sys.argv
        sys.stderr.write("Aliased: {ALIAS_URL} [3s]\\n")
        print("{DEPLOY_URL}")
    ''')
    project = _project(tmp_path)
    result = asyncio.run(deploy_project(project, token="secret-token", scope="", cli=cli))
    assert result.ok, result.error
    assert (result.live_url, result.deployment_url) == (ALIAS_URL, DEPLOY_URL)
    config = json.loads((project / "vercel.json").read_text(encoding="utf-8"))
    assert config["builds"][0] == {"src": "index.py", "use": "@vercel/python",
                                   "config": {"includeFiles": "templates/**"}}
    assert config["routes"] == [{"src": "/(.*)", "dest": "index.py"}]


def test_failed_deploy_reports_error_without_leaking_token(tmp_path):
    cli = _fake_cli(tmp_path, '''
        import sys
        sys.stderr.write("Error: bad things happened with token " + sys.argv[sys.argv.index("--token") + 1])
        sys.exit(1)
    ''')
    result = asyncio.run(deploy_project(_project(tmp_path), token="secret-token", scope="team", cli=cli))
    assert not result.ok
    assert "bad things happened" in result.error
    assert "secret-token" not in result.error


def test_discover_projects(tmp_path):
    _project(tmp_path)
    (tmp_path / "not-a-project").mkdir()
    assert [p.name for p in discover_projects(tmp_path)] == ["shop-demo-1"]
    assert discover_projects(tmp_path / "missing") == []
