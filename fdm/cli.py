"""Command line entry point: search -> rank -> shortlist -> generate -> deploy."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .deploy import DeployError, deploy_all, discover_projects, preflight
from .generator import generate_all
from .llm import LLMAuthError
from .models import DeployResult, GeneratedProject
from .ranking import rank_gigs
from .sources import fetch_gigs
from .store import save_shortlist, write_json
from .web import create_app

log = logging.getLogger("fdm")


def parse_selection(text: str, count: int) -> list[int]:
    """'1, 4, 7' -> [1, 4, 7]; 'all' -> every rank. Raises ValueError on bad input."""
    text = text.strip().lower()
    if text in {"all", "*"}:
        return list(range(1, count + 1))
    picks: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ValueError(f"'{part}' is not a rank number")
        rank = int(part)
        if not 1 <= rank <= count:
            raise ValueError(f"{rank} is outside 1-{count}")
        if rank not in picks:
            picks.append(rank)
    if not picks:
        raise ValueError("pick at least one project")
    return picks


def prompt_selection(count: int) -> list[int]:
    while True:
        try:
            raw = input(f"\nShortlist projects to build: ranks like '1, 4, 7', 'all', or 'q' to quit (1-{count}): ")
        except EOFError:
            return []
        if raw.strip().lower() in {"q", "quit", "exit"}:
            return []
        try:
            return parse_selection(raw, count)
        except ValueError as exc:
            print(f"  {exc}. Try again.")


def render_generation(projects: list[GeneratedProject]) -> str:
    lines = [f"Generated {len(projects)} project(s):"]
    for project in projects:
        how = "FALLBACK app (agent output failed validation)" if project.used_fallback else f"built by {project.model}"
        lines.append(f"  - {project.project_id}: {how} -> {project.path}")
    return "\n".join(lines)


def _gig_link(project_dir: Path) -> str:
    try:
        gig = json.loads((project_dir / ".fdm" / "gig.json").read_text(encoding="utf-8"))["gig"]
        return f"[{gig['platform']}]({gig['url']})"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def render_deploy_report(results: list[DeployResult], gig_links: dict[str, str]) -> str:
    lines = ["| # | Project | Gig | Status | Live URL |", "|---:|---|---|---|---|"]
    for i, result in enumerate(results, 1):
        status = "live" if result.ok else f"failed: {' '.join((result.error or '').split())[:120]}"
        gig = gig_links.get(result.project_id, "")
        lines.append(f"| {i} | {result.project_id} | {gig} | {status} | {result.live_url or ''} |")
    return "\n".join(lines)


def _report_deploys(results: list[DeployResult], paths: list[Path], output_dir: Path) -> int:
    report = render_deploy_report(results, {p.name: _gig_link(p) for p in paths})
    print(f"\n## Deployments\n\n{report}\n")
    (output_dir / "deployments.md").write_text(report + "\n", encoding="utf-8")
    write_json(output_dir / "deployments.json", [asdict(r) for r in results])
    return 0 if all(r.ok for r in results) else 1


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if not args.rank_only and not settings.ollama_api_key:
        print("OLLAMA_API_KEY is not set (see .env.example). Use --rank-only to just see the shortlist.")
        return 2
    cli = None
    if not args.rank_only and not args.skip_deploy:
        try:
            cli = asyncio.run(preflight(settings))
        except DeployError as exc:
            print(f"Deployment preflight failed: {exc}\nFix it or pass --skip-deploy.")
            return 2

    # Phase 1: aggregate and rank.
    gigs = asyncio.run(fetch_gigs(settings))
    if not gigs:
        print("No gigs were fetched; check your network connection and FDM_* settings.")
        return 1
    ranked = rank_gigs(gigs)[: args.top]
    table = save_shortlist(ranked, settings.output_dir)
    print(f"\n## Top {len(ranked)} easiest opportunities ({len(gigs)} scanned)\n\n{table}\n")
    if args.rank_only:
        print(f"Saved the shortlist with links to {settings.output_dir / 'top10.md'}")
        return 0

    try:
        ranks = parse_selection(args.select, len(ranked)) if args.select else prompt_selection(len(ranked))
    except ValueError as exc:
        print(f"Invalid --select: {exc}")
        return 2
    if not ranks:
        print("Nothing selected.")
        return 0
    chosen = [ranked[r - 1] for r in ranks]

    # Phase 2: generate one demo app per shortlisted gig.
    try:
        projects = asyncio.run(generate_all(chosen, settings, smoke=not args.no_smoke))
    except LLMAuthError as exc:
        print(f"Generation stopped: {exc}")
        return 2
    print("\n" + render_generation(projects))
    if args.skip_deploy:
        print("\nSkipped deployment. Deploy later with: python -m fdm deploy")
        return 0

    # Phases 3-4: authenticated bulk deployment.
    paths = [p.path for p in projects]
    results = asyncio.run(deploy_all(paths, settings, cli))
    return _report_deploys(results, paths, settings.output_dir)


def cmd_deploy(args: argparse.Namespace, settings: Settings) -> int:
    paths = discover_projects(settings.output_dir)
    if not paths:
        print(f"No generated projects found in {settings.output_dir}")
        return 1
    try:
        results = asyncio.run(deploy_all(paths, settings))
    except DeployError as exc:
        print(f"Deployment failed: {exc}")
        return 2
    return _report_deploys(results, paths, settings.output_dir)


def cmd_portal(args: argparse.Namespace, settings: Settings) -> int:
    app = create_app(settings, top=args.top)
    print(f"Freelance Demo Machine portal: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser = argparse.ArgumentParser(prog="python -m fdm", description="Freelance Demo Machine")
    commands = parser.add_subparsers(dest="command")

    run = commands.add_parser("run", parents=[common], help="search, rank, shortlist, generate and deploy (default)")
    run.add_argument("--top", type=int, default=10, help="how many ranked gigs to show (default 10)")
    run.add_argument("--select", help="ranks to build, e.g. '1,4,7' or 'all'; skips the interactive prompt")
    run.add_argument("--rank-only", action="store_true", help="stop after printing the ranked table")
    run.add_argument("--skip-deploy", action="store_true", help="generate projects but do not deploy them")
    run.add_argument("--no-smoke", action="store_true",
                     help="skip importing and requesting generated apps locally (static checks still run)")

    commands.add_parser("deploy", parents=[common], help="deploy every generated project in the output directory")

    portal = commands.add_parser("portal", parents=[common], help="web portal to pick gigs and build projects")
    portal.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1; the portal has no login, keep it local)")
    portal.add_argument("--port", type=int, default=5000)
    portal.add_argument("--top", type=int, default=10, help="shortlist size when refreshing (default 10)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(arg in {"run", "deploy", "portal", "-h", "--help"} for arg in argv):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = Settings.load()
    if args.command == "deploy":
        return cmd_deploy(args, settings)
    if args.command == "portal":
        return cmd_portal(args, settings)
    return cmd_run(args, settings)
