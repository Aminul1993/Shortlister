"""Runtime settings, read from environment variables and an optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
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


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    values = tuple(part.strip() for part in os.getenv(name, "").split(",") if part.strip())
    return values or default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Settings:
    ollama_api_key: str
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
    freelancer_oauth_token: str = ""
    # The portal server. It has no login, so keep it bound to localhost unless something else guards it.
    host: str = "127.0.0.1"
    port: int = 5000
    shortlist_size: int = 10
    log_level: str = "INFO"

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] = ".env") -> Settings:
        load_dotenv(env_file, override=False)
        return cls(
            ollama_api_key=os.getenv("OLLAMA_API_KEY", "").strip(),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com").strip().rstrip("/"),
            ollama_models=_csv("OLLAMA_MODELS", DEFAULT_MODELS),
            output_dir=Path(os.getenv("FDM_OUTPUT_DIR", "output")),
            search_queries=_csv("FDM_SEARCH_QUERIES", DEFAULT_QUERIES),
            rss_feeds=_csv("FDM_RSS_FEEDS", ()),
            results_per_query=_int("FDM_RESULTS_PER_QUERY", 50),
            llm_concurrency=_int("FDM_LLM_CONCURRENCY", 2),
            freelancer_name=os.getenv("FDM_FREELANCER_NAME", "").strip(),
            freelancer_profile=os.getenv("FDM_FREELANCER_PROFILE", "").strip(),
            freelancer_oauth_token=os.getenv("FREELANCER_OAUTH_TOKEN", "").strip(),
            host=os.getenv("FDM_HOST", "").strip() or "127.0.0.1",
            port=_int("FDM_PORT", 5000),
            shortlist_size=_int("FDM_SHORTLIST_SIZE", 10),
            log_level=os.getenv("FDM_LOG_LEVEL", "").strip().upper() or "INFO",
        )
