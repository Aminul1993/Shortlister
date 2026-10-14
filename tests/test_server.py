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
