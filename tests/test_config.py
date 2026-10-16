import pytest

from fdm.config import ConfigError, Settings

VARIABLES = ("FDM_HOST", "FDM_PORT", "FDM_TRUSTED_HOSTS", "FDM_LLM_CONCURRENCY", "FDM_RESULTS_PER_QUERY",
             "FDM_SHORTLIST_SIZE", "FDM_LOG_LEVEL", "OLLAMA_API_KEY", "FREELANCER_OAUTH_TOKEN")


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Settings.load() with only the variables given, and no .env file."""
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)

    def load_with(**env: str) -> Settings:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Settings.load(env_file=tmp_path / "missing.env")

    return load_with


def test_defaults(load):
    settings = load()
    assert (settings.host, settings.port, settings.llm_concurrency, settings.log_level) == ("127.0.0.1", 5000, 2,
                                                                                          "INFO")
    assert settings.trusted_hosts == ("127.0.0.1", "localhost", "::1")


@pytest.mark.parametrize(("name", "value", "message"), [
    ("FDM_PORT", "eighty", "FDM_PORT='eighty' is not a whole number"),
    ("FDM_PORT", "0", "FDM_PORT must be from 1 to 65535, not 0"),
    ("FDM_PORT", "70000", "FDM_PORT must be from 1 to 65535, not 70000"),
    ("FDM_LLM_CONCURRENCY", "0", "FDM_LLM_CONCURRENCY must be at least 1, not 0"),
    ("FDM_RESULTS_PER_QUERY", "-5", "FDM_RESULTS_PER_QUERY must be at least 1, not -5"),
    ("FDM_SHORTLIST_SIZE", "2.5", "FDM_SHORTLIST_SIZE='2.5' is not a whole number"),
    ("FDM_LOG_LEVEL", "verbose", "FDM_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, not 'VERBOSE'"),
])
def test_unusable_settings_name_the_variable(load, name, value, message):
    with pytest.raises(ConfigError) as caught:
        load(**{name: value})
    assert str(caught.value) == message


def test_settings_are_parsed(load):
    settings = load(FDM_PORT=" 8123 ", FDM_LOG_LEVEL="debug", FDM_LLM_CONCURRENCY="4")
    assert (settings.port, settings.log_level, settings.llm_concurrency) == (8123, "DEBUG", 4)


@pytest.mark.parametrize(("env", "trusted"), [
    ({"FDM_HOST": "192.168.1.5"}, ("127.0.0.1", "localhost", "::1", "192.168.1.5")),
    ({"FDM_HOST": "0.0.0.0"}, ("127.0.0.1", "localhost", "::1")),  # a wildcard names no host to trust
    ({"FDM_HOST": "::"}, ("127.0.0.1", "localhost", "::1")),
    ({"FDM_HOST": "fe80::1"}, ("127.0.0.1", "localhost", "::1", "fe80::1")),
    ({"FDM_HOST": "0.0.0.0", "FDM_TRUSTED_HOSTS": "Portal.LAN, [fe80::1], localhost"},
     ("127.0.0.1", "localhost", "::1", "portal.lan", "fe80::1")),
])
def test_trusted_hosts(load, env, trusted):
    assert load(**env).trusted_hosts == trusted


def test_secrets_stay_out_of_the_repr(load):
    settings = load(OLLAMA_API_KEY="ollama-secret", FREELANCER_OAUTH_TOKEN="fl-secret")
    assert settings.ollama_api_key == "ollama-secret" and settings.freelancer_oauth_token == "fl-secret"
    assert "secret" not in repr(settings)
