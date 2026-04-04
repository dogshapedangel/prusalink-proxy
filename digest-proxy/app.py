import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

logging.basicConfig(
    level=os.environ.get("DIGEST_PROXY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)

UPSTREAM = os.environ["PRUSALINK_URL"].rstrip("/")
USERNAME = os.environ["PRUSALINK_USERNAME"]
PASSWORD = os.environ["PRUSALINK_PASSWORD"]
PORT = int(os.environ.get("DIGEST_PROXY_PORT", "8080"))
TIMEOUT = float(os.environ.get("DIGEST_PROXY_TIMEOUT", "120"))
UPSTREAM_HOST = urlsplit(UPSTREAM).netloc

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
RESPONSE_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {"www-authenticate"}
REQUEST_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {"authorization", "host"}

session = requests.Session()
auth = HTTPDigestAuth(USERNAME, PASSWORD)


class DigestProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._proxy_request()

    def do_HEAD(self):
        self._proxy_request()

    def do_POST(self):
        self._proxy_request()

    def do_PUT(self):
        self._proxy_request()

    def do_PATCH(self):
        self._proxy_request()

    def do_DELETE(self):
        self._proxy_request()

    def do_OPTIONS(self):
        self._proxy_request()

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _read_body(self):
        content_length = self.headers.get("Content-Length")
        if not content_length:
            return None
        try:
            length = int(content_length)
        except ValueError:
            return None
        return self.rfile.read(length) if length > 0 else b""

    def _proxy_request(self):
        url = f"{UPSTREAM}{self.path}"
        body = self._read_body()

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in REQUEST_HEADERS_TO_STRIP
        }
        headers["Host"] = UPSTREAM_HOST

        try:
            response = session.request(
                method=self.command,
                url=url,
                headers=headers,
                data=body,
                allow_redirects=False,
                stream=True,
                timeout=TIMEOUT,
                auth=auth,
            )
        except requests.RequestException as exc:
            logging.exception("Upstream request failed for %s %s", self.command, self.path)
            message = f"PrusaLink upstream request failed: {exc}\n".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            return

        if response.status_code == 401:
            logging.error("PrusaLink rejected the configured credentials for %s %s", self.command, self.path)
            message = b"PrusaLink rejected the configured proxy credentials.\n"
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(message)
            response.close()
            return

        self.send_response(response.status_code)
        self.send_header("Connection", "close")
        for key, value in response.headers.items():
            if key.lower() not in RESPONSE_HEADERS_TO_STRIP:
                self.send_header(key, value)
        self.end_headers()

        if self.command != "HEAD":
            response.raw.decode_content = False
            for chunk in response.raw.stream(64 * 1024, decode_content=False):
                if chunk:
                    self.wfile.write(chunk)
            self.wfile.flush()

        self.close_connection = True
        response.close()


if __name__ == "__main__":
    logging.info("Starting digest proxy on 0.0.0.0:%s -> %s", PORT, UPSTREAM)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), DigestProxyHandler)
    server.serve_forever()
