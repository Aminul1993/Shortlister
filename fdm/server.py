"""Entry point: `python app.py` (or `python -m fdm`) serves the web portal, where every operation happens.

There are no command-line options: the host, port and everything else come from environment
variables and .env (see fdm.config). The portal runs on Waitress, a production WSGI server, in a
single process, because jobs and their progress live in that process's memory.
"""

from __future__ import annotations

import logging
import sys

from waitress import serve

from .config import Settings
from .web import create_app

log = logging.getLogger("fdm")

# Refreshing the shortlist holds a thread for about 15 seconds while job pages keep polling.
THREADS = 8


def main() -> int:
    settings = Settings.load()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if settings.host not in {"127.0.0.1", "localhost", "::1"}:
        log.warning("Listening on %s: the portal has no login, so anyone who can reach it can spend your "
                    "Ollama tokens. Keep FDM_HOST=127.0.0.1 unless something else guards it.", settings.host)
    log.info("Shortlister portal: http://%s:%s  (Ctrl+C to stop)", settings.host, settings.port)
    serve(create_app(settings), host=settings.host, port=settings.port, threads=THREADS)
    return 0
