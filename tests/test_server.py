import pytest
from flask import Flask

import fdm.server as server


def test_main_serves_the_portal_from_environment_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here, so only the variables below apply
    monkeypatch.setenv("FDM_HOST", "0.0.0.0")
    monkeypatch.setenv("FDM_PORT", "8123")
    monkeypatch.setenv("FDM_SHORTLIST_SIZE", "5")
    calls = []
    monkeypatch.setattr(server, "serve", lambda app, **options: calls.append((app, options)))

    assert server.main() == 0
    (app, options), = calls
    assert isinstance(app, Flask) and "index" in app.view_functions
    assert options == {"host": "0.0.0.0", "port": 8123, "threads": server.THREADS}


def test_main_stops_on_an_unusable_setting(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FDM_LLM_CONCURRENCY", "0")
    monkeypatch.setattr(server, "serve", lambda app, **options: pytest.fail("served with a bad setting"))

    assert server.main() == 2
    assert "FDM_LLM_CONCURRENCY must be at least 1, not 0" in capsys.readouterr().err
