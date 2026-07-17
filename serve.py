#!/usr/bin/env python3
"""Minimal static file server for Railway (serves pulse.json and repo files)."""

from __future__ import annotations

import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class FeedHandler(SimpleHTTPRequestHandler):
    """Serve static files with CORS and short cache for the pulse feed."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        # Feed updates a few times a week; 5 minutes is fine for clients.
        if self.path.rstrip("/").endswith("pulse.json") or self.path in ("/", "/pulse.json"):
            self.send_header("Cache-Control", "public, max-age=300")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        super().end_headers()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def list_directory(self, path: str):  # type: ignore[override]
        # No directory listing — keep surface area small.
        self.send_error(404, "Not found")
        return None

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Railway captures stdout; keep access logs one-line.
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), FeedHandler)
    print(f"datacentral-feed listening on 0.0.0.0:{port}", flush=True)
    print(f"  pulse → http://0.0.0.0:{port}/pulse.json", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
