#!/usr/bin/env python3
from __future__ import annotations

import os
import json
import shutil
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class FrontendRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, directory: str, backend_origin: str, **kwargs):
        self.backend_origin = backend_origin.rstrip("/")
        super().__init__(*args, directory=directory, **kwargs)

    def do_GET(self) -> None:
        if self._should_proxy():
            self._proxy_request()
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if self._should_proxy():
            self._proxy_request()
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        self._proxy_request()

    def do_PUT(self) -> None:
        self._proxy_request()

    def do_PATCH(self) -> None:
        self._proxy_request()

    def do_DELETE(self) -> None:
        self._proxy_request()

    def send_head(self):
        parsed = urlsplit(self.path)
        request_path = parsed.path or "/"

        if request_path == "/":
            self.path = "/index.html"
        else:
            translated = Path(self.translate_path(request_path))
            if not translated.exists() and "." not in Path(request_path).name:
                self.path = "/index.html"

        return super().send_head()

    def _should_proxy(self) -> bool:
        path = urlsplit(self.path).path
        return path == "/api" or path.startswith("/api/")

    def _proxy_request(self) -> None:
        target_url = f"{self.backend_origin}{self.path}"
        request_headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
        }

        body = None
        content_length = self.headers.get("Content-Length")
        if content_length:
            body = self.rfile.read(int(content_length))

        request = Request(target_url, data=body, headers=request_headers, method=self.command)

        try:
            with urlopen(request, timeout=120) as response:
                self._write_proxy_response(response.status, response.headers.items(), response)
        except HTTPError as error:
            self._write_proxy_response(error.code, error.headers.items(), error)
        except TimeoutError:
            self._proxy_error(504, "Backend request timed out. Check discovery progress before retrying.")
        except URLError as error:
            status = 504 if isinstance(error.reason, TimeoutError) else 502
            self._proxy_error(status, "Backend request timed out." if status == 504 else "Backend is unavailable.")

    def _proxy_error(self, status, message):
        identifier = self.headers.get("X-Request-ID")
        body = json.dumps({"detail": {"message": message, "request_id": identifier,
                                      "code": "proxy_timeout" if status == 504 else "proxy_unavailable"}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if identifier:
            self.send_header("X-Request-ID", identifier)
        self.end_headers()
        self.wfile.write(body)

    def _write_proxy_response(self, status: int, headers, response) -> None:
        self.send_response(status)
        for key, value in headers:
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            self.send_header(key, value)
        self.end_headers()

        if self.command != "HEAD":
            shutil.copyfileobj(response, self.wfile)


def main() -> None:
    frontend_host = os.environ.get("FRONTEND_HOST", "0.0.0.0")
    frontend_port = int(os.environ.get("FRONTEND_PORT", "5400"))
    backend_origin = os.environ.get("BACKEND_PROXY_ORIGIN", "http://127.0.0.1:5401")
    dist_dir = Path(os.environ.get("FRONTEND_DIST_DIR", Path(__file__).resolve().parents[1] / "frontend" / "dist"))

    if not dist_dir.is_dir():
        raise SystemExit(f"Frontend dist directory not found: {dist_dir}")

    handler = partial(
        FrontendRequestHandler,
        directory=str(dist_dir),
        backend_origin=backend_origin,
    )
    server = ThreadingHTTPServer((frontend_host, frontend_port), handler)

    print(
        f"Serving frontend from {dist_dir} on http://{frontend_host}:{frontend_port} "
        f"with API proxy to {backend_origin}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
