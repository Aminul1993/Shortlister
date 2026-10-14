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
    max_repairs: int

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
            max_repairs=_int("FDM_MAX_REPAIRS", 2),
        )
