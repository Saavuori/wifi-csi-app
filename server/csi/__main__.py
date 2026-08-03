"""Entry point: `python -m csi`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="csi", description="WiFi CSI ingest and analysis server")
    parser.add_argument("--http-port", type=int, default=None)
    parser.add_argument("--udp-port", type=int, default=None)
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--web-dir", default=None)
    parser.add_argument("--no-record", action="store_true", help="do not auto-start a recording")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # First line in the log, so a journal from a node that has been up for a month still says
    # which build produced everything under it.
    from .version import version_string

    logging.getLogger("csi").info("WiFi CSI server %s", version_string())

    settings = Settings()
    if args.http_port is not None:
        settings.http_port = args.http_port
    if args.udp_port is not None:
        settings.udp_port = args.udp_port
    if args.data_dir:
        from pathlib import Path

        settings.data_dir = Path(args.data_dir).expanduser().resolve()
    if args.web_dir:
        from pathlib import Path

        settings.web_dir = Path(args.web_dir).expanduser().resolve()
    if args.no_record:
        settings.record = False

    uvicorn.run(
        create_app(settings),
        host=settings.http_host,
        port=settings.http_port,
        log_level=args.log_level,
        # The default uvicorn access log prints a line per request; with a WebSocket app that
        # is mostly noise, and it competes with the ingest task for the GIL under load.
        access_log=False,
    )


if __name__ == "__main__":
    main()
