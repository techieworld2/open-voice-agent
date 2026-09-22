"""Command line interface."""

import argparse
import uvicorn

from voice_engine.config import Settings


def main() -> None:
    """Run CLI commands."""
    settings = Settings()
    parser = argparse.ArgumentParser(prog="voice-engine")
    sub = parser.add_subparsers(dest="command")
    dev = sub.add_parser("dev")
    dev.add_argument("--host", default=settings.host)
    dev.add_argument("--port", type=int, default=settings.port)
    args = parser.parse_args()
    uvicorn.run(
        "voice_engine.server.app:app",
        host=getattr(args, "host", settings.host),
        port=getattr(args, "port", settings.port),
    )
