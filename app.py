"""Start the Shortlister portal: `python app.py`, then open http://127.0.0.1:5000.

Settings come from .env (see .env.example); there are no command-line options.
"""

import os
from pathlib import Path

from fdm.server import main

if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)  # so .env and output/ are found wherever it is started from
    raise SystemExit(main())
