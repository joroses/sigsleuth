"""Serve the API on the recorded sample, with the clock stopped a minute after
its last delivery, for the browser tests (src/frontend/e2e).

Usage: uv run --locked python tests/serve_sample.py PORT
"""

import sys
import tempfile
from pathlib import Path

import uvicorn

from recorded_sample import build_sample
from sigsleuth import db
from sigsleuth.api import create_app
from sigsleuth.config import Settings


def main() -> None:
    port = int(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        sample = build_sample(Path(directory) / "history.db")
        db.fixed_now_ms = sample.now_ms
        app = create_app(Settings(sample.path, default_preset="LongFast"))
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
