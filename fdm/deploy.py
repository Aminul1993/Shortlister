"""Phases 3-4: authenticate with your Vercel token and bulk-deploy generated projects with the Vercel CLI."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

import httpx

from .config import Settings
from .models import DeployResult
from .scaffold import write_vercel_config

log = logging.getLogger(__name__)

VERCEL_API = "https://api.vercel.com"
DEPLOY_TIMEOUT = 600
_VERCEL_URL = re.compile(r"https://[a-z0-9][a-z0-9.-]*\.vercel\.app", re.I)


class DeployError(RuntimeError):
    pass


def vercel_api_client(token: str, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=VERCEL_API, headers={"Authorization": f"Bearer {token}"}, timeout=30,
                             transport=transport)


async def verify_vercel_token(token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Return the account's username, or raise DeployError if the token is missing or rejected."""
    if not token:
        raise DeployError("VERCEL_TOKEN is not set. Create one at https://vercel.com/account/tokens")
    async with vercel_api_client(token, transport) as client:
        try:
            response = await client.get("/v2/user")
        except httpx.HTTPError as exc:
            raise DeployError(f"could not reach the Vercel API: {exc!r}") from exc
    if response.status_code in (401, 403):
        raise DeployError("Vercel rejected VERCEL_TOKEN (expired, revoked or missing scope)")
    if response.status_code != 200:
        raise DeployError(f"Vercel API returned HTTP {response.status_code} when checking the token")
    user = response.json().get("user") or {}
    return user.get("username") or user.get("email") or "unknown"


def find_vercel_cli() -> list[str]:
    if exe := shutil.which("vercel"):
        return [exe]
    if npx := shutil.which("npx"):
        return [npx, "--yes", "vercel@latest"]
    raise DeployError("Vercel CLI not found. Install Node.js, then run: npm i -g vercel")


def parse_vercel_output(stdout: str, stderr: str) -> tuple[str | None, str | None]:
    """Return (production_alias, deployment_url) from `vercel deploy --prod` output.

    The CLI prints the unique deployment URL on stdout and logs the stable production alias
    (e.g. "Aliased: https://name.vercel.app") on stderr.
    """
    stdout_urls = _VERCEL_URL.findall(stdout)
    deployment = stdout_urls[-1] if stdout_urls else None
    alias = None
    for line in stderr.splitlines():
        match = _VERCEL_URL.search(line)
        if match and "alias" in line.lower():
            alias = match.group(0)
    if deployment is None:
        others = [url for url in _VERCEL_URL.findall(stderr) if url != alias]
        deployment = others[-1] if others else None
    return alias, deployment


async def _lookup_alias(api: httpx.AsyncClient, deployment_url: str, scope: str) -> str | None:
    """Ask the Vercel API for the shortest *.vercel.app production alias of a deployment."""
    host = deployment_url.removeprefix("https://")
    try:
        response = await api.get(f"/v13/deployments/{host}", params={"slug": scope} if scope else None)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    aliases = [a for a in response.json().get("alias") or [] if a.endswith(".vercel.app")]
    return f"https://{min(aliases, key=len)}" if aliases else None


def _tail(text: str, secret: str, limit: int = 600) -> str:
    text = text.replace(secret, "***") if secret else text
    return text.strip()[-limit:] or "no output"


async def deploy_project(path: Path, *, token: str, scope: str, cli: list[str],
                         api: httpx.AsyncClient | None = None, timeout: int = DEPLOY_TIMEOUT) -> DeployResult:
    write_vercel_config(path)
    command = [*cli, "deploy", "--yes", "--prod", "--token", token]
    if scope:
        command += ["--scope", scope]
    log.info("[%s] vercel deploy --yes --prod", path.name)

    proc = await asyncio.create_subprocess_exec(
        *command, cwd=path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "VERCEL_TELEMETRY_DISABLED": "1"},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return DeployResult(path.name, ok=False, error=f"vercel deploy timed out after {timeout}s")

    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if proc.returncode != 0:
        return DeployResult(path.name, ok=False, error=_tail(stderr or stdout, token))

    alias, deployment = parse_vercel_output(stdout, stderr)
    if deployment is None:
        return DeployResult(path.name, ok=False, error="deploy succeeded but no URL was found in the CLI output")
    if alias is None and api is not None:
        alias = await _lookup_alias(api, deployment, scope)
    # Production aliases are public; unique deployment URLs may sit behind Vercel deployment protection.
    return DeployResult(path.name, ok=True, live_url=alias or deployment, deployment_url=deployment)


def discover_projects(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return sorted(p for p in output_dir.iterdir() if (p / "index.py").is_file() and (p / "templates").is_dir())


async def preflight(settings: Settings) -> list[str]:
    """Fail fast before any LLM tokens are spent: check the token and locate the CLI."""
    username = await verify_vercel_token(settings.vercel_token)
    cli = find_vercel_cli()
    log.info("Vercel token OK (account: %s); CLI: %s", username, " ".join(cli))
    return cli


async def deploy_all(paths: list[Path], settings: Settings, cli: list[str] | None = None) -> list[DeployResult]:
    if cli is None:
        cli = await preflight(settings)
    semaphore = asyncio.Semaphore(settings.deploy_concurrency)

    async with vercel_api_client(settings.vercel_token) as api:
        async def deploy_one(path: Path) -> DeployResult:
            async with semaphore:
                try:
                    result = await deploy_project(path, token=settings.vercel_token, scope=settings.vercel_scope,
                                                  cli=cli, api=api)
                except OSError as exc:
                    result = DeployResult(path.name, ok=False, error=f"could not run the Vercel CLI: {exc}")
            log.info("[%s] %s", path.name, result.live_url if result.ok else f"FAILED: {result.error}")
            return result

        return list(await asyncio.gather(*(deploy_one(p) for p in paths)))
