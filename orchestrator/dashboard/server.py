"""Read-only live operator dashboard; no external scripts or anonymous remote access."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from orchestrator.dashboard.auth import DashboardUnauthorizedError, build_dashboard_auth
from orchestrator.delivery_metrics import observations, operational_snapshot


def make_server(cfg, *, port=8765):
    auth = build_dashboard_auth(cfg)
    allowed_hosts = {"localhost", "127.0.0.1", "::1", auth.bind_address} | set(
        cfg.get("dashboard_allowed_hosts", [])
    )
    trusted_proxies = set(cfg.get("dashboard_trusted_proxies", ["127.0.0.1", "::1"]))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not record query strings, credentials or raw request text.

        def do_GET(self):
            try:
                host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            except ValueError:
                self._send(400, b"Invalid Host", "text/plain")
                return
            if host not in allowed_hosts:
                self._send(403, b"Host is not allowed", "text/plain")
                return
            try:
                if (
                    auth.backend == "tailscale"
                    and self.client_address[0] not in trusted_proxies
                ):
                    raise DashboardUnauthorizedError(
                        "Identity headers require a trusted local proxy"
                    )
                auth.require_read(self.headers)
            except DashboardUnauthorizedError:
                self._send(401, b"Dashboard authentication required", "text/plain")
                return
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    from proof.operations import render_operations_dashboard

                    self._send(
                        200,
                        render_operations_dashboard().encode(),
                        "text/html; charset=utf-8",
                    )
                elif path in {"/api/delivery", "/api/observations"}:
                    result = (
                        operational_snapshot(cfg)
                        if path == "/api/delivery"
                        else observations(cfg)
                    )
                    self._send(
                        200,
                        json.dumps(result, allow_nan=False).encode(),
                        "application/json; charset=utf-8",
                    )
                else:
                    self._send(404, b"Not found", "text/plain")
            except Exception:
                self._send(
                    503,
                    b'{"error":"Operational observations unavailable; no result can be inferred"}',
                    "application/json",
                )

        def do_POST(self):
            self._send(
                405,
                b"Use authenticated goal controls; this dashboard is read-only",
                "text/plain",
            )

        def _send(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            script_hashes = []
            if content_type.startswith("text/html"):
                for script in re.findall(rb"<script>(.*?)</script>", body, re.S):
                    script_hashes.append(
                        "'sha256-"
                        + base64.b64encode(hashlib.sha256(script).digest()).decode()
                        + "'"
                    )
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src "
                + (" ".join(script_hashes) or "'none'")
                + "; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((auth.bind_address, port), Handler)


def main():
    from orchestrator.paths import load_config

    parser = argparse.ArgumentParser(
        description="Serve the private Proof delivery operations dashboard"
    )
    parser.add_argument("--port", type=int, default=8765)
    options = parser.parse_args()
    server = make_server(load_config(), port=options.port)
    print(
        f"Delivery dashboard listening at {server.server_address[0]}:{server.server_address[1]}"
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
