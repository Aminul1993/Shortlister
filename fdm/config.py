"""Runtime settings, read from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Ollama Cloud models, largest first. fdm.llm falls back down this chain when a
# model reports a rate-limit or token-limit error.
DEFAULT_MODELS = ("kimi-k2.7-code", "gpt-oss:120b", "gpt-oss:20b")

# Keyword searches run in addition to the "latest gigs" feed.
DEFAULT_QUERIES = (
    "landing page",
    "simple website",
    "calculator",
    "fastapi",
    "flask",
    "dashboard",
    "form",
    "api",
)

# Host names that always reach this machine. Trusting them is safe: another site can't make a browser send them.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
WILDCARD_HOSTS = ("0.0.0.0", "::")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(ValueError):
    """A setting is unusable. The message names the variable."""


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())
    return values or default


def _int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a whole number") from None
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"from {minimum} to {maximum}" if maximum is not None else f"at least {minimum}"
        raise ConfigError(f"{name} must be {bounds}, not {value}")
    return value


def _log_level() -> str:
    level = os.getenv("FDM_LOG_LEVEL", "").strip().upper() or "INFO"
    if level not in LOG_LEVELS:
        raise ConfigError(f"FDM_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, not {level!r}")
    return level


def _trusted_hosts(bind: str) -> tuple[str, ...]:
    """The host names the portal answers to: localhost, the address it listens on, and FDM_TRUSTED_HOSTS.

    A wildcard address like 0.0.0.0 doesn't say which name you'll open the portal with, so that name has to be
    listed in FDM_TRUSTED_HOSTS.
    """
    names = (*LOCAL_HOSTS, *(() if bind in WILDCARD_HOSTS else (bind,)), *_csv("FDM_TRUSTED_HOSTS", ()))
    return tuple(dict.fromkeys(name.strip("[]").lower() for name in names))


@dataclass(frozen=True)
class Settings:
    # Secrets stay out of the repr, so a logged or diffed Settings doesn't print them.
    ollama_api_key: str = field(repr=False)
    ollama_base_url: str
    ollama_models: tuple[str, ...]
    output_dir: Path
    search_queries: tuple[str, ...]
    rss_feeds: tuple[str, ...]
    results_per_query: int
    llm_concurrency: int
    # Facts the Pitch Writer may use about you. It never invents experience, so leaving these empty is safe.
    freelancer_name: str = ""
    freelancer_profile: str = ""
    # OAuth token for placing bids through Freelancer.com's API (fdm.bids). Without one the portal never bids.
    freelancer_oauth_token: str = field(default="", repr=False)
    # The portal server. It has no login, so keep it bound to localhost unless something else guards it.
    host: str = "127.0.0.1"
    port: int = 5000
    # Any other Host header gets HTTP 400. That stops DNS rebinding: a page on another domain that re-resolves
    # to this machine would otherwise be same-origin with the portal, and could place bids as you.
    trusted_hosts: tuple[str, ...] = LOCAL_HOSTS
    shortlist_size: int = 10
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] = ".env") -> Settings:
        """Read the settings. Raises ConfigError, naming the variable, if one is unusable."""
        load_dotenv(env_file, override=False)
        host = os.getenv("FDM_HOST", "").strip() or "127.0.0.1"
        return cls(
            ollama_api_key=os.getenv("OLLAMA_API_KEY", "").strip(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com").strip().rstrip("/"),
            ollama_models=_csv("OLLAMA_MODELS", DEFAULT_MODELS),
            output_dir=Path(os.getenv("FDM_OUTPUT_DIR", "output")),
            search_queries=_csv("FDM_SEARCH_QUERIES", DEFAULT_QUERIES),
            rss_feeds=_csv("FDM_RSS_FEEDS", ()),
            results_per_query=_int("FDM_RESULTS_PER_QUERY", 50, 1),
            llm_concurrency=_int("FDM_LLM_CONCURRENCY", 2, 1),  # 0 would make every job wait forever
            freelancer_name=os.getenv("FDM_FREELANCER_NAME", "").strip(),
            freelancer_profile=os.getenv("FDM_FREELANCER_PROFILE", "").strip(),
            freelancer_oauth_token=os.getenv("FREELANCER_OAUTH_TOKEN", "").strip(),
            host=host,
            port=_int("FDM_PORT", 5000, 1, 65535),
            trusted_hosts=_trusted_hosts(host),
            shortlist_size=_int("FDM_SHORTLIST_SIZE", 10, 1),
            log_level=_log_level(),
        )
