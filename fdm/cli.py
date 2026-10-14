"""Entry point: `python -m fdm` starts the web portal, where every operation happens."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import Settings
from .web import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m fdm",
                                     description="Shortlister portal: shortlist gigs and screenshot demos")
    parser.add_argument("--host", default="127.0.0.1",
                        help="interface to bind (default 127.0.0.1; the portal has no login, keep it local)")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--top", type=int, default=10, help="shortlist size when refreshing (default 10)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app = create_app(Settings.load(), top=args.top)
    print(f"Shortlister portal: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    app.run(host=args.host, port=args.port, threaded=True)
    return 0
